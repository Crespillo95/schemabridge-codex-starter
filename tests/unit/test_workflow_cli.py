"""CLI coverage for restart-safe M12 workflow inspection."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

import schemabridge.entrypoints.cli.main as cli_module
from schemabridge.application.ports.control_plane_migrations import (
    ControlPlaneMigrationHistoryRecord,
    ControlPlaneMigrationInspection,
    ControlPlaneMigrationResult,
)
from schemabridge.domain.control_plane_operations import (
    ControlPlaneRestoreVerification,
)
from schemabridge.entrypoints.cli.main import app

runner = CliRunner()


def test_workflow_demo_start_then_show_restores_same_pause(tmp_path: Path) -> None:
    environment = {
        "DATABASE_URL": (
            "postgresql://schemabridge_reader:schemabridge_reader@127.0.0.1:55433/schemabridge"
        ),
        "SCHEMABRIDGE_DRAFT_STORE_PATH": str(tmp_path / "workflow.db"),
    }
    started = runner.invoke(
        app,
        [
            "workflow-demo",
            "--action",
            "start",
            "--workflow-id",
            "cli-workflow",
            "--json",
        ],
        env=environment,
    )
    assert started.exit_code == 0, started.stdout
    started_payload = json.loads(started.stdout)

    restored = runner.invoke(
        app,
        [
            "workflow-demo",
            "--action",
            "show",
            "--workflow-id",
            "cli-workflow",
            "--json",
        ],
        env=environment,
    )
    assert restored.exit_code == 0, restored.stdout
    restored_payload = json.loads(restored.stdout)

    assert restored_payload == started_payload
    assert restored_payload["draft"]["stage"] == "decision_required"
    assert restored_payload["draft"]["checkpoint"]["kind"] == "intent_confirmation"
    assert restored_payload["publication_adapter"] == "fake:local-idempotency-only"
    assert '"sql"' not in restored.stdout.casefold()


def test_managed_profile_disables_legacy_caller_identified_cli(tmp_path: Path) -> None:
    connector_secret_directory = tmp_path / "connector-secrets"
    connector_secret_directory.mkdir(mode=0o700)
    environment = {
        "SCHEMABRIDGE_ENVIRONMENT": "production",
        "SCHEMABRIDGE_AUTH_MODE": "oidc",
        "SCHEMABRIDGE_OIDC_ISSUER": "https://identity.example.test",
        "SCHEMABRIDGE_OIDC_AUDIENCE": "schemabridge",
        "SCHEMABRIDGE_OIDC_PROVIDER": "corporate-oidc",
        "SCHEMABRIDGE_OIDC_ALLOWED_GROUPS": '{"operators":["analyst"]}',
        "SCHEMABRIDGE_OIDC_ALLOWED_TENANTS": '["tenant-a"]',
        "SCHEMABRIDGE_PSEUDONYMIZATION_KEY": ("unit-test-pseudonymization-key-at-least-32-bytes"),
        "SCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY": (
            "unit-test-query-studio-signing-key-with-distinct-material"
        ),
        "DATABASE_URL": (
            "postgresql://source_reader:source_password@source.example.test/source"
            "?sslmode=verify-full"
        ),
        "SCHEMABRIDGE_CONTROL_DATABASE_URL": (
            "postgresql://control_runtime:control_password@control.example.test/control"
            "?sslmode=verify-full"
        ),
        "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY": (
            "unit-test-control-audit-signing-key-with-diversity"
        ),
        "SCHEMABRIDGE_IDENTITY_MIGRATION_KEY": ("unit-test-identity-migration-key-with-diversity"),
        "SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY": str(connector_secret_directory),
        "SCHEMABRIDGE_DRAFT_STORE_PATH": str(tmp_path / "production-cli.db"),
    }

    result = runner.invoke(
        app,
        ["workflow-demo", "--action", "show", "--actor", "spoofed-operator"],
        env=environment,
    )

    assert result.exit_code == 2
    assert "cli_authentication_required" in result.stderr
    assert not (tmp_path / "production-cli.db").exists()


def test_managed_cli_sanitizes_malformed_runtime_configuration(tmp_path: Path) -> None:
    sensitive_marker = "malformed-secret-marker"
    environment = {
        "SCHEMABRIDGE_ENVIRONMENT": "production",
        "SCHEMABRIDGE_AUTH_MODE": "oidc",
        "SCHEMABRIDGE_OIDC_ISSUER": "https://identity.example.test",
        "SCHEMABRIDGE_OIDC_AUDIENCE": "schemabridge",
        "SCHEMABRIDGE_OIDC_PROVIDER": "corporate-oidc",
        "SCHEMABRIDGE_OIDC_ALLOWED_GROUPS": sensitive_marker,
        "SCHEMABRIDGE_DRAFT_STORE_PATH": str(tmp_path / "invalid-production-cli.db"),
    }

    result = runner.invoke(app, ["version"], env=environment)

    assert result.exit_code == 2
    assert "cli_runtime_configuration_invalid" in result.stderr
    assert "Traceback" not in result.stderr
    assert "SettingsError" not in result.stderr
    assert sensitive_marker not in result.stderr
    assert not (tmp_path / "invalid-production-cli.db").exists()


def test_managed_cli_allows_only_explicit_control_plane_operator_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeMigrator:
        def migrate(self) -> ControlPlaneMigrationResult:
            return ControlPlaneMigrationResult(
                inspection=ControlPlaneMigrationInspection(
                    expected_version=1,
                    applied=(
                        ControlPlaneMigrationHistoryRecord(
                            version=1,
                            name="initial_control_plane",
                            checksum="a" * 64,
                        ),
                    ),
                    pending=(),
                ),
                applied_versions=(1,),
            )

    def fake_builder(*, credential_kind: str, **_kwargs: object) -> FakeMigrator:
        calls.append(credential_kind)
        return FakeMigrator()

    monkeypatch.setattr(cli_module, "build_control_plane_migrator", fake_builder)
    secret_marker = "operator-secret-must-not-be-rendered"
    connector_secret_directory = tmp_path / "connector-secrets"
    connector_secret_directory.mkdir(mode=0o700)
    environment = {
        "SCHEMABRIDGE_ENVIRONMENT": "production",
        "SCHEMABRIDGE_AUTH_MODE": "oidc",
        "SCHEMABRIDGE_OIDC_ISSUER": "https://identity.example.test",
        "SCHEMABRIDGE_OIDC_AUDIENCE": "schemabridge",
        "SCHEMABRIDGE_OIDC_PROVIDER": "corporate-oidc",
        "SCHEMABRIDGE_OIDC_ALLOWED_GROUPS": '{"operators":["platform_admin"]}',
        "SCHEMABRIDGE_OIDC_ALLOWED_TENANTS": '["tenant-a"]',
        "SCHEMABRIDGE_PSEUDONYMIZATION_KEY": ("unit-test-pseudonymization-key-at-least-32-bytes"),
        "SCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY": (
            "unit-test-query-studio-signing-key-with-distinct-material"
        ),
        "DATABASE_URL": (
            "postgresql://source_reader:source_password@source.example.test/source"
            "?sslmode=verify-full"
        ),
        "SCHEMABRIDGE_CONTROL_DATABASE_URL": (
            "postgresql://control_runtime:runtime_password@control.example.test/control"
            "?sslmode=verify-full"
        ),
        "SCHEMABRIDGE_CONTROL_RECONCILER_DATABASE_URL": (
            "postgresql://control_reconciler:reconciler_password@control.example.test/control"
            "?sslmode=verify-full"
        ),
        "SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL": (
            f"postgresql://control_migrator:{secret_marker}@control.example.test/control"
            "?sslmode=verify-full"
        ),
        "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY": (
            "unit-test-control-audit-signing-key-with-diversity"
        ),
        "SCHEMABRIDGE_IDENTITY_MIGRATION_KEY": ("unit-test-identity-migration-key-with-diversity"),
        "SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY": str(connector_secret_directory),
    }

    result = runner.invoke(
        app,
        ["control-plane", "migrate", "--json"],
        env=environment,
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "current_version": 1,
        "expected_version": 1,
        "applied_versions": [1],
        "already_current": False,
    }
    assert calls == ["migrator"]
    assert secret_marker not in result.output


def test_control_plane_restore_never_accepts_or_renders_the_target_dsn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_secret = "restore-target-secret-marker"
    observed: list[tuple[Path, Path]] = []
    verification = ControlPlaneRestoreVerification(
        target_database_fingerprint="a" * 64,
        schema_version=1,
        schema_checksum="b" * 64,
        state_sha256="c" * 64,
        table_counts={"schema_migrations": 1},
        audited_workspaces=1,
        audit_events=2,
        active_pointers=1,
        transition_records=1,
        pending_outbox_records=0,
        quarantine_records=0,
        verified_at=datetime(2026, 7, 23, 18, 0, tzinfo=UTC),
    )

    class FakeRestore:
        def restore_backup(
            self,
            archive: Path,
            manifest: Path,
        ) -> ControlPlaneRestoreVerification:
            observed.append((archive, manifest))
            return verification

    def fake_builder() -> FakeRestore:
        return FakeRestore()

    monkeypatch.setattr(cli_module, "build_control_plane_restore", fake_builder)
    archive = tmp_path / "synthetic.dump"
    manifest = tmp_path / "synthetic.manifest.json"

    result = runner.invoke(
        app,
        [
            "control-plane",
            "restore",
            "--archive",
            str(archive),
            "--manifest",
            str(manifest),
            "--json",
        ],
    )
    help_result = runner.invoke(app, ["control-plane", "restore", "--help"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["target_database_fingerprint"] == "a" * 64
    assert observed == [(archive, manifest)]
    assert "--target-dsn" not in help_result.output
    assert target_secret not in result.output
