"""Real PostgreSQL proof for M24 queue, leases, fencing, and role isolation."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb
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
from schemabridge.adapters.control_plane.postgres_jobs import (
    PostgresBackgroundJobStore,
)
from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.adapters.control_plane.threaded_heartbeat import (
    ThreadedLeaseHeartbeatSupervisor,
)
from schemabridge.adapters.storage.postgres import PostgresWorkflowAccessStore
from schemabridge.application.job_worker import (
    RunOneJobWorker,
    WorkerExecutionRouteContext,
    WorkerIterationOutcome,
)
from schemabridge.application.ports.background_jobs import (
    JobStoreError,
    JobStoreErrorCode,
)
from schemabridge.application.ports.control_plane_migrations import (
    ControlPlaneMigrationError,
    ControlPlaneMigrationErrorCode,
)
from schemabridge.domain.background_jobs import (
    BackgroundJob,
    JobAuthorization,
    JobExecutionTargetRef,
    JobFailureCode,
    JobRejectionCount,
    JobResultSummary,
    JobStatus,
    job_retry_delay,
)
from schemabridge.domain.catalog_inventory import (
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogConnectionRegistration,
)
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.workflows import (
    AgentWorkflowDraft,
    ExecutionWorkflowDecision,
    RetryWorkflowDecision,
    WorkflowCheckpoint,
    WorkflowCheckpointKind,
    WorkflowDecisionAction,
    WorkflowDecisionKind,
    WorkflowDecisionRecord,
    WorkflowFailure,
    WorkflowOperation,
    WorkflowStage,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations/control_plane"
ADMIN_DSN = "postgresql://postgres:local-only-not-a-secret@127.0.0.1:55434/postgres"


@dataclass(frozen=True)
class _DatabaseUrls:
    database: str
    migrator: str
    api: str
    worker: str
    runtime: str
    reconciler: str
    catalog: str
    observer: str
    backup: str


@pytest.fixture
def job_database() -> Iterator[_DatabaseUrls]:
    database = f"schemabridge_jobs_{uuid4().hex[:16]}"
    admin_dsn = os.environ.get("SCHEMABRIDGE_TEST_CONTROL_ADMIN_DATABASE_URL", ADMIN_DSN)
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
                    schemabridge_reconciler,
                    schemabridge_api,
                    schemabridge_worker,
                    schemabridge_catalog,
                    schemabridge_observer,
                    schemabridge_backup
                """
            ).format(sql.Identifier(database))
        )

    urls = _DatabaseUrls(
        database=database,
        migrator=_role_dsn("schemabridge_migrator", database),
        api=_role_dsn("schemabridge_api", database),
        worker=_role_dsn("schemabridge_worker", database),
        runtime=_role_dsn("schemabridge_runtime", database),
        reconciler=_role_dsn("schemabridge_reconciler", database),
        catalog=_role_dsn("schemabridge_catalog", database),
        observer=_role_dsn("schemabridge_observer", database),
        backup=_role_dsn("schemabridge_backup", database),
    )
    try:
        with tempfile.TemporaryDirectory(prefix="schemabridge-m24-v1-") as directory:
            v1_directory = Path(directory)
            shutil.copyfile(
                MIGRATIONS / "0001_initial_control_plane.sql",
                v1_directory / "0001_initial_control_plane.sql",
            )
            first = PostgresControlPlaneMigrator(
                urls.migrator,
                v1_directory,
            ).migrate()
            assert first.inspection.current_version == 1
        with pytest.raises(ControlPlaneMigrationError) as stale_schema:
            PostgresControlPlaneMigrator(urls.migrator, MIGRATIONS).require_current()
        assert stale_schema.value.code is ControlPlaneMigrationErrorCode.SCHEMA_NOT_CURRENT
        upgraded = PostgresControlPlaneMigrator(urls.migrator, MIGRATIONS).migrate()
        assert upgraded.applied_versions == (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13)
        assert upgraded.inspection.current_version == 13
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


def _seed_workflow(
    urls: _DatabaseUrls,
    *,
    workspace_id: str,
    workflow_id: str,
    owner_actor_id: str,
) -> None:
    now = datetime.now(UTC)
    with psycopg.connect(urls.migrator) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.tenant_capacity_policies (
                workspace_id, connection_limit, asset_limit, field_limit,
                api_requests_per_minute, nonterminal_job_limit, version,
                updated_by, created_at, updated_at
            ) VALUES (%s, 100, 1000000, 10000000, 10000, 1000, 1,
                      'test_platform_admin', %s, %s)
            ON CONFLICT (workspace_id) DO NOTHING
            """,
            (workspace_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.agent_workflow_drafts (
                workspace_id,
                id,
                revision,
                payload,
                execution_row_count,
                execution_preview_fingerprint,
                updated_at
            ) VALUES (%s, %s, 1, %s, NULL, NULL, %s)
            """,
            (workspace_id, workflow_id, Jsonb({"synthetic": True}), now),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.workflow_access_grants (
                workspace_id,
                workflow_id,
                owner_actor_id,
                created_at
            ) VALUES (%s, %s, %s, %s)
            """,
            (workspace_id, workflow_id, owner_actor_id, now),
        )


@cache
def _execution_target(
    urls: _DatabaseUrls,
    *,
    workspace_id: str,
) -> JobExecutionTargetRef:
    connection_id = CatalogConnectionId("connection_background_jobs")
    PostgresCatalogConnectionStore(
        urls.api,
        application_name="schemabridge-control-api",
    ).register(
        CatalogConnectionRegistration(
            workspace_id=workspace_id,
            connection_id=connection_id,
            display_name="Background job test source",
            kind=CatalogConnectionKind.SYNTHETIC,
            environment="TEST",
            catalog_scope="background-jobs",
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
    return JobExecutionTargetRef.from_target(target)


def _job(
    urls: _DatabaseUrls,
    *,
    name: str,
    workspace_name: str | None = None,
    max_attempts: int = 3,
    expires_in: timedelta = timedelta(minutes=10),
    plan_fingerprint: str | None = None,
    idempotency_digest: str | None = None,
    owner_submission: bool = False,
) -> tuple[BackgroundJob, PostgresBackgroundJobStore, PostgresBackgroundJobStore]:
    workspace_id = f"workspace-{workspace_name or name}"
    workflow_id = f"workflow-{name}"
    owner = f"owner-{name}"
    submitter = owner if owner_submission else f"submitter-{name}"
    _seed_workflow(
        urls,
        workspace_id=workspace_id,
        workflow_id=workflow_id,
        owner_actor_id=owner,
    )
    execution_target = _execution_target(urls, workspace_id=workspace_id)
    now = datetime.now(UTC)
    authorization = JobAuthorization.create(
        workspace_id=workspace_id,
        workflow_id=workflow_id,
        workflow_owner_actor_id=owner,
        submitting_actor_id=submitter,
        expected_workflow_revision=1,
        expected_plan_fingerprint=plan_fingerprint or _digest(f"plan-{name}"),
        execution_target=execution_target,
        authenticated_at=now - timedelta(minutes=1),
        authorized_at=now - timedelta(seconds=1),
        expires_at=now + expires_in,
    )
    job = BackgroundJob.create(
        id=f"job-{name}",
        authorization=authorization,
        idempotency_digest=idempotency_digest or _digest(f"idempotency-{name}"),
        max_attempts=max_attempts,
        created_at=now,
    )
    return (
        job,
        PostgresBackgroundJobStore(urls.api),
        PostgresBackgroundJobStore(urls.worker),
    )


def _result(job: BackgroundJob) -> JobResultSummary:
    return JobResultSummary(
        workflow_id=job.authorization.workflow_id,
        workflow_revision=job.authorization.expected_workflow_revision + 1,
        stage=WorkflowStage.PUBLICATION_PROPOSED,
        row_count=3,
        preview_fingerprint=_digest(f"preview-{job.id}"),
        rejected_count=2,
        rejection_code_counts=(
            JobRejectionCount(code="empty_identifier", count=1),
            JobRejectionCount(code="unsafe_float", count=1),
        ),
        truncated=False,
        completed_at=datetime.now(UTC),
    )


def _execution_approval(job: BackgroundJob) -> AgentWorkflowDraft:
    return AgentWorkflowDraft(
        id=job.authorization.workflow_id,
        revision=job.authorization.expected_workflow_revision,
        stage=WorkflowStage.DECISION_REQUIRED,
        text="Agrupa clientes por fecha.",
        language="es",
        requested_datasets=(PhysicalDatasetRef("crm.customers"),),
        plan_fingerprint=job.authorization.expected_plan_fingerprint,
        checkpoint=WorkflowCheckpoint(
            kind=WorkflowCheckpointKind.EXECUTION_APPROVAL,
            fingerprint=job.authorization.expected_plan_fingerprint,
            reason="Exact execution approval is required.",
        ),
        created_at=job.created_at,
        updated_at=job.created_at,
    )


def _transient_workflow_failure(
    approval: AgentWorkflowDraft,
    *,
    actor: str,
    attempt: int,
) -> AgentWorkflowDraft:
    approved_at = approval.updated_at + timedelta(milliseconds=1)
    failure = WorkflowFailure.create(
        code="source_unavailable",
        operation=WorkflowOperation.PREVIEW_EXECUTION,
        retryable=True,
        attempt=attempt,
        occurred_at=approved_at + timedelta(seconds=attempt),
    )
    return approval.model_copy(
        update={
            "revision": approval.revision + (attempt * 2),
            "stage": WorkflowStage.FAILED,
            "checkpoint": None,
            "failure": failure,
            "decisions": (
                WorkflowDecisionRecord(
                    actor=actor,
                    kind=WorkflowDecisionKind.EXECUTION,
                    action=WorkflowDecisionAction.APPROVE,
                    decided_at=approved_at,
                    bound_fingerprint=approval.plan_fingerprint,
                ),
            ),
            "updated_at": failure.occurred_at,
        }
    )


class _DatabaseClock:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def now(self) -> datetime:
        with psycopg.connect(self.dsn) as connection:
            row = connection.execute("SELECT clock_timestamp()").fetchone()
        assert row is not None
        value = row[0]
        assert isinstance(value, datetime)
        return value


class _TransientRetryOrchestrator:
    """Typed fake workflow whose protected operation fails twice transiently."""

    def __init__(
        self,
        approval: AgentWorkflowDraft,
        first_failure: AgentWorkflowDraft,
        second_failure: AgentWorkflowDraft,
    ) -> None:
        self.current = approval
        self.first_failure = first_failure
        self.second_failure = second_failure
        self.inspect_calls: list[str] = []
        self.decide_calls: list[tuple[str, ExecutionWorkflowDecision]] = []
        self.retry_calls: list[
            tuple[
                str,
                RetryWorkflowDecision,
                ExecutionWorkflowDecision | None,
            ]
        ] = []
        self.protected_source_operations: list[str] = []

    def inspect(self, workflow_id: str) -> AgentWorkflowDraft:
        self.inspect_calls.append(workflow_id)
        return self.current

    def decide_execution(
        self,
        workflow_id: str,
        decision: ExecutionWorkflowDecision,
        *,
        should_continue: Callable[[], bool] | None = None,
    ) -> AgentWorkflowDraft:
        self.decide_calls.append((workflow_id, decision))
        assert should_continue is not None and should_continue()
        self.protected_source_operations.append("decide_execution")
        self.current = self.first_failure
        return self.current

    def retry(
        self,
        workflow_id: str,
        decision: RetryWorkflowDecision,
        *,
        reserved_execution: ExecutionWorkflowDecision | None = None,
        should_continue: Callable[[], bool] | None = None,
    ) -> AgentWorkflowDraft:
        self.retry_calls.append((workflow_id, decision, reserved_execution))
        assert self.current.failure is not None
        assert decision.failure_fingerprint == self.current.failure.fingerprint
        assert decision.operation is self.current.failure.operation
        assert reserved_execution is not None
        assert should_continue is not None and should_continue()
        self.protected_source_operations.append("retry")
        self.current = self.second_failure
        return self.current

    def recover_interrupted(
        self,
        workflow_id: str,
        *,
        expected_operation: WorkflowOperation,
    ) -> AgentWorkflowDraft:
        del workflow_id, expected_operation
        raise AssertionError("the transient test workflow is not interrupted")


def _events(urls: _DatabaseUrls, job: BackgroundJob) -> tuple[str, ...]:
    with psycopg.connect(urls.migrator) as connection:
        rows = connection.execute(
            """
            SELECT event_type
            FROM schemabridge_control.execution_job_events
            WHERE workspace_id = %s AND job_id = %s
            ORDER BY sequence
            """,
            (job.authorization.workspace_id, job.id),
        ).fetchall()
    return tuple(str(row[0]) for row in rows)


def test_submission_is_atomic_scoped_and_exactly_idempotent(
    job_database: _DatabaseUrls,
) -> None:
    job, api, _ = _job(job_database, name="idempotency")
    raw_key = "raw-idempotency-key-must-never-persist"

    first = api.submit(job)
    replay = PostgresBackgroundJobStore(job_database.api).submit(job)

    assert not first.replayed
    assert replay.replayed
    assert replay.job.id == first.job.id
    assert first.job.created_at != job.created_at
    assert (
        api.load(
            job.authorization.workspace_id,
            job.id,
            submitting_actor_id=job.authorization.submitting_actor_id,
        )
        == first.job
    )
    assert (
        api.load(
            job.authorization.workspace_id,
            job.id,
            submitting_actor_id="another-submitter",
        )
        is None
    )
    assert api.load("another-workspace", job.id) is None
    assert _events(job_database, job) == ("submitted",)

    changed_authorization = JobAuthorization.create(
        workspace_id=job.authorization.workspace_id,
        workflow_id=job.authorization.workflow_id,
        workflow_owner_actor_id=job.authorization.workflow_owner_actor_id,
        submitting_actor_id=job.authorization.submitting_actor_id,
        expected_workflow_revision=1,
        expected_plan_fingerprint=_digest("changed-plan"),
        execution_target=job.authorization.execution_target,
        authenticated_at=job.authorization.authenticated_at,
        authorized_at=job.authorization.authorized_at,
        expires_at=job.authorization.expires_at,
    )
    changed = BackgroundJob.create(
        id="job-idempotency-collision",
        authorization=changed_authorization,
        idempotency_digest=job.idempotency_digest,
        max_attempts=3,
        created_at=datetime.now(UTC),
    )
    with pytest.raises(JobStoreError) as raised:
        api.submit(changed)
    assert raised.value.code is JobStoreErrorCode.IDEMPOTENCY_CONFLICT
    assert _events(job_database, job) == ("submitted",)

    with psycopg.connect(job_database.migrator) as connection:
        stored = connection.execute(
            """
            SELECT row_to_json(job)::text
            FROM schemabridge_control.execution_jobs AS job
            WHERE job_id = %s
            """,
            (job.id,),
        ).fetchone()
        columns = connection.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'schemabridge_control'
              AND table_name IN ('execution_jobs', 'execution_job_events')
            ORDER BY column_name
            """
        ).fetchall()
    assert stored is not None
    durable_text = str(stored[0])
    assert raw_key not in durable_text
    assert "SELECT " not in durable_text
    assert all(
        forbidden not in {str(row[0]) for row in columns}
        for forbidden in (
            "token",
            "sql",
            "parameters",
            "prompt",
            "preview_rows",
            "result_rows",
        )
    )


def test_submission_and_initial_event_rollback_together(
    job_database: _DatabaseUrls,
) -> None:
    job, api, _ = _job(job_database, name="atomic-submit")
    with psycopg.connect(job_database.migrator) as connection:
        connection.execute(
            """
            REVOKE INSERT (
                event_id,
                workspace_id,
                job_id,
                event_type,
                status,
                attempt_count,
                fencing_token,
                reason_code,
                occurred_at
            ) ON schemabridge_control.execution_job_events
            FROM schemabridge_api
            """
        )

    with pytest.raises(JobStoreError) as raised:
        api.submit(job)
    assert raised.value.code is JobStoreErrorCode.STORE_UNAVAILABLE

    with psycopg.connect(job_database.migrator) as connection:
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM schemabridge_control.execution_jobs),
                (SELECT count(*) FROM schemabridge_control.execution_job_events)
            """
        ).fetchone()
    assert counts == (0, 0)


def test_concurrent_exact_submission_creates_one_job_and_one_event(
    job_database: _DatabaseUrls,
) -> None:
    job, _, _ = _job(job_database, name="concurrent-submit")
    barrier = Barrier(2)

    def submit() -> tuple[str, bool]:
        barrier.wait(timeout=5)
        result = PostgresBackgroundJobStore(job_database.api).submit(job)
        return result.job.id, result.replayed

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = tuple(executor.submit(submit) for _ in range(2))
        results = tuple(future.result(timeout=10) for future in futures)

    assert {job_id for job_id, _ in results} == {job.id}
    assert sorted(replayed for _, replayed in results) == [False, True]
    assert _events(job_database, job) == ("submitted",)


def test_two_workers_claim_once_heartbeat_and_stale_fencing_fail_closed(
    job_database: _DatabaseUrls,
) -> None:
    job, api, _ = _job(job_database, name="contention")
    submitted = api.submit(job).job
    barrier = Barrier(2)
    token_one = "worker-one-capability-" + ("a" * 48)
    token_two = "worker-two-capability-" + ("b" * 48)

    def claim(worker_id: str, token: str) -> BackgroundJob | None:
        barrier.wait(timeout=5)
        return PostgresBackgroundJobStore(job_database.worker).claim_next(
            worker_id=worker_id,
            lease_token=token,
            lease_duration=timedelta(seconds=2),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(claim, "worker-one", token_one),
            executor.submit(claim, "worker-two", token_two),
        )
        claims = tuple(future.result(timeout=10) for future in futures)

    claimed = next(item for item in claims if item is not None)
    assert sum(item is not None for item in claims) == 1
    assert claimed.id == submitted.id
    assert claimed.lease is not None
    winning_token = token_one if claimed.lease.worker_id == "worker-one" else token_two
    worker = PostgresBackgroundJobStore(job_database.worker)

    heartbeat = worker.heartbeat(
        job.id,
        worker_id=claimed.lease.worker_id,
        lease_token=winning_token,
        fencing_token=claimed.lease.fencing_token,
        lease_duration=timedelta(seconds=2),
    )
    assert heartbeat.lease is not None
    assert heartbeat.lease.expires_at > claimed.lease.expires_at

    with pytest.raises(JobStoreError) as wrong_token:
        worker.heartbeat(
            job.id,
            worker_id=claimed.lease.worker_id,
            lease_token="wrong-capability-" + ("z" * 48),
            fencing_token=claimed.lease.fencing_token,
            lease_duration=timedelta(seconds=2),
        )
    assert wrong_token.value.code is JobStoreErrorCode.LEASE_CONFLICT
    with pytest.raises(JobStoreError) as wrong_fence:
        worker.heartbeat(
            job.id,
            worker_id=claimed.lease.worker_id,
            lease_token=winning_token,
            fencing_token=claimed.lease.fencing_token + 1,
            lease_duration=timedelta(seconds=2),
        )
    assert wrong_fence.value.code is JobStoreErrorCode.LEASE_CONFLICT
    assert _events(job_database, job) == ("submitted", "claimed")


def test_periodic_heartbeat_blocks_reclaim_and_observes_cooperative_cancel(
    job_database: _DatabaseUrls,
) -> None:
    job, api, worker = _job(job_database, name="periodic-heartbeat")
    api.submit(job)
    token = "periodic-postgres-capability-" + ("h" * 48)
    claim = worker.claim_next(
        worker_id="worker-periodic",
        lease_token=token,
        lease_duration=timedelta(seconds=1),
    )
    assert claim is not None and claim.lease is not None

    class _CountingHeartbeatStore:
        def __init__(self, inner: PostgresBackgroundJobStore) -> None:
            self.inner = inner
            self.calls = 0

        def heartbeat(
            self,
            job_id: str,
            *,
            worker_id: str,
            lease_token: str,
            fencing_token: int,
            lease_duration: timedelta,
        ) -> BackgroundJob:
            self.calls += 1
            return self.inner.heartbeat(
                job_id,
                worker_id=worker_id,
                lease_token=lease_token,
                fencing_token=fencing_token,
                lease_duration=lease_duration,
            )

    heartbeats = _CountingHeartbeatStore(worker)
    contender = PostgresBackgroundJobStore(job_database.worker)
    contender_claims: list[BackgroundJob | None] = []
    cancellation_requested = False

    def blocked_operation() -> str:
        nonlocal cancellation_requested
        deadline = time.monotonic() + 2.2
        while time.monotonic() < deadline:
            time.sleep(0.25)
            if not cancellation_requested and time.monotonic() > deadline - 1.3:
                cancelled = api.request_cancellation(
                    job.authorization.workspace_id,
                    job.id,
                    submitting_actor_id=job.authorization.submitting_actor_id,
                    requested_at=datetime(2000, 1, 1, tzinfo=UTC),
                )
                assert cancelled is not None
                assert cancelled.status is JobStatus.CANCEL_REQUESTED
                cancellation_requested = True
            contender_claims.append(
                contender.claim_next(
                    worker_id="worker-contender",
                    lease_token="contender-capability-" + ("c" * 48),
                    lease_duration=timedelta(seconds=1),
                )
            )
        return "completed"

    supervised = ThreadedLeaseHeartbeatSupervisor(  # type: ignore[arg-type]
        heartbeats,
        join_timeout=timedelta(seconds=2),
    ).run(
        claim=claim,
        worker_id="worker-periodic",
        lease_token=token,
        lease_duration=timedelta(seconds=1),
        heartbeat_interval=timedelta(milliseconds=200),
        operation=blocked_operation,
    )

    assert supervised.value == "completed"
    assert supervised.claim.status is JobStatus.CANCEL_REQUESTED
    assert heartbeats.calls >= 9
    assert contender_claims
    assert all(item is None for item in contender_claims)
    succeeded = worker.succeed(
        job.id,
        worker_id="worker-periodic",
        lease_token=token,
        fencing_token=claim.lease.fencing_token,
        result=_result(job),
    )
    assert succeeded.status is JobStatus.SUCCEEDED
    assert _events(job_database, job) == (
        "submitted",
        "claimed",
        "cancel_requested",
        "succeeded",
    )


def test_queued_and_cooperative_cancellation_are_distinct(
    job_database: _DatabaseUrls,
) -> None:
    queued, api, worker = _job(job_database, name="cancel-queued")
    api.submit(queued)
    cancelled = api.request_cancellation(
        queued.authorization.workspace_id,
        queued.id,
        submitting_actor_id=queued.authorization.submitting_actor_id,
        requested_at=datetime(2000, 1, 1, tzinfo=UTC),
    )
    assert cancelled is not None
    assert cancelled.status is JobStatus.CANCELLED
    assert (
        worker.claim_next(
            worker_id="worker-cancel-queued",
            lease_token="queued-cancel-capability-" + ("q" * 48),
            lease_duration=timedelta(seconds=2),
        )
        is None
    )
    assert _events(job_database, queued) == ("submitted", "cancelled")

    leased, leased_api, leased_worker = _job(job_database, name="cancel-leased")
    leased_api.submit(leased)
    token = "leased-cancel-capability-" + ("l" * 48)
    claim = leased_worker.claim_next(
        worker_id="worker-cancel-leased",
        lease_token=token,
        lease_duration=timedelta(seconds=2),
    )
    assert claim is not None and claim.lease is not None
    requested = leased_api.request_cancellation(
        leased.authorization.workspace_id,
        leased.id,
        submitting_actor_id=leased.authorization.submitting_actor_id,
        requested_at=datetime(2000, 1, 1, tzinfo=UTC),
    )
    assert requested is not None
    assert requested.status is JobStatus.CANCEL_REQUESTED
    acknowledged = leased_worker.acknowledge_cancellation(
        leased.id,
        worker_id=claim.lease.worker_id,
        lease_token=token,
        fencing_token=claim.lease.fencing_token,
        cancelled_at=datetime(2000, 1, 1, tzinfo=UTC),
    )
    assert acknowledged.status is JobStatus.CANCELLED
    assert _events(job_database, leased) == (
        "submitted",
        "claimed",
        "cancel_requested",
        "cancelled",
    )


def test_crashed_lease_reclaims_with_new_fence_and_old_worker_cannot_finish(
    job_database: _DatabaseUrls,
) -> None:
    job, api, worker = _job(job_database, name="reclaim")
    api.submit(job)
    first_token = "first-reclaim-capability-" + ("f" * 48)
    first = worker.claim_next(
        worker_id="worker-first",
        lease_token=first_token,
        lease_duration=timedelta(seconds=1),
    )
    assert first is not None and first.lease is not None
    time.sleep(1.1)
    second_token = "second-reclaim-capability-" + ("s" * 48)
    second = worker.claim_next(
        worker_id="worker-second",
        lease_token=second_token,
        lease_duration=timedelta(seconds=2),
    )
    assert second is not None and second.lease is not None
    assert second.attempt_count == 2
    assert second.lease.fencing_token == first.lease.fencing_token + 1

    with pytest.raises(JobStoreError) as stale:
        worker.succeed(
            job.id,
            worker_id="worker-first",
            lease_token=first_token,
            fencing_token=first.lease.fencing_token,
            result=_result(job),
        )
    assert stale.value.code is JobStoreErrorCode.LEASE_CONFLICT

    succeeded = worker.succeed(
        job.id,
        worker_id="worker-second",
        lease_token=second_token,
        fencing_token=second.lease.fencing_token,
        result=_result(job),
    )
    assert succeeded.status is JobStatus.SUCCEEDED
    assert succeeded.result is not None
    assert succeeded.result.row_count == 3
    assert succeeded.result.rejected_count == 2
    assert _events(job_database, job) == (
        "submitted",
        "claimed",
        "claimed",
        "succeeded",
    )


def test_expired_authorization_is_atomically_failed_and_trigger_rejects_raw_success(
    job_database: _DatabaseUrls,
) -> None:
    job, api, worker = _job(
        job_database,
        name="expires-during-commit",
        expires_in=timedelta(milliseconds=400),
    )
    api.submit(job)
    token = "expiry-commit-capability-" + ("e" * 48)
    claim = worker.claim_next(
        worker_id="worker-expiry-commit",
        lease_token=token,
        lease_duration=timedelta(seconds=2),
    )
    assert claim is not None and claim.lease is not None
    time.sleep(0.5)

    expired = worker.succeed(
        job.id,
        worker_id=claim.lease.worker_id,
        lease_token=token,
        fencing_token=claim.lease.fencing_token,
        result=_result(job),
    )

    assert expired.status is JobStatus.FAILED
    assert expired.result is None
    assert expired.failure is not None
    assert expired.failure.code is JobFailureCode.AUTHORIZATION_EXPIRED
    assert _events(job_database, job) == ("submitted", "claimed", "failed")

    raw_job, raw_api, raw_worker = _job(
        job_database,
        name="raw-expired-success",
        expires_in=timedelta(milliseconds=400),
    )
    raw_api.submit(raw_job)
    raw_token = "raw-expiry-capability-" + ("r" * 48)
    raw_claim = raw_worker.claim_next(
        worker_id="worker-raw-expiry",
        lease_token=raw_token,
        lease_duration=timedelta(seconds=2),
    )
    assert raw_claim is not None
    time.sleep(0.5)

    with (
        psycopg.connect(job_database.worker) as connection,
        pytest.raises(
            psycopg.errors.ObjectNotInPrerequisiteState,
            match="expired execution job authorization cannot succeed",
        ),
        connection.transaction(),
    ):
        connection.execute(
            """
            UPDATE schemabridge_control.execution_jobs
            SET status = 'succeeded'
            WHERE job_id = %s
            """,
            (raw_job.id,),
        )


def test_retry_expiry_and_exhausted_lease_have_bounded_terminal_outcomes(
    job_database: _DatabaseUrls,
) -> None:
    retry_job, retry_api, retry_worker = _job(
        job_database,
        name="retry-dead",
        max_attempts=2,
    )
    retry_api.submit(retry_job)
    first_token = "retry-first-capability-" + ("r" * 48)
    first = retry_worker.claim_next(
        worker_id="worker-retry-first",
        lease_token=first_token,
        lease_duration=timedelta(seconds=30),
    )
    assert first is not None and first.lease is not None
    failed_at = datetime.now(UTC)
    retry_wait = retry_worker.fail(
        retry_job.id,
        worker_id=first.lease.worker_id,
        lease_token=first_token,
        fencing_token=first.lease.fencing_token,
        code=JobFailureCode.SOURCE_UNAVAILABLE,
        failed_at=failed_at,
        retry_at=failed_at + job_retry_delay(first.attempt_count),
    )
    assert retry_wait.status is JobStatus.RETRY_WAIT
    assert (
        retry_worker.claim_next(
            worker_id="worker-too-early",
            lease_token="too-early-capability-" + ("e" * 48),
            lease_duration=timedelta(seconds=2),
        )
        is None
    )
    time.sleep(job_retry_delay(first.attempt_count).total_seconds() + 0.1)
    second_token = "retry-second-capability-" + ("t" * 48)
    second = retry_worker.claim_next(
        worker_id="worker-retry-second",
        lease_token=second_token,
        lease_duration=timedelta(seconds=2),
    )
    assert second is not None and second.lease is not None
    dead = retry_worker.fail(
        retry_job.id,
        worker_id=second.lease.worker_id,
        lease_token=second_token,
        fencing_token=second.lease.fencing_token,
        code=JobFailureCode.SOURCE_UNAVAILABLE,
        failed_at=datetime.now(UTC),
    )
    assert dead.status is JobStatus.DEAD_LETTERED

    expiring, expiring_api, expiring_worker = _job(
        job_database,
        name="expired-auth",
        expires_in=timedelta(seconds=0.5),
    )
    expiring_api.submit(expiring)
    time.sleep(0.6)
    assert expiring_worker.expire_authorizations() == 1
    expired = expiring_api.load(expiring.authorization.workspace_id, expiring.id)
    assert expired is not None
    assert expired.status is JobStatus.FAILED
    assert expired.attempt_count == 0
    assert expired.failure is not None
    assert expired.failure.code is JobFailureCode.AUTHORIZATION_EXPIRED

    exhausted, exhausted_api, exhausted_worker = _job(
        job_database,
        name="exhausted-lease",
        max_attempts=1,
    )
    exhausted_api.submit(exhausted)
    lease = exhausted_worker.claim_next(
        worker_id="worker-exhausted",
        lease_token="exhausted-capability-" + ("x" * 48),
        lease_duration=timedelta(seconds=1),
    )
    assert lease is not None
    time.sleep(1.1)
    assert exhausted_worker.reap_exhausted_leases() == 1
    reaped = exhausted_api.load(exhausted.authorization.workspace_id, exhausted.id)
    assert reaped is not None
    assert reaped.status is JobStatus.DEAD_LETTERED
    assert reaped.failure is not None
    assert reaped.failure.code is JobFailureCode.AMBIGUOUS_EXTERNAL_EFFECT


def test_public_worker_retries_transient_workflow_then_dead_letters_in_postgres(
    job_database: _DatabaseUrls,
) -> None:
    job, api, worker_store = _job(
        job_database,
        name="public-worker-retry",
        max_attempts=2,
        owner_submission=True,
    )
    submitted = api.submit(job).job
    approval = _execution_approval(submitted)
    first_failure = _transient_workflow_failure(
        approval,
        actor=submitted.authorization.submitting_actor_id,
        attempt=1,
    )
    second_failure = _transient_workflow_failure(
        approval,
        actor=submitted.authorization.submitting_actor_id,
        attempt=2,
    )
    orchestrator = _TransientRetryOrchestrator(
        approval,
        first_failure,
        second_failure,
    )
    access_store = PostgresWorkflowAccessStore(
        job_database.worker,
        application_name="schemabridge-control-worker",
    )
    access_factory_calls: list[tuple[str, str]] = []
    orchestrator_factory_calls: list[tuple[str, str]] = []
    route_contexts: list[WorkerExecutionRouteContext] = []

    def access_factory(
        workspace_id: str,
        actor_id: str,
    ) -> PostgresWorkflowAccessStore:
        access_factory_calls.append((workspace_id, actor_id))
        return access_store

    def orchestrator_factory(
        workspace_id: str,
        actor_id: str,
        *,
        route_context: WorkerExecutionRouteContext | None = None,
    ) -> _TransientRetryOrchestrator:
        orchestrator_factory_calls.append((workspace_id, actor_id))
        assert route_context is not None
        assert route_context.execution_target == submitted.authorization.execution_target
        route_contexts.append(route_context)
        return orchestrator

    worker = RunOneJobWorker(
        job_store=worker_store,
        access_store_factory=access_factory,
        orchestrator_factory=orchestrator_factory,
        clock=_DatabaseClock(job_database.worker),
        capability_factory=lambda: f"public-retry-capability-{uuid4().hex}-{uuid4().hex}",
        heartbeat_supervisor=ThreadedLeaseHeartbeatSupervisor(worker_store),
        worker_id="worker-public-retry-integration",
        lease_duration=timedelta(seconds=30),
        heartbeat_interval=timedelta(seconds=10),
    )

    first = worker.execute()

    assert first.outcome is WorkerIterationOutcome.RETRY_SCHEDULED
    assert first.status is JobStatus.RETRY_WAIT
    assert first.failure_code is JobFailureCode.SOURCE_UNAVAILABLE
    retrying = api.load(
        submitted.authorization.workspace_id,
        submitted.id,
        submitting_actor_id=submitted.authorization.submitting_actor_id,
    )
    assert retrying is not None
    assert retrying.status is JobStatus.RETRY_WAIT
    assert retrying.attempt_count == 1
    assert retrying.failure is not None
    assert retrying.failure.code is JobFailureCode.SOURCE_UNAVAILABLE
    assert retrying.available_at is not None
    assert retrying.available_at - retrying.updated_at == job_retry_delay(1)

    too_early = worker.execute()

    assert too_early.outcome is WorkerIterationOutcome.IDLE
    assert orchestrator.inspect_calls == [submitted.authorization.workflow_id]
    assert orchestrator.retry_calls == []
    assert orchestrator.protected_source_operations == ["decide_execution"]

    # Use the persisted database timestamp as the wait boundary instead of a
    # fixed process sleep, so the next claim is demonstrably after available_at.
    with psycopg.connect(job_database.migrator) as connection:
        waited = connection.execute(
            """
            SELECT pg_sleep(
                LEAST(
                    GREATEST(
                        EXTRACT(EPOCH FROM (available_at - clock_timestamp())),
                        0
                    ) + 0.05,
                    6
                )
            )
            FROM schemabridge_control.execution_jobs
            WHERE workspace_id = %s AND job_id = %s
            """,
            (submitted.authorization.workspace_id, submitted.id),
        ).fetchone()
    assert waited is not None

    exhausted = worker.execute()

    assert exhausted.outcome is WorkerIterationOutcome.DEAD_LETTERED
    assert exhausted.status is JobStatus.DEAD_LETTERED
    assert exhausted.failure_code is JobFailureCode.SOURCE_UNAVAILABLE
    dead = api.load(
        submitted.authorization.workspace_id,
        submitted.id,
        submitting_actor_id=submitted.authorization.submitting_actor_id,
    )
    assert dead is not None
    assert dead.status is JobStatus.DEAD_LETTERED
    assert dead.attempt_count == 2
    assert dead.last_fencing_token == 2
    assert dead.available_at is None
    assert dead.failure is not None
    assert dead.failure.code is JobFailureCode.SOURCE_UNAVAILABLE

    terminal_poll = worker.execute()

    assert terminal_poll.outcome is WorkerIterationOutcome.IDLE
    assert orchestrator.inspect_calls == [
        submitted.authorization.workflow_id,
        submitted.authorization.workflow_id,
    ]
    assert len(orchestrator.decide_calls) == 1
    assert len(orchestrator.retry_calls) == 1
    _, retry_decision, reserved_execution = orchestrator.retry_calls[0]
    assert first_failure.failure is not None
    assert retry_decision.failure_fingerprint == first_failure.failure.fingerprint
    assert retry_decision.operation is WorkflowOperation.PREVIEW_EXECUTION
    assert reserved_execution is not None
    assert reserved_execution.actor == submitted.authorization.submitting_actor_id
    assert reserved_execution.plan_fingerprint == (
        submitted.authorization.expected_plan_fingerprint
    )
    assert orchestrator.protected_source_operations == [
        "decide_execution",
        "retry",
    ]
    expected_scope = (
        submitted.authorization.workspace_id,
        submitted.authorization.submitting_actor_id,
    )
    assert access_factory_calls == [expected_scope, expected_scope]
    assert orchestrator_factory_calls == [
        (
            submitted.authorization.workflow_workspace_id,
            submitted.authorization.workflow_owner_actor_id,
        ),
        (
            submitted.authorization.workflow_workspace_id,
            submitted.authorization.workflow_owner_actor_id,
        ),
    ]
    assert len(route_contexts) == 2

    with psycopg.connect(job_database.migrator) as connection:
        event_rows = connection.execute(
            """
            SELECT event_type, status, attempt_count, reason_code
            FROM schemabridge_control.execution_job_events
            WHERE workspace_id = %s AND job_id = %s
            ORDER BY sequence
            """,
            (submitted.authorization.workspace_id, submitted.id),
        ).fetchall()
    assert event_rows == [
        ("submitted", "queued", 0, None),
        ("claimed", "leased", 1, None),
        ("retry_scheduled", "retry_wait", 1, "source_unavailable"),
        ("claimed", "leased", 2, None),
        ("dead_lettered", "dead_lettered", 2, "source_unavailable"),
    ]


def test_expired_authorization_closes_abandoned_leases_without_stealing_a_heartbeat(
    job_database: _DatabaseUrls,
) -> None:
    leased_job, leased_api, leased_worker = _job(
        job_database,
        name="expired-abandoned-lease",
        workspace_name="expired-abandoned-a",
        expires_in=timedelta(seconds=1),
    )
    cancelled_job, cancelled_api, cancelled_worker = _job(
        job_database,
        name="expired-abandoned-cancel",
        workspace_name="expired-abandoned-b",
        expires_in=timedelta(seconds=1),
    )
    leased_api.submit(leased_job)
    cancelled_api.submit(cancelled_job)
    leased_token = "expired-leased-capability-" + ("l" * 48)
    cancelled_token = "expired-cancelled-capability-" + ("c" * 48)
    leased = leased_worker.claim_next(
        worker_id="worker-expired-leased",
        lease_token=leased_token,
        lease_duration=timedelta(seconds=2),
    )
    cancelled_lease = cancelled_worker.claim_next(
        worker_id="worker-expired-cancelled",
        lease_token=cancelled_token,
        lease_duration=timedelta(seconds=2),
    )
    assert leased is not None and leased.lease is not None
    assert cancelled_lease is not None and cancelled_lease.lease is not None
    cancel_requested = cancelled_api.request_cancellation(
        cancelled_job.authorization.workspace_id,
        cancelled_job.id,
        submitting_actor_id=cancelled_job.authorization.submitting_actor_id,
        requested_at=datetime.now(UTC),
    )
    assert cancel_requested is not None
    assert cancel_requested.status is JobStatus.CANCEL_REQUESTED

    time.sleep(1.1)
    renewed = leased_worker.heartbeat(
        leased_job.id,
        worker_id=leased.lease.worker_id,
        lease_token=leased_token,
        fencing_token=leased.lease.fencing_token,
        lease_duration=timedelta(seconds=2),
    )
    assert renewed.status is JobStatus.LEASED
    assert renewed.lease is not None
    assert renewed.last_fencing_token == leased.last_fencing_token
    assert leased_worker.expire_authorizations(limit=10) == 0

    time.sleep(1.1)
    assert leased_worker.expire_authorizations(limit=10) == 1
    still_renewed = leased_api.load(
        leased_job.authorization.workspace_id,
        leased_job.id,
    )
    assert still_renewed is not None
    assert still_renewed.status is JobStatus.LEASED
    assert still_renewed.last_fencing_token == leased.last_fencing_token

    time.sleep(1.1)
    assert leased_worker.expire_authorizations(limit=10) == 1
    expired_leased = leased_api.load(
        leased_job.authorization.workspace_id,
        leased_job.id,
    )
    expired_cancelled = cancelled_api.load(
        cancelled_job.authorization.workspace_id,
        cancelled_job.id,
    )
    assert expired_leased is not None and expired_cancelled is not None
    for expired in (expired_leased, expired_cancelled):
        assert expired.status is JobStatus.FAILED
        assert expired.failure is not None
        assert expired.failure.code is JobFailureCode.AUTHORIZATION_EXPIRED
        assert expired.attempt_count == 1
        assert expired.last_fencing_token == 1
        assert expired.lease is None
    assert expired_cancelled.cancel_requested_at is not None
    assert _events(job_database, leased_job) == ("submitted", "claimed", "failed")
    assert _events(job_database, cancelled_job) == (
        "submitted",
        "claimed",
        "cancel_requested",
        "failed",
    )
    assert (
        leased_worker.claim_next(
            worker_id="worker-must-not-reclaim-expired",
            lease_token="must-not-reclaim-" + ("r" * 48),
            lease_duration=timedelta(seconds=1),
        )
        is None
    )
    with pytest.raises(JobStoreError) as stale:
        leased_worker.heartbeat(
            leased_job.id,
            worker_id=leased.lease.worker_id,
            lease_token=leased_token,
            fencing_token=leased.lease.fencing_token,
            lease_duration=timedelta(seconds=1),
        )
    assert stale.value.code in {
        JobStoreErrorCode.LEASE_CONFLICT,
        JobStoreErrorCode.STATE_CONFLICT,
    }


def test_api_worker_and_existing_roles_have_exact_negative_privileges(
    job_database: _DatabaseUrls,
) -> None:
    immutable, immutable_api, _ = _job(job_database, name="immutable")
    immutable_api.submit(immutable)
    immutable_cancelled = immutable_api.request_cancellation(
        immutable.authorization.workspace_id,
        immutable.id,
        submitting_actor_id=immutable.authorization.submitting_actor_id,
        requested_at=datetime.now(UTC),
    )
    assert immutable_cancelled is not None

    privilege_expectations = {
        job_database.api: {
            "job_select": True,
            "job_insert_column": True,
            "lease_update": False,
            "cancel_update": True,
            "pointer_select": False,
        },
        job_database.worker: {
            "job_select": True,
            "job_insert_column": False,
            "lease_update": True,
            "cancel_update": False,
            "pointer_select": True,
        },
        job_database.runtime: {
            "job_select": False,
            "job_insert_column": False,
            "lease_update": False,
            "cancel_update": False,
            "pointer_select": True,
        },
        job_database.reconciler: {
            "job_select": False,
            "job_insert_column": False,
            "lease_update": False,
            "cancel_update": False,
            "pointer_select": True,
        },
        job_database.observer: {
            "job_select": False,
            "job_insert_column": False,
            "lease_update": False,
            "cancel_update": False,
            "pointer_select": False,
        },
        job_database.backup: {
            "job_select": True,
            "job_insert_column": False,
            "lease_update": False,
            "cancel_update": False,
            "pointer_select": True,
        },
    }
    for dsn, expected in privilege_expectations.items():
        with psycopg.connect(dsn) as connection:
            row = connection.execute(
                """
                SELECT
                    has_table_privilege(
                        current_user,
                        'schemabridge_control.execution_jobs',
                        'SELECT'
                    ),
                    has_column_privilege(
                        current_user,
                        'schemabridge_control.execution_jobs',
                        'job_id',
                        'INSERT'
                    ),
                    has_column_privilege(
                        current_user,
                        'schemabridge_control.execution_jobs',
                        'lease_owner_id',
                        'UPDATE'
                    ),
                    has_column_privilege(
                        current_user,
                        'schemabridge_control.execution_jobs',
                        'cancel_requested_at',
                        'UPDATE'
                    ),
                    has_table_privilege(
                        current_user,
                        'schemabridge_control.registry_active_pointers',
                        'SELECT'
                    )
                """
            ).fetchone()
        assert row is not None
        assert (
            dict(
                zip(
                    (
                        "job_select",
                        "job_insert_column",
                        "lease_update",
                        "cancel_update",
                        "pointer_select",
                    ),
                    row,
                    strict=True,
                )
            )
            == expected
        )

    forbidden_statements = (
        (
            job_database.api,
            "UPDATE schemabridge_control.execution_jobs "
            "SET lease_owner_id = 'forbidden' WHERE false",
        ),
        (
            job_database.worker,
            "UPDATE schemabridge_control.execution_jobs "
            "SET cancel_requested_at = clock_timestamp() WHERE false",
        ),
        (
            job_database.worker,
            "INSERT INTO schemabridge_control.execution_jobs (job_id) "
            "VALUES ('forbidden-worker-submit')",
        ),
        (
            job_database.api,
            "UPDATE schemabridge_control.registry_active_pointers "
            "SET generation = generation WHERE false",
        ),
        (
            job_database.worker,
            "UPDATE schemabridge_control.registry_active_pointers "
            "SET generation = generation WHERE false",
        ),
        (
            job_database.runtime,
            "SELECT * FROM schemabridge_control.execution_jobs LIMIT 1",
        ),
        (
            job_database.reconciler,
            "SELECT * FROM schemabridge_control.execution_jobs LIMIT 1",
        ),
        (
            job_database.observer,
            "SELECT * FROM schemabridge_control.execution_jobs LIMIT 1",
        ),
        (
            job_database.api,
            "SET ROLE schemabridge_migrator",
        ),
        (
            job_database.worker,
            "SET ROLE schemabridge_migrator",
        ),
        (
            job_database.observer,
            "SET ROLE schemabridge_migrator",
        ),
        (
            job_database.api,
            "CREATE TABLE schemabridge_control.forbidden_api (id integer)",
        ),
        (
            job_database.worker,
            "CREATE TABLE schemabridge_control.forbidden_worker (id integer)",
        ),
    )
    for dsn, statement in forbidden_statements:
        with (
            psycopg.connect(dsn) as connection,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            connection.execute(statement)

    with (
        psycopg.connect(job_database.migrator) as connection,
        pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState),
    ):
        connection.execute(
            """
            UPDATE schemabridge_control.execution_jobs
            SET status = 'failed'
            WHERE job_id = %s
            """,
            (immutable.id,),
        )

    event_params = (
        immutable.authorization.workspace_id,
        immutable.id,
        immutable.attempt_count,
        immutable.last_fencing_token,
        immutable_cancelled.updated_at,
    )
    with (
        psycopg.connect(job_database.api) as connection,
        pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState),
    ):
        connection.execute(
            """
            INSERT INTO schemabridge_control.execution_job_events (
                event_id,
                workspace_id,
                job_id,
                event_type,
                status,
                attempt_count,
                fencing_token,
                reason_code,
                occurred_at
            ) VALUES (
                'job_event_forged_submitted',
                %s,
                %s,
                'submitted',
                'cancelled',
                %s,
                %s,
                NULL,
                %s
            )
            """,
            event_params,
        )
    with (
        psycopg.connect(job_database.worker) as connection,
        pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState),
    ):
        connection.execute(
            """
            INSERT INTO schemabridge_control.execution_job_events (
                event_id,
                workspace_id,
                job_id,
                event_type,
                status,
                attempt_count,
                fencing_token,
                reason_code,
                occurred_at
            ) VALUES (
                'job_event_forged_claimed',
                %s,
                %s,
                'claimed',
                'cancelled',
                %s,
                %s,
                NULL,
                %s
            )
            """,
            event_params,
        )
    with (
        psycopg.connect(job_database.api) as connection,
        pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState),
    ):
        connection.execute(
            """
            INSERT INTO schemabridge_control.execution_job_events (
                event_id,
                workspace_id,
                job_id,
                event_type,
                status,
                attempt_count,
                fencing_token,
                reason_code,
                occurred_at
            ) VALUES (
                'job_event_forged_time',
                %s,
                %s,
                'cancelled',
                'cancelled',
                %s,
                %s,
                NULL,
                %s
            )
            """,
            (*event_params[:-1], event_params[-1] + timedelta(seconds=1)),
        )
    with (
        psycopg.connect(job_database.api) as connection,
        pytest.raises(psycopg.errors.UniqueViolation),
    ):
        connection.execute(
            """
            INSERT INTO schemabridge_control.execution_job_events (
                event_id,
                workspace_id,
                job_id,
                event_type,
                status,
                attempt_count,
                fencing_token,
                reason_code,
                occurred_at
            ) VALUES (
                'job_event_duplicate_cancelled',
                %s,
                %s,
                'cancelled',
                'cancelled',
                %s,
                %s,
                NULL,
                %s
            )
            """,
            event_params,
        )
    with (
        psycopg.connect(job_database.migrator) as connection,
        pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState),
    ):
        connection.execute(
            """
            UPDATE schemabridge_control.execution_job_events
            SET event_type = 'failed'
            WHERE job_id = %s
            """,
            (immutable.id,),
        )


def test_two_replica_submission_admits_exactly_five_tenant_jobs(
    job_database: _DatabaseUrls,
) -> None:
    jobs = [
        _job(
            job_database,
            name=f"quota-{index}",
            workspace_name="quota-shared",
        )[0]
        for index in range(8)
    ]
    workspace_id = jobs[0].authorization.workspace_id
    with psycopg.connect(job_database.migrator) as connection:
        connection.execute(
            """
            UPDATE schemabridge_control.tenant_capacity_policies
            SET nonterminal_job_limit = 5,
                version = version + 1,
                updated_by = 'test_capacity_admin',
                updated_at = clock_timestamp()
            WHERE workspace_id = %s
            """,
            (workspace_id,),
        )
    barrier = Barrier(len(jobs))

    def submit(candidate: BackgroundJob) -> str:
        barrier.wait()
        try:
            PostgresBackgroundJobStore(job_database.api).submit(candidate)
        except JobStoreError as error:
            return error.code.value
        return "admitted"

    with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
        outcomes = list(executor.map(submit, jobs))

    assert outcomes.count("admitted") == 5
    assert outcomes.count(JobStoreErrorCode.CAPACITY_EXCEEDED.value) == 3


@pytest.mark.scale
def test_fair_claim_reaches_small_tenant_behind_one_hundred_jobs(
    job_database: _DatabaseUrls,
) -> None:
    noisy = [
        _job(
            job_database,
            name=f"fair-noisy-{index:03d}",
            workspace_name="fair-noisy",
        )[0]
        for index in range(100)
    ]
    quiet = _job(
        job_database,
        name="fair-quiet-only",
        workspace_name="fair-quiet",
    )[0]
    api = PostgresBackgroundJobStore(job_database.api)
    worker = PostgresBackgroundJobStore(job_database.worker)
    for candidate in (*noisy, quiet):
        api.submit(candidate)

    first = worker.claim_next(
        worker_id="worker-fair-one",
        lease_token="fair-lease-token-one-with-enough-entropy-1234567890",
        lease_duration=timedelta(minutes=1),
    )
    second = worker.claim_next(
        worker_id="worker-fair-two",
        lease_token="fair-lease-token-two-with-enough-entropy-0987654321",
        lease_duration=timedelta(minutes=1),
    )

    assert first is not None
    assert second is not None
    assert {
        first.authorization.workspace_id,
        second.authorization.workspace_id,
    } == {"workspace-fair-noisy", "workspace-fair-quiet"}
