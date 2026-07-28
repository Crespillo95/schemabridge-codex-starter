from __future__ import annotations

import json
import stat
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from typer.testing import CliRunner

from schemabridge.application.connector_route_operator import ConnectorRouteOperator
from schemabridge.application.ports.connector_route_operator import ConnectorRouteWrite
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    ConnectorRouteApplyResult,
    ConnectorRouteOperation,
    ConnectorRouteSnapshot,
    ConnectorRouteStatus,
    GovernedExecutionTarget,
    SourceConnectorKind,
    SourceDialect,
    postgres_type_contract_fingerprint,
)
from schemabridge.entrypoints.connector_route import main as route_main
from schemabridge.entrypoints.connector_route.main import app

WORKSPACE_ID = "workspace-route-cli"
CONNECTION_ID = CatalogConnectionId("connection-route-cli")
ACTOR_ID = "platform-admin"
SOURCE_IDENTITY_FINGERPRINT = "1" * 64
CATALOG_IDENTITY_FINGERPRINT = "2" * 64
PRIVATE_VALUES = (
    "vault:preflight:cli-alpha",
    "vault:catalog:cli-bravo",
    "vault:execution:cli-charlie",
    "vault:profile:cli-delta",
)
RUNNER = CliRunner()


@dataclass
class _Store:
    current: ConnectorRouteSnapshot | None = None
    writes: list[ConnectorRouteWrite] = field(default_factory=list)

    def inspect(
        self,
        *,
        workspace_id: str,
        connection_id: CatalogConnectionId,
    ) -> ConnectorRouteSnapshot | None:
        assert workspace_id == WORKSPACE_ID
        assert connection_id == CONNECTION_ID
        return self.current

    def apply(self, change: ConnectorRouteWrite) -> ConnectorRouteApplyResult:
        self.writes.append(change)
        status = (
            ConnectorRouteStatus.DISABLED
            if change.operation is ConnectorRouteOperation.DISABLE
            else ConnectorRouteStatus.ENABLED
        )
        target = GovernedExecutionTarget(
            workspace_id=change.workspace_id,
            connection_id=change.connection_id,
            connector_kind=SourceConnectorKind.POSTGRESQL,
            dialect=SourceDialect.POSTGRESQL,
            route_revision=change.route_revision,
            route_fingerprint=change.route_fingerprint,
            expected_reader=change.expected_reader,
            source_identity_fingerprint=change.source_identity_fingerprint,
            catalog_identity_fingerprint=change.catalog_identity_fingerprint,
            type_contract_fingerprint=change.type_contract_fingerprint,
            cost_budget=change.cost_budget,
            cost_budget_fingerprint=change.cost_budget_fingerprint,
        )
        result = ConnectorRouteApplyResult(
            workspace_id=change.workspace_id,
            connection_id=change.connection_id,
            head_revision=change.expected_head_revision + 1,
            contract_version=change.contract_version,
            route_revision=change.route_revision,
            route_fingerprint=change.route_fingerprint,
            target_fingerprint=change.target_fingerprint,
            status=status,
            audit_id=change.audit_id,
        )
        self.current = ConnectorRouteSnapshot(
            workspace_id=change.workspace_id,
            connection_id=change.connection_id,
            head_revision=result.head_revision,
            route_status=status,
            connection_status=ConnectorRouteStatus.ENABLED,
            contract_version=change.contract_version,
            type_contract_version=change.type_contract_version,
            target=target,
        )
        return result


def _patch_operator(
    monkeypatch: pytest.MonkeyPatch,
    store: _Store,
) -> None:
    operator = ConnectorRouteOperator(store)
    monkeypatch.setattr(
        route_main,
        "resolve_control_operator_actor",
        lambda _actor, *, required_role: (
            ACTOR_ID
            if required_role == "platform_admin"
            else pytest.fail("unexpected route operator role")
        ),
    )
    monkeypatch.setattr(route_main, "build_connector_route_operator", lambda: operator)


def _write_bindings(path: Path, *, mode: int = 0o600) -> None:
    path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "private_bindings": {
                    "catalog": PRIVATE_VALUES[1],
                    "execution": PRIVATE_VALUES[2],
                    "preflight": PRIVATE_VALUES[0],
                    "profile": PRIVATE_VALUES[3],
                },
            }
        ),
        encoding="utf-8",
    )
    path.chmod(mode)


def _prepare_args(bindings_file: Path, proposal_file: Path) -> list[str]:
    return [
        "prepare",
        "--workspace-id",
        WORKSPACE_ID,
        "--connection-id",
        CONNECTION_ID.root,
        "--operation",
        "create",
        "--expected-head-revision",
        "0",
        "--idempotency-key",
        "connector-route-cli-create-0001",
        "--proposal-output",
        str(proposal_file),
        "--contract-version",
        "1",
        "--route-revision",
        "1",
        "--expected-reader",
        "schemabridge_source_reader",
        "--source-identity-fingerprint",
        SOURCE_IDENTITY_FINGERPRINT,
        "--catalog-identity-fingerprint",
        CATALOG_IDENTITY_FINGERPRINT,
        "--type-contract-version",
        "1",
        "--type-contract-fingerprint",
        postgres_type_contract_fingerprint(),
        "--explain-timeout-ms",
        "2500",
        "--max-response-bytes",
        "262144",
        "--max-total-cost",
        "12345.67",
        "--max-estimated-rows",
        "250000",
        "--max-plan-nodes",
        "500",
        "--max-plan-depth",
        "32",
        "--max-plan-width",
        "8192",
        "--private-bindings-file",
        str(bindings_file),
    ]


def test_cli_full_flow_separates_prepare_approval_and_apply_without_binding_leakage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _Store()
    _patch_operator(monkeypatch, store)
    bindings_file = tmp_path / "bindings.json"
    proposal_file = tmp_path / "proposal.json"
    approval_file = tmp_path / "approval.json"
    _write_bindings(bindings_file)

    prepared = RUNNER.invoke(app, _prepare_args(bindings_file, proposal_file))

    assert prepared.exit_code == 0, prepared.output
    prepared_summary = json.loads(prepared.stdout)
    assert prepared_summary["control_plane_writes_performed"] is False
    assert store.writes == []
    assert stat.S_IMODE(proposal_file.stat().st_mode) == 0o600
    proposal_text = proposal_file.read_text(encoding="utf-8")
    for private_value in PRIVATE_VALUES:
        assert private_value not in prepared.stdout
        assert private_value not in proposal_text

    approved = RUNNER.invoke(
        app,
        [
            "approve",
            "--proposal-file",
            str(proposal_file),
            "--expected-proposal-fingerprint",
            prepared_summary["proposal_fingerprint"],
            "--confirm",
            "CREATE CONNECTOR ROUTE",
            "--approval-output",
            str(approval_file),
        ],
    )

    assert approved.exit_code == 0, approved.output
    approval_summary = json.loads(approved.stdout)
    assert approval_summary["control_plane_writes_performed"] is False
    assert store.writes == []
    assert stat.S_IMODE(approval_file.stat().st_mode) == 0o600
    approval_text = approval_file.read_text(encoding="utf-8")
    for private_value in PRIVATE_VALUES:
        assert private_value not in approved.stdout
        assert private_value not in approval_text

    applied = RUNNER.invoke(
        app,
        [
            "apply",
            "--proposal-file",
            str(proposal_file),
            "--approval-file",
            str(approval_file),
            "--expected-proposal-fingerprint",
            prepared_summary["proposal_fingerprint"],
            "--expected-approval-fingerprint",
            approval_summary["approval_fingerprint"],
            "--private-bindings-file",
            str(bindings_file),
        ],
    )

    assert applied.exit_code == 0, applied.output
    applied_payload = json.loads(applied.stdout)
    assert applied_payload["writes_performed"] is True
    assert applied_payload["status"] == "enabled"
    assert len(store.writes) == 1
    for private_value in PRIVATE_VALUES:
        assert private_value not in applied.stdout


def test_cli_rejects_unsafe_or_tampered_files_before_store_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _Store()
    _patch_operator(monkeypatch, store)
    bindings_file = tmp_path / "bindings.json"
    proposal_file = tmp_path / "proposal.json"
    _write_bindings(bindings_file, mode=0o640)

    unsafe = RUNNER.invoke(app, _prepare_args(bindings_file, proposal_file))

    assert unsafe.exit_code == 1
    assert store.writes == []
    for private_value in PRIVATE_VALUES:
        assert private_value not in unsafe.stdout

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"format_version":1,"format_version":1,"private_bindings":{}}',
        encoding="utf-8",
    )
    duplicate.chmod(0o600)
    rejected = RUNNER.invoke(app, _prepare_args(duplicate, proposal_file))
    assert rejected.exit_code == 1
    assert store.writes == []


def test_cli_help_and_public_outputs_expose_no_binding_input_options() -> None:
    help_result = RUNNER.invoke(app, ["--help"])

    assert help_result.exit_code == 0
    for private_value in PRIVATE_VALUES:
        assert private_value not in help_result.stdout
    assert "--preflight-binding-ref" not in help_result.stdout
    assert "--execution-binding-ref" not in help_result.stdout


def test_cli_derives_source_identity_from_owner_only_file_without_topology_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _Store()
    _patch_operator(monkeypatch, store)
    identity_file = tmp_path / "source-identity.json"
    identity_file.write_text(
        json.dumps(
            {
                "format_version": 1,
                "source_identity": {
                    "database": "tenant_cli_private",
                    "server_address": "127.0.0.7",
                    "server_port": 55432,
                    "user": "schemabridge_source_reader",
                },
            }
        ),
        encoding="utf-8",
    )
    identity_file.chmod(0o600)

    result = RUNNER.invoke(
        app,
        [
            "fingerprint-source-identity",
            "--identity-file",
            str(identity_file),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert len(payload["source_identity_fingerprint"]) == 64
    assert payload["control_plane_writes_performed"] is False
    for protected in (
        "tenant_cli_private",
        "127.0.0.7",
        "55432",
        "schemabridge_source_reader",
    ):
        assert protected not in result.stdout
    assert store.writes == []
