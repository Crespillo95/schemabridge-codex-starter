"""Pure M32 contracts for governed natural-language analytical requests.

This module contains no I/O, provider SDK, physical locator, or SQL contract. A
provider may only return bounded mentions and one typed advanced request (or
closed ambiguity codes). The server remains responsible for context retrieval,
validation, routing, compilation, and any optional execution.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    field_validator,
    model_validator,
)

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.advanced_requests import (
    AdvancedAnalyticalRequest,
    AnalyticalRequestLike,
    logical_predicate_filters,
)
from schemabridge.domain.concepts import (
    CanonicalType,
    LogicalFieldRef,
    LogicalModelRef,
)
from schemabridge.domain.fields import PhysicalDatasetRef, PhysicalFieldRef
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.joins import Cardinality, FanoutPolicy
from schemabridge.domain.query_studio import (
    MAX_PREVIEW_TTL_SECONDS,
    ProviderOutcomeCode,
    ProviderStage,
    ProviderUsageFacts,
)
from schemabridge.domain.request_context import (
    LogicalFieldRole,
    ValidatedRequestLike,
)
from schemabridge.domain.requests import AnalyticalRequest, Filter, FilterOperator
from schemabridge.domain.resolution import FanoutMitigation, ResolutionAssumption

MAX_NATURAL_QUERY_CHARACTERS = 2_000
MAX_ADVANCED_MENTIONS = 12
MAX_ADVANCED_CONTEXT_MODELS = 3
MAX_ADVANCED_CONTEXT_FIELDS = 12
MAX_ADVANCED_CONTEXT_JOINS = 2

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTEXT_SOURCE = re.compile(r"^[a-z][a-z0-9_-]*:[A-Za-z0-9._/-]+$")
_JOIN_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_CONTROL_EXCEPT_WHITESPACE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ADVANCED_PREVIEW_TOKEN = re.compile(r"^qsp2\.[A-Za-z0-9_-]{16,3900}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{16,120}$")


class AdvancedMentionPurpose(StrEnum):
    """Closed reasons why one exact source span matters to interpretation."""

    PRIMARY_ENTITY = "primary_entity"
    DIMENSION = "dimension"
    METRIC = "metric"
    FILTER = "filter"
    GROUPING = "grouping"
    ORDERING = "ordering"
    WINDOW = "window"
    LIMIT = "limit"


class AdvancedInterpretationAmbiguity(StrEnum):
    """Closed ambiguity outcomes; free-form provider explanations are excluded."""

    PRIMARY_ENTITY = "primary_entity"
    FIELD_MEANING = "field_meaning"
    METRIC_MEANING = "metric_meaning"
    DATE_MEANING = "date_meaning"
    FILTER_VALUE = "filter_value"
    AGGREGATION_GRAIN = "aggregation_grain"
    JOIN_PATH = "join_path"
    RANK_TIE_POLICY = "rank_tie_policy"
    WINDOW_FRAME = "window_frame"
    UNSUPPORTED_REQUEST = "unsupported_request"


class AdvancedQueryRoute(StrEnum):
    """Deterministic compiler lane selected after structured interpretation."""

    V1 = "v1"
    V2 = "v2"


class AdvancedQueryConfirmationAction(StrEnum):
    CONFIRM = "CONFIRM ADVANCED QUERY INTERPRETATION"


class SignedAdvancedQueryPreviewToken(RootModel[str]):
    """Opaque authenticated qsp2 token; decoded claims never contain query text."""

    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def token_must_be_bounded(cls, value: str) -> str:
        if _ADVANCED_PREVIEW_TOKEN.fullmatch(value) is None:
            raise ValueError("advanced query preview token is invalid")
        return value


class AdvancedNaturalLanguageInput(FrozenDomainModel):
    """Exact user text accepted by the natural-language boundary."""

    text: str = Field(min_length=1, max_length=MAX_NATURAL_QUERY_CHARACTERS)
    language: UserLanguage

    @field_validator("text")
    @classmethod
    def text_must_be_nonblank_and_safe_to_index(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("natural-language query must not be blank")
        if _CONTROL_EXCEPT_WHITESPACE.search(value):
            raise ValueError("natural-language query contains forbidden control characters")
        return value

    @property
    def digest(self) -> str:
        return advanced_query_request_digest(self)


class AdvancedSourceSpan(FrozenDomainModel):
    """Half-open Unicode-codepoint offsets into the exact submitted text."""

    start: int = Field(ge=0, lt=MAX_NATURAL_QUERY_CHARACTERS)
    end: int = Field(gt=0, le=MAX_NATURAL_QUERY_CHARACTERS)

    @model_validator(mode="after")
    def span_must_be_nonempty(self) -> Self:
        if self.end <= self.start:
            raise ValueError("mention source span must be nonempty")
        return self


class AdvancedQueryMention(FrozenDomainModel):
    value: str = Field(min_length=1, max_length=300)
    source_span: AdvancedSourceSpan
    purpose: AdvancedMentionPurpose

    @field_validator("value")
    @classmethod
    def mention_value_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query mention value must not be blank")
        return value


class AdvancedMentionExtractionInput(FrozenDomainModel):
    query: AdvancedNaturalLanguageInput


class AdvancedMentionExtraction(FrozenDomainModel):
    """Provider-safe extraction output, bound to but not containing source text."""

    request_digest: str
    mentions: tuple[AdvancedQueryMention, ...] = Field(
        min_length=1,
        max_length=MAX_ADVANCED_MENTIONS,
    )

    @field_validator("request_digest")
    @classmethod
    def request_digest_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "mention-extraction request digest")

    @model_validator(mode="after")
    def mentions_must_be_unique_and_source_ordered(self) -> Self:
        signatures = tuple(
            (item.source_span.start, item.source_span.end, item.purpose) for item in self.mentions
        )
        if len(signatures) != len(set(signatures)):
            raise ValueError("query mentions must be structurally unique")
        if signatures != tuple(sorted(signatures, key=lambda item: (item[0], item[1], item[2]))):
            raise ValueError("query mentions must be ordered by their source spans")
        return self

    @property
    def fingerprint(self) -> str:
        return advanced_mention_extraction_fingerprint(self)


class AdvancedMentionExtractionResult(FrozenDomainModel):
    """One mention-extraction port result with locally verifiable grounding."""

    input: AdvancedMentionExtractionInput
    extraction: AdvancedMentionExtraction
    usage: ProviderUsageFacts | None = None

    @model_validator(mode="after")
    def extraction_must_be_grounded_and_usage_must_match(self) -> Self:
        _validate_extraction_grounding(self.input.query, self.extraction)
        if self.usage is not None and (
            self.usage.stage is not ProviderStage.EXPANSION
            or self.usage.outcome is not ProviderOutcomeCode.SUCCEEDED
        ):
            raise ValueError("mention extraction requires successful expansion-stage usage")
        return self


class AdvancedSemanticModel(FrozenDomainModel):
    id: LogicalModelRef
    definition: str = Field(min_length=1, max_length=300)

    @field_validator("definition")
    @classmethod
    def definition_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("semantic model definition must not be blank")
        return value


class AdvancedSemanticField(FrozenDomainModel):
    id: LogicalFieldRef
    canonical_type: CanonicalType
    role: LogicalFieldRole
    definition: str = Field(min_length=1, max_length=300)
    allowed_values: tuple[str, ...] = Field(default=(), max_length=12)

    @field_validator("definition")
    @classmethod
    def definition_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("semantic field definition must not be blank")
        return value

    @field_validator("allowed_values")
    @classmethod
    def allowed_values_must_be_bounded_unique_and_nonblank(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("semantic field allowed values must be unique")
        if any(not value.strip() or len(value) > 120 for value in values):
            raise ValueError("semantic field allowed values must be nonblank and bounded")
        return values

    @model_validator(mode="after")
    def role_and_allowed_values_must_match_type(self) -> Self:
        numeric_types = {CanonicalType.INTEGER, CanonicalType.DECIMAL}
        temporal_types = {CanonicalType.DATE, CanonicalType.TIMESTAMP}
        if self.role is LogicalFieldRole.MEASURE and self.canonical_type not in numeric_types:
            raise ValueError("measure semantic fields must be numeric")
        if self.role is LogicalFieldRole.TEMPORAL and self.canonical_type not in temporal_types:
            raise ValueError("temporal semantic fields must be date or timestamp")
        if self.allowed_values and self.canonical_type is not CanonicalType.STRING:
            raise ValueError("closed semantic values require a string field")
        return self


class AdvancedSemanticJoin(FrozenDomainModel):
    id: str = Field(min_length=1)
    left_model: LogicalModelRef
    right_model: LogicalModelRef
    cardinality: Cardinality
    fanout_policy: FanoutPolicy

    @field_validator("id")
    @classmethod
    def id_must_be_inert(cls, value: str) -> str:
        if _JOIN_ID.fullmatch(value) is None:
            raise ValueError("semantic join id must be a lowercase inert identifier")
        return value

    @model_validator(mode="after")
    def endpoints_must_be_distinct(self) -> Self:
        if self.left_model == self.right_model:
            raise ValueError("advanced semantic context does not support self joins")
        return self


class AdvancedApprovedSemanticContext(FrozenDomainModel):
    """Bounded connected slice of one exact approved logical context."""

    approval_status: Literal["approved"] = "approved"
    context_source: str = Field(min_length=1, max_length=200)
    context_version: int = Field(ge=1)
    approved_context_fingerprint: str
    governed_registry_fingerprint: str
    scope_fingerprint: str
    models: tuple[AdvancedSemanticModel, ...] = Field(
        min_length=1,
        max_length=MAX_ADVANCED_CONTEXT_MODELS,
    )
    fields: tuple[AdvancedSemanticField, ...] = Field(
        min_length=1,
        max_length=MAX_ADVANCED_CONTEXT_FIELDS,
    )
    joins: tuple[AdvancedSemanticJoin, ...] = Field(
        default=(),
        max_length=MAX_ADVANCED_CONTEXT_JOINS,
    )

    @field_validator("context_source")
    @classmethod
    def context_source_must_be_explicit(cls, value: str) -> str:
        if _CONTEXT_SOURCE.fullmatch(value) is None:
            raise ValueError("advanced semantic context source must be kind:location")
        return value

    @field_validator(
        "approved_context_fingerprint",
        "governed_registry_fingerprint",
        "scope_fingerprint",
    )
    @classmethod
    def context_fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "advanced semantic context fingerprint")

    @model_validator(mode="after")
    def members_must_be_unique_owned_and_connected(self) -> Self:
        model_ids = tuple(item.id.root for item in self.models)
        field_ids = tuple(item.id.root for item in self.fields)
        join_ids = tuple(item.id for item in self.joins)
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("advanced semantic context models must be unique")
        if len(field_ids) != len(set(field_ids)):
            raise ValueError("advanced semantic context fields must be unique")
        if len(join_ids) != len(set(join_ids)):
            raise ValueError("advanced semantic context joins must be unique")

        known_models = set(model_ids)
        field_owners = tuple(item.id.root.split(".", 1)[0] for item in self.fields)
        if any(owner not in known_models for owner in field_owners):
            raise ValueError("advanced semantic fields must belong to included models")
        if set(field_owners) != known_models:
            raise ValueError("every advanced semantic model requires at least one field")
        if any(
            join.left_model.root not in known_models or join.right_model.root not in known_models
            for join in self.joins
        ):
            raise ValueError("advanced semantic joins must connect included models")

        endpoint_pairs = tuple(
            frozenset((join.left_model.root, join.right_model.root)) for join in self.joins
        )
        if len(endpoint_pairs) != len(set(endpoint_pairs)):
            raise ValueError("advanced semantic context cannot contain duplicate join paths")
        if not _models_are_connected(model_ids, self.joins):
            raise ValueError("advanced semantic context models must form one connected graph")
        return self

    def field_index(self) -> dict[str, AdvancedSemanticField]:
        return {item.id.root: item for item in self.fields}

    @property
    def fingerprint(self) -> str:
        return advanced_semantic_context_fingerprint(self)


class AdvancedInterpretationInput(FrozenDomainModel):
    """Exact interpretation-port input after bounded semantic retrieval."""

    query: AdvancedNaturalLanguageInput
    extraction: AdvancedMentionExtraction
    context: AdvancedApprovedSemanticContext

    @model_validator(mode="after")
    def extraction_must_bind_and_be_grounded_in_query(self) -> Self:
        _validate_extraction_grounding(self.query, self.extraction)
        return self


class AdvancedInterpretationEnvelope(FrozenDomainModel):
    """Strict model envelope: one advanced request or explicit ambiguities, never SQL."""

    request_digest: str
    mention_fingerprint: str
    semantic_context_fingerprint: str
    request: AdvancedAnalyticalRequest | None = None
    ambiguities: tuple[AdvancedInterpretationAmbiguity, ...] = Field(
        default=(),
        max_length=12,
    )

    @field_validator(
        "request_digest",
        "mention_fingerprint",
        "semantic_context_fingerprint",
    )
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "advanced interpretation binding")

    @model_validator(mode="after")
    def envelope_must_contain_exactly_one_outcome(self) -> Self:
        if (self.request is None) == (not self.ambiguities):
            raise ValueError(
                "advanced interpretation requires either one request or explicit ambiguities"
            )
        if len(self.ambiguities) != len(set(self.ambiguities)):
            raise ValueError("advanced interpretation ambiguities must be unique")
        return self

    @property
    def fingerprint(self) -> str:
        return advanced_interpretation_fingerprint(self)


class AdvancedInterpretationResult(FrozenDomainModel):
    """Interpretation-port result with exact input and structural grounding checks."""

    input: AdvancedInterpretationInput
    envelope: AdvancedInterpretationEnvelope
    usage: ProviderUsageFacts | None = None

    @model_validator(mode="after")
    def envelope_must_bind_input_and_usage_must_match(self) -> Self:
        expected = self.input
        if self.envelope.request_digest != expected.query.digest:
            raise ValueError("advanced interpretation does not bind the exact query")
        if self.envelope.mention_fingerprint != expected.extraction.fingerprint:
            raise ValueError("advanced interpretation does not bind the exact mentions")
        if self.envelope.semantic_context_fingerprint != expected.context.fingerprint:
            raise ValueError("advanced interpretation does not bind the exact semantic context")
        if self.envelope.request is not None:
            _validate_request_grounding(self.envelope.request, expected.context)
        if self.usage is not None and (
            self.usage.stage is not ProviderStage.INTERPRETATION
            or self.usage.outcome is not ProviderOutcomeCode.SUCCEEDED
        ):
            raise ValueError("advanced interpretation requires successful interpretation usage")
        return self


class AdvancedMappingReview(FrozenDomainModel):
    """Bounded public evidence for one selected approved mapping."""

    logical_field: LogicalFieldRef
    physical_field: PhysicalFieldRef
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: tuple[str, ...] = Field(min_length=1, max_length=8)
    risks: tuple[str, ...] = Field(default=(), max_length=8)


class AdvancedJoinReview(FrozenDomainModel):
    """Bounded public evidence and risks for one selected join contract."""

    contract_id: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    evidence: tuple[str, ...] = Field(min_length=1, max_length=8)
    risks: tuple[str, ...] = Field(default=(), max_length=8)


class AdvancedQueryPreview(FrozenDomainModel):
    """Confirmable preview without raw natural-language text."""

    request_digest: str
    mention_fingerprint: str
    semantic_context_fingerprint: str
    approved_context_fingerprint: str
    governed_registry_fingerprint: str
    scope_fingerprint: str
    activation_generation: int | None = Field(default=None, ge=1)
    active_pointer_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    interpretation_fingerprint: str
    resolved_plan_fingerprint: str
    datasets: tuple[PhysicalDatasetRef, ...] = Field(min_length=1, max_length=3)
    join_contract_ids: tuple[str, ...] = Field(default=(), max_length=2)
    mapping_reviews: tuple[AdvancedMappingReview, ...] = Field(
        default=(),
        max_length=24,
    )
    join_reviews: tuple[AdvancedJoinReview, ...] = Field(default=(), max_length=2)
    assumptions: tuple[ResolutionAssumption, ...] = Field(min_length=1, max_length=24)
    fanout_mitigations: tuple[FanoutMitigation, ...] = Field(default=(), max_length=12)
    route: AdvancedQueryRoute
    routed_request: AnalyticalRequestLike
    routed_request_fingerprint: str
    validated_request: ValidatedRequestLike

    @field_validator(
        "request_digest",
        "mention_fingerprint",
        "semantic_context_fingerprint",
        "approved_context_fingerprint",
        "governed_registry_fingerprint",
        "scope_fingerprint",
        "interpretation_fingerprint",
        "resolved_plan_fingerprint",
        "routed_request_fingerprint",
    )
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "advanced query preview fingerprint")

    @model_validator(mode="after")
    def route_and_validation_must_bind_exact_request(self) -> Self:
        if (self.activation_generation is None) != (self.active_pointer_fingerprint is None):
            raise ValueError(
                "advanced query preview requires generation and active pointer together"
            )
        if len(self.datasets) != len(set(self.datasets)):
            raise ValueError("advanced query preview datasets must be unique")
        if len(self.join_contract_ids) != len(set(self.join_contract_ids)):
            raise ValueError("advanced query preview join contracts must be unique")
        mapping_fields = tuple(item.logical_field for item in self.mapping_reviews)
        if len(mapping_fields) != len(set(mapping_fields)):
            raise ValueError("advanced query preview mapping reviews must be unique")
        reviewed_joins = tuple(item.contract_id for item in self.join_reviews)
        if len(reviewed_joins) != len(set(reviewed_joins)):
            raise ValueError("advanced query preview join reviews must be unique")
        if reviewed_joins and set(reviewed_joins) != set(self.join_contract_ids):
            raise ValueError("advanced query preview join reviews must match selected joins")
        if any(item.automatic for item in self.fanout_mitigations):
            raise ValueError("advanced query preview cannot silently apply a fanout mitigation")
        if self.route is AdvancedQueryRoute.V2 and not isinstance(
            self.routed_request, AdvancedAnalyticalRequest
        ):
            raise ValueError("v2 route requires an advanced analytical request")
        if self.route is AdvancedQueryRoute.V1 and not isinstance(
            self.routed_request, AnalyticalRequest
        ):
            raise ValueError("v1 route requires a simple analytical request")
        if self.validated_request.request != self.routed_request:
            raise ValueError("preview validated request differs from routed request")
        if self.validated_request.context_fingerprint != self.approved_context_fingerprint:
            raise ValueError("preview validated request uses a different approved context")
        expected_request_fingerprint = advanced_routed_request_fingerprint(self.routed_request)
        if self.routed_request_fingerprint != expected_request_fingerprint:
            raise ValueError("preview routed request fingerprint does not match")
        return self

    @property
    def fingerprint(self) -> str:
        return advanced_query_preview_fingerprint(self)


class AdvancedQueryConfirmation(FrozenDomainModel):
    """Explicit client choice bound to one exact text-free preview."""

    action: AdvancedQueryConfirmationAction
    request_digest: str
    preview_fingerprint: str
    routed_request_fingerprint: str
    token: SignedAdvancedQueryPreviewToken

    @field_validator(
        "request_digest",
        "preview_fingerprint",
        "routed_request_fingerprint",
    )
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "advanced query confirmation fingerprint")

    @property
    def fingerprint(self) -> str:
        return advanced_query_confirmation_fingerprint(self)


class AdvancedPreviewTokenClaims(FrozenDomainModel):
    """Digest-only qsp2 claims binding one exact preview to current governed context."""

    version: Literal[2] = 2
    request_digest: str
    mention_fingerprint: str
    semantic_context_fingerprint: str
    approved_context_fingerprint: str
    governed_registry_fingerprint: str
    scope_fingerprint: str
    activation_generation: int | None = Field(default=None, ge=1)
    active_pointer_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    interpretation_fingerprint: str
    resolved_plan_fingerprint: str
    routed_request_fingerprint: str
    preview_fingerprint: str
    issued_at: datetime
    expires_at: datetime
    nonce: str = Field(min_length=16, max_length=120)

    @field_validator(
        "request_digest",
        "mention_fingerprint",
        "semantic_context_fingerprint",
        "approved_context_fingerprint",
        "governed_registry_fingerprint",
        "scope_fingerprint",
        "interpretation_fingerprint",
        "resolved_plan_fingerprint",
        "routed_request_fingerprint",
        "preview_fingerprint",
    )
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "advanced preview token fingerprint")

    @model_validator(mode="after")
    def active_head_must_be_complete(self) -> Self:
        if (self.activation_generation is None) != (self.active_pointer_fingerprint is None):
            raise ValueError(
                "advanced preview claims require generation and active pointer together"
            )
        return self

    @field_validator("issued_at", "expires_at")
    @classmethod
    def timestamps_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("advanced preview timestamps must include a timezone")
        return value

    @field_validator("nonce")
    @classmethod
    def nonce_must_be_inert(cls, value: str) -> str:
        if _NONCE.fullmatch(value) is None:
            raise ValueError("advanced preview nonce is invalid")
        return value

    @model_validator(mode="after")
    def lifetime_must_be_positive_and_bounded(self) -> Self:
        seconds = (self.expires_at - self.issued_at).total_seconds()
        if seconds <= 0 or seconds > MAX_PREVIEW_TTL_SECONDS:
            raise ValueError("advanced preview lifetime is invalid")
        return self


def advanced_query_request_digest(value: AdvancedNaturalLanguageInput) -> str:
    return _fingerprint(
        {
            "text": value.text,
            "language": value.language.value,
        }
    )


def advanced_mention_extraction_fingerprint(value: AdvancedMentionExtraction) -> str:
    return _fingerprint(value.model_dump(mode="json"))


def advanced_semantic_context_fingerprint(
    value: AdvancedApprovedSemanticContext,
) -> str:
    return _fingerprint(value.model_dump(mode="json"))


def advanced_interpretation_fingerprint(
    value: AdvancedInterpretationEnvelope,
) -> str:
    return _fingerprint(value.model_dump(mode="json"))


def advanced_routed_request_fingerprint(value: AnalyticalRequestLike) -> str:
    return _fingerprint(value.model_dump(mode="json"))


def advanced_query_preview_fingerprint(value: AdvancedQueryPreview) -> str:
    return _fingerprint(value.model_dump(mode="json"))


def advanced_query_confirmation_fingerprint(
    value: AdvancedQueryConfirmation,
) -> str:
    return _fingerprint(value.model_dump(mode="json"))


def confirm_advanced_query_preview(
    preview: AdvancedQueryPreview,
    confirmation: AdvancedQueryConfirmation,
) -> ValidatedRequestLike:
    """Return only the exactly reviewed request, or fail closed on any mismatch."""

    if confirmation.request_digest != preview.request_digest:
        raise ValueError("confirmation request digest does not match the preview")
    if confirmation.preview_fingerprint != preview.fingerprint:
        raise ValueError("confirmation preview fingerprint does not match")
    if confirmation.routed_request_fingerprint != preview.routed_request_fingerprint:
        raise ValueError("confirmation routed request fingerprint does not match")
    return preview.validated_request


def _validate_extraction_grounding(
    query: AdvancedNaturalLanguageInput,
    extraction: AdvancedMentionExtraction,
) -> None:
    if extraction.request_digest != query.digest:
        raise ValueError("mention extraction does not bind the exact query")
    for mention in extraction.mentions:
        span = mention.source_span
        if span.end > len(query.text):
            raise ValueError("query mention source span exceeds the submitted text")
        if query.text[span.start : span.end] != mention.value:
            raise ValueError("query mention value is not grounded in its exact source span")


def _validate_request_grounding(
    request: AdvancedAnalyticalRequest,
    context: AdvancedApprovedSemanticContext,
) -> None:
    model_ids = {item.id.root for item in context.models}
    if request.primary_entity.root not in model_ids:
        raise ValueError("advanced request primary entity is absent from semantic context")

    referenced_fields = {item.field.root for item in request.fields}
    referenced_fields.update(item.field.root for item in request.metrics if item.field is not None)
    predicates: list[Filter] = []
    if request.where is not None:
        predicates.extend(logical_predicate_filters(request.where))
    for metric in request.metrics:
        if metric.condition is not None:
            predicates.extend(logical_predicate_filters(metric.condition))
    referenced_fields.update(item.field.root for item in predicates)

    field_index = context.field_index()
    unknown_fields = referenced_fields - set(field_index)
    if unknown_fields:
        raise ValueError("advanced request references fields outside semantic context")

    for predicate in predicates:
        field = field_index[predicate.field.root]
        if not field.allowed_values:
            continue
        values = predicate.value if isinstance(predicate.value, tuple) else (predicate.value,)
        string_values = tuple(value for value in values if isinstance(value, str))
        if predicate.operator in {
            FilterOperator.EQUALS,
            FilterOperator.NOT_EQUALS,
            FilterOperator.IN,
        } and any(value not in field.allowed_values for value in string_values):
            raise ValueError("advanced request uses a value outside approved field values")

    required_models = {
        request.primary_entity.root,
        *(field_id.split(".", 1)[0] for field_id in referenced_fields),
    }
    if not _required_models_are_connected(required_models, context):
        raise ValueError("advanced request models are not connected by approved context joins")


def _models_are_connected(
    models: tuple[str, ...],
    joins: tuple[AdvancedSemanticJoin, ...],
) -> bool:
    if len(models) == 1:
        return not joins
    return _graph_reaches_all(set(models), joins, models[0])


def _required_models_are_connected(
    required_models: set[str],
    context: AdvancedApprovedSemanticContext,
) -> bool:
    if len(required_models) <= 1:
        return True
    all_models = {item.id.root for item in context.models}
    start = next(iter(required_models))
    reached = _graph_reachable(context.joins, start)
    return required_models <= reached <= all_models


def _graph_reaches_all(
    models: set[str],
    joins: tuple[AdvancedSemanticJoin, ...],
    start: str,
) -> bool:
    return models <= _graph_reachable(joins, start)


def _graph_reachable(
    joins: tuple[AdvancedSemanticJoin, ...],
    start: str,
) -> set[str]:
    reached = {start}
    pending = [start]
    while pending:
        current = pending.pop()
        for join in joins:
            endpoints = {join.left_model.root, join.right_model.root}
            if current not in endpoints:
                continue
            neighbour = next(iter(endpoints - {current}))
            if neighbour not in reached:
                reached.add(neighbour)
                pending.append(neighbour)
    return reached


def _sha256(value: str, label: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


def _fingerprint(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
