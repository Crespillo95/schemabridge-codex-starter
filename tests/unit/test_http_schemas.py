from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from schemabridge.domain.background_jobs import (
    BackgroundJob,
    JobAuthorization,
    JobRejectionCount,
    JobResultSummary,
    claim_job,
    complete_job,
)
from schemabridge.domain.workflows import WorkflowStage
from schemabridge.entrypoints.http.schemas import (
    ExecutionJobCancellationRequest,
    ExecutionJobResponse,
    ExecutionJobSubmissionRequest,
)

NOW = datetime(2026, 7, 23, 18, 30, tzinfo=UTC)
FINGERPRINT = "a" * 64


def test_submission_schema_forbids_extra_fields_and_requires_exact_confirmation() -> None:
    accepted = ExecutionJobSubmissionRequest.model_validate(
        {
            "expected_workflow_revision": 7,
            "expected_plan_fingerprint": FINGERPRINT,
            "confirmation": "EXECUTE GOVERNED PREVIEW",
        }
    )
    assert accepted.expected_workflow_revision == 7

    for payload in (
        {
            **accepted.model_dump(),
            "sql": "SELECT protected",
        },
        {
            **accepted.model_dump(),
            "confirmation": "yes",
        },
    ):
        with pytest.raises(ValidationError):
            ExecutionJobSubmissionRequest.model_validate(payload)


def test_cancellation_requires_the_exact_explicit_phrase() -> None:
    assert (
        ExecutionJobCancellationRequest(confirmation="CANCEL EXECUTION JOB").confirmation
        == "CANCEL EXECUTION JOB"
    )
    with pytest.raises(ValidationError):
        ExecutionJobCancellationRequest.model_validate({"confirmation": "cancel"})


def test_job_response_excludes_authorization_and_lease_secrets() -> None:
    authorization = JobAuthorization.create(
        workspace_id="workspace-1",
        workflow_id="workflow-1",
        workflow_owner_actor_id="actor-owner",
        submitting_actor_id="actor-submitter",
        expected_workflow_revision=7,
        expected_plan_fingerprint=FINGERPRINT,
        authenticated_at=NOW - timedelta(minutes=1),
        authorized_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    job = BackgroundJob.create(
        id="job-v1-" + "b" * 64,
        authorization=authorization,
        idempotency_digest="c" * 64,
        max_attempts=3,
        created_at=NOW,
    )

    payload = ExecutionJobResponse.from_domain(job).model_dump(mode="json")
    encoded = str(payload)

    assert payload["status"] == "queued"
    for forbidden in (
        "idempotency",
        "submitting_actor",
        "workflow_owner",
        "authenticated_at",
        "authorized_at",
        "expires_at",
        "lease",
        "token",
        "sql",
        "rows",
    ):
        assert forbidden not in encoded.casefold()


def test_job_response_labels_partial_rejection_counts_without_rows_or_storage_sentinel() -> None:
    authorization = JobAuthorization.create(
        workspace_id="workspace-1",
        workflow_id="workflow-1",
        workflow_owner_actor_id="actor-owner",
        submitting_actor_id="actor-submitter",
        expected_workflow_revision=7,
        expected_plan_fingerprint=FINGERPRINT,
        authenticated_at=NOW - timedelta(minutes=1),
        authorized_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    queued = BackgroundJob.create(
        id="job-v1-" + "b" * 64,
        authorization=authorization,
        idempotency_digest="c" * 64,
        max_attempts=3,
        created_at=NOW,
    )
    lease_token = "lease-capability-" + ("d" * 48)
    claimed = claim_job(
        queued,
        worker_id="worker-unit-1",
        lease_token=lease_token,
        claimed_at=NOW + timedelta(seconds=1),
        lease_expires_at=NOW + timedelta(minutes=2),
    )
    succeeded = complete_job(
        claimed,
        worker_id="worker-unit-1",
        lease_token=lease_token,
        fencing_token=1,
        result=JobResultSummary(
            workflow_id="workflow-1",
            workflow_revision=11,
            stage=WorkflowStage.PUBLICATION_PROPOSED,
            row_count=3,
            preview_fingerprint="e" * 64,
            rejected_count=25_001,
            rejection_code_counts=(
                JobRejectionCount(code="non_finite_identifier", count=1),
                JobRejectionCount(code="null_join_key", count=1),
            ),
            truncated=True,
            completed_at=NOW + timedelta(seconds=2),
        ),
    )

    payload = ExecutionJobResponse.from_domain(succeeded).model_dump(mode="json")
    result = payload["result"]
    assert isinstance(result, dict)
    assert result["rejected_count"] == 25_001
    assert result["unclassified_rejection_count"] == 24_999
    assert result["rejection_counts_complete"] is False
    assert result["rejection_truncated"] is True
    assert result["truncated"] is True
    encoded = str(payload)
    assert "unclassified_rejections" not in encoded
    assert "rows" not in encoded
