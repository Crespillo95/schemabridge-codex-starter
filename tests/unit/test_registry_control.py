"""Adversarial unit tests for the authoritative semantic-registry control plane."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.unit.test_registry_publication_v2 import _proposal as _publication_proposal

from schemabridge.adapters.semantic_registry.recorded import (
    RecordedGovernedSemanticRegistry,
)
from schemabridge.application.ports.registry_control import (
    RegistryControlError,
    RegistryControlErrorCode,
)
from schemabridge.application.registry_control import (
    CommitRegistryActivation,
    InspectRegistryReconciliation,
    LoadActiveGovernedSemanticRegistry,
    PrepareRegistryActivation,
    PrepareRegistryActivationApproval,
    PrepareRegistryReconciliationApproval,
    PrepareRegistryRollback,
    ReconcileRegistryProjection,
)
from schemabridge.domain.registry_control import (
    ActiveRegistryPointer,
    ControlAuditChainVerification,
    GovernedRegistryVersion,
    RegistryActivationApproval,
    RegistryActivationConfirmation,
    RegistryActivationProposal,
    RegistryActivationReadyHandoff,
    RegistryActivationTransition,
    RegistryControlCommit,
    RegistryProjectionOutboxItem,
    RegistryProjectionOutboxStatus,
    RegistryProjectionOutcome,
    RegistryProjectionState,
    RegistryReconciliationApproval,
    RegistryReconciliationCode,
    RegistryReconciliationConfirmation,
    RegistryReconciliationReport,
    RegistryVersionTrust,
    registry_projection_fingerprint,
)
from schemabridge.domain.registry_publication import (
    assemble_publishable_registry_version,
    registry_publication_candidate_id,
)
from schemabridge.domain.registry_publication_jobs import registry_publication_job_id
from schemabridge.domain.semantic_registry import (
    GovernedSemanticRegistrySnapshot,
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
    datahub_registry_document_urn,
    prepare_datahub_registry_version,
    semantic_registry_decision_ids,
)

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "demo/ground_truth/registries/manifest.yml"
NOW = datetime(2026, 7, 23, 9, 30, tzinfo=UTC)
SCOPE = SemanticRegistryScope(
    workspace_id="workspace-unit-live",
    catalog_scope="synthetic-demo",
    registry_id="synthetic_enterprise",
)


class StubVersionReader:
    def __init__(self, versions: dict[int, GovernedRegistryVersion]) -> None:
        self.versions = versions
        self.calls: list[tuple[SemanticRegistryScope, int]] = []

    def load_version(
        self,
        scope: SemanticRegistryScope,
        version: int,
    ) -> GovernedRegistryVersion:
        self.calls.append((scope, version))
        try:
            return self.versions[version]
        except KeyError as error:
            raise RegistryControlError(
                RegistryControlErrorCode.VERSION_UNAVAILABLE,
                "immutable registry version was not found",
            ) from error


class MemoryControlStore:
    def __init__(self) -> None:
        self.active: ActiveRegistryPointer | None = None
        self.transitions: list[RegistryActivationTransition] = []
        self.pending: RegistryProjectionOutboxItem | None = None
        self.audit_transition_ids: set[str] = set()
        self.commits: dict[str, RegistryControlCommit] = {}
        self.commit_calls = 0
        self.outcomes: list[RegistryProjectionOutcome] = []
        self.audit_chain_valid = True
        self.activation_ready_enabled = True
        self.activation_handoff_overrides: dict[int, RegistryActivationReadyHandoff | None] = {}

    def load_active(self, scope: SemanticRegistryScope) -> ActiveRegistryPointer | None:
        if self.active is None or self.active.scope == scope:
            return self.active
        return None

    def load_activation_ready_handoff(
        self,
        scope: SemanticRegistryScope,
        version: int,
    ) -> RegistryActivationReadyHandoff | None:
        if not self.activation_ready_enabled:
            return None
        if version in self.activation_handoff_overrides:
            return self.activation_handoff_overrides[version]
        return _activation_ready_handoff(_governed_version(_v2_registry(), version))

    def list_transitions(
        self,
        scope: SemanticRegistryScope,
        *,
        limit: int = 100,
    ) -> tuple[RegistryActivationTransition, ...]:
        matches = [
            transition
            for transition in self.transitions
            if transition.active_pointer.scope == scope
        ]
        return tuple(matches[-limit:])

    def commit_transition(
        self,
        transition: RegistryActivationTransition,
        outbox: RegistryProjectionOutboxItem,
    ) -> RegistryControlCommit:
        self.commit_calls += 1
        prior = self.commits.get(transition.id)
        if prior is not None:
            if prior.transition != transition or prior.outbox != outbox:
                raise RegistryControlError(
                    RegistryControlErrorCode.CAS_CONFLICT,
                    "transition identity was reused with another payload",
                )
            return prior.model_copy(update={"replayed": True})
        if self.active != transition.previous_pointer:
            raise RegistryControlError(
                RegistryControlErrorCode.CAS_CONFLICT,
                "active pointer changed during compare-and-swap",
            )
        result = RegistryControlCommit(
            transition=transition,
            outbox=outbox,
            audit_event_hash="a" * 64,
        )
        self.active = transition.active_pointer
        self.transitions.append(transition)
        self.pending = outbox
        self.audit_transition_ids.add(transition.id)
        self.commits[transition.id] = result
        return result

    def load_pending_outbox(
        self,
        scope: SemanticRegistryScope,
    ) -> RegistryProjectionOutboxItem | None:
        if self.pending is None or self.pending.desired.pointer.scope == scope:
            return self.pending
        return None

    def load_transition_outbox(
        self,
        scope: SemanticRegistryScope,
        transition_id: str,
    ) -> RegistryProjectionOutboxItem | None:
        result = self.commits.get(transition_id)
        if result is None or result.outbox.desired.pointer.scope != scope:
            return None
        if self.pending is not None and self.pending.transition_id == transition_id:
            return self.pending
        outcome = next(
            (
                item
                for item in self.outcomes
                if item.transition_id == transition_id and item.scope == scope
            ),
            None,
        )
        if outcome is None:
            return result.outbox
        return result.outbox.model_copy(
            update={
                "status": outcome.status,
                "attempts": result.outbox.attempts + 1,
                "last_reason_code": outcome.reason_code,
            }
        )

    def has_audit_event(self, transition_id: str) -> bool:
        return transition_id in self.audit_transition_ids

    def verify_audit_chain(self, workspace_id: str) -> ControlAuditChainVerification:
        event_count = len(self.audit_transition_ids)
        return ControlAuditChainVerification(
            workspace_id=workspace_id,
            event_count=event_count,
            head_hash="a" * 64 if event_count else None,
            valid=self.audit_chain_valid,
        )

    def record_projection_outcome(self, outcome: RegistryProjectionOutcome) -> None:
        if (
            self.pending is None
            or self.pending.id != outcome.outbox_id
            or self.pending.transition_id != outcome.transition_id
        ):
            raise AssertionError("projection outcome did not close the exact pending outbox")
        self.outcomes.append(outcome)
        self.pending = None


class MemoryProjection:
    def __init__(
        self,
        state: RegistryProjectionState | None = None,
        *,
        persist_writes: bool = True,
    ) -> None:
        self.state = state
        self.persist_writes = persist_writes
        self.read_calls: list[SemanticRegistryScope] = []
        self.writes: list[tuple[RegistryProjectionState, RegistryReconciliationApproval]] = []

    def read(self, scope: SemanticRegistryScope) -> RegistryProjectionState | None:
        self.read_calls.append(scope)
        if self.state is None or self.state.pointer.scope == scope:
            return self.state
        return None

    def project(
        self,
        desired: RegistryProjectionState,
        approval: RegistryReconciliationApproval,
    ) -> RegistryProjectionState:
        self.writes.append((desired, approval))
        if self.persist_writes:
            self.state = desired
        return desired


@pytest.fixture(scope="module")
def strict_versions() -> dict[int, GovernedRegistryVersion]:
    registry = _v2_registry()
    return {version: _governed_version(registry, version) for version in range(1, 5)}


def test_activation_approval_binds_exact_cas_actor_time_and_decisions(
    strict_versions: dict[int, GovernedRegistryVersion],
) -> None:
    store = MemoryControlStore()
    versions = StubVersionReader(dict(strict_versions))
    proposal = PrepareRegistryActivation(store, versions, SCOPE).execute(2)
    approvals = PrepareRegistryActivationApproval()

    first = approvals.execute(
        proposal,
        actor="registry-operator",
        approved_at=NOW,
        confirmation=(RegistryActivationConfirmation.ACTIVATE_APPROVED_REGISTRY_VERSION),
    )
    replay = approvals.execute(
        proposal,
        actor="registry-operator",
        approved_at=NOW,
        confirmation=(RegistryActivationConfirmation.ACTIVATE_APPROVED_REGISTRY_VERSION),
    )
    later = approvals.execute(
        proposal,
        actor="registry-operator",
        approved_at=NOW + timedelta(seconds=1),
        confirmation=(RegistryActivationConfirmation.ACTIVATE_APPROVED_REGISTRY_VERSION),
    )
    other_actor = approvals.execute(
        proposal,
        actor="another-operator",
        approved_at=NOW,
        confirmation=(RegistryActivationConfirmation.ACTIVATE_APPROVED_REGISTRY_VERSION),
    )

    assert proposal.expected_generation == 0
    assert proposal.expected_registry_version is None
    assert proposal.expected_registry_fingerprint is None
    assert proposal.expected_transition_id is None
    assert proposal.target_registry_version == 2
    assert proposal.target_registry_urn == datahub_registry_document_urn(SCOPE, 2)
    assert proposal.target_publication_approval_id == "publication-v2"
    assert proposal.decision_ids == semantic_registry_decision_ids(
        strict_versions[2].snapshot.registry
    )
    assert first == replay
    assert first.id != later.id
    assert first.id != other_actor.id

    with pytest.raises(RegistryControlError) as raised:
        approvals.execute(
            proposal,
            actor="registry-operator",
            approved_at=NOW,
            confirmation=(RegistryActivationConfirmation.ROLLBACK_TO_APPROVED_REGISTRY_VERSION),
        )
    assert raised.value.code is RegistryControlErrorCode.APPROVAL_MISMATCH


def test_forged_activation_approval_is_rejected_before_store_mutation(
    strict_versions: dict[int, GovernedRegistryVersion],
) -> None:
    store = MemoryControlStore()
    versions = StubVersionReader(dict(strict_versions))
    proposal = PrepareRegistryActivation(store, versions, SCOPE).execute(2)
    approval = _activation_approval(proposal, approved_at=NOW)
    forged = approval.model_copy(
        update={
            "actor": "forged-operator",
            "approved_at": NOW + timedelta(hours=1),
        }
    )

    with pytest.raises(RegistryControlError) as raised:
        CommitRegistryActivation(store, versions).execute(
            proposal,
            forged,
            committed_at=NOW + timedelta(hours=2),
        )

    assert raised.value.code is RegistryControlErrorCode.APPROVAL_MISMATCH
    assert store.commit_calls == 0
    assert store.active is None
    assert store.transitions == []
    assert store.pending is None


def test_initial_activation_commits_pointer_transition_audit_and_pending_outbox_only(
    strict_versions: dict[int, GovernedRegistryVersion],
) -> None:
    store = MemoryControlStore()
    versions = StubVersionReader(dict(strict_versions))

    committed = _activate(store, versions, 2, approved_at=NOW)

    assert committed.transition.previous_pointer is None
    assert committed.transition.active_pointer.generation == 1
    assert committed.transition.active_pointer.registry_version == 2
    assert committed.transition.active_pointer == store.active
    assert committed.transition.id in store.audit_transition_ids
    assert committed.outbox.status is RegistryProjectionOutboxStatus.PENDING
    assert committed.outbox == store.pending
    assert committed.outbox.attempts == 0
    assert store.commit_calls == 1


def test_exact_activation_retry_replays_without_duplicate_history_or_outbox() -> None:
    recorded = RecordedGovernedSemanticRegistry(MANIFEST_PATH, SCOPE).load().registry
    versions = StubVersionReader({2: _governed_version(recorded, 2)})
    store = MemoryControlStore()
    proposal = PrepareRegistryActivation(store, versions, SCOPE).execute(2)
    approval = _activation_approval(proposal, approved_at=NOW)
    committed_at = NOW + timedelta(minutes=1)

    first = CommitRegistryActivation(store, versions).execute(
        proposal,
        approval,
        committed_at=committed_at,
    )
    replay = CommitRegistryActivation(store, versions).execute(
        proposal,
        approval,
        committed_at=committed_at,
    )

    assert replay.replayed is True
    assert replay.transition == first.transition
    assert replay.outbox == first.outbox
    assert len(store.transitions) == 1
    assert store.pending == first.outbox


def test_stale_approval_performs_zero_additional_mutation(
    strict_versions: dict[int, GovernedRegistryVersion],
) -> None:
    store = MemoryControlStore()
    versions = StubVersionReader(dict(strict_versions))
    _activate(store, versions, 2, approved_at=NOW)
    proposal = PrepareRegistryActivation(store, versions, SCOPE).execute(3)
    approval = _activation_approval(
        proposal,
        approved_at=NOW + timedelta(minutes=10),
    )
    winner = CommitRegistryActivation(store, versions).execute(
        proposal,
        approval,
        committed_at=NOW + timedelta(minutes=11),
    )
    mutation_snapshot = (
        store.commit_calls,
        tuple(store.transitions),
        store.pending,
        frozenset(store.audit_transition_ids),
    )

    with pytest.raises(RegistryControlError) as raised:
        CommitRegistryActivation(store, versions).execute(
            proposal,
            approval,
            committed_at=NOW + timedelta(minutes=12),
        )

    assert raised.value.code is RegistryControlErrorCode.CAS_CONFLICT
    assert store.active == winner.transition.active_pointer
    assert (
        store.commit_calls,
        tuple(store.transitions),
        store.pending,
        frozenset(store.audit_transition_ids),
    ) == mutation_snapshot


def test_target_publication_change_after_approval_fails_closed(
    strict_versions: dict[int, GovernedRegistryVersion],
) -> None:
    store = MemoryControlStore()
    versions = StubVersionReader(dict(strict_versions))
    proposal = PrepareRegistryActivation(store, versions, SCOPE).execute(2)
    approval = _activation_approval(proposal, approved_at=NOW)
    versions.versions[2] = strict_versions[2].model_copy(
        update={"publication_approval_id": "replacement-publication"}
    )

    with pytest.raises(RegistryControlError) as raised:
        CommitRegistryActivation(store, versions).execute(
            proposal,
            approval,
            committed_at=NOW + timedelta(minutes=1),
        )

    assert raised.value.code is RegistryControlErrorCode.VERSION_INVALID
    assert store.commit_calls == 0


def test_strict_v2_without_exact_activation_ready_handoff_cannot_be_prepared(
    strict_versions: dict[int, GovernedRegistryVersion],
) -> None:
    store = MemoryControlStore()
    store.activation_ready_enabled = False
    versions = StubVersionReader(dict(strict_versions))

    with pytest.raises(RegistryControlError) as raised:
        PrepareRegistryActivation(store, versions, SCOPE).execute(2)

    assert raised.value.code is RegistryControlErrorCode.ACTIVATION_NOT_READY
    assert store.commit_calls == 0
    assert store.active is None


def test_activation_ready_catalog_authority_drift_fails_before_store_commit(
    strict_versions: dict[int, GovernedRegistryVersion],
) -> None:
    store = MemoryControlStore()
    versions = StubVersionReader(dict(strict_versions))
    proposal = PrepareRegistryActivation(store, versions, SCOPE).execute(2)
    approval = _activation_approval(proposal, approved_at=NOW)
    original = proposal.activation_ready_handoff
    assert original is not None
    store.activation_handoff_overrides[2] = _changed_activation_handoff(
        original,
        catalog_authority_fingerprint="f" * 64,
    )

    with pytest.raises(RegistryControlError) as raised:
        CommitRegistryActivation(store, versions).execute(
            proposal,
            approval,
            committed_at=NOW + timedelta(minutes=1),
        )

    assert raised.value.code is RegistryControlErrorCode.ACTIVATION_HANDOFF_MISMATCH
    assert store.commit_calls == 0
    assert store.active is None


def test_activation_approval_identity_changes_with_exact_publication_handoff(
    strict_versions: dict[int, GovernedRegistryVersion],
) -> None:
    store = MemoryControlStore()
    versions = StubVersionReader(dict(strict_versions))
    first = PrepareRegistryActivation(store, versions, SCOPE).execute(2)
    original = first.activation_ready_handoff
    assert original is not None
    store.activation_handoff_overrides[2] = _changed_activation_handoff(
        original,
        candidate_fingerprint="e" * 64,
    )
    second = PrepareRegistryActivation(store, versions, SCOPE).execute(2)

    assert first.fingerprint != second.fingerprint
    assert (
        _activation_approval(first, approved_at=NOW).id
        != _activation_approval(
            second,
            approved_at=NOW,
        ).id
    )


def test_legacy_version_is_read_only_and_cannot_be_activated(
    strict_versions: dict[int, GovernedRegistryVersion],
) -> None:
    legacy = strict_versions[1].model_copy(update={"trust": RegistryVersionTrust.LEGACY_READ_ONLY})
    store = MemoryControlStore()
    versions = StubVersionReader({1: legacy})

    with pytest.raises(RegistryControlError) as raised:
        PrepareRegistryActivation(store, versions, SCOPE).execute(1)

    assert raised.value.code is RegistryControlErrorCode.LEGACY_VERSION
    assert store.commit_calls == 0
    assert store.active is None


def test_active_loader_uses_exact_pointer_and_exposes_generation_binding(
    strict_versions: dict[int, GovernedRegistryVersion],
) -> None:
    store = MemoryControlStore()
    versions = StubVersionReader(dict(strict_versions))
    committed = _activate(store, versions, 2, approved_at=NOW)
    loader = LoadActiveGovernedSemanticRegistry(store, versions, SCOPE)

    loaded = loader.load()

    assert loaded.registry == strict_versions[2].snapshot.registry
    assert loaded.activation_generation == 1
    assert loaded.active_pointer_fingerprint == registry_projection_fingerprint(
        committed.transition.active_pointer
    )

    versions.versions[2] = strict_versions[3]
    with pytest.raises(RegistryControlError) as raised:
        loader.load()
    assert raised.value.code is RegistryControlErrorCode.VERSION_INVALID


def test_forged_control_or_projection_models_are_revalidated_at_port_boundaries(
    strict_versions: dict[int, GovernedRegistryVersion],
) -> None:
    store = MemoryControlStore()
    versions = StubVersionReader(dict(strict_versions))
    committed = _activate(store, versions, 2, approved_at=NOW)
    valid_pointer = committed.transition.active_pointer
    store.active = valid_pointer.model_copy(update={"registry_fingerprint": "invalid"})

    with pytest.raises(RegistryControlError) as pointer_error:
        LoadActiveGovernedSemanticRegistry(store, versions, SCOPE).load()
    assert pointer_error.value.code is RegistryControlErrorCode.INVALID_RESPONSE

    store.active = valid_pointer
    projection = MemoryProjection(
        committed.outbox.desired.model_copy(update={"projection_fingerprint": "f" * 64})
    )
    with pytest.raises(RegistryControlError) as projection_error:
        InspectRegistryReconciliation(store, versions, projection, SCOPE).execute(
            inspected_at=NOW + timedelta(minutes=2)
        )
    assert projection_error.value.code is RegistryControlErrorCode.INVALID_RESPONSE


def test_rollback_creates_a_higher_generation_to_a_prior_strict_version(
    strict_versions: dict[int, GovernedRegistryVersion],
) -> None:
    store = MemoryControlStore()
    versions = StubVersionReader(dict(strict_versions))
    first = _activate(store, versions, 2, approved_at=NOW)
    second = _activate(
        store,
        versions,
        3,
        approved_at=NOW + timedelta(minutes=10),
    )

    rollback = PrepareRegistryRollback(store, versions, SCOPE).execute(first.transition.id)
    approval = PrepareRegistryActivationApproval().execute(
        rollback,
        actor="rollback-operator",
        approved_at=NOW + timedelta(minutes=20),
        confirmation=(RegistryActivationConfirmation.ROLLBACK_TO_APPROVED_REGISTRY_VERSION),
    )
    result = CommitRegistryActivation(store, versions).execute(
        rollback,
        approval,
        committed_at=NOW + timedelta(minutes=21),
    )

    assert rollback.expected_generation == second.transition.active_pointer.generation
    assert rollback.rollback_transition_id == first.transition.id
    assert result.transition.active_pointer.generation == 3
    assert result.transition.active_pointer.registry_version == 2
    assert result.transition.previous_pointer == second.transition.active_pointer
    assert tuple(item.active_pointer.generation for item in store.transitions) == (
        1,
        2,
        3,
    )

    with pytest.raises(RegistryControlError) as raised:
        PrepareRegistryRollback(store, versions, SCOPE).execute(
            f"registry-transition-v1-{'f' * 64}"
        )
    assert raised.value.code is RegistryControlErrorCode.ROLLBACK_NOT_ALLOWED


def test_rollback_revalidates_and_rejects_a_legacy_historical_target(
    strict_versions: dict[int, GovernedRegistryVersion],
) -> None:
    store = MemoryControlStore()
    versions = StubVersionReader(dict(strict_versions))
    first = _activate(store, versions, 1, approved_at=NOW)
    _activate(
        store,
        versions,
        2,
        approved_at=NOW + timedelta(minutes=10),
    )
    store.audit_transition_ids.remove(first.transition.id)
    with pytest.raises(RegistryControlError) as audit_error:
        PrepareRegistryRollback(store, versions, SCOPE).execute(first.transition.id)
    assert audit_error.value.code is RegistryControlErrorCode.ROLLBACK_NOT_ALLOWED

    store.audit_transition_ids.add(first.transition.id)
    versions.versions[1] = strict_versions[1].model_copy(
        update={"trust": RegistryVersionTrust.LEGACY_READ_ONLY}
    )

    with pytest.raises(RegistryControlError) as raised:
        PrepareRegistryRollback(store, versions, SCOPE).execute(first.transition.id)

    assert raised.value.code is RegistryControlErrorCode.LEGACY_VERSION
    assert store.active is not None
    assert store.active.registry_version == 2


def test_rollback_rejects_invalid_hmac_chain_with_existing_transition_event(
    strict_versions: dict[int, GovernedRegistryVersion],
) -> None:
    store = MemoryControlStore()
    versions = StubVersionReader(dict(strict_versions))
    first = _activate(store, versions, 1, approved_at=NOW)
    _activate(
        store,
        versions,
        2,
        approved_at=NOW + timedelta(minutes=10),
    )
    assert first.transition.id in store.audit_transition_ids
    store.audit_chain_valid = False

    with pytest.raises(RegistryControlError) as raised:
        PrepareRegistryRollback(store, versions, SCOPE).execute(first.transition.id)

    assert raised.value.code is RegistryControlErrorCode.ROLLBACK_NOT_ALLOWED


def test_rollback_rejects_missing_or_corrupt_immutable_target_without_mutation(
    strict_versions: dict[int, GovernedRegistryVersion],
) -> None:
    store = MemoryControlStore()
    versions = StubVersionReader(dict(strict_versions))
    first = _activate(store, versions, 1, approved_at=NOW)
    _activate(
        store,
        versions,
        2,
        approved_at=NOW + timedelta(minutes=10),
    )
    mutation_snapshot = (
        store.active,
        store.commit_calls,
        tuple(store.transitions),
        store.pending,
        frozenset(store.audit_transition_ids),
        dict(store.commits),
        tuple(store.outcomes),
    )

    del versions.versions[1]
    with pytest.raises(RegistryControlError) as missing:
        PrepareRegistryRollback(store, versions, SCOPE).execute(first.transition.id)
    assert missing.value.code is RegistryControlErrorCode.VERSION_UNAVAILABLE
    assert (
        store.active,
        store.commit_calls,
        tuple(store.transitions),
        store.pending,
        frozenset(store.audit_transition_ids),
        dict(store.commits),
        tuple(store.outcomes),
    ) == mutation_snapshot

    versions.versions[1] = strict_versions[3]
    with pytest.raises(RegistryControlError) as corrupt:
        PrepareRegistryRollback(store, versions, SCOPE).execute(first.transition.id)
    assert corrupt.value.code is RegistryControlErrorCode.VERSION_INVALID
    assert (
        store.active,
        store.commit_calls,
        tuple(store.transitions),
        store.pending,
        frozenset(store.audit_transition_ids),
        dict(store.commits),
        tuple(store.outcomes),
    ) == mutation_snapshot


def test_stale_rollback_approval_fails_cas_with_zero_additional_mutation(
    strict_versions: dict[int, GovernedRegistryVersion],
) -> None:
    store = MemoryControlStore()
    versions = StubVersionReader(dict(strict_versions))
    first = _activate(store, versions, 1, approved_at=NOW)
    _activate(
        store,
        versions,
        2,
        approved_at=NOW + timedelta(minutes=10),
    )
    rollback = PrepareRegistryRollback(store, versions, SCOPE).execute(first.transition.id)
    approval = _activation_approval(
        rollback,
        approved_at=NOW + timedelta(minutes=20),
    )
    winner = _activate(
        store,
        versions,
        3,
        approved_at=NOW + timedelta(minutes=30),
    )
    mutation_snapshot = (
        store.commit_calls,
        tuple(store.transitions),
        store.pending,
        frozenset(store.audit_transition_ids),
        dict(store.commits),
        tuple(store.outcomes),
    )

    with pytest.raises(RegistryControlError) as raised:
        CommitRegistryActivation(store, versions).execute(
            rollback,
            approval,
            committed_at=NOW + timedelta(minutes=40),
        )

    assert raised.value.code is RegistryControlErrorCode.CAS_CONFLICT
    assert store.active == winner.transition.active_pointer
    assert (
        store.commit_calls,
        tuple(store.transitions),
        store.pending,
        frozenset(store.audit_transition_ids),
        dict(store.commits),
        tuple(store.outcomes),
    ) == mutation_snapshot


def test_missing_projection_is_repaired_only_with_exact_approval_and_read_back(
    strict_versions: dict[int, GovernedRegistryVersion],
) -> None:
    store = MemoryControlStore()
    versions = StubVersionReader(dict(strict_versions))
    _activate(store, versions, 2, approved_at=NOW)
    projection = MemoryProjection()
    report = InspectRegistryReconciliation(
        store,
        versions,
        projection,
        SCOPE,
    ).execute(inspected_at=NOW + timedelta(minutes=2))
    approval = _reconciliation_approval(
        report,
        approved_at=NOW + timedelta(minutes=3),
    )

    outcome = ReconcileRegistryProjection(store, versions, projection).execute(
        report,
        approval,
        occurred_at=NOW + timedelta(minutes=4),
    )

    assert _codes(report) == {
        RegistryReconciliationCode.PENDING_OUTBOX,
        RegistryReconciliationCode.PROJECTION_MISSING,
    }
    assert [desired for desired, _approval in projection.writes] == [report.desired_projection]
    assert projection.state == report.desired_projection
    assert outcome.status is RegistryProjectionOutboxStatus.DELIVERED
    assert outcome.observed_projection == report.desired_projection
    assert store.outcomes == [outcome]
    assert store.pending is None


def test_behind_projection_is_repaired_to_exact_authoritative_generation(
    strict_versions: dict[int, GovernedRegistryVersion],
) -> None:
    store = MemoryControlStore()
    versions = StubVersionReader(dict(strict_versions))
    prior = _activate(store, versions, 1, approved_at=NOW)
    current = _activate(
        store,
        versions,
        2,
        approved_at=NOW + timedelta(minutes=10),
    )
    projection = MemoryProjection(prior.outbox.desired)
    report = InspectRegistryReconciliation(
        store,
        versions,
        projection,
        SCOPE,
    ).execute(inspected_at=NOW + timedelta(minutes=12))
    approval = _reconciliation_approval(
        report,
        approved_at=NOW + timedelta(minutes=13),
    )

    outcome = ReconcileRegistryProjection(store, versions, projection).execute(
        report,
        approval,
        occurred_at=NOW + timedelta(minutes=14),
    )

    assert _codes(report) == {
        RegistryReconciliationCode.PENDING_OUTBOX,
        RegistryReconciliationCode.PROJECTION_BEHIND,
    }
    assert report.observed_projection == prior.outbox.desired
    assert report.desired_projection == current.outbox.desired
    assert [desired for desired, _approval in projection.writes] == [current.outbox.desired]
    assert projection.state == current.outbox.desired
    assert outcome.status is RegistryProjectionOutboxStatus.DELIVERED
    assert outcome.observed_projection == current.outbox.desired
    assert store.outcomes == [outcome]
    assert store.pending is None


def test_exact_prior_projection_success_closes_pending_without_rewrite(
    strict_versions: dict[int, GovernedRegistryVersion],
) -> None:
    store = MemoryControlStore()
    versions = StubVersionReader(dict(strict_versions))
    committed = _activate(store, versions, 2, approved_at=NOW)
    projection = MemoryProjection(committed.outbox.desired)
    report = InspectRegistryReconciliation(
        store,
        versions,
        projection,
        SCOPE,
    ).execute(inspected_at=NOW + timedelta(minutes=2))
    approval = _reconciliation_approval(
        report,
        approved_at=NOW + timedelta(minutes=3),
    )

    outcome = ReconcileRegistryProjection(store, versions, projection).execute(
        report,
        approval,
        occurred_at=NOW + timedelta(minutes=4),
    )

    assert _codes(report) == {
        RegistryReconciliationCode.PENDING_OUTBOX,
        RegistryReconciliationCode.IN_SYNC,
    }
    assert projection.writes == []
    assert outcome.status is RegistryProjectionOutboxStatus.DELIVERED
    assert store.pending is None


@pytest.mark.parametrize(
    ("observed_generation", "expected_code"),
    [
        (2, RegistryReconciliationCode.PROJECTION_AHEAD),
        (1, RegistryReconciliationCode.PROJECTION_CONFLICT),
    ],
)
def test_ahead_or_conflicting_projection_is_never_automatically_overwritten(
    strict_versions: dict[int, GovernedRegistryVersion],
    observed_generation: int,
    expected_code: RegistryReconciliationCode,
) -> None:
    store = MemoryControlStore()
    versions = StubVersionReader(dict(strict_versions))
    _activate(store, versions, 2, approved_at=NOW)
    conflicting = _projection_state(
        strict_versions[3],
        generation=observed_generation,
        transition_suffix="b",
    )
    projection = MemoryProjection(conflicting)
    report = InspectRegistryReconciliation(
        store,
        versions,
        projection,
        SCOPE,
    ).execute(inspected_at=NOW + timedelta(minutes=2))
    approval = _reconciliation_approval(
        report,
        approved_at=NOW + timedelta(minutes=3),
    )

    with pytest.raises(RegistryControlError) as raised:
        ReconcileRegistryProjection(store, versions, projection).execute(
            report,
            approval,
            occurred_at=NOW + timedelta(minutes=4),
        )

    assert expected_code in _codes(report)
    assert raised.value.code is RegistryControlErrorCode.RECONCILIATION_CONFLICT
    assert projection.writes == []
    assert store.outcomes == []
    assert store.pending is not None


def test_superseded_outbox_never_overwrites_the_newer_generation(
    strict_versions: dict[int, GovernedRegistryVersion],
) -> None:
    store = MemoryControlStore()
    versions = StubVersionReader(dict(strict_versions))
    older = _activate(store, versions, 2, approved_at=NOW)
    newer = _activate(
        store,
        versions,
        3,
        approved_at=NOW + timedelta(minutes=10),
    )
    store.pending = older.outbox
    projection = MemoryProjection(newer.outbox.desired)
    report = InspectRegistryReconciliation(
        store,
        versions,
        projection,
        SCOPE,
    ).execute(inspected_at=NOW + timedelta(minutes=12))
    approval = _reconciliation_approval(
        report,
        approved_at=NOW + timedelta(minutes=13),
    )

    outcome = ReconcileRegistryProjection(store, versions, projection).execute(
        report,
        approval,
        occurred_at=NOW + timedelta(minutes=14),
    )

    assert RegistryReconciliationCode.SUPERSEDED in _codes(report)
    assert projection.writes == []
    assert outcome.status is RegistryProjectionOutboxStatus.SUPERSEDED
    assert outcome.transition_id == older.transition.id
    assert outcome.generation == older.transition.active_pointer.generation
    assert store.pending is None


def test_failed_projection_read_back_leaves_outbox_pending(
    strict_versions: dict[int, GovernedRegistryVersion],
) -> None:
    store = MemoryControlStore()
    versions = StubVersionReader(dict(strict_versions))
    _activate(store, versions, 2, approved_at=NOW)
    projection = MemoryProjection(persist_writes=False)
    report = InspectRegistryReconciliation(
        store,
        versions,
        projection,
        SCOPE,
    ).execute(inspected_at=NOW + timedelta(minutes=2))
    approval = _reconciliation_approval(
        report,
        approved_at=NOW + timedelta(minutes=3),
    )
    pending = store.pending

    with pytest.raises(RegistryControlError) as raised:
        ReconcileRegistryProjection(store, versions, projection).execute(
            report,
            approval,
            occurred_at=NOW + timedelta(minutes=4),
        )

    assert raised.value.code is RegistryControlErrorCode.INVALID_RESPONSE
    assert len(projection.writes) == 1
    assert store.outcomes == []
    assert store.pending == pending


def test_reconciliation_classifies_version_corruption_and_audit_gap(
    strict_versions: dict[int, GovernedRegistryVersion],
) -> None:
    store = MemoryControlStore()
    versions = StubVersionReader(dict(strict_versions))
    committed = _activate(store, versions, 2, approved_at=NOW)
    del versions.versions[2]
    store.audit_transition_ids.remove(committed.transition.id)
    projection = MemoryProjection()

    report = InspectRegistryReconciliation(
        store,
        versions,
        projection,
        SCOPE,
    ).execute(inspected_at=NOW + timedelta(minutes=2))
    approval = _reconciliation_approval(
        report,
        approved_at=NOW + timedelta(minutes=3),
    )

    assert {
        RegistryReconciliationCode.VERSION_CORRUPT,
        RegistryReconciliationCode.AUDIT_GAP,
    } <= _codes(report)
    with pytest.raises(RegistryControlError) as raised:
        ReconcileRegistryProjection(store, versions, projection).execute(
            report,
            approval,
            occurred_at=NOW + timedelta(minutes=4),
        )
    assert raised.value.code is RegistryControlErrorCode.RECONCILIATION_CONFLICT
    assert projection.writes == []


def test_reconciliation_classifies_invalid_hmac_chain_as_audit_gap(
    strict_versions: dict[int, GovernedRegistryVersion],
) -> None:
    store = MemoryControlStore()
    versions = StubVersionReader(dict(strict_versions))
    committed = _activate(store, versions, 2, approved_at=NOW)
    assert committed.transition.id in store.audit_transition_ids
    store.audit_chain_valid = False
    projection = MemoryProjection()

    report = InspectRegistryReconciliation(
        store,
        versions,
        projection,
        SCOPE,
    ).execute(inspected_at=NOW + timedelta(minutes=2))
    approval = _reconciliation_approval(
        report,
        approved_at=NOW + timedelta(minutes=3),
    )

    assert RegistryReconciliationCode.AUDIT_GAP in _codes(report)
    with pytest.raises(RegistryControlError) as raised:
        ReconcileRegistryProjection(store, versions, projection).execute(
            report,
            approval,
            occurred_at=NOW + timedelta(minutes=4),
        )
    assert raised.value.code is RegistryControlErrorCode.RECONCILIATION_CONFLICT
    assert projection.writes == []


def test_reconciliation_approval_or_observation_change_fails_before_write(
    strict_versions: dict[int, GovernedRegistryVersion],
) -> None:
    store = MemoryControlStore()
    versions = StubVersionReader(dict(strict_versions))
    committed = _activate(store, versions, 2, approved_at=NOW)
    projection = MemoryProjection()
    report = InspectRegistryReconciliation(
        store,
        versions,
        projection,
        SCOPE,
    ).execute(inspected_at=NOW + timedelta(minutes=2))
    approval = _reconciliation_approval(
        report,
        approved_at=NOW + timedelta(minutes=3),
    )
    forged = approval.model_copy(update={"approved_at": NOW + timedelta(minutes=30)})

    with pytest.raises(RegistryControlError) as forged_error:
        ReconcileRegistryProjection(store, versions, projection).execute(
            report,
            forged,
            occurred_at=NOW + timedelta(minutes=4),
        )
    assert forged_error.value.code is RegistryControlErrorCode.APPROVAL_MISMATCH
    assert projection.writes == []

    projection.state = committed.outbox.desired
    with pytest.raises(RegistryControlError) as stale_error:
        ReconcileRegistryProjection(store, versions, projection).execute(
            report,
            approval,
            occurred_at=NOW + timedelta(minutes=4),
        )
    assert stale_error.value.code is RegistryControlErrorCode.RECONCILIATION_CONFLICT
    assert projection.writes == []
    assert store.outcomes == []


def _governed_version(
    recorded: GovernedSemanticRegistrySnapshot,
    version: int,
) -> GovernedRegistryVersion:
    del recorded
    registry = _v2_registry()
    numbered = GovernedSemanticRegistrySnapshot.model_validate(
        {
            **registry.model_dump(mode="python"),
            "version": version,
        }
    )
    live = prepare_datahub_registry_version(numbered, SCOPE)
    return GovernedRegistryVersion(
        snapshot=ScopedSemanticRegistrySnapshot(scope=SCOPE, registry=live),
        publication_approval_id=f"publication-v{version}",
        trust=RegistryVersionTrust.STRICT,
    )


def _v2_registry() -> GovernedSemanticRegistrySnapshot:
    return assemble_publishable_registry_version(
        _publication_proposal(scope=SCOPE),
        base=None,
    ).registry


def _activation_ready_handoff(
    version: GovernedRegistryVersion,
) -> RegistryActivationReadyHandoff:
    registry = version.snapshot.registry
    binding = registry.physical_bindings[0]
    source_proposal_id = binding.source_proposal_id
    source_proposal_fingerprint = binding.source_proposal_fingerprint
    return RegistryActivationReadyHandoff.create(
        job_id=registry_publication_job_id(SCOPE, registry.version),
        scope=SCOPE,
        source_proposal_id=source_proposal_id,
        source_proposal_fingerprint=source_proposal_fingerprint,
        candidate_id=registry_publication_candidate_id(
            source_proposal_id,
            source_proposal_fingerprint,
        ),
        candidate_fingerprint=hashlib.sha256(f"candidate:{registry.version}".encode()).hexdigest(),
        target_registry_version=registry.version,
        target_registry_fingerprint=registry.fingerprint,
        target_registry_urn=datahub_registry_document_urn(SCOPE, registry.version),
        attempt_authorization_id=version.publication_approval_id,
        observed_authorization_id=version.publication_approval_id,
        catalog_authority_fingerprint=hashlib.sha256(
            f"catalog:{registry.version}".encode()
        ).hexdigest(),
        observed_at=NOW,
    )


def _changed_activation_handoff(
    handoff: RegistryActivationReadyHandoff,
    *,
    candidate_fingerprint: str | None = None,
    catalog_authority_fingerprint: str | None = None,
) -> RegistryActivationReadyHandoff:
    return RegistryActivationReadyHandoff.create(
        job_id=handoff.job_id,
        scope=handoff.scope,
        source_proposal_id=handoff.source_proposal_id,
        source_proposal_fingerprint=handoff.source_proposal_fingerprint,
        candidate_id=handoff.candidate_id,
        candidate_fingerprint=candidate_fingerprint or handoff.candidate_fingerprint,
        target_registry_version=handoff.target_registry_version,
        target_registry_fingerprint=handoff.target_registry_fingerprint,
        target_registry_urn=handoff.target_registry_urn,
        attempt_authorization_id=handoff.attempt_authorization_id,
        observed_authorization_id=handoff.observed_authorization_id,
        catalog_authority_fingerprint=(
            catalog_authority_fingerprint or handoff.catalog_authority_fingerprint
        ),
        observed_at=handoff.observed_at,
    )


def _activation_approval(
    proposal: RegistryActivationProposal,
    *,
    approved_at: datetime,
    actor: str = "registry-operator",
) -> RegistryActivationApproval:
    confirmation = {
        "activate": RegistryActivationConfirmation.ACTIVATE_APPROVED_REGISTRY_VERSION,
        "rollback": RegistryActivationConfirmation.ROLLBACK_TO_APPROVED_REGISTRY_VERSION,
    }[proposal.action.value]
    return PrepareRegistryActivationApproval().execute(
        proposal,
        actor=actor,
        approved_at=approved_at,
        confirmation=confirmation,
    )


def _activate(
    store: MemoryControlStore,
    versions: StubVersionReader,
    version: int,
    *,
    approved_at: datetime,
) -> RegistryControlCommit:
    proposal = PrepareRegistryActivation(store, versions, SCOPE).execute(version)
    approval = _activation_approval(proposal, approved_at=approved_at)
    return CommitRegistryActivation(store, versions).execute(
        proposal,
        approval,
        committed_at=approved_at + timedelta(minutes=1),
    )


def _reconciliation_approval(
    report: RegistryReconciliationReport,
    *,
    approved_at: datetime,
) -> RegistryReconciliationApproval:
    return PrepareRegistryReconciliationApproval().execute(
        report,
        actor="reconciliation-operator",
        approved_at=approved_at,
        confirmation=(RegistryReconciliationConfirmation.REPAIR_ACTIVE_REGISTRY_PROJECTION),
    )


def _projection_state(
    version: GovernedRegistryVersion,
    *,
    generation: int,
    transition_suffix: str,
) -> RegistryProjectionState:
    registry = version.snapshot.registry
    pointer = ActiveRegistryPointer(
        scope=SCOPE,
        generation=generation,
        registry_version=registry.version,
        registry_fingerprint=registry.fingerprint,
        registry_target=datahub_registry_document_urn(SCOPE, registry.version),
        transition_id=f"registry-transition-v1-{transition_suffix * 64}",
        activated_by="other-reconciler",
        activated_at=NOW,
        decision_ids=semantic_registry_decision_ids(registry),
    )
    return RegistryProjectionState(
        pointer=pointer,
        projection_fingerprint=registry_projection_fingerprint(pointer),
    )


def _codes(report: RegistryReconciliationReport) -> set[RegistryReconciliationCode]:
    return {finding.code for finding in report.findings}
