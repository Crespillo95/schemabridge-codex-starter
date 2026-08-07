"""Strict OpenAI adapters for the bounded M32 natural-language contracts.

The provider sees only screened business text and the approved logical context.
It never receives physical identifiers, SQL, registry fingerprints, or mutation
authority. Server code reconstructs every digest and validates provider output
against the exact input and governed context.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Annotated, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, Strict, ValidationError, model_validator

from schemabridge.adapters.language.openai_boundary import (
    OpenAIAdapterError,
    OpenAIAdapterErrorCode,
    OpenAIParsedOutput,
    OpenAIResponsesBoundary,
    OpenAIResponsesConfig,
    OpenAIStage,
    build_interpretation_provider_input,
    create_managed_openai_client_from_environment,
    normalize_and_screen_public_metadata,
    normalize_and_screen_user_text,
    provider_input_token_reservation_bound,
    provider_input_token_reservation_bound_for_payload,
    response_config_fingerprint,
    validate_safety_identifier,
)
from schemabridge.application.ports.advanced_query_studio import (
    AdvancedQueryStudioPortError,
    AdvancedQueryStudioPortErrorCode,
)
from schemabridge.domain.advanced_query_studio import (
    MAX_ADVANCED_MENTIONS,
    AdvancedInterpretationAmbiguity,
    AdvancedInterpretationEnvelope,
    AdvancedInterpretationInput,
    AdvancedInterpretationResult,
    AdvancedMentionExtraction,
    AdvancedMentionExtractionInput,
    AdvancedMentionExtractionResult,
    AdvancedMentionPurpose,
    AdvancedQueryMention,
    AdvancedSourceSpan,
)
from schemabridge.domain.advanced_requests import AdvancedAnalyticalRequest
from schemabridge.domain.query_studio import (
    EXTERNAL_AI_ATTEMPT_POLICY_VERSION,
    QUERY_STUDIO_PUBLIC_METADATA_POLICY_VERSION,
    ProviderConfigurationFacts,
    ProviderOutcomeCode,
    ProviderStage,
    ProviderUsageFacts,
    query_studio_fingerprint,
)


class OpenAIAdvancedMentionSpan(BaseModel):
    """One provider-selected half-open span in normalized business text."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    start: int = Field(ge=0, lt=2_000)
    end: int = Field(gt=0, le=2_000)
    purpose: AdvancedMentionPurpose

    @model_validator(mode="after")
    def span_must_be_nonempty(self) -> Self:
        if self.end <= self.start:
            raise ValueError("advanced provider mention span must be nonempty")
        return self


class OpenAIAdvancedMentionExtraction(BaseModel):
    """Provider output containing spans only; values and bindings are server-owned."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mentions: tuple[OpenAIAdvancedMentionSpan, ...] = Field(
        min_length=1,
        max_length=MAX_ADVANCED_MENTIONS,
    )


class OpenAIAdvancedInterpretation(BaseModel):
    """Provider output containing exactly one typed request or closed ambiguities."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    request: Annotated[AdvancedAnalyticalRequest, Strict()] | None = None
    ambiguities: tuple[AdvancedInterpretationAmbiguity, ...] = Field(
        default=(),
        max_length=12,
    )

    @model_validator(mode="after")
    def exactly_one_outcome_must_be_present(self) -> Self:
        if (self.request is None) == (not self.ambiguities):
            raise ValueError("advanced provider interpretation requires one request or ambiguities")
        if len(self.ambiguities) != len(set(self.ambiguities)):
            raise ValueError("advanced provider ambiguities must be unique")
        return self


_ProviderOutputT = TypeVar("_ProviderOutputT", bound=BaseModel)
_ADAPTER_LABEL = "openai_responses_advanced_structured"
_M32_ORCHESTRATION_POLICY_VERSION = "m32-natural-sql-orchestration-v1"
_M32_PROVIDER_CONTRACT_VERSION = "m32-openai-provider-contract-v1"
_M32_PUBLIC_METADATA_POLICY_VERSION = "m32-approved-public-context-v1"


def openai_advanced_mention_response_config_fingerprint(
    config: OpenAIResponsesConfig,
) -> str:
    """Bind the exact managed config, M32 mention instructions, and strict schema."""

    return response_config_fingerprint(
        config,
        stage=OpenAIStage.ADVANCED_MENTION_EXTRACTION,
        output_type=OpenAIAdvancedMentionExtraction,
    )


def openai_advanced_interpretation_response_config_fingerprint(
    config: OpenAIResponsesConfig,
) -> str:
    """Bind the exact managed config, M32 interpretation instructions, and schema."""

    return response_config_fingerprint(
        config,
        stage=OpenAIStage.ADVANCED_INTERPRETATION,
        output_type=OpenAIAdvancedInterpretation,
    )


def openai_advanced_provider_contract_fingerprint(
    config: OpenAIResponsesConfig,
) -> str:
    """Return one common provider-contract identity for both M32 stages."""

    return query_studio_fingerprint(
        {
            "kind": _M32_PROVIDER_CONTRACT_VERSION,
            "mention_response_config_fingerprint": (
                openai_advanced_mention_response_config_fingerprint(config)
            ),
            "interpretation_response_config_fingerprint": (
                openai_advanced_interpretation_response_config_fingerprint(config)
            ),
        }
    )


def openai_advanced_public_metadata_policy_fingerprint(
    config: OpenAIResponsesConfig,
    *,
    semantic_scope_fingerprint: str,
    registry_fingerprint: str,
) -> str:
    """Bind public-context egress to one exact governed scope and registry."""

    _require_sha256(semantic_scope_fingerprint)
    _require_sha256(registry_fingerprint)
    return query_studio_fingerprint(
        {
            "kind": _M32_PUBLIC_METADATA_POLICY_VERSION,
            "base_policy": QUERY_STUDIO_PUBLIC_METADATA_POLICY_VERSION,
            "provider_contract_fingerprint": (
                openai_advanced_provider_contract_fingerprint(config)
            ),
            "semantic_scope_fingerprint": semantic_scope_fingerprint,
            "registry_fingerprint": registry_fingerprint,
            "maximum_models": 3,
            "maximum_fields": 12,
            "maximum_joins": 2,
            "physical_identifiers": False,
        }
    )


def openai_advanced_mention_input_token_reservation_bound() -> int:
    """Reserve against the maximum admitted M32 mention prompt and schema."""

    return provider_input_token_reservation_bound(
        stage=OpenAIStage.ADVANCED_MENTION_EXTRACTION,
        output_type=OpenAIAdvancedMentionExtraction,
    )


def openai_advanced_mention_input_token_reservation_bound_for(
    value: AdvancedMentionExtractionInput,
) -> int:
    """Reserve against one exact screened M32 mention payload."""

    if not isinstance(value, AdvancedMentionExtractionInput):
        raise OpenAIAdapterError(OpenAIAdapterErrorCode.CONFIGURATION_INVALID)
    normalized_text = normalize_and_screen_user_text(value.query.text)
    return provider_input_token_reservation_bound_for_payload(
        stage=OpenAIStage.ADVANCED_MENTION_EXTRACTION,
        output_type=OpenAIAdvancedMentionExtraction,
        provider_input=_mention_provider_input(
            normalized_text=normalized_text,
            language=value.query.language.value,
        ),
    )


def openai_advanced_interpretation_input_token_reservation_bound() -> int:
    """Reserve against the maximum admitted M32 interpretation prompt and schema."""

    return provider_input_token_reservation_bound(
        stage=OpenAIStage.ADVANCED_INTERPRETATION,
        output_type=OpenAIAdvancedInterpretation,
    )


def openai_advanced_interpretation_input_token_reservation_bound_for(
    value: AdvancedInterpretationInput,
) -> int:
    """Reserve against one exact screened M32 interpretation payload."""

    if not isinstance(value, AdvancedInterpretationInput):
        raise OpenAIAdapterError(OpenAIAdapterErrorCode.CONFIGURATION_INVALID)
    normalized_text = normalize_and_screen_user_text(value.query.text)
    public_context, public_text = _public_context_payload(value)
    provider_input = build_interpretation_provider_input(
        text=normalized_text,
        language=value.query.language.value,
        metadata_payload=public_context,
        public_metadata_text=public_text,
        metadata_approved_public=True,
    )
    return provider_input_token_reservation_bound_for_payload(
        stage=OpenAIStage.ADVANCED_INTERPRETATION,
        output_type=OpenAIAdvancedInterpretation,
        provider_input=provider_input,
    )


class OpenAIAdvancedMentionExtractionAdapter:
    """Extract source-grounded mentions without catalog or SQL authority."""

    def __init__(
        self,
        boundary: OpenAIResponsesBoundary,
        *,
        safety_identifier: str,
        matcher_version: str,
        semantic_scope_fingerprint: str,
        public_metadata_registry_fingerprint: str,
    ) -> None:
        try:
            validate_safety_identifier(safety_identifier)
            _require_application_owned_retry(boundary)
            configuration = _configuration_facts(
                boundary,
                matcher_version=matcher_version,
                semantic_scope_fingerprint=semantic_scope_fingerprint,
                public_metadata_registry_fingerprint=(public_metadata_registry_fingerprint),
            )
        except OpenAIAdapterError as error:
            raise _port_error(error) from None
        except (ValidationError, TypeError, ValueError):
            raise AdvancedQueryStudioPortError(
                AdvancedQueryStudioPortErrorCode.INVALID_ADAPTER_OUTPUT,
                "advanced language provider configuration failed validation",
            ) from None
        self._boundary = boundary
        self._safety_identifier = safety_identifier
        self._configuration = configuration

    @property
    def configuration(self) -> ProviderConfigurationFacts:
        return self._configuration

    def extract(
        self,
        value: AdvancedMentionExtractionInput,
    ) -> AdvancedMentionExtractionResult:
        if not isinstance(value, AdvancedMentionExtractionInput):
            raise AdvancedQueryStudioPortError(
                AdvancedQueryStudioPortErrorCode.INPUT_MISMATCH,
                "advanced mention extraction received an invalid input contract",
            )
        try:
            normalized_text = normalize_and_screen_user_text(value.query.text)
            provider_input = _mention_provider_input(
                normalized_text=normalized_text,
                language=value.query.language.value,
            )
            parsed = self._boundary.parse(
                stage=OpenAIStage.ADVANCED_MENTION_EXTRACTION,
                provider_input=provider_input,
                output_type=OpenAIAdvancedMentionExtraction,
                safety_identifier=self._safety_identifier,
            )
            provider_output = _require_parsed_type(
                parsed,
                OpenAIAdvancedMentionExtraction,
            )
            extraction = AdvancedMentionExtraction(
                request_digest=value.query.digest,
                mentions=_reconstruct_mentions(
                    source_text=value.query.text,
                    normalized_text=normalized_text,
                    provider_mentions=provider_output.mentions,
                ),
            )
            return AdvancedMentionExtractionResult(
                input=value,
                extraction=extraction,
                usage=_successful_usage(
                    boundary=self._boundary,
                    configuration=self._configuration,
                    openai_stage=OpenAIStage.ADVANCED_MENTION_EXTRACTION,
                    provider_stage=ProviderStage.EXPANSION,
                    parsed=parsed,
                ),
            )
        except OpenAIAdapterError as error:
            raise _port_error(error) from None
        except AdvancedQueryStudioPortError:
            raise
        except (ValidationError, TypeError, ValueError):
            raise AdvancedQueryStudioPortError(
                AdvancedQueryStudioPortErrorCode.INVALID_ADAPTER_OUTPUT,
                "advanced mention extraction failed closed validation",
            ) from None


class OpenAIAdvancedInterpretationAdapter:
    """Interpret only one approved public logical closure into typed intent."""

    def __init__(
        self,
        boundary: OpenAIResponsesBoundary,
        *,
        safety_identifier: str,
        matcher_version: str,
        semantic_scope_fingerprint: str,
        public_metadata_registry_fingerprint: str,
    ) -> None:
        try:
            validate_safety_identifier(safety_identifier)
            _require_application_owned_retry(boundary)
            configuration = _configuration_facts(
                boundary,
                matcher_version=matcher_version,
                semantic_scope_fingerprint=semantic_scope_fingerprint,
                public_metadata_registry_fingerprint=(public_metadata_registry_fingerprint),
            )
        except OpenAIAdapterError as error:
            raise _port_error(error) from None
        except (ValidationError, TypeError, ValueError):
            raise AdvancedQueryStudioPortError(
                AdvancedQueryStudioPortErrorCode.INVALID_ADAPTER_OUTPUT,
                "advanced language provider configuration failed validation",
            ) from None
        self._boundary = boundary
        self._safety_identifier = safety_identifier
        self._configuration = configuration

    @property
    def configuration(self) -> ProviderConfigurationFacts:
        return self._configuration

    def interpret(
        self,
        value: AdvancedInterpretationInput,
    ) -> AdvancedInterpretationResult:
        if not isinstance(value, AdvancedInterpretationInput):
            raise AdvancedQueryStudioPortError(
                AdvancedQueryStudioPortErrorCode.INPUT_MISMATCH,
                "advanced interpretation received an invalid input contract",
            )
        try:
            _require_context_matches_configuration(
                value,
                configuration=self._configuration,
            )
            normalized_text = normalize_and_screen_user_text(value.query.text)
            public_context, public_text = _public_context_payload(value)
            provider_input = build_interpretation_provider_input(
                text=normalized_text,
                language=value.query.language.value,
                metadata_payload=public_context,
                public_metadata_text=public_text,
                metadata_approved_public=True,
            )
            parsed = self._boundary.parse(
                stage=OpenAIStage.ADVANCED_INTERPRETATION,
                provider_input=provider_input,
                output_type=OpenAIAdvancedInterpretation,
                safety_identifier=self._safety_identifier,
            )
            provider_output = _require_parsed_type(
                parsed,
                OpenAIAdvancedInterpretation,
            )
            envelope = AdvancedInterpretationEnvelope(
                request_digest=value.query.digest,
                mention_fingerprint=value.extraction.fingerprint,
                semantic_context_fingerprint=value.context.fingerprint,
                request=(
                    None
                    if provider_output.request is None
                    else AdvancedAnalyticalRequest.model_validate(
                        provider_output.request.model_dump(),
                        strict=True,
                    )
                ),
                ambiguities=provider_output.ambiguities,
            )
            usage = _successful_usage(
                boundary=self._boundary,
                configuration=self._configuration,
                openai_stage=OpenAIStage.ADVANCED_INTERPRETATION,
                provider_stage=ProviderStage.INTERPRETATION,
                parsed=parsed,
            )
        except OpenAIAdapterError as error:
            raise _port_error(error) from None
        except AdvancedQueryStudioPortError:
            raise
        except (ValidationError, TypeError, ValueError):
            raise AdvancedQueryStudioPortError(
                AdvancedQueryStudioPortErrorCode.INVALID_ADAPTER_OUTPUT,
                "advanced interpretation output failed closed validation",
            ) from None

        try:
            return AdvancedInterpretationResult(
                input=value,
                envelope=envelope,
                usage=usage,
            )
        except (ValidationError, TypeError, ValueError):
            raise AdvancedQueryStudioPortError(
                AdvancedQueryStudioPortErrorCode.GOVERNED_CONTEXT_MISMATCH,
                "advanced interpretation is incompatible with approved semantic context",
            ) from None


def create_openai_advanced_query_studio_adapters_from_environment(
    config: OpenAIResponsesConfig,
    *,
    safety_identifier: str,
    matcher_version: str,
    semantic_scope_fingerprint: str,
    public_metadata_registry_fingerprint: str,
) -> tuple[
    OpenAIAdvancedMentionExtractionAdapter,
    OpenAIAdvancedInterpretationAdapter,
]:
    """Compose both M32 live stages over one managed client and common policy."""

    try:
        client = create_managed_openai_client_from_environment(config)
        boundary = OpenAIResponsesBoundary(client, config)
    except OpenAIAdapterError as error:
        raise _port_error(error) from None
    common = {
        "safety_identifier": safety_identifier,
        "matcher_version": matcher_version,
        "semantic_scope_fingerprint": semantic_scope_fingerprint,
        "public_metadata_registry_fingerprint": (public_metadata_registry_fingerprint),
    }
    return (
        OpenAIAdvancedMentionExtractionAdapter(boundary, **common),
        OpenAIAdvancedInterpretationAdapter(boundary, **common),
    )


@dataclass(frozen=True, slots=True)
class _NormalizedCharacter:
    value: str
    source_start: int
    source_end: int


def _mention_provider_input(*, normalized_text: str, language: str) -> str:
    if language not in {"es", "en"}:
        raise OpenAIAdapterError(OpenAIAdapterErrorCode.CONFIGURATION_INVALID)
    return (
        f"CURRENT_USER_LANGUAGE: {language}\n"
        "UNTRUSTED_BUSINESS_TEXT_BEGIN\n"
        f"{normalized_text}\n"
        "UNTRUSTED_BUSINESS_TEXT_END"
    )


def _reconstruct_mentions(
    *,
    source_text: str,
    normalized_text: str,
    provider_mentions: tuple[OpenAIAdvancedMentionSpan, ...],
) -> tuple[AdvancedQueryMention, ...]:
    ordered = tuple(
        sorted(
            provider_mentions,
            key=lambda item: (item.start, item.end, item.purpose.value),
        )
    )
    previous_end = -1
    for mention in ordered:
        if mention.end > len(normalized_text):
            raise ValueError("provider mention exceeds normalized source text")
        if mention.start < previous_end:
            raise ValueError("provider mentions cannot overlap")
        value = normalized_text[mention.start : mention.end]
        if not value or value != value.strip():
            raise ValueError("provider mention must select one exact nonblank span")
        previous_end = mention.end

    normalized_characters = _normalized_characters(source_text)
    if "".join(item.value for item in normalized_characters) != normalized_text:
        raise ValueError("source normalization could not be mapped exactly")

    reconstructed: list[AdvancedQueryMention] = []
    for mention in ordered:
        first = normalized_characters[mention.start]
        last = normalized_characters[mention.end - 1]
        exact_value = source_text[first.source_start : last.source_end]
        if (
            normalize_and_screen_user_text(exact_value)
            != normalized_text[mention.start : mention.end]
        ):
            raise ValueError("provider mention cannot be grounded in exact source bytes")
        reconstructed.append(
            AdvancedQueryMention(
                value=exact_value,
                source_span=AdvancedSourceSpan(
                    start=first.source_start,
                    end=last.source_end,
                ),
                purpose=mention.purpose,
            )
        )
    return tuple(reconstructed)


def _normalized_characters(source_text: str) -> tuple[_NormalizedCharacter, ...]:
    nfkc_characters: list[_NormalizedCharacter] = []
    index = 0
    while index < len(source_text):
        segment_start = index
        index += 1
        while index < len(source_text) and unicodedata.combining(source_text[index]):
            index += 1
        normalized_segment = unicodedata.normalize(
            "NFKC",
            source_text[segment_start:index],
        )
        nfkc_characters.extend(
            _NormalizedCharacter(
                value=character,
                source_start=segment_start,
                source_end=index,
            )
            for character in normalized_segment
        )
    if "".join(item.value for item in nfkc_characters) != unicodedata.normalize(
        "NFKC",
        source_text,
    ):
        raise ValueError("unsupported context-sensitive Unicode normalization")

    token_ranges: list[tuple[int, int]] = []
    cursor = 0
    while cursor < len(nfkc_characters):
        while cursor < len(nfkc_characters) and nfkc_characters[cursor].value.isspace():
            cursor += 1
        if cursor == len(nfkc_characters):
            break
        token_start = cursor
        while cursor < len(nfkc_characters) and not nfkc_characters[cursor].value.isspace():
            cursor += 1
        token_ranges.append((token_start, cursor))

    result: list[_NormalizedCharacter] = []
    for token_index, (token_start, token_end) in enumerate(token_ranges):
        if token_index:
            previous_end = token_ranges[token_index - 1][1]
            result.append(
                _NormalizedCharacter(
                    value=" ",
                    source_start=nfkc_characters[previous_end - 1].source_end,
                    source_end=nfkc_characters[token_start].source_start,
                )
            )
        result.extend(nfkc_characters[token_start:token_end])
    return tuple(result)


def _public_context_payload(
    value: AdvancedInterpretationInput,
) -> tuple[dict[str, object], tuple[str, ...]]:
    context = value.context
    public_text: list[str] = []

    def screened(text: str, *, exact: bool = False) -> str:
        normalized = normalize_and_screen_public_metadata(
            text,
            approved_public=True,
        )
        if exact and normalized != text:
            raise OpenAIAdapterError(OpenAIAdapterErrorCode.METADATA_NOT_PUBLIC)
        public_text.append(normalized)
        return normalized

    payload: dict[str, object] = {
        "models": [
            {
                "id": screened(item.id.root, exact=True),
                "definition": screened(item.definition),
            }
            for item in context.models
        ],
        "fields": [
            {
                "id": screened(item.id.root, exact=True),
                "canonical_type": item.canonical_type.value,
                "role": item.role.value,
                "definition": screened(item.definition),
                "allowed_values": [
                    screened(allowed_value, exact=True) for allowed_value in item.allowed_values
                ],
            }
            for item in context.fields
        ],
        "joins": [
            {
                "id": screened(item.id, exact=True),
                "left_model": screened(item.left_model.root, exact=True),
                "right_model": screened(item.right_model.root, exact=True),
                "cardinality": item.cardinality.value,
                "fanout_policy": item.fanout_policy.value,
            }
            for item in context.joins
        ],
    }
    return payload, tuple(public_text)


def _configuration_facts(
    boundary: OpenAIResponsesBoundary,
    *,
    matcher_version: str,
    semantic_scope_fingerprint: str,
    public_metadata_registry_fingerprint: str,
) -> ProviderConfigurationFacts:
    config = boundary.config
    _require_sha256(semantic_scope_fingerprint)
    _require_sha256(public_metadata_registry_fingerprint)
    provider_contract_fingerprint = openai_advanced_provider_contract_fingerprint(config)
    managed_config_fingerprint = query_studio_fingerprint(
        {
            "kind": "m32-openai-managed-config-v1",
            "mention_stage_config_fingerprint": config.fingerprint(
                stage=OpenAIStage.ADVANCED_MENTION_EXTRACTION
            ),
            "interpretation_stage_config_fingerprint": config.fingerprint(
                stage=OpenAIStage.ADVANCED_INTERPRETATION
            ),
        }
    )
    return ProviderConfigurationFacts.create(
        adapter=_ADAPTER_LABEL,
        model_snapshot=config.model.value,
        reasoning_effort=config.reasoning_effort.value,
        endpoint_region=config.region.value,
        prompt_version=config.prompt_version,
        schema_version=config.schema_version,
        matcher_version=matcher_version,
        orchestration_policy_version=_M32_ORCHESTRATION_POLICY_VERSION,
        attempt_policy_version=EXTERNAL_AI_ATTEMPT_POLICY_VERSION,
        managed_config_fingerprint=managed_config_fingerprint,
        provider_contract_fingerprint=provider_contract_fingerprint,
        public_metadata_policy_fingerprint=(
            openai_advanced_public_metadata_policy_fingerprint(
                config,
                semantic_scope_fingerprint=semantic_scope_fingerprint,
                registry_fingerprint=public_metadata_registry_fingerprint,
            )
        ),
        public_metadata_semantic_scope_fingerprint=(semantic_scope_fingerprint),
        public_metadata_registry_fingerprint=(public_metadata_registry_fingerprint),
        external_ai=True,
    )


def _require_context_matches_configuration(
    value: AdvancedInterpretationInput,
    *,
    configuration: ProviderConfigurationFacts,
) -> None:
    if (
        value.context.scope_fingerprint != configuration.public_metadata_semantic_scope_fingerprint
        or value.context.governed_registry_fingerprint
        != configuration.public_metadata_registry_fingerprint
    ):
        raise AdvancedQueryStudioPortError(
            AdvancedQueryStudioPortErrorCode.GOVERNED_CONTEXT_MISMATCH,
            "approved semantic context differs from the configured governed scope",
        )


def _require_application_owned_retry(boundary: OpenAIResponsesBoundary) -> None:
    if boundary.config.max_transient_retries != 0:
        raise OpenAIAdapterError(OpenAIAdapterErrorCode.CONFIGURATION_INVALID)


def _require_sha256(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise OpenAIAdapterError(OpenAIAdapterErrorCode.CONFIGURATION_INVALID)


def _successful_usage(
    *,
    boundary: OpenAIResponsesBoundary,
    configuration: ProviderConfigurationFacts,
    openai_stage: OpenAIStage,
    provider_stage: ProviderStage,
    parsed: OpenAIParsedOutput,
) -> ProviderUsageFacts:
    if parsed.usage.input_tokens is None or parsed.usage.output_tokens is None:
        raise ValueError("provider usage is incomplete")
    if parsed.model_snapshot != boundary.config.model.value:
        raise OpenAIAdapterError(OpenAIAdapterErrorCode.MODEL_MISMATCH)
    if parsed.config_fingerprint != boundary.config.fingerprint(stage=openai_stage):
        raise OpenAIAdapterError(OpenAIAdapterErrorCode.CONFIGURATION_INVALID)
    return ProviderUsageFacts(
        stage=provider_stage,
        model_snapshot=parsed.model_snapshot,
        configuration_fingerprint=configuration.fingerprint,
        input_tokens=parsed.usage.input_tokens,
        output_tokens=parsed.usage.output_tokens,
        duration_ms=parsed.duration_ms,
        outcome=ProviderOutcomeCode.SUCCEEDED,
    )


def _require_parsed_type(
    parsed: OpenAIParsedOutput,
    output_type: type[_ProviderOutputT],
) -> _ProviderOutputT:
    if not isinstance(parsed.value, output_type):
        raise ValueError("provider returned another structured output type")
    return parsed.value


def _port_error(error: OpenAIAdapterError) -> AdvancedQueryStudioPortError:
    code = _PORT_ERROR_CODES.get(
        error.code,
        AdvancedQueryStudioPortErrorCode.PROVIDER_UNAVAILABLE,
    )
    return AdvancedQueryStudioPortError(
        code,
        _PORT_ERROR_MESSAGES[code],
    )


_PORT_ERROR_CODES = {
    OpenAIAdapterErrorCode.CONFIGURATION_INVALID: (
        AdvancedQueryStudioPortErrorCode.PROVIDER_UNAVAILABLE
    ),
    OpenAIAdapterErrorCode.INPUT_TOO_LARGE: (
        AdvancedQueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT
    ),
    OpenAIAdapterErrorCode.PROMPT_TOO_LARGE: (
        AdvancedQueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT
    ),
    OpenAIAdapterErrorCode.SENSITIVE_INPUT_BLOCKED: (
        AdvancedQueryStudioPortErrorCode.SENSITIVE_INPUT_BLOCKED
    ),
    OpenAIAdapterErrorCode.METADATA_NOT_PUBLIC: (
        AdvancedQueryStudioPortErrorCode.SENSITIVE_METADATA_BLOCKED
    ),
    OpenAIAdapterErrorCode.SENSITIVE_METADATA_BLOCKED: (
        AdvancedQueryStudioPortErrorCode.SENSITIVE_METADATA_BLOCKED
    ),
    OpenAIAdapterErrorCode.PROVIDER_TIMEOUT: (AdvancedQueryStudioPortErrorCode.PROVIDER_TIMEOUT),
    OpenAIAdapterErrorCode.PROVIDER_RATE_LIMITED: (
        AdvancedQueryStudioPortErrorCode.PROVIDER_RATE_LIMITED
    ),
    OpenAIAdapterErrorCode.PROVIDER_UNAVAILABLE: (
        AdvancedQueryStudioPortErrorCode.PROVIDER_UNAVAILABLE
    ),
    OpenAIAdapterErrorCode.PROVIDER_REJECTED: (
        AdvancedQueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT
    ),
    OpenAIAdapterErrorCode.MODEL_REFUSED: (AdvancedQueryStudioPortErrorCode.PROVIDER_REFUSED),
    OpenAIAdapterErrorCode.MISSING_OUTPUT: (
        AdvancedQueryStudioPortErrorCode.PROVIDER_MISSING_OUTPUT
    ),
    OpenAIAdapterErrorCode.INVALID_OUTPUT: (
        AdvancedQueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT
    ),
    OpenAIAdapterErrorCode.MODEL_MISMATCH: (
        AdvancedQueryStudioPortErrorCode.PROVIDER_MODEL_MISMATCH
    ),
}
_PORT_ERROR_MESSAGES = {
    AdvancedQueryStudioPortErrorCode.PROVIDER_UNAVAILABLE: (
        "the live advanced AI provider is unavailable"
    ),
    AdvancedQueryStudioPortErrorCode.PROVIDER_RATE_LIMITED: (
        "the live advanced AI provider rate limit was reached"
    ),
    AdvancedQueryStudioPortErrorCode.PROVIDER_TIMEOUT: ("the live advanced AI provider timed out"),
    AdvancedQueryStudioPortErrorCode.PROVIDER_REFUSED: (
        "the live advanced AI provider refused the bounded request"
    ),
    AdvancedQueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT: (
        "the live advanced AI request or structured output failed validation"
    ),
    AdvancedQueryStudioPortErrorCode.PROVIDER_MISSING_OUTPUT: (
        "the live advanced AI provider returned no structured output"
    ),
    AdvancedQueryStudioPortErrorCode.PROVIDER_MODEL_MISMATCH: (
        "the live advanced AI provider returned an unexpected model snapshot"
    ),
    AdvancedQueryStudioPortErrorCode.SENSITIVE_INPUT_BLOCKED: (
        "live advanced AI was blocked by the business-text privacy screen"
    ),
    AdvancedQueryStudioPortErrorCode.SENSITIVE_METADATA_BLOCKED: (
        "live advanced AI was blocked by the public-metadata privacy screen"
    ),
}


__all__ = [
    "OpenAIAdvancedInterpretation",
    "OpenAIAdvancedInterpretationAdapter",
    "OpenAIAdvancedMentionExtraction",
    "OpenAIAdvancedMentionExtractionAdapter",
    "OpenAIAdvancedMentionSpan",
    "create_openai_advanced_query_studio_adapters_from_environment",
    "openai_advanced_interpretation_input_token_reservation_bound",
    "openai_advanced_interpretation_input_token_reservation_bound_for",
    "openai_advanced_interpretation_response_config_fingerprint",
    "openai_advanced_mention_input_token_reservation_bound",
    "openai_advanced_mention_input_token_reservation_bound_for",
    "openai_advanced_mention_response_config_fingerprint",
    "openai_advanced_provider_contract_fingerprint",
    "openai_advanced_public_metadata_policy_fingerprint",
]
