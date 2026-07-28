"""Provider-free contract tests for the server-owned Query Studio v17 shape.

The historical filename is retained so the focused M27 gate remains stable.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from pydantic import ValidationError

from schemabridge.adapters.language import openai_query_studio as subject
from schemabridge.application.ports.query_studio import (
    QueryStudioPortError,
    QueryStudioPortErrorCode,
)
from schemabridge.domain.concepts import CanonicalType, LogicalFieldRef, LogicalModelRef
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.query_studio import (
    DescriptionExpansion,
    DescriptionExpansionInput,
    DescriptionQuery,
    DescriptionSearchProbe,
    OpaqueCandidateId,
    ProviderOutputFailureCategory,
    QueryFieldPurpose,
    QueryStudioInterpretationInput,
    QueryStudioPromptCandidate,
    QueryStudioPromptModel,
    QueryStudioPromptVocabulary,
    QueryStudioRequiredSelectionCounts,
    SemanticMatchState,
)
from schemabridge.domain.query_studio_proposals import canonicalize_query_studio_proposal
from schemabridge.domain.request_context import LogicalFieldRole
from schemabridge.domain.requests import (
    DateGrain,
    FilterOperator,
    MetricOperation,
    SortDirection,
)


def _candidate_id(marker: str) -> OpaqueCandidateId:
    return OpaqueCandidateId(f"qsc1_{marker * 32}")


def _reconstruct_expansion(
    text: str,
    language: UserLanguage,
    slots: Sequence[tuple[str, str]],
) -> DescriptionExpansion:
    value = DescriptionExpansionInput(
        text=DescriptionQuery(text),
        language=language,
    )
    return subject._reconstruct_expansion(
        value,
        subject._OpenAIDescriptionExpansion.model_validate(
            {
                "slots": tuple(
                    {
                        "slot_id": slot_id,
                        "query": query,
                    }
                    for slot_id, query in slots
                )
            }
        ),
    )


_FIVE_CORE_CASES = (
    (
        "agrupa por fecha de registro los clientes que sean segundo titular de una cuenta",
        UserLanguage.SPANISH,
        (
            ("metric_1", "customer identifier"),
            ("dimension_1", "registration date"),
            ("filter_1", "secondary account holder role"),
        ),
        (
            ("metric_1", "clientes"),
            ("dimension_1", "fecha de registro"),
            ("filter_1", "segundo titular de una cuenta"),
        ),
        (
            ("metric_1", MetricOperation.COUNT_DISTINCT, None),
            ("dimension_1", None, DateGrain.DAY),
            ("filter_1", None, None),
        ),
    ),
    (
        "count active customers grouped by country",
        UserLanguage.ENGLISH,
        (
            ("metric_1", "customer identifier"),
            ("dimension_1", "customer country"),
            ("filter_1", "active customer status"),
        ),
        (
            ("metric_1", "customers"),
            ("dimension_1", "country"),
            ("filter_1", "active customers"),
        ),
        (
            ("metric_1", MetricOperation.COUNT_DISTINCT, None),
            ("dimension_1", None, None),
            ("filter_1", None, None),
        ),
    ),
    (
        "cuenta los productos activos por categoría",
        UserLanguage.SPANISH,
        (
            ("metric_1", "product identifier"),
            ("dimension_1", "product category"),
            ("filter_1", "product active status"),
        ),
        (
            ("metric_1", "productos"),
            ("dimension_1", "categoría"),
            ("filter_1", "productos activos"),
        ),
        (
            ("metric_1", MetricOperation.COUNT_DISTINCT, None),
            ("dimension_1", None, None),
            ("filter_1", None, None),
        ),
    ),
    (
        "count distinct delivered orders grouped by shipment delivery day",
        UserLanguage.ENGLISH,
        (
            ("metric_1", "order identifier"),
            ("dimension_1", "shipment delivery day"),
            ("filter_1", "shipment status"),
        ),
        (
            ("metric_1", "orders"),
            ("dimension_1", "shipment delivery day"),
            ("filter_1", "delivered orders"),
        ),
        (
            ("metric_1", MetricOperation.COUNT_DISTINCT, None),
            ("dimension_1", None, DateGrain.DAY),
            ("filter_1", None, None),
        ),
    ),
    (
        "suma el importe neto por fecha de pedido y categoría de producto",
        UserLanguage.SPANISH,
        (
            ("metric_1", "net amount"),
            ("dimension_1", "order date"),
            ("dimension_2", "product category"),
        ),
        (
            ("metric_1", "importe neto"),
            ("dimension_1", "fecha de pedido"),
            ("dimension_2", "categoría de producto"),
        ),
        (
            ("metric_1", MetricOperation.SUM, None),
            ("dimension_1", None, DateGrain.DAY),
            ("dimension_2", None, None),
        ),
    ),
)

_CORE_EXPECTED_FOCUS = {
    "agrupa por fecha de registro los clientes que sean segundo titular de una cuenta": (
        ("metric_1", ("customer", "identifier"), "customer identifier"),
        (
            "dimension_1",
            ("customer", "registration", "temporal"),
            "customer registration date",
        ),
        (
            "filter_1",
            ("account", "holder", "role", "secondary"),
            "account holder role",
        ),
    ),
    "count active customers grouped by country": (
        ("metric_1", ("customer", "identifier"), "customer identifier"),
        ("dimension_1", ("customer", "country"), "customer country"),
        ("filter_1", ("customer", "status", "active"), "customer status"),
    ),
    "cuenta los productos activos por categoría": (
        ("metric_1", ("product", "identifier"), "product identifier"),
        ("dimension_1", ("product", "category"), "product category"),
        ("filter_1", ("product", "status", "active"), "product status"),
    ),
    "count distinct delivered orders grouped by shipment delivery day": (
        ("metric_1", ("order", "identifier"), "order identifier"),
        (
            "dimension_1",
            ("shipment", "delivery", "temporal"),
            "shipment delivery date",
        ),
        ("filter_1", ("shipment", "delivery", "status"), "shipment status"),
    ),
    "suma el importe neto por fecha de pedido y categoría de producto": (
        ("metric_1", ("net", "amount"), "net amount"),
        ("dimension_1", ("order", "temporal"), "order date"),
        ("dimension_2", ("product", "category"), "product category"),
    ),
}

_FROZEN_HOLDOUT_CASES = (
    (
        "cuenta clientes que sean segundo titular por fecha de registro",
        UserLanguage.SPANISH,
        (
            (
                "metric_1",
                ("customer", "identifier"),
                "customer identifier",
                "clientes",
            ),
            (
                "dimension_1",
                ("customer", "registration", "temporal"),
                "customer registration date",
                "fecha de registro",
            ),
            (
                "filter_1",
                ("account", "holder", "role", "secondary"),
                "account holder role",
                "segundo titular",
            ),
        ),
    ),
    (
        "agrupa por fecha de alta los clientes que sean segundo titular de una cuenta",
        UserLanguage.SPANISH,
        (
            (
                "metric_1",
                ("customer", "identifier"),
                "customer identifier",
                "clientes",
            ),
            (
                "dimension_1",
                ("customer", "registration", "temporal"),
                "customer registration date",
                "fecha de alta",
            ),
            (
                "filter_1",
                ("account", "holder", "role", "secondary"),
                "account holder role",
                "segundo titular de una cuenta",
            ),
        ),
    ),
    (
        "count customers that are active grouped by country",
        UserLanguage.ENGLISH,
        (
            (
                "metric_1",
                ("customer", "identifier"),
                "customer identifier",
                "customers",
            ),
            (
                "dimension_1",
                ("customer", "country"),
                "customer country",
                "country",
            ),
            (
                "filter_1",
                ("customer", "status", "active"),
                "customer status",
                "customers that are active",
            ),
        ),
    ),
    (
        "group active customers by country",
        UserLanguage.ENGLISH,
        (
            (
                "metric_1",
                ("customer", "identifier"),
                "customer identifier",
                "customers",
            ),
            (
                "dimension_1",
                ("customer", "country"),
                "customer country",
                "country",
            ),
            (
                "filter_1",
                ("customer", "status", "active"),
                "customer status",
                "active customers",
            ),
        ),
    ),
    (
        "agrupa los productos activos por categoría",
        UserLanguage.SPANISH,
        (
            (
                "metric_1",
                ("product", "identifier"),
                "product identifier",
                "productos",
            ),
            (
                "dimension_1",
                ("product", "category"),
                "product category",
                "categoría",
            ),
            (
                "filter_1",
                ("product", "status", "active"),
                "product status",
                "productos activos",
            ),
        ),
    ),
    (
        "cuenta productos que sean activos por categoría",
        UserLanguage.SPANISH,
        (
            (
                "metric_1",
                ("product", "identifier"),
                "product identifier",
                "productos",
            ),
            (
                "dimension_1",
                ("product", "category"),
                "product category",
                "categoría",
            ),
            (
                "filter_1",
                ("product", "status", "active"),
                "product status",
                "productos que sean activos",
            ),
        ),
    ),
    (
        "count delivered orders by delivery date",
        UserLanguage.ENGLISH,
        (
            ("metric_1", ("order", "identifier"), "order identifier", "orders"),
            (
                "dimension_1",
                ("shipment", "delivery", "temporal"),
                "shipment delivery date",
                "delivery date",
            ),
            (
                "filter_1",
                ("shipment", "delivery", "status"),
                "shipment status",
                "delivered orders",
            ),
        ),
    ),
    (
        "group delivered orders by shipment delivery day",
        UserLanguage.ENGLISH,
        (
            ("metric_1", ("order", "identifier"), "order identifier", "orders"),
            (
                "dimension_1",
                ("shipment", "delivery", "temporal"),
                "shipment delivery date",
                "shipment delivery day",
            ),
            (
                "filter_1",
                ("shipment", "delivery", "status"),
                "shipment status",
                "delivered orders",
            ),
        ),
    ),
    (
        "suma el ingreso neto por fecha del pedido y categoría del producto",
        UserLanguage.SPANISH,
        (
            ("metric_1", ("net", "amount"), "net amount", "ingreso neto"),
            ("dimension_1", ("order", "temporal"), "order date", "fecha del pedido"),
            (
                "dimension_2",
                ("product", "category"),
                "product category",
                "categoría del producto",
            ),
        ),
    ),
    (
        "totaliza el importe neto por fecha de pedido y categoría de producto",
        UserLanguage.SPANISH,
        (
            ("metric_1", ("net", "amount"), "net amount", "importe neto"),
            ("dimension_1", ("order", "temporal"), "order date", "fecha de pedido"),
            (
                "dimension_2",
                ("product", "category"),
                "product category",
                "categoría de producto",
            ),
        ),
    ),
)


@pytest.mark.parametrize(
    ("text", "_language", "_provider_slots", "_expected_spans", "expected_specs"),
    _FIVE_CORE_CASES,
)
def test_server_derives_exact_slots_for_all_five_core_requests(
    text: str,
    _language: UserLanguage,
    _provider_slots: tuple[tuple[str, str], ...],
    _expected_spans: tuple[tuple[str, str], ...],
    expected_specs: tuple[tuple[str, MetricOperation | None, DateGrain | None], ...],
) -> None:
    slots = subject._required_slot_specs(text)

    assert (
        tuple((slot.slot_id, slot.metric_operation, slot.date_grain) for slot in slots)
        == expected_specs
    )
    assert (
        tuple((slot.slot_id, slot.semantic_focus, slot.operational_query.root) for slot in slots)
        == _CORE_EXPECTED_FOCUS[text]
    )
    assert all(
        slot.filter_operator is FilterOperator.EQUALS
        for slot in slots
        if slot.intended_use is QueryFieldPurpose.FILTER
    )


@pytest.mark.parametrize(
    ("text", "language", "provider_slots", "expected_spans", "_expected_specs"),
    _FIVE_CORE_CASES,
)
def test_server_derives_exact_contiguous_source_spans_for_all_five_core_requests(
    text: str,
    language: UserLanguage,
    provider_slots: tuple[tuple[str, str], ...],
    expected_spans: tuple[tuple[str, str], ...],
    _expected_specs: tuple[tuple[str, MetricOperation | None, DateGrain | None], ...],
) -> None:
    expansion = _reconstruct_expansion(text, language, provider_slots)

    assert (
        tuple((probe.purpose_id, probe.source_span) for probe in expansion.probes) == expected_spans
    )
    subject._require_grounded_expansion(
        DescriptionExpansionInput(text=DescriptionQuery(text), language=language),
        expansion,
    )
    subject._require_complete_expansion(
        DescriptionExpansionInput(text=DescriptionQuery(text), language=language),
        expansion,
    )


@pytest.mark.parametrize(("text", "language", "expected"), _FROZEN_HOLDOUT_CASES)
def test_v17_frozen_holdouts_keep_server_focus_queries_and_source_spans_stable(
    text: str,
    language: UserLanguage,
    expected: tuple[tuple[str, tuple[str, ...], str, str], ...],
) -> None:
    specs = subject._required_slot_specs(text)
    expansion = _reconstruct_expansion(
        text,
        language,
        tuple((slot_id, operational_query) for slot_id, _, operational_query, _ in expected),
    )

    assert tuple(
        (spec.slot_id, spec.semantic_focus, spec.operational_query.root) for spec in specs
    ) == tuple((slot_id, focus, query) for slot_id, focus, query, _ in expected)
    assert tuple(
        (probe.purpose_id, probe.query.root, probe.source_span) for probe in expansion.probes
    ) == tuple((slot_id, query, span) for slot_id, _, query, span in expected)
    subject._require_grounded_expansion(
        DescriptionExpansionInput(text=DescriptionQuery(text), language=language),
        expansion,
    )


def test_provider_paraphrases_cannot_change_operational_probes_or_fingerprint() -> None:
    text = "agrupa por fecha de registro los clientes que sean segundo titular de una cuenta"
    baseline = _reconstruct_expansion(
        text,
        UserLanguage.SPANISH,
        (
            ("metric_1", "customer identifier"),
            ("dimension_1", "customer registration date"),
            ("filter_1", "account holder role"),
        ),
    )
    plausible_variant = _reconstruct_expansion(
        text,
        UserLanguage.SPANISH,
        (
            ("metric_1", "identifier for clients"),
            ("dimension_1", "registered customer date"),
            ("filter_1", "secondary holder role"),
        ),
    )

    assert plausible_variant == baseline
    assert plausible_variant.fingerprint == baseline.fingerprint
    assert tuple(probe.query.root for probe in baseline.probes) == (
        "customer identifier",
        "customer registration date",
        "account holder role",
    )


def test_provider_query_cannot_switch_the_counted_business_entity() -> None:
    with pytest.raises(QueryStudioPortError) as raised:
        _reconstruct_expansion(
            "count clients by registration date",
            UserLanguage.ENGLISH,
            (
                ("metric_1", "account identifier"),
                ("dimension_1", "registration date"),
            ),
        )

    assert raised.value.code is QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT
    assert raised.value.output_failure_category is ProviderOutputFailureCategory.SEMANTIC_CONTRACT


def test_clients_alias_keeps_customer_focus_and_counted_identifier() -> None:
    specs = subject._required_slot_specs("count clients by registration date")

    assert tuple(
        (spec.slot_id, spec.semantic_focus, spec.operational_query.root) for spec in specs
    ) == (
        ("metric_1", ("customer", "identifier"), "customer identifier"),
        (
            "dimension_1",
            ("customer", "registration", "temporal"),
            "customer registration date",
        ),
    )


@pytest.mark.parametrize(
    "text",
    (
        "group latency by second",
        "mysterious business datum",
    ),
)
def test_insufficient_server_focus_fails_closed(text: str) -> None:
    with pytest.raises(QueryStudioPortError) as raised:
        subject._required_slot_specs(text)

    assert raised.value.code is QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT
    assert raised.value.output_failure_category is ProviderOutputFailureCategory.SEMANTIC_CONTRACT


def test_v17_provider_models_expose_only_minimal_server_references() -> None:
    assert set(subject._OpenAIExpansionSlot.model_fields) == {"slot_id", "query"}
    assert set(subject._OpenAISelectedSlot.model_fields) == {
        "slot_id",
        "option_index",
    }

    with pytest.raises(ValidationError):
        subject._OpenAIExpansionSlot.model_validate(
            {
                "slot_id": "metric_1",
                "query": "customer identifier",
                "source_span": "customers",
            }
        )
    with pytest.raises(ValidationError):
        subject._OpenAISelectedSlot.model_validate(
            {
                "slot_id": "metric_1",
                "option_index": 1,
                "candidate_id": _candidate_id("x").root,
                "filter_value": None,
            }
        )


@pytest.mark.parametrize("option_index", (0, 2))
def test_v17_provider_cannot_select_an_option_outside_the_single_exposed_index(
    option_index: int,
) -> None:
    with pytest.raises(ValidationError):
        subject._OpenAISelectedSlot.model_validate(
            {
                "slot_id": "metric_1",
                "option_index": option_index,
            }
        )


@pytest.mark.parametrize(
    "slots",
    (
        (
            ("metric_1", "customer identifier"),
            ("dimension_1", "customer registration date"),
        ),
        (
            ("metric_1", "customer identifier"),
            ("metric_1", "customer identifier"),
            ("dimension_1", "customer registration date"),
            ("filter_1", "secondary holder role"),
        ),
        (
            ("metric_1", "customer identifier"),
            ("dimension_1", "customer registration date"),
            ("filter_2", "secondary holder role"),
        ),
    ),
)
def test_expansion_requires_exact_server_owned_slot_coverage(
    slots: tuple[tuple[str, str], ...],
) -> None:
    with pytest.raises(QueryStudioPortError) as raised:
        _reconstruct_expansion(
            "agrupa por fecha de registro los clientes que sean segundo titular de una cuenta",
            UserLanguage.SPANISH,
            slots,
        )

    assert raised.value.code is QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT
    assert raised.value.output_failure_category is ProviderOutputFailureCategory.SEMANTIC_CONTRACT


def test_unicode_source_span_derivation_preserves_the_original_accented_text() -> None:
    expansion = _reconstruct_expansion(
        "suma importe por año",
        UserLanguage.SPANISH,
        (
            ("metric_1", "amount"),
            ("dimension_1", "year"),
        ),
    )

    assert tuple(probe.source_span for probe in expansion.probes) == (
        "importe",
        "año",
    )


def test_equally_supported_source_spans_fail_closed() -> None:
    text = "sum revenue by date and time"
    value = DescriptionExpansionInput(
        text=DescriptionQuery(text),
        language=UserLanguage.ENGLISH,
    )
    spec = next(
        slot for slot in subject._required_slot_specs(text) if slot.slot_id == "dimension_1"
    )

    with pytest.raises(QueryStudioPortError, match="equally supported"):
        subject._derive_source_span(
            value,
            spec=spec,
            query=DescriptionQuery("temporal date"),
        )


def test_query_without_a_server_known_anchor_fails_closed() -> None:
    text = "count customers by country"
    value = DescriptionExpansionInput(
        text=DescriptionQuery(text),
        language=UserLanguage.ENGLISH,
    )
    spec = next(
        slot for slot in subject._required_slot_specs(text) if slot.slot_id == "dimension_1"
    )

    with pytest.raises(QueryStudioPortError, match="no server-owned semantic focus anchor"):
        subject._derive_source_span(
            value,
            spec=spec,
            query=DescriptionQuery("opaque mystery field"),
        )


def _multi_sum_interpretation() -> QueryStudioInterpretationInput:
    text = DescriptionQuery(
        "suma ingresos netos y unidades por categoría de producto y mes de pedido"
    )
    expansion = _reconstruct_expansion(
        text.root,
        UserLanguage.SPANISH,
        (
            ("dimension_2", "order date"),
            ("metric_2", "quantity"),
            ("metric_1", "net amount"),
            ("dimension_1", "product category"),
        ),
    )
    rows = (
        (
            "a",
            "net_amount",
            CanonicalType.DECIMAL,
            LogicalFieldRole.MEASURE,
            QueryFieldPurpose.METRIC,
            "metric_1",
        ),
        (
            "b",
            "quantity",
            CanonicalType.INTEGER,
            LogicalFieldRole.MEASURE,
            QueryFieldPurpose.METRIC,
            "metric_2",
        ),
        (
            "c",
            "category",
            CanonicalType.STRING,
            LogicalFieldRole.ATTRIBUTE,
            QueryFieldPurpose.DIMENSION,
            "dimension_1",
        ),
        (
            "d",
            "ordered_at",
            CanonicalType.TIMESTAMP,
            LogicalFieldRole.TEMPORAL,
            QueryFieldPurpose.DIMENSION,
            "dimension_2",
        ),
    )
    vocabulary = QueryStudioPromptVocabulary(
        context_source="synthetic:v16",
        context_version=1,
        models=(
            QueryStudioPromptModel(
                id=LogicalModelRef("Synthetic"),
                definition="Synthetic governed commerce model.",
            ),
        ),
        candidates=tuple(
            QueryStudioPromptCandidate(
                candidate_id=_candidate_id(marker),
                logical_field=LogicalFieldRef(f"Synthetic.{field}"),
                definition=f"Synthetic governed {field}.",
                canonical_type=canonical_type,
                role=role,
                intended_uses=(purpose,),
                purpose_ids=(purpose_id,),
                score=100 - index,
            )
            for index, (
                marker,
                field,
                canonical_type,
                role,
                purpose,
                purpose_id,
            ) in enumerate(rows)
        ),
        required_selection_counts=QueryStudioRequiredSelectionCounts(
            dimensions=2,
            metrics=2,
            filters=0,
        ),
        metric_operations=tuple(MetricOperation),
        filter_operators=tuple(FilterOperator),
        date_grains=tuple(DateGrain),
        sort_directions=tuple(SortDirection),
    )
    return QueryStudioInterpretationInput(
        text=text,
        language=UserLanguage.SPANISH,
        vocabulary=vocabulary,
        expansion=expansion,
    )


def test_option_one_is_mapped_to_server_owned_candidates_and_canonicalized() -> None:
    value = _multi_sum_interpretation()
    provider = subject._OpenAIQueryStudioSelections.model_validate(
        {
            "selections": tuple(
                {
                    "slot_id": slot_id,
                    "option_index": 1,
                }
                for slot_id in (
                    "metric_1",
                    "metric_2",
                    "dimension_1",
                    "dimension_2",
                )
            ),
            "ambiguity_kinds": (),
        }
    )

    proposal = canonicalize_query_studio_proposal(
        subject._reconstruct_proposal(provider, value),
        value,
    )

    assert proposal.semantic_state is SemanticMatchState.ALIGNED
    assert tuple(metric.operation for metric in proposal.metrics) == (
        MetricOperation.SUM,
        MetricOperation.SUM,
    )
    assert tuple(dimension.grain for dimension in proposal.dimensions) == (
        None,
        DateGrain.MONTH,
    )
    assert proposal.primary_candidate_id == _candidate_id("a")
    assert proposal.limit == 500


def test_interpretation_requires_exact_slot_coverage() -> None:
    value = _multi_sum_interpretation()
    partial = subject._OpenAIQueryStudioSelections.model_validate(
        {
            "selections": (
                {
                    "slot_id": "metric_1",
                    "option_index": 1,
                },
            ),
            "ambiguity_kinds": (),
        }
    )

    with pytest.raises(QueryStudioPortError) as raised:
        subject._reconstruct_proposal(partial, value)

    assert raised.value.code is QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT
    assert raised.value.output_failure_category is ProviderOutputFailureCategory.SEMANTIC_CONTRACT


def test_same_use_candidate_outside_the_exact_slot_is_rejected() -> None:
    probe = DescriptionSearchProbe(
        purpose_id="metric_1",
        query=DescriptionQuery("customer identifier"),
        source_span="customers",
        intended_use=QueryFieldPurpose.METRIC,
        roles=(LogicalFieldRole.IDENTIFIER,),
        metric_operation=MetricOperation.COUNT_DISTINCT,
    )
    candidate = QueryStudioPromptCandidate(
        candidate_id=_candidate_id("x"),
        logical_field=LogicalFieldRef("Synthetic.other_key"),
        definition="A different governed identifier.",
        canonical_type=CanonicalType.STRING,
        role=LogicalFieldRole.IDENTIFIER,
        intended_uses=(QueryFieldPurpose.METRIC,),
        purpose_ids=("metric_2",),
        score=100,
    )

    with pytest.raises(QueryStudioPortError) as raised:
        subject._require_candidate_matches_slot(candidate, probe)

    assert raised.value.code is QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT
    assert raised.value.output_failure_category is ProviderOutputFailureCategory.SEMANTIC_CONTRACT


@pytest.mark.parametrize(
    ("value", "source", "expected"),
    (
        ("1", "amount greater than 100", False),
        ("east", "region northeast", False),
        ("100", "amount greater than 100", True),
        ("north east", "region north east", True),
    ),
)
def test_filter_grounding_uses_exact_token_sequences(
    value: str,
    source: str,
    expected: bool,
) -> None:
    assert subject._value_is_grounded(value, source) is expected
