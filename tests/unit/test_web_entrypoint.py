from __future__ import annotations

from collections.abc import Callable

import pytest

import schemabridge.entrypoints.streamlit.main as web_main
from schemabridge.bootstrap import WebProcessRuntime


class _Readiness:
    def __init__(self, failure: Exception | None = None) -> None:
        self.calls = 0
        self.failure = failure

    def __call__(self) -> None:
        self.calls += 1
        if self.failure is not None:
            raise self.failure


def _runtime(
    readiness: Callable[[], None],
    listener_readiness: Callable[[], None] | None = None,
) -> WebProcessRuntime:
    return WebProcessRuntime(
        readiness_check=readiness,
        listener_readiness_check=listener_readiness or (lambda: None),
        streamlit_argv=(
            "streamlit",
            "run",
            "streamlit_app.py",
            "--server.address=0.0.0.0",
            "--server.port=7860",
            "--server.headless=true",
            "--server.fileWatcherType=none",
            "--browser.gatherUsageStats=false",
        ),
    )


def test_readiness_probe_runs_preflight_without_starting_streamlit() -> None:
    readiness = _Readiness()
    listener_readiness = _Readiness()
    executions: list[tuple[str, list[str]]] = []

    status = web_main.command(
        ["--probe-ready"],
        runtime=_runtime(readiness, listener_readiness),
        exec_process=lambda executable, argv: executions.append((executable, argv)),
    )

    assert status == 0
    assert readiness.calls == 1
    assert listener_readiness.calls == 1
    assert executions == []


def test_web_start_preflights_before_the_fixed_streamlit_exec(
    capsys: pytest.CaptureFixture[str],
) -> None:
    readiness = _Readiness()
    listener_readiness = _Readiness()
    executions: list[tuple[str, list[str]]] = []

    status = web_main.command(
        [],
        runtime=_runtime(readiness, listener_readiness),
        exec_process=lambda executable, argv: executions.append((executable, argv)),
    )

    assert status == 1
    assert readiness.calls == 1
    assert listener_readiness.calls == 0
    assert executions == [
        (
            "streamlit",
            [
                "streamlit",
                "run",
                "streamlit_app.py",
                "--server.address=0.0.0.0",
                "--server.port=7860",
                "--server.headless=true",
                "--server.fileWatcherType=none",
                "--browser.gatherUsageStats=false",
            ],
        )
    ]
    assert capsys.readouterr().err == (
        "web_runtime_unavailable: configuration, authentication, schema, or process launch failed\n"
    )


def test_web_preflight_failure_is_sanitized_and_never_executes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_marker = "must-not-echo-private-auth-value"
    readiness = _Readiness(RuntimeError(private_marker))
    listener_readiness = _Readiness()
    executions: list[tuple[str, list[str]]] = []

    status = web_main.command(
        [],
        runtime=_runtime(readiness, listener_readiness),
        exec_process=lambda executable, argv: executions.append((executable, argv)),
    )

    captured = capsys.readouterr()
    assert status == 1
    assert readiness.calls == 1
    assert listener_readiness.calls == 0
    assert executions == []
    assert private_marker not in captured.err
    assert captured.out == ""
    assert captured.err.startswith("web_runtime_unavailable:")


def test_each_probe_rebuilds_settings_and_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builds = 0
    readiness = _Readiness()
    listener_readiness = _Readiness()

    def build_runtime() -> WebProcessRuntime:
        nonlocal builds
        builds += 1
        return _runtime(readiness, listener_readiness)

    monkeypatch.setattr(web_main, "build_web_process_runtime", build_runtime)

    assert web_main.command(["--probe-ready"]) == 0
    assert web_main.command(["--probe-ready"]) == 0
    assert builds == 2
    assert readiness.calls == 2
    assert listener_readiness.calls == 2


def test_probe_listener_failure_is_sanitized_after_successful_preflight(
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_marker = "must-not-echo-loopback-failure"
    readiness = _Readiness()
    listener_readiness = _Readiness(ConnectionRefusedError(private_marker))

    status = web_main.command(
        ["--probe-ready"],
        runtime=_runtime(readiness, listener_readiness),
    )

    captured = capsys.readouterr()
    assert status == 1
    assert readiness.calls == 1
    assert listener_readiness.calls == 1
    assert private_marker not in captured.err
    assert captured.err.startswith("web_runtime_unavailable:")


def test_exec_failure_is_sanitized(
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_marker = "must-not-echo-executable-path"

    def fail_exec(_executable: str, _argv: list[str]) -> object:
        raise OSError(private_marker)

    assert web_main.command([], runtime=_runtime(_Readiness()), exec_process=fail_exec) == 1
    captured = capsys.readouterr()
    assert private_marker not in captured.err
    assert captured.err.startswith("web_runtime_unavailable:")
