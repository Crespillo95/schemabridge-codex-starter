"""Adversarial unit tests for one bounded M24 worker iteration."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from schemabridge.application.job_worker import (
    RunOneJobWorker,
    WorkerExecutionRouteContext,
    WorkerIterationOutcome,
    WorkerUseCaseError,
    WorkerUseCaseErrorCode,
)
from schemabridge.application.ports.background_jobs import (
    JobStoreError,
    JobStoreErrorCode,
    LeaseHeartbeatRunResult,
    LeaseHeartbeatSupervisorError,
)
from schemabridge.application.ports.workflow_access import (
    WorkflowAccessError,
    WorkflowAccessErrorCode,
)
from schemabridge.application.ports.workflows import WorkflowError, WorkflowErrorCode
from schemabridge.domain.background_jobs import (
    BackgroundJob,
    JobAuthorization,
    JobExecutionTargetRef,
    JobFailureCode,
    JobStatus,
    JobSubmissionResult,
    JobWorkflowAccessScope,
    acknowledge_job_cancellation,
    claim_job,
    complete_job,
    dead_letter_exhausted_lease,
    expire_job_authorization,
    fail_job,
    heartbeat_job,
    request_job_cancellation,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    QueryCostBudget,
    SourceConnectorKind,
    SourceDialect,
    postgres_type_contract_fingerprint,
)
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.identity import WorkflowAccessGrant
from schemabridge.domain.workflows import (
    AgentWorkflowDraft,
    ExecutionWorkflowDecision,
    RetryWorkflowDecision,
    WorkflowCheckpoint,
    WorkflowCheckpointKind,
    WorkflowDecisionAction,
    WorkflowDecisionKind,
    WorkflowDecisionRecord,
    WorkflowExecutionRecord,
    WorkflowFailure,
    WorkflowOperation,
    WorkflowPublicationProposal,
    WorkflowStage,
    WorkflowTraceEvent,
    WorkflowTraceStatus,
)

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
WORKSPACE = "sb_workspace_a"
WORKFLOW_ID = "workflow-unit-1"
OWNER = "sb_actor_owner"
SUBMITTER = "sb_actor_submitter"
PLAN = "a" * 64
QUERY = "b" * 64
PREVIEW = "c" * 64
LEASE_TOKEN = "worker-capability-" + ("x" * 40)
WORKER_ID = "worker-unit-1"


class _Clock:
    def __init__(self, current: datetime = NOW + timedelta(milliseconds=1500)) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


class _SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self.values = list(values)
        self.last = values[-1]

    def now(self) -> datetime:
        if self.values:
            self.last = self.values.pop(0)
        return self.last


class _CapabilityFactory:
    def __init__(self) -> None:
        self.calls = 0
        self.invalid = False
        self.crash = False

    def __call__(self) -> str:
        self.calls += 1
        if self.crash:
            raise RuntimeError("generator internals must not escape")
        if self.invalid:
            return "short"
        return LEASE_TOKEN


class _InlineHeartbeatSupervisor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, timedelta, timedelta]] = []
        self.fail = False

    def run(
        self,
        *,
        claim: BackgroundJob,
        worker_id: str,
        lease_token: str,
        lease_duration: timedelta,
        heartbeat_interval: timedelta,
        operation: Callable[[], AgentWorkflowDraft],
    ) -> LeaseHeartbeatRunResult[AgentWorkflowDraft]:
        self.calls.append((claim.id, worker_id, lease_duration, heartbeat_interval))
        if self.fail:
            raise LeaseHeartbeatSupervisorError("sensitive heartbeat detail")
        value = operation()
        assert isinstance(value, AgentWorkflowDraft)
        return LeaseHeartbeatRunResult(value=value, claim=claim)


class _CancelAfterOperationSupervisor(_InlineHeartbeatSupervisor):
    def __init__(self, store: _MemoryJobStore) -> None:
        super().__init__()
        self.store = store

    def run(
        self,
        *,
        claim: BackgroundJob,
        worker_id: str,
        lease_token: str,
        lease_duration: timedelta,
        heartbeat_interval: timedelta,
        operation: Callable[[], AgentWorkflowDraft],
    ) -> LeaseHeartbeatRunResult[AgentWorkflowDraft]:
        del worker_id, lease_token, lease_duration, heartbeat_interval
        value = operation()
        current = self.store.jobs[claim.id]
        cancelled = request_job_cancellation(
            current,
            requested_at=current.updated_at + timedelta(milliseconds=1),
        )
        self.store.jobs[claim.id] = cancelled
        return LeaseHeartbeatRunResult(value=value, claim=cancelled)


class _MemoryJobStore:
    """Database-time-shaped fake that persists only typed domain state."""

    def __init__(
        self,
        *jobs: BackgroundJob,
        database_now: datetime = NOW + timedelta(seconds=1),
    ) -> None:
        self.jobs = {job.id: job for job in jobs}
        self.database_now = database_now
        self.expire_calls = 0
        self.reap_calls = 0
        self.claim_calls = 0
        self.heartbeat_calls: list[tuple[str, str, int, timedelta]] = []
        self.fail_calls: list[tuple[JobFailureCode, timedelta | None]] = []
        self.succeed_calls = 0
        self.acknowledge_calls = 0
        self.cancel_after_claim = False
        self.cancel_during_heartbeat = False
        self.raise_on_expire = False

    def _tick(self) -> datetime:
        instant = self.database_now
        self.database_now += timedelta(seconds=1)
        return instant

    def submit(self, job: BackgroundJob) -> JobSubmissionResult:
        self.jobs[job.id] = job
        return JobSubmissionResult(job=job)

    def load_by_idempotency(
        self,
        workspace_id: str,
        submitting_actor_id: str,
        idempotency_digest: str,
    ) -> BackgroundJob | None:
        return next(
            (
                job
                for job in self.jobs.values()
                if job.authorization.workspace_id == workspace_id
                and job.authorization.submitting_actor_id == submitting_actor_id
                and job.idempotency_digest == idempotency_digest
            ),
            None,
        )

    def load(
        self,
        workspace_id: str,
        job_id: str,
        *,
        submitting_actor_id: str | None = None,
    ) -> BackgroundJob | None:
        job = self.jobs.get(job_id)
        if (
            job is None
            or job.authorization.workspace_id != workspace_id
            or (
                submitting_actor_id is not None
                and job.authorization.submitting_actor_id != submitting_actor_id
            )
        ):
            return None
        return job

    def request_cancellation(
        self,
        workspace_id: str,
        job_id: str,
        *,
        submitting_actor_id: str | None,
        requested_at: datetime,
    ) -> BackgroundJob | None:
        job = self.load(
            workspace_id,
            job_id,
            submitting_actor_id=submitting_actor_id,
        )
        if job is None:
            return None
        cancelled = request_job_cancellation(job, requested_at=requested_at)
        self.jobs[job.id] = cancelled
        return cancelled

    def expire_authorizations(self, *, limit: int = 100) -> int:
        self.expire_calls += 1
        if self.raise_on_expire:
            raise JobStoreError(JobStoreErrorCode.STORE_UNAVAILABLE, "sanitized outage")
        changed = 0
        for job in tuple(self.jobs.values()):
            if changed >= limit:
                break
            if (
                job.status in {JobStatus.QUEUED, JobStatus.RETRY_WAIT}
                and job.authorization.expires_at <= self.database_now
            ):
                self.jobs[job.id] = expire_job_authorization(
                    job,
                    expired_at=self.database_now,
                )
                changed += 1
        return changed

    def reap_exhausted_leases(self, *, limit: int = 100) -> int:
        self.reap_calls += 1
        changed = 0
        for job in tuple(self.jobs.values()):
            if changed >= limit:
                break
            if (
                job.status in {JobStatus.LEASED, JobStatus.CANCEL_REQUESTED}
                and job.lease is not None
                and job.attempt_count >= job.max_attempts
                and job.lease.expires_at <= self.database_now
            ):
                self.jobs[job.id] = dead_letter_exhausted_lease(
                    job,
                    expired_at=self.database_now,
                )
                changed += 1
        return changed

    def claim_next(
        self,
        *,
        worker_id: str,
        lease_token: str,
        lease_duration: timedelta,
    ) -> BackgroundJob | None:
        self.claim_calls += 1
        claimed_at = self._tick()
        for job in tuple(self.jobs.values()):
            waiting = (
                job.status in {JobStatus.QUEUED, JobStatus.RETRY_WAIT}
                and job.available_at is not None
                and job.available_at <= claimed_at
            )
            reclaimable = (
                job.status in {JobStatus.LEASED, JobStatus.CANCEL_REQUESTED}
                and job.lease is not None
                and job.lease.expires_at <= claimed_at
                and job.attempt_count < job.max_attempts
            )
            if not (waiting or reclaimable):
                continue
            claimed = claim_job(
                job,
                worker_id=worker_id,
                lease_token=lease_token,
                claimed_at=claimed_at,
                lease_expires_at=claimed_at + lease_duration,
            )
            if self.cancel_after_claim:
                claimed = request_job_cancellation(
                    claimed,
                    requested_at=claimed_at,
                )
            self.jobs[job.id] = claimed
            return claimed
        return None

    def heartbeat(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        fencing_token: int,
        lease_duration: timedelta,
    ) -> BackgroundJob:
        self.heartbeat_calls.append((job_id, worker_id, fencing_token, lease_duration))
        current = self.jobs[job_id]
        heartbeat_at = self._tick()
        if self.cancel_during_heartbeat:
            current = request_job_cancellation(
                current,
                requested_at=heartbeat_at,
            )
        heartbeat = heartbeat_job(
            current,
            worker_id=worker_id,
            lease_token=lease_token,
            fencing_token=fencing_token,
            heartbeat_at=heartbeat_at,
            lease_expires_at=heartbeat_at + lease_duration,
        )
        self.jobs[job_id] = heartbeat
        return heartbeat

    def succeed(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        fencing_token: int,
        result: object,
    ) -> BackgroundJob:
        from schemabridge.domain.background_jobs import JobResultSummary

        assert isinstance(result, JobResultSummary)
        self.succeed_calls += 1
        completed_at = self._tick()
        current = self.jobs[job_id]
        if current.authorization.is_current(completed_at):
            database_result = result.model_copy(update={"completed_at": completed_at})
            changed = complete_job(
                current,
                worker_id=worker_id,
                lease_token=lease_token,
                fencing_token=fencing_token,
                result=database_result,
            )
        else:
            changed = fail_job(
                current,
                worker_id=worker_id,
                lease_token=lease_token,
                fencing_token=fencing_token,
                code=JobFailureCode.AUTHORIZATION_EXPIRED,
                failed_at=completed_at,
            )
        self.jobs[job_id] = changed
        return changed

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
        retry_delay = None if retry_at is None else retry_at - failed_at
        self.fail_calls.append((code, retry_delay))
        database_failed_at = self._tick()
        failed = fail_job(
            self.jobs[job_id],
            worker_id=worker_id,
            lease_token=lease_token,
            fencing_token=fencing_token,
            code=code,
            failed_at=database_failed_at,
            retry_at=(None if retry_delay is None else database_failed_at + retry_delay),
        )
        self.jobs[job_id] = failed
        return failed

    def acknowledge_cancellation(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        fencing_token: int,
        cancelled_at: datetime,
    ) -> BackgroundJob:
        del cancelled_at
        self.acknowledge_calls += 1
        cancelled = acknowledge_job_cancellation(
            self.jobs[job_id],
            worker_id=worker_id,
            lease_token=lease_token,
            fencing_token=fencing_token,
            cancelled_at=self._tick(),
        )
        self.jobs[job_id] = cancelled
        return cancelled


class _AccessStore:
    def __init__(
        self,
        grant: WorkflowAccessGrant | None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.grant = grant
        self.error = error
        self.loads: list[tuple[str, str, str | None]] = []

    def load(
        self,
        workspace_id: str,
        workflow_id: str,
        *,
        owner_principal_id: str | None = None,
    ) -> WorkflowAccessGrant | None:
        self.loads.append((workspace_id, workflow_id, owner_principal_id))
        if self.error is not None:
            raise self.error
        grant = self.grant
        if (
            grant is None
            or grant.workspace_id != workspace_id
            or grant.workflow_id != workflow_id
            or (owner_principal_id is not None and grant.owner_actor_id != owner_principal_id)
        ):
            return None
        return grant


class _Orchestrator:
    def __init__(
        self,
        current: AgentWorkflowDraft,
        *,
        completed: AgentWorkflowDraft | None = None,
        retry_completed: AgentWorkflowDraft | None = None,
        inspect_error: Exception | None = None,
        decide_error: Exception | None = None,
    ) -> None:
        self.current = current
        self.completed = completed
        self.retry_completed = retry_completed
        self.inspect_error = inspect_error
        self.decide_error = decide_error
        self.inspect_calls: list[str] = []
        self.decide_calls: list[tuple[str, ExecutionWorkflowDecision]] = []
        self.retry_calls: list[
            tuple[str, RetryWorkflowDecision, ExecutionWorkflowDecision | None]
        ] = []
        self.recover_calls: list[tuple[str, WorkflowOperation]] = []

    def inspect(self, workflow_id: str) -> AgentWorkflowDraft:
        self.inspect_calls.append(workflow_id)
        if self.inspect_error is not None:
            raise self.inspect_error
        return self.current

    def decide_execution(
        self,
        workflow_id: str,
        decision: ExecutionWorkflowDecision,
        *,
        should_continue: Callable[[], bool] | None = None,
    ) -> AgentWorkflowDraft:
        self.decide_calls.append((workflow_id, decision))
        if self.decide_error is not None:
            raise self.decide_error
        if should_continue is not None and not should_continue():
            return self.current
        if self.completed is None:
            raise AssertionError("test orchestrator has no completion")
        self.current = self.completed
        return self.completed

    def retry(
        self,
        workflow_id: str,
        decision: RetryWorkflowDecision,
        *,
        reserved_execution: ExecutionWorkflowDecision | None = None,
        should_continue: Callable[[], bool] | None = None,
    ) -> AgentWorkflowDraft:
        self.retry_calls.append((workflow_id, decision, reserved_execution))
        if should_continue is not None and not should_continue():
            return self.current
        if self.retry_completed is None:
            raise AssertionError("test orchestrator has no retry completion")
        self.current = self.retry_completed
        return self.retry_completed

    def recover_interrupted(
        self,
        workflow_id: str,
        *,
        expected_operation: WorkflowOperation,
    ) -> AgentWorkflowDraft:
        self.recover_calls.append((workflow_id, expected_operation))
        return self.current


class _ScopedFactory:
    def __init__(self, value: object) -> None:
        self.value = value
        self.calls: list[tuple[str, str]] = []
        self.route_contexts: list[WorkerExecutionRouteContext] = []

    def __call__(
        self,
        workspace_id: str,
        actor_id: str,
        *,
        route_context: WorkerExecutionRouteContext | None = None,
    ) -> object:
        self.calls.append((workspace_id, actor_id))
        if route_context is not None:
            self.route_contexts.append(route_context)
        return self.value


def _execution_target(*, workspace_id: str = WORKSPACE) -> GovernedExecutionTarget:
    budget = QueryCostBudget(
        explain_timeout_ms=1_000,
        max_response_bytes=65_536,
        max_total_cost=Decimal("10000"),
        max_estimated_rows=100_000,
        max_plan_nodes=1_000,
        max_plan_depth=32,
        max_plan_width=16_384,
    )
    return GovernedExecutionTarget(
        workspace_id=workspace_id,
        connection_id=CatalogConnectionId("warehouse-primary"),
        connector_kind=SourceConnectorKind.POSTGRESQL,
        dialect=SourceDialect.POSTGRESQL,
        route_revision=7,
        route_fingerprint="e" * 64,
        expected_reader="schemabridge_reader",
        source_identity_fingerprint="a" * 64,
        catalog_identity_fingerprint="b" * 64,
        type_contract_fingerprint=postgres_type_contract_fingerprint(),
        cost_budget=budget,
        cost_budget_fingerprint=budget.fingerprint,
    )


def _authorization(
    *,
    owner: str = OWNER,
    submitter: str = SUBMITTER,
    workspace_id: str = WORKSPACE,
    workflow_workspace_id: str | None = None,
    access_scope: JobWorkflowAccessScope = JobWorkflowAccessScope.WORKSPACE,
    expires_at: datetime = NOW + timedelta(minutes=10),
    execution_target: GovernedExecutionTarget | None = None,
) -> JobAuthorization:
    return JobAuthorization.create(
        workspace_id=workspace_id,
        workflow_workspace_id=workflow_workspace_id,
        workflow_id=WORKFLOW_ID,
        workflow_owner_actor_id=owner,
        submitting_actor_id=submitter,
        workflow_access_scope=access_scope,
        expected_workflow_revision=7,
        expected_plan_fingerprint=PLAN,
        execution_target=(
            None
            if execution_target is None
            else JobExecutionTargetRef.from_target(execution_target)
        ),
        authenticated_at=NOW - timedelta(minutes=1),
        authorized_at=NOW,
        expires_at=expires_at,
    )


def _job(
    *,
    job_id: str = "execution-job-unit-1",
    authorization: JobAuthorization | None = None,
    max_attempts: int = 3,
    connector_contract_version: int | None = None,
) -> BackgroundJob:
    return BackgroundJob.create(
        id=job_id,
        authorization=authorization or _authorization(),
        connector_contract_version=connector_contract_version,
        idempotency_digest="d" * 64,
        max_attempts=max_attempts,
        created_at=NOW,
    )


def _grant(*, owner: str = OWNER) -> WorkflowAccessGrant:
    return WorkflowAccessGrant(
        workflow_id=WORKFLOW_ID,
        workspace_id=WORKSPACE,
        owner_actor_id=owner,
        created_at=NOW,
    )


def _approval_draft(
    *,
    revision: int = 7,
    plan: str = PLAN,
    stage: WorkflowStage = WorkflowStage.DECISION_REQUIRED,
    checkpoint_kind: WorkflowCheckpointKind = WorkflowCheckpointKind.EXECUTION_APPROVAL,
) -> AgentWorkflowDraft:
    return AgentWorkflowDraft(
        id=WORKFLOW_ID,
        revision=revision,
        stage=stage,
        text="Agrupa clientes por fecha.",
        language="es",
        requested_datasets=(PhysicalDatasetRef("crm.customers"),),
        plan_fingerprint=plan,
        checkpoint=WorkflowCheckpoint(
            kind=checkpoint_kind,
            fingerprint=plan,
            reason="Exact execution approval is required.",
        ),
        created_at=NOW - timedelta(minutes=2),
        updated_at=NOW,
    )


def _completed_draft(
    *,
    submitter: str = SUBMITTER,
    include_rows: bool = True,
) -> AgentWorkflowDraft:
    rows: tuple[tuple[str, ...], ...] = (
        (
            ("private-customer-123",),
            ("private-customer-456",),
        )
        if include_rows
        else ()
    )
    execution = WorkflowExecutionRecord(
        plan_fingerprint=PLAN,
        query_fingerprint=QUERY,
        columns=("customer_id",),
        rows=rows,
        row_count=len(rows),
        database_user="schemabridge_reader",
        transaction_read_only=True,
        statement_timeout_ms=5_000,
        truncated=False,
        preview_fingerprint=PREVIEW,
        rejection_codes=(
            ("null_join_key", "null_join_key", "non_finite_identifier") if include_rows else ()
        ),
        rejected_count=3 if include_rows else 0,
        rejection_complete=True,
    )
    proposal = WorkflowPublicationProposal.create(
        workflow_id=WORKFLOW_ID,
        plan_fingerprint=PLAN,
        execution_fingerprint=PREVIEW,
        request_fingerprint="e" * 64,
    )
    approved = WorkflowDecisionRecord(
        actor=submitter,
        kind=WorkflowDecisionKind.EXECUTION,
        action=WorkflowDecisionAction.APPROVE,
        decided_at=NOW + timedelta(seconds=2),
        bound_fingerprint=PLAN,
    )
    return _approval_draft().model_copy(
        update={
            "revision": 11,
            "stage": WorkflowStage.PUBLICATION_PROPOSED,
            "query_fingerprint": QUERY,
            "execution": execution,
            "publication_proposal": proposal,
            "checkpoint": WorkflowCheckpoint(
                kind=WorkflowCheckpointKind.PUBLICATION_DECISION,
                fingerprint=proposal.fingerprint,
                reason="Publication remains an explicit human decision.",
            ),
            "decisions": (approved,),
            "updated_at": NOW + timedelta(seconds=2),
        }
    )


def _failed_draft() -> AgentWorkflowDraft:
    failure = WorkflowFailure.create(
        code="planning_context_unavailable",
        operation=WorkflowOperation.SEMANTIC_RESOLUTION,
        retryable=True,
        attempt=1,
        occurred_at=NOW + timedelta(seconds=2),
    )
    return _approval_draft().model_copy(
        update={
            "revision": 8,
            "stage": WorkflowStage.FAILED,
            "checkpoint": None,
            "failure": failure,
            "updated_at": NOW + timedelta(seconds=2),
        }
    )


def _transient_failed_draft(
    *,
    code: str,
    operation: WorkflowOperation,
    attempt: int,
    revision: int,
    retryable: bool = True,
) -> AgentWorkflowDraft:
    approved = WorkflowDecisionRecord(
        actor=SUBMITTER,
        kind=WorkflowDecisionKind.EXECUTION,
        action=WorkflowDecisionAction.APPROVE,
        decided_at=NOW + timedelta(seconds=1),
        bound_fingerprint=PLAN,
    )
    failure = WorkflowFailure.create(
        code=code,
        operation=operation,
        retryable=retryable,
        attempt=attempt,
        occurred_at=NOW + timedelta(seconds=attempt + 1),
    )
    return _approval_draft().model_copy(
        update={
            "revision": revision,
            "stage": WorkflowStage.FAILED,
            "checkpoint": None,
            "failure": failure,
            "decisions": (approved,),
            "updated_at": failure.occurred_at,
        }
    )


def _ambiguous_draft() -> AgentWorkflowDraft:
    trace = WorkflowTraceEvent(
        sequence=1,
        stage=WorkflowStage.EXECUTION,
        operation=WorkflowOperation.PREVIEW_EXECUTION,
        status=WorkflowTraceStatus.STARTED,
        occurred_at=NOW,
        input_refs=(PLAN,),
    )
    return _approval_draft().model_copy(update={"trace": (trace,)})


def _worker(
    store: _MemoryJobStore,
    *,
    access: _AccessStore | None = None,
    orchestrator: _Orchestrator | None = None,
    clock: _Clock | _SequenceClock | None = None,
    capability_factory: _CapabilityFactory | None = None,
    heartbeat_supervisor: _InlineHeartbeatSupervisor | None = None,
) -> tuple[
    RunOneJobWorker,
    _AccessStore,
    _Orchestrator,
    _ScopedFactory,
    _ScopedFactory,
    _CapabilityFactory,
]:
    selected_access = access or _AccessStore(_grant())
    selected_orchestrator = orchestrator or _Orchestrator(
        _approval_draft(),
        completed=_completed_draft(),
    )
    access_factory = _ScopedFactory(selected_access)
    orchestrator_factory = _ScopedFactory(selected_orchestrator)
    selected_capability = capability_factory or _CapabilityFactory()
    selected_heartbeat = heartbeat_supervisor or _InlineHeartbeatSupervisor()
    worker = RunOneJobWorker(
        job_store=store,
        access_store_factory=access_factory,  # type: ignore[arg-type]
        orchestrator_factory=orchestrator_factory,  # type: ignore[arg-type]
        clock=clock or _Clock(),
        capability_factory=selected_capability,
        heartbeat_supervisor=selected_heartbeat,
        worker_id=WORKER_ID,
        lease_duration=timedelta(seconds=60),
        heartbeat_interval=timedelta(seconds=20),
    )
    return (
        worker,
        selected_access,
        selected_orchestrator,
        access_factory,
        orchestrator_factory,
        selected_capability,
    )


def test_idle_iteration_expires_and_reaps_before_one_claim() -> None:
    store = _MemoryJobStore()
    worker, _access, orchestrator, _af, _of, capability = _worker(store)

    result = worker.execute()

    assert result.outcome is WorkerIterationOutcome.IDLE
    assert result.job_id is None
    assert store.expire_calls == 1
    assert store.reap_calls == 1
    assert store.claim_calls == 1
    assert capability.calls == 1
    assert orchestrator.inspect_calls == []


def test_worker_rejects_lease_duration_above_the_domain_and_store_bound() -> None:
    worker, *_rest = _worker(_MemoryJobStore())

    with pytest.raises(ValueError, match="lease_duration"):
        replace(worker, lease_duration=timedelta(minutes=5, milliseconds=1))


def test_success_revalidates_exact_scope_heartbeats_and_persists_only_summary() -> None:
    job = _job()
    store = _MemoryJobStore(job)
    completed = _completed_draft()
    orchestrator = _Orchestrator(_approval_draft(), completed=completed)
    worker, access, _orchestrator, access_factory, orchestrator_factory, _capability = _worker(
        store, orchestrator=orchestrator
    )

    result = worker.execute()

    durable = store.jobs[job.id]
    assert result.outcome is WorkerIterationOutcome.SUCCEEDED
    assert result.status is JobStatus.SUCCEEDED
    assert durable.result is not None
    assert durable.result.row_count == 2
    assert durable.result.rejected_count == 3
    assert tuple((item.code, item.count) for item in durable.result.rejection_code_counts) == (
        ("non_finite_identifier", 1),
        ("null_join_key", 2),
    )
    assert access.loads == [(WORKSPACE, WORKFLOW_ID, None)]
    assert access_factory.calls == [(WORKSPACE, SUBMITTER)]
    assert orchestrator_factory.calls == [(WORKSPACE, OWNER)]
    assert len(store.heartbeat_calls) == 1
    assert store.heartbeat_calls[0] == (
        job.id,
        WORKER_ID,
        1,
        timedelta(seconds=60),
    )
    assert len(orchestrator.decide_calls) == 1
    decision = orchestrator.decide_calls[0][1]
    assert decision.actor == SUBMITTER
    assert decision.plan_fingerprint == PLAN
    assert decision.action is WorkflowDecisionAction.APPROVE
    serialized = json.dumps(durable.model_dump(mode="json"), sort_keys=True)
    assert LEASE_TOKEN not in serialized
    assert "private-customer-123" not in serialized
    assert "private-customer-456" not in serialized
    assert '"rows"' not in serialized
    assert "SELECT" not in serialized
    assert LEASE_TOKEN not in repr(result)


def test_managed_job_factory_receives_only_its_exact_leased_route_context() -> None:
    target = _execution_target()
    job = _job(
        authorization=_authorization(execution_target=target),
        connector_contract_version=3,
    )
    store = _MemoryJobStore(job)
    worker, _access, _orchestrator, _af, orchestrator_factory, _capability = _worker(store)

    result = worker.execute()

    assert result.outcome is WorkerIterationOutcome.SUCCEEDED
    assert orchestrator_factory.calls == [(WORKSPACE, OWNER)]
    assert len(orchestrator_factory.route_contexts) == 1
    context = orchestrator_factory.route_contexts[0]
    assert context.job_id == job.id
    assert context.workflow_id == WORKFLOW_ID
    assert context.job_workspace_id == WORKSPACE
    assert context.worker_id == WORKER_ID
    assert context.fencing_token == 1
    assert context.execution_target == job.authorization.execution_target
    assert context.connector_contract_version == 3
    assert LEASE_TOKEN not in repr(context)


def test_managed_job_without_persisted_contract_fails_before_workflow_io() -> None:
    job = _job(authorization=_authorization(execution_target=_execution_target()))
    store = _MemoryJobStore(job)
    worker, _access, orchestrator, _af, orchestrator_factory, _capability = _worker(store)

    result = worker.execute()

    assert result.outcome is WorkerIterationOutcome.FAILED
    assert result.failure_code is JobFailureCode.AUTHORIZATION_MISMATCH
    assert orchestrator_factory.calls == []
    assert orchestrator.inspect_calls == []


def test_worker_executes_only_the_reserved_historical_workflow_coordinates() -> None:
    current_workspace = "sb_workspace_current"
    current_submitter = "sb_actor_current"
    historical_workspace = "sb_workspace_historical"
    historical_owner = "sb_actor_historical"
    historical_target = _execution_target(workspace_id=historical_workspace)
    job = _job(
        authorization=_authorization(
            workspace_id=current_workspace,
            workflow_workspace_id=historical_workspace,
            owner=historical_owner,
            submitter=current_submitter,
            access_scope=JobWorkflowAccessScope.OWNER,
            execution_target=historical_target,
        ),
        connector_contract_version=3,
    )
    historical_grant = WorkflowAccessGrant(
        workflow_id=WORKFLOW_ID,
        workspace_id=historical_workspace,
        owner_actor_id=historical_owner,
        created_at=NOW,
    )

    class _HistoricalAccess(_AccessStore):
        def load(
            self,
            workspace_id: str,
            workflow_id: str,
            *,
            owner_principal_id: str | None = None,
        ) -> WorkflowAccessGrant | None:
            self.loads.append((workspace_id, workflow_id, owner_principal_id))
            return historical_grant

    store = _MemoryJobStore(job)
    access = _HistoricalAccess(historical_grant)
    orchestrator = _Orchestrator(
        _approval_draft(),
        completed=_completed_draft(submitter=current_submitter),
    )
    worker, _access, _orchestrator, access_factory, orchestrator_factory, _capability = _worker(
        store, access=access, orchestrator=orchestrator
    )

    result = worker.execute()

    assert result.outcome is WorkerIterationOutcome.SUCCEEDED
    assert access_factory.calls == [(current_workspace, current_submitter)]
    assert access.loads == [(current_workspace, WORKFLOW_ID, current_submitter)]
    assert orchestrator_factory.calls == [(historical_workspace, historical_owner)]
    assert len(orchestrator_factory.route_contexts) == 1
    route_context = orchestrator_factory.route_contexts[0]
    assert route_context.job_workspace_id == current_workspace
    assert route_context.connector_workspace_id == historical_workspace
    assert route_context.execution_target.workspace_id == historical_workspace


def test_success_preserves_truncated_rejection_total_without_inventing_counts() -> None:
    job = _job()
    store = _MemoryJobStore(job)
    completed = _completed_draft()
    assert completed.execution is not None
    execution = WorkflowExecutionRecord.model_validate(
        {
            **completed.execution.model_dump(mode="python"),
            "rejection_codes": (
                "null_join_key",
                "non_finite_identifier",
            ),
            "rejected_count": 25_001,
            "rejection_truncated": True,
        }
    )
    completed = completed.model_copy(update={"execution": execution})
    orchestrator = _Orchestrator(_approval_draft(), completed=completed)
    worker, *_rest = _worker(store, orchestrator=orchestrator)

    result = worker.execute()

    durable = store.jobs[job.id]
    assert result.outcome is WorkerIterationOutcome.SUCCEEDED
    assert durable.result is not None
    assert durable.result.rejected_count == 25_001
    assert tuple((item.code, item.count) for item in durable.result.rejection_code_counts) == (
        ("non_finite_identifier", 1),
        ("null_join_key", 1),
    )
    assert durable.result.sampled_rejected_count == 2
    assert durable.result.unclassified_rejection_count == 24_999
    assert durable.result.rejection_counts_complete is False
    assert durable.result.rejection_truncated is True
    assert durable.result.truncated is True
    serialized = json.dumps(durable.model_dump(mode="json"), sort_keys=True)
    assert '"rows"' not in serialized
    assert "unclassified_rejections" not in serialized


def test_zero_row_preview_succeeds_with_an_explicit_empty_summary() -> None:
    job = _job()
    store = _MemoryJobStore(job)
    orchestrator = _Orchestrator(
        _approval_draft(),
        completed=_completed_draft(include_rows=False),
    )
    worker, *_rest = _worker(store, orchestrator=orchestrator)

    result = worker.execute()

    assert result.outcome is WorkerIterationOutcome.SUCCEEDED
    assert store.jobs[job.id].result is not None
    assert store.jobs[job.id].result.row_count == 0
    assert store.jobs[job.id].result.rejection_code_counts == ()


def test_cancellation_after_claim_is_acknowledged_before_protected_io() -> None:
    job = _job()
    store = _MemoryJobStore(job)
    store.cancel_after_claim = True
    worker, access, orchestrator, access_factory, orchestrator_factory, _capability = _worker(store)

    result = worker.execute()

    assert result.outcome is WorkerIterationOutcome.CANCELLED
    assert store.jobs[job.id].status is JobStatus.CANCELLED
    assert store.acknowledge_calls == 1
    assert store.heartbeat_calls == []
    assert access.loads == []
    assert access_factory.calls == []
    assert orchestrator_factory.calls == []
    assert orchestrator.inspect_calls == []


def test_cancellation_winning_during_heartbeat_prevents_execution() -> None:
    job = _job()
    store = _MemoryJobStore(job)
    store.cancel_during_heartbeat = True
    orchestrator = _Orchestrator(
        _approval_draft(),
        completed=_completed_draft(),
    )
    worker, _access, _orchestrator, _af, _of, _capability = _worker(
        store,
        orchestrator=orchestrator,
    )

    result = worker.execute()

    assert result.outcome is WorkerIterationOutcome.CANCELLED
    assert store.jobs[job.id].status is JobStatus.CANCELLED
    assert len(store.heartbeat_calls) == 1
    assert store.acknowledge_calls == 1
    assert orchestrator.inspect_calls == [WORKFLOW_ID]
    assert orchestrator.decide_calls == []


def test_cancellation_observed_after_operation_discards_success_and_is_acknowledged() -> None:
    job = _job()
    store = _MemoryJobStore(job)
    supervisor = _CancelAfterOperationSupervisor(store)
    orchestrator = _Orchestrator(
        _approval_draft(),
        completed=_completed_draft(),
    )
    worker, *_rest = _worker(
        store,
        orchestrator=orchestrator,
        heartbeat_supervisor=supervisor,
    )

    result = worker.execute()

    assert orchestrator.decide_calls
    assert store.succeed_calls == 0
    assert store.acknowledge_calls == 1
    assert result.outcome is WorkerIterationOutcome.CANCELLED
    assert result.status is JobStatus.CANCELLED
    assert store.jobs[job.id].status is JobStatus.CANCELLED


def test_expired_reserved_authorization_fails_before_grant_or_workflow_io() -> None:
    job = _job(
        authorization=_authorization(
            expires_at=NOW + timedelta(milliseconds=1250),
        )
    )
    store = _MemoryJobStore(job)
    worker, access, orchestrator, access_factory, orchestrator_factory, _capability = _worker(store)

    result = worker.execute()

    assert result.outcome is WorkerIterationOutcome.FAILED
    assert result.failure_code is JobFailureCode.AUTHORIZATION_EXPIRED
    assert store.jobs[job.id].status is JobStatus.FAILED
    assert access.loads == []
    assert access_factory.calls == []
    assert orchestrator_factory.calls == []
    assert orchestrator.inspect_calls == []


def test_authorization_expiring_during_heartbeat_is_rechecked_before_execution() -> None:
    job = _job(
        authorization=_authorization(
            expires_at=NOW + timedelta(milliseconds=2500),
        )
    )
    store = _MemoryJobStore(job)
    before_expiry = NOW + timedelta(milliseconds=1500)
    after_expiry = NOW + timedelta(seconds=3)
    clock = _SequenceClock(
        before_expiry,
        before_expiry,
        before_expiry,
        before_expiry,
        after_expiry,
    )
    orchestrator = _Orchestrator(
        _approval_draft(),
        completed=_completed_draft(),
    )
    worker, *_rest = _worker(store, orchestrator=orchestrator, clock=clock)

    result = worker.execute()

    assert result.outcome is WorkerIterationOutcome.FAILED
    assert result.failure_code is JobFailureCode.AUTHORIZATION_EXPIRED
    assert len(store.heartbeat_calls) == 1
    assert orchestrator.decide_calls == []


def test_authorization_expiring_during_operation_cannot_commit_success() -> None:
    job = _job(
        authorization=_authorization(
            expires_at=NOW + timedelta(milliseconds=2500),
        )
    )
    store = _MemoryJobStore(job)
    orchestrator = _Orchestrator(
        _approval_draft(),
        completed=_completed_draft(),
    )
    worker, *_rest = _worker(store, orchestrator=orchestrator)

    result = worker.execute()

    durable = store.jobs[job.id]
    assert orchestrator.decide_calls
    assert store.succeed_calls == 1
    assert result.outcome is WorkerIterationOutcome.FAILED
    assert result.status is JobStatus.FAILED
    assert result.failure_code is JobFailureCode.AUTHORIZATION_EXPIRED
    assert durable.status is JobStatus.FAILED
    assert durable.result is None
    assert durable.failure is not None
    assert durable.failure.code is JobFailureCode.AUTHORIZATION_EXPIRED


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (_approval_draft(revision=8), JobFailureCode.WORKFLOW_STALE),
        (_approval_draft(plan="f" * 64), JobFailureCode.PLAN_STALE),
        (
            _approval_draft(
                checkpoint_kind=WorkflowCheckpointKind.PUBLICATION_DECISION,
            ),
            JobFailureCode.WORKFLOW_STALE,
        ),
        (
            _approval_draft(stage=WorkflowStage.EXECUTED),
            JobFailureCode.WORKFLOW_STALE,
        ),
    ],
)
def test_stale_revision_plan_or_checkpoint_fails_without_execution(
    current: AgentWorkflowDraft,
    expected: JobFailureCode,
) -> None:
    job = _job()
    store = _MemoryJobStore(job)
    orchestrator = _Orchestrator(current, completed=_completed_draft())
    worker, *_rest = _worker(store, orchestrator=orchestrator)

    result = worker.execute()

    assert result.outcome is WorkerIterationOutcome.FAILED
    assert result.failure_code is expected
    assert store.jobs[job.id].status is JobStatus.FAILED
    assert store.heartbeat_calls == []
    assert orchestrator.decide_calls == []


def test_missing_exact_owner_grant_fails_before_workflow_or_source_io() -> None:
    job = _job()
    store = _MemoryJobStore(job)
    access = _AccessStore(None)
    worker, _access, orchestrator, _af, orchestrator_factory, _capability = _worker(
        store,
        access=access,
    )

    result = worker.execute()

    assert result.failure_code is JobFailureCode.AUTHORIZATION_MISMATCH
    assert result.outcome is WorkerIterationOutcome.FAILED
    assert orchestrator_factory.calls == []
    assert orchestrator.inspect_calls == []


@pytest.mark.parametrize(
    ("error_code", "outcome", "failure_code"),
    [
        (
            WorkflowAccessErrorCode.CONFLICT,
            WorkerIterationOutcome.FAILED,
            JobFailureCode.AUTHORIZATION_MISMATCH,
        ),
        (
            WorkflowAccessErrorCode.IDENTITY_MISMATCH,
            WorkerIterationOutcome.FAILED,
            JobFailureCode.AUTHORIZATION_MISMATCH,
        ),
        (
            WorkflowAccessErrorCode.STORE_FAILURE,
            WorkerIterationOutcome.DEAD_LETTERED,
            JobFailureCode.UNEXPECTED_WORKER_FAILURE,
        ),
    ],
)
def test_workflow_access_failures_preserve_revocation_vs_uncertainty(
    error_code: WorkflowAccessErrorCode,
    outcome: WorkerIterationOutcome,
    failure_code: JobFailureCode,
) -> None:
    job = _job()
    store = _MemoryJobStore(job)
    access = _AccessStore(
        _grant(),
        error=WorkflowAccessError(error_code, "sanitized workflow access failure"),
    )
    worker, _access, orchestrator, _af, orchestrator_factory, _capability = _worker(
        store,
        access=access,
    )

    result = worker.execute()

    assert result.outcome is outcome
    assert result.failure_code is failure_code
    assert orchestrator_factory.calls == []
    assert orchestrator.inspect_calls == []


def test_completed_workflow_is_reconciled_without_replaying_external_execution() -> None:
    job = _job()
    store = _MemoryJobStore(job)
    completed = _completed_draft()
    orchestrator = _Orchestrator(completed, completed=completed)
    worker, *_rest = _worker(store, orchestrator=orchestrator)

    result = worker.execute()

    assert result.outcome is WorkerIterationOutcome.SUCCEEDED
    assert store.jobs[job.id].status is JobStatus.SUCCEEDED
    assert orchestrator.inspect_calls == [WORKFLOW_ID]
    assert orchestrator.decide_calls == []
    assert store.heartbeat_calls == []


def test_ambiguous_started_external_trace_is_recovered_then_dead_lettered() -> None:
    job = _job()
    store = _MemoryJobStore(job)
    orchestrator = _Orchestrator(
        _ambiguous_draft(),
        completed=_completed_draft(),
    )
    worker, *_rest = _worker(store, orchestrator=orchestrator)

    result = worker.execute()

    assert result.outcome is WorkerIterationOutcome.DEAD_LETTERED
    assert result.failure_code is JobFailureCode.AMBIGUOUS_EXTERNAL_EFFECT
    assert store.jobs[job.id].status is JobStatus.DEAD_LETTERED
    assert orchestrator.recover_calls == [(WORKFLOW_ID, WorkflowOperation.PREVIEW_EXECUTION)]
    assert orchestrator.decide_calls == []


def test_failed_workflow_return_requires_human_retry_and_no_job_redelivery() -> None:
    job = _job()
    store = _MemoryJobStore(job)
    orchestrator = _Orchestrator(
        _approval_draft(),
        completed=_failed_draft(),
    )
    worker, *_rest = _worker(store, orchestrator=orchestrator)

    result = worker.execute()

    assert result.outcome is WorkerIterationOutcome.FAILED
    assert result.failure_code is JobFailureCode.WORKFLOW_RETRY_REQUIRED
    assert store.jobs[job.id].status is JobStatus.FAILED
    assert store.jobs[job.id].available_at is None
    assert len(orchestrator.decide_calls) == 1


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("unexpected adapter crash"),
        WorkflowError(WorkflowErrorCode.STORE_FAILURE, "sanitized workflow outage"),
    ],
)
def test_uncertain_orchestrator_crash_dead_letters_without_blind_replay(
    error: Exception,
) -> None:
    job = _job()
    store = _MemoryJobStore(job)
    orchestrator = _Orchestrator(
        _approval_draft(),
        completed=_completed_draft(),
        decide_error=error,
    )
    worker, *_rest = _worker(store, orchestrator=orchestrator)

    result = worker.execute()

    assert result.outcome is WorkerIterationOutcome.DEAD_LETTERED
    assert result.failure_code is JobFailureCode.UNEXPECTED_WORKER_FAILURE
    assert store.jobs[job.id].status is JobStatus.DEAD_LETTERED
    assert len(orchestrator.decide_calls) == 1


def test_transient_failure_uses_domain_backoff_then_exhaustion_dead_letters() -> None:
    job = _job(max_attempts=2)
    store = _MemoryJobStore(job)
    worker, *_rest = _worker(store)
    first_claim = store.claim_next(
        worker_id=WORKER_ID,
        lease_token=LEASE_TOKEN,
        lease_duration=timedelta(seconds=60),
    )
    assert first_claim is not None

    first = worker._record_failure(
        first_claim,
        LEASE_TOKEN,
        JobFailureCode.SOURCE_TIMEOUT,
        at=NOW + timedelta(seconds=2),
    )

    assert first.outcome is WorkerIterationOutcome.RETRY_SCHEDULED
    assert store.fail_calls[-1] == (JobFailureCode.SOURCE_TIMEOUT, timedelta(seconds=5))
    retrying = store.jobs[job.id]
    assert retrying.status is JobStatus.RETRY_WAIT
    store.database_now = retrying.available_at or store.database_now
    second_claim = store.claim_next(
        worker_id=WORKER_ID,
        lease_token=LEASE_TOKEN,
        lease_duration=timedelta(seconds=60),
    )
    assert second_claim is not None

    exhausted = worker._record_failure(
        second_claim,
        LEASE_TOKEN,
        JobFailureCode.SOURCE_TIMEOUT,
        at=NOW + timedelta(seconds=20),
    )

    assert exhausted.outcome is WorkerIterationOutcome.DEAD_LETTERED
    assert store.fail_calls[-1] == (JobFailureCode.SOURCE_TIMEOUT, None)
    assert store.jobs[job.id].status is JobStatus.DEAD_LETTERED


@pytest.mark.parametrize(
    ("workflow_code", "operation", "job_code"),
    (
        (
            "planning_context_unavailable",
            WorkflowOperation.SQL_VALIDATION,
            JobFailureCode.REGISTRY_UNAVAILABLE,
        ),
        (
            "source_unavailable",
            WorkflowOperation.PREVIEW_EXECUTION,
            JobFailureCode.SOURCE_UNAVAILABLE,
        ),
        (
            "source_timeout",
            WorkflowOperation.PREVIEW_EXECUTION,
            JobFailureCode.SOURCE_TIMEOUT,
        ),
        (
            "source_unavailable",
            WorkflowOperation.REJECTION_INSPECTION,
            JobFailureCode.SOURCE_UNAVAILABLE,
        ),
    ),
)
def test_public_worker_path_retries_typed_transient_workflow_failure_then_exhausts(
    workflow_code: str,
    operation: WorkflowOperation,
    job_code: JobFailureCode,
) -> None:
    job = _job(max_attempts=2)
    store = _MemoryJobStore(job)
    first_failure = _transient_failed_draft(
        code=workflow_code,
        operation=operation,
        attempt=1,
        revision=9,
    )
    second_failure = _transient_failed_draft(
        code=workflow_code,
        operation=operation,
        attempt=2,
        revision=11,
    )
    orchestrator = _Orchestrator(
        _approval_draft(),
        completed=first_failure,
        retry_completed=second_failure,
    )
    clock = _Clock()
    worker, *_rest = _worker(
        store,
        orchestrator=orchestrator,
        clock=clock,
    )

    first = worker.execute()

    assert first.outcome is WorkerIterationOutcome.RETRY_SCHEDULED
    assert first.failure_code is job_code
    retrying = store.jobs[job.id]
    assert retrying.status is JobStatus.RETRY_WAIT
    assert retrying.available_at is not None
    store.database_now = retrying.available_at
    clock.current = retrying.available_at + timedelta(milliseconds=500)

    exhausted = worker.execute()

    assert exhausted.outcome is WorkerIterationOutcome.DEAD_LETTERED
    assert exhausted.failure_code is job_code
    assert store.jobs[job.id].status is JobStatus.DEAD_LETTERED
    assert len(orchestrator.decide_calls) == 1
    assert len(orchestrator.retry_calls) == 1
    _, retry_decision, reserved = orchestrator.retry_calls[0]
    assert retry_decision.failure_fingerprint == first_failure.failure.fingerprint
    assert retry_decision.operation is operation
    assert reserved is not None
    assert reserved.actor == SUBMITTER
    assert reserved.plan_fingerprint == PLAN


@pytest.mark.parametrize(
    "operation",
    (
        WorkflowOperation.PREVIEW_EXECUTION,
        WorkflowOperation.REJECTION_INSPECTION,
    ),
)
def test_public_worker_path_terminal_source_policy_failure_is_not_retried(
    operation: WorkflowOperation,
) -> None:
    job = _job(max_attempts=3)
    store = _MemoryJobStore(job)
    terminal_failure = _transient_failed_draft(
        code="source_policy_rejected",
        operation=operation,
        attempt=1,
        revision=9,
        retryable=False,
    )
    orchestrator = _Orchestrator(
        _approval_draft(),
        completed=terminal_failure,
    )
    worker, *_rest = _worker(store, orchestrator=orchestrator)

    result = worker.execute()

    assert result.outcome is WorkerIterationOutcome.FAILED
    assert result.failure_code is JobFailureCode.SOURCE_POLICY_REJECTED
    assert store.jobs[job.id].status is JobStatus.FAILED
    assert store.jobs[job.id].available_at is None
    assert len(orchestrator.decide_calls) == 1
    assert orchestrator.retry_calls == []


@pytest.mark.parametrize(
    "operation",
    (
        WorkflowOperation.SQL_VALIDATION,
        WorkflowOperation.PREVIEW_EXECUTION,
        WorkflowOperation.REJECTION_INSPECTION,
    ),
)
def test_public_worker_path_preserves_terminal_semantic_context_stale_code(
    operation: WorkflowOperation,
) -> None:
    job = _job(max_attempts=3)
    store = _MemoryJobStore(job)
    terminal_failure = _transient_failed_draft(
        code="semantic_context_stale",
        operation=operation,
        attempt=1,
        revision=9,
        retryable=False,
    )
    orchestrator = _Orchestrator(
        _approval_draft(),
        completed=terminal_failure,
    )
    worker, *_rest = _worker(store, orchestrator=orchestrator)

    result = worker.execute()

    assert result.outcome is WorkerIterationOutcome.FAILED
    assert result.failure_code is JobFailureCode.SEMANTIC_CONTEXT_STALE
    assert store.jobs[job.id].status is JobStatus.FAILED
    assert store.jobs[job.id].available_at is None
    assert orchestrator.retry_calls == []


def test_expired_final_lease_is_reaped_before_claim_and_cannot_stick_forever() -> None:
    queued = _job(max_attempts=1)
    leased = claim_job(
        queued,
        worker_id="crashed-worker",
        lease_token="crashed-capability-" + ("z" * 40),
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(seconds=1),
    )
    store = _MemoryJobStore(
        leased,
        database_now=NOW + timedelta(seconds=2),
    )
    worker, *_rest = _worker(
        store,
        clock=_Clock(NOW + timedelta(seconds=2)),
    )

    result = worker.execute()

    assert result.outcome is WorkerIterationOutcome.IDLE
    reaped = store.jobs[queued.id]
    assert reaped.status is JobStatus.DEAD_LETTERED
    assert reaped.failure is not None
    assert reaped.failure.code is JobFailureCode.AMBIGUOUS_EXTERNAL_EFFECT
    assert store.reap_calls == 1


def test_store_outage_and_invalid_capability_are_sanitized() -> None:
    store = _MemoryJobStore()
    store.raise_on_expire = True
    worker, *_rest = _worker(store)

    with pytest.raises(WorkerUseCaseError) as unavailable:
        worker.execute()
    assert unavailable.value.code is WorkerUseCaseErrorCode.STORE_UNAVAILABLE
    assert "outage" not in str(unavailable.value)

    invalid_store = _MemoryJobStore()
    capability = _CapabilityFactory()
    capability.invalid = True
    invalid_worker, *_rest = _worker(
        invalid_store,
        capability_factory=capability,
    )
    with pytest.raises(WorkerUseCaseError) as invalid:
        invalid_worker.execute()
    assert invalid.value.code is WorkerUseCaseErrorCode.INVALID_CAPABILITY
    assert invalid_store.claim_calls == 0


def test_heartbeat_supervisor_failure_never_completes_with_a_stale_fence() -> None:
    job = _job()
    store = _MemoryJobStore(job)
    supervisor = _InlineHeartbeatSupervisor()
    supervisor.fail = True
    worker, *_rest = _worker(store, heartbeat_supervisor=supervisor)

    with pytest.raises(WorkerUseCaseError) as lost:
        worker.execute()

    assert lost.value.code is WorkerUseCaseErrorCode.LEASE_LOST
    assert str(lost.value) == "The worker lease could not be safely maintained."
    assert "sensitive" not in str(lost.value)
    assert store.succeed_calls == 0
    assert store.fail_calls == []
    assert store.jobs[job.id].status is JobStatus.LEASED


def test_capability_factory_crash_is_sanitized_without_claim() -> None:
    store = _MemoryJobStore()
    capability = _CapabilityFactory()
    capability.crash = True
    worker, *_rest = _worker(store, capability_factory=capability)

    with pytest.raises(WorkerUseCaseError) as invalid:
        worker.execute()

    assert invalid.value.code is WorkerUseCaseErrorCode.INVALID_CAPABILITY
    assert "internals" not in str(invalid.value)
    assert store.claim_calls == 0
