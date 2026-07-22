"""Use cases for governed semantic planning, guarding, and bounded preview execution."""

from __future__ import annotations

from dataclasses import dataclass

from schemabridge.application.ports.planning import (
    RejectedSourceReportPort,
    SemanticPlanningContextPort,
)
from schemabridge.application.query_execution import (
    QueryCompilerPort,
    QueryPreviewPort,
    QueryPreviewResult,
    SqlPolicyFinding,
    SqlPolicyGuardPort,
    ValidatedQuery,
)
from schemabridge.domain.plans import ParameterScalar
from schemabridge.domain.request_context import ValidatedAnalyticalRequest
from schemabridge.domain.resolution import (
    RejectedSourceReport,
    ResolutionLimits,
    ResolvedSemanticPlan,
    resolve_semantic_request,
)


@dataclass(frozen=True, slots=True)
class PlanSemanticRequest:
    context: SemanticPlanningContextPort
    limits: ResolutionLimits

    def execute(self, request: ValidatedAnalyticalRequest) -> ResolvedSemanticPlan:
        return resolve_semantic_request(request, self.context.load(), self.limits)


@dataclass(frozen=True, slots=True)
class GovernedPreparedQuery:
    resolved_plan: ResolvedSemanticPlan
    query: ValidatedQuery
    policy_status: str = "accepted"
    policy_findings: tuple[SqlPolicyFinding, ...] = ()


@dataclass(frozen=True, slots=True)
class PrepareGovernedRequest:
    planner: PlanSemanticRequest
    compiler: QueryCompilerPort
    guard: SqlPolicyGuardPort

    def execute(self, request: ValidatedAnalyticalRequest) -> GovernedPreparedQuery:
        return self.validate_resolved(self.planner.execute(request))

    def validate_resolved(self, resolved: ResolvedSemanticPlan) -> GovernedPreparedQuery:
        """Compile and independently guard one already-versioned resolved plan."""

        compiled = self.compiler.compile(
            resolved.query_plan,
            max_preview_rows=resolved.query_policy.max_preview_rows,
        )
        query = self.guard.validate(compiled, resolved.query_policy)
        return GovernedPreparedQuery(resolved_plan=resolved, query=query)


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

        return self.executor.execute(prepared.query)

    def inspect_rejections(self, prepared: GovernedPreparedQuery) -> RejectedSourceReport:
        """Inspect governed rejection checks separately from preview execution."""

        return self.rejection_reporter.inspect(
            prepared.resolved_plan.rejection_checks,
            statement_timeout_ms=prepared.query.statement_timeout_ms,
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
