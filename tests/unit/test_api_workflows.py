"""Adversarial unit tests for authenticated M24 execution-job use cases."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from schemabridge.adapters.storage.identity_resolving import (
    IdentityResolvingBackgroundJobApiStore,
)
from schemabridge.application.api_workflows import (
    EXECUTION_CONFIRMATION,
    CancelExecutionJob,
    ExecutionJobUseCaseError,
    ExecutionJobUseCaseErrorCode,
    InspectExecutionJob,
    SubmitExecutionJob,
)
from schemabridge.application.authorization import DenyByDefaultAuthorizationPolicy
from schemabridge.application.ports.background_jobs import (
    JobStoreError,
    JobStoreErrorCode,
)
from schemabridge.domain.background_jobs import (
    BackgroundJob,
    JobRejectionCount,
    JobResultSummary,
    JobStatus,
    JobSubmissionResult,
    JobWorkflowAccessScope,
    claim_job,
    complete_job,
    request_job_cancellation,
)
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
    WorkflowAccessGrant,
)
from schemabridge.domain.identity_rotation import IdentityAuthorizationScope
from schemabridge.domain.workflows import (
    AgentWorkflowDraft,
    ExecutionWorkflowDecision,
    WorkflowCheckpoint,
    WorkflowCheckpointKind,
    WorkflowOperation,
    WorkflowStage,
)

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
WORKSPACE = "sb_workspace_a"
OWNER = "sb_actor_owner"
OTHER_ACTOR = "sb_actor_other"
WORKFLOW_ID = "workflow-unit-1"
PLAN = "a" * 64
IDEMPOTENCY_KEY = "request-key-safe-0001"
LEASE_TOKEN = "test-worker-capability-" + ("x" * 32)


class _Clock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


class _MemoryJobStore:
    def __init__(self) -> None:
        self.jobs: dict[str, BackgroundJob] = {}
        self.load_calls = 0
        self.submit_calls = 0
        self.cancel_calls = 0
        self.race_cancel = False

    def submit(self, job: BackgroundJob) -> JobSubmissionResult:
        self.submit_calls += 1
        existing = self.jobs.get(job.id)
        if existing is not None:
            if (
                existing.idempotency_digest != job.idempotency_digest
                or existing.request_fingerprint != job.request_fingerprint
            ):
                raise JobStoreError(
                    JobStoreErrorCode.IDEMPOTENCY_CONFLICT,
                    "sanitized idempotency conflict",
                )
            return JobSubmissionResult(job=existing, replayed=True)
        self.jobs[job.id] = job
        return JobSubmissionResult(job=job)

    def load(
        self,
        workspace_id: str,
        job_id: str,
        *,
        submitting_actor_id: str | None = None,
    ) -> BackgroundJob | None:
        self.load_calls += 1
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

    def load_by_idempotency(
        self,
        workspace_id: str,
        submitting_actor_id: str,
        idempotency_digest: str,
    ) -> BackgroundJob | None:
        self.load_calls += 1
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

    def request_cancellation(
        self,
        workspace_id: str,
        job_id: str,
        *,
        submitting_actor_id: str | None,
        requested_at: datetime,
    ) -> BackgroundJob | None:
        self.cancel_calls += 1
        job = self.load(
            workspace_id,
            job_id,
            submitting_actor_id=submitting_actor_id,
        )
        if job is None:
            return None
        cancelled = request_job_cancellation(job, requested_at=requested_at)
        self.jobs[job_id] = cancelled
        if self.race_cancel:
            raise JobStoreError(JobStoreErrorCode.STATE_CONFLICT, "sanitized race")
        return cancelled


class _AccessStore:
    def __init__(self, grant: WorkflowAccessGrant | None) -> None:
        self.grant_record = grant
        self.loads: list[tuple[str, str, str | None]] = []

    def load(
        self,
        workspace_id: str,
        workflow_id: str,
        *,
        owner_principal_id: str | None = None,
    ) -> WorkflowAccessGrant | None:
        self.loads.append((workspace_id, workflow_id, owner_principal_id))
        grant = self.grant_record
        if (
            grant is None
            or grant.workspace_id != workspace_id
            or grant.workflow_id != workflow_id
            or (owner_principal_id is not None and grant.owner_actor_id != owner_principal_id)
        ):
            return None
        return grant


class _Orchestrator:
    def __init__(self, draft: AgentWorkflowDraft) -> None:
        self.draft = draft
        self.inspect_calls: list[str] = []

    def inspect(self, workflow_id: str) -> AgentWorkflowDraft:
        self.inspect_calls.append(workflow_id)
        return self.draft

    def decide_execution(
        self,
        workflow_id: str,
        decision: ExecutionWorkflowDecision,
        *,
        should_continue: Callable[[], bool] | None = None,
    ) -> AgentWorkflowDraft:
        del should_continue
        raise AssertionError("the API process must never execute a workflow")

    def recover_interrupted(
        self,
        workflow_id: str,
        *,
        expected_operation: WorkflowOperation,
    ) -> AgentWorkflowDraft:
        raise AssertionError("the API process must never recover a workflow")


class _ScopedFactory:
    def __init__(self, value: object) -> None:
        self.value = value
        self.calls: list[tuple[str, str]] = []

    def __call__(self, workspace_id: str, actor_id: str) -> object:
        self.calls.append((workspace_id, actor_id))
        return self.value


def _principal(
    *,
    actor_id: str = OWNER,
    workspace_id: str = WORKSPACE,
    roles: frozenset[IdentityRole] = frozenset({IdentityRole.ANALYST}),
    authenticated_at: datetime = NOW - timedelta(minutes=1),
    expires_at: datetime = NOW + timedelta(hours=1),
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        actor_id=actor_id,
        workspace_id=workspace_id,
        roles=roles,
        authentication_method=AuthenticationMethod.OIDC,
        authenticated_at=authenticated_at,
        expires_at=expires_at,
    )


def _grant() -> WorkflowAccessGrant:
    return WorkflowAccessGrant(
        workflow_id=WORKFLOW_ID,
        workspace_id=WORKSPACE,
        owner_actor_id=OWNER,
        created_at=NOW - timedelta(minutes=2),
    )


def _draft(
    *,
    workflow_id: str = WORKFLOW_ID,
    revision: int = 7,
    stage: WorkflowStage = WorkflowStage.DECISION_REQUIRED,
    plan: str = PLAN,
    checkpoint_kind: WorkflowCheckpointKind = WorkflowCheckpointKind.EXECUTION_APPROVAL,
) -> AgentWorkflowDraft:
    return AgentWorkflowDraft(
        id=workflow_id,
        revision=revision,
        stage=stage,
        text="Agrupa clientes por fecha.",
        language="es",
        requested_datasets=(PhysicalDatasetRef("crm.customers"),),
        plan_fingerprint=plan,
        checkpoint=WorkflowCheckpoint(
            kind=checkpoint_kind,
            fingerprint=plan,
            reason="Approval is required before governed preview execution.",
        ),
        created_at=NOW - timedelta(minutes=2),
        updated_at=NOW - timedelta(minutes=1),
    )


def _use_case(
    *,
    store: _MemoryJobStore | None = None,
    access: _AccessStore | None = None,
    orchestrator: _Orchestrator | None = None,
    principal_clock: _Clock | None = None,
    authorization_ttl: timedelta = timedelta(minutes=5),
) -> tuple[
    SubmitExecutionJob,
    _MemoryJobStore,
    _AccessStore,
    _Orchestrator,
    _ScopedFactory,
    _ScopedFactory,
]:
    selected_store = store or _MemoryJobStore()
    selected_access = access or _AccessStore(_grant())
    selected_orchestrator = orchestrator or _Orchestrator(_draft())
    access_factory = _ScopedFactory(selected_access)
    orchestrator_factory = _ScopedFactory(selected_orchestrator)
    use_case = SubmitExecutionJob(
        job_store=selected_store,
        access_store_factory=access_factory,  # type: ignore[arg-type]
        orchestrator_factory=orchestrator_factory,  # type: ignore[arg-type]
        authorization=DenyByDefaultAuthorizationPolicy(),
        clock=principal_clock or _Clock(),
        authorization_ttl=authorization_ttl,
    )
    return (
        use_case,
        selected_store,
        selected_access,
        selected_orchestrator,
        access_factory,
        orchestrator_factory,
    )


def _submit(
    use_case: SubmitExecutionJob,
    principal: AuthenticatedPrincipal | None = None,
    *,
    workflow_id: str = WORKFLOW_ID,
    revision: int = 7,
    plan: str = PLAN,
    confirmation: str = EXECUTION_CONFIRMATION,
    key: str = IDEMPOTENCY_KEY,
) -> JobSubmissionResult:
    return use_case.execute(
        principal or _principal(),
        workflow_id=workflow_id,
        expected_workflow_revision=revision,
        expected_plan_fingerprint=plan,
        confirmation=confirmation,
        idempotency_key=key,
    )


def _persist_success(store: _MemoryJobStore, job: BackgroundJob) -> BackgroundJob:
    claimed = claim_job(
        job,
        worker_id="worker-unit-1",
        lease_token=LEASE_TOKEN,
        claimed_at=NOW + timedelta(seconds=1),
        lease_expires_at=NOW + timedelta(minutes=2),
    )
    result = JobResultSummary(
        workflow_id=WORKFLOW_ID,
        workflow_revision=11,
        stage=WorkflowStage.PUBLICATION_PROPOSED,
        row_count=2,
        preview_fingerprint="b" * 64,
        rejected_count=1,
        rejection_code_counts=(JobRejectionCount(code="null_join_key", count=1),),
        truncated=False,
        completed_at=NOW + timedelta(seconds=2),
    )
    succeeded = complete_job(
        claimed,
        worker_id="worker-unit-1",
        lease_token=LEASE_TOKEN,
        fencing_token=1,
        result=result,
    )
    store.jobs[job.id] = succeeded
    return succeeded


def test_submit_reserves_exact_scope_and_never_persists_raw_key() -> None:
    use_case, store, access, orchestrator, access_factory, orchestrator_factory = _use_case()

    result = _submit(use_case)

    assert result.job.status is JobStatus.QUEUED
    assert not result.replayed
    assert result.job.authorization.workspace_id == WORKSPACE
    assert result.job.authorization.workflow_owner_actor_id == OWNER
    assert result.job.authorization.submitting_actor_id == OWNER
    assert result.job.authorization.expected_workflow_revision == 7
    assert result.job.authorization.expected_plan_fingerprint == PLAN
    assert result.job.authorization.expires_at == NOW + timedelta(minutes=5)
    assert store.submit_calls == 1
    assert access.loads == [(WORKSPACE, WORKFLOW_ID, OWNER)]
    assert orchestrator.inspect_calls == [WORKFLOW_ID]
    assert access_factory.calls == [(WORKSPACE, OWNER)]
    assert orchestrator_factory.calls == [(WORKSPACE, OWNER)]
    serialized = json.dumps(result.model_dump(mode="json"), sort_keys=True)
    assert IDEMPOTENCY_KEY not in serialized
    assert IDEMPOTENCY_KEY not in repr(result)


def test_submit_separates_current_submitter_from_historical_workflow_scope() -> None:
    historical_workspace = "sb_workspace_historical"
    historical_owner = "sb_actor_historical"
    historical_grant = WorkflowAccessGrant(
        workflow_id=WORKFLOW_ID,
        workspace_id=historical_workspace,
        owner_actor_id=historical_owner,
        created_at=NOW - timedelta(minutes=2),
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

    use_case, _store, _access, _orchestrator, _access_factory, orchestrator_factory = _use_case(
        access=_HistoricalAccess(historical_grant)
    )

    result = _submit(use_case)

    assert result.job.authorization.workspace_id == WORKSPACE
    assert result.job.authorization.submitting_actor_id == OWNER
    assert result.job.authorization.workflow_workspace_id == historical_workspace
    assert result.job.authorization.workflow_owner_actor_id == historical_owner
    assert result.job.authorization.workflow_access_scope is JobWorkflowAccessScope.OWNER
    assert orchestrator_factory.calls == [(historical_workspace, historical_owner)]


def test_rotated_submitter_replays_pre_rotation_job_without_workflow_io() -> None:
    old_workspace = f"sb_workspace_v1_{'1' * 64}"
    old_actor = f"sb_actor_v1_{'2' * 64}"
    new_workspace = f"sb_workspace_v2_{'3' * 64}"
    new_actor = f"sb_actor_v2_{'4' * 64}"
    raw_store = _MemoryJobStore()
    old_use_case, *_old_dependencies = _use_case(
        store=raw_store,
        access=_AccessStore(
            WorkflowAccessGrant(
                workflow_id=WORKFLOW_ID,
                workspace_id=old_workspace,
                owner_actor_id=old_actor,
                created_at=NOW - timedelta(minutes=2),
            )
        ),
    )
    old_result = _submit(
        old_use_case,
        _principal(workspace_id=old_workspace, actor_id=old_actor),
    )

    class _RotationResolver:
        def resolve_workspace_aliases(self, workspace_id: str) -> tuple[str, ...]:
            assert workspace_id == new_workspace
            return old_workspace, new_workspace

        def resolve_authorization_scopes(
            self,
            workspace_id: str,
            actor_id: str,
        ) -> tuple[IdentityAuthorizationScope, ...]:
            assert (workspace_id, actor_id) == (new_workspace, new_actor)
            return (
                IdentityAuthorizationScope(
                    workspace_id=old_workspace,
                    actor_id=old_actor,
                    key_version="v1",
                ),
                IdentityAuthorizationScope(
                    workspace_id=new_workspace,
                    actor_id=new_actor,
                    key_version="v2",
                ),
            )

    protected_access = _AccessStore(None)
    protected_orchestrator = _Orchestrator(_draft())
    new_use_case = SubmitExecutionJob(
        job_store=IdentityResolvingBackgroundJobApiStore(
            raw_store,
            _RotationResolver(),
        ),
        access_store_factory=_ScopedFactory(protected_access),  # type: ignore[arg-type]
        orchestrator_factory=_ScopedFactory(protected_orchestrator),  # type: ignore[arg-type]
        authorization=DenyByDefaultAuthorizationPolicy(),
        clock=_Clock(),
    )

    replay = _submit(
        new_use_case,
        _principal(workspace_id=new_workspace, actor_id=new_actor),
    )

    assert replay.replayed
    assert replay.job == old_result.job
    assert replay.job.authorization.workspace_id == old_workspace
    assert protected_access.loads == []
    assert protected_orchestrator.inspect_calls == []
    assert raw_store.submit_calls == 1


def test_platform_admin_executes_owner_workflow_through_owner_scoped_orchestrator() -> None:
    principal = _principal(
        actor_id=OTHER_ACTOR,
        roles=frozenset({IdentityRole.PLATFORM_ADMIN}),
    )
    use_case, _store, access, _orchestrator, _access_factory, orchestrator_factory = _use_case()

    result = _submit(use_case, principal)

    assert result.job.authorization.submitting_actor_id == OTHER_ACTOR
    assert result.job.authorization.workflow_owner_actor_id == OWNER
    assert access.loads == [(WORKSPACE, WORKFLOW_ID, None)]
    assert orchestrator_factory.calls == [(WORKSPACE, OWNER)]


def test_authorization_ttl_is_bounded_by_session_and_original_authentication() -> None:
    principal = _principal(
        authenticated_at=NOW - timedelta(minutes=59),
        expires_at=NOW + timedelta(minutes=20),
    )
    use_case, *_rest = _use_case(authorization_ttl=timedelta(minutes=10))

    result = _submit(use_case, principal)

    assert result.job.authorization.expires_at == NOW + timedelta(minutes=1)
    with pytest.raises(ValueError, match="authorization_ttl"):
        _use_case(authorization_ttl=timedelta(seconds=29))
    with pytest.raises(ValueError, match="authorization_ttl"):
        _use_case(authorization_ttl=timedelta(minutes=16))


@pytest.mark.parametrize(
    "principal",
    [
        _principal(workspace_id="sb_workspace_b"),
        _principal(actor_id=OTHER_ACTOR),
        _principal(roles=frozenset({IdentityRole.AUDITOR})),
        _principal(
            authenticated_at=NOW - timedelta(hours=2),
            expires_at=NOW - timedelta(hours=1),
        ),
    ],
)
def test_cross_tenant_owner_role_and_expired_session_denials_are_indistinguishable(
    principal: AuthenticatedPrincipal,
) -> None:
    use_case, store, _access, orchestrator, _access_factory, _orchestrator_factory = _use_case()

    with pytest.raises(ExecutionJobUseCaseError) as denied:
        _submit(use_case, principal)

    assert denied.value.code is ExecutionJobUseCaseErrorCode.UNAVAILABLE
    assert str(denied.value) == (
        "The execution job is not available to the authenticated principal."
    )
    assert store.submit_calls == 0
    assert orchestrator.inspect_calls == []


def test_revoked_execute_role_cannot_replay_or_read_an_existing_job() -> None:
    use_case, store, _access, orchestrator, _access_factory, _orchestrator_factory = _use_case()
    _submit(use_case)
    loads_before_revocation = store.load_calls
    inspect_calls_before_revocation = tuple(orchestrator.inspect_calls)
    revoked = _principal(roles=frozenset({IdentityRole.AUDITOR}))

    with pytest.raises(ExecutionJobUseCaseError) as denied:
        _submit(use_case, revoked)

    assert denied.value.code is ExecutionJobUseCaseErrorCode.UNAVAILABLE
    assert store.load_calls == loads_before_revocation
    assert tuple(orchestrator.inspect_calls) == inspect_calls_before_revocation


@pytest.mark.parametrize(
    ("workflow_id", "revision", "plan", "confirmation", "key"),
    [
        ("workflow;drop", 7, PLAN, EXECUTION_CONFIRMATION, IDEMPOTENCY_KEY),
        (WORKFLOW_ID, 0, PLAN, EXECUTION_CONFIRMATION, IDEMPOTENCY_KEY),
        (WORKFLOW_ID, 7, "A" * 64, EXECUTION_CONFIRMATION, IDEMPOTENCY_KEY),
        (WORKFLOW_ID, 7, PLAN, "approve", IDEMPOTENCY_KEY),
        (WORKFLOW_ID, 7, PLAN, EXECUTION_CONFIRMATION, "too-short"),
        (WORKFLOW_ID, 7, PLAN, EXECUTION_CONFIRMATION, "x" * 129),
        (WORKFLOW_ID, 7, PLAN, EXECUTION_CONFIRMATION, "x" * 15 + "/"),
    ],
)
def test_malformed_submission_is_rejected_before_any_store_or_workflow_io(
    workflow_id: str,
    revision: int,
    plan: str,
    confirmation: str,
    key: str,
) -> None:
    use_case, store, access, orchestrator, _access_factory, _orchestrator_factory = _use_case()

    with pytest.raises(ExecutionJobUseCaseError) as invalid:
        _submit(
            use_case,
            workflow_id=workflow_id,
            revision=revision,
            plan=plan,
            confirmation=confirmation,
            key=key,
        )

    assert invalid.value.code is ExecutionJobUseCaseErrorCode.INVALID_REQUEST
    assert store.load_calls == 0
    assert store.submit_calls == 0
    assert access.loads == []
    assert orchestrator.inspect_calls == []


@pytest.mark.parametrize(
    "draft",
    [
        _draft(revision=8),
        _draft(plan="b" * 64),
        _draft(checkpoint_kind=WorkflowCheckpointKind.PUBLICATION_DECISION),
        _draft(stage=WorkflowStage.EXECUTED),
        _draft(workflow_id="workflow-unit-2"),
    ],
)
def test_submission_rejects_stale_or_non_execution_checkpoint(
    draft: AgentWorkflowDraft,
) -> None:
    use_case, store, _access, _orchestrator, _access_factory, _orchestrator_factory = _use_case(
        orchestrator=_Orchestrator(draft)
    )

    with pytest.raises(ExecutionJobUseCaseError) as unavailable:
        _submit(use_case)

    assert unavailable.value.code is ExecutionJobUseCaseErrorCode.UNAVAILABLE
    assert store.submit_calls == 0


def test_exact_replay_remains_stable_after_success_and_workflow_advancement() -> None:
    use_case, store, _access, orchestrator, _access_factory, _orchestrator_factory = _use_case()
    submitted = _submit(use_case)
    succeeded = _persist_success(store, submitted.job)
    orchestrator.draft = _draft(revision=8, stage=WorkflowStage.EXECUTED)
    inspect_count = len(orchestrator.inspect_calls)

    replay = _submit(use_case)

    assert replay.replayed
    assert replay.job == succeeded
    assert store.submit_calls == 1
    assert len(orchestrator.inspect_calls) == inspect_count


@pytest.mark.parametrize(
    ("workflow_id", "revision", "plan"),
    [
        ("workflow-unit-2", 7, PLAN),
        (WORKFLOW_ID, 8, PLAN),
        (WORKFLOW_ID, 7, "b" * 64),
    ],
)
def test_same_idempotency_key_with_changed_payload_conflicts_without_protected_io(
    workflow_id: str,
    revision: int,
    plan: str,
) -> None:
    use_case, store, access, orchestrator, _access_factory, _orchestrator_factory = _use_case()
    _submit(use_case)
    access.loads.clear()
    orchestrator.inspect_calls.clear()

    with pytest.raises(ExecutionJobUseCaseError) as conflict:
        _submit(use_case, workflow_id=workflow_id, revision=revision, plan=plan)

    assert conflict.value.code is ExecutionJobUseCaseErrorCode.IDEMPOTENCY_CONFLICT
    assert store.submit_calls == 1
    assert access.loads == []
    assert orchestrator.inspect_calls == []


def test_submit_maps_atomic_job_capacity_denial_to_typed_safe_error() -> None:
    secret = "private tenant capacity row says current_count=6"

    class _CapacityDeniedStore(_MemoryJobStore):
        def submit(self, job: BackgroundJob) -> JobSubmissionResult:
            del job
            self.submit_calls += 1
            raise JobStoreError(
                JobStoreErrorCode.CAPACITY_EXCEEDED,
                secret,
            )

    store = _CapacityDeniedStore()
    use_case, _store, access, orchestrator, _access_factory, _orchestrator_factory = _use_case(
        store=store
    )

    with pytest.raises(ExecutionJobUseCaseError) as denied:
        _submit(use_case)

    assert denied.value.code is ExecutionJobUseCaseErrorCode.CAPACITY_EXCEEDED
    assert str(denied.value) == "The tenant execution-job capacity has been reached."
    assert secret not in str(denied.value)
    assert store.submit_calls == 1
    assert access.loads == [(WORKSPACE, WORKFLOW_ID, OWNER)]
    assert orchestrator.inspect_calls == [WORKFLOW_ID]


def test_inspect_and_cancel_are_tenant_owner_and_role_scoped() -> None:
    use_case, store, *_rest = _use_case()
    job = _submit(use_case).job
    policy = DenyByDefaultAuthorizationPolicy()
    clock = _Clock()
    inspect = InspectExecutionJob(store, policy, clock)
    cancel = CancelExecutionJob(store, policy, clock)

    assert inspect.execute(_principal(), job.id) == job
    auditor = _principal(actor_id=OTHER_ACTOR, roles=frozenset({IdentityRole.AUDITOR}))
    assert inspect.execute(auditor, job.id) == job
    with pytest.raises(ExecutionJobUseCaseError) as auditor_cancel:
        cancel.execute(auditor, job.id)
    assert auditor_cancel.value.code is ExecutionJobUseCaseErrorCode.UNAVAILABLE
    for hidden in (
        _principal(actor_id=OTHER_ACTOR),
        _principal(workspace_id="sb_workspace_b"),
    ):
        with pytest.raises(ExecutionJobUseCaseError) as unavailable:
            inspect.execute(hidden, job.id)
        assert unavailable.value.code is ExecutionJobUseCaseErrorCode.UNAVAILABLE

    cancelled = cancel.execute(_principal(), job.id)
    assert cancelled.status is JobStatus.CANCELLED
    assert cancel.execute(_principal(), job.id) == cancelled
    assert store.cancel_calls == 1


def test_platform_admin_cancel_and_terminal_race_are_idempotent() -> None:
    use_case, store, *_rest = _use_case()
    job = _submit(use_case).job
    store.race_cancel = True
    admin = _principal(
        actor_id=OTHER_ACTOR,
        roles=frozenset({IdentityRole.PLATFORM_ADMIN}),
    )
    cancel = CancelExecutionJob(
        store,
        DenyByDefaultAuthorizationPolicy(),
        _Clock(),
    )

    cancelled = cancel.execute(admin, job.id)

    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.authorization.submitting_actor_id == OWNER


@pytest.mark.parametrize("job_id", ["bad/id", "x", "X-job", "job;drop"])
def test_inspection_rejects_unsafe_or_unknown_identifier_with_same_safe_error(
    job_id: str,
) -> None:
    use_case, store, *_rest = _use_case()
    _submit(use_case)
    inspect = InspectExecutionJob(
        store,
        DenyByDefaultAuthorizationPolicy(),
        _Clock(),
    )

    with pytest.raises(ExecutionJobUseCaseError) as unavailable:
        inspect.execute(_principal(), job_id)

    assert unavailable.value.code is ExecutionJobUseCaseErrorCode.UNAVAILABLE
