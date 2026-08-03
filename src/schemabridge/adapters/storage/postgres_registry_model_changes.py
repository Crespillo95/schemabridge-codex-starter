"""PostgreSQL persistence and current-head readers for M35 Phase-B changes."""

from __future__ import annotations

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
    _JOB_COLUMNS as _profile_job_columns,
)
from schemabridge.adapters.semantic_change.postgres_profile_queue import (
    _profile_job_from_row,
)
from schemabridge.adapters.storage.postgres import ControlConnectionProvider, _ControlDatabase
from schemabridge.application.ports.registry_model_changes import (
    RegistryModelChangeOperationReplay,
    RegistryModelChangePortError,
    RegistryModelChangePortErrorCode,
    RegistryModelProfileOperationReplay,
)
from schemabridge.application.ports.semantic_profile_jobs import (
    SemanticJoinProfileQueueError,
    SemanticJoinProfileQueueErrorCode,
)
from schemabridge.domain.registry_model_change_authoring import (
    MAX_REGISTRY_MODEL_CHANGE_HISTORY,
    RegistryModelChangeAuditRecord,
    RegistryModelChangeDraft,
    RegistryModelChangeMutation,
    RegistryModelJoinProfileAuditRecord,
    RegistryModelJoinProfileAuthoringRequest,
    RegistryModelJoinProfileMutation,
    RegistryModelReplacementSourceEvidence,
)
from schemabridge.domain.registry_model_changes import (
    PreparedRegistryModelReplacementProposal,
    RegistryModelJoinProfileWitness,
)
from schemabridge.domain.semantic_change import (
    SemanticChangeImpact,
    SemanticChangeInspectionContext,
    SemanticChangeReport,
    SemanticImpactSet,
)
from schemabridge.domain.semantic_onboarding import (
    PreparedSemanticOnboardingProposal,
    SemanticOnboardingDecision,
)
from schemabridge.domain.semantic_profile_jobs import (
    SemanticJoinProfileJob,
    SemanticJoinProfileJobStatus,
    SemanticJoinProfileSubmission,
    semantic_join_profile_proposal_fingerprint,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope

_MODEL_OPERATIONS = frozenset(
    {
        "create_model_change",
        "decide_model_change",
        "prepare_model_change_publication",
    }
)
_PROFILE_OPERATIONS = frozenset({"request_model_join_profile", "finalize_model_join_profile"})


@dataclass(frozen=True, slots=True)
class PostgresRegistryModelJoinProfileQueue:
    """API-only definer queue for an already durable candidate-profile request."""

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
        authoring: RegistryModelJoinProfileAuthoringRequest,
        *,
        max_attempts: int = 5,
    ) -> SemanticJoinProfileSubmission:
        if not 1 <= max_attempts <= 100:
            raise ValueError("registry model profile max attempts is invalid")
        replay = self.load(authoring)
        if replay is not None:
            _require_job_matches(authoring, replay)
            return SemanticJoinProfileSubmission(job=replay, replayed=True)
        function = self._database.table("enqueue_registry_model_join_profile")
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
                raise _queue_invalid()
            persisted = self.load(authoring)
            if persisted is None or persisted.job_id != row[0]:
                raise _queue_invalid()
            _require_job_matches(authoring, persisted)
            return SemanticJoinProfileSubmission(job=persisted, replayed=False)
        except SemanticJoinProfileQueueError:
            raise
        except ObjectNotInPrerequisiteState as error:
            raise _queue_conflict() from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _queue_invalid() from error
        except (UndefinedColumn, UndefinedFunction, UndefinedTable) as error:
            raise _queue_unavailable() from error
        except psycopg.Error as error:
            raise _queue_unavailable() from error

    def load(
        self,
        authoring: RegistryModelJoinProfileAuthoringRequest,
    ) -> SemanticJoinProfileJob | None:
        function = self._database.table("load_registry_model_join_profile_job")
        try:
            with self._database.connect() as connection:
                rows = connection.execute(
                    sql.SQL("SELECT {columns} FROM {function}(%s, %s, %s)").format(
                        columns=_profile_job_columns,
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
                raise _queue_invalid()
            job = _profile_job_from_row(rows[0])
            _require_job_matches(authoring, job)
            return job
        except SemanticJoinProfileQueueError:
            raise
        except (ValidationError, TypeError, ValueError) as error:
            raise _queue_invalid() from error
        except (UndefinedColumn, UndefinedFunction, UndefinedTable) as error:
            raise _queue_unavailable() from error
        except psycopg.Error as error:
            raise _queue_unavailable() from error


@dataclass(frozen=True, slots=True)
class PostgresRegistryModelProfileStore:
    """Append-only request/job/witness bindings for candidate relationships."""

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
    ) -> RegistryModelProfileOperationReplay | None:
        table = self._database.table("registry_model_join_profile_operations")
        try:
            with self._database.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT operation, request_fingerprint, actor_id,
                               response_authoring, response_job, response_witness
                        FROM {table}
                        WHERE workspace_id = %s AND idempotency_digest = %s
                        """
                    ).format(table=table),
                    (workspace_id, idempotency_digest),
                ).fetchone()
            if row is None:
                return None
            if len(row) != 6:
                raise ValueError("registry model profile operation shape is invalid")
            operation = _text(row[0])
            request_fingerprint = _text(row[1])
            actor_id = _text(row[2])
            authoring = RegistryModelJoinProfileAuthoringRequest.model_validate(row[3])
            job = None if row[4] is None else SemanticJoinProfileJob.model_validate(row[4])
            witness = (
                None if row[5] is None else RegistryModelJoinProfileWitness.model_validate(row[5])
            )
            if operation not in _PROFILE_OPERATIONS or authoring.workspace_id != workspace_id:
                raise ValueError("registry model profile operation identity is invalid")
            if job is not None:
                _require_job_matches(authoring, job)
            if witness is not None:
                RegistryModelJoinProfileMutation(
                    authoring=authoring,
                    job=job,
                    witness=witness,
                )
            return RegistryModelProfileOperationReplay(
                operation=operation,
                request_fingerprint=request_fingerprint,
                actor_id=actor_id,
                authoring=authoring,
                job=job,
                witness=witness,
            )
        except RegistryModelChangePortError:
            raise
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except (UndefinedColumn, UndefinedTable) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def persist_request(
        self,
        authoring: RegistryModelJoinProfileAuthoringRequest,
        audit: RegistryModelJoinProfileAuditRecord,
        *,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryModelJoinProfileMutation:
        replay = self._exact_replay(
            authoring.workspace_id,
            idempotency_digest,
            operation=operation,
            actor_id=actor_id,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            return RegistryModelJoinProfileMutation(
                authoring=replay.authoring,
                job=replay.job,
                witness=replay.witness,
                replayed=True,
            )
        _require_profile_audit(authoring, audit, actor_id=actor_id)
        request = authoring.request
        base_registry = request.base.base_registry
        if base_registry.registry_version is None or base_registry.registry_fingerprint is None:
            raise _invalid_response()
        try:
            with self._database.connect() as connection, connection.transaction():
                requests = self._database.table("registry_model_join_profile_requests")
                connection.execute(
                    sql.SQL(
                        """
                        INSERT INTO {requests} (
                            workspace_id, request_id, change_id, scan_id,
                            source_proposal_id, source_proposal_fingerprint,
                            catalog_scope, registry_id, base_registry_version,
                            base_registry_fingerprint, base_fingerprint,
                            incident_join_id, connection_id, proposal_fingerprint,
                            execution_target_fingerprint, authoring_fingerprint,
                            requested_by, payload, requested_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        """
                    ).format(requests=requests),
                    (
                        authoring.workspace_id,
                        authoring.id,
                        request.change_id,
                        request.scan_id,
                        request.replacement_source.proposal.id,
                        request.replacement_source.proposal.fingerprint,
                        request.base.scope.catalog_scope,
                        request.base.scope.registry_id,
                        base_registry.registry_version,
                        base_registry.registry_fingerprint,
                        request.base.fingerprint,
                        request.incident_join_id,
                        request.connection_id.root,
                        semantic_join_profile_proposal_fingerprint(request.bound_proposal),
                        request.execution_target.target_fingerprint,
                        authoring.fingerprint,
                        authoring.owner_actor_id,
                        Jsonb(authoring.model_dump(mode="json")),
                        authoring.created_at,
                    ),
                )
                self._insert_profile_audit(connection, audit)
                self._insert_profile_operation(
                    connection,
                    authoring=authoring,
                    job=None,
                    witness=None,
                    operation=operation,
                    actor_id=actor_id,
                    idempotency_digest=idempotency_digest,
                    request_fingerprint=request_fingerprint,
                    created_at=audit.occurred_at,
                )
            return RegistryModelJoinProfileMutation(authoring=authoring)
        except UniqueViolation as error:
            replay = self._exact_replay(
                authoring.workspace_id,
                idempotency_digest,
                operation=operation,
                actor_id=actor_id,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None:
                return RegistryModelJoinProfileMutation(
                    authoring=replay.authoring,
                    job=replay.job,
                    witness=replay.witness,
                    replayed=True,
                )
            raise _conflict() from error
        except RegistryModelChangePortError:
            raise
        except (
            CheckViolation,
            ForeignKeyViolation,
            ValidationError,
            TypeError,
            ValueError,
        ) as error:
            raise _invalid_response() from error
        except (UndefinedColumn, UndefinedFunction, UndefinedTable) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def bind_job(
        self,
        authoring: RegistryModelJoinProfileAuthoringRequest,
        job: SemanticJoinProfileJob,
        audit: RegistryModelJoinProfileAuditRecord,
        *,
        idempotency_digest: str,
    ) -> RegistryModelJoinProfileMutation:
        _require_job_matches(authoring, job)
        if job.status is not SemanticJoinProfileJobStatus.REQUESTED or job.lease is not None:
            raise _conflict()
        replay = self.load_operation_replay(authoring.workspace_id, idempotency_digest)
        if (
            replay is None
            or replay.operation != "request_model_join_profile"
            or replay.authoring != authoring
            or replay.actor_id != audit.actor_id
            or replay.witness is not None
        ):
            raise _conflict()
        if replay.job is not None:
            if replay.job != job:
                raise _conflict()
            return RegistryModelJoinProfileMutation(authoring=authoring, job=job, replayed=True)
        _require_profile_audit(authoring, audit, actor_id=audit.actor_id)
        try:
            with self._database.connect() as connection, connection.transaction():
                self._lock_profile_request(
                    connection,
                    authoring,
                    idempotency_digest=idempotency_digest,
                )
                persisted = self._load_profile_job(connection, authoring)
                if persisted != job:
                    raise _conflict()
                self._insert_profile_audit(connection, audit)
                operations = self._database.table("registry_model_join_profile_operations")
                updated = connection.execute(
                    sql.SQL(
                        """
                        UPDATE {operations}
                        SET response_job_id = %s, response_job = %s
                        WHERE workspace_id = %s
                          AND idempotency_digest = %s
                          AND operation = 'request_model_join_profile'
                          AND response_authoring = %s
                          AND response_job_id IS NULL
                        """
                    ).format(operations=operations),
                    (
                        job.job_id,
                        Jsonb(job.model_dump(mode="json")),
                        authoring.workspace_id,
                        idempotency_digest,
                        Jsonb(authoring.model_dump(mode="json")),
                    ),
                )
                if updated.rowcount != 1:
                    raise _conflict()
            return RegistryModelJoinProfileMutation(authoring=authoring, job=job)
        except RegistryModelChangePortError as error:
            bound_replay = self._bound_job_replay(
                authoring,
                job,
                actor_id=audit.actor_id,
                idempotency_digest=idempotency_digest,
            )
            if bound_replay is not None:
                return bound_replay
            raise error
        except (CheckViolation, ForeignKeyViolation, UniqueViolation) as error:
            bound_replay = self._bound_job_replay(
                authoring,
                job,
                actor_id=audit.actor_id,
                idempotency_digest=idempotency_digest,
            )
            if bound_replay is not None:
                return bound_replay
            raise _conflict() from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except (UndefinedColumn, UndefinedFunction, UndefinedTable) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def load_request(
        self,
        workspace_id: str,
        request_id: str,
    ) -> RegistryModelJoinProfileAuthoringRequest | None:
        table = self._database.table("registry_model_join_profile_requests")
        try:
            with self._database.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        "SELECT payload FROM {table} WHERE workspace_id = %s AND request_id = %s"
                    ).format(table=table),
                    (workspace_id, request_id),
                ).fetchone()
            if row is None:
                return None
            value = RegistryModelJoinProfileAuthoringRequest.model_validate(row[0])
            if value.workspace_id != workspace_id or value.id != request_id:
                raise ValueError("registry model profile request identity is invalid")
            return value
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except (UndefinedColumn, UndefinedTable) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def _bound_job_replay(
        self,
        authoring: RegistryModelJoinProfileAuthoringRequest,
        job: SemanticJoinProfileJob,
        *,
        actor_id: str,
        idempotency_digest: str,
    ) -> RegistryModelJoinProfileMutation | None:
        """Re-read an exact winner after a concurrent bind transaction commits."""

        replay = self.load_operation_replay(authoring.workspace_id, idempotency_digest)
        if replay is None or replay.job is None:
            return None
        if (
            replay.operation != "request_model_join_profile"
            or replay.authoring != authoring
            or replay.actor_id != actor_id
            or replay.job != job
            or replay.witness is not None
        ):
            raise _conflict()
        return RegistryModelJoinProfileMutation(
            authoring=authoring,
            job=job,
            replayed=True,
        )

    def record_witness(
        self,
        authoring: RegistryModelJoinProfileAuthoringRequest,
        job: SemanticJoinProfileJob,
        witness: RegistryModelJoinProfileWitness,
        audit: RegistryModelJoinProfileAuditRecord,
        *,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryModelJoinProfileMutation:
        replay = self._exact_replay(
            authoring.workspace_id,
            idempotency_digest,
            operation=operation,
            actor_id=actor_id,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            if replay.job is None or replay.witness is None:
                raise _conflict()
            return RegistryModelJoinProfileMutation(
                authoring=replay.authoring,
                job=replay.job,
                witness=replay.witness,
                replayed=True,
            )
        _require_job_matches(authoring, job)
        if job.status is not SemanticJoinProfileJobStatus.COMPLETED or job.result is None:
            raise _conflict()
        RegistryModelJoinProfileMutation(authoring=authoring, job=job, witness=witness)
        _require_profile_audit(authoring, audit, actor_id=actor_id)
        try:
            with self._database.connect() as connection, connection.transaction():
                persisted = self._load_profile_job(connection, authoring)
                if persisted != job:
                    raise _conflict()
                requests = self._database.table("registry_model_join_profile_operations")
                bound = connection.execute(
                    sql.SQL(
                        """
                        SELECT 1 FROM {operations}
                        WHERE workspace_id = %s
                          AND request_id = %s
                          AND operation = 'request_model_join_profile'
                          AND response_job_id = %s
                          AND response_authoring = %s
                        """
                    ).format(operations=requests),
                    (
                        authoring.workspace_id,
                        authoring.id,
                        job.job_id,
                        Jsonb(authoring.model_dump(mode="json")),
                    ),
                ).fetchone()
                if bound is None:
                    raise _conflict()
                witnesses = self._database.table("registry_model_join_profile_witnesses")
                connection.execute(
                    sql.SQL(
                        """
                        INSERT INTO {witnesses} (
                            workspace_id, request_id, change_id,
                            source_proposal_id, incident_join_id, job_id,
                            result_fingerprint, witness_fingerprint,
                            completed_at, expires_at, payload, recorded_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s
                        )
                        """
                    ).format(witnesses=witnesses),
                    (
                        authoring.workspace_id,
                        authoring.id,
                        witness.change_id,
                        witness.source_replacement_proposal_id,
                        witness.proposal.id,
                        job.job_id,
                        job.result.fingerprint,
                        witness.fingerprint,
                        witness.result.completed_at,
                        witness.expires_at,
                        Jsonb(witness.model_dump(mode="json")),
                        audit.occurred_at,
                    ),
                )
                self._insert_profile_audit(connection, audit)
                self._insert_profile_operation(
                    connection,
                    authoring=authoring,
                    job=job,
                    witness=witness,
                    operation=operation,
                    actor_id=actor_id,
                    idempotency_digest=idempotency_digest,
                    request_fingerprint=request_fingerprint,
                    created_at=audit.occurred_at,
                )
            return RegistryModelJoinProfileMutation(
                authoring=authoring,
                job=job,
                witness=witness,
            )
        except UniqueViolation as error:
            replay = self._exact_replay(
                authoring.workspace_id,
                idempotency_digest,
                operation=operation,
                actor_id=actor_id,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None and replay.job is not None and replay.witness is not None:
                return RegistryModelJoinProfileMutation(
                    authoring=replay.authoring,
                    job=replay.job,
                    witness=replay.witness,
                    replayed=True,
                )
            raise _conflict() from error
        except RegistryModelChangePortError:
            raise
        except (CheckViolation, ForeignKeyViolation) as error:
            raise _conflict() from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except (UndefinedColumn, UndefinedFunction, UndefinedTable) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def load(
        self,
        workspace_id: str,
        change_id: str,
        replacement_proposal_id: str,
        join_id: str,
    ) -> RegistryModelJoinProfileWitness | None:
        function = self._database.table("load_current_registry_model_join_witness")
        try:
            with self._database.connect() as connection:
                rows = connection.execute(
                    sql.SQL("SELECT witness FROM {function}(%s, %s, %s, %s)").format(
                        function=function
                    ),
                    (workspace_id, change_id, replacement_proposal_id, join_id),
                ).fetchall()
            if not rows:
                return None
            if len(rows) != 1:
                raise ValueError("registry model witness head is ambiguous")
            witness = RegistryModelJoinProfileWitness.model_validate(rows[0][0])
            if (
                witness.change_id != change_id
                or witness.source_replacement_proposal_id != replacement_proposal_id
                or witness.proposal.id != join_id
            ):
                raise ValueError("registry model witness crossed its head")
            return witness
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except (UndefinedColumn, UndefinedFunction, UndefinedTable) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def _exact_replay(
        self,
        workspace_id: str,
        idempotency_digest: str,
        *,
        operation: str,
        actor_id: str,
        request_fingerprint: str,
    ) -> RegistryModelProfileOperationReplay | None:
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

    def _insert_profile_audit(
        self,
        connection: psycopg.Connection[Any],
        audit: RegistryModelJoinProfileAuditRecord,
    ) -> None:
        table = self._database.table("registry_model_join_profile_audit")
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {table} (
                    workspace_id, audit_id, request_id, event, actor_id,
                    job_id, witness_fingerprint, fingerprint, payload, occurred_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
            ).format(table=table),
            (
                audit.workspace_id,
                audit.id,
                audit.request_id,
                audit.event.value,
                audit.actor_id,
                audit.job_id,
                audit.witness_fingerprint,
                audit.fingerprint,
                Jsonb(audit.model_dump(mode="json")),
                audit.occurred_at,
            ),
        )

    def _insert_profile_operation(
        self,
        connection: psycopg.Connection[Any],
        *,
        authoring: RegistryModelJoinProfileAuthoringRequest,
        job: SemanticJoinProfileJob | None,
        witness: RegistryModelJoinProfileWitness | None,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
        created_at: datetime,
    ) -> None:
        if operation not in _PROFILE_OPERATIONS:
            raise ValueError("registry model profile operation is unsupported")
        table = self._database.table("registry_model_join_profile_operations")
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {table} (
                    workspace_id, actor_id, idempotency_digest, operation,
                    request_fingerprint, request_id, response_authoring,
                    response_job_id, response_job, response_witness,
                    witness_fingerprint, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
            ).format(table=table),
            (
                authoring.workspace_id,
                actor_id,
                idempotency_digest,
                operation,
                request_fingerprint,
                authoring.id,
                Jsonb(authoring.model_dump(mode="json")),
                None if job is None else job.job_id,
                None if job is None else Jsonb(job.model_dump(mode="json")),
                None if witness is None else Jsonb(witness.model_dump(mode="json")),
                None if witness is None else witness.fingerprint,
                created_at,
            ),
        )

    def _lock_profile_request(
        self,
        connection: psycopg.Connection[Any],
        authoring: RegistryModelJoinProfileAuthoringRequest,
        *,
        idempotency_digest: str,
    ) -> None:
        table = self._database.table("registry_model_join_profile_operations")
        row = connection.execute(
            sql.SQL(
                "SELECT response_authoring FROM {table} "
                "WHERE workspace_id = %s AND idempotency_digest = %s "
                "AND operation = 'request_model_join_profile' FOR UPDATE"
            ).format(table=table),
            (authoring.workspace_id, idempotency_digest),
        ).fetchone()
        if (
            row is None
            or RegistryModelJoinProfileAuthoringRequest.model_validate(row[0]) != authoring
        ):
            raise _conflict()

    def _load_profile_job(
        self,
        connection: psycopg.Connection[Any],
        authoring: RegistryModelJoinProfileAuthoringRequest,
    ) -> SemanticJoinProfileJob | None:
        function = self._database.table("load_registry_model_join_profile_job")
        rows = connection.execute(
            sql.SQL("SELECT {columns} FROM {function}(%s, %s, %s)").format(
                columns=_profile_job_columns,
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
            raise ValueError("registry model profile job is ambiguous")
        job = _profile_job_from_row(rows[0])
        _require_job_matches(authoring, job)
        return job


@dataclass(frozen=True, slots=True)
class PostgresRegistryModelChangeStore:
    """CAS model-change store with immutable decisions, audit, proposals, and replay."""

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
    ) -> RegistryModelChangeOperationReplay | None:
        table = self._database.table("semantic_registry_model_change_operations")
        try:
            with self._database.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT operation, request_fingerprint, actor_id,
                               response_draft, response_proposal
                        FROM {table}
                        WHERE workspace_id = %s AND idempotency_digest = %s
                        """
                    ).format(table=table),
                    (workspace_id, idempotency_digest),
                ).fetchone()
            if row is None:
                return None
            if len(row) != 5:
                raise ValueError("registry model operation shape is invalid")
            operation = _text(row[0])
            request_fingerprint = _text(row[1])
            actor_id = _text(row[2])
            draft = RegistryModelChangeDraft.model_validate(row[3])
            proposal = (
                None
                if row[4] is None
                else PreparedRegistryModelReplacementProposal.model_validate(row[4])
            )
            if operation not in _MODEL_OPERATIONS or draft.workspace_id != workspace_id:
                raise ValueError("registry model operation identity is invalid")
            RegistryModelChangeMutation(draft=draft, proposal=proposal)
            return RegistryModelChangeOperationReplay(
                operation=operation,
                request_fingerprint=request_fingerprint,
                actor_id=actor_id,
                draft=draft,
                proposal=proposal,
            )
        except RegistryModelChangePortError:
            raise
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except (UndefinedColumn, UndefinedTable) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def create(
        self,
        draft: RegistryModelChangeDraft,
        audit: RegistryModelChangeAuditRecord,
        *,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryModelChangeMutation:
        replay = self._exact_replay(
            draft.workspace_id,
            idempotency_digest,
            operation=operation,
            actor_id=actor_id,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            if replay.draft != draft or replay.proposal is not None:
                raise _conflict()
            return RegistryModelChangeMutation(draft=replay.draft, replayed=True)
        _require_change_audit(draft, audit, actor_id=actor_id)
        if draft.revision != 1 or draft.outer_decision is not None:
            raise _conflict()
        try:
            with self._database.connect() as connection, connection.transaction():
                self._insert_draft(connection, draft)
                self._insert_change_audit(connection, audit)
                self._insert_change_operation(
                    connection,
                    draft=draft,
                    proposal=None,
                    operation=operation,
                    actor_id=actor_id,
                    idempotency_digest=idempotency_digest,
                    request_fingerprint=request_fingerprint,
                    created_at=audit.occurred_at,
                )
            return RegistryModelChangeMutation(draft=draft)
        except UniqueViolation as error:
            replay = self._exact_replay(
                draft.workspace_id,
                idempotency_digest,
                operation=operation,
                actor_id=actor_id,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None and replay.draft == draft and replay.proposal is None:
                return RegistryModelChangeMutation(draft=replay.draft, replayed=True)
            raise _conflict() from error
        except RegistryModelChangePortError:
            raise
        except ObjectNotInPrerequisiteState as error:
            raise _conflict() from error
        except (
            CheckViolation,
            ForeignKeyViolation,
            ValidationError,
            TypeError,
            ValueError,
        ) as error:
            raise _invalid_response() from error
        except (UndefinedColumn, UndefinedFunction, UndefinedTable) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def load(self, workspace_id: str, change_id: str) -> RegistryModelChangeDraft | None:
        table = self._database.table("semantic_registry_model_change_drafts")
        try:
            with self._database.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        "SELECT payload FROM {table} WHERE workspace_id = %s AND draft_id = %s"
                    ).format(table=table),
                    (workspace_id, change_id),
                ).fetchone()
            if row is None:
                return None
            draft = RegistryModelChangeDraft.model_validate(row[0])
            if draft.workspace_id != workspace_id or draft.id != change_id:
                raise ValueError("registry model draft identity is invalid")
            return draft
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except (UndefinedColumn, UndefinedTable) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def list_for_workspace(
        self,
        workspace_id: str,
        *,
        owner_actor_id: str | None,
        limit: int,
    ) -> tuple[RegistryModelChangeDraft, ...]:
        if not 1 <= limit <= 50:
            raise _invalid_response()
        table = self._database.table("semantic_registry_model_change_drafts")
        owner = sql.SQL("") if owner_actor_id is None else sql.SQL(" AND owner_actor_id = %s")
        parameters: list[object] = [workspace_id]
        if owner_actor_id is not None:
            parameters.append(owner_actor_id)
        parameters.append(limit)
        try:
            with self._database.connect() as connection:
                rows = connection.execute(
                    sql.SQL(
                        """
                        SELECT payload FROM {table}
                        WHERE workspace_id = %s{owner}
                        ORDER BY updated_at DESC, draft_id DESC
                        LIMIT %s
                        """
                    ).format(table=table, owner=owner),
                    tuple(parameters),
                ).fetchall()
            return tuple(RegistryModelChangeDraft.model_validate(row[0]) for row in rows)
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except (UndefinedColumn, UndefinedTable) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def commit_decision(
        self,
        draft: RegistryModelChangeDraft,
        audit: RegistryModelChangeAuditRecord,
        *,
        expected_revision: int,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryModelChangeMutation:
        if draft.outer_decision is None:
            raise _conflict()
        replay = self._exact_replay(
            draft.workspace_id,
            idempotency_digest,
            operation=operation,
            actor_id=actor_id,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            if replay.draft != draft or replay.proposal is not None:
                raise _conflict()
            return RegistryModelChangeMutation(draft=replay.draft, replayed=True)
        _require_change_audit(draft, audit, actor_id=actor_id)
        try:
            with self._database.connect() as connection, connection.transaction():
                self._cas_draft(connection, draft, expected_revision=expected_revision)
                self._insert_outer_decision(connection, draft)
                self._insert_change_audit(connection, audit)
                self._insert_change_operation(
                    connection,
                    draft=draft,
                    proposal=None,
                    operation=operation,
                    actor_id=actor_id,
                    idempotency_digest=idempotency_digest,
                    request_fingerprint=request_fingerprint,
                    created_at=audit.occurred_at,
                )
            return RegistryModelChangeMutation(draft=draft)
        except UniqueViolation as error:
            replay = self._exact_replay(
                draft.workspace_id,
                idempotency_digest,
                operation=operation,
                actor_id=actor_id,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None and replay.draft == draft and replay.proposal is None:
                return RegistryModelChangeMutation(draft=replay.draft, replayed=True)
            raise _conflict() from error
        except RegistryModelChangePortError as error:
            replay = self._exact_replay(
                draft.workspace_id,
                idempotency_digest,
                operation=operation,
                actor_id=actor_id,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None and replay.draft == draft and replay.proposal is None:
                return RegistryModelChangeMutation(draft=replay.draft, replayed=True)
            raise error
        except (ObjectNotInPrerequisiteState, CheckViolation, ForeignKeyViolation) as error:
            raise _conflict() from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except (UndefinedColumn, UndefinedFunction, UndefinedTable) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def commit_preparation(
        self,
        draft: RegistryModelChangeDraft,
        proposal: PreparedRegistryModelReplacementProposal,
        audit: RegistryModelChangeAuditRecord,
        *,
        expected_revision: int,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryModelChangeMutation:
        RegistryModelChangeMutation(draft=draft, proposal=proposal)
        replay = self._exact_replay(
            draft.workspace_id,
            idempotency_digest,
            operation=operation,
            actor_id=actor_id,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            if replay.draft != draft or replay.proposal != proposal:
                raise _conflict()
            return RegistryModelChangeMutation(
                draft=replay.draft,
                proposal=replay.proposal,
                replayed=True,
            )
        _require_change_audit(draft, audit, actor_id=actor_id)
        if (
            proposal.draft_revision != expected_revision
            or proposal.draft_id != draft.id
            or draft.revision != expected_revision + 1
        ):
            raise _conflict()
        try:
            with self._database.connect() as connection, connection.transaction():
                self._cas_draft(connection, draft, expected_revision=expected_revision)
                # Proposal insertion is guarded by this immutable audit witness.
                self._insert_change_audit(connection, audit)
                self._insert_proposal(connection, proposal)
                self._insert_change_operation(
                    connection,
                    draft=draft,
                    proposal=proposal,
                    operation=operation,
                    actor_id=actor_id,
                    idempotency_digest=idempotency_digest,
                    request_fingerprint=request_fingerprint,
                    created_at=audit.occurred_at,
                )
            return RegistryModelChangeMutation(draft=draft, proposal=proposal)
        except UniqueViolation as error:
            replay = self._exact_replay(
                draft.workspace_id,
                idempotency_digest,
                operation=operation,
                actor_id=actor_id,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None and replay.draft == draft and replay.proposal == proposal:
                return RegistryModelChangeMutation(
                    draft=replay.draft,
                    proposal=replay.proposal,
                    replayed=True,
                )
            raise _conflict() from error
        except RegistryModelChangePortError as error:
            replay = self._exact_replay(
                draft.workspace_id,
                idempotency_digest,
                operation=operation,
                actor_id=actor_id,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None and replay.draft == draft and replay.proposal == proposal:
                return RegistryModelChangeMutation(
                    draft=replay.draft,
                    proposal=replay.proposal,
                    replayed=True,
                )
            raise error
        except (ObjectNotInPrerequisiteState, CheckViolation, ForeignKeyViolation) as error:
            raise _conflict() from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except (UndefinedColumn, UndefinedFunction, UndefinedTable) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def list_audit(
        self,
        workspace_id: str,
        change_id: str,
        *,
        limit: int = MAX_REGISTRY_MODEL_CHANGE_HISTORY + 1,
    ) -> tuple[RegistryModelChangeAuditRecord, ...]:
        if not 1 <= limit <= MAX_REGISTRY_MODEL_CHANGE_HISTORY + 1:
            raise _invalid_response()
        table = self._database.table("semantic_registry_model_change_audit")
        try:
            with self._database.connect() as connection:
                rows = connection.execute(
                    sql.SQL(
                        """
                        SELECT payload FROM {table}
                        WHERE workspace_id = %s AND draft_id = %s
                        ORDER BY resulting_revision DESC, occurred_at DESC, audit_id DESC
                        LIMIT %s
                        """
                    ).format(table=table),
                    (workspace_id, change_id, limit),
                ).fetchall()
            return tuple(
                reversed(
                    tuple(RegistryModelChangeAuditRecord.model_validate(row[0]) for row in rows)
                )
            )
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except (UndefinedColumn, UndefinedTable) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def _exact_replay(
        self,
        workspace_id: str,
        idempotency_digest: str,
        *,
        operation: str,
        actor_id: str,
        request_fingerprint: str,
    ) -> RegistryModelChangeOperationReplay | None:
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

    def _insert_draft(
        self,
        connection: psycopg.Connection[Any],
        draft: RegistryModelChangeDraft,
    ) -> None:
        table = self._database.table("semantic_registry_model_change_drafts")
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {table} (
                    workspace_id, draft_id, source_proposal_id,
                    source_proposal_fingerprint, catalog_scope, registry_id,
                    owner_actor_id, status, revision, fingerprint,
                    prepared_proposal_id, prepared_proposal_fingerprint,
                    payload, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )
                """
            ).format(table=table),
            (
                draft.workspace_id,
                draft.id,
                draft.source.proposal.id,
                draft.source.proposal.fingerprint,
                draft.base.scope.catalog_scope,
                draft.base.scope.registry_id,
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
        draft: RegistryModelChangeDraft,
        *,
        expected_revision: int,
    ) -> None:
        if draft.revision != expected_revision + 1:
            raise _conflict()
        table = self._database.table("semantic_registry_model_change_drafts")
        updated = connection.execute(
            sql.SQL(
                """
                UPDATE {table}
                SET status = %s, revision = %s, fingerprint = %s,
                    prepared_proposal_id = %s,
                    prepared_proposal_fingerprint = %s,
                    payload = %s, updated_at = %s
                WHERE workspace_id = %s AND draft_id = %s
                  AND revision = %s AND fingerprint <> %s
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
                draft.fingerprint,
            ),
        )
        if updated.rowcount != 1:
            raise _conflict()

    def _insert_outer_decision(
        self,
        connection: psycopg.Connection[Any],
        draft: RegistryModelChangeDraft,
    ) -> None:
        decision = draft.outer_decision
        if decision is None:
            raise _conflict()
        table = self._database.table("semantic_registry_model_change_decisions")
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {table} (
                    workspace_id, decision_id, draft_id, resulting_revision,
                    actor_id, action, status, payload, decided_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
            ).format(table=table),
            (
                draft.workspace_id,
                decision.id,
                draft.id,
                draft.revision,
                decision.actor,
                decision.action.value,
                decision.status.value,
                Jsonb(decision.model_dump(mode="json")),
                decision.decided_at,
            ),
        )

    def _insert_proposal(
        self,
        connection: psycopg.Connection[Any],
        proposal: PreparedRegistryModelReplacementProposal,
    ) -> None:
        table = self._database.table("semantic_registry_model_change_proposals")
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {table} (
                    workspace_id, proposal_id, proposal_kind, draft_id,
                    draft_revision, draft_fingerprint, source_proposal_id,
                    source_proposal_fingerprint, target_registry_version,
                    fingerprint, prepared_by, payload, prepared_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s
                )
                """
            ).format(table=table),
            (
                proposal.workspace_id,
                proposal.id,
                proposal.proposal_kind,
                proposal.draft_id,
                proposal.draft_revision,
                proposal.draft_fingerprint,
                proposal.source_replacement_proposal_id,
                proposal.source_replacement_proposal_fingerprint,
                proposal.target_registry_version,
                proposal.fingerprint,
                proposal.prepared_by,
                Jsonb(proposal.model_dump(mode="json")),
                proposal.prepared_at,
            ),
        )

    def _insert_change_audit(
        self,
        connection: psycopg.Connection[Any],
        audit: RegistryModelChangeAuditRecord,
    ) -> None:
        table = self._database.table("semantic_registry_model_change_audit")
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

    def _insert_change_operation(
        self,
        connection: psycopg.Connection[Any],
        *,
        draft: RegistryModelChangeDraft,
        proposal: PreparedRegistryModelReplacementProposal | None,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
        created_at: datetime,
    ) -> None:
        if operation not in _MODEL_OPERATIONS:
            raise ValueError("registry model operation is unsupported")
        table = self._database.table("semantic_registry_model_change_operations")
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {table} (
                    workspace_id, actor_id, idempotency_digest, operation,
                    request_fingerprint, draft_id, response_revision,
                    response_fingerprint, response_draft, response_proposal,
                    proposal_id, proposal_fingerprint, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s
                )
                """
            ).format(table=table),
            (
                draft.workspace_id,
                actor_id,
                idempotency_digest,
                operation,
                request_fingerprint,
                draft.id,
                draft.revision,
                draft.fingerprint,
                Jsonb(draft.model_dump(mode="json")),
                None if proposal is None else Jsonb(proposal.model_dump(mode="json")),
                None if proposal is None else proposal.id,
                None if proposal is None else proposal.fingerprint,
                created_at,
            ),
        )


@dataclass(frozen=True, slots=True)
class PostgresRegistryModelReplacementSourceReader:
    """Read one exact immutable M33 proposal and its complete decision closure."""

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

    def load(
        self,
        workspace_id: str,
        proposal_id: str,
    ) -> RegistryModelReplacementSourceEvidence | None:
        function = self._database.table("load_registry_model_replacement_source")
        try:
            with self._database.connect() as connection:
                rows = connection.execute(
                    sql.SQL(
                        "SELECT proposal, owner_actor_id, decisions FROM {function}(%s, %s)"
                    ).format(function=function),
                    (workspace_id, proposal_id),
                ).fetchall()
            if not rows:
                return None
            if len(rows) != 1 or not isinstance(rows[0][2], list):
                raise ValueError("registry model source is ambiguous")
            proposal = PreparedSemanticOnboardingProposal.model_validate(rows[0][0])
            decisions = tuple(
                SemanticOnboardingDecision.model_validate(value) for value in rows[0][2]
            )
            evidence = RegistryModelReplacementSourceEvidence.create(
                proposal=proposal,
                owner_actor_id=_text(rows[0][1]),
                decisions=decisions,
            )
            if proposal.workspace_id != workspace_id or proposal.id != proposal_id:
                raise ValueError("registry model source crossed its tenant")
            return evidence
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except (UndefinedColumn, UndefinedFunction, UndefinedTable) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error


@dataclass(frozen=True, slots=True)
class PostgresRegistryModelRemediationEvidenceReader:
    """Current-head-only M26 report and complete exact impact set reader."""

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

    def load_current(
        self,
        scope: SemanticRegistryScope,
        report_id: str,
        expected_context: SemanticChangeInspectionContext,
    ) -> tuple[SemanticChangeReport, SemanticImpactSet] | None:
        function = self._database.table("load_current_registry_model_remediation")
        try:
            with self._database.connect() as connection:
                rows = connection.execute(
                    sql.SQL("SELECT report, impacts FROM {function}(%s, %s, %s, %s, %s)").format(
                        function=function
                    ),
                    (
                        scope.workspace_id,
                        scope.catalog_scope,
                        scope.registry_id,
                        report_id,
                        expected_context.fingerprint,
                    ),
                ).fetchall()
            if not rows:
                return None
            if len(rows) != 1 or not isinstance(rows[0][1], list):
                raise ValueError("registry model remediation head is ambiguous")
            report = SemanticChangeReport.model_validate(rows[0][0])
            impacts = tuple(SemanticChangeImpact.model_validate(value) for value in rows[0][1])
            impact_set = SemanticImpactSet.create(
                impacts=impacts,
                complete=report.context.dependency_index.complete,
                watermark=report.context.dependency_index.watermark,
                dependency_index_fingerprint=report.context.dependency_index.fingerprint,
            )
            if (
                report.id != report_id
                or report.context != expected_context
                or report.impacts.impact_set_fingerprint != impact_set.fingerprint
            ):
                raise ValueError("registry model remediation head differs from its request")
            return report, impact_set
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except (UndefinedColumn, UndefinedFunction, UndefinedTable) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error


@dataclass(frozen=True, slots=True)
class PostgresRegistryModelJoinProfileWitnessReader:
    """Read only the newest request-ordered witness for one outer change endpoint."""

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

    def load(
        self,
        workspace_id: str,
        change_id: str,
        replacement_proposal_id: str,
        join_id: str,
    ) -> RegistryModelJoinProfileWitness | None:
        function = self._database.table("load_current_registry_model_join_witness")
        try:
            with self._database.connect() as connection:
                rows = connection.execute(
                    sql.SQL("SELECT witness FROM {function}(%s, %s, %s, %s)").format(
                        function=function
                    ),
                    (workspace_id, change_id, replacement_proposal_id, join_id),
                ).fetchall()
            if not rows:
                return None
            if len(rows) != 1:
                raise ValueError("registry model witness head is ambiguous")
            witness = RegistryModelJoinProfileWitness.model_validate(rows[0][0])
            if (
                witness.change_id != change_id
                or witness.source_replacement_proposal_id != replacement_proposal_id
                or witness.proposal.id != join_id
            ):
                raise ValueError("registry model witness crossed its head")
            return witness
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except (UndefinedColumn, UndefinedFunction, UndefinedTable) as error:
            raise _unavailable() from error
        except psycopg.Error as error:
            raise _unavailable() from error


def _require_job_matches(
    authoring: RegistryModelJoinProfileAuthoringRequest,
    job: SemanticJoinProfileJob,
) -> None:
    request = authoring.request
    if (
        job.workspace_id != authoring.workspace_id
        or job.scan_id != request.scan_id
        or job.bound_proposal != request.bound_proposal
        or job.proposal_fingerprint
        != semantic_join_profile_proposal_fingerprint(request.bound_proposal)
        or job.execution_target != request.execution_target
        or job.connector_contract_version is None
        or job.requested_at != request.requested_at
    ):
        raise ValueError("registry model profile job crossed its durable request")


def _require_profile_audit(
    authoring: RegistryModelJoinProfileAuthoringRequest,
    audit: RegistryModelJoinProfileAuditRecord,
    *,
    actor_id: str,
) -> None:
    if (
        audit.workspace_id != authoring.workspace_id
        or audit.request_id != authoring.id
        or audit.actor_id != actor_id
        or audit.request_fingerprint != authoring.fingerprint
    ):
        raise _conflict()


def _require_change_audit(
    draft: RegistryModelChangeDraft,
    audit: RegistryModelChangeAuditRecord,
    *,
    actor_id: str,
) -> None:
    if (
        audit.workspace_id != draft.workspace_id
        or audit.change_id != draft.id
        or audit.actor_id != actor_id
        or audit.resulting_revision != draft.revision
        or audit.resulting_fingerprint != draft.fingerprint
    ):
        raise _conflict()


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("registry model persistence text is invalid")
    return value


def _conflict() -> RegistryModelChangePortError:
    return RegistryModelChangePortError(
        RegistryModelChangePortErrorCode.CONFLICT,
        "registry model persistence conflict",
    )


def _invalid_response() -> RegistryModelChangePortError:
    return RegistryModelChangePortError(
        RegistryModelChangePortErrorCode.INVALID_RESPONSE,
        "registry model persistence response was invalid",
    )


def _unavailable() -> RegistryModelChangePortError:
    return RegistryModelChangePortError(
        RegistryModelChangePortErrorCode.UNAVAILABLE,
        "registry model persistence is unavailable",
    )


def _queue_invalid() -> SemanticJoinProfileQueueError:
    return SemanticJoinProfileQueueError(
        SemanticJoinProfileQueueErrorCode.INVALID_RESPONSE,
        "registry model profile queue response was invalid",
    )


def _queue_conflict() -> SemanticJoinProfileQueueError:
    return SemanticJoinProfileQueueError(
        SemanticJoinProfileQueueErrorCode.STATE_CONFLICT,
        "registry model profile queue state changed",
    )


def _queue_unavailable() -> SemanticJoinProfileQueueError:
    return SemanticJoinProfileQueueError(
        SemanticJoinProfileQueueErrorCode.STORE_UNAVAILABLE,
        "registry model profile queue is unavailable",
    )


__all__ = [
    "PostgresRegistryModelChangeStore",
    "PostgresRegistryModelJoinProfileQueue",
    "PostgresRegistryModelJoinProfileWitnessReader",
    "PostgresRegistryModelProfileStore",
    "PostgresRegistryModelRemediationEvidenceReader",
    "PostgresRegistryModelReplacementSourceReader",
]
