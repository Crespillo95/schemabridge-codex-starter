"""Inspect, prepare, separately approve, and CAS-apply connector routes."""

from __future__ import annotations

import json
import os
import stat
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Never

import typer
from pydantic import ValidationError

from schemabridge.application.connector_route_operator import (
    ConnectorRouteOperatorError,
    PreparedConnectorRouteChange,
)
from schemabridge.application.ports.connector_route_operator import (
    ConnectorPrivateBindings,
)
from schemabridge.application.ports.control_plane_migrations import (
    ControlPlaneMigrationError,
)
from schemabridge.application.postgres_health import DatabaseConfigurationError
from schemabridge.bootstrap import (
    build_connector_route_operator,
    resolve_control_operator_actor,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    ConnectorRouteApproval,
    ConnectorRouteOperation,
    ConnectorRouteProposal,
    ConnectorRouteSnapshot,
    QueryCostBudget,
    postgres_source_identity_fingerprint,
)

MAX_OPERATOR_FILE_BYTES = 65_536
app = typer.Typer(
    help="Operate governed connector routes through exact owner-only artifacts.",
    no_args_is_help=True,
)


@app.command("fingerprint-source-identity")
def fingerprint_source_identity(
    identity_file: Annotated[Path, typer.Option("--identity-file")],
    actor: Annotated[str | None, typer.Option("--actor")] = None,
) -> None:
    """Derive a public SHA-256 from owner-only PostgreSQL-observed identity."""

    try:
        resolve_control_operator_actor(actor, required_role="platform_admin")
        document = _read_owner_only_json(identity_file)
        if set(document) != {"format_version", "source_identity"}:
            raise ValueError("PostgreSQL source identity document shape is invalid")
        identity = document["source_identity"]
        if document["format_version"] != 1 or not isinstance(identity, dict):
            raise ValueError("PostgreSQL source identity document shape is invalid")
        if set(identity) != {"database", "server_address", "server_port", "user"}:
            raise ValueError("PostgreSQL source identity document shape is invalid")
        fingerprint = postgres_source_identity_fingerprint(
            server_address=_strict_string(identity["server_address"]),
            server_port=_strict_integer(identity["server_port"]),
            database=_strict_string(identity["database"]),
            user=_strict_string(identity["user"]),
        )
    except _OPERATOR_ERRORS as error:
        _fail(error)
    _emit(
        {
            "control_plane_writes_performed": False,
            "kind": "postgresql_source_identity_fingerprint",
            "source_identity_fingerprint": fingerprint,
        }
    )


@app.command("inspect")
def inspect_route(
    workspace_id: Annotated[str, typer.Option("--workspace-id")],
    connection_id: Annotated[str, typer.Option("--connection-id")],
    actor: Annotated[str | None, typer.Option("--actor")] = None,
) -> None:
    """Inspect current public route state and perform no write."""

    try:
        resolve_control_operator_actor(actor, required_role="platform_admin")
        current = build_connector_route_operator().inspect(
            workspace_id=workspace_id,
            connection_id=CatalogConnectionId(connection_id),
        )
    except _OPERATOR_ERRORS as error:
        _fail(error)
    _emit(
        {
            "control_plane_writes_performed": False,
            "kind": "connector_route_inspection",
            "route": None if current is None else _snapshot_payload(current),
        }
    )


@app.command("prepare")
def prepare_route(
    workspace_id: Annotated[str, typer.Option("--workspace-id")],
    connection_id: Annotated[str, typer.Option("--connection-id")],
    operation: Annotated[ConnectorRouteOperation, typer.Option("--operation")],
    expected_head_revision: Annotated[int, typer.Option("--expected-head-revision")],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    proposal_output: Annotated[Path, typer.Option("--proposal-output")],
    contract_version: Annotated[int | None, typer.Option("--contract-version")] = None,
    route_revision: Annotated[int | None, typer.Option("--route-revision")] = None,
    expected_reader: Annotated[str | None, typer.Option("--expected-reader")] = None,
    source_identity_fingerprint: Annotated[
        str | None,
        typer.Option("--source-identity-fingerprint"),
    ] = None,
    catalog_identity_fingerprint: Annotated[
        str | None,
        typer.Option("--catalog-identity-fingerprint"),
    ] = None,
    type_contract_version: Annotated[
        int | None,
        typer.Option("--type-contract-version"),
    ] = None,
    type_contract_fingerprint: Annotated[
        str | None,
        typer.Option("--type-contract-fingerprint"),
    ] = None,
    explain_timeout_ms: Annotated[
        int | None,
        typer.Option("--explain-timeout-ms"),
    ] = None,
    max_response_bytes: Annotated[
        int | None,
        typer.Option("--max-response-bytes"),
    ] = None,
    max_total_cost: Annotated[str | None, typer.Option("--max-total-cost")] = None,
    max_estimated_rows: Annotated[
        int | None,
        typer.Option("--max-estimated-rows"),
    ] = None,
    max_plan_nodes: Annotated[int | None, typer.Option("--max-plan-nodes")] = None,
    max_plan_depth: Annotated[int | None, typer.Option("--max-plan-depth")] = None,
    max_plan_width: Annotated[int | None, typer.Option("--max-plan-width")] = None,
    private_bindings_file: Annotated[
        Path | None,
        typer.Option(
            "--private-bindings-file",
            help="Owner-only strict JSON; binding values never enter argv or output.",
        ),
    ] = None,
    actor: Annotated[str | None, typer.Option("--actor")] = None,
) -> None:
    """Prepare an exact owner-only proposal; the control plane remains unchanged."""

    try:
        resolved_actor = resolve_control_operator_actor(
            actor,
            required_role="platform_admin",
        )
        private_bindings = (
            None if private_bindings_file is None else _read_private_bindings(private_bindings_file)
        )
        budget = _optional_budget(
            explain_timeout_ms=explain_timeout_ms,
            max_response_bytes=max_response_bytes,
            max_total_cost=max_total_cost,
            max_estimated_rows=max_estimated_rows,
            max_plan_nodes=max_plan_nodes,
            max_plan_depth=max_plan_depth,
            max_plan_width=max_plan_width,
        )
        prepared = build_connector_route_operator().prepare(
            workspace_id=workspace_id,
            connection_id=CatalogConnectionId(connection_id),
            operation=operation,
            expected_head_revision=expected_head_revision,
            contract_version=contract_version,
            route_revision=route_revision,
            expected_reader=expected_reader,
            source_identity_fingerprint=source_identity_fingerprint,
            catalog_identity_fingerprint=catalog_identity_fingerprint,
            type_contract_version=type_contract_version,
            type_contract_fingerprint=type_contract_fingerprint,
            cost_budget=budget,
            private_bindings=private_bindings,
            proposed_by=resolved_actor,
            idempotency_key=idempotency_key,
        )
        _write_owner_only_json(proposal_output, _proposal_document(prepared))
    except _OPERATOR_ERRORS as error:
        _fail(error)
    _emit(
        {
            "control_plane_writes_performed": False,
            "kind": "connector_route_proposal_file",
            "operation": prepared.proposal.operation.value,
            "proposal_file_written": True,
            "proposal_fingerprint": prepared.proposal.fingerprint,
            "public_target_fingerprint": prepared.proposal.target.fingerprint,
        }
    )


@app.command("approve")
def approve_route(
    proposal_file: Annotated[Path, typer.Option("--proposal-file")],
    expected_proposal_fingerprint: Annotated[
        str,
        typer.Option("--expected-proposal-fingerprint"),
    ],
    confirmation: Annotated[str, typer.Option("--confirm")],
    approval_output: Annotated[Path, typer.Option("--approval-output")],
    actor: Annotated[str | None, typer.Option("--actor")] = None,
) -> None:
    """Create a separate exact approval artifact and perform no control-plane write."""

    try:
        resolved_actor = resolve_control_operator_actor(
            actor,
            required_role="platform_admin",
        )
        proposal = _read_proposal(proposal_file)
        approval = build_connector_route_operator().approve(
            proposal,
            expected_proposal_fingerprint=expected_proposal_fingerprint,
            confirmation=confirmation,
            approved_by=resolved_actor,
        )
        _write_owner_only_json(approval_output, _approval_document(approval))
    except _OPERATOR_ERRORS as error:
        _fail(error)
    _emit(
        {
            "approval_file_written": True,
            "approval_fingerprint": approval.approval_fingerprint,
            "approval_id": approval.approval_id,
            "control_plane_writes_performed": False,
            "kind": "connector_route_approval_file",
            "proposal_fingerprint": approval.proposal_fingerprint,
        }
    )


@app.command("apply")
def apply_route(
    proposal_file: Annotated[Path, typer.Option("--proposal-file")],
    approval_file: Annotated[Path, typer.Option("--approval-file")],
    expected_proposal_fingerprint: Annotated[
        str,
        typer.Option("--expected-proposal-fingerprint"),
    ],
    expected_approval_fingerprint: Annotated[
        str,
        typer.Option("--expected-approval-fingerprint"),
    ],
    private_bindings_file: Annotated[
        Path | None,
        typer.Option("--private-bindings-file"),
    ] = None,
    actor: Annotated[str | None, typer.Option("--actor")] = None,
) -> None:
    """Apply one separately approved proposal through the database CAS function."""

    try:
        resolved_actor = resolve_control_operator_actor(
            actor,
            required_role="platform_admin",
        )
        proposal = _read_proposal(proposal_file)
        private_bindings = (
            None if private_bindings_file is None else _read_private_bindings(private_bindings_file)
        )
        prepared = PreparedConnectorRouteChange(
            proposal=proposal,
            private_bindings=private_bindings,
        )
        approval = _read_approval(approval_file)
        if approval.approved_by != resolved_actor:
            raise ValueError("connector route approval actor is not current")
        result = build_connector_route_operator().apply(
            prepared,
            approval,
            expected_proposal_fingerprint=expected_proposal_fingerprint,
            expected_approval_fingerprint=expected_approval_fingerprint,
        )
    except _OPERATOR_ERRORS as error:
        _fail(error)
    _emit(
        {
            "audit_id": result.audit_id,
            "connection_id": result.connection_id.root,
            "contract_version": result.contract_version,
            "head_revision": result.head_revision,
            "kind": "connector_route_apply",
            "route_revision": result.route_revision,
            "status": result.status.value,
            "target_fingerprint": result.target_fingerprint,
            "workspace_id": result.workspace_id,
            "writes_performed": True,
        }
    )


def _optional_budget(
    *,
    explain_timeout_ms: int | None,
    max_response_bytes: int | None,
    max_total_cost: str | None,
    max_estimated_rows: int | None,
    max_plan_nodes: int | None,
    max_plan_depth: int | None,
    max_plan_width: int | None,
) -> QueryCostBudget | None:
    values = (
        explain_timeout_ms,
        max_response_bytes,
        max_total_cost,
        max_estimated_rows,
        max_plan_nodes,
        max_plan_depth,
        max_plan_width,
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError("connector route cost budget is incomplete")
    assert explain_timeout_ms is not None
    assert max_response_bytes is not None
    assert max_total_cost is not None
    assert max_estimated_rows is not None
    assert max_plan_nodes is not None
    assert max_plan_depth is not None
    assert max_plan_width is not None
    return QueryCostBudget(
        explain_timeout_ms=explain_timeout_ms,
        max_response_bytes=max_response_bytes,
        max_total_cost=Decimal(max_total_cost),
        max_estimated_rows=max_estimated_rows,
        max_plan_nodes=max_plan_nodes,
        max_plan_depth=max_plan_depth,
        max_plan_width=max_plan_width,
    )


def _proposal_document(prepared: PreparedConnectorRouteChange) -> dict[str, object]:
    return {
        "format_version": 1,
        "kind": "connector_route_proposal",
        "proposal": prepared.proposal.model_dump(mode="json"),
    }


def _approval_document(approval: ConnectorRouteApproval) -> dict[str, object]:
    return {
        "approval": approval.model_dump(mode="json"),
        "format_version": 1,
        "kind": "connector_route_approval",
    }


def _read_private_bindings(path: Path) -> ConnectorPrivateBindings:
    document = _read_owner_only_json(path)
    if set(document) != {"format_version", "private_bindings"}:
        raise ValueError("connector private binding document shape is invalid")
    if document["format_version"] != 1 or not isinstance(
        document["private_bindings"],
        dict,
    ):
        raise ValueError("connector private binding document shape is invalid")
    return _bindings_from_payload(document["private_bindings"])


def _read_proposal(path: Path) -> ConnectorRouteProposal:
    document = _read_owner_only_json(path)
    if set(document) != {"format_version", "kind", "proposal"}:
        raise ValueError("connector route proposal document shape is invalid")
    if (
        document["format_version"] != 1
        or document["kind"] != "connector_route_proposal"
        or not isinstance(document["proposal"], dict)
    ):
        raise ValueError("connector route proposal document shape is invalid")
    try:
        return ConnectorRouteProposal.model_validate(document["proposal"])
    except (TypeError, ValueError, ValidationError) as error:
        raise ValueError("connector route proposal contains invalid values") from error


def _read_approval(path: Path) -> ConnectorRouteApproval:
    document = _read_owner_only_json(path)
    if set(document) != {"approval", "format_version", "kind"}:
        raise ValueError("connector route approval document shape is invalid")
    if (
        document["format_version"] != 1
        or document["kind"] != "connector_route_approval"
        or not isinstance(document["approval"], dict)
    ):
        raise ValueError("connector route approval document shape is invalid")
    try:
        return ConnectorRouteApproval.model_validate(document["approval"])
    except (TypeError, ValueError, ValidationError) as error:
        raise ValueError("connector route approval contains invalid values") from error


def _bindings_from_payload(payload: dict[str, object]) -> ConnectorPrivateBindings:
    if set(payload) != {"catalog", "execution", "preflight", "profile"}:
        raise ValueError("connector private binding document shape is invalid")
    try:
        return ConnectorPrivateBindings(
            preflight=_strict_string(payload["preflight"]),
            catalog=_strict_string(payload["catalog"]),
            execution=_strict_string(payload["execution"]),
            profile=_strict_string(payload["profile"]),
        )
    except (TypeError, ValueError, KeyError) as error:
        raise ValueError("connector private bindings are invalid") from error


def _snapshot_payload(snapshot: ConnectorRouteSnapshot) -> dict[str, object]:
    target = snapshot.target
    return {
        "connection_id": snapshot.connection_id.root,
        "connection_status": snapshot.connection_status.value,
        "contract_version": snapshot.contract_version,
        "dialect": target.dialect.value,
        "expected_reader": target.expected_reader,
        "head_revision": snapshot.head_revision,
        "route_fingerprint": target.route_fingerprint,
        "route_revision": target.route_revision,
        "route_status": snapshot.route_status.value,
        "source_identity_fingerprint": target.source_identity_fingerprint,
        "catalog_identity_fingerprint": target.catalog_identity_fingerprint,
        "state_fingerprint": snapshot.fingerprint,
        "target_fingerprint": target.fingerprint,
        "type_contract_fingerprint": target.type_contract_fingerprint,
        "type_contract_version": snapshot.type_contract_version,
        "workspace_id": snapshot.workspace_id,
    }


def _read_owner_only_json(path: Path) -> dict[str, object]:
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) & 0o077
            or before.st_uid != os.geteuid()
            or not 1 <= before.st_size <= MAX_OPERATOR_FILE_BYTES
        ):
            raise ValueError("connector route operator file is unsafe")
        raw = _read_exact(descriptor, before.st_size)
        after = os.fstat(descriptor)
        if (
            os.read(descriptor, 1)
            or after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
        ):
            raise ValueError("connector route operator file changed while reading")
    except OSError as error:
        raise ValueError("connector route operator file is unavailable") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("connector route operator file is invalid") from error
    if not isinstance(document, dict):
        raise ValueError("connector route operator document is invalid")
    return document


def _write_owner_only_json(path: Path, document: dict[str, object]) -> None:
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if not 1 <= len(raw) <= MAX_OPERATOR_FILE_BYTES:
        raise ValueError("connector route operator output size is invalid")
    descriptor: int | None = None
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
        ):
            raise ValueError("connector route operator output is unsafe")
        remaining = memoryview(raw)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("connector route operator output write failed")
            remaining = remaining[written:]
        os.fsync(descriptor)
    except OSError as error:
        raise ValueError("connector route operator output is unavailable") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_exact(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            raise ValueError("connector route operator file changed while reading")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("connector route operator document contains duplicate keys")
        result[key] = value
    return result


def _strict_string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("connector route operator string is invalid")
    return value


def _strict_integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError("connector route operator integer is invalid")
    return value


def _emit(payload: dict[str, object]) -> None:
    typer.echo(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _fail(error: Exception) -> Never:
    raw_code = getattr(error, "code", "connector_route_configuration_error")
    code = raw_code.value if hasattr(raw_code, "value") else str(raw_code)
    _emit({"code": code, "ok": False})
    raise typer.Exit(code=1)


def main() -> None:
    app()


_OPERATOR_ERRORS = (
    ConnectorRouteOperatorError,
    ControlPlaneMigrationError,
    DatabaseConfigurationError,
    OSError,
    TypeError,
    ValueError,
    ValidationError,
)


if __name__ == "__main__":
    main()
