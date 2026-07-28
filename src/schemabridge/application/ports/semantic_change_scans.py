"""Ports for durable semantic-change scan reconciliation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from schemabridge.domain.semantic_change import (
    SemanticChangeReport,
    SemanticEvidenceObservation,
)
from schemabridge.domain.semantic_change_scans import (
    SemanticChangeScanFailureCode,
    SemanticChangeScanRequest,
)


class SemanticChangeScanStoreErrorCode(StrEnum):
    """Sanitized persistence failures safe for the reconciler boundary."""

    NOT_FOUND = "semantic_scan_not_found"
    STATE_CONFLICT = "semantic_scan_state_conflict"
    LEASE_LOST = "semantic_scan_lease_lost"
    SCHEMA_MISMATCH = "semantic_scan_schema_mismatch"
    STORE_UNAVAILABLE = "semantic_scan_store_unavailable"
    INVALID_RESPONSE = "semantic_scan_invalid_store_response"


class SemanticChangeScanStoreError(RuntimeError):
    """Persistence failure without SQL, DSN, capability, or protected payload."""

    def __init__(
        self,
        code: SemanticChangeScanStoreErrorCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


class SemanticChangeScanRunnerErrorCode(StrEnum):
    """Closed read-only inspection failures returned by a scan runner."""

    REGISTRY_UNAVAILABLE = "registry_unavailable"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"
    DEPENDENCY_INDEX_INCOMPLETE = "dependency_index_incomplete"
    SOURCE_TIMEOUT = "source_timeout"
    STOP_REQUESTED = "stop_requested"
    INVALID_INSPECTION = "invalid_inspection"


class SemanticChangeScanRunnerError(RuntimeError):
    """Sanitized inspection failure without vendor or source detail."""

    def __init__(
        self,
        code: SemanticChangeScanRunnerErrorCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SemanticChangeScanInspection:
    """Read-only inspection output committed atomically by the scan store."""

    report: SemanticChangeReport
    observation: SemanticEvidenceObservation

    def __post_init__(self) -> None:
        if (
            self.observation.context != self.report.context
            or self.observation.fingerprint != self.report.observation_fingerprint
        ):
            raise ValueError("semantic scan inspection report and observation do not match")


class SemanticChangeScanClockPort(Protocol):
    def now(self) -> datetime:
        """Return one timezone-aware current instant."""


class SemanticChangeScanRunnerPort(Protocol):
    """Capture current evidence without persisting a report or mutating a source."""

    def inspect(
        self,
        request: SemanticChangeScanRequest,
        *,
        should_continue: Callable[[], bool],
    ) -> SemanticChangeScanInspection:
        """Inspect exactly the claimed trigger and cooperate with lease/shutdown checks."""


class SemanticChangeScanStorePort(Protocol):
    """Atomic lifecycle and report-commit boundary for the reconciler role."""

    def reclaim_expired(
        self,
        *,
        limit: int,
        retention: timedelta,
    ) -> int:
        """Requeue or terminally fail a bounded expired-lease batch using database time."""

    def supersede_obsolete(
        self,
        *,
        limit: int,
        retention: timedelta,
    ) -> int:
        """Supersede a bounded waiting batch that has an exact newer request."""

    def claim_next(
        self,
        *,
        reconciler_id: str,
        lease_capability: str,
        lease_duration: timedelta,
    ) -> SemanticChangeScanRequest | None:
        """Claim one due request; the store hashes the raw capability and uses database time."""

    def load(
        self,
        workspace_id: str,
        scan_id: str,
    ) -> SemanticChangeScanRequest | None:
        """Load one exact request for lease-loss and idempotent-completion checks."""

    def heartbeat(
        self,
        workspace_id: str,
        scan_id: str,
        *,
        reconciler_id: str,
        lease_capability: str,
        fencing_token: int,
        lease_duration: timedelta,
    ) -> SemanticChangeScanRequest:
        """Extend only the exact capability/fence using database time."""

    def supersede_if_obsolete(
        self,
        workspace_id: str,
        scan_id: str,
        *,
        reconciler_id: str,
        lease_capability: str,
        fencing_token: int,
        retention: timedelta,
    ) -> SemanticChangeScanRequest:
        """Return the owned lease unchanged or atomically bind it to an exact successor."""

    def complete(
        self,
        workspace_id: str,
        scan_id: str,
        *,
        reconciler_id: str,
        lease_capability: str,
        fencing_token: int,
        inspection: SemanticChangeScanInspection,
        completed_at: datetime,
        retain_until: datetime,
    ) -> SemanticChangeScanRequest:
        """Atomically persist report+observation and terminally complete the exact scan."""

    def fail(
        self,
        workspace_id: str,
        scan_id: str,
        *,
        reconciler_id: str,
        lease_capability: str,
        fencing_token: int,
        code: SemanticChangeScanFailureCode,
        failed_at: datetime,
        retry_at: datetime | None,
        retain_until: datetime | None,
    ) -> SemanticChangeScanRequest:
        """Schedule retry or terminal failure through the exact current lease."""


__all__ = [
    "SemanticChangeScanClockPort",
    "SemanticChangeScanInspection",
    "SemanticChangeScanRunnerError",
    "SemanticChangeScanRunnerErrorCode",
    "SemanticChangeScanRunnerPort",
    "SemanticChangeScanStoreError",
    "SemanticChangeScanStoreErrorCode",
    "SemanticChangeScanStorePort",
]
