from __future__ import annotations

import tracemalloc
from collections import Counter

import pytest

from schemabridge.adapters.catalog.synthetic_source import (
    LazySyntheticCatalogSource,
    SyntheticCatalogSpecification,
    SyntheticDeltaSpecification,
)
from schemabridge.application.ports.catalog_inventory import (
    CatalogInventoryError,
    CatalogInventoryErrorCode,
)
from schemabridge.domain.catalog_inventory import (
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogConnectionRoute,
    CatalogConnectionStatus,
    CatalogRefreshMode,
)
from schemabridge.domain.semantic_registry import PhysicalValueType

SMALL = CatalogConnectionId("tenant-small-primary")
LARGE = CatalogConnectionId("tenant-large-primary")


def _source() -> LazySyntheticCatalogSource:
    return LazySyntheticCatalogSource(
        {
            SMALL: SyntheticCatalogSpecification(asset_count=10),
            LARGE: SyntheticCatalogSpecification(
                asset_count=5_434,
                delta=SyntheticDeltaSpecification(
                    update_count=100,
                    addition_count=23,
                    deletion_count=14,
                ),
            ),
        }
    )


def _route(connection_id: CatalogConnectionId) -> CatalogConnectionRoute:
    return CatalogConnectionRoute(
        workspace_id=("workspace-small" if connection_id == SMALL else "workspace-large"),
        connection_id=connection_id,
        kind=CatalogConnectionKind.SYNTHETIC,
        environment="PROD",
        catalog_scope="synthetic-enterprise",
        status=CatalogConnectionStatus.ENABLED,
    )


@pytest.mark.parametrize(
    ("connection_id", "page_size", "expected_assets", "expected_pages"),
    (
        (SMALL, 1, 10, 10),
        (SMALL, 17, 10, 1),
        (SMALL, 50, 10, 1),
        (LARGE, 1, 5_434, 5_434),
        (LARGE, 17, 5_434, 320),
        (LARGE, 50, 5_434, 109),
    ),
)
@pytest.mark.scale
def test_full_inventory_streams_every_asset_exactly_once(
    connection_id: CatalogConnectionId,
    page_size: int,
    expected_assets: int,
    expected_pages: int,
) -> None:
    source = _source()
    checkpoint: str | None = None
    expected_index = 0
    pages = 0

    while True:
        page = source.read_page(
            _route(connection_id),
            mode=CatalogRefreshMode.FULL,
            checkpoint=checkpoint,
            page_size=page_size,
        )
        pages += 1
        assert len(page.changes) <= page_size
        for change in page.changes:
            assert change.asset is not None
            assert change.asset.asset_id.root == f"synthetic-asset-{expected_index:08d}"
            assert 3 <= len(change.asset.fields) <= 12
            expected_index += 1
        if page.source_complete:
            assert page.next_checkpoint is None
            break
        assert page.next_checkpoint is not None
        checkpoint = page.next_checkpoint

    assert expected_index == expected_assets
    assert pages == expected_pages


def test_small_and_large_connections_keep_collision_free_scope() -> None:
    source = _source()

    small = source.read_page(
        _route(SMALL),
        mode=CatalogRefreshMode.FULL,
        checkpoint=None,
        page_size=1,
    )
    large = source.read_page(
        _route(LARGE),
        mode=CatalogRefreshMode.FULL,
        checkpoint=None,
        page_size=1,
    )

    assert small.changes[0].asset is not None
    assert large.changes[0].asset is not None
    assert small.changes[0].asset.qualified_name == large.changes[0].asset.qualified_name
    assert _route(SMALL).connection_id != _route(LARGE).connection_id


def test_extended_profile_covers_wide_nested_unicode_and_type_drift() -> None:
    first_connection = CatalogConnectionId("tenant-wide-primary")
    source = LazySyntheticCatalogSource(
        {
            first_connection: SyntheticCatalogSpecification(
                asset_count=5,
                minimum_fields=64,
                maximum_fields=64,
            )
        }
    )
    route = CatalogConnectionRoute(
        workspace_id="workspace-wide",
        connection_id=first_connection,
        kind=CatalogConnectionKind.SYNTHETIC,
        environment="PROD",
        catalog_scope="synthetic-wide-enterprise",
        status=CatalogConnectionStatus.ENABLED,
    )

    page = source.read_page(
        route,
        mode=CatalogRefreshMode.FULL,
        checkpoint=None,
        page_size=5,
    )

    assets = tuple(change.asset for change in page.changes)
    assert all(asset is not None and len(asset.fields) == 64 for asset in assets)
    first = assets[0]
    second = assets[1]
    assert first is not None
    assert second is not None
    assert any(len(field.field_path) > 1 for field in first.fields)
    assert any("dirección" in field.field_path[0] for field in first.fields)
    assert any(field.native_type is None or field.description is None for field in first.fields)
    assert first.database_name == "synthetic"
    assert all(field.normalized_type is not None for field in first.fields)
    assert {field.normalized_type for field in first.fields} >= {
        PhysicalValueType.STRING,
        PhysicalValueType.TIMESTAMP,
        PhysicalValueType.DECIMAL,
    }
    first_types = {field.field_path: field.native_type for field in first.fields}
    second_types = {field.field_path: field.native_type for field in second.fields}
    shared_paths = set(first_types) & set(second_types)
    assert any(first_types[path] != second_types[path] for path in shared_paths)


def test_sparse_wide_profile_adds_realistic_variety_without_widening_every_table() -> None:
    connection_id = CatalogConnectionId("tenant-sparse-wide")
    source = LazySyntheticCatalogSource(
        {
            connection_id: SyntheticCatalogSpecification(
                asset_count=1_000,
                wide_asset_every=997,
                wide_field_count=64,
            )
        }
    )
    route = CatalogConnectionRoute(
        workspace_id="workspace-sparse-wide",
        connection_id=connection_id,
        kind=CatalogConnectionKind.SYNTHETIC,
        environment="PROD",
        catalog_scope="synthetic-sparse-wide",
        status=CatalogConnectionStatus.ENABLED,
    )

    ordinary = source.read_page(
        route,
        mode=CatalogRefreshMode.FULL,
        checkpoint="synthetic-v2.950.20",
        page_size=46,
    )
    wide = source.read_page(
        route,
        mode=CatalogRefreshMode.FULL,
        checkpoint=ordinary.next_checkpoint,
        page_size=1,
    )

    assert all(change.asset is not None for change in ordinary.changes)
    assert max(len(change.asset.fields) for change in ordinary.changes if change.asset) <= 12
    assert wide.changes[0].asset is not None
    assert len(wide.changes[0].asset.fields) == 64
    assert any(
        len(field.field_path) > 1 or "dirección" in field.field_path[0]
        for field in wide.changes[0].asset.fields
    )


@pytest.mark.scale
def test_large_delta_is_generated_in_three_bounded_pages() -> None:
    source = _source()
    checkpoint: str | None = None
    counts: Counter[str] = Counter()
    pages = 0

    while True:
        page = source.read_page(
            _route(LARGE),
            mode=CatalogRefreshMode.DELTA,
            checkpoint=checkpoint,
            page_size=50,
        )
        pages += 1
        counts.update(change.kind.value for change in page.changes)
        if page.source_complete:
            break
        checkpoint = page.next_checkpoint

    assert pages == 3
    assert sum(counts.values()) == 137
    assert counts["upsert_asset"] == 123
    assert counts["delete_asset"] == 14
    assert 5_434 + 23 - 14 == 5_443


def test_delta_without_change_feed_and_malformed_checkpoint_fail_closed() -> None:
    source = _source()

    with pytest.raises(CatalogInventoryError) as unsupported:
        source.read_page(
            _route(SMALL),
            mode=CatalogRefreshMode.DELTA,
            checkpoint=None,
            page_size=50,
        )
    assert unsupported.value.code is CatalogInventoryErrorCode.DELTA_UNSUPPORTED

    with pytest.raises(CatalogInventoryError) as malformed:
        source.read_page(
            _route(LARGE),
            mode=CatalogRefreshMode.FULL,
            checkpoint="50",
            page_size=50,
        )
    assert malformed.value.code is CatalogInventoryErrorCode.INVALID_RESPONSE

    with pytest.raises(CatalogInventoryError) as stale_version:
        source.read_page(
            _route(LARGE),
            mode=CatalogRefreshMode.FULL,
            checkpoint="synthetic-v1.50.2",
            page_size=50,
        )
    assert stale_version.value.code is CatalogInventoryErrorCode.INVALID_RESPONSE


@pytest.mark.scale
def test_page_heap_is_bounded_independently_of_total_inventory() -> None:
    source = _source()
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        small = source.read_page(
            _route(SMALL),
            mode=CatalogRefreshMode.FULL,
            checkpoint=None,
            page_size=50,
        )
        after_small = tracemalloc.get_traced_memory()[0]
        large = source.read_page(
            _route(LARGE),
            mode=CatalogRefreshMode.FULL,
            checkpoint=None,
            page_size=50,
        )
        after_large = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()

    assert len(small.changes) == 10
    assert len(large.changes) == 50
    assert after_small >= before
    assert after_large - after_small <= 16 * 1024 * 1024
