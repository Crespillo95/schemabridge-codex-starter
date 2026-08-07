"""Adversarial unit tests for pure M24 durable-job contracts."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TypeVar

import pytest
from pydantic import BaseModel, ValidationError

from schemabridge.application.ports.background_jobs import (
    JobStoreError,
    JobStoreErrorCode,
)
from schemabridge.domain.background_jobs import (
    BackgroundJob,
    JobAuthorization,
    JobExecutionTargetRef,
    JobFailure,
    JobFailureCode,
    JobFailureDisposition,
    JobKind,
    JobRejectionCount,
    JobResultSummary,
    JobStatus,
    JobTransitionError,
    JobTransitionErrorCode,
    JobWorkflowAccessScope,
    acknowledge_job_cancellation,
    claim_job,
    classify_job_failure,
    complete_job,
    dead_letter_exhausted_lease,
    digest_lease_token,
    expire_job_authorization,
    fail_job,
    heartbeat_job,
    job_retry_delay,
    request_job_cancellation,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.workflows import WorkflowStage

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
PLAN_FINGERPRINT = "a" * 64
IDEMPOTENCY_DIGEST = "b" * 64
PREVIEW_FINGERPRINT = "c" * 64
LEASE_TOKEN = "worker-capability-" + ("x" * 32)
SECOND_LEASE_TOKEN = "worker-capability-" + ("y" * 32)
ModelT = TypeVar("ModelT", bound=BaseModel)


def _authorization(
    *,
    workflow_id: str = "workflow-unit-1",
    workflow_workspace_id: str = "sb_workspace_unit",
    owner_actor_id: str = "sb_actor_owner",
    submitting_actor_id: str = "sb_actor_submitter",
    workflow_access_scope: JobWorkflowAccessScope = JobWorkflowAccessScope.OWNER,
    execution_target: JobExecutionTargetRef | None = None,
    authenticated_at: datetime = NOW - timedelta(minutes=1),
    authorized_at: datetime = NOW,
    expires_at: datetime = NOW + timedelta(minutes=30),
) -> JobAuthorization:
    return JobAuthorization.create(
        workspace_id="sb_workspace_unit",
        workflow_workspace_id=workflow_workspace_id,
        workflow_id=workflow_id,
        workflow_owner_actor_id=owner_actor_id,
        submitting_actor_id=submitting_actor_id,
        workflow_access_scope=workflow_access_scope,
        expected_workflow_revision=7,
        expected_plan_fingerprint=PLAN_FINGERPRINT,
        execution_target=execution_target,
        authenticated_at=authenticated_at,
        authorized_at=authorized_at,
        expires_at=expires_at,
    )


def _execution_target_ref(workspace_id: str) -> JobExecutionTargetRef:
    return JobExecutionTargetRef(
        workspace_id=workspace_id,
        connection_id=CatalogConnectionId("warehouse-unit"),
        route_revision=7,
        route_fingerprint="d" * 64,
        target_fingerprint="e" * 64,
    )


def _job(
    *,
    authorization: JobAuthorization | None = None,
    max_attempts: int = 3,
) -> BackgroundJob:
    return BackgroundJob.create(
        id="execution-job-unit-1",
        authorization=authorization or _authorization(),
        idempotency_digest=IDEMPOTENCY_DIGEST,
        max_attempts=max_attempts,
        created_at=NOW,
    )


def _claim(
    job: BackgroundJob,
    *,
    token: str = LEASE_TOKEN,
    worker_id: str = "worker-unit-1",
    claimed_at: datetime = NOW + timedelta(seconds=1),
    lease_expires_at: datetime = NOW + timedelta(minutes=2),
) -> BackgroundJob:
    return claim_job(
        job,
        worker_id=worker_id,
        lease_token=token,
        claimed_at=claimed_at,
        lease_expires_at=lease_expires_at,
    )


def _result(
    *,
    workflow_id: str = "workflow-unit-1",
    workflow_revision: int = 11,
    completed_at: datetime = NOW + timedelta(seconds=10),
    rejected_count: int = 3,
) -> JobResultSummary:
    counts = (
        (
            JobRejectionCount(code="non_finite_identifier", count=1),
            JobRejectionCount(code="null_join_key", count=2),
        )
        if rejected_count
        else ()
    )
    return JobResultSummary(
        workflow_id=workflow_id,
        workflow_revision=workflow_revision,
        stage=WorkflowStage.PUBLICATION_PROPOSED,
        row_count=3,
        preview_fingerprint=PREVIEW_FINGERPRINT,
        rejected_count=rejected_count,
        rejection_code_counts=counts,
        truncated=False,
        completed_at=completed_at,
    )


def _validated_copy(model: ModelT, **updates: object) -> ModelT:
    payload = model.model_dump(mode="python")
    payload.update(updates)
    return type(model).model_validate(payload)


def test_authorization_fingerprints_stable_request_not_session_renewal() -> None:
    first = _authorization()
    renewed = _authorization(
        authenticated_at=NOW + timedelta(minutes=1),
        authorized_at=NOW + timedelta(minutes=2),
        expires_at=NOW + timedelta(minutes=31),
    )

    assert first.payload_fingerprint == renewed.payload_fingerprint
    assert first.request_fingerprint == renewed.request_fingerprint
    assert (
        first.request_fingerprint
        != _authorization(submitting_actor_id="sb_actor_another").request_fingerprint
    )
    assert (
        first.payload_fingerprint
        != _authorization(owner_actor_id="sb_actor_another_owner").payload_fingerprint
    )
    assert (
        first.payload_fingerprint
        != _authorization(workflow_workspace_id="sb_workspace_historical").payload_fingerprint
    )
    assert (
        first.payload_fingerprint
        != _authorization(
            workflow_access_scope=JobWorkflowAccessScope.WORKSPACE
        ).payload_fingerprint
    )
    assert first.is_current(NOW)
    assert not first.is_current(first.expires_at)
    with pytest.raises(ValueError, match="timezone"):
        first.is_current(NOW.replace(tzinfo=None))


def test_authorization_binds_connector_to_historical_workflow_workspace() -> None:
    historical_workspace = "sb_workspace_historical"
    target = _execution_target_ref(historical_workspace)

    authorization = _authorization(
        workflow_workspace_id=historical_workspace,
        execution_target=target,
    )

    assert authorization.workspace_id == "sb_workspace_unit"
    assert authorization.workflow_workspace_id == historical_workspace
    assert authorization.execution_target == target
    assert authorization.payload_fingerprint != _authorization().payload_fingerprint

    with pytest.raises(
        ValidationError,
        match="immutable workflow workspace",
    ):
        _authorization(
            workflow_workspace_id=historical_workspace,
            execution_target=_execution_target_ref("sb_workspace_substitute"),
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"payload_fingerprint": "0" * 64},
        {"request_fingerprint": "0" * 64},
        {"expected_plan_fingerprint": "not-a-fingerprint"},
        {"operation": "arbitrary_command"},
        {"workflow_id": "workflow;drop"},
        {"authorized_at": NOW - timedelta(minutes=2)},
        {"expires_at": NOW + timedelta(hours=2)},
    ],
)
def test_authorization_rejects_tampering_unknown_commands_and_bad_time(
    updates: dict[str, object],
) -> None:
    with pytest.raises((ValidationError, ValueError)):
        _validated_copy(_authorization(), **updates)


def test_authorization_rejects_naive_times_and_secret_shaped_extra_fields() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        _authorization(authorized_at=NOW.replace(tzinfo=None))

    payload = _authorization().model_dump(mode="python")
    payload.update(
        {
            "bearer_token": "must-never-be-representable",
            "sql": "SELECT secret",
            "claims": {"email": "private@example.test"},
        }
    )
    with pytest.raises(ValidationError):
        JobAuthorization.model_validate(payload)


def test_job_result_is_bounded_sanitized_and_has_no_row_surface() -> None:
    result = _result()
    encoded = json.dumps(result.model_dump(mode="json"), sort_keys=True)

    assert result.rejected_count == 3
    assert tuple(item.code for item in result.rejection_code_counts) == (
        "non_finite_identifier",
        "null_join_key",
    )
    assert '"rows"' not in encoded
    assert "SELECT" not in encoded

    payload = result.model_dump(mode="python")
    payload["rows"] = (("sensitive",),)
    with pytest.raises(ValidationError):
        JobResultSummary.model_validate(payload)


def test_job_result_preserves_exact_total_without_inventing_truncated_distribution() -> None:
    result = JobResultSummary(
        workflow_id="workflow-unit-1",
        workflow_revision=11,
        stage=WorkflowStage.PUBLICATION_PROPOSED,
        row_count=3,
        preview_fingerprint=PREVIEW_FINGERPRINT,
        rejected_count=25_001,
        rejection_code_counts=(
            JobRejectionCount(code="non_finite_identifier", count=1),
            JobRejectionCount(code="null_join_key", count=1),
        ),
        truncated=True,
        completed_at=NOW + timedelta(seconds=10),
    )

    assert result.sampled_rejected_count == 2
    assert result.unclassified_rejection_count == 24_999
    assert result.rejection_counts_complete is False
    assert result.rejection_truncated is True
    assert "unclassified_rejections" not in result.model_dump_json()


@pytest.mark.parametrize(
    "updates",
    [
        {"stage": WorkflowStage.EXECUTED},
        {"rejected_count": 2},
        {"rejected_count": 4, "truncated": False},
        {
            "rejection_code_counts": (
                JobRejectionCount(code="null_join_key", count=2),
                JobRejectionCount(code="non_finite_identifier", count=1),
            )
        },
        {
            "rejection_code_counts": (
                JobRejectionCount(code="null_join_key", count=1),
                JobRejectionCount(code="null_join_key", count=2),
            )
        },
        {"preview_fingerprint": "A" * 64},
        {"workflow_id": "workflow/unit"},
        {"completed_at": NOW.replace(tzinfo=None)},
    ],
)
def test_job_result_rejects_inconsistent_or_unsafe_summary(
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _validated_copy(_result(), **updates)


def test_job_result_rejects_the_reserved_durable_truncation_code() -> None:
    with pytest.raises(ValidationError, match="reserved"):
        JobRejectionCount(code="unclassified_rejections", count=1)


def test_new_job_is_immutable_queued_and_stores_only_key_digest() -> None:
    job = _job()

    assert job.kind is JobKind.EXECUTE_WORKFLOW_PREVIEW
    assert job.status is JobStatus.QUEUED
    assert job.available_at == NOW
    assert job.attempt_count == 0
    assert job.last_fencing_token == 0
    assert not job.is_terminal
    assert job.idempotency_digest == IDEMPOTENCY_DIGEST
    with pytest.raises(ValidationError):
        job.status = JobStatus.SUCCEEDED  # type: ignore[misc]


@pytest.mark.parametrize(
    "updates",
    [
        {"kind": "unknown_kind"},
        {"request_fingerprint": "d" * 64},
        {"last_fencing_token": 1},
        {"attempt_count": 4},
        {"updated_at": NOW - timedelta(seconds=1)},
        {"status": JobStatus.LEASED},
        {"status": JobStatus.SUCCEEDED, "available_at": None},
        {"available_at": None},
        {"completed_at": NOW},
    ],
)
def test_job_model_rejects_impossible_manual_states(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _validated_copy(_job(), **updates)


def test_claim_heartbeat_and_success_keep_only_lease_digest() -> None:
    claimed = _claim(_job())
    assert claimed.status is JobStatus.LEASED
    assert claimed.attempt_count == 1
    assert claimed.last_fencing_token == 1
    assert claimed.lease is not None
    assert claimed.lease.token_digest == digest_lease_token(LEASE_TOKEN)
    assert LEASE_TOKEN not in json.dumps(claimed.model_dump(mode="json"))

    heartbeat = heartbeat_job(
        claimed,
        worker_id="worker-unit-1",
        lease_token=LEASE_TOKEN,
        fencing_token=1,
        heartbeat_at=NOW + timedelta(seconds=5),
        lease_expires_at=NOW + timedelta(minutes=3),
    )
    assert heartbeat.lease is not None
    assert heartbeat.lease.acquired_at == NOW + timedelta(seconds=1)
    assert heartbeat.lease.heartbeat_at == NOW + timedelta(seconds=5)
    assert heartbeat.lease.expires_at == NOW + timedelta(minutes=3)

    completed = complete_job(
        heartbeat,
        worker_id="worker-unit-1",
        lease_token=LEASE_TOKEN,
        fencing_token=1,
        result=_result(),
    )
    assert completed.status is JobStatus.SUCCEEDED
    assert completed.is_terminal
    assert completed.lease is None
    assert completed.result == _result()
    assert completed.completed_at == _result().completed_at


def test_completion_rejects_authorization_expired_during_a_current_lease() -> None:
    authorization = _authorization(expires_at=NOW + timedelta(seconds=5))
    claimed = _claim(
        _job(authorization=authorization),
        lease_expires_at=NOW + timedelta(minutes=2),
    )

    with pytest.raises(JobTransitionError) as expired:
        complete_job(
            claimed,
            worker_id="worker-unit-1",
            lease_token=LEASE_TOKEN,
            fencing_token=1,
            result=_result(completed_at=authorization.expires_at),
        )

    assert expired.value.code is JobTransitionErrorCode.AUTHORIZATION_EXPIRED


@pytest.mark.parametrize(
    ("kwargs", "expected_code"),
    [
        ({"worker_id": "worker-unit-2"}, JobTransitionErrorCode.LEASE_MISMATCH),
        ({"lease_token": SECOND_LEASE_TOKEN}, JobTransitionErrorCode.LEASE_MISMATCH),
        ({"fencing_token": 2}, JobTransitionErrorCode.FENCING_MISMATCH),
        (
            {"heartbeat_at": NOW + timedelta(minutes=2)},
            JobTransitionErrorCode.LEASE_EXPIRED,
        ),
    ],
)
def test_stale_or_expired_worker_cannot_heartbeat(
    kwargs: dict[str, object],
    expected_code: JobTransitionErrorCode,
) -> None:
    claimed = _claim(_job())
    arguments: dict[str, object] = {
        "worker_id": "worker-unit-1",
        "lease_token": LEASE_TOKEN,
        "fencing_token": 1,
        "heartbeat_at": NOW + timedelta(seconds=5),
        "lease_expires_at": NOW + timedelta(minutes=3),
    }
    arguments.update(kwargs)

    with pytest.raises(JobTransitionError) as failure:
        heartbeat_job(claimed, **arguments)  # type: ignore[arg-type]
    assert failure.value.code is expected_code


def test_heartbeat_requires_a_strict_extension_and_bounded_token() -> None:
    claimed = _claim(_job())
    with pytest.raises(JobTransitionError) as not_extended:
        heartbeat_job(
            claimed,
            worker_id="worker-unit-1",
            lease_token=LEASE_TOKEN,
            fencing_token=1,
            heartbeat_at=NOW + timedelta(seconds=5),
            lease_expires_at=NOW + timedelta(minutes=2),
        )
    assert not_extended.value.code is JobTransitionErrorCode.TEMPORAL_CONFLICT

    for unsafe in ("short", "x" * 513, "x" * 31 + "\n"):
        with pytest.raises(ValueError, match="lease token"):
            digest_lease_token(unsafe)

    with pytest.raises(JobTransitionError) as excessive_heartbeat:
        heartbeat_job(
            claimed,
            worker_id="worker-unit-1",
            lease_token=LEASE_TOKEN,
            fencing_token=1,
            heartbeat_at=NOW + timedelta(seconds=5),
            lease_expires_at=NOW + timedelta(minutes=6),
        )
    assert excessive_heartbeat.value.code is JobTransitionErrorCode.TEMPORAL_CONFLICT

    with pytest.raises(JobTransitionError) as excessive_claim:
        _claim(
            _job(),
            lease_expires_at=NOW + timedelta(minutes=6),
        )
    assert excessive_claim.value.code is JobTransitionErrorCode.TEMPORAL_CONFLICT


@pytest.mark.parametrize(
    ("code", "attempt", "max_attempts", "expected"),
    [
        (
            JobFailureCode.REGISTRY_UNAVAILABLE,
            1,
            3,
            JobFailureDisposition.RETRY,
        ),
        (
            JobFailureCode.SOURCE_UNAVAILABLE,
            3,
            3,
            JobFailureDisposition.DEAD_LETTER,
        ),
        (
            JobFailureCode.SOURCE_TIMEOUT,
            2,
            3,
            JobFailureDisposition.RETRY,
        ),
        (
            JobFailureCode.WORKFLOW_STALE,
            1,
            3,
            JobFailureDisposition.FAIL,
        ),
        (
            JobFailureCode.WORKFLOW_RETRY_REQUIRED,
            1,
            3,
            JobFailureDisposition.FAIL,
        ),
        (
            JobFailureCode.AMBIGUOUS_EXTERNAL_EFFECT,
            1,
            3,
            JobFailureDisposition.DEAD_LETTER,
        ),
        (
            JobFailureCode.UNEXPECTED_WORKER_FAILURE,
            1,
            3,
            JobFailureDisposition.DEAD_LETTER,
        ),
    ],
)
def test_failure_classification_is_closed_and_finite(
    code: JobFailureCode,
    attempt: int,
    max_attempts: int,
    expected: JobFailureDisposition,
) -> None:
    assert classify_job_failure(code, attempt=attempt, max_attempts=max_attempts) is expected


@pytest.mark.parametrize(
    ("attempt", "max_attempts"),
    [(0, 3), (4, 3), (1, 11), (2, 1)],
)
def test_failure_classification_rejects_invalid_attempt_bounds(
    attempt: int,
    max_attempts: int,
) -> None:
    with pytest.raises(ValueError, match="attempt bounds"):
        classify_job_failure(
            JobFailureCode.SOURCE_TIMEOUT,
            attempt=attempt,
            max_attempts=max_attempts,
        )


def test_retry_backoff_is_exponential_deterministic_and_bounded() -> None:
    assert job_retry_delay(1) == timedelta(seconds=5)
    assert job_retry_delay(2) == timedelta(seconds=10)
    assert job_retry_delay(7) == timedelta(seconds=300)
    assert job_retry_delay(10) == timedelta(seconds=300)
    for attempt in (0, 11):
        with pytest.raises(ValueError, match="between one and ten"):
            job_retry_delay(attempt)


@pytest.mark.parametrize(
    ("code", "disposition", "attempt"),
    [
        (
            JobFailureCode.AMBIGUOUS_EXTERNAL_EFFECT,
            JobFailureDisposition.FAIL,
            1,
        ),
        (JobFailureCode.PLAN_STALE, JobFailureDisposition.RETRY, 1),
        (JobFailureCode.SOURCE_TIMEOUT, JobFailureDisposition.FAIL, 1),
        (JobFailureCode.SOURCE_TIMEOUT, JobFailureDisposition.RETRY, 0),
    ],
)
def test_failure_value_rejects_forged_classification(
    code: JobFailureCode,
    disposition: JobFailureDisposition,
    attempt: int,
) -> None:
    with pytest.raises(ValidationError):
        JobFailure(
            code=code,
            disposition=disposition,
            attempt=attempt,
            occurred_at=NOW,
        )


def test_transient_failure_waits_then_reclaims_with_new_fence() -> None:
    first = _claim(_job())
    waiting = fail_job(
        first,
        worker_id="worker-unit-1",
        lease_token=LEASE_TOKEN,
        fencing_token=1,
        code=JobFailureCode.SOURCE_TIMEOUT,
        failed_at=NOW + timedelta(seconds=10),
        retry_at=NOW + timedelta(seconds=15),
    )
    assert waiting.status is JobStatus.RETRY_WAIT
    assert waiting.failure is not None
    assert waiting.failure.disposition is JobFailureDisposition.RETRY
    assert waiting.available_at == NOW + timedelta(seconds=15)
    assert waiting.lease is None

    with pytest.raises(JobTransitionError) as too_early:
        _claim(
            waiting,
            token=SECOND_LEASE_TOKEN,
            worker_id="worker-unit-2",
            claimed_at=NOW + timedelta(seconds=14),
        )
    assert too_early.value.code is JobTransitionErrorCode.NOT_AVAILABLE

    second = _claim(
        waiting,
        token=SECOND_LEASE_TOKEN,
        worker_id="worker-unit-2",
        claimed_at=NOW + timedelta(seconds=15),
    )
    assert second.attempt_count == 2
    assert second.last_fencing_token == 2
    assert second.failure is None
    assert second.lease is not None
    assert second.lease.fencing_token == 2


def test_retry_exhaustion_and_ambiguous_failure_dead_letter() -> None:
    first = _claim(_job(max_attempts=2))
    waiting = fail_job(
        first,
        worker_id="worker-unit-1",
        lease_token=LEASE_TOKEN,
        fencing_token=1,
        code=JobFailureCode.SOURCE_UNAVAILABLE,
        failed_at=NOW + timedelta(seconds=5),
        retry_at=NOW + timedelta(seconds=10),
    )
    second = _claim(
        waiting,
        token=SECOND_LEASE_TOKEN,
        worker_id="worker-unit-2",
        claimed_at=NOW + timedelta(seconds=10),
    )
    exhausted = fail_job(
        second,
        worker_id="worker-unit-2",
        lease_token=SECOND_LEASE_TOKEN,
        fencing_token=2,
        code=JobFailureCode.SOURCE_UNAVAILABLE,
        failed_at=NOW + timedelta(seconds=20),
    )
    assert exhausted.status is JobStatus.DEAD_LETTERED
    assert exhausted.failure is not None
    assert exhausted.failure.disposition is JobFailureDisposition.DEAD_LETTER

    ambiguous = fail_job(
        _claim(_job()),
        worker_id="worker-unit-1",
        lease_token=LEASE_TOKEN,
        fencing_token=1,
        code=JobFailureCode.AMBIGUOUS_EXTERNAL_EFFECT,
        failed_at=NOW + timedelta(seconds=5),
    )
    assert ambiguous.status is JobStatus.DEAD_LETTERED


def test_permanent_failure_is_terminal_and_retry_time_is_forbidden() -> None:
    claimed = _claim(_job())
    failed = fail_job(
        claimed,
        worker_id="worker-unit-1",
        lease_token=LEASE_TOKEN,
        fencing_token=1,
        code=JobFailureCode.PLAN_STALE,
        failed_at=NOW + timedelta(seconds=5),
    )
    assert failed.status is JobStatus.FAILED
    assert failed.failure is not None
    assert failed.failure.disposition is JobFailureDisposition.FAIL

    with pytest.raises(JobTransitionError) as retry_on_terminal:
        fail_job(
            claimed,
            worker_id="worker-unit-1",
            lease_token=LEASE_TOKEN,
            fencing_token=1,
            code=JobFailureCode.PLAN_STALE,
            failed_at=NOW + timedelta(seconds=5),
            retry_at=NOW + timedelta(seconds=10),
        )
    assert retry_on_terminal.value.code is JobTransitionErrorCode.TEMPORAL_CONFLICT


def test_retryable_failure_requires_future_retry_time() -> None:
    claimed = _claim(_job())
    for retry_at in (
        None,
        NOW + timedelta(seconds=5),
        NOW + timedelta(seconds=11),
    ):
        with pytest.raises(JobTransitionError) as invalid:
            fail_job(
                claimed,
                worker_id="worker-unit-1",
                lease_token=LEASE_TOKEN,
                fencing_token=1,
                code=JobFailureCode.SOURCE_TIMEOUT,
                failed_at=NOW + timedelta(seconds=5),
                retry_at=retry_at,
            )
        assert invalid.value.code is JobTransitionErrorCode.TEMPORAL_CONFLICT


def test_queued_and_retry_wait_cancellation_are_immediate_and_idempotent() -> None:
    queued = _job()
    cancelled = request_job_cancellation(
        queued,
        requested_at=NOW + timedelta(seconds=1),
    )
    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.attempt_count == 0
    assert cancelled.completed_at == NOW + timedelta(seconds=1)
    assert (
        request_job_cancellation(
            cancelled,
            requested_at=NOW + timedelta(seconds=2),
        )
        is cancelled
    )

    waiting = fail_job(
        _claim(_job()),
        worker_id="worker-unit-1",
        lease_token=LEASE_TOKEN,
        fencing_token=1,
        code=JobFailureCode.SOURCE_TIMEOUT,
        failed_at=NOW + timedelta(seconds=5),
        retry_at=NOW + timedelta(seconds=10),
    )
    cancelled_waiting = request_job_cancellation(
        waiting,
        requested_at=NOW + timedelta(seconds=6),
    )
    assert cancelled_waiting.status is JobStatus.CANCELLED
    assert cancelled_waiting.failure is None


def test_leased_cancellation_is_cooperative_and_requires_exact_lease() -> None:
    claimed = _claim(_job())
    requested = request_job_cancellation(
        claimed,
        requested_at=NOW + timedelta(seconds=5),
    )
    assert requested.status is JobStatus.CANCEL_REQUESTED
    assert requested.lease == claimed.lease
    assert (
        request_job_cancellation(
            requested,
            requested_at=NOW + timedelta(seconds=6),
        )
        is requested
    )

    with pytest.raises(JobTransitionError) as stale:
        acknowledge_job_cancellation(
            requested,
            worker_id="worker-unit-1",
            lease_token=SECOND_LEASE_TOKEN,
            fencing_token=1,
            cancelled_at=NOW + timedelta(seconds=7),
        )
    assert stale.value.code is JobTransitionErrorCode.LEASE_MISMATCH

    cancelled = acknowledge_job_cancellation(
        requested,
        worker_id="worker-unit-1",
        lease_token=LEASE_TOKEN,
        fencing_token=1,
        cancelled_at=NOW + timedelta(seconds=7),
    )
    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.lease is None


def test_success_may_win_cancel_race_but_retry_may_not() -> None:
    requested = request_job_cancellation(
        _claim(_job()),
        requested_at=NOW + timedelta(seconds=5),
    )
    succeeded = complete_job(
        requested,
        worker_id="worker-unit-1",
        lease_token=LEASE_TOKEN,
        fencing_token=1,
        result=_result(completed_at=NOW + timedelta(seconds=7)),
    )
    assert succeeded.status is JobStatus.SUCCEEDED
    assert succeeded.cancel_requested_at == NOW + timedelta(seconds=5)

    with pytest.raises(JobTransitionError) as cancelled_retry:
        fail_job(
            requested,
            worker_id="worker-unit-1",
            lease_token=LEASE_TOKEN,
            fencing_token=1,
            code=JobFailureCode.SOURCE_TIMEOUT,
            failed_at=NOW + timedelta(seconds=7),
            retry_at=NOW + timedelta(seconds=10),
        )
    assert cancelled_retry.value.code is JobTransitionErrorCode.CANCELLATION_PENDING


def test_crashed_lease_reclaims_only_after_expiry_and_fences_old_worker() -> None:
    first = _claim(
        _job(),
        lease_expires_at=NOW + timedelta(seconds=10),
    )
    with pytest.raises(JobTransitionError) as still_owned:
        _claim(
            first,
            token=SECOND_LEASE_TOKEN,
            worker_id="worker-unit-2",
            claimed_at=NOW + timedelta(seconds=9),
        )
    assert still_owned.value.code is JobTransitionErrorCode.NOT_AVAILABLE

    reclaimed = _claim(
        first,
        token=SECOND_LEASE_TOKEN,
        worker_id="worker-unit-2",
        claimed_at=NOW + timedelta(seconds=10),
        lease_expires_at=NOW + timedelta(minutes=3),
    )
    assert reclaimed.attempt_count == 2
    assert reclaimed.lease is not None
    assert reclaimed.lease.fencing_token == 2

    with pytest.raises(JobTransitionError) as old_token:
        complete_job(
            reclaimed,
            worker_id="worker-unit-1",
            lease_token=LEASE_TOKEN,
            fencing_token=1,
            result=_result(completed_at=NOW + timedelta(seconds=20)),
        )
    assert old_token.value.code is JobTransitionErrorCode.LEASE_MISMATCH

    with pytest.raises(JobTransitionError) as old_fence:
        complete_job(
            reclaimed,
            worker_id="worker-unit-2",
            lease_token=SECOND_LEASE_TOKEN,
            fencing_token=1,
            result=_result(completed_at=NOW + timedelta(seconds=20)),
        )
    assert old_fence.value.code is JobTransitionErrorCode.FENCING_MISMATCH


def test_cancel_requested_crash_reclaims_without_losing_cancellation() -> None:
    requested = request_job_cancellation(
        _claim(_job(), lease_expires_at=NOW + timedelta(seconds=10)),
        requested_at=NOW + timedelta(seconds=5),
    )
    reclaimed = _claim(
        requested,
        token=SECOND_LEASE_TOKEN,
        worker_id="worker-unit-2",
        claimed_at=NOW + timedelta(seconds=10),
        lease_expires_at=NOW + timedelta(minutes=3),
    )

    assert reclaimed.status is JobStatus.CANCEL_REQUESTED
    assert reclaimed.cancel_requested_at == NOW + timedelta(seconds=5)
    cancelled = acknowledge_job_cancellation(
        reclaimed,
        worker_id="worker-unit-2",
        lease_token=SECOND_LEASE_TOKEN,
        fencing_token=2,
        cancelled_at=NOW + timedelta(seconds=11),
    )
    assert cancelled.status is JobStatus.CANCELLED


def test_expired_final_lease_is_dead_lettered_instead_of_sticking() -> None:
    leased = _claim(
        _job(max_attempts=1),
        lease_expires_at=NOW + timedelta(seconds=10),
    )
    with pytest.raises(JobTransitionError) as current:
        dead_letter_exhausted_lease(
            leased,
            expired_at=NOW + timedelta(seconds=9),
        )
    assert current.value.code is JobTransitionErrorCode.NOT_AVAILABLE

    dead = dead_letter_exhausted_lease(
        leased,
        expired_at=NOW + timedelta(seconds=10),
    )
    assert dead.status is JobStatus.DEAD_LETTERED
    assert dead.failure is not None
    assert dead.failure.code is JobFailureCode.AMBIGUOUS_EXTERNAL_EFFECT
    assert dead.failure.disposition is JobFailureDisposition.DEAD_LETTER
    assert dead.lease is None
    assert (
        dead_letter_exhausted_lease(
            dead,
            expired_at=NOW + timedelta(seconds=11),
        )
        is dead
    )


def test_expired_lease_with_remaining_attempt_is_reclaimed_not_reaped() -> None:
    leased = _claim(
        _job(max_attempts=2),
        lease_expires_at=NOW + timedelta(seconds=10),
    )
    with pytest.raises(JobTransitionError) as remaining:
        dead_letter_exhausted_lease(
            leased,
            expired_at=NOW + timedelta(seconds=10),
        )
    assert remaining.value.code is JobTransitionErrorCode.NOT_AVAILABLE

    reclaimed = _claim(
        leased,
        token=SECOND_LEASE_TOKEN,
        worker_id="worker-unit-2",
        claimed_at=NOW + timedelta(seconds=10),
        lease_expires_at=NOW + timedelta(seconds=20),
    )
    assert reclaimed.attempt_count == 2
    with pytest.raises(JobTransitionError) as exhausted:
        _claim(
            reclaimed,
            token=LEASE_TOKEN,
            worker_id="worker-unit-3",
            claimed_at=NOW + timedelta(seconds=20),
        )
    assert exhausted.value.code is JobTransitionErrorCode.ATTEMPTS_EXHAUSTED


def test_expired_waiting_authorization_fails_without_a_lease_or_attempt() -> None:
    authorization = _authorization(expires_at=NOW + timedelta(seconds=5))
    queued = _job(authorization=authorization)
    with pytest.raises(JobTransitionError) as current:
        expire_job_authorization(queued, expired_at=NOW + timedelta(seconds=4))
    assert current.value.code is JobTransitionErrorCode.TEMPORAL_CONFLICT

    expired = expire_job_authorization(
        queued,
        expired_at=NOW + timedelta(seconds=5),
    )
    assert expired.status is JobStatus.FAILED
    assert expired.attempt_count == 0
    assert expired.failure is not None
    assert expired.failure.code is JobFailureCode.AUTHORIZATION_EXPIRED
    assert expired.completed_at == NOW + timedelta(seconds=5)
    assert (
        expire_job_authorization(
            expired,
            expired_at=NOW + timedelta(seconds=10),
        )
        is expired
    )

    premature_failure = JobFailure(
        code=JobFailureCode.AUTHORIZATION_EXPIRED,
        disposition=JobFailureDisposition.FAIL,
        attempt=0,
        occurred_at=NOW + timedelta(seconds=4),
    )
    with pytest.raises(ValidationError, match="precedes its expiry"):
        _validated_copy(
            queued,
            status=JobStatus.FAILED,
            updated_at=NOW + timedelta(seconds=4),
            available_at=None,
            completed_at=NOW + timedelta(seconds=4),
            failure=premature_failure,
        )


def test_expiry_cleanup_never_steals_a_current_lease() -> None:
    authorization = _authorization(expires_at=NOW + timedelta(seconds=5))
    leased = _claim(
        _job(authorization=authorization),
        lease_expires_at=NOW + timedelta(seconds=20),
    )
    with pytest.raises(JobTransitionError) as current:
        expire_job_authorization(leased, expired_at=NOW + timedelta(seconds=5))
    assert current.value.code is JobTransitionErrorCode.NOT_AVAILABLE

    failed = fail_job(
        leased,
        worker_id="worker-unit-1",
        lease_token=LEASE_TOKEN,
        fencing_token=1,
        code=JobFailureCode.AUTHORIZATION_EXPIRED,
        failed_at=NOW + timedelta(seconds=5),
    )
    assert failed.status is JobStatus.FAILED


@pytest.mark.parametrize("cancellation_requested", (False, True))
def test_expired_authorization_closes_only_an_abandoned_expired_lease(
    cancellation_requested: bool,
) -> None:
    authorization = _authorization(expires_at=NOW + timedelta(seconds=5))
    leased = _claim(
        _job(authorization=authorization),
        lease_expires_at=NOW + timedelta(seconds=4),
    )
    current = (
        request_job_cancellation(
            leased,
            requested_at=NOW + timedelta(seconds=3),
        )
        if cancellation_requested
        else leased
    )

    expired = expire_job_authorization(
        current,
        expired_at=NOW + timedelta(seconds=5),
    )

    assert expired.status is JobStatus.FAILED
    assert expired.attempt_count == current.attempt_count == 1
    assert expired.last_fencing_token == current.last_fencing_token == 1
    assert expired.lease is None
    assert expired.cancel_requested_at == current.cancel_requested_at
    assert expired.failure is not None
    assert expired.failure.code is JobFailureCode.AUTHORIZATION_EXPIRED
    assert expired.failure.disposition is JobFailureDisposition.FAIL
    assert expired.completed_at == NOW + timedelta(seconds=5)


def test_terminal_jobs_reject_every_non_idempotent_mutation() -> None:
    succeeded = complete_job(
        _claim(_job()),
        worker_id="worker-unit-1",
        lease_token=LEASE_TOKEN,
        fencing_token=1,
        result=_result(),
    )
    operations: tuple[Callable[[], BackgroundJob], ...] = (
        lambda: _claim(succeeded),
        lambda: request_job_cancellation(
            succeeded,
            requested_at=NOW + timedelta(seconds=20),
        ),
        lambda: fail_job(
            succeeded,
            worker_id="worker-unit-1",
            lease_token=LEASE_TOKEN,
            fencing_token=1,
            code=JobFailureCode.PLAN_STALE,
            failed_at=NOW + timedelta(seconds=20),
        ),
    )
    for operation in operations:
        with pytest.raises(JobTransitionError) as immutable:
            operation()
        assert immutable.value.code is JobTransitionErrorCode.TERMINAL_IMMUTABLE


def test_mutations_reject_timestamps_older_than_durable_state() -> None:
    requested = request_job_cancellation(
        _claim(_job()),
        requested_at=NOW + timedelta(seconds=5),
    )
    operations: tuple[Callable[[], BackgroundJob], ...] = (
        lambda: heartbeat_job(
            requested,
            worker_id="worker-unit-1",
            lease_token=LEASE_TOKEN,
            fencing_token=1,
            heartbeat_at=NOW + timedelta(seconds=4),
            lease_expires_at=NOW + timedelta(minutes=3),
        ),
        lambda: acknowledge_job_cancellation(
            requested,
            worker_id="worker-unit-1",
            lease_token=LEASE_TOKEN,
            fencing_token=1,
            cancelled_at=NOW + timedelta(seconds=4),
        ),
        lambda: complete_job(
            requested,
            worker_id="worker-unit-1",
            lease_token=LEASE_TOKEN,
            fencing_token=1,
            result=_result(completed_at=NOW + timedelta(seconds=4)),
        ),
        lambda: fail_job(
            requested,
            worker_id="worker-unit-1",
            lease_token=LEASE_TOKEN,
            fencing_token=1,
            code=JobFailureCode.PLAN_STALE,
            failed_at=NOW + timedelta(seconds=4),
        ),
    )
    for operation in operations:
        with pytest.raises(JobTransitionError) as stale_time:
            operation()
        assert stale_time.value.code is JobTransitionErrorCode.TEMPORAL_CONFLICT


def test_store_errors_expose_only_stable_typed_codes() -> None:
    error = JobStoreError(
        JobStoreErrorCode.IDEMPOTENCY_CONFLICT,
        "job request conflicts with an existing idempotency identity",
    )

    assert error.code is JobStoreErrorCode.IDEMPOTENCY_CONFLICT
    assert "postgres" not in str(error).lower()
    assert "sql" not in str(error).lower()
