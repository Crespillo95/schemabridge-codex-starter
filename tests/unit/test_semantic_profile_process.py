"""Process-loop tests for the isolated aggregate semantic profile worker."""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import pytest

from schemabridge.adapters.observability.runtime import RuntimeOperationalTelemetry
from schemabridge.application.semantic_profile_worker import (
    SemanticJoinProfileWorkerError,
    SemanticJoinProfileWorkerErrorCode,
    SemanticJoinProfileWorkerOutcome,
    SemanticJoinProfileWorkerResult,
)
from schemabridge.domain.semantic_profile_jobs import (
    SemanticJoinProfileFailureCode,
    SemanticJoinProfileJobStatus,
)
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


def _source_timeout() -> SemanticJoinProfileWorkerResult:
    return SemanticJoinProfileWorkerResult(
        outcome=SemanticJoinProfileWorkerOutcome.RETRY_SCHEDULED,
        job_id=f"profile_job_{'a' * 64}",
        status=SemanticJoinProfileJobStatus.RETRY_WAIT,
        attempts=1,
        failure_code=SemanticJoinProfileFailureCode.SOURCE_TIMEOUT,
    )


def test_probe_opens_only_control_resource() -> None:
    lifecycle = _Lifecycle()
    metrics_exporter = _Lifecycle()
    runtime = SemanticProfileWorkerProcessRuntime(
        worker=None,
        log_level="INFO",
        poll_interval_seconds=0.5,
        control_resource=lifecycle,
        metrics_exporter=metrics_exporter,
    )

    assert command(["--probe-ready"], runtime=runtime) == 0
    assert lifecycle.opened == lifecycle.closed == 1
    assert metrics_exporter.opened == metrics_exporter.closed == 0


def test_once_processes_one_job_and_closes_resource() -> None:
    lifecycle = _Lifecycle()
    metrics_exporter = _Lifecycle()
    iteration = _Iteration(_completed())
    runtime = SemanticProfileWorkerProcessRuntime(
        worker=iteration,
        log_level="INFO",
        poll_interval_seconds=0.5,
        control_resource=lifecycle,
        metrics_exporter=metrics_exporter,
    )

    assert command(["--once"], runtime=runtime) == 0
    assert iteration.calls == 1
    assert lifecycle.opened == lifecycle.closed == 1
    assert metrics_exporter.opened == metrics_exporter.closed == 1


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


@pytest.mark.parametrize(
    ("result", "source_outcome"),
    [
        (_idle(), None),
        (_completed(), "success"),
        (_source_timeout(), "timeout"),
    ],
)
def test_profile_source_metrics_exclude_idle_polls(
    result: SemanticJoinProfileWorkerResult,
    source_outcome: str | None,
) -> None:
    telemetry = RuntimeOperationalTelemetry(
        service="profile",
        environment="production",
        stream=io.StringIO(),
    )

    assert (
        run_semantic_profile_worker(
            _Iteration(result),
            poll_interval_seconds=0.05,
            once=True,
            telemetry=telemetry,
        )
        == 0
    )

    metrics = telemetry.render_openmetrics()
    if source_outcome is None:
        assert "schemabridge_source_operations_total{" not in metrics
    else:
        assert (
            "schemabridge_source_operations_total"
            f'{{capability="profile",outcome="{source_outcome}"}} 1.0' in metrics
        )


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
    assert "profile.run outcome=failed error_code=queue_unavailable" in caplog.text
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
    assert "profile.run outcome=failed error_code=internal_failure" in caplog.text
    assert "RuntimeError" not in caplog.text
    assert "protected-source-value" not in caplog.text


def test_entrypoint_has_no_llm_datahub_or_database_adapter_imports() -> None:
    source = (ROOT / "src/schemabridge/entrypoints/semantic_profile_worker/main.py").read_text(
        encoding="utf-8"
    )
    lowered = source.casefold()

    assert "openai" not in lowered
    assert "datahub" not in lowered
    assert "psycopg" not in lowered
    assert source.count("from schemabridge.adapters.") == 0
    assert "from schemabridge.bootstrap import configure_runtime_logging" in source
