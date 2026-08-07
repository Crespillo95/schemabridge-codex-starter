"""PostgreSQL proof for the bounded M29 operational observer identity."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations/control_plane"
ADMIN_DSN = "postgresql://postgres:local-only-not-a-secret@127.0.0.1:55434/postgres"
ALLOWED_RELATIONS = {
    "schema_migrations",
    "operational_queue_snapshot",
}
RAW_QUEUE_IDENTIFIERS = {
    "execution_jobs": "job_id",
    "catalog_refresh_runs": "refresh_id",
    "semantic_join_profile_jobs": "job_id",
    "registry_publication_jobs": "job_id",
    "semantic_change_scan_requests": "scan_id",
}


def _admin_dsn() -> str:
    return os.environ.get(
        "SCHEMABRIDGE_TEST_CONTROL_ADMIN_DATABASE_URL",
        ADMIN_DSN,
    )


def _role_dsn(role: str, database: str) -> str:
    return make_conninfo(
        _admin_dsn(),
        user=role,
        password=role,
        dbname=database,
    )


@pytest.fixture(scope="module")
def observer_database() -> Iterator[str]:
    database = f"schemabridge_observer_{uuid4().hex[:12]}"
    with psycopg.connect(_admin_dsn(), autocommit=True) as connection:
        role_exists = connection.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_roles
                WHERE rolname = 'schemabridge_observer'
            )
            """
        ).fetchone()
        assert role_exists == (True,)
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
                    schemabridge_observer,
                    schemabridge_backup
                """
            ).format(sql.Identifier(database))
        )

    migrator_dsn = _role_dsn("schemabridge_migrator", database)
    try:
        migrated = PostgresControlPlaneMigrator(
            migrator_dsn,
            MIGRATIONS,
        ).migrate()
        assert migrated.inspection.current_version == 15
        yield _role_dsn("schemabridge_observer", database)
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )


def test_observer_role_and_session_defaults_are_strict(
    observer_database: str,
) -> None:
    with psycopg.connect(_admin_dsn()) as admin:
        attributes = admin.execute(
            """
            SELECT
                rolcanlogin,
                rolsuper,
                rolcreatedb,
                rolcreaterole,
                rolinherit
            FROM pg_catalog.pg_roles
            WHERE rolname = 'schemabridge_observer'
            """
        ).fetchone()
        membership = admin.execute(
            """
            SELECT
                membership.admin_option,
                membership.inherit_option,
                membership.set_option
            FROM pg_catalog.pg_auth_members AS membership
            JOIN pg_catalog.pg_roles AS granted_role
              ON granted_role.oid = membership.roleid
            JOIN pg_catalog.pg_roles AS member_role
              ON member_role.oid = membership.member
            WHERE granted_role.rolname = 'schemabridge_observer'
              AND member_role.rolname = 'schemabridge_migrator'
            """
        ).fetchone()

    assert attributes == (True, False, False, False, False)
    assert membership == (False, False, True)

    with psycopg.connect(observer_database) as observer:
        identity = observer.execute(
            """
            SELECT
                session_user,
                current_user,
                current_setting('transaction_read_only'),
                EXTRACT(
                    EPOCH FROM current_setting('statement_timeout')::interval
                ) * 1000
            """
        ).fetchone()

    assert identity is not None
    assert identity[:3] == (
        "schemabridge_observer",
        "schemabridge_observer",
        "on",
    )
    assert 0 < int(identity[3]) <= 5_000


def test_observer_reads_only_bounded_operational_aggregates(
    observer_database: str,
) -> None:
    with psycopg.connect(observer_database) as observer:
        migration_count = observer.execute(
            """
            SELECT count(*)
            FROM schemabridge_control.schema_migrations
            """
        ).fetchone()
        aggregates = observer.execute(
            """
            SELECT queue, depth, oldest_due_age_seconds
            FROM schemabridge_control.operational_queue_snapshot
            ORDER BY queue
            """
        ).fetchall()
        view_metadata = observer.execute(
            """
            SELECT
                pg_catalog.pg_get_userbyid(relation.relowner),
                relation.relkind,
                relation.reloptions,
                pg_catalog.array_agg(
                    attribute.attname
                    ORDER BY attribute.attnum
                )
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            JOIN pg_catalog.pg_attribute AS attribute
              ON attribute.attrelid = relation.oid
             AND attribute.attnum > 0
             AND NOT attribute.attisdropped
            WHERE namespace.nspname = 'schemabridge_control'
              AND relation.relname = 'operational_queue_snapshot'
            GROUP BY
                relation.relowner,
                relation.relkind,
                relation.reloptions
            """
        ).fetchone()
        relation_privileges = observer.execute(
            """
            SELECT
                relation.relname,
                has_table_privilege(
                    current_user,
                    relation.oid,
                    'SELECT'
                ),
                has_table_privilege(
                    current_user,
                    relation.oid,
                    'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                )
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'schemabridge_control'
              AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
            ORDER BY relation.relname
            """
        ).fetchall()
        privileged_sequences = observer.execute(
            """
            SELECT count(*)
            FROM pg_catalog.pg_class AS sequence
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = sequence.relnamespace
            WHERE namespace.nspname = 'schemabridge_control'
              AND sequence.relkind = 'S'
              AND has_sequence_privilege(
                    current_user,
                    sequence.oid,
                    'USAGE,SELECT,UPDATE'
              )
            """
        ).fetchone()
        executable_routines = observer.execute(
            """
            SELECT count(*)
            FROM pg_catalog.pg_proc AS routine
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = routine.pronamespace
            WHERE namespace.nspname = 'schemabridge_control'
              AND has_function_privilege(
                    current_user,
                    routine.oid,
                    'EXECUTE'
              )
            """
        ).fetchone()
        can_create_in_schema = observer.execute(
            """
            SELECT has_schema_privilege(
                current_user,
                'schemabridge_control',
                'CREATE'
            )
            """
        ).fetchone()

    assert migration_count == (15,)
    assert [row[0] for row in aggregates] == [
        "catalog",
        "execution",
        "profile",
        "publication",
        "reconciliation",
    ]
    assert all(
        isinstance(depth, int)
        and depth >= 0
        and isinstance(oldest_due_age_seconds, int)
        and oldest_due_age_seconds >= 0
        for _, depth, oldest_due_age_seconds in aggregates
    )
    assert view_metadata is not None
    assert view_metadata[:2] == ("schemabridge_migrator", "v")
    assert set(view_metadata[2] or ()) == {
        "security_barrier=true",
        "security_invoker=false",
    }
    assert view_metadata[3] == [
        "queue",
        "depth",
        "oldest_due_age_seconds",
    ]
    assert {
        relation_name for relation_name, can_select, _ in relation_privileges if can_select
    } == ALLOWED_RELATIONS
    assert not any(can_write for _, _, can_write in relation_privileges)
    assert privileged_sequences == (0,)
    assert executable_routines == (0,)
    assert can_create_in_schema == (False,)


def test_observer_cannot_read_raw_queue_ids_mutate_or_escalate(
    observer_database: str,
) -> None:
    with psycopg.connect(observer_database, autocommit=True) as observer:
        observer.execute("SET default_transaction_read_only = off")

        for table_name, identifier_column in RAW_QUEUE_IDENTIFIERS.items():
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                observer.execute(
                    sql.SQL(
                        """
                        SELECT {identifier_column}
                        FROM schemabridge_control.{table_name}
                        LIMIT 1
                        """
                    ).format(
                        identifier_column=sql.Identifier(identifier_column),
                        table_name=sql.Identifier(table_name),
                    )
                )

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            observer.execute(
                """
                SELECT count(*)
                FROM schemabridge_control.control_audit_events
                """
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            observer.execute(
                """
                UPDATE schemabridge_control.execution_jobs
                SET status = status
                WHERE false
                """
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            observer.execute(
                """
                SELECT schemabridge_control.semantic_json_has_protected_keys(
                    '{}'::jsonb
                )
                """
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            observer.execute(
                """
                CREATE OR REPLACE VIEW
                    schemabridge_control.operational_queue_snapshot
                AS
                SELECT
                    'leaked'::text AS queue,
                    0::bigint AS depth,
                    0::bigint AS oldest_due_age_seconds
                """
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            observer.execute(
                """
                ALTER VIEW schemabridge_control.operational_queue_snapshot
                    SET (security_barrier = false)
                """
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            observer.execute("SET ROLE schemabridge_migrator")
