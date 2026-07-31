from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import schemabridge.adapters.language.openai_advanced_query_studio as advanced_openai
from schemabridge.adapters.language.openai_advanced_query_studio import (
    OpenAIAdvancedInterpretation,
    OpenAIAdvancedInterpretationAdapter,
    OpenAIAdvancedMentionExtraction,
    OpenAIAdvancedMentionExtractionAdapter,
    OpenAIAdvancedMentionSpan,
    create_openai_advanced_query_studio_adapters_from_environment,
    openai_advanced_interpretation_input_token_reservation_bound,
    openai_advanced_interpretation_input_token_reservation_bound_for,
    openai_advanced_interpretation_response_config_fingerprint,
    openai_advanced_mention_input_token_reservation_bound,
    openai_advanced_mention_input_token_reservation_bound_for,
    openai_advanced_mention_response_config_fingerprint,
    openai_advanced_provider_contract_fingerprint,
    openai_advanced_public_metadata_policy_fingerprint,
)
from schemabridge.adapters.language.openai_boundary import (
    OpenAIResponsesBoundary,
    OpenAIResponsesConfig,
    OpenAIStage,
    derive_safety_identifier,
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
    AdvancedMentionExtraction,
    AdvancedMentionExtractionInput,
    AdvancedMentionPurpose,
    AdvancedNaturalLanguageInput,
    AdvancedQueryMention,
    AdvancedSemanticField,
    AdvancedSemanticModel,
    AdvancedSourceSpan,
)
from schemabridge.domain.advanced_requests import (
    AdvancedAnalyticalRequest,
    AdvancedField,
    AdvancedQueryMode,
    LogicalBooleanPredicate,
    OutputOrder,
)
from schemabridge.domain.concepts import CanonicalType, LogicalFieldRef, LogicalModelRef
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.query_studio import ProviderStage
from schemabridge.domain.request_context import LogicalFieldRole
from schemabridge.domain.requests import Filter, FilterOperator, SortDirection


@dataclass(slots=True)
class _FakeResponse:
    output_parsed: object
    model: object = "gpt-5-nano-2025-08-07"
    output: object = ()
    status: object = "completed"
    incomplete_details: object = None
    usage: object = None


class _ScriptedResponses:
    def __init__(self, *outcomes: _FakeResponse | Exception) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, object]] = []

    def parse(self, **kwargs: object) -> _FakeResponse:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@dataclass(slots=True)
class _FakeClient:
    responses: _ScriptedResponses


class _ProviderStatusError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


RAW_QUERY = "\uff2duestra   productos activos por categoría"
NORMALIZED_QUERY = "Muestra productos activos por categoría"
SEMANTIC_SCOPE_FINGERPRINT = "d" * 64
REGISTRY_FINGERPRINT = "e" * 64
MATCHER_VERSION = "m32-advanced-retrieval-v1"


def _usage() -> object:
    return SimpleNamespace(
        input_tokens=123,
        output_tokens=45,
        output_tokens_details=SimpleNamespace(reasoning_tokens=6),
    )


def _response(value: object) -> _FakeResponse:
    return _FakeResponse(output_parsed=value, usage=_usage())


def _boundary(responses: _ScriptedResponses) -> OpenAIResponsesBoundary:
    return OpenAIResponsesBoundary(
        _FakeClient(responses),
        OpenAIResponsesConfig(),
    )


def _mention_adapter(
    boundary: OpenAIResponsesBoundary,
) -> OpenAIAdvancedMentionExtractionAdapter:
    return OpenAIAdvancedMentionExtractionAdapter(
        boundary,
        safety_identifier=_safety_identifier(),
        matcher_version=MATCHER_VERSION,
        semantic_scope_fingerprint=SEMANTIC_SCOPE_FINGERPRINT,
        public_metadata_registry_fingerprint=REGISTRY_FINGERPRINT,
    )


def _interpretation_adapter(
    boundary: OpenAIResponsesBoundary,
) -> OpenAIAdvancedInterpretationAdapter:
    return OpenAIAdvancedInterpretationAdapter(
        boundary,
        safety_identifier=_safety_identifier(),
        matcher_version=MATCHER_VERSION,
        semantic_scope_fingerprint=SEMANTIC_SCOPE_FINGERPRINT,
        public_metadata_registry_fingerprint=REGISTRY_FINGERPRINT,
    )


def _safety_identifier() -> str:
    return derive_safety_identifier(
        bytes(range(32)),
        workspace_identity="workspace-internal",
        actor_identity="actor-internal",
    )


def _query() -> AdvancedNaturalLanguageInput:
    return AdvancedNaturalLanguageInput(
        text=RAW_QUERY,
        language=UserLanguage.SPANISH,
    )


def _provider_mentions() -> OpenAIAdvancedMentionExtraction:
    return OpenAIAdvancedMentionExtraction(
        mentions=tuple(
            OpenAIAdvancedMentionSpan(
                start=NORMALIZED_QUERY.index(phrase),
                end=NORMALIZED_QUERY.index(phrase) + len(phrase),
                purpose=purpose,
            )
            for phrase, purpose in (
                ("productos", AdvancedMentionPurpose.PRIMARY_ENTITY),
                ("activos", AdvancedMentionPurpose.FILTER),
                ("categoría", AdvancedMentionPurpose.DIMENSION),
            )
        )
    )


def _grounded_extraction(
    query: AdvancedNaturalLanguageInput | None = None,
) -> AdvancedMentionExtraction:
    query = query or _query()
    values = (
        ("productos", AdvancedMentionPurpose.PRIMARY_ENTITY),
        ("activos", AdvancedMentionPurpose.FILTER),
        ("categoría", AdvancedMentionPurpose.DIMENSION),
    )
    return AdvancedMentionExtraction(
        request_digest=query.digest,
        mentions=tuple(
            AdvancedQueryMention(
                value=phrase,
                source_span=AdvancedSourceSpan(
                    start=query.text.index(phrase),
                    end=query.text.index(phrase) + len(phrase),
                ),
                purpose=purpose,
            )
            for phrase, purpose in values
        ),
    )


def _context() -> AdvancedApprovedSemanticContext:
    return AdvancedApprovedSemanticContext(
        context_source="registry:commerce",
        context_version=7,
        approved_context_fingerprint="a" * 64,
        governed_registry_fingerprint=REGISTRY_FINGERPRINT,
        scope_fingerprint=SEMANTIC_SCOPE_FINGERPRINT,
        models=(
            AdvancedSemanticModel(
                id=LogicalModelRef("Product"),
                definition="Producto disponible para venta.",
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
                definition="Categoría comercial del producto.",
            ),
            AdvancedSemanticField(
                id=LogicalFieldRef("Product.lifecycle_status"),
                canonical_type=CanonicalType.STRING,
                role=LogicalFieldRole.ATTRIBUTE,
                definition="Estado comercial normalizado.",
                allowed_values=("ACTIVE", "INACTIVE"),
            ),
        ),
    )


def _request(
    *,
    field: str = "Product.category",
    lifecycle_status: str = "ACTIVE",
) -> AdvancedAnalyticalRequest:
    return AdvancedAnalyticalRequest(
        version=2,
        mode=AdvancedQueryMode.ROWS,
        primary_entity=LogicalModelRef("Product"),
        fields=(
            AdvancedField(
                field=LogicalFieldRef("Product.product_key"),
                alias="product_key",
            ),
            AdvancedField(
                field=LogicalFieldRef(field),
                alias="category",
            ),
        ),
        where=LogicalBooleanPredicate.leaf(
            Filter(
                field=LogicalFieldRef("Product.lifecycle_status"),
                operator=FilterOperator.EQUALS,
                value=lifecycle_status,
            )
        ),
        result_order_by=(
            OutputOrder(alias="category", direction=SortDirection.ASC),
            OutputOrder(alias="product_key", direction=SortDirection.ASC),
        ),
        limit=50,
    )


def _interpretation_input() -> AdvancedInterpretationInput:
    query = _query()
    return AdvancedInterpretationInput(
        query=query,
        extraction=_grounded_extraction(query),
        context=_context(),
    )


def test_mention_adapter_reconstructs_digest_and_exact_raw_source_spans() -> None:
    responses = _ScriptedResponses(_response(_provider_mentions()))
    port: AdvancedMentionExtractionPort = _mention_adapter(_boundary(responses))

    value = AdvancedMentionExtractionInput(query=_query())
    result = port.extract(value)

    assert result.input == value
    assert result.extraction.request_digest == value.query.digest
    assert tuple(item.value for item in result.extraction.mentions) == (
        "productos",
        "activos",
        "categoría",
    )
    assert all(
        value.query.text[item.source_span.start : item.source_span.end] == item.value
        for item in result.extraction.mentions
    )
    assert result.usage is not None
    assert result.usage.stage is ProviderStage.EXPANSION
    assert result.usage.input_tokens == 123
    assert result.usage.output_tokens == 45

    call = responses.calls[0]
    assert call["text_format"] is OpenAIAdvancedMentionExtraction
    assert call["tools"] == ()
    assert call["store"] is False
    provider_input = str(call["input"])
    assert "UNTRUSTED_BUSINESS_TEXT_BEGIN" in provider_input
    assert "UNTRUSTED_BUSINESS_TEXT_END" in provider_input
    assert NORMALIZED_QUERY in provider_input
    assert RAW_QUERY not in provider_input
    assert value.query.digest not in provider_input


def test_provider_output_schemas_are_strict_and_have_no_sql_or_physical_authority() -> None:
    for output_type in (
        OpenAIAdvancedMentionExtraction,
        OpenAIAdvancedInterpretation,
    ):
        schema = json.dumps(output_type.model_json_schema(), sort_keys=True).casefold()
        assert output_type.model_config["strict"] is True
        assert output_type.model_config["extra"] == "forbid"
        assert "sql" not in schema
        assert "physical" not in schema
        assert "request_digest" not in schema
        assert "fingerprint" not in schema

    with pytest.raises(ValidationError):
        OpenAIAdvancedMentionExtraction.model_validate(
            {
                "mentions": [
                    {
                        "start": 0,
                        "end": 1,
                        "purpose": AdvancedMentionPurpose.DIMENSION,
                    }
                ],
                "sql": "SELECT 1",
            }
        )
    with pytest.raises(ValidationError):
        OpenAIAdvancedInterpretation(
            request=_request(),
            sql="SELECT 1",  # type: ignore[call-arg]
        )


@pytest.mark.parametrize(
    "provider_mentions",
    (
        OpenAIAdvancedMentionExtraction(
            mentions=(
                OpenAIAdvancedMentionSpan(
                    start=0,
                    end=10,
                    purpose=AdvancedMentionPurpose.PRIMARY_ENTITY,
                ),
                OpenAIAdvancedMentionSpan(
                    start=9,
                    end=15,
                    purpose=AdvancedMentionPurpose.DIMENSION,
                ),
            )
        ),
        OpenAIAdvancedMentionExtraction(
            mentions=(
                OpenAIAdvancedMentionSpan(
                    start=0,
                    end=200,
                    purpose=AdvancedMentionPurpose.PRIMARY_ENTITY,
                ),
            )
        ),
    ),
)
def test_invalid_or_overlapping_provider_spans_fail_closed(
    provider_mentions: OpenAIAdvancedMentionExtraction,
) -> None:
    responses = _ScriptedResponses(_response(provider_mentions))
    adapter = _mention_adapter(_boundary(responses))

    with pytest.raises(AdvancedQueryStudioPortError) as captured:
        adapter.extract(AdvancedMentionExtractionInput(query=_query()))

    assert captured.value.code is AdvancedQueryStudioPortErrorCode.INVALID_ADAPTER_OUTPUT
    assert RAW_QUERY not in str(captured.value)


def test_interpretation_sends_only_normalized_text_and_public_logical_context() -> None:
    responses = _ScriptedResponses(_response(OpenAIAdvancedInterpretation(request=_request())))
    port: AdvancedInterpretationPort = _interpretation_adapter(_boundary(responses))
    value = _interpretation_input()

    result = port.interpret(value)

    assert result.envelope.request == _request()
    assert result.envelope.ambiguities == ()
    assert result.envelope.request_digest == value.query.digest
    assert result.envelope.mention_fingerprint == value.extraction.fingerprint
    assert result.envelope.semantic_context_fingerprint == value.context.fingerprint
    assert result.usage is not None
    assert result.usage.stage is ProviderStage.INTERPRETATION

    call = responses.calls[0]
    assert call["text_format"] is OpenAIAdvancedInterpretation
    provider_input = str(call["input"])
    assert NORMALIZED_QUERY in provider_input
    assert RAW_QUERY not in provider_input
    assert "UNTRUSTED_CATALOG_METADATA_BEGIN" in provider_input
    assert "UNTRUSTED_CATALOG_METADATA_END" in provider_input
    assert value.context.context_source not in provider_input
    assert value.context.approved_context_fingerprint not in provider_input
    assert value.context.governed_registry_fingerprint not in provider_input
    assert value.context.scope_fingerprint not in provider_input
    assert "physical" not in provider_input.casefold()

    metadata_text = provider_input.split(
        "UNTRUSTED_CATALOG_METADATA_BEGIN\n",
        maxsplit=1,
    )[1].split("\nUNTRUSTED_CATALOG_METADATA_END", maxsplit=1)[0]
    metadata = json.loads(metadata_text)
    assert set(metadata) == {"models", "fields", "joins"}
    assert set(metadata["models"][0]) == {"id", "definition"}
    assert set(metadata["fields"][0]) == {
        "id",
        "canonical_type",
        "role",
        "definition",
        "allowed_values",
    }
    assert metadata["joins"] == []
    assert "mentions" not in metadata
    assert "request_digest" not in metadata


def test_interpretation_accepts_a_closed_ambiguity_without_inventing_bindings() -> None:
    responses = _ScriptedResponses(
        _response(
            OpenAIAdvancedInterpretation(
                ambiguities=(AdvancedInterpretationAmbiguity.FIELD_MEANING,),
            )
        )
    )
    adapter = _interpretation_adapter(_boundary(responses))

    result = adapter.interpret(_interpretation_input())

    assert result.envelope.request is None
    assert result.envelope.ambiguities == (AdvancedInterpretationAmbiguity.FIELD_MEANING,)


@pytest.mark.parametrize(
    ("proposed_request", "secret_fragment"),
    (
        (_request(field="Product.raw_category"), "Product.raw_category"),
        (_request(lifecycle_status="ARCHIVED"), "ARCHIVED"),
    ),
)
def test_hallucinated_field_or_unapproved_value_fails_closed(
    proposed_request: AdvancedAnalyticalRequest,
    secret_fragment: str,
) -> None:
    responses = _ScriptedResponses(
        _response(OpenAIAdvancedInterpretation(request=proposed_request))
    )
    adapter = _interpretation_adapter(_boundary(responses))

    with pytest.raises(AdvancedQueryStudioPortError) as captured:
        adapter.interpret(_interpretation_input())

    assert captured.value.code is AdvancedQueryStudioPortErrorCode.GOVERNED_CONTEXT_MISMATCH
    assert secret_fragment not in str(captured.value)
    assert RAW_QUERY not in str(captured.value)


@pytest.mark.parametrize(
    ("provider_failure", "expected_code"),
    (
        (
            TimeoutError("provider timeout leaked a private deployment detail"),
            AdvancedQueryStudioPortErrorCode.PROVIDER_TIMEOUT,
        ),
        (
            _ProviderStatusError(
                429,
                "provider rate limit leaked a private deployment detail",
            ),
            AdvancedQueryStudioPortErrorCode.PROVIDER_RATE_LIMITED,
        ),
        (
            _ProviderStatusError(
                503,
                "provider unavailable leaked a private deployment detail",
            ),
            AdvancedQueryStudioPortErrorCode.PROVIDER_UNAVAILABLE,
        ),
    ),
)
def test_provider_failure_preserves_typed_code_without_leaking_details(
    provider_failure: Exception,
    expected_code: AdvancedQueryStudioPortErrorCode,
) -> None:
    responses = _ScriptedResponses(provider_failure)
    adapter = _mention_adapter(_boundary(responses))

    with pytest.raises(AdvancedQueryStudioPortError) as captured:
        adapter.extract(AdvancedMentionExtractionInput(query=_query()))

    assert captured.value.code is expected_code
    assert str(provider_failure) not in str(captured.value)


@pytest.mark.parametrize(
    ("provider_response", "expected_code"),
    (
        (
            _FakeResponse(output_parsed=None, usage=_usage()),
            AdvancedQueryStudioPortErrorCode.PROVIDER_MISSING_OUTPUT,
        ),
        (
            _FakeResponse(
                output_parsed=None,
                output=(
                    SimpleNamespace(
                        content=(
                            SimpleNamespace(
                                type="refusal",
                                refusal="private refusal detail",
                            ),
                        )
                    ),
                ),
                usage=_usage(),
            ),
            AdvancedQueryStudioPortErrorCode.PROVIDER_REFUSED,
        ),
        (
            _FakeResponse(
                output_parsed={"mentions": "invalid"},
                usage=_usage(),
            ),
            AdvancedQueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
        ),
        (
            _FakeResponse(
                output_parsed=_provider_mentions(),
                model="unexpected-model",
                usage=_usage(),
            ),
            AdvancedQueryStudioPortErrorCode.PROVIDER_MODEL_MISMATCH,
        ),
    ),
)
def test_non_transient_provider_failures_keep_their_closed_code(
    provider_response: _FakeResponse,
    expected_code: AdvancedQueryStudioPortErrorCode,
) -> None:
    responses = _ScriptedResponses(provider_response)
    adapter = _mention_adapter(_boundary(responses))

    with pytest.raises(AdvancedQueryStudioPortError) as captured:
        adapter.extract(AdvancedMentionExtractionInput(query=_query()))

    assert captured.value.code is expected_code
    assert "private refusal detail" not in str(captured.value)
    assert "unexpected-model" not in str(captured.value)


def test_sensitive_public_metadata_is_blocked_before_the_provider_call() -> None:
    context = _context()
    blocked = AdvancedApprovedSemanticContext(
        **{
            **context.model_dump(),
            "models": (
                AdvancedSemanticModel(
                    id=LogicalModelRef("Product"),
                    definition="Owner email used for restricted notifications.",
                ),
            ),
        }
    )
    value = AdvancedInterpretationInput(
        query=_query(),
        extraction=_grounded_extraction(),
        context=blocked,
    )
    responses = _ScriptedResponses(_response(OpenAIAdvancedInterpretation(request=_request())))
    adapter = _interpretation_adapter(_boundary(responses))

    with pytest.raises(AdvancedQueryStudioPortError) as captured:
        adapter.interpret(value)

    assert captured.value.code is AdvancedQueryStudioPortErrorCode.SENSITIVE_METADATA_BLOCKED
    assert responses.calls == []


@pytest.mark.parametrize(
    ("field", "fingerprint"),
    (
        ("scope_fingerprint", "1" * 64),
        ("governed_registry_fingerprint", "2" * 64),
    ),
)
def test_context_outside_configured_scope_or_registry_never_reaches_provider(
    field: str,
    fingerprint: str,
) -> None:
    context_payload = _context().model_dump()
    context_payload[field] = fingerprint
    context = AdvancedApprovedSemanticContext.model_validate(context_payload)
    value = AdvancedInterpretationInput(
        query=_query(),
        extraction=_grounded_extraction(),
        context=context,
    )
    responses = _ScriptedResponses(_response(OpenAIAdvancedInterpretation(request=_request())))
    adapter = _interpretation_adapter(_boundary(responses))

    with pytest.raises(AdvancedQueryStudioPortError) as captured:
        adapter.interpret(value)

    assert captured.value.code is AdvancedQueryStudioPortErrorCode.GOVERNED_CONTEXT_MISMATCH
    assert fingerprint not in str(captured.value)
    assert responses.calls == []


def test_sensitive_business_text_maps_to_typed_privacy_failure() -> None:
    query = AdvancedNaturalLanguageInput(
        text="Muestra productos con password=secret123",
        language=UserLanguage.SPANISH,
    )
    responses = _ScriptedResponses(_response(_provider_mentions()))
    adapter = _mention_adapter(_boundary(responses))

    with pytest.raises(AdvancedQueryStudioPortError) as captured:
        adapter.extract(AdvancedMentionExtractionInput(query=query))

    assert captured.value.code is AdvancedQueryStudioPortErrorCode.SENSITIVE_INPUT_BLOCKED
    assert "secret123" not in str(captured.value)
    assert responses.calls == []


def test_adapters_use_exact_m32_stages_and_instructions() -> None:
    mention_responses = _ScriptedResponses(_response(_provider_mentions()))
    mention_adapter = _mention_adapter(_boundary(mention_responses))
    mention_adapter.extract(AdvancedMentionExtractionInput(query=_query()))

    interpretation_responses = _ScriptedResponses(
        _response(OpenAIAdvancedInterpretation(request=_request()))
    )
    interpretation_adapter = _interpretation_adapter(_boundary(interpretation_responses))
    interpretation_adapter.interpret(_interpretation_input())

    mention_instructions = str(mention_responses.calls[0]["instructions"])
    interpretation_instructions = str(interpretation_responses.calls[0]["instructions"])
    assert "half-open" in mention_instructions
    assert "Unicode-codepoint offsets" in mention_instructions
    assert "version-2 typed analytical request" in interpretation_instructions
    assert "bounded approved public logical context" in interpretation_instructions
    assert mention_instructions != interpretation_instructions
    assert OpenAIStage.ADVANCED_MENTION_EXTRACTION.value == "advanced_mention_extraction"
    assert OpenAIStage.ADVANCED_INTERPRETATION.value == "advanced_typed_interpretation"
    assert OpenAIStage.EXPANSION.value == "description_expansion"
    assert OpenAIStage.INTERPRETATION.value == "typed_interpretation"


def test_both_adapters_share_one_scope_and_registry_bound_configuration() -> None:
    responses = _ScriptedResponses(
        _response(_provider_mentions()),
        _response(OpenAIAdvancedInterpretation(request=_request())),
    )
    boundary = _boundary(responses)
    mention_adapter = _mention_adapter(boundary)
    interpretation_adapter = _interpretation_adapter(boundary)

    mention_result = mention_adapter.extract(AdvancedMentionExtractionInput(query=_query()))
    interpretation_result = interpretation_adapter.interpret(_interpretation_input())

    assert mention_adapter.configuration == interpretation_adapter.configuration
    configuration = mention_adapter.configuration
    assert configuration.external_ai is True
    assert configuration.public_metadata_semantic_scope_fingerprint == SEMANTIC_SCOPE_FINGERPRINT
    assert configuration.public_metadata_registry_fingerprint == REGISTRY_FINGERPRINT
    assert mention_result.usage is not None
    assert interpretation_result.usage is not None
    assert mention_result.usage.configuration_fingerprint == configuration.fingerprint
    assert interpretation_result.usage.configuration_fingerprint == configuration.fingerprint


def test_response_contracts_and_token_bounds_cover_exact_m32_payloads() -> None:
    config = OpenAIResponsesConfig()
    mention_fingerprint = openai_advanced_mention_response_config_fingerprint(config)
    interpretation_fingerprint = openai_advanced_interpretation_response_config_fingerprint(config)

    assert mention_fingerprint != interpretation_fingerprint
    assert openai_advanced_provider_contract_fingerprint(config) not in {
        mention_fingerprint,
        interpretation_fingerprint,
    }
    assert (
        openai_advanced_mention_input_token_reservation_bound()
        >= openai_advanced_mention_input_token_reservation_bound_for(
            AdvancedMentionExtractionInput(query=_query())
        )
        > 0
    )
    assert (
        openai_advanced_interpretation_input_token_reservation_bound()
        >= openai_advanced_interpretation_input_token_reservation_bound_for(_interpretation_input())
        > 0
    )


def test_public_metadata_policy_changes_with_scope_or_registry() -> None:
    config = OpenAIResponsesConfig()
    baseline = openai_advanced_public_metadata_policy_fingerprint(
        config,
        semantic_scope_fingerprint=SEMANTIC_SCOPE_FINGERPRINT,
        registry_fingerprint=REGISTRY_FINGERPRINT,
    )

    assert baseline != openai_advanced_public_metadata_policy_fingerprint(
        config,
        semantic_scope_fingerprint="f" * 64,
        registry_fingerprint=REGISTRY_FINGERPRINT,
    )
    assert baseline != openai_advanced_public_metadata_policy_fingerprint(
        config,
        semantic_scope_fingerprint=SEMANTIC_SCOPE_FINGERPRINT,
        registry_fingerprint="f" * 64,
    )


def test_environment_factory_is_key_free_under_a_fake_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _ScriptedResponses()
    fake_client = _FakeClient(responses)
    monkeypatch.setattr(
        advanced_openai,
        "create_managed_openai_client_from_environment",
        lambda _config: fake_client,
    )

    mention_adapter, interpretation_adapter = (
        create_openai_advanced_query_studio_adapters_from_environment(
            OpenAIResponsesConfig(),
            safety_identifier=_safety_identifier(),
            matcher_version=MATCHER_VERSION,
            semantic_scope_fingerprint=SEMANTIC_SCOPE_FINGERPRINT,
            public_metadata_registry_fingerprint=REGISTRY_FINGERPRINT,
        )
    )

    assert mention_adapter.configuration == interpretation_adapter.configuration
    assert responses.calls == []


def test_historical_m27_configuration_fingerprints_remain_byte_stable() -> None:
    config = OpenAIResponsesConfig()

    assert config.fingerprint() == (
        "99ca4724023188bf52cf78ba6f57c980d4a51dd0e5976f6b5ef6010072458d2c"
    )
    assert config.stage_fingerprint(OpenAIStage.EXPANSION) == (
        "f7438a5af7e84ee96090ba6884a2d9b3cb456920d82b950547467bcc304104ad"
    )
    assert config.stage_fingerprint(OpenAIStage.INTERPRETATION) == (
        "5f1231bc9acd2dec58bd7b780785a26b80c0c935f512175bca6b5eaa5d4d52d1"
    )
    assert config.stage_fingerprint(OpenAIStage.LEGACY_INTERPRETATION) == (
        "325ca84e5a07ecde0c1935edff91194f8c12bdcfb94a94e1f01f6d44edff5b71"
    )
