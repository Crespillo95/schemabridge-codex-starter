"""M13 restart path: persisted recipe provenance with fresh validation and preview."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.acceptance.test_workflow_acceptance import _orchestrator

from schemabridge.adapters.recipes.sqlite import SqliteQueryRecipeRepository
from schemabridge.application.query_recipes import PrepareQueryRecipe, PublishQueryRecipe
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.intents import IntentAlternativeId, UserLanguage
from schemabridge.domain.recipes import (
    RecipePublicationApproval,
    RecipePublicationConfirmation,
    RecipePublicationStatus,
    RecipeReuseStatus,
)
from schemabridge.domain.workflows import (
    ExecutionWorkflowDecision,
    IntentWorkflowDecision,
    StartWorkflowCommand,
    WorkflowDecisionAction,
    WorkflowOperation,
    WorkflowTraceStatus,
)

pytestmark = pytest.mark.acceptance
TEXT = "Agrupa por fecha de registro todos los clientes que sean segundo titular de una cuenta."
DATASETS = (
    PhysicalDatasetRef("crm.customers"),
    PhysicalDatasetRef("bank.account_holders"),
)


def _resolve(orchestrator, workflow_id: str):  # type: ignore[no-untyped-def]
    paused = orchestrator.start(
        StartWorkflowCommand(
            id=workflow_id,
            text=TEXT,
            language=UserLanguage.SPANISH,
            datasets=DATASETS,
        )
    )
    assert paused.intent is not None
    return orchestrator.decide_intent(
        workflow_id,
        IntentWorkflowDecision(
            actor="local-operator",
            interpretation_fingerprint=paused.intent.interpretation_fingerprint,
            selected_alternative=IntentAlternativeId.COUNT_DISTINCT_CUSTOMERS,
        ),
    )


def _execute(orchestrator, resolved):  # type: ignore[no-untyped-def]
    assert resolved.plan_fingerprint is not None
    return orchestrator.decide_execution(
        resolved.id,
        ExecutionWorkflowDecision(
            actor="local-operator",
            plan_fingerprint=resolved.plan_fingerprint,
            action=WorkflowDecisionAction.APPROVE,
        ),
    )


def test_context_reuse_survives_restart_and_revalidates_before_real_preview(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "context-reuse.db"
    repository = SqliteQueryRecipeRepository(database_path)

    first_process = _orchestrator(database_path)
    executed = _execute(first_process, _resolve(first_process, "context-reuse-first"))
    recipe = PrepareQueryRecipe(repository).execute(executed)
    approval = RecipePublicationApproval(
        id="context-reuse-approval-v1",
        recipe_id=recipe.id,
        recipe_version=recipe.version,
        recipe_fingerprint=recipe.fingerprint,
        actor="local-operator",
        approved_at=datetime(2026, 7, 21, 14, 0, tzinfo=UTC),
        confirmation=RecipePublicationConfirmation.PUBLISH_VALIDATED_QUERY_RECIPE,
    )
    first_publication = PublishQueryRecipe(repository).execute(recipe, approval)
    replay = PublishQueryRecipe(repository).execute(recipe, approval)
    assert first_publication.status is RecipePublicationStatus.CREATED
    assert replay.status is RecipePublicationStatus.ALREADY_CURRENT

    second_process = _orchestrator(
        database_path,
        recipes=SqliteQueryRecipeRepository(database_path),
    )
    reused = _resolve(second_process, "context-reuse-second")
    assert reused.recipe_reuse is not None
    assert reused.recipe_reuse.status is RecipeReuseStatus.REUSABLE
    assert reused.recipe_reuse.recipe_fingerprint == recipe.fingerprint
    assert reused.recipe_reuse.provenance_document_urn == first_publication.versioned_document_urn
    assert reused.recipe_reuse.revalidated is True

    second_execution = _execute(second_process, reused)
    assert second_execution.execution is not None
    assert second_execution.execution.rows == (
        ("2026-01-01", 2),
        ("2026-01-02", 1),
        ("2026-01-03", 1),
    )
    assert second_execution.execution.database_user == "schemabridge_reader"
    assert second_execution.execution.transaction_read_only is True
    assert (
        sum(
            event.operation is WorkflowOperation.SQL_VALIDATION
            and event.status is WorkflowTraceStatus.SUCCEEDED
            for event in second_execution.trace
        )
        == 2
    )
    assert any(
        event.operation is WorkflowOperation.QUERY_RECIPE_LOOKUP
        and event.status is WorkflowTraceStatus.SUCCEEDED
        for event in second_execution.trace
    )
