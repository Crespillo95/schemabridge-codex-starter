"""One bounded, fenced background-worker iteration for governed preview jobs."""

from __future__ import annotations

import re
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from schemabridge.application.api_workflows import (
    WorkflowAccessStoreFactoryPort,
    WorkflowOrchestratorPort,
)
from schemabridge.application.ports.background_jobs import (
    BackgroundJobStorePort,
    JobStoreError,
    LeaseHeartbeatSupervisorError,
    LeaseHeartbeatSupervisorPort,
)
from schemabridge.application.ports.workflow_access import (
    WorkflowAccessError,
    WorkflowAccessErrorCode,
)
from schemabridge.application.ports.workflows import (
    WorkflowClockPort,
    WorkflowError,
    WorkflowErrorCode,
)
from schemabridge.domain.background_jobs import (
    BackgroundJob,
    JobExecutionTargetRef,
    JobFailureCode,
    JobFailureDisposition,
    JobRejectionCount,
    JobResultSummary,
    JobStatus,
    JobWorkflowAccessScope,
    classify_job_failure,
    digest_lease_token,
    job_retry_delay,
)
from schemabridge.domain.workflows import (
    AgentWorkflowDraft,
    ExecutionWorkflowDecision,
    RetryWorkflowDecision,
    WorkflowCheckpointKind,
    WorkflowDecisionAction,
    WorkflowDecisionKind,
    WorkflowFailure,
    WorkflowOperation,
    WorkflowStage,
    WorkflowTraceStatus,
)

_SAFE_WORKER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,199}$")
_AMBIGUOUS_EXTERNAL_OPERATIONS = frozenset(
    {
        WorkflowOperation.PREVIEW_EXECUTION,
        WorkflowOperation.REJECTION_INSPECTION,
    }
)
_TRANSIENT_WORKFLOW_FAILURE_CODES = {
    (
        WorkflowOperation.SQL_VALIDATION,
        "planning_context_unavailable",
    ): JobFailureCode.REGISTRY_UNAVAILABLE,
    (
        WorkflowOperation.PREVIEW_EXECUTION,
        "source_unavailable",
    ): JobFailureCode.SOURCE_UNAVAILABLE,
    (
        WorkflowOperation.REJECTION_INSPECTION,
        "source_unavailable",
    ): JobFailureCode.SOURCE_UNAVAILABLE,
    (
        WorkflowOperation.REJECTION_INSPECTION,
        "source_timeout",
    ): JobFailureCode.SOURCE_TIMEOUT,
    (
        WorkflowOperation.PREVIEW_EXECUTION,
        "source_timeout",
    ): JobFailureCode.SOURCE_TIMEOUT,
}
_TERMINAL_WORKFLOW_FAILURE_CODES = {
    (
        WorkflowOperation.SQL_VALIDATION,
        "stale_registry",
    ): JobFailureCode.REGISTRY_STALE,
    (
        WorkflowOperation.SQL_VALIDATION,
        "semantic_context_stale",
    ): JobFailureCode.SEMANTIC_CONTEXT_STALE,
    (
        WorkflowOperation.PREVIEW_EXECUTION,
        "semantic_context_stale",
    ): JobFailureCode.SEMANTIC_CONTEXT_STALE,
    (
        WorkflowOperation.REJECTION_INSPECTION,
        "semantic_context_stale",
    ): JobFailureCode.SEMANTIC_CONTEXT_STALE,
    (
        WorkflowOperation.PREVIEW_EXECUTION,
        "source_policy_rejected",
    ): JobFailureCode.SOURCE_POLICY_REJECTED,
    (
        WorkflowOperation.REJECTION_INSPECTION,
        "source_policy_rejected",
    ): JobFailureCode.SOURCE_POLICY_REJECTED,
}


class LeaseCapabilityFactoryPort(Protocol):
    """Generate one transient high-entropy raw lease capability."""

    def __call__(self) -> str:
        """Return a fresh capability that is never placed in worker results."""


@dataclass(frozen=True, slots=True)
class WorkerExecutionRouteContext:
    """Exact private-route coordinates for one currently leased execution job."""

    job_id: str
    workflow_id: str
    job_workspace_id: str
    connector_workspace_id: str
    worker_id: str
    lease_capability: str = field(repr=False)
    fencing_token: int
    execution_target: JobExecutionTargetRef
    connector_contract_version: int

    def __post_init__(self) -> None:
        if self.execution_target.workspace_id != self.connector_workspace_id:
            raise ValueError("managed execution connector workspace is inconsistent")

    @classmethod
    def from_claim(
        cls,
        job: BackgroundJob,
        *,
        worker_id: str,
        lease_capability: str,
    ) -> WorkerExecutionRouteContext:
        """Build context only from the exact public target persisted by the store."""

        target = job.authorization.execution_target
        lease = job.lease
        contract_version = job.connector_contract_version
        if (
            target is None
            or lease is None
            or contract_version is None
            or lease.worker_id != worker_id
            or lease.token_digest != digest_lease_token(lease_capability)
            or lease.fencing_token != job.last_fencing_token
            or target.workspace_id != job.authorization.workflow_workspace_id
        ):
            raise ValueError("managed execution job route context is incomplete")
        return cls(
            job_id=job.id,
            workflow_id=job.authorization.workflow_id,
            job_workspace_id=job.authorization.workspace_id,
            connector_workspace_id=target.workspace_id,
            worker_id=worker_id,
            lease_capability=lease_capability,
            fencing_token=lease.fencing_token,
            execution_target=target,
            connector_contract_version=contract_version,
        )


class WorkerOrchestratorFactoryPort(Protocol):
    """Compose a worker orchestrator, optionally bound to one private source route."""

    def __call__(
        self,
        workspace_id: str,
        actor_id: str,
        *,
        route_context: WorkerExecutionRouteContext | None = None,
    ) -> WorkflowOrchestratorPort:
        """Return the exact historical workflow boundary for this leased job."""


class WorkerIterationOutcome(StrEnum):
    """Bounded observable outcomes for one worker poll."""

    IDLE = "idle"
    SUCCEEDED = "succeeded"
    CANCELLED = "cancelled"
    FAILED = "failed"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTERED = "dead_lettered"


@dataclass(frozen=True, slots=True)
class WorkerIterationResult:
    """Sanitized worker result without lease capability, rows, SQL, or claims."""

    outcome: WorkerIterationOutcome
    job_id: str | None = None
    status: JobStatus | None = None
    failure_code: JobFailureCode | None = None


class WorkerUseCaseErrorCode(StrEnum):
    """Stable worker-process failures that contain no infrastructure detail."""

    STORE_UNAVAILABLE = "worker_job_store_unavailable"
    INVALID_CLAIM = "worker_invalid_job_claim"
    INVALID_CAPABILITY = "worker_invalid_lease_capability"
    LEASE_LOST = "worker_lease_lost"


class WorkerUseCaseError(RuntimeError):
    """Sanitized failure when no trustworthy durable transition can be recorded."""

    def __init__(self, code: WorkerUseCaseErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RunOneJobWorker:
    """Claim and process at most one job with exact authorization revalidation."""

    job_store: BackgroundJobStorePort
    access_store_factory: WorkflowAccessStoreFactoryPort
    orchestrator_factory: WorkerOrchestratorFactoryPort
    clock: WorkflowClockPort
    capability_factory: LeaseCapabilityFactoryPort
    heartbeat_supervisor: LeaseHeartbeatSupervisorPort
    worker_id: str
    lease_duration: timedelta = timedelta(seconds=60)
    heartbeat_interval: timedelta = timedelta(seconds=20)
    expiry_batch_limit: int = 100

    def __post_init__(self) -> None:
        if _SAFE_WORKER_ID.fullmatch(self.worker_id) is None:
            raise ValueError("worker_id must be a bounded inert identifier")
        if not timedelta(seconds=1) <= self.lease_duration <= timedelta(minutes=5):
            raise ValueError("lease_duration is outside the supported bound")
        if not timedelta(milliseconds=10) <= self.heartbeat_interval:
            raise ValueError("heartbeat_interval is outside the supported bound")
        if self.heartbeat_interval * 2 >= self.lease_duration:
            raise ValueError("heartbeat_interval must be less than half the lease duration")
        if not 1 <= self.expiry_batch_limit <= 1_000:
            raise ValueError("expiry_batch_limit is outside the supported bound")

    def execute(self) -> WorkerIterationResult:
        """Run one non-recursive delivery attempt."""

        try:
            self.job_store.expire_authorizations(limit=self.expiry_batch_limit)
            self.job_store.reap_exhausted_leases(limit=self.expiry_batch_limit)
        except JobStoreError:
            raise _store_unavailable() from None
        try:
            capability = self.capability_factory()
            capability_digest = digest_lease_token(capability)
        except Exception:
            raise WorkerUseCaseError(
                WorkerUseCaseErrorCode.INVALID_CAPABILITY,
                "The worker lease capability generator returned an invalid value.",
            ) from None
        try:
            job = self.job_store.claim_next(
                worker_id=self.worker_id,
                lease_token=capability,
                lease_duration=self.lease_duration,
            )
        except JobStoreError:
            raise _store_unavailable() from None
        if job is None:
            return WorkerIterationResult(outcome=WorkerIterationOutcome.IDLE)
        if not _claim_matches(job, self.worker_id, capability_digest, self.clock.now()):
            raise WorkerUseCaseError(
                WorkerUseCaseErrorCode.INVALID_CLAIM,
                "The job store returned an invalid worker claim.",
            )
        if job.status is JobStatus.CANCEL_REQUESTED:
            return self._acknowledge_cancellation(job, capability)

        now = self.clock.now()
        if not job.authorization.is_current(now):
            return self._record_failure(
                job,
                capability,
                JobFailureCode.AUTHORIZATION_EXPIRED,
                at=now,
            )
        try:
            access_store = self.access_store_factory(
                job.authorization.workspace_id,
                job.authorization.submitting_actor_id,
            )
            grant = access_store.load(
                job.authorization.workspace_id,
                job.authorization.workflow_id,
                owner_principal_id=(
                    job.authorization.submitting_actor_id
                    if job.authorization.workflow_access_scope is JobWorkflowAccessScope.OWNER
                    else None
                ),
            )
        except WorkflowAccessError as error:
            failure_code = (
                JobFailureCode.UNEXPECTED_WORKER_FAILURE
                if error.code is WorkflowAccessErrorCode.STORE_FAILURE
                else JobFailureCode.AUTHORIZATION_MISMATCH
            )
            return self._record_failure(
                job,
                capability,
                failure_code,
                at=self.clock.now(),
            )
        except ValueError:
            return self._record_failure(
                job,
                capability,
                JobFailureCode.AUTHORIZATION_MISMATCH,
                at=self.clock.now(),
            )
        except Exception:
            return self._record_failure(
                job,
                capability,
                JobFailureCode.UNEXPECTED_WORKER_FAILURE,
                at=self.clock.now(),
            )
        if (
            grant is None
            or grant.workspace_id != job.authorization.workflow_workspace_id
            or grant.workflow_id != job.authorization.workflow_id
            or grant.owner_actor_id != job.authorization.workflow_owner_actor_id
        ):
            return self._record_failure(
                job,
                capability,
                JobFailureCode.AUTHORIZATION_MISMATCH,
                at=self.clock.now(),
            )

        route_context: WorkerExecutionRouteContext | None = None
        if job.authorization.execution_target is not None:
            try:
                route_context = WorkerExecutionRouteContext.from_claim(
                    job,
                    worker_id=self.worker_id,
                    lease_capability=capability,
                )
            except ValueError:
                return self._record_failure(
                    job,
                    capability,
                    JobFailureCode.AUTHORIZATION_MISMATCH,
                    at=self.clock.now(),
                )
        try:
            if route_context is None:
                orchestrator = self.orchestrator_factory(
                    job.authorization.workflow_workspace_id,
                    job.authorization.workflow_owner_actor_id,
                )
            else:
                orchestrator = self.orchestrator_factory(
                    job.authorization.workflow_workspace_id,
                    job.authorization.workflow_owner_actor_id,
                    route_context=route_context,
                )
            current = orchestrator.inspect(job.authorization.workflow_id)
        except WorkflowError as error:
            return self._record_failure(
                job,
                capability,
                _workflow_error_code(error),
                at=self.clock.now(),
            )
        except Exception:
            return self._record_failure(
                job,
                capability,
                JobFailureCode.UNEXPECTED_WORKER_FAILURE,
                at=self.clock.now(),
            )

        recovered = _completed_result_summary(current, job, completed_at=self.clock.now())
        if recovered is not None:
            return self._succeed(job, capability, recovered)
        interrupted_operation = _ambiguous_external_operation(current)
        if interrupted_operation is not None:
            with suppress(Exception):
                orchestrator.recover_interrupted(
                    job.authorization.workflow_id,
                    expected_operation=interrupted_operation,
                )
            return self._record_failure(
                job,
                capability,
                JobFailureCode.AMBIGUOUS_EXTERNAL_EFFECT,
                at=self.clock.now(),
            )
        automatic_retry = _automatic_retry_failure(current, job)
        if automatic_retry is None:
            mismatch = _workflow_mismatch(current, job)
            if mismatch is not None:
                return self._record_failure(
                    job,
                    capability,
                    mismatch,
                    at=self.clock.now(),
                )

        refreshed = self._reload_claim(job, capability)
        if refreshed.status is JobStatus.CANCEL_REQUESTED:
            return self._acknowledge_cancellation(refreshed, capability)
        refreshed = self._heartbeat_claim(refreshed, capability)
        if refreshed.status is JobStatus.CANCEL_REQUESTED:
            return self._acknowledge_cancellation(refreshed, capability)
        now = self.clock.now()
        if not refreshed.authorization.is_current(now):
            return self._record_failure(
                refreshed,
                capability,
                JobFailureCode.AUTHORIZATION_EXPIRED,
                at=now,
            )
        reserved_decision = ExecutionWorkflowDecision(
            actor=refreshed.authorization.submitting_actor_id,
            plan_fingerprint=refreshed.authorization.expected_plan_fingerprint,
            action=WorkflowDecisionAction.APPROVE,
        )

        def apply_reserved_execution() -> AgentWorkflowDraft:
            def continuation() -> bool:
                return self._protected_io_may_continue(
                    refreshed,
                    capability,
                )

            if automatic_retry is not None:
                return orchestrator.retry(
                    refreshed.authorization.workflow_id,
                    RetryWorkflowDecision(
                        actor=refreshed.authorization.submitting_actor_id,
                        failure_fingerprint=automatic_retry.fingerprint,
                        operation=automatic_retry.operation,
                    ),
                    reserved_execution=reserved_decision,
                    should_continue=continuation,
                )
            return orchestrator.decide_execution(
                refreshed.authorization.workflow_id,
                reserved_decision,
                should_continue=continuation,
            )

        try:
            supervised = self.heartbeat_supervisor.run(
                claim=refreshed,
                worker_id=self.worker_id,
                lease_token=capability,
                lease_duration=self.lease_duration,
                heartbeat_interval=self.heartbeat_interval,
                operation=apply_reserved_execution,
            )
        except LeaseHeartbeatSupervisorError:
            raise WorkerUseCaseError(
                WorkerUseCaseErrorCode.LEASE_LOST,
                "The worker lease could not be safely maintained.",
            ) from None
        except WorkerUseCaseError:
            raise
        except WorkflowError as error:
            return self._record_failure(
                refreshed,
                capability,
                _workflow_error_code(error),
                at=self.clock.now(),
            )
        except Exception:
            return self._record_failure(
                refreshed,
                capability,
                JobFailureCode.UNEXPECTED_WORKER_FAILURE,
                at=self.clock.now(),
            )
        refreshed = self._validate_supervised_claim(
            refreshed,
            supervised.claim,
            capability,
        )
        if refreshed.status is JobStatus.CANCEL_REQUESTED:
            return self._acknowledge_cancellation(refreshed, capability)
        completed = supervised.value
        if completed.stage is WorkflowStage.FAILED and completed.failure is not None:
            return self._record_failure(
                refreshed,
                capability,
                _job_failure_for_workflow_failure(completed.failure),
                at=self.clock.now(),
            )
        summary = _completed_result_summary(
            completed,
            refreshed,
            completed_at=self.clock.now(),
        )
        if summary is None:
            return self._record_failure(
                refreshed,
                capability,
                JobFailureCode.RESULT_INVALID,
                at=self.clock.now(),
            )
        return self._succeed(refreshed, capability, summary)

    def _protected_io_may_continue(
        self,
        expected: BackgroundJob,
        capability: str,
    ) -> bool:
        """Re-read the exact lease immediately before each protected source operation."""

        current = self._reload_claim(expected, capability)
        return current.status is JobStatus.LEASED

    def _reload_claim(self, expected: BackgroundJob, capability: str) -> BackgroundJob:
        try:
            current = self.job_store.load(
                expected.authorization.workspace_id,
                expected.id,
            )
        except JobStoreError:
            raise _store_unavailable() from None
        if (
            current is None
            or current.id != expected.id
            or current.kind is not expected.kind
            or current.authorization != expected.authorization
            or current.connector_contract_version != expected.connector_contract_version
            or current.lease is None
            or expected.lease is None
            or current.lease.worker_id != self.worker_id
            or current.lease.token_digest != digest_lease_token(capability)
            or current.lease.fencing_token != expected.lease.fencing_token
            or not _claim_matches(
                current,
                self.worker_id,
                digest_lease_token(capability),
                self.clock.now(),
            )
        ):
            raise WorkerUseCaseError(
                WorkerUseCaseErrorCode.INVALID_CLAIM,
                "The worker lease changed before execution.",
            )
        return current

    def _heartbeat_claim(
        self,
        job: BackgroundJob,
        capability: str,
    ) -> BackgroundJob:
        assert job.lease is not None
        try:
            heartbeat = self.job_store.heartbeat(
                job.id,
                worker_id=self.worker_id,
                lease_token=capability,
                fencing_token=job.lease.fencing_token,
                lease_duration=self.lease_duration,
            )
        except JobStoreError:
            raise _store_unavailable() from None
        if (
            heartbeat.lease is None
            or heartbeat.id != job.id
            or heartbeat.authorization != job.authorization
            or heartbeat.connector_contract_version != job.connector_contract_version
            or heartbeat.attempt_count != job.attempt_count
            or heartbeat.last_fencing_token != job.last_fencing_token
            or heartbeat.lease.worker_id != self.worker_id
            or heartbeat.lease.token_digest != digest_lease_token(capability)
            or heartbeat.lease.fencing_token != job.lease.fencing_token
            or heartbeat.status not in {JobStatus.LEASED, JobStatus.CANCEL_REQUESTED}
        ):
            raise WorkerUseCaseError(
                WorkerUseCaseErrorCode.INVALID_CLAIM,
                "The job store returned an invalid heartbeat claim.",
            )
        return heartbeat

    def _validate_supervised_claim(
        self,
        expected: BackgroundJob,
        current: BackgroundJob,
        capability: str,
    ) -> BackgroundJob:
        expected_lease = expected.lease
        current_lease = current.lease
        if (
            expected_lease is None
            or current_lease is None
            or current.id != expected.id
            or current.kind is not expected.kind
            or current.authorization != expected.authorization
            or current.connector_contract_version != expected.connector_contract_version
            or current.attempt_count != expected.attempt_count
            or current.last_fencing_token != expected.last_fencing_token
            or current_lease.worker_id != self.worker_id
            or current_lease.token_digest != digest_lease_token(capability)
            or current_lease.fencing_token != expected_lease.fencing_token
            or not _claim_matches(
                current,
                self.worker_id,
                digest_lease_token(capability),
                self.clock.now(),
            )
        ):
            raise WorkerUseCaseError(
                WorkerUseCaseErrorCode.INVALID_CLAIM,
                "The worker heartbeat returned an invalid claim.",
            )
        return current

    def _succeed(
        self,
        job: BackgroundJob,
        capability: str,
        summary: JobResultSummary,
    ) -> WorkerIterationResult:
        assert job.lease is not None
        try:
            succeeded = self.job_store.succeed(
                job.id,
                worker_id=self.worker_id,
                lease_token=capability,
                fencing_token=job.lease.fencing_token,
                result=summary,
            )
        except JobStoreError:
            raise _store_unavailable() from None
        if (
            succeeded.status is JobStatus.FAILED
            and succeeded.failure is not None
            and succeeded.failure.code is JobFailureCode.AUTHORIZATION_EXPIRED
        ):
            return WorkerIterationResult(
                outcome=WorkerIterationOutcome.FAILED,
                job_id=succeeded.id,
                status=succeeded.status,
                failure_code=succeeded.failure.code,
            )
        if succeeded.status is not JobStatus.SUCCEEDED:
            raise WorkerUseCaseError(
                WorkerUseCaseErrorCode.INVALID_CLAIM,
                "The job store returned an invalid success transition.",
            )
        return WorkerIterationResult(
            outcome=WorkerIterationOutcome.SUCCEEDED,
            job_id=succeeded.id,
            status=succeeded.status,
        )

    def _acknowledge_cancellation(
        self,
        job: BackgroundJob,
        capability: str,
    ) -> WorkerIterationResult:
        assert job.lease is not None
        try:
            cancelled = self.job_store.acknowledge_cancellation(
                job.id,
                worker_id=self.worker_id,
                lease_token=capability,
                fencing_token=job.lease.fencing_token,
                cancelled_at=self.clock.now(),
            )
        except JobStoreError:
            raise _store_unavailable() from None
        return WorkerIterationResult(
            outcome=WorkerIterationOutcome.CANCELLED,
            job_id=cancelled.id,
            status=cancelled.status,
        )

    def _record_failure(
        self,
        job: BackgroundJob,
        capability: str,
        code: JobFailureCode,
        *,
        at: datetime,
    ) -> WorkerIterationResult:
        assert job.lease is not None
        disposition = classify_job_failure(
            code,
            attempt=job.attempt_count,
            max_attempts=job.max_attempts,
        )
        retry_at = (
            at + job_retry_delay(job.attempt_count)
            if disposition is JobFailureDisposition.RETRY
            else None
        )
        try:
            failed = self.job_store.fail(
                job.id,
                worker_id=self.worker_id,
                lease_token=capability,
                fencing_token=job.lease.fencing_token,
                code=code,
                failed_at=at,
                retry_at=retry_at,
            )
        except JobStoreError:
            raise _store_unavailable() from None
        outcome = {
            JobStatus.RETRY_WAIT: WorkerIterationOutcome.RETRY_SCHEDULED,
            JobStatus.FAILED: WorkerIterationOutcome.FAILED,
            JobStatus.DEAD_LETTERED: WorkerIterationOutcome.DEAD_LETTERED,
        }.get(failed.status)
        if outcome is None:
            raise WorkerUseCaseError(
                WorkerUseCaseErrorCode.INVALID_CLAIM,
                "The job store returned an invalid failure transition.",
            )
        return WorkerIterationResult(
            outcome=outcome,
            job_id=failed.id,
            status=failed.status,
            failure_code=code,
        )


def _claim_matches(
    job: BackgroundJob,
    worker_id: str,
    capability_digest: str,
    at: datetime,
) -> bool:
    lease = job.lease
    return (
        job.status in {JobStatus.LEASED, JobStatus.CANCEL_REQUESTED}
        and lease is not None
        and lease.worker_id == worker_id
        and lease.token_digest == capability_digest
        and lease.fencing_token == job.last_fencing_token
        and lease.attempt == job.attempt_count
        and lease.is_current(at)
    )


def _workflow_mismatch(
    draft: AgentWorkflowDraft,
    job: BackgroundJob,
) -> JobFailureCode | None:
    authorization = job.authorization
    if draft.id != authorization.workflow_id:
        return JobFailureCode.WORKFLOW_UNAVAILABLE
    if draft.revision != authorization.expected_workflow_revision:
        return JobFailureCode.WORKFLOW_STALE
    if draft.plan_fingerprint != authorization.expected_plan_fingerprint:
        return JobFailureCode.PLAN_STALE
    checkpoint = draft.checkpoint
    if (
        draft.stage is not WorkflowStage.DECISION_REQUIRED
        or checkpoint is None
        or checkpoint.kind is not WorkflowCheckpointKind.EXECUTION_APPROVAL
        or checkpoint.fingerprint != authorization.expected_plan_fingerprint
        or draft.execution is not None
    ):
        return JobFailureCode.WORKFLOW_STALE
    return None


def _automatic_retry_failure(
    draft: AgentWorkflowDraft,
    job: BackgroundJob,
) -> WorkflowFailure | None:
    failure = draft.failure
    authorization = job.authorization
    if (
        draft.id != authorization.workflow_id
        or draft.stage is not WorkflowStage.FAILED
        or draft.plan_fingerprint != authorization.expected_plan_fingerprint
        or draft.revision <= authorization.expected_workflow_revision
        or failure is None
        or not failure.retryable
        or failure.operation
        not in {
            WorkflowOperation.SQL_VALIDATION,
            WorkflowOperation.PREVIEW_EXECUTION,
            WorkflowOperation.REJECTION_INSPECTION,
        }
        or _job_failure_for_workflow_failure(failure)
        not in {
            JobFailureCode.REGISTRY_UNAVAILABLE,
            JobFailureCode.SOURCE_UNAVAILABLE,
            JobFailureCode.SOURCE_TIMEOUT,
        }
        or not any(
            decision.kind is WorkflowDecisionKind.EXECUTION
            and decision.action is WorkflowDecisionAction.APPROVE
            and decision.actor == authorization.submitting_actor_id
            and decision.bound_fingerprint == authorization.expected_plan_fingerprint
            for decision in draft.decisions
        )
    ):
        return None
    return failure


def _job_failure_for_workflow_failure(
    failure: WorkflowFailure,
) -> JobFailureCode:
    if not failure.retryable:
        if failure.operation is WorkflowOperation.SQL_VALIDATION:
            return _TERMINAL_WORKFLOW_FAILURE_CODES.get(
                (failure.operation, failure.code),
                JobFailureCode.SQL_POLICY_REJECTED,
            )
        return _TERMINAL_WORKFLOW_FAILURE_CODES.get(
            (failure.operation, failure.code),
            JobFailureCode.WORKFLOW_RETRY_REQUIRED,
        )
    return _TRANSIENT_WORKFLOW_FAILURE_CODES.get(
        (failure.operation, failure.code),
        JobFailureCode.WORKFLOW_RETRY_REQUIRED,
    )


def _ambiguous_external_operation(
    draft: AgentWorkflowDraft,
) -> WorkflowOperation | None:
    if (
        draft.trace
        and draft.trace[-1].status is WorkflowTraceStatus.STARTED
        and draft.trace[-1].operation in _AMBIGUOUS_EXTERNAL_OPERATIONS
    ):
        return draft.trace[-1].operation
    return None


def _completed_result_summary(
    draft: AgentWorkflowDraft,
    job: BackgroundJob,
    *,
    completed_at: datetime,
) -> JobResultSummary | None:
    execution = draft.execution
    authorization = job.authorization
    approved = any(
        decision.kind is WorkflowDecisionKind.EXECUTION
        and decision.action is WorkflowDecisionAction.APPROVE
        and decision.actor == authorization.submitting_actor_id
        and decision.bound_fingerprint == authorization.expected_plan_fingerprint
        for decision in draft.decisions
    )
    if (
        draft.id != authorization.workflow_id
        or draft.revision < authorization.expected_workflow_revision
        or draft.stage is not WorkflowStage.PUBLICATION_PROPOSED
        or draft.plan_fingerprint != authorization.expected_plan_fingerprint
        or execution is None
        or execution.plan_fingerprint != authorization.expected_plan_fingerprint
        or not execution.rejection_complete
        or draft.publication_proposal is None
        or not approved
    ):
        return None
    grouped = Counter(execution.rejection_codes)
    try:
        return JobResultSummary(
            workflow_id=draft.id,
            workflow_revision=draft.revision,
            stage=WorkflowStage.PUBLICATION_PROPOSED,
            row_count=execution.observed_row_count,
            preview_fingerprint=execution.preview_fingerprint,
            rejected_count=execution.rejected_count,
            rejection_code_counts=tuple(
                JobRejectionCount(code=code, count=count) for code, count in sorted(grouped.items())
            ),
            truncated=execution.any_truncated,
            completed_at=completed_at,
        )
    except ValueError:
        return None


def _workflow_error_code(error: WorkflowError) -> JobFailureCode:
    if error.code is WorkflowErrorCode.NOT_FOUND:
        return JobFailureCode.WORKFLOW_UNAVAILABLE
    if error.code in {
        WorkflowErrorCode.CONFLICT,
        WorkflowErrorCode.INVALID_TRANSITION,
        WorkflowErrorCode.DECISION_MISMATCH,
    }:
        return JobFailureCode.WORKFLOW_STALE
    return JobFailureCode.UNEXPECTED_WORKER_FAILURE


def _store_unavailable() -> WorkerUseCaseError:
    return WorkerUseCaseError(
        WorkerUseCaseErrorCode.STORE_UNAVAILABLE,
        "The worker job store is unavailable.",
    )
