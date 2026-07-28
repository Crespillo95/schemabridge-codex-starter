"""Independent worker process for queued aggregate-only join profiles."""

from __future__ import annotations

import argparse
import logging
import signal
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from threading import Event
from types import FrameType
from typing import Any, Protocol, cast

from schemabridge.application.semantic_profile_worker import (
    SemanticJoinProfileWorkerError,
    SemanticJoinProfileWorkerOutcome,
    SemanticJoinProfileWorkerResult,
)

logger = logging.getLogger(__name__)
_SignalHandler = Callable[[int, FrameType | None], Any] | int | None
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class SemanticProfileIterationPort(Protocol):
    def execute(self) -> SemanticJoinProfileWorkerResult:
        """Process at most one aggregate profile job."""


class SemanticProfileProcessLifecyclePort(Protocol):
    def open(self) -> None:
        """Open resources only after exact schema preflight."""

    def close(self) -> None:
        """Release process resources idempotently."""


@dataclass(frozen=True, slots=True)
class SemanticProfileWorkerProcessRuntime:
    """Injected process components; concrete composition stays in bootstrap."""

    worker: SemanticProfileIterationPort | None = field(repr=False)
    log_level: str
    poll_interval_seconds: float
    control_resource: SemanticProfileProcessLifecyclePort | None = field(
        default=None,
        repr=False,
    )
    stop_event: Event = field(default_factory=Event, repr=False)


def run_semantic_profile_worker(
    worker: SemanticProfileIterationPort,
    *,
    poll_interval_seconds: float,
    once: bool = False,
    stop_event: Event | None = None,
) -> int:
    """Poll serially until signalled, or perform exactly one iteration."""

    if not 0.05 <= poll_interval_seconds <= 60:
        raise ValueError("semantic profile poll interval is outside the supported bound")
    stopping = stop_event or Event()
    while not stopping.is_set():
        try:
            result = worker.execute()
        except SemanticJoinProfileWorkerError as error:
            logger.error("semantic_profile_iteration_failed code=%s", error.code.value)
            if once:
                return 1
            stopping.wait(poll_interval_seconds)
            continue
        except Exception as error:
            logger.error(
                "semantic_profile_iteration_failed "
                "code=unexpected_profile_worker_error error_type=%s",
                type(error).__name__,
            )
            if once:
                return 1
            stopping.wait(poll_interval_seconds)
            continue
        logger.info(
            "semantic_profile_iteration outcome=%s status=%s failure_code=%s attempts=%s",
            result.outcome.value,
            result.status.value if result.status is not None else "none",
            result.failure_code.value if result.failure_code is not None else "none",
            result.attempts,
        )
        if once or result.outcome is SemanticJoinProfileWorkerOutcome.STOPPED:
            return 0
        if result.outcome is SemanticJoinProfileWorkerOutcome.IDLE:
            stopping.wait(poll_interval_seconds)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="schemabridge-semantic-profile-worker",
        description="Process aggregate-only join profile jobs through the read-only source role.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once",
        action="store_true",
        help="Process at most one profile queue poll and exit.",
    )
    mode.add_argument(
        "--probe-ready",
        action="store_true",
        help="Verify the exact control schema and exit without source I/O.",
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


def _build_runtime(*, readiness_probe: bool) -> SemanticProfileWorkerProcessRuntime:
    from schemabridge import bootstrap

    candidate = getattr(bootstrap, "build_semantic_profile_worker_process_runtime", None)
    if not callable(candidate):
        raise RuntimeError("semantic profile worker composition is unavailable")
    return cast(
        SemanticProfileWorkerProcessRuntime,
        candidate(readiness_probe=readiness_probe),
    )


def command(
    argv: Sequence[str] | None = None,
    *,
    runtime: SemanticProfileWorkerProcessRuntime | None = None,
) -> int:
    """Run only process components supplied by the composition root."""

    arguments = _parser().parse_args(argv)
    resolved = runtime or _build_runtime(readiness_probe=arguments.probe_ready)
    logging.basicConfig(level=resolved.log_level.upper(), format=_LOG_FORMAT)
    if resolved.control_resource is not None:
        resolved.control_resource.open()
    try:
        if arguments.probe_ready:
            if resolved.worker is not None:
                raise ValueError("readiness runtime must not include a polling profile worker")
            return 0
        if resolved.worker is None:
            raise ValueError("polling runtime requires a composed profile worker")
        previous = _install_signal_handlers(resolved.stop_event)
        try:
            return run_semantic_profile_worker(
                resolved.worker,
                poll_interval_seconds=resolved.poll_interval_seconds,
                once=arguments.once,
                stop_event=resolved.stop_event,
            )
        finally:
            _restore_signal_handlers(previous)
    finally:
        if resolved.control_resource is not None:
            resolved.control_resource.close()


def main() -> None:
    try:
        status = command()
    except Exception as error:
        logging.basicConfig(level=logging.ERROR, format=_LOG_FORMAT)
        logger.error(
            "semantic_profile_startup_failed error_type=%s",
            type(error).__name__,
        )
        raise SystemExit(1) from None
    raise SystemExit(status)


if __name__ == "__main__":
    main()
