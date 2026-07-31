from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemabridge.domain.advanced_query_studio import (
    AdvancedApprovedSemanticContext,
    AdvancedInterpretationAmbiguity,
    AdvancedInterpretationEnvelope,
    AdvancedInterpretationInput,
    AdvancedInterpretationResult,
    AdvancedMentionExtraction,
    AdvancedMentionExtractionInput,
    AdvancedMentionExtractionResult,
    AdvancedMentionPurpose,
    AdvancedNaturalLanguageInput,
    AdvancedQueryConfirmation,
    AdvancedQueryConfirmationAction,
    AdvancedQueryMention,
    AdvancedQueryPreview,
    AdvancedQueryRoute,
    AdvancedSemanticField,
    AdvancedSemanticJoin,
    AdvancedSemanticModel,
    AdvancedSourceSpan,
    SignedAdvancedQueryPreviewToken,
    advanced_interpretation_fingerprint,
    advanced_mention_extraction_fingerprint,
    advanced_query_confirmation_fingerprint,
    advanced_query_preview_fingerprint,
    advanced_query_request_digest,
    advanced_routed_request_fingerprint,
    advanced_semantic_context_fingerprint,
    confirm_advanced_query_preview,
)
from schemabridge.domain.advanced_requests import (
    AdvancedAnalyticalRequest,
    AdvancedField,
    AdvancedMetric,
    AdvancedMetricOperation,
    AdvancedQueryMode,
    LogicalBooleanPredicate,
)
from schemabridge.domain.concepts import CanonicalType, LogicalFieldRef, LogicalModelRef
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.joins import Cardinality, FanoutPolicy
from schemabridge.domain.query_studio import (
    ProviderOutcomeCode,
    ProviderStage,
    ProviderUsageFacts,
)
from schemabridge.domain.request_context import (
    LogicalFieldRole,
    ValidatedAdvancedAnalyticalRequest,
    ValidatedAnalyticalRequest,
    ValidatedRequestLike,
)
from schemabridge.domain.requests import (
    AnalyticalRequest,
    Filter,
    FilterOperator,
    Metric,
    MetricOperation,
)
from schemabridge.domain.resolution import ResolutionAssumption

SHA_A = "a" * 64
SHA_B = "b" * 64


def _query() -> AdvancedNaturalLanguageInput:
    return AdvancedNaturalLanguageInput(
        text="Ingresos por categoría para pedidos completados",
        language=UserLanguage.SPANISH,
    )


def _extraction(query: AdvancedNaturalLanguageInput | None = None) -> AdvancedMentionExtraction:
    query = query or _query()
    values = (
        ("Ingresos", AdvancedMentionPurpose.METRIC),
        ("categoría", AdvancedMentionPurpose.DIMENSION),
        ("pedidos completados", AdvancedMentionPurpose.FILTER),
    )
    mentions = tuple(
        AdvancedQueryMention(
            value=value,
            source_span=AdvancedSourceSpan(
                start=query.text.index(value),
                end=query.text.index(value) + len(value),
            ),
            purpose=purpose,
        )
        for value, purpose in values
    )
    return AdvancedMentionExtraction(
        request_digest=query.digest,
        mentions=mentions,
    )


def _context() -> AdvancedApprovedSemanticContext:
    return AdvancedApprovedSemanticContext(
        context_source="registry:commerce",
        context_version=3,
        approved_context_fingerprint=SHA_A,
        governed_registry_fingerprint=SHA_B,
        scope_fingerprint=SHA_B,
        models=(
            AdvancedSemanticModel(
                id=LogicalModelRef("SaleLine"),
                definition="Una línea vendida.",
            ),
            AdvancedSemanticModel(
                id=LogicalModelRef("SalesOrder"),
                definition="Un pedido de venta.",
            ),
        ),
        fields=(
            AdvancedSemanticField(
                id=LogicalFieldRef("SaleLine.category"),
                canonical_type=CanonicalType.STRING,
                role=LogicalFieldRole.ATTRIBUTE,
                definition="Categoría comercial.",
            ),
            AdvancedSemanticField(
                id=LogicalFieldRef("SaleLine.net_amount"),
                canonical_type=CanonicalType.DECIMAL,
                role=LogicalFieldRole.MEASURE,
                definition="Importe neto de la línea.",
            ),
            AdvancedSemanticField(
                id=LogicalFieldRef("SalesOrder.status"),
                canonical_type=CanonicalType.STRING,
                role=LogicalFieldRole.ATTRIBUTE,
                definition="Estado aprobado del pedido.",
                allowed_values=("COMPLETED", "CANCELLED"),
            ),
        ),
        joins=(
            AdvancedSemanticJoin(
                id="sale_line_order",
                left_model=LogicalModelRef("SaleLine"),
                right_model=LogicalModelRef("SalesOrder"),
                cardinality=Cardinality.MANY_TO_ONE,
                fanout_policy=FanoutPolicy.NONE,
            ),
        ),
    )


def _advanced_request(
    *,
    status: str = "COMPLETED",
    field: str = "SaleLine.category",
) -> AdvancedAnalyticalRequest:
    return AdvancedAnalyticalRequest(
        version=2,
        mode=AdvancedQueryMode.AGGREGATE,
        primary_entity=LogicalModelRef("SaleLine"),
        fields=(
            AdvancedField(
                field=LogicalFieldRef(field),
                alias="category",
            ),
        ),
        metrics=(
            AdvancedMetric(
                operation=AdvancedMetricOperation.SUM,
                field=LogicalFieldRef("SaleLine.net_amount"),
                alias="net_revenue",
            ),
        ),
        group_by=("category",),
        where=LogicalBooleanPredicate.leaf(
            Filter(
                field=LogicalFieldRef("SalesOrder.status"),
                operator=FilterOperator.EQUALS,
                value=status,
            )
        ),
    )


def _interpretation_input() -> AdvancedInterpretationInput:
    query = _query()
    return AdvancedInterpretationInput(
        query=query,
        extraction=_extraction(query),
        context=_context(),
    )


def _envelope(
    value: AdvancedInterpretationInput | None = None,
) -> AdvancedInterpretationEnvelope:
    value = value or _interpretation_input()
    return AdvancedInterpretationEnvelope(
        request_digest=value.query.digest,
        mention_fingerprint=value.extraction.fingerprint,
        semantic_context_fingerprint=value.context.fingerprint,
        request=_advanced_request(),
    )


def _usage(stage: ProviderStage) -> ProviderUsageFacts:
    return ProviderUsageFacts(
        stage=stage,
        model_snapshot="model-v1",
        configuration_fingerprint=SHA_B,
        input_tokens=100,
        output_tokens=50,
        duration_ms=10,
        outcome=ProviderOutcomeCode.SUCCEEDED,
    )


def _validated(
    request: AnalyticalRequest | AdvancedAnalyticalRequest,
) -> ValidatedRequestLike:
    validated_type = (
        ValidatedAdvancedAnalyticalRequest
        if isinstance(request, AdvancedAnalyticalRequest)
        else ValidatedAnalyticalRequest
    )
    return validated_type(
        request=request,
        context_source="registry:commerce",
        context_version=3,
        context_fingerprint=SHA_A,
        required_models=(
            LogicalModelRef("SaleLine"),
            LogicalModelRef("SalesOrder"),
        ),
        join_contract_ids=("sale_line_order",),
    )


def _preview(
    request: AnalyticalRequest | AdvancedAnalyticalRequest | None = None,
) -> AdvancedQueryPreview:
    request = request or _advanced_request()
    route = (
        AdvancedQueryRoute.V2
        if isinstance(request, AdvancedAnalyticalRequest)
        else AdvancedQueryRoute.V1
    )
    return AdvancedQueryPreview(
        request_digest=_query().digest,
        mention_fingerprint=_extraction().fingerprint,
        semantic_context_fingerprint=_context().fingerprint,
        approved_context_fingerprint=SHA_A,
        governed_registry_fingerprint=SHA_B,
        scope_fingerprint=SHA_B,
        interpretation_fingerprint=_envelope().fingerprint,
        resolved_plan_fingerprint=SHA_A,
        datasets=(PhysicalDatasetRef("sales.order_lines"),),
        assumptions=(
            ResolutionAssumption(
                code="approved_context_only",
                message="Only approved governed context is used.",
            ),
        ),
        route=route,
        routed_request=request,
        routed_request_fingerprint=advanced_routed_request_fingerprint(request),
        validated_request=_validated(request),
    )


def test_natural_input_is_bounded_frozen_and_forbids_extra_fields() -> None:
    query = _query()

    assert query.digest == advanced_query_request_digest(query)
    with pytest.raises(ValidationError, match="frozen"):
        query.text = "otro"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="Extra inputs"):
        AdvancedNaturalLanguageInput(
            text="consulta",
            language=UserLanguage.SPANISH,
            sql="SELECT 1",  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError, match="at most 2000"):
        AdvancedNaturalLanguageInput(
            text="x" * 2_001,
            language=UserLanguage.SPANISH,
        )


def test_mentions_are_exactly_grounded_and_bounded() -> None:
    query = _query()
    extraction = _extraction(query)
    result = AdvancedMentionExtractionResult(
        input=AdvancedMentionExtractionInput(query=query),
        extraction=extraction,
    )

    assert result.extraction.fingerprint == advanced_mention_extraction_fingerprint(extraction)

    first = extraction.mentions[0]
    malformed = extraction.model_copy(
        update={
            "mentions": (
                first.model_copy(update={"value": "ingresos"}),
                *extraction.mentions[1:],
            )
        }
    )
    with pytest.raises(ValidationError, match="not grounded"):
        AdvancedMentionExtractionResult(
            input=AdvancedMentionExtractionInput(query=query),
            extraction=malformed,
        )

    with pytest.raises(ValidationError, match="at most 12"):
        AdvancedMentionExtraction(
            request_digest=query.digest,
            mentions=tuple(extraction.mentions[0] for _ in range(13)),
        )


def test_semantic_context_requires_owned_connected_approved_slice() -> None:
    context = _context()

    assert context.fingerprint == advanced_semantic_context_fingerprint(context)
    assert context.fingerprint == advanced_semantic_context_fingerprint(
        AdvancedApprovedSemanticContext.model_validate(context.model_dump())
    )

    with pytest.raises(ValidationError, match="connected graph"):
        AdvancedApprovedSemanticContext(
            **{
                **context.model_dump(),
                "joins": (),
            }
        )
    with pytest.raises(ValidationError, match="belong to included models"):
        AdvancedApprovedSemanticContext(
            **{
                **context.model_dump(),
                "fields": (
                    *context.fields,
                    AdvancedSemanticField(
                        id=LogicalFieldRef("Unknown.value"),
                        canonical_type=CanonicalType.STRING,
                        role=LogicalFieldRole.ATTRIBUTE,
                        definition="Campo fuera del contexto.",
                    ),
                ),
            }
        )


def test_interpretation_envelope_is_request_xor_ambiguities_and_never_sql() -> None:
    value = _interpretation_input()
    envelope = _envelope(value)

    assert envelope.fingerprint == advanced_interpretation_fingerprint(envelope)
    with pytest.raises(ValidationError, match="either one request or explicit ambiguities"):
        AdvancedInterpretationEnvelope(
            request_digest=value.query.digest,
            mention_fingerprint=value.extraction.fingerprint,
            semantic_context_fingerprint=value.context.fingerprint,
        )
    with pytest.raises(ValidationError, match="either one request or explicit ambiguities"):
        AdvancedInterpretationEnvelope(
            request_digest=value.query.digest,
            mention_fingerprint=value.extraction.fingerprint,
            semantic_context_fingerprint=value.context.fingerprint,
            request=_advanced_request(),
            ambiguities=(AdvancedInterpretationAmbiguity.JOIN_PATH,),
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        AdvancedInterpretationEnvelope(
            request_digest=value.query.digest,
            mention_fingerprint=value.extraction.fingerprint,
            semantic_context_fingerprint=value.context.fingerprint,
            ambiguities=(AdvancedInterpretationAmbiguity.UNSUPPORTED_REQUEST,),
            sql="SELECT 1",  # type: ignore[call-arg]
        )


def test_interpretation_result_enforces_context_grounding_and_allowed_values() -> None:
    value = _interpretation_input()
    result = AdvancedInterpretationResult(
        input=value,
        envelope=_envelope(value),
    )

    assert result.envelope.request == _advanced_request()

    outside = _envelope(value).model_copy(
        update={"request": _advanced_request(field="Unknown.category")}
    )
    with pytest.raises(ValidationError, match="outside semantic context"):
        AdvancedInterpretationResult(input=value, envelope=outside)

    disallowed = _envelope(value).model_copy(
        update={"request": _advanced_request(status="PENDING")}
    )
    with pytest.raises(ValidationError, match="outside approved field values"):
        AdvancedInterpretationResult(input=value, envelope=disallowed)


def test_port_results_accept_only_successful_usage_at_their_stage() -> None:
    query = _query()
    extraction = _extraction(query)
    AdvancedMentionExtractionResult(
        input=AdvancedMentionExtractionInput(query=query),
        extraction=extraction,
        usage=_usage(ProviderStage.EXPANSION),
    )
    with pytest.raises(ValidationError, match="expansion-stage"):
        AdvancedMentionExtractionResult(
            input=AdvancedMentionExtractionInput(query=query),
            extraction=extraction,
            usage=_usage(ProviderStage.INTERPRETATION),
        )

    value = _interpretation_input()
    AdvancedInterpretationResult(
        input=value,
        envelope=_envelope(value),
        usage=_usage(ProviderStage.INTERPRETATION),
    )
    with pytest.raises(ValidationError, match="interpretation usage"):
        AdvancedInterpretationResult(
            input=value,
            envelope=_envelope(value),
            usage=_usage(ProviderStage.EXPANSION),
        )


def test_preview_is_text_free_and_binds_v2_route_request_and_context() -> None:
    preview = _preview()
    payload = preview.model_dump(mode="json")

    assert preview.fingerprint == advanced_query_preview_fingerprint(preview)
    assert "text" not in payload
    assert preview.route is AdvancedQueryRoute.V2

    with pytest.raises(ValidationError, match="v1 route"):
        AdvancedQueryPreview(**{**payload, "route": AdvancedQueryRoute.V1})
    with pytest.raises(ValidationError, match="different approved context"):
        AdvancedQueryPreview(
            **{
                **payload,
                "approved_context_fingerprint": SHA_B,
            }
        )


def test_preview_accepts_a_simple_v1_request_on_the_v1_route() -> None:
    request = AnalyticalRequest(
        primary_entity=LogicalModelRef("SaleLine"),
        metrics=(
            Metric(
                operation=MetricOperation.SUM,
                field=LogicalFieldRef("SaleLine.net_amount"),
                alias="net_revenue",
            ),
        ),
    )

    preview = _preview(request)

    assert preview.route is AdvancedQueryRoute.V1
    assert preview.routed_request == request


def test_confirmation_returns_only_the_exact_validated_request() -> None:
    preview = _preview()
    confirmation = AdvancedQueryConfirmation(
        action=AdvancedQueryConfirmationAction.CONFIRM,
        request_digest=preview.request_digest,
        preview_fingerprint=preview.fingerprint,
        routed_request_fingerprint=preview.routed_request_fingerprint,
        token=SignedAdvancedQueryPreviewToken("qsp2." + "A" * 32),
    )

    assert confirm_advanced_query_preview(preview, confirmation) == preview.validated_request
    assert confirmation.fingerprint == advanced_query_confirmation_fingerprint(confirmation)

    altered = confirmation.model_copy(update={"preview_fingerprint": SHA_B})
    with pytest.raises(ValueError, match="preview fingerprint does not match"):
        confirm_advanced_query_preview(preview, altered)
