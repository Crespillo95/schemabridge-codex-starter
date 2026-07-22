"""Application contract and use case for database readiness."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class DatabaseHealthError(RuntimeError):
    """Base error for a database readiness check."""


class DatabaseConfigurationError(DatabaseHealthError):
    """Required database configuration is missing or invalid."""


class DatabaseUnavailableError(DatabaseHealthError):
    """The configured database could not complete a readiness check."""


@dataclass(frozen=True, slots=True)
class DatabaseHealthDetails:
    """Vendor-neutral facts returned by a database health adapter."""

    backend: str
    server_version: str
    database: str
    user: str
    default_transaction_read_only: bool
    transaction_read_only: bool
    statement_timeout_ms: int


class DatabaseHealthPort(Protocol):
    """Inspect a database connection without mutating external state."""

    def inspect(self) -> DatabaseHealthDetails:
        """Return bounded connection and safety facts."""


@dataclass(frozen=True, slots=True)
class DatabaseReadinessReport:
    """Readiness result with explicit policy findings."""

    details: DatabaseHealthDetails
    findings: tuple[str, ...]

    @property
    def is_ready(self) -> bool:
        """Return whether every readiness policy passed."""

        return not self.findings

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation."""

        return {
            "healthy": self.is_ready,
            "backend": self.details.backend,
            "server_version": self.details.server_version,
            "database": self.details.database,
            "user": self.details.user,
            "default_transaction_read_only": self.details.default_transaction_read_only,
            "transaction_read_only": self.details.transaction_read_only,
            "statement_timeout_ms": self.details.statement_timeout_ms,
            "findings": list(self.findings),
        }


@dataclass(frozen=True, slots=True)
class CheckDatabaseReadiness:
    """Validate adapter facts against the expected reader policy."""

    probe: DatabaseHealthPort
    expected_user: str
    expected_statement_timeout_ms: int
    expected_server_major: int = 16

    def execute(self) -> DatabaseReadinessReport:
        """Inspect the database and return every readiness finding."""

        details = self.probe.inspect()
        findings: list[str] = []

        if details.backend != "postgresql":
            findings.append("unexpected_database_backend")
        if details.server_version.partition(".")[0] != str(self.expected_server_major):
            findings.append("unexpected_postgres_major_version")
        if details.user != self.expected_user:
            findings.append("unexpected_database_user")
        if not details.default_transaction_read_only:
            findings.append("reader_default_is_not_read_only")
        if not details.transaction_read_only:
            findings.append("health_transaction_is_not_read_only")
        if details.statement_timeout_ms != self.expected_statement_timeout_ms:
            findings.append("unexpected_statement_timeout")

        return DatabaseReadinessReport(details=details, findings=tuple(findings))
