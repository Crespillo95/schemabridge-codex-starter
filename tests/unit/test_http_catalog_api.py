from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from schemabridge.application.authentication import AuthenticationBoundaryError
from schemabridge.application.catalog_inventory import (
    CatalogUseCaseError,
    CatalogUseCaseErrorCode,
    DisableCatalogConnection,
    InspectCatalogRefresh,
    ListCatalogAssets,
    ListCatalogConnections,
    ListCatalogFields,
    RegisterCatalogConnection,
    RequestCatalogRefresh,
)
from schemabridge.application.ports.catalog_inventory import (
    CatalogConnectionStorePort,
    CatalogInventoryReadPort,
    CatalogRefreshStorePort,
    InventoryCursorPort,
)
from schemabridge.domain.catalog_inventory import (
    CatalogAssetFilter,
    CatalogAssetId,
    CatalogAssetLocator,
    CatalogAssetSummary,
    CatalogConnectionDisable,
    CatalogConnectionFilter,
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogConnectionRegistration,
    CatalogConnectionRegistrationResult,
    CatalogConnectionRoute,
    CatalogConnectionStatus,
    CatalogConnectionSummary,
    CatalogFieldFilter,
    CatalogFieldLocator,
    CatalogFieldSummary,
    CatalogRefreshCommand,
    CatalogRefreshId,
    CatalogRefreshRequestResult,
    CatalogRefreshState,
    CatalogRefreshStatus,
    CatalogRefreshSummary,
    InventoryCursorBinding,
    InventoryCursorPosition,
    InventoryCursorResource,
    InventoryPageKey,
    InventoryStorePage,
)
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)
from schemabridge.domain.semantic_registry import PhysicalValueType
from schemabridge.entrypoints.http.app import (
    ApiHttpServices,
    CatalogHttpServices,
    create_http_app,
)

NOW = datetime(2026, 7, 23, 22, 0, tzinfo=UTC)
IDEMPOTENCY_KEY = "catalog-http-idempotency-key-001"
CONNECTION_ID = CatalogConnectionId("warehouse-primary")
ASSET_ID = CatalogAssetId("urn:li:dataset:customers")
REFRESH_ID = CatalogRefreshId("refresh-v1-0001")


class _Clock:
    def now(self) -> datetime:
        return NOW


class _Authenticator:
    def authenticate(self, bearer_token: str, now: datetime) -> AuthenticatedPrincipal:
        assert now == NOW
        identities = {
            "admin-token": ("workspace-a", frozenset({IdentityRole.PLATFORM_ADMIN})),
            "analyst-token": ("workspace-a", frozenset({IdentityRole.ANALYST})),
            "auditor-token": ("workspace-a", frozenset({IdentityRole.AUDITOR})),
            "tenant-b-token": ("workspace-b", frozenset({IdentityRole.ANALYST})),
        }
        identity = identities.get(bearer_token)
        if identity is None:
            raise AuthenticationBoundaryError("invalid_bearer_token")
        workspace_id, roles = identity
        return AuthenticatedPrincipal(
            actor_id=f"actor-{workspace_id}-{next(iter(roles)).value}",
            workspace_id=workspace_id,
            roles=roles,
            authentication_method=AuthenticationMethod.LOCAL_DEMO,
            authenticated_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=10),
        )


class _UnusedJobPort:
    def execute(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("execution-job service must not be called by catalog routes")


class _Readiness:
    def require_ready(self) -> None:
        return None


class _Cursor:
    def __init__(self) -> None:
        self.decodes: list[tuple[str, InventoryCursorBinding]] = []

    def encode(
        self,
        *,
        binding: InventoryCursorBinding,
        last_key: InventoryPageKey,
        issued_at: datetime,
        not_after: datetime | None = None,
    ) -> str:
        assert issued_at == NOW
        assert not_after is None
        assert last_key.stable_id
        return f"opaque-{binding.resource.value}-cursor"

    def decode(
        self,
        *,
        cursor: str,
        expected_binding: InventoryCursorBinding,
        at: datetime,
    ) -> InventoryCursorPosition:
        assert at == NOW
        self.decodes.append((cursor, expected_binding))
        return InventoryCursorPosition(
            last_key=InventoryPageKey(sort_value="last-item", stable_id="last-id"),
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=15),
        )


@dataclass
class _ConnectionStore:
    summary: CatalogConnectionSummary
    registration: CatalogConnectionRegistration | None = None
    disable_command: CatalogConnectionDisable | None = None
    list_calls: list[tuple[str, CatalogConnectionFilter, int, InventoryPageKey | None]] | None = (
        None
    )

    def register(
        self,
        command: CatalogConnectionRegistration,
    ) -> CatalogConnectionRegistrationResult:
        if self.registration is None:
            self.registration = command
            replayed = False
        elif self.registration.model_dump(mode="json") == command.model_dump(mode="json"):
            replayed = True
        else:
            raise AssertionError("registration fixture received conflicting content")
        self.summary = self.summary.model_copy(
            update={"platform_instance": command.platform_instance},
        )
        return CatalogConnectionRegistrationResult(
            connection=self.summary,
            replayed=replayed,
        )

    def disable(self, command: CatalogConnectionDisable) -> CatalogConnectionSummary:
        self.disable_command = command
        self.summary = self.summary.model_copy(
            update={"status": CatalogConnectionStatus.DISABLED},
        )
        return self.summary

    def load_public(
        self,
        workspace_id: str,
        connection_id: CatalogConnectionId,
    ) -> CatalogConnectionSummary | None:
        if workspace_id != self.summary.workspace_id or connection_id != self.summary.connection_id:
            return None
        return self.summary

    def load_route(
        self,
        workspace_id: str,
        connection_id: CatalogConnectionId,
    ) -> CatalogConnectionRoute | None:
        del workspace_id, connection_id
        raise AssertionError("the HTTP API must never load indexer routing metadata")

    def list_connections(
        self,
        workspace_id: str,
        *,
        filters: CatalogConnectionFilter,
        page_size: int,
        after: InventoryPageKey | None,
    ) -> InventoryStorePage[CatalogConnectionSummary]:
        if self.list_calls is None:
            self.list_calls = []
        self.list_calls.append((workspace_id, filters, page_size, after))
        if workspace_id != self.summary.workspace_id:
            items: tuple[CatalogConnectionSummary, ...] = ()
        else:
            items = (self.summary,)
        return InventoryStorePage(
            items=items,
            resource=InventoryCursorResource.CONNECTIONS,
            page_size=page_size,
            rows_read=len(items) + (1 if items else 0),
            has_more=bool(items),
            last_key=(
                InventoryPageKey(
                    sort_value=self.summary.display_name,
                    stable_id=self.summary.connection_id.root,
                )
                if items
                else None
            ),
        )


@dataclass
class _Inventory:
    asset_calls: (
        list[
            tuple[
                str,
                CatalogConnectionId,
                CatalogAssetFilter,
                int,
                InventoryPageKey | None,
                int | None,
            ]
        ]
        | None
    ) = None
    field_calls: (
        list[
            tuple[
                CatalogAssetLocator,
                CatalogFieldFilter,
                int,
                InventoryPageKey | None,
                int | None,
            ]
        ]
        | None
    ) = None

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
        if self.asset_calls is None:
            self.asset_calls = []
        self.asset_calls.append(
            (workspace_id, connection_id, filters, page_size, after, generation)
        )
        item = _asset()
        return InventoryStorePage(
            items=(item,),
            resource=InventoryCursorResource.ASSETS,
            generation=generation or 7,
            page_size=page_size,
            rows_read=1,
            has_more=False,
            last_key=InventoryPageKey(
                sort_value=item.qualified_name,
                stable_id=item.locator.asset_id.root,
            ),
        )

    def list_fields(
        self,
        asset: CatalogAssetLocator,
        *,
        filters: CatalogFieldFilter,
        page_size: int,
        after: InventoryPageKey | None,
        generation: int | None,
    ) -> InventoryStorePage[CatalogFieldSummary]:
        if self.field_calls is None:
            self.field_calls = []
        self.field_calls.append((asset, filters, page_size, after, generation))
        item = _field()
        return InventoryStorePage(
            items=(item,),
            resource=InventoryCursorResource.FIELDS,
            generation=generation or 7,
            page_size=page_size,
            rows_read=1,
            has_more=False,
            last_key=InventoryPageKey(
                sort_value="customer_id",
                stable_id="customer_id",
            ),
        )


@dataclass
class _RefreshStore:
    state: CatalogRefreshState | None = None
    request_calls: int = 0

    def request(self, command: CatalogRefreshCommand) -> CatalogRefreshRequestResult:
        if self.state is None:
            self.request_calls += 1
            self.state = CatalogRefreshState(
                refresh_id=REFRESH_ID,
                workspace_id=command.workspace_id,
                connection_id=command.connection_id,
                mode=command.mode,
                status=CatalogRefreshStatus.REQUESTED,
                base_generation=7,
                target_generation=8,
                idempotency_digest=command.idempotency_digest,
                requested_by=command.requested_by,
                requested_at=command.requested_at,
                updated_at=command.requested_at,
            )
            replayed = False
        else:
            replayed = True
        return CatalogRefreshRequestResult(
            refresh=CatalogRefreshSummary.from_state(self.state),
            replayed=replayed,
        )

    def load_public(
        self,
        workspace_id: str,
        refresh_id: CatalogRefreshId,
    ) -> CatalogRefreshSummary | None:
        if (
            self.state is None
            or workspace_id != self.state.workspace_id
            or refresh_id != self.state.refresh_id
        ):
            return None
        return CatalogRefreshSummary.from_state(self.state)


@dataclass
class _Fixture:
    connections: _ConnectionStore
    inventory: _Inventory
    refreshes: _RefreshStore
    cursors: _Cursor

    @classmethod
    def create(cls) -> _Fixture:
        return cls(
            connections=_ConnectionStore(_connection()),
            inventory=_Inventory(),
            refreshes=_RefreshStore(),
            cursors=_Cursor(),
        )

    def services(self) -> CatalogHttpServices:
        connections = cast(CatalogConnectionStorePort, self.connections)
        inventory = cast(CatalogInventoryReadPort, self.inventory)
        refreshes = cast(CatalogRefreshStorePort, self.refreshes)
        cursors = cast(InventoryCursorPort, self.cursors)
        clock = _Clock()
        return CatalogHttpServices(
            list_connections=ListCatalogConnections(
                store=connections,
                cursors=cursors,
                clock=clock,
            ),
            register_connection=RegisterCatalogConnection(
                store=connections,
                clock=clock,
            ),
            disable_connection=DisableCatalogConnection(
                store=connections,
                clock=clock,
            ),
            list_assets=ListCatalogAssets(
                connections=connections,
                inventory=inventory,
                cursors=cursors,
                clock=clock,
            ),
            list_fields=ListCatalogFields(
                connections=connections,
                inventory=inventory,
                cursors=cursors,
                clock=clock,
            ),
            request_refresh=RequestCatalogRefresh(
                connections=connections,
                refreshes=refreshes,
                clock=clock,
            ),
            inspect_refresh=InspectCatalogRefresh(
                refreshes=refreshes,
                clock=clock,
            ),
        )


def _client(
    fixture: _Fixture | None = None,
    *,
    catalog: CatalogHttpServices | None = None,
) -> TestClient:
    selected_catalog = fixture.services() if fixture is not None else catalog
    unused = _UnusedJobPort()
    return TestClient(
        create_http_app(
            ApiHttpServices(
                authenticator=_Authenticator(),
                clock=_Clock(),
                submit=unused,  # type: ignore[arg-type]
                inspect=unused,  # type: ignore[arg-type]
                cancel=unused,  # type: ignore[arg-type]
                readiness=_Readiness(),
                catalog=selected_catalog,
            )
        ),
        raise_server_exceptions=False,
    )


def _headers(
    token: str = "admin-token",
    *,
    idempotency: bool = False,
) -> dict[str, str]:
    result = {"Authorization": f"Bearer {token}"}
    if idempotency:
        result["Idempotency-Key"] = IDEMPOTENCY_KEY
    return result


def _connection() -> CatalogConnectionSummary:
    return CatalogConnectionSummary(
        workspace_id="workspace-a",
        connection_id=CONNECTION_ID,
        display_name="Primary warehouse",
        kind=CatalogConnectionKind.DATAHUB_GRAPHQL,
        environment="PROD",
        catalog_scope="synthetic-demo",
        status=CatalogConnectionStatus.ENABLED,
        active_generation=7,
        asset_count=5_434,
        field_count=32_604,
        last_completed_at=NOW - timedelta(minutes=5),
        stale=False,
    )


def _asset() -> CatalogAssetSummary:
    return CatalogAssetSummary(
        locator=CatalogAssetLocator(
            workspace_id="workspace-a",
            connection_id=CONNECTION_ID,
            asset_id=ASSET_ID,
        ),
        generation=7,
        qualified_name="crm.customers",
        display_name="customers",
        platform="postgres",
        environment="PROD",
        database_name="schemabridge",
        schema_name="crm",
        description="Synthetic customers",
        field_count=6,
        metadata_fingerprint="a" * 64,
        observed_at=NOW - timedelta(minutes=5),
    )


def _field() -> CatalogFieldSummary:
    return CatalogFieldSummary(
        locator=CatalogFieldLocator(
            asset=CatalogAssetLocator(
                workspace_id="workspace-a",
                connection_id=CONNECTION_ID,
                asset_id=ASSET_ID,
            ),
            field_path=("customer_id",),
        ),
        generation=7,
        native_type="varchar",
        normalized_type=PhysicalValueType.STRING,
        description="Padded synthetic customer identifier",
        nullable=False,
        is_part_of_key=True,
        tags=("identifier",),
        glossary_terms=("Customer Key",),
        metadata_fingerprint="b" * 64,
        observed_at=NOW - timedelta(minutes=5),
    )


def _registration_body() -> dict[str, object]:
    return {
        "connection_id": CONNECTION_ID.root,
        "display_name": "Primary warehouse",
        "kind": "datahub_graphql",
        "environment": "PROD",
        "catalog_scope": "synthetic-demo",
        "platform_instance": (
            "urn:li:dataPlatformInstance:(urn:li:dataPlatform:postgres,warehouse-a)"
        ),
        "confirmation": "REGISTER CATALOG CONNECTION",
    }


def test_catalog_reads_are_strict_paginated_and_minimized() -> None:
    fixture = _Fixture.create()
    with _client(fixture) as client:
        connections = client.get(
            "/v1/catalog/connections",
            headers=_headers("analyst-token"),
            params={"query": "  PRIMARY   warehouse ", "status": "enabled", "page_size": 1},
        )
        assets = client.get(
            f"/v1/catalog/connections/{CONNECTION_ID.root}/assets",
            headers=_headers("analyst-token"),
            params={
                "platform": " POSTGRES ",
                "page_size": 1,
                "cursor": "opaque-assets-cursor",
                "generation": 7,
            },
        )
        fields = client.get(
            (
                f"/v1/catalog/connections/{CONNECTION_ID.root}/assets/"
                "urn:li:dataset:customers/fields"
            ),
            headers=_headers("analyst-token"),
            params={
                "native_type": " VARCHAR ",
                "page_size": 1,
                "cursor": "opaque-fields-cursor",
                "generation": 7,
            },
        )

    assert connections.status_code == 200
    assert connections.json()["resource"] == "connections"
    assert connections.json()["next_cursor"] == "opaque-connections-cursor"
    assert len(connections.json()["items"]) == 1
    assert fixture.connections.list_calls is not None
    workspace, filters, size, after = fixture.connections.list_calls[0]
    assert (workspace, filters.query, filters.status, size, after) == (
        "workspace-a",
        "primary warehouse",
        CatalogConnectionStatus.ENABLED,
        1,
        None,
    )

    assert assets.status_code == 200
    assert assets.json()["generation"] == 7
    assert assets.json()["items"][0]["asset_id"] == ASSET_ID.root
    assert assets.json()["items"][0]["database_name"] == "schemabridge"
    assert fields.status_code == 200
    assert fields.json()["generation"] == 7
    assert fields.json()["items"][0]["field_path"] == ["customer_id"]
    assert fields.json()["items"][0]["normalized_type"] == "string"
    assert fixture.inventory.asset_calls is not None
    assert fixture.inventory.asset_calls[0][0] == "workspace-a"
    assert fixture.inventory.asset_calls[0][2].platform == "postgres"
    assert fixture.inventory.asset_calls[0][-1] == 7
    assert fixture.inventory.field_calls is not None
    assert fixture.inventory.field_calls[0][0].workspace_id == "workspace-a"
    assert fixture.inventory.field_calls[0][1].native_type == "varchar"
    assert fixture.inventory.field_calls[0][-1] == 7

    encoded = " ".join((connections.text, assets.text, fields.text)).casefold()
    for forbidden in (
        "workspace_id",
        "credential_binding",
        "source_checkpoint",
        "lease",
        "requested_by",
        "dsn",
        "password",
        "token",
        "sql",
        "rows",
    ):
        assert forbidden not in encoded


def test_field_route_preserves_opaque_asset_ids_containing_slashes() -> None:
    fixture = _Fixture.create()
    opaque_asset_id = "urn:li:dataset:(urn:li:dataPlatform:postgres,folder/orders,PROD)"
    encoded_asset_id = quote(opaque_asset_id, safe="")

    with _client(fixture) as client:
        response = client.get(
            (f"/v1/catalog/connections/{CONNECTION_ID.root}/assets/{encoded_asset_id}/fields"),
            headers=_headers("analyst-token"),
            params={"page_size": 1, "generation": 7},
        )

    assert response.status_code == 200
    assert fixture.inventory.field_calls is not None
    assert fixture.inventory.field_calls[-1][0].asset_id == CatalogAssetId(opaque_asset_id)


def test_registration_is_admin_only_strict_and_exactly_replayable() -> None:
    fixture = _Fixture.create()
    with _client(fixture) as client:
        denied = client.post(
            "/v1/catalog/connections",
            headers=_headers("analyst-token", idempotency=True),
            json=_registration_body(),
        )
        extra_secret = client.post(
            "/v1/catalog/connections",
            headers=_headers(idempotency=True),
            json={**_registration_body(), "dsn": "postgresql://secret"},
        )
        extra_binding = client.post(
            "/v1/catalog/connections",
            headers=_headers(idempotency=True),
            json={
                **_registration_body(),
                "credential_binding_ref": "catalog-secret-binding-v1",
            },
        )
        invalid_storage_values = [
            client.post(
                "/v1/catalog/connections",
                headers=_headers(idempotency=True),
                json={**_registration_body(), field: value},
            )
            for field, value in (
                ("connection_id", "1connection"),
                ("display_name", "😀" * 101),
                ("environment", "A" * 81),
                ("platform_instance", "😀" * 101),
            )
        ]
        created = client.post(
            "/v1/catalog/connections",
            headers=_headers(idempotency=True),
            json=_registration_body(),
        )
        replayed = client.post(
            "/v1/catalog/connections",
            headers=_headers(idempotency=True),
            json=_registration_body(),
        )

    assert denied.status_code == 404
    assert denied.json()["code"] == "catalog_resource_unavailable"
    assert extra_secret.status_code == 422
    assert extra_binding.status_code == 422
    assert all(response.status_code == 422 for response in invalid_storage_values)
    assert created.status_code == 201
    assert created.json()["replayed"] is False
    assert replayed.status_code == 200
    assert replayed.json()["replayed"] is True
    assert fixture.connections.registration is not None
    assert fixture.connections.registration.platform_instance == (
        "urn:li:dataPlatformInstance:(urn:li:dataPlatform:postgres,warehouse-a)"
    )
    for response in (created, replayed):
        assert response.json()["connection"]["platform_instance"] == (
            "urn:li:dataPlatformInstance:(urn:li:dataPlatform:postgres,warehouse-a)"
        )
        assert "credential" not in response.text.casefold()
        assert "workspace" not in response.text.casefold()
        assert "idempotency" not in response.text.casefold()


def test_disable_requires_admin_confirmation_and_never_deletes_inventory() -> None:
    fixture = _Fixture.create()
    with _client(fixture) as client:
        denied = client.post(
            f"/v1/catalog/connections/{CONNECTION_ID.root}/disable",
            headers=_headers("auditor-token", idempotency=True),
            json={"confirmation": "DISABLE CATALOG CONNECTION"},
        )
        invalid = client.post(
            f"/v1/catalog/connections/{CONNECTION_ID.root}/disable",
            headers=_headers(idempotency=True),
            json={"confirmation": "disable"},
        )
        disabled = client.post(
            f"/v1/catalog/connections/{CONNECTION_ID.root}/disable",
            headers=_headers(idempotency=True),
            json={"confirmation": "DISABLE CATALOG CONNECTION"},
        )

    assert denied.status_code == 404
    assert invalid.status_code == 422
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    assert disabled.json()["asset_count"] == 5_434
    assert disabled.json()["active_generation"] == 7
    assert fixture.connections.disable_command is not None


def test_refresh_request_and_inspection_expose_only_safe_state() -> None:
    fixture = _Fixture.create()
    with _client(fixture) as client:
        denied = client.post(
            f"/v1/catalog/connections/{CONNECTION_ID.root}/refreshes",
            headers=_headers("analyst-token", idempotency=True),
            json={
                "mode": "full",
                "confirmation": "REQUEST CATALOG REFRESH",
            },
        )
        created = client.post(
            f"/v1/catalog/connections/{CONNECTION_ID.root}/refreshes",
            headers=_headers(idempotency=True),
            json={
                "mode": "full",
                "confirmation": "REQUEST CATALOG REFRESH",
            },
        )
        replayed = client.post(
            f"/v1/catalog/connections/{CONNECTION_ID.root}/refreshes",
            headers=_headers(idempotency=True),
            json={
                "mode": "full",
                "confirmation": "REQUEST CATALOG REFRESH",
            },
        )
        inspected = client.get(
            f"/v1/catalog/refreshes/{REFRESH_ID.root}",
            headers=_headers("auditor-token"),
        )
        hidden_tenant = client.get(
            f"/v1/catalog/refreshes/{REFRESH_ID.root}",
            headers=_headers("tenant-b-token"),
        )

    assert denied.status_code == 404
    assert created.status_code == 202 and created.json()["replayed"] is False
    assert replayed.status_code == 200 and replayed.json()["replayed"] is True
    assert inspected.status_code == 200
    assert inspected.json()["status"] == "requested"
    assert inspected.json()["target_generation"] == 8
    assert hidden_tenant.status_code == 404
    assert hidden_tenant.json()["code"] == "catalog_resource_unavailable"
    assert fixture.refreshes.request_calls == 1
    for forbidden in (
        "workspace",
        "requested_by",
        "idempotency",
        "checkpoint",
        "capability",
        "fencing",
        "lease",
        "token",
        "dsn",
    ):
        assert forbidden not in inspected.text.casefold()


@pytest.mark.parametrize(
    ("code", "status", "retry_after"),
    (
        (CatalogUseCaseErrorCode.INVALID_REQUEST, 422, None),
        (CatalogUseCaseErrorCode.UNAVAILABLE, 404, None),
        (CatalogUseCaseErrorCode.IDEMPOTENCY_CONFLICT, 409, None),
        (CatalogUseCaseErrorCode.CAPACITY_EXCEEDED, 429, "60"),
        (CatalogUseCaseErrorCode.CURSOR_UNAVAILABLE, 404, None),
        (CatalogUseCaseErrorCode.SERVICE_UNAVAILABLE, 503, None),
    ),
)
def test_catalog_failures_are_sanitized(
    code: CatalogUseCaseErrorCode,
    status: int,
    retry_after: str | None,
) -> None:
    secret = "postgresql://catalog-user:private-password@control/internal"

    class _FailingList:
        def execute(
            self,
            principal: AuthenticatedPrincipal,
            *,
            filters: CatalogConnectionFilter,
            page: object,
        ) -> object:
            del principal, filters, page
            raise CatalogUseCaseError(code, secret)

    fixture = _Fixture.create()
    catalog = replace(fixture.services(), list_connections=_FailingList())
    with _client(catalog=catalog) as client:
        response = client.get(
            "/v1/catalog/connections",
            headers=_headers("analyst-token"),
        )

    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == code.value
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert secret not in response.text
    if retry_after is None:
        assert "retry-after" not in response.headers
    else:
        assert response.headers["retry-after"] == retry_after


def test_catalog_schema_rejects_unknown_query_cursor_and_duplicate_idempotency() -> None:
    fixture = _Fixture.create()
    with _client(fixture) as client:
        unknown_query = client.get(
            "/v1/catalog/connections",
            headers=_headers("analyst-token"),
            params={"offset": 5},
        )
        oversized_cursor = client.get(
            f"/v1/catalog/connections/{CONNECTION_ID.root}/assets",
            headers=_headers("analyst-token"),
            params={"cursor": "x" * 1_025, "generation": 7},
        )
        duplicate_idempotency = client.post(
            "/v1/catalog/connections",
            headers=[
                ("Authorization", "Bearer admin-token"),
                ("Idempotency-Key", IDEMPOTENCY_KEY),
                ("Idempotency-Key", IDEMPOTENCY_KEY),
            ],
            json=_registration_body(),
        )

    assert unknown_query.status_code == 422
    assert oversized_cursor.status_code == 422
    assert duplicate_idempotency.status_code == 422
    assert fixture.connections.list_calls is None
    assert fixture.connections.registration is None


def test_uncomposed_catalog_is_503_but_authentication_still_fails_first() -> None:
    with _client() as client:
        unauthenticated = client.get(
            "/v1/catalog/connections",
            headers=_headers("wrong-token"),
        )
        unavailable = client.get(
            "/v1/catalog/connections",
            headers=_headers("analyst-token"),
        )

    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["code"] == "invalid_bearer_token"
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "catalog_service_unavailable"
