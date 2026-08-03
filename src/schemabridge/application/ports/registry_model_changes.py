"""Application ports for M35 Phase-B model replacement authoring."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from schemabridge.domain.registry_model_change_authoring import (
    MAX_REGISTRY_MODEL_CHANGE_HISTORY,
    RegistryModelChangeAuditRecord,
    RegistryModelChangeDraft,
    RegistryModelChangeMutation,
    RegistryModelJoinProfileAuditRecord,
    RegistryModelJoinProfileAuthoringRequest,
    RegistryModelJoinProfileMutation,
    RegistryModelReplacementSourceEvidence,
)
from schemabridge.domain.registry_model_changes import (
    PreparedRegistryModelReplacementProposal,
    RegistryModelJoinProfileWitness,
)
from schemabridge.domain.semantic_change import (
    SemanticChangeInspectionContext,
    SemanticChangeReport,
    SemanticImpactSet,
)
from schemabridge.domain.semantic_profile_jobs import (
    SemanticJoinProfileJob,
    SemanticJoinProfileSubmission,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope


class RegistryModelChangePortErrorCode(StrEnum):
    NOT_FOUND = "registry_model_change_not_found"
    CONFLICT = "registry_model_change_conflict"
    INVALID_RESPONSE = "registry_model_change_invalid_response"
    UNAVAILABLE = "registry_model_change_unavailable"


class RegistryModelChangePortError(RuntimeError):
    def __init__(self, code: RegistryModelChangePortErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RegistryModelChangeOperationReplay:
    operation: str
    request_fingerprint: str
    actor_id: str
    draft: RegistryModelChangeDraft
    proposal: PreparedRegistryModelReplacementProposal | None = None


@dataclass(frozen=True, slots=True)
class RegistryModelProfileOperationReplay:
    operation: str
    request_fingerprint: str
    actor_id: str
    authoring: RegistryModelJoinProfileAuthoringRequest
    job: SemanticJoinProfileJob | None = None
    witness: RegistryModelJoinProfileWitness | None = None


class RegistryModelChangeStorePort(Protocol):
    def load_operation_replay(
        self,
        workspace_id: str,
        idempotency_digest: str,
    ) -> RegistryModelChangeOperationReplay | None: ...

    def create(
        self,
        draft: RegistryModelChangeDraft,
        audit: RegistryModelChangeAuditRecord,
        *,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryModelChangeMutation: ...

    def load(self, workspace_id: str, change_id: str) -> RegistryModelChangeDraft | None: ...

    def list_for_workspace(
        self,
        workspace_id: str,
        *,
        owner_actor_id: str | None,
        limit: int,
    ) -> tuple[RegistryModelChangeDraft, ...]: ...

    def commit_decision(
        self,
        draft: RegistryModelChangeDraft,
        audit: RegistryModelChangeAuditRecord,
        *,
        expected_revision: int,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryModelChangeMutation: ...

    def commit_preparation(
        self,
        draft: RegistryModelChangeDraft,
        proposal: PreparedRegistryModelReplacementProposal,
        audit: RegistryModelChangeAuditRecord,
        *,
        expected_revision: int,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryModelChangeMutation: ...

    def list_audit(
        self,
        workspace_id: str,
        change_id: str,
        *,
        limit: int = MAX_REGISTRY_MODEL_CHANGE_HISTORY + 1,
    ) -> tuple[RegistryModelChangeAuditRecord, ...]: ...


class RegistryModelReplacementSourcePort(Protocol):
    def load(
        self,
        workspace_id: str,
        proposal_id: str,
    ) -> RegistryModelReplacementSourceEvidence | None:
        """Load exact persisted M33 proposal, owner, and decision closure."""


class RegistryModelRemediationEvidencePort(Protocol):
    def load_current(
        self,
        scope: SemanticRegistryScope,
        report_id: str,
        expected_context: SemanticChangeInspectionContext,
    ) -> tuple[SemanticChangeReport, SemanticImpactSet] | None:
        """Load only the current blocking report and its full persisted impact set."""


class RegistryModelJoinProfileWitnessPort(Protocol):
    def load(
        self,
        workspace_id: str,
        change_id: str,
        replacement_proposal_id: str,
        join_id: str,
    ) -> RegistryModelJoinProfileWitness | None:
        """Load one worker-recorded aggregate-only candidate profile witness."""


class RegistryModelProfileStorePort(RegistryModelJoinProfileWitnessPort, Protocol):
    def load_operation_replay(
        self,
        workspace_id: str,
        idempotency_digest: str,
    ) -> RegistryModelProfileOperationReplay | None: ...

    def persist_request(
        self,
        authoring: RegistryModelJoinProfileAuthoringRequest,
        audit: RegistryModelJoinProfileAuditRecord,
        *,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryModelJoinProfileMutation: ...

    def bind_job(
        self,
        authoring: RegistryModelJoinProfileAuthoringRequest,
        job: SemanticJoinProfileJob,
        audit: RegistryModelJoinProfileAuditRecord,
        *,
        idempotency_digest: str,
    ) -> RegistryModelJoinProfileMutation: ...

    def load_request(
        self,
        workspace_id: str,
        request_id: str,
    ) -> RegistryModelJoinProfileAuthoringRequest | None: ...

    def record_witness(
        self,
        authoring: RegistryModelJoinProfileAuthoringRequest,
        job: SemanticJoinProfileJob,
        witness: RegistryModelJoinProfileWitness,
        audit: RegistryModelJoinProfileAuditRecord,
        *,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryModelJoinProfileMutation: ...


class RegistryModelProfileQueuePort(Protocol):
    def enqueue(
        self,
        authoring: RegistryModelJoinProfileAuthoringRequest,
        *,
        max_attempts: int = 5,
    ) -> SemanticJoinProfileSubmission: ...

    def load(
        self,
        authoring: RegistryModelJoinProfileAuthoringRequest,
    ) -> SemanticJoinProfileJob | None: ...


__all__ = [
    "RegistryModelChangeOperationReplay",
    "RegistryModelChangePortError",
    "RegistryModelChangePortErrorCode",
    "RegistryModelChangeStorePort",
    "RegistryModelJoinProfileWitnessPort",
    "RegistryModelProfileOperationReplay",
    "RegistryModelProfileQueuePort",
    "RegistryModelProfileStorePort",
    "RegistryModelRemediationEvidencePort",
    "RegistryModelReplacementSourcePort",
]
