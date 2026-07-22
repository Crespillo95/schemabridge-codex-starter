"""Focused M13 tests for recipe approval, fingerprints, staleness, and reuse."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from schemabridge.adapters.datahub.recorded import RecordedCatalogAdapter
from schemabridge.adapters.language.fake import FakeIntentParser
from schemabridge.adapters.planning.recorded import RecordedSemanticPlanningContext
from schemabridge.adapters.recipes.sqlite import SqliteQueryRecipeRepository
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
from schemabridge.application.ports.recipes import RecipeError, RecipeErrorCode
from schemabridge.application.query_execution import QueryPreviewResult, ValidatedQuery
from schemabridge.application.query_recipes import (
    AssessQueryRecipeReuse,
    PrepareQueryRecipe,
    PublishQueryRecipe,
    source_schema_fingerprint,
)
from schemabridge.application.workflow_orchestration import AgentWorkflowOrchestrator
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.intents import IntentAlternativeId, UserLanguage
from schemabridge.domain.recipes import (
    QueryRecipe,
    RecipePublicationApproval,
    RecipePublicationConfirmation,
    RecipePublicationStatus,
    RecipeReuseStatus,
    RecipeStalenessCode,
)
from schemabridge.domain.resolution import (
    RejectedSourceReport,
    RejectionCheck,
    ResolutionLimits,
    ResolvedSemanticPlan,
    resolved_semantic_plan_fingerprint,
)
from schemabridge.domain.workflows import (
    AgentWorkflowDraft,
    ExecutionWorkflowDecision,
    IntentWorkflowDecision,
    StartWorkflowCommand,
    WorkflowDecisionAction,
    WorkflowOperation,
    WorkflowTraceStatus,
)

ROOT = Path(__file__).resolve().parents[2]
TEXT = "Agrupa por fecha de registro todos los clientes que sean segundo titular de una cuenta."
DATASETS = (
    PhysicalDatasetRef("crm.customers"),
    PhysicalDatasetRef("bank.account_holders"),
)


@dataclass(slots=True)
class CountingPreview:
    calls: int = 0

    def execute(self, query: ValidatedQuery) -> QueryPreviewResult:
        self.calls += 1
        return QueryPreviewResult(
            columns=("registration_date", "secondary_holder_customers"),
            rows=(("2026-01-10", 2), ("2026-01-11", 1), ("2026-01-12", 1)),
            database_user="schemabridge_reader",
            transaction_read_only=True,
            statement_timeout_ms=query.statement_timeout_ms,
            truncated=False,
        )


class EmptyRejectionReporter:
    def inspect(
        self,
        checks: tuple[RejectionCheck, ...],
        *,
        statement_timeout_ms: int,
    ) -> RejectedSourceReport:
        return RejectedSourceReport(
            inspected_fields=tuple(check.physical_field for check in checks),
            database_user="schemabridge_reader",
            transaction_read_only=True,
            statement_timeout_ms=statement_timeout_ms,
        )


def _build(
    path: Path,
    *,
    recipes: SqliteQueryRecipeRepository | None = None,
    preview: CountingPreview | None = None,
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
        store=SqliteWorkflowDraftStore(path),
        clock=SystemWorkflowClock(),
        catalog=RecordedCatalogAdapter(ROOT / "demo/datahub/catalog_snapshot.json"),
        intent=ResolveNaturalLanguageIntent(
            parser=FakeIntentParser(),
            context=RecordedRequestContextAdapter(logical),
            adapter_label="fake:typed-intent-only",
        ),
        prepare=prepare,
        execute=ExecuteGovernedRequest(
            prepare=prepare,
            executor=preview or CountingPreview(),
            rejection_reporter=EmptyRejectionReporter(),
        ),
        publisher=SqliteFakeWorkflowPublisher(path),
        recipe_assessor=AssessQueryRecipeReuse(recipes) if recipes is not None else None,
    )


def _start(orchestrator: AgentWorkflowOrchestrator, workflow_id: str) -> AgentWorkflowDraft:
    return orchestrator.start(
        StartWorkflowCommand(
            id=workflow_id,
            text=TEXT,
            language=UserLanguage.SPANISH,
            datasets=DATASETS,
        )
    )


def _resolve(orchestrator: AgentWorkflowOrchestrator, draft: AgentWorkflowDraft):
    assert draft.intent is not None
    return orchestrator.decide_intent(
        draft.id,
        IntentWorkflowDecision(
            actor="operator@example.test",
            interpretation_fingerprint=draft.intent.interpretation_fingerprint,
            selected_alternative=IntentAlternativeId.COUNT_DISTINCT_CUSTOMERS,
        ),
    )


def _execute(orchestrator: AgentWorkflowOrchestrator, draft: AgentWorkflowDraft):
    assert draft.plan_fingerprint is not None
    return orchestrator.decide_execution(
        draft.id,
        ExecutionWorkflowDecision(
            actor="operator@example.test",
            plan_fingerprint=draft.plan_fingerprint,
            action=WorkflowDecisionAction.APPROVE,
        ),
    )


def _approval(recipe: QueryRecipe) -> RecipePublicationApproval:
    return RecipePublicationApproval(
        id=f"recipe-approval-{recipe.version}",
        recipe_id=recipe.id,
        recipe_version=recipe.version,
        recipe_fingerprint=recipe.fingerprint,
        actor="operator@example.test",
        approved_at=datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
        confirmation=RecipePublicationConfirmation.PUBLISH_VALIDATED_QUERY_RECIPE,
    )


def test_recipe_requires_completed_execution_and_exact_explicit_approval(tmp_path: Path) -> None:
    database = tmp_path / "recipes.db"
    repository = SqliteQueryRecipeRepository(database)
    orchestrator = _build(database)
    paused = _start(orchestrator, "recipe-before-execution")

    with pytest.raises(RecipeError) as incomplete:
        PrepareQueryRecipe(repository).execute(paused)
    assert incomplete.value.code is RecipeErrorCode.INVALID_WORKFLOW

    completed = _execute(orchestrator, _resolve(orchestrator, paused))
    recipe = PrepareQueryRecipe(repository).execute(completed)
    assert recipe.normalized_intent == completed.validated_request.request
    assert recipe.mapping_versions
    assert recipe.join_versions
    assert recipe.validation.transaction_read_only is True
    assert "sql" not in recipe.__class__.model_fields

    wrong = _approval(recipe).model_copy(update={"recipe_fingerprint": "0" * 64})
    with pytest.raises(RecipeError) as mismatch:
        PublishQueryRecipe(repository).execute(recipe, wrong)
    assert mismatch.value.code is RecipeErrorCode.APPROVAL_MISMATCH


def test_recipe_round_trip_publication_is_idempotent_and_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "recipes.db"
    repository = SqliteQueryRecipeRepository(database)
    orchestrator = _build(database)
    completed = _execute(
        orchestrator,
        _resolve(orchestrator, _start(orchestrator, "recipe-published")),
    )
    recipe = PrepareQueryRecipe(repository).execute(completed)
    assert QueryRecipe.model_validate_json(recipe.model_dump_json()) == recipe

    first = PublishQueryRecipe(repository).execute(recipe, _approval(recipe))
    second = PublishQueryRecipe(repository).execute(recipe, _approval(recipe))
    assert first.status is RecipePublicationStatus.CREATED
    assert second.status is RecipePublicationStatus.ALREADY_CURRENT

    restarted = SqliteQueryRecipeRepository(database)
    published = restarted.find_current(recipe.intent_fingerprint)
    assert published is not None
    assert published.recipe == recipe
    assert published.versioned_document_urn.startswith("fake://query-recipe/")


def test_recipe_reuse_is_compatible_only_for_current_context(tmp_path: Path) -> None:
    database = tmp_path / "recipes.db"
    repository = SqliteQueryRecipeRepository(database)
    orchestrator = _build(database)
    completed = _execute(
        orchestrator,
        _resolve(orchestrator, _start(orchestrator, "recipe-current-context")),
    )
    recipe = PrepareQueryRecipe(repository).execute(completed)
    PublishQueryRecipe(repository).execute(recipe, _approval(recipe))
    assert completed.validated_request is not None
    assert completed.resolved_plan is not None
    assert completed.plan_fingerprint is not None
    assert completed.query_fingerprint is not None

    assessor = AssessQueryRecipeReuse(SqliteQueryRecipeRepository(database))
    reusable = assessor.execute(
        validated_request=completed.validated_request,
        resolved_plan=completed.resolved_plan,
        plan_fingerprint=completed.plan_fingerprint,
        query_fingerprint=completed.query_fingerprint,
        source_schema_fingerprint=source_schema_fingerprint(completed),
    )
    assert reusable.status is RecipeReuseStatus.REUSABLE
    assert reusable.provenance_document_urn is not None
    assert reusable.revalidated is True

    payload = completed.resolved_plan.model_dump(mode="python")
    payload["selected_mappings"][0]["mapping"]["version"] += 1
    changed = ResolvedSemanticPlan.model_validate(payload)
    stale = assessor.execute(
        validated_request=completed.validated_request,
        resolved_plan=changed,
        plan_fingerprint=resolved_semantic_plan_fingerprint(changed),
        query_fingerprint=completed.query_fingerprint,
        source_schema_fingerprint=source_schema_fingerprint(completed),
    )
    assert stale.status is RecipeReuseStatus.STALE
    assert RecipeStalenessCode.MAPPING_VERSION_CHANGED in stale.reasons
    assert RecipeStalenessCode.PLAN_CHANGED in stale.reasons


def test_new_workflow_reuses_provenance_but_revalidates_and_executes_once(tmp_path: Path) -> None:
    database = tmp_path / "recipes.db"
    repository = SqliteQueryRecipeRepository(database)
    first = _build(database)
    completed = _execute(first, _resolve(first, _start(first, "recipe-first-process")))
    recipe = PrepareQueryRecipe(repository).execute(completed)
    PublishQueryRecipe(repository).execute(recipe, _approval(recipe))

    preview = CountingPreview()
    restarted = _build(
        database,
        recipes=SqliteQueryRecipeRepository(database),
        preview=preview,
    )
    resolved = _resolve(restarted, _start(restarted, "recipe-second-process"))
    assert resolved.recipe_reuse is not None
    assert resolved.recipe_reuse.status is RecipeReuseStatus.REUSABLE
    assert resolved.recipe_reuse.recipe_fingerprint == recipe.fingerprint
    assert any(
        event.operation is WorkflowOperation.QUERY_RECIPE_LOOKUP
        and event.status is WorkflowTraceStatus.SUCCEEDED
        for event in resolved.trace
    )
    executed = _execute(restarted, resolved)
    assert executed.execution is not None
    assert preview.calls == 1
    assert (
        sum(
            event.operation is WorkflowOperation.SQL_VALIDATION
            and event.status is WorkflowTraceStatus.SUCCEEDED
            for event in executed.trace
        )
        == 2
    )


def test_recipe_application_has_no_concrete_adapter_imports() -> None:
    source = (ROOT / "src/schemabridge/application/query_recipes.py").read_text(encoding="utf-8")

    assert "schemabridge.adapters" not in source
    assert "datahub" not in source.casefold()
    assert "sqlite" not in source.casefold()


def test_generated_synthetic_recipe_artifact_is_a_valid_sql_free_round_trip() -> None:
    payload = yaml.safe_load((ROOT / "examples/query-recipe-secondary-holders.yml").read_text())
    recipe = QueryRecipe.model_validate(payload)

    assert recipe.validation.policy_status == "accepted"
    assert recipe.validation.row_count == 3
    assert set(recipe.model_dump()) == set(QueryRecipe.model_fields)
    assert "sql" not in recipe.__class__.model_fields
