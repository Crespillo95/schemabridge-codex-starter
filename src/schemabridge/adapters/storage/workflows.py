"""SQLite persistence for typed, resumable agent workflow drafts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pydantic import ValidationError

from schemabridge.application.ports.workflows import (
    WorkflowError,
    WorkflowErrorCode,
)
from schemabridge.domain.workflows import AgentWorkflowDraft


class SqliteWorkflowDraftStore:
    """Persist only bounded workflow state in the local application database."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def load(self, workflow_id: str) -> AgentWorkflowDraft | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT payload FROM agent_workflow_drafts WHERE id = ?",
                    (workflow_id,),
                ).fetchone()
            return None if row is None else AgentWorkflowDraft.model_validate_json(row[0])
        except ValidationError as error:
            raise WorkflowError(
                WorkflowErrorCode.STORE_FAILURE,
                "local workflow draft contains invalid typed data",
            ) from error
        except sqlite3.Error as error:
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
                    try:
                        connection.execute(
                            """
                            INSERT INTO agent_workflow_drafts (id, revision, payload)
                            VALUES (?, ?, ?)
                            """,
                            (draft.id, draft.revision, draft.model_dump_json()),
                        )
                    except sqlite3.IntegrityError as error:
                        raise WorkflowError(
                            WorkflowErrorCode.CONFLICT,
                            "workflow draft already exists; resume it instead",
                        ) from error
                    return
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
                if updated.rowcount != 1:
                    raise WorkflowError(
                        WorkflowErrorCode.CONFLICT,
                        "workflow draft changed; reload before continuing",
                    )
        except WorkflowError:
            raise
        except sqlite3.Error as error:
            raise _store_failure() from error

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, isolation_level=None, timeout=5.0)

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_workflow_drafts (
                        id TEXT PRIMARY KEY,
                        revision INTEGER NOT NULL,
                        payload TEXT NOT NULL
                    )
                    """
                )
        except sqlite3.Error as error:
            raise _store_failure() from error


def _store_failure() -> WorkflowError:
    return WorkflowError(
        WorkflowErrorCode.STORE_FAILURE,
        "local workflow draft store failed",
    )
