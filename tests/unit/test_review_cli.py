"""Explicit CLI approval and publication boundary tests."""

import json

from typer.testing import CliRunner

from schemabridge.entrypoints.cli.main import app

runner = CliRunner()
TARGETS = (
    "crm.customers.customer_id->Customer.customer_key",
    "legacy.client_master.client_no->Customer.customer_key",
    "bank.account_holders.gf_customer_id->Customer.customer_key",
    "crm.customers.registration_date->Customer.registration_date",
)


def test_cli_requires_explicit_decisions_and_exact_publish_confirmation(tmp_path) -> None:
    environment = {"SCHEMABRIDGE_DRAFT_STORE_PATH": str(tmp_path / "review.db")}
    initialized = runner.invoke(app, ["review-init", "--json"], env=environment)
    assert initialized.exit_code == 0

    for revision, target in enumerate(TARGETS, start=1):
        decided = runner.invoke(
            app,
            [
                "review-decide",
                target,
                "--action",
                "approve",
                "--revision",
                str(revision),
                "--actor",
                "operator@example.test",
                "--rationale",
                "Reviewed synthetic evidence and transformation risks.",
                "--json",
            ],
            env=environment,
        )
        assert decided.exit_code == 0, decided.stdout

    missing_confirmation = runner.invoke(
        app,
        ["review-publish", "--actor", "operator@example.test", "--adapter", "fake"],
        env=environment,
    )
    assert missing_confirmation.exit_code == 2

    published = runner.invoke(
        app,
        [
            "review-publish",
            "--actor",
            "operator@example.test",
            "--confirm",
            "publish-approved-canonical-context",
            "--adapter",
            "fake",
            "--json",
        ],
        env=environment,
    )
    assert published.exit_code == 0, published.stdout
    payload = json.loads(published.stdout)
    assert payload["status"] == "published"
    assert payload["adapter"] == "fake"
    assert all(item["decision_refs"] for item in payload["items"])
