"""SQLite persistence for typed, resumable agent workflow drafts."""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path

from pydantic import ValidationError

from schemabridge.adapters.storage.sqlite_connection import managed_sqlite_connection
from schemabridge.application.ports.workflows import (
    WorkflowError,
    WorkflowErrorCode,
)
from schemabridge.domain.workflows import AgentWorkflowDraft


class SqliteWorkflowDraftStore:
    """Persist only bounded workflow state in the local application database."""

    def __init__(
        self,
        path: Path,
        *,
        workspace_id: str | None = None,
        owner_actor_id: str | None = None,
    ) -> None:
        if (workspace_id is None) != (owner_actor_id is None):
            raise ValueError("workflow scope requires both workspace and owner")
        if workspace_id is not None and not workspace_id.strip():
            raise ValueError("workflow workspace must not be blank")
        if owner_actor_id is not None and not owner_actor_id.strip():
            raise ValueError("workflow owner must not be blank")
        self._path = path
        self._workspace_id = workspace_id
        self._owner_actor_id = owner_actor_id
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise _store_failure() from error
        self._initialize()

    def load(self, workflow_id: str) -> AgentWorkflowDraft | None:
        try:
            with self._connect() as connection:
                if self._workspace_id is None:
                    row = connection.execute(
                        "SELECT payload FROM agent_workflow_drafts WHERE id = ?",
                        (workflow_id,),
                    ).fetchone()
                else:
                    row = connection.execute(
                        """
                        SELECT draft.payload
                        FROM agent_workflow_drafts AS draft
                        JOIN workflow_access_grants AS access
                          ON access.workflow_id = draft.id
                        WHERE draft.id = ? AND access.workspace_id = ?
                        """,
                        (workflow_id, self._workspace_id),
                    ).fetchone()
            return None if row is None else AgentWorkflowDraft.model_validate_json(row[0])
        except ValidationError as error:
            raise WorkflowError(
                WorkflowErrorCode.STORE_FAILURE,
                "local workflow draft contains invalid typed data",
            ) from error
        except (OSError, sqlite3.Error) as error:
            raise _store_failure() from error

    def save(
        self,
        draft: AgentWorkflowDraft,
        *,
        expected_revision: int | None,
    ) -> None:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if expected_revision is None:
                    self._insert_new(connection, draft)
                    return
                if self._workspace_id is None:
                    updated = connection.execute(
                        """
                        UPDATE agent_workflow_drafts SET revision = ?, payload = ?
                        WHERE id = ? AND revision = ?
                        """,
                        (
                            draft.revision,
                            draft.model_dump_json(),
                            draft.id,
                            expected_revision,
                        ),
                    )
                else:
                    updated = connection.execute(
                        """
                        UPDATE agent_workflow_drafts SET revision = ?, payload = ?
                        WHERE id = ? AND revision = ?
                          AND EXISTS (
                            SELECT 1 FROM workflow_access_grants AS access
                            WHERE access.workflow_id = agent_workflow_drafts.id
                              AND access.workspace_id = ?
                          )
                        """,
                        (
                            draft.revision,
                            draft.model_dump_json(),
                            draft.id,
                            expected_revision,
                            self._workspace_id,
                        ),
                    )
                if updated.rowcount != 1:
                    raise WorkflowError(
                        WorkflowErrorCode.CONFLICT,
                        "workflow draft changed; reload before continuing",
                    )
        except WorkflowError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise _store_failure() from error

    def _connect(self) -> AbstractContextManager[sqlite3.Connection]:
        return managed_sqlite_connection(self._path, isolation_level=None)

    def _insert_new(
        self,
        connection: sqlite3.Connection,
        draft: AgentWorkflowDraft,
    ) -> None:
        try:
            if self._workspace_id is not None:
                reserved = connection.execute(
                    "SELECT 1 FROM workflow_access_grants WHERE workflow_id = ?",
                    (draft.id,),
                ).fetchone()
                if reserved is not None:
                    raise WorkflowError(
                        WorkflowErrorCode.CONFLICT,
                        "workflow identity is already reserved",
                    )
            connection.execute(
                """
                INSERT INTO agent_workflow_drafts (id, revision, payload)
                VALUES (?, ?, ?)
                """,
                (draft.id, draft.revision, draft.model_dump_json()),
            )
            if self._workspace_id is not None:
                assert self._owner_actor_id is not None
                connection.execute(
                    """
                    INSERT INTO workflow_access_grants
                        (workflow_id, workspace_id, owner_actor_id, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        draft.id,
                        self._workspace_id,
                        self._owner_actor_id,
                        draft.created_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise WorkflowError(
                WorkflowErrorCode.CONFLICT,
                "workflow draft or ownership already exists; resume it instead",
            ) from error

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS agent_workflow_drafts (
                        id TEXT PRIMARY KEY,
                        revision INTEGER NOT NULL,
                        payload TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS workflow_access_grants (
                        workflow_id TEXT PRIMARY KEY,
                        workspace_id TEXT NOT NULL CHECK (length(trim(workspace_id)) > 0),
                        owner_actor_id TEXT NOT NULL CHECK (length(trim(owner_actor_id)) > 0),
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS workflow_access_grants_scope_idx
                    ON workflow_access_grants (
                        workspace_id,
                        workflow_id,
                        owner_actor_id
                    );
                    """
                )
        except (OSError, sqlite3.Error) as error:
            raise _store_failure() from error


def _store_failure() -> WorkflowError:
    return WorkflowError(
        WorkflowErrorCode.STORE_FAILURE,
        "local workflow draft store failed",
    )
