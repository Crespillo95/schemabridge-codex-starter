"""Adversarial M35 Phase-B model replacement/remediation contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from tests.unit.test_registry_join_changes import (
    _approved_draft,
    _profile,
    _profile_job,
    _two_model_base,
)
from tests.unit.test_registry_publication_v2 import FP_A, _mapping

from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.concepts import CanonicalType, LogicalFieldRef, LogicalModelRef
from schemabridge.domain.decisions import (
    ApprovalStatus,
    DecisionAction,
    DecisionRecord,
    DecisionTargetType,
)
from schemabridge.domain.joins import (
    Cardinality,
    FanoutPolicy,
    JoinContract,
    JoinProposal,
    NormalizedJoinKey,
)
from schemabridge.domain.registry_changes import (
    PreparedRegistryJoinProposal,
    assemble_join_change_registry_version,
)
from schemabridge.domain.registry_model_changes import (
    PreparedRegistryModelReplacementProposal,
    RegistryIncidentJoinPreservation,
    RegistryIncidentJoinRemoval,
    RegistryIncidentJoinUpsert,
    RegistryModelChangeAuthority,
    RegistryModelJoinProfileWitness,
    assemble_model_replacement_registry_version,
    resolve_registry_model_replacement_base,
)
from schemabridge.domain.request_context import LogicalFieldRole
from schemabridge.domain.semantic_change import (
    CatalogGenerationObservation,
    CatalogGenerationVector,
    GovernedJoinRef,
    GovernedMappingRef,
    SemanticChangeFinding,
    SemanticChangeImpact,
    SemanticChangeInspectionContext,
    SemanticChangeKind,
    SemanticChangeReport,
    SemanticChangeSeverity,
    SemanticChangeStatus,
    SemanticDependencyIndexState,
    SemanticImpactKind,
    SemanticImpactSet,
    SemanticImpactSummary,
    semantic_change_fingerprint,
)
from schemabridge.domain.semantic_onboarding import (
    OnboardingRegistryBase,
    PreparedSemanticOnboardingProposal,
    SemanticFieldDefinition,
    SemanticModelDefinition,
    SemanticModelProposal,
    SemanticOnboardingDraft,
)
from schemabridge.domain.semantic_registry import (
    GovernedSemanticRegistrySnapshot,
    RegistryArtifactKind,
)

NOW = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)
FP_1 = "1" * 64
FP_2 = "2" * 64
FP_3 = "3" * 64
FP_4 = "4" * 64


def test_planned_replacement_preserves_unaffected_artifacts_and_exact_incident_join() -> None:
    base, identity = _joined_base()
    evidence = _replacement_base(base, identity)
    replacement = _replacement(identity, physical_field="crm.customers.customer_id")
    proposal = _prepared_change(
        base=evidence,
        replacement=replacement,
        incident_changes=(RegistryIncidentJoinPreservation(base=evidence.incident_joins[0]),),
    )

    candidate = assemble_model_replacement_registry_version(proposal, base=base)
    registry = candidate.registry

    assert registry.version == base.version + 1
    assert registry.logical_context.model_index()["Customer"].version == 2
    assert (
        registry.logical_context.model_index()["Order"]
        == base.logical_context.model_index()["Order"]
    )
    assert registry.join_contracts.contracts == base.join_contracts.contracts
    assert registry.logical_context.joins == base.logical_context.joins
    assert tuple(
        item
        for item in registry.mapping_set.mappings
        if item.mapping.logical_field.root.startswith("Order.")
    ) == tuple(
        item
        for item in base.mapping_set.mappings
        if item.mapping.logical_field.root.startswith("Order.")
    )
    assert registry.fingerprint != base.fingerprint
    provenance = {item.kind: item for item in registry.provenance}
    assert (
        "decision-model-customer-v2" in provenance[RegistryArtifactKind.LOGICAL_MODELS].decision_ids
    )
    assert provenance[RegistryArtifactKind.PHYSICAL_MAPPINGS].decision_ids == (
        "decision-mapping-customer-v2",
        "decision-mapping-order",
    )
    assert candidate.source_proposal_fingerprint == proposal.fingerprint
    assert (
        PreparedRegistryModelReplacementProposal.model_validate(proposal.model_dump(mode="json"))
        == proposal
    )
    assert candidate.review_decision_ids == tuple(
        sorted(
            {
                *candidate.active_decision_ids,
                *proposal.decision_ids,
            }
        )
    )


def test_model_change_requires_the_exact_active_base_and_next_model_version() -> None:
    base, identity = _joined_base()
    evidence = _replacement_base(base, identity)
    replacement = _replacement(identity, model_version=3)

    with pytest.raises(ValidationError, match=r"fingerprint|incomplete or stale"):
        _prepared_change(
            base=evidence,
            replacement=replacement,
            incident_changes=(RegistryIncidentJoinPreservation(base=evidence.incident_joins[0]),),
        )

    valid = _prepared_change(
        base=evidence,
        replacement=_replacement(identity),
        incident_changes=(RegistryIncidentJoinPreservation(base=evidence.incident_joins[0]),),
    )
    with pytest.raises(ValueError, match="base changed"):
        assemble_model_replacement_registry_version(
            valid,
            base=base.model_copy(update={"version": base.version + 1}),
        )


def test_replacement_rejects_incomplete_mapping_binding_and_double_physical_meaning() -> None:
    base, identity = _joined_base()
    evidence = _replacement_base(base, identity)
    two_fields = _replacement(identity)
    definition = two_fields.model.definition.model_copy(
        update={
            "fields": (
                *two_fields.model.definition.fields,
                SemanticFieldDefinition(
                    id=LogicalFieldRef("Customer.segment"),
                    canonical_type=CanonicalType.STRING,
                    role=LogicalFieldRole.ATTRIBUTE,
                    definition="Governed customer segment.",
                ),
            )
        }
    )
    incomplete = two_fields.model_copy(
        update={"model": two_fields.model.model_copy(update={"definition": definition})}
    )
    with pytest.raises(ValidationError, match=r"fingerprint|incomplete or stale"):
        _prepared_change(
            base=evidence,
            replacement=incomplete,
            incident_changes=(RegistryIncidentJoinPreservation(base=evidence.incident_joins[0]),),
        )

    collision = _replacement(
        identity,
        physical_field="sales.orders.order_id",
        asset_id="urn:li:dataset:(urn:li:dataPlatform:postgres,opaque.orders,PROD)",
    )
    with pytest.raises(ValidationError, match="two active logical meanings"):
        _prepared_change(
            base=evidence,
            replacement=collision,
            incident_changes=(
                RegistryIncidentJoinRemoval(
                    base=evidence.incident_joins[0],
                    decision=_join_removal_decision(evidence.incident_joins[0].contract),
                ),
            ),
        )


def test_every_incident_join_must_be_accounted_and_preserve_rejects_stale_key() -> None:
    base, identity = _joined_base()
    evidence = _replacement_base(base, identity)

    with pytest.raises(ValidationError, match="incomplete or stale"):
        _prepared_change(
            base=evidence,
            replacement=_replacement(identity),
            incident_changes=(),
        )

    with pytest.raises(ValidationError, match="stale for the replacement mappings"):
        _prepared_change(
            base=evidence,
            replacement=_replacement(
                identity,
                physical_field="crm.customers.customer_key_v2",
                asset_id=("urn:li:dataset:(urn:li:dataPlatform:postgres,opaque.customers_v2,PROD)"),
            ),
            incident_changes=(RegistryIncidentJoinPreservation(base=evidence.incident_joins[0]),),
        )


def test_explicit_removal_drops_only_the_incident_join_and_keeps_removal_decision_nonactive() -> (
    None
):
    base, identity = _joined_base()
    evidence = _replacement_base(base, identity)
    removal = _join_removal_decision(evidence.incident_joins[0].contract)
    proposal = _prepared_change(
        base=evidence,
        replacement=_replacement(
            identity,
            physical_field="crm.customers.customer_key_v2",
            asset_id=("urn:li:dataset:(urn:li:dataPlatform:postgres,opaque.customers_v2,PROD)"),
        ),
        incident_changes=(
            RegistryIncidentJoinRemoval(
                base=evidence.incident_joins[0],
                decision=removal,
            ),
        ),
    )

    candidate = assemble_model_replacement_registry_version(proposal, base=base)

    assert candidate.registry.join_contracts.contracts == ()
    assert candidate.registry.logical_context.joins == ()
    assert removal.id in candidate.review_decision_ids
    assert removal.id not in candidate.active_decision_ids


def test_upsert_requires_fresh_candidate_bound_read_only_profile_and_new_decision() -> None:
    base, identity = _joined_base()
    evidence = _replacement_base(base, identity)
    replacement = _replacement(
        identity,
        physical_field="crm.customers.customer_key_v2",
        asset_id="urn:li:dataset:(urn:li:dataPlatform:postgres,opaque.customers_v2,PROD)",
    )
    upsert = _join_upsert(base, identity, evidence, replacement)
    proposal = _prepared_change(
        base=evidence,
        replacement=replacement,
        incident_changes=(upsert,),
    )

    candidate = assemble_model_replacement_registry_version(proposal, base=base)
    contract = candidate.registry.join_contracts.contracts[0]

    assert contract.version == 2
    assert contract.right_key.physical_field.root == "crm.customers.customer_key_v2"
    assert contract.approval_decision_id == "decision-order-customer-v2"
    assert upsert.profile_witness.result.profile.transaction_read_only is True
    assert upsert.profile_witness.is_current(proposal.prepared_at)
    assert (
        PreparedRegistryModelReplacementProposal.model_validate(proposal.model_dump(mode="json"))
        == proposal
    )

    reused = upsert.profile_witness.model_copy(update={"change_id": "another-change"})
    forged = upsert.model_copy(update={"profile_witness": reused})
    with pytest.raises(ValidationError, match="reused across a change"):
        _prepared_change(
            base=evidence,
            replacement=replacement,
            incident_changes=(forged,),
        )

    expired_replacement = _replacement(
        identity,
        physical_field="crm.customers.customer_key_v2",
        asset_id="urn:li:dataset:(urn:li:dataPlatform:postgres,opaque.customers_v2,PROD)",
        prepared_at=NOW + timedelta(hours=2),
    )
    expired_upsert = _join_upsert(base, identity, evidence, expired_replacement)
    with pytest.raises(ValidationError, match="reused across a change"):
        _prepared_change(
            base=evidence,
            replacement=expired_replacement,
            incident_changes=(expired_upsert,),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reader_user", None),
        ("transaction_read_only", False),
        ("statement_timeout_ms", 99),
        ("statement_timeout_ms", 60_001),
    ],
)
def test_upsert_profile_rejects_missing_read_only_attestation(
    field: str,
    value: object,
) -> None:
    base, identity = _joined_base()
    evidence = _replacement_base(base, identity)
    replacement = _replacement(identity)
    profile = _profile().model_copy(update={field: value})
    incident = evidence.incident_joins[0]
    target = _profile_job(base, identity).execution_target
    assert target is not None

    with pytest.raises(ValueError, match="incomplete or unsafe"):
        RegistryModelJoinProfileWitness.create(
            change_id="replace-customer-change",
            base=evidence,
            replacement=replacement,
            incident=incident,
            proposal=JoinProposal(
                id=incident.contract.id,
                left_key=incident.contract.left_key,
                right_key=incident.contract.right_key,
            ),
            execution_target=target,
            profile=profile,
            completed_at=NOW + timedelta(minutes=3),
        )


def test_planned_change_requires_complete_exact_dependency_snapshot() -> None:
    base, identity = _joined_base()
    incomplete = _dependency_context(base, identity, complete=False)
    with pytest.raises(ValueError, match="incomplete or stale"):
        resolve_registry_model_replacement_base(
            scope=_scope(base),
            base=base,
            base_registry=identity,
            target_model_id=LogicalModelRef("Customer"),
            dependency_context=incomplete,
        )

    changed = _dependency_context(base, identity).model_copy(update={"registry_fingerprint": FP_4})
    with pytest.raises(ValueError, match="incomplete or stale"):
        resolve_registry_model_replacement_base(
            scope=_scope(base),
            base=base,
            base_registry=identity,
            target_model_id=LogicalModelRef("Customer"),
            dependency_context=changed,
        )


def test_m26_remediation_requires_current_blocked_report_full_impacts_and_exact_findings() -> None:
    base, identity = _joined_base()
    evidence = _replacement_base(base, identity)
    report, impacts = _blocking_report(evidence)
    finding_id = report.findings[0].id

    authority = RegistryModelChangeAuthority.remediation(
        evidence,
        report=report,
        impacts=impacts,
        resolved_finding_ids=(finding_id,),
    )
    assert authority.report == report

    with pytest.raises(ValueError, match="not exact"):
        RegistryModelChangeAuthority.remediation(
            evidence,
            report=report,
            impacts=impacts,
            resolved_finding_ids=(),
        )
    with pytest.raises(ValueError, match="not exact"):
        RegistryModelChangeAuthority.remediation(
            evidence,
            report=report.model_copy(update={"status": SemanticChangeStatus.REVIEW_REQUIRED}),
            impacts=impacts,
            resolved_finding_ids=(finding_id,),
        )
    incomplete = SemanticImpactSet.create(
        impacts=impacts.impacts,
        complete=False,
        watermark=impacts.watermark,
        dependency_index_fingerprint=impacts.dependency_index_fingerprint,
    )
    with pytest.raises(ValueError, match="not exact"):
        RegistryModelChangeAuthority.remediation(
            evidence,
            report=report,
            impacts=incomplete,
            resolved_finding_ids=(finding_id,),
        )


def test_m26_remediation_assembles_but_cannot_claim_unrelated_blocking_finding() -> None:
    base, identity = _joined_base()
    evidence = _replacement_base(base, identity)
    report, impacts = _blocking_report(evidence)
    authority = RegistryModelChangeAuthority.remediation(
        evidence,
        report=report,
        impacts=impacts,
        resolved_finding_ids=(report.findings[0].id,),
    )
    removal = RegistryIncidentJoinRemoval(
        base=evidence.incident_joins[0],
        decision=_join_removal_decision(evidence.incident_joins[0].contract),
    )
    proposal = _prepared_change(
        base=evidence,
        replacement=_replacement(
            identity,
            physical_field="crm.customers.customer_key_v2",
            asset_id=("urn:li:dataset:(urn:li:dataPlatform:postgres,opaque.customers_v2,PROD)"),
        ),
        incident_changes=(removal,),
        authority=authority,
    )
    candidate = assemble_model_replacement_registry_version(proposal, base=base)
    assert candidate.registry.version == identity.next_registry_version

    unrelated_ref = next(
        item
        for item in evidence.dependency_context.mappings
        if item.logical_field.root.startswith("Order.")
    )
    unrelated_report, unrelated_impacts = _blocking_report(
        evidence,
        mapping=unrelated_ref,
    )
    with pytest.raises(ValueError, match="not exact"):
        RegistryModelChangeAuthority.remediation(
            evidence,
            report=unrelated_report,
            impacts=unrelated_impacts,
            resolved_finding_ids=(unrelated_report.findings[0].id,),
        )


def test_preparation_enforces_separation_of_duties_and_temporal_order() -> None:
    base, identity = _joined_base()
    evidence = _replacement_base(base, identity)
    replacement = _replacement(identity)
    incident_changes = (RegistryIncidentJoinPreservation(base=evidence.incident_joins[0]),)

    with pytest.raises(ValidationError, match="incomplete or stale"):
        _prepared_change(
            base=evidence,
            replacement=replacement,
            incident_changes=incident_changes,
            owner_actor_id="publisher-model",
        )

    late_model_decision = _model_decision().model_copy(
        update={"decided_at": replacement.prepared_at + timedelta(seconds=1)}
    )
    with pytest.raises(ValidationError, match="incomplete or stale"):
        _prepared_change(
            base=evidence,
            replacement=replacement,
            incident_changes=incident_changes,
            model_decision=late_model_decision,
        )


def _joined_base() -> tuple[GovernedSemanticRegistrySnapshot, OnboardingRegistryBase]:
    base, identity = _two_model_base()
    draft = _approved_draft(base, identity)
    join_proposal = PreparedRegistryJoinProposal.create(
        id="join-change-order-customer-v3",
        draft=draft,
        prepared_by="publisher-join",
        prepared_at=datetime(2026, 8, 3, 12, 4, tzinfo=UTC),
    )
    joined = assemble_join_change_registry_version(join_proposal, base=base).registry
    return joined, OnboardingRegistryBase(
        registry_version=joined.version,
        registry_fingerprint=joined.fingerprint,
        activation_generation=5,
        active_pointer_fingerprint=FP_1,
    )


def _scope(base: GovernedSemanticRegistrySnapshot):
    from schemabridge.domain.semantic_registry import SemanticRegistryScope

    return SemanticRegistryScope(
        workspace_id=base.physical_bindings[0].workspace_id,
        catalog_scope=base.catalog_scope,
        registry_id=base.registry_id,
    )


def _dependency_context(
    base: GovernedSemanticRegistrySnapshot,
    identity: OnboardingRegistryBase,
    *,
    complete: bool = True,
) -> SemanticChangeInspectionContext:
    scope = _scope(base)
    mappings = tuple(
        GovernedMappingRef(
            logical_field=item.mapping.logical_field,
            physical_field=item.mapping.physical_field,
            version=item.mapping.version,
            approval_decision_id=item.approval_decision_id or "missing",
            physical_type=item.physical_type,
        )
        for item in base.mapping_set.mappings
    )
    joins = tuple(
        GovernedJoinRef(
            contract_id=item.id,
            version=item.version,
            approval_decision_id=item.approval_decision_id or "missing",
            left_field=item.left_key.physical_field,
            right_field=item.right_key.physical_field,
            cardinality=item.cardinality,
            fanout_policy=item.fanout_policy,
        )
        for item in base.join_contracts.contracts
    )
    dependency = SemanticDependencyIndexState(
        scope=scope,
        watermark=17,
        fingerprint=FP_2,
        complete=complete,
    )
    return SemanticChangeInspectionContext.create(
        scope=scope,
        pointer_generation=identity.activation_generation or 1,
        pointer_fingerprint=identity.active_pointer_fingerprint or FP_1,
        pointer_transition_id="registry-transition-5",
        registry_version=base.version,
        registry_fingerprint=base.fingerprint,
        mappings=mappings,
        joins=joins,
        dependency_index=dependency,
    )


def _replacement_base(
    base: GovernedSemanticRegistrySnapshot,
    identity: OnboardingRegistryBase,
):
    return resolve_registry_model_replacement_base(
        scope=_scope(base),
        base=base,
        base_registry=identity,
        target_model_id=LogicalModelRef("Customer"),
        dependency_context=_dependency_context(base, identity),
    )


def _replacement(
    identity: OnboardingRegistryBase,
    *,
    physical_field: str = "crm.customers.customer_id",
    asset_id: str = ("urn:li:dataset:(urn:li:dataPlatform:postgres,opaque.customers,PROD)"),
    model_version: int = 2,
    prepared_at: datetime = NOW + timedelta(minutes=5),
) -> PreparedSemanticOnboardingProposal:
    scope = _scope(_joined_base()[0])
    mapping = _mapping(
        mapping_id="mapping-customer-v2",
        logical_field="Customer.customer_id",
        physical_field=physical_field,
        decision_id="decision-mapping-customer-v2",
        observed_urn=asset_id,
        asset_id=asset_id,
        workspace_id=scope.workspace_id,
        catalog_scope=scope.catalog_scope,
        catalog_generation=7,
        catalog_generation_fingerprint=FP_A,
    )
    draft = SemanticOnboardingDraft(
        id="replace-customer-draft",
        workspace_id=scope.workspace_id,
        owner_actor_id="analyst-model",
        scope=scope,
        connection_id=CatalogConnectionId("warehouse-a"),
        catalog_generation=7,
        catalog_generation_fingerprint=FP_A,
        base_registry=identity,
        model=SemanticModelProposal(
            definition=SemanticModelDefinition(
                id=LogicalModelRef("Customer"),
                description="Governed replacement Customer business model.",
                fields=(
                    SemanticFieldDefinition(
                        id=LogicalFieldRef("Customer.customer_id"),
                        canonical_type=CanonicalType.STRING,
                        role=LogicalFieldRole.IDENTIFIER,
                        definition="Stable replacement customer identifier.",
                    ),
                ),
                version=model_version,
            ),
            status=ApprovalStatus.APPROVED,
            decision_id="decision-model-customer-v2",
            decided_by="steward-model",
        ),
        mappings=(mapping,),
        created_at=NOW,
        updated_at=NOW + timedelta(minutes=4),
    )
    return PreparedSemanticOnboardingProposal.create(
        id="replace-customer-v4",
        draft=draft,
        decision_ids=(
            "decision-mapping-customer-v2",
            "decision-model-customer-v2",
        ),
        prepared_by="publisher-model",
        prepared_at=prepared_at,
    )


def _model_decision() -> DecisionRecord:
    return DecisionRecord(
        id="decision-model-customer-v2",
        target_type=DecisionTargetType.LOGICAL_MODEL,
        target_id="Customer",
        action=DecisionAction.APPROVE,
        status=ApprovalStatus.APPROVED,
        actor="steward-model",
        decided_at=NOW + timedelta(minutes=1),
        source_version=1,
        resulting_version=2,
        rationale="The replacement model meaning and complete field set were reviewed.",
        evidence=("approved_change_request:customer-v2",),
        risks=("Dependent queries require post-activation M26 inspection.",),
    )


def _mapping_decision() -> DecisionRecord:
    return DecisionRecord(
        id="decision-mapping-customer-v2",
        target_type=DecisionTargetType.COLUMN_MAPPING,
        target_id="mapping-customer-v2",
        action=DecisionAction.APPROVE,
        status=ApprovalStatus.APPROVED,
        actor="steward-a",
        decided_at=NOW + timedelta(minutes=2),
        source_version=1,
        resulting_version=2,
        rationale="The exact physical coordinate and transformation were reviewed.",
        evidence=("catalog_generation:7",),
        risks=("Identifier ownership was reviewed by the steward.",),
    )


def _outer_decision() -> DecisionRecord:
    return DecisionRecord(
        id="decision-registry-model-replacement-customer-v2",
        target_type=DecisionTargetType.LOGICAL_MODEL,
        target_id="Customer",
        action=DecisionAction.APPROVE,
        status=ApprovalStatus.APPROVED,
        actor="steward-outer-model",
        decided_at=NOW + timedelta(minutes=5),
        source_version=1,
        resulting_version=2,
        rationale="The complete governed replacement delta was reviewed as one outer change.",
        evidence=("registry_model_authority:approved",),
        risks=("Activation requires a new post-publication M26 inspection.",),
    )


def _prepared_change(
    *,
    base,
    replacement: PreparedSemanticOnboardingProposal,
    incident_changes,
    authority: RegistryModelChangeAuthority | None = None,
    owner_actor_id: str = "analyst-model",
    model_decision: DecisionRecord | None = None,
) -> PreparedRegistryModelReplacementProposal:
    return PreparedRegistryModelReplacementProposal.create(
        id="registry-model-change-fixture",
        draft_id="replace-customer-change",
        draft_revision=2,
        draft_fingerprint=FP_4,
        owner_actor_id=owner_actor_id,
        base=base,
        replacement=replacement,
        outer_decision=_outer_decision(),
        model_decision=model_decision or _model_decision(),
        mapping_decisions=(_mapping_decision(),),
        authority=authority or RegistryModelChangeAuthority.planned(base),
        incident_join_changes=incident_changes,
        risks=("Activation remains separate and a new M26 inspection is mandatory.",),
        created_at=NOW,
        prepared_by="publisher-registry-change",
        prepared_at=replacement.prepared_at + timedelta(minutes=1),
    )


def _join_removal_decision(contract: JoinContract) -> DecisionRecord:
    return DecisionRecord(
        id="decision-order-customer-remove-v2",
        target_type=DecisionTargetType.JOIN_CONTRACT,
        target_id=contract.id,
        action=DecisionAction.REJECT,
        status=ApprovalStatus.REJECTED,
        actor="steward-join-v2",
        decided_at=NOW + timedelta(minutes=3),
        source_version=contract.version,
        resulting_version=contract.version + 1,
        rationale="The old customer key relationship is explicitly retired by this change.",
        evidence=("approved_change_request:customer-key-retirement",),
        risks=("Dependent multi-table requests remain blocked until a new join is approved.",),
    )


def _join_upsert(
    base: GovernedSemanticRegistrySnapshot,
    identity: OnboardingRegistryBase,
    evidence,
    replacement: PreparedSemanticOnboardingProposal,
) -> RegistryIncidentJoinUpsert:
    incident = evidence.incident_joins[0]
    mapping = replacement.mappings[0]
    proposal = JoinProposal(
        id=incident.contract.id,
        left_key=incident.contract.left_key,
        right_key=NormalizedJoinKey(
            logical_field=mapping.logical_field,
            physical_field=mapping.observation.physical_field,
            transformation_plan=mapping.transformation_plan,
        ),
        default_join_type=incident.contract.default_join_type,
    )
    target = _profile_job(base, identity).execution_target
    assert target is not None
    witness = RegistryModelJoinProfileWitness.create(
        change_id="replace-customer-change",
        base=evidence,
        replacement=replacement,
        incident=incident,
        proposal=proposal,
        execution_target=target,
        profile=_profile(),
        completed_at=NOW + timedelta(minutes=3),
    )
    decision = DecisionRecord(
        id="decision-order-customer-v2",
        target_type=DecisionTargetType.JOIN_CONTRACT,
        target_id=incident.contract.id,
        action=DecisionAction.APPROVE,
        status=ApprovalStatus.APPROVED,
        actor="steward-join-v2",
        decided_at=NOW + timedelta(minutes=4),
        source_version=incident.contract.version,
        resulting_version=incident.contract.version + 1,
        rationale="Fresh aggregate evidence confirms the replacement customer relationship.",
        evidence=("aggregate_profile:replacement-key-overlap",),
        risks=("Replacement join requires post-activation M26 inspection.",),
    )
    contract = JoinContract(
        id=incident.contract.id,
        left_key=proposal.left_key,
        right_key=proposal.right_key,
        cardinality=Cardinality.MANY_TO_ONE,
        default_join_type=proposal.default_join_type,
        fanout_policy=FanoutPolicy.NONE,
        status=ApprovalStatus.APPROVED,
        version=incident.contract.version + 1,
        evidence=decision.evidence,
        risks=decision.risks,
        approval_decision_id=decision.id,
    )
    return RegistryIncidentJoinUpsert(
        base=incident,
        contract=contract,
        decision=decision,
        profile_witness=witness,
    )


def _blocking_report(
    base,
    *,
    mapping: GovernedMappingRef | None = None,
) -> tuple[SemanticChangeReport, SemanticImpactSet]:
    selected = mapping or next(
        item
        for item in base.dependency_context.mappings
        if item.logical_field.root.startswith("Customer.")
    )
    finding_values = {
        "kind": SemanticChangeKind.PHYSICAL_TYPE_CHANGED,
        "severity": SemanticChangeSeverity.BLOCKING,
        "mapping": selected,
        "join": None,
        "previous_fingerprint": FP_3,
        "current_fingerprint": FP_4,
        "risks": ("physical_type_requires_registry_remediation",),
    }
    finding_fingerprint = semantic_change_fingerprint(finding_values)
    finding = SemanticChangeFinding(
        id=f"finding_{finding_fingerprint}",
        fingerprint=finding_fingerprint,
        **finding_values,
    )
    impact = SemanticChangeImpact.create(
        kind=SemanticImpactKind.MAPPING,
        artifact_id=selected.logical_field.root,
        artifact_version=selected.version,
        finding_ids=(finding.id,),
    )
    dependency = base.dependency_context.dependency_index
    impacts = SemanticImpactSet.create(
        impacts=(impact,),
        complete=True,
        watermark=dependency.watermark,
        dependency_index_fingerprint=dependency.fingerprint,
    )
    generations = CatalogGenerationVector.create(
        (
            CatalogGenerationObservation(
                connection_id=CatalogConnectionId("warehouse-a"),
                generation=7,
                inventory_fingerprint=FP_A,
            ),
        )
    )
    values = {
        "context": base.dependency_context,
        "observation_fingerprint": FP_3,
        "catalog_generations": generations,
        "baseline_revision": 1,
        "baseline_fingerprint": FP_1,
        "findings": (finding,),
        "impacts": SemanticImpactSummary.from_set(impacts),
        "status": SemanticChangeStatus.BLOCKED,
        "inspected_at": NOW - timedelta(minutes=1),
    }
    report_payload = {
        "context": values["context"],
        "observation_fingerprint": values["observation_fingerprint"],
        "catalog_generations": values["catalog_generations"],
        "baseline_revision": values["baseline_revision"],
        "baseline_fingerprint": values["baseline_fingerprint"],
        "findings": values["findings"],
        "impacts": values["impacts"],
        "status": values["status"],
        "inspected_at": values["inspected_at"],
    }
    fingerprint = semantic_change_fingerprint(report_payload)
    report = SemanticChangeReport(
        id=f"report_{fingerprint}",
        fingerprint=fingerprint,
        **values,
    )
    return report, impacts
