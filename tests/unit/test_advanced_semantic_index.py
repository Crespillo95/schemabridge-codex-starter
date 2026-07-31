from __future__ import annotations

from pathlib import Path

import pytest
from tests.m32_natural_support import (
    M32_REFERENCE_LOGICAL_FIELDS,
    M32_REFERENCE_QUESTION,
)

from schemabridge.adapters.query_studio.advanced_fake_language import (
    DeterministicAdvancedLanguageAdapter,
)
from schemabridge.adapters.query_studio.advanced_semantic_index import (
    RegistryWideAdvancedSemanticIndex,
)
from schemabridge.application.ports.advanced_query_studio import (
    AdvancedQueryStudioPortError,
    AdvancedQueryStudioPortErrorCode,
)
from schemabridge.bootstrap import build_semantic_registry
from schemabridge.domain.advanced_query_studio import (
    AdvancedMentionExtraction,
    AdvancedMentionExtractionInput,
    AdvancedMentionPurpose,
    AdvancedNaturalLanguageInput,
    AdvancedQueryMention,
    AdvancedSourceSpan,
)
from schemabridge.domain.concepts import (
    CanonicalType,
    LogicalFieldRef,
    LogicalModelRef,
)
from schemabridge.domain.decisions import ApprovalStatus
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.mappings import ColumnMapping, ConfidenceScore
from schemabridge.domain.request_context import (
    ApprovedLogicalContext,
    ApprovedRequestField,
    ApprovedRequestModel,
    LogicalFieldRole,
)
from schemabridge.domain.semantic_registry import (
    GovernedFieldMapping,
    GovernedMappingRegistry,
    GovernedSemanticRegistrySnapshot,
    PhysicalValueType,
    RegistryArtifactKind,
)
from schemabridge.domain.transformations import IdentityStep, TransformationPlan

ROOT = Path(__file__).resolve().parents[2]


def _query(text: str) -> AdvancedNaturalLanguageInput:
    return AdvancedNaturalLanguageInput(
        text=text,
        language=UserLanguage.SPANISH,
    )


def _single_mention(
    query: AdvancedNaturalLanguageInput,
    phrase: str,
    purpose: AdvancedMentionPurpose,
) -> AdvancedMentionExtraction:
    start = query.text.index(phrase)
    return AdvancedMentionExtraction(
        request_digest=query.digest,
        mentions=(
            AdvancedQueryMention(
                value=phrase,
                source_span=AdvancedSourceSpan(
                    start=start,
                    end=start + len(phrase),
                ),
                purpose=purpose,
            ),
        ),
    )


def _mentions(
    query: AdvancedNaturalLanguageInput,
    specifications: tuple[tuple[str, AdvancedMentionPurpose], ...],
) -> AdvancedMentionExtraction:
    mentions = []
    for phrase, purpose in specifications:
        start = query.text.index(phrase)
        mentions.append(
            AdvancedQueryMention(
                value=phrase,
                source_span=AdvancedSourceSpan(
                    start=start,
                    end=start + len(phrase),
                ),
                purpose=purpose,
            )
        )
    return AdvancedMentionExtraction(
        request_digest=query.digest,
        mentions=tuple(
            sorted(
                mentions,
                key=lambda item: (
                    item.source_span.start,
                    item.source_span.end,
                    item.purpose.value,
                ),
            )
        ),
    )


def test_reference_retrieval_searches_registry_and_returns_bounded_approved_hits() -> None:
    registry = build_semantic_registry(repository_root=ROOT).load().registry
    query = _query(M32_REFERENCE_QUESTION)
    language = DeterministicAdvancedLanguageAdapter()
    extraction = language.extract(AdvancedMentionExtractionInput(query=query)).extraction

    result = RegistryWideAdvancedSemanticIndex().retrieve(
        query=query,
        extraction=extraction,
        registry=registry,
    )

    returned = {item.root for item in result.logical_fields}
    assert result.registry_fingerprint == registry.fingerprint
    assert returned >= M32_REFERENCE_LOGICAL_FIELDS
    assert 1 <= len(returned) <= 12
    assert returned <= set(registry.logical_context.field_index())


def test_retrieval_rejects_an_extraction_bound_to_another_request() -> None:
    registry = build_semantic_registry(repository_root=ROOT).load().registry
    original = _query("Calcula ingresos netos.")
    extraction = _single_mention(
        original,
        "ingresos netos",
        AdvancedMentionPurpose.METRIC,
    )

    with pytest.raises(AdvancedQueryStudioPortError) as captured:
        RegistryWideAdvancedSemanticIndex().retrieve(
            query=_query("Calcula unidades."),
            extraction=extraction,
            registry=registry,
        )

    assert captured.value.code is AdvancedQueryStudioPortErrorCode.RETRIEVAL_INVALID
    assert original.text not in str(captured.value)


def test_retrieval_considers_the_last_field_of_a_thousand_field_registry() -> None:
    registry = _wide_registry()
    target = LogicalFieldRef("WideEntity.terminal_ultraviolet_metric")
    assert len(registry.logical_context.field_index()) == 1_000
    assert tuple(registry.logical_context.field_index())[-1] == target.root
    query = _query("Calcula la señal ultravioleta terminal.")
    extraction = _single_mention(
        query,
        "ultravioleta terminal",
        AdvancedMentionPurpose.METRIC,
    )

    result = RegistryWideAdvancedSemanticIndex().retrieve(
        query=query,
        extraction=extraction,
        registry=registry,
    )

    assert result.logical_fields == (target,)
    assert len(result.logical_fields) <= 12


@pytest.mark.parametrize(
    ("text", "phrase", "purpose", "expected"),
    (
        (
            "Ordena por categoría.",
            "categoría",
            AdvancedMentionPurpose.ORDERING,
            LogicalFieldRef("Product.category"),
        ),
        (
            "Calcula una ventana mensual.",
            "mensual",
            AdvancedMentionPurpose.WINDOW,
            LogicalFieldRef("SalesOrder.ordered_at"),
        ),
    ),
)
def test_retrieval_includes_fields_used_only_by_ordering_or_windows(
    text: str,
    phrase: str,
    purpose: AdvancedMentionPurpose,
    expected: LogicalFieldRef,
) -> None:
    registry = build_semantic_registry(repository_root=ROOT).load().registry
    query = _query(text)

    result = RegistryWideAdvancedSemanticIndex().retrieve(
        query=query,
        extraction=_single_mention(query, phrase, purpose),
        registry=registry,
    )

    assert expected in result.logical_fields


@pytest.mark.parametrize(
    ("text", "specifications", "expected_fields"),
    (
        (
            "Cuenta clientes por país.",
            (
                ("clientes", AdvancedMentionPurpose.METRIC),
                ("país", AdvancedMentionPurpose.DIMENSION),
            ),
            frozenset({"Customer.customer_key", "Customer.country_code"}),
        ),
        (
            "Suma el saldo de las cuentas.",
            (
                ("saldo", AdvancedMentionPurpose.METRIC),
                ("cuentas", AdvancedMentionPurpose.PRIMARY_ENTITY),
            ),
            frozenset({"Account.current_balance"}),
        ),
        (
            "Calcula la facturación por región.",
            (
                ("facturación", AdvancedMentionPurpose.METRIC),
                ("región", AdvancedMentionPurpose.DIMENSION),
            ),
            frozenset({"SalesOrder.order_total", "SalesOrder.region"}),
        ),
        (
            "Muestra productos activos por categoría.",
            (
                ("productos", AdvancedMentionPurpose.PRIMARY_ENTITY),
                ("activos", AdvancedMentionPurpose.FILTER),
                ("categoría", AdvancedMentionPurpose.DIMENSION),
            ),
            frozenset({"Product.is_active", "Product.category"}),
        ),
        (
            "Cuenta pedidos completados por canal.",
            (
                ("pedidos", AdvancedMentionPurpose.METRIC),
                ("completados", AdvancedMentionPurpose.FILTER),
                ("canal", AdvancedMentionPurpose.DIMENSION),
            ),
            frozenset(
                {
                    "SalesOrder.order_key",
                    "SalesOrder.order_status",
                    "SalesOrder.sales_channel",
                }
            ),
        ),
        (
            "Lista envíos entregados por fecha de entrega.",
            (
                ("envíos", AdvancedMentionPurpose.PRIMARY_ENTITY),
                ("entregados", AdvancedMentionPurpose.FILTER),
                ("fecha de entrega", AdvancedMentionPurpose.DIMENSION),
            ),
            frozenset({"Shipment.shipment_status", "Shipment.delivered_at"}),
        ),
        (
            "Suma unidades vendidas por categoría.",
            (
                ("unidades vendidas", AdvancedMentionPurpose.METRIC),
                ("categoría", AdvancedMentionPurpose.DIMENSION),
            ),
            frozenset({"SaleLine.quantity", "Product.category"}),
        ),
        (
            "Suma descuentos por mes de pedido.",
            (
                ("descuentos", AdvancedMentionPurpose.METRIC),
                ("mes de pedido", AdvancedMentionPurpose.GROUPING),
            ),
            frozenset({"SaleLine.discount_amount", "SalesOrder.ordered_at"}),
        ),
        (
            "Calcula la facturación neta por mes de pedido y categoría.",
            (
                ("facturación neta", AdvancedMentionPurpose.METRIC),
                ("mes de pedido", AdvancedMentionPurpose.GROUPING),
                ("categoría", AdvancedMentionPurpose.DIMENSION),
            ),
            frozenset(
                {
                    "SaleLine.net_amount",
                    "SalesOrder.ordered_at",
                    "Product.category",
                }
            ),
        ),
        (
            "Muestra el precio medio de productos por categoría.",
            (
                ("precio", AdvancedMentionPurpose.METRIC),
                ("productos", AdvancedMentionPurpose.PRIMARY_ENTITY),
                ("categoría", AdvancedMentionPurpose.DIMENSION),
            ),
            frozenset({"Product.unit_price", "Product.category"}),
        ),
        (
            "Ranking de facturación por región.",
            (
                ("Ranking", AdvancedMentionPurpose.WINDOW),
                ("facturación", AdvancedMentionPurpose.METRIC),
                ("región", AdvancedMentionPurpose.DIMENSION),
            ),
            frozenset({"SalesOrder.order_total", "SalesOrder.region"}),
        ),
        (
            "Ingreso neto acumulado por categoría de producto.",
            (
                ("Ingreso neto acumulado", AdvancedMentionPurpose.WINDOW),
                ("categoría de producto", AdvancedMentionPurpose.DIMENSION),
            ),
            frozenset({"SaleLine.net_amount", "Product.category"}),
        ),
        (
            "Porcentaje de unidades vendidas por categoría.",
            (
                ("unidades vendidas", AdvancedMentionPurpose.WINDOW),
                ("categoría", AdvancedMentionPurpose.DIMENSION),
            ),
            frozenset({"SaleLine.quantity", "Product.category"}),
        ),
        (
            "Ranking de pedidos por importe bruto del pedido.",
            (
                ("pedidos", AdvancedMentionPurpose.PRIMARY_ENTITY),
                ("importe bruto del pedido", AdvancedMentionPurpose.METRIC),
            ),
            frozenset({"SalesOrder.order_key", "SalesOrder.order_total"}),
        ),
    ),
    ids=(
        "customers-by-country",
        "account-balance",
        "billing-by-region",
        "active-products",
        "completed-orders-by-channel",
        "delivered-shipments",
        "units-by-category",
        "discounts-by-order-month",
        "net-billing-by-month-and-category",
        "average-product-price",
        "rank-billing-by-region",
        "running-net-revenue",
        "unit-share-by-category",
        "rank-orders-by-gross-amount",
    ),
)
def test_spanish_simple_and_advanced_retrieval_corpus_is_governed_and_unambiguous(
    text: str,
    specifications: tuple[tuple[str, AdvancedMentionPurpose], ...],
    expected_fields: frozenset[str],
) -> None:
    registry = build_semantic_registry(repository_root=ROOT).load().registry
    query = _query(text)

    result = RegistryWideAdvancedSemanticIndex().retrieve(
        query=query,
        extraction=_mentions(query, specifications),
        registry=registry,
    )

    returned = {item.root for item in result.logical_fields}
    assert expected_fields <= returned
    assert returned <= set(registry.logical_context.field_index())
    assert len(returned) <= 12


def test_retrieval_rejects_equal_disconnected_model_branches_as_ambiguous() -> None:
    original = build_semantic_registry(repository_root=ROOT).load().registry
    tied_models = tuple(
        ApprovedRequestModel(
            id=LogicalModelRef(model_id),
            description="Entidad quasar sintética.",
            fields=(
                ApprovedRequestField(
                    id=LogicalFieldRef(f"{model_id}.quasar_signal"),
                    canonical_type=CanonicalType.DECIMAL,
                    role=LogicalFieldRole.MEASURE,
                    definition="Señal quasar sintética.",
                    status=ApprovalStatus.APPROVED,
                    version=1,
                ),
            ),
            status=ApprovalStatus.APPROVED,
            version=1,
        )
        for model_id in ("QuasarAlpha", "QuasarBeta")
    )
    logical_context = original.logical_context.model_copy(
        update={
            "models": (*original.logical_context.models, *tied_models),
        }
    )
    registry = original.model_copy(update={"logical_context": logical_context})
    query = _query("Calcula la señal quasar.")

    with pytest.raises(AdvancedQueryStudioPortError) as captured:
        RegistryWideAdvancedSemanticIndex().retrieve(
            query=query,
            extraction=_single_mention(
                query,
                "señal quasar",
                AdvancedMentionPurpose.METRIC,
            ),
            registry=registry,
        )

    assert captured.value.code is AdvancedQueryStudioPortErrorCode.RETRIEVAL_AMBIGUOUS


def _wide_registry() -> GovernedSemanticRegistrySnapshot:
    original = build_semantic_registry(repository_root=ROOT).load().registry
    additional_count = 1_000 - len(original.logical_context.field_index())
    fields: list[ApprovedRequestField] = []
    mappings: list[GovernedFieldMapping] = []
    decisions: list[str] = []
    for index in range(additional_count):
        terminal = index == additional_count - 1
        name = "terminal_ultraviolet_metric" if terminal else f"filler_attribute_{index:04d}"
        logical = LogicalFieldRef(f"WideEntity.{name}")
        physical_type = PhysicalValueType.DECIMAL if terminal else PhysicalValueType.STRING
        canonical_type = CanonicalType.DECIMAL if terminal else CanonicalType.STRING
        role = LogicalFieldRole.MEASURE if terminal else LogicalFieldRole.ATTRIBUTE
        fields.append(
            ApprovedRequestField(
                id=logical,
                canonical_type=canonical_type,
                role=role,
                definition=(
                    "Señal ultravioleta terminal gobernada."
                    if terminal
                    else f"Atributo sintético de relleno {index:04d}."
                ),
                status=ApprovalStatus.APPROVED,
                version=1,
            )
        )
        decision = f"decision_m32_wide_{index:04d}"
        decisions.append(decision)
        mappings.append(
            GovernedFieldMapping(
                mapping=ColumnMapping(
                    logical_field=logical,
                    physical_field=PhysicalFieldRef(f"synthetic.wide.{name}"),
                    confidence=ConfidenceScore(1.0),
                    status=ApprovalStatus.APPROVED,
                    evidence=("Synthetic M32 registry-width fixture.",),
                    transformation_plan=TransformationPlan(steps=(IdentityStep(),)),
                ),
                physical_type=physical_type,
                logical_field_version=1,
                approval_decision_id=decision,
            )
        )

    wide_model = ApprovedRequestModel(
        id=LogicalModelRef("WideEntity"),
        description="Población sintética amplia para probar recuperación completa.",
        fields=tuple(fields),
        status=ApprovalStatus.APPROVED,
        version=1,
    )
    logical_context = ApprovedLogicalContext(
        version=original.logical_context.version,
        source=original.logical_context.source,
        models=(*original.logical_context.models, wide_model),
        joins=original.logical_context.joins,
    )
    provenance = tuple(
        item.model_copy(update={"decision_ids": (*item.decision_ids, *decisions)})
        if item.kind is RegistryArtifactKind.PHYSICAL_MAPPINGS
        else item
        for item in original.provenance
    )
    return GovernedSemanticRegistrySnapshot(
        format_version=original.format_version,
        registry_id=original.registry_id,
        version=original.version,
        source=original.source,
        catalog_scope=original.catalog_scope,
        logical_context=logical_context,
        mapping_set=GovernedMappingRegistry(
            version=original.mapping_set.version,
            mappings=(*original.mapping_set.mappings, *mappings),
        ),
        join_contracts=original.join_contracts,
        provenance=provenance,
    )
