"""Independent, gracefully stoppable M24 worker process."""

from __future__ import annotations

import argparse
import logging
import signal
from collections.abc import Callable, Sequence
from threading import Event
from types import FrameType
from typing import Any, Protocol

from schemabridge.application.job_worker import (
    WorkerIterationResult,
    WorkerUseCaseError,
)
from schemabridge.bootstrap import (
    WorkerProcessRuntime,
    build_worker_process_runtime,
)

logger = logging.getLogger(__name__)
_SignalHandler = Callable[[int, FrameType | None], Any] | int | None
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class _WorkerPort(Protocol):
    def execute(self) -> WorkerIterationResult:
        """Process at most one durable job."""


def run_worker(
    worker: _WorkerPort,
    *,
    poll_interval_seconds: float,
    once: bool = False,
    stop_event: Event | None = None,
) -> int:
    """Poll serially until a signal requests shutdown or one iteration completes."""

    if not 0.05 <= poll_interval_seconds <= 10:
        raise ValueError("worker poll interval is outside the supported bound")
    stopping = stop_event or Event()
    while not stopping.is_set():
        try:
            result = worker.execute()
        except WorkerUseCaseError as error:
            logger.error("worker_iteration_failed code=%s", error.code.value)
            if once:
                return 1
            stopping.wait(poll_interval_seconds)
            continue
        except Exception as error:
            logger.error(
                "worker_iteration_failed code=unexpected_worker_error error_type=%s",
                type(error).__name__,
            )
            if once:
                return 1
            stopping.wait(poll_interval_seconds)
            continue
        logger.info(
            "worker_iteration outcome=%s status=%s failure_code=%s",
            result.outcome.value,
            result.status.value if result.status is not None else "none",
            result.failure_code.value if result.failure_code is not None else "none",
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
    logging.basicConfig(
        level=resolved.log_level.upper(),
        format=_LOG_FORMAT,
    )
    if resolved.control_pool is not None:
        resolved.control_pool.open()
    try:
        if arguments.probe_ready:
            if resolved.worker is not None:
                raise ValueError("readiness runtime must not include a polling worker")
            return 0
        if resolved.worker is None:
            raise ValueError("polling runtime requires a composed worker")
        stop_event = Event()
        previous = _install_signal_handlers(stop_event)
        try:
            return run_worker(
                resolved.worker,
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
        logger.error("worker_startup_failed error_type=%s", type(error).__name__)
        raise SystemExit(1) from None
    raise SystemExit(status)


if __name__ == "__main__":
    main()
