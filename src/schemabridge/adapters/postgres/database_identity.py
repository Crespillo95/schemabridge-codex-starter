"""Read-only PostgreSQL endpoint identity probe."""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg

from schemabridge.application.database_separation import (
    DatabaseEndpointIdentity,
    DatabaseIdentityProbeError,
)


@dataclass(frozen=True, slots=True)
class PsycopgDatabaseIdentityProbe:
    """Observe server address/database without trusting a configured hostname."""

    dsn: str = field(repr=False)
    expected_user: str
    connect_timeout_seconds: int = 5
    statement_timeout_ms: int = 5_000

    def __post_init__(self) -> None:
        if (
            not self.dsn.strip()
            or not self.expected_user.strip()
            or not 1 <= self.connect_timeout_seconds <= 30
            or not 100 <= self.statement_timeout_ms <= 60_000
        ):
            raise ValueError("database identity probe configuration is invalid")

    def inspect(self) -> DatabaseEndpointIdentity:
        try:
            with psycopg.connect(
                self.dsn,
                connect_timeout=self.connect_timeout_seconds,
                application_name="schemabridge-database-separation-check",
            ) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (f"{self.statement_timeout_ms}ms",),
                )
                row = connection.execute(
                    """
                    SELECT
                        COALESCE(inet_server_addr()::text, 'local_socket'),
                        COALESCE(inet_server_port(), 0),
                        current_database(),
                        current_user
                    """
                ).fetchone()
            if (
                row is None
                or not isinstance(row[0], str)
                or isinstance(row[1], bool)
                or not isinstance(row[1], int)
                or not isinstance(row[2], str)
                or not isinstance(row[3], str)
                or row[3] != self.expected_user
            ):
                raise ValueError("database identity response is invalid")
            return DatabaseEndpointIdentity(
                server_address=row[0],
                server_port=row[1],
                database=row[2],
                user=row[3],
            )
        except (psycopg.Error, TypeError, ValueError) as error:
            raise DatabaseIdentityProbeError("database identity probe failed") from error
