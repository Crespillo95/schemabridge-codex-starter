from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

import pytest
from psycopg import sql

from schemabridge.adapters.control_plane.postgres_jobs import (
    PostgresBackgroundJobStore,
)
from schemabridge.application.ports.background_jobs import (
    JobStoreError,
    JobStoreErrorCode,
)
from schemabridge.domain.background_jobs import (
    BackgroundJob,
    JobAuthorization,
)

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
DB_NOW = NOW + timedelta(seconds=11)
RAW_LEASE_TOKEN = "worker-capability-" + ("x" * 48)
ROOT = Path(__file__).resolve().parents[2]


@dataclass
class _Cursor:
    rows: list[tuple[object, ...]] = field(default_factory=list)
    rowcount: int = 0

    def fetchone(self) -> tuple[object, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> Sequence[tuple[object, ...]]:
        return tuple(self.rows)


class _Composable(Protocol):
    def as_string(self) -> str: ...


@dataclass(frozen=True)
class _Step:
    query_fragment: str
    rows: tuple[tuple[object, ...], ...] = ()
    rowcount: int = 0


@dataclass
class _Connection:
    steps: list[_Step]
    statements: list[tuple[str, Sequence[object] | None]] = field(default_factory=list)

    def execute(
        self,
        query: object,
        params: Sequence[object] | None = None,
    ) -> _Cursor:
        text = query if isinstance(query, str) else cast(_Composable, query).as_string()
        assert isinstance(text, str)
        self.statements.append((text, params))
        assert self.steps, f"unexpected query: {text}"
        step = self.steps.pop(0)
        assert step.query_fragment in " ".join(text.split())
        return _Cursor(list(step.rows), step.rowcount)


@dataclass
class _Database:
    connection: _Connection

    @contextmanager
    def connect(self) -> Iterator[_Connection]:
        yield self.connection

    @staticmethod
    def table(name: str) -> sql.Composed:
        return sql.SQL("{}.{}").format(
            sql.Identifier("schemabridge_control"),
            sql.Identifier(name),
        )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _queued_job(
    *,
    job_id: str = "job-unit-001",
    idempotency_digest: str | None = None,
    expected_plan_fingerprint: str | None = None,
) -> BackgroundJob:
    authorization = JobAuthorization.create(
        workspace_id="workspace-unit",
        workflow_id="workflow-unit",
        workflow_owner_actor_id="owner-unit",
        submitting_actor_id="submitter-unit",
        expected_workflow_revision=4,
        expected_plan_fingerprint=expected_plan_fingerprint or _digest("plan"),
        authenticated_at=NOW - timedelta(minutes=1),
        authorized_at=NOW - timedelta(seconds=30),
        expires_at=NOW + timedelta(minutes=30),
    )
    return BackgroundJob.create(
        id=job_id,
        authorization=authorization,
        idempotency_digest=idempotency_digest or _digest("idempotency-key"),
        max_attempts=3,
        created_at=NOW,
    )


def _queued_row(job: BackgroundJob, *, created_at: datetime = NOW) -> tuple[object, ...]:
    authorization = job.authorization
    values: dict[str, object] = {
        "job_id": job.id,
        "kind": job.kind.value,
        "workspace_id": authorization.workspace_id,
        "workflow_workspace_id": authorization.workflow_workspace_id,
        "workflow_id": authorization.workflow_id,
        "workflow_owner_actor_id": authorization.workflow_owner_actor_id,
        "submitting_actor_id": authorization.submitting_actor_id,
        "workflow_access_scope": authorization.workflow_access_scope.value,
        "authorized_operation": authorization.operation.value,
        "expected_workflow_revision": authorization.expected_workflow_revision,
        "expected_plan_fingerprint": authorization.expected_plan_fingerprint,
        "connector_workspace_id": None,
        "connector_connection_id": None,
        "connector_contract_version": None,
        "connector_route_revision": None,
        "connector_route_fingerprint": None,
        "connector_target_fingerprint": None,
        "authenticated_at": authorization.authenticated_at,
        "authorized_at": authorization.authorized_at,
        "authorization_expires_at": authorization.expires_at,
        "payload_fingerprint": authorization.payload_fingerprint,
        "request_fingerprint": authorization.request_fingerprint,
        "idempotency_digest": job.idempotency_digest,
        "status": "queued",
        "attempt_count": 0,
        "max_attempts": job.max_attempts,
        "available_at": created_at,
        "lease_owner_id": None,
        "lease_token_digest": None,
        "fencing_token": 0,
        "lease_acquired_at": None,
        "lease_heartbeat_at": None,
        "lease_expires_at": None,
        "cancel_requested_at": None,
        "failure_code": None,
        "result_workflow_stage": None,
        "result_workflow_revision": None,
        "result_row_count": None,
        "result_preview_fingerprint": None,
        "result_rejected_count": None,
        "result_rejection_code_counts": None,
        "result_truncated": None,
        "created_at": created_at,
        "updated_at": created_at,
        "completed_at": None,
    }
    column_order = (
        "job_id",
        "kind",
        "workspace_id",
        "workflow_workspace_id",
        "workflow_id",
        "workflow_owner_actor_id",
        "submitting_actor_id",
        "workflow_access_scope",
        "authorized_operation",
        "expected_workflow_revision",
        "expected_plan_fingerprint",
        "connector_workspace_id",
        "connector_connection_id",
        "connector_contract_version",
        "connector_route_revision",
        "connector_route_fingerprint",
        "connector_target_fingerprint",
        "authenticated_at",
        "authorized_at",
        "authorization_expires_at",
        "payload_fingerprint",
        "request_fingerprint",
        "idempotency_digest",
        "status",
        "attempt_count",
        "max_attempts",
        "available_at",
        "lease_owner_id",
        "lease_token_digest",
        "fencing_token",
        "lease_acquired_at",
        "lease_heartbeat_at",
        "lease_expires_at",
        "cancel_requested_at",
        "failure_code",
        "result_workflow_stage",
        "result_workflow_revision",
        "result_row_count",
        "result_preview_fingerprint",
        "result_rejected_count",
        "result_rejection_code_counts",
        "result_truncated",
        "created_at",
        "updated_at",
        "completed_at",
    )
    return tuple(values[name] for name in column_order)


def _store_with_steps(*steps: _Step) -> tuple[PostgresBackgroundJobStore, _Connection]:
    store = PostgresBackgroundJobStore("postgresql://ignored:secret@invalid/control")
    connection = _Connection(list(steps))
    object.__setattr__(store, "_database", _Database(connection))
    return store, connection


def test_store_redacts_dsn_and_validates_schema() -> None:
    store = PostgresBackgroundJobStore("postgresql://operator:secret@invalid/control")

    assert "secret" not in repr(store)
    with pytest.raises(ValueError, match="schema"):
        PostgresBackgroundJobStore(
            "postgresql://ignored:secret@invalid/control",
            schema="invalid-schema",
        )


def test_load_applies_workspace_and_submitting_actor_scope() -> None:
    job = _queued_job()
    store, connection = _store_with_steps(
        _Step("AND submitting_actor_id = %s", rows=(_queued_row(job),))
    )

    loaded = store.load(
        "workspace-unit",
        job.id,
        submitting_actor_id="submitter-unit",
    )

    assert loaded == job
    assert connection.statements[0][1] == (
        "workspace-unit",
        job.id,
        "submitter-unit",
    )
    assert connection.steps == []


def test_load_by_idempotency_requires_exact_workspace_and_submitter_scope() -> None:
    job = _queued_job()
    store, connection = _store_with_steps(
        _Step("AND idempotency_digest = %s", rows=(_queued_row(job),))
    )

    loaded = store.load_by_idempotency(
        "workspace-unit",
        "submitter-unit",
        job.idempotency_digest,
    )

    assert loaded == job
    assert connection.statements[0][1] == (
        "workspace-unit",
        "submitter-unit",
        job.idempotency_digest,
    )
    assert connection.steps == []


def test_claim_uses_database_time_and_persists_only_lease_token_digest() -> None:
    job = _queued_job()
    store, connection = _store_with_steps(
        _Step(
            "FOR UPDATE OF tenant SKIP LOCKED",
            rows=((job.authorization.workspace_id,),),
        ),
        _Step("FOR UPDATE SKIP LOCKED", rows=(_queued_row(job),)),
        _Step("SELECT clock_timestamp()", rows=((DB_NOW,),)),
        _Step("UPDATE", rowcount=1),
        _Step("INSERT INTO", rowcount=1),
        _Step("UPDATE", rowcount=1),
    )

    claimed = store.claim_next(
        worker_id="worker-unit",
        lease_token=RAW_LEASE_TOKEN,
        lease_duration=timedelta(seconds=30),
    )

    assert claimed is not None
    assert claimed.lease is not None
    assert claimed.lease.acquired_at == DB_NOW
    assert claimed.lease.heartbeat_at == DB_NOW
    assert claimed.lease.expires_at == DB_NOW + timedelta(seconds=30)
    assert claimed.lease.token_digest == _digest(RAW_LEASE_TOKEN)
    all_parameters = repr([params for _, params in connection.statements])
    assert RAW_LEASE_TOKEN not in all_parameters
    assert _digest(RAW_LEASE_TOKEN) in all_parameters
    assert connection.statements[1][1] == (job.authorization.workspace_id,)
    assert connection.steps == []


def test_queued_cancellation_ignores_caller_time_and_uses_database_time() -> None:
    job = _queued_job()
    store, connection = _store_with_steps(
        _Step("FOR UPDATE", rows=(_queued_row(job),)),
        _Step("SELECT clock_timestamp()", rows=((DB_NOW,),)),
        _Step("UPDATE", rowcount=1),
        _Step("INSERT INTO", rowcount=1),
    )

    cancelled = store.request_cancellation(
        "workspace-unit",
        job.id,
        submitting_actor_id="submitter-unit",
        requested_at=datetime(2000, 1, 1, tzinfo=UTC),
    )

    assert cancelled is not None
    assert cancelled.status.value == "cancelled"
    assert cancelled.cancel_requested_at == DB_NOW
    assert cancelled.completed_at == DB_NOW
    assert cancelled.updated_at == DB_NOW
    assert connection.steps == []


def test_same_digest_with_different_request_is_a_sanitized_conflict() -> None:
    existing = _queued_job()
    changed = _queued_job(
        job_id="job-unit-002",
        idempotency_digest=existing.idempotency_digest,
        expected_plan_fingerprint=_digest("different-plan"),
    )
    store, connection = _store_with_steps(
        _Step("SELECT clock_timestamp()", rows=((DB_NOW,),)),
        _Step("ON CONFLICT", rows=()),
        _Step("FOR SHARE", rows=(_queued_row(existing),)),
    )

    with pytest.raises(JobStoreError) as raised:
        store.submit(changed)

    assert raised.value.code is JobStoreErrorCode.IDEMPOTENCY_CONFLICT
    assert str(raised.value) == "idempotency key was already used for a different request"
    assert "different-plan" not in str(raised.value)
    assert connection.steps == []


def test_migration_has_rows_free_state_digest_leases_and_separate_roles() -> None:
    migration = (ROOT / "migrations/control_plane/0002_authenticated_api_jobs.sql").read_text(
        encoding="utf-8"
    )
    expiry_guard = (
        ROOT / "migrations/control_plane/0003_reject_expired_job_success.sql"
    ).read_text(encoding="utf-8")
    roles = (ROOT / "demo/control_plane/init/001_roles.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE schemabridge_control.execution_jobs" in migration
    assert "CREATE TABLE schemabridge_control.execution_job_events" in migration
    assert "lease_token_digest" in migration
    assert "lease_token " not in migration
    assert "preview_rows" not in migration
    assert "result_preview_fingerprint" in migration
    assert "result_rejected_count bigint" in migration
    assert "result_rejected_count BETWEEN 0 AND 2147483647" in migration
    assert "rejection_key = 'unclassified_rejections'" in migration
    assert "rejection_key <> 'unclassified_rejections'" in migration
    assert "FOR UPDATE SKIP LOCKED" not in migration
    assert "CREATE ROLE" not in migration.upper()
    assert "CREATE ROLE schemabridge_api" in roles
    assert "CREATE ROLE schemabridge_worker" in roles
    assert "NOINHERIT" in roles
    assert "execution_job_events_transition_unique" in migration
    assert "NEW.occurred_at <> current_updated_at" in migration
    assert "NEW.event_type = 'submitted' AND NEW.status = 'queued'" in migration
    assert "OLD.authorization_expires_at <= clock_timestamp()" in expiry_guard
    assert "expired execution job authorization cannot succeed" in expiry_guard
    assert "WHEN (NEW.status = 'succeeded')" in expiry_guard
    api_update = migration.split(
        "GRANT UPDATE (\n    status,\n    available_at,\n    cancel_requested_by,",
        maxsplit=1,
    )[1].split("TO schemabridge_api;", maxsplit=1)[0]
    assert "lease_owner_id" not in api_update
    assert "lease_token_digest" not in api_update
    assert "lease_heartbeat_at" not in api_update
