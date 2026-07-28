"""Use cases for governed semantic planning, guarding, and bounded preview execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from schemabridge.application.connectors import (
    ConnectorTargetError,
    ConnectorTargetErrorCode,
)
from schemabridge.application.ports.connectors import ExecutionTargetResolverPort
from schemabridge.application.ports.planning import (
    RejectedSourceReportPort,
    SemanticPlanningContextPort,
)
from schemabridge.application.query_cost import AssessGovernedQueryCost
from schemabridge.application.query_execution import (
    QueryCompilerPort,
    QueryPreviewPort,
    QueryPreviewResult,
    SqlPolicyFinding,
    SqlPolicyGuardPort,
    ValidatedQuery,
)
from schemabridge.application.semantic_change import (
    AssertSemanticContextCurrent,
    SemanticChangeError,
    SemanticChangeErrorCode,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import GovernedExecutionTarget, QueryCostAssessment
from schemabridge.domain.plans import ParameterScalar
from schemabridge.domain.request_context import ValidatedAnalyticalRequest
from schemabridge.domain.resolution import (
    RejectedSourceReport,
    ResolutionErrorCode,
    ResolutionLimits,
    ResolvedSemanticPlan,
    SemanticResolutionError,
    resolve_semantic_request,
    resolved_semantic_plan_fingerprint,
)
from schemabridge.domain.semantic_change import (
    GovernedJoinRef,
    GovernedMappingRef,
    SemanticContextGateAssessment,
    SemanticPlanDependencies,
)
from schemabridge.domain.semantic_registry import (
    SemanticRegistryScope,
    semantic_registry_scope_fingerprint,
)


@dataclass(frozen=True, slots=True)
class PlanSemanticRequest:
    context: SemanticPlanningContextPort
    limits: ResolutionLimits

    def execute(self, request: ValidatedAnalyticalRequest) -> ResolvedSemanticPlan:
        loaded = self.context.load()
        resolved = resolve_semantic_request(
            request,
            loaded.registry,
            self.limits,
        )
        if loaded.activation_generation is None:
            return resolved
        assert loaded.active_pointer_fingerprint is not None
        return resolved.model_copy(
            update={
                "activation_generation": loaded.activation_generation,
                "active_pointer_fingerprint": loaded.active_pointer_fingerprint,
                "active_scope_fingerprint": semantic_registry_scope_fingerprint(loaded.scope),
            }
        )


@dataclass(frozen=True, slots=True)
class GovernedPreparedQuery:
    resolved_plan: ResolvedSemanticPlan
    query: ValidatedQuery
    semantic_dependencies: SemanticPlanDependencies | None = None
    semantic_gate_assessment: SemanticContextGateAssessment | None = None
    cost_assessment: QueryCostAssessment | None = None
    policy_status: str = "accepted"
    policy_findings: tuple[SqlPolicyFinding, ...] = ()


@dataclass(frozen=True, slots=True)
class PrepareGovernedRequest:
    planner: PlanSemanticRequest
    compiler: QueryCompilerPort
    guard: SqlPolicyGuardPort
    semantic_gate: AssertSemanticContextCurrent | None = None
    semantic_scope: SemanticRegistryScope | None = None
    target_resolver: ExecutionTargetResolverPort | None = None
    cost_preflight: AssessGovernedQueryCost | None = None

    def __post_init__(self) -> None:
        if (self.semantic_gate is None) != (self.semantic_scope is None):
            raise ValueError("semantic execution gate and scope must be configured together")
        if self.target_resolver is not None and self.semantic_gate is None:
            raise ValueError("execution target resolution requires the semantic execution gate")
        if (self.target_resolver is None) != (self.cost_preflight is None):
            raise ValueError(
                "managed execution target resolution and cost preflight must be configured together"
            )

    def execute(self, request: ValidatedAnalyticalRequest) -> GovernedPreparedQuery:
        return self.validate_resolved(self.plan(request))

    def plan(self, request: ValidatedAnalyticalRequest) -> ResolvedSemanticPlan:
        """Resolve semantics and bind the exact current public target before fingerprinting."""

        resolved = self.planner.execute(request)
        if self.target_resolver is None:
            return resolved
        _dependencies, assessment = self._assess_semantic_current(resolved)
        if assessment is None or assessment.connection_id is None:
            raise SemanticChangeError(
                SemanticChangeErrorCode.STALE_CONTEXT,
                "semantic context is unavailable or stale",
            )
        assert self.semantic_scope is not None
        target = self._resolve_current_target(
            workspace_id=self.semantic_scope.workspace_id,
            connection_id=assessment.connection_id,
        )
        return resolved.model_copy(update={"execution_target": target})

    def refresh_and_validate(
        self,
        request: ValidatedAnalyticalRequest,
        expected: ResolvedSemanticPlan,
        *,
        assess_cost: bool = True,
    ) -> GovernedPreparedQuery:
        """Re-resolve and compile; optionally defer source-I/O cost admission.

        Execution paths keep the default and therefore repeat the bounded cost
        preflight. Read-only presentation paths may set ``assess_cost=False`` to
        rebuild deterministic SQL and policy facts without issuing ``EXPLAIN``.
        """

        try:
            current = self.plan(request)
        except SemanticResolutionError as error:
            raise SemanticResolutionError(
                ResolutionErrorCode.STALE_REGISTRY,
                "the active semantic registry changed; start a newly confirmed workflow",
            ) from error
        if resolved_semantic_plan_fingerprint(current) != resolved_semantic_plan_fingerprint(
            expected
        ):
            raise SemanticResolutionError(
                ResolutionErrorCode.STALE_REGISTRY,
                "the active semantic registry changed; start a newly confirmed workflow",
            )
        return self.validate_resolved(current, assess_cost=assess_cost)

    def validate_resolved(
        self,
        resolved: ResolvedSemanticPlan,
        *,
        assess_cost: bool = True,
    ) -> GovernedPreparedQuery:
        """Compile and guard one versioned plan, with optional source cost I/O."""

        dependencies, gate_assessment = self._assess_semantic_current(resolved)
        self._assert_execution_target_current(resolved, gate_assessment)
        target = resolved.execution_target
        if target is None:
            compiled = self.compiler.compile(
                resolved.query_plan,
                max_preview_rows=resolved.query_policy.max_preview_rows,
            )
            query = self.guard.validate(compiled, resolved.query_policy)
        else:
            compiled = self.compiler.compile(
                resolved.query_plan,
                max_preview_rows=resolved.query_policy.max_preview_rows,
                target=target,
            )
            query = self.guard.validate(
                compiled,
                resolved.query_policy,
                target=target,
            )
        cost_assessment = (
            self.cost_preflight.execute(query, target)
            if assess_cost and self.cost_preflight is not None and target is not None
            else None
        )
        return GovernedPreparedQuery(
            resolved_plan=resolved,
            query=query,
            semantic_dependencies=dependencies,
            semantic_gate_assessment=gate_assessment,
            cost_assessment=cost_assessment,
        )

    def reassess_cost(
        self,
        prepared: GovernedPreparedQuery,
    ) -> QueryCostAssessment | None:
        """Repeat cost admission on the current target immediately before preview."""

        self.assert_semantic_current(
            prepared.resolved_plan,
            expected=prepared.semantic_dependencies,
        )
        target = prepared.resolved_plan.execution_target
        if target is None:
            return None
        if self.cost_preflight is None or prepared.cost_assessment is None:
            raise ValueError("managed preview lacks a query cost preflight")
        assessment = self.cost_preflight.execute(prepared.query, target)
        self.cost_preflight.require_accepted(assessment)
        return assessment

    def assert_semantic_current(
        self,
        resolved: ResolvedSemanticPlan,
        *,
        expected: SemanticPlanDependencies | None = None,
    ) -> SemanticPlanDependencies | None:
        """Recheck exact selected evidence before compilation or protected source I/O."""

        dependencies, assessment = self._assess_semantic_current(
            resolved,
            expected=expected,
        )
        self._assert_execution_target_current(resolved, assessment)
        return dependencies

    def _assert_execution_target_current(
        self,
        resolved: ResolvedSemanticPlan,
        assessment: SemanticContextGateAssessment | None,
    ) -> None:
        if self.target_resolver is None:
            if resolved.execution_target is not None:
                raise ConnectorTargetError(
                    ConnectorTargetErrorCode.INVALID_RESPONSE,
                    "a governed execution target has no configured resolver",
                )
            return
        if (
            assessment is None
            or assessment.connection_id is None
            or resolved.execution_target is None
        ):
            raise ConnectorTargetError(
                ConnectorTargetErrorCode.INVALID_RESPONSE,
                "the managed resolved plan has no current execution target",
            )
        assert self.semantic_scope is not None
        expected = resolved.execution_target
        if (
            expected.workspace_id != self.semantic_scope.workspace_id
            or expected.connection_id != assessment.connection_id
        ):
            raise ConnectorTargetError(
                ConnectorTargetErrorCode.ROUTE_STALE,
                "the governed execution target no longer matches semantic evidence",
            )
        current = self._resolve_current_target(
            workspace_id=self.semantic_scope.workspace_id,
            connection_id=assessment.connection_id,
        )
        if current != expected:
            raise ConnectorTargetError(
                ConnectorTargetErrorCode.ROUTE_STALE,
                "the governed execution target changed; start a newly confirmed workflow",
            )

    def _resolve_current_target(
        self,
        *,
        workspace_id: str,
        connection_id: CatalogConnectionId,
    ) -> GovernedExecutionTarget:
        assert self.target_resolver is not None
        try:
            target = self.target_resolver.resolve_current(
                workspace_id=workspace_id,
                connection_id=connection_id,
            )
        except ConnectorTargetError:
            raise
        except Exception as error:
            raise ConnectorTargetError(
                ConnectorTargetErrorCode.UNAVAILABLE,
                "the governed execution target is unavailable",
            ) from error
        if type(target) is not GovernedExecutionTarget:
            raise ConnectorTargetError(
                ConnectorTargetErrorCode.INVALID_RESPONSE,
                "the connector resolver returned an invalid execution target",
            )
        if target.workspace_id != workspace_id or target.connection_id != connection_id:
            raise ConnectorTargetError(
                ConnectorTargetErrorCode.INVALID_RESPONSE,
                "the connector resolver returned another execution target",
            )
        return target

    def _assess_semantic_current(
        self,
        resolved: ResolvedSemanticPlan,
        *,
        expected: SemanticPlanDependencies | None = None,
    ) -> tuple[
        SemanticPlanDependencies | None,
        SemanticContextGateAssessment | None,
    ]:
        """Return only the exact server-side evidence actually checked by the gate."""

        if self.semantic_gate is None:
            return None, None
        assert self.semantic_scope is not None
        dependencies = semantic_plan_dependencies(resolved, self.semantic_scope)
        if expected is not None and dependencies != expected:
            raise SemanticChangeError(
                SemanticChangeErrorCode.STALE_CONTEXT,
                "semantic context is unavailable or stale",
            )
        assessment = self.semantic_gate.execute(dependencies)
        return dependencies, assessment


@dataclass(frozen=True, slots=True)
class GovernedQueryResult:
    resolved_plan: ResolvedSemanticPlan
    sql: str
    parameters: tuple[ParameterScalar, ...]
    policy_status: str
    policy_findings: tuple[SqlPolicyFinding, ...]
    preview: QueryPreviewResult
    rejected_sources: RejectedSourceReport

    def as_dict(self) -> dict[str, object]:
        return {
            "resolved_plan": self.resolved_plan.model_dump(mode="json"),
            "sql": self.sql,
            "parameters": list(self.parameters),
            "policy": {
                "status": self.policy_status,
                "findings": [
                    {"code": finding.code.value, "message": finding.message}
                    for finding in self.policy_findings
                ],
            },
            "preview": self.preview.as_dict(),
            "rejected_sources": self.rejected_sources.model_dump(mode="json"),
        }


@dataclass(frozen=True, slots=True)
class ExecuteGovernedRequest:
    prepare: PrepareGovernedRequest
    executor: QueryPreviewPort
    rejection_reporter: RejectedSourceReportPort

    def execute(self, request: ValidatedAnalyticalRequest) -> GovernedQueryResult:
        return self.execute_prepared(self.prepare.execute(request))

    def execute_preview(self, prepared: GovernedPreparedQuery) -> QueryPreviewResult:
        """Execute only an independently guarded preview."""

        self.prepare.reassess_cost(prepared)
        target = prepared.resolved_plan.execution_target
        if target is None:
            return self.executor.execute(prepared.query)
        return self.executor.execute(prepared.query, target=target)

    def inspect_rejections(
        self,
        prepared: GovernedPreparedQuery,
        *,
        should_continue: Callable[[], bool] | None = None,
    ) -> RejectedSourceReport:
        """Inspect governed rejection checks separately from preview execution."""

        self.prepare.assert_semantic_current(
            prepared.resolved_plan,
            expected=prepared.semantic_dependencies,
        )
        target = prepared.resolved_plan.execution_target
        if should_continue is None:
            if target is None:
                return self.rejection_reporter.inspect(
                    prepared.resolved_plan.rejection_checks,
                    statement_timeout_ms=prepared.query.statement_timeout_ms,
                )
            return self.rejection_reporter.inspect(
                prepared.resolved_plan.rejection_checks,
                statement_timeout_ms=prepared.query.statement_timeout_ms,
                target=target,
            )
        if target is None:
            return self.rejection_reporter.inspect(
                prepared.resolved_plan.rejection_checks,
                statement_timeout_ms=prepared.query.statement_timeout_ms,
                should_continue=should_continue,
            )
        return self.rejection_reporter.inspect(
            prepared.resolved_plan.rejection_checks,
            statement_timeout_ms=prepared.query.statement_timeout_ms,
            should_continue=should_continue,
            target=target,
        )

    def execute_prepared(self, prepared: GovernedPreparedQuery) -> GovernedQueryResult:
        """Execute one exact prepared plan without reloading semantic context."""

        preview = self.execute_preview(prepared)
        rejected = self.inspect_rejections(prepared)
        return self.compose_result(prepared, preview, rejected)

    @staticmethod
    def compose_result(
        prepared: GovernedPreparedQuery,
        preview: QueryPreviewResult,
        rejected: RejectedSourceReport,
    ) -> GovernedQueryResult:
        """Assemble the complete typed outcome from independently observed results."""

        return GovernedQueryResult(
            resolved_plan=prepared.resolved_plan,
            sql=prepared.query.sql,
            parameters=tuple(prepared.query.parameters),
            policy_status=prepared.policy_status,
            policy_findings=prepared.policy_findings,
            preview=preview,
            rejected_sources=rejected,
        )


def semantic_plan_dependencies(
    resolved: ResolvedSemanticPlan,
    scope: SemanticRegistryScope,
) -> SemanticPlanDependencies:
    """Project one resolved plan into exact governed evidence dependencies."""

    if resolved.activation_generation is None or resolved.active_pointer_fingerprint is None:
        raise SemanticChangeError(
            SemanticChangeErrorCode.STALE_CONTEXT,
            "semantic context is unavailable or stale",
        )
    try:
        mappings = tuple(
            GovernedMappingRef(
                logical_field=item.mapping.logical_field,
                physical_field=item.mapping.physical_field,
                version=item.mapping.version,
                approval_decision_id=_required_semantic_decision(item.approval_decision_id),
                physical_type=item.physical_type,
            )
            for item in resolved.selected_mappings
        )
        joins = tuple(
            GovernedJoinRef(
                contract_id=item.id,
                version=item.version,
                approval_decision_id=_required_semantic_decision(item.approval_decision_id),
                left_field=item.left_key.physical_field,
                right_field=item.right_key.physical_field,
                cardinality=item.cardinality,
                fanout_policy=item.fanout_policy,
            )
            for item in resolved.selected_contracts
        )
        return SemanticPlanDependencies.create(
            scope=scope,
            pointer_generation=resolved.activation_generation,
            pointer_fingerprint=resolved.active_pointer_fingerprint,
            registry_version=resolved.context_version,
            registry_fingerprint=resolved.context_fingerprint,
            mappings=mappings,
            joins=joins,
        )
    except (TypeError, ValueError) as error:
        raise SemanticChangeError(
            SemanticChangeErrorCode.STALE_CONTEXT,
            "semantic context is unavailable or stale",
        ) from error


def _required_semantic_decision(value: str | None) -> str:
    if value is None:
        raise ValueError("governed semantic dependency lacks an approval decision")
    return value
