"""Authenticated use cases for the dynamic catalog inventory."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, TypeVar

from schemabridge.application.ports.catalog_inventory import (
    CatalogConnectionStorePort,
    CatalogInventoryError,
    CatalogInventoryErrorCode,
    CatalogInventoryReadPort,
    CatalogRefreshPublicStorePort,
    InventoryCursorError,
    InventoryCursorPort,
    TenantCapacityPolicyOperatorPort,
)
from schemabridge.application.ports.operational_telemetry import (
    OperationalResourceAccessCause,
)
from schemabridge.domain.catalog_inventory import (
    CatalogAssetFilter,
    CatalogAssetLocator,
    CatalogAssetSummary,
    CatalogConnectionDisable,
    CatalogConnectionFilter,
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogConnectionRegistration,
    CatalogConnectionRegistrationResult,
    CatalogConnectionSummary,
    CatalogFieldFilter,
    CatalogFieldSummary,
    CatalogRefreshCommand,
    CatalogRefreshId,
    CatalogRefreshMode,
    CatalogRefreshRequestResult,
    CatalogRefreshSummary,
    InventoryCursorBinding,
    InventoryCursorPosition,
    InventoryCursorResource,
    InventoryPage,
    InventoryPageRequest,
    InventoryStorePage,
    TenantCapacityPolicy,
    TenantCapacityPolicyChange,
    inventory_filter_fingerprint,
)
from schemabridge.domain.identity import AuthenticatedPrincipal, IdentityRole

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
_CONNECTION_SORT_FINGERPRINT = inventory_filter_fingerprint(
    {"keys": ["display_name", "connection_id"], "version": 1}
)
_ASSET_SORT_FINGERPRINT = inventory_filter_fingerprint(
    {"keys": ["qualified_name", "asset_id"], "version": 1}
)
_FIELD_SORT_FINGERPRINT = inventory_filter_fingerprint(
    {"keys": ["field_path", "field_id"], "version": 1}
)
_ReadableT = TypeVar("_ReadableT")


class CatalogUseCaseErrorCode(StrEnum):
    """Stable failures safe for the authenticated HTTP boundary."""

    INVALID_REQUEST = "catalog_invalid_request"
    UNAVAILABLE = "catalog_resource_unavailable"
    IDEMPOTENCY_CONFLICT = "catalog_idempotency_conflict"
    CAPACITY_EXCEEDED = "catalog_capacity_exceeded"
    POLICY_CONFLICT = "tenant_capacity_policy_conflict"
    CURSOR_UNAVAILABLE = "inventory_cursor_unavailable"
    SERVICE_UNAVAILABLE = "catalog_service_unavailable"


class CatalogUseCaseError(RuntimeError):
    """A sanitized catalog operation failure."""

    def __init__(
        self,
        code: CatalogUseCaseErrorCode,
        message: str,
        *,
        resource_access_cause: OperationalResourceAccessCause | None = None,
    ) -> None:
        if (code is CatalogUseCaseErrorCode.UNAVAILABLE) != (resource_access_cause is not None):
            raise ValueError("invalid internal resource access cause")
        self.code = code
        self.resource_access_cause = resource_access_cause
        super().__init__(message)


class CatalogClockPort(Protocol):
    def now(self) -> datetime:
        """Return one aware current instant."""


@dataclass(frozen=True, slots=True)
class ApplyTenantCapacityPolicy:
    """Apply one exact migrator-only policy revision through optimistic locking."""

    store: TenantCapacityPolicyOperatorPort

    def execute(self, change: TenantCapacityPolicyChange) -> TenantCapacityPolicy:
        return _call_store(lambda: self.store.apply(change))


@dataclass(frozen=True, slots=True)
class ListCatalogConnections:
    store: CatalogConnectionStorePort
    cursors: InventoryCursorPort
    clock: CatalogClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        filters: CatalogConnectionFilter,
        page: InventoryPageRequest,
    ) -> InventoryPage[CatalogConnectionSummary]:
        now = self.clock.now()
        _require_reader(principal, now)
        binding = InventoryCursorBinding(
            workspace_id=principal.workspace_id,
            resource=InventoryCursorResource.CONNECTIONS,
            filter_fingerprint=filters.fingerprint,
            sort_fingerprint=_CONNECTION_SORT_FINGERPRINT,
        )
        continuation = _decode_cursor(self.cursors, page.cursor, binding=binding, at=now)
        result = _call_store(
            lambda: self.store.list_connections(
                principal.workspace_id,
                filters=filters,
                page_size=page.size,
                after=None if continuation is None else continuation.last_key,
            )
        )
        _require_store_page(
            result,
            resource=InventoryCursorResource.CONNECTIONS,
            page_size=page.size,
            generation=None,
        )
        return _public_page(
            result,
            binding=binding,
            cursors=self.cursors,
            at=now,
            stale=any(item.stale for item in result.items),
            continuation=continuation,
        )


@dataclass(frozen=True, slots=True)
class ListCatalogAssets:
    connections: CatalogConnectionStorePort
    inventory: CatalogInventoryReadPort
    cursors: InventoryCursorPort
    clock: CatalogClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        connection_id: CatalogConnectionId,
        *,
        filters: CatalogAssetFilter,
        page: InventoryPageRequest,
        generation: int | None = None,
    ) -> InventoryPage[CatalogAssetSummary]:
        now = self.clock.now()
        _require_reader(principal, now)
        if page.cursor is not None and generation is None:
            raise _cursor_unavailable()
        binding = (
            None
            if generation is None
            else InventoryCursorBinding(
                workspace_id=principal.workspace_id,
                resource=InventoryCursorResource.ASSETS,
                connection_id=connection_id,
                generation=generation,
                filter_fingerprint=filters.fingerprint,
                sort_fingerprint=_ASSET_SORT_FINGERPRINT,
            )
        )
        continuation = (
            None
            if page.cursor is None
            else _decode_cursor(
                self.cursors,
                page.cursor,
                binding=_required_binding(binding),
                at=now,
            )
        )
        connection = _load_enabled_connection(
            self.connections,
            principal.workspace_id,
            connection_id,
        )
        result = _call_store(
            lambda: self.inventory.list_assets(
                principal.workspace_id,
                connection_id,
                filters=filters,
                page_size=page.size,
                after=None if continuation is None else continuation.last_key,
                generation=generation,
            )
        )
        _require_store_page(
            result,
            resource=InventoryCursorResource.ASSETS,
            page_size=page.size,
            generation=generation,
        )
        assert result.generation is not None
        _require_generation_cursor_window(
            result,
            active_generation=connection.active_generation,
        )
        result_binding = InventoryCursorBinding(
            workspace_id=principal.workspace_id,
            resource=InventoryCursorResource.ASSETS,
            connection_id=connection_id,
            generation=result.generation,
            filter_fingerprint=filters.fingerprint,
            sort_fingerprint=_ASSET_SORT_FINGERPRINT,
        )
        return _public_page(
            result,
            binding=result_binding,
            cursors=self.cursors,
            at=now,
            stale=(connection.stale or connection.active_generation != result.generation),
            continuation=continuation,
        )


@dataclass(frozen=True, slots=True)
class ListCatalogFields:
    connections: CatalogConnectionStorePort
    inventory: CatalogInventoryReadPort
    cursors: InventoryCursorPort
    clock: CatalogClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        asset: CatalogAssetLocator,
        *,
        filters: CatalogFieldFilter,
        page: InventoryPageRequest,
        generation: int | None = None,
    ) -> InventoryPage[CatalogFieldSummary]:
        now = self.clock.now()
        _require_reader(principal, now)
        if asset.workspace_id != principal.workspace_id:
            raise _not_found()
        if page.cursor is not None and generation is None:
            raise _cursor_unavailable()
        binding = (
            None
            if generation is None
            else InventoryCursorBinding(
                workspace_id=principal.workspace_id,
                resource=InventoryCursorResource.FIELDS,
                connection_id=asset.connection_id,
                asset_id=asset.asset_id,
                generation=generation,
                filter_fingerprint=filters.fingerprint,
                sort_fingerprint=_FIELD_SORT_FINGERPRINT,
            )
        )
        continuation = (
            None
            if page.cursor is None
            else _decode_cursor(
                self.cursors,
                page.cursor,
                binding=_required_binding(binding),
                at=now,
            )
        )
        connection = _load_enabled_connection(
            self.connections,
            principal.workspace_id,
            asset.connection_id,
        )
        result = _call_store(
            lambda: self.inventory.list_fields(
                asset,
                filters=filters,
                page_size=page.size,
                after=None if continuation is None else continuation.last_key,
                generation=generation,
            )
        )
        _require_store_page(
            result,
            resource=InventoryCursorResource.FIELDS,
            page_size=page.size,
            generation=generation,
        )
        assert result.generation is not None
        _require_generation_cursor_window(
            result,
            active_generation=connection.active_generation,
        )
        result_binding = InventoryCursorBinding(
            workspace_id=principal.workspace_id,
            resource=InventoryCursorResource.FIELDS,
            connection_id=asset.connection_id,
            asset_id=asset.asset_id,
            generation=result.generation,
            filter_fingerprint=filters.fingerprint,
            sort_fingerprint=_FIELD_SORT_FINGERPRINT,
        )
        return _public_page(
            result,
            binding=result_binding,
            cursors=self.cursors,
            at=now,
            stale=(connection.stale or connection.active_generation != result.generation),
            continuation=continuation,
        )


@dataclass(frozen=True, slots=True)
class RegisterCatalogConnection:
    store: CatalogConnectionStorePort
    clock: CatalogClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        connection_id: CatalogConnectionId,
        display_name: str,
        kind: CatalogConnectionKind,
        environment: str,
        catalog_scope: str,
        confirmation: str,
        idempotency_key: str,
        platform_instance: str | None = None,
    ) -> CatalogConnectionRegistrationResult:
        now = self.clock.now()
        _require_admin(principal, now)
        if confirmation != "REGISTER CATALOG CONNECTION":
            raise _invalid_request()
        try:
            command = CatalogConnectionRegistration(
                workspace_id=principal.workspace_id,
                connection_id=connection_id,
                display_name=display_name,
                kind=kind,
                environment=environment,
                catalog_scope=catalog_scope,
                platform_instance=platform_instance,
                requested_by=principal.actor_id,
                requested_at=now,
                idempotency_digest=_idempotency_digest(idempotency_key),
            )
        except ValueError as error:
            raise _invalid_request() from error
        return _call_store(lambda: self.store.register(command))


@dataclass(frozen=True, slots=True)
class DisableCatalogConnection:
    store: CatalogConnectionStorePort
    clock: CatalogClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        connection_id: CatalogConnectionId,
        *,
        confirmation: str,
        idempotency_key: str,
    ) -> CatalogConnectionSummary:
        now = self.clock.now()
        _require_admin(principal, now)
        if confirmation != "DISABLE CATALOG CONNECTION":
            raise _invalid_request()
        try:
            command = CatalogConnectionDisable(
                workspace_id=principal.workspace_id,
                connection_id=connection_id,
                requested_by=principal.actor_id,
                requested_at=now,
                idempotency_digest=_idempotency_digest(idempotency_key),
            )
        except ValueError as error:
            raise _invalid_request() from error
        return _call_store(lambda: self.store.disable(command))


@dataclass(frozen=True, slots=True)
class RequestCatalogRefresh:
    connections: CatalogConnectionStorePort
    refreshes: CatalogRefreshPublicStorePort
    clock: CatalogClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        connection_id: CatalogConnectionId,
        *,
        mode: CatalogRefreshMode,
        confirmation: str,
        idempotency_key: str,
    ) -> CatalogRefreshRequestResult:
        now = self.clock.now()
        _require_admin(principal, now)
        if confirmation != "REQUEST CATALOG REFRESH":
            raise _invalid_request()
        _load_enabled_connection(
            self.connections,
            principal.workspace_id,
            connection_id,
        )
        try:
            command = CatalogRefreshCommand(
                workspace_id=principal.workspace_id,
                connection_id=connection_id,
                mode=mode,
                requested_by=principal.actor_id,
                requested_at=now,
                idempotency_digest=_idempotency_digest(idempotency_key),
            )
        except ValueError as error:
            raise _invalid_request() from error
        return _call_store(lambda: self.refreshes.request(command))


@dataclass(frozen=True, slots=True)
class InspectCatalogRefresh:
    refreshes: CatalogRefreshPublicStorePort
    clock: CatalogClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        refresh_id: CatalogRefreshId,
    ) -> CatalogRefreshSummary:
        now = self.clock.now()
        _require_reader(principal, now)
        summary = _call_store(
            lambda: self.refreshes.load_public(principal.workspace_id, refresh_id)
        )
        if summary is None:
            raise _not_found()
        return summary


def _public_page(
    page: InventoryStorePage[_ReadableT],
    *,
    binding: InventoryCursorBinding,
    cursors: InventoryCursorPort,
    at: datetime,
    stale: bool,
    continuation: InventoryCursorPosition | None,
) -> InventoryPage[_ReadableT]:
    next_cursor = None
    if page.has_more:
        assert page.last_key is not None
        try:
            next_cursor = cursors.encode(
                binding=binding,
                last_key=page.last_key,
                issued_at=at if continuation is None else continuation.issued_at,
                not_after=page.cursor_valid_until,
            )
        except (InventoryCursorError, TypeError, ValueError) as error:
            raise _cursor_unavailable() from error
    return InventoryPage(
        items=page.items,
        resource=page.resource,
        generation=page.generation,
        next_cursor=next_cursor,
        as_of=at,
        stale=stale,
    )


def _decode_cursor(
    cursors: InventoryCursorPort,
    cursor: str | None,
    *,
    binding: InventoryCursorBinding,
    at: datetime,
) -> InventoryCursorPosition | None:
    if cursor is None:
        return None
    try:
        return cursors.decode(
            cursor=cursor,
            expected_binding=binding,
            at=at,
        )
    except (InventoryCursorError, TypeError, ValueError) as error:
        raise _cursor_unavailable() from error


def _require_store_page(
    page: InventoryStorePage[_ReadableT],
    *,
    resource: InventoryCursorResource,
    page_size: int,
    generation: int | None,
) -> None:
    if (
        page.resource is not resource
        or page.page_size != page_size
        or (generation is not None and page.generation != generation)
    ):
        raise CatalogUseCaseError(
            CatalogUseCaseErrorCode.SERVICE_UNAVAILABLE,
            "catalog service is unavailable",
        )


def _require_generation_cursor_window(
    page: InventoryStorePage[_ReadableT],
    *,
    active_generation: int | None,
) -> None:
    if (
        page.generation is not None
        and page.generation != active_generation
        and page.cursor_valid_until is None
    ):
        raise CatalogUseCaseError(
            CatalogUseCaseErrorCode.SERVICE_UNAVAILABLE,
            "catalog service is unavailable",
        )


def _load_enabled_connection(
    store: CatalogConnectionStorePort,
    workspace_id: str,
    connection_id: CatalogConnectionId,
) -> CatalogConnectionSummary:
    connection = _call_store(lambda: store.load_public(workspace_id, connection_id))
    if connection is None or connection.status.value != "enabled":
        raise _not_found()
    return connection


def _required_binding(
    binding: InventoryCursorBinding | None,
) -> InventoryCursorBinding:
    if binding is None:
        raise _cursor_unavailable()
    return binding


def _require_reader(principal: AuthenticatedPrincipal, at: datetime) -> None:
    if not principal.is_current(at) or not principal.roles:
        raise _denied()


def _require_admin(principal: AuthenticatedPrincipal, at: datetime) -> None:
    if not principal.is_current(at) or IdentityRole.PLATFORM_ADMIN not in principal.roles:
        raise _denied()


def _idempotency_digest(value: str) -> str:
    if not isinstance(value, str) or _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise _invalid_request()
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _call_store(operation: Callable[[], _ReadableT]) -> _ReadableT:
    try:
        return operation()
    except CatalogUseCaseError:
        raise
    except CatalogInventoryError as error:
        if error.code in {
            CatalogInventoryErrorCode.RESOURCE_UNAVAILABLE,
            CatalogInventoryErrorCode.CONNECTION_DISABLED,
            CatalogInventoryErrorCode.GENERATION_UNAVAILABLE,
        }:
            raise _not_found() from error
        code = {
            CatalogInventoryErrorCode.IDEMPOTENCY_CONFLICT: (
                CatalogUseCaseErrorCode.IDEMPOTENCY_CONFLICT
            ),
            CatalogInventoryErrorCode.CAPACITY_EXCEEDED: (
                CatalogUseCaseErrorCode.CAPACITY_EXCEEDED
            ),
            CatalogInventoryErrorCode.POLICY_CONFLICT: (CatalogUseCaseErrorCode.POLICY_CONFLICT),
        }.get(error.code, CatalogUseCaseErrorCode.SERVICE_UNAVAILABLE)
        raise CatalogUseCaseError(code, "catalog operation could not be completed") from error
    except Exception as error:
        raise CatalogUseCaseError(
            CatalogUseCaseErrorCode.SERVICE_UNAVAILABLE,
            "catalog service is unavailable",
        ) from error


def _invalid_request() -> CatalogUseCaseError:
    return CatalogUseCaseError(
        CatalogUseCaseErrorCode.INVALID_REQUEST,
        "catalog request is invalid",
    )


def _denied() -> CatalogUseCaseError:
    return CatalogUseCaseError(
        CatalogUseCaseErrorCode.UNAVAILABLE,
        "catalog resource is unavailable",
        resource_access_cause=OperationalResourceAccessCause.DENIED,
    )


def _not_found() -> CatalogUseCaseError:
    return CatalogUseCaseError(
        CatalogUseCaseErrorCode.UNAVAILABLE,
        "catalog resource is unavailable",
        resource_access_cause=OperationalResourceAccessCause.NOT_FOUND,
    )


def _cursor_unavailable() -> CatalogUseCaseError:
    return CatalogUseCaseError(
        CatalogUseCaseErrorCode.CURSOR_UNAVAILABLE,
        "inventory cursor is unavailable",
    )
