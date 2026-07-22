"""Explicitly fake, SQLite-backed publication adapter for M12 orchestration demos."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from schemabridge.application.ports.workflows import WorkflowError, WorkflowErrorCode
from schemabridge.domain.workflows import (
    WorkflowPublicationApproval,
    WorkflowPublicationProposal,
    WorkflowPublicationResult,
    WorkflowPublicationStatus,
)


class SqliteFakeWorkflowPublisher:
    """Prove idempotent orchestration without performing a DataHub mutation in M12."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def publish(
        self,
        proposal: WorkflowPublicationProposal,
        approval: WorkflowPublicationApproval,
    ) -> WorkflowPublicationResult:
        if (
            approval.workflow_id != proposal.workflow_id
            or approval.proposal_fingerprint != proposal.fingerprint
            or approval.idempotency_key != proposal.idempotency_key
        ):
            raise WorkflowError(
                WorkflowErrorCode.PUBLICATION_FAILED,
                "fake publication approval does not match the proposal",
            )
        document_ref = f"fake://workflow-context/{proposal.idempotency_key}"
        try:
            with self._connect() as connection:
                existing = connection.execute(
                    """
                    SELECT document_ref, published_at FROM fake_workflow_publications
                    WHERE idempotency_key = ?
                    """,
                    (proposal.idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return WorkflowPublicationResult(
                        status=WorkflowPublicationStatus.ALREADY_CURRENT,
                        idempotency_key=proposal.idempotency_key,
                        document_ref=str(existing[0]),
                        published_at=datetime.fromisoformat(str(existing[1])),
                    )
                published_at = datetime.now(UTC)
                connection.execute(
                    """
                    INSERT INTO fake_workflow_publications
                        (idempotency_key, document_ref, published_at)
                    VALUES (?, ?, ?)
                    """,
                    (proposal.idempotency_key, document_ref, published_at.isoformat()),
                )
                return WorkflowPublicationResult(
                    status=WorkflowPublicationStatus.CREATED,
                    idempotency_key=proposal.idempotency_key,
                    document_ref=document_ref,
                    published_at=published_at,
                )
        except sqlite3.Error as error:
            raise WorkflowError(
                WorkflowErrorCode.PUBLICATION_FAILED,
                "fake workflow publication store failed",
            ) from error

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, isolation_level=None, timeout=5.0)

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS fake_workflow_publications (
                        idempotency_key TEXT PRIMARY KEY,
                        document_ref TEXT NOT NULL,
                        published_at TEXT NOT NULL
                    )
                    """
                )
        except sqlite3.Error as error:
            raise WorkflowError(
                WorkflowErrorCode.PUBLICATION_FAILED,
                "fake workflow publication store failed",
            ) from error
