"""Unit tests for the database readiness use case and CLI boundary."""

from dataclasses import dataclass

from typer.testing import CliRunner

from schemabridge.application.postgres_health import (
    CheckDatabaseReadiness,
    DatabaseHealthDetails,
)
from schemabridge.entrypoints.cli.main import app

runner = CliRunner()


@dataclass(frozen=True)
class FakeDatabaseHealthProbe:
    details: DatabaseHealthDetails

    def inspect(self) -> DatabaseHealthDetails:
        return self.details


def healthy_details() -> DatabaseHealthDetails:
    return DatabaseHealthDetails(
        backend="postgresql",
        server_version="16.13",
        database="schemabridge",
        user="schemabridge_reader",
        default_transaction_read_only=True,
        transaction_read_only=True,
        statement_timeout_ms=5_000,
    )


def test_database_readiness_accepts_expected_reader_policy() -> None:
    report = CheckDatabaseReadiness(
        probe=FakeDatabaseHealthProbe(healthy_details()),
        expected_user="schemabridge_reader",
        expected_statement_timeout_ms=5_000,
    ).execute()

    assert report.is_ready is True
    assert report.findings == ()
    assert report.as_dict()["healthy"] is True


def test_database_readiness_reports_every_policy_mismatch() -> None:
    unsafe = DatabaseHealthDetails(
        backend="other",
        server_version="15.9",
        database="schemabridge",
        user="postgres",
        default_transaction_read_only=False,
        transaction_read_only=False,
        statement_timeout_ms=0,
    )

    report = CheckDatabaseReadiness(
        probe=FakeDatabaseHealthProbe(unsafe),
        expected_user="schemabridge_reader",
        expected_statement_timeout_ms=5_000,
    ).execute()

    assert report.is_ready is False
    assert report.findings == (
        "unexpected_database_backend",
        "unexpected_postgres_major_version",
        "unexpected_database_user",
        "reader_default_is_not_read_only",
        "health_transaction_is_not_read_only",
        "unexpected_statement_timeout",
    )


def test_postgres_health_cli_requires_database_url() -> None:
    result = runner.invoke(app, ["postgres-health", "--json"], env={"DATABASE_URL": ""})

    assert result.exit_code == 1
    assert '"healthy": false' in result.stdout
    assert "DATABASE_URL is required" in result.stdout
    assert "postgresql://" not in result.stdout
