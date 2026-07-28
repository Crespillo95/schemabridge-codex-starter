from __future__ import annotations

import os

import psycopg
import pytest

from schemabridge.adapters.postgres.database_identity import (
    PsycopgDatabaseIdentityProbe,
)
from schemabridge.application.database_separation import (
    VerifySourceControlDatabaseSeparation,
)
from schemabridge.application.postgres_health import DatabaseConfigurationError

pytestmark = pytest.mark.integration

SOURCE_DSN = "postgresql://schemabridge_reader:schemabridge_reader@127.0.0.1:55433/schemabridge"
CONTROL_DSN = (
    "postgresql://schemabridge_runtime:schemabridge_runtime@127.0.0.1:55434/schemabridge_control"
)


def test_live_server_observation_proves_source_control_separation_and_alias_safety() -> None:
    source = PsycopgDatabaseIdentityProbe(
        os.environ.get("SCHEMABRIDGE_TEST_DATABASE_URL", SOURCE_DSN),
        expected_user="schemabridge_reader",
    )
    control = PsycopgDatabaseIdentityProbe(
        os.environ.get("SCHEMABRIDGE_TEST_CONTROL_DATABASE_URL", CONTROL_DSN),
        expected_user="schemabridge_runtime",
    )

    report = VerifySourceControlDatabaseSeparation(source, control).execute()

    assert report.separate is True
    assert report.source.database == "schemabridge"
    assert report.control.database == "schemabridge_control"

    with pytest.raises(DatabaseConfigurationError, match="must be separate"):
        VerifySourceControlDatabaseSeparation(source, source).execute()


def test_source_reader_role_denies_dml_ddl_and_role_escalation() -> None:
    source_dsn = os.environ.get("SCHEMABRIDGE_TEST_DATABASE_URL", SOURCE_DSN)

    with psycopg.connect(source_dsn) as connection:
        identity = connection.execute(
            """
            SELECT
                current_user,
                current_setting('transaction_read_only'),
                EXTRACT(
                    EPOCH FROM current_setting('statement_timeout')::interval
                ) * 1000
            """
        ).fetchone()
    assert identity is not None
    assert identity[0] == "schemabridge_reader"
    assert identity[1] == "on"
    assert 0 < int(identity[2]) <= 60_000

    forbidden = (
        "UPDATE crm.customers SET customer_id = customer_id WHERE false",
        "CREATE TABLE public.forbidden_worker_write (id integer)",
        "SET ROLE postgres",
    )
    for statement in forbidden:
        with (
            psycopg.connect(source_dsn) as connection,
            pytest.raises(
                (
                    psycopg.errors.ReadOnlySqlTransaction,
                    psycopg.errors.InsufficientPrivilege,
                )
            ),
        ):
            connection.execute(statement)
