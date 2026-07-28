from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from schemabridge.adapters.control_plane.identity_evidence import (
    IdentityEvidenceError,
    IdentityEvidenceErrorCode,
    IdentityEvidenceFileReader,
    SignedIdentityEvidenceEnvelope,
    build_signed_identity_evidence,
)
from schemabridge.domain.identity import AuthenticationMethod
from schemabridge.domain.identity_rotation import (
    VerifiedDualKeyOidcDerivation,
    VerifiedOidcKeyDerivation,
)

NOW = datetime(2026, 7, 23, 20, 0, tzinfo=UTC)
KEY = b"unit-test-identity-evidence-key-0123456789"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _opaque(kind: str, version: str, label: str) -> str:
    return f"sb_{kind}_{version}_{_digest(f'{kind}:{version}:{label}')}"


def _evidence() -> VerifiedDualKeyOidcDerivation:
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
            actor_id=_opaque("actor", "v1", "actor"),
            key_version="v1",
        ),
        current=VerifiedOidcKeyDerivation(
            **shared,
            workspace_id=_opaque("workspace", "v2", "workspace"),
            actor_id=_opaque("actor", "v2", "actor"),
            key_version="v2",
        ),
    )


def _write(path: Path, payload: str) -> None:
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o600)


def _envelope() -> SignedIdentityEvidenceEnvelope:
    return build_signed_identity_evidence(
        (_evidence(),),
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=5),
        nonce=_digest("nonce"),
        signature_key=KEY,
        signature_key_version="v1",
    )


def test_owner_only_signed_evidence_round_trips_without_raw_claims(tmp_path: Path) -> None:
    envelope = _envelope()
    path = tmp_path / "identity-evidence.json"
    _write(path, envelope.model_dump_json())

    loaded = IdentityEvidenceFileReader(
        path,
        signing_keys={"v1": KEY},
        clock=lambda: NOW,
    ).read(expected_fingerprint=envelope.payload_fingerprint)

    assert loaded == envelope
    rendered = path.read_text(encoding="utf-8")
    for forbidden in ("email", '"sub"', "tenant_id", "access_token", "id_token"):
        assert forbidden not in rendered


@pytest.mark.parametrize(
    ("mutation", "code"),
    (
        ("signature", IdentityEvidenceErrorCode.SIGNATURE_INVALID),
        ("fingerprint", IdentityEvidenceErrorCode.FINGERPRINT_MISMATCH),
        ("expired", IdentityEvidenceErrorCode.EXPIRED),
    ),
)
def test_signature_fingerprint_and_expiry_fail_closed(
    tmp_path: Path,
    mutation: str,
    code: IdentityEvidenceErrorCode,
) -> None:
    envelope = _envelope()
    path = tmp_path / "identity-evidence.json"
    payload = envelope.model_dump(mode="json")
    if mutation == "signature":
        payload["signature"] = "f" * 64
    _write(path, json.dumps(payload))
    clock = (lambda: envelope.expires_at) if mutation == "expired" else (lambda: NOW)
    expected = "0" * 64 if mutation == "fingerprint" else envelope.payload_fingerprint

    with pytest.raises(IdentityEvidenceError) as raised:
        IdentityEvidenceFileReader(
            path,
            signing_keys={"v1": KEY},
            clock=clock,
        ).read(expected_fingerprint=expected)
    assert raised.value.code is code


def test_symlink_permissions_duplicates_and_raw_claim_fields_are_rejected(
    tmp_path: Path,
) -> None:
    envelope = _envelope()
    target = tmp_path / "identity-evidence.json"
    _write(target, envelope.model_dump_json())
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(IdentityEvidenceError) as symlink:
        IdentityEvidenceFileReader(
            link,
            signing_keys={"v1": KEY},
            clock=lambda: NOW,
        ).read()
    assert symlink.value.code is IdentityEvidenceErrorCode.FILE_UNSAFE

    target.chmod(0o640)
    with pytest.raises(IdentityEvidenceError) as permissions:
        IdentityEvidenceFileReader(
            target,
            signing_keys={"v1": KEY},
            clock=lambda: NOW,
        ).read()
    assert permissions.value.code is IdentityEvidenceErrorCode.FILE_UNSAFE

    duplicate = tmp_path / "duplicate.json"
    _write(
        duplicate,
        envelope.model_dump_json()[:-1] + ',"format_version":1}',
    )
    with pytest.raises(IdentityEvidenceError) as repeated:
        IdentityEvidenceFileReader(
            duplicate,
            signing_keys={"v1": KEY},
            clock=lambda: NOW,
        ).read()
    assert repeated.value.code is IdentityEvidenceErrorCode.PAYLOAD_INVALID

    raw_claim = tmp_path / "raw-claim.json"
    payload = envelope.model_dump(mode="json")
    payload["sub"] = "raw-subject"
    _write(raw_claim, json.dumps(payload))
    with pytest.raises(IdentityEvidenceError) as raw:
        IdentityEvidenceFileReader(
            raw_claim,
            signing_keys={"v1": KEY},
            clock=lambda: NOW,
        ).read()
    assert raw.value.code is IdentityEvidenceErrorCode.PAYLOAD_INVALID
