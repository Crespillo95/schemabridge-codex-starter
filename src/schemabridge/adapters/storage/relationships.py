"""Local join-review state; this adapter never connects to a source database."""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path
from threading import RLock

from schemabridge.adapters.storage.sqlite_connection import managed_sqlite_connection
from schemabridge.application.ports.relationships import (
    RelationshipErrorCode,
    RelationshipWorkflowError,
)
from schemabridge.domain.decisions import DecisionRecord
from schemabridge.domain.join_reviews import JoinPublicationResult, JoinReviewDraft


class InMemoryJoinReviewStore:
    def __init__(self) -> None:
        self._drafts: dict[str, JoinReviewDraft] = {}
        self._decisions: dict[str, list[DecisionRecord]] = {}
        self._publications: dict[str, list[JoinPublicationResult]] = {}
        self._lock = RLock()

    def create(self, draft: JoinReviewDraft) -> JoinReviewDraft:
        with self._lock:
            current = self._drafts.get(draft.id)
            if current is not None and current != draft:
                raise RelationshipWorkflowError(
                    RelationshipErrorCode.CONFLICT,
                    "join review already exists with different content",
                )
            self._drafts.setdefault(draft.id, draft)
            return self._drafts[draft.id]

    def load(self, draft_id: str) -> JoinReviewDraft | None:
        with self._lock:
            return self._drafts.get(draft_id)

    def commit_decision(
        self,
        draft: JoinReviewDraft,
        decision: DecisionRecord,
        *,
        expected_revision: int,
    ) -> None:
        with self._lock:
            current = self._drafts.get(draft.id)
            if current is None:
                raise RelationshipWorkflowError(
                    RelationshipErrorCode.NOT_FOUND, "join review was not found"
                )
            if current.revision != expected_revision or draft.revision != expected_revision + 1:
                raise RelationshipWorkflowError(
                    RelationshipErrorCode.CONFLICT,
                    "join review revision changed; reload before deciding",
                )
            if any(item.id == decision.id for item in self._decisions.get(draft.id, ())):
                raise RelationshipWorkflowError(
                    RelationshipErrorCode.CONFLICT,
                    "join decision already exists and is immutable",
                )
            self._decisions.setdefault(draft.id, []).append(decision)
            self._drafts[draft.id] = draft

    def list_decisions(self, draft_id: str) -> tuple[DecisionRecord, ...]:
        with self._lock:
            return tuple(self._decisions.get(draft_id, ()))

    def record_publication(self, result: JoinPublicationResult) -> None:
        with self._lock:
            self._publications.setdefault(result.draft_id, []).append(result)

    def list_publications(self, draft_id: str) -> tuple[JoinPublicationResult, ...]:
        with self._lock:
            return tuple(self._publications.get(draft_id, ()))


class SqliteJoinReviewStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def create(self, draft: JoinReviewDraft) -> JoinReviewDraft:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT payload FROM join_review_drafts WHERE id = ?", (draft.id,)
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO join_review_drafts (id, revision, payload) VALUES (?, ?, ?)",
                        (draft.id, draft.revision, draft.model_dump_json()),
                    )
                    return draft
                current = JoinReviewDraft.model_validate_json(row[0])
                if current != draft:
                    raise RelationshipWorkflowError(
                        RelationshipErrorCode.CONFLICT,
                        "join review already exists with different content",
                    )
                return current
        except sqlite3.Error as error:
            raise _store_failure(error) from error

    def load(self, draft_id: str) -> JoinReviewDraft | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT payload FROM join_review_drafts WHERE id = ?", (draft_id,)
                ).fetchone()
            return None if row is None else JoinReviewDraft.model_validate_json(row[0])
        except sqlite3.Error as error:
            raise _store_failure(error) from error

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
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                updated = connection.execute(
                    """
                    UPDATE join_review_drafts SET revision = ?, payload = ?
                    WHERE id = ? AND revision = ?
                    """,
                    (draft.revision, draft.model_dump_json(), draft.id, expected_revision),
                )
                if updated.rowcount != 1:
                    raise RelationshipWorkflowError(
                        RelationshipErrorCode.CONFLICT,
                        "join review revision changed; reload before deciding",
                    )
                try:
                    connection.execute(
                        """
                        INSERT INTO join_review_decisions
                            (id, draft_id, resulting_version, payload)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            decision.id,
                            draft.id,
                            decision.resulting_version,
                            decision.model_dump_json(),
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    raise RelationshipWorkflowError(
                        RelationshipErrorCode.CONFLICT,
                        "join decision already exists and is immutable",
                    ) from error
        except sqlite3.Error as error:
            raise _store_failure(error) from error

    def list_decisions(self, draft_id: str) -> tuple[DecisionRecord, ...]:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT payload FROM join_review_decisions
                    WHERE draft_id = ? ORDER BY resulting_version, id
                    """,
                    (draft_id,),
                ).fetchall()
            return tuple(DecisionRecord.model_validate_json(row[0]) for row in rows)
        except sqlite3.Error as error:
            raise _store_failure(error) from error

    def record_publication(self, result: JoinPublicationResult) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO join_publications (draft_id, payload) VALUES (?, ?)",
                    (result.draft_id, result.model_dump_json()),
                )
        except sqlite3.Error as error:
            raise _store_failure(error) from error

    def list_publications(self, draft_id: str) -> tuple[JoinPublicationResult, ...]:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT payload FROM join_publications WHERE draft_id = ? ORDER BY sequence",
                    (draft_id,),
                ).fetchall()
            return tuple(JoinPublicationResult.model_validate_json(row[0]) for row in rows)
        except sqlite3.Error as error:
            raise _store_failure(error) from error

    def _connect(self) -> AbstractContextManager[sqlite3.Connection]:
        return managed_sqlite_connection(
            self._path,
            isolation_level="DEFERRED",
            foreign_keys=True,
        )

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS join_review_drafts (
                        id TEXT PRIMARY KEY,
                        revision INTEGER NOT NULL,
                        payload TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS join_review_decisions (
                        id TEXT PRIMARY KEY,
                        draft_id TEXT NOT NULL,
                        resulting_version INTEGER NOT NULL,
                        payload TEXT NOT NULL,
                        FOREIGN KEY (draft_id) REFERENCES join_review_drafts(id),
                        UNIQUE (draft_id, resulting_version)
                    );
                    CREATE TABLE IF NOT EXISTS join_publications (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        draft_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        FOREIGN KEY (draft_id) REFERENCES join_review_drafts(id)
                    );
                    """
                )
        except sqlite3.Error as error:
            raise _store_failure(error) from error


def _store_failure(error: sqlite3.Error) -> RelationshipWorkflowError:
    return RelationshipWorkflowError(
        RelationshipErrorCode.STORE_FAILURE,
        f"local join-review store failed ({error.__class__.__name__})",
    )
