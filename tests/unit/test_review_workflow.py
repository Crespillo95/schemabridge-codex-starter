from __future__ import annotations

from datetime import UTC, datetime

import pytest

from schemabridge.adapters.datahub.canonical_urns import (
    CUSTOMER_KEY_TERM_URN,
    LOGICAL_CUSTOMER_URN,
)
from schemabridge.adapters.datahub.fake_writeback import (
    FakeCatalogWriteAdapter,
    FakeCatalogWriteBackend,
)
from schemabridge.adapters.storage.publication_audit import InMemoryPublicationAuditStore
from schemabridge.adapters.storage.reviews import InMemoryReviewStore
from schemabridge.application.canonical_review import (
    DecideCanonicalMapping,
    EditCanonicalReview,
    PrepareCanonicalPublication,
    PublishCanonicalReview,
    StartCanonicalReview,
)
from schemabridge.application.ports.reviews import ReviewErrorCode, ReviewWorkflowError
from schemabridge.application.review_demo import build_customer_review_draft
from schemabridge.domain.decisions import ApprovalStatus, DecisionAction
from schemabridge.domain.reviews import (
    CanonicalPublication,
    CanonicalReviewDraft,
    ModelDescriptionEdit,
    PublicationApproval,
    PublicationConfirmation,
    PublicationItemKind,
    PublicationItemStatus,
    PublicationStatus,
    mapping_target_id,
)

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def test_approve_reject_and_mark_different_concept_create_immutable_versions() -> None:
    store = InMemoryReviewStore()
    draft = StartCanonicalReview(store).execute(build_customer_review_draft())
    decide = DecideCanonicalMapping(store)
    targets = [mapping_target_id(item.mapping) for item in draft.mappings]

    snapshot = decide.execute(
        draft.id,
        targets[0],
        DecisionAction.APPROVE,
        expected_revision=1,
        actor="steward@example.test",
        decided_at=NOW,
        rationale="CRM key evidence accepted.",
    )
    snapshot = decide.execute(
        draft.id,
        targets[1],
        DecisionAction.REJECT,
        expected_revision=2,
        actor="steward@example.test",
        decided_at=NOW,
        rationale="Legacy mapping rejected for this review.",
    )
    from schemabridge.domain.concepts import LogicalFieldRef

    snapshot = decide.execute(
        draft.id,
        targets[2],
        DecisionAction.MARK_DIFFERENT_CONCEPT,
        expected_revision=3,
        actor="steward@example.test",
        decided_at=NOW,
        rationale="The bank identifier belongs to a separate concept.",
        different_concept=LogicalFieldRef("AccountHolder.global_customer_key"),
    )

    assert snapshot.draft.revision == 4
    assert [decision.resulting_version for decision in snapshot.decisions] == [2, 3, 4]
    assert snapshot.draft.mappings[0].mapping.status is ApprovalStatus.APPROVED
    assert snapshot.draft.mappings[1].mapping.status is ApprovalStatus.REJECTED
    assert snapshot.draft.mappings[2].different_concept is not None


def test_edit_invalidates_existing_approvals() -> None:
    store, draft = _ready_review()
    edited = EditCanonicalReview(store).execute(
        draft.id,
        expected_revision=draft.revision,
        actor="steward@example.test",
        decided_at=NOW,
        rationale="Clarify the reviewed logical definition.",
        edit=ModelDescriptionEdit(
            description="Revised governed Customer context for the synthetic sources."
        ),
    )

    assert edited.draft.logical_model.status is ApprovalStatus.NEEDS_REVIEW
    assert {item.mapping.status for item in edited.draft.mappings} == {ApprovalStatus.NEEDS_REVIEW}
    with pytest.raises(ReviewWorkflowError) as raised:
        PrepareCanonicalPublication(store).execute(draft.id)
    assert raised.value.code is ReviewErrorCode.NOT_READY


def test_publish_requires_an_exact_separate_approval_and_is_idempotent() -> None:
    store, draft = _ready_review()
    publication = PrepareCanonicalPublication(store).execute(draft.id)
    writer = FakeCatalogWriteAdapter()
    audit_store = InMemoryPublicationAuditStore()
    use_case = PublishCanonicalReview(store, writer, audit_store)

    with pytest.raises(ReviewWorkflowError) as raised:
        use_case.execute(draft.id, None)  # type: ignore[arg-type]
    assert raised.value.code is ReviewErrorCode.APPROVAL_REQUIRED
    assert writer.publish_calls == 0

    approval = _approval(publication)
    first = use_case.execute(draft.id, approval)
    second = use_case.execute(draft.id, approval)

    assert first.status is PublicationStatus.PUBLISHED
    assert second.status is PublicationStatus.PUBLISHED
    assert {
        item.kind for item in second.items if item.status is PublicationItemStatus.PUBLISHED
    } == {
        PublicationItemKind.STRUCTURED_PROPERTY,
        PublicationItemKind.PHYSICAL_LINK,
    }
    assert len(store.list_publications(draft.id)) == 2
    assert all(item.decision_refs for item in first.items)
    records = audit_store.list_for_approval(approval.id)
    assert len(records) == 18
    assert {record.actor for record in records} == {approval.actor}
    assert {record.approved_at for record in records} == {approval.approved_at}
    assert {record.new_fingerprint for record in records} == {publication.fingerprint}


def test_partial_failure_is_recorded_but_never_read_as_current() -> None:
    store, draft = _ready_review()
    publication = PrepareCanonicalPublication(store).execute(draft.id)
    writer = FakeCatalogWriteAdapter(fail_at=PublicationItemKind.PHYSICAL_LINK)

    approval = _approval(publication)
    audit_store = InMemoryPublicationAuditStore()
    result = PublishCanonicalReview(store, writer, audit_store).execute(draft.id, approval)

    assert result.status is PublicationStatus.PARTIAL_FAILURE
    assert writer.read_context(publication) is None
    assert store.list_publications(draft.id) == (result,)
    records = audit_store.list_for_approval(approval.id)
    assert len(records) == 9
    assert {record.outcome.value for record in records} >= {"failed", "not_attempted"}


def test_fake_catalog_audits_each_targets_own_previous_fingerprint() -> None:
    _store, draft = _ready_review()
    publication = PrepareCanonicalPublication(_store).execute(draft.id)
    glossary_previous = "a" * 64
    marker_previous = "b" * 64
    backend = FakeCatalogWriteBackend(
        target_fingerprints={
            (PublicationItemKind.GLOSSARY_TERM, CUSTOMER_KEY_TERM_URN): glossary_previous,
            (PublicationItemKind.PUBLICATION_MARKER, LOGICAL_CUSTOMER_URN): marker_previous,
        }
    )

    result = FakeCatalogWriteAdapter(backend=backend).publish(
        publication,
        _approval(publication),
    )

    glossary = next(
        item
        for item in result.items
        if item.kind is PublicationItemKind.GLOSSARY_TERM and item.target == CUSTOMER_KEY_TERM_URN
    )
    marker = next(
        item for item in result.items if item.kind is PublicationItemKind.PUBLICATION_MARKER
    )
    assert glossary.audit_record.previous_fingerprint == glossary_previous
    assert marker.audit_record.previous_fingerprint == marker_previous


def test_fake_catalog_retry_skips_only_targets_completed_before_failure() -> None:
    store, draft = _ready_review()
    publication = PrepareCanonicalPublication(store).execute(draft.id)
    approval = _approval(publication)
    backend = FakeCatalogWriteBackend()

    first = FakeCatalogWriteAdapter(
        fail_at=PublicationItemKind.DECISION_DOCUMENT,
        backend=backend,
    ).publish(publication, approval)
    retry = FakeCatalogWriteAdapter(backend=backend).publish(publication, approval)

    assert first.status is PublicationStatus.PARTIAL_FAILURE
    assert retry.status is PublicationStatus.PUBLISHED
    assert [item.status for item in retry.items].count(PublicationItemStatus.ALREADY_CURRENT) == 3
    assert [item.status for item in retry.items].count(PublicationItemStatus.PUBLISHED) == 6
    already_current = tuple(
        item for item in retry.items if item.status is PublicationItemStatus.ALREADY_CURRENT
    )
    assert {item.audit_record.previous_fingerprint for item in already_current} == {
        publication.fingerprint
    }
    retried = tuple(item for item in retry.items if item.status is PublicationItemStatus.PUBLISHED)
    assert {item.kind for item in retried} == {
        PublicationItemKind.STRUCTURED_PROPERTY,
        PublicationItemKind.PHYSICAL_LINK,
        PublicationItemKind.DECISION_DOCUMENT,
        PublicationItemKind.PUBLICATION_MARKER,
    }
    assert {item.audit_record.previous_fingerprint for item in retried} == {None}


def test_fake_catalog_rejects_immutable_document_conflict_before_mutation() -> None:
    store, draft = _ready_review()
    publication = PrepareCanonicalPublication(store).execute(draft.id)
    document_target = (
        f"urn:li:document:schemabridge-{publication.draft_id}-v{publication.draft_version}"
    )
    original = {
        (PublicationItemKind.DECISION_DOCUMENT, document_target): "c" * 64,
    }
    backend = FakeCatalogWriteBackend(target_fingerprints=dict(original))

    with pytest.raises(ReviewWorkflowError) as raised:
        FakeCatalogWriteAdapter(backend=backend).publish(publication, _approval(publication))

    assert raised.value.code is ReviewErrorCode.CONFLICT
    assert backend.target_fingerprints == original


def test_rejected_mapping_is_never_in_publication_payload() -> None:
    store = InMemoryReviewStore()
    draft = StartCanonicalReview(store).execute(build_customer_review_draft())
    decide = DecideCanonicalMapping(store)
    targets = [mapping_target_id(item.mapping) for item in draft.mappings]
    actions = (
        DecisionAction.APPROVE,
        DecisionAction.REJECT,
        DecisionAction.APPROVE,
        DecisionAction.APPROVE,
    )
    for revision, (target, action) in enumerate(zip(targets, actions, strict=True), start=1):
        decide.execute(
            draft.id,
            target,
            action,
            expected_revision=revision,
            actor="steward@example.test",
            decided_at=NOW,
            rationale=f"Explicit {action.value} decision.",
        )

    publication = PrepareCanonicalPublication(store).execute(draft.id)
    assert targets[1] not in {mapping_target_id(mapping) for mapping in publication.mappings}
    assert targets[1] not in {decision.target_id for decision in publication.decisions}


def _ready_review() -> tuple[InMemoryReviewStore, CanonicalReviewDraft]:
    store = InMemoryReviewStore()
    draft = StartCanonicalReview(store).execute(build_customer_review_draft())
    decide = DecideCanonicalMapping(store)
    for revision, item in enumerate(draft.mappings, start=1):
        snapshot = decide.execute(
            draft.id,
            mapping_target_id(item.mapping),
            DecisionAction.APPROVE,
            expected_revision=revision,
            actor="steward@example.test",
            decided_at=NOW,
            rationale="Synthetic evidence reviewed and accepted.",
        )
    return store, snapshot.draft


def _approval(publication: CanonicalPublication) -> PublicationApproval:
    decision_ids = tuple(
        decision.id
        for decision in publication.decisions
        if decision.action is DecisionAction.APPROVE
    )
    return PublicationApproval(
        id=f"operator-{publication.draft_id}-v{publication.draft_version}",
        draft_id=publication.draft_id,
        draft_version=publication.draft_version,
        payload_fingerprint=publication.fingerprint,
        actor="steward@example.test",
        approved_at=NOW,
        decision_ids=decision_ids,
        confirmation=PublicationConfirmation.PUBLISH_APPROVED_CANONICAL_CONTEXT,
    )
