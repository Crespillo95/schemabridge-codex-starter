"""Pre-I/O semantic-change gate tests for governed execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest

from schemabridge.adapters.planning.recorded import RecordedSemanticPlanningContext
from schemabridge.adapters.requests.recorded_context import RecordedRequestContextAdapter
from schemabridge.adapters.semantic_change.postgres_read import _gate_assessment
from schemabridge.adapters.sql.compiler import PostgresQueryCompiler
from schemabridge.adapters.sql.guard import SqlGlotPolicyGuard
from schemabridge.application.connectors import (
    ConnectorTargetError,
    ConnectorTargetErrorCode,
)
from schemabridge.application.governed_execution import (
    ExecuteGovernedRequest,
    PlanSemanticRequest,
    PrepareGovernedRequest,
)
from schemabridge.application.guided_requests import (
    BuildGuidedRequest,
    GuidedRequestCase,
    build_demo_guided_input,
)
from schemabridge.application.query_cost import AssessGovernedQueryCost
from schemabridge.application.query_execution import (
    CompiledQuery,
    QueryPreviewResult,
    ValidatedQuery,
)
from schemabridge.application.semantic_change import (
    AssertSemanticContextCurrent,
    SemanticChangeError,
    SemanticChangeErrorCode,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    QueryCostAssessment,
    QueryCostBudget,
    QueryCostDecision,
    SourceConnectorKind,
    SourceDialect,
    postgres_type_contract_fingerprint,
)
from schemabridge.domain.plans import QueryPlan, QueryPolicy
from schemabridge.domain.request_context import ValidatedAnalyticalRequest
from schemabridge.domain.resolution import (
    RejectedSourceReport,
    RejectionCheck,
    ResolutionLimits,
)
from schemabridge.domain.semantic_change import (
    SemanticChangeKind,
    SemanticChangeStatus,
    SemanticContextGateAssessment,
    SemanticPlanDependencies,
)
from schemabridge.domain.semantic_registry import (
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
)

ROOT = Path(__file__).resolve().parents[2]
POINTER_FINGERPRINT = "a" * 64


@dataclass(frozen=True, slots=True)
class ActivePlanningContext:
    recorded: RecordedSemanticPlanningContext

    @property
    def scope(self) -> SemanticRegistryScope:
        return self.recorded.scope

    def load(self) -> ScopedSemanticRegistrySnapshot:
        loaded = self.recorded.load()
        return loaded.model_copy(
            update={
                "activation_generation": 7,
                "active_pointer_fingerprint": POINTER_FINGERPRINT,
            }
        )


@dataclass(slots=True)
class MutableGate:
    eligible: bool = True
    unavailable: bool = False
    received: list[SemanticPlanDependencies] = field(default_factory=list)

    def assess(
        self,
        dependencies: SemanticPlanDependencies,
    ) -> SemanticContextGateAssessment:
        self.received.append(dependencies)
        if self.unavailable:
            raise RuntimeError("synthetic semantic gate outage")
        if self.eligible:
            return SemanticContextGateAssessment(
                dependencies_fingerprint=dependencies.fingerprint,
                eligible=True,
                status=SemanticChangeStatus.CURRENT,
                connection_id=CatalogConnectionId("warehouse-primary"),
                baseline_revision=3,
            )
        return SemanticContextGateAssessment(
            dependencies_fingerprint=dependencies.fingerprint,
            eligible=False,
            status=SemanticChangeStatus.BLOCKED,
            reason_codes=(SemanticChangeKind.PHYSICAL_TYPE_CHANGED,),
            baseline_revision=3,
        )


@dataclass(slots=True)
class CountingCompiler:
    calls: int = 0
    inner: PostgresQueryCompiler = field(default_factory=PostgresQueryCompiler)

    def compile(
        self,
        plan: QueryPlan,
        *,
        max_preview_rows: int,
        target: GovernedExecutionTarget | None = None,
    ) -> CompiledQuery:
        self.calls += 1
        return self.inner.compile(
            plan,
            max_preview_rows=max_preview_rows,
            target=target,
        )


@dataclass(slots=True)
class CountingGuard:
    calls: int = 0
    inner: SqlGlotPolicyGuard = field(default_factory=SqlGlotPolicyGuard)

    def validate(
        self,
        query: CompiledQuery,
        policy: QueryPolicy,
        *,
        target: GovernedExecutionTarget | None = None,
    ) -> ValidatedQuery:
        self.calls += 1
        return self.inner.validate(query, policy, target=target)


@dataclass(slots=True)
class MutableTargetResolver:
    target: GovernedExecutionTarget
    calls: int = 0

    def resolve_current(
        self,
        *,
        workspace_id: str,
        connection_id: CatalogConnectionId,
    ) -> GovernedExecutionTarget:
        self.calls += 1
        del workspace_id, connection_id
        return self.target


@dataclass(slots=True)
class CountingCostPreflight:
    calls: int = 0

    def assess(
        self,
        query: ValidatedQuery,
        target: GovernedExecutionTarget,
    ) -> QueryCostAssessment:
        self.calls += 1
        assert query.target_fingerprint == target.fingerprint
        return QueryCostAssessment(
            decision=QueryCostDecision.ACCEPTED,
            total_cost=Decimal("100"),
            estimated_root_rows=10,
            plan_width=64,
            plan_node_count=3,
            plan_depth=2,
            response_bytes=512,
            observed_reader=target.expected_reader,
            read_only=True,
            explain_timeout_ms=target.cost_budget.explain_timeout_ms,
            target_fingerprint=target.fingerprint,
            budget_fingerprint=target.cost_budget_fingerprint,
            cost_budget=target.cost_budget,
        )


@dataclass(slots=True)
class CountingPreview:
    calls: int = 0

    def execute(
        self,
        query: ValidatedQuery,
        *,
        target: GovernedExecutionTarget | None = None,
    ) -> QueryPreviewResult:
        self.calls += 1
        assert target is None or query.target_fingerprint == target.fingerprint
        return QueryPreviewResult(
            columns=("registration_date", "secondary_holder_customers"),
            rows=(("2026-01-01", 2),),
            database_user="schemabridge_reader",
            transaction_read_only=True,
            statement_timeout_ms=query.statement_timeout_ms,
            truncated=False,
        )


@dataclass(slots=True)
class CountingRejectionReporter:
    calls: int = 0

    def inspect(
        self,
        checks: tuple[RejectionCheck, ...],
        *,
        statement_timeout_ms: int,
        should_continue: Callable[[], bool] | None = None,
        target: GovernedExecutionTarget | None = None,
    ) -> RejectedSourceReport:
        self.calls += 1
        del target
        assert should_continue is None or should_continue()
        return RejectedSourceReport(
            inspected_fields=tuple(check.physical_field for check in checks),
            database_user="schemabridge_reader",
            transaction_read_only=True,
            statement_timeout_ms=statement_timeout_ms,
        )


@dataclass(frozen=True, slots=True)
class GateHarness:
    request: ValidatedAnalyticalRequest
    prepare: PrepareGovernedRequest
    execute: ExecuteGovernedRequest
    scope: SemanticRegistryScope
    gate: MutableGate
    compiler: CountingCompiler
    guard: CountingGuard
    preview: CountingPreview
    rejections: CountingRejectionReporter
    target_resolver: MutableTargetResolver | None
    cost_preflight: CountingCostPreflight | None


def _harness(
    gate: MutableGate,
    *,
    target_resolver: MutableTargetResolver | None = None,
) -> GateHarness:
    logical_path = ROOT / "demo/ground_truth/approved_logical_context.yml"
    context = ActivePlanningContext(
        RecordedSemanticPlanningContext(
            logical_path,
            ROOT / "demo/ground_truth/planning_mappings.yml",
            ROOT / "demo/ground_truth/join_contracts.yml",
        )
    )
    request = BuildGuidedRequest(RecordedRequestContextAdapter(logical_path)).execute(
        build_demo_guided_input(GuidedRequestCase.NORTH_STAR)
    )
    compiler = CountingCompiler()
    guard = CountingGuard()
    preview = CountingPreview()
    rejections = CountingRejectionReporter()
    cost_preflight = CountingCostPreflight() if target_resolver is not None else None
    prepare = PrepareGovernedRequest(
        planner=PlanSemanticRequest(context, ResolutionLimits()),
        compiler=compiler,
        guard=guard,
        semantic_gate=AssertSemanticContextCurrent(gate),
        semantic_scope=context.scope,
        target_resolver=target_resolver,
        cost_preflight=(
            AssessGovernedQueryCost(cost_preflight) if cost_preflight is not None else None
        ),
    )
    return GateHarness(
        request=request,
        prepare=prepare,
        execute=ExecuteGovernedRequest(
            prepare=prepare,
            executor=preview,
            rejection_reporter=rejections,
        ),
        scope=context.scope,
        gate=gate,
        compiler=compiler,
        guard=guard,
        preview=preview,
        rejections=rejections,
        target_resolver=target_resolver,
        cost_preflight=cost_preflight,
    )


def _target(
    *,
    workspace_id: str = "legacy-m10",
    route_revision: int = 1,
    route_fingerprint: str = "a" * 64,
) -> GovernedExecutionTarget:
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
        workspace_id=workspace_id,
        connection_id=CatalogConnectionId("warehouse-primary"),
        connector_kind=SourceConnectorKind.POSTGRESQL,
        dialect=SourceDialect.POSTGRESQL,
        route_revision=route_revision,
        route_fingerprint=route_fingerprint,
        expected_reader="schemabridge_reader",
        source_identity_fingerprint="c" * 64,
        catalog_identity_fingerprint="d" * 64,
        type_contract_fingerprint=postgres_type_contract_fingerprint(),
        cost_budget=budget,
        cost_budget_fingerprint=budget.fingerprint,
    )


def test_affected_context_blocks_before_compiler_guard_or_source_io() -> None:
    harness = _harness(MutableGate(eligible=False))

    with pytest.raises(SemanticChangeError) as captured:
        harness.execute.execute(harness.request)

    assert captured.value.code is SemanticChangeErrorCode.STALE_CONTEXT
    assert harness.compiler.calls == 0
    assert harness.guard.calls == 0
    assert harness.preview.calls == 0
    assert harness.rejections.calls == 0
    assert len(harness.gate.received) == 1


def test_current_context_proceeds_and_rechecks_before_each_source_operation() -> None:
    harness = _harness(MutableGate())

    prepared = harness.prepare.execute(harness.request)
    assert prepared.semantic_gate_assessment is not None
    assert prepared.semantic_dependencies is not None
    assert prepared.semantic_gate_assessment.eligible
    assert prepared.semantic_gate_assessment.status is SemanticChangeStatus.CURRENT
    assert prepared.semantic_gate_assessment.connection_id == CatalogConnectionId(
        "warehouse-primary"
    )
    assert (
        prepared.semantic_gate_assessment.dependencies_fingerprint
        == prepared.semantic_dependencies.fingerprint
    )
    result = harness.execute.execute_prepared(prepared)

    assert result.policy_status == "accepted"
    assert harness.compiler.calls == 1
    assert harness.guard.calls == 1
    assert harness.preview.calls == 1
    assert harness.rejections.calls == 1
    assert len(harness.gate.received) == 3
    assert all(item.scope == harness.scope for item in harness.gate.received)
    assert all(item.pointer_generation == 7 for item in harness.gate.received)
    assert all(item.pointer_fingerprint == POINTER_FINGERPRINT for item in harness.gate.received)
    assert len(harness.gate.received[0].mappings) == len(result.resolved_plan.selected_mappings)
    assert len(harness.gate.received[0].joins) == len(result.resolved_plan.selected_contracts)


def test_managed_plan_binds_and_rechecks_the_exact_public_target() -> None:
    resolver = MutableTargetResolver(_target())
    harness = _harness(MutableGate(), target_resolver=resolver)

    prepared = harness.prepare.execute(harness.request)

    assert prepared.resolved_plan.execution_target == resolver.target
    assert prepared.query.dialect is SourceDialect.POSTGRESQL
    assert prepared.query.target_fingerprint == resolver.target.fingerprint
    assert prepared.cost_assessment is not None
    assert resolver.calls == 2
    assert harness.cost_preflight is not None
    assert harness.cost_preflight.calls == 1

    harness.execute.execute_preview(prepared)

    assert resolver.calls == 3
    assert harness.cost_preflight.calls == 2
    assert harness.preview.calls == 1


def test_managed_presentation_compile_can_skip_source_cost_preflight() -> None:
    resolver = MutableTargetResolver(_target())
    harness = _harness(MutableGate(), target_resolver=resolver)
    resolved = harness.prepare.plan(harness.request)
    assert harness.cost_preflight is not None

    presentation = harness.prepare.refresh_and_validate(
        harness.request,
        resolved,
        assess_cost=False,
    )

    assert presentation.query.target_fingerprint == resolver.target.fingerprint
    assert presentation.cost_assessment is None
    assert harness.cost_preflight.calls == 0

    execution = harness.prepare.refresh_and_validate(harness.request, resolved)

    assert execution.cost_assessment is not None
    assert harness.cost_preflight.calls == 1


def test_route_rotation_invalidates_the_target_bound_plan_before_compilation_or_io() -> None:
    resolver = MutableTargetResolver(_target())
    harness = _harness(MutableGate(), target_resolver=resolver)
    prepared = harness.prepare.execute(harness.request)
    resolver.target = _target(route_revision=2, route_fingerprint="c" * 64)

    with pytest.raises(ConnectorTargetError) as captured:
        harness.execute.execute_preview(prepared)

    assert captured.value.code is ConnectorTargetErrorCode.ROUTE_STALE
    assert harness.preview.calls == 0


def test_cross_workspace_target_response_fails_before_compilation() -> None:
    resolver = MutableTargetResolver(_target(workspace_id="other-tenant"))
    harness = _harness(MutableGate(), target_resolver=resolver)

    with pytest.raises(ConnectorTargetError) as captured:
        harness.prepare.execute(harness.request)

    assert captured.value.code is ConnectorTargetErrorCode.INVALID_RESPONSE
    assert harness.compiler.calls == 0
    assert harness.guard.calls == 0


def test_gate_unavailability_fails_closed_before_compilation_or_source_io() -> None:
    harness = _harness(MutableGate(unavailable=True))

    with pytest.raises(SemanticChangeError) as captured:
        harness.execute.execute(harness.request)

    assert captured.value.code is SemanticChangeErrorCode.STALE_CONTEXT
    assert harness.compiler.calls == 0
    assert harness.guard.calls == 0
    assert harness.preview.calls == 0
    assert harness.rejections.calls == 0


def test_change_after_preparation_blocks_preview_and_rejected_source_inspection() -> None:
    harness = _harness(MutableGate())
    prepared = harness.prepare.execute(harness.request)
    harness.gate.eligible = False

    with pytest.raises(SemanticChangeError):
        harness.execute.execute_preview(prepared)
    with pytest.raises(SemanticChangeError):
        harness.execute.inspect_rejections(prepared)

    assert harness.compiler.calls == 1
    assert harness.guard.calls == 1
    assert harness.preview.calls == 0
    assert harness.rejections.calls == 0


def test_postgres_gate_projection_rejects_dependencies_without_a_connection() -> None:
    harness = _harness(MutableGate())
    harness.prepare.execute(harness.request)
    dependencies = harness.gate.received[0]

    assessment = _gate_assessment(
        dependencies,
        _eligible_projection_rows(dependencies, connection_ids=(None,)),
    )

    assert not assessment.eligible
    assert assessment.connection_id is None
    assert SemanticChangeKind.BINDING_MISSING in assessment.reason_codes


def test_postgres_gate_projection_returns_the_unique_validated_connection() -> None:
    harness = _harness(MutableGate())
    harness.prepare.execute(harness.request)
    dependencies = harness.gate.received[0]

    assessment = _gate_assessment(
        dependencies,
        _eligible_projection_rows(
            dependencies,
            connection_ids=("warehouse-primary",),
        ),
    )

    assert assessment.eligible
    assert assessment.connection_id == CatalogConnectionId("warehouse-primary")


def test_postgres_gate_projection_rejects_an_unsafe_connection_identifier() -> None:
    harness = _harness(MutableGate())
    harness.prepare.execute(harness.request)
    dependencies = harness.gate.received[0]

    assessment = _gate_assessment(
        dependencies,
        _eligible_projection_rows(
            dependencies,
            connection_ids=("postgresql://reader:secret@example/source",),
        ),
    )

    assert not assessment.eligible
    assert assessment.connection_id is None
    assert SemanticChangeKind.BINDING_MISSING in assessment.reason_codes


def test_postgres_gate_projection_rejects_dependencies_across_connections() -> None:
    harness = _harness(MutableGate())
    harness.prepare.execute(harness.request)
    dependencies = harness.gate.received[0]

    assessment = _gate_assessment(
        dependencies,
        _eligible_projection_rows(
            dependencies,
            connection_ids=("warehouse-primary", "warehouse-secondary"),
        ),
    )

    assert not assessment.eligible
    assert assessment.connection_id is None
    assert SemanticChangeKind.BINDING_AMBIGUOUS in assessment.reason_codes


def _eligible_projection_rows(
    dependencies: SemanticPlanDependencies,
    *,
    connection_ids: tuple[str | None, ...],
) -> list[tuple[object, ...]]:
    identities = [
        ("mapping", item.approval_decision_id, item.version) for item in dependencies.mappings
    ] + [("join", item.contract_id, item.version) for item in dependencies.joins]
    return [
        (
            kind,
            dependency_id,
            version,
            3,
            SemanticChangeStatus.CURRENT.value,
            True,
            True,
            True,
            True,
            connection_ids[index % len(connection_ids)],
        )
        for index, (kind, dependency_id, version) in enumerate(identities)
    ]
