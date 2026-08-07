from __future__ import annotations

import json
import stat
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from schemabridge.application.ports.query_studio_ai_control import (
    TenantAiPolicyOperatorSnapshot,
    TenantAiPolicyWrite,
)
from schemabridge.application.query_studio_ai_policy import (
    TenantAiPolicyDesired,
    TenantAiPolicyOperator,
)
from schemabridge.entrypoints.ai_policy import main as ai_policy_main
from schemabridge.entrypoints.ai_policy.main import app

NOW = datetime(2026, 7, 26, 14, 0, tzinfo=UTC)
SHA_A = "a" * 64
SHA_C = "c" * 64
GLOBAL_ENDPOINT_ORIGIN_FINGERPRINT = (
    "6836a1bf89b42ed7a325b83c7825e0ae3a17048d4d7395d0606ee18f2800f71c"
)
RUNNER = CliRunner()


@dataclass
class _Store:
    current: TenantAiPolicyOperatorSnapshot | None = None
    writes: list[TenantAiPolicyWrite] = field(default_factory=list)

    def inspect(self, workspace_id: str) -> TenantAiPolicyOperatorSnapshot | None:
        assert workspace_id == "workspace-alpha"
        return self.current

    def apply(self, change: TenantAiPolicyWrite) -> TenantAiPolicyOperatorSnapshot:
        self.writes.append(change)
        self.current = TenantAiPolicyOperatorSnapshot(
            workspace_id=change.workspace_id,
            version=change.expected_version + 1,
            external_ai_enabled=change.external_ai_enabled,
            provider_governance_accepted=change.provider_governance_accepted,
            provider_governance_fingerprint=change.provider_governance_fingerprint,
            provider_governance_accepted_at=(NOW if change.provider_governance_accepted else None),
            model_snapshot=change.model_snapshot,
            endpoint_region=change.endpoint_region,
            endpoint_origin_fingerprint=change.endpoint_origin_fingerprint,
            configuration_fingerprint=change.configuration_fingerprint,
            requests_per_minute=change.requests_per_minute,
            daily_input_token_limit=change.daily_input_token_limit,
            daily_output_token_limit=change.daily_output_token_limit,
            concurrent_attempt_limit=change.concurrent_attempt_limit,
            reservation_lease_seconds=change.reservation_lease_seconds,
            audit_retention_seconds=change.audit_retention_seconds,
            updated_by=change.updated_by,
            updated_at=NOW,
        )
        return self.current


def _patch_operator(
    monkeypatch: pytest.MonkeyPatch,
    store: _Store,
) -> None:
    operator = TenantAiPolicyOperator(store)
    monkeypatch.setattr(
        ai_policy_main,
        "resolve_control_operator_actor",
        lambda _actor, *, required_role: (
            "platform-admin"
            if required_role == "platform_admin"
            else pytest.fail("unexpected operator role")
        ),
    )
    monkeypatch.setattr(
        ai_policy_main,
        "build_tenant_ai_policy_operator",
        lambda: operator,
    )


def _prepare_args() -> list[str]:
    return [
        "prepare",
        "--workspace-id",
        "workspace-alpha",
        "--expected-version",
        "0",
        "--external-ai-enabled",
        "--provider-governance-accepted",
        "--provider-governance-fingerprint",
        SHA_A,
        "--model-snapshot",
        "gpt-5-nano-2025-08-07",
        "--endpoint-region",
        "global",
        "--endpoint-origin-fingerprint",
        GLOBAL_ENDPOINT_ORIGIN_FINGERPRINT,
        "--configuration-fingerprint",
        SHA_C,
    ]


def test_cli_prepare_is_read_only_then_apply_requires_exact_file_and_phrase(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _Store()
    _patch_operator(monkeypatch, store)

    prepared = RUNNER.invoke(app, _prepare_args())
    assert prepared.exit_code == 0, prepared.output
    document = json.loads(prepared.stdout)
    assert document["writes_performed"] is False
    assert store.writes == []
    proposal_file = tmp_path / "tenant-ai-policy.json"
    proposal_file.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    proposal_file.chmod(0o600)

    applied = RUNNER.invoke(
        app,
        [
            "apply",
            "--proposal-file",
            str(proposal_file),
            "--expected-proposal-fingerprint",
            document["proposal"]["fingerprint"],
            "--confirm",
            "APPLY TENANT AI POLICY",
        ],
    )

    assert applied.exit_code == 0, applied.output
    payload = json.loads(applied.stdout)
    assert payload["writes_performed"] is True
    assert payload["immutable_revision_recorded"] is True
    assert payload["policy"]["version"] == 1
    assert len(store.writes) == 1


def test_cli_rejects_tampered_fingerprint_and_duplicate_json_before_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _Store()
    _patch_operator(monkeypatch, store)
    prepared = RUNNER.invoke(app, _prepare_args())
    document = json.loads(prepared.stdout)
    proposal_file = tmp_path / "proposal.json"
    proposal_file.write_text(json.dumps(document), encoding="utf-8")
    proposal_file.chmod(0o600)

    mismatch = RUNNER.invoke(
        app,
        [
            "apply",
            "--proposal-file",
            str(proposal_file),
            "--expected-proposal-fingerprint",
            "f" * 64,
            "--confirm",
            "APPLY TENANT AI POLICY",
        ],
    )
    assert mismatch.exit_code == 1
    assert json.loads(mismatch.stdout)["code"] == ("tenant_ai_policy_confirmation_mismatch")
    assert store.writes == []

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"kind":"tenant_ai_policy_proposal","kind":"duplicate",'
        '"proposal":{},"writes_performed":false}',
        encoding="utf-8",
    )
    duplicate.chmod(0o600)
    rejected = RUNNER.invoke(
        app,
        [
            "apply",
            "--proposal-file",
            str(duplicate),
            "--expected-proposal-fingerprint",
            "f" * 64,
            "--confirm",
            "APPLY TENANT AI POLICY",
        ],
    )
    assert rejected.exit_code == 1
    assert store.writes == []


def test_cli_help_and_proposal_are_secret_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store()
    _patch_operator(monkeypatch, store)

    help_result = RUNNER.invoke(app, ["--help"])
    prepared = RUNNER.invoke(app, _prepare_args())

    assert help_result.exit_code == 0
    assert "OPENAI_API_KEY" not in help_result.stdout
    assert "api-key" not in help_result.stdout.lower()
    payload = json.loads(prepared.stdout)
    proposal_text = json.dumps(payload, sort_keys=True)
    assert "https://" not in proposal_text
    assert "sk-" not in proposal_text
    assert payload["proposal"]["desired"] == asdict(
        TenantAiPolicyDesired(
            external_ai_enabled=True,
            provider_governance_accepted=True,
            provider_governance_fingerprint=SHA_A,
            model_snapshot="gpt-5-nano-2025-08-07",
            endpoint_region="global",
            endpoint_origin_fingerprint=GLOBAL_ENDPOINT_ORIGIN_FINGERPRINT,
            configuration_fingerprint=SHA_C,
            requests_per_minute=20,
            daily_input_token_limit=100_000,
            daily_output_token_limit=20_000,
            concurrent_attempt_limit=2,
            reservation_lease_seconds=60,
            audit_retention_seconds=2_592_000,
        )
    )


def test_cli_defaults_to_global_and_rejects_a_caller_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store()
    _patch_operator(monkeypatch, store)
    default_args = _prepare_args()
    region_index = default_args.index("--endpoint-region")
    del default_args[region_index : region_index + 2]

    prepared = RUNNER.invoke(app, default_args)

    assert prepared.exit_code == 0, prepared.output
    assert json.loads(prepared.stdout)["proposal"]["desired"]["endpoint_region"] == "global"
    assert store.writes == []

    caller_url_args = _prepare_args()
    caller_url_args[caller_url_args.index("--endpoint-region") + 1] = "https://attacker.invalid/v1"
    rejected = RUNNER.invoke(app, caller_url_args)

    assert rejected.exit_code == 1
    assert store.writes == []


def test_cli_prepare_can_create_owner_only_proposal_without_policy_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _Store()
    _patch_operator(monkeypatch, store)
    proposal_file = tmp_path / "tenant-ai-policy.json"

    prepared = RUNNER.invoke(
        app,
        [*_prepare_args(), "--proposal-output", str(proposal_file)],
    )

    assert prepared.exit_code == 0, prepared.output
    summary = json.loads(prepared.stdout)
    assert summary == {
        "kind": "tenant_ai_policy_proposal_file",
        "proposal_file": str(proposal_file),
        "proposal_file_written": True,
        "proposal_fingerprint": json.loads(proposal_file.read_text())["proposal"]["fingerprint"],
        "writes_performed": False,
    }
    assert stat.S_IMODE(proposal_file.stat().st_mode) == 0o600
    assert store.writes == []

    repeated = RUNNER.invoke(
        app,
        [*_prepare_args(), "--proposal-output", str(proposal_file)],
    )
    assert repeated.exit_code == 1
    assert proposal_file.read_bytes()
    assert store.writes == []


def test_cli_apply_rejects_non_owner_only_proposal_before_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _Store()
    _patch_operator(monkeypatch, store)
    prepared = RUNNER.invoke(app, _prepare_args())
    document = json.loads(prepared.stdout)
    proposal_file = tmp_path / "proposal.json"
    proposal_file.write_text(json.dumps(document), encoding="utf-8")
    proposal_file.chmod(0o640)

    rejected = RUNNER.invoke(
        app,
        [
            "apply",
            "--proposal-file",
            str(proposal_file),
            "--expected-proposal-fingerprint",
            document["proposal"]["fingerprint"],
            "--confirm",
            "APPLY TENANT AI POLICY",
        ],
    )

    assert rejected.exit_code == 1
    assert store.writes == []
