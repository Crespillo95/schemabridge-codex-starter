"""Approved M07 write-back against the pinned local synthetic DataHub Core."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import schemabridge.adapters.datahub.writeback as writeback_module
from schemabridge.adapters.datahub.writeback import DataHubCatalogWriteAdapter
from schemabridge.adapters.storage.publication_audit import SqlitePublicationAuditStore
from schemabridge.adapters.storage.reviews import InMemoryReviewStore
from schemabridge.application.canonical_review import (
    DecideCanonicalMapping,
    PrepareCanonicalPublication,
    PublishCanonicalReview,
    StartCanonicalReview,
)
from schemabridge.application.ports.reviews import ReviewErrorCode, ReviewWorkflowError
from schemabridge.application.review_demo import build_customer_review_draft
from schemabridge.domain.decisions import DecisionAction
from schemabridge.domain.reviews import (
    CanonicalPublication,
    PublicationApproval,
    PublicationConfirmation,
    PublicationItemKind,
    PublicationItemStatus,
    PublicationStatus,
    mapping_target_id,
)

pytestmark = pytest.mark.integration


def test_datahub_approved_write_is_idempotent_and_retrievable(tmp_path: Path) -> None:
    credential = Path(".local/datahub/writer.env")
    if not credential.is_file():
        pytest.skip("DataHub writer credentials are absent; run make datahub-provision-writer")
    store = InMemoryReviewStore()
    draft = StartCanonicalReview(store).execute(build_customer_review_draft())
    decide = DecideCanonicalMapping(store)
    for revision, item in enumerate(draft.mappings, start=1):
        snapshot = decide.execute(
            draft.id,
            mapping_target_id(item.mapping),
            DecisionAction.APPROVE,
            expected_revision=revision,
            actor="integration-steward",
            decided_at=datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
            rationale="M07 integration steward approved the synthetic mapping evidence.",
        )
    publication = PrepareCanonicalPublication(store).execute(snapshot.draft.id)
    approval = PublicationApproval(
        id=f"integration-{publication.draft_id}-v{publication.draft_version}",
        draft_id=publication.draft_id,
        draft_version=publication.draft_version,
        payload_fingerprint=publication.fingerprint,
        actor="integration-steward",
        approved_at=datetime(2026, 7, 21, 12, 5, tzinfo=UTC),
        decision_ids=tuple(decision.id for decision in publication.decisions),
        confirmation=PublicationConfirmation.PUBLISH_APPROVED_CANONICAL_CONTEXT,
    )
    writer = DataHubCatalogWriteAdapter.from_env_file(credential)
    audit_path = tmp_path / "publication-audit.db"
    use_case = PublishCanonicalReview(store, writer, SqlitePublicationAuditStore(audit_path))

    first = use_case.execute(publication.draft_id, approval)

    assert first.status in {PublicationStatus.PUBLISHED, PublicationStatus.ALREADY_CURRENT}
    context = writer.read_context(publication)
    assert context is not None
    assert {field.root for field in context.fields} == {
        "Customer.customer_key",
        "Customer.registration_date",
    }
    assert context.physical_links == (
        "bank.account_holders",
        "crm.customers",
        "legacy.client_master",
    )
    assert len(context.glossary_terms) == 2
    assert len(context.decision_refs) == 4

    replay = use_case.execute(publication.draft_id, approval)
    assert replay.status is PublicationStatus.PUBLISHED
    assert {
        item.kind for item in replay.items if item.status is PublicationItemStatus.PUBLISHED
    } == {
        PublicationItemKind.STRUCTURED_PROPERTY,
        PublicationItemKind.PHYSICAL_LINK,
    }
    assert all(
        item.audit_record.previous_fingerprint is None
        for item in replay.items
        if item.status is PublicationItemStatus.PUBLISHED
    )
    assert all(
        item.audit_record.previous_fingerprint == publication.fingerprint
        for item in replay.items
        if item.status is PublicationItemStatus.ALREADY_CURRENT
    )
    records = SqlitePublicationAuditStore(audit_path).list_for_approval(approval.id)
    assert len(records) == 18
    assert {record.actor for record in records} == {approval.actor}
    assert {record.approved_at for record in records} == {approval.approved_at}
    assert {record.new_fingerprint for record in records} == {publication.fingerprint}


def test_datahub_canonical_retry_is_per_target_and_version_document_is_immutable() -> None:
    credential = Path(".local/datahub/writer.env")
    if not credential.is_file():
        pytest.skip("DataHub writer credentials are absent; run make datahub-provision-writer")
    base_publication, _base_approval = _approved_publication()
    publication = CanonicalPublication.create(
        draft_id=base_publication.draft_id,
        draft_version=base_publication.draft_version + 1000,
        logical_model=base_publication.logical_model,
        fields=base_publication.fields,
        mappings=base_publication.mappings,
        decisions=base_publication.decisions,
    )
    approval = _approval(publication, "integration-canonical-target-retry")
    writer = _FailOnceDocumentWriter.from_env_file(credential)

    first = writer.publish(publication, approval)
    retry = writer.publish(publication, approval)

    assert first.status is PublicationStatus.PARTIAL_FAILURE
    assert retry.status is PublicationStatus.PUBLISHED
    marker = next(
        item for item in retry.items if item.kind is PublicationItemKind.PUBLICATION_MARKER
    )
    document = next(
        item for item in retry.items if item.kind is PublicationItemKind.DECISION_DOCUMENT
    )
    assert marker.status is PublicationItemStatus.PUBLISHED
    assert marker.audit_record.previous_fingerprint != publication.fingerprint
    assert document.status in {
        PublicationItemStatus.PUBLISHED,
        PublicationItemStatus.ALREADY_CURRENT,
    }
    assert document.audit_record.previous_fingerprint in {
        None,
        publication.fingerprint,
    }
    assert all(
        item.audit_record.previous_fingerprint is None
        for item in retry.items
        if item.kind in {PublicationItemKind.STRUCTURED_PROPERTY, PublicationItemKind.PHYSICAL_LINK}
    )
    assert writer.read_context(publication) is not None

    conflicting_model = base_publication.logical_model.model_copy(
        update={"description": "Conflicting immutable canonical integration payload."}
    )
    conflicting = CanonicalPublication.create(
        draft_id=publication.draft_id,
        draft_version=publication.draft_version,
        logical_model=conflicting_model,
        fields=publication.fields,
        mappings=publication.mappings,
        decisions=publication.decisions,
    )
    with pytest.raises(ReviewWorkflowError) as raised:
        writer.publish(conflicting, _approval(conflicting, "integration-canonical-conflict"))
    assert raised.value.code is ReviewErrorCode.CONFLICT
    assert writer.read_context(publication) is not None


class _FailOnceDocumentWriter(DataHubCatalogWriteAdapter):
    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self._fail_document_once = True

    def _target_state(
        self,
        client: Any,
        publication: CanonicalPublication,
        kind: PublicationItemKind,
        target: str,
    ) -> Any:
        state = super()._target_state(client, publication, kind, target)
        if (
            kind is PublicationItemKind.DECISION_DOCUMENT
            and self._fail_document_once
            and state.previous_fingerprint in {None, publication.fingerprint}
        ):
            return writeback_module._TargetState(None, False)
        return state

    def _actions(self, client: Any, publication: CanonicalPublication) -> tuple[Any, ...]:
        actions = super()._actions(client, publication)
        wrapped: list[Any] = []
        for kind, target, action in actions:
            if kind is PublicationItemKind.DECISION_DOCUMENT:

                def fail_once(action: Any = action) -> None:
                    if self._fail_document_once:
                        self._fail_document_once = False
                        raise RuntimeError("injected versioned document failure")
                    action()

                wrapped.append((kind, target, fail_once))
            else:
                wrapped.append((kind, target, action))
        return tuple(wrapped)


def _approved_publication() -> tuple[CanonicalPublication, PublicationApproval]:
    store = InMemoryReviewStore()
    draft = StartCanonicalReview(store).execute(build_customer_review_draft())
    decide = DecideCanonicalMapping(store)
    for revision, item in enumerate(draft.mappings, start=1):
        snapshot = decide.execute(
            draft.id,
            mapping_target_id(item.mapping),
            DecisionAction.APPROVE,
            expected_revision=revision,
            actor="integration-steward",
            decided_at=datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
            rationale="Integration steward approved the synthetic mapping evidence.",
        )
    publication = PrepareCanonicalPublication(store).execute(snapshot.draft.id)
    return publication, _approval(publication, "integration-canonical-base")


def _approval(publication: CanonicalPublication, approval_id: str) -> PublicationApproval:
    return PublicationApproval(
        id=approval_id,
        draft_id=publication.draft_id,
        draft_version=publication.draft_version,
        payload_fingerprint=publication.fingerprint,
        actor="integration-steward",
        approved_at=datetime(2026, 7, 21, 12, 5, tzinfo=UTC),
        decision_ids=tuple(decision.id for decision in publication.decisions),
        confirmation=PublicationConfirmation.PUBLISH_APPROVED_CANONICAL_CONTEXT,
    )
