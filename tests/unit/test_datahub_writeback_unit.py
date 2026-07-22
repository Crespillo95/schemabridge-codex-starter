"""Concrete DataHub writer approval and credential boundaries."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from schemabridge.adapters.datahub.canonical_urns import DECISION_PROPERTY_URN
from schemabridge.adapters.datahub.decision_property import ensure_decision_property
from schemabridge.adapters.datahub.writeback import (
    DataHubCatalogWriteAdapter,
    DataHubWriteConfig,
)
from schemabridge.application.ports.reviews import ReviewErrorCode, ReviewWorkflowError


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
    from schemabridge.adapters.storage.reviews import InMemoryReviewStore
    from schemabridge.application.canonical_review import (
        DecideCanonicalMapping,
        PrepareCanonicalPublication,
    )
    from schemabridge.application.review_demo import build_customer_review_draft
    from schemabridge.domain.decisions import DecisionAction
    from schemabridge.domain.reviews import mapping_target_id

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
