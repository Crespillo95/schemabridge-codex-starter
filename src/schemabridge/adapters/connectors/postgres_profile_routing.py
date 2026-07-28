"""Lease-bound PostgreSQL route reader for aggregate semantic profiling."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import psycopg
from psycopg import sql

from schemabridge.adapters.connectors.local_secrets import OpaqueConnectorSecretRef
from schemabridge.adapters.storage.postgres import (
    ControlConnectionProvider,
    _ControlDatabase,
)
from schemabridge.application.connectors import (
    ConnectorTargetError,
    ConnectorTargetErrorCode,
)
from schemabridge.application.ports.semantic_profile_jobs import (
    SemanticJoinProfileRouteContext,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import SourceDialect

_POSTGRES_READER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_PROFILE_ROUTE_COLUMN_COUNT = 10


@dataclass(frozen=True, slots=True)
class ProfilePostgresConnectorRoute:
    """Adapter-private route material returned only for one current lease."""

    dialect: SourceDialect
    expected_reader: str
    source_identity_fingerprint: str
    secret_reference: OpaqueConnectorSecretRef = field(repr=False)

    def __post_init__(self) -> None:
        if (
            self.dialect is not SourceDialect.POSTGRESQL
            or _POSTGRES_READER.fullmatch(self.expected_reader) is None
            or len(self.source_identity_fingerprint) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.source_identity_fingerprint
            )
        ):
            raise ValueError("semantic profile connector route is invalid")


@dataclass(frozen=True, slots=True)
class PostgresSemanticProfileConnectorRouteReader:
    """Load a profile binding through the exact v9 lease-owned capability."""

    dsn: str = field(repr=False)
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-worker"
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

    def load(
        self,
        context: SemanticJoinProfileRouteContext,
    ) -> ProfilePostgresConnectorRoute:
        """Return only the private route owned by this exact live profile claim."""

        if not isinstance(context, SemanticJoinProfileRouteContext):
            raise _route_error(
                ConnectorTargetErrorCode.ROUTE_STALE,
                "semantic profile connector route is stale",
            )
        target = context.execution_target
        loader = self._database.table("load_owned_profile_connector_route")
        rows: list[tuple[object, ...]] = []
        try:
            with self._database.connect() as connection, connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                rows = [
                    tuple(row)
                    for row in connection.execute(
                        sql.SQL(
                            """
                            SELECT
                                workspace_id, job_id, connection_id,
                                contract_version, route_revision,
                                target_fingerprint, sql_dialect,
                                expected_reader, source_identity_fingerprint,
                                credential_binding_ref
                            FROM {}(%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """
                        ).format(loader),
                        (
                            context.workspace_id,
                            context.job_id,
                            context.worker_id,
                            context.lease_capability,
                            context.fencing_token,
                            target.connection_id.root,
                            context.connector_contract_version,
                            target.route_revision,
                            target.target_fingerprint,
                        ),
                    ).fetchall()
                ]
        except psycopg.Error:
            raise _route_error(
                ConnectorTargetErrorCode.UNAVAILABLE,
                "semantic profile connector route is unavailable",
            ) from None
        except (TypeError, ValueError):
            raise _route_error(
                ConnectorTargetErrorCode.INVALID_RESPONSE,
                "semantic profile connector route response is invalid",
            ) from None
        if not rows:
            raise _route_error(
                ConnectorTargetErrorCode.ROUTE_STALE,
                "semantic profile connector route is stale",
            )
        if len(rows) != 1:
            raise _route_error(
                ConnectorTargetErrorCode.INVALID_RESPONSE,
                "semantic profile connector route response is invalid",
            )
        try:
            return _route_from_row(rows[0], context=context)
        except ConnectorTargetError:
            raise
        except (TypeError, ValueError):
            raise _route_error(
                ConnectorTargetErrorCode.INVALID_RESPONSE,
                "semantic profile connector route response is invalid",
            ) from None


def _route_from_row(
    row: tuple[object, ...],
    *,
    context: SemanticJoinProfileRouteContext,
) -> ProfilePostgresConnectorRoute:
    if len(row) != _PROFILE_ROUTE_COLUMN_COUNT:
        raise ValueError("semantic profile connector route row has an invalid shape")
    target = context.execution_target
    workspace_id = _text(row[0])
    job_id = _text(row[1])
    connection_id = CatalogConnectionId(_text(row[2]))
    contract_version = _positive_integer(row[3])
    route_revision = _positive_integer(row[4])
    target_fingerprint = _sha256(row[5])
    dialect = SourceDialect(_text(row[6]))
    expected_reader = _reader(row[7])
    source_identity_fingerprint = _sha256(row[8])
    if (
        workspace_id != context.workspace_id
        or job_id != context.job_id
        or connection_id != target.connection_id
        or contract_version != context.connector_contract_version
        or route_revision != target.route_revision
        or target_fingerprint != target.target_fingerprint
        or dialect is not SourceDialect.POSTGRESQL
    ):
        raise ValueError("semantic profile connector route crossed its exact lease")
    return ProfilePostgresConnectorRoute(
        dialect=dialect,
        expected_reader=expected_reader,
        source_identity_fingerprint=source_identity_fingerprint,
        secret_reference=OpaqueConnectorSecretRef(_text(row[9])),
    )


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("semantic profile connector route text is invalid")
    return value


def _reader(value: object) -> str:
    candidate = _text(value)
    if _POSTGRES_READER.fullmatch(candidate) is None:
        raise ValueError("semantic profile connector reader is invalid")
    return candidate


def _positive_integer(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("semantic profile connector counter is invalid")
    return value


def _sha256(value: object) -> str:
    candidate = _text(value)
    if len(candidate) != 64 or any(character not in "0123456789abcdef" for character in candidate):
        raise ValueError("semantic profile connector fingerprint is invalid")
    return candidate


def _route_error(
    code: ConnectorTargetErrorCode,
    message: str,
) -> ConnectorTargetError:
    return ConnectorTargetError(code, message)


__all__ = [
    "PostgresSemanticProfileConnectorRouteReader",
    "ProfilePostgresConnectorRoute",
]
