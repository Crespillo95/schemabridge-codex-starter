"""CLI coverage for guided choices, actionable rejection, and draft reload."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from schemabridge.entrypoints.cli.main import app

runner = CliRunner()


def test_guided_request_demo_builds_north_star_without_physical_or_sql_input() -> None:
    result = runner.invoke(app, ["request-demo", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    encoded = json.dumps(payload, sort_keys=True)
    assert payload["request"]["primary_entity"] == "Customer"
    assert payload["summary"]["required_models"] == ["Customer", "AccountHolder"]
    assert payload["summary"]["join_contracts"] == ["customer_to_account_holder"]
    assert payload["planning_handoff"]["adapter"] == "fake:no-physical-resolution"
    assert "raw_sql" not in encoded
    assert "crm." not in encoded
    assert "bank." not in encoded


def test_guided_request_demo_returns_actionable_closed_validation_errors() -> None:
    invalid_sum = runner.invoke(
        app,
        ["request-demo", "--metric-operation", "sum", "--json"],
    )
    assert invalid_sum.exit_code == 1
    sum_payload = json.loads(invalid_sum.stdout)
    assert sum_payload["code"] == "incompatible_metric_operation"
    assert "count_distinct" in sum_payload["error"]

    unsupported_grain = runner.invoke(
        app,
        ["request-demo", "--grain", "quarter", "--json"],
    )
    assert unsupported_grain.exit_code == 1
    grain_payload = json.loads(unsupported_grain.stdout)
    assert grain_payload["code"] == "unsupported_date_grain"
    assert "day, week, month, year" in grain_payload["error"]


def test_guided_request_demo_saves_and_reloads_the_same_typed_draft(tmp_path: Path) -> None:
    environment = {"SCHEMABRIDGE_DRAFT_STORE_PATH": str(tmp_path / "request-cli.db")}
    saved = runner.invoke(
        app,
        ["request-demo", "--save-draft", "operator-demo", "--json"],
        env=environment,
    )
    assert saved.exit_code == 0, saved.stdout
    saved_payload = json.loads(saved.stdout)

    loaded = runner.invoke(
        app,
        ["request-demo", "--load-draft", "operator-demo", "--json"],
        env=environment,
    )
    assert loaded.exit_code == 0, loaded.stdout
    loaded_payload = json.loads(loaded.stdout)

    assert saved_payload["draft"]["revision"] == 1
    assert loaded_payload["case"] == "reloaded-draft"
    assert loaded_payload["request"] == saved_payload["request"]
    assert loaded_payload["draft"] == saved_payload["draft"]


def test_guided_request_demo_rejects_non_inert_draft_ids(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["request-demo", "--save-draft", "../../unsafe", "--json"],
        env={"SCHEMABRIDGE_DRAFT_STORE_PATH": str(tmp_path / "request-cli.db")},
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "invalid_request_draft_id"
