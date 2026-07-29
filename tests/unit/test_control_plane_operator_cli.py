from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click import unstyle
from typer.testing import CliRunner

import schemabridge.entrypoints.cli.main as cli_module
from schemabridge.application.postgres_health import DatabaseConfigurationError
from schemabridge.domain.catalog_inventory import (
    TenantCapacityPolicy,
    TenantCapacityPolicyChange,
    TenantCapacityPolicyConfirmation,
)
from schemabridge.domain.legacy_import import LegacyImportConfirmation
from schemabridge.entrypoints.cli.main import app

runner = CliRunner()


def test_managed_control_plane_help_accepts_the_explicit_operator_component() -> None:
    result = runner.invoke(
        app,
        ["control-plane", "migrate", "--help"],
        env={
            "SCHEMABRIDGE_ENVIRONMENT": "production",
            "SCHEMABRIDGE_COMPONENT": "operator",
            "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
            "SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL": (
                "postgresql://migrator:password@control.example.test/control?sslmode=verify-full"
            ),
        },
    )

    assert result.exit_code == 0, result.output


class _Counts:
    total = 7
    imported = 2
    quarantined = 3
    skipped = 2
    preview_rows_stripped = 9

    def model_dump(self, *, mode: str) -> dict[str, int]:
        assert mode == "json"
        return {
            "total": self.total,
            "imported": self.imported,
            "quarantined": self.quarantined,
            "skipped": self.skipped,
            "preview_rows_stripped": self.preview_rows_stripped,
        }


def test_capacity_apply_authenticates_and_writes_one_exact_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor_id = "sb_actor_v1_" + "a" * 64
    captured: dict[str, object] = {}

    class _Apply:
        def execute(self, change: TenantCapacityPolicyChange) -> TenantCapacityPolicy:
            captured["change"] = change
            return TenantCapacityPolicy(
                workspace_id=change.workspace_id,
                version=1,
                connection_limit=change.connection_limit,
                asset_limit=change.asset_limit,
                field_limit=change.field_limit,
                api_requests_per_minute=change.api_requests_per_minute,
                nonterminal_job_limit=change.nonterminal_job_limit,
                generation_retention_seconds=change.generation_retention_seconds,
                updated_by=change.updated_by,
                updated_at=cli_module.datetime(2026, 7, 23, tzinfo=cli_module.UTC),
            )

    monkeypatch.setattr(
        cli_module,
        "_control_operator_actor",
        lambda _actor, *, required_role: (
            actor_id
            if required_role == "platform_admin"
            else (_ for _ in ()).throw(AssertionError("unexpected role"))
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "build_tenant_capacity_policy_operator",
        lambda: _Apply(),
    )

    result = runner.invoke(
        app,
        [
            "control-plane",
            "capacity",
            "apply",
            "--workspace-id",
            "workspace-enterprise",
            "--expected-version",
            "0",
            "--connection-limit",
            "6000",
            "--asset-limit",
            "100000",
            "--field-limit",
            "1000000",
            "--api-requests-per-minute",
            "1000",
            "--nonterminal-job-limit",
            "100",
            "--generation-retention-seconds",
            "3600",
            "--confirm",
            TenantCapacityPolicyConfirmation.APPLY.value,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["policy"]["version"] == 1
    assert payload["writes_performed"] is True
    assert payload["immutable_revision_recorded"] is True
    change = captured["change"]
    assert isinstance(change, TenantCapacityPolicyChange)
    assert change.updated_by == actor_id
    assert change.asset_limit == 100_000
    assert change.confirmation is TenantCapacityPolicyConfirmation.APPLY


def test_capacity_apply_authenticates_before_building_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = False

    def _deny_actor(_actor: object, *, required_role: str) -> str:
        assert required_role == "platform_admin"
        raise DatabaseConfigurationError("operator is not authorized")

    def _build() -> object:
        nonlocal built
        built = True
        raise AssertionError("unauthorized capacity change must not build a writer")

    monkeypatch.setattr(cli_module, "_control_operator_actor", _deny_actor)
    monkeypatch.setattr(cli_module, "build_tenant_capacity_policy_operator", _build)

    result = runner.invoke(
        app,
        [
            "control-plane",
            "capacity",
            "apply",
            "--workspace-id",
            "workspace-enterprise",
            "--expected-version",
            "0",
            "--connection-limit",
            "10",
            "--asset-limit",
            "100",
            "--field-limit",
            "1000",
            "--api-requests-per-minute",
            "100",
            "--nonterminal-job-limit",
            "10",
            "--generation-retention-seconds",
            "1800",
            "--confirm",
            TenantCapacityPolicyConfirmation.APPLY.value,
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert built is False


def test_control_plane_check_inspects_all_seven_dedicated_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    inspection = SimpleNamespace(
        current_version=2,
        expected_version=2,
        pending=(),
    )

    class _Migrator:
        def require_current(self) -> SimpleNamespace:
            return inspection

    def build_migrator(*, credential_kind: str) -> _Migrator:
        calls.append(credential_kind)
        return _Migrator()

    separation = SimpleNamespace(
        separate=True,
        source=SimpleNamespace(database="source", user="reader"),
        control=SimpleNamespace(database="control", user="runtime"),
    )
    monkeypatch.setattr(cli_module, "build_control_plane_migrator", build_migrator)
    monkeypatch.setattr(
        cli_module,
        "build_source_control_database_separation",
        lambda: SimpleNamespace(execute=lambda: separation),
    )

    result = runner.invoke(app, ["control-plane", "check", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert tuple(payload["roles"]) == (
        "runtime",
        "reconciler",
        "migrator",
        "api",
        "worker",
        "catalog",
        "observer",
    )
    assert calls == [
        "runtime",
        "reconciler",
        "migrator",
        "api",
        "worker",
        "catalog",
        "observer",
    ]
    assert payload["writes_performed"] is False


def _plan() -> SimpleNamespace:
    return SimpleNamespace(
        id="legacy-import-v1-" + "a" * 64,
        source_fingerprint="b" * 64,
        source_schema_fingerprint="c" * 64,
        fingerprint="d" * 64,
        counts=_Counts(),
        items=(
            SimpleNamespace(reason_code=None),
            SimpleNamespace(reason_code="orphan_workflow"),
            SimpleNamespace(reason_code="orphan_workflow"),
            SimpleNamespace(reason_code="fake_local_resource"),
        ),
    )


def test_legacy_import_inspect_exposes_only_bounded_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = _plan()
    source = tmp_path / "legacy.sqlite"

    class _Prepare:
        def execute(self, *, recorded_at: object) -> SimpleNamespace:
            assert recorded_at is not None
            return plan

    monkeypatch.setattr(cli_module, "resolve_runtime_profile", lambda: "development")
    monkeypatch.setattr(
        cli_module,
        "build_legacy_control_plane_import",
        lambda selected: SimpleNamespace(prepare=_Prepare()),
    )
    result = runner.invoke(
        app,
        [
            "control-plane",
            "legacy-import",
            "inspect",
            "--source",
            str(source),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == {
        "ok": True,
        "import_id": plan.id,
        "source_fingerprint": plan.source_fingerprint,
        "source_schema_fingerprint": plan.source_schema_fingerprint,
        "plan_fingerprint": plan.fingerprint,
        "counts": plan.counts.model_dump(mode="json"),
        "reason_counts": {
            "fake_local_resource": 1,
            "orphan_workflow": 2,
        },
        "dry_run_reserved": True,
        "target_rows_written": 0,
        "source_payloads_exposed": False,
    }
    assert "target_payload" not in result.stdout
    assert str(source) not in result.stdout


def test_legacy_import_apply_reconstructs_and_approves_the_exact_plan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = _plan()
    captured: dict[str, object] = {}

    class _Inspect:
        def execute(self) -> SimpleNamespace:
            return plan

    class _Apply:
        def execute(
            self,
            supplied_plan: object,
            approval: object,
            *,
            completed_at: object,
        ) -> SimpleNamespace:
            captured.update(
                plan=supplied_plan,
                approval=approval,
                completed_at=completed_at,
            )
            return SimpleNamespace(
                reservation=SimpleNamespace(
                    status=SimpleNamespace(value="completed"),
                    counts=plan.counts,
                )
            )

    class _Approver:
        def execute(
            self,
            supplied_plan: object,
            *,
            actor: str,
            approved_at: object,
            confirmation: LegacyImportConfirmation,
        ) -> SimpleNamespace:
            captured.update(
                approved_plan=supplied_plan,
                actor=actor,
                approved_at=approved_at,
                confirmation=confirmation,
            )
            return SimpleNamespace(id="legacy-import-approval-v1-" + "e" * 64)

    monkeypatch.setattr(cli_module, "resolve_runtime_profile", lambda: "development")
    monkeypatch.setattr(
        cli_module,
        "_control_operator_actor",
        lambda _actor, *, required_role: "sb_actor_v1_" + "f" * 64,
    )
    monkeypatch.setattr(
        cli_module,
        "build_legacy_control_plane_import",
        lambda _selected: SimpleNamespace(inspect=_Inspect(), apply=_Apply()),
    )
    monkeypatch.setattr(cli_module, "ApproveLegacyControlPlaneImport", _Approver)
    result = runner.invoke(
        app,
        [
            "control-plane",
            "legacy-import",
            "apply",
            "--source",
            str(tmp_path / "legacy.sqlite"),
            "--plan-fingerprint",
            plan.fingerprint,
            "--confirm",
            LegacyImportConfirmation.IMPORT_VALIDATED_LEGACY_CONTROL_STATE.value,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "completed"
    assert payload["plan_fingerprint"] == plan.fingerprint
    assert payload["source_payloads_exposed"] is False
    assert captured["plan"] is plan
    assert captured["approved_plan"] is plan
    assert captured["confirmation"] is (
        LegacyImportConfirmation.IMPORT_VALIDATED_LEGACY_CONTROL_STATE
    )


def test_legacy_import_apply_rejects_a_changed_plan_before_approval_or_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = _plan()
    applied = False
    prepared = False

    class _Inspect:
        def execute(self) -> SimpleNamespace:
            return plan

    class _Prepare:
        def execute(self, *, recorded_at: object) -> SimpleNamespace:
            nonlocal prepared
            prepared = True
            raise AssertionError("apply must never reserve another dry run")

    class _Apply:
        def execute(self, *_args: object, **_kwargs: object) -> object:
            nonlocal applied
            applied = True
            raise AssertionError("apply must not run")

    monkeypatch.setattr(cli_module, "resolve_runtime_profile", lambda: "development")
    monkeypatch.setattr(
        cli_module,
        "_control_operator_actor",
        lambda _actor, *, required_role: "sb_actor_v1_" + "f" * 64,
    )
    monkeypatch.setattr(
        cli_module,
        "build_legacy_control_plane_import",
        lambda _selected: SimpleNamespace(
            inspect=_Inspect(),
            prepare=_Prepare(),
            apply=_Apply(),
        ),
    )
    result = runner.invoke(
        app,
        [
            "control-plane",
            "legacy-import",
            "apply",
            "--source",
            str(tmp_path / "legacy.sqlite"),
            "--plan-fingerprint",
            "0" * 64,
            "--confirm",
            LegacyImportConfirmation.IMPORT_VALIDATED_LEGACY_CONTROL_STATE.value,
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "legacy_import_approval_mismatch"
    assert prepared is False
    assert applied is False


def test_legacy_import_apply_authenticates_before_building_or_inspecting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    built = False

    def _deny_actor(_actor: object, *, required_role: str) -> str:
        assert required_role == "publisher"
        raise DatabaseConfigurationError("operator is not authorized")

    def _build(_selected: Path) -> object:
        nonlocal built
        built = True
        raise AssertionError("unauthorized apply must not inspect or write")

    monkeypatch.setattr(cli_module, "_control_operator_actor", _deny_actor)
    monkeypatch.setattr(cli_module, "build_legacy_control_plane_import", _build)

    result = runner.invoke(
        app,
        [
            "control-plane",
            "legacy-import",
            "apply",
            "--source",
            str(tmp_path / "legacy.sqlite"),
            "--plan-fingerprint",
            "0" * 64,
            "--confirm",
            LegacyImportConfirmation.IMPORT_VALIDATED_LEGACY_CONTROL_STATE.value,
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "control_plane_configuration_error"
    assert built is False


def test_legacy_import_help_never_accepts_secrets_or_raw_identity() -> None:
    result = runner.invoke(
        app,
        ["control-plane", "legacy-import", "apply", "--help"],
    )

    assert result.exit_code == 0
    help_output = unstyle(result.stdout)
    assert "--plan-fingerprint" in help_output
    assert "--confirm" in help_output
    for forbidden in ("--dsn", "--token", "--key", "--claims", "--subject", "--email"):
        assert forbidden not in help_output
