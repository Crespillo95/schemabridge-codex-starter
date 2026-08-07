"""Application contracts for explicit control-plane schema migrations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ControlPlaneMigrationErrorCode(StrEnum):
    """Stable, operator-safe migration failure categories."""

    MIGRATION_SET_INVALID = "control_plane_migration_set_invalid"
    DATABASE_UNAVAILABLE = "control_plane_database_unavailable"
    LOCK_UNAVAILABLE = "control_plane_migration_lock_unavailable"
    INCOMPATIBLE_SCHEMA = "control_plane_schema_incompatible"
    SCHEMA_AHEAD = "control_plane_schema_ahead"
    HISTORY_INVALID = "control_plane_migration_history_invalid"
    CHECKSUM_DRIFT = "control_plane_migration_checksum_drift"
    SCHEMA_NOT_CURRENT = "control_plane_schema_not_current"
    APPLY_FAILED = "control_plane_migration_apply_failed"


class ControlPlaneMigrationError(RuntimeError):
    """A sanitized migration boundary failure."""

    def __init__(self, code: ControlPlaneMigrationErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ControlPlaneMigrationDefinition:
    """One immutable, checksum-pinned migration known to this release."""

    version: int
    name: str
    checksum: str


@dataclass(frozen=True, slots=True)
class ControlPlaneMigrationHistoryRecord:
    """One migration identity read from the control database."""

    version: int
    name: str
    checksum: str


@dataclass(frozen=True, slots=True)
class ControlPlaneMigrationInspection:
    """Exact comparison between database history and the local migration set."""

    expected_version: int
    applied: tuple[ControlPlaneMigrationHistoryRecord, ...]
    pending: tuple[ControlPlaneMigrationDefinition, ...]

    @property
    def current_version(self) -> int:
        """Return zero for a pristine database, otherwise the last applied version."""

        return self.applied[-1].version if self.applied else 0

    @property
    def is_current(self) -> bool:
        """Whether the database exactly matches this release's migration set."""

        return self.current_version == self.expected_version and not self.pending


@dataclass(frozen=True, slots=True)
class ControlPlaneMigrationResult:
    """Outcome of an explicit migration attempt."""

    inspection: ControlPlaneMigrationInspection
    applied_versions: tuple[int, ...]

    @property
    def already_current(self) -> bool:
        """Whether replay required no migration statements."""

        return not self.applied_versions


class ControlPlaneMigrationPort(Protocol):
    """Inspect and explicitly advance the isolated control-plane schema."""

    def inspect(self) -> ControlPlaneMigrationInspection:
        """Inspect without executing DDL or changing migration history."""

    def require_current(self) -> ControlPlaneMigrationInspection:
        """Fail unless the database exactly matches this release."""

    def migrate(self) -> ControlPlaneMigrationResult:
        """Apply every pending migration atomically under an advisory lock."""
