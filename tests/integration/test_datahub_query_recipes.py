"""Live DataHub document contract for approval-gated query recipes."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from tests.acceptance.test_context_reuse_acceptance import _execute, _resolve
from tests.acceptance.test_workflow_acceptance import _orchestrator

from schemabridge.adapters.datahub.query_recipes import DataHubQueryRecipeAdapter
from schemabridge.adapters.storage.publication_audit import SqlitePublicationAuditStore
from schemabridge.application.query_recipes import (
    AssessQueryRecipeReuse,
    PrepareQueryRecipe,
    PublishQueryRecipe,
)
from schemabridge.domain.recipes import (
    RecipePublicationApproval,
    RecipePublicationConfirmation,
    RecipePublicationStatus,
    RecipeReuseStatus,
)
from schemabridge.domain.resolution import resolved_semantic_plan_fingerprint

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def test_datahub_recipe_document_round_trip_and_new_process_reuse(tmp_path: Path) -> None:
    credential = ROOT / ".local/datahub/writer.env"
    if not credential.is_file():
        pytest.skip("DataHub writer credentials are absent; run make datahub-provision-writer")
    database_path = tmp_path / "datahub-recipe.db"
    repository = DataHubQueryRecipeAdapter.from_env_file(credential)
    audit_path = tmp_path / "publication-audit.db"
    first_process = _orchestrator(database_path)
    executed = _execute(
        first_process,
        _resolve(first_process, "datahub-recipe-document"),
    )
    assert executed.resolved_plan is not None
    assert executed.execution is not None
    assert executed.validated_request is not None
    assert executed.query_fingerprint is not None
    scope_fingerprint = uuid4().hex + uuid4().hex
    scoped_plan = executed.resolved_plan.model_copy(
        update={
            "activation_generation": 1,
            "active_pointer_fingerprint": "a" * 64,
            "active_scope_fingerprint": scope_fingerprint,
        }
    )
    scoped_plan_fingerprint = resolved_semantic_plan_fingerprint(scoped_plan)
    scoped_execution = executed.execution.model_copy(
        update={"plan_fingerprint": scoped_plan_fingerprint}
    )
    scoped_executed = executed.model_copy(
        update={
            "resolved_plan": scoped_plan,
            "plan_fingerprint": scoped_plan_fingerprint,
            "execution": scoped_execution,
        }
    )
    recipe = PrepareQueryRecipe(repository).execute(scoped_executed)
    approval = RecipePublicationApproval(
        id=f"datahub-recipe-approval-{scope_fingerprint[:12]}-v{recipe.version}",
        recipe_id=recipe.id,
        recipe_version=recipe.version,
        recipe_fingerprint=recipe.fingerprint,
        actor="local-operator",
        approved_at=datetime.now(UTC),
        confirmation=RecipePublicationConfirmation.PUBLISH_VALIDATED_QUERY_RECIPE,
    )
    result = PublishQueryRecipe(repository, SqlitePublicationAuditStore(audit_path)).execute(
        recipe, approval
    )
    assert result.status in {
        RecipePublicationStatus.CREATED,
        RecipePublicationStatus.ALREADY_CURRENT,
    }
    replay = PublishQueryRecipe(repository, SqlitePublicationAuditStore(audit_path)).execute(
        recipe, approval
    )
    assert replay.status is RecipePublicationStatus.ALREADY_CURRENT
    assert replay.versioned_document_urn == result.versioned_document_urn

    reopened = DataHubQueryRecipeAdapter.from_env_file(credential)
    published = reopened.find_current(
        recipe.intent_fingerprint,
        scope_fingerprint=scope_fingerprint,
    )
    assert published is not None
    assert published.recipe.fingerprint == recipe.fingerprint
    assert published.versioned_document_urn == result.versioned_document_urn
    assert set(published.recipe.linked_asset_urns) == {
        "urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge.crm.customers,PROD)",
        "urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge.bank.account_holders,PROD)",
    }
    records = SqlitePublicationAuditStore(audit_path).list_for_approval(approval.id)
    assert len(records) == 4
    assert {record.actor for record in records} == {approval.actor}
    assert {record.new_fingerprint for record in records} == {recipe.fingerprint}

    assert (
        reopened.find_current(
            recipe.intent_fingerprint,
            scope_fingerprint="0" * 64,
        )
        is None
    )
    reused = AssessQueryRecipeReuse(reopened).execute(
        validated_request=executed.validated_request,
        resolved_plan=scoped_plan,
        plan_fingerprint=scoped_plan_fingerprint,
        query_fingerprint=executed.query_fingerprint,
        source_schema_fingerprint=recipe.source_schema_fingerprint,
    )
    assert reused.status is RecipeReuseStatus.REUSABLE
    assert reused.provenance_document_urn == result.versioned_document_urn
