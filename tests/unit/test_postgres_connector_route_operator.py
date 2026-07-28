from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Protocol, cast

import psycopg
import pytest

from schemabridge.adapters.connectors.postgres_route_operator import (
    PostgresConnectorRouteOperator,
)
from schemabridge.application.ports.connector_route_operator import (
    ConnectorPrivateBindings,
    ConnectorRouteStoreError,
    ConnectorRouteStoreErrorCode,
    ConnectorRouteWrite,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    ConnectorRouteConfirmation,
    ConnectorRouteOperation,
    ConnectorRouteStatus,
    GovernedExecutionTarget,
    QueryCostBudget,
    SourceConnectorKind,
    SourceDialect,
    postgres_type_contract_fingerprint,
)

WORKSPACE_ID = "workspace-route-adapter"
CONNECTION_ID = CatalogConnectionId("connection-route-adapter")
CONTROL_DSN = "postgresql://private-control.invalid/control"
SOURCE_IDENTITY_FINGERPRINT = "1" * 64
CATALOG_IDENTITY_FINGERPRINT = "2" * 64
PRIVATE_VALUES = (
    "vault:preflight:adapter-alpha",
    "vault:catalog:adapter-bravo",
    "vault:execution:adapter-charlie",
    "vault:profile:adapter-delta",
)


class _Composable(Protocol):
    def as_string(self) -> str: ...


@dataclass
class _Cursor:
    rows: Sequence[tuple[object, ...]]

    def fetchall(self) -> Sequence[tuple[object, ...]]:
        return self.rows

    def fetchone(self) -> tuple[object, ...] | None:
        return None if not self.rows else self.rows[0]


@dataclass
class _Connection:
    batches: list[Sequence[tuple[object, ...]]]
    database_error: psycopg.Error | None = None
    statements: list[tuple[str, Sequence[object] | None]] = field(default_factory=list)
    transaction_count: int = 0

    def execute(
        self,
        query: object,
        params: Sequence[object] | None = None,
    ) -> _Cursor:
        text = query if isinstance(query, str) else cast(_Composable, query).as_string()
        normalized = " ".join(text.split())
        self.statements.append((normalized, params))
        if normalized == "SET TRANSACTION READ ONLY":
            return _Cursor(())
        if self.database_error is not None:
            raise self.database_error
        assert self.batches, f"unexpected connector route query: {normalized}"
        return _Cursor(self.batches.pop(0))

    @contextmanager
    def transaction(self) -> Iterator[object]:
        self.transaction_count += 1
        yield object()


@dataclass
class _Provider:
    connection_value: _Connection

    @contextmanager
    def connection(self) -> Iterator[_Connection]:
        yield self.connection_value


def _budget() -> QueryCostBudget:
    return QueryCostBudget(
        explain_timeout_ms=2_500,
        max_response_bytes=262_144,
        max_total_cost=Decimal("12345.67"),
        max_estimated_rows=250_000,
        max_plan_nodes=500,
        max_plan_depth=32,
        max_plan_width=8_192,
    )


def _target() -> GovernedExecutionTarget:
    budget = _budget()
    return GovernedExecutionTarget(
        workspace_id=WORKSPACE_ID,
        connection_id=CONNECTION_ID,
        connector_kind=SourceConnectorKind.POSTGRESQL,
        dialect=SourceDialect.POSTGRESQL,
        route_revision=1,
        route_fingerprint="3" * 64,
        expected_reader="schemabridge_source_reader",
        source_identity_fingerprint=SOURCE_IDENTITY_FINGERPRINT,
        catalog_identity_fingerprint=CATALOG_IDENTITY_FINGERPRINT,
        type_contract_fingerprint=postgres_type_contract_fingerprint(),
        cost_budget=budget,
        cost_budget_fingerprint=budget.fingerprint,
    )


def _public_row(
    *,
    route_status: str = "enabled",
) -> tuple[object, ...]:
    target = _target()
    budget = target.cost_budget
    return (
        WORKSPACE_ID,
        CONNECTION_ID.root,
        1,
        route_status,
        "enabled",
        1,
        "postgresql",
        "postgresql",
        target.route_revision,
        target.route_fingerprint,
        target.fingerprint,
        target.expected_reader,
        1,
        target.type_contract_fingerprint,
        target.source_identity_fingerprint,
        target.catalog_identity_fingerprint,
        1,
        budget.explain_timeout_ms,
        budget.max_response_bytes,
        budget.max_total_cost,
        budget.max_estimated_rows,
        budget.max_plan_nodes,
        budget.max_plan_depth,
        budget.max_plan_width,
        budget.fingerprint,
    )


def _write() -> ConnectorRouteWrite:
    target = _target()
    return ConnectorRouteWrite(
        workspace_id=WORKSPACE_ID,
        connection_id=CONNECTION_ID,
        operation=ConnectorRouteOperation.CREATE,
        expected_head_revision=0,
        contract_version=1,
        route_revision=1,
        route_fingerprint=target.route_fingerprint,
        expected_reader=target.expected_reader,
        source_identity_fingerprint=target.source_identity_fingerprint,
        catalog_identity_fingerprint=target.catalog_identity_fingerprint,
        type_contract_version=1,
        type_contract_fingerprint=target.type_contract_fingerprint,
        cost_budget=target.cost_budget,
        cost_budget_fingerprint=target.cost_budget_fingerprint,
        contract_fingerprint="5" * 64,
        target_fingerprint=target.fingerprint,
        private_bindings=ConnectorPrivateBindings(
            preflight=PRIVATE_VALUES[0],
            catalog=PRIVATE_VALUES[1],
            execution=PRIVATE_VALUES[2],
            profile=PRIVATE_VALUES[3],
        ),
        proposal_fingerprint="6" * 64,
        approval_id="connector_route_approval_" + ("7" * 64),
        approval_fingerprint="8" * 64,
        actor_id="platform-admin",
        idempotency_digest="9" * 64,
        audit_id="connector_route_audit_" + ("a" * 64),
        audit_fingerprint="b" * 64,
        head_fingerprint="c" * 64,
        confirmation=ConnectorRouteConfirmation.CREATE,
    )


def _adapter(
    batches: list[Sequence[tuple[object, ...]]],
) -> tuple[PostgresConnectorRouteOperator, _Connection]:
    connection = _Connection(batches=batches)
    return (
        PostgresConnectorRouteOperator(
            dsn=CONTROL_DSN,
            connection_provider=_Provider(connection),  # type: ignore[arg-type]
        ),
        connection,
    )


def test_inspect_reads_one_public_target_read_only_including_identity_fingerprints() -> None:
    adapter, connection = _adapter([[_public_row()]])

    snapshot = adapter.inspect(
        workspace_id=WORKSPACE_ID,
        connection_id=CONNECTION_ID,
    )

    assert snapshot is not None
    assert snapshot.target == _target()
    assert snapshot.target.source_identity_fingerprint == SOURCE_IDENTITY_FINGERPRINT
    assert snapshot.target.catalog_identity_fingerprint == CATALOG_IDENTITY_FINGERPRINT
    assert connection.transaction_count == 1
    assert connection.statements[0] == ("SET TRANSACTION READ ONLY", None)
    query, params = connection.statements[1]
    assert "load_current_connector_target" in query
    assert params == (WORKSPACE_ID, CONNECTION_ID.root)
    assert CONTROL_DSN not in repr(adapter)


def test_inspect_returns_disabled_public_state_without_private_route_read() -> None:
    adapter, connection = _adapter([[_public_row(route_status="disabled")]])

    snapshot = adapter.inspect(
        workspace_id=WORKSPACE_ID,
        connection_id=CONNECTION_ID,
    )

    assert snapshot is not None
    assert snapshot.route_status is ConnectorRouteStatus.DISABLED
    query = connection.statements[1][0]
    assert "connector_private_route_revisions" not in query
    assert "credential_binding_ref" not in query


def test_apply_calls_fixed_cas_with_identity_fingerprints_and_sanitized_result() -> None:
    change = _write()
    result_row = (
        WORKSPACE_ID,
        CONNECTION_ID.root,
        1,
        1,
        1,
        change.route_fingerprint,
        change.target_fingerprint,
        "enabled",
        change.audit_id,
    )
    adapter, connection = _adapter([[result_row]])

    result = adapter.apply(change)

    assert result.audit_id == change.audit_id
    query, params = connection.statements[0]
    assert "apply_connector_route_change" in query
    assert params is not None
    assert len(params) == 35
    assert params[10] == SOURCE_IDENTITY_FINGERPRINT
    assert params[11] == CATALOG_IDENTITY_FINGERPRINT
    assert params[22:26] == PRIVATE_VALUES
    rendered = repr(change)
    for private_value in PRIVATE_VALUES:
        assert private_value not in rendered


def test_inspect_rejects_substituted_identity_without_disclosing_it() -> None:
    row = list(_public_row())
    row[14] = "f" * 64
    adapter, _ = _adapter([[tuple(row)]])

    with pytest.raises(ConnectorRouteStoreError) as raised:
        adapter.inspect(
            workspace_id=WORKSPACE_ID,
            connection_id=CONNECTION_ID,
        )

    assert raised.value.code is ConnectorRouteStoreErrorCode.INVALID_RESPONSE
    assert "f" * 64 not in str(raised.value)


@pytest.mark.parametrize(
    ("row_index", "invalid_value"),
    (
        (12, 2),
        (13, "f" * 64),
    ),
)
def test_inspect_rejects_unsupported_type_contract(
    row_index: int,
    invalid_value: object,
) -> None:
    row = list(_public_row())
    row[row_index] = invalid_value
    adapter, _ = _adapter([[tuple(row)]])

    with pytest.raises(ConnectorRouteStoreError) as raised:
        adapter.inspect(
            workspace_id=WORKSPACE_ID,
            connection_id=CONNECTION_ID,
        )

    assert raised.value.code is ConnectorRouteStoreErrorCode.INVALID_RESPONSE


def test_write_rejects_unsupported_type_contract_version() -> None:
    with pytest.raises(ValueError, match="type contract identity is unsupported"):
        replace(_write(), type_contract_version=2)


def test_write_rejects_unsupported_type_contract_fingerprint() -> None:
    with pytest.raises(ValueError, match="type contract identity is unsupported"):
        replace(_write(), type_contract_fingerprint="f" * 64)


def test_apply_strips_private_database_error_context_and_values() -> None:
    sentinel = PRIVATE_VALUES[2]
    connection = _Connection(
        batches=[],
        database_error=psycopg.OperationalError(sentinel),
    )
    adapter = PostgresConnectorRouteOperator(
        dsn=CONTROL_DSN,
        connection_provider=_Provider(connection),  # type: ignore[arg-type]
    )

    with pytest.raises(ConnectorRouteStoreError) as raised:
        adapter.apply(_write())

    assert raised.value.code is ConnectorRouteStoreErrorCode.UNAVAILABLE
    assert sentinel not in str(raised.value)
    assert sentinel not in repr(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
