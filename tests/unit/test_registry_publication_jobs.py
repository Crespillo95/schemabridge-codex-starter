"""Pure durable queue lifecycle tests for M34 publication jobs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from tests.unit.test_registry_join_changes import _approved_draft, _two_model_base
from tests.unit.test_registry_model_changes import (
    _joined_base,
    _prepared_change,
    _replacement,
    _replacement_base,
)
from tests.unit.test_registry_publication_v2 import OPAQUE_ORDERS_URN, _proposal

from schemabridge.domain.registry_changes import PreparedRegistryJoinProposal
from schemabridge.domain.registry_model_changes import (
    PreparedRegistryModelReplacementProposal,
    RegistryIncidentJoinPreservation,
)
from schemabridge.domain.registry_publication import (
    PublicationReadbackReceipt,
    RegistryPublicationAuthorization,
    RegistryPublicationAuthorizationConfirmation,
    assemble_publishable_registry_version,
    observed_registry_related_asset_urns,
)
from schemabridge.domain.registry_publication_jobs import (
    PreparedRegistryPublicationProposal,
    RegistryPublicationFailureCode,
    RegistryPublicationJob,
    RegistryPublicationJobStatus,
    RegistryPublicationTransitionCode,
    RegistryPublicationTransitionError,
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

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
CAPABILITY_A = "a" * 64
CAPABILITY_B = "b" * 64
FP_A = "a" * 64


def test_full_two_phase_lifecycle_finishes_activation_ready_without_activation() -> None:
    proposal = _proposal()
    job = _job(proposal)
    candidate = assemble_publishable_registry_version(proposal, base=None)

    preparing = lease_registry_publication_job(
        job,
        worker_id="publisher-worker-a",
        lease_capability=CAPABILITY_A,
        acquired_at=NOW,
        lease_duration=timedelta(seconds=60),
    )
    awaiting = record_registry_publication_candidate(
        preparing,
        candidate,
        worker_id="publisher-worker-a",
        lease_capability=CAPABILITY_A,
        fencing_token=1,
        completed_at=NOW + timedelta(seconds=5),
    )
    authorization = _authorization(candidate, at=NOW + timedelta(seconds=10))
    approved = authorize_registry_publication_job(
        awaiting,
        authorization,
        authorized_at=NOW + timedelta(seconds=10),
    )
    publishing = lease_registry_publication_job(
        approved,
        worker_id="publisher-worker-b",
        lease_capability=CAPABILITY_B,
        acquired_at=NOW + timedelta(seconds=11),
        lease_duration=timedelta(seconds=60),
    )
    completed = complete_registry_publication_job(
        publishing,
        _receipt(candidate, authorization, at=NOW + timedelta(seconds=15)),
        worker_id="publisher-worker-b",
        lease_capability=CAPABILITY_B,
        fencing_token=2,
        completed_at=NOW + timedelta(seconds=15),
    )

    assert job.status is RegistryPublicationJobStatus.QUEUED
    assert awaiting.status is RegistryPublicationJobStatus.AWAITING_APPROVAL
    assert approved.status is RegistryPublicationJobStatus.APPROVED
    assert completed.status is RegistryPublicationJobStatus.ACTIVATION_READY
    assert completed.receipt is not None
    assert completed.receipt.related_asset_urns == (OPAQUE_ORDERS_URN,)
    assert not hasattr(completed, "active_pointer")


def test_target_reservation_id_is_scope_version_based_not_proposal_based() -> None:
    first = _proposal()
    competing = _proposal(
        proposal_id="proposal-competing-v1",
        draft_id="competing-onboarding",
    )

    first_job = _job(first)
    competing_job = _job(competing)

    assert first_job.id == competing_job.id
    assert first_job.proposal.fingerprint != competing_job.proposal.fingerprint
    assert first_job.request_fingerprint != competing_job.request_fingerprint


def test_stale_fence_capability_and_expired_lease_are_rejected() -> None:
    leased = lease_registry_publication_job(
        _job(_proposal()),
        worker_id="publisher-worker-a",
        lease_capability=CAPABILITY_A,
        acquired_at=NOW,
        lease_duration=timedelta(seconds=30),
    )

    with pytest.raises(RegistryPublicationTransitionError) as wrong_token:
        heartbeat_registry_publication_job(
            leased,
            worker_id="publisher-worker-a",
            lease_capability=CAPABILITY_B,
            fencing_token=1,
            heartbeat_at=NOW + timedelta(seconds=5),
            lease_duration=timedelta(seconds=30),
        )
    with pytest.raises(RegistryPublicationTransitionError) as wrong_fence:
        heartbeat_registry_publication_job(
            leased,
            worker_id="publisher-worker-a",
            lease_capability=CAPABILITY_A,
            fencing_token=2,
            heartbeat_at=NOW + timedelta(seconds=5),
            lease_duration=timedelta(seconds=30),
        )
    with pytest.raises(RegistryPublicationTransitionError) as expired:
        heartbeat_registry_publication_job(
            leased,
            worker_id="publisher-worker-a",
            lease_capability=CAPABILITY_A,
            fencing_token=1,
            heartbeat_at=NOW + timedelta(seconds=30),
            lease_duration=timedelta(seconds=30),
        )

    assert wrong_token.value.code is RegistryPublicationTransitionCode.LEASE_MISMATCH
    assert wrong_fence.value.code is RegistryPublicationTransitionCode.FENCING_MISMATCH
    assert expired.value.code is RegistryPublicationTransitionCode.LEASE_EXPIRED


def test_cancellation_is_immediate_before_claim_and_cooperative_while_leased() -> None:
    queued = _job(_proposal())
    cancelled = request_registry_publication_cancellation(
        queued,
        requested_at=NOW,
    )
    assert cancelled.status is RegistryPublicationJobStatus.CANCELLED

    leased = lease_registry_publication_job(
        queued,
        worker_id="publisher-worker-a",
        lease_capability=CAPABILITY_A,
        acquired_at=NOW,
        lease_duration=timedelta(seconds=60),
    )
    requested = request_registry_publication_cancellation(
        leased,
        requested_at=NOW + timedelta(seconds=1),
    )
    replay = request_registry_publication_cancellation(
        requested,
        requested_at=NOW + timedelta(seconds=2),
    )
    acknowledged = acknowledge_registry_publication_cancellation(
        requested,
        worker_id="publisher-worker-a",
        lease_capability=CAPABILITY_A,
        fencing_token=1,
        cancelled_at=NOW + timedelta(seconds=2),
    )

    assert requested.status is RegistryPublicationJobStatus.CANCEL_REQUESTED
    assert replay == requested
    assert acknowledged.status is RegistryPublicationJobStatus.CANCELLED


def test_transient_failure_retries_then_dead_letters_at_bound() -> None:
    current = _job(_proposal(), max_attempts=2)
    current = lease_registry_publication_job(
        current,
        worker_id="publisher-worker-a",
        lease_capability=CAPABILITY_A,
        acquired_at=NOW,
        lease_duration=timedelta(seconds=60),
    )
    current = fail_registry_publication_job(
        current,
        RegistryPublicationFailureCode.DATAHUB_UNAVAILABLE,
        worker_id="publisher-worker-a",
        lease_capability=CAPABILITY_A,
        fencing_token=1,
        failed_at=NOW + timedelta(seconds=1),
    )
    assert current.status is RegistryPublicationJobStatus.RETRY_WAIT
    assert current.available_at == NOW + timedelta(seconds=6)

    current = lease_registry_publication_job(
        current,
        worker_id="publisher-worker-b",
        lease_capability=CAPABILITY_B,
        acquired_at=current.available_at,
        lease_duration=timedelta(seconds=60),
    )
    current = fail_registry_publication_job(
        current,
        RegistryPublicationFailureCode.DATAHUB_UNAVAILABLE,
        worker_id="publisher-worker-b",
        lease_capability=CAPABILITY_B,
        fencing_token=2,
        failed_at=NOW + timedelta(seconds=7),
    )

    assert current.status is RegistryPublicationJobStatus.DEAD_LETTERED
    assert current.failure_code is RegistryPublicationFailureCode.DATAHUB_UNAVAILABLE


def test_successful_readback_wins_over_late_cancellation_after_external_write() -> None:
    proposal = _proposal()
    candidate = assemble_publishable_registry_version(proposal, base=None)
    preparing = lease_registry_publication_job(
        _job(proposal),
        worker_id="publisher-worker-a",
        lease_capability=CAPABILITY_A,
        acquired_at=NOW,
        lease_duration=timedelta(seconds=60),
    )
    awaiting = record_registry_publication_candidate(
        preparing,
        candidate,
        worker_id="publisher-worker-a",
        lease_capability=CAPABILITY_A,
        fencing_token=1,
        completed_at=NOW + timedelta(seconds=1),
    )
    authorization = _authorization(candidate, at=NOW + timedelta(seconds=2))
    approved = authorize_registry_publication_job(
        awaiting,
        authorization,
        authorized_at=NOW + timedelta(seconds=2),
    )
    publishing = lease_registry_publication_job(
        approved,
        worker_id="publisher-worker-b",
        lease_capability=CAPABILITY_B,
        acquired_at=NOW + timedelta(seconds=3),
        lease_duration=timedelta(seconds=60),
    )
    cancellation = request_registry_publication_cancellation(
        publishing,
        requested_at=NOW + timedelta(seconds=4),
    )

    recovered = complete_registry_publication_job(
        cancellation,
        _receipt(candidate, authorization, at=NOW + timedelta(seconds=5)),
        worker_id="publisher-worker-b",
        lease_capability=CAPABILITY_B,
        fencing_token=2,
        completed_at=NOW + timedelta(seconds=5),
    )

    assert recovered.status is RegistryPublicationJobStatus.ACTIVATION_READY
    assert recovered.cancel_requested_at is None


def test_expired_publication_lease_requires_readback_even_after_cancellation() -> None:
    proposal = _proposal()
    candidate = assemble_publishable_registry_version(proposal, base=None)
    preparing = lease_registry_publication_job(
        _job(proposal),
        worker_id="publisher-worker-a",
        lease_capability=CAPABILITY_A,
        acquired_at=NOW,
        lease_duration=timedelta(seconds=10),
    )
    awaiting = record_registry_publication_candidate(
        preparing,
        candidate,
        worker_id="publisher-worker-a",
        lease_capability=CAPABILITY_A,
        fencing_token=1,
        completed_at=NOW + timedelta(seconds=1),
    )
    approved = authorize_registry_publication_job(
        awaiting,
        _authorization(candidate, at=NOW + timedelta(seconds=2)),
        authorized_at=NOW + timedelta(seconds=2),
    )
    publishing = lease_registry_publication_job(
        approved,
        worker_id="publisher-worker-b",
        lease_capability=CAPABILITY_B,
        acquired_at=NOW + timedelta(seconds=3),
        lease_duration=timedelta(seconds=10),
    )
    cancellation = request_registry_publication_cancellation(
        publishing,
        requested_at=NOW + timedelta(seconds=4),
    )

    recovered = reap_expired_registry_publication_lease(
        cancellation,
        expired_at=NOW + timedelta(seconds=13),
    )

    assert recovered.status is RegistryPublicationJobStatus.RETRY_WAIT
    assert recovered.failure_code is RegistryPublicationFailureCode.READBACK_REQUIRED
    assert recovered.authorization is not None
    assert recovered.cancel_requested_at == NOW + timedelta(seconds=4)


def test_job_round_trip_accepts_closed_join_proposal_without_changing_m33_payload() -> None:
    historical = _job(_proposal())
    historical_payload = historical.model_dump(mode="json")
    base, base_identity = _two_model_base()
    proposal = PreparedRegistryJoinProposal.create(
        id="join-change-job-v3",
        draft=_approved_draft(base, base_identity),
        prepared_by="publisher-job",
        prepared_at=NOW + timedelta(minutes=4),
    )
    joined = _job(proposal, submitted_at=NOW + timedelta(minutes=5))

    restored = RegistryPublicationJob.model_validate(joined.model_dump(mode="json"))
    historical_restored = RegistryPublicationJob.model_validate(historical_payload)

    assert isinstance(restored.proposal, PreparedRegistryJoinProposal)
    assert restored == joined
    assert historical_restored == historical
    assert historical_restored.model_dump(mode="json") == historical_payload
    assert historical_restored.request_fingerprint == historical.request_fingerprint


def test_job_rejects_a_join_payload_with_a_mismatched_proposal_kind() -> None:
    base, base_identity = _two_model_base()
    proposal = PreparedRegistryJoinProposal.create(
        id="join-change-kind-v3",
        draft=_approved_draft(base, base_identity),
        prepared_by="publisher-kind",
        prepared_at=NOW + timedelta(minutes=4),
    )
    job = _job(proposal, submitted_at=NOW + timedelta(minutes=5))
    forged = proposal.model_dump(mode="json")
    forged["proposal_kind"] = "onboarding_additive"

    with pytest.raises(ValidationError, match="proposal_kind"):
        RegistryPublicationJob.model_validate({**job.model_dump(mode="python"), "proposal": forged})


def test_job_round_trip_accepts_closed_model_replacement_proposal() -> None:
    base, identity = _joined_base()
    replacement_base = _replacement_base(base, identity)
    proposal = _prepared_change(
        base=replacement_base,
        replacement=_replacement(identity),
        incident_changes=(
            RegistryIncidentJoinPreservation(base=replacement_base.incident_joins[0]),
        ),
    )
    job = _job(proposal, submitted_at=proposal.prepared_at + timedelta(minutes=1))

    restored = RegistryPublicationJob.model_validate(job.model_dump(mode="json"))

    assert isinstance(restored.proposal, PreparedRegistryModelReplacementProposal)
    assert restored == job
    assert restored.proposal.base_registry == identity
    assert restored.proposal.target_registry_version == base.version + 1


def _job(
    proposal: PreparedRegistryPublicationProposal,
    *,
    max_attempts: int = 5,
    submitted_at: datetime = NOW,
) -> RegistryPublicationJob:
    request_fingerprint = registry_publication_request_fingerprint(
        proposal,
        submitted_by="publisher-a",
    )
    return create_registry_publication_job(
        proposal,
        submitted_by="publisher-a",
        submitted_at=submitted_at,
        idempotency_digest=FP_A,
        request_fingerprint=request_fingerprint,
        max_attempts=max_attempts,
    )


def _authorization(candidate: object, *, at: datetime) -> RegistryPublicationAuthorization:
    from schemabridge.domain.registry_publication import PublishableRegistryVersion

    assert isinstance(candidate, PublishableRegistryVersion)
    return RegistryPublicationAuthorization.create(
        candidate,
        actor_id="publisher-a",
        authenticated_at=at - timedelta(minutes=1),
        approved_at=at,
        expires_at=at + timedelta(minutes=10),
        confirmation=(
            RegistryPublicationAuthorizationConfirmation.PUBLISH_EXACT_OBSERVED_REGISTRY_VERSION
        ),
    )


def _receipt(
    candidate: object,
    authorization: RegistryPublicationAuthorization,
    *,
    at: datetime,
) -> PublicationReadbackReceipt:
    from schemabridge.domain.registry_publication import PublishableRegistryVersion

    assert isinstance(candidate, PublishableRegistryVersion)
    return PublicationReadbackReceipt(
        candidate_id=candidate.id,
        candidate_fingerprint=candidate.fingerprint,
        scope=candidate.scope,
        registry_version=candidate.registry.version,
        registry_fingerprint=candidate.registry.fingerprint,
        target=candidate.target,
        observed_authorization_id=authorization.id,
        related_asset_urns=observed_registry_related_asset_urns(candidate.registry),
        observed_at=at,
    )
