"""Concrete DataHub writer approval and credential boundaries."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import schemabridge.adapters.datahub.writeback as writeback_module
from schemabridge.adapters.datahub.canonical_urns import DECISION_PROPERTY_URN
from schemabridge.adapters.datahub.decision_property import ensure_decision_property
from schemabridge.adapters.datahub.writeback import (
    DataHubCatalogWriteAdapter,
    DataHubWriteConfig,
)
from schemabridge.application.ports.reviews import ReviewErrorCode, ReviewWorkflowError
from schemabridge.domain.reviews import (
    PublicationItemKind,
    PublicationItemStatus,
    PublicationStatus,
)


def test_datahub_writer_rejects_group_readable_secret_file(tmp_path: Path) -> None:
    credential = tmp_path / "writer.env"
    credential.write_text(
        "DATAHUB_GMS_URL=http://127.0.0.1:8080\n"
        "DATAHUB_GMS_TOKEN=synthetic-test-token\n"
        "DATAHUB_WRITER_ACTOR_URN=urn:li:corpuser:test\n"
    )
    credential.chmod(0o640)

    with pytest.raises(ReviewWorkflowError) as raised:
        DataHubCatalogWriteAdapter.from_env_file(credential)
    assert raised.value.code is ReviewErrorCode.CATALOG_PERMISSION_DENIED


def test_concrete_datahub_writer_checks_approval_before_network_access() -> None:
    publication, _approval = _approved_publication()
    writer = DataHubCatalogWriteAdapter(
        DataHubWriteConfig(
            server="http://127.0.0.1:1",
            token="synthetic-test-token",
            actor_urn="urn:li:corpuser:test",
        )
    )
    with pytest.raises(ReviewWorkflowError) as raised:
        writer.publish(publication, None)  # type: ignore[arg-type]
    assert raised.value.code is ReviewErrorCode.APPROVAL_REQUIRED


def test_concrete_writer_rejects_immutable_document_conflict_before_any_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication, approval = _approved_publication()
    writer = _isolated_writer(monkeypatch)
    mutations: list[tuple[PublicationItemKind, str]] = []
    actions = tuple(
        (
            kind,
            target,
            lambda kind=kind, target=target: mutations.append((kind, target)),
        )
        for kind, target in writeback_module._targets(publication)
    )
    monkeypatch.setattr(writer, "_actions", lambda _client, _publication: actions)
    monkeypatch.setattr(
        writer,
        "_target_state",
        lambda _client, _publication, kind, _target: writeback_module._TargetState(
            previous_fingerprint=(
                "d" * 64 if kind is PublicationItemKind.DECISION_DOCUMENT else None
            ),
            is_current=False,
        ),
    )

    with pytest.raises(ReviewWorkflowError) as raised:
        writer.publish(publication, approval)

    assert raised.value.code is ReviewErrorCode.CONFLICT
    assert mutations == []


def test_concrete_writer_retries_per_target_without_copying_marker_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication, approval = _approved_publication()
    writer = _isolated_writer(monkeypatch)
    targets = writeback_module._targets(publication)
    marker_previous = "a" * 64
    glossary_previous = "b" * 64
    states = {
        (kind, target): writeback_module._TargetState(None, False) for kind, target in targets
    }
    marker_key = next(key for key in states if key[0] is PublicationItemKind.PUBLICATION_MARKER)
    glossary_key = next(key for key in states if key[0] is PublicationItemKind.GLOSSARY_TERM)
    states[marker_key] = writeback_module._TargetState(marker_previous, False)
    states[glossary_key] = writeback_module._TargetState(glossary_previous, False)
    fail_document_once = True

    def action(kind: PublicationItemKind, target: str) -> None:
        nonlocal fail_document_once
        if kind is PublicationItemKind.DECISION_DOCUMENT and fail_document_once:
            fail_document_once = False
            raise RuntimeError("injected document failure")
        if kind in {
            PublicationItemKind.STRUCTURED_PROPERTY,
            PublicationItemKind.PHYSICAL_LINK,
        }:
            states[(kind, target)] = writeback_module._TargetState(None, True)
        else:
            states[(kind, target)] = writeback_module._TargetState(
                publication.fingerprint,
                True,
            )

    monkeypatch.setattr(
        writer,
        "_actions",
        lambda _client, _publication: tuple(
            (
                kind,
                target,
                lambda kind=kind, target=target: action(kind, target),
            )
            for kind, target in targets
        ),
    )
    monkeypatch.setattr(
        writer,
        "_target_state",
        lambda _client, _publication, kind, target: states[(kind, target)],
    )

    first = writer.publish(publication, approval)
    retry = writer.publish(publication, approval)

    assert first.status is PublicationStatus.PARTIAL_FAILURE
    assert retry.status is PublicationStatus.PUBLISHED
    marker = next(
        item for item in retry.items if item.kind is PublicationItemKind.PUBLICATION_MARKER
    )
    glossary = next(item for item in retry.items if (item.kind, item.target) == glossary_key)
    assert marker.status is PublicationItemStatus.PUBLISHED
    assert marker.audit_record.previous_fingerprint == marker_previous
    assert glossary.status is PublicationItemStatus.ALREADY_CURRENT
    assert glossary.audit_record.previous_fingerprint == publication.fingerprint
    assert all(
        item.audit_record.previous_fingerprint is None
        for item in retry.items
        if item.kind in {PublicationItemKind.STRUCTURED_PROPERTY, PublicationItemKind.PHYSICAL_LINK}
    )


def test_decision_property_is_created_without_canonical_publication_ordering() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        calls.append((query, variables))
        if "query Property" in query:
            return {"structuredProperty": None}
        return {"createStructuredProperty": {"urn": DECISION_PROPERTY_URN}}

    ensure_decision_property(graphql)

    assert len(calls) == 2
    assert calls[0][1] == {"urn": DECISION_PROPERTY_URN}
    assert calls[1][1]["input"] == {
        "id": "io.schemabridge.decisionRef",
        "qualifiedName": "io.schemabridge.decisionRef",
        "displayName": "SchemaBridge decision reference",
        "description": "Immutable SchemaBridge decision identifiers and versions.",
        "valueType": "urn:li:dataType:datahub.string",
        "cardinality": "MULTIPLE",
        "entityTypes": [
            "urn:li:entityType:datahub.dataset",
            "urn:li:entityType:datahub.glossaryTerm",
            "urn:li:entityType:datahub.document",
        ],
    }


def _isolated_writer(monkeypatch: pytest.MonkeyPatch) -> DataHubCatalogWriteAdapter:
    writer = DataHubCatalogWriteAdapter(
        DataHubWriteConfig(
            server="http://127.0.0.1:1",
            token="synthetic-test-token",
            actor_urn="urn:li:corpuser:test",
        )
    )
    monkeypatch.setattr(writer, "_verify_runtime_identity", lambda: None)
    monkeypatch.setattr(writer, "_client", lambda: object())
    return writer


def _approved_publication() -> tuple[Any, Any]:
    from schemabridge.adapters.storage.reviews import InMemoryReviewStore
    from schemabridge.application.canonical_review import (
        DecideCanonicalMapping,
        PrepareCanonicalPublication,
    )
    from schemabridge.application.review_demo import build_customer_review_draft
    from schemabridge.domain.decisions import DecisionAction
    from schemabridge.domain.reviews import (
        PublicationApproval,
        PublicationConfirmation,
        mapping_target_id,
    )

    draft = build_customer_review_draft()
    store = InMemoryReviewStore()
    store.create(draft)
    decide = DecideCanonicalMapping(store)
    for revision, item in enumerate(draft.mappings, start=1):
        decide.execute(
            draft.id,
            mapping_target_id(item.mapping),
            DecisionAction.APPROVE,
            expected_revision=revision,
            actor="test-steward",
            decided_at=datetime(2026, 7, 21, tzinfo=UTC),
            rationale="Explicitly approved for this isolated writer boundary test.",
        )
    publication = PrepareCanonicalPublication(store).execute(draft.id)
    approval = PublicationApproval(
        id="unit-canonical-approval",
        draft_id=publication.draft_id,
        draft_version=publication.draft_version,
        payload_fingerprint=publication.fingerprint,
        actor="test-steward",
        approved_at=datetime(2026, 7, 21, 1, tzinfo=UTC),
        decision_ids=tuple(decision.id for decision in publication.decisions),
        confirmation=PublicationConfirmation.PUBLISH_APPROVED_CANONICAL_CONTEXT,
    )
    return publication, approval
