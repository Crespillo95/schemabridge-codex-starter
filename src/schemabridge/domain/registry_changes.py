"""Pure M35 contracts for immutable registry-v2 change proposals."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.decisions import (
    ApprovalStatus,
    DecisionAction,
    DecisionRecord,
    DecisionTargetType,
)
from schemabridge.domain.joins import (
    Cardinality,
    FanoutPolicy,
    JoinCandidate,
    JoinContract,
    JoinRecommendation,
    NormalizedJoinKey,
    score_join_candidate,
)
from schemabridge.domain.registry_publication import (
    PublishableRegistryVersion,
    create_publishable_registry_version,
)
from schemabridge.domain.request_context import ApprovedLogicalContext, ApprovedLogicalJoin
from schemabridge.domain.semantic_onboarding import OnboardingRegistryBase
from schemabridge.domain.semantic_profile_jobs import (
    SemanticJoinProfileJob,
    SemanticJoinProfileJobStatus,
    SemanticJoinProfileProposal,
    SemanticJoinProfileResult,
    SemanticJoinProfileTargetRef,
)
from schemabridge.domain.semantic_registry import (
    GovernedFieldMapping,
    GovernedJoinRegistry,
    GovernedMappingRegistry,
    GovernedPhysicalBinding,
    GovernedSemanticRegistrySnapshot,
    RegistryArtifactKind,
    RegistryArtifactProvenance,
    SemanticRegistryScope,
    datahub_registry_document_id,
    prepare_datahub_registry_version,
    semantic_registry_decision_ids,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[a-z][a-z0-9_-]{2,199}$")
_SCAN_ID = re.compile(r"^scan_[0-9a-f]{64}$")
_PROFILE_JOB_ID = re.compile(r"^profile_job_[0-9a-f]{64}$")
_MAX_PROFILE_AGE = timedelta(hours=1)
_MIN_PROFILE_TIMEOUT_MS = 100
_MAX_PROFILE_TIMEOUT_MS = 60_000
_MAX_DRAFT_BYTES = 512 * 1024
_MAX_PROPOSAL_BYTES = 512 * 1024


class RegistryChangeProposalKind(StrEnum):
    """Closed proposal families accepted by the M35 publication dispatcher."""

    ADD_JOIN_V1 = "add_join_v1"
    REPLACE_MODEL_V1 = "replace_model_v1"


class RegistryJoinChangeStatus(StrEnum):
    NEEDS_REVIEW = "needs_review"
    READY_FOR_PUBLICATION = "ready_for_publication"
    SUPERSEDED = "superseded"


class RegistryJoinEndpointEvidence(FrozenDomainModel):
    """Exact active mapping and v2 binding used by one proposed join key."""

    key: NormalizedJoinKey
    mapping: GovernedFieldMapping
    binding: GovernedPhysicalBinding
    fingerprint: str

    @field_validator("fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "join endpoint fingerprint")

    @model_validator(mode="after")
    def endpoint_must_match_active_mapping(self) -> RegistryJoinEndpointEvidence:
        mapped = self.mapping.mapping
        if (
            self.key.logical_field != mapped.logical_field
            or self.key.physical_field != mapped.physical_field
            or self.key.transformation_plan != mapped.transformation_plan
            or self.binding.logical_field != mapped.logical_field
            or self.binding.physical_field != mapped.physical_field
            or self.binding.physical_type is not self.mapping.physical_type
            or self.fingerprint != registry_change_fingerprint(_endpoint_payload(self))
        ):
            raise ValueError("join endpoint does not match its exact active mapping authority")
        return self

    @classmethod
    def create(
        cls,
        *,
        key: NormalizedJoinKey,
        mapping: GovernedFieldMapping,
        binding: GovernedPhysicalBinding,
    ) -> RegistryJoinEndpointEvidence:
        construct: Any = cls.model_construct
        provisional = construct(
            key=key,
            mapping=mapping,
            binding=binding,
            fingerprint="0" * 64,
        )
        return cls(
            key=key,
            mapping=mapping,
            binding=binding,
            fingerprint=registry_change_fingerprint(_endpoint_payload(provisional)),
        )


class RegistryJoinBaseEvidence(FrozenDomainModel):
    """Compact exact base slice sufficient to bind a new relationship review."""

    base_registry: OnboardingRegistryBase
    left: RegistryJoinEndpointEvidence
    right: RegistryJoinEndpointEvidence
    fingerprint: str

    @field_validator("fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "join base evidence fingerprint")

    @model_validator(mode="after")
    def base_evidence_must_be_same_connection_and_complete(self) -> RegistryJoinBaseEvidence:
        if (
            self.base_registry.registry_version is None
            or self.left.binding.workspace_id != self.right.binding.workspace_id
            or self.left.binding.connection_id != self.right.binding.connection_id
            or self.left.binding.catalog_scope != self.right.binding.catalog_scope
            or self.left.key.logical_field.root.split(".", 1)[0]
            == self.right.key.logical_field.root.split(".", 1)[0]
            or self.fingerprint != registry_change_fingerprint(_base_evidence_payload(self))
        ):
            raise ValueError("join base evidence is incomplete or cross-connection")
        return self

    @classmethod
    def create(
        cls,
        *,
        base_registry: OnboardingRegistryBase,
        left: RegistryJoinEndpointEvidence,
        right: RegistryJoinEndpointEvidence,
    ) -> RegistryJoinBaseEvidence:
        construct: Any = cls.model_construct
        provisional = construct(
            base_registry=base_registry,
            left=left,
            right=right,
            fingerprint="0" * 64,
        )
        return cls(
            base_registry=base_registry,
            left=left,
            right=right,
            fingerprint=registry_change_fingerprint(_base_evidence_payload(provisional)),
        )


def resolve_registry_join_base_evidence(
    *,
    scope: SemanticRegistryScope,
    base: GovernedSemanticRegistrySnapshot,
    base_registry: OnboardingRegistryBase,
    proposal: SemanticJoinProfileProposal,
) -> RegistryJoinBaseEvidence:
    """Resolve exact active v2 mappings before any relationship source I/O."""

    expected_source = f"datahub:{datahub_registry_document_id(scope, base.version)}"
    if (
        base.format_version != 2
        or base.registry_id != scope.registry_id
        or base.catalog_scope != scope.catalog_scope
        or base_registry.registry_version != base.version
        or base_registry.registry_fingerprint != base.fingerprint
        or base.source != expected_source
        or base.logical_context.source != f"{expected_source}/logical-context"
        or any(item.workspace_id != scope.workspace_id for item in base.physical_bindings)
    ):
        raise ValueError("registry join change requires the exact active registry-v2 base")
    left = _endpoint_from_base(base, proposal.proposal.left_key)
    right = _endpoint_from_base(base, proposal.proposal.right_key)
    if (
        left.binding.connection_id != proposal.connection_id
        or right.binding.connection_id != proposal.connection_id
    ):
        raise ValueError("registry join proposal targets another governed connection")
    return RegistryJoinBaseEvidence.create(
        base_registry=base_registry,
        left=left,
        right=right,
    )


def registry_join_profile_scan_id_v1(
    *,
    scope: SemanticRegistryScope,
    base_evidence: RegistryJoinBaseEvidence,
    proposal: SemanticJoinProfileProposal,
    execution_target: SemanticJoinProfileTargetRef,
    requested_at: datetime,
) -> str:
    """Derive one deterministic attempt identity from the complete pre-I/O authority."""

    requested_at = _aware(requested_at, "registry join profile request timestamp")
    if (
        base_evidence.left.binding.workspace_id != scope.workspace_id
        or base_evidence.left.binding.catalog_scope != scope.catalog_scope
        or base_evidence.left.binding.connection_id != proposal.connection_id
        or base_evidence.right.binding.connection_id != proposal.connection_id
        or proposal.proposal.left_key != base_evidence.left.key
        or proposal.proposal.right_key != base_evidence.right.key
        or execution_target.workspace_id != scope.workspace_id
        or execution_target.connection_id != proposal.connection_id
    ):
        raise ValueError("registry join profile request authority is inconsistent")
    digest = registry_change_fingerprint(
        {
            "contract": "registry_join_profile_scan_v1",
            "scope": scope,
            "base_evidence_fingerprint": base_evidence.fingerprint,
            "proposal": proposal,
            "execution_target": execution_target,
            "requested_at": requested_at,
        }
    )
    return f"scan_{digest}"


class RegistryJoinProfileRequest(FrozenDomainModel):
    """Fingerprintable pre-I/O authority used to request one profiling job."""

    scope: SemanticRegistryScope
    base_evidence: RegistryJoinBaseEvidence
    proposal: SemanticJoinProfileProposal
    execution_target: SemanticJoinProfileTargetRef
    scan_id: str
    requested_at: datetime
    external_writes_performed: Literal[False] = False
    fingerprint: str

    @field_validator("scan_id")
    @classmethod
    def scan_id_must_be_canonical(cls, value: str) -> str:
        if _SCAN_ID.fullmatch(value) is None:
            raise ValueError("registry join profile request scan id is invalid")
        return value

    @field_validator("requested_at")
    @classmethod
    def requested_at_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "registry join profile request timestamp")

    @field_validator("fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "registry join profile request fingerprint")

    @model_validator(mode="after")
    def request_must_be_exact_and_fingerprinted(self) -> RegistryJoinProfileRequest:
        if self.scan_id != registry_join_profile_scan_id_v1(
            scope=self.scope,
            base_evidence=self.base_evidence,
            proposal=self.proposal,
            execution_target=self.execution_target,
            requested_at=self.requested_at,
        ) or self.fingerprint != registry_change_fingerprint(_profile_request_payload(self)):
            raise ValueError("registry join profile request authority does not match")
        return self

    @classmethod
    def create(
        cls,
        *,
        scope: SemanticRegistryScope,
        base_evidence: RegistryJoinBaseEvidence,
        proposal: SemanticJoinProfileProposal,
        execution_target: SemanticJoinProfileTargetRef,
        requested_at: datetime,
    ) -> RegistryJoinProfileRequest:
        scan_id = registry_join_profile_scan_id_v1(
            scope=scope,
            base_evidence=base_evidence,
            proposal=proposal,
            execution_target=execution_target,
            requested_at=requested_at,
        )
        payload = {
            "scope": scope,
            "base_evidence": base_evidence,
            "proposal": proposal,
            "execution_target": execution_target,
            "scan_id": scan_id,
            "requested_at": requested_at,
            "external_writes_performed": False,
        }
        construct: Any = cls.model_construct
        provisional = construct(**payload, fingerprint="0" * 64)
        return cls(
            **payload,
            fingerprint=registry_change_fingerprint(_profile_request_payload(provisional)),
        )


class RegistryJoinProfileCampaign(FrozenDomainModel):
    """Exact aggregate-only profiling authority for one join review."""

    request: RegistryJoinProfileRequest
    request_fingerprint: str
    workspace_id: str = Field(min_length=3, max_length=200)
    scope: SemanticRegistryScope
    base_registry: OnboardingRegistryBase
    base_evidence_fingerprint: str
    catalog_generation_vector: tuple[tuple[int, str], ...] = Field(
        min_length=1,
        max_length=2,
    )
    connection_id: CatalogConnectionId
    scan_id: str
    proposal: SemanticJoinProfileProposal
    proposal_fingerprint: str
    execution_target: SemanticJoinProfileTargetRef
    connector_contract_version: int = Field(ge=1)
    profile_job_id: str
    profile_result_fingerprint: str
    reader_user: str = Field(min_length=1, max_length=200)
    transaction_read_only: Literal[True]
    statement_timeout_ms: int = Field(
        ge=_MIN_PROFILE_TIMEOUT_MS,
        le=_MAX_PROFILE_TIMEOUT_MS,
    )
    completed_at: datetime
    expires_at: datetime
    fingerprint: str

    @field_validator("workspace_id", "reader_user")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("registry join profile campaign text must not be blank")
        return value

    @field_validator("scan_id")
    @classmethod
    def scan_id_must_be_canonical(cls, value: str) -> str:
        if _SCAN_ID.fullmatch(value) is None:
            raise ValueError("registry join profile campaign scan id is invalid")
        return value

    @field_validator("profile_job_id")
    @classmethod
    def profile_job_id_must_be_canonical(cls, value: str) -> str:
        if _PROFILE_JOB_ID.fullmatch(value) is None:
            raise ValueError("registry join profile campaign job id is invalid")
        return value

    @field_validator(
        "base_evidence_fingerprint",
        "request_fingerprint",
        "proposal_fingerprint",
        "profile_result_fingerprint",
        "fingerprint",
    )
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "registry join profile campaign fingerprint")

    @field_validator("completed_at", "expires_at")
    @classmethod
    def times_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "registry join profile campaign timestamp")

    @model_validator(mode="after")
    def campaign_must_be_complete_and_fingerprinted(
        self,
    ) -> RegistryJoinProfileCampaign:
        if any(
            generation < 1 or _SHA256.fullmatch(fingerprint) is None
            for generation, fingerprint in self.catalog_generation_vector
        ):
            raise ValueError("registry join profile catalog vector is invalid")
        if tuple(sorted(set(self.catalog_generation_vector))) != (self.catalog_generation_vector):
            raise ValueError("registry join profile catalog vector must be canonical")
        if (
            self.request_fingerprint != self.request.fingerprint
            or self.request.scope != self.scope
            or self.request.base_evidence.fingerprint != self.base_evidence_fingerprint
            or self.request.proposal != self.proposal
            or self.request.execution_target != self.execution_target
            or self.request.scan_id != self.scan_id
            or self.scope.workspace_id != self.workspace_id
            or self.base_registry.registry_version is None
            or self.proposal.connection_id != self.connection_id
            or self.execution_target.workspace_id != self.workspace_id
            or self.execution_target.connection_id != self.connection_id
            or self.expires_at != self.completed_at + _MAX_PROFILE_AGE
            or self.fingerprint != registry_change_fingerprint(_profile_campaign_payload(self))
        ):
            raise ValueError("registry join profile campaign authority is incomplete")
        return self

    @classmethod
    def create(
        cls,
        *,
        request: RegistryJoinProfileRequest,
        profile_job: SemanticJoinProfileJob,
    ) -> RegistryJoinProfileCampaign:
        result, target, contract_version = _validated_profile_job_evidence(profile_job)
        profile = result.profile
        assert profile.reader_user is not None
        assert profile.statement_timeout_ms is not None
        payload = {
            "request": request,
            "request_fingerprint": request.fingerprint,
            "workspace_id": profile_job.workspace_id,
            "scope": request.scope,
            "base_registry": request.base_evidence.base_registry,
            "base_evidence_fingerprint": request.base_evidence.fingerprint,
            "catalog_generation_vector": _catalog_generation_vector(request.base_evidence),
            "connection_id": profile_job.connection_id,
            "scan_id": profile_job.scan_id,
            "proposal": profile_job.bound_proposal,
            "proposal_fingerprint": profile_job.proposal_fingerprint,
            "execution_target": target,
            "connector_contract_version": contract_version,
            "profile_job_id": profile_job.job_id,
            "profile_result_fingerprint": result.fingerprint,
            "reader_user": profile.reader_user,
            "transaction_read_only": True,
            "statement_timeout_ms": profile.statement_timeout_ms,
            "completed_at": result.completed_at,
            "expires_at": result.completed_at + _MAX_PROFILE_AGE,
        }
        construct: Any = cls.model_construct
        provisional = construct(**payload, fingerprint="0" * 64)
        campaign = cls(
            **payload,
            fingerprint=registry_change_fingerprint(_profile_campaign_payload(provisional)),
        )
        validate_registry_join_profile_campaign(
            campaign,
            scope=request.scope,
            base_evidence=request.base_evidence,
            profile_job=profile_job,
            at=result.completed_at,
        )
        return campaign


class RegistryJoinChangeDraft(FrozenDomainModel):
    """Tenant-bound review aggregate for exactly one new join contract."""

    id: str = Field(min_length=3, max_length=80)
    workspace_id: str = Field(min_length=3, max_length=200)
    owner_actor_id: str = Field(min_length=1, max_length=200)
    scope: SemanticRegistryScope
    base_evidence: RegistryJoinBaseEvidence
    profile_campaign: RegistryJoinProfileCampaign
    profile_job: SemanticJoinProfileJob
    candidate: JoinCandidate
    revision: int = Field(default=1, ge=1)
    status: RegistryJoinChangeStatus = RegistryJoinChangeStatus.NEEDS_REVIEW
    decision: DecisionRecord | None = None
    contract: JoinContract | None = None
    prepared_proposal_id: str | None = Field(default=None, min_length=3, max_length=200)
    prepared_proposal_fingerprint: str | None = None
    created_at: datetime
    updated_at: datetime
    fingerprint: str

    @field_validator("id")
    @classmethod
    def identifier_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("registry join draft identifier is invalid")
        return value

    @field_validator("workspace_id", "owner_actor_id")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("registry join draft text must not be blank")
        return value

    @field_validator("prepared_proposal_fingerprint", "fingerprint")
    @classmethod
    def optional_fingerprints_must_be_sha256(cls, value: str | None) -> str | None:
        if value is not None:
            return _sha256(value, "registry join draft fingerprint")
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def times_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "registry join draft timestamp")

    @model_validator(mode="after")
    def draft_must_be_complete_and_fingerprinted(self) -> RegistryJoinChangeDraft:
        profile_result = self.profile_job.result
        if (
            self.scope.workspace_id != self.workspace_id
            or self.base_evidence.left.binding.workspace_id != self.workspace_id
            or self.base_evidence.left.binding.catalog_scope != self.scope.catalog_scope
            or self.updated_at < self.created_at
            or self.profile_job.workspace_id != self.workspace_id
            or self.profile_job.status is not SemanticJoinProfileJobStatus.COMPLETED
            or profile_result is None
            or self.profile_job.bound_proposal.proposal != self.candidate.proposal
            or self.candidate
            != score_join_candidate(self.candidate.proposal, profile_result.profile)
            or self.candidate.proposal.left_key != self.base_evidence.left.key
            or self.candidate.proposal.right_key != self.base_evidence.right.key
        ):
            raise ValueError("registry join draft does not match its base or profile evidence")
        validate_registry_join_profile_campaign(
            self.profile_campaign,
            scope=self.scope,
            base_evidence=self.base_evidence,
            profile_job=self.profile_job,
            at=self.created_at,
        )
        approved = self.decision is not None and self.decision.action is DecisionAction.APPROVE
        rejected = self.decision is not None and self.decision.action is DecisionAction.REJECT
        if approved != (self.contract is not None):
            raise ValueError("approved registry join decision must match one contract")
        if rejected and self.contract is not None:
            raise ValueError("rejected registry join decision cannot carry a contract")
        if self.decision is not None and (
            self.decision.target_type is not DecisionTargetType.JOIN_CONTRACT
            or self.decision.target_id != self.candidate.proposal.id
            or self.decision.source_version + 1 != self.decision.resulting_version
            or self.decision.resulting_version != self.revision
        ):
            raise ValueError("registry join decision identifies another draft revision")
        if self.contract is not None and (
            self.contract.id != self.candidate.proposal.id
            or self.contract.approval_decision_id != self.decision.id  # type: ignore[union-attr]
        ):
            raise ValueError("registry join contract differs from its exact decision")
        prepared = self.prepared_proposal_id is not None
        if prepared != (self.prepared_proposal_fingerprint is not None):
            raise ValueError("registry join prepared proposal identity is incomplete")
        if (
            self.status
            in {
                RegistryJoinChangeStatus.READY_FOR_PUBLICATION,
                RegistryJoinChangeStatus.SUPERSEDED,
            }
        ) != prepared:
            raise ValueError("registry join lifecycle does not match its prepared proposal")
        if self.fingerprint != registry_change_fingerprint(_draft_payload(self)):
            raise ValueError("registry join draft fingerprint does not match")
        if len(_canonical_json(self.model_dump(mode="json"))) > _MAX_DRAFT_BYTES:
            raise ValueError("registry join draft exceeds its byte limit")
        return self

    @classmethod
    def create(
        cls,
        *,
        id: str,
        workspace_id: str,
        owner_actor_id: str,
        scope: SemanticRegistryScope,
        base: GovernedSemanticRegistrySnapshot,
        base_registry: OnboardingRegistryBase,
        profile_job: SemanticJoinProfileJob,
        created_at: datetime,
    ) -> RegistryJoinChangeDraft:
        result, target, _ = _validated_profile_job_evidence(profile_job)
        if scope.workspace_id != workspace_id or profile_job.workspace_id != workspace_id:
            raise ValueError("registry join draft requires one completed profile job")
        bound_proposal = profile_job.bound_proposal
        proposal = bound_proposal.proposal
        evidence = resolve_registry_join_base_evidence(
            scope=scope,
            base=base,
            base_registry=base_registry,
            proposal=bound_proposal,
        )
        request = RegistryJoinProfileRequest.create(
            scope=scope,
            base_evidence=evidence,
            proposal=bound_proposal,
            execution_target=target,
            requested_at=profile_job.requested_at,
        )
        campaign = RegistryJoinProfileCampaign.create(
            request=request,
            profile_job=profile_job,
        )
        candidate = score_join_candidate(proposal, result.profile)
        payload = {
            "id": id,
            "workspace_id": workspace_id,
            "owner_actor_id": owner_actor_id,
            "scope": scope,
            "base_evidence": evidence,
            "profile_campaign": campaign,
            "profile_job": profile_job,
            "candidate": candidate,
            "revision": 1,
            "status": RegistryJoinChangeStatus.NEEDS_REVIEW,
            "decision": None,
            "contract": None,
            "prepared_proposal_id": None,
            "prepared_proposal_fingerprint": None,
            "created_at": created_at,
            "updated_at": created_at,
        }
        return _build_draft(payload)


class PreparedRegistryJoinProposal(FrozenDomainModel):
    """Immutable, non-executable M35 join delta consumed by the M34 worker."""

    proposal_kind: Literal["add_join_v1"] = "add_join_v1"
    id: str = Field(min_length=3, max_length=200)
    workspace_id: str = Field(min_length=3, max_length=200)
    owner_actor_id: str = Field(min_length=1, max_length=200)
    scope: SemanticRegistryScope
    draft_id: str = Field(min_length=3, max_length=80)
    draft_revision: int = Field(ge=2)
    draft_fingerprint: str
    target_registry_version: int = Field(ge=2)
    base_registry: OnboardingRegistryBase
    base_evidence: RegistryJoinBaseEvidence
    profile_campaign: RegistryJoinProfileCampaign
    profile_job: SemanticJoinProfileJob
    profile_expires_at: datetime
    decision: DecisionRecord
    contract: JoinContract
    decision_ids: tuple[str, ...] = Field(min_length=1, max_length=1)
    prepared_by: str = Field(min_length=1, max_length=200)
    prepared_at: datetime
    external_writes_performed: Literal[False] = False
    fingerprint: str

    @field_validator("id", "workspace_id", "owner_actor_id", "prepared_by")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prepared registry join proposal text must not be blank")
        return value

    @field_validator("draft_fingerprint", "fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "prepared registry join proposal fingerprint")

    @field_validator("profile_expires_at", "prepared_at")
    @classmethod
    def times_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "prepared registry join proposal timestamp")

    @model_validator(mode="after")
    def proposal_must_be_exact_and_fingerprinted(self) -> PreparedRegistryJoinProposal:
        result = self.profile_job.result
        if (
            self.scope.workspace_id != self.workspace_id
            or self.target_registry_version != self.base_registry.next_registry_version
            or self.base_registry != self.base_evidence.base_registry
            or self.profile_job.status is not SemanticJoinProfileJobStatus.COMPLETED
            or result is None
            or self.profile_job.workspace_id != self.workspace_id
            or self.profile_job.proposal.left_key != self.base_evidence.left.key
            or self.profile_job.proposal.right_key != self.base_evidence.right.key
            or self.profile_campaign.base_registry != self.base_registry
            or self.profile_campaign.expires_at != self.profile_expires_at
            or self.decision.action is not DecisionAction.APPROVE
            or self.decision.status is not ApprovalStatus.APPROVED
            or self.contract.approval_decision_id != self.decision.id
            or self.contract.id != self.profile_job.proposal.id
            or self.decision_ids != (self.decision.id,)
            or self.prepared_by in {self.owner_actor_id, self.decision.actor}
            or not self.prepared_at < self.profile_expires_at
            or self.profile_expires_at != result.completed_at + _MAX_PROFILE_AGE
        ):
            raise ValueError("prepared registry join proposal is incomplete or stale")
        validate_registry_join_profile_campaign(
            self.profile_campaign,
            scope=self.scope,
            base_evidence=self.base_evidence,
            profile_job=self.profile_job,
            at=self.prepared_at,
        )
        if self.fingerprint != registry_change_fingerprint(
            self.model_dump(mode="json", exclude={"fingerprint"})
        ):
            raise ValueError("prepared registry join proposal fingerprint does not match")
        if len(_canonical_json(self.model_dump(mode="json"))) > _MAX_PROPOSAL_BYTES:
            raise ValueError("prepared registry join proposal exceeds its byte limit")
        return self

    def is_current(self, at: datetime) -> bool:
        instant = _aware(at, "registry join proposal current time")
        if instant < self.prepared_at:
            return False
        try:
            validate_registry_join_profile_campaign(
                self.profile_campaign,
                scope=self.scope,
                base_evidence=self.base_evidence,
                profile_job=self.profile_job,
                at=instant,
            )
        except ValueError:
            return False
        return True

    @classmethod
    def create(
        cls,
        *,
        id: str,
        draft: RegistryJoinChangeDraft,
        prepared_by: str,
        prepared_at: datetime,
    ) -> PreparedRegistryJoinProposal:
        if draft.decision is None or draft.contract is None:
            raise ValueError("registry join draft requires an approved decision")
        if prepared_by in {draft.owner_actor_id, draft.decision.actor}:
            raise ValueError("registry join preparation requires a distinct publisher")
        result = draft.profile_job.result
        if result is None:
            raise ValueError("registry join profile result is unavailable")
        payload = {
            "proposal_kind": RegistryChangeProposalKind.ADD_JOIN_V1.value,
            "id": id,
            "workspace_id": draft.workspace_id,
            "owner_actor_id": draft.owner_actor_id,
            "scope": draft.scope,
            "draft_id": draft.id,
            "draft_revision": draft.revision,
            "draft_fingerprint": draft.fingerprint,
            "target_registry_version": draft.base_evidence.base_registry.next_registry_version,
            "base_registry": draft.base_evidence.base_registry,
            "base_evidence": draft.base_evidence,
            "profile_campaign": draft.profile_campaign,
            "profile_job": draft.profile_job,
            "profile_expires_at": result.completed_at + _MAX_PROFILE_AGE,
            "decision": draft.decision,
            "contract": draft.contract,
            "decision_ids": (draft.decision.id,),
            "prepared_by": prepared_by,
            "prepared_at": prepared_at,
            "external_writes_performed": False,
        }
        construct: Any = cls.model_construct
        provisional = construct(**payload, fingerprint="0" * 64)
        return cls(
            **payload,
            fingerprint=registry_change_fingerprint(
                provisional.model_dump(mode="json", exclude={"fingerprint"})
            ),
        )


def validate_registry_join_profile_campaign(
    campaign: RegistryJoinProfileCampaign,
    *,
    scope: SemanticRegistryScope,
    base_evidence: RegistryJoinBaseEvidence,
    profile_job: SemanticJoinProfileJob,
    at: datetime,
) -> RegistryJoinProfileCampaign:
    """Revalidate the exact profile job authority and its bounded freshness window."""

    instant = _aware(at, "registry join profile campaign validation time")
    result, target, contract_version = _validated_profile_job_evidence(profile_job)
    profile = result.profile
    if (
        campaign.request.scope != scope
        or campaign.request.base_evidence != base_evidence
        or campaign.request.proposal != profile_job.bound_proposal
        or campaign.request.execution_target != target
        or campaign.request.scan_id != profile_job.scan_id
        or campaign.request.requested_at != profile_job.requested_at
        or campaign.request_fingerprint != campaign.request.fingerprint
        or campaign.scope != scope
        or campaign.workspace_id != scope.workspace_id
        or campaign.base_registry != base_evidence.base_registry
        or campaign.base_evidence_fingerprint != base_evidence.fingerprint
        or campaign.catalog_generation_vector != _catalog_generation_vector(base_evidence)
        or campaign.connection_id != base_evidence.left.binding.connection_id
        or campaign.connection_id != base_evidence.right.binding.connection_id
        or campaign.connection_id != profile_job.connection_id
        or campaign.scan_id != profile_job.scan_id
        or campaign.scan_id
        != registry_join_profile_scan_id_v1(
            scope=scope,
            base_evidence=base_evidence,
            proposal=profile_job.bound_proposal,
            execution_target=target,
            requested_at=profile_job.requested_at,
        )
        or campaign.proposal != profile_job.bound_proposal
        or campaign.proposal_fingerprint != profile_job.proposal_fingerprint
        or campaign.execution_target != target
        or campaign.connector_contract_version != contract_version
        or campaign.profile_job_id != profile_job.job_id
        or campaign.profile_result_fingerprint != result.fingerprint
        or campaign.reader_user != profile.reader_user
        or campaign.transaction_read_only is not profile.transaction_read_only
        or campaign.statement_timeout_ms != profile.statement_timeout_ms
        or campaign.completed_at != result.completed_at
        or campaign.expires_at != result.completed_at + _MAX_PROFILE_AGE
    ):
        raise ValueError("registry join profile campaign authority changed")
    if not campaign.completed_at <= instant < campaign.expires_at:
        raise ValueError("registry join profile campaign is stale")
    return campaign


def decide_registry_join_change(
    draft: RegistryJoinChangeDraft,
    *,
    action: DecisionAction,
    expected_revision: int,
    actor_id: str,
    decided_at: datetime,
    rationale: str,
) -> RegistryJoinChangeDraft:
    """Apply one explicit terminal review decision to a join draft."""

    if (
        draft.status is not RegistryJoinChangeStatus.NEEDS_REVIEW
        or draft.revision != expected_revision
        or draft.decision is not None
    ):
        raise ValueError("registry join draft revision changed or was already decided")
    if action not in {DecisionAction.APPROVE, DecisionAction.REJECT}:
        raise ValueError("registry join review requires approve or reject")
    normalized_rationale = rationale.strip()
    if len(normalized_rationale) < 12:
        raise ValueError("registry join rationale must be meaningful")
    if action is DecisionAction.APPROVE and (
        draft.candidate.recommendation is JoinRecommendation.REJECT_UNSAFE
        or draft.candidate.cardinality.cardinality is Cardinality.MANY_TO_MANY
        or not _has_non_name_evidence(draft.candidate)
    ):
        raise ValueError("registry join candidate lacks safe non-name evidence")
    resulting_revision = draft.revision + 1
    decision_id = _decision_id(draft, action, actor_id, resulting_revision)
    decision = DecisionRecord(
        id=decision_id,
        target_type=DecisionTargetType.JOIN_CONTRACT,
        target_id=draft.candidate.proposal.id,
        action=action,
        status=(
            ApprovalStatus.APPROVED if action is DecisionAction.APPROVE else ApprovalStatus.REJECTED
        ),
        actor=actor_id,
        decided_at=decided_at,
        source_version=draft.revision,
        resulting_version=resulting_revision,
        rationale=normalized_rationale,
        evidence=draft.candidate.evidence,
        risks=draft.candidate.risks,
    )
    contract = (
        _approved_contract(draft.candidate, decision.id)
        if action is DecisionAction.APPROVE
        else None
    )
    return _build_draft(
        {
            **_draft_values(draft),
            "revision": resulting_revision,
            "decision": decision,
            "contract": contract,
            "updated_at": decided_at,
        }
    )


def mark_registry_join_change_ready(
    draft: RegistryJoinChangeDraft,
    proposal: PreparedRegistryJoinProposal,
) -> RegistryJoinChangeDraft:
    """Bind one exact prepared proposal to its immutable authoring draft."""

    if (
        draft.status is not RegistryJoinChangeStatus.NEEDS_REVIEW
        or draft.decision is None
        or draft.contract is None
        or proposal.workspace_id != draft.workspace_id
        or proposal.owner_actor_id != draft.owner_actor_id
        or proposal.scope != draft.scope
        or proposal.base_evidence != draft.base_evidence
        or proposal.profile_campaign != draft.profile_campaign
        or proposal.profile_job != draft.profile_job
        or proposal.decision != draft.decision
        or proposal.contract != draft.contract
        or proposal.draft_id != draft.id
        or proposal.draft_revision != draft.revision
        or proposal.draft_fingerprint != draft.fingerprint
    ):
        raise ValueError("prepared registry join proposal does not match its draft")
    return _build_draft(
        {
            **_draft_values(draft),
            "status": RegistryJoinChangeStatus.READY_FOR_PUBLICATION,
            "prepared_proposal_id": proposal.id,
            "prepared_proposal_fingerprint": proposal.fingerprint,
            "updated_at": proposal.prepared_at,
        }
    )


def supersede_registry_join_change(
    draft: RegistryJoinChangeDraft,
    *,
    superseded_at: datetime,
) -> RegistryJoinChangeDraft:
    """Close one prepared draft while retaining its exact proposal lineage."""

    instant = _aware(superseded_at, "registry join superseded timestamp")
    if (
        draft.status is not RegistryJoinChangeStatus.READY_FOR_PUBLICATION
        or draft.prepared_proposal_id is None
        or draft.prepared_proposal_fingerprint is None
        or instant < draft.updated_at
    ):
        raise ValueError("only a prepared registry join change can be superseded")
    return _build_draft(
        {
            **_draft_values(draft),
            "status": RegistryJoinChangeStatus.SUPERSEDED,
            "updated_at": instant,
        }
    )


def assemble_join_change_registry_version(
    proposal: PreparedRegistryJoinProposal,
    *,
    base: GovernedSemanticRegistrySnapshot,
) -> PublishableRegistryVersion:
    """Append exactly one approved join to one exact complete registry-v2 base."""

    _validate_join_change_base(proposal, base)
    contract = proposal.contract
    if contract.id in {item.id for item in base.join_contracts.contracts}:
        raise ValueError("registry join change cannot replace an active join")
    left_model = contract.left_key.logical_field.root.split(".", 1)[0]
    right_model = contract.right_key.logical_field.root.split(".", 1)[0]
    if _models_are_connected(base.logical_context, left_model, right_model):
        raise ValueError("registry join change cannot connect models already in the same component")
    logical_join = ApprovedLogicalJoin(
        id=contract.id,
        left_model=left_model,
        right_model=right_model,
        cardinality=contract.cardinality,
        fanout_policy=contract.fanout_policy,
        status=ApprovalStatus.APPROVED,
        version=contract.version,
        approval_decision_id=proposal.decision.id,
    )
    provenance = {item.kind: item for item in base.provenance}
    source = f"registry-change:{proposal.id}"
    registry = GovernedSemanticRegistrySnapshot(
        format_version=2,
        registry_id=base.registry_id,
        version=proposal.target_registry_version,
        source=source,
        catalog_scope=base.catalog_scope,
        logical_context=ApprovedLogicalContext(
            version=proposal.target_registry_version,
            source=f"{source}/logical-context",
            models=base.logical_context.models,
            joins=(*base.logical_context.joins, logical_join),
        ),
        mapping_set=GovernedMappingRegistry(
            version=proposal.target_registry_version,
            mappings=base.mapping_set.mappings,
        ),
        join_contracts=GovernedJoinRegistry(
            version=proposal.target_registry_version,
            contracts=(*base.join_contracts.contracts, contract),
        ),
        provenance=(
            RegistryArtifactProvenance(
                kind=RegistryArtifactKind.LOGICAL_MODELS,
                source=f"{source}/{RegistryArtifactKind.LOGICAL_MODELS.value}",
                decision_ids=provenance[RegistryArtifactKind.LOGICAL_MODELS].decision_ids,
            ),
            RegistryArtifactProvenance(
                kind=RegistryArtifactKind.PHYSICAL_MAPPINGS,
                source=f"{source}/{RegistryArtifactKind.PHYSICAL_MAPPINGS.value}",
                decision_ids=provenance[RegistryArtifactKind.PHYSICAL_MAPPINGS].decision_ids,
            ),
            RegistryArtifactProvenance(
                kind=RegistryArtifactKind.JOIN_CONTRACTS,
                source=f"{source}/{RegistryArtifactKind.JOIN_CONTRACTS.value}",
                decision_ids=tuple(
                    sorted(
                        {
                            *provenance[RegistryArtifactKind.JOIN_CONTRACTS].decision_ids,
                            proposal.decision.id,
                        }
                    )
                ),
            ),
        ),
        physical_bindings=base.physical_bindings,
    )
    registry = prepare_datahub_registry_version(registry, proposal.scope)
    return create_publishable_registry_version(
        scope=proposal.scope,
        source_proposal_id=proposal.id,
        source_proposal_fingerprint=proposal.fingerprint,
        base_registry=proposal.base_registry,
        registry=registry,
        review_decision_ids=tuple(
            sorted({*semantic_registry_decision_ids(registry), *proposal.decision_ids})
        ),
    )


def registry_change_fingerprint(payload: object) -> str:
    return hashlib.sha256(_canonical_json(_jsonable(payload)).encode()).hexdigest()


def _endpoint_from_base(
    base: GovernedSemanticRegistrySnapshot,
    key: NormalizedJoinKey,
) -> RegistryJoinEndpointEvidence:
    mapping = next(
        (
            item
            for item in base.mapping_set.mappings
            if item.mapping.logical_field == key.logical_field
            and item.mapping.physical_field == key.physical_field
            and item.mapping.transformation_plan == key.transformation_plan
        ),
        None,
    )
    binding = next(
        (
            item
            for item in base.physical_bindings
            if item.logical_field == key.logical_field and item.physical_field == key.physical_field
        ),
        None,
    )
    if mapping is None or binding is None:
        raise ValueError("registry join key is not backed by an exact active mapping")
    return RegistryJoinEndpointEvidence.create(key=key, mapping=mapping, binding=binding)


def _validated_profile_job_evidence(
    profile_job: SemanticJoinProfileJob,
) -> tuple[SemanticJoinProfileResult, SemanticJoinProfileTargetRef, int]:
    result = profile_job.result
    target = profile_job.execution_target
    contract_version = profile_job.connector_contract_version
    if (
        profile_job.status is not SemanticJoinProfileJobStatus.COMPLETED
        or result is None
        or target is None
        or contract_version is None
    ):
        raise ValueError("registry join profile campaign requires one completed routed job")
    profile = result.profile
    timeout = profile.statement_timeout_ms
    if (
        profile.reader_user is None
        or not profile.reader_user.strip()
        or profile.transaction_read_only is not True
        or timeout is None
        or not _MIN_PROFILE_TIMEOUT_MS <= timeout <= _MAX_PROFILE_TIMEOUT_MS
    ):
        raise ValueError(
            "registry join profile evidence requires a read-only reader and bounded timeout"
        )
    return result, target, contract_version


def _catalog_generation_vector(
    evidence: RegistryJoinBaseEvidence,
) -> tuple[tuple[int, str], ...]:
    return tuple(
        sorted(
            {
                (
                    endpoint.binding.catalog_generation,
                    endpoint.binding.catalog_generation_fingerprint,
                )
                for endpoint in (evidence.left, evidence.right)
            }
        )
    )


def _models_are_connected(
    context: ApprovedLogicalContext,
    left_model: str,
    right_model: str,
) -> bool:
    if left_model == right_model:
        return True
    adjacency: dict[str, set[str]] = {}
    for join in context.joins:
        left = join.left_model.root
        right = join.right_model.root
        adjacency.setdefault(left, set()).add(right)
        adjacency.setdefault(right, set()).add(left)
    pending = [left_model]
    visited: set[str] = set()
    while pending:
        current = pending.pop()
        if current == right_model:
            return True
        if current in visited:
            continue
        visited.add(current)
        pending.extend(adjacency.get(current, set()) - visited)
    return False


def _approved_contract(candidate: JoinCandidate, decision_id: str) -> JoinContract:
    cardinality = candidate.cardinality.cardinality
    fanout_policy = (
        FanoutPolicy.REQUIRE_DISTINCT_FOR_LEFT_ENTITY_METRICS
        if cardinality is Cardinality.ONE_TO_MANY
        else FanoutPolicy.NONE
    )
    return JoinContract(
        id=candidate.proposal.id,
        left_key=candidate.proposal.left_key,
        right_key=candidate.proposal.right_key,
        cardinality=cardinality,
        default_join_type=candidate.proposal.default_join_type,
        fanout_policy=fanout_policy,
        status=ApprovalStatus.APPROVED,
        version=1,
        evidence=candidate.evidence,
        risks=candidate.risks,
        approval_decision_id=decision_id,
    )


def _has_non_name_evidence(candidate: JoinCandidate) -> bool:
    return any(not value.startswith("names_and_definitions:") for value in candidate.evidence)


def _decision_id(
    draft: RegistryJoinChangeDraft,
    action: DecisionAction,
    actor_id: str,
    revision: int,
) -> str:
    digest = registry_change_fingerprint(
        {
            "contract": "registry_join_decision_v1",
            "draft_id": draft.id,
            "draft_fingerprint": draft.fingerprint,
            "action": action.value,
            "actor_id": actor_id,
            "revision": revision,
        }
    )
    return f"join_decision_{digest}"


def _validate_join_change_base(
    proposal: PreparedRegistryJoinProposal,
    base: GovernedSemanticRegistrySnapshot,
) -> None:
    expected = proposal.base_registry
    expected_source = f"datahub:{datahub_registry_document_id(proposal.scope, base.version)}"
    if (
        base.format_version != 2
        or base.registry_id != proposal.scope.registry_id
        or base.catalog_scope != proposal.scope.catalog_scope
        or expected.registry_version != base.version
        or expected.registry_fingerprint != base.fingerprint
        or base.version + 1 != proposal.target_registry_version
        or base.source != expected_source
        or base.logical_context.source != f"{expected_source}/logical-context"
        or any(item.workspace_id != proposal.workspace_id for item in base.physical_bindings)
    ):
        raise ValueError("registry join change base changed or lacks exact v2 authority")
    for endpoint in (proposal.base_evidence.left, proposal.base_evidence.right):
        current = _endpoint_from_base(base, endpoint.key)
        if current != endpoint:
            raise ValueError("registry join endpoint changed after review")


def _build_draft(payload: dict[str, object]) -> RegistryJoinChangeDraft:
    construct: Any = RegistryJoinChangeDraft.model_construct
    provisional = construct(**payload, fingerprint="0" * 64)
    return RegistryJoinChangeDraft(
        **payload,
        fingerprint=registry_change_fingerprint(_draft_payload(provisional)),
    )


def _draft_values(value: RegistryJoinChangeDraft) -> dict[str, object]:
    """Return typed values so provisional serialization never sees nested raw dicts."""

    return {
        "id": value.id,
        "workspace_id": value.workspace_id,
        "owner_actor_id": value.owner_actor_id,
        "scope": value.scope,
        "base_evidence": value.base_evidence,
        "profile_campaign": value.profile_campaign,
        "profile_job": value.profile_job,
        "candidate": value.candidate,
        "revision": value.revision,
        "status": value.status,
        "decision": value.decision,
        "contract": value.contract,
        "prepared_proposal_id": value.prepared_proposal_id,
        "prepared_proposal_fingerprint": value.prepared_proposal_fingerprint,
        "created_at": value.created_at,
        "updated_at": value.updated_at,
    }


def _endpoint_payload(value: RegistryJoinEndpointEvidence) -> dict[str, object]:
    return value.model_dump(mode="json", exclude={"fingerprint"})


def _base_evidence_payload(value: RegistryJoinBaseEvidence) -> dict[str, object]:
    return value.model_dump(mode="json", exclude={"fingerprint"})


def _profile_request_payload(
    value: RegistryJoinProfileRequest,
) -> dict[str, object]:
    return value.model_dump(mode="json", exclude={"fingerprint"})


def _profile_campaign_payload(
    value: RegistryJoinProfileCampaign,
) -> dict[str, object]:
    return value.model_dump(mode="json", exclude={"fingerprint"})


def _draft_payload(value: RegistryJoinChangeDraft) -> dict[str, object]:
    return value.model_dump(mode="json", exclude={"fingerprint"})


def _jsonable(value: object) -> object:
    if isinstance(value, FrozenDomainModel):
        return value.model_dump(mode="json")
    return json.loads(json.dumps(value, default=_json_default))


def _json_default(value: object) -> object:
    if isinstance(value, FrozenDomainModel):
        return value.model_dump(mode="json")
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"unsupported registry change value: {type(value)!r}")


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(value: str, label: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return value


__all__ = [
    "PreparedRegistryJoinProposal",
    "RegistryChangeProposalKind",
    "RegistryJoinBaseEvidence",
    "RegistryJoinChangeDraft",
    "RegistryJoinChangeStatus",
    "RegistryJoinEndpointEvidence",
    "RegistryJoinProfileCampaign",
    "RegistryJoinProfileRequest",
    "assemble_join_change_registry_version",
    "decide_registry_join_change",
    "mark_registry_join_change_ready",
    "registry_change_fingerprint",
    "registry_join_profile_scan_id_v1",
    "resolve_registry_join_base_evidence",
    "supersede_registry_join_change",
    "validate_registry_join_profile_campaign",
]
