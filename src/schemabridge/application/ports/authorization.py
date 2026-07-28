"""Application port for deterministic identity authorization decisions."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    WorkflowAccessGrant,
    WorkflowPermission,
)


class WorkflowAuthorizationPort(Protocol):
    """Closed policy contract consumed by authenticated workflow use cases."""

    def permissions_for(
        self,
        principal: AuthenticatedPrincipal,
        *,
        at: datetime,
    ) -> frozenset[WorkflowPermission]:
        """Return effective permissions for a current principal."""

    def require(
        self,
        principal: AuthenticatedPrincipal,
        permission: WorkflowPermission,
        *,
        at: datetime,
    ) -> None:
        """Require one explicit permission."""

    def require_workflow(
        self,
        principal: AuthenticatedPrincipal,
        grant: WorkflowAccessGrant,
        permission: WorkflowPermission,
        *,
        at: datetime,
    ) -> None:
        """Require both role permission and resource scope."""

    def owner_filter_for(
        self,
        principal: AuthenticatedPrincipal,
        permission: WorkflowPermission,
        *,
        at: datetime,
    ) -> str | None:
        """Return an owner ID for owner-only access, or None for workspace-wide access."""
