from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from schemabridge.domain.decisions import (
    ApprovalStatus,
    DecisionAction,
    DecisionRecord,
    DecisionTargetType,
)
from schemabridge.domain.reviews import PublicationApproval, PublicationConfirmation


def test_decision_action_and_status_cannot_contradict() -> None:
    with pytest.raises(ValidationError):
        DecisionRecord(
            id="contradictory",
            target_type=DecisionTargetType.COLUMN_MAPPING,
            target_id="crm.customers.customer_id->Customer.customer_key",
            action=DecisionAction.APPROVE,
            status=ApprovalStatus.NEEDS_REVIEW,
            actor="steward@example.test",
            decided_at=datetime(2026, 7, 21, tzinfo=UTC),
            source_version=1,
            resulting_version=2,
            rationale="This invalid record must not parse.",
        )


def test_publication_confirmation_is_a_closed_exact_value() -> None:
    with pytest.raises(ValidationError):
        PublicationApproval(
            id="approval",
            draft_id="customer-canonical",
            draft_version=5,
            payload_fingerprint="a" * 64,
            actor="steward@example.test",
            approved_at=datetime(2026, 7, 21, tzinfo=UTC),
            decision_ids=("decision",),
            confirmation="yes",  # type: ignore[arg-type]
        )
    assert (
        PublicationConfirmation.PUBLISH_APPROVED_CANONICAL_CONTEXT.value
        == "publish-approved-canonical-context"
    )
