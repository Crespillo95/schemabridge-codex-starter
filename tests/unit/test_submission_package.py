"""M18 submission artifacts remain generated, exact, and honestly labeled."""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

import pytest
import yaml
from scripts.generate_submission_package import (
    ReleaseState,
    SubmissionPackageError,
    build_core_artifacts,
    build_manifest,
    require_release_state,
)

ROOT = Path(__file__).parents[2]


def test_core_artifacts_come_from_current_typed_and_guarded_path() -> None:
    artifacts = build_core_artifacts(ROOT)

    assert set(artifacts) == {
        "logical-model-customer.yml",
        "column-mapping-customer-key.yml",
        "join-contract-customer-account-holder.yml",
        "analytical-request-secondary-holders.yml",
        "resolved-query-plan-secondary-holders.yml",
        "generated-secondary-holders.sql",
        "query-validation-report.yml",
        "rejected-records.csv",
        "datahub-writeback.yml",
    }
    request = yaml.safe_load(artifacts["analytical-request-secondary-holders.yml"])
    plan = yaml.safe_load(artifacts["resolved-query-plan-secondary-holders.yml"])
    validation = yaml.safe_load(artifacts["query-validation-report.yml"])
    writeback = yaml.safe_load(artifacts["datahub-writeback.yml"])
    rows = list(csv.DictReader(io.StringIO(artifacts["rejected-records.csv"].decode())))

    assert request["request"]["metrics"][0]["operation"] == "count_distinct"
    assert plan["fanout_mitigations"][0]["contract_id"] == "customer_to_account_holder"
    assert validation["sql_policy"] == {"status": "accepted", "findings": []}
    assert validation["preview"]["rows"] == [
        ["2026-01-01", 2],
        ["2026-01-02", 1],
        ["2026-01-03", 1],
    ]
    assert [row["code"] for row in rows] == [
        "non_integral_identifier",
        "non_finite_identifier",
        "null_join_key",
    ]
    assert writeback["adapter_mode"].startswith("contract-compatible fake")
    assert writeback["before"] is None
    assert "schemabridge.Customer" in writeback["after"]["logical_model_urn"]
    assert artifacts["generated-secondary-holders.sql"].startswith(b"SELECT\n")


def test_manifest_is_deterministic_and_never_promotes_dirty_evidence() -> None:
    artifacts = {"a.txt": b"alpha\n", "b.txt": b"beta\n"}
    dirty = ReleaseState(revision="abc123", dirty=True)

    with pytest.raises(SubmissionPackageError, match="existing clean HEAD"):
        require_release_state(dirty, allow_uncommitted=False)
    require_release_state(dirty, allow_uncommitted=True)
    first = json.loads(build_manifest(artifacts, dirty))
    second = json.loads(build_manifest(dict(reversed(tuple(artifacts.items()))), dirty))

    assert first == second
    assert first["release_ready"] is False
    assert first["source_revision"] == "abc123"
    assert first["notice"].startswith("Development evidence only")


def test_caption_package_ends_before_three_minutes() -> None:
    captions = (ROOT / "docs/18_CAPTIONS.srt").read_text(encoding="utf-8")
    matches = re.findall(r"--> (\d{2}):(\d{2}):(\d{2}),(\d{3})", captions)

    assert matches
    end_seconds = [
        int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(milliseconds) / 1000
        for hours, minutes, seconds, milliseconds in matches
    ]
    assert end_seconds == sorted(end_seconds)
    assert end_seconds[-1] == 175.0
    assert end_seconds[-1] < 180.0
    assert "DataHub" in captions
    assert "read-only" in captions
