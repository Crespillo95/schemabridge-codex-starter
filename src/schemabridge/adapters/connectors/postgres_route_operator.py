"""Migrator-only PostgreSQL adapter for exact connector-route changes."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import psycopg
from psycopg import sql

from schemabridge.adapters.storage.postgres import (
    ControlConnectionProvider,
    _ControlDatabase,
)
from schemabridge.application.ports.connector_route_operator import (
    ConnectorRouteStoreError,
    ConnectorRouteStoreErrorCode,
    ConnectorRouteWrite,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    MAX_ROUTE_REVISION,
    ConnectorRouteApplyResult,
    ConnectorRouteSnapshot,
    ConnectorRouteStatus,
    GovernedExecutionTarget,
    QueryCostBudget,
    SourceConnectorKind,
    SourceDialect,
    validate_postgres_type_contract_identity,
)

_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_READER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_PUBLIC_TARGET_COLUMNS = 25
_APPLY_RESULT_COLUMNS = 9


@dataclass(frozen=True, slots=True)
class PostgresConnectorRouteOperator:
    """Invoke only version-pinned migrator functions; never select private route tables."""

    dsn: str = field(repr=False)
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-migrator"
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

    def inspect(
        self,
        *,
        workspace_id: str,
        connection_id: CatalogConnectionId,
    ) -> ConnectorRouteSnapshot | None:
        _validate_scope(workspace_id, connection_id)
        statement = sql.SQL(
            """
            SELECT
                workspace_id, connection_id, head_revision,
                route_status, connection_status, contract_version,
                connector_kind, sql_dialect, route_revision,
                route_fingerprint, target_fingerprint,
                expected_reader, type_contract_version,
                type_contract_fingerprint, source_identity_fingerprint,
                catalog_identity_fingerprint, cost_budget_version,
                explain_timeout_ms, max_response_bytes,
                max_total_cost, max_estimated_rows, max_plan_nodes,
                max_plan_depth, max_plan_width,
                cost_budget_fingerprint
            FROM {}.load_current_connector_target(%s::varchar, %s::varchar)
            """
        ).format(sql.Identifier(self.schema))
        database_failure: ConnectorRouteStoreErrorCode | None = None
        invalid_response = False
        snapshot: ConnectorRouteSnapshot | None = None
        try:
            with self._database.connect() as connection, connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                rows = connection.execute(
                    statement,
                    (workspace_id, connection_id.root),
                ).fetchall()
            if not rows:
                return None
            if len(rows) != 1:
                raise ValueError("connector route inspection returned multiple rows")
            snapshot = _snapshot_from_row(
                rows[0],
                expected_workspace_id=workspace_id,
                expected_connection_id=connection_id,
            )
        except ConnectorRouteStoreError:
            raise
        except (TypeError, ValueError, IndexError):
            invalid_response = True
        except psycopg.Error as error:
            database_failure = _database_error_code(error.sqlstate)
        if database_failure is not None:
            raise _store_error(database_failure, "connector route inspection failed")
        if invalid_response:
            raise _store_error(
                ConnectorRouteStoreErrorCode.INVALID_RESPONSE,
                "connector route inspection response is invalid",
            )
        return snapshot

    def apply(self, change: ConnectorRouteWrite) -> ConnectorRouteApplyResult:
        if not isinstance(change, ConnectorRouteWrite):
            raise ValueError("connector route write is invalid")
        bindings = change.private_bindings
        statement = sql.SQL(
            """
            SELECT *
            FROM {}.apply_connector_route_change_v2(
                %s::varchar, %s::varchar, %s::varchar, %s::bigint,
                %s::bigint, %s::bigint, %s::char, %s::varchar,
                %s::integer, %s::char, %s::char, %s::char,
                %s::integer, %s::integer, %s::numeric, %s::bigint,
                %s::integer, %s::integer,
                %s::integer, %s::char, %s::char, %s::char,
                %s::varchar, %s::varchar, %s::varchar, %s::varchar,
                %s::bigint, %s::bigint, %s::bigint, %s::bigint,
                %s::char, %s::varchar, %s::char, %s::varchar,
                %s::char, %s::varchar, %s::char, %s::char,
                %s::varchar
            )
            """
        ).format(sql.Identifier(self.schema))
        params: tuple[object, ...] = (
            change.workspace_id,
            change.connection_id.root,
            change.operation.value,
            change.expected_head_revision,
            change.contract_version,
            change.route_revision,
            change.route_fingerprint,
            change.expected_reader,
            change.type_contract_version,
            change.type_contract_fingerprint,
            change.source_identity_fingerprint,
            change.catalog_identity_fingerprint,
            change.cost_budget.explain_timeout_ms,
            change.cost_budget.max_response_bytes,
            change.cost_budget.max_total_cost,
            change.cost_budget.max_estimated_rows,
            change.cost_budget.max_plan_nodes,
            change.cost_budget.max_plan_depth,
            change.cost_budget.max_plan_width,
            change.cost_budget_fingerprint,
            change.contract_fingerprint,
            change.target_fingerprint,
            None if bindings is None else bindings.preflight.reference,
            None if bindings is None else bindings.catalog.reference,
            None if bindings is None else bindings.execution.reference,
            None if bindings is None else bindings.profile.reference,
            None if bindings is None else bindings.preflight.provider_secret_version,
            None if bindings is None else bindings.catalog.provider_secret_version,
            None if bindings is None else bindings.execution.provider_secret_version,
            None if bindings is None else bindings.profile.provider_secret_version,
            change.proposal_fingerprint,
            change.approval_id,
            change.approval_fingerprint,
            change.actor_id,
            change.idempotency_digest,
            change.audit_id,
            change.audit_fingerprint,
            change.head_fingerprint,
            change.confirmation.value,
        )
        database_failure: ConnectorRouteStoreErrorCode | None = None
        invalid_response = False
        result: ConnectorRouteApplyResult | None = None
        try:
            with self._database.connect() as connection, connection.transaction():
                row = connection.execute(statement, params).fetchone()
            if row is None:
                raise ValueError("connector route apply returned no outcome")
            result = _result_from_row(row)
        except ConnectorRouteStoreError:
            raise
        except (TypeError, ValueError, IndexError):
            invalid_response = True
        except psycopg.Error as error:
            database_failure = _database_error_code(error.sqlstate)
        if database_failure is not None:
            raise _store_error(database_failure, "connector route apply failed")
        if invalid_response or result is None:
            raise _store_error(
                ConnectorRouteStoreErrorCode.INVALID_RESPONSE,
                "connector route apply response is invalid",
            )
        return result


def _snapshot_from_row(
    row: Sequence[Any],
    *,
    expected_workspace_id: str,
    expected_connection_id: CatalogConnectionId,
) -> ConnectorRouteSnapshot:
    if len(row) != _PUBLIC_TARGET_COLUMNS:
        raise ValueError("connector route row has an invalid shape")
    workspace_id = _text(row[0])
    connection_id = CatalogConnectionId(_text(row[1]))
    head_revision = _positive(row[2])
    route_status = ConnectorRouteStatus(_text(row[3]))
    connection_status = ConnectorRouteStatus(_text(row[4]))
    contract_version = _positive(row[5])
    connector_kind = SourceConnectorKind(_text(row[6]))
    dialect = SourceDialect(_text(row[7]))
    route_revision = _positive(row[8])
    route_fingerprint = _sha256(row[9])
    stored_target_fingerprint = _sha256(row[10])
    expected_reader = _reader(row[11])
    type_contract_version = _positive(row[12])
    type_contract_fingerprint = _sha256(row[13])
    source_identity_fingerprint = _sha256(row[14])
    catalog_identity_fingerprint = _sha256(row[15])
    cost_budget_version = _positive(row[16])
    if (
        workspace_id != expected_workspace_id
        or connection_id != expected_connection_id
        or connector_kind is not SourceConnectorKind.POSTGRESQL
        or dialect is not SourceDialect.POSTGRESQL
        or cost_budget_version != 1
    ):
        raise ValueError("connector route row does not match the request")
    validate_postgres_type_contract_identity(
        version=type_contract_version,
        fingerprint=type_contract_fingerprint,
    )
    budget = QueryCostBudget(
        version=cost_budget_version,
        explain_timeout_ms=_positive(row[17]),
        max_response_bytes=_positive(row[18]),
        max_total_cost=_decimal(row[19]),
        max_estimated_rows=_non_negative(row[20]),
        max_plan_nodes=_positive(row[21]),
        max_plan_depth=_positive(row[22]),
        max_plan_width=_non_negative(row[23]),
    )
    budget_fingerprint = _sha256(row[24])
    target = GovernedExecutionTarget(
        workspace_id=workspace_id,
        connection_id=connection_id,
        connector_kind=connector_kind,
        dialect=dialect,
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
        raise ValueError("connector route target fingerprint is invalid")
    return ConnectorRouteSnapshot(
        workspace_id=workspace_id,
        connection_id=connection_id,
        head_revision=head_revision,
        route_status=route_status,
        connection_status=connection_status,
        contract_version=contract_version,
        type_contract_version=type_contract_version,
        target=target,
    )


def _result_from_row(row: Sequence[Any]) -> ConnectorRouteApplyResult:
    if len(row) != _APPLY_RESULT_COLUMNS:
        raise ValueError("connector route apply row has an invalid shape")
    return ConnectorRouteApplyResult(
        workspace_id=_text(row[0]),
        connection_id=CatalogConnectionId(_text(row[1])),
        head_revision=_positive(row[2]),
        contract_version=_positive(row[3]),
        route_revision=_positive(row[4]),
        route_fingerprint=_sha256(row[5]),
        target_fingerprint=_sha256(row[6]),
        status=ConnectorRouteStatus(_text(row[7])),
        audit_id=_text(row[8]),
    )


def _validate_scope(
    workspace_id: str,
    connection_id: CatalogConnectionId,
) -> None:
    if (
        not isinstance(workspace_id, str)
        or _SAFE_ID.fullmatch(workspace_id) is None
        or not isinstance(connection_id, CatalogConnectionId)
    ):
        raise ValueError("connector route scope is invalid")


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("connector route text is invalid")
    return value


def _sha256(value: object) -> str:
    candidate = _text(value)
    if _SHA256.fullmatch(candidate) is None:
        raise ValueError("connector route fingerprint is invalid")
    return candidate


def _reader(value: object) -> str:
    candidate = _text(value)
    if _READER.fullmatch(candidate) is None:
        raise ValueError("connector route reader is invalid")
    return candidate


def _positive(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_ROUTE_REVISION:
        raise ValueError("connector route counter is invalid")
    return value


def _non_negative(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_ROUTE_REVISION:
        raise ValueError("connector route counter is invalid")
    return value


def _decimal(value: object) -> Decimal:
    if not isinstance(value, Decimal):
        raise ValueError("connector route cost is invalid")
    return value


def _database_error_code(sqlstate: str | None) -> ConnectorRouteStoreErrorCode:
    return {
        "40001": ConnectorRouteStoreErrorCode.STATE_CONFLICT,
        "23505": ConnectorRouteStoreErrorCode.IDEMPOTENCY_CONFLICT,
        "22023": ConnectorRouteStoreErrorCode.INVALID_RESPONSE,
        "22003": ConnectorRouteStoreErrorCode.INVALID_RESPONSE,
        "55000": ConnectorRouteStoreErrorCode.STATE_CONFLICT,
        "42501": ConnectorRouteStoreErrorCode.UNAVAILABLE,
    }.get(sqlstate or "", ConnectorRouteStoreErrorCode.UNAVAILABLE)


def _store_error(
    code: ConnectorRouteStoreErrorCode,
    message: str,
) -> ConnectorRouteStoreError:
    return ConnectorRouteStoreError(code, message)


__all__ = ["PostgresConnectorRouteOperator"]
