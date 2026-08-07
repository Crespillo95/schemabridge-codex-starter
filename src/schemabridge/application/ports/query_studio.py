"""Application ports for governed Query Studio retrieval and typed AI boundaries."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from schemabridge.domain.query_studio import (
    DescriptionExpansion,
    DescriptionExpansionInput,
    DescriptionExpansionResult,
    GovernedBindingFactsRequest,
    GovernedFieldBinding,
    GovernedFieldSearchPage,
    GovernedFieldSearchRequest,
    GuidedQueryStudioEvidence,
    OpaqueCandidateId,
    PhysicalDiscoveryCardinality,
    PhysicalFieldDiscoveryPage,
    PhysicalFieldDiscoveryRequest,
    PreviewTokenPayload,
    ProviderOutputFailureCategory,
    QueryStudioConfirmation,
    QueryStudioInterpretationInput,
    QueryStudioInterpretationResult,
    QueryStudioModelProposal,
    QueryStudioScopeSnapshot,
    SignedPreviewToken,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope


class QueryStudioPortErrorCode(StrEnum):
    RESOURCE_UNAVAILABLE = "query_studio_resource_unavailable"
    INVALID_RESPONSE = "query_studio_invalid_response"
    SCOPE_CHANGED = "query_studio_scope_changed"
    PROVIDER_UNAVAILABLE = "query_studio_provider_unavailable"
    PROVIDER_TIMEOUT = "query_studio_provider_timeout"
    PROVIDER_RATE_LIMITED = "query_studio_provider_rate_limited"
    PROVIDER_QUOTA_EXHAUSTED = "query_studio_provider_quota_exhausted"
    PROVIDER_REFUSED = "query_studio_provider_refused"
    PROVIDER_MISSING_OUTPUT = "query_studio_provider_missing_output"
    PROVIDER_INVALID_OUTPUT = "query_studio_provider_invalid_output"
    PROVIDER_MODEL_MISMATCH = "query_studio_provider_model_mismatch"
    SENSITIVE_INPUT_BLOCKED = "query_studio_sensitive_input_blocked"
    TOKEN_INVALID = "query_studio_preview_token_invalid"
    TOKEN_EXPIRED = "query_studio_preview_token_expired"


class QueryStudioRetryDisposition(StrEnum):
    """Closed provider retry authority carried across the adapter boundary."""

    NEVER = "never"
    RETRY_ONCE = "retry_once"


class QueryStudioPortError(RuntimeError):
    """Sanitized provider/storage failure with no metadata, prompt, or credential payload."""

    def __init__(
        self,
        code: QueryStudioPortErrorCode,
        message: str,
        *,
        retry_disposition: QueryStudioRetryDisposition = QueryStudioRetryDisposition.NEVER,
        output_failure_category: ProviderOutputFailureCategory | None = None,
    ) -> None:
        if not isinstance(code, QueryStudioPortErrorCode) or not isinstance(
            retry_disposition,
            QueryStudioRetryDisposition,
        ):
            raise TypeError("Query Studio port failure metadata is invalid")
        if output_failure_category is not None and (
            not isinstance(output_failure_category, ProviderOutputFailureCategory)
            or code is not QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT
        ):
            raise TypeError("Query Studio output failure category is invalid")
        self.code = code
        self.retry_disposition = retry_disposition
        self.output_failure_category = output_failure_category
        super().__init__(message)


class GovernedFieldSearchPort(Protocol):
    """Retrieve only current executable binding/catalog evidence by bounded keyset."""

    def search(self, request: GovernedFieldSearchRequest) -> GovernedFieldSearchPage:
        """Return at most 50 governed rows plus one stable continuation key."""


class GovernedBindingFactsSearchPort(Protocol):
    """PostgreSQL edge: current facts filtered by an exact logical-field allowlist."""

    def search(self, request: GovernedBindingFactsRequest) -> GovernedFieldSearchPage:
        """Return binding/catalog facts; logical role/type metadata is not duplicated here."""


class PhysicalFieldDiscoveryPort(Protocol):
    """Browse non-executable physical metadata through a deliberately distinct contract."""

    def search(self, request: PhysicalFieldDiscoveryRequest) -> PhysicalFieldDiscoveryPage:
        """Return only `needs_mapping_review` results and its own cursor type."""

    def inspect_cardinality(
        self,
        scope: SemanticRegistryScope,
    ) -> PhysicalDiscoveryCardinality:
        """Return current aggregate counts without loading inventory rows."""


class DescriptionExpansionPort(Protocol):
    """Expand business text into at most six typed search purposes without catalog access."""

    def expand(self, value: DescriptionExpansionInput) -> DescriptionExpansionResult:
        """Return typed probes and sanitized usage; never SQL, tools, or identifiers."""


class DescriptionExpansionPreflightPort(Protocol):
    """Screen and locally expand every server-supported description route."""

    def expand_if_local(
        self,
        value: DescriptionExpansionInput,
    ) -> DescriptionExpansion | None:
        """Return one grounded deterministic expansion, or delegate if unsupported."""


class QueryStudioIntentPort(Protocol):
    """Interpret only one bounded opaque-candidate vocabulary."""

    def interpret(
        self,
        value: QueryStudioInterpretationInput,
    ) -> QueryStudioInterpretationResult:
        """Return a strict candidate-ID proposal and sanitized usage facts."""


class QueryStudioCandidateIdPort(Protocol):
    """Issue opaque candidate references without exposing its signing key."""

    def issue(
        self,
        *,
        scope: QueryStudioScopeSnapshot,
        binding: GovernedFieldBinding,
        request_digest: str,
        nonce: str,
    ) -> OpaqueCandidateId:
        """Bind one exact current governed row to a request nonce."""


class QueryStudioPreviewTokenPort(Protocol):
    """Authenticate digest-only preview claims."""

    def issue(self, payload: PreviewTokenPayload) -> SignedPreviewToken:
        """Sign one bounded text-free payload."""

    def verify(
        self,
        token: SignedPreviewToken,
        *,
        at: datetime,
    ) -> PreviewTokenPayload:
        """Authenticate and return claims or fail closed."""


class QueryStudioClockPort(Protocol):
    def now(self) -> datetime:
        """Return one timezone-aware current instant."""


class QueryStudioNoncePort(Protocol):
    def new_nonce(self) -> str:
        """Return a fresh opaque nonce without using domain randomness."""


class GuidedQueryStudioRecomputePort(Protocol):
    """Reload exact current governed evidence for a guided confirmation."""

    def recompute(
        self,
        confirmation: QueryStudioConfirmation,
        *,
        nonce: str,
    ) -> GuidedQueryStudioEvidence:
        """Re-run bounded retrieval; browser/session selections are not authoritative."""


class GuidedQueryStudioSelectionPort(Protocol):
    """Resolve one opaque guided selection from current server-side governed evidence."""

    def resolve(
        self,
        proposal: QueryStudioModelProposal,
        *,
        nonce: str,
    ) -> GuidedQueryStudioEvidence:
        """Resolve all selected IDs or fail closed without trusting browser metadata."""


__all__ = [
    "DescriptionExpansionPort",
    "DescriptionExpansionPreflightPort",
    "GovernedBindingFactsSearchPort",
    "GovernedFieldSearchPort",
    "GuidedQueryStudioRecomputePort",
    "GuidedQueryStudioSelectionPort",
    "PhysicalFieldDiscoveryPort",
    "QueryStudioCandidateIdPort",
    "QueryStudioClockPort",
    "QueryStudioIntentPort",
    "QueryStudioNoncePort",
    "QueryStudioPortError",
    "QueryStudioPortErrorCode",
    "QueryStudioPreviewTokenPort",
]
