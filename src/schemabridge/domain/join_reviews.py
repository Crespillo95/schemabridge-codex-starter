"""Pure review, approval, and publication values for governed join contracts."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.decisions import ApprovalStatus, DecisionAction, DecisionRecord
from schemabridge.domain.joins import JoinCandidate, JoinContract, JoinContractSet

_DRAFT_ID = re.compile(r"^[a-z][a-z0-9_-]*$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


class JoinPublicationConfirmation(StrEnum):
    PUBLISH_APPROVED_JOIN_CONTRACTS = "publish-approved-join-contracts"


class JoinPublicationStatus(StrEnum):
    PUBLISHED = "published"
    ALREADY_CURRENT = "already_current"
    PARTIAL_FAILURE = "partial_failure"


class JoinPublicationItemKind(StrEnum):
    VERSIONED_DECISION_DOCUMENT = "versioned_decision_document"
    CURRENT_CONTEXT_MARKER = "current_context_marker"


class JoinPublicationItemStatus(StrEnum):
    PUBLISHED = "published"
    ALREADY_CURRENT = "already_current"
    FAILED = "failed"
    NOT_ATTEMPTED = "not_attempted"


class ReviewedJoinCandidate(FrozenDomainModel):
    candidate: JoinCandidate
    status: ApprovalStatus = ApprovalStatus.NEEDS_REVIEW
    decision_id: str | None = None
    contract: JoinContract | None = None

    @model_validator(mode="after")
    def state_must_match_decision(self) -> ReviewedJoinCandidate:
        if self.status is ApprovalStatus.NEEDS_REVIEW:
            if self.decision_id is not None or self.contract is not None:
                raise ValueError("unreviewed join cannot carry a decision or contract")
        elif self.status is ApprovalStatus.APPROVED:
            if self.decision_id is None or self.contract is None:
                raise ValueError("approved join requires a decision and contract")
            if self.contract.status is not ApprovalStatus.APPROVED:
                raise ValueError("approved review requires an approved contract")
            if self.contract.approval_decision_id != self.decision_id:
                raise ValueError("join contract must reference its approval decision")
        elif self.status is ApprovalStatus.REJECTED:
            if self.decision_id is None or self.contract is not None:
                raise ValueError("rejected join requires a decision and no contract")
        else:
            raise ValueError("join review status must be needs_review, approved, or rejected")
        return self


class JoinReviewDraft(FrozenDomainModel):
    id: str = Field(min_length=1)
    revision: int = Field(default=1, ge=1)
    joins: tuple[ReviewedJoinCandidate, ...] = Field(min_length=1, max_length=2)

    @field_validator("id")
    @classmethod
    def draft_id_must_be_inert(cls, value: str) -> str:
        if _DRAFT_ID.fullmatch(value) is None:
            raise ValueError("join review id must be a lowercase inert identifier")
        return value

    @model_validator(mode="after")
    def join_ids_must_be_unique(self) -> JoinReviewDraft:
        identifiers = [item.candidate.proposal.id for item in self.joins]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("join review candidates must be unique")
        return self

    def approved_contracts(self) -> tuple[JoinContract, ...]:
        return tuple(item.contract for item in self.joins if item.contract is not None)

    def is_ready_to_publish(self) -> bool:
        return bool(self.joins) and all(
            item.status is ApprovalStatus.APPROVED for item in self.joins
        )


class JoinContractPublication(FrozenDomainModel):
    draft_id: str = Field(min_length=1)
    draft_version: int = Field(ge=1)
    contract_set: JoinContractSet
    decisions: tuple[DecisionRecord, ...] = Field(min_length=1)
    fingerprint: str

    @classmethod
    def create(
        cls,
        *,
        draft_id: str,
        draft_version: int,
        contracts: tuple[JoinContract, ...],
        decisions: tuple[DecisionRecord, ...],
    ) -> JoinContractPublication:
        provisional = cls.model_construct(
            draft_id=draft_id,
            draft_version=draft_version,
            contract_set=JoinContractSet(version=draft_version, contracts=contracts),
            decisions=decisions,
            fingerprint="0" * 64,
        )
        return cls(
            draft_id=draft_id,
            draft_version=draft_version,
            contract_set=provisional.contract_set,
            decisions=decisions,
            fingerprint=join_publication_fingerprint(provisional),
        )

    @field_validator("fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        if _FINGERPRINT.fullmatch(value) is None:
            raise ValueError("join publication fingerprint must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def contracts_must_have_matching_approvals(self) -> JoinContractPublication:
        approvals = {
            decision.id: decision
            for decision in self.decisions
            if decision.action is DecisionAction.APPROVE
            and decision.status is ApprovalStatus.APPROVED
        }
        if any(
            contract.status is not ApprovalStatus.APPROVED
            for contract in self.contract_set.contracts
        ):
            raise ValueError("join publication contains an unapproved contract")
        for contract in self.contract_set.contracts:
            decision = approvals.get(contract.approval_decision_id or "")
            if decision is None or decision.target_id != contract.id:
                raise ValueError("every join contract requires its immutable approval decision")
        if self.fingerprint != join_publication_fingerprint(self):
            raise ValueError("join publication fingerprint does not match its payload")
        return self


def join_publication_fingerprint(publication: JoinContractPublication) -> str:
    payload = publication.model_dump(mode="json", exclude={"fingerprint"})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class JoinPublicationApproval(FrozenDomainModel):
    id: str = Field(min_length=1)
    draft_id: str = Field(min_length=1)
    draft_version: int = Field(ge=1)
    payload_fingerprint: str
    actor: str = Field(min_length=1)
    approved_at: datetime
    decision_ids: tuple[str, ...] = Field(min_length=1)
    confirmation: JoinPublicationConfirmation

    @field_validator("id", "draft_id", "actor")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("join publication approval text must not be blank")
        return value

    @field_validator("payload_fingerprint")
    @classmethod
    def approval_fingerprint_must_be_sha256(cls, value: str) -> str:
        if _FINGERPRINT.fullmatch(value) is None:
            raise ValueError("join approval fingerprint must be lowercase SHA-256")
        return value

    @field_validator("approved_at")
    @classmethod
    def approval_time_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("join publication approval time must include a timezone")
        return value

    @model_validator(mode="after")
    def decision_ids_must_be_unique(self) -> JoinPublicationApproval:
        if len(self.decision_ids) != len(set(self.decision_ids)):
            raise ValueError("join approval decision ids must be unique")
        return self


class JoinPublicationItemResult(FrozenDomainModel):
    kind: JoinPublicationItemKind
    target: str = Field(min_length=1)
    status: JoinPublicationItemStatus
    reason_code: str | None = None

    @model_validator(mode="after")
    def failure_reason_must_match_status(self) -> JoinPublicationItemResult:
        failed = self.status in {
            JoinPublicationItemStatus.FAILED,
            JoinPublicationItemStatus.NOT_ATTEMPTED,
        }
        if failed != (self.reason_code is not None):
            raise ValueError("failed join publication item must carry exactly one reason code")
        return self


class JoinPublicationResult(FrozenDomainModel):
    approval_id: str = Field(min_length=1)
    draft_id: str = Field(min_length=1)
    fingerprint: str
    status: JoinPublicationStatus
    items: tuple[JoinPublicationItemResult, ...] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def status_must_match_items(self) -> JoinPublicationResult:
        statuses = {item.status for item in self.items}
        if self.status is JoinPublicationStatus.ALREADY_CURRENT:
            if statuses != {JoinPublicationItemStatus.ALREADY_CURRENT}:
                raise ValueError("current join publication requires all items to be current")
        elif self.status is JoinPublicationStatus.PUBLISHED:
            if (
                not statuses
                <= {
                    JoinPublicationItemStatus.PUBLISHED,
                    JoinPublicationItemStatus.ALREADY_CURRENT,
                }
                or JoinPublicationItemStatus.PUBLISHED not in statuses
            ):
                raise ValueError("published join result contains a failure")
        elif not statuses & {
            JoinPublicationItemStatus.FAILED,
            JoinPublicationItemStatus.NOT_ATTEMPTED,
        }:
            raise ValueError("partial join publication requires a failed or skipped item")
        return self


class PublishedJoinContext(FrozenDomainModel):
    document_urn: str = Field(min_length=1)
    fingerprint: str
    contract_set: JoinContractSet
    decision_ids: tuple[str, ...] = Field(min_length=1)
    related_asset_urns: tuple[str, ...] = Field(min_length=1)
