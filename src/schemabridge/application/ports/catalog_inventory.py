"""Application ports and sanitized failures for dynamic catalog inventory."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from schemabridge.domain.catalog_inventory import (
    CapacityAdmission,
    CatalogAssetFilter,
    CatalogAssetLocator,
    CatalogAssetSummary,
    CatalogConnectionDisable,
    CatalogConnectionFilter,
    CatalogConnectionId,
    CatalogConnectionRegistration,
    CatalogConnectionRegistrationResult,
    CatalogConnectionRoute,
    CatalogConnectionSummary,
    CatalogFieldFilter,
    CatalogFieldSummary,
    CatalogRefreshCommand,
    CatalogRefreshFailureCode,
    CatalogRefreshId,
    CatalogRefreshMode,
    CatalogRefreshRequestResult,
    CatalogRefreshState,
    CatalogRefreshSummary,
    CatalogScaleReport,
    CatalogSourcePage,
    InventoryCursorBinding,
    InventoryCursorPosition,
    InventoryPageKey,
    InventoryStorePage,
    TenantCapacityPolicy,
    TenantCapacityPolicyChange,
    TenantCapacitySnapshot,
)
from schemabridge.domain.connectors import MAX_ROUTE_REVISION

_PRIVATE_CATALOG_BINDING = re.compile(r"^[a-z][a-z0-9._:-]{2,199}$")


@dataclass(frozen=True, slots=True)
class ManagedCatalogConnectorRoute:
    """Lease-bound adapter value; only its public route may cross API boundaries."""

    route: CatalogConnectionRoute
    credential_binding_ref: str = field(repr=False)
    provider_secret_version: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.route, CatalogConnectionRoute)
            or _PRIVATE_CATALOG_BINDING.fullmatch(self.credential_binding_ref) is None
            or "://" in self.credential_binding_ref
            or "@" in self.credential_binding_ref
            or (
                self.provider_secret_version is not None
                and (
                    type(self.provider_secret_version) is not int
                    or not 1 <= self.provider_secret_version <= MAX_ROUTE_REVISION
                )
            )
        ):
            raise ValueError("managed catalog connector route is invalid")


class InventoryCursorErrorCode(StrEnum):
    """One non-disclosing boundary for every invalid external cursor."""

    UNAVAILABLE = "inventory_cursor_unavailable"


class InventoryCursorError(RuntimeError):
    """A cursor could not be authenticated against its expected read scope."""

    def __init__(self, message: str = "inventory cursor is unavailable") -> None:
        self.code = InventoryCursorErrorCode.UNAVAILABLE
        super().__init__(message)


class InventoryCursorPort(Protocol):
    """Encode and authenticate bounded keyset cursors without exposing signing keys."""

    def encode(
        self,
        *,
        binding: InventoryCursorBinding,
        last_key: InventoryPageKey,
        issued_at: datetime,
        not_after: datetime | None = None,
    ) -> str:
        """Encode one final key only when its full lifetime fits the generation window."""

    def decode(
        self,
        *,
        cursor: str,
        expected_binding: InventoryCursorBinding,
        at: datetime,
    ) -> InventoryCursorPosition:
        """Return the key and immutable lifetime after authenticating the full cursor."""


class CatalogInventoryErrorCode(StrEnum):
    """Stable non-vendor failures safe at API and indexer boundaries."""

    UNAVAILABLE = "catalog_inventory_unavailable"
    RESOURCE_UNAVAILABLE = "catalog_resource_unavailable"
    POLICY_MISSING = "tenant_capacity_policy_missing"
    CAPACITY_EXCEEDED = "catalog_capacity_exceeded"
    POLICY_CONFLICT = "tenant_capacity_policy_conflict"
    IDEMPOTENCY_CONFLICT = "catalog_idempotency_conflict"
    CONNECTION_DISABLED = "catalog_connection_disabled"
    DELTA_UNSUPPORTED = "catalog_delta_unsupported"
    REFRESH_CONFLICT = "catalog_refresh_conflict"
    LEASE_CONFLICT = "catalog_refresh_lease_conflict"
    GENERATION_UNAVAILABLE = "catalog_generation_unavailable"
    INVALID_RESPONSE = "catalog_inventory_invalid_response"


class CatalogInventoryError(RuntimeError):
    """Sanitized persistence/source failure without SQL, secrets, or vendor payloads."""

    def __init__(self, code: CatalogInventoryErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class CatalogConnectionStorePort(Protocol):
    """API-safe public connection metadata and logical-disable operations."""

    def register(
        self,
        command: CatalogConnectionRegistration,
    ) -> CatalogConnectionRegistrationResult:
        """Create or exactly replay one connection registration."""

    def disable(self, command: CatalogConnectionDisable) -> CatalogConnectionSummary:
        """Logically disable one exact tenant connection without deleting history."""

    def load_public(
        self,
        workspace_id: str,
        connection_id: CatalogConnectionId,
    ) -> CatalogConnectionSummary | None:
        """Load public metadata without returning the credential-binding reference."""

    def list_connections(
        self,
        workspace_id: str,
        *,
        filters: CatalogConnectionFilter,
        page_size: int,
        after: InventoryPageKey | None,
    ) -> InventoryStorePage[CatalogConnectionSummary]:
        """Read at most page_size plus one row by exclusive tuple key."""


class CatalogConnectionRoutePort(Protocol):
    """Indexer-only route lookup kept out of API composition."""

    def load_route(
        self,
        workspace_id: str,
        connection_id: CatalogConnectionId,
        *,
        refresh_id: CatalogRefreshId,
        indexer_id: str,
        lease_capability: str,
        fencing_token: int,
    ) -> ManagedCatalogConnectorRoute | None:
        """Load one binding only for the exact currently owned refresh lease."""


class CatalogRefreshPublicStorePort(Protocol):
    """API-safe refresh request and sanitized inspection boundary."""

    def request(self, command: CatalogRefreshCommand) -> CatalogRefreshRequestResult:
        """Create or exactly replay one asynchronous refresh request."""

    def load_public(
        self,
        workspace_id: str,
        refresh_id: CatalogRefreshId,
    ) -> CatalogRefreshSummary | None:
        """Load a public projection without checkpoint or lease material."""


class CatalogInventoryReadPort(Protocol):
    """Read only active or explicitly retained catalog generations by keyset."""

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
        """Read one active/exact generation, never a complete materialized collection."""

    def list_fields(
        self,
        asset: CatalogAssetLocator,
        *,
        filters: CatalogFieldFilter,
        page_size: int,
        after: InventoryPageKey | None,
        generation: int | None,
    ) -> InventoryStorePage[CatalogFieldSummary]:
        """Read a bounded field page scoped by tenant, connection, and asset."""


class CatalogRefreshStorePort(Protocol):
    """Durable request/lease/staging/promotion boundary for catalog generations."""

    def request(self, command: CatalogRefreshCommand) -> CatalogRefreshRequestResult:
        """Create or exactly replay one asynchronous refresh request."""

    def load(
        self,
        workspace_id: str,
        refresh_id: CatalogRefreshId,
    ) -> CatalogRefreshState | None:
        """Load one tenant-scoped refresh without cross-scope disclosure."""

    def claim_next(
        self,
        *,
        indexer_id: str,
        lease_capability: str,
        lease_duration: timedelta,
    ) -> CatalogRefreshState | None:
        """Lease one eligible refresh using database time and a monotonic fence."""

    def begin_staging(
        self,
        workspace_id: str,
        refresh_id: CatalogRefreshId,
        *,
        indexer_id: str,
        lease_capability: str,
        fencing_token: int,
    ) -> CatalogRefreshState:
        """Move the exact lease from discovery ownership into staging."""

    def persist_page(
        self,
        workspace_id: str,
        refresh_id: CatalogRefreshId,
        *,
        indexer_id: str,
        lease_capability: str,
        fencing_token: int,
        page: CatalogSourcePage,
    ) -> CatalogRefreshState:
        """Atomically stage one idempotent page and its source checkpoint."""

    def heartbeat(
        self,
        workspace_id: str,
        refresh_id: CatalogRefreshId,
        *,
        indexer_id: str,
        lease_capability: str,
        fencing_token: int,
        lease_duration: timedelta,
    ) -> CatalogRefreshState:
        """Renew exact refresh ownership using database time."""

    def complete(
        self,
        workspace_id: str,
        refresh_id: CatalogRefreshId,
        *,
        indexer_id: str,
        lease_capability: str,
        fencing_token: int,
        expected_base_generation: int,
        expected_contract_version: int,
        expected_route_revision: int,
        expected_target_fingerprint: str,
    ) -> CatalogRefreshState:
        """Promote only while the exact lease-bound connector target stays current."""

    def fail(
        self,
        workspace_id: str,
        refresh_id: CatalogRefreshId,
        *,
        indexer_id: str,
        lease_capability: str,
        fencing_token: int,
        code: CatalogRefreshFailureCode,
    ) -> CatalogRefreshState:
        """Persist one closed sanitized failure without exposing vendor detail."""

    def reclaim_expired(self, *, limit: int = 100) -> int:
        """Return a bounded set of expired leased/staging refreshes to requested."""

    def prune_due_generations(self, *, limit: int = 100) -> int:
        """Prune one bounded cross-tenant batch whose retention window has elapsed."""

    def prune_generations(
        self,
        workspace_id: str,
        connection_id: CatalogConnectionId,
        *,
        completed_before: datetime,
        limit: int = 100,
    ) -> int:
        """Delete only inactive generations older than the cursor-retention boundary."""


class CatalogSourcePort(Protocol):
    """Mutation-free source discovery used only by the catalog-indexer process."""

    @property
    def source_label(self) -> str:
        """Bounded operator-visible source label."""

    def read_page(
        self,
        route: CatalogConnectionRoute,
        *,
        mode: CatalogRefreshMode,
        checkpoint: str | None,
        page_size: int,
    ) -> CatalogSourcePage:
        """Read one stable source page; callers persist it before requesting another."""


class TenantCapacityPort(Protocol):
    """Cross-replica policy and atomic capacity-admission boundary."""

    def load_policy(self, workspace_id: str) -> TenantCapacityPolicy | None:
        """Load durable tenant policy; absence must fail closed."""

    def inspect(self, workspace_id: str) -> TenantCapacitySnapshot:
        """Return current tenant counts against the exact policy version."""

    def admit_api_request(
        self,
        workspace_id: str,
        actor_id: str,
    ) -> CapacityAdmission:
        """Atomically apply the fixed one-minute principal/workspace limit."""


class TenantCapacityPolicyOperatorPort(Protocol):
    """Migrator-only optimistic policy creation/revision boundary."""

    def apply(self, change: TenantCapacityPolicyChange) -> TenantCapacityPolicy:
        """Apply one explicitly confirmed revision and retain its immutable audit row."""


class CatalogScaleReportPort(Protocol):
    """Persist a bounded reproducible scale report supplied by a reporting use case."""

    def record(self, report: CatalogScaleReport) -> None:
        """Record sanitized measurements; adapters choose JSON/Markdown persistence."""
