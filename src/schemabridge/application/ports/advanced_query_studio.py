"""Application ports for M32 governed natural-language interpretation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from schemabridge.domain.advanced_query_studio import (
    AdvancedInterpretationInput,
    AdvancedInterpretationResult,
    AdvancedMentionExtraction,
    AdvancedMentionExtractionInput,
    AdvancedMentionExtractionResult,
    AdvancedNaturalLanguageInput,
    AdvancedPreviewTokenClaims,
    SignedAdvancedQueryPreviewToken,
)
from schemabridge.domain.concepts import LogicalFieldRef
from schemabridge.domain.semantic_registry import GovernedSemanticRegistrySnapshot

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AdvancedQueryStudioPortErrorCode(StrEnum):
    """Closed, sanitized failures exposed by an M32 language adapter."""

    UNSUPPORTED_INPUT = "advanced_query_studio_unsupported_input"
    INPUT_MISMATCH = "advanced_query_studio_input_mismatch"
    GOVERNED_CONTEXT_MISMATCH = "advanced_query_studio_governed_context_mismatch"
    INVALID_ADAPTER_OUTPUT = "advanced_query_studio_invalid_adapter_output"
    SENSITIVE_INPUT_BLOCKED = "advanced_query_studio_sensitive_input_blocked"
    SENSITIVE_METADATA_BLOCKED = "advanced_query_studio_sensitive_metadata_blocked"
    PROVIDER_TIMEOUT = "advanced_query_studio_provider_timeout"
    PROVIDER_RATE_LIMITED = "advanced_query_studio_provider_rate_limited"
    PROVIDER_QUOTA_EXHAUSTED = "advanced_query_studio_provider_quota_exhausted"
    PROVIDER_UNAVAILABLE = "advanced_query_studio_provider_unavailable"
    PROVIDER_REFUSED = "advanced_query_studio_provider_refused"
    PROVIDER_MISSING_OUTPUT = "advanced_query_studio_provider_missing_output"
    PROVIDER_INVALID_OUTPUT = "advanced_query_studio_provider_invalid_output"
    PROVIDER_MODEL_MISMATCH = "advanced_query_studio_provider_model_mismatch"
    RETRIEVAL_UNAVAILABLE = "advanced_query_studio_retrieval_unavailable"
    RETRIEVAL_AMBIGUOUS = "advanced_query_studio_retrieval_ambiguous"
    RETRIEVAL_INVALID = "advanced_query_studio_retrieval_invalid"
    TOKEN_INVALID = "advanced_query_studio_token_invalid"
    TOKEN_EXPIRED = "advanced_query_studio_token_expired"


class AdvancedQueryStudioPortError(RuntimeError):
    """Failure without query text, provider payloads, or governed metadata."""

    def __init__(
        self,
        code: AdvancedQueryStudioPortErrorCode,
        message: str,
    ) -> None:
        if not isinstance(code, AdvancedQueryStudioPortErrorCode):
            raise TypeError("advanced Query Studio port error code is invalid")
        if not message.strip():
            raise ValueError("advanced Query Studio port error message must not be blank")
        self.code = code
        super().__init__(message)


class AdvancedMentionExtractionPort(Protocol):
    """Extract bounded, source-grounded business mentions without schema authority."""

    def extract(
        self,
        value: AdvancedMentionExtractionInput,
    ) -> AdvancedMentionExtractionResult:
        """Return only the exact M32 mention-extraction result contract."""


class AdvancedInterpretationPort(Protocol):
    """Interpret one bounded approved semantic closure into typed intent or ambiguity."""

    def interpret(
        self,
        value: AdvancedInterpretationInput,
    ) -> AdvancedInterpretationResult:
        """Return only a typed M32 request or closed ambiguity codes."""


@dataclass(frozen=True, slots=True)
class AdvancedSemanticRetrievalResult:
    """Logical field hits from the complete current governed registry."""

    registry_fingerprint: str
    logical_fields: tuple[LogicalFieldRef, ...]

    def __post_init__(self) -> None:
        if _SHA256.fullmatch(self.registry_fingerprint) is None:
            raise ValueError("advanced retrieval registry fingerprint is invalid")
        field_ids = tuple(item.root for item in self.logical_fields)
        if not field_ids or len(field_ids) > 12:
            raise ValueError("advanced retrieval requires between one and twelve fields")
        if len(field_ids) != len(set(field_ids)):
            raise ValueError("advanced retrieval fields must be unique")


class AdvancedSemanticRetrievalPort(Protocol):
    """Search the entire approved registry and return one bounded logical closure."""

    def retrieve(
        self,
        *,
        query: AdvancedNaturalLanguageInput,
        extraction: AdvancedMentionExtraction,
        registry: GovernedSemanticRegistrySnapshot,
    ) -> AdvancedSemanticRetrievalResult:
        """Return logical hits only; the server verifies and connects the result."""


class AdvancedQueryPreviewTokenPort(Protocol):
    """Issue and authenticate digest-only qsp2 preview claims."""

    def issue(
        self,
        payload: AdvancedPreviewTokenClaims,
    ) -> SignedAdvancedQueryPreviewToken:
        """Sign one bounded preview claim set without query text."""

    def verify(
        self,
        token: SignedAdvancedQueryPreviewToken,
        *,
        at: datetime,
    ) -> AdvancedPreviewTokenClaims:
        """Authenticate one qsp2 token or fail closed."""


__all__ = [
    "AdvancedInterpretationPort",
    "AdvancedMentionExtractionPort",
    "AdvancedQueryPreviewTokenPort",
    "AdvancedQueryStudioPortError",
    "AdvancedQueryStudioPortErrorCode",
    "AdvancedSemanticRetrievalPort",
    "AdvancedSemanticRetrievalResult",
]
