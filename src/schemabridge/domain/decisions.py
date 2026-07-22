"""Human-governance decisions and approval states."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel


class ApprovalStatus(StrEnum):
    """Governance state; confidence never changes this state implicitly."""

    PROPOSED = "proposed"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class DecisionAction(StrEnum):
    """An explicit human action on a governed target."""

    APPROVE = "approve"
    EDIT = "edit"
    REJECT = "reject"
    MARK_DIFFERENT_CONCEPT = "mark_different_concept"


class DecisionTargetType(StrEnum):
    """Kinds of governed objects that can receive a decision."""

    LOGICAL_MODEL = "logical_model"
    CANONICAL_FIELD = "canonical_field"
    COLUMN_MAPPING = "column_mapping"
    JOIN_CONTRACT = "join_contract"
    CATALOG_PUBLICATION = "catalog_publication"


class DecisionRecord(FrozenDomainModel):
    """Auditable record of one explicit semantic decision."""

    id: str = Field(min_length=1)
    target_type: DecisionTargetType
    target_id: str = Field(min_length=1)
    action: DecisionAction
    status: ApprovalStatus
    actor: str = Field(min_length=1)
    decided_at: datetime
    source_version: int = Field(ge=1)
    resulting_version: int = Field(ge=1)
    rationale: str = Field(min_length=1)
    evidence: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()

    @field_validator("id", "target_id", "actor", "rationale")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        """Reject values that contain whitespace only."""

        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("evidence", "risks")
    @classmethod
    def entries_must_not_be_blank(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Keep evidence and risks meaningful and deterministic."""

        if any(not value.strip() for value in values):
            raise ValueError("entries must not be blank")
        return values

    @field_validator("decided_at")
    @classmethod
    def decision_time_must_be_aware(cls, value: datetime) -> datetime:
        """Require callers to inject an unambiguous timestamp."""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("decided_at must include a timezone")
        return value

    @model_validator(mode="after")
    def action_must_match_status(self) -> DecisionRecord:
        """Prevent contradictory approval and rejection records."""

        expected = {
            DecisionAction.APPROVE: ApprovalStatus.APPROVED,
            DecisionAction.REJECT: ApprovalStatus.REJECTED,
            DecisionAction.MARK_DIFFERENT_CONCEPT: ApprovalStatus.REJECTED,
            DecisionAction.EDIT: ApprovalStatus.NEEDS_REVIEW,
        }.get(self.action)
        if expected is not None and self.status is not expected:
            raise ValueError(f"{self.action.value} requires status {expected.value}")
        return self
