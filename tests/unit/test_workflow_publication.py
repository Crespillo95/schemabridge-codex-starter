"""Focused checks for exact DataHub workflow publication evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from schemabridge.adapters.datahub.workflow_publication import (
    DataHubWorkflowPublicationAdapter,
    DataHubWorkflowPublicationConfig,
)
from schemabridge.domain.publication_audit import (
    PublicationAuditOutcome,
    PublicationFamily,
    PublicationTargetAuditRecord,
)
from schemabridge.domain.workflows import (
    WorkflowPublicationApproval,
    WorkflowPublicationConfirmation,
    WorkflowPublicationProposal,
    WorkflowPublicationStatus,
)


def _proposal() -> WorkflowPublicationProposal:
    return WorkflowPublicationProposal.create(
        workflow_id="workflow-audit-readback",
        plan_fingerprint="1" * 64,
        execution_fingerprint="2" * 64,
        request_fingerprint="3" * 64,
    )


def _approval(proposal: WorkflowPublicationProposal) -> WorkflowPublicationApproval:
    return WorkflowPublicationApproval(
        id=f"workflow-publication-{proposal.idempotency_key}",
        workflow_id=proposal.workflow_id,
        proposal_fingerprint=proposal.fingerprint,
        idempotency_key=proposal.idempotency_key,
        actor="local-operator",
        approved_at=datetime(2026, 7, 22, 10, 30, tzinfo=UTC),
        confirmation=WorkflowPublicationConfirmation.PUBLISH_EXECUTION_CONTEXT,
    )


def _writer() -> DataHubWorkflowPublicationAdapter:
    return DataHubWorkflowPublicationAdapter(
        DataHubWorkflowPublicationConfig(
            server="http://datahub.invalid",
            token="not-logged",
            actor_urn="urn:li:corpuser:schemabridge",
        )
    )


def _fingerprint_properties(proposal: WorkflowPublicationProposal) -> dict[str, str]:
    return {
        "schemabridge.workflowId": proposal.workflow_id,
        "schemabridge.workflowFingerprint": proposal.fingerprint,
        "schemabridge.workflowIdempotencyKey": proposal.idempotency_key,
        "schemabridge.planFingerprint": proposal.plan_fingerprint,
        "schemabridge.executionFingerprint": proposal.execution_fingerprint,
        "schemabridge.requestFingerprint": proposal.request_fingerprint,
    }


def test_datahub_workflow_does_not_claim_success_without_target_audit_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = _proposal()
    approval = _approval(proposal)
    incomplete_document = SimpleNamespace(customProperties=_fingerprint_properties(proposal))
    graph = SimpleNamespace(get_aspect=lambda _urn, _aspect: incomplete_document)
    writer = _writer()
    writes: list[object] = []
    monkeypatch.setattr(writer, "_verify_runtime_identity", lambda: None)
    monkeypatch.setattr(writer, "_client", lambda: SimpleNamespace(_graph=graph))
    monkeypatch.setattr(writer, "_upsert_document", lambda *_args: writes.append(object()))

    result = writer.publish(proposal, approval)

    assert len(writes) == 1
    assert result.status is WorkflowPublicationStatus.FAILED
    assert result.failure_code == "datahub_publication_failed"
    assert result.audit_record.outcome is PublicationAuditOutcome.FAILED


def test_datahub_workflow_replay_requires_exact_embedded_approval_and_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = _proposal()
    approval = _approval(proposal)
    document_ref = f"urn:li:document:schemabridge-workflow-{proposal.idempotency_key}"
    audit_record = PublicationTargetAuditRecord(
        family=PublicationFamily.WORKFLOW,
        operation="upsert_document",
        target=document_ref,
        approval_id=approval.id,
        actor=approval.actor,
        approved_at=approval.approved_at,
        new_fingerprint=proposal.fingerprint,
        outcome=PublicationAuditOutcome.SUCCEEDED,
    )
    properties = {
        **_fingerprint_properties(proposal),
        "schemabridge.approvalId": approval.id,
        "schemabridge.approvedBy": approval.actor,
        "schemabridge.approvedAt": approval.approved_at.isoformat(),
        "schemabridge.publicationAudit": audit_record.model_dump_json(),
    }
    document = SimpleNamespace(customProperties=properties)
    graph = SimpleNamespace(get_aspect=lambda _urn, _aspect: document)
    writer = _writer()
    monkeypatch.setattr(writer, "_verify_runtime_identity", lambda: None)
    monkeypatch.setattr(writer, "_client", lambda: SimpleNamespace(_graph=graph))
    monkeypatch.setattr(
        writer,
        "_upsert_document",
        lambda *_args: pytest.fail("an exact replay must not write DataHub again"),
    )

    result = writer.publish(proposal, approval)

    assert result.status is WorkflowPublicationStatus.ALREADY_CURRENT
    assert result.audit_record.outcome is PublicationAuditOutcome.ALREADY_CURRENT


def test_datahub_workflow_rejects_conflicting_content_before_upsert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = _proposal()
    approval = _approval(proposal)
    conflicting_document = SimpleNamespace(
        customProperties={"schemabridge.workflowFingerprint": "f" * 64}
    )
    graph = SimpleNamespace(get_aspect=lambda _urn, _aspect: conflicting_document)
    writer = _writer()
    monkeypatch.setattr(writer, "_verify_runtime_identity", lambda: None)
    monkeypatch.setattr(writer, "_client", lambda: SimpleNamespace(_graph=graph))
    monkeypatch.setattr(
        writer,
        "_upsert_document",
        lambda *_args: pytest.fail("an immutable workflow conflict must not be overwritten"),
    )

    result = writer.publish(proposal, approval)

    assert result.status is WorkflowPublicationStatus.FAILED
    assert result.failure_code == "workflow_version_conflict"
    assert result.audit_record.previous_fingerprint == "f" * 64
    assert result.audit_record.outcome is PublicationAuditOutcome.FAILED


def test_workflow_proposal_rejects_reused_idempotency_key_for_different_content() -> None:
    first = _proposal()
    second = WorkflowPublicationProposal.create(
        workflow_id=first.workflow_id,
        plan_fingerprint=first.plan_fingerprint,
        execution_fingerprint=first.execution_fingerprint,
        request_fingerprint="4" * 64,
    )

    with pytest.raises(ValidationError, match="idempotency key does not match"):
        WorkflowPublicationProposal(
            workflow_id=second.workflow_id,
            plan_fingerprint=second.plan_fingerprint,
            execution_fingerprint=second.execution_fingerprint,
            request_fingerprint=second.request_fingerprint,
            idempotency_key=first.idempotency_key,
            fingerprint=second.fingerprint,
        )
