"""In-memory application tests for M26 inspection, decisions, and pre-I/O gating."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from schemabridge.adapters.semantic_registry.recorded import (
    RecordedGovernedSemanticRegistry,
)
from schemabridge.application.semantic_change import (
    AssertSemanticContextCurrent,
    CommitSemanticChangeDecision,
    InspectSemanticChange,
    PrepareSemanticChangeDecision,
    PrepareSemanticChangeDecisionApproval,
    SemanticChangeError,
    SemanticChangeErrorCode,
)
from schemabridge.application.semantic_change_operator import InspectLatestSemanticChange
from schemabridge.domain.catalog_inventory import (
    CatalogAssetId,
    CatalogAssetLocator,
    CatalogConnectionId,
    CatalogFieldLocator,
)
from schemabridge.domain.joins import (
    Cardinality,
    DeclaredRelationship,
    RelationshipProfile,
)
from schemabridge.domain.registry_control import (
    ActiveRegistryPointer,
    GovernedRegistryVersion,
    RegistryVersionTrust,
)
from schemabridge.domain.semantic_change import (
    AggregateJoinProfile,
    CatalogGenerationObservation,
    CatalogGenerationVector,
    GovernedMappingRef,
    GovernedResourceBinding,
    ObservedFieldEvidence,
    SemanticBindingSelection,
    SemanticBindingSelectionSet,
    SemanticChangeCommit,
    SemanticChangeConfirmation,
    SemanticChangeDecision,
    SemanticChangeDecisionAction,
    SemanticChangeDecisionApproval,
    SemanticChangeDecisionProposal,
    SemanticChangeFinding,
    SemanticChangeImpact,
    SemanticChangeInspectionContext,
    SemanticChangeKind,
    SemanticChangeReport,
    SemanticChangeStatus,
    SemanticContextGateAssessment,
    SemanticDependencyIndexState,
    SemanticEvidenceBaseline,
    SemanticEvidenceObservation,
    SemanticImpactKind,
    SemanticImpactSet,
    SemanticPlanDependencies,
    semantic_change_fingerprint,
)
from schemabridge.domain.semantic_change_scans import (
    SemanticChangeScanRequest,
    SemanticChangeScanSourceKind,
)
from schemabridge.domain.semantic_registry import (
    GovernedSemanticRegistrySnapshot,
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
    datahub_registry_document_urn,
    prepare_datahub_registry_version,
    semantic_registry_decision_ids,
)

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "demo/ground_truth/registries/manifest.yml"
NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
SCOPE = SemanticRegistryScope(
    workspace_id="workspace-semantic-unit",
    catalog_scope="synthetic-demo",
    registry_id="synthetic_enterprise",
)
CONNECTION = CatalogConnectionId("warehouse-primary")
DEPENDENCY_FINGERPRINT = semantic_change_fingerprint({"dependencies": "complete-v1"})


@dataclass
class _PointerReader:
    pointer: ActiveRegistryPointer | None
    calls: int = 0

    def load_active(self, scope: SemanticRegistryScope) -> ActiveRegistryPointer | None:
        self.calls += 1
        assert scope == SCOPE
        return self.pointer


@dataclass
class _VersionReader:
    version: GovernedRegistryVersion
    calls: int = 0

    def load_version(
        self,
        scope: SemanticRegistryScope,
        version: int,
    ) -> GovernedRegistryVersion:
        self.calls += 1
        assert scope == SCOPE
        assert version == self.version.snapshot.registry.version
        return self.version


@dataclass
class _Evidence:
    definition_change: bool = False
    type_change: bool = False
    calls: list[tuple[int, int | None]] = field(default_factory=list)

    def observe(
        self,
        context: SemanticChangeInspectionContext,
        registry: GovernedSemanticRegistrySnapshot,
        baseline: SemanticEvidenceBaseline | None,
        *,
        binding_selections: SemanticBindingSelectionSet | None = None,
    ) -> SemanticEvidenceObservation:
        assert registry.fingerprint == context.registry_fingerprint
        self.calls.append((len(context.mappings), None if baseline is None else baseline.revision))
        selection_by_mapping = {
            item.mapping_identity: item
            for item in (() if binding_selections is None else binding_selections.selections)
        }
        vector = CatalogGenerationVector.create(
            (
                CatalogGenerationObservation(
                    connection_id=CONNECTION,
                    generation=7,
                    inventory_fingerprint="a" * 64,
                ),
            )
        )
        fields = tuple(
            self._field(
                mapping,
                index=index,
                vector=vector,
                explicit_selection=selection_by_mapping.get(
                    (
                        mapping.approval_decision_id,
                        mapping.version,
                        mapping.physical_field.root,
                    )
                ),
            )
            for index, mapping in enumerate(context.mappings)
        )
        joins = tuple(
            AggregateJoinProfile.create(
                join=join,
                profile=_profile(join.cardinality),
            )
            for join in context.joins
        )
        return SemanticEvidenceObservation.create(
            context=context,
            catalog_generations=vector,
            fields=fields,
            joins=joins,
            observed_at=NOW,
            complete=True,
        )

    def _field(
        self,
        mapping: GovernedMappingRef,
        *,
        index: int,
        vector: CatalogGenerationVector,
        explicit_selection: SemanticBindingSelection | None,
    ) -> ObservedFieldEvidence:
        dataset, column = mapping.physical_field.root.rsplit(".", 1)
        changed = index == 0
        normalized_type = mapping.physical_type
        if changed and self.type_change:
            from schemabridge.domain.semantic_registry import PhysicalValueType

            normalized_type = (
                PhysicalValueType.INTEGER
                if mapping.physical_type is not PhysicalValueType.INTEGER
                else PhysicalValueType.STRING
            )
        definition = semantic_change_fingerprint(
            {
                "field": mapping.physical_field.root,
                "definition": "changed" if changed and self.definition_change else "stable",
            }
        )
        metadata = semantic_change_fingerprint(
            {
                "field": mapping.physical_field.root,
                "type": normalized_type.value,
            }
        )
        asset = semantic_change_fingerprint({"asset": dataset})
        terms = semantic_change_fingerprint({"field": mapping.physical_field.root, "terms": []})
        locator = CatalogFieldLocator(
            asset=CatalogAssetLocator(
                workspace_id=SCOPE.workspace_id,
                connection_id=CONNECTION,
                asset_id=CatalogAssetId(dataset),
            ),
            field_path=(column,),
        )
        if explicit_selection is not None:
            locator = explicit_selection.locator
        binding = GovernedResourceBinding.create(
            mapping=mapping,
            locator=locator,
            catalog_generation=7,
            catalog_generation_fingerprint=vector.fingerprint,
            asset_metadata_fingerprint=asset,
            field_metadata_fingerprint=metadata,
            field_definition_fingerprint=definition,
            field_terms_fingerprint=terms,
        )
        return ObservedFieldEvidence.create(
            mapping=mapping,
            binding=binding,
            explicit_selection=explicit_selection,
            present=True,
            candidate_count=2 if explicit_selection is not None else 1,
            normalized_type=normalized_type,
            nullable=False,
            is_part_of_key=True,
            asset_metadata_fingerprint=asset,
            field_metadata_fingerprint=metadata,
            field_definition_fingerprint=definition,
            field_terms_fingerprint=terms,
            reason_code=None,
        )


@dataclass
class _Dependencies:
    complete: bool = True
    watermark: int = 11
    state_scope: SemanticRegistryScope = SCOPE
    resolved: list[tuple[str, ...]] = field(default_factory=list)

    def load_state(self, scope: SemanticRegistryScope) -> SemanticDependencyIndexState:
        assert scope == SCOPE
        return SemanticDependencyIndexState(
            scope=self.state_scope,
            watermark=self.watermark,
            fingerprint=DEPENDENCY_FINGERPRINT,
            complete=self.complete,
        )

    def resolve_impacts(
        self,
        context: SemanticChangeInspectionContext,
        findings: tuple[SemanticChangeFinding, ...],
    ) -> SemanticImpactSet:
        assert context.dependency_index.fingerprint == DEPENDENCY_FINGERPRINT
        self.resolved.append(tuple(item.id for item in findings))
        grouped: dict[tuple[SemanticImpactKind, str, int], list[str]] = {}
        for finding in findings:
            if finding.mapping is not None:
                identity = (
                    SemanticImpactKind.MAPPING,
                    finding.mapping.logical_field.root,
                    finding.mapping.version,
                )
            else:
                assert finding.join is not None
                identity = (
                    SemanticImpactKind.JOIN,
                    finding.join.contract_id,
                    finding.join.version,
                )
            grouped.setdefault(identity, []).append(finding.id)
        impacts = tuple(
            SemanticChangeImpact.create(
                kind=kind,
                artifact_id=artifact_id,
                artifact_version=artifact_version,
                finding_ids=tuple(finding_ids),
            )
            for (kind, artifact_id, artifact_version), finding_ids in grouped.items()
        )
        return SemanticImpactSet.create(
            impacts=impacts,
            complete=self.complete,
            watermark=self.watermark,
            dependency_index_fingerprint=DEPENDENCY_FINGERPRINT,
        )


@dataclass
class _Store:
    reports: dict[str, SemanticChangeReport] = field(default_factory=dict)
    observations: dict[str, SemanticEvidenceObservation] = field(default_factory=dict)
    baseline: SemanticEvidenceBaseline | None = None
    head: SemanticChangeCommit | None = None
    report_writes: int = 0
    commit_writes: int = 0

    def load_baseline(self, scope: SemanticRegistryScope) -> SemanticEvidenceBaseline | None:
        assert scope == SCOPE
        return self.baseline

    def load_report(
        self,
        scope: SemanticRegistryScope,
        report_id: str,
    ) -> SemanticChangeReport | None:
        assert scope == SCOPE
        return self.reports.get(report_id)

    def load_observation(
        self,
        scope: SemanticRegistryScope,
        report_id: str,
    ) -> SemanticEvidenceObservation | None:
        assert scope == SCOPE
        return self.observations.get(report_id)

    def record_report(
        self,
        report: SemanticChangeReport,
        observation: SemanticEvidenceObservation,
    ) -> SemanticChangeReport:
        prior = self.reports.get(report.id)
        if prior is not None:
            assert prior == report
            assert self.observations[report.id] == observation
            return prior
        self.report_writes += 1
        self.reports[report.id] = report
        self.observations[report.id] = observation
        return report

    def load_head(self, scope: SemanticRegistryScope) -> SemanticChangeCommit | None:
        assert scope == SCOPE
        return self.head

    def commit_decision(
        self,
        proposal: SemanticChangeDecisionProposal,
        approval: SemanticChangeDecisionApproval,
        decision: SemanticChangeDecision,
        baseline: SemanticEvidenceBaseline | None,
    ) -> SemanticChangeCommit:
        del approval
        if self.head is not None and self.head.decision == decision:
            return self.head.model_copy(update={"replayed": True})
        current_revision = 0 if self.head is None else self.head.head_revision
        if current_revision != proposal.expected_head_revision:
            raise AssertionError("semantic evidence CAS conflict")
        self.commit_writes += 1
        commit = SemanticChangeCommit(
            scope=SCOPE,
            head_revision=current_revision + 1,
            decision=decision,
            baseline=baseline,
            audit_event_hash=semantic_change_fingerprint(
                {"decision": decision.fingerprint, "revision": current_revision + 1}
            ),
        )
        self.head = commit
        if baseline is not None:
            self.baseline = baseline
        return commit


@dataclass
class _LatestScan:
    scan: SemanticChangeScanRequest | None

    def load_latest_for_scope(
        self,
        scope: SemanticRegistryScope,
    ) -> SemanticChangeScanRequest | None:
        assert scope == SCOPE
        return self.scan


@dataclass
class _Gate:
    eligible: bool
    calls: list[SemanticPlanDependencies] = field(default_factory=list)

    def assess(self, dependencies: SemanticPlanDependencies) -> SemanticContextGateAssessment:
        self.calls.append(dependencies)
        return SemanticContextGateAssessment(
            dependencies_fingerprint=dependencies.fingerprint,
            eligible=self.eligible,
            status=(
                SemanticChangeStatus.CURRENT if self.eligible else SemanticChangeStatus.BLOCKED
            ),
            connection_id=(CatalogConnectionId("warehouse-primary") if self.eligible else None),
            reason_codes=(() if self.eligible else (SemanticChangeKind.PHYSICAL_TYPE_CHANGED,)),
            baseline_revision=1,
        )


@pytest.fixture(scope="module")
def governed_version() -> GovernedRegistryVersion:
    recorded = RecordedGovernedSemanticRegistry(MANIFEST, SCOPE).load().registry
    live = prepare_datahub_registry_version(recorded, SCOPE)
    return GovernedRegistryVersion(
        snapshot=ScopedSemanticRegistrySnapshot(scope=SCOPE, registry=live),
        publication_approval_id="semantic-change-unit-publication",
        trust=RegistryVersionTrust.STRICT,
    )


def _pointer(version: GovernedRegistryVersion) -> ActiveRegistryPointer:
    registry = version.snapshot.registry
    return ActiveRegistryPointer(
        scope=SCOPE,
        generation=3,
        registry_version=registry.version,
        registry_fingerprint=registry.fingerprint,
        registry_target=datahub_registry_document_urn(SCOPE, registry.version),
        transition_id="transition-semantic-unit",
        activated_by="publisher-unit",
        activated_at=NOW,
        decision_ids=semantic_registry_decision_ids(registry),
    )


def _registry_scan(*, generation: int = 3) -> SemanticChangeScanRequest:
    fingerprint = semantic_change_fingerprint({"scope": SCOPE, "registry_generation": generation})
    return SemanticChangeScanRequest.requested(
        workspace_id=SCOPE.workspace_id,
        source_kind=SemanticChangeScanSourceKind.REGISTRY_POINTER,
        source_event_key=f"transition-semantic-unit-{generation}",
        source_fingerprint=fingerprint,
        catalog_scope=SCOPE.catalog_scope,
        registry_id=SCOPE.registry_id,
        registry_generation=generation,
        requested_at=NOW,
    )


def _services(
    version: GovernedRegistryVersion,
    *,
    evidence: _Evidence | None = None,
    dependencies: _Dependencies | None = None,
    store: _Store | None = None,
) -> tuple[InspectSemanticChange, _Evidence, _Dependencies, _Store]:
    evidence = evidence or _Evidence()
    dependencies = dependencies or _Dependencies()
    store = store or _Store()
    inspector = InspectSemanticChange(
        pointers=_PointerReader(_pointer(version)),
        versions=_VersionReader(version),
        evidence=evidence,
        dependency_index=dependencies,
        store=store,
        scope=SCOPE,
    )
    return inspector, evidence, dependencies, store


def _establish_baseline(
    inspector: InspectSemanticChange,
    store: _Store,
) -> SemanticChangeCommit:
    report = inspector.execute()
    proposal = PrepareSemanticChangeDecision(store).execute(
        SCOPE,
        report.id,
        action=SemanticChangeDecisionAction.ESTABLISH_BASELINE,
    )
    approval = PrepareSemanticChangeDecisionApproval().execute(
        proposal,
        actor="steward-unit",
        approved_at=NOW,
        confirmation=SemanticChangeConfirmation.ESTABLISH,
    )
    return CommitSemanticChangeDecision(inspector, store).execute(proposal, approval)


def _profile(cardinality: Cardinality) -> RelationshipProfile:
    declared = {
        Cardinality.ONE_TO_ONE: DeclaredRelationship.NONE,
        Cardinality.ONE_TO_MANY: DeclaredRelationship.RIGHT_FOREIGN_KEY_TO_LEFT,
        Cardinality.MANY_TO_ONE: DeclaredRelationship.LEFT_FOREIGN_KEY_TO_RIGHT,
    }[cardinality]
    return RelationshipProfile(
        left_row_count=10,
        right_row_count=10,
        left_null_count=0,
        right_null_count=0,
        left_invalid_count=0,
        right_invalid_count=0,
        left_distinct_valid=10,
        right_distinct_valid=10,
        matching_distinct_keys=10,
        left_max_multiplicity=1,
        right_max_multiplicity=1,
        declared_relationship=declared,
    )


def test_first_inspection_requires_explicit_baseline_and_records_only_governed_resources(
    governed_version: GovernedRegistryVersion,
) -> None:
    inspector, evidence, dependencies, store = _services(governed_version)

    report = inspector.execute()
    replay = inspector.execute()

    assert report.status is SemanticChangeStatus.REVIEW_REQUIRED
    assert {item.kind for item in report.findings} == {SemanticChangeKind.BASELINE_REQUIRED}
    assert evidence.calls == [(31, None), (31, None)]
    assert dependencies.resolved[0] == tuple(item.id for item in report.findings)
    assert store.report_writes == 1
    assert replay == report
    assert len(report.findings) == len(report.context.mappings) == 31


def test_operator_inspection_is_bound_to_latest_exact_registry_scan(
    governed_version: GovernedRegistryVersion,
) -> None:
    inspector, _evidence, _dependencies, store = _services(governed_version)
    latest = InspectLatestSemanticChange(
        scans=_LatestScan(_registry_scan()),
        inspector_factory=lambda scan: inspector,
        store=store,
        scope=SCOPE,
    )

    report = latest.execute()

    assert report.context.pointer_generation == 3
    assert store.report_writes == 1


def test_operator_scan_generation_mismatch_writes_no_report(
    governed_version: GovernedRegistryVersion,
) -> None:
    inspector, _evidence, _dependencies, store = _services(governed_version)
    latest = InspectLatestSemanticChange(
        scans=_LatestScan(_registry_scan(generation=4)),
        inspector_factory=lambda scan: inspector,
        store=store,
        scope=SCOPE,
    )

    with pytest.raises(SemanticChangeError) as raised:
        latest.execute()

    assert raised.value.code is SemanticChangeErrorCode.CAS_CONFLICT
    assert store.report_writes == 0


def test_inspection_passes_only_exact_scoped_binding_selections(
    governed_version: GovernedRegistryVersion,
) -> None:
    inspector, evidence, _dependencies, store = _services(governed_version)
    preview_report, _ = inspector.capture_current()
    mapping = preview_report.context.mappings[0]
    dataset, column = mapping.physical_field.root.rsplit(".", 1)
    selection = SemanticBindingSelection(
        mapping_approval_decision_id=mapping.approval_decision_id,
        mapping_version=mapping.version,
        physical_field=mapping.physical_field,
        locator=CatalogFieldLocator(
            asset=CatalogAssetLocator(
                workspace_id=SCOPE.workspace_id,
                connection_id=CONNECTION,
                asset_id=CatalogAssetId(dataset),
            ),
            field_path=(column,),
        ),
    )
    selections = SemanticBindingSelectionSet.create(
        scope=SCOPE,
        selections=(selection,),
    )

    report = inspector.execute(binding_selections=selections)
    observation = store.observations[report.id]
    selected_evidence = next(item for item in observation.fields if item.mapping == mapping)

    assert selected_evidence.explicit_selection == selection
    assert selected_evidence.candidate_count == 2
    assert report.status is SemanticChangeStatus.REVIEW_REQUIRED
    assert evidence.calls[-1] == (31, None)


def test_out_of_registry_binding_selection_fails_before_evidence(
    governed_version: GovernedRegistryVersion,
) -> None:
    inspector, evidence, _dependencies, _store = _services(governed_version)
    unknown = GovernedMappingRef(
        logical_field=next(
            iter(governed_version.snapshot.registry.mapping_set.mappings)
        ).mapping.logical_field,
        physical_field=next(
            iter(governed_version.snapshot.registry.mapping_set.mappings)
        ).mapping.physical_field,
        version=999,
        approval_decision_id="mapping-decision-not-active",
        physical_type=next(
            iter(governed_version.snapshot.registry.mapping_set.mappings)
        ).physical_type,
    )
    selection = SemanticBindingSelection(
        mapping_approval_decision_id=unknown.approval_decision_id,
        mapping_version=unknown.version,
        physical_field=unknown.physical_field,
        locator=CatalogFieldLocator(
            asset=CatalogAssetLocator(
                workspace_id=SCOPE.workspace_id,
                connection_id=CONNECTION,
                asset_id=CatalogAssetId("crm.customers"),
            ),
            field_path=("customer_id",),
        ),
    )
    selections = SemanticBindingSelectionSet.create(
        scope=SCOPE,
        selections=(selection,),
    )

    with pytest.raises(SemanticChangeError) as raised:
        inspector.execute(binding_selections=selections)

    assert raised.value.code is SemanticChangeErrorCode.INVALID_REQUEST
    assert evidence.calls == []


def test_establish_baseline_is_exact_cas_and_replay_safe(
    governed_version: GovernedRegistryVersion,
) -> None:
    inspector, _evidence, _dependencies, store = _services(governed_version)
    report = inspector.execute()
    proposal = PrepareSemanticChangeDecision(store).execute(
        SCOPE,
        report.id,
        action=SemanticChangeDecisionAction.ESTABLISH_BASELINE,
    )
    approvals = PrepareSemanticChangeDecisionApproval()
    approval = approvals.execute(
        proposal,
        actor="steward-unit",
        approved_at=NOW,
        confirmation=SemanticChangeConfirmation.ESTABLISH,
    )
    committer = CommitSemanticChangeDecision(inspector, store)

    committed = committer.execute(proposal, approval)
    replay = committer.execute(proposal, approval)

    assert committed.head_revision == 1
    assert committed.baseline is not None
    assert committed.baseline.revision == 1
    assert committed.baseline.approval_id == approval.id
    assert replay.replayed
    assert replay.decision == committed.decision
    assert store.commit_writes == 1


def test_unchanged_approved_evidence_is_current_without_false_impact(
    governed_version: GovernedRegistryVersion,
) -> None:
    inspector, _evidence, dependencies, store = _services(governed_version)
    _establish_baseline(inspector, store)

    current = inspector.execute()

    assert current.status is SemanticChangeStatus.CURRENT
    assert current.findings == ()
    assert current.impacts.mapping_count == 0
    assert current.impacts.join_count == 0
    assert current.impacts.workflow_count == 0
    assert current.impacts.recipe_count == 0
    assert dependencies.resolved[-1] == ()


def test_compatible_definition_change_requires_and_advances_revalidation(
    governed_version: GovernedRegistryVersion,
) -> None:
    inspector, evidence, _dependencies, store = _services(governed_version)
    _establish_baseline(inspector, store)
    evidence.definition_change = True
    report = inspector.execute()

    assert report.status is SemanticChangeStatus.REVIEW_REQUIRED
    assert SemanticChangeKind.FIELD_DEFINITION_CHANGED in {item.kind for item in report.findings}
    proposal = PrepareSemanticChangeDecision(store).execute(
        SCOPE,
        report.id,
        action=SemanticChangeDecisionAction.REVALIDATE_COMPATIBLE_CHANGE,
    )
    approval = PrepareSemanticChangeDecisionApproval().execute(
        proposal,
        actor="steward-unit",
        approved_at=NOW,
        confirmation=SemanticChangeConfirmation.REVALIDATE,
    )
    committed = CommitSemanticChangeDecision(inspector, store).execute(
        proposal,
        approval,
    )

    assert committed.head_revision == 2
    assert committed.baseline is not None
    assert committed.baseline.revision == 2
    assert committed.decision.resulting_status is SemanticChangeStatus.REVALIDATED


def test_blocking_type_change_cannot_be_waived_and_rejection_keeps_baseline(
    governed_version: GovernedRegistryVersion,
) -> None:
    inspector, evidence, _dependencies, store = _services(governed_version)
    initial = _establish_baseline(inspector, store)
    evidence.type_change = True
    report = inspector.execute()

    assert report.status is SemanticChangeStatus.BLOCKED
    assert SemanticChangeKind.PHYSICAL_TYPE_CHANGED in {item.kind for item in report.findings}
    with pytest.raises(SemanticChangeError) as raised:
        PrepareSemanticChangeDecision(store).execute(
            SCOPE,
            report.id,
            action=SemanticChangeDecisionAction.REVALIDATE_COMPATIBLE_CHANGE,
        )
    assert raised.value.code is SemanticChangeErrorCode.BLOCKING_CHANGE

    proposal = PrepareSemanticChangeDecision(store).execute(
        SCOPE,
        report.id,
        action=SemanticChangeDecisionAction.REJECT_CHANGE,
    )
    approval = PrepareSemanticChangeDecisionApproval().execute(
        proposal,
        actor="steward-unit",
        approved_at=NOW,
        confirmation=SemanticChangeConfirmation.REJECT,
    )
    rejected = CommitSemanticChangeDecision(inspector, store).execute(proposal, approval)

    assert rejected.decision.resulting_status is SemanticChangeStatus.REJECTED
    assert rejected.baseline is None
    assert store.baseline == initial.baseline


def test_stale_head_conflicts_before_evidence_reinspection(
    governed_version: GovernedRegistryVersion,
) -> None:
    inspector, evidence, _dependencies, store = _services(governed_version)
    _establish_baseline(inspector, store)
    evidence.definition_change = True
    report = inspector.execute()
    proposal = PrepareSemanticChangeDecision(store).execute(
        SCOPE,
        report.id,
        action=SemanticChangeDecisionAction.REVALIDATE_COMPATIBLE_CHANGE,
    )
    approval = PrepareSemanticChangeDecisionApproval().execute(
        proposal,
        actor="steward-unit",
        approved_at=NOW,
        confirmation=SemanticChangeConfirmation.REVALIDATE,
    )
    competing = PrepareSemanticChangeDecisionApproval().execute(
        proposal,
        actor="competing-steward",
        approved_at=NOW,
        confirmation=SemanticChangeConfirmation.REVALIDATE,
    )
    CommitSemanticChangeDecision(inspector, store).execute(proposal, competing)
    calls_before = len(evidence.calls)

    with pytest.raises(SemanticChangeError) as raised:
        CommitSemanticChangeDecision(inspector, store).execute(proposal, approval)

    assert raised.value.code is SemanticChangeErrorCode.CAS_CONFLICT
    assert len(evidence.calls) == calls_before
    assert store.commit_writes == 2


def test_evidence_change_after_prepare_causes_zero_decision_write(
    governed_version: GovernedRegistryVersion,
) -> None:
    inspector, evidence, _dependencies, store = _services(governed_version)
    _establish_baseline(inspector, store)
    evidence.definition_change = True
    report = inspector.execute()
    proposal = PrepareSemanticChangeDecision(store).execute(
        SCOPE,
        report.id,
        action=SemanticChangeDecisionAction.REVALIDATE_COMPATIBLE_CHANGE,
    )
    approval = PrepareSemanticChangeDecisionApproval().execute(
        proposal,
        actor="steward-unit",
        approved_at=NOW,
        confirmation=SemanticChangeConfirmation.REVALIDATE,
    )
    evidence.type_change = True

    with pytest.raises(SemanticChangeError) as raised:
        CommitSemanticChangeDecision(inspector, store).execute(proposal, approval)

    assert raised.value.code is SemanticChangeErrorCode.CAS_CONFLICT
    assert store.commit_writes == 1
    assert store.head is not None
    assert store.head.head_revision == 1


def test_new_registry_activation_observes_without_old_baseline_and_recovers_history(
    governed_version: GovernedRegistryVersion,
) -> None:
    pointer_reader = _PointerReader(_pointer(governed_version))
    evidence = _Evidence()
    dependencies = _Dependencies()
    store = _Store()
    inspector = InspectSemanticChange(
        pointers=pointer_reader,
        versions=_VersionReader(governed_version),
        evidence=evidence,
        dependency_index=dependencies,
        store=store,
        scope=SCOPE,
    )
    initial = _establish_baseline(inspector, store)
    assert initial.baseline is not None
    old_context_fingerprint = initial.baseline.context_fingerprint
    assert pointer_reader.pointer is not None
    pointer_reader.pointer = pointer_reader.pointer.model_copy(
        update={
            "generation": pointer_reader.pointer.generation + 1,
            "transition_id": "transition-semantic-corrected",
        }
    )

    corrected_report = inspector.execute()

    assert evidence.calls[-1] == (31, None)
    assert corrected_report.baseline_revision is None
    assert corrected_report.context.fingerprint != old_context_fingerprint
    assert corrected_report.status is SemanticChangeStatus.REVIEW_REQUIRED
    proposal = PrepareSemanticChangeDecision(store).execute(
        SCOPE,
        corrected_report.id,
        action=SemanticChangeDecisionAction.ESTABLISH_BASELINE,
    )
    assert proposal.expected_head_revision == 1
    assert proposal.previous_head_context_fingerprint == old_context_fingerprint
    approval = PrepareSemanticChangeDecisionApproval().execute(
        proposal,
        actor="steward-corrected-registry",
        approved_at=NOW,
        confirmation=SemanticChangeConfirmation.ESTABLISH,
    )
    recovered = CommitSemanticChangeDecision(inspector, store).execute(
        proposal,
        approval,
    )

    assert recovered.head_revision == 2
    assert recovered.baseline is not None
    assert recovered.baseline.revision == 2
    assert recovered.baseline.context_fingerprint == corrected_report.context.fingerprint
    assert store.commit_writes == 2


def test_missing_baseline_in_same_registry_cannot_bypass_revalidation(
    governed_version: GovernedRegistryVersion,
) -> None:
    inspector, _evidence, _dependencies, store = _services(governed_version)
    established = _establish_baseline(inspector, store)
    assert established.baseline is not None
    store.baseline = None
    same_context_report = inspector.execute()
    assert same_context_report.baseline_revision is None

    with pytest.raises(SemanticChangeError) as raised:
        PrepareSemanticChangeDecision(store).execute(
            SCOPE,
            same_context_report.id,
            action=SemanticChangeDecisionAction.ESTABLISH_BASELINE,
        )

    assert raised.value.code is SemanticChangeErrorCode.INVALID_REQUEST
    assert store.commit_writes == 1


def test_incomplete_dependency_index_blocks_baseline_approval(
    governed_version: GovernedRegistryVersion,
) -> None:
    dependencies = _Dependencies(complete=False)
    inspector, _evidence, _dependencies, store = _services(
        governed_version,
        dependencies=dependencies,
    )

    report = inspector.execute()

    assert report.status is SemanticChangeStatus.BLOCKED
    assert not report.impacts.complete
    with pytest.raises(SemanticChangeError) as raised:
        PrepareSemanticChangeDecision(store).execute(
            SCOPE,
            report.id,
            action=SemanticChangeDecisionAction.ESTABLISH_BASELINE,
        )
    assert raised.value.code is SemanticChangeErrorCode.BLOCKING_CHANGE


def test_cross_scope_dependency_response_is_rejected_before_evidence(
    governed_version: GovernedRegistryVersion,
) -> None:
    dependencies = _Dependencies(
        state_scope=SemanticRegistryScope(
            workspace_id="workspace-other",
            catalog_scope=SCOPE.catalog_scope,
            registry_id=SCOPE.registry_id,
        )
    )
    inspector, evidence, _dependencies, _store = _services(
        governed_version,
        dependencies=dependencies,
    )

    with pytest.raises(SemanticChangeError) as raised:
        inspector.execute()

    assert raised.value.code is SemanticChangeErrorCode.INVALID_RESPONSE
    assert evidence.calls == []


def test_dependency_aware_gate_allows_exact_subset_and_fails_closed(
    governed_version: GovernedRegistryVersion,
) -> None:
    inspector, _evidence, _dependencies, store = _services(governed_version)
    committed = _establish_baseline(inspector, store)
    assert committed.baseline is not None
    report = inspector.execute()
    dependencies = SemanticPlanDependencies.create(
        scope=SCOPE,
        pointer_generation=report.context.pointer_generation,
        pointer_fingerprint=report.context.pointer_fingerprint,
        registry_version=report.context.registry_version,
        registry_fingerprint=report.context.registry_fingerprint,
        mappings=(report.context.mappings[0],),
        joins=(),
    )
    allowed_gate = _Gate(eligible=True)

    allowed = AssertSemanticContextCurrent(allowed_gate).execute(dependencies)

    assert allowed.eligible
    assert allowed_gate.calls == [dependencies]
    denied_gate = _Gate(eligible=False)
    with pytest.raises(SemanticChangeError) as denied:
        AssertSemanticContextCurrent(denied_gate).execute(dependencies)
    assert denied.value.code is SemanticChangeErrorCode.STALE_CONTEXT
    assert denied_gate.calls == [dependencies]
