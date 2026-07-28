"""Explicit join review, versioning, publication approval, and process-reload behavior."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from schemabridge.adapters.datahub.fake_join_context import (
    FakeJoinContextAdapter,
    FakeJoinContextBackend,
)
from schemabridge.adapters.datahub.join_context import (
    DataHubJoinContextAdapter,
    DataHubJoinContextConfig,
)
from schemabridge.adapters.storage.publication_audit import InMemoryPublicationAuditStore
from schemabridge.adapters.storage.relationships import (
    InMemoryJoinReviewStore,
    SqliteJoinReviewStore,
)
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
from schemabridge.application.ports.relationships import (
    RelationshipErrorCode,
    RelationshipWorkflowError,
)
from schemabridge.domain.decisions import ApprovalStatus, DecisionAction
from schemabridge.domain.join_reviews import (
    JoinContractPublication,
    JoinPublicationApproval,
    JoinPublicationConfirmation,
    JoinPublicationItemKind,
    JoinPublicationItemStatus,
    JoinPublicationStatus,
)
from schemabridge.domain.joins import (
    DeclaredRelationship,
    RelationshipProfile,
    score_join_candidate,
)
from schemabridge.domain.publication_audit import PublicationAuditOutcome

NOW = datetime(2026, 7, 21, 15, 0, tzinfo=UTC)


def test_approvals_create_versioned_contracts_and_exact_publication() -> None:
    store, draft = _ready_review()
    publication = PrepareJoinPublication(store).execute(draft.id)
    backend = FakeJoinContextBackend()
    writer = FakeJoinContextAdapter(backend)
    audit_store = InMemoryPublicationAuditStore()
    use_case = PublishJoinContracts(store, writer, audit_store)

    with pytest.raises(RelationshipWorkflowError) as raised:
        use_case.execute(draft.id, None)  # type: ignore[arg-type]
    assert raised.value.code is RelationshipErrorCode.APPROVAL_REQUIRED
    assert writer.publish_calls == 0

    approval = _approval(publication)
    first = use_case.execute(draft.id, approval)
    second = use_case.execute(draft.id, approval)

    assert first.status is JoinPublicationStatus.PUBLISHED
    assert second.status is JoinPublicationStatus.ALREADY_CURRENT
    assert {contract.cardinality.value for contract in publication.contract_set.contracts} == {
        "one_to_many",
        "many_to_one",
    }
    assert all(contract.approval_decision_id for contract in publication.contract_set.contracts)
    assert all(
        contract.left_key.transformation_plan.steps
        for contract in publication.contract_set.contracts
    )

    new_process = FakeJoinContextAdapter(backend)
    loaded = new_process.load_current()
    assert loaded is not None
    assert loaded.contract_set == publication.contract_set
    records = audit_store.list_for_approval(approval.id)
    assert len(records) == 4
    assert {record.actor for record in records} == {approval.actor}
    assert {record.new_fingerprint for record in records} == {publication.fingerprint}


def test_join_review_store_survives_a_new_sqlite_instance(tmp_path: object) -> None:
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    path = tmp_path / "join-reviews.sqlite3"
    store = SqliteJoinReviewStore(path)
    draft = StartJoinReview(store).execute(_draft())
    snapshot = DecideJoinCandidate(store).execute(
        draft.id,
        draft.joins[0].candidate.proposal.id,
        DecisionAction.APPROVE,
        expected_revision=1,
        actor="steward@example.test",
        decided_at=NOW,
        rationale="Reviewed overlap and fanout evidence.",
    )

    reloaded = SqliteJoinReviewStore(path).load(draft.id)
    assert reloaded == snapshot.draft
    assert reloaded is not None
    assert reloaded.joins[0].status is ApprovalStatus.APPROVED


def test_concrete_datahub_join_writer_checks_approval_before_network() -> None:
    publication = PrepareJoinPublication(_ready_review()[0]).execute("north-star-joins")
    writer = DataHubJoinContextAdapter(
        DataHubJoinContextConfig(
            server="http://127.0.0.1:1",
            token="not-logged",
            actor_urn="urn:li:corpuser:schemabridge",
        )
    )

    with pytest.raises(RelationshipWorkflowError) as raised:
        writer.publish(publication, None)  # type: ignore[arg-type]
    assert raised.value.code is RelationshipErrorCode.APPROVAL_REQUIRED


def test_concrete_datahub_join_writer_rejects_immutable_version_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = PrepareJoinPublication(_ready_review()[0]).execute("north-star-joins")
    writer = DataHubJoinContextAdapter(
        DataHubJoinContextConfig(
            server="http://datahub.invalid",
            token="not-logged",
            actor_urn="urn:li:corpuser:schemabridge",
        )
    )
    monkeypatch.setattr(writer, "_load_current", lambda *, require_version: None)
    monkeypatch.setattr(writer, "_client", object)
    monkeypatch.setattr(
        writer,
        "_document_fingerprint",
        lambda _client, target: (
            "f" * 64 if target.endswith(f"v{publication.draft_version}") else None
        ),
    )

    with pytest.raises(RelationshipWorkflowError) as raised:
        writer.publish(publication, _approval(publication))

    assert raised.value.code is RelationshipErrorCode.CONFLICT


def test_concrete_datahub_join_writer_rejects_unvalidated_current_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = PrepareJoinPublication(_ready_review()[0]).execute("north-star-joins")
    writer = DataHubJoinContextAdapter(
        DataHubJoinContextConfig(
            server="http://datahub.invalid",
            token="not-logged",
            actor_urn="urn:li:corpuser:schemabridge",
        )
    )
    monkeypatch.setattr(writer, "_load_current", lambda *, require_version: None)
    monkeypatch.setattr(writer, "_client", object)
    monkeypatch.setattr(
        writer,
        "_document_fingerprint",
        lambda _client, target: (
            publication.fingerprint if target.endswith("join-contracts-current") else None
        ),
    )

    with pytest.raises(RelationshipWorkflowError) as raised:
        writer.publish(publication, _approval(publication))

    assert raised.value.code is RelationshipErrorCode.CATALOG_INVALID_RESPONSE


def test_concrete_datahub_join_writer_requires_post_write_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = PrepareJoinPublication(_ready_review()[0]).execute("north-star-joins")
    writer = DataHubJoinContextAdapter(
        DataHubJoinContextConfig(
            server="http://datahub.invalid",
            token="not-logged",
            actor_urn="urn:li:corpuser:schemabridge",
        )
    )
    monkeypatch.setattr(writer, "_load_current", lambda *, require_version: None)
    monkeypatch.setattr(writer, "_client", object)
    monkeypatch.setattr(writer, "_document_fingerprint", lambda _client, _target: None)
    monkeypatch.setattr(writer, "_upsert_document", lambda *_args, **_kwargs: None)

    result = writer.publish(publication, _approval(publication))

    assert result.status is JoinPublicationStatus.PARTIAL_FAILURE
    assert result.items[0].status is JoinPublicationItemStatus.FAILED
    assert result.items[0].audit_record.outcome is PublicationAuditOutcome.FAILED
    assert result.items[1].status is JoinPublicationItemStatus.NOT_ATTEMPTED


def test_concrete_datahub_join_reader_requires_immutable_version_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = PrepareJoinPublication(_ready_review()[0]).execute("north-star-joins")
    related_assets = tuple(
        SimpleNamespace(
            asset=f"urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge.{dataset},PROD)"
        )
        for dataset in sorted(
            {
                key.physical_field.root.rsplit(".", 1)[0]
                for contract in publication.contract_set.contracts
                for key in (contract.left_key, contract.right_key)
            }
        )
    )
    current_document = SimpleNamespace(
        customProperties={
            "schemabridge.joinPublication": publication.model_dump_json(),
            "schemabridge.joinFingerprint": publication.fingerprint,
        },
        relatedAssets=related_assets,
    )

    class MissingVersionGraph:
        def get_aspect(self, urn: str, _aspect: object) -> object | None:
            return current_document if urn.endswith("join-contracts-current") else None

    writer = DataHubJoinContextAdapter(
        DataHubJoinContextConfig(
            server="http://datahub.invalid",
            token="not-logged",
            actor_urn="urn:li:corpuser:schemabridge",
        )
    )
    monkeypatch.setattr(writer, "_verify_runtime_identity", lambda: None)
    monkeypatch.setattr(writer, "_client", lambda: SimpleNamespace(_graph=MissingVersionGraph()))

    with pytest.raises(RelationshipWorkflowError) as raised:
        writer.load_current()

    assert raised.value.code is RelationshipErrorCode.CATALOG_INVALID_RESPONSE


def test_partial_join_retry_reports_each_targets_actual_previous_state() -> None:
    store, draft = _ready_review()
    publication = PrepareJoinPublication(store).execute(draft.id)
    approval = _approval(publication)
    backend = FakeJoinContextBackend()
    audit_store = InMemoryPublicationAuditStore()

    first = PublishJoinContracts(
        store,
        FakeJoinContextAdapter(
            backend,
            fail_at=JoinPublicationItemKind.CURRENT_CONTEXT_MARKER,
        ),
        audit_store,
    ).execute(draft.id, approval)
    retry = PublishJoinContracts(
        store,
        FakeJoinContextAdapter(backend),
        audit_store,
    ).execute(draft.id, approval)

    assert first.status is JoinPublicationStatus.PARTIAL_FAILURE
    assert retry.status is JoinPublicationStatus.PUBLISHED
    retry_by_kind = {item.kind: item for item in retry.items}
    versioned = retry_by_kind[JoinPublicationItemKind.VERSIONED_DECISION_DOCUMENT]
    marker = retry_by_kind[JoinPublicationItemKind.CURRENT_CONTEXT_MARKER]
    assert versioned.status is JoinPublicationItemStatus.ALREADY_CURRENT
    assert versioned.audit_record.previous_fingerprint == publication.fingerprint
    assert marker.status is JoinPublicationItemStatus.PUBLISHED
    assert marker.audit_record.previous_fingerprint is None


def test_fake_join_writer_rejects_same_version_with_different_content() -> None:
    store, draft = _ready_review()
    publication = PrepareJoinPublication(store).execute(draft.id)
    conflicting = JoinContractPublication.create(
        draft_id="conflicting-join-review",
        draft_version=publication.draft_version,
        contracts=publication.contract_set.contracts,
        decisions=publication.decisions,
    )
    backend = FakeJoinContextBackend()
    writer = FakeJoinContextAdapter(backend)

    writer.publish(publication, _approval(publication))
    with pytest.raises(RelationshipWorkflowError) as raised:
        writer.publish(conflicting, _approval(conflicting))

    assert raised.value.code is RelationshipErrorCode.CONFLICT


def _draft() -> object:
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
    report = JoinDiscoveryReport(
        catalog_source="fake",
        candidates=tuple(
            score_join_candidate(proposal, profile)
            for proposal, profile in zip(proposals, profiles, strict=True)
        ),
    )
    return build_join_review_draft(report)


def _ready_review() -> tuple[InMemoryJoinReviewStore, object]:
    from schemabridge.domain.join_reviews import JoinReviewDraft

    store = InMemoryJoinReviewStore()
    draft = StartJoinReview(store).execute(JoinReviewDraft.model_validate(_draft()))
    decider = DecideJoinCandidate(store)
    for revision, item in enumerate(draft.joins, start=1):
        snapshot = decider.execute(
            draft.id,
            item.candidate.proposal.id,
            DecisionAction.APPROVE,
            expected_revision=revision,
            actor="steward@example.test",
            decided_at=NOW,
            rationale="Synthetic relationship evidence and fanout policy accepted.",
        )
    return store, snapshot.draft


def _approval(publication: object) -> JoinPublicationApproval:
    from schemabridge.domain.join_reviews import JoinContractPublication

    resolved = JoinContractPublication.model_validate(publication)
    return JoinPublicationApproval(
        id=f"operator-{resolved.draft_id}-v{resolved.draft_version}",
        draft_id=resolved.draft_id,
        draft_version=resolved.draft_version,
        payload_fingerprint=resolved.fingerprint,
        actor="steward@example.test",
        approved_at=NOW,
        decision_ids=tuple(decision.id for decision in resolved.decisions),
        confirmation=JoinPublicationConfirmation.PUBLISH_APPROVED_JOIN_CONTRACTS,
    )
