"""Append-only in-memory and SQLite publication audit stores."""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from datetime import UTC
from pathlib import Path

from pydantic import ValidationError

from schemabridge.adapters.storage.sqlite_connection import managed_sqlite_connection
from schemabridge.application.ports.publication_audit import PublicationAuditStoreError
from schemabridge.domain.publication_audit import PublicationTargetAuditRecord


class InMemoryPublicationAuditStore:
    def __init__(self) -> None:
        self._records: list[PublicationTargetAuditRecord] = []
        self._identities: dict[str, tuple[str, str, str, str]] = {}

    def append(self, records: tuple[PublicationTargetAuditRecord, ...]) -> None:
        approval_id, identity = _approval_identity(records)
        existing = self._identities.get(approval_id)
        if existing is not None and existing != identity:
            raise PublicationAuditStoreError("publication approval audit identity changed")
        self._identities[approval_id] = identity
        self._records.extend(records)

    def list_for_approval(self, approval_id: str) -> tuple[PublicationTargetAuditRecord, ...]:
        return tuple(record for record in self._records if record.approval_id == approval_id)


class SqlitePublicationAuditStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise PublicationAuditStoreError(
                "publication audit store initialization failed"
            ) from error
        self._initialize()

    def append(self, records: tuple[PublicationTargetAuditRecord, ...]) -> None:
        approval_id, identity = _approval_identity(records)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """
                    SELECT family, actor, approved_at, new_fingerprint
                    FROM publication_approval_identity WHERE approval_id = ?
                    """,
                    (approval_id,),
                ).fetchone()
                if existing is not None and tuple(existing) != identity:
                    raise PublicationAuditStoreError("publication approval audit identity changed")
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO publication_approval_identity (
                            approval_id, family, actor, approved_at, new_fingerprint
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (approval_id, *identity),
                    )
                connection.executemany(
                    """
                    INSERT INTO publication_target_audit (
                        approval_id, family, operation, target, record_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    tuple(
                        (
                            record.approval_id,
                            record.family.value,
                            record.operation,
                            record.target,
                            record.model_dump_json(),
                        )
                        for record in records
                    ),
                )
                connection.commit()
        except (OSError, sqlite3.Error) as error:
            raise PublicationAuditStoreError("publication audit append failed") from error

    def list_for_approval(self, approval_id: str) -> tuple[PublicationTargetAuditRecord, ...]:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT record_json FROM publication_target_audit
                    WHERE approval_id = ? ORDER BY sequence
                    """,
                    (approval_id,),
                ).fetchall()
            return tuple(PublicationTargetAuditRecord.model_validate_json(row[0]) for row in rows)
        except (OSError, sqlite3.Error, ValidationError) as error:
            raise PublicationAuditStoreError("publication audit read failed") from error

    def _connect(self) -> AbstractContextManager[sqlite3.Connection]:
        return managed_sqlite_connection(self._path, isolation_level=None)

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS publication_approval_identity (
                        approval_id TEXT PRIMARY KEY,
                        family TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        approved_at TEXT NOT NULL,
                        new_fingerprint TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS publication_target_audit (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        approval_id TEXT NOT NULL,
                        family TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        target TEXT NOT NULL,
                        record_json TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS publication_audit_by_approval
                    ON publication_target_audit(approval_id, sequence)
                    """
                )
        except (OSError, sqlite3.Error) as error:
            raise PublicationAuditStoreError(
                "publication audit store initialization failed"
            ) from error


def _approval_identity(
    records: tuple[PublicationTargetAuditRecord, ...],
) -> tuple[str, tuple[str, str, str, str]]:
    if not records:
        raise PublicationAuditStoreError("publication audit append cannot be empty")
    first = records[0]
    identity = (
        first.family.value,
        first.actor,
        first.approved_at.astimezone(UTC).isoformat(),
        first.new_fingerprint,
    )
    if any(
        record.approval_id != first.approval_id
        or (
            record.family.value,
            record.actor,
            record.approved_at.astimezone(UTC).isoformat(),
            record.new_fingerprint,
        )
        != identity
        for record in records
    ):
        raise PublicationAuditStoreError(
            "one publication audit append must share an immutable approval identity"
        )
    return first.approval_id, identity
