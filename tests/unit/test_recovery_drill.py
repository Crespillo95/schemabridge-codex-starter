from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from schemabridge.application.recovery import (
    RecoveryDrillError,
    RecoveryDrillRunner,
    RecoveryPolicy,
    RollbackDecision,
)
from schemabridge.domain.control_plane_operations import (
    ControlPlaneBackupManifest,
    ControlPlaneRestoreVerification,
)

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def _manifest(created_at: datetime = NOW) -> ControlPlaneBackupManifest:
    return ControlPlaneBackupManifest(
        source_database_fingerprint="a" * 64,
        schema_name="schemabridge_control",
        schema_version=10,
        schema_checksum="b" * 64,
        archive_name="control-plane-20260729T120000Z-000000000001.dump",
        archive_size_bytes=100,
        archive_sha256="c" * 64,
        state_sha256="d" * 64,
        table_counts={"schema_migrations": 9, "control_audit_events": 6},
        audit_key_version="v1",
        created_at=created_at,
        manifest_hmac="e" * 64,
    )


def _verification(
    manifest: ControlPlaneBackupManifest,
    *,
    target_fingerprint: str = "f" * 64,
    state_sha256: str | None = None,
) -> ControlPlaneRestoreVerification:
    return ControlPlaneRestoreVerification(
        target_database_fingerprint=target_fingerprint,
        schema_version=manifest.schema_version,
        schema_checksum=manifest.schema_checksum,
        state_sha256=state_sha256 or manifest.state_sha256,
        table_counts=manifest.table_counts,
        audited_workspaces=2,
        audit_events=6,
        active_pointers=1,
        transition_records=3,
        pending_outbox_records=1,
        quarantine_records=4,
        verified_at=NOW + timedelta(seconds=30),
    )


class FakeBackup:
    def __init__(self, manifest: ControlPlaneBackupManifest) -> None:
        self.manifest = manifest
        self.destinations: list[Path] = []

    def create_backup(
        self,
        destination: Path,
    ) -> tuple[Path, Path, ControlPlaneBackupManifest]:
        self.destinations.append(destination)
        return (
            destination / self.manifest.archive_name,
            destination / "manifest.json",
            self.manifest,
        )


class FakeRestore:
    def __init__(self, verification: ControlPlaneRestoreVerification) -> None:
        self.verification = verification
        self.calls: list[tuple[Path, Path]] = []

    def restore_backup(
        self,
        archive: Path,
        manifest: Path,
    ) -> ControlPlaneRestoreVerification:
        self.calls.append((archive, manifest))
        return self.verification


class SecretReprBackup(FakeBackup):
    def __repr__(self) -> str:
        return "postgresql://backup:do-not-print@control.example/control"


class SecretReprRestore(FakeRestore):
    def __repr__(self) -> str:
        return "postgresql://restore:do-not-print@fresh.example/control"


def _sequence(values: tuple[float, ...]) -> Iterator[float]:
    yield from values


def test_drill_reuses_m23_ports_and_returns_only_sanitized_timing_integrity_evidence(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    backup = FakeBackup(manifest)
    restore = FakeRestore(_verification(manifest))
    ticks = _sequence((100.0, 112.5, 142.0))
    times = iter(
        (
            NOW,
            NOW + timedelta(seconds=13),
            NOW + timedelta(seconds=43),
        )
    )
    runner = RecoveryDrillRunner(
        backup=backup,
        restore=restore,
        policy=RecoveryPolicy.production_default(),
        clock=lambda: next(times),
        monotonic=lambda: next(ticks),
    )

    evidence = runner.run(
        tmp_path / "private-artifacts",
        release_healthy=False,
        schema_forward_compatible=True,
        verified_pre_migration_backup=True,
    )

    assert backup.destinations == [tmp_path / "private-artifacts"]
    assert len(restore.calls) == 1
    assert evidence.backup_duration_ms == 12_500
    assert evidence.restore_duration_ms == 29_500
    assert evidence.total_duration_ms == 42_000
    assert evidence.backup_age_seconds == 43
    assert evidence.rpo_met is True
    assert evidence.rto_met is True
    assert evidence.verification_passed is True
    assert evidence.rollback_decision is RollbackDecision.ROLLBACK_APPLICATION
    assert evidence.cutover_authorized is False
    assert evidence.state_sha256 == manifest.state_sha256
    assert evidence.target_database_fingerprint == "f" * 64
    rendered = repr(evidence)
    assert str(tmp_path) not in rendered
    assert "private-artifacts" not in rendered


def test_drill_runner_repr_never_includes_adapter_credentials() -> None:
    manifest = _manifest()
    runner = RecoveryDrillRunner(
        backup=SecretReprBackup(manifest),
        restore=SecretReprRestore(_verification(manifest)),
        policy=RecoveryPolicy.production_default(),
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
    )

    assert "postgresql://" not in repr(runner)
    assert "do-not-print" not in repr(runner)


@pytest.mark.parametrize("mismatch", ["state", "source-target", "negative-clock"])
def test_drill_fails_closed_on_inconsistent_or_impossible_verification(
    tmp_path: Path,
    mismatch: str,
) -> None:
    manifest = _manifest()
    target = "f" * 64
    state = None
    ticks = (100.0, 110.0, 120.0)
    if mismatch == "state":
        state = "9" * 64
    elif mismatch == "source-target":
        target = manifest.source_database_fingerprint
    else:
        ticks = (100.0, 90.0, 120.0)
    runner = RecoveryDrillRunner(
        backup=FakeBackup(manifest),
        restore=FakeRestore(
            _verification(
                manifest,
                target_fingerprint=target,
                state_sha256=state,
            )
        ),
        policy=RecoveryPolicy.production_default(),
        clock=lambda: NOW + timedelta(seconds=30),
        monotonic=lambda: next(tick_iterator),
    )
    tick_iterator = iter(ticks)

    with pytest.raises(RecoveryDrillError, match="recovery drill verification failed"):
        runner.run(
            tmp_path,
            release_healthy=True,
            schema_forward_compatible=True,
            verified_pre_migration_backup=True,
        )


def test_drill_records_rto_failure_and_blocks_cutover_and_rollback_claim(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    ticks = iter((0.0, 60.0, 15_000.0))
    times = iter((NOW, NOW + timedelta(minutes=1), NOW + timedelta(minutes=250)))
    runner = RecoveryDrillRunner(
        backup=FakeBackup(manifest),
        restore=FakeRestore(_verification(manifest)),
        policy=RecoveryPolicy.production_default(),
        clock=lambda: next(times),
        monotonic=lambda: next(ticks),
    )

    evidence = runner.run(
        tmp_path,
        release_healthy=False,
        schema_forward_compatible=False,
        verified_pre_migration_backup=True,
    )

    assert evidence.rpo_met is False
    assert evidence.rto_met is False
    assert evidence.rollback_decision is RollbackDecision.BLOCKED
    assert evidence.cutover_authorized is False
