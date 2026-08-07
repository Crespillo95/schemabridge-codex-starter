from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import schemabridge.application.query_studio_ai_admission as ai_admission
from schemabridge.bootstrap import build_query_studio_runtime
from schemabridge.config import Settings
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.query_studio import (
    GovernedFieldSearchRequest,
    PhysicalFieldDiscoveryRequest,
    QueryStudioConfirmation,
    QueryStudioConfirmationAction,
    SemanticMatchState,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        actor_id="query-studio-analyst",
        workspace_id="query-studio-bootstrap",
        roles=frozenset({IdentityRole.ANALYST}),
        authentication_method=AuthenticationMethod.LOCAL_DEMO,
        authenticated_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )


def _settings(mode: str) -> Settings:
    return Settings(
        _env_file=None,
        OPENAI_API_KEY=None,
        DATABASE_URL=None,
        SCHEMABRIDGE_QUERY_STUDIO_AI_MODE=mode,
        SCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY=(
            "query-studio-bootstrap-signing-key-with-byte-diversity-2026"
        ),
    )


def test_fake_runtime_composes_dynamic_guided_and_confirmed_natural_paths() -> None:
    runtime = build_query_studio_runtime(
        principal=_principal(),
        repository_root=ROOT,
        settings=_settings("fake"),
    )

    guided = runtime.browse_guided.execute(
        GovernedFieldSearchRequest(scope=runtime.scope, page_size=7)
    )
    natural = runtime.prepare_natural
    confirmer = runtime.confirm_natural

    assert runtime.ai_mode == "fake"
    assert runtime.configuration.external_ai is False
    assert runtime.expansion is not None
    assert runtime.discover_physical is not None
    assert runtime.catalog_cardinality.connection_count == 1
    assert runtime.catalog_cardinality.asset_count == 11
    assert runtime.catalog_cardinality.field_count == 59
    assert runtime.governed_mapping_count == 31
    assert guided.page_size == 7
    assert len(guided.items) == 7
    assert guided.next_key is not None
    assert natural is not None
    assert confirmer is not None
    assert runtime.recompute_natural is not None

    preview = natural.execute(
        "agrupa por fecha de registro los clientes que sean segundo titular de una cuenta",
        UserLanguage.SPANISH,
    )

    assert preview.semantic_state is SemanticMatchState.ALIGNED
    assert preview.proposal is not None
    assert preview.expansion is not None
    assert preview.token is not None
    confirmed = confirmer.execute(
        QueryStudioConfirmation(
            original_text=(
                "agrupa por fecha de registro los clientes que sean segundo titular de una cuenta"
            ),
            language=UserLanguage.SPANISH,
            expansion=preview.expansion,
            proposal=preview.proposal,
            token=preview.token,
            action=QueryStudioConfirmationAction.CONFIRM_INTERPRETATION,
        )
    )
    assert confirmed.validated_request.request.primary_entity.root == "Customer"


def test_disabled_runtime_keeps_dynamic_guided_mode_without_provider_capability() -> None:
    runtime = build_query_studio_runtime(
        principal=_principal(),
        repository_root=ROOT,
        settings=_settings("disabled"),
    )

    page = runtime.browse_guided.execute(
        GovernedFieldSearchRequest(scope=runtime.scope, page_size=1)
    )

    assert runtime.ai_mode == "disabled"
    assert runtime.configuration.external_ai is False
    assert runtime.prepare_natural is None
    assert runtime.confirm_natural is None
    assert runtime.recompute_natural is None
    assert runtime.expansion is None
    assert runtime.discover_physical is not None
    assert len(page.items) == 1
    assert page.next_key is not None
    physical = runtime.discover_physical.execute(
        PhysicalFieldDiscoveryRequest(scope=runtime.scope, page_size=1)
    )
    assert len(physical.items) == 1
    assert not hasattr(physical.items[0], "candidate_id")


def test_live_bootstrap_composes_local_expansion_and_only_admitted_provider_intent() -> None:
    """Keep the live provider surface at one interpretation-only composition seam."""

    path = ROOT / "src" / "schemabridge" / "bootstrap.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_query_studio_runtime"
    )
    calls = tuple(
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    )
    call_names = tuple(node.func.id for node in calls if isinstance(node.func, ast.Name))
    assigned_constructors: dict[str, set[str]] = {}
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
        ):
            assigned_constructors.setdefault(node.targets[0].id, set()).add(node.value.func.id)

    assert call_names.count("create_openai_query_studio_intent_adapter_from_environment") == 1
    assert "create_openai_query_studio_adapters_from_environment" not in call_names
    assert "OpenAIDescriptionExpansionAdapter" not in call_names
    assert "AdmittedDescriptionExpansion" not in call_names
    assert assigned_constructors["expansion"] == {
        "BoundaryScreenedDescriptionExpansionPreflight",
        "DeterministicDescriptionExpansion",
    }
    assert assigned_constructors["interpreter"] == {
        "AdmittedQueryStudioIntent",
        "DeterministicQueryStudioIntent",
    }
    assert "AdmittedDescriptionExpansion" not in ai_admission.__all__
    assert ai_admission.__all__ == [
        "AdmittedAdvancedInterpretation",
        "AdmittedAdvancedMentionExtraction",
        "AdmittedQueryStudioIntent",
    ]
