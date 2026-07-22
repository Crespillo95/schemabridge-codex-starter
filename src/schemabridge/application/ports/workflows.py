"""Ports for durable workflow state, time, and explicitly approved publication."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from schemabridge.domain.workflows import (
    AgentWorkflowDraft,
    WorkflowPublicationApproval,
    WorkflowPublicationProposal,
    WorkflowPublicationResult,
)


class WorkflowErrorCode(StrEnum):
    NOT_FOUND = "workflow_not_found"
    CONFLICT = "workflow_conflict"
    STORE_FAILURE = "workflow_store_failure"
    INVALID_TRANSITION = "workflow_invalid_transition"
    DECISION_MISMATCH = "workflow_decision_mismatch"
    RETRY_NOT_ALLOWED = "workflow_retry_not_allowed"
    PUBLICATION_FAILED = "workflow_publication_failed"


class WorkflowError(RuntimeError):
    """Sanitized workflow failure safe for entrypoint presentation."""

    def __init__(self, code: WorkflowErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class WorkflowDraftStorePort(Protocol):
    def load(self, workflow_id: str) -> AgentWorkflowDraft | None:
        """Load one typed workflow draft."""

    def save(
        self,
        draft: AgentWorkflowDraft,
        *,
        expected_revision: int | None,
    ) -> None:
        """Persist a new draft or exactly the expected prior revision."""


class WorkflowClockPort(Protocol):
    def now(self) -> datetime:
        """Return one timezone-aware application timestamp."""


class WorkflowPublicationPort(Protocol):
    def publish(
        self,
        proposal: WorkflowPublicationProposal,
        approval: WorkflowPublicationApproval,
    ) -> WorkflowPublicationResult:
        """Publish only an explicitly approved, idempotent context proposal."""
