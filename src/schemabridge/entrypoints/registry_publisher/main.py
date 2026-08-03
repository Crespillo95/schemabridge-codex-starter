"""Independent, gracefully stoppable governed registry publisher process."""

from __future__ import annotations

import argparse
import logging
import signal
from collections.abc import Callable, Sequence
from threading import Event
from time import perf_counter
from types import FrameType
from typing import Any, Protocol

from schemabridge.application.ports.operational_telemetry import (
    OperationalErrorCode,
    OperationalOutcome,
    OperationalTelemetryPort,
)
from schemabridge.application.registry_publication_worker import (
    RegistryPublisherIterationOutcome,
    RegistryPublisherIterationResult,
    RegistryPublisherWorkerError,
    RegistryPublisherWorkerErrorCode,
)
from schemabridge.bootstrap import (
    RegistryPublisherProcessRuntime,
    build_registry_publisher_process_runtime,
    configure_runtime_logging,
)
from schemabridge.domain.registry_publication_jobs import RegistryPublicationFailureCode

logger = logging.getLogger(__name__)
_SignalHandler = Callable[[int, FrameType | None], Any] | int | None


class _PublisherPort(Protocol):
    def execute(self) -> RegistryPublisherIterationResult:
        """Process at most one registry publication transition."""


def _failure_error_code(
    failure: RegistryPublicationFailureCode | None,
) -> OperationalErrorCode:
    if failure in {
        RegistryPublicationFailureCode.AUTHORIZATION_EXPIRED,
        RegistryPublicationFailureCode.AUTHORIZATION_MISMATCH,
    }:
        return "stale_authorization"
    if failure in {
        RegistryPublicationFailureCode.BASE_UNAVAILABLE,
        RegistryPublicationFailureCode.DATAHUB_UNAVAILABLE,
        RegistryPublicationFailureCode.READBACK_REQUIRED,
    }:
        return "source_unavailable"
    return "internal_failure"


def _worker_error_code(error: RegistryPublisherWorkerError) -> OperationalErrorCode:
    if error.code is RegistryPublisherWorkerErrorCode.STORE_UNAVAILABLE:
        return "queue_unavailable"
    if error.code is RegistryPublisherWorkerErrorCode.LEASE_LOST:
        return "lease_lost"
    return "internal_failure"


def _emit_result(
    telemetry: OperationalTelemetryPort,
    result: RegistryPublisherIterationResult,
    *,
    duration_ms: int,
) -> None:
    if result.outcome is RegistryPublisherIterationOutcome.IDLE:
        return
    if result.outcome in {
        RegistryPublisherIterationOutcome.CANDIDATE_READY,
        RegistryPublisherIterationOutcome.ACTIVATION_READY,
    }:
        outcome: OperationalOutcome = "succeeded"
        error_code: OperationalErrorCode | None = None
    elif result.outcome is RegistryPublisherIterationOutcome.CANCELLED:
        outcome = "cancelled"
        error_code = "operation_cancelled"
    elif result.outcome is RegistryPublisherIterationOutcome.RETRY_SCHEDULED:
        outcome = "degraded"
        error_code = _failure_error_code(result.failure_code)
    else:
        outcome = "failed"
        error_code = _failure_error_code(result.failure_code)
    telemetry.emit(
        event="registry.publication",
        outcome=outcome,
        duration_ms=duration_ms,
        error_code=error_code,
        counts={
            "jobs_completed": int(
                result.outcome is RegistryPublisherIterationOutcome.ACTIVATION_READY
            ),
            "jobs_failed": int(
                result.outcome
                in {
                    RegistryPublisherIterationOutcome.FAILED,
                    RegistryPublisherIterationOutcome.DEAD_LETTERED,
                }
            ),
            "retries_scheduled": int(
                result.outcome is RegistryPublisherIterationOutcome.RETRY_SCHEDULED
            ),
            "dead_letters": int(result.outcome is RegistryPublisherIterationOutcome.DEAD_LETTERED),
        },
    )


def run_registry_publisher(
    publisher: _PublisherPort,
    *,
    poll_interval_seconds: float,
    once: bool = False,
    stop_event: Event | None = None,
    telemetry: OperationalTelemetryPort | None = None,
) -> int:
    if not 0.05 <= poll_interval_seconds <= 10:
        raise ValueError("registry publisher poll interval is outside its bound")
    stopping = stop_event or Event()
    while not stopping.is_set():
        started = perf_counter()
        try:
            result = publisher.execute()
        except RegistryPublisherWorkerError as error:
            duration_ms = min(int((perf_counter() - started) * 1_000), 86_400_000)
            if telemetry is not None:
                telemetry.emit(
                    event="registry.publication",
                    outcome="failed",
                    duration_ms=duration_ms,
                    error_code=_worker_error_code(error),
                    counts={"jobs_failed": 1},
                )
            if once:
                return 1
            stopping.wait(poll_interval_seconds)
            continue
        except Exception:
            if telemetry is not None:
                telemetry.emit(
                    event="registry.publication",
                    outcome="failed",
                    duration_ms=min(int((perf_counter() - started) * 1_000), 86_400_000),
                    error_code="internal_failure",
                    counts={"jobs_failed": 1},
                )
            if once:
                return 1
            stopping.wait(poll_interval_seconds)
            continue
        if telemetry is not None:
            _emit_result(
                telemetry,
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
        prog="schemabridge-registry-publisher",
        description="Publish approved immutable registry versions through the isolated role.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Process one queue poll and exit.")
    mode.add_argument(
        "--probe-ready",
        action="store_true",
        help="Check the control plane without resolving the DataHub writer secret.",
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
    runtime: RegistryPublisherProcessRuntime | None = None,
) -> int:
    arguments = _parser().parse_args(argv)
    resolved = runtime or build_registry_publisher_process_runtime(
        readiness_probe=arguments.probe_ready
    )
    if resolved.control_pool is not None:
        resolved.control_pool.open()
    metrics_opened = False
    try:
        if resolved.telemetry is not None:
            resolved.telemetry.emit(event="service.health", outcome="succeeded", duration_ms=0)
        if arguments.probe_ready:
            if resolved.publisher is not None:
                raise ValueError("readiness runtime must not include a publisher")
            return 0
        if resolved.publisher is None:
            raise ValueError("polling runtime requires a publisher")
        if resolved.metrics_exporter is not None:
            resolved.metrics_exporter.open()
            metrics_opened = True
        stop_event = Event()
        previous = _install_signal_handlers(stop_event)
        try:
            return run_registry_publisher(
                resolved.publisher,
                poll_interval_seconds=resolved.poll_interval_seconds,
                once=arguments.once,
                stop_event=stop_event,
                telemetry=resolved.telemetry,
            )
        finally:
            _restore_signal_handlers(previous)
    finally:
        try:
            if metrics_opened and resolved.metrics_exporter is not None:
                resolved.metrics_exporter.close()
        finally:
            if resolved.control_pool is not None:
                resolved.control_pool.close()


def main() -> None:
    logging_session = configure_runtime_logging(service="publisher")
    try:
        logging_session.emit(event="service.health", outcome="started", duration_ms=0)
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
