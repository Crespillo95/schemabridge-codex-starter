"""Unit tests for the independent M25 catalog-indexer process."""

from __future__ import annotations

import logging
import signal
from threading import Event

import pytest

import schemabridge.entrypoints.catalog.main as catalog_main
from schemabridge.application.catalog_indexer import (
    CatalogIndexerIterationOutcome,
    CatalogIndexerIterationResult,
    CatalogIndexerUseCaseError,
    CatalogIndexerUseCaseErrorCode,
)
from schemabridge.domain.catalog_inventory import (
    CatalogRefreshFailureCode,
    CatalogRefreshStatus,
)
from schemabridge.entrypoints.catalog.main import CatalogProcessRuntime


class _OneResultIndexer:
    def __init__(
        self,
        result: CatalogIndexerIterationResult,
        *,
        stop_event: Event | None = None,
    ) -> None:
        self.result = result
        self.stop_event = stop_event
        self.calls = 0

    def execute(self) -> CatalogIndexerIterationResult:
        self.calls += 1
        if self.stop_event is not None:
            self.stop_event.set()
        return self.result


class _CrashingIndexer:
    def execute(self) -> CatalogIndexerIterationResult:
        raise RuntimeError("must-not-appear-in-catalog-log")


class _ExpectedFailureIndexer:
    def execute(self) -> CatalogIndexerIterationResult:
        raise CatalogIndexerUseCaseError(
            CatalogIndexerUseCaseErrorCode.STORE_UNAVAILABLE,
            "database-route-and-secret-must-not-appear",
        )


class _LifecyclePool:
    def __init__(self) -> None:
        self.events: list[str] = []

    def open(self) -> None:
        self.events.append("opened")

    def close(self) -> None:
        self.events.append("closed")


def _idle() -> CatalogIndexerIterationResult:
    return CatalogIndexerIterationResult(
        outcome=CatalogIndexerIterationOutcome.IDLE,
    )


def _completed() -> CatalogIndexerIterationResult:
    return CatalogIndexerIterationResult(
        outcome=CatalogIndexerIterationOutcome.COMPLETED,
        status=CatalogRefreshStatus.COMPLETED,
        pages_processed=2,
        total_pages=2,
        asset_count=10,
        field_count=50,
    )


def _failed() -> CatalogIndexerIterationResult:
    return CatalogIndexerIterationResult(
        outcome=CatalogIndexerIterationOutcome.FAILED,
        status=CatalogRefreshStatus.FAILED,
        failure_code=CatalogRefreshFailureCode.SOURCE_UNAVAILABLE,
        pages_processed=1,
        total_pages=1,
        asset_count=4,
        field_count=20,
    )


def _stopped() -> CatalogIndexerIterationResult:
    return CatalogIndexerIterationResult(
        outcome=CatalogIndexerIterationOutcome.STOPPED,
        status=CatalogRefreshStatus.STAGING,
        pages_processed=1,
        total_pages=4,
        asset_count=50,
        field_count=300,
    )


@pytest.mark.parametrize("result", [_idle(), _completed(), _failed(), _stopped()])
def test_catalog_once_runs_exactly_one_serial_iteration(
    result: CatalogIndexerIterationResult,
) -> None:
    indexer = _OneResultIndexer(result)

    status = catalog_main.run_catalog_indexer(
        indexer,
        poll_interval_seconds=0.1,
        once=True,
    )

    assert status == 0
    assert indexer.calls == 1


def test_catalog_polling_stops_gracefully_after_current_iteration() -> None:
    stopping = Event()
    indexer = _OneResultIndexer(_idle(), stop_event=stopping)

    status = catalog_main.run_catalog_indexer(
        indexer,
        poll_interval_seconds=0.1,
        stop_event=stopping,
    )

    assert status == 0
    assert indexer.calls == 1


@pytest.mark.parametrize(
    ("indexer", "expected_code", "secret"),
    [
        (
            _ExpectedFailureIndexer(),
            CatalogIndexerUseCaseErrorCode.STORE_UNAVAILABLE.value,
            "database-route-and-secret-must-not-appear",
        ),
        (
            _CrashingIndexer(),
            "unexpected_catalog_error",
            "must-not-appear-in-catalog-log",
        ),
    ],
)
def test_catalog_iteration_logs_only_sanitized_failure_metadata(
    indexer: _ExpectedFailureIndexer | _CrashingIndexer,
    expected_code: str,
    secret: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger=catalog_main.__name__)

    status = catalog_main.run_catalog_indexer(
        indexer,
        poll_interval_seconds=0.1,
        once=True,
    )

    assert status == 1
    assert expected_code in caplog.text
    assert secret not in caplog.text


def test_catalog_command_opens_and_closes_pool_and_restores_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _LifecyclePool()
    indexer = _OneResultIndexer(_completed())
    runtime = CatalogProcessRuntime(
        indexer=indexer,
        log_level="INFO",
        poll_interval_seconds=0.1,
        control_pool=pool,
    )
    restored: list[dict[int, catalog_main._SignalHandler]] = []
    monkeypatch.setattr(catalog_main, "_install_signal_handlers", lambda _event: {})
    monkeypatch.setattr(
        catalog_main,
        "_restore_signal_handlers",
        lambda previous: restored.append(previous),
    )

    status = catalog_main.command(["--once"], runtime=runtime)

    assert status == 0
    assert indexer.calls == 1
    assert restored == [{}]
    assert pool.events == ["opened", "closed"]


def test_catalog_readiness_only_checks_lifecycle_resource() -> None:
    pool = _LifecyclePool()
    runtime = CatalogProcessRuntime(
        indexer=None,
        log_level="INFO",
        poll_interval_seconds=0.1,
        control_pool=pool,
    )

    status = catalog_main.command(["--probe-ready"], runtime=runtime)

    assert status == 0
    assert pool.events == ["opened", "closed"]


@pytest.mark.parametrize(
    ("argv", "runtime"),
    [
        (
            ["--probe-ready"],
            CatalogProcessRuntime(
                indexer=_OneResultIndexer(_idle()),
                log_level="INFO",
                poll_interval_seconds=0.1,
            ),
        ),
        (
            ["--once"],
            CatalogProcessRuntime(
                indexer=None,
                log_level="INFO",
                poll_interval_seconds=0.1,
            ),
        ),
    ],
)
def test_catalog_command_rejects_mismatched_runtime_and_closes_pool(
    argv: list[str],
    runtime: CatalogProcessRuntime,
) -> None:
    pool = _LifecyclePool()
    runtime = CatalogProcessRuntime(
        indexer=runtime.indexer,
        log_level=runtime.log_level,
        poll_interval_seconds=runtime.poll_interval_seconds,
        control_pool=pool,
    )

    with pytest.raises(ValueError):
        catalog_main.command(argv, runtime=runtime)

    assert pool.events == ["opened", "closed"]


def test_catalog_signal_handler_requests_graceful_stop_and_restores_previous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: list[tuple[int, object]] = []
    monkeypatch.setattr(signal, "getsignal", lambda _signum: signal.SIG_DFL)
    monkeypatch.setattr(
        signal,
        "signal",
        lambda signum, handler: installed.append((signum, handler)),
    )
    stopping = Event()

    previous = catalog_main._install_signal_handlers(stopping)
    handler = next(handler for signum, handler in installed if signum == signal.SIGTERM)
    assert callable(handler)
    handler(signal.SIGTERM, None)
    catalog_main._restore_signal_handlers(previous)

    assert stopping.is_set()
    assert installed[-2:] == [
        (signal.SIGINT, signal.SIG_DFL),
        (signal.SIGTERM, signal.SIG_DFL),
    ]


def test_catalog_modes_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit) as captured:
        catalog_main.command(["--once", "--probe-ready"])

    assert captured.value.code == 2


def test_catalog_main_omits_startup_exception_message(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger=catalog_main.__name__)
    monkeypatch.setattr(
        catalog_main,
        "command",
        lambda: (_ for _ in ()).throw(RuntimeError("database-url-must-not-appear-in-startup-log")),
    )

    with pytest.raises(SystemExit) as captured:
        catalog_main.main()

    assert captured.value.code == 1
    assert "RuntimeError" in caplog.text
    assert "database-url-must-not-appear-in-startup-log" not in caplog.text


def test_catalog_runtime_repr_excludes_indexer_and_pool() -> None:
    runtime = CatalogProcessRuntime(
        indexer=_CrashingIndexer(),
        log_level="INFO",
        poll_interval_seconds=0.1,
        control_pool=_LifecyclePool(),
    )

    rendered = repr(runtime)

    assert "indexer=" not in rendered
    assert "control_pool=" not in rendered
