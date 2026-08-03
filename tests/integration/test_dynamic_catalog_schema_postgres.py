"""PostgreSQL catalog schema, CAS, admission, and eight-role privilege proof."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from tests.integration.connector_target_support import (
    ensure_catalog_connector_target,
    ensure_compatible_catalog_generation,
)

from schemabridge.adapters.catalog.postgres_governed_search import (
    PostgresGovernedBindingFactsSearch,
)
from schemabridge.adapters.catalog.postgres_inventory import (
    PostgresCatalogConnectionStore,
)
from schemabridge.adapters.connectors.postgres_routing import (
    PostgresExecutionTargetResolver,
)
from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.application.ports.control_plane_migrations import (
    ControlPlaneMigrationError,
    ControlPlaneMigrationErrorCode,
)
from schemabridge.domain.catalog_inventory import (
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogConnectionRegistration,
)
from schemabridge.domain.connectors import GovernedExecutionTarget
from schemabridge.domain.query_studio import (
    DescriptionQuery,
    GovernedBindingFactsRequest,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations/control_plane"
ADMIN_DSN = "postgresql://postgres:local-only-not-a-secret@127.0.0.1:55434/postgres"


@dataclass(frozen=True)
class _DatabaseUrls:
    database: str
    migrator: str
    runtime: str
    reconciler: str
    api: str
    worker: str
    catalog: str
    observer: str
    backup: str


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
        observer=_role_dsn("schemabridge_observer", database),
        backup=_role_dsn("schemabridge_backup", database),
    )


def _admin_dsn() -> str:
    return os.environ.get(
        "SCHEMABRIDGE_TEST_CONTROL_ADMIN_DATABASE_URL",
        ADMIN_DSN,
    )


def _create_database(database: str) -> _DatabaseUrls:
    urls = _database_urls(database)
    with psycopg.connect(_admin_dsn(), autocommit=True) as connection:
        catalog_role = connection.execute(
            "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'schemabridge_catalog'"
        ).fetchone()
        if catalog_role is None:
            pytest.fail(
                "schemabridge_catalog role is missing; recreate the M25 control-plane service"
            )
        observer_role = connection.execute(
            "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'schemabridge_observer'"
        ).fetchone()
        if observer_role is None:
            pytest.fail(
                "schemabridge_observer role is missing; recreate the M29 control-plane service"
            )
        backup_role = connection.execute(
            "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'schemabridge_backup'"
        ).fetchone()
        if backup_role is None:
            pytest.fail(
                "schemabridge_backup role is missing; recreate the M29 control-plane service"
            )
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
                    schemabridge_runtime,
                    schemabridge_reconciler,
                    schemabridge_api,
                    schemabridge_worker,
                    schemabridge_catalog,
                    schemabridge_observer,
                    schemabridge_backup
                """
            ).format(sql.Identifier(database))
        )
    return urls


def _drop_database(database: str) -> None:
    with psycopg.connect(_admin_dsn(), autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
        )


@pytest.fixture(scope="module")
def catalog_database(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_DatabaseUrls]:
    database = f"schemabridge_catalog_v4_{uuid4().hex[:12]}"
    urls = _create_database(database)
    try:
        v3_migrations = tmp_path_factory.mktemp("control-v3")
        for version in (1, 2, 3):
            source = next(MIGRATIONS.glob(f"{version:04d}_*.sql"))
            shutil.copyfile(source, v3_migrations / source.name)
        v3 = PostgresControlPlaneMigrator(urls.migrator, v3_migrations).migrate()
        assert v3.inspection.current_version == 3
        with pytest.raises(ControlPlaneMigrationError) as stale:
            PostgresControlPlaneMigrator(urls.migrator, MIGRATIONS).require_current()
        assert stale.value.code is ControlPlaneMigrationErrorCode.SCHEMA_NOT_CURRENT
        upgraded = PostgresControlPlaneMigrator(urls.migrator, MIGRATIONS).migrate()
        assert upgraded.applied_versions == (4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14)
        assert upgraded.inspection.current_version == 14
        yield urls
    finally:
        _drop_database(database)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _insert_policy(
    dsn: str,
    workspace_id: str,
    *,
    request_limit: int = 3,
    job_limit: int = 2,
) -> None:
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.tenant_capacity_policies (
                workspace_id,
                connection_limit,
                asset_limit,
                field_limit,
                api_requests_per_minute,
                api_window_seconds,
                nonterminal_job_limit,
                catalog_cursor_ttl_seconds,
                generation_retention_seconds,
                version,
                updated_by,
                created_at,
                updated_at
            ) VALUES (
                %s, 10, 10000, 100000, %s, 60, %s, 900, 1800, 1,
                'sb_platform_admin_v1', clock_timestamp(), clock_timestamp()
            )
            """,
            (workspace_id, request_limit, job_limit),
        )


def _execution_target(
    urls: _DatabaseUrls,
    *,
    workspace_id: str,
) -> GovernedExecutionTarget:
    connection_id = CatalogConnectionId("connection_dynamic_job_capacity")
    PostgresCatalogConnectionStore(
        urls.api,
        application_name="schemabridge-control-api",
    ).register(
        CatalogConnectionRegistration(
            workspace_id=workspace_id,
            connection_id=connection_id,
            display_name="Dynamic catalog job capacity source",
            kind=CatalogConnectionKind.SYNTHETIC,
            environment="TEST",
            catalog_scope="dynamic-job-capacity",
            requested_by="sb_platform_admin_v1",
            requested_at=datetime.now(UTC),
            idempotency_digest=_digest(f"register-job-target:{workspace_id}"),
        )
    )
    facts = ensure_catalog_connector_target(
        urls.migrator,
        workspace_id=workspace_id,
        connection_id=connection_id,
    )
    ensure_compatible_catalog_generation(
        urls.migrator,
        urls.api,
        urls.catalog,
        workspace_id=workspace_id,
        connection_id=connection_id,
        target=facts,
    )
    target = PostgresExecutionTargetResolver(urls.runtime).resolve_current(
        workspace_id=workspace_id,
        connection_id=connection_id,
    )
    assert target.route_revision == facts.route_revision
    assert target.fingerprint == facts.target_fingerprint
    return target


def test_pristine_schema_reaches_exact_current_version() -> None:
    database = f"schemabridge_catalog_pristine_{uuid4().hex[:12]}"
    urls = _create_database(database)
    try:
        migrated = PostgresControlPlaneMigrator(urls.migrator, MIGRATIONS).migrate()
        assert migrated.applied_versions == (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14)
        assert migrated.inspection.current_version == 14
        with psycopg.connect(urls.migrator) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT tablename
                    FROM pg_catalog.pg_tables
                    WHERE schemaname = 'schemabridge_control'
                    """
                )
            }
        assert {
            "tenant_capacity_policies",
            "api_rate_limit_windows",
            "tenant_execution_capacity",
            "tenant_job_schedule",
            "catalog_connections",
            "catalog_connection_routes",
            "catalog_refresh_runs",
            "catalog_generations",
            "catalog_assets",
            "catalog_fields",
            "catalog_tombstones",
            "catalog_generation_changes",
            "semantic_resource_bindings",
            "semantic_join_profiles",
            "semantic_change_reports",
            "semantic_change_findings",
            "semantic_change_impacts",
            "semantic_change_resolutions",
            "semantic_artifact_dependencies",
            "semantic_dependency_index_states",
            "semantic_change_heads",
            "semantic_change_scan_requests",
            "connector_private_route_secret_versions",
        } <= tables
    finally:
        _drop_database(database)


def test_eight_role_matrix_is_closed_and_catalog_cannot_activate_directly(
    catalog_database: _DatabaseUrls,
) -> None:
    roles = {
        "migrator": catalog_database.migrator,
        "runtime": catalog_database.runtime,
        "reconciler": catalog_database.reconciler,
        "api": catalog_database.api,
        "worker": catalog_database.worker,
        "catalog": catalog_database.catalog,
        "observer": catalog_database.observer,
        "backup": catalog_database.backup,
    }
    expected = {
        "migrator": (True, True, True, True, True, True, True, True),
        "runtime": (False, False, False, False, False, False, False, False),
        "reconciler": (False, False, False, False, False, False, False, False),
        "api": (True, False, True, True, False, True, True, False),
        "worker": (True, False, False, False, False, False, True, True),
        "catalog": (True, False, True, False, True, True, False, False),
        "observer": (False, False, False, False, False, False, False, False),
        "backup": (True, True, True, False, False, True, True, False),
    }
    query = """
        SELECT
            has_table_privilege(
                current_user,
                'schemabridge_control.tenant_capacity_policies',
                'SELECT'
            ),
            has_table_privilege(
                current_user,
                'schemabridge_control.catalog_connection_routes',
                'SELECT'
            ),
            has_table_privilege(
                current_user,
                'schemabridge_control.catalog_connections',
                'SELECT'
            ),
            has_table_privilege(
                current_user,
                'schemabridge_control.catalog_connections',
                'INSERT'
            ),
            has_column_privilege(
                current_user,
                'schemabridge_control.catalog_refresh_runs',
                'status',
                'UPDATE'
            ),
            has_table_privilege(
                current_user,
                'schemabridge_control.catalog_assets',
                'SELECT'
            ),
            has_table_privilege(
                current_user,
                'schemabridge_control.tenant_job_schedule',
                'SELECT'
            ),
            has_column_privilege(
                current_user,
                'schemabridge_control.tenant_job_schedule',
                'claim_sequence',
                'UPDATE'
            )
    """
    for role, dsn in roles.items():
        with psycopg.connect(dsn) as connection:
            row = connection.execute(query).fetchone()
            properties = connection.execute(
                """
                SELECT rolsuper, rolcreatedb, rolcreaterole, rolinherit
                FROM pg_catalog.pg_roles
                WHERE rolname = current_user
                """
            ).fetchone()
        assert row == expected[role]
        assert properties == (False, False, False, False)

    function_expected = {
        "migrator": (True, True, True, True, True, True, True, True, True),
        "runtime": (False, False, False, False, False, False, False, False, False),
        "reconciler": (False, False, False, False, False, False, False, False, False),
        "api": (True, True, True, True, False, False, False, False, False),
        "worker": (False, False, False, False, False, False, False, False, False),
        "catalog": (False, False, False, False, False, True, True, True, True),
        "observer": (False, False, False, False, False, False, False, False, False),
        "backup": (False, False, False, False, False, False, False, False, False),
    }
    for role, dsn in roles.items():
        with psycopg.connect(dsn) as connection:
            row = connection.execute(
                """
                SELECT
                    has_function_privilege(
                        current_user,
                        'schemabridge_control.admit_api_request(varchar,char,varchar)',
                        'EXECUTE'
                    ),
                    has_function_privilege(
                        current_user,
                        'schemabridge_control.admit_catalog_connection(varchar)',
                        'EXECUTE'
                    ),
                    has_function_privilege(
                        current_user,
                        'schemabridge_control.request_catalog_refresh('
                        'varchar,varchar,varchar,varchar,char,char,varchar,timestamptz)',
                        'EXECUTE'
                    ),
                    has_function_privilege(
                        current_user,
                        'schemabridge_control.load_catalog_refresh_public(varchar,varchar)',
                        'EXECUTE'
                    ),
                    has_function_privilege(
                        current_user,
                        'schemabridge_control.load_owned_catalog_connector_route('
                        'varchar,varchar,varchar,varchar,varchar,bigint,'
                        'bigint,bigint,char)',
                        'EXECUTE'
                    ),
                    has_function_privilege(
                        current_user,
                        'schemabridge_control.load_owned_catalog_connector_route_v2('
                        'varchar,varchar,varchar,varchar,varchar,bigint,'
                        'bigint,bigint,char)',
                        'EXECUTE'
                    ),
                    has_function_privilege(
                        current_user,
                        'schemabridge_control.lock_catalog_completion_scope('
                        'varchar,varchar,varchar,bigint,bigint,char)',
                        'EXECUTE'
                    ),
                    has_function_privilege(
                        current_user,
                        'schemabridge_control.activate_catalog_generation_for_target('
                        'varchar,varchar,varchar,varchar,varchar,bigint,'
                        'bigint,bigint,char,bigint,bigint,char)',
                        'EXECUTE'
                    ),
                    has_function_privilege(
                        current_user,
                        'schemabridge_control.prune_catalog_generations('
                        'varchar,varchar,integer)',
                        'EXECUTE'
                    )
                """
            ).fetchone()
        assert row == function_expected[role]

    table_names = (
        "tenant_capacity_policies",
        "api_rate_limit_windows",
        "tenant_execution_capacity",
        "tenant_job_schedule",
        "catalog_connections",
        "catalog_connection_routes",
        "catalog_refresh_runs",
        "catalog_generations",
        "catalog_assets",
        "catalog_fields",
        "catalog_tombstones",
    )
    table_actions = ("SELECT", "INSERT", "UPDATE", "DELETE")
    api_allowed = {
        ("tenant_capacity_policies", "SELECT"),
        ("tenant_execution_capacity", "SELECT"),
        ("tenant_job_schedule", "SELECT"),
        ("catalog_connections", "SELECT"),
        ("catalog_connections", "INSERT"),
        ("catalog_generations", "SELECT"),
        ("catalog_assets", "SELECT"),
        ("catalog_fields", "SELECT"),
    }
    worker_allowed = {
        ("tenant_capacity_policies", "SELECT"),
        ("tenant_execution_capacity", "SELECT"),
        ("tenant_job_schedule", "SELECT"),
        ("tenant_job_schedule", "INSERT"),
    }
    catalog_allowed = {
        ("tenant_capacity_policies", "SELECT"),
        ("catalog_connections", "SELECT"),
        ("catalog_refresh_runs", "SELECT"),
        ("catalog_refresh_runs", "UPDATE"),
        ("catalog_generations", "SELECT"),
        ("catalog_generations", "INSERT"),
        ("catalog_assets", "SELECT"),
        ("catalog_assets", "INSERT"),
        ("catalog_assets", "UPDATE"),
        ("catalog_assets", "DELETE"),
        ("catalog_fields", "SELECT"),
        ("catalog_fields", "INSERT"),
        ("catalog_fields", "UPDATE"),
        ("catalog_fields", "DELETE"),
        ("catalog_tombstones", "SELECT"),
        ("catalog_tombstones", "INSERT"),
    }
    expected_allowed = {
        "migrator": {
            (table_name, action) for table_name in table_names for action in table_actions
        },
        "runtime": set(),
        "reconciler": set(),
        "api": api_allowed,
        "worker": worker_allowed,
        "catalog": catalog_allowed,
        "observer": set(),
        "backup": {(table_name, "SELECT") for table_name in table_names},
    }
    expected_memberships = {
        role: {"schemabridge_observer"} if role == "migrator" else set() for role in roles
    }
    for role, dsn in roles.items():
        with psycopg.connect(dsn) as connection:
            observed = {
                (table_name, action)
                for table_name, action, allowed in connection.execute(
                    """
                    SELECT
                        table_name,
                        action,
                        has_table_privilege(
                            current_user,
                            'schemabridge_control.' || table_name,
                            action
                        )
                    FROM unnest(%s::text[]) AS table_names(table_name)
                    CROSS JOIN unnest(%s::text[]) AS actions(action)
                    """,
                    (list(table_names), list(table_actions)),
                )
                if allowed
            }
            memberships = connection.execute(
                """
                SELECT target_role,
                       pg_has_role(current_user, target_role, 'MEMBER')
                FROM unnest(%s::text[]) AS targets(target_role)
                WHERE target_role <> current_user
                """,
                (
                    [
                        "schemabridge_migrator",
                        "schemabridge_runtime",
                        "schemabridge_reconciler",
                        "schemabridge_api",
                        "schemabridge_worker",
                        "schemabridge_catalog",
                        "schemabridge_observer",
                        "schemabridge_backup",
                    ],
                ),
            ).fetchall()
        assert observed == expected_allowed[role]
        assert {
            target_role for target_role, is_member in memberships if is_member
        } == expected_memberships[role]

    with (
        psycopg.connect(catalog_database.catalog) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        connection.execute(
            """
            UPDATE schemabridge_control.catalog_connections
            SET active_generation = 1
            WHERE false
            """
        )


def test_v5_semantic_change_privileges_keep_the_eight_role_boundary(
    catalog_database: _DatabaseUrls,
) -> None:
    roles = {
        "migrator": catalog_database.migrator,
        "runtime": catalog_database.runtime,
        "reconciler": catalog_database.reconciler,
        "api": catalog_database.api,
        "worker": catalog_database.worker,
        "catalog": catalog_database.catalog,
        "observer": catalog_database.observer,
        "backup": catalog_database.backup,
    }
    tables = (
        "catalog_generation_changes",
        "semantic_resource_bindings",
        "semantic_join_profiles",
        "semantic_change_reports",
        "semantic_change_findings",
        "semantic_change_impacts",
        "semantic_change_resolutions",
        "semantic_artifact_dependencies",
        "semantic_dependency_index_states",
        "semantic_change_heads",
        "semantic_change_scan_requests",
    )
    actions = ("SELECT", "INSERT", "UPDATE", "DELETE")
    reconciler_allowed = (
        {(table, "SELECT") for table in tables}
        | {
            (table, "INSERT")
            for table in tables
            if table not in {"catalog_generation_changes", "semantic_change_scan_requests"}
        }
        | {
            ("semantic_change_heads", "UPDATE"),
            ("semantic_dependency_index_states", "UPDATE"),
        }
    )
    expected_allowed = {
        "migrator": {(table, action) for table in tables for action in actions},
        "runtime": set(),
        "reconciler": reconciler_allowed,
        "api": set(),
        "worker": set(),
        "catalog": set(),
        "observer": set(),
        "backup": {(table, "SELECT") for table in tables},
    }
    view_names = (
        "semantic_catalog_evidence_projection",
        "semantic_catalog_connection_evidence_projection",
        "semantic_context_gate_projection",
        "semantic_change_report_public",
        "semantic_change_finding_public",
        "semantic_change_impact_public",
    )
    expected_views = {
        "migrator": set(view_names),
        "runtime": {"semantic_context_gate_projection"},
        "reconciler": {
            "semantic_catalog_evidence_projection",
            "semantic_catalog_connection_evidence_projection",
        },
        "api": {
            "semantic_change_report_public",
            "semantic_change_finding_public",
            "semantic_change_impact_public",
        },
        "worker": {"semantic_context_gate_projection"},
        "catalog": set(),
        "observer": set(),
        "backup": set(view_names),
    }
    for role, dsn in roles.items():
        with psycopg.connect(dsn) as connection:
            observed = {
                (table, action)
                for table, action, allowed in connection.execute(
                    """
                    SELECT
                        table_name,
                        action,
                        has_table_privilege(
                            current_user,
                            'schemabridge_control.' || table_name,
                            action
                        )
                    FROM unnest(%s::text[]) AS tables(table_name)
                    CROSS JOIN unnest(%s::text[]) AS actions(action)
                    """,
                    (list(tables), list(actions)),
                )
                if allowed
            }
            readable_views = {
                view
                for view, allowed in connection.execute(
                    """
                    SELECT
                        view_name,
                        has_table_privilege(
                            current_user,
                            'schemabridge_control.' || view_name,
                            'SELECT'
                        )
                    FROM unnest(%s::text[]) AS views(view_name)
                    """,
                    (list(view_names),),
                )
                if allowed
            }
            scan_update = connection.execute(
                """
                SELECT has_column_privilege(
                    current_user,
                    'schemabridge_control.semantic_change_scan_requests',
                    'status',
                    'UPDATE'
                )
                """
            ).fetchone()
            function_capabilities = connection.execute(
                """
                SELECT
                    has_function_privilege(
                        current_user,
                        'schemabridge_control.semantic_catalog_asset_locator_key(character varying,character varying,bigint,character varying)',
                        'EXECUTE'
                    ),
                    has_function_privilege(
                        current_user,
                        'schemabridge_control.semantic_catalog_field_locator_key(character varying,character varying,bigint,character,character)',
                        'EXECUTE'
                    ),
                    has_function_privilege(
                        current_user,
                        'schemabridge_control.load_semantic_initial_catalog_candidates(character varying,character varying,jsonb)',
                        'EXECUTE'
                    ),
                    has_function_privilege(
                        current_user,
                        'schemabridge_control.load_semantic_bound_catalog_evidence(character varying,character varying,jsonb)',
                        'EXECUTE'
                    ),
                    has_function_privilege(
                        current_user,
                        'schemabridge_control.capture_catalog_generation_change()',
                        'EXECUTE'
                    ),
                    has_function_privilege(
                        current_user,
                        'schemabridge_control.enqueue_registry_semantic_scan()',
                        'EXECUTE'
                    )
                """
            ).fetchone()
            audit_capability = connection.execute(
                """
                SELECT
                    has_table_privilege(
                        current_user,
                        'schemabridge_control.control_audit_events',
                        'SELECT'
                    ),
                    has_table_privilege(
                        current_user,
                        'schemabridge_control.control_audit_events',
                        'INSERT'
                    ),
                    has_sequence_privilege(
                        current_user,
                        'schemabridge_control.control_audit_events_sequence_seq',
                        'USAGE'
                    )
                """
            ).fetchone()
        assert observed == expected_allowed[role]
        assert readable_views == expected_views[role]
        assert scan_update == (role in {"migrator", "reconciler"},)
        assert function_capabilities == (
            role in {"migrator", "catalog"},
            role in {"migrator", "catalog"},
            role in {"migrator", "reconciler"},
            role in {"migrator", "reconciler"},
            role == "migrator",
            role == "migrator",
        )
        assert audit_capability == (
            role in {"migrator", "runtime", "reconciler", "backup"},
            role in {"migrator", "runtime", "reconciler"},
            role in {"migrator", "runtime", "reconciler"},
        )

    workspace_id = f"workspace-audit-{uuid4().hex[:12]}"
    event_id = f"semantic-audit-{uuid4().hex}"
    event_hash = _digest(event_id)
    with psycopg.connect(catalog_database.reconciler) as connection:
        appended = connection.execute(
            """
            INSERT INTO schemabridge_control.control_audit_events (
                event_id,
                workspace_id,
                operation,
                transition_id,
                previous_hash,
                event_hash,
                payload_fingerprint,
                key_version,
                event_json,
                occurred_at
            ) VALUES (
                %s, %s, 'semantic_change_decision', NULL, NULL,
                %s, %s, 'v1', '{}'::jsonb, clock_timestamp()
            )
            RETURNING event_id, sequence
            """,
            (event_id, workspace_id, event_hash, _digest("semantic-audit-payload")),
        ).fetchone()
    assert appended is not None
    assert appended[0] == event_id
    assert appended[1] > 0


def test_v5_registry_pointer_activation_enqueues_exact_idempotent_scans(
    catalog_database: _DatabaseUrls,
) -> None:
    workspace_id = f"workspace-registry-{uuid4().hex[:12]}"
    catalog_scope = "synthetic-test"
    registry_id = "synthetic_registry"
    first_transition = f"transition-{uuid4().hex}"
    second_transition = f"transition-{uuid4().hex}"
    first_fingerprint = _digest("registry-v1")
    second_fingerprint = _digest("registry-v2")

    with psycopg.connect(catalog_database.runtime) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.registry_activation_transitions (
                transition_id,
                workspace_id,
                catalog_scope,
                registry_id,
                generation,
                action,
                expected_generation,
                expected_registry_version,
                expected_registry_fingerprint,
                expected_transition_id,
                target_registry_version,
                target_registry_fingerprint,
                target_registry_urn,
                target_publication_approval_id,
                rollback_transition_id,
                proposal_fingerprint,
                approval_id,
                actor,
                approved_at,
                decision_ids_json,
                committed_at,
                payload_json
            ) VALUES (
                %s, %s, %s, %s, 1, 'activate', 0,
                NULL, NULL, NULL, 1, %s, %s, %s, NULL,
                %s, %s, 'sb_semantic_steward_v1', clock_timestamp(),
                '["mapping-decision-1"]'::jsonb, clock_timestamp(), '{}'::jsonb
            )
            """,
            (
                first_transition,
                workspace_id,
                catalog_scope,
                registry_id,
                first_fingerprint,
                "urn:li:dataset:registry-v1",
                f"publication-{uuid4().hex}",
                _digest("proposal-v1"),
                f"approval-{uuid4().hex}",
            ),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.registry_active_pointers (
                workspace_id,
                catalog_scope,
                registry_id,
                generation,
                registry_version,
                registry_fingerprint,
                registry_target,
                transition_id,
                activated_by,
                activated_at,
                decision_ids_json
            ) VALUES (
                %s, %s, %s, 1, 1, %s, %s, %s,
                'sb_semantic_steward_v1', clock_timestamp(),
                '["mapping-decision-1"]'::jsonb
            )
            """,
            (
                workspace_id,
                catalog_scope,
                registry_id,
                first_fingerprint,
                "urn:li:dataset:registry-v1",
                first_transition,
            ),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.registry_activation_transitions (
                transition_id,
                workspace_id,
                catalog_scope,
                registry_id,
                generation,
                action,
                expected_generation,
                expected_registry_version,
                expected_registry_fingerprint,
                expected_transition_id,
                target_registry_version,
                target_registry_fingerprint,
                target_registry_urn,
                target_publication_approval_id,
                rollback_transition_id,
                proposal_fingerprint,
                approval_id,
                actor,
                approved_at,
                decision_ids_json,
                committed_at,
                payload_json
            ) VALUES (
                %s, %s, %s, %s, 2, 'activate', 1,
                1, %s, %s, 2, %s, %s, %s, NULL,
                %s, %s, 'sb_semantic_steward_v1', clock_timestamp(),
                '["mapping-decision-2"]'::jsonb, clock_timestamp(), '{}'::jsonb
            )
            """,
            (
                second_transition,
                workspace_id,
                catalog_scope,
                registry_id,
                first_fingerprint,
                first_transition,
                second_fingerprint,
                "urn:li:dataset:registry-v2",
                f"publication-{uuid4().hex}",
                _digest("proposal-v2"),
                f"approval-{uuid4().hex}",
            ),
        )
        connection.execute(
            """
            UPDATE schemabridge_control.registry_active_pointers
            SET generation = 2,
                registry_version = 2,
                registry_fingerprint = %s,
                registry_target = %s,
                transition_id = %s,
                activated_at = clock_timestamp(),
                decision_ids_json = '["mapping-decision-2"]'::jsonb
            WHERE workspace_id = %s
              AND catalog_scope = %s
              AND registry_id = %s
            """,
            (
                second_fingerprint,
                "urn:li:dataset:registry-v2",
                second_transition,
                workspace_id,
                catalog_scope,
                registry_id,
            ),
        )

    with psycopg.connect(catalog_database.reconciler) as connection:
        scans = connection.execute(
            """
            SELECT
                source_event_key,
                registry_generation,
                status,
                attempts,
                fencing_token
            FROM schemabridge_control.semantic_change_scan_requests
            WHERE workspace_id = %s
              AND catalog_scope = %s
              AND registry_id = %s
              AND source_kind = 'registry_pointer'
            ORDER BY registry_generation
            """,
            (workspace_id, catalog_scope, registry_id),
        ).fetchall()
        first_scan_id = connection.execute(
            """
            SELECT scan_id
            FROM schemabridge_control.semantic_change_scan_requests
            WHERE workspace_id = %s
              AND source_event_key = %s
            """,
            (workspace_id, first_transition),
        ).fetchone()
    assert scans == [
        (first_transition, 1, "requested", 0, 0),
        (second_transition, 2, "requested", 0, 0),
    ]
    assert first_scan_id is not None

    lease_digest = _digest("semantic-scan-lease")
    with psycopg.connect(catalog_database.reconciler) as connection:
        connection.execute(
            """
            UPDATE schemabridge_control.semantic_change_scan_requests
            SET status = 'leased',
                attempts = 1,
                lease_owner_id = 'semantic-reconciler-a',
                lease_capability_digest = %s,
                fencing_token = 1,
                lease_acquired_at = statement_timestamp(),
                lease_heartbeat_at = statement_timestamp(),
                lease_expires_at = statement_timestamp() + interval '1 minute',
                updated_at = updated_at + interval '1 second'
            WHERE scan_id = %s
            """,
            (lease_digest, first_scan_id[0]),
        )
        connection.execute(
            """
            UPDATE schemabridge_control.semantic_change_scan_requests
            SET lease_heartbeat_at = lease_heartbeat_at + interval '1 second',
                lease_expires_at = lease_expires_at + interval '1 minute',
                updated_at = updated_at + interval '1 second'
            WHERE scan_id = %s
            """,
            (first_scan_id[0],),
        )
        leased = connection.execute(
            """
            SELECT status, attempts, fencing_token, lease_owner_id
            FROM schemabridge_control.semantic_change_scan_requests
            WHERE scan_id = %s
            """,
            (first_scan_id[0],),
        ).fetchone()
    assert leased == ("leased", 1, 1, "semantic-reconciler-a")

    with (
        psycopg.connect(catalog_database.reconciler) as connection,
        pytest.raises(psycopg.Error) as stale_fence,
    ):
        connection.execute(
            """
            UPDATE schemabridge_control.semantic_change_scan_requests
            SET fencing_token = 0,
                updated_at = updated_at + interval '1 second'
            WHERE scan_id = %s
            """,
            (first_scan_id[0],),
        )
    assert stale_fence.value.sqlstate == "55000"


def test_rate_admission_is_cross_replica_state_and_missing_policy_fails_closed(
    catalog_database: _DatabaseUrls,
) -> None:
    workspace_id = f"workspace-rate-{uuid4().hex[:12]}"
    _insert_policy(catalog_database.migrator, workspace_id, request_limit=3)
    principal = _digest("rate-principal")
    expired_principal = _digest("expired-inactive-principal")
    with psycopg.connect(catalog_database.migrator) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.api_rate_limit_windows (
                workspace_id,
                principal_digest,
                operation_scope,
                window_started_at,
                window_expires_at,
                request_count,
                created_at,
                updated_at
            ) VALUES (
                'workspace-expired-inactive',
                %s,
                'inactive_operation',
                clock_timestamp() - interval '2 minutes',
                clock_timestamp() - interval '1 minute',
                1,
                clock_timestamp() - interval '2 minutes',
                clock_timestamp() - interval '2 minutes'
            )
            """,
            (expired_principal,),
        )

    observations: list[tuple[bool, int, int, int, int]] = []
    for _ in range(4):
        with psycopg.connect(catalog_database.api) as connection:
            row = connection.execute(
                """
                SELECT *
                FROM schemabridge_control.admit_api_request(%s, %s, %s)
                """,
                (workspace_id, principal, "catalog_read"),
            ).fetchone()
        assert row is not None
        observations.append(row)

    with psycopg.connect(catalog_database.migrator) as connection:
        expired_count = connection.execute(
            """
            SELECT count(*)
            FROM schemabridge_control.api_rate_limit_windows
            WHERE principal_digest = %s
            """,
            (expired_principal,),
        ).fetchone()
    assert expired_count == (0,)

    assert [row[0] for row in observations] == [True, True, True, False]
    assert [row[2] for row in observations] == [2, 1, 0, 0]
    assert [row[3] for row in observations] == [3, 3, 3, 3]
    assert [row[4] for row in observations] == [1, 2, 3, 3]
    assert 1 <= observations[-1][1] <= 60

    with psycopg.connect(catalog_database.api) as connection:
        other = connection.execute(
            """
            SELECT *
            FROM schemabridge_control.admit_api_request(%s, %s, %s)
            """,
            (workspace_id, _digest("another-principal"), "catalog_read"),
        ).fetchone()
    assert other == (True, 0, 2, 3, 1)

    concurrent_principal = _digest("concurrent-rate-principal")
    contender_count = 8
    start = Barrier(contender_count)

    def admit(_: int) -> tuple[bool, int, int, int, int]:
        with psycopg.connect(catalog_database.api) as connection:
            start.wait()
            row = connection.execute(
                """
                SELECT *
                FROM schemabridge_control.admit_api_request(%s, %s, %s)
                """,
                (workspace_id, concurrent_principal, "catalog_read"),
            ).fetchone()
        assert row is not None
        return row

    with ThreadPoolExecutor(max_workers=contender_count) as executor:
        concurrent = list(executor.map(admit, range(contender_count)))

    assert sum(row[0] for row in concurrent) == 3
    assert all(row[1] == 0 if row[0] else 1 <= row[1] <= 60 for row in concurrent)
    assert all(row[3] == 3 for row in concurrent)
    assert sorted(row[4] for row in concurrent if row[0]) == [1, 2, 3]
    assert all(row[4] == 3 for row in concurrent if not row[0])

    with psycopg.connect(catalog_database.migrator) as connection:
        connection.execute(
            """
            UPDATE schemabridge_control.tenant_capacity_policies
            SET api_requests_per_minute = 1,
                version = version + 1,
                updated_by = 'sb_platform_admin_v1',
                updated_at = clock_timestamp()
            WHERE workspace_id = %s
            """,
            (workspace_id,),
        )
    with psycopg.connect(catalog_database.api) as connection:
        lowered_policy = connection.execute(
            """
            SELECT *
            FROM schemabridge_control.admit_api_request(%s, %s, %s)
            """,
            (workspace_id, principal, "catalog_read"),
        ).fetchone()
    assert lowered_policy is not None
    assert lowered_policy[0] is False
    assert lowered_policy[2:] == (0, 1, 3)

    with (
        psycopg.connect(catalog_database.api) as connection,
        pytest.raises(psycopg.Error) as missing,
    ):
        connection.execute(
            """
            SELECT *
            FROM schemabridge_control.admit_api_request(%s, %s, %s)
            """,
            ("workspace-without-policy", principal, "catalog_read"),
        )
    assert missing.value.sqlstate == "55000"
    assert str(missing.value).splitlines()[0] == "tenant capacity policy is unavailable"
    assert "catalog_read" not in str(missing.value)


def _insert_job(
    connection: psycopg.Connection[Any],
    *,
    job_id: str,
    workspace_id: str,
    workflow_id: str,
    owner_actor_id: str,
    idempotency_digest: str,
    execution_target: GovernedExecutionTarget,
    now: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO schemabridge_control.execution_jobs (
            job_id,
            kind,
            workspace_id,
            workflow_workspace_id,
            workflow_id,
            workflow_owner_actor_id,
            submitting_actor_id,
            workflow_access_scope,
            authorized_operation,
            expected_workflow_revision,
            expected_plan_fingerprint,
            authenticated_at,
            authorized_at,
            authorization_expires_at,
            payload_fingerprint,
            request_fingerprint,
            idempotency_digest,
            connector_workspace_id,
            connector_connection_id,
            connector_route_revision,
            connector_route_fingerprint,
            connector_target_fingerprint,
            status,
            attempt_count,
            max_attempts,
            available_at,
            fencing_token,
            created_at,
            updated_at
        ) VALUES (
            %s,
            'execute_workflow_preview',
            %s,
            %s,
            %s,
            %s,
            %s,
            'owner',
            'execute_workflow_preview',
            1,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            'queued',
            0,
            3,
            %s,
            0,
            %s,
            %s
        )
        ON CONFLICT DO NOTHING
        """,
        (
            job_id,
            workspace_id,
            workspace_id,
            workflow_id,
            owner_actor_id,
            owner_actor_id,
            _digest(f"plan:{job_id}"),
            now - timedelta(minutes=1),
            now - timedelta(seconds=1),
            now + timedelta(minutes=5),
            _digest(f"payload:{job_id}"),
            _digest(f"request:{job_id}"),
            idempotency_digest,
            execution_target.workspace_id,
            execution_target.connection_id.root,
            execution_target.route_revision,
            execution_target.route_fingerprint,
            execution_target.fingerprint,
            now,
            now,
            now,
        ),
    )


def test_job_capacity_reserves_on_insert_replay_is_free_and_terminal_releases_once(
    catalog_database: _DatabaseUrls,
) -> None:
    workspace_id = f"workspace-jobs-{uuid4().hex[:12]}"
    workflow_id = f"workflow-{uuid4().hex[:12]}"
    owner = f"owner-{uuid4().hex[:12]}"
    _insert_policy(catalog_database.migrator, workspace_id, job_limit=2)
    execution_target = _execution_target(
        catalog_database,
        workspace_id=workspace_id,
    )
    with psycopg.connect(catalog_database.migrator) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.agent_workflow_drafts (
                workspace_id,
                id,
                revision,
                payload,
                execution_row_count,
                execution_preview_fingerprint,
                updated_at
            ) VALUES (%s, %s, 1, '{}'::jsonb, NULL, NULL, clock_timestamp())
            """,
            (workspace_id, workflow_id),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.workflow_access_grants (
                workspace_id,
                workflow_id,
                owner_actor_id,
                created_at
            ) VALUES (%s, %s, %s, clock_timestamp())
            """,
            (workspace_id, workflow_id, owner),
        )
    now = datetime.now(UTC)
    first_key = _digest("job-capacity-first")
    with psycopg.connect(catalog_database.api) as connection:
        _insert_job(
            connection,
            job_id="job-capacity-first",
            workspace_id=workspace_id,
            workflow_id=workflow_id,
            owner_actor_id=owner,
            idempotency_digest=first_key,
            execution_target=execution_target,
            now=now,
        )
        _insert_job(
            connection,
            job_id="job-capacity-first",
            workspace_id=workspace_id,
            workflow_id=workflow_id,
            owner_actor_id=owner,
            idempotency_digest=first_key,
            execution_target=execution_target,
            now=now,
        )
        _insert_job(
            connection,
            job_id="job-capacity-second",
            workspace_id=workspace_id,
            workflow_id=workflow_id,
            owner_actor_id=owner,
            idempotency_digest=_digest("job-capacity-second"),
            execution_target=execution_target,
            now=now + timedelta(microseconds=1),
        )

    with psycopg.connect(catalog_database.api) as connection:
        count = connection.execute(
            """
            SELECT nonterminal_job_count
            FROM schemabridge_control.tenant_execution_capacity
            WHERE workspace_id = %s
            """,
            (workspace_id,),
        ).fetchone()
    assert count == (2,)

    with (
        psycopg.connect(catalog_database.api) as connection,
        pytest.raises(psycopg.Error) as exhausted,
    ):
        _insert_job(
            connection,
            job_id="job-capacity-third",
            workspace_id=workspace_id,
            workflow_id=workflow_id,
            owner_actor_id=owner,
            idempotency_digest=_digest("job-capacity-third"),
            execution_target=execution_target,
            now=now + timedelta(microseconds=2),
        )
    assert exhausted.value.sqlstate == "53300"

    with psycopg.connect(catalog_database.api) as connection:
        connection.execute(
            """
            WITH observed AS (SELECT clock_timestamp() AS occurred_at)
            UPDATE schemabridge_control.execution_jobs
            SET status = 'cancelled',
                available_at = NULL,
                cancel_requested_by = submitting_actor_id,
                cancel_requested_at = observed.occurred_at,
                updated_at = observed.occurred_at,
                completed_at = observed.occurred_at
            FROM observed
            WHERE workspace_id = %s
              AND job_id = 'job-capacity-first'
            """,
            (workspace_id,),
        )
    with psycopg.connect(catalog_database.api) as connection:
        count = connection.execute(
            """
            SELECT nonterminal_job_count
            FROM schemabridge_control.tenant_execution_capacity
            WHERE workspace_id = %s
            """,
            (workspace_id,),
        ).fetchone()
        _insert_job(
            connection,
            job_id="job-capacity-third",
            workspace_id=workspace_id,
            workflow_id=workflow_id,
            owner_actor_id=owner,
            idempotency_digest=_digest("job-capacity-third"),
            execution_target=execution_target,
            now=now + timedelta(microseconds=3),
        )
        final_count = connection.execute(
            """
            SELECT nonterminal_job_count
            FROM schemabridge_control.tenant_execution_capacity
            WHERE workspace_id = %s
            """,
            (workspace_id,),
        ).fetchone()
    assert count == (1,)
    assert final_count == (2,)


def test_job_capacity_serializes_concurrent_replica_admission(
    catalog_database: _DatabaseUrls,
) -> None:
    workspace_id = f"workspace-job-race-{uuid4().hex[:12]}"
    workflow_id = f"workflow-{uuid4().hex[:12]}"
    owner = f"owner-{uuid4().hex[:12]}"
    _insert_policy(catalog_database.migrator, workspace_id, job_limit=5)
    execution_target = _execution_target(
        catalog_database,
        workspace_id=workspace_id,
    )
    with psycopg.connect(catalog_database.migrator) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.agent_workflow_drafts (
                workspace_id,
                id,
                revision,
                payload,
                execution_row_count,
                execution_preview_fingerprint,
                updated_at
            ) VALUES (%s, %s, 1, '{}'::jsonb, NULL, NULL, clock_timestamp())
            """,
            (workspace_id, workflow_id),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.workflow_access_grants (
                workspace_id,
                workflow_id,
                owner_actor_id,
                created_at
            ) VALUES (%s, %s, %s, clock_timestamp())
            """,
            (workspace_id, workflow_id, owner),
        )

    contender_count = 8
    start = Barrier(contender_count)
    observed_at = datetime.now(UTC)

    def submit(index: int) -> str:
        try:
            with psycopg.connect(catalog_database.api) as connection:
                start.wait()
                _insert_job(
                    connection,
                    job_id=f"job-race-{index}",
                    workspace_id=workspace_id,
                    workflow_id=workflow_id,
                    owner_actor_id=owner,
                    idempotency_digest=_digest(f"job-race-{index}"),
                    execution_target=execution_target,
                    now=observed_at + timedelta(microseconds=index),
                )
        except psycopg.Error as exc:
            return exc.sqlstate or "unknown"
        return "admitted"

    with ThreadPoolExecutor(max_workers=contender_count) as executor:
        outcomes = list(executor.map(submit, range(contender_count)))

    assert outcomes.count("admitted") == 5
    assert outcomes.count("53300") == 3
    with psycopg.connect(catalog_database.api) as connection:
        counts = connection.execute(
            """
            SELECT
                capacity.nonterminal_job_count,
                count(job.job_id)
            FROM schemabridge_control.tenant_execution_capacity AS capacity
            LEFT JOIN schemabridge_control.execution_jobs AS job
              ON job.workspace_id = capacity.workspace_id
             AND job.status IN ('queued', 'leased', 'retry_wait', 'cancel_requested')
            WHERE capacity.workspace_id = %s
            GROUP BY capacity.nonterminal_job_count
            """,
            (workspace_id,),
        ).fetchone()
        schedule = connection.execute(
            """
            SELECT claim_sequence
            FROM schemabridge_control.tenant_job_schedule
            WHERE workspace_id = %s
            """,
            (workspace_id,),
        ).fetchone()
    assert counts == (5, 5)
    assert schedule == (0,)

    with psycopg.connect(catalog_database.migrator) as connection:
        connection.execute(
            """
            UPDATE schemabridge_control.tenant_capacity_policies
            SET nonterminal_job_limit = 1,
                version = 2,
                updated_by = 'sb_platform_admin_v1',
                updated_at = clock_timestamp()
            WHERE workspace_id = %s
            """,
            (workspace_id,),
        )
    with (
        psycopg.connect(catalog_database.api) as connection,
        pytest.raises(psycopg.Error) as lowered_policy,
    ):
        _insert_job(
            connection,
            job_id="job-race-after-policy-lower",
            workspace_id=workspace_id,
            workflow_id=workflow_id,
            owner_actor_id=owner,
            idempotency_digest=_digest("job-race-after-policy-lower"),
            execution_target=execution_target,
            now=observed_at + timedelta(seconds=1),
        )
    assert lowered_policy.value.sqlstate == "53300"


def test_worker_schedule_rows_support_skip_locked_tenant_rotation(
    catalog_database: _DatabaseUrls,
) -> None:
    first_workspace = f"workspace-fair-a-{uuid4().hex[:10]}"
    second_workspace = f"workspace-fair-b-{uuid4().hex[:10]}"
    with psycopg.connect(catalog_database.worker) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.tenant_job_schedule (
                workspace_id,
                last_claimed_at,
                claim_sequence,
                updated_at
            ) VALUES
                (%s, NULL, 0, clock_timestamp()),
                (%s, NULL, 0, clock_timestamp())
            """,
            (first_workspace, second_workspace),
        )

    claim_sql = """
        SELECT workspace_id
        FROM schemabridge_control.tenant_job_schedule
        WHERE workspace_id IN (%s, %s)
        ORDER BY last_claimed_at NULLS FIRST, claim_sequence, workspace_id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    """
    with (
        psycopg.connect(catalog_database.worker) as first_connection,
        psycopg.connect(catalog_database.worker) as second_connection,
    ):
        first_claim = first_connection.execute(
            claim_sql,
            (first_workspace, second_workspace),
        ).fetchone()
        second_claim = second_connection.execute(
            claim_sql,
            (first_workspace, second_workspace),
        ).fetchone()

    assert first_claim == (first_workspace,)
    assert second_claim == (second_workspace,)


def test_catalog_connection_disable_requires_one_terminal_idempotency_digest(
    catalog_database: _DatabaseUrls,
) -> None:
    workspace_id = f"workspace-disable-{uuid4().hex[:12]}"
    connection_id = f"connection-{uuid4().hex[:12]}"
    disable_digest = _digest("connection-disable")
    _insert_policy(catalog_database.migrator, workspace_id)
    with psycopg.connect(catalog_database.api) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.catalog_connections (
                workspace_id,
                connection_id,
                display_name,
                source_kind,
                catalog_scope,
                environment,
                status,
                registration_fingerprint,
                idempotency_digest,
                created_by_actor_id,
                created_at,
                updated_at
            ) VALUES (
                %s, %s, 'Disable test', 'synthetic', 'synthetic-test',
                'TEST', 'enabled', %s, %s, 'sb_platform_admin_v1',
                clock_timestamp(), clock_timestamp()
            )
            """,
            (
                workspace_id,
                connection_id,
                _digest("disable-registration"),
                _digest("disable-registration-idempotency"),
            ),
        )

    with (
        psycopg.connect(catalog_database.api) as connection,
        pytest.raises(psycopg.Error) as missing_digest,
    ):
        connection.execute(
            """
            UPDATE schemabridge_control.catalog_connections
            SET status = 'disabled',
                disabled_by_actor_id = 'sb_platform_admin_v1',
                disabled_at = clock_timestamp(),
                updated_at = clock_timestamp()
            WHERE workspace_id = %s
              AND connection_id = %s
            """,
            (workspace_id, connection_id),
        )
    assert missing_digest.value.sqlstate == "55000"

    with psycopg.connect(catalog_database.api) as connection:
        connection.execute(
            """
            UPDATE schemabridge_control.catalog_connections
            SET status = 'disabled',
                disabled_by_actor_id = 'sb_platform_admin_v1',
                disabled_idempotency_digest = %s,
                disabled_at = clock_timestamp(),
                updated_at = clock_timestamp()
            WHERE workspace_id = %s
              AND connection_id = %s
            """,
            (disable_digest, workspace_id, connection_id),
        )
        disabled = connection.execute(
            """
            SELECT status, disabled_by_actor_id, disabled_idempotency_digest
            FROM schemabridge_control.catalog_connections
            WHERE workspace_id = %s
              AND connection_id = %s
            """,
            (workspace_id, connection_id),
        ).fetchone()
    assert disabled == ("disabled", "sb_platform_admin_v1", disable_digest)

    with (
        psycopg.connect(catalog_database.api) as connection,
        pytest.raises(psycopg.Error) as terminal,
    ):
        connection.execute(
            """
            UPDATE schemabridge_control.catalog_connections
            SET disabled_idempotency_digest = %s,
                updated_at = clock_timestamp()
            WHERE workspace_id = %s
              AND connection_id = %s
            """,
            (_digest("different-disable"), workspace_id, connection_id),
        )
    assert terminal.value.sqlstate == "55000"


def test_catalog_generation_activation_is_fenced_atomic_and_public_route_is_hidden(
    catalog_database: _DatabaseUrls,
) -> None:
    workspace_id = f"workspace-catalog-{uuid4().hex[:12]}"
    connection_id = f"connection-{uuid4().hex[:12]}"
    refresh_id = f"refresh-{uuid4().hex[:12]}"
    asset_key = _digest("catalog-asset")
    asset_id = "urn:synthetic:dataset:customers"
    field_key = _digest("catalog-field")
    lease_capability = "catalog-lease-capability-0123456789abcdef"
    lease_digest = _digest(lease_capability)
    inventory_fingerprint = _digest("catalog-inventory")
    _insert_policy(catalog_database.migrator, workspace_id)

    with psycopg.connect(catalog_database.api) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.catalog_connections (
                workspace_id,
                connection_id,
                display_name,
                source_kind,
                catalog_scope,
                environment,
                platform_instance,
                status,
                registration_fingerprint,
                idempotency_digest,
                created_by_actor_id,
                created_at,
                updated_at
            ) VALUES (
                %s, %s, 'Synthetic catalog', 'synthetic', 'synthetic-test',
                'TEST', 'instance-a', 'enabled', %s, %s,
                'sb_platform_admin_v1', clock_timestamp(), clock_timestamp()
            )
            """,
            (
                workspace_id,
                connection_id,
                _digest("connection-registration"),
                _digest("connection-idempotency"),
            ),
        )
    target_facts = ensure_catalog_connector_target(
        catalog_database.migrator,
        workspace_id=workspace_id,
        connection_id=CatalogConnectionId(connection_id),
    )

    with psycopg.connect(catalog_database.api) as connection:
        requested_generation = connection.execute(
            """
            SELECT base_generation, target_generation
            FROM schemabridge_control.request_catalog_refresh(
                %s, %s, %s, 'full', %s, %s,
                'sb_platform_admin_v1', clock_timestamp()
            )
            """,
            (
                workspace_id,
                connection_id,
                refresh_id,
                _digest("refresh-request"),
                _digest("refresh-idempotency"),
            ),
        ).fetchone()
    assert requested_generation == (0, 1)

    with (
        psycopg.connect(catalog_database.api) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        connection.execute(
            """
            SELECT credential_binding_ref
            FROM schemabridge_control.catalog_connection_routes
            WHERE workspace_id = %s
            """,
            (workspace_id,),
        )

    with (
        psycopg.connect(catalog_database.api) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        connection.execute(
            """
            SELECT source_checkpoint, lease_owner_id, lease_capability_digest
            FROM schemabridge_control.catalog_refresh_runs
            WHERE workspace_id = %s
            """,
            (workspace_id,),
        )

    with (
        psycopg.connect(catalog_database.catalog) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        connection.execute(
            """
            SELECT credential_binding_ref
            FROM schemabridge_control.catalog_connection_routes
            WHERE workspace_id = %s
            """,
            (workspace_id,),
        )

    with psycopg.connect(catalog_database.catalog) as connection:
        unavailable_route = connection.execute(
            """
            SELECT *
            FROM schemabridge_control.load_owned_catalog_connector_route_v2(
                %s, %s, %s, 'catalog-indexer-a', %s, 1, %s, %s, %s
            )
            """,
            (
                workspace_id,
                connection_id,
                refresh_id,
                lease_capability,
                target_facts.contract_version,
                target_facts.route_revision,
                target_facts.target_fingerprint,
            ),
        ).fetchone()
    assert unavailable_route is None

    with psycopg.connect(catalog_database.catalog) as connection:
        connection.execute(
            """
            UPDATE schemabridge_control.catalog_refresh_runs
            SET status = 'leased',
                base_generation = NULL,
                target_generation = 1,
                lease_owner_id = 'catalog-indexer-a',
                lease_capability_digest = %s,
                fencing_token = 1,
                lease_acquired_at = clock_timestamp(),
                lease_heartbeat_at = clock_timestamp(),
                lease_expires_at = clock_timestamp() + interval '5 minutes',
                updated_at = clock_timestamp()
            WHERE workspace_id = %s
              AND connection_id = %s
              AND refresh_id = %s
            """,
            (lease_digest, workspace_id, connection_id, refresh_id),
        )
        wrong_route = connection.execute(
            """
            SELECT *
            FROM schemabridge_control.load_owned_catalog_connector_route_v2(
                %s, %s, %s, 'catalog-indexer-a', %s, 1, %s, %s, %s
            )
            """,
            (
                workspace_id,
                connection_id,
                refresh_id,
                "wrong-catalog-lease-capability-0123456789",
                target_facts.contract_version,
                target_facts.route_revision,
                target_facts.target_fingerprint,
            ),
        ).fetchone()
        other_replica_route = connection.execute(
            """
            SELECT *
            FROM schemabridge_control.load_owned_catalog_connector_route_v2(
                %s, %s, %s, 'catalog-indexer-b', %s, 1, %s, %s, %s
            )
            """,
            (
                workspace_id,
                connection_id,
                refresh_id,
                lease_capability,
                target_facts.contract_version,
                target_facts.route_revision,
                target_facts.target_fingerprint,
            ),
        ).fetchone()
        exact_route = connection.execute(
            """
            SELECT *
            FROM schemabridge_control.load_owned_catalog_connector_route_v2(
                %s, %s, %s, 'catalog-indexer-a', %s, 1, %s, %s, %s
            )
            """,
            (
                workspace_id,
                connection_id,
                refresh_id,
                lease_capability,
                target_facts.contract_version,
                target_facts.route_revision,
                target_facts.target_fingerprint,
            ),
        ).fetchone()
    assert wrong_route is None
    assert other_replica_route is None
    assert exact_route is not None
    assert exact_route[:10] == (
        workspace_id,
        connection_id,
        "synthetic",
        "TEST",
        "synthetic-test",
        "instance-a",
        target_facts.catalog_identity_fingerprint,
        target_facts.contract_version,
        target_facts.route_revision,
        target_facts.target_fingerprint,
    )
    assert str(exact_route[10]).startswith("test.catalog.")
    assert exact_route[11] == 202

    with psycopg.connect(catalog_database.catalog) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.catalog_generations (
                workspace_id,
                connection_id,
                generation,
                refresh_id,
                base_generation,
                refresh_mode,
                status,
                created_at
            ) VALUES (
                %s, %s, 1, %s, NULL, 'full', 'staging', clock_timestamp()
            )
            """,
            (
                workspace_id,
                connection_id,
                refresh_id,
            ),
        )
        connection.execute(
            """
            UPDATE schemabridge_control.catalog_refresh_runs
            SET status = 'staging',
                lease_heartbeat_at = clock_timestamp(),
                lease_expires_at = clock_timestamp() + interval '5 minutes',
                updated_at = clock_timestamp()
            WHERE workspace_id = %s
              AND connection_id = %s
              AND refresh_id = %s
            """,
            (workspace_id, connection_id, refresh_id),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.catalog_assets (
                workspace_id,
                connection_id,
                generation,
                asset_key,
                asset_id,
                qualified_name,
                asset_sort_key,
                platform,
                environment,
                database_name,
                schema_name,
                table_name,
                display_name,
                description,
                field_count,
                metadata_fingerprint,
                observed_at
            ) VALUES (
                %s, %s, 1, %s, %s, 'public.customers',
                'public.customers', 'postgres', 'TEST', 'synthetic', 'public',
                'customers', 'Customers', 'Synthetic customer metadata',
                1, %s, clock_timestamp()
            )
            """,
            (
                workspace_id,
                connection_id,
                asset_key,
                asset_id,
                _digest("asset-metadata"),
            ),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.catalog_fields (
                workspace_id,
                connection_id,
                generation,
                asset_key,
                field_key,
                field_path,
                field_sort_key,
                field_name,
                ordinal_position,
                native_type,
                normalized_type,
                nullable,
                is_part_of_key,
                tags,
                glossary_terms,
                description,
                metadata_fingerprint,
                observed_at
            ) VALUES (
                %s, %s, 1, %s, %s, ARRAY['customer_id'], 'customer_id',
                'customer_id', 1, 'varchar(20)', NULL, false, true,
                ARRAY['identifier'], ARRAY['Customer Identifier'],
                'Synthetic customer identifier', %s, clock_timestamp()
            )
            """,
            (
                workspace_id,
                connection_id,
                asset_key,
                field_key,
                _digest("field-metadata"),
            ),
        )
        connection.execute(
            """
            UPDATE schemabridge_control.catalog_refresh_runs
            SET source_checkpoint = 'complete',
                source_page_number = 1,
                source_page_fingerprint = %s,
                staged_asset_count = 1,
                staged_field_count = 1,
                source_complete = true,
                lease_heartbeat_at = clock_timestamp(),
                lease_expires_at = clock_timestamp() + interval '5 minutes',
                updated_at = clock_timestamp()
            WHERE workspace_id = %s
              AND connection_id = %s
              AND refresh_id = %s
            """,
            (
                _digest("source-page"),
                workspace_id,
                connection_id,
                refresh_id,
            ),
        )

    with (
        psycopg.connect(catalog_database.migrator) as connection,
        pytest.raises(psycopg.Error) as wrong_session,
    ):
        connection.execute(
            """
            SELECT schemabridge_control.lock_catalog_completion_scope(
                %s, %s, %s, 1, 1, %s
            )
            """,
            (workspace_id, connection_id, refresh_id, lease_digest),
        )
    assert wrong_session.value.sqlstate == "42501"

    with (
        psycopg.connect(catalog_database.catalog) as connection,
        pytest.raises(psycopg.Error) as lost_lease,
    ):
        connection.execute(
            """
            SELECT schemabridge_control.lock_catalog_completion_scope(
                %s, %s, %s, 1, 1, %s
            )
            """,
            (
                workspace_id,
                connection_id,
                refresh_id,
                _digest("wrong-catalog-lease"),
            ),
        )
    assert lost_lease.value.sqlstate == "55000"

    with psycopg.connect(catalog_database.catalog) as connection:
        connection.execute(
            """
            SELECT schemabridge_control.lock_catalog_completion_scope(
                %s, %s, %s, 1, 1, %s
            )
            """,
            (workspace_id, connection_id, refresh_id, lease_digest),
        ).fetchone()

    with (
        psycopg.connect(catalog_database.catalog) as connection,
        pytest.raises(psycopg.Error) as stale,
    ):
        connection.execute(
            """
            SELECT *
            FROM schemabridge_control.activate_catalog_generation_for_target(
                %s, %s, %s, 'catalog-indexer-a', %s,
                99, 1, 1, %s, %s, %s, %s
            )
            """,
            (
                workspace_id,
                connection_id,
                refresh_id,
                lease_capability,
                inventory_fingerprint,
                target_facts.contract_version,
                target_facts.route_revision,
                target_facts.target_fingerprint,
            ),
        )
    assert stale.value.sqlstate == "55000"

    with psycopg.connect(catalog_database.catalog) as connection:
        activated = connection.execute(
            """
            SELECT *
            FROM schemabridge_control.activate_catalog_generation_for_target(
                %s, %s, %s, 'catalog-indexer-a', %s,
                NULL, 1, 1, %s, %s, %s, %s
            )
            """,
            (
                workspace_id,
                connection_id,
                refresh_id,
                lease_capability,
                inventory_fingerprint,
                target_facts.contract_version,
                target_facts.route_revision,
                target_facts.target_fingerprint,
            ),
        ).fetchone()
    assert activated == (1, 1)

    with psycopg.connect(catalog_database.api) as connection:
        public_generation = connection.execute(
            """
            SELECT
                connection.active_generation,
                connection.active_generation_fingerprint,
                generation.status,
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
            (workspace_id, connection_id),
        ).fetchone()
        public_refresh = connection.execute(
            """
            SELECT status
            FROM schemabridge_control.load_catalog_refresh_public(%s, %s)
            """,
            (workspace_id, refresh_id),
        ).fetchone()
    assert public_generation == (
        1,
        inventory_fingerprint,
        "completed",
        1,
        1,
    )
    assert public_refresh == ("completed",)
    with psycopg.connect(catalog_database.reconciler) as connection:
        initial_change_count = connection.execute(
            """
            SELECT count(*)
            FROM schemabridge_control.catalog_generation_changes
            WHERE workspace_id = %s
              AND connection_id = %s
            """,
            (workspace_id, connection_id),
        ).fetchone()
        initial_scan = connection.execute(
            """
            SELECT status, base_catalog_generation, observed_catalog_generation
            FROM schemabridge_control.semantic_change_scan_requests
            WHERE workspace_id = %s
              AND source_kind = 'catalog_generation'
              AND connection_id = %s
              AND observed_catalog_generation = 1
            """,
            (workspace_id, connection_id),
        ).fetchone()
    assert initial_change_count == (0,)
    assert initial_scan is None

    registry_id = f"registry_{uuid4().hex[:12]}"
    second_registry_id = f"registry_{uuid4().hex[:12]}"
    transition_id = f"transition-{uuid4().hex}"
    second_transition_id = f"transition-{uuid4().hex}"
    registry_fingerprint = _digest("gate-registry")
    pointer_fingerprint = _digest("gate-pointer")
    context_fingerprint = _digest("gate-context")
    observation_fingerprint = _digest("gate-observation")
    vector_fingerprint = _digest("gate-vector")
    dependency_fingerprint = _digest("gate-dependencies")
    report_fingerprint = _digest("gate-report")
    report_id = f"report_{report_fingerprint}"
    finding_fingerprint = _digest("gate-finding")
    finding_id = f"finding_{finding_fingerprint}"
    impact_fingerprint = _digest("gate-impact")
    impact_id = f"impact-{uuid4().hex}"
    impact_set_fingerprint = _digest("gate-impact-set")
    finding_set_fingerprint = _digest("gate-finding-set")
    baseline_fingerprint = _digest("gate-baseline")
    binding_fingerprint = _digest("gate-binding")
    binding_id = f"binding_{binding_fingerprint}"
    approval_id = f"approval-{uuid4().hex}"
    audit_event_id = f"semantic-audit-{uuid4().hex}"
    resolution_id = f"decision-{uuid4().hex}"
    resolution_fingerprint = _digest(resolution_id)
    mapping_decision_id = "mapping-decision-customer-id"
    actor_id = "sb_semantic_steward_v1"

    with psycopg.connect(catalog_database.runtime) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.registry_activation_transitions (
                transition_id,
                workspace_id,
                catalog_scope,
                registry_id,
                generation,
                action,
                expected_generation,
                expected_registry_version,
                expected_registry_fingerprint,
                expected_transition_id,
                target_registry_version,
                target_registry_fingerprint,
                target_registry_urn,
                target_publication_approval_id,
                rollback_transition_id,
                proposal_fingerprint,
                approval_id,
                actor,
                approved_at,
                decision_ids_json,
                committed_at,
                payload_json
            ) VALUES (
                %s, %s, 'synthetic-test', %s, 1, 'activate', 0,
                NULL, NULL, NULL, 1, %s, 'urn:synthetic:registry:gate',
                %s, NULL, %s, %s, %s, clock_timestamp(),
                %s::jsonb, clock_timestamp(), '{}'::jsonb
            )
            """,
            (
                transition_id,
                workspace_id,
                registry_id,
                registry_fingerprint,
                f"publication-{uuid4().hex}",
                _digest("gate-registry-proposal"),
                f"registry-approval-{uuid4().hex}",
                actor_id,
                json.dumps([mapping_decision_id]),
            ),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.registry_activation_transitions (
                transition_id,
                workspace_id,
                catalog_scope,
                registry_id,
                generation,
                action,
                expected_generation,
                expected_registry_version,
                expected_registry_fingerprint,
                expected_transition_id,
                target_registry_version,
                target_registry_fingerprint,
                target_registry_urn,
                target_publication_approval_id,
                rollback_transition_id,
                proposal_fingerprint,
                approval_id,
                actor,
                approved_at,
                decision_ids_json,
                committed_at,
                payload_json
            ) VALUES (
                %s, %s, 'synthetic-test', %s, 1, 'activate', 0,
                NULL, NULL, NULL, 1, %s, 'urn:synthetic:registry:gate-two',
                %s, NULL, %s, %s, %s, clock_timestamp(),
                %s::jsonb, clock_timestamp(), '{}'::jsonb
            )
            """,
            (
                second_transition_id,
                workspace_id,
                second_registry_id,
                registry_fingerprint,
                f"publication-{uuid4().hex}",
                _digest("gate-registry-proposal-two"),
                f"registry-approval-{uuid4().hex}",
                actor_id,
                json.dumps([mapping_decision_id]),
            ),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.registry_active_pointers (
                workspace_id,
                catalog_scope,
                registry_id,
                generation,
                registry_version,
                registry_fingerprint,
                registry_target,
                transition_id,
                activated_by,
                activated_at,
                decision_ids_json
            ) VALUES (
                %s, 'synthetic-test', %s, 1, 1, %s,
                'urn:synthetic:registry:gate-two', %s, %s,
                clock_timestamp(), %s::jsonb
            )
            """,
            (
                workspace_id,
                second_registry_id,
                registry_fingerprint,
                second_transition_id,
                actor_id,
                json.dumps([mapping_decision_id]),
            ),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.registry_active_pointers (
                workspace_id,
                catalog_scope,
                registry_id,
                generation,
                registry_version,
                registry_fingerprint,
                registry_target,
                transition_id,
                activated_by,
                activated_at,
                decision_ids_json
            ) VALUES (
                %s, 'synthetic-test', %s, 1, 1, %s,
                'urn:synthetic:registry:gate', %s, %s,
                clock_timestamp(), %s::jsonb
            )
            """,
            (
                workspace_id,
                registry_id,
                registry_fingerprint,
                transition_id,
                actor_id,
                json.dumps([mapping_decision_id]),
            ),
        )

    with psycopg.connect(catalog_database.reconciler) as connection:
        evidence = connection.execute(
            """
            SELECT
                asset_metadata_fingerprint,
                field_metadata_fingerprint,
                field_definition_fingerprint,
                field_terms_fingerprint,
                normalized_type,
                nullable,
                is_part_of_key
            FROM schemabridge_control.semantic_catalog_evidence_projection
            WHERE workspace_id = %s
              AND connection_id = %s
              AND asset_key = %s
              AND field_key = %s
            """,
            (workspace_id, connection_id, asset_key, field_key),
        ).fetchone()
    assert evidence is not None
    (
        asset_metadata_fingerprint,
        field_metadata_fingerprint,
        field_definition_fingerprint,
        field_terms_fingerprint,
        normalized_type,
        nullable,
        is_part_of_key,
    ) = evidence
    assert normalized_type is None

    scope_json = {
        "workspace_id": workspace_id,
        "catalog_scope": "synthetic-test",
        "registry_id": registry_id,
    }
    mapping_json = {
        "logical_field": "Customer.customer_id",
        "physical_field": "public.customers.customer_id",
        "version": 1,
        "approval_decision_id": mapping_decision_id,
        "physical_type": "string",
    }
    dependency_index_json = {
        "scope": scope_json,
        "watermark": 1,
        "fingerprint": dependency_fingerprint,
        "complete": True,
    }
    context_json = {
        "scope": scope_json,
        "pointer_generation": 1,
        "pointer_fingerprint": pointer_fingerprint,
        "pointer_transition_id": transition_id,
        "registry_version": 1,
        "registry_fingerprint": registry_fingerprint,
        "mappings": [mapping_json],
        "joins": [],
        "dependency_index": dependency_index_json,
        "fingerprint": context_fingerprint,
    }
    vector_json = {
        "observations": [
            {
                "connection_id": connection_id,
                "generation": 1,
                "inventory_fingerprint": inventory_fingerprint,
            }
        ],
        "fingerprint": vector_fingerprint,
    }
    observation_json = {
        "context": context_json,
        "catalog_generations": vector_json,
        "fields": [{"mapping": mapping_json, "present": True}],
        "joins": [],
        "observed_at": "2026-07-24T12:00:00+00:00",
        "complete": True,
        "fingerprint": observation_fingerprint,
    }
    finding_json = {
        "id": finding_id,
        "kind": "baseline_required",
        "severity": "review_required",
        "mapping": mapping_json,
        "join": None,
        "previous_fingerprint": None,
        "current_fingerprint": observation_fingerprint,
        "risks": ["baseline_required"],
        "fingerprint": finding_fingerprint,
    }
    impact_json = {
        "kind": "mapping",
        "artifact_id": mapping_decision_id,
        "artifact_version": 1,
        "finding_ids": [finding_id],
        "fingerprint": impact_fingerprint,
    }
    report_json = {
        "id": report_id,
        "context": context_json,
        "observation_fingerprint": observation_fingerprint,
        "catalog_generations": vector_json,
        "baseline_revision": None,
        "baseline_fingerprint": None,
        "findings": [finding_json],
        "impacts": {
            "mapping_count": 1,
            "join_count": 0,
            "workflow_count": 0,
            "recipe_count": 0,
            "complete": True,
            "watermark": 1,
            "dependency_index_fingerprint": dependency_fingerprint,
            "impact_set_fingerprint": impact_set_fingerprint,
        },
        "status": "review_required",
        "inspected_at": "2026-07-24T12:00:00+00:00",
        "fingerprint": report_fingerprint,
    }

    with psycopg.connect(catalog_database.reconciler) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.semantic_dependency_index_states (
                workspace_id,
                catalog_scope,
                registry_id,
                registry_generation,
                registry_version,
                registry_fingerprint,
                pointer_transition_id,
                watermark,
                index_fingerprint,
                complete,
                indexed_at
            ) VALUES (
                %s, 'synthetic-test', %s, 1, 1, %s, %s, 1, %s, true,
                clock_timestamp()
            )
            """,
            (
                workspace_id,
                registry_id,
                registry_fingerprint,
                transition_id,
                dependency_fingerprint,
            ),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.semantic_change_reports (
                report_id,
                workspace_id,
                catalog_scope,
                registry_id,
                registry_generation,
                registry_version,
                registry_fingerprint,
                pointer_transition_id,
                pointer_fingerprint,
                context_fingerprint,
                observation_fingerprint,
                expected_head_revision,
                baseline_revision,
                baseline_fingerprint,
                report_kind,
                outcome,
                catalog_generation_vector_json,
                catalog_generation_vector_fingerprint,
                dependency_index_watermark,
                dependency_index_fingerprint,
                dependency_index_complete,
                governed_mapping_count,
                governed_join_count,
                finding_count,
                review_finding_count,
                blocking_finding_count,
                informational_finding_count,
                impact_count,
                finding_set_fingerprint,
                impact_set_fingerprint,
                report_fingerprint,
                context_json,
                observation_json,
                report_json,
                inspected_at,
                retain_until
            ) VALUES (
                %s, %s, 'synthetic-test', %s, 1, 1, %s, %s,
                %s, %s, %s, 0, 0, NULL, 'baseline_review',
                'review_required', %s::jsonb, %s, 1, %s, true,
                1, 0, 1, 1, 0, 0, 1, %s, %s, %s,
                %s::jsonb, %s::jsonb, %s::jsonb,
                '2026-07-24T12:00:00+00:00'::timestamptz,
                '2027-07-24T12:00:00+00:00'::timestamptz
            )
            """,
            (
                report_id,
                workspace_id,
                registry_id,
                registry_fingerprint,
                transition_id,
                pointer_fingerprint,
                context_fingerprint,
                observation_fingerprint,
                json.dumps(vector_json),
                vector_fingerprint,
                dependency_fingerprint,
                finding_set_fingerprint,
                impact_set_fingerprint,
                report_fingerprint,
                json.dumps(context_json),
                json.dumps(observation_json),
                json.dumps(report_json),
            ),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.semantic_change_findings (
                finding_id,
                workspace_id,
                catalog_scope,
                registry_id,
                report_id,
                finding_sort_key,
                change_kind,
                severity,
                binding_id,
                mapping_decision_id,
                affected_join_contracts_json,
                previous_evidence_fingerprint,
                current_evidence_fingerprint,
                risk_codes,
                finding_fingerprint,
                finding_json,
                created_at
            ) VALUES (
                %s, %s, 'synthetic-test', %s, %s, %s,
                'baseline_required', 'review_required', NULL, %s,
                '[]'::jsonb, NULL, %s, ARRAY['baseline_required'],
                %s, %s::jsonb, clock_timestamp()
            )
            """,
            (
                finding_id,
                workspace_id,
                registry_id,
                report_id,
                finding_id,
                mapping_decision_id,
                observation_fingerprint,
                finding_fingerprint,
                json.dumps(finding_json),
            ),
        )
        unsafe_impact_fingerprint = _digest("unsafe-gate-impact")
        unsafe_impact_json = {
            **impact_json,
            "fingerprint": unsafe_impact_fingerprint,
            "api_key": "<redacted>",
        }
        with (
            pytest.raises(psycopg.errors.CheckViolation),
            connection.transaction(),
        ):
            connection.execute(
                """
                INSERT INTO schemabridge_control.semantic_change_impacts (
                    impact_id,
                    workspace_id,
                    catalog_scope,
                    registry_id,
                    report_id,
                    impact_sort_key,
                    artifact_kind,
                    artifact_id,
                    artifact_version,
                    artifact_fingerprint,
                    finding_ids_json,
                    impact_state,
                    impact_fingerprint,
                    impact_json,
                    created_at
                ) VALUES (
                    %s, %s, 'synthetic-test', %s, %s, %s,
                    'mapping', %s, 1, %s, %s::jsonb,
                    'review_required', %s, %s::jsonb, clock_timestamp()
                )
                """,
                (
                    f"impact-{uuid4().hex}",
                    workspace_id,
                    registry_id,
                    report_id,
                    f"unsafe-{impact_id}",
                    mapping_decision_id,
                    binding_fingerprint,
                    json.dumps([finding_id]),
                    unsafe_impact_fingerprint,
                    json.dumps(unsafe_impact_json),
                ),
            )
        connection.execute(
            """
            INSERT INTO schemabridge_control.semantic_change_impacts (
                impact_id,
                workspace_id,
                catalog_scope,
                registry_id,
                report_id,
                impact_sort_key,
                artifact_kind,
                artifact_id,
                artifact_version,
                artifact_fingerprint,
                finding_ids_json,
                impact_state,
                impact_fingerprint,
                impact_json,
                created_at
            ) VALUES (
                %s, %s, 'synthetic-test', %s, %s, %s,
                'mapping', %s, 1, %s, %s::jsonb,
                'review_required', %s, %s::jsonb, clock_timestamp()
            )
            """,
            (
                impact_id,
                workspace_id,
                registry_id,
                report_id,
                impact_id,
                mapping_decision_id,
                binding_fingerprint,
                json.dumps([finding_id]),
                impact_fingerprint,
                json.dumps(impact_json),
            ),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.control_audit_events (
                event_id,
                workspace_id,
                operation,
                transition_id,
                previous_hash,
                event_hash,
                payload_fingerprint,
                key_version,
                event_json,
                occurred_at
            ) VALUES (
                %s, %s, 'semantic_change_decision', NULL, NULL,
                %s, %s, 'v1', '{}'::jsonb, clock_timestamp()
            )
            """,
            (
                audit_event_id,
                workspace_id,
                _digest(audit_event_id),
                _digest("gate-audit-payload"),
            ),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.semantic_change_resolutions (
                resolution_id,
                workspace_id,
                catalog_scope,
                registry_id,
                report_id,
                report_fingerprint,
                expected_head_revision,
                expected_baseline_revision,
                resulting_head_revision,
                resulting_baseline_revision,
                decision_action,
                resulting_state,
                proposal_fingerprint,
                approval_fingerprint,
                approval_id,
                confirmation,
                actor_id,
                approved_at,
                impact_count,
                impact_set_fingerprint,
                dependency_index_watermark,
                dependency_index_fingerprint,
                approved_binding_set_fingerprint,
                control_audit_event_id,
                resolution_fingerprint,
                committed_at
            ) VALUES (
                %s, %s, 'synthetic-test', %s, %s, %s,
                0, 0, 1, 1, 'establish_baseline', 'revalidated',
                %s, %s, %s, 'ESTABLISH SEMANTIC EVIDENCE BASELINE',
                %s, clock_timestamp(), 1, %s, 1, %s, %s, %s, %s,
                clock_timestamp()
            )
            """,
            (
                resolution_id,
                workspace_id,
                registry_id,
                report_id,
                report_fingerprint,
                _digest("gate-decision-proposal"),
                _digest("gate-decision-approval"),
                approval_id,
                actor_id,
                impact_set_fingerprint,
                dependency_fingerprint,
                baseline_fingerprint,
                audit_event_id,
                resolution_fingerprint,
            ),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.semantic_change_heads (
                workspace_id,
                catalog_scope,
                registry_id,
                head_revision,
                registry_generation,
                registry_version,
                registry_fingerprint,
                pointer_transition_id,
                baseline_revision,
                baseline_report_id,
                baseline_fingerprint,
                current_report_id,
                current_report_fingerprint,
                state,
                catalog_generation_vector_fingerprint,
                dependency_index_watermark,
                dependency_index_fingerprint,
                dependency_index_complete,
                last_resolution_id,
                updated_at
            ) VALUES (
                %s, 'synthetic-test', %s, 1, 1, 1, %s, %s,
                1, %s, %s, %s, %s, 'revalidated', %s, 1, %s,
                true, %s, clock_timestamp()
            )
            """,
            (
                workspace_id,
                registry_id,
                registry_fingerprint,
                transition_id,
                report_id,
                baseline_fingerprint,
                report_id,
                report_fingerprint,
                vector_fingerprint,
                dependency_fingerprint,
                resolution_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.semantic_resource_bindings (
                binding_id,
                workspace_id,
                catalog_scope,
                registry_id,
                binding_revision,
                registry_generation,
                registry_version,
                registry_fingerprint,
                pointer_transition_id,
                mapping_decision_id,
                mapping_version,
                logical_field,
                connection_id,
                catalog_generation,
                catalog_generation_fingerprint,
                asset_key,
                asset_id,
                field_key,
                field_path,
                normalized_type,
                nullable,
                is_part_of_key,
                field_metadata_fingerprint,
                field_definition_fingerprint,
                field_terms_fingerprint,
                asset_metadata_fingerprint,
                evidence_fingerprint,
                binding_fingerprint,
                binding_state,
                approved_baseline_revision,
                approval_id,
                actor_id,
                decided_at,
                created_at
            ) VALUES (
                %s, %s, 'synthetic-test', %s, 1, 1, 1, %s, %s,
                %s, 1, 'Customer.customer_id', %s, 1, %s, %s, %s, %s,
                ARRAY['customer_id'], %s, %s, %s, %s, %s, %s, %s,
                %s, %s, 'approved', 1, %s, %s,
                clock_timestamp(), clock_timestamp()
            )
            """,
            (
                binding_id,
                workspace_id,
                registry_id,
                registry_fingerprint,
                transition_id,
                mapping_decision_id,
                connection_id,
                inventory_fingerprint,
                asset_key,
                asset_id,
                field_key,
                normalized_type,
                nullable,
                is_part_of_key,
                field_metadata_fingerprint,
                field_definition_fingerprint,
                field_terms_fingerprint,
                asset_metadata_fingerprint,
                binding_fingerprint,
                binding_fingerprint,
                approval_id,
                actor_id,
            ),
        )

    with psycopg.connect(catalog_database.runtime) as connection:
        current_gate = connection.execute(
            """
            SELECT
                current_catalog_generation,
                catalog_evidence_available,
                catalog_evidence_matches,
                gate_eligible
            FROM schemabridge_control.semantic_context_gate_projection
            WHERE workspace_id = %s
              AND catalog_scope = 'synthetic-test'
              AND registry_id = %s
              AND dependency_kind = 'mapping'
              AND dependency_id = %s
            """,
            (workspace_id, registry_id, mapping_decision_id),
        ).fetchone()
    assert current_gate == (1, True, True, True)

    with psycopg.connect(catalog_database.runtime) as connection:
        governed_scope = connection.execute(
            """
            SELECT *
            FROM schemabridge_control.load_query_studio_governance_scope(
                %s::varchar, 'synthetic-test'::varchar, %s::varchar
            )
            """,
            (workspace_id, registry_id),
        ).fetchone()
        assert governed_scope == (
            1,
            1,
            registry_fingerprint,
            transition_id,
            pointer_fingerprint,
            1,
            1,
            baseline_fingerprint,
            vector_fingerprint,
            1,
        )
        governed_row = connection.execute(
            """
            SELECT logical_field, binding_id, connection_id, qualified_name,
                   field_path, binding_state, normalized_type, native_type,
                   deterministic_score
            FROM schemabridge_control.search_governed_query_studio_fields(
                %s::varchar, 'synthetic-test'::varchar, %s::varchar,
                %s::bigint, %s::bigint, %s, %s::varchar, %s,
                %s::bigint, %s::bigint, %s, %s,
                'customer_id'::varchar, ARRAY[]::varchar[], false,
                50::integer, NULL::integer, NULL::varchar, NULL::varchar
            )
            """,
            (
                workspace_id,
                registry_id,
                *governed_scope[:9],
            ),
        ).fetchone()
        assert governed_row is not None
        assert governed_row[:8] == (
            "Customer.customer_id",
            binding_id,
            connection_id,
            "public.customers",
            ["customer_id"],
            "approved",
            "string",
            "varchar(20)",
        )
        assert int(governed_row[8]) > 0
        after_page = connection.execute(
            """
            SELECT logical_field
            FROM schemabridge_control.search_governed_query_studio_fields(
                %s::varchar, 'synthetic-test'::varchar, %s::varchar,
                %s::bigint, %s::bigint, %s, %s::varchar, %s,
                %s::bigint, %s::bigint, %s, %s,
                'customer_id'::varchar, ARRAY[]::varchar[], false,
                50::integer, %s::integer, %s::varchar, %s::varchar
            )
            """,
            (
                workspace_id,
                registry_id,
                *governed_scope[:9],
                governed_row[8],
                governed_row[0],
                governed_row[1],
            ),
        ).fetchone()
    assert after_page is None

    governed_page = PostgresGovernedBindingFactsSearch(dsn=catalog_database.runtime).search(
        GovernedBindingFactsRequest(
            scope=SemanticRegistryScope(
                workspace_id=workspace_id,
                catalog_scope="synthetic-test",
                registry_id=registry_id,
            ),
            query=DescriptionQuery("customer_id"),
            page_size=50,
        )
    )
    assert governed_page.rows_read == 1
    assert governed_page.scope.evidence_baseline_revision == 1
    assert governed_page.items[0].logical_field.root == "Customer.customer_id"
    assert governed_page.items[0].physical_field.root == "public.customers.customer_id"
    assert governed_page.items[0].physical_type.value == "string"
    assert governed_page.items[0].native_type == "varchar(20)"

    with (
        psycopg.connect(catalog_database.api) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        connection.execute(
            """
            SELECT *
            FROM schemabridge_control.load_query_studio_governance_scope(
                %s::varchar, 'synthetic-test'::varchar, %s::varchar
            )
            """,
            (workspace_id, registry_id),
        ).fetchone()

    second_refresh_id = f"refresh-{uuid4().hex[:12]}"
    second_lease_capability = "catalog-lease-second-capability-0123456789abcdef"
    second_lease_digest = _digest(second_lease_capability)
    second_fingerprint = _digest("catalog-inventory-empty")
    with psycopg.connect(catalog_database.api) as connection:
        connection.execute(
            """
            SELECT refresh_id
            FROM schemabridge_control.request_catalog_refresh(
                %s, %s, %s, 'full', %s, %s,
                'sb_platform_admin_v1', clock_timestamp()
            )
            """,
            (
                workspace_id,
                connection_id,
                second_refresh_id,
                _digest("refresh-request-second"),
                _digest("refresh-idempotency-second"),
            ),
        )

    with psycopg.connect(catalog_database.catalog) as connection:
        connection.execute(
            """
            UPDATE schemabridge_control.catalog_refresh_runs
            SET status = 'leased',
                base_generation = 1,
                target_generation = 2,
                lease_owner_id = 'catalog-indexer-b',
                lease_capability_digest = %s,
                fencing_token = 1,
                lease_acquired_at = clock_timestamp(),
                lease_heartbeat_at = clock_timestamp(),
                lease_expires_at = clock_timestamp() + interval '5 minutes',
                updated_at = clock_timestamp()
            WHERE workspace_id = %s
              AND connection_id = %s
              AND refresh_id = %s
            """,
            (
                second_lease_digest,
                workspace_id,
                connection_id,
                second_refresh_id,
            ),
        )
        second_route = connection.execute(
            """
            SELECT *
            FROM schemabridge_control.load_owned_catalog_connector_route_v2(
                %s, %s, %s, 'catalog-indexer-b', %s, 1, %s, %s, %s
            )
            """,
            (
                workspace_id,
                connection_id,
                second_refresh_id,
                second_lease_capability,
                target_facts.contract_version,
                target_facts.route_revision,
                target_facts.target_fingerprint,
            ),
        ).fetchone()
        assert second_route is not None
        connection.execute(
            """
            INSERT INTO schemabridge_control.catalog_generations (
                workspace_id,
                connection_id,
                generation,
                refresh_id,
                base_generation,
                refresh_mode,
                status,
                created_at
            ) VALUES (
                %s, %s, 2, %s, 1, 'full', 'staging', clock_timestamp()
            )
            """,
            (
                workspace_id,
                connection_id,
                second_refresh_id,
            ),
        )
        connection.execute(
            """
            UPDATE schemabridge_control.catalog_refresh_runs
            SET status = 'staging',
                lease_heartbeat_at = clock_timestamp(),
                lease_expires_at = clock_timestamp() + interval '5 minutes',
                updated_at = clock_timestamp()
            WHERE workspace_id = %s
              AND connection_id = %s
              AND refresh_id = %s
            """,
            (workspace_id, connection_id, second_refresh_id),
        )
        connection.execute(
            """
            UPDATE schemabridge_control.catalog_refresh_runs
            SET source_checkpoint = 'complete',
                source_page_number = 1,
                source_page_fingerprint = %s,
                staged_asset_count = 0,
                staged_field_count = 0,
                source_complete = true,
                lease_heartbeat_at = clock_timestamp(),
                lease_expires_at = clock_timestamp() + interval '5 minutes',
                updated_at = clock_timestamp()
            WHERE workspace_id = %s
              AND connection_id = %s
              AND refresh_id = %s
            """,
            (
                _digest("source-page-empty"),
                workspace_id,
                connection_id,
                second_refresh_id,
            ),
        )
        activated = connection.execute(
            """
            SELECT *
            FROM schemabridge_control.activate_catalog_generation_for_target(
                %s, %s, %s, 'catalog-indexer-b', %s,
                1, 2, 1, %s, %s, %s, %s
            )
            """,
            (
                workspace_id,
                connection_id,
                second_refresh_id,
                second_lease_capability,
                second_fingerprint,
                target_facts.contract_version,
                target_facts.route_revision,
                target_facts.target_fingerprint,
            ),
        ).fetchone()
        retained = connection.execute(
            """
            SELECT
                connection.active_generation,
                count(*) FILTER (WHERE tombstone.resource_kind = 'asset'),
                count(*) FILTER (WHERE tombstone.resource_kind = 'field'),
                extract(
                    epoch FROM generation.retain_until - clock_timestamp()
                )::integer
            FROM schemabridge_control.catalog_connections AS connection
            JOIN schemabridge_control.catalog_generations AS generation
              ON generation.workspace_id = connection.workspace_id
             AND generation.connection_id = connection.connection_id
             AND generation.generation = 1
            JOIN schemabridge_control.catalog_tombstones AS tombstone
              ON tombstone.workspace_id = connection.workspace_id
             AND tombstone.connection_id = connection.connection_id
             AND tombstone.observed_missing_in_generation = 2
            WHERE connection.workspace_id = %s
              AND connection.connection_id = %s
            GROUP BY connection.active_generation, generation.retain_until
            """,
            (workspace_id, connection_id),
        ).fetchone()
        pruned = connection.execute(
            """
            SELECT schemabridge_control.prune_catalog_generations(%s, %s, 10)
            """,
            (workspace_id, connection_id),
        ).fetchone()
    assert activated == (0, 0)
    assert retained is not None
    assert retained[:3] == (2, 1, 1)
    assert retained[3] >= 899
    assert pruned == (0,)
    with psycopg.connect(catalog_database.reconciler) as connection:
        durable_changes = connection.execute(
            """
            SELECT resource_kind, change_kind, count(*)
            FROM schemabridge_control.catalog_generation_changes
            WHERE workspace_id = %s
              AND connection_id = %s
              AND observed_generation = 2
            GROUP BY resource_kind, change_kind
            ORDER BY resource_kind
            """,
            (workspace_id, connection_id),
        ).fetchall()
        second_scans = connection.execute(
            """
            SELECT
                catalog_scope,
                registry_id,
                registry_generation,
                status,
                base_catalog_generation,
                observed_catalog_generation
            FROM schemabridge_control.semantic_change_scan_requests
            WHERE workspace_id = %s
              AND source_kind = 'catalog_generation'
              AND connection_id = %s
              AND observed_catalog_generation = 2
            ORDER BY registry_id
            """,
            (workspace_id, connection_id),
        ).fetchall()
    assert durable_changes == [("asset", "removed", 1), ("field", "removed", 1)]
    assert second_scans == [
        ("synthetic-test", item, None, "requested", 1, 2)
        for item in sorted((registry_id, second_registry_id))
    ]
    with psycopg.connect(catalog_database.runtime) as connection:
        stale_gate = connection.execute(
            """
            SELECT
                current_catalog_generation,
                catalog_evidence_available,
                catalog_evidence_matches,
                gate_eligible
            FROM schemabridge_control.semantic_context_gate_projection
            WHERE workspace_id = %s
              AND catalog_scope = 'synthetic-test'
              AND registry_id = %s
              AND dependency_kind = 'mapping'
              AND dependency_id = %s
            """,
            (workspace_id, registry_id, mapping_decision_id),
        ).fetchone()
    assert stale_gate == (2, False, False, False)
