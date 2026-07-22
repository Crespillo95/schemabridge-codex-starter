"""Port for bounded natural-language interpretation into typed domain intent."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from schemabridge.domain.intents import IntentModelOutput, IntentParseInput


class IntentParserErrorCode(StrEnum):
    CONFIGURATION_MISSING = "intent_configuration_missing"
    PROVIDER_UNAVAILABLE = "intent_provider_unavailable"
    MODEL_REFUSED = "intent_model_refused"
    INVALID_MODEL_OUTPUT = "invalid_intent_model_output"


class IntentParserError(RuntimeError):
    """Sanitized parser failure that never exposes prompts, credentials, or provider payloads."""

    def __init__(self, code: IntentParserErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class IntentParserPort(Protocol):
    def parse(self, parse_input: IntentParseInput) -> IntentModelOutput:
        """Interpret untrusted business text through the supplied bounded vocabulary."""
