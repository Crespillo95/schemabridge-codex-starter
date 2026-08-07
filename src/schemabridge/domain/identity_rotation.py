"""Pure contracts for verified dual-key pseudonym rotation."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.identity import AuthenticationMethod

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_KEY_VERSION = re.compile(r"^v[1-9][0-9]{0,5}$")
_SAFE_ACTOR = re.compile(r"^sb_actor_v[1-9][0-9]{0,5}_[0-9a-f]{64}$")
_SAFE_WORKSPACE = re.compile(r"^sb_workspace_v[1-9][0-9]{0,5}_[0-9a-f]{64}$")
_MAX_OWNER_BINDINGS = 10_000
_MAX_HISTORY_BINDINGS = 50_000


class IdentityBindingKind(StrEnum):
    """Closed kinds of opaque authorization identity."""

    WORKSPACE = "workspace"
    ACTOR = "actor"


class IdentityRotationConfirmation(StrEnum):
    """Exact human confirmation required for a rotation."""

    ROTATE_VERIFIED_OIDC_BINDINGS = "rotate-verified-oidc-bindings"


class IdentityInitializationConfirmation(StrEnum):
    """Exact human confirmation required for the first durable binding."""

    INITIALIZE_VERIFIED_OIDC_BINDINGS = "initialize-verified-oidc-bindings"


class IdentityRotationInvariantCode(StrEnum):
    """Stable pure-domain rejection categories."""

    DERIVATION_MISMATCH = "identity_rotation_derivation_mismatch"
    CROSS_WORKSPACE = "identity_rotation_cross_workspace"
    COLLISION = "identity_rotation_collision"
    CYCLE = "identity_rotation_cycle"
    INCOMPLETE_OWNER_BINDING = "identity_rotation_incomplete_owner_binding"
    STALE_PLAN = "identity_rotation_stale_plan"
    APPROVAL_MISMATCH = "identity_rotation_approval_mismatch"


class IdentityRotationInvariantError(ValueError):
    """A deterministic identity-rotation invariant failure."""

    def __init__(self, code: IdentityRotationInvariantCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class VerifiedOidcKeyDerivation(FrozenDomainModel):
    """Transient opaque evidence derived from one already verified OIDC identity."""

    verification_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_reference_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    actor_reference_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_id: str = Field(min_length=1, max_length=200)
    actor_id: str = Field(min_length=1, max_length=200)
    key_version: str = Field(pattern=r"^v[1-9][0-9]{0,5}$")
    provenance_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance_version: int = Field(ge=1)
    policy_version: int = Field(ge=1)
    verified_at: datetime
    authentication_method: AuthenticationMethod

    @field_validator("verified_at")
    @classmethod
    def verified_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "identity derivation verification time")

    @model_validator(mode="after")
    def derivation_must_be_oidc_and_key_bound(self) -> VerifiedOidcKeyDerivation:
        if self.authentication_method is not AuthenticationMethod.OIDC:
            raise ValueError("identity rotation requires a verified OIDC derivation")
        if self.workspace_reference_digest == self.actor_reference_digest:
            raise ValueError("workspace and actor references must be distinct")
        if not _opaque_id_matches(
            IdentityBindingKind.WORKSPACE,
            self.workspace_id,
            self.key_version,
        ):
            raise ValueError("workspace pseudonym does not match its key version")
        if not _opaque_id_matches(
            IdentityBindingKind.ACTOR,
            self.actor_id,
            self.key_version,
        ):
            raise ValueError("actor pseudonym does not match its key version")
        return self


class VerifiedDualKeyOidcDerivation(FrozenDomainModel):
    """Two pseudonym derivations from the exact same verified OIDC identity."""

    previous: VerifiedOidcKeyDerivation
    current: VerifiedOidcKeyDerivation

    @model_validator(mode="after")
    def keys_must_share_one_verified_identity(self) -> VerifiedDualKeyOidcDerivation:
        previous = self.previous
        current = self.current
        if (
            previous.verification_id != current.verification_id
            or previous.workspace_reference_digest != current.workspace_reference_digest
            or previous.actor_reference_digest != current.actor_reference_digest
            or previous.provenance_fingerprint != current.provenance_fingerprint
            or previous.provenance_version != current.provenance_version
            or previous.policy_version != current.policy_version
            or previous.verified_at != current.verified_at
            or previous.authentication_method is not current.authentication_method
        ):
            raise ValueError("dual-key derivations must come from the same verified OIDC identity")
        if previous.key_version == current.key_version:
            raise ValueError("dual-key derivations require different key versions")
        if previous.workspace_id == current.workspace_id or previous.actor_id == current.actor_id:
            raise ValueError("dual-key derivations produced an opaque identifier collision")
        return self


class IdentityInitializationApproval(FrozenDomainModel):
    """Approval bound to one signed evidence envelope and its opaque derivations."""

    id: str = Field(pattern=r"^identity-initialization-approval-[0-9a-f]{64}$")
    workspace_id: str = Field(min_length=1, max_length=200)
    evidence_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    derivations_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    actor: str = Field(min_length=1, max_length=120)
    approved_at: datetime
    confirmation: IdentityInitializationConfirmation

    @field_validator("workspace_id")
    @classmethod
    def workspace_must_be_opaque(cls, value: str) -> str:
        if _SAFE_WORKSPACE.fullmatch(value) is None:
            raise ValueError("identity initialization workspace must be an opaque inert id")
        return value

    @field_validator("actor")
    @classmethod
    def approver_must_be_opaque(cls, value: str) -> str:
        if _SAFE_ACTOR.fullmatch(value) is None:
            raise ValueError("identity initialization approver must be an opaque inert id")
        return value

    @field_validator("approved_at")
    @classmethod
    def approval_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "identity initialization approval time")

    @model_validator(mode="after")
    def approval_must_be_self_identifying(self) -> IdentityInitializationApproval:
        if (
            self.confirmation
            is not IdentityInitializationConfirmation.INITIALIZE_VERIFIED_OIDC_BINDINGS
            or self.id
            != identity_initialization_approval_id(
                workspace_id=self.workspace_id,
                evidence_fingerprint=self.evidence_fingerprint,
                derivations_fingerprint=self.derivations_fingerprint,
                actor=self.actor,
                approved_at=self.approved_at,
                confirmation=self.confirmation,
            )
        ):
            raise ValueError("identity initialization approval does not match its facts")
        return self


class OpaqueIdentityRotationBinding(FrozenDomainModel):
    """The only identity mapping eligible for durable rotation persistence."""

    binding_kind: IdentityBindingKind
    workspace_reference_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    stable_reference_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    old_opaque_id: str = Field(min_length=1, max_length=200)
    new_opaque_id: str = Field(min_length=1, max_length=200)
    from_key_version: str = Field(pattern=r"^v[1-9][0-9]{0,5}$")
    to_key_version: str = Field(pattern=r"^v[1-9][0-9]{0,5}$")
    provenance_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance_version: int = Field(ge=1)
    policy_version: int = Field(ge=1)

    @model_validator(mode="after")
    def binding_must_be_opaque_and_key_bound(self) -> OpaqueIdentityRotationBinding:
        if self.from_key_version == self.to_key_version:
            raise ValueError("identity binding requires different key versions")
        if self.old_opaque_id == self.new_opaque_id:
            raise ValueError("identity binding cannot map an opaque id to itself")
        if not _opaque_id_matches(
            self.binding_kind,
            self.old_opaque_id,
            self.from_key_version,
        ) or not _opaque_id_matches(
            self.binding_kind,
            self.new_opaque_id,
            self.to_key_version,
        ):
            raise ValueError("identity binding ids do not match their key versions")
        if self.binding_kind is IdentityBindingKind.WORKSPACE:
            if self.stable_reference_digest != self.workspace_reference_digest:
                raise ValueError("workspace binding must use its workspace reference")
        elif self.stable_reference_digest == self.workspace_reference_digest:
            raise ValueError("actor binding cannot use the workspace reference")
        return self

    @property
    def fingerprint(self) -> str:
        """Return the canonical immutable binding identity."""

        return _fingerprint(self.model_dump(mode="json"))


class IdentityRotationState(FrozenDomainModel):
    """Current owner and binding facts read without historical payload bytes."""

    workspace_id: str = Field(min_length=1, max_length=200)
    workspace_reference_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    active_key_version: str = Field(pattern=r"^v[1-9][0-9]{0,5}$")
    provenance_version: int = Field(ge=1)
    policy_version: int = Field(ge=1)
    revision: int = Field(ge=0)
    owner_actor_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=_MAX_OWNER_BINDINGS,
    )
    previous_key_versions: tuple[str, ...] = Field(
        default=(),
        max_length=1_000,
    )
    historical_bindings: tuple[OpaqueIdentityRotationBinding, ...] = Field(
        default=(),
        max_length=_MAX_HISTORY_BINDINGS,
    )

    @field_validator("owner_actor_ids")
    @classmethod
    def owners_must_be_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if (
            values != tuple(sorted(values))
            or len(values) != len(set(values))
            or any(_SAFE_ACTOR.fullmatch(value) is None for value in values)
        ):
            raise ValueError("identity rotation owners must be unique canonical opaque ids")
        return values

    @field_validator("previous_key_versions")
    @classmethod
    def previous_versions_must_be_canonical(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if (
            values != tuple(sorted(values))
            or len(values) != len(set(values))
            or any(_KEY_VERSION.fullmatch(value) is None for value in values)
        ):
            raise ValueError("previous identity key versions must be canonical")
        return values

    @field_validator("historical_bindings")
    @classmethod
    def history_must_be_canonical(
        cls,
        values: tuple[OpaqueIdentityRotationBinding, ...],
    ) -> tuple[OpaqueIdentityRotationBinding, ...]:
        if values != tuple(sorted(values, key=_binding_sort_key)):
            raise ValueError("historical identity bindings must be canonical")
        fingerprints = tuple(value.fingerprint for value in values)
        if len(fingerprints) != len(set(fingerprints)):
            raise ValueError("historical identity bindings must be unique")
        return values

    @model_validator(mode="after")
    def current_state_must_match_the_active_key(self) -> IdentityRotationState:
        if not _opaque_id_matches(
            IdentityBindingKind.WORKSPACE,
            self.workspace_id,
            self.active_key_version,
        ):
            raise ValueError("identity rotation workspace does not match the active key")
        if any(
            not _opaque_id_matches(
                IdentityBindingKind.ACTOR,
                owner,
                self.active_key_version,
            )
            for owner in self.owner_actor_ids
        ):
            raise ValueError("identity rotation owner does not match the active key")
        if self.active_key_version in self.previous_key_versions:
            raise ValueError("active identity key cannot also be historical")
        if any(
            binding.workspace_reference_digest != self.workspace_reference_digest
            for binding in self.historical_bindings
        ):
            raise ValueError("historical identity binding crosses workspace scope")
        return self

    @property
    def fingerprint(self) -> str:
        """Bind a plan to the exact owner and binding snapshot."""

        return _fingerprint(self.model_dump(mode="json"))


class IdentityRotationPlan(FrozenDomainModel):
    """Canonical idempotent plan containing opaque mappings only."""

    id: str = Field(pattern=r"^identity-rotation-[0-9a-f]{64}$")
    old_workspace_id: str = Field(min_length=1, max_length=200)
    new_workspace_id: str = Field(min_length=1, max_length=200)
    workspace_reference_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    from_key_version: str = Field(pattern=r"^v[1-9][0-9]{0,5}$")
    to_key_version: str = Field(pattern=r"^v[1-9][0-9]{0,5}$")
    provenance_version: int = Field(ge=1)
    policy_version: int = Field(ge=1)
    expected_state_revision: int = Field(ge=0)
    expected_state_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    owner_actor_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=_MAX_OWNER_BINDINGS,
    )
    bindings: tuple[OpaqueIdentityRotationBinding, ...] = Field(
        min_length=2,
        max_length=_MAX_OWNER_BINDINGS + 1,
    )

    @field_validator("owner_actor_ids")
    @classmethod
    def owners_must_be_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(values)) or len(values) != len(set(values)):
            raise ValueError("identity rotation plan owners must be canonical")
        return values

    @field_validator("bindings")
    @classmethod
    def bindings_must_be_canonical(
        cls,
        values: tuple[OpaqueIdentityRotationBinding, ...],
    ) -> tuple[OpaqueIdentityRotationBinding, ...]:
        if values != tuple(sorted(values, key=_binding_sort_key)):
            raise ValueError("identity rotation plan bindings must be canonical")
        fingerprints = tuple(value.fingerprint for value in values)
        if len(fingerprints) != len(set(fingerprints)):
            raise ValueError("identity rotation plan bindings must be unique")
        return values

    @model_validator(mode="after")
    def plan_must_be_complete_and_self_identifying(self) -> IdentityRotationPlan:
        if self.old_workspace_id == self.new_workspace_id:
            raise ValueError("identity rotation must change the workspace pseudonym")
        if not _opaque_id_matches(
            IdentityBindingKind.WORKSPACE,
            self.old_workspace_id,
            self.from_key_version,
        ) or not _opaque_id_matches(
            IdentityBindingKind.WORKSPACE,
            self.new_workspace_id,
            self.to_key_version,
        ):
            raise ValueError("identity rotation workspaces do not match their key versions")
        if any(
            not _opaque_id_matches(
                IdentityBindingKind.ACTOR,
                owner,
                self.from_key_version,
            )
            for owner in self.owner_actor_ids
        ):
            raise ValueError("identity rotation plan owner does not match its source key")
        if any(
            binding.workspace_reference_digest != self.workspace_reference_digest
            or binding.from_key_version != self.from_key_version
            or binding.to_key_version != self.to_key_version
            or binding.provenance_version != self.provenance_version
            or binding.policy_version != self.policy_version
            for binding in self.bindings
        ):
            raise ValueError("identity rotation binding differs from its plan policy")
        workspace_bindings = tuple(
            binding
            for binding in self.bindings
            if binding.binding_kind is IdentityBindingKind.WORKSPACE
        )
        actor_bindings = tuple(
            binding
            for binding in self.bindings
            if binding.binding_kind is IdentityBindingKind.ACTOR
        )
        if (
            len(workspace_bindings) != 1
            or workspace_bindings[0].old_opaque_id != self.old_workspace_id
            or workspace_bindings[0].new_opaque_id != self.new_workspace_id
        ):
            raise ValueError("identity rotation plan requires one exact workspace binding")
        if (
            len(actor_bindings) != len(self.owner_actor_ids)
            or tuple(sorted(binding.old_opaque_id for binding in actor_bindings))
            != self.owner_actor_ids
        ):
            raise ValueError("identity rotation plan lacks an exact owner binding")
        try:
            _validate_binding_graph((), self.bindings)
        except IdentityRotationInvariantError as error:
            raise ValueError(str(error)) from error
        if self.id != identity_rotation_plan_id(self):
            raise ValueError("identity rotation plan id does not match its content")
        return self

    @property
    def fingerprint(self) -> str:
        """Return the exact approved plan fingerprint."""

        return _fingerprint(self.model_dump(mode="json", exclude={"id"}))

    @property
    def expected_binding_count(self) -> int:
        return len(self.bindings)


class IdentityRotationApproval(FrozenDomainModel):
    """Approval bound to one exact deterministic rotation plan."""

    id: str = Field(pattern=r"^identity-rotation-approval-[0-9a-f]{64}$")
    plan: IdentityRotationPlan
    actor: str = Field(min_length=1, max_length=120)
    approved_at: datetime
    confirmation: IdentityRotationConfirmation

    @field_validator("actor")
    @classmethod
    def approver_must_be_inert(cls, value: str) -> str:
        if _SAFE_ACTOR.fullmatch(value) is None:
            raise ValueError("identity rotation approver must be an opaque inert id")
        return value

    @field_validator("approved_at")
    @classmethod
    def approval_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "identity rotation approval time")

    @model_validator(mode="after")
    def approval_must_identify_its_exact_plan(self) -> IdentityRotationApproval:
        if (
            self.confirmation is not IdentityRotationConfirmation.ROTATE_VERIFIED_OIDC_BINDINGS
            or self.id
            != identity_rotation_approval_id(
                self.plan,
                actor=self.actor,
                approved_at=self.approved_at,
                confirmation=self.confirmation,
            )
        ):
            raise ValueError("identity rotation approval does not match its exact plan")
        return self


class ApprovedIdentityRotation(FrozenDomainModel):
    """Exact plan and approval reservation returned by durable storage."""

    plan: IdentityRotationPlan
    approval: IdentityRotationApproval

    @model_validator(mode="after")
    def approval_must_bind_plan(self) -> ApprovedIdentityRotation:
        if self.approval.plan != self.plan:
            raise ValueError("approved identity rotation contains another plan")
        return self


class IdentityRotationCompletion(FrozenDomainModel):
    """Immutable completion fact; historical payload rewriting is impossible by contract."""

    id: str = Field(pattern=r"^identity-rotation-completion-[0-9a-f]{64}$")
    approved: ApprovedIdentityRotation
    completed_at: datetime
    historical_payloads_rewritten: Literal[False] = False

    @field_validator("completed_at")
    @classmethod
    def completion_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "identity rotation completion time")

    @model_validator(mode="after")
    def completion_must_follow_the_exact_approval(self) -> IdentityRotationCompletion:
        if self.completed_at < self.approved.approval.approved_at:
            raise ValueError("identity rotation completion cannot predate approval")
        if self.id != identity_rotation_completion_id(self.approved):
            raise ValueError("identity rotation completion id does not match its approval")
        return self

    @property
    def verified_binding_count(self) -> int:
        """Expose the exact count required by the durable completion constraint."""

        return self.approved.plan.expected_binding_count


class IdentityRotationExecutionResult(FrozenDomainModel):
    """Application-facing result that distinguishes an idempotent replay."""

    completion: IdentityRotationCompletion
    replayed: bool = False


class IdentityAuthorizationScope(FrozenDomainModel):
    """One same-key opaque workspace/actor pair eligible for grant lookup."""

    workspace_id: str = Field(min_length=1, max_length=200)
    actor_id: str = Field(min_length=1, max_length=200)
    key_version: str = Field(pattern=r"^v[1-9][0-9]{0,5}$")

    @model_validator(mode="after")
    def identities_must_share_the_declared_key(self) -> IdentityAuthorizationScope:
        if not _opaque_id_matches(
            IdentityBindingKind.WORKSPACE,
            self.workspace_id,
            self.key_version,
        ) or not _opaque_id_matches(
            IdentityBindingKind.ACTOR,
            self.actor_id,
            self.key_version,
        ):
            raise ValueError("identity authorization scope must contain same-key opaque ids")
        return self


def build_identity_initialization_approval(
    derivations: tuple[VerifiedDualKeyOidcDerivation, ...],
    *,
    evidence_fingerprint: str,
    actor: str,
    approved_at: datetime,
    confirmation: IdentityInitializationConfirmation,
) -> IdentityInitializationApproval:
    """Build the deterministic approval for one exact signed evidence payload."""

    if not derivations:
        _raise(
            IdentityRotationInvariantCode.DERIVATION_MISMATCH,
            "identity initialization requires verified OIDC derivations",
        )
    workspace_id = derivations[0].previous.workspace_id
    derivations_fingerprint = identity_initialization_derivations_fingerprint(derivations)
    approval_id = identity_initialization_approval_id(
        workspace_id=workspace_id,
        evidence_fingerprint=evidence_fingerprint,
        derivations_fingerprint=derivations_fingerprint,
        actor=actor,
        approved_at=approved_at,
        confirmation=confirmation,
    )
    approval = IdentityInitializationApproval(
        id=approval_id,
        workspace_id=workspace_id,
        evidence_fingerprint=evidence_fingerprint,
        derivations_fingerprint=derivations_fingerprint,
        actor=actor,
        approved_at=approved_at,
        confirmation=confirmation,
    )
    validate_identity_initialization_approval(derivations, approval)
    return approval


def validate_identity_initialization_approval(
    derivations: tuple[VerifiedDualKeyOidcDerivation, ...],
    approval: IdentityInitializationApproval,
) -> None:
    """Reject a forged approval or derivations swapped after evidence verification."""

    if not derivations:
        _raise(
            IdentityRotationInvariantCode.DERIVATION_MISMATCH,
            "identity initialization requires verified OIDC derivations",
        )
    try:
        validated = IdentityInitializationApproval.model_validate(
            approval.model_dump(mode="python", warnings=False)
        )
        fingerprint = identity_initialization_derivations_fingerprint(derivations)
    except (AttributeError, TypeError, ValueError) as error:
        raise IdentityRotationInvariantError(
            IdentityRotationInvariantCode.APPROVAL_MISMATCH,
            "identity initialization approval is invalid",
        ) from error
    first = derivations[0].previous
    if any(item.previous.workspace_id != first.workspace_id for item in derivations):
        _raise(
            IdentityRotationInvariantCode.CROSS_WORKSPACE,
            "identity initialization derivations cross workspace scope",
        )
    if (
        validated != approval
        or approval.workspace_id != first.workspace_id
        or approval.derivations_fingerprint != fingerprint
        or approval.approved_at < max(item.previous.verified_at for item in derivations)
    ):
        _raise(
            IdentityRotationInvariantCode.APPROVAL_MISMATCH,
            "identity initialization approval does not match the verified evidence",
        )


def identity_initialization_derivations_fingerprint(
    derivations: tuple[VerifiedDualKeyOidcDerivation, ...],
) -> str:
    """Fingerprint only validated transient derivations; persist the digest, not evidence."""

    if not derivations or len(derivations) > _MAX_OWNER_BINDINGS:
        raise ValueError("identity initialization derivations are outside the supported bound")
    canonical = tuple(
        VerifiedDualKeyOidcDerivation.model_validate(item.model_dump(mode="python", warnings=False))
        for item in derivations
    )
    if canonical != derivations:
        raise ValueError("identity initialization derivations are invalid")
    return _fingerprint([item.model_dump(mode="json", warnings=False) for item in canonical])


def identity_initialization_approval_id(
    *,
    workspace_id: str,
    evidence_fingerprint: str,
    derivations_fingerprint: str,
    actor: str,
    approved_at: datetime,
    confirmation: IdentityInitializationConfirmation,
) -> str:
    """Derive one immutable approval identity from all approved facts."""

    payload = {
        "workspace_id": workspace_id,
        "evidence_fingerprint": evidence_fingerprint,
        "derivations_fingerprint": derivations_fingerprint,
        "actor": actor,
        "approved_at": approved_at.isoformat(),
        "confirmation": confirmation.value,
    }
    return f"identity-initialization-approval-{_fingerprint(payload)}"


def build_identity_rotation_plan(
    state: IdentityRotationState,
    derivations: tuple[VerifiedDualKeyOidcDerivation, ...],
) -> IdentityRotationPlan:
    """Create one canonical plan or reject ambiguous identity evidence."""

    if not derivations:
        _raise(
            IdentityRotationInvariantCode.INCOMPLETE_OWNER_BINDING,
            "identity rotation requires a verified binding for every owner",
        )
    _validate_binding_graph((), state.historical_bindings)
    first = derivations[0]
    source = first.previous
    target = first.current
    if (
        source.workspace_id != state.workspace_id
        or source.workspace_reference_digest != state.workspace_reference_digest
    ):
        _raise(
            IdentityRotationInvariantCode.CROSS_WORKSPACE,
            "identity rotation derivations do not match the requested workspace",
        )
    if (
        source.key_version != state.active_key_version
        or source.provenance_version != state.provenance_version
        or source.policy_version != state.policy_version
    ):
        _raise(
            IdentityRotationInvariantCode.STALE_PLAN,
            "identity rotation evidence does not match the current identity policy",
        )
    if (
        target.key_version == state.active_key_version
        or target.key_version in state.previous_key_versions
    ):
        _raise(
            IdentityRotationInvariantCode.CYCLE,
            "identity rotation cannot reuse an active or historical key version",
        )
    old_actor_ids, canonical_bindings = _rotation_evidence_bindings(derivations)
    mapped_owners = frozenset(old_actor_ids)
    expected_owners = frozenset(state.owner_actor_ids)
    if mapped_owners != expected_owners:
        _raise(
            IdentityRotationInvariantCode.INCOMPLETE_OWNER_BINDING,
            "identity rotation requires exactly one new binding for every current owner",
        )
    _validate_binding_graph(state.historical_bindings, canonical_bindings)
    payload = {
        "old_workspace_id": source.workspace_id,
        "new_workspace_id": target.workspace_id,
        "workspace_reference_digest": source.workspace_reference_digest,
        "from_key_version": source.key_version,
        "to_key_version": target.key_version,
        "provenance_version": source.provenance_version,
        "policy_version": source.policy_version,
        "expected_state_revision": state.revision,
        "expected_state_fingerprint": state.fingerprint,
        "owner_actor_ids": tuple(sorted(old_actor_ids)),
        "bindings": canonical_bindings,
    }
    plan_id = _plan_id_from_payload(payload)
    return IdentityRotationPlan(id=plan_id, **payload)


def validate_identity_rotation_plan_against_derivations(
    plan: IdentityRotationPlan,
    derivations: tuple[VerifiedDualKeyOidcDerivation, ...],
) -> None:
    """Bind a stored approval to the exact verified mappings without fresh state."""

    old_actor_ids, expected_bindings = _rotation_evidence_bindings(derivations)
    source = derivations[0].previous
    target = derivations[0].current
    if (
        plan.old_workspace_id != source.workspace_id
        or plan.new_workspace_id != target.workspace_id
        or plan.workspace_reference_digest != source.workspace_reference_digest
        or plan.from_key_version != source.key_version
        or plan.to_key_version != target.key_version
        or plan.provenance_version != source.provenance_version
        or plan.policy_version != source.policy_version
        or plan.owner_actor_ids != tuple(sorted(old_actor_ids))
        or plan.bindings != expected_bindings
    ):
        _raise(
            IdentityRotationInvariantCode.DERIVATION_MISMATCH,
            "identity rotation plan does not match the verified derivations",
        )


def validate_identity_rotation_plan_against_state(
    plan: IdentityRotationPlan,
    state: IdentityRotationState,
) -> None:
    """Recheck owner completeness before any durable completion."""

    mapped_owners = frozenset(
        binding.old_opaque_id
        for binding in plan.bindings
        if binding.binding_kind is IdentityBindingKind.ACTOR
    )
    missing_owners = frozenset(state.owner_actor_ids) - mapped_owners
    if missing_owners:
        _raise(
            IdentityRotationInvariantCode.INCOMPLETE_OWNER_BINDING,
            "identity rotation cannot complete while a current owner lacks a new binding",
        )
    if (
        state.workspace_id != plan.old_workspace_id
        or state.workspace_reference_digest != plan.workspace_reference_digest
    ):
        _raise(
            IdentityRotationInvariantCode.CROSS_WORKSPACE,
            "identity rotation plan does not belong to the current workspace",
        )
    if (
        state.revision != plan.expected_state_revision
        or state.fingerprint != plan.expected_state_fingerprint
        or state.active_key_version != plan.from_key_version
        or state.provenance_version != plan.provenance_version
        or state.policy_version != plan.policy_version
        or frozenset(state.owner_actor_ids) != mapped_owners
    ):
        _raise(
            IdentityRotationInvariantCode.STALE_PLAN,
            "identity rotation state changed; prepare and approve a new plan",
        )
    if (
        plan.to_key_version == state.active_key_version
        or plan.to_key_version in state.previous_key_versions
    ):
        _raise(
            IdentityRotationInvariantCode.CYCLE,
            "identity rotation cannot reuse an active or historical key version",
        )
    _validate_binding_graph(state.historical_bindings, plan.bindings)


def _rotation_evidence_bindings(
    derivations: tuple[VerifiedDualKeyOidcDerivation, ...],
) -> tuple[tuple[str, ...], tuple[OpaqueIdentityRotationBinding, ...]]:
    if not derivations:
        _raise(
            IdentityRotationInvariantCode.INCOMPLETE_OWNER_BINDING,
            "identity rotation requires a verified binding for every owner",
        )
    source = derivations[0].previous
    target = derivations[0].current
    for derivation in derivations:
        previous = derivation.previous
        current = derivation.current
        if (
            previous.workspace_id != source.workspace_id
            or current.workspace_id != target.workspace_id
            or previous.workspace_reference_digest != source.workspace_reference_digest
            or current.workspace_reference_digest != target.workspace_reference_digest
        ):
            _raise(
                IdentityRotationInvariantCode.CROSS_WORKSPACE,
                "identity rotation cannot combine derivations from different workspaces",
            )
        if (
            previous.key_version != source.key_version
            or current.key_version != target.key_version
            or previous.provenance_version != source.provenance_version
            or current.provenance_version != target.provenance_version
            or previous.policy_version != source.policy_version
            or current.policy_version != target.policy_version
            or previous.provenance_fingerprint != source.provenance_fingerprint
            or current.provenance_fingerprint != target.provenance_fingerprint
        ):
            _raise(
                IdentityRotationInvariantCode.DERIVATION_MISMATCH,
                "identity rotation derivations do not share one key and policy transition",
            )

    actor_references = tuple(item.previous.actor_reference_digest for item in derivations)
    old_actor_ids = tuple(item.previous.actor_id for item in derivations)
    new_actor_ids = tuple(item.current.actor_id for item in derivations)
    if (
        len(actor_references) != len(set(actor_references))
        or len(old_actor_ids) != len(set(old_actor_ids))
        or len(new_actor_ids) != len(set(new_actor_ids))
    ):
        _raise(
            IdentityRotationInvariantCode.COLLISION,
            "identity rotation contains an opaque actor collision",
        )

    bindings = [
        OpaqueIdentityRotationBinding(
            binding_kind=IdentityBindingKind.WORKSPACE,
            workspace_reference_digest=source.workspace_reference_digest,
            stable_reference_digest=source.workspace_reference_digest,
            old_opaque_id=source.workspace_id,
            new_opaque_id=target.workspace_id,
            from_key_version=source.key_version,
            to_key_version=target.key_version,
            provenance_fingerprint=source.provenance_fingerprint,
            provenance_version=source.provenance_version,
            policy_version=source.policy_version,
        )
    ]
    bindings.extend(
        OpaqueIdentityRotationBinding(
            binding_kind=IdentityBindingKind.ACTOR,
            workspace_reference_digest=item.previous.workspace_reference_digest,
            stable_reference_digest=item.previous.actor_reference_digest,
            old_opaque_id=item.previous.actor_id,
            new_opaque_id=item.current.actor_id,
            from_key_version=item.previous.key_version,
            to_key_version=item.current.key_version,
            provenance_fingerprint=item.previous.provenance_fingerprint,
            provenance_version=item.previous.provenance_version,
            policy_version=item.previous.policy_version,
        )
        for item in derivations
    )
    return old_actor_ids, tuple(sorted(bindings, key=_binding_sort_key))


def build_identity_rotation_approval(
    plan: IdentityRotationPlan,
    *,
    actor: str,
    approved_at: datetime,
    confirmation: IdentityRotationConfirmation,
) -> IdentityRotationApproval:
    """Build the only approval identity accepted for an exact plan."""

    approval_id = identity_rotation_approval_id(
        plan,
        actor=actor,
        approved_at=approved_at,
        confirmation=confirmation,
    )
    return IdentityRotationApproval(
        id=approval_id,
        plan=plan,
        actor=actor,
        approved_at=approved_at,
        confirmation=confirmation,
    )


def validate_identity_rotation_approval(
    plan: IdentityRotationPlan,
    approval: IdentityRotationApproval,
) -> None:
    """Reject stale or rebound approval objects."""

    if approval.plan != plan or approval.id != identity_rotation_approval_id(
        plan,
        actor=approval.actor,
        approved_at=approval.approved_at,
        confirmation=approval.confirmation,
    ):
        _raise(
            IdentityRotationInvariantCode.APPROVAL_MISMATCH,
            "identity rotation approval does not match the exact plan",
        )


def build_identity_rotation_completion(
    approved: ApprovedIdentityRotation,
    *,
    completed_at: datetime,
) -> IdentityRotationCompletion:
    """Build an immutable completion that contains no historical payload."""

    validate_identity_rotation_approval(approved.plan, approved.approval)
    return IdentityRotationCompletion(
        id=identity_rotation_completion_id(approved),
        approved=approved,
        completed_at=completed_at,
    )


def identity_rotation_plan_id(plan: IdentityRotationPlan) -> str:
    """Derive the deterministic plan ID from all plan content except its ID."""

    return f"identity-rotation-{plan.fingerprint}"


def identity_rotation_approval_id(
    plan: IdentityRotationPlan,
    *,
    actor: str,
    approved_at: datetime,
    confirmation: IdentityRotationConfirmation,
) -> str:
    """Derive one idempotency identity for an exact human approval."""

    payload = {
        "plan_id": plan.id,
        "plan_fingerprint": plan.fingerprint,
        "actor": actor,
        "approved_at": approved_at.isoformat(),
        "confirmation": confirmation.value,
    }
    return f"identity-rotation-approval-{_fingerprint(payload)}"


def identity_rotation_completion_id(approved: ApprovedIdentityRotation) -> str:
    """Derive the immutable completion identity."""

    return (
        "identity-rotation-completion-"
        f"{_fingerprint({'plan_id': approved.plan.id, 'approval_id': approved.approval.id})}"
    )


def _plan_id_from_payload(payload: dict[str, object]) -> str:
    json_payload = {
        key: (
            [item.model_dump(mode="json") for item in value]
            if key == "bindings" and isinstance(value, tuple)
            else value
        )
        for key, value in payload.items()
    }
    return f"identity-rotation-{_fingerprint(json_payload)}"


def _validate_binding_graph(
    historical: tuple[OpaqueIdentityRotationBinding, ...],
    proposed: tuple[OpaqueIdentityRotationBinding, ...],
) -> None:
    node_references: dict[tuple[IdentityBindingKind, str], str] = {}
    edges: dict[tuple[IdentityBindingKind, str], str] = {}
    incoming: dict[tuple[IdentityBindingKind, str], str] = {}
    for binding in (*historical, *proposed):
        old_node = (binding.binding_kind, binding.old_opaque_id)
        new_node = (binding.binding_kind, binding.new_opaque_id)
        for node in (old_node, new_node):
            prior_reference = node_references.setdefault(
                node,
                binding.stable_reference_digest,
            )
            if prior_reference != binding.stable_reference_digest:
                _raise(
                    IdentityRotationInvariantCode.COLLISION,
                    "one opaque identity is bound to multiple stable references",
                )
        prior_target = edges.setdefault(old_node, binding.new_opaque_id)
        prior_source = incoming.setdefault(new_node, binding.old_opaque_id)
        if prior_target != binding.new_opaque_id or prior_source != binding.old_opaque_id:
            _raise(
                IdentityRotationInvariantCode.COLLISION,
                "identity rotation graph contains a conflicting opaque binding",
            )

    for start in edges:
        seen: set[tuple[IdentityBindingKind, str]] = set()
        current = start
        while current in edges:
            if current in seen:
                _raise(
                    IdentityRotationInvariantCode.CYCLE,
                    "identity rotation graph contains a cycle",
                )
            seen.add(current)
            current = (current[0], edges[current])


def _binding_sort_key(
    binding: OpaqueIdentityRotationBinding,
) -> tuple[str, str, str, str]:
    return (
        binding.binding_kind.value,
        binding.stable_reference_digest,
        binding.old_opaque_id,
        binding.new_opaque_id,
    )


def _opaque_id_matches(
    kind: IdentityBindingKind,
    value: str,
    key_version: str,
) -> bool:
    pattern = _SAFE_WORKSPACE if kind is IdentityBindingKind.WORKSPACE else _SAFE_ACTOR
    prefix = (
        f"sb_workspace_{key_version}_"
        if kind is IdentityBindingKind.WORKSPACE
        else f"sb_actor_{key_version}_"
    )
    return pattern.fullmatch(value) is not None and value.startswith(prefix)


def _fingerprint(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return value


def _raise(code: IdentityRotationInvariantCode, message: str) -> None:
    raise IdentityRotationInvariantError(code, message)
