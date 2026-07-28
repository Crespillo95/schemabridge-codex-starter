"""Two-stage OpenAI adapters with server-owned Query Studio semantics."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import NoReturn, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from schemabridge.adapters.language.openai_boundary import (
    OpenAIAdapterError,
    OpenAIAdapterErrorCode,
    OpenAIParsedOutput,
    OpenAIResponsesBoundary,
    OpenAIResponsesConfig,
    OpenAIStage,
    build_expansion_provider_input,
    build_interpretation_provider_input,
    create_managed_openai_client_from_environment,
    normalize_and_screen_user_text,
    provider_input_token_reservation_bound,
    provider_input_token_reservation_bound_for_payload,
    response_config_fingerprint,
    validate_safety_identifier,
)
from schemabridge.application.ports.query_studio import (
    QueryStudioPortError,
    QueryStudioPortErrorCode,
    QueryStudioRetryDisposition,
)
from schemabridge.domain.concepts import CanonicalType
from schemabridge.domain.query_studio import (
    EXTERNAL_AI_ATTEMPT_POLICY_VERSION,
    MAX_DESCRIPTION_PROBES,
    QUERY_STUDIO_ORCHESTRATION_POLICY_VERSION,
    QUERY_STUDIO_PUBLIC_METADATA_POLICY_VERSION,
    ApprovedPublicMetadataSurface,
    DescriptionExpansion,
    DescriptionExpansionInput,
    DescriptionExpansionResult,
    DescriptionExpansionRoute,
    DescriptionQuery,
    DescriptionSearchProbe,
    ProposalAmbiguityKind,
    ProposedDimension,
    ProposedFilter,
    ProposedMetric,
    ProviderConfigurationFacts,
    ProviderOutcomeCode,
    ProviderOutputFailureCategory,
    ProviderStage,
    ProviderUsageFacts,
    QueryFieldPurpose,
    QueryStudioInterpretationInput,
    QueryStudioInterpretationResult,
    QueryStudioModelProposal,
    QueryStudioPromptCandidate,
    SemanticMatchState,
    explicit_logical_field_reference,
    find_explicit_logical_field_references,
)
from schemabridge.domain.query_studio_proposals import (
    QUERY_STUDIO_PROPOSAL_NORMALIZER_VERSION,
    canonicalize_query_studio_proposal,
)
from schemabridge.domain.request_context import LogicalFieldRole
from schemabridge.domain.requests import (
    DateGrain,
    FilterOperator,
    FilterScalar,
    MetricOperation,
)

_ADAPTER_LABEL = "openai_responses_structured"
_SHA256_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_GROUPING_INTENT = re.compile(
    r"\bagrupa(?:r|dos?|das?)?\b[^.?!]{0,200}\bpor\b"
    r"|\bgroup(?:ed|ing)?\b[^.?!]{0,200}\bby\b"
)
_AGGREGATION_INTENT = re.compile(
    r"\b(?:count|sum|average|avg|suma(?:r)?|promedio|totaliza(?:r)?)\b"
    r"|\bcuenta(?:me)?\s+(?:el|la|los|las|todos?|todas?|cuantos?|cuantas?)\b"
    r"|\bcuenta(?:me)?\s+(?:clientes?|productos?|pedidos?|ordenes?|órdenes?|"
    r"titulares?|cuentas?|registros?|envios?|envíos?|ventas?|lineas?|líneas?)\b"
)
_DIMENSION_CONNECTOR = re.compile(r"\b(?:by|por)\b")
_CONDITIONAL_FILTER_INTENT = re.compile(
    r"\b(?:where|whose|donde|cuyo|cuya|cuyos|cuyas)\b"
    r"|\b(?:who|that)\s+(?:is|are)\b"
    r"|\bque\s+sean?\b"
    r"|\b(?:for|para)\b[^.?!]{0,80}\b(?:completed|completad[oa]s?)\b"
)
_VALUE_FILTER_INTENT = re.compile(
    r"\b(?:active|inactive|enabled|disabled|delivered|cancelled|"
    r"canceled|completed|second|secondary|primary|activo|activa|activos|activas|"
    r"inactivo|inactiva|inactivos|inactivas|habilitado|habilitada|"
    r"deshabilitado|deshabilitada|entregado|entregada|entregados|entregadas|"
    r"cancelado|cancelada|cancelados|canceladas|completado|completada|"
    r"completados|completadas|segundo|segunda|segundos|segundas|principal)\b"
)
_FIELD_LIST_COORDINATOR = re.compile(r"\b(?:and|y)\b")
_COMPOUND_TEMPORAL_FIELD = re.compile(r"\b(?:date and time|fecha y hora)\b")
_NON_ATOMIC_PROBE = re.compile(
    r"\bagrupa(?:r|dos?|das?)?\b[^.?!]{0,100}\bpor\b"
    r"|\bgroup(?:ed|ing)?\b[^.?!]{0,100}\bby\b"
    r"|\b(?:count|sum|average|avg|suma(?:r)?|promedio)\b[^.?!]{0,100}\b(?:by|por)\b"
    r"|\b(?:where|whose|donde)\b"
    r"|\b(?:who|that)\s+(?:is|are)\b"
    r"|\bque\s+sean?\b"
)
_SOURCE_TOKEN = re.compile(r"[^\W_]+(?:[./:+-][^\W_]+)*", re.UNICODE)
_SEMANTIC_ANCHOR_ALIASES = {
    "account": "account",
    "accounts": "account",
    "active": "active",
    "activo": "active",
    "activa": "active",
    "activos": "active",
    "activas": "active",
    "alta": "registration",
    "almacen": "warehouse",
    "amount": "amount",
    "asistente": "assistant",
    "assistant": "assistant",
    "balance": "balance",
    "canal": "channel",
    "cantidad": "quantity",
    "card": "card",
    "carrier": "carrier",
    "category": "category",
    "categories": "category",
    "categoria": "category",
    "categorias": "category",
    "channel": "channel",
    "clave": "identifier",
    "client": "customer",
    "clients": "customer",
    "cliente": "customer",
    "clientes": "customer",
    "codigo": "code",
    "code": "code",
    "connector": "connector",
    "conector": "connector",
    "correo": "email",
    "country": "country",
    "cuenta": "account",
    "cuentas": "account",
    "customer": "customer",
    "customers": "customer",
    "completed": "completed",
    "completada": "completed",
    "completadas": "completed",
    "completado": "completed",
    "completados": "completed",
    "date": "temporal",
    "day": "temporal",
    "dia": "temporal",
    "descuento": "discount",
    "delivered": "delivery",
    "deliveries": "delivery",
    "delivery": "delivery",
    "device": "device",
    "discount": "discount",
    "dispositivo": "device",
    "email": "email",
    "empleada": "employee",
    "empleadas": "employee",
    "empleado": "employee",
    "empleados": "employee",
    "employee": "employee",
    "employees": "employee",
    "entrega": "delivery",
    "entregas": "delivery",
    "entregado": "delivery",
    "envio": "shipment",
    "envios": "shipment",
    "estado": "status",
    "factura": "invoice",
    "fecha": "temporal",
    "holder": "holder",
    "hora": "temporal",
    "id": "identifier",
    "identifier": "identifier",
    "identificador": "identifier",
    "identificadores": "identifier",
    "importe": "amount",
    "ingreso": "amount",
    "ingresos": "amount",
    "instante": "temporal",
    "impuesto": "tax",
    "impuestos": "tax",
    "invoice": "invoice",
    "key": "identifier",
    "latency": "latency",
    "lead": "lead",
    "line": "line",
    "linea": "line",
    "lineas": "line",
    "marketing": "marketing",
    "momento": "temporal",
    "mes": "temporal",
    "month": "temporal",
    "monto": "amount",
    "net": "net",
    "neto": "net",
    "neta": "net",
    "netos": "net",
    "netas": "net",
    "numero": "quantity",
    "order": "order",
    "ordered": "order",
    "orders": "order",
    "pais": "country",
    "pago": "payment",
    "payment": "payment",
    "pedido": "order",
    "pedidos": "order",
    "precio": "price",
    "price": "price",
    "product": "product",
    "products": "product",
    "producto": "product",
    "productos": "product",
    "proveedor": "supplier",
    "quantity": "quantity",
    "reembolso": "refund",
    "reference": "reference",
    "referencia": "reference",
    "refund": "refund",
    "region": "region",
    "registration": "registration",
    "registered": "registration",
    "registro": "registration",
    "revenue": "amount",
    "role": "role",
    "saldo": "balance",
    "sale": "sale",
    "sales": "sale",
    "second": "secondary",
    "secondary": "secondary",
    "segundo": "secondary",
    "segunda": "secondary",
    "segundos": "secondary",
    "segundas": "secondary",
    "semana": "temporal",
    "seguimiento": "tracking",
    "session": "session",
    "sesion": "session",
    "shipment": "shipment",
    "shipments": "shipment",
    "soporte": "support",
    "status": "status",
    "supplier": "supplier",
    "support": "support",
    "tarjeta": "card",
    "tax": "tax",
    "ticket": "ticket",
    "titular": "holder",
    "titulares": "holder",
    "time": "temporal",
    "timestamp": "temporal",
    "total": "amount",
    "tracking": "tracking",
    "transportista": "carrier",
    "unit": "quantity",
    "units": "quantity",
    "unitario": "quantity",
    "unitaria": "quantity",
    "unidades": "quantity",
    "venta": "sale",
    "warehouse": "warehouse",
    "week": "temporal",
    "year": "temporal",
    "ano": "temporal",
}
_BUSINESS_ENTITY_ANCHORS = frozenset(
    {
        "account",
        "assistant",
        "card",
        "carrier",
        "connector",
        "customer",
        "device",
        "employee",
        "holder",
        "invoice",
        "lead",
        "line",
        "order",
        "payment",
        "product",
        "refund",
        "sale",
        "session",
        "shipment",
        "supplier",
        "ticket",
        "warehouse",
    }
)
_MEASURE_FOCUS_ANCHORS = frozenset(
    {
        "amount",
        "balance",
        "discount",
        "price",
        "quantity",
        "tax",
    }
)
_FILTER_VALUE_FOCUS_ANCHORS = frozenset(
    {
        "active",
        "completed",
        "delivery",
        "secondary",
    }
)
_FOCUS_ANCHOR_ORDER = {
    anchor: index
    for index, anchor in enumerate(
        (
            "customer",
            "product",
            "order",
            "sale",
            "line",
            "shipment",
            "account",
            "holder",
            "employee",
            "supplier",
            "invoice",
            "payment",
            "refund",
            "card",
            "carrier",
            "warehouse",
            "ticket",
            "assistant",
            "connector",
            "device",
            "lead",
            "session",
            "registration",
            "delivery",
            "country",
            "category",
            "status",
            "role",
            "identifier",
            "net",
            "amount",
            "quantity",
            "price",
            "balance",
            "discount",
            "tax",
            "code",
            "reference",
            "email",
            "region",
            "channel",
            "marketing",
            "tracking",
            "latency",
            "temporal",
            "active",
            "secondary",
            "completed",
            "support",
        )
    )
}


class _OpenAIExpansionSlot(BaseModel):
    """Minimal provider output; all semantic controls remain server-owned."""

    model_config = ConfigDict(extra="forbid")

    slot_id: str = Field(pattern=r"^(?:dimension|metric|filter)_[1-6]$")
    query: DescriptionQuery


class _OpenAIDescriptionExpansion(BaseModel):
    """Strict provider output containing only grounded atomic search text."""

    model_config = ConfigDict(extra="forbid")

    slots: tuple[_OpenAIExpansionSlot, ...] = Field(
        min_length=1,
        max_length=MAX_DESCRIPTION_PROBES,
    )


class _OpenAISelectedSlot(BaseModel):
    """One local option reference; candidate IDs and values never leave the server."""

    model_config = ConfigDict(extra="forbid")

    slot_id: str = Field(pattern=r"^(?:dimension|metric|filter)_[1-6]$")
    option_index: int = Field(ge=1, le=1)


class _OpenAIQueryStudioSelections(BaseModel):
    """Minimal interpretation envelope; semantic state is reconstructed locally."""

    model_config = ConfigDict(extra="forbid")

    selections: tuple[_OpenAISelectedSlot, ...] = Field(max_length=MAX_DESCRIPTION_PROBES)
    ambiguity_kinds: tuple[ProposalAmbiguityKind, ...] = Field(max_length=12)


@dataclass(frozen=True, slots=True)
class _ServerSlotSpec:
    """Trusted closed semantics derived from the original business text."""

    slot_id: str
    intended_use: QueryFieldPurpose
    roles: tuple[LogicalFieldRole, ...]
    canonical_types: tuple[CanonicalType, ...]
    metric_operation: MetricOperation | None
    filter_operator: FilterOperator | None
    date_grain: DateGrain | None
    semantic_focus: tuple[str, ...]
    owner_focus: tuple[str, ...]
    source_focus: tuple[str, ...]
    operational_query: DescriptionQuery
    qualified_reference: str | None

    def provider_payload(self) -> dict[str, object]:
        return {
            "slot_id": self.slot_id,
            "intended_use": self.intended_use.value,
            "metric_operation": (
                None if self.metric_operation is None else self.metric_operation.value
            ),
            "filter_operator": (
                None if self.filter_operator is None else self.filter_operator.value
            ),
            "date_grain": None if self.date_grain is None else self.date_grain.value,
            "semantic_focus": self.semantic_focus,
        }


@dataclass(frozen=True, slots=True)
class _SourceToken:
    """One exact source token with a normalized semantic anchor."""

    start: int
    end: int
    normalized: str
    anchor: str | None


@dataclass(frozen=True, slots=True)
class _ServerSemanticFocus:
    """One locally derived retrieval contract for a server-owned slot."""

    semantic_focus: tuple[str, ...]
    owner_focus: tuple[str, ...]
    source_focus: tuple[str, ...]
    operational_query: DescriptionQuery
    qualified_reference: str | None = None


@dataclass(frozen=True, slots=True)
class _QualifiedFieldFocus:
    """A bounded explicit ``LogicalModel.field_name`` reference."""

    semantic_focus: tuple[str, ...]
    owner_focus: tuple[str, ...]
    operational_query: DescriptionQuery
    normalized_reference: str


def openai_expansion_response_config_fingerprint(config: OpenAIResponsesConfig) -> str:
    """Fingerprint the exact provider-only expansion schema and prompt."""

    return response_config_fingerprint(
        config,
        stage=OpenAIStage.EXPANSION,
        output_type=_OpenAIDescriptionExpansion,
    )


def openai_expansion_input_token_reservation_bound() -> int:
    """Reserve against the exact provider-only expansion schema."""

    return provider_input_token_reservation_bound(
        stage=OpenAIStage.EXPANSION,
        output_type=_OpenAIDescriptionExpansion,
    )


def openai_expansion_input_token_reservation_bound_for(
    value: DescriptionExpansionInput,
) -> int:
    """Bound the exact expansion payload used by the provider adapter."""

    return provider_input_token_reservation_bound_for_payload(
        stage=OpenAIStage.EXPANSION,
        output_type=_OpenAIDescriptionExpansion,
        provider_input=_expansion_provider_input(value),
    )


def openai_interpretation_response_config_fingerprint(config: OpenAIResponsesConfig) -> str:
    """Fingerprint the exact provider-only interpretation schema and prompt."""

    return response_config_fingerprint(
        config,
        stage=OpenAIStage.INTERPRETATION,
        output_type=_OpenAIQueryStudioSelections,
    )


def openai_provider_contract_fingerprint(config: OpenAIResponsesConfig) -> str:
    """Bind the composed interpretation contract and server-owned normalization."""

    return hashlib.sha256(
        (
            "m27-openai-intent-only-provider-contract-v1"
            + "\0"
            + openai_interpretation_response_config_fingerprint(config)
            + "\0"
            + QUERY_STUDIO_PROPOSAL_NORMALIZER_VERSION
        ).encode()
    ).hexdigest()


def openai_public_metadata_policy_fingerprint(
    config: OpenAIResponsesConfig,
    *,
    semantic_scope_fingerprint: str,
    registry_fingerprint: str,
) -> str:
    """Bind egress approval to one tenant scope, registry revision, and provider contract."""

    if (
        _SHA256_FINGERPRINT.fullmatch(semantic_scope_fingerprint) is None
        or _SHA256_FINGERPRINT.fullmatch(registry_fingerprint) is None
    ):
        raise OpenAIAdapterError(OpenAIAdapterErrorCode.CONFIGURATION_INVALID)

    return hashlib.sha256(
        (
            QUERY_STUDIO_PUBLIC_METADATA_POLICY_VERSION
            + "\0"
            + openai_provider_contract_fingerprint(config)
            + "\0"
            + semantic_scope_fingerprint
            + "\0"
            + registry_fingerprint
        ).encode()
    ).hexdigest()


def openai_interpretation_input_token_reservation_bound() -> int:
    """Reserve against the exact provider-only interpretation schema."""

    return provider_input_token_reservation_bound(
        stage=OpenAIStage.INTERPRETATION,
        output_type=_OpenAIQueryStudioSelections,
    )


def openai_interpretation_input_token_reservation_bound_for(
    value: QueryStudioInterpretationInput,
) -> int:
    """Bound the exact interpretation payload used by the provider adapter."""

    return provider_input_token_reservation_bound_for_payload(
        stage=OpenAIStage.INTERPRETATION,
        output_type=_OpenAIQueryStudioSelections,
        provider_input=_interpretation_provider_input(value),
    )


def _expansion_provider_input(value: DescriptionExpansionInput) -> str:
    required_slots = _required_slot_specs(value.text.root)
    return build_expansion_provider_input(
        text=value.text.root,
        language=value.language.value,
        required_slots=tuple(slot.provider_payload() for slot in required_slots),
    )


def _interpretation_provider_input(
    value: QueryStudioInterpretationInput,
    *,
    configuration: ProviderConfigurationFacts | None = None,
) -> str:
    _require_server_owned_expansion(value)
    _require_approved_public_metadata_surface(value, configuration=configuration)
    vocabulary = value.vocabulary
    options_by_slot = _selection_options_by_slot(value)
    model_definitions = {model.id.root: model.definition for model in vocabulary.models}
    metadata_payload = {
        "SELECTION_SLOTS_JSON": tuple(
            {
                "slot_id": probe.purpose_id,
                "query": probe.query.root,
                "source_span": probe.source_span,
                "intended_use": probe.intended_use.value,
                "semantic_focus": probe.semantic_focus,
                "owner_focus": probe.owner_focus,
                "options": tuple(
                    {
                        "option_index": option_index,
                        "definition": candidate.definition,
                        "model_definition": model_definitions[_candidate_logical_model(candidate)],
                        "canonical_type": candidate.canonical_type.value,
                        "role": candidate.role.value,
                        "allowed_values": candidate.allowed_values,
                        "score": _candidate_score_for_slot(
                            candidate,
                            probe.purpose_id,
                        ),
                        "risks": tuple(risk.value for risk in candidate.risks),
                    }
                    for option_index, candidate in enumerate(
                        options_by_slot[probe.purpose_id],
                        start=1,
                    )
                ),
                "metric_operation": (
                    None if probe.metric_operation is None else probe.metric_operation.value
                ),
                "filter_operator": (
                    None if probe.filter_operator is None else probe.filter_operator.value
                ),
                "date_grain": (None if probe.date_grain is None else probe.date_grain.value),
            }
            for probe in value.expansion.probes
        ),
    }
    public_metadata = tuple(
        [
            *(model.definition for model in vocabulary.models),
            *(
                text
                for candidates in options_by_slot.values()
                for candidate in candidates
                for text in (candidate.definition, *candidate.allowed_values)
            ),
            *(probe.query.root for probe in value.expansion.probes),
            *(
                probe.source_span
                for probe in value.expansion.probes
                if probe.source_span is not None
            ),
        ]
    )
    return build_interpretation_provider_input(
        text=value.text.root,
        language=value.language.value,
        metadata_payload=metadata_payload,
        public_metadata_text=public_metadata,
        metadata_approved_public=True,
    )


def _require_approved_public_metadata_surface(
    value: QueryStudioInterpretationInput,
    *,
    configuration: ProviderConfigurationFacts | None,
) -> ApprovedPublicMetadataSurface:
    surface = value.public_metadata_surface
    if surface is None or surface.vocabulary_fingerprint != value.vocabulary.fingerprint:
        raise OpenAIAdapterError(OpenAIAdapterErrorCode.METADATA_NOT_PUBLIC)
    if configuration is not None and (
        surface.policy_fingerprint != configuration.public_metadata_policy_fingerprint
        or surface.semantic_scope_fingerprint
        != configuration.public_metadata_semantic_scope_fingerprint
        or surface.registry_fingerprint != configuration.public_metadata_registry_fingerprint
    ):
        raise OpenAIAdapterError(OpenAIAdapterErrorCode.METADATA_NOT_PUBLIC)
    return surface


def _required_slot_specs(value: str) -> tuple[_ServerSlotSpec, ...]:
    """Derive the complete closed analytical shape without provider authority."""

    purposes = _required_expansion_purposes(value)
    if not purposes or len(purposes) > MAX_DESCRIPTION_PROBES:
        raise QueryStudioPortError(
            QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
            "the business text does not define one bounded field-slot contract",
            retry_disposition=QueryStudioRetryDisposition.NEVER,
            output_failure_category=ProviderOutputFailureCategory.SEMANTIC_CONTRACT,
        )
    analytic = bool(_required_analytic_purposes(value))
    normalized = _normalized_intent_text(value)
    dimension_fragments = _dimension_fragments(normalized)
    metric_fragments = _metric_fragments(normalized)
    primary_entity = _primary_business_entity(normalized)
    counts: Counter[QueryFieldPurpose] = Counter()
    specs: list[_ServerSlotSpec] = []
    for purpose in purposes:
        counts[purpose] += 1
        ordinal = counts[purpose]
        fragment = (
            dimension_fragments[ordinal - 1]
            if purpose is QueryFieldPurpose.DIMENSION and ordinal <= len(dimension_fragments)
            else normalized
        )
        metric_operation = (
            _server_metric_operation(normalized) if purpose is QueryFieldPurpose.METRIC else None
        )
        filter_operator = (
            _server_filter_operator(normalized) if purpose is QueryFieldPurpose.FILTER else None
        )
        role, canonical_types = _server_field_constraints(
            purpose=purpose,
            fragment=fragment,
            metric_operation=metric_operation,
        )
        focus = _derive_server_semantic_focus(
            value=normalized,
            purpose=purpose,
            ordinal=ordinal,
            metric_operation=metric_operation,
            dimension_fragments=dimension_fragments,
            metric_fragments=metric_fragments,
            primary_entity=primary_entity,
        )
        specs.append(
            _ServerSlotSpec(
                slot_id=f"{purpose.value}_{ordinal}",
                intended_use=purpose,
                roles=(role,),
                canonical_types=canonical_types,
                metric_operation=metric_operation,
                filter_operator=filter_operator,
                date_grain=(
                    _server_date_grain(fragment)
                    if analytic and purpose is QueryFieldPurpose.DIMENSION
                    else None
                ),
                semantic_focus=focus.semantic_focus,
                owner_focus=focus.owner_focus,
                source_focus=focus.source_focus,
                operational_query=focus.operational_query,
                qualified_reference=focus.qualified_reference,
            )
        )
    return tuple(specs)


def _server_owner_focus(semantic_focus: tuple[str, ...]) -> tuple[str, ...]:
    """Keep only exact governed-model identity anchors for closure eligibility."""

    return tuple(anchor for anchor in semantic_focus if anchor in _BUSINESS_ENTITY_ANCHORS)


def _derive_server_semantic_focus(
    *,
    value: str,
    purpose: QueryFieldPurpose,
    ordinal: int,
    metric_operation: MetricOperation | None,
    dimension_fragments: tuple[str, ...],
    metric_fragments: tuple[str, ...],
    primary_entity: str | None,
) -> _ServerSemanticFocus:
    """Derive one closed focus and operational query without provider wording."""

    if not _required_analytic_purposes(value):
        qualified = _exact_qualified_field_focus(value)
        if qualified is not None:
            return _ServerSemanticFocus(
                semantic_focus=qualified.semantic_focus,
                owner_focus=qualified.owner_focus,
                source_focus=qualified.semantic_focus,
                operational_query=qualified.operational_query,
                qualified_reference=qualified.normalized_reference,
            )
        source_focus = _ordered_semantic_anchors(value)
        if not source_focus:
            _raise_semantic_contract("the atomic field description has no closed semantic focus")
        semantic_focus = _canonical_focus(source_focus)
        return _ServerSemanticFocus(
            semantic_focus=semantic_focus,
            owner_focus=_server_owner_focus(semantic_focus),
            source_focus=source_focus,
            operational_query=_description_query_from_focus(semantic_focus),
        )

    if purpose is QueryFieldPurpose.METRIC:
        if metric_operation is MetricOperation.COUNT_DISTINCT:
            if primary_entity is None:
                _raise_semantic_contract(
                    "the distinct metric has no server-derivable counted entity"
                )
            semantic_focus = _canonical_focus((primary_entity, "identifier"))
            qualified_owner = _first_qualified_owner(value)
            return _ServerSemanticFocus(
                semantic_focus=semantic_focus,
                owner_focus=(
                    (qualified_owner,)
                    if qualified_owner == primary_entity
                    else _server_owner_focus(semantic_focus)
                ),
                source_focus=(primary_entity,),
                operational_query=_description_query_from_focus(semantic_focus),
            )
        fragment = metric_fragments[ordinal - 1] if ordinal <= len(metric_fragments) else ""
        source_focus = _ordered_semantic_anchors(fragment)
        measure_focus = tuple(
            anchor for anchor in source_focus if anchor in _MEASURE_FOCUS_ANCHORS or anchor == "net"
        )
        if not measure_focus:
            _raise_semantic_contract(
                "the numeric metric has no closed server-derived measure focus"
            )
        semantic_focus = _canonical_focus(measure_focus)
        return _ServerSemanticFocus(
            semantic_focus=semantic_focus,
            owner_focus=_server_owner_focus(semantic_focus),
            source_focus=source_focus,
            operational_query=_description_query_from_focus(semantic_focus),
        )

    if purpose is QueryFieldPurpose.DIMENSION:
        fragment = dimension_fragments[ordinal - 1] if ordinal <= len(dimension_fragments) else ""
        qualified = _exact_qualified_field_focus(fragment)
        if qualified is not None:
            return _ServerSemanticFocus(
                semantic_focus=qualified.semantic_focus,
                owner_focus=qualified.owner_focus,
                source_focus=qualified.semantic_focus,
                operational_query=qualified.operational_query,
                qualified_reference=qualified.normalized_reference,
            )
        fragment_focus = _ordered_semantic_anchors(fragment)
        if not fragment_focus:
            _raise_semantic_contract("the dimension has no closed server-derived field focus")
        explicit_entities = _explicit_fragment_entities(fragment)
        attributes = tuple(
            anchor
            for anchor in fragment_focus
            if anchor not in _BUSINESS_ENTITY_ANCHORS
            and anchor not in _FILTER_VALUE_FOCUS_ANCHORS - {"delivery"}
        )
        if not attributes and not explicit_entities:
            _raise_semantic_contract("the dimension source segment has no field attribute")
        owner = (
            explicit_entities[0]
            if explicit_entities
            else (
                "shipment"
                if "delivery" in attributes
                else _inferred_dimension_owner(
                    value=value,
                    dimension_fragments=dimension_fragments,
                    ordinal=ordinal,
                    fallback=primary_entity,
                )
            )
        )
        semantic_values = attributes if owner is None else (owner, *attributes)
        semantic_focus = _canonical_focus(semantic_values)
        source_focus = _canonical_focus((*explicit_entities, *attributes))
        if not semantic_focus or not source_focus:
            _raise_semantic_contract("the dimension focus cannot be derived without invention")
        return _ServerSemanticFocus(
            semantic_focus=semantic_focus,
            owner_focus=_server_owner_focus(semantic_focus),
            source_focus=source_focus,
            operational_query=_description_query_from_focus(semantic_focus),
        )

    if purpose is QueryFieldPurpose.FILTER:
        return _derive_filter_semantic_focus(value, primary_entity=primary_entity)
    _raise_semantic_contract("the server-owned field slot has an unsupported focus")


def _derive_filter_semantic_focus(
    value: str,
    *,
    primary_entity: str | None,
) -> _ServerSemanticFocus:
    tokens = _source_tokens(value)
    label_indexes = tuple(
        index for index, token in enumerate(tokens) if token.normalized in _FILTER_LABELS
    )
    if not label_indexes:
        _raise_semantic_contract(
            "the filter has no closed server-derived attribute or relationship focus"
        )
    neighborhood = tuple(
        token.anchor
        for index in label_indexes
        for token in tokens[max(0, index - 4) : min(len(tokens), index + 5)]
        if token.anchor is not None
    )
    nearby = tuple(dict.fromkeys(neighborhood))
    values = tuple(anchor for anchor in nearby if anchor in _FILTER_VALUE_FOCUS_ANCHORS)
    if "holder" in nearby:
        relationship = ("account", "holder")
        grounded_relationship = tuple(anchor for anchor in relationship if anchor in nearby)
        semantic_focus = _canonical_focus((*relationship, "role", *values))
        source_focus = _canonical_focus((*grounded_relationship, *values))
        query_focus = _canonical_focus((*relationship, "role"))
    elif "delivery" in nearby:
        semantic_focus = _canonical_focus(("shipment", "status", *values))
        source_relationship = (
            ("delivery",)
            if "completed" in values
            else tuple(anchor for anchor in ("delivery", "order") if anchor in nearby)
        )
        source_focus = _canonical_focus((*source_relationship, *values))
        query_focus = _canonical_focus(("shipment", "status"))
    else:
        entity = next(
            (anchor for anchor in nearby if anchor in _BUSINESS_ENTITY_ANCHORS),
            primary_entity,
        )
        if entity is None:
            _raise_semantic_contract("the status filter has no server-derivable owning entity")
        semantic_focus = _canonical_focus((entity, "status", *values))
        source_focus = _canonical_focus((entity, *values))
        query_focus = _canonical_focus((entity, "status"))
    if not source_focus:
        _raise_semantic_contract("the filter focus is not grounded in the source segment")
    return _ServerSemanticFocus(
        semantic_focus=semantic_focus,
        owner_focus=_server_owner_focus(semantic_focus),
        source_focus=source_focus,
        operational_query=_description_query_from_focus(query_focus),
    )


def _metric_fragments(value: str) -> tuple[str, ...]:
    aggregation = _AGGREGATION_INTENT.search(value)
    connector = _DIMENSION_CONNECTOR.search(value)
    if aggregation is None or connector is None or connector.start() <= aggregation.end():
        return ()
    protected = _COMPOUND_TEMPORAL_FIELD.sub("fecha_hora", value)
    segment = protected[aggregation.end() : connector.start()]
    return tuple(
        fragment.strip(" ,.")
        for fragment in re.split(r"\b(?:and|y)\b", segment)
        if fragment.strip(" ,.")
    )


def _primary_business_entity(value: str) -> str | None:
    qualified_owner = _first_qualified_owner(value)
    if qualified_owner is not None:
        return qualified_owner
    return next(
        (
            anchor
            for anchor in _ordered_semantic_anchors(value)
            if anchor in _BUSINESS_ENTITY_ANCHORS
        ),
        None,
    )


def _first_qualified_owner(value: str) -> str | None:
    references = find_explicit_logical_field_references(value)
    return references[0].owner if references else None


def _inferred_dimension_owner(
    *,
    value: str,
    dimension_fragments: tuple[str, ...],
    ordinal: int,
    fallback: str | None,
) -> str | None:
    used_explicit_entities = {
        entity
        for index, fragment in enumerate(dimension_fragments, start=1)
        if index != ordinal
        for entity in _explicit_fragment_entities(fragment)
    }
    remaining = tuple(
        anchor
        for anchor in _ordered_semantic_anchors(value)
        if anchor in _BUSINESS_ENTITY_ANCHORS and anchor not in used_explicit_entities
    )
    return remaining[0] if remaining else fallback


def _ordered_semantic_anchors(value: str) -> tuple[str, ...]:
    normalized = _normalized_intent_text(value)
    return tuple(
        dict.fromkeys(
            _SEMANTIC_ANCHOR_ALIASES[token]
            for index, token in enumerate(re.findall(r"[a-z0-9]+", normalized))
            if token in _SEMANTIC_ANCHOR_ALIASES
            and not (index == 0 and token == "cuenta" and normalized.startswith("cuenta "))
        )
    )


def _explicit_fragment_entities(value: str) -> tuple[str, ...]:
    words = re.findall(r"[a-z0-9]+", _normalized_intent_text(value))
    entities: list[str] = []
    for index, word in enumerate(words):
        anchor = _SEMANTIC_ANCHOR_ALIASES.get(word)
        if anchor not in _BUSINESS_ENTITY_ANCHORS:
            continue
        if index == 0 or words[index - 1] in {"de", "del", "of"}:
            entities.append(anchor)
    return tuple(dict.fromkeys(entities))


def _canonical_focus(values: Iterable[str]) -> tuple[str, ...]:
    unique = set(values)
    return tuple(
        sorted(
            unique,
            key=lambda anchor: (
                _FOCUS_ANCHOR_ORDER.get(anchor, len(_FOCUS_ANCHOR_ORDER)),
                anchor,
            ),
        )
    )


def _exact_qualified_field_focus(value: str) -> _QualifiedFieldFocus | None:
    """Preserve one explicit inert model/field reference as lexical evidence.

    Qualification is authority only for the exact governed-model allowlist. The
    field leaf remains retrieval evidence: an unknown leaf therefore scores
    zero and becomes a typed ``no_match`` before interpretation.
    """

    normalized = _normalized_intent_text(value).strip(" ,.")
    reference = explicit_logical_field_reference(normalized)
    if reference is None:
        return None
    owner, field = reference.split(".", 1)
    owner_focus = (owner,)
    field_focus = tuple(
        anchor
        for token in re.findall(r"[a-z0-9]+", field.replace("_", " "))
        if (anchor := _SEMANTIC_ANCHOR_ALIASES.get(token)) is not None
        and anchor not in _BUSINESS_ENTITY_ANCHORS
    )
    semantic_focus = _canonical_focus((*owner_focus, *field_focus))
    return _QualifiedFieldFocus(
        semantic_focus=semantic_focus,
        owner_focus=owner_focus,
        operational_query=DescriptionQuery(reference),
        normalized_reference=reference,
    )


def _description_query_from_focus(values: tuple[str, ...]) -> DescriptionQuery:
    words = tuple("date" if value == "temporal" else value for value in values)
    if not words or len(words) > 8:
        _raise_semantic_contract("the server-derived operational query is not bounded")
    return DescriptionQuery(" ".join(words))


def _dimension_fragments(value: str) -> tuple[str, ...]:
    connector = _DIMENSION_CONNECTOR.search(value)
    if connector is None:
        return ()
    tail = value[connector.end() :]
    filter_marker = _CONDITIONAL_FILTER_INTENT.search(tail)
    if filter_marker is not None:
        tail = tail[: filter_marker.start()]
    protected = _COMPOUND_TEMPORAL_FIELD.sub("fecha_hora", tail)
    return tuple(
        fragment.strip(" ,.")
        for fragment in re.split(r"\b(?:and|y)\b", protected)
        if fragment.strip(" ,.")
    )


def _server_metric_operation(value: str) -> MetricOperation:
    tokens = set(re.findall(r"[a-z0-9]+", value))
    if tokens.intersection({"sum", "suma", "sumar", "totaliza", "totalizar"}):
        return MetricOperation.SUM
    if tokens.intersection({"average", "avg", "promedio", "media"}):
        return MetricOperation.AVG
    if tokens.intersection({"minimum", "minimo", "minima"}):
        return MetricOperation.MIN
    if tokens.intersection({"maximum", "maximo", "maxima"}):
        return MetricOperation.MAX
    # Entity counts are the only count form admitted by the bounded natural
    # language contract. COUNT DISTINCT is the safe fanout-preserving default.
    return MetricOperation.COUNT_DISTINCT


def _server_filter_operator(value: str) -> FilterOperator:
    if re.search(r"\b(?:is not null|not null|no nulo|no nula)\b", value):
        return FilterOperator.IS_NOT_NULL
    if re.search(r"\b(?:is null|null|nulo|nula)\b", value):
        return FilterOperator.IS_NULL
    if re.search(r"\b(?:greater than or equal|at least|mayor o igual|minimo)\b", value):
        return FilterOperator.GREATER_THAN_OR_EQUAL
    if re.search(r"\b(?:less than or equal|at most|menor o igual|maximo)\b", value):
        return FilterOperator.LESS_THAN_OR_EQUAL
    if re.search(r"\b(?:greater than|more than|mayor que|mas de)\b", value):
        return FilterOperator.GREATER_THAN
    if re.search(r"\b(?:less than|fewer than|menor que|menos de)\b", value):
        return FilterOperator.LESS_THAN
    if re.search(
        r"\b(?:not equal|different from|distinto de|excepto"
        r"|not\s+(?:active|inactive|enabled|disabled)"
        r"|no\s+(?:activo|activa|inactivo|inactiva|habilitado|deshabilitado))\b",
        value,
    ):
        return FilterOperator.NOT_EQUALS
    return FilterOperator.EQUALS


def _server_field_constraints(
    *,
    purpose: QueryFieldPurpose,
    fragment: str,
    metric_operation: MetricOperation | None,
) -> tuple[LogicalFieldRole, tuple[CanonicalType, ...]]:
    tokens = set(re.findall(r"[a-z0-9]+", fragment))
    temporal = bool(
        tokens.intersection(
            {
                "ano",
                "day",
                "date",
                "dia",
                "fecha",
                "hora",
                "mes",
                "month",
                "semana",
                "time",
                "timestamp",
                "week",
                "year",
            }
        )
    )
    if purpose is QueryFieldPurpose.METRIC:
        if metric_operation in {MetricOperation.SUM, MetricOperation.AVG}:
            return (
                LogicalFieldRole.MEASURE,
                (CanonicalType.INTEGER, CanonicalType.DECIMAL),
            )
        return LogicalFieldRole.IDENTIFIER, ()
    if purpose is QueryFieldPurpose.FILTER:
        return LogicalFieldRole.ATTRIBUTE, ()
    if temporal:
        return (
            LogicalFieldRole.TEMPORAL,
            (CanonicalType.DATE, CanonicalType.TIMESTAMP),
        )
    if tokens.intersection(
        {"id", "identifier", "identificador", "key", "clave", "referencia", "reference"}
    ):
        return LogicalFieldRole.IDENTIFIER, ()
    if tokens.intersection(
        {
            "amount",
            "balance",
            "cantidad",
            "importe",
            "monto",
            "price",
            "quantity",
            "saldo",
            "total",
            "units",
            "unidades",
        }
    ):
        return (
            LogicalFieldRole.MEASURE,
            (CanonicalType.INTEGER, CanonicalType.DECIMAL),
        )
    return LogicalFieldRole.ATTRIBUTE, ()


def _server_date_grain(value: str) -> DateGrain | None:
    tokens = set(re.findall(r"[a-z0-9]+", value))
    if tokens.intersection({"month", "months", "mes", "meses", "mensual"}):
        return DateGrain.MONTH
    if tokens.intersection({"year", "years", "ano", "anos", "anual"}):
        return DateGrain.YEAR
    if tokens.intersection({"week", "weeks", "semana", "semanas", "semanal"}):
        return DateGrain.WEEK
    if tokens.intersection(
        {"day", "days", "date", "dia", "dias", "fecha", "hora", "time", "timestamp"}
    ):
        return DateGrain.DAY
    return None


def _source_tokens(value: str) -> tuple[_SourceToken, ...]:
    tokens: list[_SourceToken] = []
    normalized_source = _normalized_intent_text(value)
    qualified_owner_spans = {
        reference.start: (
            reference.start + len(reference.owner),
            reference.owner,
        )
        for reference in find_explicit_logical_field_references(value)
    }
    for index, match in enumerate(_SOURCE_TOKEN.finditer(value)):
        qualified_owner = qualified_owner_spans.get(match.start())
        if qualified_owner is not None:
            owner_end, owner = qualified_owner
            tokens.append(
                _SourceToken(
                    start=match.start(),
                    end=owner_end,
                    normalized=owner,
                    anchor=owner,
                )
            )
            continue
        normalized = _normalized_intent_text(match.group())
        anchor = _SEMANTIC_ANCHOR_ALIASES.get(normalized)
        if index == 0 and normalized == "cuenta" and normalized_source.startswith("cuenta "):
            anchor = None
        tokens.append(
            _SourceToken(
                start=match.start(),
                end=match.end(),
                normalized=normalized,
                anchor=anchor,
            )
        )
    return tuple(tokens)


def _derive_source_span(
    value: DescriptionExpansionInput,
    *,
    spec: _ServerSlotSpec,
    query: DescriptionQuery,
) -> str:
    """Derive one exact source excerpt from server focus, never provider wording."""

    source = value.text.root
    if spec.qualified_reference is not None:
        if query != spec.operational_query:
            _raise_semantic_contract(
                "the explicit qualified field probe changed its exact lexical reference"
            )
        matches = tuple(
            source[reference.start : reference.end]
            for reference in find_explicit_logical_field_references(source)
            if reference.value == spec.qualified_reference
        )
        if len(matches) != 1:
            _raise_semantic_contract(
                "the explicit qualified field is not grounded exactly once in the source"
            )
        source_span = matches[0]
        if len(source_span) > 256 or len(source_span.encode("utf-8")) > 512:
            _raise_semantic_contract("the qualified field source span exceeds its bound")
        return source_span
    _require_provider_query_matches_focus(spec=spec, query=query)
    if _is_atomic_field_description(source):
        if len(source) > 256 or len(source.encode("utf-8")) > 512:
            _raise_semantic_contract("the atomic source text exceeds the grounded span limit")
        return source

    tokens = _source_tokens(source)
    source_focus = frozenset(spec.source_focus)
    if not tokens or not source_focus:
        _raise_semantic_contract("the server-owned slot has no grounded semantic focus")

    focus_entities = source_focus & _BUSINESS_ENTITY_ANCHORS
    ranked: list[tuple[tuple[int, int, int], str]] = []
    for start in range(len(tokens)):
        for end in range(start, min(len(tokens), start + 8)):
            span = source[tokens[start].start : tokens[end].end]
            if (
                len(span) > 256
                or len(span.encode("utf-8")) > 512
                or (
                    spec.intended_use is not QueryFieldPurpose.FILTER
                    and _NON_ATOMIC_PROBE.search(_normalized_intent_text(span)) is not None
                )
            ):
                continue
            anchors = frozenset(
                token.anchor for token in tokens[start : end + 1] if token.anchor is not None
            )
            if not source_focus.issubset(anchors):
                continue
            extraneous_entities = (anchors & _BUSINESS_ENTITY_ANCHORS) - focus_entities
            score = (
                -len(extraneous_entities),
                -(end - start + 1),
                -len(span),
            )
            ranked.append((score, span))

    if not ranked:
        _raise_semantic_contract(
            "the live AI query cannot be grounded to one atomic source excerpt"
        )
    best_score = max(score for score, _span in ranked)
    best_spans = tuple(dict.fromkeys(span for score, span in ranked if score == best_score))
    if len(best_spans) != 1:
        _raise_semantic_contract(
            "the live AI query maps to more than one equally supported source excerpt"
        )
    return best_spans[0]


def _require_provider_query_matches_focus(
    *,
    spec: _ServerSlotSpec,
    query: DescriptionQuery,
) -> None:
    """Treat provider wording as bounded evidence that cannot change retrieval."""

    query_anchors = _semantic_anchors(query.root)
    focus = frozenset(spec.semantic_focus)
    if not query_anchors or not query_anchors.intersection(focus):
        _raise_semantic_contract("the live AI query has no server-owned semantic focus anchor")
    query_entities = query_anchors & _BUSINESS_ENTITY_ANCHORS
    focus_entities = focus & _BUSINESS_ENTITY_ANCHORS
    if query_entities - focus_entities:
        _raise_semantic_contract("the live AI query switched to a conflicting business entity")
    unexpected_known_anchors = query_anchors - focus
    if unexpected_known_anchors:
        _raise_semantic_contract("the live AI query switched to a conflicting field concept")


def _reconstruct_expansion(
    value: DescriptionExpansionInput,
    provider: _OpenAIDescriptionExpansion,
) -> DescriptionExpansion:
    specs = _required_slot_specs(value.text.root)
    observed_ids = tuple(slot.slot_id for slot in provider.slots)
    required_ids = tuple(spec.slot_id for spec in specs)
    if (
        len(observed_ids) != len(set(observed_ids))
        or len(observed_ids) != len(required_ids)
        or set(observed_ids) != set(required_ids)
    ):
        raise QueryStudioPortError(
            QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
            "the live AI expansion did not cover the exact server-owned slots",
            retry_disposition=QueryStudioRetryDisposition.RETRY_ONCE,
            output_failure_category=ProviderOutputFailureCategory.SEMANTIC_CONTRACT,
        )
    provider_by_id = {slot.slot_id: slot for slot in provider.slots}
    probes: list[DescriptionSearchProbe] = []
    for spec in specs:
        provider_query = provider_by_id[spec.slot_id].query
        source_span = _derive_source_span(
            value,
            spec=spec,
            query=provider_query,
        )
        probes.append(
            DescriptionSearchProbe(
                purpose_id=spec.slot_id,
                query=spec.operational_query,
                source_span=source_span,
                intended_use=spec.intended_use,
                semantic_focus=spec.semantic_focus,
                owner_focus=spec.owner_focus,
                roles=spec.roles,
                canonical_types=spec.canonical_types,
                metric_operation=spec.metric_operation,
                filter_operator=spec.filter_operator,
                date_grain=spec.date_grain,
            )
        )
    return DescriptionExpansion(probes=tuple(probes))


def _require_server_owned_expansion(value: QueryStudioInterpretationInput) -> None:
    """Reject any expansion whose semantic controls were altered after expansion."""

    specs = _required_slot_specs(value.text.root)
    probes = value.expansion.probes
    if tuple(probe.purpose_id for probe in probes) != tuple(spec.slot_id for spec in specs):
        raise QueryStudioPortError(
            QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
            "the interpretation expansion no longer matches the server-owned slots",
            retry_disposition=QueryStudioRetryDisposition.NEVER,
            output_failure_category=ProviderOutputFailureCategory.SEMANTIC_CONTRACT,
        )
    for probe, spec in zip(probes, specs, strict=True):
        expected_span = _derive_source_span(
            DescriptionExpansionInput(text=value.text, language=value.language),
            spec=spec,
            query=spec.operational_query,
        )
        if (
            probe.intended_use is not spec.intended_use
            or probe.semantic_focus != spec.semantic_focus
            or probe.owner_focus != spec.owner_focus
            or probe.roles != spec.roles
            or probe.canonical_types != spec.canonical_types
            or probe.metric_operation is not spec.metric_operation
            or probe.filter_operator is not spec.filter_operator
            or probe.date_grain is not spec.date_grain
            or probe.query != spec.operational_query
            or probe.source_span != expected_span
        ):
            raise QueryStudioPortError(
                QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
                "the interpretation expansion changed server-owned focus or field semantics",
                retry_disposition=QueryStudioRetryDisposition.NEVER,
                output_failure_category=ProviderOutputFailureCategory.SEMANTIC_CONTRACT,
            )
        try:
            normalize_and_screen_user_text(probe.query.root)
        except OpenAIAdapterError:
            raise QueryStudioPortError(
                QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
                "the interpretation expansion contains unsafe search text",
                retry_disposition=QueryStudioRetryDisposition.NEVER,
                output_failure_category=ProviderOutputFailureCategory.SEMANTIC_CONTRACT,
            ) from None
    source = DescriptionExpansionInput(text=value.text, language=value.language)
    _require_grounded_expansion(source, value.expansion)
    _require_complete_expansion(source, value.expansion)


def local_analytical_description_expansion(
    value: DescriptionExpansionInput,
) -> DescriptionExpansion:
    """Build the complete analytical expansion locally with server-owned semantics."""

    if value.lane is not DescriptionExpansionRoute.ANALYTICAL:
        raise QueryStudioPortError(
            QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
            "local analytical expansion requires the analytical lane",
            retry_disposition=QueryStudioRetryDisposition.NEVER,
            output_failure_category=ProviderOutputFailureCategory.SEMANTIC_CONTRACT,
        )
    specs = _required_slot_specs(value.text.root)
    expansion = DescriptionExpansion(
        probes=tuple(
            DescriptionSearchProbe(
                purpose_id=spec.slot_id,
                query=spec.operational_query,
                source_span=_derive_source_span(
                    value,
                    spec=spec,
                    query=spec.operational_query,
                ),
                intended_use=spec.intended_use,
                semantic_focus=spec.semantic_focus,
                owner_focus=spec.owner_focus,
                roles=spec.roles,
                canonical_types=spec.canonical_types,
                metric_operation=spec.metric_operation,
                filter_operator=spec.filter_operator,
                date_grain=spec.date_grain,
            )
            for spec in specs
        )
    )
    _require_grounded_expansion(value, expansion)
    _require_complete_expansion(value, expansion)
    return expansion


def _candidate_is_compatible_with_slot(
    candidate: QueryStudioPromptCandidate,
    probe: DescriptionSearchProbe,
) -> bool:
    if (
        probe.purpose_id not in candidate.purpose_ids
        or probe.intended_use not in candidate.intended_uses
        or (probe.roles and candidate.role not in probe.roles)
        or (probe.canonical_types and candidate.canonical_type not in probe.canonical_types)
    ):
        return False
    if (
        probe.metric_operation is MetricOperation.COUNT_DISTINCT
        and candidate.role is not LogicalFieldRole.IDENTIFIER
    ):
        return False
    if probe.metric_operation in {MetricOperation.SUM, MetricOperation.AVG} and (
        candidate.role is not LogicalFieldRole.MEASURE
        or candidate.canonical_type
        not in {
            CanonicalType.INTEGER,
            CanonicalType.DECIMAL,
        }
    ):
        return False
    return not (
        probe.date_grain is not None
        and (
            candidate.role is not LogicalFieldRole.TEMPORAL
            or candidate.canonical_type not in {CanonicalType.DATE, CanonicalType.TIMESTAMP}
        )
    )


def _selection_options_by_slot(
    value: QueryStudioInterpretationInput,
) -> dict[str, tuple[QueryStudioPromptCandidate, ...]]:
    """Expose one strictly leading compatible option while retaining alternatives locally."""

    options: dict[str, tuple[QueryStudioPromptCandidate, ...]] = {}
    for probe in value.expansion.probes:
        compatible = tuple(
            (
                candidate,
                _candidate_score_for_slot(candidate, probe.purpose_id),
            )
            for candidate in value.vocabulary.candidates
            if _candidate_is_compatible_with_slot(candidate, probe)
        )
        if not compatible:
            _raise_semantic_contract(
                "the governed shortlist has no compatible option for a required slot"
            )
        ordered = tuple(
            sorted(
                compatible,
                key=lambda item: (
                    -item[1],
                    item[0].logical_field.root,
                    item[0].candidate_id.root,
                ),
            )
        )
        top_score = ordered[0][1]
        if len(ordered) > 1 and ordered[1][1] == top_score:
            _raise_semantic_contract(
                "the governed shortlist has no unique leading option for a required slot"
            )
        options[probe.purpose_id] = (ordered[0][0],)
    return options


def _candidate_score_for_slot(
    candidate: QueryStudioPromptCandidate,
    purpose_id: str,
) -> int:
    try:
        return candidate.score_for_purpose(purpose_id)
    except ValueError:
        _raise_semantic_contract("the governed shortlist has no exact score for a required slot")
    raise AssertionError("unreachable Query Studio slot-score boundary")


class OpenAIDescriptionExpansionAdapter:
    """Historical provider expansion retained only to verify old evidence.

    Production Query Studio composition uses the boundary-screened local
    expansion preflight and never instantiates this adapter.
    """

    def __init__(
        self,
        boundary: OpenAIResponsesBoundary,
        *,
        safety_identifier: str,
        matcher_version: str,
        semantic_scope_fingerprint: str,
        public_metadata_registry_fingerprint: str,
    ) -> None:
        validate_safety_identifier(safety_identifier)
        _require_application_owned_retry(boundary)
        self._boundary = boundary
        self._safety_identifier = safety_identifier
        self._configuration = _configuration_facts(
            boundary,
            matcher_version=matcher_version,
            semantic_scope_fingerprint=semantic_scope_fingerprint,
            public_metadata_registry_fingerprint=public_metadata_registry_fingerprint,
        )

    @property
    def configuration(self) -> ProviderConfigurationFacts:
        return self._configuration

    def expand(self, value: DescriptionExpansionInput) -> DescriptionExpansionResult:
        try:
            provider_input = _expansion_provider_input(value)
            parsed = self._boundary.parse(
                stage=OpenAIStage.EXPANSION,
                provider_input=provider_input,
                output_type=_OpenAIDescriptionExpansion,
                safety_identifier=self._safety_identifier,
            )
            provider_expansion = cast(_OpenAIDescriptionExpansion, parsed.value)
            expansion = _reconstruct_expansion(value, provider_expansion)
            for probe in expansion.probes:
                try:
                    normalize_and_screen_user_text(probe.query.root)
                except OpenAIAdapterError:
                    raise OpenAIAdapterError(
                        OpenAIAdapterErrorCode.INVALID_OUTPUT,
                        output_failure_category=(ProviderOutputFailureCategory.SEMANTIC_CONTRACT),
                    ) from None
            _require_grounded_expansion(value, expansion)
            _require_complete_expansion(value, expansion)
            expansion = _normalize_atomic_source_span(value, expansion)
            usage = _successful_usage(
                boundary=self._boundary,
                configuration=self._configuration,
                stage=ProviderStage.EXPANSION,
                parsed=parsed,
            )
            return DescriptionExpansionResult(expansion=expansion, usage=usage)
        except OpenAIAdapterError as error:
            raise _query_studio_error(error) from None
        except (ValidationError, TypeError, ValueError):
            raise QueryStudioPortError(
                QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
                "the live AI expansion output failed validation",
                retry_disposition=QueryStudioRetryDisposition.RETRY_ONCE,
                output_failure_category=(ProviderOutputFailureCategory.SCHEMA_VALIDATION),
            ) from None


def _require_grounded_expansion(
    value: DescriptionExpansionInput,
    expansion: DescriptionExpansion,
) -> None:
    source = _normalized_intent_text(value.text.root)
    specs_by_slot = {spec.slot_id: spec for spec in _required_slot_specs(value.text.root)}
    for probe in expansion.probes:
        span = None if probe.source_span is None else _normalized_intent_text(probe.source_span)
        if span is None or span not in source:
            raise QueryStudioPortError(
                QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
                "the live AI expansion output was not grounded in the source text",
                retry_disposition=QueryStudioRetryDisposition.RETRY_ONCE,
                output_failure_category=ProviderOutputFailureCategory.GROUNDING,
            )
        spec = specs_by_slot.get(probe.purpose_id)
        if spec is None:
            raise QueryStudioPortError(
                QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
                "the live AI expansion output introduced an unknown server-owned slot",
                retry_disposition=QueryStudioRetryDisposition.RETRY_ONCE,
                output_failure_category=ProviderOutputFailureCategory.SEMANTIC_CONTRACT,
            )
        expected_span = _derive_source_span(
            value,
            spec=spec,
            query=spec.operational_query,
        )
        if (
            probe.query != spec.operational_query
            or probe.source_span != expected_span
            or probe.semantic_focus != spec.semantic_focus
            or probe.owner_focus != spec.owner_focus
        ):
            raise QueryStudioPortError(
                QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
                "the live AI expansion output changed the server-owned field focus",
                retry_disposition=QueryStudioRetryDisposition.RETRY_ONCE,
                output_failure_category=ProviderOutputFailureCategory.SEMANTIC_CONTRACT,
            )


def _semantic_anchors(value: str) -> frozenset[str]:
    return frozenset(
        _SEMANTIC_ANCHOR_ALIASES[token]
        for token in re.findall(r"[a-z0-9]+", _normalized_intent_text(value))
        if token in _SEMANTIC_ANCHOR_ALIASES
    )


def _require_complete_expansion(
    value: DescriptionExpansionInput,
    expansion: DescriptionExpansion,
) -> None:
    """Reject a structurally valid response that omits an explicit analytic purpose."""

    if _is_atomic_field_description(value.text.root):
        if (
            len(expansion.probes) != 1
            or expansion.probes[0].intended_use is not QueryFieldPurpose.DIMENSION
            or expansion.probes[0].metric_operation is not None
            or expansion.probes[0].filter_operator is not None
            or expansion.probes[0].date_grain is not None
        ):
            raise QueryStudioPortError(
                QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
                "the live AI expansion split or embellished one atomic field",
                retry_disposition=QueryStudioRetryDisposition.RETRY_ONCE,
                output_failure_category=(ProviderOutputFailureCategory.SEMANTIC_CONTRACT),
            )
        source_anchors = _semantic_anchors(value.text.root)
        query_anchors = _semantic_anchors(expansion.probes[0].query.root)
        source_entities = source_anchors & _BUSINESS_ENTITY_ANCHORS
        source_attributes = source_anchors - _BUSINESS_ENTITY_ANCHORS
        if not source_entities.issubset(query_anchors) or (
            source_attributes and not source_attributes.intersection(query_anchors)
        ):
            raise QueryStudioPortError(
                QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
                "the live AI expansion dropped an atomic field qualifier",
                retry_disposition=QueryStudioRetryDisposition.RETRY_ONCE,
                output_failure_category=(ProviderOutputFailureCategory.SEMANTIC_CONTRACT),
            )

    required = Counter(_required_expansion_purposes(value.text.root))
    if any(
        len(_normalized_intent_text(probe.query.root).split()) > 8
        or _NON_ATOMIC_PROBE.search(_normalized_intent_text(probe.query.root)) is not None
        or (
            probe.intended_use is not QueryFieldPurpose.FILTER
            and _NON_ATOMIC_PROBE.search(_normalized_intent_text(probe.source_span or ""))
            is not None
        )
        for probe in expansion.probes
    ):
        raise QueryStudioPortError(
            QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
            "the live AI expansion output violated the atomic probe contract",
            retry_disposition=QueryStudioRetryDisposition.RETRY_ONCE,
            output_failure_category=(ProviderOutputFailureCategory.SEMANTIC_CONTRACT),
        )
    observed = Counter(probe.intended_use for probe in expansion.probes)
    if any(observed[purpose] < count for purpose, count in required.items()):
        raise QueryStudioPortError(
            QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
            "the live AI expansion output omitted a required analytic purpose",
            retry_disposition=QueryStudioRetryDisposition.RETRY_ONCE,
            output_failure_category=(ProviderOutputFailureCategory.SEMANTIC_CONTRACT),
        )


def _required_expansion_purposes(value: str) -> tuple[QueryFieldPurpose, ...]:
    """Derive the same bounded checklist sent to the provider and enforced locally."""

    analytic = _required_analytic_purposes(value)
    if analytic:
        return analytic
    if _is_atomic_field_description(value):
        return (QueryFieldPurpose.DIMENSION,)
    return ()


def _required_analytic_purposes(value: str) -> tuple[QueryFieldPurpose, ...]:
    """Derive only purposes supported by an explicit analytical operation."""

    normalized = _normalized_intent_text(value)
    grouping = _GROUPING_INTENT.search(normalized)
    aggregation = _AGGREGATION_INTENT.search(normalized)
    connector = _DIMENSION_CONNECTOR.search(normalized)
    required: Counter[QueryFieldPurpose] = Counter()
    if grouping is not None:
        required[QueryFieldPurpose.METRIC] = 1
        required[QueryFieldPurpose.DIMENSION] = _dimension_purpose_count(
            normalized,
            connector,
        )
    elif aggregation is not None:
        required[QueryFieldPurpose.METRIC] = _metric_purpose_count(
            normalized,
            aggregation,
            connector,
        )
        if connector is not None:
            required[QueryFieldPurpose.DIMENSION] = _dimension_purpose_count(
                normalized,
                connector,
            )
    if required and (
        _CONDITIONAL_FILTER_INTENT.search(normalized) is not None
        or _value_filter_precedes_dimension(normalized)
    ):
        required[QueryFieldPurpose.FILTER] = 1
    return tuple(
        purpose
        for purpose in (
            QueryFieldPurpose.METRIC,
            QueryFieldPurpose.DIMENSION,
            QueryFieldPurpose.FILTER,
        )
        for _ in range(required[purpose])
    )


def _metric_purpose_count(
    value: str,
    aggregation: re.Match[str],
    connector: re.Match[str] | None,
) -> int:
    if connector is None or connector.start() <= aggregation.end():
        return 1
    return _coordinated_field_count(value[aggregation.end() : connector.start()])


def _dimension_purpose_count(
    value: str,
    connector: re.Match[str] | None,
) -> int:
    if connector is None:
        return 1
    dimension_text = value[connector.end() :]
    filter_marker = _CONDITIONAL_FILTER_INTENT.search(dimension_text)
    if filter_marker is not None:
        dimension_text = dimension_text[: filter_marker.start()]
    return _coordinated_field_count(dimension_text)


def _coordinated_field_count(value: str) -> int:
    protected = _COMPOUND_TEMPORAL_FIELD.sub("temporal", value)
    return 1 + sum(1 for _match in _FIELD_LIST_COORDINATOR.finditer(protected))


def _value_filter_precedes_dimension(value: str) -> bool:
    connector = _DIMENSION_CONNECTOR.search(value)
    marker = _VALUE_FILTER_INTENT.search(value)
    return connector is not None and marker is not None and marker.start() < connector.start()


def _is_atomic_field_description(value: str) -> bool:
    normalized = _normalized_intent_text(value)
    coordinated_text = _COMPOUND_TEMPORAL_FIELD.sub("temporal", normalized)
    return (
        not _required_analytic_purposes(value)
        and _NON_ATOMIC_PROBE.search(normalized) is None
        and _FIELD_LIST_COORDINATOR.search(coordinated_text) is None
    )


def _normalize_atomic_source_span(
    value: DescriptionExpansionInput,
    expansion: DescriptionExpansion,
) -> DescriptionExpansion:
    """Preserve every qualifier for a validated, genuinely atomic one-field request."""

    source = value.text.root
    if (
        len(expansion.probes) != 1
        or len(source) > 256
        or len(source.encode("utf-8")) > 512
        or not _is_atomic_field_description(source)
    ):
        return expansion
    probe = expansion.probes[0]
    if probe.source_span == source:
        return expansion
    normalized_probe = DescriptionSearchProbe.model_validate(
        {
            **probe.model_dump(mode="python"),
            "source_span": source,
        }
    )
    return DescriptionExpansion(
        probes=(normalized_probe,),
        ambiguity_hints=expansion.ambiguity_hints,
    )


def _normalized_intent_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    plain = "".join(character for character in decomposed if not unicodedata.combining(character))
    return " ".join(plain.casefold().split())


def _reconstruct_proposal(
    provider: _OpenAIQueryStudioSelections,
    value: QueryStudioInterpretationInput,
) -> QueryStudioModelProposal:
    """Map local option indexes into the only executable domain proposal shape."""

    probes = {probe.purpose_id: probe for probe in value.expansion.probes}
    selections = provider.selections
    slot_ids = tuple(selection.slot_id for selection in selections)
    if len(slot_ids) != len(set(slot_ids)) or len(provider.ambiguity_kinds) != len(
        set(provider.ambiguity_kinds)
    ):
        _raise_semantic_contract("the live AI selections contained duplicate references")
    unknown_slots = set(slot_ids) - set(probes)
    if unknown_slots:
        _raise_semantic_contract("the live AI selections referenced an unknown field slot")
    options_by_slot = _selection_options_by_slot(value)
    selected_candidates: dict[str, QueryStudioPromptCandidate] = {}
    normalized_values: dict[str, FilterScalar] = {}
    for selection in selections:
        probe = probes[selection.slot_id]
        options = options_by_slot[selection.slot_id]
        try:
            candidate = options[selection.option_index - 1]
        except IndexError:
            _raise_semantic_contract(
                "the live AI selection referenced an option outside its exact slot"
            )
        _require_candidate_use(candidate.intended_uses, (probe.intended_use,))
        _require_candidate_matches_slot(candidate, probe)
        selected_candidates[selection.slot_id] = candidate
        normalized_values[selection.slot_id] = _derive_filter_value(
            probe=probe,
            candidate=candidate,
            source_text=value.text.root,
        )
    candidate_ids = tuple(candidate.candidate_id.root for candidate in selected_candidates.values())
    if len(candidate_ids) != len(set(candidate_ids)):
        _raise_semantic_contract("the live AI selections reused one candidate across slots")

    required_ids = set(probes)
    observed_ids = set(slot_ids)
    if not selections:
        if provider.ambiguity_kinds:
            return QueryStudioModelProposal(
                semantic_state=SemanticMatchState.AMBIGUOUS,
                ambiguities=provider.ambiguity_kinds,
            )
        return QueryStudioModelProposal(semantic_state=SemanticMatchState.NO_MATCH)
    if observed_ids != required_ids or provider.ambiguity_kinds:
        _raise_semantic_contract(
            "the live AI selections mixed ambiguity with data or omitted a required slot"
        )

    dimensions: list[ProposedDimension] = []
    metrics: list[ProposedMetric] = []
    filters: list[ProposedFilter] = []
    for probe in value.expansion.probes:
        candidate = selected_candidates[probe.purpose_id]
        if probe.intended_use is QueryFieldPurpose.DIMENSION:
            dimensions.append(
                ProposedDimension(
                    candidate_id=candidate.candidate_id,
                    grain=probe.date_grain,
                )
            )
        elif probe.intended_use is QueryFieldPurpose.METRIC:
            if probe.metric_operation is None:
                _raise_semantic_contract("the server-owned metric slot is incomplete")
            metrics.append(
                ProposedMetric(
                    candidate_id=candidate.candidate_id,
                    operation=probe.metric_operation,
                    alias=None,
                )
            )
        elif probe.intended_use is QueryFieldPurpose.FILTER:
            if probe.filter_operator is None:
                _raise_semantic_contract("the server-owned filter slot is incomplete")
            filters.append(
                ProposedFilter(
                    candidate_id=candidate.candidate_id,
                    operator=probe.filter_operator,
                    value=normalized_values[probe.purpose_id],
                )
            )
        else:
            _raise_semantic_contract("the server-owned field slot has an unsupported use")
    if not metrics:
        _raise_semantic_contract("the aligned live AI selection has no metric slot")
    return QueryStudioModelProposal(
        semantic_state=SemanticMatchState.ALIGNED,
        primary_candidate_id=metrics[0].candidate_id,
        dimensions=tuple(dimensions),
        metrics=tuple(metrics),
        filters=tuple(filters),
        order_by=(),
        limit=500,
    )


def _require_candidate_matches_slot(
    candidate: QueryStudioPromptCandidate,
    probe: DescriptionSearchProbe,
) -> None:
    if not _candidate_is_compatible_with_slot(candidate, probe):
        _raise_semantic_contract(
            "the live AI selection used a candidate incompatible with its exact field slot"
        )
    if probe.purpose_id not in candidate.purpose_ids:
        _raise_semantic_contract(
            "the live AI selection used a candidate outside its exact field slot"
        )
    if probe.roles and candidate.role not in probe.roles:
        _raise_semantic_contract(
            "the live AI selection used a candidate outside the server-owned field role"
        )
    if probe.canonical_types and candidate.canonical_type not in probe.canonical_types:
        _raise_semantic_contract(
            "the live AI selection used a candidate outside the server-owned field type"
        )
    operation = probe.metric_operation
    if (
        operation is MetricOperation.COUNT_DISTINCT
        and candidate.role is not LogicalFieldRole.IDENTIFIER
    ):
        _raise_semantic_contract(
            "the live AI selection used a non-identifier for a distinct entity metric"
        )
    if operation in {MetricOperation.SUM, MetricOperation.AVG} and (
        candidate.role is not LogicalFieldRole.MEASURE
        or candidate.canonical_type not in {CanonicalType.INTEGER, CanonicalType.DECIMAL}
    ):
        _raise_semantic_contract(
            "the live AI selection used a non-numeric field for a numeric metric"
        )
    if probe.date_grain is not None and (
        candidate.role is not LogicalFieldRole.TEMPORAL
        or candidate.canonical_type not in {CanonicalType.DATE, CanonicalType.TIMESTAMP}
    ):
        _raise_semantic_contract("the live AI selection used a non-temporal field for a date grain")


def _derive_filter_value(
    *,
    probe: DescriptionSearchProbe,
    candidate: QueryStudioPromptCandidate,
    source_text: str,
) -> FilterScalar:
    """Derive one typed explicit value locally; never accept a model-generated scalar."""

    operator = probe.filter_operator
    if probe.intended_use is not QueryFieldPurpose.FILTER:
        return None
    if operator in {FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL}:
        return None
    if operator is None:
        _raise_semantic_contract("the server-owned filter slot is incomplete")

    if candidate.canonical_type is CanonicalType.BOOLEAN:
        expected = _source_boolean_filter(source_text)
        if expected is None:
            _raise_semantic_contract(
                "the business request has no unique explicit boolean filter value"
            )
        return expected

    if candidate.canonical_type is CanonicalType.STRING:
        if candidate.allowed_values:
            allowed: dict[str, str] = {}
            for item in candidate.allowed_values:
                canonical = _canonical_filter_label(item)
                if canonical in allowed:
                    _raise_semantic_contract(
                        "the governed filter values are not canonically unique"
                    )
                allowed[canonical] = item
            grounded = _source_governed_filter_labels(
                source_text,
                candidate.allowed_values,
            )
            if len(grounded) != 1:
                _raise_semantic_contract("the business request has no unique governed filter value")
            selected = next(iter(grounded))
            if selected not in allowed:
                _raise_semantic_contract(
                    "the grounded filter value is outside the governed allowed set"
                )
            return allowed[selected]
        labels = _source_filter_labels(source_text)
        if len(labels) != 1:
            _raise_semantic_contract(
                "the business request has no unique server-derivable string filter value"
            )
        return next(iter(labels))

    if candidate.canonical_type in {CanonicalType.INTEGER, CanonicalType.DECIMAL}:
        without_iso = _ISO_DATE_OR_TIMESTAMP.sub(" ", source_text)
        literals = tuple(dict.fromkeys(_NUMERIC_LITERAL.findall(without_iso)))
        if len(literals) != 1:
            _raise_semantic_contract("the business request has no unique numeric filter value")
        try:
            parsed = Decimal(literals[0])
        except InvalidOperation:
            _raise_semantic_contract("the business request numeric filter is invalid")
        if candidate.canonical_type is CanonicalType.INTEGER:
            if parsed != parsed.to_integral_value():
                _raise_semantic_contract(
                    "the business request integer filter contains a decimal value"
                )
            return int(parsed)
        numeric = float(parsed)
        if not math.isfinite(numeric):
            _raise_semantic_contract("the business request decimal filter is not finite")
        return numeric

    iso_values = tuple(dict.fromkeys(_ISO_DATE_OR_TIMESTAMP.findall(source_text)))
    if len(iso_values) != 1:
        _raise_semantic_contract(
            "the business request has no unique ISO date or timestamp filter value"
        )
    raw_iso = iso_values[0]
    try:
        if candidate.canonical_type is CanonicalType.DATE:
            if "T" in raw_iso or " " in raw_iso:
                _raise_semantic_contract("the business request date filter contains a timestamp")
            return date.fromisoformat(raw_iso)
        if candidate.canonical_type is CanonicalType.TIMESTAMP:
            return datetime.fromisoformat(raw_iso.replace("Z", "+00:00"))
    except ValueError:
        _raise_semantic_contract("the business request date filter is invalid")
    _raise_semantic_contract("the governed filter type has no deterministic value derivation")


def _validated_filter_value(
    raw: FilterScalar,
    *,
    probe: DescriptionSearchProbe,
    candidate: QueryStudioPromptCandidate,
    source_text: str,
) -> FilterScalar:
    operator = probe.filter_operator
    if probe.intended_use is not QueryFieldPurpose.FILTER:
        if raw is not None:
            _raise_semantic_contract(
                "the live AI selection attached a filter value to a non-filter slot"
            )
        return None
    if operator in {FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL}:
        if raw is not None:
            _raise_semantic_contract("the live AI null filter carried a value")
        return None
    if raw is None:
        _raise_semantic_contract("the live AI comparison filter omitted its value")
    if isinstance(raw, float) and not math.isfinite(raw):
        _raise_semantic_contract("the live AI filter value was not finite")

    source_labels = _source_filter_labels(source_text)
    if candidate.canonical_type is CanonicalType.BOOLEAN:
        if not isinstance(raw, bool):
            _raise_semantic_contract("the live AI boolean filter used a non-boolean value")
        expected = _source_boolean_filter(source_text)
        if expected is None or raw is not expected:
            _raise_semantic_contract(
                "the live AI boolean filter value was not grounded in the request"
            )
        return raw
    if candidate.canonical_type is CanonicalType.STRING:
        if not isinstance(raw, str) or not raw.strip():
            _raise_semantic_contract("the live AI string filter used a non-string value")
        selected_label = _canonical_filter_label(raw)
        if candidate.allowed_values:
            allowed = {_canonical_filter_label(item): item for item in candidate.allowed_values}
            if selected_label not in allowed or len(allowed) != len(candidate.allowed_values):
                _raise_semantic_contract(
                    "the live AI filter value was outside the governed allowed set"
                )
            governed_source_labels = _source_governed_filter_labels(
                source_text,
                candidate.allowed_values,
            )
            if len(governed_source_labels) != 1:
                _raise_semantic_contract(
                    "the live AI filter value was not one unambiguous governed request value"
                )
            if selected_label != next(iter(governed_source_labels)):
                _raise_semantic_contract(
                    "the live AI filter value changed the value in the request"
                )
            return allowed[selected_label]
        if source_labels:
            if len(source_labels) != 1 or selected_label != next(iter(source_labels)):
                _raise_semantic_contract(
                    "the live AI filter value changed the value in the request"
                )
            return selected_label
        if not _value_is_grounded(raw, source_text):
            _raise_semantic_contract("the live AI filter value was not grounded in the request")
        return raw
    if candidate.canonical_type is CanonicalType.INTEGER:
        if not isinstance(raw, int) or isinstance(raw, bool):
            _raise_semantic_contract("the live AI integer filter used a non-integer value")
    elif candidate.canonical_type is CanonicalType.DECIMAL:
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            _raise_semantic_contract("the live AI decimal filter used a non-numeric value")
    elif candidate.canonical_type is CanonicalType.DATE:
        if type(raw) is not date:
            _raise_semantic_contract("the live AI date filter used a non-date value")
    elif candidate.canonical_type is CanonicalType.TIMESTAMP and not isinstance(raw, datetime):
        _raise_semantic_contract("the live AI timestamp filter used a non-timestamp value")
    if not _typed_value_is_grounded(raw, candidate.canonical_type, source_text):
        _raise_semantic_contract("the live AI filter value was not grounded in the request")
    return raw


_FILTER_LABELS = {
    "active": "ACTIVE",
    "activa": "ACTIVE",
    "activas": "ACTIVE",
    "activo": "ACTIVE",
    "activos": "ACTIVE",
    "cancelada": "CANCELLED",
    "canceladas": "CANCELLED",
    "cancelado": "CANCELLED",
    "cancelados": "CANCELLED",
    "cancelled": "CANCELLED",
    "canceled": "CANCELLED",
    "completed": "COMPLETED",
    "completada": "COMPLETED",
    "completadas": "COMPLETED",
    "completado": "COMPLETED",
    "completados": "COMPLETED",
    "delivered": "DELIVERED",
    "entregada": "DELIVERED",
    "entregadas": "DELIVERED",
    "entregado": "DELIVERED",
    "entregados": "DELIVERED",
    "inactive": "INACTIVE",
    "inactiva": "INACTIVE",
    "inactivas": "INACTIVE",
    "inactivo": "INACTIVE",
    "inactivos": "INACTIVE",
    "primary": "PRIMARY",
    "principal": "PRIMARY",
    "principales": "PRIMARY",
    "second": "SECONDARY",
    "secondary": "SECONDARY",
    "segunda": "SECONDARY",
    "segundas": "SECONDARY",
    "segundo": "SECONDARY",
    "segundos": "SECONDARY",
}


def _canonical_filter_label(value: str) -> str:
    normalized = _normalized_intent_text(value)
    tokens = re.findall(r"[a-z0-9]+", normalized)
    aliases = {_FILTER_LABELS[token] for token in tokens if token in _FILTER_LABELS}
    if len(aliases) == 1:
        return next(iter(aliases))
    return normalized.upper().replace(" ", "_")


def _source_filter_labels(value: str) -> frozenset[str]:
    tokens = re.findall(r"[a-z0-9]+", _normalized_intent_text(value))
    return frozenset(_FILTER_LABELS[token] for token in tokens if token in _FILTER_LABELS)


def _source_governed_filter_labels(
    source_text: str,
    allowed_values: tuple[str, ...],
) -> frozenset[str]:
    """Return every governed label explicitly present in the business request."""

    known_aliases = _source_filter_labels(source_text)
    matches: set[str] = set()
    for allowed_value in allowed_values:
        canonical = _canonical_filter_label(allowed_value)
        if canonical in known_aliases or _value_is_grounded(allowed_value, source_text):
            matches.add(canonical)
    return frozenset(matches)


def _source_boolean_filter(value: str) -> bool | None:
    tokens = set(re.findall(r"[a-z0-9]+", _normalized_intent_text(value)))
    truthy = bool(
        tokens.intersection(
            {"active", "activa", "activas", "activo", "activos", "enabled", "habilitado"}
        )
    )
    falsey = bool(
        tokens.intersection(
            {
                "disabled",
                "inactive",
                "inactiva",
                "inactivas",
                "inactivo",
                "inactivos",
                "deshabilitado",
            }
        )
    )
    if truthy == falsey:
        return None
    return truthy


def _value_is_grounded(value: str, source_text: str) -> bool:
    value_tokens = tuple(re.findall(r"[a-z0-9]+", _normalized_intent_text(value)))
    source_tokens = tuple(re.findall(r"[a-z0-9]+", _normalized_intent_text(source_text)))
    if not value_tokens or len(value_tokens) > len(source_tokens):
        return False
    width = len(value_tokens)
    return any(
        source_tokens[index : index + width] == value_tokens
        for index in range(len(source_tokens) - width + 1)
    )


_NUMERIC_LITERAL = re.compile(r"(?<![A-Za-z0-9_.])[-+]?(?:\d+(?:\.\d+)?|\.\d+)(?![A-Za-z0-9_.])")
_ISO_DATE_OR_TIMESTAMP = re.compile(
    r"(?<![0-9A-Za-z])"
    r"\d{4}-\d{2}-\d{2}"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)?"
    r"(?![0-9A-Za-z])"
)


def _typed_value_is_grounded(
    value: FilterScalar,
    canonical_type: CanonicalType,
    source_text: str,
) -> bool:
    """Ground typed scalars without erasing signs, decimal points, or ISO separators."""

    if canonical_type in {CanonicalType.INTEGER, CanonicalType.DECIMAL}:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        try:
            expected = Decimal(str(value))
        except InvalidOperation:
            return False
        for literal in _NUMERIC_LITERAL.findall(source_text):
            try:
                if Decimal(literal) == expected:
                    return True
            except InvalidOperation:
                continue
        return False
    if canonical_type is CanonicalType.DATE and type(value) is date:
        return _exact_iso_value_is_grounded(value.isoformat(), source_text)
    if canonical_type is CanonicalType.TIMESTAMP and isinstance(value, datetime):
        return _exact_iso_value_is_grounded(value.isoformat(), source_text)
    return isinstance(value, str) and _value_is_grounded(value, source_text)


def _exact_iso_value_is_grounded(value: str, source_text: str) -> bool:
    return (
        re.search(
            rf"(?<![0-9A-Za-z]){re.escape(value)}(?![0-9A-Za-z])",
            source_text,
        )
        is not None
    )


def _raise_semantic_contract(message: str) -> NoReturn:
    raise QueryStudioPortError(
        QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
        message,
        retry_disposition=QueryStudioRetryDisposition.RETRY_ONCE,
        output_failure_category=ProviderOutputFailureCategory.SEMANTIC_CONTRACT,
    )


class OpenAIQueryStudioIntentAdapter:
    """Interpret only the bounded governed shortlist and opaque candidate IDs."""

    def __init__(
        self,
        boundary: OpenAIResponsesBoundary,
        *,
        safety_identifier: str,
        matcher_version: str,
        semantic_scope_fingerprint: str,
        public_metadata_registry_fingerprint: str,
    ) -> None:
        validate_safety_identifier(safety_identifier)
        _require_application_owned_retry(boundary)
        self._boundary = boundary
        self._safety_identifier = safety_identifier
        self._configuration = _configuration_facts(
            boundary,
            matcher_version=matcher_version,
            semantic_scope_fingerprint=semantic_scope_fingerprint,
            public_metadata_registry_fingerprint=public_metadata_registry_fingerprint,
        )

    @property
    def configuration(self) -> ProviderConfigurationFacts:
        return self._configuration

    def interpret(
        self,
        value: QueryStudioInterpretationInput,
    ) -> QueryStudioInterpretationResult:
        try:
            _require_server_owned_expansion(value)
            provider_input = _interpretation_provider_input(
                value,
                configuration=self._configuration,
            )
            parsed = self._boundary.parse(
                stage=OpenAIStage.INTERPRETATION,
                provider_input=provider_input,
                output_type=_OpenAIQueryStudioSelections,
                safety_identifier=self._safety_identifier,
            )
            provider_selections = cast(_OpenAIQueryStudioSelections, parsed.value)
            unnormalized_proposal = _reconstruct_proposal(
                provider_selections,
                value,
            )
            _validate_proposal_is_closed(unnormalized_proposal, value)
            proposal = canonicalize_query_studio_proposal(
                unnormalized_proposal,
                value,
            )
            _validate_proposal_is_closed(proposal, value)
            usage = _successful_usage(
                boundary=self._boundary,
                configuration=self._configuration,
                stage=ProviderStage.INTERPRETATION,
                parsed=parsed,
            )
            return QueryStudioInterpretationResult(proposal=proposal, usage=usage)
        except OpenAIAdapterError as error:
            raise _query_studio_error(error) from None
        except QueryStudioPortError:
            raise
        except (ValidationError, TypeError, ValueError):
            raise QueryStudioPortError(
                QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
                "the live AI interpretation output failed validation",
                retry_disposition=QueryStudioRetryDisposition.RETRY_ONCE,
                output_failure_category=(ProviderOutputFailureCategory.SCHEMA_VALIDATION),
            ) from None


def create_openai_query_studio_adapters_from_environment(
    config: OpenAIResponsesConfig,
    *,
    safety_identifier: str,
    matcher_version: str,
    semantic_scope_fingerprint: str,
    public_metadata_registry_fingerprint: str,
) -> tuple[OpenAIDescriptionExpansionAdapter, OpenAIQueryStudioIntentAdapter]:
    """Historical two-stage factory retained only for evidence compatibility.

    The composition root intentionally uses
    ``create_openai_query_studio_intent_adapter_from_environment`` instead.
    """

    try:
        client = create_managed_openai_client_from_environment(config)
        boundary = OpenAIResponsesBoundary(client, config)
        return (
            OpenAIDescriptionExpansionAdapter(
                boundary,
                safety_identifier=safety_identifier,
                matcher_version=matcher_version,
                semantic_scope_fingerprint=semantic_scope_fingerprint,
                public_metadata_registry_fingerprint=public_metadata_registry_fingerprint,
            ),
            OpenAIQueryStudioIntentAdapter(
                boundary,
                safety_identifier=safety_identifier,
                matcher_version=matcher_version,
                semantic_scope_fingerprint=semantic_scope_fingerprint,
                public_metadata_registry_fingerprint=public_metadata_registry_fingerprint,
            ),
        )
    except OpenAIAdapterError as error:
        raise _query_studio_error(error) from None


def create_openai_query_studio_intent_adapter_from_environment(
    config: OpenAIResponsesConfig,
    *,
    safety_identifier: str,
    matcher_version: str,
    semantic_scope_fingerprint: str,
    public_metadata_registry_fingerprint: str,
) -> OpenAIQueryStudioIntentAdapter:
    """Create only the structured interpretation adapter used by production."""

    try:
        client = create_managed_openai_client_from_environment(config)
        boundary = OpenAIResponsesBoundary(client, config)
        return OpenAIQueryStudioIntentAdapter(
            boundary,
            safety_identifier=safety_identifier,
            matcher_version=matcher_version,
            semantic_scope_fingerprint=semantic_scope_fingerprint,
            public_metadata_registry_fingerprint=public_metadata_registry_fingerprint,
        )
    except OpenAIAdapterError as error:
        raise _query_studio_error(error) from None


def _configuration_facts(
    boundary: OpenAIResponsesBoundary,
    *,
    matcher_version: str,
    semantic_scope_fingerprint: str,
    public_metadata_registry_fingerprint: str,
) -> ProviderConfigurationFacts:
    config = boundary.config
    exact_behavior_fingerprint = openai_provider_contract_fingerprint(config)
    try:
        return ProviderConfigurationFacts.create(
            adapter=_ADAPTER_LABEL,
            model_snapshot=config.model.value,
            reasoning_effort=config.reasoning_effort.value,
            endpoint_region=config.region.value,
            prompt_version=config.prompt_version,
            schema_version=config.schema_version,
            matcher_version=matcher_version,
            orchestration_policy_version=QUERY_STUDIO_ORCHESTRATION_POLICY_VERSION,
            attempt_policy_version=EXTERNAL_AI_ATTEMPT_POLICY_VERSION,
            managed_config_fingerprint=config.fingerprint(stage=OpenAIStage.INTERPRETATION),
            provider_contract_fingerprint=exact_behavior_fingerprint,
            public_metadata_policy_fingerprint=openai_public_metadata_policy_fingerprint(
                config,
                semantic_scope_fingerprint=semantic_scope_fingerprint,
                registry_fingerprint=public_metadata_registry_fingerprint,
            ),
            public_metadata_semantic_scope_fingerprint=semantic_scope_fingerprint,
            public_metadata_registry_fingerprint=public_metadata_registry_fingerprint,
            external_ai=True,
        )
    except (ValidationError, TypeError, ValueError):
        raise OpenAIAdapterError(OpenAIAdapterErrorCode.CONFIGURATION_INVALID) from None


def _require_application_owned_retry(boundary: OpenAIResponsesBoundary) -> None:
    if boundary.config.max_transient_retries != 0:
        raise OpenAIAdapterError(OpenAIAdapterErrorCode.CONFIGURATION_INVALID)


def _successful_usage(
    *,
    boundary: OpenAIResponsesBoundary,
    configuration: ProviderConfigurationFacts,
    stage: ProviderStage,
    parsed: OpenAIParsedOutput,
) -> ProviderUsageFacts:
    if parsed.usage.input_tokens is None or parsed.usage.output_tokens is None:
        raise OpenAIAdapterError(
            OpenAIAdapterErrorCode.INVALID_OUTPUT,
            output_failure_category=(ProviderOutputFailureCategory.SCHEMA_VALIDATION),
        )
    if parsed.model_snapshot != boundary.config.model.value:
        raise OpenAIAdapterError(OpenAIAdapterErrorCode.MODEL_MISMATCH)
    return ProviderUsageFacts(
        stage=stage,
        model_snapshot=parsed.model_snapshot,
        configuration_fingerprint=configuration.fingerprint,
        input_tokens=parsed.usage.input_tokens,
        output_tokens=parsed.usage.output_tokens,
        duration_ms=parsed.duration_ms,
        outcome=ProviderOutcomeCode.SUCCEEDED,
    )


def _validate_proposal_is_closed(
    proposal: QueryStudioModelProposal,
    value: QueryStudioInterpretationInput,
) -> None:
    if proposal.semantic_state is SemanticMatchState.ALIGNED:
        required = value.vocabulary.required_selection_counts
        observed = (
            len(proposal.dimensions),
            len(proposal.metrics),
            len(proposal.filters),
        )
        if observed != (required.dimensions, required.metrics, required.filters):
            raise QueryStudioPortError(
                QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
                "the live AI proposal did not cover the exact required selections",
                retry_disposition=QueryStudioRetryDisposition.RETRY_ONCE,
                output_failure_category=(ProviderOutputFailureCategory.SEMANTIC_CONTRACT),
            )
    allowed_candidate_ids = {
        candidate.candidate_id.root for candidate in value.vocabulary.candidates
    }
    referenced_candidate_ids = {
        candidate_id.root for candidate_id in proposal.referenced_candidate_ids
    }
    if not referenced_candidate_ids.issubset(allowed_candidate_ids):
        raise QueryStudioPortError(
            QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
            "the live AI proposal referenced a candidate outside the supplied shortlist",
            retry_disposition=QueryStudioRetryDisposition.RETRY_ONCE,
            output_failure_category=(ProviderOutputFailureCategory.SEMANTIC_CONTRACT),
        )
    candidates = {
        candidate.candidate_id.root: candidate for candidate in value.vocabulary.candidates
    }
    for dimension in proposal.dimensions:
        _require_candidate_use(
            candidates[dimension.candidate_id.root].intended_uses,
            (QueryFieldPurpose.DIMENSION,),
        )
    for metric in proposal.metrics:
        _require_candidate_use(
            candidates[metric.candidate_id.root].intended_uses,
            (QueryFieldPurpose.METRIC,),
        )
    if proposal.primary_candidate_id is not None:
        primary_model = _candidate_logical_model(candidates[proposal.primary_candidate_id.root])
        for metric in proposal.metrics:
            metric_candidate = candidates[metric.candidate_id.root]
            if (
                metric.operation is MetricOperation.COUNT_DISTINCT
                and metric_candidate.role is LogicalFieldRole.IDENTIFIER
                and _is_opposite_join_endpoint(
                    primary_model=primary_model,
                    metric_candidate=metric_candidate,
                    value=value,
                )
            ):
                raise QueryStudioPortError(
                    QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
                    (
                        "the live AI proposal counted a homologous join endpoint "
                        "instead of the primary model"
                    ),
                    retry_disposition=QueryStudioRetryDisposition.RETRY_ONCE,
                    output_failure_category=(ProviderOutputFailureCategory.SEMANTIC_CONTRACT),
                )
    for request_filter in proposal.filters:
        _require_candidate_use(
            candidates[request_filter.candidate_id.root].intended_uses,
            (QueryFieldPurpose.FILTER,),
        )
    for ordering in proposal.order_by:
        _require_candidate_use(
            candidates[ordering.candidate_id.root].intended_uses,
            (QueryFieldPurpose.ORDER, QueryFieldPurpose.DIMENSION),
        )
    _require_operations_are_supplied(
        (metric.operation for metric in proposal.metrics),
        value.vocabulary.metric_operations,
    )
    _require_operations_are_supplied(
        (item.operator for item in proposal.filters),
        value.vocabulary.filter_operators,
    )
    _require_operations_are_supplied(
        (item.grain for item in proposal.dimensions if item.grain is not None),
        value.vocabulary.date_grains,
    )
    _require_operations_are_supplied(
        (item.direction for item in proposal.order_by),
        value.vocabulary.sort_directions,
    )


def _candidate_logical_model(candidate: QueryStudioPromptCandidate) -> str:
    return candidate.logical_field.root.split(".", 1)[0]


def _is_opposite_join_endpoint(
    *,
    primary_model: str,
    metric_candidate: QueryStudioPromptCandidate,
    value: QueryStudioInterpretationInput,
) -> bool:
    metric_model = _candidate_logical_model(metric_candidate)
    for join in value.vocabulary.joins:
        if (
            join.left_model.root == primary_model
            and join.right_model.root == metric_model
            and join.right_field == metric_candidate.logical_field
        ):
            return True
        if (
            join.right_model.root == primary_model
            and join.left_model.root == metric_model
            and join.left_field == metric_candidate.logical_field
        ):
            return True
    return False


def _require_candidate_use(
    observed: tuple[QueryFieldPurpose, ...],
    allowed: tuple[QueryFieldPurpose, ...],
) -> None:
    if not set(observed).intersection(allowed):
        raise QueryStudioPortError(
            QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
            "the live AI proposal used a candidate outside its retrieval purpose",
            retry_disposition=QueryStudioRetryDisposition.RETRY_ONCE,
            output_failure_category=(ProviderOutputFailureCategory.SEMANTIC_CONTRACT),
        )


def _require_operations_are_supplied(
    selected: Iterable[object],
    allowed: Sequence[object],
) -> None:
    values = tuple(selected)
    if any(value not in allowed for value in values):
        raise QueryStudioPortError(
            QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
            "the live AI proposal used an operation outside the supplied vocabulary",
            retry_disposition=QueryStudioRetryDisposition.RETRY_ONCE,
            output_failure_category=(ProviderOutputFailureCategory.SEMANTIC_CONTRACT),
        )


def _query_studio_error(error: OpenAIAdapterError) -> QueryStudioPortError:
    code = _PORT_ERROR_CODES.get(
        error.code,
        QueryStudioPortErrorCode.PROVIDER_UNAVAILABLE,
    )
    return QueryStudioPortError(
        code,
        _PORT_ERROR_MESSAGES[code],
        retry_disposition=(
            QueryStudioRetryDisposition.RETRY_ONCE
            if error.code is OpenAIAdapterErrorCode.INVALID_OUTPUT
            else QueryStudioRetryDisposition.NEVER
        ),
        output_failure_category=error.output_failure_category,
    )


_PORT_ERROR_CODES = {
    OpenAIAdapterErrorCode.CONFIGURATION_INVALID: (QueryStudioPortErrorCode.PROVIDER_UNAVAILABLE),
    OpenAIAdapterErrorCode.INPUT_TOO_LARGE: QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
    OpenAIAdapterErrorCode.PROMPT_TOO_LARGE: QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
    OpenAIAdapterErrorCode.SENSITIVE_INPUT_BLOCKED: (
        QueryStudioPortErrorCode.SENSITIVE_INPUT_BLOCKED
    ),
    OpenAIAdapterErrorCode.METADATA_NOT_PUBLIC: (QueryStudioPortErrorCode.SENSITIVE_INPUT_BLOCKED),
    OpenAIAdapterErrorCode.SENSITIVE_METADATA_BLOCKED: (
        QueryStudioPortErrorCode.SENSITIVE_INPUT_BLOCKED
    ),
    OpenAIAdapterErrorCode.PROVIDER_TIMEOUT: QueryStudioPortErrorCode.PROVIDER_TIMEOUT,
    OpenAIAdapterErrorCode.PROVIDER_RATE_LIMITED: (QueryStudioPortErrorCode.PROVIDER_RATE_LIMITED),
    OpenAIAdapterErrorCode.PROVIDER_UNAVAILABLE: (QueryStudioPortErrorCode.PROVIDER_UNAVAILABLE),
    OpenAIAdapterErrorCode.PROVIDER_REJECTED: (QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT),
    OpenAIAdapterErrorCode.MODEL_REFUSED: QueryStudioPortErrorCode.PROVIDER_REFUSED,
    OpenAIAdapterErrorCode.MISSING_OUTPUT: QueryStudioPortErrorCode.PROVIDER_MISSING_OUTPUT,
    OpenAIAdapterErrorCode.INVALID_OUTPUT: (QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT),
    OpenAIAdapterErrorCode.MODEL_MISMATCH: (QueryStudioPortErrorCode.PROVIDER_MODEL_MISMATCH),
}
_PORT_ERROR_MESSAGES = {
    QueryStudioPortErrorCode.PROVIDER_UNAVAILABLE: ("the live AI provider is unavailable"),
    QueryStudioPortErrorCode.PROVIDER_RATE_LIMITED: ("the live AI provider rate limit was reached"),
    QueryStudioPortErrorCode.PROVIDER_TIMEOUT: "the live AI provider timed out",
    QueryStudioPortErrorCode.PROVIDER_REFUSED: ("the live AI provider refused the bounded request"),
    QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT: (
        "the live AI request or structured output failed validation"
    ),
    QueryStudioPortErrorCode.PROVIDER_MISSING_OUTPUT: (
        "the live AI provider returned no structured output"
    ),
    QueryStudioPortErrorCode.PROVIDER_MODEL_MISMATCH: (
        "the live AI provider returned an unexpected model snapshot"
    ),
    QueryStudioPortErrorCode.SENSITIVE_INPUT_BLOCKED: ("live AI was blocked by the privacy screen"),
}
