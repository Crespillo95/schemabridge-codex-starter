"""Fail-closed M29 policy and verified-backup retention operator commands."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, TextIO

import yaml
from yaml.nodes import MappingNode, Node, SequenceNode

from schemabridge.adapters.backup.retention import (
    RETENTION_QUARANTINE_CONFIRMATION,
    BackupRetentionError,
    VerifiedBackupRetentionPlanner,
)
from schemabridge.application.recovery import RecoveryPolicy, RecoveryPolicyError

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "deploy/recovery/policy.yaml"
OPERATOR_SCHEMA_VERSION = "schemabridge.recovery-operator.v1"
KEY_SCHEMA_VERSION = "schemabridge.recovery-audit-key.v1"

_KEY_VERSION = re.compile(r"^v[1-9][0-9]{0,5}$")
_MAX_KEY_FILE_BYTES = 4096
_MAX_KEY_BYTES = 1024
_GENERIC_ERROR = "recovery operator request rejected"


class RecoveryOperatorError(RuntimeError):
    """A stable path-free and secret-free operator rejection."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(_GENERIC_ERROR)


@dataclass(frozen=True, slots=True)
class RecoveryAuditKey:
    """One versioned verification key whose material is excluded from repr."""

    version: str
    key: bytes = field(repr=False)


class _SanitizedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise RecoveryOperatorError("operator_arguments_invalid")


def _reject(code: str) -> NoReturn:
    raise RecoveryOperatorError(code)


def _reject_duplicate_yaml_keys(node: Node, visited: set[int] | None = None) -> None:
    visited = set() if visited is None else visited
    if id(node) in visited:
        return
    visited.add(id(node))
    if isinstance(node, MappingNode):
        keys: set[tuple[str, str]] = set()
        for key_node, value_node in node.value:
            if not isinstance(key_node, yaml.nodes.ScalarNode):
                _reject("recovery_policy_invalid")
            identity = (key_node.tag, key_node.value)
            if identity in keys:
                _reject("recovery_policy_invalid")
            keys.add(identity)
            _reject_duplicate_yaml_keys(value_node, visited)
    elif isinstance(node, SequenceNode):
        for value_node in node.value:
            _reject_duplicate_yaml_keys(value_node, visited)


def _exact_document(actual: object, expected: object) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or actual.keys() != expected.keys():
            return False
        return all(_exact_document(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return False
        return all(
            _exact_document(actual_item, expected_item)
            for actual_item, expected_item in zip(actual, expected, strict=True)
        )
    return actual == expected


def _expected_policy_document(policy: RecoveryPolicy) -> dict[str, object]:
    return {
        "schema_version": "schemabridge.recovery-policy.v1",
        "owner": policy.owner,
        "schedule": {
            "backup_interval_minutes": policy.backup_interval_minutes,
        },
        "objectives": {
            "rpo_minutes": policy.rpo_minutes,
            "rto_minutes": policy.rto_minutes,
        },
        "retention": {
            "hourly_recovery_points": policy.hourly_recovery_points,
            "daily_recovery_points": policy.daily_recovery_points,
            "monthly_recovery_points": policy.monthly_recovery_points,
            "immutable_remote_retention_required": (policy.immutable_remote_retention_required),
            "encryption_required": policy.encryption_required,
            "dry_run_default": True,
        },
        "restore": {
            "requires_distinct_empty_target": (policy.restore_requires_distinct_empty_target),
            "cutover_requires_external_authority": (policy.cutover_requires_external_authority),
            "automatic_down_migration": False,
        },
    }


def validate_policy_document(path: Path) -> RecoveryPolicy:
    """Validate an exact duplicate-free YAML representation of the M29 floor."""

    try:
        raw = path.read_text(encoding="utf-8")
        if not raw or len(raw.encode()) > 16_384:
            _reject("recovery_policy_invalid")
        composed = yaml.compose(raw, Loader=yaml.SafeLoader)
        if composed is None:
            _reject("recovery_policy_invalid")
        _reject_duplicate_yaml_keys(composed)
        document = yaml.safe_load(raw)
        policy = RecoveryPolicy.production_default()
        if not _exact_document(document, _expected_policy_document(policy)):
            _reject("recovery_policy_invalid")
        return policy
    except RecoveryOperatorError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError, RecoveryPolicyError):
        raise RecoveryOperatorError("recovery_policy_invalid") from None


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _read_owner_only_key(path: Path) -> RecoveryAuditKey:
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        details = os.fstat(descriptor)
        permissions = stat.S_IMODE(details.st_mode)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or not permissions & stat.S_IRUSR
            or permissions & (stat.S_IXUSR | stat.S_IRWXG | stat.S_IRWXO)
            or not 1 <= details.st_size <= _MAX_KEY_FILE_BYTES
        ):
            _reject("recovery_key_invalid")
        raw = bytearray()
        while len(raw) <= _MAX_KEY_FILE_BYTES:
            block = os.read(descriptor, min(1024, _MAX_KEY_FILE_BYTES + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
        if not raw or len(raw) > _MAX_KEY_FILE_BYTES:
            _reject("recovery_key_invalid")
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
        expected_keys = {"schema_version", "key_version", "key"}
        if not isinstance(document, dict) or set(document) != expected_keys:
            _reject("recovery_key_invalid")
        schema_version = document["schema_version"]
        key_version = document["key_version"]
        key_text = document["key"]
        if (
            schema_version != KEY_SCHEMA_VERSION
            or not isinstance(schema_version, str)
            or not isinstance(key_version, str)
            or _KEY_VERSION.fullmatch(key_version) is None
            or not isinstance(key_text, str)
        ):
            _reject("recovery_key_invalid")
        key = key_text.encode("utf-8")
        if not 32 <= len(key) <= _MAX_KEY_BYTES or len(set(key)) < 8:
            _reject("recovery_key_invalid")
        return RecoveryAuditKey(version=key_version, key=key)
    except RecoveryOperatorError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise RecoveryOperatorError("recovery_key_invalid") from None
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def run_policy_check() -> dict[str, object]:
    """Validate the repository policy without opening any database connection."""

    policy = validate_policy_document(POLICY_PATH)
    return {
        "schema_version": OPERATOR_SCHEMA_VERSION,
        "command": "policy-check",
        "outcome": "passed",
        "policy_fingerprint": policy.fingerprint,
    }


def run_retention_plan(
    root: Path,
    key_file: Path,
    *,
    execute: bool = False,
    confirmation: str | None = None,
    reviewed_policy_fingerprint: str | None = None,
    reviewed_plan_fingerprint: str | None = None,
) -> dict[str, object]:
    """Plan by default, or explicitly execute, using verified local M23 pairs only."""

    review_values = (reviewed_policy_fingerprint, reviewed_plan_fingerprint)
    if (
        execute
        and (
            confirmation != RETENTION_QUARANTINE_CONFIRMATION
            or any(value is None for value in review_values)
        )
    ) or (
        not execute
        and (confirmation is not None or any(value is not None for value in review_values))
    ):
        _reject("retention_confirmation_invalid")
    try:
        policy = validate_policy_document(POLICY_PATH)
        audit_key = _read_owner_only_key(key_file)
        now = datetime.now(UTC)
        planner = VerifiedBackupRetentionPlanner(
            policy=policy,
            audit_signing_keys={audit_key.version: audit_key.key},
            clock=lambda: now,
        )
        plan = planner.plan(root)
        result: dict[str, object] = {
            "schema_version": OPERATOR_SCHEMA_VERSION,
            "command": "retention-plan",
            "outcome": "planned",
            "dry_run": not execute,
            "policy_fingerprint": plan.policy_fingerprint,
            "plan_fingerprint": plan.plan_fingerprint,
            "planned_at": _utc_text(plan.planned_at),
            "fresh": plan.fresh,
            "newest_age_seconds": plan.newest_age_seconds,
            "verified_pairs": plan.verified_pairs,
            "retained_pairs": len(plan.retained),
            "expired_pairs": len(plan.expired),
        }
        if execute:
            if reviewed_policy_fingerprint is None or reviewed_plan_fingerprint is None:
                _reject("retention_confirmation_invalid")
            evidence = planner.apply(
                plan,
                confirmation=RETENTION_QUARANTINE_CONFIRMATION,
                reviewed_policy_fingerprint=reviewed_policy_fingerprint,
                reviewed_plan_fingerprint=reviewed_plan_fingerprint,
            )
            result.update(
                {
                    "outcome": "executed",
                    "executed_at": _utc_text(evidence.executed_at),
                    "quarantined_pairs": evidence.quarantined_pairs,
                    "retained_pairs": len(evidence.retained_artifact_ids),
                }
            )
        return result
    except RecoveryOperatorError:
        raise
    except BackupRetentionError:
        raise RecoveryOperatorError("backup_retention_rejected") from None


def _parser() -> argparse.ArgumentParser:
    parser = _SanitizedArgumentParser(
        description="Validate M29 recovery policy or operate verified local retention.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "policy-check",
        help="Validate the exact repository recovery policy.",
    )
    retention = commands.add_parser(
        "retention-plan",
        help="Plan retention by default; execution requires exact confirmation.",
    )
    retention.add_argument("--root", required=True, type=Path)
    retention.add_argument("--key-file", required=True, type=Path)
    retention.add_argument("--execute", action="store_true")
    retention.add_argument("--confirmation")
    retention.add_argument("--reviewed-policy-fingerprint")
    retention.add_argument("--reviewed-plan-fingerprint")
    return parser


def _safe_command(arguments: Sequence[str]) -> str:
    if arguments and arguments[0] in {"policy-check", "retention-plan"}:
        return arguments[0]
    return "unknown"


def _write_json(stream: TextIO, payload: Mapping[str, object]) -> None:
    rendered = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    print(rendered, file=stream)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    command = _safe_command(arguments)
    try:
        parsed = _parser().parse_args(arguments)
        if parsed.command == "policy-check":
            payload = run_policy_check()
        else:
            payload = run_retention_plan(
                parsed.root,
                parsed.key_file,
                execute=parsed.execute,
                confirmation=parsed.confirmation,
                reviewed_policy_fingerprint=parsed.reviewed_policy_fingerprint,
                reviewed_plan_fingerprint=parsed.reviewed_plan_fingerprint,
            )
        _write_json(sys.stdout, payload)
        return 0
    except RecoveryOperatorError as error:
        _write_json(
            sys.stderr,
            {
                "schema_version": OPERATOR_SCHEMA_VERSION,
                "command": command,
                "outcome": "failed",
                "code": error.code,
            },
        )
        return 2
    except Exception:
        _write_json(
            sys.stderr,
            {
                "schema_version": OPERATOR_SCHEMA_VERSION,
                "command": command,
                "outcome": "failed",
                "code": "operator_internal_failure",
            },
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
