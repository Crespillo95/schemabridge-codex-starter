from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from schemabridge.adapters.catalog.datahub_secrets import (
    DataHubCatalogSecretErrorCode,
    DataHubCatalogSecretResolutionError,
    datahub_catalog_identity_fingerprint,
)
from schemabridge.adapters.catalog.remote_datahub_secrets import (
    VaultKvV2DataHubCatalogSecretResolver,
)
from schemabridge.adapters.connectors.remote_secrets import (
    ConnectorSecretCapability,
    VaultKvV2ConnectorSecretResolver,
)
from schemabridge.application.ports.catalog_inventory import (
    ManagedCatalogConnectorRoute,
)
from schemabridge.domain.catalog_inventory import (
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogConnectionRoute,
    CatalogConnectionStatus,
)

PRIVATE_BINDING = "vault:datahub:tenant-alpha"
PRIVATE_TOKEN = "private-datahub-token-alpha"
PRIVATE_SERVER = "https://datahub-alpha.example.test"
CLIENT_TOKEN = "hvs.synthetic-client-token-0000000000000000"
PROVIDER_SECRET_VERSION = 29


@dataclass(slots=True)
class _Identity:
    calls: int = 0

    def read(self) -> str:
        self.calls += 1
        return "header.payload.signature"


@dataclass(slots=True)
class _Transport:
    responses: list[bytes]
    calls: list[dict[str, object]] = field(default_factory=list)

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: object,
        body: bytes | None,
        ca_bundle: Path,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> bytes:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "body": body,
                "ca_bundle": ca_bundle,
                "timeout_seconds": timeout_seconds,
                "max_response_bytes": max_response_bytes,
            }
        )
        return self.responses.pop(0)


def _login() -> bytes:
    return json.dumps(
        {
            "auth": {
                "client_token": CLIENT_TOKEN,
                "lease_duration": 600,
                "renewable": False,
            }
        }
    ).encode()


def _secret(
    *,
    version: int = PROVIDER_SECRET_VERSION,
    server: str = PRIVATE_SERVER,
) -> bytes:
    return json.dumps(
        {
            "data": {
                "data": {
                    "format_version": 1,
                    "kind": "datahub_graphql",
                    "platform": "postgres",
                    "server": server,
                    "token": PRIVATE_TOKEN,
                },
                "metadata": {
                    "deletion_time": "",
                    "destroyed": False,
                    "version": version,
                },
            }
        }
    ).encode()


def _route(
    *,
    provider_secret_version: int | None = PROVIDER_SECRET_VERSION,
    **updates: object,
) -> ManagedCatalogConnectorRoute:
    payload: dict[str, object] = {
        "workspace_id": "workspace_alpha",
        "connection_id": CatalogConnectionId("connection_alpha"),
        "kind": CatalogConnectionKind.DATAHUB_GRAPHQL,
        "environment": "PROD",
        "catalog_scope": "warehouse_alpha",
        "platform_instance": "warehouse-alpha",
        "catalog_identity_fingerprint": datahub_catalog_identity_fingerprint(
            server=PRIVATE_SERVER,
            platform="postgres",
        ),
        "status": CatalogConnectionStatus.ENABLED,
        "contract_version": 1,
        "route_revision": 4,
        "target_fingerprint": "a" * 64,
    }
    payload.update(updates)
    return ManagedCatalogConnectorRoute(
        route=CatalogConnectionRoute.model_validate(payload),
        credential_binding_ref=PRIVATE_BINDING,
        provider_secret_version=provider_secret_version,
    )


def _resolver(
    tmp_path: Path,
    responses: list[bytes] | None = None,
) -> tuple[VaultKvV2DataHubCatalogSecretResolver, _Identity, _Transport]:
    ca_bundle = (tmp_path / "ca.crt").resolve()
    ca_bundle.write_text("synthetic trust anchor", encoding="utf-8")
    ca_bundle.chmod(0o600)
    identity = _Identity()
    transport = _Transport(responses or [_login(), _secret()])
    backend = VaultKvV2ConnectorSecretResolver(
        server="https://secrets.example.test",
        role="schemabridge-catalog",
        kv_mount="tenant-connectors",
        capability=ConnectorSecretCapability.CATALOG,
        identity=identity,
        ca_bundle=ca_bundle,
        transport=transport,
    )
    return VaultKvV2DataHubCatalogSecretResolver(backend), identity, transport


def test_remote_datahub_resolver_reads_exact_catalog_version_over_https(
    tmp_path: Path,
) -> None:
    resolver, identity, transport = _resolver(tmp_path)

    secret = resolver.resolve(_route())

    assert identity.calls == 1
    assert secret.server == PRIVATE_SERVER
    assert secret.token == PRIVATE_TOKEN
    assert secret.platform == "postgres"
    assert PRIVATE_TOKEN not in repr(secret)
    assert PRIVATE_BINDING not in repr(resolver)
    assert len(transport.calls) == 2
    read = transport.calls[1]
    assert read["url"] == (
        "https://secrets.example.test/v1/tenant-connectors/data/schemabridge/"
        f"catalog/{hashlib.sha256(PRIVATE_BINDING.encode()).hexdigest()}"
        f"?version={PROVIDER_SECRET_VERSION}"
    )
    assert PRIVATE_BINDING not in str(read)


@pytest.mark.parametrize(
    ("responses", "expected"),
    (
        ([_login(), _secret(version=28)], DataHubCatalogSecretErrorCode.ROUTE_MISMATCH),
        (
            [_login(), _secret(server="http://localhost:8080")],
            DataHubCatalogSecretErrorCode.INVALID,
        ),
        (
            [_login(), b'{"data":{"data":{"format_version":1}}}'],
            DataHubCatalogSecretErrorCode.INVALID,
        ),
    ),
)
def test_remote_datahub_resolver_fails_closed_and_sanitized(
    tmp_path: Path,
    responses: list[bytes],
    expected: DataHubCatalogSecretErrorCode,
) -> None:
    resolver, _, _ = _resolver(tmp_path, list(responses))

    with pytest.raises(DataHubCatalogSecretResolutionError) as captured:
        resolver.resolve(_route())

    assert captured.value.code is expected
    assert captured.value.__cause__ is None
    rendered = f"{captured.value} {captured.value!r}"
    for private in (
        PRIVATE_BINDING,
        PRIVATE_TOKEN,
        PRIVATE_SERVER,
        CLIENT_TOKEN,
        "secrets.example.test",
        "tenant-connectors",
    ):
        assert private not in rendered


def test_remote_datahub_resolver_rejects_disabled_route_before_identity_or_provider(
    tmp_path: Path,
) -> None:
    resolver, identity, transport = _resolver(tmp_path)

    with pytest.raises(DataHubCatalogSecretResolutionError) as captured:
        resolver.resolve(_route(status=CatalogConnectionStatus.DISABLED))

    assert captured.value.code is DataHubCatalogSecretErrorCode.ROUTE_MISMATCH
    assert identity.calls == 0
    assert transport.calls == []


def test_remote_datahub_resolver_rejects_unversioned_route_before_provider_io(
    tmp_path: Path,
) -> None:
    resolver, identity, transport = _resolver(tmp_path)

    with pytest.raises(DataHubCatalogSecretResolutionError) as captured:
        resolver.resolve(_route(provider_secret_version=None, route_revision=999))

    assert captured.value.code is DataHubCatalogSecretErrorCode.ROUTE_MISMATCH
    assert identity.calls == 0
    assert transport.calls == []


def test_remote_datahub_resolver_requires_catalog_capability(tmp_path: Path) -> None:
    ca_bundle = (tmp_path / "ca.crt").resolve()
    ca_bundle.write_text("synthetic trust anchor", encoding="utf-8")
    ca_bundle.chmod(0o600)
    backend = VaultKvV2ConnectorSecretResolver(
        server="https://secrets.example.test",
        role="schemabridge-execution",
        kv_mount="tenant-connectors",
        capability=ConnectorSecretCapability.EXECUTION,
        identity=_Identity(),
        ca_bundle=ca_bundle,
        transport=_Transport([]),
    )

    with pytest.raises(ValueError, match="configuration is invalid"):
        VaultKvV2DataHubCatalogSecretResolver(backend)
