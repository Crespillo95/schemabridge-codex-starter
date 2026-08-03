"""Authenticated, replay-safe M35 Phase-B authoring tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import pytest
from tests.unit.test_registry_change_authoring import (
    _Bindings,
    _Clock,
    _Pointers,
    _principal,
    _Resolver,
    _target,
    _Targets,
    _Versions,
)
from tests.unit.test_registry_join_changes import _profile
from tests.unit.test_registry_model_changes import (
    NOW,
    _blocking_report,
    _joined_base,
    _replacement,
    _replacement_base,
)

from schemabridge.adapters.storage.registry_model_changes import (
    InMemoryRegistryModelChangeStore,
    InMemoryRegistryModelProfileStore,
)
from schemabridge.application.registry_change_authorization import (
    RegistryChangeAuthorizationPolicy,
)
from schemabridge.application.registry_changes import (
    RegistryChangeAuthoringError,
    RegistryChangeAuthoringErrorCode,
)
from schemabridge.application.registry_model_changes import (
    CreateRegistryModelChange,
    DecideRegistryModelChange,
    FinalizeRegistryModelJoinProfile,
    InspectRegistryModelChange,
    ListRegistryModelChanges,
    PrepareRegistryModelChangePublication,
    RequestRegistryModelJoinProfile,
)
from schemabridge.domain.decisions import ApprovalStatus, DecisionAction
from schemabridge.domain.identity import IdentityRole
from schemabridge.domain.joins import JoinProposal
from schemabridge.domain.registry_control import (
    ActiveRegistryPointer,
    GovernedRegistryVersion,
    RegistryVersionTrust,
    registry_projection_fingerprint,
)
from schemabridge.domain.registry_model_change_authoring import (
    CreateRegistryModelChangeInput,
    RegistryIncidentJoinPreserveInput,
    RegistryIncidentJoinUpsertInput,
    RegistryModelChangeStatus,
    RegistryModelReplacementSourceEvidence,
    RequestRegistryModelJoinProfileInput,
)
from schemabridge.domain.registry_model_changes import RegistryModelChangeKind
from schemabridge.domain.semantic_change import SemanticDependencyIndexState
from schemabridge.domain.semantic_onboarding import (
    OnboardingEvidence,
    OnboardingEvidenceKind,
    OnboardingRegistryBase,
    SemanticOnboardingDecision,
    SemanticOnboardingTargetKind,
)
from schemabridge.domain.semantic_profile_jobs import (
    SemanticJoinProfileJob,
    SemanticJoinProfileSubmission,
    claim_semantic_join_profile_job,
    complete_semantic_join_profile_job,
)
from schemabridge.domain.semantic_registry import (
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
    datahub_registry_document_urn,
)

CREATE_KEY = "registry-model-create-0001"
DECIDE_KEY = "registry-model-decide-0001"
PREPARE_KEY = "registry-model-prepare-0001"


@dataclass
class _Dependencies:
    state: SemanticDependencyIndexState

    def load_state(self, scope: object) -> SemanticDependencyIndexState:
        return self.state

    def resolve_impacts(self, context: object, findings: object) -> object:
        raise AssertionError("model authoring must load persisted remediation impacts")


class _Sources:
    def __init__(self, source: RegistryModelReplacementSourceEvidence) -> None:
        self.source = source

    def load(self, workspace_id: str, proposal_id: str):
        if (
            workspace_id == self.source.proposal.workspace_id
            and proposal_id == self.source.proposal.id
        ):
            return self.source
        return None


class _Remediation:
    def __init__(self) -> None:
        self.value = None

    def load_current(self, scope: object, report_id: str, expected_context: object):
        if (
            self.value is not None
            and self.value[0].id == report_id
            and self.value[0].context == expected_context
        ):
            return self.value
        return None


class _Witnesses:
    def load(
        self,
        workspace_id: str,
        change_id: str,
        proposal_id: str,
        join_id: str,
    ):
        del workspace_id, change_id, proposal_id, join_id
        return None


class _ProfileQueue:
    def __init__(self, store: InMemoryRegistryModelProfileStore) -> None:
        self.store = store
        self.job: SemanticJoinProfileJob | None = None
        self.persisted_before_enqueue = False

    def enqueue(self, authoring, *, max_attempts: int = 5) -> SemanticJoinProfileSubmission:
        self.persisted_before_enqueue = (
            self.store.load_request(authoring.workspace_id, authoring.id) == authoring
        )
        if self.job is None:
            request = authoring.request
            self.job = SemanticJoinProfileJob.requested(
                workspace_id=authoring.workspace_id,
                scan_id=request.scan_id,
                proposal=request.bound_proposal,
                execution_target=request.execution_target,
                requested_at=request.requested_at,
                max_attempts=max_attempts,
                connector_contract_version=request.execution_target.route_revision,
            )
            return SemanticJoinProfileSubmission(job=self.job, replayed=False)
        return SemanticJoinProfileSubmission(job=self.job, replayed=True)

    def load(self, authoring):
        return self.job

    def complete(self) -> None:
        assert self.job is not None
        leased = claim_semantic_join_profile_job(
            self.job,
            worker_id="model-profile-worker",
            lease_capability="model-profile-capability-0123456789abcdef",
            claimed_at=self.job.requested_at + timedelta(seconds=10),
            lease_expires_at=self.job.requested_at + timedelta(minutes=3),
        )
        self.job = complete_semantic_join_profile_job(
            leased,
            worker_id="model-profile-worker",
            lease_capability="model-profile-capability-0123456789abcdef",
            fencing_token=leased.fencing_token,
            profile=_profile(),
            completed_at=self.job.requested_at + timedelta(minutes=1),
            retain_until=self.job.requested_at + timedelta(days=30),
        )


@dataclass
class _Harness:
    store: InMemoryRegistryModelChangeStore
    source: RegistryModelReplacementSourceEvidence
    request: CreateRegistryModelChangeInput
    clock: _Clock
    pointers: _Pointers
    versions: _Versions
    dependencies: _Dependencies
    targets: _Targets
    bindings: _Bindings
    sources: _Sources
    remediation: _Remediation
    witnesses: _Witnesses
    policy: RegistryChangeAuthorizationPolicy
    profile_store: InMemoryRegistryModelProfileStore
    profile_queue: _ProfileQueue

    @classmethod
    def create(cls, *, remediation: bool = False) -> _Harness:
        base, _ = _joined_base()
        scope = base.physical_bindings[0]
        registry_scope = SemanticRegistryScope(
            workspace_id=scope.workspace_id,
            catalog_scope=base.catalog_scope,
            registry_id=base.registry_id,
        )
        pointer = ActiveRegistryPointer(
            scope=registry_scope,
            generation=5,
            registry_version=base.version,
            registry_fingerprint=base.fingerprint,
            registry_target=datahub_registry_document_urn(registry_scope, base.version),
            transition_id="registry-transition-5",
            activated_by="activation-admin",
            activated_at=NOW - timedelta(days=1),
            decision_ids=("decision-active-v3",),
        )
        identity = OnboardingRegistryBase(
            registry_version=base.version,
            registry_fingerprint=base.fingerprint,
            activation_generation=pointer.generation,
            active_pointer_fingerprint=registry_projection_fingerprint(pointer),
        )
        evidence = _replacement_base(base, identity)
        replacement = _replacement(identity)
        source = RegistryModelReplacementSourceEvidence.create(
            proposal=replacement,
            owner_actor_id="analyst-source",
            decisions=_source_decisions(replacement),
        )
        version = GovernedRegistryVersion(
            snapshot=ScopedSemanticRegistrySnapshot(
                scope=registry_scope,
                registry=base,
                activation_generation=pointer.generation,
                active_pointer_fingerprint=registry_projection_fingerprint(pointer),
            ),
            publication_approval_id="publication-approval-v3",
            trust=RegistryVersionTrust.STRICT,
        )
        request_values = {
            "change_id": "replace-customer-change",
            "replacement_proposal_id": replacement.id,
            "expected_replacement_fingerprint": replacement.fingerprint,
            "target_model_id": evidence.target_model.id,
            "expected_base_registry": identity,
            "kind": RegistryModelChangeKind.PLANNED_CHANGE,
            "incident_joins": (
                RegistryIncidentJoinPreserveInput(join_id=evidence.incident_joins[0].contract.id),
            ),
            "risks": ("Activation and a new M26 inspection remain mandatory.",),
        }
        remediation_reader = _Remediation()
        if remediation:
            report, impacts = _blocking_report(evidence)
            remediation_reader.value = (report, impacts)
            request_values.update(
                {
                    "kind": RegistryModelChangeKind.M26_REMEDIATION,
                    "remediation_report_id": report.id,
                    "expected_remediation_report_fingerprint": report.fingerprint,
                    "resolved_finding_ids": (report.findings[0].id,),
                }
            )
        profile_store = InMemoryRegistryModelProfileStore()
        return cls(
            store=InMemoryRegistryModelChangeStore(),
            source=source,
            request=CreateRegistryModelChangeInput(**request_values),
            clock=_Clock(NOW + timedelta(minutes=6)),
            pointers=_Pointers(pointer),
            versions=_Versions(version),
            dependencies=_Dependencies(evidence.dependency_context.dependency_index),
            targets=_Targets(_target()),
            bindings=_Bindings(),
            sources=_Sources(source),
            remediation=remediation_reader,
            witnesses=_Witnesses(),
            policy=RegistryChangeAuthorizationPolicy(),
            profile_store=profile_store,
            profile_queue=_ProfileQueue(profile_store),
        )

    def create_use_case(self) -> CreateRegistryModelChange:
        return CreateRegistryModelChange(
            self.store,
            self.sources,
            self.remediation,
            self.witnesses,
            self.pointers,
            self.versions,
            self.dependencies,
            self.targets,
            self.bindings,
            self.policy,
            self.clock,
            self.source.proposal.scope,
        )

    def decide_use_case(self) -> DecideRegistryModelChange:
        return DecideRegistryModelChange(
            self.store,
            self.sources,
            self.remediation,
            self.witnesses,
            self.pointers,
            self.versions,
            self.dependencies,
            self.targets,
            self.bindings,
            self.policy,
            self.clock,
        )

    def prepare_use_case(self) -> PrepareRegistryModelChangePublication:
        return PrepareRegistryModelChangePublication(
            self.store,
            self.sources,
            self.remediation,
            self.witnesses,
            self.pointers,
            self.versions,
            self.dependencies,
            self.targets,
            self.bindings,
            self.policy,
            self.clock,
        )

    def request_profile_use_case(self) -> RequestRegistryModelJoinProfile:
        return RequestRegistryModelJoinProfile(
            self.profile_store,
            self.profile_queue,
            self.sources,
            self.pointers,
            self.versions,
            self.dependencies,
            self.targets,
            self.bindings,
            self.policy,
            self.clock,
            self.source.proposal.scope,
        )

    def finalize_profile_use_case(self) -> FinalizeRegistryModelJoinProfile:
        return FinalizeRegistryModelJoinProfile(
            self.profile_store,
            self.profile_queue,
            self.sources,
            self.pointers,
            self.versions,
            self.dependencies,
            self.targets,
            self.bindings,
            self.policy,
            self.clock,
        )


def test_model_change_full_flow_replays_exactly_and_keeps_inner_source_immutable() -> None:
    harness = _Harness.create()
    owner = _principal(
        "analyst-outer",
        IdentityRole.ANALYST,
        authenticated_at=harness.clock.current - timedelta(minutes=1),
    )
    created = harness.create_use_case().execute(
        owner,
        harness.request,
        idempotency_key=CREATE_KEY,
    )
    replayed = harness.create_use_case().execute(
        owner,
        harness.request,
        idempotency_key=CREATE_KEY,
    )
    assert replayed.replayed is True
    assert replayed.draft == created.draft

    with pytest.raises(RegistryChangeAuthoringError) as altered:
        harness.create_use_case().execute(
            owner,
            harness.request.model_copy(
                update={"risks": ("A different reviewed risk changes the request.",)}
            ),
            idempotency_key=CREATE_KEY,
        )
    assert altered.value.code is RegistryChangeAuthoringErrorCode.CONFLICT

    harness.clock.current += timedelta(minutes=1)
    steward = _principal(
        "steward-outer",
        IdentityRole.STEWARD,
        authenticated_at=harness.clock.current - timedelta(minutes=1),
    )
    decided = harness.decide_use_case().execute(
        steward,
        created.draft.id,
        action=DecisionAction.APPROVE,
        expected_revision=created.draft.revision,
        confirmed_draft_fingerprint=created.draft.fingerprint,
        rationale="The complete replacement and every incident join were reviewed.",
        idempotency_key=DECIDE_KEY,
    )
    assert decided.draft.status is RegistryModelChangeStatus.APPROVED

    harness.clock.current += timedelta(minutes=1)
    publisher = _principal(
        "publisher-outer",
        IdentityRole.PUBLISHER,
        authenticated_at=harness.clock.current - timedelta(minutes=1),
    )
    inner_fingerprint = harness.source.proposal.fingerprint
    prepared = harness.prepare_use_case().execute(
        publisher,
        decided.draft.id,
        expected_revision=decided.draft.revision,
        confirmed_draft_fingerprint=decided.draft.fingerprint,
        idempotency_key=PREPARE_KEY,
    )
    assert prepared.proposal is not None
    assert prepared.proposal.prepared_by == "publisher-outer"
    assert prepared.proposal.prepared_at == harness.clock.current
    assert prepared.proposal.replacement.fingerprint == inner_fingerprint
    assert harness.source.proposal.fingerprint == inner_fingerprint
    assert prepared.proposal.is_current(harness.clock.current)
    assert [
        item.event.value
        for item in harness.store.list_audit(
            prepared.draft.workspace_id,
            prepared.draft.id,
        )
    ] == ["draft_created", "decision_recorded", "publication_prepared"]


def test_candidate_upsert_profile_is_persisted_before_queue_and_becomes_durable_witness() -> None:
    harness = _Harness.create()
    registry = harness.versions.version.snapshot.registry
    base = _replacement_base(registry, harness.request.expected_base_registry)
    incident = base.incident_joins[0]
    proposal = JoinProposal(
        id=incident.contract.id,
        left_key=incident.contract.left_key,
        right_key=incident.contract.right_key,
        default_join_type=incident.contract.default_join_type,
    )
    owner = _principal(
        "analyst-profile",
        IdentityRole.ANALYST,
        authenticated_at=harness.clock.current - timedelta(minutes=1),
    )
    requested = harness.request_profile_use_case().execute(
        owner,
        RequestRegistryModelJoinProfileInput(
            request_id="replace-customer-profile",
            change_id=harness.request.change_id,
            replacement_proposal_id=harness.source.proposal.id,
            expected_replacement_fingerprint=harness.source.proposal.fingerprint,
            target_model_id=base.target_model.id,
            expected_base_registry=base.base_registry,
            join_id=incident.contract.id,
            proposal=proposal,
            expected_execution_target_fingerprint=harness.targets.target.fingerprint,
        ),
        idempotency_key="registry-model-profile-request-0001",
    )
    assert harness.profile_queue.persisted_before_enqueue is True
    assert requested.job is not None
    harness.profile_queue.complete()
    harness.clock.current += timedelta(minutes=2)
    finalized = harness.finalize_profile_use_case().execute(
        owner,
        requested.authoring.id,
        confirmed_authoring_fingerprint=requested.authoring.fingerprint,
        idempotency_key="registry-model-profile-finalize-0001",
    )
    assert finalized.witness is not None
    assert (
        harness.profile_store.load(
            requested.authoring.workspace_id,
            harness.request.change_id,
            harness.source.proposal.id,
            incident.contract.id,
        )
        == finalized.witness
    )

    harness.witnesses = harness.profile_store  # type: ignore[assignment]
    harness.request = harness.request.model_copy(
        update={
            "incident_joins": (
                RegistryIncidentJoinUpsertInput(
                    join_id=incident.contract.id,
                    proposal=proposal,
                    expected_profile_witness_fingerprint=finalized.witness.fingerprint,
                    risks=("The refreshed join requires post-activation inspection.",),
                ),
            )
        }
    )
    created = harness.create_use_case().execute(
        _principal(
            "analyst-outer",
            IdentityRole.ANALYST,
            authenticated_at=harness.clock.current - timedelta(minutes=1),
        ),
        harness.request,
        idempotency_key=CREATE_KEY,
    )
    assert created.draft.incident_intents[0].profile_witness == finalized.witness


def test_profile_witness_head_uses_request_order_when_older_work_finishes_last() -> None:
    harness = _Harness.create()
    registry = harness.versions.version.snapshot.registry
    base = _replacement_base(registry, harness.request.expected_base_registry)
    incident = base.incident_joins[0]
    proposal = JoinProposal(
        id=incident.contract.id,
        left_key=incident.contract.left_key,
        right_key=incident.contract.right_key,
        default_join_type=incident.contract.default_join_type,
    )
    owner = _principal(
        "analyst-profile-order",
        IdentityRole.ANALYST,
        authenticated_at=harness.clock.current - timedelta(minutes=1),
    )

    def request_input(request_id: str) -> RequestRegistryModelJoinProfileInput:
        return RequestRegistryModelJoinProfileInput(
            request_id=request_id,
            change_id=harness.request.change_id,
            replacement_proposal_id=harness.source.proposal.id,
            expected_replacement_fingerprint=harness.source.proposal.fingerprint,
            target_model_id=base.target_model.id,
            expected_base_registry=base.base_registry,
            join_id=incident.contract.id,
            proposal=proposal,
            expected_execution_target_fingerprint=harness.targets.target.fingerprint,
        )

    older = harness.request_profile_use_case().execute(
        owner,
        request_input("replace-customer-profile-older"),
        idempotency_key="registry-model-profile-older-request",
    )
    older_job = older.job
    assert older_job is not None
    harness.clock.current += timedelta(minutes=1)
    harness.profile_queue.job = None
    newer = harness.request_profile_use_case().execute(
        owner,
        request_input("replace-customer-profile-newer"),
        idempotency_key="registry-model-profile-newer-request",
    )
    assert newer.job is not None
    harness.profile_queue.complete()
    harness.clock.current += timedelta(minutes=2)
    newer_finalized = harness.finalize_profile_use_case().execute(
        owner,
        newer.authoring.id,
        confirmed_authoring_fingerprint=newer.authoring.fingerprint,
        idempotency_key="registry-model-profile-newer-finalize",
    )
    assert newer_finalized.witness is not None

    harness.profile_queue.job = older_job
    harness.profile_queue.complete()
    older_finalized = harness.finalize_profile_use_case().execute(
        owner,
        older.authoring.id,
        confirmed_authoring_fingerprint=older.authoring.fingerprint,
        idempotency_key="registry-model-profile-older-finalize",
    )
    assert older_finalized.witness is not None
    assert older_finalized.witness != newer_finalized.witness
    assert (
        harness.profile_store.load(
            newer.authoring.workspace_id,
            harness.request.change_id,
            harness.source.proposal.id,
            incident.contract.id,
        )
        == newer_finalized.witness
    )


def test_second_outer_draft_cannot_reuse_the_same_replacement_source() -> None:
    harness = _Harness.create()
    owner = _principal(
        "analyst-outer",
        IdentityRole.ANALYST,
        authenticated_at=harness.clock.current - timedelta(minutes=1),
    )
    harness.create_use_case().execute(owner, harness.request, idempotency_key=CREATE_KEY)

    with pytest.raises(RegistryChangeAuthoringError) as duplicate:
        harness.create_use_case().execute(
            owner,
            harness.request.model_copy(update={"change_id": "replace-customer-copy"}),
            idempotency_key="registry-model-create-duplicate-source-0001",
        )

    assert duplicate.value.code is RegistryChangeAuthoringErrorCode.CONFLICT


def test_model_change_rotated_identity_replays_but_distinct_publisher_is_enforced() -> None:
    harness = _Harness.create()
    owner = _principal(
        "analyst-old",
        IdentityRole.ANALYST,
        authenticated_at=harness.clock.current - timedelta(minutes=1),
    )
    created = harness.create_use_case().execute(owner, harness.request, idempotency_key=CREATE_KEY)
    harness.policy = RegistryChangeAuthorizationPolicy(_Resolver())
    rotated = _principal(
        "analyst-new",
        IdentityRole.ANALYST,
        workspace_id="workspace-new",
        authenticated_at=harness.clock.current - timedelta(minutes=1),
    )
    replayed = harness.create_use_case().execute(
        rotated,
        harness.request,
        idempotency_key=CREATE_KEY,
    )
    assert replayed.replayed is True
    assert replayed.draft == created.draft

    harness.policy = RegistryChangeAuthorizationPolicy()
    harness.clock.current += timedelta(minutes=1)
    decided = harness.decide_use_case().execute(
        _principal(
            "steward-outer",
            IdentityRole.STEWARD,
            authenticated_at=harness.clock.current - timedelta(minutes=1),
        ),
        created.draft.id,
        action=DecisionAction.APPROVE,
        expected_revision=1,
        confirmed_draft_fingerprint=created.draft.fingerprint,
        rationale="The complete replacement authority was reviewed by a steward.",
        idempotency_key=DECIDE_KEY,
    )
    harness.clock.current += timedelta(minutes=1)
    with pytest.raises(RegistryChangeAuthoringError) as separation:
        harness.prepare_use_case().execute(
            _principal(
                harness.source.proposal.prepared_by,
                IdentityRole.PUBLISHER,
                authenticated_at=harness.clock.current - timedelta(minutes=1),
            ),
            decided.draft.id,
            expected_revision=decided.draft.revision,
            confirmed_draft_fingerprint=decided.draft.fingerprint,
            idempotency_key=PREPARE_KEY,
        )
    assert separation.value.code is RegistryChangeAuthoringErrorCode.SEPARATION_OF_DUTIES


def test_model_change_dependency_and_remediation_authority_fail_closed() -> None:
    harness = _Harness.create(remediation=True)
    owner = _principal(
        "analyst-outer",
        IdentityRole.ANALYST,
        authenticated_at=harness.clock.current - timedelta(minutes=1),
    )
    created = harness.create_use_case().execute(owner, harness.request, idempotency_key=CREATE_KEY)
    harness.remediation.value = None
    harness.clock.current += timedelta(minutes=1)
    with pytest.raises(RegistryChangeAuthoringError) as superseded:
        harness.decide_use_case().execute(
            _principal(
                "steward-outer",
                IdentityRole.STEWARD,
                authenticated_at=harness.clock.current - timedelta(minutes=1),
            ),
            created.draft.id,
            action=DecisionAction.APPROVE,
            expected_revision=1,
            confirmed_draft_fingerprint=created.draft.fingerprint,
            rationale="The replacement cannot use a superseded M26 report.",
            idempotency_key=DECIDE_KEY,
        )
    assert superseded.value.code is RegistryChangeAuthoringErrorCode.STALE_BASE

    harness = _Harness.create(remediation=True)
    created = harness.create_use_case().execute(owner, harness.request, idempotency_key=CREATE_KEY)
    harness.dependencies.state = harness.dependencies.state.model_copy(update={"complete": False})
    harness.clock.current += timedelta(minutes=1)
    with pytest.raises(RegistryChangeAuthoringError) as stale:
        harness.decide_use_case().execute(
            _principal(
                "steward-outer",
                IdentityRole.STEWARD,
                authenticated_at=harness.clock.current - timedelta(minutes=1),
            ),
            created.draft.id,
            action=DecisionAction.APPROVE,
            expected_revision=1,
            confirmed_draft_fingerprint=created.draft.fingerprint,
            rationale="The replacement evidence was reviewed before dependency drift.",
            idempotency_key=DECIDE_KEY,
        )
    assert stale.value.code is RegistryChangeAuthoringErrorCode.STALE_BASE
    assert harness.store.load(created.draft.workspace_id, created.draft.id) == created.draft


def test_model_change_list_and_inspection_mask_tenants_and_bound_audit() -> None:
    harness = _Harness.create()
    created = harness.create_use_case().execute(
        _principal(
            "analyst-outer",
            IdentityRole.ANALYST,
            authenticated_at=harness.clock.current - timedelta(minutes=1),
        ),
        harness.request,
        idempotency_key=CREATE_KEY,
    )
    assert ListRegistryModelChanges(harness.store, harness.policy, harness.clock).execute(
        _principal(
            "steward-view",
            IdentityRole.STEWARD,
            authenticated_at=harness.clock.current - timedelta(minutes=1),
        )
    ) == (created.draft,)
    inspected = InspectRegistryModelChange(
        harness.store,
        harness.policy,
        harness.clock,
    ).execute(
        _principal(
            "auditor-view",
            IdentityRole.AUDITOR,
            authenticated_at=harness.clock.current - timedelta(minutes=1),
        ),
        created.draft.id,
    )
    assert inspected.audit_visible is True
    assert len(inspected.audit) == 1
    with pytest.raises(RegistryChangeAuthoringError) as hidden:
        InspectRegistryModelChange(harness.store, harness.policy, harness.clock).execute(
            _principal(
                "tenant-b",
                IdentityRole.STEWARD,
                workspace_id="workspace-b",
                authenticated_at=harness.clock.current - timedelta(minutes=1),
            ),
            created.draft.id,
        )
    assert hidden.value.code is RegistryChangeAuthoringErrorCode.UNAVAILABLE


def _source_decisions(proposal) -> tuple[SemanticOnboardingDecision, ...]:
    evidence = (
        OnboardingEvidence(
            kind=OnboardingEvidenceKind.DECLARED_KEY,
            detail="The exact retained catalog key was reviewed.",
            reference="catalog:generation-7",
        ),
    )
    model_id = proposal.model.decision_id
    mapping_id = proposal.mappings[0].decision_id
    assert model_id is not None and mapping_id is not None
    return tuple(
        sorted(
            (
                SemanticOnboardingDecision(
                    id=model_id,
                    workspace_id=proposal.workspace_id,
                    draft_id=proposal.draft_id,
                    target_kind=SemanticOnboardingTargetKind.MODEL,
                    target_id=proposal.model.definition.id.root,
                    action=DecisionAction.APPROVE,
                    status=ApprovalStatus.APPROVED,
                    actor_id=proposal.model.decided_by or "missing",
                    decided_at=NOW + timedelta(minutes=1),
                    source_revision=1,
                    resulting_revision=2,
                    rationale="The replacement model meaning and fields were reviewed.",
                    evidence=evidence,
                    idempotency_digest="a" * 64,
                    request_fingerprint="b" * 64,
                ),
                SemanticOnboardingDecision(
                    id=mapping_id,
                    workspace_id=proposal.workspace_id,
                    draft_id=proposal.draft_id,
                    target_kind=SemanticOnboardingTargetKind.MAPPING,
                    target_id=proposal.mappings[0].id,
                    action=DecisionAction.APPROVE,
                    status=ApprovalStatus.APPROVED,
                    actor_id=proposal.mappings[0].decided_by or "missing",
                    decided_at=NOW + timedelta(minutes=2),
                    source_revision=2,
                    resulting_revision=3,
                    rationale="The exact replacement physical mapping was reviewed.",
                    evidence=evidence,
                    idempotency_digest="c" * 64,
                    request_fingerprint="d" * 64,
                ),
            ),
            key=lambda item: item.id,
        )
    )
