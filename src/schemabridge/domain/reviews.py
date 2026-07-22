"""Pure canonical-review and approved-publication contracts."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.concepts import CanonicalField, LogicalFieldRef, LogicalModel
from schemabridge.domain.decisions import (
    ApprovalStatus,
    DecisionAction,
    DecisionRecord,
)
from schemabridge.domain.mappings import ColumnMapping
from schemabridge.domain.transformations import TransformationPlan

_DRAFT_ID = re.compile(r"^[a-z][a-z0-9_-]*$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


class PublicationConfirmation(StrEnum):
    """Exact human confirmation accepted by a publication entrypoint."""

    PUBLISH_APPROVED_CANONICAL_CONTEXT = "publish-approved-canonical-context"


class PublicationStatus(StrEnum):
    """Overall result; partial work is never represented as current."""

    PUBLISHED = "published"
    ALREADY_CURRENT = "already_current"
    PARTIAL_FAILURE = "partial_failure"


class PublicationItemStatus(StrEnum):
    PUBLISHED = "published"
    ALREADY_CURRENT = "already_current"
    FAILED = "failed"
    NOT_ATTEMPTED = "not_attempted"


class PublicationItemKind(StrEnum):
    STRUCTURED_PROPERTY = "structured_property"
    GLOSSARY_TERM = "glossary_term"
    LOGICAL_MODEL = "logical_model"
    PHYSICAL_LINK = "physical_link"
    DECISION_DOCUMENT = "decision_document"
    PUBLICATION_MARKER = "publication_marker"


def mapping_target_id(mapping: ColumnMapping) -> str:
    """Return a stable, inert audit identity for a physical-to-logical mapping."""

    return f"{mapping.physical_field.root}->{mapping.logical_field.root}"


class ReviewedMapping(FrozenDomainModel):
    """A candidate mapping plus its latest explicit human decision."""

    mapping: ColumnMapping
    last_decision_id: str | None = None
    different_concept: LogicalFieldRef | None = None

    @model_validator(mode="after")
    def terminal_states_require_decisions(self) -> ReviewedMapping:
        if (
            self.mapping.status in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}
            and self.last_decision_id is None
        ):
            raise ValueError("terminal reviewed mapping requires a decision id")
        if self.different_concept is not None:
            if self.mapping.status is not ApprovalStatus.REJECTED:
                raise ValueError("different-concept mapping must be rejected from this concept")
            if self.different_concept == self.mapping.logical_field:
                raise ValueError("different concept must differ from the reviewed logical field")
        return self


class CanonicalReviewDraft(FrozenDomainModel):
    """Versioned local review state; DataHub is not the draft source of truth."""

    id: str = Field(min_length=1)
    revision: int = Field(default=1, ge=1)
    logical_model: LogicalModel
    fields: tuple[CanonicalField, ...] = Field(min_length=1)
    mappings: tuple[ReviewedMapping, ...] = Field(min_length=1)

    @field_validator("id")
    @classmethod
    def draft_id_must_be_inert(cls, value: str) -> str:
        if _DRAFT_ID.fullmatch(value) is None:
            raise ValueError("draft id must be a lowercase inert identifier")
        return value

    @model_validator(mode="after")
    def contents_and_status_must_be_consistent(self) -> CanonicalReviewDraft:
        if self.logical_model.version != self.revision:
            raise ValueError("logical model version must match the draft revision")
        field_roots = [field.id.root for field in self.fields]
        if len(field_roots) != len(set(field_roots)):
            raise ValueError("canonical review fields must be unique")
        if set(field_roots) != {field.root for field in self.logical_model.fields}:
            raise ValueError("canonical review fields must match the logical model")
        targets = [mapping_target_id(item.mapping) for item in self.mappings]
        if len(targets) != len(set(targets)):
            raise ValueError("canonical review mappings must be unique")
        if any(item.mapping.logical_field.root not in field_roots for item in self.mappings):
            raise ValueError("reviewed mapping must target a field in the logical model")
        expected_status = (
            ApprovalStatus.APPROVED if self.is_ready_to_publish() else ApprovalStatus.NEEDS_REVIEW
        )
        if self.logical_model.status is not expected_status:
            raise ValueError("logical model status does not match review readiness")
        return self

    def is_ready_to_publish(self) -> bool:
        """Require terminal decisions and at least one approved mapping per field."""

        if any(
            item.mapping.status not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}
            for item in self.mappings
        ):
            return False
        approved_fields = {
            item.mapping.logical_field.root
            for item in self.mappings
            if item.mapping.status is ApprovalStatus.APPROVED
        }
        return approved_fields == {field.id.root for field in self.fields}


class ModelDescriptionEdit(FrozenDomainModel):
    operation: Literal["model_description"] = "model_description"
    description: str = Field(min_length=1)

    @field_validator("description")
    @classmethod
    def description_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("description must not be blank")
        return value


class FieldDefinitionEdit(FrozenDomainModel):
    operation: Literal["field_definition"] = "field_definition"
    field: LogicalFieldRef
    definition: str = Field(min_length=1)

    @field_validator("definition")
    @classmethod
    def definition_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("definition must not be blank")
        return value


class MappingTransformationEdit(FrozenDomainModel):
    operation: Literal["mapping_transformation"] = "mapping_transformation"
    target_id: str = Field(min_length=1)
    transformation_plan: TransformationPlan


ReviewEdit: TypeAlias = Annotated[
    ModelDescriptionEdit | FieldDefinitionEdit | MappingTransformationEdit,
    Field(discriminator="operation"),
]


class CanonicalPublication(FrozenDomainModel):
    """Validated approved payload that may be offered to a catalog writer."""

    draft_id: str
    draft_version: int = Field(ge=1)
    logical_model: LogicalModel
    fields: tuple[CanonicalField, ...] = Field(min_length=1)
    mappings: tuple[ColumnMapping, ...] = Field(min_length=1)
    decisions: tuple[DecisionRecord, ...] = Field(min_length=1)
    fingerprint: str

    @classmethod
    def create(
        cls,
        *,
        draft_id: str,
        draft_version: int,
        logical_model: LogicalModel,
        fields: tuple[CanonicalField, ...],
        mappings: tuple[ColumnMapping, ...],
        decisions: tuple[DecisionRecord, ...],
    ) -> CanonicalPublication:
        """Construct a publication with a fingerprint derived from its full payload."""

        provisional = cls.model_construct(
            draft_id=draft_id,
            draft_version=draft_version,
            logical_model=logical_model,
            fields=fields,
            mappings=mappings,
            decisions=decisions,
            fingerprint="0" * 64,
        )
        return cls(
            draft_id=draft_id,
            draft_version=draft_version,
            logical_model=logical_model,
            fields=fields,
            mappings=mappings,
            decisions=decisions,
            fingerprint=publication_fingerprint(provisional),
        )

    @field_validator("fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        if _FINGERPRINT.fullmatch(value) is None:
            raise ValueError("publication fingerprint must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def approval_and_fingerprint_must_be_consistent(self) -> CanonicalPublication:
        if self.logical_model.status is not ApprovalStatus.APPROVED:
            raise ValueError("publication requires an approved logical model")
        if any(mapping.status is not ApprovalStatus.APPROVED for mapping in self.mappings):
            raise ValueError("publication contains an unapproved mapping")
        approval_targets = {
            decision.target_id
            for decision in self.decisions
            if decision.action is DecisionAction.APPROVE
            and decision.status is ApprovalStatus.APPROVED
        }
        if any(mapping_target_id(mapping) not in approval_targets for mapping in self.mappings):
            raise ValueError("every published mapping requires an approval decision")
        if self.fingerprint != publication_fingerprint(self):
            raise ValueError("publication fingerprint does not match its approved payload")
        return self


def publication_fingerprint(publication: CanonicalPublication) -> str:
    """Hash only the immutable approved payload, excluding the asserted fingerprint."""

    payload = publication.model_dump(mode="json", exclude={"fingerprint"})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class PublicationApproval(FrozenDomainModel):
    """Separate explicit authorization for one exact catalog payload."""

    id: str = Field(min_length=1)
    draft_id: str = Field(min_length=1)
    draft_version: int = Field(ge=1)
    payload_fingerprint: str
    actor: str = Field(min_length=1)
    approved_at: datetime
    decision_ids: tuple[str, ...] = Field(min_length=1)
    confirmation: PublicationConfirmation

    @field_validator("id", "draft_id", "actor")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("publication approval text must not be blank")
        return value

    @field_validator("payload_fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        if _FINGERPRINT.fullmatch(value) is None:
            raise ValueError("publication approval fingerprint must be lowercase SHA-256")
        return value

    @field_validator("approved_at")
    @classmethod
    def approval_time_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("publication approval time must include a timezone")
        return value

    @model_validator(mode="after")
    def decision_ids_must_be_unique(self) -> PublicationApproval:
        if len(self.decision_ids) != len(set(self.decision_ids)):
            raise ValueError("publication approval decision ids must be unique")
        return self


class PublicationDecisionRef(FrozenDomainModel):
    id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    version: int = Field(ge=1)


class PublicationItemResult(FrozenDomainModel):
    kind: PublicationItemKind
    target: str = Field(min_length=1)
    status: PublicationItemStatus
    decision_refs: tuple[PublicationDecisionRef, ...] = Field(min_length=1)
    reason_code: str | None = None

    @model_validator(mode="after")
    def failures_require_reason(self) -> PublicationItemResult:
        if self.status in {PublicationItemStatus.FAILED, PublicationItemStatus.NOT_ATTEMPTED}:
            if self.reason_code is None:
                raise ValueError("failed or skipped publication item requires a reason code")
        elif self.reason_code is not None:
            raise ValueError("successful publication item cannot have a reason code")
        return self


class PublicationResult(FrozenDomainModel):
    approval_id: str = Field(min_length=1)
    draft_id: str = Field(min_length=1)
    fingerprint: str
    status: PublicationStatus
    items: tuple[PublicationItemResult, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def overall_status_must_match_items(self) -> PublicationResult:
        statuses = {item.status for item in self.items}
        if self.status is PublicationStatus.ALREADY_CURRENT:
            if statuses != {PublicationItemStatus.ALREADY_CURRENT}:
                raise ValueError("already-current publication requires every item to be current")
        elif self.status is PublicationStatus.PUBLISHED:
            if (
                not statuses
                <= {
                    PublicationItemStatus.PUBLISHED,
                    PublicationItemStatus.ALREADY_CURRENT,
                }
                or PublicationItemStatus.PUBLISHED not in statuses
            ):
                raise ValueError("published result requires only successful items")
        elif not statuses & {
            PublicationItemStatus.FAILED,
            PublicationItemStatus.NOT_ATTEMPTED,
        }:
            raise ValueError("partial failure requires a failed or skipped item")
        return self


class PublishedCanonicalContext(FrozenDomainModel):
    """Vendor-neutral read-back proof for the approved canonical context."""

    logical_model_urn: str = Field(min_length=1)
    fingerprint: str
    fields: tuple[LogicalFieldRef, ...] = Field(min_length=1)
    physical_links: tuple[str, ...] = Field(min_length=1)
    glossary_terms: tuple[str, ...] = Field(min_length=1)
    decision_document_urn: str = Field(min_length=1)
    decision_refs: tuple[PublicationDecisionRef, ...] = Field(min_length=1)
