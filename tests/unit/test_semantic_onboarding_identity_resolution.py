"""M33 identity-rotation regressions for historical onboarding drafts."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
from tests.unit.test_semantic_onboarding_use_cases import (
    _BaseReader,
    _Clock,
    _harness,
    _human_evidence,
    _principal,
)

from schemabridge.adapters.storage.identity_resolving_semantic_onboarding import (
    IdentityResolvingSemanticOnboardingStore,
)
from schemabridge.adapters.storage.semantic_onboarding import InMemorySemanticOnboardingStore
from schemabridge.application.ports.identity_rotation import (
    IdentityRotationStoreError,
    IdentityRotationStoreErrorCode,
)
from schemabridge.application.ports.semantic_onboarding import (
    SemanticOnboardingOperationReplay,
    SemanticOnboardingPortError,
    SemanticOnboardingPortErrorCode,
)
from schemabridge.application.semantic_onboarding import (
    CreateSemanticOnboardingDraft,
    DecideSemanticOnboarding,
    InspectSemanticOnboardingDraft,
    ListSemanticOnboardingDrafts,
    PrepareSemanticOnboardingPublication,
    SemanticOnboardingError,
    SemanticOnboardingErrorCode,
)
from schemabridge.application.semantic_onboarding_authorization import (
    SemanticOnboardingAuthorizationPolicy,
)
from schemabridge.domain.decisions import DecisionAction
from schemabridge.domain.identity import IdentityRole
from schemabridge.domain.identity_rotation import IdentityAuthorizationScope
from schemabridge.domain.semantic_onboarding import (
    SemanticOnboardingAuditRecord,
    SemanticOnboardingDecision,
    SemanticOnboardingDraft,
    SemanticOnboardingDraftMutation,
    SemanticOnboardingTargetKind,
)


def _opaque(kind: str, version: str, label: str) -> str:
    digest = hashlib.sha256(f"{kind}:{version}:{label}".encode()).hexdigest()
    return f"sb_{kind}_{version}_{digest}"


class _Resolver:
    def __init__(self) -> None:
        self.old_workspace = _opaque("workspace", "v1", "workspace")
        self.new_workspace = _opaque("workspace", "v2", "workspace")
        self.old_analyst = _opaque("actor", "v1", "analyst")
        self.new_analyst = _opaque("actor", "v2", "analyst")
        self.old_steward = _opaque("actor", "v1", "steward")
        self.new_steward = _opaque("actor", "v2", "steward")
        self.old_publisher = _opaque("actor", "v1", "publisher")
        self.new_publisher = _opaque("actor", "v2", "publisher")
        self._actors = {
            self.new_analyst: self.old_analyst,
            self.new_steward: self.old_steward,
            self.new_publisher: self.old_publisher,
        }

    def resolve_workspace_aliases(self, workspace_id: str) -> tuple[str, ...]:
        if workspace_id != self.new_workspace:
            raise IdentityRotationStoreError(
                IdentityRotationStoreErrorCode.CROSS_WORKSPACE,
                "inactive identity",
            )
        return self.old_workspace, self.new_workspace

    def resolve_authorization_scopes(
        self,
        workspace_id: str,
        actor_id: str,
    ) -> tuple[IdentityAuthorizationScope, ...]:
        old_actor = self._actors.get(actor_id)
        if workspace_id != self.new_workspace or old_actor is None:
            raise IdentityRotationStoreError(
                IdentityRotationStoreErrorCode.CROSS_WORKSPACE,
                "inactive identity",
            )
        return (
            IdentityAuthorizationScope(
                workspace_id=self.old_workspace,
                actor_id=old_actor,
                key_version="v1",
            ),
            IdentityAuthorizationScope(
                workspace_id=self.new_workspace,
                actor_id=actor_id,
                key_version="v2",
            ),
        )


class _ConcurrentExactReplayStore(InMemorySemanticOnboardingStore):
    """Expose a persisted operation only after the application replay lookup."""

    def __init__(self) -> None:
        super().__init__()
        self.hide_next_replay_for: str | None = None

    def load_operation_replay(
        self,
        workspace_id: str,
        idempotency_digest: str,
    ) -> SemanticOnboardingOperationReplay | None:
        if self.hide_next_replay_for == workspace_id:
            self.hide_next_replay_for = None
            return None
        return super().load_operation_replay(workspace_id, idempotency_digest)

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
        replay = super().load_operation_replay(draft.workspace_id, idempotency_digest)
        if replay is not None:
            if (
                replay.operation == operation
                and replay.actor_id == actor_id
                and replay.request_fingerprint == request_fingerprint
            ):
                return SemanticOnboardingDraftMutation(draft=replay.draft, replayed=True)
            raise SemanticOnboardingPortError(
                SemanticOnboardingPortErrorCode.CONFLICT,
                "semantic onboarding store conflict",
            )
        return super().create(
            draft,
            audit,
            operation=operation,
            actor_id=actor_id,
            idempotency_digest=idempotency_digest,
            request_fingerprint=request_fingerprint,
        )

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
        replay = super().load_operation_replay(draft.workspace_id, idempotency_digest)
        if replay is not None:
            if (
                replay.operation == operation
                and replay.actor_id == actor_id
                and replay.request_fingerprint == request_fingerprint
            ):
                return SemanticOnboardingDraftMutation(draft=replay.draft, replayed=True)
            raise SemanticOnboardingPortError(
                SemanticOnboardingPortErrorCode.CONFLICT,
                "semantic onboarding store conflict",
            )
        return super().commit_decision(
            draft,
            decision,
            audit,
            expected_revision=expected_revision,
            operation=operation,
            actor_id=actor_id,
            idempotency_digest=idempotency_digest,
            request_fingerprint=request_fingerprint,
        )


def test_historical_draft_access_and_exact_replay_follow_verified_actor_lineage() -> None:
    resolver = _Resolver()
    harness = _harness(workspace=resolver.old_workspace)
    original = harness.create.execute(
        _principal(
            IdentityRole.ANALYST,
            resolver.old_analyst,
            workspace=resolver.old_workspace,
        ),
        harness.request,
        idempotency_key="create-rotation-001",
    ).draft
    first = harness.decide.execute(
        _principal(
            IdentityRole.STEWARD,
            resolver.old_steward,
            workspace=resolver.old_workspace,
        ),
        original.id,
        target_kind=SemanticOnboardingTargetKind.MODEL,
        target_id="Order",
        action=DecisionAction.APPROVE,
        expected_revision=original.revision,
        confirmed_draft_fingerprint=original.fingerprint,
        rationale="The steward confirms the governed business definition.",
        evidence=(_human_evidence(),),
        idempotency_key="approve-rotation-001",
    )
    current = _principal(
        IdentityRole.STEWARD,
        resolver.new_steward,
        workspace=resolver.new_workspace,
    )
    store = IdentityResolvingSemanticOnboardingStore(
        harness.store,
        resolver,
        resolver.new_workspace,
        resolver.new_steward,
    )
    authorization = SemanticOnboardingAuthorizationPolicy(resolver)
    decide = DecideSemanticOnboarding(
        store,
        harness.catalog,
        _BaseReader(),
        authorization,
        _Clock(),
    )

    replay = decide.execute(
        current,
        original.id,
        target_kind=SemanticOnboardingTargetKind.MODEL,
        target_id="Order",
        action=DecisionAction.APPROVE,
        expected_revision=original.revision,
        confirmed_draft_fingerprint=original.fingerprint,
        rationale="The steward confirms the governed business definition.",
        evidence=(_human_evidence(),),
        idempotency_key="approve-rotation-001",
    )

    assert replay.replayed is True
    assert replay.draft == first.draft
    assert ListSemanticOnboardingDrafts(store, authorization, _Clock()).execute(current) == (
        first.draft,
    )
    snapshot = InspectSemanticOnboardingDraft(store, authorization, _Clock()).execute(
        current,
        original.id,
    )
    assert snapshot.draft == first.draft
    assert len(snapshot.decisions) == 1


def test_rotated_approver_cannot_prepare_as_a_nominally_new_publisher() -> None:
    resolver = _Resolver()
    harness = _harness(workspace=resolver.old_workspace)
    analyst = _principal(
        IdentityRole.ANALYST,
        resolver.old_analyst,
        workspace=resolver.old_workspace,
    )
    draft = harness.create.execute(
        analyst,
        harness.request,
        idempotency_key="create-sod-rotation-1",
    ).draft
    current_steward = _principal(
        IdentityRole.STEWARD,
        resolver.new_steward,
        workspace=resolver.new_workspace,
    )
    store = IdentityResolvingSemanticOnboardingStore(
        harness.store,
        resolver,
        resolver.new_workspace,
        resolver.new_steward,
    )
    authorization = SemanticOnboardingAuthorizationPolicy(resolver)
    decide = DecideSemanticOnboarding(
        store,
        harness.catalog,
        _BaseReader(),
        authorization,
        _Clock(),
    )
    for target_kind, target_id, key in (
        (SemanticOnboardingTargetKind.MODEL, "Order", "approve-sod-model-1"),
        (
            SemanticOnboardingTargetKind.MAPPING,
            "mapping-order-id",
            "approve-sod-mapping-1",
        ),
    ):
        source = draft
        result = decide.execute(
            current_steward,
            draft.id,
            target_kind=target_kind,
            target_id=target_id,
            action=DecisionAction.APPROVE,
            expected_revision=source.revision,
            confirmed_draft_fingerprint=source.fingerprint,
            rationale="The steward confirms exact retained evidence.",
            evidence=(_human_evidence(),),
            idempotency_key=key,
        )
        if target_kind is SemanticOnboardingTargetKind.MODEL:
            replay = decide.execute(
                current_steward,
                source.id,
                target_kind=target_kind,
                target_id=target_id,
                action=DecisionAction.APPROVE,
                expected_revision=source.revision,
                confirmed_draft_fingerprint=source.fingerprint,
                rationale="The steward confirms exact retained evidence.",
                evidence=(_human_evidence(),),
                idempotency_key=key,
            )
            assert replay.replayed is True
            assert replay.draft == result.draft
        draft = result.draft
    decisions = harness.store.list_decisions(resolver.old_workspace, draft.id)
    assert {decision.actor_id for decision in decisions} == {resolver.old_steward}
    decision_audit = harness.store.list_audit(resolver.old_workspace, draft.id)[1:]
    assert {record.actor_id for record in decision_audit} == {resolver.old_steward}
    current = _principal(
        IdentityRole.PUBLISHER,
        resolver.new_steward,
        workspace=resolver.new_workspace,
    )

    with pytest.raises(SemanticOnboardingError) as raised:
        PrepareSemanticOnboardingPublication(
            store,
            harness.catalog,
            _BaseReader(),
            authorization,
            _Clock(),
        ).execute(
            current,
            draft.id,
            expected_revision=draft.revision,
            confirmed_draft_fingerprint=draft.fingerprint,
            idempotency_key="prepare-sod-rotation-1",
        )

    assert raised.value.code is SemanticOnboardingErrorCode.SEPARATION_OF_DUTIES

    separate_publisher = _principal(
        IdentityRole.PUBLISHER,
        resolver.new_publisher,
        workspace=resolver.new_workspace,
    )
    publisher_store = IdentityResolvingSemanticOnboardingStore(
        harness.store,
        resolver,
        resolver.new_workspace,
        resolver.new_publisher,
    )
    prepare = PrepareSemanticOnboardingPublication(
        publisher_store,
        harness.catalog,
        _BaseReader(),
        authorization,
        _Clock(),
    )
    prepared = prepare.execute(
        separate_publisher,
        draft.id,
        expected_revision=draft.revision,
        confirmed_draft_fingerprint=draft.fingerprint,
        idempotency_key="prepare-separate-rotation-1",
    )
    replayed = prepare.execute(
        separate_publisher,
        draft.id,
        expected_revision=draft.revision,
        confirmed_draft_fingerprint=draft.fingerprint,
        idempotency_key="prepare-separate-rotation-1",
    )
    assert prepared.proposal.prepared_by == resolver.old_publisher
    assert prepared.draft.prepared_by == resolver.old_publisher
    assert replayed.replayed is True
    assert replayed.proposal == prepared.proposal
    assert harness.store.list_audit(resolver.old_workspace, draft.id)[-1].actor_id == (
        resolver.old_publisher
    )


def test_active_create_is_current_only_and_alias_collisions_fail_closed() -> None:
    resolver = _Resolver()
    old_harness = _harness(workspace=resolver.old_workspace)
    new_harness = _harness(workspace=resolver.new_workspace)
    raw = InMemorySemanticOnboardingStore()
    old_create = replace(old_harness.create, store=raw)
    new_create = replace(new_harness.create, store=raw)
    old_create.execute(
        _principal(
            IdentityRole.ANALYST,
            resolver.old_analyst,
            workspace=resolver.old_workspace,
        ),
        old_harness.request,
        idempotency_key="alias-collision-001",
    )
    new_create.execute(
        _principal(
            IdentityRole.ANALYST,
            resolver.new_analyst,
            workspace=resolver.new_workspace,
        ),
        new_harness.request,
        idempotency_key="alias-collision-001",
    )
    store = IdentityResolvingSemanticOnboardingStore(
        raw,
        resolver,
        resolver.new_workspace,
        resolver.new_analyst,
    )

    with pytest.raises(SemanticOnboardingPortError) as draft_collision:
        store.load(resolver.new_workspace, old_harness.request.draft_id)
    assert draft_collision.value.code is SemanticOnboardingPortErrorCode.UNAVAILABLE
    digest = hashlib.sha256(b"alias-collision-001").hexdigest()
    with pytest.raises(SemanticOnboardingPortError) as operation_collision:
        store.load_operation_replay(resolver.new_workspace, digest)
    assert operation_collision.value.code is SemanticOnboardingPortErrorCode.UNAVAILABLE

    unique_request = new_harness.request.model_copy(update={"draft_id": "active-draft-only"})
    active = CreateSemanticOnboardingDraft(
        store,
        new_harness.catalog,
        _BaseReader(),
        SemanticOnboardingAuthorizationPolicy(resolver),
        _Clock(),
        new_harness.catalog.scope,
    ).execute(
        _principal(
            IdentityRole.ANALYST,
            resolver.new_analyst,
            workspace=resolver.new_workspace,
        ),
        unique_request,
        idempotency_key="active-create-001",
    )
    assert active.draft.workspace_id == resolver.new_workspace
    assert raw.load(resolver.old_workspace, active.draft.id) is None


def test_concurrent_exact_create_replay_delegates_to_the_atomic_raw_store() -> None:
    resolver = _Resolver()
    harness = _harness(workspace=resolver.new_workspace)
    raw = _ConcurrentExactReplayStore()
    principal = _principal(
        IdentityRole.ANALYST,
        resolver.new_analyst,
        workspace=resolver.new_workspace,
    )
    first = replace(harness.create, store=raw).execute(
        principal,
        harness.request,
        idempotency_key="concurrent-exact-create-1",
    )
    raw.hide_next_replay_for = resolver.new_workspace
    resolving = IdentityResolvingSemanticOnboardingStore(
        raw,
        resolver,
        resolver.new_workspace,
        resolver.new_analyst,
    )

    retried = replace(
        harness.create,
        store=resolving,
        authorization=SemanticOnboardingAuthorizationPolicy(resolver),
    ).execute(
        principal,
        harness.request,
        idempotency_key="concurrent-exact-create-1",
    )

    assert retried.replayed is True
    assert retried.draft == first.draft


def test_concurrent_exact_decision_replay_delegates_to_the_atomic_raw_store() -> None:
    resolver = _Resolver()
    harness = _harness(workspace=resolver.new_workspace)
    raw = _ConcurrentExactReplayStore()
    created = (
        replace(harness.create, store=raw)
        .execute(
            _principal(
                IdentityRole.ANALYST,
                resolver.new_analyst,
                workspace=resolver.new_workspace,
            ),
            harness.request,
            idempotency_key="concurrent-decision-create-1",
        )
        .draft
    )
    steward = _principal(
        IdentityRole.STEWARD,
        resolver.new_steward,
        workspace=resolver.new_workspace,
    )
    result = replace(harness.decide, store=raw).execute(
        steward,
        created.id,
        target_kind=SemanticOnboardingTargetKind.MODEL,
        target_id="Order",
        action=DecisionAction.APPROVE,
        expected_revision=created.revision,
        confirmed_draft_fingerprint=created.fingerprint,
        rationale="The steward confirms exact retained evidence.",
        evidence=(_human_evidence(),),
        idempotency_key="concurrent-exact-decision-1",
    )
    digest = hashlib.sha256(b"concurrent-exact-decision-1").hexdigest()
    replay = raw.load_operation_replay(resolver.new_workspace, digest)
    assert replay is not None
    decision = raw.list_decisions(resolver.new_workspace, created.id)[0]
    audit = raw.list_audit(resolver.new_workspace, created.id)[-1]
    resolving = IdentityResolvingSemanticOnboardingStore(
        raw,
        resolver,
        resolver.new_workspace,
        resolver.new_steward,
    )

    retried = resolving.commit_decision(
        result.draft,
        decision,
        audit,
        expected_revision=created.revision,
        operation=replay.operation,
        actor_id=replay.actor_id,
        idempotency_digest=digest,
        request_fingerprint=replay.request_fingerprint,
    )

    assert retried.replayed is True
    assert retried.draft == result.draft
