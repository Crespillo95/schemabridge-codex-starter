"""Security and routing tests for per-connection DataHub catalog secrets."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from schemabridge.adapters.catalog.datahub_scroll import (
    DataHubCatalogSourceError,
    DataHubGraphQLRequest,
    DataHubGraphQLResponse,
)
from schemabridge.adapters.catalog.datahub_secrets import (
    DataHubCatalogSecretErrorCode,
    DataHubCatalogSecretResolutionError,
    OwnerOnlyDataHubCatalogSecretResolver,
    datahub_catalog_identity_fingerprint,
)
from schemabridge.adapters.catalog.routed_datahub import (
    RoutedDataHubGraphQLCatalogSource,
)
from schemabridge.application.ports.catalog_inventory import (
    ManagedCatalogConnectorRoute,
)
from schemabridge.domain.catalog_inventory import (
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogConnectionRoute,
    CatalogConnectionStatus,
    CatalogRefreshFailureCode,
    CatalogRefreshMode,
)

PRIVATE_BINDING = "vault:datahub:tenant-alpha"
PRIVATE_TOKEN = "private-datahub-token-alpha"
PRIVATE_SERVER = "https://datahub-alpha.example.test"
TARGET_FINGERPRINT = "a" * 64
CATALOG_IDENTITY_FINGERPRINT = datahub_catalog_identity_fingerprint(
    server=PRIVATE_SERVER,
    platform="postgres",
)


@dataclass
class _Transport:
    requests: list[DataHubGraphQLRequest] = field(default_factory=list)

    def execute(self, request: DataHubGraphQLRequest) -> DataHubGraphQLResponse:
        self.requests.append(request)
        return DataHubGraphQLResponse(
            status_code=200,
            body=b'{"data":{"scrollAcrossEntities":{"nextScrollId":null,'
            b'"count":0,"total":0,"searchResults":[]}}}',
        )


def _route(**updates: object) -> CatalogConnectionRoute:
    payload: dict[str, object] = {
        "workspace_id": "workspace_alpha",
        "connection_id": CatalogConnectionId("connection_alpha"),
        "kind": CatalogConnectionKind.DATAHUB_GRAPHQL,
        "environment": "PROD",
        "catalog_scope": "warehouse_alpha",
        "platform_instance": "warehouse-alpha",
        "catalog_identity_fingerprint": CATALOG_IDENTITY_FINGERPRINT,
        "status": CatalogConnectionStatus.ENABLED,
        "contract_version": 1,
        "route_revision": 4,
        "target_fingerprint": TARGET_FINGERPRINT,
    }
    payload.update(updates)
    return CatalogConnectionRoute.model_validate(payload)


def _managed(
    *,
    binding: str = PRIVATE_BINDING,
    **updates: object,
) -> ManagedCatalogConnectorRoute:
    return ManagedCatalogConnectorRoute(
        route=_route(**updates),
        credential_binding_ref=binding,
    )


def _secret_path(directory: Path, binding: str = PRIVATE_BINDING) -> Path:
    digest = hashlib.sha256(binding.encode()).hexdigest()
    return directory / f"{digest}.json"


def _prepare_directory(path: Path) -> None:
    path.chmod(0o700)


def _write_secret(
    directory: Path,
    *,
    binding: str = PRIVATE_BINDING,
    token: str = PRIVATE_TOKEN,
    server: str = PRIVATE_SERVER,
    platform: str = "postgres",
    mode: int = 0o600,
) -> Path:
    document = {
        "format_version": 1,
        "kind": "datahub_graphql",
        "server": server,
        "token": token,
        "platform": platform,
    }
    target = _secret_path(directory, binding)
    target.write_text(json.dumps(document), encoding="utf-8")
    target.chmod(mode)
    return target


def _write_duplicate_key_secret(directory: Path) -> Path:
    target = _secret_path(directory)
    target.write_text(
        '{"format_version":1,"kind":"datahub_graphql",'
        '"server":"https://datahub-alpha.example.test",'
        '"token":"first","token":"second","platform":"postgres"}',
        encoding="utf-8",
    )
    target.chmod(0o600)
    return target


def test_routed_datahub_resolves_secret_per_page_without_repr_or_error_leak(
    tmp_path: Path,
) -> None:
    _prepare_directory(tmp_path)
    _write_secret(tmp_path)
    resolver = OwnerOnlyDataHubCatalogSecretResolver(tmp_path)
    transport = _Transport()
    source = RoutedDataHubGraphQLCatalogSource(
        secrets=resolver,
        transport=transport,
        timeout_seconds=2.0,
        max_response_bytes=65_536,
    )

    page = source.read_managed_page(
        _managed(),
        mode=CatalogRefreshMode.FULL,
        checkpoint=None,
        page_size=7,
    )

    assert page.source_complete
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.endpoint == f"{PRIVATE_SERVER}/api/graphql"
    assert request.token == PRIVATE_TOKEN
    source_input = json.loads(request.body)["variables"]["input"]
    assert source_input["count"] == 7
    assert {
        "field": "platformInstance",
        "values": ["warehouse-alpha"],
        "condition": "EQUAL",
        "negated": False,
    } in source_input["orFilters"][0]["and"]
    rendered = repr(resolver) + repr(source) + repr(request) + repr(_route())
    for protected in (str(tmp_path), PRIVATE_SERVER, PRIVATE_TOKEN, PRIVATE_BINDING):
        assert protected not in rendered


def test_catalog_identity_fingerprint_uses_the_normalized_origin() -> None:
    assert (
        datahub_catalog_identity_fingerprint(
            server=f"{PRIVATE_SERVER}/",
            platform="postgres",
        )
        == CATALOG_IDENTITY_FINGERPRINT
    )
    assert (
        datahub_catalog_identity_fingerprint(
            server="https://datahub-beta.example.test",
            platform="postgres",
        )
        != CATALOG_IDENTITY_FINGERPRINT
    )


def test_routed_datahub_rejects_secret_retarget_before_the_next_page(
    tmp_path: Path,
) -> None:
    _prepare_directory(tmp_path)
    _write_secret(tmp_path)
    transport = _Transport()
    source = RoutedDataHubGraphQLCatalogSource(
        secrets=OwnerOnlyDataHubCatalogSecretResolver(tmp_path),
        transport=transport,
    )

    source.read_managed_page(
        _managed(),
        mode=CatalogRefreshMode.FULL,
        checkpoint=None,
        page_size=1,
    )
    _write_secret(
        tmp_path,
        server="https://datahub-beta.example.test",
        token="private-datahub-token-beta",
    )

    with pytest.raises(DataHubCatalogSourceError) as raised:
        source.read_managed_page(
            _managed(),
            mode=CatalogRefreshMode.FULL,
            checkpoint=None,
            page_size=1,
        )

    assert raised.value.failure_code is CatalogRefreshFailureCode.FINGERPRINT_MISMATCH
    assert len(transport.requests) == 1
    assert "datahub-beta" not in str(raised.value)


def test_two_equal_catalog_labels_resolve_distinct_binding_files_without_cross_read(
    tmp_path: Path,
) -> None:
    _prepare_directory(tmp_path)
    second_binding = "vault:datahub:tenant-beta"
    _write_secret(tmp_path)
    _write_secret(
        tmp_path,
        binding=second_binding,
        token="private-datahub-token-beta",
        server="https://datahub-beta.example.test",
    )
    transport = _Transport()
    source = RoutedDataHubGraphQLCatalogSource(
        secrets=OwnerOnlyDataHubCatalogSecretResolver(tmp_path),
        transport=transport,
    )

    source.read_managed_page(
        _managed(),
        mode=CatalogRefreshMode.FULL,
        checkpoint=None,
        page_size=1,
    )
    source.read_managed_page(
        _managed(
            binding=second_binding,
            workspace_id="workspace_beta",
            connection_id=CatalogConnectionId("connection_beta"),
            catalog_identity_fingerprint=datahub_catalog_identity_fingerprint(
                server="https://datahub-beta.example.test",
                platform="postgres",
            ),
            target_fingerprint="b" * 64,
        ),
        mode=CatalogRefreshMode.FULL,
        checkpoint=None,
        page_size=1,
    )

    assert [request.endpoint for request in transport.requests] == [
        "https://datahub-alpha.example.test/api/graphql",
        "https://datahub-beta.example.test/api/graphql",
    ]
    assert [request.token for request in transport.requests] == [
        PRIVATE_TOKEN,
        "private-datahub-token-beta",
    ]


def test_routed_datahub_rejects_non_postgresql_secret_platform_without_io(
    tmp_path: Path,
) -> None:
    _prepare_directory(tmp_path)
    _write_secret(tmp_path, platform="mysql")
    transport = _Transport()
    source = RoutedDataHubGraphQLCatalogSource(
        secrets=OwnerOnlyDataHubCatalogSecretResolver(tmp_path),
        transport=transport,
    )

    with pytest.raises(DataHubCatalogSourceError) as rejected:
        source.read_managed_page(
            _managed(),
            mode=CatalogRefreshMode.FULL,
            checkpoint=None,
            page_size=1,
        )

    assert rejected.value.failure_code is CatalogRefreshFailureCode.FINGERPRINT_MISMATCH
    assert transport.requests == []
    assert "mysql" not in str(rejected.value)


@pytest.mark.parametrize(
    ("prepare", "expected_code"),
    [
        (lambda path: None, DataHubCatalogSecretErrorCode.UNAVAILABLE),
        (
            lambda path: _write_secret(path, mode=0o644),
            DataHubCatalogSecretErrorCode.UNSAFE,
        ),
        (
            _write_duplicate_key_secret,
            DataHubCatalogSecretErrorCode.INVALID,
        ),
    ],
)
def test_owner_only_datahub_secret_failures_are_sanitized(
    tmp_path: Path,
    prepare: object,
    expected_code: DataHubCatalogSecretErrorCode,
) -> None:
    _prepare_directory(tmp_path)
    callback = prepare
    assert callable(callback)
    callback(tmp_path)
    resolver = OwnerOnlyDataHubCatalogSecretResolver(tmp_path)

    with pytest.raises(DataHubCatalogSecretResolutionError) as raised:
        resolver.resolve(_managed())

    assert raised.value.code is expected_code
    rendered = str(raised.value) + repr(raised.value)
    for protected in (str(tmp_path), PRIVATE_SERVER, PRIVATE_TOKEN, PRIVATE_BINDING):
        assert protected not in rendered
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    ("updates", "protected"),
    [
        ({"server": "http://datahub.example.test"}, "datahub.example.test"),
        ({"server": "https://user@datahub.example.test"}, "user@"),
        ({"server": "https://data hub.example.test"}, "data hub"),
        ({"token": "two words"}, "two words"),
        ({"platform": "Postgres"}, "Postgres"),
    ],
)
def test_invalid_datahub_secret_values_fail_before_transport_without_disclosure(
    tmp_path: Path,
    updates: dict[str, str],
    protected: str,
) -> None:
    _prepare_directory(tmp_path)
    _write_secret(
        tmp_path,
        server=updates.get("server", PRIVATE_SERVER),
        token=updates.get("token", PRIVATE_TOKEN),
        platform=updates.get("platform", "postgres"),
    )
    transport = _Transport()
    source = RoutedDataHubGraphQLCatalogSource(
        secrets=OwnerOnlyDataHubCatalogSecretResolver(tmp_path),
        transport=transport,
    )

    with pytest.raises(DataHubCatalogSourceError) as raised:
        source.read_managed_page(
            _managed(),
            mode=CatalogRefreshMode.FULL,
            checkpoint=None,
            page_size=1,
        )

    assert raised.value.failure_code is CatalogRefreshFailureCode.SOURCE_PERMISSION_DENIED
    assert protected not in f"{raised.value!s} {raised.value!r}"
    assert transport.requests == []


def test_datahub_secret_directory_and_symlink_file_fail_closed(tmp_path: Path) -> None:
    unsafe_directory = tmp_path / "unsafe"
    unsafe_directory.mkdir(mode=0o755)
    unsafe_directory.chmod(0o755)
    _write_secret(unsafe_directory)
    with pytest.raises(DataHubCatalogSecretResolutionError) as unsafe:
        OwnerOnlyDataHubCatalogSecretResolver(unsafe_directory).resolve(_managed())
    assert unsafe.value.code is DataHubCatalogSecretErrorCode.UNSAFE

    safe_directory = tmp_path / "safe"
    safe_directory.mkdir(mode=0o700)
    safe_directory.chmod(0o700)
    target = safe_directory / "actual.json"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o600)
    _secret_path(safe_directory).symlink_to(target)
    with pytest.raises(DataHubCatalogSecretResolutionError) as symlink:
        OwnerOnlyDataHubCatalogSecretResolver(safe_directory).resolve(_managed())
    assert symlink.value.code is DataHubCatalogSecretErrorCode.UNSAFE


def test_routed_datahub_rejects_legacy_or_wrong_kind_route_before_secret_or_transport(
    tmp_path: Path,
) -> None:
    _prepare_directory(tmp_path)
    _write_secret(tmp_path)
    transport = _Transport()
    source = RoutedDataHubGraphQLCatalogSource(
        secrets=OwnerOnlyDataHubCatalogSecretResolver(tmp_path),
        transport=transport,
    )

    with pytest.raises(DataHubCatalogSourceError) as legacy:
        source.read_managed_page(
            _managed(
                contract_version=None,
                route_revision=None,
                target_fingerprint=None,
                catalog_identity_fingerprint=None,
            ),
            mode=CatalogRefreshMode.FULL,
            checkpoint=None,
            page_size=1,
        )
    with pytest.raises(DataHubCatalogSourceError) as wrong_kind:
        source.read_managed_page(
            _managed(kind=CatalogConnectionKind.SYNTHETIC),
            mode=CatalogRefreshMode.FULL,
            checkpoint=None,
            page_size=1,
        )

    assert legacy.value.failure_code is CatalogRefreshFailureCode.SOURCE_PERMISSION_DENIED
    assert wrong_kind.value.failure_code is CatalogRefreshFailureCode.SOURCE_PERMISSION_DENIED
    assert transport.requests == []
