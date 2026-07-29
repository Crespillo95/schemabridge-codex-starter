"""Application use case for publishing aggregate queue observations."""

from __future__ import annotations

from dataclasses import dataclass

from schemabridge.application.ports.operational_snapshot import (
    OperationalMetricsSink,
    OperationalSnapshot,
    OperationalSnapshotReader,
    OperationalSnapshotUnavailable,
)

QUEUE_DEPTH_METRIC = "schemabridge_queue_depth"
QUEUE_OLDEST_AGE_METRIC = "schemabridge_queue_oldest_age_seconds"


@dataclass(frozen=True, slots=True)
class RefreshOperationalSnapshot:
    """Refresh only the two approved queue gauges from a validated snapshot."""

    reader: OperationalSnapshotReader
    metrics: OperationalMetricsSink

    def execute(self) -> OperationalSnapshot:
        """Read and publish the closed aggregate snapshot, failing without defaults."""

        try:
            snapshot = self.reader.read()
            if not isinstance(snapshot, OperationalSnapshot):
                raise TypeError("unexpected operational snapshot type")
            samples: list[tuple[str, dict[str, str], float]] = []
            for item in snapshot.queues:
                labels = {"queue": item.queue.value}
                samples.append(
                    (
                        QUEUE_DEPTH_METRIC,
                        labels,
                        float(item.depth),
                    )
                )
                samples.append(
                    (
                        QUEUE_OLDEST_AGE_METRIC,
                        labels,
                        float(item.oldest_due_age_seconds),
                    )
                )
            self.metrics.set_gauges_atomically(samples)
        except Exception:
            raise OperationalSnapshotUnavailable("operational snapshot is unavailable") from None
        return snapshot
