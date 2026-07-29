from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from click import unstyle
from typer.testing import CliRunner

import schemabridge.entrypoints.cli.main as cli_module
from schemabridge.adapters.control_plane.identity_evidence import (
    IdentityEvidenceError,
    IdentityEvidenceErrorCode,
    SignedIdentityEvidenceEnvelope,
    build_signed_identity_evidence,
)
from schemabridge.domain.identity import AuthenticationMethod
from schemabridge.domain.identity_rotation import (
    ApprovedIdentityRotation,
    IdentityInitializationConfirmation,
    IdentityRotationConfirmation,
    IdentityRotationExecutionResult,
    IdentityRotationState,
    VerifiedDualKeyOidcDerivation,
    VerifiedOidcKeyDerivation,
    build_identity_rotation_approval,
    build_identity_rotation_completion,
    build_identity_rotation_plan,
)
from schemabridge.entrypoints.cli.main import app

NOW = datetime(2026, 7, 23, 21, 0, tzinfo=UTC)
EVIDENCE_KEY = b"unit-test-identity-evidence-key-0123456789"
ACTOR = "sb_actor_v1_" + hashlib.sha256(b"operator").hexdigest()
runner = CliRunner()


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _opaque(kind: str, version: str, label: str) -> str:
    return f"sb_{kind}_{version}_{_digest(f'{kind}:{version}:{label}')}"


def _derivation() -> VerifiedDualKeyOidcDerivation:
    shared = {
        "verification_id": _digest("verification"),
        "workspace_reference_digest": _digest("workspace-reference"),
        "actor_reference_digest": _digest("actor-reference"),
        "provenance_fingerprint": _digest("provider-policy"),
        "provenance_version": 1,
        "policy_version": 1,
        "verified_at": NOW,
        "authentication_method": AuthenticationMethod.OIDC,
    }
    return VerifiedDualKeyOidcDerivation(
        previous=VerifiedOidcKeyDerivation(
            **shared,
            workspace_id=_opaque("workspace", "v1", "workspace"),
            actor_id=_opaque("actor", "v1", "owner"),
            key_version="v1",
        ),
        current=VerifiedOidcKeyDerivation(
            **shared,
            workspace_id=_opaque("workspace", "v2", "workspace"),
            actor_id=_opaque("actor", "v2", "owner"),
            key_version="v2",
        ),
    )


def _envelope() -> SignedIdentityEvidenceEnvelope:
    return build_signed_identity_evidence(
        (_derivation(),),
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
        nonce=_digest("nonce"),
        signature_key=EVIDENCE_KEY,
        signature_key_version="v1",
    )


def _state(envelope: SignedIdentityEvidenceEnvelope) -> IdentityRotationState:
    source = envelope.derivations[0].previous
    return IdentityRotationState(
        workspace_id=source.workspace_id,
        workspace_reference_digest=source.workspace_reference_digest,
        active_key_version=source.key_version,
        provenance_version=source.provenance_version,
        policy_version=source.policy_version,
        revision=0,
        owner_actor_ids=(source.actor_id,),
    )


class _Reader:
    def __init__(self, envelope: SignedIdentityEvidenceEnvelope) -> None:
        self.envelope = envelope
        self.expected_fingerprints: list[str | None] = []

    def read(
        self,
        *,
        expected_fingerprint: str | None = None,
    ) -> SignedIdentityEvidenceEnvelope:
        self.expected_fingerprints.append(expected_fingerprint)
        if (
            expected_fingerprint is not None
            and expected_fingerprint != self.envelope.payload_fingerprint
        ):
            raise IdentityEvidenceError(
                IdentityEvidenceErrorCode.FINGERPRINT_MISMATCH,
                "identity evidence does not match the reviewed fingerprint",
            )
        return self.envelope


def _patch_common(
    monkeypatch: pytest.MonkeyPatch,
    reader: _Reader,
    services: object,
) -> None:
    monkeypatch.setattr(cli_module, "resolve_runtime_profile", lambda: "development")
    monkeypatch.setattr(
        cli_module,
        "build_identity_evidence_reader",
        lambda *_args, **_kwargs: reader,
        raising=False,
    )
    monkeypatch.setattr(
        cli_module,
        "build_identity_rotation_services",
        lambda *_args, **_kwargs: services,
        raising=False,
    )
    monkeypatch.setattr(
        cli_module,
        "_control_operator_actor",
        lambda actor, *, required_role: ACTOR,
    )


def _command(
    name: str,
    envelope: SignedIdentityEvidenceEnvelope,
    *extra: str,
) -> list[str]:
    return [
        "control-plane",
        "identity",
        name,
        "--evidence-file",
        "identity-evidence.json",
        *extra,
        "--json",
    ]


def _assert_bounded_identity_output(
    output: str,
    envelope: SignedIdentityEvidenceEnvelope,
) -> None:
    assert len(output.encode()) < 4096
    lowered = output.lower()
    for forbidden in (
        "derivations",
        "verification_id",
        "authentication_method",
        "claims",
        "subject",
        "email",
        "token",
    ):
        assert forbidden not in lowered
    assert envelope.derivations[0].previous.verification_id not in output
    assert envelope.signature not in output
    assert envelope.nonce not in output


def test_identity_operator_help_exposes_only_file_and_opaque_approval_inputs() -> None:
    expected_options = {
        "inspect-evidence": {"--evidence-file"},
        "initialize": {
            "--evidence-file",
            "--evidence-fingerprint",
            "--approved-at",
            "--confirm",
        },
        "prepare": {"--evidence-file", "--evidence-fingerprint"},
        "approve": {
            "--evidence-file",
            "--evidence-fingerprint",
            "--plan-fingerprint",
            "--approved-at",
            "--confirm",
        },
        "complete": {
            "--evidence-file",
            "--evidence-fingerprint",
            "--plan-fingerprint",
            "--approval-id",
            "--completed-at",
        },
    }

    for command, options in expected_options.items():
        result = runner.invoke(
            app,
            ["control-plane", "identity", command, "--help"],
            terminal_width=180,
        )

        assert result.exit_code == 0, result.output
        help_output = unstyle(result.stdout)
        help_tokens = set(help_output.split())
        for option in options:
            assert any(token.startswith(option[:16]) for token in help_tokens)
        for forbidden in (
            "--dsn",
            "--token",
            "--key",
            "--claims",
            "--subject",
            "--email",
        ):
            assert forbidden not in help_output
        if command == "complete":
            assert "--actor" not in help_output


def test_inspect_evidence_emits_only_bounded_non_secret_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = _envelope()
    reader = _Reader(envelope)
    _patch_common(monkeypatch, reader, SimpleNamespace())

    result = runner.invoke(
        app,
        _command("inspect-evidence", envelope),
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["workspace_id"] == envelope.workspace_id
    assert (
        payload.get("evidence_fingerprint", payload.get("payload_fingerprint"))
        == envelope.payload_fingerprint
    )
    assert payload["owner_count"] == len(envelope.derivations)
    assert reader.expected_fingerprints == [None]
    _assert_bounded_identity_output(result.stdout, envelope)


def test_initialize_verifies_exact_evidence_before_typed_approved_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = _envelope()
    reader = _Reader(envelope)
    state = _state(envelope)
    captured: dict[str, object] = {}

    class _Initialize:
        def execute(
            self,
            derivations: tuple[VerifiedDualKeyOidcDerivation, ...],
            *,
            evidence_fingerprint: str,
            actor: str,
            approved_at: datetime,
            confirmation: IdentityInitializationConfirmation,
        ) -> IdentityRotationState:
            captured.update(
                derivations=derivations,
                evidence_fingerprint=evidence_fingerprint,
                actor=actor,
                approved_at=approved_at,
                confirmation=confirmation,
            )
            return state

    _patch_common(
        monkeypatch,
        reader,
        SimpleNamespace(initialize=_Initialize()),
    )
    result = runner.invoke(
        app,
        _command(
            "initialize",
            envelope,
            "--evidence-fingerprint",
            envelope.payload_fingerprint,
            "--approved-at",
            NOW.isoformat(),
            "--confirm",
            IdentityInitializationConfirmation.INITIALIZE_VERIFIED_OIDC_BINDINGS.value,
        ),
    )

    assert result.exit_code == 0, result.output
    assert reader.expected_fingerprints == [envelope.payload_fingerprint]
    assert captured["derivations"] == envelope.derivations
    assert captured["evidence_fingerprint"] == envelope.payload_fingerprint
    assert captured["actor"] == ACTOR
    assert captured["approved_at"] == NOW
    assert captured["confirmation"] is (
        IdentityInitializationConfirmation.INITIALIZE_VERIFIED_OIDC_BINDINGS
    )
    _assert_bounded_identity_output(result.stdout, envelope)


def test_initialize_rejects_changed_evidence_before_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = _envelope()
    reader = _Reader(envelope)
    writes = 0

    class _Initialize:
        def execute(self, *_args: object, **_kwargs: object) -> object:
            nonlocal writes
            writes += 1
            raise AssertionError("initialization must not run for changed evidence")

    _patch_common(
        monkeypatch,
        reader,
        SimpleNamespace(initialize=_Initialize()),
    )
    result = runner.invoke(
        app,
        _command(
            "initialize",
            envelope,
            "--evidence-fingerprint",
            "0" * 64,
            "--approved-at",
            NOW.isoformat(),
            "--confirm",
            IdentityInitializationConfirmation.INITIALIZE_VERIFIED_OIDC_BINDINGS.value,
        ),
    )

    assert result.exit_code != 0
    assert writes == 0
    assert reader.expected_fingerprints == ["0" * 64]
    _assert_bounded_identity_output(result.stdout, envelope)


def test_prepare_and_approve_bind_the_exact_fresh_plan_and_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = _envelope()
    reader = _Reader(envelope)
    state = _state(envelope)
    plan = build_identity_rotation_plan(state, envelope.derivations)
    captured: dict[str, object] = {}

    class _Prepare:
        def execute(
            self,
            workspace_id: str,
            derivations: tuple[VerifiedDualKeyOidcDerivation, ...],
        ):
            captured.update(
                prepared_workspace=workspace_id,
                prepared_derivations=derivations,
            )
            return plan

    class _Approve:
        def execute(
            self,
            supplied_plan: object,
            *,
            actor: str,
            approved_at: datetime,
            confirmation: IdentityRotationConfirmation,
        ) -> ApprovedIdentityRotation:
            captured.update(
                approved_plan=supplied_plan,
                actor=actor,
                approved_at=approved_at,
                confirmation=confirmation,
            )
            approval = build_identity_rotation_approval(
                plan,
                actor=actor,
                approved_at=approved_at,
                confirmation=confirmation,
            )
            return ApprovedIdentityRotation(plan=plan, approval=approval)

    services = SimpleNamespace(prepare=_Prepare(), approve=_Approve())
    _patch_common(monkeypatch, reader, services)
    prepared = runner.invoke(
        app,
        _command(
            "prepare",
            envelope,
            "--evidence-fingerprint",
            envelope.payload_fingerprint,
        ),
    )
    approved = runner.invoke(
        app,
        _command(
            "approve",
            envelope,
            "--evidence-fingerprint",
            envelope.payload_fingerprint,
            "--plan-fingerprint",
            plan.fingerprint,
            "--approved-at",
            (NOW + timedelta(minutes=1)).isoformat(),
            "--confirm",
            IdentityRotationConfirmation.ROTATE_VERIFIED_OIDC_BINDINGS.value,
        ),
    )

    assert prepared.exit_code == 0, prepared.output
    assert approved.exit_code == 0, approved.output
    assert json.loads(prepared.stdout)["plan_fingerprint"] == plan.fingerprint
    assert captured["prepared_workspace"] == envelope.workspace_id
    assert captured["prepared_derivations"] == envelope.derivations
    assert captured["approved_plan"] == plan
    assert captured["actor"] == ACTOR
    assert captured["confirmation"] is (IdentityRotationConfirmation.ROTATE_VERIFIED_OIDC_BINDINGS)
    assert reader.expected_fingerprints == [
        envelope.payload_fingerprint,
        envelope.payload_fingerprint,
    ]
    _assert_bounded_identity_output(prepared.stdout, envelope)
    _assert_bounded_identity_output(approved.stdout, envelope)

    captured.pop("approved_plan")
    stale = runner.invoke(
        app,
        _command(
            "approve",
            envelope,
            "--evidence-fingerprint",
            envelope.payload_fingerprint,
            "--plan-fingerprint",
            "0" * 64,
            "--approved-at",
            (NOW + timedelta(minutes=1)).isoformat(),
            "--confirm",
            IdentityRotationConfirmation.ROTATE_VERIFIED_OIDC_BINDINGS.value,
        ),
    )
    assert stale.exit_code != 0
    assert json.loads(stale.stdout)["code"] == "identity_rotation_approval_mismatch"
    assert "approved_plan" not in captured


def test_complete_uses_reserved_approval_id_actor_and_reports_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = _envelope()
    reader = _Reader(envelope)
    plan = build_identity_rotation_plan(_state(envelope), envelope.derivations)
    approved_at = NOW + timedelta(minutes=1)
    approval = build_identity_rotation_approval(
        plan,
        actor=ACTOR,
        approved_at=approved_at,
        confirmation=IdentityRotationConfirmation.ROTATE_VERIFIED_OIDC_BINDINGS,
    )
    approved = ApprovedIdentityRotation(plan=plan, approval=approval)
    completed_at = NOW + timedelta(minutes=2)
    completion = build_identity_rotation_completion(
        approved,
        completed_at=completed_at,
    )
    captured: dict[str, object] = {}

    class _Resolve:
        def execute(
            self,
            supplied_fingerprint: str,
            derivations: tuple[VerifiedDualKeyOidcDerivation, ...],
        ):
            captured.update(
                resolved_fingerprint=supplied_fingerprint,
                resolved_derivations=derivations,
            )
            return plan

    class _Complete:
        def execute(
            self,
            supplied_plan: object,
            *,
            approval_id: str,
            actor: str,
            completed_at: datetime,
        ) -> IdentityRotationExecutionResult:
            captured.update(
                plan=supplied_plan,
                approval_id=approval_id,
                actor=actor,
                completed_at=completed_at,
            )
            return IdentityRotationExecutionResult(
                completion=completion,
                replayed=True,
            )

    _patch_common(
        monkeypatch,
        reader,
        SimpleNamespace(resolve=_Resolve(), complete=_Complete()),
    )
    result = runner.invoke(
        app,
        _command(
            "complete",
            envelope,
            "--evidence-fingerprint",
            envelope.payload_fingerprint,
            "--plan-fingerprint",
            plan.fingerprint,
            "--approval-id",
            approval.id,
            "--completed-at",
            completed_at.isoformat(),
        ),
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["approval_id"] == approval.id
    assert payload["replayed"] is True
    assert captured == {
        "plan": plan,
        "approval_id": approval.id,
        "actor": ACTOR,
        "completed_at": completed_at,
        "resolved_fingerprint": plan.fingerprint,
        "resolved_derivations": envelope.derivations,
    }
    _assert_bounded_identity_output(result.stdout, envelope)
