"""Schema-versioned JSON events with a closed, non-sensitive field contract."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import NoReturn, TextIO

from schemabridge.application.ports.operational_telemetry import (
    OperationalErrorCode,
    OperationalEvent,
    OperationalOutcome,
)

TELEMETRY_SCHEMA_VERSION = "schemabridge.telemetry.v1"
MAX_DURATION_MS = 86_400_000
MAX_OPERATIONAL_COUNT = 1_000_000_000_000
MAX_STRUCTURED_EVENT_BYTES = 2_048

_PUBLIC_TOKEN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_CORRELATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_STRUCTURED_EVENT_RECORD_ATTRIBUTE = "_schemabridge_structured_event"
_LOGGING_CONFIGURATION_LOCK = threading.RLock()
_RUNTIME_ENVIRONMENTS = frozenset(
    {
        "development",
        "hosted-demo",
        "production",
        "staging",
    }
)
_LOG_LEVELS = MappingProxyType(
    {
        "CRITICAL": logging.CRITICAL,
        "DEBUG": logging.DEBUG,
        "ERROR": logging.ERROR,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
    }
)

SERVICES = frozenset(
    {
        "api",
        "backup",
        "catalog",
        "migrator",
        "observer",
        "profile",
        "reconciler",
        "web",
        "worker",
    }
)

EVENTS = frozenset(
    {
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
        "runtime.log",
        "secret.resolve",
        "service.health",
        "source.operation",
        "telemetry.export",
    }
)

ERROR_CODES = frozenset(
    {
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
    }
)

OPERATIONAL_COUNT_FIELDS = frozenset(
    {
        "authorization_denials",
        "dead_letters",
        "jobs_completed",
        "jobs_failed",
        "leases_reclaimed",
        "records_processed",
        "requests_completed",
        "retries_scheduled",
        "source_timeouts",
        "telemetry_dropped",
    }
)

PUBLIC_LOG_FIELD_NAMES = (
    frozenset(
        {
            "schema_version",
            "timestamp",
            "severity",
            "service",
            "environment",
            "event",
            "outcome",
            "duration_ms",
            "correlation_id",
            "error_code",
        }
    )
    | OPERATIONAL_COUNT_FIELDS
)


class LogSeverity(StrEnum):
    """Closed severity vocabulary used by every runtime process."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class LogOutcome(StrEnum):
    """Closed operation outcomes used for logs and derived metrics."""

    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    DEGRADED = "degraded"
    CANCELLED = "cancelled"


class UnsafeTelemetryError(ValueError):
    """A prospective event did not satisfy the public telemetry contract."""


def _reject() -> NoReturn:
    raise UnsafeTelemetryError("structured telemetry event rejected")


@dataclass(frozen=True, slots=True)
class StructuredEvent:
    """One public event whose constructor rejects arbitrary or sensitive values."""

    occurred_at: datetime
    severity: LogSeverity
    service: str
    environment: str
    event: str
    outcome: LogOutcome
    duration_ms: int
    correlation_id: str | None = None
    error_code: str | None = None
    counts: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.occurred_at) is not datetime:
            _reject()
        try:
            offset = self.occurred_at.utcoffset()
        except Exception:
            _reject()
        if self.occurred_at.tzinfo is None or offset is None:
            _reject()
        try:
            normalized_occurred_at = self.occurred_at.astimezone(UTC)
        except Exception:
            _reject()
        object.__setattr__(self, "occurred_at", normalized_occurred_at)
        if not isinstance(self.severity, LogSeverity):
            _reject()
        if type(self.service) is not str or self.service not in SERVICES:
            _reject()
        if type(self.environment) is not str or _PUBLIC_TOKEN.fullmatch(self.environment) is None:
            _reject()
        if type(self.event) is not str or self.event not in EVENTS:
            _reject()
        if not isinstance(self.outcome, LogOutcome):
            _reject()
        if (
            isinstance(self.duration_ms, bool)
            or not isinstance(self.duration_ms, int)
            or not 0 <= self.duration_ms <= MAX_DURATION_MS
        ):
            _reject()
        if self.correlation_id is not None and (
            type(self.correlation_id) is not str
            or _CORRELATION_ID.fullmatch(self.correlation_id) is None
        ):
            _reject()
        if self.error_code is not None and type(self.error_code) is not str:
            _reject()
        if not isinstance(self.counts, Mapping):
            _reject()

        failure_outcomes = {
            LogOutcome.FAILED,
            LogOutcome.DENIED,
            LogOutcome.DEGRADED,
            LogOutcome.CANCELLED,
        }
        if self.outcome in failure_outcomes:
            if self.error_code not in ERROR_CODES:
                _reject()
        elif self.error_code is not None:
            _reject()

        public_counts: dict[str, int] = {}
        try:
            for index, (name, value) in enumerate(self.counts.items()):
                if index >= len(OPERATIONAL_COUNT_FIELDS):
                    _reject()
                if type(name) is not str or name not in OPERATIONAL_COUNT_FIELDS:
                    _reject()
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or not 0 <= value <= MAX_OPERATIONAL_COUNT
                ):
                    _reject()
                public_counts[name] = value
        except UnsafeTelemetryError:
            raise
        except Exception:
            _reject()
        object.__setattr__(self, "counts", MappingProxyType(public_counts))


def render_structured_event(event: StructuredEvent) -> str:
    """Render one deterministic JSON object without a trailing newline."""

    if type(event) is not StructuredEvent:
        _reject()
    timestamp = (
        event.occurred_at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )
    payload: dict[str, object] = {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "timestamp": timestamp,
        "severity": event.severity.value,
        "service": event.service,
        "environment": event.environment,
        "event": event.event,
        "outcome": event.outcome.value,
        "duration_ms": event.duration_ms,
    }
    if event.correlation_id is not None:
        payload["correlation_id"] = event.correlation_id
    if event.error_code is not None:
        payload["error_code"] = event.error_code
    for name in sorted(event.counts):
        payload[name] = event.counts[name]
    rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(rendered.encode("utf-8")) > MAX_STRUCTURED_EVENT_BYTES:
        _reject()
    return rendered


class StructuredEventWriter:
    """Serialize complete events atomically to a text stream."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def write(self, event: StructuredEvent) -> None:
        """Write exactly one event and one line terminator."""

        line = render_structured_event(event)
        with self._lock:
            self._stream.write(f"{line}\n")
            self._stream.flush()


def resolve_runtime_environment(value: str | None = None) -> str:
    """Return one closed public deployment label, never an arbitrary environment value."""

    candidate = value
    if candidate is None:
        candidate = os.environ.get("SCHEMABRIDGE_ENVIRONMENT", "development")
    return candidate if type(candidate) is str and candidate in _RUNTIME_ENVIRONMENTS else "unknown"


def resolve_runtime_log_level(value: str | int | None = None) -> int:
    """Resolve the closed process log threshold with a safe INFO fallback."""

    candidate: str | int = (
        os.environ.get("SCHEMABRIDGE_LOG_LEVEL", "INFO") if value is None else value
    )
    if type(candidate) is int:
        return candidate if candidate in _LOG_LEVELS.values() else logging.INFO
    if type(candidate) is str:
        return _LOG_LEVELS.get(candidate.upper(), logging.INFO)
    return logging.INFO


def _fallback_event(
    *,
    service: str,
    environment: str,
    level: int,
) -> StructuredEvent:
    if level >= logging.ERROR:
        severity = LogSeverity.CRITICAL if level >= logging.CRITICAL else LogSeverity.ERROR
        outcome = LogOutcome.FAILED
        error_code = "internal_failure"
    elif level >= logging.WARNING:
        severity = LogSeverity.WARNING
        outcome = LogOutcome.DEGRADED
        error_code = "internal_failure"
    elif level >= logging.INFO:
        severity = LogSeverity.INFO
        outcome = LogOutcome.SUCCEEDED
        error_code = None
    else:
        severity = LogSeverity.DEBUG
        outcome = LogOutcome.STARTED
        error_code = None
    return StructuredEvent(
        occurred_at=datetime.now(UTC),
        severity=severity,
        service=service,
        environment=environment,
        event="runtime.log",
        outcome=outcome,
        duration_ms=0,
        error_code=error_code,
    )


class StructuredEventHandler(logging.Handler):
    """Convert standard-library records into the closed public event schema.

    A record's message, arguments, exception, stack, pathname, function, and arbitrary
    ``extra`` values are intentionally never formatted or serialized.
    """

    def __init__(
        self,
        *,
        service: str,
        environment: str,
        stream: TextIO | None = None,
    ) -> None:
        super().__init__(level=logging.NOTSET)
        if (
            type(service) is not str
            or service not in SERVICES
            or type(environment) is not str
            or _PUBLIC_TOKEN.fullmatch(environment) is None
        ):
            _reject()
        self._service = service
        self._environment = environment
        self._writer = StructuredEventWriter(stream if stream is not None else sys.stderr)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(service={self._service!r}, environment={self._environment!r})"
        )

    def emit(self, record: logging.LogRecord) -> None:
        """Write one bounded line without invoking ``LogRecord.getMessage``."""

        try:
            candidate = record.__dict__.get(_STRUCTURED_EVENT_RECORD_ATTRIBUTE)
            if type(candidate) is StructuredEvent:
                event = candidate
                if event.service != self._service or event.environment != self._environment:
                    event = _fallback_event(
                        service=self._service,
                        environment=self._environment,
                        level=record.levelno,
                    )
            else:
                event = _fallback_event(
                    service=self._service,
                    environment=self._environment,
                    level=record.levelno,
                )
            self._writer.write(event)
        except Exception:
            # Logging must not turn an invalid record or broken sink into a traceback channel.
            return

    def handleError(self, record: logging.LogRecord) -> None:
        """Suppress logging's default stderr traceback for handler failures."""

        del record


@dataclass(frozen=True, slots=True)
class _LoggerState:
    logger: logging.Logger = field(repr=False)
    handlers: tuple[logging.Handler, ...] = field(repr=False)
    level: int
    propagate: bool
    disabled: bool


class StructuredLoggingSession:
    """Own one process-wide structured handler and restore prior state on exit."""

    def __init__(
        self,
        *,
        service: str,
        environment: str,
        handler: StructuredEventHandler,
        root_handlers: tuple[logging.Handler, ...],
        root_level: int,
        logger_states: tuple[_LoggerState, ...],
        stream: TextIO | None,
    ) -> None:
        self._service = service
        self._environment = environment
        self._handler = handler
        self._root_handlers = root_handlers
        self._root_level = root_level
        self._logger_states = logger_states
        self._stream = stream
        self._closed = False

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(service={self._service!r}, environment={self._environment!r})"
        )

    def set_level(self, level: str | int) -> None:
        """Update the process threshold using only the closed level vocabulary."""

        with _LOGGING_CONFIGURATION_LOCK:
            logging.getLogger().setLevel(resolve_runtime_log_level(level))

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
        """Emit one closed event through this session's configured handler."""

        emit_structured_log(
            logging.getLogger("schemabridge.runtime"),
            service=self._service,
            environment=self._environment,
            event=event,
            outcome=outcome,
            duration_ms=duration_ms,
            correlation_id=correlation_id,
            error_code=error_code,
            counts=counts,
        )

    def is_installed_for(
        self,
        *,
        service: str,
        environment: str,
        stream: TextIO | None,
    ) -> bool:
        """Return whether this exact session still exclusively owns the root sink."""

        with _LOGGING_CONFIGURATION_LOCK:
            return (
                not self._closed
                and self._service == service
                and self._environment == environment
                and self._stream is stream
                and tuple(logging.getLogger().handlers) == (self._handler,)
            )

    def close(self) -> None:
        """Restore the embedding process's logging state idempotently."""

        with _LOGGING_CONFIGURATION_LOCK:
            if self._closed:
                return
            root = logging.getLogger()
            if self._handler not in root.handlers:
                self._handler.close()
                self._closed = True
                return
            for handler in tuple(root.handlers):
                root.removeHandler(handler)
            for handler in self._root_handlers:
                root.addHandler(handler)
            root.setLevel(self._root_level)
            for state in self._logger_states:
                for handler in tuple(state.logger.handlers):
                    state.logger.removeHandler(handler)
                for handler in state.handlers:
                    state.logger.addHandler(handler)
                state.logger.setLevel(state.level)
                state.logger.propagate = state.propagate
                state.logger.disabled = state.disabled
            self._handler.close()
            self._closed = True

    def __enter__(self) -> StructuredLoggingSession:
        return self

    def __exit__(self, *args: object) -> None:
        del args
        self.close()


def configure_structured_logging(
    *,
    service: str,
    environment: str | None = None,
    level: str | int | None = None,
    stream: TextIO | None = None,
) -> StructuredLoggingSession:
    """Install the sole process handler and neutralize Uvicorn's text handlers."""

    with _LOGGING_CONFIGURATION_LOCK:
        return _configure_structured_logging_unlocked(
            service=service,
            environment=environment,
            level=level,
            stream=stream,
        )


def _configure_structured_logging_unlocked(
    *,
    service: str,
    environment: str | None,
    level: str | int | None,
    stream: TextIO | None,
) -> StructuredLoggingSession:
    resolved_environment = resolve_runtime_environment(environment)
    resolved_level = resolve_runtime_log_level(level)
    handler = StructuredEventHandler(
        service=service,
        environment=resolved_environment,
        stream=stream,
    )
    root = logging.getLogger()
    root_handlers = tuple(root.handlers)
    root_level = root.level
    for existing in root_handlers:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(resolved_level)

    framework_logger_names = {
        "streamlit",
        "tornado",
        "tornado.access",
        "tornado.application",
        "tornado.general",
        "uvicorn",
        "uvicorn.access",
        "uvicorn.error",
    }
    framework_logger_names.update(
        name
        for name, candidate in logging.Logger.manager.loggerDict.items()
        if isinstance(candidate, logging.Logger)
        and (
            name.startswith("streamlit.")
            or name.startswith("tornado.")
            or name.startswith("uvicorn.")
        )
    )
    logger_states: list[_LoggerState] = []
    for name in sorted(framework_logger_names):
        candidate = logging.getLogger(name)
        logger_states.append(
            _LoggerState(
                logger=candidate,
                handlers=tuple(candidate.handlers),
                level=candidate.level,
                propagate=candidate.propagate,
                disabled=candidate.disabled,
            )
        )
        for existing in tuple(candidate.handlers):
            candidate.removeHandler(existing)
        candidate.setLevel(logging.NOTSET)
        candidate.propagate = True
        candidate.disabled = name == "uvicorn.access"

    return StructuredLoggingSession(
        service=service,
        environment=resolved_environment,
        handler=handler,
        root_handlers=root_handlers,
        root_level=root_level,
        logger_states=tuple(logger_states),
        stream=stream,
    )


_PERSISTENT_PROCESS_LOGGING_SESSION: StructuredLoggingSession | None = None


def ensure_structured_logging(
    *,
    service: str,
    environment: str | None = None,
    level: str | int | None = None,
    stream: TextIO | None = None,
) -> StructuredLoggingSession:
    """Install or reuse one process session for rerun-based entrypoints."""

    global _PERSISTENT_PROCESS_LOGGING_SESSION

    resolved_environment = resolve_runtime_environment(environment)
    with _LOGGING_CONFIGURATION_LOCK:
        existing = _PERSISTENT_PROCESS_LOGGING_SESSION
        if existing is not None and existing.is_installed_for(
            service=service,
            environment=resolved_environment,
            stream=stream,
        ):
            existing.set_level(resolve_runtime_log_level(level))
            return existing
        if existing is not None:
            existing.close()
        session = _configure_structured_logging_unlocked(
            service=service,
            environment=resolved_environment,
            level=level,
            stream=stream,
        )
        _PERSISTENT_PROCESS_LOGGING_SESSION = session
        return session


def emit_structured_log(
    logger: logging.Logger,
    *,
    service: str,
    event: str,
    outcome: LogOutcome | str,
    duration_ms: int,
    correlation_id: str | None = None,
    error_code: str | None = None,
    counts: Mapping[str, int] | None = None,
    environment: str | None = None,
) -> None:
    """Submit one typed event through stdlib logging without a payload-bearing message."""

    try:
        resolved_outcome = outcome if isinstance(outcome, LogOutcome) else LogOutcome(outcome)
    except Exception:
        _reject()
    severity = {
        LogOutcome.STARTED: LogSeverity.INFO,
        LogOutcome.SUCCEEDED: LogSeverity.INFO,
        LogOutcome.CANCELLED: LogSeverity.WARNING,
        LogOutcome.DENIED: LogSeverity.WARNING,
        LogOutcome.DEGRADED: LogSeverity.WARNING,
        LogOutcome.FAILED: LogSeverity.ERROR,
    }[resolved_outcome]
    structured_event = StructuredEvent(
        occurred_at=datetime.now(UTC),
        severity=severity,
        service=service,
        environment=resolve_runtime_environment(environment),
        event=event,
        outcome=resolved_outcome,
        duration_ms=duration_ms,
        correlation_id=correlation_id,
        error_code=error_code,
        counts=counts or {},
    )
    message = f"{event} outcome={resolved_outcome.value}"
    if error_code is not None:
        message = f"{message} error_code={error_code}"
    logger.log(
        {
            LogSeverity.DEBUG: logging.DEBUG,
            LogSeverity.INFO: logging.INFO,
            LogSeverity.WARNING: logging.WARNING,
            LogSeverity.ERROR: logging.ERROR,
            LogSeverity.CRITICAL: logging.CRITICAL,
        }[severity],
        message,
        extra={_STRUCTURED_EVENT_RECORD_ATTRIBUTE: structured_event},
    )
