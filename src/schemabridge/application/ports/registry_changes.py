"""Ports and sanitized failures for registry-v2 change authoring."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from schemabridge.domain.registry_change_authoring import (
    MAX_REGISTRY_CHANGE_HISTORY,
    RegistryChangeAuditRecord,
    RegistryJoinDraftMutation,
    RegistryJoinPreparation,
    RegistryJoinProfileAuthoringMutation,
    RegistryJoinProfileAuthoringRequest,
)
from schemabridge.domain.registry_changes import (
    PreparedRegistryJoinProposal,
    RegistryJoinChangeDraft,
)
from schemabridge.domain.semantic_profile_jobs import (
    SemanticJoinProfileJob,
    SemanticJoinProfileSubmission,
)


class RegistryChangeStoreErrorCode(StrEnum):
    NOT_FOUND = "registry_change_not_found"
    CONFLICT = "registry_change_conflict"
    INVALID_RESPONSE = "registry_change_invalid_response"
    UNAVAILABLE = "registry_change_store_unavailable"


class RegistryChangeStoreError(RuntimeError):
    def __init__(self, code: RegistryChangeStoreErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RegistryChangeOperationReplay:
    operation: str
    request_fingerprint: str
    actor_id: str
    authoring: RegistryJoinProfileAuthoringRequest
    job: SemanticJoinProfileJob | None = None
    draft: RegistryJoinChangeDraft | None = None
    proposal: PreparedRegistryJoinProposal | None = None


class RegistryChangeStorePort(Protocol):
    """Tenant-scoped request, draft, proposal, idempotency and audit persistence."""

    def load_operation_replay(
        self,
        workspace_id: str,
        idempotency_digest: str,
    ) -> RegistryChangeOperationReplay | None:
        """Load one exact operation replay within one persisted workspace key."""

    def persist_profile_request(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
        audit: RegistryChangeAuditRecord,
        *,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryJoinProfileAuthoringMutation:
        """Persist request and audit before the queue can be called."""

    def bind_profile_job(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
        job: SemanticJoinProfileJob,
        audit: RegistryChangeAuditRecord,
        *,
        idempotency_digest: str,
    ) -> RegistryJoinProfileAuthoringMutation:
        """Bind the exact queue result to the already persisted request."""

    def load_authoring(
        self,
        workspace_id: str,
        change_id: str,
    ) -> RegistryJoinProfileAuthoringRequest | None:
        """Load an authoring request only within its persisted workspace key."""

    def load_draft(
        self,
        workspace_id: str,
        change_id: str,
    ) -> RegistryJoinChangeDraft | None:
        """Load a finalized draft only within its persisted workspace key."""

    def list_for_workspace(
        self,
        workspace_id: str,
        *,
        owner_actor_id: str | None,
        limit: int,
    ) -> tuple[RegistryJoinProfileAuthoringRequest, ...]:
        """Return a bounded newest-first authoring page."""

    def commit_draft(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
        draft: RegistryJoinChangeDraft,
        audit: RegistryChangeAuditRecord,
        *,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryJoinDraftMutation:
        """Create the draft exactly once after the completed profile is verified."""

    def commit_decision(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
        draft: RegistryJoinChangeDraft,
        audit: RegistryChangeAuditRecord,
        *,
        expected_revision: int,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryJoinDraftMutation:
        """CAS the draft and append its exact terminal decision audit."""

    def commit_preparation(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
        draft: RegistryJoinChangeDraft,
        proposal: PreparedRegistryJoinProposal,
        audit: RegistryChangeAuditRecord,
        *,
        expected_revision: int,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryJoinPreparation:
        """CAS one exact prepared proposal and its immutable audit fact."""

    def list_audit(
        self,
        workspace_id: str,
        change_id: str,
        *,
        limit: int = MAX_REGISTRY_CHANGE_HISTORY + 1,
    ) -> tuple[RegistryChangeAuditRecord, ...]:
        """Return chronological bounded audit facts for one exact change."""


class RegistryJoinProfileRequestQueuePort(Protocol):
    """API-safe profile request boundary for one durable registry change."""

    def enqueue(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
        *,
        max_attempts: int = 5,
    ) -> SemanticJoinProfileSubmission:
        """Enqueue or replay only the exact already-persisted authoring request."""

    def load(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
    ) -> SemanticJoinProfileJob | None:
        """Load only the job bound to the exact durable authoring identity."""


class RegistryChangeClockPort(Protocol):
    def now(self) -> datetime:
        """Return one aware trusted application instant."""


__all__ = [
    "RegistryChangeClockPort",
    "RegistryChangeOperationReplay",
    "RegistryChangeStoreError",
    "RegistryChangeStoreErrorCode",
    "RegistryChangeStorePort",
    "RegistryJoinProfileRequestQueuePort",
]
