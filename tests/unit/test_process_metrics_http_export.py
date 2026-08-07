from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field
from threading import Event, Lock, Thread
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

from schemabridge.adapters.observability.http_export import (
    MetricsHttpExporter,
    MetricsHttpExporterError,
)


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        port = candidate.getsockname()[1]
    assert isinstance(port, int)
    return port


def _request(
    port: int,
    *,
    path: str = "/metrics",
    method: str = "GET",
) -> tuple[int, dict[str, str], bytes]:
    request = Request(
        f"http://127.0.0.1:{port}{path}",
        method=method,
    )
    try:
        with urlopen(request, timeout=2) as response:
            return response.status, dict(response.headers.items()), response.read()
    except HTTPError as error:
        return error.code, dict(error.headers.items()), error.read()


@dataclass
class _Renderer:
    document: str = (
        "# HELP schemabridge_process_ready process readiness\n"
        "# TYPE schemabridge_process_ready gauge\n"
        'schemabridge_process_ready{service="worker"} 1.0\n'
        "# EOF\n"
    )
    failure: Exception | None = None
    calls: int = 0

    def render_openmetrics(self) -> str:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.document


@dataclass
class _BarrierRenderer:
    calls: int = 0
    first_entered: Event = field(default_factory=Event)
    release_first: Event = field(default_factory=Event)
    second_entered: Event = field(default_factory=Event)
    lock: Lock = field(default_factory=Lock)

    def render_openmetrics(self) -> str:
        with self.lock:
            self.calls += 1
            call = self.calls
        if call == 1:
            self.first_entered.set()
            if not self.release_first.wait(timeout=2):
                raise RuntimeError("test exporter barrier timed out")
        else:
            self.second_entered.set()
        return (
            "# HELP exporter_call bounded call\n"
            "# TYPE exporter_call gauge\n"
            f"exporter_call {call}\n"
            "# EOF\n"
        )


def test_exporter_binds_serves_only_metrics_and_closes_idempotently() -> None:
    port = _available_port()
    renderer = _Renderer()
    exporter = MetricsHttpExporter(
        bind_host="127.0.0.1",
        port=port,
        renderer=renderer,
    )

    exporter.open()
    exporter.open()
    status, headers, body = _request(port)
    missing_status, _, missing_body = _request(port, path="/details")
    method_status, _, method_body = _request(port, method="POST")

    assert exporter.is_open
    assert status == 200
    assert headers["Content-Type"].startswith("application/openmetrics-text")
    assert headers["Cache-Control"] == "no-store"
    assert "Server" not in headers
    assert "Date" not in headers
    assert body == renderer.document.encode()
    assert renderer.calls == 1
    assert (missing_status, missing_body) == (404, b'{"status":"not_found"}\n')
    assert (method_status, method_body) == (
        405,
        b'{"status":"method_not_allowed"}\n',
    )

    exporter.close()
    exporter.close()
    assert not exporter.is_open
    with pytest.raises((URLError, OSError)):
        _request(port)


@pytest.mark.parametrize(
    "renderer",
    [
        _Renderer(document="x" * 1_025),
        _Renderer(document="not-openmetrics\n"),
        _Renderer(failure=RuntimeError("credential=must-not-leak")),
    ],
)
def test_exporter_rejects_invalid_or_oversized_documents_without_details(
    renderer: _Renderer,
) -> None:
    port = _available_port()
    exporter = MetricsHttpExporter(
        bind_host="127.0.0.1",
        port=port,
        renderer=renderer,
        max_response_bytes=1_024,
    )
    exporter.open()
    try:
        status, _, body = _request(port)
    finally:
        exporter.close()

    assert status == 503
    assert body == b'{"status":"unavailable"}\n'
    assert b"credential" not in body


def test_exporter_serializes_hostile_concurrent_scrapes() -> None:
    port = _available_port()
    renderer = _BarrierRenderer()
    exporter = MetricsHttpExporter(
        bind_host="127.0.0.1",
        port=port,
        renderer=renderer,
    )
    responses: list[tuple[int, dict[str, str], bytes]] = []
    exporter.open()
    first = Thread(target=lambda: responses.append(_request(port)), daemon=True)
    second = Thread(target=lambda: responses.append(_request(port)), daemon=True)
    try:
        first.start()
        assert renderer.first_entered.wait(timeout=1)
        second.start()
        assert not renderer.second_entered.wait(timeout=0.25)
        renderer.release_first.set()
        first.join(timeout=2)
        second.join(timeout=2)
    finally:
        renderer.release_first.set()
        exporter.close()

    assert not first.is_alive()
    assert not second.is_alive()
    assert renderer.calls == 2
    assert sorted(body for status, _, body in responses if status == 200) == [
        (
            b"# HELP exporter_call bounded call\n"
            b"# TYPE exporter_call gauge\n"
            b"exporter_call 1\n"
            b"# EOF\n"
        ),
        (
            b"# HELP exporter_call bounded call\n"
            b"# TYPE exporter_call gauge\n"
            b"exporter_call 2\n"
            b"# EOF\n"
        ),
    ]


def test_slow_client_cannot_hold_the_single_request_server_indefinitely(
    capsys: pytest.CaptureFixture[str],
) -> None:
    port = _available_port()
    exporter = MetricsHttpExporter("127.0.0.1", port, _Renderer())
    exporter.open()
    slow_client = socket.create_connection(("127.0.0.1", port), timeout=1)
    try:
        slow_client.sendall(b"GET /metrics HTTP/1.1\r\nHost:")
        started = time.monotonic()
        status, _, _ = _request(port)
        elapsed = time.monotonic() - started
    finally:
        slow_client.close()
        exporter.close()

    assert status == 200
    assert elapsed < 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_exporter_bind_collision_is_sanitized_and_recoverable() -> None:
    port = _available_port()
    first = MetricsHttpExporter("127.0.0.1", port, _Renderer())
    second = MetricsHttpExporter("127.0.0.1", port, _Renderer())
    first.open()
    try:
        with pytest.raises(MetricsHttpExporterError) as raised:
            second.open()
    finally:
        second.close()
        first.close()

    assert str(raised.value) == "metrics exporter failed to start"
    assert "127.0.0.1" not in str(raised.value)


@pytest.mark.parametrize(
    ("host", "port", "max_bytes"),
    [
        ("metrics.internal", 9464, 65_536),
        ("127.0.0.1", 1_023, 65_536),
        ("127.0.0.1", 9464, 1_023),
    ],
)
def test_exporter_rejects_unbounded_or_resolving_configuration(
    host: str,
    port: int,
    max_bytes: int,
) -> None:
    with pytest.raises(ValueError, match="metrics exporter"):
        MetricsHttpExporter(
            bind_host=host,
            port=port,
            renderer=_Renderer(),
            max_response_bytes=max_bytes,
        )
