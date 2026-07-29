from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import NoReturn, cast

import pytest

import schemabridge.bootstrap as bootstrap
import schemabridge.entrypoints.catalog.main as catalog_main
import schemabridge.entrypoints.http.app as http_app
import schemabridge.entrypoints.http.main as http_main
import schemabridge.entrypoints.observer.main as observer_main
import schemabridge.entrypoints.semantic_profile_worker.main as profile_main
import schemabridge.entrypoints.semantic_reconciler.main as reconciler_main
import schemabridge.entrypoints.streamlit.app as streamlit_main
import schemabridge.entrypoints.worker.main as worker_main


def _startup_failure() -> NoReturn:
    raise RuntimeError("postgresql://reader:credential-secret@private/source")


def _patch_failure(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    service: str,
) -> None:
    if service == "api":
        monkeypatch.setattr(http_main, "build_api_process_runtime", _startup_failure)
    elif service == "observer":
        monkeypatch.setattr(bootstrap, "build_observer_process_runtime", _startup_failure)
    else:
        monkeypatch.setattr(module, "command", _startup_failure)


@pytest.mark.parametrize(
    ("module", "service"),
    [
        (http_main, "api"),
        (observer_main, "observer"),
        (worker_main, "worker"),
        (catalog_main, "catalog"),
        (profile_main, "profile"),
        (reconciler_main, "reconciler"),
    ],
)
def test_every_process_startup_failure_is_two_sanitized_json_events(
    module: ModuleType,
    service: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SCHEMABRIDGE_ENVIRONMENT", "production")
    monkeypatch.setenv("SCHEMABRIDGE_LOG_LEVEL", "INFO")
    _patch_failure(monkeypatch, module, service)
    main = cast(Callable[[], None], module.main)

    with pytest.raises(SystemExit) as raised:
        main()

    assert raised.value.code == 1
    raw = capsys.readouterr().err
    events = [json.loads(line) for line in raw.splitlines()]
    assert [(event["event"], event["outcome"]) for event in events] == [
        ("service.health", "started"),
        ("service.health", "failed"),
    ]
    assert all(event["schema_version"] == "schemabridge.telemetry.v1" for event in events)
    assert all(event["service"] == service for event in events)
    assert all(event["environment"] == "production" for event in events)
    assert events[-1]["error_code"] == "internal_failure"
    assert all(
        set(event).isdisjoint({"message", "exception", "traceback", "stack"}) for event in events
    )
    for forbidden in (
        "credential-secret",
        "postgresql://",
        "RuntimeError",
        "Traceback",
    ):
        assert forbidden not in raw


def test_runtime_entrypoints_have_no_text_basic_config_or_uvicorn_log_config() -> None:
    modules = (
        http_app,
        http_main,
        observer_main,
        worker_main,
        catalog_main,
        profile_main,
        reconciler_main,
        streamlit_main,
    )
    sources = {
        module.__name__: Path(module.__file__).read_text(encoding="utf-8")
        for module in modules
        if module.__file__ is not None
    }

    assert all("logging.basicConfig" not in source for source in sources.values())
    assert all("schemabridge.adapters" not in source for source in sources.values())
    assert all("schemabridge.config" not in source for source in sources.values())
    assert "log_config=None" in sources[http_main.__name__]
    assert "log_config=None" in sources[observer_main.__name__]
    assert 'ensure_runtime_logging(service="web")' in sources[streamlit_main.__name__]
    for module in (
        http_main,
        observer_main,
        worker_main,
        catalog_main,
        profile_main,
        reconciler_main,
    ):
        assert 'configure_runtime_logging(service="' in sources[module.__name__]


def test_fatal_startup_failure_is_not_suppressed_by_a_critical_threshold(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SCHEMABRIDGE_ENVIRONMENT", "production")
    monkeypatch.setenv("SCHEMABRIDGE_LOG_LEVEL", "CRITICAL")
    monkeypatch.setattr(worker_main, "command", _startup_failure)

    with pytest.raises(SystemExit) as raised:
        worker_main.main()

    assert raised.value.code == 1
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert len(events) == 1
    assert events[0]["event"] == "service.health"
    assert events[0]["outcome"] == "failed"
    assert events[0]["error_code"] == "internal_failure"
