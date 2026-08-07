from __future__ import annotations

import json

import pytest
from tests.m32_natural_support import (
    M32_REFERENCE_QUESTION,
    build_m32_reference_request,
    build_m32_simple_row_request,
)

from schemabridge.adapters.query_studio.advanced_fake_language import (
    M32_AMBIGUOUS_DATE_QUESTION_ES,
    M32_REFERENCE_QUESTION_ES,
    M32_SIMPLE_PRODUCTS_QUESTION_ES,
    M32_UNSUPPORTED_CROSS_JOIN_QUESTION_ES,
    AdvancedLanguageCase,
    AdvancedLanguageMentionFixture,
    DeterministicAdvancedLanguageAdapter,
)
from schemabridge.application.ports.advanced_query_studio import (
    AdvancedInterpretationPort,
    AdvancedMentionExtractionPort,
    AdvancedQueryStudioPortError,
    AdvancedQueryStudioPortErrorCode,
)
from schemabridge.domain.advanced_query_studio import (
    AdvancedApprovedSemanticContext,
    AdvancedInterpretationAmbiguity,
    AdvancedInterpretationInput,
    AdvancedInterpretationResult,
    AdvancedMentionExtraction,
    AdvancedMentionExtractionInput,
    AdvancedMentionExtractionResult,
    AdvancedMentionPurpose,
    AdvancedNaturalLanguageInput,
    AdvancedSemanticField,
    AdvancedSemanticJoin,
    AdvancedSemanticModel,
)
from schemabridge.domain.concepts import CanonicalType, LogicalFieldRef, LogicalModelRef
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.joins import Cardinality, FanoutPolicy
from schemabridge.domain.request_context import LogicalFieldRole

SHA_A = "a" * 64


def _reference_context() -> AdvancedApprovedSemanticContext:
    return AdvancedApprovedSemanticContext(
        context_source="registry:synthetic_enterprise",
        context_version=1,
        approved_context_fingerprint=SHA_A,
        governed_registry_fingerprint="b" * 64,
        scope_fingerprint="c" * 64,
        models=(
            AdvancedSemanticModel(
                id=LogicalModelRef("SaleLine"),
                definition="Una línea comercial vendida.",
            ),
            AdvancedSemanticModel(
                id=LogicalModelRef("SalesOrder"),
                definition="Un pedido comercial.",
            ),
            AdvancedSemanticModel(
                id=LogicalModelRef("Product"),
                definition="Un producto gobernado.",
            ),
        ),
        fields=(
            AdvancedSemanticField(
                id=LogicalFieldRef("SalesOrder.ordered_at"),
                canonical_type=CanonicalType.TIMESTAMP,
                role=LogicalFieldRole.TEMPORAL,
                definition="Fecha y hora aprobadas del pedido.",
            ),
            AdvancedSemanticField(
                id=LogicalFieldRef("Product.category"),
                canonical_type=CanonicalType.STRING,
                role=LogicalFieldRole.ATTRIBUTE,
                definition="Categoría comercial aprobada.",
            ),
            AdvancedSemanticField(
                id=LogicalFieldRef("SaleLine.net_amount"),
                canonical_type=CanonicalType.DECIMAL,
                role=LogicalFieldRole.MEASURE,
                definition="Importe neto aprobado.",
            ),
            AdvancedSemanticField(
                id=LogicalFieldRef("SaleLine.quantity"),
                canonical_type=CanonicalType.INTEGER,
                role=LogicalFieldRole.MEASURE,
                definition="Unidades vendidas.",
            ),
            AdvancedSemanticField(
                id=LogicalFieldRef("SalesOrder.order_key"),
                canonical_type=CanonicalType.STRING,
                role=LogicalFieldRole.IDENTIFIER,
                definition="Identificador gobernado del pedido.",
            ),
            AdvancedSemanticField(
                id=LogicalFieldRef("SalesOrder.order_status"),
                canonical_type=CanonicalType.STRING,
                role=LogicalFieldRole.ATTRIBUTE,
                definition="Estado normalizado del pedido.",
                allowed_values=("COMPLETED", "CANCELLED"),
            ),
        ),
        joins=(
            AdvancedSemanticJoin(
                id="sale_line_to_order",
                left_model=LogicalModelRef("SaleLine"),
                right_model=LogicalModelRef("SalesOrder"),
                cardinality=Cardinality.MANY_TO_ONE,
                fanout_policy=FanoutPolicy.NONE,
            ),
            AdvancedSemanticJoin(
                id="sale_line_to_product",
                left_model=LogicalModelRef("SaleLine"),
                right_model=LogicalModelRef("Product"),
                cardinality=Cardinality.MANY_TO_ONE,
                fanout_policy=FanoutPolicy.NONE,
            ),
        ),
    )


def _product_context() -> AdvancedApprovedSemanticContext:
    return AdvancedApprovedSemanticContext(
        context_source="registry:synthetic_enterprise",
        context_version=1,
        approved_context_fingerprint=SHA_A,
        governed_registry_fingerprint="b" * 64,
        scope_fingerprint="c" * 64,
        models=(
            AdvancedSemanticModel(
                id=LogicalModelRef("Product"),
                definition="Un producto gobernado.",
            ),
        ),
        fields=(
            AdvancedSemanticField(
                id=LogicalFieldRef("Product.product_key"),
                canonical_type=CanonicalType.STRING,
                role=LogicalFieldRole.IDENTIFIER,
                definition="Identificador gobernado del producto.",
            ),
            AdvancedSemanticField(
                id=LogicalFieldRef("Product.category"),
                canonical_type=CanonicalType.STRING,
                role=LogicalFieldRole.ATTRIBUTE,
                definition="Categoría comercial aprobada.",
            ),
            AdvancedSemanticField(
                id=LogicalFieldRef("Product.unit_price"),
                canonical_type=CanonicalType.DECIMAL,
                role=LogicalFieldRole.MEASURE,
                definition="Precio unitario aprobado.",
            ),
            AdvancedSemanticField(
                id=LogicalFieldRef("Product.is_active"),
                canonical_type=CanonicalType.BOOLEAN,
                role=LogicalFieldRole.ATTRIBUTE,
                definition="Indicador de producto activo.",
            ),
        ),
    )


def _query(text: str) -> AdvancedNaturalLanguageInput:
    return AdvancedNaturalLanguageInput(
        text=text,
        language=UserLanguage.SPANISH,
    )


def _extract(
    port: AdvancedMentionExtractionPort,
    query: AdvancedNaturalLanguageInput,
) -> AdvancedMentionExtractionResult:
    return port.extract(AdvancedMentionExtractionInput(query=query))


def _interpret(
    port: AdvancedInterpretationPort,
    query: AdvancedNaturalLanguageInput,
    extraction: AdvancedMentionExtraction,
    context: AdvancedApprovedSemanticContext,
) -> AdvancedInterpretationResult:
    return port.interpret(
        AdvancedInterpretationInput(
            query=query,
            extraction=extraction,
            context=context,
        )
    )


def test_reference_question_produces_grounded_mentions_and_exact_request() -> None:
    adapter = DeterministicAdvancedLanguageAdapter()
    query = _query(M32_REFERENCE_QUESTION)

    extraction_result = _extract(adapter, query)
    interpretation = _interpret(
        adapter,
        query,
        extraction_result.extraction,
        _reference_context(),
    )

    assert M32_REFERENCE_QUESTION_ES == M32_REFERENCE_QUESTION
    assert len(extraction_result.extraction.mentions) == 12
    assert all(
        query.text[item.source_span.start : item.source_span.end] == item.value
        for item in extraction_result.extraction.mentions
    )
    assert interpretation.envelope.request == build_m32_reference_request()
    assert interpretation.envelope.ambiguities == ()
    assert extraction_result.usage is None
    assert interpretation.usage is None

    payload = interpretation.envelope.model_dump(mode="json")
    encoded = json.dumps(payload, sort_keys=True)
    assert "sql" not in payload
    assert "physical" not in encoded
    assert "schema" not in encoded
    assert "table" not in encoded
    assert "column" not in encoded


def test_simple_active_products_question_is_v1_representable_typed_v2() -> None:
    adapter = DeterministicAdvancedLanguageAdapter()
    query = _query(M32_SIMPLE_PRODUCTS_QUESTION_ES)

    extraction = _extract(adapter, query).extraction
    interpretation = _interpret(adapter, query, extraction, _product_context())

    request = interpretation.envelope.request
    assert request == build_m32_simple_row_request()
    assert request is not None
    assert request.version == 2
    assert request.windows == ()
    assert request.having is None
    assert request.post_filter is None
    assert request.group_by == ()
    assert request.metrics == ()


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        (
            M32_AMBIGUOUS_DATE_QUESTION_ES,
            (AdvancedInterpretationAmbiguity.DATE_MEANING,),
        ),
        (
            M32_UNSUPPORTED_CROSS_JOIN_QUESTION_ES,
            (AdvancedInterpretationAmbiguity.UNSUPPORTED_REQUEST,),
        ),
    ),
)
def test_ambiguity_and_unsupported_request_return_no_typed_request(
    text: str,
    expected: tuple[AdvancedInterpretationAmbiguity, ...],
) -> None:
    adapter = DeterministicAdvancedLanguageAdapter()
    query = _query(text)

    extraction = _extract(adapter, query).extraction
    interpretation = _interpret(adapter, query, extraction, _product_context())

    assert interpretation.envelope.request is None
    assert interpretation.envelope.ambiguities == expected


def test_gaps_and_islands_natural_request_is_explicitly_unsupported() -> None:
    query = _query("Encuentra huecos e islas en la secuencia de productos por identificador.")
    adapter = DeterministicAdvancedLanguageAdapter(
        cases=(
            AdvancedLanguageCase(
                query=query,
                mentions=(
                    AdvancedLanguageMentionFixture(
                        phrase="huecos e islas",
                        purpose=AdvancedMentionPurpose.WINDOW,
                    ),
                    AdvancedLanguageMentionFixture(
                        phrase="productos",
                        purpose=AdvancedMentionPurpose.PRIMARY_ENTITY,
                    ),
                    AdvancedLanguageMentionFixture(
                        phrase="identificador",
                        purpose=AdvancedMentionPurpose.DIMENSION,
                    ),
                ),
                ambiguities=(AdvancedInterpretationAmbiguity.UNSUPPORTED_REQUEST,),
            ),
        )
    )

    extraction = _extract(adapter, query).extraction
    interpretation = _interpret(adapter, query, extraction, _product_context())

    assert interpretation.envelope.request is None
    assert interpretation.envelope.ambiguities == (
        AdvancedInterpretationAmbiguity.UNSUPPORTED_REQUEST,
    )
    assert "sql" not in interpretation.envelope.model_dump(mode="json")


def test_unknown_or_near_match_input_fails_closed_without_echoing_text() -> None:
    adapter = DeterministicAdvancedLanguageAdapter()
    query = _query(f"{M32_SIMPLE_PRODUCTS_QUESTION_ES} ")

    with pytest.raises(AdvancedQueryStudioPortError) as captured:
        _extract(adapter, query)

    assert captured.value.code is AdvancedQueryStudioPortErrorCode.UNSUPPORTED_INPUT
    assert query.text not in str(captured.value)


def test_interpretation_rejects_a_forged_but_grounded_extraction() -> None:
    adapter = DeterministicAdvancedLanguageAdapter()
    query = _query(M32_REFERENCE_QUESTION_ES)
    extraction = _extract(adapter, query).extraction
    forged = AdvancedMentionExtraction(
        request_digest=extraction.request_digest,
        mentions=extraction.mentions[:-1],
    )

    with pytest.raises(AdvancedQueryStudioPortError) as captured:
        _interpret(adapter, query, forged, _reference_context())

    assert captured.value.code is AdvancedQueryStudioPortErrorCode.INPUT_MISMATCH


def test_interpretation_rejects_context_that_omits_a_requested_field() -> None:
    adapter = DeterministicAdvancedLanguageAdapter()
    query = _query(M32_REFERENCE_QUESTION_ES)
    extraction = _extract(adapter, query).extraction
    context = _reference_context()
    incomplete = AdvancedApprovedSemanticContext(
        **{
            **context.model_dump(),
            "fields": tuple(
                item for item in context.fields if item.id != LogicalFieldRef("SaleLine.net_amount")
            ),
        }
    )

    with pytest.raises(AdvancedQueryStudioPortError) as captured:
        _interpret(adapter, query, extraction, incomplete)

    assert captured.value.code is AdvancedQueryStudioPortErrorCode.GOVERNED_CONTEXT_MISMATCH
    assert "SaleLine.net_amount" not in str(captured.value)


def test_cases_are_injectable_and_replace_the_default_vocabulary() -> None:
    query = _query("Resumen comercial")
    case = AdvancedLanguageCase(
        query=query,
        mentions=(
            AdvancedLanguageMentionFixture(
                phrase="Resumen",
                purpose=AdvancedMentionPurpose.METRIC,
            ),
        ),
        ambiguities=(AdvancedInterpretationAmbiguity.METRIC_MEANING,),
    )
    adapter = DeterministicAdvancedLanguageAdapter(cases=(case,))

    extraction = _extract(adapter, query).extraction
    result = _interpret(adapter, query, extraction, _product_context())

    assert result.envelope.ambiguities == (AdvancedInterpretationAmbiguity.METRIC_MEANING,)
    with pytest.raises(AdvancedQueryStudioPortError) as captured:
        _extract(adapter, _query(M32_REFERENCE_QUESTION_ES))
    assert captured.value.code is AdvancedQueryStudioPortErrorCode.UNSUPPORTED_INPUT


def test_invalid_or_duplicate_injected_cases_are_rejected_at_construction() -> None:
    query = _query("Resumen comercial")
    with pytest.raises(ValueError, match="not grounded"):
        AdvancedLanguageCase(
            query=query,
            mentions=(
                AdvancedLanguageMentionFixture(
                    phrase="inexistente",
                    purpose=AdvancedMentionPurpose.METRIC,
                ),
            ),
            ambiguities=(AdvancedInterpretationAmbiguity.METRIC_MEANING,),
        )

    case = AdvancedLanguageCase(
        query=query,
        mentions=(
            AdvancedLanguageMentionFixture(
                phrase="Resumen",
                purpose=AdvancedMentionPurpose.METRIC,
            ),
        ),
        ambiguities=(AdvancedInterpretationAmbiguity.METRIC_MEANING,),
    )
    with pytest.raises(ValueError, match="cases must be unique"):
        DeterministicAdvancedLanguageAdapter(cases=(case, case))


def test_port_error_requires_a_closed_code_and_sanitized_message() -> None:
    with pytest.raises(TypeError, match="code is invalid"):
        AdvancedQueryStudioPortError(
            "unsupported",  # type: ignore[arg-type]
            "safe",
        )
    with pytest.raises(ValueError, match="must not be blank"):
        AdvancedQueryStudioPortError(
            AdvancedQueryStudioPortErrorCode.INVALID_ADAPTER_OUTPUT,
            " ",
        )
