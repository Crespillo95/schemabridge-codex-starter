"""Uniform, durable publication-audit contract tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from schemabridge.adapters.storage.publication_audit import SqlitePublicationAuditStore
from schemabridge.application.ports.publication_audit import PublicationAuditStoreError
from schemabridge.domain.publication_audit import (
    PublicationAuditOutcome,
    PublicationFamily,
    PublicationTargetAuditRecord,
    validate_publication_audit_binding,
)

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def _record(
    *,
    outcome: PublicationAuditOutcome = PublicationAuditOutcome.SUCCEEDED,
    previous: str | None = None,
    reason_code: str | None = None,
) -> PublicationTargetAuditRecord:
    return PublicationTargetAuditRecord(
        family=PublicationFamily.CANONICAL,
        operation="logical_model",
        target="urn:li:dataset:synthetic-customer",
        approval_id="approval-1",
        actor="steward@example.test",
        approved_at=NOW,
        previous_fingerprint=previous,
        new_fingerprint="1" * 64,
        outcome=outcome,
        decision_ids=("decision-1",),
        reason_code=reason_code,
    )


def test_audit_record_is_complete_immutable_and_round_trips() -> None:
    record = _record()

    assert PublicationTargetAuditRecord.model_validate_json(record.model_dump_json()) == record
    with pytest.raises(ValidationError, match="frozen"):
        record.actor = "other@example.test"  # type: ignore[misc]


def test_audit_record_rejects_naive_time_invalid_fingerprint_and_inconsistent_outcome() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        _record().model_copy(update={"approved_at": datetime(2026, 7, 22)}).model_validate(
            {
                **_record().model_dump(),
                "approved_at": datetime(2026, 7, 22),
            }
        )
    with pytest.raises(ValidationError, match="new_fingerprint"):
        PublicationTargetAuditRecord.model_validate(
            {**_record().model_dump(), "new_fingerprint": "not-a-fingerprint"}
        )
    with pytest.raises(ValidationError, match="exactly one reason"):
        PublicationTargetAuditRecord.model_validate({**_record().model_dump(), "outcome": "failed"})
    with pytest.raises(ValidationError, match="equal old and new"):
        PublicationTargetAuditRecord.model_validate(
            {
                **_record().model_dump(),
                "outcome": "already_current",
                "previous_fingerprint": "0" * 64,
            }
        )


def test_sqlite_audit_store_is_append_only_atomic_and_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "audit.sqlite3"
    first = SqlitePublicationAuditStore(path)
    created = _record()
    replay = _record(
        outcome=PublicationAuditOutcome.ALREADY_CURRENT,
        previous="1" * 64,
    )

    first.append((created,))
    first.append((replay,))

    restored = SqlitePublicationAuditStore(path).list_for_approval("approval-1")
    assert restored == (created, replay)
    assert SqlitePublicationAuditStore(path).list_for_approval("missing") == ()


def test_audit_binding_rejects_adapter_facts_outside_exact_approval() -> None:
    record = _record()

    validate_publication_audit_binding(
        (record,),
        approval_id=record.approval_id,
        actor=record.actor,
        approved_at=record.approved_at,
        new_fingerprint=record.new_fingerprint,
        approved_decision_ids=record.decision_ids,
    )
    with pytest.raises(ValueError, match="exact approval"):
        validate_publication_audit_binding(
            (record,),
            approval_id=record.approval_id,
            actor="different-steward@example.test",
            approved_at=record.approved_at,
            new_fingerprint=record.new_fingerprint,
            approved_decision_ids=record.decision_ids,
        )


def test_audit_store_rejects_reuse_of_approval_id_for_another_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "audit.sqlite3"
    store = SqlitePublicationAuditStore(path)
    original = _record()
    conflicting = PublicationTargetAuditRecord.model_validate(
        {**original.model_dump(), "actor": "another-steward@example.test"}
    )

    store.append((original,))
    with pytest.raises(PublicationAuditStoreError, match="identity changed"):
        store.append((conflicting,))

    assert SqlitePublicationAuditStore(path).list_for_approval(original.approval_id) == (original,)
