"""Concrete semantic scan-runner boundary tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from schemabridge.adapters.semantic_change.scan_runner import (
    InspectSemanticChangeScanRunner,
)
from schemabridge.application.ports.semantic_change_scans import (
    SemanticChangeScanInspection,
    SemanticChangeScanRunnerError,
    SemanticChangeScanRunnerErrorCode,
)
from schemabridge.application.semantic_change import (
    InspectSemanticChange,
    SemanticChangeError,
    SemanticChangeErrorCode,
)
from schemabridge.domain.catalog_inventory import (
    CatalogAssetId,
    CatalogAssetLocator,
    CatalogConnectionId,
    CatalogFieldLocator,
)
from schemabridge.domain.concepts import LogicalFieldRef
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.semantic_change import (
    CatalogGenerationObservation,
    CatalogGenerationVector,
    GovernedMappingRef,
    GovernedResourceBinding,
    ObservedFieldEvidence,
    SemanticChangeImpact,
    SemanticChangeInspectionContext,
    SemanticDependencyIndexState,
    SemanticEvidenceObservation,
    SemanticImpactKind,
    SemanticImpactSet,
    build_semantic_change_report,
    classify_semantic_change_findings,
)
from schemabridge.domain.semantic_change_scans import (
    SemanticChangeScanRequest,
    SemanticChangeScanSourceKind,
    claim_semantic_change_scan,
)
from schemabridge.domain.semantic_registry import (
    PhysicalValueType,
    SemanticRegistryScope,
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
CAPABILITY = "semantic-capability-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
SCOPE = SemanticRegistryScope(
    workspace_id="workspace-semantic-runner",
    catalog_scope="synthetic-demo",
    registry_id="enterprise_registry",
)


def _leased_registry_request() -> SemanticChangeScanRequest:
    request = SemanticChangeScanRequest.requested(
        workspace_id=SCOPE.workspace_id,
        source_kind=SemanticChangeScanSourceKind.REGISTRY_POINTER,
        source_event_key="registry-transition-7",
        source_fingerprint="a" * 64,
        catalog_scope=SCOPE.catalog_scope,
        registry_id=SCOPE.registry_id,
        registry_generation=7,
        requested_at=NOW,
    )
    return claim_semantic_change_scan(
        request,
        reconciler_id="semantic-reconciler-a",
        lease_capability=CAPABILITY,
        claimed_at=NOW + timedelta(seconds=1),
        lease_expires_at=NOW + timedelta(seconds=61),
    )


def _leased_catalog_request() -> SemanticChangeScanRequest:
    request = SemanticChangeScanRequest.requested(
        workspace_id=SCOPE.workspace_id,
        source_kind=SemanticChangeScanSourceKind.CATALOG_GENERATION,
        source_event_key="warehouse-primary:12",
        source_fingerprint="f" * 64,
        catalog_scope=SCOPE.catalog_scope,
        registry_id=SCOPE.registry_id,
        connection_id="warehouse-primary",
        base_catalog_generation=11,
        observed_catalog_generation=12,
        requested_at=NOW,
    )
    return claim_semantic_change_scan(
        request,
        reconciler_id="semantic-reconciler-a",
        lease_capability=CAPABILITY,
        claimed_at=NOW + timedelta(seconds=1),
        lease_expires_at=NOW + timedelta(seconds=61),
    )


def _inspection(
    *,
    scope: SemanticRegistryScope = SCOPE,
    pointer_generation: int = 7,
    catalog_generation: int = 12,
) -> SemanticChangeScanInspection:
    mapping = GovernedMappingRef(
        logical_field=LogicalFieldRef("Customer.customer_id"),
        physical_field=PhysicalFieldRef("crm.customers.customer_id"),
        version=1,
        approval_decision_id="mapping-decision-1",
        physical_type=PhysicalValueType.STRING,
    )
    dependency = SemanticDependencyIndexState(
        scope=scope,
        watermark=3,
        fingerprint="d" * 64,
        complete=True,
    )
    context = SemanticChangeInspectionContext.create(
        scope=scope,
        pointer_generation=pointer_generation,
        pointer_fingerprint="b" * 64,
        pointer_transition_id="registry-transition-7",
        registry_version=4,
        registry_fingerprint="c" * 64,
        mappings=(mapping,),
        joins=(),
        dependency_index=dependency,
    )
    generations = CatalogGenerationVector.create(
        (
            CatalogGenerationObservation(
                connection_id=CatalogConnectionId("warehouse-primary"),
                generation=catalog_generation,
                inventory_fingerprint="e" * 64,
            ),
        )
    )
    locator = CatalogFieldLocator(
        asset=CatalogAssetLocator(
            workspace_id=scope.workspace_id,
            connection_id=CatalogConnectionId("warehouse-primary"),
            asset_id=CatalogAssetId("crm.customers"),
        ),
        field_path=("customer_id",),
    )
    binding = GovernedResourceBinding.create(
        mapping=mapping,
        locator=locator,
        catalog_generation=catalog_generation,
        catalog_generation_fingerprint=generations.fingerprint,
        asset_metadata_fingerprint="1" * 64,
        field_metadata_fingerprint="2" * 64,
        field_definition_fingerprint="3" * 64,
        field_terms_fingerprint="4" * 64,
    )
    evidence = ObservedFieldEvidence.create(
        mapping=mapping,
        binding=binding,
        present=True,
        candidate_count=1,
        normalized_type=PhysicalValueType.STRING,
        nullable=False,
        is_part_of_key=True,
        asset_metadata_fingerprint="1" * 64,
        field_metadata_fingerprint="2" * 64,
        field_definition_fingerprint="3" * 64,
        field_terms_fingerprint="4" * 64,
        reason_code=None,
    )
    observation = SemanticEvidenceObservation.create(
        context=context,
        catalog_generations=generations,
        fields=(evidence,),
        joins=(),
        observed_at=NOW,
        complete=True,
    )
    findings = classify_semantic_change_findings(observation, None)
    impacts = SemanticImpactSet.create(
        impacts=(
            SemanticChangeImpact.create(
                kind=SemanticImpactKind.MAPPING,
                artifact_id=mapping.logical_field.root,
                artifact_version=mapping.version,
                finding_ids=tuple(item.id for item in findings),
            ),
        ),
        complete=True,
        watermark=dependency.watermark,
        dependency_index_fingerprint=dependency.fingerprint,
    )
    report = build_semantic_change_report(observation, None, impacts)
    return SemanticChangeScanInspection(report=report, observation=observation)


def _inspector(scope: SemanticRegistryScope) -> InspectSemanticChange:
    unused = cast(Any, object())
    return InspectSemanticChange(
        pointers=unused,
        versions=unused,
        evidence=unused,
        dependency_index=unused,
        store=unused,
        scope=scope,
    )


def test_dependency_index_is_prepared_before_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    expected = _inspection()

    def capture(
        self: InspectSemanticChange,
        *,
        binding_selections: object = None,
    ) -> tuple[object, object]:
        del self, binding_selections
        events.append("capture")
        return expected.report, expected.observation

    monkeypatch.setattr(InspectSemanticChange, "capture_current", capture)
    runner = InspectSemanticChangeScanRunner(
        scope_resolver=lambda request: SCOPE,
        inspector_factory=lambda scope, request: _inspector(scope),
        prepare_dependencies=lambda request, scope, should_continue: events.append("dependencies"),
    )

    result = runner.inspect(_leased_registry_request(), should_continue=lambda: True)

    assert result == expected
    assert events == ["dependencies", "capture"]


def test_dependency_preparation_failure_stops_before_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captures = 0

    def capture(
        self: InspectSemanticChange,
        *,
        binding_selections: object = None,
    ) -> tuple[object, object]:
        del self, binding_selections
        nonlocal captures
        captures += 1
        expected = _inspection()
        return expected.report, expected.observation

    def fail_dependencies(
        request: SemanticChangeScanRequest,
        scope: SemanticRegistryScope,
        should_continue: object,
    ) -> None:
        del request, scope, should_continue
        raise RuntimeError("protected dependency detail")

    monkeypatch.setattr(InspectSemanticChange, "capture_current", capture)
    runner = InspectSemanticChangeScanRunner(
        scope_resolver=lambda request: SCOPE,
        inspector_factory=lambda scope, request: _inspector(scope),
        prepare_dependencies=fail_dependencies,
    )

    with pytest.raises(SemanticChangeScanRunnerError) as raised:
        runner.inspect(_leased_registry_request(), should_continue=lambda: True)

    assert raised.value.code is SemanticChangeScanRunnerErrorCode.DEPENDENCY_INDEX_INCOMPLETE
    assert captures == 0
    assert "protected dependency detail" not in str(raised.value)


def test_catalog_trigger_requires_resolved_scope_and_exact_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _inspection(catalog_generation=12)

    def capture(
        self: InspectSemanticChange,
        *,
        binding_selections: object = None,
    ) -> tuple[object, object]:
        del self, binding_selections
        return expected.report, expected.observation

    monkeypatch.setattr(InspectSemanticChange, "capture_current", capture)
    runner = InspectSemanticChangeScanRunner(
        scope_resolver=lambda request: SCOPE,
        inspector_factory=lambda scope, request: _inspector(scope),
        prepare_dependencies=lambda request, scope, should_continue: None,
    )

    assert runner.inspect(_leased_catalog_request(), should_continue=lambda: True) == expected

    wrong = _inspection(catalog_generation=13)

    def capture_wrong(
        self: InspectSemanticChange,
        *,
        binding_selections: object = None,
    ) -> tuple[object, object]:
        del self, binding_selections
        return wrong.report, wrong.observation

    monkeypatch.setattr(InspectSemanticChange, "capture_current", capture_wrong)
    with pytest.raises(SemanticChangeScanRunnerError) as raised:
        runner.inspect(_leased_catalog_request(), should_continue=lambda: True)
    assert raised.value.code is SemanticChangeScanRunnerErrorCode.INVALID_INSPECTION

    other_registry = SemanticRegistryScope(
        workspace_id=SCOPE.workspace_id,
        catalog_scope=SCOPE.catalog_scope,
        registry_id="other_registry",
    )
    wrong_scope = InspectSemanticChangeScanRunner(
        scope_resolver=lambda request: other_registry,
        inspector_factory=lambda scope, request: _inspector(scope),
        prepare_dependencies=lambda request, scope, should_continue: None,
    )
    with pytest.raises(SemanticChangeScanRunnerError) as wrong_scope_error:
        wrong_scope.inspect(_leased_catalog_request(), should_continue=lambda: True)
    assert wrong_scope_error.value.code is SemanticChangeScanRunnerErrorCode.INVALID_INSPECTION


def test_scope_mismatch_and_graceful_stop_happen_before_dependency_work() -> None:
    calls = 0

    def prepare(
        request: SemanticChangeScanRequest,
        scope: SemanticRegistryScope,
        should_continue: object,
    ) -> None:
        del request, scope, should_continue
        nonlocal calls
        calls += 1

    foreign = SemanticRegistryScope(
        workspace_id="workspace-foreign",
        catalog_scope=SCOPE.catalog_scope,
        registry_id=SCOPE.registry_id,
    )
    wrong_scope = InspectSemanticChangeScanRunner(
        scope_resolver=lambda request: foreign,
        inspector_factory=lambda scope, request: _inspector(scope),
        prepare_dependencies=prepare,
    )
    with pytest.raises(SemanticChangeScanRunnerError) as mismatch:
        wrong_scope.inspect(_leased_registry_request(), should_continue=lambda: True)
    assert mismatch.value.code is SemanticChangeScanRunnerErrorCode.INVALID_INSPECTION

    stopped = InspectSemanticChangeScanRunner(
        scope_resolver=lambda request: SCOPE,
        inspector_factory=lambda scope, request: _inspector(scope),
        prepare_dependencies=prepare,
    )
    with pytest.raises(SemanticChangeScanRunnerError) as stop:
        stopped.inspect(_leased_registry_request(), should_continue=lambda: False)
    assert stop.value.code is SemanticChangeScanRunnerErrorCode.STOP_REQUESTED
    assert calls == 0


def test_application_failure_is_sanitized_to_closed_runner_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def capture(
        self: InspectSemanticChange,
        *,
        binding_selections: object = None,
    ) -> tuple[object, object]:
        del self, binding_selections
        raise SemanticChangeError(
            SemanticChangeErrorCode.EVIDENCE_UNAVAILABLE,
            "protected adapter detail",
        )

    monkeypatch.setattr(InspectSemanticChange, "capture_current", capture)
    runner = InspectSemanticChangeScanRunner(
        scope_resolver=lambda request: SCOPE,
        inspector_factory=lambda scope, request: _inspector(scope),
        prepare_dependencies=lambda request, scope, should_continue: None,
    )

    with pytest.raises(SemanticChangeScanRunnerError) as raised:
        runner.inspect(_leased_registry_request(), should_continue=lambda: True)

    assert raised.value.code is SemanticChangeScanRunnerErrorCode.EVIDENCE_UNAVAILABLE
    assert "protected adapter detail" not in str(raised.value)


def test_continuation_callback_exception_is_not_misreported_as_scan_failure() -> None:
    runner = InspectSemanticChangeScanRunner(
        scope_resolver=lambda request: SCOPE,
        inspector_factory=lambda scope, request: _inspector(scope),
        prepare_dependencies=lambda request, scope, should_continue: None,
    )

    def lost_lease() -> bool:
        raise LookupError("lease lost sentinel")

    with pytest.raises(LookupError, match="lease lost sentinel"):
        runner.inspect(_leased_registry_request(), should_continue=lost_lease)
