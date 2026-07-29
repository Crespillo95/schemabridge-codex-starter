"""Fail-closed retention planning over verified M23 archive/manifest pairs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import NoReturn

from schemabridge.adapters.control_plane.postgres_operations import (
    _load_and_verify_manifest,
)
from schemabridge.application.recovery import RecoveryPolicy
from schemabridge.domain.control_plane_operations import ControlPlaneBackupManifest

RETENTION_QUARANTINE_CONFIRMATION = "QUARANTINE EXPIRED VERIFIED BACKUPS"

_ARTIFACT_NAME = re.compile(
    r"^(control-plane-(?P<timestamp>[0-9]{8}T[0-9]{6}Z)-[0-9a-f]{12})"
    r"\.(?P<extension>dump|manifest\.json)$"
)
_KEY_VERSION = re.compile(r"^v[1-9][0-9]{0,5}$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_QUARANTINE_DIRECTORY = ".schemabridge-retention-quarantine"


class BackupRetentionError(RuntimeError):
    """The artifact set or requested retention transition was unsafe."""


def _reject() -> NoReturn:
    raise BackupRetentionError("backup retention operation rejected")


@dataclass(frozen=True, slots=True)
class RetentionArtifactDecision:
    """One path-free decision bound to a verified artifact identity."""

    artifact_id: str
    created_at: datetime
    action: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _VerifiedPair:
    artifact_id: str
    created_at: datetime
    archive_sha256: str
    manifest_hmac: str
    archive: Path = field(repr=False)
    manifest: Path = field(repr=False)


@dataclass(frozen=True, slots=True)
class BackupRetentionPlan:
    """Deterministic plan; artifact paths are private adapter state."""

    policy_fingerprint: str
    planned_at: datetime
    dry_run: bool
    fresh: bool
    newest_age_seconds: int
    verified_pairs: int
    retained: tuple[RetentionArtifactDecision, ...]
    expired: tuple[RetentionArtifactDecision, ...]
    plan_fingerprint: str
    _root: Path = field(repr=False)
    _pairs: tuple[_VerifiedPair, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class BackupRetentionExecutionEvidence:
    """Sanitized proof that expired pairs moved into a recoverable quarantine."""

    policy_fingerprint: str
    plan_fingerprint: str
    executed_at: datetime
    quarantined_pairs: int
    quarantined_artifact_ids: tuple[str, ...]
    retained_artifact_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerifiedBackupRetentionPlanner:
    """Verify every pair before selecting or quarantining any artifact."""

    policy: RecoveryPolicy
    audit_signing_keys: Mapping[str, bytes] = field(repr=False)
    clock: Callable[[], datetime] = field(repr=False)

    def __post_init__(self) -> None:
        if not self.audit_signing_keys:
            _reject()
        for version, key in self.audit_signing_keys.items():
            if (
                _KEY_VERSION.fullmatch(version) is None
                or not isinstance(key, bytes)
                or len(key) < 32
                or len(set(key)) < 8
            ):
                _reject()
        object.__setattr__(
            self,
            "audit_signing_keys",
            MappingProxyType(dict(self.audit_signing_keys)),
        )

    def plan(self, root: Path, *, dry_run: bool = True) -> BackupRetentionPlan:
        """Verify the complete directory and produce a path-free retention decision."""

        if not isinstance(dry_run, bool):
            _reject()
        secured_root = _secure_root(root)
        planned_at = _aware_utc(self.clock())
        pairs = _load_verified_pairs(secured_root, self.audit_signing_keys, planned_at)
        if not pairs:
            _reject()
        newest_age_seconds = int((planned_at - pairs[0].created_at).total_seconds())
        fresh = newest_age_seconds <= self.policy.rpo_minutes * 60

        reasons: dict[str, set[str]] = {pair.artifact_id: set() for pair in pairs}
        _select_buckets(
            pairs,
            limit=self.policy.hourly_recovery_points,
            reason="hourly",
            bucket=lambda value: value.strftime("%Y-%m-%dT%H"),
            selected=reasons,
        )
        _select_buckets(
            pairs,
            limit=self.policy.daily_recovery_points,
            reason="daily",
            bucket=lambda value: value.strftime("%Y-%m-%d"),
            selected=reasons,
        )
        _select_buckets(
            pairs,
            limit=self.policy.monthly_recovery_points,
            reason="monthly",
            bucket=lambda value: value.strftime("%Y-%m"),
            selected=reasons,
        )

        retained: list[RetentionArtifactDecision] = []
        expired: list[RetentionArtifactDecision] = []
        for pair in pairs:
            pair_reasons = tuple(sorted(reasons[pair.artifact_id]))
            if pair_reasons or not fresh:
                if not pair_reasons:
                    pair_reasons = ("stale_safety_hold",)
                retained.append(
                    RetentionArtifactDecision(
                        artifact_id=pair.artifact_id,
                        created_at=pair.created_at,
                        action="retain",
                        reasons=pair_reasons,
                    )
                )
            else:
                expired.append(
                    RetentionArtifactDecision(
                        artifact_id=pair.artifact_id,
                        created_at=pair.created_at,
                        action="expire",
                        reasons=(),
                    )
                )
        fingerprint = _plan_fingerprint(
            policy_fingerprint=self.policy.fingerprint,
            fresh=fresh,
            retained=tuple(retained),
            expired=tuple(expired),
        )
        return BackupRetentionPlan(
            policy_fingerprint=self.policy.fingerprint,
            planned_at=planned_at,
            dry_run=dry_run,
            fresh=fresh,
            newest_age_seconds=newest_age_seconds,
            verified_pairs=len(pairs),
            retained=tuple(retained),
            expired=tuple(expired),
            plan_fingerprint=fingerprint,
            _root=secured_root,
            _pairs=pairs,
        )

    def apply(
        self,
        plan: BackupRetentionPlan,
        *,
        confirmation: str,
        reviewed_policy_fingerprint: str,
        reviewed_plan_fingerprint: str,
    ) -> BackupRetentionExecutionEvidence:
        """Reverify and quarantine only the exact decisions reviewed by an operator."""

        if (
            confirmation != RETENTION_QUARANTINE_CONFIRMATION
            or plan.policy_fingerprint != self.policy.fingerprint
            or reviewed_policy_fingerprint != plan.policy_fingerprint
            or reviewed_plan_fingerprint != plan.plan_fingerprint
            or _FINGERPRINT.fullmatch(reviewed_policy_fingerprint) is None
            or _FINGERPRINT.fullmatch(reviewed_plan_fingerprint) is None
        ):
            _reject()
        expected_fingerprint = _plan_fingerprint(
            policy_fingerprint=plan.policy_fingerprint,
            fresh=plan.fresh,
            retained=plan.retained,
            expired=plan.expired,
        )
        if expected_fingerprint != plan.plan_fingerprint:
            _reject()
        current = self.plan(plan._root, dry_run=False)
        if (
            not current.fresh
            or current.retained != plan.retained
            or current.expired != plan.expired
            or current.plan_fingerprint != reviewed_plan_fingerprint
        ):
            _reject()

        expired_ids = {item.artifact_id for item in current.expired}
        retained_ids = tuple(sorted(item.artifact_id for item in current.retained))
        executed_at = _aware_utc(self.clock())
        if not expired_ids:
            return BackupRetentionExecutionEvidence(
                policy_fingerprint=self.policy.fingerprint,
                plan_fingerprint=plan.plan_fingerprint,
                executed_at=executed_at,
                quarantined_pairs=0,
                quarantined_artifact_ids=(),
                retained_artifact_ids=retained_ids,
            )

        quarantine_root = current._root / _QUARANTINE_DIRECTORY
        quarantine_batch = quarantine_root / reviewed_plan_fingerprint
        moved: list[tuple[Path, Path]] = []
        created_quarantine_root = False
        try:
            if quarantine_root.exists():
                _validate_quarantine_directory(
                    quarantine_root,
                    self.audit_signing_keys,
                    executed_at,
                )
            else:
                quarantine_root.mkdir(mode=0o700)
                created_quarantine_root = True
            quarantine_batch.mkdir(mode=0o700)
            _secure_root(quarantine_batch)
            for pair in current._pairs:
                if pair.artifact_id not in expired_ids:
                    continue
                for source in (pair.archive, pair.manifest):
                    target = quarantine_batch / source.name
                    os.replace(source, target)
                    moved.append((target, source))
            _validate_quarantine_directory(
                quarantine_root,
                self.audit_signing_keys,
                executed_at,
            )
            remaining = _load_verified_pairs(
                current._root,
                self.audit_signing_keys,
                executed_at,
            )
            if {pair.artifact_id for pair in remaining} != set(retained_ids):
                raise OSError
        except Exception:
            for staged, original in reversed(moved):
                try:
                    if staged.exists() and not original.exists():
                        os.replace(staged, original)
                except OSError:
                    pass
            with suppress(OSError):
                quarantine_batch.rmdir()
            if created_quarantine_root:
                with suppress(OSError):
                    quarantine_root.rmdir()
            raise BackupRetentionError("backup retention operation rejected") from None

        return BackupRetentionExecutionEvidence(
            policy_fingerprint=self.policy.fingerprint,
            plan_fingerprint=plan.plan_fingerprint,
            executed_at=executed_at,
            quarantined_pairs=len(expired_ids),
            quarantined_artifact_ids=tuple(sorted(expired_ids)),
            retained_artifact_ids=retained_ids,
        )


def _secure_root(root: Path) -> Path:
    try:
        details = root.lstat()
        current_uid = os.getuid() if hasattr(os, "getuid") else details.st_uid
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != current_uid
            or details.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        ):
            _reject()
        resolved = root.resolve(strict=True)
        if resolved != root.absolute():
            _reject()
        return resolved
    except (OSError, RuntimeError):
        _reject()


def _load_verified_pairs(
    root: Path,
    keys: Mapping[str, bytes],
    planned_at: datetime,
) -> tuple[_VerifiedPair, ...]:
    quarantine = root / _QUARANTINE_DIRECTORY
    try:
        quarantine.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        _reject()
    else:
        _validate_quarantine_directory(quarantine, keys, planned_at)
    return _load_verified_pairs_from_directory(
        root,
        keys,
        planned_at,
        ignored_names=frozenset({_QUARANTINE_DIRECTORY}),
    )


def _validate_quarantine_directory(
    root: Path,
    keys: Mapping[str, bytes],
    planned_at: datetime,
) -> None:
    secured = _secure_root(root)
    try:
        batches = tuple(sorted(secured.iterdir(), key=lambda item: item.name))
    except OSError:
        _reject()
    if not batches:
        _reject()
    observed_artifact_ids: set[str] = set()
    for batch in batches:
        if _FINGERPRINT.fullmatch(batch.name) is None:
            _reject()
        secured_batch = _secure_root(batch)
        pairs = _load_verified_pairs_from_directory(secured_batch, keys, planned_at)
        artifact_ids = {pair.artifact_id for pair in pairs}
        if not pairs or observed_artifact_ids & artifact_ids:
            _reject()
        observed_artifact_ids.update(artifact_ids)


def _load_verified_pairs_from_directory(
    root: Path,
    keys: Mapping[str, bytes],
    planned_at: datetime,
    *,
    ignored_names: frozenset[str] = frozenset(),
) -> tuple[_VerifiedPair, ...]:
    stems: dict[str, set[str]] = {}
    paths: dict[tuple[str, str], Path] = {}
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        _reject()
    for entry in entries:
        if entry.name in ignored_names:
            continue
        try:
            details = entry.lstat()
        except OSError:
            _reject()
        if not stat.S_ISREG(details.st_mode):
            _reject()
        matched = _ARTIFACT_NAME.fullmatch(entry.name)
        if matched is None:
            _reject()
        stem = matched.group(1)
        extension = matched.group("extension")
        stems.setdefault(stem, set()).add(extension)
        paths[(stem, extension)] = entry
    if any(extensions != {"dump", "manifest.json"} for extensions in stems.values()):
        _reject()

    pairs: list[_VerifiedPair] = []
    for stem in sorted(stems):
        archive = paths[(stem, "dump")]
        manifest_path = paths[(stem, "manifest.json")]
        try:
            manifest = _load_and_verify_manifest(archive, manifest_path, keys)
        except Exception:
            raise BackupRetentionError("backup retention operation rejected") from None
        _validate_pair_timestamp(stem, manifest, planned_at)
        artifact_id = _artifact_id(manifest)
        pairs.append(
            _VerifiedPair(
                artifact_id=artifact_id,
                created_at=manifest.created_at.astimezone(UTC),
                archive_sha256=manifest.archive_sha256,
                manifest_hmac=manifest.manifest_hmac,
                archive=archive,
                manifest=manifest_path,
            )
        )
    if len({pair.artifact_id for pair in pairs}) != len(pairs):
        _reject()
    return tuple(sorted(pairs, key=lambda pair: (pair.created_at, pair.artifact_id), reverse=True))


def _validate_pair_timestamp(
    stem: str,
    manifest: ControlPlaneBackupManifest,
    planned_at: datetime,
) -> None:
    matched = _ARTIFACT_NAME.fullmatch(f"{stem}.dump")
    if matched is None:
        _reject()
    created_at = manifest.created_at.astimezone(UTC)
    if (
        matched.group("timestamp") != created_at.strftime("%Y%m%dT%H%M%SZ")
        or created_at > planned_at
    ):
        _reject()


def _artifact_id(manifest: ControlPlaneBackupManifest) -> str:
    canonical = json.dumps(
        {
            "archive_sha256": manifest.archive_sha256,
            "created_at": manifest.created_at.astimezone(UTC).isoformat(),
            "manifest_hmac": manifest.manifest_hmac,
            "state_sha256": manifest.state_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _select_buckets(
    pairs: tuple[_VerifiedPair, ...],
    *,
    limit: int,
    reason: str,
    bucket: Callable[[datetime], str],
    selected: dict[str, set[str]],
) -> None:
    observed: set[str] = set()
    for pair in pairs:
        key = bucket(pair.created_at)
        if key in observed:
            continue
        if len(observed) >= limit:
            break
        observed.add(key)
        selected[pair.artifact_id].add(reason)


def _plan_fingerprint(
    *,
    policy_fingerprint: str,
    fresh: bool,
    retained: tuple[RetentionArtifactDecision, ...],
    expired: tuple[RetentionArtifactDecision, ...],
) -> str:
    """Bind reviewable decisions, not the dry-run transport or observation timestamp."""

    canonical = json.dumps(
        {
            "policy_fingerprint": policy_fingerprint,
            "fresh": fresh,
            "decisions": [
                {
                    "artifact_id": decision.artifact_id,
                    "created_at": decision.created_at.isoformat(),
                    "action": decision.action,
                    "reasons": list(decision.reasons),
                }
                for decision in (*retained, *expired)
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        _reject()
    return value.astimezone(UTC)
