"""Focused tests for the explicit, durable M12 workflow state machine."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from schemabridge.adapters.datahub.recorded import RecordedCatalogAdapter
from schemabridge.adapters.language.fake import FakeIntentParser
from schemabridge.adapters.planning.recorded import RecordedSemanticPlanningContext
from schemabridge.adapters.requests.recorded_context import RecordedRequestContextAdapter
from schemabridge.adapters.semantic_registry.memory import (
    InMemoryGovernedSemanticRegistry,
)
from schemabridge.adapters.sql.compiler import PostgresQueryCompiler
from schemabridge.adapters.sql.guard import SqlGlotPolicyGuard
from schemabridge.adapters.storage.publication_audit import InMemoryPublicationAuditStore
from schemabridge.adapters.storage.workflows import SqliteWorkflowDraftStore
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
from schemabridge.application.intent_resolution import ResolveNaturalLanguageIntent
from schemabridge.application.ports.catalog import (
    CatalogUnavailableError,
    PageRequest,
)
from schemabridge.application.ports.planning import PlanningPortError, PlanningPortErrorCode
from schemabridge.application.ports.workflows import WorkflowError, WorkflowErrorCode
from schemabridge.application.query_execution import (
    CompiledQuery,
    QueryPreviewError,
    QueryPreviewRejectedError,
    QueryPreviewResult,
    QueryPreviewTimeoutError,
    QueryPreviewUnavailableError,
    ValidatedQuery,
)
from schemabridge.application.semantic_change import AssertSemanticContextCurrent
from schemabridge.application.workflow_orchestration import AgentWorkflowOrchestrator
from schemabridge.bootstrap import build_semantic_registry
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.intents import IntentAlternativeId, UserLanguage
from schemabridge.domain.plans import QueryPlan
from schemabridge.domain.publication_audit import (
    PublicationAuditOutcome,
    PublicationFamily,
    PublicationTargetAuditRecord,
)
from schemabridge.domain.query_studio import (
    ConfirmedQueryStudioRequest,
    DescriptionQuery,
)
from schemabridge.domain.resolution import (
    RejectedSourceRecord,
    RejectedSourceReport,
    RejectionCheck,
    ResolutionLimits,
    SourceRejectionCode,
)
from schemabridge.domain.semantic_change import (
    SemanticChangeKind,
    SemanticChangeStatus,
    SemanticContextGateAssessment,
    SemanticPlanDependencies,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope
from schemabridge.domain.workflows import (
    AgentWorkflowDraft,
    ExecutionWorkflowDecision,
    IntentWorkflowDecision,
    PublicationWorkflowDecision,
    RetryWorkflowDecision,
    StartWorkflowCommand,
    WorkflowCheckpointKind,
    WorkflowDecisionAction,
    WorkflowDecisionKind,
    WorkflowExecutionRecord,
    WorkflowOperation,
    WorkflowPublicationApproval,
    WorkflowPublicationConfirmation,
    WorkflowPublicationProposal,
    WorkflowPublicationResult,
    WorkflowPublicationStatus,
    WorkflowStage,
    WorkflowTraceEvent,
    WorkflowTraceStatus,
)

ROOT = Path(__file__).resolve().parents[2]
TEXT = "Agrupa por fecha de registro todos los clientes que sean segundo titular de una cuenta."
DATASETS = (
    PhysicalDatasetRef("crm.customers"),
    PhysicalDatasetRef("bank.account_holders"),
)


@dataclass(slots=True)
class FakeClock:
    current: datetime = datetime(2026, 7, 21, 10, 0, tzinfo=UTC)

    def now(self) -> datetime:
        result = self.current
        self.current += timedelta(milliseconds=10)
        return result


@dataclass(slots=True)
class MemoryWorkflowStore:
    drafts: dict[str, AgentWorkflowDraft] = field(default_factory=dict)

    def load(self, workflow_id: str) -> AgentWorkflowDraft | None:
        return self.drafts.get(workflow_id)

    def save(
        self,
        draft: AgentWorkflowDraft,
        *,
        expected_revision: int | None,
    ) -> None:
        current = self.drafts.get(draft.id)
        if expected_revision is None:
            if current is not None:
                raise WorkflowError(WorkflowErrorCode.CONFLICT, "duplicate")
        elif current is None or current.revision != expected_revision:
            raise WorkflowError(WorkflowErrorCode.CONFLICT, "stale")
        self.drafts[draft.id] = draft


@dataclass(slots=True)
class FakePreview:
    calls: int = 0
    error: QueryPreviewError | None = None

    def execute(self, query: ValidatedQuery) -> QueryPreviewResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return QueryPreviewResult(
            columns=("registration_date", "secondary_holder_customers"),
            rows=(("2026-01-01", 2), ("2026-01-02", 1)),
            database_user="schemabridge_reader",
            transaction_read_only=True,
            statement_timeout_ms=query.statement_timeout_ms,
            truncated=False,
        )


@dataclass(slots=True)
class FakeRejectionReporter:
    fail_once: bool = False
    calls: int = 0
    sample_codes: tuple[SourceRejectionCode, ...] = ()
    total_records: int | None = None

    def inspect(
        self,
        checks: tuple[RejectionCheck, ...],
        *,
        statement_timeout_ms: int,
        should_continue: Callable[[], bool] | None = None,
    ) -> RejectedSourceReport:
        assert should_continue is None or should_continue()
        self.calls += 1
        if self.fail_once and self.calls == 1:
            raise PlanningPortError(
                PlanningPortErrorCode.REJECTION_INSPECTION_UNAVAILABLE,
                "temporary synthetic inspection failure",
            )
        records = tuple(
            RejectedSourceRecord(
                logical_field=checks[index % len(checks)].logical_field,
                physical_field=checks[index % len(checks)].physical_field,
                source_value=f"synthetic-rejected-{index + 1}",
                code=code,
                reason="Synthetic bounded rejection sample.",
            )
            for index, code in enumerate(self.sample_codes)
        )
        total_records = len(records) if self.total_records is None else self.total_records
        return RejectedSourceReport(
            inspected_fields=tuple(check.physical_field for check in checks),
            records=records,
            total_records=total_records,
            truncated=total_records > len(records),
            database_user="schemabridge_reader",
            transaction_read_only=True,
            statement_timeout_ms=statement_timeout_ms,
        )


@dataclass(slots=True)
class SwitchableRegistry:
    delegate: InMemoryGovernedSemanticRegistry
    fail: bool = False

    @property
    def scope(self):  # type: ignore[no-untyped-def]
        return self.delegate.scope

    def load(self):  # type: ignore[no-untyped-def]
        if self.fail:
            raise PlanningPortError(
                PlanningPortErrorCode.CONTEXT_UNAVAILABLE,
                "synthetic live registry outage",
            )
        return self.delegate.load()


@dataclass(slots=True)
class ActivePlanningRegistry:
    delegate: InMemoryGovernedSemanticRegistry

    @property
    def scope(self) -> SemanticRegistryScope:
        return self.delegate.scope

    def load(self):  # type: ignore[no-untyped-def]
        return self.delegate.load().model_copy(
            update={
                "activation_generation": 7,
                "active_pointer_fingerprint": "a" * 64,
            }
        )


@dataclass(slots=True)
class BlockedSemanticGate:
    calls: int = 0

    def assess(
        self,
        dependencies: SemanticPlanDependencies,
    ) -> SemanticContextGateAssessment:
        self.calls += 1
        return SemanticContextGateAssessment(
            dependencies_fingerprint=dependencies.fingerprint,
            eligible=False,
            status=SemanticChangeStatus.BLOCKED,
            reason_codes=(SemanticChangeKind.PHYSICAL_TYPE_CHANGED,),
            baseline_revision=3,
        )


@dataclass(slots=True)
class CountingCompiler:
    delegate: PostgresQueryCompiler = field(default_factory=PostgresQueryCompiler)
    calls: int = 0

    def compile(self, plan: QueryPlan, *, max_preview_rows: int) -> CompiledQuery:
        self.calls += 1
        return self.delegate.compile(plan, max_preview_rows=max_preview_rows)


@dataclass(slots=True)
class CountingPublisher:
    fail_once: bool = False
    calls: int = 0

    def publish(
        self,
        proposal: WorkflowPublicationProposal,
        approval: WorkflowPublicationApproval,
    ) -> WorkflowPublicationResult:
        assert approval.proposal_fingerprint == proposal.fingerprint
        self.calls += 1
        status = WorkflowPublicationStatus.CREATED
        failure_code = None
        if self.fail_once and self.calls == 1:
            status = WorkflowPublicationStatus.FAILED
            failure_code = "synthetic_publication_failure"
        document_ref = f"fake://workflow-context/{proposal.idempotency_key}"
        return WorkflowPublicationResult(
            status=status,
            approval_id=approval.id,
            proposal_fingerprint=proposal.fingerprint,
            idempotency_key=proposal.idempotency_key,
            document_ref=document_ref,
            published_at=datetime(2026, 7, 21, 10, 30, tzinfo=UTC),
            failure_code=failure_code,
            audit_record=PublicationTargetAuditRecord(
                family=PublicationFamily.WORKFLOW,
                operation="upsert_document",
                target=document_ref,
                approval_id=approval.id,
                actor=approval.actor,
                approved_at=approval.approved_at,
                previous_fingerprint=None,
                new_fingerprint=proposal.fingerprint,
                outcome=(
                    PublicationAuditOutcome.FAILED
                    if failure_code is not None
                    else PublicationAuditOutcome.SUCCEEDED
                ),
                reason_code=failure_code,
            ),
        )


class FailOnceCatalog:
    def __init__(self, delegate: RecordedCatalogAdapter) -> None:
        self.delegate = delegate
        self.calls = 0

    @property
    def source_label(self) -> str:
        return self.delegate.source_label

    def get_asset(self, dataset: PhysicalDatasetRef):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls == 1:
            raise CatalogUnavailableError("get_asset", "temporary catalog outage")
        return self.delegate.get_asset(dataset)

    def list_schema_fields(self, dataset: PhysicalDatasetRef, page: PageRequest):  # type: ignore[no-untyped-def]
        return self.delegate.list_schema_fields(dataset, page)


def _build(
    *,
    store: MemoryWorkflowStore | SqliteWorkflowDraftStore | None = None,
    catalog: object | None = None,
    preview: FakePreview | None = None,
    reporter: FakeRejectionReporter | None = None,
    publisher: CountingPublisher | None = None,
    clock: FakeClock | None = None,
    audit_store: InMemoryPublicationAuditStore | None = None,
) -> AgentWorkflowOrchestrator:
    logical = ROOT / "demo/ground_truth/approved_logical_context.yml"
    resolved_preview = preview or FakePreview()
    resolved_reporter = reporter or FakeRejectionReporter()
    prepare = PrepareGovernedRequest(
        planner=PlanSemanticRequest(
            RecordedSemanticPlanningContext(
                logical,
                ROOT / "demo/ground_truth/planning_mappings.yml",
                ROOT / "demo/ground_truth/join_contracts.yml",
            ),
            ResolutionLimits(),
        ),
        compiler=PostgresQueryCompiler(),
        guard=SqlGlotPolicyGuard(),
    )
    return AgentWorkflowOrchestrator(
        store=store or MemoryWorkflowStore(),
        clock=clock or FakeClock(),
        catalog=catalog  # type: ignore[arg-type]
        or RecordedCatalogAdapter(ROOT / "demo/datahub/catalog_snapshot.json"),
        intent=ResolveNaturalLanguageIntent(
            parser=FakeIntentParser(),
            context=RecordedRequestContextAdapter(logical),
            adapter_label="fake:typed-intent-only",
        ),
        prepare=prepare,
        execute=ExecuteGovernedRequest(
            prepare=prepare,
            executor=resolved_preview,
            rejection_reporter=resolved_reporter,
        ),
        publisher=publisher or CountingPublisher(),
        audit_store=audit_store or InMemoryPublicationAuditStore(),
    )


def _start(orchestrator: AgentWorkflowOrchestrator, workflow_id: str = "workflow-test"):
    return orchestrator.start(
        StartWorkflowCommand(
            id=workflow_id,
            text=TEXT,
            language=UserLanguage.SPANISH,
            datasets=DATASETS,
        )
    )


def test_confirmed_query_studio_request_derives_catalog_assets_without_legacy_intent() -> None:
    orchestrator = _build()
    validated = BuildGuidedRequest(
        RecordedRequestContextAdapter(ROOT / "demo/ground_truth/approved_logical_context.yml")
    ).execute(build_demo_guided_input(GuidedRequestCase.NORTH_STAR))

    draft = orchestrator.start_confirmed(
        "query-studio-confirmed",
        ConfirmedQueryStudioRequest(
            original_text=DescriptionQuery(TEXT),
            language=UserLanguage.SPANISH,
            validated_request=validated,
            preview_fingerprint="a" * 64,
            confirmation_fingerprint="b" * 64,
        ),
    )

    assert draft.intent is None
    assert draft.validated_request == validated
    assert draft.requested_datasets == DATASETS
    assert tuple(item.dataset for item in draft.context_assets) == DATASETS
    assert draft.stage is WorkflowStage.DECISION_REQUIRED
    assert draft.checkpoint is not None
    assert draft.checkpoint.kind is WorkflowCheckpointKind.EXECUTION_APPROVAL
    assert draft.resolved_plan is not None
    assert len(draft.resolved_plan.query_policy.assets) == 2


def test_workflow_decisions_reject_whitespace_only_actor() -> None:
    with pytest.raises(ValidationError, match="workflow actor must not be blank"):
        IntentWorkflowDecision(
            actor=" ",
            interpretation_fingerprint="a" * 64,
            selected_alternative=IntentAlternativeId.COUNT_DISTINCT_CUSTOMERS,
        )


def _confirm_intent(orchestrator: AgentWorkflowOrchestrator, draft: AgentWorkflowDraft):
    assert draft.intent is not None
    return orchestrator.decide_intent(
        draft.id,
        IntentWorkflowDecision(
            actor="operator@example.test",
            interpretation_fingerprint=draft.intent.interpretation_fingerprint,
            selected_alternative=IntentAlternativeId.COUNT_DISTINCT_CUSTOMERS,
        ),
    )


def _approve_execution(orchestrator: AgentWorkflowOrchestrator, draft: AgentWorkflowDraft):
    assert draft.plan_fingerprint is not None
    return orchestrator.decide_execution(
        draft.id,
        ExecutionWorkflowDecision(
            actor="operator@example.test",
            plan_fingerprint=draft.plan_fingerprint,
            action=WorkflowDecisionAction.APPROVE,
        ),
    )


def test_workflow_enforces_checkpoints_resumes_and_emits_only_safe_trace() -> None:
    store = MemoryWorkflowStore()
    preview = FakePreview()
    reporter = FakeRejectionReporter()
    publisher = CountingPublisher()
    orchestrator = _build(
        store=store,
        preview=preview,
        reporter=reporter,
        publisher=publisher,
    )
    paused = _start(orchestrator)

    assert paused.stage is WorkflowStage.DECISION_REQUIRED
    assert paused.checkpoint is not None
    assert paused.checkpoint.kind is WorkflowCheckpointKind.INTENT_CONFIRMATION
    with pytest.raises(WorkflowError) as captured:
        orchestrator.decide_execution(
            paused.id,
            ExecutionWorkflowDecision(
                actor="operator@example.test",
                plan_fingerprint="0" * 64,
                action=WorkflowDecisionAction.APPROVE,
            ),
        )
    assert captured.value.code is WorkflowErrorCode.INVALID_TRANSITION

    restarted = _build(
        store=store,
        preview=preview,
        reporter=reporter,
        publisher=publisher,
    )
    assert restarted.resume(paused.id) == paused
    validated = _confirm_intent(restarted, paused)
    assert validated.stage is WorkflowStage.DECISION_REQUIRED
    assert validated.checkpoint is not None
    assert validated.checkpoint.kind is WorkflowCheckpointKind.EXECUTION_APPROVAL
    assert validated.resolved_plan is not None

    proposed = _approve_execution(restarted, validated)
    assert proposed.stage is WorkflowStage.PUBLICATION_PROPOSED
    assert proposed.execution is not None and proposed.execution.rejection_complete
    assert preview.calls == 1
    assert reporter.calls == 1
    assert restarted.resume(proposed.id) == proposed
    assert preview.calls == 1

    trace_json = json.dumps(
        [event.model_dump(mode="json") for event in proposed.trace],
        sort_keys=True,
    ).lower()
    assert TEXT.casefold() not in trace_json
    assert "select " not in trace_json
    assert "chain_of_thought" not in trace_json
    assert "password" not in trace_json
    external = {
        event.operation
        for event in proposed.trace
        if event.operation is not WorkflowOperation.HUMAN_DECISION
    }
    for operation in external:
        statuses = [event.status for event in proposed.trace if event.operation is operation]
        assert statuses.count(WorkflowTraceStatus.STARTED) == statuses.count(
            WorkflowTraceStatus.SUCCEEDED
        )
    assert (
        sum(
            event.operation is WorkflowOperation.SQL_VALIDATION
            and event.status is WorkflowTraceStatus.SUCCEEDED
            for event in proposed.trace
        )
        == 2
    )


def test_cancellation_between_preview_and_rejection_stops_before_second_source_read() -> None:
    preview = FakePreview()
    reporter = FakeRejectionReporter()
    orchestrator = _build(preview=preview, reporter=reporter)
    validated = _confirm_intent(orchestrator, _start(orchestrator))
    assert validated.plan_fingerprint is not None
    checks = iter((True, False))

    partial = orchestrator.decide_execution(
        validated.id,
        ExecutionWorkflowDecision(
            actor="operator@example.test",
            plan_fingerprint=validated.plan_fingerprint,
            action=WorkflowDecisionAction.APPROVE,
        ),
        should_continue=lambda: next(checks),
    )

    assert preview.calls == 1
    assert reporter.calls == 0
    assert partial.stage is WorkflowStage.EXECUTION
    assert partial.execution is not None
    assert partial.execution.rejection_complete is False
    assert partial.publication_proposal is None


def test_typed_catalog_retry_never_falls_back_or_advances_on_plain_resume() -> None:
    store = MemoryWorkflowStore()
    catalog = FailOnceCatalog(RecordedCatalogAdapter(ROOT / "demo/datahub/catalog_snapshot.json"))
    orchestrator = _build(store=store, catalog=catalog)

    failed = _start(orchestrator, "workflow-catalog-retry")
    assert failed.stage is WorkflowStage.FAILED
    assert failed.failure is not None and failed.failure.retryable
    assert failed.failure.operation is WorkflowOperation.CATALOG_CONTEXT_READ
    assert failed.context_assets == ()
    assert orchestrator.resume(failed.id) == failed
    assert catalog.calls == 1

    retried = orchestrator.retry(
        failed.id,
        RetryWorkflowDecision(
            actor="operator@example.test",
            failure_fingerprint=failed.failure.fingerprint,
            operation=failed.failure.operation,
        ),
    )
    assert retried.stage is WorkflowStage.DECISION_REQUIRED
    assert retried.context_source == "recorded:sanitized-datahub-m04"
    assert len(retried.context_assets) == 2


def test_rejection_retry_does_not_execute_preview_twice() -> None:
    preview = FakePreview()
    reporter = FakeRejectionReporter(fail_once=True)
    orchestrator = _build(preview=preview, reporter=reporter)
    validated = _confirm_intent(orchestrator, _start(orchestrator, "workflow-retry"))

    failed = _approve_execution(orchestrator, validated)
    assert failed.stage is WorkflowStage.FAILED
    assert failed.failure is not None
    assert failed.failure.operation is WorkflowOperation.REJECTION_INSPECTION
    assert failed.execution is not None
    assert preview.calls == 1

    completed = orchestrator.retry(
        failed.id,
        RetryWorkflowDecision(
            actor="operator@example.test",
            failure_fingerprint=failed.failure.fingerprint,
            operation=failed.failure.operation,
        ),
    )
    assert completed.stage is WorkflowStage.PUBLICATION_PROPOSED
    assert preview.calls == 1
    assert reporter.calls == 2


@pytest.mark.parametrize(
    ("error", "expected_code", "retryable"),
    (
        (QueryPreviewTimeoutError("synthetic timeout"), "source_timeout", True),
        (QueryPreviewUnavailableError("synthetic outage"), "source_unavailable", True),
        (QueryPreviewRejectedError("synthetic rejection"), "source_policy_rejected", False),
    ),
)
def test_preview_failures_persist_closed_typed_codes(
    error: QueryPreviewError,
    expected_code: str,
    retryable: bool,
) -> None:
    preview = FakePreview(error=error)
    orchestrator = _build(preview=preview)
    validated = _confirm_intent(
        orchestrator,
        _start(orchestrator, f"workflow-{expected_code}"),
    )

    failed = _approve_execution(orchestrator, validated)

    assert failed.stage is WorkflowStage.FAILED
    assert failed.failure is not None
    assert failed.failure.code == expected_code
    assert failed.failure.operation is WorkflowOperation.PREVIEW_EXECUTION
    assert failed.failure.retryable is retryable
    assert preview.calls == 1


def test_reserved_execution_retry_reuses_original_approval_and_completes_preview() -> None:
    preview = FakePreview(error=QueryPreviewTimeoutError("synthetic timeout"))
    orchestrator = _build(preview=preview)
    validated = _confirm_intent(
        orchestrator,
        _start(orchestrator, "workflow-reserved-execution-retry"),
    )
    failed = _approve_execution(orchestrator, validated)
    assert failed.failure is not None
    assert failed.plan_fingerprint is not None
    preview.error = None

    completed = orchestrator.retry(
        failed.id,
        RetryWorkflowDecision(
            actor="operator@example.test",
            failure_fingerprint=failed.failure.fingerprint,
            operation=failed.failure.operation,
        ),
        reserved_execution=ExecutionWorkflowDecision(
            actor="operator@example.test",
            plan_fingerprint=failed.plan_fingerprint,
            action=WorkflowDecisionAction.APPROVE,
        ),
        should_continue=lambda: True,
    )

    assert completed.stage is WorkflowStage.PUBLICATION_PROPOSED
    assert completed.failure is None
    assert completed.execution is not None
    assert preview.calls == 2
    assert (
        tuple(item.kind for item in completed.decisions).count(WorkflowDecisionKind.EXECUTION) == 1
    )
    assert tuple(item.kind for item in completed.decisions).count(WorkflowDecisionKind.RETRY) == 1


def test_truncated_rejection_report_persists_exact_total_without_source_values(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "truncated-rejections.db"
    store = SqliteWorkflowDraftStore(database_path)
    reporter = FakeRejectionReporter(
        sample_codes=(
            SourceRejectionCode.NULL_JOIN_KEY,
            SourceRejectionCode.NON_FINITE_IDENTIFIER,
        ),
        total_records=25_001,
    )
    orchestrator = _build(store=store, reporter=reporter)

    proposed = _approve_execution(
        orchestrator,
        _confirm_intent(
            orchestrator,
            _start(orchestrator, "workflow-truncated-rejections"),
        ),
    )

    assert proposed.execution is not None
    assert proposed.execution.rejected_count == 25_001
    assert proposed.execution.rejection_codes == (
        "null_join_key",
        "non_finite_identifier",
    )
    assert proposed.execution.truncated is False
    assert proposed.execution.rejection_truncated is True
    assert proposed.execution.any_truncated is True
    inconsistent = proposed.execution.model_dump(mode="python")
    inconsistent["rejection_truncated"] = False
    with pytest.raises(ValidationError, match="truncation"):
        WorkflowExecutionRecord.model_validate(inconsistent)
    restored = SqliteWorkflowDraftStore(database_path).load(proposed.id)
    assert restored is not None and restored.execution == proposed.execution
    serialized = restored.model_dump_json()
    assert "synthetic-rejected" not in serialized


def test_registry_change_after_approval_checkpoint_blocks_before_preview() -> None:
    loaded = build_semantic_registry(repository_root=ROOT).load()
    registry = InMemoryGovernedSemanticRegistry(loaded.registry, loaded.scope)
    preview = FakePreview()
    reporter = FakeRejectionReporter()
    prepare = PrepareGovernedRequest(
        planner=PlanSemanticRequest(registry, ResolutionLimits()),
        compiler=PostgresQueryCompiler(),
        guard=SqlGlotPolicyGuard(),
    )
    base = _build(preview=preview, reporter=reporter)
    orchestrator = replace(
        base,
        intent=ResolveNaturalLanguageIntent(
            parser=FakeIntentParser(),
            context=registry,
            adapter_label="fake:typed-intent-only",
        ),
        prepare=prepare,
        execute=ExecuteGovernedRequest(
            prepare=prepare,
            executor=preview,
            rejection_reporter=reporter,
        ),
    )
    validated = _confirm_intent(
        orchestrator,
        _start(orchestrator, "workflow-stale-registry"),
    )
    registry.registry = registry.registry.model_copy(
        update={"version": registry.registry.version + 1}
    )

    failed = _approve_execution(orchestrator, validated)

    assert failed.stage is WorkflowStage.FAILED
    assert failed.failure is not None
    assert failed.failure.code == "stale_registry"
    assert failed.failure.retryable is False
    assert preview.calls == 0
    assert reporter.calls == 0


def test_registry_outage_after_approval_checkpoint_fails_before_compile_or_io() -> None:
    loaded = build_semantic_registry(repository_root=ROOT).load()
    registry = SwitchableRegistry(InMemoryGovernedSemanticRegistry(loaded.registry, loaded.scope))
    compiler = CountingCompiler()
    preview = FakePreview()
    reporter = FakeRejectionReporter()
    prepare = PrepareGovernedRequest(
        planner=PlanSemanticRequest(registry, ResolutionLimits()),
        compiler=compiler,
        guard=SqlGlotPolicyGuard(),
    )
    base = _build(preview=preview, reporter=reporter)
    orchestrator = replace(
        base,
        intent=ResolveNaturalLanguageIntent(
            parser=FakeIntentParser(),
            context=registry,
            adapter_label="fake:typed-intent-only",
        ),
        prepare=prepare,
        execute=ExecuteGovernedRequest(
            prepare=prepare,
            executor=preview,
            rejection_reporter=reporter,
        ),
    )
    validated = _confirm_intent(
        orchestrator,
        _start(orchestrator, "workflow-registry-outage"),
    )
    compiled_before_outage = compiler.calls
    registry.fail = True

    failed = _approve_execution(orchestrator, validated)

    assert failed.stage is WorkflowStage.FAILED
    assert failed.failure is not None
    assert failed.failure.code == PlanningPortErrorCode.CONTEXT_UNAVAILABLE.value
    assert failed.failure.retryable is True
    assert compiler.calls == compiled_before_outage
    assert preview.calls == 0
    assert reporter.calls == 0


def test_stale_semantic_context_becomes_terminal_sanitized_workflow_failure() -> None:
    loaded = build_semantic_registry(repository_root=ROOT).load()
    registry = ActivePlanningRegistry(
        InMemoryGovernedSemanticRegistry(loaded.registry, loaded.scope)
    )
    compiler = CountingCompiler()
    preview = FakePreview()
    reporter = FakeRejectionReporter()
    gate = BlockedSemanticGate()
    prepare = PrepareGovernedRequest(
        planner=PlanSemanticRequest(registry, ResolutionLimits()),
        compiler=compiler,
        guard=SqlGlotPolicyGuard(),
        semantic_gate=AssertSemanticContextCurrent(gate),
        semantic_scope=registry.scope,
    )
    base = _build(preview=preview, reporter=reporter)
    orchestrator = replace(
        base,
        intent=ResolveNaturalLanguageIntent(
            parser=FakeIntentParser(),
            context=registry,
            adapter_label="fake:typed-intent-only",
        ),
        prepare=prepare,
        execute=ExecuteGovernedRequest(
            prepare=prepare,
            executor=preview,
            rejection_reporter=reporter,
        ),
    )

    failed = _confirm_intent(
        orchestrator,
        _start(orchestrator, "workflow-stale-semantic-evidence"),
    )

    assert failed.stage is WorkflowStage.FAILED
    assert failed.failure is not None
    assert failed.failure.code == "semantic_context_stale"
    assert failed.failure.retryable is False
    assert gate.calls == 1
    assert compiler.calls == 0
    assert preview.calls == 0
    assert reporter.calls == 0


def test_inspection_is_inert_and_explicit_recovery_creates_typed_retry() -> None:
    store = MemoryWorkflowStore()
    preview = FakePreview()
    orchestrator = _build(store=store, preview=preview)
    validated = _confirm_intent(
        orchestrator,
        _start(orchestrator, "workflow-interrupted-preview"),
    )
    interrupted = AgentWorkflowDraft.model_validate(
        {
            **validated.model_dump(mode="python"),
            "revision": validated.revision + 1,
            "stage": WorkflowStage.EXECUTION,
            "checkpoint": None,
            "trace": (
                *validated.trace,
                WorkflowTraceEvent(
                    sequence=len(validated.trace) + 1,
                    stage=WorkflowStage.EXECUTION,
                    operation=WorkflowOperation.PREVIEW_EXECUTION,
                    status=WorkflowTraceStatus.STARTED,
                    occurred_at=datetime(2026, 7, 21, 10, 0, tzinfo=UTC),
                    input_refs=(validated.plan_fingerprint or "missing",),
                ),
            ),
            "updated_at": datetime(2026, 7, 21, 10, 0, tzinfo=UTC),
        }
    )
    store.save(interrupted, expected_revision=validated.revision)

    inspected = orchestrator.resume(interrupted.id)
    assert inspected == interrupted
    assert inspected.failure is None

    with pytest.raises(WorkflowError) as changed:
        orchestrator.recover_interrupted(
            interrupted.id,
            expected_operation=WorkflowOperation.CONTEXT_PUBLICATION,
        )
    assert changed.value.code is WorkflowErrorCode.DECISION_MISMATCH
    assert orchestrator.inspect(interrupted.id) == interrupted

    failed = orchestrator.recover_interrupted(
        interrupted.id,
        expected_operation=WorkflowOperation.PREVIEW_EXECUTION,
    )
    assert failed.failure is not None
    assert failed.failure.code == "workflow_external_action_interrupted"
    assert failed.failure.operation is WorkflowOperation.PREVIEW_EXECUTION
    assert preview.calls == 0

    completed = orchestrator.retry(
        failed.id,
        RetryWorkflowDecision(
            actor="operator@example.test",
            failure_fingerprint=failed.failure.fingerprint,
            operation=failed.failure.operation,
        ),
    )
    assert completed.stage is WorkflowStage.PUBLICATION_PROPOSED
    assert preview.calls == 1


def test_publication_requires_approval_and_is_not_repeated_after_success() -> None:
    publisher = CountingPublisher()
    audit_store = InMemoryPublicationAuditStore()
    orchestrator = _build(publisher=publisher, audit_store=audit_store)
    proposed = _approve_execution(
        orchestrator,
        _confirm_intent(orchestrator, _start(orchestrator, "workflow-publish")),
    )
    assert proposed.publication_proposal is not None

    with pytest.raises(ValueError, match="confirmation"):
        PublicationWorkflowDecision(
            actor="operator@example.test",
            proposal_fingerprint=proposed.publication_proposal.fingerprint,
            action=WorkflowDecisionAction.PUBLISH,
        )
    decision = PublicationWorkflowDecision(
        actor="operator@example.test",
        proposal_fingerprint=proposed.publication_proposal.fingerprint,
        action=WorkflowDecisionAction.PUBLISH,
        confirmation=WorkflowPublicationConfirmation.PUBLISH_EXECUTION_CONTEXT,
    )
    published = orchestrator.decide_publication(proposed.id, decision)
    assert published.stage is WorkflowStage.PUBLICATION_COMPLETED
    assert publisher.calls == 1
    assert orchestrator.decide_publication(proposed.id, decision) == published
    assert publisher.calls == 1
    assert published.publication_approval is not None
    records = audit_store.list_for_approval(published.publication_approval.id)
    assert len(records) == 1
    assert records[0].outcome is PublicationAuditOutcome.SUCCEEDED


def test_publication_retry_reuses_key_without_reexecuting_preview() -> None:
    preview = FakePreview()
    publisher = CountingPublisher(fail_once=True)
    audit_store = InMemoryPublicationAuditStore()
    orchestrator = _build(
        preview=preview,
        publisher=publisher,
        audit_store=audit_store,
    )
    proposed = _approve_execution(
        orchestrator,
        _confirm_intent(orchestrator, _start(orchestrator, "workflow-publish-retry")),
    )
    assert proposed.publication_proposal is not None
    failed = orchestrator.decide_publication(
        proposed.id,
        PublicationWorkflowDecision(
            actor="operator@example.test",
            proposal_fingerprint=proposed.publication_proposal.fingerprint,
            action=WorkflowDecisionAction.PUBLISH,
            confirmation=WorkflowPublicationConfirmation.PUBLISH_EXECUTION_CONTEXT,
        ),
    )
    assert failed.failure is not None
    assert failed.failure.operation is WorkflowOperation.CONTEXT_PUBLICATION
    assert preview.calls == 1

    published = orchestrator.retry(
        failed.id,
        RetryWorkflowDecision(
            actor="operator@example.test",
            failure_fingerprint=failed.failure.fingerprint,
            operation=failed.failure.operation,
        ),
    )
    assert published.stage is WorkflowStage.PUBLICATION_COMPLETED
    assert published.publication_result is not None
    assert published.publication_result.idempotency_key == (
        proposed.publication_proposal.idempotency_key
    )
    assert publisher.calls == 2
    assert preview.calls == 1
    assert published.publication_approval is not None
    assert [
        record.outcome
        for record in audit_store.list_for_approval(published.publication_approval.id)
    ] == [PublicationAuditOutcome.FAILED, PublicationAuditOutcome.SUCCEEDED]


def test_sqlite_store_round_trips_pause_state_for_new_process(tmp_path: Path) -> None:
    path = tmp_path / "workflow.db"
    first = _build(store=SqliteWorkflowDraftStore(path))
    paused = _start(first, "workflow-durable")

    second = _build(store=SqliteWorkflowDraftStore(path))
    restored = second.resume(paused.id)

    assert restored == paused
    assert restored.intent is not None
    assert restored.checkpoint is not None
    assert restored.model_dump_json() == paused.model_dump_json()


def test_scoped_sqlite_store_creates_draft_and_owner_grant_atomically(
    tmp_path: Path,
) -> None:
    path = tmp_path / "scoped-workflow.db"
    scoped = SqliteWorkflowDraftStore(
        path,
        workspace_id="workspace-a",
        owner_actor_id="actor-a",
    )

    draft = _start(_build(store=scoped), "scoped-durable")

    with closing(sqlite3.connect(path)) as connection:
        draft_row = connection.execute(
            "SELECT revision FROM agent_workflow_drafts WHERE id = ?",
            (draft.id,),
        ).fetchone()
        grant_row = connection.execute(
            """
            SELECT workspace_id, owner_actor_id
            FROM workflow_access_grants
            WHERE workflow_id = ?
            """,
            (draft.id,),
        ).fetchone()
    assert draft_row == (draft.revision,)
    assert grant_row == ("workspace-a", "actor-a")
    assert (
        SqliteWorkflowDraftStore(
            path,
            workspace_id="workspace-a",
            owner_actor_id="actor-other",
        ).load(draft.id)
        == draft
    )
    assert (
        SqliteWorkflowDraftStore(
            path,
            workspace_id="workspace-b",
            owner_actor_id="actor-b",
        ).load(draft.id)
        is None
    )


def test_scoped_sqlite_store_cannot_adopt_legacy_or_pre_reserved_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unverified-workflow.db"
    legacy = _build(store=SqliteWorkflowDraftStore(path))
    _start(legacy, "legacy-unverified")
    scoped = _build(
        store=SqliteWorkflowDraftStore(
            path,
            workspace_id="workspace-a",
            owner_actor_id="actor-a",
        )
    )

    with pytest.raises(WorkflowError) as legacy_conflict:
        _start(scoped, "legacy-unverified")
    assert legacy_conflict.value.code is WorkflowErrorCode.CONFLICT
    with closing(sqlite3.connect(path)) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM workflow_access_grants WHERE workflow_id = ?",
                ("legacy-unverified",),
            ).fetchone()
            is None
        )
        connection.execute(
            """
            INSERT INTO workflow_access_grants
                (workflow_id, workspace_id, owner_actor_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            ("reserved-without-draft", "workspace-a", "actor-a", FakeClock().now().isoformat()),
        )
        connection.commit()
    with pytest.raises(WorkflowError) as reserved_conflict:
        _start(scoped, "reserved-without-draft")
    assert reserved_conflict.value.code is WorkflowErrorCode.CONFLICT
    with closing(sqlite3.connect(path)) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM agent_workflow_drafts WHERE id = ?",
                ("reserved-without-draft",),
            ).fetchone()
            is None
        )


def test_sqlite_workflow_store_translates_filesystem_failure(
    tmp_path: Path,
) -> None:
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.touch()

    with pytest.raises(WorkflowError) as failure:
        SqliteWorkflowDraftStore(blocked_parent / "workflow.db")

    assert failure.value.code is WorkflowErrorCode.STORE_FAILURE
    assert str(failure.value) == "local workflow draft store failed"
    assert str(blocked_parent) not in str(failure.value)


def test_workflow_application_has_no_concrete_adapter_imports() -> None:
    source = (ROOT / "src/schemabridge/application/workflow_orchestration.py").read_text(
        encoding="utf-8"
    )

    assert "schemabridge.adapters" not in source
    assert "psycopg" not in source
    assert "sqlglot" not in source
