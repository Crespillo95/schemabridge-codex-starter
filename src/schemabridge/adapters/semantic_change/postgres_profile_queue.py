"""Durable control-plane queue for aggregate-only semantic join profiles."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg import sql
from psycopg.errors import (
    CheckViolation,
    ForeignKeyViolation,
    UndefinedColumn,
    UndefinedFunction,
    UndefinedTable,
    UniqueViolation,
)
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from schemabridge.adapters.storage.postgres import (
    ControlConnectionProvider,
    _ControlDatabase,
)
from schemabridge.application.connectors import ConnectorTargetError
from schemabridge.application.ports.connectors import ExecutionTargetResolverPort
from schemabridge.application.ports.relationships import (
    RelationshipErrorCode,
    RelationshipWorkflowError,
)
from schemabridge.application.ports.semantic_profile_jobs import (
    SemanticJoinProfileQueueError,
    SemanticJoinProfileQueueErrorCode,
    SemanticJoinProfileQueuePort,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.joins import JoinProposal, RelationshipProfile
from schemabridge.domain.semantic_profile_jobs import (
    SemanticJoinProfileFailure,
    SemanticJoinProfileFailureCode,
    SemanticJoinProfileFailureDisposition,
    SemanticJoinProfileJob,
    SemanticJoinProfileJobStatus,
    SemanticJoinProfileLease,
    SemanticJoinProfileProposal,
    SemanticJoinProfileResult,
    SemanticJoinProfileSubmission,
    SemanticJoinProfileTargetRef,
    SemanticJoinProfileTransitionError,
    claim_semantic_join_profile_job,
    complete_semantic_join_profile_job,
    digest_semantic_join_profile_capability,
    fail_semantic_join_profile_job,
    heartbeat_semantic_join_profile_job,
    reclaim_expired_semantic_join_profile_job,
    semantic_join_profile_proposal_fingerprint,
    validate_semantic_join_profile_proposal,
)

_MIN_LEASE_DURATION = timedelta(seconds=10)
_MAX_LEASE_DURATION = timedelta(minutes=5)
_MIN_RETENTION = timedelta(days=1)
_MAX_RETENTION = timedelta(days=366)
_MAX_MAINTENANCE_BATCH = 1_000

_JOB_COLUMN_NAMES = (
    "job_id",
    "workspace_id",
    "scan_id",
    "connection_id",
    "connector_contract_version",
    "connector_route_revision",
    "connector_route_fingerprint",
    "connector_target_fingerprint",
    "proposal_fingerprint",
    "proposal_json",
    "status",
    "attempt_count",
    "max_attempts",
    "available_at",
    "lease_owner_id",
    "lease_capability_digest",
    "fencing_token",
    "lease_acquired_at",
    "lease_heartbeat_at",
    "lease_expires_at",
    "failure_code",
    "result_profile_json",
    "result_profile_fingerprint",
    "requested_at",
    "updated_at",
    "completed_at",
    "retain_until",
)
_JOB_COLUMNS = sql.SQL(", ").join(sql.Identifier(name) for name in _JOB_COLUMN_NAMES)


@dataclass(frozen=True, slots=True)
class PostgresSemanticJoinProfileQueue:
    """PostgreSQL implementation shared by narrow reconciler and worker grants."""

    dsn: str = field(repr=False)
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-reconciler"
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

    def enqueue(
        self,
        workspace_id: str,
        scan_id: str,
        proposal: SemanticJoinProfileProposal,
        *,
        execution_target: SemanticJoinProfileTargetRef,
        requested_at: datetime,
        max_attempts: int = 5,
    ) -> SemanticJoinProfileSubmission:
        """Insert one exact request; PostgreSQL enforces the per-scan queue bound."""

        _aware(requested_at, "semantic profile requested time")
        checked_proposal = validate_semantic_join_profile_proposal(proposal)
        checked_target = SemanticJoinProfileTargetRef.model_validate(
            execution_target.model_dump(mode="python")
        )
        if (
            checked_target.workspace_id != workspace_id
            or checked_target.connection_id != checked_proposal.connection_id
        ):
            raise ValueError("semantic profile connector target does not match its exact scope")
        if not 1 <= max_attempts <= 100:
            raise ValueError("semantic profile max attempts is outside the supported bound")
        table = self._database.table("semantic_join_profile_jobs")
        try:
            with self._database.connect() as connection:
                database_time = _database_now(connection)
                requested = SemanticJoinProfileJob.requested(
                    workspace_id=workspace_id,
                    scan_id=scan_id,
                    proposal=checked_proposal,
                    execution_target=checked_target,
                    requested_at=database_time,
                    max_attempts=max_attempts,
                )
                row = connection.execute(
                    sql.SQL(
                        """
                        INSERT INTO {table} (
                            job_id, workspace_id, scan_id, connection_id,
                            connector_route_revision,
                            connector_route_fingerprint,
                            connector_target_fingerprint,
                            proposal_fingerprint, proposal_json, status,
                            attempt_count, max_attempts, available_at,
                            fencing_token, requested_at, updated_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s, %s, %s, %s
                        )
                        ON CONFLICT DO NOTHING
                        RETURNING {columns}
                        """
                    ).format(table=table, columns=_JOB_COLUMNS),
                    (
                        requested.job_id,
                        requested.workspace_id,
                        requested.scan_id,
                        requested.connection_id.root,
                        checked_target.route_revision,
                        checked_target.route_fingerprint,
                        checked_target.target_fingerprint,
                        requested.proposal_fingerprint,
                        Jsonb(requested.proposal.model_dump(mode="json")),
                        requested.status.value,
                        requested.attempts,
                        requested.max_attempts,
                        requested.available_at,
                        requested.fencing_token,
                        requested.requested_at,
                        requested.updated_at,
                    ),
                ).fetchone()
                if row is not None:
                    persisted = _profile_job_from_row(row)
                    if (
                        not _same_profile_request(persisted, requested)
                        or persisted.connector_contract_version is None
                    ):
                        raise _invalid_response("semantic profile insertion returned another job")
                    return SemanticJoinProfileSubmission(
                        job=persisted,
                        replayed=False,
                    )
                replay_row = connection.execute(
                    sql.SQL(
                        """
                        SELECT {columns}
                        FROM {table}
                        WHERE workspace_id = %s
                          AND scan_id = %s
                          AND proposal_fingerprint = %s
                        """
                    ).format(columns=_JOB_COLUMNS, table=table),
                    (
                        workspace_id,
                        scan_id,
                        requested.proposal_fingerprint,
                    ),
                ).fetchone()
                if replay_row is None:
                    raise SemanticJoinProfileQueueError(
                        SemanticJoinProfileQueueErrorCode.STATE_CONFLICT,
                        "semantic profile request conflicted",
                    )
                replayed = _profile_job_from_row(replay_row)
                if (
                    replayed.job_id != requested.job_id
                    or replayed.workspace_id != requested.workspace_id
                    or replayed.scan_id != requested.scan_id
                    or replayed.connection_id != requested.connection_id
                    or replayed.execution_target != requested.execution_target
                    or replayed.connector_contract_version is None
                    or replayed.proposal != requested.proposal
                    or replayed.proposal_fingerprint != requested.proposal_fingerprint
                    or replayed.max_attempts != requested.max_attempts
                ):
                    raise SemanticJoinProfileQueueError(
                        SemanticJoinProfileQueueErrorCode.IDEMPOTENCY_CONFLICT,
                        "semantic profile identity already contains different content",
                    )
                return SemanticJoinProfileSubmission(job=replayed, replayed=True)
        except SemanticJoinProfileQueueError:
            raise
        except UniqueViolation as error:
            raise SemanticJoinProfileQueueError(
                SemanticJoinProfileQueueErrorCode.IDEMPOTENCY_CONFLICT,
                "semantic profile identity already contains different content",
            ) from error
        except (CheckViolation, ForeignKeyViolation) as error:
            if error.sqlstate == "53300":
                raise _capacity_exceeded() from error
            raise SemanticJoinProfileQueueError(
                SemanticJoinProfileQueueErrorCode.STATE_CONFLICT,
                "semantic profile request no longer matches scan state",
            ) from error
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except (
            ValidationError,
            SemanticJoinProfileTransitionError,
            TypeError,
            ValueError,
        ) as error:
            raise _invalid_response("semantic profile request response was invalid") from error
        except psycopg.Error as error:
            if error.sqlstate == "53300":
                raise _capacity_exceeded() from error
            raise _store_unavailable("semantic profile request failed") from error

    def load(
        self,
        workspace_id: str,
        scan_id: str,
        proposal_fingerprint: str,
    ) -> SemanticJoinProfileJob | None:
        """Return no signal for unknown or cross-workspace identities."""

        _bounded_text(workspace_id, "semantic profile workspace", maximum_bytes=200)
        _scan_id(scan_id)
        _sha256(proposal_fingerprint, "semantic profile proposal fingerprint")
        table = self._database.table("semantic_join_profile_jobs")
        try:
            with self._database.connect() as connection, connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT {columns}
                        FROM {table}
                        WHERE workspace_id = %s
                          AND scan_id = %s
                          AND proposal_fingerprint = %s
                        """
                    ).format(columns=_JOB_COLUMNS, table=table),
                    (workspace_id, scan_id, proposal_fingerprint),
                ).fetchone()
            return None if row is None else _profile_job_from_row(row)
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response("semantic profile read response was invalid") from error
        except psycopg.Error as error:
            raise _store_unavailable("semantic profile read failed") from error

    def reclaim_expired(
        self,
        *,
        limit: int,
        retention: timedelta,
    ) -> int:
        if not 1 <= limit <= _MAX_MAINTENANCE_BATCH:
            raise ValueError("semantic profile reclaim limit is outside the supported bound")
        retained_for = _bounded_duration(
            retention,
            minimum=_MIN_RETENTION,
            maximum=_MAX_RETENTION,
            label="semantic profile retention",
        )
        table = self._database.table("semantic_join_profile_jobs")
        try:
            with self._database.connect() as connection:
                now = _database_now(connection)
                rows = connection.execute(
                    sql.SQL(
                        """
                        SELECT {columns}
                        FROM {table}
                        WHERE status = 'leased'
                          AND lease_expires_at <= %s
                          AND connector_target_fingerprint IS NOT NULL
                        ORDER BY lease_expires_at, workspace_id, connection_id, job_id
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                        """
                    ).format(columns=_JOB_COLUMNS, table=table),
                    (now, limit),
                ).fetchall()
                for row in rows:
                    current = _profile_job_from_row(row)
                    terminal_retention = (
                        now + retained_for if current.attempts >= current.max_attempts else None
                    )
                    reclaimed = reclaim_expired_semantic_join_profile_job(
                        current,
                        reclaimed_at=now,
                        retain_until=terminal_retention,
                    )
                    self._write_mutable_state(connection, current, reclaimed)
                return len(rows)
        except SemanticJoinProfileQueueError:
            raise
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except (
            ValidationError,
            SemanticJoinProfileTransitionError,
            TypeError,
            ValueError,
        ) as error:
            raise _invalid_response("semantic profile reclaim response was invalid") from error
        except psycopg.Error as error:
            raise _store_unavailable("semantic profile lease reclaim failed") from error

    def claim_next(
        self,
        *,
        worker_id: str,
        lease_capability: str,
        lease_duration: timedelta,
    ) -> SemanticJoinProfileJob | None:
        _bounded_worker_id(worker_id)
        digest_semantic_join_profile_capability(lease_capability)
        leased_for = _bounded_duration(
            lease_duration,
            minimum=_MIN_LEASE_DURATION,
            maximum=_MAX_LEASE_DURATION,
            label="semantic profile lease duration",
        )
        table = self._database.table("semantic_join_profile_jobs")
        reject_stale = self._database.table("reject_stale_registry_model_profile_jobs")
        claimable = self._database.table("registry_model_profile_job_claimable")
        try:
            with self._database.connect() as connection:
                connection.execute(
                    sql.SQL("SELECT {function}(%s)").format(function=reject_stale),
                    (100,),
                ).fetchone()
                now = _database_now(connection)
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT {columns}
                        FROM {table}
                        WHERE status IN ('requested', 'retry_wait')
                          AND available_at <= %s
                          AND attempt_count < max_attempts
                          AND connector_target_fingerprint IS NOT NULL
                          AND {claimable}(
                              workspace_id,
                              job_id,
                              scan_id,
                              proposal_fingerprint
                          )
                        ORDER BY
                            available_at,
                            requested_at,
                            workspace_id,
                            connection_id,
                            job_id
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                        """
                    ).format(
                        claimable=claimable,
                        columns=_JOB_COLUMNS,
                        table=table,
                    ),
                    (now,),
                ).fetchone()
                if row is None:
                    return None
                current = _profile_job_from_row(row)
                claimed = claim_semantic_join_profile_job(
                    current,
                    worker_id=worker_id,
                    lease_capability=lease_capability,
                    claimed_at=now,
                    lease_expires_at=now + leased_for,
                )
                self._write_mutable_state(connection, current, claimed)
                return claimed
        except SemanticJoinProfileQueueError:
            raise
        except (UndefinedTable, UndefinedColumn, UndefinedFunction) as error:
            raise _schema_mismatch() from error
        except (
            ValidationError,
            SemanticJoinProfileTransitionError,
            TypeError,
            ValueError,
        ) as error:
            raise _invalid_response("semantic profile claim response was invalid") from error
        except psycopg.Error as error:
            raise _store_unavailable("semantic profile claim failed") from error

    def heartbeat(
        self,
        workspace_id: str,
        job_id: str,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
        lease_duration: timedelta,
    ) -> SemanticJoinProfileJob:
        leased_for = _lease_inputs(
            workspace_id,
            job_id,
            worker_id,
            lease_capability,
            fencing_token,
            lease_duration,
        )
        try:
            with self._database.connect() as connection:
                current = self._load_for_update(connection, workspace_id, job_id)
                now = _database_now(connection)
                heartbeated = heartbeat_semantic_join_profile_job(
                    current,
                    worker_id=worker_id,
                    lease_capability=lease_capability,
                    fencing_token=fencing_token,
                    heartbeat_at=now,
                    lease_expires_at=now + leased_for,
                )
                self._write_mutable_state(connection, current, heartbeated)
                return heartbeated
        except SemanticJoinProfileQueueError:
            raise
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except SemanticJoinProfileTransitionError as error:
            raise _lease_conflict() from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response("semantic profile heartbeat response was invalid") from error
        except psycopg.Error as error:
            raise _store_unavailable("semantic profile heartbeat failed") from error

    def complete(
        self,
        workspace_id: str,
        job_id: str,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
        profile: RelationshipProfile,
        retention: timedelta,
    ) -> SemanticJoinProfileJob:
        _lease_identity(
            workspace_id,
            job_id,
            worker_id,
            lease_capability,
            fencing_token,
        )
        retained_for = _bounded_duration(
            retention,
            minimum=_MIN_RETENTION,
            maximum=_MAX_RETENTION,
            label="semantic profile retention",
        )
        checked_profile = RelationshipProfile.model_validate(profile.model_dump(mode="json"))
        try:
            with self._database.connect() as connection:
                current = self._load_for_update(connection, workspace_id, job_id)
                now = _database_now(connection)
                completed = complete_semantic_join_profile_job(
                    current,
                    worker_id=worker_id,
                    lease_capability=lease_capability,
                    fencing_token=fencing_token,
                    profile=checked_profile,
                    completed_at=now,
                    retain_until=now + retained_for,
                )
                self._write_mutable_state(connection, current, completed)
                return completed
        except SemanticJoinProfileQueueError:
            raise
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except SemanticJoinProfileTransitionError as error:
            raise _lease_conflict() from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response("semantic profile completion response was invalid") from error
        except psycopg.Error as error:
            raise _store_unavailable("semantic profile completion failed") from error

    def fail(
        self,
        workspace_id: str,
        job_id: str,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
        code: SemanticJoinProfileFailureCode,
        retry_delay: timedelta | None,
        retention: timedelta | None,
    ) -> SemanticJoinProfileJob:
        _lease_identity(
            workspace_id,
            job_id,
            worker_id,
            lease_capability,
            fencing_token,
        )
        retry_for = (
            None
            if retry_delay is None
            else _bounded_duration(
                retry_delay,
                minimum=timedelta(seconds=1),
                maximum=timedelta(minutes=5),
                label="semantic profile retry delay",
            )
        )
        retained_for = (
            None
            if retention is None
            else _bounded_duration(
                retention,
                minimum=_MIN_RETENTION,
                maximum=_MAX_RETENTION,
                label="semantic profile retention",
            )
        )
        try:
            with self._database.connect() as connection:
                current = self._load_for_update(connection, workspace_id, job_id)
                now = _database_now(connection)
                failed = fail_semantic_join_profile_job(
                    current,
                    worker_id=worker_id,
                    lease_capability=lease_capability,
                    fencing_token=fencing_token,
                    code=code,
                    failed_at=now,
                    retry_at=None if retry_for is None else now + retry_for,
                    retain_until=None if retained_for is None else now + retained_for,
                )
                self._write_mutable_state(connection, current, failed)
                return failed
        except SemanticJoinProfileQueueError:
            raise
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except SemanticJoinProfileTransitionError as error:
            raise _lease_conflict() from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response("semantic profile failure response was invalid") from error
        except psycopg.Error as error:
            raise _store_unavailable("semantic profile failure transition failed") from error

    def _load_for_update(
        self,
        connection: psycopg.Connection[Any],
        workspace_id: str,
        job_id: str,
    ) -> SemanticJoinProfileJob:
        table = self._database.table("semantic_join_profile_jobs")
        row = connection.execute(
            sql.SQL(
                """
                SELECT {columns}
                FROM {table}
                WHERE workspace_id = %s
                  AND job_id = %s
                FOR UPDATE
                """
            ).format(columns=_JOB_COLUMNS, table=table),
            (workspace_id, job_id),
        ).fetchone()
        if row is None:
            raise SemanticJoinProfileQueueError(
                SemanticJoinProfileQueueErrorCode.NOT_FOUND,
                "semantic profile job is unavailable",
            )
        return _profile_job_from_row(row)

    def _write_mutable_state(
        self,
        connection: psycopg.Connection[Any],
        previous: SemanticJoinProfileJob,
        current: SemanticJoinProfileJob,
    ) -> None:
        table = self._database.table("semantic_join_profile_jobs")
        lease = current.lease
        failure_code = None if current.failure is None else current.failure.code.value
        result_json = (
            None
            if current.result is None
            else Jsonb(current.result.profile.model_dump(mode="json"))
        )
        result_fingerprint = None if current.result is None else current.result.fingerprint
        updated = connection.execute(
            sql.SQL(
                """
                UPDATE {table}
                SET status = %s,
                    attempt_count = %s,
                    available_at = %s,
                    lease_owner_id = %s,
                    lease_capability_digest = %s,
                    fencing_token = %s,
                    lease_acquired_at = %s,
                    lease_heartbeat_at = %s,
                    lease_expires_at = %s,
                    failure_code = %s,
                    result_profile_json = %s,
                    result_profile_fingerprint = %s,
                    updated_at = %s,
                    completed_at = %s,
                    retain_until = %s
                WHERE workspace_id = %s
                  AND job_id = %s
                  AND status = %s
                  AND attempt_count = %s
                  AND fencing_token = %s
                  AND updated_at = %s
                """
            ).format(table=table),
            (
                current.status.value,
                current.attempts,
                current.available_at,
                None if lease is None else lease.worker_id,
                None if lease is None else lease.capability_digest,
                current.fencing_token,
                None if lease is None else lease.acquired_at,
                None if lease is None else lease.heartbeat_at,
                None if lease is None else lease.expires_at,
                failure_code,
                result_json,
                result_fingerprint,
                current.updated_at,
                current.completed_at,
                current.retain_until,
                previous.workspace_id,
                previous.job_id,
                previous.status.value,
                previous.attempts,
                previous.fencing_token,
                previous.updated_at,
            ),
        )
        if updated.rowcount != 1:
            raise _lease_conflict()


@dataclass(frozen=True, slots=True)
class QueuedRelationshipEvidencePort:
    """Reconciler adapter that never receives a source DSN.

    A scan attempt first enqueues every exact proposal. A later attempt consumes
    the completed aggregate profile. Pending work remains an explicit evidence
    outage so the durable scan lifecycle schedules a bounded retry.
    """

    queue: SemanticJoinProfileQueuePort = field(repr=False)
    target_resolver: ExecutionTargetResolverPort = field(repr=False)
    workspace_id: str
    scan_id: str
    clock: Callable[[], datetime] = field(repr=False)
    max_attempts: int = 5

    def __post_init__(self) -> None:
        _bounded_text(
            self.workspace_id,
            "semantic profile workspace",
            maximum_bytes=200,
        )
        _scan_id(self.scan_id)
        if not 1 <= self.max_attempts <= 100:
            raise ValueError("semantic profile max attempts is outside the supported bound")

    def profile_bound(
        self,
        proposal: SemanticJoinProfileProposal,
    ) -> RelationshipProfile:
        return self.profiles_bound((proposal,))[0]

    def profiles_bound(
        self,
        proposals: tuple[SemanticJoinProfileProposal, ...],
    ) -> tuple[RelationshipProfile, ...]:
        """Enqueue the complete batch before checking whether any result is pending."""

        checked_proposals = tuple(
            validate_semantic_join_profile_proposal(proposal) for proposal in proposals
        )
        execution_targets = self._resolve_targets(checked_proposals)
        fingerprints = tuple(
            semantic_join_profile_proposal_fingerprint(proposal) for proposal in checked_proposals
        )
        if len(fingerprints) != len(set(fingerprints)):
            raise RelationshipWorkflowError(
                RelationshipErrorCode.INVALID_EVIDENCE,
                "aggregate relationship profile batch contains duplicate proposals",
            )
        try:
            jobs = tuple(
                self.queue.enqueue(
                    self.workspace_id,
                    self.scan_id,
                    proposal,
                    execution_target=execution_target,
                    requested_at=_aware(
                        self.clock(),
                        "semantic profile request timestamp",
                    ),
                    max_attempts=self.max_attempts,
                ).job
                for proposal, execution_target in zip(
                    checked_proposals,
                    execution_targets,
                    strict=True,
                )
            )
            for job, proposal, execution_target, fingerprint in zip(
                jobs,
                checked_proposals,
                execution_targets,
                fingerprints,
                strict=True,
            ):
                self._validate_exact_job(
                    job,
                    proposal,
                    execution_target,
                    fingerprint,
                )
            latest_jobs = tuple(
                (
                    job
                    if job.status is SemanticJoinProfileJobStatus.COMPLETED
                    else self.queue.load(
                        self.workspace_id,
                        self.scan_id,
                        fingerprint,
                    )
                )
                for job, fingerprint in zip(jobs, fingerprints, strict=True)
            )
        except SemanticJoinProfileQueueError as error:
            code = (
                RelationshipErrorCode.INVALID_EVIDENCE
                if error.code
                in {
                    SemanticJoinProfileQueueErrorCode.IDEMPOTENCY_CONFLICT,
                    SemanticJoinProfileQueueErrorCode.INVALID_RESPONSE,
                }
                else RelationshipErrorCode.EVIDENCE_UNAVAILABLE
            )
            raise RelationshipWorkflowError(
                code,
                "aggregate relationship profile queue is unavailable",
            ) from None
        except RelationshipWorkflowError:
            raise
        except Exception:
            raise RelationshipWorkflowError(
                RelationshipErrorCode.INVALID_EVIDENCE,
                "aggregate relationship profile request is invalid",
            ) from None
        profiles: list[RelationshipProfile] = []
        for latest_job, proposal, execution_target, fingerprint in zip(
            latest_jobs,
            checked_proposals,
            execution_targets,
            fingerprints,
            strict=True,
        ):
            if latest_job is None:
                raise RelationshipWorkflowError(
                    RelationshipErrorCode.EVIDENCE_UNAVAILABLE,
                    "aggregate relationship profile is pending or unavailable",
                )
            self._validate_exact_job(
                latest_job,
                proposal,
                execution_target,
                fingerprint,
            )
            if (
                latest_job.status is not SemanticJoinProfileJobStatus.COMPLETED
                or latest_job.result is None
            ):
                raise RelationshipWorkflowError(
                    RelationshipErrorCode.EVIDENCE_UNAVAILABLE,
                    "aggregate relationship profile is pending or unavailable",
                )
            profiles.append(latest_job.result.profile)
        return tuple(profiles)

    def _validate_exact_job(
        self,
        job: SemanticJoinProfileJob,
        proposal: SemanticJoinProfileProposal,
        execution_target: SemanticJoinProfileTargetRef,
        proposal_fingerprint: str,
    ) -> None:
        if (
            job.workspace_id != self.workspace_id
            or job.scan_id != self.scan_id
            or job.connection_id != proposal.connection_id
            or job.execution_target != execution_target
            or job.connector_contract_version is None
            or job.proposal != proposal.proposal
            or job.proposal_fingerprint != proposal_fingerprint
        ):
            raise RelationshipWorkflowError(
                RelationshipErrorCode.INVALID_EVIDENCE,
                "aggregate relationship profile crossed its exact scope",
            )

    def _resolve_targets(
        self,
        proposals: tuple[SemanticJoinProfileProposal, ...],
    ) -> tuple[SemanticJoinProfileTargetRef, ...]:
        resolved_by_connection: dict[CatalogConnectionId, SemanticJoinProfileTargetRef] = {}
        try:
            for proposal in proposals:
                if proposal.connection_id not in resolved_by_connection:
                    target = self.target_resolver.resolve_current(
                        workspace_id=self.workspace_id,
                        connection_id=proposal.connection_id,
                    )
                    resolved = SemanticJoinProfileTargetRef.from_target(target)
                    if (
                        resolved.workspace_id != self.workspace_id
                        or resolved.connection_id != proposal.connection_id
                    ):
                        raise ValueError("connector target crossed its requested scope")
                    resolved_by_connection[proposal.connection_id] = resolved
        except ConnectorTargetError:
            raise RelationshipWorkflowError(
                RelationshipErrorCode.EVIDENCE_UNAVAILABLE,
                "aggregate relationship profile target is unavailable",
            ) from None
        except Exception:
            raise RelationshipWorkflowError(
                RelationshipErrorCode.INVALID_EVIDENCE,
                "aggregate relationship profile target is invalid",
            ) from None
        return tuple(resolved_by_connection[proposal.connection_id] for proposal in proposals)


def _profile_job_from_row(row: Sequence[object]) -> SemanticJoinProfileJob:
    if len(row) != len(_JOB_COLUMN_NAMES):
        raise ValueError("semantic profile row has an invalid column count")
    values = dict(zip(_JOB_COLUMN_NAMES, row, strict=True))
    proposal = JoinProposal.model_validate(values["proposal_json"])
    status = SemanticJoinProfileJobStatus(_required_str(values["status"]))
    attempts = _required_int(values["attempt_count"])
    fence = _required_int(values["fencing_token"])
    lease: SemanticJoinProfileLease | None = None
    if values["lease_owner_id"] is not None:
        lease = SemanticJoinProfileLease(
            job_id=_required_str(values["job_id"]),
            worker_id=_required_str(values["lease_owner_id"]),
            capability_digest=_required_str(values["lease_capability_digest"]),
            fencing_token=fence,
            attempt=attempts,
            acquired_at=_required_datetime(values["lease_acquired_at"]),
            heartbeat_at=_required_datetime(values["lease_heartbeat_at"]),
            expires_at=_required_datetime(values["lease_expires_at"]),
        )
    failure: SemanticJoinProfileFailure | None = None
    if values["failure_code"] is not None:
        disposition = {
            SemanticJoinProfileJobStatus.RETRY_WAIT: (SemanticJoinProfileFailureDisposition.RETRY),
            SemanticJoinProfileJobStatus.FAILED: (SemanticJoinProfileFailureDisposition.FAIL),
        }.get(status)
        if disposition is None:
            raise ValueError("semantic profile failure has an invalid status")
        failure = SemanticJoinProfileFailure(
            code=SemanticJoinProfileFailureCode(_required_str(values["failure_code"])),
            disposition=disposition,
            attempt=attempts,
            occurred_at=_required_datetime(values["updated_at"]),
        )
    completed_at = _optional_datetime(values["completed_at"])
    execution_target, connector_contract_version = _profile_target_from_values(values)
    result: SemanticJoinProfileResult | None = None
    if values["result_profile_json"] is not None:
        if completed_at is None:
            raise ValueError("semantic profile result has no completion time")
        result = SemanticJoinProfileResult(
            profile=RelationshipProfile.model_validate(values["result_profile_json"]),
            fingerprint=_required_str(values["result_profile_fingerprint"]),
            completed_at=completed_at,
        )
    return SemanticJoinProfileJob(
        job_id=_required_str(values["job_id"]),
        workspace_id=_required_str(values["workspace_id"]),
        scan_id=_required_str(values["scan_id"]),
        connection_id=CatalogConnectionId(_required_str(values["connection_id"])),
        execution_target=execution_target,
        connector_contract_version=connector_contract_version,
        proposal=proposal,
        proposal_fingerprint=_required_str(values["proposal_fingerprint"]),
        status=status,
        max_attempts=_required_int(values["max_attempts"]),
        attempts=attempts,
        available_at=_required_datetime(values["available_at"]),
        fencing_token=fence,
        lease=lease,
        failure=failure,
        result=result,
        requested_at=_required_datetime(values["requested_at"]),
        updated_at=_required_datetime(values["updated_at"]),
        completed_at=completed_at,
        retain_until=_optional_datetime(values["retain_until"]),
    )


def _profile_target_from_values(
    values: dict[str, object],
) -> tuple[SemanticJoinProfileTargetRef | None, int | None]:
    raw = (
        values["connector_contract_version"],
        values["connector_route_revision"],
        values["connector_route_fingerprint"],
        values["connector_target_fingerprint"],
    )
    if all(value is None for value in raw):
        return None, None
    if any(value is None for value in raw):
        raise ValueError("semantic profile connector target is incomplete")
    return (
        SemanticJoinProfileTargetRef(
            workspace_id=_required_str(values["workspace_id"]),
            connection_id=CatalogConnectionId(_required_str(values["connection_id"])),
            route_revision=_required_int(values["connector_route_revision"]),
            route_fingerprint=_required_str(values["connector_route_fingerprint"]),
            target_fingerprint=_required_str(values["connector_target_fingerprint"]),
        ),
        _required_int(values["connector_contract_version"]),
    )


def _same_profile_request(
    persisted: SemanticJoinProfileJob,
    requested: SemanticJoinProfileJob,
) -> bool:
    return (
        persisted.job_id == requested.job_id
        and persisted.workspace_id == requested.workspace_id
        and persisted.scan_id == requested.scan_id
        and persisted.connection_id == requested.connection_id
        and persisted.execution_target == requested.execution_target
        and persisted.proposal == requested.proposal
        and persisted.proposal_fingerprint == requested.proposal_fingerprint
        and persisted.status is requested.status
        and persisted.max_attempts == requested.max_attempts
        and persisted.attempts == requested.attempts
        and persisted.available_at == requested.available_at
        and persisted.fencing_token == requested.fencing_token
        and persisted.requested_at == requested.requested_at
        and persisted.updated_at == requested.updated_at
    )


def _lease_inputs(
    workspace_id: str,
    job_id: str,
    worker_id: str,
    lease_capability: str,
    fencing_token: int,
    lease_duration: timedelta,
) -> timedelta:
    _lease_identity(
        workspace_id,
        job_id,
        worker_id,
        lease_capability,
        fencing_token,
    )
    return _bounded_duration(
        lease_duration,
        minimum=_MIN_LEASE_DURATION,
        maximum=_MAX_LEASE_DURATION,
        label="semantic profile lease duration",
    )


def _lease_identity(
    workspace_id: str,
    job_id: str,
    worker_id: str,
    lease_capability: str,
    fencing_token: int,
) -> None:
    _bounded_text(workspace_id, "semantic profile workspace", maximum_bytes=200)
    if not isinstance(job_id, str) or not job_id.startswith("profile_job_") or len(job_id) != 76:
        raise ValueError("semantic profile job id is invalid")
    _bounded_worker_id(worker_id)
    digest_semantic_join_profile_capability(lease_capability)
    if not isinstance(fencing_token, int) or isinstance(fencing_token, bool):
        raise ValueError("semantic profile fencing token is invalid")
    if fencing_token < 1:
        raise ValueError("semantic profile fencing token is invalid")


def _bounded_worker_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 3 <= len(value) <= 200
        or not value.isascii()
        or value.casefold() != value
        or not value[0].isalpha()
        or not value.replace("_", "").replace("-", "").isalnum()
    ):
        raise ValueError("semantic profile worker id must be a bounded inert identifier")
    return value


def _scan_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("scan_")
        or len(value) != 69
        or any(character not in "0123456789abcdef" for character in value[5:])
    ):
        raise ValueError("semantic profile scan id must be canonical")
    return value


def _sha256(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


def _bounded_text(value: str, label: str, *, maximum_bytes: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value.strip() != value
        or len(value.encode("utf-8")) > maximum_bytes
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{label} must be bounded nonblank text")
    return value


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


def _database_now(connection: psycopg.Connection[Any]) -> datetime:
    row = connection.execute("SELECT clock_timestamp()").fetchone()
    if row is None or len(row) != 1:
        raise ValueError("database time response is invalid")
    return _required_datetime(row[0])


def _required_str(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("semantic profile text value is invalid")
    return value


def _required_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("semantic profile integer value is invalid")
    return value


def _required_datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("semantic profile timestamp is invalid")
    return _aware(value, "semantic profile timestamp")


def _optional_datetime(value: object) -> datetime | None:
    return None if value is None else _required_datetime(value)


def _aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


def _capacity_exceeded() -> SemanticJoinProfileQueueError:
    return SemanticJoinProfileQueueError(
        SemanticJoinProfileQueueErrorCode.CAPACITY_EXCEEDED,
        "semantic profile queue capacity is exhausted",
    )


def _lease_conflict() -> SemanticJoinProfileQueueError:
    return SemanticJoinProfileQueueError(
        SemanticJoinProfileQueueErrorCode.LEASE_CONFLICT,
        "semantic profile lease is no longer current",
    )


def _schema_mismatch() -> SemanticJoinProfileQueueError:
    return SemanticJoinProfileQueueError(
        SemanticJoinProfileQueueErrorCode.SCHEMA_MISMATCH,
        "semantic profile queue schema is unavailable",
    )


def _invalid_response(message: str) -> SemanticJoinProfileQueueError:
    return SemanticJoinProfileQueueError(
        SemanticJoinProfileQueueErrorCode.INVALID_RESPONSE,
        message,
    )


def _store_unavailable(message: str) -> SemanticJoinProfileQueueError:
    return SemanticJoinProfileQueueError(
        SemanticJoinProfileQueueErrorCode.STORE_UNAVAILABLE,
        message,
    )


__all__ = [
    "PostgresSemanticJoinProfileQueue",
    "QueuedRelationshipEvidencePort",
]
