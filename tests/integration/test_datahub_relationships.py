"""Approval-gated M08 join persistence and fresh-process reuse in local DataHub."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from schemabridge.adapters.datahub.join_context import DataHubJoinContextAdapter
from schemabridge.adapters.storage.publication_audit import SqlitePublicationAuditStore
from schemabridge.adapters.storage.relationships import InMemoryJoinReviewStore
from schemabridge.application.join_demo import (
    build_join_review_draft,
    build_north_star_join_proposals,
)
from schemabridge.application.join_discovery import (
    DecideJoinCandidate,
    JoinDiscoveryReport,
    PrepareJoinPublication,
    PublishJoinContracts,
    StartJoinReview,
)
from schemabridge.domain.decisions import DecisionAction
from schemabridge.domain.join_reviews import (
    JoinPublicationApproval,
    JoinPublicationConfirmation,
    JoinPublicationStatus,
)
from schemabridge.domain.joins import (
    DeclaredRelationship,
    RelationshipProfile,
    score_join_candidate,
)

pytestmark = pytest.mark.integration
NOW = datetime(2026, 7, 21, 16, 0, tzinfo=UTC)


def test_approved_join_contracts_persist_and_load_in_a_new_process(tmp_path: Path) -> None:
    credential = Path(".local/datahub/writer.env")
    if not credential.is_file():
        pytest.skip("DataHub writer credentials are absent; run make datahub-provision-writer")
    proposals = build_north_star_join_proposals()
    profiles = (
        RelationshipProfile(
            left_row_count=7,
            right_row_count=9,
            left_null_count=0,
            right_null_count=1,
            left_invalid_count=0,
            right_invalid_count=2,
            left_distinct_valid=7,
            right_distinct_valid=5,
            matching_distinct_keys=5,
            left_max_multiplicity=1,
            right_max_multiplicity=2,
        ),
        RelationshipProfile(
            left_row_count=9,
            right_row_count=9,
            left_null_count=0,
            right_null_count=0,
            left_invalid_count=0,
            right_invalid_count=0,
            left_distinct_valid=9,
            right_distinct_valid=9,
            matching_distinct_keys=9,
            left_max_multiplicity=1,
            right_max_multiplicity=1,
            declared_relationship=DeclaredRelationship.LEFT_FOREIGN_KEY_TO_RIGHT,
        ),
    )
    draft = build_join_review_draft(
        JoinDiscoveryReport(
            catalog_source="integration:synthetic-aggregate-evidence",
            candidates=tuple(
                score_join_candidate(proposal, profile)
                for proposal, profile in zip(proposals, profiles, strict=True)
            ),
        )
    )
    store = InMemoryJoinReviewStore()
    draft = StartJoinReview(store).execute(draft)
    decider = DecideJoinCandidate(store)
    for revision, item in enumerate(draft.joins, start=1):
        snapshot = decider.execute(
            draft.id,
            item.candidate.proposal.id,
            DecisionAction.APPROVE,
            expected_revision=revision,
            actor="integration-steward",
            decided_at=NOW,
            rationale="M08 integration steward accepted the synthetic relationship evidence.",
        )
    publication = PrepareJoinPublication(store).execute(snapshot.draft.id)
    approval = JoinPublicationApproval(
        id=f"integration-{publication.draft_id}-v{publication.draft_version}",
        draft_id=publication.draft_id,
        draft_version=publication.draft_version,
        payload_fingerprint=publication.fingerprint,
        actor="integration-steward",
        approved_at=NOW,
        decision_ids=tuple(decision.id for decision in publication.decisions),
        confirmation=JoinPublicationConfirmation.PUBLISH_APPROVED_JOIN_CONTRACTS,
    )
    writer = DataHubJoinContextAdapter.from_env_file(credential)
    audit_path = tmp_path / "publication-audit.db"

    first = PublishJoinContracts(store, writer, SqlitePublicationAuditStore(audit_path)).execute(
        publication.draft_id, approval
    )

    assert first.status in {
        JoinPublicationStatus.PUBLISHED,
        JoinPublicationStatus.ALREADY_CURRENT,
    }
    fresh_process = DataHubJoinContextAdapter.from_env_file(credential)
    loaded = fresh_process.load_current()
    assert loaded is not None
    assert loaded.contract_set == publication.contract_set
    assert {contract.id for contract in loaded.contract_set.contracts} == {
        "customer_to_account_holder",
        "account_holder_to_account",
    }
    assert len(loaded.related_asset_urns) == 3

    replay = PublishJoinContracts(
        store, fresh_process, SqlitePublicationAuditStore(audit_path)
    ).execute(publication.draft_id, approval)
    assert replay.status is JoinPublicationStatus.ALREADY_CURRENT
    records = SqlitePublicationAuditStore(audit_path).list_for_approval(approval.id)
    assert len(records) == 4
    assert {record.actor for record in records} == {approval.actor}
    assert {record.approved_at for record in records} == {approval.approved_at}
    assert {record.new_fingerprint for record in records} == {publication.fingerprint}
