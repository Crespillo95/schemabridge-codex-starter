"""Observable M30 Phase-1a authorization journey."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
import scripts.m30_authenticate_campaign_manifest as campaign_script
from tests.m30_campaign_support import VERIFICATION_TIME, build_manifest, write_manifest
from tests.m30_readiness_support import build_candidate_repository

import schemabridge.adapters.evaluation.m30_campaign as campaign_adapter
from schemabridge.domain.production_campaign import M30_GITHUB_CLI_VERSION

pytestmark = pytest.mark.acceptance


def test_exact_hosted_manifest_authenticates_inputs_but_keeps_campaign_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    manifest_path = write_manifest(tmp_path / "external/manifest.json", build_manifest(repository))
    bundle_path = tmp_path / "external/attestation.jsonl"
    bundle_path.write_text('{"synthetic":"verified-by-test-double"}\n', encoding="ascii")
    output = tmp_path / "authentication"

    def run(_root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        if arguments == ("--version",):
            stdout = f"gh version {M30_GITHUB_CLI_VERSION} (acceptance)\n".encode()
        else:
            stdout = json.dumps(
                {
                    "certificate": {"issuer": "synthetic-github-oidc"},
                    "count": 1,
                    "trustedTimestamps": [{"type": "synthetic-trusted-timestamp"}],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        return subprocess.CompletedProcess(arguments, 0, stdout, b"")

    class _Clock:
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return VERIFICATION_TIME

    monkeypatch.setattr(campaign_adapter, "_run_safe_gh", run)
    monkeypatch.setattr(campaign_adapter, "datetime", _Clock)

    exit_code = campaign_script.main(
        [
            "--repository-root",
            str(repository),
            "--manifest",
            str(manifest_path),
            "--attestation-bundle",
            str(bundle_path),
            "--output-directory",
            str(output),
        ]
    )

    assert exit_code == 0
    payload = json.loads((output / "authentication.json").read_text(encoding="utf-8"))["report"]
    assert payload["state"] == "authenticated"
    assert payload["campaign_executable"] is False
    assert payload["release_decision"] == "no_go"
    assert payload["workflow_attested_manifest_authenticated"] is True
    assert payload["external_controls_passed"] == 0
    assert payload["external_controls_remaining"] == 24
    assert all(
        gate["status"] == "missing_external"
        for gate in payload["preflight"]["gates"]
        if gate["evidence_class"] != "repository_contract"
    )
