"""PostgreSQL workflow/pointer integration with the persistent fake recipe catalog."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from tests.unit.test_recipe_migration import (
    _approval,
    _bind_to_pointer,
    _completed,
    _publish,
    _raw_recipe_payload,
)

from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.adapters.control_plane.postgres_registry_control import (
    PostgresRegistryControlStore,
)
from schemabridge.adapters.recipes.sqlite import SqliteQueryRecipeRepository
from schemabridge.adapters.storage.postgres import PostgresWorkflowDraftStore
from schemabridge.adapters.storage.publication_audit import SqlitePublicationAuditStore
from schemabridge.application.ports.recipes import RecipeError, RecipeErrorCode
from schemabridge.application.query_recipes import (
    PrepareStoredStaleQueryRecipeMigration,
    PublishStaleQueryRecipeMigration,
)
from schemabridge.application.registry_control import PrepareRegistryActivationApproval
from schemabridge.domain.recipes import RecipePublicationStatus
from schemabridge.domain.registry_control import (
    RegistryActivationAction,
    RegistryActivationConfirmation,
    RegistryActivationProposal,
    build_registry_activation_transition,
    build_registry_projection_outbox,
)
from schemabridge.domain.semantic_registry import (
    SemanticRegistryScope,
    datahub_registry_document_urn,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations/control_plane"
RUNTIME_DSN = (
    "postgresql://schemabridge_runtime:schemabridge_runtime@127.0.0.1:55434/schemabridge_control"
)
MIGRATOR_DSN = (
    "postgresql://schemabridge_migrator:schemabridge_migrator@127.0.0.1:55434/schemabridge_control"
)
AUDIT_KEYS = {"v1": b"control-audit-key-0123456789-abcdef"}
NOW = datetime(2026, 7, 23, 19, 0, tzinfo=UTC)


def _dsn(name: str, fallback: str) -> str:
    return os.environ.get(name, fallback)


@pytest.fixture(scope="module", autouse=True)
def _require_current_control_schema() -> None:
    PostgresControlPlaneMigrator(
        _dsn("SCHEMABRIDGE_TEST_CONTROL_MIGRATOR_DATABASE_URL", MIGRATOR_DSN),
        MIGRATIONS,
    ).require_current()


def test_postgres_stored_migration_preserves_history_and_stale_retry_writes_nothing(
    tmp_path: Path,
) -> None:
    namespace = uuid4().hex
    workspace_id = f"recipe-workspace-{namespace}"
    owner_actor_id = f"recipe-owner-{namespace}"
    scope = SemanticRegistryScope(
        workspace_id=workspace_id,
        catalog_scope="synthetic-demo",
        registry_id="synthetic_enterprise",
    )
    local_database = tmp_path / "recipe-migration.db"
    repository = SqliteQueryRecipeRepository(local_database)
    historical_workflow = _completed(
        local_database,
        f"recipe-historical-{namespace[:12]}",
    )
    historical = _publish(
        repository,
        local_database,
        historical_workflow,
        label=f"historical-{namespace[:8]}",
    )
    historical_bytes = _raw_recipe_payload(
        local_database,
        historical.recipe.intent_fingerprint,
        historical.recipe.version,
    )
    replacement_workflow = _completed(
        local_database,
        f"recipe-replacement-{namespace[:12]}",
    )
    assert replacement_workflow.resolved_plan is not None

    proposal = RegistryActivationProposal(
        action=RegistryActivationAction.ACTIVATE,
        scope=scope,
        expected_generation=0,
        target_registry_version=replacement_workflow.resolved_plan.context_version,
        target_registry_fingerprint=replacement_workflow.resolved_plan.context_fingerprint,
        target_registry_urn=datahub_registry_document_urn(
            scope,
            replacement_workflow.resolved_plan.context_version,
        ),
        target_publication_approval_id=f"recipe-publication-{namespace}",
        decision_ids=(f"recipe-decision-{namespace}",),
    )
    approval = PrepareRegistryActivationApproval().execute(
        proposal,
        actor=f"recipe-registry-operator-{namespace[:8]}",
        approved_at=NOW,
        confirmation=RegistryActivationConfirmation.ACTIVATE_APPROVED_REGISTRY_VERSION,
    )
    transition = build_registry_activation_transition(
        proposal,
        approval,
        previous_pointer=None,
        committed_at=NOW + timedelta(seconds=1),
    )
    registry_store = PostgresRegistryControlStore(
        _dsn("SCHEMABRIDGE_TEST_CONTROL_DATABASE_URL", RUNTIME_DSN),
        AUDIT_KEYS,
        "v1",
    )
    registry_store.commit_transition(
        transition,
        build_registry_projection_outbox(transition),
    )
    active_pointer = transition.active_pointer
    bound_workflow = _bind_to_pointer(replacement_workflow, active_pointer)
    workflow_store = PostgresWorkflowDraftStore(
        _dsn("SCHEMABRIDGE_TEST_CONTROL_DATABASE_URL", RUNTIME_DSN),
        workspace_id=workspace_id,
        owner_actor_id=owner_actor_id,
    )
    workflow_store.save(bound_workflow, expected_revision=None)

    migration = PrepareStoredStaleQueryRecipeMigration(
        store=workflow_store,
        repository=repository,
        pointers=registry_store,
        scope=scope,
    ).execute(
        workflow_id=bound_workflow.id,
        intent_fingerprint=historical.recipe.intent_fingerprint,
    )
    migration_approval = _approval(
        migration.replacement,
        label=f"migration-{namespace[:8]}",
    )
    publisher = PublishStaleQueryRecipeMigration(
        repository,
        SqlitePublicationAuditStore(local_database),
    )
    result = publisher.execute(migration, migration_approval)

    assert result.status is RecipePublicationStatus.CREATED
    current = repository.find_current(historical.recipe.intent_fingerprint)
    assert current is not None
    assert current.recipe == migration.replacement
    assert current.recipe.version == historical.recipe.version + 1
    assert (
        _raw_recipe_payload(
            local_database,
            historical.recipe.intent_fingerprint,
            historical.recipe.version,
        )
        == historical_bytes
    )
    with closing(sqlite3.connect(local_database)) as connection:
        before_stale_retry = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM fake_query_recipes WHERE intent_fingerprint = ?),
              (SELECT count(*) FROM publication_target_audit)
            """,
            (historical.recipe.intent_fingerprint,),
        ).fetchone()

    with pytest.raises(RecipeError) as stale:
        publisher.execute(migration, migration_approval)

    assert stale.value.code is RecipeErrorCode.CURRENT_CHANGED
    with closing(sqlite3.connect(local_database)) as connection:
        after_stale_retry = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM fake_query_recipes WHERE intent_fingerprint = ?),
              (SELECT count(*) FROM publication_target_audit)
            """,
            (historical.recipe.intent_fingerprint,),
        ).fetchone()
    assert after_stale_retry == before_stale_retry
    assert (
        _raw_recipe_payload(
            local_database,
            historical.recipe.intent_fingerprint,
            historical.recipe.version,
        )
        == historical_bytes
    )
