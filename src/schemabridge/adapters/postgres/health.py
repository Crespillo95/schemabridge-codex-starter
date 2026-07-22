"""Psycopg implementation of the database health port."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import psycopg

from schemabridge.application.postgres_health import (
    DatabaseHealthDetails,
    DatabaseUnavailableError,
)

_HEALTH_QUERY = """
SELECT
    current_database(),
    current_user,
    current_setting('server_version'),
    current_setting('default_transaction_read_only')::BOOLEAN,
    current_setting('transaction_read_only')::BOOLEAN,
    (SELECT setting::INTEGER FROM pg_settings WHERE name = 'statement_timeout')
"""


@dataclass(frozen=True, slots=True)
class PsycopgDatabaseHealthProbe:
    """Inspect PostgreSQL through a bounded, explicitly read-only transaction."""

    dsn: str
    connect_timeout_seconds: int = 3

    def inspect(self) -> DatabaseHealthDetails:
        """Return connection facts while redacting connection details on failure."""

        try:
            with psycopg.connect(
                self.dsn,
                connect_timeout=self.connect_timeout_seconds,
                autocommit=False,
            ) as connection:
                connection.read_only = True
                with connection.cursor() as cursor:
                    cursor.execute(_HEALTH_QUERY)
                    row = cursor.fetchone()
                connection.rollback()
        except psycopg.Error as error:
            raise DatabaseUnavailableError("PostgreSQL readiness check failed") from error

        if row is None:
            raise DatabaseUnavailableError("PostgreSQL readiness check returned no result")

        database, user, version, default_read_only, transaction_read_only, timeout_ms = cast(
            tuple[str, str, str, bool, bool, int], row
        )
        return DatabaseHealthDetails(
            backend="postgresql",
            server_version=version,
            database=database,
            user=user,
            default_transaction_read_only=default_read_only,
            transaction_read_only=transaction_read_only,
            statement_timeout_ms=timeout_ms,
        )
