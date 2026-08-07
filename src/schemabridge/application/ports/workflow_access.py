"""Application port for durable, tenant-scoped workflow ownership."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from schemabridge.domain.identity import WorkflowAccessGrant


class WorkflowAccessErrorCode(StrEnum):
    """Sanitized failures exposed by a workflow-access store."""

    CONFLICT = "workflow_access_conflict"
    IDENTITY_MISMATCH = "workflow_access_identity_mismatch"
    STORE_FAILURE = "workflow_access_store_failure"


class WorkflowAccessError(RuntimeError):
    """Typed workflow-access failure safe for application-boundary handling."""

    def __init__(self, code: WorkflowAccessErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class WorkflowAccessStorePort(Protocol):
    """Persist and resolve one immutable owner for each workflow identifier."""

    def grant(self, grant: WorkflowAccessGrant) -> WorkflowAccessGrant:
        """Create a grant or return its existing identical ownership binding."""

    def load(
        self,
        workspace_id: str,
        workflow_id: str,
        *,
        owner_principal_id: str | None = None,
    ) -> WorkflowAccessGrant | None:
        """Load only within the requested workspace and optional owner scope."""

    def list_for_workspace(
        self,
        workspace_id: str,
        *,
        owner_principal_id: str | None = None,
        limit: int = 50,
    ) -> tuple[WorkflowAccessGrant, ...]:
        """List a bounded newest-first workspace or owner scope."""
