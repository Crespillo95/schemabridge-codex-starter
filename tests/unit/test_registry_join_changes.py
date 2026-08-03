"""M35 pure add-join review and registry-v2 assembly contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from tests.unit.test_registry_publication_v2 import FP_D, _proposal

from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    QueryCostBudget,
    SourceConnectorKind,
    SourceDialect,
    postgres_type_contract_fingerprint,
)
from schemabridge.domain.decisions import ApprovalStatus, DecisionAction
from schemabridge.domain.joins import (
    Cardinality,
    DeclaredRelationship,
    FanoutPolicy,
    JoinProposal,
    NormalizedJoinKey,
    RelationshipProfile,
)
from schemabridge.domain.registry_changes import (
    PreparedRegistryJoinProposal,
    RegistryJoinChangeDraft,
    RegistryJoinChangeStatus,
    RegistryJoinProfileRequest,
    assemble_join_change_registry_version,
    decide_registry_join_change,
    mark_registry_join_change_ready,
    resolve_registry_join_base_evidence,
    supersede_registry_join_change,
    validate_registry_join_profile_campaign,
)
from schemabridge.domain.registry_publication import assemble_publishable_registry_version
from schemabridge.domain.semantic_onboarding import OnboardingRegistryBase
from schemabridge.domain.semantic_profile_jobs import (
    SemanticJoinProfileJob,
    SemanticJoinProfileProposal,
    SemanticJoinProfileTargetRef,
    claim_semantic_join_profile_job,
    complete_semantic_join_profile_job,
)
from schemabridge.domain.semantic_registry import RegistryArtifactKind

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
FP_E = "e" * 64
FP_F = "f" * 64


def test_add_join_starts_unapproved_and_appends_exactly_one_governed_relationship() -> None:
    base, base_identity = _two_model_base()
    draft = _draft(base, base_identity)

    assert draft.status is RegistryJoinChangeStatus.NEEDS_REVIEW
    assert draft.decision is None
    assert draft.contract is None

    approved = decide_registry_join_change(
        draft,
        action=DecisionAction.APPROVE,
        expected_revision=1,
        actor_id="steward-join",
        decided_at=NOW + timedelta(minutes=3),
        rationale="Aggregate key evidence confirms the reviewed many-to-one relationship.",
    )
    proposal = PreparedRegistryJoinProposal.create(
        id="join-change-order-customer-v3",
        draft=approved,
        prepared_by="publisher-join",
        prepared_at=NOW + timedelta(minutes=4),
    )
    ready = mark_registry_join_change_ready(approved, proposal)
    candidate = assemble_join_change_registry_version(proposal, base=base)

    assert ready.status is RegistryJoinChangeStatus.READY_FOR_PUBLICATION
    assert ready.prepared_proposal_fingerprint == proposal.fingerprint
    assert candidate.registry.version == base.version + 1
    assert candidate.registry.logical_context.models == base.logical_context.models
    assert candidate.registry.mapping_set.mappings == base.mapping_set.mappings
    assert candidate.registry.physical_bindings == base.physical_bindings
    assert len(candidate.registry.logical_context.joins) == 1
    assert len(candidate.registry.join_contracts.contracts) == 1
    contract = candidate.registry.join_contracts.contracts[0]
    assert contract.status is ApprovalStatus.APPROVED
    assert contract.cardinality is Cardinality.MANY_TO_ONE
    assert contract.fanout_policy is FanoutPolicy.NONE
    assert contract.approval_decision_id == approved.decision.id
    provenance = {item.kind: item for item in candidate.registry.provenance}
    assert provenance[RegistryArtifactKind.LOGICAL_MODELS].decision_ids == (
        "decision-model-customer",
        "decision-model-order",
    )
    assert provenance[RegistryArtifactKind.PHYSICAL_MAPPINGS].decision_ids == (
        "decision-mapping-customer",
        "decision-mapping-order",
    )
    assert provenance[RegistryArtifactKind.JOIN_CONTRACTS].decision_ids == (approved.decision.id,)
    assert candidate.source_proposal_id == proposal.id
    assert candidate.source_proposal_fingerprint == proposal.fingerprint


def test_join_proposal_round_trip_keeps_m33_payloads_unchanged() -> None:
    existing = _proposal()
    before = existing.model_dump(mode="json")
    candidate_before = assemble_publishable_registry_version(existing, base=None)

    base, base_identity = _two_model_base()
    approved = _approved_draft(base, base_identity)
    join_proposal = PreparedRegistryJoinProposal.create(
        id="join-change-roundtrip-v3",
        draft=approved,
        prepared_by="publisher-roundtrip",
        prepared_at=NOW + timedelta(minutes=4),
    )
    checked = PreparedRegistryJoinProposal.model_validate(join_proposal.model_dump(mode="json"))

    assert checked == join_proposal
    assert checked.owner_actor_id == "analyst-join"
    assert existing.model_dump(mode="json") == before
    assert assemble_publishable_registry_version(existing, base=None) == candidate_before

    owner_replay = join_proposal.model_dump(mode="json")
    owner_replay["prepared_by"] = join_proposal.owner_actor_id
    with pytest.raises(ValidationError, match="incomplete or stale"):
        PreparedRegistryJoinProposal.model_validate(owner_replay)


def test_join_approval_requires_safe_non_name_evidence_and_meaningful_rationale() -> None:
    base, base_identity = _two_model_base()
    draft = _draft(base, base_identity)

    with pytest.raises(ValueError, match="meaningful"):
        decide_registry_join_change(
            draft,
            action=DecisionAction.APPROVE,
            expected_revision=1,
            actor_id="steward-join",
            decided_at=NOW + timedelta(minutes=3),
            rationale="looks ok",
        )

    many_many = _draft(
        base,
        base_identity,
        profile=_profile(left_max=2, right_max=2, declared=DeclaredRelationship.NONE),
    )
    with pytest.raises(ValueError, match="safe non-name evidence"):
        decide_registry_join_change(
            many_many,
            action=DecisionAction.APPROVE,
            expected_revision=1,
            actor_id="steward-join",
            decided_at=NOW + timedelta(minutes=3),
            rationale="The relationship was reviewed but remains unsafe to execute.",
        )


def test_rejection_is_immutable_and_cannot_be_prepared() -> None:
    base, base_identity = _two_model_base()
    rejected = decide_registry_join_change(
        _draft(base, base_identity),
        action=DecisionAction.REJECT,
        expected_revision=1,
        actor_id="steward-join",
        decided_at=NOW + timedelta(minutes=3),
        rationale="The relationship meaning is not approved for governed query use.",
    )

    assert rejected.contract is None
    assert rejected.decision is not None
    assert rejected.decision.status is ApprovalStatus.REJECTED
    with pytest.raises(ValueError, match="approved decision"):
        PreparedRegistryJoinProposal.create(
            id="join-change-rejected-v3",
            draft=rejected,
            prepared_by="publisher-join",
            prepared_at=NOW + timedelta(minutes=4),
        )


def test_join_preparation_requires_distinct_publisher_and_current_profile() -> None:
    base, base_identity = _two_model_base()
    approved = _approved_draft(base, base_identity)

    with pytest.raises(ValueError, match="distinct publisher"):
        PreparedRegistryJoinProposal.create(
            id="join-change-sod-v3",
            draft=approved,
            prepared_by="steward-join",
            prepared_at=NOW + timedelta(minutes=4),
        )
    with pytest.raises(ValidationError, match="incomplete or stale"):
        PreparedRegistryJoinProposal.create(
            id="join-change-expired-v3",
            draft=approved,
            prepared_by="publisher-join",
            prepared_at=NOW + timedelta(hours=2),
        )


def test_profile_request_binds_exact_base_target_scan_and_current_job() -> None:
    base, base_identity = _two_model_base()
    draft = _draft(base, base_identity)
    campaign = draft.profile_campaign
    request = campaign.request

    assert request.external_writes_performed is False
    assert request.scan_id == draft.profile_job.scan_id == campaign.scan_id
    assert request.base_evidence == draft.base_evidence
    assert request.proposal == draft.profile_job.bound_proposal
    assert request.execution_target == draft.profile_job.execution_target
    assert RegistryJoinProfileRequest.model_validate(request.model_dump(mode="json")) == request
    assert (
        validate_registry_join_profile_campaign(
            campaign,
            scope=draft.scope,
            base_evidence=draft.base_evidence,
            profile_job=draft.profile_job,
            at=campaign.expires_at - timedelta(microseconds=1),
        )
        == campaign
    )
    with pytest.raises(ValueError, match="stale"):
        validate_registry_join_profile_campaign(
            campaign,
            scope=draft.scope,
            base_evidence=draft.base_evidence,
            profile_job=draft.profile_job,
            at=campaign.expires_at,
        )

    arbitrary_scan_job = _profile_job(
        base,
        base_identity,
        scan_id_override=f"scan_{'2' * 64}",
    )
    with pytest.raises(ValidationError, match="campaign authority is incomplete"):
        RegistryJoinChangeDraft.create(
            id="join-change-arbitrary-scan",
            workspace_id="workspace-a",
            owner_actor_id="analyst-join",
            scope=_proposal().scope,
            base=base,
            base_registry=base_identity,
            profile_job=arbitrary_scan_job,
            created_at=NOW + timedelta(minutes=2),
        )


def test_profile_request_identity_changes_for_a_fresh_attempt() -> None:
    base, base_identity = _two_model_base()
    first = _draft(base, base_identity).profile_campaign.request
    second = RegistryJoinProfileRequest.create(
        scope=first.scope,
        base_evidence=first.base_evidence,
        proposal=first.proposal,
        execution_target=first.execution_target,
        requested_at=first.requested_at + timedelta(hours=2),
    )

    assert second.scan_id != first.scan_id
    assert second.fingerprint != first.fingerprint


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reader_user", None),
        ("transaction_read_only", False),
        ("statement_timeout_ms", None),
        ("statement_timeout_ms", 99),
        ("statement_timeout_ms", 60_001),
    ],
)
def test_profile_campaign_rejects_missing_read_only_attestation(
    field: str,
    value: object,
) -> None:
    base, base_identity = _two_model_base()
    unsafe = _profile().model_copy(update={field: value})

    with pytest.raises(ValueError, match="read-only reader and bounded timeout"):
        _draft(base, base_identity, profile=unsafe)


def test_profile_request_rejects_cross_connection_authority() -> None:
    base, base_identity = _two_model_base()
    job = _profile_job(base, base_identity)
    other_connection = SemanticJoinProfileProposal(
        connection_id=CatalogConnectionId("warehouse-b"),
        proposal=job.proposal,
    )
    with pytest.raises(ValueError, match="another governed connection"):
        resolve_registry_join_base_evidence(
            scope=_proposal().scope,
            base=base,
            base_registry=base_identity,
            proposal=other_connection,
        )

    evidence = resolve_registry_join_base_evidence(
        scope=_proposal().scope,
        base=base,
        base_registry=base_identity,
        proposal=job.bound_proposal,
    )
    assert job.execution_target is not None
    wrong_target = job.execution_target.model_copy(
        update={"connection_id": CatalogConnectionId("warehouse-b")}
    )
    with pytest.raises(ValueError, match="authority is inconsistent"):
        RegistryJoinProfileRequest.create(
            scope=_proposal().scope,
            base_evidence=evidence,
            proposal=job.bound_proposal,
            execution_target=wrong_target,
            requested_at=NOW,
        )


def test_ready_join_change_can_be_superseded_without_losing_lineage() -> None:
    base, base_identity = _two_model_base()
    approved = _approved_draft(base, base_identity)
    proposal = PreparedRegistryJoinProposal.create(
        id="join-change-superseded-v3",
        draft=approved,
        prepared_by="publisher-supersede",
        prepared_at=NOW + timedelta(minutes=4),
    )
    ready = mark_registry_join_change_ready(approved, proposal)
    superseded = supersede_registry_join_change(
        ready,
        superseded_at=NOW + timedelta(minutes=5),
    )

    assert superseded.status is RegistryJoinChangeStatus.SUPERSEDED
    assert superseded.prepared_proposal_id == proposal.id
    assert superseded.prepared_proposal_fingerprint == proposal.fingerprint
    assert RegistryJoinChangeDraft.model_validate(superseded.model_dump(mode="json")) == superseded
    with pytest.raises(ValueError, match="does not match its draft"):
        mark_registry_join_change_ready(superseded, proposal)


def test_join_keys_must_be_exact_active_mapping_transformations() -> None:
    base, base_identity = _two_model_base()
    job = _profile_job(base, base_identity)
    wrong = job.proposal.model_copy(
        update={
            "left_key": job.proposal.left_key.model_copy(
                update={"physical_field": job.proposal.right_key.physical_field}
            )
        }
    )
    altered = job.model_copy(update={"proposal": wrong})

    with pytest.raises(
        (ValidationError, ValueError),
        match=r"proposal fingerprint|active mapping|self joins",
    ):
        RegistryJoinChangeDraft.create(
            id="join-change-altered-key",
            workspace_id="workspace-a",
            owner_actor_id="analyst-join",
            scope=_proposal().scope,
            base=base,
            base_registry=base_identity,
            profile_job=altered,
            created_at=NOW + timedelta(minutes=2),
        )


def test_assembler_rejects_base_drift_and_join_id_collision() -> None:
    base, base_identity = _two_model_base()
    proposal = PreparedRegistryJoinProposal.create(
        id="join-change-drift-v3",
        draft=_approved_draft(base, base_identity),
        prepared_by="publisher-join",
        prepared_at=NOW + timedelta(minutes=4),
    )

    with pytest.raises(ValueError, match="base changed"):
        assemble_join_change_registry_version(
            proposal,
            base=base.model_copy(update={"version": base.version + 1}),
        )

    first = assemble_join_change_registry_version(proposal, base=base).registry
    next_identity = OnboardingRegistryBase(
        registry_version=first.version,
        registry_fingerprint=first.fingerprint,
        activation_generation=base_identity.activation_generation + 1,
        active_pointer_fingerprint=FP_F,
    )
    next_draft = _draft(first, next_identity)
    next_approved = decide_registry_join_change(
        next_draft,
        action=DecisionAction.APPROVE,
        expected_revision=1,
        actor_id="steward-next",
        decided_at=NOW + timedelta(minutes=7),
        rationale="The duplicate relationship was deliberately reviewed for collision handling.",
    )
    next_proposal = PreparedRegistryJoinProposal.create(
        id="join-change-collision-v4",
        draft=next_approved,
        prepared_by="publisher-next",
        prepared_at=NOW + timedelta(minutes=8),
    )
    with pytest.raises(ValueError, match="cannot replace an active join"):
        assemble_join_change_registry_version(next_proposal, base=first)


def test_assembler_rejects_new_edges_inside_an_existing_component() -> None:
    base, base_identity = _two_model_base()
    first_proposal = PreparedRegistryJoinProposal.create(
        id="join-change-first-edge-v3",
        draft=_approved_draft(base, base_identity),
        prepared_by="publisher-first-edge",
        prepared_at=NOW + timedelta(minutes=4),
    )
    first = assemble_join_change_registry_version(first_proposal, base=base).registry
    next_identity = OnboardingRegistryBase(
        registry_version=first.version,
        registry_fingerprint=first.fingerprint,
        activation_generation=base_identity.activation_generation + 1,
        active_pointer_fingerprint=FP_F,
    )
    alternate = decide_registry_join_change(
        _draft(
            first,
            next_identity,
            proposal_id="order_customer_alternate",
        ),
        action=DecisionAction.APPROVE,
        expected_revision=1,
        actor_id="steward-alternate",
        decided_at=NOW + timedelta(minutes=7),
        rationale="A second reviewed edge must not create ambiguity in one component.",
    )
    alternate_proposal = PreparedRegistryJoinProposal.create(
        id="join-change-alternate-edge-v4",
        draft=alternate,
        prepared_by="publisher-alternate",
        prepared_at=NOW + timedelta(minutes=8),
    )

    with pytest.raises(ValueError, match="already in the same component"):
        assemble_join_change_registry_version(alternate_proposal, base=first)


def _two_model_base() -> tuple[object, OnboardingRegistryBase]:
    first = assemble_publishable_registry_version(_proposal(), base=None).registry
    first_identity = OnboardingRegistryBase(
        registry_version=first.version,
        registry_fingerprint=first.fingerprint,
        activation_generation=3,
        active_pointer_fingerprint=FP_D,
    )
    second_proposal = _proposal(
        model_id="Customer",
        logical_field="Customer.customer_id",
        physical_field="crm.customers.customer_id",
        proposal_id="proposal-customers-v2",
        draft_id="customers-onboarding",
        model_decision="decision-model-customer",
        mapping_decision="decision-mapping-customer",
        observed_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,opaque.customers,PROD)",
        asset_id="urn:li:dataset:(urn:li:dataPlatform:postgres,opaque.customers,PROD)",
        base=first_identity,
    )
    second = assemble_publishable_registry_version(second_proposal, base=first).registry
    return second, OnboardingRegistryBase(
        registry_version=second.version,
        registry_fingerprint=second.fingerprint,
        activation_generation=4,
        active_pointer_fingerprint=FP_E,
    )


def _draft(
    base: object,
    base_identity: OnboardingRegistryBase,
    *,
    profile: RelationshipProfile | None = None,
    proposal_id: str = "order_customer",
) -> RegistryJoinChangeDraft:
    from schemabridge.domain.semantic_registry import GovernedSemanticRegistrySnapshot

    assert isinstance(base, GovernedSemanticRegistrySnapshot)
    return RegistryJoinChangeDraft.create(
        id="join-change-order-customer",
        workspace_id="workspace-a",
        owner_actor_id="analyst-join",
        scope=_proposal().scope,
        base=base,
        base_registry=base_identity,
        profile_job=_profile_job(
            base,
            base_identity,
            profile=profile,
            proposal_id=proposal_id,
        ),
        created_at=NOW + timedelta(minutes=2),
    )


def _approved_draft(
    base: object,
    base_identity: OnboardingRegistryBase,
) -> RegistryJoinChangeDraft:
    return decide_registry_join_change(
        _draft(base, base_identity),
        action=DecisionAction.APPROVE,
        expected_revision=1,
        actor_id="steward-join",
        decided_at=NOW + timedelta(minutes=3),
        rationale="Aggregate key evidence confirms the reviewed many-to-one relationship.",
    )


def _profile_job(
    base: object,
    base_identity: OnboardingRegistryBase,
    *,
    profile: RelationshipProfile | None = None,
    scan_id_override: str | None = None,
    proposal_id: str = "order_customer",
) -> SemanticJoinProfileJob:
    from schemabridge.domain.semantic_registry import GovernedSemanticRegistrySnapshot

    assert isinstance(base, GovernedSemanticRegistrySnapshot)
    mappings = {item.mapping.logical_field.root: item.mapping for item in base.mapping_set.mappings}
    left = mappings["Order.order_id"]
    right = mappings["Customer.customer_id"]
    proposal = SemanticJoinProfileProposal(
        connection_id=CatalogConnectionId("warehouse-a"),
        proposal=JoinProposal(
            id=proposal_id,
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
        ),
    )
    budget = QueryCostBudget(
        explain_timeout_ms=1_000,
        max_response_bytes=65_536,
        max_total_cost=Decimal("10000"),
        max_estimated_rows=100_000,
        max_plan_nodes=1_000,
        max_plan_depth=32,
        max_plan_width=16_384,
    )
    target = SemanticJoinProfileTargetRef.from_target(
        GovernedExecutionTarget(
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
    )
    scope = _proposal().scope
    evidence = resolve_registry_join_base_evidence(
        scope=scope,
        base=base,
        base_registry=base_identity,
        proposal=proposal,
    )
    request = RegistryJoinProfileRequest.create(
        scope=scope,
        base_evidence=evidence,
        proposal=proposal,
        execution_target=target,
        requested_at=NOW,
    )
    requested = SemanticJoinProfileJob.requested(
        workspace_id="workspace-a",
        scan_id=scan_id_override or request.scan_id,
        proposal=proposal,
        execution_target=target,
        requested_at=NOW,
        connector_contract_version=1,
    )
    leased = claim_semantic_join_profile_job(
        requested,
        worker_id="profile-worker-a",
        lease_capability="profile-capability-0123456789abcdef",
        claimed_at=NOW + timedelta(seconds=30),
        lease_expires_at=NOW + timedelta(minutes=3),
    )
    return complete_semantic_join_profile_job(
        leased,
        worker_id="profile-worker-a",
        lease_capability="profile-capability-0123456789abcdef",
        fencing_token=leased.fencing_token,
        profile=profile or _profile(),
        completed_at=NOW + timedelta(minutes=1),
        retain_until=NOW + timedelta(days=30),
    )


def _profile(
    *,
    left_max: int = 2,
    right_max: int = 1,
    declared: DeclaredRelationship = DeclaredRelationship.LEFT_FOREIGN_KEY_TO_RIGHT,
) -> RelationshipProfile:
    return RelationshipProfile(
        left_row_count=100,
        right_row_count=80,
        left_null_count=0,
        right_null_count=0,
        left_invalid_count=0,
        right_invalid_count=0,
        left_distinct_valid=80 if left_max > 1 else 100,
        right_distinct_valid=60 if right_max > 1 else 80,
        matching_distinct_keys=60 if right_max > 1 else 80,
        left_max_multiplicity=left_max,
        right_max_multiplicity=right_max,
        declared_relationship=declared,
        reader_user="schemabridge_reader",
        transaction_read_only=True,
        statement_timeout_ms=2_000,
    )
