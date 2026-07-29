"""Strict PostgreSQL readers for public targets and private execution routes."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

import psycopg
from psycopg import sql
from pydantic import ValidationError

from schemabridge.adapters.connectors.local_secrets import (
    ConnectorSecretResolutionError,
    OpaqueConnectorSecretRef,
)
from schemabridge.adapters.storage.postgres import (
    ControlConnectionProvider,
    _ControlDatabase,
)
from schemabridge.application.connectors import (
    ConnectorTargetError,
    ConnectorTargetErrorCode,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    MAX_ROUTE_REVISION,
    GovernedExecutionTarget,
    QueryCostBudget,
    SourceConnectorKind,
    SourceDialect,
    validate_postgres_type_contract_identity,
)

_WORKSPACE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,199}$")
_JOB_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,199}$")
_POSTGRES_READER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NO_CONTROL = re.compile(r"^[^\x00-\x1f\x7f]+$")
_ENABLED = "enabled"
_DISABLED = "disabled"
_PUBLIC_TARGET_COLUMN_COUNT = 25
_PRIVATE_PREFLIGHT_ROUTE_COLUMN_COUNT = 10
_PRIVATE_EXECUTION_ROUTE_COLUMN_COUNT = 12


@dataclass(frozen=True, slots=True)
class ExecutionConnectorLeaseContext:
    """Adapter-private exact lease proof used for one route lookup."""

    job_workspace_id: str
    connector_workspace_id: str
    job_id: str
    worker_id: str
    lease_capability: str = field(repr=False)
    fencing_token: int
    connection_id: CatalogConnectionId
    contract_version: int
    route_revision: int
    target_fingerprint: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.job_workspace_id, str)
            or _WORKSPACE_ID.fullmatch(self.job_workspace_id) is None
            or not isinstance(self.connector_workspace_id, str)
            or _WORKSPACE_ID.fullmatch(self.connector_workspace_id) is None
            or not isinstance(self.job_id, str)
            or _JOB_ID.fullmatch(self.job_id) is None
            or not _bounded_inert_text(self.worker_id, minimum_bytes=1, maximum_bytes=200)
            or not _bounded_inert_text(
                self.lease_capability,
                minimum_bytes=32,
                maximum_bytes=1_024,
            )
            or not isinstance(self.connection_id, CatalogConnectionId)
            or not _positive_counter(self.fencing_token)
            or not _positive_counter(self.contract_version)
            or not _positive_counter(self.route_revision)
            or not isinstance(self.target_fingerprint, str)
            or _SHA256.fullmatch(self.target_fingerprint) is None
        ):
            raise ValueError("execution connector lease context is invalid")


@dataclass(frozen=True, slots=True)
class PostgresExecutionTargetResolver:
    """Resolve one current, public target without reading private route state."""

    dsn: str = field(repr=False)
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-runtime"
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

    def resolve_current(
        self,
        *,
        workspace_id: str,
        connection_id: CatalogConnectionId,
    ) -> GovernedExecutionTarget:
        """Load and verify exactly one current public target."""

        if (
            not isinstance(workspace_id, str)
            or _WORKSPACE_ID.fullmatch(workspace_id) is None
            or not isinstance(connection_id, CatalogConnectionId)
        ):
            raise _target_error(
                ConnectorTargetErrorCode.INVALID_RESPONSE,
                "connector target request is invalid",
            )
        loader = self._database.table("load_current_connector_target")
        database_unavailable = False
        invalid_database_response = False
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
                                workspace_id, connection_id, head_revision,
                                route_status, connection_status, contract_version,
                                connector_kind, sql_dialect, route_revision,
                                route_fingerprint, target_fingerprint,
                                expected_reader, type_contract_version,
                                type_contract_fingerprint,
                                source_identity_fingerprint,
                                catalog_identity_fingerprint,
                                cost_budget_version,
                                explain_timeout_ms, max_response_bytes,
                                max_total_cost, max_estimated_rows, max_plan_nodes,
                                max_plan_depth, max_plan_width,
                                cost_budget_fingerprint
                            FROM {}(%s, %s)
                            """
                        ).format(loader),
                        (workspace_id, connection_id.root),
                    ).fetchall()
                ]
        except psycopg.Error:
            database_unavailable = True
        except (TypeError, ValueError):
            invalid_database_response = True
        if database_unavailable:
            raise _target_error(
                ConnectorTargetErrorCode.UNAVAILABLE,
                "connector target is unavailable",
            )
        if invalid_database_response:
            raise _target_error(
                ConnectorTargetErrorCode.INVALID_RESPONSE,
                "connector target response is invalid",
            )
        if not rows:
            raise _target_error(
                ConnectorTargetErrorCode.UNAVAILABLE,
                "connector target is unavailable",
            )
        if len(rows) != 1:
            raise _target_error(
                ConnectorTargetErrorCode.INVALID_RESPONSE,
                "connector target response is invalid",
            )
        invalid_response = False
        target: GovernedExecutionTarget | None = None
        try:
            target = _public_target_from_row(
                rows[0],
                expected_workspace_id=workspace_id,
                expected_connection_id=connection_id,
            )
        except ConnectorTargetError:
            raise
        except (TypeError, ValueError, ValidationError):
            invalid_response = True
        if invalid_response or target is None:
            raise _target_error(
                ConnectorTargetErrorCode.INVALID_RESPONSE,
                "connector target response is invalid",
            )
        return target


@dataclass(frozen=True, slots=True)
class PostgresPreflightConnectorRouteReader:
    """Load only the runtime preflight binding for one exact current target."""

    dsn: str = field(repr=False)
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-runtime"
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

    def load_secret_reference(
        self,
        target: GovernedExecutionTarget,
    ) -> OpaqueConnectorSecretRef:
        """Return only the opaque preflight binding for this exact target."""

        if (
            not isinstance(target, GovernedExecutionTarget)
            or target.connector_kind is not SourceConnectorKind.POSTGRESQL
            or target.dialect is not SourceDialect.POSTGRESQL
        ):
            raise _target_error(
                ConnectorTargetErrorCode.ROUTE_STALE,
                "connector route is stale",
            )
        loader = self._database.table("load_current_preflight_connector_route_v2")
        database_unavailable = False
        invalid_database_response = False
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
                                workspace_id, connection_id, contract_version,
                                route_revision, target_fingerprint, sql_dialect,
                                expected_reader, source_identity_fingerprint,
                                credential_binding_ref,
                                provider_secret_version
                            FROM {}(%s, %s, %s, %s)
                            """
                        ).format(loader),
                        (
                            target.workspace_id,
                            target.connection_id.root,
                            target.route_revision,
                            target.fingerprint,
                        ),
                    ).fetchall()
                ]
        except psycopg.Error:
            database_unavailable = True
        except (TypeError, ValueError):
            invalid_database_response = True
        if database_unavailable:
            raise _target_error(
                ConnectorTargetErrorCode.UNAVAILABLE,
                "connector route is unavailable",
            )
        if invalid_database_response:
            raise _target_error(
                ConnectorTargetErrorCode.INVALID_RESPONSE,
                "connector route response is invalid",
            )
        if not rows:
            raise _target_error(
                ConnectorTargetErrorCode.ROUTE_STALE,
                "connector route is stale",
            )
        if len(rows) != 1:
            raise _target_error(
                ConnectorTargetErrorCode.INVALID_RESPONSE,
                "connector route response is invalid",
            )
        invalid_response = False
        reference: OpaqueConnectorSecretRef | None = None
        try:
            reference = _preflight_reference_from_row(rows[0], target=target)
        except ConnectorTargetError:
            raise
        except (ConnectorSecretResolutionError, TypeError, ValueError, ValidationError):
            invalid_response = True
        if invalid_response or reference is None:
            raise _target_error(
                ConnectorTargetErrorCode.INVALID_RESPONSE,
                "connector route response is invalid",
            )
        return reference


@dataclass(frozen=True, slots=True)
class PostgresExecutionConnectorRouteReader:
    """Load only an opaque execution binding for one exact live worker lease."""

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

    def load_secret_reference(
        self,
        lease: ExecutionConnectorLeaseContext,
        target: GovernedExecutionTarget,
    ) -> OpaqueConnectorSecretRef:
        """Return only the opaque private binding after exact target validation."""

        if (
            not isinstance(lease, ExecutionConnectorLeaseContext)
            or not isinstance(target, GovernedExecutionTarget)
            or lease.connector_workspace_id != target.workspace_id
            or lease.connection_id != target.connection_id
            or lease.route_revision != target.route_revision
            or lease.target_fingerprint != target.fingerprint
            or target.connector_kind is not SourceConnectorKind.POSTGRESQL
            or target.dialect is not SourceDialect.POSTGRESQL
        ):
            raise _target_error(
                ConnectorTargetErrorCode.ROUTE_STALE,
                "connector route is stale",
            )
        loader = self._database.table("load_owned_execution_connector_route_v2")
        database_unavailable = False
        invalid_database_response = False
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
                                job_workspace_id, connector_workspace_id,
                                job_id, connection_id,
                                contract_version, route_revision,
                                target_fingerprint, sql_dialect,
                                expected_reader, source_identity_fingerprint,
                                credential_binding_ref,
                                provider_secret_version
                            FROM {}(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """
                        ).format(loader),
                        (
                            lease.job_workspace_id,
                            lease.connector_workspace_id,
                            lease.job_id,
                            lease.worker_id,
                            lease.lease_capability,
                            lease.fencing_token,
                            lease.connection_id.root,
                            lease.contract_version,
                            lease.route_revision,
                            lease.target_fingerprint,
                        ),
                    ).fetchall()
                ]
        except psycopg.Error:
            database_unavailable = True
        except (TypeError, ValueError):
            invalid_database_response = True
        if database_unavailable:
            raise _target_error(
                ConnectorTargetErrorCode.UNAVAILABLE,
                "connector route is unavailable",
            )
        if invalid_database_response:
            raise _target_error(
                ConnectorTargetErrorCode.INVALID_RESPONSE,
                "connector route response is invalid",
            )
        if not rows:
            raise _target_error(
                ConnectorTargetErrorCode.ROUTE_STALE,
                "connector route is stale",
            )
        if len(rows) != 1:
            raise _target_error(
                ConnectorTargetErrorCode.INVALID_RESPONSE,
                "connector route response is invalid",
            )
        invalid_response = False
        reference: OpaqueConnectorSecretRef | None = None
        try:
            reference = _private_reference_from_row(
                rows[0],
                lease=lease,
                target=target,
            )
        except ConnectorTargetError:
            raise
        except (ConnectorSecretResolutionError, TypeError, ValueError, ValidationError):
            invalid_response = True
        if invalid_response or reference is None:
            raise _target_error(
                ConnectorTargetErrorCode.INVALID_RESPONSE,
                "connector route response is invalid",
            )
        return reference


def _public_target_from_row(
    row: tuple[object, ...],
    *,
    expected_workspace_id: str,
    expected_connection_id: CatalogConnectionId,
) -> GovernedExecutionTarget:
    if len(row) != _PUBLIC_TARGET_COLUMN_COUNT:
        raise ValueError("connector target row has an invalid shape")
    workspace_id = _text(row[0])
    connection_id = CatalogConnectionId(_text(row[1]))
    head_revision = _positive_integer(row[2])
    route_status = _text(row[3])
    connection_status = _text(row[4])
    if workspace_id != expected_workspace_id or connection_id != expected_connection_id:
        raise ValueError("connector target scope does not match the request")
    if route_status not in {_ENABLED, _DISABLED} or connection_status not in {
        _ENABLED,
        _DISABLED,
    }:
        raise ValueError("connector target state is invalid")
    if route_status == _DISABLED or connection_status == _DISABLED:
        raise _target_error(
            ConnectorTargetErrorCode.ROUTE_DISABLED,
            "connector route is disabled",
        )

    _positive_integer(row[5])
    connector_kind = _text(row[6])
    dialect = _text(row[7])
    if (
        connector_kind != SourceConnectorKind.POSTGRESQL.value
        or dialect != SourceDialect.POSTGRESQL.value
    ):
        raise _target_error(
            ConnectorTargetErrorCode.DIALECT_UNSUPPORTED,
            "connector dialect is unsupported",
        )
    route_revision = _positive_integer(row[8])
    if route_revision > MAX_ROUTE_REVISION or head_revision < route_revision:
        raise ValueError("connector route revision is invalid")
    route_fingerprint = _sha256(row[9])
    stored_target_fingerprint = _sha256(row[10])
    expected_reader = _reader(row[11])
    type_contract_version = _positive_integer(row[12])
    type_contract_fingerprint = _sha256(row[13])
    source_identity_fingerprint = _sha256(row[14])
    catalog_identity_fingerprint = _sha256(row[15])
    cost_budget_version = _positive_integer(row[16])
    validate_postgres_type_contract_identity(
        version=type_contract_version,
        fingerprint=type_contract_fingerprint,
    )
    if cost_budget_version != 1:
        raise ValueError("connector contract version is invalid")
    budget = QueryCostBudget(
        version=cost_budget_version,
        explain_timeout_ms=_positive_integer(row[17]),
        max_response_bytes=_positive_integer(row[18]),
        max_total_cost=_decimal(row[19]),
        max_estimated_rows=_non_negative_integer(row[20]),
        max_plan_nodes=_positive_integer(row[21]),
        max_plan_depth=_positive_integer(row[22]),
        max_plan_width=_non_negative_integer(row[23]),
    )
    budget_fingerprint = _sha256(row[24])
    target = GovernedExecutionTarget(
        workspace_id=workspace_id,
        connection_id=connection_id,
        connector_kind=SourceConnectorKind.POSTGRESQL,
        dialect=SourceDialect.POSTGRESQL,
        route_revision=route_revision,
        route_fingerprint=route_fingerprint,
        expected_reader=expected_reader,
        source_identity_fingerprint=source_identity_fingerprint,
        catalog_identity_fingerprint=catalog_identity_fingerprint,
        type_contract_fingerprint=type_contract_fingerprint,
        cost_budget=budget,
        cost_budget_fingerprint=budget_fingerprint,
    )
    if target.fingerprint != stored_target_fingerprint:
        raise ValueError("connector target fingerprint is invalid")
    return target


def _preflight_reference_from_row(
    row: tuple[object, ...],
    *,
    target: GovernedExecutionTarget,
) -> OpaqueConnectorSecretRef:
    if len(row) != _PRIVATE_PREFLIGHT_ROUTE_COLUMN_COUNT:
        raise ValueError("connector route row has an invalid shape")
    workspace_id = _text(row[0])
    connection_id = CatalogConnectionId(_text(row[1]))
    _positive_integer(row[2])
    route_revision = _positive_integer(row[3])
    target_fingerprint = _sha256(row[4])
    dialect = _text(row[5])
    expected_reader = _reader(row[6])
    source_identity_fingerprint = _sha256(row[7])
    if (
        workspace_id != target.workspace_id
        or connection_id != target.connection_id
        or route_revision != target.route_revision
        or target_fingerprint != target.fingerprint
    ):
        raise ValueError("connector route response does not match the target")
    if dialect != SourceDialect.POSTGRESQL.value:
        raise _target_error(
            ConnectorTargetErrorCode.DIALECT_UNSUPPORTED,
            "connector dialect is unsupported",
        )
    if (
        dialect != target.dialect.value
        or expected_reader != target.expected_reader
        or source_identity_fingerprint != target.source_identity_fingerprint
    ):
        raise ValueError("connector route response does not match the target")
    return OpaqueConnectorSecretRef(
        _text(row[8]),
        provider_secret_version=_positive_integer(row[9]),
    )


def _private_reference_from_row(
    row: tuple[object, ...],
    *,
    lease: ExecutionConnectorLeaseContext,
    target: GovernedExecutionTarget,
) -> OpaqueConnectorSecretRef:
    if len(row) != _PRIVATE_EXECUTION_ROUTE_COLUMN_COUNT:
        raise ValueError("connector route row has an invalid shape")
    job_workspace_id = _text(row[0])
    connector_workspace_id = _text(row[1])
    job_id = _text(row[2])
    connection_id = CatalogConnectionId(_text(row[3]))
    contract_version = _positive_integer(row[4])
    route_revision = _positive_integer(row[5])
    target_fingerprint = _sha256(row[6])
    dialect = _text(row[7])
    expected_reader = _reader(row[8])
    source_identity_fingerprint = _sha256(row[9])
    if (
        job_workspace_id != lease.job_workspace_id
        or connector_workspace_id != lease.connector_workspace_id
        or connector_workspace_id != target.workspace_id
        or job_id != lease.job_id
        or connection_id != lease.connection_id
        or contract_version != lease.contract_version
        or route_revision != lease.route_revision
        or target_fingerprint != lease.target_fingerprint
    ):
        raise ValueError("connector route response does not match the lease")
    if dialect != SourceDialect.POSTGRESQL.value:
        raise _target_error(
            ConnectorTargetErrorCode.DIALECT_UNSUPPORTED,
            "connector dialect is unsupported",
        )
    if (
        dialect != target.dialect.value
        or expected_reader != target.expected_reader
        or source_identity_fingerprint != target.source_identity_fingerprint
    ):
        raise ValueError("connector route response does not match the target")
    return OpaqueConnectorSecretRef(
        _text(row[10]),
        provider_secret_version=_positive_integer(row[11]),
    )


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("connector route text is invalid")
    return value


def _sha256(value: object) -> str:
    candidate = _text(value)
    if _SHA256.fullmatch(candidate) is None:
        raise ValueError("connector fingerprint is invalid")
    return candidate


def _reader(value: object) -> str:
    candidate = _text(value)
    if _POSTGRES_READER.fullmatch(candidate) is None:
        raise ValueError("connector reader is invalid")
    return candidate


def _positive_integer(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_ROUTE_REVISION:
        raise ValueError("connector counter is invalid")
    return value


def _non_negative_integer(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_ROUTE_REVISION:
        raise ValueError("connector counter is invalid")
    return value


def _decimal(value: object) -> Decimal:
    if not isinstance(value, Decimal):
        raise ValueError("connector cost is invalid")
    return value


def _positive_counter(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_ROUTE_REVISION


def _bounded_inert_text(value: object, *, minimum_bytes: int, maximum_bytes: int) -> bool:
    if not isinstance(value, str) or value.strip() != value or _NO_CONTROL.fullmatch(value) is None:
        return False
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        return False
    return minimum_bytes <= size <= maximum_bytes


def _target_error(
    code: ConnectorTargetErrorCode,
    message: str,
) -> ConnectorTargetError:
    return ConnectorTargetError(code, message)
