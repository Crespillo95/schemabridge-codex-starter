"""Composition checks for the isolated M25 catalog-indexer process."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

import schemabridge.bootstrap as bootstrap_module
from schemabridge.adapters.catalog.datahub_secrets import (
    OwnerOnlyDataHubCatalogSecretResolver,
)
from schemabridge.adapters.catalog.postgres_connector_routing import (
    PostgresCatalogConnectorRouteReader,
)
from schemabridge.adapters.catalog.postgres_refresh import (
    PostgresCatalogRefreshStore,
)
from schemabridge.adapters.catalog.routed_datahub import (
    RoutedCatalogSourceResolver,
    RoutedDataHubGraphQLCatalogSource,
)
from schemabridge.adapters.catalog.synthetic_source import (
    LazySyntheticCatalogSource,
)
from schemabridge.adapters.control_plane.postgres_pool import PostgresControlPool
from schemabridge.adapters.storage.postgres import ControlConnectionProvider
from schemabridge.application.catalog_indexer import RunOneCatalogRefresh
from schemabridge.bootstrap import (
    build_catalog_indexer,
    build_catalog_process_runtime,
)
from schemabridge.config import Settings
from schemabridge.domain.catalog_inventory import (
    CatalogConnectionId,
    CatalogConnectionKind,
)

ROOT = Path(__file__).resolve().parents[2]
CATALOG_DSN = "postgresql://schemabridge_catalog:catalog-secret@control.example.test/control"


class _LifecyclePool:
    def __init__(self) -> None:
        self.events: list[str] = []

    def open(self) -> None:
        self.events.append("opened")

    def close(self) -> None:
        self.events.append("closed")


def _catalog_settings() -> Settings:
    return Settings(
        _env_file=None,
        OPENAI_API_KEY=None,
        DATABASE_URL=None,
        SCHEMABRIDGE_COMPONENT="catalog",
        SCHEMABRIDGE_CONTROL_PLANE_MODE="postgres",
        SCHEMABRIDGE_CONTROL_CATALOG_DATABASE_URL=CATALOG_DSN,
        DATAHUB_GMS_TOKEN=None,
        SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY=ROOT / ".local/test-catalog-connectors",
        SCHEMABRIDGE_CATALOG_SYNTHETIC_ASSET_COUNTS={
            "small-catalog": 10,
            "large-catalog": 5_434,
        },
    )


def test_catalog_composition_uses_only_catalog_role_and_metadata_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks: list[dict[str, object]] = []
    provider = cast(ControlConnectionProvider, object())
    monkeypatch.setattr(
        bootstrap_module,
        "require_current_control_plane_schema",
        lambda **kwargs: checks.append(kwargs),
    )

    indexer = build_catalog_indexer(
        repository_root=ROOT,
        settings=_catalog_settings(),
        control_connection_provider=provider,
    )

    assert isinstance(indexer, RunOneCatalogRefresh)
    assert isinstance(indexer.refreshes, PostgresCatalogRefreshStore)
    assert isinstance(indexer.routes, PostgresCatalogConnectorRouteReader)
    assert indexer.refreshes.application_name == "schemabridge-control-catalog"
    assert indexer.routes.application_name == "schemabridge-control-catalog"
    assert indexer.refreshes.connection_provider is provider
    assert indexer.routes.connection_provider is provider
    sources = cast(RoutedCatalogSourceResolver, indexer.sources)
    datahub = cast(RoutedDataHubGraphQLCatalogSource, sources.datahub)
    synthetic = cast(
        LazySyntheticCatalogSource,
        sources.sources[CatalogConnectionKind.SYNTHETIC],
    )
    assert isinstance(datahub.secrets, OwnerOnlyDataHubCatalogSecretResolver)
    assert synthetic.specifications[CatalogConnectionId("small-catalog")].asset_count == 10
    assert synthetic.specifications[CatalogConnectionId("large-catalog")].asset_count == 5_434
    assert datahub.timeout_seconds == _catalog_settings().catalog_source_timeout_seconds
    assert indexer.source_operation_timeout.total_seconds() == (
        _catalog_settings().catalog_source_timeout_seconds
    )
    assert indexer.store_operation_timeout.total_seconds() == (
        _catalog_settings().control_pool_acquisition_timeout_seconds
        + _catalog_settings().statement_timeout_ms / 1_000
    )
    assert checks == [
        {
            "credential_kind": "catalog",
            "repository_root": ROOT,
            "settings": _catalog_settings(),
        }
    ]
    assert "test-catalog-connectors" not in repr(indexer)
    assert "catalog-secret" not in repr(indexer)
    assert not hasattr(indexer, "execute_query")
    assert not hasattr(indexer, "publish")


def test_catalog_readiness_checks_schema_without_composing_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _catalog_settings()
    pool = _LifecyclePool()
    checks: list[dict[str, object]] = []
    monkeypatch.setattr(
        bootstrap_module,
        "build_control_plane_pool",
        lambda **_kwargs: cast(PostgresControlPool, pool),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "require_current_control_plane_schema",
        lambda **kwargs: checks.append(kwargs),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "build_catalog_indexer",
        lambda **_kwargs: pytest.fail("readiness must not compose a catalog source"),
    )

    runtime = build_catalog_process_runtime(
        readiness_probe=True,
        repository_root=ROOT,
        settings=settings,
    )

    assert runtime.indexer is None
    assert runtime.control_pool is pool
    assert checks == [
        {
            "credential_kind": "catalog",
            "repository_root": ROOT,
            "settings": settings,
        }
    ]


def test_catalog_polling_runtime_injects_one_bounded_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _catalog_settings()
    pool = _LifecyclePool()
    indexer = cast(RunOneCatalogRefresh, object())
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(
        bootstrap_module,
        "build_control_plane_pool",
        lambda **_kwargs: cast(PostgresControlPool, pool),
    )

    def build(**kwargs: object) -> RunOneCatalogRefresh:
        captured.append(kwargs)
        return indexer

    monkeypatch.setattr(bootstrap_module, "build_catalog_indexer", build)

    runtime = build_catalog_process_runtime(
        repository_root=ROOT,
        settings=settings,
    )

    assert runtime.indexer is indexer
    assert runtime.control_pool is pool
    assert runtime.poll_interval_seconds == settings.catalog_poll_interval_ms / 1_000
    assert not runtime.stop_event.is_set()
    assert len(captured) == 1
    stop_requested = captured[0].pop("stop_requested")
    assert callable(stop_requested)
    assert not stop_requested()
    assert captured == [
        {
            "repository_root": ROOT,
            "settings": settings,
            "control_connection_provider": pool,
        }
    ]


def test_catalog_response_bound_matches_transport_bound() -> None:
    with pytest.raises(ValidationError):
        Settings(
            **{
                **_catalog_settings().model_dump(by_alias=True),
                "SCHEMABRIDGE_CATALOG_MAX_RESPONSE_BYTES": 8_388_609,
            }
        )


def test_catalog_runtime_identifiers_match_postgres_route_contract() -> None:
    payload = _catalog_settings().model_dump(by_alias=True)
    payload["SCHEMABRIDGE_CATALOG_INDEXER_ID"] = "catalog:indexer.prod"
    settings = Settings(**payload)
    assert settings.catalog_indexer_id == "catalog:indexer.prod"

    with pytest.raises(ValidationError):
        Settings(**{**payload, "SCHEMABRIDGE_CATALOG_INDEXER_ID": "1indexer"})


def test_catalog_rejects_the_retired_global_datahub_binding_selector() -> None:
    payload = _catalog_settings().model_dump(by_alias=True)

    with pytest.raises(ValidationError, match="is retired"):
        Settings(
            **{
                **payload,
                "SCHEMABRIDGE_CATALOG_DATAHUB_CREDENTIAL_BINDING_REF": ("vault:datahub.primary"),
            }
        )


def test_catalog_lease_covers_source_store_and_renewal_boundaries() -> None:
    payload = _catalog_settings().model_dump(by_alias=True)
    payload.update(
        {
            "SCHEMABRIDGE_CATALOG_LEASE_SECONDS": 26,
            "SCHEMABRIDGE_CATALOG_SOURCE_TIMEOUT_SECONDS": 15,
            "SCHEMABRIDGE_CONTROL_POOL_ACQUISITION_TIMEOUT_SECONDS": 2,
            "SCHEMABRIDGE_STATEMENT_TIMEOUT_MS": 5_000,
        }
    )

    with pytest.raises(ValidationError, match="source/store operations"):
        Settings(**payload)

    payload["SCHEMABRIDGE_CATALOG_LEASE_SECONDS"] = 27
    settings = Settings(**payload)
    assert settings.catalog_lease_seconds == 27
