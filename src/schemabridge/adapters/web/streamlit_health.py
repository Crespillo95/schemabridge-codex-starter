"""Fixed, bounded loopback readiness check for the Streamlit listener."""

from __future__ import annotations

import http.client
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Protocol, cast

_LOOPBACK_HOST = "127.0.0.1"
_STREAMLIT_PORT = 7860
_HEALTH_PATH = "/_stcore/health"
_EXPECTED_BODY = b"ok"


class StreamlitLoopbackHealthError(RuntimeError):
    """Sanitized failure when the local Streamlit listener is not ready."""


class _Response(Protocol):
    status: int

    def read(self, amount: int | None = None) -> bytes:
        """Read at most the requested bounded response bytes."""


class _Connection(Protocol):
    def request(
        self,
        method: str,
        url: str,
        body: object | None,
        headers: Mapping[str, str],
    ) -> None:
        """Issue one request without redirect handling."""

    def getresponse(self) -> _Response:
        """Return the response."""

    def close(self) -> None:
        """Close the connection."""


class _ConnectionFactory(Protocol):
    def __call__(
        self,
        host: str,
        port: int,
        timeout: float,
    ) -> _Connection:
        """Create one fixed-target connection."""


def _open_connection(host: str, port: int, timeout: float) -> _Connection:
    return cast(_Connection, http.client.HTTPConnection(host, port, timeout=timeout))


@dataclass(frozen=True, slots=True)
class StreamlitLoopbackHealth:
    """Require one exact healthy response from the fixed local Streamlit endpoint."""

    timeout_seconds: float = 1.0
    max_response_bytes: int = 16
    connection_factory: _ConnectionFactory = field(
        default=_open_connection,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not 0.1 <= self.timeout_seconds <= 2.0
            or type(self.max_response_bytes) is not int
            or not len(_EXPECTED_BODY) <= self.max_response_bytes <= 64
        ):
            raise ValueError("Streamlit loopback health configuration is invalid")

    def require_ready(self) -> None:
        """Reject connection errors, non-200 states, and every unexpected body."""

        connection: _Connection | None = None
        try:
            connection = self.connection_factory(
                _LOOPBACK_HOST,
                _STREAMLIT_PORT,
                self.timeout_seconds,
            )
            connection.request(
                "GET",
                _HEALTH_PATH,
                body=None,
                headers={
                    "Accept": "text/plain",
                    "Connection": "close",
                    "Host": f"{_LOOPBACK_HOST}:{_STREAMLIT_PORT}",
                },
            )
            response = connection.getresponse()
            body = response.read(self.max_response_bytes + 1)
            if response.status != 200 or body != _EXPECTED_BODY:
                raise ValueError
        except (OSError, http.client.HTTPException, ValueError):
            raise StreamlitLoopbackHealthError(
                "Streamlit loopback listener is unavailable"
            ) from None
        finally:
            if connection is not None:
                with suppress(OSError, http.client.HTTPException):
                    connection.close()
