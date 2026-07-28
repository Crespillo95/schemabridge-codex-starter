"""Real PostgreSQL proof for public catalog metadata and capacity adapters."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from schemabridge.adapters.catalog.postgres_inventory import (
    PostgresCatalogConnectionStore,
    PostgresTenantCapacityPolicyOperator,
    PostgresTenantCapacityStore,
)
from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.application.ports.catalog_inventory import (
    CatalogInventoryError,
    CatalogInventoryErrorCode,
)
from schemabridge.domain.catalog_inventory import (
    CatalogConnectionDisable,
    CatalogConnectionFilter,
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogConnectionRegistration,
    CatalogConnectionStatus,
    TenantCapacityPolicyChange,
    TenantCapacityPolicyConfirmation,
)

pytestmark = pytest.mark.integration

API_DSN = "postgresql://schemabridge_api:schemabridge_api@127.0.0.1:55434/schemabridge_control"
MIGRATOR_DSN = (
    "postgresql://schemabridge_migrator:schemabridge_migrator@127.0.0.1:55434/schemabridge_control"
)
NOW = datetime(2026, 7, 23, 21, 0, tzinfo=UTC)


@pytest.fixture(scope="module", autouse=True)
def _current_schema() -> None:
    PostgresControlPlaneMigrator(
        MIGRATOR_DSN,
        Path("migrations/control_plane"),
    ).require_current()


def _workspace() -> str:
    return f"workspace_{uuid4().hex}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _policy(
    workspace_id: str,
    *,
    connection_limit: int = 10,
    request_limit: int = 3,
) -> None:
    with psycopg.connect(MIGRATOR_DSN) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.tenant_capacity_policies (
                workspace_id, connection_limit, asset_limit, field_limit,
                api_requests_per_minute, nonterminal_job_limit, version,
                updated_by, created_at, updated_at
            ) VALUES (
                %s, %s, 100000, 1000000, %s, 50, 1,
                'actor_platform_admin', clock_timestamp(), clock_timestamp()
            )
            """,
            (workspace_id, connection_limit, request_limit),
        )


def _registration(
    workspace_id: str,
    connection_id: str,
    *,
    key: str,
    display_name: str | None = None,
) -> CatalogConnectionRegistration:
    return CatalogConnectionRegistration(
        workspace_id=workspace_id,
        connection_id=CatalogConnectionId(connection_id),
        display_name=display_name or connection_id,
        kind=CatalogConnectionKind.SYNTHETIC,
        environment="PROD",
        catalog_scope="synthetic-enterprise",
        requested_by="actor_platform_admin",
        requested_at=NOW,
        idempotency_digest=_digest(key),
    )


def test_capacity_policy_operator_creates_revises_and_audits_optimistically() -> None:
    workspace_id = _workspace()
    operator = PostgresTenantCapacityPolicyOperator(MIGRATOR_DSN)
    first_change = TenantCapacityPolicyChange(
        workspace_id=workspace_id,
        expected_version=0,
        connection_limit=10,
        asset_limit=100_000,
        field_limit=1_000_000,
        api_requests_per_minute=100,
        nonterminal_job_limit=50,
        generation_retention_seconds=1_800,
        updated_by="actor_platform_admin",
        confirmation=TenantCapacityPolicyConfirmation.APPLY,
    )

    first = operator.apply(first_change)

    assert first.version == 1
    assert first.catalog_cursor_ttl_seconds == 900
    assert first.generation_retention_seconds == 1_800
    with pytest.raises(CatalogInventoryError) as stale:
        operator.apply(first_change)
    assert stale.value.code is CatalogInventoryErrorCode.POLICY_CONFLICT

    second = operator.apply(
        first_change.model_copy(
            update={
                "expected_version": 1,
                "connection_limit": 6_000,
                "asset_limit": 10_000_000,
                "field_limit": 100_000_000,
                "generation_retention_seconds": 3_600,
            }
        )
    )

    assert second.version == 2
    assert second.connection_limit == 6_000
    assert second.generation_retention_seconds == 3_600
    assert PostgresTenantCapacityStore(API_DSN).load_policy(workspace_id) == second
    with psycopg.connect(MIGRATOR_DSN) as connection:
        revisions = connection.execute(
            """
            SELECT version, asset_limit, generation_retention_seconds
            FROM schemabridge_control.tenant_capacity_policy_revisions
            WHERE workspace_id = %s
            ORDER BY version
            """,
            (workspace_id,),
        ).fetchall()
        assert revisions == [(1, 100_000, 1_800), (2, 10_000_000, 3_600)]
        with pytest.raises(psycopg.Error):
            connection.execute(
                """
                UPDATE schemabridge_control.tenant_capacity_policy_revisions
                SET asset_limit = 1
                WHERE workspace_id = %s AND version = 1
                """,
                (workspace_id,),
            )


def test_registration_replay_listing_and_disable_idempotency() -> None:
    workspace_id = _workspace()
    _policy(workspace_id)
    store = PostgresCatalogConnectionStore(API_DSN)

    first = store.register(
        _registration(workspace_id, "connection_alpha", key="registration-alpha")
    )
    replay = store.register(
        _registration(workspace_id, "connection_alpha", key="registration-alpha")
    )
    store.register(
        _registration(
            workspace_id,
            "connection_beta",
            key="registration-beta",
            display_name="Beta warehouse",
        )
    )

    assert not first.replayed
    assert replay.replayed
    assert first.connection == replay.connection
    assert not hasattr(store, "load_route")

    page_one = store.list_connections(
        workspace_id,
        filters=CatalogConnectionFilter(),
        page_size=1,
        after=None,
    )
    assert page_one.rows_read == 2
    assert page_one.has_more
    assert page_one.last_key is not None
    page_two = store.list_connections(
        workspace_id,
        filters=CatalogConnectionFilter(),
        page_size=1,
        after=page_one.last_key,
    )
    assert len(page_two.items) == 1
    assert page_two.items[0].connection_id != page_one.items[0].connection_id

    disable = CatalogConnectionDisable(
        workspace_id=workspace_id,
        connection_id=CatalogConnectionId("connection_alpha"),
        requested_by="actor_platform_admin",
        requested_at=NOW + timedelta(seconds=1),
        idempotency_digest=_digest("disable-alpha"),
    )
    disabled = store.disable(disable)
    replayed_disable = store.disable(disable)
    assert disabled.status is CatalogConnectionStatus.DISABLED
    assert replayed_disable == disabled

    conflicting_disable = disable.model_copy(
        update={"idempotency_digest": _digest("another-disable-key")}
    )
    with pytest.raises(CatalogInventoryError) as conflict:
        store.disable(conflicting_disable)
    assert conflict.value.code is CatalogInventoryErrorCode.IDEMPOTENCY_CONFLICT


def test_connection_policy_changes_capacity_without_code_or_restart() -> None:
    workspace_id = _workspace()
    _policy(workspace_id, connection_limit=1)
    store = PostgresCatalogConnectionStore(API_DSN)
    registration = _registration(workspace_id, "connection_one", key="register-one")
    first = store.register(registration)
    replay = store.register(registration)
    assert not first.replayed
    assert replay.replayed
    assert replay.connection == first.connection

    conflicting_registration = registration.model_copy(
        update={"platform_instance": "warehouse-secondary"}
    )
    with pytest.raises(CatalogInventoryError) as conflict:
        store.register(conflicting_registration)
    assert conflict.value.code is CatalogInventoryErrorCode.IDEMPOTENCY_CONFLICT

    with pytest.raises(CatalogInventoryError) as exhausted:
        store.register(_registration(workspace_id, "connection_two", key="register-two"))
    assert exhausted.value.code is CatalogInventoryErrorCode.CAPACITY_EXCEEDED

    with psycopg.connect(MIGRATOR_DSN) as connection:
        connection.execute(
            """
            UPDATE schemabridge_control.tenant_capacity_policies
            SET connection_limit = 2,
                version = version + 1,
                updated_by = 'actor_capacity_admin',
                updated_at = clock_timestamp()
            WHERE workspace_id = %s
            """,
            (workspace_id,),
        )

    second = store.register(_registration(workspace_id, "connection_two", key="register-two"))
    assert second.connection.connection_id == CatalogConnectionId("connection_two")


def test_concurrent_registration_admits_one_connection_without_a_legacy_route() -> None:
    workspace_id = _workspace()
    _policy(workspace_id, connection_limit=1)
    registration = _registration(
        workspace_id,
        "connection_concurrent",
        key="register-concurrent",
    )

    def register() -> bool:
        return PostgresCatalogConnectionStore(API_DSN).register(registration).replayed

    with ThreadPoolExecutor(max_workers=8) as executor:
        replayed = list(executor.map(lambda _: register(), range(8)))

    assert replayed.count(False) == 1
    assert replayed.count(True) == 7
    with psycopg.connect(MIGRATOR_DSN) as connection:
        counts = connection.execute(
            """
            SELECT
                (
                    SELECT count(*)
                    FROM schemabridge_control.catalog_connections
                    WHERE workspace_id = %s
                ),
                (
                    SELECT count(*)
                    FROM schemabridge_control.catalog_connection_routes
                    WHERE workspace_id = %s
                )
            """,
            (workspace_id, workspace_id),
        ).fetchone()
    assert counts == (1, 0)


def test_distributed_rate_limit_is_principal_and_tenant_scoped() -> None:
    workspace_id = _workspace()
    other_workspace = _workspace()
    _policy(workspace_id, request_limit=3)
    _policy(other_workspace, request_limit=3)
    capacity = PostgresTenantCapacityStore(API_DSN)

    results = [capacity.admit_api_request(workspace_id, "actor_primary") for _ in range(4)]
    assert [item.allowed for item in results] == [True, True, True, False]
    assert [(item.used, item.limit) for item in results] == [
        (1, 3),
        (2, 3),
        (3, 3),
        (3, 3),
    ]
    assert 1 <= (results[-1].retry_after_seconds or 0) <= 60
    assert capacity.admit_api_request(workspace_id, "actor_other").allowed
    assert capacity.admit_api_request(other_workspace, "actor_primary").allowed

    with psycopg.connect(MIGRATOR_DSN) as connection:
        connection.execute(
            """
            UPDATE schemabridge_control.tenant_capacity_policies
            SET api_requests_per_minute = 1,
                version = version + 1,
                updated_by = 'actor_capacity_admin',
                updated_at = clock_timestamp()
            WHERE workspace_id = %s
            """,
            (workspace_id,),
        )
    lowered = capacity.admit_api_request(workspace_id, "actor_primary")
    assert not lowered.allowed
    assert lowered.used == 3
    assert lowered.limit == 1

    snapshot = capacity.inspect(workspace_id)
    assert snapshot.policy.api_requests_per_minute == 1
    assert snapshot.usage.connection_count == 0
