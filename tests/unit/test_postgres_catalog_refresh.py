from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from schemabridge.adapters.catalog.postgres_inventory import _asset_page, _field_page
from schemabridge.adapters.catalog.postgres_refresh import (
    _asset_key,
    _capability_digest,
    _field_key,
    _lease_seconds,
    _refresh_from_row,
    _request_fingerprint,
)
from schemabridge.domain.catalog_inventory import (
    CatalogAssetId,
    CatalogAssetLocator,
    CatalogConnectionId,
    CatalogRefreshCommand,
    CatalogRefreshId,
    CatalogRefreshMode,
    CatalogRefreshStatus,
)
from schemabridge.domain.semantic_registry import PhysicalValueType

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
SHA_A = "a" * 64


def _command(*, requested_at: datetime = NOW, digest: str = SHA_A) -> CatalogRefreshCommand:
    return CatalogRefreshCommand(
        workspace_id="workspace-alpha",
        connection_id=CatalogConnectionId("connection-alpha"),
        mode=CatalogRefreshMode.FULL,
        requested_by="actor-alpha",
        requested_at=requested_at,
        idempotency_digest=digest,
    )


def test_request_fingerprint_binds_logical_request_but_not_retry_metadata() -> None:
    first = _request_fingerprint(_command())
    replay = _request_fingerprint(
        _command(
            requested_at=NOW + timedelta(minutes=1),
            digest="b" * 64,
        )
    )
    changed = _request_fingerprint(_command().model_copy(update={"mode": CatalogRefreshMode.DELTA}))

    assert first == replay
    assert first != changed


def test_inventory_keys_are_canonical_and_unambiguous() -> None:
    asset_id = "urn:li:dataset:(urn:li:dataPlatform:postgres,public.customers,PROD)"

    assert _asset_key(asset_id) == hashlib.sha256(asset_id.encode()).hexdigest()
    assert _field_key(("address", "city")) == _field_key(("address", "city"))
    assert _field_key(("address.city",)) != _field_key(("address", "city"))


def test_capability_and_lease_bounds_fail_before_database_io() -> None:
    capability = "0123456789abcdef" * 2

    assert _capability_digest(capability) == hashlib.sha256(capability.encode()).hexdigest()
    assert _lease_seconds(timedelta(seconds=10)) == 10
    assert _lease_seconds(timedelta(minutes=5)) == 300
    with pytest.raises(ValueError, match="capability"):
        _capability_digest("short")
    with pytest.raises(ValueError, match="duration"):
        _lease_seconds(timedelta(seconds=9))
    with pytest.raises(ValueError, match="duration"):
        _lease_seconds(timedelta(seconds=10, microseconds=1))


def test_refresh_row_maps_physical_empty_base_and_never_exposes_raw_capability() -> None:
    requested_row = (
        "refresh-alpha",
        "workspace-alpha",
        "connection-alpha",
        "full",
        "requested",
        None,
        1,
        SHA_A,
        "actor-alpha",
        NOW,
        NOW,
        None,
        None,
        0,
        None,
        None,
        0,
        None,
        0,
        0,
        False,
        None,
        None,
        None,
        None,
    )

    requested = _refresh_from_row(requested_row)

    assert requested.refresh_id == CatalogRefreshId("refresh-alpha")
    assert requested.status is CatalogRefreshStatus.REQUESTED
    assert requested.base_generation == 0
    assert requested.target_generation == 1
    assert requested.lease is None

    leased_row = list(requested_row)
    leased_row[4] = "leased"
    leased_row[11] = "indexer-alpha"
    leased_row[12] = "b" * 64
    leased_row[13] = 1
    leased_row[14] = NOW
    leased_row[15] = NOW + timedelta(minutes=2)
    leased = _refresh_from_row(tuple(leased_row))

    assert leased.lease is not None
    assert leased.lease.capability_digest == "b" * 64
    assert not hasattr(leased.lease, "capability")


def test_inventory_rows_rehydrate_database_and_normalized_type_evidence() -> None:
    connection_id = CatalogConnectionId("connection-alpha")
    asset_page = _asset_page(
        [
            (
                "asset-alpha",
                "crm.customers",
                "customers",
                "postgres",
                "PROD",
                "schemabridge",
                "crm",
                "Synthetic customers.",
                1,
                SHA_A,
                NOW,
                "crm.customers",
                "asset-key",
            )
        ],
        workspace_id="workspace-alpha",
        connection_id=connection_id,
        generation=3,
        page_size=1,
        cursor_valid_until=None,
    )
    asset = asset_page.items[0]
    field_page = _field_page(
        [
            (
                ["customer_id"],
                "varchar(12)",
                "string",
                "Stable customer identifier.",
                False,
                True,
                ["identifier"],
                ["Customer"],
                "b" * 64,
                NOW,
                "customer_id",
                "field-key",
            )
        ],
        asset=CatalogAssetLocator(
            workspace_id="workspace-alpha",
            connection_id=connection_id,
            asset_id=CatalogAssetId("asset-alpha"),
        ),
        generation=3,
        page_size=1,
        cursor_valid_until=None,
    )
    field = field_page.items[0]

    assert asset.database_name == "schemabridge"
    assert asset.schema_name == "crm"
    assert field.native_type == "varchar(12)"
    assert field.normalized_type is PhysicalValueType.STRING
