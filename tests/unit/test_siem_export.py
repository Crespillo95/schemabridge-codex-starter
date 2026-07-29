from __future__ import annotations

import json
import ssl
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.request import HTTPSHandler, ProxyHandler, Request

import pytest

from schemabridge.adapters.observability.metrics import OpenMetricsRegistry
from schemabridge.adapters.observability.siem import (
    MAX_SIEM_EVENT_BYTES,
    SiemExporter,
    SiemExportError,
    SiemTransportError,
    UrllibSiemTransport,
)
from schemabridge.adapters.observability.structured_logging import (
    LogOutcome,
    LogSeverity,
    StructuredEvent,
    render_structured_event,
)

ENDPOINT = "https://siem.example.test/private-ingest"
TOKENS = (
    "header.payload.signature-one",
    "header.payload.signature-two",
    "header.payload.signature-three",
)


class FakeIdentity:
    def __init__(self, tokens: tuple[str, ...] = TOKENS) -> None:
        self._tokens = tokens
        self.reads = 0

    def read(self) -> str:
        token = self._tokens[min(self.reads, len(self._tokens) - 1)]
        self.reads += 1
        return token


class FakeTransport:
    def __init__(self, *, failures: int = 0) -> None:
        self._failures = failures
        self.calls: list[dict[str, object]] = []

    def send(
        self,
        *,
        endpoint: str,
        ndjson: bytes,
        bearer_token: str,
        ca_bundle: Path,
        client_certificate: Path,
        client_key: Path,
        timeout_seconds: float,
    ) -> None:
        self.calls.append(
            {
                "endpoint": endpoint,
                "ndjson": ndjson,
                "bearer_token": bearer_token,
                "ca_bundle": ca_bundle,
                "client_certificate": client_certificate,
                "client_key": client_key,
                "timeout_seconds": timeout_seconds,
            }
        )
        if len(self.calls) <= self._failures:
            raise RuntimeError(f"provider leaked {endpoint} {bearer_token} {client_key}")


def _tls_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    ca_bundle = tmp_path / "private-ca.pem"
    certificate = tmp_path / "private-client.crt"
    key = tmp_path / "private-client.key"
    for path in (ca_bundle, certificate, key):
        path.write_text("synthetic-test-material\n", encoding="utf-8")
    key.chmod(0o600)
    return ca_bundle, certificate, key


def _event(correlation_id: str = "req-public-1") -> StructuredEvent:
    return StructuredEvent(
        occurred_at=datetime(2026, 7, 29, 12, 0, tzinfo=UTC),
        severity=LogSeverity.INFO,
        service="observer",
        environment="production",
        event="telemetry.export",
        outcome=LogOutcome.SUCCEEDED,
        duration_ms=5,
        correlation_id=correlation_id,
        counts={"requests_completed": 1},
    )


def _exporter(
    tmp_path: Path,
    *,
    identity: FakeIdentity | None = None,
    transport: FakeTransport | None = None,
    metrics: OpenMetricsRegistry | None = None,
    max_buffered_events: int = 10,
    batch_size: int = 2,
    max_retries: int = 2,
    sleeper: Any = lambda _delay: None,
) -> SiemExporter:
    ca_bundle, certificate, key = _tls_paths(tmp_path)
    return SiemExporter(
        endpoint=ENDPOINT,
        identity=identity or FakeIdentity(),
        ca_bundle=ca_bundle,
        client_certificate=certificate,
        client_key=key,
        metrics=metrics or OpenMetricsRegistry(),
        max_buffered_events=max_buffered_events,
        batch_size=batch_size,
        max_retries=max_retries,
        transport=transport or FakeTransport(),
        sleeper=sleeper,
    )


def _assert_counter(
    metrics: OpenMetricsRegistry,
    *,
    outcome: str,
    value: float,
) -> None:
    assert (
        f'schemabridge_telemetry_exports_total{{outcome="{outcome}"}} {value}\n' in metrics.render()
    )


def test_success_sends_one_bounded_ndjson_batch_and_removes_only_delivered_events(
    tmp_path: Path,
) -> None:
    identity = FakeIdentity()
    transport = FakeTransport()
    metrics = OpenMetricsRegistry()
    exporter = _exporter(
        tmp_path,
        identity=identity,
        transport=transport,
        metrics=metrics,
    )
    exporter.enqueue(_event("req-public-1"))
    exporter.enqueue(_event("req-public-2"))
    exporter.enqueue(_event("req-public-3"))

    delivered = exporter.flush()

    assert delivered == 2
    assert exporter.pending_events == 1
    assert identity.reads == 1
    assert len(transport.calls) == 1
    body = transport.calls[0]["ndjson"]
    assert isinstance(body, bytes)
    lines = body.splitlines(keepends=True)
    assert len(lines) == 2
    assert all(line.endswith(b"\n") and len(line) <= MAX_SIEM_EVENT_BYTES for line in lines)
    assert [json.loads(line)["correlation_id"] for line in lines] == [
        "req-public-1",
        "req-public-2",
    ]
    _assert_counter(metrics, outcome="success", value=1.0)


def test_retry_reads_fresh_identity_for_every_send_and_never_exceeds_the_bound(
    tmp_path: Path,
) -> None:
    identity = FakeIdentity()
    transport = FakeTransport(failures=2)
    metrics = OpenMetricsRegistry()
    delays: list[float] = []
    exporter = _exporter(
        tmp_path,
        identity=identity,
        transport=transport,
        metrics=metrics,
        max_retries=2,
        sleeper=delays.append,
    )
    exporter.enqueue(_event())

    assert exporter.flush() == 1

    assert identity.reads == 3
    assert [call["bearer_token"] for call in transport.calls] == list(TOKENS)
    assert delays == [0.1, 0.2]
    assert exporter.pending_events == 0
    _assert_counter(metrics, outcome="success", value=1.0)


def test_retry_exhaustion_retains_the_batch_signals_error_and_sanitizes_failure(
    tmp_path: Path,
) -> None:
    identity = FakeIdentity()
    transport = FakeTransport(failures=99)
    metrics = OpenMetricsRegistry()
    exporter = _exporter(
        tmp_path,
        identity=identity,
        transport=transport,
        metrics=metrics,
        max_retries=2,
    )
    exporter.enqueue(_event())

    with pytest.raises(SiemExportError) as raised:
        exporter.flush()

    assert str(raised.value) == "siem telemetry export failed"
    for sensitive in (ENDPOINT, *TOKENS, "private-client.key", "req-public-1"):
        assert sensitive not in str(raised.value)
    assert identity.reads == 3
    assert len(transport.calls) == 3
    assert exporter.pending_events == 1
    _assert_counter(metrics, outcome="error", value=1.0)


def test_buffer_overflow_rejects_new_event_and_increments_dropped_metric(
    tmp_path: Path,
) -> None:
    metrics = OpenMetricsRegistry()
    exporter = _exporter(
        tmp_path,
        metrics=metrics,
        max_buffered_events=1,
        batch_size=1,
    )
    exporter.enqueue(_event("req-public-kept"))

    with pytest.raises(SiemExportError, match="siem telemetry event rejected"):
        exporter.enqueue(_event("req-public-dropped"))

    assert exporter.pending_events == 1
    _assert_counter(metrics, outcome="dropped", value=1.0)


def test_exporter_accepts_no_string_or_arbitrary_payload_and_hides_configuration(
    tmp_path: Path,
) -> None:
    identity = FakeIdentity(("identity-secret-token-value",))
    exporter = _exporter(tmp_path, identity=identity)

    for hostile in (
        "password=hunter2",
        {"token": "identity-secret-token-value"},
        b'{"sql":"select secret"}',
    ):
        with pytest.raises(SiemExportError) as raised:
            exporter.enqueue(hostile)  # type: ignore[arg-type]
        assert str(raised.value) == "siem telemetry event rejected"
        assert "hunter2" not in str(raised.value)
        assert "identity-secret-token-value" not in str(raised.value)

    rendered = repr(exporter)
    for sensitive in (
        ENDPOINT,
        "identity-secret-token-value",
        "private-ca.pem",
        "private-client.crt",
        "private-client.key",
    ):
        assert sensitive not in rendered


@pytest.mark.parametrize(
    "override",
    [
        {"max_buffered_events": 10_001},
        {"batch_size": 101},
        {"max_retries": 6},
        {"timeout_seconds": 15.1},
        {"retry_backoff_seconds": 1.1},
    ],
)
def test_configuration_bounds_fail_closed_without_echoing_paths(
    tmp_path: Path,
    override: dict[str, object],
) -> None:
    ca_bundle, certificate, key = _tls_paths(tmp_path)
    values: dict[str, object] = {
        "endpoint": ENDPOINT,
        "identity": FakeIdentity(),
        "ca_bundle": ca_bundle,
        "client_certificate": certificate,
        "client_key": key,
        "metrics": OpenMetricsRegistry(),
        **override,
    }

    with pytest.raises(ValueError) as raised:
        SiemExporter(**values)  # type: ignore[arg-type]

    assert str(raised.value) == "siem export configuration rejected"
    assert ENDPOINT not in str(raised.value)
    assert str(key) not in str(raised.value)


def test_private_key_must_be_owner_only_and_cannot_be_a_symlink(tmp_path: Path) -> None:
    ca_bundle, certificate, key = _tls_paths(tmp_path)
    key.chmod(0o640)
    with pytest.raises(ValueError, match="siem export configuration rejected"):
        SiemExporter(
            endpoint=ENDPOINT,
            identity=FakeIdentity(),
            ca_bundle=ca_bundle,
            client_certificate=certificate,
            client_key=key,
            metrics=OpenMetricsRegistry(),
        )

    key.chmod(0o600)
    link = (tmp_path / "linked-private-client.key").resolve()
    link.symlink_to(key)
    with pytest.raises(ValueError, match="siem export configuration rejected"):
        SiemExporter(
            endpoint=ENDPOINT,
            identity=FakeIdentity(),
            ca_bundle=ca_bundle,
            client_certificate=certificate,
            client_key=link,
            metrics=OpenMetricsRegistry(),
        )


class _FakeTlsContext:
    def __init__(self) -> None:
        self.minimum_version: ssl.TLSVersion | None = None
        self.cert_chain: tuple[str, str] | None = None

    def load_cert_chain(self, *, certfile: str, keyfile: str) -> None:
        self.cert_chain = (certfile, keyfile)


class _FakeResponse:
    status = 202

    def __init__(self) -> None:
        self._reads = 0

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_arguments: object) -> None:
        return None

    def read(self, _size: int) -> bytes:
        self._reads += 1
        return b"{}" if self._reads == 1 else b""


class _FakeOpener:
    def __init__(self) -> None:
        self.request: Request | None = None
        self.timeout: float | None = None

    def open(self, request: Request, *, timeout: float) -> _FakeResponse:
        self.request = request
        self.timeout = timeout
        return _FakeResponse()


def test_urllib_transport_enforces_https_no_proxy_no_redirect_tls12_mtls_and_bearer(
    tmp_path: Path,
) -> None:
    ca_bundle, certificate, key = _tls_paths(tmp_path)
    context = _FakeTlsContext()
    opener = _FakeOpener()
    captured_handlers: tuple[object, ...] = ()

    def fake_build_opener(*handlers: object) -> _FakeOpener:
        nonlocal captured_handlers
        captured_handlers = handlers
        return opener

    ndjson = f"{render_structured_event(_event())}\n".encode()
    with (
        patch(
            "schemabridge.adapters.observability.siem.ssl.create_default_context",
            return_value=context,
        ) as create_context,
        patch(
            "schemabridge.adapters.observability.siem.build_opener",
            side_effect=fake_build_opener,
        ),
    ):
        UrllibSiemTransport().send(
            endpoint=ENDPOINT,
            ndjson=ndjson,
            bearer_token=TOKENS[0],
            ca_bundle=ca_bundle,
            client_certificate=certificate,
            client_key=key,
            timeout_seconds=3.0,
        )

    create_context.assert_called_once_with(cafile=str(ca_bundle))
    assert context.minimum_version is ssl.TLSVersion.TLSv1_2
    assert context.cert_chain == (str(certificate), str(key))
    proxy_handler = next(
        handler for handler in captured_handlers if isinstance(handler, ProxyHandler)
    )
    assert proxy_handler.proxies == {}
    assert any(isinstance(handler, HTTPSHandler) for handler in captured_handlers)
    assert any(type(handler).__name__ == "_RejectRedirects" for handler in captured_handlers)
    assert opener.request is not None
    assert opener.request.full_url == ENDPOINT
    assert opener.request.method == "POST"
    assert opener.request.data == ndjson
    assert opener.request.get_header("Authorization") == f"Bearer {TOKENS[0]}"
    assert opener.request.get_header("Content-type") == "application/x-ndjson"
    assert opener.timeout == 3.0


def test_urllib_transport_rejects_http_and_sanitizes_tls_errors(tmp_path: Path) -> None:
    ca_bundle, certificate, key = _tls_paths(tmp_path)
    ndjson = f"{render_structured_event(_event())}\n".encode()

    with pytest.raises(SiemTransportError) as insecure:
        UrllibSiemTransport().send(
            endpoint="http://user:password@siem.example.test/private",
            ndjson=ndjson,
            bearer_token=TOKENS[0],
            ca_bundle=ca_bundle,
            client_certificate=certificate,
            client_key=key,
            timeout_seconds=3.0,
        )
    assert str(insecure.value) == "siem transport failed"
    assert "password" not in str(insecure.value)

    with (
        patch(
            "schemabridge.adapters.observability.siem.ssl.create_default_context",
            side_effect=OSError(f"TLS failed for {key} with {TOKENS[0]}"),
        ),
        pytest.raises(SiemTransportError) as tls_failure,
    ):
        UrllibSiemTransport().send(
            endpoint=ENDPOINT,
            ndjson=ndjson,
            bearer_token=TOKENS[0],
            ca_bundle=ca_bundle,
            client_certificate=certificate,
            client_key=key,
            timeout_seconds=3.0,
        )
    assert str(tls_failure.value) == "siem transport failed"
    assert str(key) not in str(tls_failure.value)
    assert TOKENS[0] not in str(tls_failure.value)
