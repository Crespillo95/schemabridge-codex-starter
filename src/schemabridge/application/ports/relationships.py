"""Application ports for read-only relationship evidence and governed join persistence."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from schemabridge.domain.decisions import DecisionRecord
from schemabridge.domain.join_reviews import (
    JoinContractPublication,
    JoinPublicationApproval,
    JoinPublicationResult,
    JoinReviewDraft,
    PublishedJoinContext,
)
from schemabridge.domain.joins import JoinProposal, RelationshipProfile


class RelationshipErrorCode(StrEnum):
    NOT_FOUND = "relationship_not_found"
    CONFLICT = "relationship_revision_conflict"
    INVALID_TRANSITION = "relationship_invalid_transition"
    UNSAFE_CARDINALITY = "relationship_unsafe_cardinality"
    NOT_READY = "relationship_not_ready"
    EVIDENCE_UNAVAILABLE = "relationship_evidence_unavailable"
    EVIDENCE_NOT_ALLOWED = "relationship_evidence_not_allowed"
    INVALID_EVIDENCE = "relationship_invalid_evidence"
    APPROVAL_REQUIRED = "join_publication_approval_required"
    APPROVAL_MISMATCH = "join_publication_approval_mismatch"
    STORE_FAILURE = "relationship_store_failure"
    CATALOG_UNAVAILABLE = "join_catalog_unavailable"
    CATALOG_PERMISSION_DENIED = "join_catalog_permission_denied"
    CATALOG_INVALID_RESPONSE = "join_catalog_invalid_response"


class RelationshipWorkflowError(RuntimeError):
    """Sanitized typed relationship failure."""

    def __init__(self, code: RelationshipErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class RelationshipEvidencePort(Protocol):
    """Return aggregate-only evidence for one explicitly allowlisted proposal."""

    def profile(self, proposal: JoinProposal) -> RelationshipProfile:
        """Profile normalized keys without returning or modifying source rows."""


class JoinReviewStorePort(Protocol):
    def create(self, draft: JoinReviewDraft) -> JoinReviewDraft:
        """Create an immutable-identity draft or return the identical existing draft."""

    def load(self, draft_id: str) -> JoinReviewDraft | None:
        """Load the latest local review revision."""

    def commit_decision(
        self,
        draft: JoinReviewDraft,
        decision: DecisionRecord,
        *,
        expected_revision: int,
    ) -> None:
        """Atomically append a decision and advance exactly one revision."""

    def list_decisions(self, draft_id: str) -> tuple[DecisionRecord, ...]:
        """List immutable decisions in version order."""

    def record_publication(self, result: JoinPublicationResult) -> None:
        """Append one external publication attempt."""

    def list_publications(self, draft_id: str) -> tuple[JoinPublicationResult, ...]:
        """List publication attempts in insertion order."""


class JoinContextWritePort(Protocol):
    def publish(
        self,
        publication: JoinContractPublication,
        approval: JoinPublicationApproval,
    ) -> JoinPublicationResult:
        """Publish one exact approved payload; approval is a required argument."""


class JoinContextReadPort(Protocol):
    def load_current(self) -> PublishedJoinContext | None:
        """Load the current DataHub context without relying on local review state."""
