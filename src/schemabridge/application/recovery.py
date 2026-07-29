"""Provider-neutral recovery policy, rollback decision, and verified drill orchestration."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import NoReturn

from schemabridge.application.ports.control_plane_operations import (
    ControlPlaneBackupPort,
    ControlPlaneRestorePort,
)

_OWNER = re.compile(r"^[a-z][a-z0-9-]{1,63}$")


class RecoveryPolicyError(ValueError):
    """A configured policy is weaker than the M29 recovery floor."""


class RecoveryDrillError(RuntimeError):
    """A drill did not produce internally consistent fresh-target evidence."""


class RollbackDecision(StrEnum):
    """Closed release decisions; no automatic down-migration exists."""

    NO_ROLLBACK = "no_rollback"
    ROLLBACK_APPLICATION = "rollback_application_with_forward_compatible_schema"
    RESTORE_VERIFIED_BACKUP = "restore_verified_pre_migration_backup_to_fresh_target"
    BLOCKED = "rollback_blocked"


def _reject_policy() -> NoReturn:
    raise RecoveryPolicyError("recovery policy rejected")


@dataclass(frozen=True, slots=True)
class RecoveryPolicy:
    """Minimum operated backup, retention, restore, and cutover contract."""

    backup_interval_minutes: int
    rpo_minutes: int
    rto_minutes: int
    hourly_recovery_points: int
    daily_recovery_points: int
    monthly_recovery_points: int
    encryption_required: bool
    immutable_remote_retention_required: bool
    restore_requires_distinct_empty_target: bool
    cutover_requires_external_authority: bool
    owner: str

    def __post_init__(self) -> None:
        integer_values = (
            self.backup_interval_minutes,
            self.rpo_minutes,
            self.rto_minutes,
            self.hourly_recovery_points,
            self.daily_recovery_points,
            self.monthly_recovery_points,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in integer_values):
            _reject_policy()
        if not 1 <= self.backup_interval_minutes <= 60:
            _reject_policy()
        if not self.backup_interval_minutes <= self.rpo_minutes <= 60:
            _reject_policy()
        if not 1 <= self.rto_minutes <= 240:
            _reject_policy()
        if not 24 <= self.hourly_recovery_points <= 744:
            _reject_policy()
        if not 35 <= self.daily_recovery_points <= 366:
            _reject_policy()
        if not 12 <= self.monthly_recovery_points <= 120:
            _reject_policy()
        if (
            self.encryption_required is not True
            or self.immutable_remote_retention_required is not True
            or self.restore_requires_distinct_empty_target is not True
            or self.cutover_requires_external_authority is not True
        ):
            _reject_policy()
        if _OWNER.fullmatch(self.owner) is None:
            _reject_policy()

    @classmethod
    def production_default(cls) -> RecoveryPolicy:
        """Return the explicit M29 floor: hourly, 60-minute RPO, four-hour RTO."""

        return cls(
            backup_interval_minutes=60,
            rpo_minutes=60,
            rto_minutes=240,
            hourly_recovery_points=24,
            daily_recovery_points=35,
            monthly_recovery_points=12,
            encryption_required=True,
            immutable_remote_retention_required=True,
            restore_requires_distinct_empty_target=True,
            cutover_requires_external_authority=True,
            owner="recovery-operations",
        )

    def as_dict(self) -> dict[str, object]:
        """Return the complete policy facts used by config validation and hashing."""

        return {
            "backup_interval_minutes": self.backup_interval_minutes,
            "rpo_minutes": self.rpo_minutes,
            "rto_minutes": self.rto_minutes,
            "hourly_recovery_points": self.hourly_recovery_points,
            "daily_recovery_points": self.daily_recovery_points,
            "monthly_recovery_points": self.monthly_recovery_points,
            "encryption_required": self.encryption_required,
            "immutable_remote_retention_required": self.immutable_remote_retention_required,
            "restore_requires_distinct_empty_target": self.restore_requires_distinct_empty_target,
            "cutover_requires_external_authority": self.cutover_requires_external_authority,
            "owner": self.owner,
        }

    @property
    def fingerprint(self) -> str:
        """Bind retention and drill evidence to the complete policy."""

        canonical = json.dumps(
            self.as_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


def select_rollback_decision(
    *,
    release_healthy: bool,
    schema_forward_compatible: bool,
    verified_pre_migration_backup: bool,
) -> RollbackDecision:
    """Choose only a forward-schema application rollback or verified fresh restore."""

    if any(
        not isinstance(value, bool)
        for value in (
            release_healthy,
            schema_forward_compatible,
            verified_pre_migration_backup,
        )
    ):
        raise RecoveryDrillError("recovery drill verification failed")
    if release_healthy:
        return RollbackDecision.NO_ROLLBACK
    if schema_forward_compatible:
        return RollbackDecision.ROLLBACK_APPLICATION
    if verified_pre_migration_backup:
        return RollbackDecision.RESTORE_VERIFIED_BACKUP
    return RollbackDecision.BLOCKED


@dataclass(frozen=True, slots=True)
class RecoveryDrillEvidence:
    """Sanitized timing and integrity facts; never paths, DSNs, commands, or credentials."""

    policy_fingerprint: str
    started_at: datetime
    completed_at: datetime
    backup_duration_ms: int
    restore_duration_ms: int
    total_duration_ms: int
    backup_age_seconds: int
    rpo_met: bool
    rto_met: bool
    verification_passed: bool
    rollback_decision: RollbackDecision
    cutover_authorized: bool
    schema_version: int
    schema_checksum: str
    state_sha256: str
    target_database_fingerprint: str
    table_counts: dict[str, int]
    audited_workspaces: int
    audit_events: int
    active_pointers: int
    transition_records: int
    pending_outbox_records: int
    quarantine_records: int


@dataclass(frozen=True, slots=True)
class RecoveryDrillRunner:
    """Create through M23, restore through M23, then independently bind safe evidence."""

    backup: ControlPlaneBackupPort = field(repr=False)
    restore: ControlPlaneRestorePort = field(repr=False)
    policy: RecoveryPolicy
    clock: Callable[[], datetime] = field(repr=False)
    monotonic: Callable[[], float] = field(repr=False)

    def run(
        self,
        destination: Path,
        *,
        release_healthy: bool,
        schema_forward_compatible: bool,
        verified_pre_migration_backup: bool,
    ) -> RecoveryDrillEvidence:
        """Run one backup/fresh-target restore without authorizing cutover."""

        started_at = _aware_utc(self.clock())
        started_tick = self.monotonic()
        archive, manifest_path, manifest = self.backup.create_backup(destination)
        backup_completed_at = _aware_utc(self.clock())
        backup_tick = self.monotonic()
        verification = self.restore.restore_backup(archive, manifest_path)
        completed_at = _aware_utc(self.clock())
        completed_tick = self.monotonic()

        ticks = (started_tick, backup_tick, completed_tick)
        if any(not math.isfinite(value) for value in ticks):
            raise RecoveryDrillError("recovery drill verification failed")
        if not started_tick <= backup_tick <= completed_tick:
            raise RecoveryDrillError("recovery drill verification failed")
        if not started_at <= backup_completed_at <= completed_at:
            raise RecoveryDrillError("recovery drill verification failed")
        manifest_created_at = _aware_utc(manifest.created_at)
        verified_at = _aware_utc(verification.verified_at)
        if (
            manifest_created_at > completed_at
            or not manifest_created_at <= verified_at <= completed_at
        ):
            raise RecoveryDrillError("recovery drill verification failed")
        if (
            verification.target_database_fingerprint == manifest.source_database_fingerprint
            or verification.schema_version != manifest.schema_version
            or verification.schema_checksum != manifest.schema_checksum
            or verification.state_sha256 != manifest.state_sha256
            or verification.table_counts != manifest.table_counts
        ):
            raise RecoveryDrillError("recovery drill verification failed")

        backup_duration_ms = _milliseconds(backup_tick - started_tick)
        restore_duration_ms = _milliseconds(completed_tick - backup_tick)
        total_duration_ms = _milliseconds(completed_tick - started_tick)
        backup_age_seconds = int((completed_at - manifest_created_at).total_seconds())
        rpo_met = backup_age_seconds <= self.policy.rpo_minutes * 60
        rto_met = total_duration_ms <= self.policy.rto_minutes * 60_000
        rollback_decision = select_rollback_decision(
            release_healthy=release_healthy,
            schema_forward_compatible=schema_forward_compatible,
            verified_pre_migration_backup=verified_pre_migration_backup,
        )
        if not rpo_met or not rto_met:
            rollback_decision = RollbackDecision.BLOCKED

        return RecoveryDrillEvidence(
            policy_fingerprint=self.policy.fingerprint,
            started_at=started_at,
            completed_at=completed_at,
            backup_duration_ms=backup_duration_ms,
            restore_duration_ms=restore_duration_ms,
            total_duration_ms=total_duration_ms,
            backup_age_seconds=backup_age_seconds,
            rpo_met=rpo_met,
            rto_met=rto_met,
            verification_passed=True,
            rollback_decision=rollback_decision,
            cutover_authorized=False,
            schema_version=verification.schema_version,
            schema_checksum=verification.schema_checksum,
            state_sha256=verification.state_sha256,
            target_database_fingerprint=verification.target_database_fingerprint,
            table_counts=dict(sorted(verification.table_counts.items())),
            audited_workspaces=verification.audited_workspaces,
            audit_events=verification.audit_events,
            active_pointers=verification.active_pointers,
            transition_records=verification.transition_records,
            pending_outbox_records=verification.pending_outbox_records,
            quarantine_records=verification.quarantine_records,
        )


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RecoveryDrillError("recovery drill verification failed")
    return value.astimezone(UTC)


def _milliseconds(seconds: float) -> int:
    if seconds < 0 or seconds > 604_800:
        raise RecoveryDrillError("recovery drill verification failed")
    return round(seconds * 1_000)
