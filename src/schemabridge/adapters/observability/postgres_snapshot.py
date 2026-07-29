"""Read-only PostgreSQL adapter for bounded aggregate queue observations."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, NoReturn, Protocol, cast

from schemabridge.application.ports.operational_snapshot import (
    OPERATIONAL_QUEUES,
    OperationalQueue,
    OperationalSnapshot,
    OperationalSnapshotUnavailable,
    QueueOperationalSnapshot,
)

MIN_STATEMENT_TIMEOUT_MS = 100
MAX_STATEMENT_TIMEOUT_MS = 5_000

TRANSACTION_CONTRACT_SQL = """
SELECT
    (
        EXTRACT(
            EPOCH FROM set_config('statement_timeout', %s, TRUE)::INTERVAL
        ) * 1000
    )::BIGINT,
    current_setting('transaction_read_only')::BOOLEAN,
    CURRENT_USER::TEXT,
    SESSION_USER::TEXT,
    current_setting('default_transaction_read_only')::BOOLEAN
"""

QUEUE_SNAPSHOT_SQL = """
SELECT
    queue,
    depth,
    oldest_due_age_seconds
FROM schemabridge_control.operational_queue_snapshot
ORDER BY CASE queue
    WHEN 'execution' THEN 1
    WHEN 'catalog' THEN 2
    WHEN 'profile' THEN 3
    WHEN 'reconciliation' THEN 4
    ELSE 5
END
"""


class ControlPlanePoolPort(Protocol):
    """Acquire an already lifecycle-managed control-plane connection."""

    def connection(self) -> AbstractContextManager[Any]:
        """Return one bounded pooled connection context."""


@dataclass(frozen=True, slots=True)
class PostgresOperationalSnapshotReader:
    """Observe due work through one locally timed, read-only transaction."""

    pool: ControlPlanePoolPort = field(repr=False)
    statement_timeout_ms: int = 2_000

    def __post_init__(self) -> None:
        timeout = self.statement_timeout_ms
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int)
            or not MIN_STATEMENT_TIMEOUT_MS <= timeout <= MAX_STATEMENT_TIMEOUT_MS
        ):
            raise ValueError("operational snapshot statement timeout is invalid")

    def read(self) -> OperationalSnapshot:
        """Return only the four approved aggregates, never queue records."""

        try:
            with self.pool.connection() as connection:
                connection.read_only = True
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute(
                        TRANSACTION_CONTRACT_SQL,
                        (f"{self.statement_timeout_ms}ms",),
                    )
                    contract_row = cursor.fetchone()
                    self._require_transaction_contract(contract_row)
                    cursor.execute(QUEUE_SNAPSHOT_SQL)
                    rows = cursor.fetchall()
            return self._parse_snapshot(rows)
        except OperationalSnapshotUnavailable:
            raise
        except Exception:
            raise OperationalSnapshotUnavailable("operational snapshot is unavailable") from None

    def _require_transaction_contract(self, row: object) -> None:
        if not isinstance(row, tuple) or len(row) != 5:
            self._reject()
        (
            configured_timeout,
            transaction_read_only,
            current_user,
            session_user,
            default_read_only,
        ) = cast(tuple[object, object, object, object, object], row)
        if (
            configured_timeout != self.statement_timeout_ms
            or transaction_read_only is not True
            or current_user != "schemabridge_observer"
            or session_user != "schemabridge_observer"
            or default_read_only is not True
        ):
            self._reject()

    def _parse_snapshot(self, rows: object) -> OperationalSnapshot:
        if not isinstance(rows, list) or len(rows) != len(OPERATIONAL_QUEUES):
            self._reject()
        observed: dict[OperationalQueue, QueueOperationalSnapshot] = {}
        for raw_row in rows:
            if not isinstance(raw_row, tuple) or len(raw_row) != 3:
                self._reject()
            raw_queue, raw_depth, raw_age = cast(tuple[object, object, object], raw_row)
            if (
                not isinstance(raw_queue, str)
                or not isinstance(raw_depth, int)
                or not isinstance(raw_age, int | float)
            ):
                self._reject()
            try:
                queue = OperationalQueue(raw_queue)
                item = QueueOperationalSnapshot(
                    queue=queue,
                    depth=raw_depth,
                    oldest_due_age_seconds=raw_age,
                )
            except (TypeError, ValueError):
                self._reject()
            if queue in observed:
                self._reject()
            observed[queue] = item
        if set(observed) != set(OPERATIONAL_QUEUES):
            self._reject()
        return OperationalSnapshot(queues=tuple(observed[queue] for queue in OPERATIONAL_QUEUES))

    @staticmethod
    def _reject() -> NoReturn:
        raise OperationalSnapshotUnavailable("operational snapshot is unavailable")
