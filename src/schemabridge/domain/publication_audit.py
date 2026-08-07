"""Pure, immutable audit facts for one governed publication target."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel

_SAFE_OPERATION = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class PublicationAuditOutcome(StrEnum):
    """Normalized result shared by every publication family."""

    SUCCEEDED = "succeeded"
    ALREADY_CURRENT = "already_current"
    FAILED = "failed"
    NOT_ATTEMPTED = "not_attempted"


class PublicationFamily(StrEnum):
    CANONICAL = "canonical"
    JOIN = "join"
    REGISTRY = "registry"
    WORKFLOW = "workflow"
    RECIPE = "recipe"


class PublicationTargetAuditRecord(FrozenDomainModel):
    """Complete append-only transition evidence for exactly one target."""

    family: PublicationFamily
    operation: str = Field(min_length=2, max_length=64)
    target: str = Field(min_length=1, max_length=500)
    approval_id: str = Field(min_length=1, max_length=200)
    actor: str = Field(min_length=1, max_length=120)
    approved_at: datetime
    previous_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description=(
            "Verified prior publication fingerprint for this exact target; null means that no "
            "prior fingerprint was verifiable, not necessarily that the target did not exist."
        ),
    )
    new_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: PublicationAuditOutcome
    decision_ids: tuple[str, ...] = ()
    reason_code: str | None = Field(default=None, min_length=2, max_length=64)

    @field_validator("operation")
    @classmethod
    def operation_must_be_inert(cls, value: str) -> str:
        if _SAFE_OPERATION.fullmatch(value) is None:
            raise ValueError("publication audit operation must be an inert identifier")
        return value

    @field_validator("target", "approval_id", "actor")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("publication audit text must not be blank")
        return value

    @field_validator("approved_at")
    @classmethod
    def approval_time_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("publication audit approval time must include a timezone")
        return value

    @field_validator("decision_ids")
    @classmethod
    def decision_ids_must_be_unique_and_nonblank(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(not value.strip() for value in values):
            raise ValueError("publication audit decision ids must be unique and nonblank")
        return values

    @field_validator("reason_code")
    @classmethod
    def reason_must_be_inert(cls, value: str | None) -> str | None:
        if value is not None and _SAFE_REASON.fullmatch(value) is None:
            raise ValueError("publication audit reason must be an inert identifier")
        return value

    @model_validator(mode="after")
    def reason_must_match_outcome(self) -> PublicationTargetAuditRecord:
        failed = self.outcome in {
            PublicationAuditOutcome.FAILED,
            PublicationAuditOutcome.NOT_ATTEMPTED,
        }
        if failed != (self.reason_code is not None):
            raise ValueError("failed publication audit outcomes require exactly one reason")
        if (
            self.outcome is PublicationAuditOutcome.ALREADY_CURRENT
            and self.previous_fingerprint != self.new_fingerprint
        ):
            raise ValueError("already-current audit records require equal old and new fingerprints")
        return self


def validate_publication_audit_binding(
    records: tuple[PublicationTargetAuditRecord, ...],
    *,
    approval_id: str,
    actor: str,
    approved_at: datetime,
    new_fingerprint: str,
    approved_decision_ids: tuple[str, ...] = (),
) -> None:
    """Reject an adapter result that is not bound to the exact approved mutation."""

    if not records:
        raise ValueError("publication result must contain target audit records")
    expected_decisions = set(approved_decision_ids)
    for record in records:
        if (
            record.approval_id != approval_id
            or record.actor != actor
            or record.approved_at != approved_at
            or record.new_fingerprint != new_fingerprint
        ):
            raise ValueError("publication target audit is not bound to the exact approval")
        if set(record.decision_ids) != expected_decisions:
            raise ValueError("publication target audit decisions do not match the exact approval")
