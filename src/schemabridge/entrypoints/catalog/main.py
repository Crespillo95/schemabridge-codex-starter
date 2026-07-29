"""Independent, gracefully stoppable M25 catalog-indexer process."""

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

from schemabridge.application.catalog_indexer import (
    CatalogIndexerIterationOutcome,
    CatalogIndexerIterationResult,
    CatalogIndexerUseCaseError,
)
from schemabridge.application.ports.operational_telemetry import (
    OperationalErrorCode,
    OperationalEvent,
    OperationalOutcome,
    OperationalTelemetryPort,
)
from schemabridge.domain.catalog_inventory import CatalogRefreshFailureCode

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


class CatalogIndexerIterationPort(Protocol):
    def execute(self) -> CatalogIndexerIterationResult:
        """Process at most one durable catalog refresh."""


class CatalogProcessLifecyclePort(Protocol):
    def open(self) -> None:
        """Start and synchronously verify the bounded process resource."""

    def close(self) -> None:
        """Release the process resource idempotently."""


@dataclass(frozen=True, slots=True)
class CatalogProcessRuntime:
    """Injected process components; concrete composition remains in bootstrap."""

    indexer: CatalogIndexerIterationPort | None = field(repr=False)
    log_level: str
    poll_interval_seconds: float
    control_pool: CatalogProcessLifecyclePort | None = field(
        default=None,
        repr=False,
    )
    stop_event: Event = field(default_factory=Event, repr=False)
    telemetry: OperationalTelemetryPort | None = field(default=None, repr=False)
    metrics_exporter: CatalogProcessLifecyclePort | None = field(
        default=None,
        repr=False,
    )


def _catalog_source_error_code(
    failure_code: CatalogRefreshFailureCode | None,
) -> OperationalErrorCode | None:
    if failure_code in {
        CatalogRefreshFailureCode.SOURCE_UNAVAILABLE,
        CatalogRefreshFailureCode.SOURCE_PERMISSION_DENIED,
        CatalogRefreshFailureCode.SOURCE_INVALID_RESPONSE,
        CatalogRefreshFailureCode.SOURCE_CURSOR_STALLED,
        CatalogRefreshFailureCode.SOURCE_PAGE_REPEATED,
        CatalogRefreshFailureCode.SOURCE_RESPONSE_TOO_LARGE,
        CatalogRefreshFailureCode.SOURCE_ASSET_MALFORMED,
    }:
        return "source_unavailable"
    return None


def run_catalog_indexer(
    indexer: CatalogIndexerIterationPort,
    *,
    poll_interval_seconds: float,
    once: bool = False,
    stop_event: Event | None = None,
    telemetry: OperationalTelemetryPort | None = None,
) -> int:
    """Poll serially until signalled, or perform exactly one queue iteration."""

    if not 0.1 <= poll_interval_seconds <= 60:
        raise ValueError("catalog poll interval is outside the supported bound")
    stopping = stop_event or Event()
    while not stopping.is_set():
        started = perf_counter()
        try:
            result = indexer.execute()
        except CatalogIndexerUseCaseError:
            duration_ms = min(int((perf_counter() - started) * 1_000), 86_400_000)
            if telemetry is not None:
                telemetry.emit(
                    event="catalog.refresh",
                    outcome="failed",
                    duration_ms=duration_ms,
                    error_code="queue_unavailable",
                    counts={"jobs_failed": 1},
                )
            else:
                _emit_fallback_log(
                    event="catalog.refresh",
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
                    event="catalog.refresh",
                    outcome="failed",
                    duration_ms=duration_ms,
                    error_code="internal_failure",
                    counts={"jobs_failed": 1},
                )
            else:
                _emit_fallback_log(
                    event="catalog.refresh",
                    outcome="failed",
                    error_code="internal_failure",
                )
            if once:
                return 1
            stopping.wait(poll_interval_seconds)
            continue
        if telemetry is not None:
            failed = result.outcome is CatalogIndexerIterationOutcome.FAILED
            cancelled = result.outcome is CatalogIndexerIterationOutcome.STOPPED
            duration_ms = min(int((perf_counter() - started) * 1_000), 86_400_000)
            telemetry.emit(
                event="catalog.refresh",
                outcome="failed" if failed else ("cancelled" if cancelled else "succeeded"),
                duration_ms=duration_ms,
                error_code=(
                    "internal_failure" if failed else ("operation_cancelled" if cancelled else None)
                ),
                counts={
                    "jobs_failed": int(failed),
                    "jobs_completed": int(
                        result.outcome is CatalogIndexerIterationOutcome.COMPLETED
                    ),
                    "records_processed": result.asset_count + result.field_count,
                },
            )
            source_error_code = _catalog_source_error_code(result.failure_code)
            if result.outcome is CatalogIndexerIterationOutcome.COMPLETED:
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
        else:
            failed = result.outcome is CatalogIndexerIterationOutcome.FAILED
            cancelled = result.outcome is CatalogIndexerIterationOutcome.STOPPED
            fallback_outcome: OperationalOutcome = (
                "failed" if failed else ("cancelled" if cancelled else "succeeded")
            )
            _emit_fallback_log(
                event="catalog.refresh",
                outcome=fallback_outcome,
                error_code=(
                    "internal_failure" if failed else ("operation_cancelled" if cancelled else None)
                ),
            )
        if once:
            return 0
        if result.status is None:
            stopping.wait(poll_interval_seconds)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="schemabridge-catalog",
        description="Refresh governed catalog metadata through the dedicated indexer role.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once",
        action="store_true",
        help="Process at most one catalog refresh and exit.",
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


def _build_runtime(*, readiness_probe: bool) -> CatalogProcessRuntime:
    """Resolve the composition root lazily so injected entrypoint tests remain isolated."""

    from schemabridge import bootstrap

    candidate = getattr(bootstrap, "build_catalog_process_runtime", None)
    if not callable(candidate):
        raise RuntimeError("catalog process composition is unavailable")
    runtime = candidate(readiness_probe=readiness_probe)
    return cast(CatalogProcessRuntime, runtime)


def command(
    argv: Sequence[str] | None = None,
    *,
    runtime: CatalogProcessRuntime | None = None,
) -> int:
    """Run only components supplied by the bootstrap composition root."""

    arguments = _parser().parse_args(argv)
    resolved = runtime or _build_runtime(readiness_probe=arguments.probe_ready)
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
            if resolved.indexer is not None:
                raise ValueError("readiness runtime must not include a polling indexer")
            return 0
        if resolved.indexer is None:
            raise ValueError("polling runtime requires a composed catalog indexer")
        if resolved.metrics_exporter is not None:
            resolved.metrics_exporter.open()
            metrics_exporter_opened = True
        stop_event = resolved.stop_event
        previous = _install_signal_handlers(stop_event)
        try:
            return run_catalog_indexer(
                resolved.indexer,
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
    from schemabridge.bootstrap import configure_runtime_logging

    logging_session = configure_runtime_logging(service="catalog")
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
