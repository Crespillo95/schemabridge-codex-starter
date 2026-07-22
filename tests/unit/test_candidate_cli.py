"""Observable candidate CLI ranking and fixture disclosure."""

import json

from typer.testing import CliRunner

from schemabridge.entrypoints.cli.main import app

runner = CliRunner()


def test_recorded_candidate_cli_exposes_scores_risks_and_fixture_errors() -> None:
    result = runner.invoke(
        app,
        [
            "candidates",
            "--concept",
            "Customer.customer_key",
            "--adapter",
            "recorded",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["evidence_source"] == "recorded:synthetic-bounded-signals"
    assert [candidate["physical_field"] for candidate in payload["candidates"][:3]] == [
        "crm.customers.customer_id",
        "legacy.client_master.client_no",
        "bank.account_holders.gf_customer_id",
    ]
    assert payload["candidates"][0]["status"] == "needs_review"
    float_candidate = payload["candidates"][2]
    assert "unsafe_float_identifier" in float_candidate["risks"]
    assert len(float_candidate["score_breakdown"]) == 7
    metrics = payload["evaluation_fixture_metrics"]
    assert metrics["false_positives"]
    assert metrics["false_negatives"]
    assert "not production evidence" in metrics["fixture_notice"]


def test_candidate_cli_rejects_unimplemented_concept_without_catalog_access() -> None:
    result = runner.invoke(
        app,
        ["candidates", "--concept", "Account.account_key", "--adapter", "recorded", "--json"],
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout)["code"] == "unsupported_demo_concept"
