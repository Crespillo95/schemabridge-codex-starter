"""Shared catalog contract against the authenticated local DataHub MCP."""

from pathlib import Path

import pytest
from tests.contract.catalog_read_contract import assert_catalog_read_contract

from schemabridge.bootstrap import build_catalog_reader

ROOT = Path(__file__).parents[2]
MCP_CREDENTIALS = ROOT / ".local/datahub/mcp.env"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not MCP_CREDENTIALS.is_file(),
        reason="DataHub MCP credentials are absent; run make datahub-provision-mcp",
    ),
]


def test_local_datahub_satisfies_shared_catalog_contract() -> None:
    catalog = build_catalog_reader("live", repository_root=ROOT)

    assert_catalog_read_contract(catalog)
