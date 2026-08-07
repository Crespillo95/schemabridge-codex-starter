"""Ports and sanitized adapter failures for governed semantic onboarding."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.semantic_onboarding import (
    MAX_ONBOARDING_DECISIONS,
    OnboardingRegistryBase,
    PreflightSemanticOnboardingRequest,
    PreparedSemanticOnboardingProposal,
    ResolvedOnboardingCatalogEvidence,
    SemanticOnboardingAuditRecord,
    SemanticOnboardingDecision,
    SemanticOnboardingDraft,
    SemanticOnboardingDraftMutation,
    SemanticOnboardingMappingInput,
    SemanticOnboardingPreflight,
    SemanticOnboardingPreparation,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope


class SemanticOnboardingPortErrorCode(StrEnum):
    UNAVAILABLE = "semantic_onboarding_port_unavailable"
    RESOURCE_UNAVAILABLE = "semantic_onboarding_resource_unavailable"
    CONFLICT = "semantic_onboarding_port_conflict"
    INVALID_RESPONSE = "semantic_onboarding_invalid_response"


class SemanticOnboardingPortError(RuntimeError):
    """A bounded adapter failure safe to classify at the application boundary."""

    def __init__(self, code: SemanticOnboardingPortErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SemanticOnboardingOperationReplay:
    operation: str
    request_fingerprint: str
    actor_id: str
    draft: SemanticOnboardingDraft
    proposal: PreparedSemanticOnboardingProposal | None = None


class SemanticOnboardingCatalogEvidencePort(Protocol):
    """Resolve one complete retained catalog selection without source access."""

    def resolve_exact(
        self,
        scope: SemanticRegistryScope,
        connection_id: CatalogConnectionId,
        generation: int,
        expected_generation_fingerprint: str,
        selections: tuple[SemanticOnboardingMappingInput, ...],
    ) -> ResolvedOnboardingCatalogEvidence:
        """Return every exact observation or fail without a partial result."""


class SemanticOnboardingPreflightPort(Protocol):
    """Resolve current catalog and registry authority in one read-only snapshot."""

    def resolve_active(
        self,
        scope: SemanticRegistryScope,
        request: PreflightSemanticOnboardingRequest,
    ) -> SemanticOnboardingPreflight:
        """Return one complete tenant-bound preflight or no result."""


class SemanticOnboardingRegistryBasePort(Protocol):
    """Read the exact active registry base, returning an explicit empty base when absent."""

    def load(self, scope: SemanticRegistryScope) -> OnboardingRegistryBase:
        """Return current immutable base facts for one workspace-bound scope."""


class SemanticOnboardingStorePort(Protocol):
    """Tenant-scoped drafts, append-only decisions, preparations and immutable audit."""

    def load_operation_replay(
        self,
        workspace_id: str,
        idempotency_digest: str,
    ) -> SemanticOnboardingOperationReplay | None:
        """Load the exact prior response bound to an idempotency digest."""

    def create(
        self,
        draft: SemanticOnboardingDraft,
        audit: SemanticOnboardingAuditRecord,
        *,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> SemanticOnboardingDraftMutation:
        """CAS authority and create a draft plus audit/operation atomically."""

    def load(self, workspace_id: str, draft_id: str) -> SemanticOnboardingDraft | None:
        """Load a draft only inside the exact workspace."""

    def list_for_workspace(
        self,
        workspace_id: str,
        *,
        owner_actor_id: str | None,
        limit: int,
    ) -> tuple[SemanticOnboardingDraft, ...]:
        """Return a bounded newest-first workspace page."""

    def commit_decision(
        self,
        draft: SemanticOnboardingDraft,
        decision: SemanticOnboardingDecision,
        audit: SemanticOnboardingAuditRecord,
        *,
        expected_revision: int,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> SemanticOnboardingDraftMutation:
        """CAS authority plus draft and append decision/audit/operation atomically."""

    def commit_preparation(
        self,
        draft: SemanticOnboardingDraft,
        proposal: PreparedSemanticOnboardingProposal,
        audit: SemanticOnboardingAuditRecord,
        *,
        expected_revision: int,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> SemanticOnboardingPreparation:
        """CAS authority plus draft and append proposal/audit/operation atomically."""

    def list_decisions(
        self,
        workspace_id: str,
        draft_id: str,
        *,
        limit: int = MAX_ONBOARDING_DECISIONS + 2,
    ) -> tuple[SemanticOnboardingDecision, ...]:
        """Return at most ``limit`` most-recent decisions, in chronological order."""

    def list_proposals(
        self,
        workspace_id: str,
        draft_id: str,
        *,
        limit: int = MAX_ONBOARDING_DECISIONS + 2,
    ) -> tuple[PreparedSemanticOnboardingProposal, ...]:
        """Return at most ``limit`` most-recent proposals, in chronological order."""

    def list_audit(
        self,
        workspace_id: str,
        draft_id: str,
        *,
        limit: int = MAX_ONBOARDING_DECISIONS + 2,
    ) -> tuple[SemanticOnboardingAuditRecord, ...]:
        """Return at most ``limit`` most-recent audit facts, in chronological order."""


class SemanticOnboardingClockPort(Protocol):
    def now(self) -> datetime:
        """Return one aware trusted application instant."""


__all__ = [
    "SemanticOnboardingCatalogEvidencePort",
    "SemanticOnboardingClockPort",
    "SemanticOnboardingOperationReplay",
    "SemanticOnboardingPortError",
    "SemanticOnboardingPortErrorCode",
    "SemanticOnboardingPreflightPort",
    "SemanticOnboardingRegistryBasePort",
    "SemanticOnboardingStorePort",
]
