"""Explicit human review and approval-gated canonical publication use cases."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from schemabridge.application.ports.reviews import (
    CanonicalContextReadPort,
    CatalogWritePort,
    ReviewErrorCode,
    ReviewStorePort,
    ReviewWorkflowError,
)
from schemabridge.domain.concepts import CanonicalField, LogicalFieldRef, LogicalModel
from schemabridge.domain.decisions import (
    ApprovalStatus,
    DecisionAction,
    DecisionRecord,
    DecisionTargetType,
)
from schemabridge.domain.mappings import ColumnMapping
from schemabridge.domain.reviews import (
    CanonicalPublication,
    CanonicalReviewDraft,
    FieldDefinitionEdit,
    ModelDescriptionEdit,
    PublicationApproval,
    PublicationResult,
    PublishedCanonicalContext,
    ReviewEdit,
    ReviewedMapping,
    mapping_target_id,
)


@dataclass(frozen=True, slots=True)
class ReviewSnapshot:
    draft: CanonicalReviewDraft
    decisions: tuple[DecisionRecord, ...]
    publications: tuple[PublicationResult, ...]


@dataclass(frozen=True, slots=True)
class StartCanonicalReview:
    store: ReviewStorePort

    def execute(self, draft: CanonicalReviewDraft) -> CanonicalReviewDraft:
        return self.store.create(draft)


@dataclass(frozen=True, slots=True)
class InspectCanonicalReview:
    store: ReviewStorePort

    def execute(self, draft_id: str) -> ReviewSnapshot:
        draft = _required_draft(self.store, draft_id)
        return ReviewSnapshot(
            draft=draft,
            decisions=self.store.list_decisions(draft_id),
            publications=self.store.list_publications(draft_id),
        )


@dataclass(frozen=True, slots=True)
class EditCanonicalReview:
    store: ReviewStorePort

    def execute(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        actor: str,
        decided_at: datetime,
        rationale: str,
        edit: ReviewEdit,
    ) -> ReviewSnapshot:
        draft = _required_draft(self.store, draft_id)
        _require_revision(draft, expected_revision)
        new_revision = draft.revision + 1
        target_type, target_id, fields, mappings, logical_description = _apply_edit(draft, edit)
        decision = _decision(
            draft=draft,
            target_type=target_type,
            target_id=target_id,
            action=DecisionAction.EDIT,
            actor=actor,
            decided_at=decided_at,
            rationale=rationale,
            resulting_version=new_revision,
        )
        affected_targets = _affected_mapping_targets(edit, draft)
        mappings = tuple(
            ReviewedMapping(
                mapping=item.mapping,
                last_decision_id=(
                    decision.id
                    if mapping_target_id(item.mapping) in affected_targets
                    else item.last_decision_id
                ),
                different_concept=(
                    None
                    if mapping_target_id(item.mapping) in affected_targets
                    else item.different_concept
                ),
            )
            for item in mappings
        )
        revised = _rebuild_draft(
            draft,
            revision=new_revision,
            fields=fields,
            mappings=mappings,
            description=logical_description,
        )
        self.store.commit_decision(revised, decision, expected_revision=expected_revision)
        return InspectCanonicalReview(self.store).execute(draft_id)


@dataclass(frozen=True, slots=True)
class DecideCanonicalMapping:
    store: ReviewStorePort

    def execute(
        self,
        draft_id: str,
        target_id: str,
        action: DecisionAction,
        *,
        expected_revision: int,
        actor: str,
        decided_at: datetime,
        rationale: str,
        different_concept: LogicalFieldRef | None = None,
    ) -> ReviewSnapshot:
        if action not in {
            DecisionAction.APPROVE,
            DecisionAction.REJECT,
            DecisionAction.MARK_DIFFERENT_CONCEPT,
        }:
            raise ReviewWorkflowError(
                ReviewErrorCode.INVALID_TRANSITION,
                "mapping decision must approve, reject, or mark a different concept",
            )
        if action is DecisionAction.MARK_DIFFERENT_CONCEPT and different_concept is None:
            raise ReviewWorkflowError(
                ReviewErrorCode.INVALID_TRANSITION,
                "different-concept decision requires the alternative logical field",
            )
        if action is not DecisionAction.MARK_DIFFERENT_CONCEPT and different_concept is not None:
            raise ReviewWorkflowError(
                ReviewErrorCode.INVALID_TRANSITION,
                "alternative logical field is valid only for different-concept decisions",
            )

        draft = _required_draft(self.store, draft_id)
        _require_revision(draft, expected_revision)
        selected = next(
            (item for item in draft.mappings if mapping_target_id(item.mapping) == target_id),
            None,
        )
        if selected is None:
            raise ReviewWorkflowError(ReviewErrorCode.NOT_FOUND, "review mapping was not found")
        status = (
            ApprovalStatus.APPROVED if action is DecisionAction.APPROVE else ApprovalStatus.REJECTED
        )
        new_revision = draft.revision + 1
        decision = _decision(
            draft=draft,
            target_type=DecisionTargetType.COLUMN_MAPPING,
            target_id=target_id,
            action=action,
            actor=actor,
            decided_at=decided_at,
            rationale=rationale,
            resulting_version=new_revision,
            evidence=selected.mapping.evidence,
            risks=selected.mapping.risks,
        )
        mappings = tuple(
            ReviewedMapping(
                mapping=_mapping_with_status(item.mapping, status),
                last_decision_id=decision.id,
                different_concept=different_concept,
            )
            if mapping_target_id(item.mapping) == target_id
            else item
            for item in draft.mappings
        )
        revised = _rebuild_draft(
            draft,
            revision=new_revision,
            fields=draft.fields,
            mappings=mappings,
            description=draft.logical_model.description,
        )
        self.store.commit_decision(revised, decision, expected_revision=expected_revision)
        return InspectCanonicalReview(self.store).execute(draft_id)


@dataclass(frozen=True, slots=True)
class PrepareCanonicalPublication:
    store: ReviewStorePort

    def execute(self, draft_id: str) -> CanonicalPublication:
        draft = _required_draft(self.store, draft_id)
        if not draft.is_ready_to_publish():
            raise ReviewWorkflowError(
                ReviewErrorCode.NOT_READY,
                "canonical review still has unresolved or unmapped fields",
            )
        decisions = self.store.list_decisions(draft_id)
        decisions_by_id = {decision.id: decision for decision in decisions}
        approved_items = tuple(
            item for item in draft.mappings if item.mapping.status is ApprovalStatus.APPROVED
        )
        approved = tuple(item.mapping for item in approved_items)
        try:
            current_decisions = tuple(
                decisions_by_id[item.last_decision_id]
                for item in approved_items
                if item.last_decision_id is not None
            )
        except KeyError as error:
            raise ReviewWorkflowError(
                ReviewErrorCode.STORE_FAILURE,
                "approved mapping is missing its immutable decision record",
            ) from error
        return CanonicalPublication.create(
            draft_id=draft.id,
            draft_version=draft.revision,
            logical_model=draft.logical_model,
            fields=draft.fields,
            mappings=approved,
            decisions=current_decisions,
        )


@dataclass(frozen=True, slots=True)
class PublishCanonicalReview:
    store: ReviewStorePort
    writer: CatalogWritePort

    def execute(
        self,
        draft_id: str,
        approval: PublicationApproval,
    ) -> PublicationResult:
        if not isinstance(approval, PublicationApproval):
            raise ReviewWorkflowError(
                ReviewErrorCode.APPROVAL_REQUIRED,
                "explicit publication approval is required",
            )
        publication = PrepareCanonicalPublication(self.store).execute(draft_id)
        expected_ids = tuple(
            sorted(
                decision.id
                for decision in publication.decisions
                if decision.action is DecisionAction.APPROVE
                and decision.status is ApprovalStatus.APPROVED
            )
        )
        if (
            approval.draft_id != publication.draft_id
            or approval.draft_version != publication.draft_version
            or approval.payload_fingerprint != publication.fingerprint
            or tuple(sorted(approval.decision_ids)) != expected_ids
        ):
            raise ReviewWorkflowError(
                ReviewErrorCode.APPROVAL_MISMATCH,
                "publication approval does not match the current approved payload",
            )
        result = self.writer.publish(publication, approval)
        self.store.record_publication(result)
        return result


@dataclass(frozen=True, slots=True)
class ReadPublishedCanonicalContext:
    store: ReviewStorePort
    reader: CanonicalContextReadPort

    def execute(self, draft_id: str) -> PublishedCanonicalContext | None:
        publication = PrepareCanonicalPublication(self.store).execute(draft_id)
        return self.reader.read_context(publication)


def _required_draft(store: ReviewStorePort, draft_id: str) -> CanonicalReviewDraft:
    draft = store.load(draft_id)
    if draft is None:
        raise ReviewWorkflowError(ReviewErrorCode.NOT_FOUND, "canonical review was not found")
    return draft


def _require_revision(draft: CanonicalReviewDraft, expected_revision: int) -> None:
    if draft.revision != expected_revision:
        raise ReviewWorkflowError(
            ReviewErrorCode.CONFLICT,
            "canonical review revision changed; reload before deciding",
        )


def _decision(
    *,
    draft: CanonicalReviewDraft,
    target_type: DecisionTargetType,
    target_id: str,
    action: DecisionAction,
    actor: str,
    decided_at: datetime,
    rationale: str,
    resulting_version: int,
    evidence: tuple[str, ...] = (),
    risks: tuple[str, ...] = (),
) -> DecisionRecord:
    digest = hashlib.sha256(target_id.encode()).hexdigest()[:12]
    decision_id = f"{draft.id}-{action.value}-v{resulting_version}-{digest}"
    status = {
        DecisionAction.APPROVE: ApprovalStatus.APPROVED,
        DecisionAction.REJECT: ApprovalStatus.REJECTED,
        DecisionAction.MARK_DIFFERENT_CONCEPT: ApprovalStatus.REJECTED,
        DecisionAction.EDIT: ApprovalStatus.NEEDS_REVIEW,
    }[action]
    return DecisionRecord(
        id=decision_id,
        target_type=target_type,
        target_id=target_id,
        action=action,
        status=status,
        actor=actor,
        decided_at=decided_at,
        source_version=draft.revision,
        resulting_version=resulting_version,
        rationale=rationale,
        evidence=evidence,
        risks=risks,
    )


def _apply_edit(
    draft: CanonicalReviewDraft,
    edit: ReviewEdit,
) -> tuple[
    DecisionTargetType,
    str,
    tuple[CanonicalField, ...],
    tuple[ReviewedMapping, ...],
    str,
]:
    if isinstance(edit, ModelDescriptionEdit):
        reset = tuple(_reset_mapping(item) for item in draft.mappings)
        return (
            DecisionTargetType.LOGICAL_MODEL,
            draft.logical_model.id.root,
            draft.fields,
            reset,
            edit.description,
        )
    if isinstance(edit, FieldDefinitionEdit):
        if edit.field not in {field.id for field in draft.fields}:
            raise ReviewWorkflowError(ReviewErrorCode.NOT_FOUND, "canonical field was not found")
        fields = tuple(
            CanonicalField.model_validate(
                {**field.model_dump(mode="json"), "definition": edit.definition}
            )
            if field.id == edit.field
            else field
            for field in draft.fields
        )
        mappings = tuple(
            _reset_mapping(item) if item.mapping.logical_field == edit.field else item
            for item in draft.mappings
        )
        return (
            DecisionTargetType.CANONICAL_FIELD,
            edit.field.root,
            fields,
            mappings,
            draft.logical_model.description,
        )
    selected = next(
        (item for item in draft.mappings if mapping_target_id(item.mapping) == edit.target_id),
        None,
    )
    if selected is None:
        raise ReviewWorkflowError(ReviewErrorCode.NOT_FOUND, "review mapping was not found")
    mappings = tuple(
        ReviewedMapping(
            mapping=ColumnMapping.model_validate(
                {
                    **item.mapping.model_dump(mode="json"),
                    "status": ApprovalStatus.NEEDS_REVIEW,
                    "transformation_plan": edit.transformation_plan.model_dump(mode="json"),
                    "version": draft.revision + 1,
                }
            )
        )
        if mapping_target_id(item.mapping) == edit.target_id
        else item
        for item in draft.mappings
    )
    return (
        DecisionTargetType.COLUMN_MAPPING,
        edit.target_id,
        draft.fields,
        mappings,
        draft.logical_model.description,
    )


def _affected_mapping_targets(
    edit: ReviewEdit,
    draft: CanonicalReviewDraft,
) -> frozenset[str]:
    if isinstance(edit, ModelDescriptionEdit):
        return frozenset(mapping_target_id(item.mapping) for item in draft.mappings)
    if isinstance(edit, FieldDefinitionEdit):
        return frozenset(
            mapping_target_id(item.mapping)
            for item in draft.mappings
            if item.mapping.logical_field == edit.field
        )
    return frozenset({edit.target_id})


def _reset_mapping(item: ReviewedMapping) -> ReviewedMapping:
    return ReviewedMapping(mapping=_mapping_with_status(item.mapping, ApprovalStatus.NEEDS_REVIEW))


def _mapping_with_status(mapping: ColumnMapping, status: ApprovalStatus) -> ColumnMapping:
    return ColumnMapping.model_validate(
        {
            **mapping.model_dump(mode="json"),
            "status": status,
            "version": mapping.version + 1,
        }
    )


def _rebuild_draft(
    draft: CanonicalReviewDraft,
    *,
    revision: int,
    fields: tuple[CanonicalField, ...],
    mappings: tuple[ReviewedMapping, ...],
    description: str,
) -> CanonicalReviewDraft:
    terminal = all(
        item.mapping.status in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}
        for item in mappings
    )
    approved_fields = {
        item.mapping.logical_field.root
        for item in mappings
        if item.mapping.status is ApprovalStatus.APPROVED
    }
    ready = terminal and approved_fields == {field.id.root for field in fields}
    logical_model = LogicalModel(
        id=draft.logical_model.id,
        name=draft.logical_model.name,
        description=description,
        fields=tuple(field.id for field in fields),
        status=ApprovalStatus.APPROVED if ready else ApprovalStatus.NEEDS_REVIEW,
        version=revision,
    )
    return CanonicalReviewDraft(
        id=draft.id,
        revision=revision,
        logical_model=logical_model,
        fields=fields,
        mappings=mappings,
    )
