"""Application failures for governed connector target resolution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import GovernedExecutionTarget


class ConnectorTargetErrorCode(StrEnum):
    """Stable, non-disclosing failures before any source credential is resolved."""

    UNAVAILABLE = "connector_target_unavailable"
    ROUTE_STALE = "connector_route_stale"
    ROUTE_DISABLED = "connector_route_disabled"
    SECRET_UNAVAILABLE = "connector_secret_unavailable"
    DIALECT_UNSUPPORTED = "connector_dialect_unsupported"
    TYPE_UNSUPPORTED = "connector_type_unsupported"
    INVALID_RESPONSE = "connector_target_invalid"


class ConnectorTargetError(RuntimeError):
    """One current public execution target could not be established."""

    def __init__(self, code: ConnectorTargetErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ExactExecutionTargetResolver:
    """Return only the target already bound to one durable worker authorization."""

    target: GovernedExecutionTarget

    def resolve_current(
        self,
        *,
        workspace_id: str,
        connection_id: CatalogConnectionId,
    ) -> GovernedExecutionTarget:
        if workspace_id != self.target.workspace_id or connection_id != self.target.connection_id:
            raise ConnectorTargetError(
                ConnectorTargetErrorCode.ROUTE_STALE,
                "the bound execution target does not match the requested scope",
            )
        return self.target
