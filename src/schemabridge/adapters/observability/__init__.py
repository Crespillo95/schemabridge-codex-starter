"""Closed, sanitized operational telemetry adapters."""

from schemabridge.adapters.observability.contracts import (
    ObservabilityBundleReport,
    ObservabilityContractError,
    validate_observability_bundle,
)
from schemabridge.adapters.observability.metrics import (
    OPERATIONAL_METRICS,
    MetricContractError,
    MetricDefinition,
    MetricKind,
    MetricLabel,
    OpenMetricsRegistry,
)
from schemabridge.adapters.observability.structured_logging import (
    LogOutcome,
    LogSeverity,
    StructuredEvent,
    StructuredEventHandler,
    StructuredEventWriter,
    StructuredLoggingSession,
    UnsafeTelemetryError,
    configure_structured_logging,
    emit_structured_log,
    ensure_structured_logging,
    render_structured_event,
    resolve_runtime_environment,
    resolve_runtime_log_level,
)

__all__ = [
    "OPERATIONAL_METRICS",
    "LogOutcome",
    "LogSeverity",
    "MetricContractError",
    "MetricDefinition",
    "MetricKind",
    "MetricLabel",
    "ObservabilityBundleReport",
    "ObservabilityContractError",
    "OpenMetricsRegistry",
    "StructuredEvent",
    "StructuredEventHandler",
    "StructuredEventWriter",
    "StructuredLoggingSession",
    "UnsafeTelemetryError",
    "configure_structured_logging",
    "emit_structured_log",
    "ensure_structured_logging",
    "render_structured_event",
    "resolve_runtime_environment",
    "resolve_runtime_log_level",
    "validate_observability_bundle",
]
