from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from schemabridge.adapters.backup.retention import (
    RETENTION_QUARANTINE_CONFIRMATION,
    BackupRetentionError,
    BackupRetentionExecutionEvidence,
    BackupRetentionPlan,
    VerifiedBackupRetentionPlanner,
)
from schemabridge.adapters.control_plane.postgres_operations import _signed_manifest
from schemabridge.application.recovery import RecoveryPolicy

KEY = b"recovery-retention-key-0123456789-abcdef"
NOW = datetime(2026, 7, 29, 12, 30, tzinfo=UTC)


def _artifact(
    root: Path,
    created_at: datetime,
    *,
    suffix: str,
) -> tuple[Path, Path]:
    timestamp = created_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    stem = f"control-plane-{timestamp}-{suffix}"
    archive = root / f"{stem}.dump"
    archive.write_bytes(f"synthetic archive {stem}".encode())
    os.chmod(archive, 0o600)
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
        key=KEY,
        created_at=created_at,
    )
    manifest_path = root / f"{stem}.manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.model_dump(mode="json")),
        encoding="utf-8",
    )
    os.chmod(manifest_path, 0o600)
    return archive, manifest_path


def _series(root: Path, *, count: int = 50) -> list[tuple[Path, Path]]:
    return [
        _artifact(
            root,
            NOW - timedelta(days=index),
            suffix=f"{index:012x}",
        )
        for index in range(count)
    ]


def _planner() -> VerifiedBackupRetentionPlanner:
    return VerifiedBackupRetentionPlanner(
        policy=RecoveryPolicy.production_default(),
        audit_signing_keys={"v1": KEY},
        clock=lambda: NOW,
    )


def _apply(
    planner: VerifiedBackupRetentionPlanner,
    plan: BackupRetentionPlan,
    *,
    confirmation: str = RETENTION_QUARANTINE_CONFIRMATION,
    reviewed_policy_fingerprint: str | None = None,
    reviewed_plan_fingerprint: str | None = None,
) -> BackupRetentionExecutionEvidence:
    return planner.apply(
        plan,
        confirmation=confirmation,
        reviewed_policy_fingerprint=(reviewed_policy_fingerprint or plan.policy_fingerprint),
        reviewed_plan_fingerprint=reviewed_plan_fingerprint or plan.plan_fingerprint,
    )


def test_retention_plan_is_verified_deterministic_and_dry_run_by_default(
    tmp_path: Path,
) -> None:
    os.chmod(tmp_path, 0o700)
    pairs = _series(tmp_path)

    first = _planner().plan(tmp_path)
    second = _planner().plan(tmp_path)

    assert first == second
    assert first.dry_run is True
    assert first.fresh is True
    assert first.verified_pairs == len(pairs)
    assert first.retained
    assert first.expired
    assert len(first.retained) + len(first.expired) == len(pairs)
    assert first.retained[0].created_at == NOW
    assert {"hourly", "daily", "monthly"} <= {
        reason for decision in first.retained for reason in decision.reasons
    }
    assert all(decision.action == "retain" for decision in first.retained)
    assert all(decision.action == "expire" for decision in first.expired)
    assert len(first.plan_fingerprint) == 64
    assert str(tmp_path) not in repr(first)
    assert all(archive.exists() and manifest.exists() for archive, manifest in pairs)


@pytest.mark.parametrize("tamper", ["archive", "manifest", "orphan", "symlink", "permissions"])
def test_tampering_or_unpaired_artifacts_fail_the_whole_plan_without_deletion(
    tmp_path: Path,
    tamper: str,
) -> None:
    os.chmod(tmp_path, 0o700)
    archive, manifest = _artifact(tmp_path, NOW, suffix="000000000001")
    if tamper == "archive":
        archive.write_bytes(b"changed")
    elif tamper == "manifest":
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["state_sha256"] = "f" * 64
        manifest.write_text(json.dumps(payload), encoding="utf-8")
    elif tamper == "orphan":
        manifest.unlink()
    elif tamper == "symlink":
        link = tmp_path / "control-plane-20260729T113000Z-000000000002.dump"
        link.symlink_to(archive)
    else:
        os.chmod(archive, 0o640)

    before = {path.name: path.lstat() for path in tmp_path.iterdir()}
    with pytest.raises(BackupRetentionError, match="backup retention operation rejected"):
        _planner().plan(tmp_path)
    after = {path.name: path.lstat() for path in tmp_path.iterdir()}

    assert before.keys() == after.keys()
    assert archive.exists()


def test_stale_backup_blocks_expiration_even_when_all_pairs_are_verified(tmp_path: Path) -> None:
    os.chmod(tmp_path, 0o700)
    _series(tmp_path)
    stale_now = NOW + timedelta(hours=2)
    planner = VerifiedBackupRetentionPlanner(
        policy=RecoveryPolicy.production_default(),
        audit_signing_keys={"v1": KEY},
        clock=lambda: stale_now,
    )

    plan = planner.plan(tmp_path, dry_run=False)

    assert plan.fresh is False
    assert plan.expired == ()
    with pytest.raises(BackupRetentionError, match="backup retention operation rejected"):
        _apply(planner, plan)


def test_apply_requires_reviewed_fingerprints_exact_confirmation_and_revalidation(
    tmp_path: Path,
) -> None:
    os.chmod(tmp_path, 0o700)
    pairs = _series(tmp_path)
    planner = _planner()
    dry_run = planner.plan(tmp_path)
    executable = planner.plan(tmp_path, dry_run=False)

    assert dry_run.plan_fingerprint == executable.plan_fingerprint

    with pytest.raises(BackupRetentionError, match="backup retention operation rejected"):
        planner.apply(
            dry_run,
            confirmation=RETENTION_QUARANTINE_CONFIRMATION,
            reviewed_policy_fingerprint="",
            reviewed_plan_fingerprint="",
        )

    with pytest.raises(BackupRetentionError, match="backup retention operation rejected"):
        _apply(planner, executable, confirmation="yes")

    _artifact(
        tmp_path,
        NOW - timedelta(hours=1),
        suffix="ffffffffffff",
    )
    with pytest.raises(BackupRetentionError, match="backup retention operation rejected"):
        _apply(planner, executable)

    assert all(archive.exists() and manifest.exists() for archive, manifest in pairs)


def test_explicit_apply_quarantines_only_reverified_expired_pairs_and_preserves_recovery(
    tmp_path: Path,
) -> None:
    os.chmod(tmp_path, 0o700)
    _series(tmp_path)
    planner = _planner()
    plan = planner.plan(tmp_path, dry_run=False)
    expired_ids = {item.artifact_id for item in plan.expired}
    retained_ids = {item.artifact_id for item in plan.retained}

    evidence = _apply(planner, plan)

    assert evidence.quarantined_artifact_ids == tuple(sorted(expired_ids))
    assert evidence.retained_artifact_ids == tuple(sorted(retained_ids))
    assert evidence.quarantined_pairs == len(expired_ids)
    assert evidence.executed_at == NOW
    assert str(tmp_path) not in repr(evidence)
    quarantine = tmp_path / ".schemabridge-retention-quarantine" / plan.plan_fingerprint
    quarantined_files = tuple(quarantine.iterdir())
    assert len(quarantined_files) == len(expired_ids) * 2
    assert all(path.is_file() for path in quarantined_files)
    remaining = planner.plan(tmp_path)
    assert {item.artifact_id for item in remaining.retained} == retained_ids
    assert remaining.expired == ()


def test_apply_accepts_normal_clock_progress_but_rejects_a_forged_plan(
    tmp_path: Path,
) -> None:
    os.chmod(tmp_path, 0o700)
    _series(tmp_path)
    times = iter((NOW, NOW + timedelta(minutes=1), NOW + timedelta(minutes=2)))
    planner = VerifiedBackupRetentionPlanner(
        policy=RecoveryPolicy.production_default(),
        audit_signing_keys={"v1": KEY},
        clock=lambda: next(times),
    )
    plan = planner.plan(tmp_path, dry_run=False)
    forged = replace(plan, plan_fingerprint="f" * 64)

    with pytest.raises(BackupRetentionError, match="backup retention operation rejected"):
        _apply(planner, forged, reviewed_plan_fingerprint=plan.plan_fingerprint)

    evidence = _apply(planner, plan)
    assert evidence.executed_at == NOW + timedelta(minutes=2)


def test_interrupted_quarantine_move_rolls_back_the_complete_pair_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    os.chmod(tmp_path, 0o700)
    pairs = _series(tmp_path)
    planner = _planner()
    plan = planner.plan(tmp_path)
    original_replace = os.replace
    calls = 0

    def fail_second_move(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError
        original_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_second_move)

    with pytest.raises(BackupRetentionError, match="backup retention operation rejected"):
        _apply(planner, plan)

    assert all(archive.exists() and manifest.exists() for archive, manifest in pairs)
    assert not (tmp_path / ".schemabridge-retention-quarantine").exists()


def test_tampered_recovery_quarantine_blocks_future_retention_plans(
    tmp_path: Path,
) -> None:
    os.chmod(tmp_path, 0o700)
    _series(tmp_path)
    planner = _planner()
    plan = planner.plan(tmp_path)
    _apply(planner, plan)
    quarantine = tmp_path / ".schemabridge-retention-quarantine" / plan.plan_fingerprint
    quarantined_archive = next(path for path in quarantine.iterdir() if path.suffix == ".dump")
    quarantined_archive.write_bytes(b"tampered")

    with pytest.raises(BackupRetentionError, match="backup retention operation rejected"):
        planner.plan(tmp_path)


def test_duplicate_recovery_batch_cannot_hide_replayed_verified_pairs(
    tmp_path: Path,
) -> None:
    os.chmod(tmp_path, 0o700)
    _series(tmp_path)
    planner = _planner()
    plan = planner.plan(tmp_path)
    _apply(planner, plan)
    quarantine_root = tmp_path / ".schemabridge-retention-quarantine"
    original = quarantine_root / plan.plan_fingerprint
    replay = quarantine_root / ("f" * 64)
    replay.mkdir(mode=0o700)
    for path in original.iterdir():
        shutil.copy2(path, replay / path.name)

    with pytest.raises(BackupRetentionError, match="backup retention operation rejected"):
        planner.plan(tmp_path)
