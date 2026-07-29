"""Independent, schema-checking, gracefully stoppable semantic reconciler."""

from __future__ import annotations

import argparse
import logging
import signal
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from threading import Event
from time import perf_counter
from types import FrameType
from typing import Any, Protocol, cast

from schemabridge.application.ports.operational_telemetry import (
    OperationalErrorCode,
    OperationalEvent,
    OperationalOutcome,
    OperationalTelemetryPort,
)
from schemabridge.application.semantic_change_reconciler import (
    SemanticChangeReconcilerIterationOutcome,
    SemanticChangeReconcilerIterationResult,
    SemanticChangeReconcilerUseCaseError,
)

logger = logging.getLogger(__name__)
_SignalHandler = Callable[[int, FrameType | None], Any] | int | None


def _emit_fallback_log(
    *,
    event: OperationalEvent,
    outcome: OperationalOutcome,
    error_code: OperationalErrorCode | None,
) -> None:
    """Retain a fixed safe diagnostic for explicitly injected test runtimes."""

    message = f"{event} outcome={outcome}"
    if error_code is not None:
        message = f"{message} error_code={error_code}"
    level = logging.ERROR if outcome in {"failed", "denied", "degraded"} else logging.INFO
    logger.log(level, message)


class SemanticReconcilerIterationPort(Protocol):
    def execute(self) -> SemanticChangeReconcilerIterationResult:
        """Process at most one durable semantic-change scan."""


class SemanticReconcilerProcessLifecyclePort(Protocol):
    def open(self) -> None:
        """Open resources only after verifying the exact existing control schema."""

    def close(self) -> None:
        """Release the process resource idempotently."""


@dataclass(frozen=True, slots=True)
class SemanticReconcilerProcessRuntime:
    """Injected process components; concrete composition remains outside the entrypoint."""

    reconciler: SemanticReconcilerIterationPort | None = field(repr=False)
    log_level: str
    poll_interval_seconds: float
    control_resource: SemanticReconcilerProcessLifecyclePort | None = field(
        default=None,
        repr=False,
    )
    stop_event: Event = field(default_factory=Event, repr=False)
    telemetry: OperationalTelemetryPort | None = field(default=None, repr=False)
    metrics_exporter: SemanticReconcilerProcessLifecyclePort | None = field(
        default=None,
        repr=False,
    )


def run_semantic_reconciler(
    reconciler: SemanticReconcilerIterationPort,
    *,
    poll_interval_seconds: float,
    once: bool = False,
    stop_event: Event | None = None,
    telemetry: OperationalTelemetryPort | None = None,
) -> int:
    """Poll serially until signalled, or perform exactly one queue iteration."""

    if not 0.1 <= poll_interval_seconds <= 60:
        raise ValueError("semantic reconciler poll interval is outside the supported bound")
    stopping = stop_event or Event()
    while not stopping.is_set():
        started = perf_counter()
        try:
            result = reconciler.execute()
        except SemanticChangeReconcilerUseCaseError:
            duration_ms = min(int((perf_counter() - started) * 1_000), 86_400_000)
            if telemetry is not None:
                telemetry.emit(
                    event="reconciliation.run",
                    outcome="failed",
                    duration_ms=duration_ms,
                    error_code="queue_unavailable",
                    counts={"jobs_failed": 1},
                )
            else:
                _emit_fallback_log(
                    event="reconciliation.run",
                    outcome="failed",
                    error_code="queue_unavailable",
                )
            if once:
                return 1
            stopping.wait(poll_interval_seconds)
            continue
        except Exception:
            duration_ms = min(int((perf_counter() - started) * 1_000), 86_400_000)
            if telemetry is not None:
                telemetry.emit(
                    event="reconciliation.run",
                    outcome="failed",
                    duration_ms=duration_ms,
                    error_code="internal_failure",
                    counts={"jobs_failed": 1},
                )
            else:
                _emit_fallback_log(
                    event="reconciliation.run",
                    outcome="failed",
                    error_code="internal_failure",
                )
            if once:
                return 1
            stopping.wait(poll_interval_seconds)
            continue
        if telemetry is not None:
            failed = result.outcome is SemanticChangeReconcilerIterationOutcome.FAILED
            retry = result.outcome is SemanticChangeReconcilerIterationOutcome.RETRY_SCHEDULED
            stopped = result.outcome is SemanticChangeReconcilerIterationOutcome.STOPPED
            telemetry.emit(
                event="reconciliation.run",
                outcome=(
                    "failed"
                    if failed
                    else ("degraded" if retry else ("cancelled" if stopped else "succeeded"))
                ),
                duration_ms=min(int((perf_counter() - started) * 1_000), 86_400_000),
                error_code=(
                    "queue_unavailable"
                    if failed or retry
                    else ("operation_cancelled" if stopped else None)
                ),
                counts={
                    "jobs_completed": int(
                        result.outcome is SemanticChangeReconcilerIterationOutcome.COMPLETED
                    ),
                    "jobs_failed": int(failed),
                    "retries_scheduled": int(retry),
                },
            )
        else:
            failed = result.outcome is SemanticChangeReconcilerIterationOutcome.FAILED
            retry = result.outcome is SemanticChangeReconcilerIterationOutcome.RETRY_SCHEDULED
            stopped = result.outcome is SemanticChangeReconcilerIterationOutcome.STOPPED
            fallback_outcome: OperationalOutcome = (
                "failed"
                if failed
                else ("degraded" if retry else ("cancelled" if stopped else "succeeded"))
            )
            _emit_fallback_log(
                event="reconciliation.run",
                outcome=fallback_outcome,
                error_code=(
                    "queue_unavailable"
                    if failed or retry
                    else ("operation_cancelled" if stopped else None)
                ),
            )
        if once or result.outcome is SemanticChangeReconcilerIterationOutcome.STOPPED:
            return 0
        if result.outcome is SemanticChangeReconcilerIterationOutcome.IDLE:
            stopping.wait(poll_interval_seconds)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="schemabridge-semantic-reconciler",
        description="Process durable governed semantic-change scans.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once",
        action="store_true",
        help="Process at most one semantic-change queue poll and exit.",
    )
    mode.add_argument(
        "--probe-ready",
        action="store_true",
        help="Verify the exact existing control schema and exit without polling.",
    )
    return parser


def _install_signal_handlers(stop_event: Event) -> dict[int, _SignalHandler]:
    previous: dict[int, _SignalHandler] = {}

    def request_shutdown(signum: int, frame: object) -> None:
        del signum, frame
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, request_shutdown)
    return previous


def _restore_signal_handlers(previous: dict[int, _SignalHandler]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def _build_runtime(*, readiness_probe: bool) -> SemanticReconcilerProcessRuntime:
    """Resolve the composition root lazily while keeping startup schema-only."""

    from schemabridge import bootstrap

    candidate = getattr(bootstrap, "build_semantic_reconciler_process_runtime", None)
    if not callable(candidate):
        raise RuntimeError("semantic reconciler process composition is unavailable")
    runtime = candidate(readiness_probe=readiness_probe)
    return cast(SemanticReconcilerProcessRuntime, runtime)


def command(
    argv: Sequence[str] | None = None,
    *,
    runtime: SemanticReconcilerProcessRuntime | None = None,
) -> int:
    """Run only process components supplied by the composition root."""

    arguments = _parser().parse_args(argv)
    resolved = runtime or _build_runtime(readiness_probe=arguments.probe_ready)
    if resolved.control_resource is not None:
        resolved.control_resource.open()
    metrics_exporter_opened = False
    try:
        if resolved.telemetry is not None:
            resolved.telemetry.emit(
                event="service.health",
                outcome="succeeded",
                duration_ms=0,
            )
        if arguments.probe_ready:
            if resolved.reconciler is not None:
                raise ValueError("readiness runtime must not include a polling reconciler")
            return 0
        if resolved.reconciler is None:
            raise ValueError("polling runtime requires a composed semantic reconciler")
        if resolved.metrics_exporter is not None:
            resolved.metrics_exporter.open()
            metrics_exporter_opened = True
        previous = _install_signal_handlers(resolved.stop_event)
        try:
            return run_semantic_reconciler(
                resolved.reconciler,
                poll_interval_seconds=resolved.poll_interval_seconds,
                once=arguments.once,
                stop_event=resolved.stop_event,
                telemetry=resolved.telemetry,
            )
        finally:
            _restore_signal_handlers(previous)
    finally:
        try:
            if metrics_exporter_opened and resolved.metrics_exporter is not None:
                resolved.metrics_exporter.close()
        finally:
            if resolved.control_resource is not None:
                resolved.control_resource.close()


def main() -> None:
    from schemabridge.bootstrap import configure_runtime_logging

    logging_session = configure_runtime_logging(service="reconciler")
    try:
        logging_session.emit(
            event="service.health",
            outcome="started",
            duration_ms=0,
        )
        status = command()
    except Exception:
        logging_session.set_level("ERROR")
        logging_session.emit(
            event="service.health",
            outcome="failed",
            duration_ms=0,
            error_code="internal_failure",
        )
        raise SystemExit(1) from None
    finally:
        logging_session.close()
    raise SystemExit(status)


if __name__ == "__main__":
    main()
