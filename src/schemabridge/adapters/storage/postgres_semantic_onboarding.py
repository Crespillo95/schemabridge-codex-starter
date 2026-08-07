"""Durable PostgreSQL store for authenticated M33 semantic onboarding."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg
from psycopg import sql
from psycopg.errors import CheckViolation, ForeignKeyViolation, UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from schemabridge.adapters.control_plane.workspace_lock import workspace_control_lock_id
from schemabridge.adapters.storage.postgres import ControlConnectionProvider, _ControlDatabase
from schemabridge.application.ports.semantic_onboarding import (
    SemanticOnboardingOperationReplay,
    SemanticOnboardingPortError,
    SemanticOnboardingPortErrorCode,
)
from schemabridge.domain.registry_control import (
    ActiveRegistryPointer,
    registry_projection_fingerprint,
)
from schemabridge.domain.semantic_onboarding import (
    MAX_ONBOARDING_DECISIONS,
    OnboardingRegistryBase,
    PreparedSemanticOnboardingProposal,
    SemanticOnboardingAuditRecord,
    SemanticOnboardingDecision,
    SemanticOnboardingDraft,
    SemanticOnboardingDraftMutation,
    SemanticOnboardingPreparation,
    apply_semantic_onboarding_decision,
)


@dataclass(frozen=True, slots=True)
class PostgresSemanticOnboardingStore:
    """Atomic workspace-scoped store over migrator-owned M33 tables."""

    dsn: str = field(repr=False)
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-api"
    stale_after_seconds: int = 900
    connection_provider: ControlConnectionProvider | None = field(default=None, repr=False)
    _database: _ControlDatabase = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not 60 <= self.stale_after_seconds <= 2_592_000:
            raise ValueError("semantic onboarding catalog stale threshold is invalid")
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
    ) -> SemanticOnboardingOperationReplay | None:
        operations = self._database.table("semantic_onboarding_operations")
        decisions = self._database.table("semantic_onboarding_decisions")
        try:
            with self._database.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT operation, request_fingerprint, actor_id, draft_id,
                               response_revision, response_fingerprint,
                               response_draft, response_proposal
                        FROM {operations}
                        WHERE workspace_id = %s AND idempotency_digest = %s
                        """
                    ).format(operations=operations),
                    (workspace_id, idempotency_digest),
                ).fetchone()
                return (
                    None
                    if row is None
                    else _operation_from_row(
                        connection,
                        operations,
                        decisions,
                        workspace_id,
                        row,
                    )
                )
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def create(
        self,
        draft: SemanticOnboardingDraft,
        audit: SemanticOnboardingAuditRecord,
        *,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> SemanticOnboardingDraftMutation:
        drafts = self._database.table("semantic_onboarding_drafts")
        replay = self._exact_replay(
            draft.workspace_id,
            idempotency_digest,
            operation=operation,
            actor_id=actor_id,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            return SemanticOnboardingDraftMutation(draft=replay.draft, replayed=True)
        try:
            with self._database.connect() as connection, connection.transaction():
                self._lock_and_validate_authority(connection, draft)
                _insert_draft(connection, drafts, draft)
                self._insert_audit(connection, audit)
                self._insert_operation(
                    connection,
                    draft=draft,
                    proposal=None,
                    operation=operation,
                    actor_id=actor_id,
                    idempotency_digest=idempotency_digest,
                    request_fingerprint=request_fingerprint,
                    created_at=audit.occurred_at,
                )
            return SemanticOnboardingDraftMutation(draft=draft)
        except UniqueViolation as error:
            replay = self._exact_replay(
                draft.workspace_id,
                idempotency_digest,
                operation=operation,
                actor_id=actor_id,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None:
                return SemanticOnboardingDraftMutation(draft=replay.draft, replayed=True)
            raise _conflict() from error
        except SemanticOnboardingPortError:
            raise
        except (
            CheckViolation,
            ForeignKeyViolation,
            ValidationError,
            TypeError,
            ValueError,
        ) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def load(self, workspace_id: str, draft_id: str) -> SemanticOnboardingDraft | None:
        drafts = self._database.table("semantic_onboarding_drafts")
        try:
            with self._database.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT workspace_id, draft_id, owner_actor_id, revision, status,
                               fingerprint, payload, prepared_proposal_id,
                               prepared_proposal_fingerprint, created_at, updated_at
                        FROM {drafts}
                        WHERE workspace_id = %s AND draft_id = %s
                        """
                    ).format(drafts=drafts),
                    (workspace_id, draft_id),
                ).fetchone()
            return None if row is None else _draft_from_row(row)
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def list_for_workspace(
        self,
        workspace_id: str,
        *,
        owner_actor_id: str | None,
        limit: int,
    ) -> tuple[SemanticOnboardingDraft, ...]:
        if not 1 <= limit <= 50:
            raise _invalid_response()
        drafts = self._database.table("semantic_onboarding_drafts")
        owner_clause = (
            sql.SQL("") if owner_actor_id is None else sql.SQL(" AND owner_actor_id = %s")
        )
        params: list[object] = [workspace_id]
        if owner_actor_id is not None:
            params.append(owner_actor_id)
        params.append(limit)
        try:
            with self._database.connect() as connection:
                rows = connection.execute(
                    sql.SQL(
                        """
                        SELECT workspace_id, draft_id, owner_actor_id, revision, status,
                               fingerprint, payload, prepared_proposal_id,
                               prepared_proposal_fingerprint, created_at, updated_at
                        FROM {drafts}
                        WHERE workspace_id = %s{owner_clause}
                        ORDER BY updated_at DESC, draft_id DESC
                        LIMIT %s
                        """
                    ).format(drafts=drafts, owner_clause=owner_clause),
                    tuple(params),
                ).fetchall()
            return tuple(_draft_from_row(row) for row in rows)
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def commit_decision(
        self,
        draft: SemanticOnboardingDraft,
        decision: SemanticOnboardingDecision,
        audit: SemanticOnboardingAuditRecord,
        *,
        expected_revision: int,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> SemanticOnboardingDraftMutation:
        replay = self._exact_replay(
            draft.workspace_id,
            idempotency_digest,
            operation=operation,
            actor_id=actor_id,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            return SemanticOnboardingDraftMutation(draft=replay.draft, replayed=True)
        try:
            with self._database.connect() as connection, connection.transaction():
                self._lock_and_validate_authority(connection, draft)
                self._cas_draft(connection, draft, expected_revision=expected_revision)
                self._insert_decision(connection, decision)
                self._insert_audit(connection, audit)
                self._insert_operation(
                    connection,
                    draft=draft,
                    proposal=None,
                    operation=operation,
                    actor_id=actor_id,
                    idempotency_digest=idempotency_digest,
                    request_fingerprint=request_fingerprint,
                    created_at=audit.occurred_at,
                )
            return SemanticOnboardingDraftMutation(draft=draft)
        except UniqueViolation as error:
            replay = self._exact_replay(
                draft.workspace_id,
                idempotency_digest,
                operation=operation,
                actor_id=actor_id,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None:
                return SemanticOnboardingDraftMutation(draft=replay.draft, replayed=True)
            raise _conflict() from error
        except SemanticOnboardingPortError as error:
            replay = self._exact_replay(
                draft.workspace_id,
                idempotency_digest,
                operation=operation,
                actor_id=actor_id,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None:
                return SemanticOnboardingDraftMutation(draft=replay.draft, replayed=True)
            raise error
        except (
            CheckViolation,
            ForeignKeyViolation,
            ValidationError,
            TypeError,
            ValueError,
        ) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def commit_preparation(
        self,
        draft: SemanticOnboardingDraft,
        proposal: PreparedSemanticOnboardingProposal,
        audit: SemanticOnboardingAuditRecord,
        *,
        expected_revision: int,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> SemanticOnboardingPreparation:
        replay = self._exact_replay(
            draft.workspace_id,
            idempotency_digest,
            operation=operation,
            actor_id=actor_id,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            if replay.proposal is None:
                raise _conflict()
            return SemanticOnboardingPreparation(
                draft=replay.draft,
                proposal=replay.proposal,
                replayed=True,
            )
        try:
            with self._database.connect() as connection, connection.transaction():
                self._lock_and_validate_authority(connection, draft)
                self._cas_draft(connection, draft, expected_revision=expected_revision)
                self._insert_proposal(connection, proposal)
                self._insert_audit(connection, audit)
                self._insert_operation(
                    connection,
                    draft=draft,
                    proposal=proposal,
                    operation=operation,
                    actor_id=actor_id,
                    idempotency_digest=idempotency_digest,
                    request_fingerprint=request_fingerprint,
                    created_at=audit.occurred_at,
                )
            return SemanticOnboardingPreparation(draft=draft, proposal=proposal)
        except UniqueViolation as error:
            replay = self._exact_replay(
                draft.workspace_id,
                idempotency_digest,
                operation=operation,
                actor_id=actor_id,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None and replay.proposal is not None:
                return SemanticOnboardingPreparation(
                    draft=replay.draft,
                    proposal=replay.proposal,
                    replayed=True,
                )
            raise _conflict() from error
        except SemanticOnboardingPortError as error:
            replay = self._exact_replay(
                draft.workspace_id,
                idempotency_digest,
                operation=operation,
                actor_id=actor_id,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None and replay.proposal is not None:
                return SemanticOnboardingPreparation(
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
        except psycopg.Error as error:
            raise _unavailable() from error

    def list_decisions(
        self,
        workspace_id: str,
        draft_id: str,
        *,
        limit: int = MAX_ONBOARDING_DECISIONS + 2,
    ) -> tuple[SemanticOnboardingDecision, ...]:
        return self._list_models(
            table_name="semantic_onboarding_decisions",
            model=SemanticOnboardingDecision,
            workspace_id=workspace_id,
            draft_id=draft_id,
            order_by=("resulting_revision", "decision_id"),
            limit=limit,
        )

    def list_proposals(
        self,
        workspace_id: str,
        draft_id: str,
        *,
        limit: int = MAX_ONBOARDING_DECISIONS + 2,
    ) -> tuple[PreparedSemanticOnboardingProposal, ...]:
        return self._list_models(
            table_name="semantic_onboarding_proposals",
            model=PreparedSemanticOnboardingProposal,
            workspace_id=workspace_id,
            draft_id=draft_id,
            order_by=("target_registry_version", "proposal_id"),
            limit=limit,
        )

    def list_audit(
        self,
        workspace_id: str,
        draft_id: str,
        *,
        limit: int = MAX_ONBOARDING_DECISIONS + 2,
    ) -> tuple[SemanticOnboardingAuditRecord, ...]:
        return self._list_models(
            table_name="semantic_onboarding_audit",
            model=SemanticOnboardingAuditRecord,
            workspace_id=workspace_id,
            draft_id=draft_id,
            order_by=("resulting_revision", "audit_id"),
            limit=limit,
        )

    def _list_models(
        self,
        *,
        table_name: str,
        model: type[SemanticOnboardingDecision]
        | type[PreparedSemanticOnboardingProposal]
        | type[SemanticOnboardingAuditRecord],
        workspace_id: str,
        draft_id: str,
        order_by: tuple[str, ...],
        limit: int,
    ) -> tuple[Any, ...]:
        if not 1 <= limit <= MAX_ONBOARDING_DECISIONS + 2:
            raise _unavailable()
        table = self._database.table(table_name)
        descending_order = sql.SQL(", ").join(
            sql.SQL("{} DESC").format(sql.Identifier(column)) for column in order_by
        )
        try:
            with self._database.connect() as connection:
                rows = connection.execute(
                    sql.SQL(
                        "SELECT payload FROM {table} "
                        "WHERE workspace_id = %s AND draft_id = %s "
                        "ORDER BY {order_by} LIMIT %s"
                    ).format(
                        table=table,
                        order_by=descending_order,
                    ),
                    (workspace_id, draft_id, limit),
                ).fetchall()
            return tuple(model.model_validate(row[0]) for row in reversed(rows))
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def _cas_draft(
        self,
        connection: psycopg.Connection[Any],
        draft: SemanticOnboardingDraft,
        *,
        expected_revision: int,
    ) -> None:
        drafts = self._database.table("semantic_onboarding_drafts")
        updated = connection.execute(
            sql.SQL(
                """
                UPDATE {drafts}
                SET revision = %s,
                    status = %s,
                    fingerprint = %s,
                    payload = %s,
                    prepared_proposal_id = %s,
                    prepared_proposal_fingerprint = %s,
                    updated_at = %s
                WHERE workspace_id = %s
                  AND draft_id = %s
                  AND revision = %s
                  AND status = 'needs_review'
                """
            ).format(drafts=drafts),
            (
                draft.revision,
                draft.status.value,
                draft.fingerprint,
                Jsonb(draft.model_dump(mode="json")),
                draft.prepared_proposal_id,
                draft.prepared_proposal_fingerprint,
                draft.updated_at,
                draft.workspace_id,
                draft.id,
                expected_revision,
            ),
        )
        if updated.rowcount != 1 or draft.revision != expected_revision + 1:
            raise _conflict()

    def _lock_and_validate_authority(
        self,
        connection: psycopg.Connection[Any],
        draft: SemanticOnboardingDraft,
    ) -> None:
        """Freeze and compare every mutable authority behind a reviewed draft."""

        connection.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (workspace_control_lock_id(draft.workspace_id),),
        )
        connections = self._database.table("catalog_connections")
        catalog_row = connection.execute(
            sql.SQL(
                """
                SELECT catalog_scope, status, active_generation,
                       active_generation_fingerprint,
                       active_generation_completed_at,
                       active_generation_completed_at
                           < clock_timestamp() - make_interval(secs => %s) AS stale
                FROM {connections}
                WHERE workspace_id = %s
                  AND connection_id = %s
                FOR SHARE
                """
            ).format(connections=connections),
            (
                self.stale_after_seconds,
                draft.workspace_id,
                draft.connection_id.root,
            ),
        ).fetchone()
        if (
            catalog_row is None
            or str(catalog_row[0]) != draft.scope.catalog_scope
            or str(catalog_row[1]) != "enabled"
            or catalog_row[2] != draft.catalog_generation
            or str(catalog_row[3]) != draft.catalog_generation_fingerprint
            or catalog_row[4] is None
            or bool(catalog_row[5])
        ):
            raise _conflict()

        # Registry activation takes this same workspace lock. A plain SELECT is
        # therefore a locking read with respect to the only production writer,
        # without granting the API role UPDATE on the authoritative pointer.
        observed_base = self._load_registry_base_under_workspace_lock(connection, draft)
        if observed_base != draft.base_registry:
            raise _conflict()

    def _load_registry_base_under_workspace_lock(
        self,
        connection: psycopg.Connection[Any],
        draft: SemanticOnboardingDraft,
    ) -> OnboardingRegistryBase:
        pointers = self._database.table("registry_active_pointers")
        row = connection.execute(
            sql.SQL(
                """
                SELECT generation, registry_version, registry_fingerprint,
                       registry_target, transition_id, activated_by,
                       activated_at, decision_ids_json
                FROM {pointers}
                WHERE workspace_id = %s
                  AND catalog_scope = %s
                  AND registry_id = %s
                """
            ).format(pointers=pointers),
            (
                draft.workspace_id,
                draft.scope.catalog_scope,
                draft.scope.registry_id,
            ),
        ).fetchone()
        if row is None:
            return OnboardingRegistryBase()
        pointer = ActiveRegistryPointer(
            scope=draft.scope,
            generation=row[0],
            registry_version=row[1],
            registry_fingerprint=str(row[2]),
            registry_target=str(row[3]),
            transition_id=str(row[4]),
            activated_by=str(row[5]),
            activated_at=row[6],
            decision_ids=tuple(str(item) for item in row[7]),
        )
        return OnboardingRegistryBase(
            registry_version=pointer.registry_version,
            registry_fingerprint=pointer.registry_fingerprint,
            activation_generation=pointer.generation,
            active_pointer_fingerprint=registry_projection_fingerprint(pointer),
        )

    def _insert_decision(
        self,
        connection: psycopg.Connection[Any],
        decision: SemanticOnboardingDecision,
    ) -> None:
        decisions = self._database.table("semantic_onboarding_decisions")
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {decisions} (
                    workspace_id, decision_id, draft_id, resulting_revision,
                    actor_id, target_kind, target_id, status, payload, decided_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
            ).format(decisions=decisions),
            (
                decision.workspace_id,
                decision.id,
                decision.draft_id,
                decision.resulting_revision,
                decision.actor_id,
                decision.target_kind.value,
                decision.target_id,
                decision.status.value,
                Jsonb(decision.model_dump(mode="json")),
                decision.decided_at,
            ),
        )

    def _insert_proposal(
        self,
        connection: psycopg.Connection[Any],
        proposal: PreparedSemanticOnboardingProposal,
    ) -> None:
        proposals = self._database.table("semantic_onboarding_proposals")
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {proposals} (
                    workspace_id, proposal_id, draft_id, draft_revision,
                    target_registry_version, fingerprint, prepared_by, payload, prepared_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
            ).format(proposals=proposals),
            (
                proposal.workspace_id,
                proposal.id,
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
        audit: SemanticOnboardingAuditRecord,
    ) -> None:
        audits = self._database.table("semantic_onboarding_audit")
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {audits} (
                    workspace_id, audit_id, draft_id, resulting_revision,
                    event, actor_id, payload, occurred_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """
            ).format(audits=audits),
            (
                audit.workspace_id,
                audit.id,
                audit.draft_id,
                audit.resulting_revision,
                audit.event.value,
                audit.actor_id,
                Jsonb(audit.model_dump(mode="json")),
                audit.occurred_at,
            ),
        )

    def _insert_operation(
        self,
        connection: psycopg.Connection[Any],
        *,
        draft: SemanticOnboardingDraft,
        proposal: PreparedSemanticOnboardingProposal | None,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
        created_at: datetime,
    ) -> None:
        operations = self._database.table("semantic_onboarding_operations")
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {operations} (
                    workspace_id, idempotency_digest, operation, request_fingerprint,
                    actor_id, draft_id, response_revision, response_fingerprint,
                    response_draft, response_proposal, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
            ).format(operations=operations),
            (
                draft.workspace_id,
                idempotency_digest,
                operation,
                request_fingerprint,
                actor_id,
                draft.id,
                draft.revision,
                draft.fingerprint,
                (None if operation == "record_decision" else Jsonb(draft.model_dump(mode="json"))),
                None if proposal is None else Jsonb(proposal.model_dump(mode="json")),
                created_at,
            ),
        )

    def _exact_replay(
        self,
        workspace_id: str,
        idempotency_digest: str,
        *,
        operation: str,
        actor_id: str,
        request_fingerprint: str,
    ) -> SemanticOnboardingOperationReplay | None:
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
    connection: psycopg.Connection[Any],
    drafts: sql.Composed,
    draft: SemanticOnboardingDraft,
) -> None:
    connection.execute(
        sql.SQL(
            """
            INSERT INTO {drafts} (
                workspace_id, draft_id, owner_actor_id, revision, status,
                fingerprint, payload, prepared_proposal_id,
                prepared_proposal_fingerprint, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
        ).format(drafts=drafts),
        (
            draft.workspace_id,
            draft.id,
            draft.owner_actor_id,
            draft.revision,
            draft.status.value,
            draft.fingerprint,
            Jsonb(draft.model_dump(mode="json")),
            draft.prepared_proposal_id,
            draft.prepared_proposal_fingerprint,
            draft.created_at,
            draft.updated_at,
        ),
    )


def _draft_from_row(row: tuple[Any, ...]) -> SemanticOnboardingDraft:
    draft = SemanticOnboardingDraft.model_validate(row[6])
    if (
        row[0] != draft.workspace_id
        or row[1] != draft.id
        or row[2] != draft.owner_actor_id
        or int(row[3]) != draft.revision
        or row[4] != draft.status.value
        or row[5] != draft.fingerprint
        or row[7] != draft.prepared_proposal_id
        or row[8] != draft.prepared_proposal_fingerprint
        or row[9] != draft.created_at
        or row[10] != draft.updated_at
    ):
        raise ValueError("semantic onboarding draft row is inconsistent")
    return draft


def _operation_from_row(
    connection: psycopg.Connection[Any],
    operations: sql.Composed,
    decisions: sql.Composed,
    workspace_id: str,
    row: tuple[Any, ...],
) -> SemanticOnboardingOperationReplay:
    operation = str(row[0])
    draft_id = str(row[3])
    response_revision = int(row[4])
    response_fingerprint = str(row[5])
    response_draft = row[6]
    if operation not in {"create_draft", "record_decision", "prepare_publication"}:
        raise ValueError("semantic onboarding operation is invalid")
    if not 1 <= response_revision <= MAX_ONBOARDING_DECISIONS + 2:
        raise ValueError("semantic onboarding operation revision is invalid")
    if response_draft is None:
        if operation != "record_decision" or response_revision > MAX_ONBOARDING_DECISIONS + 1:
            raise ValueError("semantic onboarding compact replay is invalid")
        root_rows = connection.execute(
            sql.SQL(
                """
                SELECT response_draft, response_revision, response_fingerprint
                FROM {operations}
                WHERE workspace_id = %s
                  AND draft_id = %s
                  AND operation = 'create_draft'
                """
            ).format(operations=operations),
            (workspace_id, draft_id),
        ).fetchall()
        if len(root_rows) != 1 or root_rows[0][0] is None or int(root_rows[0][1]) != 1:
            raise ValueError("semantic onboarding replay root is invalid")
        draft = SemanticOnboardingDraft.model_validate(root_rows[0][0])
        if draft.fingerprint != str(root_rows[0][2]):
            raise ValueError("semantic onboarding replay root fingerprint is invalid")
        decision_rows = connection.execute(
            sql.SQL(
                """
                SELECT payload
                FROM {decisions}
                WHERE workspace_id = %s
                  AND draft_id = %s
                  AND resulting_revision <= %s
                ORDER BY resulting_revision, decision_id
                LIMIT %s
                """
            ).format(decisions=decisions),
            (
                workspace_id,
                draft_id,
                response_revision,
                MAX_ONBOARDING_DECISIONS + 1,
            ),
        ).fetchall()
        if len(decision_rows) != response_revision - 1:
            raise ValueError("semantic onboarding compact replay history is incomplete")
        for decision_row in decision_rows:
            draft = apply_semantic_onboarding_decision(
                draft,
                SemanticOnboardingDecision.model_validate(decision_row[0]),
            )
    else:
        if operation == "record_decision":
            raise ValueError("semantic onboarding decision replay is not compact")
        draft = SemanticOnboardingDraft.model_validate(response_draft)
    proposal = None if row[7] is None else PreparedSemanticOnboardingProposal.model_validate(row[7])
    if (
        draft.workspace_id != workspace_id
        or draft.id != draft_id
        or draft.revision != response_revision
        or draft.fingerprint != response_fingerprint
        or (operation == "prepare_publication") != (proposal is not None)
    ):
        raise ValueError("semantic onboarding operation response is inconsistent")
    if proposal is not None and (
        proposal.workspace_id != draft.workspace_id
        or proposal.draft_id != draft.id
        or draft.prepared_proposal_id != proposal.id
        or draft.prepared_proposal_fingerprint != proposal.fingerprint
    ):
        raise ValueError("semantic onboarding operation proposal is inconsistent")
    return SemanticOnboardingOperationReplay(
        operation=operation,
        request_fingerprint=str(row[1]),
        actor_id=str(row[2]),
        draft=draft,
        proposal=proposal,
    )


def _conflict() -> SemanticOnboardingPortError:
    return SemanticOnboardingPortError(
        SemanticOnboardingPortErrorCode.CONFLICT,
        "semantic onboarding store conflict",
    )


def _invalid_response() -> SemanticOnboardingPortError:
    return SemanticOnboardingPortError(
        SemanticOnboardingPortErrorCode.INVALID_RESPONSE,
        "semantic onboarding store returned invalid state",
    )


def _unavailable() -> SemanticOnboardingPortError:
    return SemanticOnboardingPortError(
        SemanticOnboardingPortErrorCode.UNAVAILABLE,
        "semantic onboarding store is unavailable",
    )


__all__ = ["PostgresSemanticOnboardingStore"]
