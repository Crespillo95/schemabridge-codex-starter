"""Typed, secret-safe view models for the judge-facing interface."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from schemabridge.application.candidate_engine import CandidateGenerationReport
from schemabridge.application.connectors import ConnectorTargetError
from schemabridge.application.governed_execution import PrepareGovernedRequest
from schemabridge.application.query_execution import (
    QueryCompilationError,
    SqlPolicyViolation,
)
from schemabridge.application.semantic_change import SemanticChangeError
from schemabridge.application.workflow_orchestration import workflow_recovery_operation
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    QueryCostBudget,
    QueryCostDecision,
)
from schemabridge.domain.decisions import ApprovalStatus
from schemabridge.domain.intents import IntentAlternativeId
from schemabridge.domain.request_context import validated_analytical_request_fingerprint
from schemabridge.domain.requests import MetricOperation
from schemabridge.domain.resolution import (
    ResolvedSemanticPlan,
    SemanticResolutionError,
    resolved_semantic_plan_fingerprint,
)
from schemabridge.domain.semantic_registry import GovernedSemanticRegistrySnapshot
from schemabridge.domain.validation import ValidationSeverity
from schemabridge.domain.workflows import (
    AgentWorkflowDraft,
    WorkflowCheckpointKind,
    WorkflowOperation,
    WorkflowStage,
    WorkflowTraceEvent,
    WorkflowTraceStatus,
)


class UiHealthStatus(StrEnum):
    """Closed status vocabulary rendered with both text and color."""

    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    NOT_CHECKED = "not_checked"


class UiRegistryProjectionStatus(StrEnum):
    """Closed operator-facing state for the non-authoritative projection."""

    FIXED = "fixed"
    PENDING = "pending"
    DELIVERED = "delivered"
    SUPERSEDED = "superseded"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class UiMode:
    name: str
    label: str
    kind: str
    detail: str


@dataclass(frozen=True, slots=True)
class UiHealth:
    name: str
    status: UiHealthStatus
    detail: str


@dataclass(frozen=True, slots=True)
class UiCandidateSignal:
    name: str
    score: float
    weight: float
    detail: str
    available: bool


@dataclass(frozen=True, slots=True)
class UiCandidate:
    logical_field: str
    physical_field: str
    native_type: str
    confidence: float
    recommendation: str
    status: str
    evidence: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    risks: tuple[str, ...]
    transformations: tuple[str, ...]
    signals: tuple[UiCandidateSignal, ...]


@dataclass(frozen=True, slots=True)
class UiMapping:
    logical_field: str
    physical_field: str
    physical_type: str
    status: str
    version: int
    confidence: float
    evidence: tuple[str, ...]
    risks: tuple[str, ...]
    transformations: tuple[str, ...]
    decision_id: str


@dataclass(frozen=True, slots=True)
class UiLogicalField:
    id: str
    definition: str
    canonical_type: str
    role: str
    allowed_values: tuple[str, ...]
    status: str
    version: int


@dataclass(frozen=True, slots=True)
class UiLogicalModel:
    id: str
    description: str
    status: str
    version: int
    fields: tuple[UiLogicalField, ...]


@dataclass(frozen=True, slots=True)
class UiRelationship:
    id: str
    left_logical_field: str
    left_physical_field: str
    right_logical_field: str
    right_physical_field: str
    cardinality: str
    join_type: str
    fanout_policy: str
    status: str
    version: int
    evidence: tuple[str, ...]
    risks: tuple[str, ...]
    left_transformations: tuple[str, ...]
    right_transformations: tuple[str, ...]
    decision_id: str


@dataclass(frozen=True, slots=True)
class UiInterpretationAlternative:
    id: str
    label: str
    rationale: str
    available: bool


@dataclass(frozen=True, slots=True)
class UiPolicyCheck:
    name: str
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class UiSemanticGateEvidence:
    """Positive M26 evidence returned by the server-side semantic gate."""

    status: str
    baseline_revision: int
    dependencies_fingerprint: str


@dataclass(frozen=True, slots=True)
class UiExecutionTarget:
    """Sanitized public connector identity bound into one governed plan."""

    connection_id: str
    connector_kind: str
    dialect: str
    route_revision: int
    route_fingerprint: str
    target_fingerprint: str
    type_contract_fingerprint: str


@dataclass(frozen=True, slots=True)
class UiQueryCostBudget:
    """Public numeric preflight limits without connector secret material."""

    fingerprint: str
    explain_timeout_ms: int
    max_response_bytes: int
    max_total_cost: str
    max_estimated_rows: int
    max_plan_nodes: int
    max_plan_depth: int
    max_plan_width: int


@dataclass(frozen=True, slots=True)
class UiQueryCostAssessment:
    """Sanitized EXPLAIN result; raw plans and topology have no field surface."""

    decision: str
    rejection_codes: tuple[str, ...]
    total_cost: str | None
    estimated_root_rows: int | None
    plan_width: int | None
    plan_node_count: int | None
    plan_depth: int | None
    response_bytes: int | None
    read_only: bool | None
    explain_timeout_ms: int
    fingerprint: str


@dataclass(frozen=True, slots=True)
class UiPlanInspection:
    """Explicit allowlist for a downloadable plan inspection.

    This projection deliberately cannot carry request/filter/source values, SQL,
    bound values, workflow principals, or workspace identities.
    """

    validated_request_fingerprint: str
    resolved_plan_fingerprint: str
    selected_assets: tuple[str, ...]
    mapping_versions: tuple[str, ...]
    approved_join_path: tuple[str, ...]
    fanout_mitigations: tuple[str, ...]
    bound_value_count: int
    bound_value_types: tuple[str, ...]
    policy_checks: tuple[UiPolicyCheck, ...]
    max_tables: int
    max_preview_rows: int
    statement_timeout_ms: int
    semantic_gate: UiSemanticGateEvidence | None
    execution_target: UiExecutionTarget | None
    cost_budget: UiQueryCostBudget | None
    cost_assessment: UiQueryCostAssessment | None
    preflight_error_code: str | None

    def as_json(self) -> str:
        """Serialize only the stable, reader-facing inspection allowlist."""

        payload: dict[str, object] = {
            "schema_version": 2,
            "fingerprints": {
                "validated_request": self.validated_request_fingerprint,
                "resolved_plan": self.resolved_plan_fingerprint,
            },
            "governed_plan": {
                "selected_assets": list(self.selected_assets),
                "mapping_versions": list(self.mapping_versions),
                "approved_join_path": list(self.approved_join_path),
                "fanout_mitigations": list(self.fanout_mitigations),
            },
            "bound_value_shape": {
                "count": self.bound_value_count,
                "types": list(self.bound_value_types),
            },
            "policy_checks": [
                {"name": item.name, "status": item.status} for item in self.policy_checks
            ],
            "limits": {
                "max_tables": self.max_tables,
                "max_preview_rows": self.max_preview_rows,
                "statement_timeout_ms": self.statement_timeout_ms,
            },
        }
        if self.semantic_gate is not None:
            payload["semantic_gate"] = {
                "status": self.semantic_gate.status,
                "baseline_revision": self.semantic_gate.baseline_revision,
                "dependencies_fingerprint": self.semantic_gate.dependencies_fingerprint,
            }
        if self.execution_target is not None:
            payload["execution_target"] = {
                "connection_id": self.execution_target.connection_id,
                "connector_kind": self.execution_target.connector_kind,
                "dialect": self.execution_target.dialect,
                "route_revision": self.execution_target.route_revision,
                "route_fingerprint": self.execution_target.route_fingerprint,
                "target_fingerprint": self.execution_target.target_fingerprint,
                "type_contract_fingerprint": self.execution_target.type_contract_fingerprint,
            }
        if self.cost_budget is not None:
            query_cost: dict[str, object] = {
                "budget": {
                    "fingerprint": self.cost_budget.fingerprint,
                    "explain_timeout_ms": self.cost_budget.explain_timeout_ms,
                    "max_response_bytes": self.cost_budget.max_response_bytes,
                    "max_total_cost": self.cost_budget.max_total_cost,
                    "max_estimated_rows": self.cost_budget.max_estimated_rows,
                    "max_plan_nodes": self.cost_budget.max_plan_nodes,
                    "max_plan_depth": self.cost_budget.max_plan_depth,
                    "max_plan_width": self.cost_budget.max_plan_width,
                },
            }
            if self.cost_assessment is not None:
                query_cost["assessment"] = {
                    "decision": self.cost_assessment.decision,
                    "rejection_codes": list(self.cost_assessment.rejection_codes),
                    "total_cost": self.cost_assessment.total_cost,
                    "estimated_root_rows": self.cost_assessment.estimated_root_rows,
                    "plan_width": self.cost_assessment.plan_width,
                    "plan_node_count": self.cost_assessment.plan_node_count,
                    "plan_depth": self.cost_assessment.plan_depth,
                    "response_bytes": self.cost_assessment.response_bytes,
                    "read_only": self.cost_assessment.read_only,
                    "explain_timeout_ms": self.cost_assessment.explain_timeout_ms,
                    "fingerprint": self.cost_assessment.fingerprint,
                }
            if self.preflight_error_code is not None:
                query_cost["error_code"] = self.preflight_error_code
            payload["query_cost"] = query_cost
        return json.dumps(payload, indent=2, sort_keys=True)


@dataclass(frozen=True, slots=True)
class UiRejection:
    record: int
    code: str
    reason: str


@dataclass(frozen=True, slots=True)
class UiResult:
    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]
    row_count: int
    preview_fingerprint: str
    database_user: str
    transaction_read_only: bool
    statement_timeout_ms: int
    truncated: bool
    rejections: tuple[UiRejection, ...]
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UiDecision:
    kind: str
    subject: str
    action: str
    actor: str
    version: str
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class UiTraceEvent:
    sequence: int
    stage: str
    operation: str
    status: str
    duration_ms: int | None
    summary: str


@dataclass(frozen=True, slots=True)
class UiQuery:
    business_text: str
    stage: str
    checkpoint: str | None
    interpretation_adapter: str | None
    ambiguities: tuple[str, ...]
    findings: tuple[str, ...]
    alternatives: tuple[UiInterpretationAlternative, ...]
    selected_assets: tuple[str, ...]
    mapping_versions: tuple[str, ...]
    join_path: tuple[str, ...]
    assumptions: tuple[str, ...]
    fanout_summary: str
    fanout_mitigations: tuple[str, ...]
    sql: str | None
    parameters: tuple[object, ...]
    policy_checks: tuple[UiPolicyCheck, ...]
    validated_request_fingerprint: str | None
    resolved_plan_fingerprint: str | None
    semantic_gate: UiSemanticGateEvidence | None
    execution_target: UiExecutionTarget | None
    cost_budget: UiQueryCostBudget | None
    cost_assessment: UiQueryCostAssessment | None
    preflight_error_code: str | None
    plan_json: str | None
    can_confirm: bool
    can_execute: bool
    can_publish: bool
    execution_blockers: tuple[str, ...]
    result: UiResult | None

    @property
    def parameter_types(self) -> tuple[str, ...]:
        """Expose safe logical value classes without exposing bound values."""

        return tuple(_safe_parameter_type(value) for value in self.parameters)


@dataclass(frozen=True, slots=True)
class UiReferenceData:
    concept_name: str
    concept_definition: str
    candidate_source: str
    context_source: str
    context_version: int
    registry_id: str
    registry_fingerprint: str
    catalog_scope: str
    model_count: int
    logical_models: tuple[UiLogicalModel, ...]
    candidates: tuple[UiCandidate, ...]
    mappings: tuple[UiMapping, ...]
    relationships: tuple[UiRelationship, ...]
    activation_generation: int | None
    active_pointer_fingerprint: str | None
    projection_status: UiRegistryProjectionStatus


@dataclass(frozen=True, slots=True)
class JudgeUiView:
    workflow_id: str | None
    revision: int | None
    modes: tuple[UiMode, ...]
    health: tuple[UiHealth, ...]
    reference: UiReferenceData
    query: UiQuery | None
    decisions: tuple[UiDecision, ...]
    trace: tuple[UiTraceEvent, ...]
    error_code: str | None = None
    error_message: str | None = None
    corrective_action: str | None = None
    can_retry: bool = False
    recovery_operation: str | None = None


@dataclass(frozen=True, slots=True)
class JudgeUiViewFactory:
    """Convert governed state to immutable presentation values."""

    reference: UiReferenceData
    prepare: PrepareGovernedRequest
    modes: tuple[UiMode, ...]

    def empty(self) -> JudgeUiView:
        return JudgeUiView(
            workflow_id=None,
            revision=None,
            modes=self.modes,
            health=_empty_health(self.modes),
            reference=self.reference,
            query=None,
            decisions=_reference_decisions(self.reference),
            trace=(),
        )

    def from_draft(self, draft: AgentWorkflowDraft) -> JudgeUiView:
        query = _query_view(draft, self.prepare)
        recovery_operation = workflow_recovery_operation(draft)
        return JudgeUiView(
            workflow_id=draft.id,
            revision=draft.revision,
            modes=self.modes,
            health=_health(draft, self.modes),
            reference=self.reference,
            query=query,
            decisions=(*_reference_decisions(self.reference), *_workflow_decisions(draft)),
            trace=tuple(
                UiTraceEvent(
                    sequence=event.sequence,
                    stage=event.stage.value,
                    operation=event.operation.value,
                    status=event.status.value,
                    duration_ms=event.duration_ms,
                    summary="; ".join(
                        f"{fact.key}={_trace_fact_display(fact.key, fact.value)}"
                        for fact in event.facts
                    ),
                )
                for event in draft.trace
            ),
            error_code=draft.failure.code if draft.failure else None,
            error_message=(
                f"The {draft.failure.operation.value} step failed safely."
                if draft.failure
                else None
            ),
            corrective_action=(
                "Restore the selected integration and use the explicit Retry action."
                if draft.failure and draft.failure.retryable
                else "Review the typed finding and start a corrected request."
                if draft.failure
                else None
            ),
            can_retry=bool(draft.failure and draft.failure.retryable),
            recovery_operation=(
                recovery_operation.value if recovery_operation is not None else None
            ),
        )


def build_ui_reference_data(
    candidate_report: CandidateGenerationReport,
    context: GovernedSemanticRegistrySnapshot,
    *,
    activation_generation: int | None = None,
    active_pointer_fingerprint: str | None = None,
    projection_status: UiRegistryProjectionStatus = UiRegistryProjectionStatus.FIXED,
) -> UiReferenceData:
    """Build the five-page reference model from existing governed objects."""

    active = activation_generation is not None
    if active != (active_pointer_fingerprint is not None):
        raise ValueError("active registry UI reference requires generation and pointer together")
    if active != (projection_status is not UiRegistryProjectionStatus.FIXED):
        raise ValueError("registry projection UI status does not match its selection mode")

    candidates = tuple(
        UiCandidate(
            logical_field=item.logical_field.root,
            physical_field=item.physical_field.root,
            native_type=item.native_type or "unknown",
            confidence=item.confidence.root,
            recommendation=item.recommendation.value,
            status=item.status.value,
            evidence=item.evidence,
            missing_evidence=item.missing_evidence,
            risks=item.risks,
            transformations=_operations(item.suggested_transformation_plan.model_dump(mode="json")),
            signals=tuple(
                UiCandidateSignal(
                    name=signal.signal.value,
                    score=signal.raw_score,
                    weight=signal.weight,
                    detail=signal.detail or signal.missing_reason or "not available",
                    available=signal.available,
                )
                for signal in item.score_breakdown
            ),
        )
        for item in candidate_report.candidates
    )
    mappings = tuple(
        UiMapping(
            logical_field=item.mapping.logical_field.root,
            physical_field=item.mapping.physical_field.root,
            physical_type=item.physical_type.value,
            status=item.mapping.status.value,
            version=item.mapping.version,
            confidence=item.mapping.confidence.root,
            evidence=item.mapping.evidence,
            risks=item.mapping.risks,
            transformations=_operations(item.mapping.transformation_plan.model_dump(mode="json")),
            decision_id=item.approval_decision_id or "not-approved",
        )
        for item in context.mapping_set.mappings
    )
    logical_models = tuple(
        UiLogicalModel(
            id=model.id.root,
            description=model.description,
            status=model.status.value,
            version=model.version,
            fields=tuple(
                UiLogicalField(
                    id=field.id.root,
                    definition=field.definition,
                    canonical_type=field.canonical_type.value,
                    role=field.role.value,
                    allowed_values=field.allowed_values,
                    status=field.status.value,
                    version=field.version,
                )
                for field in model.fields
            ),
        )
        for model in context.logical_context.models
    )
    relationships = tuple(
        UiRelationship(
            id=item.id,
            left_logical_field=item.left_key.logical_field.root,
            left_physical_field=item.left_key.physical_field.root,
            right_logical_field=item.right_key.logical_field.root,
            right_physical_field=item.right_key.physical_field.root,
            cardinality=item.cardinality.value,
            join_type=item.default_join_type.value,
            fanout_policy=item.fanout_policy.value,
            status=item.status.value,
            version=item.version,
            evidence=item.evidence,
            risks=item.risks,
            left_transformations=_operations(
                item.left_key.transformation_plan.model_dump(mode="json")
            ),
            right_transformations=_operations(
                item.right_key.transformation_plan.model_dump(mode="json")
            ),
            decision_id=item.approval_decision_id or "not-approved",
        )
        for item in context.join_contracts.contracts
    )
    return UiReferenceData(
        concept_name=candidate_report.concept.id.root,
        concept_definition=candidate_report.concept.definition,
        candidate_source=(
            f"{candidate_report.catalog_source} + {candidate_report.evidence_source}"
        ),
        context_source=context.source,
        context_version=context.version,
        registry_id=context.registry_id,
        registry_fingerprint=context.fingerprint,
        catalog_scope=context.catalog_scope,
        model_count=len(context.logical_context.models),
        logical_models=logical_models,
        candidates=candidates,
        mappings=mappings,
        relationships=relationships,
        activation_generation=activation_generation,
        active_pointer_fingerprint=active_pointer_fingerprint,
        projection_status=projection_status,
    )


def _query_view(draft: AgentWorkflowDraft, prepare: PrepareGovernedRequest) -> UiQuery:
    intent = draft.intent
    resolved = draft.resolved_plan
    prepared = None
    cost_assessment, preflight_error_code = _persisted_cost_preflight_view(
        draft,
        resolved.execution_target if resolved is not None else None,
    )
    if resolved is not None and draft.validated_request is not None:
        try:
            prepared = prepare.refresh_and_validate(
                draft.validated_request,
                resolved,
                assess_cost=False,
            )
        except ConnectorTargetError as error:
            preflight_error_code = error.code.value
        except (
            QueryCompilationError,
            SemanticChangeError,
            SqlPolicyViolation,
            SemanticResolutionError,
        ):
            prepared = None
    alternatives = (
        tuple(
            UiInterpretationAlternative(
                id=item.id.value,
                label=item.label,
                rationale=item.rationale,
                available=item.request is not None,
            )
            for item in intent.alternatives
        )
        if intent
        else ()
    )
    checkpoint = draft.checkpoint.kind if draft.checkpoint else None
    can_confirm = (
        checkpoint is WorkflowCheckpointKind.INTENT_CONFIRMATION
        and not any(
            finding.severity is ValidationSeverity.ERROR
            for finding in (intent.findings if intent else ())
        )
        and any(
            item.id == IntentAlternativeId.COUNT_DISTINCT_CUSTOMERS.value and item.available
            for item in alternatives
        )
    )
    plan_is_approved = bool(
        resolved
        and all(
            item.mapping.status is ApprovalStatus.APPROVED for item in resolved.selected_mappings
        )
        and all(item.status is ApprovalStatus.APPROVED for item in resolved.selected_contracts)
    )
    can_execute = (
        checkpoint is WorkflowCheckpointKind.EXECUTION_APPROVAL
        and draft.checkpoint is not None
        and resolved is not None
        and draft.checkpoint.fingerprint == resolved_semantic_plan_fingerprint(resolved)
        and prepared is not None
        and prepared.policy_status == "accepted"
        and _cost_preflight_is_accepted(resolved, cost_assessment)
        and preflight_error_code is None
        and plan_is_approved
    )
    blockers = _execution_blockers(
        draft,
        plan_is_approved,
        prepared_available=prepared is not None,
        preflight_error_code=preflight_error_code,
        cost_assessment=cost_assessment,
    )
    policy_checks = _policy_checks(prepared)
    selected_assets = (
        tuple(item.dataset.root for item in resolved.query_policy.assets) if resolved else ()
    )
    mapping_versions = (
        tuple(
            f"{item.mapping.logical_field.root} → {item.mapping.physical_field.root} "
            f"(v{item.mapping.version}, {item.mapping.status.value})"
            for item in resolved.selected_mappings
        )
        if resolved
        else ()
    )
    join_path = (
        tuple(
            f"{item.id} · {item.cardinality.value} · v{item.version} · {item.status.value}"
            for item in resolved.selected_contracts
        )
        if resolved
        else ()
    )
    request_fingerprint = (
        validated_analytical_request_fingerprint(draft.validated_request)
        if draft.validated_request is not None
        else None
    )
    plan_fingerprint = resolved_semantic_plan_fingerprint(resolved) if resolved else None
    gate_assessment = prepared.semantic_gate_assessment if prepared is not None else None
    semantic_gate = None
    if gate_assessment is not None and gate_assessment.eligible:
        assert gate_assessment.baseline_revision is not None
        semantic_gate = UiSemanticGateEvidence(
            status=gate_assessment.status.value,
            baseline_revision=gate_assessment.baseline_revision,
            dependencies_fingerprint=gate_assessment.dependencies_fingerprint,
        )
    execution_target = (
        _execution_target_view(resolved.execution_target)
        if resolved is not None and resolved.execution_target is not None
        else None
    )
    cost_budget = (
        _cost_budget_view(resolved.execution_target.cost_budget)
        if resolved is not None and resolved.execution_target is not None
        else None
    )
    parameter_values = tuple(prepared.query.parameters) if prepared else ()
    parameter_types = tuple(_safe_parameter_type(value) for value in parameter_values)
    fanout_mitigations = (
        tuple(
            f"{item.requested_operation.value} → {item.applied_operation.value}: {item.reason}"
            for item in resolved.fanout_mitigations
        )
        if resolved
        else ()
    )
    plan_inspection = (
        UiPlanInspection(
            validated_request_fingerprint=request_fingerprint,
            resolved_plan_fingerprint=plan_fingerprint,
            selected_assets=selected_assets,
            mapping_versions=mapping_versions,
            approved_join_path=join_path,
            fanout_mitigations=fanout_mitigations,
            bound_value_count=len(parameter_values),
            bound_value_types=parameter_types,
            policy_checks=policy_checks,
            max_tables=resolved.query_policy.max_tables,
            max_preview_rows=resolved.query_policy.max_preview_rows,
            statement_timeout_ms=resolved.query_policy.statement_timeout_ms,
            semantic_gate=semantic_gate,
            execution_target=execution_target,
            cost_budget=cost_budget,
            cost_assessment=cost_assessment,
            preflight_error_code=preflight_error_code,
        )
        if resolved is not None and request_fingerprint is not None and plan_fingerprint is not None
        else None
    )
    result = _result_view(draft)
    can_publish = (
        draft.stage is WorkflowStage.PUBLICATION_PROPOSED
        and draft.publication_proposal is not None
        and draft.execution is not None
    )
    return UiQuery(
        business_text=draft.text,
        stage=draft.stage.value,
        checkpoint=checkpoint.value if checkpoint else None,
        interpretation_adapter=intent.adapter if intent else None,
        ambiguities=tuple(item.value for item in intent.ambiguities) if intent else (),
        findings=(
            tuple(
                f"{item.severity.value}: {item.code} — {item.message}" for item in intent.findings
            )
            if intent
            else ()
        ),
        alternatives=alternatives,
        selected_assets=selected_assets,
        mapping_versions=mapping_versions,
        join_path=join_path,
        assumptions=tuple(item.message for item in resolved.assumptions) if resolved else (),
        fanout_summary=_fanout_summary(resolved),
        fanout_mitigations=fanout_mitigations,
        sql=prepared.query.sql if prepared else None,
        parameters=parameter_values,
        policy_checks=policy_checks,
        validated_request_fingerprint=request_fingerprint,
        resolved_plan_fingerprint=plan_fingerprint,
        semantic_gate=semantic_gate,
        execution_target=execution_target,
        cost_budget=cost_budget,
        cost_assessment=cost_assessment,
        preflight_error_code=preflight_error_code,
        plan_json=plan_inspection.as_json() if plan_inspection is not None else None,
        can_confirm=can_confirm,
        can_execute=can_execute,
        can_publish=can_publish,
        execution_blockers=blockers,
        result=result,
    )


def _execution_target_view(target: GovernedExecutionTarget) -> UiExecutionTarget:
    """Project only the public, inert target fields required for review."""

    return UiExecutionTarget(
        connection_id=target.connection_id.root,
        connector_kind=target.connector_kind.value,
        dialect=target.dialect.value,
        route_revision=target.route_revision,
        route_fingerprint=_abbreviated_fingerprint(target.route_fingerprint),
        target_fingerprint=_abbreviated_fingerprint(target.fingerprint),
        type_contract_fingerprint=_abbreviated_fingerprint(target.type_contract_fingerprint),
    )


def _cost_budget_view(budget: QueryCostBudget) -> UiQueryCostBudget:
    """Project the exact public numeric limits without private route state."""

    return UiQueryCostBudget(
        fingerprint=_abbreviated_fingerprint(budget.fingerprint),
        explain_timeout_ms=budget.explain_timeout_ms,
        max_response_bytes=budget.max_response_bytes,
        max_total_cost=_decimal_display(budget.max_total_cost),
        max_estimated_rows=budget.max_estimated_rows,
        max_plan_nodes=budget.max_plan_nodes,
        max_plan_depth=budget.max_plan_depth,
        max_plan_width=budget.max_plan_width,
    )


def _persisted_cost_preflight_view(
    draft: AgentWorkflowDraft,
    target: GovernedExecutionTarget | None,
) -> tuple[UiQueryCostAssessment | None, str | None]:
    """Read the latest exact-plan cost decision from durable sanitized trace facts.

    UI construction must never turn into source I/O. The SQL validation workflow
    already persisted the bounded assessment fingerprint and selected scalar
    metrics, so this projection deliberately has no dependency on a cost adapter.
    """

    if target is None or draft.plan_fingerprint is None:
        return None, None
    attempt = _latest_sql_validation_attempt(draft)
    if attempt is None:
        return None, None
    started, completed = attempt
    if draft.plan_fingerprint not in started.input_refs:
        return None, None
    facts = _unique_trace_facts(completed)
    if facts is None:
        return None, None
    error_code = _persisted_preflight_error(facts.get("error_code"))
    raw_decision = facts.get("cost_decision")
    if raw_decision not in {
        QueryCostDecision.ACCEPTED.value,
        QueryCostDecision.REJECTED.value,
    }:
        return None, error_code
    expected_status = (
        WorkflowTraceStatus.SUCCEEDED
        if raw_decision == QueryCostDecision.ACCEPTED.value
        else WorkflowTraceStatus.FAILED
    )
    if completed.status is not expected_status:
        return None, error_code
    if raw_decision == QueryCostDecision.ACCEPTED.value and error_code is not None:
        return None, error_code

    fingerprint = facts.get("cost_assessment")
    total_cost = _persisted_non_negative_decimal(facts.get("estimated_total_cost"))
    estimated_root_rows = _persisted_non_negative_integer(
        facts.get("estimated_root_rows"),
        minimum=0,
    )
    plan_node_count = _persisted_non_negative_integer(
        facts.get("plan_node_count"),
        minimum=1,
    )
    if (
        fingerprint is None
        or not _is_lowercase_sha256(fingerprint)
        or total_cost is None
        or estimated_root_rows is None
        or plan_node_count is None
    ):
        return None, error_code

    return (
        UiQueryCostAssessment(
            decision=raw_decision,
            rejection_codes=(
                (error_code,)
                if raw_decision == QueryCostDecision.REJECTED.value and error_code is not None
                else ()
            ),
            total_cost=_decimal_display(total_cost),
            estimated_root_rows=estimated_root_rows,
            plan_width=None,
            plan_node_count=plan_node_count,
            plan_depth=None,
            response_bytes=None,
            read_only=None,
            explain_timeout_ms=target.cost_budget.explain_timeout_ms,
            fingerprint=_abbreviated_fingerprint(fingerprint),
        ),
        error_code,
    )


def _latest_sql_validation_attempt(
    draft: AgentWorkflowDraft,
) -> tuple[WorkflowTraceEvent, WorkflowTraceEvent] | None:
    """Return only a completed latest attempt; an in-flight retry fails closed."""

    matching = tuple(
        event for event in draft.trace if event.operation is WorkflowOperation.SQL_VALIDATION
    )
    if not matching or matching[-1].status is WorkflowTraceStatus.STARTED:
        return None
    if len(matching) < 2 or matching[-2].status is not WorkflowTraceStatus.STARTED:
        return None
    return matching[-2], matching[-1]


def _unique_trace_facts(event: WorkflowTraceEvent) -> dict[str, str] | None:
    facts = {fact.key: fact.value for fact in event.facts}
    return facts if len(facts) == len(event.facts) else None


def _persisted_preflight_error(value: str | None) -> str | None:
    if value is None or not value.startswith(("connector_", "query_cost_")):
        return None
    return value


def _persisted_non_negative_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(value)
    except ArithmeticError:
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def _persisted_non_negative_integer(
    value: str | None,
    *,
    minimum: int,
) -> int | None:
    if value is None or not value.isascii() or not value.isdecimal():
        return None
    parsed = int(value)
    return parsed if parsed >= minimum else None


def _is_lowercase_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _trace_fact_display(key: str, value: str) -> str:
    """Avoid expanding the persisted cost correlation token in the visible trace."""

    if key != "cost_assessment":
        return value
    return _abbreviated_fingerprint(value) if _is_lowercase_sha256(value) else "invalid"


def _cost_preflight_is_accepted(
    resolved: ResolvedSemanticPlan | None,
    assessment: UiQueryCostAssessment | None,
) -> bool:
    if resolved is None or resolved.execution_target is None:
        return assessment is None
    return assessment is not None and assessment.decision == QueryCostDecision.ACCEPTED.value


def _abbreviated_fingerprint(value: str) -> str:
    """Return a stable reader hint without rendering a full correlation token."""

    return f"{value[:12]}…"


def _decimal_display(value: Decimal) -> str:
    """Render a finite domain decimal without scientific notation."""

    return format(value, "f")


def _fanout_summary(resolved: ResolvedSemanticPlan | None) -> str:
    """Explain fanout from the governed plan without creating mitigation evidence."""

    if resolved is None:
        return "Fanout handling is available after semantic resolution."
    if any(
        item.applied_operation is MetricOperation.COUNT_DISTINCT
        for item in resolved.fanout_mitigations
    ):
        return (
            "COUNT DISTINCT already mitigates the recorded one-to-many fanout for the "
            "affected metric; no additional rewrite is needed."
        )
    if resolved.fanout_mitigations:
        return (
            "The governed plan records duplication-invariant fanout handling for the "
            "affected metric."
        )
    if not resolved.selected_contracts:
        return "No join fanout applies to this plan."
    if any(
        metric.operation is MetricOperation.COUNT_DISTINCT for metric in resolved.request.metrics
    ):
        return (
            "COUNT DISTINCT is already the requested metric, but the governed plan does not "
            "record it as a fanout mitigation for this join orientation."
        )
    return (
        "The governed plan records no fanout mitigation for the requested metric in this "
        "join orientation."
    )


def _safe_parameter_type(value: object) -> str:
    """Map bound values to a closed display label without rendering their content."""

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, (float, Decimal)):
        return "number"
    if isinstance(value, datetime):
        return "timestamp"
    if isinstance(value, date):
        return "date"
    if isinstance(value, str):
        return "text"
    return "typed"


def _empty_health(modes: tuple[UiMode, ...]) -> tuple[UiHealth, ...]:
    catalog_recorded = _mode_kind(modes, "Catalog") == "recorded"
    source_recorded = _mode_kind(modes, "Source") == "recorded"
    return (
        UiHealth(
            "Catalog",
            UiHealthStatus.READY if catalog_recorded else UiHealthStatus.NOT_CHECKED,
            (
                "The versioned sanitized catalog recording loaded successfully."
                if catalog_recorded
                else "Load a scenario to perform a bounded live catalog read."
            ),
        ),
        UiHealth(
            "Source",
            UiHealthStatus.READY if source_recorded else UiHealthStatus.NOT_CHECKED,
            (
                "The exact versioned result/rejection recording loaded; no database call occurs."
                if source_recorded
                else "No query has been sent to PostgreSQL."
            ),
        ),
    )


def _health(draft: AgentWorkflowDraft, modes: tuple[UiMode, ...]) -> tuple[UiHealth, ...]:
    source_recorded = _mode_kind(modes, "Source") == "recorded"
    catalog = (
        UiHealth(
            "Catalog",
            UiHealthStatus.READY,
            f"{draft.context_source}; {len(draft.context_assets)} allowlisted assets read.",
        )
        if draft.context_assets
        else UiHealth(
            "Catalog",
            UiHealthStatus.BLOCKED if draft.failure else UiHealthStatus.NOT_CHECKED,
            "No catalog context is available.",
        )
    )
    source = (
        UiHealth(
            "Source",
            UiHealthStatus.READY,
            f"{draft.execution.database_user}; read_only={draft.execution.transaction_read_only}; "
            f"timeout={draft.execution.statement_timeout_ms} ms; "
            f"mode={'recorded observation' if source_recorded else 'live database'}.",
        )
        if draft.execution
        else UiHealth(
            "Source",
            UiHealthStatus.BLOCKED
            if draft.failure
            and draft.failure.operation.value
            in {
                "preview_execution",
                "rejection_inspection",
            }
            else UiHealthStatus.NOT_CHECKED,
            (
                "Recorded result is ready, but no approved replay has completed."
                if source_recorded
                else "No bounded preview has completed."
            ),
        )
    )
    return catalog, source


def _mode_kind(modes: tuple[UiMode, ...], name: str) -> str | None:
    return next((mode.kind for mode in modes if mode.name == name), None)


def _policy_checks(prepared: object | None) -> tuple[UiPolicyCheck, ...]:
    if prepared is None:
        return ()
    return (
        UiPolicyCheck("AST parse", "accepted", "Exactly one read-only SELECT/CTE statement."),
        UiPolicyCheck("Asset allowlist", "accepted", "All tables and columns are approved."),
        UiPolicyCheck("Join policy", "accepted", "No Cartesian join; approved predicates only."),
        UiPolicyCheck("Bound parameters", "accepted", "Filter values are not interpolated."),
        UiPolicyCheck("Preview bounds", "accepted", "Limit and statement timeout are enforced."),
    )


def _execution_blockers(
    draft: AgentWorkflowDraft,
    plan_is_approved: bool,
    *,
    prepared_available: bool,
    preflight_error_code: str | None,
    cost_assessment: UiQueryCostAssessment | None,
) -> tuple[str, ...]:
    if draft.execution is not None:
        return ("The exact approved preview has already executed.",)
    if draft.failure is not None:
        return (f"Resolve typed failure: {draft.failure.code}.",)
    if draft.intent is None:
        return ("Load or enter a request to produce a typed interpretation.",)
    if draft.validated_request is None:
        return ("Confirm an explicit interpretation before semantic planning.",)
    if draft.resolved_plan is None:
        return ("A governed semantic plan is not ready.",)
    if preflight_error_code is not None:
        return (f"Governed connector/cost preflight blocked execution: {preflight_error_code}.",)
    if draft.resolved_plan.execution_target is not None and (
        cost_assessment is None or cost_assessment.decision == QueryCostDecision.REJECTED.value
    ):
        reasons = (
            ", ".join(cost_assessment.rejection_codes)
            if cost_assessment is not None
            else "assessment_missing"
        )
        return (f"Governed query cost preflight rejected execution: {reasons}.",)
    if not prepared_available:
        return (
            "The plan no longer matches the active semantic registry or SQL policy; "
            "start a newly confirmed request.",
        )
    if not plan_is_approved:
        return ("Every selected mapping and join contract must be approved and current.",)
    if (
        draft.checkpoint is None
        or draft.checkpoint.kind is not WorkflowCheckpointKind.EXECUTION_APPROVAL
    ):
        return ("Execution requires the typed approval checkpoint for this exact plan.",)
    return ()


def _result_view(draft: AgentWorkflowDraft) -> UiResult | None:
    execution = draft.execution
    if execution is None:
        return None
    rejections = tuple(
        UiRejection(record=index, code=code, reason=_rejection_reason(code))
        for index, code in enumerate(execution.rejection_codes, start=1)
    )
    return UiResult(
        columns=execution.columns,
        rows=tuple(tuple(value for value in row) for row in execution.rows),
        row_count=execution.observed_row_count,
        preview_fingerprint=execution.preview_fingerprint,
        database_user=execution.database_user,
        transaction_read_only=execution.transaction_read_only,
        statement_timeout_ms=execution.statement_timeout_ms,
        truncated=execution.truncated,
        rejections=rejections,
        limitations=(
            "Synthetic demonstration data only; these rows are not production evidence.",
            "Rejected source values are described but not persisted in workflow state.",
            "The result is a bounded preview, not an exported analytical dataset.",
        ),
    )


def _rejection_reason(code: str) -> str:
    return {
        "non_integral_identifier": "127.5 is fractional and was rejected without truncation.",
        "non_finite_identifier": "NaN is non-finite and cannot be a governed identifier.",
        "null_join_key": "NULL was preserved, reported separately, and excluded from the join.",
        "unsafe_float_identifier": "The float identifier is outside the safe exact-integer range.",
        "malformed_identifier": "The value does not match the approved identifier format.",
    }.get(code, "The value failed its approved closed transformation plan.")


def _reference_decisions(reference: UiReferenceData) -> tuple[UiDecision, ...]:
    mapping_decisions = tuple(
        UiDecision(
            kind="mapping",
            subject=f"{item.logical_field} → {item.physical_field}",
            action="approve",
            actor="synthetic fixture review",
            version=str(item.version),
            status=item.status,
            detail=f"Decision {item.decision_id}; {len(item.evidence)} evidence item(s).",
        )
        for item in reference.mappings
    )
    join_decisions = tuple(
        UiDecision(
            kind="join",
            subject=item.id,
            action="approve",
            actor="synthetic fixture review",
            version=str(item.version),
            status=item.status,
            detail=f"{item.cardinality}; {item.fanout_policy}; decision {item.decision_id}.",
        )
        for item in reference.relationships
    )
    return (*mapping_decisions, *join_decisions)


def _workflow_decisions(draft: AgentWorkflowDraft) -> tuple[UiDecision, ...]:
    decisions = tuple(
        UiDecision(
            kind=item.kind.value,
            subject=item.bound_fingerprint[:12],
            action=item.action.value,
            actor=item.actor,
            version=f"revision {draft.revision}",
            status="recorded",
            detail=item.decided_at.isoformat(),
        )
        for item in draft.decisions
    )
    if draft.publication_result is None:
        return decisions
    return (
        *decisions,
        UiDecision(
            kind="publication",
            subject=draft.publication_result.document_ref,
            action="publish",
            actor=draft.publication_approval.actor if draft.publication_approval else "unknown",
            version="current",
            status=draft.publication_result.status.value,
            detail="Approved execution context publication completed idempotently.",
        ),
    )


def _operations(plan: dict[str, object]) -> tuple[str, ...]:
    steps = plan.get("steps", [])
    if not isinstance(steps, list):
        return ()
    return tuple(str(step.get("operation", "unknown")) for step in steps if isinstance(step, dict))
