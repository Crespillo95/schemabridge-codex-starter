"""Pure contracts for approval-gated legacy control-plane import."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[a-z][a-z0-9_-]{2,199}$")
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SAFE_TABLE = re.compile(r"^[a-z][a-z0-9_]{0,119}$")
_MAX_IMPORT_ITEMS = 100_000
_MAX_TARGET_PAYLOAD_BYTES = 2_500_000


class LegacyImportDisposition(StrEnum):
    """Planned handling for one exact legacy row."""

    IMPORT = "import"
    QUARANTINE = "quarantine"
    SKIP = "skip"


class LegacyImportResourceKind(StrEnum):
    WORKFLOW_DRAFT = "workflow_draft"
    WORKFLOW_ACCESS_GRANT = "workflow_access_grant"
    REQUEST_DRAFT = "request_draft"
    REVIEW_DRAFT = "review_draft"
    REVIEW_DECISION = "review_decision"
    REVIEW_PUBLICATION = "review_publication"
    JOIN_REVIEW_DRAFT = "join_review_draft"
    JOIN_REVIEW_DECISION = "join_review_decision"
    JOIN_PUBLICATION = "join_publication"
    PUBLICATION_APPROVAL_IDENTITY = "publication_approval_identity"
    PUBLICATION_TARGET_AUDIT = "publication_target_audit"
    QUERY_RECIPE = "query_recipe"
    FAKE_WORKFLOW_PUBLICATION = "fake_workflow_publication"


class LegacyImportConfirmation(StrEnum):
    IMPORT_VALIDATED_LEGACY_CONTROL_STATE = "import-validated-legacy-control-state"


class LegacyImportStatus(StrEnum):
    DRY_RUN = "dry_run"
    COMPLETED = "completed"


class LegacyImportCounts(FrozenDomainModel):
    total: int = Field(ge=0, le=_MAX_IMPORT_ITEMS)
    imported: int = Field(ge=0, le=_MAX_IMPORT_ITEMS)
    quarantined: int = Field(ge=0, le=_MAX_IMPORT_ITEMS)
    skipped: int = Field(ge=0, le=_MAX_IMPORT_ITEMS)
    preview_rows_stripped: int = Field(ge=0, le=10_000_000)

    @model_validator(mode="after")
    def dispositions_must_cover_every_item(self) -> LegacyImportCounts:
        if self.imported + self.quarantined + self.skipped != self.total:
            raise ValueError("legacy import counts must cover every source row")
        return self


class LegacyImportItem(FrozenDomainModel):
    """One fingerprinted import, quarantine, or local-only decision."""

    source_table: str = Field(min_length=1, max_length=120)
    source_id_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource_kind: LegacyImportResourceKind
    disposition: LegacyImportDisposition
    payload_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_id: str | None = Field(default=None, min_length=1, max_length=200)
    target_id: str | None = Field(default=None, min_length=1, max_length=200)
    reason_code: str | None = Field(default=None, min_length=2, max_length=64)
    target_payload_json: str | None = Field(
        default=None,
        max_length=_MAX_TARGET_PAYLOAD_BYTES,
        repr=False,
    )

    @field_validator("source_table")
    @classmethod
    def table_must_be_inert(cls, value: str) -> str:
        if _SAFE_TABLE.fullmatch(value) is None:
            raise ValueError("legacy source table must be inert")
        return value

    @field_validator("workspace_id", "target_id")
    @classmethod
    def optional_identifiers_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("legacy import identifiers must not be blank")
        return value

    @field_validator("reason_code")
    @classmethod
    def reason_must_be_inert(cls, value: str | None) -> str | None:
        if value is not None and _SAFE_REASON.fullmatch(value) is None:
            raise ValueError("legacy import reason code must be inert")
        return value

    @field_validator("target_payload_json")
    @classmethod
    def payload_must_be_canonical_object(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as error:
            raise ValueError("legacy target payload must be JSON") from error
        if not isinstance(parsed, dict) or _canonical_json(parsed) != value:
            raise ValueError("legacy target payload must be one canonical JSON object")
        return value

    @model_validator(mode="after")
    def disposition_shape_must_be_exact(self) -> LegacyImportItem:
        if self.disposition is LegacyImportDisposition.IMPORT:
            if (
                self.workspace_id is None
                or self.target_id is None
                or self.target_payload_json is None
                or self.reason_code is not None
            ):
                raise ValueError("imported legacy items need an exact target and no reason")
        elif (
            self.target_id is not None
            or self.target_payload_json is not None
            or self.reason_code is None
        ):
            raise ValueError("non-imported legacy items require only a stable reason")
        return self


class LegacyImportPlan(FrozenDomainModel):
    """Deterministic dry-run result for one immutable SQLite file."""

    id: str = Field(min_length=3, max_length=200)
    source_kind: str = Field(pattern=r"^sqlite$")
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_schema_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    items: tuple[LegacyImportItem, ...] = Field(max_length=_MAX_IMPORT_ITEMS)
    counts: LegacyImportCounts
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("id")
    @classmethod
    def identifier_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("legacy import id must be inert")
        return value

    @model_validator(mode="after")
    def identity_order_and_counts_must_match(self) -> LegacyImportPlan:
        if self.id != legacy_import_id(self.source_fingerprint):
            raise ValueError("legacy import id does not match its source")
        identities = tuple(
            (item.source_table, item.source_id_digest, item.resource_kind.value)
            for item in self.items
        )
        if identities != tuple(sorted(identities)) or len(identities) != len(set(identities)):
            raise ValueError("legacy import items must be canonical and unique")
        counts = LegacyImportCounts(
            total=len(self.items),
            imported=sum(item.disposition is LegacyImportDisposition.IMPORT for item in self.items),
            quarantined=sum(
                item.disposition is LegacyImportDisposition.QUARANTINE for item in self.items
            ),
            skipped=sum(item.disposition is LegacyImportDisposition.SKIP for item in self.items),
            preview_rows_stripped=self.counts.preview_rows_stripped,
        )
        if counts != self.counts:
            raise ValueError("legacy import plan counts do not match its items")
        expected = _fingerprint(
            self.model_dump(mode="json", exclude={"fingerprint"}, warnings=False)
        )
        if self.fingerprint != expected:
            raise ValueError("legacy import plan fingerprint does not match")
        return self


class LegacyImportApproval(FrozenDomainModel):
    id: str = Field(min_length=3, max_length=200)
    import_id: str = Field(min_length=3, max_length=200)
    plan_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    actor: str = Field(min_length=1, max_length=120)
    approved_at: datetime
    confirmation: LegacyImportConfirmation

    @field_validator("id", "import_id")
    @classmethod
    def identifiers_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("legacy import approval identifiers must be inert")
        return value

    @field_validator("actor")
    @classmethod
    def actor_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("legacy import actor must not be blank")
        return value

    @field_validator("approved_at")
    @classmethod
    def approval_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "legacy import approval time")


class LegacyImportReservation(FrozenDomainModel):
    import_id: str = Field(min_length=3, max_length=200)
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_schema_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    counts: LegacyImportCounts
    status: LegacyImportStatus
    recorded_at: datetime
    approval_id: str | None = Field(default=None, min_length=3, max_length=200)
    completed_at: datetime | None = None

    @field_validator("import_id", "approval_id")
    @classmethod
    def optional_ids_must_be_inert(cls, value: str | None) -> str | None:
        if value is not None and _SAFE_ID.fullmatch(value) is None:
            raise ValueError("legacy import reservation id must be inert")
        return value

    @field_validator("recorded_at", "completed_at")
    @classmethod
    def reservation_times_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _aware(value, "legacy import reservation time")

    @model_validator(mode="after")
    def terminal_fields_must_match_status(self) -> LegacyImportReservation:
        complete = self.status is LegacyImportStatus.COMPLETED
        if (complete and (self.approval_id is None or self.completed_at is None)) or (
            not complete and (self.approval_id is not None or self.completed_at is not None)
        ):
            raise ValueError("legacy import completion fields do not match status")
        if self.completed_at is not None and self.completed_at < self.recorded_at:
            raise ValueError("legacy import completion cannot predate dry-run")
        return self


class LegacyImportResult(FrozenDomainModel):
    reservation: LegacyImportReservation
    replayed: bool

    @model_validator(mode="after")
    def result_must_be_complete(self) -> LegacyImportResult:
        if self.reservation.status is not LegacyImportStatus.COMPLETED:
            raise ValueError("legacy import result requires completed state")
        return self


def build_legacy_import_plan(
    source_fingerprint: str,
    source_schema_fingerprint: str,
    items: tuple[LegacyImportItem, ...],
    *,
    preview_rows_stripped: int,
) -> LegacyImportPlan:
    """Build and fingerprint one canonical dry-run."""

    if (
        _SHA256.fullmatch(source_fingerprint) is None
        or _SHA256.fullmatch(source_schema_fingerprint) is None
    ):
        raise ValueError("legacy import source fingerprints must be lowercase SHA-256")
    ordered = tuple(
        sorted(
            items,
            key=lambda item: (
                item.source_table,
                item.source_id_digest,
                item.resource_kind.value,
            ),
        )
    )
    counts = LegacyImportCounts(
        total=len(ordered),
        imported=sum(item.disposition is LegacyImportDisposition.IMPORT for item in ordered),
        quarantined=sum(item.disposition is LegacyImportDisposition.QUARANTINE for item in ordered),
        skipped=sum(item.disposition is LegacyImportDisposition.SKIP for item in ordered),
        preview_rows_stripped=preview_rows_stripped,
    )
    values: dict[str, object] = {
        "id": legacy_import_id(source_fingerprint),
        "source_kind": "sqlite",
        "source_fingerprint": source_fingerprint,
        "source_schema_fingerprint": source_schema_fingerprint,
        "items": ordered,
        "counts": counts,
    }
    return LegacyImportPlan(
        **values,
        fingerprint=_fingerprint(
            {
                **values,
                "items": [item.model_dump(mode="json") for item in ordered],
                "counts": counts.model_dump(mode="json"),
            }
        ),
    )


def build_legacy_import_approval(
    plan: LegacyImportPlan,
    *,
    actor: str,
    approved_at: datetime,
    confirmation: LegacyImportConfirmation,
) -> LegacyImportApproval:
    """Bind a human approval to one exact dry-run."""

    approval_id = legacy_import_approval_id(plan, actor)
    return LegacyImportApproval(
        id=approval_id,
        import_id=plan.id,
        plan_fingerprint=plan.fingerprint,
        source_fingerprint=plan.source_fingerprint,
        actor=actor,
        approved_at=approved_at,
        confirmation=confirmation,
    )


def validate_legacy_import_approval(
    plan: LegacyImportPlan,
    approval: LegacyImportApproval,
) -> None:
    """Reject forged or stale approval objects."""

    try:
        validated_plan = LegacyImportPlan.model_validate(
            plan.model_dump(mode="python", warnings=False)
        )
        validated_approval = LegacyImportApproval.model_validate(
            approval.model_dump(mode="python", warnings=False)
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("legacy import approval failed typed validation") from error
    if (
        validated_plan != plan
        or validated_approval != approval
        or approval.id != legacy_import_approval_id(plan, approval.actor)
        or approval.import_id != plan.id
        or approval.plan_fingerprint != plan.fingerprint
        or approval.source_fingerprint != plan.source_fingerprint
        or approval.confirmation
        is not LegacyImportConfirmation.IMPORT_VALIDATED_LEGACY_CONTROL_STATE
    ):
        raise ValueError("legacy import approval does not match the exact dry-run")


def legacy_import_id(source_fingerprint: str) -> str:
    return f"legacy-import-v1-{source_fingerprint}"


def legacy_import_approval_id(plan: LegacyImportPlan, actor: str) -> str:
    return f"legacy-import-approval-v1-{_fingerprint({'plan': plan.fingerprint, 'actor': actor})}"


def legacy_quarantine_id(import_id: str, item: LegacyImportItem) -> str:
    return (
        "legacy-quarantine-v1-"
        f"{_fingerprint({'import_id': import_id, 'table': item.source_table, 'row': item.source_id_digest})}"
    )


def canonical_json(value: object) -> str:
    """Return the one accepted durable JSON encoding."""

    return _canonical_json(value)


def fingerprint_legacy_value(value: object) -> str:
    return _fingerprint(value)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return value
