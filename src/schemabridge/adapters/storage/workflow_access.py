"""In-memory and SQLite persistence for immutable workflow ownership grants."""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path
from threading import RLock

from pydantic import ValidationError

from schemabridge.adapters.storage.sqlite_connection import managed_sqlite_connection
from schemabridge.application.ports.workflow_access import (
    WorkflowAccessError,
    WorkflowAccessErrorCode,
)
from schemabridge.domain.identity import WorkflowAccessGrant


class InMemoryWorkflowAccessStore:
    """Thread-safe workflow ownership store for tests and local demo composition."""

    def __init__(self) -> None:
        self._grants: dict[str, WorkflowAccessGrant] = {}
        self._lock = RLock()

    def grant(self, grant: WorkflowAccessGrant) -> WorkflowAccessGrant:
        with self._lock:
            current = self._grants.get(grant.workflow_id)
            if current is None:
                self._grants[grant.workflow_id] = grant
                return grant
            if _same_ownership(current, grant):
                return current
            raise _ownership_conflict()

    def load(
        self,
        workspace_id: str,
        workflow_id: str,
        *,
        owner_principal_id: str | None = None,
    ) -> WorkflowAccessGrant | None:
        with self._lock:
            grant = self._grants.get(workflow_id)
            if grant is None or grant.workspace_id != workspace_id:
                return None
            if owner_principal_id is not None and grant.owner_actor_id != owner_principal_id:
                return None
            return grant

    def list_for_workspace(
        self,
        workspace_id: str,
        *,
        owner_principal_id: str | None = None,
        limit: int = 50,
    ) -> tuple[WorkflowAccessGrant, ...]:
        _validate_limit(limit)
        with self._lock:
            grants = (
                grant
                for grant in self._grants.values()
                if grant.workspace_id == workspace_id
                and (owner_principal_id is None or grant.owner_actor_id == owner_principal_id)
            )
            return tuple(
                sorted(
                    grants,
                    key=lambda grant: (-grant.created_at.timestamp(), grant.workflow_id),
                )[:limit]
            )


class SqliteWorkflowAccessStore:
    """Durable workflow ownership store sharing the local application database."""

    def __init__(self, path: Path) -> None:
        self._path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise _store_failure() from error
        self._initialize()

    def grant(self, grant: WorkflowAccessGrant) -> WorkflowAccessGrant:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT workflow_id, workspace_id, owner_actor_id, created_at
                    FROM workflow_access_grants
                    WHERE workflow_id = ?
                    """,
                    (grant.workflow_id,),
                ).fetchone()
                if row is not None:
                    current = _grant_from_row(row)
                    if _same_ownership(current, grant):
                        return current
                    raise _ownership_conflict()
                try:
                    connection.execute(
                        """
                        INSERT INTO workflow_access_grants
                            (workflow_id, workspace_id, owner_actor_id, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            grant.workflow_id,
                            grant.workspace_id,
                            grant.owner_actor_id,
                            grant.created_at.isoformat(),
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    raise _ownership_conflict() from error
                return grant
        except WorkflowAccessError:
            raise
        except (OSError, sqlite3.Error, ValidationError) as error:
            raise _store_failure() from error

    def load(
        self,
        workspace_id: str,
        workflow_id: str,
        *,
        owner_principal_id: str | None = None,
    ) -> WorkflowAccessGrant | None:
        try:
            with self._connect() as connection:
                if owner_principal_id is None:
                    row = connection.execute(
                        """
                        SELECT workflow_id, workspace_id, owner_actor_id, created_at
                        FROM workflow_access_grants
                        WHERE workspace_id = ? AND workflow_id = ?
                        """,
                        (workspace_id, workflow_id),
                    ).fetchone()
                else:
                    row = connection.execute(
                        """
                        SELECT workflow_id, workspace_id, owner_actor_id, created_at
                        FROM workflow_access_grants
                        WHERE workspace_id = ? AND workflow_id = ? AND owner_actor_id = ?
                        """,
                        (workspace_id, workflow_id, owner_principal_id),
                    ).fetchone()
            return None if row is None else _grant_from_row(row)
        except (OSError, sqlite3.Error, ValidationError) as error:
            raise _store_failure() from error

    def list_for_workspace(
        self,
        workspace_id: str,
        *,
        owner_principal_id: str | None = None,
        limit: int = 50,
    ) -> tuple[WorkflowAccessGrant, ...]:
        _validate_limit(limit)
        try:
            with self._connect() as connection:
                if owner_principal_id is None:
                    rows = connection.execute(
                        """
                        SELECT workflow_id, workspace_id, owner_actor_id, created_at
                        FROM workflow_access_grants
                        WHERE workspace_id = ?
                        ORDER BY created_at DESC, workflow_id ASC
                        LIMIT ?
                        """,
                        (workspace_id, limit),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """
                        SELECT workflow_id, workspace_id, owner_actor_id, created_at
                        FROM workflow_access_grants
                        WHERE workspace_id = ? AND owner_actor_id = ?
                        ORDER BY created_at DESC, workflow_id ASC
                        LIMIT ?
                        """,
                        (workspace_id, owner_principal_id, limit),
                    ).fetchall()
            return tuple(_grant_from_row(row) for row in rows)
        except (OSError, sqlite3.Error, ValidationError) as error:
            raise _store_failure() from error

    def _connect(self) -> AbstractContextManager[sqlite3.Connection]:
        return managed_sqlite_connection(self._path, isolation_level=None)

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.executescript(
                    """
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


def _grant_from_row(row: tuple[object, ...]) -> WorkflowAccessGrant:
    return WorkflowAccessGrant.model_validate(
        {
            "workflow_id": row[0],
            "workspace_id": row[1],
            "owner_actor_id": row[2],
            "created_at": row[3],
        }
    )


def _same_ownership(left: WorkflowAccessGrant, right: WorkflowAccessGrant) -> bool:
    return (
        left.workflow_id == right.workflow_id
        and left.workspace_id == right.workspace_id
        and left.owner_actor_id == right.owner_actor_id
    )


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("workflow access list limit must be between 1 and 100")


def _ownership_conflict() -> WorkflowAccessError:
    return WorkflowAccessError(
        WorkflowAccessErrorCode.CONFLICT,
        "workflow ownership already exists with a different binding",
    )


def _store_failure() -> WorkflowAccessError:
    return WorkflowAccessError(
        WorkflowAccessErrorCode.STORE_FAILURE,
        "local workflow access store failed",
    )
