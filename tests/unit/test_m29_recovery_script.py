from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from scripts import m29_recovery

from schemabridge.adapters.control_plane.postgres_operations import _signed_manifest

ROOT = Path(__file__).resolve().parents[2]
KEY_TEXT = "m29-recovery-script-key-0123456789-abcdef"


def _key_file(root: Path, *, mode: int = 0o600) -> Path:
    path = root / "recovery-key.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "schemabridge.recovery-audit-key.v1",
                "key_version": "v1",
                "key": KEY_TEXT,
            }
        ),
        encoding="utf-8",
    )
    path.chmod(mode)
    return path


def _artifact(root: Path, created_at: datetime, *, suffix: str) -> None:
    timestamp = created_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    stem = f"control-plane-{timestamp}-{suffix}"
    archive = root / f"{stem}.dump"
    archive.write_bytes(f"synthetic archive {stem}".encode())
    archive.chmod(0o600)
    manifest = _signed_manifest(
        source_database_fingerprint="a" * 64,
        schema_version=10,
        schema_checksum="b" * 64,
        archive_name=archive.name,
        archive_size_bytes=archive.stat().st_size,
        archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        state_sha256=hashlib.sha256(f"state:{stem}".encode()).hexdigest(),
        table_counts={"schema_migrations": 9},
        key_version="v1",
        key=KEY_TEXT.encode(),
        created_at=created_at,
    )
    manifest_path = root / f"{stem}.manifest.json"
    manifest_path.write_text(json.dumps(manifest.model_dump(mode="json")), encoding="utf-8")
    manifest_path.chmod(0o600)


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (sys.executable, "scripts/m29_recovery.py", *arguments),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_policy_check_subprocess_returns_only_bounded_sanitized_json() -> None:
    result = _run("policy-check")

    assert result.returncode == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload == {
        "command": "policy-check",
        "outcome": "passed",
        "policy_fingerprint": m29_recovery.validate_policy_document(
            ROOT / "deploy/recovery/policy.yaml"
        ).fingerprint,
        "schema_version": "schemabridge.recovery-operator.v1",
    }
    assert str(ROOT) not in result.stdout


def test_policy_validator_rejects_duplicate_or_weaker_documents_without_echo(
    tmp_path: Path,
) -> None:
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "schema_version: schemabridge.recovery-policy.v1\nschema_version: attacker-controlled\n",
        encoding="utf-8",
    )

    with pytest.raises(m29_recovery.RecoveryOperatorError) as raised:
        m29_recovery.validate_policy_document(policy)

    assert str(raised.value) == "recovery operator request rejected"
    assert "attacker" not in str(raised.value)
    assert str(tmp_path) not in str(raised.value)


def test_retention_plan_subprocess_reads_owner_only_key_and_leaks_no_secret_or_path(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(mode=0o700)
    key_file = _key_file(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)
    _artifact(artifact_root, now, suffix="000000000001")

    result = _run(
        "retention-plan",
        "--root",
        str(artifact_root),
        "--key-file",
        str(key_file),
    )

    assert result.returncode == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["command"] == "retention-plan"
    assert payload["outcome"] == "planned"
    assert payload["dry_run"] is True
    assert payload["fresh"] is True
    assert payload["verified_pairs"] == 1
    assert payload["retained_pairs"] == 1
    assert payload["expired_pairs"] == 0
    encoded = result.stdout + result.stderr
    assert KEY_TEXT not in encoded
    assert str(key_file) not in encoded
    assert str(artifact_root) not in encoded
    assert "control-plane-" not in encoded


@pytest.mark.parametrize("unsafe", ["permissions", "symlink", "key-in-argv"])
def test_key_boundary_and_cli_fail_closed_without_leak(
    tmp_path: Path,
    unsafe: str,
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(mode=0o700)
    now = datetime.now(UTC).replace(microsecond=0)
    _artifact(artifact_root, now, suffix="000000000001")
    key_file = _key_file(tmp_path)
    if unsafe == "permissions":
        key_file.chmod(0o640)
    elif unsafe == "symlink":
        target = key_file
        key_file = tmp_path / "linked-key.json"
        key_file.symlink_to(target)
    else:
        result = _run(
            "retention-plan",
            "--root",
            str(artifact_root),
            "--key-file",
            str(key_file),
            "--key",
            KEY_TEXT,
        )
        assert result.returncode != 0
        assert KEY_TEXT not in result.stdout + result.stderr
        return

    result = _run(
        "retention-plan",
        "--root",
        str(artifact_root),
        "--key-file",
        str(key_file),
    )

    assert result.returncode != 0
    payload = json.loads(result.stderr)
    assert payload["code"] == "recovery_key_invalid"
    encoded = result.stdout + result.stderr
    assert KEY_TEXT not in encoded
    assert str(key_file) not in encoded
    assert str(artifact_root) not in encoded


def test_key_material_is_excluded_from_value_repr() -> None:
    audit_key = m29_recovery.RecoveryAuditKey(
        version="v1",
        key=KEY_TEXT.encode(),
    )

    assert KEY_TEXT not in repr(audit_key)


def test_execute_requires_reviewed_fingerprints_and_quarantines_expired_pairs(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(mode=0o700)
    key_file = _key_file(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)
    for index in range(50):
        _artifact(
            artifact_root,
            now - timedelta(days=index),
            suffix=f"{index:012x}",
        )

    reviewed = _run(
        "retention-plan",
        "--root",
        str(artifact_root),
        "--key-file",
        str(key_file),
    )
    assert reviewed.returncode == 0
    reviewed_payload = json.loads(reviewed.stdout)

    denied = _run(
        "retention-plan",
        "--root",
        str(artifact_root),
        "--key-file",
        str(key_file),
        "--execute",
        "--confirmation",
        "yes",
    )
    assert denied.returncode != 0
    assert json.loads(denied.stderr)["code"] == "retention_confirmation_invalid"
    before = {path.name for path in artifact_root.iterdir()}

    accepted = _run(
        "retention-plan",
        "--root",
        str(artifact_root),
        "--key-file",
        str(key_file),
        "--execute",
        "--confirmation",
        m29_recovery.RETENTION_QUARANTINE_CONFIRMATION,
        "--reviewed-policy-fingerprint",
        reviewed_payload["policy_fingerprint"],
        "--reviewed-plan-fingerprint",
        reviewed_payload["plan_fingerprint"],
    )

    assert accepted.returncode == 0
    payload = json.loads(accepted.stdout)
    assert payload["outcome"] == "executed"
    assert payload["dry_run"] is False
    assert payload["quarantined_pairs"] > 0
    assert payload["retained_pairs"] > 0
    assert len({path.name for path in artifact_root.iterdir()}) < len(before)
    quarantine = (
        artifact_root / ".schemabridge-retention-quarantine" / reviewed_payload["plan_fingerprint"]
    )
    assert quarantine.is_dir()
    assert len(tuple(quarantine.iterdir())) == payload["quarantined_pairs"] * 2
    encoded = accepted.stdout + accepted.stderr
    assert KEY_TEXT not in encoded
    assert str(key_file) not in encoded
    assert str(artifact_root) not in encoded


def test_execute_rejects_unreviewed_or_changed_plan_fingerprint_without_moving_files(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(mode=0o700)
    key_file = _key_file(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)
    for index in range(50):
        _artifact(
            artifact_root,
            now - timedelta(days=index),
            suffix=f"{index:012x}",
        )
    planned = _run(
        "retention-plan",
        "--root",
        str(artifact_root),
        "--key-file",
        str(key_file),
    )
    plan_payload = json.loads(planned.stdout)
    before = {path.name for path in artifact_root.iterdir()}

    rejected = _run(
        "retention-plan",
        "--root",
        str(artifact_root),
        "--key-file",
        str(key_file),
        "--execute",
        "--confirmation",
        m29_recovery.RETENTION_QUARANTINE_CONFIRMATION,
        "--reviewed-policy-fingerprint",
        plan_payload["policy_fingerprint"],
        "--reviewed-plan-fingerprint",
        "f" * 64,
    )

    assert rejected.returncode != 0
    assert json.loads(rejected.stderr)["code"] == "backup_retention_rejected"
    assert {path.name for path in artifact_root.iterdir()} == before


def test_script_has_no_database_or_source_runtime_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_connect(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("operator script must not connect to a database")

    monkeypatch.setattr(
        "schemabridge.adapters.control_plane.postgres_operations.psycopg.connect",
        forbidden_connect,
    )

    policy = m29_recovery.run_policy_check()
    assert policy["outcome"] == "passed"


def test_makefile_exposes_only_explicit_m29_recovery_help_and_policy_targets() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "m29-recovery-help:" in makefile
    assert "m29-recovery-policy-check:" in makefile
    assert "$(BIN)/python scripts/m29_recovery.py policy-check" in makefile
    check_line = next(line for line in makefile.splitlines() if line.startswith("check:"))
    assert "m29-recovery" not in check_line
