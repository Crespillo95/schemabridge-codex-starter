"""Phase-A registry-change authoring, authorization and in-memory CAS tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import ClassVar

import pytest
from tests.unit.test_registry_join_changes import _profile, _two_model_base
from tests.unit.test_registry_publication_v2 import _proposal

from schemabridge.adapters.storage.registry_changes import InMemoryRegistryChangeStore
from schemabridge.application.authorization import AuthorizationError
from schemabridge.application.ports.semantic_profile_jobs import (
    SemanticJoinProfileQueueError,
    SemanticJoinProfileQueueErrorCode,
)
from schemabridge.application.registry_change_authorization import (
    RegistryChangeAuthorizationPolicy,
)
from schemabridge.application.registry_changes import (
    DecideRegistryJoinChange,
    FinalizeRegistryJoinChangeDraft,
    InspectRegistryJoinChange,
    ListRegistryJoinChanges,
    PrepareRegistryJoinChangePublication,
    RegistryChangeAuthoringError,
    RegistryChangeAuthoringErrorCode,
    RequestRegistryJoinProfile,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    QueryCostBudget,
    SourceConnectorKind,
    SourceDialect,
    postgres_type_contract_fingerprint,
)
from schemabridge.domain.decisions import DecisionAction
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)
from schemabridge.domain.joins import JoinProposal, NormalizedJoinKey
from schemabridge.domain.registry_change_authoring import (
    RegistryChangeAuditEvent,
    RegistryJoinProfileAuthoringRequest,
    RequestRegistryJoinProfileInput,
)
from schemabridge.domain.registry_control import (
    ActiveRegistryPointer,
    GovernedRegistryVersion,
    RegistryVersionTrust,
    registry_projection_fingerprint,
)
from schemabridge.domain.semantic_onboarding import OnboardingRegistryBase
from schemabridge.domain.semantic_profile_jobs import (
    SemanticJoinProfileJob,
    SemanticJoinProfileSubmission,
    claim_semantic_join_profile_job,
    complete_semantic_join_profile_job,
    semantic_join_profile_proposal_fingerprint,
)
from schemabridge.domain.semantic_registry import (
    GovernedSemanticRegistrySnapshot,
    ScopedSemanticRegistrySnapshot,
    datahub_registry_document_urn,
)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
REQUEST_KEY = "registry-change-request-0001"
FINALIZE_KEY = "registry-change-finalize-0001"
DECIDE_KEY = "registry-change-decision-0001"
PREPARE_KEY = "registry-change-prepare-0001"


def test_full_authoring_flow_persists_before_enqueue_and_replays_exactly() -> None:
    harness = _Harness.create()
    owner = _principal("analyst-owner", IdentityRole.ANALYST)
    request_use_case = harness.request_use_case()

    submitted = request_use_case.execute(
        owner,
        harness.request_input,
        idempotency_key=REQUEST_KEY,
    )

    assert harness.queue.persisted_before_enqueue is True
    assert submitted.authoring.request.fingerprint
    assert submitted.authoring.external_writes_performed is False
    assert submitted.job is not None
    assert (
        harness.store.list_audit("workspace-a", submitted.authoring.id)[0].event
        is RegistryChangeAuditEvent.PROFILE_REQUESTED
    )
    replay = request_use_case.execute(
        owner,
        harness.request_input,
        idempotency_key=REQUEST_KEY,
    )
    assert replay.replayed is True
    assert replay.authoring == submitted.authoring
    assert replay.job == submitted.job
    assert harness.queue.enqueue_calls == 1

    altered = harness.request_input.model_copy(update={"change_id": "join-change-altered"})
    with pytest.raises(RegistryChangeAuthoringError) as conflict:
        request_use_case.execute(owner, altered, idempotency_key=REQUEST_KEY)
    assert conflict.value.code is RegistryChangeAuthoringErrorCode.CONFLICT

    harness.queue.complete_current()
    harness.clock.current = NOW + timedelta(minutes=2)
    finalized = harness.finalize_use_case().execute(
        owner,
        submitted.authoring.id,
        confirmed_authoring_fingerprint=submitted.authoring.fingerprint,
        idempotency_key=FINALIZE_KEY,
    )
    assert finalized.draft.revision == 1
    assert finalized.draft.decision is None

    steward = _principal("steward-reviewer", IdentityRole.STEWARD)
    harness.clock.current = NOW + timedelta(minutes=3)
    decided = harness.decide_use_case().execute(
        steward,
        finalized.draft.id,
        action=DecisionAction.APPROVE,
        expected_revision=finalized.draft.revision,
        confirmed_draft_fingerprint=finalized.draft.fingerprint,
        rationale="Aggregate key evidence confirms this governed relationship safely.",
        idempotency_key=DECIDE_KEY,
    )
    assert decided.draft.decision is not None
    assert decided.draft.contract is not None

    publisher = _principal(
        "publisher-separate",
        IdentityRole.PUBLISHER,
        authenticated_at=NOW + timedelta(minutes=2),
    )
    harness.clock.current = NOW + timedelta(minutes=4)
    prepared = harness.prepare_use_case().execute(
        publisher,
        decided.draft.id,
        expected_revision=decided.draft.revision,
        confirmed_draft_fingerprint=decided.draft.fingerprint,
        idempotency_key=PREPARE_KEY,
    )
    assert prepared.external_writes_performed is False
    assert prepared.proposal.external_writes_performed is False
    assert prepared.draft.prepared_proposal_fingerprint == prepared.proposal.fingerprint

    listed = ListRegistryJoinChanges(
        harness.store,
        harness.policy,
        harness.clock,
    ).execute(publisher)
    assert listed == (submitted.authoring,)
    inspected = InspectRegistryJoinChange(
        harness.store,
        harness.policy,
        harness.clock,
    ).execute(publisher, submitted.authoring.id)
    assert inspected.draft == prepared.draft
    assert tuple(item.event for item in inspected.audit) == (
        RegistryChangeAuditEvent.PROFILE_REQUESTED,
        RegistryChangeAuditEvent.PROFILE_JOB_BOUND,
        RegistryChangeAuditEvent.DRAFT_FINALIZED,
        RegistryChangeAuditEvent.DECISION_RECORDED,
        RegistryChangeAuditEvent.PUBLICATION_PREPARED,
    )


def test_profile_must_be_completed_exact_and_current_before_draft() -> None:
    harness = _Harness.create()
    owner = _principal("analyst-owner", IdentityRole.ANALYST)
    submitted = harness.request_use_case().execute(
        owner,
        harness.request_input,
        idempotency_key=REQUEST_KEY,
    )
    finalize = harness.finalize_use_case()

    with pytest.raises(RegistryChangeAuthoringError) as pending:
        finalize.execute(
            owner,
            submitted.authoring.id,
            confirmed_authoring_fingerprint=submitted.authoring.fingerprint,
            idempotency_key=FINALIZE_KEY,
        )
    assert pending.value.code is RegistryChangeAuthoringErrorCode.PROFILE_NOT_READY
    assert harness.store.load_draft("workspace-a", submitted.authoring.id) is None

    harness.queue.complete_current()
    harness.clock.current = NOW + timedelta(hours=2)
    with pytest.raises(RegistryChangeAuthoringError) as stale:
        finalize.execute(
            owner,
            submitted.authoring.id,
            confirmed_authoring_fingerprint=submitted.authoring.fingerprint,
            idempotency_key="registry-change-finalize-expired",
        )
    assert stale.value.code is RegistryChangeAuthoringErrorCode.PROFILE_STALE
    assert harness.store.load_draft("workspace-a", submitted.authoring.id) is None


def test_cas_rbac_tenant_scope_and_separation_of_duties_fail_closed() -> None:
    harness = _Harness.create()
    owner = _principal("analyst-owner", IdentityRole.ANALYST)
    auditor = _principal("audit-only", IdentityRole.AUDITOR)
    with pytest.raises(AuthorizationError):
        harness.request_use_case().execute(
            auditor,
            harness.request_input,
            idempotency_key=REQUEST_KEY,
        )

    submitted = harness.request_use_case().execute(
        owner,
        harness.request_input,
        idempotency_key=REQUEST_KEY,
    )
    harness.queue.complete_current()
    harness.clock.current = NOW + timedelta(minutes=2)
    draft = (
        harness.finalize_use_case()
        .execute(
            owner,
            submitted.authoring.id,
            confirmed_authoring_fingerprint=submitted.authoring.fingerprint,
            idempotency_key=FINALIZE_KEY,
        )
        .draft
    )
    steward = _principal("steward-reviewer", IdentityRole.STEWARD)
    harness.clock.current = NOW + timedelta(minutes=3)
    with pytest.raises(RegistryChangeAuthoringError) as cas:
        harness.decide_use_case().execute(
            steward,
            draft.id,
            action=DecisionAction.APPROVE,
            expected_revision=draft.revision + 1,
            confirmed_draft_fingerprint=draft.fingerprint,
            rationale="Aggregate key evidence confirms this governed relationship safely.",
            idempotency_key=DECIDE_KEY,
        )
    assert cas.value.code is RegistryChangeAuthoringErrorCode.CONFLICT
    approved = (
        harness.decide_use_case()
        .execute(
            steward,
            draft.id,
            action=DecisionAction.APPROVE,
            expected_revision=draft.revision,
            confirmed_draft_fingerprint=draft.fingerprint,
            rationale="Aggregate key evidence confirms this governed relationship safely.",
            idempotency_key="registry-change-decision-0002",
        )
        .draft
    )

    same_reviewer_as_publisher = _principal(
        "steward-reviewer",
        IdentityRole.PUBLISHER,
        authenticated_at=NOW + timedelta(minutes=2),
    )
    harness.clock.current = NOW + timedelta(minutes=4)
    with pytest.raises(RegistryChangeAuthoringError) as sod:
        harness.prepare_use_case().execute(
            same_reviewer_as_publisher,
            approved.id,
            expected_revision=approved.revision,
            confirmed_draft_fingerprint=approved.fingerprint,
            idempotency_key=PREPARE_KEY,
        )
    assert sod.value.code is RegistryChangeAuthoringErrorCode.SEPARATION_OF_DUTIES

    foreign = _principal(
        "publisher-foreign",
        IdentityRole.PUBLISHER,
        workspace_id="workspace-b",
    )
    with pytest.raises(RegistryChangeAuthoringError) as unavailable:
        InspectRegistryJoinChange(
            harness.store,
            harness.policy,
            harness.clock,
        ).execute(foreign, approved.id)
    assert unavailable.value.code is RegistryChangeAuthoringErrorCode.UNAVAILABLE


def test_identity_rotation_preserves_exact_replay_scope_and_sod() -> None:
    harness = _Harness.create()
    old_owner = _principal("analyst-old", IdentityRole.ANALYST)
    submitted = harness.request_use_case().execute(
        old_owner,
        harness.request_input,
        idempotency_key=REQUEST_KEY,
    )
    resolver = _Resolver()
    harness.policy = RegistryChangeAuthorizationPolicy(resolver)
    new_owner = _principal(
        "analyst-new",
        IdentityRole.ANALYST,
        workspace_id="workspace-new",
    )

    replay = harness.request_use_case().execute(
        new_owner,
        harness.request_input,
        idempotency_key=REQUEST_KEY,
    )
    assert replay.replayed is True
    assert replay.authoring == submitted.authoring
    assert harness.queue.enqueue_calls == 1

    harness.queue.complete_current()
    harness.clock.current = NOW + timedelta(minutes=2)
    draft = (
        harness.finalize_use_case()
        .execute(
            new_owner,
            submitted.authoring.id,
            confirmed_authoring_fingerprint=submitted.authoring.fingerprint,
            idempotency_key=FINALIZE_KEY,
        )
        .draft
    )
    new_steward = _principal(
        "steward-new",
        IdentityRole.STEWARD,
        workspace_id="workspace-new",
    )
    harness.clock.current = NOW + timedelta(minutes=3)
    approved = (
        harness.decide_use_case()
        .execute(
            new_steward,
            draft.id,
            action=DecisionAction.APPROVE,
            expected_revision=draft.revision,
            confirmed_draft_fingerprint=draft.fingerprint,
            rationale="Aggregate key evidence confirms this governed relationship safely.",
            idempotency_key=DECIDE_KEY,
        )
        .draft
    )
    assert approved.decision is not None
    assert approved.decision.actor == "steward-old"

    harness.clock.current = NOW + timedelta(minutes=4)
    rotated_reviewer_as_publisher = _principal(
        "steward-new",
        IdentityRole.PUBLISHER,
        workspace_id="workspace-new",
        authenticated_at=NOW + timedelta(minutes=2),
    )
    with pytest.raises(RegistryChangeAuthoringError) as sod:
        harness.prepare_use_case().execute(
            rotated_reviewer_as_publisher,
            approved.id,
            expected_revision=approved.revision,
            confirmed_draft_fingerprint=approved.fingerprint,
            idempotency_key=PREPARE_KEY,
        )
    assert sod.value.code is RegistryChangeAuthoringErrorCode.SEPARATION_OF_DUTIES

    new_publisher = _principal(
        "publisher-new",
        IdentityRole.PUBLISHER,
        workspace_id="workspace-new",
        authenticated_at=NOW + timedelta(minutes=2),
    )
    prepared = harness.prepare_use_case().execute(
        new_publisher,
        approved.id,
        expected_revision=approved.revision,
        confirmed_draft_fingerprint=approved.fingerprint,
        idempotency_key="registry-change-prepare-rotated",
    )
    assert prepared.proposal.prepared_by == "publisher-old"
    assert ListRegistryJoinChanges(
        harness.store,
        harness.policy,
        harness.clock,
    ).execute(new_owner) == (submitted.authoring,)


def test_stale_authority_fails_before_request_persistence_or_queue() -> None:
    harness = _Harness.create()
    owner = _principal("analyst-owner", IdentityRole.ANALYST)
    stale_target = harness.request_input.model_copy(
        update={"expected_execution_target_fingerprint": "f" * 64}
    )
    with pytest.raises(RegistryChangeAuthoringError) as target_error:
        harness.request_use_case().execute(
            owner,
            stale_target,
            idempotency_key=REQUEST_KEY,
        )
    assert target_error.value.code is RegistryChangeAuthoringErrorCode.STALE_TARGET
    assert harness.queue.enqueue_calls == 0
    assert harness.store.list_for_workspace("workspace-a", owner_actor_id=None, limit=50) == ()

    harness.bindings.fail = True
    with pytest.raises(RegistryChangeAuthoringError) as catalog_error:
        harness.request_use_case().execute(
            owner,
            harness.request_input,
            idempotency_key="registry-change-request-catalog-stale",
        )
    assert catalog_error.value.code is RegistryChangeAuthoringErrorCode.STALE_CATALOG
    assert harness.queue.enqueue_calls == 0
    assert harness.store.list_for_workspace("workspace-a", owner_actor_id=None, limit=50) == ()


def test_retry_after_enqueue_failure_revalidates_authority_before_source_io() -> None:
    harness = _Harness.create()
    owner = _principal("analyst-owner", IdentityRole.ANALYST)
    harness.queue.fail_next_enqueue = True

    with pytest.raises(RegistryChangeAuthoringError) as unavailable:
        harness.request_use_case().execute(
            owner,
            harness.request_input,
            idempotency_key=REQUEST_KEY,
        )
    assert unavailable.value.code is RegistryChangeAuthoringErrorCode.SERVICE_UNAVAILABLE
    assert harness.queue.enqueue_calls == 1
    assert harness.queue.job is None
    assert harness.store.list_for_workspace("workspace-a", owner_actor_id=None, limit=50)

    harness.targets.target = harness.target.model_copy(
        update={"route_revision": harness.target.route_revision + 1}
    )
    with pytest.raises(RegistryChangeAuthoringError) as stale:
        harness.request_use_case().execute(
            owner,
            harness.request_input,
            idempotency_key=REQUEST_KEY,
        )
    assert stale.value.code is RegistryChangeAuthoringErrorCode.STALE_TARGET
    assert harness.queue.enqueue_calls == 1
    assert harness.queue.job is None


@dataclass
class _Clock:
    current: datetime

    def now(self) -> datetime:
        return self.current


@dataclass
class _Pointers:
    pointer: ActiveRegistryPointer

    def load_active(self, scope: object) -> ActiveRegistryPointer:
        return self.pointer


@dataclass
class _Versions:
    version: GovernedRegistryVersion

    def load_version(self, scope: object, version: int) -> GovernedRegistryVersion:
        return self.version


@dataclass
class _Targets:
    target: GovernedExecutionTarget

    def resolve_current(
        self,
        *,
        workspace_id: str,
        connection_id: CatalogConnectionId,
    ) -> GovernedExecutionTarget:
        return self.target


class _Bindings:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    def require_current(self, scope: object, bindings: tuple[object, ...]) -> None:
        self.calls += 1
        if self.fail:
            raise ValueError("catalog changed")


@dataclass(frozen=True)
class _Scope:
    workspace_id: str
    actor_id: str


class _Resolver:
    _actors: ClassVar[dict[str, str]] = {
        "analyst-new": "analyst-old",
        "steward-new": "steward-old",
        "publisher-new": "publisher-old",
    }

    def resolve_workspace_aliases(self, workspace_id: str) -> tuple[str, ...]:
        if workspace_id != "workspace-new":
            raise AssertionError("unexpected workspace")
        return "workspace-a", "workspace-new"

    def resolve_authorization_scopes(
        self,
        workspace_id: str,
        actor_id: str,
    ) -> tuple[_Scope, ...]:
        old_actor = self._actors.get(actor_id)
        if workspace_id != "workspace-new" or old_actor is None:
            raise AssertionError("unexpected identity")
        return (
            _Scope("workspace-a", old_actor),
            _Scope("workspace-new", actor_id),
        )


class _Queue:
    def __init__(self, store: InMemoryRegistryChangeStore) -> None:
        self.store = store
        self.job: SemanticJoinProfileJob | None = None
        self.enqueue_calls = 0
        self.persisted_before_enqueue = False
        self.fail_next_enqueue = False

    def enqueue(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
        *,
        max_attempts: int = 5,
    ) -> SemanticJoinProfileSubmission:
        request = authoring.request
        self.enqueue_calls += 1
        self.persisted_before_enqueue = bool(
            self.store.list_for_workspace(
                authoring.workspace_id,
                owner_actor_id=None,
                limit=50,
            )
        )
        if self.fail_next_enqueue:
            self.fail_next_enqueue = False
            raise SemanticJoinProfileQueueError(
                SemanticJoinProfileQueueErrorCode.STORE_UNAVAILABLE,
                "simulated queue outage",
            )
        if self.job is not None:
            return SemanticJoinProfileSubmission(job=self.job, replayed=True)
        self.job = SemanticJoinProfileJob.requested(
            workspace_id=authoring.workspace_id,
            scan_id=request.scan_id,
            proposal=request.proposal,
            execution_target=request.execution_target,
            requested_at=request.requested_at,
            max_attempts=max_attempts,
            connector_contract_version=request.execution_target.route_revision,
        )
        return SemanticJoinProfileSubmission(job=self.job, replayed=False)

    def load(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
    ) -> SemanticJoinProfileJob | None:
        request = authoring.request
        if self.job is None:
            return None
        if (
            self.job.workspace_id != authoring.workspace_id
            or self.job.scan_id != request.scan_id
            or self.job.proposal_fingerprint
            != semantic_join_profile_proposal_fingerprint(request.proposal)
        ):
            return None
        return self.job

    def complete_current(self) -> None:
        assert self.job is not None
        leased = claim_semantic_join_profile_job(
            self.job,
            worker_id="profile-worker-a",
            lease_capability="profile-capability-0123456789abcdef",
            claimed_at=self.job.requested_at + timedelta(seconds=10),
            lease_expires_at=self.job.requested_at + timedelta(minutes=3),
        )
        self.job = complete_semantic_join_profile_job(
            leased,
            worker_id="profile-worker-a",
            lease_capability="profile-capability-0123456789abcdef",
            fencing_token=leased.fencing_token,
            profile=_profile(),
            completed_at=self.job.requested_at + timedelta(minutes=1),
            retain_until=self.job.requested_at + timedelta(days=30),
        )


@dataclass
class _Harness:
    base: GovernedSemanticRegistrySnapshot
    base_registry: OnboardingRegistryBase
    pointer: ActiveRegistryPointer
    version: GovernedRegistryVersion
    target: GovernedExecutionTarget
    request_input: RequestRegistryJoinProfileInput
    store: InMemoryRegistryChangeStore
    queue: _Queue
    clock: _Clock
    pointers: _Pointers
    versions: _Versions
    targets: _Targets
    bindings: _Bindings
    policy: RegistryChangeAuthorizationPolicy

    @classmethod
    def create(cls) -> _Harness:
        base, _ = _two_model_base()
        scope = _proposal().scope
        pointer = ActiveRegistryPointer(
            scope=scope,
            generation=4,
            registry_version=base.version,
            registry_fingerprint=base.fingerprint,
            registry_target=datahub_registry_document_urn(scope, base.version),
            transition_id="transition_active_v2",
            activated_by="activation-admin",
            activated_at=NOW - timedelta(days=1),
            decision_ids=("decision-active-v2",),
        )
        pointer_fingerprint = registry_projection_fingerprint(pointer)
        base_registry = OnboardingRegistryBase(
            registry_version=base.version,
            registry_fingerprint=base.fingerprint,
            activation_generation=pointer.generation,
            active_pointer_fingerprint=pointer_fingerprint,
        )
        version = GovernedRegistryVersion(
            snapshot=ScopedSemanticRegistrySnapshot(
                scope=scope,
                registry=base,
                activation_generation=pointer.generation,
                active_pointer_fingerprint=pointer_fingerprint,
            ),
            publication_approval_id="publication-approval-v2",
            trust=RegistryVersionTrust.STRICT,
        )
        target = _target()
        proposal = _join_proposal(base)
        request_input = RequestRegistryJoinProfileInput(
            change_id="join-change-order-customer",
            connection_id=CatalogConnectionId("warehouse-a"),
            proposal=proposal,
            expected_base_registry=base_registry,
            expected_execution_target_fingerprint=target.fingerprint,
        )
        store = InMemoryRegistryChangeStore()
        queue = _Queue(store)
        return cls(
            base=base,
            base_registry=base_registry,
            pointer=pointer,
            version=version,
            target=target,
            request_input=request_input,
            store=store,
            queue=queue,
            clock=_Clock(NOW),
            pointers=_Pointers(pointer),
            versions=_Versions(version),
            targets=_Targets(target),
            bindings=_Bindings(),
            policy=RegistryChangeAuthorizationPolicy(),
        )

    def request_use_case(self) -> RequestRegistryJoinProfile:
        return RequestRegistryJoinProfile(
            self.store,
            self.pointers,
            self.versions,
            self.targets,
            self.bindings,
            self.queue,
            self.policy,
            self.clock,
            _proposal().scope,
        )

    def finalize_use_case(self) -> FinalizeRegistryJoinChangeDraft:
        return FinalizeRegistryJoinChangeDraft(
            self.store,
            self.pointers,
            self.versions,
            self.targets,
            self.bindings,
            self.queue,
            self.policy,
            self.clock,
        )

    def decide_use_case(self) -> DecideRegistryJoinChange:
        return DecideRegistryJoinChange(
            self.store,
            self.pointers,
            self.versions,
            self.targets,
            self.bindings,
            self.queue,
            self.policy,
            self.clock,
        )

    def prepare_use_case(self) -> PrepareRegistryJoinChangePublication:
        return PrepareRegistryJoinChangePublication(
            self.store,
            self.pointers,
            self.versions,
            self.targets,
            self.bindings,
            self.queue,
            self.policy,
            self.clock,
        )


def _join_proposal(base: GovernedSemanticRegistrySnapshot) -> JoinProposal:
    mappings = {item.mapping.logical_field.root: item.mapping for item in base.mapping_set.mappings}
    left = mappings["Order.order_id"]
    right = mappings["Customer.customer_id"]
    return JoinProposal(
        id="order_customer",
        left_key=NormalizedJoinKey(
            logical_field=left.logical_field,
            physical_field=left.physical_field,
            transformation_plan=left.transformation_plan,
        ),
        right_key=NormalizedJoinKey(
            logical_field=right.logical_field,
            physical_field=right.physical_field,
            transformation_plan=right.transformation_plan,
        ),
    )


def _target() -> GovernedExecutionTarget:
    budget = QueryCostBudget(
        explain_timeout_ms=1_000,
        max_response_bytes=65_536,
        max_total_cost=Decimal("10000"),
        max_estimated_rows=100_000,
        max_plan_nodes=1_000,
        max_plan_depth=32,
        max_plan_width=16_384,
    )
    return GovernedExecutionTarget(
        workspace_id="workspace-a",
        connection_id=CatalogConnectionId("warehouse-a"),
        connector_kind=SourceConnectorKind.POSTGRESQL,
        dialect=SourceDialect.POSTGRESQL,
        route_revision=1,
        route_fingerprint="a" * 64,
        expected_reader="schemabridge_reader",
        source_identity_fingerprint="b" * 64,
        catalog_identity_fingerprint="c" * 64,
        type_contract_fingerprint=postgres_type_contract_fingerprint(),
        cost_budget=budget,
        cost_budget_fingerprint=budget.fingerprint,
    )


def _principal(
    actor_id: str,
    role: IdentityRole,
    *,
    workspace_id: str = "workspace-a",
    authenticated_at: datetime = NOW - timedelta(minutes=1),
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        actor_id=actor_id,
        workspace_id=workspace_id,
        roles=frozenset({role}),
        authentication_method=AuthenticationMethod.LOCAL_DEMO,
        authenticated_at=authenticated_at,
        expires_at=NOW + timedelta(days=1),
    )
