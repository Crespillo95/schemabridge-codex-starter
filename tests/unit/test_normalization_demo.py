"""Tests for the operator-facing normalization demonstration."""

import json

from typer.testing import CliRunner

from schemabridge.entrypoints.cli.main import app

runner = CliRunner()


def test_normalization_demo_json_shows_canonical_values_and_stable_rejections() -> None:
    result = runner.invoke(app, ["normalize-demo", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    outcomes = {item["case"]: item["outcome"] for item in payload["results"]}
    assert payload["plan"]["leading_zero_policy"] == "strip"
    assert outcomes["padded_string"]["canonical_value"] == "123"
    assert outcomes["integer"]["canonical_value"] == "123"
    assert outcomes["integral_float"]["canonical_value"] == "123"
    assert outcomes["all_zero"]["canonical_value"] == "0"
    assert outcomes["fractional_float"]["code"] == "non_integral_identifier"
    assert outcomes["nan"]["code"] == "non_finite_identifier"
    assert outcomes["positive_infinity"]["code"] == "non_finite_identifier"
    assert outcomes["negative_infinity"]["code"] == "non_finite_identifier"
    assert outcomes["boolean"]["code"] == "boolean_identifier"
    assert outcomes["malformed_string"]["code"] == "malformed_identifier"
    assert outcomes["null"]["code"] == "null_not_allowed"
    assert all(
        item["outcome"].get("reason")
        for item in payload["results"]
        if item["outcome"]["status"] == "rejected"
    )


def test_normalization_demo_table_is_renderable() -> None:
    result = runner.invoke(app, ["normalize-demo"])

    assert result.exit_code == 0
    assert "non_integral_identifier" in result.stdout
    assert "null_not_allowed" in result.stdout
