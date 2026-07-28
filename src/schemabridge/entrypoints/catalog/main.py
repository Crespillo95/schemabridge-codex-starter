"""Independent, gracefully stoppable M25 catalog-indexer process."""

from __future__ import annotations

import argparse
import logging
import signal
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from threading import Event
from types import FrameType
from typing import Any, Protocol, cast

from schemabridge.application.catalog_indexer import (
    CatalogIndexerIterationResult,
    CatalogIndexerUseCaseError,
)

logger = logging.getLogger(__name__)
_SignalHandler = Callable[[int, FrameType | None], Any] | int | None
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


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


def run_catalog_indexer(
    indexer: CatalogIndexerIterationPort,
    *,
    poll_interval_seconds: float,
    once: bool = False,
    stop_event: Event | None = None,
) -> int:
    """Poll serially until signalled, or perform exactly one queue iteration."""

    if not 0.1 <= poll_interval_seconds <= 60:
        raise ValueError("catalog poll interval is outside the supported bound")
    stopping = stop_event or Event()
    while not stopping.is_set():
        try:
            result = indexer.execute()
        except CatalogIndexerUseCaseError as error:
            logger.error("catalog_iteration_failed code=%s", error.code.value)
            if once:
                return 1
            stopping.wait(poll_interval_seconds)
            continue
        except Exception as error:
            logger.error(
                "catalog_iteration_failed code=unexpected_catalog_error error_type=%s",
                type(error).__name__,
            )
            if once:
                return 1
            stopping.wait(poll_interval_seconds)
            continue
        logger.info(
            "catalog_iteration outcome=%s status=%s failure_code=%s "
            "pages_processed=%s total_pages=%s asset_count=%s field_count=%s",
            result.outcome.value,
            result.status.value if result.status is not None else "none",
            result.failure_code.value if result.failure_code is not None else "none",
            result.pages_processed,
            result.total_pages,
            result.asset_count,
            result.field_count,
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
    logging.basicConfig(
        level=resolved.log_level.upper(),
        format=_LOG_FORMAT,
    )
    if resolved.control_pool is not None:
        resolved.control_pool.open()
    try:
        if arguments.probe_ready:
            if resolved.indexer is not None:
                raise ValueError("readiness runtime must not include a polling indexer")
            return 0
        if resolved.indexer is None:
            raise ValueError("polling runtime requires a composed catalog indexer")
        stop_event = resolved.stop_event
        previous = _install_signal_handlers(stop_event)
        try:
            return run_catalog_indexer(
                resolved.indexer,
                poll_interval_seconds=resolved.poll_interval_seconds,
                once=arguments.once,
                stop_event=stop_event,
            )
        finally:
            _restore_signal_handlers(previous)
    finally:
        if resolved.control_pool is not None:
            resolved.control_pool.close()


def main() -> None:
    try:
        status = command()
    except Exception as error:
        logging.basicConfig(level=logging.ERROR, format=_LOG_FORMAT)
        logger.error("catalog_startup_failed error_type=%s", type(error).__name__)
        raise SystemExit(1) from None
    raise SystemExit(status)


if __name__ == "__main__":
    main()
