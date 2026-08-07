"""Read-only regression over the retained M27 PostgreSQL browser fixture."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from scripts import m27_browser_acceptance_runtime as browser_runtime

from schemabridge.adapters.catalog.postgres_governed_search import (
    PostgresGovernedBindingFactsSearch,
)
from schemabridge.adapters.evaluation import query_studio_live as live_evaluation
from schemabridge.adapters.semantic_registry.recorded import (
    RecordedGovernedSemanticRegistry,
)
from schemabridge.application.query_studio import (
    _score_binding_from_registry,
    rank_governed_bindings,
)
from schemabridge.domain.query_studio import (
    DescriptionExpansionInput,
    DescriptionExpansionRoute,
    DescriptionQuery,
    GovernedBindingFactsRequest,
    atomic_description_expansion,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


def test_retained_small_browser_fixture_ranks_the_v45_field_match_regressions() -> None:
    """The real browser catalog and public corpus share one exact connection identity."""

    state_file = browser_runtime.DEFAULT_SMALL_STATE_DIR / browser_runtime.STATE_FILE
    if not state_file.is_file():
        pytest.skip("retained M27 small browser fixture is unavailable")
    state = browser_runtime.BrowserAcceptanceState.from_json(state_file.read_bytes())
    runtime_dsn = browser_runtime._runtime_dsn(state.database)
    try:
        with psycopg.connect(runtime_dsn) as connection:
            connection.execute("SELECT 1").fetchone()
    except psycopg.Error:
        pytest.skip("retained M27 small browser database is unavailable")

    scope = SemanticRegistryScope(
        workspace_id=state.workspace_id,
        catalog_scope=browser_runtime.CATALOG_SCOPE,
        registry_id=browser_runtime.REGISTRY_ID,
    )
    facts = PostgresGovernedBindingFactsSearch(runtime_dsn).search(
        GovernedBindingFactsRequest(
            scope=scope,
            page_size=50,
        )
    )
    assert len(facts.items) == state.governed_mapping_count == 31
    assert facts.next_key is None
    assert {item.locator.asset.connection_id.root for item in facts.items} == {
        browser_runtime.PRIMARY_CONNECTION_ID
    }

    registry = (
        RecordedGovernedSemanticRegistry(
            ROOT / "demo/ground_truth/registries/manifest.yml",
            scope,
        )
        .load()
        .registry
    )
    corpus, _corpus_sha256 = live_evaluation._load_synthetic_corpus(ROOT)
    cases = {
        item.id: item
        for item in corpus.positive_mappings
        if item.id in {"product-active", "order-total"}
    }
    assert set(cases) == {"product-active", "order-total"}

    for case in cases.values():
        expansion = atomic_description_expansion(
            DescriptionExpansionInput(
                text=DescriptionQuery(case.descriptions.es),
                language=live_evaluation.UserLanguage.SPANISH,
                lane=DescriptionExpansionRoute.FIELD_MATCH,
            )
        )
        assert len(expansion.probes) == 1
        probe = expansion.probes[0]
        ranked = rank_governed_bindings(
            tuple(
                scored
                for item in facts.items
                if (
                    scored := _score_binding_from_registry(
                        item=item,
                        query=probe.query.root,
                        registry=registry,
                    )
                )
                is not None
            )
        )
        identities = tuple(
            (
                item.logical_field.root,
                item.locator.asset.connection_id.root,
                item.physical_field.root,
            )
            for item in ranked[:3]
        )
        assert (
            case.logical_field,
            browser_runtime.PRIMARY_CONNECTION_ID,
            case.physical_field,
        ) in identities
