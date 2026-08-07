from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from schemabridge.application.identity_rotation import (
    ApproveIdentityRotation,
    CompleteIdentityRotation,
    CompleteReservedIdentityRotation,
    IdentityRotationError,
    IdentityRotationErrorCode,
    InitializeVerifiedIdentityState,
    PrepareIdentityRotation,
    ResolveReservedIdentityRotation,
)
from schemabridge.application.ports.identity_rotation import (
    IdentityRotationStoreError,
    IdentityRotationStoreErrorCode,
)
from schemabridge.domain.identity import AuthenticationMethod
from schemabridge.domain.identity_rotation import (
    ApprovedIdentityRotation,
    IdentityBindingKind,
    IdentityInitializationApproval,
    IdentityInitializationConfirmation,
    IdentityRotationCompletion,
    IdentityRotationConfirmation,
    IdentityRotationPlan,
    IdentityRotationState,
    OpaqueIdentityRotationBinding,
    VerifiedDualKeyOidcDerivation,
    VerifiedOidcKeyDerivation,
)

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
PROVENANCE_FINGERPRINT = hashlib.sha256(b"oidc-provenance-v1").hexdigest()
WORKSPACE_REFERENCE = hashlib.sha256(b"workspace-reference").hexdigest()


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _opaque(kind: str, key_version: str, label: str) -> str:
    return f"sb_{kind}_{key_version}_{_digest(f'{key_version}:{label}')}"


def _key_derivation(
    owner: str,
    key_version: str,
    *,
    workspace: str = "workspace-a",
    workspace_reference: str = WORKSPACE_REFERENCE,
    actor_reference: str | None = None,
    actor_id: str | None = None,
    provenance_fingerprint: str = PROVENANCE_FINGERPRINT,
    policy_version: int = 1,
    authentication_method: AuthenticationMethod = AuthenticationMethod.OIDC,
) -> VerifiedOidcKeyDerivation:
    return VerifiedOidcKeyDerivation(
        verification_id=_digest(f"verification:{owner}"),
        workspace_reference_digest=workspace_reference,
        actor_reference_digest=actor_reference or _digest(f"actor-reference:{owner}"),
        workspace_id=_opaque("workspace", key_version, workspace),
        actor_id=actor_id or _opaque("actor", key_version, owner),
        key_version=key_version,
        provenance_fingerprint=provenance_fingerprint,
        provenance_version=1,
        policy_version=policy_version,
        verified_at=NOW,
        authentication_method=authentication_method,
    )


def _pair(
    owner: str,
    *,
    from_key: str = "v1",
    to_key: str = "v2",
    workspace: str = "workspace-a",
    workspace_reference: str = WORKSPACE_REFERENCE,
    new_actor_id: str | None = None,
    provenance_fingerprint: str = PROVENANCE_FINGERPRINT,
    policy_version: int = 1,
) -> VerifiedDualKeyOidcDerivation:
    return VerifiedDualKeyOidcDerivation(
        previous=_key_derivation(
            owner,
            from_key,
            workspace=workspace,
            workspace_reference=workspace_reference,
            provenance_fingerprint=provenance_fingerprint,
            policy_version=policy_version,
        ),
        current=_key_derivation(
            owner,
            to_key,
            workspace=workspace,
            workspace_reference=workspace_reference,
            actor_id=new_actor_id,
            provenance_fingerprint=provenance_fingerprint,
            policy_version=policy_version,
        ),
    )


def _state(
    *owners: str,
    active_key: str = "v1",
    workspace: str = "workspace-a",
    workspace_reference: str = WORKSPACE_REFERENCE,
    revision: int = 1,
    previous_key_versions: tuple[str, ...] = (),
    history: tuple[OpaqueIdentityRotationBinding, ...] = (),
) -> IdentityRotationState:
    return IdentityRotationState(
        workspace_id=_opaque("workspace", active_key, workspace),
        workspace_reference_digest=workspace_reference,
        active_key_version=active_key,
        provenance_version=1,
        policy_version=1,
        revision=revision,
        owner_actor_ids=tuple(sorted(_opaque("actor", active_key, owner) for owner in owners)),
        previous_key_versions=previous_key_versions,
        historical_bindings=tuple(
            sorted(
                history,
                key=lambda item: (
                    item.binding_kind.value,
                    item.stable_reference_digest,
                    item.old_opaque_id,
                    item.new_opaque_id,
                ),
            )
        ),
    )


class InMemoryIdentityRotationStore:
    def __init__(self, state: IdentityRotationState) -> None:
        self.state = state
        self.initializations: list[
            tuple[
                tuple[VerifiedDualKeyOidcDerivation, ...],
                IdentityInitializationApproval,
            ]
        ] = []
        self.approved: dict[str, ApprovedIdentityRotation] = {}
        self.completions: dict[str, IdentityRotationCompletion] = {}
        self.complete_calls = 0
        self.historical_payload = b'{"actor":"immutable-v1","rows":[1,2,3]}'

    def initialize_verified_state(
        self,
        derivations: tuple[VerifiedDualKeyOidcDerivation, ...],
        approval: IdentityInitializationApproval,
    ) -> IdentityRotationState:
        self.initializations.append((derivations, approval))
        return self.state

    def load_state(self, workspace_id: str) -> IdentityRotationState:
        return self.state

    def reserve_approved_plan(
        self,
        approved: ApprovedIdentityRotation,
    ) -> ApprovedIdentityRotation:
        current = self.approved.get(approved.plan.id)
        if current is not None and current != approved:
            raise IdentityRotationStoreError(
                IdentityRotationStoreErrorCode.CONFLICT,
                "approval identity is already reserved",
            )
        self.approved[approved.plan.id] = approved
        return approved

    def load_approved_plan(self, plan_id: str) -> ApprovedIdentityRotation | None:
        return self.approved.get(plan_id)

    def load_completion(self, plan_id: str) -> IdentityRotationCompletion | None:
        return self.completions.get(plan_id)

    def complete_rotation(
        self,
        completion: IdentityRotationCompletion,
    ) -> IdentityRotationCompletion:
        self.complete_calls += 1
        current = self.completions.get(completion.approved.plan.id)
        if current is not None and current != completion:
            raise IdentityRotationStoreError(
                IdentityRotationStoreErrorCode.CONFLICT,
                "rotation completion is already recorded",
            )
        self.completions[completion.approved.plan.id] = completion
        return completion


def _prepare(
    store: InMemoryIdentityRotationStore,
    *pairs: VerifiedDualKeyOidcDerivation,
) -> IdentityRotationPlan:
    return PrepareIdentityRotation(store).execute(
        store.state.workspace_id,
        tuple(pairs),
    )


def _approve(
    store: InMemoryIdentityRotationStore,
    plan: IdentityRotationPlan,
    *,
    actor: str | None = None,
    approved_at: datetime = NOW + timedelta(minutes=1),
) -> ApprovedIdentityRotation:
    return ApproveIdentityRotation(store).execute(
        plan,
        actor=actor or store.state.owner_actor_ids[0],
        approved_at=approved_at,
        confirmation=IdentityRotationConfirmation.ROTATE_VERIFIED_OIDC_BINDINGS,
    )


def test_initialization_is_approval_bound_deterministic_and_privacy_preserving() -> None:
    pairs = (_pair("owner-a"), _pair("owner-b"))
    store = InMemoryIdentityRotationStore(_state("owner-a", "owner-b", revision=0))
    initialize = InitializeVerifiedIdentityState(store)
    evidence_fingerprint = _digest("signed-identity-evidence")
    actor = _opaque("actor", "v1", "initialization-operator")

    first = initialize.execute(
        pairs,
        evidence_fingerprint=evidence_fingerprint,
        actor=actor,
        approved_at=NOW,
        confirmation=(IdentityInitializationConfirmation.INITIALIZE_VERIFIED_OIDC_BINDINGS),
    )
    replay = initialize.execute(
        pairs,
        evidence_fingerprint=evidence_fingerprint,
        actor=actor,
        approved_at=NOW,
        confirmation=(IdentityInitializationConfirmation.INITIALIZE_VERIFIED_OIDC_BINDINGS),
    )

    assert first == replay == store.state
    assert len(store.initializations) == 2
    approval = store.initializations[0][1]
    assert store.initializations[1][1] == approval
    assert approval.workspace_id == pairs[0].previous.workspace_id
    assert approval.evidence_fingerprint == evidence_fingerprint
    rendered = approval.model_dump_json()
    for forbidden in (
        "verification_id",
        "authentication_method",
        "email",
        "subject",
        "claims",
        "token",
    ):
        assert forbidden not in rendered

    forged = approval.model_dump(mode="python")
    forged["actor"] = _opaque("actor", "v1", "another-operator")
    with pytest.raises(ValidationError, match="approval does not match its facts"):
        IdentityInitializationApproval.model_validate(forged)


def test_initialization_rejects_cross_workspace_evidence_before_store_io() -> None:
    store = InMemoryIdentityRotationStore(_state("owner-a", "owner-b", revision=0))

    with pytest.raises(IdentityRotationError) as raised:
        InitializeVerifiedIdentityState(store).execute(
            (
                _pair("owner-a"),
                _pair(
                    "owner-b",
                    workspace="workspace-b",
                    workspace_reference=_digest("workspace-b-reference"),
                ),
            ),
            evidence_fingerprint=_digest("signed-identity-evidence"),
            actor=_opaque("actor", "v1", "initialization-operator"),
            approved_at=NOW,
            confirmation=(IdentityInitializationConfirmation.INITIALIZE_VERIFIED_OIDC_BINDINGS),
        )

    assert raised.value.code is IdentityRotationErrorCode.CROSS_WORKSPACE
    assert store.initializations == []


def test_initialization_rejects_a_response_from_another_identity_lineage() -> None:
    pair = _pair("owner-a")
    store = InMemoryIdentityRotationStore(
        _state(
            "owner-a",
            workspace_reference=_digest("another-workspace-reference"),
            revision=0,
        )
    )

    with pytest.raises(IdentityRotationError) as raised:
        InitializeVerifiedIdentityState(store).execute(
            (pair,),
            evidence_fingerprint=_digest("signed-identity-evidence"),
            actor=_opaque("actor", "v1", "initialization-operator"),
            approved_at=NOW,
            confirmation=(IdentityInitializationConfirmation.INITIALIZE_VERIFIED_OIDC_BINDINGS),
        )

    assert raised.value.code is IdentityRotationErrorCode.INVALID_RESPONSE


def test_exact_all_owner_plan_is_idempotent_opaque_and_replay_safe() -> None:
    store = InMemoryIdentityRotationStore(_state("owner-a", "owner-b"))
    first = _prepare(store, _pair("owner-b"), _pair("owner-a"))
    repeated = _prepare(store, _pair("owner-a"), _pair("owner-b"))

    assert repeated == first
    assert first.expected_binding_count == 3
    assert [binding.binding_kind for binding in first.bindings] == [
        IdentityBindingKind.ACTOR,
        IdentityBindingKind.ACTOR,
        IdentityBindingKind.WORKSPACE,
    ]
    serialized = first.model_dump_json()
    assert "verification_id" not in serialized
    assert "subject" not in serialized
    assert "tenant" not in serialized
    assert "email" not in serialized
    assert "claims" not in serialized
    assert "token" not in serialized

    approved = _approve(store, first)
    same_approval = _approve(store, first)
    historical_before = store.historical_payload
    completed = CompleteIdentityRotation(store).execute(
        first,
        approved.approval,
        completed_at=NOW + timedelta(minutes=2),
    )
    replayed = CompleteIdentityRotation(store).execute(
        first,
        same_approval.approval,
        completed_at=NOW + timedelta(minutes=10),
    )

    assert approved == same_approval
    assert completed.replayed is False
    assert replayed.replayed is True
    assert replayed.completion == completed.completion
    assert completed.completion.verified_binding_count == 3
    assert completed.completion.historical_payloads_rewritten is False
    assert store.complete_calls == 1
    assert store.historical_payload == historical_before


def test_reserved_completion_accepts_only_the_exact_durable_approval_id() -> None:
    store = InMemoryIdentityRotationStore(_state("owner-a"))
    plan = _prepare(store, _pair("owner-a"))
    approved = _approve(store, plan)
    complete = CompleteReservedIdentityRotation(store)

    with pytest.raises(IdentityRotationError) as mismatch:
        complete.execute(
            plan,
            approval_id="identity-rotation-approval-" + "0" * 64,
            actor=approved.approval.actor,
            completed_at=NOW + timedelta(minutes=2),
        )
    assert mismatch.value.code is IdentityRotationErrorCode.APPROVAL_MISMATCH
    assert store.complete_calls == 0

    first = complete.execute(
        plan,
        approval_id=approved.approval.id,
        actor=approved.approval.actor,
        completed_at=NOW + timedelta(minutes=2),
    )
    replay = complete.execute(
        plan,
        approval_id=approved.approval.id,
        actor=approved.approval.actor,
        completed_at=NOW + timedelta(minutes=10),
    )
    assert first.replayed is False
    assert replay.replayed is True
    assert replay.completion == first.completion


def test_reserved_plan_resolution_uses_exact_fingerprint_and_evidence_without_fresh_state() -> None:
    pair = _pair("owner-a")
    source_store = InMemoryIdentityRotationStore(_state("owner-a"))
    plan = _prepare(source_store, pair)
    approved = _approve(source_store, plan)

    class NoFreshStateStore(InMemoryIdentityRotationStore):
        def load_state(self, workspace_id: str) -> IdentityRotationState:
            raise AssertionError("reserved-plan replay must not load mutable current state")

    replay_store = NoFreshStateStore(source_store.state)
    replay_store.approved[plan.id] = approved
    resolver = ResolveReservedIdentityRotation(replay_store)

    assert resolver.execute(plan.fingerprint, (pair,)) == plan

    with pytest.raises(IdentityRotationError) as mismatched_evidence:
        resolver.execute(plan.fingerprint, (_pair("owner-b"),))
    assert mismatched_evidence.value.code is IdentityRotationErrorCode.DERIVATION_MISMATCH

    with pytest.raises(IdentityRotationError) as invalid_fingerprint:
        resolver.execute("0" * 63, (pair,))
    assert invalid_fingerprint.value.code is IdentityRotationErrorCode.APPROVAL_MISMATCH


@pytest.mark.parametrize(
    "mutate",
    [
        "verification",
        "actor_reference",
        "workspace_reference",
        "provenance",
        "policy",
    ],
)
def test_dual_key_derivations_require_the_same_verified_oidc_identity(
    mutate: str,
) -> None:
    previous = _key_derivation("owner-a", "v1")
    values = previous.model_dump(mode="python")
    values.update(
        {
            "workspace_id": _opaque("workspace", "v2", "workspace-a"),
            "actor_id": _opaque("actor", "v2", "owner-a"),
            "key_version": "v2",
        }
    )
    if mutate == "verification":
        values["verification_id"] = _digest("different-verification")
    elif mutate == "actor_reference":
        values["actor_reference_digest"] = _digest("different-actor")
    elif mutate == "workspace_reference":
        values["workspace_reference_digest"] = _digest("different-workspace")
    elif mutate == "provenance":
        values["provenance_fingerprint"] = _digest("different-provenance")
    else:
        values["policy_version"] = 2
    current = VerifiedOidcKeyDerivation.model_validate(values)

    with pytest.raises(ValidationError, match="same verified OIDC identity"):
        VerifiedDualKeyOidcDerivation(previous=previous, current=current)


def test_non_oidc_derivation_is_never_rotation_evidence() -> None:
    with pytest.raises(ValidationError, match="verified OIDC"):
        _key_derivation(
            "owner-a",
            "v1",
            authentication_method=AuthenticationMethod.LOCAL_DEMO,
        )


def test_prepare_rejects_two_owners_colliding_on_one_new_actor() -> None:
    store = InMemoryIdentityRotationStore(_state("owner-a", "owner-b"))
    collision = _opaque("actor", "v2", "collision")

    with pytest.raises(IdentityRotationError) as failure:
        _prepare(
            store,
            _pair("owner-a", new_actor_id=collision),
            _pair("owner-b", new_actor_id=collision),
        )

    assert failure.value.code is IdentityRotationErrorCode.COLLISION


def test_prepare_rejects_cross_workspace_derivations() -> None:
    store = InMemoryIdentityRotationStore(_state("owner-a", "owner-b"))

    with pytest.raises(IdentityRotationError) as failure:
        _prepare(
            store,
            _pair("owner-a"),
            _pair(
                "owner-b",
                workspace="workspace-b",
                workspace_reference=_digest("workspace-b-reference"),
            ),
        )

    assert failure.value.code is IdentityRotationErrorCode.CROSS_WORKSPACE


def test_prepare_rejects_mixed_provenance_across_owner_bindings() -> None:
    store = InMemoryIdentityRotationStore(_state("owner-a", "owner-b"))

    with pytest.raises(IdentityRotationError) as failure:
        _prepare(
            store,
            _pair("owner-a"),
            _pair(
                "owner-b",
                provenance_fingerprint=_digest("another-oidc-provenance"),
            ),
        )

    assert failure.value.code is IdentityRotationErrorCode.DERIVATION_MISMATCH


def test_prepare_rejects_reuse_of_a_historical_key_version_as_a_cycle() -> None:
    store = InMemoryIdentityRotationStore(
        _state(
            "owner-a",
            active_key="v2",
            previous_key_versions=("v1",),
        )
    )

    with pytest.raises(IdentityRotationError) as failure:
        _prepare(store, _pair("owner-a", from_key="v2", to_key="v1"))

    assert failure.value.code is IdentityRotationErrorCode.CYCLE


def test_prepare_rejects_a_cycle_already_present_in_binding_history() -> None:
    actor_reference = _digest("actor-reference:owner-a")
    old_actor = _opaque("actor", "v1", "owner-a")
    current_actor = _opaque("actor", "v2", "owner-a")
    forward = OpaqueIdentityRotationBinding(
        binding_kind=IdentityBindingKind.ACTOR,
        workspace_reference_digest=WORKSPACE_REFERENCE,
        stable_reference_digest=actor_reference,
        old_opaque_id=old_actor,
        new_opaque_id=current_actor,
        from_key_version="v1",
        to_key_version="v2",
        provenance_fingerprint=PROVENANCE_FINGERPRINT,
        provenance_version=1,
        policy_version=1,
    )
    reverse = OpaqueIdentityRotationBinding(
        binding_kind=IdentityBindingKind.ACTOR,
        workspace_reference_digest=WORKSPACE_REFERENCE,
        stable_reference_digest=actor_reference,
        old_opaque_id=current_actor,
        new_opaque_id=old_actor,
        from_key_version="v2",
        to_key_version="v1",
        provenance_fingerprint=PROVENANCE_FINGERPRINT,
        provenance_version=1,
        policy_version=1,
    )
    store = InMemoryIdentityRotationStore(
        _state(
            "owner-a",
            active_key="v2",
            previous_key_versions=("v1",),
            history=(forward, reverse),
        )
    )

    with pytest.raises(IdentityRotationError) as failure:
        _prepare(store, _pair("owner-a", from_key="v2", to_key="v3"))

    assert failure.value.code is IdentityRotationErrorCode.CYCLE


def test_completion_rejects_stale_policy_after_exact_approval() -> None:
    store = InMemoryIdentityRotationStore(_state("owner-a"))
    plan = _prepare(store, _pair("owner-a"))
    approved = _approve(store, plan)
    store.state = store.state.model_copy(update={"policy_version": 2, "revision": 2})

    with pytest.raises(IdentityRotationError) as failure:
        CompleteIdentityRotation(store).execute(
            plan,
            approved.approval,
            completed_at=NOW + timedelta(minutes=2),
        )

    assert failure.value.code is IdentityRotationErrorCode.STALE_PLAN
    assert store.complete_calls == 0


def test_completion_rejects_when_a_new_owner_lacks_a_binding() -> None:
    store = InMemoryIdentityRotationStore(_state("owner-a"))
    plan = _prepare(store, _pair("owner-a"))
    approved = _approve(store, plan)
    store.state = _state("owner-a", "owner-b", revision=2)

    with pytest.raises(IdentityRotationError) as failure:
        CompleteIdentityRotation(store).execute(
            plan,
            approved.approval,
            completed_at=NOW + timedelta(minutes=2),
        )

    assert failure.value.code is IdentityRotationErrorCode.INCOMPLETE_OWNER_BINDING
    assert store.complete_calls == 0


def test_completion_rejects_an_approval_for_another_plan() -> None:
    store = InMemoryIdentityRotationStore(_state("owner-a"))
    plan_v2 = _prepare(store, _pair("owner-a"))
    approved_v2 = _approve(store, plan_v2)
    plan_v3 = _prepare(store, _pair("owner-a", to_key="v3"))

    with pytest.raises(IdentityRotationError) as failure:
        CompleteIdentityRotation(store).execute(
            plan_v3,
            approved_v2.approval,
            completed_at=NOW + timedelta(minutes=2),
        )

    assert failure.value.code is IdentityRotationErrorCode.APPROVAL_MISMATCH
    assert store.complete_calls == 0


def test_same_plan_id_cannot_be_rebound_to_another_approval() -> None:
    store = InMemoryIdentityRotationStore(_state("owner-a"))
    plan = _prepare(store, _pair("owner-a"))
    _approve(store, plan)

    with pytest.raises(IdentityRotationError) as failure:
        _approve(
            store,
            plan,
            actor=_opaque("actor", "v1", "another-approver"),
            approved_at=NOW + timedelta(minutes=2),
        )

    assert failure.value.code is IdentityRotationErrorCode.STORE_CONFLICT


def test_approval_cannot_persist_a_non_pseudonymous_actor() -> None:
    store = InMemoryIdentityRotationStore(_state("owner-a"))
    plan = _prepare(store, _pair("owner-a"))

    with pytest.raises(IdentityRotationError) as failure:
        _approve(store, plan, actor="named-administrator")

    assert failure.value.code is IdentityRotationErrorCode.APPROVAL_MISMATCH
    assert store.approved == {}
