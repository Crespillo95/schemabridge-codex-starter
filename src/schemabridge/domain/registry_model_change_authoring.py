"""Pure authoring contracts for one governed registry-v2 model replacement."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.concepts import LogicalModelRef
from schemabridge.domain.decisions import (
    ApprovalStatus,
    DecisionAction,
    DecisionRecord,
    DecisionTargetType,
)
from schemabridge.domain.joins import (
    Cardinality,
    FanoutPolicy,
    JoinContract,
    JoinProposal,
    classify_cardinality,
)
from schemabridge.domain.registry_model_changes import (
    PreparedRegistryModelReplacementProposal,
    RegistryIncidentJoinChange,
    RegistryIncidentJoinPreservation,
    RegistryIncidentJoinRemoval,
    RegistryIncidentJoinUpsert,
    RegistryModelChangeAuthority,
    RegistryModelChangeKind,
    RegistryModelJoinProfileWitness,
    RegistryModelReplacementBase,
    resolve_registry_model_join_profile_endpoints,
)
from schemabridge.domain.semantic_onboarding import (
    OnboardingEvidence,
    OnboardingRegistryBase,
    PreparedSemanticOnboardingProposal,
    SemanticOnboardingDecision,
    SemanticOnboardingTargetKind,
)
from schemabridge.domain.semantic_profile_jobs import (
    SemanticJoinProfileJob,
    SemanticJoinProfileProposal,
    SemanticJoinProfileTargetRef,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[a-z][a-z0-9_-]{2,79}$")
_MAX_DRAFT_BYTES = 4 * 1024 * 1024
MAX_REGISTRY_MODEL_CHANGE_HISTORY = 1_000


class RegistryModelChangeStatus(StrEnum):
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    READY_FOR_PUBLICATION = "ready_for_publication"


class RegistryModelChangeAuditEvent(StrEnum):
    DRAFT_CREATED = "draft_created"
    DECISION_RECORDED = "decision_recorded"
    PUBLICATION_PREPARED = "publication_prepared"


class RegistryModelReplacementSourceEvidence(FrozenDomainModel):
    """Exact persisted M33 proposal, draft owner, and immutable decision closure."""

    proposal: PreparedSemanticOnboardingProposal
    owner_actor_id: str = Field(min_length=1, max_length=200)
    decisions: tuple[SemanticOnboardingDecision, ...] = Field(
        min_length=2,
        max_length=2_002,
    )
    fingerprint: str

    @field_validator("owner_actor_id")
    @classmethod
    def owner_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("registry model source owner must not be blank")
        return value

    @field_validator("fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "registry model source fingerprint")

    @model_validator(mode="after")
    def source_must_be_exact(self) -> RegistryModelReplacementSourceEvidence:
        proposal = self.proposal
        decision_ids = tuple(item.id for item in self.decisions)
        current_ids = {
            proposal.model.decision_id,
            *(item.decision_id for item in proposal.mappings),
        }
        current_actors = {
            proposal.model.decided_by,
            *(item.decided_by for item in proposal.mappings),
        }
        if (
            decision_ids != tuple(sorted(set(decision_ids)))
            or decision_ids != proposal.decision_ids
            or None in current_ids
            or not current_ids.issubset(set(decision_ids))
            or None in current_actors
            or self.owner_actor_id == proposal.prepared_by
            or proposal.prepared_by in current_actors
            or any(
                item.workspace_id != proposal.workspace_id or item.draft_id != proposal.draft_id
                for item in self.decisions
            )
            or self.fingerprint
            != registry_model_authoring_fingerprint(
                self.model_dump(mode="json", exclude={"fingerprint"})
            )
        ):
            raise ValueError("registry model replacement source is incomplete")
        return self

    @classmethod
    def create(
        cls,
        *,
        proposal: PreparedSemanticOnboardingProposal,
        owner_actor_id: str,
        decisions: tuple[SemanticOnboardingDecision, ...],
    ) -> RegistryModelReplacementSourceEvidence:
        payload = {
            "proposal": proposal,
            "owner_actor_id": owner_actor_id,
            "decisions": tuple(sorted(decisions, key=lambda item: item.id)),
        }
        construct: Any = cls.model_construct
        provisional = construct(**payload, fingerprint="0" * 64)
        return cls(
            **payload,
            fingerprint=registry_model_authoring_fingerprint(
                provisional.model_dump(mode="json", exclude={"fingerprint"})
            ),
        )


class RequestRegistryModelJoinProfileInput(FrozenDomainModel):
    """Client-safe candidate-profile intent; authority is always reread server-side."""

    request_id: str = Field(min_length=3, max_length=80)
    change_id: str = Field(min_length=3, max_length=80)
    replacement_proposal_id: str = Field(min_length=3, max_length=200)
    expected_replacement_fingerprint: str
    target_model_id: LogicalModelRef
    expected_base_registry: OnboardingRegistryBase
    join_id: str = Field(min_length=3, max_length=200)
    proposal: JoinProposal
    expected_execution_target_fingerprint: str

    @field_validator("request_id", "change_id")
    @classmethod
    def request_id_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("registry model profile request identifier is invalid")
        return value

    @field_validator(
        "expected_replacement_fingerprint",
        "expected_execution_target_fingerprint",
    )
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "registry model profile expected fingerprint")

    @model_validator(mode="after")
    def proposal_must_match_join(self) -> RequestRegistryModelJoinProfileInput:
        if self.proposal.id != self.join_id:
            raise ValueError("registry model profile proposal targets another join")
        return self


class RegistryModelJoinProfileRequest(FrozenDomainModel):
    """Complete pre-I/O authority for profiling one candidate replacement join."""

    workspace_id: str = Field(min_length=3, max_length=200)
    change_id: str = Field(min_length=3, max_length=80)
    replacement_source: RegistryModelReplacementSourceEvidence
    base: RegistryModelReplacementBase
    incident_join_id: str = Field(min_length=3, max_length=200)
    proposal: JoinProposal
    connection_id: CatalogConnectionId
    execution_target: SemanticJoinProfileTargetRef
    requested_at: datetime
    scan_id: str = Field(min_length=69, max_length=69)
    external_writes_performed: Literal[False] = False
    fingerprint: str

    @field_validator("change_id")
    @classmethod
    def change_id_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("registry model profile change identifier is invalid")
        return value

    @field_validator("requested_at")
    @classmethod
    def time_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("registry model profile timestamp requires a timezone")
        return value

    @field_validator("scan_id")
    @classmethod
    def scan_id_must_be_sha256(cls, value: str) -> str:
        if not value.startswith("scan_"):
            raise ValueError("registry model profile scan id is invalid")
        _sha256(value.removeprefix("scan_"), "registry model profile scan id")
        return value

    @field_validator("fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "registry model profile request fingerprint")

    @model_validator(mode="after")
    def request_must_be_exact(self) -> RegistryModelJoinProfileRequest:
        source = self.replacement_source.proposal
        incident = next(
            (
                item
                for item in self.base.incident_joins
                if item.contract.id == self.incident_join_id
            ),
            None,
        )
        if incident is None:
            raise ValueError("registry model profile incident join is unavailable")
        left, right = resolve_registry_model_join_profile_endpoints(
            base=self.base,
            replacement=source,
            incident=incident,
            proposal=self.proposal,
        )
        expected_scan = "scan_" + registry_model_authoring_fingerprint(
            {
                "kind": "registry_model_join_profile_v1",
                "workspace_id": self.workspace_id,
                "change_id": self.change_id,
                "source_fingerprint": self.replacement_source.fingerprint,
                "base_fingerprint": self.base.fingerprint,
                "join_id": self.incident_join_id,
                "proposal": self.proposal,
                "execution_target": self.execution_target,
                "requested_at": self.requested_at,
            }
        )
        if (
            self.workspace_id != self.base.scope.workspace_id
            or source.workspace_id != self.workspace_id
            or self.proposal.id != self.incident_join_id
            or left.binding.connection_id != self.connection_id
            or right.binding.connection_id != self.connection_id
            or self.execution_target.workspace_id != self.workspace_id
            or self.execution_target.connection_id != self.connection_id
            or self.scan_id != expected_scan
            or self.fingerprint
            != registry_model_authoring_fingerprint(
                self.model_dump(mode="json", exclude={"fingerprint"})
            )
        ):
            raise ValueError("registry model profile authority is incomplete")
        return self

    @classmethod
    def create(
        cls,
        *,
        workspace_id: str,
        change_id: str,
        replacement_source: RegistryModelReplacementSourceEvidence,
        base: RegistryModelReplacementBase,
        incident_join_id: str,
        proposal: JoinProposal,
        execution_target: SemanticJoinProfileTargetRef,
        requested_at: datetime,
    ) -> RegistryModelJoinProfileRequest:
        incident = next(
            item for item in base.incident_joins if item.contract.id == incident_join_id
        )
        left, _ = resolve_registry_model_join_profile_endpoints(
            base=base,
            replacement=replacement_source.proposal,
            incident=incident,
            proposal=proposal,
        )
        scan_id = "scan_" + registry_model_authoring_fingerprint(
            {
                "kind": "registry_model_join_profile_v1",
                "workspace_id": workspace_id,
                "change_id": change_id,
                "source_fingerprint": replacement_source.fingerprint,
                "base_fingerprint": base.fingerprint,
                "join_id": incident_join_id,
                "proposal": proposal,
                "execution_target": execution_target,
                "requested_at": requested_at,
            }
        )
        payload = {
            "workspace_id": workspace_id,
            "change_id": change_id,
            "replacement_source": replacement_source,
            "base": base,
            "incident_join_id": incident_join_id,
            "proposal": proposal,
            "connection_id": left.binding.connection_id,
            "execution_target": execution_target,
            "requested_at": requested_at,
            "scan_id": scan_id,
            "external_writes_performed": False,
        }
        construct: Any = cls.model_construct
        provisional = construct(**payload, fingerprint="0" * 64)
        return cls(
            **payload,
            fingerprint=registry_model_authoring_fingerprint(
                provisional.model_dump(mode="json", exclude={"fingerprint"})
            ),
        )

    @property
    def bound_proposal(self) -> SemanticJoinProfileProposal:
        return SemanticJoinProfileProposal(
            connection_id=self.connection_id,
            proposal=self.proposal,
        )


class RegistryModelJoinProfileAuthoringRequest(FrozenDomainModel):
    id: str = Field(min_length=3, max_length=80)
    workspace_id: str = Field(min_length=3, max_length=200)
    owner_actor_id: str = Field(min_length=1, max_length=200)
    request: RegistryModelJoinProfileRequest
    created_at: datetime
    external_writes_performed: Literal[False] = False
    fingerprint: str

    @field_validator("id")
    @classmethod
    def id_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("registry model profile authoring id is invalid")
        return value

    @field_validator("created_at")
    @classmethod
    def time_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("registry model profile authoring time requires a timezone")
        return value

    @field_validator("fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "registry model profile authoring fingerprint")

    @model_validator(mode="after")
    def authoring_must_be_exact(self) -> RegistryModelJoinProfileAuthoringRequest:
        if (
            self.workspace_id != self.request.workspace_id
            or self.created_at != self.request.requested_at
            or self.fingerprint
            != registry_model_authoring_fingerprint(
                self.model_dump(mode="json", exclude={"fingerprint"})
            )
        ):
            raise ValueError("registry model profile authoring is incomplete")
        return self

    @classmethod
    def create(
        cls,
        *,
        id: str,
        workspace_id: str,
        owner_actor_id: str,
        request: RegistryModelJoinProfileRequest,
        created_at: datetime,
    ) -> RegistryModelJoinProfileAuthoringRequest:
        payload = {
            "id": id,
            "workspace_id": workspace_id,
            "owner_actor_id": owner_actor_id,
            "request": request,
            "created_at": created_at,
            "external_writes_performed": False,
        }
        construct: Any = cls.model_construct
        provisional = construct(**payload, fingerprint="0" * 64)
        return cls(
            **payload,
            fingerprint=registry_model_authoring_fingerprint(
                provisional.model_dump(mode="json", exclude={"fingerprint"})
            ),
        )


class RegistryModelJoinProfileAuditEvent(StrEnum):
    REQUEST_PERSISTED = "request_persisted"
    JOB_BOUND = "job_bound"
    WITNESS_RECORDED = "witness_recorded"


class RegistryModelJoinProfileAuditRecord(FrozenDomainModel):
    id: str = Field(min_length=3, max_length=200)
    workspace_id: str = Field(min_length=3, max_length=200)
    request_id: str = Field(min_length=3, max_length=80)
    event: RegistryModelJoinProfileAuditEvent
    actor_id: str = Field(min_length=1, max_length=200)
    occurred_at: datetime
    request_fingerprint: str
    job_id: str | None = Field(default=None, min_length=3, max_length=200)
    witness_fingerprint: str | None = None
    external_writes_performed: Literal[False] = False
    fingerprint: str

    @field_validator("occurred_at")
    @classmethod
    def time_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("registry model profile audit time requires a timezone")
        return value

    @field_validator("request_fingerprint", "witness_fingerprint", "fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str | None) -> str | None:
        if value is not None:
            return _sha256(value, "registry model profile audit fingerprint")
        return value

    @model_validator(mode="after")
    def event_shape_must_be_closed(self) -> RegistryModelJoinProfileAuditRecord:
        if self.event is RegistryModelJoinProfileAuditEvent.REQUEST_PERSISTED:
            valid = self.job_id is None and self.witness_fingerprint is None
        elif self.event is RegistryModelJoinProfileAuditEvent.JOB_BOUND:
            valid = self.job_id is not None and self.witness_fingerprint is None
        else:
            valid = self.job_id is not None and self.witness_fingerprint is not None
        if not valid or self.fingerprint != registry_model_authoring_fingerprint(
            self.model_dump(mode="json", exclude={"fingerprint"})
        ):
            raise ValueError("registry model profile audit is invalid")
        return self

    @classmethod
    def create(
        cls,
        *,
        authoring: RegistryModelJoinProfileAuthoringRequest,
        event: RegistryModelJoinProfileAuditEvent,
        actor_id: str,
        occurred_at: datetime,
        job_id: str | None = None,
        witness_fingerprint: str | None = None,
    ) -> RegistryModelJoinProfileAuditRecord:
        identity = registry_model_authoring_fingerprint(
            {
                "workspace_id": authoring.workspace_id,
                "request_id": authoring.id,
                "event": event.value,
                "actor_id": actor_id,
                "job_id": job_id,
                "witness_fingerprint": witness_fingerprint,
            }
        )
        payload = {
            "id": f"registry_model_profile_audit_{identity}",
            "workspace_id": authoring.workspace_id,
            "request_id": authoring.id,
            "event": event,
            "actor_id": actor_id,
            "occurred_at": occurred_at,
            "request_fingerprint": authoring.fingerprint,
            "job_id": job_id,
            "witness_fingerprint": witness_fingerprint,
            "external_writes_performed": False,
        }
        construct: Any = cls.model_construct
        provisional = construct(**payload, fingerprint="0" * 64)
        return cls(
            **payload,
            fingerprint=registry_model_authoring_fingerprint(
                provisional.model_dump(mode="json", exclude={"fingerprint"})
            ),
        )


class RegistryModelJoinProfileMutation(FrozenDomainModel):
    authoring: RegistryModelJoinProfileAuthoringRequest
    job: SemanticJoinProfileJob | None = None
    witness: RegistryModelJoinProfileWitness | None = None
    replayed: bool = False
    external_writes_performed: Literal[False] = False

    @model_validator(mode="after")
    def response_must_match_request(self) -> RegistryModelJoinProfileMutation:
        request = self.authoring.request
        if self.job is not None and (
            self.job.workspace_id != self.authoring.workspace_id
            or self.job.scan_id != request.scan_id
            or self.job.bound_proposal != request.bound_proposal
            or self.job.execution_target != request.execution_target
        ):
            raise ValueError("registry model profile job differs from request")
        if self.witness is not None and (
            self.job is None
            or self.witness.change_id != request.change_id
            or self.witness.source_replacement_proposal_id != request.replacement_source.proposal.id
            or self.witness.proposal != request.proposal
            or self.witness.scope != request.base.scope
            or self.witness.base_registry != request.base.base_registry
            or self.witness.base_fingerprint != request.base.fingerprint
            or self.witness.target_model_id != request.base.target_model.id
            or self.witness.target_model_version
            != request.replacement_source.proposal.model.definition.version
            or self.witness.connection_id != request.connection_id
            or self.witness.execution_target != request.execution_target
            or self.witness.result != self.job.result
        ):
            raise ValueError("registry model profile witness differs from request")
        return self


class RegistryIncidentJoinPreserveInput(FrozenDomainModel):
    action: Literal["preserve_exact"] = "preserve_exact"
    join_id: str = Field(min_length=3, max_length=200)


class RegistryIncidentJoinRemovalInput(FrozenDomainModel):
    action: Literal["remove_explicit"] = "remove_explicit"
    join_id: str = Field(min_length=3, max_length=200)
    risks: tuple[str, ...] = Field(min_length=1, max_length=20)


class RegistryIncidentJoinUpsertInput(FrozenDomainModel):
    action: Literal["upsert_fresh"] = "upsert_fresh"
    join_id: str = Field(min_length=3, max_length=200)
    proposal: JoinProposal
    expected_profile_witness_fingerprint: str
    risks: tuple[str, ...] = Field(min_length=1, max_length=20)

    @field_validator("expected_profile_witness_fingerprint")
    @classmethod
    def witness_fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "registry model profile witness fingerprint")


RegistryIncidentJoinInput: TypeAlias = Annotated[
    RegistryIncidentJoinPreserveInput
    | RegistryIncidentJoinRemovalInput
    | RegistryIncidentJoinUpsertInput,
    Field(discriminator="action"),
]


class CreateRegistryModelChangeInput(FrozenDomainModel):
    change_id: str = Field(min_length=3, max_length=80)
    replacement_proposal_id: str = Field(min_length=3, max_length=200)
    expected_replacement_fingerprint: str
    target_model_id: LogicalModelRef
    expected_base_registry: OnboardingRegistryBase
    kind: RegistryModelChangeKind
    remediation_report_id: str | None = Field(default=None, min_length=3, max_length=200)
    expected_remediation_report_fingerprint: str | None = None
    resolved_finding_ids: tuple[str, ...] = Field(default=(), max_length=2_000)
    incident_joins: tuple[RegistryIncidentJoinInput, ...] = Field(default=(), max_length=500)
    risks: tuple[str, ...] = Field(min_length=1, max_length=100)

    @field_validator("change_id")
    @classmethod
    def id_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("registry model change identifier is invalid")
        return value

    @field_validator(
        "expected_replacement_fingerprint",
        "expected_remediation_report_fingerprint",
    )
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str | None) -> str | None:
        if value is not None:
            return _sha256(value, "registry model change expected fingerprint")
        return value

    @field_validator("risks", "resolved_finding_ids")
    @classmethod
    def values_must_be_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))) or any(not item.strip() for item in values):
            raise ValueError("registry model change values must be sorted and unique")
        return values

    @model_validator(mode="after")
    def authority_shape_must_match_kind(self) -> CreateRegistryModelChangeInput:
        remediation = self.kind is RegistryModelChangeKind.M26_REMEDIATION
        supplied = (
            self.remediation_report_id is not None
            and self.expected_remediation_report_fingerprint is not None
            and bool(self.resolved_finding_ids)
        )
        if remediation != supplied:
            raise ValueError("registry model remediation authority is incomplete")
        join_ids = tuple(item.join_id for item in self.incident_joins)
        if join_ids != tuple(sorted(set(join_ids))):
            raise ValueError("registry model incident joins must be sorted and unique")
        return self


class RegistryModelIncidentIntent(FrozenDomainModel):
    action: Literal["preserve_exact", "remove_explicit", "upsert_fresh"]
    base_join_id: str = Field(min_length=3, max_length=200)
    proposal: JoinProposal | None = None
    profile_witness: RegistryModelJoinProfileWitness | None = None
    risks: tuple[str, ...] = Field(default=(), max_length=20)

    @field_validator("risks")
    @classmethod
    def risks_must_be_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))) or any(not value.strip() for value in values):
            raise ValueError("registry model incident risks must be sorted and unique")
        return values

    @model_validator(mode="after")
    def intent_shape_must_be_closed(self) -> RegistryModelIncidentIntent:
        upsert = self.action == "upsert_fresh"
        changed = self.action != "preserve_exact"
        if (
            upsert != (self.proposal is not None and self.profile_witness is not None)
            or changed != bool(self.risks)
            or (
                upsert
                and self.proposal is not None
                and self.profile_witness is not None
                and (
                    self.proposal.id != self.base_join_id
                    or self.profile_witness.proposal != self.proposal
                )
            )
        ):
            raise ValueError("registry model incident intent is incomplete")
        return self


class RegistryModelChangeDraft(FrozenDomainModel):
    id: str = Field(min_length=3, max_length=80)
    workspace_id: str = Field(min_length=3, max_length=200)
    owner_actor_id: str = Field(min_length=1, max_length=200)
    source: RegistryModelReplacementSourceEvidence
    base: RegistryModelReplacementBase
    authority: RegistryModelChangeAuthority
    model_decision: DecisionRecord
    mapping_decisions: tuple[DecisionRecord, ...] = Field(min_length=1, max_length=2_000)
    incident_intents: tuple[RegistryModelIncidentIntent, ...] = Field(default=(), max_length=500)
    incident_changes: tuple[RegistryIncidentJoinChange, ...] = Field(default=(), max_length=500)
    outer_decision: DecisionRecord | None = None
    risks: tuple[str, ...] = Field(min_length=1, max_length=100)
    revision: int = Field(default=1, ge=1)
    status: RegistryModelChangeStatus = RegistryModelChangeStatus.NEEDS_REVIEW
    reviewed_by: str | None = Field(default=None, min_length=1, max_length=200)
    reviewed_at: datetime | None = None
    prepared_proposal_id: str | None = Field(default=None, min_length=3, max_length=200)
    prepared_proposal_fingerprint: str | None = None
    prepared_by: str | None = Field(default=None, min_length=1, max_length=200)
    created_at: datetime
    updated_at: datetime
    fingerprint: str

    @field_validator("id")
    @classmethod
    def id_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("registry model draft identifier is invalid")
        return value

    @field_validator("created_at", "updated_at", "reviewed_at")
    @classmethod
    def times_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("registry model draft timestamps require a timezone")
        return value

    @field_validator("prepared_proposal_fingerprint", "fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str | None) -> str | None:
        if value is not None:
            return _sha256(value, "registry model draft fingerprint")
        return value

    @field_validator("risks")
    @classmethod
    def risks_must_be_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))) or any(not item.strip() for item in values):
            raise ValueError("registry model draft risks must be sorted and unique")
        return values

    @model_validator(mode="after")
    def draft_must_be_exact(self) -> RegistryModelChangeDraft:
        source = self.source.proposal
        incident_ids = tuple(item.base_join_id for item in self.incident_intents)
        base_ids = tuple(item.contract.id for item in self.base.incident_joins)
        change_ids = tuple(item.base.contract.id for item in self.incident_changes)
        reviewed = self.reviewed_by is not None and self.reviewed_at is not None
        decision_recorded = self.outer_decision is not None
        prepared = all(
            value is not None
            for value in (
                self.prepared_proposal_id,
                self.prepared_proposal_fingerprint,
                self.prepared_by,
            )
        )
        if (
            self.workspace_id != self.base.scope.workspace_id
            or source.workspace_id != self.workspace_id
            or source.base_registry != self.base.base_registry
            or self.authority.base_fingerprint != self.base.fingerprint
            or source.model.definition.id != self.base.target_model.id
            or incident_ids != base_ids
            or self.updated_at < self.created_at
            or (
                self.status is RegistryModelChangeStatus.NEEDS_REVIEW
                and (reviewed or decision_recorded or prepared)
            )
            or (
                self.status is RegistryModelChangeStatus.REJECTED
                and (not reviewed or not decision_recorded or prepared)
            )
            or (
                self.status is RegistryModelChangeStatus.APPROVED
                and (not reviewed or not decision_recorded or prepared)
            )
            or (
                self.status is RegistryModelChangeStatus.READY_FOR_PUBLICATION
                and (not reviewed or not decision_recorded or not prepared)
            )
            or (
                self.outer_decision is not None
                and (
                    self.outer_decision.actor != self.reviewed_by
                    or self.outer_decision.decided_at != self.reviewed_at
                    or self.outer_decision.target_type is not DecisionTargetType.LOGICAL_MODEL
                    or self.outer_decision.target_id != source.model.definition.id.root
                    or self.outer_decision.source_version != self.base.target_model.version
                    or self.outer_decision.resulting_version != source.model.definition.version
                    or (
                        self.status is RegistryModelChangeStatus.REJECTED
                        and (
                            self.outer_decision.action is not DecisionAction.REJECT
                            or self.outer_decision.status is not ApprovalStatus.REJECTED
                        )
                    )
                    or (
                        self.status
                        in {
                            RegistryModelChangeStatus.APPROVED,
                            RegistryModelChangeStatus.READY_FOR_PUBLICATION,
                        }
                        and (
                            self.outer_decision.action is not DecisionAction.APPROVE
                            or self.outer_decision.status is not ApprovalStatus.APPROVED
                        )
                    )
                )
            )
            or (self.status is RegistryModelChangeStatus.APPROVED and change_ids != base_ids)
            or (
                self.status is RegistryModelChangeStatus.READY_FOR_PUBLICATION
                and change_ids != base_ids
            )
            or (
                self.status
                in {RegistryModelChangeStatus.NEEDS_REVIEW, RegistryModelChangeStatus.REJECTED}
                and self.incident_changes
            )
            or self.fingerprint
            != registry_model_authoring_fingerprint(
                self.model_dump(mode="json", exclude={"fingerprint"})
            )
        ):
            raise ValueError("registry model change draft is incomplete or stale")
        if len(_canonical_json(self.model_dump(mode="json"))) > _MAX_DRAFT_BYTES:
            raise ValueError("registry model change draft exceeds its byte limit")
        return self


class RegistryModelChangeAuditRecord(FrozenDomainModel):
    id: str = Field(min_length=3, max_length=200)
    workspace_id: str = Field(min_length=3, max_length=200)
    change_id: str = Field(min_length=3, max_length=80)
    event: RegistryModelChangeAuditEvent
    actor_id: str = Field(min_length=1, max_length=200)
    occurred_at: datetime
    source_revision: int = Field(ge=0)
    resulting_revision: int = Field(ge=1)
    previous_fingerprint: str | None = None
    resulting_fingerprint: str
    proposal_id: str | None = Field(default=None, min_length=3, max_length=200)
    external_writes_performed: Literal[False] = False
    fingerprint: str

    @field_validator("occurred_at")
    @classmethod
    def time_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("registry model audit timestamp requires a timezone")
        return value

    @field_validator("previous_fingerprint", "resulting_fingerprint", "fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str | None) -> str | None:
        if value is not None:
            return _sha256(value, "registry model audit fingerprint")
        return value

    @model_validator(mode="after")
    def audit_must_be_exact(self) -> RegistryModelChangeAuditRecord:
        if self.event is RegistryModelChangeAuditEvent.DRAFT_CREATED:
            valid = (
                self.source_revision == 0
                and self.resulting_revision == 1
                and self.previous_fingerprint is None
                and self.proposal_id is None
            )
        elif self.event is RegistryModelChangeAuditEvent.DECISION_RECORDED:
            valid = (
                self.resulting_revision == self.source_revision + 1
                and self.previous_fingerprint is not None
                and self.proposal_id is None
            )
        else:
            valid = (
                self.event is RegistryModelChangeAuditEvent.PUBLICATION_PREPARED
                and self.resulting_revision == self.source_revision + 1
                and self.previous_fingerprint is not None
                and self.proposal_id is not None
            )
        if not valid or self.fingerprint != registry_model_authoring_fingerprint(
            self.model_dump(mode="json", exclude={"fingerprint"})
        ):
            raise ValueError("registry model audit binding is invalid")
        return self

    @classmethod
    def create(
        cls,
        *,
        draft: RegistryModelChangeDraft,
        event: RegistryModelChangeAuditEvent,
        actor_id: str,
        occurred_at: datetime,
        source_revision: int,
        previous_fingerprint: str | None,
        proposal_id: str | None = None,
    ) -> RegistryModelChangeAuditRecord:
        identity = registry_model_authoring_fingerprint(
            {
                "workspace_id": draft.workspace_id,
                "change_id": draft.id,
                "event": event.value,
                "actor_id": actor_id,
                "resulting_fingerprint": draft.fingerprint,
            }
        )
        payload = {
            "id": f"registry_model_audit_{identity}",
            "workspace_id": draft.workspace_id,
            "change_id": draft.id,
            "event": event,
            "actor_id": actor_id,
            "occurred_at": occurred_at,
            "source_revision": source_revision,
            "resulting_revision": draft.revision,
            "previous_fingerprint": previous_fingerprint,
            "resulting_fingerprint": draft.fingerprint,
            "proposal_id": proposal_id,
            "external_writes_performed": False,
        }
        construct: Any = cls.model_construct
        provisional = construct(**payload, fingerprint="0" * 64)
        return cls(
            **payload,
            fingerprint=registry_model_authoring_fingerprint(
                provisional.model_dump(mode="json", exclude={"fingerprint"})
            ),
        )


class RegistryModelChangeMutation(FrozenDomainModel):
    draft: RegistryModelChangeDraft
    proposal: PreparedRegistryModelReplacementProposal | None = None
    replayed: bool = False
    external_writes_performed: Literal[False] = False

    @model_validator(mode="after")
    def proposal_must_match_draft(self) -> RegistryModelChangeMutation:
        if self.proposal is not None and (
            self.draft.prepared_proposal_id != self.proposal.id
            or self.draft.prepared_proposal_fingerprint != self.proposal.fingerprint
            or self.proposal.workspace_id != self.draft.workspace_id
            or self.draft.status is not RegistryModelChangeStatus.READY_FOR_PUBLICATION
            or self.proposal.draft_id != self.draft.id
            or self.proposal.draft_revision != self.draft.revision - 1
            or self.proposal.source_replacement_proposal_id != self.draft.source.proposal.id
            or self.proposal.source_replacement_proposal_fingerprint
            != self.draft.source.proposal.fingerprint
            or self.proposal.owner_actor_id != self.draft.owner_actor_id
            or self.proposal.base != self.draft.base
            or self.proposal.replacement != self.draft.source.proposal
            or self.proposal.outer_decision != self.draft.outer_decision
            or self.proposal.model_decision != self.draft.model_decision
            or self.proposal.mapping_decisions != self.draft.mapping_decisions
            or self.proposal.authority != self.draft.authority
            or self.proposal.incident_join_changes != self.draft.incident_changes
            or self.proposal.risks != self.draft.risks
            or self.proposal.created_at != self.draft.created_at
            or self.proposal.prepared_by != self.draft.prepared_by
            or self.proposal.prepared_at != self.draft.updated_at
        ):
            raise ValueError("registry model proposal differs from its draft")
        return self


class RegistryModelChangeSnapshot(FrozenDomainModel):
    draft: RegistryModelChangeDraft
    audit_visible: bool = False
    history_truncated: bool = False
    audit: tuple[RegistryModelChangeAuditRecord, ...] = ()

    @model_validator(mode="after")
    def audit_must_be_scoped(self) -> RegistryModelChangeSnapshot:
        if not self.audit_visible and (self.audit or self.history_truncated):
            raise ValueError("hidden registry model audit cannot carry history")
        if any(
            item.workspace_id != self.draft.workspace_id or item.change_id != self.draft.id
            for item in self.audit
        ):
            raise ValueError("registry model audit belongs to another change")
        return self


def create_registry_model_change_draft(
    *,
    change_id: str,
    owner_actor_id: str,
    source: RegistryModelReplacementSourceEvidence,
    base: RegistryModelReplacementBase,
    authority: RegistryModelChangeAuthority,
    incident_intents: tuple[RegistryModelIncidentIntent, ...],
    risks: tuple[str, ...],
    created_at: datetime,
) -> RegistryModelChangeDraft:
    model_decision, mapping_decisions = replacement_source_decisions(
        source,
        base=base,
        model_risks=tuple(sorted(set(risks))),
    )
    payload = {
        "id": change_id,
        "workspace_id": source.proposal.workspace_id,
        "owner_actor_id": owner_actor_id,
        "source": source,
        "base": base,
        "authority": authority,
        "model_decision": model_decision,
        "mapping_decisions": mapping_decisions,
        "incident_intents": incident_intents,
        "incident_changes": (),
        "outer_decision": None,
        "risks": tuple(sorted(set(risks))),
        "revision": 1,
        "status": RegistryModelChangeStatus.NEEDS_REVIEW,
        "reviewed_by": None,
        "reviewed_at": None,
        "prepared_proposal_id": None,
        "prepared_proposal_fingerprint": None,
        "prepared_by": None,
        "created_at": created_at,
        "updated_at": created_at,
    }
    return _build_draft(payload)


def decide_registry_model_change(
    draft: RegistryModelChangeDraft,
    *,
    action: DecisionAction,
    actor_id: str,
    decided_at: datetime,
    rationale: str,
) -> RegistryModelChangeDraft:
    if draft.status is not RegistryModelChangeStatus.NEEDS_REVIEW or draft.revision != 1:
        raise ValueError("registry model draft was already decided")
    if action not in {DecisionAction.APPROVE, DecisionAction.REJECT}:
        raise ValueError("registry model review requires approve or reject")
    normalized = rationale.strip()
    if len(normalized) < 12:
        raise ValueError("registry model review rationale must be meaningful")
    source_actors = {
        draft.owner_actor_id,
        draft.source.proposal.prepared_by,
        *(item.actor_id for item in draft.source.decisions),
    }
    if actor_id in source_actors:
        raise ValueError("registry model review requires a separate steward")
    outer_decision = DecisionRecord(
        id=_outer_decision_id(draft, actor_id, action),
        target_type=DecisionTargetType.LOGICAL_MODEL,
        target_id=draft.source.proposal.model.definition.id.root,
        action=action,
        status=(
            ApprovalStatus.APPROVED if action is DecisionAction.APPROVE else ApprovalStatus.REJECTED
        ),
        actor=actor_id,
        decided_at=decided_at,
        source_version=draft.base.target_model.version,
        resulting_version=draft.source.proposal.model.definition.version,
        rationale=normalized,
        evidence=(
            f"registry_model_source:{draft.source.fingerprint}",
            f"registry_model_authority:{draft.authority.fingerprint}",
        ),
        risks=draft.risks,
    )
    if action is DecisionAction.REJECT:
        return _build_draft(
            {
                **_draft_values(draft),
                "revision": 2,
                "status": RegistryModelChangeStatus.REJECTED,
                "outer_decision": outer_decision,
                "reviewed_by": actor_id,
                "reviewed_at": decided_at,
                "updated_at": decided_at,
            }
        )
    base_by_id = {item.contract.id: item for item in draft.base.incident_joins}
    changes: list[RegistryIncidentJoinChange] = []
    for intent in draft.incident_intents:
        incident = base_by_id[intent.base_join_id]
        if intent.action == "preserve_exact":
            changes.append(RegistryIncidentJoinPreservation(base=incident))
            continue
        evidence = (
            f"registry_model_change:{draft.authority.fingerprint}"
            if intent.profile_witness is None
            else f"aggregate_profile:{intent.profile_witness.result.fingerprint}"
        )
        decision = DecisionRecord(
            id=_incident_decision_id(draft, intent, actor_id),
            target_type=DecisionTargetType.JOIN_CONTRACT,
            target_id=incident.contract.id,
            action=(
                DecisionAction.REJECT
                if intent.action == "remove_explicit"
                else DecisionAction.APPROVE
            ),
            status=(
                ApprovalStatus.REJECTED
                if intent.action == "remove_explicit"
                else ApprovalStatus.APPROVED
            ),
            actor=actor_id,
            decided_at=decided_at,
            source_version=incident.contract.version,
            resulting_version=incident.contract.version + 1,
            rationale=normalized,
            evidence=(evidence,),
            risks=intent.risks,
        )
        if intent.action == "remove_explicit":
            changes.append(RegistryIncidentJoinRemoval(base=incident, decision=decision))
            continue
        witness = intent.profile_witness
        proposal = intent.proposal
        assert witness is not None and proposal is not None
        cardinality = classify_cardinality(witness.result.profile).cardinality
        fanout = (
            FanoutPolicy.REQUIRE_DISTINCT_FOR_LEFT_ENTITY_METRICS
            if cardinality is Cardinality.ONE_TO_MANY
            else FanoutPolicy.NONE
        )
        contract = JoinContract(
            id=incident.contract.id,
            left_key=proposal.left_key,
            right_key=proposal.right_key,
            cardinality=cardinality,
            default_join_type=proposal.default_join_type,
            fanout_policy=fanout,
            status=ApprovalStatus.APPROVED,
            version=incident.contract.version + 1,
            evidence=decision.evidence,
            risks=decision.risks,
            approval_decision_id=decision.id,
        )
        changes.append(
            RegistryIncidentJoinUpsert(
                base=incident,
                contract=contract,
                decision=decision,
                profile_witness=witness,
            )
        )
    return _build_draft(
        {
            **_draft_values(draft),
            "revision": 2,
            "status": RegistryModelChangeStatus.APPROVED,
            "outer_decision": outer_decision,
            "incident_changes": tuple(changes),
            "reviewed_by": actor_id,
            "reviewed_at": decided_at,
            "updated_at": decided_at,
        }
    )


def prepare_registry_model_change(
    draft: RegistryModelChangeDraft,
    *,
    prepared_by: str,
    prepared_at: datetime,
) -> tuple[RegistryModelChangeDraft, PreparedRegistryModelReplacementProposal]:
    if draft.status is not RegistryModelChangeStatus.APPROVED or draft.reviewed_by is None:
        raise ValueError("registry model draft is not approved")
    if draft.outer_decision is None:
        raise ValueError("registry model draft outer decision is unavailable")
    proposal_id = "registry-model-change-" + registry_model_authoring_fingerprint(
        {
            "draft_id": draft.id,
            "draft_revision": draft.revision,
            "draft_fingerprint": draft.fingerprint,
            "source_replacement_proposal_id": draft.source.proposal.id,
            "source_replacement_proposal_fingerprint": draft.source.proposal.fingerprint,
        }
    )
    proposal = PreparedRegistryModelReplacementProposal.create(
        id=proposal_id,
        draft_id=draft.id,
        draft_revision=draft.revision,
        draft_fingerprint=draft.fingerprint,
        owner_actor_id=draft.owner_actor_id,
        base=draft.base,
        replacement=draft.source.proposal,
        outer_decision=draft.outer_decision,
        model_decision=draft.model_decision,
        mapping_decisions=draft.mapping_decisions,
        authority=draft.authority,
        incident_join_changes=draft.incident_changes,
        risks=draft.risks,
        created_at=draft.created_at,
        prepared_by=prepared_by,
        prepared_at=prepared_at,
    )
    ready = _build_draft(
        {
            **_draft_values(draft),
            "revision": draft.revision + 1,
            "status": RegistryModelChangeStatus.READY_FOR_PUBLICATION,
            "prepared_proposal_id": proposal.id,
            "prepared_proposal_fingerprint": proposal.fingerprint,
            "prepared_by": prepared_by,
            "updated_at": prepared_at,
        }
    )
    return ready, proposal


def replacement_source_decisions(
    source: RegistryModelReplacementSourceEvidence,
    *,
    base: RegistryModelReplacementBase,
    model_risks: tuple[str, ...],
) -> tuple[DecisionRecord, tuple[DecisionRecord, ...]]:
    decisions = {item.id: item for item in source.decisions}
    proposal = source.proposal
    model_id = proposal.model.decision_id
    if model_id is None:
        raise ValueError("replacement model decision is unavailable")
    model_source = decisions[model_id]
    model = DecisionRecord(
        id=model_source.id,
        target_type=DecisionTargetType.LOGICAL_MODEL,
        target_id=proposal.model.definition.id.root,
        action=model_source.action,
        status=model_source.status,
        actor=model_source.actor_id,
        decided_at=model_source.decided_at,
        source_version=base.target_model.version,
        resulting_version=proposal.model.definition.version,
        rationale=model_source.rationale,
        evidence=tuple(_evidence_text(item) for item in model_source.evidence),
        risks=model_risks,
    )
    mappings: list[DecisionRecord] = []
    for mapping in proposal.mappings:
        decision_id = mapping.decision_id
        if decision_id is None or not mapping.risks:
            raise ValueError("replacement mapping lacks decision or explicit risk")
        source_decision = decisions[decision_id]
        if source_decision.target_kind is not SemanticOnboardingTargetKind.MAPPING:
            raise ValueError("replacement mapping decision kind is invalid")
        mappings.append(
            DecisionRecord(
                id=source_decision.id,
                target_type=DecisionTargetType.COLUMN_MAPPING,
                target_id=mapping.id,
                action=source_decision.action,
                status=source_decision.status,
                actor=source_decision.actor_id,
                decided_at=source_decision.decided_at,
                source_version=base.target_model.version,
                resulting_version=proposal.model.definition.version,
                rationale=source_decision.rationale,
                evidence=tuple(_evidence_text(item) for item in source_decision.evidence),
                risks=mapping.risks,
            )
        )
    return model, tuple(sorted(mappings, key=lambda item: item.target_id))


def registry_model_authoring_fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json(_jsonable(value))).hexdigest()


def _incident_decision_id(
    draft: RegistryModelChangeDraft,
    intent: RegistryModelIncidentIntent,
    actor_id: str,
) -> str:
    digest = registry_model_authoring_fingerprint(
        {
            "actor_id": actor_id,
            "action": intent.action,
            "draft_fingerprint": draft.fingerprint,
            "join_id": intent.base_join_id,
        }
    )
    return f"registry-model-decision-{digest}"


def _outer_decision_id(
    draft: RegistryModelChangeDraft,
    actor_id: str,
    action: DecisionAction,
) -> str:
    digest = registry_model_authoring_fingerprint(
        {
            "actor_id": actor_id,
            "action": action.value,
            "draft_fingerprint": draft.fingerprint,
            "target_model_id": draft.source.proposal.model.definition.id.root,
        }
    )
    return f"registry-model-outer-decision-{digest}"


def _evidence_text(value: OnboardingEvidence) -> str:
    suffix = "" if value.reference is None else f" [{value.reference}]"
    return f"{value.kind.value}: {value.detail}{suffix}"


def _build_draft(payload: dict[str, object]) -> RegistryModelChangeDraft:
    construct: Any = RegistryModelChangeDraft.model_construct
    provisional = construct(**payload, fingerprint="0" * 64)
    return RegistryModelChangeDraft(
        **payload,
        fingerprint=registry_model_authoring_fingerprint(
            provisional.model_dump(mode="json", exclude={"fingerprint"})
        ),
    )


def _draft_values(draft: RegistryModelChangeDraft) -> dict[str, object]:
    return {
        name: getattr(draft, name) for name in type(draft).model_fields if name != "fingerprint"
    }


def _jsonable(value: object) -> object:
    if isinstance(value, FrozenDomainModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256(value: str, label: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


__all__ = [
    "MAX_REGISTRY_MODEL_CHANGE_HISTORY",
    "CreateRegistryModelChangeInput",
    "RegistryIncidentJoinInput",
    "RegistryIncidentJoinPreserveInput",
    "RegistryIncidentJoinRemovalInput",
    "RegistryIncidentJoinUpsertInput",
    "RegistryModelChangeAuditEvent",
    "RegistryModelChangeAuditRecord",
    "RegistryModelChangeDraft",
    "RegistryModelChangeMutation",
    "RegistryModelChangeSnapshot",
    "RegistryModelChangeStatus",
    "RegistryModelIncidentIntent",
    "RegistryModelJoinProfileAuditEvent",
    "RegistryModelJoinProfileAuditRecord",
    "RegistryModelJoinProfileAuthoringRequest",
    "RegistryModelJoinProfileMutation",
    "RegistryModelJoinProfileRequest",
    "RegistryModelReplacementSourceEvidence",
    "RequestRegistryModelJoinProfileInput",
    "create_registry_model_change_draft",
    "decide_registry_model_change",
    "prepare_registry_model_change",
    "registry_model_authoring_fingerprint",
    "replacement_source_decisions",
]
