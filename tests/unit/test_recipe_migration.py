"""M23 governed migration of stale immutable query recipes."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from schemabridge.adapters.datahub.recorded import RecordedCatalogAdapter
from schemabridge.adapters.language.fake import FakeIntentParser
from schemabridge.adapters.planning.recorded import RecordedSemanticPlanningContext
from schemabridge.adapters.recipes.sqlite import SqliteQueryRecipeRepository
from schemabridge.adapters.requests.recorded_context import RecordedRequestContextAdapter
from schemabridge.adapters.sql.compiler import PostgresQueryCompiler
from schemabridge.adapters.sql.guard import SqlGlotPolicyGuard
from schemabridge.adapters.storage.publication_audit import SqlitePublicationAuditStore
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
from schemabridge.application.ports.registry_control import (
    RegistryControlError,
    RegistryControlErrorCode,
)
from schemabridge.application.ports.workflows import WorkflowError, WorkflowErrorCode
from schemabridge.application.query_execution import QueryPreviewResult, ValidatedQuery
from schemabridge.application.query_recipes import (
    AssessQueryRecipeReuse,
    PrepareQueryRecipe,
    PrepareStaleQueryRecipeMigration,
    PrepareStoredStaleQueryRecipeMigration,
    PublishQueryRecipe,
    PublishStaleQueryRecipeMigration,
)
from schemabridge.application.workflow_orchestration import AgentWorkflowOrchestrator
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.intents import IntentAlternativeId, UserLanguage
from schemabridge.domain.recipes import (
    PublishedQueryRecipe,
    QueryRecipe,
    RecipeMigrationProposal,
    RecipePublicationApproval,
    RecipePublicationConfirmation,
    RecipePublicationStatus,
    RecipeReuseStatus,
    RecipeStalenessCode,
    query_recipe_payload_fingerprint,
)
from schemabridge.domain.registry_control import (
    ActiveRegistryPointer,
    registry_projection_fingerprint,
)
from schemabridge.domain.resolution import (
    RejectedSourceReport,
    RejectionCheck,
    ResolutionLimits,
    resolved_semantic_plan_fingerprint,
)
from schemabridge.domain.semantic_registry import (
    SemanticRegistryScope,
    datahub_registry_document_urn,
)
from schemabridge.domain.workflows import (
    AgentWorkflowDraft,
    ExecutionWorkflowDecision,
    IntentWorkflowDecision,
    StartWorkflowCommand,
    WorkflowDecisionAction,
    WorkflowDecisionKind,
    WorkflowExecutionRecord,
)

ROOT = Path(__file__).resolve().parents[2]
TEXT = "Agrupa por fecha de registro todos los clientes que sean segundo titular de una cuenta."
DATASETS = (
    PhysicalDatasetRef("crm.customers"),
    PhysicalDatasetRef("bank.account_holders"),
)
NOW = datetime(2026, 7, 23, 17, 0, tzinfo=UTC)
SCOPE = SemanticRegistryScope(
    workspace_id="synthetic-recipe-migration",
    catalog_scope="synthetic-demo",
    registry_id="synthetic_enterprise",
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
        should_continue: Callable[[], bool] | None = None,
    ) -> RejectedSourceReport:
        assert should_continue is None or should_continue()
        return RejectedSourceReport(
            inspected_fields=tuple(check.physical_field for check in checks),
            database_user="schemabridge_reader",
            transaction_read_only=True,
            statement_timeout_ms=statement_timeout_ms,
        )


@dataclass(frozen=True, slots=True)
class _PointerReader:
    pointer: ActiveRegistryPointer | None

    def load_active(self, scope: SemanticRegistryScope) -> ActiveRegistryPointer | None:
        if self.pointer is not None:
            assert self.pointer.scope == scope
        return self.pointer


class _UnavailableWorkflowStore:
    def load(self, workflow_id: str) -> AgentWorkflowDraft | None:
        del workflow_id
        raise WorkflowError(
            WorkflowErrorCode.STORE_FAILURE,
            "secret workflow storage detail",
        )

    def save(
        self,
        draft: AgentWorkflowDraft,
        *,
        expected_revision: int | None,
    ) -> None:
        del draft, expected_revision
        raise AssertionError("migration preparation never saves a workflow")


class _UnavailablePointerReader:
    def load_active(self, scope: SemanticRegistryScope) -> ActiveRegistryPointer | None:
        del scope
        raise RegistryControlError(
            RegistryControlErrorCode.STORE_UNAVAILABLE,
            "secret registry storage detail",
        )


def test_stored_migration_sanitizes_workflow_storage_failure(tmp_path: Path) -> None:
    with pytest.raises(RecipeError) as raised:
        PrepareStoredStaleQueryRecipeMigration(
            store=_UnavailableWorkflowStore(),
            repository=SqliteQueryRecipeRepository(tmp_path / "unavailable-workflow.db"),
            pointers=_PointerReader(None),
            scope=SCOPE,
        ).execute(
            workflow_id="stored-unavailable-workflow",
            intent_fingerprint="a" * 64,
        )

    assert raised.value.code is RecipeErrorCode.WORKFLOW_STORE_UNAVAILABLE
    assert str(raised.value) == "workflow draft storage is unavailable"
    assert "secret" not in str(raised.value)


def test_stored_migration_loads_exact_workflow_recipe_and_active_pointer(
    tmp_path: Path,
) -> None:
    database = tmp_path / "stored-recipes.db"
    repository = SqliteQueryRecipeRepository(database)
    historical = _publish(
        repository,
        database,
        _completed(database, "stored-migration-historical"),
        label="historical",
    )
    current = _completed(database, "stored-migration-current")
    pointer = _pointer(current, generation=1)
    bound = _bind_to_pointer(current, pointer)
    SqliteWorkflowDraftStore(database).save(
        bound,
        expected_revision=current.revision,
    )

    proposal = PrepareStoredStaleQueryRecipeMigration(
        store=SqliteWorkflowDraftStore(database),
        repository=repository,
        pointers=_PointerReader(pointer),
        scope=SCOPE,
    ).execute(
        workflow_id=bound.id,
        intent_fingerprint=historical.recipe.intent_fingerprint,
    )

    assert proposal.historical == historical
    assert proposal.replacement.source_workflow_id == bound.id
    assert proposal.replacement.version == historical.recipe.version + 1


def test_stored_migration_missing_pointer_is_sanitized_without_repository_write(
    tmp_path: Path,
) -> None:
    database = tmp_path / "stored-missing-pointer.db"
    repository = SqliteQueryRecipeRepository(database)
    historical = _publish(
        repository,
        database,
        _completed(database, "missing-pointer-historical"),
        label="historical",
    )
    current = _completed(database, "missing-pointer-current")
    pointer = _pointer(current, generation=1)
    bound = _bind_to_pointer(current, pointer)
    SqliteWorkflowDraftStore(database).save(
        bound,
        expected_revision=current.revision,
    )

    with pytest.raises(RecipeError) as raised:
        PrepareStoredStaleQueryRecipeMigration(
            store=SqliteWorkflowDraftStore(database),
            repository=repository,
            pointers=_PointerReader(None),
            scope=SCOPE,
        ).execute(
            workflow_id=bound.id,
            intent_fingerprint=historical.recipe.intent_fingerprint,
        )

    assert raised.value.code is RecipeErrorCode.ACTIVE_POINTER_NOT_FOUND
    assert repository.find_current(historical.recipe.intent_fingerprint) == historical


def test_stored_migration_sanitizes_registry_storage_failure(tmp_path: Path) -> None:
    database = tmp_path / "stored-registry-unavailable.db"
    repository = SqliteQueryRecipeRepository(database)
    historical = _publish(
        repository,
        database,
        _completed(database, "registry-unavailable-historical"),
        label="historical",
    )
    current = _completed(database, "registry-unavailable-current")

    with pytest.raises(RecipeError) as raised:
        PrepareStoredStaleQueryRecipeMigration(
            store=SqliteWorkflowDraftStore(database),
            repository=repository,
            pointers=_UnavailablePointerReader(),
            scope=SCOPE,
        ).execute(
            workflow_id=current.id,
            intent_fingerprint=historical.recipe.intent_fingerprint,
        )

    assert raised.value.code is RecipeErrorCode.REGISTRY_CONTROL_UNAVAILABLE
    assert str(raised.value) == "active registry control state is unavailable"
    assert "secret" not in str(raised.value)


def test_migration_creates_new_approved_version_and_preserves_historical_bytes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "recipes.db"
    repository = SqliteQueryRecipeRepository(database)
    historical_workflow = _completed(database, "recipe-migration-historical")
    historical = _publish(repository, database, historical_workflow, label="historical")
    historical_bytes = _raw_recipe_payload(
        database,
        historical.recipe.intent_fingerprint,
        historical.recipe.version,
    )
    current_workflow = _completed(database, "recipe-migration-current")
    pointer = _pointer(current_workflow, generation=1)
    current_workflow = _bind_to_pointer(current_workflow, pointer)

    proposal = PrepareStaleQueryRecipeMigration(repository).execute(
        historical=historical,
        draft=current_workflow,
        active_pointer=pointer,
    )

    replacement = proposal.replacement
    assert proposal.assessment.status is RecipeReuseStatus.STALE
    assert RecipeStalenessCode.ACTIVE_REGISTRY_CHANGED in proposal.assessment.reasons
    assert RecipeStalenessCode.PLAN_CHANGED in proposal.assessment.reasons
    assert replacement.version == historical.recipe.version + 1
    assert replacement.source_workflow_id == current_workflow.id
    assert replacement.registry_binding is not None
    assert replacement.registry_binding.generation == pointer.generation
    assert replacement.registry_binding.pointer_fingerprint == registry_projection_fingerprint(
        pointer
    )
    assert replacement.fingerprint != historical.recipe.fingerprint
    assert replacement.content_fingerprint != historical.recipe.content_fingerprint
    assert replacement.migration_provenance is not None
    assert (
        replacement.migration_provenance.previous_recipe_fingerprint
        == historical.recipe.fingerprint
    )
    assert (
        replacement.migration_provenance.previous_recipe_payload_fingerprint
        == query_recipe_payload_fingerprint(historical.recipe)
    )
    assert replacement.migration_provenance.previous_recipe_version == historical.recipe.version
    assert RecipeMigrationProposal.model_validate_json(proposal.model_dump_json()) == proposal

    publisher = PublishStaleQueryRecipeMigration(
        repository,
        SqlitePublicationAuditStore(database),
    )
    wrong = _approval(replacement, label="wrong").model_copy(
        update={"recipe_fingerprint": historical.recipe.fingerprint}
    )
    with pytest.raises(RecipeError) as mismatch:
        publisher.execute(proposal, wrong)
    assert mismatch.value.code is RecipeErrorCode.APPROVAL_MISMATCH
    assert repository.find_current(historical.recipe.intent_fingerprint) == historical
    assert (
        _raw_recipe_payload(
            database,
            historical.recipe.intent_fingerprint,
            historical.recipe.version,
        )
        == historical_bytes
    )

    result = publisher.execute(
        proposal,
        _approval(replacement, label="migration"),
    )

    assert result.status is RecipePublicationStatus.CREATED
    migrated = SqliteQueryRecipeRepository(database).find_current(
        historical.recipe.intent_fingerprint
    )
    assert migrated is not None
    assert migrated.recipe == replacement
    assert (
        _raw_recipe_payload(
            database,
            historical.recipe.intent_fingerprint,
            historical.recipe.version,
        )
        == historical_bytes
    )
    with closing(sqlite3.connect(database)) as connection:
        versions = connection.execute(
            """
            SELECT version FROM fake_query_recipes
            WHERE intent_fingerprint = ? ORDER BY version
            """,
            (historical.recipe.intent_fingerprint,),
        ).fetchall()
    assert versions == [(1,), (2,)]


def test_migration_rejects_old_incomplete_or_unexecuted_workflow(tmp_path: Path) -> None:
    database = tmp_path / "recipes.db"
    repository = SqliteQueryRecipeRepository(database)
    historical_workflow = _completed(database, "recipe-invalid-historical")
    historical = _publish(repository, database, historical_workflow, label="historical")
    current = _completed(database, "recipe-invalid-current")
    pointer = _pointer(current, generation=1)
    managed = _bind_to_pointer(current, pointer)
    prepare = PrepareStaleQueryRecipeMigration(repository)

    incomplete = _start(_build(database), "recipe-incomplete")
    with pytest.raises(RecipeError) as incomplete_error:
        prepare.execute(
            historical=historical,
            draft=incomplete,
            active_pointer=pointer,
        )
    assert incomplete_error.value.code is RecipeErrorCode.INVALID_WORKFLOW

    no_execution = managed.model_copy(update={"execution": None})
    with pytest.raises(RecipeError) as execution_error:
        prepare.execute(
            historical=historical,
            draft=no_execution,
            active_pointer=pointer,
        )
    assert execution_error.value.code is RecipeErrorCode.INVALID_WORKFLOW

    reused_old_workflow = _bind_to_pointer(historical_workflow, pointer)
    with pytest.raises(RecipeError) as old_workflow_error:
        prepare.execute(
            historical=historical,
            draft=reused_old_workflow,
            active_pointer=pointer,
        )
    assert old_workflow_error.value.code is RecipeErrorCode.INVALID_WORKFLOW


@pytest.mark.parametrize("fingerprint_field", ["plan_fingerprint", "query_fingerprint"])
def test_migration_rejects_execution_fingerprint_not_bound_to_draft(
    tmp_path: Path,
    fingerprint_field: str,
) -> None:
    database = tmp_path / "recipes.db"
    repository = SqliteQueryRecipeRepository(database)
    historical_workflow = _completed(database, "recipe-forged-historical")
    historical = _publish(repository, database, historical_workflow, label="historical")
    current = _completed(database, "recipe-forged-current")
    pointer = _pointer(current, generation=1)
    managed = _bind_to_pointer(current, pointer)
    assert managed.execution is not None
    forged_execution = managed.execution.model_copy(update={fingerprint_field: "0" * 64})
    forged_payload = {
        **managed.model_dump(mode="python"),
        "execution": forged_execution,
    }

    with pytest.raises(ValueError, match="execution fingerprints must match"):
        AgentWorkflowDraft.model_validate(forged_payload)

    forged = managed.model_copy(update={"execution": forged_execution})
    with pytest.raises(RecipeError) as raised:
        PrepareStaleQueryRecipeMigration(repository).execute(
            historical=historical,
            draft=forged,
            active_pointer=pointer,
        )

    assert raised.value.code is RecipeErrorCode.INVALID_WORKFLOW


def test_migration_rejects_workflow_bound_to_a_stale_registry_pointer(
    tmp_path: Path,
) -> None:
    database = tmp_path / "recipes.db"
    repository = SqliteQueryRecipeRepository(database)
    historical_workflow = _completed(database, "recipe-stale-historical")
    historical = _publish(repository, database, historical_workflow, label="historical")
    current = _completed(database, "recipe-stale-current")
    previous_pointer = _pointer(current, generation=1)
    stale_workflow = _bind_to_pointer(current, previous_pointer)
    active_pointer = _pointer(current, generation=2)

    with pytest.raises(RecipeError) as raised:
        PrepareStaleQueryRecipeMigration(repository).execute(
            historical=historical,
            draft=stale_workflow,
            active_pointer=active_pointer,
        )

    assert raised.value.code is RecipeErrorCode.STALE_WORKFLOW


def test_migration_rejects_a_different_intent(tmp_path: Path) -> None:
    database = tmp_path / "recipes.db"
    repository = SqliteQueryRecipeRepository(database)
    historical_workflow = _completed(database, "recipe-intent-historical")
    historical = _publish(repository, database, historical_workflow, label="historical")
    different = _completed(
        database,
        "recipe-intent-different",
        alternative=IntentAlternativeId.COUNT_HOLDER_RELATIONSHIPS,
    )
    pointer = _pointer(different, generation=1)
    different = _bind_to_pointer(different, pointer)

    with pytest.raises(RecipeError) as raised:
        PrepareStaleQueryRecipeMigration(repository).execute(
            historical=historical,
            draft=different,
            active_pointer=pointer,
        )

    assert raised.value.code is RecipeErrorCode.INTENT_MISMATCH


def test_migration_rejects_current_recipe_without_staleness(tmp_path: Path) -> None:
    database = tmp_path / "recipes.db"
    repository = SqliteQueryRecipeRepository(database)
    source = _completed(database, "recipe-current-source")
    pointer = _pointer(source, generation=1)
    source = _bind_to_pointer(source, pointer)
    historical = _publish(repository, database, source, label="historical")
    replacement_workflow = _bind_to_pointer(
        _completed(database, "recipe-current-replacement"),
        pointer,
    )
    assert replacement_workflow.validated_request is not None
    assert replacement_workflow.resolved_plan is not None
    assert replacement_workflow.plan_fingerprint is not None
    assert replacement_workflow.query_fingerprint is not None

    assessment = AssessQueryRecipeReuse(repository).execute(
        validated_request=replacement_workflow.validated_request,
        resolved_plan=replacement_workflow.resolved_plan,
        plan_fingerprint=replacement_workflow.plan_fingerprint,
        query_fingerprint=replacement_workflow.query_fingerprint,
        source_schema_fingerprint=historical.recipe.source_schema_fingerprint,
    )
    assert assessment.status is RecipeReuseStatus.REUSABLE

    with pytest.raises(RecipeError) as raised:
        PrepareStaleQueryRecipeMigration(repository).execute(
            historical=historical,
            draft=replacement_workflow,
            active_pointer=pointer,
        )

    assert raised.value.code is RecipeErrorCode.MIGRATION_NOT_REQUIRED


def test_migration_rechecks_repository_before_publication(tmp_path: Path) -> None:
    database = tmp_path / "recipes.db"
    repository = SqliteQueryRecipeRepository(database)
    historical_workflow = _completed(database, "recipe-race-historical")
    historical = _publish(repository, database, historical_workflow, label="historical")
    historical_bytes = _raw_recipe_payload(
        database,
        historical.recipe.intent_fingerprint,
        historical.recipe.version,
    )
    first_workflow = _completed(database, "recipe-race-first")
    pointer = _pointer(first_workflow, generation=1)
    first_workflow = _bind_to_pointer(first_workflow, pointer)
    second_workflow = _bind_to_pointer(
        _completed(database, "recipe-race-second"),
        pointer,
    )
    prepare = PrepareStaleQueryRecipeMigration(repository)
    first = prepare.execute(
        historical=historical,
        draft=first_workflow,
        active_pointer=pointer,
    )
    competing = prepare.execute(
        historical=historical,
        draft=second_workflow,
        active_pointer=pointer,
    )
    publisher = PublishStaleQueryRecipeMigration(
        repository,
        SqlitePublicationAuditStore(database),
    )
    publisher.execute(
        competing,
        _approval(competing.replacement, label="competing"),
    )

    with pytest.raises(RecipeError) as raised:
        publisher.execute(
            first,
            _approval(first.replacement, label="loser"),
        )

    assert raised.value.code is RecipeErrorCode.CURRENT_CHANGED
    current = repository.find_current(historical.recipe.intent_fingerprint)
    assert current is not None
    assert current.recipe == competing.replacement
    assert (
        _raw_recipe_payload(
            database,
            historical.recipe.intent_fingerprint,
            historical.recipe.version,
        )
        == historical_bytes
    )


def test_minimized_workflow_uses_observed_row_count_for_recipe(tmp_path: Path) -> None:
    database = tmp_path / "recipes.db"
    completed = _completed(database, "recipe-minimized")
    assert completed.execution is not None
    observed = completed.execution.observed_row_count
    minimized_execution = WorkflowExecutionRecord.model_validate(
        {
            **completed.execution.model_dump(mode="python"),
            "rows": (),
            "row_count": observed,
        }
    )
    minimized = completed.model_copy(update={"execution": minimized_execution})

    recipe = PrepareQueryRecipe(SqliteQueryRecipeRepository(database)).execute(minimized)

    assert minimized.execution is not None
    assert minimized.execution.rows == ()
    assert recipe.validation.row_count == observed
    assert recipe.validation.row_count > 0


def _build(path: Path) -> AgentWorkflowOrchestrator:
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
            executor=CountingPreview(),
            rejection_reporter=EmptyRejectionReporter(),
        ),
        publisher=SqliteFakeWorkflowPublisher(path),
        audit_store=SqlitePublicationAuditStore(path),
    )


def _start(
    orchestrator: AgentWorkflowOrchestrator,
    workflow_id: str,
) -> AgentWorkflowDraft:
    return orchestrator.start(
        StartWorkflowCommand(
            id=workflow_id,
            text=TEXT,
            language=UserLanguage.SPANISH,
            datasets=DATASETS,
        )
    )


def _completed(
    path: Path,
    workflow_id: str,
    *,
    alternative: IntentAlternativeId = IntentAlternativeId.COUNT_DISTINCT_CUSTOMERS,
) -> AgentWorkflowDraft:
    orchestrator = _build(path)
    started = _start(orchestrator, workflow_id)
    assert started.intent is not None
    resolved = orchestrator.decide_intent(
        started.id,
        IntentWorkflowDecision(
            actor="synthetic-recipe-operator",
            interpretation_fingerprint=started.intent.interpretation_fingerprint,
            selected_alternative=alternative,
        ),
    )
    assert resolved.plan_fingerprint is not None
    return orchestrator.decide_execution(
        resolved.id,
        ExecutionWorkflowDecision(
            actor="synthetic-recipe-operator",
            plan_fingerprint=resolved.plan_fingerprint,
            action=WorkflowDecisionAction.APPROVE,
        ),
    )


def _pointer(
    draft: AgentWorkflowDraft,
    *,
    generation: int,
) -> ActiveRegistryPointer:
    assert draft.resolved_plan is not None
    transition_id = f"recipe-transition-{generation}"
    return ActiveRegistryPointer(
        scope=SCOPE,
        generation=generation,
        registry_version=draft.resolved_plan.context_version,
        registry_fingerprint=draft.resolved_plan.context_fingerprint,
        registry_target=datahub_registry_document_urn(
            SCOPE,
            draft.resolved_plan.context_version,
        ),
        transition_id=transition_id,
        activated_by="synthetic-registry-operator",
        activated_at=NOW + timedelta(minutes=generation),
        decision_ids=(f"synthetic-registry-decision-{generation}",),
    )


def _bind_to_pointer(
    draft: AgentWorkflowDraft,
    pointer: ActiveRegistryPointer,
) -> AgentWorkflowDraft:
    assert draft.resolved_plan is not None
    assert draft.execution is not None
    resolved = draft.resolved_plan.model_copy(
        update={
            "activation_generation": pointer.generation,
            "active_pointer_fingerprint": registry_projection_fingerprint(pointer),
        }
    )
    plan_fingerprint = resolved_semantic_plan_fingerprint(resolved)
    execution = draft.execution.model_copy(update={"plan_fingerprint": plan_fingerprint})
    decisions = tuple(
        decision.model_copy(update={"bound_fingerprint": plan_fingerprint})
        if decision.kind is WorkflowDecisionKind.EXECUTION
        else decision
        for decision in draft.decisions
    )
    return AgentWorkflowDraft.model_validate(
        {
            **draft.model_dump(mode="python"),
            "resolved_plan": resolved,
            "plan_fingerprint": plan_fingerprint,
            "execution": execution,
            "decisions": decisions,
        }
    )


def _publish(
    repository: SqliteQueryRecipeRepository,
    database: Path,
    workflow: AgentWorkflowDraft,
    *,
    label: str,
) -> PublishedQueryRecipe:
    recipe = PrepareQueryRecipe(repository).execute(workflow)
    PublishQueryRecipe(repository, SqlitePublicationAuditStore(database)).execute(
        recipe,
        _approval(recipe, label=label),
    )
    current = repository.find_current(recipe.intent_fingerprint)
    assert current is not None
    return current


def _approval(recipe: QueryRecipe, *, label: str) -> RecipePublicationApproval:
    return RecipePublicationApproval(
        id=f"recipe-{label}-v{recipe.version}",
        recipe_id=recipe.id,
        recipe_version=recipe.version,
        recipe_fingerprint=recipe.fingerprint,
        actor="synthetic-recipe-operator",
        approved_at=NOW + timedelta(minutes=recipe.version),
        confirmation=RecipePublicationConfirmation.PUBLISH_VALIDATED_QUERY_RECIPE,
    )


def _raw_recipe_payload(
    database: Path,
    intent_fingerprint: str,
    version: int,
) -> bytes:
    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            """
            SELECT payload FROM fake_query_recipes
            WHERE intent_fingerprint = ? AND version = ?
            """,
            (intent_fingerprint, version),
        ).fetchone()
    assert row is not None
    return str(row[0]).encode()
