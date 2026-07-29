"""Lease-bound PostgreSQL routing for managed catalog refreshes."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import psycopg
from psycopg import sql
from pydantic import ValidationError

from schemabridge.adapters.storage.postgres import (
    ControlConnectionProvider,
    _ControlDatabase,
)
from schemabridge.application.ports.catalog_inventory import (
    CatalogInventoryError,
    CatalogInventoryErrorCode,
    ManagedCatalogConnectorRoute,
)
from schemabridge.domain.catalog_inventory import (
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogConnectionRoute,
    CatalogConnectionStatus,
    CatalogRefreshId,
)
from schemabridge.domain.connectors import MAX_ROUTE_REVISION

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ROUTE_COLUMN_COUNT = 12


@dataclass(frozen=True, slots=True)
class PostgresCatalogConnectorRouteReader:
    """Read one current version-pinned catalog binding for the exact live refresh lease."""

    dsn: str = field(repr=False)
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-catalog"
    connection_provider: ControlConnectionProvider | None = field(default=None, repr=False)
    _database: _ControlDatabase = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_database",
            _ControlDatabase(
                self.dsn,
                self.schema,
                application_name=self.application_name,
                connection_provider=self.connection_provider,
            ),
        )

    def load_route(
        self,
        workspace_id: str,
        connection_id: CatalogConnectionId,
        *,
        refresh_id: CatalogRefreshId,
        indexer_id: str,
        lease_capability: str,
        fencing_token: int,
    ) -> ManagedCatalogConnectorRoute | None:
        """Resolve the current public head and private catalog capability atomically."""

        target_loader = self._database.table("load_current_connector_target")
        route_loader = self._database.table("load_owned_catalog_connector_route_v2")
        try:
            with self._database.connect() as connection, connection.transaction():
                rows = [
                    tuple(row)
                    for row in connection.execute(
                        sql.SQL(
                            """
                            SELECT
                                route.workspace_id,
                                route.connection_id,
                                route.source_kind,
                                route.environment,
                                route.catalog_scope,
                                route.platform_instance,
                                route.catalog_identity_fingerprint,
                                route.contract_version,
                                route.route_revision,
                                route.target_fingerprint,
                                route.credential_binding_ref,
                                route.provider_secret_version
                            FROM {}(%s, %s) AS target
                            CROSS JOIN LATERAL {}(
                                %s, %s, %s, %s, %s, %s,
                                target.contract_version,
                                target.route_revision,
                                target.target_fingerprint
                            ) AS route
                            """
                        ).format(target_loader, route_loader),
                        (
                            workspace_id,
                            connection_id.root,
                            workspace_id,
                            connection_id.root,
                            refresh_id.root,
                            indexer_id,
                            lease_capability,
                            fencing_token,
                        ),
                    ).fetchall()
                ]
        except psycopg.Error:
            raise _route_error(
                CatalogInventoryErrorCode.UNAVAILABLE,
                "catalog connector route is unavailable",
            ) from None
        except (TypeError, ValueError):
            raise _route_error(
                CatalogInventoryErrorCode.INVALID_RESPONSE,
                "catalog connector route response is invalid",
            ) from None
        if not rows:
            return None
        if len(rows) != 1:
            raise _route_error(
                CatalogInventoryErrorCode.INVALID_RESPONSE,
                "catalog connector route response is invalid",
            )
        try:
            return _route_from_row(
                rows[0],
                expected_workspace_id=workspace_id,
                expected_connection_id=connection_id,
            )
        except (TypeError, ValueError, ValidationError):
            raise _route_error(
                CatalogInventoryErrorCode.INVALID_RESPONSE,
                "catalog connector route response is invalid",
            ) from None


def _route_from_row(
    row: tuple[object, ...],
    *,
    expected_workspace_id: str,
    expected_connection_id: CatalogConnectionId,
) -> ManagedCatalogConnectorRoute:
    if len(row) != _ROUTE_COLUMN_COUNT:
        raise ValueError("catalog connector route row has an invalid shape")
    workspace_id = _text(row[0])
    connection_id = CatalogConnectionId(_text(row[1]))
    if workspace_id != expected_workspace_id or connection_id != expected_connection_id:
        raise ValueError("catalog connector route scope does not match")
    platform_instance = row[5]
    if platform_instance is not None and not isinstance(platform_instance, str):
        raise ValueError("catalog connector platform instance is invalid")
    catalog_identity_fingerprint = _sha256(row[6])
    contract_version = _positive_counter(row[7])
    route_revision = _positive_counter(row[8])
    target_fingerprint = _sha256(row[9])
    return ManagedCatalogConnectorRoute(
        route=CatalogConnectionRoute(
            workspace_id=workspace_id,
            connection_id=connection_id,
            kind=CatalogConnectionKind(_text(row[2])),
            environment=_text(row[3]),
            catalog_scope=_text(row[4]),
            platform_instance=platform_instance,
            catalog_identity_fingerprint=catalog_identity_fingerprint,
            status=CatalogConnectionStatus.ENABLED,
            contract_version=contract_version,
            route_revision=route_revision,
            target_fingerprint=target_fingerprint,
        ),
        credential_binding_ref=_text(row[10]),
        provider_secret_version=_positive_counter(row[11]),
    )


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("catalog connector route text is invalid")
    return value


def _positive_counter(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_ROUTE_REVISION:
        raise ValueError("catalog connector route counter is invalid")
    return value


def _sha256(value: object) -> str:
    candidate = _text(value)
    if _SHA256.fullmatch(candidate) is None:
        raise ValueError("catalog connector target fingerprint is invalid")
    return candidate


def _route_error(
    code: CatalogInventoryErrorCode,
    message: str,
) -> CatalogInventoryError:
    return CatalogInventoryError(code, message)


__all__ = ["PostgresCatalogConnectorRouteReader"]
