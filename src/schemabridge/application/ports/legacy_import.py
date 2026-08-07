"""Ports for offline legacy-state inspection and controlled import."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from schemabridge.domain.legacy_import import (
    LegacyImportApproval,
    LegacyImportPlan,
    LegacyImportReservation,
    LegacyImportResult,
)


class LegacyImportPortErrorCode(StrEnum):
    SOURCE_UNAVAILABLE = "legacy_import_source_unavailable"
    SOURCE_NOT_OFFLINE = "legacy_import_source_not_offline"
    SOURCE_TOO_LARGE = "legacy_import_source_too_large"
    SOURCE_CORRUPT = "legacy_import_source_corrupt"
    SOURCE_SCHEMA_INVALID = "legacy_import_source_schema_invalid"
    SOURCE_CHANGED = "legacy_import_source_changed"
    STORE_CONFLICT = "legacy_import_store_conflict"
    STORE_UNAVAILABLE = "legacy_import_store_unavailable"


class LegacyImportPortError(RuntimeError):
    """Sanitized source/store failure."""

    def __init__(self, code: LegacyImportPortErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class LegacySqliteRow:
    source_id_digest: str
    values: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class LegacySqliteTable:
    name: str
    columns: tuple[str, ...]
    rows: tuple[LegacySqliteRow, ...]


@dataclass(frozen=True, slots=True)
class LegacySqliteSnapshot:
    source_fingerprint: str
    source_schema_fingerprint: str
    tables: tuple[LegacySqliteTable, ...]


class LegacyControlPlaneSourcePort(Protocol):
    def inspect(self) -> LegacySqliteSnapshot:
        """Read one immutable offline source without changing it."""


class LegacyControlPlaneImportStorePort(Protocol):
    def reserve_dry_run(
        self,
        plan: LegacyImportPlan,
        *,
        recorded_at: datetime,
    ) -> LegacyImportReservation:
        """Record only checksums/counts for an exact dry-run."""

    def load_reservation(self, import_id: str) -> LegacyImportReservation | None:
        """Load prior dry-run/completion state for replay."""

    def apply(
        self,
        plan: LegacyImportPlan,
        approval: LegacyImportApproval,
        *,
        completed_at: datetime,
    ) -> LegacyImportResult:
        """Atomically import and quarantine the exact approved plan."""
