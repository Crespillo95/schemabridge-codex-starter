"""Process-loop tests for the isolated aggregate semantic profile worker."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import pytest

from schemabridge.application.semantic_profile_worker import (
    SemanticJoinProfileWorkerError,
    SemanticJoinProfileWorkerErrorCode,
    SemanticJoinProfileWorkerOutcome,
    SemanticJoinProfileWorkerResult,
)
from schemabridge.domain.semantic_profile_jobs import SemanticJoinProfileJobStatus
from schemabridge.entrypoints.semantic_profile_worker.main import (
    SemanticProfileWorkerProcessRuntime,
    command,
    run_semantic_profile_worker,
)

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class _Iteration:
    result: SemanticJoinProfileWorkerResult
    error: Exception | None = None
    calls: int = 0

    def execute(self) -> SemanticJoinProfileWorkerResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


@dataclass
class _Lifecycle:
    opened: int = 0
    closed: int = 0

    def open(self) -> None:
        self.opened += 1

    def close(self) -> None:
        self.closed += 1


class _StopAfterWait(Event):
    waits: int = 0

    def wait(self, timeout: float | None = None) -> bool:
        assert timeout is not None
        self.waits += 1
        self.set()
        return True


def _idle() -> SemanticJoinProfileWorkerResult:
    return SemanticJoinProfileWorkerResult(outcome=SemanticJoinProfileWorkerOutcome.IDLE)


def _completed() -> SemanticJoinProfileWorkerResult:
    return SemanticJoinProfileWorkerResult(
        outcome=SemanticJoinProfileWorkerOutcome.COMPLETED,
        job_id=f"profile_job_{'a' * 64}",
        status=SemanticJoinProfileJobStatus.COMPLETED,
        attempts=1,
        result_fingerprint="b" * 64,
    )


def test_probe_opens_only_control_resource() -> None:
    lifecycle = _Lifecycle()
    runtime = SemanticProfileWorkerProcessRuntime(
        worker=None,
        log_level="INFO",
        poll_interval_seconds=0.5,
        control_resource=lifecycle,
    )

    assert command(["--probe-ready"], runtime=runtime) == 0
    assert lifecycle.opened == lifecycle.closed == 1


def test_once_processes_one_job_and_closes_resource() -> None:
    lifecycle = _Lifecycle()
    iteration = _Iteration(_completed())
    runtime = SemanticProfileWorkerProcessRuntime(
        worker=iteration,
        log_level="INFO",
        poll_interval_seconds=0.5,
        control_resource=lifecycle,
    )

    assert command(["--once"], runtime=runtime) == 0
    assert iteration.calls == 1
    assert lifecycle.opened == lifecycle.closed == 1


def test_idle_wait_is_interruptible() -> None:
    iteration = _Iteration(_idle())
    stopping = _StopAfterWait()

    assert (
        run_semantic_profile_worker(
            iteration,
            poll_interval_seconds=0.05,
            stop_event=stopping,
        )
        == 0
    )
    assert iteration.calls == 1
    assert stopping.waits == 1


def test_closed_and_unexpected_errors_never_log_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    known = _Iteration(
        _idle(),
        error=SemanticJoinProfileWorkerError(
            SemanticJoinProfileWorkerErrorCode.STORE_UNAVAILABLE,
            "source-secret-and-capability",
        ),
    )
    with caplog.at_level(logging.ERROR):
        assert (
            run_semantic_profile_worker(
                known,
                poll_interval_seconds=0.05,
                once=True,
            )
            == 1
        )
    assert "semantic_profile_worker_store_unavailable" in caplog.text
    assert "source-secret-and-capability" not in caplog.text

    caplog.clear()
    unexpected = _Iteration(_idle(), error=RuntimeError("protected-source-value"))
    with caplog.at_level(logging.ERROR):
        assert (
            run_semantic_profile_worker(
                unexpected,
                poll_interval_seconds=0.05,
                once=True,
            )
            == 1
        )
    assert "RuntimeError" in caplog.text
    assert "protected-source-value" not in caplog.text


def test_entrypoint_has_no_llm_datahub_or_database_adapter_imports() -> None:
    source = (ROOT / "src/schemabridge/entrypoints/semantic_profile_worker/main.py").read_text(
        encoding="utf-8"
    )
    lowered = source.casefold()

    assert "openai" not in lowered
    assert "datahub" not in lowered
    assert "psycopg" not in lowered
    assert "schemabridge.adapters" not in source
