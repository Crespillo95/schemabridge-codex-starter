"""Shared public-only fixtures for M28 browser unit and acceptance tests."""

from __future__ import annotations

from schemabridge.application.connectors import ConnectorTargetErrorCode
from schemabridge.domain.connectors import (
    QueryCostDecision,
    QueryCostRejectionCode,
    postgres_type_contract_fingerprint,
)
from schemabridge.domain.workflows import fingerprint_payload
from schemabridge.entrypoints.streamlit.connector_acceptance import (
    M28BrowserAcceptanceState,
    M28BrowserScenario,
    M28CostAssessmentEvidence,
    M28CostBudgetEvidence,
    M28ResultEvidence,
    M28ScenarioEvidence,
)


def public_m28_state() -> M28BrowserAcceptanceState:
    """Build the complete closed matrix without any connector capability."""

    alpha_budget = _budget("alpha-budget", maximum_cost="100000")
    beta_budget = _budget("beta-budget", maximum_cost="90000", timeout=1_750)
    rejected_budget = _budget("rejected-budget", maximum_cost="0")
    alpha_result = _result("sb_m28_a_reader_deadbeef", 2)
    beta_result = _result("sb_m28_b_reader_deadbeef", 3)
    accepted_alpha = _accepted_assessment("accepted-alpha", alpha_budget)
    accepted_beta = _accepted_assessment("accepted-beta", beta_budget)
    rejected_cost = M28CostAssessmentEvidence(
        decision=QueryCostDecision.REJECTED,
        rejection_codes=(QueryCostRejectionCode.TOTAL_COST_EXCEEDED,),
        total_cost="1.25",
        estimated_root_rows=2,
        plan_width=8,
        plan_node_count=2,
        plan_depth=2,
        response_bytes=128,
        read_only=True,
        explain_timeout_ms=rejected_budget.explain_timeout_ms,
        fingerprint=_fingerprint("assessment-cost-rejected"),
    )
    timeout = M28CostAssessmentEvidence(
        decision=QueryCostDecision.REJECTED,
        rejection_codes=(QueryCostRejectionCode.TIMEOUT,),
        explain_timeout_ms=alpha_budget.explain_timeout_ms,
        fingerprint=_fingerprint("assessment-timeout"),
    )

    scenarios = (
        _scenario(
            M28BrowserScenario.TENANT_A_ACCEPTED,
            revision=1,
            budget=alpha_budget,
            assessment=accepted_alpha,
            result=alpha_result,
            execution_allowed=True,
            target_label="target-alpha",
            connection_label="Warehouse primary A",
            blocker="Accepted preflight; execution requires explicit approval.",
        ),
        _scenario(
            M28BrowserScenario.TENANT_B_ACCEPTED,
            revision=7,
            budget=beta_budget,
            assessment=accepted_beta,
            result=beta_result,
            execution_allowed=True,
            target_label="target-beta",
            connection_label="Warehouse primary B",
            workspace_label="Workspace B",
            blocker="Accepted preflight; execution requires explicit approval.",
        ),
        _scenario(
            M28BrowserScenario.COST_REJECTED,
            revision=2,
            budget=rejected_budget,
            assessment=rejected_cost,
            target_label="target-cost-rejected",
            connection_label="Warehouse primary A",
            blocker="The governed total-cost budget rejected this plan before preview.",
        ),
        _scenario(
            M28BrowserScenario.ROUTE_DISABLED,
            revision=1,
            budget=alpha_budget,
            target_label="target-alpha",
            connection_label="Warehouse primary A",
            error=ConnectorTargetErrorCode.ROUTE_DISABLED.value,
            blocker="The current connector route is disabled; preview was not attempted.",
        ),
        _scenario(
            M28BrowserScenario.ROUTE_STALE,
            revision=1,
            budget=alpha_budget,
            target_label="target-alpha",
            connection_label="Warehouse primary A",
            error=ConnectorTargetErrorCode.ROUTE_STALE.value,
            blocker="The confirmed target does not match the current route.",
        ),
        _scenario(
            M28BrowserScenario.ROUTE_UNAVAILABLE,
            revision=1,
            budget=alpha_budget,
            target_label="target-alpha",
            connection_label="Warehouse primary A",
            error=ConnectorTargetErrorCode.UNAVAILABLE.value,
            blocker="The connector target is unavailable; preview was not attempted.",
        ),
        _scenario(
            M28BrowserScenario.EXPLAIN_TIMEOUT,
            revision=1,
            budget=alpha_budget,
            assessment=timeout,
            target_label="target-alpha",
            connection_label="Warehouse primary A",
            error=QueryCostRejectionCode.TIMEOUT.value,
            blocker="The independent EXPLAIN timeout elapsed before preview.",
        ),
        _scenario(
            M28BrowserScenario.UNSUPPORTED_DIALECT,
            revision=1,
            budget=alpha_budget,
            target_label="target-unsupported",
            connection_label="Catalog only source",
            connector_kind="catalog_only",
            dialect="snowflake",
            error=ConnectorTargetErrorCode.DIALECT_UNSUPPORTED.value,
            blocker="No executable capability exists for this dialect.",
        ),
        _scenario(
            M28BrowserScenario.ROTATED_AFTER_CONFIRMATION,
            revision=1,
            current_revision=2,
            budget=alpha_budget,
            target_label="target-alpha",
            connection_label="Warehouse primary A",
            error=ConnectorTargetErrorCode.ROUTE_STALE.value,
            blocker="Route rotation invalidated the confirmed target.",
        ),
    )
    return M28BrowserAcceptanceState.create(
        real_preflight_calls=4,
        query_shape_fingerprint=_fingerprint("query-shape"),
        scenarios=scenarios,
    )


def _budget(
    label: str,
    *,
    maximum_cost: str,
    timeout: int = 2_000,
) -> M28CostBudgetEvidence:
    return M28CostBudgetEvidence(
        fingerprint=_fingerprint(label),
        explain_timeout_ms=timeout,
        max_response_bytes=65_536,
        max_total_cost=maximum_cost,
        max_estimated_rows=100_000,
        max_plan_nodes=100,
        max_plan_depth=16,
        max_plan_width=4_096,
    )


def _accepted_assessment(
    label: str,
    budget: M28CostBudgetEvidence,
) -> M28CostAssessmentEvidence:
    return M28CostAssessmentEvidence(
        decision=QueryCostDecision.ACCEPTED,
        total_cost="1.25",
        estimated_root_rows=2,
        plan_width=8,
        plan_node_count=2,
        plan_depth=2,
        response_bytes=128,
        read_only=True,
        explain_timeout_ms=budget.explain_timeout_ms,
        fingerprint=_fingerprint(label),
    )


def _result(reader: str, count: int) -> M28ResultEvidence:
    facts = {
        "columns": ("approved_rows",),
        "rows": ((count,),),
        "database_user": reader,
        "transaction_read_only": True,
        "statement_timeout_ms": 2_000,
        "truncated": False,
    }
    return M28ResultEvidence(
        columns=("approved_rows",),
        rows=((count,),),
        row_count=1,
        preview_fingerprint=fingerprint_payload(facts),
        database_user=reader,
        transaction_read_only=True,
        statement_timeout_ms=2_000,
        truncated=False,
    )


def _scenario(
    scenario: M28BrowserScenario,
    *,
    revision: int,
    budget: M28CostBudgetEvidence,
    target_label: str,
    connection_label: str,
    blocker: str,
    assessment: M28CostAssessmentEvidence | None = None,
    result: M28ResultEvidence | None = None,
    execution_allowed: bool = False,
    workspace_label: str = "Workspace A",
    connector_kind: str = "postgresql",
    dialect: str = "postgresql",
    error: str | None = None,
    current_revision: int | None = None,
) -> M28ScenarioEvidence:
    return M28ScenarioEvidence(
        scenario=scenario,
        workspace_label=workspace_label,
        connection_label=connection_label,
        connection_id="warehouse-primary",
        connector_kind=connector_kind,
        dialect=dialect,
        route_revision=revision,
        current_route_revision=current_revision,
        route_fingerprint=_fingerprint(f"route-{revision}-{workspace_label}"),
        target_fingerprint=_fingerprint(target_label),
        type_contract_fingerprint=postgres_type_contract_fingerprint(),
        cost_budget=budget,
        cost_assessment=assessment,
        preflight_error_code=error,
        execution_allowed=execution_allowed,
        result=result,
        blocker=blocker,
    )


def _fingerprint(label: str) -> str:
    return fingerprint_payload({"m28_browser_fixture": label})
