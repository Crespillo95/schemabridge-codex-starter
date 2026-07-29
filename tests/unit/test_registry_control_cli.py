"""Composition and operator-CLI checks for M23 registry control."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click import unstyle
from typer.testing import CliRunner

import schemabridge.bootstrap as bootstrap_module
import schemabridge.entrypoints.cli.main as cli_module
from schemabridge.adapters.semantic_registry.datahub_projection import (
    DataHubRegistryProjectionAdapter,
)
from schemabridge.application.registry_control import (
    CommitRegistryActivation,
    InspectRegistryReconciliation,
    PrepareRegistryActivation,
    PrepareRegistryRollback,
    ReconcileRegistryProjection,
)
from schemabridge.bootstrap import (
    build_registry_activation_committer,
    build_registry_activation_preparer,
    build_registry_projection,
    build_registry_reconciliation_inspector,
    build_registry_reconciliation_repairer,
    build_registry_rollback_preparer,
)
from schemabridge.config import Settings
from schemabridge.domain.registry_control import (
    ActiveRegistryPointer,
    RegistryActivationAction,
    RegistryActivationConfirmation,
    RegistryActivationProposal,
    RegistryProjectionState,
    RegistryReconciliationCode,
    RegistryReconciliationConfirmation,
    RegistryReconciliationFinding,
    RegistryReconciliationReport,
    RegistryReconciliationSeverity,
    registry_projection_fingerprint,
)
from schemabridge.domain.semantic_registry import (
    SemanticRegistryScope,
    datahub_registry_document_urn,
)
from schemabridge.entrypoints.cli.main import app

runner = CliRunner()
SCOPE = SemanticRegistryScope(
    workspace_id="sb_workspace_cli",
    catalog_scope="synthetic-demo",
    registry_id="synthetic_enterprise",
)
INSPECTED_AT = datetime(2026, 7, 23, 18, 30, tzinfo=UTC)


def test_managed_operator_actor_is_configured_and_never_trusted_from_argv() -> None:
    actor = "sb_actor_v2_" + "a" * 64
    settings = SimpleNamespace(
        runtime_profile="production",
        control_operator_actor_id=actor,
        control_operator_roles=("platform_admin",),
    )

    assert (
        bootstrap_module.resolve_control_operator_actor(
            None,
            required_role="publisher",
            settings=settings,
        )
        == actor
    )
    with pytest.raises(
        cli_module.DatabaseConfigurationError,
        match="cannot be supplied on the command line",
    ):
        bootstrap_module.resolve_control_operator_actor(
            "spoofed",
            required_role="publisher",
            settings=settings,
        )


def _proposal() -> RegistryActivationProposal:
    return RegistryActivationProposal(
        action=RegistryActivationAction.ACTIVATE,
        scope=SCOPE,
        expected_generation=0,
        target_registry_version=2,
        target_registry_fingerprint="a" * 64,
        target_registry_urn=datahub_registry_document_urn(SCOPE, 2),
        target_publication_approval_id="publication-v2",
        decision_ids=("decision-1",),
    )


def _report() -> RegistryReconciliationReport:
    pointer = ActiveRegistryPointer(
        scope=SCOPE,
        generation=2,
        registry_version=2,
        registry_fingerprint="a" * 64,
        registry_target=datahub_registry_document_urn(SCOPE, 2),
        transition_id=f"registry-transition-v1-{'b' * 64}",
        activated_by="registry-operator",
        activated_at=INSPECTED_AT,
        decision_ids=("decision-1",),
    )
    projection = RegistryProjectionState(
        pointer=pointer,
        projection_fingerprint=registry_projection_fingerprint(pointer),
    )
    return RegistryReconciliationReport(
        scope=SCOPE,
        active_pointer=pointer,
        desired_projection=projection,
        observed_projection=projection,
        pending_outbox=None,
        findings=(
            RegistryReconciliationFinding(
                code=RegistryReconciliationCode.IN_SYNC,
                severity=RegistryReconciliationSeverity.INFO,
                message="Projection matches.",
            ),
        ),
        inspected_at=INSPECTED_AT,
    )


def test_registry_use_case_builders_select_runtime_and_reconciler_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings.model_validate({})
    checks: list[str] = []
    runtime_store = object()
    reconciler_store = object()
    versions = object()
    projection = object()

    def check_schema(**kwargs: object) -> object:
        checks.append(str(kwargs["credential_kind"]))
        return object()

    def control_store(**kwargs: object) -> object:
        if kwargs.get("credential_kind") == "reconciler":
            return reconciler_store
        return runtime_store

    monkeypatch.setattr(
        bootstrap_module,
        "require_current_control_plane_schema",
        check_schema,
    )
    monkeypatch.setattr(bootstrap_module, "build_registry_control_store", control_store)
    monkeypatch.setattr(
        bootstrap_module,
        "build_registry_version_reader",
        lambda **_kwargs: versions,
    )
    monkeypatch.setattr(
        bootstrap_module,
        "build_registry_projection",
        lambda **_kwargs: projection,
    )

    activation = build_registry_activation_preparer(
        workspace_id=SCOPE.workspace_id,
        settings=settings,
    )
    rollback = build_registry_rollback_preparer(
        workspace_id=SCOPE.workspace_id,
        settings=settings,
    )
    committer = build_registry_activation_committer(settings=settings)
    inspection = build_registry_reconciliation_inspector(
        workspace_id=SCOPE.workspace_id,
        settings=settings,
    )
    repair = build_registry_reconciliation_repairer(settings=settings)

    assert isinstance(activation, PrepareRegistryActivation)
    assert isinstance(rollback, PrepareRegistryRollback)
    assert isinstance(committer, CommitRegistryActivation)
    assert activation.store is runtime_store
    assert rollback.store is runtime_store
    assert committer.store is runtime_store
    assert isinstance(inspection, InspectRegistryReconciliation)
    assert isinstance(repair, ReconcileRegistryProjection)
    assert inspection.store is reconciler_store
    assert repair.store is reconciler_store
    assert inspection.projection is projection
    assert repair.projection is projection
    assert checks == ["runtime", "runtime", "runtime", "reconciler", "reconciler"]


def test_projection_builder_reads_owner_only_writer_configuration(tmp_path: Path) -> None:
    credential = tmp_path / ".local/datahub/writer.env"
    credential.parent.mkdir(parents=True)
    credential.write_text(
        "DATAHUB_GMS_URL=http://127.0.0.1:8080\n"
        "DATAHUB_GMS_TOKEN=projection-secret-token\n"
        "DATAHUB_WRITER_ACTOR_URN=urn:li:corpuser:registry-reconciler\n",
        encoding="utf-8",
    )
    credential.chmod(0o600)

    projection = build_registry_projection(repository_root=tmp_path)

    assert isinstance(projection, DataHubRegistryProjectionAdapter)
    assert "projection-secret-token" not in repr(projection)


def test_registry_activate_requires_the_exact_prepared_fingerprint_and_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = _proposal()
    captured: dict[str, Any] = {}

    class FakePreparer:
        def execute(self, target_version: int) -> RegistryActivationProposal:
            assert target_version == 2
            return proposal

    class FakeCommit:
        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {"transition_id": "registry-transition-v1-test"}

    class FakeCommitter:
        def execute(
            self,
            prepared: RegistryActivationProposal,
            approval: object,
            *,
            committed_at: datetime,
        ) -> FakeCommit:
            captured.update(
                proposal=prepared,
                approval=approval,
                committed_at=committed_at,
            )
            return FakeCommit()

    monkeypatch.setattr(cli_module, "resolve_runtime_profile", lambda: "development")
    monkeypatch.setattr(
        cli_module,
        "build_registry_activation_preparer",
        lambda **_kwargs: FakePreparer(),
    )
    monkeypatch.setattr(
        cli_module,
        "build_registry_activation_committer",
        lambda: FakeCommitter(),
    )
    monkeypatch.setattr(
        cli_module,
        "_control_operator_actor",
        lambda _actor, *, required_role: "registry-operator",
    )

    result = runner.invoke(
        app,
        [
            "control-plane",
            "registry",
            "activate",
            "--workspace-id",
            SCOPE.workspace_id,
            "--target-version",
            "2",
            "--proposal-fingerprint",
            proposal.fingerprint,
            "--actor",
            "registry-operator",
            "--confirm",
            RegistryActivationConfirmation.ACTIVATE_APPROVED_REGISTRY_VERSION.value,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["proposal_fingerprint"] == proposal.fingerprint
    assert payload["writes_performed"] is True
    approval = captured["approval"]
    assert approval.actor == "registry-operator"
    assert (
        approval.confirmation is RegistryActivationConfirmation.ACTIVATE_APPROVED_REGISTRY_VERSION
    )


def test_registry_activate_rejects_a_stale_fingerprint_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = _proposal()

    class FakePreparer:
        def execute(self, _target_version: int) -> RegistryActivationProposal:
            return proposal

    monkeypatch.setattr(cli_module, "resolve_runtime_profile", lambda: "development")
    monkeypatch.setattr(
        cli_module,
        "build_registry_activation_preparer",
        lambda **_kwargs: FakePreparer(),
    )
    monkeypatch.setattr(
        cli_module,
        "build_registry_activation_committer",
        lambda: pytest.fail("stale activation must not compose a committer"),
    )

    result = runner.invoke(
        app,
        [
            "control-plane",
            "registry",
            "activate",
            "--workspace-id",
            SCOPE.workspace_id,
            "--target-version",
            "2",
            "--proposal-fingerprint",
            "f" * 64,
            "--actor",
            "registry-operator",
            "--confirm",
            RegistryActivationConfirmation.ACTIVATE_APPROVED_REGISTRY_VERSION.value,
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "registry_activation_approval_mismatch"


def test_reconcile_repair_reconstructs_the_exact_timestamped_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _report()
    captured: dict[str, Any] = {}

    class FakeInspector:
        def execute(self, *, inspected_at: datetime) -> RegistryReconciliationReport:
            captured["inspected_at"] = inspected_at
            assert inspected_at == INSPECTED_AT
            return report

    class FakeOutcome:
        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {"status": "delivered", "generation": 2}

    class FakeRepairer:
        def execute(
            self,
            inspected: RegistryReconciliationReport,
            approval: object,
            *,
            occurred_at: datetime,
        ) -> FakeOutcome:
            captured.update(
                report=inspected,
                approval=approval,
                occurred_at=occurred_at,
            )
            return FakeOutcome()

    monkeypatch.setattr(cli_module, "resolve_runtime_profile", lambda: "development")
    monkeypatch.setattr(
        cli_module,
        "build_registry_reconciliation_inspector",
        lambda **_kwargs: FakeInspector(),
    )
    monkeypatch.setattr(
        cli_module,
        "build_registry_reconciliation_repairer",
        lambda: FakeRepairer(),
    )
    monkeypatch.setattr(
        cli_module,
        "_control_operator_actor",
        lambda _actor, *, required_role: "reconciliation-operator",
    )

    result = runner.invoke(
        app,
        [
            "control-plane",
            "reconcile",
            "repair",
            "--workspace-id",
            SCOPE.workspace_id,
            "--report-fingerprint",
            report.fingerprint,
            "--inspected-at",
            INSPECTED_AT.isoformat(),
            "--actor",
            "reconciliation-operator",
            "--confirm",
            RegistryReconciliationConfirmation.REPAIR_ACTIVE_REGISTRY_PROJECTION.value,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["report_fingerprint"] == report.fingerprint
    assert payload["writes_performed"] is True
    approval = captured["approval"]
    assert approval.actor == "reconciliation-operator"
    assert (
        approval.confirmation
        is RegistryReconciliationConfirmation.REPAIR_ACTIVE_REGISTRY_PROJECTION
    )


def test_registry_operator_help_exposes_prepare_commit_and_repair_boundaries() -> None:
    registry = runner.invoke(app, ["control-plane", "registry", "--help"])
    reconcile = runner.invoke(app, ["control-plane", "reconcile", "--help"])
    repair = runner.invoke(app, ["control-plane", "reconcile", "repair", "--help"])

    assert registry.exit_code == 0
    registry_output = unstyle(registry.output)
    assert "prepare-activation" in registry_output
    assert "prepare-rollback" in registry_output
    assert "activate" in registry_output
    assert "rollback" in registry_output
    assert reconcile.exit_code == 0
    reconcile_output = unstyle(reconcile.output)
    assert "inspect" in reconcile_output
    assert "repair" in reconcile_output
    assert repair.exit_code == 0
    repair_output = unstyle(repair.output)
    assert "--report-fingerprint" in repair_output
    assert "--inspected-at" in repair_output
    assert "--actor" in repair_output
    assert "--confirm" in repair_output


def test_registry_prepare_derives_the_explicit_v3_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "resolve_runtime_profile", lambda: "development")

    result = runner.invoke(
        app,
        ["registry-prepare", "--target-version", "3", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["version"] == 3
    assert "-v3-" in payload["target"]
    assert payload["writes_performed"] is False
