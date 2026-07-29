"""PostgreSQL proof that M24 jobs survive verified OIDC identity rotation."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from tests.integration.connector_target_support import (
    ensure_catalog_connector_target,
    ensure_compatible_catalog_generation,
)

from schemabridge.adapters.catalog.postgres_inventory import (
    PostgresCatalogConnectionStore,
)
from schemabridge.adapters.connectors.postgres_routing import (
    PostgresExecutionTargetResolver,
)
from schemabridge.adapters.control_plane.postgres_identity_bindings import (
    PostgresIdentityBindingResolver,
)
from schemabridge.adapters.control_plane.postgres_identity_rotation import (
    PostgresIdentityRotationStore,
)
from schemabridge.adapters.control_plane.postgres_jobs import (
    PostgresBackgroundJobStore,
)
from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.adapters.storage.identity_resolving import (
    IdentityResolvingBackgroundJobApiStore,
    IdentityResolvingWorkflowAccessStore,
)
from schemabridge.adapters.storage.postgres import (
    PostgresWorkflowAccessStore,
    PostgresWorkflowDraftStore,
)
from schemabridge.adapters.workflows.read_only import ReadOnlyWorkflowInspector
from schemabridge.application.api_workflows import (
    EXECUTION_CONFIRMATION,
    CancelExecutionJob,
    ExecutionJobUseCaseError,
    ExecutionJobUseCaseErrorCode,
    InspectExecutionJob,
    SubmitExecutionJob,
)
from schemabridge.application.authorization import DenyByDefaultAuthorizationPolicy
from schemabridge.application.identity_rotation import (
    ApproveIdentityRotation,
    CompleteIdentityRotation,
    InitializeVerifiedIdentityState,
    PrepareIdentityRotation,
)
from schemabridge.application.job_worker import (
    RunOneJobWorker,
    WorkerExecutionRouteContext,
    WorkerIterationOutcome,
)
from schemabridge.application.ports.background_jobs import LeaseHeartbeatRunResult
from schemabridge.application.ports.identity_rotation import (
    IdentityRotationStoreError,
    IdentityRotationStoreErrorCode,
)
from schemabridge.application.ports.workflow_access import (
    WorkflowAccessError,
    WorkflowAccessErrorCode,
)
from schemabridge.domain.background_jobs import (
    BackgroundJob,
    JobFailureCode,
    JobStatus,
    JobWorkflowAccessScope,
)
from schemabridge.domain.catalog_inventory import (
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogConnectionRegistration,
)
from schemabridge.domain.connectors import GovernedExecutionTarget
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)
from schemabridge.domain.identity_rotation import (
    IdentityInitializationConfirmation,
    IdentityRotationConfirmation,
    VerifiedDualKeyOidcDerivation,
    VerifiedOidcKeyDerivation,
    build_identity_initialization_approval,
)
from schemabridge.domain.workflows import (
    AgentWorkflowDraft,
    ExecutionWorkflowDecision,
    WorkflowCheckpoint,
    WorkflowCheckpointKind,
    WorkflowDecisionAction,
    WorkflowDecisionKind,
    WorkflowDecisionRecord,
    WorkflowExecutionRecord,
    WorkflowOperation,
    WorkflowPublicationProposal,
    WorkflowStage,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations/control_plane"
ADMIN_DSN = "postgresql://postgres:local-only-not-a-secret@127.0.0.1:55434/postgres"
AUDIT_KEYS = {"v1": b"control-audit-key-0123456789-abcdef"}
PLAN = "a" * 64
QUERY = "b" * 64
PREVIEW = "c" * 64


@dataclass(frozen=True)
class _DatabaseUrls:
    database: str
    migrator: str
    runtime: str
    api: str
    worker: str
    catalog: str


@pytest.fixture(scope="module")
def lineage_database() -> Iterator[_DatabaseUrls]:
    database = f"schemabridge_lineage_{uuid4().hex[:16]}"
    admin_dsn = os.environ.get(
        "SCHEMABRIDGE_TEST_CONTROL_ADMIN_DATABASE_URL",
        ADMIN_DSN,
    )
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {} OWNER schemabridge_migrator").format(
                sql.Identifier(database)
            )
        )
        connection.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database))
        )
        connection.execute(
            sql.SQL(
                """
                GRANT CONNECT ON DATABASE {} TO
                    schemabridge_migrator,
                    schemabridge_runtime,
                    schemabridge_api,
                    schemabridge_worker,
                    schemabridge_catalog
                """
            ).format(sql.Identifier(database))
        )

    urls = _DatabaseUrls(
        database=database,
        migrator=_role_dsn("schemabridge_migrator", database),
        runtime=_role_dsn("schemabridge_runtime", database),
        api=_role_dsn("schemabridge_api", database),
        worker=_role_dsn("schemabridge_worker", database),
        catalog=_role_dsn("schemabridge_catalog", database),
    )
    try:
        migrated = PostgresControlPlaneMigrator(urls.migrator, MIGRATIONS).migrate()
        assert migrated.inspection.current_version == 11
        yield urls
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )


def _role_dsn(role: str, database: str) -> str:
    return f"postgresql://{role}:{role}@127.0.0.1:55434/{database}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _opaque(kind: str, key_version: str, label: str) -> str:
    return f"sb_{kind}_{key_version}_{_digest(f'{key_version}:{label}')}"


def _pair(
    namespace: str,
    owner: str,
    *,
    observed_at: datetime,
) -> VerifiedDualKeyOidcDerivation:
    workspace_reference = _digest(f"{namespace}:workspace-reference")
    actor_reference = _digest(f"{namespace}:actor-reference:{owner}")
    verification = _digest(f"{namespace}:verification:{owner}")
    provenance = _digest(f"{namespace}:oidc-provenance")

    def derivation(version: str) -> VerifiedOidcKeyDerivation:
        return VerifiedOidcKeyDerivation(
            verification_id=verification,
            workspace_reference_digest=workspace_reference,
            actor_reference_digest=actor_reference,
            workspace_id=_opaque("workspace", version, namespace),
            actor_id=_opaque("actor", version, f"{namespace}:{owner}"),
            key_version=version,
            provenance_fingerprint=provenance,
            provenance_version=1,
            policy_version=1,
            verified_at=observed_at,
            authentication_method=AuthenticationMethod.OIDC,
        )

    return VerifiedDualKeyOidcDerivation(
        previous=derivation("v1"),
        current=derivation("v2"),
    )


def _rotation_store(urls: _DatabaseUrls) -> PostgresIdentityRotationStore:
    return PostgresIdentityRotationStore(
        urls.runtime,
        AUDIT_KEYS,
        "v1",
    )


def _seed_capacity_policies(
    urls: _DatabaseUrls,
    *workspace_ids: str,
) -> None:
    now = datetime.now(UTC)
    with (
        psycopg.connect(urls.migrator) as connection,
        connection.cursor() as cursor,
    ):
        cursor.executemany(
            """
            INSERT INTO schemabridge_control.tenant_capacity_policies (
                workspace_id, connection_limit, asset_limit, field_limit,
                api_requests_per_minute, nonterminal_job_limit, version,
                updated_by, created_at, updated_at
            ) VALUES (%s, 100, 1000000, 10000000, 10000, 1000, 1,
                      'test_platform_admin', %s, %s)
            ON CONFLICT (workspace_id) DO NOTHING
            """,
            tuple((workspace_id, now, now) for workspace_id in workspace_ids),
        )


def _execution_target(
    urls: _DatabaseUrls,
    *,
    workspace_id: str,
) -> GovernedExecutionTarget:
    connection_id = CatalogConnectionId("connection_identity_lineage")
    PostgresCatalogConnectionStore(
        urls.api,
        application_name="schemabridge-control-api",
    ).register(
        CatalogConnectionRegistration(
            workspace_id=workspace_id,
            connection_id=connection_id,
            display_name="Identity lineage execution source",
            kind=CatalogConnectionKind.SYNTHETIC,
            environment="TEST",
            catalog_scope="identity-lineage",
            requested_by="test_platform_admin",
            requested_at=datetime.now(UTC),
            idempotency_digest=_digest(f"register-target:{workspace_id}"),
        )
    )
    facts = ensure_catalog_connector_target(
        urls.migrator,
        workspace_id=workspace_id,
        connection_id=connection_id,
    )
    ensure_compatible_catalog_generation(
        urls.migrator,
        urls.api,
        urls.catalog,
        workspace_id=workspace_id,
        connection_id=connection_id,
        target=facts,
    )
    target = PostgresExecutionTargetResolver(urls.runtime).resolve_current(
        workspace_id=workspace_id,
        connection_id=connection_id,
    )
    assert target.route_revision == facts.route_revision
    assert target.fingerprint == facts.target_fingerprint
    return target


def _initialize(
    store: PostgresIdentityRotationStore,
    pair: VerifiedDualKeyOidcDerivation,
    *,
    approved_at: datetime,
) -> None:
    _initialize_pairs(store, (pair,), approved_at=approved_at)


def _initialize_pairs(
    store: PostgresIdentityRotationStore,
    pairs: tuple[VerifiedDualKeyOidcDerivation, ...],
    *,
    approved_at: datetime,
) -> None:
    approval = build_identity_initialization_approval(
        pairs,
        evidence_fingerprint=_digest(
            "evidence:" + ",".join(pair.previous.actor_id for pair in pairs)
        ),
        actor=pairs[0].previous.actor_id,
        approved_at=approved_at,
        confirmation=IdentityInitializationConfirmation.INITIALIZE_VERIFIED_OIDC_BINDINGS,
    )
    InitializeVerifiedIdentityState(store).execute(
        pairs,
        evidence_fingerprint=approval.evidence_fingerprint,
        actor=approval.actor,
        approved_at=approval.approved_at,
        confirmation=approval.confirmation,
    )


def _rotate(
    store: PostgresIdentityRotationStore,
    pair: VerifiedDualKeyOidcDerivation,
    *,
    approved_at: datetime,
) -> None:
    _rotate_pairs(store, (pair,), approved_at=approved_at)


def _rotate_pairs(
    store: PostgresIdentityRotationStore,
    pairs: tuple[VerifiedDualKeyOidcDerivation, ...],
    *,
    approved_at: datetime,
) -> None:
    plan = PrepareIdentityRotation(store).execute(
        pairs[0].previous.workspace_id,
        pairs,
    )
    approved = ApproveIdentityRotation(store).execute(
        plan,
        actor=pairs[0].previous.actor_id,
        approved_at=approved_at,
        confirmation=IdentityRotationConfirmation.ROTATE_VERIFIED_OIDC_BINDINGS,
    )
    CompleteIdentityRotation(store).execute(
        plan,
        approved.approval,
        completed_at=approved_at + timedelta(seconds=1),
    )


def _approval_draft(
    workflow_id: str,
    *,
    created_at: datetime,
) -> AgentWorkflowDraft:
    return AgentWorkflowDraft(
        id=workflow_id,
        revision=7,
        stage=WorkflowStage.DECISION_REQUIRED,
        text="Agrupa clientes por fecha.",
        language="es",
        requested_datasets=(PhysicalDatasetRef("crm.customers"),),
        plan_fingerprint=PLAN,
        checkpoint=WorkflowCheckpoint(
            kind=WorkflowCheckpointKind.EXECUTION_APPROVAL,
            fingerprint=PLAN,
            reason="Exact execution approval is required.",
        ),
        created_at=created_at,
        updated_at=created_at + timedelta(seconds=1),
    )


def _completed_draft(
    approval: AgentWorkflowDraft,
    *,
    submitting_actor_id: str,
    completed_at: datetime,
) -> AgentWorkflowDraft:
    execution = WorkflowExecutionRecord(
        plan_fingerprint=PLAN,
        query_fingerprint=QUERY,
        columns=("customer_id",),
        rows=(("synthetic-customer",),),
        row_count=1,
        database_user="schemabridge_reader",
        transaction_read_only=True,
        statement_timeout_ms=5_000,
        truncated=False,
        preview_fingerprint=PREVIEW,
        rejection_codes=(),
        rejected_count=0,
        rejection_complete=True,
    )
    proposal = WorkflowPublicationProposal.create(
        workflow_id=approval.id,
        plan_fingerprint=PLAN,
        execution_fingerprint=PREVIEW,
        request_fingerprint="e" * 64,
    )
    decision = WorkflowDecisionRecord(
        actor=submitting_actor_id,
        kind=WorkflowDecisionKind.EXECUTION,
        action=WorkflowDecisionAction.APPROVE,
        decided_at=completed_at,
        bound_fingerprint=PLAN,
    )
    return approval.model_copy(
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
            "decisions": (decision,),
            "updated_at": completed_at,
        }
    )


class _Clock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class _Factory:
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


class _ExecutionTargetInspector:
    def __init__(
        self,
        delegate: ReadOnlyWorkflowInspector,
        target: GovernedExecutionTarget,
    ) -> None:
        self._delegate = delegate
        self._target = target

    def inspect(self, workflow_id: str) -> AgentWorkflowDraft:
        draft = self._delegate.inspect(workflow_id)
        return draft.model_copy(
            update={
                "resolved_plan": SimpleNamespace(execution_target=self._target),
            }
        )


class _InspectorFactory:
    def __init__(
        self,
        dsn: str,
        *,
        execution_target: GovernedExecutionTarget | None = None,
    ) -> None:
        self.dsn = dsn
        self.execution_target = execution_target
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self,
        workspace_id: str,
        actor_id: str,
    ) -> ReadOnlyWorkflowInspector | _ExecutionTargetInspector:
        self.calls.append((workspace_id, actor_id))
        inspector = ReadOnlyWorkflowInspector(
            PostgresWorkflowDraftStore(
                self.dsn,
                workspace_id=workspace_id,
                owner_actor_id=actor_id,
                application_name="schemabridge-control-api",
            )
        )
        if self.execution_target is None:
            return inspector
        return _ExecutionTargetInspector(inspector, self.execution_target)


class _ForbiddenFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, workspace_id: str, actor_id: str) -> object:
        self.calls.append((workspace_id, actor_id))
        raise AssertionError("protected workflow I/O must not be reached")


class _WorkerOrchestrator:
    def __init__(
        self,
        approval: AgentWorkflowDraft,
        completed: AgentWorkflowDraft,
    ) -> None:
        self.approval = approval
        self.completed = completed
        self.inspect_calls: list[str] = []
        self.decide_calls: list[tuple[str, ExecutionWorkflowDecision]] = []

    def inspect(self, workflow_id: str) -> AgentWorkflowDraft:
        self.inspect_calls.append(workflow_id)
        return self.approval

    def decide_execution(
        self,
        workflow_id: str,
        decision: ExecutionWorkflowDecision,
        *,
        should_continue: Callable[[], bool] | None = None,
    ) -> AgentWorkflowDraft:
        self.decide_calls.append((workflow_id, decision))
        assert should_continue is None or should_continue()
        return self.completed

    def recover_interrupted(
        self,
        workflow_id: str,
        *,
        expected_operation: WorkflowOperation,
    ) -> AgentWorkflowDraft:
        del workflow_id, expected_operation
        raise AssertionError("the test workflow is not interrupted")


class _InlineHeartbeatSupervisor:
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
        return LeaseHeartbeatRunResult(value=operation(), claim=claim)


def _principal(
    workspace_id: str,
    actor_id: str,
    *,
    roles: frozenset[IdentityRole] = frozenset({IdentityRole.ANALYST}),
) -> AuthenticatedPrincipal:
    now = datetime.now(UTC)
    return AuthenticatedPrincipal(
        workspace_id=workspace_id,
        actor_id=actor_id,
        roles=roles,
        authentication_method=AuthenticationMethod.OIDC,
        authenticated_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
    )


def _submitter(
    job_store: IdentityResolvingBackgroundJobApiStore,
    access_store: IdentityResolvingWorkflowAccessStore,
    inspector_factory: object,
) -> SubmitExecutionJob:
    return SubmitExecutionJob(
        job_store=job_store,
        access_store_factory=_Factory(access_store),  # type: ignore[arg-type]
        orchestrator_factory=inspector_factory,  # type: ignore[arg-type]
        authorization=DenyByDefaultAuthorizationPolicy(),
        clock=_Clock(),
    )


def _submit(
    use_case: SubmitExecutionJob,
    principal: AuthenticatedPrincipal,
    *,
    workflow_id: str,
    idempotency_key: str,
):
    return use_case.execute(
        principal,
        workflow_id=workflow_id,
        expected_workflow_revision=7,
        expected_plan_fingerprint=PLAN,
        confirmation=EXECUTION_CONFIRMATION,
        idempotency_key=idempotency_key,
    )


def test_submitted_v1_job_executes_after_rotation_through_verified_lineage(
    lineage_database: _DatabaseUrls,
) -> None:
    urls = lineage_database
    observed_at = datetime.now(UTC) - timedelta(minutes=2)
    namespace = f"m24-lineage-{uuid4().hex}"
    pair = _pair(namespace, "owner", observed_at=observed_at)
    _seed_capacity_policies(
        urls,
        pair.previous.workspace_id,
        pair.current.workspace_id,
    )
    execution_target = _execution_target(
        urls,
        workspace_id=pair.previous.workspace_id,
    )
    workflow_id = f"wf-{uuid4().hex}"
    approval = _approval_draft(
        workflow_id,
        created_at=observed_at,
    )
    PostgresWorkflowDraftStore(
        urls.runtime,
        workspace_id=pair.previous.workspace_id,
        owner_actor_id=pair.previous.actor_id,
    ).save(approval, expected_revision=None)

    rotations = _rotation_store(urls)
    _initialize(rotations, pair, approved_at=observed_at + timedelta(seconds=2))
    api_resolver = PostgresIdentityBindingResolver(
        urls.api,
        application_name="schemabridge-control-api",
    )
    raw_api_jobs = PostgresBackgroundJobStore(
        urls.api,
        application_name="schemabridge-control-api",
    )
    api_jobs = IdentityResolvingBackgroundJobApiStore(raw_api_jobs, api_resolver)
    api_access = IdentityResolvingWorkflowAccessStore(
        PostgresWorkflowAccessStore(
            urls.api,
            application_name="schemabridge-control-api",
        ),
        api_resolver,
    )
    inspector_factory = _InspectorFactory(
        urls.api,
        execution_target=execution_target,
    )
    submit = _submitter(api_jobs, api_access, inspector_factory)
    v1 = _principal(
        pair.previous.workspace_id,
        pair.previous.actor_id,
    )
    pre_rotation = _submit(
        submit,
        v1,
        workflow_id=workflow_id,
        idempotency_key="pre-rotation-idempotency-0001",  # gitleaks:allow -- non-secret key
    )
    assert not pre_rotation.replayed
    assert pre_rotation.job.authorization.workspace_id == pair.previous.workspace_id

    _rotate(
        rotations,
        pair,
        approved_at=observed_at + timedelta(seconds=4),
    )
    with pytest.raises(IdentityRotationStoreError) as historical_principal:
        api_resolver.resolve_authorization_scopes(
            pair.previous.workspace_id,
            pair.previous.actor_id,
        )
    assert historical_principal.value.code is IdentityRotationStoreErrorCode.CROSS_WORKSPACE
    v2 = _principal(
        pair.current.workspace_id,
        pair.current.actor_id,
    )
    forbidden_access = _ForbiddenFactory()
    forbidden_workflow = _ForbiddenFactory()
    replay_submit = SubmitExecutionJob(
        job_store=api_jobs,
        access_store_factory=forbidden_access,  # type: ignore[arg-type]
        orchestrator_factory=forbidden_workflow,  # type: ignore[arg-type]
        authorization=DenyByDefaultAuthorizationPolicy(),
        clock=_Clock(),
    )
    replay = _submit(
        replay_submit,
        v2,
        workflow_id=workflow_id,
        idempotency_key="pre-rotation-idempotency-0001",  # gitleaks:allow -- non-secret key
    )
    assert replay.replayed
    assert replay.job == pre_rotation.job
    assert forbidden_access.calls == []
    assert forbidden_workflow.calls == []

    inspect = InspectExecutionJob(
        api_jobs,
        DenyByDefaultAuthorizationPolicy(),
        _Clock(),
    )
    assert inspect.execute(v2, pre_rotation.job.id) == pre_rotation.job
    workspace_admin = _principal(
        pair.current.workspace_id,
        _opaque("actor", "v2", f"{namespace}:admin"),
        roles=frozenset({IdentityRole.PLATFORM_ADMIN}),
    )
    assert inspect.execute(workspace_admin, pre_rotation.job.id) == pre_rotation.job
    post_rotation_cancel = _submit(
        submit,
        v2,
        workflow_id=workflow_id,
        idempotency_key="post-rotation-cancel-0001",
    )
    assert post_rotation_cancel.job.authorization.workspace_id == pair.current.workspace_id
    assert (
        post_rotation_cancel.job.authorization.workflow_workspace_id == pair.previous.workspace_id
    )
    assert post_rotation_cancel.job.authorization.workflow_owner_actor_id == pair.previous.actor_id
    assert inspector_factory.calls[-1] == (
        pair.previous.workspace_id,
        pair.previous.actor_id,
    )
    cancel = CancelExecutionJob(
        api_jobs,
        DenyByDefaultAuthorizationPolicy(),
        _Clock(),
    )
    cancelled = cancel.execute(v2, post_rotation_cancel.job.id)
    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.authorization.workspace_id == pair.current.workspace_id

    worker_resolver = PostgresIdentityBindingResolver(
        urls.worker,
        application_name="schemabridge-control-worker",
    )
    worker_access = IdentityResolvingWorkflowAccessStore(
        PostgresWorkflowAccessStore(
            urls.worker,
            application_name="schemabridge-control-worker",
        ),
        worker_resolver,
        persisted_job_scope_resolver=worker_resolver,
        persisted_job_actor_id=pair.previous.actor_id,
    )
    completed = _completed_draft(
        approval,
        submitting_actor_id=pair.previous.actor_id,
        completed_at=datetime.now(UTC),
    )
    worker_orchestrator = _WorkerOrchestrator(approval, completed)
    worker_orchestrator_factory = _Factory(worker_orchestrator)
    worker = RunOneJobWorker(
        job_store=PostgresBackgroundJobStore(
            urls.worker,
            application_name="schemabridge-control-worker",
        ),
        access_store_factory=_Factory(worker_access),  # type: ignore[arg-type]
        orchestrator_factory=worker_orchestrator_factory,  # type: ignore[arg-type]
        clock=_Clock(),
        capability_factory=lambda: "lineage-worker-capability-" + ("x" * 48),
        heartbeat_supervisor=_InlineHeartbeatSupervisor(),  # type: ignore[arg-type]
        worker_id="worker-lineage-integration",
        lease_duration=timedelta(seconds=30),
        heartbeat_interval=timedelta(seconds=10),
    )

    result = worker.execute()

    assert result.outcome is WorkerIterationOutcome.SUCCEEDED
    assert result.job_id == pre_rotation.job.id
    assert worker_orchestrator_factory.calls == [
        (pair.previous.workspace_id, pair.previous.actor_id)
    ]
    assert len(worker_orchestrator_factory.route_contexts) == 1
    assert (
        worker_orchestrator_factory.route_contexts[0].connector_workspace_id
        == pair.previous.workspace_id
    )
    assert len(worker_orchestrator.decide_calls) == 1
    persisted = inspect.execute(v2, pre_rotation.job.id)
    assert persisted.status is JobStatus.SUCCEEDED
    assert persisted.result is not None
    assert persisted.result.row_count == 1
    assert not hasattr(api_resolver, "complete_rotation")
    assert not hasattr(worker_resolver, "audit_signing_keys")

    with psycopg.connect(urls.migrator) as connection:
        rows = connection.execute(
            """
            SELECT workspace_id, workflow_workspace_id,
                   workflow_owner_actor_id, submitting_actor_id
            FROM schemabridge_control.execution_jobs
            WHERE job_id IN (%s, %s)
            ORDER BY job_id
            """,
            (pre_rotation.job.id, post_rotation_cancel.job.id),
        ).fetchall()
    assert len(rows) == 2
    assert {(str(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in rows} == {
        (
            pair.previous.workspace_id,
            pair.previous.workspace_id,
            pair.previous.actor_id,
            pair.previous.actor_id,
        ),
        (
            pair.current.workspace_id,
            pair.previous.workspace_id,
            pair.previous.actor_id,
            pair.current.actor_id,
        ),
    }


def test_platform_admin_v1_workspace_job_executes_then_quarantine_fails_closed(
    lineage_database: _DatabaseUrls,
) -> None:
    urls = lineage_database
    observed_at = datetime.now(UTC) - timedelta(minutes=2)
    namespace = f"m24-workspace-job-{uuid4().hex}"
    owner = _pair(namespace, "owner", observed_at=observed_at)
    admin = _pair(namespace, "admin", observed_at=observed_at)
    _seed_capacity_policies(
        urls,
        owner.previous.workspace_id,
        owner.current.workspace_id,
        admin.previous.workspace_id,
        admin.current.workspace_id,
    )
    execution_target = _execution_target(
        urls,
        workspace_id=owner.previous.workspace_id,
    )
    workflow_id = f"wf-{uuid4().hex}"
    approval = _approval_draft(workflow_id, created_at=observed_at)
    PostgresWorkflowDraftStore(
        urls.runtime,
        workspace_id=owner.previous.workspace_id,
        owner_actor_id=owner.previous.actor_id,
    ).save(approval, expected_revision=None)
    PostgresWorkflowDraftStore(
        urls.runtime,
        workspace_id=admin.previous.workspace_id,
        owner_actor_id=admin.previous.actor_id,
    ).save(
        _approval_draft(
            f"wf-{uuid4().hex}",
            created_at=observed_at,
        ),
        expected_revision=None,
    )

    rotations = _rotation_store(urls)
    _initialize_pairs(
        rotations,
        (owner, admin),
        approved_at=observed_at + timedelta(seconds=2),
    )
    api_resolver = PostgresIdentityBindingResolver(
        urls.api,
        application_name="schemabridge-control-api",
    )
    api_jobs = IdentityResolvingBackgroundJobApiStore(
        PostgresBackgroundJobStore(
            urls.api,
            application_name="schemabridge-control-api",
        ),
        api_resolver,
    )
    api_access = IdentityResolvingWorkflowAccessStore(
        PostgresWorkflowAccessStore(
            urls.api,
            application_name="schemabridge-control-api",
        ),
        api_resolver,
    )
    submit = _submitter(
        api_jobs,
        api_access,
        _InspectorFactory(urls.api, execution_target=execution_target),
    )
    v1_admin = _principal(
        admin.previous.workspace_id,
        admin.previous.actor_id,
        roles=frozenset({IdentityRole.PLATFORM_ADMIN}),
    )
    executable = _submit(
        submit,
        v1_admin,
        workflow_id=workflow_id,
        idempotency_key="workspace-scope-executable-0001",
    )
    quarantined_candidate = _submit(
        submit,
        v1_admin,
        workflow_id=workflow_id,
        idempotency_key="workspace-scope-quarantined-0001",
    )
    assert executable.job.authorization.workflow_access_scope is JobWorkflowAccessScope.WORKSPACE
    assert executable.job.authorization.submitting_actor_id == admin.previous.actor_id

    _rotate_pairs(
        rotations,
        (owner, admin),
        approved_at=observed_at + timedelta(seconds=4),
    )
    worker_resolver = PostgresIdentityBindingResolver(
        urls.worker,
        application_name="schemabridge-control-worker",
    )
    worker_access = IdentityResolvingWorkflowAccessStore(
        PostgresWorkflowAccessStore(
            urls.worker,
            application_name="schemabridge-control-worker",
        ),
        worker_resolver,
        persisted_job_scope_resolver=worker_resolver,
        persisted_job_actor_id=admin.previous.actor_id,
    )
    completed = _completed_draft(
        approval,
        submitting_actor_id=admin.previous.actor_id,
        completed_at=datetime.now(UTC),
    )
    worker_orchestrator = _WorkerOrchestrator(approval, completed)
    worker_orchestrator_factory = _Factory(worker_orchestrator)
    worker = RunOneJobWorker(
        job_store=PostgresBackgroundJobStore(
            urls.worker,
            application_name="schemabridge-control-worker",
        ),
        access_store_factory=_Factory(worker_access),  # type: ignore[arg-type]
        orchestrator_factory=worker_orchestrator_factory,  # type: ignore[arg-type]
        clock=_Clock(),
        capability_factory=lambda: "workspace-job-capability-" + ("x" * 48),
        heartbeat_supervisor=_InlineHeartbeatSupervisor(),  # type: ignore[arg-type]
        worker_id="worker-workspace-lineage",
        lease_duration=timedelta(seconds=30),
        heartbeat_interval=timedelta(seconds=10),
    )

    succeeded = worker.execute()

    assert succeeded.outcome is WorkerIterationOutcome.SUCCEEDED
    assert succeeded.job_id == executable.job.id
    assert worker_orchestrator_factory.calls == [
        (owner.previous.workspace_id, owner.previous.actor_id)
    ]
    assert len(worker_orchestrator_factory.route_contexts) == 1
    assert (
        worker_orchestrator_factory.route_contexts[0].connector_workspace_id
        == owner.previous.workspace_id
    )

    with psycopg.connect(urls.migrator) as connection:
        changed = connection.execute(
            """
            UPDATE schemabridge_control.identity_bindings
            SET status = 'quarantined', updated_at = %s
            WHERE workspace_id = %s
              AND binding_kind = 'actor'
              AND opaque_id = %s
              AND status = 'previous'
            """,
            (
                datetime.now(UTC),
                admin.previous.workspace_id,
                admin.previous.actor_id,
            ),
        )
    assert changed.rowcount == 1

    with pytest.raises(WorkflowAccessError) as revoked_access:
        worker_access.load(
            admin.previous.workspace_id,
            workflow_id,
            owner_principal_id=None,
        )
    assert revoked_access.value.code is WorkflowAccessErrorCode.IDENTITY_MISMATCH

    rejected = worker.execute()

    assert rejected.job_id == quarantined_candidate.job.id
    assert rejected.outcome is WorkerIterationOutcome.FAILED
    assert rejected.failure_code is JobFailureCode.AUTHORIZATION_MISMATCH
    assert worker_orchestrator_factory.calls == [
        (owner.previous.workspace_id, owner.previous.actor_id)
    ]


def test_persisted_scope_rejects_arbitrary_mixed_and_quarantined_ids(
    lineage_database: _DatabaseUrls,
) -> None:
    urls = lineage_database
    observed_at = datetime.now(UTC) - timedelta(minutes=2)
    namespace = f"m24-persisted-scope-{uuid4().hex}"
    pair = _pair(namespace, "owner", observed_at=observed_at)
    other = _pair(f"{namespace}-other", "owner", observed_at=observed_at)
    rotations = _rotation_store(urls)
    _initialize(rotations, pair, approved_at=observed_at + timedelta(seconds=2))
    _initialize(rotations, other, approved_at=observed_at + timedelta(seconds=3))
    _rotate(rotations, pair, approved_at=observed_at + timedelta(seconds=4))
    _rotate(rotations, other, approved_at=observed_at + timedelta(seconds=6))
    resolver = PostgresIdentityBindingResolver(
        urls.worker,
        application_name="schemabridge-control-worker",
    )

    scopes = resolver.resolve_persisted_authorization_scopes(
        pair.previous.workspace_id,
        pair.previous.actor_id,
    )
    assert tuple((scope.workspace_id, scope.actor_id, scope.key_version) for scope in scopes) == (
        (
            pair.previous.workspace_id,
            pair.previous.actor_id,
            "v1",
        ),
        (
            pair.current.workspace_id,
            pair.current.actor_id,
            "v2",
        ),
    )

    invalid_pairs = (
        (
            pair.previous.workspace_id,
            _opaque("actor", "v1", f"{namespace}:arbitrary"),
        ),
        (pair.previous.workspace_id, pair.current.actor_id),
        (pair.previous.workspace_id, other.previous.actor_id),
    )
    for workspace_id, actor_id in invalid_pairs:
        with pytest.raises(IdentityRotationStoreError) as rejected:
            resolver.resolve_persisted_authorization_scopes(workspace_id, actor_id)
        assert rejected.value.code is IdentityRotationStoreErrorCode.CROSS_WORKSPACE

    with psycopg.connect(urls.migrator) as connection:
        connection.execute(
            """
            UPDATE schemabridge_control.identity_bindings
            SET status = 'quarantined', updated_at = %s
            WHERE workspace_id = %s
              AND binding_kind = 'actor'
              AND opaque_id = %s
              AND status = 'previous'
            """,
            (
                datetime.now(UTC),
                pair.previous.workspace_id,
                pair.previous.actor_id,
            ),
        )

    with pytest.raises(IdentityRotationStoreError) as quarantined:
        resolver.resolve_persisted_authorization_scopes(
            pair.previous.workspace_id,
            pair.previous.actor_id,
        )
    assert quarantined.value.code is IdentityRotationStoreErrorCode.CROSS_WORKSPACE


@pytest.mark.parametrize("failure_kind", ["uninitialized", "cross_lineage", "ambiguous"])
def test_invalid_identity_lineage_fails_before_workflow_io(
    lineage_database: _DatabaseUrls,
    failure_kind: str,
) -> None:
    urls = lineage_database
    observed_at = datetime.now(UTC) - timedelta(minutes=2)
    namespace = f"m24-invalid-{failure_kind}-{uuid4().hex}"
    pair = _pair(namespace, "owner", observed_at=observed_at)
    workflow_id = f"wf-{uuid4().hex}"
    approval = _approval_draft(workflow_id, created_at=observed_at)
    PostgresWorkflowDraftStore(
        urls.runtime,
        workspace_id=pair.previous.workspace_id,
        owner_actor_id=pair.previous.actor_id,
    ).save(approval, expected_revision=None)

    rotations = _rotation_store(urls)
    if failure_kind != "uninitialized":
        _initialize(rotations, pair, approved_at=observed_at + timedelta(seconds=2))

    workspace_id = pair.previous.workspace_id
    actor_id = pair.previous.actor_id
    if failure_kind == "cross_lineage":
        other_namespace = f"{namespace}-other"
        other = _pair(other_namespace, "owner", observed_at=observed_at)
        other_workflow = f"wf-{uuid4().hex}"
        PostgresWorkflowDraftStore(
            urls.runtime,
            workspace_id=other.previous.workspace_id,
            owner_actor_id=other.previous.actor_id,
        ).save(
            _approval_draft(other_workflow, created_at=observed_at),
            expected_revision=None,
        )
        _initialize(
            rotations,
            other,
            approved_at=observed_at + timedelta(seconds=3),
        )
        actor_id = other.previous.actor_id
    elif failure_kind == "ambiguous":
        with psycopg.connect(urls.migrator) as connection:
            connection.execute(
                """
                INSERT INTO schemabridge_control.identity_bindings (
                    binding_id, workspace_id, binding_kind,
                    stable_reference_digest, opaque_id, key_version,
                    provenance_version, policy_version,
                    provenance_fingerprint, status, created_at, updated_at
                ) VALUES (
                    %s, %s, 'workspace', %s, %s, 'v1',
                    1, 1, %s, 'active', %s, %s
                )
                """,
                (
                    f"binding-{uuid4().hex}",
                    f"anchor-{uuid4().hex}",
                    _digest(f"{namespace}:ambiguous-reference"),
                    workspace_id,
                    _digest(f"{namespace}:ambiguous-provenance"),
                    observed_at,
                    observed_at,
                ),
            )

    resolver = PostgresIdentityBindingResolver(
        urls.api,
        application_name="schemabridge-control-api",
    )
    jobs = IdentityResolvingBackgroundJobApiStore(
        PostgresBackgroundJobStore(
            urls.api,
            application_name="schemabridge-control-api",
        ),
        resolver,
    )
    forbidden_access = _ForbiddenFactory()
    forbidden_workflow = _ForbiddenFactory()
    submit = SubmitExecutionJob(
        job_store=jobs,
        access_store_factory=forbidden_access,  # type: ignore[arg-type]
        orchestrator_factory=forbidden_workflow,  # type: ignore[arg-type]
        authorization=DenyByDefaultAuthorizationPolicy(),
        clock=_Clock(),
    )

    with pytest.raises(ExecutionJobUseCaseError) as failed:
        _submit(
            submit,
            _principal(workspace_id, actor_id),
            workflow_id=workflow_id,
            idempotency_key=f"invalid-lineage-{failure_kind}-0001",
        )

    assert failed.value.code is ExecutionJobUseCaseErrorCode.SERVICE_UNAVAILABLE
    assert forbidden_access.calls == []
    assert forbidden_workflow.calls == []
