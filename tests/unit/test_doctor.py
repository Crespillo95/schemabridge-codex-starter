"""Tests for the executable baseline."""

from pathlib import Path

from typer.testing import CliRunner

from schemabridge.application.doctor import run_doctor, supports_python_version
from schemabridge.entrypoints.cli.main import app

runner = CliRunner()


def test_doctor_reports_missing_required_files(tmp_path: Path) -> None:
    report = run_doctor(tmp_path)

    assert report.is_healthy is False
    project_check = next(check for check in report.checks if check.name == "project-files")
    assert project_check.ok is False
    repository_check = next(check for check in report.checks if check.name == "git-repository")
    assert repository_check.ok is False


def test_doctor_accepts_only_supported_python_minors() -> None:
    assert supports_python_version((3, 10)) is False
    assert supports_python_version((3, 11)) is True
    assert supports_python_version((3, 13)) is True
    assert supports_python_version((3, 14)) is False


def test_cli_version() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "0.1.0"


def test_cli_doctor_json_from_repository() -> None:
    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0
    assert '"healthy": true' in result.stdout
