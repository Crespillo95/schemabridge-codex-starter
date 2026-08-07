"""Per-route DataHub catalog discovery with transient secret resolution."""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from schemabridge.adapters.catalog.datahub_scroll import (
    DataHubCatalogSourceConfig,
    DataHubCatalogSourceError,
    DataHubGraphQLCatalogSource,
    DataHubGraphQLTransport,
    UrllibDataHubGraphQLTransport,
)
from schemabridge.adapters.catalog.datahub_secrets import (
    DataHubCatalogSecretResolutionError,
    DataHubCatalogSecretResolverPort,
    datahub_catalog_identity_fingerprint,
)
from schemabridge.application.ports.catalog_inventory import (
    CatalogInventoryErrorCode,
    CatalogSourcePort,
    ManagedCatalogConnectorRoute,
)
from schemabridge.domain.catalog_inventory import (
    CatalogConnectionKind,
    CatalogConnectionRoute,
    CatalogConnectionStatus,
    CatalogRefreshFailureCode,
    CatalogRefreshMode,
    CatalogSourcePage,
)


@dataclass(frozen=True, slots=True)
class RoutedDataHubGraphQLCatalogSource:
    """Resolve the exact catalog secret immediately before every source page."""

    secrets: DataHubCatalogSecretResolverPort = field(repr=False)
    transport: DataHubGraphQLTransport = field(
        default_factory=UrllibDataHubGraphQLTransport,
        repr=False,
    )
    timeout_seconds: float = 15.0
    max_response_bytes: int = 4 * 1024 * 1024

    @property
    def source_label(self) -> str:
        return "live:routed-datahub-graphql-scroll-v1"

    def read_managed_page(
        self,
        managed_route: ManagedCatalogConnectorRoute,
        *,
        mode: CatalogRefreshMode,
        checkpoint: str | None,
        page_size: int,
    ) -> CatalogSourcePage:
        """Read one page without retaining secret material on this adapter."""

        route = managed_route.route
        if (
            route.kind is not CatalogConnectionKind.DATAHUB_GRAPHQL
            or route.status is not CatalogConnectionStatus.ENABLED
            or route.contract_version is None
            or route.route_revision is None
            or route.target_fingerprint is None
            or route.catalog_identity_fingerprint is None
        ):
            raise DataHubCatalogSourceError(
                CatalogInventoryErrorCode.RESOURCE_UNAVAILABLE,
                CatalogRefreshFailureCode.SOURCE_PERMISSION_DENIED,
                "catalog source route is unavailable",
            )
        try:
            secret = self.secrets.resolve(managed_route)
        except DataHubCatalogSecretResolutionError:
            raise DataHubCatalogSourceError(
                CatalogInventoryErrorCode.RESOURCE_UNAVAILABLE,
                CatalogRefreshFailureCode.SOURCE_PERMISSION_DENIED,
                "catalog source is unavailable",
            ) from None
        if secret.platform != "postgres":
            raise DataHubCatalogSourceError(
                CatalogInventoryErrorCode.RESOURCE_UNAVAILABLE,
                CatalogRefreshFailureCode.FINGERPRINT_MISMATCH,
                "catalog source is unavailable",
            )
        observed_identity = datahub_catalog_identity_fingerprint(
            server=secret.server,
            platform=secret.platform,
        )
        if not hmac.compare_digest(
            observed_identity,
            route.catalog_identity_fingerprint,
        ):
            raise DataHubCatalogSourceError(
                CatalogInventoryErrorCode.RESOURCE_UNAVAILABLE,
                CatalogRefreshFailureCode.FINGERPRINT_MISMATCH,
                "catalog source is unavailable",
            )
        try:
            source = DataHubGraphQLCatalogSource(
                config=DataHubCatalogSourceConfig(
                    server=secret.server,
                    token=secret.token,
                    credential_binding_ref=managed_route.credential_binding_ref,
                    platform=secret.platform,
                    timeout_seconds=self.timeout_seconds,
                    max_response_bytes=self.max_response_bytes,
                ),
                transport=self.transport,
            )
        except (TypeError, ValueError):
            raise DataHubCatalogSourceError(
                CatalogInventoryErrorCode.INVALID_RESPONSE,
                CatalogRefreshFailureCode.SOURCE_INVALID_RESPONSE,
                "catalog source configuration is invalid",
            ) from None
        return source.read_page(
            route,
            mode=mode,
            checkpoint=checkpoint,
            page_size=page_size,
        )


@dataclass(frozen=True, slots=True)
class _BoundRoutedDataHubGraphQLCatalogSource:
    """Private binding captured once; source pages receive only the public route."""

    source: RoutedDataHubGraphQLCatalogSource = field(repr=False)
    managed_route: ManagedCatalogConnectorRoute = field(repr=False)

    @property
    def source_label(self) -> str:
        return self.source.source_label

    def read_page(
        self,
        route: CatalogConnectionRoute,
        *,
        mode: CatalogRefreshMode,
        checkpoint: str | None,
        page_size: int,
    ) -> CatalogSourcePage:
        if route != self.managed_route.route:
            raise DataHubCatalogSourceError(
                CatalogInventoryErrorCode.RESOURCE_UNAVAILABLE,
                CatalogRefreshFailureCode.FINGERPRINT_MISMATCH,
                "catalog source route is unavailable",
            )
        return self.source.read_managed_page(
            self.managed_route,
            mode=mode,
            checkpoint=checkpoint,
            page_size=page_size,
        )


@dataclass(frozen=True, slots=True)
class RoutedCatalogSourceResolver:
    """Bind managed DataHub routes and delegate non-secret source kinds."""

    datahub: RoutedDataHubGraphQLCatalogSource = field(repr=False)
    sources: Mapping[CatalogConnectionKind, CatalogSourcePort] = field(
        default_factory=dict,
        repr=False,
    )

    def __post_init__(self) -> None:
        normalized = dict(self.sources)
        if CatalogConnectionKind.DATAHUB_GRAPHQL in normalized or any(
            not isinstance(kind, CatalogConnectionKind) for kind in normalized
        ):
            raise ValueError("routed catalog source registry is invalid")
        object.__setattr__(self, "sources", MappingProxyType(normalized))

    def resolve(
        self,
        route: ManagedCatalogConnectorRoute,
    ) -> CatalogSourcePort:
        if route.route.kind is CatalogConnectionKind.DATAHUB_GRAPHQL:
            return _BoundRoutedDataHubGraphQLCatalogSource(
                source=self.datahub,
                managed_route=route,
            )
        try:
            return self.sources[route.route.kind]
        except KeyError:
            raise DataHubCatalogSourceError(
                CatalogInventoryErrorCode.RESOURCE_UNAVAILABLE,
                CatalogRefreshFailureCode.SOURCE_INVALID_RESPONSE,
                "catalog source is unavailable",
            ) from None


__all__ = [
    "RoutedCatalogSourceResolver",
    "RoutedDataHubGraphQLCatalogSource",
]
