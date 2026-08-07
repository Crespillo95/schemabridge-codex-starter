"""Dry-run-first orchestration for importing legacy SQLite control state."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from schemabridge.application.ports.legacy_import import (
    LegacyControlPlaneImportStorePort,
    LegacyControlPlaneSourcePort,
    LegacyImportPortError,
    LegacyImportPortErrorCode,
    LegacySqliteRow,
    LegacySqliteSnapshot,
)
from schemabridge.domain.decisions import DecisionRecord
from schemabridge.domain.identity import WorkflowAccessGrant
from schemabridge.domain.join_reviews import JoinPublicationResult, JoinReviewDraft
from schemabridge.domain.legacy_import import (
    LegacyImportApproval,
    LegacyImportConfirmation,
    LegacyImportDisposition,
    LegacyImportItem,
    LegacyImportPlan,
    LegacyImportReservation,
    LegacyImportResourceKind,
    LegacyImportResult,
    LegacyImportStatus,
    build_legacy_import_approval,
    build_legacy_import_plan,
    canonical_json,
    fingerprint_legacy_value,
    validate_legacy_import_approval,
)
from schemabridge.domain.publication_audit import (
    PublicationFamily,
    PublicationTargetAuditRecord,
)
from schemabridge.domain.recipes import QueryRecipe
from schemabridge.domain.requests import AnalyticalRequestDraft
from schemabridge.domain.reviews import CanonicalReviewDraft, PublicationResult
from schemabridge.domain.workflows import AgentWorkflowDraft

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_ROWS = 100_000
_MAX_LEGACY_TEXT_BYTES = 2_000_000
_ModelT = TypeVar("_ModelT", bound=BaseModel)

_EXPECTED_COLUMNS: dict[str, tuple[str, ...]] = {
    "agent_workflow_drafts": ("id", "revision", "payload"),
    "workflow_access_grants": (
        "workflow_id",
        "workspace_id",
        "owner_actor_id",
        "created_at",
    ),
    "analytical_request_drafts": ("id", "revision", "payload"),
    "review_drafts": ("id", "revision", "payload"),
    "review_decisions": (
        "id",
        "draft_id",
        "resulting_version",
        "payload",
    ),
    "review_publications": (
        "sequence",
        "draft_id",
        "approval_id",
        "fingerprint",
        "payload",
    ),
    "join_review_drafts": ("id", "revision", "payload"),
    "join_review_decisions": (
        "id",
        "draft_id",
        "resulting_version",
        "payload",
    ),
    "join_publications": ("sequence", "draft_id", "payload"),
    "publication_approval_identity": (
        "approval_id",
        "family",
        "actor",
        "approved_at",
        "new_fingerprint",
    ),
    "publication_target_audit": (
        "sequence",
        "approval_id",
        "family",
        "operation",
        "target",
        "record_json",
    ),
    "fake_query_recipes": (
        "intent_fingerprint",
        "version",
        "fingerprint",
        "payload",
        "is_current",
        "current_document_urn",
        "versioned_document_urn",
        "approval_id",
        "published_at",
    ),
    "fake_workflow_publications": (
        "idempotency_key",
        "document_ref",
        "published_at",
    ),
}

_KINDS: dict[str, LegacyImportResourceKind] = {
    "agent_workflow_drafts": LegacyImportResourceKind.WORKFLOW_DRAFT,
    "workflow_access_grants": LegacyImportResourceKind.WORKFLOW_ACCESS_GRANT,
    "analytical_request_drafts": LegacyImportResourceKind.REQUEST_DRAFT,
    "review_drafts": LegacyImportResourceKind.REVIEW_DRAFT,
    "review_decisions": LegacyImportResourceKind.REVIEW_DECISION,
    "review_publications": LegacyImportResourceKind.REVIEW_PUBLICATION,
    "join_review_drafts": LegacyImportResourceKind.JOIN_REVIEW_DRAFT,
    "join_review_decisions": LegacyImportResourceKind.JOIN_REVIEW_DECISION,
    "join_publications": LegacyImportResourceKind.JOIN_PUBLICATION,
    "publication_approval_identity": (LegacyImportResourceKind.PUBLICATION_APPROVAL_IDENTITY),
    "publication_target_audit": LegacyImportResourceKind.PUBLICATION_TARGET_AUDIT,
    "fake_query_recipes": LegacyImportResourceKind.QUERY_RECIPE,
    "fake_workflow_publications": LegacyImportResourceKind.FAKE_WORKFLOW_PUBLICATION,
}


class LegacyImportErrorCode(StrEnum):
    SOURCE_UNAVAILABLE = "legacy_import_source_unavailable"
    SOURCE_NOT_OFFLINE = "legacy_import_source_not_offline"
    SOURCE_TOO_LARGE = "legacy_import_source_too_large"
    SOURCE_CORRUPT = "legacy_import_source_corrupt"
    SOURCE_SCHEMA_INVALID = "legacy_import_source_schema_invalid"
    SOURCE_CHANGED = "legacy_import_source_changed"
    APPROVAL_MISMATCH = "legacy_import_approval_mismatch"
    DRY_RUN_REQUIRED = "legacy_import_dry_run_required"
    STORE_CONFLICT = "legacy_import_store_conflict"
    STORE_UNAVAILABLE = "legacy_import_store_unavailable"
    INVALID_RESPONSE = "legacy_import_invalid_response"


class LegacyImportError(RuntimeError):
    """Sanitized application failure suitable for an operator entrypoint."""

    def __init__(self, code: LegacyImportErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class InspectLegacyControlPlaneImport:
    """Inspect one complete offline source without reserving or importing state."""

    source: LegacyControlPlaneSourcePort

    def execute(self) -> LegacyImportPlan:
        return _inspect_plan(self.source)


@dataclass(frozen=True, slots=True)
class PrepareLegacyControlPlaneImport:
    """Inspect completely, then reserve checksum/count metadata only."""

    source: LegacyControlPlaneSourcePort
    store: LegacyControlPlaneImportStorePort

    def execute(self, *, recorded_at: datetime) -> LegacyImportPlan:
        _require_aware(recorded_at, "legacy import dry-run time")
        plan = InspectLegacyControlPlaneImport(self.source).execute()
        try:
            returned = self.store.reserve_dry_run(plan, recorded_at=recorded_at)
        except LegacyImportPortError as error:
            raise _store_error(error) from error
        reservation = _validated_reservation(returned)
        _require_reservation_matches(plan, reservation)
        return plan


@dataclass(frozen=True, slots=True)
class ApproveLegacyControlPlaneImport:
    """Create one exact approval without performing target writes."""

    def execute(
        self,
        plan: LegacyImportPlan,
        *,
        actor: str,
        approved_at: datetime,
        confirmation: LegacyImportConfirmation,
    ) -> LegacyImportApproval:
        try:
            return build_legacy_import_approval(
                plan,
                actor=actor,
                approved_at=approved_at,
                confirmation=confirmation,
            )
        except ValueError as error:
            raise LegacyImportError(
                LegacyImportErrorCode.APPROVAL_MISMATCH,
                "legacy import approval does not match the exact dry-run",
            ) from error


@dataclass(frozen=True, slots=True)
class ApplyLegacyControlPlaneImport:
    """Reinspect the source, then apply one previously reserved exact plan."""

    source: LegacyControlPlaneSourcePort
    store: LegacyControlPlaneImportStorePort

    def execute(
        self,
        plan: LegacyImportPlan,
        approval: LegacyImportApproval,
        *,
        completed_at: datetime,
    ) -> LegacyImportResult:
        _require_aware(completed_at, "legacy import completion time")
        if completed_at < approval.approved_at:
            raise LegacyImportError(
                LegacyImportErrorCode.APPROVAL_MISMATCH,
                "legacy import completion cannot predate its approval",
            )
        try:
            validate_legacy_import_approval(plan, approval)
        except ValueError as error:
            raise LegacyImportError(
                LegacyImportErrorCode.APPROVAL_MISMATCH,
                "legacy import approval does not match the exact dry-run",
            ) from error

        current = InspectLegacyControlPlaneImport(self.source).execute()
        if current != plan:
            raise LegacyImportError(
                LegacyImportErrorCode.SOURCE_CHANGED,
                "legacy SQLite state changed after the approved dry-run",
            )
        try:
            reserved = self.store.load_reservation(plan.id)
        except LegacyImportPortError as error:
            raise _store_error(error) from error
        if reserved is None:
            raise LegacyImportError(
                LegacyImportErrorCode.DRY_RUN_REQUIRED,
                "legacy import requires a durably recorded dry-run",
            )
        reservation = _validated_reservation(reserved)
        _require_reservation_matches(plan, reservation)
        try:
            returned = self.store.apply(
                plan,
                approval,
                completed_at=completed_at,
            )
        except LegacyImportPortError as error:
            raise _store_error(error) from error
        result = _validated_result(returned)
        _require_reservation_matches(plan, result.reservation)
        if (
            result.reservation.status is not LegacyImportStatus.COMPLETED
            or result.reservation.approval_id != approval.id
        ):
            raise LegacyImportError(
                LegacyImportErrorCode.INVALID_RESPONSE,
                "legacy import store returned another completion",
            )
        return result


@dataclass(frozen=True, slots=True)
class _Assessment:
    table: str
    source_id_digest: str
    kind: LegacyImportResourceKind
    payload_fingerprint: str
    valid: bool
    target_id: str | None = None
    reference_id: str | None = None
    workspace_id: str | None = None
    target_payload_json: str | None = None
    preview_rows: int = 0
    association_fingerprint: str | None = None


def plan_legacy_control_plane_import(snapshot: LegacySqliteSnapshot) -> LegacyImportPlan:
    """Validate typed rows and derive deterministic import/quarantine decisions."""

    _validate_snapshot(snapshot)
    assessments = tuple(_assess(table.name, row) for table in snapshot.tables for row in table.rows)
    workflows = {
        item.target_id: item
        for item in assessments
        if item.kind is LegacyImportResourceKind.WORKFLOW_DRAFT
        and item.valid
        and item.target_id is not None
    }
    grants = {
        item.target_id: item
        for item in assessments
        if item.kind is LegacyImportResourceKind.WORKFLOW_ACCESS_GRANT
        and item.valid
        and item.target_id is not None
    }
    valid_parent_ids: dict[LegacyImportResourceKind, set[str]] = {
        LegacyImportResourceKind.REVIEW_DRAFT: {
            item.target_id
            for item in assessments
            if item.kind is LegacyImportResourceKind.REVIEW_DRAFT
            and item.valid
            and item.target_id is not None
        },
        LegacyImportResourceKind.JOIN_REVIEW_DRAFT: {
            item.target_id
            for item in assessments
            if item.kind is LegacyImportResourceKind.JOIN_REVIEW_DRAFT
            and item.valid
            and item.target_id is not None
        },
        LegacyImportResourceKind.PUBLICATION_APPROVAL_IDENTITY: {
            item.target_id
            for item in assessments
            if item.kind is LegacyImportResourceKind.PUBLICATION_APPROVAL_IDENTITY
            and item.valid
            and item.target_id is not None
        },
    }
    approval_identities = {
        item.target_id: item.association_fingerprint
        for item in assessments
        if item.kind is LegacyImportResourceKind.PUBLICATION_APPROVAL_IDENTITY
        and item.valid
        and item.target_id is not None
    }

    items: list[LegacyImportItem] = []
    preview_rows_stripped = 0
    for assessment in assessments:
        disposition = LegacyImportDisposition.QUARANTINE
        reason = "ownership_not_provable"
        workspace_id: str | None = None
        target_id: str | None = None
        target_payload: str | None = None

        if not assessment.valid:
            reason = "invalid_payload"
        elif assessment.kind is LegacyImportResourceKind.WORKFLOW_DRAFT:
            grant = None if assessment.target_id is None else grants.get(assessment.target_id)
            if grant is None:
                reason = "orphan_workflow"
            else:
                disposition = LegacyImportDisposition.IMPORT
                reason = ""
                workspace_id = grant.workspace_id
                target_id = assessment.target_id
                target_payload = assessment.target_payload_json
                preview_rows_stripped += assessment.preview_rows
        elif assessment.kind is LegacyImportResourceKind.WORKFLOW_ACCESS_GRANT:
            if assessment.target_id not in workflows:
                reason = "orphan_access_grant"
            else:
                disposition = LegacyImportDisposition.IMPORT
                reason = ""
                workspace_id = assessment.workspace_id
                target_id = assessment.target_id
                target_payload = assessment.target_payload_json
        elif (
            assessment.kind is LegacyImportResourceKind.PUBLICATION_TARGET_AUDIT
            and assessment.reference_id in approval_identities
            and assessment.association_fingerprint != approval_identities[assessment.reference_id]
        ):
            reason = "invalid_payload"
        elif _has_missing_parent(assessment, valid_parent_ids):
            reason = "orphan_reference"
        elif assessment.kind is LegacyImportResourceKind.QUERY_RECIPE:
            reason = "requires_governed_recipe_migration"
        elif assessment.kind is LegacyImportResourceKind.FAKE_WORKFLOW_PUBLICATION:
            disposition = LegacyImportDisposition.SKIP
            reason = "local_only_resource"

        items.append(
            LegacyImportItem(
                source_table=assessment.table,
                source_id_digest=assessment.source_id_digest,
                resource_kind=assessment.kind,
                disposition=disposition,
                payload_fingerprint=assessment.payload_fingerprint,
                workspace_id=workspace_id,
                target_id=target_id,
                reason_code=reason or None,
                target_payload_json=target_payload,
            )
        )

    return build_legacy_import_plan(
        snapshot.source_fingerprint,
        snapshot.source_schema_fingerprint,
        tuple(items),
        preview_rows_stripped=preview_rows_stripped,
    )


def _assess(table: str, row: LegacySqliteRow) -> _Assessment:
    values = row.values
    kind = _KINDS[table]
    payload_fingerprint = _row_fingerprint(values)
    try:
        if table == "agent_workflow_drafts":
            identifier = _text(values, 0)
            revision = _integer(values, 1)
            draft = _model(values, 2, AgentWorkflowDraft)
            if draft.id != identifier or draft.revision != revision:
                raise ValueError
            payload = draft.model_dump(mode="json")
            execution = draft.execution
            preview_rows = 0
            row_count: int | None = None
            preview_fingerprint: str | None = None
            if execution is not None:
                preview_rows = len(execution.rows)
                row_count = execution.observed_row_count
                preview_fingerprint = execution.preview_fingerprint
                execution_payload = payload.get("execution")
                if not isinstance(execution_payload, dict):
                    raise ValueError
                execution_payload["rows"] = []
                execution_payload["row_count"] = row_count
                AgentWorkflowDraft.model_validate(payload)
            return _Assessment(
                table,
                row.source_id_digest,
                kind,
                payload_fingerprint,
                True,
                target_id=identifier,
                target_payload_json=canonical_json(
                    {
                        "id": identifier,
                        "revision": revision,
                        "payload": payload,
                        "execution_row_count": row_count,
                        "execution_preview_fingerprint": preview_fingerprint,
                        "updated_at": draft.updated_at.isoformat(),
                    }
                ),
                preview_rows=preview_rows,
            )
        if table == "workflow_access_grants":
            grant = WorkflowAccessGrant.model_validate(
                {
                    "workflow_id": _text(values, 0),
                    "workspace_id": _text(values, 1),
                    "owner_actor_id": _text(values, 2),
                    "created_at": _text(values, 3),
                }
            )
            if len(grant.workspace_id) > 200 or len(grant.owner_actor_id) > 200:
                raise ValueError
            return _Assessment(
                table,
                row.source_id_digest,
                kind,
                payload_fingerprint,
                True,
                target_id=grant.workflow_id,
                workspace_id=grant.workspace_id,
                target_payload_json=canonical_json(grant.model_dump(mode="json")),
            )
        if table == "analytical_request_drafts":
            identifier = _text(values, 0)
            revision = _integer(values, 1)
            request_draft = _model(values, 2, AnalyticalRequestDraft)
            _require_identity_revision(
                request_draft.id,
                request_draft.revision,
                identifier,
                revision,
            )
            return _valid_ambiguous(table, row, kind, payload_fingerprint, identifier)
        if table == "review_drafts":
            identifier = _text(values, 0)
            revision = _integer(values, 1)
            review_draft = _model(values, 2, CanonicalReviewDraft)
            _require_identity_revision(
                review_draft.id,
                review_draft.revision,
                identifier,
                revision,
            )
            return _valid_ambiguous(table, row, kind, payload_fingerprint, identifier)
        if table == "review_decisions":
            identifier = _text(values, 0)
            draft_id = _text(values, 1)
            resulting_version = _integer(values, 2)
            decision = _model(values, 3, DecisionRecord)
            if decision.id != identifier or decision.resulting_version != resulting_version:
                raise ValueError
            return _valid_ambiguous(
                table,
                row,
                kind,
                payload_fingerprint,
                identifier,
                reference_id=draft_id,
            )
        if table == "review_publications":
            _integer(values, 0)
            draft_id = _text(values, 1)
            approval_id = _text(values, 2)
            fingerprint = _sha256(values, 3)
            publication = _model(values, 4, PublicationResult)
            if (
                publication.draft_id != draft_id
                or publication.approval_id != approval_id
                or publication.fingerprint != fingerprint
            ):
                raise ValueError
            return _valid_ambiguous(
                table,
                row,
                kind,
                payload_fingerprint,
                approval_id,
                reference_id=draft_id,
            )
        if table == "join_review_drafts":
            identifier = _text(values, 0)
            revision = _integer(values, 1)
            join_draft = _model(values, 2, JoinReviewDraft)
            _require_identity_revision(
                join_draft.id,
                join_draft.revision,
                identifier,
                revision,
            )
            return _valid_ambiguous(table, row, kind, payload_fingerprint, identifier)
        if table == "join_review_decisions":
            identifier = _text(values, 0)
            draft_id = _text(values, 1)
            resulting_version = _integer(values, 2)
            decision = _model(values, 3, DecisionRecord)
            if decision.id != identifier or decision.resulting_version != resulting_version:
                raise ValueError
            return _valid_ambiguous(
                table,
                row,
                kind,
                payload_fingerprint,
                identifier,
                reference_id=draft_id,
            )
        if table == "join_publications":
            sequence = _integer(values, 0)
            draft_id = _text(values, 1)
            join_publication = _model(values, 2, JoinPublicationResult)
            if join_publication.draft_id != draft_id:
                raise ValueError
            return _valid_ambiguous(
                table,
                row,
                kind,
                payload_fingerprint,
                str(sequence),
                reference_id=draft_id,
            )
        if table == "publication_approval_identity":
            approval_id = _text(values, 0)
            family = PublicationFamily(_text(values, 1))
            actor = _bounded_text(values, 2, 120)
            approved_at = _aware_text(values, 3)
            new_fingerprint = _sha256(values, 4)
            assessed = _valid_ambiguous(
                table,
                row,
                kind,
                payload_fingerprint,
                approval_id,
            )
            return _with_association(
                assessed,
                family=family,
                actor=actor,
                approved_at=approved_at,
                new_fingerprint=new_fingerprint,
            )
        if table == "publication_target_audit":
            sequence = _integer(values, 0)
            approval_id = _text(values, 1)
            family = PublicationFamily(_text(values, 2))
            operation = _text(values, 3)
            target = _text(values, 4)
            record = _model(values, 5, PublicationTargetAuditRecord)
            if (
                record.approval_id != approval_id
                or record.family is not family
                or record.operation != operation
                or record.target != target
            ):
                raise ValueError
            assessed = _valid_ambiguous(
                table,
                row,
                kind,
                payload_fingerprint,
                str(sequence),
                reference_id=approval_id,
            )
            return _with_association(
                assessed,
                family=record.family,
                actor=record.actor,
                approved_at=record.approved_at,
                new_fingerprint=record.new_fingerprint,
            )
        if table == "fake_query_recipes":
            intent_fingerprint = _sha256(values, 0)
            version = _integer(values, 1)
            fingerprint = _sha256(values, 2)
            recipe = _model(values, 3, QueryRecipe)
            if (
                recipe.intent_fingerprint != intent_fingerprint
                or recipe.version != version
                or recipe.fingerprint != fingerprint
            ):
                raise ValueError
            current = _integer(values, 4)
            if current not in {0, 1}:
                raise ValueError
            _bounded_text(values, 5, 500)
            _bounded_text(values, 6, 500)
            _bounded_text(values, 7, 200)
            _aware_text(values, 8)
            return _valid_ambiguous(
                table,
                row,
                kind,
                payload_fingerprint,
                f"{intent_fingerprint}:{version}",
            )
        if table == "fake_workflow_publications":
            identifier = _sha256(values, 0)
            _bounded_text(values, 1, 500)
            _aware_text(values, 2)
            return _valid_ambiguous(
                table,
                row,
                kind,
                payload_fingerprint,
                identifier,
            )
    except (IndexError, TypeError, ValueError, ValidationError):
        pass
    return _Assessment(
        table,
        row.source_id_digest,
        kind,
        payload_fingerprint,
        False,
    )


def _valid_ambiguous(
    table: str,
    row: LegacySqliteRow,
    kind: LegacyImportResourceKind,
    payload_fingerprint: str,
    target_id: str,
    *,
    reference_id: str | None = None,
) -> _Assessment:
    return _Assessment(
        table,
        row.source_id_digest,
        kind,
        payload_fingerprint,
        True,
        target_id=target_id,
        reference_id=reference_id,
    )


def _has_missing_parent(
    assessment: _Assessment,
    valid_parent_ids: dict[LegacyImportResourceKind, set[str]],
) -> bool:
    parent_kind = {
        LegacyImportResourceKind.REVIEW_DECISION: LegacyImportResourceKind.REVIEW_DRAFT,
        LegacyImportResourceKind.REVIEW_PUBLICATION: LegacyImportResourceKind.REVIEW_DRAFT,
        LegacyImportResourceKind.JOIN_REVIEW_DECISION: (LegacyImportResourceKind.JOIN_REVIEW_DRAFT),
        LegacyImportResourceKind.JOIN_PUBLICATION: (LegacyImportResourceKind.JOIN_REVIEW_DRAFT),
        LegacyImportResourceKind.PUBLICATION_TARGET_AUDIT: (
            LegacyImportResourceKind.PUBLICATION_APPROVAL_IDENTITY
        ),
    }.get(assessment.kind)
    return parent_kind is not None and assessment.reference_id not in valid_parent_ids[parent_kind]


def _with_association(
    assessment: _Assessment,
    *,
    family: PublicationFamily,
    actor: str,
    approved_at: datetime,
    new_fingerprint: str,
) -> _Assessment:
    return _Assessment(
        table=assessment.table,
        source_id_digest=assessment.source_id_digest,
        kind=assessment.kind,
        payload_fingerprint=assessment.payload_fingerprint,
        valid=assessment.valid,
        target_id=assessment.target_id,
        reference_id=assessment.reference_id,
        association_fingerprint=fingerprint_legacy_value(
            {
                "family": family.value,
                "actor": actor,
                "approved_at": approved_at.astimezone(UTC).isoformat(),
                "new_fingerprint": new_fingerprint,
            }
        ),
    )


def _model(values: tuple[object, ...], index: int, model: type[_ModelT]) -> _ModelT:
    payload = _text(values, index)
    if len(payload.encode("utf-8")) > _MAX_LEGACY_TEXT_BYTES:
        raise ValueError
    return model.model_validate_json(payload)


def _text(values: tuple[object, ...], index: int) -> str:
    value = values[index]
    if not isinstance(value, str) or not value.strip():
        raise ValueError
    return value


def _bounded_text(values: tuple[object, ...], index: int, limit: int) -> str:
    value = _text(values, index)
    if len(value) > limit:
        raise ValueError
    return value


def _integer(values: tuple[object, ...], index: int) -> int:
    value = values[index]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError
    return value


def _sha256(values: tuple[object, ...], index: int) -> str:
    value = _text(values, index)
    if _SHA256.fullmatch(value) is None:
        raise ValueError
    return value


def _aware_text(values: tuple[object, ...], index: int) -> datetime:
    value = datetime.fromisoformat(_text(values, index))
    _require_aware(value, "legacy timestamp")
    return value


def _require_identity_revision(
    actual_id: str,
    actual_revision: int,
    expected_id: str,
    expected_revision: int,
) -> None:
    if actual_id != expected_id or actual_revision != expected_revision:
        raise ValueError


def _row_fingerprint(values: tuple[object, ...]) -> str:
    return fingerprint_legacy_value([_safe_scalar(value) for value in values])


def _safe_scalar(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return {"non_finite_float": repr(value)}
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest()}
    return {"unsupported_type": type(value).__name__}


def _validate_snapshot(snapshot: LegacySqliteSnapshot) -> None:
    if (
        _SHA256.fullmatch(snapshot.source_fingerprint) is None
        or _SHA256.fullmatch(snapshot.source_schema_fingerprint) is None
        or not snapshot.tables
    ):
        raise LegacyImportError(
            LegacyImportErrorCode.INVALID_RESPONSE,
            "legacy source returned invalid snapshot identity",
        )
    names = tuple(table.name for table in snapshot.tables)
    if (
        names != tuple(sorted(names))
        or len(names) != len(set(names))
        or any(name not in _EXPECTED_COLUMNS for name in names)
    ):
        raise LegacyImportError(
            LegacyImportErrorCode.INVALID_RESPONSE,
            "legacy source returned an unexpected table set",
        )
    total = 0
    for table in snapshot.tables:
        if table.columns != _EXPECTED_COLUMNS[table.name]:
            raise LegacyImportError(
                LegacyImportErrorCode.INVALID_RESPONSE,
                "legacy source returned unexpected columns",
            )
        digests = tuple(row.source_id_digest for row in table.rows)
        if (
            digests != tuple(sorted(digests))
            or len(digests) != len(set(digests))
            or any(_SHA256.fullmatch(value) is None for value in digests)
            or any(len(row.values) != len(table.columns) for row in table.rows)
        ):
            raise LegacyImportError(
                LegacyImportErrorCode.INVALID_RESPONSE,
                "legacy source returned invalid row identities",
            )
        total += len(table.rows)
    if total > _MAX_ROWS:
        raise LegacyImportError(
            LegacyImportErrorCode.INVALID_RESPONSE,
            "legacy source exceeded the bounded row contract",
        )


def _inspect_plan(source: LegacyControlPlaneSourcePort) -> LegacyImportPlan:
    try:
        snapshot = source.inspect()
    except LegacyImportPortError as error:
        raise _source_error(error) from error
    return plan_legacy_control_plane_import(snapshot)


def _validated_reservation(value: object) -> LegacyImportReservation:
    try:
        validated = LegacyImportReservation.model_validate(
            value.model_dump(mode="python", warnings=False)  # type: ignore[attr-defined]
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise LegacyImportError(
            LegacyImportErrorCode.INVALID_RESPONSE,
            "legacy import store returned invalid reservation state",
        ) from error
    if validated != value:
        raise LegacyImportError(
            LegacyImportErrorCode.INVALID_RESPONSE,
            "legacy import store returned inconsistent reservation state",
        )
    return validated


def _validated_result(value: object) -> LegacyImportResult:
    try:
        validated = LegacyImportResult.model_validate(
            value.model_dump(mode="python", warnings=False)  # type: ignore[attr-defined]
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise LegacyImportError(
            LegacyImportErrorCode.INVALID_RESPONSE,
            "legacy import store returned invalid completion state",
        ) from error
    if validated != value:
        raise LegacyImportError(
            LegacyImportErrorCode.INVALID_RESPONSE,
            "legacy import store returned inconsistent completion state",
        )
    return validated


def _require_reservation_matches(
    plan: LegacyImportPlan,
    reservation: LegacyImportReservation,
) -> None:
    if (
        reservation.import_id != plan.id
        or reservation.source_fingerprint != plan.source_fingerprint
        or reservation.source_schema_fingerprint != plan.source_schema_fingerprint
        or reservation.plan_fingerprint != plan.fingerprint
        or reservation.counts != plan.counts
    ):
        raise LegacyImportError(
            LegacyImportErrorCode.INVALID_RESPONSE,
            "legacy import reservation does not match the exact dry-run",
        )


def _source_error(error: LegacyImportPortError) -> LegacyImportError:
    mapping = {
        LegacyImportPortErrorCode.SOURCE_UNAVAILABLE: (LegacyImportErrorCode.SOURCE_UNAVAILABLE),
        LegacyImportPortErrorCode.SOURCE_NOT_OFFLINE: (LegacyImportErrorCode.SOURCE_NOT_OFFLINE),
        LegacyImportPortErrorCode.SOURCE_TOO_LARGE: (LegacyImportErrorCode.SOURCE_TOO_LARGE),
        LegacyImportPortErrorCode.SOURCE_CORRUPT: LegacyImportErrorCode.SOURCE_CORRUPT,
        LegacyImportPortErrorCode.SOURCE_SCHEMA_INVALID: (
            LegacyImportErrorCode.SOURCE_SCHEMA_INVALID
        ),
        LegacyImportPortErrorCode.SOURCE_CHANGED: LegacyImportErrorCode.SOURCE_CHANGED,
    }
    return LegacyImportError(
        mapping.get(error.code, LegacyImportErrorCode.SOURCE_UNAVAILABLE),
        str(error),
    )


def _store_error(error: LegacyImportPortError) -> LegacyImportError:
    code = (
        LegacyImportErrorCode.STORE_CONFLICT
        if error.code is LegacyImportPortErrorCode.STORE_CONFLICT
        else LegacyImportErrorCode.STORE_UNAVAILABLE
    )
    return LegacyImportError(code, str(error))


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
