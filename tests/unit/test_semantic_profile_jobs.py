"""Pure lifecycle and protected-payload tests for semantic join-profile jobs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from schemabridge.application.join_demo import build_north_star_join_proposals
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.joins import (
    JoinProposal,
    NormalizedJoinKey,
    RelationshipProfile,
)
from schemabridge.domain.semantic_profile_jobs import (
    SemanticJoinProfileFailureCode,
    SemanticJoinProfileJob,
    SemanticJoinProfileJobStatus,
    SemanticJoinProfileProposal,
    SemanticJoinProfileTargetRef,
    SemanticJoinProfileTransitionError,
    SemanticJoinProfileTransitionErrorCode,
    canonical_semantic_join_profile_proposal_json,
    claim_semantic_join_profile_job,
    complete_semantic_join_profile_job,
    fail_semantic_join_profile_job,
    heartbeat_semantic_join_profile_job,
    reclaim_expired_semantic_join_profile_job,
    semantic_join_profile_claim_matches,
    semantic_join_profile_job_id,
    semantic_join_profile_proposal_fingerprint,
    semantic_join_profile_retry_delay,
    semantic_join_profile_source_matches,
    validate_semantic_join_profile_proposal,
    validate_semantic_join_profile_workspace_id,
)
from schemabridge.domain.transformations import (
    MapValuesStep,
    TransformationPlan,
    ValueMapEntry,
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
SCAN_ID = f"scan_{'a' * 64}"
CAPABILITY = "semantic-profile-capability-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
OTHER_CAPABILITY = "other-profile-capability-9876543210-zyxwvutsrqponmlkjihgfedcba"


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


def _target(
    *,
    workspace_id: str = "workspace-semantic",
    connection_id: str = "warehouse-primary",
    route_revision: int = 1,
) -> SemanticJoinProfileTargetRef:
    return SemanticJoinProfileTargetRef(
        workspace_id=workspace_id,
        connection_id=CatalogConnectionId(connection_id),
        route_revision=route_revision,
        route_fingerprint=f"{route_revision:064x}",
        target_fingerprint=f"{route_revision + 100:064x}",
    )


def _requested(
    *,
    workspace_id: str = "workspace-semantic",
    max_attempts: int = 5,
) -> SemanticJoinProfileJob:
    return SemanticJoinProfileJob.requested(
        workspace_id=workspace_id,
        scan_id=SCAN_ID,
        proposal=_bound_proposal(),
        execution_target=_target(workspace_id=workspace_id),
        connector_contract_version=1,
        requested_at=NOW,
        max_attempts=max_attempts,
    )


def _claimed(
    *,
    max_attempts: int = 5,
) -> SemanticJoinProfileJob:
    return claim_semantic_join_profile_job(
        _requested(max_attempts=max_attempts),
        worker_id="semantic-profile-worker-a",
        lease_capability=CAPABILITY,
        claimed_at=NOW + timedelta(seconds=1),
        lease_expires_at=NOW + timedelta(seconds=121),
    )


def test_request_identity_is_exact_idempotent_and_tenant_bound() -> None:
    first = _requested()
    replay = _requested()
    other_tenant = _requested(workspace_id="workspace-other")

    assert first == replay
    assert first.job_id == semantic_join_profile_job_id(
        first.workspace_id,
        first.scan_id,
        first.proposal_fingerprint,
    )
    assert first.proposal_fingerprint == semantic_join_profile_proposal_fingerprint(
        first.bound_proposal
    )
    assert first.job_id != other_tenant.job_id
    assert first.status is SemanticJoinProfileJobStatus.REQUESTED
    assert first.attempts == first.fencing_token == 0


def test_source_binding_requires_exact_workspace_and_connection_pair() -> None:
    job = _requested(workspace_id="workspace-semantic")

    assert semantic_join_profile_source_matches(
        job,
        expected_workspace_id="workspace-semantic",
        expected_connection_id=CatalogConnectionId("warehouse-primary"),
    )
    assert not semantic_join_profile_source_matches(
        job,
        expected_workspace_id="workspace-other",
        expected_connection_id=CatalogConnectionId("warehouse-primary"),
    )
    assert not semantic_join_profile_source_matches(
        job,
        expected_workspace_id="workspace-semantic",
        expected_connection_id=CatalogConnectionId("warehouse-homonym"),
    )
    with pytest.raises(ValueError, match="bounded nonblank"):
        validate_semantic_join_profile_workspace_id(" workspace-semantic")


def test_proposal_payload_is_canonical_bounded_and_rejects_value_maps() -> None:
    proposal = _bound_proposal()
    canonical = canonical_semantic_join_profile_proposal_json(proposal)

    assert canonical.startswith('{"connection_id":"warehouse-primary","proposal":')
    assert "sql" not in canonical.casefold()
    assert "source_row" not in canonical.casefold()
    assert canonical == canonical_semantic_join_profile_proposal_json(
        SemanticJoinProfileProposal.model_validate(proposal.model_dump(mode="json"))
    )

    mapped_join = proposal.proposal.model_copy(
        update={
            "left_key": NormalizedJoinKey(
                logical_field=proposal.proposal.left_key.logical_field,
                physical_field=proposal.proposal.left_key.physical_field,
                transformation_plan=TransformationPlan(
                    steps=(
                        MapValuesStep(
                            entries=(ValueMapEntry(source="private-key", target="canonical"),)
                        ),
                    )
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="value maps"):
        validate_semantic_join_profile_proposal(
            SemanticJoinProfileProposal.model_construct(
                connection_id=proposal.connection_id,
                proposal=mapped_join,
            )
        )


def test_homonymous_proposals_in_different_connections_have_distinct_identity() -> None:
    primary = _bound_proposal("warehouse-primary")
    homonym = _bound_proposal("warehouse-homonym")

    assert primary.proposal == homonym.proposal
    assert primary.connection_id != homonym.connection_id
    assert semantic_join_profile_proposal_fingerprint(
        primary
    ) != semantic_join_profile_proposal_fingerprint(homonym)
    assert (
        SemanticJoinProfileJob.requested(
            workspace_id="workspace-semantic",
            scan_id=SCAN_ID,
            proposal=primary,
            execution_target=_target(),
            connector_contract_version=1,
            requested_at=NOW,
        ).job_id
        != SemanticJoinProfileJob.requested(
            workspace_id="workspace-semantic",
            scan_id=SCAN_ID,
            proposal=homonym,
            execution_target=_target(connection_id="warehouse-homonym"),
            connector_contract_version=1,
            requested_at=NOW,
        ).job_id
    )


def test_claim_heartbeat_completion_and_terminal_immutability() -> None:
    claimed = _claimed()
    assert claimed.lease is not None
    assert claimed.lease.capability_digest != CAPABILITY
    assert claimed.attempts == claimed.fencing_token == 1
    assert semantic_join_profile_claim_matches(
        claimed,
        worker_id="semantic-profile-worker-a",
        lease_capability=CAPABILITY,
        fencing_token=1,
        at=NOW + timedelta(seconds=2),
    )

    heartbeated = heartbeat_semantic_join_profile_job(
        claimed,
        worker_id="semantic-profile-worker-a",
        lease_capability=CAPABILITY,
        fencing_token=1,
        heartbeat_at=NOW + timedelta(seconds=10),
        lease_expires_at=NOW + timedelta(seconds=130),
    )
    completed = complete_semantic_join_profile_job(
        heartbeated,
        worker_id="semantic-profile-worker-a",
        lease_capability=CAPABILITY,
        fencing_token=1,
        profile=_profile(),
        completed_at=NOW + timedelta(seconds=11),
        retain_until=NOW + timedelta(days=30),
    )

    assert completed.status is SemanticJoinProfileJobStatus.COMPLETED
    assert completed.result is not None
    assert completed.result.profile == _profile()
    persisted = completed.result.profile.model_dump(mode="json")
    assert set(persisted) == {
        "left_row_count",
        "right_row_count",
        "left_null_count",
        "right_null_count",
        "left_invalid_count",
        "right_invalid_count",
        "left_distinct_valid",
        "right_distinct_valid",
        "matching_distinct_keys",
        "left_max_multiplicity",
        "right_max_multiplicity",
        "declared_relationship",
        "reader_user",
        "transaction_read_only",
        "statement_timeout_ms",
    }
    assert not {"sql", "parameters", "source_row", "sample_value"} & set(persisted)

    with pytest.raises(SemanticJoinProfileTransitionError) as terminal:
        claim_semantic_join_profile_job(
            completed,
            worker_id="semantic-profile-worker-b",
            lease_capability=OTHER_CAPABILITY,
            claimed_at=NOW + timedelta(days=31),
            lease_expires_at=NOW + timedelta(days=31, seconds=60),
        )
    assert terminal.value.code is SemanticJoinProfileTransitionErrorCode.TERMINAL_IMMUTABLE


def test_stale_capability_fence_and_expired_lease_fail_closed() -> None:
    claimed = _claimed()

    for capability, fence, at, expected in (
        (
            OTHER_CAPABILITY,
            1,
            NOW + timedelta(seconds=2),
            SemanticJoinProfileTransitionErrorCode.LEASE_MISMATCH,
        ),
        (
            CAPABILITY,
            2,
            NOW + timedelta(seconds=2),
            SemanticJoinProfileTransitionErrorCode.FENCING_MISMATCH,
        ),
        (
            CAPABILITY,
            1,
            NOW + timedelta(seconds=121),
            SemanticJoinProfileTransitionErrorCode.LEASE_EXPIRED,
        ),
    ):
        with pytest.raises(SemanticJoinProfileTransitionError) as rejected:
            complete_semantic_join_profile_job(
                claimed,
                worker_id="semantic-profile-worker-a",
                lease_capability=capability,
                fencing_token=fence,
                profile=_profile(),
                completed_at=at,
                retain_until=at + timedelta(days=1),
            )
        assert rejected.value.code is expected


def test_retry_is_finite_and_expired_crash_lease_is_reclaimable() -> None:
    claimed = _claimed(max_attempts=2)
    failed = fail_semantic_join_profile_job(
        claimed,
        worker_id="semantic-profile-worker-a",
        lease_capability=CAPABILITY,
        fencing_token=1,
        code=SemanticJoinProfileFailureCode.SOURCE_UNAVAILABLE,
        failed_at=NOW + timedelta(seconds=2),
        retry_at=NOW + timedelta(seconds=7),
        retain_until=None,
    )
    assert failed.status is SemanticJoinProfileJobStatus.RETRY_WAIT
    assert failed.available_at == NOW + timedelta(seconds=7)
    assert semantic_join_profile_retry_delay(1) == timedelta(seconds=5)

    reclaimed_claim = claim_semantic_join_profile_job(
        failed,
        worker_id="semantic-profile-worker-b",
        lease_capability=OTHER_CAPABILITY,
        claimed_at=NOW + timedelta(seconds=8),
        lease_expires_at=NOW + timedelta(seconds=128),
    )
    exhausted = reclaim_expired_semantic_join_profile_job(
        reclaimed_claim,
        reclaimed_at=NOW + timedelta(seconds=128),
        retain_until=NOW + timedelta(days=30),
    )
    assert exhausted.status is SemanticJoinProfileJobStatus.FAILED
    assert exhausted.attempts == exhausted.fencing_token == 2
    assert exhausted.failure is not None
    assert exhausted.failure.code is SemanticJoinProfileFailureCode.LEASE_EXPIRED


def test_tampered_fingerprints_and_naive_time_are_rejected() -> None:
    requested = _requested()
    with pytest.raises(ValidationError):
        SemanticJoinProfileJob.model_validate(
            {
                **requested.model_dump(mode="python"),
                "proposal_fingerprint": "f" * 64,
            }
        )
    with pytest.raises(ValidationError):
        SemanticJoinProfileJob.requested(
            workspace_id="workspace-semantic",
            scan_id=SCAN_ID,
            proposal=_bound_proposal(),
            execution_target=_target(),
            connector_contract_version=1,
            requested_at=datetime(2026, 7, 24, 12, 0),
        )
