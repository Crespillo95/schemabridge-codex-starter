"""Minimal synchronous transport over the pinned DataHub MCP stdio server."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class McpFailureKind(StrEnum):
    """Sanitized MCP failure categories used by the catalog translator."""

    UNAVAILABLE = "unavailable"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    TOOL_ERROR = "tool_error"


class McpClientError(RuntimeError):
    """Sanitized MCP failure with no server response body."""

    def __init__(self, tool_name: str, kind: McpFailureKind) -> None:
        super().__init__(f"MCP {tool_name} failed ({kind.value})")
        self.tool_name = tool_name
        self.kind = kind


class McpToolClient(Protocol):
    """Small adapter-internal boundary used for translation tests."""

    def list_tools(self) -> frozenset[str]:
        """Return exposed MCP tool names."""

    def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        """Call one tool and return decoded JSON-compatible content."""


@dataclass(frozen=True, slots=True)
class McpStdioToolClient:
    """Launch a fresh read-only MCP stdio session for each synchronous operation."""

    command: str
    arguments: tuple[str, ...]
    working_directory: Path

    def list_tools(self) -> frozenset[str]:
        """List tools without persisting a background process or credentials."""

        return asyncio.run(self._list_tools())

    def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        """Decode a JSON tool result while suppressing raw error bodies."""

        return asyncio.run(self._call_tool(name, arguments))

    async def _list_tools(self) -> frozenset[str]:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            parameters = StdioServerParameters(
                command=self.command,
                args=list(self.arguments),
                cwd=self.working_directory,
            )
            async with (
                stdio_client(parameters) as (read_stream, write_stream),
                ClientSession(read_stream, write_stream) as session,
            ):
                await session.initialize()
                result = await session.list_tools()
                return frozenset(tool.name for tool in result.tools)
        except Exception as error:
            raise McpClientError("list_tools", McpFailureKind.UNAVAILABLE) from error

    async def _call_tool(self, name: str, arguments: dict[str, object]) -> object:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            from mcp.types import TextContent

            parameters = StdioServerParameters(
                command=self.command,
                args=list(self.arguments),
                cwd=self.working_directory,
            )
            async with (
                stdio_client(parameters) as (read_stream, write_stream),
                ClientSession(read_stream, write_stream) as session,
            ):
                await session.initialize()
                result = await session.call_tool(name, arguments)
                body = "\n".join(
                    block.text for block in result.content if isinstance(block, TextContent)
                )
                if result.isError:
                    raise McpClientError(name, _classify_tool_error(body))
                try:
                    decoded: object = json.loads(body)
                except json.JSONDecodeError as error:
                    raise McpClientError(name, McpFailureKind.TOOL_ERROR) from error
                return decoded
        except McpClientError:
            raise
        except Exception as error:
            raise McpClientError(name, McpFailureKind.UNAVAILABLE) from error


def _classify_tool_error(body: str) -> McpFailureKind:
    normalized = body.casefold()
    if any(marker in normalized for marker in ("forbidden", "permission", "unauthorized")):
        return McpFailureKind.PERMISSION_DENIED
    if "not found" in normalized:
        return McpFailureKind.NOT_FOUND
    return McpFailureKind.TOOL_ERROR
