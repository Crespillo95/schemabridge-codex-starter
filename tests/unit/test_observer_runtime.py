from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import schemabridge.bootstrap as bootstrap
import schemabridge.entrypoints.observer.main as observer_main
from schemabridge.bootstrap import ObserverProcessRuntime
from schemabridge.config import Settings

_OBSERVER_DSN = (
    "postgresql://schemabridge_observer:synthetic-observer-password@control.example.test/control"
)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        OPENAI_API_KEY=None,
        DATAHUB_GMS_TOKEN=None,
        SCHEMABRIDGE_COMPONENT="observer",
        SCHEMABRIDGE_CONTROL_PLANE_MODE="postgres",
        SCHEMABRIDGE_CONTROL_OBSERVER_DATABASE_URL=_OBSERVER_DSN,
    )


@dataclass
class _Pool:
    events: list[str] = field(default_factory=list)

    def open(self) -> None:
        self.events.append("open")

    def close(self) -> None:
        self.events.append("close")


def test_observer_composition_uses_only_observer_pool_and_schema_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    pool_requests: list[str] = []
    readiness_requests: list[tuple[str, object]] = []

    def build_pool(*, credential_kind: str, settings: Settings) -> _Pool:
        assert settings.runtime_component == "observer"
        pool_requests.append(credential_kind)
        return pool

    def require_current(**kwargs: object) -> object:
        readiness_requests.append(
            (
                str(kwargs["credential_kind"]),
                kwargs["connection_provider"],
            )
        )
        return object()

    monkeypatch.setattr(bootstrap, "build_control_plane_pool", build_pool)
    monkeypatch.setattr(bootstrap, "require_current_control_plane_schema", require_current)

    runtime = bootstrap.build_observer_process_runtime(settings=_settings())

    assert runtime.bind_host == "127.0.0.1"
    assert runtime.port == 9464
    assert runtime.limit_concurrency == 20
    assert runtime.graceful_shutdown_seconds == 20
    assert pool_requests == ["observer"]
    assert _OBSERVER_DSN not in repr(runtime)

    with TestClient(runtime.application) as client:
        assert pool.events == ["open"]
        response = client.get("/health/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ready"}

    assert pool.events == ["open", "close"]
    assert readiness_requests == [("observer", pool)]


def test_observer_control_pool_is_bound_to_observer_application_name_and_secret_dsn() -> None:
    pool = bootstrap.build_control_plane_pool(
        credential_kind="observer",
        settings=_settings(),
    )

    assert pool.settings.application_name == "schemabridge-control-observer"
    assert pool.settings.dsn == _OBSERVER_DSN
    assert _OBSERVER_DSN not in repr(pool.settings)


def test_observer_composition_rejects_every_other_component() -> None:
    with pytest.raises(Exception, match="observer composition requires"):
        bootstrap.build_observer_process_runtime(settings=Settings(_env_file=None))


def test_observer_entrypoint_passes_only_bounded_server_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = FastAPI()
    runtime = ObserverProcessRuntime(
        application=application,
        log_level="INFO",
        bind_host="127.0.0.1",
        port=9464,
        limit_concurrency=17,
        graceful_shutdown_seconds=23,
    )
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(bootstrap, "build_observer_process_runtime", lambda: runtime)

    def run(_app: FastAPI, **kwargs: object) -> None:
        assert _app is application
        calls.append(dict(kwargs))

    monkeypatch.setattr("uvicorn.run", run)

    observer_main.main()

    assert calls == [
        {
            "host": "127.0.0.1",
            "port": 9464,
            "workers": 1,
            "limit_concurrency": 17,
            "timeout_graceful_shutdown": 23,
            "access_log": False,
            "proxy_headers": False,
            "server_header": False,
            "date_header": False,
            "log_config": None,
        }
    ]


def test_observer_startup_failure_logs_only_closed_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail() -> ObserverProcessRuntime:
        raise RuntimeError("postgresql://observer:secret@private/control")

    monkeypatch.setattr(bootstrap, "build_observer_process_runtime", fail)
    monkeypatch.setenv("SCHEMABRIDGE_LOG_LEVEL", "INFO")

    with pytest.raises(SystemExit) as raised:
        observer_main.main()

    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert raised.value.code == 1
    assert events[-1]["service"] == "observer"
    assert events[-1]["event"] == "service.health"
    assert events[-1]["outcome"] == "failed"
    assert events[-1]["error_code"] == "internal_failure"
    serialized = json.dumps(events)
    assert "RuntimeError" not in serialized
    assert "postgresql://" not in serialized
    assert "secret" not in serialized
