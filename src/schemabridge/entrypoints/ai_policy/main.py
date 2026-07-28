"""Inspect, prepare, and exactly apply tenant external-AI policy."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Annotated, Never

import typer

from schemabridge.application.ports.control_plane_migrations import (
    ControlPlaneMigrationError,
)
from schemabridge.application.ports.query_studio_ai_control import (
    TenantAiPolicyOperatorSnapshot,
)
from schemabridge.application.postgres_health import DatabaseConfigurationError
from schemabridge.application.query_studio_ai_policy import (
    TenantAiPolicyDesired,
    TenantAiPolicyError,
    TenantAiPolicyProposal,
)
from schemabridge.bootstrap import (
    build_tenant_ai_policy_operator,
    resolve_control_operator_actor,
)

MAX_PROPOSAL_BYTES = 65_536
app = typer.Typer(
    help="Inspect, prepare, and exactly apply tenant external-AI policy.",
    no_args_is_help=True,
)


@app.command("inspect")
def inspect_policy(
    workspace_id: Annotated[str, typer.Option("--workspace-id")],
    actor: Annotated[str | None, typer.Option("--actor")] = None,
) -> None:
    """Read the complete non-secret current policy without writing."""

    try:
        resolve_control_operator_actor(actor, required_role="platform_admin")
        current = build_tenant_ai_policy_operator().inspect(workspace_id)
    except _OPERATOR_ERRORS as error:
        _fail(error)
    _emit(
        {
            "kind": "tenant_ai_policy_inspection",
            "policy": None if current is None else _snapshot_payload(current),
            "writes_performed": False,
        }
    )


@app.command("prepare")
def prepare_policy(
    workspace_id: Annotated[str, typer.Option("--workspace-id")],
    expected_version: Annotated[int, typer.Option("--expected-version")],
    external_ai_enabled: Annotated[
        bool,
        typer.Option("--external-ai-enabled/--external-ai-disabled"),
    ] = False,
    provider_governance_accepted: Annotated[
        bool,
        typer.Option("--provider-governance-accepted/--provider-governance-not-accepted"),
    ] = False,
    provider_governance_fingerprint: Annotated[
        str | None,
        typer.Option("--provider-governance-fingerprint"),
    ] = None,
    model_snapshot: Annotated[str, typer.Option("--model-snapshot")] = ("gpt-5-nano-2025-08-07"),
    endpoint_region: Annotated[str, typer.Option("--endpoint-region")] = "global",
    endpoint_origin_fingerprint: Annotated[
        str,
        typer.Option("--endpoint-origin-fingerprint"),
    ] = "",
    configuration_fingerprint: Annotated[
        str,
        typer.Option("--configuration-fingerprint"),
    ] = "",
    requests_per_minute: Annotated[
        int,
        typer.Option("--requests-per-minute"),
    ] = 20,
    daily_input_token_limit: Annotated[
        int,
        typer.Option("--daily-input-token-limit"),
    ] = 100_000,
    daily_output_token_limit: Annotated[
        int,
        typer.Option("--daily-output-token-limit"),
    ] = 20_000,
    concurrent_attempt_limit: Annotated[
        int,
        typer.Option("--concurrent-attempt-limit"),
    ] = 2,
    reservation_lease_seconds: Annotated[
        int,
        typer.Option("--reservation-lease-seconds"),
    ] = 60,
    audit_retention_seconds: Annotated[
        int,
        typer.Option("--audit-retention-seconds"),
    ] = 2_592_000,
    proposal_output: Annotated[
        Path | None,
        typer.Option(
            "--proposal-output",
            help="Exclusively create the exact proposal as an owner-only JSON file.",
        ),
    ] = None,
    actor: Annotated[str | None, typer.Option("--actor")] = None,
) -> None:
    """Prepare an exact proposal and perform no policy write."""

    try:
        resolved_actor = resolve_control_operator_actor(
            actor,
            required_role="platform_admin",
        )
        proposal = build_tenant_ai_policy_operator().prepare(
            workspace_id=workspace_id,
            expected_version=expected_version,
            desired=TenantAiPolicyDesired(
                external_ai_enabled=external_ai_enabled,
                provider_governance_accepted=provider_governance_accepted,
                provider_governance_fingerprint=provider_governance_fingerprint,
                model_snapshot=model_snapshot,
                endpoint_region=endpoint_region,
                endpoint_origin_fingerprint=endpoint_origin_fingerprint,
                configuration_fingerprint=configuration_fingerprint,
                requests_per_minute=requests_per_minute,
                daily_input_token_limit=daily_input_token_limit,
                daily_output_token_limit=daily_output_token_limit,
                concurrent_attempt_limit=concurrent_attempt_limit,
                reservation_lease_seconds=reservation_lease_seconds,
                audit_retention_seconds=audit_retention_seconds,
            ),
            updated_by=resolved_actor,
        )
        document = _proposal_document(proposal)
        if proposal_output is not None:
            _write_proposal(proposal_output, document)
    except _OPERATOR_ERRORS as error:
        _fail(error)
    if proposal_output is None:
        _emit(document)
        return
    _emit(
        {
            "kind": "tenant_ai_policy_proposal_file",
            "proposal_file": str(proposal_output),
            "proposal_file_written": True,
            "proposal_fingerprint": proposal.fingerprint,
            "writes_performed": False,
        }
    )


@app.command("apply")
def apply_policy(
    proposal_file: Annotated[Path, typer.Option("--proposal-file")],
    expected_proposal_fingerprint: Annotated[
        str,
        typer.Option("--expected-proposal-fingerprint"),
    ],
    confirmation: Annotated[str, typer.Option("--confirm")],
    actor: Annotated[str | None, typer.Option("--actor")] = None,
) -> None:
    """Revalidate one prepared file and apply exactly its confirmed revision."""

    try:
        resolved_actor = resolve_control_operator_actor(
            actor,
            required_role="platform_admin",
        )
        proposal = _read_proposal(proposal_file)
        if proposal.updated_by != resolved_actor:
            raise ValueError("tenant AI policy proposal actor is not current")
        applied = build_tenant_ai_policy_operator().apply(
            proposal,
            expected_proposal_fingerprint=expected_proposal_fingerprint,
            confirmation=confirmation,
        )
    except _OPERATOR_ERRORS as error:
        _fail(error)
    _emit(
        {
            "immutable_revision_recorded": True,
            "kind": "tenant_ai_policy_apply",
            "policy": _snapshot_payload(applied),
            "writes_performed": True,
        }
    )


def _proposal_document(proposal: TenantAiPolicyProposal) -> dict[str, object]:
    return {
        "kind": "tenant_ai_policy_proposal",
        "proposal": {
            "desired": {
                "audit_retention_seconds": proposal.desired.audit_retention_seconds,
                "concurrent_attempt_limit": proposal.desired.concurrent_attempt_limit,
                "configuration_fingerprint": proposal.desired.configuration_fingerprint,
                "daily_input_token_limit": proposal.desired.daily_input_token_limit,
                "daily_output_token_limit": proposal.desired.daily_output_token_limit,
                "endpoint_origin_fingerprint": (proposal.desired.endpoint_origin_fingerprint),
                "endpoint_region": proposal.desired.endpoint_region,
                "external_ai_enabled": proposal.desired.external_ai_enabled,
                "model_snapshot": proposal.desired.model_snapshot,
                "provider_governance_accepted": (proposal.desired.provider_governance_accepted),
                "provider_governance_fingerprint": (
                    proposal.desired.provider_governance_fingerprint
                ),
                "requests_per_minute": proposal.desired.requests_per_minute,
                "reservation_lease_seconds": proposal.desired.reservation_lease_seconds,
            },
            "expected_state_fingerprint": proposal.expected_state_fingerprint,
            "expected_version": proposal.expected_version,
            "fingerprint": proposal.fingerprint,
            "updated_by": proposal.updated_by,
            "workspace_id": proposal.workspace_id,
        },
        "writes_performed": False,
    }


def _read_proposal(path: Path) -> TenantAiPolicyProposal:
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_uid != os.geteuid()
        ):
            raise ValueError("tenant AI policy proposal must be a regular file")
        if not 1 <= metadata.st_size <= MAX_PROPOSAL_BYTES:
            raise ValueError("tenant AI policy proposal file size is invalid")
        raw = _read_exact(descriptor, metadata.st_size)
        after = os.fstat(descriptor)
        if (
            os.read(descriptor, 1)
            or after.st_dev != metadata.st_dev
            or after.st_ino != metadata.st_ino
            or after.st_size != metadata.st_size
            or after.st_mtime_ns != metadata.st_mtime_ns
        ):
            raise ValueError("tenant AI policy proposal changed while reading")
    except OSError as error:
        raise ValueError("tenant AI policy proposal file is unavailable") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("tenant AI policy proposal file is invalid") from error
    if (
        not isinstance(document, dict)
        or set(document) != {"kind", "proposal", "writes_performed"}
        or document["kind"] != "tenant_ai_policy_proposal"
        or document["writes_performed"] is not False
        or not isinstance(document["proposal"], dict)
    ):
        raise ValueError("tenant AI policy proposal document shape is invalid")
    proposal = document["proposal"]
    if set(proposal) != {
        "desired",
        "expected_state_fingerprint",
        "expected_version",
        "fingerprint",
        "updated_by",
        "workspace_id",
    } or not isinstance(proposal["desired"], dict):
        raise ValueError("tenant AI policy proposal payload shape is invalid")
    desired = proposal["desired"]
    if set(desired) != {
        "audit_retention_seconds",
        "concurrent_attempt_limit",
        "configuration_fingerprint",
        "daily_input_token_limit",
        "daily_output_token_limit",
        "endpoint_origin_fingerprint",
        "endpoint_region",
        "external_ai_enabled",
        "model_snapshot",
        "provider_governance_accepted",
        "provider_governance_fingerprint",
        "requests_per_minute",
        "reservation_lease_seconds",
    }:
        raise ValueError("tenant AI policy desired payload shape is invalid")
    try:
        return TenantAiPolicyProposal(
            workspace_id=_strict_str(proposal["workspace_id"]),
            expected_version=_strict_int(proposal["expected_version"]),
            expected_state_fingerprint=_strict_str(proposal["expected_state_fingerprint"]),
            desired=TenantAiPolicyDesired(
                external_ai_enabled=_strict_bool(desired["external_ai_enabled"]),
                provider_governance_accepted=_strict_bool(desired["provider_governance_accepted"]),
                provider_governance_fingerprint=(
                    None
                    if desired["provider_governance_fingerprint"] is None
                    else _strict_str(desired["provider_governance_fingerprint"])
                ),
                model_snapshot=_strict_str(desired["model_snapshot"]),
                endpoint_region=_strict_str(desired["endpoint_region"]),
                endpoint_origin_fingerprint=_strict_str(desired["endpoint_origin_fingerprint"]),
                configuration_fingerprint=_strict_str(desired["configuration_fingerprint"]),
                requests_per_minute=_strict_int(desired["requests_per_minute"]),
                daily_input_token_limit=_strict_int(desired["daily_input_token_limit"]),
                daily_output_token_limit=_strict_int(desired["daily_output_token_limit"]),
                concurrent_attempt_limit=_strict_int(desired["concurrent_attempt_limit"]),
                reservation_lease_seconds=_strict_int(desired["reservation_lease_seconds"]),
                audit_retention_seconds=_strict_int(desired["audit_retention_seconds"]),
            ),
            updated_by=_strict_str(proposal["updated_by"]),
            fingerprint=_strict_str(proposal["fingerprint"]),
        )
    except (TypeError, ValueError, KeyError) as error:
        raise ValueError("tenant AI policy proposal contains invalid values") from error


def _write_proposal(path: Path, document: dict[str, object]) -> None:
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if not 1 <= len(raw) <= MAX_PROPOSAL_BYTES:
        raise ValueError("tenant AI policy proposal file size is invalid")
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
            raise ValueError("tenant AI policy proposal output is not owner-only")
        remaining = memoryview(raw)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("tenant AI policy proposal output write failed")
            remaining = remaining[written:]
        os.fsync(descriptor)
    except OSError as error:
        raise ValueError("tenant AI policy proposal output is unavailable") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_exact(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            raise ValueError("tenant AI policy proposal changed while reading")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _snapshot_payload(snapshot: TenantAiPolicyOperatorSnapshot) -> dict[str, object]:
    return {
        "audit_retention_seconds": snapshot.audit_retention_seconds,
        "concurrent_attempt_limit": snapshot.concurrent_attempt_limit,
        "configuration_fingerprint": snapshot.configuration_fingerprint,
        "daily_input_token_limit": snapshot.daily_input_token_limit,
        "daily_output_token_limit": snapshot.daily_output_token_limit,
        "endpoint_origin_fingerprint": snapshot.endpoint_origin_fingerprint,
        "endpoint_region": snapshot.endpoint_region,
        "external_ai_enabled": snapshot.external_ai_enabled,
        "fingerprint": snapshot.fingerprint,
        "model_snapshot": snapshot.model_snapshot,
        "provider_governance_accepted": snapshot.provider_governance_accepted,
        "provider_governance_accepted_at": (
            None
            if snapshot.provider_governance_accepted_at is None
            else snapshot.provider_governance_accepted_at.isoformat()
        ),
        "provider_governance_fingerprint": snapshot.provider_governance_fingerprint,
        "requests_per_minute": snapshot.requests_per_minute,
        "reservation_lease_seconds": snapshot.reservation_lease_seconds,
        "updated_at": snapshot.updated_at.isoformat(),
        "updated_by": snapshot.updated_by,
        "version": snapshot.version,
        "workspace_id": snapshot.workspace_id,
    }


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("tenant AI policy proposal contains duplicate keys")
        result[key] = value
    return result


def _strict_str(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("tenant AI policy proposal string is invalid")
    return value


def _strict_int(value: object) -> int:
    if type(value) is not int:
        raise ValueError("tenant AI policy proposal integer is invalid")
    return value


def _strict_bool(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("tenant AI policy proposal boolean is invalid")
    return value


def _emit(payload: dict[str, object]) -> None:
    typer.echo(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _fail(error: Exception) -> Never:
    raw_code = getattr(error, "code", "tenant_ai_policy_configuration_error")
    code = raw_code.value if hasattr(raw_code, "value") else str(raw_code)
    _emit({"code": code, "ok": False})
    raise typer.Exit(code=1)


def main() -> None:
    app()


_OPERATOR_ERRORS = (
    ControlPlaneMigrationError,
    DatabaseConfigurationError,
    TenantAiPolicyError,
    TypeError,
    ValueError,
)


if __name__ == "__main__":
    main()
