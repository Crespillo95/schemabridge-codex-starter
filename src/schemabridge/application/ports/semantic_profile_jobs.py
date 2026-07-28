"""Ports for the reconciler-to-worker aggregate join-profile queue."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol, runtime_checkable

from schemabridge.domain.connectors import MAX_ROUTE_REVISION
from schemabridge.domain.joins import RelationshipProfile
from schemabridge.domain.semantic_profile_jobs import (
    SemanticJoinProfileFailureCode,
    SemanticJoinProfileJob,
    SemanticJoinProfileProposal,
    SemanticJoinProfileSubmission,
    SemanticJoinProfileTargetRef,
    digest_semantic_join_profile_capability,
)

_PROFILE_JOB_ID = re.compile(r"^profile_job_[0-9a-f]{64}$")
_PROFILE_WORKER_ID = re.compile(r"^[a-z][a-z0-9_-]{2,199}$")


class SemanticJoinProfileQueueErrorCode(StrEnum):
    """Sanitized queue failures without SQL, DSNs, capabilities, or source values."""

    NOT_FOUND = "semantic_profile_job_not_found"
    IDEMPOTENCY_CONFLICT = "semantic_profile_job_idempotency_conflict"
    CAPACITY_EXCEEDED = "semantic_profile_job_capacity_exceeded"
    STATE_CONFLICT = "semantic_profile_job_state_conflict"
    LEASE_CONFLICT = "semantic_profile_job_lease_conflict"
    SCHEMA_MISMATCH = "semantic_profile_job_schema_mismatch"
    STORE_UNAVAILABLE = "semantic_profile_job_store_unavailable"
    INVALID_RESPONSE = "semantic_profile_job_invalid_response"


class SemanticJoinProfileQueueError(RuntimeError):
    """Stable persistence failure safe for worker/reconciler logs."""

    def __init__(
        self,
        code: SemanticJoinProfileQueueErrorCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


class SemanticJoinProfileSourceCancelled(RuntimeError):
    """Cooperative cancellation before a protected profile source operation."""


class SemanticJoinProfileClockPort(Protocol):
    def now(self) -> datetime:
        """Return one timezone-aware current instant."""


@dataclass(frozen=True, slots=True)
class SemanticJoinProfileRouteContext:
    """Exact lease and public target coordinates for one private route lookup."""

    job_id: str
    workspace_id: str
    worker_id: str
    lease_capability: str = field(repr=False)
    fencing_token: int
    execution_target: SemanticJoinProfileTargetRef
    connector_contract_version: int

    def __post_init__(self) -> None:
        if (
            _PROFILE_JOB_ID.fullmatch(self.job_id) is None
            or self.execution_target.workspace_id != self.workspace_id
            or _PROFILE_WORKER_ID.fullmatch(self.worker_id) is None
            or type(self.fencing_token) is not int
            or not 1 <= self.fencing_token <= MAX_ROUTE_REVISION
            or type(self.connector_contract_version) is not int
            or not 1 <= self.connector_contract_version <= MAX_ROUTE_REVISION
        ):
            raise ValueError("semantic profile connector route context is invalid")
        digest_semantic_join_profile_capability(self.lease_capability)

    @classmethod
    def from_claim(
        cls,
        job: SemanticJoinProfileJob,
        *,
        worker_id: str,
        lease_capability: str,
    ) -> SemanticJoinProfileRouteContext:
        """Build a context only from the exact current leased job."""

        lease = job.lease
        target = job.execution_target
        contract_version = job.connector_contract_version
        if (
            lease is None
            or target is None
            or contract_version is None
            or lease.worker_id != worker_id
            or lease.capability_digest != digest_semantic_join_profile_capability(lease_capability)
            or lease.fencing_token != job.fencing_token
            or target.workspace_id != job.workspace_id
            or target.connection_id != job.connection_id
        ):
            raise ValueError("semantic profile connector route context is incomplete")
        return cls(
            job_id=job.job_id,
            workspace_id=job.workspace_id,
            worker_id=worker_id,
            lease_capability=lease_capability,
            fencing_token=job.fencing_token,
            execution_target=target,
            connector_contract_version=contract_version,
        )


@runtime_checkable
class SemanticJoinProfileEvidencePort(Protocol):
    """Connection-qualified aggregate evidence used by one semantic scan."""

    def profile_bound(
        self,
        proposal: SemanticJoinProfileProposal,
    ) -> RelationshipProfile:
        """Profile only the exact proposal and catalog connection."""


@runtime_checkable
class SemanticJoinProfileEvidenceFactoryPort(Protocol):
    """Bind aggregate source evidence to one exact current profile-job lease."""

    def for_claim(
        self,
        context: SemanticJoinProfileRouteContext,
        *,
        should_continue: Callable[[], bool],
    ) -> SemanticJoinProfileEvidencePort:
        """Return an evidence port that cannot outlive or redirect this claim."""


@runtime_checkable
class BatchSemanticJoinProfileEvidencePort(SemanticJoinProfileEvidencePort, Protocol):
    """Connection-qualified batch evidence without unbound proposal fallback."""

    def profiles_bound(
        self,
        proposals: tuple[SemanticJoinProfileProposal, ...],
    ) -> tuple[RelationshipProfile, ...]:
        """Profile a bounded batch while retaining every connection identity."""


class SemanticJoinProfileQueuePort(Protocol):
    """Exact, bounded, tenant-scoped profile-job persistence."""

    def enqueue(
        self,
        workspace_id: str,
        scan_id: str,
        proposal: SemanticJoinProfileProposal,
        *,
        execution_target: SemanticJoinProfileTargetRef,
        requested_at: datetime,
        max_attempts: int = 5,
    ) -> SemanticJoinProfileSubmission:
        """Create one exact request or return its byte-equivalent replay."""

    def load(
        self,
        workspace_id: str,
        scan_id: str,
        proposal_fingerprint: str,
    ) -> SemanticJoinProfileJob | None:
        """Load only the exact workspace, scan, and proposal identity."""

    def reclaim_expired(
        self,
        *,
        limit: int,
        retention: timedelta,
    ) -> int:
        """Requeue a bounded set of expired leases across governed targets."""

    def claim_next(
        self,
        *,
        worker_id: str,
        lease_capability: str,
        lease_duration: timedelta,
    ) -> SemanticJoinProfileJob | None:
        """Claim one due target-bearing job for dynamic connector resolution."""

    def heartbeat(
        self,
        workspace_id: str,
        job_id: str,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
        lease_duration: timedelta,
    ) -> SemanticJoinProfileJob:
        """Extend only the exact current capability and fencing token."""

    def complete(
        self,
        workspace_id: str,
        job_id: str,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
        profile: RelationshipProfile,
        retention: timedelta,
    ) -> SemanticJoinProfileJob:
        """Persist only the validated aggregate profile and close the lease."""

    def fail(
        self,
        workspace_id: str,
        job_id: str,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
        code: SemanticJoinProfileFailureCode,
        retry_delay: timedelta | None,
        retention: timedelta | None,
    ) -> SemanticJoinProfileJob:
        """Schedule a finite retry or terminally fail the exact lease."""


__all__ = [
    "BatchSemanticJoinProfileEvidencePort",
    "SemanticJoinProfileClockPort",
    "SemanticJoinProfileEvidenceFactoryPort",
    "SemanticJoinProfileEvidencePort",
    "SemanticJoinProfileQueueError",
    "SemanticJoinProfileQueueErrorCode",
    "SemanticJoinProfileQueuePort",
    "SemanticJoinProfileRouteContext",
    "SemanticJoinProfileSourceCancelled",
]
