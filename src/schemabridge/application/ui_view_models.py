"""Typed, secret-safe view models for the judge-facing interface."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from schemabridge.application.candidate_engine import CandidateGenerationReport
from schemabridge.application.governed_execution import PrepareGovernedRequest
from schemabridge.domain.decisions import ApprovalStatus
from schemabridge.domain.intents import IntentAlternativeId
from schemabridge.domain.resolution import SemanticPlanningContext
from schemabridge.domain.validation import ValidationSeverity
from schemabridge.domain.workflows import (
    AgentWorkflowDraft,
    WorkflowCheckpointKind,
    WorkflowStage,
)


class UiHealthStatus(StrEnum):
    """Closed status vocabulary rendered with both text and color."""

    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    NOT_CHECKED = "not_checked"


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
class UiRejection:
    record: int
    code: str
    reason: str


@dataclass(frozen=True, slots=True)
class UiResult:
    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]
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
    fanout_mitigations: tuple[str, ...]
    sql: str | None
    parameters: tuple[object, ...]
    policy_checks: tuple[UiPolicyCheck, ...]
    plan_json: str | None
    can_confirm: bool
    can_execute: bool
    can_publish: bool
    execution_blockers: tuple[str, ...]
    result: UiResult | None


@dataclass(frozen=True, slots=True)
class UiReferenceData:
    concept_name: str
    concept_definition: str
    candidate_source: str
    context_source: str
    context_version: int
    candidates: tuple[UiCandidate, ...]
    mappings: tuple[UiMapping, ...]
    relationships: tuple[UiRelationship, ...]


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
            health=(
                UiHealth(
                    "Catalog",
                    UiHealthStatus.NOT_CHECKED,
                    "Load a scenario to perform a bounded catalog read.",
                ),
                UiHealth(
                    "Source",
                    UiHealthStatus.NOT_CHECKED,
                    "No query has been sent to PostgreSQL.",
                ),
            ),
            reference=self.reference,
            query=None,
            decisions=_reference_decisions(self.reference),
            trace=(),
        )

    def from_draft(self, draft: AgentWorkflowDraft) -> JudgeUiView:
        query = _query_view(draft, self.prepare)
        return JudgeUiView(
            workflow_id=draft.id,
            revision=draft.revision,
            modes=self.modes,
            health=_health(draft),
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
                    summary="; ".join(f"{fact.key}={fact.value}" for fact in event.facts),
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
        )


def build_ui_reference_data(
    candidate_report: CandidateGenerationReport,
    context: SemanticPlanningContext,
) -> UiReferenceData:
    """Build the five-page reference model from existing governed objects."""

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
        candidates=candidates,
        mappings=mappings,
        relationships=relationships,
    )


def _query_view(draft: AgentWorkflowDraft, prepare: PrepareGovernedRequest) -> UiQuery:
    intent = draft.intent
    resolved = draft.resolved_plan
    prepared = prepare.validate_resolved(resolved) if resolved is not None else None
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
        and prepared is not None
        and prepared.policy_status == "accepted"
        and plan_is_approved
    )
    blockers = _execution_blockers(draft, plan_is_approved)
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
        fanout_mitigations=(
            tuple(
                f"{item.requested_operation.value} → {item.applied_operation.value}: {item.reason}"
                for item in resolved.fanout_mitigations
            )
            if resolved
            else ()
        ),
        sql=prepared.query.sql if prepared else None,
        parameters=tuple(prepared.query.parameters) if prepared else (),
        policy_checks=policy_checks,
        plan_json=(
            json.dumps(resolved.model_dump(mode="json"), indent=2, sort_keys=True)
            if resolved
            else None
        ),
        can_confirm=can_confirm,
        can_execute=can_execute,
        can_publish=can_publish,
        execution_blockers=blockers,
        result=result,
    )


def _health(draft: AgentWorkflowDraft) -> tuple[UiHealth, ...]:
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
            f"timeout={draft.execution.statement_timeout_ms} ms.",
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
            "No bounded preview has completed.",
        )
    )
    return catalog, source


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
