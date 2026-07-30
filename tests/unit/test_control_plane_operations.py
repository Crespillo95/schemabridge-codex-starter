"""Security and integrity tests for M23 control-plane operator artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from schemabridge.adapters.control_plane.postgres_operations import (
    PostgresControlPlaneBackup,
    PostgresControlPlaneRestore,
    _load_and_verify_manifest,
    _postgres_environment,
    _require_backup_posture,
    _signed_manifest,
)
from schemabridge.application.ports.control_plane_operations import (
    ControlPlaneOperationError,
    ControlPlaneOperationErrorCode,
)
from schemabridge.domain.control_plane_operations import (
    ControlPlaneBackupManifest,
)

ROOT = Path(__file__).resolve().parents[2]
DSN = "postgresql://runtime:do-not-print@control.example:5432/control"
KEY = b"control-audit-key-0123456789-abcdef"
NOW = datetime(2026, 7, 23, 15, 0, tzinfo=UTC)


class _PostureCursor:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self._row = row

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row


class _PostureConnection:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self._row = row
        self.queries: list[str] = []

    def execute(self, query: str) -> _PostureCursor:
        self.queries.append(query)
        return _PostureCursor(self._row)


def _safe_backup_posture() -> tuple[object, ...]:
    return (
        "schemabridge_backup",
        "schemabridge_backup",
        "control",
        True,
        False,
        False,
        False,
        False,
        False,
        False,
        True,
        True,
        True,
        900_000,
    )


def _artifacts(
    tmp_path: Path,
) -> tuple[Path, Path, ControlPlaneBackupManifest]:
    archive = tmp_path / "control-plane-test.dump"
    archive.write_bytes(b"synthetic custom-format archive")
    os.chmod(archive, 0o600)
    _, source_fingerprint = _postgres_environment(DSN)
    manifest = _signed_manifest(
        source_database_fingerprint=source_fingerprint,
        schema_version=1,
        schema_checksum="a" * 64,
        archive_name=archive.name,
        archive_size_bytes=archive.stat().st_size,
        archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        state_sha256="b" * 64,
        table_counts={"schema_migrations": 1},
        key_version="v1",
        key=KEY,
        created_at=NOW,
    )
    manifest_path = tmp_path / "control-plane-test.manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.model_dump(mode="json")),
        encoding="utf-8",
    )
    os.chmod(manifest_path, 0o600)
    return archive, manifest_path, manifest


def test_database_password_is_process_environment_only_and_repr_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY",
        "must-never-reach-postgres-tools",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "must-never-reach-postgres-tools")
    environment, fingerprint = _postgres_environment(
        DSN + "?sslmode=verify-full&sslrootcert=%2Fsafe%2Fca.pem"
    )
    backup = PostgresControlPlaneBackup(
        DSN,
        ROOT / "migrations/control_plane",
        {"v1": KEY},
        "v1",
    )
    restore = PostgresControlPlaneRestore(
        "postgresql://migrator:other-secret@restore.example/control",
        ROOT / "migrations/control_plane",
        {"v1": KEY},
    )

    assert environment["PGPASSWORD"] == "do-not-print"
    assert set(environment) == {
        "PATH",
        "PGHOST",
        "PGPORT",
        "PGUSER",
        "PGDATABASE",
        "PGCONNECT_TIMEOUT",
        "PGPASSWORD",
        "PGSSLMODE",
        "PGSSLROOTCERT",
    }
    assert "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert len(fingerprint) == 64
    assert "do-not-print" not in repr(backup)
    assert "postgresql://" not in repr(backup)
    assert "other-secret" not in repr(restore)
    assert "postgresql://" not in repr(restore)


def test_backup_posture_accepts_only_the_exact_observed_read_identity() -> None:
    connection = _PostureConnection(_safe_backup_posture())

    _require_backup_posture(
        connection,  # type: ignore[arg-type]
        expected_database="control",
        max_statement_timeout_ms=900_000,
    )

    assert len(connection.queries) == 1
    assert "pg_catalog.pg_auth_members" in connection.queries[0]
    assert "current_setting('transaction_read_only')" in connection.queries[0]


@pytest.mark.parametrize(
    ("position", "unsafe_value"),
    [
        (0, "schemabridge_migrator"),
        (1, "schemabridge_migrator"),
        (2, "another_control_database"),
        (3, False),
        (4, True),
        (5, True),
        (6, True),
        (7, True),
        (8, True),
        (9, True),
        (10, False),
        (11, False),
        (12, False),
        (13, 0),
        (13, 900_001),
    ],
)
def test_backup_posture_rejects_every_unsafe_observed_fact(
    position: int,
    unsafe_value: object,
) -> None:
    row = list(_safe_backup_posture())
    row[position] = unsafe_value

    with pytest.raises(ControlPlaneOperationError) as raised:
        _require_backup_posture(
            _PostureConnection(tuple(row)),  # type: ignore[arg-type]
            expected_database="control",
            max_statement_timeout_ms=900_000,
        )

    assert raised.value.code is ControlPlaneOperationErrorCode.BACKUP_FAILED
    assert str(raised.value) == "control-plane backup identity posture is invalid"


def test_signed_manifest_and_archive_verify_exactly(tmp_path: Path) -> None:
    archive, manifest_path, manifest = _artifacts(tmp_path)

    loaded = _load_and_verify_manifest(
        archive,
        manifest_path,
        {"v1": KEY},
    )

    assert loaded == manifest


@pytest.mark.parametrize("tamper", ["manifest", "archive", "permissions", "wrong-key"])
def test_tampered_or_overexposed_artifact_fails_before_restore(
    tmp_path: Path,
    tamper: str,
) -> None:
    archive, manifest_path, manifest = _artifacts(tmp_path)
    keys = {"v1": KEY}
    if tamper == "manifest":
        payload = manifest.model_dump(mode="json")
        payload["state_sha256"] = "f" * 64
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    elif tamper == "archive":
        archive.write_bytes(b"changed archive")
    elif tamper == "permissions":
        os.chmod(archive, 0o640)
    else:
        keys = {"v1": b"different-audit-key-0123456789-abcd"}

    with pytest.raises(ControlPlaneOperationError) as raised:
        _load_and_verify_manifest(archive, manifest_path, keys)

    assert raised.value.code is ControlPlaneOperationErrorCode.ARTIFACT_INVALID


def test_source_database_restore_is_rejected_before_runner_or_database_io(
    tmp_path: Path,
) -> None:
    archive, manifest_path, _ = _artifacts(tmp_path)
    runner_calls = 0

    def forbidden_runner(
        command: Sequence[str],
        environment: Mapping[str, str],
        timeout_seconds: int,
    ) -> int:
        nonlocal runner_calls
        del command, environment, timeout_seconds
        runner_calls += 1
        return 0

    restore = PostgresControlPlaneRestore(
        DSN,
        ROOT / "migrations/control_plane",
        {"v1": KEY},
        runner=forbidden_runner,
    )

    with pytest.raises(ControlPlaneOperationError) as raised:
        restore.restore_backup(archive, manifest_path)

    assert raised.value.code is ControlPlaneOperationErrorCode.TARGET_IS_SOURCE
    assert runner_calls == 0


def test_manifest_rejects_traversal_archive_name() -> None:
    payload = {
        "source_database_fingerprint": "a" * 64,
        "schema_name": "schemabridge_control",
        "schema_version": 1,
        "schema_checksum": "b" * 64,
        "archive_name": "../control.dump",
        "archive_size_bytes": 1,
        "archive_sha256": "c" * 64,
        "state_sha256": "d" * 64,
        "table_counts": {"schema_migrations": 1},
        "audit_key_version": "v1",
        "created_at": NOW,
        "manifest_hmac": "e" * 64,
    }

    with pytest.raises(ValidationError):
        ControlPlaneBackupManifest.model_validate(payload)
