"""Composition and operator CLI checks for stale query-recipe migration."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

import schemabridge.bootstrap as bootstrap_module
import schemabridge.entrypoints.cli.main as cli_module
from schemabridge.application.query_recipes import (
    PrepareStoredStaleQueryRecipeMigration,
    PublishStaleQueryRecipeMigration,
)
from schemabridge.bootstrap import (
    build_query_recipe_migration_preparer,
    build_query_recipe_migration_publisher,
)
from schemabridge.config import Settings
from schemabridge.domain.recipes import (
    RecipePublicationConfirmation,
    RecipePublicationStatus,
    RecipeStalenessCode,
)
from schemabridge.entrypoints.cli.main import app

runner = CliRunner()
WORKSPACE = "sb_workspace_recipe_cli"
OWNER = "sb_actor_recipe_owner"
WORKFLOW = "recipe-migration-workflow"
INTENT = "a" * 64
PROPOSAL_FINGERPRINT = "b" * 64


def _proposal() -> SimpleNamespace:
    historical_recipe = SimpleNamespace(
        id="query_recipe_aaaaaaaaaaaaaaaa",
        version=1,
        fingerprint="c" * 64,
        intent_fingerprint=INTENT,
    )
    replacement = SimpleNamespace(
        id=historical_recipe.id,
        version=2,
        fingerprint="d" * 64,
        source_workflow_id=WORKFLOW,
    )
    return SimpleNamespace(
        fingerprint=PROPOSAL_FINGERPRINT,
        historical=SimpleNamespace(recipe=historical_recipe),
        historical_payload_fingerprint="e" * 64,
        historical_snapshot_fingerprint="f" * 64,
        replacement=replacement,
        assessment=SimpleNamespace(
            reasons=(RecipeStalenessCode.ACTIVE_REGISTRY_CHANGED,),
        ),
    )


class _Preparer:
    def __init__(self, proposal: SimpleNamespace) -> None:
        self.proposal = proposal
        self.calls: list[tuple[str, str]] = []

    def execute(
        self,
        *,
        workflow_id: str,
        intent_fingerprint: str,
    ) -> SimpleNamespace:
        self.calls.append((workflow_id, intent_fingerprint))
        return self.proposal


def test_migration_builders_bind_explicit_scope_store_repository_and_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings.model_validate(
        {"SCHEMABRIDGE_DRAFT_STORE_PATH": tmp_path / "recipe-builders.db"}
    )
    drafts = object()
    recipes = object()
    pointers = object()
    audit = object()
    draft_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        bootstrap_module,
        "build_workflow_draft_store",
        lambda **kwargs: draft_calls.append(kwargs) or drafts,
    )
    monkeypatch.setattr(
        bootstrap_module,
        "build_query_recipe_repository",
        lambda *_args, **_kwargs: recipes,
    )
    monkeypatch.setattr(
        bootstrap_module,
        "build_publication_audit_store",
        lambda *_args, **_kwargs: audit,
    )

    prepare = build_query_recipe_migration_preparer(
        "fake",
        workspace_id=WORKSPACE,
        owner_actor_id=OWNER,
        settings=settings,
        pointer_store=pointers,
    )
    publish = build_query_recipe_migration_publisher(
        "fake",
        workspace_id=WORKSPACE,
        settings=settings,
    )

    assert isinstance(prepare, PrepareStoredStaleQueryRecipeMigration)
    assert prepare.store is drafts
    assert prepare.repository is recipes
    assert prepare.pointers is pointers
    assert prepare.scope.workspace_id == WORKSPACE
    assert draft_calls == [
        {
            "workspace_id": WORKSPACE,
            "owner_actor_id": OWNER,
            "settings": settings,
        }
    ]
    assert isinstance(publish, PublishStaleQueryRecipeMigration)
    assert publish.repository is recipes
    assert publish.audit_store is audit


def test_recipe_migration_prepare_is_read_only_and_emits_no_sql_or_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = _proposal()
    preparer = _Preparer(proposal)
    monkeypatch.setattr(cli_module, "resolve_runtime_profile", lambda: "development")
    monkeypatch.setattr(
        cli_module,
        "build_query_recipe_migration_preparer",
        lambda *_args, **_kwargs: preparer,
    )

    result = runner.invoke(
        app,
        [
            "control-plane",
            "recipe-migration",
            "prepare",
            "--workspace-id",
            WORKSPACE,
            "--workflow-id",
            WORKFLOW,
            "--intent-fingerprint",
            INTENT,
            "--owner-actor-id",
            OWNER,
            "--adapter",
            "fake",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["proposal_fingerprint"] == PROPOSAL_FINGERPRINT
    assert payload["writes_performed"] is False
    assert payload["sql_exposed"] is False
    assert payload["preview_rows_exposed"] is False
    assert "SELECT " not in result.stdout
    assert '"rows"' not in result.stdout
    assert preparer.calls == [(WORKFLOW, INTENT)]


def test_recipe_migration_publish_reprepares_exact_state_and_uses_trusted_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = _proposal()
    preparer = _Preparer(proposal)
    captured: dict[str, Any] = {}

    class _Publisher:
        def execute(self, prepared: object, approval: object) -> SimpleNamespace:
            captured["proposal"] = prepared
            captured["approval"] = approval
            return SimpleNamespace(
                status=RecipePublicationStatus.CREATED,
                failure_code=None,
                recipe_fingerprint=proposal.replacement.fingerprint,
                current_document_urn="urn:li:dataHubQuery:current",
                versioned_document_urn="urn:li:dataHubQuery:v2",
            )

    monkeypatch.setattr(cli_module, "resolve_runtime_profile", lambda: "development")
    monkeypatch.setattr(
        cli_module,
        "build_query_recipe_migration_preparer",
        lambda *_args, **_kwargs: preparer,
    )
    monkeypatch.setattr(
        cli_module,
        "build_query_recipe_migration_publisher",
        lambda *_args, **_kwargs: _Publisher(),
    )
    monkeypatch.setattr(
        cli_module,
        "_control_operator_actor",
        lambda _actor, *, required_role: "trusted-recipe-operator",
    )

    result = runner.invoke(
        app,
        [
            "control-plane",
            "recipe-migration",
            "publish",
            "--workspace-id",
            WORKSPACE,
            "--workflow-id",
            WORKFLOW,
            "--intent-fingerprint",
            INTENT,
            "--owner-actor-id",
            OWNER,
            "--proposal-fingerprint",
            PROPOSAL_FINGERPRINT,
            "--adapter",
            "fake",
            "--confirm",
            RecipePublicationConfirmation.PUBLISH_VALIDATED_QUERY_RECIPE.value,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["publication"]["status"] == RecipePublicationStatus.CREATED.value
    assert payload["writes_performed"] is True
    assert "SELECT " not in result.stdout
    assert '"rows"' not in result.stdout
    assert captured["proposal"] is proposal
    approval = captured["approval"]
    assert approval.actor == "trusted-recipe-operator"
    assert approval.recipe_fingerprint == proposal.replacement.fingerprint
    assert approval.confirmation is RecipePublicationConfirmation.PUBLISH_VALIDATED_QUERY_RECIPE


def test_recipe_migration_publish_rejects_stale_fingerprint_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = _proposal()
    preparer = _Preparer(proposal)
    monkeypatch.setattr(cli_module, "resolve_runtime_profile", lambda: "development")
    monkeypatch.setattr(
        cli_module,
        "build_query_recipe_migration_preparer",
        lambda *_args, **_kwargs: preparer,
    )
    monkeypatch.setattr(
        cli_module,
        "build_query_recipe_migration_publisher",
        lambda *_args, **_kwargs: pytest.fail("stale proposal must not compose a publisher"),
    )
    monkeypatch.setattr(
        cli_module,
        "_control_operator_actor",
        lambda *_args, **_kwargs: pytest.fail("stale proposal must fail before operator approval"),
    )

    result = runner.invoke(
        app,
        [
            "control-plane",
            "recipe-migration",
            "publish",
            "--workspace-id",
            WORKSPACE,
            "--workflow-id",
            WORKFLOW,
            "--intent-fingerprint",
            INTENT,
            "--owner-actor-id",
            OWNER,
            "--proposal-fingerprint",
            "0" * 64,
            "--adapter",
            "fake",
            "--confirm",
            RecipePublicationConfirmation.PUBLISH_VALIDATED_QUERY_RECIPE.value,
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "recipe_approval_mismatch"
