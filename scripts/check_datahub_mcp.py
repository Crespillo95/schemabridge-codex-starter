#!/usr/bin/env python3
"""Verify the pinned DataHub MCP exposes only the required read surface."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

FIXTURE_PATH = Path("tests/fixtures/datahub/mcp_catalog.json")
READ_TOOLS = {
    "get_dataset_queries",
    "get_entities",
    "get_lineage",
    "get_lineage_paths_between",
    "list_schema_fields",
    "search",
}
MUTATION_TOOLS = {
    "add_owners",
    "add_structured_properties",
    "add_tags",
    "add_terms",
    "remove_domains",
    "remove_owners",
    "remove_structured_properties",
    "remove_tags",
    "remove_terms",
    "save_document",
    "set_domains",
    "update_description",
}


def _text(result: Any) -> str:
    return "\n".join(block.text for block in result.content if isinstance(block, TextContent))


def _arguments(tool: Any, values: dict[str, str]) -> dict[str, str]:
    properties = tool.inputSchema.get("properties", {})
    for candidate, value in values.items():
        if candidate in properties:
            return {candidate: value}
    raise SystemExit(f"Unexpected input schema for MCP tool {tool.name}: {sorted(properties)}")


async def check() -> None:
    fixture = json.loads(FIXTURE_PATH.read_text())
    parameters = StdioServerParameters(command="bash", args=["scripts/datahub-mcp.sh"])
    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        listed = await session.list_tools()
        tools = {tool.name: tool for tool in listed.tools}
        required = {"search", "list_schema_fields"}
        missing = required - tools.keys()
        unsafe = MUTATION_TOOLS & tools.keys()
        unexpected = tools.keys() - READ_TOOLS
        if missing:
            raise SystemExit(f"Missing DataHub MCP read tools: {sorted(missing)}")
        if unsafe:
            raise SystemExit(f"DataHub MCP mutation tools are enabled: {sorted(unsafe)}")
        if unexpected:
            raise SystemExit(f"DataHub MCP exposed unexpected tools: {sorted(unexpected)}")

        search_args = _arguments(
            tools["search"],
            {"query": fixture["search_query"], "search_query": fixture["search_query"]},
        )
        search_result = await session.call_tool("search", search_args)
        search_text = _text(search_result)
        if search_result.isError or fixture["dataset_urn"] not in search_text:
            raise SystemExit("DataHub MCP search did not return the expected CRM dataset.")

        fields_args = _arguments(
            tools["list_schema_fields"],
            {
                "urn": fixture["dataset_urn"],
                "entity_urn": fixture["dataset_urn"],
                "identifier": fixture["dataset_urn"],
            },
        )
        fields_result = await session.call_tool("list_schema_fields", fields_args)
        fields_text = _text(fields_result)
        if fields_result.isError:
            raise SystemExit("DataHub MCP schema-field read returned an error.")
        missing_fields = [field for field in fixture["fields"] if field not in fields_text]
        if missing_fields:
            raise SystemExit(f"DataHub MCP schema read missed fields: {missing_fields}")

    print(
        "DataHub MCP read check passed: search and list_schema_fields succeeded; "
        "mutation tools were absent."
    )


if __name__ == "__main__":
    asyncio.run(check())
