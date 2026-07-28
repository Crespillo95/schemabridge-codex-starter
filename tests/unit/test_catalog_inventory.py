"""Unit tests for tenant-scoped dynamic inventory values and bounded pages."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from schemabridge.application.ports.catalog_inventory import (
    ManagedCatalogConnectorRoute,
)
from schemabridge.domain.catalog_inventory import (
    MAX_INVENTORY_CURSOR_BYTES,
    CatalogAssetFilter,
    CatalogAssetId,
    CatalogAssetLocator,
    CatalogAssetSummary,
    CatalogConnectionConfirmation,
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogConnectionRegistration,
    CatalogConnectionRoute,
    CatalogConnectionStatus,
    CatalogConnectionSummary,
    CatalogFieldFilter,
    CatalogFieldLocator,
    CatalogFieldSummary,
    CatalogRefreshId,
    CatalogRefreshLease,
    CatalogSourceAsset,
    CatalogSourceField,
    InventoryCursorBinding,
    InventoryCursorResource,
    InventoryPage,
    InventoryPageKey,
    InventoryPageRequest,
    InventoryStorePage,
)
from schemabridge.domain.semantic_registry import PhysicalValueType

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64


def _asset_locator(
    *,
    workspace_id: str = "workspace_alpha",
    connection_id: str = "connection_primary",
    asset_id: str = "urn:li:dataset:(urn:li:dataPlatform:postgres,crm.customers,PROD)",
) -> CatalogAssetLocator:
    return CatalogAssetLocator(
        workspace_id=workspace_id,
        connection_id=CatalogConnectionId(connection_id),
        asset_id=CatalogAssetId(asset_id),
    )


def _asset(index: int = 1) -> CatalogAssetSummary:
    return CatalogAssetSummary(
        locator=_asset_locator(
            asset_id=f"urn:li:dataset:(urn:li:dataPlatform:postgres,scale.table_{index:05d},PROD)"
        ),
        generation=3,
        qualified_name=f"scale.table_{index:05d}",
        display_name=f"table_{index:05d}",
        platform="postgres",
        environment="PROD",
        database_name="schemabridge",
        schema_name="scale",
        description="Synthetic scale metadata.",
        field_count=4,
        metadata_fingerprint=SHA_A,
        observed_at=NOW,
    )


def test_inventory_identity_is_scoped_beyond_physical_schema_table() -> None:
    first = _asset_locator(connection_id="connection_one")
    second = _asset_locator(connection_id="connection_two")
    another_tenant = _asset_locator(
        workspace_id="workspace_beta",
        connection_id="connection_one",
    )
    first_field = CatalogFieldLocator(asset=first, field_path=("contract_id",))
    second_field = CatalogFieldLocator(asset=second, field_path=("contract_id",))

    assert first != second
    assert first != another_tenant
    assert first_field != second_field
    assert first.asset_id.root.endswith("crm.customers,PROD)")
    assert "PhysicalDatasetRef" not in type(first).__name__


@pytest.mark.parametrize(
    ("value", "expected_error"),
    [
        ("", "catalog connection id"),
        ("1connection", "catalog connection id"),
        ("Connection-UPPER", "catalog connection id"),
        ("postgresql://user:password@host/db", "catalog connection id"),
    ],
)
def test_connection_id_rejects_blank_executable_or_credential_shaped_values(
    value: str,
    expected_error: str,
) -> None:
    with pytest.raises(ValidationError, match=expected_error):
        CatalogConnectionId(value)


def test_private_route_binding_has_no_public_registration_representation() -> None:
    registration = CatalogConnectionRegistration(
        workspace_id="workspace_alpha",
        connection_id=CatalogConnectionId("connection_primary"),
        display_name="Primary DataHub catalog",
        kind=CatalogConnectionKind.DATAHUB_GRAPHQL,
        environment="PROD",
        catalog_scope="synthetic-demo",
        platform_instance=(
            "urn:li:dataPlatformInstance:(urn:li:dataPlatform:postgres,warehouse-alpha)"
        ),
        requested_by="actor_platform_admin",
        requested_at=NOW,
        idempotency_digest=SHA_A,
    )
    route = CatalogConnectionRoute(
        workspace_id=registration.workspace_id,
        connection_id=registration.connection_id,
        kind=registration.kind,
        environment=registration.environment,
        catalog_scope=registration.catalog_scope,
        platform_instance=registration.platform_instance,
        status=CatalogConnectionStatus.ENABLED,
    )
    managed = ManagedCatalogConnectorRoute(
        route=route,
        credential_binding_ref="binding_datahub_alpha",
    )
    summary = CatalogConnectionSummary(
        workspace_id=registration.workspace_id,
        connection_id=registration.connection_id,
        display_name=registration.display_name,
        kind=registration.kind,
        environment=registration.environment,
        catalog_scope=registration.catalog_scope,
        platform_instance=registration.platform_instance,
        status=CatalogConnectionStatus.ENABLED,
    )

    assert managed.credential_binding_ref == "binding_datahub_alpha"
    assert summary.platform_instance == registration.platform_instance
    assert "credential_binding_ref" not in registration.model_dump()
    assert "credential_binding_ref" not in summary.model_dump()
    assert "binding_datahub_alpha" not in summary.model_dump_json()
    assert "binding_datahub_alpha" not in repr(route)
    assert "binding_datahub_alpha" not in repr(managed)
    assert "binding_datahub_alpha" not in route.model_dump_json()
    assert "credential_binding_ref" not in route.model_dump()
    assert not hasattr(route, "credential_binding_ref")

    payload = registration.model_dump(mode="python")
    payload["confirmation"] = CatalogConnectionConfirmation.DISABLE
    with pytest.raises(ValidationError, match="does not match"):
        CatalogConnectionRegistration.model_validate(payload)


def test_persisted_identifiers_and_route_values_match_postgres_contract() -> None:
    assert CatalogConnectionId("connection_primary").root == "connection_primary"
    assert CatalogRefreshId("refresh_primary").root == "refresh_primary"
    lease = CatalogRefreshLease(
        indexer_id="catalog:indexer.prod",
        capability_digest=SHA_A,
        fencing_token=1,
        leased_at=NOW,
        expires_at=NOW.replace(minute=1),
    )
    assert lease.indexer_id == "catalog:indexer.prod"

    for model, value in (
        (CatalogConnectionId, "1connection"),
        (CatalogRefreshId, "1refresh"),
    ):
        with pytest.raises(ValidationError, match="durable storage"):
            model(value)
    with pytest.raises(ValidationError, match="durable storage"):
        CatalogRefreshLease(
            indexer_id="1indexer",
            capability_digest=SHA_A,
            fencing_token=1,
            leased_at=NOW,
            expires_at=NOW.replace(minute=1),
        )


def test_connection_metadata_enforces_postgres_utf8_and_environment_boundaries() -> None:
    valid = CatalogConnectionRegistration(
        workspace_id="workspace_alpha",
        connection_id=CatalogConnectionId("connection_unicode"),
        display_name="😀" * 100,
        kind=CatalogConnectionKind.DATAHUB_GRAPHQL,
        environment="PROD.eu-1",
        catalog_scope="界" * 133 + "a",
        platform_instance="warehouse-😀",
        requested_by="actor_platform_admin",
        requested_at=NOW,
        idempotency_digest=SHA_A,
    )
    assert len(valid.display_name.encode("utf-8")) == 400
    assert len(valid.catalog_scope.encode("utf-8")) == 400

    base = valid.model_dump(mode="python")
    for field, value in (
        ("display_name", "😀" * 101),
        ("catalog_scope", "😀" * 101),
        ("environment", "A" * 81),
        ("environment", "PROD_Ñ"),
        ("platform_instance", "😀" * 101),
    ):
        with pytest.raises(ValidationError):
            CatalogConnectionRegistration.model_validate({**base, field: value})


def test_public_registration_rejects_even_an_opaque_private_binding() -> None:
    with pytest.raises(ValidationError):
        CatalogConnectionRegistration.model_validate(
            {
                "workspace_id": "workspace_alpha",
                "connection_id": CatalogConnectionId("connection_primary"),
                "display_name": "Primary",
                "kind": CatalogConnectionKind.DATAHUB_GRAPHQL,
                "environment": "PROD",
                "catalog_scope": "synthetic-demo",
                "credential_binding_ref": "binding_datahub_alpha",
                "requested_by": "actor_platform_admin",
                "requested_at": NOW,
                "idempotency_digest": SHA_A,
            }
        )


def test_secret_and_source_value_fields_have_no_domain_representation() -> None:
    payload = {
        "workspace_id": "workspace_alpha",
        "connection_id": CatalogConnectionId("connection_primary"),
        "display_name": "Primary",
        "kind": CatalogConnectionKind.DATAHUB_GRAPHQL,
        "environment": "PROD",
        "catalog_scope": "synthetic-demo",
        "platform_instance": None,
        "credential_binding_ref": "binding_datahub_alpha",
        "requested_by": "actor_platform_admin",
        "requested_at": NOW,
        "idempotency_digest": SHA_A,
        "dsn": "postgresql://sensitive",
        "token": "sensitive",
        "sample_values": ("private",),
    }
    with pytest.raises(ValidationError):
        CatalogConnectionRegistration.model_validate(payload)


def test_platform_instance_is_never_inferred_from_scope_or_display_name() -> None:
    registration = CatalogConnectionRegistration(
        workspace_id="workspace_alpha",
        connection_id=CatalogConnectionId("connection_primary"),
        display_name="Warehouse alpha",
        kind=CatalogConnectionKind.DATAHUB_GRAPHQL,
        environment="PROD",
        catalog_scope="warehouse_alpha",
        requested_by="actor_platform_admin",
        requested_at=NOW,
        idempotency_digest=SHA_A,
    )

    assert registration.platform_instance is None


def test_page_limits_are_transport_bounds_not_catalog_capacity() -> None:
    first_fifty = tuple(_asset(index) for index in range(50))
    page = InventoryStorePage[CatalogAssetSummary](
        items=first_fifty,
        resource=InventoryCursorResource.ASSETS,
        generation=3,
        page_size=50,
        rows_read=51,
        has_more=True,
        last_key=InventoryPageKey(
            sort_value="table_00049",
            stable_id=first_fifty[-1].locator.asset_id.root,
        ),
    )

    assert len(page.items) == 50
    assert page.rows_read == 51
    assert page.has_more is True

    with pytest.raises(ValidationError):
        InventoryStorePage[int](
            items=tuple(range(51)),
            resource=InventoryCursorResource.ASSETS,
            generation=3,
            page_size=50,
            rows_read=51,
            has_more=False,
            last_key=InventoryPageKey(sort_value="50", stable_id="50"),
        )


@pytest.mark.parametrize("size", [1, 17, 50])
def test_supported_page_sizes_are_independent_from_inventory_total(size: int) -> None:
    request = InventoryPageRequest(size=size)
    assert request.size == size


@pytest.mark.parametrize("size", [0, 51, 5_434])
def test_request_cannot_turn_inventory_total_into_page_size(size: int) -> None:
    with pytest.raises(ValidationError):
        InventoryPageRequest(size=size)


def test_store_page_requires_limit_plus_one_contract_and_resource_generation() -> None:
    item = _asset()
    key = InventoryPageKey(sort_value="table_00001", stable_id=item.locator.asset_id.root)

    with pytest.raises(ValidationError, match="continuation"):
        InventoryStorePage[CatalogAssetSummary](
            items=(item,),
            resource=InventoryCursorResource.ASSETS,
            generation=3,
            page_size=1,
            rows_read=1,
            has_more=True,
            last_key=key,
        )
    with pytest.raises(ValidationError, match="require an active generation"):
        InventoryStorePage[CatalogAssetSummary](
            items=(item,),
            resource=InventoryCursorResource.ASSETS,
            page_size=1,
            rows_read=1,
            has_more=False,
            last_key=key,
        )
    with pytest.raises(ValidationError, match="cannot bind"):
        InventoryStorePage[int](
            items=(1,),
            resource=InventoryCursorResource.CONNECTIONS,
            generation=3,
            page_size=1,
            rows_read=1,
            has_more=False,
            last_key=InventoryPageKey(sort_value="one", stable_id="one"),
        )

    with pytest.raises(ValidationError, match="requested size"):
        InventoryStorePage[CatalogAssetSummary](
            items=(item, _asset(2)),
            resource=InventoryCursorResource.ASSETS,
            generation=3,
            page_size=1,
            rows_read=2,
            has_more=True,
            last_key=key,
        )
    with pytest.raises(ValidationError, match="empty"):
        InventoryStorePage[CatalogAssetSummary](
            items=(),
            resource=InventoryCursorResource.ASSETS,
            generation=3,
            page_size=1,
            rows_read=1,
            has_more=True,
        )


def test_store_page_generation_cursor_deadline_is_typed_and_not_public() -> None:
    item = _asset()
    deadline = NOW.replace(hour=13)
    page = InventoryStorePage[CatalogAssetSummary](
        items=(item,),
        resource=InventoryCursorResource.ASSETS,
        generation=3,
        page_size=1,
        rows_read=1,
        has_more=False,
        last_key=InventoryPageKey(
            sort_value=item.qualified_name,
            stable_id=item.locator.asset_id.root,
        ),
        cursor_valid_until=deadline,
    )

    assert page.cursor_valid_until == deadline
    with pytest.raises(ValidationError, match="timezone"):
        InventoryStorePage[CatalogAssetSummary].model_validate(
            {
                **page.model_dump(mode="python"),
                "cursor_valid_until": deadline.replace(tzinfo=None),
            }
        )
    with pytest.raises(ValidationError, match="connection pages"):
        InventoryStorePage[int](
            items=(1,),
            resource=InventoryCursorResource.CONNECTIONS,
            page_size=1,
            rows_read=1,
            has_more=False,
            last_key=InventoryPageKey(sort_value="one", stable_id="one"),
            cursor_valid_until=deadline,
        )


def test_public_page_is_bounded_timed_and_does_not_expose_rows_read() -> None:
    page = InventoryPage[CatalogAssetSummary](
        items=(_asset(),),
        resource=InventoryCursorResource.ASSETS,
        generation=3,
        next_cursor="opaque.payload",
        as_of=NOW,
    )
    encoded = json.loads(page.model_dump_json())

    assert encoded["generation"] == 3
    assert "rows_read" not in encoded
    with pytest.raises(ValidationError, match="timezone"):
        InventoryPage[CatalogAssetSummary](
            items=(),
            resource=InventoryCursorResource.ASSETS,
            generation=3,
            as_of=NOW.replace(tzinfo=None),
        )


def test_cursor_request_rejects_blank_control_and_oversized_values() -> None:
    for value in ("", " cursor", "cursor\nnext", "x" * (MAX_INVENTORY_CURSOR_BYTES + 1)):
        with pytest.raises(ValidationError, match="cursor"):
            InventoryPageRequest(cursor=value)

    with pytest.raises(ValidationError, match="cursor"):
        InventoryPage[CatalogAssetSummary](
            items=(),
            resource=InventoryCursorResource.ASSETS,
            generation=3,
            next_cursor="\n",
            as_of=NOW,
        )


def test_cursor_binding_shape_is_exact_for_each_resource() -> None:
    connection = CatalogConnectionId("connection_primary")
    asset = CatalogAssetId("asset-primary")
    connection_binding = InventoryCursorBinding(
        workspace_id="workspace_alpha",
        resource=InventoryCursorResource.CONNECTIONS,
        filter_fingerprint=SHA_A,
        sort_fingerprint=SHA_B,
    )
    asset_binding = InventoryCursorBinding(
        workspace_id="workspace_alpha",
        resource=InventoryCursorResource.ASSETS,
        connection_id=connection,
        generation=3,
        filter_fingerprint=SHA_A,
        sort_fingerprint=SHA_B,
    )
    field_binding = InventoryCursorBinding(
        workspace_id="workspace_alpha",
        resource=InventoryCursorResource.FIELDS,
        connection_id=connection,
        asset_id=asset,
        generation=3,
        filter_fingerprint=SHA_A,
        sort_fingerprint=SHA_B,
    )

    assert connection_binding.generation is None
    assert asset_binding.generation == 3
    assert field_binding.asset_id == asset

    payload = asset_binding.model_dump(mode="python")
    payload["asset_id"] = asset
    with pytest.raises(ValidationError, match="asset cursor"):
        InventoryCursorBinding.model_validate(payload)


def test_filters_normalize_equivalent_requests_to_the_same_fingerprint() -> None:
    first = CatalogAssetFilter(
        query="  Contrato   Principal ",
        platform=" POSTGRES ",
        schema_name=" CRM ",
    )
    second = CatalogAssetFilter(
        query="contrato principal",
        platform="postgres",
        schema_name="crm",
    )
    unrelated = CatalogFieldFilter(query="contrato principal", native_type="text")

    assert first == second
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != unrelated.fingerprint


def test_field_summary_contains_metadata_but_no_source_values() -> None:
    summary = CatalogFieldSummary(
        locator=CatalogFieldLocator(
            asset=_asset_locator(),
            field_path=("contract_id",),
        ),
        generation=3,
        native_type="VARCHAR(12)",
        normalized_type=PhysicalValueType.STRING,
        description="Synthetic contract identifier.",
        nullable=False,
        is_part_of_key=True,
        tags=("identifier",),
        glossary_terms=("Contract",),
        metadata_fingerprint=SHA_A,
        observed_at=NOW,
    )
    encoded = summary.model_dump_json()

    assert "Synthetic contract identifier" in encoded
    assert summary.normalized_type is PhysicalValueType.STRING
    assert "sample" not in encoded
    assert "value" not in encoded


def test_source_metadata_matches_postgres_utf8_column_and_array_boundaries() -> None:
    exact_terms = tuple(sorted("😀" * 199 + chr(0x1F680 + index) for index in range(25)))
    assert sum(len(term.encode("utf-8")) for term in exact_terms) == 20_000
    field = CatalogSourceField(
        field_path=("contract_id",),
        native_type="😀" * 100,
        description="😀" * 4_000,
        tags=exact_terms,
        metadata_fingerprint=SHA_A,
    )
    asset = CatalogSourceAsset(
        asset_id=CatalogAssetId("asset-unicode-boundary"),
        qualified_name=("界" * 100) + ".table",
        display_name="😀" * 100,
        platform="postgres",
        environment="PROD",
        database_name="schemabridge",
        schema_name="界" * 133 + "a",
        description="😀" * 4_000,
        fields=(field,),
        metadata_fingerprint=SHA_B,
    )
    assert len(asset.display_name.encode("utf-8")) == 400
    assert asset.database_name == "schemabridge"
    assert len(asset.schema_name.encode("utf-8")) == 400  # type: ignore[union-attr]
    assert len(field.native_type.encode("utf-8")) == 400  # type: ignore[union-attr]

    invalid_terms = tuple(sorted((*exact_terms, "zz")))
    with pytest.raises(ValidationError, match="storage bytes"):
        CatalogSourceField(
            field_path=("contract_id",),
            tags=invalid_terms,
            metadata_fingerprint=SHA_A,
        )
    for field_name, value in (
        ("native_type", "😀" * 101),
        ("tags", invalid_terms),
    ):
        with pytest.raises(ValidationError):
            CatalogSourceField.model_validate(
                {
                    **field.model_dump(mode="python"),
                    field_name: value,
                }
            )
    for field_name, value in (
        ("display_name", "😀" * 101),
        ("schema_name", "😀" * 101),
    ):
        with pytest.raises(ValidationError):
            CatalogSourceAsset.model_validate(
                {
                    **asset.model_dump(mode="python"),
                    field_name: value,
                }
            )


def _source_asset(qualified_name: str) -> CatalogSourceAsset:
    return CatalogSourceAsset(
        asset_id=CatalogAssetId("asset-unicode"),
        qualified_name=qualified_name,
        display_name="unicode_table",
        platform="postgres",
        environment="PROD",
        schema_name="public",
        fields=(),
        metadata_fingerprint=SHA_A,
    )


def test_multibyte_qualified_names_fit_both_storage_and_sort_key_bytes() -> None:
    valid = ("界" * 37) + "." + ("表" * 100)
    assert len(valid.encode("utf-8")) == 412
    assert _source_asset(valid).qualified_name == valid

    oversized_sort_key = ("界" * 101) + "." + ("表" * 70)
    assert len(oversized_sort_key) < 500
    assert len(oversized_sort_key.encode("utf-8")) > 512
    with pytest.raises(ValidationError, match="qualified name"):
        _source_asset(oversized_sort_key)

    oversized_final_component = "s." + ("😀" * 101)
    assert len(oversized_final_component.encode("utf-8")) <= 512
    with pytest.raises(ValidationError, match="final component"):
        _source_asset(oversized_final_component)


def test_multibyte_field_paths_fit_array_and_final_sort_key_bytes() -> None:
    valid_final = "😀" * 100
    field = CatalogSourceField(
        field_path=("nested", valid_final),
        metadata_fingerprint=SHA_A,
    )
    assert len(field.field_path[-1].encode("utf-8")) == 400

    with pytest.raises(ValidationError, match="sort key"):
        CatalogSourceField(
            field_path=("nested", "😀" * 101),
            metadata_fingerprint=SHA_A,
        )

    oversized_joined_path = tuple("界" * 67 for _ in range(64))
    assert len(".".join(oversized_joined_path).encode("utf-8")) > 12_800
    with pytest.raises(ValidationError, match="field path"):
        CatalogSourceField(
            field_path=oversized_joined_path,
            metadata_fingerprint=SHA_A,
        )


def test_inventory_sort_key_enforces_utf8_bytes_and_rejects_surrogates() -> None:
    boundary = InventoryPageKey(sort_value="😀" * 128, stable_id="stable")
    assert len(boundary.sort_value.encode("utf-8")) == 512

    with pytest.raises(ValidationError, match="sort key"):
        InventoryPageKey(sort_value="😀" * 129, stable_id="stable")
    with pytest.raises(ValidationError, match="valid string"):
        InventoryPageKey(sort_value="\ud800", stable_id="stable")
