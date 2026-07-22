from __future__ import annotations

from datetime import UTC, datetime

import pytest

from schemabridge.adapters.storage.reviews import SqliteReviewStore
from schemabridge.application.canonical_review import DecideCanonicalMapping, StartCanonicalReview
from schemabridge.application.ports.reviews import ReviewErrorCode, ReviewWorkflowError
from schemabridge.application.review_demo import build_customer_review_draft
from schemabridge.domain.decisions import DecisionAction
from schemabridge.domain.reviews import mapping_target_id


def test_sqlite_store_persists_versioned_decisions_and_detects_stale_writes(tmp_path) -> None:
    path = tmp_path / "review.db"
    store = SqliteReviewStore(path)
    draft = StartCanonicalReview(store).execute(build_customer_review_draft())
    target = mapping_target_id(draft.mappings[0].mapping)
    snapshot = DecideCanonicalMapping(store).execute(
        draft.id,
        target,
        DecisionAction.APPROVE,
        expected_revision=1,
        actor="steward@example.test",
        decided_at=datetime(2026, 7, 21, tzinfo=UTC),
        rationale="Approved after evidence review.",
    )

    reopened = SqliteReviewStore(path)
    assert reopened.load(draft.id) == snapshot.draft
    assert reopened.list_decisions(draft.id) == snapshot.decisions
    with pytest.raises(ReviewWorkflowError) as raised:
        DecideCanonicalMapping(reopened).execute(
            draft.id,
            mapping_target_id(draft.mappings[1].mapping),
            DecisionAction.APPROVE,
            expected_revision=1,
            actor="steward@example.test",
            decided_at=datetime(2026, 7, 21, tzinfo=UTC),
            rationale="Stale decision must be rejected.",
        )
    assert raised.value.code is ReviewErrorCode.CONFLICT
