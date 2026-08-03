"""Observable M30 Phase-1b policy-binding journey with no control acceptance."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
import scripts.m30_validate_control_policy as policy_script
from tests.m30_campaign_support import VERIFICATION_TIME, build_manifest, write_manifest
from tests.m30_control_policy_support import build_control_policy, write_control_policy
from tests.m30_readiness_support import build_candidate_repository

import schemabridge.adapters.evaluation.m30_campaign as campaign_adapter
from schemabridge.domain.production_campaign import M30_GITHUB_CLI_VERSION

pytestmark = pytest.mark.acceptance


def test_external_policy_binds_exact_manifest_but_keeps_24_controls_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    provisional = build_manifest(repository)
    provisional_policy = build_control_policy(provisional)
    manifest = build_manifest(
        repository,
        control_policy_id=provisional_policy.policy_id,
        control_policy_sha256=provisional_policy.fingerprint(),
    )
    policy = build_control_policy(manifest)
    manifest_path = write_manifest(tmp_path / "external/manifest.json", manifest)
    policy_path = write_control_policy(tmp_path / "external/control-policy.json", policy)
    bundle_path = tmp_path / "external/attestation.jsonl"
    bundle_path.write_text('{"synthetic":"verified-by-test-double"}\n', encoding="ascii")
    output = tmp_path / "policy-validation"

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

    exit_code = policy_script.main(
        [
            "--repository-root",
            str(repository),
            "--manifest",
            str(manifest_path),
            "--manifest-attestation-bundle",
            str(bundle_path),
            "--control-policy",
            str(policy_path),
            "--output-directory",
            str(output),
        ]
    )

    assert exit_code == 0
    payload = json.loads((output / "policy-validation.json").read_text(encoding="utf-8"))["report"]
    assert payload["schema_version"] == 2
    assert payload["state"] == "validated"
    assert (
        datetime.fromisoformat(payload["observed_at"].replace("Z", "+00:00")) == VERIFICATION_TIME
    )
    assert datetime.fromisoformat(payload["policy_not_before"].replace("Z", "+00:00")) == (
        manifest.not_before
    )
    assert datetime.fromisoformat(payload["policy_expires_at"].replace("Z", "+00:00")) == (
        manifest.expires_at
    )
    assert payload["manifest_authentication_sha256"]
    assert payload["policy_bound_to_authenticated_manifest"] is True
    assert payload["control_dag_validated"] is True
    assert payload["external_policy_trust_authenticated"] is False
    assert payload["receipt_authentication_enabled"] is False
    assert payload["implemented_adjudicators"] == 1
    assert payload["admitted_unadjudicated_controls"] == 23
    assert payload["external_controls_passed"] == 0
    assert payload["external_controls_remaining"] == 24
    assert payload["campaign_executable"] is False
    assert payload["production_release_authorized"] is False
    assert payload["release_decision"] == "no_go"
    assert payload["provider_calls"] == 0
    assert payload["source_reads"] == 0
    assert payload["target_calls"] == 0
    assert payload["corpus_reads"] == 0
    assert payload["datahub_writes"] == 0
