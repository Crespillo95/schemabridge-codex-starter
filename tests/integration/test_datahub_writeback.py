"""Approved M07 write-back against the pinned local synthetic DataHub Core."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from schemabridge.adapters.datahub.writeback import DataHubCatalogWriteAdapter
from schemabridge.adapters.storage.reviews import InMemoryReviewStore
from schemabridge.application.canonical_review import (
    DecideCanonicalMapping,
    PrepareCanonicalPublication,
    PublishCanonicalReview,
    StartCanonicalReview,
)
from schemabridge.application.review_demo import build_customer_review_draft
from schemabridge.domain.decisions import DecisionAction
from schemabridge.domain.reviews import (
    PublicationApproval,
    PublicationConfirmation,
    PublicationStatus,
    mapping_target_id,
)

pytestmark = pytest.mark.integration


def test_datahub_approved_write_is_idempotent_and_retrievable() -> None:
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
    use_case = PublishCanonicalReview(store, writer)

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
    assert replay.status is PublicationStatus.ALREADY_CURRENT
