"""Adversarial unit tests for the authenticated M34 publication API use cases."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from tests.unit.test_registry_publication_v2 import _proposal

from schemabridge.application.authorization import AuthorizationError
from schemabridge.application.ports.identity_rotation import (
    IdentityRotationStoreError,
    IdentityRotationStoreErrorCode,
)
from schemabridge.application.ports.registry_publication import (
    RegistryPublicationJobMutation,
    RegistryPublicationStoreError,
    RegistryPublicationStoreErrorCode,
)
from schemabridge.application.registry_publication import (
    AuthorizeRegistryPublication,
    CancelRegistryPublication,
    InspectRegistryPublication,
    RegistryPublicationError,
    RegistryPublicationErrorCode,
    SubmitRegistryPublication,
)
from schemabridge.application.semantic_onboarding_authorization import (
    SemanticOnboardingAuthorizationPolicy,
)
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)
from schemabridge.domain.identity_rotation import IdentityAuthorizationScope
from schemabridge.domain.registry_publication import (
    RegistryPublicationAuthorization,
    RegistryPublicationAuthorizationConfirmation,
    assemble_publishable_registry_version,
)
from schemabridge.domain.registry_publication_jobs import (
    RegistryPublicationJob,
    RegistryPublicationJobStatus,
    authorize_registry_publication_job,
    create_registry_publication_job,
    lease_registry_publication_job,
    record_registry_publication_candidate,
    registry_publication_request_fingerprint,
    request_registry_publication_cancellation,
)
from schemabridge.domain.semantic_onboarding import PreparedSemanticOnboardingProposal

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
IDEMPOTENCY_KEY = "registry-publication-key-0001"
CAPABILITY = "lease-capability-" + ("x" * 48)
WORKER_ID = "publisher-worker-api"


class _Clock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


class _ProposalStore:
    def __init__(self, *proposals: PreparedSemanticOnboardingProposal) -> None:
        self.proposals = {(proposal.workspace_id, proposal.id): proposal for proposal in proposals}
        self.calls: list[tuple[str, str]] = []
        self.duplicate_aliases: tuple[str, ...] = ()
        self.error: RegistryPublicationStoreError | None = None

    def load_exact(
        self,
        workspace_id: str,
        proposal_id: str,
    ) -> PreparedSemanticOnboardingProposal | None:
        self.calls.append((workspace_id, proposal_id))
        if self.error is not None:
            raise self.error
        direct = self.proposals.get((workspace_id, proposal_id))
        if direct is not None:
            return direct
        if workspace_id in self.duplicate_aliases:
            return next(
                (item for item in self.proposals.values() if item.id == proposal_id),
                None,
            )
        return None


class _ApiStore:
    """Tenant-shaped in-memory fake that applies the pure queue transitions."""

    def __init__(self, *jobs: RegistryPublicationJob, now: datetime = NOW) -> None:
        self.jobs = {(job.scope.workspace_id, job.id): job for job in jobs}
        self.now = now
        self.submit_calls = 0
        self.load_calls: list[tuple[str, str]] = []
        self.authorize_calls = 0
        self.cancel_calls: list[str] = []
        self.duplicate_aliases: tuple[str, ...] = ()
        self.error: RegistryPublicationStoreError | None = None

    def submit(self, job: RegistryPublicationJob) -> RegistryPublicationJobMutation:
        self.submit_calls += 1
        if self.error is not None:
            raise self.error
        key = (job.scope.workspace_id, job.id)
        existing = self.jobs.get(key)
        if existing is not None:
            if (
                existing.idempotency_digest == job.idempotency_digest
                and existing.request_fingerprint == job.request_fingerprint
                and existing.submitted_by == job.submitted_by
            ):
                return RegistryPublicationJobMutation(job=existing, replayed=True)
            raise RegistryPublicationStoreError(
                RegistryPublicationStoreErrorCode.TARGET_RESERVED,
                "sensitive target reservation row",
            )
        self.jobs[key] = job
        return RegistryPublicationJobMutation(job=job)

    def load_by_idempotency(
        self,
        workspace_id: str,
        submitted_by: str,
        idempotency_digest: str,
    ) -> RegistryPublicationJob | None:
        if self.error is not None:
            raise self.error
        return next(
            (
                job
                for (workspace, _), job in self.jobs.items()
                if workspace == workspace_id
                and job.submitted_by == submitted_by
                and job.idempotency_digest == idempotency_digest
            ),
            None,
        )

    def load(self, workspace_id: str, job_id: str) -> RegistryPublicationJob | None:
        self.load_calls.append((workspace_id, job_id))
        if self.error is not None:
            raise self.error
        direct = self.jobs.get((workspace_id, job_id))
        if direct is not None:
            return direct
        if workspace_id in self.duplicate_aliases:
            return next((job for (_, key), job in self.jobs.items() if key == job_id), None)
        return None

    def authorize(
        self,
        job_id: str,
        *,
        workspace_id: str,
        expected_revision: int,
        authorization: RegistryPublicationAuthorization,
    ) -> RegistryPublicationJob:
        self.authorize_calls += 1
        job = self._exact(workspace_id, job_id)
        if job.revision != expected_revision:
            raise RegistryPublicationStoreError(
                RegistryPublicationStoreErrorCode.STATE_CONFLICT,
                "sensitive concurrent revision",
            )
        updated = authorize_registry_publication_job(
            job,
            authorization,
            authorized_at=self.now,
        )
        self.jobs[(workspace_id, job_id)] = updated
        return updated

    def request_cancellation(
        self,
        job_id: str,
        *,
        workspace_id: str,
        expected_revision: int,
        requested_by: str,
    ) -> RegistryPublicationJob:
        self.cancel_calls.append(requested_by)
        job = self._exact(workspace_id, job_id)
        if job.revision != expected_revision:
            raise RegistryPublicationStoreError(
                RegistryPublicationStoreErrorCode.STATE_CONFLICT,
                "sensitive concurrent revision",
            )
        updated = request_registry_publication_cancellation(job, requested_at=self.now)
        self.jobs[(workspace_id, job_id)] = updated
        return updated

    def _exact(self, workspace_id: str, job_id: str) -> RegistryPublicationJob:
        job = self.jobs.get((workspace_id, job_id))
        if job is None:
            raise RegistryPublicationStoreError(
                RegistryPublicationStoreErrorCode.NOT_FOUND,
                "sensitive missing row",
            )
        return job


class _Resolver:
    def __init__(
        self,
        *,
        old_workspace: str = "workspace-a",
        new_workspace: str = "workspace-new",
        old_actor: str = "publisher-old",
        new_actor: str = "publisher-new",
    ) -> None:
        self.old_workspace = old_workspace
        self.new_workspace = new_workspace
        self.old_actor = old_actor
        self.new_actor = new_actor
        self.duplicate_aliases = False
        self.incomplete = False

    def resolve_workspace_aliases(self, workspace_id: str) -> tuple[str, ...]:
        if workspace_id != self.new_workspace:
            raise IdentityRotationStoreError(
                IdentityRotationStoreErrorCode.CROSS_WORKSPACE,
                "sensitive inactive workspace",
            )
        if self.duplicate_aliases:
            return self.old_workspace, self.old_workspace, self.new_workspace
        return self.old_workspace, self.new_workspace

    def resolve_authorization_scopes(
        self,
        workspace_id: str,
        actor_id: str,
    ) -> tuple[IdentityAuthorizationScope, ...]:
        if workspace_id != self.new_workspace or actor_id != self.new_actor:
            raise IdentityRotationStoreError(
                IdentityRotationStoreErrorCode.CROSS_WORKSPACE,
                "sensitive inactive actor",
            )
        if self.incomplete:
            return (
                IdentityAuthorizationScope.model_construct(
                    workspace_id=self.new_workspace,
                    actor_id=self.new_actor,
                    key_version="v2",
                ),
            )
        return (
            IdentityAuthorizationScope.model_construct(
                workspace_id=self.old_workspace,
                actor_id=self.old_actor,
                key_version="v1",
            ),
            IdentityAuthorizationScope.model_construct(
                workspace_id=self.new_workspace,
                actor_id=self.new_actor,
                key_version="v2",
            ),
        )


def test_submit_reserves_exact_target_hashes_key_and_replays_without_raw_secret() -> None:
    proposal = _proposal()
    proposals = _ProposalStore(proposal)
    jobs = _ApiStore()
    use_case = SubmitRegistryPublication(
        proposals,
        jobs,
        SemanticOnboardingAuthorizationPolicy(),
        _Clock(),
    )

    submitted = _submit(use_case, _principal())
    replay = _submit(use_case, _principal())

    assert submitted.job.status is RegistryPublicationJobStatus.QUEUED
    assert not submitted.replayed
    assert replay.replayed
    assert replay.job == submitted.job
    assert jobs.submit_calls == 1
    assert submitted.job.idempotency_digest == hashlib.sha256(IDEMPOTENCY_KEY.encode()).hexdigest()
    encoded = json.dumps(submitted.job.model_dump(mode="json"), sort_keys=True)
    assert IDEMPOTENCY_KEY not in encoded
    assert IDEMPOTENCY_KEY not in repr(submitted)


@pytest.mark.parametrize(
    "role",
    (IdentityRole.ANALYST, IdentityRole.STEWARD, IdentityRole.AUDITOR),
)
def test_submit_denies_non_publishers_before_proposal_or_job_io(role: IdentityRole) -> None:
    proposal = _proposal()
    proposals = _ProposalStore(proposal)
    jobs = _ApiStore()
    use_case = SubmitRegistryPublication(
        proposals,
        jobs,
        SemanticOnboardingAuthorizationPolicy(),
        _Clock(),
    )

    with pytest.raises(AuthorizationError):
        _submit(use_case, _principal(role=role))

    assert proposals.calls == []
    assert jobs.submit_calls == 0


def test_submit_rejects_session_at_freshness_boundary_before_resource_io() -> None:
    proposal = _proposal()
    proposals = _ProposalStore(proposal)
    jobs = _ApiStore()
    use_case = SubmitRegistryPublication(
        proposals,
        jobs,
        SemanticOnboardingAuthorizationPolicy(),
        _Clock(),
    )
    stale = _principal(authenticated_at=NOW - timedelta(minutes=15))

    with pytest.raises(RegistryPublicationError) as raised:
        _submit(use_case, stale)

    assert raised.value.code is RegistryPublicationErrorCode.STALE_SESSION
    assert proposals.calls == []
    assert jobs.submit_calls == 0


def test_proposal_absence_and_fingerprint_mismatch_share_non_disclosing_error() -> None:
    proposal = _proposal()
    proposals = _ProposalStore(proposal)
    jobs = _ApiStore()
    use_case = SubmitRegistryPublication(
        proposals,
        jobs,
        SemanticOnboardingAuthorizationPolicy(),
        _Clock(),
    )

    with pytest.raises(RegistryPublicationError) as absent:
        use_case.execute(
            _principal(),
            proposal_id="proposal-not-present",
            confirmed_proposal_fingerprint="f" * 64,
            idempotency_key=IDEMPOTENCY_KEY,
        )
    with pytest.raises(RegistryPublicationError) as mismatch:
        use_case.execute(
            _principal(),
            proposal_id=proposal.id,
            confirmed_proposal_fingerprint="f" * 64,
            idempotency_key=IDEMPOTENCY_KEY,
        )

    assert absent.value.code is RegistryPublicationErrorCode.UNAVAILABLE
    assert mismatch.value.code is RegistryPublicationErrorCode.UNAVAILABLE
    assert str(absent.value) == str(mismatch.value)
    assert jobs.submit_calls == 0


def test_authorize_binds_exact_candidate_and_never_outlives_principal_session() -> None:
    proposal = _proposal()
    awaiting = _awaiting_job(proposal)
    jobs = _ApiStore(awaiting)
    principal = _principal(expires_at=NOW + timedelta(minutes=3))
    use_case = AuthorizeRegistryPublication(
        jobs,
        SemanticOnboardingAuthorizationPolicy(),
        _Clock(),
    )
    assert awaiting.candidate is not None

    approved = use_case.execute(
        principal,
        awaiting.id,
        expected_revision=awaiting.revision,
        confirmed_candidate_fingerprint=awaiting.candidate.fingerprint,
        confirmation=(
            RegistryPublicationAuthorizationConfirmation.PUBLISH_EXACT_OBSERVED_REGISTRY_VERSION
        ),
    )

    assert approved.status is RegistryPublicationJobStatus.APPROVED
    assert approved.authorization is not None
    assert approved.authorization.candidate_fingerprint == awaiting.candidate.fingerprint
    assert approved.authorization.actor_id == principal.actor_id
    assert approved.authorization.expires_at == principal.expires_at


def test_authorize_rejects_stale_session_before_loading_the_candidate() -> None:
    awaiting = _awaiting_job(_proposal())
    jobs = _ApiStore(awaiting)
    use_case = AuthorizeRegistryPublication(
        jobs,
        SemanticOnboardingAuthorizationPolicy(),
        _Clock(),
    )
    assert awaiting.candidate is not None

    with pytest.raises(RegistryPublicationError) as raised:
        use_case.execute(
            _principal(authenticated_at=NOW - timedelta(minutes=15)),
            awaiting.id,
            expected_revision=awaiting.revision,
            confirmed_candidate_fingerprint=awaiting.candidate.fingerprint,
            confirmation=(
                RegistryPublicationAuthorizationConfirmation.PUBLISH_EXACT_OBSERVED_REGISTRY_VERSION
            ),
        )

    assert raised.value.code is RegistryPublicationErrorCode.STALE_SESSION
    assert jobs.load_calls == []
    assert jobs.authorize_calls == 0


@pytest.mark.parametrize("change", ("revision", "fingerprint"))
def test_authorize_rejects_stale_candidate_confirmation_without_mutation(change: str) -> None:
    awaiting = _awaiting_job(_proposal())
    jobs = _ApiStore(awaiting)
    use_case = AuthorizeRegistryPublication(
        jobs,
        SemanticOnboardingAuthorizationPolicy(),
        _Clock(),
    )
    assert awaiting.candidate is not None

    with pytest.raises(RegistryPublicationError) as raised:
        use_case.execute(
            _principal(),
            awaiting.id,
            expected_revision=(
                awaiting.revision + 1 if change == "revision" else awaiting.revision
            ),
            confirmed_candidate_fingerprint=(
                "f" * 64 if change == "fingerprint" else awaiting.candidate.fingerprint
            ),
            confirmation=(
                RegistryPublicationAuthorizationConfirmation.PUBLISH_EXACT_OBSERVED_REGISTRY_VERSION
            ),
        )

    assert raised.value.code is RegistryPublicationErrorCode.CONFLICT
    assert jobs.authorize_calls == 0


def test_cancel_is_cooperative_and_repeated_request_preserves_the_exact_lease() -> None:
    queued = _new_job(_proposal())
    leased = lease_registry_publication_job(
        queued,
        worker_id=WORKER_ID,
        lease_capability=CAPABILITY,
        acquired_at=NOW - timedelta(seconds=1),
        lease_duration=timedelta(seconds=60),
    )
    jobs = _ApiStore(leased)
    use_case = CancelRegistryPublication(
        jobs,
        SemanticOnboardingAuthorizationPolicy(),
        _Clock(),
    )

    requested = use_case.execute(
        _principal(),
        leased.id,
        expected_revision=leased.revision,
    )
    repeated = use_case.execute(
        _principal(),
        leased.id,
        expected_revision=requested.revision,
    )

    assert requested.status is RegistryPublicationJobStatus.CANCEL_REQUESTED
    assert repeated == requested
    assert repeated.lease == leased.lease
    assert jobs.cancel_calls == ["publisher-a", "publisher-a"]


def test_auditor_can_inspect_but_cannot_authorize_or_cancel() -> None:
    awaiting = _awaiting_job(_proposal())
    jobs = _ApiStore(awaiting)
    policy = SemanticOnboardingAuthorizationPolicy()
    auditor = _principal(role=IdentityRole.AUDITOR, actor_id="auditor-a")

    inspected = InspectRegistryPublication(jobs, policy, _Clock()).execute(
        auditor,
        awaiting.id,
    )
    assert inspected == awaiting
    assert awaiting.candidate is not None

    with pytest.raises(AuthorizationError):
        AuthorizeRegistryPublication(jobs, policy, _Clock()).execute(
            auditor,
            awaiting.id,
            expected_revision=awaiting.revision,
            confirmed_candidate_fingerprint=awaiting.candidate.fingerprint,
            confirmation=(
                RegistryPublicationAuthorizationConfirmation.PUBLISH_EXACT_OBSERVED_REGISTRY_VERSION
            ),
        )
    with pytest.raises(AuthorizationError):
        CancelRegistryPublication(jobs, policy, _Clock()).execute(
            auditor,
            awaiting.id,
            expected_revision=awaiting.revision,
        )

    assert jobs.authorize_calls == 0
    assert jobs.cancel_calls == []


def test_historical_alias_routes_submit_inspect_authorize_and_cancel_to_exact_actor_pair() -> None:
    proposal = _proposal()
    resolver = _Resolver()
    policy = SemanticOnboardingAuthorizationPolicy(resolver)
    proposals = _ProposalStore(proposal)
    jobs = _ApiStore()
    current = _principal(
        actor_id=resolver.new_actor,
        workspace_id=resolver.new_workspace,
    )
    submit = SubmitRegistryPublication(proposals, jobs, policy, _Clock())

    submitted = _submit(submit, current)
    assert submitted.job.scope.workspace_id == resolver.old_workspace
    assert submitted.job.submitted_by == resolver.old_actor

    awaiting = _awaiting_from_job(submitted.job)
    jobs.jobs[(awaiting.scope.workspace_id, awaiting.id)] = awaiting
    inspected = InspectRegistryPublication(jobs, policy, _Clock()).execute(
        current,
        awaiting.id,
    )
    assert inspected == awaiting
    assert awaiting.candidate is not None

    approved = AuthorizeRegistryPublication(jobs, policy, _Clock()).execute(
        current,
        awaiting.id,
        expected_revision=awaiting.revision,
        confirmed_candidate_fingerprint=awaiting.candidate.fingerprint,
        confirmation=(
            RegistryPublicationAuthorizationConfirmation.PUBLISH_EXACT_OBSERVED_REGISTRY_VERSION
        ),
    )
    assert approved.authorization is not None
    assert approved.authorization.actor_id == resolver.old_actor

    cancelled = CancelRegistryPublication(jobs, policy, _Clock()).execute(
        current,
        approved.id,
        expected_revision=approved.revision,
    )
    assert cancelled.status is RegistryPublicationJobStatus.CANCELLED
    assert jobs.cancel_calls[-1] == resolver.old_actor


def test_alias_collision_fails_closed_without_job_mutation() -> None:
    proposal = _proposal()
    resolver = _Resolver()
    proposals = _ProposalStore(proposal)
    proposals.duplicate_aliases = (resolver.new_workspace,)
    jobs = _ApiStore()
    use_case = SubmitRegistryPublication(
        proposals,
        jobs,
        SemanticOnboardingAuthorizationPolicy(resolver),
        _Clock(),
    )

    with pytest.raises(RegistryPublicationError) as raised:
        _submit(
            use_case,
            _principal(
                actor_id=resolver.new_actor,
                workspace_id=resolver.new_workspace,
            ),
        )

    assert raised.value.code is RegistryPublicationErrorCode.UNAVAILABLE
    assert jobs.submit_calls == 0


def test_store_failure_is_sanitized_at_api_boundary() -> None:
    proposal = _proposal()
    proposals = _ProposalStore(proposal)
    proposals.error = RegistryPublicationStoreError(
        RegistryPublicationStoreErrorCode.UNAVAILABLE,
        "private database host and row contents",
    )
    use_case = SubmitRegistryPublication(
        proposals,
        _ApiStore(),
        SemanticOnboardingAuthorizationPolicy(),
        _Clock(),
    )

    with pytest.raises(RegistryPublicationError) as raised:
        _submit(use_case, _principal())

    assert raised.value.code is RegistryPublicationErrorCode.SERVICE_UNAVAILABLE
    assert "private" not in str(raised.value)
    assert "database" not in str(raised.value)


def _principal(
    *,
    role: IdentityRole = IdentityRole.PUBLISHER,
    actor_id: str = "publisher-a",
    workspace_id: str = "workspace-a",
    authenticated_at: datetime = NOW - timedelta(minutes=1),
    expires_at: datetime = NOW + timedelta(hours=1),
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        actor_id=actor_id,
        workspace_id=workspace_id,
        roles=frozenset({role}),
        authentication_method=AuthenticationMethod.OIDC,
        authenticated_at=authenticated_at,
        expires_at=expires_at,
    )


def _submit(
    use_case: SubmitRegistryPublication,
    principal: AuthenticatedPrincipal,
) -> RegistryPublicationJobMutation:
    proposal = _proposal()
    return use_case.execute(
        principal,
        proposal_id=proposal.id,
        confirmed_proposal_fingerprint=proposal.fingerprint,
        idempotency_key=IDEMPOTENCY_KEY,
    )


def _new_job(
    proposal: PreparedSemanticOnboardingProposal,
    *,
    submitted_by: str = "publisher-a",
    submitted_at: datetime = NOW - timedelta(seconds=10),
) -> RegistryPublicationJob:
    return create_registry_publication_job(
        proposal,
        submitted_by=submitted_by,
        submitted_at=submitted_at,
        idempotency_digest="d" * 64,
        request_fingerprint=registry_publication_request_fingerprint(
            proposal,
            submitted_by=submitted_by,
        ),
    )


def _awaiting_job(proposal: PreparedSemanticOnboardingProposal) -> RegistryPublicationJob:
    return _awaiting_from_job(_new_job(proposal))


def _awaiting_from_job(job: RegistryPublicationJob) -> RegistryPublicationJob:
    candidate = assemble_publishable_registry_version(job.proposal, base=None)
    leased = lease_registry_publication_job(
        job,
        worker_id=WORKER_ID,
        lease_capability=CAPABILITY,
        acquired_at=job.submitted_at + timedelta(seconds=1),
        lease_duration=timedelta(seconds=60),
    )
    return record_registry_publication_candidate(
        leased,
        candidate,
        worker_id=WORKER_ID,
        lease_capability=CAPABILITY,
        fencing_token=leased.last_fencing_token,
        completed_at=job.submitted_at + timedelta(seconds=2),
    )
