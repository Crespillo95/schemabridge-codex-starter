"""PostgreSQL queue for governed preview-execution jobs.

The API role may submit, inspect, and request cancellation. The worker role may
claim and close work. Lease capabilities are hashed before they cross the
database boundary, and every state-changing method uses PostgreSQL time.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.errors import (
    CheckViolation,
    ForeignKeyViolation,
    UndefinedColumn,
    UndefinedTable,
    UniqueViolation,
)
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from schemabridge.adapters.storage.postgres import (
    ControlConnectionProvider,
    _ControlDatabase,
)
from schemabridge.application.ports.background_jobs import (
    JobStoreError,
    JobStoreErrorCode,
)
from schemabridge.domain.background_jobs import (
    UNCLASSIFIED_REJECTION_COUNT_CODE,
    BackgroundJob,
    JobAuthorization,
    JobExecutionTargetRef,
    JobFailure,
    JobFailureCode,
    JobFailureDisposition,
    JobKind,
    JobLease,
    JobRejectionCount,
    JobResultSummary,
    JobStatus,
    JobSubmissionResult,
    JobTransitionError,
    JobTransitionErrorCode,
    JobWorkflowAccessScope,
    acknowledge_job_cancellation,
    claim_job,
    complete_job,
    dead_letter_exhausted_lease,
    digest_lease_token,
    expire_job_authorization,
    fail_job,
    heartbeat_job,
    job_retry_delay,
    request_job_cancellation,
)
from schemabridge.domain.resolution import MAX_REJECTED_SOURCE_TOTAL
from schemabridge.domain.workflows import WorkflowStage

_MAX_EXPIRY_BATCH = 1_000
_MIN_LEASE_DURATION = timedelta(seconds=1)
_MAX_LEASE_DURATION = timedelta(minutes=5)
_MIN_RETRY_DELAY = timedelta(seconds=1)
_MAX_RETRY_DELAY = timedelta(minutes=5)

_JOB_COLUMN_NAMES = (
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
_JOB_COLUMNS = sql.SQL(", ").join(sql.Identifier(name) for name in _JOB_COLUMN_NAMES)


@dataclass(frozen=True, slots=True)
class PostgresBackgroundJobStore:
    """Atomic, tenant-scoped PostgreSQL implementation of the background-job port."""

    dsn: str = field(repr=False)
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-runtime"
    connection_provider: ControlConnectionProvider | None = field(default=None, repr=False)
    _database: _ControlDatabase = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_database",
            _ControlDatabase(
                self.dsn,
                self.schema,
                application_name=self.application_name,
                connection_provider=self.connection_provider,
            ),
        )

    @property
    def _db(self) -> _ControlDatabase:
        return self._database

    def submit(self, job: BackgroundJob) -> JobSubmissionResult:
        """Atomically insert a queued job plus its first immutable event."""

        if job.status is not JobStatus.QUEUED:
            raise ValueError("only a queued background job may be submitted")
        table = self._db.table("execution_jobs")
        try:
            with self._db.connect() as connection:
                created_at = _database_now(connection)
                submitted = BackgroundJob.create(
                    id=job.id,
                    authorization=job.authorization,
                    connector_contract_version=job.connector_contract_version,
                    idempotency_digest=job.idempotency_digest,
                    max_attempts=job.max_attempts,
                    created_at=created_at,
                )
                row = connection.execute(
                    sql.SQL(
                        """
                        INSERT INTO {table} (
                            job_id,
                            kind,
                            workspace_id,
                            workflow_workspace_id,
                            workflow_id,
                            workflow_owner_actor_id,
                            submitting_actor_id,
                            workflow_access_scope,
                            authorized_operation,
                            expected_workflow_revision,
                            expected_plan_fingerprint,
                            connector_workspace_id,
                            connector_connection_id,
                            connector_route_revision,
                            connector_route_fingerprint,
                            connector_target_fingerprint,
                            authenticated_at,
                            authorized_at,
                            authorization_expires_at,
                            payload_fingerprint,
                            request_fingerprint,
                            idempotency_digest,
                            status,
                            attempt_count,
                            max_attempts,
                            available_at,
                            fencing_token,
                            created_at,
                            updated_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s
                        )
                        ON CONFLICT DO NOTHING
                        RETURNING {columns}
                        """
                    ).format(table=table, columns=_JOB_COLUMNS),
                    _submission_params(submitted),
                ).fetchone()
                if row is not None:
                    persisted = _job_from_row(row)
                    self._append_event(
                        connection,
                        persisted,
                        event_type="submitted",
                    )
                    return JobSubmissionResult(job=persisted, replayed=False)

                replay_row = connection.execute(
                    sql.SQL(
                        """
                        SELECT {columns}
                        FROM {table}
                        WHERE workspace_id = %s
                          AND submitting_actor_id = %s
                          AND idempotency_digest = %s
                        FOR SHARE
                        """
                    ).format(columns=_JOB_COLUMNS, table=table),
                    (
                        job.authorization.workspace_id,
                        job.authorization.submitting_actor_id,
                        job.idempotency_digest,
                    ),
                ).fetchone()
                if replay_row is None:
                    raise JobStoreError(
                        JobStoreErrorCode.STATE_CONFLICT,
                        "background job submission conflicted",
                    )
                replayed = _job_from_row(replay_row)
                if replayed.request_fingerprint != job.request_fingerprint:
                    raise JobStoreError(
                        JobStoreErrorCode.IDEMPOTENCY_CONFLICT,
                        "idempotency key was already used for a different request",
                    )
                return JobSubmissionResult(job=replayed, replayed=True)
        except JobStoreError:
            raise
        except UniqueViolation as error:
            raise JobStoreError(
                JobStoreErrorCode.STATE_CONFLICT,
                "background job identity already exists",
            ) from error
        except (CheckViolation, ForeignKeyViolation) as error:
            raise JobStoreError(
                JobStoreErrorCode.STATE_CONFLICT,
                "background job submission no longer matches control state",
            ) from error
        except (ValidationError, JobTransitionError, TypeError, ValueError) as error:
            raise _invalid_response("background job submission response was invalid") from error
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except psycopg.Error as error:
            if error.sqlstate == "53300":
                raise JobStoreError(
                    JobStoreErrorCode.CAPACITY_EXCEEDED,
                    "tenant execution-job capacity is exhausted",
                ) from error
            raise _store_unavailable("background job submission failed") from error

    def load(
        self,
        workspace_id: str,
        job_id: str,
        *,
        submitting_actor_id: str | None = None,
    ) -> BackgroundJob | None:
        """Load within an exact workspace and optional submitting-actor scope."""

        _bounded_identifier(workspace_id, "job workspace", maximum=200)
        _bounded_identifier(job_id, "job identifier", maximum=200)
        if submitting_actor_id is not None:
            _bounded_identifier(submitting_actor_id, "job submitting actor", maximum=200)
        table = self._db.table("execution_jobs")
        owner_clause = (
            sql.SQL(" AND submitting_actor_id = %s")
            if submitting_actor_id is not None
            else sql.SQL("")
        )
        params: tuple[object, ...] = (workspace_id, job_id)
        if submitting_actor_id is not None:
            params = (*params, submitting_actor_id)
        try:
            with self._db.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT {columns}
                        FROM {table}
                        WHERE workspace_id = %s
                          AND job_id = %s
                        """
                    ).format(columns=_JOB_COLUMNS, table=table)
                    + owner_clause,
                    params,
                ).fetchone()
            return None if row is None else _job_from_row(row)
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response("background job response was invalid") from error
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except psycopg.Error as error:
            raise _store_unavailable("background job read failed") from error

    def load_by_idempotency(
        self,
        workspace_id: str,
        submitting_actor_id: str,
        idempotency_digest: str,
    ) -> BackgroundJob | None:
        """Load one exact submitter-scoped idempotency reservation."""

        _bounded_identifier(workspace_id, "job workspace", maximum=200)
        _bounded_identifier(submitting_actor_id, "job submitting actor", maximum=200)
        if (
            not isinstance(idempotency_digest, str)
            or len(idempotency_digest) != 64
            or any(character not in "0123456789abcdef" for character in idempotency_digest)
        ):
            raise ValueError("job idempotency digest must be lowercase SHA-256")
        table = self._db.table("execution_jobs")
        try:
            with self._db.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT {columns}
                        FROM {table}
                        WHERE workspace_id = %s
                          AND submitting_actor_id = %s
                          AND idempotency_digest = %s
                        """
                    ).format(columns=_JOB_COLUMNS, table=table),
                    (workspace_id, submitting_actor_id, idempotency_digest),
                ).fetchone()
            return None if row is None else _job_from_row(row)
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response("background job response was invalid") from error
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except psycopg.Error as error:
            raise _store_unavailable("background job read failed") from error

    def request_cancellation(
        self,
        workspace_id: str,
        job_id: str,
        *,
        submitting_actor_id: str | None,
        requested_at: datetime,
    ) -> BackgroundJob | None:
        """Cancel waiting work or mark leased work, using database time."""

        del requested_at
        _bounded_identifier(workspace_id, "job workspace", maximum=200)
        _bounded_identifier(job_id, "job identifier", maximum=200)
        if submitting_actor_id is not None:
            _bounded_identifier(submitting_actor_id, "job submitting actor", maximum=200)
        table = self._db.table("execution_jobs")
        owner_clause = (
            sql.SQL(" AND submitting_actor_id = %s")
            if submitting_actor_id is not None
            else sql.SQL("")
        )
        params: tuple[object, ...] = (workspace_id, job_id)
        if submitting_actor_id is not None:
            params = (*params, submitting_actor_id)
        try:
            with self._db.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT {columns}
                        FROM {table}
                        WHERE workspace_id = %s
                          AND job_id = %s
                        """
                    ).format(columns=_JOB_COLUMNS, table=table)
                    + owner_clause
                    + sql.SQL(" FOR UPDATE"),
                    params,
                ).fetchone()
                if row is None:
                    return None
                current = _job_from_row(row)
                if current.status in {JobStatus.CANCELLED, JobStatus.CANCEL_REQUESTED}:
                    return current
                changed = request_job_cancellation(
                    current,
                    requested_at=_database_now(connection),
                )
                self._write_api_cancellation(connection, current, changed)
                self._append_event(
                    connection,
                    changed,
                    event_type=(
                        "cancelled" if changed.status is JobStatus.CANCELLED else "cancel_requested"
                    ),
                )
                return changed
        except JobStoreError:
            raise
        except JobTransitionError as error:
            raise _transition_error(error) from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response("background job cancellation response was invalid") from error
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except psycopg.Error as error:
            raise _store_unavailable("background job cancellation failed") from error

    def expire_authorizations(self, *, limit: int = 100) -> int:
        """Fail expired waiting work and abandoned expired leases in one locked batch."""

        if isinstance(limit, bool) or not 1 <= limit <= _MAX_EXPIRY_BATCH:
            raise ValueError("authorization expiry batch must be between 1 and 1000")
        table = self._db.table("execution_jobs")
        try:
            with self._db.connect() as connection:
                rows = connection.execute(
                    sql.SQL(
                        """
                        SELECT {columns}
                        FROM {table}
                        WHERE authorization_expires_at <= clock_timestamp()
                          AND (
                              status IN ('queued', 'retry_wait')
                              OR (
                                  status IN ('leased', 'cancel_requested')
                                  AND lease_expires_at <= clock_timestamp()
                              )
                          )
                        ORDER BY
                            GREATEST(
                                authorization_expires_at,
                                COALESCE(lease_expires_at, authorization_expires_at)
                            ),
                            created_at,
                            job_id
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                        """
                    ).format(columns=_JOB_COLUMNS, table=table),
                    (limit,),
                ).fetchall()
                if not rows:
                    return 0
                expired_at = _database_now(connection)
                for row in rows:
                    current = _job_from_row(row)
                    changed = expire_job_authorization(
                        current,
                        expired_at=expired_at,
                    )
                    self._write_worker_state(connection, current, changed)
                    self._append_event(
                        connection,
                        changed,
                        event_type="failed",
                        reason_code=JobFailureCode.AUTHORIZATION_EXPIRED.value,
                    )
                return len(rows)
        except JobStoreError:
            raise
        except JobTransitionError as error:
            raise _transition_error(error) from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response("expired authorization response was invalid") from error
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except psycopg.Error as error:
            raise _store_unavailable("authorization expiry sweep failed") from error

    def reap_exhausted_leases(self, *, limit: int = 100) -> int:
        """Dead-letter a bounded batch of expired final-attempt leases."""

        if isinstance(limit, bool) or not 1 <= limit <= _MAX_EXPIRY_BATCH:
            raise ValueError("exhausted lease batch must be between 1 and 1000")
        table = self._db.table("execution_jobs")
        try:
            with self._db.connect() as connection:
                rows = connection.execute(
                    sql.SQL(
                        """
                        SELECT {columns}
                        FROM {table}
                        WHERE status IN ('leased', 'cancel_requested')
                          AND attempt_count >= max_attempts
                          AND lease_expires_at <= clock_timestamp()
                        ORDER BY lease_expires_at, created_at, job_id
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                        """
                    ).format(columns=_JOB_COLUMNS, table=table),
                    (limit,),
                ).fetchall()
                if not rows:
                    return 0
                expired_at = _database_now(connection)
                for row in rows:
                    current = _job_from_row(row)
                    changed = dead_letter_exhausted_lease(
                        current,
                        expired_at=expired_at,
                    )
                    self._write_worker_state(connection, current, changed)
                    self._append_event(
                        connection,
                        changed,
                        event_type="dead_lettered",
                        reason_code=JobFailureCode.AMBIGUOUS_EXTERNAL_EFFECT.value,
                    )
                return len(rows)
        except JobStoreError:
            raise
        except JobTransitionError as error:
            raise _transition_error(error) from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response("exhausted lease response was invalid") from error
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except psycopg.Error as error:
            raise _store_unavailable("exhausted lease sweep failed") from error

    def claim_next(
        self,
        *,
        worker_id: str,
        lease_token: str,
        lease_duration: timedelta,
    ) -> BackgroundJob | None:
        """Claim one due or expired lease with SKIP LOCKED and a new fence."""

        _bounded_identifier(worker_id, "worker identifier", maximum=200)
        digest_lease_token(lease_token)
        duration = _bounded_duration(
            lease_duration,
            minimum=_MIN_LEASE_DURATION,
            maximum=_MAX_LEASE_DURATION,
            label="job lease duration",
        )
        table = self._db.table("execution_jobs")
        schedule = self._db.table("tenant_job_schedule")
        try:
            with self._db.connect() as connection:
                workspace_row = connection.execute(
                    sql.SQL(
                        """
                        SELECT tenant.workspace_id
                        FROM {schedule} AS tenant
                        WHERE EXISTS (
                            SELECT 1
                            FROM {table} AS candidate
                            WHERE candidate.workspace_id = tenant.workspace_id
                              AND candidate.authorization_expires_at > clock_timestamp()
                              AND candidate.attempt_count < candidate.max_attempts
                              AND (
                                  (
                                      candidate.status IN ('queued', 'retry_wait')
                                      AND candidate.available_at <= clock_timestamp()
                                  )
                                  OR
                                  (
                                      candidate.status IN ('leased', 'cancel_requested')
                                      AND candidate.lease_expires_at <= clock_timestamp()
                                  )
                              )
                        )
                        ORDER BY tenant.last_claimed_at NULLS FIRST,
                                 tenant.claim_sequence,
                                 tenant.workspace_id
                        LIMIT 1
                        FOR UPDATE OF tenant SKIP LOCKED
                        """
                    ).format(schedule=schedule, table=table)
                ).fetchone()
                if workspace_row is None:
                    return None
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT {columns}
                        FROM {table}
                        WHERE workspace_id = %s
                          AND authorization_expires_at > clock_timestamp()
                          AND attempt_count < max_attempts
                          AND (
                              (
                                  status IN ('queued', 'retry_wait')
                                  AND available_at <= clock_timestamp()
                              )
                              OR
                              (
                                  status IN ('leased', 'cancel_requested')
                                  AND lease_expires_at <= clock_timestamp()
                              )
                          )
                        ORDER BY
                            CASE
                                WHEN status IN ('leased', 'cancel_requested')
                                THEN lease_expires_at
                                ELSE available_at
                            END,
                            created_at,
                            job_id
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                        """
                    ).format(columns=_JOB_COLUMNS, table=table),
                    (workspace_row[0],),
                ).fetchone()
                if row is None:
                    return None
                current = _job_from_row(row)
                claimed_at = _database_now(connection)
                changed = claim_job(
                    current,
                    worker_id=worker_id,
                    lease_token=lease_token,
                    claimed_at=claimed_at,
                    lease_expires_at=claimed_at + duration,
                )
                self._write_worker_state(connection, current, changed)
                self._append_event(connection, changed, event_type="claimed")
                updated_schedule = connection.execute(
                    sql.SQL(
                        """
                        UPDATE {schedule}
                        SET last_claimed_at = %s,
                            claim_sequence = claim_sequence + 1,
                            updated_at = %s
                        WHERE workspace_id = %s
                        """
                    ).format(schedule=schedule),
                    (claimed_at, claimed_at, current.authorization.workspace_id),
                )
                if updated_schedule.rowcount != 1:
                    raise JobStoreError(
                        JobStoreErrorCode.STATE_CONFLICT,
                        "background job fair-scheduling state is unavailable",
                    )
                return changed
        except JobStoreError:
            raise
        except JobTransitionError as error:
            raise _transition_error(error) from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response("background job claim response was invalid") from error
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except psycopg.Error as error:
            raise _store_unavailable("background job claim failed") from error

    def heartbeat(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        fencing_token: int,
        lease_duration: timedelta,
    ) -> BackgroundJob:
        """Extend only the exact unexpired lease, using database time."""

        _bounded_identifier(job_id, "job identifier", maximum=200)
        _bounded_identifier(worker_id, "worker identifier", maximum=200)
        digest_lease_token(lease_token)
        duration = _bounded_duration(
            lease_duration,
            minimum=_MIN_LEASE_DURATION,
            maximum=_MAX_LEASE_DURATION,
            label="job lease duration",
        )
        try:
            with self._db.connect() as connection:
                current = self._load_worker_job_for_update(connection, job_id)
                heartbeat_at = _database_now(connection)
                changed = heartbeat_job(
                    current,
                    worker_id=worker_id,
                    lease_token=lease_token,
                    fencing_token=fencing_token,
                    heartbeat_at=heartbeat_at,
                    lease_expires_at=heartbeat_at + duration,
                )
                self._write_worker_state(connection, current, changed)
                return changed
        except JobStoreError:
            raise
        except JobTransitionError as error:
            raise _transition_error(error) from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response("background job heartbeat response was invalid") from error
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except psycopg.Error as error:
            raise _store_unavailable("background job heartbeat failed") from error

    def succeed(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        fencing_token: int,
        result: JobResultSummary,
    ) -> BackgroundJob:
        """Persist a rows-free result summary and its immutable success event."""

        _bounded_identifier(job_id, "job identifier", maximum=200)
        _bounded_identifier(worker_id, "worker identifier", maximum=200)
        digest_lease_token(lease_token)
        try:
            with self._db.connect() as connection:
                current = self._load_worker_job_for_update(connection, job_id)
                completed_at = _database_now(connection)
                if current.authorization.is_current(completed_at):
                    server_result = result.model_copy(update={"completed_at": completed_at})
                    changed = complete_job(
                        current,
                        worker_id=worker_id,
                        lease_token=lease_token,
                        fencing_token=fencing_token,
                        result=server_result,
                    )
                    event_type = "succeeded"
                else:
                    changed = fail_job(
                        current,
                        worker_id=worker_id,
                        lease_token=lease_token,
                        fencing_token=fencing_token,
                        code=JobFailureCode.AUTHORIZATION_EXPIRED,
                        failed_at=completed_at,
                    )
                    event_type = "failed"
                self._write_worker_state(connection, current, changed)
                self._append_event(
                    connection,
                    changed,
                    event_type=event_type,
                    reason_code=(None if changed.failure is None else changed.failure.code.value),
                )
                return changed
        except JobStoreError:
            raise
        except JobTransitionError as error:
            raise _transition_error(error) from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response("background job success response was invalid") from error
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except psycopg.Error as error:
            raise _store_unavailable("background job completion failed") from error

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
        """Classify and persist one failure, deriving occurrence time in PostgreSQL."""

        _bounded_identifier(job_id, "job identifier", maximum=200)
        _bounded_identifier(worker_id, "worker identifier", maximum=200)
        digest_lease_token(lease_token)
        try:
            with self._db.connect() as connection:
                current = self._load_worker_job_for_update(connection, job_id)
                database_failed_at = _database_now(connection)
                retry_delay: timedelta | None = None
                if retry_at is not None:
                    retry_delay = _bounded_duration(
                        retry_at - failed_at,
                        minimum=_MIN_RETRY_DELAY,
                        maximum=_MAX_RETRY_DELAY,
                        label="job retry delay",
                    )
                    expected_delay = job_retry_delay(current.attempt_count)
                    if retry_delay != expected_delay:
                        raise ValueError("job retry delay is not deterministic")
                changed = fail_job(
                    current,
                    worker_id=worker_id,
                    lease_token=lease_token,
                    fencing_token=fencing_token,
                    code=code,
                    failed_at=database_failed_at,
                    retry_at=(None if retry_delay is None else database_failed_at + retry_delay),
                )
                self._write_worker_state(connection, current, changed)
                event_type = {
                    JobStatus.RETRY_WAIT: "retry_scheduled",
                    JobStatus.FAILED: "failed",
                    JobStatus.DEAD_LETTERED: "dead_lettered",
                }[changed.status]
                self._append_event(
                    connection,
                    changed,
                    event_type=event_type,
                    reason_code=code.value,
                )
                return changed
        except JobStoreError:
            raise
        except JobTransitionError as error:
            raise _transition_error(error) from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response("background job failure response was invalid") from error
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except psycopg.Error as error:
            raise _store_unavailable("background job failure update failed") from error

    def acknowledge_cancellation(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        fencing_token: int,
        cancelled_at: datetime,
    ) -> BackgroundJob:
        """Acknowledge cooperative cancellation with database-observed time."""

        del cancelled_at
        _bounded_identifier(job_id, "job identifier", maximum=200)
        _bounded_identifier(worker_id, "worker identifier", maximum=200)
        digest_lease_token(lease_token)
        try:
            with self._db.connect() as connection:
                current = self._load_worker_job_for_update(connection, job_id)
                if current.status is JobStatus.CANCELLED:
                    return current
                changed = acknowledge_job_cancellation(
                    current,
                    worker_id=worker_id,
                    lease_token=lease_token,
                    fencing_token=fencing_token,
                    cancelled_at=_database_now(connection),
                )
                self._write_worker_state(connection, current, changed)
                self._append_event(connection, changed, event_type="cancelled")
                return changed
        except JobStoreError:
            raise
        except JobTransitionError as error:
            raise _transition_error(error) from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response("job cancellation acknowledgement was invalid") from error
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except psycopg.Error as error:
            raise _store_unavailable("job cancellation acknowledgement failed") from error

    def _load_worker_job_for_update(
        self,
        connection: psycopg.Connection[Any],
        job_id: str,
    ) -> BackgroundJob:
        table = self._db.table("execution_jobs")
        row = connection.execute(
            sql.SQL(
                """
                SELECT {columns}
                FROM {table}
                WHERE job_id = %s
                FOR UPDATE
                """
            ).format(columns=_JOB_COLUMNS, table=table),
            (job_id,),
        ).fetchone()
        if row is None:
            raise JobStoreError(
                JobStoreErrorCode.NOT_FOUND,
                "background job was not found",
            )
        return _job_from_row(row)

    def _write_api_cancellation(
        self,
        connection: psycopg.Connection[Any],
        before: BackgroundJob,
        after: BackgroundJob,
    ) -> None:
        table = self._db.table("execution_jobs")
        result = connection.execute(
            sql.SQL(
                """
                UPDATE {table}
                SET status = %s,
                    available_at = %s,
                    cancel_requested_by = %s,
                    cancel_requested_at = %s,
                    failure_code = NULL,
                    updated_at = %s,
                    completed_at = %s
                WHERE job_id = %s
                  AND status = %s
                  AND updated_at = %s
                """
            ).format(table=table),
            (
                after.status.value,
                after.available_at,
                after.authorization.submitting_actor_id,
                after.cancel_requested_at,
                after.updated_at,
                after.completed_at,
                before.id,
                before.status.value,
                before.updated_at,
            ),
        )
        if result.rowcount != 1:
            raise JobStoreError(
                JobStoreErrorCode.STATE_CONFLICT,
                "background job changed before cancellation",
            )

    def _write_worker_state(
        self,
        connection: psycopg.Connection[Any],
        before: BackgroundJob,
        after: BackgroundJob,
    ) -> None:
        table = self._db.table("execution_jobs")
        lease = after.lease
        result = after.result
        failure = after.failure
        rejection_counts = None if result is None else _serialized_rejection_counts(result)
        update = connection.execute(
            sql.SQL(
                """
                UPDATE {table}
                SET status = %s,
                    attempt_count = %s,
                    available_at = %s,
                    lease_owner_id = %s,
                    lease_token_digest = %s,
                    fencing_token = %s,
                    lease_acquired_at = %s,
                    lease_heartbeat_at = %s,
                    lease_expires_at = %s,
                    failure_code = %s,
                    result_workflow_stage = %s,
                    result_workflow_revision = %s,
                    result_row_count = %s,
                    result_preview_fingerprint = %s,
                    result_rejected_count = %s,
                    result_rejection_code_counts = %s,
                    result_truncated = %s,
                    updated_at = %s,
                    completed_at = %s
                WHERE job_id = %s
                  AND status = %s
                  AND attempt_count = %s
                  AND fencing_token = %s
                  AND updated_at = %s
                """
            ).format(table=table),
            (
                after.status.value,
                after.attempt_count,
                after.available_at,
                None if lease is None else lease.worker_id,
                None if lease is None else lease.token_digest,
                after.last_fencing_token,
                None if lease is None else lease.acquired_at,
                None if lease is None else lease.heartbeat_at,
                None if lease is None else lease.expires_at,
                None if failure is None else failure.code.value,
                None if result is None else result.stage.value,
                None if result is None else result.workflow_revision,
                None if result is None else result.row_count,
                None if result is None else result.preview_fingerprint,
                None if result is None else result.rejected_count,
                rejection_counts,
                None if result is None else result.truncated,
                after.updated_at,
                after.completed_at,
                before.id,
                before.status.value,
                before.attempt_count,
                before.last_fencing_token,
                before.updated_at,
            ),
        )
        if update.rowcount != 1:
            raise JobStoreError(
                JobStoreErrorCode.LEASE_CONFLICT,
                "background job lease or state changed",
            )

    def _append_event(
        self,
        connection: psycopg.Connection[Any],
        job: BackgroundJob,
        *,
        event_type: str,
        reason_code: str | None = None,
    ) -> None:
        events = self._db.table("execution_job_events")
        event_id = f"job_event_{uuid4().hex}"
        inserted = connection.execute(
            sql.SQL(
                """
                INSERT INTO {events} (
                    event_id,
                    workspace_id,
                    job_id,
                    event_type,
                    status,
                    attempt_count,
                    fencing_token,
                    reason_code,
                    occurred_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
            ).format(events=events),
            (
                event_id,
                job.authorization.workspace_id,
                job.id,
                event_type,
                job.status.value,
                job.attempt_count,
                job.last_fencing_token,
                reason_code,
                job.updated_at,
            ),
        )
        if inserted.rowcount != 1:
            raise JobStoreError(
                JobStoreErrorCode.STATE_CONFLICT,
                "background job event was not appended",
            )


def _submission_params(job: BackgroundJob) -> tuple[object, ...]:
    authorization = job.authorization
    target = authorization.execution_target
    assert job.available_at is not None
    return (
        job.id,
        job.kind.value,
        authorization.workspace_id,
        authorization.workflow_workspace_id,
        authorization.workflow_id,
        authorization.workflow_owner_actor_id,
        authorization.submitting_actor_id,
        authorization.workflow_access_scope.value,
        authorization.operation.value,
        authorization.expected_workflow_revision,
        authorization.expected_plan_fingerprint,
        target.workspace_id if target is not None else None,
        target.connection_id.root if target is not None else None,
        target.route_revision if target is not None else None,
        target.route_fingerprint if target is not None else None,
        target.target_fingerprint if target is not None else None,
        authorization.authenticated_at,
        authorization.authorized_at,
        authorization.expires_at,
        authorization.payload_fingerprint,
        authorization.request_fingerprint,
        job.idempotency_digest,
        job.status.value,
        job.attempt_count,
        job.max_attempts,
        job.available_at,
        job.last_fencing_token,
        job.created_at,
        job.updated_at,
    )


def _job_from_row(row: Sequence[object]) -> BackgroundJob:
    if len(row) != len(_JOB_COLUMN_NAMES):
        raise ValueError("background job row has an invalid column count")
    values = dict(zip(_JOB_COLUMN_NAMES, row, strict=True))
    target_values = (
        values["connector_workspace_id"],
        values["connector_connection_id"],
        values["connector_contract_version"],
        values["connector_route_revision"],
        values["connector_route_fingerprint"],
        values["connector_target_fingerprint"],
    )
    if all(value is None for value in target_values):
        execution_target = None
    elif any(value is None for value in target_values):
        raise ValueError("background job connector target is incomplete")
    else:
        _required_int(values["connector_contract_version"])
        execution_target = JobExecutionTargetRef(
            workspace_id=_required_str(values["connector_workspace_id"]),
            connection_id=_required_str(values["connector_connection_id"]),
            route_revision=_required_int(values["connector_route_revision"]),
            route_fingerprint=_required_str(values["connector_route_fingerprint"]),
            target_fingerprint=_required_str(values["connector_target_fingerprint"]),
        )
    authorization = JobAuthorization(
        workspace_id=_required_str(values["workspace_id"]),
        workflow_workspace_id=_required_str(values["workflow_workspace_id"]),
        workflow_id=_required_str(values["workflow_id"]),
        workflow_owner_actor_id=_required_str(values["workflow_owner_actor_id"]),
        submitting_actor_id=_required_str(values["submitting_actor_id"]),
        workflow_access_scope=JobWorkflowAccessScope(
            _required_str(values["workflow_access_scope"])
        ),
        operation=JobKind(_required_str(values["authorized_operation"])),
        expected_workflow_revision=_required_int(values["expected_workflow_revision"]),
        expected_plan_fingerprint=_required_str(values["expected_plan_fingerprint"]),
        execution_target=execution_target,
        authenticated_at=_required_datetime(values["authenticated_at"]),
        authorized_at=_required_datetime(values["authorized_at"]),
        expires_at=_required_datetime(values["authorization_expires_at"]),
        payload_fingerprint=_required_str(values["payload_fingerprint"]),
        request_fingerprint=_required_str(values["request_fingerprint"]),
    )
    status = JobStatus(_required_str(values["status"]))
    attempt = _required_int(values["attempt_count"])
    fence = _required_int(values["fencing_token"])

    lease: JobLease | None = None
    if values["lease_owner_id"] is not None:
        lease = JobLease(
            job_id=_required_str(values["job_id"]),
            worker_id=_required_str(values["lease_owner_id"]),
            token_digest=_required_str(values["lease_token_digest"]),
            fencing_token=fence,
            attempt=attempt,
            acquired_at=_required_datetime(values["lease_acquired_at"]),
            heartbeat_at=_required_datetime(values["lease_heartbeat_at"]),
            expires_at=_required_datetime(values["lease_expires_at"]),
        )

    completed_at = _optional_datetime(values["completed_at"])
    result: JobResultSummary | None = None
    if values["result_workflow_stage"] is not None:
        if completed_at is None:
            raise ValueError("background job result has no completion time")
        rejection_counts, unclassified_count = _rejection_counts(
            values["result_rejection_code_counts"]
        )
        rejected_count = _required_int(values["result_rejected_count"])
        if unclassified_count != rejected_count - sum(item.count for item in rejection_counts):
            raise ValueError("background job unclassified rejection count is invalid")
        result = JobResultSummary(
            workflow_id=authorization.workflow_id,
            workflow_revision=_required_int(values["result_workflow_revision"]),
            stage=WorkflowStage(_required_str(values["result_workflow_stage"])),
            row_count=_required_int(values["result_row_count"]),
            preview_fingerprint=_required_str(values["result_preview_fingerprint"]),
            rejected_count=rejected_count,
            rejection_code_counts=rejection_counts,
            truncated=_required_bool(values["result_truncated"]),
            completed_at=completed_at,
        )

    failure: JobFailure | None = None
    if values["failure_code"] is not None:
        disposition = {
            JobStatus.RETRY_WAIT: JobFailureDisposition.RETRY,
            JobStatus.FAILED: JobFailureDisposition.FAIL,
            JobStatus.DEAD_LETTERED: JobFailureDisposition.DEAD_LETTER,
        }.get(status)
        if disposition is None:
            raise ValueError("background job failure has an invalid status")
        failure = JobFailure(
            code=JobFailureCode(_required_str(values["failure_code"])),
            disposition=disposition,
            attempt=attempt,
            occurred_at=_required_datetime(values["updated_at"]),
        )

    return BackgroundJob(
        id=_required_str(values["job_id"]),
        kind=JobKind(_required_str(values["kind"])),
        status=status,
        authorization=authorization,
        connector_contract_version=(
            _required_int(values["connector_contract_version"])
            if execution_target is not None
            else None
        ),
        idempotency_digest=_required_str(values["idempotency_digest"]),
        request_fingerprint=_required_str(values["request_fingerprint"]),
        max_attempts=_required_int(values["max_attempts"]),
        attempt_count=attempt,
        last_fencing_token=fence,
        created_at=_required_datetime(values["created_at"]),
        updated_at=_required_datetime(values["updated_at"]),
        available_at=_optional_datetime(values["available_at"]),
        lease=lease,
        cancel_requested_at=_optional_datetime(values["cancel_requested_at"]),
        completed_at=completed_at,
        result=result,
        failure=failure,
    )


def _serialized_rejection_counts(result: JobResultSummary) -> Jsonb:
    counts = {item.code: item.count for item in result.rejection_code_counts}
    if result.unclassified_rejection_count:
        counts[UNCLASSIFIED_REJECTION_COUNT_CODE] = result.unclassified_rejection_count
    return Jsonb(counts)


def _rejection_counts(
    value: object,
) -> tuple[tuple[JobRejectionCount, ...], int]:
    if not isinstance(value, Mapping):
        raise ValueError("background job rejection counts are invalid")
    counts: list[JobRejectionCount] = []
    unclassified_count = 0
    for code, count in sorted(value.items(), key=lambda item: str(item[0])):
        if not isinstance(code, str):
            raise ValueError("background job rejection code is invalid")
        if code == UNCLASSIFIED_REJECTION_COUNT_CODE:
            unclassified_count = _required_int(count)
            if not 1 <= unclassified_count <= MAX_REJECTED_SOURCE_TOTAL:
                raise ValueError("background job unclassified rejection count is invalid")
            continue
        counts.append(JobRejectionCount(code=code, count=_required_int(count)))
    return tuple(counts), unclassified_count


def _database_now(connection: psycopg.Connection[Any]) -> datetime:
    row = connection.execute("SELECT clock_timestamp()").fetchone()
    if row is None or len(row) != 1:
        raise ValueError("database time response is invalid")
    return _required_datetime(row[0])


def _bounded_duration(
    value: timedelta,
    *,
    minimum: timedelta,
    maximum: timedelta,
    label: str,
) -> timedelta:
    if not isinstance(value, timedelta) or not minimum <= value <= maximum:
        raise ValueError(f"{label} is outside its bounded range")
    return value


def _bounded_identifier(value: str, label: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not 3 <= len(value) <= maximum
        or not value[0].isalnum()
        or not value.replace("_", "").replace("-", "").isalnum()
        or not value.isascii()
        or value.lower() != value
    ):
        raise ValueError(f"{label} is invalid")
    return value


def _required_str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("background job string column is invalid")
    return value


def _required_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("background job integer column is invalid")
    return value


def _required_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("background job boolean column is invalid")
    return value


def _required_datetime(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError("background job timestamp column is invalid")
    return value


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    return _required_datetime(value)


def _transition_error(error: JobTransitionError) -> JobStoreError:
    if error.code in {
        JobTransitionErrorCode.LEASE_MISMATCH,
        JobTransitionErrorCode.LEASE_EXPIRED,
        JobTransitionErrorCode.FENCING_MISMATCH,
    }:
        return JobStoreError(
            JobStoreErrorCode.LEASE_CONFLICT,
            "background job lease is no longer current",
        )
    return JobStoreError(
        JobStoreErrorCode.STATE_CONFLICT,
        "background job state no longer permits this operation",
    )


def _schema_mismatch() -> JobStoreError:
    return JobStoreError(
        JobStoreErrorCode.SCHEMA_MISMATCH,
        "background job schema is not current",
    )


def _invalid_response(message: str) -> JobStoreError:
    return JobStoreError(JobStoreErrorCode.INVALID_RESPONSE, message)


def _store_unavailable(message: str) -> JobStoreError:
    return JobStoreError(JobStoreErrorCode.STORE_UNAVAILABLE, message)
