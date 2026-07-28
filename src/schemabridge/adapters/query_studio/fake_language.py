"""Deterministic, key-free Query Studio language adapter for synthetic evaluation."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from schemabridge.application.ports.query_studio import (
    QueryStudioPortError,
    QueryStudioPortErrorCode,
)
from schemabridge.domain.concepts import CanonicalType
from schemabridge.domain.query_studio import (
    LOCAL_AI_ATTEMPT_POLICY_VERSION,
    QUERY_STUDIO_ORCHESTRATION_POLICY_VERSION,
    DescriptionExpansion,
    DescriptionExpansionInput,
    DescriptionExpansionResult,
    DescriptionExpansionRoute,
    DescriptionQuery,
    DescriptionSearchProbe,
    OpaqueCandidateId,
    ProposalAmbiguityKind,
    ProposedDimension,
    ProposedFilter,
    ProposedMetric,
    ProposedOrder,
    ProviderConfigurationFacts,
    ProviderOutcomeCode,
    ProviderStage,
    ProviderUsageFacts,
    QueryFieldPurpose,
    QueryStudioInterpretationInput,
    QueryStudioInterpretationResult,
    QueryStudioModelProposal,
    SemanticMatchState,
    atomic_description_expansion,
    classify_description_expansion_route,
)
from schemabridge.domain.query_studio_matching import (
    GOVERNED_DESCRIPTION_MATCHER_VERSION,
)
from schemabridge.domain.query_studio_proposals import (
    canonicalize_query_studio_proposal,
)
from schemabridge.domain.request_context import LogicalFieldRole
from schemabridge.domain.requests import (
    DateGrain,
    FilterOperator,
    MetricOperation,
    SortDirection,
)

_MODEL_SNAPSHOT = "query-studio-fake-v1"
_MATCHER_VERSION = GOVERNED_DESCRIPTION_MATCHER_VERSION


class _SyntheticQueryCase(StrEnum):
    SECONDARY_HOLDERS = "secondary_holders"
    ACTIVE_CUSTOMERS = "active_customers"
    ACTIVE_PRODUCTS = "active_products"
    DELIVERED_ORDERS = "delivered_orders"
    NET_REVENUE = "net_revenue"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


def fake_query_studio_configuration() -> ProviderConfigurationFacts:
    """Return the exact configuration fingerprint shared by both local stages."""

    return ProviderConfigurationFacts.create(
        adapter="deterministic_fake",
        model_snapshot=_MODEL_SNAPSHOT,
        reasoning_effort="none",
        endpoint_region="local",
        prompt_version="m27-fake-v3",
        schema_version="m27-v1",
        matcher_version=_MATCHER_VERSION,
        orchestration_policy_version=QUERY_STUDIO_ORCHESTRATION_POLICY_VERSION,
        attempt_policy_version=LOCAL_AI_ATTEMPT_POLICY_VERSION,
        external_ai=False,
    )


@dataclass(frozen=True, slots=True)
class DeterministicDescriptionExpansion:
    """Expand only the bounded public synthetic query cases, with no provider I/O."""

    configuration: ProviderConfigurationFacts = field(
        default_factory=fake_query_studio_configuration
    )

    def expand(self, value: DescriptionExpansionInput) -> DescriptionExpansionResult:
        _reject_dangerous_text(value.text.root)
        if classify_description_expansion_route(value) is DescriptionExpansionRoute.FIELD_MATCH:
            return DescriptionExpansionResult(
                expansion=atomic_description_expansion(value),
                usage=None,
            )
        case = _classify(value.text.root)
        expansion = _expansion(case, value.text)
        return DescriptionExpansionResult(
            expansion=expansion,
            usage=_usage(self.configuration, ProviderStage.EXPANSION),
        )


@dataclass(frozen=True, slots=True)
class DeterministicQueryStudioIntent:
    """Resolve synthetic cases only through logical fields in the supplied vocabulary."""

    configuration: ProviderConfigurationFacts = field(
        default_factory=fake_query_studio_configuration
    )

    def interpret(
        self,
        value: QueryStudioInterpretationInput,
    ) -> QueryStudioInterpretationResult:
        _reject_dangerous_text(value.text.root)
        case = _classify(value.text.root)
        candidates = {
            item.logical_field.root: item.candidate_id for item in value.vocabulary.candidates
        }
        proposal = canonicalize_query_studio_proposal(
            _proposal(case, candidates),
            value,
        )
        return QueryStudioInterpretationResult(
            proposal=proposal,
            usage=_usage(self.configuration, ProviderStage.INTERPRETATION),
        )


def _expansion(
    case: _SyntheticQueryCase,
    original: DescriptionQuery,
) -> DescriptionExpansion:
    if case is _SyntheticQueryCase.SECONDARY_HOLDERS:
        return DescriptionExpansion(
            probes=(
                _dimension(
                    "registration_date",
                    "fecha de registro del cliente",
                    role=LogicalFieldRole.TEMPORAL,
                    grain=DateGrain.DAY,
                ),
                _metric(
                    "customer_metric",
                    "identificador estable del cliente",
                    role=LogicalFieldRole.IDENTIFIER,
                    operation=MetricOperation.COUNT_DISTINCT,
                ),
                _filter(
                    "holder_role",
                    "tipo o posición del titular de la cuenta",
                    role=LogicalFieldRole.ATTRIBUTE,
                ),
            )
        )
    if case is _SyntheticQueryCase.ACTIVE_CUSTOMERS:
        return DescriptionExpansion(
            probes=(
                _dimension(
                    "customer_country",
                    "country code associated with the customer",
                    role=LogicalFieldRole.ATTRIBUTE,
                ),
                _metric(
                    "customer_metric",
                    "stable customer identifier",
                    role=LogicalFieldRole.IDENTIFIER,
                    operation=MetricOperation.COUNT_DISTINCT,
                ),
                _filter(
                    "customer_status",
                    "current customer lifecycle status",
                    role=LogicalFieldRole.ATTRIBUTE,
                ),
            )
        )
    if case is _SyntheticQueryCase.ACTIVE_PRODUCTS:
        return DescriptionExpansion(
            probes=(
                _dimension(
                    "product_category",
                    "categoría de negocio del producto",
                    role=LogicalFieldRole.ATTRIBUTE,
                ),
                _metric(
                    "product_metric",
                    "identificador estable del producto",
                    role=LogicalFieldRole.IDENTIFIER,
                    operation=MetricOperation.COUNT_DISTINCT,
                ),
                _filter(
                    "product_active",
                    "producto activo booleano",
                    role=LogicalFieldRole.ATTRIBUTE,
                    canonical_type=CanonicalType.BOOLEAN,
                ),
            )
        )
    if case is _SyntheticQueryCase.DELIVERED_ORDERS:
        return DescriptionExpansion(
            probes=(
                _dimension(
                    "delivery_date",
                    "actual delivery date and time",
                    role=LogicalFieldRole.TEMPORAL,
                    grain=DateGrain.DAY,
                ),
                _metric(
                    "order_metric",
                    "stable sales order identifier",
                    role=LogicalFieldRole.IDENTIFIER,
                    operation=MetricOperation.COUNT_DISTINCT,
                ),
                _filter(
                    "shipment_status",
                    "normalized shipment delivery status",
                    role=LogicalFieldRole.ATTRIBUTE,
                ),
            )
        )
    if case is _SyntheticQueryCase.NET_REVENUE:
        return DescriptionExpansion(
            probes=(
                _dimension(
                    "order_date",
                    "fecha del pedido comercial",
                    role=LogicalFieldRole.TEMPORAL,
                    grain=DateGrain.DAY,
                ),
                _dimension(
                    "product_category",
                    "categoría de negocio del producto",
                    role=LogicalFieldRole.ATTRIBUTE,
                ),
                _metric(
                    "net_revenue",
                    "importe neto de la línea después del descuento",
                    role=LogicalFieldRole.MEASURE,
                    operation=MetricOperation.SUM,
                ),
            )
        )
    hint = (ProposalAmbiguityKind.FIELD_MEANING,) if case is _SyntheticQueryCase.AMBIGUOUS else ()
    return DescriptionExpansion(
        probes=(
            DescriptionSearchProbe(
                purpose_id="requested_field",
                query=original,
                intended_use=QueryFieldPurpose.DIMENSION,
            ),
        ),
        ambiguity_hints=hint,
    )


def _dimension(
    purpose_id: str,
    query: str,
    *,
    role: LogicalFieldRole,
    grain: DateGrain | None = None,
) -> DescriptionSearchProbe:
    return DescriptionSearchProbe(
        purpose_id=purpose_id,
        query=DescriptionQuery(query),
        intended_use=QueryFieldPurpose.DIMENSION,
        roles=(role,),
        date_grain=grain,
    )


def _metric(
    purpose_id: str,
    query: str,
    *,
    role: LogicalFieldRole,
    operation: MetricOperation,
) -> DescriptionSearchProbe:
    return DescriptionSearchProbe(
        purpose_id=purpose_id,
        query=DescriptionQuery(query),
        intended_use=QueryFieldPurpose.METRIC,
        roles=(role,),
        metric_operation=operation,
    )


def _filter(
    purpose_id: str,
    query: str,
    *,
    role: LogicalFieldRole,
    canonical_type: CanonicalType | None = None,
) -> DescriptionSearchProbe:
    return DescriptionSearchProbe(
        purpose_id=purpose_id,
        query=DescriptionQuery(query),
        intended_use=QueryFieldPurpose.FILTER,
        canonical_types=() if canonical_type is None else (canonical_type,),
        roles=(role,),
        filter_operator=FilterOperator.EQUALS,
    )


def _proposal(
    case: _SyntheticQueryCase,
    candidates: Mapping[str, OpaqueCandidateId],
) -> QueryStudioModelProposal:
    if case is _SyntheticQueryCase.AMBIGUOUS:
        return QueryStudioModelProposal(
            semantic_state=SemanticMatchState.AMBIGUOUS,
            ambiguities=(ProposalAmbiguityKind.FIELD_MEANING,),
        )
    if case is _SyntheticQueryCase.UNKNOWN:
        return QueryStudioModelProposal(semantic_state=SemanticMatchState.NO_MATCH)

    required = _REQUIRED_FIELDS[case]
    if any(field not in candidates for field in required):
        return QueryStudioModelProposal(semantic_state=SemanticMatchState.NO_MATCH)

    candidate = candidates.__getitem__
    if case is _SyntheticQueryCase.SECONDARY_HOLDERS:
        return QueryStudioModelProposal(
            semantic_state=SemanticMatchState.ALIGNED,
            primary_candidate_id=candidate("Customer.customer_key"),
            dimensions=(
                ProposedDimension(
                    candidate_id=candidate("Customer.registration_date"),
                    grain=DateGrain.DAY,
                ),
            ),
            metrics=(
                ProposedMetric(
                    candidate_id=candidate("Customer.customer_key"),
                    operation=MetricOperation.COUNT_DISTINCT,
                    alias="secondary_holder_customers",
                ),
            ),
            filters=(
                ProposedFilter(
                    candidate_id=candidate("AccountHolder.holder_role"),
                    operator=FilterOperator.EQUALS,
                    value="SECONDARY",
                ),
            ),
            order_by=(
                ProposedOrder(
                    candidate_id=candidate("Customer.registration_date"),
                    direction=SortDirection.ASC,
                ),
            ),
            limit=500,
        )
    if case is _SyntheticQueryCase.ACTIVE_CUSTOMERS:
        return QueryStudioModelProposal(
            semantic_state=SemanticMatchState.ALIGNED,
            primary_candidate_id=candidate("Customer.customer_key"),
            dimensions=(ProposedDimension(candidate_id=candidate("Customer.country_code")),),
            metrics=(
                ProposedMetric(
                    candidate_id=candidate("Customer.customer_key"),
                    operation=MetricOperation.COUNT_DISTINCT,
                    alias="active_customers",
                ),
            ),
            filters=(
                ProposedFilter(
                    candidate_id=candidate("Customer.customer_status"),
                    operator=FilterOperator.EQUALS,
                    value="ACTIVE",
                ),
            ),
            order_by=(ProposedOrder(candidate_id=candidate("Customer.country_code")),),
            limit=500,
        )
    if case is _SyntheticQueryCase.ACTIVE_PRODUCTS:
        return QueryStudioModelProposal(
            semantic_state=SemanticMatchState.ALIGNED,
            primary_candidate_id=candidate("Product.product_key"),
            dimensions=(ProposedDimension(candidate_id=candidate("Product.category")),),
            metrics=(
                ProposedMetric(
                    candidate_id=candidate("Product.product_key"),
                    operation=MetricOperation.COUNT_DISTINCT,
                    alias="active_products",
                ),
            ),
            filters=(
                ProposedFilter(
                    candidate_id=candidate("Product.is_active"),
                    operator=FilterOperator.EQUALS,
                    value=True,
                ),
            ),
            order_by=(ProposedOrder(candidate_id=candidate("Product.category")),),
            limit=100,
        )
    if case is _SyntheticQueryCase.DELIVERED_ORDERS:
        return QueryStudioModelProposal(
            semantic_state=SemanticMatchState.ALIGNED,
            primary_candidate_id=candidate("SalesOrder.order_key"),
            dimensions=(
                ProposedDimension(
                    candidate_id=candidate("Shipment.delivered_at"),
                    grain=DateGrain.DAY,
                ),
            ),
            metrics=(
                ProposedMetric(
                    candidate_id=candidate("SalesOrder.order_key"),
                    operation=MetricOperation.COUNT_DISTINCT,
                    alias="delivered_orders",
                ),
            ),
            filters=(
                ProposedFilter(
                    candidate_id=candidate("Shipment.shipment_status"),
                    operator=FilterOperator.EQUALS,
                    value="DELIVERED",
                ),
            ),
            order_by=(ProposedOrder(candidate_id=candidate("Shipment.delivered_at")),),
            limit=100,
        )
    return QueryStudioModelProposal(
        semantic_state=SemanticMatchState.ALIGNED,
        primary_candidate_id=candidate("SaleLine.net_amount"),
        dimensions=(
            ProposedDimension(
                candidate_id=candidate("SalesOrder.ordered_at"),
                grain=DateGrain.DAY,
            ),
            ProposedDimension(candidate_id=candidate("Product.category")),
        ),
        metrics=(
            ProposedMetric(
                candidate_id=candidate("SaleLine.net_amount"),
                operation=MetricOperation.SUM,
                alias="net_revenue",
            ),
        ),
        order_by=(
            ProposedOrder(candidate_id=candidate("SalesOrder.ordered_at")),
            ProposedOrder(candidate_id=candidate("Product.category")),
        ),
        limit=100,
    )


def _classify(value: str) -> _SyntheticQueryCase:
    normalized = _normalized(value)
    tokens = set(normalized.split())
    if ({"segundo", "titular"} <= tokens or {"secondary", "holder"} <= tokens) and (
        {"cliente", "clientes", "customer", "customers"} & tokens
    ):
        return _SyntheticQueryCase.SECONDARY_HOLDERS
    if (
        ({"cliente", "clientes", "customer", "customers"} & tokens)
        and ({"activo", "activos", "active"} & tokens)
        and ({"pais", "country"} & tokens)
    ):
        return _SyntheticQueryCase.ACTIVE_CUSTOMERS
    if (
        ({"producto", "productos", "product", "products"} & tokens)
        and ({"activo", "activos", "active"} & tokens)
        and ({"categoria", "category"} & tokens)
    ):
        return _SyntheticQueryCase.ACTIVE_PRODUCTS
    if ({"pedido", "pedidos", "order", "orders"} & tokens) and (
        {"entregado", "entregados", "delivered", "delivery"} & tokens
    ):
        return _SyntheticQueryCase.DELIVERED_ORDERS
    if (
        ({"importe", "ingreso", "ingresos", "revenue", "amount"} & tokens)
        and ({"neto", "net"} & tokens)
        and ({"pedido", "order"} & tokens)
        and ({"categoria", "category"} & tokens)
    ):
        return _SyntheticQueryCase.NET_REVENUE
    if len(tokens) <= 5 and tokens & {
        "cuenta",
        "date",
        "fecha",
        "id",
        "identificador",
        "reference",
        "referencia",
        "status",
        "estado",
    }:
        return _SyntheticQueryCase.AMBIGUOUS
    return _SyntheticQueryCase.UNKNOWN


def _reject_dangerous_text(value: str) -> None:
    normalized = _normalized(value)
    if (
        any(
            character in value
            for character in (
                "\u202a",
                "\u202b",
                "\u202d",
                "\u202e",
                "\u2066",
                "\u2067",
                "\u2068",
                "\u2069",
            )
        )
        or value.lstrip().startswith(("=", "+", "-", "@"))
        or _DANGEROUS.search(normalized)
        or _DSN.search(value)
        or _SECRET_LIKE.search(value)
    ):
        raise QueryStudioPortError(
            QueryStudioPortErrorCode.SENSITIVE_INPUT_BLOCKED,
            "the synthetic request was blocked before language interpretation",
        )


def _normalized(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    plain = "".join(character for character in decomposed if not unicodedata.combining(character))
    return " ".join(re.findall(r"[a-z0-9]+", plain.casefold()))


def _usage(
    configuration: ProviderConfigurationFacts,
    stage: ProviderStage,
) -> ProviderUsageFacts:
    return ProviderUsageFacts(
        stage=stage,
        model_snapshot=configuration.model_snapshot,
        configuration_fingerprint=configuration.fingerprint,
        input_tokens=0,
        output_tokens=0,
        duration_ms=0,
        outcome=ProviderOutcomeCode.SUCCEEDED,
    )


_REQUIRED_FIELDS = {
    _SyntheticQueryCase.SECONDARY_HOLDERS: frozenset(
        {
            "Customer.customer_key",
            "Customer.registration_date",
            "AccountHolder.holder_role",
        }
    ),
    _SyntheticQueryCase.ACTIVE_CUSTOMERS: frozenset(
        {
            "Customer.customer_key",
            "Customer.country_code",
            "Customer.customer_status",
        }
    ),
    _SyntheticQueryCase.ACTIVE_PRODUCTS: frozenset(
        {"Product.product_key", "Product.category", "Product.is_active"}
    ),
    _SyntheticQueryCase.DELIVERED_ORDERS: frozenset(
        {
            "SalesOrder.order_key",
            "Shipment.delivered_at",
            "Shipment.shipment_status",
        }
    ),
    _SyntheticQueryCase.NET_REVENUE: frozenset(
        {
            "SalesOrder.ordered_at",
            "Product.category",
            "SaleLine.net_amount",
        }
    ),
}

_DANGEROUS = re.compile(
    r"\b(?:alter|approve|apprueba|create|delete|drop|grant|ignore|ignora|insert|"
    r"inventa|invent|private key|revoke|run it|select|truncate|update)\b|--|/\*|\*/|;"
)
_DSN = re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mssql|oracle)://\S+")
_SECRET_LIKE = re.compile(r"\b[A-Za-z0-9_-]{36,}\b")


__all__ = [
    "DeterministicDescriptionExpansion",
    "DeterministicQueryStudioIntent",
    "fake_query_studio_configuration",
]
