"""Closed application contracts for aggregate control-plane queue observations."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

MAX_OPERATIONAL_QUEUE_DEPTH = 1_000_000_000_000
MAX_OPERATIONAL_QUEUE_AGE_SECONDS = 1_000_000_000_000.0


class OperationalQueue(StrEnum):
    """The only queue identities exposed by the operational observer."""

    EXECUTION = "execution"
    CATALOG = "catalog"
    PROFILE = "profile"
    RECONCILIATION = "reconciliation"


OPERATIONAL_QUEUES = tuple(OperationalQueue)


class OperationalSnapshotUnavailable(RuntimeError):
    """A safe aggregate queue snapshot could not be produced or published."""


@dataclass(frozen=True, slots=True)
class QueueOperationalSnapshot:
    """One bounded aggregate with no tenant, job, asset, or row identity."""

    queue: OperationalQueue
    depth: int
    oldest_due_age_seconds: float

    def __post_init__(self) -> None:
        if not isinstance(self.queue, OperationalQueue):
            raise ValueError("operational queue is invalid")
        if (
            isinstance(self.depth, bool)
            or not isinstance(self.depth, int)
            or not 0 <= self.depth <= MAX_OPERATIONAL_QUEUE_DEPTH
        ):
            raise ValueError("operational queue depth is invalid")
        age = self.oldest_due_age_seconds
        if (
            isinstance(age, bool)
            or not isinstance(age, int | float)
            or not math.isfinite(age)
            or not 0 <= age <= MAX_OPERATIONAL_QUEUE_AGE_SECONDS
        ):
            raise ValueError("operational queue age is invalid")


@dataclass(frozen=True, slots=True)
class OperationalSnapshot:
    """The exact four aggregate queue observations in canonical order."""

    queues: tuple[QueueOperationalSnapshot, ...]

    def __post_init__(self) -> None:
        identities = tuple(item.queue for item in self.queues)
        if identities != OPERATIONAL_QUEUES:
            raise ValueError("operational snapshot queue set is invalid")


class OperationalSnapshotReader(Protocol):
    """Read one bounded snapshot from the control plane."""

    def read(self) -> OperationalSnapshot:
        """Return exactly one aggregate for every approved queue."""


class OperationalMetricsSink(Protocol):
    """Publish samples into a registry whose names and labels are already closed."""

    def set_gauges_atomically(
        self,
        samples: Sequence[tuple[str, Mapping[str, str], float]],
    ) -> None:
        """Commit a complete validated gauge batch without exposing partial state."""
