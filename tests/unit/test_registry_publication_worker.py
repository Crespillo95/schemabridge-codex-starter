"""Adversarial unit tests for one bounded M34 publisher-worker iteration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from tests.unit.test_registry_join_changes import _approved_draft, _two_model_base
from tests.unit.test_registry_model_changes import (
    _join_upsert,
    _joined_base,
    _prepared_change,
    _replacement,
    _replacement_base,
)
from tests.unit.test_registry_publication_jobs import _authorization
from tests.unit.test_registry_publication_v2 import _proposal

from schemabridge.application.ports.registry_publication import (
    RegistryPublicationAuthorityError,
    RegistryPublicationHeartbeatSupervisorError,
    RegistryPublicationStoreError,
    RegistryPublicationStoreErrorCode,
)
from schemabridge.application.registry_publication_worker import (
    RegistryPublisherIterationOutcome,
    RegistryPublisherWorkerError,
    RegistryPublisherWorkerErrorCode,
    RunOneRegistryPublisherWorker,
)
from schemabridge.domain.publication_audit import (
    PublicationAuditOutcome,
    PublicationFamily,
    PublicationTargetAuditRecord,
)
from schemabridge.domain.registry_changes import (
    PreparedRegistryJoinProposal,
    assemble_join_change_registry_version,
)
from schemabridge.domain.registry_model_changes import (
    RegistryIncidentJoinPreservation,
    assemble_model_replacement_registry_version,
)
from schemabridge.domain.registry_publication import (
    ObservedRegistryPublicationResult,
    PublicationReadbackReceipt,
    PublishableRegistryVersion,
    RegistryPublicationAuthorization,
    assemble_publishable_registry_version,
    observed_registry_related_asset_urns,
)
from schemabridge.domain.registry_publication_jobs import (
    PreparedRegistryPublicationProposal,
    RegistryPublicationFailureCode,
    RegistryPublicationJob,
    RegistryPublicationJobStatus,
    acknowledge_registry_publication_cancellation,
    authorize_registry_publication_job,
    complete_registry_publication_job,
    create_registry_publication_job,
    fail_registry_publication_job,
    heartbeat_registry_publication_job,
    lease_registry_publication_job,
    reap_expired_registry_publication_lease,
    record_registry_publication_candidate,
    registry_publication_request_fingerprint,
    request_registry_publication_cancellation,
)
from schemabridge.domain.semantic_registry import GovernedSemanticRegistrySnapshot

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
CAPABILITY = "publisher-worker-capability-" + ("x" * 48)
WORKER_ID = "publisher-worker-unit"


class _Clock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


class _CapabilityFactory:
    def __init__(self, *, value: str = CAPABILITY, error: Exception | None = None) -> None:
        self.value = value
        self.error = error
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.value


class _Authority:
    def __init__(
        self,
        base: GovernedSemanticRegistrySnapshot | None = None,
        after_resolve: Callable[[], None] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.error: RegistryPublicationAuthorityError | None = None
        self.base = base
        self.after_resolve = after_resolve

    def resolve_base(
        self,
        proposal: PreparedRegistryPublicationProposal,
    ) -> GovernedSemanticRegistrySnapshot | None:
        self.calls.append(proposal.id)
        if self.error is not None:
            raise self.error
        if self.after_resolve is not None:
            self.after_resolve()
        return self.base


class _Publisher:
    def __init__(self, order: list[str] | None = None) -> None:
        self.order = order if order is not None else []
        self.observe_mode = "target_absent"
        self.publish_mode = "published"
        self.observe_error: Exception | None = None
        self.publish_error: Exception | None = None
        self.observe_calls: list[tuple[str, str]] = []
        self.publish_calls: list[tuple[str, str]] = []

    def observe(
        self,
        candidate: PublishableRegistryVersion,
        authorization: RegistryPublicationAuthorization,
        *,
        observed_at: datetime,
    ) -> ObservedRegistryPublicationResult:
        self.order.append("observe")
        self.observe_calls.append((candidate.fingerprint, authorization.id))
        if self.observe_error is not None:
            raise self.observe_error
        return _result(
            candidate,
            authorization,
            observed_at=observed_at,
            mode=self.observe_mode,
        )

    def publish(
        self,
        candidate: PublishableRegistryVersion,
        authorization: RegistryPublicationAuthorization,
        *,
        observed_at: datetime,
    ) -> ObservedRegistryPublicationResult:
        self.order.append("publish")
        self.publish_calls.append((candidate.fingerprint, authorization.id))
        if self.publish_error is not None:
            raise self.publish_error
        return _result(
            candidate,
            authorization,
            observed_at=observed_at,
            mode=self.publish_mode,
        )


class _WorkerStore:
    """Database-time-shaped fake applying only pure durable transitions."""

    def __init__(self, *jobs: RegistryPublicationJob, database_now: datetime = NOW) -> None:
        self.jobs = {job.id: job for job in jobs}
        self.database_now = database_now
        self.reap_calls = 0
        self.claim_calls = 0
        self.record_calls = 0
        self.complete_calls = 0
        self.fail_calls: list[RegistryPublicationFailureCode] = []
        self.acknowledge_calls = 0
        self.heartbeat_calls = 0
        self.after_heartbeat: Callable[[], None] | None = None
        self.cancel_on_heartbeat = False
        self.cancel_after_claim = False
        self.invalid_claim = False
        self.raise_on_reap = False

    def _tick(self) -> datetime:
        current = self.database_now
        self.database_now += timedelta(seconds=1)
        return current

    def reap_expired_leases(self, *, limit: int = 100) -> int:
        self.reap_calls += 1
        if self.raise_on_reap:
            raise RegistryPublicationStoreError(
                RegistryPublicationStoreErrorCode.UNAVAILABLE,
                "private registry queue DSN",
            )
        reaped = 0
        for job_id, job in tuple(self.jobs.items()):
            if reaped >= limit or job.lease is None or job.lease.expires_at > self.database_now:
                continue
            self.jobs[job_id] = reap_expired_registry_publication_lease(
                job,
                expired_at=self.database_now,
            )
            reaped += 1
        return reaped

    def claim_next(
        self,
        *,
        worker_id: str,
        lease_capability: str,
        lease_duration: timedelta,
    ) -> RegistryPublicationJob | None:
        self.claim_calls += 1
        claimable = next(
            (
                job
                for job in self.jobs.values()
                if job.status
                in {
                    RegistryPublicationJobStatus.QUEUED,
                    RegistryPublicationJobStatus.APPROVED,
                    RegistryPublicationJobStatus.RETRY_WAIT,
                }
                and job.available_at <= self.database_now
            ),
            None,
        )
        if claimable is None:
            return None
        claimed = lease_registry_publication_job(
            claimable,
            worker_id=worker_id,
            lease_capability=lease_capability,
            acquired_at=self._tick(),
            lease_duration=lease_duration,
        )
        if self.cancel_after_claim:
            claimed = request_registry_publication_cancellation(
                claimed,
                requested_at=self._tick(),
            )
        self.jobs[claimed.id] = claimed
        if self.invalid_claim:
            return claimed.model_copy(update={"last_fencing_token": claimed.last_fencing_token + 1})
        return claimed

    def heartbeat(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
        lease_duration: timedelta,
    ) -> RegistryPublicationJob:
        self.heartbeat_calls += 1
        if self.cancel_on_heartbeat:
            self.jobs[job_id] = request_registry_publication_cancellation(
                self.jobs[job_id],
                requested_at=self._tick(),
            )
            self.cancel_on_heartbeat = False
        updated = heartbeat_registry_publication_job(
            self.jobs[job_id],
            worker_id=worker_id,
            lease_capability=lease_capability,
            fencing_token=fencing_token,
            heartbeat_at=self._tick(),
            lease_duration=lease_duration,
        )
        self.jobs[job_id] = updated
        if self.after_heartbeat is not None:
            self.after_heartbeat()
        return updated

    def record_candidate(
        self,
        job_id: str,
        candidate: PublishableRegistryVersion,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
    ) -> RegistryPublicationJob:
        self.record_calls += 1
        updated = record_registry_publication_candidate(
            self.jobs[job_id],
            candidate,
            worker_id=worker_id,
            lease_capability=lease_capability,
            fencing_token=fencing_token,
            completed_at=self._tick(),
        )
        self.jobs[job_id] = updated
        return updated

    def complete(
        self,
        job_id: str,
        receipt: PublicationReadbackReceipt,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
    ) -> RegistryPublicationJob:
        self.complete_calls += 1
        updated = complete_registry_publication_job(
            self.jobs[job_id],
            receipt,
            worker_id=worker_id,
            lease_capability=lease_capability,
            fencing_token=fencing_token,
            completed_at=self._tick(),
        )
        self.jobs[job_id] = updated
        return updated

    def fail(
        self,
        job_id: str,
        code: RegistryPublicationFailureCode,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
    ) -> RegistryPublicationJob:
        self.fail_calls.append(code)
        updated = fail_registry_publication_job(
            self.jobs[job_id],
            code,
            worker_id=worker_id,
            lease_capability=lease_capability,
            fencing_token=fencing_token,
            failed_at=self._tick(),
        )
        self.jobs[job_id] = updated
        return updated

    def acknowledge_cancellation(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
    ) -> RegistryPublicationJob:
        self.acknowledge_calls += 1
        updated = acknowledge_registry_publication_cancellation(
            self.jobs[job_id],
            worker_id=worker_id,
            lease_capability=lease_capability,
            fencing_token=fencing_token,
            cancelled_at=self._tick(),
        )
        self.jobs[job_id] = updated
        return updated


class _InlineSupervisor:
    def __init__(self, store: _WorkerStore) -> None:
        self.store = store
        self.calls = 0
        self.cancel_during_operation = False
        self.return_invalid_claim = False
        self.after_operation: Callable[[], None] | None = None
        self.error: RegistryPublicationHeartbeatSupervisorError | None = None

    def run(
        self,
        *,
        claim: RegistryPublicationJob,
        worker_id: str,
        lease_capability: str,
        lease_duration: timedelta,
        heartbeat_interval: timedelta,
        operation: Callable[[], object],
    ) -> tuple[object, RegistryPublicationJob]:
        del worker_id, lease_capability, lease_duration, heartbeat_interval
        self.calls += 1
        if self.error is not None:
            raise self.error
        value = operation()
        if self.after_operation is not None:
            self.after_operation()
        refreshed = self.store.jobs[claim.id]
        if self.cancel_during_operation:
            refreshed = request_registry_publication_cancellation(
                refreshed,
                requested_at=self.store._tick(),
            )
            self.store.jobs[claim.id] = refreshed
        if self.return_invalid_claim:
            refreshed = refreshed.model_copy(
                update={"last_fencing_token": refreshed.last_fencing_token + 1}
            )
        return value, refreshed


def test_idle_reaps_before_one_claim_and_performs_no_external_io() -> None:
    store = _WorkerStore()
    worker, authority, publisher, _supervisor = _worker(store)

    result = worker.execute()

    assert result.outcome is RegistryPublisherIterationOutcome.IDLE
    assert store.reap_calls == 1
    assert store.claim_calls == 1
    assert authority.calls == []
    assert publisher.observe_calls == []
    assert publisher.publish_calls == []


@pytest.mark.parametrize(
    "factory",
    (
        _CapabilityFactory(value="short"),
        _CapabilityFactory(error=RuntimeError("private random generator state")),
    ),
)
def test_invalid_or_crashing_capability_factory_is_sanitized_before_claim(
    factory: _CapabilityFactory,
) -> None:
    store = _WorkerStore()
    worker, _authority, _publisher, _supervisor = _worker(
        store,
        capability_factory=factory,
    )

    with pytest.raises(RegistryPublisherWorkerError) as raised:
        worker.execute()

    assert raised.value.code is RegistryPublisherWorkerErrorCode.INVALID_CAPABILITY
    assert "private" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert store.claim_calls == 0


def test_invalid_fencing_claim_fails_before_authority_or_datahub() -> None:
    job = _queued_job()
    store = _WorkerStore(job)
    store.invalid_claim = True
    worker, authority, publisher, _supervisor = _worker(store)

    with pytest.raises(RegistryPublisherWorkerError) as raised:
        worker.execute()

    assert raised.value.code is RegistryPublisherWorkerErrorCode.INVALID_CLAIM
    assert authority.calls == []
    assert publisher.observe_calls == []
    assert publisher.publish_calls == []


def test_prepare_builds_candidate_but_never_calls_datahub_publisher() -> None:
    job = _queued_job()
    store = _WorkerStore(job)
    worker, authority, publisher, _supervisor = _worker(store)

    result = worker.execute()

    durable = store.jobs[job.id]
    assert result.outcome is RegistryPublisherIterationOutcome.CANDIDATE_READY
    assert durable.status is RegistryPublicationJobStatus.AWAITING_APPROVAL
    assert durable.candidate == assemble_publishable_registry_version(job.proposal, base=None)
    assert authority.calls == [job.proposal.id, job.proposal.id]
    assert publisher.observe_calls == []
    assert publisher.publish_calls == []


def test_prepare_dispatches_join_proposal_to_additive_m35_assembler() -> None:
    proposal, base = _join_proposal("join-change-worker-v3")
    job = _queued_job(proposal, submitted_at=NOW + timedelta(minutes=5))
    store = _WorkerStore(job, database_now=NOW + timedelta(minutes=5))
    authority = _Authority(base)
    worker, _authority, publisher, _supervisor = _worker(store, authority=authority)

    result = worker.execute()

    durable = store.jobs[job.id]
    assert result.outcome is RegistryPublisherIterationOutcome.CANDIDATE_READY
    assert durable.status is RegistryPublicationJobStatus.AWAITING_APPROVAL
    assert durable.candidate == assemble_join_change_registry_version(proposal, base=base)
    assert durable.candidate.registry.mapping_set.mappings == base.mapping_set.mappings
    assert len(durable.candidate.registry.join_contracts.contracts) == 1
    assert authority.calls == [proposal.id, proposal.id]
    assert publisher.observe_calls == []
    assert publisher.publish_calls == []


def test_prepare_dispatches_model_replacement_to_phase_b_assembler() -> None:
    base, identity = _joined_base()
    replacement_base = _replacement_base(base, identity)
    proposal = _prepared_change(
        base=replacement_base,
        replacement=_replacement(identity),
        incident_changes=(
            RegistryIncidentJoinPreservation(base=replacement_base.incident_joins[0]),
        ),
    )
    submitted_at = proposal.prepared_at + timedelta(minutes=1)
    job = _queued_job(proposal, submitted_at=submitted_at)
    store = _WorkerStore(job, database_now=submitted_at)
    authority = _Authority(base)
    worker, _authority, publisher, _supervisor = _worker(store, authority=authority)

    result = worker.execute()

    durable = store.jobs[job.id]
    assert result.outcome is RegistryPublisherIterationOutcome.CANDIDATE_READY
    assert durable.status is RegistryPublicationJobStatus.AWAITING_APPROVAL
    assert durable.candidate == assemble_model_replacement_registry_version(
        proposal,
        base=base,
    )
    assert authority.calls == [proposal.id, proposal.id]
    assert publisher.observe_calls == []
    assert publisher.publish_calls == []


def test_expired_model_replacement_profile_fails_before_authority_or_candidate() -> None:
    base, identity = _joined_base()
    replacement_base = _replacement_base(base, identity)
    replacement = _replacement(identity, physical_field="crm.customers.customer_id_v2")
    upsert = _join_upsert(base, identity, replacement_base, replacement)
    proposal = _prepared_change(
        base=replacement_base,
        replacement=replacement,
        incident_changes=(upsert,),
    )
    job = _queued_job(proposal, submitted_at=proposal.prepared_at + timedelta(minutes=1))
    store = _WorkerStore(job, database_now=upsert.profile_witness.expires_at)
    authority = _Authority(base)
    worker, _authority, publisher, _supervisor = _worker(store, authority=authority)

    result = worker.execute()

    durable = store.jobs[job.id]
    assert result.outcome is RegistryPublisherIterationOutcome.FAILED
    assert durable.failure_code is RegistryPublicationFailureCode.PROPOSAL_STALE
    assert durable.candidate is None
    assert authority.calls == []
    assert publisher.observe_calls == []
    assert publisher.publish_calls == []


def test_expired_join_profile_fails_before_authority_candidate_or_datahub() -> None:
    proposal, base = _join_proposal("join-change-expired-worker-v3")
    job = _queued_job(proposal, submitted_at=NOW + timedelta(minutes=5))
    store = _WorkerStore(job, database_now=proposal.profile_expires_at)
    authority = _Authority(base)
    worker, _authority, publisher, _supervisor = _worker(store, authority=authority)

    result = worker.execute()

    durable = store.jobs[job.id]
    assert result.outcome is RegistryPublisherIterationOutcome.FAILED
    assert durable.failure_code is RegistryPublicationFailureCode.PROPOSAL_STALE
    assert durable.candidate is None
    assert store.record_calls == 0
    assert authority.calls == []
    assert publisher.observe_calls == []
    assert publisher.publish_calls == []


def test_join_profile_expiring_during_authority_fails_before_candidate() -> None:
    proposal, base = _join_proposal("join-change-expiring-worker-v3")
    current = _Clock(proposal.profile_expires_at - timedelta(microseconds=1))
    job = _queued_job(proposal, submitted_at=NOW + timedelta(minutes=5))
    store = _WorkerStore(
        job,
        database_now=proposal.profile_expires_at - timedelta(seconds=30),
    )
    authority = _Authority(
        base,
        after_resolve=lambda: setattr(current, "current", proposal.profile_expires_at),
    )
    worker, _authority, publisher, _supervisor = _worker(
        store,
        authority=authority,
        clock=current,
    )

    result = worker.execute()

    durable = store.jobs[job.id]
    assert result.outcome is RegistryPublisherIterationOutcome.FAILED
    assert durable.failure_code is RegistryPublicationFailureCode.PROPOSAL_STALE
    assert durable.candidate is None
    assert store.record_calls == 0
    assert authority.calls == [proposal.id]
    assert publisher.publish_calls == []


def test_join_base_drift_fails_before_candidate_or_datahub() -> None:
    proposal, base = _join_proposal("join-change-drift-worker-v3")
    job = _queued_job(proposal, submitted_at=NOW + timedelta(minutes=5))
    store = _WorkerStore(job, database_now=NOW + timedelta(minutes=5))
    drifted = base.model_copy(update={"version": base.version + 1})
    authority = _Authority(drifted)
    worker, _authority, publisher, _supervisor = _worker(store, authority=authority)

    result = worker.execute()

    durable = store.jobs[job.id]
    assert result.outcome is RegistryPublisherIterationOutcome.FAILED
    assert durable.failure_code is RegistryPublicationFailureCode.CANDIDATE_INVALID
    assert durable.candidate is None
    assert store.record_calls == 0
    assert authority.calls == [proposal.id]
    assert publisher.observe_calls == []
    assert publisher.publish_calls == []


def test_join_base_drift_after_supervised_build_blocks_candidate_mutation() -> None:
    proposal, base = _join_proposal("join-change-post-build-drift-v3")
    job = _queued_job(proposal, submitted_at=NOW + timedelta(minutes=5))
    store = _WorkerStore(job, database_now=NOW + timedelta(minutes=5))
    authority = _Authority(base)
    supervisor = _InlineSupervisor(store)
    supervisor.after_operation = lambda: setattr(
        authority,
        "base",
        base.model_copy(update={"version": base.version + 1}),
    )
    worker, _authority, publisher, _supervisor = _worker(
        store,
        authority=authority,
        supervisor=supervisor,
    )

    result = worker.execute()

    durable = store.jobs[job.id]
    assert result.outcome is RegistryPublisherIterationOutcome.FAILED
    assert durable.failure_code is RegistryPublicationFailureCode.CANDIDATE_INVALID
    assert durable.candidate is None
    assert store.record_calls == 0
    assert authority.calls == [proposal.id, proposal.id]
    assert publisher.observe_calls == []
    assert publisher.publish_calls == []


def test_prepare_cancellation_observed_by_supervisor_is_acknowledged_without_candidate() -> None:
    job = _queued_job()
    store = _WorkerStore(job)
    supervisor = _InlineSupervisor(store)
    supervisor.cancel_during_operation = True
    worker, _authority, publisher, _ = _worker(store, supervisor=supervisor)

    result = worker.execute()

    durable = store.jobs[job.id]
    assert result.outcome is RegistryPublisherIterationOutcome.CANCELLED
    assert durable.status is RegistryPublicationJobStatus.CANCELLED
    assert durable.candidate is None
    assert store.record_calls == 0
    assert store.acknowledge_calls == 1
    assert publisher.publish_calls == []


def test_refreshed_claim_with_another_fence_is_lease_lost_without_settlement() -> None:
    job = _queued_job()
    store = _WorkerStore(job)
    supervisor = _InlineSupervisor(store)
    supervisor.return_invalid_claim = True
    worker, _authority, _publisher, _ = _worker(store, supervisor=supervisor)

    with pytest.raises(RegistryPublisherWorkerError) as raised:
        worker.execute()

    assert raised.value.code is RegistryPublisherWorkerErrorCode.LEASE_LOST
    assert store.record_calls == 0
    assert store.fail_calls == []
    assert store.jobs[job.id].status is RegistryPublicationJobStatus.LEASED


def test_publish_observes_before_authority_and_writes_only_when_target_is_absent() -> None:
    job = _approved_job()
    order: list[str] = []
    store = _WorkerStore(job)
    authority = _Authority()
    publisher = _Publisher(order)
    worker, _authority, _publisher, _supervisor = _worker(
        store,
        authority=authority,
        publisher=publisher,
    )

    result = worker.execute()

    assert result.outcome is RegistryPublisherIterationOutcome.ACTIVATION_READY
    assert order == ["observe", "publish"]
    assert authority.calls == [job.proposal.id, job.proposal.id]
    assert len(publisher.publish_calls) == 1
    assert store.jobs[job.id].status is RegistryPublicationJobStatus.ACTIVATION_READY


def test_join_base_drift_after_authorization_blocks_the_external_write() -> None:
    proposal, base = _join_proposal("join-change-prewrite-drift-v3")
    job = _approved_join_job(proposal, base)
    store = _WorkerStore(job, database_now=NOW + timedelta(minutes=6))
    authority = _Authority(base.model_copy(update={"version": base.version + 1}))
    worker, _authority, publisher, _supervisor = _worker(store, authority=authority)

    result = worker.execute()

    durable = store.jobs[job.id]
    assert result.outcome is RegistryPublisherIterationOutcome.FAILED
    assert durable.failure_code is RegistryPublicationFailureCode.CANDIDATE_INVALID
    assert authority.calls == [proposal.id]
    assert len(publisher.observe_calls) == 1
    assert publisher.publish_calls == []
    assert store.heartbeat_calls == 0


def test_join_profile_expiring_during_prewrite_authority_blocks_external_write() -> None:
    proposal, base = _join_proposal("join-change-prewrite-expiry-v3")
    current = _Clock(proposal.profile_expires_at - timedelta(microseconds=1))
    job = _approved_join_job(
        proposal,
        base,
        authorized_at=proposal.profile_expires_at - timedelta(minutes=1),
    )
    store = _WorkerStore(
        job,
        database_now=proposal.profile_expires_at - timedelta(seconds=30),
    )
    authority = _Authority(
        base,
        after_resolve=lambda: setattr(current, "current", proposal.profile_expires_at),
    )
    worker, _authority, publisher, _supervisor = _worker(
        store,
        authority=authority,
        clock=current,
    )

    result = worker.execute()

    durable = store.jobs[job.id]
    assert result.outcome is RegistryPublisherIterationOutcome.FAILED
    assert durable.failure_code is RegistryPublicationFailureCode.PROPOSAL_STALE
    assert authority.calls == [proposal.id]
    assert len(publisher.observe_calls) == 1
    assert publisher.publish_calls == []
    assert store.heartbeat_calls == 0


def test_join_base_drift_during_prewrite_heartbeat_blocks_external_write() -> None:
    proposal, base = _join_proposal("join-change-heartbeat-drift-v3")
    job = _approved_join_job(proposal, base)
    store = _WorkerStore(job, database_now=NOW + timedelta(minutes=6))
    authority = _Authority(base)
    store.after_heartbeat = lambda: setattr(
        authority,
        "base",
        base.model_copy(update={"version": base.version + 1}),
    )
    worker, _authority, publisher, _supervisor = _worker(store, authority=authority)

    result = worker.execute()

    durable = store.jobs[job.id]
    assert result.outcome is RegistryPublisherIterationOutcome.FAILED
    assert durable.failure_code is RegistryPublicationFailureCode.CANDIDATE_INVALID
    assert authority.calls == [proposal.id, proposal.id]
    assert len(publisher.observe_calls) == 1
    assert publisher.publish_calls == []
    assert store.heartbeat_calls == 1


def test_cancellation_seen_at_prewrite_boundary_avoids_datahub_write() -> None:
    job = _approved_job()
    store = _WorkerStore(job)
    store.cancel_on_heartbeat = True
    worker, authority, publisher, _supervisor = _worker(store)

    result = worker.execute()

    assert result.outcome is RegistryPublisherIterationOutcome.CANCELLED
    assert store.jobs[job.id].status is RegistryPublicationJobStatus.CANCELLED
    assert authority.calls == [job.proposal.id]
    assert len(publisher.observe_calls) == 1
    assert publisher.publish_calls == []
    assert store.heartbeat_calls == 1
    assert store.acknowledge_calls == 1


def test_existing_exact_target_recovers_with_expired_authorization_and_no_authority_io() -> None:
    job = _approved_job(authorization_at=NOW - timedelta(minutes=20))
    store = _WorkerStore(job)
    publisher = _Publisher()
    publisher.observe_mode = "existing"
    authority = _Authority()
    authority.error = RegistryPublicationAuthorityError(
        RegistryPublicationFailureCode.CATALOG_STALE,
        "catalog changed after the external write",
    )
    worker, _authority, _publisher, _supervisor = _worker(
        store,
        authority=authority,
        publisher=publisher,
    )

    result = worker.execute()

    assert result.outcome is RegistryPublisherIterationOutcome.ACTIVATION_READY
    assert authority.calls == []
    assert publisher.publish_calls == []
    assert store.complete_calls == 1


def test_absent_target_with_expired_authorization_returns_to_approval_without_write() -> None:
    job = _approved_job(authorization_at=NOW - timedelta(minutes=20))
    store = _WorkerStore(job)
    worker, authority, publisher, _supervisor = _worker(store)

    result = worker.execute()

    durable = store.jobs[job.id]
    assert result.outcome is RegistryPublisherIterationOutcome.CANDIDATE_READY
    assert durable.status is RegistryPublicationJobStatus.AWAITING_APPROVAL
    assert durable.candidate is not None
    assert durable.authorization is None
    assert authority.calls == []
    assert publisher.publish_calls == []
    assert store.fail_calls == [RegistryPublicationFailureCode.AUTHORIZATION_EXPIRED]


@pytest.mark.parametrize("reason", ("permission_denied", "catalog_unavailable"))
def test_any_failed_post_write_result_schedules_readback_instead_of_terminal_failure(
    reason: str,
) -> None:
    job = _approved_job()
    store = _WorkerStore(job)
    publisher = _Publisher()
    publisher.publish_mode = reason
    worker, _authority, _publisher, _supervisor = _worker(store, publisher=publisher)

    result = worker.execute()

    durable = store.jobs[job.id]
    assert result.outcome is RegistryPublisherIterationOutcome.RETRY_SCHEDULED
    assert durable.status is RegistryPublicationJobStatus.RETRY_WAIT
    assert durable.failure_code is RegistryPublicationFailureCode.READBACK_REQUIRED
    assert store.fail_calls == [RegistryPublicationFailureCode.READBACK_REQUIRED]


def test_unexpected_publisher_crash_is_ambiguous_readback_not_blind_dead_letter() -> None:
    job = _approved_job()
    store = _WorkerStore(job)
    publisher = _Publisher()
    publisher.publish_error = RuntimeError("private SDK state after request send")
    worker, _authority, _publisher, _supervisor = _worker(store, publisher=publisher)

    result = worker.execute()

    assert result.outcome is RegistryPublisherIterationOutcome.RETRY_SCHEDULED
    assert result.failure_code is RegistryPublicationFailureCode.READBACK_REQUIRED
    assert store.jobs[job.id].status is RegistryPublicationJobStatus.RETRY_WAIT


def test_cancel_during_ambiguous_write_is_retained_then_recovered_readback_only() -> None:
    job = _approved_job()
    store = _WorkerStore(job)
    publisher = _Publisher()
    publisher.publish_mode = "catalog_unavailable"
    supervisor = _InlineSupervisor(store)
    supervisor.cancel_during_operation = True
    first_worker, authority, _publisher, _ = _worker(
        store,
        publisher=publisher,
        supervisor=supervisor,
    )

    first = first_worker.execute()

    retrying = store.jobs[job.id]
    assert first.outcome is RegistryPublisherIterationOutcome.RETRY_SCHEDULED
    assert retrying.status is RegistryPublicationJobStatus.RETRY_WAIT
    assert retrying.failure_code is RegistryPublicationFailureCode.READBACK_REQUIRED
    assert retrying.cancel_requested_at is not None
    assert len(publisher.publish_calls) == 1

    store.database_now = retrying.available_at
    publisher.observe_mode = "target_absent"
    second_worker, _authority, _publisher, _supervisor = _worker(
        store,
        authority=authority,
        publisher=publisher,
    )
    second = second_worker.execute()

    assert second.outcome is RegistryPublisherIterationOutcome.CANCELLED
    assert store.jobs[job.id].status is RegistryPublicationJobStatus.CANCELLED
    assert len(publisher.observe_calls) == 2
    assert len(publisher.publish_calls) == 1
    assert authority.calls == [job.proposal.id, job.proposal.id]


def test_cancelled_publish_with_absent_target_is_readback_only_then_cancelled() -> None:
    job = _approved_job()
    store = _WorkerStore(job)
    store.cancel_after_claim = True
    worker, authority, publisher, _supervisor = _worker(store)

    result = worker.execute()

    assert result.outcome is RegistryPublisherIterationOutcome.CANCELLED
    assert store.jobs[job.id].status is RegistryPublicationJobStatus.CANCELLED
    assert authority.calls == []
    assert len(publisher.observe_calls) == 1
    assert publisher.publish_calls == []
    assert store.acknowledge_calls == 1


def test_cancelled_publish_with_exact_target_completes_activation_ready() -> None:
    job = _approved_job()
    store = _WorkerStore(job)
    store.cancel_after_claim = True
    publisher = _Publisher()
    publisher.observe_mode = "existing"
    worker, authority, _publisher, _supervisor = _worker(store, publisher=publisher)

    result = worker.execute()

    assert result.outcome is RegistryPublisherIterationOutcome.ACTIVATION_READY
    assert store.jobs[job.id].status is RegistryPublicationJobStatus.ACTIVATION_READY
    assert authority.calls == []
    assert publisher.publish_calls == []
    assert store.complete_calls == 1


def test_store_outage_is_sanitized_before_capability_generation() -> None:
    store = _WorkerStore()
    store.raise_on_reap = True
    capability = _CapabilityFactory()
    worker, _authority, _publisher, _supervisor = _worker(
        store,
        capability_factory=capability,
    )

    with pytest.raises(RegistryPublisherWorkerError) as raised:
        worker.execute()

    assert raised.value.code is RegistryPublisherWorkerErrorCode.STORE_UNAVAILABLE
    assert "DSN" not in str(raised.value)
    assert capability.calls == 0


def _worker(
    store: _WorkerStore,
    *,
    authority: _Authority | None = None,
    publisher: _Publisher | None = None,
    supervisor: _InlineSupervisor | None = None,
    capability_factory: _CapabilityFactory | None = None,
    clock: _Clock | None = None,
) -> tuple[RunOneRegistryPublisherWorker, _Authority, _Publisher, _InlineSupervisor]:
    selected_authority = authority or _Authority()
    selected_publisher = publisher or _Publisher()
    selected_supervisor = supervisor or _InlineSupervisor(store)
    worker = RunOneRegistryPublisherWorker(
        jobs=store,  # type: ignore[arg-type]
        authority=selected_authority,
        publisher=selected_publisher,
        clock=clock or _Clock(store.database_now),
        capability_factory=capability_factory or _CapabilityFactory(),
        heartbeat_supervisor=selected_supervisor,  # type: ignore[arg-type]
        worker_id=WORKER_ID,
        lease_duration=timedelta(seconds=60),
        heartbeat_interval=timedelta(seconds=20),
    )
    return worker, selected_authority, selected_publisher, selected_supervisor


def _queued_job(
    proposal: PreparedRegistryPublicationProposal | None = None,
    *,
    submitted_at: datetime = NOW - timedelta(minutes=1),
) -> RegistryPublicationJob:
    selected = proposal or _proposal()
    return create_registry_publication_job(
        selected,
        submitted_by="publisher-a",
        submitted_at=submitted_at,
        idempotency_digest="d" * 64,
        request_fingerprint=registry_publication_request_fingerprint(
            selected,
            submitted_by="publisher-a",
        ),
    )


def _join_proposal(
    proposal_id: str,
) -> tuple[PreparedRegistryJoinProposal, GovernedSemanticRegistrySnapshot]:
    base_value, base_identity = _two_model_base()
    assert isinstance(base_value, GovernedSemanticRegistrySnapshot)
    proposal = PreparedRegistryJoinProposal.create(
        id=proposal_id,
        draft=_approved_draft(base_value, base_identity),
        prepared_by="publisher-worker-join",
        prepared_at=NOW + timedelta(minutes=4),
    )
    return proposal, base_value


def _approved_join_job(
    proposal: PreparedRegistryJoinProposal,
    base: GovernedSemanticRegistrySnapshot,
    *,
    authorized_at: datetime = NOW + timedelta(minutes=5, seconds=3),
) -> RegistryPublicationJob:
    queued = _queued_job(proposal, submitted_at=NOW + timedelta(minutes=5))
    candidate = assemble_join_change_registry_version(proposal, base=base)
    preparing = lease_registry_publication_job(
        queued,
        worker_id="publisher-worker-prepare",
        lease_capability="prepare-capability-" + ("p" * 48),
        acquired_at=NOW + timedelta(minutes=5, seconds=1),
        lease_duration=timedelta(seconds=60),
    )
    awaiting = record_registry_publication_candidate(
        preparing,
        candidate,
        worker_id="publisher-worker-prepare",
        lease_capability="prepare-capability-" + ("p" * 48),
        fencing_token=preparing.last_fencing_token,
        completed_at=NOW + timedelta(minutes=5, seconds=2),
    )
    return authorize_registry_publication_job(
        awaiting,
        _authorization(candidate, at=authorized_at),
        authorized_at=authorized_at,
    )


def _approved_job(
    *,
    authorization_at: datetime = NOW - timedelta(minutes=1),
) -> RegistryPublicationJob:
    proposal = _proposal()
    queued = _queued_job(
        proposal,
        submitted_at=authorization_at - timedelta(seconds=10),
    )
    candidate = assemble_publishable_registry_version(proposal, base=None)
    preparing = lease_registry_publication_job(
        queued,
        worker_id="publisher-worker-prepare",
        lease_capability="prepare-capability-" + ("p" * 48),
        acquired_at=authorization_at - timedelta(seconds=9),
        lease_duration=timedelta(seconds=60),
    )
    awaiting = record_registry_publication_candidate(
        preparing,
        candidate,
        worker_id="publisher-worker-prepare",
        lease_capability="prepare-capability-" + ("p" * 48),
        fencing_token=preparing.last_fencing_token,
        completed_at=authorization_at - timedelta(seconds=8),
    )
    authorization = _authorization(candidate, at=authorization_at)
    return authorize_registry_publication_job(
        awaiting,
        authorization,
        authorized_at=authorization_at,
    )


def _result(
    candidate: PublishableRegistryVersion,
    authorization: RegistryPublicationAuthorization,
    *,
    observed_at: datetime,
    mode: str,
) -> ObservedRegistryPublicationResult:
    succeeded = mode in {"published", "existing"}
    already_current = mode == "existing"
    reason_code = None if succeeded else mode
    status = "already_current" if already_current else ("published" if succeeded else "failed")
    observed_authorization_id = authorization.id if succeeded else None
    receipt = (
        PublicationReadbackReceipt(
            candidate_id=candidate.id,
            candidate_fingerprint=candidate.fingerprint,
            scope=candidate.scope,
            registry_version=candidate.registry.version,
            registry_fingerprint=candidate.registry.fingerprint,
            target=candidate.target,
            observed_authorization_id=authorization.id,
            related_asset_urns=observed_registry_related_asset_urns(candidate.registry),
            observed_at=observed_at,
        )
        if succeeded
        else None
    )
    outcome = (
        PublicationAuditOutcome.ALREADY_CURRENT
        if already_current
        else (PublicationAuditOutcome.SUCCEEDED if succeeded else PublicationAuditOutcome.FAILED)
    )
    return ObservedRegistryPublicationResult(
        attempt_authorization_id=authorization.id,
        observed_authorization_id=observed_authorization_id,
        status=status,
        receipt=receipt,
        reason_code=reason_code,
        audit_record=PublicationTargetAuditRecord(
            family=PublicationFamily.REGISTRY,
            operation="versioned_document_v2",
            target=candidate.target,
            approval_id=authorization.id,
            actor=authorization.actor_id,
            approved_at=authorization.approved_at,
            previous_fingerprint=(candidate.registry.fingerprint if already_current else None),
            new_fingerprint=candidate.registry.fingerprint,
            outcome=outcome,
            decision_ids=candidate.active_decision_ids,
            reason_code=reason_code,
        ),
    )
