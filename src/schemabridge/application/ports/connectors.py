"""Ports for resolving one public, tenant-scoped execution target."""

from __future__ import annotations

from typing import Protocol

from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import GovernedExecutionTarget


class ExecutionTargetResolverPort(Protocol):
    """Resolve only the current public target; secret routing stays in adapters."""

    def resolve_current(
        self,
        *,
        workspace_id: str,
        connection_id: CatalogConnectionId,
    ) -> GovernedExecutionTarget:
        """Return the exact current public target or fail closed."""
