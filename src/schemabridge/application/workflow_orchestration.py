"""Observable, resumable orchestration across existing governed use cases."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from schemabridge.application.connectors import ConnectorTargetError
from schemabridge.application.governed_execution import (
    ExecuteGovernedRequest,
    GovernedPreparedQuery,
    PrepareGovernedRequest,
)
from schemabridge.application.intent_resolution import (
    IntentAlternative,
    IntentConfirmationError,
    IntentPreview,
    ResolveNaturalLanguageIntent,
)
from schemabridge.application.ports.catalog import (
    CatalogReadError,
    CatalogReadPort,
    PageRequest,
)
from schemabridge.application.ports.intents import IntentParserError
from schemabridge.application.ports.planning import (
    PlanningPortError,
    PlanningPortErrorCode,
    ProtectedSourceOperationCancelled,
)
from schemabridge.application.ports.publication_audit import (
    PublicationAuditStoreError,
    PublicationAuditStorePort,
)
from schemabridge.application.ports.recipes import RecipeError, RecipeErrorCode
from schemabridge.application.ports.requests import RequestWorkflowError
from schemabridge.application.ports.workflows import (
    WorkflowClockPort,
    WorkflowDraftStorePort,
    WorkflowError,
    WorkflowErrorCode,
    WorkflowPublicationPort,
)
from schemabridge.application.query_cost import (
    AssessGovernedQueryCost,
    QueryCostError,
    QueryCostErrorCode,
)
from schemabridge.application.query_execution import (
    QueryCompilationError,
    QueryPreviewError,
    QueryPreviewResult,
    QueryPreviewTimeoutError,
    QueryPreviewUnavailableError,
    SqlPolicyViolation,
)
from schemabridge.application.query_recipes import (
    AssessQueryRecipeReuse,
    source_schema_fingerprint,
)
from schemabridge.application.semantic_change import SemanticChangeError
from schemabridge.domain.connectors import QueryCostAssessment
from schemabridge.domain.intents import IntentConfirmation, UserLanguage
from schemabridge.domain.publication_audit import validate_publication_audit_binding
from schemabridge.domain.query_studio import ConfirmedQueryStudioRequest
from schemabridge.domain.request_context import validated_analytical_request_fingerprint
from schemabridge.domain.resolution import (
    RejectedSourceReport,
    SemanticResolutionError,
    resolved_semantic_plan_fingerprint,
)
from schemabridge.domain.workflows import (
    AgentWorkflowDraft,
    ExecutionWorkflowDecision,
    IntentWorkflowDecision,
    PublicationWorkflowDecision,
    RetryWorkflowDecision,
    StartWorkflowCommand,
    WorkflowCatalogAsset,
    WorkflowCheckpoint,
    WorkflowCheckpointKind,
    WorkflowDecisionAction,
    WorkflowDecisionKind,
    WorkflowDecisionRecord,
    WorkflowExecutionRecord,
    WorkflowFailure,
    WorkflowIntentAlternative,
    WorkflowIntentSnapshot,
    WorkflowOperation,
    WorkflowPublicationApproval,
    WorkflowPublicationProposal,
    WorkflowPublicationStatus,
    WorkflowScalar,
    WorkflowStage,
    WorkflowTraceEvent,
    WorkflowTraceFact,
    WorkflowTraceStatus,
    fingerprint_payload,
)


def _continue_execution() -> bool:
    return True


@dataclass(frozen=True, slots=True)
class AgentWorkflowOrchestrator:
    """Drive only explicit transitions; adapters remain injected behind ports."""

    store: WorkflowDraftStorePort
    clock: WorkflowClockPort
    catalog: CatalogReadPort
    intent: ResolveNaturalLanguageIntent
    prepare: PrepareGovernedRequest
    execute: ExecuteGovernedRequest
    publisher: WorkflowPublicationPort
    audit_store: PublicationAuditStorePort
    recipe_assessor: AssessQueryRecipeReuse | None = None

    def start(self, command: StartWorkflowCommand) -> AgentWorkflowDraft:
        now = self.clock.now()
        draft = AgentWorkflowDraft(
            id=command.id,
            revision=1,
            stage=WorkflowStage.CREATED,
            text=command.text,
            language=command.language,
            requested_datasets=command.datasets,
            created_at=now,
            updated_at=now,
        )
        self.store.save(draft, expected_revision=None)
        return self._retrieve_context(draft)

    def start_confirmed(
        self,
        workflow_id: str,
        confirmed: ConfirmedQueryStudioRequest,
    ) -> AgentWorkflowDraft:
        """Start from an exact Query Studio confirmation without replaying model output.

        Semantic resolution is used once before persistence only to derive the bounded physical
        catalog slice. The normal workflow then reloads that exact catalog slice, resolves the
        current registry again, runs the M26 semantic gate, compiles deterministically, and
        independently validates SQL before it can request execution approval.
        """

        try:
            preflight = self.prepare.planner.execute(confirmed.validated_request)
        except (ConnectorTargetError, PlanningPortError, SemanticResolutionError) as error:
            raise WorkflowError(
                WorkflowErrorCode.INVALID_TRANSITION,
                "the confirmed Query Studio request is no longer valid in the current registry",
            ) from error
        datasets = tuple(asset.dataset for asset in preflight.query_policy.assets)
        now = self.clock.now()
        draft = AgentWorkflowDraft(
            id=workflow_id,
            revision=1,
            stage=WorkflowStage.CREATED,
            text=(
                confirmed.original_text.root
                if confirmed.original_text is not None
                else "Confirmed guided Query Studio request."
            ),
            language=confirmed.language or UserLanguage.ENGLISH,
            requested_datasets=datasets,
            validated_request=confirmed.validated_request,
            created_at=now,
            updated_at=now,
        )
        self.store.save(draft, expected_revision=None)
        return self._retrieve_context(draft)

    def inspect(self, workflow_id: str) -> AgentWorkflowDraft:
        """Load durable state without advancing or repairing the workflow."""

        return self._load(workflow_id)

    def resume(self, workflow_id: str) -> AgentWorkflowDraft:
        """Backward-compatible, read-only alias for :meth:`inspect`."""

        return self.inspect(workflow_id)

    def recover_interrupted(
        self,
        workflow_id: str,
        *,
        expected_operation: WorkflowOperation,
    ) -> AgentWorkflowDraft:
        """Repair one explicitly authorized interrupted transition without external I/O."""

        draft = self._load(workflow_id)
        recovery_operation = workflow_recovery_operation(draft)
        if recovery_operation is None:
            raise _invalid_transition("workflow has no interrupted transition to recover")
        if recovery_operation is not expected_operation:
            raise _decision_mismatch("workflow recovery operation changed before authorization")
        if draft.trace and draft.trace[-1].status is WorkflowTraceStatus.STARTED:
            return self._fail_external(
                draft,
                draft.trace[-1].operation,
                code="workflow_external_action_interrupted",
                retryable=True,
            )
        pending = _pending_resume_operation(draft)
        if pending is not None:
            return self._pause_for_retry(draft, pending)
        if (
            draft.stage is WorkflowStage.VALIDATED
            and draft.checkpoint is None
            and draft.plan_fingerprint is not None
        ):
            return self._save_changes(
                draft,
                stage=WorkflowStage.DECISION_REQUIRED,
                checkpoint=WorkflowCheckpoint(
                    kind=WorkflowCheckpointKind.EXECUTION_APPROVAL,
                    fingerprint=draft.plan_fingerprint,
                    reason="Approve the exact validated plan before bounded preview execution.",
                ),
            )
        if draft.stage is WorkflowStage.EXECUTED:
            return self._propose_publication(draft)
        return draft

    def decide_intent(
        self,
        workflow_id: str,
        decision: IntentWorkflowDecision,
    ) -> AgentWorkflowDraft:
        draft = self._load(workflow_id)
        self._require_checkpoint(draft, WorkflowCheckpointKind.INTENT_CONFIRMATION)
        checkpoint = draft.checkpoint
        assert checkpoint is not None
        if decision.interpretation_fingerprint != checkpoint.fingerprint:
            raise _decision_mismatch("intent decision does not match the paused interpretation")
        if draft.intent is None:
            raise _invalid_transition("intent snapshot is unavailable")
        preview = _restore_intent_preview(draft.intent)
        try:
            validated = self.intent.confirm(
                preview,
                IntentConfirmation(
                    interpretation_fingerprint=decision.interpretation_fingerprint,
                    selected_alternative=decision.selected_alternative,
                ),
            )
        except (IntentConfirmationError, RequestWorkflowError) as error:
            raise _decision_mismatch(str(error)) from error
        decided = self._record_decision(
            draft,
            kind=WorkflowDecisionKind.INTENT,
            action=WorkflowDecisionAction.CONFIRM,
            actor=decision.actor,
            bound_fingerprint=decision.interpretation_fingerprint,
            updates={
                "validated_request": validated,
                "stage": WorkflowStage.SEMANTIC_RESOLUTION,
                "checkpoint": None,
            },
        )
        return self._resolve_semantics(decided)

    def decide_execution(
        self,
        workflow_id: str,
        decision: ExecutionWorkflowDecision,
        *,
        should_continue: Callable[[], bool] | None = None,
    ) -> AgentWorkflowDraft:
        draft = self._load(workflow_id)
        self._require_checkpoint(draft, WorkflowCheckpointKind.EXECUTION_APPROVAL)
        checkpoint = draft.checkpoint
        assert checkpoint is not None
        if decision.plan_fingerprint != checkpoint.fingerprint:
            raise _decision_mismatch("execution decision does not match the validated plan")
        if decision.action is WorkflowDecisionAction.DECLINE:
            return self._record_decision(
                draft,
                kind=WorkflowDecisionKind.EXECUTION,
                action=decision.action,
                actor=decision.actor,
                bound_fingerprint=decision.plan_fingerprint,
                updates={"stage": WorkflowStage.COMPLETED, "checkpoint": None},
            )
        decided = self._record_decision(
            draft,
            kind=WorkflowDecisionKind.EXECUTION,
            action=decision.action,
            actor=decision.actor,
            bound_fingerprint=decision.plan_fingerprint,
            updates={"stage": WorkflowStage.VALIDATED, "checkpoint": None},
        )
        prepared, current = self._revalidate_resolved(decided)
        if prepared is None:
            return current
        return self._execute_preview(
            current,
            prepared,
            should_continue=should_continue or _continue_execution,
        )

    def decide_publication(
        self,
        workflow_id: str,
        decision: PublicationWorkflowDecision,
    ) -> AgentWorkflowDraft:
        draft = self._load(workflow_id)
        if draft.publication_result is not None:
            return draft
        self._require_checkpoint(draft, WorkflowCheckpointKind.PUBLICATION_DECISION)
        proposal = draft.publication_proposal
        if proposal is None or decision.proposal_fingerprint != proposal.fingerprint:
            raise _decision_mismatch("publication decision does not match the proposed context")
        if decision.action is WorkflowDecisionAction.SKIP:
            return self._record_decision(
                draft,
                kind=WorkflowDecisionKind.PUBLICATION,
                action=decision.action,
                actor=decision.actor,
                bound_fingerprint=proposal.fingerprint,
                updates={"stage": WorkflowStage.COMPLETED, "checkpoint": None},
            )
        assert decision.confirmation is not None
        approved_at = self.clock.now()
        approval = WorkflowPublicationApproval(
            id=f"workflow-publication-{proposal.idempotency_key}",
            workflow_id=draft.id,
            proposal_fingerprint=proposal.fingerprint,
            idempotency_key=proposal.idempotency_key,
            actor=decision.actor,
            approved_at=approved_at,
            confirmation=decision.confirmation,
        )
        decided = self._record_decision(
            draft,
            kind=WorkflowDecisionKind.PUBLICATION,
            action=decision.action,
            actor=decision.actor,
            bound_fingerprint=proposal.fingerprint,
            updates={"publication_approval": approval, "checkpoint": None},
        )
        return self._publish(decided)

    def retry(
        self,
        workflow_id: str,
        decision: RetryWorkflowDecision,
        *,
        reserved_execution: ExecutionWorkflowDecision | None = None,
        should_continue: Callable[[], bool] | None = None,
    ) -> AgentWorkflowDraft:
        draft = self._load(workflow_id)
        failure = draft.failure
        if draft.stage is not WorkflowStage.FAILED or failure is None or not failure.retryable:
            raise WorkflowError(
                WorkflowErrorCode.RETRY_NOT_ALLOWED,
                "the workflow has no retryable external failure",
            )
        if (
            decision.failure_fingerprint != failure.fingerprint
            or decision.operation is not failure.operation
        ):
            raise _decision_mismatch("retry decision does not match the recorded failure")
        if reserved_execution is not None and (
            reserved_execution.action is not WorkflowDecisionAction.APPROVE
            or reserved_execution.actor != decision.actor
            or reserved_execution.plan_fingerprint != draft.plan_fingerprint
            or failure.operation
            not in {
                WorkflowOperation.SQL_VALIDATION,
                WorkflowOperation.PREVIEW_EXECUTION,
                WorkflowOperation.REJECTION_INSPECTION,
            }
            or not any(
                prior.kind is WorkflowDecisionKind.EXECUTION
                and prior.action is WorkflowDecisionAction.APPROVE
                and prior.actor == reserved_execution.actor
                and prior.bound_fingerprint == reserved_execution.plan_fingerprint
                for prior in draft.decisions
            )
        ):
            raise _decision_mismatch(
                "reserved execution retry does not match the original approval"
            )
        continuation = should_continue or _continue_execution
        retried = self._record_decision(
            draft,
            kind=WorkflowDecisionKind.RETRY,
            action=WorkflowDecisionAction.RETRY,
            actor=decision.actor,
            bound_fingerprint=failure.fingerprint,
            updates={
                "stage": _retry_stage(failure.operation),
                "failure": None,
                "checkpoint": None,
            },
        )
        if failure.operation is WorkflowOperation.CATALOG_CONTEXT_READ:
            return self._retrieve_context(retried)
        if failure.operation is WorkflowOperation.INTENT_RESOLUTION:
            return self._resolve_intent(retried)
        if failure.operation is WorkflowOperation.SEMANTIC_RESOLUTION:
            return self._resolve_semantics(retried)
        if failure.operation is WorkflowOperation.SQL_VALIDATION:
            prepared, current = self._revalidate_resolved(retried)
            if prepared is None:
                return current
            assessed = self._lookup_recipe(current)
            if assessed.stage is WorkflowStage.FAILED:
                return assessed
            if reserved_execution is None:
                return self._pause_for_execution(assessed, prepared)
            return self._execute_preview(
                assessed,
                prepared,
                should_continue=continuation,
            )
        if failure.operation is WorkflowOperation.QUERY_RECIPE_LOOKUP:
            assessed = self._lookup_recipe(retried)
            if assessed.stage is WorkflowStage.FAILED:
                return assessed
            prepared, current = self._revalidate_resolved(assessed)
            if prepared is None:
                return current
            return self._pause_for_execution(current, prepared)
        if failure.operation is WorkflowOperation.PREVIEW_EXECUTION:
            prepared, current = self._revalidate_resolved(retried)
            if prepared is None:
                return current
            return self._execute_preview(
                current,
                prepared,
                should_continue=continuation,
            )
        if failure.operation is WorkflowOperation.REJECTION_INSPECTION:
            prepared, current = self._revalidate_resolved(retried)
            if prepared is None:
                return current
            return self._inspect_rejections(
                current,
                prepared,
                should_continue=continuation,
            )
        if failure.operation is WorkflowOperation.CONTEXT_PUBLICATION:
            return self._publish(retried)
        raise WorkflowError(
            WorkflowErrorCode.RETRY_NOT_ALLOWED,
            "the recorded workflow operation is not retryable",
        )

    def _retrieve_context(self, draft: AgentWorkflowDraft) -> AgentWorkflowDraft:
        started = self._begin_external(
            draft,
            stage=WorkflowStage.CONTEXT_RETRIEVAL,
            operation=WorkflowOperation.CATALOG_CONTEXT_READ,
            input_refs=tuple(item.root for item in draft.requested_datasets),
        )
        try:
            assets: list[WorkflowCatalogAsset] = []
            for dataset in started.requested_datasets:
                asset = self.catalog.get_asset(dataset)
                fields = self.catalog.list_schema_fields(dataset, PageRequest(size=50))
                assets.append(
                    WorkflowCatalogAsset(
                        dataset=dataset,
                        urn=asset.urn,
                        field_count=len(fields.items),
                        partial=fields.partial or fields.next_cursor is not None,
                        schema_fingerprint=fingerprint_payload(
                            [
                                {
                                    "id": field.id.root,
                                    "field_path": field.field_path,
                                    "native_type": field.native_type,
                                    "description": field.description,
                                    "nullable": field.nullable,
                                    "is_part_of_key": field.is_part_of_key,
                                    "tags": field.tags,
                                    "glossary_terms": field.glossary_terms,
                                }
                                for field in fields.items
                            ]
                        ),
                    )
                )
        except CatalogReadError as error:
            return self._fail_external(
                started,
                WorkflowOperation.CATALOG_CONTEXT_READ,
                code=error.code.value,
                retryable=error.code.value == "catalog_unavailable",
            )
        completed = self._finish_external(
            started,
            operation=WorkflowOperation.CATALOG_CONTEXT_READ,
            stage=WorkflowStage.CONTEXT_RETRIEVAL,
            facts=(
                _fact("asset_count", len(assets)),
                _fact("catalog_source", self.catalog.source_label),
            ),
            updates={
                "context_source": self.catalog.source_label,
                "context_assets": tuple(assets),
            },
        )
        if completed.validated_request is not None:
            return self._resolve_semantics(completed)
        return self._resolve_intent(completed)

    def _resolve_intent(self, draft: AgentWorkflowDraft) -> AgentWorkflowDraft:
        started = self._begin_external(
            draft,
            stage=WorkflowStage.INTENT_RESOLUTION,
            operation=WorkflowOperation.INTENT_RESOLUTION,
            input_refs=tuple(asset.urn for asset in draft.context_assets),
        )
        try:
            preview = self.intent.preview(started.text, started.language)
        except (IntentParserError, RequestWorkflowError) as error:
            code = getattr(error, "code", "intent_resolution_failed")
            code_value = code.value if hasattr(code, "value") else str(code)
            return self._fail_external(
                started,
                WorkflowOperation.INTENT_RESOLUTION,
                code=code_value,
                retryable=code_value
                in {
                    "intent_provider_unavailable",
                    "request_context_unavailable",
                },
            )
        snapshot = _snapshot_intent_preview(preview)
        return self._finish_external(
            started,
            operation=WorkflowOperation.INTENT_RESOLUTION,
            stage=WorkflowStage.DECISION_REQUIRED,
            facts=(
                _fact("adapter", preview.adapter),
                _fact("ambiguity_count", len(preview.ambiguities)),
                _fact("alternative_count", len(preview.alternatives)),
                _fact("interpretation", preview.interpretation_fingerprint),
            ),
            updates={
                "intent": snapshot,
                "checkpoint": WorkflowCheckpoint(
                    kind=WorkflowCheckpointKind.INTENT_CONFIRMATION,
                    fingerprint=preview.interpretation_fingerprint,
                    reason="Confirm one typed interpretation before semantic resolution.",
                ),
            },
        )

    def _resolve_semantics(self, draft: AgentWorkflowDraft) -> AgentWorkflowDraft:
        if draft.validated_request is None:
            raise _invalid_transition("semantic resolution requires a confirmed typed request")
        started = self._begin_external(
            draft,
            stage=WorkflowStage.SEMANTIC_RESOLUTION,
            operation=WorkflowOperation.SEMANTIC_RESOLUTION,
            input_refs=(validated_analytical_request_fingerprint(draft.validated_request),),
        )
        validated_request = started.validated_request
        assert validated_request is not None
        try:
            resolved = self.prepare.plan(validated_request)
        except (ConnectorTargetError, PlanningPortError, SemanticResolutionError) as error:
            code = getattr(error, "code", "semantic_resolution_failed")
            code_value = code.value if hasattr(code, "value") else str(code)
            return self._fail_external(
                started,
                WorkflowOperation.SEMANTIC_RESOLUTION,
                code=code_value,
                retryable=code_value == "planning_context_unavailable",
            )
        plan_fingerprint = resolved_semantic_plan_fingerprint(resolved)
        planned = self._finish_external(
            started,
            operation=WorkflowOperation.SEMANTIC_RESOLUTION,
            stage=WorkflowStage.PLAN_READY,
            facts=(
                _fact("plan", plan_fingerprint),
                _fact("context_version", resolved.context_version),
                _fact("table_count", len(resolved.query_policy.assets)),
            ),
            updates={"resolved_plan": resolved, "plan_fingerprint": plan_fingerprint},
        )
        prepared, current = self._revalidate_resolved(planned)
        if prepared is None:
            return current
        assessed = self._lookup_recipe(current)
        if assessed.stage is WorkflowStage.FAILED:
            return assessed
        return self._pause_for_execution(assessed, prepared)

    def _lookup_recipe(self, draft: AgentWorkflowDraft) -> AgentWorkflowDraft:
        if self.recipe_assessor is None or draft.recipe_reuse is not None:
            return draft
        if (
            draft.validated_request is None
            or draft.resolved_plan is None
            or draft.plan_fingerprint is None
            or draft.query_fingerprint is None
        ):
            raise _invalid_transition("recipe lookup requires the current guarded resolved plan")
        started = self._begin_external(
            draft,
            stage=WorkflowStage.VALIDATED,
            operation=WorkflowOperation.QUERY_RECIPE_LOOKUP,
            input_refs=(draft.plan_fingerprint, draft.query_fingerprint),
        )
        validated_request = started.validated_request
        resolved_plan = started.resolved_plan
        assert validated_request is not None and resolved_plan is not None
        try:
            assessment = self.recipe_assessor.execute(
                validated_request=validated_request,
                resolved_plan=resolved_plan,
                plan_fingerprint=started.plan_fingerprint or "missing",
                query_fingerprint=started.query_fingerprint or "missing",
                source_schema_fingerprint=source_schema_fingerprint(started),
            )
        except RecipeError as error:
            return self._fail_external(
                started,
                WorkflowOperation.QUERY_RECIPE_LOOKUP,
                code=error.code.value,
                retryable=error.code is RecipeErrorCode.CATALOG_UNAVAILABLE,
            )
        return self._finish_external(
            started,
            operation=WorkflowOperation.QUERY_RECIPE_LOOKUP,
            stage=WorkflowStage.VALIDATED,
            facts=(
                _fact("reuse_status", assessment.status.value),
                _fact("recipe_id", assessment.recipe_id or "none"),
                _fact(
                    "stale_reasons",
                    ",".join(reason.value for reason in assessment.reasons) or "none",
                ),
                _fact("revalidated", assessment.revalidated),
            ),
            updates={"recipe_reuse": assessment},
        )

    def _revalidate_resolved(
        self,
        draft: AgentWorkflowDraft,
    ) -> tuple[GovernedPreparedQuery | None, AgentWorkflowDraft]:
        if (
            draft.resolved_plan is None
            or draft.plan_fingerprint is None
            or draft.validated_request is None
        ):
            raise _invalid_transition("SQL validation requires an exact resolved plan")
        current_fingerprint = resolved_semantic_plan_fingerprint(draft.resolved_plan)
        if current_fingerprint != draft.plan_fingerprint:
            raise _decision_mismatch("stored resolved plan no longer matches its fingerprint")
        started = self._begin_external(
            draft,
            stage=WorkflowStage.PLAN_READY,
            operation=WorkflowOperation.SQL_VALIDATION,
            input_refs=(draft.plan_fingerprint,),
        )
        resolved_plan = started.resolved_plan
        validated_request = started.validated_request
        assert resolved_plan is not None and validated_request is not None
        try:
            prepared = self.prepare.refresh_and_validate(
                validated_request,
                resolved_plan,
            )
        except SemanticChangeError as error:
            return None, self._fail_external(
                started,
                WorkflowOperation.SQL_VALIDATION,
                code=error.code.value,
                retryable=False,
            )
        except PlanningPortError as error:
            return None, self._fail_external(
                started,
                WorkflowOperation.SQL_VALIDATION,
                code=error.code.value,
                retryable=error.code.value == "planning_context_unavailable",
            )
        except QueryCostError as error:
            return None, self._fail_external(
                started,
                WorkflowOperation.SQL_VALIDATION,
                code=error.code.value,
                retryable=error.code
                in {QueryCostErrorCode.TIMEOUT, QueryCostErrorCode.UNAVAILABLE},
            )
        except (
            ConnectorTargetError,
            QueryCompilationError,
            SqlPolicyViolation,
            SemanticResolutionError,
        ) as error:
            code = getattr(error, "code", "sql_policy_rejected")
            code_value = code.value if hasattr(code, "value") else str(code)
            return None, self._fail_external(
                started,
                WorkflowOperation.SQL_VALIDATION,
                code=code_value,
                retryable=False,
            )
        if prepared.cost_assessment is not None:
            try:
                AssessGovernedQueryCost.require_accepted(prepared.cost_assessment)
            except QueryCostError as error:
                return None, self._fail_external(
                    started,
                    WorkflowOperation.SQL_VALIDATION,
                    code=error.code.value,
                    retryable=error.code
                    in {QueryCostErrorCode.TIMEOUT, QueryCostErrorCode.UNAVAILABLE},
                    extra_facts=_cost_trace_facts(prepared),
                )
        query_fingerprint = _query_fingerprint(prepared)
        if draft.query_fingerprint is not None and draft.query_fingerprint != query_fingerprint:
            raise _decision_mismatch(
                "deterministic query changed after approval; execution blocked"
            )
        validated = self._finish_external(
            started,
            operation=WorkflowOperation.SQL_VALIDATION,
            stage=WorkflowStage.VALIDATED,
            facts=(
                _fact("query", query_fingerprint),
                _fact("policy_status", prepared.policy_status),
                _fact("preview_limit", prepared.query.max_rows),
                *_cost_trace_facts(prepared),
            ),
            updates={"query_fingerprint": query_fingerprint},
        )
        return prepared, validated

    def _pause_for_execution(
        self,
        draft: AgentWorkflowDraft,
        prepared: GovernedPreparedQuery,
    ) -> AgentWorkflowDraft:
        del prepared
        assert draft.plan_fingerprint is not None
        return self._save_changes(
            draft,
            stage=WorkflowStage.DECISION_REQUIRED,
            checkpoint=WorkflowCheckpoint(
                kind=WorkflowCheckpointKind.EXECUTION_APPROVAL,
                fingerprint=draft.plan_fingerprint,
                reason="Approve the exact validated plan before bounded preview execution.",
            ),
        )

    def _execute_preview(
        self,
        draft: AgentWorkflowDraft,
        prepared: GovernedPreparedQuery,
        *,
        should_continue: Callable[[], bool],
    ) -> AgentWorkflowDraft:
        if draft.execution is not None:
            return self._inspect_rejections(
                draft,
                prepared,
                should_continue=should_continue,
            )
        if not should_continue():
            return draft
        started = self._begin_external(
            draft,
            stage=WorkflowStage.EXECUTION,
            operation=WorkflowOperation.PREVIEW_EXECUTION,
            input_refs=(draft.plan_fingerprint or "missing", draft.query_fingerprint or "missing"),
        )
        try:
            preview = self.execute.execute_preview(prepared)
        except SemanticChangeError as error:
            return self._fail_external(
                started,
                WorkflowOperation.PREVIEW_EXECUTION,
                code=error.code.value,
                retryable=False,
            )
        except ConnectorTargetError as error:
            return self._fail_external(
                started,
                WorkflowOperation.PREVIEW_EXECUTION,
                code=error.code.value,
                retryable=error.code.value == "connector_target_unavailable",
            )
        except QueryCostError as error:
            return self._fail_external(
                started,
                WorkflowOperation.PREVIEW_EXECUTION,
                code=error.code.value,
                retryable=error.code
                in {QueryCostErrorCode.TIMEOUT, QueryCostErrorCode.UNAVAILABLE},
                extra_facts=(
                    _cost_trace_facts_from_assessment(error.assessment)
                    if error.assessment is not None
                    else ()
                ),
            )
        except QueryPreviewTimeoutError:
            return self._fail_external(
                started,
                WorkflowOperation.PREVIEW_EXECUTION,
                code="source_timeout",
                retryable=True,
            )
        except QueryPreviewUnavailableError:
            return self._fail_external(
                started,
                WorkflowOperation.PREVIEW_EXECUTION,
                code="source_unavailable",
                retryable=True,
            )
        except QueryPreviewError:
            return self._fail_external(
                started,
                WorkflowOperation.PREVIEW_EXECUTION,
                code="source_policy_rejected",
                retryable=False,
            )
        record = _execution_record(started, preview)
        executed = self._finish_external(
            started,
            operation=WorkflowOperation.PREVIEW_EXECUTION,
            stage=WorkflowStage.EXECUTION,
            facts=(
                _fact("row_count", len(record.rows)),
                _fact("database_user", record.database_user),
                _fact("read_only", record.transaction_read_only),
                _fact("preview", record.preview_fingerprint),
            ),
            updates={"execution": record},
        )
        return self._inspect_rejections(
            executed,
            prepared,
            should_continue=should_continue,
        )

    def _inspect_rejections(
        self,
        draft: AgentWorkflowDraft,
        prepared: GovernedPreparedQuery,
        *,
        should_continue: Callable[[], bool],
    ) -> AgentWorkflowDraft:
        if draft.execution is None:
            raise _invalid_transition("rejection inspection requires a completed preview")
        if draft.execution.rejection_complete:
            return self._propose_publication(draft)
        if not should_continue():
            return draft
        started = self._begin_external(
            draft,
            stage=WorkflowStage.EXECUTION,
            operation=WorkflowOperation.REJECTION_INSPECTION,
            input_refs=(draft.execution.preview_fingerprint,),
        )
        try:
            report = self.execute.inspect_rejections(
                prepared,
                should_continue=should_continue,
            )
        except SemanticChangeError as error:
            return self._fail_external(
                started,
                WorkflowOperation.REJECTION_INSPECTION,
                code=error.code.value,
                retryable=False,
            )
        except ConnectorTargetError as error:
            return self._fail_external(
                started,
                WorkflowOperation.REJECTION_INSPECTION,
                code=error.code.value,
                retryable=error.code.value == "connector_target_unavailable",
            )
        except ProtectedSourceOperationCancelled:
            return self._fail_external(
                started,
                WorkflowOperation.REJECTION_INSPECTION,
                code="operation_cancelled",
                retryable=True,
            )
        except PlanningPortError as error:
            if error.code is PlanningPortErrorCode.REJECTION_INSPECTION_TIMEOUT:
                code = "source_timeout"
                retryable = True
            elif error.code is PlanningPortErrorCode.REJECTION_INSPECTION_UNAVAILABLE:
                code = "source_unavailable"
                retryable = True
            else:
                code = "source_policy_rejected"
                retryable = False
            return self._fail_external(
                started,
                WorkflowOperation.REJECTION_INSPECTION,
                code=code,
                retryable=retryable,
            )
        execution = _complete_rejections(started.execution, report)
        assert execution is not None
        completed = self._finish_external(
            started,
            operation=WorkflowOperation.REJECTION_INSPECTION,
            stage=WorkflowStage.EXECUTED,
            facts=(
                _fact("rejected_count", execution.rejected_count),
                _fact("inspection_truncated", execution.rejection_truncated),
                _fact("inspection_user", report.database_user or "none"),
                _fact("read_only", report.transaction_read_only is True),
            ),
            updates={"execution": execution},
        )
        return self._propose_publication(completed)

    def _propose_publication(self, draft: AgentWorkflowDraft) -> AgentWorkflowDraft:
        if draft.publication_proposal is not None:
            return draft
        if (
            draft.execution is None
            or not draft.execution.rejection_complete
            or draft.validated_request is None
            or draft.plan_fingerprint is None
        ):
            raise _invalid_transition("publication proposal requires completed governed execution")
        proposal = WorkflowPublicationProposal.create(
            workflow_id=draft.id,
            plan_fingerprint=draft.plan_fingerprint,
            execution_fingerprint=fingerprint_payload(draft.execution.model_dump(mode="json")),
            request_fingerprint=validated_analytical_request_fingerprint(draft.validated_request),
        )
        return self._save_changes(
            draft,
            stage=WorkflowStage.PUBLICATION_PROPOSED,
            publication_proposal=proposal,
            checkpoint=WorkflowCheckpoint(
                kind=WorkflowCheckpointKind.PUBLICATION_DECISION,
                fingerprint=proposal.fingerprint,
                reason="Explicitly publish or skip the bounded execution-context proposal.",
            ),
        )

    def _publish(self, draft: AgentWorkflowDraft) -> AgentWorkflowDraft:
        proposal = draft.publication_proposal
        approval = draft.publication_approval
        if proposal is None or approval is None:
            raise _invalid_transition("publication requires an explicit typed approval")
        started = self._begin_external(
            draft,
            stage=WorkflowStage.PUBLICATION_PROPOSED,
            operation=WorkflowOperation.CONTEXT_PUBLICATION,
            input_refs=(proposal.fingerprint, proposal.idempotency_key),
        )
        try:
            result = self.publisher.publish(proposal, approval)
        except WorkflowError as error:
            return self._fail_external(
                started,
                WorkflowOperation.CONTEXT_PUBLICATION,
                code=error.code.value,
                retryable=error.code is WorkflowErrorCode.PUBLICATION_FAILED,
            )
        if result.idempotency_key != proposal.idempotency_key:
            raise _decision_mismatch("publisher returned a different idempotency key")
        try:
            validate_publication_audit_binding(
                result.audit_records,
                approval_id=approval.id,
                actor=approval.actor,
                approved_at=approval.approved_at,
                new_fingerprint=proposal.fingerprint,
            )
            self.audit_store.append(result.audit_records)
        except ValueError:
            return self._fail_external(
                started,
                WorkflowOperation.CONTEXT_PUBLICATION,
                code="workflow_publication_invalid_audit",
                retryable=False,
            )
        except PublicationAuditStoreError:
            return self._fail_external(
                started,
                WorkflowOperation.CONTEXT_PUBLICATION,
                code="workflow_audit_store_failed",
                retryable=True,
            )
        if result.status is WorkflowPublicationStatus.FAILED:
            assert result.failure_code is not None
            return self._fail_external(
                started,
                WorkflowOperation.CONTEXT_PUBLICATION,
                code=result.failure_code,
                retryable=True,
                updates={"publication_result": result},
            )
        return self._finish_external(
            started,
            operation=WorkflowOperation.CONTEXT_PUBLICATION,
            stage=WorkflowStage.PUBLICATION_COMPLETED,
            facts=(
                _fact("publication_status", result.status.value),
                _fact("document_ref", result.document_ref),
                _fact("idempotency_key", result.idempotency_key),
            ),
            updates={"publication_result": result},
        )

    def _begin_external(
        self,
        draft: AgentWorkflowDraft,
        *,
        stage: WorkflowStage,
        operation: WorkflowOperation,
        input_refs: tuple[str, ...],
    ) -> AgentWorkflowDraft:
        now = self.clock.now()
        event = WorkflowTraceEvent(
            sequence=len(draft.trace) + 1,
            stage=stage,
            operation=operation,
            status=WorkflowTraceStatus.STARTED,
            occurred_at=now,
            input_refs=input_refs,
        )
        return self._save_changes(
            draft,
            stage=stage,
            checkpoint=None,
            failure=None,
            trace=(*draft.trace, event),
        )

    def _finish_external(
        self,
        draft: AgentWorkflowDraft,
        *,
        operation: WorkflowOperation,
        stage: WorkflowStage,
        facts: tuple[WorkflowTraceFact, ...],
        updates: dict[str, object],
    ) -> AgentWorkflowDraft:
        now = self.clock.now()
        event = WorkflowTraceEvent(
            sequence=len(draft.trace) + 1,
            stage=stage,
            operation=operation,
            status=WorkflowTraceStatus.SUCCEEDED,
            occurred_at=now,
            facts=facts,
            duration_ms=_duration_ms(draft.trace[-1].occurred_at, now),
        )
        return self._save_changes(
            draft,
            **updates,
            stage=stage,
            trace=(*draft.trace, event),
        )

    def _fail_external(
        self,
        draft: AgentWorkflowDraft,
        operation: WorkflowOperation,
        *,
        code: str,
        retryable: bool,
        updates: dict[str, object] | None = None,
        extra_facts: tuple[WorkflowTraceFact, ...] = (),
    ) -> AgentWorkflowDraft:
        now = self.clock.now()
        attempt = sum(
            event.operation is operation and event.status is WorkflowTraceStatus.STARTED
            for event in draft.trace
        )
        failure = WorkflowFailure.create(
            code=_normalize_error_code(code),
            operation=operation,
            retryable=retryable,
            attempt=attempt,
            occurred_at=now,
        )
        event = WorkflowTraceEvent(
            sequence=len(draft.trace) + 1,
            stage=draft.stage,
            operation=operation,
            status=WorkflowTraceStatus.FAILED,
            occurred_at=now,
            facts=(
                _fact("error_code", failure.code),
                _fact("retryable", failure.retryable),
                *extra_facts,
            ),
            duration_ms=_duration_ms(draft.trace[-1].occurred_at, now),
        )
        checkpoint = (
            WorkflowCheckpoint(
                kind=WorkflowCheckpointKind.RETRY,
                fingerprint=failure.fingerprint,
                reason="A typed retry decision is required; no fallback context is used.",
            )
            if retryable
            else None
        )
        return self._save_changes(
            draft,
            **(updates or {}),
            stage=WorkflowStage.FAILED,
            failure=failure,
            checkpoint=checkpoint,
            trace=(*draft.trace, event),
        )

    def _pause_for_retry(
        self,
        draft: AgentWorkflowDraft,
        operation: WorkflowOperation,
    ) -> AgentWorkflowDraft:
        now = self.clock.now()
        attempt = (
            sum(
                event.operation is operation and event.status is WorkflowTraceStatus.STARTED
                for event in draft.trace
            )
            + 1
        )
        failure = WorkflowFailure.create(
            code="workflow_resume_required",
            operation=operation,
            retryable=True,
            attempt=attempt,
            occurred_at=now,
        )
        return self._save_changes(
            draft,
            stage=WorkflowStage.FAILED,
            failure=failure,
            checkpoint=WorkflowCheckpoint(
                kind=WorkflowCheckpointKind.RETRY,
                fingerprint=failure.fingerprint,
                reason="The prior process stopped between durable stages; retry explicitly.",
            ),
        )

    def _record_decision(
        self,
        draft: AgentWorkflowDraft,
        *,
        kind: WorkflowDecisionKind,
        action: WorkflowDecisionAction,
        actor: str,
        bound_fingerprint: str,
        updates: dict[str, object],
    ) -> AgentWorkflowDraft:
        now = self.clock.now()
        decision = WorkflowDecisionRecord(
            kind=kind,
            action=action,
            actor=actor,
            decided_at=now,
            bound_fingerprint=bound_fingerprint,
        )
        event = WorkflowTraceEvent(
            sequence=len(draft.trace) + 1,
            stage=draft.stage,
            operation=WorkflowOperation.HUMAN_DECISION,
            status=WorkflowTraceStatus.SUCCEEDED,
            occurred_at=now,
            facts=(
                _fact("decision_kind", kind.value),
                _fact("decision_action", action.value),
                _fact("actor", actor),
            ),
            duration_ms=0,
        )
        return self._save_changes(
            draft,
            **updates,
            decisions=(*draft.decisions, decision),
            trace=(*draft.trace, event),
        )

    def _require_checkpoint(
        self,
        draft: AgentWorkflowDraft,
        kind: WorkflowCheckpointKind,
    ) -> None:
        if (
            draft.stage
            not in {
                WorkflowStage.DECISION_REQUIRED,
                WorkflowStage.PUBLICATION_PROPOSED,
            }
            or draft.checkpoint is None
            or draft.checkpoint.kind is not kind
        ):
            raise _invalid_transition(f"workflow is not paused for {kind.value}")

    def _load(self, workflow_id: str) -> AgentWorkflowDraft:
        draft = self.store.load(workflow_id)
        if draft is None:
            raise WorkflowError(WorkflowErrorCode.NOT_FOUND, "workflow draft was not found")
        return draft

    def _save_changes(self, draft: AgentWorkflowDraft, **updates: object) -> AgentWorkflowDraft:
        payload = draft.model_dump(mode="python")
        payload.update(updates)
        payload["revision"] = draft.revision + 1
        payload["updated_at"] = self.clock.now()
        updated = AgentWorkflowDraft.model_validate(payload)
        self.store.save(updated, expected_revision=draft.revision)
        return updated


def _snapshot_intent_preview(preview: IntentPreview) -> WorkflowIntentSnapshot:
    return WorkflowIntentSnapshot(
        language=preview.language,
        adapter=preview.adapter,
        vocabulary=preview.vocabulary,
        interpretation_fingerprint=preview.interpretation_fingerprint,
        proposed_request=preview.proposed_request,
        ambiguities=preview.ambiguities,
        alternatives=tuple(
            WorkflowIntentAlternative(
                id=item.id,
                label=item.label,
                rationale=item.rationale,
                resolves=item.resolves,
                request=item.request,
            )
            for item in preview.alternatives
        ),
        findings=preview.findings,
    )


def _restore_intent_preview(snapshot: WorkflowIntentSnapshot) -> IntentPreview:
    return IntentPreview(
        language=snapshot.language,
        adapter=snapshot.adapter,
        vocabulary=snapshot.vocabulary,
        interpretation_fingerprint=snapshot.interpretation_fingerprint,
        proposed_request=snapshot.proposed_request,
        ambiguities=snapshot.ambiguities,
        alternatives=tuple(
            IntentAlternative(
                id=item.id,
                label=item.label,
                rationale=item.rationale,
                resolves=item.resolves,
                request=item.request,
            )
            for item in snapshot.alternatives
        ),
        findings=snapshot.findings,
    )


def _query_fingerprint(prepared: GovernedPreparedQuery) -> str:
    return fingerprint_payload(
        {
            "sql": prepared.query.sql,
            "parameters": [_to_scalar(item) for item in prepared.query.parameters],
            "max_rows": prepared.query.max_rows,
            "statement_timeout_ms": prepared.query.statement_timeout_ms,
            "dialect": prepared.query.dialect.value,
            "target_fingerprint": prepared.query.target_fingerprint,
        }
    )


def _cost_trace_facts(
    prepared: GovernedPreparedQuery,
) -> tuple[WorkflowTraceFact, ...]:
    return _cost_trace_facts_from_assessment(prepared.cost_assessment)


def _cost_trace_facts_from_assessment(
    assessment: QueryCostAssessment | None,
) -> tuple[WorkflowTraceFact, ...]:
    if assessment is None:
        return ()
    return (
        _fact("cost_decision", assessment.decision.value),
        _fact("cost_assessment", assessment.fingerprint),
        _fact("estimated_total_cost", assessment.total_cost),
        _fact("estimated_root_rows", assessment.estimated_root_rows),
        _fact("plan_node_count", assessment.plan_node_count),
    )


def _execution_record(
    draft: AgentWorkflowDraft,
    preview: QueryPreviewResult,
) -> WorkflowExecutionRecord:
    if draft.plan_fingerprint is None or draft.query_fingerprint is None:
        raise _invalid_transition("preview result lacks its approved plan/query fingerprint")
    rows = tuple(tuple(_to_scalar(value) for value in row) for row in preview.rows)
    preview_payload = {
        "columns": preview.columns,
        "rows": rows,
        "database_user": preview.database_user,
        "transaction_read_only": preview.transaction_read_only,
        "statement_timeout_ms": preview.statement_timeout_ms,
        "truncated": preview.truncated,
    }
    return WorkflowExecutionRecord(
        plan_fingerprint=draft.plan_fingerprint,
        query_fingerprint=draft.query_fingerprint,
        columns=preview.columns,
        rows=rows,
        row_count=len(rows),
        database_user=preview.database_user,
        transaction_read_only=preview.transaction_read_only,
        statement_timeout_ms=preview.statement_timeout_ms,
        truncated=preview.truncated,
        preview_fingerprint=fingerprint_payload(preview_payload),
    )


def _complete_rejections(
    execution: WorkflowExecutionRecord | None,
    report: RejectedSourceReport,
) -> WorkflowExecutionRecord | None:
    if execution is None:
        return None
    codes = tuple(record.code.value for record in report.records)
    return WorkflowExecutionRecord.model_validate(
        {
            **execution.model_dump(mode="python"),
            "rejection_codes": codes,
            "rejected_count": report.total_records,
            "rejection_truncated": report.truncated,
            "rejection_complete": True,
        }
    )


def _to_scalar(value: object) -> WorkflowScalar:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, date | datetime | Decimal):
        return str(value)
    raise WorkflowError(
        WorkflowErrorCode.INVALID_TRANSITION,
        "preview returned a value that cannot be stored in bounded workflow state",
    )


def _fact(key: str, value: object) -> WorkflowTraceFact:
    rendered = str(value)
    if not rendered:
        rendered = "none"
    return WorkflowTraceFact(key=key, value=rendered[:200])


def _duration_ms(started_at: datetime, finished_at: datetime) -> int:
    return max(0, int((finished_at - started_at).total_seconds() * 1_000))


def _normalize_error_code(value: str) -> str:
    normalized = "".join(character if character.isalnum() else "_" for character in value.lower())
    normalized = normalized.strip("_")[:64]
    if len(normalized) < 2 or not normalized[0].isalpha():
        return "workflow_external_failure"
    return normalized


def _error_code(error: Exception, fallback: str) -> str:
    code = getattr(error, "code", fallback)
    return code.value if hasattr(code, "value") else str(code)


def _retry_stage(operation: WorkflowOperation) -> WorkflowStage:
    return {
        WorkflowOperation.CATALOG_CONTEXT_READ: WorkflowStage.CREATED,
        WorkflowOperation.INTENT_RESOLUTION: WorkflowStage.CONTEXT_RETRIEVAL,
        WorkflowOperation.SEMANTIC_RESOLUTION: WorkflowStage.SEMANTIC_RESOLUTION,
        WorkflowOperation.SQL_VALIDATION: WorkflowStage.PLAN_READY,
        WorkflowOperation.QUERY_RECIPE_LOOKUP: WorkflowStage.VALIDATED,
        WorkflowOperation.PREVIEW_EXECUTION: WorkflowStage.VALIDATED,
        WorkflowOperation.REJECTION_INSPECTION: WorkflowStage.EXECUTION,
        WorkflowOperation.CONTEXT_PUBLICATION: WorkflowStage.PUBLICATION_PROPOSED,
    }.get(operation, WorkflowStage.FAILED)


def workflow_recovery_operation(draft: AgentWorkflowDraft) -> WorkflowOperation | None:
    """Describe the exact state-repair operation required, without mutating the draft."""

    if draft.trace and draft.trace[-1].status is WorkflowTraceStatus.STARTED:
        return draft.trace[-1].operation
    pending = _pending_resume_operation(draft)
    if pending is not None:
        return pending
    if (
        draft.stage is WorkflowStage.VALIDATED
        and draft.checkpoint is None
        and draft.plan_fingerprint is not None
    ):
        return WorkflowOperation.HUMAN_DECISION
    if draft.stage is WorkflowStage.EXECUTED:
        return WorkflowOperation.HUMAN_DECISION
    return None


def _pending_resume_operation(draft: AgentWorkflowDraft) -> WorkflowOperation | None:
    if draft.stage is WorkflowStage.CREATED:
        return WorkflowOperation.CATALOG_CONTEXT_READ
    if draft.stage is WorkflowStage.CONTEXT_RETRIEVAL and draft.context_assets:
        return WorkflowOperation.INTENT_RESOLUTION
    if draft.stage is WorkflowStage.SEMANTIC_RESOLUTION and draft.validated_request is not None:
        return WorkflowOperation.SEMANTIC_RESOLUTION
    if draft.stage is WorkflowStage.PLAN_READY and draft.resolved_plan is not None:
        return WorkflowOperation.SQL_VALIDATION
    if draft.stage is WorkflowStage.EXECUTION:
        return (
            WorkflowOperation.REJECTION_INSPECTION
            if draft.execution is not None
            else WorkflowOperation.PREVIEW_EXECUTION
        )
    return None


def _invalid_transition(message: str) -> WorkflowError:
    return WorkflowError(WorkflowErrorCode.INVALID_TRANSITION, message)


def _decision_mismatch(message: str) -> WorkflowError:
    return WorkflowError(WorkflowErrorCode.DECISION_MISMATCH, message)
