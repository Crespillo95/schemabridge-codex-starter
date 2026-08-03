"""Pure M35 contracts for replacing one model in an immutable registry-v2."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, TypeAlias

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.concepts import LogicalFieldRef, LogicalModelRef
from schemabridge.domain.decisions import (
    ApprovalStatus,
    DecisionAction,
    DecisionRecord,
    DecisionTargetType,
)
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.joins import (
    JoinContract,
    JoinProposal,
    NormalizedJoinKey,
    RelationshipProfile,
    classify_cardinality,
)
from schemabridge.domain.mappings import ColumnMapping
from schemabridge.domain.registry_changes import RegistryChangeProposalKind
from schemabridge.domain.registry_publication import (
    PublishableRegistryVersion,
    create_publishable_registry_version,
)
from schemabridge.domain.request_context import (
    ApprovedLogicalContext,
    ApprovedLogicalJoin,
    ApprovedRequestField,
    ApprovedRequestModel,
)
from schemabridge.domain.semantic_change import (
    GovernedJoinRef,
    GovernedMappingRef,
    SemanticChangeInspectionContext,
    SemanticChangeReport,
    SemanticChangeSeverity,
    SemanticChangeStatus,
    SemanticImpactSet,
    SemanticImpactSummary,
)
from schemabridge.domain.semantic_onboarding import (
    OnboardingEvidence,
    OnboardingRegistryBase,
    PreparedSemanticOnboardingProposal,
    SemanticMappingProposal,
)
from schemabridge.domain.semantic_profile_jobs import (
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
_MAX_PROFILE_AGE = timedelta(hours=1)
_MIN_PROFILE_TIMEOUT_MS = 100
_MAX_PROFILE_TIMEOUT_MS = 60_000
_MAX_PROPOSAL_BYTES = 2 * 1024 * 1024


class RegistryModelChangeKind(StrEnum):
    PLANNED_CHANGE = "planned_change"
    M26_REMEDIATION = "m26_remediation"


class RegistryIncidentJoinAction(StrEnum):
    PRESERVE_EXACT = "preserve_exact"
    UPSERT_FRESH = "upsert_fresh"
    REMOVE_EXPLICIT = "remove_explicit"


class RegistryPhysicalMeaning(FrozenDomainModel):
    """Compact proof that an unaffected physical coordinate keeps one meaning."""

    physical_field: PhysicalFieldRef
    logical_field: LogicalFieldRef


class RegistryModelJoinEndpointEvidence(FrozenDomainModel):
    """One candidate or active mapping/binding behind an incident join endpoint."""

    key: NormalizedJoinKey
    mapping: GovernedFieldMapping
    binding: GovernedPhysicalBinding
    fingerprint: str

    @field_validator("fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "registry model join endpoint fingerprint")

    @model_validator(mode="after")
    def endpoint_must_match_mapping_and_binding(
        self,
    ) -> RegistryModelJoinEndpointEvidence:
        mapped = self.mapping.mapping
        if (
            self.key.logical_field != mapped.logical_field
            or self.key.physical_field != mapped.physical_field
            or self.key.transformation_plan != mapped.transformation_plan
            or self.binding.logical_field != mapped.logical_field
            or self.binding.physical_field != mapped.physical_field
            or self.binding.physical_type is not self.mapping.physical_type
            or self.fingerprint != registry_model_change_fingerprint(_endpoint_payload(self))
        ):
            raise ValueError("registry model join endpoint authority does not match")
        return self

    @classmethod
    def create(
        cls,
        *,
        key: NormalizedJoinKey,
        mapping: GovernedFieldMapping,
        binding: GovernedPhysicalBinding,
    ) -> RegistryModelJoinEndpointEvidence:
        values = {"key": key, "mapping": mapping, "binding": binding}
        construct: Any = cls.model_construct
        provisional = construct(**values, fingerprint="0" * 64)
        return cls(
            **values,
            fingerprint=registry_model_change_fingerprint(_endpoint_payload(provisional)),
        )


class RegistryIncidentJoinBase(FrozenDomainModel):
    """Exact active logical and physical representation of one incident join."""

    summary: ApprovedLogicalJoin
    contract: JoinContract
    left: RegistryModelJoinEndpointEvidence
    right: RegistryModelJoinEndpointEvidence
    fingerprint: str

    @field_validator("fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "registry incident join base fingerprint")

    @model_validator(mode="after")
    def logical_and_physical_join_must_match(self) -> RegistryIncidentJoinBase:
        contract = self.contract
        left_model = contract.left_key.logical_field.root.split(".", 1)[0]
        right_model = contract.right_key.logical_field.root.split(".", 1)[0]
        if (
            self.left.key != contract.left_key
            or self.right.key != contract.right_key
            or self.summary.id != contract.id
            or self.summary.left_model.root != left_model
            or self.summary.right_model.root != right_model
            or self.summary.cardinality is not contract.cardinality
            or self.summary.fanout_policy is not contract.fanout_policy
            or self.summary.version != contract.version
            or self.summary.approval_decision_id != contract.approval_decision_id
            or self.fingerprint != registry_model_change_fingerprint(_incident_base_payload(self))
        ):
            raise ValueError("registry incident join base is inconsistent")
        return self

    @classmethod
    def create(
        cls,
        *,
        summary: ApprovedLogicalJoin,
        contract: JoinContract,
        left: RegistryModelJoinEndpointEvidence,
        right: RegistryModelJoinEndpointEvidence,
    ) -> RegistryIncidentJoinBase:
        values = {
            "summary": summary,
            "contract": contract,
            "left": left,
            "right": right,
        }
        construct: Any = cls.model_construct
        provisional = construct(**values, fingerprint="0" * 64)
        return cls(
            **values,
            fingerprint=registry_model_change_fingerprint(_incident_base_payload(provisional)),
        )


class RegistryModelReplacementBase(FrozenDomainModel):
    """Exact active model slice plus complete dependency and incident-join authority."""

    scope: SemanticRegistryScope
    base_registry: OnboardingRegistryBase
    target_model: ApprovedRequestModel
    target_mappings: tuple[GovernedFieldMapping, ...] = Field(min_length=1, max_length=2_000)
    target_bindings: tuple[GovernedPhysicalBinding, ...] = Field(
        min_length=1,
        max_length=2_000,
    )
    unaffected_physical_meanings: tuple[RegistryPhysicalMeaning, ...] = Field(
        default=(),
        max_length=2_000,
    )
    incident_joins: tuple[RegistryIncidentJoinBase, ...] = Field(default=(), max_length=500)
    dependency_context: SemanticChangeInspectionContext
    active_decision_ids: tuple[str, ...] = Field(min_length=1, max_length=2_000)
    fingerprint: str

    @field_validator("active_decision_ids")
    @classmethod
    def decisions_must_be_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))) or any(not value.strip() for value in values):
            raise ValueError("registry model base decisions must be sorted and unique")
        return values

    @field_validator("fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "registry model replacement base fingerprint")

    @model_validator(mode="after")
    def base_slice_must_be_complete_and_canonical(
        self,
    ) -> RegistryModelReplacementBase:
        model_id = self.target_model.id.root
        mapping_pairs = tuple(_mapping_pair(item) for item in self.target_mappings)
        binding_pairs = tuple(item.mapping_identity for item in self.target_bindings)
        target_fields = {field.id.root for field in self.target_model.fields}
        mapped_fields = {item.mapping.logical_field.root for item in self.target_mappings}
        meaning_pairs = tuple(
            (item.physical_field.root, item.logical_field.root)
            for item in self.unaffected_physical_meanings
        )
        incident_ids = tuple(item.contract.id for item in self.incident_joins)
        dependency = self.dependency_context
        if (
            self.base_registry.registry_version is None
            or self.base_registry.registry_fingerprint is None
            or self.base_registry.activation_generation is None
            or self.base_registry.active_pointer_fingerprint is None
            or mapping_pairs != tuple(sorted(set(mapping_pairs)))
            or binding_pairs != tuple(sorted(set(binding_pairs)))
            or set(mapping_pairs) != set(binding_pairs)
            or mapped_fields != target_fields
            or any(
                item.mapping.logical_field.root.split(".", 1)[0] != model_id
                for item in self.target_mappings
            )
            or meaning_pairs != tuple(sorted(set(meaning_pairs)))
            or incident_ids != tuple(sorted(set(incident_ids)))
            or any(
                model_id
                not in {
                    item.summary.left_model.root,
                    item.summary.right_model.root,
                }
                for item in self.incident_joins
            )
            or dependency.scope != self.scope
            or not dependency.dependency_index.complete
            or dependency.pointer_generation != self.base_registry.activation_generation
            or dependency.pointer_fingerprint != self.base_registry.active_pointer_fingerprint
            or dependency.registry_version != self.base_registry.registry_version
            or dependency.registry_fingerprint != self.base_registry.registry_fingerprint
            or self.fingerprint
            != registry_model_change_fingerprint(_replacement_base_payload(self))
        ):
            raise ValueError("registry model replacement base is incomplete or stale")
        return self


def resolve_registry_model_replacement_base(
    *,
    scope: SemanticRegistryScope,
    base: GovernedSemanticRegistrySnapshot,
    base_registry: OnboardingRegistryBase,
    target_model_id: LogicalModelRef,
    dependency_context: SemanticChangeInspectionContext,
) -> RegistryModelReplacementBase:
    """Resolve the exact active model and all incident joins before authoring."""

    expected_source = f"datahub:{datahub_registry_document_id(scope, base.version)}"
    if (
        base.format_version != 2
        or base.registry_id != scope.registry_id
        or base.catalog_scope != scope.catalog_scope
        or base_registry.registry_version != base.version
        or base_registry.registry_fingerprint != base.fingerprint
        or base.source != expected_source
        or base.logical_context.source != f"{expected_source}/logical-context"
        or any(binding.workspace_id != scope.workspace_id for binding in base.physical_bindings)
    ):
        raise ValueError("registry model change requires the exact active registry-v2 base")
    model = base.logical_context.model_index().get(target_model_id.root)
    if model is None:
        raise ValueError("registry model change target is not active")
    expected_mapping_refs = tuple(
        sorted(
            (_governed_mapping_ref(item) for item in base.mapping_set.mappings),
            key=lambda item: item.identity,
        )
    )
    expected_join_refs = tuple(
        sorted(
            (_governed_join_ref(item) for item in base.join_contracts.contracts),
            key=lambda item: item.identity,
        )
    )
    if (
        dependency_context.scope != scope
        or not dependency_context.dependency_index.complete
        or dependency_context.pointer_generation != base_registry.activation_generation
        or dependency_context.pointer_fingerprint != base_registry.active_pointer_fingerprint
        or dependency_context.registry_version != base.version
        or dependency_context.registry_fingerprint != base.fingerprint
        or dependency_context.mappings != expected_mapping_refs
        or dependency_context.joins != expected_join_refs
    ):
        raise ValueError("registry model change dependency snapshot is incomplete or stale")

    target_mappings = tuple(
        sorted(
            (
                item
                for item in base.mapping_set.mappings
                if item.mapping.logical_field.root.split(".", 1)[0] == target_model_id.root
            ),
            key=_mapping_pair,
        )
    )
    target_pairs = {_mapping_pair(item) for item in target_mappings}
    target_bindings = tuple(
        sorted(
            (item for item in base.physical_bindings if item.mapping_identity in target_pairs),
            key=lambda item: item.mapping_identity,
        )
    )
    unaffected_meanings = tuple(
        sorted(
            (
                RegistryPhysicalMeaning(
                    physical_field=item.mapping.physical_field,
                    logical_field=item.mapping.logical_field,
                )
                for item in base.mapping_set.mappings
                if item.mapping.logical_field.root.split(".", 1)[0] != target_model_id.root
            ),
            key=lambda item: (item.physical_field.root, item.logical_field.root),
        )
    )
    summaries = {item.id: item for item in base.logical_context.joins}
    incidents: list[RegistryIncidentJoinBase] = []
    for contract in base.join_contracts.contracts:
        endpoint_models = {
            contract.left_key.logical_field.root.split(".", 1)[0],
            contract.right_key.logical_field.root.split(".", 1)[0],
        }
        if target_model_id.root not in endpoint_models:
            continue
        incidents.append(
            RegistryIncidentJoinBase.create(
                summary=summaries[contract.id],
                contract=contract,
                left=_endpoint_from_registry(base, contract.left_key),
                right=_endpoint_from_registry(base, contract.right_key),
            )
        )
    values = {
        "scope": scope,
        "base_registry": base_registry,
        "target_model": model,
        "target_mappings": target_mappings,
        "target_bindings": target_bindings,
        "unaffected_physical_meanings": unaffected_meanings,
        "incident_joins": tuple(sorted(incidents, key=lambda item: item.contract.id)),
        "dependency_context": dependency_context,
        "active_decision_ids": semantic_registry_decision_ids(base),
    }
    construct: Any = RegistryModelReplacementBase.model_construct
    provisional = construct(**values, fingerprint="0" * 64)
    return RegistryModelReplacementBase(
        **values,
        fingerprint=registry_model_change_fingerprint(_replacement_base_payload(provisional)),
    )


class RegistryModelChangeAuthority(FrozenDomainModel):
    """Closed planned/remediation authority over one complete M26 dependency snapshot."""

    kind: RegistryModelChangeKind
    base_fingerprint: str
    report: SemanticChangeReport | None = None
    impacts: SemanticImpactSet | None = None
    resolved_finding_ids: tuple[str, ...] = Field(default=(), max_length=2_000)
    fingerprint: str

    @field_validator("base_fingerprint", "fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "registry model change authority fingerprint")

    @field_validator("resolved_finding_ids")
    @classmethod
    def findings_must_be_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))):
            raise ValueError("resolved registry findings must be sorted and unique")
        return values

    @model_validator(mode="after")
    def authority_shape_must_be_closed(self) -> RegistryModelChangeAuthority:
        if self.kind is RegistryModelChangeKind.PLANNED_CHANGE:
            if self.report is not None or self.impacts is not None or self.resolved_finding_ids:
                raise ValueError("planned registry change cannot claim M26 remediation")
        elif self.report is None or self.impacts is None:
            raise ValueError("M26 remediation requires one report and complete impact set")
        if self.fingerprint != registry_model_change_fingerprint(_authority_payload(self)):
            raise ValueError("registry model change authority fingerprint does not match")
        return self

    @classmethod
    def planned(
        cls,
        base: RegistryModelReplacementBase,
    ) -> RegistryModelChangeAuthority:
        return _build_authority(
            kind=RegistryModelChangeKind.PLANNED_CHANGE,
            base=base,
            report=None,
            impacts=None,
            resolved_finding_ids=(),
        )

    @classmethod
    def remediation(
        cls,
        base: RegistryModelReplacementBase,
        *,
        report: SemanticChangeReport,
        impacts: SemanticImpactSet,
        resolved_finding_ids: tuple[str, ...],
    ) -> RegistryModelChangeAuthority:
        return _build_authority(
            kind=RegistryModelChangeKind.M26_REMEDIATION,
            base=base,
            report=report,
            impacts=impacts,
            resolved_finding_ids=tuple(sorted(set(resolved_finding_ids))),
        )


class RegistryModelJoinProfileWitness(FrozenDomainModel):
    """Fresh aggregate-only profile bound to candidate endpoints and one exact change."""

    change_id: str = Field(min_length=3, max_length=200)
    source_replacement_proposal_id: str = Field(min_length=3, max_length=200)
    scope: SemanticRegistryScope
    base_registry: OnboardingRegistryBase
    base_fingerprint: str
    target_model_id: LogicalModelRef
    target_model_version: int = Field(ge=2)
    connection_id: CatalogConnectionId
    catalog_generation_vector: tuple[tuple[int, str], ...] = Field(
        min_length=1,
        max_length=2,
    )
    proposal: JoinProposal
    left: RegistryModelJoinEndpointEvidence
    right: RegistryModelJoinEndpointEvidence
    execution_target: SemanticJoinProfileTargetRef
    result: SemanticJoinProfileResult
    expires_at: datetime
    fingerprint: str

    @field_validator("change_id", "source_replacement_proposal_id")
    @classmethod
    def id_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("registry model join witness change id is invalid")
        return value

    @field_validator("expires_at")
    @classmethod
    def expiry_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "registry model join witness expiry")

    @field_validator("base_fingerprint", "fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "registry model join witness fingerprint")

    @model_validator(mode="after")
    def witness_must_be_exact_read_only_and_fresh(
        self,
    ) -> RegistryModelJoinProfileWitness:
        profile = self.result.profile
        endpoint_models = {
            self.left.key.logical_field.root.split(".", 1)[0],
            self.right.key.logical_field.root.split(".", 1)[0],
        }
        bindings = (self.left.binding, self.right.binding)
        expected_vector = _catalog_vector(bindings)
        if (
            self.base_registry.registry_version is None
            or self.change_id == self.source_replacement_proposal_id
            or self.proposal.left_key != self.left.key
            or self.proposal.right_key != self.right.key
            or self.target_model_id.root not in endpoint_models
            or len(endpoint_models) != 2
            or any(binding.workspace_id != self.scope.workspace_id for binding in bindings)
            or any(binding.catalog_scope != self.scope.catalog_scope for binding in bindings)
            or any(binding.connection_id != self.connection_id for binding in bindings)
            or self.execution_target.workspace_id != self.scope.workspace_id
            or self.execution_target.connection_id != self.connection_id
            or self.catalog_generation_vector != expected_vector
            or profile.reader_user is None
            or not profile.reader_user.strip()
            or profile.transaction_read_only is not True
            or profile.statement_timeout_ms is None
            or not _MIN_PROFILE_TIMEOUT_MS
            <= profile.statement_timeout_ms
            <= _MAX_PROFILE_TIMEOUT_MS
            or self.expires_at != self.result.completed_at + _MAX_PROFILE_AGE
            or self.fingerprint != registry_model_change_fingerprint(_profile_witness_payload(self))
        ):
            raise ValueError("registry model join witness authority is incomplete or unsafe")
        return self

    def is_current(self, at: datetime) -> bool:
        instant = _aware(at, "registry model join witness current time")
        return self.result.completed_at <= instant < self.expires_at

    @classmethod
    def create(
        cls,
        *,
        change_id: str,
        base: RegistryModelReplacementBase,
        replacement: PreparedSemanticOnboardingProposal,
        incident: RegistryIncidentJoinBase,
        proposal: JoinProposal,
        execution_target: SemanticJoinProfileTargetRef,
        profile: RelationshipProfile,
        completed_at: datetime,
    ) -> RegistryModelJoinProfileWitness:
        if incident.contract.id != proposal.id:
            raise ValueError("registry model join witness targets another join")
        model, mappings, bindings = _replacement_artifacts(replacement)
        if model.id != base.target_model.id:
            raise ValueError("registry model join witness targets another replacement model")
        left = _candidate_endpoint(
            key=proposal.left_key,
            base=base,
            incident=incident,
            replacement_mappings=mappings,
            replacement_bindings=bindings,
        )
        right = _candidate_endpoint(
            key=proposal.right_key,
            base=base,
            incident=incident,
            replacement_mappings=mappings,
            replacement_bindings=bindings,
        )
        result = SemanticJoinProfileResult.create(profile, completed_at=completed_at)
        values = {
            "change_id": change_id,
            "source_replacement_proposal_id": replacement.id,
            "scope": base.scope,
            "base_registry": base.base_registry,
            "base_fingerprint": base.fingerprint,
            "target_model_id": model.id,
            "target_model_version": model.version,
            "connection_id": left.binding.connection_id,
            "catalog_generation_vector": _catalog_vector((left.binding, right.binding)),
            "proposal": proposal,
            "left": left,
            "right": right,
            "execution_target": execution_target,
            "result": result,
            "expires_at": completed_at + _MAX_PROFILE_AGE,
        }
        construct: Any = cls.model_construct
        provisional = construct(**values, fingerprint="0" * 64)
        return cls(
            **values,
            fingerprint=registry_model_change_fingerprint(_profile_witness_payload(provisional)),
        )


class RegistryIncidentJoinPreservation(FrozenDomainModel):
    action: Literal["preserve_exact"] = "preserve_exact"
    base: RegistryIncidentJoinBase


class RegistryIncidentJoinRemoval(FrozenDomainModel):
    action: Literal["remove_explicit"] = "remove_explicit"
    base: RegistryIncidentJoinBase
    decision: DecisionRecord

    @model_validator(mode="after")
    def removal_must_be_an_explicit_new_rejection(self) -> RegistryIncidentJoinRemoval:
        contract = self.base.contract
        if (
            self.decision.target_type is not DecisionTargetType.JOIN_CONTRACT
            or self.decision.target_id != contract.id
            or self.decision.action is not DecisionAction.REJECT
            or self.decision.status is not ApprovalStatus.REJECTED
            or self.decision.source_version != contract.version
            or self.decision.resulting_version != contract.version + 1
            or self.decision.id == contract.approval_decision_id
            or len(self.decision.rationale.strip()) < 12
            or not self.decision.evidence
            or not self.decision.risks
        ):
            raise ValueError("registry incident join removal lacks an explicit decision")
        return self


class RegistryIncidentJoinUpsert(FrozenDomainModel):
    action: Literal["upsert_fresh"] = "upsert_fresh"
    base: RegistryIncidentJoinBase
    contract: JoinContract
    decision: DecisionRecord
    profile_witness: RegistryModelJoinProfileWitness

    @model_validator(mode="after")
    def upsert_must_have_fresh_profile_and_new_approval(
        self,
    ) -> RegistryIncidentJoinUpsert:
        prior = self.base.contract
        observed = classify_cardinality(self.profile_witness.result.profile).cardinality
        if (
            self.contract.id != prior.id
            or self.contract.version != prior.version + 1
            or self.contract.left_key != self.profile_witness.proposal.left_key
            or self.contract.right_key != self.profile_witness.proposal.right_key
            or self.contract.default_join_type
            is not self.profile_witness.proposal.default_join_type
            or self.contract.cardinality is not observed
            or self.contract.status is not ApprovalStatus.APPROVED
            or self.contract.approval_decision_id != self.decision.id
            or self.decision.target_type is not DecisionTargetType.JOIN_CONTRACT
            or self.decision.target_id != prior.id
            or self.decision.action is not DecisionAction.APPROVE
            or self.decision.status is not ApprovalStatus.APPROVED
            or self.decision.source_version != prior.version
            or self.decision.resulting_version != self.contract.version
            or self.decision.id == prior.approval_decision_id
            or self.decision.evidence != self.contract.evidence
            or self.decision.risks != self.contract.risks
            or len(self.decision.rationale.strip()) < 12
            or not self.decision.evidence
            or not self.decision.risks
            or self.decision.decided_at < self.profile_witness.result.completed_at
            or not self.decision.decided_at < self.profile_witness.expires_at
        ):
            raise ValueError("registry incident join upsert lacks fresh approved evidence")
        return self


RegistryIncidentJoinChange: TypeAlias = (
    RegistryIncidentJoinPreservation | RegistryIncidentJoinRemoval | RegistryIncidentJoinUpsert
)


class PreparedRegistryModelReplacementProposal(FrozenDomainModel):
    """Immutable, non-executable Phase-B delta consumed by registry publication."""

    proposal_kind: Literal["replace_model_v1"] = "replace_model_v1"
    id: str = Field(min_length=3, max_length=200)
    draft_id: str = Field(min_length=3, max_length=80)
    draft_revision: int = Field(ge=2)
    draft_fingerprint: str
    source_replacement_proposal_id: str = Field(min_length=3, max_length=200)
    source_replacement_proposal_fingerprint: str
    workspace_id: str = Field(min_length=3, max_length=200)
    owner_actor_id: str = Field(min_length=1, max_length=200)
    scope: SemanticRegistryScope
    base: RegistryModelReplacementBase
    replacement: PreparedSemanticOnboardingProposal
    outer_decision: DecisionRecord
    model_decision: DecisionRecord
    mapping_decisions: tuple[DecisionRecord, ...] = Field(min_length=1, max_length=2_000)
    authority: RegistryModelChangeAuthority
    incident_join_changes: tuple[RegistryIncidentJoinChange, ...] = Field(
        default=(),
        max_length=500,
    )
    risks: tuple[str, ...] = Field(min_length=1, max_length=100)
    decision_ids: tuple[str, ...] = Field(min_length=2, max_length=2_500)
    created_at: datetime
    prepared_by: str = Field(min_length=1, max_length=200)
    prepared_at: datetime
    external_writes_performed: Literal[False] = False
    fingerprint: str

    @property
    def base_registry(self) -> OnboardingRegistryBase:
        """Expose the common immutable-publication base contract."""

        return self.base.base_registry

    @property
    def target_registry_version(self) -> int:
        """Expose the common immutable-publication target contract."""

        return self.replacement.target_registry_version

    @field_validator("id", "draft_id")
    @classmethod
    def id_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("registry model replacement id is invalid")
        return value

    @field_validator("workspace_id", "owner_actor_id", "prepared_by")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("registry model replacement text must not be blank")
        return value

    @field_validator("risks")
    @classmethod
    def risks_must_be_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))) or any(not item.strip() for item in values):
            raise ValueError("registry model replacement risks must be sorted and unique")
        return values

    @field_validator("decision_ids")
    @classmethod
    def decisions_must_be_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))) or any(not item.strip() for item in values):
            raise ValueError("registry model replacement decisions must be sorted and unique")
        return values

    @field_validator("created_at", "prepared_at")
    @classmethod
    def times_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "registry model replacement timestamp")

    @field_validator(
        "draft_fingerprint",
        "source_replacement_proposal_fingerprint",
        "fingerprint",
    )
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "registry model replacement fingerprint")

    @model_validator(mode="after")
    def proposal_must_be_complete_and_fingerprinted(
        self,
    ) -> PreparedRegistryModelReplacementProposal:
        model, mappings, bindings = _replacement_artifacts(self.replacement)
        mapping_proposals = {item.id: item for item in self.replacement.mappings}
        mapping_decisions = {item.target_id: item for item in self.mapping_decisions}
        incident_index = {item.contract.id: item for item in self.base.incident_joins}
        change_index = {item.base.contract.id: item for item in self.incident_join_changes}
        change_ids = tuple(item.base.contract.id for item in self.incident_join_changes)
        incident_decisions = tuple(
            item.decision
            for item in self.incident_join_changes
            if isinstance(
                item,
                (RegistryIncidentJoinRemoval, RegistryIncidentJoinUpsert),
            )
        )
        source_decisions = (self.model_decision, *self.mapping_decisions)
        change_decisions = (self.outer_decision, *incident_decisions)
        all_decisions = (*source_decisions, *change_decisions)
        expected_decision_ids = tuple(sorted(item.id for item in all_decisions))
        field_ids = {field.id.root for field in model.fields}
        base_fields = {field.id.root: field for field in self.base.target_model.fields}
        mapped_fields = {item.mapping.logical_field.root for item in mappings}
        mapping_pairs = tuple(_mapping_pair(item) for item in mappings)
        binding_pairs = tuple(item.mapping_identity for item in bindings)
        base_connections = {item.connection_id for item in self.base.target_bindings}
        replacement_connections = {item.connection_id for item in bindings}
        if (
            self.proposal_kind != RegistryChangeProposalKind.REPLACE_MODEL_V1.value
            or self.id == self.replacement.id
            or self.source_replacement_proposal_id != self.replacement.id
            or self.source_replacement_proposal_fingerprint != self.replacement.fingerprint
            or self.workspace_id != self.scope.workspace_id
            or self.workspace_id != self.replacement.workspace_id
            or self.owner_actor_id == self.prepared_by
            or self.owner_actor_id == self.replacement.prepared_by
            or self.outer_decision.actor == self.owner_actor_id
            or self.outer_decision.actor == self.replacement.prepared_by
            or self.prepared_by == self.replacement.prepared_by
            or self.prepared_at < self.replacement.prepared_at
            or self.created_at > self.prepared_at
            or self.scope != self.replacement.scope
            or self.replacement.base_registry != self.base.base_registry
            or self.replacement.target_registry_version
            != self.base.base_registry.next_registry_version
            or model.id != self.base.target_model.id
            or model.version != self.base.target_model.version + 1
            or any(
                field.id.root in base_fields
                and field.version != base_fields[field.id.root].version + 1
                for field in model.fields
            )
            or mapped_fields != field_ids
            or len(mapping_pairs) != len(set(mapping_pairs))
            or set(mapping_pairs) != set(binding_pairs)
            or len(bindings) != len(binding_pairs)
            or set(change_index) != set(incident_index)
            or len(change_index) != len(self.incident_join_changes)
            or change_ids != tuple(sorted(set(change_ids)))
            or any(
                change.base != incident_index[join_id] for join_id, change in change_index.items()
            )
            or len(base_connections) != 1
            or replacement_connections != base_connections
            or self.authority.base_fingerprint != self.base.fingerprint
            or expected_decision_ids != self.decision_ids
            or set(self.decision_ids) & set(self.base.active_decision_ids)
            or any(decision.actor == self.prepared_by for decision in all_decisions)
            or self.outer_decision.target_type is not DecisionTargetType.LOGICAL_MODEL
            or self.outer_decision.target_id != model.id.root
            or self.outer_decision.action is not DecisionAction.APPROVE
            or self.outer_decision.status is not ApprovalStatus.APPROVED
            or self.outer_decision.source_version != self.base.target_model.version
            or self.outer_decision.resulting_version != model.version
            or len(self.outer_decision.rationale.strip()) < 12
            or not self.outer_decision.evidence
            or not self.outer_decision.risks
            or self.outer_decision.id in {item.id for item in source_decisions}
            or self.outer_decision.actor in {item.actor for item in source_decisions}
            or any(
                decision.decided_at > self.replacement.prepared_at for decision in source_decisions
            )
            or any(
                not self.created_at <= decision.decided_at <= self.prepared_at
                for decision in change_decisions
            )
            or self.model_decision.target_type is not DecisionTargetType.LOGICAL_MODEL
            or self.model_decision.target_id != model.id.root
            or self.model_decision.action is not DecisionAction.APPROVE
            or self.model_decision.status is not ApprovalStatus.APPROVED
            or self.model_decision.source_version != self.base.target_model.version
            or self.model_decision.resulting_version != model.version
            or self.model_decision.id != self.replacement.model.decision_id
            or self.model_decision.actor != self.replacement.model.decided_by
            or len(self.model_decision.rationale.strip()) < 12
            or not self.model_decision.evidence
            or not self.model_decision.risks
            or set(mapping_decisions) != set(mapping_proposals)
            or len(mapping_decisions) != len(self.mapping_decisions)
            or any(
                not _mapping_decision_matches(
                    mapping_proposals[target_id],
                    decision,
                    source_version=self.base.target_model.version,
                    resulting_version=model.version,
                )
                for target_id, decision in mapping_decisions.items()
            )
            or any(not item.risks for item in self.replacement.mappings)
            or self.fingerprint
            != registry_model_change_fingerprint(
                self.model_dump(mode="json", exclude={"fingerprint"})
            )
        ):
            raise ValueError("prepared registry model replacement is incomplete or stale")
        _validate_candidate_physical_meanings(self.base, mappings)
        _validate_incident_join_changes(
            proposal=self,
            mappings=mappings,
            bindings=bindings,
        )
        _validate_change_authority(self)
        if len(_canonical_json(self.model_dump(mode="json"))) > _MAX_PROPOSAL_BYTES:
            raise ValueError("prepared registry model replacement exceeds its byte limit")
        return self

    def is_current(self, at: datetime) -> bool:
        """Require every candidate-only relationship witness to remain fresh."""

        instant = _aware(at, "registry model replacement current time")
        if instant < self.prepared_at:
            return False
        return all(
            not isinstance(item, RegistryIncidentJoinUpsert)
            or item.profile_witness.is_current(instant)
            for item in self.incident_join_changes
        )

    @classmethod
    def create(
        cls,
        *,
        id: str,
        draft_id: str,
        draft_revision: int,
        draft_fingerprint: str,
        owner_actor_id: str,
        base: RegistryModelReplacementBase,
        replacement: PreparedSemanticOnboardingProposal,
        outer_decision: DecisionRecord,
        model_decision: DecisionRecord,
        mapping_decisions: tuple[DecisionRecord, ...],
        authority: RegistryModelChangeAuthority,
        incident_join_changes: tuple[RegistryIncidentJoinChange, ...],
        risks: tuple[str, ...],
        created_at: datetime,
        prepared_by: str,
        prepared_at: datetime,
    ) -> PreparedRegistryModelReplacementProposal:
        change_decisions = tuple(
            item.decision
            for item in incident_join_changes
            if isinstance(
                item,
                (RegistryIncidentJoinRemoval, RegistryIncidentJoinUpsert),
            )
        )
        decision_ids = tuple(
            sorted(
                item.id
                for item in (
                    outer_decision,
                    model_decision,
                    *mapping_decisions,
                    *change_decisions,
                )
            )
        )
        values = {
            "proposal_kind": RegistryChangeProposalKind.REPLACE_MODEL_V1.value,
            "id": id,
            "draft_id": draft_id,
            "draft_revision": draft_revision,
            "draft_fingerprint": draft_fingerprint,
            "source_replacement_proposal_id": replacement.id,
            "source_replacement_proposal_fingerprint": replacement.fingerprint,
            "workspace_id": replacement.workspace_id,
            "owner_actor_id": owner_actor_id,
            "scope": replacement.scope,
            "base": base,
            "replacement": replacement,
            "outer_decision": outer_decision,
            "model_decision": model_decision,
            "mapping_decisions": mapping_decisions,
            "authority": authority,
            "incident_join_changes": incident_join_changes,
            "risks": tuple(sorted(set(risks))),
            "decision_ids": decision_ids,
            "created_at": created_at,
            "prepared_by": prepared_by,
            "prepared_at": prepared_at,
            "external_writes_performed": False,
        }
        construct: Any = cls.model_construct
        provisional = construct(**values, fingerprint="0" * 64)
        return cls(
            **values,
            fingerprint=registry_model_change_fingerprint(
                provisional.model_dump(mode="json", exclude={"fingerprint"})
            ),
        )


def assemble_model_replacement_registry_version(
    proposal: PreparedRegistryModelReplacementProposal,
    *,
    base: GovernedSemanticRegistrySnapshot,
) -> PublishableRegistryVersion:
    """Assemble one exact successor while retaining every unaffected artifact byte-for-byte."""

    _validate_current_base(proposal, base)
    model, replacement_mappings, replacement_bindings = _replacement_artifacts(proposal.replacement)
    target_model_id = proposal.base.target_model.id.root
    changes = {item.base.contract.id: item for item in proposal.incident_join_changes}

    models = tuple(
        model if item.id.root == target_model_id else item for item in base.logical_context.models
    )
    mappings = _replace_target_sequence(
        base.mapping_set.mappings,
        replacement_mappings,
        target_model_id=target_model_id,
        model_of=lambda item: item.mapping.logical_field.root.split(".", 1)[0],
    )
    bindings = _replace_target_sequence(
        base.physical_bindings,
        replacement_bindings,
        target_model_id=target_model_id,
        model_of=lambda item: item.logical_field.root.split(".", 1)[0],
    )
    logical_joins: list[ApprovedLogicalJoin] = []
    contracts: list[JoinContract] = []
    logical_by_id = {item.id: item for item in base.logical_context.joins}
    for contract in base.join_contracts.contracts:
        change = changes.get(contract.id)
        if change is None or isinstance(change, RegistryIncidentJoinPreservation):
            contracts.append(contract)
            logical_joins.append(logical_by_id[contract.id])
        elif isinstance(change, RegistryIncidentJoinUpsert):
            contracts.append(change.contract)
            logical_joins.append(_logical_join(change.contract))

    source = f"registry-model-change:{proposal.id}"
    base_provenance = {item.kind: item for item in base.provenance}
    registry = GovernedSemanticRegistrySnapshot(
        format_version=2,
        registry_id=base.registry_id,
        version=proposal.replacement.target_registry_version,
        source=source,
        catalog_scope=base.catalog_scope,
        logical_context=ApprovedLogicalContext(
            version=proposal.replacement.target_registry_version,
            source=f"{source}/logical-context",
            models=models,
            joins=tuple(logical_joins),
        ),
        mapping_set=GovernedMappingRegistry(
            version=proposal.replacement.target_registry_version,
            mappings=mappings,
        ),
        join_contracts=GovernedJoinRegistry(
            version=proposal.replacement.target_registry_version,
            contracts=tuple(contracts),
        ),
        provenance=(
            RegistryArtifactProvenance(
                kind=RegistryArtifactKind.LOGICAL_MODELS,
                source=f"{source}/{RegistryArtifactKind.LOGICAL_MODELS.value}",
                decision_ids=tuple(
                    sorted(
                        {
                            *base_provenance[RegistryArtifactKind.LOGICAL_MODELS].decision_ids,
                            proposal.model_decision.id,
                        }
                    )
                ),
            ),
            RegistryArtifactProvenance(
                kind=RegistryArtifactKind.PHYSICAL_MAPPINGS,
                source=f"{source}/{RegistryArtifactKind.PHYSICAL_MAPPINGS.value}",
                decision_ids=tuple(
                    sorted(
                        item.approval_decision_id
                        for item in mappings
                        if item.approval_decision_id is not None
                    )
                ),
            ),
            RegistryArtifactProvenance(
                kind=RegistryArtifactKind.JOIN_CONTRACTS,
                source=f"{source}/{RegistryArtifactKind.JOIN_CONTRACTS.value}",
                decision_ids=tuple(
                    sorted(
                        item.approval_decision_id
                        for item in contracts
                        if item.approval_decision_id is not None
                    )
                ),
            ),
        ),
        physical_bindings=bindings,
    )
    registry = prepare_datahub_registry_version(registry, proposal.scope)
    return create_publishable_registry_version(
        scope=proposal.scope,
        source_proposal_id=proposal.id,
        source_proposal_fingerprint=proposal.fingerprint,
        base_registry=proposal.base.base_registry,
        registry=registry,
        review_decision_ids=tuple(
            sorted({*semantic_registry_decision_ids(registry), *proposal.decision_ids})
        ),
    )


def registry_model_replacement_artifacts(
    replacement: PreparedSemanticOnboardingProposal,
) -> tuple[
    ApprovedRequestModel,
    tuple[GovernedFieldMapping, ...],
    tuple[GovernedPhysicalBinding, ...],
]:
    """Expose the exact typed candidate artifacts for application revalidation."""

    return _replacement_artifacts(replacement)


def resolve_registry_model_join_profile_endpoints(
    *,
    base: RegistryModelReplacementBase,
    replacement: PreparedSemanticOnboardingProposal,
    incident: RegistryIncidentJoinBase,
    proposal: JoinProposal,
) -> tuple[RegistryModelJoinEndpointEvidence, RegistryModelJoinEndpointEvidence]:
    """Resolve exact candidate/active endpoint authority before source profiling."""

    model, mappings, bindings = _replacement_artifacts(replacement)
    if (
        model.id != base.target_model.id
        or replacement.base_registry != base.base_registry
        or incident not in base.incident_joins
        or proposal.id != incident.contract.id
    ):
        raise ValueError("registry model join profile targets another replacement")
    left = _candidate_endpoint(
        key=proposal.left_key,
        base=base,
        incident=incident,
        replacement_mappings=mappings,
        replacement_bindings=bindings,
    )
    right = _candidate_endpoint(
        key=proposal.right_key,
        base=base,
        incident=incident,
        replacement_mappings=mappings,
        replacement_bindings=bindings,
    )
    if left.binding.connection_id != right.binding.connection_id:
        raise ValueError("registry model join profile crosses connections")
    return left, right


def registry_model_change_fingerprint(payload: object) -> str:
    return hashlib.sha256(_canonical_json(_jsonable(payload)).encode()).hexdigest()


def _build_authority(
    *,
    kind: RegistryModelChangeKind,
    base: RegistryModelReplacementBase,
    report: SemanticChangeReport | None,
    impacts: SemanticImpactSet | None,
    resolved_finding_ids: tuple[str, ...],
) -> RegistryModelChangeAuthority:
    if not base.dependency_context.dependency_index.complete:
        raise ValueError("registry model change requires complete dependencies")
    if kind is RegistryModelChangeKind.M26_REMEDIATION:
        if report is None or impacts is None:
            raise ValueError("M26 remediation requires one report and impact set")
        blocking = tuple(
            sorted(
                item.id
                for item in report.findings
                if item.severity is SemanticChangeSeverity.BLOCKING
            )
        )
        report_ids = {item.id for item in report.findings}
        impact_ids = {finding_id for impact in impacts.impacts for finding_id in impact.finding_ids}
        incident_ids = {item.contract.id for item in base.incident_joins}
        if (
            report.status is not SemanticChangeStatus.BLOCKED
            or report.context != base.dependency_context
            or report.context.registry_version != base.base_registry.registry_version
            or report.context.registry_fingerprint != base.base_registry.registry_fingerprint
            or not impacts.complete
            or report.impacts != SemanticImpactSummary.from_set(impacts)
            or tuple(sorted(set(resolved_finding_ids))) != blocking
            or not blocking
            or not impact_ids.issubset(report_ids)
            or not set(blocking).issubset(impact_ids)
            or any(
                (
                    finding.mapping is not None
                    and finding.mapping.logical_field.root.split(".", 1)[0]
                    != base.target_model.id.root
                )
                or (finding.join is not None and finding.join.contract_id not in incident_ids)
                for finding in report.findings
                if finding.severity is SemanticChangeSeverity.BLOCKING
            )
        ):
            raise ValueError("M26 remediation report or resolved findings are not exact")
    values = {
        "kind": kind,
        "base_fingerprint": base.fingerprint,
        "report": report,
        "impacts": impacts,
        "resolved_finding_ids": tuple(sorted(set(resolved_finding_ids))),
    }
    construct: Any = RegistryModelChangeAuthority.model_construct
    provisional = construct(**values, fingerprint="0" * 64)
    return RegistryModelChangeAuthority(
        **values,
        fingerprint=registry_model_change_fingerprint(_authority_payload(provisional)),
    )


def _replacement_artifacts(
    replacement: PreparedSemanticOnboardingProposal,
) -> tuple[
    ApprovedRequestModel,
    tuple[GovernedFieldMapping, ...],
    tuple[GovernedPhysicalBinding, ...],
]:
    definition = replacement.model.definition
    model = ApprovedRequestModel(
        id=definition.id,
        description=definition.description,
        fields=tuple(
            ApprovedRequestField(
                id=field.id,
                canonical_type=field.canonical_type,
                role=field.role,
                definition=field.definition,
                allowed_values=field.allowed_values,
                status=ApprovalStatus.APPROVED,
                version=definition.version,
            )
            for field in definition.fields
        ),
        status=ApprovalStatus.APPROVED,
        version=definition.version,
    )
    mappings = tuple(
        _replacement_mapping(item, logical_field_version=definition.version)
        for item in replacement.mappings
    )
    bindings = tuple(_replacement_binding(replacement, item) for item in replacement.mappings)
    return model, mappings, bindings


def _replacement_mapping(
    mapping: SemanticMappingProposal,
    *,
    logical_field_version: int,
) -> GovernedFieldMapping:
    if mapping.status is not ApprovalStatus.APPROVED or mapping.decision_id is None:
        raise ValueError("registry replacement contains an unapproved mapping")
    observation = mapping.observation
    return GovernedFieldMapping(
        mapping=ColumnMapping(
            logical_field=mapping.logical_field,
            physical_field=observation.physical_field,
            confidence=mapping.confidence,
            status=ApprovalStatus.APPROVED,
            evidence=tuple(_evidence_text(item) for item in mapping.evidence),
            risks=mapping.risks,
            transformation_plan=mapping.transformation_plan,
            version=1,
        ),
        physical_type=observation.physical_type,
        logical_field_version=logical_field_version,
        approval_decision_id=mapping.decision_id,
    )


def _replacement_binding(
    replacement: PreparedSemanticOnboardingProposal,
    mapping: SemanticMappingProposal,
) -> GovernedPhysicalBinding:
    observation = mapping.observation
    urn = observation.observed_datahub_asset_urn
    if urn is None:
        raise ValueError("registry replacement mapping requires an observed DataHub URN")
    return GovernedPhysicalBinding(
        workspace_id=replacement.workspace_id,
        connection_id=observation.locator.asset.connection_id,
        catalog_scope=observation.catalog_scope,
        catalog_generation=observation.generation,
        catalog_generation_fingerprint=observation.generation_fingerprint,
        locator=observation.locator,
        asset_metadata_fingerprint=observation.asset_metadata_fingerprint,
        field_metadata_fingerprint=observation.field_metadata_fingerprint,
        logical_field=mapping.logical_field,
        physical_field=observation.physical_field,
        physical_type=observation.physical_type,
        observed_datahub_asset_urn=urn,
        source_proposal_id=replacement.id,
        source_proposal_fingerprint=replacement.fingerprint,
    )


def _validate_change_authority(
    proposal: PreparedRegistryModelReplacementProposal,
) -> None:
    authority = proposal.authority
    if authority.kind is RegistryModelChangeKind.PLANNED_CHANGE:
        if not proposal.base.dependency_context.dependency_index.complete:
            raise ValueError("planned registry replacement requires complete dependencies")
        return
    report = authority.report
    impacts = authority.impacts
    if (
        report is None
        or impacts is None
        or report.inspected_at > proposal.prepared_at
        or report.context != proposal.base.dependency_context
    ):
        raise ValueError("M26 remediation evidence is not current for preparation")
    generations = {
        item.connection_id: (item.generation, item.inventory_fingerprint)
        for item in report.catalog_generations.observations
    }
    _, _, bindings = _replacement_artifacts(proposal.replacement)
    if any(
        generations.get(binding.connection_id)
        != (binding.catalog_generation, binding.catalog_generation_fingerprint)
        for binding in bindings
    ):
        raise ValueError("M26 remediation catalog evidence differs from replacement bindings")


def _validate_candidate_physical_meanings(
    base: RegistryModelReplacementBase,
    mappings: tuple[GovernedFieldMapping, ...],
) -> None:
    meanings = {
        item.physical_field.root: item.logical_field.root
        for item in base.unaffected_physical_meanings
    }
    pairs: set[tuple[str, str]] = set()
    for governed in mappings:
        mapping = governed.mapping
        pair = (mapping.logical_field.root, mapping.physical_field.root)
        if pair in pairs:
            raise ValueError("registry replacement mapping pairs must be unique")
        pairs.add(pair)
        prior = meanings.setdefault(mapping.physical_field.root, mapping.logical_field.root)
        if prior != mapping.logical_field.root:
            raise ValueError("one physical field cannot acquire two active logical meanings")


def _validate_incident_join_changes(
    *,
    proposal: PreparedRegistryModelReplacementProposal,
    mappings: tuple[GovernedFieldMapping, ...],
    bindings: tuple[GovernedPhysicalBinding, ...],
) -> None:
    target = proposal.base.target_model.id.root
    mapping_index = {
        (
            item.mapping.logical_field.root,
            item.mapping.physical_field.root,
            registry_model_change_fingerprint(
                item.mapping.transformation_plan.model_dump(mode="json")
            ),
        ): item
        for item in mappings
    }
    binding_index = {item.mapping_identity: item for item in bindings}
    for change in proposal.incident_join_changes:
        if isinstance(change, RegistryIncidentJoinPreservation):
            for endpoint in (change.base.left, change.base.right):
                if endpoint.key.logical_field.root.split(".", 1)[0] == target:
                    _require_candidate_key(endpoint.key, mapping_index, binding_index)
        elif isinstance(change, RegistryIncidentJoinRemoval):
            continue
        else:
            witness = change.profile_witness
            if (
                witness.change_id != proposal.draft_id
                or witness.source_replacement_proposal_id != proposal.replacement.id
                or witness.scope != proposal.scope
                or witness.base_registry != proposal.base.base_registry
                or witness.base_fingerprint != proposal.base.fingerprint
                or witness.target_model_id != proposal.base.target_model.id
                or witness.target_model_version != proposal.replacement.model.definition.version
                or not witness.is_current(proposal.prepared_at)
                or witness.result.completed_at < proposal.created_at
            ):
                raise ValueError("registry join upsert witness was reused across a change")
            for endpoint in (witness.left, witness.right):
                if endpoint.key.logical_field.root.split(".", 1)[0] == target:
                    _require_candidate_key(endpoint.key, mapping_index, binding_index)
                elif endpoint not in {change.base.left, change.base.right}:
                    raise ValueError("registry join upsert changed the unaffected endpoint")


def _require_candidate_key(
    key: NormalizedJoinKey,
    mappings: dict[tuple[str, str, str], GovernedFieldMapping],
    bindings: dict[tuple[str, str], GovernedPhysicalBinding],
) -> None:
    identity = (
        key.logical_field.root,
        key.physical_field.root,
        registry_model_change_fingerprint(key.transformation_plan.model_dump(mode="json")),
    )
    if (
        identity not in mappings
        or (
            key.logical_field.root,
            key.physical_field.root,
        )
        not in bindings
    ):
        raise ValueError("incident join key is stale for the replacement mappings")


def _validate_current_base(
    proposal: PreparedRegistryModelReplacementProposal,
    base: GovernedSemanticRegistrySnapshot,
) -> None:
    expected = proposal.base.base_registry
    expected_source = f"datahub:{datahub_registry_document_id(proposal.scope, base.version)}"
    if (
        base.format_version != 2
        or base.registry_id != proposal.scope.registry_id
        or base.catalog_scope != proposal.scope.catalog_scope
        or expected.registry_version != base.version
        or expected.registry_fingerprint != base.fingerprint
        or base.version + 1 != proposal.replacement.target_registry_version
        or base.source != expected_source
        or base.logical_context.source != f"{expected_source}/logical-context"
        or any(item.workspace_id != proposal.workspace_id for item in base.physical_bindings)
    ):
        raise ValueError("registry model replacement base changed or lacks exact v2 authority")
    resolved = resolve_registry_model_replacement_base(
        scope=proposal.scope,
        base=base,
        base_registry=expected,
        target_model_id=proposal.base.target_model.id,
        dependency_context=proposal.base.dependency_context,
    )
    if resolved != proposal.base:
        raise ValueError("registry model replacement target slice changed after review")


def _mapping_decision_matches(
    mapping: SemanticMappingProposal,
    decision: DecisionRecord,
    *,
    source_version: int,
    resulting_version: int,
) -> bool:
    return bool(
        mapping.decision_id is not None
        and decision.id == mapping.decision_id
        and decision.target_type is DecisionTargetType.COLUMN_MAPPING
        and decision.target_id == mapping.id
        and decision.action is DecisionAction.APPROVE
        and decision.status is ApprovalStatus.APPROVED
        and decision.actor == mapping.decided_by
        and decision.source_version == source_version
        and decision.resulting_version == resulting_version
        and len(decision.rationale.strip()) >= 12
        and decision.evidence
        and decision.risks == mapping.risks
    )


def _candidate_endpoint(
    *,
    key: NormalizedJoinKey,
    base: RegistryModelReplacementBase,
    incident: RegistryIncidentJoinBase,
    replacement_mappings: tuple[GovernedFieldMapping, ...],
    replacement_bindings: tuple[GovernedPhysicalBinding, ...],
) -> RegistryModelJoinEndpointEvidence:
    model_id = key.logical_field.root.split(".", 1)[0]
    if model_id != base.target_model.id.root:
        for endpoint in (incident.left, incident.right):
            if endpoint.key == key:
                return endpoint
        raise ValueError("registry join upsert changed the unaffected endpoint")
    mapping = next(
        (
            item
            for item in replacement_mappings
            if item.mapping.logical_field == key.logical_field
            and item.mapping.physical_field == key.physical_field
            and item.mapping.transformation_plan == key.transformation_plan
        ),
        None,
    )
    binding = next(
        (
            item
            for item in replacement_bindings
            if item.logical_field == key.logical_field and item.physical_field == key.physical_field
        ),
        None,
    )
    if mapping is None or binding is None:
        raise ValueError("registry join upsert target key lacks a candidate mapping")
    return RegistryModelJoinEndpointEvidence.create(
        key=key,
        mapping=mapping,
        binding=binding,
    )


def _endpoint_from_registry(
    base: GovernedSemanticRegistrySnapshot,
    key: NormalizedJoinKey,
) -> RegistryModelJoinEndpointEvidence:
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
        raise ValueError("registry incident join key lacks exact active mapping authority")
    return RegistryModelJoinEndpointEvidence.create(
        key=key,
        mapping=mapping,
        binding=binding,
    )


def _governed_mapping_ref(item: GovernedFieldMapping) -> GovernedMappingRef:
    decision_id = item.approval_decision_id
    if decision_id is None:
        raise ValueError("active registry mapping lacks its approval decision")
    return GovernedMappingRef(
        logical_field=item.mapping.logical_field,
        physical_field=item.mapping.physical_field,
        version=item.mapping.version,
        approval_decision_id=decision_id,
        physical_type=item.physical_type,
    )


def _governed_join_ref(item: JoinContract) -> GovernedJoinRef:
    decision_id = item.approval_decision_id
    if decision_id is None:
        raise ValueError("active registry join lacks its approval decision")
    return GovernedJoinRef(
        contract_id=item.id,
        version=item.version,
        approval_decision_id=decision_id,
        left_field=item.left_key.physical_field,
        right_field=item.right_key.physical_field,
        cardinality=item.cardinality,
        fanout_policy=item.fanout_policy,
    )


def _logical_join(contract: JoinContract) -> ApprovedLogicalJoin:
    decision_id = contract.approval_decision_id
    if decision_id is None:
        raise ValueError("replacement join contract lacks an approval decision")
    return ApprovedLogicalJoin(
        id=contract.id,
        left_model=LogicalModelRef(contract.left_key.logical_field.root.split(".", 1)[0]),
        right_model=LogicalModelRef(contract.right_key.logical_field.root.split(".", 1)[0]),
        cardinality=contract.cardinality,
        fanout_policy=contract.fanout_policy,
        status=ApprovalStatus.APPROVED,
        version=contract.version,
        approval_decision_id=decision_id,
    )


def _replace_target_sequence(
    current: tuple[Any, ...],
    replacement: tuple[Any, ...],
    *,
    target_model_id: str,
    model_of: Any,
) -> tuple[Any, ...]:
    result: list[Any] = []
    inserted = False
    for item in current:
        if model_of(item) == target_model_id:
            if not inserted:
                result.extend(replacement)
                inserted = True
            continue
        result.append(item)
    if not inserted:
        raise ValueError("registry replacement target artifacts disappeared")
    return tuple(result)


def _catalog_vector(
    bindings: tuple[GovernedPhysicalBinding, ...],
) -> tuple[tuple[int, str], ...]:
    return tuple(
        sorted(
            {
                (
                    item.catalog_generation,
                    item.catalog_generation_fingerprint,
                )
                for item in bindings
            }
        )
    )


def _mapping_pair(item: GovernedFieldMapping) -> tuple[str, str]:
    return item.mapping.logical_field.root, item.mapping.physical_field.root


def _evidence_text(evidence: OnboardingEvidence) -> str:
    suffix = "" if evidence.reference is None else f" [{evidence.reference}]"
    return f"{evidence.kind.value}: {evidence.detail}{suffix}"


def _endpoint_payload(value: RegistryModelJoinEndpointEvidence) -> dict[str, object]:
    return value.model_dump(mode="json", exclude={"fingerprint"})


def _incident_base_payload(value: RegistryIncidentJoinBase) -> dict[str, object]:
    return value.model_dump(mode="json", exclude={"fingerprint"})


def _replacement_base_payload(value: RegistryModelReplacementBase) -> dict[str, object]:
    return value.model_dump(mode="json", exclude={"fingerprint"})


def _authority_payload(value: RegistryModelChangeAuthority) -> dict[str, object]:
    return value.model_dump(mode="json", exclude={"fingerprint"})


def _profile_witness_payload(value: RegistryModelJoinProfileWitness) -> dict[str, object]:
    return value.model_dump(mode="json", exclude={"fingerprint"})


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
    return json.loads(json.dumps(value))


def _canonical_json(value: object) -> str:
    return json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"))


def _sha256(value: str, label: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return value


__all__ = [
    "PreparedRegistryModelReplacementProposal",
    "RegistryIncidentJoinAction",
    "RegistryIncidentJoinBase",
    "RegistryIncidentJoinChange",
    "RegistryIncidentJoinPreservation",
    "RegistryIncidentJoinRemoval",
    "RegistryIncidentJoinUpsert",
    "RegistryModelChangeAuthority",
    "RegistryModelChangeKind",
    "RegistryModelJoinEndpointEvidence",
    "RegistryModelJoinProfileWitness",
    "RegistryModelReplacementBase",
    "RegistryPhysicalMeaning",
    "assemble_model_replacement_registry_version",
    "registry_model_change_fingerprint",
    "registry_model_replacement_artifacts",
    "resolve_registry_model_join_profile_endpoints",
    "resolve_registry_model_replacement_base",
]
