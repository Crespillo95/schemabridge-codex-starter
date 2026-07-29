"""PostgreSQL integration proof for refresh leasing, staging, delta, and promotion."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from time import sleep
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from tests.integration.connector_target_support import (
    acquire_catalog_refresh_route,
    ensure_catalog_connector_target,
)

from schemabridge.adapters.catalog.postgres_refresh import (
    PostgresCatalogRefreshStore,
)
from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.application.ports.catalog_inventory import (
    CatalogInventoryError,
    CatalogInventoryErrorCode,
)
from schemabridge.domain.catalog_inventory import (
    CatalogAssetId,
    CatalogConnectionId,
    CatalogRefreshCommand,
    CatalogRefreshFailureCode,
    CatalogRefreshMode,
    CatalogRefreshState,
    CatalogRefreshStatus,
    CatalogSourceAsset,
    CatalogSourceChange,
    CatalogSourceChangeKind,
    CatalogSourceField,
    CatalogSourcePage,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations/control_plane"
ADMIN_DSN = "postgresql://postgres:local-only-not-a-secret@127.0.0.1:55434/postgres"
CAPABILITY_A = "0123456789abcdef" * 4
CAPABILITY_B = "fedcba9876543210" * 4


def _admin_dsn() -> str:
    return os.environ.get(
        "SCHEMABRIDGE_TEST_CONTROL_ADMIN_DATABASE_URL",
        ADMIN_DSN,
    )


def _role_dsn(role: str, database: str) -> str:
    return f"postgresql://{role}:{role}@127.0.0.1:55434/{database}"


@pytest.fixture(scope="module")
def refresh_database() -> Iterator[tuple[str, str, str]]:
    database = f"schemabridge_refresh_{uuid4().hex[:12]}"
    with psycopg.connect(_admin_dsn(), autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {} OWNER schemabridge_migrator").format(
                sql.Identifier(database)
            )
        )
        connection.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database))
        )
        connection.execute(
            sql.SQL(
                """
                GRANT CONNECT ON DATABASE {} TO
                    schemabridge_migrator,
                    schemabridge_api,
                    schemabridge_catalog
                """
            ).format(sql.Identifier(database))
        )
    migrator_dsn = _role_dsn("schemabridge_migrator", database)
    api_dsn = _role_dsn("schemabridge_api", database)
    catalog_dsn = _role_dsn("schemabridge_catalog", database)
    try:
        migrated = PostgresControlPlaneMigrator(migrator_dsn, MIGRATIONS).migrate()
        assert migrated.inspection.current_version == 9
        yield migrator_dsn, api_dsn, catalog_dsn
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _field(
    name: str,
    *,
    native_type: str = "varchar",
    description: str | None = None,
    tags: tuple[str, ...] = (),
    glossary_terms: tuple[str, ...] = (),
) -> CatalogSourceField:
    return CatalogSourceField(
        field_path=(name,),
        native_type=native_type,
        description=description,
        nullable=False,
        is_part_of_key=name.endswith("_id"),
        tags=tags,
        glossary_terms=glossary_terms,
        metadata_fingerprint=_digest(
            f"field:{name}:{native_type}:{description}:{tags}:{glossary_terms}"
        ),
    )


def _asset(
    asset_id: str,
    qualified_name: str,
    fields: tuple[CatalogSourceField, ...],
) -> CatalogSourceAsset:
    return CatalogSourceAsset(
        asset_id=CatalogAssetId(asset_id),
        qualified_name=qualified_name,
        display_name=qualified_name.rsplit(".", 1)[-1],
        platform="postgres",
        environment="PROD",
        schema_name="public",
        description=f"Synthetic metadata for {qualified_name}",
        fields=tuple(sorted(fields, key=lambda item: item.field_path)),
        metadata_fingerprint=_digest(f"asset:{asset_id}:{qualified_name}"),
    )


def _upsert(asset: CatalogSourceAsset) -> CatalogSourceChange:
    return CatalogSourceChange(
        kind=CatalogSourceChangeKind.UPSERT_ASSET,
        asset=asset,
    )


def _request(
    workspace_id: str,
    connection_id: CatalogConnectionId,
    *,
    mode: CatalogRefreshMode,
    key: str,
    requested_at: datetime,
) -> CatalogRefreshCommand:
    return CatalogRefreshCommand(
        workspace_id=workspace_id,
        connection_id=connection_id,
        mode=mode,
        requested_by="actor_catalog_admin",
        requested_at=requested_at,
        idempotency_digest=_digest(key),
    )


def _prepare_connection(
    migrator_dsn: str,
    api_dsn: str,
    workspace_id: str,
    connection_id: CatalogConnectionId,
) -> None:
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
                %s, 10, 10000, 100000, 100, 60, 100, 900, 1800,
                1, 'actor_platform_admin', clock_timestamp(), clock_timestamp()
            )
            """,
            (workspace_id,),
        )
    with psycopg.connect(api_dsn) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.catalog_connections (
                workspace_id, connection_id, display_name, source_kind,
                catalog_scope, environment, platform_instance, status,
                active_generation, active_generation_fingerprint,
                active_generation_completed_at, registration_fingerprint,
                idempotency_digest, created_by_actor_id,
                disabled_by_actor_id, disabled_idempotency_digest,
                created_at, updated_at, disabled_at
            ) VALUES (
                %s, %s, 'Synthetic enterprise catalog', 'synthetic',
                'synthetic-enterprise', 'PROD', NULL, 'enabled',
                NULL, NULL, NULL, %s, %s, 'actor_platform_admin',
                NULL, NULL, clock_timestamp(), clock_timestamp(), NULL
            )
            """,
            (
                workspace_id,
                connection_id.root,
                _digest("registration-fingerprint"),
                _digest("register-connection"),
            ),
        )
    ensure_catalog_connector_target(
        migrator_dsn,
        workspace_id=workspace_id,
        connection_id=connection_id,
    )


def _begin_full_staging(
    refresh_database: tuple[str, str, str],
    *,
    workspace_id: str,
    connection_id: CatalogConnectionId,
    request_key: str,
    indexer_id: str,
) -> tuple[PostgresCatalogRefreshStore, CatalogRefreshState]:
    migrator_dsn, api_dsn, catalog_dsn = refresh_database
    _prepare_connection(migrator_dsn, api_dsn, workspace_id, connection_id)
    api_store = PostgresCatalogRefreshStore(
        api_dsn,
        application_name="schemabridge-control-api",
    )
    catalog_store = PostgresCatalogRefreshStore(catalog_dsn)
    requested = api_store.request(
        _request(
            workspace_id,
            connection_id,
            mode=CatalogRefreshMode.FULL,
            key=request_key,
            requested_at=datetime.now(UTC),
        )
    )
    leased = catalog_store.claim_next(
        indexer_id=indexer_id,
        lease_capability=CAPABILITY_A,
        lease_duration=timedelta(minutes=5),
    )
    assert leased is not None
    assert leased.refresh_id == requested.refresh.refresh_id
    assert leased.lease is not None
    acquire_catalog_refresh_route(
        catalog_dsn,
        workspace_id=workspace_id,
        connection_id=connection_id,
        refresh_id=leased.refresh_id,
        indexer_id=indexer_id,
        lease_capability=CAPABILITY_A,
        fencing_token=leased.lease.fencing_token,
    )
    staging = catalog_store.begin_staging(
        workspace_id,
        leased.refresh_id,
        indexer_id=indexer_id,
        lease_capability=CAPABILITY_A,
        fencing_token=1,
    )
    assert staging.status is CatalogRefreshStatus.STAGING
    return catalog_store, staging


def test_full_delta_failure_reclaim_and_prune_are_durable(
    refresh_database: tuple[str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrator_dsn, api_dsn, catalog_dsn = refresh_database
    workspace_id = f"workspace_{uuid4().hex}"
    connection_id = CatalogConnectionId("connection_enterprise")
    _prepare_connection(migrator_dsn, api_dsn, workspace_id, connection_id)
    target = ensure_catalog_connector_target(
        migrator_dsn,
        workspace_id=workspace_id,
        connection_id=connection_id,
    )
    api_store = PostgresCatalogRefreshStore(
        api_dsn,
        application_name="schemabridge-control-api",
    )
    catalog_store = PostgresCatalogRefreshStore(catalog_dsn)
    now = datetime.now(UTC)

    full_command = _request(
        workspace_id,
        connection_id,
        mode=CatalogRefreshMode.FULL,
        key="refresh-full",
        requested_at=now,
    )
    requested = api_store.request(full_command)
    replayed = api_store.request(
        full_command.model_copy(update={"requested_at": now + timedelta(seconds=1)})
    )
    assert not requested.replayed
    assert replayed.replayed
    assert replayed.refresh.refresh_id == requested.refresh.refresh_id
    assert requested.refresh.base_generation == 0
    assert requested.refresh.target_generation == 1
    assert catalog_store.load("workspace_other", requested.refresh.refresh_id) is None

    with pytest.raises(CatalogInventoryError) as changed_replay:
        api_store.request(full_command.model_copy(update={"mode": CatalogRefreshMode.DELTA}))
    assert changed_replay.value.code is CatalogInventoryErrorCode.IDEMPOTENCY_CONFLICT
    with pytest.raises(CatalogInventoryError) as concurrent:
        api_store.request(
            _request(
                workspace_id,
                connection_id,
                mode=CatalogRefreshMode.FULL,
                key="refresh-concurrent",
                requested_at=now + timedelta(seconds=2),
            )
        )
    assert concurrent.value.code is CatalogInventoryErrorCode.REFRESH_CONFLICT

    leased = catalog_store.claim_next(
        indexer_id="catalog-indexer-a",
        lease_capability=CAPABILITY_A,
        lease_duration=timedelta(seconds=30),
    )
    assert leased is not None
    assert leased.refresh_id == requested.refresh.refresh_id
    assert leased.lease is not None
    assert leased.lease.fencing_token == 1
    assert CAPABILITY_A not in repr(leased)
    public_lease = api_store.load_public(workspace_id, leased.refresh_id)
    assert public_lease is not None
    assert public_lease.status is CatalogRefreshStatus.LEASED
    assert not hasattr(public_lease, "lease")
    assert not hasattr(public_lease, "source_checkpoint")
    with pytest.raises(CatalogInventoryError) as stale_capability:
        catalog_store.begin_staging(
            workspace_id,
            leased.refresh_id,
            indexer_id="catalog-indexer-a",
            lease_capability=CAPABILITY_B,
            fencing_token=1,
        )
    assert stale_capability.value.code is CatalogInventoryErrorCode.LEASE_CONFLICT

    heartbeat = catalog_store.heartbeat(
        workspace_id,
        leased.refresh_id,
        indexer_id="catalog-indexer-a",
        lease_capability=CAPABILITY_A,
        fencing_token=1,
        lease_duration=timedelta(seconds=30),
    )
    assert heartbeat.lease is not None
    acquire_catalog_refresh_route(
        catalog_dsn,
        workspace_id=workspace_id,
        connection_id=connection_id,
        refresh_id=leased.refresh_id,
        indexer_id="catalog-indexer-a",
        lease_capability=CAPABILITY_A,
        fencing_token=1,
    )
    staging = catalog_store.begin_staging(
        workspace_id,
        leased.refresh_id,
        indexer_id="catalog-indexer-a",
        lease_capability=CAPABILITY_A,
        fencing_token=1,
    )
    assert staging.status is CatalogRefreshStatus.STAGING
    assert (
        catalog_store.begin_staging(
            workspace_id,
            leased.refresh_id,
            indexer_id="catalog-indexer-a",
            lease_capability=CAPABILITY_A,
            fencing_token=1,
        )
        == staging
    )

    customers = _asset(
        "asset-customers",
        "public.customers",
        (
            _field("customer_id", native_type="bigint"),
            _field("name"),
            _field("status"),
        ),
    )
    accounts = _asset(
        "asset-accounts",
        "public.accounts",
        (
            _field("account_id", native_type="bigint"),
            _field("category"),
        ),
    )
    full_page = CatalogSourcePage.create(
        mode=CatalogRefreshMode.FULL,
        sequence=1,
        changes=(_upsert(accounts), _upsert(customers)),
        next_checkpoint=None,
        source_complete=True,
    )
    persisted = catalog_store.persist_page(
        workspace_id,
        leased.refresh_id,
        indexer_id="catalog-indexer-a",
        lease_capability=CAPABILITY_A,
        fencing_token=1,
        page=full_page,
    )
    assert (persisted.staged_asset_count, persisted.staged_field_count) == (2, 5)
    replayed_page = catalog_store.persist_page(
        workspace_id,
        leased.refresh_id,
        indexer_id="catalog-indexer-a",
        lease_capability=CAPABILITY_A,
        fencing_token=1,
        page=full_page,
    )
    assert replayed_page == persisted
    with psycopg.connect(api_dsn) as connection:
        invisible = connection.execute(
            """
            SELECT active_generation
            FROM schemabridge_control.catalog_connections
            WHERE workspace_id = %s AND connection_id = %s
            """,
            (workspace_id, connection_id.root),
        ).fetchone()
    assert invisible == (None,)

    original_fingerprint = PostgresCatalogRefreshStore._stream_generation_fingerprint
    fingerprint_started = Event()
    permit_fingerprint = Event()

    def _pause_after_completion_locks(
        store: PostgresCatalogRefreshStore,
        connection: psycopg.Connection[Any],
        state: CatalogRefreshState,
    ) -> str:
        fingerprint_started.set()
        if not permit_fingerprint.wait(timeout=5):
            raise AssertionError("completion fingerprint test coordination timed out")
        return original_fingerprint(store, connection, state)

    monkeypatch.setattr(
        PostgresCatalogRefreshStore,
        "_stream_generation_fingerprint",
        _pause_after_completion_locks,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        completion_future = executor.submit(
            catalog_store.complete,
            workspace_id,
            leased.refresh_id,
            indexer_id="catalog-indexer-a",
            lease_capability=CAPABILITY_A,
            fencing_token=1,
            expected_base_generation=0,
            expected_contract_version=target.contract_version,
            expected_route_revision=target.route_revision,
            expected_target_fingerprint=target.target_fingerprint,
        )
        assert fingerprint_started.wait(timeout=5)
        page_replay_future = executor.submit(
            catalog_store.persist_page,
            workspace_id,
            leased.refresh_id,
            indexer_id="catalog-indexer-a",
            lease_capability=CAPABILITY_A,
            fencing_token=1,
            page=full_page,
        )
        try:
            sleep(0.2)
            assert not page_replay_future.done()
        finally:
            permit_fingerprint.set()
        completed = completion_future.result(timeout=5)
        with pytest.raises(CatalogInventoryError) as concurrent_page:
            page_replay_future.result(timeout=5)
    assert concurrent_page.value.code is CatalogInventoryErrorCode.LEASE_CONFLICT
    monkeypatch.setattr(
        PostgresCatalogRefreshStore,
        "_stream_generation_fingerprint",
        original_fingerprint,
    )
    assert completed.status is CatalogRefreshStatus.COMPLETED
    assert completed.catalog_fingerprint is not None
    with psycopg.connect(api_dsn) as connection:
        promoted_fingerprint = connection.execute(
            """
            SELECT connection.active_generation_fingerprint,
                   generation.inventory_fingerprint,
                   generation.asset_count,
                   generation.field_count
            FROM schemabridge_control.catalog_connections AS connection
            JOIN schemabridge_control.catalog_generations AS generation
              ON generation.workspace_id = connection.workspace_id
             AND generation.connection_id = connection.connection_id
             AND generation.generation = connection.active_generation
            WHERE connection.workspace_id = %s
              AND connection.connection_id = %s
            """,
            (workspace_id, connection_id.root),
        ).fetchone()
    assert promoted_fingerprint == (
        completed.catalog_fingerprint,
        completed.catalog_fingerprint,
        2,
        5,
    )

    delta_requested = api_store.request(
        _request(
            workspace_id,
            connection_id,
            mode=CatalogRefreshMode.DELTA,
            key="refresh-delta",
            requested_at=datetime.now(UTC),
        )
    )
    assert delta_requested.refresh.base_generation == 1
    assert delta_requested.refresh.target_generation == 2
    delta_lease = catalog_store.claim_next(
        indexer_id="catalog-indexer-b",
        lease_capability=CAPABILITY_B,
        lease_duration=timedelta(seconds=30),
    )
    assert delta_lease is not None
    assert delta_lease.refresh_id == delta_requested.refresh.refresh_id
    assert delta_lease.lease is not None
    acquire_catalog_refresh_route(
        catalog_dsn,
        workspace_id=workspace_id,
        connection_id=connection_id,
        refresh_id=delta_lease.refresh_id,
        indexer_id="catalog-indexer-b",
        lease_capability=CAPABILITY_B,
        fencing_token=delta_lease.lease.fencing_token,
    )
    delta_staging = catalog_store.begin_staging(
        workspace_id,
        delta_lease.refresh_id,
        indexer_id="catalog-indexer-b",
        lease_capability=CAPABILITY_B,
        fencing_token=1,
    )
    assert delta_staging.base_generation == 1
    with psycopg.connect(catalog_dsn) as connection:
        cloned = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM schemabridge_control.catalog_assets
                 WHERE workspace_id = %s AND connection_id = %s AND generation = 2),
                (SELECT count(*) FROM schemabridge_control.catalog_fields
                 WHERE workspace_id = %s AND connection_id = %s AND generation = 2)
            """,
            (
                workspace_id,
                connection_id.root,
                workspace_id,
                connection_id.root,
            ),
        ).fetchone()
    assert cloned == (2, 5)

    delta_page = CatalogSourcePage.create(
        mode=CatalogRefreshMode.DELTA,
        sequence=1,
        changes=(
            CatalogSourceChange(
                kind=CatalogSourceChangeKind.DELETE_ASSET,
                asset_id=accounts.asset_id,
            ),
            CatalogSourceChange(
                kind=CatalogSourceChangeKind.DELETE_FIELD,
                asset_id=customers.asset_id,
                field_path=("status",),
            ),
            CatalogSourceChange(
                kind=CatalogSourceChangeKind.UPSERT_FIELD,
                asset_id=customers.asset_id,
                field=_field("email"),
            ),
        ),
        next_checkpoint=None,
        source_complete=True,
    )
    delta_persisted = catalog_store.persist_page(
        workspace_id,
        delta_lease.refresh_id,
        indexer_id="catalog-indexer-b",
        lease_capability=CAPABILITY_B,
        fencing_token=1,
        page=delta_page,
    )
    assert (delta_persisted.staged_asset_count, delta_persisted.staged_field_count) == (
        1,
        3,
    )
    delta_completed = catalog_store.complete(
        workspace_id,
        delta_lease.refresh_id,
        indexer_id="catalog-indexer-b",
        lease_capability=CAPABILITY_B,
        fencing_token=1,
        expected_base_generation=1,
        expected_contract_version=target.contract_version,
        expected_route_revision=target.route_revision,
        expected_target_fingerprint=target.target_fingerprint,
    )
    assert delta_completed.status is CatalogRefreshStatus.COMPLETED
    with psycopg.connect(catalog_dsn) as connection:
        active_and_tombstones = connection.execute(
            """
            SELECT connection.active_generation,
                   generation.asset_count,
                   generation.field_count,
                   (
                       SELECT count(*)
                       FROM schemabridge_control.catalog_tombstones
                       WHERE workspace_id = connection.workspace_id
                         AND connection_id = connection.connection_id
                         AND observed_missing_in_generation = 2
                   )
            FROM schemabridge_control.catalog_connections AS connection
            JOIN schemabridge_control.catalog_generations AS generation
              ON generation.workspace_id = connection.workspace_id
             AND generation.connection_id = connection.connection_id
             AND generation.generation = connection.active_generation
            WHERE connection.workspace_id = %s
              AND connection.connection_id = %s
            """,
            (workspace_id, connection_id.root),
        ).fetchone()
    assert active_and_tombstones == (2, 1, 3, 4)

    failed_request = api_store.request(
        _request(
            workspace_id,
            connection_id,
            mode=CatalogRefreshMode.FULL,
            key="refresh-failed",
            requested_at=datetime.now(UTC),
        )
    )
    failed_lease = catalog_store.claim_next(
        indexer_id="catalog-indexer-a",
        lease_capability=CAPABILITY_A,
        lease_duration=timedelta(seconds=30),
    )
    assert failed_lease is not None
    assert failed_lease.refresh_id == failed_request.refresh.refresh_id
    assert failed_lease.lease is not None
    acquire_catalog_refresh_route(
        catalog_dsn,
        workspace_id=workspace_id,
        connection_id=connection_id,
        refresh_id=failed_lease.refresh_id,
        indexer_id="catalog-indexer-a",
        lease_capability=CAPABILITY_A,
        fencing_token=failed_lease.lease.fencing_token,
    )
    catalog_store.begin_staging(
        workspace_id,
        failed_lease.refresh_id,
        indexer_id="catalog-indexer-a",
        lease_capability=CAPABILITY_A,
        fencing_token=1,
    )
    failed = catalog_store.fail(
        workspace_id,
        failed_lease.refresh_id,
        indexer_id="catalog-indexer-a",
        lease_capability=CAPABILITY_A,
        fencing_token=1,
        code=CatalogRefreshFailureCode.SOURCE_INVALID_RESPONSE,
    )
    assert failed.status is CatalogRefreshStatus.FAILED
    assert (
        catalog_store.fail(
            workspace_id,
            failed_lease.refresh_id,
            indexer_id="catalog-indexer-a",
            lease_capability=CAPABILITY_A,
            fencing_token=1,
            code=CatalogRefreshFailureCode.SOURCE_INVALID_RESPONSE,
        )
        == failed
    )
    assert failed.completed_at is not None
    assert (
        catalog_store.prune_generations(
            workspace_id,
            connection_id,
            completed_before=failed.completed_at,
            limit=1,
        )
        == 0
    )
    assert catalog_store.prune_due_generations(limit=1) == 1

    reclaimed_request = api_store.request(
        _request(
            workspace_id,
            connection_id,
            mode=CatalogRefreshMode.FULL,
            key="refresh-reclaim",
            requested_at=datetime.now(UTC),
        )
    )
    first_lease = catalog_store.claim_next(
        indexer_id="catalog-indexer-a",
        lease_capability=CAPABILITY_A,
        lease_duration=timedelta(seconds=10),
    )
    assert first_lease is not None
    assert first_lease.refresh_id == reclaimed_request.refresh.refresh_id
    sleep(10.1)
    assert catalog_store.reclaim_expired(limit=1) == 1
    second_lease = catalog_store.claim_next(
        indexer_id="catalog-indexer-b",
        lease_capability=CAPABILITY_B,
        lease_duration=timedelta(seconds=30),
    )
    assert second_lease is not None
    assert second_lease.refresh_id == first_lease.refresh_id
    assert second_lease.lease is not None
    assert second_lease.lease.fencing_token == 2
    with pytest.raises(CatalogInventoryError) as stale_fence:
        catalog_store.heartbeat(
            workspace_id,
            first_lease.refresh_id,
            indexer_id="catalog-indexer-a",
            lease_capability=CAPABILITY_A,
            fencing_token=1,
            lease_duration=timedelta(seconds=30),
        )
    assert stale_fence.value.code is CatalogInventoryErrorCode.LEASE_CONFLICT


def test_failed_first_staging_is_prunable_without_an_active_generation(
    refresh_database: tuple[str, str, str],
) -> None:
    _, _, catalog_dsn = refresh_database
    workspace_id = f"workspace_{uuid4().hex}"
    connection_id = CatalogConnectionId("connection_failed_first")
    indexer_id = "catalog-failed-first"
    catalog_store, staging = _begin_full_staging(
        refresh_database,
        workspace_id=workspace_id,
        connection_id=connection_id,
        request_key="refresh-failed-first",
        indexer_id=indexer_id,
    )

    failed = catalog_store.fail(
        workspace_id,
        staging.refresh_id,
        indexer_id=indexer_id,
        lease_capability=CAPABILITY_A,
        fencing_token=1,
        code=CatalogRefreshFailureCode.SOURCE_INVALID_RESPONSE,
    )
    assert failed.completed_at is not None
    with psycopg.connect(catalog_dsn) as connection:
        assert connection.execute(
            """
                SELECT active_generation
                FROM schemabridge_control.catalog_connections
                WHERE workspace_id = %s
                  AND connection_id = %s
                """,
            (workspace_id, connection_id.root),
        ).fetchone() == (None,)

    assert (
        catalog_store.prune_generations(
            workspace_id,
            connection_id,
            completed_before=failed.completed_at + timedelta(seconds=1),
            limit=1,
        )
        == 1
    )


def test_full_batch_rejects_a_repeated_asset_and_keeps_the_prior_page_intact(
    refresh_database: tuple[str, str, str],
) -> None:
    _, _, catalog_dsn = refresh_database
    workspace_id = f"workspace_{uuid4().hex}"
    connection_id = CatalogConnectionId("connection_batch_replace")
    indexer_id = "catalog-batch-replace"
    catalog_store, staging = _begin_full_staging(
        refresh_database,
        workspace_id=workspace_id,
        connection_id=connection_id,
        request_key="refresh-batch-replace",
        indexer_id=indexer_id,
    )
    original = _asset(
        "asset-repeated",
        "public.original_contracts",
        (
            _field("contract_id", native_type="bigint"),
            _field("status"),
        ),
    )
    replacement = _asset(
        "asset-repeated",
        "public.replacement_contracts",
        (),
    )
    later_asset = _asset(
        "asset-later",
        "public.later_asset",
        (
            _field("later_id", native_type="bigint"),
            _field("created_at", native_type="timestamp"),
            _field("status"),
        ),
    )
    first_page = CatalogSourcePage.create(
        mode=CatalogRefreshMode.FULL,
        sequence=1,
        changes=(_upsert(original),),
        next_checkpoint="full-page-2",
        source_complete=False,
    )
    second_page = CatalogSourcePage.create(
        mode=CatalogRefreshMode.FULL,
        sequence=2,
        changes=(_upsert(replacement), _upsert(later_asset)),
        next_checkpoint=None,
        source_complete=True,
    )

    first = catalog_store.persist_page(
        workspace_id,
        staging.refresh_id,
        indexer_id=indexer_id,
        lease_capability=CAPABILITY_A,
        fencing_token=1,
        page=first_page,
    )
    assert (first.staged_asset_count, first.staged_field_count) == (1, 2)
    with pytest.raises(CatalogInventoryError) as rejected:
        catalog_store.persist_page(
            workspace_id,
            staging.refresh_id,
            indexer_id=indexer_id,
            lease_capability=CAPABILITY_A,
            fencing_token=1,
            page=second_page,
        )
    assert rejected.value.code is CatalogInventoryErrorCode.INVALID_RESPONSE
    assert str(rejected.value) == "catalog inventory response is invalid"

    durable = catalog_store.load(workspace_id, staging.refresh_id)
    assert durable is not None
    assert durable.status is CatalogRefreshStatus.STAGING
    assert durable.source_page_count == 1
    assert durable.source_checkpoint == first_page.next_checkpoint
    assert durable.source_page_fingerprint == first_page.page_fingerprint
    assert not durable.source_complete
    assert (durable.staged_asset_count, durable.staged_field_count) == (1, 2)
    with psycopg.connect(catalog_dsn) as connection:
        asset_row = connection.execute(
            """
            SELECT qualified_name, field_count, metadata_fingerprint
            FROM schemabridge_control.catalog_assets
            WHERE workspace_id = %s
              AND connection_id = %s
              AND generation = %s
              AND asset_id = %s
            """,
            (
                workspace_id,
                connection_id.root,
                staging.target_generation,
                original.asset_id.root,
            ),
        ).fetchone()
        field_rows = connection.execute(
            """
            SELECT field.field_name
            FROM schemabridge_control.catalog_fields AS field
            JOIN schemabridge_control.catalog_assets AS asset
              ON asset.workspace_id = field.workspace_id
             AND asset.connection_id = field.connection_id
             AND asset.generation = field.generation
             AND asset.asset_key = field.asset_key
            WHERE field.workspace_id = %s
              AND field.connection_id = %s
              AND field.generation = %s
              AND asset.asset_id = %s
            ORDER BY field.ordinal_position
            """,
            (
                workspace_id,
                connection_id.root,
                staging.target_generation,
                original.asset_id.root,
            ),
        ).fetchall()
        later_asset_count = connection.execute(
            """
            SELECT count(*)
            FROM schemabridge_control.catalog_assets
            WHERE workspace_id = %s
              AND connection_id = %s
              AND generation = %s
              AND asset_id = %s
            """,
            (
                workspace_id,
                connection_id.root,
                staging.target_generation,
                later_asset.asset_id.root,
            ),
        ).fetchone()
    assert asset_row == (
        original.qualified_name,
        2,
        original.metadata_fingerprint,
    )
    assert field_rows == [("contract_id",), ("status",)]
    assert later_asset_count == (0,)

    failed = catalog_store.fail(
        workspace_id,
        staging.refresh_id,
        indexer_id=indexer_id,
        lease_capability=CAPABILITY_A,
        fencing_token=1,
        code=CatalogRefreshFailureCode.SOURCE_INVALID_RESPONSE,
    )
    assert failed.status is CatalogRefreshStatus.FAILED


def test_full_batch_rolls_back_every_row_and_checkpoint_on_constraint_failure(
    refresh_database: tuple[str, str, str],
) -> None:
    migrator_dsn, _, catalog_dsn = refresh_database
    workspace_id = f"workspace_{uuid4().hex}"
    connection_id = CatalogConnectionId("connection_batch_rollback")
    indexer_id = "catalog-batch-rollback"
    catalog_store, staging = _begin_full_staging(
        refresh_database,
        workspace_id=workspace_id,
        connection_id=connection_id,
        request_key="refresh-batch-rollback",
        indexer_id=indexer_id,
    )
    valid = _asset(
        "asset-valid",
        "public.valid_asset",
        (_field("valid_id", native_type="bigint"),),
    )
    invalid = _asset(
        "asset-invalid",
        "public.invalid_asset",
        (_field("invalid_type"),),
    )
    invalid_page = CatalogSourcePage.create(
        mode=CatalogRefreshMode.FULL,
        sequence=1,
        changes=(_upsert(valid), _upsert(invalid)),
        next_checkpoint=None,
        source_complete=True,
    )

    constraint_name = f"test_reject_asset_{uuid4().hex}"
    with psycopg.connect(migrator_dsn) as connection:
        connection.execute(
            sql.SQL(
                "ALTER TABLE schemabridge_control.catalog_assets "
                "ADD CONSTRAINT {} CHECK (asset_id <> 'asset-invalid')"
            ).format(sql.Identifier(constraint_name))
        )
    try:
        with pytest.raises(CatalogInventoryError) as rejected:
            catalog_store.persist_page(
                workspace_id,
                staging.refresh_id,
                indexer_id=indexer_id,
                lease_capability=CAPABILITY_A,
                fencing_token=1,
                page=invalid_page,
            )
    finally:
        with psycopg.connect(migrator_dsn) as connection:
            connection.execute(
                sql.SQL(
                    "ALTER TABLE schemabridge_control.catalog_assets DROP CONSTRAINT {}"
                ).format(sql.Identifier(constraint_name))
            )
    assert rejected.value.code is CatalogInventoryErrorCode.INVALID_RESPONSE
    assert str(rejected.value) == "catalog inventory response is invalid"

    durable = catalog_store.load(workspace_id, staging.refresh_id)
    assert durable is not None
    assert durable.status is CatalogRefreshStatus.STAGING
    assert durable.source_page_count == 0
    assert durable.source_checkpoint is None
    assert not durable.source_complete
    assert (durable.staged_asset_count, durable.staged_field_count) == (0, 0)
    with psycopg.connect(catalog_dsn) as connection:
        persisted_counts = connection.execute(
            """
            SELECT
                (
                    SELECT count(*)
                    FROM schemabridge_control.catalog_assets
                    WHERE workspace_id = %s
                      AND connection_id = %s
                      AND generation = %s
                ),
                (
                    SELECT count(*)
                    FROM schemabridge_control.catalog_fields
                    WHERE workspace_id = %s
                      AND connection_id = %s
                      AND generation = %s
                )
            """,
            (
                workspace_id,
                connection_id.root,
                staging.target_generation,
                workspace_id,
                connection_id.root,
                staging.target_generation,
            ),
        ).fetchone()
    assert persisted_counts == (0, 0)

    failed = catalog_store.fail(
        workspace_id,
        staging.refresh_id,
        indexer_id=indexer_id,
        lease_capability=CAPABILITY_A,
        fencing_token=1,
        code=CatalogRefreshFailureCode.SOURCE_INVALID_RESPONSE,
    )
    assert failed.status is CatalogRefreshStatus.FAILED


def test_promotion_capacity_rejection_is_typed_and_can_be_closed_terminally(
    refresh_database: tuple[str, str, str],
) -> None:
    migrator_dsn, _, _ = refresh_database
    workspace_id = f"workspace_{uuid4().hex}"
    connection_id = CatalogConnectionId("connection_capacity_rejection")
    indexer_id = "catalog-capacity-rejection"
    catalog_store, staging = _begin_full_staging(
        refresh_database,
        workspace_id=workspace_id,
        connection_id=connection_id,
        request_key="refresh-capacity-rejection",
        indexer_id=indexer_id,
    )
    page = CatalogSourcePage.create(
        mode=CatalogRefreshMode.FULL,
        sequence=1,
        changes=(
            _upsert(
                _asset(
                    "asset-capacity-a",
                    "public.capacity_a",
                    (_field("id_a", native_type="bigint"),),
                )
            ),
            _upsert(
                _asset(
                    "asset-capacity-b",
                    "public.capacity_b",
                    (_field("id_b", native_type="bigint"),),
                )
            ),
        ),
        next_checkpoint=None,
        source_complete=True,
    )
    persisted = catalog_store.persist_page(
        workspace_id,
        staging.refresh_id,
        indexer_id=indexer_id,
        lease_capability=CAPABILITY_A,
        fencing_token=1,
        page=page,
    )
    assert (persisted.staged_asset_count, persisted.staged_field_count) == (2, 2)
    with psycopg.connect(migrator_dsn) as connection:
        connection.execute(
            """
            UPDATE schemabridge_control.tenant_capacity_policies
            SET asset_limit = 1,
                version = version + 1,
                updated_by = 'actor_capacity_operator',
                updated_at = clock_timestamp()
            WHERE workspace_id = %s
            """,
            (workspace_id,),
        )

    target = ensure_catalog_connector_target(
        migrator_dsn,
        workspace_id=workspace_id,
        connection_id=connection_id,
    )
    with pytest.raises(CatalogInventoryError) as rejected:
        catalog_store.complete(
            workspace_id,
            staging.refresh_id,
            indexer_id=indexer_id,
            lease_capability=CAPABILITY_A,
            fencing_token=1,
            expected_base_generation=0,
            expected_contract_version=target.contract_version,
            expected_route_revision=target.route_revision,
            expected_target_fingerprint=target.target_fingerprint,
        )

    assert rejected.value.code is CatalogInventoryErrorCode.CAPACITY_EXCEEDED
    assert str(rejected.value) == "catalog capacity is exceeded"
    durable = catalog_store.load(workspace_id, staging.refresh_id)
    assert durable is not None
    assert durable.status is CatalogRefreshStatus.STAGING
    assert durable.lease is not None
    failed = catalog_store.fail(
        workspace_id,
        staging.refresh_id,
        indexer_id=indexer_id,
        lease_capability=CAPABILITY_A,
        fencing_token=1,
        code=CatalogRefreshFailureCode.CAPACITY_EXCEEDED,
    )
    assert failed.status is CatalogRefreshStatus.FAILED
    assert failed.failure_code is CatalogRefreshFailureCode.CAPACITY_EXCEEDED
    assert failed.lease is None


def test_field_search_document_indexes_tags_and_glossary_terms(
    refresh_database: tuple[str, str, str],
) -> None:
    migrator_dsn, _, _ = refresh_database
    workspace_id = f"workspace_{uuid4().hex}"
    connection_id = CatalogConnectionId("connection_search_evidence")
    indexer_id = "catalog-search-evidence"
    catalog_store, staging = _begin_full_staging(
        refresh_database,
        workspace_id=workspace_id,
        connection_id=connection_id,
        request_key="refresh-search-evidence",
        indexer_id=indexer_id,
    )
    page = CatalogSourcePage.create(
        mode=CatalogRefreshMode.FULL,
        sequence=1,
        changes=(
            _upsert(
                _asset(
                    "asset-search-evidence",
                    "public.account_roles",
                    (
                        _field(
                            "role_code",
                            tags=("secondary_holder",),
                            glossary_terms=("account_ownership_role",),
                        ),
                    ),
                )
            ),
        ),
        next_checkpoint=None,
        source_complete=True,
    )
    catalog_store.persist_page(
        workspace_id,
        staging.refresh_id,
        indexer_id=indexer_id,
        lease_capability=CAPABILITY_A,
        fencing_token=1,
        page=page,
    )

    with psycopg.connect(migrator_dsn) as connection:
        matches = connection.execute(
            """
            SELECT
                search_document @@ plainto_tsquery('simple', 'secondary_holder'),
                search_document @@ plainto_tsquery('simple', 'account_ownership_role')
            FROM schemabridge_control.catalog_fields
            WHERE workspace_id = %s
              AND connection_id = %s
              AND generation = %s
            """,
            (workspace_id, connection_id.root, staging.target_generation),
        ).fetchone()

    assert matches == (True, True)


def test_full_batch_persists_the_exact_maximum_page_shape(
    refresh_database: tuple[str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrator_dsn, _, catalog_dsn = refresh_database
    workspace_id = f"workspace_{uuid4().hex}"
    connection_id = CatalogConnectionId("connection_batch_maximum")
    indexer_id = "catalog-batch-maximum"
    catalog_store, staging = _begin_full_staging(
        refresh_database,
        workspace_id=workspace_id,
        connection_id=connection_id,
        request_key="refresh-batch-maximum",
        indexer_id=indexer_id,
    )
    assets = tuple(
        _asset(
            f"asset-batch-{asset_index:02d}",
            f"public.batch_{asset_index:02d}",
            tuple(_field(f"field_{field_index:03d}") for field_index in range(100)),
        )
        for asset_index in range(50)
    )
    maximum_page = CatalogSourcePage.create(
        mode=CatalogRefreshMode.FULL,
        sequence=1,
        changes=tuple(_upsert(asset) for asset in assets),
        next_checkpoint=None,
        source_complete=True,
    )

    def reject_generation_scan(*args: object, **kwargs: object) -> tuple[int, int]:
        del args, kwargs
        raise AssertionError("full refresh page counts must be incremental")

    monkeypatch.setattr(
        PostgresCatalogRefreshStore,
        "_generation_counts",
        reject_generation_scan,
    )
    persisted = catalog_store.persist_page(
        workspace_id,
        staging.refresh_id,
        indexer_id=indexer_id,
        lease_capability=CAPABILITY_A,
        fencing_token=1,
        page=maximum_page,
    )
    assert (persisted.staged_asset_count, persisted.staged_field_count) == (50, 5_000)
    assert (
        catalog_store.persist_page(
            workspace_id,
            staging.refresh_id,
            indexer_id=indexer_id,
            lease_capability=CAPABILITY_A,
            fencing_token=1,
            page=maximum_page,
        )
        == persisted
    )

    with psycopg.connect(catalog_dsn) as connection:
        exact_shape = connection.execute(
            """
            SELECT count(*), min(field_count), max(field_count), sum(field_count)
            FROM schemabridge_control.catalog_assets
            WHERE workspace_id = %s
              AND connection_id = %s
              AND generation = %s
            """,
            (workspace_id, connection_id.root, staging.target_generation),
        ).fetchone()
        field_shape = connection.execute(
            """
            SELECT count(*), min(ordinal_position), max(ordinal_position)
            FROM schemabridge_control.catalog_fields
            WHERE workspace_id = %s
              AND connection_id = %s
              AND generation = %s
            """,
            (workspace_id, connection_id.root, staging.target_generation),
        ).fetchone()
    assert exact_shape == (50, 100, 100, 5_000)
    assert field_shape == (5_000, 1, 100)

    target = ensure_catalog_connector_target(
        migrator_dsn,
        workspace_id=workspace_id,
        connection_id=connection_id,
    )
    completed = catalog_store.complete(
        workspace_id,
        staging.refresh_id,
        indexer_id=indexer_id,
        lease_capability=CAPABILITY_A,
        fencing_token=1,
        expected_base_generation=0,
        expected_contract_version=target.contract_version,
        expected_route_revision=target.route_revision,
        expected_target_fingerprint=target.target_fingerprint,
    )
    assert (completed.staged_asset_count, completed.staged_field_count) == (50, 5_000)
