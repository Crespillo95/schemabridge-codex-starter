"""Framework-free contract for one composed process logging session."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol, TypeAlias

from schemabridge.application.ports.operational_telemetry import (
    OperationalErrorCode,
    OperationalEvent,
    OperationalOutcome,
)

RuntimeLoggingService: TypeAlias = Literal[
    "api",
    "backup",
    "catalog",
    "migrator",
    "observer",
    "profile",
    "reconciler",
    "web",
    "worker",
]


class RuntimeLoggingSessionPort(Protocol):
    """Emit closed events and own the process logging configuration."""

    def emit(
        self,
        *,
        event: OperationalEvent,
        outcome: OperationalOutcome,
        duration_ms: int,
        correlation_id: str | None = None,
        error_code: OperationalErrorCode | None = None,
        counts: Mapping[str, int] | None = None,
    ) -> None:
        """Emit one sanitized event through the configured process sink."""

    def set_level(self, level: str | int) -> None:
        """Set the process threshold through the adapter's closed level policy."""

    def close(self) -> None:
        """Restore the prior process logging state idempotently."""
