"""PostgreSQL proof that every domain-valid UTF-8 boundary is persistable."""

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

from schemabridge.adapters.catalog.postgres_inventory import (
    PostgresCatalogConnectionStore,
)
from schemabridge.adapters.catalog.postgres_refresh import PostgresCatalogRefreshStore
from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.domain.catalog_inventory import (
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
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations/control_plane"
ADMIN_DSN = "postgresql://postgres:local-only-not-a-secret@127.0.0.1:55434/postgres"
CAPABILITY = "0123456789abcdef" * 4


def _admin_dsn() -> str:
    return os.environ.get(
        "SCHEMABRIDGE_TEST_CONTROL_ADMIN_DATABASE_URL",
        ADMIN_DSN,
    )


def _role_dsn(role: str, database: str) -> str:
    return f"postgresql://{role}:{role}@127.0.0.1:55434/{database}"


@pytest.fixture(scope="module")
def aligned_database() -> Iterator[tuple[str, str, str]]:
    database = f"schemabridge_alignment_{uuid4().hex[:12]}"
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
        assert migrated.inspection.current_version == 15
        yield migrator_dsn, api_dsn, catalog_dsn
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_domain_utf8_boundaries_persist_without_constraint_failures(
    aligned_database: tuple[str, str, str],
) -> None:
    migrator_dsn, api_dsn, catalog_dsn = aligned_database
    workspace_id = f"workspace_{uuid4().hex}"
    connection_id = CatalogConnectionId("connection_unicode")
    now = datetime.now(UTC)
    storage_400 = "😀" * 100
    schema_400 = "界" * 133 + "a"
    description_16000 = "😀" * 4_000
    terms_20000 = tuple(sorted("😀" * 199 + chr(0x1F680 + index) for index in range(25)))
    assert len(storage_400.encode("utf-8")) == 400
    assert len(schema_400.encode("utf-8")) == 400
    assert len(description_16000.encode("utf-8")) == 16_000
    assert sum(len(term.encode("utf-8")) for term in terms_20000) == 20_000

    with psycopg.connect(migrator_dsn) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.tenant_capacity_policies (
                workspace_id, connection_limit, asset_limit, field_limit,
                api_requests_per_minute, nonterminal_job_limit, version,
                updated_by, created_at, updated_at
            ) VALUES (
                %s, 10, 10000, 100000, 100, 100, 1,
                'actor_platform_admin', %s, %s
            )
            """,
            (workspace_id, now, now),
        )

    registered = PostgresCatalogConnectionStore(api_dsn).register(
        CatalogConnectionRegistration(
            workspace_id=workspace_id,
            connection_id=connection_id,
            display_name=storage_400,
            kind=CatalogConnectionKind.SYNTHETIC,
            environment="PROD.eu-1",
            catalog_scope=storage_400,
            platform_instance=storage_400,
            requested_by="actor_platform_admin",
            requested_at=now,
            idempotency_digest=_digest("registration"),
        )
    )
    assert not registered.replayed
    ensure_catalog_connector_target(
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
            requested_at=now,
            idempotency_digest=_digest("refresh"),
        )
    )
    leased = catalog_refreshes.claim_next(
        indexer_id="catalog:indexer.prod",
        lease_capability=CAPABILITY,
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
        indexer_id="catalog:indexer.prod",
        lease_capability=CAPABILITY,
        fencing_token=leased.lease.fencing_token,
    )
    staging = catalog_refreshes.begin_staging(
        workspace_id,
        leased.refresh_id,
        indexer_id="catalog:indexer.prod",
        lease_capability=CAPABILITY,
        fencing_token=1,
    )
    source_field = CatalogSourceField(
        field_path=("contract_id",),
        native_type=storage_400,
        description=description_16000,
        tags=terms_20000,
        metadata_fingerprint=_digest("field"),
    )
    source_asset = CatalogSourceAsset(
        asset_id=CatalogAssetId("asset-unicode-boundary"),
        qualified_name=f"{schema_400}.table",
        display_name=storage_400,
        platform="postgres",
        environment="PROD.eu-1",
        schema_name=schema_400,
        description=description_16000,
        fields=(source_field,),
        metadata_fingerprint=_digest("asset"),
    )
    page = CatalogSourcePage.create(
        mode=CatalogRefreshMode.FULL,
        sequence=1,
        changes=(
            CatalogSourceChange(
                kind=CatalogSourceChangeKind.UPSERT_ASSET,
                asset=source_asset,
            ),
        ),
        next_checkpoint=None,
        source_complete=True,
    )
    persisted = catalog_refreshes.persist_page(
        workspace_id,
        staging.refresh_id,
        indexer_id="catalog:indexer.prod",
        lease_capability=CAPABILITY,
        fencing_token=1,
        page=page,
    )
    assert (persisted.staged_asset_count, persisted.staged_field_count) == (1, 1)

    with psycopg.connect(migrator_dsn) as connection:
        row = connection.execute(
            """
            SELECT
                octet_length(catalog_connection.display_name),
                octet_length(catalog_connection.catalog_scope),
                octet_length(catalog_connection.platform_instance),
                octet_length(asset.schema_name),
                octet_length(asset.display_name),
                octet_length(asset.description),
                octet_length(field.native_type),
                octet_length(array_to_string(field.tags, '')),
                octet_length(field.description)
            FROM schemabridge_control.catalog_connections AS catalog_connection
            JOIN schemabridge_control.catalog_assets AS asset
              USING (workspace_id, connection_id)
            JOIN schemabridge_control.catalog_fields AS field
              USING (workspace_id, connection_id, generation, asset_key)
            WHERE catalog_connection.workspace_id = %s
              AND catalog_connection.connection_id = %s
            """,
            (workspace_id, connection_id.root),
        ).fetchone()
    assert row == (
        400,
        400,
        400,
        400,
        400,
        16_000,
        400,
        20_000,
        16_000,
    )
