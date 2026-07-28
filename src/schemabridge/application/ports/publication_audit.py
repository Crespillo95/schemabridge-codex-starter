"""Port for append-only governed publication audit records."""

from __future__ import annotations

from typing import Protocol

from schemabridge.domain.publication_audit import PublicationTargetAuditRecord


class PublicationAuditStoreError(RuntimeError):
    """The audit record could not be durably appended or read."""


class PublicationAuditStorePort(Protocol):
    def append(self, records: tuple[PublicationTargetAuditRecord, ...]) -> None:
        """Atomically append a non-empty publication attempt."""

    def list_for_approval(self, approval_id: str) -> tuple[PublicationTargetAuditRecord, ...]:
        """Return every target outcome for an approval in append order."""
