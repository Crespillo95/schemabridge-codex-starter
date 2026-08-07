from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from schemabridge.application.recovery import (
    RecoveryDrillError,
    RecoveryPolicy,
    RecoveryPolicyError,
    RollbackDecision,
    select_rollback_decision,
)

ROOT = Path(__file__).resolve().parents[2]


def test_production_recovery_policy_is_explicit_and_never_weaker_than_m29() -> None:
    policy = RecoveryPolicy.production_default()

    assert policy.backup_interval_minutes == 60
    assert policy.rpo_minutes == 60
    assert policy.rto_minutes == 240
    assert policy.hourly_recovery_points == 24
    assert policy.daily_recovery_points == 35
    assert policy.monthly_recovery_points == 12
    assert policy.encryption_required is True
    assert policy.immutable_remote_retention_required is True
    assert policy.restore_requires_distinct_empty_target is True
    assert policy.cutover_requires_external_authority is True
    assert len(policy.fingerprint) == 64


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("backup_interval_minutes", 61),
        ("rpo_minutes", 61),
        ("rto_minutes", 241),
        ("hourly_recovery_points", 23),
        ("daily_recovery_points", 34),
        ("monthly_recovery_points", 11),
        ("encryption_required", False),
        ("immutable_remote_retention_required", False),
        ("restore_requires_distinct_empty_target", False),
        ("cutover_requires_external_authority", False),
        ("owner", "owner\nforged"),
    ],
)
def test_weaker_or_hostile_recovery_policy_is_rejected_without_echoing_input(
    field: str,
    value: object,
) -> None:
    values: dict[str, object] = RecoveryPolicy.production_default().as_dict()
    values[field] = value

    with pytest.raises(RecoveryPolicyError) as raised:
        RecoveryPolicy(**values)  # type: ignore[arg-type]

    assert str(raised.value) == "recovery policy rejected"
    assert repr(value) not in str(raised.value)


def test_stricter_recovery_policy_is_allowed_and_changes_fingerprint() -> None:
    baseline = RecoveryPolicy.production_default()
    stricter = RecoveryPolicy(
        backup_interval_minutes=30,
        rpo_minutes=30,
        rto_minutes=120,
        hourly_recovery_points=48,
        daily_recovery_points=60,
        monthly_recovery_points=24,
        encryption_required=True,
        immutable_remote_retention_required=True,
        restore_requires_distinct_empty_target=True,
        cutover_requires_external_authority=True,
        owner="recovery-operations",
    )

    assert stricter.fingerprint != baseline.fingerprint


@pytest.mark.parametrize(
    (
        "release_healthy",
        "schema_forward_compatible",
        "verified_pre_migration_backup",
        "expected",
    ),
    [
        (True, True, True, RollbackDecision.NO_ROLLBACK),
        (False, True, False, RollbackDecision.ROLLBACK_APPLICATION),
        (False, True, True, RollbackDecision.ROLLBACK_APPLICATION),
        (False, False, True, RollbackDecision.RESTORE_VERIFIED_BACKUP),
        (False, False, False, RollbackDecision.BLOCKED),
    ],
)
def test_rollback_decision_never_selects_a_down_migration(
    release_healthy: bool,
    schema_forward_compatible: bool,
    verified_pre_migration_backup: bool,
    expected: RollbackDecision,
) -> None:
    assert (
        select_rollback_decision(
            release_healthy=release_healthy,
            schema_forward_compatible=schema_forward_compatible,
            verified_pre_migration_backup=verified_pre_migration_backup,
        )
        is expected
    )
    assert "down" not in expected.value


def test_rollback_decision_rejects_truthy_non_boolean_input() -> None:
    with pytest.raises(RecoveryDrillError, match="recovery drill verification failed"):
        select_rollback_decision(
            release_healthy="false",  # type: ignore[arg-type]
            schema_forward_compatible=True,
            verified_pre_migration_backup=True,
        )


def test_operated_policy_document_matches_the_code_contract() -> None:
    payload = yaml.safe_load((ROOT / "deploy/recovery/policy.yaml").read_text(encoding="utf-8"))
    policy = RecoveryPolicy.production_default()

    assert payload == {
        "schema_version": "schemabridge.recovery-policy.v1",
        "owner": policy.owner,
        "schedule": {"backup_interval_minutes": policy.backup_interval_minutes},
        "objectives": {
            "rpo_minutes": policy.rpo_minutes,
            "rto_minutes": policy.rto_minutes,
        },
        "retention": {
            "hourly_recovery_points": policy.hourly_recovery_points,
            "daily_recovery_points": policy.daily_recovery_points,
            "monthly_recovery_points": policy.monthly_recovery_points,
            "immutable_remote_retention_required": True,
            "encryption_required": True,
            "dry_run_default": True,
        },
        "restore": {
            "requires_distinct_empty_target": True,
            "cutover_requires_external_authority": True,
            "automatic_down_migration": False,
        },
    }


def test_recovery_runbook_has_executable_safety_and_evidence_sections() -> None:
    content = (ROOT / "deploy/recovery/RUNBOOK.md").read_text(encoding="utf-8")

    for heading in (
        "## Backup",
        "## Retention",
        "## Restore drill",
        "## Release rollback",
        "## Evidence",
    ):
        assert heading in content
    assert "fresh target" in content
    assert "source database" in content
    assert "down-migration" in content
