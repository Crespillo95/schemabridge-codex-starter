"""Adversarial tests for pure M26 semantic-change contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel, ValidationError

from schemabridge.domain.catalog_inventory import (
    CatalogAssetId,
    CatalogAssetLocator,
    CatalogConnectionId,
    CatalogFieldLocator,
)
from schemabridge.domain.concepts import LogicalFieldRef
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.joins import (
    Cardinality,
    DeclaredRelationship,
    FanoutPolicy,
    RelationshipProfile,
)
from schemabridge.domain.semantic_change import (
    AggregateJoinProfile,
    CatalogGenerationObservation,
    CatalogGenerationVector,
    GovernedJoinRef,
    GovernedMappingRef,
    GovernedResourceBinding,
    ObservedFieldEvidence,
    SemanticBindingSelection,
    SemanticBindingSelectionSet,
    SemanticChangeConfirmation,
    SemanticChangeDecision,
    SemanticChangeDecisionAction,
    SemanticChangeDecisionApproval,
    SemanticChangeFinding,
    SemanticChangeImpact,
    SemanticChangeInspectionContext,
    SemanticChangeKind,
    SemanticChangeReport,
    SemanticChangeSeverity,
    SemanticChangeStatus,
    SemanticContextGateAssessment,
    SemanticDependencyIndexState,
    SemanticEvidenceObservation,
    SemanticImpactKind,
    SemanticImpactSet,
    build_semantic_change_approval,
    build_semantic_change_decision,
    build_semantic_change_report,
    classify_semantic_change_findings,
    prepare_semantic_change_decision,
    semantic_change_fingerprint,
)
from schemabridge.domain.semantic_registry import PhysicalValueType, SemanticRegistryScope

NOW = datetime(2026, 7, 24, 9, 30, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SCOPE = SemanticRegistryScope(
    workspace_id="workspace-m26",
    catalog_scope="synthetic-demo",
    registry_id="enterprise_registry",
)


def _mapping(
    physical: str = "crm.customers.customer_id",
    *,
    physical_type: PhysicalValueType = PhysicalValueType.STRING,
) -> GovernedMappingRef:
    return GovernedMappingRef(
        logical_field=LogicalFieldRef("Customer.customer_id"),
        physical_field=PhysicalFieldRef(physical),
        version=1,
        approval_decision_id="mapping-decision-1",
        physical_type=physical_type,
    )


def _dependency_state(
    *,
    complete: bool = True,
    fingerprint: str = SHA_D,
    watermark: int = 7,
) -> SemanticDependencyIndexState:
    return SemanticDependencyIndexState(
        scope=SCOPE,
        watermark=watermark,
        fingerprint=fingerprint,
        complete=complete,
    )


def _context(
    mapping: GovernedMappingRef | None = None,
    *,
    dependency_state: SemanticDependencyIndexState | None = None,
) -> SemanticChangeInspectionContext:
    selected = mapping or _mapping()
    return SemanticChangeInspectionContext.create(
        scope=SCOPE,
        pointer_generation=3,
        pointer_fingerprint=SHA_A,
        pointer_transition_id="registry-transition-3",
        registry_version=4,
        registry_fingerprint=SHA_B,
        mappings=(selected,),
        joins=(),
        dependency_index=dependency_state or _dependency_state(),
    )


def _generation(
    *,
    connection_id: str = "catalog-main",
    generation: int = 9,
    fingerprint: str = SHA_C,
) -> CatalogGenerationVector:
    return CatalogGenerationVector.create(
        (
            CatalogGenerationObservation(
                connection_id=CatalogConnectionId(connection_id),
                generation=generation,
                inventory_fingerprint=fingerprint,
            ),
        )
    )


def _binding(
    mapping: GovernedMappingRef | None = None,
    *,
    connection_id: str = "catalog-main",
    workspace_id: str = SCOPE.workspace_id,
    generation: int = 9,
    definition_fingerprint: str = SHA_B,
    terms_fingerprint: str = SHA_C,
    asset_fingerprint: str = SHA_A,
    field_fingerprint: str = SHA_D,
) -> GovernedResourceBinding:
    selected = mapping or _mapping()
    return GovernedResourceBinding.create(
        mapping=selected,
        locator=CatalogFieldLocator(
            asset=CatalogAssetLocator(
                workspace_id=workspace_id,
                connection_id=CatalogConnectionId(connection_id),
                asset_id=CatalogAssetId("urn:li:dataset:customer"),
            ),
            field_path=("customer_id",),
        ),
        catalog_generation=generation,
        catalog_generation_fingerprint=SHA_C,
        asset_metadata_fingerprint=asset_fingerprint,
        field_metadata_fingerprint=field_fingerprint,
        field_definition_fingerprint=definition_fingerprint,
        field_terms_fingerprint=terms_fingerprint,
    )


def _field_evidence(
    mapping: GovernedMappingRef | None = None,
    *,
    binding: GovernedResourceBinding | None = None,
    normalized_type: PhysicalValueType = PhysicalValueType.STRING,
    nullable: bool = False,
    is_part_of_key: bool = True,
    definition_fingerprint: str = SHA_B,
    terms_fingerprint: str = SHA_C,
    asset_fingerprint: str = SHA_A,
    field_fingerprint: str = SHA_D,
) -> ObservedFieldEvidence:
    selected = mapping or _mapping()
    exact_binding = binding or _binding(
        selected,
        definition_fingerprint=definition_fingerprint,
        terms_fingerprint=terms_fingerprint,
        asset_fingerprint=asset_fingerprint,
        field_fingerprint=field_fingerprint,
    )
    return ObservedFieldEvidence.create(
        mapping=selected,
        binding=exact_binding,
        present=True,
        candidate_count=1,
        normalized_type=normalized_type,
        nullable=nullable,
        is_part_of_key=is_part_of_key,
        asset_metadata_fingerprint=asset_fingerprint,
        field_metadata_fingerprint=field_fingerprint,
        field_definition_fingerprint=definition_fingerprint,
        field_terms_fingerprint=terms_fingerprint,
        reason_code=None,
    )


def _observation(
    field: ObservedFieldEvidence | None = None,
    *,
    context: SemanticChangeInspectionContext | None = None,
    observed_at: datetime = NOW,
) -> SemanticEvidenceObservation:
    selected = field or _field_evidence()
    assert selected.binding is not None
    return SemanticEvidenceObservation.create(
        context=context or _context(selected.mapping),
        catalog_generations=_generation(generation=selected.binding.catalog_generation),
        fields=(selected,),
        joins=(),
        observed_at=observed_at,
        complete=True,
    )


def _impact_set(
    observation: SemanticEvidenceObservation,
    findings: tuple[SemanticChangeFinding, ...],
    *,
    complete: bool | None = None,
    dependency_fingerprint: str | None = None,
) -> SemanticImpactSet:
    impacts = (
        (
            SemanticChangeImpact.create(
                kind=SemanticImpactKind.MAPPING,
                artifact_id=observation.context.mappings[0].logical_field.root,
                artifact_version=observation.context.mappings[0].version,
                finding_ids=tuple(item.id for item in findings),
            ),
        )
        if findings
        else ()
    )
    state = observation.context.dependency_index
    return SemanticImpactSet.create(
        impacts=impacts,
        complete=state.complete if complete is None else complete,
        watermark=state.watermark,
        dependency_index_fingerprint=dependency_fingerprint or state.fingerprint,
    )


def _baseline_flow() -> tuple[
    SemanticEvidenceObservation,
    SemanticChangeReport,
    SemanticChangeDecisionApproval,
    SemanticChangeDecision,
]:
    observation = _observation()
    findings = classify_semantic_change_findings(observation, None)
    report = build_semantic_change_report(
        observation,
        None,
        _impact_set(observation, findings),
    )
    proposal = prepare_semantic_change_decision(
        report,
        observation,
        action=SemanticChangeDecisionAction.ESTABLISH_BASELINE,
        expected_head_revision=0,
    )
    approval = build_semantic_change_approval(
        proposal,
        actor="steward-one",
        approved_at=NOW + timedelta(minutes=1),
        confirmation=SemanticChangeConfirmation.ESTABLISH,
    )
    decision = build_semantic_change_decision(proposal, approval)
    return observation, report, approval, decision


def test_first_inspection_requires_explicit_baseline_and_exact_approval() -> None:
    observation, report, approval, decision = _baseline_flow()

    assert report.status is SemanticChangeStatus.REVIEW_REQUIRED
    assert {item.kind for item in report.findings} == {SemanticChangeKind.BASELINE_REQUIRED}
    assert all(item.severity is SemanticChangeSeverity.REVIEW_REQUIRED for item in report.findings)
    assert decision.baseline is not None
    assert decision.baseline.revision == 1
    assert decision.baseline.approval_id == approval.id
    assert decision.baseline.fields == observation.fields


def test_equivalent_later_observation_is_current_with_zero_findings() -> None:
    observation, _, _, decision = _baseline_flow()
    assert decision.baseline is not None
    later = _observation(
        observation.fields[0],
        context=observation.context,
        observed_at=NOW + timedelta(hours=1),
    )
    findings = classify_semantic_change_findings(later, decision.baseline)
    report = build_semantic_change_report(
        later,
        decision.baseline,
        _impact_set(later, findings),
    )

    assert findings == ()
    assert report.findings == ()
    assert report.status is SemanticChangeStatus.CURRENT


@pytest.mark.parametrize(
    ("normalized_type", "nullable", "is_part_of_key", "expected_kind"),
    [
        (
            PhysicalValueType.INTEGER,
            False,
            True,
            SemanticChangeKind.PHYSICAL_TYPE_CHANGED,
        ),
        (
            PhysicalValueType.STRING,
            True,
            True,
            SemanticChangeKind.NULLABILITY_CHANGED,
        ),
        (
            PhysicalValueType.STRING,
            False,
            False,
            SemanticChangeKind.KEY_STATUS_CHANGED,
        ),
    ],
)
def test_blocking_field_drift_cannot_be_revalidated_in_same_registry(
    normalized_type: PhysicalValueType,
    nullable: bool,
    is_part_of_key: bool,
    expected_kind: SemanticChangeKind,
) -> None:
    _, _, _, decision = _baseline_flow()
    assert decision.baseline is not None
    changed_field = _field_evidence(
        normalized_type=normalized_type,
        nullable=nullable,
        is_part_of_key=is_part_of_key,
    )
    changed = _observation(changed_field)
    findings = classify_semantic_change_findings(changed, decision.baseline)
    report = build_semantic_change_report(
        changed,
        decision.baseline,
        _impact_set(changed, findings),
    )

    assert report.status is SemanticChangeStatus.BLOCKED
    assert expected_kind in {item.kind for item in report.findings}
    with pytest.raises(ValueError, match="blocking semantic change"):
        prepare_semantic_change_decision(
            report,
            changed,
            action=SemanticChangeDecisionAction.REVALIDATE_COMPATIBLE_CHANGE,
            expected_head_revision=1,
        )

    rejected = prepare_semantic_change_decision(
        report,
        changed,
        action=SemanticChangeDecisionAction.REJECT_CHANGE,
        expected_head_revision=1,
    )
    assert rejected.action is SemanticChangeDecisionAction.REJECT_CHANGE


def test_definition_change_can_be_revalidated_with_exact_existing_head() -> None:
    _, _, _, first_decision = _baseline_flow()
    assert first_decision.baseline is not None
    changed_field = _field_evidence(
        definition_fingerprint=SHA_E,
        binding=_binding(definition_fingerprint=SHA_E),
    )
    changed = _observation(changed_field)
    findings = classify_semantic_change_findings(changed, first_decision.baseline)
    report = build_semantic_change_report(
        changed,
        first_decision.baseline,
        _impact_set(changed, findings),
    )
    assert {item.kind for item in report.findings} == {SemanticChangeKind.FIELD_DEFINITION_CHANGED}
    proposal = prepare_semantic_change_decision(
        report,
        changed,
        action=SemanticChangeDecisionAction.REVALIDATE_COMPATIBLE_CHANGE,
        expected_head_revision=1,
    )
    approval = build_semantic_change_approval(
        proposal,
        actor="steward-two",
        approved_at=NOW + timedelta(minutes=2),
        confirmation=SemanticChangeConfirmation.REVALIDATE,
    )
    decision = build_semantic_change_decision(proposal, approval)

    assert report.status is SemanticChangeStatus.REVIEW_REQUIRED
    assert decision.baseline is not None
    assert decision.baseline.revision == 2
    assert decision.baseline.approval_id == approval.id


def test_same_physical_name_in_another_connection_is_not_the_same_binding() -> None:
    mapping = _mapping()
    first = _binding(mapping, connection_id="catalog-main")
    homonym = _binding(mapping, connection_id="catalog-secondary")

    assert first.locator.field_path == homonym.locator.field_path
    assert first.locator.asset.connection_id != homonym.locator.asset.connection_id
    assert first.fingerprint != homonym.fingerprint


def test_ambiguous_binding_requires_an_exact_explicit_selection() -> None:
    mapping = _mapping()
    selected_binding = _binding(mapping, connection_id="catalog-secondary")
    selection = SemanticBindingSelection(
        mapping_approval_decision_id=mapping.approval_decision_id,
        mapping_version=mapping.version,
        physical_field=mapping.physical_field,
        locator=selected_binding.locator,
    )
    selected = ObservedFieldEvidence.create(
        mapping=mapping,
        binding=selected_binding,
        explicit_selection=selection,
        present=True,
        candidate_count=2,
        normalized_type=PhysicalValueType.STRING,
        nullable=False,
        is_part_of_key=True,
        asset_metadata_fingerprint=SHA_A,
        field_metadata_fingerprint=SHA_D,
        field_definition_fingerprint=SHA_B,
        field_terms_fingerprint=SHA_C,
        reason_code=None,
    )

    assert selected.candidate_count == 2
    assert selected.binding is not None
    assert selected.binding.locator.asset.connection_id == CatalogConnectionId("catalog-secondary")
    with pytest.raises(ValidationError, match="requires one explicit selection"):
        ObservedFieldEvidence.create(
            **selected.model_dump(
                mode="python",
                exclude={"fingerprint", "explicit_selection"},
            ),
        )


def test_binding_selection_set_is_canonical_bounded_and_scope_exact() -> None:
    mapping = _mapping()
    selection = SemanticBindingSelection(
        mapping_approval_decision_id=mapping.approval_decision_id,
        mapping_version=mapping.version,
        physical_field=mapping.physical_field,
        locator=_binding(mapping).locator,
    )
    selections = SemanticBindingSelectionSet.create(
        scope=SCOPE,
        selections=(selection,),
    )

    assert selections.selections == (selection,)
    assert len(selections.fingerprint) == 64
    foreign = selection.model_copy(
        update={
            "locator": selection.locator.model_copy(
                update={
                    "asset": selection.locator.asset.model_copy(
                        update={"workspace_id": "workspace-foreign"}
                    )
                }
            )
        }
    )
    with pytest.raises(ValidationError, match="crosses workspaces"):
        SemanticBindingSelectionSet.create(scope=SCOPE, selections=(foreign,))


def test_corrected_mapping_context_can_establish_a_new_baseline_after_revision_one() -> None:
    old_observation, _, _, old_decision = _baseline_flow()
    assert old_decision.baseline is not None
    corrected_mapping = _mapping("corrected.customers.customer_id")
    corrected_field = _field_evidence(
        corrected_mapping,
        binding=_binding(corrected_mapping),
    )
    corrected_observation = _observation(
        corrected_field,
        context=_context(corrected_mapping),
        observed_at=NOW + timedelta(hours=1),
    )
    findings = classify_semantic_change_findings(corrected_observation, None)
    report = build_semantic_change_report(
        corrected_observation,
        None,
        _impact_set(corrected_observation, findings),
    )

    proposal = prepare_semantic_change_decision(
        report,
        corrected_observation,
        action=SemanticChangeDecisionAction.ESTABLISH_BASELINE,
        expected_head_revision=1,
        previous_head_context_fingerprint=old_observation.context.fingerprint,
    )
    approval = build_semantic_change_approval(
        proposal,
        actor="steward-recovery",
        approved_at=NOW + timedelta(hours=1, minutes=1),
        confirmation=SemanticChangeConfirmation.ESTABLISH,
    )
    recovered = build_semantic_change_decision(proposal, approval)

    assert report.status is SemanticChangeStatus.REVIEW_REQUIRED
    assert recovered.baseline is not None
    assert recovered.baseline.revision == 2
    assert recovered.baseline.context_fingerprint == corrected_observation.context.fingerprint


def test_same_registry_cannot_use_establish_baseline_as_a_revision_shortcut() -> None:
    observation = _observation()
    findings = classify_semantic_change_findings(observation, None)
    report = build_semantic_change_report(
        observation,
        None,
        _impact_set(observation, findings),
    )

    with pytest.raises(ValueError, match="different previous registry context"):
        prepare_semantic_change_decision(
            report,
            observation,
            action=SemanticChangeDecisionAction.ESTABLISH_BASELINE,
            expected_head_revision=2,
            previous_head_context_fingerprint=observation.context.fingerprint,
        )
    with pytest.raises(ValueError, match="different previous registry context"):
        prepare_semantic_change_decision(
            report,
            observation,
            action=SemanticChangeDecisionAction.ESTABLISH_BASELINE,
            expected_head_revision=2,
        )


def test_observation_rejects_explicit_binding_from_another_workspace() -> None:
    mapping = _mapping()
    foreign = _binding(mapping, workspace_id="workspace-foreign")
    evidence = _field_evidence(mapping, binding=foreign)

    with pytest.raises(ValidationError, match="crosses workspaces"):
        _observation(evidence)


def test_dependency_snapshot_mismatch_cannot_be_hidden_in_report() -> None:
    observation = _observation()
    findings = classify_semantic_change_findings(observation, None)
    mismatched = _impact_set(
        observation,
        findings,
        dependency_fingerprint=SHA_E,
    )

    with pytest.raises(ValidationError, match="dependency-index snapshot"):
        build_semantic_change_report(observation, None, mismatched)


def test_incomplete_dependency_index_is_visible_and_blocks_approval() -> None:
    dependency = _dependency_state(complete=False)
    context = _context(dependency_state=dependency)
    observation = _observation(context=context)
    findings = classify_semantic_change_findings(observation, None)
    impacts = _impact_set(observation, findings)
    report = build_semantic_change_report(
        observation,
        None,
        impacts,
    )

    assert report.status is SemanticChangeStatus.BLOCKED
    assert SemanticChangeKind.EVIDENCE_UNAVAILABLE in {finding.kind for finding in report.findings}
    assert impacts.impacts[0].finding_ids == tuple(item.id for item in report.findings)
    assert report.impacts.impact_set_fingerprint == impacts.fingerprint
    with pytest.raises(ValueError, match="blocking semantic change"):
        prepare_semantic_change_decision(
            report,
            observation,
            action=SemanticChangeDecisionAction.ESTABLISH_BASELINE,
            expected_head_revision=0,
        )


def test_aggregate_join_profile_retains_counts_only_and_classifies_cardinality() -> None:
    left = _mapping("crm.customers.customer_id")
    right = GovernedMappingRef(
        logical_field=LogicalFieldRef("AccountHolder.customer_id"),
        physical_field=PhysicalFieldRef("bank.account_holders.customer_id"),
        version=1,
        approval_decision_id="mapping-decision-2",
        physical_type=PhysicalValueType.STRING,
    )
    join = GovernedJoinRef(
        contract_id="customer_to_holder",
        version=1,
        approval_decision_id="join-decision-1",
        left_field=left.physical_field,
        right_field=right.physical_field,
        cardinality=Cardinality.ONE_TO_MANY,
        fanout_policy=FanoutPolicy.REQUIRE_DISTINCT_FOR_LEFT_ENTITY_METRICS,
    )
    profile = RelationshipProfile(
        left_row_count=10,
        right_row_count=15,
        left_null_count=0,
        right_null_count=0,
        left_invalid_count=0,
        right_invalid_count=0,
        left_distinct_valid=10,
        right_distinct_valid=10,
        matching_distinct_keys=10,
        left_max_multiplicity=1,
        right_max_multiplicity=2,
        declared_relationship=DeclaredRelationship.RIGHT_FOREIGN_KEY_TO_LEFT,
        reader_user="schemabridge_reader",
        transaction_read_only=True,
        statement_timeout_ms=5_000,
    )
    aggregate = AggregateJoinProfile.create(join=join, profile=profile)

    assert aggregate.observed_cardinality is Cardinality.ONE_TO_MANY
    serialized = aggregate.model_dump_json()
    assert "source_rows" not in serialized
    assert "sample_values" not in serialized


def test_join_overlap_crossing_approved_policy_is_blocking() -> None:
    left = _mapping("crm.customers.customer_id")
    right = GovernedMappingRef(
        logical_field=LogicalFieldRef("AccountHolder.customer_id"),
        physical_field=PhysicalFieldRef("bank.account_holders.customer_id"),
        version=1,
        approval_decision_id="mapping-decision-2",
        physical_type=PhysicalValueType.STRING,
    )
    join = GovernedJoinRef(
        contract_id="customer_to_holder",
        version=1,
        approval_decision_id="join-decision-1",
        left_field=left.physical_field,
        right_field=right.physical_field,
        cardinality=Cardinality.ONE_TO_MANY,
        fanout_policy=FanoutPolicy.REQUIRE_DISTINCT_FOR_LEFT_ENTITY_METRICS,
    )
    context = SemanticChangeInspectionContext.create(
        scope=SCOPE,
        pointer_generation=3,
        pointer_fingerprint=SHA_A,
        pointer_transition_id="registry-transition-3",
        registry_version=4,
        registry_fingerprint=SHA_B,
        mappings=(left, right),
        joins=(join,),
        dependency_index=_dependency_state(),
    )
    fields = (_field_evidence(left), _field_evidence(right, binding=_binding(right)))

    def observation(matching_keys: int, observed_at: datetime) -> SemanticEvidenceObservation:
        profile = RelationshipProfile(
            left_row_count=10,
            right_row_count=15,
            left_null_count=0,
            right_null_count=0,
            left_invalid_count=0,
            right_invalid_count=0,
            left_distinct_valid=10,
            right_distinct_valid=10,
            matching_distinct_keys=matching_keys,
            left_max_multiplicity=1,
            right_max_multiplicity=2,
            declared_relationship=DeclaredRelationship.RIGHT_FOREIGN_KEY_TO_LEFT,
            reader_user="schemabridge_reader",
            transaction_read_only=True,
            statement_timeout_ms=5_000,
        )
        return SemanticEvidenceObservation.create(
            context=context,
            catalog_generations=_generation(),
            fields=fields,
            joins=(AggregateJoinProfile.create(join=join, profile=profile),),
            observed_at=observed_at,
            complete=True,
        )

    approved = observation(8, NOW)
    initial_findings = classify_semantic_change_findings(approved, None)
    initial_report = build_semantic_change_report(
        approved,
        None,
        _impact_set(approved, initial_findings),
    )
    proposal = prepare_semantic_change_decision(
        initial_report,
        approved,
        action=SemanticChangeDecisionAction.ESTABLISH_BASELINE,
        expected_head_revision=0,
    )
    approval = build_semantic_change_approval(
        proposal,
        actor="steward-one",
        approved_at=NOW + timedelta(minutes=1),
        confirmation=SemanticChangeConfirmation.ESTABLISH,
    )
    baseline = build_semantic_change_decision(proposal, approval).baseline
    assert baseline is not None

    degraded = observation(4, NOW + timedelta(hours=1))
    findings = classify_semantic_change_findings(degraded, baseline)

    assert {finding.kind for finding in findings} == {SemanticChangeKind.JOIN_OVERLAP_CHANGED}
    assert all(finding.severity is SemanticChangeSeverity.BLOCKING for finding in findings)


def test_fingerprints_accept_arbitrary_pydantic_models_without_raw_serialization_errors() -> None:
    class Example(BaseModel):
        connection: CatalogConnectionId

    first = semantic_change_fingerprint(Example(connection=CatalogConnectionId("catalog-main")))
    second = semantic_change_fingerprint(Example(connection=CatalogConnectionId("catalog-main")))

    assert first == second
    assert len(first) == 64


def test_tampered_fingerprint_and_naive_approval_time_are_rejected() -> None:
    binding = _binding()
    tampered = binding.model_dump(mode="python")
    tampered["field_definition_fingerprint"] = SHA_E
    with pytest.raises(ValidationError, match="identity does not match"):
        GovernedResourceBinding.model_validate(tampered)

    observation = _observation()
    findings = classify_semantic_change_findings(observation, None)
    report = build_semantic_change_report(
        observation,
        None,
        _impact_set(observation, findings),
    )
    proposal = prepare_semantic_change_decision(
        report,
        observation,
        action=SemanticChangeDecisionAction.ESTABLISH_BASELINE,
        expected_head_revision=0,
    )
    with pytest.raises(ValueError, match="timezone"):
        build_semantic_change_approval(
            proposal,
            actor="steward-one",
            approved_at=datetime(2026, 7, 24, 9, 31),
            confirmation=SemanticChangeConfirmation.ESTABLISH,
        )


def test_gate_requires_a_baseline_for_eligible_context_and_reasons_when_blocked() -> None:
    dependencies = semantic_change_fingerprint({"plan": "exact"})
    current = SemanticContextGateAssessment(
        dependencies_fingerprint=dependencies,
        eligible=True,
        status=SemanticChangeStatus.CURRENT,
        connection_id=CatalogConnectionId("warehouse-primary"),
        baseline_revision=1,
    )
    assert current.eligible is True
    assert current.connection_id == CatalogConnectionId("warehouse-primary")

    with pytest.raises(ValidationError, match="current baseline and connection"):
        SemanticContextGateAssessment(
            dependencies_fingerprint=dependencies,
            eligible=True,
            status=SemanticChangeStatus.CURRENT,
            baseline_revision=1,
        )

    with pytest.raises(ValidationError, match="cannot expose a connection"):
        SemanticContextGateAssessment(
            dependencies_fingerprint=dependencies,
            eligible=False,
            status=SemanticChangeStatus.BLOCKED,
            connection_id=CatalogConnectionId("warehouse-primary"),
            reason_codes=(SemanticChangeKind.PHYSICAL_TYPE_CHANGED,),
            baseline_revision=1,
        )

    with pytest.raises(ValidationError, match="safe reasons"):
        SemanticContextGateAssessment(
            dependencies_fingerprint=dependencies,
            eligible=False,
            status=SemanticChangeStatus.BLOCKED,
        )
