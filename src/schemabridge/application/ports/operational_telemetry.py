"""Small, framework-free port for sanitized runtime telemetry."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum, auto
from typing import Literal, Protocol, TypeAlias

OperationalEvent: TypeAlias = Literal[
    "audit.verify",
    "backup.run",
    "catalog.refresh",
    "http.request",
    "identity.authorization",
    "job.execution",
    "profile.run",
    "queue.transition",
    "reconciliation.run",
    "release.policy",
    "restore.drill",
    "secret.resolve",
    "service.health",
    "source.operation",
    "telemetry.export",
]
OperationalOutcome: TypeAlias = Literal[
    "started",
    "succeeded",
    "failed",
    "denied",
    "degraded",
    "cancelled",
]
OperationalErrorCode: TypeAlias = Literal[
    "audit_chain_invalid",
    "backup_stale",
    "backup_verification_failed",
    "cross_capability_denied",
    "identity_invalid",
    "internal_failure",
    "lease_lost",
    "operation_cancelled",
    "queue_unavailable",
    "release_policy_failed",
    "request_rejected",
    "route_denied",
    "secret_invalid",
    "secret_unavailable",
    "source_timeout",
    "source_unavailable",
    "stale_authorization",
    "telemetry_export_failed",
    "tls_failure",
    "unauthorized",
]


class OperationalResourceAccessCause(Enum):
    """Closed, non-serializable cause for publicly collapsed resource failures."""

    DENIED = auto()
    NOT_FOUND = auto()


class OperationalTelemetryPort(Protocol):
    """Record only closed operational events and expose bounded process metrics."""

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
        """Record one sanitized event without accepting arbitrary context."""

    def render_openmetrics(self) -> str:
        """Return the current bounded OpenMetrics document without external I/O."""
