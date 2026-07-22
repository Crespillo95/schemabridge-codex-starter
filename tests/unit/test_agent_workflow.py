"""Focused tests for the explicit, durable M12 workflow state machine."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from schemabridge.adapters.datahub.recorded import RecordedCatalogAdapter
from schemabridge.adapters.language.fake import FakeIntentParser
from schemabridge.adapters.planning.recorded import RecordedSemanticPlanningContext
from schemabridge.adapters.requests.recorded_context import RecordedRequestContextAdapter
from schemabridge.adapters.sql.compiler import PostgresQueryCompiler
from schemabridge.adapters.sql.guard import SqlGlotPolicyGuard
from schemabridge.adapters.storage.workflows import SqliteWorkflowDraftStore
from schemabridge.application.governed_execution import (
    ExecuteGovernedRequest,
    PlanSemanticRequest,
    PrepareGovernedRequest,
)
from schemabridge.application.intent_resolution import ResolveNaturalLanguageIntent
from schemabridge.application.ports.catalog import (
    CatalogUnavailableError,
    PageRequest,
)
from schemabridge.application.ports.planning import PlanningPortError, PlanningPortErrorCode
from schemabridge.application.ports.workflows import WorkflowError, WorkflowErrorCode
from schemabridge.application.query_execution import QueryPreviewResult, ValidatedQuery
from schemabridge.application.workflow_orchestration import AgentWorkflowOrchestrator
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.intents import IntentAlternativeId, UserLanguage
from schemabridge.domain.resolution import (
    RejectedSourceReport,
    RejectionCheck,
    ResolutionLimits,
)
from schemabridge.domain.workflows import (
    AgentWorkflowDraft,
    ExecutionWorkflowDecision,
    IntentWorkflowDecision,
    PublicationWorkflowDecision,
    RetryWorkflowDecision,
    StartWorkflowCommand,
    WorkflowCheckpointKind,
    WorkflowDecisionAction,
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

    def execute(self, query: ValidatedQuery) -> QueryPreviewResult:
        self.calls += 1
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

    def inspect(
        self,
        checks: tuple[RejectionCheck, ...],
        *,
        statement_timeout_ms: int,
    ) -> RejectedSourceReport:
        self.calls += 1
        if self.fail_once and self.calls == 1:
            raise PlanningPortError(
                PlanningPortErrorCode.REJECTION_INSPECTION_UNAVAILABLE,
                "temporary synthetic inspection failure",
            )
        return RejectedSourceReport(
            inspected_fields=tuple(check.physical_field for check in checks),
            database_user="schemabridge_reader",
            transaction_read_only=True,
            statement_timeout_ms=statement_timeout_ms,
        )


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
        if self.fail_once and self.calls == 1:
            raise WorkflowError(
                WorkflowErrorCode.PUBLICATION_FAILED,
                "temporary synthetic publication failure",
            )
        return WorkflowPublicationResult(
            status=WorkflowPublicationStatus.CREATED,
            idempotency_key=proposal.idempotency_key,
            document_ref=f"fake://workflow-context/{proposal.idempotency_key}",
            published_at=datetime(2026, 7, 21, 10, 30, tzinfo=UTC),
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


def test_resume_turns_interrupted_preview_into_typed_retry_without_auto_execution() -> None:
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

    failed = orchestrator.resume(interrupted.id)
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
    orchestrator = _build(publisher=publisher)
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


def test_publication_retry_reuses_key_without_reexecuting_preview() -> None:
    preview = FakePreview()
    publisher = CountingPublisher(fail_once=True)
    orchestrator = _build(preview=preview, publisher=publisher)
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


def test_workflow_application_has_no_concrete_adapter_imports() -> None:
    source = (ROOT / "src/schemabridge/application/workflow_orchestration.py").read_text(
        encoding="utf-8"
    )

    assert "schemabridge.adapters" not in source
    assert "psycopg" not in source
    assert "sqlglot" not in source
