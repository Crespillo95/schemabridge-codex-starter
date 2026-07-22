"""Local durable stores for drafts, immutable decisions, and publication attempts."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import RLock

from schemabridge.application.ports.reviews import ReviewErrorCode, ReviewWorkflowError
from schemabridge.domain.decisions import DecisionRecord
from schemabridge.domain.reviews import CanonicalReviewDraft, PublicationResult


class InMemoryReviewStore:
    """Deterministic test/demo store implementing optimistic draft revisions."""

    def __init__(self) -> None:
        self._drafts: dict[str, CanonicalReviewDraft] = {}
        self._decisions: dict[str, list[DecisionRecord]] = {}
        self._publications: dict[str, list[PublicationResult]] = {}
        self._lock = RLock()

    def create(self, draft: CanonicalReviewDraft) -> CanonicalReviewDraft:
        with self._lock:
            current = self._drafts.get(draft.id)
            if current is not None and current != draft:
                raise ReviewWorkflowError(
                    ReviewErrorCode.CONFLICT,
                    "canonical review already exists with different content",
                )
            self._drafts.setdefault(draft.id, draft)
            return self._drafts[draft.id]

    def load(self, draft_id: str) -> CanonicalReviewDraft | None:
        with self._lock:
            return self._drafts.get(draft_id)

    def commit_decision(
        self,
        draft: CanonicalReviewDraft,
        decision: DecisionRecord,
        *,
        expected_revision: int,
    ) -> None:
        with self._lock:
            current = self._drafts.get(draft.id)
            if current is None:
                raise ReviewWorkflowError(ReviewErrorCode.NOT_FOUND, "review was not found")
            if current.revision != expected_revision or draft.revision != expected_revision + 1:
                raise ReviewWorkflowError(
                    ReviewErrorCode.CONFLICT,
                    "canonical review revision changed; reload before deciding",
                )
            if any(item.id == decision.id for item in self._decisions.get(draft.id, ())):
                raise ReviewWorkflowError(
                    ReviewErrorCode.CONFLICT,
                    "decision record already exists and is immutable",
                )
            self._decisions.setdefault(draft.id, []).append(decision)
            self._drafts[draft.id] = draft

    def list_decisions(self, draft_id: str) -> tuple[DecisionRecord, ...]:
        with self._lock:
            return tuple(self._decisions.get(draft_id, ()))

    def record_publication(self, result: PublicationResult) -> None:
        with self._lock:
            self._publications.setdefault(result.draft_id, []).append(result)

    def list_publications(self, draft_id: str) -> tuple[PublicationResult, ...]:
        with self._lock:
            return tuple(self._publications.get(draft_id, ()))


class SqliteReviewStore:
    """SQLite-backed local review store; it never connects to a source database."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def create(self, draft: CanonicalReviewDraft) -> CanonicalReviewDraft:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT payload FROM review_drafts WHERE id = ?", (draft.id,)
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO review_drafts (id, revision, payload) VALUES (?, ?, ?)",
                        (draft.id, draft.revision, draft.model_dump_json()),
                    )
                    return draft
                current = CanonicalReviewDraft.model_validate_json(row[0])
                if current != draft:
                    raise ReviewWorkflowError(
                        ReviewErrorCode.CONFLICT,
                        "canonical review already exists with different content",
                    )
                return current
        except sqlite3.Error as error:
            raise _store_failure(error) from error

    def load(self, draft_id: str) -> CanonicalReviewDraft | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT payload FROM review_drafts WHERE id = ?", (draft_id,)
                ).fetchone()
            return None if row is None else CanonicalReviewDraft.model_validate_json(row[0])
        except sqlite3.Error as error:
            raise _store_failure(error) from error

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
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                updated = connection.execute(
                    """
                    UPDATE review_drafts SET revision = ?, payload = ?
                    WHERE id = ? AND revision = ?
                    """,
                    (draft.revision, draft.model_dump_json(), draft.id, expected_revision),
                )
                if updated.rowcount != 1:
                    raise ReviewWorkflowError(
                        ReviewErrorCode.CONFLICT,
                        "canonical review revision changed; reload before deciding",
                    )
                try:
                    connection.execute(
                        """
                        INSERT INTO review_decisions
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
                    raise ReviewWorkflowError(
                        ReviewErrorCode.CONFLICT,
                        "decision record already exists and is immutable",
                    ) from error
        except ReviewWorkflowError:
            raise
        except sqlite3.Error as error:
            raise _store_failure(error) from error

    def list_decisions(self, draft_id: str) -> tuple[DecisionRecord, ...]:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT payload FROM review_decisions
                    WHERE draft_id = ? ORDER BY resulting_version, id
                    """,
                    (draft_id,),
                ).fetchall()
            return tuple(DecisionRecord.model_validate_json(row[0]) for row in rows)
        except sqlite3.Error as error:
            raise _store_failure(error) from error

    def record_publication(self, result: PublicationResult) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO review_publications
                        (draft_id, approval_id, fingerprint, payload)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        result.draft_id,
                        result.approval_id,
                        result.fingerprint,
                        result.model_dump_json(),
                    ),
                )
        except sqlite3.Error as error:
            raise _store_failure(error) from error

    def list_publications(self, draft_id: str) -> tuple[PublicationResult, ...]:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT payload FROM review_publications
                    WHERE draft_id = ? ORDER BY sequence
                    """,
                    (draft_id,),
                ).fetchall()
            return tuple(PublicationResult.model_validate_json(row[0]) for row in rows)
        except sqlite3.Error as error:
            raise _store_failure(error) from error

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, isolation_level=None, timeout=5.0)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS review_drafts (
                        id TEXT PRIMARY KEY,
                        revision INTEGER NOT NULL,
                        payload TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS review_decisions (
                        id TEXT PRIMARY KEY,
                        draft_id TEXT NOT NULL,
                        resulting_version INTEGER NOT NULL,
                        payload TEXT NOT NULL,
                        FOREIGN KEY (draft_id) REFERENCES review_drafts(id),
                        UNIQUE (draft_id, resulting_version)
                    );
                    CREATE TABLE IF NOT EXISTS review_publications (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        draft_id TEXT NOT NULL,
                        approval_id TEXT NOT NULL,
                        fingerprint TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        FOREIGN KEY (draft_id) REFERENCES review_drafts(id)
                    );
                    """
                )
        except sqlite3.Error as error:
            raise _store_failure(error) from error


def _store_failure(error: sqlite3.Error) -> ReviewWorkflowError:
    del error
    return ReviewWorkflowError(ReviewErrorCode.STORE_FAILURE, "local review store failed")
