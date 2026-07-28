from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol, cast

import psycopg
import pytest

from schemabridge.adapters.connectors.local_secrets import OpaqueConnectorSecretRef
from schemabridge.adapters.connectors.postgres_routing import (
    ExecutionConnectorLeaseContext,
    PostgresExecutionConnectorRouteReader,
    PostgresExecutionTargetResolver,
    PostgresPreflightConnectorRouteReader,
)
from schemabridge.application.connectors import (
    ConnectorTargetError,
    ConnectorTargetErrorCode,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    QueryCostBudget,
    SourceConnectorKind,
    SourceDialect,
    postgres_type_contract_fingerprint,
)

WORKSPACE_ID = "workspace-routing-unit"
CONNECTION_ID = CatalogConnectionId("connection-routing-unit")
JOB_ID = "job-routing-unit"
WORKER_ID = "worker-routing-unit"
LEASE_CAPABILITY = "worker-lease-capability-" + ("x" * 48)
EXPECTED_READER = "schemabridge_source_reader"
SOURCE_IDENTITY_FINGERPRINT = "1" * 64
CATALOG_IDENTITY_FINGERPRINT = "2" * 64
PRIVATE_REFERENCE = "vault:execution:workspace-routing-unit"
PREFLIGHT_REFERENCE = "vault:preflight:workspace-routing-unit"
CONTROL_DSN = "postgresql://private-control.invalid/control"


class _Composable(Protocol):
    def as_string(self) -> str: ...


@dataclass
class _Cursor:
    rows: Sequence[tuple[object, ...]]

    def fetchall(self) -> Sequence[tuple[object, ...]]:
        return self.rows


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
        assert self.batches, f"unexpected connector query: {normalized}"
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


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


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


def _target(
    *,
    workspace_id: str = WORKSPACE_ID,
    connection_id: CatalogConnectionId = CONNECTION_ID,
    route_revision: int = 2,
    label: str = "default",
) -> GovernedExecutionTarget:
    budget = _budget()
    return GovernedExecutionTarget(
        workspace_id=workspace_id,
        connection_id=connection_id,
        connector_kind=SourceConnectorKind.POSTGRESQL,
        dialect=SourceDialect.POSTGRESQL,
        route_revision=route_revision,
        route_fingerprint=_digest(f"route:{label}:{route_revision}"),
        expected_reader=EXPECTED_READER,
        source_identity_fingerprint=SOURCE_IDENTITY_FINGERPRINT,
        catalog_identity_fingerprint=CATALOG_IDENTITY_FINGERPRINT,
        type_contract_fingerprint=postgres_type_contract_fingerprint(),
        cost_budget=budget,
        cost_budget_fingerprint=budget.fingerprint,
    )


def _public_row(
    target: GovernedExecutionTarget,
    *,
    head_revision: int | None = None,
) -> tuple[object, ...]:
    budget = target.cost_budget
    return (
        target.workspace_id,
        target.connection_id.root,
        target.route_revision if head_revision is None else head_revision,
        "enabled",
        "enabled",
        1,
        target.connector_kind.value,
        target.dialect.value,
        target.route_revision,
        target.route_fingerprint,
        target.fingerprint,
        target.expected_reader,
        1,
        target.type_contract_fingerprint,
        target.source_identity_fingerprint,
        target.catalog_identity_fingerprint,
        budget.version,
        budget.explain_timeout_ms,
        budget.max_response_bytes,
        budget.max_total_cost,
        budget.max_estimated_rows,
        budget.max_plan_nodes,
        budget.max_plan_depth,
        budget.max_plan_width,
        budget.fingerprint,
    )


def _lease(
    target: GovernedExecutionTarget | None = None,
    *,
    job_workspace_id: str | None = None,
) -> ExecutionConnectorLeaseContext:
    selected = target or _target()
    return ExecutionConnectorLeaseContext(
        job_workspace_id=job_workspace_id or selected.workspace_id,
        connector_workspace_id=selected.workspace_id,
        job_id=JOB_ID,
        worker_id=WORKER_ID,
        lease_capability=LEASE_CAPABILITY,
        fencing_token=7,
        connection_id=selected.connection_id,
        contract_version=1,
        route_revision=selected.route_revision,
        target_fingerprint=selected.fingerprint,
    )


def _private_row(
    lease: ExecutionConnectorLeaseContext,
    target: GovernedExecutionTarget,
) -> tuple[object, ...]:
    return (
        lease.job_workspace_id,
        lease.connector_workspace_id,
        lease.job_id,
        lease.connection_id.root,
        lease.contract_version,
        lease.route_revision,
        lease.target_fingerprint,
        target.dialect.value,
        target.expected_reader,
        target.source_identity_fingerprint,
        PRIVATE_REFERENCE,
    )


def _preflight_row(target: GovernedExecutionTarget) -> tuple[object, ...]:
    return (
        target.workspace_id,
        target.connection_id.root,
        1,
        target.route_revision,
        target.fingerprint,
        target.dialect.value,
        target.expected_reader,
        target.source_identity_fingerprint,
        PREFLIGHT_REFERENCE,
    )


def _resolver(
    rows: Sequence[tuple[object, ...]],
) -> tuple[PostgresExecutionTargetResolver, _Connection]:
    connection = _Connection(batches=[rows])
    return (
        PostgresExecutionTargetResolver(
            dsn=CONTROL_DSN,
            connection_provider=_Provider(connection),  # type: ignore[arg-type]
        ),
        connection,
    )


def _preflight_reader(
    rows: Sequence[tuple[object, ...]],
) -> tuple[PostgresPreflightConnectorRouteReader, _Connection]:
    connection = _Connection(batches=[rows])
    return (
        PostgresPreflightConnectorRouteReader(
            dsn=CONTROL_DSN,
            connection_provider=_Provider(connection),  # type: ignore[arg-type]
        ),
        connection,
    )


def _route_reader(
    rows: Sequence[tuple[object, ...]],
) -> tuple[PostgresExecutionConnectorRouteReader, _Connection]:
    connection = _Connection(batches=[rows])
    return (
        PostgresExecutionConnectorRouteReader(
            dsn=CONTROL_DSN,
            connection_provider=_Provider(connection),  # type: ignore[arg-type]
        ),
        connection,
    )


def test_public_target_resolver_returns_exact_canonical_target_read_only() -> None:
    expected = _target()
    resolver, connection = _resolver([_public_row(expected)])

    observed = resolver.resolve_current(
        workspace_id=WORKSPACE_ID,
        connection_id=CONNECTION_ID,
    )

    assert observed == expected
    assert observed.fingerprint == expected.fingerprint
    assert connection.transaction_count == 1
    assert connection.statements[0] == ("SET TRANSACTION READ ONLY", None)
    query, params = connection.statements[1]
    assert "load_current_connector_target" in query
    assert params == (WORKSPACE_ID, CONNECTION_ID.root)
    assert CONTROL_DSN not in repr(resolver)


@pytest.mark.parametrize(
    ("row_index", "disabled_value"),
    ((3, "disabled"), (4, "disabled")),
)
def test_public_target_resolver_rejects_disabled_route_or_connection(
    row_index: int,
    disabled_value: str,
) -> None:
    target = _target()
    row = list(_public_row(target, head_revision=3))
    row[row_index] = disabled_value
    resolver, _ = _resolver([tuple(row)])

    with pytest.raises(ConnectorTargetError) as raised:
        resolver.resolve_current(
            workspace_id=WORKSPACE_ID,
            connection_id=CONNECTION_ID,
        )

    assert raised.value.code is ConnectorTargetErrorCode.ROUTE_DISABLED
    assert str(raised.value) == "connector route is disabled"


@pytest.mark.parametrize(
    ("rows", "expected_code"),
    (
        ((), ConnectorTargetErrorCode.UNAVAILABLE),
        (
            (_public_row(_target()), _public_row(_target())),
            ConnectorTargetErrorCode.INVALID_RESPONSE,
        ),
    ),
)
def test_public_target_resolver_requires_exactly_one_row(
    rows: Sequence[tuple[object, ...]],
    expected_code: ConnectorTargetErrorCode,
) -> None:
    resolver, _ = _resolver(rows)

    with pytest.raises(ConnectorTargetError) as raised:
        resolver.resolve_current(
            workspace_id=WORKSPACE_ID,
            connection_id=CONNECTION_ID,
        )

    assert raised.value.code is expected_code


def test_public_target_resolver_rejects_one_row_with_changed_shape() -> None:
    resolver, _ = _resolver([_public_row(_target())[:-1]])

    with pytest.raises(ConnectorTargetError) as raised:
        resolver.resolve_current(
            workspace_id=WORKSPACE_ID,
            connection_id=CONNECTION_ID,
        )

    assert raised.value.code is ConnectorTargetErrorCode.INVALID_RESPONSE


@pytest.mark.parametrize(
    ("row_index", "invalid_value"),
    (
        (0, "workspace-other"),
        (1, "connection-other"),
        (2, True),
        (9, "A" * 64),
        (10, "f" * 64),
        (12, 0),
        (12, 2),
        (13, "3" * 64),
        (16, 2),
        (19, "12345.67"),
        (24, "e" * 64),
    ),
)
def test_public_target_resolver_rejects_malformed_or_substituted_rows(
    row_index: int,
    invalid_value: object,
) -> None:
    target = _target()
    row = list(_public_row(target))
    row[row_index] = invalid_value
    resolver, _ = _resolver([tuple(row)])

    with pytest.raises(ConnectorTargetError) as raised:
        resolver.resolve_current(
            workspace_id=WORKSPACE_ID,
            connection_id=CONNECTION_ID,
        )

    assert raised.value.code is ConnectorTargetErrorCode.INVALID_RESPONSE
    assert invalid_value.__str__() not in str(raised.value)


def test_public_target_resolver_classifies_unsupported_dialect_before_returning_target() -> None:
    target = _target()
    row = list(_public_row(target))
    row[7] = "snowflake"
    resolver, _ = _resolver([tuple(row)])

    with pytest.raises(ConnectorTargetError) as raised:
        resolver.resolve_current(
            workspace_id=WORKSPACE_ID,
            connection_id=CONNECTION_ID,
        )

    assert raised.value.code is ConnectorTargetErrorCode.DIALECT_UNSUPPORTED
    assert "snowflake" not in str(raised.value)


def test_public_target_resolver_uses_workspace_in_identity_for_same_connection_id() -> None:
    alpha = _target(workspace_id="workspace-routing-alpha", label="alpha")
    beta = _target(workspace_id="workspace-routing-beta", label="beta")
    alpha_resolver, alpha_connection = _resolver([_public_row(alpha)])
    beta_resolver, beta_connection = _resolver([_public_row(beta)])

    alpha_observed = alpha_resolver.resolve_current(
        workspace_id=alpha.workspace_id,
        connection_id=CONNECTION_ID,
    )
    beta_observed = beta_resolver.resolve_current(
        workspace_id=beta.workspace_id,
        connection_id=CONNECTION_ID,
    )

    assert alpha_observed.fingerprint != beta_observed.fingerprint
    assert alpha_connection.statements[1][1] == (alpha.workspace_id, CONNECTION_ID.root)
    assert beta_connection.statements[1][1] == (beta.workspace_id, CONNECTION_ID.root)


def test_public_target_resolver_sanitizes_database_errors_without_cause() -> None:
    sentinel = "postgresql://reader:password@private-host/source"
    connection = _Connection(
        batches=[],
        database_error=psycopg.OperationalError(sentinel),
    )
    resolver = PostgresExecutionTargetResolver(
        dsn=CONTROL_DSN,
        connection_provider=_Provider(connection),  # type: ignore[arg-type]
    )

    with pytest.raises(ConnectorTargetError) as raised:
        resolver.resolve_current(
            workspace_id=WORKSPACE_ID,
            connection_id=CONNECTION_ID,
        )

    assert raised.value.code is ConnectorTargetErrorCode.UNAVAILABLE
    assert sentinel not in str(raised.value)
    assert sentinel not in repr(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_public_target_resolver_rejects_invalid_request_before_database_access() -> None:
    resolver, connection = _resolver([_public_row(_target())])

    with pytest.raises(ConnectorTargetError) as raised:
        resolver.resolve_current(
            workspace_id="Workspace Invalid",
            connection_id=CONNECTION_ID,
        )

    assert raised.value.code is ConnectorTargetErrorCode.INVALID_RESPONSE
    assert connection.statements == []


def test_preflight_route_returns_only_exact_opaque_reference_read_only() -> None:
    target = _target()
    reader, connection = _preflight_reader([_preflight_row(target)])

    reference = reader.load_secret_reference(target)

    assert type(reference) is OpaqueConnectorSecretRef
    assert reference.value == PREFLIGHT_REFERENCE
    assert PREFLIGHT_REFERENCE not in repr(reference)
    assert CONTROL_DSN not in repr(reader)
    assert connection.transaction_count == 1
    assert connection.statements[0] == ("SET TRANSACTION READ ONLY", None)
    query, params = connection.statements[1]
    assert "load_current_preflight_connector_route" in query
    assert params == (
        target.workspace_id,
        target.connection_id.root,
        target.route_revision,
        target.fingerprint,
    )


@pytest.mark.parametrize(
    ("rows", "expected_code"),
    (
        ((), ConnectorTargetErrorCode.ROUTE_STALE),
        (
            (
                _preflight_row(_target()),
                _preflight_row(_target()),
            ),
            ConnectorTargetErrorCode.INVALID_RESPONSE,
        ),
    ),
)
def test_preflight_route_requires_exactly_one_row(
    rows: Sequence[tuple[object, ...]],
    expected_code: ConnectorTargetErrorCode,
) -> None:
    reader, _ = _preflight_reader(rows)

    with pytest.raises(ConnectorTargetError) as raised:
        reader.load_secret_reference(_target())

    assert raised.value.code is expected_code


@pytest.mark.parametrize(
    ("row_index", "invalid_value"),
    (
        (0, "workspace-other"),
        (1, "connection-other"),
        (2, True),
        (3, 3),
        (4, "e" * 64),
        (6, "other_reader"),
        (7, "e" * 64),
    ),
)
def test_preflight_route_rejects_malformed_or_substituted_row(
    row_index: int,
    invalid_value: object,
) -> None:
    target = _target()
    row = list(_preflight_row(target))
    row[row_index] = invalid_value
    reader, _ = _preflight_reader([tuple(row)])

    with pytest.raises(ConnectorTargetError) as raised:
        reader.load_secret_reference(target)

    assert raised.value.code is ConnectorTargetErrorCode.INVALID_RESPONSE
    assert invalid_value.__str__() not in str(raised.value)


def test_preflight_route_rejects_changed_shape_and_unsupported_dialect() -> None:
    target = _target()
    shape_reader, _ = _preflight_reader([_preflight_row(target)[:-1]])
    dialect_row = list(_preflight_row(target))
    dialect_row[5] = "snowflake"
    dialect_reader, _ = _preflight_reader([tuple(dialect_row)])

    with pytest.raises(ConnectorTargetError) as shape_error:
        shape_reader.load_secret_reference(target)
    with pytest.raises(ConnectorTargetError) as dialect_error:
        dialect_reader.load_secret_reference(target)

    assert shape_error.value.code is ConnectorTargetErrorCode.INVALID_RESPONSE
    assert dialect_error.value.code is ConnectorTargetErrorCode.DIALECT_UNSUPPORTED
    assert "snowflake" not in str(dialect_error.value)


def test_preflight_route_rejects_invalid_binding_without_disclosure() -> None:
    sentinel = "postgresql://reader:password@private-host/preflight"
    target = _target()
    row = list(_preflight_row(target))
    row[8] = sentinel
    reader, _ = _preflight_reader([tuple(row)])

    with pytest.raises(ConnectorTargetError) as raised:
        reader.load_secret_reference(target)

    assert raised.value.code is ConnectorTargetErrorCode.INVALID_RESPONSE
    assert sentinel not in str(raised.value)
    assert sentinel not in repr(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_preflight_route_sanitizes_database_errors_without_context() -> None:
    sentinel = "private preflight route endpoint"
    connection = _Connection(
        batches=[],
        database_error=psycopg.OperationalError(sentinel),
    )
    reader = PostgresPreflightConnectorRouteReader(
        dsn=CONTROL_DSN,
        connection_provider=_Provider(connection),  # type: ignore[arg-type]
    )

    with pytest.raises(ConnectorTargetError) as raised:
        reader.load_secret_reference(_target())

    assert raised.value.code is ConnectorTargetErrorCode.UNAVAILABLE
    assert sentinel not in str(raised.value)
    assert sentinel not in repr(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_execution_lease_context_and_opaque_reference_hide_private_values() -> None:
    target = _target()
    lease = _lease(target, job_workspace_id="workspace-current")
    reader, connection = _route_reader([_private_row(lease, target)])

    reference = reader.load_secret_reference(lease, target)

    assert type(reference) is OpaqueConnectorSecretRef
    assert reference.value == PRIVATE_REFERENCE
    assert LEASE_CAPABILITY not in repr(lease)
    assert PRIVATE_REFERENCE not in repr(reference)
    assert CONTROL_DSN not in repr(reader)
    assert connection.transaction_count == 1
    assert connection.statements[0] == ("SET TRANSACTION READ ONLY", None)
    query, params = connection.statements[1]
    assert "load_owned_execution_connector_route" in query
    assert params == (
        lease.job_workspace_id,
        lease.connector_workspace_id,
        lease.job_id,
        lease.worker_id,
        LEASE_CAPABILITY,
        lease.fencing_token,
        lease.connection_id.root,
        lease.contract_version,
        lease.route_revision,
        lease.target_fingerprint,
    )


@pytest.mark.parametrize(
    ("row_index", "invalid_value"),
    (
        (0, "workspace-other"),
        (1, "workspace-other"),
        (2, "job-other"),
        (3, "connection-other"),
        (4, 2),
        (5, 3),
        (6, "e" * 64),
        (8, "other_reader"),
        (9, "e" * 64),
    ),
)
def test_private_execution_route_requires_exact_lease_and_target_row(
    row_index: int,
    invalid_value: object,
) -> None:
    target = _target()
    lease = _lease(target)
    row = list(_private_row(lease, target))
    row[row_index] = invalid_value
    reader, _ = _route_reader([tuple(row)])

    with pytest.raises(ConnectorTargetError) as raised:
        reader.load_secret_reference(lease, target)

    assert raised.value.code is ConnectorTargetErrorCode.INVALID_RESPONSE
    assert invalid_value.__str__() not in str(raised.value)


def test_private_execution_route_rejects_unsupported_dialect() -> None:
    target = _target()
    lease = _lease(target)
    row = list(_private_row(lease, target))
    row[7] = "snowflake"
    reader, _ = _route_reader([tuple(row)])

    with pytest.raises(ConnectorTargetError) as raised:
        reader.load_secret_reference(lease, target)

    assert raised.value.code is ConnectorTargetErrorCode.DIALECT_UNSUPPORTED
    assert "snowflake" not in str(raised.value)


@pytest.mark.parametrize(
    ("rows", "expected_code"),
    (
        ((), ConnectorTargetErrorCode.ROUTE_STALE),
        (
            (
                _private_row(_lease(), _target()),
                _private_row(_lease(), _target()),
            ),
            ConnectorTargetErrorCode.INVALID_RESPONSE,
        ),
    ),
)
def test_private_execution_route_requires_exactly_one_row(
    rows: Sequence[tuple[object, ...]],
    expected_code: ConnectorTargetErrorCode,
) -> None:
    target = _target()
    lease = _lease(target)
    reader, _ = _route_reader(rows)

    with pytest.raises(ConnectorTargetError) as raised:
        reader.load_secret_reference(lease, target)

    assert raised.value.code is expected_code


def test_private_execution_route_rejects_one_row_with_changed_shape() -> None:
    target = _target()
    lease = _lease(target)
    reader, _ = _route_reader([_private_row(lease, target)[:-1]])

    with pytest.raises(ConnectorTargetError) as raised:
        reader.load_secret_reference(lease, target)

    assert raised.value.code is ConnectorTargetErrorCode.INVALID_RESPONSE


def test_private_execution_route_rejects_invalid_binding_without_disclosure() -> None:
    sentinel = "postgresql://reader:password@private-host/source"
    target = _target()
    lease = _lease(target)
    row = list(_private_row(lease, target))
    row[10] = sentinel
    reader, _ = _route_reader([tuple(row)])

    with pytest.raises(ConnectorTargetError) as raised:
        reader.load_secret_reference(lease, target)

    assert raised.value.code is ConnectorTargetErrorCode.INVALID_RESPONSE
    assert sentinel not in str(raised.value)
    assert sentinel not in repr(raised.value)
    assert raised.value.__cause__ is None


def test_private_execution_route_rejects_target_substitution_before_database_access() -> None:
    original = _target(label="original")
    substitute = _target(label="substitute")
    lease = _lease(original)
    reader, connection = _route_reader([_private_row(lease, original)])

    with pytest.raises(ConnectorTargetError) as raised:
        reader.load_secret_reference(lease, substitute)

    assert raised.value.code is ConnectorTargetErrorCode.ROUTE_STALE
    assert connection.statements == []


def test_private_execution_route_sanitizes_database_errors_without_cause() -> None:
    sentinel = "private endpoint and credential"
    target = _target()
    lease = _lease(target)
    connection = _Connection(
        batches=[],
        database_error=psycopg.OperationalError(sentinel),
    )
    reader = PostgresExecutionConnectorRouteReader(
        dsn=CONTROL_DSN,
        connection_provider=_Provider(connection),  # type: ignore[arg-type]
    )

    with pytest.raises(ConnectorTargetError) as raised:
        reader.load_secret_reference(lease, target)

    assert raised.value.code is ConnectorTargetErrorCode.UNAVAILABLE
    assert sentinel not in str(raised.value)
    assert sentinel not in repr(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_execution_lease_context_rejects_secret_shaped_or_short_capability_safely() -> None:
    sentinel = "short-secret"
    target = _target()

    with pytest.raises(ValueError) as raised:
        ExecutionConnectorLeaseContext(
            job_workspace_id=target.workspace_id,
            connector_workspace_id=target.workspace_id,
            job_id=JOB_ID,
            worker_id=WORKER_ID,
            lease_capability=sentinel,
            fencing_token=1,
            connection_id=target.connection_id,
            contract_version=1,
            route_revision=target.route_revision,
            target_fingerprint=target.fingerprint,
        )

    assert str(raised.value) == "execution connector lease context is invalid"
    assert sentinel not in repr(raised.value)
