"""Persistence port and sanitized failures for durable background jobs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Generic, Protocol, TypeVar

from schemabridge.domain.background_jobs import (
    BackgroundJob,
    JobFailureCode,
    JobResultSummary,
    JobSubmissionResult,
)

OperationResultT = TypeVar("OperationResultT")


class JobStoreErrorCode(StrEnum):
    """Stable failures safe to translate at API and worker boundaries."""

    NOT_FOUND = "job_not_found"
    IDEMPOTENCY_CONFLICT = "job_idempotency_conflict"
    CAPACITY_EXCEEDED = "job_capacity_exceeded"
    STATE_CONFLICT = "job_state_conflict"
    LEASE_CONFLICT = "job_lease_conflict"
    SCHEMA_MISMATCH = "job_store_schema_mismatch"
    STORE_UNAVAILABLE = "job_store_unavailable"
    INVALID_RESPONSE = "job_store_invalid_response"


class JobStoreError(RuntimeError):
    """Sanitized persistence failure without SQL or database details."""

    def __init__(self, code: JobStoreErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class LeaseHeartbeatSupervisorError(RuntimeError):
    """Sanitized failure when a lease cannot be safely maintained."""


@dataclass(frozen=True, slots=True)
class LeaseHeartbeatRunResult(Generic[OperationResultT]):
    """Operation value plus the final, freshly heartbeated claim."""

    value: OperationResultT
    claim: BackgroundJob


class LeaseHeartbeatSupervisorPort(Protocol):
    """Maintain one lease on a separate heartbeat path during bounded work."""

    def run(
        self,
        *,
        claim: BackgroundJob,
        worker_id: str,
        lease_token: str,
        lease_duration: timedelta,
        heartbeat_interval: timedelta,
        operation: Callable[[], OperationResultT],
    ) -> LeaseHeartbeatRunResult[OperationResultT]:
        """Run work while renewing the exact lease, then return a fresh claim."""


class BackgroundJobApiStorePort(Protocol):
    """Tenant-scoped API transitions implemented by the control-plane store."""

    def submit(self, job: BackgroundJob) -> JobSubmissionResult:
        """Create job plus submitted event, or replay the exact idempotent request."""

    def load_by_idempotency(
        self,
        workspace_id: str,
        submitting_actor_id: str,
        idempotency_digest: str,
    ) -> BackgroundJob | None:
        """Load one exact workspace, submitter, and idempotency identity."""

    def load(
        self,
        workspace_id: str,
        job_id: str,
        *,
        submitting_actor_id: str | None = None,
    ) -> BackgroundJob | None:
        """Load one exact workspace/optional-submitter scoped job."""

    def request_cancellation(
        self,
        workspace_id: str,
        job_id: str,
        *,
        submitting_actor_id: str | None,
        requested_at: datetime,
    ) -> BackgroundJob | None:
        """Atomically request cancellation and append its immutable event."""


class BackgroundJobStorePort(BackgroundJobApiStorePort, Protocol):
    """Full API and worker queue boundary for the control-plane store."""

    def expire_authorizations(self, *, limit: int = 100) -> int:
        """Fail bounded expired waiting jobs or expired abandoned leases using database time."""

    def reap_exhausted_leases(self, *, limit: int = 100) -> int:
        """Dead-letter a bounded batch of expired final leases using database time."""

    def claim_next(
        self,
        *,
        worker_id: str,
        lease_token: str,
        lease_duration: timedelta,
    ) -> BackgroundJob | None:
        """Claim one due job; the store derives both lease times from database time."""

    def heartbeat(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        fencing_token: int,
        lease_duration: timedelta,
    ) -> BackgroundJob:
        """Extend an exact lease using database time, raw-token hashing, and its fence."""

    def succeed(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        fencing_token: int,
        result: JobResultSummary,
    ) -> BackgroundJob:
        """Atomically persist success or close an authorization expired at commit time."""

    def fail(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        fencing_token: int,
        code: JobFailureCode,
        failed_at: datetime,
        retry_at: datetime | None = None,
    ) -> BackgroundJob:
        """Atomically classify failure, schedule retry, or make it terminal."""

    def acknowledge_cancellation(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        fencing_token: int,
        cancelled_at: datetime,
    ) -> BackgroundJob:
        """Make cooperative cancellation terminal for the exact lease."""
