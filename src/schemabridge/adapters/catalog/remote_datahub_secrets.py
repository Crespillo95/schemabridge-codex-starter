"""Exact-version remote DataHub catalog secrets behind the M28 route contract."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from schemabridge.adapters.catalog.datahub_secrets import (
    DataHubCatalogSecretErrorCode,
    DataHubCatalogSecretResolutionError,
    ResolvedDataHubCatalogSecret,
    _parse_document,
    _secret_error,
    _validated_platform,
    _validated_server,
    _validated_token,
)
from schemabridge.adapters.connectors.remote_secrets import (
    ConnectorSecretCapability,
    VaultKvV2ConnectorSecretResolver,
)
from schemabridge.application.ports.catalog_inventory import (
    ManagedCatalogConnectorRoute,
)
from schemabridge.application.ports.connector_secrets import (
    ConnectorSecretErrorCode,
    ConnectorSecretResolutionError,
    OpaqueConnectorSecretRef,
)
from schemabridge.domain.catalog_inventory import (
    CatalogConnectionKind,
    CatalogConnectionStatus,
)


@dataclass(frozen=True, slots=True)
class VaultKvV2DataHubCatalogSecretResolver:
    """Validate a catalog route and read only its exact remote KV version."""

    backend: VaultKvV2ConnectorSecretResolver = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.backend, VaultKvV2ConnectorSecretResolver)
            or self.backend.capability is not ConnectorSecretCapability.CATALOG
        ):
            raise ValueError("remote DataHub catalog resolver configuration is invalid")

    def resolve(
        self,
        route: ManagedCatalogConnectorRoute,
    ) -> ResolvedDataHubCatalogSecret:
        """Return transient HTTPS DataHub material or one sanitized catalog failure."""

        if (
            not isinstance(route, ManagedCatalogConnectorRoute)
            or route.route.kind is not CatalogConnectionKind.DATAHUB_GRAPHQL
            or route.route.status is not CatalogConnectionStatus.ENABLED
            or route.route.contract_version is None
            or route.route.route_revision is None
            or route.route.target_fingerprint is None
            or route.provider_secret_version is None
        ):
            raise _secret_error(DataHubCatalogSecretErrorCode.ROUTE_MISMATCH)
        provider_secret_version = route.provider_secret_version
        assert provider_secret_version is not None
        try:
            reference = OpaqueConnectorSecretRef(
                route.credential_binding_ref,
                provider_secret_version=provider_secret_version,
            )
            document = self.backend.read_document(
                reference,
                version=provider_secret_version,
            )
            parsed = _parse_document(
                json.dumps(
                    document,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            server = _validated_server(parsed["server"])
            if urlsplit(server).scheme != "https":
                raise _secret_error(DataHubCatalogSecretErrorCode.INVALID)
            return ResolvedDataHubCatalogSecret(
                server=server,
                token=_validated_token(parsed["token"]),
                platform=_validated_platform(parsed["platform"]),
            )
        except DataHubCatalogSecretResolutionError:
            raise
        except ConnectorSecretResolutionError as error:
            code = {
                ConnectorSecretErrorCode.TARGET_MISMATCH: (
                    DataHubCatalogSecretErrorCode.ROUTE_MISMATCH
                ),
                ConnectorSecretErrorCode.VERSION_UNAVAILABLE: (
                    DataHubCatalogSecretErrorCode.ROUTE_MISMATCH
                ),
                ConnectorSecretErrorCode.INVALID: DataHubCatalogSecretErrorCode.INVALID,
            }.get(error.code, DataHubCatalogSecretErrorCode.UNAVAILABLE)
            raise _secret_error(code) from None
        except (TypeError, ValueError):
            raise _secret_error(DataHubCatalogSecretErrorCode.INVALID) from None


__all__ = ["VaultKvV2DataHubCatalogSecretResolver"]
