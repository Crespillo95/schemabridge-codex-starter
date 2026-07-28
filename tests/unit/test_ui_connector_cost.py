from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from schemabridge.application.connectors import (
    ConnectorTargetError,
    ConnectorTargetErrorCode,
)
from schemabridge.application.governed_execution import (
    GovernedPreparedQuery,
    PrepareGovernedRequest,
)
from schemabridge.application.query_cost import QueryCostErrorCode
from schemabridge.application.ui_view_models import JudgeUiView, JudgeUiViewFactory
from schemabridge.bootstrap import build_streamlit_ui_service
from schemabridge.config import Settings
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    QueryCostAssessment,
    QueryCostBudget,
    QueryCostDecision,
    QueryCostRejectionCode,
    SourceConnectorKind,
    SourceDialect,
    postgres_type_contract_fingerprint,
)
from schemabridge.domain.intents import IntentAlternativeId
from schemabridge.domain.resolution import resolved_semantic_plan_fingerprint
from schemabridge.domain.workflows import (
    AgentWorkflowDraft,
    WorkflowOperation,
    WorkflowStage,
    WorkflowTraceEvent,
    WorkflowTraceFact,
    WorkflowTraceStatus,
)
from schemabridge.entrypoints.streamlit import components

ROUTE_FINGERPRINT = "a" * 64
TYPE_CONTRACT_FINGERPRINT = postgres_type_contract_fingerprint()
SOURCE_IDENTITY_FINGERPRINT = "c" * 64
CATALOG_IDENTITY_FINGERPRINT = "d" * 64


def _budget() -> QueryCostBudget:
    return QueryCostBudget(
        explain_timeout_ms=750,
        max_response_bytes=65_536,
        max_total_cost=Decimal("10000"),
        max_estimated_rows=50_000,
        max_plan_nodes=500,
        max_plan_depth=24,
        max_plan_width=8_192,
    )


def _target() -> GovernedExecutionTarget:
    budget = _budget()
    return GovernedExecutionTarget(
        workspace_id="workspace_managed",
        connection_id=CatalogConnectionId("warehouse_primary"),
        connector_kind=SourceConnectorKind.POSTGRESQL,
        dialect=SourceDialect.POSTGRESQL,
        route_revision=7,
        route_fingerprint=ROUTE_FINGERPRINT,
        expected_reader="managed_reader",
        source_identity_fingerprint=SOURCE_IDENTITY_FINGERPRINT,
        catalog_identity_fingerprint=CATALOG_IDENTITY_FINGERPRINT,
        type_contract_fingerprint=TYPE_CONTRACT_FINGERPRINT,
        cost_budget=budget,
        cost_budget_fingerprint=budget.fingerprint,
    )


def _assessment(
    target: GovernedExecutionTarget,
    *,
    decision: QueryCostDecision = QueryCostDecision.ACCEPTED,
) -> QueryCostAssessment:
    rejected = decision is QueryCostDecision.REJECTED
    return QueryCostAssessment(
        decision=decision,
        rejection_codes=((QueryCostRejectionCode.TOTAL_COST_EXCEEDED,) if rejected else ()),
        total_cost=Decimal("12000") if rejected else Decimal("42.5"),
        estimated_root_rows=8,
        plan_width=96,
        plan_node_count=4,
        plan_depth=3,
        response_bytes=512,
        observed_reader="managed_reader",
        read_only=True,
        explain_timeout_ms=target.cost_budget.explain_timeout_ms,
        target_fingerprint=target.fingerprint,
        budget_fingerprint=target.cost_budget_fingerprint,
        cost_budget=target.cost_budget,
    )


@dataclass(frozen=True, slots=True)
class _ExplodingCostPreflight:
    def execute(self) -> None:
        raise AssertionError("UI construction attempted source cost preflight")


@dataclass(frozen=True, slots=True)
class _StaticPrepare:
    prepared: GovernedPreparedQuery
    forbidden_cost_preflight: _ExplodingCostPreflight
    error: Exception | None = None

    def refresh_and_validate(
        self,
        request: object,
        expected: object,
        *,
        assess_cost: bool = True,
    ) -> GovernedPreparedQuery:
        del request, expected
        if assess_cost:
            self.forbidden_cost_preflight.execute()
        if self.error is not None:
            raise self.error
        return self.prepared


def _append_cost_validation_trace(
    draft: AgentWorkflowDraft,
    *,
    plan_fingerprint: str,
    assessment: QueryCostAssessment | None,
    error_code: str | None,
) -> AgentWorkflowDraft:
    started = WorkflowTraceEvent(
        sequence=len(draft.trace) + 1,
        stage=WorkflowStage.PLAN_READY,
        operation=WorkflowOperation.SQL_VALIDATION,
        status=WorkflowTraceStatus.STARTED,
        occurred_at=draft.updated_at,
        input_refs=(plan_fingerprint,),
    )
    facts: list[WorkflowTraceFact] = []
    if error_code is not None:
        facts.append(WorkflowTraceFact(key="error_code", value=error_code))
    if assessment is not None:
        facts.extend(
            (
                WorkflowTraceFact(
                    key="cost_decision",
                    value=assessment.decision.value,
                ),
                WorkflowTraceFact(
                    key="cost_assessment",
                    value=assessment.fingerprint,
                ),
                WorkflowTraceFact(
                    key="estimated_total_cost",
                    value=str(assessment.total_cost),
                ),
                WorkflowTraceFact(
                    key="estimated_root_rows",
                    value=str(assessment.estimated_root_rows),
                ),
                WorkflowTraceFact(
                    key="plan_node_count",
                    value=str(assessment.plan_node_count),
                ),
            )
        )
    completed = WorkflowTraceEvent(
        sequence=started.sequence + 1,
        stage=(WorkflowStage.VALIDATED if error_code is None else WorkflowStage.PLAN_READY),
        operation=WorkflowOperation.SQL_VALIDATION,
        status=(
            WorkflowTraceStatus.SUCCEEDED if error_code is None else WorkflowTraceStatus.FAILED
        ),
        occurred_at=draft.updated_at,
        facts=tuple(facts),
        duration_ms=0,
    )
    return draft.model_copy(update={"trace": (*draft.trace, started, completed)})


def _managed_view(
    tmp_path: Path,
    *,
    decision: QueryCostDecision = QueryCostDecision.ACCEPTED,
    error_code: QueryCostErrorCode | None = None,
    connector_error_code: ConnectorTargetErrorCode | None = None,
    forbidden_cost_preflight: _ExplodingCostPreflight | None = None,
    persist_cost_decision: bool = True,
) -> JudgeUiView:
    assert error_code is None or connector_error_code is None
    settings = Settings.model_validate(
        {
            "SCHEMABRIDGE_DRAFT_STORE_PATH": tmp_path / "m28-ui.db",
            "SCHEMABRIDGE_JUDGE_EXECUTION": "recorded",
        }
    )
    service = build_streamlit_ui_service(settings=settings, execution_kind="recorded")
    workflow_id = f"m28-ui-{decision.value}-{error_code or connector_error_code or 'ok'}"
    service.start_demo(workflow_id)
    service.confirm_intent(
        workflow_id,
        IntentAlternativeId.COUNT_DISTINCT_CUSTOMERS,
    )
    draft = service.orchestrator_factory().inspect(workflow_id)
    assert draft.validated_request is not None
    assert draft.resolved_plan is not None
    assert draft.checkpoint is not None
    factory = cast(JudgeUiViewFactory, service.views)
    base = factory.prepare.refresh_and_validate(
        draft.validated_request,
        draft.resolved_plan,
    )

    target = _target()
    managed_plan = draft.resolved_plan.model_copy(
        update={
            "activation_generation": 1,
            "active_pointer_fingerprint": "c" * 64,
            "active_scope_fingerprint": "d" * 64,
            "execution_target": target,
        }
    )
    assessment = _assessment(target, decision=decision)
    managed_query = replace(
        base.query,
        dialect=SourceDialect.POSTGRESQL,
        target_fingerprint=target.fingerprint,
    )
    prepared = GovernedPreparedQuery(
        resolved_plan=managed_plan,
        query=managed_query,
        cost_assessment=None,
    )
    plan_fingerprint = resolved_semantic_plan_fingerprint(managed_plan)
    managed_draft = draft.model_copy(
        update={
            "resolved_plan": managed_plan,
            "plan_fingerprint": plan_fingerprint,
            "checkpoint": draft.checkpoint.model_copy(update={"fingerprint": plan_fingerprint}),
        }
    )
    persisted_error = (
        error_code.value
        if error_code is not None
        else (
            connector_error_code.value
            if connector_error_code is not None
            else (
                QueryCostErrorCode.REJECTED.value
                if decision is QueryCostDecision.REJECTED
                else None
            )
        )
    )
    if persist_cost_decision:
        managed_draft = _append_cost_validation_trace(
            managed_draft,
            plan_fingerprint=plan_fingerprint,
            assessment=None if connector_error_code is not None else assessment,
            error_code=persisted_error,
        )
    error: Exception | None = (
        ConnectorTargetError(
            connector_error_code,
            "sanitized connector failure",
        )
        if connector_error_code is not None
        else None
    )
    static = _StaticPrepare(
        prepared=prepared,
        forbidden_cost_preflight=(forbidden_cost_preflight or _ExplodingCostPreflight()),
        error=error,
    )
    return replace(
        factory,
        prepare=cast(PrepareGovernedRequest, static),
    ).from_draft(managed_draft)


def test_managed_ui_projects_public_target_and_accepted_cost_without_secrets(
    tmp_path: Path,
) -> None:
    view = _managed_view(tmp_path)

    assert view.query is not None
    query = view.query
    assert query.can_execute is True
    assert query.execution_target is not None
    assert query.execution_target.connection_id == "warehouse_primary"
    assert query.execution_target.dialect == "postgresql"
    assert query.execution_target.route_revision == 7
    assert query.execution_target.route_fingerprint == ("a" * 12) + "…"
    assert query.cost_budget is not None
    assert query.cost_budget.max_total_cost == "10000"
    assert query.cost_assessment is not None
    assert query.cost_assessment.decision == "accepted"
    assert query.cost_assessment.total_cost == "42.5"
    assert query.cost_assessment.rejection_codes == ()
    assert query.preflight_error_code is None

    assert query.plan_json is not None
    payload = json.loads(query.plan_json)
    assert payload["schema_version"] == 2
    assert payload["execution_target"] == {
        "connection_id": "warehouse_primary",
        "connector_kind": "postgresql",
        "dialect": "postgresql",
        "route_revision": 7,
        "route_fingerprint": ("a" * 12) + "…",
        "target_fingerprint": query.execution_target.target_fingerprint,
        "type_contract_fingerprint": TYPE_CONTRACT_FINGERPRINT[:12] + "…",
    }
    assert payload["query_cost"]["assessment"]["decision"] == "accepted"
    assert payload["query_cost"]["assessment"]["read_only"] is None
    assert payload["query_cost"]["assessment"]["plan_width"] is None
    assert payload["query_cost"]["assessment"]["plan_depth"] is None
    assert payload["query_cost"]["assessment"]["response_bytes"] is None

    serialized = json.dumps(asdict(view), default=str)
    for forbidden in (
        ROUTE_FINGERPRINT,
        TYPE_CONTRACT_FINGERPRINT,
        SOURCE_IDENTITY_FINGERPRINT,
        CATALOG_IDENTITY_FINGERPRINT,
        _assessment(_target()).fingerprint,
        "workspace_managed",
        "managed_reader",
        "postgresql://",
        "secret_binding",
        "Relation Name",
    ):
        assert forbidden not in serialized


def test_rejected_cost_is_visible_and_never_exposes_execute(
    tmp_path: Path,
) -> None:
    view = _managed_view(tmp_path, decision=QueryCostDecision.REJECTED)

    assert view.query is not None
    query = view.query
    assert query.can_execute is False
    assert query.cost_assessment is not None
    assert query.cost_assessment.decision == "rejected"
    assert query.cost_assessment.rejection_codes == ("query_cost_rejected",)
    assert query.preflight_error_code == "query_cost_rejected"
    assert query.execution_blockers == (
        "Governed connector/cost preflight blocked execution: query_cost_rejected.",
    )
    assert query.plan_json is not None
    payload = json.loads(query.plan_json)
    assert payload["query_cost"]["assessment"]["rejection_codes"] == ["query_cost_rejected"]


def test_cost_timeout_is_sanitized_and_never_exposes_execute(
    tmp_path: Path,
) -> None:
    view = _managed_view(
        tmp_path,
        decision=QueryCostDecision.REJECTED,
        error_code=QueryCostErrorCode.TIMEOUT,
    )

    assert view.query is not None
    query = view.query
    assert query.can_execute is False
    assert query.sql is not None
    assert query.preflight_error_code == "query_cost_timeout"
    assert query.cost_assessment is not None
    assert query.execution_blockers == (
        "Governed connector/cost preflight blocked execution: query_cost_timeout.",
    )


def test_disabled_route_is_visible_without_assessment_or_execute(
    tmp_path: Path,
) -> None:
    view = _managed_view(
        tmp_path,
        connector_error_code=ConnectorTargetErrorCode.ROUTE_DISABLED,
    )

    assert view.query is not None
    query = view.query
    assert query.can_execute is False
    assert query.sql is None
    assert query.execution_target is not None
    assert query.execution_target.connection_id == "warehouse_primary"
    assert query.cost_budget is not None
    assert query.cost_assessment is None
    assert query.preflight_error_code == "connector_route_disabled"
    assert query.plan_json is not None
    payload = json.loads(query.plan_json)
    assert payload["query_cost"]["error_code"] == "connector_route_disabled"
    assert "assessment" not in payload["query_cost"]


class _MetricColumn:
    def __init__(self, rendered: list[str]) -> None:
        self.rendered = rendered

    def metric(self, label: str, value: object) -> None:
        self.rendered.append(f"{label}={value}")


class _StreamlitRecorder:
    def __init__(self) -> None:
        self.rendered: list[str] = []

    def columns(self, count: int) -> tuple[_MetricColumn, ...]:
        return tuple(_MetricColumn(self.rendered) for _ in range(count))

    def caption(self, value: object) -> None:
        self.rendered.append(str(value))

    def success(self, value: object) -> None:
        self.rendered.append(str(value))

    def warning(self, value: object) -> None:
        self.rendered.append(str(value))

    def error(self, value: object) -> None:
        self.rendered.append(str(value))


def test_streamlit_component_renders_accepted_preflight_without_secret_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view = _managed_view(tmp_path)
    assert view.query is not None
    recorder = _StreamlitRecorder()
    monkeypatch.setattr(components, "st", recorder)

    components._render_connector_cost_preflight(view.query)

    rendered = " ".join(recorder.rendered)
    assert "Connection=warehouse_primary" in rendered
    assert "Dialect=postgresql" in rendered
    assert "Route revision=r7" in rendered
    assert "Cost decision=Accepted" in rendered
    assert "Persisted bounded EXPLAIN accepted · ANALYZE disabled" in rendered
    assert "read_only" not in rendered
    assert "42.5" in rendered
    assert "managed_reader" not in rendered
    assert "postgresql://" not in rendered


def test_streamlit_component_renders_only_sanitized_connector_and_cost_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view = _managed_view(tmp_path, decision=QueryCostDecision.REJECTED)
    assert view.query is not None
    recorder = _StreamlitRecorder()
    monkeypatch.setattr(components, "st", recorder)

    components._render_connector_cost_preflight(view.query)

    rendered = " ".join(recorder.rendered)
    assert "Connection=warehouse_primary" in rendered
    assert "Dialect=postgresql" in rendered
    assert "Route revision=r7" in rendered
    assert "Cost decision=Rejected" in rendered
    assert "Cost preflight rejected: query_cost_rejected." in rendered
    assert "ANALYZE" not in rendered
    for forbidden in (
        ROUTE_FINGERPRINT,
        TYPE_CONTRACT_FINGERPRINT,
        SOURCE_IDENTITY_FINGERPRINT,
        CATALOG_IDENTITY_FINGERPRINT,
        "workspace_managed",
        "managed_reader",
        "postgresql://",
        "Relation Name",
    ):
        assert forbidden not in rendered


def test_ui_build_never_invokes_source_cost_preflight(tmp_path: Path) -> None:
    forbidden = _ExplodingCostPreflight()

    view = _managed_view(
        tmp_path,
        forbidden_cost_preflight=forbidden,
    )

    assert view.query is not None
    assert view.query.sql is not None
    assert view.query.cost_assessment is not None
    assert view.query.can_execute is True


def test_managed_execute_requires_persisted_accepted_cost_decision(
    tmp_path: Path,
) -> None:
    view = _managed_view(tmp_path, persist_cost_decision=False)

    assert view.query is not None
    assert view.query.sql is not None
    assert view.query.cost_assessment is None
    assert view.query.can_execute is False
    assert view.query.execution_blockers == (
        "Governed query cost preflight rejected execution: assessment_missing.",
    )
