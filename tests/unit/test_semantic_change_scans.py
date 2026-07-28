"""Pure lifecycle tests for durable semantic-change scan requests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from schemabridge.domain.semantic_change_scans import (
    SemanticChangeScanCompletion,
    SemanticChangeScanFailureCode,
    SemanticChangeScanFailureDisposition,
    SemanticChangeScanRequest,
    SemanticChangeScanSourceKind,
    SemanticChangeScanStatus,
    SemanticChangeScanSupersession,
    SemanticChangeScanTransitionError,
    SemanticChangeScanTransitionErrorCode,
    claim_semantic_change_scan,
    complete_semantic_change_scan,
    digest_semantic_change_scan_capability,
    fail_semantic_change_scan,
    heartbeat_semantic_change_scan,
    reclaim_expired_semantic_change_scan,
    semantic_change_scan_claim_matches,
    semantic_change_scan_retry_delay,
    supersede_semantic_change_scan,
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
CAPABILITY = "semantic-capability-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
OTHER_CAPABILITY = "other-capability-9876543210-zyxwvutsrqponmlkjihgfedcba"


def _registry_request(
    *,
    fingerprint: str = "a" * 64,
    max_attempts: int = 5,
) -> SemanticChangeScanRequest:
    return SemanticChangeScanRequest.requested(
        workspace_id="workspace-semantic",
        source_kind=SemanticChangeScanSourceKind.REGISTRY_POINTER,
        source_event_key="transition-semantic-7",
        source_fingerprint=fingerprint,
        catalog_scope="synthetic-demo",
        registry_id="synthetic_enterprise",
        registry_generation=7,
        requested_at=NOW,
        max_attempts=max_attempts,
    )


def _catalog_request() -> SemanticChangeScanRequest:
    return SemanticChangeScanRequest.requested(
        workspace_id="workspace-semantic",
        source_kind=SemanticChangeScanSourceKind.CATALOG_GENERATION,
        source_event_key="warehouse-primary:12",
        source_fingerprint="b" * 64,
        catalog_scope="synthetic-demo",
        registry_id="synthetic_enterprise",
        connection_id="warehouse-primary",
        base_catalog_generation=11,
        observed_catalog_generation=12,
        requested_at=NOW,
    )


def _claim(
    request: SemanticChangeScanRequest,
    *,
    claimed_at: datetime | None = None,
) -> SemanticChangeScanRequest:
    instant = claimed_at or NOW + timedelta(seconds=1)
    return claim_semantic_change_scan(
        request,
        reconciler_id="semantic-reconciler-a",
        lease_capability=CAPABILITY,
        claimed_at=instant,
        lease_expires_at=instant + timedelta(seconds=60),
    )


def test_requested_sources_are_exact_and_invalid_cross_kind_shapes_fail() -> None:
    registry = _registry_request()
    catalog = _catalog_request()

    assert registry.scan_id == f"scan_{'a' * 64}"
    assert registry.status is SemanticChangeScanStatus.REQUESTED
    assert registry.max_attempts == 5
    assert catalog.connection_id == "warehouse-primary"
    assert catalog.catalog_scope == "synthetic-demo"
    assert catalog.registry_id == "synthetic_enterprise"
    assert catalog.registry_generation is None
    assert catalog.observed_catalog_generation == 12

    with pytest.raises(ValidationError):
        registry.model_copy(
            update={"connection_id": "warehouse-primary"},
        ).__class__.model_validate(
            {
                **registry.model_dump(mode="python"),
                "connection_id": "warehouse-primary",
            }
        )
    with pytest.raises(ValidationError):
        SemanticChangeScanRequest.requested(
            workspace_id="workspace-semantic",
            source_kind=SemanticChangeScanSourceKind.CATALOG_GENERATION,
            source_event_key="warehouse-primary:12",
            source_fingerprint="c" * 64,
            catalog_scope="synthetic-demo",
            registry_id="synthetic_enterprise",
            connection_id="warehouse-primary",
            base_catalog_generation=12,
            observed_catalog_generation=12,
            requested_at=NOW,
        )
    with pytest.raises(ValidationError):
        SemanticChangeScanRequest.requested(
            workspace_id="workspace-semantic",
            source_kind=SemanticChangeScanSourceKind.CATALOG_GENERATION,
            source_event_key="warehouse-primary:13",
            source_fingerprint="d" * 64,
            connection_id="warehouse-primary",
            base_catalog_generation=12,
            observed_catalog_generation=13,
            requested_at=NOW,
        )


def test_claim_heartbeat_completion_and_exact_replay_are_fenced() -> None:
    claimed = _claim(_registry_request())
    assert claimed.status is SemanticChangeScanStatus.LEASED
    assert claimed.lease is not None
    assert claimed.lease.capability_digest == digest_semantic_change_scan_capability(CAPABILITY)
    assert CAPABILITY not in claimed.model_dump_json()
    assert semantic_change_scan_claim_matches(
        claimed,
        reconciler_id="semantic-reconciler-a",
        lease_capability=CAPABILITY,
        fencing_token=1,
        at=NOW + timedelta(seconds=2),
    )
    assert not semantic_change_scan_claim_matches(
        claimed,
        reconciler_id="semantic-reconciler-a",
        lease_capability=OTHER_CAPABILITY,
        fencing_token=1,
        at=NOW + timedelta(seconds=2),
    )

    heartbeat = heartbeat_semantic_change_scan(
        claimed,
        reconciler_id="semantic-reconciler-a",
        lease_capability=CAPABILITY,
        fencing_token=1,
        heartbeat_at=NOW + timedelta(seconds=10),
        lease_expires_at=NOW + timedelta(seconds=90),
    )
    assert heartbeat.lease is not None
    assert heartbeat.lease.acquired_at == NOW + timedelta(seconds=1)
    assert heartbeat.lease.heartbeat_at == NOW + timedelta(seconds=10)

    completion = SemanticChangeScanCompletion(
        report_id=f"report_{'d' * 64}",
        report_fingerprint="d" * 64,
        completed_at=NOW + timedelta(seconds=20),
    )
    completed = complete_semantic_change_scan(
        heartbeat,
        reconciler_id="semantic-reconciler-a",
        lease_capability=CAPABILITY,
        fencing_token=1,
        completion=completion,
        retain_until=NOW + timedelta(days=30),
    )
    replay = complete_semantic_change_scan(
        completed,
        reconciler_id="semantic-reconciler-a",
        lease_capability=OTHER_CAPABILITY,
        fencing_token=99,
        completion=completion,
        retain_until=NOW + timedelta(days=30),
    )

    assert completed.status is SemanticChangeScanStatus.COMPLETED
    assert completed.completion == completion
    assert replay == completed
    with pytest.raises(SemanticChangeScanTransitionError) as terminal:
        fail_semantic_change_scan(
            completed,
            reconciler_id="semantic-reconciler-a",
            lease_capability=CAPABILITY,
            fencing_token=1,
            code=SemanticChangeScanFailureCode.EVIDENCE_UNAVAILABLE,
            failed_at=NOW + timedelta(seconds=21),
        )
    assert terminal.value.code is SemanticChangeScanTransitionErrorCode.TERMINAL_IMMUTABLE


def test_retry_backoff_is_finite_and_final_attempt_fails_terminally() -> None:
    first = _claim(_registry_request(max_attempts=2))
    first_failed_at = NOW + timedelta(seconds=2)
    retry = fail_semantic_change_scan(
        first,
        reconciler_id="semantic-reconciler-a",
        lease_capability=CAPABILITY,
        fencing_token=1,
        code=SemanticChangeScanFailureCode.EVIDENCE_UNAVAILABLE,
        failed_at=first_failed_at,
        retry_at=first_failed_at + semantic_change_scan_retry_delay(1),
    )
    assert retry.status is SemanticChangeScanStatus.RETRY_WAIT
    assert retry.failure is not None
    assert retry.failure.disposition is SemanticChangeScanFailureDisposition.RETRY

    second = _claim(retry, claimed_at=retry.available_at + timedelta(seconds=1))
    second_failed_at = second.updated_at + timedelta(seconds=1)
    terminal = fail_semantic_change_scan(
        second,
        reconciler_id="semantic-reconciler-a",
        lease_capability=CAPABILITY,
        fencing_token=2,
        code=SemanticChangeScanFailureCode.EVIDENCE_UNAVAILABLE,
        failed_at=second_failed_at,
        retain_until=second_failed_at + timedelta(days=30),
    )
    assert terminal.status is SemanticChangeScanStatus.FAILED
    assert terminal.failure is not None
    assert terminal.failure.disposition is SemanticChangeScanFailureDisposition.FAIL


def test_crash_reclaim_retries_and_rejects_the_stale_owner() -> None:
    claimed = _claim(_registry_request())
    assert claimed.lease is not None
    reclaimed_at = claimed.lease.expires_at
    reclaimed = reclaim_expired_semantic_change_scan(
        claimed,
        reclaimed_at=reclaimed_at,
    )

    assert reclaimed.status is SemanticChangeScanStatus.RETRY_WAIT
    assert reclaimed.failure is not None
    assert reclaimed.failure.code is SemanticChangeScanFailureCode.LEASE_EXPIRED
    assert not semantic_change_scan_claim_matches(
        reclaimed,
        reconciler_id="semantic-reconciler-a",
        lease_capability=CAPABILITY,
        fencing_token=1,
        at=reclaimed_at,
    )

    replacement = _claim(
        reclaimed,
        claimed_at=reclaimed.available_at + timedelta(seconds=1),
    )
    assert replacement.fencing_token == 2
    with pytest.raises(SemanticChangeScanTransitionError) as stale:
        complete_semantic_change_scan(
            replacement,
            reconciler_id="semantic-reconciler-a",
            lease_capability=CAPABILITY,
            fencing_token=1,
            completion=SemanticChangeScanCompletion(
                report_id=f"report_{'e' * 64}",
                report_fingerprint="e" * 64,
                completed_at=replacement.updated_at + timedelta(seconds=1),
            ),
            retain_until=replacement.updated_at + timedelta(days=30),
        )
    assert stale.value.code is SemanticChangeScanTransitionErrorCode.FENCING_MISMATCH


def test_waiting_or_owned_scan_can_be_superseded_by_one_exact_newer_request() -> None:
    request = _registry_request()
    successor = SemanticChangeScanSupersession(
        scan_id=f"scan_{'f' * 64}",
        source_fingerprint="f" * 64,
    )
    superseded = supersede_semantic_change_scan(
        request,
        superseded_by=successor,
        superseded_at=NOW + timedelta(seconds=1),
        retain_until=NOW + timedelta(days=30),
    )
    replay = supersede_semantic_change_scan(
        superseded,
        superseded_by=successor,
        superseded_at=NOW + timedelta(seconds=2),
        retain_until=NOW + timedelta(days=31),
    )

    assert superseded.status is SemanticChangeScanStatus.SUPERSEDED
    assert superseded.superseded_by == successor
    assert replay == superseded

    leased = _claim(_registry_request(fingerprint="9" * 64))
    with pytest.raises(SemanticChangeScanTransitionError) as missing_owner:
        supersede_semantic_change_scan(
            leased,
            superseded_by=successor,
            superseded_at=NOW + timedelta(seconds=2),
            retain_until=NOW + timedelta(days=30),
        )
    assert missing_owner.value.code is SemanticChangeScanTransitionErrorCode.LEASE_MISMATCH
