"""Pure contracts for governed registry activation and reconciliation."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.semantic_registry import (
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
    datahub_registry_document_id,
    datahub_registry_document_urn,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[a-z][a-z0-9_-]{2,199}$")
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class RegistryVersionTrust(StrEnum):
    """Whether an immutable version passed the current strict approval contract."""

    STRICT = "strict"
    LEGACY_READ_ONLY = "legacy_read_only"


class RegistryActivationAction(StrEnum):
    ACTIVATE = "activate"
    ROLLBACK = "rollback"


class RegistryActivationConfirmation(StrEnum):
    ACTIVATE_APPROVED_REGISTRY_VERSION = "activate-approved-registry-version"
    ROLLBACK_TO_APPROVED_REGISTRY_VERSION = "rollback-to-approved-registry-version"


class RegistryProjectionOutboxStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    SUPERSEDED = "superseded"
    BLOCKED = "blocked"


class ControlAuditChainVerification(FrozenDomainModel):
    """Bounded integrity result for one workspace control-audit chain."""

    workspace_id: str = Field(min_length=1, max_length=200)
    event_count: int = Field(ge=0)
    head_hash: str | None = None
    valid: bool

    @field_validator("workspace_id")
    @classmethod
    def workspace_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("control audit workspace must not be blank")
        return value

    @field_validator("head_hash")
    @classmethod
    def optional_head_must_be_sha256(cls, value: str | None) -> str | None:
        if value is not None and _SHA256.fullmatch(value) is None:
            raise ValueError("control audit head must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def valid_chain_head_must_match_count(self) -> ControlAuditChainVerification:
        if self.valid and ((self.event_count == 0) != (self.head_hash is None)):
            raise ValueError("valid control audit chain head does not match its event count")
        return self


class RegistryReconciliationCode(StrEnum):
    IN_SYNC = "in_sync"
    PROJECTION_MISSING = "projection_missing"
    PROJECTION_BEHIND = "projection_behind"
    PROJECTION_AHEAD = "projection_ahead"
    PROJECTION_CONFLICT = "projection_conflict"
    SUPERSEDED = "superseded"
    VERSION_CORRUPT = "version_corrupt"
    AUDIT_GAP = "audit_gap"
    PENDING_OUTBOX = "pending_outbox"


class RegistryReconciliationSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKING = "blocking"


class RegistryReconciliationConfirmation(StrEnum):
    REPAIR_ACTIVE_REGISTRY_PROJECTION = "repair-active-registry-projection"


class GovernedRegistryVersion(FrozenDomainModel):
    """One exact DataHub version plus its explicitly observed approval trust."""

    snapshot: ScopedSemanticRegistrySnapshot
    publication_approval_id: str = Field(min_length=1, max_length=200)
    trust: RegistryVersionTrust

    @field_validator("publication_approval_id")
    @classmethod
    def approval_id_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("registry version approval id must not be blank")
        return value

    @model_validator(mode="after")
    def source_must_identify_the_exact_datahub_version(self) -> GovernedRegistryVersion:
        registry = self.snapshot.registry
        document_id = datahub_registry_document_id(self.snapshot.scope, registry.version)
        expected_source = f"datahub:{document_id}"
        if (
            registry.source != expected_source
            or registry.logical_context.source != f"{expected_source}/logical-context"
            or any(
                item.source != f"{expected_source}/{item.kind.value}"
                for item in registry.provenance
            )
        ):
            raise ValueError("governed registry version is not bound to its exact DataHub target")
        return self


class ActiveRegistryPointer(FrozenDomainModel):
    """Authoritative control-plane selection for one registry scope."""

    scope: SemanticRegistryScope
    generation: int = Field(ge=1)
    registry_version: int = Field(ge=1)
    registry_fingerprint: str
    registry_target: str = Field(min_length=1, max_length=500)
    transition_id: str = Field(min_length=3, max_length=200)
    activated_by: str = Field(min_length=1, max_length=120)
    activated_at: datetime
    decision_ids: tuple[str, ...] = Field(min_length=1, max_length=2_000)

    @field_validator("registry_fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("active registry fingerprint must be lowercase SHA-256")
        return value

    @field_validator("transition_id")
    @classmethod
    def transition_id_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("registry transition id must be inert")
        return value

    @field_validator("activated_by")
    @classmethod
    def actor_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("registry activation actor must not be blank")
        return value

    @field_validator("activated_at")
    @classmethod
    def activation_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "registry activation time")

    @field_validator("decision_ids")
    @classmethod
    def decisions_must_be_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_decisions(values, "active registry")

    @model_validator(mode="after")
    def target_must_match_scope_and_version(self) -> ActiveRegistryPointer:
        if self.registry_target != datahub_registry_document_urn(
            self.scope,
            self.registry_version,
        ):
            raise ValueError("active registry target does not match its scope and version")
        return self


class RegistryActivationProposal(FrozenDomainModel):
    """Exact compare-and-swap intent prepared from current and target state."""

    action: RegistryActivationAction
    scope: SemanticRegistryScope
    expected_generation: int = Field(ge=0)
    expected_registry_version: int | None = Field(default=None, ge=1)
    expected_registry_fingerprint: str | None = None
    expected_transition_id: str | None = Field(default=None, min_length=3, max_length=200)
    target_registry_version: int = Field(ge=1)
    target_registry_fingerprint: str
    target_registry_urn: str = Field(min_length=1, max_length=500)
    target_publication_approval_id: str = Field(min_length=1, max_length=200)
    decision_ids: tuple[str, ...] = Field(min_length=1, max_length=2_000)
    rollback_transition_id: str | None = Field(default=None, min_length=3, max_length=200)

    @field_validator("expected_registry_fingerprint", "target_registry_fingerprint")
    @classmethod
    def optional_fingerprints_must_be_sha256(cls, value: str | None) -> str | None:
        if value is not None and _SHA256.fullmatch(value) is None:
            raise ValueError("registry activation fingerprints must be lowercase SHA-256")
        return value

    @field_validator(
        "expected_transition_id",
        "rollback_transition_id",
    )
    @classmethod
    def optional_transition_ids_must_be_inert(cls, value: str | None) -> str | None:
        if value is not None and _SAFE_ID.fullmatch(value) is None:
            raise ValueError("registry activation transition ids must be inert")
        return value

    @field_validator("target_publication_approval_id")
    @classmethod
    def target_approval_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("target registry publication approval must not be blank")
        return value

    @field_validator("decision_ids")
    @classmethod
    def decisions_must_be_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_decisions(values, "registry activation proposal")

    @model_validator(mode="after")
    def compare_and_swap_shape_must_be_exact(self) -> RegistryActivationProposal:
        expected_values = (
            self.expected_registry_version,
            self.expected_registry_fingerprint,
            self.expected_transition_id,
        )
        if self.expected_generation == 0:
            if any(value is not None for value in expected_values):
                raise ValueError("initial activation cannot claim an existing pointer")
        elif any(value is None for value in expected_values):
            raise ValueError("existing activation requires the complete expected pointer")
        if self.target_registry_urn != datahub_registry_document_urn(
            self.scope,
            self.target_registry_version,
        ):
            raise ValueError("activation target does not match its scope and version")
        if self.action is RegistryActivationAction.ACTIVATE:
            if self.rollback_transition_id is not None:
                raise ValueError("forward activation cannot identify a rollback transition")
        elif self.rollback_transition_id is None:
            raise ValueError("rollback must identify a previously active transition")
        if (
            self.expected_registry_fingerprint == self.target_registry_fingerprint
            and self.expected_registry_version == self.target_registry_version
        ):
            raise ValueError("activation target is already active")
        return self

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json"))


class RegistryActivationApproval(FrozenDomainModel):
    """Human authorization bound to one exact activation proposal."""

    id: str = Field(min_length=3, max_length=200)
    proposal: RegistryActivationProposal
    actor: str = Field(min_length=1, max_length=120)
    approved_at: datetime
    confirmation: RegistryActivationConfirmation

    @field_validator("id")
    @classmethod
    def approval_id_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("registry activation approval id must be inert")
        return value

    @field_validator("actor")
    @classmethod
    def actor_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("registry activation approval actor must not be blank")
        return value

    @field_validator("approved_at")
    @classmethod
    def approval_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "registry activation approval time")

    @model_validator(mode="after")
    def identity_and_confirmation_must_match_proposal(self) -> RegistryActivationApproval:
        expected_confirmation = {
            RegistryActivationAction.ACTIVATE: (
                RegistryActivationConfirmation.ACTIVATE_APPROVED_REGISTRY_VERSION
            ),
            RegistryActivationAction.ROLLBACK: (
                RegistryActivationConfirmation.ROLLBACK_TO_APPROVED_REGISTRY_VERSION
            ),
        }[self.proposal.action]
        if (
            self.id
            != registry_activation_approval_id(
                self.proposal,
                self.actor,
                self.approved_at,
                self.confirmation,
            )
            or self.confirmation is not expected_confirmation
        ):
            raise ValueError("registry activation approval does not match its exact proposal")
        return self


class RegistryActivationTransition(FrozenDomainModel):
    """Immutable activation history event committed by the control store."""

    id: str = Field(min_length=3, max_length=200)
    approval: RegistryActivationApproval
    previous_pointer: ActiveRegistryPointer | None
    active_pointer: ActiveRegistryPointer
    committed_at: datetime

    @field_validator("id")
    @classmethod
    def transition_id_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("registry activation transition id must be inert")
        return value

    @field_validator("committed_at")
    @classmethod
    def commit_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "registry transition commit time")

    @model_validator(mode="after")
    def transition_must_materialize_the_exact_approval(self) -> RegistryActivationTransition:
        proposal = self.approval.proposal
        if self.id != registry_activation_transition_id(self.approval):
            raise ValueError("registry transition id does not match its approval")
        if self.committed_at < self.approval.approved_at:
            raise ValueError("registry transition cannot predate its approval")
        if proposal.expected_generation == 0:
            if self.previous_pointer is not None:
                raise ValueError("initial registry transition cannot have a previous pointer")
        elif self.previous_pointer is None or not pointer_matches_proposal(
            self.previous_pointer,
            proposal,
        ):
            raise ValueError("registry transition previous pointer does not match its proposal")
        pointer = self.active_pointer
        if (
            pointer.scope != proposal.scope
            or pointer.generation != proposal.expected_generation + 1
            or pointer.registry_version != proposal.target_registry_version
            or pointer.registry_fingerprint != proposal.target_registry_fingerprint
            or pointer.registry_target != proposal.target_registry_urn
            or pointer.transition_id != self.id
            or pointer.activated_by != self.approval.actor
            or pointer.activated_at != self.committed_at
            or pointer.decision_ids != proposal.decision_ids
        ):
            raise ValueError("registry transition active pointer differs from its approval")
        return self


class RegistryProjectionState(FrozenDomainModel):
    """Exact DataHub projection payload observed or desired by reconciliation."""

    pointer: ActiveRegistryPointer
    projection_fingerprint: str

    @field_validator("projection_fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("registry projection fingerprint must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def fingerprint_must_match_pointer(self) -> RegistryProjectionState:
        if self.projection_fingerprint != registry_projection_fingerprint(self.pointer):
            raise ValueError("registry projection fingerprint does not match its pointer")
        return self


class RegistryProjectionOutboxItem(FrozenDomainModel):
    """Durable projection intent created atomically with an activation."""

    id: str = Field(min_length=3, max_length=200)
    transition_id: str = Field(min_length=3, max_length=200)
    desired: RegistryProjectionState
    status: RegistryProjectionOutboxStatus = RegistryProjectionOutboxStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    created_at: datetime
    last_reason_code: str | None = Field(default=None, min_length=2, max_length=64)

    @field_validator("id", "transition_id")
    @classmethod
    def ids_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("registry projection outbox ids must be inert")
        return value

    @field_validator("created_at")
    @classmethod
    def created_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "registry projection outbox creation time")

    @field_validator("last_reason_code")
    @classmethod
    def reason_must_be_inert(cls, value: str | None) -> str | None:
        if value is not None and _SAFE_REASON.fullmatch(value) is None:
            raise ValueError("registry projection reason must be inert")
        return value

    @model_validator(mode="after")
    def identity_and_reason_must_match_status(self) -> RegistryProjectionOutboxItem:
        if self.id != registry_projection_outbox_id(self.transition_id):
            raise ValueError("registry projection outbox id does not match its transition")
        if self.desired.pointer.transition_id != self.transition_id:
            raise ValueError("registry projection outbox points to another transition")
        blocked = self.status is RegistryProjectionOutboxStatus.BLOCKED
        if blocked != (self.last_reason_code is not None):
            raise ValueError("only blocked projection outbox items carry a reason")
        return self


class RegistryControlCommit(FrozenDomainModel):
    """Typed result of the atomic pointer/history/outbox/audit transaction."""

    transition: RegistryActivationTransition
    outbox: RegistryProjectionOutboxItem
    audit_event_hash: str
    replayed: bool = False

    @field_validator("audit_event_hash")
    @classmethod
    def audit_hash_must_be_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("registry control audit hash must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def commit_parts_must_match(self) -> RegistryControlCommit:
        if (
            self.outbox.transition_id != self.transition.id
            or self.outbox.desired.pointer != self.transition.active_pointer
        ):
            raise ValueError("registry control commit parts identify different transitions")
        return self


class RegistryReconciliationFinding(FrozenDomainModel):
    code: RegistryReconciliationCode
    severity: RegistryReconciliationSeverity
    message: str = Field(min_length=1, max_length=240)

    @field_validator("message")
    @classmethod
    def message_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("registry reconciliation message must not be blank")
        return value


class RegistryReconciliationReport(FrozenDomainModel):
    scope: SemanticRegistryScope
    active_pointer: ActiveRegistryPointer
    desired_projection: RegistryProjectionState
    observed_projection: RegistryProjectionState | None
    pending_outbox: RegistryProjectionOutboxItem | None
    findings: tuple[RegistryReconciliationFinding, ...] = Field(min_length=1, max_length=12)
    inspected_at: datetime

    @field_validator("inspected_at")
    @classmethod
    def inspection_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "registry reconciliation inspection time")

    @model_validator(mode="after")
    def report_parts_must_share_one_scope(self) -> RegistryReconciliationReport:
        if (
            self.active_pointer.scope != self.scope
            or self.desired_projection.pointer != self.active_pointer
            or (
                self.observed_projection is not None
                and self.observed_projection.pointer.scope != self.scope
            )
            or (
                self.pending_outbox is not None
                and self.pending_outbox.desired.pointer.scope != self.scope
            )
        ):
            raise ValueError("registry reconciliation report crosses registry scopes")
        codes = tuple(finding.code for finding in self.findings)
        if len(codes) != len(set(codes)):
            raise ValueError("registry reconciliation findings must be unique")
        return self

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json"))


class RegistryReconciliationApproval(FrozenDomainModel):
    id: str = Field(min_length=3, max_length=200)
    report_fingerprint: str
    scope: SemanticRegistryScope
    generation: int = Field(ge=1)
    registry_fingerprint: str
    actor: str = Field(min_length=1, max_length=120)
    approved_at: datetime
    confirmation: RegistryReconciliationConfirmation

    @field_validator("id")
    @classmethod
    def approval_id_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("registry reconciliation approval id must be inert")
        return value

    @field_validator("report_fingerprint", "registry_fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("registry reconciliation fingerprints must be lowercase SHA-256")
        return value

    @field_validator("actor")
    @classmethod
    def actor_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("registry reconciliation actor must not be blank")
        return value

    @field_validator("approved_at")
    @classmethod
    def approval_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "registry reconciliation approval time")

    @model_validator(mode="after")
    def id_must_match_immutable_approval_facts(self) -> RegistryReconciliationApproval:
        expected = registry_reconciliation_approval_id_from_fingerprint(
            self.report_fingerprint,
            self.actor,
            self.approved_at,
            self.confirmation,
        )
        if self.id != expected:
            raise ValueError("registry reconciliation approval id does not match its facts")
        return self


class RegistryProjectionOutcome(FrozenDomainModel):
    outbox_id: str = Field(min_length=3, max_length=200)
    transition_id: str = Field(min_length=3, max_length=200)
    scope: SemanticRegistryScope
    generation: int = Field(ge=1)
    status: RegistryProjectionOutboxStatus
    observed_projection: RegistryProjectionState | None = None
    occurred_at: datetime
    reason_code: str | None = Field(default=None, min_length=2, max_length=64)

    @field_validator("outbox_id", "transition_id")
    @classmethod
    def ids_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("registry projection outcome ids must be inert")
        return value

    @field_validator("occurred_at")
    @classmethod
    def outcome_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "registry projection outcome time")

    @field_validator("reason_code")
    @classmethod
    def reason_must_be_inert(cls, value: str | None) -> str | None:
        if value is not None and _SAFE_REASON.fullmatch(value) is None:
            raise ValueError("registry projection outcome reason must be inert")
        return value

    @model_validator(mode="after")
    def outcome_must_be_terminal_and_consistent(self) -> RegistryProjectionOutcome:
        if self.status is RegistryProjectionOutboxStatus.PENDING:
            raise ValueError("registry projection outcome must be terminal")
        blocked = self.status is RegistryProjectionOutboxStatus.BLOCKED
        if blocked != (self.reason_code is not None):
            raise ValueError("only blocked projection outcomes carry a reason")
        if self.observed_projection is not None and (
            self.observed_projection.pointer.scope != self.scope
            or self.observed_projection.pointer.generation != self.generation
        ):
            raise ValueError("registry projection outcome observed another pointer")
        return self


def registry_activation_approval_id(
    proposal: RegistryActivationProposal,
    actor: str,
    approved_at: datetime,
    confirmation: RegistryActivationConfirmation,
) -> str:
    payload = {
        "proposal": proposal.fingerprint,
        "actor": actor,
        "approved_at": _canonical_datetime(approved_at, "registry activation approval time"),
        "confirmation": confirmation.value,
    }
    return f"registry-activation-v1-{_fingerprint(payload)}"


def registry_activation_transition_id(approval: RegistryActivationApproval) -> str:
    return f"registry-transition-v1-{_fingerprint({'approval_id': approval.id})}"


def registry_projection_outbox_id(transition_id: str) -> str:
    return f"registry-projection-v1-{_fingerprint({'transition_id': transition_id})}"


def registry_projection_fingerprint(pointer: ActiveRegistryPointer) -> str:
    return _fingerprint(pointer.model_dump(mode="json"))


def registry_reconciliation_approval_id(
    report: RegistryReconciliationReport,
    actor: str,
    approved_at: datetime,
    confirmation: RegistryReconciliationConfirmation,
) -> str:
    return registry_reconciliation_approval_id_from_fingerprint(
        report.fingerprint,
        actor,
        approved_at,
        confirmation,
    )


def registry_reconciliation_approval_id_from_fingerprint(
    report_fingerprint: str,
    actor: str,
    approved_at: datetime,
    confirmation: RegistryReconciliationConfirmation,
) -> str:
    if _SHA256.fullmatch(report_fingerprint) is None:
        raise ValueError("registry reconciliation report fingerprint is invalid")
    payload = {
        "report": report_fingerprint,
        "actor": actor,
        "approved_at": _canonical_datetime(
            approved_at,
            "registry reconciliation approval time",
        ),
        "confirmation": confirmation.value,
    }
    return f"registry-reconcile-v1-{_fingerprint(payload)}"


def validate_registry_activation_approval(
    proposal: RegistryActivationProposal,
    approval: RegistryActivationApproval,
) -> None:
    """Reject forged Pydantic copies and approvals for another exact CAS."""

    if not isinstance(proposal, RegistryActivationProposal) or not isinstance(
        approval,
        RegistryActivationApproval,
    ):
        raise ValueError("explicit registry activation approval is required")
    try:
        validated_proposal = RegistryActivationProposal.model_validate(
            proposal.model_dump(mode="python", warnings=False)
        )
        validated_approval = RegistryActivationApproval.model_validate(
            approval.model_dump(mode="python", warnings=False)
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("registry activation contracts failed typed validation") from error
    if (
        validated_proposal != proposal
        or validated_approval != approval
        or validated_approval.proposal != validated_proposal
    ):
        raise ValueError("registry activation approval does not match the exact proposal")


def validate_registry_reconciliation_approval(
    report: RegistryReconciliationReport,
    approval: RegistryReconciliationApproval,
) -> None:
    if not isinstance(report, RegistryReconciliationReport) or not isinstance(
        approval,
        RegistryReconciliationApproval,
    ):
        raise ValueError("explicit registry reconciliation approval is required")
    try:
        validated_report = RegistryReconciliationReport.model_validate(
            report.model_dump(mode="python", warnings=False)
        )
        validated_approval = RegistryReconciliationApproval.model_validate(
            approval.model_dump(mode="python", warnings=False)
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("registry reconciliation contracts failed typed validation") from error
    if (
        validated_report != report
        or validated_approval != approval
        or approval.id
        != registry_reconciliation_approval_id(
            report,
            approval.actor,
            approval.approved_at,
            approval.confirmation,
        )
        or approval.report_fingerprint != report.fingerprint
        or approval.scope != report.scope
        or approval.generation != report.active_pointer.generation
        or approval.registry_fingerprint != report.active_pointer.registry_fingerprint
        or approval.confirmation
        is not RegistryReconciliationConfirmation.REPAIR_ACTIVE_REGISTRY_PROJECTION
    ):
        raise ValueError("registry reconciliation approval does not match the exact report")


def pointer_matches_proposal(
    pointer: ActiveRegistryPointer,
    proposal: RegistryActivationProposal,
) -> bool:
    return (
        pointer.scope == proposal.scope
        and pointer.generation == proposal.expected_generation
        and pointer.registry_version == proposal.expected_registry_version
        and pointer.registry_fingerprint == proposal.expected_registry_fingerprint
        and pointer.transition_id == proposal.expected_transition_id
    )


def build_registry_activation_transition(
    proposal: RegistryActivationProposal,
    approval: RegistryActivationApproval,
    *,
    previous_pointer: ActiveRegistryPointer | None,
    committed_at: datetime,
) -> RegistryActivationTransition:
    validate_registry_activation_approval(proposal, approval)
    transition_id = registry_activation_transition_id(approval)
    pointer = ActiveRegistryPointer(
        scope=proposal.scope,
        generation=proposal.expected_generation + 1,
        registry_version=proposal.target_registry_version,
        registry_fingerprint=proposal.target_registry_fingerprint,
        registry_target=proposal.target_registry_urn,
        transition_id=transition_id,
        activated_by=approval.actor,
        activated_at=committed_at,
        decision_ids=proposal.decision_ids,
    )
    return RegistryActivationTransition(
        id=transition_id,
        approval=approval,
        previous_pointer=previous_pointer,
        active_pointer=pointer,
        committed_at=committed_at,
    )


def build_registry_projection_outbox(
    transition: RegistryActivationTransition,
) -> RegistryProjectionOutboxItem:
    return RegistryProjectionOutboxItem(
        id=registry_projection_outbox_id(transition.id),
        transition_id=transition.id,
        desired=RegistryProjectionState(
            pointer=transition.active_pointer,
            projection_fingerprint=registry_projection_fingerprint(transition.active_pointer),
        ),
        created_at=transition.committed_at,
    )


def _canonical_decisions(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if (
        len(values) != len(set(values))
        or any(not value.strip() for value in values)
        or values != tuple(sorted(values))
    ):
        raise ValueError(f"{label} decision ids must be unique, nonblank, and sorted")
    return values


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return value


def _canonical_datetime(value: datetime, label: str) -> str:
    return _aware(value, label).astimezone(UTC).isoformat().replace("+00:00", "Z")


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
