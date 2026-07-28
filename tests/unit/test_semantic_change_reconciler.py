"""Application tests for one durable semantic-change reconciler iteration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest

from schemabridge.application.ports.semantic_change_scans import (
    SemanticChangeScanInspection,
    SemanticChangeScanRunnerError,
    SemanticChangeScanRunnerErrorCode,
    SemanticChangeScanStoreError,
    SemanticChangeScanStoreErrorCode,
)
from schemabridge.application.semantic_change_reconciler import (
    RunOneSemanticChangeScan,
    SemanticChangeReconcilerIterationOutcome,
    SemanticChangeReconcilerUseCaseError,
    SemanticChangeReconcilerUseCaseErrorCode,
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
    SemanticChangeReport,
    SemanticDependencyIndexState,
    SemanticEvidenceObservation,
    SemanticImpactKind,
    SemanticImpactSet,
    build_semantic_change_report,
    classify_semantic_change_findings,
)
from schemabridge.domain.semantic_change_scans import (
    SemanticChangeScanCompletion,
    SemanticChangeScanFailureCode,
    SemanticChangeScanRequest,
    SemanticChangeScanSourceKind,
    SemanticChangeScanStatus,
    SemanticChangeScanSupersession,
    claim_semantic_change_scan,
    complete_semantic_change_scan,
    fail_semantic_change_scan,
    heartbeat_semantic_change_scan,
    reclaim_expired_semantic_change_scan,
    supersede_semantic_change_scan,
)
from schemabridge.domain.semantic_registry import (
    PhysicalValueType,
    SemanticRegistryScope,
)

BASE = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
CAPABILITY = "semantic-capability-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
SCOPE = SemanticRegistryScope(
    workspace_id="workspace-semantic",
    catalog_scope="synthetic-demo",
    registry_id="enterprise_registry",
)


@dataclass
class TickingClock:
    value: datetime = BASE

    def now(self) -> datetime:
        self.value += timedelta(seconds=1)
        return self.value

    def advance_to(self, value: datetime) -> None:
        if value > self.value:
            self.value = value


def _request(*, max_attempts: int = 5) -> SemanticChangeScanRequest:
    return SemanticChangeScanRequest.requested(
        workspace_id=SCOPE.workspace_id,
        source_kind=SemanticChangeScanSourceKind.REGISTRY_POINTER,
        source_event_key="registry-transition-7",
        source_fingerprint="a" * 64,
        catalog_scope=SCOPE.catalog_scope,
        registry_id=SCOPE.registry_id,
        registry_generation=7,
        requested_at=BASE,
        max_attempts=max_attempts,
    )


def _inspection(
    *,
    scope: SemanticRegistryScope = SCOPE,
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
        pointer_generation=7,
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
                generation=12,
                inventory_fingerprint="e" * 64,
            ),
        )
    )
    locator = CatalogFieldLocator(
        asset=CatalogAssetLocator(
            workspace_id=scope.workspace_id,
            connection_id=CatalogConnectionId("warehouse-primary"),
            asset_id=CatalogAssetId("urn:li:dataset:customer"),
        ),
        field_path=("customer_id",),
    )
    binding = GovernedResourceBinding.create(
        mapping=mapping,
        locator=locator,
        catalog_generation=12,
        catalog_generation_fingerprint=generations.fingerprint,
        asset_metadata_fingerprint="1" * 64,
        field_metadata_fingerprint="2" * 64,
        field_definition_fingerprint="3" * 64,
        field_terms_fingerprint="4" * 64,
    )
    field_evidence = ObservedFieldEvidence.create(
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
        fields=(field_evidence,),
        joins=(),
        observed_at=BASE,
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


@dataclass
class FakeRunner:
    inspection: SemanticChangeScanInspection
    error: SemanticChangeScanRunnerError | None = None
    calls: int = 0
    continuation_calls: int = 0
    stop_before_continuation: Event | None = None

    def inspect(
        self,
        request: SemanticChangeScanRequest,
        *,
        should_continue: Callable[[], bool],
    ) -> SemanticChangeScanInspection:
        self.calls += 1
        assert request.status is SemanticChangeScanStatus.LEASED
        if self.error is not None:
            raise self.error
        if self.stop_before_continuation is not None:
            self.stop_before_continuation.set()
        self.continuation_calls += 1
        if not should_continue():
            raise SemanticChangeScanRunnerError(
                SemanticChangeScanRunnerErrorCode.STOP_REQUESTED,
                "synthetic stop",
            )
        return self.inspection


@dataclass
class FakeScanStore:
    state: SemanticChangeScanRequest
    clock: TickingClock
    obsolete: bool = False
    drop_on_load: bool = False
    commit_then_error: bool = False
    heartbeat_calls: int = 0
    complete_calls: int = 0
    fail_calls: int = 0
    reports: dict[str, SemanticChangeReport] = field(default_factory=dict)

    def reclaim_expired(self, *, limit: int, retention: timedelta) -> int:
        assert 1 <= limit <= 1_000
        if (
            self.state.status is SemanticChangeScanStatus.LEASED
            and self.state.lease is not None
            and self.state.lease.expires_at <= self.clock.value
        ):
            retain_until = (
                None
                if self.state.attempts < self.state.max_attempts
                else self.clock.value + retention
            )
            self.state = reclaim_expired_semantic_change_scan(
                self.state,
                reclaimed_at=self.clock.now(),
                retain_until=retain_until,
            )
            return 1
        return 0

    def supersede_obsolete(self, *, limit: int, retention: timedelta) -> int:
        assert 1 <= limit <= 1_000
        del retention
        return 0

    def claim_next(
        self,
        *,
        reconciler_id: str,
        lease_capability: str,
        lease_duration: timedelta,
    ) -> SemanticChangeScanRequest | None:
        if (
            self.state.status
            not in {
                SemanticChangeScanStatus.REQUESTED,
                SemanticChangeScanStatus.RETRY_WAIT,
            }
            or self.state.available_at > self.clock.value
        ):
            return None
        claimed_at = self.clock.now()
        self.state = claim_semantic_change_scan(
            self.state,
            reconciler_id=reconciler_id,
            lease_capability=lease_capability,
            claimed_at=claimed_at,
            lease_expires_at=claimed_at + lease_duration,
        )
        return self.state

    def load(
        self,
        workspace_id: str,
        scan_id: str,
    ) -> SemanticChangeScanRequest | None:
        assert workspace_id == self.state.workspace_id
        assert scan_id == self.state.scan_id
        if self.drop_on_load:
            return None
        return self.state

    def heartbeat(
        self,
        workspace_id: str,
        scan_id: str,
        *,
        reconciler_id: str,
        lease_capability: str,
        fencing_token: int,
        lease_duration: timedelta,
    ) -> SemanticChangeScanRequest:
        assert workspace_id == self.state.workspace_id
        assert scan_id == self.state.scan_id
        heartbeat_at = self.clock.now()
        self.state = heartbeat_semantic_change_scan(
            self.state,
            reconciler_id=reconciler_id,
            lease_capability=lease_capability,
            fencing_token=fencing_token,
            heartbeat_at=heartbeat_at,
            lease_expires_at=heartbeat_at + lease_duration,
        )
        self.heartbeat_calls += 1
        return self.state

    def supersede_if_obsolete(
        self,
        workspace_id: str,
        scan_id: str,
        *,
        reconciler_id: str,
        lease_capability: str,
        fencing_token: int,
        retention: timedelta,
    ) -> SemanticChangeScanRequest:
        assert workspace_id == self.state.workspace_id
        assert scan_id == self.state.scan_id
        if self.obsolete and self.state.status is SemanticChangeScanStatus.LEASED:
            superseded_at = self.clock.now()
            self.state = supersede_semantic_change_scan(
                self.state,
                superseded_by=SemanticChangeScanSupersession(
                    scan_id=f"scan_{'f' * 64}",
                    source_fingerprint="f" * 64,
                ),
                superseded_at=superseded_at,
                retain_until=superseded_at + retention,
                reconciler_id=reconciler_id,
                lease_capability=lease_capability,
                fencing_token=fencing_token,
            )
        return self.state

    def complete(
        self,
        workspace_id: str,
        scan_id: str,
        *,
        reconciler_id: str,
        lease_capability: str,
        fencing_token: int,
        inspection: SemanticChangeScanInspection,
        completed_at: datetime,
        retain_until: datetime,
    ) -> SemanticChangeScanRequest:
        assert workspace_id == self.state.workspace_id
        assert scan_id == self.state.scan_id
        completion = SemanticChangeScanCompletion(
            report_id=inspection.report.id,
            report_fingerprint=inspection.report.fingerprint,
            completed_at=completed_at,
        )
        self.state = complete_semantic_change_scan(
            self.state,
            reconciler_id=reconciler_id,
            lease_capability=lease_capability,
            fencing_token=fencing_token,
            completion=completion,
            retain_until=retain_until,
        )
        self.reports.setdefault(inspection.report.id, inspection.report)
        self.complete_calls += 1
        if self.commit_then_error:
            raise SemanticChangeScanStoreError(
                SemanticChangeScanStoreErrorCode.STORE_UNAVAILABLE,
                "synthetic post-commit disconnect",
            )
        return self.state

    def fail(
        self,
        workspace_id: str,
        scan_id: str,
        *,
        reconciler_id: str,
        lease_capability: str,
        fencing_token: int,
        code: SemanticChangeScanFailureCode,
        failed_at: datetime,
        retry_at: datetime | None,
        retain_until: datetime | None,
    ) -> SemanticChangeScanRequest:
        assert workspace_id == self.state.workspace_id
        assert scan_id == self.state.scan_id
        self.state = fail_semantic_change_scan(
            self.state,
            reconciler_id=reconciler_id,
            lease_capability=lease_capability,
            fencing_token=fencing_token,
            code=code,
            failed_at=failed_at,
            retry_at=retry_at,
            retain_until=retain_until,
        )
        self.fail_calls += 1
        return self.state


def _worker(
    store: FakeScanStore,
    runner: FakeRunner,
    *,
    stop_event: Event | None = None,
) -> RunOneSemanticChangeScan:
    return RunOneSemanticChangeScan(
        scans=store,
        runner=runner,
        clock=store.clock,
        capability_factory=lambda: CAPABILITY,
        reconciler_id="semantic-reconciler-a",
        stop_requested=(stop_event or Event()).is_set,
    )


def test_success_heartbeats_and_atomically_commits_one_exact_report() -> None:
    clock = TickingClock()
    store = FakeScanStore(_request(), clock)
    runner = FakeRunner(_inspection())

    result = _worker(store, runner).execute()

    assert result.outcome is SemanticChangeReconcilerIterationOutcome.COMPLETED
    assert result.report_id == runner.inspection.report.id
    assert store.state.status is SemanticChangeScanStatus.COMPLETED
    assert store.heartbeat_calls == 3
    assert store.complete_calls == 1
    assert tuple(store.reports) == (runner.inspection.report.id,)
    assert runner.continuation_calls == 1
    assert CAPABILITY not in store.state.model_dump_json()


def test_transient_runner_failure_retries_then_fails_at_durable_attempt_limit() -> None:
    clock = TickingClock()
    store = FakeScanStore(_request(max_attempts=2), clock)
    runner = FakeRunner(
        _inspection(),
        error=SemanticChangeScanRunnerError(
            SemanticChangeScanRunnerErrorCode.EVIDENCE_UNAVAILABLE,
            "synthetic protected detail",
        ),
    )
    worker = _worker(store, runner)

    first = worker.execute()
    assert first.outcome is SemanticChangeReconcilerIterationOutcome.RETRY_SCHEDULED
    assert first.failure_code is SemanticChangeScanFailureCode.EVIDENCE_UNAVAILABLE
    first_state = store.state
    assert first_state.status is SemanticChangeScanStatus.RETRY_WAIT

    clock.advance_to(first_state.available_at)
    second = worker.execute()
    assert second.outcome is SemanticChangeReconcilerIterationOutcome.FAILED
    assert second.failure_code is SemanticChangeScanFailureCode.EVIDENCE_UNAVAILABLE
    final_state = store.state
    assert final_state.status is SemanticChangeScanStatus.FAILED
    assert final_state.attempts == final_state.max_attempts == 2
    assert "synthetic protected detail" not in final_state.model_dump_json()


def test_lost_lease_aborts_without_stale_completion_or_failure_write() -> None:
    clock = TickingClock()
    store = FakeScanStore(_request(), clock, drop_on_load=True)
    runner = FakeRunner(_inspection())

    with pytest.raises(SemanticChangeReconcilerUseCaseError) as raised:
        _worker(store, runner).execute()

    assert raised.value.code is SemanticChangeReconcilerUseCaseErrorCode.LEASE_LOST
    assert store.complete_calls == 0
    assert store.fail_calls == 0
    assert store.state.status is SemanticChangeScanStatus.LEASED


def test_post_commit_disconnect_recovers_exact_completion_without_duplicate_report() -> None:
    clock = TickingClock()
    store = FakeScanStore(_request(), clock, commit_then_error=True)
    runner = FakeRunner(_inspection())

    result = _worker(store, runner).execute()

    assert result.outcome is SemanticChangeReconcilerIterationOutcome.COMPLETED
    assert store.complete_calls == 1
    assert len(store.reports) == 1
    assert store.state.status is SemanticChangeScanStatus.COMPLETED


def test_obsolete_request_is_superseded_before_inspection() -> None:
    clock = TickingClock()
    store = FakeScanStore(_request(), clock, obsolete=True)
    runner = FakeRunner(_inspection())

    result = _worker(store, runner).execute()

    assert result.outcome is SemanticChangeReconcilerIterationOutcome.SUPERSEDED
    assert store.state.status is SemanticChangeScanStatus.SUPERSEDED
    assert runner.calls == 0
    assert store.complete_calls == 0


def test_shutdown_during_inspection_requeues_without_committing_report() -> None:
    stop_event = Event()
    clock = TickingClock()
    store = FakeScanStore(_request(), clock)
    runner = FakeRunner(_inspection(), stop_before_continuation=stop_event)

    result = _worker(store, runner, stop_event=stop_event).execute()

    assert result.outcome is SemanticChangeReconcilerIterationOutcome.STOPPED
    assert result.failure_code is SemanticChangeScanFailureCode.SHUTDOWN_REQUESTED
    assert store.state.status is SemanticChangeScanStatus.RETRY_WAIT
    assert store.complete_calls == 0
    assert store.reports == {}


def test_well_formed_cross_scope_inspection_fails_safely_without_report_write() -> None:
    foreign_scope = SemanticRegistryScope(
        workspace_id="workspace-foreign",
        catalog_scope=SCOPE.catalog_scope,
        registry_id=SCOPE.registry_id,
    )
    clock = TickingClock()
    store = FakeScanStore(_request(), clock)
    runner = FakeRunner(_inspection(scope=foreign_scope))

    result = _worker(store, runner).execute()

    assert result.outcome is SemanticChangeReconcilerIterationOutcome.FAILED
    assert result.failure_code is SemanticChangeScanFailureCode.INSPECTION_INVALID
    assert store.state.status is SemanticChangeScanStatus.FAILED
    assert store.complete_calls == 0
    assert store.reports == {}
