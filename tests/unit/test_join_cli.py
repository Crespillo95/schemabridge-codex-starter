"""CLI evidence display, explicit decisions, and exact join publication confirmation."""

import json
from dataclasses import dataclass

from typer.testing import CliRunner

from schemabridge.application.join_demo import build_north_star_join_proposals
from schemabridge.application.join_discovery import JoinDiscoveryReport
from schemabridge.domain.joins import (
    DeclaredRelationship,
    RelationshipProfile,
    score_join_candidate,
)
from schemabridge.entrypoints.cli import main

runner = CliRunner()


@dataclass(frozen=True)
class FakeDiscoverer:
    report: JoinDiscoveryReport

    def execute(self, proposals: object) -> JoinDiscoveryReport:
        return self.report


def test_join_cli_warns_about_fanout_and_requires_explicit_publication(
    tmp_path, monkeypatch
) -> None:
    report = _report()
    monkeypatch.setattr(main, "build_join_discoverer", lambda _kind: FakeDiscoverer(report))
    environment = {"SCHEMABRIDGE_DRAFT_STORE_PATH": str(tmp_path / "reviews.db")}

    discovered = runner.invoke(main.app, ["join-discover", "--catalog", "recorded", "--json"])
    assert discovered.exit_code == 0, discovered.stdout
    discovery_payload = json.loads(discovered.stdout)
    warning = discovery_payload["candidates"][0]["fanout_warning"]
    assert "COUNT DISTINCT" in warning
    assert "customer 123" in warning

    initialized = runner.invoke(
        main.app,
        ["join-review-init", "--catalog", "recorded", "--json"],
        env=environment,
    )
    assert initialized.exit_code == 0, initialized.stdout

    for revision, proposal_id in enumerate(
        ("customer_to_account_holder", "account_holder_to_account"), start=1
    ):
        decided = runner.invoke(
            main.app,
            [
                "join-review-decide",
                proposal_id,
                "--action",
                "approve",
                "--revision",
                str(revision),
                "--actor",
                "operator@example.test",
                "--rationale",
                "Reviewed overlap, cardinality, and fanout evidence.",
                "--json",
            ],
            env=environment,
        )
        assert decided.exit_code == 0, decided.stdout

    missing_confirmation = runner.invoke(
        main.app,
        ["join-publish", "--actor", "operator@example.test", "--adapter", "fake"],
        env=environment,
    )
    assert missing_confirmation.exit_code == 2

    published = runner.invoke(
        main.app,
        [
            "join-publish",
            "--actor",
            "operator@example.test",
            "--confirm",
            "publish-approved-join-contracts",
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


def _report() -> JoinDiscoveryReport:
    proposals = build_north_star_join_proposals()
    profiles = (
        RelationshipProfile(
            left_row_count=7,
            right_row_count=9,
            left_null_count=0,
            right_null_count=1,
            left_invalid_count=0,
            right_invalid_count=2,
            left_distinct_valid=7,
            right_distinct_valid=5,
            matching_distinct_keys=5,
            left_max_multiplicity=1,
            right_max_multiplicity=2,
        ),
        RelationshipProfile(
            left_row_count=9,
            right_row_count=9,
            left_null_count=0,
            right_null_count=0,
            left_invalid_count=0,
            right_invalid_count=0,
            left_distinct_valid=9,
            right_distinct_valid=9,
            matching_distinct_keys=9,
            left_max_multiplicity=1,
            right_max_multiplicity=1,
            declared_relationship=DeclaredRelationship.LEFT_FOREIGN_KEY_TO_RIGHT,
        ),
    )
    return JoinDiscoveryReport(
        catalog_source="recorded:test",
        candidates=tuple(
            score_join_candidate(proposal, profile)
            for proposal, profile in zip(proposals, profiles, strict=True)
        ),
    )
