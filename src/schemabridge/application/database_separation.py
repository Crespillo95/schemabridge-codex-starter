"""Read-only proof that source and control connections reach different databases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from schemabridge.application.postgres_health import DatabaseConfigurationError


class DatabaseIdentityProbeError(RuntimeError):
    """Sanitized failure from a read-only endpoint identity probe."""


@dataclass(frozen=True, slots=True)
class DatabaseEndpointIdentity:
    """Server-observed database coordinates; credentials and host aliases are excluded."""

    server_address: str
    server_port: int
    database: str
    user: str

    @property
    def database_key(self) -> tuple[str, int, str]:
        return self.server_address, self.server_port, self.database


class DatabaseIdentityProbePort(Protocol):
    def inspect(self) -> DatabaseEndpointIdentity:
        """Return coordinates observed by PostgreSQL inside a read-only transaction."""


@dataclass(frozen=True, slots=True)
class DatabaseSeparationReport:
    source: DatabaseEndpointIdentity
    control: DatabaseEndpointIdentity
    separate: bool


@dataclass(frozen=True, slots=True)
class VerifySourceControlDatabaseSeparation:
    """Fail closed when DNS aliases resolve to the same physical database."""

    source: DatabaseIdentityProbePort
    control: DatabaseIdentityProbePort

    def execute(self) -> DatabaseSeparationReport:
        try:
            source = self.source.inspect()
            control = self.control.inspect()
        except DatabaseIdentityProbeError as error:
            raise DatabaseConfigurationError(
                "source/control database separation could not be verified"
            ) from error
        if source.database_key == control.database_key:
            raise DatabaseConfigurationError(
                "control database must be separate from every source database"
            )
        return DatabaseSeparationReport(
            source=source,
            control=control,
            separate=True,
        )
