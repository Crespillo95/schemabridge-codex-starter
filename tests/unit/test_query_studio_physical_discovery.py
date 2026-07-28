from __future__ import annotations

import json
import tracemalloc
from pathlib import Path

import pytest
from pydantic import ValidationError

from schemabridge.adapters.query_studio.recorded_physical import (
    RecordedPhysicalFieldDiscovery,
)
from schemabridge.application.ports.query_studio import (
    QueryStudioPortError,
    QueryStudioPortErrorCode,
)
from schemabridge.application.query_studio import (
    DiscoverPhysicalFields,
    InspectPhysicalDiscoveryCardinality,
)
from schemabridge.domain.query_studio import (
    DescriptionQuery,
    PhysicalDiscoveryCursor,
    PhysicalDiscoveryStatus,
    PhysicalFieldDiscoveryRequest,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope

ROOT = Path(__file__).resolve().parents[2]
SIGNING_KEY = b"recorded-physical-discovery-test-key-with-byte-diversity"


def _scope(workspace_id: str = "physical-discovery") -> SemanticRegistryScope:
    return SemanticRegistryScope(
        workspace_id=workspace_id,
        catalog_scope="synthetic-demo",
        registry_id="synthetic_enterprise",
    )


def _adapter(path: Path | None = None) -> RecordedPhysicalFieldDiscovery:
    return RecordedPhysicalFieldDiscovery(
        path or ROOT / "demo/datahub/catalog_snapshot.json",
        SIGNING_KEY,
    )


def test_recorded_physical_discovery_is_separate_non_executable_and_paginated() -> None:
    service = DiscoverPhysicalFields(_adapter())
    first = service.execute(PhysicalFieldDiscoveryRequest(scope=_scope(), page_size=3))

    assert len(first.items) == 3
    assert first.next_cursor is not None
    assert all(item.status is PhysicalDiscoveryStatus.NEEDS_MAPPING_REVIEW for item in first.items)
    assert all(not hasattr(item, "candidate_id") for item in first.items)

    second = service.execute(
        PhysicalFieldDiscoveryRequest(
            scope=_scope(),
            page_size=3,
            cursor=first.next_cursor,
        )
    )
    first_ids = {
        (item.locator.asset.asset_id.root, item.locator.field_path) for item in first.items
    }
    second_ids = {
        (item.locator.asset.asset_id.root, item.locator.field_path) for item in second.items
    }
    assert first_ids.isdisjoint(second_ids)


def test_recorded_physical_cursor_rejects_tampering_query_and_workspace_replay() -> None:
    adapter = _adapter()
    first = adapter.search(PhysicalFieldDiscoveryRequest(scope=_scope(), page_size=1))
    assert first.next_cursor is not None
    raw = first.next_cursor.root
    replacement = "A" if raw[-1] != "A" else "B"

    requests = (
        PhysicalFieldDiscoveryRequest(
            scope=_scope(),
            page_size=1,
            cursor=PhysicalDiscoveryCursor(raw[:-1] + replacement),
        ),
        PhysicalFieldDiscoveryRequest(
            scope=_scope(),
            query=DescriptionQuery("customer"),
            page_size=1,
            cursor=first.next_cursor,
        ),
        PhysicalFieldDiscoveryRequest(
            scope=_scope("different-workspace"),
            page_size=1,
            cursor=first.next_cursor,
        ),
    )
    for request in requests:
        with pytest.raises(QueryStudioPortError) as failure:
            adapter.search(request)
        assert failure.value.code is QueryStudioPortErrorCode.SCOPE_CHANGED


def test_recorded_physical_search_matches_public_metadata_only() -> None:
    page = _adapter().search(
        PhysicalFieldDiscoveryRequest(
            scope=_scope(),
            query=DescriptionQuery("Customer identifier"),
            page_size=10,
        )
    )

    assert page.items
    assert all(item.status is PhysicalDiscoveryStatus.NEEDS_MAPPING_REVIEW for item in page.items)
    assert any(item.locator.field_path == ("customer_id",) for item in page.items)


def test_recorded_physical_cardinality_is_observed_without_rows() -> None:
    cardinality = InspectPhysicalDiscoveryCardinality(_adapter()).execute(_scope())

    assert cardinality.scope == _scope()
    assert cardinality.connection_count == 1
    assert cardinality.asset_count == 11
    assert cardinality.field_count == 59
    assert len(cardinality.catalog_generation_vector_fingerprint) == 64
    with pytest.raises(ValidationError):
        cardinality.asset_count = 12  # type: ignore[misc]


@pytest.mark.scale
def test_5434_table_41028_field_discovery_keeps_memory_bounded(tmp_path: Path) -> None:
    catalog = tmp_path / "recorded-5434.json"
    _write_scaled_catalog(catalog, asset_count=5_434, field_count=41_028)
    adapter = _adapter(catalog)

    tracemalloc.start()
    page = adapter.search(
        PhysicalFieldDiscoveryRequest(
            scope=_scope(),
            query=DescriptionQuery("needle-final-field"),
            page_size=1,
        )
    )
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(page.items) == 1
    assert page.items[0].asset_qualified_name == "synthetic.asset_05433"
    assert page.items[0].status is PhysicalDiscoveryStatus.NEEDS_MAPPING_REVIEW
    assert peak < 8 * 1_024 * 1_024

    tracemalloc.start()
    cardinality = InspectPhysicalDiscoveryCardinality(adapter).execute(_scope())
    _current, count_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert cardinality.connection_count == 1
    assert cardinality.asset_count == 5_434
    assert cardinality.field_count == 41_028
    assert count_peak < 8 * 1_024 * 1_024


def _write_scaled_catalog(path: Path, *, asset_count: int, field_count: int) -> None:
    minimum = asset_count * 7
    extra = field_count - minimum
    assert 0 <= extra <= asset_count
    written_fields = 0
    with path.open("w", encoding="utf-8") as handle:
        handle.write('{"fixture_kind":"synthetic-scale","assets":[')
        for asset_index in range(asset_count):
            if asset_index:
                handle.write(",")
            fields_in_asset = 8 if asset_index < extra else 7
            fields = []
            for field_index in range(fields_in_asset):
                is_final = asset_index == asset_count - 1 and field_index == fields_in_asset - 1
                fields.append(
                    {
                        "field_path": f"field_{field_index:02d}",
                        "native_type": "TEXT",
                        "description": ("needle-final-field" if is_final else "synthetic decoy"),
                        "nullable": True,
                        "is_part_of_key": False,
                        "tags": ["synthetic"],
                        "glossary_terms": [],
                    }
                )
            json.dump(
                {
                    "dataset": f"synthetic.asset_{asset_index:05d}",
                    "description": "Synthetic bounded-memory scale asset.",
                    "fields": fields,
                },
                handle,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            written_fields += fields_in_asset
        handle.write("]}")
    assert written_fields == field_count
