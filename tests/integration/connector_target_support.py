"""Small deterministic v9 connector target fixture for catalog integration tests."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg

from schemabridge.adapters.catalog.postgres_connector_routing import (
    PostgresCatalogConnectorRouteReader,
)
from schemabridge.adapters.catalog.postgres_refresh import PostgresCatalogRefreshStore
from schemabridge.application.ports.catalog_inventory import (
    ManagedCatalogConnectorRoute,
)
from schemabridge.domain.catalog_inventory import (
    CatalogConnectionId,
    CatalogRefreshCommand,
    CatalogRefreshId,
    CatalogRefreshMode,
    CatalogRefreshState,
    CatalogSourcePage,
)
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    QueryCostBudget,
    SourceConnectorKind,
    SourceDialect,
    postgres_type_contract_fingerprint,
)

_APPLY_ROUTE_SQL = (
    "SELECT * FROM schemabridge_control.apply_connector_route_change_v2("
    + ", ".join(["%s"] * 39)
    + ")"
)


@dataclass(frozen=True, slots=True)
class CatalogTargetFacts:
    """Public connector facts required by atomic catalog promotion."""

    contract_version: int
    route_revision: int
    target_fingerprint: str
    source_identity_fingerprint: str
    catalog_identity_fingerprint: str
    type_contract_fingerprint: str


def ensure_catalog_connector_target(
    migrator_dsn: str,
    *,
    workspace_id: str,
    connection_id: CatalogConnectionId,
) -> CatalogTargetFacts:
    """Return or create the one deterministic enabled test connector target."""

    with psycopg.connect(migrator_dsn) as connection:
        existing = connection.execute(
            """
            SELECT
                head.contract_version,
                head.route_revision,
                head.target_fingerprint,
                head.status,
                contract.catalog_identity_fingerprint,
                contract.source_identity_fingerprint,
                contract.type_contract_fingerprint
            FROM schemabridge_control.connector_route_heads AS head
            JOIN schemabridge_control.connector_contract_revisions AS contract
              ON contract.workspace_id = head.workspace_id
             AND contract.connection_id = head.connection_id
             AND contract.contract_version = head.contract_version
            WHERE head.workspace_id = %s
              AND head.connection_id = %s
            """,
            (workspace_id, connection_id.root),
        ).fetchone()
    if existing is not None:
        if existing[3] != "enabled":
            raise AssertionError("catalog connector target fixture is disabled")
        return CatalogTargetFacts(
            contract_version=int(existing[0]),
            route_revision=int(existing[1]),
            target_fingerprint=str(existing[2]).strip(),
            source_identity_fingerprint=str(existing[5]).strip(),
            catalog_identity_fingerprint=str(existing[4]).strip(),
            type_contract_fingerprint=str(existing[6]).strip(),
        )

    label = _digest(f"{workspace_id}|{connection_id.root}")[:20]
    budget = QueryCostBudget(
        explain_timeout_ms=2_500,
        max_response_bytes=262_144,
        max_total_cost=Decimal("12345.67"),
        max_estimated_rows=250_000,
        max_plan_nodes=500,
        max_plan_depth=32,
        max_plan_width=8_192,
    )
    target = GovernedExecutionTarget(
        workspace_id=workspace_id,
        connection_id=connection_id,
        connector_kind=SourceConnectorKind.POSTGRESQL,
        dialect=SourceDialect.POSTGRESQL,
        route_revision=1,
        route_fingerprint=_digest(f"catalog-test-route:{label}"),
        expected_reader="schemabridge_source_reader",
        source_identity_fingerprint=_digest(f"catalog-test-source:{label}"),
        catalog_identity_fingerprint=_digest(f"catalog-test-origin:{label}"),
        type_contract_fingerprint=postgres_type_contract_fingerprint(),
        cost_budget=budget,
        cost_budget_fingerprint=budget.fingerprint,
    )
    with psycopg.connect(migrator_dsn) as connection:
        applied = connection.execute(
            _APPLY_ROUTE_SQL,
            (
                workspace_id,
                connection_id.root,
                "create",
                0,
                1,
                target.route_revision,
                target.route_fingerprint,
                target.expected_reader,
                1,
                target.type_contract_fingerprint,
                target.source_identity_fingerprint,
                target.catalog_identity_fingerprint,
                budget.explain_timeout_ms,
                budget.max_response_bytes,
                budget.max_total_cost,
                budget.max_estimated_rows,
                budget.max_plan_nodes,
                budget.max_plan_depth,
                budget.max_plan_width,
                budget.fingerprint,
                _digest(f"catalog-test-contract:{label}"),
                target.fingerprint,
                f"test.preflight.{label}",
                f"test.catalog.{label}",
                f"test.execution.{label}",
                f"test.profile.{label}",
                101,
                202,
                303,
                404,
                _digest(f"catalog-test-proposal:{label}"),
                f"approval-catalog-test-{label}",
                _digest(f"catalog-test-approval:{label}"),
                "sb_platform_admin_v1",
                _digest(f"catalog-test-idempotency:{label}"),
                "connector_route_audit_" + _digest(f"catalog-test-audit-id:{label}"),
                _digest(f"catalog-test-audit:{label}"),
                _digest(f"catalog-test-head:{label}"),
                "CREATE CONNECTOR ROUTE",
            ),
        ).fetchone()
    if applied is None:
        raise AssertionError("catalog connector target fixture was not created")
    return CatalogTargetFacts(
        contract_version=1,
        route_revision=target.route_revision,
        target_fingerprint=target.fingerprint,
        source_identity_fingerprint=target.source_identity_fingerprint,
        catalog_identity_fingerprint=target.catalog_identity_fingerprint,
        type_contract_fingerprint=target.type_contract_fingerprint,
    )


def acquire_catalog_refresh_route(
    catalog_dsn: str,
    *,
    workspace_id: str,
    connection_id: CatalogConnectionId,
    refresh_id: CatalogRefreshId,
    indexer_id: str,
    lease_capability: str,
    fencing_token: int,
) -> ManagedCatalogConnectorRoute:
    """Acquire the exact lease-owned v9 route and persist its semantic binding."""

    route = PostgresCatalogConnectorRouteReader(catalog_dsn).load_route(
        workspace_id,
        connection_id,
        refresh_id=refresh_id,
        indexer_id=indexer_id,
        lease_capability=lease_capability,
        fencing_token=fencing_token,
    )
    if route is None:
        raise AssertionError("catalog connector route fixture is unavailable")
    return route


def ensure_compatible_catalog_generation(
    migrator_dsn: str,
    api_dsn: str,
    catalog_dsn: str,
    *,
    workspace_id: str,
    connection_id: CatalogConnectionId,
    target: CatalogTargetFacts,
) -> CatalogRefreshState | None:
    """Promote one empty full generation through the real v9 catalog workflow."""

    with psycopg.connect(migrator_dsn) as connection:
        compatible = connection.execute(
            """
            SELECT generation.generation
            FROM schemabridge_control.catalog_connections AS catalog_connection
            JOIN schemabridge_control.catalog_generations AS generation
              ON generation.workspace_id = catalog_connection.workspace_id
             AND generation.connection_id = catalog_connection.connection_id
             AND generation.generation = catalog_connection.active_generation
            WHERE catalog_connection.workspace_id = %s
              AND catalog_connection.connection_id = %s
              AND generation.status = 'completed'
              AND generation.source_identity_fingerprint = %s
              AND generation.catalog_identity_fingerprint = %s
              AND generation.type_contract_fingerprint = %s
            """,
            (
                workspace_id,
                connection_id.root,
                target.source_identity_fingerprint,
                target.catalog_identity_fingerprint,
                target.type_contract_fingerprint,
            ),
        ).fetchone()
    if compatible is not None:
        return None

    label = _digest(f"{workspace_id}|{connection_id.root}|{target.target_fingerprint}")
    api_store = PostgresCatalogRefreshStore(
        api_dsn,
        application_name="schemabridge-control-api",
    )
    catalog_store = PostgresCatalogRefreshStore(catalog_dsn)
    requested = api_store.request(
        CatalogRefreshCommand(
            workspace_id=workspace_id,
            connection_id=connection_id,
            mode=CatalogRefreshMode.FULL,
            requested_by="actor_catalog_test_support",
            requested_at=datetime.now(UTC),
            idempotency_digest=_digest(f"catalog-test-generation:{label}"),
        )
    )
    indexer_id = f"catalog:test-support-{label[:16]}"
    capability = f"catalog-test-support-capability-{label}"
    claimed = catalog_store.claim_next(
        indexer_id=indexer_id,
        lease_capability=capability,
        lease_duration=timedelta(minutes=2),
    )
    if claimed is None or claimed.lease is None:
        raise AssertionError("catalog generation fixture lease is unavailable")
    if claimed.refresh_id != requested.refresh.refresh_id:
        raise AssertionError("catalog generation fixture claimed another refresh")
    acquire_catalog_refresh_route(
        catalog_dsn,
        workspace_id=workspace_id,
        connection_id=connection_id,
        refresh_id=claimed.refresh_id,
        indexer_id=indexer_id,
        lease_capability=capability,
        fencing_token=claimed.lease.fencing_token,
    )
    staging = catalog_store.begin_staging(
        workspace_id,
        claimed.refresh_id,
        indexer_id=indexer_id,
        lease_capability=capability,
        fencing_token=claimed.lease.fencing_token,
    )
    catalog_store.persist_page(
        workspace_id,
        claimed.refresh_id,
        indexer_id=indexer_id,
        lease_capability=capability,
        fencing_token=claimed.lease.fencing_token,
        page=CatalogSourcePage.create(
            mode=CatalogRefreshMode.FULL,
            sequence=1,
            changes=(),
            next_checkpoint=None,
            source_complete=True,
        ),
    )
    return catalog_store.complete(
        workspace_id,
        claimed.refresh_id,
        indexer_id=indexer_id,
        lease_capability=capability,
        fencing_token=claimed.lease.fencing_token,
        expected_base_generation=staging.base_generation,
        expected_contract_version=target.contract_version,
        expected_route_revision=target.route_revision,
        expected_target_fingerprint=target.target_fingerprint,
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
