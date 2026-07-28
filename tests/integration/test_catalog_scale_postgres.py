"""End-to-end PostgreSQL scale proof for the dynamic M25 catalog inventory."""

from __future__ import annotations

import hashlib
import os
import platform
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from tests.integration.connector_target_support import ensure_catalog_connector_target

from schemabridge.adapters.catalog.postgres_connector_routing import (
    PostgresCatalogConnectorRouteReader,
)
from schemabridge.adapters.catalog.postgres_inventory import (
    PostgresCatalogConnectionStore,
    PostgresCatalogInventoryReader,
    PostgresTenantCapacityStore,
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
    SyntheticDeltaSpecification,
)
from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.adapters.control_plane.postgres_pool import (
    ControlPoolSettings,
    PostgresControlPool,
)
from schemabridge.application.catalog_indexer import (
    CatalogIndexerIterationOutcome,
    KindCatalogSourceResolver,
    RunOneCatalogRefresh,
)
from schemabridge.application.catalog_inventory import (
    CatalogUseCaseError,
    CatalogUseCaseErrorCode,
    RegisterCatalogConnection,
    RequestCatalogRefresh,
)
from schemabridge.application.ports.catalog_inventory import (
    CatalogInventoryError,
    CatalogInventoryErrorCode,
)
from schemabridge.domain.catalog_inventory import (
    CatalogAssetFilter,
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogConnectionRoute,
    CatalogRefreshMode,
    CatalogRefreshStatus,
    CatalogSourcePage,
    InventoryPageKey,
)
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)
from schemabridge.domain.query_studio import (
    DescriptionQuery,
    PhysicalDiscoveryStatus,
    PhysicalFieldDiscoveryRequest,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope

pytestmark = [pytest.mark.integration, pytest.mark.scale]

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations/control_plane"
ADMIN_DSN = "postgresql://postgres:postgres@127.0.0.1:55434/postgres"
ROLES = (
    "schemabridge_migrator",
    "schemabridge_runtime",
    "schemabridge_reconciler",
    "schemabridge_api",
    "schemabridge_worker",
    "schemabridge_catalog",
)
SMALL_ASSET_COUNT = 10
LARGE_ASSET_COUNT = 5_434
LARGE_PRIMARY_ASSET_COUNT = 5_424
LARGE_SECONDARY_ASSET_COUNT = 10
LARGE_CONNECTION_ASSET_COUNTS = (
    LARGE_PRIMARY_ASSET_COUNT,
    LARGE_SECONDARY_ASSET_COUNT,
)
LARGE_WIDE_ASSET_EVERY = 997
LARGE_WIDE_FIELD_COUNT = 64
EXPECTED_LARGE_FIELD_COUNT = 41_028
DELTA_UPDATE_COUNT = 100
DELTA_ADDITION_COUNT = 23
DELTA_DELETION_COUNT = 14
EXPECTED_DELTA_CONNECTION_ASSET_COUNT = (
    LARGE_PRIMARY_ASSET_COUNT + DELTA_ADDITION_COUNT - DELTA_DELETION_COUNT
)
EXPECTED_DELTA_ASSET_COUNT = 5_443
SOURCE_PAGE_SIZE = 50
MAX_FULL_REFRESH_SECONDS = 60.0
PHYSICAL_DISCOVERY_CURSOR_KEY = b"m27-physical-discovery-scale-cursor-key-with-diversity-123"


@dataclass(frozen=True, slots=True)
class _DatabaseUrls:
    database: str
    migrator: str
    runtime: str
    reconciler: str
    api: str
    worker: str
    catalog: str


@dataclass(frozen=True, slots=True)
class _Clock:
    instant: datetime

    def now(self) -> datetime:
        return self.instant


@dataclass(slots=True)
class _CapabilityFactory:
    sequence: int = 0

    def __call__(self) -> str:
        self.sequence += 1
        return f"catalog-scale-capability-{self.sequence:08d}-ABCDEFGHIJKLMNOPQRSTUVWXYZ-0123456789"


@dataclass(slots=True)
class _ObservedSyntheticSource:
    """Assert that every prior source page is durable before the next read."""

    delegate: LazySyntheticCatalogSource
    catalog_pool: PostgresControlPool = field(repr=False)
    page_lengths: dict[tuple[str, str, CatalogRefreshMode], list[int]] = field(default_factory=dict)

    @property
    def source_label(self) -> str:
        return self.delegate.source_label

    def read_page(
        self,
        route: CatalogConnectionRoute,
        *,
        mode: CatalogRefreshMode,
        checkpoint: str | None,
        page_size: int,
    ) -> CatalogSourcePage:
        key = (route.workspace_id, route.connection_id.root, mode)
        observed_lengths = self.page_lengths.setdefault(key, [])
        pages_already_requested = len(observed_lengths)
        if pages_already_requested:
            with self.catalog_pool.connection() as connection:
                durable = connection.execute(
                    """
                    SELECT source_page_number, status
                    FROM schemabridge_control.catalog_refresh_runs
                    WHERE workspace_id = %s
                      AND connection_id = %s
                      AND refresh_mode = %s
                      AND status = 'staging'
                    ORDER BY target_generation DESC
                    LIMIT 1
                    """,
                    (route.workspace_id, route.connection_id.root, mode.value),
                ).fetchone()
            assert durable == (pages_already_requested, "staging")

        page = self.delegate.read_page(
            route,
            mode=mode,
            checkpoint=checkpoint,
            page_size=page_size,
        )
        assert page.sequence == pages_already_requested + 1
        assert 1 <= len(page.changes) <= page_size <= SOURCE_PAGE_SIZE
        observed_lengths.append(len(page.changes))
        return page


def _admin_dsn() -> str:
    return os.environ.get(
        "SCHEMABRIDGE_TEST_CONTROL_ADMIN_DATABASE_URL",
        ADMIN_DSN,
    )


def _role_dsn(role: str, database: str) -> str:
    return f"postgresql://{role}:{role}@127.0.0.1:55434/{database}"


def _database_urls(database: str) -> _DatabaseUrls:
    return _DatabaseUrls(
        database=database,
        migrator=_role_dsn("schemabridge_migrator", database),
        runtime=_role_dsn("schemabridge_runtime", database),
        reconciler=_role_dsn("schemabridge_reconciler", database),
        api=_role_dsn("schemabridge_api", database),
        worker=_role_dsn("schemabridge_worker", database),
        catalog=_role_dsn("schemabridge_catalog", database),
    )


@pytest.fixture(scope="module")
def scale_database() -> Iterator[_DatabaseUrls]:
    database = f"schemabridge_catalog_scale_{uuid4().hex[:12]}"
    urls = _database_urls(database)
    with psycopg.connect(_admin_dsn(), autocommit=True) as connection:
        available_roles = {
            row[0]
            for row in connection.execute(
                """
                SELECT rolname
                FROM pg_catalog.pg_roles
                WHERE rolname = ANY(%s)
                """,
                (list(ROLES),),
            )
        }
        missing_roles = sorted(set(ROLES) - available_roles)
        if missing_roles:
            pytest.fail("M25 control-plane roles are unavailable")
        connection.execute(
            sql.SQL("CREATE DATABASE {} OWNER schemabridge_migrator").format(
                sql.Identifier(database)
            )
        )
        connection.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database))
        )
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(database),
                sql.SQL(", ").join(sql.Identifier(role) for role in ROLES),
            )
        )
    try:
        migrated = PostgresControlPlaneMigrator(urls.migrator, MIGRATIONS).migrate()
        assert migrated.inspection.current_version == 9
        with psycopg.connect(urls.migrator, autocommit=True) as connection:
            connection.execute(
                """
                ALTER TABLE schemabridge_control.catalog_assets
                    SET (autovacuum_enabled = false)
                """
            )
            connection.execute(
                """
                ALTER TABLE schemabridge_control.catalog_fields
                    SET (autovacuum_enabled = false)
                """
            )
        yield urls
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )


def _pool(
    dsn: str,
    *,
    application_name: str,
) -> PostgresControlPool:
    return PostgresControlPool(
        ControlPoolSettings(
            dsn=dsn,
            application_name=application_name,
            min_size=1,
            max_size=2,
            max_waiting=4,
            acquisition_timeout_seconds=5.0,
            startup_timeout_seconds=15.0,
            statement_timeout_ms=60_000,
        )
    )


def _provision_policy(migrator_dsn: str, workspace_id: str) -> None:
    with psycopg.connect(migrator_dsn) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.tenant_capacity_policies (
                workspace_id, connection_limit, asset_limit, field_limit,
                api_requests_per_minute, api_window_seconds,
                nonterminal_job_limit,
                catalog_cursor_ttl_seconds, generation_retention_seconds,
                version, updated_by, created_at, updated_at
            ) VALUES (
                %s, 10, 10000, 100000, 1000, 60, 100, 900, 1800,
                1, 'operator_scale_test', clock_timestamp(), clock_timestamp()
            )
            """,
            (workspace_id,),
        )


def _principal(workspace_id: str, now: datetime) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        actor_id="actor_scale_platform_admin",
        workspace_id=workspace_id,
        roles=frozenset({IdentityRole.PLATFORM_ADMIN}),
        authentication_method=AuthenticationMethod.LOCAL_DEMO,
        authenticated_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
    )


def _register_connection(
    use_case: RegisterCatalogConnection,
    principal: AuthenticatedPrincipal,
    connection_id: CatalogConnectionId,
) -> None:
    result = use_case.execute(
        principal,
        connection_id=connection_id,
        display_name=f"Synthetic {connection_id.root}",
        kind=CatalogConnectionKind.SYNTHETIC,
        environment="PROD",
        catalog_scope="synthetic-scale",
        confirmation="REGISTER CATALOG CONNECTION",
        idempotency_key=f"register-{connection_id.root}-0001",
    )
    assert not result.replayed
    assert result.connection.asset_count == 0
    assert result.connection.field_count == 0


def _synthetic_field_count(index: int, *, wide: bool = False) -> int:
    if wide and (index + 1) % LARGE_WIDE_ASSET_EVERY == 0:
        return LARGE_WIDE_FIELD_COUNT
    return 3 + index % 10


def _expected_field_count(asset_count: int, *, wide: bool = False) -> int:
    return sum(_synthetic_field_count(index, wide=wide) for index in range(asset_count))


def _collect_plan_evidence(
    value: object,
    *,
    node_types: set[str],
    index_names: set[str],
) -> None:
    if isinstance(value, dict):
        node_type = value.get("Node Type")
        index_name = value.get("Index Name")
        if isinstance(node_type, str):
            node_types.add(node_type)
        if isinstance(index_name, str):
            index_names.add(index_name)
        for child in value.values():
            _collect_plan_evidence(
                child,
                node_types=node_types,
                index_names=index_names,
            )
    elif isinstance(value, list):
        for child in value:
            _collect_plan_evidence(
                child,
                node_types=node_types,
                index_names=index_names,
            )


def _assert_plan_uses_index(
    plan: object,
    *,
    expected_index: str,
    allow_bounded_sort: bool = False,
    require_actual_rows: bool = False,
) -> None:
    node_types: set[str] = set()
    index_names: set[str] = set()
    _collect_plan_evidence(
        plan,
        node_types=node_types,
        index_names=index_names,
    )
    assert expected_index in index_names, (
        f"{expected_index} absent from reviewed plan; observed={sorted(index_names)}"
    )
    forbidden_nodes = {"Parallel Seq Scan", "Seq Scan"}
    if not allow_bounded_sort:
        forbidden_nodes.update({"Incremental Sort", "Sort"})
    assert node_types.isdisjoint(forbidden_nodes), (
        f"forbidden node in {expected_index} plan: {sorted(node_types)}"
    )
    if require_actual_rows:
        assert _index_actual_rows(plan, expected_index=expected_index) > 0, (
            f"{expected_index} was selected but returned no representative row"
        )


def _index_actual_rows(value: object, *, expected_index: str) -> int:
    if isinstance(value, dict):
        own_rows = value.get("Actual Rows", 0) if value.get("Index Name") == expected_index else 0
        return int(own_rows) + sum(
            _index_actual_rows(child, expected_index=expected_index) for child in value.values()
        )
    if isinstance(value, list):
        return sum(_index_actual_rows(child, expected_index=expected_index) for child in value)
    return 0


def _assert_inventory_index_plans(
    api_pool: PostgresControlPool,
    *,
    workspace_id: str,
    connection_id: CatalogConnectionId,
    generation: int,
) -> None:
    asset_key = hashlib.sha256(b"synthetic-asset-00000000").hexdigest()
    with api_pool.connection() as connection:
        asset_row = connection.execute(
            """
            EXPLAIN (
                FORMAT JSON, ANALYZE TRUE, BUFFERS TRUE, COSTS TRUE, TIMING FALSE
            )
            SELECT asset_id, qualified_name, asset_sort_key, asset_key
            FROM schemabridge_control.catalog_assets
            WHERE workspace_id = %s
              AND connection_id = %s
              AND generation = %s
              AND (asset_sort_key, asset_key) > (%s, %s)
            ORDER BY asset_sort_key, asset_key
            LIMIT %s
            """,
            (
                workspace_id,
                connection_id.root,
                generation,
                "",
                "",
                18,
            ),
        ).fetchone()
        field_row = connection.execute(
            """
            EXPLAIN (
                FORMAT JSON, ANALYZE TRUE, BUFFERS TRUE, COSTS TRUE, TIMING FALSE
            )
            SELECT field_path, field_sort_key, field_key
            FROM schemabridge_control.catalog_fields
            WHERE workspace_id = %s
              AND connection_id = %s
              AND generation = %s
              AND asset_key = %s
              AND (field_sort_key, field_key) > (%s, %s)
            ORDER BY field_sort_key, field_key
            LIMIT %s
            """,
            (
                workspace_id,
                connection_id.root,
                generation,
                asset_key,
                "",
                "",
                18,
            ),
        ).fetchone()
        asset_exact_row = connection.execute(
            """
            EXPLAIN (
                FORMAT JSON, ANALYZE TRUE, BUFFERS TRUE, COSTS TRUE, TIMING FALSE
            )
            SELECT asset_id
            FROM schemabridge_control.catalog_assets
            WHERE workspace_id = %s
              AND connection_id = %s
              AND generation = %s
              AND schema_name = %s
              AND table_name = %s
            ORDER BY asset_key
            LIMIT 1
            """,
            (
                workspace_id,
                connection_id.root,
                generation,
                "bank",
                "account_holders_00000000",
            ),
        ).fetchone()
        field_exact_row = connection.execute(
            """
            EXPLAIN (
                FORMAT JSON, ANALYZE TRUE, BUFFERS TRUE, COSTS TRUE, TIMING FALSE
            )
            SELECT asset_key, field_key
            FROM schemabridge_control.catalog_fields
            WHERE workspace_id = %s
              AND connection_id = %s
              AND generation = %s
              AND lower(field_name) = %s
            ORDER BY field_key
            LIMIT 1
            """,
            (
                workspace_id,
                connection_id.root,
                generation,
                "contract_id",
            ),
        ).fetchone()
    assert asset_row is not None
    assert field_row is not None
    assert asset_exact_row is not None
    assert field_exact_row is not None
    for plan, expected_index in (
        (asset_row[0], "catalog_assets_keyset_idx"),
        (field_row[0], "catalog_fields_keyset_idx"),
        (asset_exact_row[0], "catalog_assets_exact_lookup_idx"),
        (field_exact_row[0], "catalog_fields_exact_lookup_idx"),
    ):
        _assert_plan_uses_index(
            plan,
            expected_index=expected_index,
            require_actual_rows=True,
        )


def _assert_control_index_plans(
    scale_database: _DatabaseUrls,
    *,
    workspace_id: str,
    connection_id: CatalogConnectionId,
) -> None:
    """Review low-cardinality control indexes with sequential scans disabled locally.

    The scale fixture makes inventory plans naturally selective. Control tables deliberately
    contain only a few rows in this isolated test. The caller proves the partial unique active
    index behavior with a real conflicting request and leaves one legitimate request pending for
    the claim plan. Disabling sequential scans for the remaining read-only EXPLAIN statements
    proves each operational predicate has a usable matching index without manufacturing thousands
    of unrelated control-plane rows.
    """

    digest = hashlib.sha256(b"scale-principal").hexdigest()
    reviewed: list[tuple[object, str]] = []
    with psycopg.connect(scale_database.catalog) as connection, connection.transaction():
        connection.execute("SET LOCAL enable_seqscan = off")
        active_index_contract = connection.execute(
            """
            SELECT index_metadata.indisunique,
                   index_metadata.indisvalid,
                   index_metadata.indisready,
                   pg_catalog.pg_get_indexdef(index_metadata.indexrelid),
                   pg_catalog.pg_get_expr(
                       index_metadata.indpred,
                       index_metadata.indrelid
                   )
            FROM pg_catalog.pg_index AS index_metadata
            JOIN pg_catalog.pg_class AS index_relation
              ON index_relation.oid = index_metadata.indexrelid
            JOIN pg_catalog.pg_class AS table_relation
              ON table_relation.oid = index_metadata.indrelid
            JOIN pg_catalog.pg_namespace AS table_namespace
              ON table_namespace.oid = table_relation.relnamespace
            WHERE table_namespace.nspname = 'schemabridge_control'
              AND table_relation.relname = 'catalog_refresh_runs'
              AND index_relation.relname = 'catalog_refresh_one_active_idx'
            """
        ).fetchone()
        connection.execute("SET LOCAL enable_sort = off")
        claim = connection.execute(
            """
            EXPLAIN (
                FORMAT JSON, ANALYZE TRUE, BUFFERS TRUE, COSTS TRUE, TIMING FALSE
            )
            SELECT refresh.workspace_id, refresh.connection_id, refresh.refresh_id
            FROM schemabridge_control.catalog_refresh_runs AS refresh
            JOIN schemabridge_control.catalog_connections AS catalog_connection
              ON catalog_connection.workspace_id = refresh.workspace_id
             AND catalog_connection.connection_id = refresh.connection_id
            WHERE refresh.status = 'requested'
              AND catalog_connection.status = 'enabled'
            ORDER BY refresh.requested_at,
                     refresh.workspace_id,
                     refresh.connection_id,
                     refresh.refresh_id
            LIMIT 1
            """
        ).fetchone()
        connection.execute("SET LOCAL enable_sort = on")
        connection.execute("SET LOCAL enable_sort = off")
        expired = connection.execute(
            """
            EXPLAIN (
                FORMAT JSON, ANALYZE TRUE, BUFFERS TRUE, COSTS TRUE, TIMING FALSE
            )
            SELECT workspace_id, connection_id, refresh_id
            FROM schemabridge_control.catalog_refresh_runs
            WHERE status IN ('leased', 'staging')
              AND lease_expires_at <= clock_timestamp()
            ORDER BY lease_expires_at, workspace_id, connection_id, refresh_id
            LIMIT 100
            """
        ).fetchone()
        assert active_index_contract is not None
        assert active_index_contract[:3] == (True, True, True)
        assert "(workspace_id, connection_id)" in active_index_contract[3]
        active_predicate = active_index_contract[4]
        assert active_predicate is not None
        assert all(status in active_predicate for status in ("requested", "leased", "staging"))
        assert claim is not None
        assert expired is not None
        _assert_plan_uses_index(
            claim[0],
            expected_index="catalog_refresh_claim_idx",
            allow_bounded_sort=True,
            require_actual_rows=True,
        )
        reviewed.append((expired[0], "catalog_refresh_expired_lease_idx"))

    with psycopg.connect(scale_database.migrator) as connection, connection.transaction():
        connection.execute("SET LOCAL enable_seqscan = off")
        rate_exact = connection.execute(
            """
            EXPLAIN (
                FORMAT JSON, ANALYZE TRUE, BUFFERS TRUE, COSTS TRUE, TIMING FALSE
            )
            SELECT request_count
            FROM schemabridge_control.api_rate_limit_windows
            WHERE workspace_id = %s
              AND principal_digest = %s
              AND operation_scope = %s
              AND window_started_at = date_trunc('minute', clock_timestamp())
            """,
            (workspace_id, digest, "authenticated_api"),
        ).fetchone()
        rate_expiry = connection.execute(
            """
            EXPLAIN (
                FORMAT JSON, ANALYZE TRUE, BUFFERS TRUE, COSTS TRUE, TIMING FALSE
            )
            SELECT workspace_id, principal_digest
            FROM schemabridge_control.api_rate_limit_windows
            WHERE window_expires_at <= clock_timestamp()
            ORDER BY window_expires_at, workspace_id, principal_digest
            LIMIT 100
            """
        ).fetchone()
        assert rate_exact is not None
        assert rate_expiry is not None
        reviewed.extend(
            (
                (rate_exact[0], "api_rate_limit_windows_pkey"),
                (rate_expiry[0], "api_rate_limit_windows_expiry_idx"),
            )
        )

    with psycopg.connect(scale_database.api) as connection, connection.transaction():
        connection.execute("SET LOCAL enable_seqscan = off")
        connections_all = connection.execute(
            """
            EXPLAIN (
                FORMAT JSON, ANALYZE TRUE, BUFFERS TRUE, COSTS TRUE, TIMING FALSE
            )
            SELECT connection_id, display_name
            FROM schemabridge_control.catalog_connections
            WHERE workspace_id = %s
              AND (display_name, connection_id) > (%s, %s)
            ORDER BY display_name, connection_id
            LIMIT 51
            """,
            (workspace_id, "", ""),
        ).fetchone()
        capacity = connection.execute(
            """
            EXPLAIN (
                FORMAT JSON, ANALYZE TRUE, BUFFERS TRUE, COSTS TRUE, TIMING FALSE
            )
            SELECT nonterminal_job_count, capacity_version
            FROM schemabridge_control.tenant_execution_capacity
            WHERE workspace_id = %s
            """,
            (workspace_id,),
        ).fetchone()
        assert connections_all is not None
        assert capacity is not None
        reviewed.append((connections_all[0], "catalog_connections_all_page_idx"))
        reviewed.append((capacity[0], "tenant_execution_capacity_pkey"))

    with psycopg.connect(scale_database.worker) as connection, connection.transaction():
        connection.execute("SET LOCAL enable_seqscan = off")
        fair_claim = connection.execute(
            """
            EXPLAIN (
                FORMAT JSON, ANALYZE TRUE, BUFFERS TRUE, COSTS TRUE, TIMING FALSE
            )
            SELECT job_id
            FROM schemabridge_control.execution_jobs
            WHERE workspace_id = %s
              AND authorization_expires_at > clock_timestamp()
              AND attempt_count < max_attempts
              AND status IN ('queued', 'retry_wait')
              AND available_at <= clock_timestamp()
            ORDER BY available_at, created_at, job_id
            LIMIT 1
            """,
            (workspace_id,),
        ).fetchone()
        assert fair_claim is not None
        reviewed.append((fair_claim[0], "execution_jobs_fair_claim_idx"))

    for plan, expected_index in reviewed:
        _assert_plan_uses_index(
            plan,
            expected_index=expected_index,
            allow_bounded_sort=True,
        )


def _traverse_assets(
    inventory: PostgresCatalogInventoryReader,
    *,
    workspace_id: str,
    connection_id: CatalogConnectionId,
    generation: int,
    expected_count: int,
    page_size: int,
) -> tuple[tuple[str, str] | None, set[str], int]:
    after: InventoryPageKey | None = None
    identities: set[str] = set()
    last_order_key: tuple[str, str] | None = None
    maximum_rows_read = 0
    while True:
        page = inventory.list_assets(
            workspace_id,
            connection_id,
            filters=CatalogAssetFilter(),
            page_size=page_size,
            after=after,
            generation=generation,
        )
        assert len(page.items) <= page_size
        assert page.rows_read <= page_size + 1
        maximum_rows_read = max(maximum_rows_read, page.rows_read)
        for item in page.items:
            identity = item.locator.asset_id.root
            order_key = (
                item.qualified_name,
                hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            )
            assert last_order_key is None or order_key > last_order_key
            assert identity not in identities
            identities.add(identity)
            last_order_key = order_key
        if not page.has_more:
            assert page.last_key is not None
            break
        assert page.last_key is not None
        after = page.last_key
    assert len(identities) == expected_count
    return last_order_key, identities, maximum_rows_read


def test_dynamic_catalog_refresh_and_keyset_scale_on_real_postgres(
    scale_database: _DatabaseUrls,
    record_property: Callable[[str, object], None],
) -> None:
    now = datetime.now(UTC)
    small_workspace = "workspace_scale_a_small"
    large_workspace = "workspace_scale_b_large"
    small_connection = CatalogConnectionId("connection_scale_small")
    large_connections = (
        CatalogConnectionId("connection_scale_large_a"),
        CatalogConnectionId("connection_scale_large_b"),
    )
    large_primary_connection, large_secondary_connection = large_connections
    _provision_policy(scale_database.migrator, small_workspace)
    _provision_policy(scale_database.migrator, large_workspace)

    api_pool = _pool(
        scale_database.api,
        application_name="schemabridge-control-api",
    )
    catalog_pool = _pool(
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
        inventory = PostgresCatalogInventoryReader(
            scale_database.api,
            application_name="schemabridge-control-api",
            connection_provider=api_pool,
        )
        capacity = PostgresTenantCapacityStore(
            scale_database.api,
            application_name="schemabridge-control-api",
            connection_provider=api_pool,
        )
        clock = _Clock(now)
        register = RegisterCatalogConnection(api_connections, clock)
        request = RequestCatalogRefresh(api_connections, api_refreshes, clock)
        small_principal = _principal(small_workspace, now)
        large_principal = _principal(large_workspace, now)
        _register_connection(register, small_principal, small_connection)
        for large_connection in large_connections:
            _register_connection(register, large_principal, large_connection)
        for workspace_id, connection_id in (
            (small_workspace, small_connection),
            *((large_workspace, item) for item in large_connections),
        ):
            ensure_catalog_connector_target(
                scale_database.migrator,
                workspace_id=workspace_id,
                connection_id=connection_id,
            )
        routes = PostgresCatalogConnectorRouteReader(
            scale_database.catalog,
            connection_provider=catalog_pool,
        )

        source = _ObservedSyntheticSource(
            delegate=LazySyntheticCatalogSource(
                {
                    small_connection: SyntheticCatalogSpecification(asset_count=SMALL_ASSET_COUNT),
                    large_primary_connection: SyntheticCatalogSpecification(
                        asset_count=LARGE_PRIMARY_ASSET_COUNT,
                        wide_asset_every=LARGE_WIDE_ASSET_EVERY,
                        wide_field_count=LARGE_WIDE_FIELD_COUNT,
                        delta=SyntheticDeltaSpecification(
                            update_count=DELTA_UPDATE_COUNT,
                            addition_count=DELTA_ADDITION_COUNT,
                            deletion_count=DELTA_DELETION_COUNT,
                        ),
                    ),
                    large_secondary_connection: SyntheticCatalogSpecification(
                        asset_count=LARGE_SECONDARY_ASSET_COUNT,
                        wide_asset_every=LARGE_WIDE_ASSET_EVERY,
                        wide_field_count=LARGE_WIDE_FIELD_COUNT,
                    ),
                }
            ),
            catalog_pool=catalog_pool,
        )
        indexer = RunOneCatalogRefresh(
            refreshes=catalog_refreshes,
            routes=routes,
            sources=KindCatalogSourceResolver({CatalogConnectionKind.SYNTHETIC: source}),
            capability_factory=_CapabilityFactory(),
            indexer_id="catalog_scale_indexer",
            lease_duration=timedelta(minutes=2),
            page_size=SOURCE_PAGE_SIZE,
        )

        request.execute(
            small_principal,
            small_connection,
            mode=CatalogRefreshMode.FULL,
            confirmation="REQUEST CATALOG REFRESH",
            idempotency_key="refresh-small-full-0001",
        )
        for index, large_connection in enumerate(large_connections, start=1):
            request.execute(
                large_principal,
                large_connection,
                mode=CatalogRefreshMode.FULL,
                confirmation="REQUEST CATALOG REFRESH",
                idempotency_key=f"refresh-large-full-000{index}",
            )

        small_result = indexer.execute()
        large_started = perf_counter()
        large_results = tuple(indexer.execute() for _ in large_connections)
        large_elapsed_seconds = perf_counter() - large_started
        record_property(
            "large_full_refresh_seconds",
            round(large_elapsed_seconds, 6),
        )
        record_property("scale_test_platform", platform.platform())

        assert small_result.outcome is CatalogIndexerIterationOutcome.COMPLETED
        assert small_result.asset_count == SMALL_ASSET_COUNT
        assert small_result.field_count == _expected_field_count(SMALL_ASSET_COUNT)
        assert small_result.pages_processed == 1
        for large_result, expected_asset_count in zip(
            large_results,
            LARGE_CONNECTION_ASSET_COUNTS,
            strict=True,
        ):
            assert large_result.outcome is CatalogIndexerIterationOutcome.COMPLETED
            assert large_result.asset_count == expected_asset_count
            assert large_result.field_count == _expected_field_count(
                expected_asset_count,
                wide=True,
            )
            assert large_result.pages_processed == max(
                1,
                (expected_asset_count + SOURCE_PAGE_SIZE - 1) // SOURCE_PAGE_SIZE,
            )
        assert sum(result.asset_count for result in large_results) == LARGE_ASSET_COUNT
        assert sum(result.field_count for result in large_results) == EXPECTED_LARGE_FIELD_COUNT
        assert large_elapsed_seconds < MAX_FULL_REFRESH_SECONDS, (
            f"multi-connection large full refresh took {large_elapsed_seconds:.3f}s"
        )

        small_summary = api_connections.load_public(
            small_workspace,
            small_connection,
        )
        large_summaries = tuple(
            api_connections.load_public(large_workspace, large_connection)
            for large_connection in large_connections
        )
        assert small_summary is not None
        assert all(summary is not None for summary in large_summaries)
        assert (
            small_summary.active_generation,
            small_summary.asset_count,
            small_summary.field_count,
        ) == (1, SMALL_ASSET_COUNT, _expected_field_count(SMALL_ASSET_COUNT))
        for large_summary, expected_asset_count in zip(
            large_summaries,
            LARGE_CONNECTION_ASSET_COUNTS,
            strict=True,
        ):
            assert large_summary is not None
            assert (
                large_summary.active_generation,
                large_summary.asset_count,
                large_summary.field_count,
            ) == (
                1,
                expected_asset_count,
                _expected_field_count(expected_asset_count, wide=True),
            )
        large_capacity = capacity.inspect(large_workspace)
        assert (
            large_capacity.usage.connection_count,
            large_capacity.usage.asset_count,
            large_capacity.usage.field_count,
        ) == (
            len(large_connections),
            LARGE_ASSET_COUNT,
            EXPECTED_LARGE_FIELD_COUNT,
        )

        full_small_key = (
            small_workspace,
            small_connection.root,
            CatalogRefreshMode.FULL,
        )
        full_large_keys = tuple(
            (
                large_workspace,
                large_connection.root,
                CatalogRefreshMode.FULL,
            )
            for large_connection in large_connections
        )
        assert source.page_lengths[full_small_key] == [SMALL_ASSET_COUNT]
        for full_large_key, expected_asset_count in zip(
            full_large_keys,
            LARGE_CONNECTION_ASSET_COUNTS,
            strict=True,
        ):
            assert len(source.page_lengths[full_large_key]) == max(
                1,
                (expected_asset_count + SOURCE_PAGE_SIZE - 1) // SOURCE_PAGE_SIZE,
            )
            assert max(source.page_lengths[full_large_key]) == min(
                expected_asset_count,
                SOURCE_PAGE_SIZE,
            )
            assert sum(source.page_lengths[full_large_key]) == expected_asset_count
        assert (
            sum(sum(source.page_lengths[full_large_key]) for full_large_key in full_large_keys)
            == LARGE_ASSET_COUNT
        )

        traversals: dict[tuple[str, int], set[str]] = {}
        final_keys: dict[tuple[str, int], tuple[str, str] | None] = {}
        for case, workspace_id, connection_id, expected_count in (
            ("small", small_workspace, small_connection, SMALL_ASSET_COUNT),
            (
                "large_a",
                large_workspace,
                large_primary_connection,
                LARGE_PRIMARY_ASSET_COUNT,
            ),
            (
                "large_b",
                large_workspace,
                large_secondary_connection,
                LARGE_SECONDARY_ASSET_COUNT,
            ),
        ):
            for page_size in (1, 17, 50):
                final_key, identities, maximum_rows_read = _traverse_assets(
                    inventory,
                    workspace_id=workspace_id,
                    connection_id=connection_id,
                    generation=1,
                    expected_count=expected_count,
                    page_size=page_size,
                )
                assert maximum_rows_read <= page_size + 1
                traversals[(case, page_size)] = identities
                final_keys[(case, page_size)] = final_key

        for case in ("small", "large_a", "large_b"):
            assert traversals[(case, 1)] == traversals[(case, 17)]
            assert traversals[(case, 17)] == traversals[(case, 50)]
            assert final_keys[(case, 1)] == final_keys[(case, 17)]
            assert final_keys[(case, 17)] == final_keys[(case, 50)]
        for page_size in (1, 17, 50):
            tenant_locators = {
                (large_connection.root, asset_id)
                for large_connection, case in (
                    (large_primary_connection, "large_a"),
                    (large_secondary_connection, "large_b"),
                )
                for asset_id in traversals[(case, page_size)]
            }
            assert len(tenant_locators) == LARGE_ASSET_COUNT

        physical_discovery = PostgresPhysicalFieldDiscovery.from_signing_key(
            dsn=scale_database.runtime,
            cursor_signing_key=PHYSICAL_DISCOVERY_CURSOR_KEY,
        )
        small_discovery_scope = SemanticRegistryScope(
            workspace_id=small_workspace,
            catalog_scope="synthetic-scale",
            registry_id="registry_scale",
        )
        large_discovery_scope = SemanticRegistryScope(
            workspace_id=large_workspace,
            catalog_scope="synthetic-scale",
            registry_id="registry_scale",
        )
        with psycopg.connect(scale_database.runtime) as connection:
            small_physical_scope = connection.execute(
                """
                SELECT *
                FROM schemabridge_control.load_physical_discovery_scope(
                    %s, %s, %s
                )
                """,
                (
                    small_workspace,
                    small_discovery_scope.catalog_scope,
                    small_discovery_scope.registry_id,
                ),
            ).fetchone()
            large_physical_scope = connection.execute(
                """
                SELECT *
                FROM schemabridge_control.load_physical_discovery_scope(
                    %s, %s, %s
                )
                """,
                (
                    large_workspace,
                    large_discovery_scope.catalog_scope,
                    large_discovery_scope.registry_id,
                ),
            ).fetchone()
        assert small_physical_scope is not None
        assert large_physical_scope is not None
        assert small_physical_scope[1:] == (
            1,
            SMALL_ASSET_COUNT,
            _expected_field_count(SMALL_ASSET_COUNT),
        )
        assert large_physical_scope[1:] == (
            2,
            LARGE_ASSET_COUNT,
            EXPECTED_LARGE_FIELD_COUNT,
        )
        small_cardinality = physical_discovery.inspect_cardinality(small_discovery_scope)
        large_cardinality = physical_discovery.inspect_cardinality(large_discovery_scope)
        assert (
            small_cardinality.connection_count,
            small_cardinality.asset_count,
            small_cardinality.field_count,
        ) == small_physical_scope[1:]
        assert (
            large_cardinality.connection_count,
            large_cardinality.asset_count,
            large_cardinality.field_count,
        ) == large_physical_scope[1:]

        descriptive_physical_page = physical_discovery.search(
            PhysicalFieldDiscoveryRequest(
                scope=large_discovery_scope,
                query=DescriptionQuery("contract identifier"),
                page_size=5,
            )
        )
        assert len(descriptive_physical_page.items) == 5
        assert descriptive_physical_page.next_cursor is not None
        assert all(
            item.definition is not None
            and "contract" in item.definition.lower()
            and "identifier" in item.definition.lower()
            for item in descriptive_physical_page.items
        )

        small_physical_page = physical_discovery.search(
            PhysicalFieldDiscoveryRequest(
                scope=small_discovery_scope,
                query=DescriptionQuery("contract_id"),
                page_size=50,
            )
        )
        large_physical_page = physical_discovery.search(
            PhysicalFieldDiscoveryRequest(
                scope=large_discovery_scope,
                query=DescriptionQuery("contract_id"),
                page_size=50,
            )
        )
        small_matching_assets = {item.locator.asset.asset_id for item in small_physical_page.items}
        assert len(small_matching_assets) == 7
        assert len(small_matching_assets) < small_cardinality.asset_count
        assert len(small_physical_page.items) <= small_physical_scope[3]
        assert small_physical_page.next_cursor is None
        assert len(large_physical_page.items) == 50
        assert large_physical_page.next_cursor is not None
        assert all(
            item.status is PhysicalDiscoveryStatus.NEEDS_MAPPING_REVIEW
            and not hasattr(item, "candidate_id")
            for item in (*small_physical_page.items, *large_physical_page.items)
        )
        large_physical_continuation = physical_discovery.search(
            PhysicalFieldDiscoveryRequest(
                scope=large_discovery_scope,
                query=DescriptionQuery("contract_id"),
                page_size=50,
                cursor=large_physical_page.next_cursor,
            )
        )
        first_locators = {
            (
                item.locator.asset.connection_id.root,
                item.locator.asset.asset_id.root,
                item.locator.field_path,
            )
            for item in large_physical_page.items
        }
        second_locators = {
            (
                item.locator.asset.connection_id.root,
                item.locator.asset.asset_id.root,
                item.locator.field_path,
            )
            for item in large_physical_continuation.items
        }
        assert len(second_locators) == 50
        assert first_locators.isdisjoint(second_locators)

        small_first = inventory.list_assets(
            small_workspace,
            small_connection,
            filters=CatalogAssetFilter(),
            page_size=1,
            after=None,
            generation=1,
        ).items[0]
        large_primary_first = inventory.list_assets(
            large_workspace,
            large_primary_connection,
            filters=CatalogAssetFilter(),
            page_size=1,
            after=None,
            generation=1,
        ).items[0]
        large_secondary_first = inventory.list_assets(
            large_workspace,
            large_secondary_connection,
            filters=CatalogAssetFilter(),
            page_size=1,
            after=None,
            generation=1,
        ).items[0]
        assert (
            small_first.qualified_name
            == large_primary_first.qualified_name
            == large_secondary_first.qualified_name
        )
        assert (
            small_first.locator.asset_id
            == large_primary_first.locator.asset_id
            == large_secondary_first.locator.asset_id
        )
        assert small_first.locator.workspace_id != large_primary_first.locator.workspace_id
        assert small_first.locator.connection_id != large_primary_first.locator.connection_id
        assert (
            large_primary_first.locator.workspace_id
            == large_secondary_first.locator.workspace_id
            == large_workspace
        )
        assert (
            large_primary_first.locator.connection_id != large_secondary_first.locator.connection_id
        )
        with pytest.raises(CatalogInventoryError) as cross_tenant:
            inventory.list_assets(
                large_workspace,
                small_connection,
                filters=CatalogAssetFilter(),
                page_size=1,
                after=None,
                generation=1,
            )
        assert cross_tenant.value.code is CatalogInventoryErrorCode.GENERATION_UNAVAILABLE

        with psycopg.connect(scale_database.migrator) as connection:
            cold_statistics = connection.execute(
                """
                SELECT relname, last_analyze, last_autoanalyze
                FROM pg_catalog.pg_stat_user_tables
                WHERE schemaname = 'schemabridge_control'
                  AND relname IN ('catalog_assets', 'catalog_fields')
                ORDER BY relname
                """
            ).fetchall()
        assert cold_statistics == [
            ("catalog_assets", None, None),
            ("catalog_fields", None, None),
        ]

        request.execute(
            large_principal,
            large_primary_connection,
            mode=CatalogRefreshMode.DELTA,
            confirmation="REQUEST CATALOG REFRESH",
            idempotency_key="refresh-large-delta-0001",
        )
        delta_result = indexer.execute()
        assert delta_result.outcome is CatalogIndexerIterationOutcome.COMPLETED
        assert delta_result.pages_processed == 3
        assert delta_result.asset_count == EXPECTED_DELTA_CONNECTION_ASSET_COUNT
        deleted_start = LARGE_PRIMARY_ASSET_COUNT - DELTA_DELETION_COUNT
        added_end = LARGE_PRIMARY_ASSET_COUNT + DELTA_ADDITION_COUNT
        expected_deleted_fields = sum(
            _synthetic_field_count(index, wide=True)
            for index in range(deleted_start, LARGE_PRIMARY_ASSET_COUNT)
        )
        expected_delta_connection_fields = (
            _expected_field_count(LARGE_PRIMARY_ASSET_COUNT, wide=True)
            - expected_deleted_fields
            + sum(
                _synthetic_field_count(index, wide=True)
                for index in range(LARGE_PRIMARY_ASSET_COUNT, added_end)
            )
        )
        expected_delta_fields = expected_delta_connection_fields + _expected_field_count(
            LARGE_SECONDARY_ASSET_COUNT,
            wide=True,
        )
        assert delta_result.field_count == expected_delta_connection_fields
        delta_key = (
            large_workspace,
            large_primary_connection.root,
            CatalogRefreshMode.DELTA,
        )
        assert source.page_lengths[delta_key] == [50, 50, 37]

        large_after_delta = api_connections.load_public(
            large_workspace,
            large_primary_connection,
        )
        assert large_after_delta is not None
        assert (
            large_after_delta.active_generation,
            large_after_delta.asset_count,
            large_after_delta.field_count,
        ) == (
            2,
            EXPECTED_DELTA_CONNECTION_ASSET_COUNT,
            expected_delta_connection_fields,
        )
        large_capacity_after_delta = capacity.inspect(large_workspace)
        assert (
            large_capacity_after_delta.usage.connection_count,
            large_capacity_after_delta.usage.asset_count,
            large_capacity_after_delta.usage.field_count,
        ) == (
            len(large_connections),
            EXPECTED_DELTA_ASSET_COUNT,
            expected_delta_fields,
        )

        with catalog_pool.connection() as connection:
            tombstones = connection.execute(
                """
                SELECT resource_kind, count(*)
                FROM schemabridge_control.catalog_tombstones
                WHERE workspace_id = %s
                  AND connection_id = %s
                  AND observed_missing_in_generation = 2
                  AND removal_reason = 'delta_delete'
                GROUP BY resource_kind
                ORDER BY resource_kind
                """,
                (large_workspace, large_primary_connection.root),
            ).fetchall()
        assert tombstones == [
            ("asset", DELTA_DELETION_COUNT),
            ("field", expected_deleted_fields),
        ]

        pending_plan_refresh = request.execute(
            large_principal,
            large_primary_connection,
            mode=CatalogRefreshMode.DELTA,
            confirmation="REQUEST CATALOG REFRESH",
            idempotency_key="refresh-large-plan-evidence-0001",
        )
        assert pending_plan_refresh.replayed is False
        assert pending_plan_refresh.refresh.status is CatalogRefreshStatus.REQUESTED
        with pytest.raises(CatalogUseCaseError) as active_refresh_conflict:
            request.execute(
                large_principal,
                large_primary_connection,
                mode=CatalogRefreshMode.DELTA,
                confirmation="REQUEST CATALOG REFRESH",
                idempotency_key="refresh-large-plan-conflict-0001",
            )
        assert active_refresh_conflict.value.code is CatalogUseCaseErrorCode.SERVICE_UNAVAILABLE
        store_conflict = active_refresh_conflict.value.__cause__
        assert isinstance(store_conflict, CatalogInventoryError)
        assert store_conflict.code is CatalogInventoryErrorCode.REFRESH_CONFLICT

        with psycopg.connect(scale_database.migrator) as connection:
            connection.execute("ANALYZE schemabridge_control.catalog_assets")
            connection.execute("ANALYZE schemabridge_control.catalog_fields")
            connection.execute("ANALYZE schemabridge_control.catalog_refresh_runs")
        _assert_inventory_index_plans(
            api_pool,
            workspace_id=large_workspace,
            connection_id=large_primary_connection,
            generation=2,
        )
        _assert_control_index_plans(
            scale_database,
            workspace_id=large_workspace,
            connection_id=large_primary_connection,
        )
