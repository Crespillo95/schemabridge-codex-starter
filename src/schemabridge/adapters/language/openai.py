"""Legacy typed-intent adapter backed by the managed OpenAI Responses boundary."""

from __future__ import annotations

import hashlib
from typing import cast

from schemabridge.adapters.language.openai_boundary import (
    OpenAIAdapterError,
    OpenAIAdapterErrorCode,
    OpenAIClient,
    OpenAIResponsesBoundary,
    OpenAIResponsesConfig,
    OpenAIStage,
    build_interpretation_provider_input,
    create_managed_openai_client_from_api_key,
)
from schemabridge.application.ports.intents import (
    IntentParserError,
    IntentParserErrorCode,
)
from schemabridge.domain.intents import IntentModelOutput, IntentParseInput

_LEGACY_SAFETY_IDENTIFIER = (
    "sb_ai_v1_" + hashlib.sha256(b"schemabridge-legacy-intent-safety-v1").hexdigest()[:32]
)


class OpenAIIntentParser:
    """Compatibility parser; new M27 workflows use the two stage adapters."""

    def __init__(
        self,
        client: OpenAIClient,
        model: str,
        *,
        metadata_approved_public: bool = False,
    ) -> None:
        try:
            config = OpenAIResponsesConfig.for_model(model)
        except OpenAIAdapterError as error:
            raise _legacy_error(error) from error
        self._boundary = OpenAIResponsesBoundary(client, config)
        self._metadata_approved_public = metadata_approved_public

    @classmethod
    def from_api_key(
        cls,
        api_key: str,
        model: str,
        *,
        metadata_approved_public: bool = False,
    ) -> OpenAIIntentParser:
        try:
            config = OpenAIResponsesConfig.for_model(model)
            client = create_managed_openai_client_from_api_key(api_key, config)
        except OpenAIAdapterError as error:
            raise _legacy_error(error) from error
        return cls(
            client,
            model,
            metadata_approved_public=metadata_approved_public,
        )

    def parse(self, parse_input: IntentParseInput) -> IntentModelOutput:
        vocabulary = parse_input.vocabulary.model_dump(
            mode="json",
            exclude={"context_source", "context_version"},
        )
        metadata_text = tuple(
            [
                *(model.definition for model in parse_input.vocabulary.models),
                *(
                    value
                    for field in parse_input.vocabulary.fields
                    for value in (
                        field.definition,
                        *field.allowed_values,
                    )
                ),
            ]
        )
        try:
            provider_input = build_interpretation_provider_input(
                text=parse_input.text,
                language=parse_input.language.value,
                metadata_payload=vocabulary,
                public_metadata_text=metadata_text,
                metadata_approved_public=self._metadata_approved_public,
            )
            parsed = self._boundary.parse(
                stage=OpenAIStage.LEGACY_INTERPRETATION,
                provider_input=provider_input,
                output_type=IntentModelOutput,
                safety_identifier=_LEGACY_SAFETY_IDENTIFIER,
            )
        except OpenAIAdapterError as error:
            raise _legacy_error(error) from error
        return cast(IntentModelOutput, parsed.value)


def _legacy_error(error: OpenAIAdapterError) -> IntentParserError:
    if error.code is OpenAIAdapterErrorCode.CONFIGURATION_INVALID:
        return IntentParserError(
            IntentParserErrorCode.CONFIGURATION_MISSING,
            "the managed live intent configuration is unavailable",
        )
    if error.code in {
        OpenAIAdapterErrorCode.MODEL_REFUSED,
        OpenAIAdapterErrorCode.MISSING_OUTPUT,
    }:
        return IntentParserError(
            IntentParserErrorCode.MODEL_REFUSED,
            "the live intent model returned no structured interpretation",
        )
    if error.code in {
        OpenAIAdapterErrorCode.INVALID_OUTPUT,
        OpenAIAdapterErrorCode.INPUT_TOO_LARGE,
        OpenAIAdapterErrorCode.PROMPT_TOO_LARGE,
        OpenAIAdapterErrorCode.SENSITIVE_INPUT_BLOCKED,
        OpenAIAdapterErrorCode.METADATA_NOT_PUBLIC,
        OpenAIAdapterErrorCode.SENSITIVE_METADATA_BLOCKED,
    }:
        return IntentParserError(
            IntentParserErrorCode.INVALID_MODEL_OUTPUT,
            "the live intent request or structured output failed validation",
        )
    return IntentParserError(
        IntentParserErrorCode.PROVIDER_UNAVAILABLE,
        "the live intent provider is unavailable",
    )
