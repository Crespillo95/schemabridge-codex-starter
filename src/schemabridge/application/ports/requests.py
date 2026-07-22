"""Ports for approved request context, local drafts, and the future planner seam."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from schemabridge.domain.request_context import (
    ApprovedLogicalContext,
    ValidatedAnalyticalRequest,
)
from schemabridge.domain.requests import AnalyticalRequestDraft


class RequestWorkflowErrorCode(StrEnum):
    CONTEXT_UNAVAILABLE = "request_context_unavailable"
    CONTEXT_INVALID = "request_context_invalid"
    DRAFT_NOT_FOUND = "request_draft_not_found"
    DRAFT_CONFLICT = "request_draft_conflict"
    STORE_FAILURE = "request_store_failure"


class RequestWorkflowError(RuntimeError):
    """Sanitized failure at a guided-request application boundary."""

    def __init__(self, code: RequestWorkflowErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RequestPlanningAcknowledgement:
    """Proof of a typed handoff; M09 deliberately does not return a physical plan."""

    adapter: str
    request_fingerprint: str
    status: str

    def as_dict(self) -> dict[str, str]:
        return {
            "adapter": self.adapter,
            "request_fingerprint": self.request_fingerprint,
            "status": self.status,
        }


class ApprovedRequestContextPort(Protocol):
    def load(self) -> ApprovedLogicalContext:
        """Load one explicitly labeled, approved logical context."""


class RequestDraftStorePort(Protocol):
    def load(self, draft_id: str) -> AnalyticalRequestDraft | None:
        """Load a local request draft without treating it as approved context."""

    def save(
        self,
        draft: AnalyticalRequestDraft,
        *,
        expected_revision: int | None,
    ) -> None:
        """Create or replace exactly the expected local revision."""


class RequestPlannerPort(Protocol):
    def accept(
        self,
        request: ValidatedAnalyticalRequest,
    ) -> RequestPlanningAcknowledgement:
        """Accept a validated logical request without resolving physical assets in M09."""
