from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypedDict, cast

import pytest

from schemabridge.adapters.catalog.cursor import SignedInventoryCursorCodec
from schemabridge.application.catalog_inventory import (
    CatalogUseCaseError,
    CatalogUseCaseErrorCode,
    ListCatalogAssets,
    ListCatalogConnections,
    ListCatalogFields,
    RegisterCatalogConnection,
)
from schemabridge.application.ports.catalog_inventory import (
    CatalogConnectionStorePort,
    CatalogInventoryReadPort,
)
from schemabridge.domain.catalog_inventory import (
    CatalogAssetFilter,
    CatalogAssetId,
    CatalogAssetLocator,
    CatalogAssetSummary,
    CatalogConnectionFilter,
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogConnectionRegistration,
    CatalogConnectionRegistrationResult,
    CatalogConnectionStatus,
    CatalogConnectionSummary,
    CatalogFieldFilter,
    CatalogFieldLocator,
    CatalogFieldSummary,
    InventoryCursorBinding,
    InventoryCursorResource,
    InventoryPageKey,
    InventoryPageRequest,
    InventoryStorePage,
    inventory_filter_fingerprint,
)
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)

NOW = datetime(2026, 7, 23, 21, 0, tzinfo=UTC)
CONNECTION_ID = CatalogConnectionId("warehouse-primary")
ASSET_ID = CatalogAssetId("asset-customers")
CURSOR_KEY = b"inventory-cursor-unit-test-key-with-byte-diversity-123"


class _RegistrationArguments(TypedDict):
    connection_id: CatalogConnectionId
    display_name: str
    kind: CatalogConnectionKind
    environment: str
    catalog_scope: str
    platform_instance: str | None
    confirmation: str
    idempotency_key: str


@dataclass(frozen=True)
class _Clock:
    current: datetime = NOW

    def now(self) -> datetime:
        return self.current


@dataclass
class _Connections:
    summary: CatalogConnectionSummary | None
    page: InventoryStorePage[CatalogConnectionSummary] | None = None
    protected_reads: int = 0
    registrations: list[CatalogConnectionRegistration] | None = None

    def load_public(
        self,
        workspace_id: str,
        connection_id: CatalogConnectionId,
    ) -> CatalogConnectionSummary | None:
        self.protected_reads += 1
        assert connection_id == CONNECTION_ID
        if self.summary is not None:
            assert workspace_id == self.summary.workspace_id
        return self.summary

    def list_connections(
        self,
        workspace_id: str,
        *,
        filters: CatalogConnectionFilter,
        page_size: int,
        after: InventoryPageKey | None,
    ) -> InventoryStorePage[CatalogConnectionSummary]:
        del workspace_id, filters, page_size, after
        self.protected_reads += 1
        assert self.page is not None
        return self.page

    def register(
        self,
        command: CatalogConnectionRegistration,
    ) -> CatalogConnectionRegistrationResult:
        if self.registrations is None:
            self.registrations = []
        self.registrations.append(command)
        assert self.summary is not None
        return CatalogConnectionRegistrationResult(connection=self.summary)


@dataclass
class _Inventory:
    assets_page: InventoryStorePage[CatalogAssetSummary]
    fields_page: InventoryStorePage[CatalogFieldSummary]
    calls: list[tuple[str, InventoryPageKey | None, int | None]] | None = None

    def list_assets(
        self,
        workspace_id: str,
        connection_id: CatalogConnectionId,
        *,
        filters: CatalogAssetFilter,
        page_size: int,
        after: InventoryPageKey | None,
        generation: int | None,
    ) -> InventoryStorePage[CatalogAssetSummary]:
        del workspace_id, connection_id, filters, page_size
        if self.calls is None:
            self.calls = []
        self.calls.append(("assets", after, generation))
        return self.assets_page

    def list_fields(
        self,
        asset: CatalogAssetLocator,
        *,
        filters: CatalogFieldFilter,
        page_size: int,
        after: InventoryPageKey | None,
        generation: int | None,
    ) -> InventoryStorePage[CatalogFieldSummary]:
        del asset, filters, page_size
        if self.calls is None:
            self.calls = []
        self.calls.append(("fields", after, generation))
        return self.fields_page


def _principal(
    *,
    workspace: str = "workspace-a",
    roles: frozenset[IdentityRole] = frozenset({IdentityRole.ANALYST}),
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        actor_id="actor-one",
        workspace_id=workspace,
        roles=roles,
        authentication_method=AuthenticationMethod.LOCAL_DEMO,
        authenticated_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=30),
    )


def _connection(*, generation: int = 7, stale: bool = False) -> CatalogConnectionSummary:
    return CatalogConnectionSummary(
        workspace_id="workspace-a",
        connection_id=CONNECTION_ID,
        display_name="Primary warehouse",
        kind=CatalogConnectionKind.DATAHUB_GRAPHQL,
        environment="PROD",
        catalog_scope="schemabridge",
        status=CatalogConnectionStatus.ENABLED,
        active_generation=generation,
        asset_count=5_434,
        field_count=32_604,
        last_completed_at=NOW - timedelta(minutes=5),
        stale=stale,
    )


def _asset(asset_id: str, qualified_name: str, *, generation: int = 7) -> CatalogAssetSummary:
    return CatalogAssetSummary(
        locator=CatalogAssetLocator(
            workspace_id="workspace-a",
            connection_id=CONNECTION_ID,
            asset_id=CatalogAssetId(asset_id),
        ),
        generation=generation,
        qualified_name=qualified_name,
        display_name=qualified_name.rpartition(".")[2],
        platform="postgres",
        environment="PROD",
        schema_name="crm",
        field_count=6,
        metadata_fingerprint="a" * 64,
        observed_at=NOW - timedelta(minutes=5),
    )


def _field(*, generation: int = 7) -> CatalogFieldSummary:
    return CatalogFieldSummary(
        locator=CatalogFieldLocator(
            asset=CatalogAssetLocator(
                workspace_id="workspace-a",
                connection_id=CONNECTION_ID,
                asset_id=ASSET_ID,
            ),
            field_path=("customer_id",),
        ),
        generation=generation,
        native_type="varchar",
        description="Stable governed customer identifier",
        nullable=False,
        is_part_of_key=True,
        metadata_fingerprint="b" * 64,
        observed_at=NOW - timedelta(minutes=5),
    )


def _inventory() -> _Inventory:
    assets = (
        _asset("asset-accounts", "bank.accounts"),
        _asset("asset-customers", "crm.customers"),
    )
    return _Inventory(
        assets_page=InventoryStorePage(
            items=assets,
            resource=InventoryCursorResource.ASSETS,
            generation=7,
            page_size=2,
            rows_read=3,
            has_more=True,
            last_key=InventoryPageKey(
                sort_value="crm.customers",
                stable_id="asset-customers",
            ),
        ),
        fields_page=InventoryStorePage(
            items=(_field(),),
            resource=InventoryCursorResource.FIELDS,
            generation=7,
            page_size=20,
            rows_read=1,
            has_more=False,
            last_key=InventoryPageKey(
                sort_value="customer_id",
                stable_id="customer_id",
            ),
        ),
    )


def _asset_service(
    connections: _Connections,
    inventory: _Inventory,
    *,
    at: datetime = NOW,
) -> ListCatalogAssets:
    return ListCatalogAssets(
        connections=cast(CatalogConnectionStorePort, connections),
        inventory=cast(CatalogInventoryReadPort, inventory),
        cursors=SignedInventoryCursorCodec(signing_key=CURSOR_KEY),
        clock=_Clock(at),
    )


def test_asset_page_exposes_generation_and_signed_continuation() -> None:
    connections = _Connections(_connection())
    inventory = _inventory()

    result = _asset_service(connections, inventory).execute(
        _principal(),
        CONNECTION_ID,
        filters=CatalogAssetFilter(),
        page=InventoryPageRequest(size=2),
    )

    assert result.generation == 7
    assert len(result.items) == 2
    assert result.next_cursor is not None
    assert result.stale is False
    assert connections.protected_reads == 1
    assert inventory.calls == [("assets", None, None)]


def test_asset_continuation_requires_declared_generation_and_authenticates_before_read() -> None:
    connections = _Connections(_connection())
    inventory = _inventory()
    service = _asset_service(connections, inventory)
    first = service.execute(
        _principal(),
        CONNECTION_ID,
        filters=CatalogAssetFilter(),
        page=InventoryPageRequest(size=2),
    )
    assert first.next_cursor is not None
    connections.protected_reads = 0
    inventory.calls = []

    with pytest.raises(CatalogUseCaseError) as missing_generation:
        service.execute(
            _principal(),
            CONNECTION_ID,
            filters=CatalogAssetFilter(),
            page=InventoryPageRequest(size=2, cursor=first.next_cursor),
        )
    assert missing_generation.value.code is CatalogUseCaseErrorCode.CURSOR_UNAVAILABLE
    assert connections.protected_reads == 0
    assert inventory.calls == []

    with pytest.raises(CatalogUseCaseError) as wrong_tenant:
        service.execute(
            _principal(workspace="workspace-b"),
            CONNECTION_ID,
            filters=CatalogAssetFilter(),
            page=InventoryPageRequest(size=2, cursor=first.next_cursor),
            generation=7,
        )
    assert wrong_tenant.value.code is CatalogUseCaseErrorCode.CURSOR_UNAVAILABLE
    assert connections.protected_reads == 0
    assert inventory.calls == []


def test_retained_generation_can_continue_and_is_labeled_stale() -> None:
    connections = _Connections(_connection(generation=8))
    inventory = _inventory()
    inventory.assets_page = inventory.assets_page.model_copy(
        update={"cursor_valid_until": NOW + timedelta(minutes=16)}
    )
    service = _asset_service(
        connections,
        inventory,
        at=NOW + timedelta(minutes=15) - timedelta(microseconds=1),
    )
    seed_connections = _Connections(_connection(generation=7))
    first = _asset_service(seed_connections, inventory).execute(
        _principal(),
        CONNECTION_ID,
        filters=CatalogAssetFilter(),
        page=InventoryPageRequest(size=2),
    )
    assert first.next_cursor is not None
    inventory.calls = []

    result = service.execute(
        _principal(),
        CONNECTION_ID,
        filters=CatalogAssetFilter(),
        page=InventoryPageRequest(size=2, cursor=first.next_cursor),
        generation=7,
    )

    assert result.generation == 7
    assert result.stale is True
    assert result.next_cursor is not None
    continued_position = SignedInventoryCursorCodec(signing_key=CURSOR_KEY).decode(
        cursor=result.next_cursor,
        expected_binding=InventoryCursorBinding(
            workspace_id="workspace-a",
            resource=InventoryCursorResource.ASSETS,
            connection_id=CONNECTION_ID,
            generation=7,
            filter_fingerprint=CatalogAssetFilter().fingerprint,
            sort_fingerprint=inventory_filter_fingerprint(
                {"keys": ["qualified_name", "asset_id"], "version": 1}
            ),
        ),
        at=NOW + timedelta(minutes=15) - timedelta(microseconds=1),
    )
    assert continued_position.issued_at == NOW
    assert continued_position.expires_at == NOW + timedelta(minutes=15)
    assert inventory.calls == [
        (
            "assets",
            InventoryPageKey(
                sort_value="crm.customers",
                stable_id="asset-customers",
            ),
            7,
        )
    ]


def test_inactive_generation_cannot_start_a_cursor_beyond_its_retention() -> None:
    connections = _Connections(_connection(generation=8))
    inventory = _inventory()
    inventory.assets_page = inventory.assets_page.model_copy(
        update={"cursor_valid_until": NOW + timedelta(minutes=15)}
    )
    service = _asset_service(
        connections,
        inventory,
        at=NOW + timedelta(minutes=1),
    )

    with pytest.raises(CatalogUseCaseError) as captured:
        service.execute(
            _principal(),
            CONNECTION_ID,
            filters=CatalogAssetFilter(),
            page=InventoryPageRequest(size=2),
            generation=7,
        )

    assert captured.value.code is CatalogUseCaseErrorCode.CURSOR_UNAVAILABLE
    assert str(captured.value) == "inventory cursor is unavailable"


def test_inactive_generation_without_a_retention_window_fails_closed() -> None:
    connections = _Connections(_connection(generation=8))
    inventory = _inventory()

    with pytest.raises(CatalogUseCaseError) as captured:
        _asset_service(connections, inventory).execute(
            _principal(),
            CONNECTION_ID,
            filters=CatalogAssetFilter(),
            page=InventoryPageRequest(size=2),
            generation=7,
        )

    assert captured.value.code is CatalogUseCaseErrorCode.SERVICE_UNAVAILABLE
    assert str(captured.value) == "catalog service is unavailable"


def test_connection_cursor_is_tenant_bound_before_store_read() -> None:
    summary = _connection()
    connections = _Connections(
        summary,
        page=InventoryStorePage(
            items=(summary,),
            resource=InventoryCursorResource.CONNECTIONS,
            page_size=1,
            rows_read=2,
            has_more=True,
            last_key=InventoryPageKey(
                sort_value=summary.display_name,
                stable_id=summary.connection_id.root,
            ),
        ),
    )
    service = ListCatalogConnections(
        store=cast(CatalogConnectionStorePort, connections),
        cursors=SignedInventoryCursorCodec(signing_key=CURSOR_KEY),
        clock=_Clock(),
    )
    first = service.execute(
        _principal(),
        filters=CatalogConnectionFilter(),
        page=InventoryPageRequest(size=1),
    )
    assert first.next_cursor is not None
    connections.protected_reads = 0

    with pytest.raises(CatalogUseCaseError) as captured:
        service.execute(
            _principal(workspace="workspace-b"),
            filters=CatalogConnectionFilter(),
            page=InventoryPageRequest(size=1, cursor=first.next_cursor),
        )

    assert captured.value.code is CatalogUseCaseErrorCode.CURSOR_UNAVAILABLE
    assert connections.protected_reads == 0


def test_field_listing_rejects_cross_tenant_locator_without_store_read() -> None:
    connections = _Connections(_connection())
    inventory = _inventory()
    service = ListCatalogFields(
        connections=cast(CatalogConnectionStorePort, connections),
        inventory=cast(CatalogInventoryReadPort, inventory),
        cursors=SignedInventoryCursorCodec(signing_key=CURSOR_KEY),
        clock=_Clock(),
    )
    foreign = CatalogAssetLocator(
        workspace_id="workspace-b",
        connection_id=CONNECTION_ID,
        asset_id=ASSET_ID,
    )

    with pytest.raises(CatalogUseCaseError) as captured:
        service.execute(
            _principal(),
            foreign,
            filters=CatalogFieldFilter(),
            page=InventoryPageRequest(),
        )

    assert captured.value.code is CatalogUseCaseErrorCode.UNAVAILABLE
    assert connections.protected_reads == 0
    assert inventory.calls is None


def test_only_current_platform_admin_can_register_connection() -> None:
    connections = _Connections(_connection())
    service = RegisterCatalogConnection(
        store=cast(CatalogConnectionStorePort, connections),
        clock=_Clock(),
    )
    arguments: _RegistrationArguments = {
        "connection_id": CONNECTION_ID,
        "display_name": "Primary warehouse",
        "kind": CatalogConnectionKind.DATAHUB_GRAPHQL,
        "environment": "PROD",
        "catalog_scope": "schemabridge",
        "platform_instance": "warehouse-primary",
        "confirmation": "REGISTER CATALOG CONNECTION",
        "idempotency_key": "catalog-registration-key-0001",
    }

    with pytest.raises(CatalogUseCaseError) as denied:
        service.execute(_principal(), **arguments)
    assert denied.value.code is CatalogUseCaseErrorCode.UNAVAILABLE
    assert connections.registrations is None

    result = service.execute(
        _principal(roles=frozenset({IdentityRole.PLATFORM_ADMIN})),
        **arguments,
    )
    assert result.connection.connection_id == CONNECTION_ID
    assert connections.registrations is not None
    command = connections.registrations[0]
    assert command.workspace_id == "workspace-a"
    assert command.requested_by == "actor-one"
    assert command.platform_instance == "warehouse-primary"
    assert command.idempotency_digest != arguments["idempotency_key"]
    assert len(command.idempotency_digest) == 64


def test_registration_rejects_wrong_confirmation_before_store_write() -> None:
    connections = _Connections(_connection())
    service = RegisterCatalogConnection(
        store=cast(CatalogConnectionStorePort, connections),
        clock=_Clock(),
    )

    with pytest.raises(CatalogUseCaseError) as captured:
        service.execute(
            _principal(roles=frozenset({IdentityRole.PLATFORM_ADMIN})),
            connection_id=CONNECTION_ID,
            display_name="Primary warehouse",
            kind=CatalogConnectionKind.DATAHUB_GRAPHQL,
            environment="PROD",
            catalog_scope="schemabridge",
            confirmation="yes",
            idempotency_key="catalog-registration-key-0001",
        )

    assert captured.value.code is CatalogUseCaseErrorCode.INVALID_REQUEST
    assert connections.registrations is None
