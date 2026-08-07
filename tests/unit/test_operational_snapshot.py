from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext

import pytest

from schemabridge.adapters.observability.metrics import OpenMetricsRegistry
from schemabridge.adapters.observability.postgres_snapshot import (
    QUEUE_SNAPSHOT_SQL,
    TRANSACTION_CONTRACT_SQL,
    PostgresOperationalSnapshotReader,
)
from schemabridge.application.operational_snapshot import RefreshOperationalSnapshot
from schemabridge.application.ports.operational_snapshot import (
    OPERATIONAL_QUEUES,
    OperationalQueue,
    OperationalSnapshot,
    OperationalSnapshotUnavailable,
    QueueOperationalSnapshot,
)


def _snapshot() -> OperationalSnapshot:
    return OperationalSnapshot(
        queues=(
            QueueOperationalSnapshot(OperationalQueue.EXECUTION, 7, 45.5),
            QueueOperationalSnapshot(OperationalQueue.CATALOG, 2, 3.0),
            QueueOperationalSnapshot(OperationalQueue.PROFILE, 0, 0.0),
            QueueOperationalSnapshot(OperationalQueue.PUBLICATION, 3, 6.0),
            QueueOperationalSnapshot(OperationalQueue.RECONCILIATION, 1, 8.25),
        )
    )


class _Reader:
    def __init__(self, snapshot: object) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def read(self) -> OperationalSnapshot:
        self.calls += 1
        return self.snapshot  # type: ignore[return-value]


class _Metrics:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.samples: list[tuple[str, Mapping[str, str], float]] = []

    def set_gauges_atomically(
        self,
        samples: Sequence[tuple[str, Mapping[str, str], float]],
    ) -> None:
        if self.failure is not None:
            raise self.failure
        self.samples.extend((name, dict(labels), value) for name, labels, value in samples)


class _Cursor:
    def __init__(
        self,
        *,
        contract_row: object = (
            2000,
            True,
            "schemabridge_observer",
            "schemabridge_observer",
            True,
        ),
        rows: object | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.contract_row = contract_row
        self.rows = (
            rows
            if rows is not None
            else [
                ("execution", 7, 45.5),
                ("catalog", 2, 3.0),
                ("profile", 0, 0.0),
                ("publication", 3, 6.0),
                ("reconciliation", 1, 8.25),
            ]
        )
        self.failure = failure
        self.executions: list[tuple[str, object | None]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str, parameters: object | None = None) -> None:
        self.executions.append((statement, parameters))
        if self.failure is not None:
            raise self.failure

    def fetchone(self) -> object:
        return self.contract_row

    def fetchall(self) -> object:
        return self.rows


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self.read_only = False
        self.cursor_value = cursor
        self.transaction_entries = 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        assert self.read_only is True
        self.transaction_entries += 1
        yield

    def cursor(self) -> _Cursor:
        return self.cursor_value


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection_value = connection
        self.acquisitions = 0

    def connection(self) -> AbstractContextManager[_Connection]:
        self.acquisitions += 1
        return nullcontext(self.connection_value)


def test_snapshot_contract_requires_exact_canonical_queue_set() -> None:
    with pytest.raises(ValueError, match="queue set"):
        OperationalSnapshot(
            queues=(
                QueueOperationalSnapshot(OperationalQueue.CATALOG, 1, 0.0),
                QueueOperationalSnapshot(OperationalQueue.EXECUTION, 1, 0.0),
                QueueOperationalSnapshot(OperationalQueue.PROFILE, 1, 0.0),
                QueueOperationalSnapshot(OperationalQueue.PUBLICATION, 1, 0.0),
                QueueOperationalSnapshot(OperationalQueue.RECONCILIATION, 1, 0.0),
            )
        )


@pytest.mark.parametrize(
    ("depth", "age"),
    [
        (-1, 0.0),
        (True, 0.0),
        (1, -0.1),
        (1, float("inf")),
        (1, float("nan")),
    ],
)
def test_queue_aggregate_rejects_unbounded_or_invalid_values(
    depth: int,
    age: float,
) -> None:
    with pytest.raises(ValueError, match="operational queue"):
        QueueOperationalSnapshot(OperationalQueue.EXECUTION, depth, age)


def test_use_case_publishes_only_closed_metric_names_and_queue_labels() -> None:
    reader = _Reader(_snapshot())
    metrics = _Metrics()

    result = RefreshOperationalSnapshot(reader=reader, metrics=metrics).execute()

    assert result == _snapshot()
    assert reader.calls == 1
    assert len(metrics.samples) == 10
    assert {name for name, _labels, _value in metrics.samples} == {
        "schemabridge_queue_depth",
        "schemabridge_queue_oldest_age_seconds",
    }
    assert {tuple(labels) for _name, labels, _value in metrics.samples} == {("queue",)}
    assert {labels["queue"] for _name, labels, _value in metrics.samples} == {
        queue.value for queue in OPERATIONAL_QUEUES
    }


def test_use_case_populates_the_existing_closed_registry() -> None:
    registry = OpenMetricsRegistry()

    RefreshOperationalSnapshot(reader=_Reader(_snapshot()), metrics=registry).execute()
    rendered = registry.render()

    assert 'schemabridge_queue_depth{queue="execution"} 7.0' in rendered
    assert 'schemabridge_queue_oldest_age_seconds{queue="catalog"} 3.0' in rendered
    assert "workspace" not in rendered
    assert "tenant" not in rendered


@pytest.mark.parametrize(
    "dependency",
    [
        _Reader(object()),
        _Metrics(RuntimeError("postgresql://operator:secret@internal/control")),
    ],
)
def test_use_case_failures_are_sanitized(dependency: object) -> None:
    reader = dependency if isinstance(dependency, _Reader) else _Reader(_snapshot())
    metrics = dependency if isinstance(dependency, _Metrics) else _Metrics()

    with pytest.raises(OperationalSnapshotUnavailable) as captured:
        RefreshOperationalSnapshot(reader=reader, metrics=metrics).execute()  # type: ignore[arg-type]

    assert str(captured.value) == "operational snapshot is unavailable"
    assert captured.value.__cause__ is None
    assert "secret" not in str(captured.value)


def test_postgres_sql_is_select_only_and_reads_only_the_sanitized_view() -> None:
    statements = (TRANSACTION_CONTRACT_SQL, QUEUE_SNAPSHOT_SQL)
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert all(";" not in statement for statement in statements)

    tables = re.findall(
        r"\bFROM\s+(schemabridge_control\.[a-z_][a-z0-9_]*)",
        QUEUE_SNAPSHOT_SQL,
        re.IGNORECASE,
    )
    assert tables == ["schemabridge_control.operational_queue_snapshot"]
    assert "COUNT(" not in QUEUE_SNAPSHOT_SQL.upper()
    assert "UNION" not in QUEUE_SNAPSHOT_SQL.upper()
    assert "ORDER BY CASE queue" in QUEUE_SNAPSHOT_SQL
    assert "set_config('statement_timeout'" in TRANSACTION_CONTRACT_SQL
    assert "* 1000" in TRANSACTION_CONTRACT_SQL
    assert "::BIGINT" in TRANSACTION_CONTRACT_SQL
    assert "transaction_read_only" in TRANSACTION_CONTRACT_SQL
    assert "default_transaction_read_only" in TRANSACTION_CONTRACT_SQL
    assert "CURRENT_USER" in TRANSACTION_CONTRACT_SQL
    assert "SESSION_USER" in TRANSACTION_CONTRACT_SQL
    for forbidden in (
        "workspace_id",
        "tenant_id",
        "job_id",
        "refresh_id",
        "scan_id",
        "connection_id",
        "payload",
        "result_rows",
    ):
        assert forbidden not in QUEUE_SNAPSHOT_SQL


def test_postgres_reader_enforces_read_only_timeout_and_exact_aggregates() -> None:
    cursor = _Cursor()
    connection = _Connection(cursor)
    pool = _Pool(connection)

    result = PostgresOperationalSnapshotReader(pool=pool).read()

    assert result == _snapshot()
    assert pool.acquisitions == 1
    assert connection.read_only is True
    assert connection.transaction_entries == 1
    assert cursor.executions == [
        (TRANSACTION_CONTRACT_SQL, ("2000ms",)),
        (QUEUE_SNAPSHOT_SQL, None),
    ]


@pytest.mark.parametrize(
    "cursor",
    [
        _Cursor(
            contract_row=(
                2000,
                False,
                "schemabridge_observer",
                "schemabridge_observer",
                True,
            )
        ),
        _Cursor(
            contract_row=(
                9000,
                True,
                "schemabridge_observer",
                "schemabridge_observer",
                True,
            )
        ),
        _Cursor(
            contract_row=(
                2000,
                True,
                "schemabridge_migrator",
                "schemabridge_migrator",
                False,
            )
        ),
        _Cursor(rows=[("execution", 1, 1.0)]),
        _Cursor(
            rows=[
                ("execution", 1, 1.0),
                ("catalog", 1, 1.0),
                ("profile", 1, 1.0),
                ("publication", 1, 1.0),
                ("unknown", 1, 1.0),
            ]
        ),
        _Cursor(failure=RuntimeError("postgresql://operator:secret@internal/control")),
    ],
)
def test_postgres_reader_rejects_contract_drift_without_leaking_details(
    cursor: _Cursor,
) -> None:
    reader = PostgresOperationalSnapshotReader(pool=_Pool(_Connection(cursor)))

    with pytest.raises(OperationalSnapshotUnavailable) as captured:
        reader.read()

    assert str(captured.value) == "operational snapshot is unavailable"
    assert "secret" not in str(captured.value)


@pytest.mark.parametrize("timeout", [99, 5_001, True])
def test_postgres_reader_rejects_an_unsafe_timeout(timeout: int) -> None:
    with pytest.raises(ValueError, match="statement timeout"):
        PostgresOperationalSnapshotReader(
            pool=_Pool(_Connection(_Cursor())),
            statement_timeout_ms=timeout,
        )
