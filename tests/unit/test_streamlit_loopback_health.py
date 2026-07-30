from __future__ import annotations

from collections.abc import Mapping

import pytest

from schemabridge.adapters.web.streamlit_health import (
    StreamlitLoopbackHealth,
    StreamlitLoopbackHealthError,
)


class _Response:
    def __init__(self, *, status: int, body: bytes) -> None:
        self.status = status
        self.body = body
        self.read_bounds: list[int | None] = []

    def read(self, amount: int | None = None) -> bytes:
        self.read_bounds.append(amount)
        return self.body if amount is None else self.body[:amount]


class _Connection:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.requests: list[tuple[str, str, object | None, Mapping[str, str]]] = []
        self.closed = False

    def request(
        self,
        method: str,
        url: str,
        body: object | None,
        headers: Mapping[str, str],
    ) -> None:
        self.requests.append((method, url, body, headers))

    def getresponse(self) -> _Response:
        return self.response

    def close(self) -> None:
        self.closed = True


def test_streamlit_listener_check_is_fixed_to_bounded_ipv4_loopback() -> None:
    response = _Response(status=200, body=b"ok")
    connection = _Connection(response)
    targets: list[tuple[str, int, float]] = []

    def factory(host: str, port: int, timeout: float) -> _Connection:
        targets.append((host, port, timeout))
        return connection

    StreamlitLoopbackHealth(connection_factory=factory).require_ready()

    assert targets == [("127.0.0.1", 7860, 1.0)]
    assert connection.requests == [
        (
            "GET",
            "/_stcore/health",
            None,
            {
                "Accept": "text/plain",
                "Connection": "close",
                "Host": "127.0.0.1:7860",
            },
        )
    ]
    assert response.read_bounds == [17]
    assert connection.closed is True


def test_streamlit_listener_down_fails_with_only_a_sanitized_error() -> None:
    private_marker = "private-loopback-socket-detail"

    def refused(_host: str, _port: int, _timeout: float) -> _Connection:
        raise ConnectionRefusedError(private_marker)

    with pytest.raises(StreamlitLoopbackHealthError) as failure:
        StreamlitLoopbackHealth(connection_factory=refused).require_ready()

    assert str(failure.value) == "Streamlit loopback listener is unavailable"
    assert private_marker not in str(failure.value)
    assert failure.value.__cause__ is None


@pytest.mark.parametrize(
    ("status", "body"),
    (
        (503, b"ok"),
        (204, b""),
        (200, b"not-ready"),
        (200, b"ok\n"),
    ),
)
def test_streamlit_listener_rejects_non_200_or_non_exact_body(
    status: int,
    body: bytes,
) -> None:
    response = _Response(status=status, body=body)
    connection = _Connection(response)

    with pytest.raises(StreamlitLoopbackHealthError):
        StreamlitLoopbackHealth(
            connection_factory=lambda _host, _port, _timeout: connection
        ).require_ready()

    assert connection.closed is True
