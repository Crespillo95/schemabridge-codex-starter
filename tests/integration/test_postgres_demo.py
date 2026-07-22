"""Integration contract for the synthetic PostgreSQL database."""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


def test_postgres_health_adapter_reports_safe_reader(reader_dsn: str) -> None:
    from schemabridge.adapters.postgres.health import PsycopgDatabaseHealthProbe
    from schemabridge.application.postgres_health import CheckDatabaseReadiness

    report = CheckDatabaseReadiness(
        probe=PsycopgDatabaseHealthProbe(reader_dsn),
        expected_user="schemabridge_reader",
        expected_statement_timeout_ms=5_000,
    ).execute()

    assert report.is_ready is True
    assert report.details.database == "schemabridge"
    assert report.details.server_version.startswith("16.")


def test_postgres_seeded_row_counts_and_reader_access(reader_dsn: str) -> None:
    import psycopg

    expected_counts = {
        "crm.customers": 7,
        "legacy.client_master": 7,
        "bank.accounts": 9,
        "bank.account_holders": 9,
        "reporting.customer_accounts": 6,
    }

    with psycopg.connect(reader_dsn) as connection:
        for table, expected_count in expected_counts.items():
            schema_name, table_name = table.split(".", maxsplit=1)
            with connection.cursor() as cursor:
                cursor.execute(
                    psycopg.sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
                        psycopg.sql.Identifier(schema_name),
                        psycopg.sql.Identifier(table_name),
                    )
                )
                row = cursor.fetchone()
            assert row == (expected_count,)


def test_postgres_schema_comments_constraints_and_cluster_settings(reader_dsn: str) -> None:
    import psycopg

    expected_schemas = {"bank", "crm", "legacy", "reporting"}
    expected_constraints = {
        "account_holders_account_number_fkey",
        "account_holders_pkey",
        "accounts_pkey",
        "bank_account_balance_nonnegative",
        "bank_holder_date_order",
        "bank_holder_role_known",
        "client_master_pkey",
        "crm_customer_id_format",
        "customer_accounts_pkey",
        "customers_pkey",
    }

    with psycopg.connect(reader_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT nspname, obj_description(oid, 'pg_namespace')
            FROM pg_namespace
            WHERE nspname IN ('bank', 'crm', 'legacy', 'reporting')
            """
        )
        schemas = cursor.fetchall()
        cursor.execute(
            """
            SELECT conname
            FROM pg_constraint AS constraint_record
            INNER JOIN pg_namespace AS namespace
                ON namespace.oid = constraint_record.connamespace
            WHERE namespace.nspname IN ('bank', 'crm', 'legacy', 'reporting')
            """
        )
        constraints = {row[0] for row in cursor.fetchall()}
        cursor.execute("SELECT current_setting('TimeZone'), current_setting('data_checksums')")
        cluster_settings = cursor.fetchone()

    assert {row[0] for row in schemas} == expected_schemas
    assert all(row[1] for row in schemas)
    assert expected_constraints <= constraints
    assert cluster_settings == ("UTC", "on")


def test_postgres_reference_query_matches_ground_truth(reader_dsn: str) -> None:
    import psycopg

    query = (ROOT / "demo/reference/north_star.sql").read_text(encoding="utf-8")
    ground_truth = yaml.safe_load(
        (ROOT / "demo/ground_truth/query_cases.yml").read_text(encoding="utf-8")
    )
    expected = ground_truth["cases"][0]["expected_rows"]
    expected_rows = [
        (row["registration_date"], row["secondary_holder_customers"]) for row in expected
    ]

    with psycopg.connect(reader_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(query)
        actual_rows = cursor.fetchall()

    assert (
        actual_rows
        == expected_rows
        == [
            (date(2026, 1, 1), 2),
            (date(2026, 1, 2), 1),
            (date(2026, 1, 3), 1),
        ]
    )


def test_postgres_duplicate_relationship_demonstrates_fanout(reader_dsn: str) -> None:
    import psycopg

    query = """
        SELECT COUNT(*), COUNT(DISTINCT gf_customer_id)
        FROM bank.account_holders
        WHERE gf_customer_id = 123.0
          AND holder_type IN ('SECONDARY', '2', 'CO_HOLDER')
    """
    with psycopg.connect(reader_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(query)
        row = cursor.fetchone()

    assert row == (2, 1)


def test_postgres_invalid_identifiers_have_distinct_rejection_paths(reader_dsn: str) -> None:
    import psycopg

    query = """
        SELECT
            account_number,
            gf_customer_id,
            CASE
                WHEN gf_customer_id IS NULL THEN 'null_join_key'
                WHEN gf_customer_id IN (
                    'NaN'::DOUBLE PRECISION,
                    'Infinity'::DOUBLE PRECISION,
                    '-Infinity'::DOUBLE PRECISION
                ) THEN 'non_finite_identifier'
                WHEN gf_customer_id <> TRUNC(gf_customer_id) THEN 'non_integral_identifier'
            END AS rejection_reason
        FROM bank.account_holders
        WHERE account_number IN ('ACC-007', 'ACC-008', 'ACC-009')
        ORDER BY account_number
    """
    with psycopg.connect(reader_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(query)
        rows = cursor.fetchall()

    ground_truth = yaml.safe_load(
        (ROOT / "demo/ground_truth/query_cases.yml").read_text(encoding="utf-8")
    )
    expected_reasons = [
        rejection["reason"] for rejection in ground_truth["cases"][0]["expected_rejections"]
    ]

    assert rows[0] == ("ACC-007", 127.5, "non_integral_identifier")
    assert rows[1][0] == "ACC-008"
    assert math.isnan(rows[1][1])
    assert rows[1][2] == "non_finite_identifier"
    assert rows[2] == ("ACC-009", None, "null_join_key")
    assert [row[2] for row in rows] == expected_reasons


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO crm.customers VALUES ('00000000999', CURRENT_DATE, 'ES', 'ACTIVE')",
        "UPDATE crm.customers SET customer_status = 'INACTIVE' WHERE customer_id = '00000000123'",
        "DELETE FROM crm.customers WHERE customer_id = '00000000123'",
        "CREATE TABLE crm.reader_forbidden (id INTEGER)",
        "ALTER TABLE crm.customers ADD COLUMN reader_forbidden TEXT",
    ],
    ids=["insert", "update", "delete", "create", "alter"],
)
def test_postgres_reader_default_rejects_writes(reader_dsn: str, statement: str) -> None:
    import psycopg

    with pytest.raises(psycopg.Error) as captured, psycopg.connect(reader_dsn) as connection:
        connection.execute(statement)

    assert captured.value.sqlstate in {"25006", "42501"}


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO crm.customers VALUES ('00000000999', CURRENT_DATE, 'ES', 'ACTIVE')",
        "CREATE TABLE crm.reader_forbidden (id INTEGER)",
        "CREATE TEMP TABLE reader_forbidden (id INTEGER)",
    ],
    ids=["insert", "create", "create-temp"],
)
def test_postgres_privileges_reject_writes_when_session_default_is_disabled(
    reader_dsn: str, statement: str
) -> None:
    import psycopg

    with psycopg.connect(reader_dsn, autocommit=True) as connection:
        connection.execute("SET default_transaction_read_only = off")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(statement)
