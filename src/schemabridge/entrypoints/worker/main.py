"""Independent, gracefully stoppable M24 worker process."""

from __future__ import annotations

import argparse
import logging
import signal
from collections.abc import Callable, Sequence
from threading import Event
from time import perf_counter
from types import FrameType
from typing import Any, Protocol

from schemabridge.application.job_worker import (
    WorkerIterationOutcome,
    WorkerIterationResult,
    WorkerUseCaseError,
)
from schemabridge.application.ports.operational_telemetry import (
    OperationalErrorCode,
    OperationalOutcome,
    OperationalTelemetryPort,
)
from schemabridge.bootstrap import (
    WorkerProcessRuntime,
    build_worker_process_runtime,
    configure_runtime_logging,
)
from schemabridge.domain.background_jobs import JobFailureCode

logger = logging.getLogger(__name__)
_SignalHandler = Callable[[int, FrameType | None], Any] | int | None


class _WorkerPort(Protocol):
    def execute(self) -> WorkerIterationResult:
        """Process at most one durable job."""


def _emit_iteration_result(
    telemetry: OperationalTelemetryPort,
    result: WorkerIterationResult,
    *,
    duration_ms: int,
) -> None:
    """Emit only a real job outcome; an idle queue is not a source success."""

    if result.outcome is WorkerIterationOutcome.IDLE:
        return
    outcome: OperationalOutcome
    error_code: OperationalErrorCode | None
    counts = {
        "jobs_completed": int(result.outcome is WorkerIterationOutcome.SUCCEEDED),
        "jobs_failed": int(
            result.outcome
            in {
                WorkerIterationOutcome.FAILED,
                WorkerIterationOutcome.DEAD_LETTERED,
            }
        ),
        "retries_scheduled": int(result.outcome is WorkerIterationOutcome.RETRY_SCHEDULED),
        "dead_letters": int(result.outcome is WorkerIterationOutcome.DEAD_LETTERED),
    }
    if result.outcome is WorkerIterationOutcome.SUCCEEDED:
        outcome = "succeeded"
        error_code = None
    elif result.outcome is WorkerIterationOutcome.CANCELLED:
        outcome = "cancelled"
        error_code = "operation_cancelled"
    elif result.outcome is WorkerIterationOutcome.RETRY_SCHEDULED:
        outcome = "degraded"
        error_code = _worker_failure_error_code(result.failure_code)
    else:
        outcome = "failed"
        error_code = _worker_failure_error_code(result.failure_code)
    telemetry.emit(
        event="job.execution",
        outcome=outcome,
        duration_ms=duration_ms,
        error_code=error_code,
        counts=counts,
    )
    source_error_code = _source_operation_error_code(result.failure_code)
    if result.outcome is WorkerIterationOutcome.SUCCEEDED:
        telemetry.emit(
            event="source.operation",
            outcome="succeeded",
            duration_ms=duration_ms,
        )
    elif source_error_code is not None:
        telemetry.emit(
            event="source.operation",
            outcome="failed",
            duration_ms=duration_ms,
            error_code=source_error_code,
        )


def _emit_iteration_log(result: WorkerIterationResult, *, duration_ms: int) -> None:
    """Emit the same closed result contract when no metrics sink is composed."""

    if result.outcome is WorkerIterationOutcome.IDLE:
        return
    if result.outcome is WorkerIterationOutcome.SUCCEEDED:
        outcome: OperationalOutcome = "succeeded"
        error_code: OperationalErrorCode | None = None
    elif result.outcome is WorkerIterationOutcome.CANCELLED:
        outcome = "cancelled"
        error_code = "operation_cancelled"
    elif result.outcome is WorkerIterationOutcome.RETRY_SCHEDULED:
        outcome = "degraded"
        error_code = _worker_failure_error_code(result.failure_code)
    else:
        outcome = "failed"
        error_code = _worker_failure_error_code(result.failure_code)
    _emit_fallback_log(
        event="job.execution",
        outcome=outcome,
        error_code=error_code,
    )


def _emit_fallback_log(
    *,
    event: str,
    outcome: OperationalOutcome,
    error_code: OperationalErrorCode | None,
) -> None:
    """Retain a fixed safe diagnostic for explicitly injected test runtimes."""

    message = f"{event} outcome={outcome}"
    if error_code is not None:
        message = f"{message} error_code={error_code}"
    level = logging.ERROR if outcome in {"failed", "denied", "degraded"} else logging.INFO
    logger.log(level, message)


def _worker_failure_error_code(
    failure_code: JobFailureCode | None,
) -> OperationalErrorCode:
    if failure_code is JobFailureCode.SOURCE_TIMEOUT:
        return "source_timeout"
    if failure_code in {
        JobFailureCode.REGISTRY_UNAVAILABLE,
        JobFailureCode.SOURCE_UNAVAILABLE,
    }:
        return "source_unavailable"
    if failure_code in {
        JobFailureCode.AUTHORIZATION_EXPIRED,
        JobFailureCode.AUTHORIZATION_MISMATCH,
    }:
        return "stale_authorization"
    return "internal_failure"


def _source_operation_error_code(
    failure_code: JobFailureCode | None,
) -> OperationalErrorCode | None:
    if failure_code is JobFailureCode.SOURCE_TIMEOUT:
        return "source_timeout"
    if failure_code is JobFailureCode.SOURCE_UNAVAILABLE:
        return "source_unavailable"
    return None


def run_worker(
    worker: _WorkerPort,
    *,
    poll_interval_seconds: float,
    once: bool = False,
    stop_event: Event | None = None,
    telemetry: OperationalTelemetryPort | None = None,
) -> int:
    """Poll serially until a signal requests shutdown or one iteration completes."""

    if not 0.05 <= poll_interval_seconds <= 10:
        raise ValueError("worker poll interval is outside the supported bound")
    stopping = stop_event or Event()
    while not stopping.is_set():
        started = perf_counter()
        try:
            result = worker.execute()
        except WorkerUseCaseError:
            duration_ms = min(int((perf_counter() - started) * 1_000), 86_400_000)
            if telemetry is not None:
                telemetry.emit(
                    event="job.execution",
                    outcome="failed",
                    duration_ms=duration_ms,
                    error_code="queue_unavailable",
                    counts={"jobs_failed": 1},
                )
            else:
                _emit_fallback_log(
                    event="job.execution",
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
                    event="job.execution",
                    outcome="failed",
                    duration_ms=duration_ms,
                    error_code="internal_failure",
                    counts={"jobs_failed": 1},
                )
            else:
                _emit_fallback_log(
                    event="job.execution",
                    outcome="failed",
                    error_code="internal_failure",
                )
            if once:
                return 1
            stopping.wait(poll_interval_seconds)
            continue
        if telemetry is not None:
            _emit_iteration_result(
                telemetry,
                result,
                duration_ms=min(int((perf_counter() - started) * 1_000), 86_400_000),
            )
        else:
            _emit_iteration_log(
                result,
                duration_ms=min(int((perf_counter() - started) * 1_000), 86_400_000),
            )
        if once:
            return 0
        if result.job_id is None:
            stopping.wait(poll_interval_seconds)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="schemabridge-worker",
        description="Process governed execution jobs through the dedicated worker role.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once",
        action="store_true",
        help="Process at most one queue poll and exit.",
    )
    mode.add_argument(
        "--probe-ready",
        action="store_true",
        help="Check exact control-plane schema readiness and exit without polling.",
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


def command(
    argv: Sequence[str] | None = None,
    *,
    runtime: WorkerProcessRuntime | None = None,
) -> int:
    """Run a worker runtime composed exclusively by the bootstrap root."""

    arguments = _parser().parse_args(argv)
    resolved = runtime or build_worker_process_runtime(
        readiness_probe=arguments.probe_ready,
    )
    if resolved.control_pool is not None:
        resolved.control_pool.open()
    metrics_exporter_opened = False
    try:
        if resolved.telemetry is not None:
            resolved.telemetry.emit(
                event="service.health",
                outcome="succeeded",
                duration_ms=0,
            )
        if arguments.probe_ready:
            if resolved.worker is not None:
                raise ValueError("readiness runtime must not include a polling worker")
            return 0
        if resolved.worker is None:
            raise ValueError("polling runtime requires a composed worker")
        if resolved.metrics_exporter is not None:
            resolved.metrics_exporter.open()
            metrics_exporter_opened = True
        stop_event = Event()
        previous = _install_signal_handlers(stop_event)
        try:
            return run_worker(
                resolved.worker,
                poll_interval_seconds=resolved.poll_interval_seconds,
                once=arguments.once,
                stop_event=stop_event,
                telemetry=resolved.telemetry,
            )
        finally:
            _restore_signal_handlers(previous)
    finally:
        try:
            if metrics_exporter_opened and resolved.metrics_exporter is not None:
                resolved.metrics_exporter.close()
        finally:
            if resolved.control_pool is not None:
                resolved.control_pool.close()


def main() -> None:
    logging_session = configure_runtime_logging(service="worker")
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
