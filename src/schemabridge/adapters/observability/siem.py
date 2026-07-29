"""Bounded, sanitized SIEM export for validated structured telemetry events."""

from __future__ import annotations

import os
import re
import ssl
import stat
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import Any, NoReturn, Protocol
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from schemabridge.adapters.connectors.remote_secrets import WorkloadIdentitySource
from schemabridge.adapters.observability.metrics import OpenMetricsRegistry
from schemabridge.adapters.observability.structured_logging import (
    StructuredEvent,
    render_structured_event,
)

MAX_SIEM_EVENT_BYTES = 4_096
MAX_SIEM_BUFFERED_EVENTS = 10_000
MAX_SIEM_BATCH_EVENTS = 100
MAX_SIEM_RETRIES = 5
MAX_SIEM_TIMEOUT_SECONDS = 15.0
MAX_SIEM_RESPONSE_BYTES = 4_096
TELEMETRY_EXPORT_METRIC = "schemabridge_telemetry_exports_total"

_BEARER_TOKEN = re.compile(r"^[A-Za-z0-9._~+/=-]{16,16384}$")
_SANITIZED_CONFIGURATION_ERROR = "siem export configuration rejected"
_SANITIZED_EVENT_ERROR = "siem telemetry event rejected"
_SANITIZED_EXPORT_ERROR = "siem telemetry export failed"
_SANITIZED_TRANSPORT_ERROR = "siem transport failed"


class SiemExportError(RuntimeError):
    """A SIEM event or batch could not be accepted or delivered safely."""


class SiemTransportError(RuntimeError):
    """The HTTPS transport failed without exposing request material."""


class SiemTransport(Protocol):
    """Send one already bounded NDJSON batch over an authenticated channel."""

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
        """Deliver one batch or raise a sanitized transport error."""


def _reject_configuration() -> NoReturn:
    raise ValueError(_SANITIZED_CONFIGURATION_ERROR) from None


def _reject_event() -> NoReturn:
    raise SiemExportError(_SANITIZED_EVENT_ERROR) from None


def _reject_transport() -> NoReturn:
    raise SiemTransportError(_SANITIZED_TRANSPORT_ERROR) from None


def _valid_https_endpoint(endpoint: object) -> bool:
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > 2_048:
        return False
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and (port is None or 1 <= port <= 65_535)
        and "\r" not in endpoint
        and "\n" not in endpoint
    )


def _valid_tls_path(path: object, *, private: bool = False) -> bool:
    try:
        if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
            return False
        metadata = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid not in {0, os.geteuid()}
            or metadata.st_size < 1
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            return False
        return not (private and metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO))
    except OSError:
        return False


def _valid_token(token: object) -> bool:
    return isinstance(token, str) and _BEARER_TOKEN.fullmatch(token) is not None


def _valid_ndjson_batch(payload: object) -> bool:
    if (
        not isinstance(payload, bytes)
        or not payload
        or len(payload) > MAX_SIEM_EVENT_BYTES * MAX_SIEM_BATCH_EVENTS
        or not payload.endswith(b"\n")
    ):
        return False
    lines = payload.splitlines(keepends=True)
    return 1 <= len(lines) <= MAX_SIEM_BATCH_EVENTS and all(
        line.endswith(b"\n") and 1 < len(line) <= MAX_SIEM_EVENT_BYTES for line in lines
    )


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: Any,
        msg: Any,
        headers: Any,
        newurl: Any,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


@dataclass(frozen=True, slots=True)
class UrllibSiemTransport:
    """HTTPS-only urllib transport with explicit trust and mutual TLS."""

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
        """POST one bounded batch without redirects or ambient proxies."""

        try:
            if (
                not _valid_https_endpoint(endpoint)
                or not _valid_ndjson_batch(ndjson)
                or not _valid_token(bearer_token)
                or not _valid_tls_path(ca_bundle)
                or not _valid_tls_path(client_certificate)
                or not _valid_tls_path(client_key, private=True)
                or type(timeout_seconds) not in {int, float}
                or not 0.1 <= float(timeout_seconds) <= MAX_SIEM_TIMEOUT_SECONDS
            ):
                raise ValueError
            context = ssl.create_default_context(cafile=str(ca_bundle))
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(
                certfile=str(client_certificate),
                keyfile=str(client_key),
            )
            opener = build_opener(
                ProxyHandler({}),
                HTTPSHandler(context=context),
                _RejectRedirects(),
            )
            request = Request(
                url=endpoint,
                data=ndjson,
                method="POST",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {bearer_token}",
                    "Content-Type": "application/x-ndjson",
                },
            )
            with opener.open(request, timeout=float(timeout_seconds)) as response:
                if response.status not in {200, 201, 202, 204}:
                    raise ValueError
                body = bytes(response.read(MAX_SIEM_RESPONSE_BYTES + 1))
                if len(body) > MAX_SIEM_RESPONSE_BYTES or response.read(1):
                    raise ValueError
        except Exception:
            _reject_transport()


@dataclass(slots=True)
class SiemExporter:
    """Queue and deliver only prevalidated :class:`StructuredEvent` instances."""

    endpoint: str = field(repr=False)
    identity: WorkloadIdentitySource = field(repr=False)
    ca_bundle: Path = field(repr=False)
    client_certificate: Path = field(repr=False)
    client_key: Path = field(repr=False)
    metrics: OpenMetricsRegistry = field(repr=False)
    max_buffered_events: int = MAX_SIEM_BUFFERED_EVENTS
    batch_size: int = MAX_SIEM_BATCH_EVENTS
    max_retries: int = MAX_SIEM_RETRIES
    timeout_seconds: float = 5.0
    retry_backoff_seconds: float = 0.1
    transport: SiemTransport = field(
        default_factory=UrllibSiemTransport,
        repr=False,
        compare=False,
    )
    sleeper: Callable[[float], None] = field(
        default=time.sleep,
        repr=False,
        compare=False,
    )
    _buffer: deque[bytes] = field(default_factory=deque, init=False, repr=False)
    _state_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _flush_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        try:
            paths = (self.ca_bundle, self.client_certificate, self.client_key)
            if (
                not _valid_https_endpoint(self.endpoint)
                or not _valid_tls_path(self.ca_bundle)
                or not _valid_tls_path(self.client_certificate)
                or not _valid_tls_path(self.client_key, private=True)
                or len(set(paths)) != len(paths)
                or not callable(getattr(self.identity, "read", None))
                or not isinstance(self.metrics, OpenMetricsRegistry)
                or not callable(getattr(self.transport, "send", None))
                or not callable(self.sleeper)
                or type(self.max_buffered_events) is not int
                or not 1 <= self.max_buffered_events <= MAX_SIEM_BUFFERED_EVENTS
                or type(self.batch_size) is not int
                or not 1 <= self.batch_size <= MAX_SIEM_BATCH_EVENTS
                or self.batch_size > self.max_buffered_events
                or type(self.max_retries) is not int
                or not 0 <= self.max_retries <= MAX_SIEM_RETRIES
                or type(self.timeout_seconds) not in {int, float}
                or not 0.1 <= float(self.timeout_seconds) <= MAX_SIEM_TIMEOUT_SECONDS
                or type(self.retry_backoff_seconds) not in {int, float}
                or not 0.0 <= float(self.retry_backoff_seconds) <= 1.0
            ):
                _reject_configuration()
        except (OSError, TypeError, ValueError):
            _reject_configuration()

    @property
    def pending_events(self) -> int:
        """Return the current bounded queue depth."""

        with self._state_lock:
            return len(self._buffer)

    def enqueue(self, event: StructuredEvent) -> None:
        """Accept one exact validated event or reject it without retaining input."""

        if type(event) is not StructuredEvent:
            _reject_event()
        try:
            encoded = f"{render_structured_event(event)}\n".encode()
        except (UnicodeError, ValueError):
            self._record_outcome("dropped")
            _reject_event()
        if not 1 < len(encoded) <= MAX_SIEM_EVENT_BYTES:
            self._record_outcome("dropped")
            _reject_event()
        with self._state_lock:
            if len(self._buffer) >= self.max_buffered_events:
                dropped = True
            else:
                self._buffer.append(encoded)
                dropped = False
        if dropped:
            self._record_outcome("dropped")
            _reject_event()

    def flush(self) -> int:
        """Deliver at most one bounded batch and retain it after exhausted retries."""

        with self._flush_lock:
            with self._state_lock:
                batch = tuple(islice(self._buffer, self.batch_size))
            if not batch:
                return 0
            payload = b"".join(batch)
            delivered = False
            for attempt in range(self.max_retries + 1):
                try:
                    token = self.identity.read()
                    if not _valid_token(token):
                        raise ValueError
                    self.transport.send(
                        endpoint=self.endpoint,
                        ndjson=payload,
                        bearer_token=token,
                        ca_bundle=self.ca_bundle,
                        client_certificate=self.client_certificate,
                        client_key=self.client_key,
                        timeout_seconds=float(self.timeout_seconds),
                    )
                    delivered = True
                    break
                except Exception:
                    if attempt < self.max_retries:
                        self._wait_before_retry(attempt)
            if not delivered:
                self._record_outcome("error")
                raise SiemExportError(_SANITIZED_EXPORT_ERROR) from None
            with self._state_lock:
                for expected in batch:
                    if not self._buffer or self._buffer[0] != expected:
                        raise SiemExportError(_SANITIZED_EXPORT_ERROR) from None
                    self._buffer.popleft()
            self._record_outcome("success")
            return len(batch)

    def _wait_before_retry(self, attempt: int) -> None:
        delay = min(float(self.retry_backoff_seconds) * (2**attempt), 5.0)
        with suppress(Exception):
            self.sleeper(delay)

    def _record_outcome(self, outcome: str) -> None:
        try:
            self.metrics.increment_counter(
                TELEMETRY_EXPORT_METRIC,
                {"outcome": outcome},
            )
        except Exception:
            raise SiemExportError(_SANITIZED_EXPORT_ERROR) from None


__all__ = [
    "MAX_SIEM_BATCH_EVENTS",
    "MAX_SIEM_BUFFERED_EVENTS",
    "MAX_SIEM_EVENT_BYTES",
    "MAX_SIEM_RETRIES",
    "SiemExportError",
    "SiemExporter",
    "SiemTransport",
    "SiemTransportError",
    "UrllibSiemTransport",
]
