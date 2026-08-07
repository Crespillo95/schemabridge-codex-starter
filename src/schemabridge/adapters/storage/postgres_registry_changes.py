"""Durable PostgreSQL authoring store for governed registry-v2 join changes."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg
from psycopg import sql
from psycopg.errors import (
    CheckViolation,
    ForeignKeyViolation,
    ObjectNotInPrerequisiteState,
    UndefinedColumn,
    UndefinedFunction,
    UndefinedTable,
    UniqueViolation,
)
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from schemabridge.adapters.semantic_change.postgres_profile_queue import (
    _JOB_COLUMNS as _shared_profile_job_columns,
)
from schemabridge.adapters.semantic_change.postgres_profile_queue import (
    _profile_job_from_row as _shared_profile_job_from_row,
)
from schemabridge.adapters.storage.postgres import ControlConnectionProvider, _ControlDatabase
from schemabridge.application.ports.registry_changes import (
    RegistryChangeOperationReplay,
    RegistryChangeStoreError,
    RegistryChangeStoreErrorCode,
)
from schemabridge.application.ports.semantic_profile_jobs import (
    SemanticJoinProfileQueueError,
    SemanticJoinProfileQueueErrorCode,
)
from schemabridge.domain.registry_change_authoring import (
    MAX_REGISTRY_CHANGE_HISTORY,
    RegistryChangeAuditEvent,
    RegistryChangeAuditRecord,
    RegistryJoinDraftMutation,
    RegistryJoinPreparation,
    RegistryJoinProfileAuthoringMutation,
    RegistryJoinProfileAuthoringRequest,
)
from schemabridge.domain.registry_changes import (
    PreparedRegistryJoinProposal,
    RegistryJoinChangeDraft,
    RegistryJoinChangeStatus,
)
from schemabridge.domain.semantic_profile_jobs import (
    SemanticJoinProfileJob,
    SemanticJoinProfileJobStatus,
    SemanticJoinProfileSubmission,
    semantic_join_profile_proposal_fingerprint,
)

_MAX_WORKSPACE_PAGE = 50
_OPERATIONS = frozenset(
    {
        "request_profile",
        "finalize_draft",
        "record_decision",
        "prepare_publication",
    }
)
_OPERATION_AUDIT = {
    "request_profile": RegistryChangeAuditEvent.PROFILE_REQUESTED,
    "finalize_draft": RegistryChangeAuditEvent.DRAFT_FINALIZED,
    "record_decision": RegistryChangeAuditEvent.DECISION_RECORDED,
    "prepare_publication": RegistryChangeAuditEvent.PUBLICATION_PREPARED,
}


@dataclass(frozen=True, slots=True)
class PostgresRegistryJoinProfileRequestQueue:
    """Two-operation API queue backed only by the M35 definer functions."""

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

    def enqueue(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
        *,
        max_attempts: int = 5,
    ) -> SemanticJoinProfileSubmission:
        if not 1 <= max_attempts <= 100:
            raise ValueError("semantic profile max attempts is outside the supported bound")
        replay = self.load(authoring)
        if replay is not None:
            _require_job_matches_authoring(replay, authoring)
            return SemanticJoinProfileSubmission(job=replay, replayed=True)
        function = self._database.table("enqueue_registry_join_profile")
        try:
            with self._database.connect() as connection:
                row = connection.execute(
                    sql.SQL("SELECT {function}(%s, %s, %s, %s)").format(function=function),
                    (
                        authoring.workspace_id,
                        authoring.request.scan_id,
                        authoring.fingerprint,
                        max_attempts,
                    ),
                ).fetchone()
            if row is None or len(row) != 1 or not isinstance(row[0], str):
                raise _queue_invalid_response()
            persisted = self.load(authoring)
            if persisted is None or persisted.job_id != row[0]:
                raise _queue_invalid_response()
            _require_job_matches_authoring(persisted, authoring)
            if (
                persisted.status is not SemanticJoinProfileJobStatus.REQUESTED
                or persisted.lease is not None
            ):
                raise _queue_state_conflict()
            return SemanticJoinProfileSubmission(job=persisted, replayed=False)
        except SemanticJoinProfileQueueError:
            raise
        except ObjectNotInPrerequisiteState as error:
            raise _queue_state_conflict() from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _queue_invalid_response() from error
        except (UndefinedTable, UndefinedColumn, UndefinedFunction) as error:
            raise _queue_unavailable() from error
        except psycopg.Error as error:
            raise _queue_unavailable() from error

    def load(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
    ) -> SemanticJoinProfileJob | None:
        function = self._database.table("load_registry_join_profile_job")
        try:
            with self._database.connect() as connection:
                rows = connection.execute(
                    sql.SQL("SELECT {columns} FROM {function}(%s, %s, %s)").format(
                        columns=_shared_profile_job_columns,
                        function=function,
                    ),
                    (
                        authoring.workspace_id,
                        authoring.request.scan_id,
                        authoring.fingerprint,
                    ),
                ).fetchall()
            if not rows:
                return None
            if len(rows) != 1:
                raise _queue_invalid_response()
            job = _shared_profile_job_from_row(rows[0])
            _require_job_matches_authoring(job, authoring)
            return job
        except SemanticJoinProfileQueueError:
            raise
        except (ValidationError, TypeError, ValueError) as error:
            raise _queue_invalid_response() from error
        except (UndefinedTable, UndefinedColumn, UndefinedFunction) as error:
            raise _queue_unavailable() from error
        except psycopg.Error as error:
            raise _queue_unavailable() from error


@dataclass(frozen=True, slots=True)
class PostgresRegistryChangeStore:
    """Atomic tenant-scoped store over the additive M35 authoring tables."""

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

    def load_operation_replay(
        self,
        workspace_id: str,
        idempotency_digest: str,
    ) -> RegistryChangeOperationReplay | None:
        operations = self._database.table("semantic_registry_change_operations")
        try:
            with self._database.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT operation, request_fingerprint, actor_id, draft_id,
                               response_revision, response_fingerprint,
                               response_authoring, response_job_id,
                               response_job, response_draft, response_proposal,
                               proposal_id, proposal_fingerprint, created_at
                        FROM {operations}
                        WHERE workspace_id = %s
                          AND idempotency_digest = %s
                        """
                    ).format(operations=operations),
                    (workspace_id, idempotency_digest),
                ).fetchone()
                if row is None:
                    return None
                return self._operation_from_row(
                    connection,
                    workspace_id=workspace_id,
                    row=row,
                )
        except RegistryChangeStoreError:
            raise
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except (UndefinedTable, UndefinedColumn, UndefinedFunction) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def commit_draft(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
        draft: RegistryJoinChangeDraft,
        audit: RegistryChangeAuditRecord,
        *,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryJoinDraftMutation:
        _require_draft_finalized_audit(authoring, draft, audit, actor_id=actor_id)
        replay = self._exact_replay(
            authoring.workspace_id,
            actor_id,
            idempotency_digest,
            operation=operation,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            if replay.draft is None:
                raise _conflict()
            return RegistryJoinDraftMutation(
                authoring=replay.authoring,
                draft=replay.draft,
                replayed=True,
            )
        persisted_job = self._load_profile_job(authoring)
        if persisted_job is None:
            raise _conflict()
        _require_same_job_snapshot(persisted_job, draft.profile_job)
        try:
            with self._database.connect() as connection, connection.transaction():
                self._lock_authoring(connection, authoring)
                self._require_bound_profile_audit(connection, authoring, persisted_job)
                self._lock_profile_job_identity(connection, authoring, persisted_job)
                self._insert_draft(connection, draft)
                self._insert_audit(connection, audit)
                self._insert_operation(
                    connection,
                    authoring=authoring,
                    draft=draft,
                    proposal=None,
                    operation=operation,
                    actor_id=actor_id,
                    idempotency_digest=idempotency_digest,
                    request_fingerprint=request_fingerprint,
                    created_at=audit.occurred_at,
                )
            return RegistryJoinDraftMutation(authoring=authoring, draft=draft)
        except UniqueViolation as error:
            replay = self._exact_replay(
                authoring.workspace_id,
                actor_id,
                idempotency_digest,
                operation=operation,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None and replay.draft is not None:
                return RegistryJoinDraftMutation(
                    authoring=replay.authoring,
                    draft=replay.draft,
                    replayed=True,
                )
            raise _conflict() from error
        except RegistryChangeStoreError as error:
            replay = self._exact_replay(
                authoring.workspace_id,
                actor_id,
                idempotency_digest,
                operation=operation,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None and replay.draft is not None:
                return RegistryJoinDraftMutation(
                    authoring=replay.authoring,
                    draft=replay.draft,
                    replayed=True,
                )
            raise error
        except (
            CheckViolation,
            ForeignKeyViolation,
            ValidationError,
            TypeError,
            ValueError,
        ) as error:
            raise _invalid_response() from error
        except (UndefinedTable, UndefinedColumn, UndefinedFunction) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def commit_decision(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
        draft: RegistryJoinChangeDraft,
        audit: RegistryChangeAuditRecord,
        *,
        expected_revision: int,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryJoinDraftMutation:
        _require_decision_audit(authoring, draft, audit, actor_id=actor_id)
        replay = self._exact_replay(
            authoring.workspace_id,
            actor_id,
            idempotency_digest,
            operation=operation,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            if replay.draft is None:
                raise _conflict()
            return RegistryJoinDraftMutation(
                authoring=replay.authoring,
                draft=replay.draft,
                replayed=True,
            )
        try:
            with self._database.connect() as connection, connection.transaction():
                self._lock_authoring(connection, authoring)
                self._cas_draft(connection, draft, expected_revision=expected_revision)
                self._insert_decision(connection, draft)
                self._insert_audit(connection, audit)
                self._insert_operation(
                    connection,
                    authoring=authoring,
                    draft=draft,
                    proposal=None,
                    operation=operation,
                    actor_id=actor_id,
                    idempotency_digest=idempotency_digest,
                    request_fingerprint=request_fingerprint,
                    created_at=audit.occurred_at,
                )
            return RegistryJoinDraftMutation(authoring=authoring, draft=draft)
        except UniqueViolation as error:
            replay = self._exact_replay(
                authoring.workspace_id,
                actor_id,
                idempotency_digest,
                operation=operation,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None and replay.draft is not None:
                return RegistryJoinDraftMutation(
                    authoring=replay.authoring,
                    draft=replay.draft,
                    replayed=True,
                )
            raise _conflict() from error
        except RegistryChangeStoreError as error:
            replay = self._exact_replay(
                authoring.workspace_id,
                actor_id,
                idempotency_digest,
                operation=operation,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None and replay.draft is not None:
                return RegistryJoinDraftMutation(
                    authoring=replay.authoring,
                    draft=replay.draft,
                    replayed=True,
                )
            raise error
        except (
            CheckViolation,
            ForeignKeyViolation,
            ValidationError,
            TypeError,
            ValueError,
        ) as error:
            raise _invalid_response() from error
        except (UndefinedTable, UndefinedColumn, UndefinedFunction) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def commit_preparation(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
        draft: RegistryJoinChangeDraft,
        proposal: PreparedRegistryJoinProposal,
        audit: RegistryChangeAuditRecord,
        *,
        expected_revision: int,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryJoinPreparation:
        _require_preparation_audit(
            authoring,
            draft,
            proposal,
            audit,
            actor_id=actor_id,
        )
        replay = self._exact_replay(
            authoring.workspace_id,
            actor_id,
            idempotency_digest,
            operation=operation,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            if replay.draft is None or replay.proposal is None:
                raise _conflict()
            return RegistryJoinPreparation(
                authoring=replay.authoring,
                draft=replay.draft,
                proposal=replay.proposal,
                replayed=True,
            )
        try:
            with self._database.connect() as connection, connection.transaction():
                self._lock_authoring(connection, authoring)
                self._cas_draft(connection, draft, expected_revision=expected_revision)
                self._insert_proposal(connection, proposal)
                self._insert_audit(connection, audit)
                self._insert_operation(
                    connection,
                    authoring=authoring,
                    draft=draft,
                    proposal=proposal,
                    operation=operation,
                    actor_id=actor_id,
                    idempotency_digest=idempotency_digest,
                    request_fingerprint=request_fingerprint,
                    created_at=audit.occurred_at,
                )
            return RegistryJoinPreparation(
                authoring=authoring,
                draft=draft,
                proposal=proposal,
            )
        except UniqueViolation as error:
            replay = self._exact_replay(
                authoring.workspace_id,
                actor_id,
                idempotency_digest,
                operation=operation,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None and replay.draft is not None and replay.proposal is not None:
                return RegistryJoinPreparation(
                    authoring=replay.authoring,
                    draft=replay.draft,
                    proposal=replay.proposal,
                    replayed=True,
                )
            raise _conflict() from error
        except RegistryChangeStoreError as error:
            replay = self._exact_replay(
                authoring.workspace_id,
                actor_id,
                idempotency_digest,
                operation=operation,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None and replay.draft is not None and replay.proposal is not None:
                return RegistryJoinPreparation(
                    authoring=replay.authoring,
                    draft=replay.draft,
                    proposal=replay.proposal,
                    replayed=True,
                )
            raise error
        except (
            CheckViolation,
            ForeignKeyViolation,
            ValidationError,
            TypeError,
            ValueError,
        ) as error:
            raise _invalid_response() from error
        except (UndefinedTable, UndefinedColumn, UndefinedFunction) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def list_audit(
        self,
        workspace_id: str,
        change_id: str,
        *,
        limit: int = MAX_REGISTRY_CHANGE_HISTORY + 1,
    ) -> tuple[RegistryChangeAuditRecord, ...]:
        if not 1 <= limit <= MAX_REGISTRY_CHANGE_HISTORY + 1:
            raise _invalid_response()
        table = self._database.table("semantic_registry_change_audit")
        try:
            with self._database.connect() as connection:
                rows = connection.execute(
                    sql.SQL(
                        """
                        SELECT payload
                        FROM {table}
                        WHERE workspace_id = %s AND draft_id = %s
                        ORDER BY
                            occurred_at,
                            CASE event
                                WHEN 'profile_requested' THEN 1
                                WHEN 'profile_job_bound' THEN 2
                                WHEN 'draft_finalized' THEN 3
                                WHEN 'decision_recorded' THEN 4
                                WHEN 'publication_prepared' THEN 5
                            END,
                            audit_id
                        LIMIT %s
                        """
                    ).format(table=table),
                    (workspace_id, change_id, limit),
                ).fetchall()
            return tuple(RegistryChangeAuditRecord.model_validate(row[0]) for row in rows)
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except (UndefinedTable, UndefinedColumn) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def persist_profile_request(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
        audit: RegistryChangeAuditRecord,
        *,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryJoinProfileAuthoringMutation:
        _require_profile_requested_audit(authoring, audit, actor_id=actor_id)
        replay = self._exact_replay(
            authoring.workspace_id,
            actor_id,
            idempotency_digest,
            operation=operation,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            return RegistryJoinProfileAuthoringMutation(
                authoring=replay.authoring,
                job=replay.job,
                replayed=True,
            )
        try:
            with self._database.connect() as connection, connection.transaction():
                self._insert_authoring(connection, authoring)
                self._insert_audit(connection, audit)
                self._insert_operation(
                    connection,
                    authoring=authoring,
                    draft=None,
                    proposal=None,
                    operation=operation,
                    actor_id=actor_id,
                    idempotency_digest=idempotency_digest,
                    request_fingerprint=request_fingerprint,
                    created_at=audit.occurred_at,
                )
            return RegistryJoinProfileAuthoringMutation(authoring=authoring)
        except UniqueViolation as error:
            replay = self._exact_replay(
                authoring.workspace_id,
                actor_id,
                idempotency_digest,
                operation=operation,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None:
                return RegistryJoinProfileAuthoringMutation(
                    authoring=replay.authoring,
                    job=replay.job,
                    replayed=True,
                )
            raise _conflict() from error
        except RegistryChangeStoreError:
            raise
        except (
            CheckViolation,
            ForeignKeyViolation,
            ValidationError,
            TypeError,
            ValueError,
        ) as error:
            raise _invalid_response() from error
        except (UndefinedTable, UndefinedColumn, UndefinedFunction) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def bind_profile_job(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
        job: SemanticJoinProfileJob,
        audit: RegistryChangeAuditRecord,
        *,
        idempotency_digest: str,
    ) -> RegistryJoinProfileAuthoringMutation:
        actor_id = audit.actor_id
        _require_profile_bound_audit(authoring, job, audit, actor_id=actor_id)
        if job.status is not SemanticJoinProfileJobStatus.REQUESTED or job.lease is not None:
            raise _conflict()
        operation = self._require_profile_operation(
            authoring,
            actor_id=actor_id,
            idempotency_digest=idempotency_digest,
        )
        if operation.job is not None:
            _require_same_job_snapshot(operation.job, job)
            return RegistryJoinProfileAuthoringMutation(
                authoring=operation.authoring,
                job=operation.job,
                replayed=True,
            )
        persisted = self._load_profile_job(authoring)
        if persisted is None:
            raise _conflict()
        _require_job_matches_authoring(persisted, authoring)
        _require_same_job_snapshot(persisted, job)
        try:
            with self._database.connect() as connection, connection.transaction():
                self._lock_authoring(connection, authoring)
                current = self._load_profile_job_with_connection(connection, authoring)
                if current is None:
                    raise _conflict()
                _require_same_job_snapshot(current, persisted)
                self._insert_audit(connection, audit)
                operations = self._database.table("semantic_registry_change_operations")
                updated = connection.execute(
                    sql.SQL(
                        """
                        UPDATE {operations}
                        SET response_job_id = %s, response_job = %s
                        WHERE workspace_id = %s
                          AND actor_id = %s
                          AND idempotency_digest = %s
                          AND operation = 'request_profile'
                          AND draft_id = %s
                          AND response_authoring = %s
                          AND response_job_id IS NULL
                          AND response_job IS NULL
                        """
                    ).format(operations=operations),
                    (
                        persisted.job_id,
                        Jsonb(persisted.model_dump(mode="json")),
                        authoring.workspace_id,
                        actor_id,
                        idempotency_digest,
                        authoring.id,
                        Jsonb(authoring.model_dump(mode="json")),
                    ),
                )
                if updated.rowcount != 1:
                    raise _conflict()
            return RegistryJoinProfileAuthoringMutation(authoring=authoring, job=persisted)
        except UniqueViolation as error:
            replay = self._require_profile_operation(
                authoring,
                actor_id=actor_id,
                idempotency_digest=idempotency_digest,
            )
            if replay.job is not None:
                _require_same_job_snapshot(replay.job, persisted)
                return RegistryJoinProfileAuthoringMutation(
                    authoring=replay.authoring,
                    job=replay.job,
                    replayed=True,
                )
            raise _conflict() from error
        except RegistryChangeStoreError as error:
            replay = self._require_profile_operation(
                authoring,
                actor_id=actor_id,
                idempotency_digest=idempotency_digest,
            )
            if replay.job is not None:
                _require_same_job_snapshot(replay.job, persisted)
                return RegistryJoinProfileAuthoringMutation(
                    authoring=replay.authoring,
                    job=replay.job,
                    replayed=True,
                )
            raise error
        except (
            CheckViolation,
            ForeignKeyViolation,
            ValidationError,
            TypeError,
            ValueError,
        ) as error:
            raise _invalid_response() from error
        except (UndefinedTable, UndefinedColumn, UndefinedFunction) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def load_authoring(
        self,
        workspace_id: str,
        change_id: str,
    ) -> RegistryJoinProfileAuthoringRequest | None:
        table = self._database.table("registry_join_profile_requests")
        try:
            with self._database.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT workspace_id, change_id, scan_id, catalog_scope,
                               registry_id, base_registry_version,
                               base_registry_fingerprint, connection_id,
                               proposal_fingerprint, execution_target_fingerprint,
                               profile_request_fingerprint, request_fingerprint,
                               requested_by, payload, requested_at
                        FROM {table}
                        WHERE workspace_id = %s AND change_id = %s
                        """
                    ).format(table=table),
                    (workspace_id, change_id),
                ).fetchone()
            return None if row is None else _authoring_from_row(row)
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except (UndefinedTable, UndefinedColumn) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def load_draft(
        self,
        workspace_id: str,
        change_id: str,
    ) -> RegistryJoinChangeDraft | None:
        table = self._database.table("semantic_registry_change_drafts")
        try:
            with self._database.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT workspace_id, draft_id, change_kind, catalog_scope,
                               registry_id, owner_actor_id, status, revision,
                               fingerprint, prepared_proposal_id,
                               prepared_proposal_fingerprint, payload,
                               created_at, updated_at
                        FROM {table}
                        WHERE workspace_id = %s AND draft_id = %s
                        """
                    ).format(table=table),
                    (workspace_id, change_id),
                ).fetchone()
            return None if row is None else _draft_from_row(row)
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except (UndefinedTable, UndefinedColumn) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def list_for_workspace(
        self,
        workspace_id: str,
        *,
        owner_actor_id: str | None,
        limit: int,
    ) -> tuple[RegistryJoinProfileAuthoringRequest, ...]:
        if not 1 <= limit <= _MAX_WORKSPACE_PAGE:
            raise _invalid_response()
        table = self._database.table("registry_join_profile_requests")
        owner_clause = sql.SQL("") if owner_actor_id is None else sql.SQL(" AND requested_by = %s")
        params: list[object] = [workspace_id]
        if owner_actor_id is not None:
            params.append(owner_actor_id)
        params.append(limit)
        try:
            with self._database.connect() as connection:
                rows = connection.execute(
                    sql.SQL(
                        """
                        SELECT workspace_id, change_id, scan_id, catalog_scope,
                               registry_id, base_registry_version,
                               base_registry_fingerprint, connection_id,
                               proposal_fingerprint, execution_target_fingerprint,
                               profile_request_fingerprint, request_fingerprint,
                               requested_by, payload, requested_at
                        FROM {table}
                        WHERE workspace_id = %s{owner_clause}
                        ORDER BY requested_at DESC, change_id DESC
                        LIMIT %s
                        """
                    ).format(table=table, owner_clause=owner_clause),
                    tuple(params),
                ).fetchall()
            return tuple(_authoring_from_row(row) for row in rows)
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except (UndefinedTable, UndefinedColumn) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def _exact_replay(
        self,
        workspace_id: str,
        actor_id: str,
        idempotency_digest: str,
        *,
        operation: str,
        request_fingerprint: str,
    ) -> RegistryChangeOperationReplay | None:
        replay = self.load_operation_replay(workspace_id, idempotency_digest)
        if replay is None:
            return None
        if (
            replay.operation != operation
            or replay.actor_id != actor_id
            or replay.request_fingerprint != request_fingerprint
        ):
            raise _conflict()
        return replay

    def _require_profile_operation(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
        *,
        actor_id: str,
        idempotency_digest: str,
    ) -> RegistryChangeOperationReplay:
        replay = self.load_operation_replay(
            authoring.workspace_id,
            idempotency_digest,
        )
        if (
            replay is None
            or replay.operation != "request_profile"
            or replay.actor_id != actor_id
            or replay.authoring != authoring
            or replay.draft is not None
            or replay.proposal is not None
        ):
            raise _conflict()
        return replay

    def _operation_from_row(
        self,
        connection: psycopg.Connection[Any],
        *,
        workspace_id: str,
        row: Sequence[object],
    ) -> RegistryChangeOperationReplay:
        if len(row) != 14:
            raise ValueError("registry change operation row has an invalid column count")
        operation = _required_str(row[0])
        request_fingerprint = _required_str(row[1])
        actor_id = _required_str(row[2])
        draft_id = _required_str(row[3])
        response_revision = _required_int(row[4])
        response_fingerprint = _required_str(row[5])
        authoring = RegistryJoinProfileAuthoringRequest.model_validate(row[6])
        response_job_id = _optional_str(row[7])
        job = None if row[8] is None else SemanticJoinProfileJob.model_validate(row[8])
        draft = None if row[9] is None else RegistryJoinChangeDraft.model_validate(row[9])
        proposal = None if row[10] is None else PreparedRegistryJoinProposal.model_validate(row[10])
        proposal_id = _optional_str(row[11])
        proposal_fingerprint = _optional_str(row[12])
        created_at = _required_datetime(row[13])
        if (
            operation not in _OPERATIONS
            or authoring.workspace_id != workspace_id
            or authoring.id != draft_id
            or (response_job_id is None) != (job is None)
            or (proposal_id is None) != (proposal_fingerprint is None)
            or (proposal is None) != (proposal_id is None)
        ):
            raise ValueError("registry change operation identity is inconsistent")
        if job is not None:
            if job.job_id != response_job_id:
                raise ValueError("registry change operation job binding is inconsistent")
            _require_job_matches_authoring(job, authoring)
            if job.status is not SemanticJoinProfileJobStatus.REQUESTED or job.lease is not None:
                raise ValueError("registry change operation job snapshot is not requested")
        if draft is not None:
            RegistryJoinDraftMutation(authoring=authoring, draft=draft)
        if proposal is not None:
            if proposal.id != proposal_id or proposal.fingerprint != proposal_fingerprint:
                raise ValueError("registry change operation proposal binding is inconsistent")
            if draft is None:
                raise ValueError("registry change operation proposal has no draft")
            RegistryJoinPreparation(
                authoring=authoring,
                draft=draft,
                proposal=proposal,
            )
        if operation == "request_profile":
            valid_shape = (
                draft is None
                and proposal is None
                and response_revision == 0
                and response_fingerprint == authoring.fingerprint
            )
        elif operation in {"finalize_draft", "record_decision"}:
            valid_shape = (
                job is None
                and draft is not None
                and proposal is None
                and response_revision == draft.revision
                and response_fingerprint == draft.fingerprint
            )
        else:
            valid_shape = (
                job is None
                and draft is not None
                and proposal is not None
                and response_revision == draft.revision
                and response_fingerprint == draft.fingerprint
            )
        if not valid_shape:
            raise ValueError("registry change operation response shape is inconsistent")
        audit = self._load_exact_audit(
            connection,
            workspace_id=workspace_id,
            change_id=draft_id,
            event=_OPERATION_AUDIT[operation],
            resulting_revision=response_revision,
        )
        if (
            audit.actor_id != actor_id
            or audit.resulting_fingerprint != response_fingerprint
            or audit.occurred_at != created_at
        ):
            raise ValueError("registry change operation audit is inconsistent")
        _require_operation_audit_payload(
            operation,
            authoring=authoring,
            draft=draft,
            proposal=proposal,
            audit=audit,
        )
        bound = self._load_bound_audit(
            connection,
            workspace_id=workspace_id,
            change_id=draft_id,
        )
        if job is None and operation == "request_profile" and bound is not None:
            raise ValueError("registry change operation has an uncommitted job audit")
        if job is not None:
            if bound is None:
                raise ValueError("registry change operation job has no audit")
            try:
                _require_bound_audit_payload(authoring, job, bound, actor_id=actor_id)
            except RegistryChangeStoreError as error:
                raise ValueError("registry change operation job audit is inconsistent") from error
        return RegistryChangeOperationReplay(
            operation=operation,
            request_fingerprint=request_fingerprint,
            actor_id=actor_id,
            authoring=authoring,
            job=job,
            draft=draft,
            proposal=proposal,
        )

    def _load_exact_audit(
        self,
        connection: psycopg.Connection[Any],
        *,
        workspace_id: str,
        change_id: str,
        event: RegistryChangeAuditEvent,
        resulting_revision: int,
    ) -> RegistryChangeAuditRecord:
        table = self._database.table("semantic_registry_change_audit")
        rows = connection.execute(
            sql.SQL(
                """
                SELECT payload
                FROM {table}
                WHERE workspace_id = %s
                  AND draft_id = %s
                  AND event = %s
                  AND resulting_revision = %s
                LIMIT 2
                """
            ).format(table=table),
            (workspace_id, change_id, event.value, resulting_revision),
        ).fetchall()
        if len(rows) != 1:
            raise ValueError("registry change operation audit is unavailable")
        return RegistryChangeAuditRecord.model_validate(rows[0][0])

    def _load_bound_audit(
        self,
        connection: psycopg.Connection[Any],
        *,
        workspace_id: str,
        change_id: str,
    ) -> RegistryChangeAuditRecord | None:
        table = self._database.table("semantic_registry_change_audit")
        rows = connection.execute(
            sql.SQL(
                """
                SELECT payload
                FROM {table}
                WHERE workspace_id = %s
                  AND draft_id = %s
                  AND event = 'profile_job_bound'
                LIMIT 2
                """
            ).format(table=table),
            (workspace_id, change_id),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise ValueError("registry change profile binding audit is ambiguous")
        return RegistryChangeAuditRecord.model_validate(rows[0][0])

    def _insert_authoring(
        self,
        connection: psycopg.Connection[Any],
        authoring: RegistryJoinProfileAuthoringRequest,
    ) -> None:
        request = authoring.request
        base = request.base_evidence.base_registry
        if base.registry_version is None or base.registry_fingerprint is None:
            raise ValueError("registry change authoring requires an active base")
        table = self._database.table("registry_join_profile_requests")
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {table} (
                    workspace_id, change_id, scan_id, catalog_scope, registry_id,
                    base_registry_version, base_registry_fingerprint,
                    connection_id, proposal_fingerprint,
                    execution_target_fingerprint, profile_request_fingerprint,
                    request_fingerprint, requested_by, payload, requested_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s
                )
                """
            ).format(table=table),
            (
                authoring.workspace_id,
                authoring.id,
                request.scan_id,
                request.scope.catalog_scope,
                request.scope.registry_id,
                base.registry_version,
                base.registry_fingerprint,
                request.proposal.connection_id.root,
                semantic_join_profile_proposal_fingerprint(request.proposal),
                request.execution_target.target_fingerprint,
                request.fingerprint,
                authoring.fingerprint,
                authoring.owner_actor_id,
                Jsonb(authoring.model_dump(mode="json")),
                authoring.created_at,
            ),
        )

    def _insert_draft(
        self,
        connection: psycopg.Connection[Any],
        draft: RegistryJoinChangeDraft,
    ) -> None:
        table = self._database.table("semantic_registry_change_drafts")
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {table} (
                    workspace_id, draft_id, change_kind, catalog_scope,
                    registry_id, owner_actor_id, status, revision, fingerprint,
                    prepared_proposal_id, prepared_proposal_fingerprint,
                    payload, created_at, updated_at
                ) VALUES (
                    %s, %s, 'add_join_v1', %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s
                )
                """
            ).format(table=table),
            (
                draft.workspace_id,
                draft.id,
                draft.scope.catalog_scope,
                draft.scope.registry_id,
                draft.owner_actor_id,
                draft.status.value,
                draft.revision,
                draft.fingerprint,
                draft.prepared_proposal_id,
                draft.prepared_proposal_fingerprint,
                Jsonb(draft.model_dump(mode="json")),
                draft.created_at,
                draft.updated_at,
            ),
        )

    def _cas_draft(
        self,
        connection: psycopg.Connection[Any],
        draft: RegistryJoinChangeDraft,
        *,
        expected_revision: int,
    ) -> None:
        table = self._database.table("semantic_registry_change_drafts")
        if draft.status is RegistryJoinChangeStatus.NEEDS_REVIEW:
            valid_revision = draft.revision == expected_revision + 1
        elif draft.status is RegistryJoinChangeStatus.READY_FOR_PUBLICATION:
            valid_revision = draft.revision == expected_revision
        else:
            valid_revision = False
        if not valid_revision:
            raise _conflict()
        updated = connection.execute(
            sql.SQL(
                """
                UPDATE {table}
                SET status = %s,
                    revision = %s,
                    fingerprint = %s,
                    prepared_proposal_id = %s,
                    prepared_proposal_fingerprint = %s,
                    payload = %s,
                    updated_at = %s
                WHERE workspace_id = %s
                  AND draft_id = %s
                  AND status = 'needs_review'
                  AND revision = %s
                """
            ).format(table=table),
            (
                draft.status.value,
                draft.revision,
                draft.fingerprint,
                draft.prepared_proposal_id,
                draft.prepared_proposal_fingerprint,
                Jsonb(draft.model_dump(mode="json")),
                draft.updated_at,
                draft.workspace_id,
                draft.id,
                expected_revision,
            ),
        )
        if updated.rowcount != 1:
            raise _conflict()

    def _insert_decision(
        self,
        connection: psycopg.Connection[Any],
        draft: RegistryJoinChangeDraft,
    ) -> None:
        decision = draft.decision
        if decision is None:
            raise ValueError("registry join decision is unavailable")
        table = self._database.table("semantic_registry_change_decisions")
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {table} (
                    workspace_id, decision_id, draft_id, resulting_revision,
                    actor_id, action, payload, decided_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """
            ).format(table=table),
            (
                draft.workspace_id,
                decision.id,
                draft.id,
                decision.resulting_version,
                decision.actor,
                decision.action.value,
                Jsonb(decision.model_dump(mode="json")),
                decision.decided_at,
            ),
        )

    def _insert_proposal(
        self,
        connection: psycopg.Connection[Any],
        proposal: PreparedRegistryJoinProposal,
    ) -> None:
        table = self._database.table("semantic_registry_change_proposals")
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {table} (
                    workspace_id, proposal_id, proposal_kind, draft_id,
                    draft_revision, target_registry_version, fingerprint,
                    prepared_by, payload, prepared_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
            ).format(table=table),
            (
                proposal.workspace_id,
                proposal.id,
                proposal.proposal_kind,
                proposal.draft_id,
                proposal.draft_revision,
                proposal.target_registry_version,
                proposal.fingerprint,
                proposal.prepared_by,
                Jsonb(proposal.model_dump(mode="json")),
                proposal.prepared_at,
            ),
        )

    def _insert_audit(
        self,
        connection: psycopg.Connection[Any],
        audit: RegistryChangeAuditRecord,
    ) -> None:
        table = self._database.table("semantic_registry_change_audit")
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {table} (
                    workspace_id, audit_id, draft_id, source_revision,
                    resulting_revision, event, actor_id, fingerprint,
                    payload, occurred_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
            ).format(table=table),
            (
                audit.workspace_id,
                audit.id,
                audit.change_id,
                audit.source_revision,
                audit.resulting_revision,
                audit.event.value,
                audit.actor_id,
                audit.fingerprint,
                Jsonb(audit.model_dump(mode="json")),
                audit.occurred_at,
            ),
        )

    def _insert_operation(
        self,
        connection: psycopg.Connection[Any],
        *,
        authoring: RegistryJoinProfileAuthoringRequest,
        draft: RegistryJoinChangeDraft | None,
        proposal: PreparedRegistryJoinProposal | None,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
        created_at: datetime,
    ) -> None:
        if operation not in _OPERATIONS:
            raise ValueError("registry change operation is unsupported")
        response_revision = 0 if draft is None else draft.revision
        response_fingerprint = authoring.fingerprint if draft is None else draft.fingerprint
        table = self._database.table("semantic_registry_change_operations")
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {table} (
                    workspace_id, actor_id, idempotency_digest, operation,
                    request_fingerprint, draft_id, response_revision,
                    response_fingerprint, response_authoring, response_job_id,
                    response_job, response_draft, response_proposal, proposal_id,
                    proposal_fingerprint, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, NULL, NULL, %s, %s, %s, %s, %s
                )
                """
            ).format(table=table),
            (
                authoring.workspace_id,
                actor_id,
                idempotency_digest,
                operation,
                request_fingerprint,
                authoring.id,
                response_revision,
                response_fingerprint,
                Jsonb(authoring.model_dump(mode="json")),
                None if draft is None else Jsonb(draft.model_dump(mode="json")),
                None if proposal is None else Jsonb(proposal.model_dump(mode="json")),
                None if proposal is None else proposal.id,
                None if proposal is None else proposal.fingerprint,
                created_at,
            ),
        )

    def _lock_authoring(
        self,
        connection: psycopg.Connection[Any],
        authoring: RegistryJoinProfileAuthoringRequest,
    ) -> None:
        table = self._database.table("registry_join_profile_requests")
        row = connection.execute(
            sql.SQL(
                """
                SELECT payload
                FROM {table}
                WHERE workspace_id = %s AND change_id = %s
                """
            ).format(table=table),
            (authoring.workspace_id, authoring.id),
        ).fetchone()
        if row is None or RegistryJoinProfileAuthoringRequest.model_validate(row[0]) != authoring:
            raise _conflict()

    def _lock_profile_job_identity(
        self,
        connection: psycopg.Connection[Any],
        authoring: RegistryJoinProfileAuthoringRequest,
        job: SemanticJoinProfileJob,
    ) -> None:
        persisted = self._load_profile_job_with_connection(connection, authoring)
        if persisted is None:
            raise _conflict()
        _require_same_job_snapshot(persisted, job)

    def _require_bound_profile_audit(
        self,
        connection: psycopg.Connection[Any],
        authoring: RegistryJoinProfileAuthoringRequest,
        job: SemanticJoinProfileJob,
    ) -> None:
        audits = self._database.table("semantic_registry_change_audit")
        operations = self._database.table("semantic_registry_change_operations")
        rows = connection.execute(
            sql.SQL(
                """
                SELECT audit.payload, operation.response_job
                FROM {audits} AS audit
                JOIN {operations} AS operation
                  ON operation.workspace_id = audit.workspace_id
                 AND operation.draft_id = audit.draft_id
                 AND operation.operation = 'request_profile'
                 AND operation.response_job_id = %s
                WHERE audit.workspace_id = %s
                  AND audit.draft_id = %s
                  AND audit.event = 'profile_job_bound'
                LIMIT 2
                """
            ).format(audits=audits, operations=operations),
            (job.job_id, authoring.workspace_id, authoring.id),
        ).fetchall()
        if len(rows) != 1:
            raise _conflict()
        audit = RegistryChangeAuditRecord.model_validate(rows[0][0])
        bound_job = SemanticJoinProfileJob.model_validate(rows[0][1])
        if bound_job.job_id != job.job_id:
            raise _conflict()
        _require_bound_audit_payload(
            authoring,
            bound_job,
            audit,
            actor_id=audit.actor_id,
        )

    def _load_profile_job(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
    ) -> SemanticJoinProfileJob | None:
        try:
            with self._database.connect() as connection:
                return self._load_profile_job_with_connection(connection, authoring)
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except (UndefinedTable, UndefinedColumn, UndefinedFunction) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def _load_profile_job_with_connection(
        self,
        connection: psycopg.Connection[Any],
        authoring: RegistryJoinProfileAuthoringRequest,
    ) -> SemanticJoinProfileJob | None:
        function = self._database.table("load_registry_join_profile_job")
        rows = connection.execute(
            sql.SQL("SELECT {columns} FROM {function}(%s, %s, %s)").format(
                columns=_shared_profile_job_columns,
                function=function,
            ),
            (
                authoring.workspace_id,
                authoring.request.scan_id,
                authoring.fingerprint,
            ),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise ValueError("registry join profile read returned multiple jobs")
        job = _shared_profile_job_from_row(rows[0])
        _require_job_matches_authoring(job, authoring)
        return job


def _authoring_from_row(row: Sequence[object]) -> RegistryJoinProfileAuthoringRequest:
    if len(row) != 15:
        raise ValueError("registry join authoring row has an invalid column count")
    authoring = RegistryJoinProfileAuthoringRequest.model_validate(row[13])
    request = authoring.request
    base = request.base_evidence.base_registry
    expected = (
        authoring.workspace_id,
        authoring.id,
        request.scan_id,
        request.scope.catalog_scope,
        request.scope.registry_id,
        base.registry_version,
        base.registry_fingerprint,
        request.proposal.connection_id.root,
        semantic_join_profile_proposal_fingerprint(request.proposal),
        request.execution_target.target_fingerprint,
        request.fingerprint,
        authoring.fingerprint,
        authoring.owner_actor_id,
        authoring.created_at,
    )
    actual = (
        _required_str(row[0]),
        _required_str(row[1]),
        _required_str(row[2]),
        _required_str(row[3]),
        _required_str(row[4]),
        _required_int(row[5]),
        _required_str(row[6]),
        _required_str(row[7]),
        _required_str(row[8]),
        _required_str(row[9]),
        _required_str(row[10]),
        _required_str(row[11]),
        _required_str(row[12]),
        _required_datetime(row[14]),
    )
    if actual != expected:
        raise ValueError("registry join authoring row disagrees with its payload")
    return authoring


def _draft_from_row(row: Sequence[object]) -> RegistryJoinChangeDraft:
    if len(row) != 14:
        raise ValueError("registry join draft row has an invalid column count")
    draft = RegistryJoinChangeDraft.model_validate(row[11])
    expected = (
        draft.workspace_id,
        draft.id,
        "add_join_v1",
        draft.scope.catalog_scope,
        draft.scope.registry_id,
        draft.owner_actor_id,
        draft.status.value,
        draft.revision,
        draft.fingerprint,
        draft.prepared_proposal_id,
        draft.prepared_proposal_fingerprint,
        draft.created_at,
        draft.updated_at,
    )
    actual = (
        _required_str(row[0]),
        _required_str(row[1]),
        _required_str(row[2]),
        _required_str(row[3]),
        _required_str(row[4]),
        _required_str(row[5]),
        _required_str(row[6]),
        _required_int(row[7]),
        _required_str(row[8]),
        _optional_str(row[9]),
        _optional_str(row[10]),
        _required_datetime(row[12]),
        _required_datetime(row[13]),
    )
    if actual != expected:
        raise ValueError("registry join draft row disagrees with its payload")
    return draft


def _require_job_matches_authoring(
    job: SemanticJoinProfileJob,
    authoring: RegistryJoinProfileAuthoringRequest,
) -> None:
    request = authoring.request
    if (
        job.workspace_id != authoring.workspace_id
        or job.scan_id != request.scan_id
        or job.bound_proposal != request.proposal
        or job.proposal_fingerprint != semantic_join_profile_proposal_fingerprint(request.proposal)
        or job.execution_target != request.execution_target
        or job.connector_contract_version is None
        or job.requested_at != request.requested_at
    ):
        raise ValueError("registry join profile job crossed its durable request")


def _require_same_job_snapshot(
    persisted: SemanticJoinProfileJob,
    supplied: SemanticJoinProfileJob,
) -> None:
    if persisted != supplied:
        raise _conflict()


def _require_profile_requested_audit(
    authoring: RegistryJoinProfileAuthoringRequest,
    audit: RegistryChangeAuditRecord,
    *,
    actor_id: str,
) -> None:
    if (
        audit.workspace_id != authoring.workspace_id
        or audit.change_id != authoring.id
        or audit.event is not RegistryChangeAuditEvent.PROFILE_REQUESTED
        or audit.actor_id != actor_id
        or audit.occurred_at != authoring.created_at
        or audit.resulting_fingerprint != authoring.fingerprint
        or audit.profile_request_fingerprint != authoring.request.fingerprint
    ):
        raise _conflict()


def _require_profile_bound_audit(
    authoring: RegistryJoinProfileAuthoringRequest,
    job: SemanticJoinProfileJob,
    audit: RegistryChangeAuditRecord,
    *,
    actor_id: str,
) -> None:
    try:
        _require_job_matches_authoring(job, authoring)
    except ValueError as error:
        raise _conflict() from error
    _require_bound_audit_payload(authoring, job, audit, actor_id=actor_id)


def _require_bound_audit_payload(
    authoring: RegistryJoinProfileAuthoringRequest,
    job: SemanticJoinProfileJob,
    audit: RegistryChangeAuditRecord,
    *,
    actor_id: str,
) -> None:
    if (
        audit.workspace_id != authoring.workspace_id
        or audit.change_id != authoring.id
        or audit.event is not RegistryChangeAuditEvent.PROFILE_JOB_BOUND
        or audit.actor_id != actor_id
        or audit.previous_fingerprint != authoring.fingerprint
        or audit.resulting_fingerprint != authoring.fingerprint
        or audit.profile_request_fingerprint != authoring.request.fingerprint
        or audit.profile_job_id != job.job_id
    ):
        raise _conflict()


def _require_draft_finalized_audit(
    authoring: RegistryJoinProfileAuthoringRequest,
    draft: RegistryJoinChangeDraft,
    audit: RegistryChangeAuditRecord,
    *,
    actor_id: str,
) -> None:
    try:
        RegistryJoinDraftMutation(authoring=authoring, draft=draft)
    except ValidationError as error:
        raise _conflict() from error
    if (
        draft.status is not RegistryJoinChangeStatus.NEEDS_REVIEW
        or draft.revision != 1
        or audit.workspace_id != authoring.workspace_id
        or audit.change_id != authoring.id
        or audit.event is not RegistryChangeAuditEvent.DRAFT_FINALIZED
        or audit.actor_id != actor_id
        or audit.source_revision != 0
        or audit.resulting_revision != draft.revision
        or audit.previous_fingerprint != authoring.fingerprint
        or audit.resulting_fingerprint != draft.fingerprint
        or audit.profile_request_fingerprint != authoring.request.fingerprint
        or audit.profile_job_id != draft.profile_job.job_id
    ):
        raise _conflict()


def _require_decision_audit(
    authoring: RegistryJoinProfileAuthoringRequest,
    draft: RegistryJoinChangeDraft,
    audit: RegistryChangeAuditRecord,
    *,
    actor_id: str,
) -> None:
    decision = draft.decision
    if (
        decision is None
        or draft.status is not RegistryJoinChangeStatus.NEEDS_REVIEW
        or audit.workspace_id != authoring.workspace_id
        or audit.change_id != authoring.id
        or audit.event is not RegistryChangeAuditEvent.DECISION_RECORDED
        or audit.actor_id != actor_id
        or decision.actor != actor_id
        or audit.resulting_revision != draft.revision
        or audit.resulting_fingerprint != draft.fingerprint
        or audit.decision_id != decision.id
    ):
        raise _conflict()


def _require_preparation_audit(
    authoring: RegistryJoinProfileAuthoringRequest,
    draft: RegistryJoinChangeDraft,
    proposal: PreparedRegistryJoinProposal,
    audit: RegistryChangeAuditRecord,
    *,
    actor_id: str,
) -> None:
    try:
        RegistryJoinPreparation(
            authoring=authoring,
            draft=draft,
            proposal=proposal,
        )
    except ValidationError as error:
        raise _conflict() from error
    if (
        draft.status is not RegistryJoinChangeStatus.READY_FOR_PUBLICATION
        or audit.workspace_id != authoring.workspace_id
        or audit.change_id != authoring.id
        or audit.event is not RegistryChangeAuditEvent.PUBLICATION_PREPARED
        or audit.actor_id != actor_id
        or proposal.prepared_by != actor_id
        or audit.resulting_revision != draft.revision
        or audit.resulting_fingerprint != draft.fingerprint
        or audit.proposal_id != proposal.id
    ):
        raise _conflict()


def _require_operation_audit_payload(
    operation: str,
    *,
    authoring: RegistryJoinProfileAuthoringRequest,
    draft: RegistryJoinChangeDraft | None,
    proposal: PreparedRegistryJoinProposal | None,
    audit: RegistryChangeAuditRecord,
) -> None:
    if operation == "request_profile":
        valid = (
            audit.profile_request_fingerprint == authoring.request.fingerprint
            and audit.previous_fingerprint is None
        )
    elif operation == "finalize_draft":
        valid = (
            draft is not None
            and audit.profile_request_fingerprint == authoring.request.fingerprint
            and audit.profile_job_id == draft.profile_job.job_id
            and audit.previous_fingerprint == authoring.fingerprint
        )
    elif operation == "record_decision":
        valid = (
            draft is not None
            and draft.decision is not None
            and audit.decision_id == draft.decision.id
        )
    else:
        valid = proposal is not None and audit.proposal_id == proposal.id
    if not valid:
        raise ValueError("registry change operation audit payload is inconsistent")


def _required_str(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("registry change row text is invalid")
    return value


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return _required_str(value)


def _required_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("registry change row integer is invalid")
    return value


def _required_datetime(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("registry change row timestamp is invalid")
    return value


def _invalid_response() -> RegistryChangeStoreError:
    return RegistryChangeStoreError(
        RegistryChangeStoreErrorCode.INVALID_RESPONSE,
        "Registry change persistence returned an invalid response.",
    )


def _conflict() -> RegistryChangeStoreError:
    return RegistryChangeStoreError(
        RegistryChangeStoreErrorCode.CONFLICT,
        "Registry change persistence conflicts with durable state.",
    )


def _unavailable() -> RegistryChangeStoreError:
    return RegistryChangeStoreError(
        RegistryChangeStoreErrorCode.UNAVAILABLE,
        "Registry change persistence is unavailable.",
    )


def _queue_invalid_response() -> SemanticJoinProfileQueueError:
    return SemanticJoinProfileQueueError(
        SemanticJoinProfileQueueErrorCode.INVALID_RESPONSE,
        "Registry join profile persistence returned an invalid response.",
    )


def _queue_state_conflict() -> SemanticJoinProfileQueueError:
    return SemanticJoinProfileQueueError(
        SemanticJoinProfileQueueErrorCode.STATE_CONFLICT,
        "Registry join profile request conflicts with durable state.",
    )


def _queue_unavailable() -> SemanticJoinProfileQueueError:
    return SemanticJoinProfileQueueError(
        SemanticJoinProfileQueueErrorCode.STORE_UNAVAILABLE,
        "Registry join profile persistence is unavailable.",
    )


__all__ = [
    "PostgresRegistryChangeStore",
    "PostgresRegistryJoinProfileRequestQueue",
]
