"""Runtime composition of closed JSON events and bounded OpenMetrics samples."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TextIO

from schemabridge.adapters.observability.metrics import OpenMetricsRegistry
from schemabridge.adapters.observability.structured_logging import (
    LogOutcome,
    LogSeverity,
    StructuredEvent,
    StructuredEventWriter,
    UnsafeTelemetryError,
)
from schemabridge.application.ports.operational_telemetry import (
    OperationalErrorCode,
    OperationalEvent,
    OperationalOutcome,
)

_OUTCOME_METRIC = {
    "succeeded": "success",
    "started": "success",
    "failed": "error",
    "degraded": "error",
    "cancelled": "error",
    "denied": "denied",
}
_SOURCE_CAPABILITY_BY_SERVICE = {
    "catalog": "catalog",
    "profile": "profile",
    "worker": "execution",
}
_QUEUE_EVENT_BY_SERVICE = {
    "catalog": ("catalog.refresh", "catalog"),
    "profile": ("profile.run", "profile"),
    "publisher": ("registry.publication", "publication"),
    "reconciler": ("reconciliation.run", "reconciliation"),
    "worker": ("job.execution", "execution"),
}
_READINESS_SERVICES = frozenset(
    {"api", "catalog", "observer", "profile", "publisher", "reconciler", "worker"}
)
_EVENT_SERVICES = {
    "audit.verify": frozenset({"observer", "reconciler"}),
    "backup.run": frozenset({"backup"}),
    "catalog.refresh": frozenset({"catalog"}),
    "http.request": frozenset({"api", "web"}),
    "identity.authorization": frozenset({"api", "web", "worker"}),
    "job.execution": frozenset({"worker"}),
    "profile.run": frozenset({"profile"}),
    "queue.transition": frozenset({"catalog", "profile", "reconciler", "worker"}),
    "registry.publication": frozenset({"publisher"}),
    "reconciliation.run": frozenset({"reconciler"}),
    "release.policy": frozenset({"migrator"}),
    "restore.drill": frozenset({"backup"}),
    "secret.resolve": frozenset({"catalog", "profile", "publisher", "web", "worker"}),
    "service.health": frozenset(
        {
            "api",
            "backup",
            "catalog",
            "migrator",
            "observer",
            "profile",
            "publisher",
            "reconciler",
            "web",
            "worker",
        }
    ),
    "source.operation": frozenset({"catalog", "profile", "web", "worker"}),
    "telemetry.export": frozenset({"observer"}),
}


class RuntimeOperationalTelemetry:
    """One process-local telemetry sink with no network or source dependency."""

    def __init__(
        self,
        *,
        service: str,
        environment: str,
        stream: TextIO | None = None,
        registry: OpenMetricsRegistry | None = None,
    ) -> None:
        self._service = service
        self._environment = environment
        self._writer = StructuredEventWriter(stream if stream is not None else sys.stderr)
        self._registry = registry or OpenMetricsRegistry()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(service={self._service!r}, environment={self._environment!r})"
        )

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
        """Write one event and update only its predefined low-cardinality samples."""

        allowed_services = _EVENT_SERVICES.get(event)
        if allowed_services is None or self._service not in allowed_services:
            raise UnsafeTelemetryError("structured telemetry event rejected")
        log_outcome = LogOutcome(outcome)
        severity = {
            LogOutcome.STARTED: LogSeverity.INFO,
            LogOutcome.SUCCEEDED: LogSeverity.INFO,
            LogOutcome.CANCELLED: LogSeverity.WARNING,
            LogOutcome.DENIED: LogSeverity.WARNING,
            LogOutcome.DEGRADED: LogSeverity.WARNING,
            LogOutcome.FAILED: LogSeverity.ERROR,
        }[log_outcome]
        structured_event = StructuredEvent(
            occurred_at=datetime.now(UTC),
            severity=severity,
            service=self._service,
            environment=self._environment,
            event=event,
            outcome=log_outcome,
            duration_ms=duration_ms,
            correlation_id=correlation_id,
            error_code=error_code,
            counts=counts or {},
        )
        self._writer.write(structured_event)
        self._record_metrics(
            event=event,
            outcome=outcome,
            duration_ms=duration_ms,
            error_code=error_code,
            counts=structured_event.counts,
        )

    def render_openmetrics(self) -> str:
        """Render in-memory samples only; this performs no database or network I/O."""

        return self._registry.render()

    def _record_metrics(
        self,
        *,
        event: OperationalEvent,
        outcome: OperationalOutcome,
        duration_ms: int,
        error_code: OperationalErrorCode | None,
        counts: Mapping[str, int],
    ) -> None:
        metric_outcome = _OUTCOME_METRIC[outcome]
        if event == "http.request" and self._service == "api":
            self._registry.increment_counter(
                "schemabridge_http_requests_total",
                {"service": self._service, "outcome": metric_outcome},
            )
            self._registry.observe_histogram(
                "schemabridge_http_request_duration_seconds",
                {"service": self._service, "outcome": metric_outcome},
                value=duration_ms / 1_000,
            )
            if error_code == "unauthorized":
                self._registry.increment_counter(
                    "schemabridge_http_authorization_denials_total",
                    {"service": self._service},
                )
        capability = (
            _SOURCE_CAPABILITY_BY_SERVICE.get(self._service)
            if event == "source.operation"
            else None
        )
        if capability is not None:
            source_outcome = "timeout" if error_code == "source_timeout" else metric_outcome
            self._registry.increment_counter(
                "schemabridge_source_operations_total",
                {"capability": capability, "outcome": source_outcome},
            )
        queue_event = _QUEUE_EVENT_BY_SERVICE.get(self._service)
        if queue_event is not None and event == queue_event[0]:
            queue = queue_event[1]
            transitions = (
                ("retry_scheduled", counts.get("retries_scheduled", 0)),
                ("dead_lettered", counts.get("dead_letters", 0)),
                ("authorization_stale", int(error_code == "stale_authorization")),
                ("cancelled", int(outcome == "cancelled")),
            )
            for transition, amount in transitions:
                if amount:
                    self._registry.increment_counter(
                        "schemabridge_job_transitions_total",
                        {"queue": queue, "transition": transition},
                        amount=amount,
                    )
        if event == "service.health" and self._service in _READINESS_SERVICES:
            self._registry.set_gauge(
                "schemabridge_process_ready",
                {"service": self._service},
                value=1 if outcome == "succeeded" else 0,
            )
