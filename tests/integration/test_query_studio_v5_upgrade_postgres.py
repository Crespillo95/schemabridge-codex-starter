"""Real PostgreSQL proof for the M27 v5→v8 and fail-closed v6→v8 upgrades."""

from __future__ import annotations

import hashlib
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.application.ports.control_plane_migrations import (
    ControlPlaneMigrationError,
    ControlPlaneMigrationErrorCode,
)
from schemabridge.bootstrap import require_current_control_plane_schema
from schemabridge.config import Settings

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations/control_plane"
ADMIN_DSN = "postgresql://postgres:local-only-not-a-secret@127.0.0.1:55434/postgres"
NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
WORKSPACE_ID = "workspace-m27-v5-upgrade"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
EU_ENDPOINT_ORIGIN_FINGERPRINT = "83203af93d5b1b9a2b7ab344441b99f7e19d980c881ba78de44e16073bd199c7"
CONTROL_ROLES = (
    "schemabridge_migrator",
    "schemabridge_runtime",
    "schemabridge_reconciler",
    "schemabridge_api",
    "schemabridge_worker",
    "schemabridge_catalog",
)


def _admin_dsn() -> str:
    return os.environ.get("SCHEMABRIDGE_TEST_CONTROL_ADMIN_DATABASE_URL", ADMIN_DSN)


def _role_dsn(role: str, database: str) -> str:
    return f"postgresql://{role}:{role}@127.0.0.1:55434/{database}"


def _create_database(database: str) -> None:
    with psycopg.connect(_admin_dsn(), autocommit=True) as connection:
        available_roles = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT rolname
                FROM pg_catalog.pg_roles
                WHERE rolname = ANY(%s)
                """,
                (list(CONTROL_ROLES),),
            )
        }
        if available_roles != set(CONTROL_ROLES):
            pytest.fail("the six control-plane roles must exist before the v5 upgrade test")
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
                sql.SQL(", ").join(sql.Identifier(role) for role in CONTROL_ROLES),
            )
        )


def _drop_database(database: str) -> None:
    with psycopg.connect(_admin_dsn(), autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
        )


def _runtime_settings(runtime_dsn: str) -> Settings:
    return Settings(
        _env_file=None,
        OPENAI_API_KEY=None,
        DATAHUB_GMS_TOKEN=None,
        SCHEMABRIDGE_CONTROL_PLANE_MODE="postgres",
        SCHEMABRIDGE_CONTROL_DATABASE_URL=runtime_dsn,
        SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY=(
            "m27-v5-upgrade-control-audit-key-with-byte-diversity"
        ),
        SCHEMABRIDGE_IDENTITY_MIGRATION_KEY=(
            "m27-v5-upgrade-identity-migration-key-with-diversity"
        ),
    )


def _capacity_policy_row(dsn: str) -> tuple[object, ...] | None:
    with psycopg.connect(dsn) as connection:
        return connection.execute(
            """
            SELECT workspace_id, connection_limit, asset_limit, field_limit,
                   api_requests_per_minute, api_window_seconds,
                   nonterminal_job_limit, catalog_cursor_ttl_seconds,
                   generation_retention_seconds, version, updated_by,
                   created_at, updated_at
            FROM schemabridge_control.tenant_capacity_policies
            WHERE workspace_id = %s
            """,
            (WORKSPACE_ID,),
        ).fetchone()


def _migration_subset(tmp_path: Path, latest: int, label: str) -> Path:
    migrations = tmp_path / label
    migrations.mkdir()
    for version in range(1, latest + 1):
        source = next(MIGRATIONS.glob(f"{version:04d}_*.sql"))
        shutil.copyfile(source, migrations / source.name)
    return migrations


def _apply_ai_policy(dsn: str, workspace_id: str) -> None:
    with psycopg.connect(dsn) as connection:
        applied = connection.execute(
            """
            SELECT *
            FROM schemabridge_control.apply_tenant_ai_policy(
                %s, 0, true, true, %s, 'gpt-5-nano-2025-08-07',
                'eu', %s, %s, 50, 10000, 10000, 4, 10, 2592000,
                'sb_platform_admin_v1', 'APPLY TENANT AI POLICY'
            )
            """,
            (workspace_id, SHA_A, EU_ENDPOINT_ORIGIN_FINGERPRINT, SHA_C),
        ).fetchone()
    assert applied is not None


def _reserve_ai_attempt(
    dsn: str,
    workspace_id: str,
    request_id: str,
    *,
    estimated_input: int,
    estimated_output: int,
    capability: str,
) -> tuple[object, ...]:
    digest = hashlib.sha256(request_id.encode()).hexdigest()
    with psycopg.connect(dsn) as connection:
        reserved = connection.execute(
            """
            SELECT *
            FROM schemabridge_control.reserve_ai_provider_attempt(
                %s, %s, %s, 'expansion', 1::smallint, %s,
                %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                workspace_id,
                request_id,
                SHA_A,
                digest,
                SHA_A,
                SHA_B,
                SHA_C,
                SHA_C,
                estimated_input,
                estimated_output,
                capability,
            ),
        ).fetchone()
    assert reserved is not None
    assert reserved[0] == "reserved"
    return reserved


def _settle_ai_attempt(
    dsn: str,
    workspace_id: str,
    reserved: tuple[object, ...],
    capability: str,
    *,
    outcome: str,
    observed_input: int,
    observed_output: int,
) -> tuple[object, ...]:
    with psycopg.connect(dsn) as connection:
        settled = connection.execute(
            """
            SELECT *
            FROM schemabridge_control.settle_ai_provider_attempt(
                %s, %s, %s, %s, %s, %s, %s, 10
            )
            """,
            (
                workspace_id,
                reserved[2],
                capability,
                reserved[4],
                outcome,
                observed_input,
                observed_output,
            ),
        ).fetchone()
    assert settled is not None
    return settled


def test_v5_to_v9_preserves_state_and_keeps_runtime_non_migrating(
    tmp_path: Path,
) -> None:
    database = f"schemabridge_m27_upgrade_{uuid4().hex[:12]}"
    migrator_dsn = _role_dsn("schemabridge_migrator", database)
    runtime_dsn = _role_dsn("schemabridge_runtime", database)
    _create_database(database)
    try:
        v5_migrations = _migration_subset(tmp_path, 5, "control-v5")

        v5 = PostgresControlPlaneMigrator(migrator_dsn, v5_migrations).migrate()
        assert v5.applied_versions == (1, 2, 3, 4, 5)
        assert v5.inspection.current_version == 5

        expected_state = (
            WORKSPACE_ID,
            10,
            5_434,
            41_028,
            1_000,
            60,
            100,
            900,
            3_600,
            7,
            "sb_platform_admin_v1",
            NOW,
            NOW,
        )
        with psycopg.connect(migrator_dsn) as connection:
            connection.execute(
                """
                INSERT INTO schemabridge_control.tenant_capacity_policies (
                    workspace_id, connection_limit, asset_limit, field_limit,
                    api_requests_per_minute, api_window_seconds,
                    nonterminal_job_limit, catalog_cursor_ttl_seconds,
                    generation_retention_seconds, version, updated_by,
                    created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                expected_state,
            )
            history_before = connection.execute(
                """
                SELECT version, name, checksum
                FROM schemabridge_control.schema_migrations
                ORDER BY version
                """
            ).fetchall()
            v6_relation_before = connection.execute(
                "SELECT to_regclass('schemabridge_control.tenant_ai_policies')"
            ).fetchone()
        assert _capacity_policy_row(migrator_dsn) == expected_state
        assert [row[0] for row in history_before] == [1, 2, 3, 4, 5]
        assert v6_relation_before == (None,)

        with pytest.raises(ControlPlaneMigrationError) as stale_runtime:
            require_current_control_plane_schema(
                credential_kind="runtime",
                repository_root=ROOT,
                settings=_runtime_settings(runtime_dsn),
            )
        assert stale_runtime.value.code is ControlPlaneMigrationErrorCode.SCHEMA_NOT_CURRENT

        with psycopg.connect(migrator_dsn) as connection:
            history_after_runtime = connection.execute(
                """
                SELECT version, name, checksum
                FROM schemabridge_control.schema_migrations
                ORDER BY version
                """
            ).fetchall()
            v6_relation_after_runtime = connection.execute(
                "SELECT to_regclass('schemabridge_control.tenant_ai_policies')"
            ).fetchone()
        assert history_after_runtime == history_before
        assert v6_relation_after_runtime == (None,)
        assert _capacity_policy_row(migrator_dsn) == expected_state

        release_migrator = PostgresControlPlaneMigrator(migrator_dsn, MIGRATIONS)
        known = release_migrator.known_migrations()
        upgraded = release_migrator.migrate()
        assert upgraded.applied_versions == (6, 7, 8, 9)
        assert upgraded.inspection.current_version == 9
        assert upgraded.inspection.is_current is True

        with psycopg.connect(migrator_dsn) as connection:
            complete_history = connection.execute(
                """
                SELECT version, name, checksum, applied_by
                FROM schemabridge_control.schema_migrations
                ORDER BY version
                """
            ).fetchall()
            new_table_counts = connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM schemabridge_control.tenant_ai_policies),
                    (SELECT count(*) FROM schemabridge_control.ai_provider_usage_audit)
                """
            ).fetchone()
            privileges = {
                str(row[0]): tuple(bool(value) for value in row[1:])
                for row in connection.execute(
                    """
                    WITH functions AS (
                        SELECT
                            max(oid) FILTER (
                                WHERE proname = 'load_tenant_ai_policy'
                            ) AS load_policy,
                            max(oid) FILTER (
                                WHERE proname = 'apply_tenant_ai_policy'
                            ) AS apply_policy,
                            max(oid) FILTER (
                                WHERE proname = 'reserve_ai_provider_attempt'
                            ) AS reserve_attempt
                        FROM pg_catalog.pg_proc
                        WHERE pronamespace = (
                            SELECT oid
                            FROM pg_catalog.pg_namespace
                            WHERE nspname = 'schemabridge_control'
                        )
                    )
                    SELECT role_name,
                           has_schema_privilege(
                               role_name, 'schemabridge_control', 'CREATE'
                           ),
                           has_table_privilege(
                               role_name,
                               'schemabridge_control.schema_migrations',
                               'INSERT'
                           ),
                           has_table_privilege(
                               role_name,
                               'schemabridge_control.tenant_ai_policies',
                               'SELECT'
                           ),
                           has_function_privilege(
                               role_name, functions.load_policy, 'EXECUTE'
                           ),
                           has_function_privilege(
                               role_name, functions.apply_policy, 'EXECUTE'
                           ),
                           has_function_privilege(
                               role_name, functions.reserve_attempt, 'EXECUTE'
                           )
                    FROM unnest(%s::text[]) AS role_name
                    CROSS JOIN functions
                    ORDER BY role_name
                    """,
                    (list(CONTROL_ROLES),),
                )
            }

        assert _capacity_policy_row(migrator_dsn) == expected_state
        assert [(row[0], row[1], row[2]) for row in complete_history] == [
            (definition.version, definition.name, definition.checksum) for definition in known
        ]
        assert complete_history[-1][3] == "schemabridge_migrator"
        assert complete_history[:5] == [(*row, "schemabridge_migrator") for row in history_before]
        assert new_table_counts == (0, 0)
        assert privileges["schemabridge_migrator"] == (
            True,
            True,
            True,
            True,
            True,
            True,
        )
        assert privileges["schemabridge_runtime"] == (
            False,
            False,
            False,
            True,
            False,
            True,
        )
        for role in (
            "schemabridge_reconciler",
            "schemabridge_api",
            "schemabridge_worker",
            "schemabridge_catalog",
        ):
            assert privileges[role] == (False, False, False, False, False, False)

        current_runtime = require_current_control_plane_schema(
            credential_kind="runtime",
            repository_root=ROOT,
            settings=_runtime_settings(runtime_dsn),
        )
        assert current_runtime.current_version == 9
        with psycopg.connect(runtime_dsn) as connection:
            assert (
                connection.execute(
                    "SELECT * FROM schemabridge_control.load_tenant_ai_policy(%s)",
                    (WORKSPACE_ID,),
                ).fetchone()
                is None
            )
            with (
                pytest.raises(psycopg.errors.InsufficientPrivilege),
                connection.transaction(),
            ):
                connection.execute(
                    "SELECT count(*) FROM schemabridge_control.tenant_ai_policies"
                ).fetchone()
    finally:
        _drop_database(database)


def test_v6_to_v8_aborts_on_invalid_historical_success_without_rewriting(
    tmp_path: Path,
) -> None:
    database = f"schemabridge_m27_invalid_{uuid4().hex[:12]}"
    migrator_dsn = _role_dsn("schemabridge_migrator", database)
    runtime_dsn = _role_dsn("schemabridge_runtime", database)
    workspace_id = "workspace-m27-v6-invalid"
    capability = "opaque-capability-v6-invalid-012345"
    _create_database(database)
    try:
        v6_migrations = _migration_subset(tmp_path, 6, "control-v6-invalid-usage")

        v6_migrator = PostgresControlPlaneMigrator(migrator_dsn, v6_migrations)
        v6 = v6_migrator.migrate()
        assert v6.applied_versions == (1, 2, 3, 4, 5, 6)
        assert v6.inspection.current_version == 6

        with psycopg.connect(migrator_dsn) as connection:
            connection.execute(
                """
                SELECT *
                FROM schemabridge_control.apply_tenant_ai_policy(
                    %s, 0, true, true, %s, 'gpt-5-nano-2025-08-07',
                    'eu', %s, %s, 20, 1000, 1000, 2, 60, 2592000,
                    'sb_platform_admin_v1', 'APPLY TENANT AI POLICY'
                )
                """,
                (workspace_id, SHA_A, EU_ENDPOINT_ORIGIN_FINGERPRINT, SHA_C),
            ).fetchone()

        with psycopg.connect(runtime_dsn) as connection:
            reserved = connection.execute(
                """
                SELECT *
                FROM schemabridge_control.reserve_ai_provider_attempt(
                    %s, 'request-invalid-history', %s, 'expansion',
                    1::smallint, %s, %s, %s, %s, %s, 10, 5, %s
                )
                """,
                (
                    workspace_id,
                    SHA_A,
                    SHA_A,
                    SHA_A,
                    SHA_B,
                    SHA_C,
                    SHA_C,
                    capability,
                ),
            ).fetchone()
            assert reserved is not None
            assert reserved[0] == "reserved"
            invalid_settlement = connection.execute(
                """
                SELECT *
                FROM schemabridge_control.settle_ai_provider_attempt(
                    %s, %s, %s, %s, 'succeeded', 0, 0, 10
                )
                """,
                (workspace_id, reserved[2], capability, reserved[4]),
            ).fetchone()
            assert invalid_settlement is not None
            assert invalid_settlement[3:6] == ("succeeded", 0, 0)

        with psycopg.connect(migrator_dsn) as connection:
            history_before = connection.execute(
                """
                SELECT version, name, checksum
                FROM schemabridge_control.schema_migrations
                ORDER BY version
                """
            ).fetchall()
            reservation_before = connection.execute(
                """
                SELECT status, outcome_code, observed_input_tokens,
                       observed_output_tokens, charged_input_tokens,
                       charged_output_tokens, settlement_fingerprint, settled_at
                FROM schemabridge_control.ai_provider_attempt_reservations
                WHERE reservation_id = %s
                """,
                (reserved[2],),
            ).fetchone()
            audit_before = connection.execute(
                """
                SELECT input_tokens, output_tokens, duration_ms, outcome_code,
                       occurred_at, retain_until
                FROM schemabridge_control.ai_provider_usage_audit
                WHERE reservation_id = %s
                """,
                (reserved[2],),
            ).fetchone()

        release_migrator = PostgresControlPlaneMigrator(migrator_dsn, MIGRATIONS)
        with pytest.raises(ControlPlaneMigrationError) as rejected:
            release_migrator.migrate()
        assert rejected.value.code is ControlPlaneMigrationErrorCode.APPLY_FAILED
        assert str(rejected.value) == "control-plane migration failed and was rolled back"

        with psycopg.connect(migrator_dsn) as connection:
            history_after = connection.execute(
                """
                SELECT version, name, checksum
                FROM schemabridge_control.schema_migrations
                ORDER BY version
                """
            ).fetchall()
            reservation_after = connection.execute(
                """
                SELECT status, outcome_code, observed_input_tokens,
                       observed_output_tokens, charged_input_tokens,
                       charged_output_tokens, settlement_fingerprint, settled_at
                FROM schemabridge_control.ai_provider_attempt_reservations
                WHERE reservation_id = %s
                """,
                (reserved[2],),
            ).fetchone()
            audit_after = connection.execute(
                """
                SELECT input_tokens, output_tokens, duration_ms, outcome_code,
                       occurred_at, retain_until
                FROM schemabridge_control.ai_provider_usage_audit
                WHERE reservation_id = %s
                """,
                (reserved[2],),
            ).fetchone()
            v7_constraint = connection.execute(
                """
                SELECT count(*)
                FROM pg_catalog.pg_constraint
                WHERE conname = 'ai_attempt_terminal_usage_accounting_v7'
                """
            ).fetchone()
            settlement_definition = connection.execute(
                """
                SELECT pg_catalog.pg_get_functiondef(
                    'schemabridge_control.settle_ai_provider_attempt(
                        varchar,varchar,varchar,bigint,varchar,integer,integer,integer
                    )'::regprocedure
                )
                """
            ).fetchone()

        assert [row[0] for row in history_before] == [1, 2, 3, 4, 5, 6]
        assert history_after == history_before
        assert reservation_after == reservation_before
        assert audit_after == audit_before
        assert v7_constraint == (0,)
        assert settlement_definition is not None
        assert "p_observed_input_tokens < 1" not in str(settlement_definition[0])
        assert v6_migrator.require_current().current_version == 6
        pending = release_migrator.inspect()
        assert pending.current_version == 6
        assert tuple(item.version for item in pending.pending) == (7, 8, 9)
    finally:
        _drop_database(database)


def test_populated_v6_success_failure_and_expiry_upgrade_to_v9_with_exact_acl(
    tmp_path: Path,
) -> None:
    database = f"schemabridge_m27_populated_{uuid4().hex[:12]}"
    migrator_dsn = _role_dsn("schemabridge_migrator", database)
    runtime_dsn = _role_dsn("schemabridge_runtime", database)
    workspace_id = "workspace-m27-v6-populated"
    capability = "opaque-capability-v6-populated-012345"
    _create_database(database)
    try:
        v6_migrations = _migration_subset(tmp_path, 6, "control-v6-populated")
        v6_migrator = PostgresControlPlaneMigrator(migrator_dsn, v6_migrations)
        assert v6_migrator.migrate().inspection.current_version == 6
        _apply_ai_policy(migrator_dsn, workspace_id)

        succeeded = _reserve_ai_attempt(
            runtime_dsn,
            workspace_id,
            "request-populated-success",
            estimated_input=10,
            estimated_output=5,
            capability=capability,
        )
        success_settlement = _settle_ai_attempt(
            runtime_dsn,
            workspace_id,
            succeeded,
            capability,
            outcome="succeeded",
            observed_input=8,
            observed_output=4,
        )
        assert success_settlement[3:6] == ("succeeded", 8, 4)

        failed = _reserve_ai_attempt(
            runtime_dsn,
            workspace_id,
            "request-populated-failure",
            estimated_input=7,
            estimated_output=3,
            capability=capability,
        )
        failure_settlement = _settle_ai_attempt(
            runtime_dsn,
            workspace_id,
            failed,
            capability,
            outcome="timeout",
            observed_input=0,
            observed_output=0,
        )
        assert failure_settlement[3:6] == ("timeout", 7, 3)

        expiring = _reserve_ai_attempt(
            runtime_dsn,
            workspace_id,
            "request-populated-expiry",
            estimated_input=6,
            estimated_output=2,
            capability=capability,
        )
        with psycopg.connect(migrator_dsn) as connection:
            connection.execute(
                """
                ALTER TABLE schemabridge_control.ai_provider_attempt_reservations
                DISABLE TRIGGER ai_provider_attempt_reservations_guard
                """
            )
            connection.execute(
                """
                UPDATE schemabridge_control.ai_provider_attempt_reservations
                SET lease_expires_at = lease_acquired_at + interval '1 millisecond'
                WHERE reservation_id = %s
                """,
                (expiring[2],),
            )
            connection.execute(
                """
                ALTER TABLE schemabridge_control.ai_provider_attempt_reservations
                ENABLE TRIGGER ai_provider_attempt_reservations_guard
                """
            )
        with psycopg.connect(runtime_dsn) as connection:
            connection.execute("SELECT pg_sleep(0.01)")
            expired_count = connection.execute(
                """
                SELECT schemabridge_control.expire_ai_provider_attempts(%s, 10)
                """,
                (workspace_id,),
            ).fetchone()
        assert expired_count == (1,)

        with psycopg.connect(migrator_dsn) as connection:
            reservations_before = connection.execute(
                """
                SELECT request_id, status, outcome_code,
                       observed_input_tokens, observed_output_tokens,
                       charged_input_tokens, charged_output_tokens,
                       duration_ms, settlement_fingerprint, settled_at
                FROM schemabridge_control.ai_provider_attempt_reservations
                WHERE workspace_id = %s
                ORDER BY request_id
                """,
                (workspace_id,),
            ).fetchall()
            audits_before = connection.execute(
                """
                SELECT audit_id, reservation_id, workspace_scope_digest,
                       input_tokens, output_tokens, duration_ms, outcome_code,
                       occurred_at, retain_until
                FROM schemabridge_control.ai_provider_usage_audit
                WHERE request_id LIKE 'request-populated-%'
                ORDER BY request_id
                """
            ).fetchall()
        assert [row[2] for row in reservations_before] == [
            "expired_crash",
            "timeout",
            "succeeded",
        ]

        v7_migrations = _migration_subset(tmp_path, 7, "control-v7-populated")
        upgraded_v7 = PostgresControlPlaneMigrator(
            migrator_dsn,
            v7_migrations,
        ).migrate()
        assert upgraded_v7.applied_versions == (7,)
        assert upgraded_v7.inspection.current_version == 7

        with psycopg.connect(migrator_dsn) as connection:
            connection.execute(
                """
                GRANT EXECUTE ON FUNCTION
                    schemabridge_control.settle_ai_provider_attempt(
                        varchar, varchar, varchar, bigint, varchar,
                        integer, integer, integer
                    )
                TO PUBLIC,
                   schemabridge_api,
                   schemabridge_worker,
                   schemabridge_catalog,
                   schemabridge_reconciler
                """
            )

        release = PostgresControlPlaneMigrator(migrator_dsn, MIGRATIONS)
        upgraded_release = release.migrate()
        assert upgraded_release.applied_versions == (8, 9)
        assert upgraded_release.inspection.current_version == 9

        with psycopg.connect(migrator_dsn) as connection:
            reservations_after = connection.execute(
                """
                SELECT request_id, status, outcome_code,
                       observed_input_tokens, observed_output_tokens,
                       charged_input_tokens, charged_output_tokens,
                       duration_ms, settlement_fingerprint, settled_at
                FROM schemabridge_control.ai_provider_attempt_reservations
                WHERE workspace_id = %s
                ORDER BY request_id
                """,
                (workspace_id,),
            ).fetchall()
            audits_after = connection.execute(
                """
                SELECT audit_id, reservation_id, workspace_scope_digest,
                       input_tokens, output_tokens, duration_ms, outcome_code,
                       occurred_at, retain_until
                FROM schemabridge_control.ai_provider_usage_audit
                WHERE request_id LIKE 'request-populated-%'
                ORDER BY request_id
                """
            ).fetchall()
            privileges = connection.execute(
                """
                SELECT role_name,
                       has_function_privilege(
                           role_name,
                           'schemabridge_control.settle_ai_provider_attempt(
                                varchar,varchar,varchar,bigint,varchar,
                                integer,integer,integer
                           )',
                           'EXECUTE'
                       )
                FROM unnest(%s::text[]) AS role_name
                ORDER BY role_name
                """,
                (list(CONTROL_ROLES),),
            ).fetchall()

        assert reservations_after == reservations_before
        assert audits_after == audits_before
        assert dict(privileges) == {
            "schemabridge_api": False,
            "schemabridge_catalog": False,
            "schemabridge_migrator": True,
            "schemabridge_reconciler": False,
            "schemabridge_runtime": True,
            "schemabridge_worker": False,
        }

        with psycopg.connect(runtime_dsn) as connection:
            replay = connection.execute(
                """
                SELECT *
                FROM schemabridge_control.settle_ai_provider_attempt(
                    %s, %s, %s, %s, 'succeeded', 8, 4, 10
                )
                """,
                (workspace_id, succeeded[2], capability, succeeded[4]),
            ).fetchone()
        assert replay is not None
        assert replay[1] is True
        assert replay[3:6] == ("succeeded", 8, 4)
    finally:
        _drop_database(database)


@pytest.mark.parametrize(
    "invalid_derivation",
    (
        "audit_id",
        "workspace_scope_digest",
        "retain_until",
        "settlement_fingerprint",
    ),
)
def test_v8_aborts_on_invalid_audit_derivation_without_rewriting(
    tmp_path: Path,
    invalid_derivation: str,
) -> None:
    database = f"schemabridge_m27_bad_audit_{uuid4().hex[:10]}"
    migrator_dsn = _role_dsn("schemabridge_migrator", database)
    runtime_dsn = _role_dsn("schemabridge_runtime", database)
    workspace_id = f"workspace-m27-bad-{invalid_derivation.replace('_', '-')}"
    capability = "opaque-capability-v8-bad-audit-012345"
    _create_database(database)
    try:
        v7_migrations = _migration_subset(
            tmp_path,
            7,
            f"control-v7-bad-{invalid_derivation}",
        )
        v7_migrator = PostgresControlPlaneMigrator(migrator_dsn, v7_migrations)
        assert v7_migrator.migrate().inspection.current_version == 7
        _apply_ai_policy(migrator_dsn, workspace_id)
        reserved = _reserve_ai_attempt(
            runtime_dsn,
            workspace_id,
            f"request-bad-{invalid_derivation}",
            estimated_input=10,
            estimated_output=5,
            capability=capability,
        )
        _settle_ai_attempt(
            runtime_dsn,
            workspace_id,
            reserved,
            capability,
            outcome="succeeded",
            observed_input=8,
            observed_output=4,
        )

        with psycopg.connect(migrator_dsn) as connection:
            connection.execute(
                """
                ALTER TABLE schemabridge_control.ai_provider_usage_audit
                DISABLE TRIGGER ai_provider_usage_audit_immutable
                """
            )
            if invalid_derivation == "audit_id":
                connection.execute(
                    """
                    UPDATE schemabridge_control.ai_provider_usage_audit
                    SET audit_id = %s
                    WHERE reservation_id = %s
                    """,
                    ("aia_" + SHA_D, reserved[2]),
                )
            elif invalid_derivation == "workspace_scope_digest":
                connection.execute(
                    """
                    UPDATE schemabridge_control.ai_provider_usage_audit
                    SET workspace_scope_digest = %s
                    WHERE reservation_id = %s
                    """,
                    (SHA_D, reserved[2]),
                )
            elif invalid_derivation == "settlement_fingerprint":
                connection.execute(
                    """
                    ALTER TABLE
                        schemabridge_control.ai_provider_attempt_reservations
                    DISABLE TRIGGER ai_provider_attempt_reservations_guard
                    """
                )
                connection.execute(
                    """
                    UPDATE schemabridge_control.ai_provider_attempt_reservations
                    SET settlement_fingerprint = %s
                    WHERE reservation_id = %s
                    """,
                    (SHA_D, reserved[2]),
                )
                connection.execute(
                    """
                    ALTER TABLE
                        schemabridge_control.ai_provider_attempt_reservations
                    ENABLE TRIGGER ai_provider_attempt_reservations_guard
                    """
                )
                connection.execute(
                    """
                    UPDATE schemabridge_control.ai_provider_usage_audit
                    SET audit_id = 'aia_' || encode(
                        sha256(convert_to(reservation_id || %s, 'UTF8')),
                        'hex'
                    )
                    WHERE reservation_id = %s
                    """,
                    (SHA_D, reserved[2]),
                )
            else:
                connection.execute(
                    """
                    UPDATE schemabridge_control.ai_provider_usage_audit
                    SET retain_until = retain_until + interval '1 second'
                    WHERE reservation_id = %s
                    """,
                    (reserved[2],),
                )
            connection.execute(
                """
                ALTER TABLE schemabridge_control.ai_provider_usage_audit
                ENABLE TRIGGER ai_provider_usage_audit_immutable
                """
            )

        with psycopg.connect(migrator_dsn) as connection:
            history_before = connection.execute(
                """
                SELECT version, name, checksum
                FROM schemabridge_control.schema_migrations
                ORDER BY version
                """
            ).fetchall()
            audit_before = connection.execute(
                """
                SELECT audit_id, reservation_id, workspace_scope_digest,
                       input_tokens, output_tokens, duration_ms, outcome_code,
                       occurred_at, retain_until
                FROM schemabridge_control.ai_provider_usage_audit
                WHERE reservation_id = %s
                """,
                (reserved[2],),
            ).fetchone()
            reservation_before = connection.execute(
                """
                SELECT settlement_fingerprint
                FROM schemabridge_control.ai_provider_attempt_reservations
                WHERE reservation_id = %s
                """,
                (reserved[2],),
            ).fetchone()

        release = PostgresControlPlaneMigrator(migrator_dsn, MIGRATIONS)
        with pytest.raises(ControlPlaneMigrationError) as rejected:
            release.migrate()
        assert rejected.value.code is ControlPlaneMigrationErrorCode.APPLY_FAILED

        with psycopg.connect(migrator_dsn) as connection:
            history_after = connection.execute(
                """
                SELECT version, name, checksum
                FROM schemabridge_control.schema_migrations
                ORDER BY version
                """
            ).fetchall()
            audit_after = connection.execute(
                """
                SELECT audit_id, reservation_id, workspace_scope_digest,
                       input_tokens, output_tokens, duration_ms, outcome_code,
                       occurred_at, retain_until
                FROM schemabridge_control.ai_provider_usage_audit
                WHERE reservation_id = %s
                """,
                (reserved[2],),
            ).fetchone()
            reservation_after = connection.execute(
                """
                SELECT settlement_fingerprint
                FROM schemabridge_control.ai_provider_attempt_reservations
                WHERE reservation_id = %s
                """,
                (reserved[2],),
            ).fetchone()
            wrapper = connection.execute(
                """
                SELECT to_regprocedure(
                    'schemabridge_control.settle_ai_provider_attempt_v7_core(
                        varchar,varchar,varchar,bigint,varchar,
                        integer,integer,integer
                    )'
                )
                """
            ).fetchone()
        assert [row[0] for row in history_before] == [1, 2, 3, 4, 5, 6, 7]
        assert history_after == history_before
        assert audit_after == audit_before
        assert reservation_after == reservation_before
        assert wrapper == (None,)
        assert v7_migrator.require_current().current_version == 7
        pending = release.inspect()
        assert pending.current_version == 7
        assert tuple(item.version for item in pending.pending) == (8, 9)
    finally:
        _drop_database(database)
