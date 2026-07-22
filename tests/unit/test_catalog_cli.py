"""Catalog inspection CLI source labeling and failure behavior."""

import json

from typer.testing import CliRunner

from schemabridge.entrypoints.cli.main import app

runner = CliRunner()


def test_recorded_catalog_inspection_is_visibly_labeled() -> None:
    result = runner.invoke(
        app,
        ["catalog-inspect", "crm.customers", "--adapter", "recorded", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["source"] == "recorded:sanitized-datahub-m04"
    assert payload["asset"]["dataset"] == "crm.customers"
    assert payload["lineage"]["upstream"]["status"] == "missing"
    assert payload["query_context"]["status"] == "missing"


def test_catalog_inspection_rejects_unqualified_dataset() -> None:
    result = runner.invoke(app, ["catalog-inspect", "customers", "--json"])

    assert result.exit_code == 2
    assert json.loads(result.stdout)["code"] == "invalid_dataset"
