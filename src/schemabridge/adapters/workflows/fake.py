"""Explicitly fake, SQLite-backed publication adapter for M12 orchestration demos."""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path

from schemabridge.adapters.storage.sqlite_connection import managed_sqlite_connection
from schemabridge.application.ports.workflows import WorkflowError, WorkflowErrorCode
from schemabridge.domain.publication_audit import (
    PublicationAuditOutcome,
    PublicationFamily,
    PublicationTargetAuditRecord,
)
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
                    if str(existing[0]) != document_ref:
                        return _result(
                            proposal,
                            approval,
                            document_ref,
                            WorkflowPublicationStatus.FAILED,
                            None,
                            datetime.now(UTC),
                            failure_code="fake_store_invalid",
                        )
                    return _result(
                        proposal,
                        approval,
                        str(existing[0]),
                        WorkflowPublicationStatus.ALREADY_CURRENT,
                        proposal.fingerprint,
                        datetime.fromisoformat(str(existing[1])),
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
                return _result(
                    proposal,
                    approval,
                    document_ref,
                    WorkflowPublicationStatus.CREATED,
                    None,
                    published_at,
                )
        except sqlite3.Error:
            return _result(
                proposal,
                approval,
                document_ref,
                WorkflowPublicationStatus.FAILED,
                None,
                datetime.now(UTC),
                failure_code="fake_store_failed",
            )

    def _connect(self) -> AbstractContextManager[sqlite3.Connection]:
        return managed_sqlite_connection(self._path, isolation_level=None)

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


def _result(
    proposal: WorkflowPublicationProposal,
    approval: WorkflowPublicationApproval,
    document_ref: str,
    status: WorkflowPublicationStatus,
    previous_fingerprint: str | None,
    published_at: datetime,
    *,
    failure_code: str | None = None,
) -> WorkflowPublicationResult:
    outcome = {
        WorkflowPublicationStatus.CREATED: PublicationAuditOutcome.SUCCEEDED,
        WorkflowPublicationStatus.ALREADY_CURRENT: PublicationAuditOutcome.ALREADY_CURRENT,
        WorkflowPublicationStatus.FAILED: PublicationAuditOutcome.FAILED,
    }[status]
    return WorkflowPublicationResult(
        status=status,
        approval_id=approval.id,
        proposal_fingerprint=proposal.fingerprint,
        idempotency_key=proposal.idempotency_key,
        document_ref=document_ref,
        published_at=published_at,
        failure_code=failure_code,
        audit_record=PublicationTargetAuditRecord(
            family=PublicationFamily.WORKFLOW,
            operation="upsert_document",
            target=document_ref,
            approval_id=approval.id,
            actor=approval.actor,
            approved_at=approval.approved_at,
            previous_fingerprint=previous_fingerprint,
            new_fingerprint=proposal.fingerprint,
            outcome=outcome,
            reason_code=failure_code,
        ),
    )
