"""Real PostgreSQL acceptance path through the resumable M12 workflow."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from schemabridge.adapters.datahub.recorded import RecordedCatalogAdapter
from schemabridge.adapters.datahub.workflow_publication import (
    DataHubWorkflowPublicationAdapter,
)
from schemabridge.adapters.language.fake import FakeIntentParser
from schemabridge.adapters.planning.recorded import RecordedSemanticPlanningContext
from schemabridge.adapters.postgres.preview import PsycopgQueryPreview
from schemabridge.adapters.postgres.rejections import PsycopgRejectedSourceReporter
from schemabridge.adapters.requests.recorded_context import RecordedRequestContextAdapter
from schemabridge.adapters.sql.compiler import PostgresQueryCompiler
from schemabridge.adapters.sql.guard import SqlGlotPolicyGuard
from schemabridge.adapters.storage.workflows import SqliteWorkflowDraftStore
from schemabridge.adapters.workflows.fake import SqliteFakeWorkflowPublisher
from schemabridge.adapters.workflows.system import SystemWorkflowClock
from schemabridge.application.governed_execution import (
    ExecuteGovernedRequest,
    PlanSemanticRequest,
    PrepareGovernedRequest,
)
from schemabridge.application.intent_resolution import ResolveNaturalLanguageIntent
from schemabridge.application.ports.catalog import CatalogReadPort
from schemabridge.application.ports.recipes import QueryRecipeReadPort
from schemabridge.application.ports.workflows import WorkflowPublicationPort
from schemabridge.application.query_recipes import AssessQueryRecipeReuse
from schemabridge.application.workflow_orchestration import AgentWorkflowOrchestrator
from schemabridge.bootstrap import build_catalog_reader
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.intents import IntentAlternativeId, UserLanguage
from schemabridge.domain.resolution import ResolutionLimits
from schemabridge.domain.workflows import (
    ExecutionWorkflowDecision,
    IntentWorkflowDecision,
    PublicationWorkflowDecision,
    StartWorkflowCommand,
    WorkflowDecisionAction,
    WorkflowPublicationConfirmation,
    WorkflowPublicationStatus,
    WorkflowStage,
)

pytestmark = pytest.mark.acceptance
ROOT = Path(__file__).resolve().parents[2]
READER_DSN = os.environ.get(
    "SCHEMABRIDGE_TEST_DATABASE_URL",
    "postgresql://schemabridge_reader:schemabridge_reader@127.0.0.1:55433/schemabridge",
)


def _orchestrator(
    database_path: Path,
    catalog: CatalogReadPort | None = None,
    publisher: WorkflowPublicationPort | None = None,
    recipes: QueryRecipeReadPort | None = None,
) -> AgentWorkflowOrchestrator:
    logical = ROOT / "demo/ground_truth/approved_logical_context.yml"
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
        store=SqliteWorkflowDraftStore(database_path),
        clock=SystemWorkflowClock(),
        catalog=catalog or RecordedCatalogAdapter(ROOT / "demo/datahub/catalog_snapshot.json"),
        intent=ResolveNaturalLanguageIntent(
            parser=FakeIntentParser(),
            context=RecordedRequestContextAdapter(logical),
            adapter_label="fake:typed-intent-only",
        ),
        prepare=prepare,
        execute=ExecuteGovernedRequest(
            prepare=prepare,
            executor=PsycopgQueryPreview(READER_DSN),
            rejection_reporter=PsycopgRejectedSourceReporter(
                READER_DSN,
                frozenset(
                    {
                        "crm.customers.customer_id",
                        "bank.account_holders.gf_customer_id",
                    }
                ),
            ),
        ),
        publisher=publisher or SqliteFakeWorkflowPublisher(database_path),
        recipe_assessor=AssessQueryRecipeReuse(recipes) if recipes is not None else None,
    )


def test_workflow_resumes_then_executes_real_north_star_once(tmp_path: Path) -> None:
    database_path = tmp_path / "workflow.db"
    first_process = _orchestrator(database_path)
    paused = first_process.start(
        StartWorkflowCommand(
            id="workflow-real-north-star",
            text=(
                "Agrupa por fecha de registro todos los clientes que sean segundo titular "
                "de una cuenta."
            ),
            language=UserLanguage.SPANISH,
            datasets=(
                PhysicalDatasetRef("crm.customers"),
                PhysicalDatasetRef("bank.account_holders"),
            ),
        )
    )
    assert paused.stage is WorkflowStage.DECISION_REQUIRED
    assert paused.intent is not None

    second_process = _orchestrator(database_path)
    restored = second_process.resume(paused.id)
    assert restored.intent is not None
    validated = second_process.decide_intent(
        restored.id,
        IntentWorkflowDecision(
            actor="local-operator",
            interpretation_fingerprint=restored.intent.interpretation_fingerprint,
            selected_alternative=IntentAlternativeId.COUNT_DISTINCT_CUSTOMERS,
        ),
    )
    assert validated.plan_fingerprint is not None
    executed = second_process.decide_execution(
        validated.id,
        ExecutionWorkflowDecision(
            actor="local-operator",
            plan_fingerprint=validated.plan_fingerprint,
            action=WorkflowDecisionAction.APPROVE,
        ),
    )

    assert executed.stage is WorkflowStage.PUBLICATION_PROPOSED
    assert executed.execution is not None
    assert executed.execution.rows == (
        ("2026-01-01", 2),
        ("2026-01-02", 1),
        ("2026-01-03", 1),
    )
    assert executed.execution.rejection_codes == (
        "non_integral_identifier",
        "non_finite_identifier",
        "null_join_key",
    )
    assert executed.execution.database_user == "schemabridge_reader"
    assert executed.execution.transaction_read_only is True

    third_process = _orchestrator(database_path)
    resumed = third_process.resume(executed.id)
    assert resumed.execution == executed.execution
    assert resumed.plan_fingerprint == executed.plan_fingerprint


def test_live_datahub_and_postgres_complete_local_north_star(tmp_path: Path) -> None:
    if (
        not (ROOT / ".local/datahub/mcp.env").is_file()
        or not (ROOT / ".local/datahub/writer.env").is_file()
    ):
        pytest.skip(
            "DataHub MCP/writer credentials are absent; run the M04/M07 provisioning targets"
        )
    publisher = DataHubWorkflowPublicationAdapter.from_env_file(ROOT / ".local/datahub/writer.env")
    orchestrator = _orchestrator(
        tmp_path / "live-workflow.db",
        build_catalog_reader("live", repository_root=ROOT),
        publisher,
    )
    paused = orchestrator.start(
        StartWorkflowCommand(
            id="workflow-live-north-star",
            text=(
                "Agrupa por fecha de registro todos los clientes que sean segundo titular "
                "de una cuenta."
            ),
            language=UserLanguage.SPANISH,
            datasets=(
                PhysicalDatasetRef("crm.customers"),
                PhysicalDatasetRef("bank.account_holders"),
            ),
        )
    )
    assert paused.intent is not None
    validated = orchestrator.decide_intent(
        paused.id,
        IntentWorkflowDecision(
            actor="local-operator",
            interpretation_fingerprint=paused.intent.interpretation_fingerprint,
            selected_alternative=IntentAlternativeId.COUNT_DISTINCT_CUSTOMERS,
        ),
    )
    assert validated.plan_fingerprint is not None
    executed = orchestrator.decide_execution(
        validated.id,
        ExecutionWorkflowDecision(
            actor="local-operator",
            plan_fingerprint=validated.plan_fingerprint,
            action=WorkflowDecisionAction.APPROVE,
        ),
    )

    assert executed.context_source == "live:datahub-mcp"
    assert executed.execution is not None
    assert executed.execution.rows == (
        ("2026-01-01", 2),
        ("2026-01-02", 1),
        ("2026-01-03", 1),
    )
    assert executed.publication_proposal is not None
    published = orchestrator.decide_publication(
        executed.id,
        PublicationWorkflowDecision(
            actor="local-operator",
            proposal_fingerprint=executed.publication_proposal.fingerprint,
            action=WorkflowDecisionAction.PUBLISH,
            confirmation=WorkflowPublicationConfirmation.PUBLISH_EXECUTION_CONTEXT,
        ),
    )
    assert published.stage is WorkflowStage.PUBLICATION_COMPLETED
    assert published.publication_result is not None
    assert published.publication_approval is not None

    replay = DataHubWorkflowPublicationAdapter.from_env_file(
        ROOT / ".local/datahub/writer.env"
    ).publish(executed.publication_proposal, published.publication_approval)
    assert replay.status is WorkflowPublicationStatus.ALREADY_CURRENT
    assert replay.document_ref == published.publication_result.document_ref
