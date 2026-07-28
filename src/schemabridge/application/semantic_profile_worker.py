"""One bounded worker iteration for aggregate-only semantic join profiling."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from schemabridge.application.ports.relationships import (
    RelationshipErrorCode,
    RelationshipWorkflowError,
)
from schemabridge.application.ports.semantic_profile_jobs import (
    SemanticJoinProfileClockPort,
    SemanticJoinProfileEvidenceFactoryPort,
    SemanticJoinProfileQueueError,
    SemanticJoinProfileQueueErrorCode,
    SemanticJoinProfileQueuePort,
    SemanticJoinProfileRouteContext,
    SemanticJoinProfileSourceCancelled,
)
from schemabridge.domain.joins import RelationshipProfile
from schemabridge.domain.semantic_profile_jobs import (
    SemanticJoinProfileFailureCode,
    SemanticJoinProfileJob,
    SemanticJoinProfileJobStatus,
    digest_semantic_join_profile_capability,
    semantic_join_profile_claim_matches,
    semantic_join_profile_retry_delay,
    validate_semantic_join_profile_proposal,
    validate_semantic_join_profile_result,
)

_SAFE_WORKER_ID = re.compile(r"^[a-z][a-z0-9_-]{2,199}$")


def _never_stop() -> bool:
    return False


class SemanticJoinProfileWorkerOutcome(StrEnum):
    IDLE = "idle"
    COMPLETED = "completed"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class SemanticJoinProfileWorkerResult:
    """Sanitized observable outcome without proposal, source, or lease material."""

    outcome: SemanticJoinProfileWorkerOutcome
    job_id: str | None = None
    status: SemanticJoinProfileJobStatus | None = None
    attempts: int = 0
    result_fingerprint: str | None = None
    failure_code: SemanticJoinProfileFailureCode | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.attempts <= 100:
            raise ValueError("semantic profile worker attempt count is invalid")
        if self.outcome is SemanticJoinProfileWorkerOutcome.IDLE:
            if (
                self.job_id is not None
                or self.status is not None
                or self.attempts
                or self.result_fingerprint is not None
                or self.failure_code is not None
            ):
                raise ValueError("idle semantic profile result cannot expose job state")
        elif self.outcome is SemanticJoinProfileWorkerOutcome.COMPLETED:
            if (
                self.job_id is None
                or self.status is not SemanticJoinProfileJobStatus.COMPLETED
                or self.attempts < 1
                or self.result_fingerprint is None
                or self.failure_code is not None
            ):
                raise ValueError("completed semantic profile result is inconsistent")
        elif self.outcome is SemanticJoinProfileWorkerOutcome.RETRY_SCHEDULED:
            if (
                self.job_id is None
                or self.status is not SemanticJoinProfileJobStatus.RETRY_WAIT
                or self.attempts < 1
                or self.result_fingerprint is not None
                or self.failure_code is None
            ):
                raise ValueError("retry semantic profile result is inconsistent")
        elif self.outcome is SemanticJoinProfileWorkerOutcome.FAILED:
            if (
                self.job_id is None
                or self.status is not SemanticJoinProfileJobStatus.FAILED
                or self.attempts < 1
                or self.result_fingerprint is not None
                or self.failure_code is None
            ):
                raise ValueError("failed semantic profile result is inconsistent")
        elif self.job_id is None:
            if (
                self.status is not None
                or self.attempts
                or self.result_fingerprint is not None
                or self.failure_code is not None
            ):
                raise ValueError("unclaimed stopped result cannot expose job state")
        elif (
            self.status
            not in {
                SemanticJoinProfileJobStatus.RETRY_WAIT,
                SemanticJoinProfileJobStatus.FAILED,
            }
            or self.failure_code is not SemanticJoinProfileFailureCode.SHUTDOWN_REQUESTED
            or self.result_fingerprint is not None
        ):
            raise ValueError("claimed stopped semantic profile result is inconsistent")


class SemanticJoinProfileWorkerErrorCode(StrEnum):
    """Failures where no trustworthy queue transition can be claimed."""

    STORE_UNAVAILABLE = "semantic_profile_worker_store_unavailable"
    INVALID_CAPABILITY = "semantic_profile_worker_invalid_capability"
    INVALID_CLAIM = "semantic_profile_worker_invalid_claim"
    LEASE_LOST = "semantic_profile_worker_lease_lost"
    INVALID_RESPONSE = "semantic_profile_worker_invalid_response"
    INVALID_STOP_SIGNAL = "semantic_profile_worker_invalid_stop_signal"


class SemanticJoinProfileWorkerError(RuntimeError):
    """Sanitized worker failure without exception, DSN, or source detail."""

    def __init__(
        self,
        code: SemanticJoinProfileWorkerErrorCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RunOneSemanticJoinProfile:
    """Claim, profile through the read-only port, and close at most one job."""

    queue: SemanticJoinProfileQueuePort = field(repr=False)
    evidence_factory: SemanticJoinProfileEvidenceFactoryPort = field(repr=False)
    clock: SemanticJoinProfileClockPort = field(repr=False)
    capability_factory: Callable[[], str] = field(repr=False)
    worker_id: str
    lease_duration: timedelta = timedelta(seconds=120)
    retention: timedelta = timedelta(days=30)
    maintenance_batch_size: int = 100
    stop_requested: Callable[[], bool] = field(default=_never_stop, repr=False)

    def __post_init__(self) -> None:
        if _SAFE_WORKER_ID.fullmatch(self.worker_id) is None:
            raise ValueError("semantic profile worker id must be a bounded inert identifier")
        if not timedelta(seconds=10) <= self.lease_duration <= timedelta(minutes=5):
            raise ValueError("semantic profile lease duration is outside the supported bound")
        if not timedelta(days=1) <= self.retention <= timedelta(days=366):
            raise ValueError("semantic profile retention is outside the supported bound")
        if not 1 <= self.maintenance_batch_size <= 1_000:
            raise ValueError("semantic profile maintenance batch is outside the supported bound")

    def execute(self) -> SemanticJoinProfileWorkerResult:
        if self._stopping():
            return SemanticJoinProfileWorkerResult(
                outcome=SemanticJoinProfileWorkerOutcome.STOPPED,
            )
        self._reclaim_expired()
        capability = self._new_capability()
        try:
            claimed = self.queue.claim_next(
                worker_id=self.worker_id,
                lease_capability=capability,
                lease_duration=self.lease_duration,
            )
        except SemanticJoinProfileQueueError as error:
            raise _queue_failure(error) from None
        except Exception:
            raise _store_unavailable() from None
        if claimed is None:
            return SemanticJoinProfileWorkerResult(
                outcome=SemanticJoinProfileWorkerOutcome.IDLE,
            )
        current = self._validate_claim(claimed, capability)
        if self._stopping():
            return self._record_failure(
                current,
                capability,
                SemanticJoinProfileFailureCode.SHUTDOWN_REQUESTED,
            )
        current = self._heartbeat(current, capability)
        try:
            proposal = validate_semantic_join_profile_proposal(current.bound_proposal)
        except Exception:
            return self._record_failure(
                current,
                capability,
                SemanticJoinProfileFailureCode.PROPOSAL_INVALID,
            )
        execution_target = current.execution_target
        if execution_target is None or proposal.connection_id != execution_target.connection_id:
            return self._record_failure(
                current,
                capability,
                SemanticJoinProfileFailureCode.SOURCE_CONNECTION_MISMATCH,
            )
        if self._stopping():
            return self._record_failure(
                current,
                capability,
                SemanticJoinProfileFailureCode.SHUTDOWN_REQUESTED,
            )
        try:
            context = SemanticJoinProfileRouteContext.from_claim(
                current,
                worker_id=self.worker_id,
                lease_capability=capability,
            )
            relationships = self.evidence_factory.for_claim(
                context,
                should_continue=self._should_continue,
            )
            profile = relationships.profile_bound(proposal)
            profile = validate_semantic_join_profile_result(profile)
        except SemanticJoinProfileSourceCancelled:
            return self._record_failure(
                current,
                capability,
                SemanticJoinProfileFailureCode.SHUTDOWN_REQUESTED,
            )
        except RelationshipWorkflowError as error:
            return self._record_failure(
                current,
                capability,
                _relationship_failure_code(error.code),
            )
        except Exception:
            return self._record_failure(
                current,
                capability,
                SemanticJoinProfileFailureCode.UNEXPECTED_WORKER_FAILURE,
            )
        if self._stopping():
            return self._record_failure(
                current,
                capability,
                SemanticJoinProfileFailureCode.SHUTDOWN_REQUESTED,
            )
        current = self._heartbeat(current, capability)
        return self._complete(current, capability, profile)

    def _reclaim_expired(self) -> None:
        try:
            count = self.queue.reclaim_expired(
                limit=self.maintenance_batch_size,
                retention=self.retention,
            )
        except SemanticJoinProfileQueueError as error:
            raise _queue_failure(error) from None
        except Exception:
            raise _store_unavailable() from None
        if not 0 <= count <= self.maintenance_batch_size:
            raise SemanticJoinProfileWorkerError(
                SemanticJoinProfileWorkerErrorCode.INVALID_RESPONSE,
                "semantic profile queue returned an invalid maintenance count",
            )

    def _new_capability(self) -> str:
        try:
            capability = self.capability_factory()
            digest_semantic_join_profile_capability(capability)
        except Exception:
            raise SemanticJoinProfileWorkerError(
                SemanticJoinProfileWorkerErrorCode.INVALID_CAPABILITY,
                "semantic profile capability generation failed",
            ) from None
        return capability

    def _validate_claim(
        self,
        claimed: SemanticJoinProfileJob,
        capability: str,
    ) -> SemanticJoinProfileJob:
        checked = _validated_job(claimed)
        if (
            checked.status is not SemanticJoinProfileJobStatus.LEASED
            or checked.lease is None
            or not semantic_join_profile_claim_matches(
                checked,
                worker_id=self.worker_id,
                lease_capability=capability,
                fencing_token=checked.fencing_token,
                at=self._now(),
            )
        ):
            raise SemanticJoinProfileWorkerError(
                SemanticJoinProfileWorkerErrorCode.INVALID_CLAIM,
                "semantic profile queue returned an invalid claim",
            )
        return checked

    def _heartbeat(
        self,
        expected: SemanticJoinProfileJob,
        capability: str,
    ) -> SemanticJoinProfileJob:
        try:
            current = self.queue.heartbeat(
                expected.workspace_id,
                expected.job_id,
                worker_id=self.worker_id,
                lease_capability=capability,
                fencing_token=expected.fencing_token,
                lease_duration=self.lease_duration,
            )
        except SemanticJoinProfileQueueError as error:
            if error.code in {
                SemanticJoinProfileQueueErrorCode.LEASE_CONFLICT,
                SemanticJoinProfileQueueErrorCode.STATE_CONFLICT,
                SemanticJoinProfileQueueErrorCode.NOT_FOUND,
            }:
                raise _lease_lost() from None
            raise _queue_failure(error) from None
        except Exception:
            raise _store_unavailable() from None
        checked = _validated_job(current)
        if (
            not _same_job_identity(checked, expected)
            or checked.attempts != expected.attempts
            or checked.fencing_token != expected.fencing_token
            or not semantic_join_profile_claim_matches(
                checked,
                worker_id=self.worker_id,
                lease_capability=capability,
                fencing_token=expected.fencing_token,
                at=self._now(),
            )
        ):
            raise _lease_lost()
        return checked

    def _complete(
        self,
        expected: SemanticJoinProfileJob,
        capability: str,
        profile: RelationshipProfile,
    ) -> SemanticJoinProfileWorkerResult:
        checked_profile = RelationshipProfile.model_validate(profile.model_dump(mode="json"))
        try:
            completed = self.queue.complete(
                expected.workspace_id,
                expected.job_id,
                worker_id=self.worker_id,
                lease_capability=capability,
                fencing_token=expected.fencing_token,
                profile=checked_profile,
                retention=self.retention,
            )
        except SemanticJoinProfileQueueError as error:
            if error.code in {
                SemanticJoinProfileQueueErrorCode.LEASE_CONFLICT,
                SemanticJoinProfileQueueErrorCode.STATE_CONFLICT,
                SemanticJoinProfileQueueErrorCode.NOT_FOUND,
            }:
                raise _lease_lost() from None
            raise _queue_failure(error) from None
        except Exception:
            raise _store_unavailable() from None
        checked = _validated_job(completed)
        if (
            not _same_job_identity(checked, expected)
            or checked.status is not SemanticJoinProfileJobStatus.COMPLETED
            or checked.attempts != expected.attempts
            or checked.fencing_token != expected.fencing_token
            or checked.result is None
            or checked.result.profile != checked_profile
        ):
            raise SemanticJoinProfileWorkerError(
                SemanticJoinProfileWorkerErrorCode.INVALID_RESPONSE,
                "semantic profile queue returned another completion",
            )
        return SemanticJoinProfileWorkerResult(
            outcome=SemanticJoinProfileWorkerOutcome.COMPLETED,
            job_id=checked.job_id,
            status=checked.status,
            attempts=checked.attempts,
            result_fingerprint=checked.result.fingerprint,
        )

    def _record_failure(
        self,
        expected: SemanticJoinProfileJob,
        capability: str,
        code: SemanticJoinProfileFailureCode,
    ) -> SemanticJoinProfileWorkerResult:
        retryable = (
            code
            in {
                SemanticJoinProfileFailureCode.SOURCE_UNAVAILABLE,
                SemanticJoinProfileFailureCode.SOURCE_TIMEOUT,
                SemanticJoinProfileFailureCode.LEASE_EXPIRED,
                SemanticJoinProfileFailureCode.SHUTDOWN_REQUESTED,
            }
            and expected.attempts < expected.max_attempts
        )
        retry_delay = semantic_join_profile_retry_delay(expected.attempts) if retryable else None
        retention = None if retryable else self.retention
        try:
            failed = self.queue.fail(
                expected.workspace_id,
                expected.job_id,
                worker_id=self.worker_id,
                lease_capability=capability,
                fencing_token=expected.fencing_token,
                code=code,
                retry_delay=retry_delay,
                retention=retention,
            )
        except SemanticJoinProfileQueueError as error:
            if error.code in {
                SemanticJoinProfileQueueErrorCode.LEASE_CONFLICT,
                SemanticJoinProfileQueueErrorCode.STATE_CONFLICT,
                SemanticJoinProfileQueueErrorCode.NOT_FOUND,
            }:
                raise _lease_lost() from None
            raise _queue_failure(error) from None
        except Exception:
            raise _store_unavailable() from None
        checked = _validated_job(failed)
        expected_status = (
            SemanticJoinProfileJobStatus.RETRY_WAIT
            if retryable
            else SemanticJoinProfileJobStatus.FAILED
        )
        if (
            not _same_job_identity(checked, expected)
            or checked.status is not expected_status
            or checked.attempts != expected.attempts
            or checked.failure is None
            or checked.failure.code is not code
        ):
            raise SemanticJoinProfileWorkerError(
                SemanticJoinProfileWorkerErrorCode.INVALID_RESPONSE,
                "semantic profile queue returned another failure transition",
            )
        outcome = (
            SemanticJoinProfileWorkerOutcome.STOPPED
            if code is SemanticJoinProfileFailureCode.SHUTDOWN_REQUESTED
            else SemanticJoinProfileWorkerOutcome.RETRY_SCHEDULED
            if retryable
            else SemanticJoinProfileWorkerOutcome.FAILED
        )
        return SemanticJoinProfileWorkerResult(
            outcome=outcome,
            job_id=checked.job_id,
            status=checked.status,
            attempts=checked.attempts,
            failure_code=code,
        )

    def _now(self) -> datetime:
        try:
            instant = self.clock.now()
        except Exception:
            raise SemanticJoinProfileWorkerError(
                SemanticJoinProfileWorkerErrorCode.INVALID_RESPONSE,
                "semantic profile clock is unavailable",
            ) from None
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise SemanticJoinProfileWorkerError(
                SemanticJoinProfileWorkerErrorCode.INVALID_RESPONSE,
                "semantic profile clock returned a naive instant",
            )
        return instant

    def _stopping(self) -> bool:
        try:
            stopping = self.stop_requested()
        except Exception:
            raise SemanticJoinProfileWorkerError(
                SemanticJoinProfileWorkerErrorCode.INVALID_STOP_SIGNAL,
                "semantic profile stop signal is unavailable",
            ) from None
        if not isinstance(stopping, bool):
            raise SemanticJoinProfileWorkerError(
                SemanticJoinProfileWorkerErrorCode.INVALID_STOP_SIGNAL,
                "semantic profile stop signal is invalid",
            )
        return stopping

    def _should_continue(self) -> bool:
        return not self._stopping()


def _relationship_failure_code(
    code: RelationshipErrorCode,
) -> SemanticJoinProfileFailureCode:
    if code in {
        RelationshipErrorCode.EVIDENCE_UNAVAILABLE,
        RelationshipErrorCode.CATALOG_UNAVAILABLE,
    }:
        return SemanticJoinProfileFailureCode.SOURCE_UNAVAILABLE
    return SemanticJoinProfileFailureCode.EVIDENCE_INVALID


def _validated_job(value: object) -> SemanticJoinProfileJob:
    try:
        if not isinstance(value, SemanticJoinProfileJob):
            raise TypeError("semantic profile queue returned another value")
        return SemanticJoinProfileJob.model_validate(value.model_dump(mode="python"))
    except Exception:
        raise SemanticJoinProfileWorkerError(
            SemanticJoinProfileWorkerErrorCode.INVALID_RESPONSE,
            "semantic profile queue returned an invalid job",
        ) from None


def _same_job_identity(
    left: SemanticJoinProfileJob,
    right: SemanticJoinProfileJob,
) -> bool:
    return (
        left.job_id == right.job_id
        and left.workspace_id == right.workspace_id
        and left.scan_id == right.scan_id
        and left.connection_id == right.connection_id
        and left.execution_target == right.execution_target
        and left.connector_contract_version == right.connector_contract_version
        and left.proposal_fingerprint == right.proposal_fingerprint
        and left.proposal == right.proposal
        and left.max_attempts == right.max_attempts
        and left.requested_at == right.requested_at
    )


def _queue_failure(
    error: SemanticJoinProfileQueueError,
) -> SemanticJoinProfileWorkerError:
    if error.code in {
        SemanticJoinProfileQueueErrorCode.LEASE_CONFLICT,
        SemanticJoinProfileQueueErrorCode.STATE_CONFLICT,
        SemanticJoinProfileQueueErrorCode.NOT_FOUND,
    }:
        return _lease_lost()
    return _store_unavailable()


def _store_unavailable() -> SemanticJoinProfileWorkerError:
    return SemanticJoinProfileWorkerError(
        SemanticJoinProfileWorkerErrorCode.STORE_UNAVAILABLE,
        "semantic profile queue is unavailable",
    )


def _lease_lost() -> SemanticJoinProfileWorkerError:
    return SemanticJoinProfileWorkerError(
        SemanticJoinProfileWorkerErrorCode.LEASE_LOST,
        "semantic profile lease was lost",
    )


__all__ = [
    "RunOneSemanticJoinProfile",
    "SemanticJoinProfileWorkerError",
    "SemanticJoinProfileWorkerErrorCode",
    "SemanticJoinProfileWorkerOutcome",
    "SemanticJoinProfileWorkerResult",
]
