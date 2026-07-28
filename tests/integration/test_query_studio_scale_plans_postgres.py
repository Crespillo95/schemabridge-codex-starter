"""Real M27 PostgreSQL search-plan proof at 5,434 assets and 41,028 fields."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from time import perf_counter

import psycopg
import pytest
from tests.integration import test_catalog_scale_postgres as scale_support
from tests.integration.connector_target_support import ensure_catalog_connector_target

from schemabridge.adapters.catalog.postgres_connector_routing import (
    PostgresCatalogConnectorRouteReader,
)
from schemabridge.adapters.catalog.postgres_inventory import (
    PostgresCatalogConnectionStore,
)
from schemabridge.adapters.catalog.postgres_physical_discovery import (
    PostgresPhysicalFieldDiscovery,
)
from schemabridge.adapters.catalog.postgres_refresh import (
    PostgresCatalogRefreshStore,
)
from schemabridge.adapters.catalog.synthetic_source import (
    LazySyntheticCatalogSource,
    SyntheticCatalogSpecification,
)
from schemabridge.application.catalog_indexer import (
    CatalogIndexerIterationOutcome,
    KindCatalogSourceResolver,
    RunOneCatalogRefresh,
)
from schemabridge.application.catalog_inventory import (
    RegisterCatalogConnection,
    RequestCatalogRefresh,
)
from schemabridge.domain.catalog_inventory import (
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogRefreshMode,
)
from schemabridge.domain.query_studio import (
    DescriptionQuery,
    PhysicalFieldDiscoveryRequest,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope

pytestmark = [pytest.mark.integration, pytest.mark.scale]

scale_database = scale_support.scale_database

_ASSET_COUNT = 5_434
_FIELD_COUNT = 41_028
_PAGE_SIZE = 17
_STATEMENT_TIMEOUT_MS = 5_000
_CURSOR_SIGNING_KEY = b"m27-real-scale-plan-cursor-key-with-diversity-123"


def _execution_time_ms(plan: object) -> float:
    assert isinstance(plan, list) and len(plan) == 1
    root = plan[0]
    assert isinstance(root, dict)
    value = root.get("Execution Time")
    assert isinstance(value, int | float)
    return float(value)


def _top_actual_rows(plan: object) -> int:
    assert isinstance(plan, list) and len(plan) == 1
    root = plan[0]
    assert isinstance(root, dict)
    operation = root.get("Plan")
    assert isinstance(operation, dict)
    rows = operation.get("Actual Rows")
    assert isinstance(rows, int)
    return rows


def _explain(
    connection: psycopg.Connection[tuple[object, ...]],
    query: str,
    parameters: tuple[object, ...],
) -> object:
    row = connection.execute(
        "EXPLAIN (ANALYZE TRUE, FORMAT JSON, BUFFERS TRUE, COSTS TRUE, TIMING FALSE) " + query,
        parameters,
    ).fetchone()
    assert row is not None and len(row) == 1
    return row[0]


def test_m27_text_and_locator_plans_are_indexed_bounded_and_under_timeout(
    scale_database: scale_support._DatabaseUrls,
    record_property: Callable[[str, object], None],
) -> None:
    """Exercise the production discovery function and every reviewed catalog index."""

    workspace_id = "workspace_m27_exact_scale_plans"
    connection_id = CatalogConnectionId("connection_m27_exact_scale")
    scope = SemanticRegistryScope(
        workspace_id=workspace_id,
        catalog_scope="synthetic-scale",
        registry_id="synthetic_enterprise",
    )
    now = datetime.now(UTC)
    scale_support._provision_policy(scale_database.migrator, workspace_id)

    api_pool = scale_support._pool(
        scale_database.api,
        application_name="schemabridge-control-api",
    )
    catalog_pool = scale_support._pool(
        scale_database.catalog,
        application_name="schemabridge-control-catalog",
    )
    with api_pool, catalog_pool:
        api_connections = PostgresCatalogConnectionStore(
            scale_database.api,
            application_name="schemabridge-control-api",
            connection_provider=api_pool,
        )
        api_refreshes = PostgresCatalogRefreshStore(
            scale_database.api,
            application_name="schemabridge-control-api",
            connection_provider=api_pool,
        )
        catalog_refreshes = PostgresCatalogRefreshStore(
            scale_database.catalog,
            connection_provider=catalog_pool,
        )
        principal = scale_support._principal(workspace_id, now)
        register = RegisterCatalogConnection(api_connections, scale_support._Clock(now))
        request = RequestCatalogRefresh(
            api_connections,
            api_refreshes,
            scale_support._Clock(now),
        )
        scale_support._register_connection(
            register,
            principal,
            connection_id,
        )
        ensure_catalog_connector_target(
            scale_database.migrator,
            workspace_id=workspace_id,
            connection_id=connection_id,
        )
        routes = PostgresCatalogConnectorRouteReader(
            scale_database.catalog,
            connection_provider=catalog_pool,
        )

        source = scale_support._ObservedSyntheticSource(
            delegate=LazySyntheticCatalogSource(
                {
                    connection_id: SyntheticCatalogSpecification(
                        asset_count=_ASSET_COUNT,
                        minimum_fields=1,
                        maximum_fields=13,
                        wide_asset_every=40,
                        wide_field_count=29,
                    )
                }
            ),
            catalog_pool=catalog_pool,
        )
        indexer = RunOneCatalogRefresh(
            refreshes=catalog_refreshes,
            routes=routes,
            sources=KindCatalogSourceResolver({CatalogConnectionKind.SYNTHETIC: source}),
            capability_factory=scale_support._CapabilityFactory(),
            indexer_id="m27_scale_plan_indexer",
            lease_duration=timedelta(minutes=2),
            page_size=scale_support.SOURCE_PAGE_SIZE,
        )
        request.execute(
            principal,
            connection_id,
            mode=CatalogRefreshMode.FULL,
            confirmation="REQUEST CATALOG REFRESH",
            idempotency_key="m27-scale-plan-refresh-0001",
        )
        refresh_started = perf_counter()
        refresh = indexer.execute()
        refresh_elapsed = perf_counter() - refresh_started

    assert refresh.outcome is CatalogIndexerIterationOutcome.COMPLETED
    assert (refresh.asset_count, refresh.field_count) == (_ASSET_COUNT, _FIELD_COUNT)
    assert (_ASSET_COUNT, _FIELD_COUNT) == (
        scale_support.LARGE_ASSET_COUNT,
        scale_support.EXPECTED_LARGE_FIELD_COUNT,
    )
    # This test owns the Query Studio search-plan budget. The authoritative 60-second
    # refresh assertion for this exact cardinality remains in test_catalog_scale_postgres;
    # retain this second setup duration only as diagnostic evidence because it runs after
    # external-service integrations in the complete suite.
    record_property("query_studio_fixture_refresh_seconds", round(refresh_elapsed, 6))
    source_key = (workspace_id, connection_id.root, CatalogRefreshMode.FULL)
    assert len(source.page_lengths[source_key]) == 109
    assert sum(source.page_lengths[source_key]) == _ASSET_COUNT

    physical = PostgresPhysicalFieldDiscovery.from_signing_key(
        dsn=scale_database.runtime,
        cursor_signing_key=_CURSOR_SIGNING_KEY,
    )
    cardinality = physical.inspect_cardinality(scope)
    assert (
        cardinality.connection_count,
        cardinality.asset_count,
        cardinality.field_count,
    ) == (1, _ASSET_COUNT, _FIELD_COUNT)

    text_started = perf_counter()
    text_page = physical.search(
        PhysicalFieldDiscoveryRequest(
            scope=scope,
            query=DescriptionQuery("contract identifier preserving leading zeroes"),
            page_size=_PAGE_SIZE,
        )
    )
    text_elapsed_ms = (perf_counter() - text_started) * 1_000
    assert len(text_page.items) == _PAGE_SIZE
    assert text_page.next_cursor is not None
    assert text_elapsed_ms < _STATEMENT_TIMEOUT_MS

    exact_started = perf_counter()
    exact_page = physical.search(
        PhysicalFieldDiscoveryRequest(
            scope=scope,
            query=DescriptionQuery("contract_id"),
            page_size=_PAGE_SIZE,
        )
    )
    exact_elapsed_ms = (perf_counter() - exact_started) * 1_000
    assert len(exact_page.items) == _PAGE_SIZE
    assert exact_page.next_cursor is not None
    assert exact_elapsed_ms < _STATEMENT_TIMEOUT_MS

    with psycopg.connect(scale_database.migrator) as connection:
        connection.execute("ANALYZE schemabridge_control.catalog_assets")
        connection.execute("ANALYZE schemabridge_control.catalog_fields")
        representative = connection.execute(
            """
            SELECT
                asset.asset_key,
                asset.qualified_name,
                field.field_key,
                field.field_path
            FROM schemabridge_control.catalog_assets AS asset
            JOIN schemabridge_control.catalog_fields AS field
              ON field.workspace_id = asset.workspace_id
             AND field.connection_id = asset.connection_id
             AND field.generation = asset.generation
             AND field.asset_key = asset.asset_key
            WHERE asset.workspace_id = %s
              AND asset.connection_id = %s
              AND asset.generation = 1
            ORDER BY asset.asset_key, field.field_key
            LIMIT 1
            """,
            (workspace_id, connection_id.root),
        ).fetchone()
        assert representative is not None
        asset_key, qualified_name, field_key, field_path = representative

        text_plan = _explain(
            connection,
            """
            SELECT field.field_key
            FROM schemabridge_control.catalog_fields AS field
            WHERE field.workspace_id = %s
              AND field.search_document @@ plainto_tsquery(
                    'pg_catalog.simple'::regconfig,
                    %s
              )
            LIMIT %s
            """,
            (
                workspace_id,
                "contract identifier preserving leading zeroes",
                _PAGE_SIZE + 1,
            ),
        )
        exact_plan = _explain(
            connection,
            """
            SELECT field.asset_key, field.field_key
            FROM schemabridge_control.catalog_fields AS field
            WHERE field.workspace_id = %s
              AND field.connection_id = %s
              AND field.generation = 1
              AND lower(field.field_name) = 'contract_id'
            ORDER BY field.asset_key, field.field_key
            LIMIT %s
            """,
            (workspace_id, connection_id.root, _PAGE_SIZE + 1),
        )
        asset_locator_plan = _explain(
            connection,
            """
            SELECT asset.asset_key
            FROM schemabridge_control.catalog_assets AS asset
            WHERE schemabridge_control.semantic_catalog_asset_locator_key(
                      asset.workspace_id,
                      asset.connection_id,
                      asset.generation,
                      asset.qualified_name
                  )
                  = schemabridge_control.semantic_catalog_asset_locator_key(
                      %s, %s, 1, %s
                  )
              AND asset.workspace_id = %s
              AND asset.connection_id = %s
              AND asset.generation = 1
              AND asset.qualified_name = %s
            LIMIT 1
            """,
            (
                workspace_id,
                connection_id.root,
                qualified_name,
                workspace_id,
                connection_id.root,
                qualified_name,
            ),
        )
        field_locator_plan = _explain(
            connection,
            """
            SELECT field.field_key
            FROM schemabridge_control.catalog_fields AS field
            WHERE schemabridge_control.semantic_catalog_field_locator_key(
                      field.workspace_id,
                      field.connection_id,
                      field.generation,
                      field.asset_key,
                      field.field_key
                  )
                  = schemabridge_control.semantic_catalog_field_locator_key(
                      %s, %s, 1, %s, %s
                  )
              AND field.workspace_id = %s
              AND field.connection_id = %s
              AND field.generation = 1
              AND field.asset_key = %s
              AND field.field_key = %s
              AND field.field_path = %s
            LIMIT 1
            """,
            (
                workspace_id,
                connection_id.root,
                asset_key,
                field_key,
                workspace_id,
                connection_id.root,
                asset_key,
                field_key,
                field_path,
            ),
        )

    scale_support._assert_plan_uses_index(
        text_plan,
        expected_index="catalog_fields_search_idx",
        allow_bounded_sort=True,
        require_actual_rows=True,
    )
    scale_support._assert_plan_uses_index(
        exact_plan,
        expected_index="catalog_fields_physical_discovery_exact_idx",
        require_actual_rows=True,
    )
    scale_support._assert_plan_uses_index(
        asset_locator_plan,
        expected_index="catalog_assets_semantic_lookup_idx",
        allow_bounded_sort=True,
        require_actual_rows=True,
    )
    scale_support._assert_plan_uses_index(
        field_locator_plan,
        expected_index="catalog_fields_semantic_lookup_idx",
        allow_bounded_sort=True,
        require_actual_rows=True,
    )
    for plan, maximum_rows in (
        (text_plan, _PAGE_SIZE + 1),
        (exact_plan, _PAGE_SIZE + 1),
        (asset_locator_plan, 1),
        (field_locator_plan, 1),
    ):
        assert _top_actual_rows(plan) <= maximum_rows
        assert _execution_time_ms(plan) < _STATEMENT_TIMEOUT_MS

    with psycopg.connect(scale_database.runtime) as connection:
        function_plan = _explain(
            connection,
            """
            SELECT *
            FROM schemabridge_control.discover_physical_fields(
                %s::varchar,
                %s::varchar,
                %s::varchar,
                %s,
                %s::varchar,
                %s::integer,
                NULL::integer,
                NULL::varchar,
                NULL,
                NULL
            )
            """,
            (
                workspace_id,
                scope.catalog_scope,
                scope.registry_id,
                cardinality.catalog_generation_vector_fingerprint,
                "contract identifier preserving leading zeroes",
                _PAGE_SIZE,
            ),
        )
    assert _top_actual_rows(function_plan) == _PAGE_SIZE + 1
    assert _execution_time_ms(function_plan) < _STATEMENT_TIMEOUT_MS
