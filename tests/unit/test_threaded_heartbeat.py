"""Concurrency tests for periodic fenced lease renewal."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Condition

import pytest

from schemabridge.adapters.control_plane.threaded_heartbeat import (
    ThreadedLeaseHeartbeatSupervisor,
)
from schemabridge.application.ports.background_jobs import (
    LeaseHeartbeatSupervisorError,
)
from schemabridge.domain.background_jobs import (
    BackgroundJob,
    JobAuthorization,
    JobStatus,
    claim_job,
    heartbeat_job,
    request_job_cancellation,
)

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
LEASE_TOKEN = "periodic-heartbeat-capability-" + ("x" * 40)


class _HeartbeatStore:
    def __init__(self, claim: BackgroundJob) -> None:
        self.current = claim
        self.calls = 0
        self.fail_on_call: int | None = None
        self._condition = Condition()

    def heartbeat(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        fencing_token: int,
        lease_duration: timedelta,
    ) -> BackgroundJob:
        with self._condition:
            self.calls += 1
            self._condition.notify_all()
            if self.calls == self.fail_on_call:
                raise RuntimeError("sensitive database heartbeat failure")
            assert job_id == self.current.id
            at = self.current.updated_at + timedelta(milliseconds=100)
            self.current = heartbeat_job(
                self.current,
                worker_id=worker_id,
                lease_token=lease_token,
                fencing_token=fencing_token,
                heartbeat_at=at,
                lease_expires_at=at + lease_duration,
            )
            return self.current

    def request_cancellation(self) -> None:
        with self._condition:
            self.current = request_job_cancellation(
                self.current,
                requested_at=self.current.updated_at + timedelta(milliseconds=10),
            )

    def wait_for_calls(self, expected: int) -> None:
        with self._condition:
            assert self._condition.wait_for(
                lambda: self.calls >= expected,
                timeout=1,
            )


def _claim() -> BackgroundJob:
    authorization = JobAuthorization.create(
        workspace_id="sb_workspace_heartbeat",
        workflow_id="workflow-heartbeat",
        workflow_owner_actor_id="sb_actor_owner",
        submitting_actor_id="sb_actor_submitter",
        expected_workflow_revision=3,
        expected_plan_fingerprint="a" * 64,
        authenticated_at=NOW - timedelta(minutes=1),
        authorized_at=NOW,
        expires_at=NOW + timedelta(minutes=50),
    )
    queued = BackgroundJob.create(
        id="job-heartbeat",
        authorization=authorization,
        idempotency_digest="d" * 64,
        max_attempts=3,
        created_at=NOW,
    )
    return claim_job(
        queued,
        worker_id="worker-heartbeat",
        lease_token=LEASE_TOKEN,
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(milliseconds=500),
    )


def _supervisor(store: _HeartbeatStore) -> ThreadedLeaseHeartbeatSupervisor:
    return ThreadedLeaseHeartbeatSupervisor(  # type: ignore[arg-type]
        job_store=store,
        join_timeout=timedelta(seconds=1),
    )


def test_periodic_heartbeat_runs_more_than_two_intervals_and_joins() -> None:
    claim = _claim()
    store = _HeartbeatStore(claim)
    supervisor = _supervisor(store)

    def operation() -> str:
        store.wait_for_calls(3)
        return "completed"

    result = supervisor.run(
        claim=claim,
        worker_id="worker-heartbeat",
        lease_token=LEASE_TOKEN,
        lease_duration=timedelta(seconds=1),
        heartbeat_interval=timedelta(milliseconds=20),
        operation=operation,
    )

    assert result.value == "completed"
    assert result.claim.status is JobStatus.LEASED
    assert store.calls >= 4
    calls_after_join = store.calls
    assert store.calls == calls_after_join


def test_cancellation_during_operation_is_returned_by_final_heartbeat() -> None:
    claim = _claim()
    store = _HeartbeatStore(claim)
    supervisor = _supervisor(store)

    def operation() -> str:
        store.wait_for_calls(1)
        store.request_cancellation()
        store.wait_for_calls(2)
        return "completed-before-cooperative-stop"

    result = supervisor.run(
        claim=claim,
        worker_id="worker-heartbeat",
        lease_token=LEASE_TOKEN,
        lease_duration=timedelta(seconds=1),
        heartbeat_interval=timedelta(milliseconds=20),
        operation=operation,
    )

    assert result.claim.status is JobStatus.CANCEL_REQUESTED
    assert result.claim.cancel_requested_at is not None
    assert store.calls >= 3


def test_heartbeat_loss_is_sanitized_and_never_runs_a_final_transition() -> None:
    claim = _claim()
    store = _HeartbeatStore(claim)
    store.fail_on_call = 2
    supervisor = _supervisor(store)

    with pytest.raises(LeaseHeartbeatSupervisorError) as failure:
        supervisor.run(
            claim=claim,
            worker_id="worker-heartbeat",
            lease_token=LEASE_TOKEN,
            lease_duration=timedelta(seconds=1),
            heartbeat_interval=timedelta(milliseconds=20),
            operation=lambda: store.wait_for_calls(2),
        )

    assert str(failure.value) == "The worker lease heartbeat was lost."
    assert "sensitive" not in str(failure.value)
    assert store.calls == 2


def test_operation_error_is_reraised_only_after_heartbeat_thread_stops() -> None:
    claim = _claim()
    store = _HeartbeatStore(claim)
    supervisor = _supervisor(store)

    def fail_operation() -> None:
        store.wait_for_calls(2)
        raise LookupError("sanitized workflow failure")

    with pytest.raises(LookupError, match="sanitized workflow failure"):
        supervisor.run(
            claim=claim,
            worker_id="worker-heartbeat",
            lease_token=LEASE_TOKEN,
            lease_duration=timedelta(seconds=1),
            heartbeat_interval=timedelta(milliseconds=20),
            operation=fail_operation,
        )

    calls_after_join = store.calls
    assert calls_after_join >= 2
    assert store.calls == calls_after_join


def test_heartbeat_cadence_must_leave_reclaim_margin() -> None:
    claim = _claim()
    store = _HeartbeatStore(claim)

    with pytest.raises(ValueError, match="less than half"):
        _supervisor(store).run(
            claim=claim,
            worker_id="worker-heartbeat",
            lease_token=LEASE_TOKEN,
            lease_duration=timedelta(seconds=1),
            heartbeat_interval=timedelta(milliseconds=500),
            operation=lambda: None,
        )
