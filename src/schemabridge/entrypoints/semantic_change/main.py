"""Safe, dependency-injected operator CLI for governed semantic changes."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import stat
import sys
from collections.abc import Callable, Sequence
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import IO, Any, Literal, Never, Protocol, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from schemabridge.application.semantic_change import (
    SemanticChangeError,
)
from schemabridge.application.semantic_change_operator import (
    SemanticChangeAuditVerification,
)
from schemabridge.domain.semantic_change import (
    SemanticBindingSelection,
    SemanticBindingSelectionSet,
    SemanticChangeCommit,
    SemanticChangeConfirmation,
    SemanticChangeDecisionAction,
    SemanticChangeDecisionApproval,
    SemanticChangeDecisionProposal,
    SemanticChangeReport,
    validate_semantic_change_approval,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPORT_ID = re.compile(r"^report_[0-9a-f]{64}$")
_NO_CONTROL = re.compile(r"^[^\x00-\x1f\x7f]+$")
_MAX_APPROVAL_ENVELOPE_BYTES = 16_384
_MAX_BINDING_SELECTION_FILE_BYTES = 2_097_152
_MAX_OUTPUT_BYTES = 16_384
_ModelT = TypeVar("_ModelT", bound=BaseModel)


class SemanticChangeOperatorErrorCode(StrEnum):
    INVALID_REQUEST = "semantic_change_cli_invalid_request"
    APPROVAL_MISMATCH = "semantic_change_approval_mismatch"
    AUDIT_INVALID = "semantic_change_audit_invalid"
    SERVICE_UNAVAILABLE = "semantic_change_service_unavailable"


class SemanticChangeOperatorError(RuntimeError):
    """One closed CLI failure that never contains supplied values."""

    def __init__(self, code: SemanticChangeOperatorErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class SemanticChangeInspectionPort(Protocol):
    def execute(
        self,
        *,
        binding_selections: SemanticBindingSelectionSet | None = None,
    ) -> SemanticChangeReport:
        """Inspect governed evidence and return its immutable report."""


class SemanticChangePreparationPort(Protocol):
    def execute(
        self,
        scope: SemanticRegistryScope,
        report_id: str,
        *,
        action: SemanticChangeDecisionAction,
    ) -> SemanticChangeDecisionProposal:
        """Reconstruct one exact current decision proposal."""


class SemanticChangeApprovalPort(Protocol):
    def execute(
        self,
        proposal: SemanticChangeDecisionProposal,
        *,
        actor: str,
        approved_at: datetime,
        confirmation: SemanticChangeConfirmation,
    ) -> SemanticChangeDecisionApproval:
        """Create one exact transient approval."""


class SemanticChangeCommitPort(Protocol):
    def execute(
        self,
        proposal: SemanticChangeDecisionProposal,
        approval: SemanticChangeDecisionApproval,
    ) -> SemanticChangeCommit:
        """Revalidate evidence and compare-and-swap one approved decision."""


class SemanticChangeHeadPort(Protocol):
    def execute(
        self,
        scope: SemanticRegistryScope,
    ) -> SemanticChangeCommit | None:
        """Return the current minimized evidence head."""


class SemanticChangeAuditVerificationPort(Protocol):
    def execute(
        self,
        scope: SemanticRegistryScope,
    ) -> SemanticChangeAuditVerification:
        """Verify the complete immutable audit chain for one scope."""


@dataclass(frozen=True, slots=True)
class SemanticChangeOperatorServices:
    inspect: SemanticChangeInspectionPort = field(repr=False)
    prepare: SemanticChangePreparationPort = field(repr=False)
    approve: SemanticChangeApprovalPort = field(repr=False)
    commit: SemanticChangeCommitPort = field(repr=False)
    head: SemanticChangeHeadPort = field(repr=False)
    verify_audit: SemanticChangeAuditVerificationPort = field(repr=False)


@dataclass(frozen=True, slots=True)
class SemanticChangeOperatorConfig:
    scope: SemanticRegistryScope
    trusted_actor: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not self.trusted_actor
            or self.trusted_actor.strip() != self.trusted_actor
            or len(self.trusted_actor) > 120
            or _NO_CONTROL.fullmatch(self.trusted_actor) is None
        ):
            raise ValueError("semantic change trusted actor is invalid")


@dataclass(frozen=True, slots=True)
class SemanticChangeOperatorRuntime:
    """Production operator components supplied only by the composition root."""

    services: SemanticChangeOperatorServices = field(repr=False)
    config: SemanticChangeOperatorConfig = field(repr=False)


class SemanticChangeApprovalEnvelope(BaseModel):
    """Canonical capability binding an approval to one freshly prepared proposal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    kind: Literal["semantic_change_approval"] = "semantic_change_approval"
    report_id: str = Field(pattern=r"^report_[0-9a-f]{64}$")
    report_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: SemanticChangeDecisionAction
    expected_head_revision: int = Field(ge=0)
    approval: SemanticChangeDecisionApproval
    envelope_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def envelope_fingerprint_must_match(self) -> SemanticChangeApprovalEnvelope:
        expected = _approval_envelope_fingerprint(
            self.model_dump(mode="json", exclude={"envelope_fingerprint"})
        )
        if not hmac.compare_digest(self.envelope_fingerprint, expected):
            raise ValueError("semantic change approval envelope does not match")
        return self

    @classmethod
    def create(
        cls,
        proposal: SemanticChangeDecisionProposal,
        approval: SemanticChangeDecisionApproval,
    ) -> SemanticChangeApprovalEnvelope:
        values: dict[str, object] = {
            "schema_version": 1,
            "kind": "semantic_change_approval",
            "report_id": proposal.report.id,
            "report_fingerprint": proposal.report.fingerprint,
            "proposal_fingerprint": proposal.fingerprint,
            "action": proposal.action,
            "expected_head_revision": proposal.expected_head_revision,
            "approval": approval,
        }
        fingerprint = _approval_envelope_fingerprint(
            _jsonable(values),
        )
        return cls(**values, envelope_fingerprint=fingerprint)


class SemanticBindingSelectionFile(BaseModel):
    """Canonical bounded operator input; it grants no semantic approval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    kind: Literal["semantic_binding_selections"] = "semantic_binding_selections"
    scope: SemanticRegistryScope
    selections: tuple[SemanticBindingSelection, ...] = Field(
        min_length=1,
        max_length=2_000,
    )

    @model_validator(mode="after")
    def selections_must_form_a_canonical_scoped_set(
        self,
    ) -> SemanticBindingSelectionFile:
        selection_set = SemanticBindingSelectionSet.create(
            scope=self.scope,
            selections=self.selections,
        )
        if selection_set.selections != self.selections:
            raise ValueError("semantic binding selections are not in canonical order")
        return self

    def as_selection_set(self) -> SemanticBindingSelectionSet:
        return SemanticBindingSelectionSet.create(
            scope=self.scope,
            selections=self.selections,
        )


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        del message
        raise SemanticChangeOperatorError(SemanticChangeOperatorErrorCode.INVALID_REQUEST)


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="schemabridge-semantic-change",
        description="Review and commit exact governed semantic-change decisions.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser(
        "inspect",
        help="Inspect exact governed evidence and emit a minimized report.",
    )
    inspect.add_argument(
        "--binding-selections",
        type=Path,
        help="Optional canonical bounded JSON file with exact mapping-to-field selections.",
    )

    prepare = commands.add_parser(
        "prepare",
        help="Prepare one exact decision without approving or committing it.",
    )
    _add_proposal_arguments(prepare)

    approve = commands.add_parser(
        "approve",
        help="Approve one freshly reconstructed proposal with the configured actor.",
    )
    _add_proposal_arguments(approve)
    approve.add_argument(
        "--proposal-fingerprint",
        required=True,
        help="Exact SHA-256 emitted by prepare.",
    )
    approve.add_argument(
        "--report-fingerprint",
        required=True,
        help="Exact report SHA-256 emitted by prepare.",
    )
    approve.add_argument(
        "--approved-at",
        required=True,
        help="Exact timezone-aware approval timestamp.",
    )
    approve.add_argument(
        "--confirm",
        required=True,
        choices=tuple(item.value for item in SemanticChangeConfirmation),
        help="Closed confirmation phrase required by the selected action.",
    )

    commit = commands.add_parser(
        "commit",
        help="Commit only one canonical approval envelope after fresh revalidation.",
    )
    commit.add_argument(
        "--approval-envelope",
        required=True,
        type=Path,
        help="Canonical bounded JSON envelope emitted by approve.",
    )

    commands.add_parser(
        "head",
        help="Inspect the current evidence head without mutation.",
    )
    commands.add_parser(
        "verify-audit",
        help="Verify the complete immutable semantic-change audit chain.",
    )
    return parser


def _add_proposal_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--report-id",
        required=True,
        help="Exact immutable report identifier.",
    )
    parser.add_argument(
        "--action",
        required=True,
        choices=tuple(item.value for item in SemanticChangeDecisionAction),
        help="Closed baseline, revalidation, or rejection action.",
    )


def _build_runtime() -> SemanticChangeOperatorRuntime:
    """Resolve production infrastructure lazily from the sole composition root."""

    from schemabridge import bootstrap

    candidate = getattr(bootstrap, "build_semantic_change_operator_runtime", None)
    if not callable(candidate):
        raise RuntimeError("semantic change operator composition is unavailable")
    runtime = candidate()
    if not isinstance(runtime, SemanticChangeOperatorRuntime):
        raise RuntimeError("semantic change operator composition is invalid")
    return runtime


def command(
    argv: Sequence[str] | None = None,
    *,
    services: SemanticChangeOperatorServices,
    config: SemanticChangeOperatorConfig,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Execute one injected operator action without composing infrastructure."""

    return _run_command(
        argv,
        runtime_factory=lambda: SemanticChangeOperatorRuntime(
            services=services,
            config=config,
        ),
        stdout=stdout,
        stderr=stderr,
    )


def _run_command(
    argv: Sequence[str] | None = None,
    *,
    runtime_factory: Callable[[], SemanticChangeOperatorRuntime],
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Parse first, then resolve the runtime only for an executable command."""

    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    try:
        with redirect_stdout(output):
            try:
                arguments = _parser().parse_args(argv)
            except SystemExit as exit_error:
                return exit_error.code if isinstance(exit_error.code, int) else 1
        runtime = runtime_factory()
        if not isinstance(runtime, SemanticChangeOperatorRuntime):
            raise RuntimeError("semantic change operator composition is invalid")
        payload = _dispatch(
            arguments,
            services=runtime.services,
            config=runtime.config,
        )
        _write_json(output, payload)
        return 0
    except SemanticChangeOperatorError as error:
        _write_error(errors, error.code.value)
    except SemanticChangeError as error:
        _write_error(errors, error.code.value)
    except (OSError, TypeError, ValueError, ValidationError):
        _write_error(errors, SemanticChangeOperatorErrorCode.INVALID_REQUEST.value)
    except Exception:
        _write_error(
            errors,
            SemanticChangeOperatorErrorCode.SERVICE_UNAVAILABLE.value,
        )
    return 1


def _dispatch(
    arguments: argparse.Namespace,
    *,
    services: SemanticChangeOperatorServices,
    config: SemanticChangeOperatorConfig,
) -> BaseModel | dict[str, object]:
    selected = arguments.command
    if selected == "inspect":
        binding_selections = (
            None
            if arguments.binding_selections is None
            else _read_binding_selections(arguments.binding_selections, config.scope)
        )
        report = _validated_model(
            services.inspect.execute(binding_selections=binding_selections),
            SemanticChangeReport,
        )
        if report.context.scope != config.scope:
            raise _service_unavailable()
        return _report_payload(report, binding_selections=binding_selections)
    if selected == "prepare":
        proposal = _prepare_from_arguments(arguments, services, config)
        return _proposal_payload(proposal)
    if selected == "approve":
        proposal = _prepare_from_arguments(arguments, services, config)
        _require_fingerprint(proposal.fingerprint, arguments.proposal_fingerprint)
        _require_fingerprint(
            proposal.report.fingerprint,
            arguments.report_fingerprint,
        )
        approved_at = _parse_aware_time(arguments.approved_at)
        confirmation = SemanticChangeConfirmation(arguments.confirm)
        approval = _validated_model(
            services.approve.execute(
                proposal,
                actor=config.trusted_actor,
                approved_at=approved_at,
                confirmation=confirmation,
            ),
            SemanticChangeDecisionApproval,
        )
        _validate_exact_approval(
            proposal,
            approval,
            trusted_actor=config.trusted_actor,
            approved_at=approved_at,
            confirmation=confirmation,
        )
        return SemanticChangeApprovalEnvelope.create(proposal, approval)
    if selected == "commit":
        envelope = _read_approval_envelope(arguments.approval_envelope)
        if not hmac.compare_digest(envelope.approval.actor, config.trusted_actor):
            raise _approval_mismatch()
        proposal = _prepare(
            services,
            config,
            report_id=envelope.report_id,
            action=envelope.action,
        )
        _require_fingerprint(proposal.fingerprint, envelope.proposal_fingerprint)
        _require_fingerprint(
            proposal.report.fingerprint,
            envelope.report_fingerprint,
        )
        if proposal.expected_head_revision != envelope.expected_head_revision:
            raise _approval_mismatch()
        _validate_exact_approval(
            proposal,
            envelope.approval,
            trusted_actor=config.trusted_actor,
            approved_at=envelope.approval.approved_at,
            confirmation=envelope.approval.confirmation,
        )
        committed = _validated_model(
            services.commit.execute(proposal, envelope.approval),
            SemanticChangeCommit,
        )
        if (
            committed.scope != config.scope
            or committed.decision.proposal_fingerprint != proposal.fingerprint
            or committed.decision.report_id != proposal.report.id
        ):
            raise _service_unavailable()
        return _commit_payload(committed, operation="commit")
    if selected == "head":
        current = services.head.execute(config.scope)
        if current is None:
            return {
                "has_head": False,
                "head_revision": 0,
                "ok": True,
                "writes_performed": False,
            }
        commit = _validated_model(current, SemanticChangeCommit)
        if commit.scope != config.scope:
            raise _service_unavailable()
        return _commit_payload(commit, operation="head")
    if selected == "verify-audit":
        verification = services.verify_audit.execute(config.scope)
        _validate_audit_verification(verification)
        if not verification.valid:
            raise SemanticChangeOperatorError(SemanticChangeOperatorErrorCode.AUDIT_INVALID)
        return {
            "audit_valid": True,
            "chain_head_hash": verification.chain_head_hash,
            "event_count": verification.event_count,
            "head_revision": verification.head_revision,
            "ok": True,
            "writes_performed": False,
        }
    raise SemanticChangeOperatorError(SemanticChangeOperatorErrorCode.INVALID_REQUEST)


def _prepare_from_arguments(
    arguments: argparse.Namespace,
    services: SemanticChangeOperatorServices,
    config: SemanticChangeOperatorConfig,
) -> SemanticChangeDecisionProposal:
    report_id = _valid_report_id(arguments.report_id)
    action = SemanticChangeDecisionAction(arguments.action)
    return _prepare(
        services,
        config,
        report_id=report_id,
        action=action,
    )


def _prepare(
    services: SemanticChangeOperatorServices,
    config: SemanticChangeOperatorConfig,
    *,
    report_id: str,
    action: SemanticChangeDecisionAction,
) -> SemanticChangeDecisionProposal:
    proposal = _validated_model(
        services.prepare.execute(config.scope, report_id, action=action),
        SemanticChangeDecisionProposal,
    )
    if (
        proposal.report.context.scope != config.scope
        or proposal.report.id != report_id
        or proposal.action is not action
    ):
        raise _service_unavailable()
    return proposal


def _proposal_payload(
    proposal: SemanticChangeDecisionProposal,
) -> dict[str, object]:
    return {
        "action": proposal.action.value,
        "confirmation_required": _required_confirmation(proposal.action).value,
        "expected_head_revision": proposal.expected_head_revision,
        "ok": True,
        "proposal_fingerprint": proposal.fingerprint,
        "report_fingerprint": proposal.report.fingerprint,
        "report_id": proposal.report.id,
        "writes_performed": False,
    }


def _report_payload(
    report: SemanticChangeReport,
    *,
    binding_selections: SemanticBindingSelectionSet | None,
) -> dict[str, object]:
    return {
        "baseline_revision": report.baseline_revision,
        "binding_selection_count": (
            0 if binding_selections is None else len(binding_selections.selections)
        ),
        "binding_selection_fingerprint": (
            None if binding_selections is None else binding_selections.fingerprint
        ),
        "catalog_generation_fingerprint": report.catalog_generations.fingerprint,
        "finding_count": len(report.findings),
        "impact_set_fingerprint": report.impacts.impact_set_fingerprint,
        "impacts_complete": report.impacts.complete,
        "inspected_at": report.inspected_at.isoformat(),
        "ok": True,
        "report_fingerprint": report.fingerprint,
        "report_id": report.id,
        "status": report.status.value,
        "writes_performed": True,
    }


def _commit_payload(
    commit: SemanticChangeCommit,
    *,
    operation: Literal["commit", "head"],
) -> dict[str, object]:
    baseline = commit.baseline
    return {
        "action": commit.decision.action.value,
        "audit_event_hash": commit.audit_event_hash,
        "baseline_fingerprint": None if baseline is None else baseline.fingerprint,
        "baseline_revision": None if baseline is None else baseline.revision,
        "decision_fingerprint": commit.decision.fingerprint,
        "decision_id": commit.decision.id,
        "head_revision": commit.head_revision,
        "ok": True,
        "operation": operation,
        "replayed": commit.replayed,
        "report_fingerprint": commit.decision.report_fingerprint,
        "report_id": commit.decision.report_id,
        "resulting_status": commit.decision.resulting_status.value,
        "writes_performed": operation == "commit" and not commit.replayed,
    }


def _required_confirmation(
    action: SemanticChangeDecisionAction,
) -> SemanticChangeConfirmation:
    return {
        SemanticChangeDecisionAction.ESTABLISH_BASELINE: (SemanticChangeConfirmation.ESTABLISH),
        SemanticChangeDecisionAction.REVALIDATE_COMPATIBLE_CHANGE: (
            SemanticChangeConfirmation.REVALIDATE
        ),
        SemanticChangeDecisionAction.REJECT_CHANGE: (SemanticChangeConfirmation.REJECT),
    }[action]


def _validate_exact_approval(
    proposal: SemanticChangeDecisionProposal,
    approval: SemanticChangeDecisionApproval,
    *,
    trusted_actor: str,
    approved_at: datetime,
    confirmation: SemanticChangeConfirmation,
) -> None:
    try:
        validate_semantic_change_approval(proposal, approval)
    except (TypeError, ValueError) as error:
        raise _approval_mismatch() from error
    if (
        not hmac.compare_digest(approval.actor, trusted_actor)
        or approval.approved_at != approved_at
        or approval.confirmation is not confirmation
        or approval.confirmation is not _required_confirmation(proposal.action)
    ):
        raise _approval_mismatch()


def _read_approval_envelope(path: Path) -> SemanticChangeApprovalEnvelope:
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("approval envelope path is invalid")
        if not 1 <= metadata.st_size <= _MAX_APPROVAL_ENVELOPE_BYTES:
            raise ValueError("approval envelope size is invalid")
        raw = os.read(descriptor, _MAX_APPROVAL_ENVELOPE_BYTES + 1)
        if len(raw) != metadata.st_size or os.read(descriptor, 1):
            raise ValueError("approval envelope changed while reading")
        decoded = raw.decode("utf-8")
        payload = json.loads(decoded, object_pairs_hook=_unique_object)
        if not isinstance(payload, dict):
            raise ValueError("approval envelope root is invalid")
        envelope = SemanticChangeApprovalEnvelope.model_validate(payload)
        if raw != _canonical_json_bytes(envelope.model_dump(mode="json")):
            raise ValueError("approval envelope is not canonical")
        return envelope
    except SemanticChangeOperatorError:
        raise
    except (OSError, UnicodeError, ValueError, ValidationError) as error:
        raise _approval_mismatch() from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_binding_selections(
    path: Path,
    scope: SemanticRegistryScope,
) -> SemanticBindingSelectionSet:
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("binding selection path is invalid")
        if not 1 <= metadata.st_size <= _MAX_BINDING_SELECTION_FILE_BYTES:
            raise ValueError("binding selection size is invalid")
        raw = os.read(descriptor, _MAX_BINDING_SELECTION_FILE_BYTES + 1)
        if len(raw) != metadata.st_size or os.read(descriptor, 1):
            raise ValueError("binding selection file changed while reading")
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(payload, dict):
            raise ValueError("binding selection root is invalid")
        selection_file = SemanticBindingSelectionFile.model_validate(payload)
        if raw != _canonical_json_bytes(selection_file.model_dump(mode="json")):
            raise ValueError("binding selection file is not canonical")
        selections = selection_file.as_selection_set()
        if selections.scope != scope:
            raise ValueError("binding selection file crosses scope")
        return selections
    except (OSError, UnicodeError, ValueError, ValidationError) as error:
        raise SemanticChangeOperatorError(
            SemanticChangeOperatorErrorCode.INVALID_REQUEST
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_audit_verification(
    value: SemanticChangeAuditVerification,
) -> None:
    if (
        not isinstance(value, SemanticChangeAuditVerification)
        or isinstance(value.event_count, bool)
        or value.event_count < 0
        or isinstance(value.head_revision, bool)
        or value.head_revision < 0
        or (value.event_count == 0) != (value.chain_head_hash is None)
        or (value.chain_head_hash is not None and _SHA256.fullmatch(value.chain_head_hash) is None)
        # The workspace audit chain is shared with registry-control operations.
        # A semantic head revision therefore cannot exceed the verified chain
        # length, but unrelated immutable audit events may make it larger.
        or (value.valid and value.event_count < value.head_revision)
    ):
        raise _service_unavailable()


def _validated_model(value: object, model: type[_ModelT]) -> _ModelT:
    if not isinstance(value, model):
        raise _service_unavailable()
    try:
        return model.model_validate(value.model_dump(mode="json"))
    except (TypeError, ValueError, ValidationError) as error:
        raise _service_unavailable() from error


def _parse_aware_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise SemanticChangeOperatorError(
            SemanticChangeOperatorErrorCode.INVALID_REQUEST
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SemanticChangeOperatorError(SemanticChangeOperatorErrorCode.INVALID_REQUEST)
    return parsed


def _valid_report_id(value: str) -> str:
    if not isinstance(value, str) or _REPORT_ID.fullmatch(value) is None:
        raise SemanticChangeOperatorError(SemanticChangeOperatorErrorCode.INVALID_REQUEST)
    return value


def _require_fingerprint(actual: str, supplied: str) -> None:
    if (
        not isinstance(supplied, str)
        or _SHA256.fullmatch(supplied) is None
        or not hmac.compare_digest(actual, supplied)
    ):
        raise _approval_mismatch()


def _approval_envelope_fingerprint(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    ).hexdigest()


def _jsonable(values: dict[str, object]) -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(
            json.dumps(
                values,
                default=lambda value: (
                    value.model_dump(mode="json")
                    if isinstance(value, BaseModel)
                    else value.value
                    if isinstance(value, StrEnum)
                    else str(value)
                ),
                ensure_ascii=True,
            )
        ),
    )


def _canonical_json_bytes(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate approval envelope key")
        result[key] = value
    return result


def _write_json(stream: IO[str], payload: BaseModel | dict[str, object]) -> None:
    serializable = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    encoded = _canonical_json_bytes(serializable).decode("ascii")
    if len(encoded.encode("ascii")) > _MAX_OUTPUT_BYTES:
        raise _service_unavailable()
    stream.write(encoded)


def _write_error(stream: IO[str], code: str) -> None:
    _write_json(stream, {"code": code, "ok": False})


def _approval_mismatch() -> SemanticChangeOperatorError:
    return SemanticChangeOperatorError(SemanticChangeOperatorErrorCode.APPROVAL_MISMATCH)


def _service_unavailable() -> SemanticChangeOperatorError:
    return SemanticChangeOperatorError(SemanticChangeOperatorErrorCode.SERVICE_UNAVAILABLE)


def main() -> None:
    """Compose and execute the operator without disclosing startup failures."""

    status = _run_command(runtime_factory=_build_runtime)
    raise SystemExit(status)


__all__ = [
    "SemanticBindingSelectionFile",
    "SemanticChangeApprovalEnvelope",
    "SemanticChangeAuditVerification",
    "SemanticChangeOperatorConfig",
    "SemanticChangeOperatorError",
    "SemanticChangeOperatorErrorCode",
    "SemanticChangeOperatorRuntime",
    "SemanticChangeOperatorServices",
    "command",
    "main",
]


if __name__ == "__main__":
    main()
