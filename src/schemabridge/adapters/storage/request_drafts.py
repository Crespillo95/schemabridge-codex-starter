"""SQLite persistence for local analytical request drafts only."""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path

from pydantic import ValidationError

from schemabridge.adapters.storage.sqlite_connection import managed_sqlite_connection
from schemabridge.application.ports.requests import (
    RequestWorkflowError,
    RequestWorkflowErrorCode,
)
from schemabridge.domain.requests import AnalyticalRequestDraft


class SqliteRequestDraftStore:
    """Persist typed local drafts without connecting to source or catalog databases."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def load(self, draft_id: str) -> AnalyticalRequestDraft | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT payload FROM analytical_request_drafts WHERE id = ?",
                    (draft_id,),
                ).fetchone()
            return None if row is None else AnalyticalRequestDraft.model_validate_json(row[0])
        except ValidationError as error:
            raise RequestWorkflowError(
                RequestWorkflowErrorCode.STORE_FAILURE,
                "local request draft contains invalid typed data",
            ) from error
        except sqlite3.Error as error:
            raise _store_failure(error) from error

    def save(
        self,
        draft: AnalyticalRequestDraft,
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
                            INSERT INTO analytical_request_drafts (id, revision, payload)
                            VALUES (?, ?, ?)
                            """,
                            (draft.id, draft.revision, draft.model_dump_json()),
                        )
                    except sqlite3.IntegrityError as error:
                        raise RequestWorkflowError(
                            RequestWorkflowErrorCode.DRAFT_CONFLICT,
                            "request draft already exists; reload before saving",
                        ) from error
                    return
                updated = connection.execute(
                    """
                    UPDATE analytical_request_drafts SET revision = ?, payload = ?
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
                    raise RequestWorkflowError(
                        RequestWorkflowErrorCode.DRAFT_CONFLICT,
                        "request draft changed; reload before saving",
                    )
        except RequestWorkflowError:
            raise
        except sqlite3.Error as error:
            raise _store_failure(error) from error

    def _connect(self) -> AbstractContextManager[sqlite3.Connection]:
        return managed_sqlite_connection(self._path, isolation_level=None)

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS analytical_request_drafts (
                        id TEXT PRIMARY KEY,
                        revision INTEGER NOT NULL,
                        payload TEXT NOT NULL
                    )
                    """
                )
        except sqlite3.Error as error:
            raise _store_failure(error) from error


def _store_failure(error: sqlite3.Error) -> RequestWorkflowError:
    del error
    return RequestWorkflowError(
        RequestWorkflowErrorCode.STORE_FAILURE,
        "local request draft store failed",
    )
