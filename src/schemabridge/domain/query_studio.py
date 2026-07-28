"""Pure, bounded contracts for dynamic governed Query Studio matching.

The values in this module contain neither provider SDK types nor executable SQL. Catalog
metadata, deterministic scores, and model output remain evidence; only a later application
confirmation may invoke the existing guided-request validator.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from typing import Any, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.catalog_inventory import (
    CatalogAssetId,
    CatalogFieldLocator,
)
from schemabridge.domain.concepts import CanonicalType, LogicalFieldRef, LogicalModelRef
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.joins import Cardinality, FanoutPolicy
from schemabridge.domain.request_context import (
    LogicalFieldRole,
    ValidatedAnalyticalRequest,
)
from schemabridge.domain.requests import (
    DateGrain,
    FilterOperator,
    FilterScalar,
    MetricOperation,
    SortDirection,
)
from schemabridge.domain.semantic_change import SemanticChangeStatus
from schemabridge.domain.semantic_registry import (
    PhysicalValueType,
    SemanticRegistryScope,
    semantic_registry_scope_fingerprint,
)

MAX_DESCRIPTION_CHARACTERS = 2_000
MAX_DESCRIPTION_BYTES = 8_192
MAX_DESCRIPTION_PROBES = 6
MAX_GOVERNED_SEARCH_PAGE_SIZE = 50
MAX_GOVERNED_SEARCH_ROWS_READ = 51
MAX_EXECUTABLE_SHORTLIST = 20
MAX_PROMPT_MODELS = 3
MAX_PROMPT_FIELDS = 12
MAX_PROMPT_JOINS = 2
MAX_CANDIDATES_PER_PURPOSE = 2
MAX_PREVIEW_TTL_SECONDS = 600
MAX_PROMPT_BYTES = 32 * 1_024
MAX_GUIDED_LOGICAL_FIELDS = 1_000
MAX_GUIDED_BINDINGS = 2_000
EXTERNAL_AI_ATTEMPT_POLICY_VERSION = "m27-durable-attempts-v3"
LOCAL_AI_ATTEMPT_POLICY_VERSION = "m27-no-egress-v1"
QUERY_STUDIO_ORCHESTRATION_POLICY_VERSION = "m27-local-analytical-preflight-v5"
QUERY_STUDIO_PUBLIC_METADATA_POLICY_VERSION = "m27-approved-public-definition-surface-v1"
_GOVERNED_SEARCH_CONTRACT_VERSION = "m27-v2"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INERT_ID = re.compile(r"^[a-z][a-z0-9_-]{1,79}$")
_BINDING_ID = re.compile(r"^[a-z][a-z0-9_-]{2,199}$")
_MODEL_SNAPSHOT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,119}$")
_VERSION_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_REGION_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_CANDIDATE_ID = re.compile(r"^qsc1_[A-Za-z0-9_-]{32,180}$")
_TOKEN = re.compile(r"^qsp1\.[A-Za-z0-9_-]{16,1800}$")
_CONTROL_EXCEPT_WHITESPACE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_EXPLICIT_LOGICAL_FIELD_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9_.])"
    r"(?P<owner>[A-Za-z][A-Za-z0-9_]{1,39})"
    r"\."
    r"(?P<field>[A-Za-z][A-Za-z0-9_]{1,79})"
    r"(?![A-Za-z0-9_.])"
)
_QUALIFIED_LOOKING_PATH = re.compile(
    r"(?<![A-Za-z0-9_.])"
    r"(?P<path>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)"
    r"(?![A-Za-z0-9_.])"
)


class QueryStudioMode(StrEnum):
    GUIDED = "guided"
    NATURAL_LANGUAGE = "natural_language"


class DescriptionExpansionRoute(StrEnum):
    """Server-owned nominal lane for the description-expansion stage."""

    FIELD_MATCH = "field_match"
    ANALYTICAL = "analytical"


class SemanticMatchState(StrEnum):
    """Business-semantic outcomes, kept separate from infrastructure failures."""

    ALIGNED = "aligned"
    AMBIGUOUS = "ambiguous"
    NO_MATCH = "no_match"
    CONFLICTING = "conflicting"


class QueryStudioOperationalState(StrEnum):
    """Operational outcomes that must never be presented as semantic ambiguity."""

    STALE = "stale"
    CLOSURE_OVERFLOW = "closure_overflow"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    SENSITIVE_INPUT_BLOCKED = "sensitive_input_blocked"


class PhysicalDiscoveryStatus(StrEnum):
    NEEDS_MAPPING_REVIEW = "needs_mapping_review"


class ExecutableEvidenceStatus(StrEnum):
    CURRENT = "current"
    REVALIDATED = "revalidated"


class QueryFieldPurpose(StrEnum):
    PRIMARY_ENTITY = "primary_entity"
    DIMENSION = "dimension"
    METRIC = "metric"
    FILTER = "filter"
    ORDER = "order"


class SearchSignalCode(StrEnum):
    EXACT_LOGICAL_FIELD = "exact_logical_field"
    EXACT_PHYSICAL_FIELD = "exact_physical_field"
    LOGICAL_NAME_OVERLAP = "logical_name_overlap"
    PHYSICAL_NAME_OVERLAP = "physical_name_overlap"
    DEFINITION_OVERLAP = "definition_overlap"
    TAXONOMY_OVERLAP = "taxonomy_overlap"
    TYPE_MATCH = "type_match"
    ROLE_MATCH = "role_match"


class CandidateRiskCode(StrEnum):
    MULTIPLE_LOGICAL_MATCHES = "multiple_logical_matches"
    MULTIPLE_PHYSICAL_BINDINGS = "multiple_physical_bindings"
    HOMONYMOUS_FIELD = "homonymous_field"
    DESCRIPTION_ONLY_EVIDENCE = "description_only_evidence"
    NATIVE_TYPE_DRIFT = "native_type_drift"
    NULLABLE_IDENTIFIER = "nullable_identifier"
    UNSAFE_FLOAT_IDENTIFIER = "unsafe_float_identifier"
    STALE_EVIDENCE = "stale_evidence"


class ProposalAmbiguityKind(StrEnum):
    FIELD_MEANING = "field_meaning"
    METRIC_MEANING = "metric_meaning"
    DATE_MEANING = "date_meaning"
    FILTER_VALUE = "filter_value"
    JOIN_PATH = "join_path"
    UNRESOLVED_REQUEST = "unresolved_request"


class ProviderStage(StrEnum):
    EXPANSION = "expansion"
    INTERPRETATION = "interpretation"


class ProviderOutcomeCode(StrEnum):
    SUCCEEDED = "succeeded"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    REFUSED = "refused"
    MISSING_OUTPUT = "missing_output"
    INVALID_OUTPUT = "invalid_output"


class ProviderOutputFailureCategory(StrEnum):
    """Closed, payload-free reason for rejecting a provider output."""

    OUTPUT_LIMIT = "output_limit"
    SCHEMA_VALIDATION = "schema_validation"
    GROUNDING = "grounding"
    SEMANTIC_CONTRACT = "semantic_contract"


class QueryStudioConfirmationAction(StrEnum):
    CONFIRM_INTERPRETATION = "CONFIRM QUERY STUDIO INTERPRETATION"


class QueryStudioRevisionAction(StrEnum):
    RECOMPUTE_INTERPRETATION = "RECOMPUTE QUERY STUDIO INTERPRETATION"


class OpaqueCandidateId(RootModel[str]):
    """Model-safe HMAC-derived candidate reference with no locator material."""

    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def value_must_be_opaque_and_bounded(cls, value: str) -> str:
        if _CANDIDATE_ID.fullmatch(value) is None:
            raise ValueError("Query Studio candidate id must be a bounded opaque value")
        return value

    def __str__(self) -> str:
        return self.root


class SignedPreviewToken(RootModel[str]):
    """Authenticated token whose decoded payload contains digests, never prompt text."""

    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def token_must_be_bounded(cls, value: str) -> str:
        if _TOKEN.fullmatch(value) is None or len(value.encode("utf-8")) > 2_048:
            raise ValueError("Query Studio preview token is invalid")
        return value

    def __str__(self) -> str:
        return self.root


class PhysicalDiscoveryCursor(RootModel[str]):
    """Opaque discovery cursor deliberately incompatible with governed keysets."""

    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def cursor_must_be_bounded(cls, value: str) -> str:
        if not value or len(value.encode("utf-8")) > 1_024:
            raise ValueError("physical discovery cursor is invalid")
        return value


class DescriptionQuery(RootModel[str]):
    """Unicode-normalized, whitespace-canonical, bounded business text."""

    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def normalize_and_bound(cls, value: str) -> str:
        return normalize_description_text(value)

    @property
    def fingerprint(self) -> str:
        return query_studio_fingerprint({"description": self.root})

    def __str__(self) -> str:
        return self.root


class DescriptionSearchProbe(FrozenDomainModel):
    purpose_id: str = Field(min_length=2, max_length=80)
    query: DescriptionQuery
    source_span: str | None = Field(default=None, min_length=1, max_length=256)
    intended_use: QueryFieldPurpose
    semantic_focus: tuple[str, ...] = Field(default=(), max_length=8)
    owner_focus: tuple[str, ...] = Field(default=(), max_length=3)
    roles: tuple[LogicalFieldRole, ...] = Field(default=(), max_length=4)
    canonical_types: tuple[CanonicalType, ...] = Field(default=(), max_length=6)
    metric_operation: MetricOperation | None = None
    filter_operator: FilterOperator | None = None
    date_grain: DateGrain | None = None

    @field_validator("purpose_id")
    @classmethod
    def purpose_id_must_be_inert(cls, value: str) -> str:
        if _INERT_ID.fullmatch(value) is None:
            raise ValueError("description-search purpose id must be inert")
        return value

    @field_validator("source_span")
    @classmethod
    def source_span_must_be_normalized_and_bounded(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_description_text(value)
        if len(normalized.encode("utf-8")) > 512:
            raise ValueError("description-search source span exceeds 512 bytes")
        return normalized

    @field_validator("semantic_focus", "owner_focus")
    @classmethod
    def focus_anchors_must_be_inert_and_unique(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            re.fullmatch(r"[a-z][a-z0-9_-]{1,39}", value) is None for value in values
        ):
            raise ValueError("description-search focus anchors must be inert and unique")
        return values

    @field_validator("roles", "canonical_types")
    @classmethod
    def set_like_enum_filters_are_stably_deduplicated(
        cls,
        values: tuple[Any, ...],
    ) -> tuple[Any, ...]:
        return tuple(dict.fromkeys(values))

    @model_validator(mode="after")
    def operations_must_match_purpose(self) -> DescriptionSearchProbe:
        if (self.metric_operation is not None) != (self.intended_use is QueryFieldPurpose.METRIC):
            raise ValueError("metric operation must appear only on a metric search purpose")
        if (self.filter_operator is not None) != (self.intended_use is QueryFieldPurpose.FILTER):
            raise ValueError("filter operator must appear only on a filter search purpose")
        if self.date_grain is not None and self.intended_use not in {
            QueryFieldPurpose.DIMENSION,
            QueryFieldPurpose.ORDER,
        }:
            raise ValueError("date grain requires a dimension or ordering purpose")
        if not set(self.owner_focus).issubset(self.semantic_focus):
            raise ValueError("description-search owner focus must be part of semantic focus")
        return self


class DescriptionExpansionInput(FrozenDomainModel):
    text: DescriptionQuery
    language: UserLanguage
    lane: DescriptionExpansionRoute = DescriptionExpansionRoute.ANALYTICAL


class DescriptionExpansion(FrozenDomainModel):
    probes: tuple[DescriptionSearchProbe, ...] = Field(
        min_length=1,
        max_length=MAX_DESCRIPTION_PROBES,
    )
    ambiguity_hints: tuple[ProposalAmbiguityKind, ...] = Field(default=(), max_length=12)

    @field_validator("probes")
    @classmethod
    def probes_must_have_unique_purposes(
        cls,
        values: tuple[DescriptionSearchProbe, ...],
    ) -> tuple[DescriptionSearchProbe, ...]:
        purposes = tuple(value.purpose_id for value in values)
        if len(purposes) != len(set(purposes)):
            raise ValueError("description expansion purposes must be unique")
        return values

    @field_validator("ambiguity_hints")
    @classmethod
    def ambiguity_hints_are_stably_deduplicated(
        cls,
        values: tuple[ProposalAmbiguityKind, ...],
    ) -> tuple[ProposalAmbiguityKind, ...]:
        return tuple(dict.fromkeys(values))

    @property
    def fingerprint(self) -> str:
        return query_studio_fingerprint(self.model_dump(mode="json"))


def classify_description_expansion_route(
    value: DescriptionExpansionInput,
) -> DescriptionExpansionRoute:
    """Return the explicit server-owned lane without interpreting user text."""

    return value.lane


def atomic_description_expansion(value: DescriptionExpansionInput) -> DescriptionExpansion:
    """Build the one fully grounded local probe allowed by the field-match lane."""

    if classify_description_expansion_route(value) is not DescriptionExpansionRoute.FIELD_MATCH:
        raise ValueError("analytical requests cannot use the local field-match expansion")
    source = value.text.root
    if len(source) > 256 or len(source.encode("utf-8")) > 512:
        raise ValueError("atomic field description exceeds the grounded source-span limit")
    return DescriptionExpansion(
        probes=(
            DescriptionSearchProbe(
                purpose_id="atomic_field",
                query=value.text,
                source_span=source,
                intended_use=QueryFieldPurpose.DIMENSION,
            ),
        )
    )


class QueryStudioScopeSnapshot(FrozenDomainModel):
    """Exact current registry, pointer, semantic head, and catalog-vector binding."""

    scope: SemanticRegistryScope
    registry_version: int = Field(ge=1)
    registry_fingerprint: str
    pointer_generation: int = Field(ge=1)
    pointer_fingerprint: str
    evidence_head_revision: int = Field(ge=1)
    evidence_baseline_revision: int = Field(ge=1)
    evidence_baseline_fingerprint: str
    catalog_generation_vector_fingerprint: str

    @field_validator(
        "registry_fingerprint",
        "pointer_fingerprint",
        "evidence_baseline_fingerprint",
        "catalog_generation_vector_fingerprint",
    )
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "Query Studio scope fingerprint")

    @property
    def fingerprint(self) -> str:
        return query_studio_fingerprint(self.model_dump(mode="json"))

    @property
    def scope_digest(self) -> str:
        return semantic_registry_scope_fingerprint(self.scope)


class GuidedBrowseContext(FrozenDomainModel):
    """Short-lived server-side context that keeps opaque IDs stable across keyset pages."""

    scope: QueryStudioScopeSnapshot
    nonce: str = Field(min_length=16, max_length=120)
    issued_at: datetime
    expires_at: datetime

    @field_validator("nonce")
    @classmethod
    def nonce_must_be_opaque(cls, value: str) -> str:
        if not value.isascii() or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
            raise ValueError("guided browse nonce must be opaque")
        return value

    @field_validator("issued_at", "expires_at")
    @classmethod
    def times_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("guided browse times must include a timezone")
        return value

    @model_validator(mode="after")
    def lifetime_must_be_positive_and_bounded(self) -> GuidedBrowseContext:
        seconds = (self.expires_at - self.issued_at).total_seconds()
        if seconds <= 0 or seconds > MAX_PREVIEW_TTL_SECONDS:
            raise ValueError("guided browse context lifetime must be at most ten minutes")
        return self

    @property
    def fingerprint(self) -> str:
        return query_studio_fingerprint(self.model_dump(mode="json"))


class SearchSignal(FrozenDomainModel):
    code: SearchSignalCode
    value: int = Field(ge=0, le=10_000)


class SearchSignalBreakdown(FrozenDomainModel):
    signals: tuple[SearchSignal, ...] = Field(default=(), max_length=8)
    total: int = Field(ge=0, le=80_000)

    @model_validator(mode="after")
    def signals_must_be_canonical_and_totalled(self) -> SearchSignalBreakdown:
        codes = tuple(signal.code.value for signal in self.signals)
        if codes != tuple(sorted(set(codes))):
            raise ValueError("search signals must be sorted and unique")
        if self.total != sum(signal.value for signal in self.signals):
            raise ValueError("search-signal total does not match its components")
        return self

    @classmethod
    def create(cls, signals: tuple[SearchSignal, ...]) -> SearchSignalBreakdown:
        ordered = tuple(sorted(signals, key=lambda item: item.code.value))
        return cls(signals=ordered, total=sum(item.value for item in ordered))


class GovernedSearchKey(FrozenDomainModel):
    """Stable keyset position; no approximate float participates in identity."""

    score: int = Field(ge=0, le=80_000)
    logical_field: LogicalFieldRef
    binding_id: str = Field(min_length=3, max_length=200)
    scope_fingerprint: str
    request_fingerprint: str
    binding_facts_fingerprint: str

    @field_validator("binding_id")
    @classmethod
    def binding_id_must_be_inert(cls, value: str) -> str:
        if _BINDING_ID.fullmatch(value) is None:
            raise ValueError("governed search binding id must be inert")
        return value

    @field_validator(
        "scope_fingerprint",
        "request_fingerprint",
        "binding_facts_fingerprint",
    )
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "governed search fingerprint")

    @property
    def sort_tuple(self) -> tuple[int, str, str]:
        return (-self.score, self.logical_field.root, self.binding_id)


class GovernedFieldSearchFilters(FrozenDomainModel):
    """Registry-owned logical filters accepted by the high-level application port."""

    canonical_types: tuple[CanonicalType, ...] = Field(default=(), max_length=6)
    roles: tuple[LogicalFieldRole, ...] = Field(default=(), max_length=4)
    logical_models: tuple[LogicalModelRef, ...] | None = Field(
        default=None,
        max_length=MAX_GUIDED_LOGICAL_FIELDS,
    )

    @field_validator("canonical_types", "roles")
    @classmethod
    def filters_must_be_unique(cls, values: tuple[Any, ...]) -> tuple[Any, ...]:
        if len(values) != len(set(values)):
            raise ValueError("governed field search filters must be unique")
        return values

    @field_validator("logical_models")
    @classmethod
    def logical_models_must_be_canonical(
        cls,
        values: tuple[LogicalModelRef, ...] | None,
    ) -> tuple[LogicalModelRef, ...] | None:
        if values is None:
            return None
        roots = tuple(value.root for value in values)
        if roots != tuple(sorted(set(roots))):
            raise ValueError("governed field search model filters must be sorted and unique")
        return values


class GovernedBindingFactsFilters(FrozenDomainModel):
    """Exact logical allowlist derived by application from the active registry."""

    logical_fields: tuple[LogicalFieldRef, ...] = Field(default=(), max_length=1_000)
    restrict_logical_fields: bool = False

    @field_validator("logical_fields")
    @classmethod
    def fields_must_be_canonical(
        cls,
        values: tuple[LogicalFieldRef, ...],
    ) -> tuple[LogicalFieldRef, ...]:
        roots = tuple(value.root for value in values)
        if roots != tuple(sorted(set(roots))):
            raise ValueError("governed field allowlist must be sorted and unique")
        return values

    @model_validator(mode="after")
    def unrestricted_filter_cannot_carry_fields(self) -> GovernedBindingFactsFilters:
        if not self.restrict_logical_fields and self.logical_fields:
            raise ValueError("unrestricted governed search cannot carry a logical allowlist")
        return self


class GovernedFieldSearchRequest(FrozenDomainModel):
    scope: SemanticRegistryScope
    query: DescriptionQuery | None = None
    filters: GovernedFieldSearchFilters = GovernedFieldSearchFilters()
    page_size: int = Field(default=20, ge=1, le=MAX_GOVERNED_SEARCH_PAGE_SIZE)
    after: GovernedSearchKey | None = None
    expected_scope: QueryStudioScopeSnapshot | None = None

    @property
    def request_fingerprint(self) -> str:
        return _governed_search_request_fingerprint(
            lane="logical",
            scope=self.scope,
            query=self.query,
            filters=self.filters,
        )

    @model_validator(mode="after")
    def continuation_must_bind_expected_scope(self) -> GovernedFieldSearchRequest:
        if (self.after is None) != (self.expected_scope is None):
            raise ValueError(
                "governed search continuation and expected scope must be supplied together"
            )
        if (
            self.after is not None
            and self.expected_scope is not None
            and self.after.scope_fingerprint != self.expected_scope.fingerprint
        ):
            raise ValueError("governed search continuation scope does not match")
        if self.after is not None and self.after.request_fingerprint != self.request_fingerprint:
            raise ValueError("governed search continuation query or logical filters do not match")
        return self


def governed_probe_search_requests(
    scope: SemanticRegistryScope,
    probe: DescriptionSearchProbe,
    *,
    owner_models: tuple[LogicalModelRef, ...] | None = None,
) -> tuple[GovernedFieldSearchRequest, ...]:
    """Build the bounded production retrieval set for one grounded atomic probe."""

    # Read one sentinel beyond the executable shortlist so a 20/21 boundary
    # tie cannot be silently presented as complete.
    page_size = MAX_EXECUTABLE_SHORTLIST + 1
    requests = [
        GovernedFieldSearchRequest(
            scope=scope,
            query=probe.query,
            filters=GovernedFieldSearchFilters(
                canonical_types=probe.canonical_types,
                roles=probe.roles,
                logical_models=owner_models,
            ),
            page_size=page_size,
        )
    ]
    value_query = _filter_value_query(probe)
    if value_query is not None:
        requests.append(
            GovernedFieldSearchRequest(
                scope=scope,
                query=value_query,
                filters=GovernedFieldSearchFilters(
                    canonical_types=probe.canonical_types,
                    roles=probe.roles,
                    logical_models=owner_models,
                ),
                page_size=page_size,
            )
        )
    count_distinct_identifier = _count_distinct_identifier_query(probe)
    if count_distinct_identifier is not None:
        # COUNT DISTINCT names the governed identifier of exactly one business
        # entity. Models often phrase that field probe as "customer count",
        # which is not itself a catalog field. This deterministic variant is
        # retrieval-only, remains role-restricted, and cannot create mapping or
        # join authority.
        requests.append(
            GovernedFieldSearchRequest(
                scope=scope,
                query=count_distinct_identifier,
                filters=GovernedFieldSearchFilters(
                    roles=(LogicalFieldRole.IDENTIFIER,),
                    canonical_types=probe.canonical_types,
                    logical_models=owner_models,
                ),
                page_size=page_size,
            )
        )
    if (
        probe.source_span is not None
        and probe.source_span.casefold() != probe.query.root.casefold()
    ):
        # The exact source excerpt is an additional governed retrieval signal.
        # It never promotes physical-only or unknown inventory into the lane.
        requests.append(
            GovernedFieldSearchRequest(
                scope=scope,
                query=DescriptionQuery(probe.source_span),
                filters=GovernedFieldSearchFilters(
                    canonical_types=probe.canonical_types,
                    roles=probe.roles,
                    logical_models=owner_models,
                ),
                page_size=page_size,
            )
        )
    return tuple({request.request_fingerprint: request for request in requests}.values())


def _filter_value_query(probe: DescriptionSearchProbe) -> DescriptionQuery | None:
    """Return one bounded owner-and-value retrieval variant for a filter slot."""

    if probe.intended_use is not QueryFieldPurpose.FILTER or not probe.owner_focus:
        return None
    values = tuple(
        anchor
        for anchor in probe.semantic_focus
        if anchor not in probe.owner_focus and anchor not in {"role", "status"}
    )
    if not values:
        return None
    # One value keeps the deterministic matcher above its evidence threshold
    # even when a slot carries multiple contextual focus anchors.
    return DescriptionQuery(" ".join((*probe.owner_focus, values[0])))


_COUNT_DISTINCT_ENTITY_ALIASES = {
    "account": "account",
    "accounts": "account",
    "carrier": "carrier",
    "carriers": "carrier",
    "client": "customer",
    "clients": "customer",
    "cliente": "customer",
    "clientes": "customer",
    "customer": "customer",
    "customers": "customer",
    "device": "device",
    "devices": "device",
    "dispositivo": "device",
    "dispositivos": "device",
    "empleado": "employee",
    "empleados": "employee",
    "employee": "employee",
    "employees": "employee",
    "envio": "shipment",
    "envios": "shipment",
    "factura": "invoice",
    "facturas": "invoice",
    "holder": "holder",
    "holders": "holder",
    "invoice": "invoice",
    "invoices": "invoice",
    "line": "line",
    "lines": "line",
    "linea": "line",
    "lineas": "line",
    "order": "order",
    "orders": "order",
    "pago": "payment",
    "pagos": "payment",
    "payment": "payment",
    "payments": "payment",
    "pedido": "order",
    "pedidos": "order",
    "product": "product",
    "products": "product",
    "producto": "product",
    "productos": "product",
    "proveedor": "supplier",
    "proveedores": "supplier",
    "reembolso": "refund",
    "reembolsos": "refund",
    "refund": "refund",
    "refunds": "refund",
    "sale": "sale",
    "sales": "sale",
    "shipment": "shipment",
    "shipments": "shipment",
    "supplier": "supplier",
    "suppliers": "supplier",
    "ticket": "ticket",
    "tickets": "ticket",
    "titular": "holder",
    "titulares": "holder",
    "transportista": "carrier",
    "transportistas": "carrier",
    "venta": "sale",
    "ventas": "sale",
    "warehouse": "warehouse",
    "warehouses": "warehouse",
}


def _count_distinct_identifier_query(
    probe: DescriptionSearchProbe,
) -> DescriptionQuery | None:
    if (
        probe.intended_use is not QueryFieldPurpose.METRIC
        or probe.metric_operation is not MetricOperation.COUNT_DISTINCT
    ):
        return None
    decomposed = unicodedata.normalize("NFKD", probe.query.root)
    normalized = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    ).casefold()
    tokens = tuple(re.findall(r"[a-z0-9]+", normalized))
    if any(
        token in {"clave", "id", "identificador", "identifier", "key", "reference", "referencia"}
        for token in tokens
    ):
        return None
    entities = {
        entity
        for token in tokens
        if (entity := _COUNT_DISTINCT_ENTITY_ALIASES.get(token)) is not None
    }
    if len(entities) != 1:
        return None
    return DescriptionQuery(f"stable {next(iter(entities))} identifier")


class GovernedBindingFactsRequest(FrozenDomainModel):
    """PostgreSQL-facing request with only an exact application-derived allowlist."""

    scope: SemanticRegistryScope
    query: DescriptionQuery | None = None
    filters: GovernedBindingFactsFilters = GovernedBindingFactsFilters()
    page_size: int = Field(default=20, ge=1, le=MAX_GOVERNED_SEARCH_PAGE_SIZE)
    after: GovernedSearchKey | None = None
    expected_scope: QueryStudioScopeSnapshot | None = None
    logical_request_fingerprint: str | None = None

    @field_validator("logical_request_fingerprint")
    @classmethod
    def logical_fingerprint_must_be_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _sha256(value, "logical governed-search request fingerprint")

    @property
    def request_fingerprint(self) -> str:
        return _governed_search_request_fingerprint(
            lane="binding_facts",
            scope=self.scope,
            query=self.query,
            filters=self.filters,
        )

    @property
    def continuation_request_fingerprint(self) -> str:
        return self.logical_request_fingerprint or self.request_fingerprint

    @model_validator(mode="after")
    def continuation_must_bind_complete_expected_scope(
        self,
    ) -> GovernedBindingFactsRequest:
        if (self.after is None) != (self.expected_scope is None):
            raise ValueError(
                "governed binding continuation and expected scope must be supplied together"
            )
        if (
            self.after is not None
            and self.expected_scope is not None
            and self.after.scope_fingerprint != self.expected_scope.fingerprint
        ):
            raise ValueError("governed binding continuation scope does not match")
        if (
            self.after is not None
            and self.after.request_fingerprint != self.continuation_request_fingerprint
        ):
            raise ValueError("governed binding continuation logical request does not match")
        if (
            self.after is not None
            and self.after.binding_facts_fingerprint != self.request_fingerprint
        ):
            raise ValueError("governed binding continuation query or raw filters do not match")
        return self


class GovernedFieldBinding(FrozenDomainModel):
    """Current approved binding plus bounded public catalog facts from PostgreSQL."""

    binding_id: str = Field(min_length=3, max_length=200)
    binding_fingerprint: str
    logical_field: LogicalFieldRef
    physical_field: PhysicalFieldRef
    locator: CatalogFieldLocator
    mapping_version: int = Field(ge=1)
    mapping_approval_decision_id: str = Field(min_length=1, max_length=200)
    physical_type: PhysicalValueType
    evidence_status: ExecutableEvidenceStatus
    catalog_generation: int = Field(ge=1)
    catalog_generation_fingerprint: str
    asset_qualified_name: str = Field(min_length=1, max_length=500)
    asset_metadata_fingerprint: str
    field_metadata_fingerprint: str
    field_definition_fingerprint: str
    field_terms_fingerprint: str
    native_type: str | None = Field(default=None, max_length=200)
    definition: str | None = Field(default=None, max_length=4_000)
    nullable: bool | None = None
    is_part_of_key: bool | None = None
    tags: tuple[str, ...] = Field(default=(), max_length=100)
    glossary_terms: tuple[str, ...] = Field(default=(), max_length=100)
    signals: SearchSignalBreakdown

    @field_validator("binding_id")
    @classmethod
    def binding_id_must_be_inert(cls, value: str) -> str:
        if _BINDING_ID.fullmatch(value) is None:
            raise ValueError("governed binding id must be inert")
        return value

    @field_validator(
        "binding_fingerprint",
        "catalog_generation_fingerprint",
        "asset_metadata_fingerprint",
        "field_metadata_fingerprint",
        "field_definition_fingerprint",
        "field_terms_fingerprint",
    )
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "governed field binding fingerprint")

    @field_validator("mapping_approval_decision_id", "asset_qualified_name")
    @classmethod
    def required_text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("governed binding text must not be blank")
        return value

    @field_validator("native_type", "definition")
    @classmethod
    def optional_text_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("governed binding metadata must not be blank")
        return value

    @field_validator("tags", "glossary_terms")
    @classmethod
    def public_terms_must_be_unique_and_nonblank(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(not value.strip() for value in values):
            raise ValueError("governed binding terms must be unique and nonblank")
        return values

    @model_validator(mode="after")
    def locator_and_physical_identity_must_match(self) -> GovernedFieldBinding:
        if self.locator.asset.workspace_id != self.locator.asset.workspace_id.strip():
            raise ValueError("governed binding workspace is not canonical")
        expected_suffix = ".".join(self.locator.field_path)
        if not self.physical_field.root.endswith(f".{expected_suffix}"):
            raise ValueError("governed binding physical field does not match its locator path")
        return self

    def search_key(
        self,
        scope_fingerprint: str,
        request_fingerprint: str,
        binding_facts_fingerprint: str,
    ) -> GovernedSearchKey:
        return GovernedSearchKey(
            score=self.signals.total,
            logical_field=self.logical_field,
            binding_id=self.binding_id,
            scope_fingerprint=scope_fingerprint,
            request_fingerprint=request_fingerprint,
            binding_facts_fingerprint=binding_facts_fingerprint,
        )


class GovernedFieldSearchPage(FrozenDomainModel):
    scope: QueryStudioScopeSnapshot
    request_fingerprint: str
    binding_facts_fingerprint: str
    items: tuple[GovernedFieldBinding, ...] = Field(
        default=(),
        max_length=MAX_GOVERNED_SEARCH_PAGE_SIZE,
    )
    page_size: int = Field(ge=1, le=MAX_GOVERNED_SEARCH_PAGE_SIZE)
    rows_read: int = Field(ge=0, le=MAX_GOVERNED_SEARCH_ROWS_READ)
    next_key: GovernedSearchKey | None = None

    @field_validator("request_fingerprint", "binding_facts_fingerprint")
    @classmethod
    def request_fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "governed field page request fingerprint")

    @model_validator(mode="after")
    def page_must_be_bounded_sorted_and_scoped(self) -> GovernedFieldSearchPage:
        if len(self.items) > self.page_size:
            raise ValueError("governed field page exceeds the requested page size")
        if self.rows_read < len(self.items) or self.rows_read > self.page_size + 1:
            raise ValueError("governed field page rows-read contract is invalid")
        keys = tuple(
            item.search_key(
                self.scope.fingerprint,
                self.request_fingerprint,
                self.binding_facts_fingerprint,
            ).sort_tuple
            for item in self.items
        )
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("governed field page must be stably sorted and unique")
        if self.next_key is not None and (
            not self.items
            or self.rows_read != self.page_size + 1
            or self.next_key
            != self.items[-1].search_key(
                self.scope.fingerprint,
                self.request_fingerprint,
                self.binding_facts_fingerprint,
            )
            or self.next_key.scope_fingerprint != self.scope.fingerprint
        ):
            raise ValueError("governed field continuation must equal the final returned key")
        workspace = self.scope.scope.workspace_id
        if any(item.locator.asset.workspace_id != workspace for item in self.items):
            raise ValueError("governed field page crosses workspaces")
        return self


class PhysicalFieldDiscoveryRequest(FrozenDomainModel):
    scope: SemanticRegistryScope
    query: DescriptionQuery | None = None
    page_size: int = Field(default=20, ge=1, le=MAX_GOVERNED_SEARCH_PAGE_SIZE)
    cursor: PhysicalDiscoveryCursor | None = None


class PhysicalDiscoveryCandidate(FrozenDomainModel):
    """Ungoverned physical field; it has no logical field or executable candidate id."""

    locator: CatalogFieldLocator
    generation: int = Field(ge=1)
    asset_qualified_name: str = Field(min_length=1, max_length=500)
    native_type: str | None = Field(default=None, max_length=200)
    definition: str | None = Field(default=None, max_length=4_000)
    metadata_fingerprint: str
    status: PhysicalDiscoveryStatus = PhysicalDiscoveryStatus.NEEDS_MAPPING_REVIEW

    @field_validator("metadata_fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "physical discovery metadata fingerprint")

    @model_validator(mode="after")
    def result_must_remain_non_executable(self) -> PhysicalDiscoveryCandidate:
        if self.status is not PhysicalDiscoveryStatus.NEEDS_MAPPING_REVIEW:
            raise ValueError("physical discovery cannot become executable")
        return self


class PhysicalFieldDiscoveryPage(FrozenDomainModel):
    """Discovery page intentionally lacks governed scope state and candidate IDs."""

    items: tuple[PhysicalDiscoveryCandidate, ...] = Field(
        default=(),
        max_length=MAX_GOVERNED_SEARCH_PAGE_SIZE,
    )
    page_size: int = Field(ge=1, le=MAX_GOVERNED_SEARCH_PAGE_SIZE)
    next_cursor: PhysicalDiscoveryCursor | None = None

    @model_validator(mode="after")
    def page_must_be_bounded(self) -> PhysicalFieldDiscoveryPage:
        if len(self.items) > self.page_size:
            raise ValueError("physical discovery page exceeds the requested page size")
        return self


class PhysicalDiscoveryCardinality(FrozenDomainModel):
    """Current aggregate inventory facts without loading any physical row."""

    scope: SemanticRegistryScope
    catalog_generation_vector_fingerprint: str
    connection_count: int = Field(ge=0)
    asset_count: int = Field(ge=0)
    field_count: int = Field(ge=0)

    @field_validator("catalog_generation_vector_fingerprint")
    @classmethod
    def vector_fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "physical discovery catalog vector fingerprint")


class RankedGovernedCandidate(FrozenDomainModel):
    candidate_id: OpaqueCandidateId
    binding: GovernedFieldBinding
    logical_model: LogicalModelRef
    model_definition: str = Field(min_length=1, max_length=300)
    field_definition: str = Field(min_length=1, max_length=300)
    canonical_type: CanonicalType
    role: LogicalFieldRole
    allowed_values: tuple[str, ...] = Field(default=(), max_length=12)
    model_version: int = Field(ge=1)
    field_version: int = Field(ge=1)
    purposes: tuple[str, ...] = Field(min_length=1, max_length=MAX_DESCRIPTION_PROBES)
    rank: int = Field(ge=1, le=MAX_EXECUTABLE_SHORTLIST)
    risks: tuple[CandidateRiskCode, ...] = Field(default=(), max_length=12)

    @field_validator("model_definition", "field_definition")
    @classmethod
    def definitions_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("candidate definitions must not be blank")
        return value

    @field_validator("allowed_values")
    @classmethod
    def allowed_values_must_be_unique(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(not value.strip() for value in values):
            raise ValueError("candidate allowed values must be unique and nonblank")
        return values

    @field_validator("purposes")
    @classmethod
    def purposes_must_be_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))):
            raise ValueError("candidate purposes must be sorted and unique")
        if any(_INERT_ID.fullmatch(value) is None for value in values):
            raise ValueError("candidate purpose is invalid")
        return values

    @field_validator("risks")
    @classmethod
    def risks_must_be_canonical(
        cls,
        values: tuple[CandidateRiskCode, ...],
    ) -> tuple[CandidateRiskCode, ...]:
        if values != tuple(sorted(set(values), key=lambda item: item.value)):
            raise ValueError("candidate risks must be sorted and unique")
        return values

    @model_validator(mode="after")
    def logical_identity_must_match(self) -> RankedGovernedCandidate:
        if self.binding.logical_field.root.split(".", 1)[0] != self.logical_model.root:
            raise ValueError("candidate logical model does not own its field")
        return self


class GuidedGovernedCandidate(FrozenDomainModel):
    """One executable guided option derived from current registry and catalog evidence."""

    candidate_id: OpaqueCandidateId
    binding: GovernedFieldBinding
    logical_model: LogicalModelRef
    model_definition: str = Field(min_length=1, max_length=300)
    field_definition: str = Field(min_length=1, max_length=300)
    canonical_type: CanonicalType
    role: LogicalFieldRole
    allowed_values: tuple[str, ...] = Field(default=(), max_length=12)
    metric_operations: tuple[MetricOperation, ...] = Field(default=(), max_length=6)
    filter_operators: tuple[FilterOperator, ...] = Field(default=(), max_length=9)
    date_grains: tuple[DateGrain, ...] = Field(default=(), max_length=4)
    sort_directions: tuple[SortDirection, ...] = Field(default=(), max_length=2)
    risks: tuple[CandidateRiskCode, ...] = Field(default=(), max_length=12)

    @field_validator("model_definition", "field_definition")
    @classmethod
    def definitions_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("guided candidate definitions must not be blank")
        return value

    @field_validator("allowed_values")
    @classmethod
    def allowed_values_must_be_unique(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(not value.strip() for value in values):
            raise ValueError("guided candidate allowed values must be unique and nonblank")
        return values

    @field_validator(
        "metric_operations",
        "filter_operators",
        "date_grains",
        "sort_directions",
        "risks",
    )
    @classmethod
    def closed_values_must_be_unique(cls, values: tuple[Any, ...]) -> tuple[Any, ...]:
        if len(values) != len(set(values)):
            raise ValueError("guided candidate controls must be unique")
        return values

    @model_validator(mode="after")
    def logical_identity_and_temporal_controls_must_match(
        self,
    ) -> GuidedGovernedCandidate:
        if self.binding.logical_field.root.split(".", 1)[0] != self.logical_model.root:
            raise ValueError("guided candidate logical model does not own its field")
        temporal = self.role is LogicalFieldRole.TEMPORAL and self.canonical_type in {
            CanonicalType.DATE,
            CanonicalType.TIMESTAMP,
        }
        if bool(self.date_grains) != temporal:
            raise ValueError("guided date grains must match a temporal field")
        return self


class GuidedGovernedFieldPage(FrozenDomainModel):
    """Opaque guided options over one bounded governed keyset page."""

    context: GuidedBrowseContext
    request_fingerprint: str
    binding_facts_fingerprint: str
    items: tuple[GuidedGovernedCandidate, ...] = Field(
        default=(),
        max_length=MAX_GOVERNED_SEARCH_PAGE_SIZE,
    )
    page_size: int = Field(ge=1, le=MAX_GOVERNED_SEARCH_PAGE_SIZE)
    rows_read: int = Field(ge=0, le=MAX_GOVERNED_SEARCH_ROWS_READ)
    next_key: GovernedSearchKey | None = None

    @field_validator("request_fingerprint", "binding_facts_fingerprint")
    @classmethod
    def request_fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "guided field page request fingerprint")

    @model_validator(mode="after")
    def page_must_preserve_underlying_keyset_contract(self) -> GuidedGovernedFieldPage:
        if len(self.items) > self.page_size:
            raise ValueError("guided field page exceeds the requested page size")
        if self.rows_read < len(self.items) or self.rows_read > self.page_size + 1:
            raise ValueError("guided field page rows-read contract is invalid")
        candidate_ids = tuple(item.candidate_id.root for item in self.items)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("guided field page candidate ids must be unique")
        keys = tuple(
            item.binding.search_key(
                self.context.scope.fingerprint,
                self.request_fingerprint,
                self.binding_facts_fingerprint,
            ).sort_tuple
            for item in self.items
        )
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("guided field page must preserve stable governed ordering")
        if self.next_key is not None and (
            not self.items
            or self.rows_read != self.page_size + 1
            or self.next_key.scope_fingerprint != self.context.scope.fingerprint
            or self.next_key.request_fingerprint != self.request_fingerprint
            or self.next_key
            != self.items[-1].binding.search_key(
                self.context.scope.fingerprint,
                self.request_fingerprint,
                self.binding_facts_fingerprint,
            )
        ):
            raise ValueError("guided field continuation must match the final returned option")
        workspace = self.context.scope.scope.workspace_id
        if any(item.binding.locator.asset.workspace_id != workspace for item in self.items):
            raise ValueError("guided field page crosses workspaces")
        return self


class GovernedShortlist(FrozenDomainModel):
    scope: QueryStudioScopeSnapshot
    query_fingerprint: str
    expansion_fingerprint: str
    candidates: tuple[RankedGovernedCandidate, ...] = Field(
        default=(),
        max_length=MAX_EXECUTABLE_SHORTLIST,
    )

    @field_validator("query_fingerprint", "expansion_fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "governed shortlist fingerprint")

    @model_validator(mode="after")
    def shortlist_must_be_canonical_and_scoped(self) -> GovernedShortlist:
        ranks = tuple(item.rank for item in self.candidates)
        if ranks != tuple(range(1, len(self.candidates) + 1)):
            raise ValueError("governed shortlist ranks must be contiguous")
        candidate_ids = tuple(item.candidate_id.root for item in self.candidates)
        binding_ids = tuple(item.binding.binding_id for item in self.candidates)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("governed shortlist candidate ids must be unique")
        if len(binding_ids) != len(set(binding_ids)):
            raise ValueError("governed shortlist bindings must be unique")
        workspace = self.scope.scope.workspace_id
        if any(item.binding.locator.asset.workspace_id != workspace for item in self.candidates):
            raise ValueError("governed shortlist crosses workspaces")
        return self

    @property
    def fingerprint(self) -> str:
        return query_studio_fingerprint(self.model_dump(mode="json"))

    def candidate_index(self) -> dict[str, RankedGovernedCandidate]:
        return {item.candidate_id.root: item for item in self.candidates}


class QueryStudioPromptModel(FrozenDomainModel):
    id: LogicalModelRef
    definition: str = Field(min_length=1, max_length=300)


class QueryStudioPromptPurposeScore(FrozenDomainModel):
    """One deterministic retrieval score scoped to exactly one server-owned slot."""

    purpose_id: str = Field(min_length=2, max_length=80)
    score: int = Field(ge=0, le=80_000)

    @field_validator("purpose_id")
    @classmethod
    def purpose_id_must_be_inert(cls, value: str) -> str:
        if _INERT_ID.fullmatch(value) is None:
            raise ValueError("prompt purpose-score id is invalid")
        return value


class QueryStudioPromptCandidate(FrozenDomainModel):
    candidate_id: OpaqueCandidateId
    logical_field: LogicalFieldRef
    definition: str = Field(min_length=1, max_length=300)
    canonical_type: CanonicalType
    role: LogicalFieldRole
    intended_uses: tuple[QueryFieldPurpose, ...] = Field(
        min_length=1,
        max_length=MAX_DESCRIPTION_PROBES,
    )
    purpose_ids: tuple[str, ...] = Field(default=(), max_length=MAX_DESCRIPTION_PROBES)
    purpose_scores: tuple[QueryStudioPromptPurposeScore, ...] = Field(
        default=(),
        max_length=MAX_DESCRIPTION_PROBES,
    )
    allowed_values: tuple[str, ...] = Field(default=(), max_length=12)
    score: int = Field(ge=0, le=80_000)
    risks: tuple[CandidateRiskCode, ...] = Field(default=(), max_length=12)

    @model_validator(mode="before")
    @classmethod
    def singleton_purpose_score_is_explicitly_materialized(cls, value: Any) -> Any:
        if not isinstance(value, dict) or value.get("purpose_scores") is not None:
            return value
        purpose_ids = value.get("purpose_ids", ())
        if (
            isinstance(purpose_ids, (tuple, list))
            and len(purpose_ids) == 1
            and isinstance(value.get("score"), int)
        ):
            return {
                **value,
                "purpose_scores": (
                    {
                        "purpose_id": purpose_ids[0],
                        "score": value["score"],
                    },
                ),
            }
        return value

    @field_validator("intended_uses")
    @classmethod
    def intended_uses_must_be_canonical(
        cls,
        values: tuple[QueryFieldPurpose, ...],
    ) -> tuple[QueryFieldPurpose, ...]:
        if values != tuple(sorted(set(values), key=lambda item: item.value)):
            raise ValueError("prompt candidate intended uses must be sorted and unique")
        return values

    @field_validator("purpose_ids")
    @classmethod
    def purpose_ids_must_be_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))):
            raise ValueError("prompt candidate purpose ids must be sorted and unique")
        if any(_INERT_ID.fullmatch(value) is None for value in values):
            raise ValueError("prompt candidate purpose id is invalid")
        return values

    @model_validator(mode="after")
    def numbered_purpose_ids_must_match_declared_uses(self) -> QueryStudioPromptCandidate:
        scored_purposes = tuple(item.purpose_id for item in self.purpose_scores)
        if scored_purposes != self.purpose_ids:
            raise ValueError("prompt candidate scores must cover its exact purposes")
        if any(item.score > self.score for item in self.purpose_scores):
            raise ValueError("prompt purpose score cannot exceed its shortlist score")
        for purpose_id in self.purpose_ids:
            match = re.fullmatch(r"(dimension|metric|filter)_[1-6]", purpose_id)
            if match is None:
                continue
            if QueryFieldPurpose(match.group(1)) not in self.intended_uses:
                raise ValueError("prompt candidate purpose id does not match its declared use")
        return self

    def score_for_purpose(self, purpose_id: str) -> int:
        matches = tuple(item.score for item in self.purpose_scores if item.purpose_id == purpose_id)
        if len(matches) != 1:
            raise ValueError("prompt candidate has no exact score for the requested purpose")
        return matches[0]


class QueryStudioEditableCandidate(FrozenDomainModel):
    """Typed UI controls derived exclusively from one signed prompt vocabulary."""

    candidate_id: OpaqueCandidateId
    logical_field: LogicalFieldRef
    canonical_type: CanonicalType
    role: LogicalFieldRole
    allowed_values: tuple[str, ...] = Field(default=(), max_length=12)
    metric_operations: tuple[MetricOperation, ...] = Field(default=(), max_length=6)
    filter_operators: tuple[FilterOperator, ...] = Field(default=(), max_length=9)
    date_grains: tuple[DateGrain, ...] = Field(default=(), max_length=4)
    sort_directions: tuple[SortDirection, ...] = Field(default=(), max_length=2)

    @field_validator(
        "allowed_values",
        "metric_operations",
        "filter_operators",
        "date_grains",
        "sort_directions",
    )
    @classmethod
    def edit_controls_must_be_unique(cls, values: tuple[Any, ...]) -> tuple[Any, ...]:
        if len(values) != len(set(values)):
            raise ValueError("editable candidate controls must be unique")
        return values


class QueryStudioPromptJoin(FrozenDomainModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    left_model: LogicalModelRef
    right_model: LogicalModelRef
    left_field: LogicalFieldRef
    right_field: LogicalFieldRef
    cardinality: Cardinality
    fanout_policy: FanoutPolicy

    @model_validator(mode="after")
    def endpoints_must_belong_to_declared_models(self) -> QueryStudioPromptJoin:
        if self.left_model == self.right_model:
            raise ValueError("prompt join models must be different")
        if self.left_field.root.split(".", 1)[0] != self.left_model.root:
            raise ValueError("prompt join left endpoint does not belong to its model")
        if self.right_field.root.split(".", 1)[0] != self.right_model.root:
            raise ValueError("prompt join right endpoint does not belong to its model")
        return self


class QueryStudioRequiredSelectionCounts(FrozenDomainModel):
    dimensions: int = Field(ge=0, le=MAX_DESCRIPTION_PROBES)
    metrics: int = Field(ge=0, le=MAX_DESCRIPTION_PROBES)
    filters: int = Field(ge=0, le=MAX_DESCRIPTION_PROBES)

    @model_validator(mode="after")
    def total_must_fit_one_expansion(self) -> QueryStudioRequiredSelectionCounts:
        if self.dimensions + self.metrics + self.filters > MAX_DESCRIPTION_PROBES:
            raise ValueError("required selections exceed the bounded expansion")
        return self


class QueryStudioPromptVocabulary(FrozenDomainModel):
    context_source: str = Field(min_length=1, max_length=240)
    context_version: int = Field(ge=1)
    models: tuple[QueryStudioPromptModel, ...] = Field(
        min_length=1,
        max_length=MAX_PROMPT_MODELS,
    )
    candidates: tuple[QueryStudioPromptCandidate, ...] = Field(
        min_length=1,
        max_length=MAX_PROMPT_FIELDS,
    )
    joins: tuple[QueryStudioPromptJoin, ...] = Field(default=(), max_length=MAX_PROMPT_JOINS)
    required_selection_counts: QueryStudioRequiredSelectionCounts
    metric_operations: tuple[MetricOperation, ...] = Field(min_length=1)
    filter_operators: tuple[FilterOperator, ...] = Field(min_length=1)
    date_grains: tuple[DateGrain, ...] = Field(min_length=1)
    sort_directions: tuple[SortDirection, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def vocabulary_must_be_closed_connected_and_bounded(
        self,
    ) -> QueryStudioPromptVocabulary:
        model_ids = tuple(item.id.root for item in self.models)
        candidate_ids = tuple(item.candidate_id.root for item in self.candidates)
        logical_fields = tuple(item.logical_field.root for item in self.candidates)
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("prompt models must be unique")
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("prompt candidate ids must be unique")
        if len(logical_fields) != len(set(logical_fields)):
            raise ValueError("prompt logical fields must be unique")
        if any(field.split(".", 1)[0] not in model_ids for field in logical_fields):
            raise ValueError("prompt candidate references a missing model")
        available_by_purpose = {
            purpose: sum(purpose in candidate.intended_uses for candidate in self.candidates)
            for purpose in (
                QueryFieldPurpose.DIMENSION,
                QueryFieldPurpose.METRIC,
                QueryFieldPurpose.FILTER,
            )
        }
        if (
            self.required_selection_counts.dimensions
            > available_by_purpose[QueryFieldPurpose.DIMENSION]
            or self.required_selection_counts.metrics
            > available_by_purpose[QueryFieldPurpose.METRIC]
            or self.required_selection_counts.filters
            > available_by_purpose[QueryFieldPurpose.FILTER]
        ):
            raise ValueError("required selections exceed the supplied candidate purposes")
        numbered_purpose_ids = {
            purpose_id
            for candidate in self.candidates
            for purpose_id in candidate.purpose_ids
            if re.fullmatch(r"(?:dimension|metric|filter)_[1-6]", purpose_id)
        }
        if numbered_purpose_ids:
            required_purpose_ids = {
                *(
                    f"dimension_{index}"
                    for index in range(1, self.required_selection_counts.dimensions + 1)
                ),
                *(
                    f"metric_{index}"
                    for index in range(1, self.required_selection_counts.metrics + 1)
                ),
                *(
                    f"filter_{index}"
                    for index in range(1, self.required_selection_counts.filters + 1)
                ),
            }
            if numbered_purpose_ids != required_purpose_ids:
                raise ValueError("prompt vocabulary does not cover every exact selection slot")
            if any(
                not 1
                <= sum(purpose_id in candidate.purpose_ids for candidate in self.candidates)
                <= MAX_CANDIDATES_PER_PURPOSE
                for purpose_id in required_purpose_ids
            ):
                raise ValueError("prompt selection slot exceeds its exact candidate bound")
        if any(
            join.left_model.root not in model_ids or join.right_model.root not in model_ids
            for join in self.joins
        ):
            raise ValueError("prompt join references a missing model")
        if len(model_ids) > 1:
            pending = {model_ids[0]}
            visited: set[str] = set()
            while pending:
                current = pending.pop()
                if current in visited:
                    continue
                visited.add(current)
                for join in self.joins:
                    if join.left_model.root == current:
                        pending.add(join.right_model.root)
                    elif join.right_model.root == current:
                        pending.add(join.left_model.root)
            if visited != set(model_ids):
                raise ValueError("prompt model graph must be connected by supplied joins")
        encoded = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if len(encoded) > MAX_PROMPT_BYTES:
            raise ValueError("Query Studio prompt vocabulary exceeds 32 KiB")
        return self

    @property
    def fingerprint(self) -> str:
        return query_studio_fingerprint(self.model_dump(mode="json"))


class ApprovedPublicMetadataSurface(FrozenDomainModel):
    """Exact tenant/registry/vocabulary surface approved for one provider contract."""

    policy_version: str = Field(
        default=QUERY_STUDIO_PUBLIC_METADATA_POLICY_VERSION,
        pattern=r"^m27-approved-public-definition-surface-v1$",
    )
    policy_fingerprint: str
    semantic_scope_fingerprint: str
    registry_fingerprint: str
    vocabulary_fingerprint: str
    fingerprint: str

    @field_validator(
        "policy_fingerprint",
        "semantic_scope_fingerprint",
        "registry_fingerprint",
        "vocabulary_fingerprint",
        "fingerprint",
    )
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "approved public metadata surface fingerprint")

    @model_validator(mode="after")
    def fingerprint_must_match(self) -> ApprovedPublicMetadataSurface:
        expected = query_studio_fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))
        if self.fingerprint != expected:
            raise ValueError("approved public metadata surface fingerprint does not match")
        return self

    @classmethod
    def create(
        cls,
        *,
        policy_fingerprint: str,
        semantic_scope_fingerprint: str,
        registry_fingerprint: str,
        vocabulary_fingerprint: str,
    ) -> ApprovedPublicMetadataSurface:
        payload = {
            "policy_version": QUERY_STUDIO_PUBLIC_METADATA_POLICY_VERSION,
            "policy_fingerprint": policy_fingerprint,
            "semantic_scope_fingerprint": semantic_scope_fingerprint,
            "registry_fingerprint": registry_fingerprint,
            "vocabulary_fingerprint": vocabulary_fingerprint,
        }
        return cls(
            **payload,
            fingerprint=query_studio_fingerprint(payload),
        )


class GuidedQueryStudioEvidence(FrozenDomainModel):
    """Current server-recomputed governed evidence for one guided selection."""

    shortlist: GovernedShortlist
    vocabulary: QueryStudioPromptVocabulary

    @model_validator(mode="after")
    def vocabulary_must_be_an_exact_shortlist_subset(self) -> GuidedQueryStudioEvidence:
        shortlist_index = self.shortlist.candidate_index()
        for candidate in self.vocabulary.candidates:
            ranked = shortlist_index.get(candidate.candidate_id.root)
            if (
                ranked is None
                or ranked.binding.logical_field != candidate.logical_field
                or ranked.field_definition != candidate.definition
                or ranked.canonical_type is not candidate.canonical_type
                or ranked.role is not candidate.role
                or ranked.allowed_values != candidate.allowed_values
                or ranked.binding.signals.total != candidate.score
                or ranked.risks != candidate.risks
            ):
                raise ValueError("guided vocabulary does not match its exact governed shortlist")
        return self

    @property
    def fingerprint(self) -> str:
        return query_studio_fingerprint(
            {
                "shortlist_fingerprint": self.shortlist.fingerprint,
                "vocabulary_fingerprint": self.vocabulary.fingerprint,
            }
        )


class QueryStudioInterpretationInput(FrozenDomainModel):
    text: DescriptionQuery
    language: UserLanguage
    vocabulary: QueryStudioPromptVocabulary
    expansion: DescriptionExpansion
    public_metadata_surface: ApprovedPublicMetadataSurface | None = None

    @model_validator(mode="after")
    def public_surface_must_bind_exact_vocabulary(self) -> QueryStudioInterpretationInput:
        if (
            self.public_metadata_surface is not None
            and self.public_metadata_surface.vocabulary_fingerprint != self.vocabulary.fingerprint
        ):
            raise ValueError("public metadata surface does not bind the exact vocabulary")
        return self


class ProposedDimension(FrozenDomainModel):
    candidate_id: OpaqueCandidateId
    grain: DateGrain | None = None


class ProposedMetric(FrozenDomainModel):
    candidate_id: OpaqueCandidateId
    operation: MetricOperation
    alias: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")


class ProposedFilter(FrozenDomainModel):
    candidate_id: OpaqueCandidateId
    operator: FilterOperator
    value: FilterScalar | tuple[FilterScalar, ...] = None


class ProposedOrder(FrozenDomainModel):
    candidate_id: OpaqueCandidateId
    direction: SortDirection = SortDirection.ASC


class QueryStudioModelProposal(FrozenDomainModel):
    """Strict model output: opaque candidate references and closed operations only."""

    semantic_state: SemanticMatchState
    primary_candidate_id: OpaqueCandidateId | None = None
    dimensions: tuple[ProposedDimension, ...] = Field(default=(), max_length=MAX_PROMPT_FIELDS)
    metrics: tuple[ProposedMetric, ...] = Field(default=(), max_length=MAX_PROMPT_FIELDS)
    filters: tuple[ProposedFilter, ...] = Field(default=(), max_length=MAX_PROMPT_FIELDS)
    order_by: tuple[ProposedOrder, ...] = Field(default=(), max_length=MAX_PROMPT_FIELDS)
    limit: int = Field(default=100, ge=1, le=1_000)
    ambiguities: tuple[ProposalAmbiguityKind, ...] = Field(default=(), max_length=12)

    @model_validator(mode="after")
    def proposal_shape_must_match_semantic_state(self) -> QueryStudioModelProposal:
        references = (
            *(item.candidate_id.root for item in self.dimensions),
            *(item.candidate_id.root for item in self.metrics),
            *(item.candidate_id.root for item in self.filters),
            *(item.candidate_id.root for item in self.order_by),
        )
        for collection in (self.dimensions, self.metrics, self.filters, self.order_by):
            ids = tuple(item.candidate_id.root for item in collection)
            if len(ids) != len(set(ids)):
                raise ValueError("proposal candidate references must be unique per selection kind")
        if len(self.ambiguities) != len(set(self.ambiguities)):
            raise ValueError("proposal ambiguities must be unique")
        if self.semantic_state is SemanticMatchState.ALIGNED:
            if (
                self.primary_candidate_id is None
                or not self.metrics
                or self.ambiguities
                or not references
            ):
                raise ValueError("aligned proposal requires one complete unambiguous request")
        elif self.semantic_state is SemanticMatchState.AMBIGUOUS:
            if (
                not self.ambiguities
                or self.primary_candidate_id is not None
                or references
                or self.metrics
            ):
                raise ValueError("ambiguous proposal requires only explicit ambiguity kinds")
        elif (
            self.primary_candidate_id is not None or references or self.ambiguities or self.metrics
        ):
            raise ValueError("no-match/conflicting proposal cannot carry a request")
        return self

    @property
    def fingerprint(self) -> str:
        return query_studio_fingerprint(self.model_dump(mode="json"))

    @property
    def referenced_candidate_ids(self) -> tuple[OpaqueCandidateId, ...]:
        values = (
            *((self.primary_candidate_id,) if self.primary_candidate_id is not None else ()),
            *(item.candidate_id for item in self.dimensions),
            *(item.candidate_id for item in self.metrics),
            *(item.candidate_id for item in self.filters),
            *(item.candidate_id for item in self.order_by),
        )
        return tuple(dict.fromkeys(values))


class ProviderConfigurationFacts(FrozenDomainModel):
    adapter: str = Field(min_length=1, max_length=80)
    model_snapshot: str = Field(min_length=2, max_length=120)
    reasoning_effort: str = Field(min_length=1, max_length=40)
    endpoint_region: str = Field(min_length=1, max_length=80)
    prompt_version: str = Field(min_length=1, max_length=80)
    schema_version: str = Field(min_length=1, max_length=80)
    matcher_version: str = Field(min_length=1, max_length=80)
    orchestration_policy_version: str = Field(
        default=QUERY_STUDIO_ORCHESTRATION_POLICY_VERSION,
        min_length=1,
        max_length=80,
    )
    attempt_policy_version: str = Field(min_length=1, max_length=80)
    managed_config_fingerprint: str
    provider_contract_fingerprint: str
    public_metadata_policy_fingerprint: str
    public_metadata_semantic_scope_fingerprint: str
    public_metadata_registry_fingerprint: str
    external_ai: bool
    fingerprint: str

    @field_validator("adapter", "reasoning_effort")
    @classmethod
    def labels_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("provider configuration labels must not be blank")
        return value

    @field_validator("model_snapshot")
    @classmethod
    def model_snapshot_must_be_inert(cls, value: str) -> str:
        if _MODEL_SNAPSHOT.fullmatch(value) is None:
            raise ValueError("provider model snapshot must be inert")
        return value

    @field_validator("endpoint_region")
    @classmethod
    def endpoint_region_must_be_inert(cls, value: str) -> str:
        if _REGION_LABEL.fullmatch(value) is None:
            raise ValueError("provider endpoint region must be inert")
        return value

    @field_validator(
        "prompt_version",
        "schema_version",
        "matcher_version",
        "orchestration_policy_version",
        "attempt_policy_version",
    )
    @classmethod
    def versions_must_be_inert(cls, value: str) -> str:
        if _VERSION_LABEL.fullmatch(value) is None:
            raise ValueError("provider configuration version must be inert")
        return value

    @field_validator(
        "managed_config_fingerprint",
        "provider_contract_fingerprint",
        "public_metadata_policy_fingerprint",
        "public_metadata_semantic_scope_fingerprint",
        "public_metadata_registry_fingerprint",
        "fingerprint",
    )
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "provider configuration fingerprint")

    @model_validator(mode="after")
    def fingerprint_must_match(self) -> ProviderConfigurationFacts:
        expected = query_studio_fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))
        if self.fingerprint != expected:
            raise ValueError("provider configuration fingerprint does not match")
        return self

    @classmethod
    def create(cls, **values: Any) -> ProviderConfigurationFacts:
        payload = {
            **values,
            "orchestration_policy_version": values.get(
                "orchestration_policy_version",
                QUERY_STUDIO_ORCHESTRATION_POLICY_VERSION,
            ),
        }
        payload.setdefault(
            "managed_config_fingerprint",
            query_studio_fingerprint(
                {
                    "kind": "managed-provider-config-v1",
                    "adapter": payload.get("adapter"),
                    "model_snapshot": payload.get("model_snapshot"),
                    "reasoning_effort": payload.get("reasoning_effort"),
                    "endpoint_region": payload.get("endpoint_region"),
                }
            ),
        )
        payload.setdefault(
            "provider_contract_fingerprint",
            query_studio_fingerprint(
                {
                    "kind": "provider-contract-v1",
                    "managed_config_fingerprint": payload["managed_config_fingerprint"],
                    "prompt_version": payload.get("prompt_version"),
                    "schema_version": payload.get("schema_version"),
                    "matcher_version": payload.get("matcher_version"),
                }
            ),
        )
        payload.setdefault(
            "public_metadata_policy_fingerprint",
            query_studio_fingerprint(
                {
                    "kind": QUERY_STUDIO_PUBLIC_METADATA_POLICY_VERSION,
                    "provider_contract_fingerprint": payload["provider_contract_fingerprint"],
                }
            ),
        )
        payload.setdefault(
            "public_metadata_semantic_scope_fingerprint",
            query_studio_fingerprint(
                {
                    "kind": "local-public-metadata-scope-v1",
                    "adapter": payload.get("adapter"),
                }
            ),
        )
        payload.setdefault(
            "public_metadata_registry_fingerprint",
            query_studio_fingerprint(
                {
                    "kind": "local-public-metadata-registry-v1",
                    "adapter": payload.get("adapter"),
                }
            ),
        )
        fingerprint = query_studio_fingerprint(payload)
        return cls(**payload, fingerprint=fingerprint)


class ProviderUsageFacts(FrozenDomainModel):
    stage: ProviderStage
    model_snapshot: str = Field(min_length=2, max_length=120)
    configuration_fingerprint: str
    input_tokens: int = Field(ge=0, le=1_000_000)
    output_tokens: int = Field(ge=0, le=1_000_000)
    duration_ms: int = Field(ge=0, le=3_600_000)
    outcome: ProviderOutcomeCode

    @field_validator("model_snapshot")
    @classmethod
    def model_snapshot_must_be_inert(cls, value: str) -> str:
        if _MODEL_SNAPSHOT.fullmatch(value) is None:
            raise ValueError("provider usage model snapshot must be inert")
        return value

    @field_validator("configuration_fingerprint")
    @classmethod
    def configuration_fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "provider usage configuration fingerprint")

    @model_validator(mode="after")
    def failed_call_cannot_claim_output_tokens(self) -> ProviderUsageFacts:
        if self.outcome is not ProviderOutcomeCode.SUCCEEDED and self.output_tokens != 0:
            raise ValueError("failed provider usage cannot claim parsed output tokens")
        return self


class DescriptionExpansionResult(FrozenDomainModel):
    expansion: DescriptionExpansion
    usage: ProviderUsageFacts | None = None

    @model_validator(mode="after")
    def usage_stage_must_match(self) -> DescriptionExpansionResult:
        if self.usage is not None and (
            self.usage.stage is not ProviderStage.EXPANSION
            or self.usage.outcome is not ProviderOutcomeCode.SUCCEEDED
        ):
            raise ValueError("description expansion requires successful expansion usage")
        return self


class QueryStudioInterpretationResult(FrozenDomainModel):
    proposal: QueryStudioModelProposal
    usage: ProviderUsageFacts

    @model_validator(mode="after")
    def usage_stage_must_match(self) -> QueryStudioInterpretationResult:
        if (
            self.usage.stage is not ProviderStage.INTERPRETATION
            or self.usage.outcome is not ProviderOutcomeCode.SUCCEEDED
        ):
            raise ValueError("Query Studio interpretation requires successful usage")
        return self


class GuidedDraftDimension(FrozenDomainModel):
    field: LogicalFieldRef
    grain: DateGrain | None = None


class GuidedDraftMetric(FrozenDomainModel):
    field: LogicalFieldRef
    operation: MetricOperation
    alias: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")


class GuidedDraftFilter(FrozenDomainModel):
    field: LogicalFieldRef
    operator: FilterOperator
    value: FilterScalar | tuple[FilterScalar, ...] = None


class GuidedDraftOrder(FrozenDomainModel):
    field: LogicalFieldRef
    direction: SortDirection = SortDirection.ASC


class GuidedRequestDraft(FrozenDomainModel):
    """Resolved primitive choices that are not yet validated or approved."""

    primary_entity: LogicalModelRef
    dimensions: tuple[GuidedDraftDimension, ...] = Field(default=(), max_length=12)
    metrics: tuple[GuidedDraftMetric, ...] = Field(min_length=1, max_length=12)
    filters: tuple[GuidedDraftFilter, ...] = Field(default=(), max_length=12)
    order_by: tuple[GuidedDraftOrder, ...] = Field(default=(), max_length=12)
    limit: int = Field(default=100, ge=1, le=1_000)
    selected_candidate_ids: tuple[OpaqueCandidateId, ...] = Field(
        min_length=1,
        max_length=MAX_PROMPT_FIELDS,
    )

    @field_validator("selected_candidate_ids")
    @classmethod
    def candidate_ids_must_be_unique(
        cls,
        values: tuple[OpaqueCandidateId, ...],
    ) -> tuple[OpaqueCandidateId, ...]:
        roots = tuple(item.root for item in values)
        if len(roots) != len(set(roots)):
            raise ValueError("guided draft candidate ids must be unique")
        return values

    @property
    def fingerprint(self) -> str:
        return query_studio_fingerprint(self.model_dump(mode="json"))


class GuidedRequestPreview(FrozenDomainModel):
    mode: QueryStudioMode
    scope: QueryStudioScopeSnapshot
    shortlist_fingerprint: str
    proposal_fingerprint: str
    configuration_fingerprint: str
    draft: GuidedRequestDraft

    @field_validator(
        "shortlist_fingerprint",
        "proposal_fingerprint",
        "configuration_fingerprint",
    )
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "guided request preview fingerprint")

    @property
    def fingerprint(self) -> str:
        return query_studio_fingerprint(self.model_dump(mode="json"))


class PreviewTokenPayload(FrozenDomainModel):
    version: int = Field(default=1, ge=1, le=1)
    request_digest: str
    shortlist_fingerprint: str
    proposal_fingerprint: str
    configuration_fingerprint: str
    scope_digest: str
    preview_fingerprint: str
    issued_at: datetime
    expires_at: datetime
    nonce: str = Field(min_length=16, max_length=120)

    @field_validator(
        "request_digest",
        "shortlist_fingerprint",
        "proposal_fingerprint",
        "configuration_fingerprint",
        "scope_digest",
        "preview_fingerprint",
    )
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "preview-token fingerprint")

    @field_validator("issued_at", "expires_at")
    @classmethod
    def times_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("preview-token times must include a timezone")
        return value

    @field_validator("nonce")
    @classmethod
    def nonce_must_be_opaque(cls, value: str) -> str:
        if not value.isascii() or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("preview-token nonce must be opaque")
        return value

    @model_validator(mode="after")
    def lifetime_must_be_positive_and_bounded(self) -> PreviewTokenPayload:
        seconds = (self.expires_at - self.issued_at).total_seconds()
        if seconds <= 0 or seconds > MAX_PREVIEW_TTL_SECONDS:
            raise ValueError("preview token lifetime must be at most ten minutes")
        return self


class QueryStudioPreview(FrozenDomainModel):
    mode: QueryStudioMode
    semantic_state: SemanticMatchState | None = None
    operational_state: QueryStudioOperationalState | None = None
    shortlist: GovernedShortlist | None = None
    vocabulary: QueryStudioPromptVocabulary | None = None
    expansion: DescriptionExpansion | None = None
    proposal: QueryStudioModelProposal | None = None
    guided_preview: GuidedRequestPreview | None = None
    token: SignedPreviewToken | None = None
    provider_usage: tuple[ProviderUsageFacts, ...] = Field(default=(), max_length=2)
    output_failure_category: ProviderOutputFailureCategory | None = None
    reason_code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{1,63}$",
    )

    @model_validator(mode="after")
    def outcome_shape_must_be_closed(self) -> QueryStudioPreview:
        if (self.semantic_state is None) == (self.operational_state is None):
            raise ValueError(
                "Query Studio preview requires exactly one semantic or operational outcome"
            )
        if self.operational_state is not None:
            if (
                self.proposal is not None
                or self.guided_preview is not None
                or self.token is not None
                or self.reason_code is None
                or (
                    self.output_failure_category is not None
                    and self.reason_code != "query_studio_provider_invalid_output"
                )
            ):
                raise ValueError("operational Query Studio preview cannot be confirmable")
            return self
        if (
            self.shortlist is None
            or self.reason_code is not None
            or self.output_failure_category is not None
        ):
            raise ValueError("semantic Query Studio preview requires one governed shortlist")
        if self.semantic_state is SemanticMatchState.ALIGNED:
            if (
                self.proposal is None
                or self.proposal.semantic_state is not SemanticMatchState.ALIGNED
                or self.guided_preview is None
                or self.token is None
            ):
                raise ValueError("aligned Query Studio preview requires a signed guided draft")
        elif self.guided_preview is not None or self.token is not None:
            raise ValueError("non-aligned Query Studio preview cannot be confirmed")
        return self


class QueryStudioConfirmation(FrozenDomainModel):
    """Browser-resubmitted typed proposal and original text; no candidate authority."""

    original_text: DescriptionQuery | None
    language: UserLanguage | None
    expansion: DescriptionExpansion | None
    proposal: QueryStudioModelProposal
    token: SignedPreviewToken
    action: QueryStudioConfirmationAction

    @model_validator(mode="after")
    def natural_language_parts_must_be_complete(self) -> QueryStudioConfirmation:
        supplied = (
            self.original_text is not None,
            self.language is not None,
            self.expansion is not None,
        )
        if any(supplied) and not all(supplied):
            raise ValueError("natural-language confirmation context must be complete")
        if self.proposal.semantic_state is not SemanticMatchState.ALIGNED:
            raise ValueError("only an aligned typed proposal can be confirmed")
        return self


class QueryStudioInterpretationRevision(FrozenDomainModel):
    """Exact signed natural preview plus one human-edited typed proposal."""

    original_text: DescriptionQuery
    language: UserLanguage
    expansion: DescriptionExpansion
    signed_proposal: QueryStudioModelProposal
    revised_proposal: QueryStudioModelProposal
    token: SignedPreviewToken
    action: QueryStudioRevisionAction

    @model_validator(mode="after")
    def revision_must_change_one_aligned_proposal(
        self,
    ) -> QueryStudioInterpretationRevision:
        if (
            self.signed_proposal.semantic_state is not SemanticMatchState.ALIGNED
            or self.revised_proposal.semantic_state is not SemanticMatchState.ALIGNED
        ):
            raise ValueError("only an aligned interpretation can be revised")
        if self.signed_proposal.fingerprint == self.revised_proposal.fingerprint:
            raise ValueError("interpretation revision must change the signed proposal")
        return self


class ConfirmedQueryStudioRequest(FrozenDomainModel):
    """Only confirmed value handed to the existing governed workflow."""

    original_text: DescriptionQuery | None
    language: UserLanguage | None
    validated_request: ValidatedAnalyticalRequest
    preview_fingerprint: str
    confirmation_fingerprint: str

    @field_validator("preview_fingerprint", "confirmation_fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "confirmed Query Studio fingerprint")


def normalize_description_text(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("business description must be text")
    normalized = unicodedata.normalize("NFKC", value)
    if _CONTROL_EXCEPT_WHITESPACE.search(normalized):
        raise ValueError("business description contains forbidden control characters")
    canonical = " ".join(normalized.split())
    if not canonical:
        raise ValueError("business description must not be blank")
    if len(canonical) > MAX_DESCRIPTION_CHARACTERS:
        raise ValueError("business description exceeds 2,000 characters")
    if len(canonical.encode("utf-8")) > MAX_DESCRIPTION_BYTES:
        raise ValueError("business description exceeds 8 KiB")
    return canonical


class ExplicitLogicalFieldReference(NamedTuple):
    """One source-ordered bounded logical-field reference and its exact span."""

    value: str
    owner: str
    field: str
    start: int
    end: int


class MalformedQualifiedPath(NamedTuple):
    """A dotted inert-looking path that is not one logical ``Model.field``."""

    value: str
    owner: str
    segments: tuple[str, ...]
    start: int
    end: int


def find_explicit_logical_field_references(
    value: str,
) -> tuple[ExplicitLogicalFieldReference, ...]:
    """Find every bounded ``LogicalModel.field_name`` reference in source order."""

    canonical = normalize_description_text(value)
    return tuple(
        ExplicitLogicalFieldReference(
            value=f"{match.group('owner').casefold()}.{match.group('field').casefold()}",
            owner=match.group("owner").casefold(),
            field=match.group("field").casefold(),
            start=match.start(),
            end=match.end(),
        )
        for match in _EXPLICIT_LOGICAL_FIELD_REFERENCE.finditer(canonical)
    )


def find_malformed_qualified_paths(value: str) -> tuple[MalformedQualifiedPath, ...]:
    """Find dotted paths that must never degrade into fuzzy logical evidence."""

    canonical = normalize_description_text(value)
    valid_spans = {
        (reference.start, reference.end)
        for reference in find_explicit_logical_field_references(canonical)
    }
    malformed: list[MalformedQualifiedPath] = []
    for match in _QUALIFIED_LOOKING_PATH.finditer(canonical):
        if (match.start(), match.end()) in valid_spans:
            continue
        segments = tuple(segment.casefold() for segment in match.group("path").split("."))
        malformed.append(
            MalformedQualifiedPath(
                value=".".join(segments),
                owner=segments[0],
                segments=segments,
                start=match.start(),
                end=match.end(),
            )
        )
    return tuple(malformed)


def explicit_logical_field_reference(value: str) -> str | None:
    """Return one bounded exact ``LogicalModel.field_name`` reference.

    The canonical case-folded value is suitable only for exact registry
    identity comparison. It grants no authority to a physical-looking or
    approximately similar field.
    """

    canonical = normalize_description_text(value)
    matches = find_explicit_logical_field_references(canonical)
    if len(matches) != 1 or matches[0].start != 0 or matches[0].end != len(canonical):
        return None
    return matches[0].value


def query_studio_fingerprint(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _governed_search_request_fingerprint(
    *,
    lane: str,
    scope: SemanticRegistryScope,
    query: DescriptionQuery | None,
    filters: GovernedFieldSearchFilters | GovernedBindingFactsFilters,
) -> str:
    """Bind a continuation to exact search semantics, independent of page size."""

    return query_studio_fingerprint(
        {
            "contract_version": _GOVERNED_SEARCH_CONTRACT_VERSION,
            "lane": lane,
            "scope": scope,
            "query": query,
            "filters": filters,
        }
    )


def query_studio_request_digest(
    text: DescriptionQuery | None,
    language: UserLanguage | None,
    expansion: DescriptionExpansion | None,
) -> str:
    return query_studio_fingerprint(
        {
            "text": text.root if text is not None else None,
            "language": language.value if language is not None else None,
            "expansion": expansion.model_dump(mode="json") if expansion is not None else None,
        }
    )


def confirmation_fingerprint(
    confirmation: QueryStudioConfirmation,
    payload: PreviewTokenPayload,
) -> str:
    return query_studio_fingerprint(
        {
            "confirmation": confirmation.model_dump(mode="json", exclude={"token"}),
            "token_payload": payload.model_dump(mode="json"),
        }
    )


def executable_evidence_status(value: SemanticChangeStatus) -> ExecutableEvidenceStatus:
    if value is SemanticChangeStatus.CURRENT:
        return ExecutableEvidenceStatus.CURRENT
    if value is SemanticChangeStatus.REVALIDATED:
        return ExecutableEvidenceStatus.REVALIDATED
    raise ValueError("semantic evidence status is not executable")


def catalog_asset_identity(asset_id: CatalogAssetId) -> str:
    """Expose an explicit helper without treating a catalog asset as a SQL identifier."""

    return asset_id.root


def _sha256(value: str, label: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


def _json_default(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"value of type {type(value).__name__} is not JSON serializable")
