"""Process-loop tests for the independent M26 semantic reconciler."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import pytest

from schemabridge.application.semantic_change_reconciler import (
    SemanticChangeReconcilerIterationOutcome,
    SemanticChangeReconcilerIterationResult,
    SemanticChangeReconcilerUseCaseError,
    SemanticChangeReconcilerUseCaseErrorCode,
)
from schemabridge.domain.semantic_change_scans import SemanticChangeScanStatus
from schemabridge.entrypoints.semantic_reconciler.main import (
    SemanticReconcilerProcessRuntime,
    command,
    run_semantic_reconciler,
)

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class FakeIteration:
    result: SemanticChangeReconcilerIterationResult
    error: Exception | None = None
    calls: int = 0

    def execute(self) -> SemanticChangeReconcilerIterationResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


@dataclass
class TrackingLifecycle:
    fail_open: bool = False
    opened: int = 0
    closed: int = 0

    def open(self) -> None:
        self.opened += 1
        if self.fail_open:
            raise RuntimeError("synthetic schema mismatch detail")

    def close(self) -> None:
        self.closed += 1


class StopAfterWait(Event):
    waits: int = 0

    def wait(self, timeout: float | None = None) -> bool:
        assert timeout is not None
        self.waits += 1
        self.set()
        return True


def _idle() -> SemanticChangeReconcilerIterationResult:
    return SemanticChangeReconcilerIterationResult(
        outcome=SemanticChangeReconcilerIterationOutcome.IDLE
    )


def _completed() -> SemanticChangeReconcilerIterationResult:
    return SemanticChangeReconcilerIterationResult(
        outcome=SemanticChangeReconcilerIterationOutcome.COMPLETED,
        scan_id=f"scan_{'a' * 64}",
        status=SemanticChangeScanStatus.COMPLETED,
        report_id=f"report_{'b' * 64}",
        attempts=1,
    )


def test_probe_ready_opens_schema_check_only_and_closes_resource() -> None:
    lifecycle = TrackingLifecycle()
    metrics_exporter = TrackingLifecycle()
    runtime = SemanticReconcilerProcessRuntime(
        reconciler=None,
        log_level="INFO",
        poll_interval_seconds=1,
        control_resource=lifecycle,
        metrics_exporter=metrics_exporter,
    )

    assert command(["--probe-ready"], runtime=runtime) == 0
    assert lifecycle.opened == 1
    assert lifecycle.closed == 1
    assert metrics_exporter.opened == 0
    assert metrics_exporter.closed == 0


def test_once_processes_one_iteration_and_closes_resource() -> None:
    lifecycle = TrackingLifecycle()
    metrics_exporter = TrackingLifecycle()
    reconciler = FakeIteration(_completed())
    runtime = SemanticReconcilerProcessRuntime(
        reconciler=reconciler,
        log_level="INFO",
        poll_interval_seconds=1,
        control_resource=lifecycle,
        metrics_exporter=metrics_exporter,
    )

    assert command(["--once"], runtime=runtime) == 0
    assert reconciler.calls == 1
    assert lifecycle.opened == lifecycle.closed == 1
    assert metrics_exporter.opened == metrics_exporter.closed == 1


def test_graceful_stop_does_not_claim_another_scan() -> None:
    reconciler = FakeIteration(_idle())
    stopping = Event()
    stopping.set()

    assert (
        run_semantic_reconciler(
            reconciler,
            poll_interval_seconds=0.1,
            stop_event=stopping,
        )
        == 0
    )
    assert reconciler.calls == 0


def test_idle_poll_uses_interruptible_wait_and_exits_after_stop() -> None:
    reconciler = FakeIteration(_idle())
    stopping = StopAfterWait()

    assert (
        run_semantic_reconciler(
            reconciler,
            poll_interval_seconds=0.1,
            stop_event=stopping,
        )
        == 0
    )
    assert reconciler.calls == 1
    assert stopping.waits == 1


def test_iteration_errors_log_only_closed_code_or_exception_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    safe_error = FakeIteration(
        _idle(),
        error=SemanticChangeReconcilerUseCaseError(
            SemanticChangeReconcilerUseCaseErrorCode.STORE_UNAVAILABLE,
            "secret-dsn-and-capability",
        ),
    )
    with caplog.at_level(logging.ERROR):
        assert (
            run_semantic_reconciler(
                safe_error,
                poll_interval_seconds=0.1,
                once=True,
            )
            == 1
        )
    assert "reconciliation.run outcome=failed error_code=queue_unavailable" in caplog.text
    assert "secret-dsn-and-capability" not in caplog.text

    caplog.clear()
    unexpected = FakeIteration(_idle(), error=RuntimeError("protected-source-value"))
    with caplog.at_level(logging.ERROR):
        assert (
            run_semantic_reconciler(
                unexpected,
                poll_interval_seconds=0.1,
                once=True,
            )
            == 1
        )
    assert "reconciliation.run outcome=failed error_code=internal_failure" in caplog.text
    assert "RuntimeError" not in caplog.text
    assert "protected-source-value" not in caplog.text


def test_schema_mismatch_fails_before_polling_without_auto_migration() -> None:
    lifecycle = TrackingLifecycle(fail_open=True)
    reconciler = FakeIteration(_completed())
    runtime = SemanticReconcilerProcessRuntime(
        reconciler=reconciler,
        log_level="INFO",
        poll_interval_seconds=1,
        control_resource=lifecycle,
    )

    with pytest.raises(RuntimeError, match="schema mismatch"):
        command(["--once"], runtime=runtime)

    assert lifecycle.opened == 1
    assert lifecycle.closed == 0
    assert reconciler.calls == 0


def test_entrypoint_has_no_migration_datahub_source_or_llm_composition() -> None:
    source = (ROOT / "src/schemabridge/entrypoints/semantic_reconciler/main.py").read_text(
        encoding="utf-8"
    )
    lowered = source.casefold()

    assert "migrator" not in lowered
    assert "auto_migrate" not in lowered
    assert "datahub" not in lowered
    assert "openai" not in lowered
    assert "psycopg" not in lowered
    assert source.count("from schemabridge.adapters.") == 0
    assert "from schemabridge.bootstrap import configure_runtime_logging" in source
