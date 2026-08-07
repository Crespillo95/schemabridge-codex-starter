"""Application tests for queued aggregate semantic join profiling."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from schemabridge.adapters.postgres.relationships import (
    QueuedProposalRelationshipEvidenceAdapter,
)
from schemabridge.adapters.semantic_change.postgres_profile_queue import (
    QueuedRelationshipEvidencePort,
)
from schemabridge.application.join_demo import build_north_star_join_proposals
from schemabridge.application.ports.relationships import (
    RelationshipErrorCode,
    RelationshipWorkflowError,
)
from schemabridge.application.ports.semantic_profile_jobs import (
    BatchSemanticJoinProfileEvidencePort,
    SemanticJoinProfileQueueError,
    SemanticJoinProfileQueueErrorCode,
    SemanticJoinProfileRouteContext,
    SemanticJoinProfileSourceCancelled,
)
from schemabridge.application.semantic_profile_worker import (
    RunOneSemanticJoinProfile,
    SemanticJoinProfileWorkerError,
    SemanticJoinProfileWorkerErrorCode,
    SemanticJoinProfileWorkerOutcome,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    QueryCostBudget,
    SourceConnectorKind,
    SourceDialect,
    postgres_type_contract_fingerprint,
)
from schemabridge.domain.joins import JoinProposal, RelationshipProfile
from schemabridge.domain.semantic_profile_jobs import (
    SemanticJoinProfileFailureCode,
    SemanticJoinProfileJob,
    SemanticJoinProfileJobStatus,
    SemanticJoinProfileProposal,
    SemanticJoinProfileSubmission,
    SemanticJoinProfileTargetRef,
    claim_semantic_join_profile_job,
    complete_semantic_join_profile_job,
    fail_semantic_join_profile_job,
    heartbeat_semantic_join_profile_job,
    semantic_join_profile_proposal_fingerprint,
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
SCAN_ID = f"scan_{'a' * 64}"
CAPABILITY = "semantic-profile-capability-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _proposal() -> JoinProposal:
    return build_north_star_join_proposals()[0]


def _bound_proposal(
    connection_id: str = "warehouse-primary",
) -> SemanticJoinProfileProposal:
    return SemanticJoinProfileProposal(
        connection_id=CatalogConnectionId(connection_id),
        proposal=_proposal(),
    )


def _profile() -> RelationshipProfile:
    return RelationshipProfile(
        left_row_count=7,
        right_row_count=9,
        left_null_count=0,
        right_null_count=1,
        left_invalid_count=0,
        right_invalid_count=2,
        left_distinct_valid=7,
        right_distinct_valid=5,
        matching_distinct_keys=5,
        left_max_multiplicity=1,
        right_max_multiplicity=2,
        reader_user="schemabridge_reader",
        transaction_read_only=True,
        statement_timeout_ms=5_000,
    )


def _governed_target(
    *,
    workspace_id: str = "workspace-semantic",
    connection_id: str = "warehouse-primary",
    route_revision: int = 1,
) -> GovernedExecutionTarget:
    budget = QueryCostBudget(
        explain_timeout_ms=1_000,
        max_response_bytes=65_536,
        max_total_cost="10000",
        max_estimated_rows=10_000,
        max_plan_nodes=100,
        max_plan_depth=20,
        max_plan_width=1_024,
    )
    return GovernedExecutionTarget(
        workspace_id=workspace_id,
        connection_id=CatalogConnectionId(connection_id),
        connector_kind=SourceConnectorKind.POSTGRESQL,
        dialect=SourceDialect.POSTGRESQL,
        route_revision=route_revision,
        route_fingerprint=f"{route_revision:064x}",
        expected_reader="schemabridge_reader",
        source_identity_fingerprint="e" * 64,
        catalog_identity_fingerprint="f" * 64,
        type_contract_fingerprint=postgres_type_contract_fingerprint(),
        cost_budget=budget,
        cost_budget_fingerprint=budget.fingerprint,
    )


def _target_ref(
    *,
    workspace_id: str = "workspace-semantic",
    connection_id: str = "warehouse-primary",
    route_revision: int = 1,
) -> SemanticJoinProfileTargetRef:
    return SemanticJoinProfileTargetRef.from_target(
        _governed_target(
            workspace_id=workspace_id,
            connection_id=connection_id,
            route_revision=route_revision,
        )
    )


@dataclass
class FakeTargetResolver:
    route_revision: int = 1
    calls: list[tuple[str, CatalogConnectionId]] = field(default_factory=list)

    def resolve_current(
        self,
        *,
        workspace_id: str,
        connection_id: CatalogConnectionId,
    ) -> GovernedExecutionTarget:
        self.calls.append((workspace_id, connection_id))
        return _governed_target(
            workspace_id=workspace_id,
            connection_id=connection_id.root,
            route_revision=self.route_revision,
        )


@dataclass
class TickingClock:
    value: datetime = NOW

    def now(self) -> datetime:
        self.value += timedelta(seconds=1)
        return self.value


@dataclass
class FakeProfileQueue:
    state: SemanticJoinProfileJob | None
    clock: TickingClock
    return_cross_tenant: bool = False
    corrupt_heartbeat: bool = False
    heartbeat_error_at: int | None = None
    enqueue_calls: int = 0
    claim_calls: int = 0
    heartbeat_calls: int = 0
    complete_calls: int = 0
    fail_calls: int = 0

    def enqueue(
        self,
        workspace_id: str,
        scan_id: str,
        proposal: SemanticJoinProfileProposal,
        *,
        execution_target: SemanticJoinProfileTargetRef,
        requested_at: datetime,
        max_attempts: int = 5,
    ) -> SemanticJoinProfileSubmission:
        self.enqueue_calls += 1
        if self.state is None:
            self.state = SemanticJoinProfileJob.requested(
                workspace_id=workspace_id,
                scan_id=scan_id,
                proposal=proposal,
                execution_target=execution_target,
                connector_contract_version=1,
                requested_at=requested_at,
                max_attempts=max_attempts,
            )
            return SemanticJoinProfileSubmission(job=self.state, replayed=False)
        if self.return_cross_tenant:
            crossed = SemanticJoinProfileJob.requested(
                workspace_id="workspace-other",
                scan_id=scan_id,
                proposal=proposal,
                execution_target=_target_ref(
                    workspace_id="workspace-other",
                    connection_id=proposal.connection_id.root,
                ),
                connector_contract_version=1,
                requested_at=self.state.requested_at,
                max_attempts=max_attempts,
            )
            return SemanticJoinProfileSubmission(job=crossed, replayed=True)
        return SemanticJoinProfileSubmission(job=self.state, replayed=True)

    def load(
        self,
        workspace_id: str,
        scan_id: str,
        proposal_fingerprint: str,
    ) -> SemanticJoinProfileJob | None:
        if self.state is None:
            return None
        if (
            self.state.workspace_id != workspace_id
            or self.state.scan_id != scan_id
            or self.state.proposal_fingerprint != proposal_fingerprint
        ):
            return None
        return self.state

    def reclaim_expired(
        self,
        *,
        limit: int,
        retention: timedelta,
    ) -> int:
        assert 1 <= limit <= 1_000
        assert timedelta(days=1) <= retention <= timedelta(days=366)
        return 0

    def claim_next(
        self,
        *,
        worker_id: str,
        lease_capability: str,
        lease_duration: timedelta,
    ) -> SemanticJoinProfileJob | None:
        self.claim_calls += 1
        if self.state is None:
            return None
        now = self.clock.now()
        self.state = claim_semantic_join_profile_job(
            self.state,
            worker_id=worker_id,
            lease_capability=lease_capability,
            claimed_at=now,
            lease_expires_at=now + lease_duration,
        )
        return self.state

    def heartbeat(
        self,
        workspace_id: str,
        job_id: str,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
        lease_duration: timedelta,
    ) -> SemanticJoinProfileJob:
        assert self.state is not None
        assert (workspace_id, job_id) == (
            self.state.workspace_id,
            self.state.job_id,
        )
        self.heartbeat_calls += 1
        if self.heartbeat_error_at == self.heartbeat_calls:
            raise SemanticJoinProfileQueueError(
                SemanticJoinProfileQueueErrorCode.STATE_CONFLICT,
                "synthetic stale profile authority",
            )
        now = self.clock.now()
        self.state = heartbeat_semantic_join_profile_job(
            self.state,
            worker_id=worker_id,
            lease_capability=lease_capability,
            fencing_token=fencing_token,
            heartbeat_at=now,
            lease_expires_at=now + lease_duration,
        )
        if self.corrupt_heartbeat:
            return self.state.model_copy(update={"workspace_id": "workspace-other"})
        return self.state

    def complete(
        self,
        workspace_id: str,
        job_id: str,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
        profile: RelationshipProfile,
        retention: timedelta,
    ) -> SemanticJoinProfileJob:
        assert self.state is not None
        assert (workspace_id, job_id) == (
            self.state.workspace_id,
            self.state.job_id,
        )
        self.complete_calls += 1
        now = self.clock.now()
        self.state = complete_semantic_join_profile_job(
            self.state,
            worker_id=worker_id,
            lease_capability=lease_capability,
            fencing_token=fencing_token,
            profile=profile,
            completed_at=now,
            retain_until=now + retention,
        )
        return self.state

    def fail(
        self,
        workspace_id: str,
        job_id: str,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
        code: SemanticJoinProfileFailureCode,
        retry_delay: timedelta | None,
        retention: timedelta | None,
    ) -> SemanticJoinProfileJob:
        assert self.state is not None
        assert (workspace_id, job_id) == (
            self.state.workspace_id,
            self.state.job_id,
        )
        self.fail_calls += 1
        now = self.clock.now()
        self.state = fail_semantic_join_profile_job(
            self.state,
            worker_id=worker_id,
            lease_capability=lease_capability,
            fencing_token=fencing_token,
            code=code,
            failed_at=now,
            retry_at=None if retry_delay is None else now + retry_delay,
            retain_until=None if retention is None else now + retention,
        )
        return self.state


@dataclass
class BatchProfileQueue:
    clock: TickingClock
    jobs: dict[str, SemanticJoinProfileJob] = field(default_factory=dict)
    enqueue_calls: list[str] = field(default_factory=list)

    def enqueue(
        self,
        workspace_id: str,
        scan_id: str,
        proposal: SemanticJoinProfileProposal,
        *,
        execution_target: SemanticJoinProfileTargetRef,
        requested_at: datetime,
        max_attempts: int = 5,
    ) -> SemanticJoinProfileSubmission:
        fingerprint = semantic_join_profile_proposal_fingerprint(proposal)
        self.enqueue_calls.append(fingerprint)
        current = self.jobs.get(fingerprint)
        if current is None:
            current = SemanticJoinProfileJob.requested(
                workspace_id=workspace_id,
                scan_id=scan_id,
                proposal=proposal,
                execution_target=execution_target,
                connector_contract_version=1,
                requested_at=requested_at,
                max_attempts=max_attempts,
            )
            self.jobs[fingerprint] = current
            return SemanticJoinProfileSubmission(job=current, replayed=False)
        return SemanticJoinProfileSubmission(job=current, replayed=True)

    def load(
        self,
        workspace_id: str,
        scan_id: str,
        proposal_fingerprint: str,
    ) -> SemanticJoinProfileJob | None:
        current = self.jobs.get(proposal_fingerprint)
        if current is None:
            return None
        if current.workspace_id != workspace_id or current.scan_id != scan_id:
            return None
        return current


@dataclass
class FakeEvidence:
    profile_result: RelationshipProfile | None = None
    error_code: RelationshipErrorCode | None = None
    expected_connection_id: str = "warehouse-primary"
    calls: int = 0

    def profile_bound(
        self,
        proposal: SemanticJoinProfileProposal,
    ) -> RelationshipProfile:
        self.calls += 1
        assert proposal == _bound_proposal(self.expected_connection_id)
        if self.error_code is not None:
            raise RelationshipWorkflowError(self.error_code, "synthetic evidence failure")
        assert self.profile_result is not None
        return self.profile_result


@dataclass
class FakeEvidenceFactory:
    evidence: FakeEvidence
    contexts: list[SemanticJoinProfileRouteContext] = field(default_factory=list)

    def for_claim(
        self,
        context: SemanticJoinProfileRouteContext,
        *,
        should_continue: Callable[[], bool],
    ) -> FakeEvidence:
        self.contexts.append(context)
        return self.evidence


@dataclass
class BoundaryEvidence:
    should_continue: Callable[[], bool]
    source_calls: int = 0

    def profile_bound(
        self,
        proposal: SemanticJoinProfileProposal,
    ) -> RelationshipProfile:
        assert proposal == _bound_proposal()
        try:
            allowed = self.should_continue()
        except Exception:
            raise SemanticJoinProfileSourceCancelled("synthetic cancellation") from None
        if not allowed:
            raise SemanticJoinProfileSourceCancelled("synthetic cancellation")
        self.source_calls += 1
        return _profile()


@dataclass
class BoundaryEvidenceFactory:
    evidence: BoundaryEvidence | None = None

    def for_claim(
        self,
        context: SemanticJoinProfileRouteContext,
        *,
        should_continue: Callable[[], bool],
    ) -> BoundaryEvidence:
        assert context.workspace_id == "workspace-semantic"
        self.evidence = BoundaryEvidence(should_continue)
        return self.evidence


def _requested_job(
    clock: TickingClock,
    *,
    workspace_id: str = "workspace-semantic",
    max_attempts: int = 5,
    connection_id: str = "warehouse-primary",
) -> SemanticJoinProfileJob:
    return SemanticJoinProfileJob.requested(
        workspace_id=workspace_id,
        scan_id=SCAN_ID,
        proposal=_bound_proposal(connection_id),
        execution_target=_target_ref(
            workspace_id=workspace_id,
            connection_id=connection_id,
        ),
        connector_contract_version=1,
        requested_at=clock.value,
        max_attempts=max_attempts,
    )


def _worker(
    queue: FakeProfileQueue,
    evidence: FakeEvidence,
    clock: TickingClock,
    *,
    stop_requested: object | None = None,
) -> RunOneSemanticJoinProfile:
    callback = (lambda: False) if stop_requested is None else stop_requested
    assert callable(callback)
    factory = FakeEvidenceFactory(evidence)
    return RunOneSemanticJoinProfile(
        queue=queue,
        evidence_factory=factory,
        clock=clock,
        capability_factory=lambda: CAPABILITY,
        worker_id="semantic-profile-worker-a",
        stop_requested=callback,
    )


def test_worker_completes_only_aggregate_profile_with_two_heartbeats() -> None:
    clock = TickingClock()
    queue = FakeProfileQueue(_requested_job(clock), clock)
    evidence = FakeEvidence(profile_result=_profile())

    result = _worker(queue, evidence, clock).execute()

    assert result.outcome is SemanticJoinProfileWorkerOutcome.COMPLETED
    assert result.status is SemanticJoinProfileJobStatus.COMPLETED
    assert result.result_fingerprint is not None
    assert evidence.calls == 1
    assert queue.heartbeat_calls == 2
    assert queue.complete_calls == 1
    assert queue.fail_calls == 0
    assert queue.state is not None and queue.state.result is not None
    assert queue.state.result.profile == _profile()


def test_authority_drift_after_initial_heartbeat_opens_zero_source_connections() -> None:
    clock = TickingClock()
    queue = FakeProfileQueue(
        _requested_job(clock),
        clock,
        heartbeat_error_at=2,
    )
    factory = BoundaryEvidenceFactory()
    worker = RunOneSemanticJoinProfile(
        queue=queue,
        evidence_factory=factory,
        clock=clock,
        capability_factory=lambda: CAPABILITY,
        worker_id="semantic-profile-worker-a",
    )

    result = worker.execute()

    assert result.outcome is SemanticJoinProfileWorkerOutcome.STOPPED
    assert result.failure_code is SemanticJoinProfileFailureCode.SHUTDOWN_REQUESTED
    assert queue.heartbeat_calls == 2
    assert queue.complete_calls == 0
    assert factory.evidence is not None
    assert factory.evidence.source_calls == 0


def test_worker_routes_a_second_connection_from_the_claimed_target() -> None:
    clock = TickingClock()
    queue = FakeProfileQueue(
        _requested_job(clock, connection_id="warehouse-homonym"),
        clock,
    )
    evidence = FakeEvidence(
        profile_result=_profile(),
        expected_connection_id="warehouse-homonym",
    )

    result = _worker(queue, evidence, clock).execute()

    assert result.outcome is SemanticJoinProfileWorkerOutcome.COMPLETED
    assert evidence.calls == 1
    assert queue.complete_calls == 1
    assert queue.fail_calls == 0


def test_worker_routes_a_second_workspace_from_the_claimed_target() -> None:
    clock = TickingClock()
    queue = FakeProfileQueue(
        _requested_job(clock, workspace_id="workspace-other"),
        clock,
    )
    evidence = FakeEvidence(profile_result=_profile())

    result = _worker(queue, evidence, clock).execute()

    assert result.outcome is SemanticJoinProfileWorkerOutcome.COMPLETED
    assert evidence.calls == 1
    assert queue.heartbeat_calls == 2
    assert queue.complete_calls == 1
    assert queue.fail_calls == 0


def test_source_adapter_rejects_wrong_connection_before_opening_dsn() -> None:
    adapter = QueuedProposalRelationshipEvidenceAdapter(
        "postgresql://this-host-must-never-be-opened.invalid/source",
        expected_connection_id=CatalogConnectionId("warehouse-primary"),
    )

    with pytest.raises(RelationshipWorkflowError) as rejected:
        adapter.profile_bound(_bound_proposal("warehouse-homonym"))

    assert rejected.value.code is RelationshipErrorCode.EVIDENCE_NOT_ALLOWED


def test_retryable_source_outage_schedules_finite_retry() -> None:
    clock = TickingClock()
    queue = FakeProfileQueue(_requested_job(clock, max_attempts=2), clock)
    evidence = FakeEvidence(error_code=RelationshipErrorCode.EVIDENCE_UNAVAILABLE)

    first = _worker(queue, evidence, clock).execute()

    assert first.outcome is SemanticJoinProfileWorkerOutcome.RETRY_SCHEDULED
    assert first.status is SemanticJoinProfileJobStatus.RETRY_WAIT
    assert first.failure_code is SemanticJoinProfileFailureCode.SOURCE_UNAVAILABLE
    assert queue.state is not None
    assert queue.state.available_at > queue.state.updated_at

    clock.value = queue.state.available_at
    second = _worker(queue, evidence, clock).execute()
    assert second.outcome is SemanticJoinProfileWorkerOutcome.FAILED
    assert second.status is SemanticJoinProfileJobStatus.FAILED
    assert second.attempts == 2


def test_invalid_evidence_is_terminal_without_retry() -> None:
    clock = TickingClock()
    queue = FakeProfileQueue(_requested_job(clock), clock)
    evidence = FakeEvidence(error_code=RelationshipErrorCode.INVALID_EVIDENCE)

    result = _worker(queue, evidence, clock).execute()

    assert result.outcome is SemanticJoinProfileWorkerOutcome.FAILED
    assert result.failure_code is SemanticJoinProfileFailureCode.EVIDENCE_INVALID
    assert queue.state is not None
    assert queue.state.status is SemanticJoinProfileJobStatus.FAILED
    assert queue.state.attempts == 1


def test_corrupt_or_cross_scope_heartbeat_fails_closed() -> None:
    clock = TickingClock()
    queue = FakeProfileQueue(
        _requested_job(clock),
        clock,
        corrupt_heartbeat=True,
    )

    with pytest.raises(SemanticJoinProfileWorkerError) as rejected:
        _worker(queue, FakeEvidence(profile_result=_profile()), clock).execute()
    assert rejected.value.code is SemanticJoinProfileWorkerErrorCode.INVALID_RESPONSE
    assert queue.complete_calls == 0


def test_queued_reconciler_adapter_enqueues_then_consumes_exact_result() -> None:
    clock = TickingClock()
    queue = FakeProfileQueue(None, clock)
    adapter = QueuedRelationshipEvidencePort(
        queue=queue,
        target_resolver=FakeTargetResolver(),
        workspace_id="workspace-semantic",
        scan_id=SCAN_ID,
        clock=clock.now,
    )

    with pytest.raises(RelationshipWorkflowError) as pending:
        adapter.profile_bound(_bound_proposal())
    assert pending.value.code is RelationshipErrorCode.EVIDENCE_UNAVAILABLE
    assert queue.enqueue_calls == 1
    assert queue.state is not None

    claimed_at = clock.now()
    claimed = claim_semantic_join_profile_job(
        queue.state,
        worker_id="semantic-profile-worker-a",
        lease_capability=CAPABILITY,
        claimed_at=claimed_at,
        lease_expires_at=claimed_at + timedelta(seconds=120),
    )
    completed_at = claimed_at + timedelta(seconds=1)
    queue.state = complete_semantic_join_profile_job(
        claimed,
        worker_id="semantic-profile-worker-a",
        lease_capability=CAPABILITY,
        fencing_token=1,
        profile=_profile(),
        completed_at=completed_at,
        retain_until=completed_at + timedelta(days=30),
    )

    assert adapter.profile_bound(_bound_proposal()) == _profile()
    assert queue.enqueue_calls == 2
    assert queue.state.proposal_fingerprint == semantic_join_profile_proposal_fingerprint(
        _bound_proposal()
    )


def test_route_rotation_cannot_redirect_an_existing_profile_job() -> None:
    clock = TickingClock()
    queue = FakeProfileQueue(None, clock)
    target_resolver = FakeTargetResolver(route_revision=1)
    adapter = QueuedRelationshipEvidencePort(
        queue=queue,
        target_resolver=target_resolver,
        workspace_id="workspace-semantic",
        scan_id=SCAN_ID,
        clock=clock.now,
    )

    with pytest.raises(RelationshipWorkflowError) as pending:
        adapter.profile_bound(_bound_proposal())
    assert pending.value.code is RelationshipErrorCode.EVIDENCE_UNAVAILABLE
    assert queue.state is not None
    original_target = queue.state.execution_target

    target_resolver.route_revision = 2
    with pytest.raises(RelationshipWorkflowError) as stale:
        adapter.profile_bound(_bound_proposal())

    assert stale.value.code is RelationshipErrorCode.INVALID_EVIDENCE
    assert queue.state.execution_target == original_target


def test_queued_reconciler_batch_enqueues_more_than_five_joins_before_pending() -> None:
    clock = TickingClock()
    queue = BatchProfileQueue(clock)
    adapter = QueuedRelationshipEvidencePort(
        queue=queue,  # type: ignore[arg-type]
        target_resolver=FakeTargetResolver(),
        workspace_id="workspace-semantic",
        scan_id=SCAN_ID,
        clock=clock.now,
        max_attempts=5,
    )
    proposals = tuple(
        SemanticJoinProfileProposal(
            connection_id=CatalogConnectionId("warehouse-primary"),
            proposal=_proposal().model_copy(update={"id": f"semantic_batch_join_{index}"}),
        )
        for index in range(6)
    )

    assert isinstance(adapter, BatchSemanticJoinProfileEvidencePort)
    with pytest.raises(RelationshipWorkflowError) as pending:
        adapter.profiles_bound(proposals)

    assert pending.value.code is RelationshipErrorCode.EVIDENCE_UNAVAILABLE
    assert len(queue.enqueue_calls) == len(queue.jobs) == 6
    assert {job.max_attempts for job in queue.jobs.values()} == {5}

    for fingerprint, requested in tuple(queue.jobs.items()):
        claimed_at = clock.now()
        claimed = claim_semantic_join_profile_job(
            requested,
            worker_id="semantic-profile-worker-a",
            lease_capability=CAPABILITY,
            claimed_at=claimed_at,
            lease_expires_at=claimed_at + timedelta(seconds=120),
        )
        completed_at = clock.now()
        queue.jobs[fingerprint] = complete_semantic_join_profile_job(
            claimed,
            worker_id="semantic-profile-worker-a",
            lease_capability=CAPABILITY,
            fencing_token=claimed.fencing_token,
            profile=_profile(),
            completed_at=completed_at,
            retain_until=completed_at + timedelta(days=30),
        )

    assert adapter.profiles_bound(proposals) == (_profile(),) * 6
    assert len(queue.enqueue_calls) == 12


def test_queued_reconciler_adapter_rejects_cross_tenant_response() -> None:
    clock = TickingClock()
    queue = FakeProfileQueue(
        _requested_job(clock),
        clock,
        return_cross_tenant=True,
    )
    adapter = QueuedRelationshipEvidencePort(
        queue=queue,
        target_resolver=FakeTargetResolver(),
        workspace_id="workspace-semantic",
        scan_id=SCAN_ID,
        clock=clock.now,
    )

    with pytest.raises(RelationshipWorkflowError) as rejected:
        adapter.profile_bound(_bound_proposal())
    assert rejected.value.code is RelationshipErrorCode.INVALID_EVIDENCE


def test_worker_stop_before_claim_is_inert() -> None:
    clock = TickingClock()
    queue = FakeProfileQueue(_requested_job(clock), clock)
    result = _worker(
        queue,
        FakeEvidence(profile_result=_profile()),
        clock,
        stop_requested=lambda: True,
    ).execute()

    assert result.outcome is SemanticJoinProfileWorkerOutcome.STOPPED
    assert result.job_id is None
    assert queue.claim_calls == 0
