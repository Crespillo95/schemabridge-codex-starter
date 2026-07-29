"""Real PostgreSQL proof that cursor lifetime never exceeds generation retention."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from tests.integration.connector_target_support import (
    acquire_catalog_refresh_route,
    ensure_catalog_connector_target,
)

from schemabridge.adapters.catalog.cursor import (
    DEFAULT_INVENTORY_CURSOR_TTL,
    SignedInventoryCursorCodec,
)
from schemabridge.adapters.catalog.postgres_inventory import (
    PostgresCatalogConnectionStore,
    PostgresCatalogInventoryReader,
)
from schemabridge.adapters.catalog.postgres_refresh import (
    PostgresCatalogRefreshStore,
)
from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.application.ports.catalog_inventory import InventoryCursorError
from schemabridge.domain.catalog_inventory import (
    CatalogAssetFilter,
    CatalogAssetId,
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogConnectionRegistration,
    CatalogRefreshCommand,
    CatalogRefreshMode,
    CatalogSourceAsset,
    CatalogSourceChange,
    CatalogSourceChangeKind,
    CatalogSourceField,
    CatalogSourcePage,
    InventoryCursorBinding,
    InventoryCursorResource,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations/control_plane"
ADMIN_DSN = "postgresql://postgres:local-only-not-a-secret@127.0.0.1:55434/postgres"
CURSOR_KEY = b"m25-postgres-retention-cursor-key-0123456789"
LEASE_CAPABILITY = "m25-catalog-lease-capability-0123456789abcdef"


def _admin_dsn() -> str:
    return os.environ.get(
        "SCHEMABRIDGE_TEST_CONTROL_ADMIN_DATABASE_URL",
        ADMIN_DSN,
    )


def _role_dsn(role: str, database: str) -> str:
    return f"postgresql://{role}:{role}@127.0.0.1:55434/{database}"


@pytest.fixture(scope="module")
def cursor_database() -> Iterator[tuple[str, str, str]]:
    database = f"schemabridge_cursor_{uuid4().hex[:12]}"
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
        assert migrated.inspection.current_version == 11
        yield migrator_dsn, api_dsn, catalog_dsn
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source_asset(index: int) -> CatalogSourceAsset:
    field = CatalogSourceField(
        field_path=("record_id",),
        native_type="bigint",
        nullable=False,
        is_part_of_key=True,
        metadata_fingerprint=_digest(f"field-{index}"),
    )
    return CatalogSourceAsset(
        asset_id=CatalogAssetId(f"asset-{index:04d}"),
        qualified_name=f"public.table_{index:04d}",
        display_name=f"table_{index:04d}",
        platform="postgres",
        environment="PROD",
        schema_name="public",
        fields=(field,),
        metadata_fingerprint=_digest(f"asset-{index}"),
    )


def _complete_full_refresh(
    *,
    migrator_dsn: str,
    api_dsn: str,
    catalog_dsn: str,
    workspace_id: str,
    connection_id: CatalogConnectionId,
    request_key: str,
    assets: tuple[CatalogSourceAsset, ...],
) -> int:
    target = ensure_catalog_connector_target(
        migrator_dsn,
        workspace_id=workspace_id,
        connection_id=connection_id,
    )
    api_refreshes = PostgresCatalogRefreshStore(
        api_dsn,
        application_name="schemabridge-control-api",
    )
    catalog_refreshes = PostgresCatalogRefreshStore(catalog_dsn)
    requested = api_refreshes.request(
        CatalogRefreshCommand(
            workspace_id=workspace_id,
            connection_id=connection_id,
            mode=CatalogRefreshMode.FULL,
            requested_by="actor_platform_admin",
            requested_at=datetime.now(UTC),
            idempotency_digest=_digest(request_key),
        )
    )
    leased = catalog_refreshes.claim_next(
        indexer_id="catalog-indexer-retention",
        lease_capability=LEASE_CAPABILITY,
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
        indexer_id=leased.lease.indexer_id,
        lease_capability=LEASE_CAPABILITY,
        fencing_token=leased.lease.fencing_token,
    )
    staging = catalog_refreshes.begin_staging(
        workspace_id,
        leased.refresh_id,
        indexer_id=leased.lease.indexer_id,
        lease_capability=LEASE_CAPABILITY,
        fencing_token=leased.lease.fencing_token,
    )
    catalog_refreshes.persist_page(
        workspace_id,
        leased.refresh_id,
        indexer_id=leased.lease.indexer_id,
        lease_capability=LEASE_CAPABILITY,
        fencing_token=leased.lease.fencing_token,
        page=CatalogSourcePage.create(
            mode=CatalogRefreshMode.FULL,
            sequence=1,
            changes=tuple(
                CatalogSourceChange(
                    kind=CatalogSourceChangeKind.UPSERT_ASSET,
                    asset=asset,
                )
                for asset in assets
            ),
            next_checkpoint=None,
            source_complete=True,
        ),
    )
    completed = catalog_refreshes.complete(
        workspace_id,
        leased.refresh_id,
        indexer_id=leased.lease.indexer_id,
        lease_capability=LEASE_CAPABILITY,
        fencing_token=leased.lease.fencing_token,
        expected_base_generation=staging.base_generation,
        expected_contract_version=target.contract_version,
        expected_route_revision=target.route_revision,
        expected_target_fingerprint=target.target_fingerprint,
    )
    return completed.target_generation


def test_retained_generation_cursor_cannot_outlive_or_renew_retention(
    cursor_database: tuple[str, str, str],
) -> None:
    migrator_dsn, api_dsn, catalog_dsn = cursor_database
    workspace_id = f"workspace_{uuid4().hex}"
    connection_id = CatalogConnectionId("connection_retention")
    with psycopg.connect(migrator_dsn) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.tenant_capacity_policies (
                workspace_id, connection_limit, asset_limit, field_limit,
                api_requests_per_minute, nonterminal_job_limit,
                catalog_cursor_ttl_seconds, generation_retention_seconds,
                version, updated_by, created_at, updated_at
            ) VALUES (
                %s, 10, 100000, 1000000, 100, 50, 900, 900,
                1, 'actor_platform_admin', clock_timestamp(), clock_timestamp()
            )
            """,
            (workspace_id,),
        )
    PostgresCatalogConnectionStore(api_dsn).register(
        CatalogConnectionRegistration(
            workspace_id=workspace_id,
            connection_id=connection_id,
            display_name="Retention test catalog",
            kind=CatalogConnectionKind.SYNTHETIC,
            environment="PROD",
            catalog_scope="synthetic-enterprise",
            requested_by="actor_platform_admin",
            requested_at=datetime.now(UTC),
            idempotency_digest=_digest("register-retention"),
        )
    )
    assets = tuple(_source_asset(index) for index in range(1, 4))
    first_generation = _complete_full_refresh(
        migrator_dsn=migrator_dsn,
        api_dsn=api_dsn,
        catalog_dsn=catalog_dsn,
        workspace_id=workspace_id,
        connection_id=connection_id,
        request_key="refresh-retention-one",
        assets=assets,
    )
    assert first_generation == 1

    reader = PostgresCatalogInventoryReader(api_dsn)
    codec = SignedInventoryCursorCodec(signing_key=CURSOR_KEY)
    filters = CatalogAssetFilter()
    binding = InventoryCursorBinding(
        workspace_id=workspace_id,
        resource=InventoryCursorResource.ASSETS,
        connection_id=connection_id,
        generation=first_generation,
        filter_fingerprint=filters.fingerprint,
        sort_fingerprint=_digest("qualified-name-asset-id-v1"),
    )
    issued_at = datetime.now(UTC)
    active_page = reader.list_assets(
        workspace_id,
        connection_id,
        filters=filters,
        page_size=1,
        after=None,
        generation=None,
    )
    assert active_page.has_more
    assert active_page.last_key is not None
    assert active_page.cursor_valid_until is None
    first_cursor = codec.encode(
        binding=binding,
        last_key=active_page.last_key,
        issued_at=issued_at,
        not_after=active_page.cursor_valid_until,
    )
    first_position = codec.decode(
        cursor=first_cursor,
        expected_binding=binding,
        at=issued_at,
    )

    second_generation = _complete_full_refresh(
        migrator_dsn=migrator_dsn,
        api_dsn=api_dsn,
        catalog_dsn=catalog_dsn,
        workspace_id=workspace_id,
        connection_id=connection_id,
        request_key="refresh-retention-two",
        assets=assets,
    )
    assert second_generation == 2
    retained_page = reader.list_assets(
        workspace_id,
        connection_id,
        filters=filters,
        page_size=1,
        after=first_position.last_key,
        generation=first_generation,
    )
    assert retained_page.has_more
    assert retained_page.last_key is not None
    assert retained_page.cursor_valid_until is not None
    assert retained_page.cursor_valid_until >= first_position.expires_at

    continued_cursor = codec.encode(
        binding=binding,
        last_key=retained_page.last_key,
        issued_at=first_position.issued_at,
        not_after=retained_page.cursor_valid_until,
    )
    just_before_expiry = first_position.expires_at - timedelta(microseconds=1)
    continued_position = codec.decode(
        cursor=continued_cursor,
        expected_binding=binding,
        at=just_before_expiry,
    )
    assert continued_position.issued_at == first_position.issued_at
    assert continued_position.expires_at == first_position.expires_at

    direct_old_page = reader.list_assets(
        workspace_id,
        connection_id,
        filters=filters,
        page_size=1,
        after=None,
        generation=first_generation,
    )
    assert direct_old_page.last_key is not None
    assert direct_old_page.cursor_valid_until is not None
    renewed_at = issued_at + timedelta(minutes=5)
    assert renewed_at + DEFAULT_INVENTORY_CURSOR_TTL > direct_old_page.cursor_valid_until
    with pytest.raises(InventoryCursorError):
        codec.encode(
            binding=binding,
            last_key=direct_old_page.last_key,
            issued_at=renewed_at,
            not_after=direct_old_page.cursor_valid_until,
        )
    with pytest.raises(InventoryCursorError):
        codec.decode(
            cursor=continued_cursor,
            expected_binding=binding,
            at=continued_position.expires_at,
        )
