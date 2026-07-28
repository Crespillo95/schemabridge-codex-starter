"""Pure orchestration for approval-gated dual-key identity rotation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from schemabridge.application.ports.identity_rotation import (
    IdentityRotationStoreError,
    IdentityRotationStoreErrorCode,
    IdentityRotationStorePort,
)
from schemabridge.domain.identity_rotation import (
    ApprovedIdentityRotation,
    IdentityInitializationConfirmation,
    IdentityRotationApproval,
    IdentityRotationCompletion,
    IdentityRotationConfirmation,
    IdentityRotationExecutionResult,
    IdentityRotationInvariantCode,
    IdentityRotationInvariantError,
    IdentityRotationPlan,
    IdentityRotationState,
    VerifiedDualKeyOidcDerivation,
    build_identity_initialization_approval,
    build_identity_rotation_approval,
    build_identity_rotation_completion,
    build_identity_rotation_plan,
    validate_identity_initialization_approval,
    validate_identity_rotation_approval,
    validate_identity_rotation_plan_against_derivations,
    validate_identity_rotation_plan_against_state,
)


class IdentityRotationErrorCode(StrEnum):
    """Stable failures safe for an entrypoint boundary."""

    DERIVATION_MISMATCH = "identity_rotation_derivation_mismatch"
    CROSS_WORKSPACE = "identity_rotation_cross_workspace"
    COLLISION = "identity_rotation_collision"
    CYCLE = "identity_rotation_cycle"
    INCOMPLETE_OWNER_BINDING = "identity_rotation_incomplete_owner_binding"
    STALE_PLAN = "identity_rotation_stale_plan"
    APPROVAL_MISMATCH = "identity_rotation_approval_mismatch"
    APPROVAL_NOT_RESERVED = "identity_rotation_approval_not_reserved"
    STORE_CONFLICT = "identity_rotation_store_conflict"
    STORE_UNAVAILABLE = "identity_rotation_store_unavailable"
    INVALID_RESPONSE = "identity_rotation_invalid_response"


class IdentityRotationError(RuntimeError):
    """A sanitized application-level identity-rotation failure."""

    def __init__(self, code: IdentityRotationErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class InitializeVerifiedIdentityState:
    """Initialize opaque bindings from one explicitly approved signed envelope."""

    store: IdentityRotationStorePort

    def execute(
        self,
        derivations: tuple[VerifiedDualKeyOidcDerivation, ...],
        *,
        evidence_fingerprint: str,
        actor: str,
        approved_at: datetime,
        confirmation: IdentityInitializationConfirmation,
    ) -> IdentityRotationState:
        try:
            approval = build_identity_initialization_approval(
                derivations,
                evidence_fingerprint=evidence_fingerprint,
                actor=actor,
                approved_at=approved_at,
                confirmation=confirmation,
            )
            validate_identity_initialization_approval(derivations, approval)
        except IdentityRotationInvariantError as error:
            raise _domain_error(error) from error
        except ValueError as error:
            raise IdentityRotationError(
                IdentityRotationErrorCode.APPROVAL_MISMATCH,
                "identity initialization approval does not match the verified evidence",
            ) from error
        try:
            returned = self.store.initialize_verified_state(derivations, approval)
        except IdentityRotationStoreError as error:
            raise _store_error(error) from error
        return _validated_initialization_state(returned, derivations)


@dataclass(frozen=True, slots=True)
class PrepareIdentityRotation:
    """Prepare an idempotent all-owner plan from verified dual-key evidence."""

    store: IdentityRotationStorePort

    def execute(
        self,
        workspace_id: str,
        derivations: tuple[VerifiedDualKeyOidcDerivation, ...],
    ) -> IdentityRotationPlan:
        state = _load_state(self.store, workspace_id)
        try:
            return build_identity_rotation_plan(state, derivations)
        except IdentityRotationInvariantError as error:
            raise _domain_error(error) from error


@dataclass(frozen=True, slots=True)
class ApproveIdentityRotation:
    """Create and durably reserve one exact approval identity."""

    store: IdentityRotationStorePort

    def execute(
        self,
        plan: IdentityRotationPlan,
        *,
        actor: str,
        approved_at: datetime,
        confirmation: IdentityRotationConfirmation,
    ) -> ApprovedIdentityRotation:
        try:
            approval = build_identity_rotation_approval(
                plan,
                actor=actor,
                approved_at=approved_at,
                confirmation=confirmation,
            )
            approved = ApprovedIdentityRotation(plan=plan, approval=approval)
        except ValueError as error:
            raise IdentityRotationError(
                IdentityRotationErrorCode.APPROVAL_MISMATCH,
                "identity rotation approval does not match the exact plan",
            ) from error
        try:
            returned = self.store.reserve_approved_plan(approved)
        except IdentityRotationStoreError as error:
            raise _store_error(error) from error
        validated = _validated_approved(returned)
        if validated != approved:
            raise IdentityRotationError(
                IdentityRotationErrorCode.INVALID_RESPONSE,
                "identity rotation store returned another approval reservation",
            )
        return validated


@dataclass(frozen=True, slots=True)
class ResolveReservedIdentityRotation:
    """Resolve one immutable approval without requiring the source binding to stay active."""

    store: IdentityRotationStorePort

    def execute(
        self,
        plan_fingerprint: str,
        derivations: tuple[VerifiedDualKeyOidcDerivation, ...],
    ) -> IdentityRotationPlan:
        if len(plan_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in plan_fingerprint
        ):
            raise IdentityRotationError(
                IdentityRotationErrorCode.APPROVAL_MISMATCH,
                "identity rotation plan fingerprint is invalid",
            )
        stored = _load_approved(
            self.store,
            f"identity-rotation-{plan_fingerprint}",
        )
        if stored is None:
            raise IdentityRotationError(
                IdentityRotationErrorCode.APPROVAL_NOT_RESERVED,
                "identity rotation approval has not been durably reserved",
            )
        if stored.plan.fingerprint != plan_fingerprint:
            raise IdentityRotationError(
                IdentityRotationErrorCode.INVALID_RESPONSE,
                "identity rotation store returned another approved plan",
            )
        try:
            validate_identity_rotation_plan_against_derivations(
                stored.plan,
                derivations,
            )
        except IdentityRotationInvariantError as error:
            raise _domain_error(error) from error
        return stored.plan


@dataclass(frozen=True, slots=True)
class CompleteIdentityRotation:
    """Complete all bindings atomically without receiving historical payloads."""

    store: IdentityRotationStorePort

    def execute(
        self,
        plan: IdentityRotationPlan,
        approval: IdentityRotationApproval,
        *,
        completed_at: datetime,
    ) -> IdentityRotationExecutionResult:
        try:
            validate_identity_rotation_approval(plan, approval)
            approved = ApprovedIdentityRotation(plan=plan, approval=approval)
        except (IdentityRotationInvariantError, ValueError) as error:
            raise IdentityRotationError(
                IdentityRotationErrorCode.APPROVAL_MISMATCH,
                "identity rotation approval does not match the exact plan",
            ) from error

        stored = _load_approved(self.store, plan.id)
        if stored is None:
            raise IdentityRotationError(
                IdentityRotationErrorCode.APPROVAL_NOT_RESERVED,
                "identity rotation approval has not been durably reserved",
            )
        if stored != approved:
            raise IdentityRotationError(
                IdentityRotationErrorCode.APPROVAL_MISMATCH,
                "stored identity rotation approval does not match the exact plan",
            )

        existing = _load_completion(self.store, plan.id)
        if existing is not None:
            if existing.approved != approved:
                raise IdentityRotationError(
                    IdentityRotationErrorCode.INVALID_RESPONSE,
                    "stored identity rotation completion belongs to another approval",
                )
            return IdentityRotationExecutionResult(
                completion=existing,
                replayed=True,
            )

        state = _load_state(self.store, plan.old_workspace_id)
        try:
            validate_identity_rotation_plan_against_state(plan, state)
            expected = build_identity_rotation_completion(
                approved,
                completed_at=completed_at,
            )
        except IdentityRotationInvariantError as error:
            raise _domain_error(error) from error
        except ValueError as error:
            raise IdentityRotationError(
                IdentityRotationErrorCode.APPROVAL_MISMATCH,
                "identity rotation completion facts are invalid",
            ) from error

        try:
            returned = self.store.complete_rotation(expected)
        except IdentityRotationStoreError as error:
            raise _store_error(error) from error
        validated = _validated_completion(returned)
        if validated.approved != expected.approved:
            raise IdentityRotationError(
                IdentityRotationErrorCode.INVALID_RESPONSE,
                "identity rotation store returned another completion",
            )
        return IdentityRotationExecutionResult(
            completion=validated,
            replayed=validated != expected,
        )


@dataclass(frozen=True, slots=True)
class CompleteReservedIdentityRotation:
    """Complete only the exact approval already reserved in durable storage."""

    store: IdentityRotationStorePort

    def execute(
        self,
        plan: IdentityRotationPlan,
        *,
        approval_id: str,
        actor: str,
        completed_at: datetime,
    ) -> IdentityRotationExecutionResult:
        stored = _load_approved(self.store, plan.id)
        if stored is None:
            raise IdentityRotationError(
                IdentityRotationErrorCode.APPROVAL_NOT_RESERVED,
                "identity rotation approval has not been durably reserved",
            )
        if (
            stored.plan != plan
            or stored.approval.id != approval_id
            or stored.approval.actor != actor
        ):
            raise IdentityRotationError(
                IdentityRotationErrorCode.APPROVAL_MISMATCH,
                "stored identity rotation approval does not match the exact plan",
            )
        return CompleteIdentityRotation(self.store).execute(
            plan,
            stored.approval,
            completed_at=completed_at,
        )


def _load_state(
    store: IdentityRotationStorePort,
    workspace_id: str,
) -> IdentityRotationState:
    try:
        returned = store.load_state(workspace_id)
    except IdentityRotationStoreError as error:
        raise _store_error(error) from error
    try:
        validated = IdentityRotationState.model_validate(
            returned.model_dump(mode="python", warnings=False)
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise IdentityRotationError(
            IdentityRotationErrorCode.INVALID_RESPONSE,
            "identity rotation store returned an invalid state",
        ) from error
    if validated != returned or validated.workspace_id != workspace_id:
        raise IdentityRotationError(
            IdentityRotationErrorCode.INVALID_RESPONSE,
            "identity rotation store returned state outside the requested workspace",
        )
    return validated


def _validated_initialization_state(
    value: object,
    derivations: tuple[VerifiedDualKeyOidcDerivation, ...],
) -> IdentityRotationState:
    try:
        dumped = value.model_dump(mode="python", warnings=False)  # type: ignore[attr-defined]
        validated = IdentityRotationState.model_validate(dumped)
    except (AttributeError, TypeError, ValueError) as error:
        raise IdentityRotationError(
            IdentityRotationErrorCode.INVALID_RESPONSE,
            "identity initialization store returned an invalid state",
        ) from error
    source = derivations[0].previous
    if (
        validated != value
        or validated.workspace_reference_digest != source.workspace_reference_digest
        or validated.provenance_version != source.provenance_version
        or validated.policy_version != source.policy_version
    ):
        raise IdentityRotationError(
            IdentityRotationErrorCode.INVALID_RESPONSE,
            "identity initialization store returned another identity lineage",
        )
    return validated


def _load_approved(
    store: IdentityRotationStorePort,
    plan_id: str,
) -> ApprovedIdentityRotation | None:
    try:
        returned = store.load_approved_plan(plan_id)
    except IdentityRotationStoreError as error:
        raise _store_error(error) from error
    if returned is None:
        return None
    return _validated_approved(returned)


def _load_completion(
    store: IdentityRotationStorePort,
    plan_id: str,
) -> IdentityRotationCompletion | None:
    try:
        returned = store.load_completion(plan_id)
    except IdentityRotationStoreError as error:
        raise _store_error(error) from error
    if returned is None:
        return None
    return _validated_completion(returned)


def _validated_approved(value: object) -> ApprovedIdentityRotation:
    try:
        dumped = value.model_dump(mode="python", warnings=False)  # type: ignore[attr-defined]
        validated = ApprovedIdentityRotation.model_validate(dumped)
    except (AttributeError, TypeError, ValueError) as error:
        raise IdentityRotationError(
            IdentityRotationErrorCode.INVALID_RESPONSE,
            "identity rotation store returned an invalid approval reservation",
        ) from error
    return validated


def _validated_completion(value: object) -> IdentityRotationCompletion:
    try:
        dumped = value.model_dump(mode="python", warnings=False)  # type: ignore[attr-defined]
        validated = IdentityRotationCompletion.model_validate(dumped)
    except (AttributeError, TypeError, ValueError) as error:
        raise IdentityRotationError(
            IdentityRotationErrorCode.INVALID_RESPONSE,
            "identity rotation store returned an invalid completion",
        ) from error
    return validated


def _domain_error(error: IdentityRotationInvariantError) -> IdentityRotationError:
    mapping = {
        IdentityRotationInvariantCode.DERIVATION_MISMATCH: (
            IdentityRotationErrorCode.DERIVATION_MISMATCH
        ),
        IdentityRotationInvariantCode.CROSS_WORKSPACE: (IdentityRotationErrorCode.CROSS_WORKSPACE),
        IdentityRotationInvariantCode.COLLISION: IdentityRotationErrorCode.COLLISION,
        IdentityRotationInvariantCode.CYCLE: IdentityRotationErrorCode.CYCLE,
        IdentityRotationInvariantCode.INCOMPLETE_OWNER_BINDING: (
            IdentityRotationErrorCode.INCOMPLETE_OWNER_BINDING
        ),
        IdentityRotationInvariantCode.STALE_PLAN: IdentityRotationErrorCode.STALE_PLAN,
        IdentityRotationInvariantCode.APPROVAL_MISMATCH: (
            IdentityRotationErrorCode.APPROVAL_MISMATCH
        ),
    }
    return IdentityRotationError(mapping[error.code], str(error))


def _store_error(error: IdentityRotationStoreError) -> IdentityRotationError:
    code = {
        IdentityRotationStoreErrorCode.CONFLICT: (IdentityRotationErrorCode.STORE_CONFLICT),
        IdentityRotationStoreErrorCode.UNAVAILABLE: (IdentityRotationErrorCode.STORE_UNAVAILABLE),
        IdentityRotationStoreErrorCode.CROSS_WORKSPACE: (IdentityRotationErrorCode.CROSS_WORKSPACE),
        IdentityRotationStoreErrorCode.COLLISION: IdentityRotationErrorCode.COLLISION,
        IdentityRotationStoreErrorCode.CYCLE: IdentityRotationErrorCode.CYCLE,
        IdentityRotationStoreErrorCode.INCOMPLETE_OWNER_BINDING: (
            IdentityRotationErrorCode.INCOMPLETE_OWNER_BINDING
        ),
        IdentityRotationStoreErrorCode.STALE_PLAN: (IdentityRotationErrorCode.STALE_PLAN),
    }[error.code]
    return IdentityRotationError(code, "identity rotation store operation failed")
