"""Durable, role-separated PostgreSQL store for governed registry publication."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg import sql
from psycopg.errors import CheckViolation, ForeignKeyViolation, UndefinedColumn, UndefinedTable
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from schemabridge.adapters.storage.postgres import ControlConnectionProvider, _ControlDatabase
from schemabridge.application.ports.registry_publication import (
    RegistryPublicationJobMutation,
    RegistryPublicationStoreError,
    RegistryPublicationStoreErrorCode,
)
from schemabridge.domain.registry_publication import (
    PublicationReadbackReceipt,
    PublishableRegistryVersion,
    RegistryPublicationAuthorization,
)
from schemabridge.domain.registry_publication_jobs import (
    RegistryPublicationEvent,
    RegistryPublicationEventKind,
    RegistryPublicationFailureCode,
    RegistryPublicationJob,
    RegistryPublicationJobStatus,
    RegistryPublicationTransitionError,
    acknowledge_registry_publication_cancellation,
    authorize_registry_publication_job,
    complete_registry_publication_job,
    create_registry_publication_job,
    fail_registry_publication_job,
    heartbeat_registry_publication_job,
    lease_registry_publication_job,
    reap_expired_registry_publication_lease,
    record_registry_publication_candidate,
    request_registry_publication_cancellation,
)
from schemabridge.domain.semantic_onboarding import PreparedSemanticOnboardingProposal

_MAX_BATCH = 1_000
_MIN_LEASE = timedelta(seconds=1)
_MAX_LEASE = timedelta(minutes=5)

_ROW_NAMES = (
    "job_id",
    "workspace_id",
    "catalog_scope",
    "registry_id",
    "target_version",
    "proposal_id",
    "proposal_fingerprint",
    "submitted_by",
    "idempotency_digest",
    "request_fingerprint",
    "status",
    "revision",
    "attempt_count",
    "max_attempts",
    "fencing_token",
    "available_at",
    "lease_owner_id",
    "lease_capability_digest",
    "lease_acquired_at",
    "lease_heartbeat_at",
    "lease_expires_at",
    "candidate_fingerprint",
    "authorization_id",
    "observed_authorization_id",
    "failure_code",
    "cancel_requested_at",
    "payload",
    "submitted_at",
    "updated_at",
    "completed_at",
)
_ROW_COLUMNS = sql.SQL(", ").join(sql.Identifier(name) for name in _ROW_NAMES)


@dataclass(frozen=True, slots=True)
class PostgresRegistryPublicationProposalReader:
    """Load only an exact M33 proposal whose draft remains publication-ready."""

    dsn: str = field(repr=False)
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-api"
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

    def load_exact(
        self,
        workspace_id: str,
        proposal_id: str,
    ) -> PreparedSemanticOnboardingProposal | None:
        proposals = self._database.table("semantic_onboarding_proposals")
        drafts = self._database.table("semantic_onboarding_drafts")
        try:
            with self._database.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT proposal.payload,
                               proposal.fingerprint,
                               proposal.target_registry_version,
                               draft.status,
                               draft.prepared_proposal_id,
                               draft.prepared_proposal_fingerprint
                        FROM {proposals} AS proposal
                        JOIN {drafts} AS draft
                          ON draft.workspace_id = proposal.workspace_id
                         AND draft.draft_id = proposal.draft_id
                        WHERE proposal.workspace_id = %s
                          AND proposal.proposal_id = %s
                        """
                    ).format(proposals=proposals, drafts=drafts),
                    (workspace_id, proposal_id),
                ).fetchone()
            if row is None:
                return None
            proposal = PreparedSemanticOnboardingProposal.model_validate(row[0])
            if (
                proposal.workspace_id != workspace_id
                or proposal.id != proposal_id
                or proposal.fingerprint != str(row[1])
                or proposal.target_registry_version != int(row[2])
                or str(row[3]) != "ready_for_publication"
                or str(row[4]) != proposal.id
                or str(row[5]) != proposal.fingerprint
            ):
                raise _invalid_response()
            return proposal
        except RegistryPublicationStoreError:
            raise
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except psycopg.Error as error:
            raise _unavailable() from error


@dataclass(frozen=True, slots=True)
class PostgresRegistryPublicationJobStore:
    """API and publisher store; PostgreSQL roles constrain each callable transition."""

    dsn: str = field(repr=False)
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-api"
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

    def submit(self, job: RegistryPublicationJob) -> RegistryPublicationJobMutation:
        if job.status is not RegistryPublicationJobStatus.QUEUED:
            raise ValueError("only queued registry publication jobs may be submitted")
        table = self._database.table("registry_publication_jobs")
        try:
            with self._database.connect() as connection, connection.transaction():
                submitted_at = _database_now(connection)
                submitted = create_registry_publication_job(
                    job.proposal,
                    submitted_by=job.submitted_by,
                    submitted_at=submitted_at,
                    idempotency_digest=job.idempotency_digest,
                    request_fingerprint=job.request_fingerprint,
                    max_attempts=job.max_attempts,
                )
                row = connection.execute(
                    sql.SQL(
                        """
                        INSERT INTO {table} (
                            job_id, workspace_id, catalog_scope, registry_id,
                            target_version, proposal_id, proposal_fingerprint,
                            submitted_by, idempotency_digest, request_fingerprint,
                            status, revision, attempt_count, max_attempts,
                            fencing_token, available_at, payload, submitted_at, updated_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        ON CONFLICT DO NOTHING
                        RETURNING {columns}
                        """
                    ).format(table=table, columns=_ROW_COLUMNS),
                    _insert_params(submitted),
                ).fetchone()
                if row is not None:
                    persisted = _job_from_row(row)
                    self._append_event(
                        connection,
                        persisted,
                        RegistryPublicationEventKind.SUBMITTED,
                        actor_id=persisted.submitted_by,
                    )
                    return RegistryPublicationJobMutation(job=persisted)
                replay_row = connection.execute(
                    sql.SQL(
                        """
                        SELECT {columns} FROM {table}
                        WHERE workspace_id = %s
                          AND submitted_by = %s
                          AND idempotency_digest = %s
                        FOR SHARE
                        """
                    ).format(columns=_ROW_COLUMNS, table=table),
                    (
                        submitted.scope.workspace_id,
                        submitted.submitted_by,
                        submitted.idempotency_digest,
                    ),
                ).fetchone()
                if replay_row is not None:
                    replay = _job_from_row(replay_row)
                    if replay.request_fingerprint != submitted.request_fingerprint:
                        raise RegistryPublicationStoreError(
                            RegistryPublicationStoreErrorCode.IDEMPOTENCY_CONFLICT,
                            "registry publication idempotency key already identifies another request",
                        )
                    return RegistryPublicationJobMutation(job=replay, replayed=True)
                target_row = connection.execute(
                    sql.SQL(
                        """
                        SELECT 1 FROM {table}
                        WHERE workspace_id = %s AND catalog_scope = %s
                          AND registry_id = %s AND target_version = %s
                        """
                    ).format(table=table),
                    (
                        submitted.scope.workspace_id,
                        submitted.scope.catalog_scope,
                        submitted.scope.registry_id,
                        submitted.proposal.target_registry_version,
                    ),
                ).fetchone()
                if target_row is not None:
                    raise RegistryPublicationStoreError(
                        RegistryPublicationStoreErrorCode.TARGET_RESERVED,
                        "registry publication target is already reserved",
                    )
                raise _state_conflict()
        except RegistryPublicationStoreError:
            raise
        except (CheckViolation, ForeignKeyViolation) as error:
            raise _state_conflict() from error
        except (
            ValidationError,
            RegistryPublicationTransitionError,
            TypeError,
            ValueError,
        ) as error:
            raise _invalid_response() from error
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def load(self, workspace_id: str, job_id: str) -> RegistryPublicationJob | None:
        return self._load(workspace_id=workspace_id, job_id=job_id)

    def load_by_idempotency(
        self,
        workspace_id: str,
        submitted_by: str,
        idempotency_digest: str,
    ) -> RegistryPublicationJob | None:
        return self._load(
            workspace_id=workspace_id,
            submitted_by=submitted_by,
            idempotency_digest=idempotency_digest,
        )

    def authorize(
        self,
        job_id: str,
        *,
        workspace_id: str,
        expected_revision: int,
        authorization: RegistryPublicationAuthorization,
    ) -> RegistryPublicationJob:
        def transition(current: RegistryPublicationJob, now: datetime) -> RegistryPublicationJob:
            if current.revision != expected_revision:
                raise _state_conflict()
            return authorize_registry_publication_job(
                current,
                authorization,
                authorized_at=now,
            )

        return self._api_transition(
            workspace_id,
            job_id,
            transition,
            event=RegistryPublicationEventKind.AUTHORIZED,
            actor_id=authorization.actor_id,
        )

    def request_cancellation(
        self,
        job_id: str,
        *,
        workspace_id: str,
        expected_revision: int,
        requested_by: str,
    ) -> RegistryPublicationJob:
        def transition(current: RegistryPublicationJob, now: datetime) -> RegistryPublicationJob:
            if current.revision != expected_revision:
                raise _state_conflict()
            return request_registry_publication_cancellation(current, requested_at=now)

        current = self.load(workspace_id, job_id)
        if current is not None and (
            current.status.is_terminal
            or current.status is RegistryPublicationJobStatus.CANCEL_REQUESTED
            or (
                current.status is RegistryPublicationJobStatus.RETRY_WAIT
                and current.cancel_requested_at is not None
            )
        ):
            return current
        return self._api_transition(
            workspace_id,
            job_id,
            transition,
            event=None,
            actor_id=requested_by,
        )

    def claim_next(
        self,
        *,
        worker_id: str,
        lease_capability: str,
        lease_duration: timedelta,
    ) -> RegistryPublicationJob | None:
        _bounded_lease(lease_duration)
        table = self._database.table("registry_publication_jobs")
        try:
            with self._database.connect() as connection, connection.transaction():
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT {columns} FROM {table}
                        WHERE status IN ('queued', 'approved', 'retry_wait')
                          AND available_at <= clock_timestamp()
                          AND attempt_count < max_attempts
                        ORDER BY available_at, submitted_at, job_id
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                        """
                    ).format(columns=_ROW_COLUMNS, table=table)
                ).fetchone()
                if row is None:
                    return None
                current = _job_from_row(row)
                changed = lease_registry_publication_job(
                    current,
                    worker_id=worker_id,
                    lease_capability=lease_capability,
                    acquired_at=_database_now(connection),
                    lease_duration=lease_duration,
                )
                self._write_job(connection, current, changed)
                self._append_event(
                    connection,
                    changed,
                    (
                        RegistryPublicationEventKind.RECOVERY_LEASED
                        if changed.status is RegistryPublicationJobStatus.CANCEL_REQUESTED
                        else RegistryPublicationEventKind.LEASED
                    ),
                    worker_id=worker_id,
                )
                return changed
        except RegistryPublicationStoreError:
            raise
        except (
            ValidationError,
            RegistryPublicationTransitionError,
            TypeError,
            ValueError,
        ) as error:
            raise _invalid_response() from error
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def heartbeat(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
        lease_duration: timedelta,
    ) -> RegistryPublicationJob:
        _bounded_lease(lease_duration)

        def transition(current: RegistryPublicationJob, now: datetime) -> RegistryPublicationJob:
            return heartbeat_registry_publication_job(
                current,
                worker_id=worker_id,
                lease_capability=lease_capability,
                fencing_token=fencing_token,
                heartbeat_at=now,
                lease_duration=lease_duration,
            )

        return self._worker_transition(
            job_id,
            transition,
            worker_id=worker_id,
            emit_event=False,
        )

    def record_candidate(
        self,
        job_id: str,
        candidate: PublishableRegistryVersion,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
    ) -> RegistryPublicationJob:
        def transition(current: RegistryPublicationJob, now: datetime) -> RegistryPublicationJob:
            return record_registry_publication_candidate(
                current,
                candidate,
                worker_id=worker_id,
                lease_capability=lease_capability,
                fencing_token=fencing_token,
                completed_at=now,
            )

        return self._worker_transition(
            job_id,
            transition,
            worker_id=worker_id,
            event=RegistryPublicationEventKind.CANDIDATE_READY,
        )

    def complete(
        self,
        job_id: str,
        receipt: PublicationReadbackReceipt,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
    ) -> RegistryPublicationJob:
        def transition(current: RegistryPublicationJob, now: datetime) -> RegistryPublicationJob:
            return complete_registry_publication_job(
                current,
                receipt,
                worker_id=worker_id,
                lease_capability=lease_capability,
                fencing_token=fencing_token,
                completed_at=now,
            )

        return self._worker_transition(
            job_id,
            transition,
            worker_id=worker_id,
            event=RegistryPublicationEventKind.ACTIVATION_READY,
        )

    def fail(
        self,
        job_id: str,
        code: RegistryPublicationFailureCode,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
    ) -> RegistryPublicationJob:
        def transition(current: RegistryPublicationJob, now: datetime) -> RegistryPublicationJob:
            return fail_registry_publication_job(
                current,
                code,
                worker_id=worker_id,
                lease_capability=lease_capability,
                fencing_token=fencing_token,
                failed_at=now,
            )

        return self._worker_transition(job_id, transition, worker_id=worker_id, event=None)

    def acknowledge_cancellation(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
    ) -> RegistryPublicationJob:
        def transition(current: RegistryPublicationJob, now: datetime) -> RegistryPublicationJob:
            return acknowledge_registry_publication_cancellation(
                current,
                worker_id=worker_id,
                lease_capability=lease_capability,
                fencing_token=fencing_token,
                cancelled_at=now,
            )

        return self._worker_transition(
            job_id,
            transition,
            worker_id=worker_id,
            event=RegistryPublicationEventKind.CANCELLED,
        )

    def reap_expired_leases(self, *, limit: int = 100) -> int:
        if isinstance(limit, bool) or not 1 <= limit <= _MAX_BATCH:
            raise ValueError("registry publication reap limit is invalid")
        table = self._database.table("registry_publication_jobs")
        try:
            with self._database.connect() as connection, connection.transaction():
                rows = connection.execute(
                    sql.SQL(
                        """
                        SELECT {columns} FROM {table}
                        WHERE status IN ('leased', 'cancel_requested')
                          AND lease_expires_at <= clock_timestamp()
                        ORDER BY lease_expires_at, submitted_at, job_id
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                        """
                    ).format(columns=_ROW_COLUMNS, table=table),
                    (limit,),
                ).fetchall()
                now = _database_now(connection)
                for row in rows:
                    current = _job_from_row(row)
                    changed = reap_expired_registry_publication_lease(
                        current,
                        expired_at=now,
                    )
                    self._write_job(connection, current, changed)
                    self._append_event(
                        connection,
                        changed,
                        _event_for_job(changed, authorization_expired=False),
                        worker_id="publisher-reaper",
                    )
                return len(rows)
        except RegistryPublicationStoreError:
            raise
        except (
            ValidationError,
            RegistryPublicationTransitionError,
            TypeError,
            ValueError,
        ) as error:
            raise _invalid_response() from error
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def _load(
        self,
        *,
        workspace_id: str,
        job_id: str | None = None,
        submitted_by: str | None = None,
        idempotency_digest: str | None = None,
    ) -> RegistryPublicationJob | None:
        table = self._database.table("registry_publication_jobs")
        if job_id is not None:
            predicate = sql.SQL("workspace_id = %s AND job_id = %s")
            params: tuple[object, ...] = (workspace_id, job_id)
        else:
            predicate = sql.SQL(
                "workspace_id = %s AND submitted_by = %s AND idempotency_digest = %s"
            )
            params = (workspace_id, submitted_by, idempotency_digest)
        try:
            with self._database.connect() as connection:
                row = connection.execute(
                    sql.SQL("SELECT {columns} FROM {table} WHERE ").format(
                        columns=_ROW_COLUMNS,
                        table=table,
                    )
                    + predicate,
                    params,
                ).fetchone()
            return None if row is None else _job_from_row(row)
        except RegistryPublicationStoreError:
            raise
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def _api_transition(
        self,
        workspace_id: str,
        job_id: str,
        transition: Callable[[RegistryPublicationJob, datetime], RegistryPublicationJob],
        *,
        event: RegistryPublicationEventKind | None,
        actor_id: str,
    ) -> RegistryPublicationJob:
        try:
            with self._database.connect() as connection, connection.transaction():
                current = self._load_for_update(connection, job_id, workspace_id=workspace_id)
                changed = transition(current, _database_now(connection))
                if changed == current:
                    return current
                self._write_api_job(connection, current, changed)
                inferred_event = event
                if inferred_event is None:
                    inferred_event = (
                        RegistryPublicationEventKind.CANCEL_REQUESTED
                        if changed.status is RegistryPublicationJobStatus.CANCEL_REQUESTED
                        else RegistryPublicationEventKind.CANCELLED
                    )
                self._append_event(
                    connection,
                    changed,
                    inferred_event,
                    actor_id=actor_id,
                )
                return changed
        except RegistryPublicationStoreError:
            raise
        except RegistryPublicationTransitionError as error:
            raise _state_conflict() from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def _worker_transition(
        self,
        job_id: str,
        transition: Callable[[RegistryPublicationJob, datetime], RegistryPublicationJob],
        *,
        worker_id: str,
        event: RegistryPublicationEventKind | None = None,
        emit_event: bool = True,
    ) -> RegistryPublicationJob:
        try:
            with self._database.connect() as connection, connection.transaction():
                current = self._load_for_update(connection, job_id)
                changed = transition(current, _database_now(connection))
                self._write_job(connection, current, changed)
                authorization_expired = (
                    current.authorization is not None
                    and changed.status is RegistryPublicationJobStatus.AWAITING_APPROVAL
                    and changed.authorization is None
                )
                if emit_event:
                    self._append_event(
                        connection,
                        changed,
                        event
                        or _event_for_job(
                            changed,
                            authorization_expired=authorization_expired,
                        ),
                        worker_id=worker_id,
                    )
                return changed
        except RegistryPublicationStoreError:
            raise
        except RegistryPublicationTransitionError as error:
            raise RegistryPublicationStoreError(
                RegistryPublicationStoreErrorCode.LEASE_CONFLICT,
                "registry publication lease changed",
            ) from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def _load_for_update(
        self,
        connection: psycopg.Connection[Any],
        job_id: str,
        *,
        workspace_id: str | None = None,
    ) -> RegistryPublicationJob:
        table = self._database.table("registry_publication_jobs")
        workspace_clause = (
            sql.SQL("") if workspace_id is None else sql.SQL(" AND workspace_id = %s")
        )
        params: tuple[object, ...] = (job_id,)
        if workspace_id is not None:
            params = (*params, workspace_id)
        row = connection.execute(
            sql.SQL("SELECT {columns} FROM {table} WHERE job_id = %s").format(
                columns=_ROW_COLUMNS,
                table=table,
            )
            + workspace_clause
            + sql.SQL(" FOR UPDATE"),
            params,
        ).fetchone()
        if row is None:
            raise RegistryPublicationStoreError(
                RegistryPublicationStoreErrorCode.NOT_FOUND,
                "registry publication job is unavailable",
            )
        return _job_from_row(row)

    def _write_job(
        self,
        connection: psycopg.Connection[Any],
        current: RegistryPublicationJob,
        changed: RegistryPublicationJob,
    ) -> None:
        table = self._database.table("registry_publication_jobs")
        lease = changed.lease
        completed_at = changed.updated_at if changed.status.is_terminal else None
        updated = connection.execute(
            sql.SQL(
                """
                UPDATE {table}
                SET status = %s, revision = %s, attempt_count = %s,
                    fencing_token = %s, available_at = %s,
                    lease_owner_id = %s, lease_capability_digest = %s,
                    lease_acquired_at = %s, lease_heartbeat_at = %s,
                    lease_expires_at = %s, candidate_fingerprint = %s,
                    authorization_id = %s, observed_authorization_id = %s,
                    failure_code = %s, cancel_requested_at = %s,
                    payload = %s, updated_at = %s, completed_at = %s
                WHERE workspace_id = %s AND job_id = %s AND revision = %s
                """
            ).format(table=table),
            (
                changed.status.value,
                changed.revision,
                changed.attempts,
                changed.last_fencing_token,
                changed.available_at,
                None if lease is None else lease.worker_id,
                None if lease is None else lease.token_digest,
                None if lease is None else lease.acquired_at,
                None if lease is None else lease.heartbeat_at,
                None if lease is None else lease.expires_at,
                None if changed.candidate is None else changed.candidate.fingerprint,
                None if changed.authorization is None else changed.authorization.id,
                None if changed.receipt is None else changed.receipt.observed_authorization_id,
                None if changed.failure_code is None else changed.failure_code.value,
                changed.cancel_requested_at,
                Jsonb(changed.model_dump(mode="json")),
                changed.updated_at,
                completed_at,
                current.scope.workspace_id,
                current.id,
                current.revision,
            ),
        )
        if updated.rowcount != 1:
            raise _state_conflict()

    def _write_api_job(
        self,
        connection: psycopg.Connection[Any],
        current: RegistryPublicationJob,
        changed: RegistryPublicationJob,
    ) -> None:
        table = self._database.table("registry_publication_jobs")
        completed_at = changed.updated_at if changed.status.is_terminal else None
        updated = connection.execute(
            sql.SQL(
                """
                UPDATE {table}
                SET status = %s, revision = %s, available_at = %s,
                    authorization_id = %s, failure_code = %s,
                    cancel_requested_at = %s, payload = %s,
                    updated_at = %s, completed_at = %s
                WHERE workspace_id = %s AND job_id = %s AND revision = %s
                """
            ).format(table=table),
            (
                changed.status.value,
                changed.revision,
                changed.available_at,
                None if changed.authorization is None else changed.authorization.id,
                None if changed.failure_code is None else changed.failure_code.value,
                changed.cancel_requested_at,
                Jsonb(changed.model_dump(mode="json")),
                changed.updated_at,
                completed_at,
                current.scope.workspace_id,
                current.id,
                current.revision,
            ),
        )
        if updated.rowcount != 1:
            raise _state_conflict()

    def _append_event(
        self,
        connection: psycopg.Connection[Any],
        job: RegistryPublicationJob,
        kind: RegistryPublicationEventKind,
        *,
        actor_id: str | None = None,
        worker_id: str | None = None,
    ) -> None:
        event = RegistryPublicationEvent(
            id=_event_id(job, kind),
            workspace_id=job.scope.workspace_id,
            job_id=job.id,
            revision=job.revision,
            kind=kind,
            status=job.status,
            actor_id=actor_id,
            worker_id=worker_id,
            failure_code=job.failure_code,
            occurred_at=job.updated_at,
        )
        table = self._database.table("registry_publication_events")
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {table} (
                    event_id, workspace_id, job_id, revision, event_kind,
                    status, actor_id, worker_id, failure_code, occurred_at, payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
            ).format(table=table),
            (
                event.id,
                event.workspace_id,
                event.job_id,
                event.revision,
                event.kind.value,
                event.status.value,
                event.actor_id,
                event.worker_id,
                None if event.failure_code is None else event.failure_code.value,
                event.occurred_at,
                Jsonb(event.model_dump(mode="json")),
            ),
        )


def _insert_params(job: RegistryPublicationJob) -> tuple[object, ...]:
    return (
        job.id,
        job.scope.workspace_id,
        job.scope.catalog_scope,
        job.scope.registry_id,
        job.proposal.target_registry_version,
        job.proposal.id,
        job.proposal.fingerprint,
        job.submitted_by,
        job.idempotency_digest,
        job.request_fingerprint,
        job.status.value,
        job.revision,
        job.attempts,
        job.max_attempts,
        job.last_fencing_token,
        job.available_at,
        Jsonb(job.model_dump(mode="json")),
        job.submitted_at,
        job.updated_at,
    )


def _job_from_row(row: tuple[object, ...]) -> RegistryPublicationJob:
    values = dict(zip(_ROW_NAMES, row, strict=True))
    job = RegistryPublicationJob.model_validate(values["payload"])
    lease = job.lease
    completed_at = job.updated_at if job.status.is_terminal else None
    expected: dict[str, object] = {
        "job_id": job.id,
        "workspace_id": job.scope.workspace_id,
        "catalog_scope": job.scope.catalog_scope,
        "registry_id": job.scope.registry_id,
        "target_version": job.proposal.target_registry_version,
        "proposal_id": job.proposal.id,
        "proposal_fingerprint": job.proposal.fingerprint,
        "submitted_by": job.submitted_by,
        "idempotency_digest": job.idempotency_digest,
        "request_fingerprint": job.request_fingerprint,
        "status": job.status.value,
        "revision": job.revision,
        "attempt_count": job.attempts,
        "max_attempts": job.max_attempts,
        "fencing_token": job.last_fencing_token,
        "available_at": job.available_at,
        "lease_owner_id": None if lease is None else lease.worker_id,
        "lease_capability_digest": None if lease is None else lease.token_digest,
        "lease_acquired_at": None if lease is None else lease.acquired_at,
        "lease_heartbeat_at": None if lease is None else lease.heartbeat_at,
        "lease_expires_at": None if lease is None else lease.expires_at,
        "candidate_fingerprint": None if job.candidate is None else job.candidate.fingerprint,
        "authorization_id": None if job.authorization is None else job.authorization.id,
        "observed_authorization_id": (
            None if job.receipt is None else job.receipt.observed_authorization_id
        ),
        "failure_code": None if job.failure_code is None else job.failure_code.value,
        "cancel_requested_at": job.cancel_requested_at,
        "submitted_at": job.submitted_at,
        "updated_at": job.updated_at,
        "completed_at": completed_at,
    }
    if any(values[name] != value for name, value in expected.items()):
        raise _invalid_response()
    return job


def _event_for_job(
    job: RegistryPublicationJob,
    *,
    authorization_expired: bool,
) -> RegistryPublicationEventKind:
    if authorization_expired:
        return RegistryPublicationEventKind.AUTHORIZATION_EXPIRED
    return {
        RegistryPublicationJobStatus.LEASED: RegistryPublicationEventKind.LEASED,
        RegistryPublicationJobStatus.CANCEL_REQUESTED: (
            RegistryPublicationEventKind.RECOVERY_LEASED
        ),
        RegistryPublicationJobStatus.AWAITING_APPROVAL: (
            RegistryPublicationEventKind.CANDIDATE_READY
        ),
        RegistryPublicationJobStatus.APPROVED: RegistryPublicationEventKind.AUTHORIZED,
        RegistryPublicationJobStatus.RETRY_WAIT: RegistryPublicationEventKind.RETRY_SCHEDULED,
        RegistryPublicationJobStatus.ACTIVATION_READY: (
            RegistryPublicationEventKind.ACTIVATION_READY
        ),
        RegistryPublicationJobStatus.FAILED: RegistryPublicationEventKind.FAILED,
        RegistryPublicationJobStatus.CANCELLED: RegistryPublicationEventKind.CANCELLED,
        RegistryPublicationJobStatus.DEAD_LETTERED: (RegistryPublicationEventKind.DEAD_LETTERED),
    }[job.status]


def _event_id(job: RegistryPublicationJob, kind: RegistryPublicationEventKind) -> str:
    payload = json.dumps(
        {
            "contract": "registry_publication_event_v1",
            "job_id": job.id,
            "revision": job.revision,
            "kind": kind.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"registry-publication-event-{hashlib.sha256(payload).hexdigest()}"


def _database_now(connection: psycopg.Connection[Any]) -> datetime:
    row = connection.execute("SELECT clock_timestamp()").fetchone()
    if row is None or not isinstance(row[0], datetime):
        raise _invalid_response()
    return row[0]


def _bounded_lease(value: timedelta) -> None:
    if not _MIN_LEASE <= value <= _MAX_LEASE:
        raise ValueError("registry publication lease duration is invalid")


def _state_conflict() -> RegistryPublicationStoreError:
    return RegistryPublicationStoreError(
        RegistryPublicationStoreErrorCode.STATE_CONFLICT,
        "registry publication state changed",
    )


def _invalid_response() -> RegistryPublicationStoreError:
    return RegistryPublicationStoreError(
        RegistryPublicationStoreErrorCode.INVALID_RESPONSE,
        "registry publication store returned invalid state",
    )


def _schema_mismatch() -> RegistryPublicationStoreError:
    return RegistryPublicationStoreError(
        RegistryPublicationStoreErrorCode.SCHEMA_MISMATCH,
        "registry publication schema is unavailable",
    )


def _unavailable() -> RegistryPublicationStoreError:
    return RegistryPublicationStoreError(
        RegistryPublicationStoreErrorCode.UNAVAILABLE,
        "registry publication store is unavailable",
    )


__all__ = [
    "PostgresRegistryPublicationJobStore",
    "PostgresRegistryPublicationProposalReader",
]
