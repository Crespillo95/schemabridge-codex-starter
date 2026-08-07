"""Bounded lifecycle tests for the synchronous MCP stdio client."""

from __future__ import annotations

import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

from schemabridge.adapters.datahub.mcp_client import (
    McpClientError,
    McpFailureKind,
    McpStdioToolClient,
)

ROOT = Path(__file__).parents[2]


def test_datahub_mcp_wrapper_disables_external_telemetry() -> None:
    wrapper = (ROOT / "scripts/datahub-mcp.sh").read_text()

    assert "DATAHUB_TELEMETRY_ENABLED=false" in wrapper


def test_stdio_client_closes_each_successful_operation_without_hidden_session(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "server.pid"
    client = McpStdioToolClient(
        command=sys.executable,
        arguments=("-c", _working_server(marker)),
        working_directory=tmp_path,
        operation_timeout_seconds=5,
    )

    assert client.list_tools() == frozenset({"echo"})
    first_pid = int(marker.read_text())
    assert _wait_until_process_exits(first_pid)

    assert client.call_tool("echo", {"value": "safe"}) == {"value": "safe"}
    second_pid = int(marker.read_text())
    assert second_pid != first_pid
    assert _wait_until_process_exits(second_pid)


def test_stdio_client_timeout_is_sanitized_and_terminates_server(tmp_path: Path) -> None:
    marker = tmp_path / "server.pid"
    client = McpStdioToolClient(
        command=sys.executable,
        arguments=("-c", _unresponsive_server(marker)),
        working_directory=tmp_path,
        operation_timeout_seconds=0.25,
    )

    started = time.monotonic()
    with pytest.raises(McpClientError) as raised:
        client.list_tools()

    assert raised.value.kind is McpFailureKind.UNAVAILABLE
    assert str(marker) not in str(raised.value)
    assert time.monotonic() - started < 5
    assert marker.is_file()
    assert _wait_until_process_exits(int(marker.read_text()))


@pytest.mark.parametrize("timeout", [True, 0, -1, float("nan"), float("inf"), 120.1])
def test_stdio_client_rejects_unbounded_or_invalid_timeouts(
    tmp_path: Path,
    timeout: float,
) -> None:
    with pytest.raises(ValueError, match="MCP operation timeout"):
        McpStdioToolClient(
            command=sys.executable,
            arguments=(),
            working_directory=tmp_path,
            operation_timeout_seconds=timeout,
        )


def _working_server(marker: Path) -> str:
    return textwrap.dedent(
        f"""
        import os
        from pathlib import Path
        from mcp.server.fastmcp import FastMCP

        Path({str(marker)!r}).write_text(str(os.getpid()))
        server = FastMCP("synthetic-test", log_level="ERROR")

        @server.tool()
        def echo(value: str) -> dict[str, str]:
            return {{"value": value}}

        server.run(transport="stdio")
        """
    )


def _unresponsive_server(marker: Path) -> str:
    return textwrap.dedent(
        f"""
        import os
        import time
        from pathlib import Path

        Path({str(marker)!r}).write_text(str(os.getpid()))
        time.sleep(60)
        """
    )


def _wait_until_process_exits(pid: int) -> bool:
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.01)
    return False
