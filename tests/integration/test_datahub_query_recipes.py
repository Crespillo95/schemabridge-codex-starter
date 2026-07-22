"""Live DataHub document contract for approval-gated query recipes."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.acceptance.test_context_reuse_acceptance import _execute, _resolve
from tests.acceptance.test_workflow_acceptance import _orchestrator

from schemabridge.adapters.datahub.query_recipes import DataHubQueryRecipeAdapter
from schemabridge.application.query_recipes import PrepareQueryRecipe, PublishQueryRecipe
from schemabridge.domain.recipes import (
    RecipePublicationApproval,
    RecipePublicationConfirmation,
    RecipePublicationStatus,
    RecipeReuseStatus,
)

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def test_datahub_recipe_document_round_trip_and_new_process_reuse(tmp_path: Path) -> None:
    credential = ROOT / ".local/datahub/writer.env"
    if not credential.is_file():
        pytest.skip("DataHub writer credentials are absent; run make datahub-provision-writer")
    database_path = tmp_path / "datahub-recipe.db"
    repository = DataHubQueryRecipeAdapter.from_env_file(credential)
    first_process = _orchestrator(database_path)
    executed = _execute(
        first_process,
        _resolve(first_process, "datahub-recipe-document"),
    )
    recipe = PrepareQueryRecipe(repository).execute(executed)
    approval = RecipePublicationApproval(
        id=f"datahub-recipe-approval-v{recipe.version}",
        recipe_id=recipe.id,
        recipe_version=recipe.version,
        recipe_fingerprint=recipe.fingerprint,
        actor="local-operator",
        approved_at=datetime.now(UTC),
        confirmation=RecipePublicationConfirmation.PUBLISH_VALIDATED_QUERY_RECIPE,
    )
    result = PublishQueryRecipe(repository).execute(recipe, approval)
    assert result.status in {
        RecipePublicationStatus.CREATED,
        RecipePublicationStatus.ALREADY_CURRENT,
    }
    replay = PublishQueryRecipe(repository).execute(recipe, approval)
    assert replay.status is RecipePublicationStatus.ALREADY_CURRENT
    assert replay.versioned_document_urn == result.versioned_document_urn

    reopened = DataHubQueryRecipeAdapter.from_env_file(credential)
    published = reopened.find_current(recipe.intent_fingerprint)
    assert published is not None
    assert published.recipe.fingerprint == recipe.fingerprint
    assert published.versioned_document_urn == result.versioned_document_urn
    assert set(published.recipe.linked_asset_urns) == {
        "urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge.crm.customers,PROD)",
        "urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge.bank.account_holders,PROD)",
    }

    second_process = _orchestrator(database_path, recipes=reopened)
    reused = _resolve(second_process, "datahub-recipe-restarted")
    assert reused.recipe_reuse is not None
    assert reused.recipe_reuse.status is RecipeReuseStatus.REUSABLE
    assert reused.recipe_reuse.provenance_document_urn == result.versioned_document_urn
