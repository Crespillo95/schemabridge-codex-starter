from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import schemabridge.entrypoints.backup.main as backup_main
from schemabridge.application.ports.control_plane_operations import (
    ControlPlaneOperationError,
    ControlPlaneOperationErrorCode,
)
from schemabridge.application.postgres_health import DatabaseConfigurationError


class _Backup:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.destinations: list[Path] = []

    def create_backup(self, destination: Path) -> tuple[Path, Path, SimpleNamespace]:
        self.destinations.append(destination)
        if self.failure is not None:
            raise self.failure
        manifest = SimpleNamespace(
            schema_version=12,
            schema_checksum="a" * 64,
            archive_sha256="b" * 64,
            state_sha256="c" * 64,
            created_at=datetime(2026, 7, 30, 10, 0, tzinfo=UTC),
        )
        return (
            destination / "private-archive.dump",
            destination / "private-manifest.json",
            manifest,
        )


def test_scheduled_backup_emits_only_path_free_bounded_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    destination = (tmp_path / "private-backup-root").resolve()
    backup = _Backup()
    monkeypatch.setattr(
        backup_main,
        "build_control_plane_backup",
        lambda: SimpleNamespace(create_backup=backup.create_backup),
    )

    status = backup_main.command(["--destination", str(destination)])

    captured = capsys.readouterr()
    assert status == 0
    assert captured.err == ""
    assert backup.destinations == [destination]
    assert json.loads(captured.out) == {
        "archive_sha256": "b" * 64,
        "command": "backup",
        "control_schema_checksum": "a" * 64,
        "control_schema_version": 12,
        "created_at": "2026-07-30T10:00:00+00:00",
        "outcome": "succeeded",
        "schema_version": "schemabridge.scheduled-backup.v1",
        "state_sha256": "c" * 64,
    }
    assert str(destination) not in captured.out
    assert "private-archive" not in captured.out


def test_scheduled_backup_failure_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_marker = "postgresql://backup:must-not-leak@private.example/control"
    backup = _Backup(failure=DatabaseConfigurationError(private_marker))
    monkeypatch.setattr(
        backup_main,
        "build_control_plane_backup",
        lambda: SimpleNamespace(create_backup=backup.create_backup),
    )

    status = backup_main.command(["--destination", str(tmp_path.resolve())])

    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "code": "backup_configuration_invalid",
        "command": "backup",
        "outcome": "failed",
        "schema_version": "schemabridge.scheduled-backup.v1",
    }
    assert private_marker not in captured.err
    assert str(tmp_path) not in captured.err


def test_scheduled_backup_identity_failure_never_emits_private_error_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_marker = "postgresql://schemabridge_backup:must-not-leak@private.example/control"
    backup = _Backup(
        failure=ControlPlaneOperationError(
            ControlPlaneOperationErrorCode.BACKUP_FAILED,
            private_marker,
        )
    )
    monkeypatch.setattr(
        backup_main,
        "build_control_plane_backup",
        lambda: SimpleNamespace(create_backup=backup.create_backup),
    )

    status = backup_main.command(["--destination", str(tmp_path.resolve())])

    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert json.loads(captured.err)["code"] == "backup_operation_failed"
    assert private_marker not in captured.err
    assert "schemabridge_backup" not in captured.err


def test_scheduled_backup_rejects_relative_or_unknown_arguments_without_echo(
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_marker = "private-relative-destination"

    status = backup_main.command(["--destination", private_marker, "--unknown", "must-not-echo"])

    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert json.loads(captured.err)["code"] == "backup_arguments_invalid"
    assert private_marker not in captured.err
    assert "must-not-echo" not in captured.err
