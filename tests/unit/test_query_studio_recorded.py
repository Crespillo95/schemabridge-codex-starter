"""Recorded Query Studio retrieval over the complete synthetic governed registry."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
import yaml
from pydantic import ValidationError

from schemabridge.adapters.query_studio.recorded import (
    RECORDED_MATCHER_VERSION,
    RecordedGovernedBindingFactsSearch,
)
from schemabridge.adapters.semantic_registry.recorded import (
    RecordedGovernedSemanticRegistry,
)
from schemabridge.application.query_studio import (
    QueryStudioError,
    QueryStudioErrorCode,
    RegistryAwareGovernedFieldSearch,
    SearchGovernedFields,
)
from schemabridge.domain.query_studio import (
    DescriptionQuery,
    GovernedBindingFactsRequest,
    GovernedFieldSearchFilters,
    GovernedFieldSearchPage,
    GovernedFieldSearchRequest,
)
from schemabridge.domain.request_context import LogicalFieldRole
from schemabridge.domain.semantic_registry import (
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
)

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "demo/ground_truth/registries/manifest.yml"
CATALOG = ROOT / "demo/datahub/catalog_snapshot.json"
CORPUS = ROOT / "demo/ground_truth/query_studio_matching.yml"


@dataclass
class _StaticRegistry:
    value: ScopedSemanticRegistrySnapshot

    @property
    def scope(self) -> SemanticRegistryScope:
        return self.value.scope

    def load(self) -> ScopedSemanticRegistrySnapshot:
        return self.value


@dataclass
class _CountingFacts:
    delegate: RecordedGovernedBindingFactsSearch
    calls: int = 0

    def search(self, request: GovernedBindingFactsRequest) -> GovernedFieldSearchPage:
        self.calls += 1
        return self.delegate.search(request)


@dataclass(frozen=True)
class _Stack:
    registry: _StaticRegistry
    facts: RecordedGovernedBindingFactsSearch
    search: RegistryAwareGovernedFieldSearch
    service: SearchGovernedFields


@pytest.fixture(scope="module")
def stack() -> _Stack:
    scope = SemanticRegistryScope(
        workspace_id="query-studio-recorded",
        catalog_scope="synthetic-demo",
        registry_id="synthetic_enterprise",
    )
    loaded = RecordedGovernedSemanticRegistry(MANIFEST, scope).load()
    registry = _StaticRegistry(loaded)
    facts = RecordedGovernedBindingFactsSearch(loaded, CATALOG)
    search = RegistryAwareGovernedFieldSearch(registry=registry, facts=facts)
    return _Stack(
        registry=registry,
        facts=facts,
        search=search,
        service=SearchGovernedFields(registry=registry, search=search),
    )


def _corpus() -> dict[str, object]:
    payload: object = yaml.safe_load(CORPUS.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _rows(payload: dict[str, object], key: str) -> list[dict[str, object]]:
    rows = payload[key]
    assert isinstance(rows, list)
    assert all(isinstance(row, dict) for row in rows)
    return cast(list[dict[str, object]], rows)


def _request(
    scope: SemanticRegistryScope,
    *,
    query: str | None = None,
    page_size: int = 50,
) -> GovernedFieldSearchRequest:
    return GovernedFieldSearchRequest(
        scope=scope,
        query=DescriptionQuery(query) if query is not None else None,
        page_size=page_size,
    )


def _traverse(
    service: SearchGovernedFields,
    request: GovernedFieldSearchRequest,
) -> tuple[tuple[str, ...], tuple[int, ...]]:
    identities: list[str] = []
    rows_read: list[int] = []
    current = request
    while True:
        page = service.execute(current)
        identities.extend(item.binding_id for item in page.items)
        rows_read.append(page.rows_read)
        assert len(page.items) <= request.page_size
        assert page.rows_read <= request.page_size + 1
        if page.next_key is None:
            break
        current = GovernedFieldSearchRequest(
            scope=request.scope,
            query=request.query,
            filters=request.filters,
            page_size=request.page_size,
            after=page.next_key,
            expected_scope=page.scope,
        )
    return tuple(identities), tuple(rows_read)


@pytest.mark.parametrize("page_size", [1, 17, 50])
def test_recorded_search_traverses_every_governed_mapping_by_stable_keyset(
    stack: _Stack,
    page_size: int,
) -> None:
    identities, rows_read = _traverse(
        stack.service,
        _request(stack.registry.scope, page_size=page_size),
    )
    expected_count = len(stack.registry.value.registry.mapping_set.mappings)

    assert expected_count == 31
    assert len(identities) == expected_count
    assert len(set(identities)) == expected_count
    assert all(value <= page_size + 1 for value in rows_read)

    baseline, _ = _traverse(
        stack.service,
        _request(stack.registry.scope, page_size=50),
    )
    assert identities == baseline


def test_recorded_search_is_bounded_per_page_not_by_a_total_result_cap(
    stack: _Stack,
) -> None:
    first = stack.service.execute(_request(stack.registry.scope, page_size=17))

    assert len(first.items) == 17
    assert first.rows_read == 18
    assert first.next_key is not None
    assert first.scope.evidence_baseline_revision == 1
    assert all(item.signals.total <= 80_000 for item in first.items)
    assert all(signal.value <= 10_000 for item in first.items for signal in item.signals.signals)
    with pytest.raises(ValidationError):
        _request(stack.registry.scope, page_size=51)


def test_all_bilingual_descriptions_meet_deterministic_quality_gates(
    stack: _Stack,
) -> None:
    positions: list[int] = []
    rows = _rows(_corpus(), "positive_mappings")

    for row in rows:
        expected = row["logical_field"]
        descriptions = row["descriptions"]
        assert isinstance(expected, str)
        assert isinstance(descriptions, dict)
        for text in descriptions.values():
            assert isinstance(text, str)
            page = stack.service.execute(
                _request(stack.registry.scope, query=text),
            )
            ranked = tuple(item.logical_field.root for item in page.items)
            positions.append(ranked.index(expected) + 1 if expected in ranked else 10_000)

    top_1 = sum(position == 1 for position in positions) / len(positions)
    top_3 = sum(position <= 3 for position in positions) / len(positions)
    reciprocal_rank = sum(
        0.0 if position == 10_000 else 1 / position for position in positions
    ) / len(positions)

    assert len(positions) == 62
    assert top_1 >= 0.85
    assert top_3 == 1.0
    assert reciprocal_rank >= 0.90


def test_slight_spanish_description_selects_registration_date_first(
    stack: _Stack,
) -> None:
    page = stack.service.execute(
        _request(
            stack.registry.scope,
            query="fecha en la que se registró el cliente",
        )
    )

    assert page.items
    assert page.items[0].logical_field.root == "Customer.registration_date"


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("fecha de registro", "Customer.registration_date"),
        ("fecha de registro del cliente", "Customer.registration_date"),
        ("registration date", "Customer.registration_date"),
        ("customer registration date", "Customer.registration_date"),
        ("identificador estable del cliente", "Customer.customer_key"),
        ("stable customer identifier", "Customer.customer_key"),
        ("segundo titular", "AccountHolder.holder_role"),
        ("second holder", "AccountHolder.holder_role"),
        ("secondary account holder", "AccountHolder.holder_role"),
        (
            "tipo o posición del titular de la cuenta",
            "AccountHolder.holder_role",
        ),
        ("account holder role or position", "AccountHolder.holder_role"),
    ],
)
def test_registry_semantics_rank_atomic_es_en_descriptions_before_physical_fallback(
    stack: _Stack,
    description: str,
    expected: str,
) -> None:
    counting = _CountingFacts(stack.facts)
    service = SearchGovernedFields(
        registry=stack.registry,
        search=RegistryAwareGovernedFieldSearch(
            registry=stack.registry,
            facts=counting,
        ),
    )

    page = service.execute(_request(stack.registry.scope, query=description))

    assert page.items[0].logical_field.root == expected
    assert page.items[0].signals.total > 0
    assert counting.calls == 1
    assert counting.delegate.scope == page.scope


def test_true_negatives_return_no_executable_binding(stack: _Stack) -> None:
    for row in _rows(_corpus(), "true_negatives"):
        text = row["text"]
        assert isinstance(text, str)
        page = stack.service.execute(_request(stack.registry.scope, query=text))
        assert page.items == ()


def test_critical_ambiguities_return_visible_governed_alternatives(
    stack: _Stack,
) -> None:
    governed_pairs = {
        (
            item.mapping.logical_field.root,
            item.mapping.physical_field.root,
        )
        for item in stack.registry.value.registry.mapping_set.mappings
    }
    alternatives: dict[str, GovernedFieldSearchPage] = {}
    for row in _rows(_corpus(), "critical_ambiguities"):
        ambiguity_id = row["id"]
        text = row["text"]
        assert isinstance(ambiguity_id, str)
        assert isinstance(text, str)
        page = stack.service.execute(_request(stack.registry.scope, query=text))
        alternatives[ambiguity_id] = page
        assert len(page.items) >= 2
        assert all(
            (item.logical_field.root, item.physical_field.root) in governed_pairs
            for item in page.items
        )

    order_reference = alternatives["order-reference"]
    assert len({item.signals.total for item in order_reference.items[:3]}) == 1
    assert tuple(item.logical_field.root for item in order_reference.items[:3]) == (
        "SaleLine.order_key",
        "SalesOrder.order_key",
        "Shipment.order_key",
    )
    assert all(
        not item.physical_field.root.startswith("support.")
        for page in alternatives.values()
        for item in page.items
    )


def test_description_keyset_continuation_uses_matcher_bound_score_and_is_complete(
    stack: _Stack,
) -> None:
    counting = _CountingFacts(stack.facts)
    service = SearchGovernedFields(
        registry=stack.registry,
        search=RegistryAwareGovernedFieldSearch(
            registry=stack.registry,
            facts=counting,
        ),
    )
    first = service.execute(
        _request(
            stack.registry.scope,
            query="identificador del registro",
            page_size=1,
        )
    )
    assert first.next_key is not None
    raw_fingerprint = counting.delegate.search(
        GovernedBindingFactsRequest(
            scope=stack.registry.scope,
            page_size=50,
            logical_request_fingerprint=first.request_fingerprint,
        )
    ).binding_facts_fingerprint
    assert first.binding_facts_fingerprint != raw_fingerprint

    second = service.execute(
        GovernedFieldSearchRequest(
            scope=stack.registry.scope,
            query=DescriptionQuery("identificador del registro"),
            page_size=1,
            after=first.next_key,
            expected_scope=first.scope,
        )
    )

    assert second.items
    assert second.items[0].binding_id != first.items[0].binding_id
    assert second.binding_facts_fingerprint == first.binding_facts_fingerprint
    assert counting.calls == 2


def test_cursor_replay_with_changed_query_filter_or_scope_fails_before_facts_io(
    stack: _Stack,
) -> None:
    counting = _CountingFacts(stack.facts)
    aware = RegistryAwareGovernedFieldSearch(
        registry=stack.registry,
        facts=counting,
    )
    service = SearchGovernedFields(registry=stack.registry, search=aware)
    first = service.execute(_request(stack.registry.scope, page_size=1))
    assert first.next_key is not None
    continuation = GovernedFieldSearchRequest(
        scope=stack.registry.scope,
        page_size=1,
        after=first.next_key,
        expected_scope=first.scope,
    )
    assert counting.calls == 1

    changed_query = continuation.model_copy(
        update={"query": DescriptionQuery("saldo disponible de la cuenta")}
    )
    with pytest.raises(ValidationError):
        service.execute(changed_query)
    assert counting.calls == 1

    changed_filter = continuation.model_copy(
        update={
            "filters": GovernedFieldSearchFilters(
                roles=(LogicalFieldRole.TEMPORAL,),
            )
        }
    )
    with pytest.raises(ValidationError):
        service.execute(changed_filter)
    assert counting.calls == 1

    changed_scope_state = continuation.model_copy(
        update={
            "expected_scope": first.scope.model_copy(
                update={"evidence_head_revision": first.scope.evidence_head_revision + 1}
            )
        }
    )
    with pytest.raises(ValidationError):
        service.execute(changed_scope_state)
    assert counting.calls == 1

    other_scope = continuation.model_copy(
        update={
            "scope": SemanticRegistryScope(
                workspace_id="another-workspace",
                catalog_scope=stack.registry.scope.catalog_scope,
                registry_id=stack.registry.scope.registry_id,
            )
        }
    )
    with pytest.raises(QueryStudioError) as error:
        service.execute(other_scope)
    assert error.value.code is QueryStudioErrorCode.INVALID_CONTEXT
    assert counting.calls == 1


def test_identical_searches_have_byte_stable_order_and_scores(stack: _Stack) -> None:
    request = _request(
        stack.registry.scope,
        query="fecha en la que se registró el cliente",
    )
    first = stack.service.execute(request)
    second = stack.service.execute(request)

    assert RECORDED_MATCHER_VERSION == "m27-deterministic-v9"
    assert first.model_dump_json() == second.model_dump_json()


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("stable customer identifier", "Customer.customer_key"),
        ("stable product identifier", "Product.product_key"),
        ("stable order identifier", "SalesOrder.order_key"),
    ],
)
def test_count_distinct_identifier_variant_has_one_strict_recorded_winner(
    stack: _Stack,
    query: str,
    expected: str,
) -> None:
    page = stack.service.execute(
        GovernedFieldSearchRequest(
            scope=stack.registry.scope,
            query=DescriptionQuery(query),
            filters=GovernedFieldSearchFilters(
                roles=(LogicalFieldRole.IDENTIFIER,),
            ),
            page_size=20,
        )
    )

    assert page.items
    assert page.items[0].logical_field.root == expected
    assert len(page.items) == 1 or page.items[0].signals.total > page.items[1].signals.total
