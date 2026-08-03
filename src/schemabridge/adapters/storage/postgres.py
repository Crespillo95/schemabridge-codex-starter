"""PostgreSQL stores for managed control-plane state.

These adapters never create tables and never connect to a source database. The explicit
control-plane migrator owns DDL; runtime composition supplies a separate, least-privilege DSN.
"""

from __future__ import annotations

import re
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any, Protocol, TypeVar, cast

import psycopg
from psycopg import sql
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ValidationError

from schemabridge.application.ports.publication_audit import PublicationAuditStoreError
from schemabridge.application.ports.relationships import (
    RelationshipErrorCode,
    RelationshipWorkflowError,
)
from schemabridge.application.ports.requests import (
    RequestWorkflowError,
    RequestWorkflowErrorCode,
)
from schemabridge.application.ports.reviews import ReviewErrorCode, ReviewWorkflowError
from schemabridge.application.ports.workflow_access import (
    WorkflowAccessError,
    WorkflowAccessErrorCode,
)
from schemabridge.application.ports.workflows import WorkflowError, WorkflowErrorCode
from schemabridge.domain.decisions import DecisionRecord
from schemabridge.domain.identity import WorkflowAccessGrant
from schemabridge.domain.join_reviews import JoinPublicationResult, JoinReviewDraft
from schemabridge.domain.publication_audit import PublicationTargetAuditRecord
from schemabridge.domain.requests import AnalyticalRequestDraft
from schemabridge.domain.reviews import CanonicalReviewDraft, PublicationResult
from schemabridge.domain.workflows import AgentWorkflowDraft

_SCHEMA = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
_APPLICATION_NAME = re.compile(
    r"^schemabridge-control-(?:runtime|api|worker|publisher|catalog|reconciler|migrator)$"
)
_MAX_LIST_LIMIT = 100
_ModelT = TypeVar("_ModelT", bound=BaseModel)


class ControlConnectionProvider(Protocol):
    """Provide one lifecycle-managed control-plane connection context."""

    def connection(self) -> AbstractContextManager[psycopg.Connection[Any]]:
        """Acquire one bounded connection."""


@dataclass(frozen=True, slots=True)
class _ControlDatabase:
    dsn: str = field(repr=False)
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-runtime"
    connect_timeout_seconds: int = 5
    statement_timeout_ms: int = 5_000
    connection_provider: ControlConnectionProvider | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.dsn.strip():
            raise ValueError("control database DSN must not be blank")
        if _SCHEMA.fullmatch(self.schema) is None:
            raise ValueError("control database schema is invalid")
        if _APPLICATION_NAME.fullmatch(self.application_name) is None:
            raise ValueError("control database application name is invalid")
        if not 1 <= self.connect_timeout_seconds <= 30:
            raise ValueError("control database connect timeout is invalid")
        if not 100 <= self.statement_timeout_ms <= 60_000:
            raise ValueError("control database statement timeout is invalid")

    def connect(self) -> AbstractContextManager[psycopg.Connection[Any]]:
        if self.connection_provider is not None:
            return self.connection_provider.connection()
        connection = psycopg.connect(
            self.dsn,
            connect_timeout=self.connect_timeout_seconds,
            application_name=self.application_name,
        )
        connection.execute(
            "SELECT set_config('statement_timeout', %s, false)",
            (f"{self.statement_timeout_ms}ms",),
        )
        return connection

    def table(self, name: str) -> sql.Composed:
        return sql.SQL("{}.{}").format(sql.Identifier(self.schema), sql.Identifier(name))


class PostgresWorkflowDraftStore:
    """Tenant-bound optimistic workflow store with result-row minimization."""

    def __init__(
        self,
        dsn: str,
        *,
        workspace_id: str,
        owner_actor_id: str,
        schema: str = "schemabridge_control",
        application_name: str = "schemabridge-control-runtime",
        connection_provider: ControlConnectionProvider | None = None,
    ) -> None:
        self._db = _ControlDatabase(
            dsn,
            schema,
            application_name=application_name,
            connection_provider=connection_provider,
        )
        self._workspace_id = _scope_value(workspace_id, "workflow workspace")
        self._owner_actor_id = _scope_value(owner_actor_id, "workflow owner")

    def load(self, workflow_id: str) -> AgentWorkflowDraft | None:
        drafts = self._db.table("agent_workflow_drafts")
        grants = self._db.table("workflow_access_grants")
        query = sql.SQL(
            """
            SELECT draft.payload
            FROM {drafts} AS draft
            JOIN {grants} AS access
              ON access.workspace_id = draft.workspace_id
             AND access.workflow_id = draft.id
            WHERE draft.workspace_id = %s
              AND draft.id = %s
            """
        ).format(drafts=drafts, grants=grants)
        try:
            with self._db.connect() as connection:
                row = connection.execute(
                    query,
                    (self._workspace_id, workflow_id),
                ).fetchone()
            return None if row is None else _model(AgentWorkflowDraft, row[0])
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _workflow_store_failure() from error

    def save(
        self,
        draft: AgentWorkflowDraft,
        *,
        expected_revision: int | None,
    ) -> None:
        drafts = self._db.table("agent_workflow_drafts")
        grants = self._db.table("workflow_access_grants")
        payload, row_count, preview_fingerprint = _durable_workflow(draft)
        try:
            with self._db.connect() as connection:
                if expected_revision is None:
                    connection.execute(
                        sql.SQL(
                            """
                            INSERT INTO {drafts} (
                                workspace_id, id, revision, payload,
                                execution_row_count, execution_preview_fingerprint, updated_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                            """
                        ).format(drafts=drafts),
                        (
                            self._workspace_id,
                            draft.id,
                            draft.revision,
                            Jsonb(payload),
                            row_count,
                            preview_fingerprint,
                            draft.updated_at,
                        ),
                    )
                    connection.execute(
                        sql.SQL(
                            """
                            INSERT INTO {grants} (
                                workspace_id, workflow_id, owner_actor_id, created_at
                            ) VALUES (%s, %s, %s, %s)
                            """
                        ).format(grants=grants),
                        (
                            self._workspace_id,
                            draft.id,
                            self._owner_actor_id,
                            draft.created_at,
                        ),
                    )
                    return
                updated = connection.execute(
                    sql.SQL(
                        """
                        UPDATE {drafts} AS draft
                        SET revision = %s,
                            payload = %s,
                            execution_row_count = %s,
                            execution_preview_fingerprint = %s,
                            updated_at = %s
                        WHERE draft.workspace_id = %s
                          AND draft.id = %s
                          AND draft.revision = %s
                          AND EXISTS (
                              SELECT 1 FROM {grants} AS access
                              WHERE access.workspace_id = draft.workspace_id
                                AND access.workflow_id = draft.id
                          )
                        """
                    ).format(drafts=drafts, grants=grants),
                    (
                        draft.revision,
                        Jsonb(payload),
                        row_count,
                        preview_fingerprint,
                        draft.updated_at,
                        self._workspace_id,
                        draft.id,
                        expected_revision,
                    ),
                )
                if updated.rowcount != 1:
                    raise WorkflowError(
                        WorkflowErrorCode.CONFLICT,
                        "workflow draft changed; reload before continuing",
                    )
        except WorkflowError:
            raise
        except UniqueViolation as error:
            raise WorkflowError(
                WorkflowErrorCode.CONFLICT,
                "workflow draft or ownership already exists; resume it instead",
            ) from error
        except psycopg.Error as error:
            raise _workflow_store_failure() from error


class PostgresWorkflowAccessStore:
    """Immutable workflow ownership grants in the managed control plane."""

    def __init__(
        self,
        dsn: str,
        *,
        schema: str = "schemabridge_control",
        application_name: str = "schemabridge-control-runtime",
        connection_provider: ControlConnectionProvider | None = None,
    ) -> None:
        self._db = _ControlDatabase(
            dsn,
            schema,
            application_name=application_name,
            connection_provider=connection_provider,
        )

    def grant(self, grant: WorkflowAccessGrant) -> WorkflowAccessGrant:
        table = self._db.table("workflow_access_grants")
        try:
            with self._db.connect() as connection:
                connection.execute(
                    sql.SQL(
                        """
                        INSERT INTO {table} (
                            workspace_id, workflow_id, owner_actor_id, created_at
                        ) VALUES (%s, %s, %s, %s)
                        ON CONFLICT (workspace_id, workflow_id) DO NOTHING
                        """
                    ).format(table=table),
                    (
                        grant.workspace_id,
                        grant.workflow_id,
                        grant.owner_actor_id,
                        grant.created_at,
                    ),
                )
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT workflow_id, workspace_id, owner_actor_id, created_at
                        FROM {table}
                        WHERE workspace_id = %s AND workflow_id = %s
                        """
                    ).format(table=table),
                    (grant.workspace_id, grant.workflow_id),
                ).fetchone()
                if row is None:
                    raise _workflow_access_failure()
                current = _workflow_grant(row)
                if _same_grant(current, grant):
                    return current
                raise _workflow_ownership_conflict()
        except WorkflowAccessError:
            raise
        except UniqueViolation as error:
            raise _workflow_ownership_conflict() from error
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _workflow_access_failure() from error

    def load(
        self,
        workspace_id: str,
        workflow_id: str,
        *,
        owner_principal_id: str | None = None,
    ) -> WorkflowAccessGrant | None:
        table = self._db.table("workflow_access_grants")
        owner_clause = (
            sql.SQL(" AND owner_actor_id = %s") if owner_principal_id is not None else sql.SQL("")
        )
        params: tuple[object, ...] = (workspace_id, workflow_id)
        if owner_principal_id is not None:
            params = (*params, owner_principal_id)
        try:
            with self._db.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT workflow_id, workspace_id, owner_actor_id, created_at
                        FROM {table}
                        WHERE workspace_id = %s AND workflow_id = %s{owner_clause}
                        """
                    ).format(table=table, owner_clause=owner_clause),
                    params,
                ).fetchone()
            return None if row is None else _workflow_grant(row)
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _workflow_access_failure() from error

    def list_for_workspace(
        self,
        workspace_id: str,
        *,
        owner_principal_id: str | None = None,
        limit: int = 50,
    ) -> tuple[WorkflowAccessGrant, ...]:
        _validate_limit(limit)
        table = self._db.table("workflow_access_grants")
        owner_clause = (
            sql.SQL(" AND owner_actor_id = %s") if owner_principal_id is not None else sql.SQL("")
        )
        params: tuple[object, ...] = (workspace_id,)
        if owner_principal_id is not None:
            params = (*params, owner_principal_id)
        params = (*params, limit)
        try:
            with self._db.connect() as connection:
                rows = connection.execute(
                    sql.SQL(
                        """
                        SELECT workflow_id, workspace_id, owner_actor_id, created_at
                        FROM {table}
                        WHERE workspace_id = %s{owner_clause}
                        ORDER BY created_at DESC, workflow_id ASC
                        LIMIT %s
                        """
                    ).format(table=table, owner_clause=owner_clause),
                    params,
                ).fetchall()
            return tuple(_workflow_grant(row) for row in rows)
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _workflow_access_failure() from error


class PostgresRequestDraftStore:
    def __init__(
        self,
        dsn: str,
        *,
        workspace_id: str,
        schema: str = "schemabridge_control",
    ) -> None:
        self._db = _ControlDatabase(dsn, schema)
        self._workspace_id = _scope_value(workspace_id, "request workspace")

    def load(self, draft_id: str) -> AnalyticalRequestDraft | None:
        table = self._db.table("analytical_request_drafts")
        try:
            with self._db.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        "SELECT payload FROM {table} WHERE workspace_id = %s AND id = %s"
                    ).format(table=table),
                    (self._workspace_id, draft_id),
                ).fetchone()
            return None if row is None else _model(AnalyticalRequestDraft, row[0])
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _request_store_failure() from error

    def save(
        self,
        draft: AnalyticalRequestDraft,
        *,
        expected_revision: int | None,
    ) -> None:
        table = self._db.table("analytical_request_drafts")
        try:
            with self._db.connect() as connection:
                if expected_revision is None:
                    connection.execute(
                        sql.SQL(
                            """
                            INSERT INTO {table} (
                                workspace_id, id, revision, payload, updated_at
                            ) VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                            """
                        ).format(table=table),
                        (
                            self._workspace_id,
                            draft.id,
                            draft.revision,
                            Jsonb(draft.model_dump(mode="json")),
                        ),
                    )
                    return
                updated = connection.execute(
                    sql.SQL(
                        """
                        UPDATE {table}
                        SET revision = %s, payload = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE workspace_id = %s AND id = %s AND revision = %s
                        """
                    ).format(table=table),
                    (
                        draft.revision,
                        Jsonb(draft.model_dump(mode="json")),
                        self._workspace_id,
                        draft.id,
                        expected_revision,
                    ),
                )
                if updated.rowcount != 1:
                    raise RequestWorkflowError(
                        RequestWorkflowErrorCode.DRAFT_CONFLICT,
                        "request draft changed; reload before saving",
                    )
        except RequestWorkflowError:
            raise
        except UniqueViolation as error:
            raise RequestWorkflowError(
                RequestWorkflowErrorCode.DRAFT_CONFLICT,
                "request draft already exists; reload before saving",
            ) from error
        except psycopg.Error as error:
            raise _request_store_failure() from error


class PostgresReviewStore:
    def __init__(
        self,
        dsn: str,
        *,
        workspace_id: str,
        schema: str = "schemabridge_control",
    ) -> None:
        self._db = _ControlDatabase(dsn, schema)
        self._workspace_id = _scope_value(workspace_id, "review workspace")

    def create(self, draft: CanonicalReviewDraft) -> CanonicalReviewDraft:
        return self._create_draft(
            table_name="review_drafts",
            draft=draft,
            conflict_message="canonical review already exists with different content",
        )

    def load(self, draft_id: str) -> CanonicalReviewDraft | None:
        return self._load_draft("review_drafts", draft_id)

    def commit_decision(
        self,
        draft: CanonicalReviewDraft,
        decision: DecisionRecord,
        *,
        expected_revision: int,
    ) -> None:
        if draft.revision != expected_revision + 1:
            raise ReviewWorkflowError(
                ReviewErrorCode.CONFLICT,
                "replacement draft must advance exactly one revision",
            )
        drafts = self._db.table("review_drafts")
        decisions = self._db.table("review_decisions")
        try:
            with self._db.connect() as connection:
                updated = connection.execute(
                    sql.SQL(
                        """
                        UPDATE {drafts}
                        SET revision = %s, payload = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE workspace_id = %s AND id = %s AND revision = %s
                        """
                    ).format(drafts=drafts),
                    (
                        draft.revision,
                        Jsonb(draft.model_dump(mode="json")),
                        self._workspace_id,
                        draft.id,
                        expected_revision,
                    ),
                )
                if updated.rowcount != 1:
                    raise ReviewWorkflowError(
                        ReviewErrorCode.CONFLICT,
                        "canonical review revision changed; reload before deciding",
                    )
                connection.execute(
                    sql.SQL(
                        """
                        INSERT INTO {decisions} (
                            workspace_id, id, draft_id, resulting_version, payload, decided_at
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        """
                    ).format(decisions=decisions),
                    (
                        self._workspace_id,
                        decision.id,
                        draft.id,
                        decision.resulting_version,
                        Jsonb(decision.model_dump(mode="json")),
                        decision.decided_at,
                    ),
                )
        except ReviewWorkflowError:
            raise
        except UniqueViolation as error:
            raise ReviewWorkflowError(
                ReviewErrorCode.CONFLICT,
                "decision record already exists and is immutable",
            ) from error
        except psycopg.Error as error:
            raise _review_store_failure() from error

    def list_decisions(self, draft_id: str) -> tuple[DecisionRecord, ...]:
        table = self._db.table("review_decisions")
        try:
            with self._db.connect() as connection:
                rows = connection.execute(
                    sql.SQL(
                        """
                        SELECT payload FROM {table}
                        WHERE workspace_id = %s AND draft_id = %s
                        ORDER BY resulting_version, id
                        """
                    ).format(table=table),
                    (self._workspace_id, draft_id),
                ).fetchall()
            return tuple(_model(DecisionRecord, row[0]) for row in rows)
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _review_store_failure() from error

    def record_publication(self, result: PublicationResult) -> None:
        table = self._db.table("review_publications")
        approved_at = result.audit_records[0].approved_at
        try:
            with self._db.connect() as connection:
                inserted = connection.execute(
                    sql.SQL(
                        """
                        INSERT INTO {table} (
                            workspace_id, draft_id, approval_id, fingerprint,
                            payload, published_at
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (workspace_id, approval_id) DO NOTHING
                        RETURNING sequence
                        """
                    ).format(table=table),
                    (
                        self._workspace_id,
                        result.draft_id,
                        result.approval_id,
                        result.fingerprint,
                        Jsonb(result.model_dump(mode="json")),
                        approved_at,
                    ),
                ).fetchone()
                if inserted is None:
                    existing = connection.execute(
                        sql.SQL(
                            """
                            SELECT payload FROM {table}
                            WHERE workspace_id = %s AND approval_id = %s
                            """
                        ).format(table=table),
                        (self._workspace_id, result.approval_id),
                    ).fetchone()
                    if existing is None or _model(PublicationResult, existing[0]) != result:
                        raise ReviewWorkflowError(
                            ReviewErrorCode.CONFLICT,
                            "publication approval already identifies another result",
                        )
        except ReviewWorkflowError:
            raise
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _review_store_failure() from error

    def list_publications(self, draft_id: str) -> tuple[PublicationResult, ...]:
        table = self._db.table("review_publications")
        try:
            with self._db.connect() as connection:
                rows = connection.execute(
                    sql.SQL(
                        """
                        SELECT payload FROM {table}
                        WHERE workspace_id = %s AND draft_id = %s
                        ORDER BY sequence
                        """
                    ).format(table=table),
                    (self._workspace_id, draft_id),
                ).fetchall()
            return tuple(_model(PublicationResult, row[0]) for row in rows)
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _review_store_failure() from error

    def _create_draft(
        self,
        *,
        table_name: str,
        draft: CanonicalReviewDraft,
        conflict_message: str,
    ) -> CanonicalReviewDraft:
        table = self._db.table(table_name)
        try:
            with self._db.connect() as connection:
                connection.execute(
                    sql.SQL(
                        """
                        INSERT INTO {table} (
                            workspace_id, id, revision, payload, updated_at
                        ) VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (workspace_id, id) DO NOTHING
                        """
                    ).format(table=table),
                    (
                        self._workspace_id,
                        draft.id,
                        draft.revision,
                        Jsonb(draft.model_dump(mode="json")),
                    ),
                )
                row = connection.execute(
                    sql.SQL(
                        "SELECT payload FROM {table} WHERE workspace_id = %s AND id = %s"
                    ).format(table=table),
                    (self._workspace_id, draft.id),
                ).fetchone()
                if row is None:
                    raise _review_store_failure()
                current = _model(CanonicalReviewDraft, row[0])
                if current != draft:
                    raise ReviewWorkflowError(ReviewErrorCode.CONFLICT, conflict_message)
                return current
        except ReviewWorkflowError:
            raise
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _review_store_failure() from error

    def _load_draft(
        self,
        table_name: str,
        draft_id: str,
    ) -> CanonicalReviewDraft | None:
        table = self._db.table(table_name)
        try:
            with self._db.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        "SELECT payload FROM {table} WHERE workspace_id = %s AND id = %s"
                    ).format(table=table),
                    (self._workspace_id, draft_id),
                ).fetchone()
            return None if row is None else _model(CanonicalReviewDraft, row[0])
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _review_store_failure() from error


class PostgresJoinReviewStore:
    def __init__(
        self,
        dsn: str,
        *,
        workspace_id: str,
        schema: str = "schemabridge_control",
    ) -> None:
        self._db = _ControlDatabase(dsn, schema)
        self._workspace_id = _scope_value(workspace_id, "join review workspace")

    def create(self, draft: JoinReviewDraft) -> JoinReviewDraft:
        table = self._db.table("join_review_drafts")
        try:
            with self._db.connect() as connection:
                connection.execute(
                    sql.SQL(
                        """
                        INSERT INTO {table} (
                            workspace_id, id, revision, payload, updated_at
                        ) VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (workspace_id, id) DO NOTHING
                        """
                    ).format(table=table),
                    (
                        self._workspace_id,
                        draft.id,
                        draft.revision,
                        Jsonb(draft.model_dump(mode="json")),
                    ),
                )
                row = connection.execute(
                    sql.SQL(
                        "SELECT payload FROM {table} WHERE workspace_id = %s AND id = %s"
                    ).format(table=table),
                    (self._workspace_id, draft.id),
                ).fetchone()
                if row is None:
                    raise _join_store_failure()
                current = _model(JoinReviewDraft, row[0])
                if current != draft:
                    raise RelationshipWorkflowError(
                        RelationshipErrorCode.CONFLICT,
                        "join review already exists with different content",
                    )
                return current
        except RelationshipWorkflowError:
            raise
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _join_store_failure() from error

    def load(self, draft_id: str) -> JoinReviewDraft | None:
        table = self._db.table("join_review_drafts")
        try:
            with self._db.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        "SELECT payload FROM {table} WHERE workspace_id = %s AND id = %s"
                    ).format(table=table),
                    (self._workspace_id, draft_id),
                ).fetchone()
            return None if row is None else _model(JoinReviewDraft, row[0])
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _join_store_failure() from error

    def commit_decision(
        self,
        draft: JoinReviewDraft,
        decision: DecisionRecord,
        *,
        expected_revision: int,
    ) -> None:
        if draft.revision != expected_revision + 1:
            raise RelationshipWorkflowError(
                RelationshipErrorCode.CONFLICT,
                "replacement join draft must advance exactly one revision",
            )
        drafts = self._db.table("join_review_drafts")
        decisions = self._db.table("join_review_decisions")
        try:
            with self._db.connect() as connection:
                updated = connection.execute(
                    sql.SQL(
                        """
                        UPDATE {drafts}
                        SET revision = %s, payload = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE workspace_id = %s AND id = %s AND revision = %s
                        """
                    ).format(drafts=drafts),
                    (
                        draft.revision,
                        Jsonb(draft.model_dump(mode="json")),
                        self._workspace_id,
                        draft.id,
                        expected_revision,
                    ),
                )
                if updated.rowcount != 1:
                    raise RelationshipWorkflowError(
                        RelationshipErrorCode.CONFLICT,
                        "join review revision changed; reload before deciding",
                    )
                connection.execute(
                    sql.SQL(
                        """
                        INSERT INTO {decisions} (
                            workspace_id, id, draft_id, resulting_version, payload, decided_at
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        """
                    ).format(decisions=decisions),
                    (
                        self._workspace_id,
                        decision.id,
                        draft.id,
                        decision.resulting_version,
                        Jsonb(decision.model_dump(mode="json")),
                        decision.decided_at,
                    ),
                )
        except RelationshipWorkflowError:
            raise
        except UniqueViolation as error:
            raise RelationshipWorkflowError(
                RelationshipErrorCode.CONFLICT,
                "join decision already exists and is immutable",
            ) from error
        except psycopg.Error as error:
            raise _join_store_failure() from error

    def list_decisions(self, draft_id: str) -> tuple[DecisionRecord, ...]:
        table = self._db.table("join_review_decisions")
        try:
            with self._db.connect() as connection:
                rows = connection.execute(
                    sql.SQL(
                        """
                        SELECT payload FROM {table}
                        WHERE workspace_id = %s AND draft_id = %s
                        ORDER BY resulting_version, id
                        """
                    ).format(table=table),
                    (self._workspace_id, draft_id),
                ).fetchall()
            return tuple(_model(DecisionRecord, row[0]) for row in rows)
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _join_store_failure() from error

    def record_publication(self, result: JoinPublicationResult) -> None:
        table = self._db.table("join_publications")
        approved_at = result.audit_records[0].approved_at
        try:
            with self._db.connect() as connection:
                inserted = connection.execute(
                    sql.SQL(
                        """
                        INSERT INTO {table} (
                            workspace_id, draft_id, approval_id, fingerprint,
                            payload, published_at
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (workspace_id, approval_id) DO NOTHING
                        RETURNING sequence
                        """
                    ).format(table=table),
                    (
                        self._workspace_id,
                        result.draft_id,
                        result.approval_id,
                        result.fingerprint,
                        Jsonb(result.model_dump(mode="json")),
                        approved_at,
                    ),
                ).fetchone()
                if inserted is None:
                    existing = connection.execute(
                        sql.SQL(
                            """
                            SELECT payload FROM {table}
                            WHERE workspace_id = %s AND approval_id = %s
                            """
                        ).format(table=table),
                        (self._workspace_id, result.approval_id),
                    ).fetchone()
                    if existing is None or _model(JoinPublicationResult, existing[0]) != result:
                        raise RelationshipWorkflowError(
                            RelationshipErrorCode.CONFLICT,
                            "join publication approval already identifies another result",
                        )
        except RelationshipWorkflowError:
            raise
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _join_store_failure() from error

    def list_publications(self, draft_id: str) -> tuple[JoinPublicationResult, ...]:
        table = self._db.table("join_publications")
        try:
            with self._db.connect() as connection:
                rows = connection.execute(
                    sql.SQL(
                        """
                        SELECT payload FROM {table}
                        WHERE workspace_id = %s AND draft_id = %s
                        ORDER BY sequence
                        """
                    ).format(table=table),
                    (self._workspace_id, draft_id),
                ).fetchall()
            return tuple(_model(JoinPublicationResult, row[0]) for row in rows)
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _join_store_failure() from error


class PostgresPublicationAuditStore:
    """Tenant-bound, replay-safe append-only publication audit."""

    def __init__(
        self,
        dsn: str,
        *,
        workspace_id: str,
        schema: str = "schemabridge_control",
    ) -> None:
        self._db = _ControlDatabase(dsn, schema)
        self._workspace_id = _scope_value(workspace_id, "publication audit workspace")

    def append(self, records: tuple[PublicationTargetAuditRecord, ...]) -> None:
        approval_id, identity = _publication_identity(records)
        identities = self._db.table("publication_approval_identity")
        targets = self._db.table("publication_target_audit")
        try:
            with self._db.connect() as connection:
                connection.execute(
                    sql.SQL(
                        """
                        INSERT INTO {identities} (
                            workspace_id, approval_id, family, actor,
                            approved_at, new_fingerprint
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (workspace_id, approval_id) DO NOTHING
                        """
                    ).format(identities=identities),
                    (self._workspace_id, approval_id, *identity),
                )
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT family, actor, approved_at, new_fingerprint
                        FROM {identities}
                        WHERE workspace_id = %s AND approval_id = %s
                        """
                    ).format(identities=identities),
                    (self._workspace_id, approval_id),
                ).fetchone()
                if row is None or _database_publication_identity(row) != identity:
                    raise PublicationAuditStoreError("publication approval audit identity changed")
                existing_rows = connection.execute(
                    sql.SQL(
                        """
                        SELECT record_json FROM {targets}
                        WHERE workspace_id = %s AND approval_id = %s
                        ORDER BY sequence
                        """
                    ).format(targets=targets),
                    (self._workspace_id, approval_id),
                ).fetchall()
                existing = tuple(
                    _model(PublicationTargetAuditRecord, existing_row[0])
                    for existing_row in existing_rows
                )
                if existing:
                    existing_subjects = {_publication_audit_subject(record) for record in existing}
                    if any(
                        _publication_audit_subject(record) not in existing_subjects
                        for record in records
                    ):
                        raise PublicationAuditStoreError(
                            "publication approval audit targets changed"
                        )
                    records = tuple(record for record in records if record not in existing)
                    if not records:
                        return
                with connection.cursor() as cursor:
                    cursor.executemany(
                        sql.SQL(
                            """
                            INSERT INTO {targets} (
                                workspace_id, approval_id, family, operation,
                                target, record_json, appended_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                            """
                        ).format(targets=targets),
                        tuple(
                            (
                                self._workspace_id,
                                record.approval_id,
                                record.family.value,
                                record.operation,
                                record.target,
                                Jsonb(record.model_dump(mode="json")),
                                record.approved_at,
                            )
                            for record in records
                        ),
                    )
        except PublicationAuditStoreError:
            raise
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise PublicationAuditStoreError("publication audit append failed") from error

    def list_for_approval(self, approval_id: str) -> tuple[PublicationTargetAuditRecord, ...]:
        table = self._db.table("publication_target_audit")
        try:
            with self._db.connect() as connection:
                rows = connection.execute(
                    sql.SQL(
                        """
                        SELECT record_json FROM {table}
                        WHERE workspace_id = %s AND approval_id = %s
                        ORDER BY sequence
                        """
                    ).format(table=table),
                    (self._workspace_id, approval_id),
                ).fetchall()
            return tuple(_model(PublicationTargetAuditRecord, row[0]) for row in rows)
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise PublicationAuditStoreError("publication audit read failed") from error


def _model(model: type[_ModelT], value: object) -> _ModelT:
    return model.model_validate(value)


def _scope_value(value: str, label: str) -> str:
    if not value.strip() or value != value.strip() or len(value) > 200:
        raise ValueError(f"{label} is invalid")
    return value


def _durable_workflow(
    draft: AgentWorkflowDraft,
) -> tuple[dict[str, object], int | None, str | None]:
    payload = cast(dict[str, object], draft.model_dump(mode="json"))
    execution = payload.get("execution")
    if execution is None:
        return payload, None, None
    if not isinstance(execution, dict):
        raise ValueError("workflow execution payload is invalid")
    typed_execution = draft.execution
    if typed_execution is None:
        raise ValueError("workflow execution summary is missing")
    row_count = typed_execution.observed_row_count
    execution["rows"] = []
    execution["row_count"] = row_count
    return payload, row_count, typed_execution.preview_fingerprint


def _workflow_grant(row: tuple[object, ...]) -> WorkflowAccessGrant:
    return WorkflowAccessGrant.model_validate(
        {
            "workflow_id": row[0],
            "workspace_id": row[1],
            "owner_actor_id": row[2],
            "created_at": row[3],
        }
    )


def _same_grant(left: WorkflowAccessGrant, right: WorkflowAccessGrant) -> bool:
    return (
        left.workflow_id == right.workflow_id
        and left.workspace_id == right.workspace_id
        and left.owner_actor_id == right.owner_actor_id
    )


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not 1 <= limit <= _MAX_LIST_LIMIT:
        raise ValueError("workflow access list limit must be between 1 and 100")


def _publication_identity(
    records: tuple[PublicationTargetAuditRecord, ...],
) -> tuple[str, tuple[str, str, object, str]]:
    if not records:
        raise PublicationAuditStoreError("publication audit append cannot be empty")
    first = records[0]
    identity: tuple[str, str, object, str] = (
        first.family.value,
        first.actor,
        first.approved_at.astimezone(UTC),
        first.new_fingerprint,
    )
    if any(
        record.approval_id != first.approval_id
        or (
            record.family.value,
            record.actor,
            record.approved_at.astimezone(UTC),
            record.new_fingerprint,
        )
        != identity
        for record in records
    ):
        raise PublicationAuditStoreError(
            "one publication audit append must share an immutable approval identity"
        )
    return first.approval_id, identity


def _database_publication_identity(
    row: tuple[object, ...],
) -> tuple[str, str, object, str]:
    timestamp = row[2]
    if hasattr(timestamp, "astimezone"):
        timestamp = timestamp.astimezone(UTC)
    return str(row[0]), str(row[1]), timestamp, str(row[3])


def _publication_audit_subject(
    record: PublicationTargetAuditRecord,
) -> tuple[str, str, str, tuple[str, ...]]:
    return (
        record.family.value,
        record.operation,
        record.target,
        record.decision_ids,
    )


def _workflow_store_failure() -> WorkflowError:
    return WorkflowError(WorkflowErrorCode.STORE_FAILURE, "workflow control store failed")


def _workflow_ownership_conflict() -> WorkflowAccessError:
    return WorkflowAccessError(
        WorkflowAccessErrorCode.CONFLICT,
        "workflow ownership already exists with a different binding",
    )


def _workflow_access_failure() -> WorkflowAccessError:
    return WorkflowAccessError(
        WorkflowAccessErrorCode.STORE_FAILURE,
        "workflow access control store failed",
    )


def _request_store_failure() -> RequestWorkflowError:
    return RequestWorkflowError(
        RequestWorkflowErrorCode.STORE_FAILURE,
        "request control store failed",
    )


def _review_store_failure() -> ReviewWorkflowError:
    return ReviewWorkflowError(ReviewErrorCode.STORE_FAILURE, "review control store failed")


def _join_store_failure() -> RelationshipWorkflowError:
    return RelationshipWorkflowError(
        RelationshipErrorCode.STORE_FAILURE,
        "join-review control store failed",
    )
