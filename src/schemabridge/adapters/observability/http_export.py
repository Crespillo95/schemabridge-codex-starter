"""Bounded internal HTTP exposition for one process-local metrics registry."""

from __future__ import annotations

import ipaddress
import socket
import threading
from contextlib import suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Protocol

MAX_METRICS_DOCUMENT_BYTES = 65_536
_METRICS_PATH = "/metrics"
_CLIENT_IO_TIMEOUT_SECONDS = 1.0
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
}


class MetricsDocumentRenderer(Protocol):
    """Render one already bounded process-local OpenMetrics document."""

    def render_openmetrics(self) -> str:
        """Return an OpenMetrics 1.0 document without external I/O."""


class MetricsHttpExporterError(RuntimeError):
    """The internal metrics endpoint could not start or stop safely."""


class _SingleRequestHTTPServer(HTTPServer):
    request_queue_size = 1
    allow_reuse_address = False

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        request, client_address = super().get_request()
        request.settimeout(_CLIENT_IO_TIMEOUT_SECONDS)
        return request, client_address

    def handle_error(
        self,
        request: object,
        client_address: object,
    ) -> None:
        del request, client_address


def _handler_type(
    renderer: MetricsDocumentRenderer,
    *,
    max_response_bytes: int,
) -> type[BaseHTTPRequestHandler]:
    class MetricsHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            if self.path != _METRICS_PATH:
                self._send_json(404, b'{"status":"not_found"}\n')
                return
            try:
                document = renderer.render_openmetrics()
                if type(document) is not str or not document.endswith("# EOF\n"):
                    raise ValueError
                encoded = document.encode("utf-8")
                if not encoded or len(encoded) > max_response_bytes or b"\x00" in encoded:
                    raise ValueError
            except Exception:
                self._send_json(503, b'{"status":"unavailable"}\n')
                return
            self._send(
                200,
                encoded,
                content_type="application/openmetrics-text; version=1.0.0; charset=utf-8",
            )

        def do_HEAD(self) -> None:
            self._method_not_allowed()

        def do_POST(self) -> None:
            self._method_not_allowed()

        def do_PUT(self) -> None:
            self._method_not_allowed()

        def do_PATCH(self) -> None:
            self._method_not_allowed()

        def do_DELETE(self) -> None:
            self._method_not_allowed()

        def do_OPTIONS(self) -> None:
            self._method_not_allowed()

        def do_TRACE(self) -> None:
            self._method_not_allowed()

        def do_CONNECT(self) -> None:
            self._method_not_allowed()

        def send_error(
            self,
            code: int,
            message: str | None = None,
            explain: str | None = None,
        ) -> None:
            del message, explain
            status = code if code in {400, 404, 405, 413, 414} else 400
            self._send_json(status, b'{"status":"invalid_request"}\n')

        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def _method_not_allowed(self) -> None:
            self._send_json(405, b'{"status":"method_not_allowed"}\n')

        def _send_json(self, status: int, body: bytes) -> None:
            self._send(
                status,
                body,
                content_type="application/json; charset=utf-8",
            )

        def _send(
            self,
            status: int,
            body: bytes,
            *,
            content_type: str,
        ) -> None:
            self.send_response_only(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            for name, value in _SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            self.close_connection = True
            if self.command != "HEAD":
                self.wfile.write(body)

    return MetricsHandler


@dataclass(slots=True)
class MetricsHttpExporter:
    """Serve one process registry on a single-request internal HTTP endpoint."""

    bind_host: str
    port: int
    renderer: MetricsDocumentRenderer = field(repr=False)
    max_response_bytes: int = MAX_METRICS_DOCUMENT_BYTES
    _server: _SingleRequestHTTPServer | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _thread: threading.Thread | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _state_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        try:
            ipaddress.ip_address(self.bind_host)
        except ValueError:
            raise ValueError("metrics exporter bind host must be an explicit IP") from None
        if (
            type(self.port) is not int
            or not 1_024 <= self.port <= 65_535
            or type(self.max_response_bytes) is not int
            or not 1_024 <= self.max_response_bytes <= MAX_METRICS_DOCUMENT_BYTES
            or not callable(getattr(self.renderer, "render_openmetrics", None))
        ):
            raise ValueError("metrics exporter configuration is invalid")

    @property
    def is_open(self) -> bool:
        """Return whether the bound server is currently owned by this resource."""

        with self._state_lock:
            return self._server is not None

    def open(self) -> None:
        """Bind synchronously, then start exactly one bounded serving thread."""

        with self._state_lock:
            if self._server is not None:
                return
            server: _SingleRequestHTTPServer | None = None
            try:
                server = _SingleRequestHTTPServer(
                    (self.bind_host, self.port),
                    _handler_type(
                        self.renderer,
                        max_response_bytes=self.max_response_bytes,
                    ),
                )
                thread = threading.Thread(
                    target=server.serve_forever,
                    kwargs={"poll_interval": 0.05},
                    name="schemabridge-metrics-export",
                    daemon=True,
                )
                thread.start()
            except Exception:
                if server is not None:
                    server.server_close()
                raise MetricsHttpExporterError("metrics exporter failed to start") from None
            self._server = server
            self._thread = thread

    def close(self) -> None:
        """Stop serving and release the listening socket idempotently."""

        with self._state_lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
        if server is None:
            return
        failed = False
        try:
            if thread is not None and thread.is_alive():
                server.shutdown()
            server.server_close()
            if thread is not None:
                thread.join(timeout=2)
                failed = thread.is_alive()
        except Exception:
            failed = True
            with suppress(Exception):
                server.server_close()
        if failed:
            raise MetricsHttpExporterError("metrics exporter failed to stop") from None


__all__ = [
    "MAX_METRICS_DOCUMENT_BYTES",
    "MetricsDocumentRenderer",
    "MetricsHttpExporter",
    "MetricsHttpExporterError",
]
