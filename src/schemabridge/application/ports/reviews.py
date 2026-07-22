"""Application ports for canonical review state and approved catalog publication."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from schemabridge.domain.decisions import DecisionRecord
from schemabridge.domain.reviews import (
    CanonicalPublication,
    CanonicalReviewDraft,
    PublicationApproval,
    PublicationResult,
    PublishedCanonicalContext,
)


class ReviewErrorCode(StrEnum):
    NOT_FOUND = "review_not_found"
    CONFLICT = "review_revision_conflict"
    INVALID_TRANSITION = "review_invalid_transition"
    NOT_READY = "review_not_ready"
    APPROVAL_REQUIRED = "publication_approval_required"
    APPROVAL_MISMATCH = "publication_approval_mismatch"
    STORE_FAILURE = "review_store_failure"
    CATALOG_UNAVAILABLE = "catalog_write_unavailable"
    CATALOG_PERMISSION_DENIED = "catalog_write_permission_denied"
    CATALOG_INVALID_RESPONSE = "catalog_write_invalid_response"


class ReviewWorkflowError(RuntimeError):
    """Sanitized typed review or publication failure."""

    def __init__(self, code: ReviewErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class ReviewStorePort(Protocol):
    """Atomic local draft and immutable-decision persistence."""

    def create(self, draft: CanonicalReviewDraft) -> CanonicalReviewDraft:
        """Create one draft, rejecting an incompatible existing identity."""

    def load(self, draft_id: str) -> CanonicalReviewDraft | None:
        """Load current draft state."""

    def commit_decision(
        self,
        draft: CanonicalReviewDraft,
        decision: DecisionRecord,
        *,
        expected_revision: int,
    ) -> None:
        """Atomically append an immutable decision and replace the expected revision."""

    def list_decisions(self, draft_id: str) -> tuple[DecisionRecord, ...]:
        """Return every decision in version order."""

    def record_publication(self, result: PublicationResult) -> None:
        """Append an external publication attempt without rewriting decisions."""

    def list_publications(self, draft_id: str) -> tuple[PublicationResult, ...]:
        """Return recorded attempts in insertion order."""


class CatalogWritePort(Protocol):
    """Mutation boundary whose signature requires a separate approval object."""

    def publish(
        self,
        publication: CanonicalPublication,
        approval: PublicationApproval,
    ) -> PublicationResult:
        """Publish one exact approved payload or return a typed partial result."""


class CanonicalContextReadPort(Protocol):
    """Read back the vendor-neutral evidence created by publication."""

    def read_context(
        self,
        publication: CanonicalPublication,
    ) -> PublishedCanonicalContext | None:
        """Return current published context, or None when the marker is absent."""
