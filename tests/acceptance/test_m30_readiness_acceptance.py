"""Observable M30 Phase-0 preparation from one clean synthetic candidate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.m30_readiness import main
from tests.m30_readiness_support import build_candidate_repository


@pytest.mark.acceptance
def test_m30_preflight_materializes_an_explicit_no_go_without_external_actions(
    tmp_path: Path,
) -> None:
    repository = build_candidate_repository(
        tmp_path / "candidate",
        include_local_claim=True,
    )
    output = tmp_path / "evidence"

    result = main(
        [
            "--repository-root",
            str(repository),
            "--output-directory",
            str(output),
            "--report-only",
        ]
    )
    payload = json.loads((output / "readiness.json").read_text(encoding="utf-8"))["report"]

    assert result == 0
    assert payload["preflight_state"] == "blocked_prerequisites"
    assert payload["campaign_executable"] is False
    assert payload["release_decision"] == "no_go"
    assert payload["synthetic_evidence_accepted_as_operated"] is False
    assert payload["network_calls"] == 0
    assert payload["database_calls"] == 0
    assert payload["datahub_writes"] == 0
    assert payload["source_writes"] == 0
    assert all(
        gate["status"] == "missing_external"
        for gate in payload["gates"]
        if gate["evidence_class"] != "repository_contract"
    )
