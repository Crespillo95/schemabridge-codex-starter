from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from schemabridge.adapters.evaluation import query_studio_live_attestation as attestation

_SIGNING_KEY = b"m27-attestation-test-key-ABCDEFGHIJKLMNOPQRSTUVWXYZ-0123456789"
_WRONG_KEY = b"m27-attestation-wrong-key-ABCDEFGHIJKLMNOPQRSTUVWXYZ-9876543210"
_WORKSPACE_ID = "sb_workspace_v1_" + ("a" * 64)
_POLICY_UPDATED_AT = datetime(2026, 7, 27, 16, 38, 54, 5_484, tzinfo=UTC)
_FIRST_CREATED_AT = datetime(2026, 7, 27, 16, 40, 13, 742_917, tzinfo=UTC)
_ATTEMPT_MATRIX = (
    ("qualification", "revenue-by-order-date-and-category", 1, 982, 73, 2_542, 2_538),
    ("full", "secondary-holders-by-registration-date", 1, 1_002, 109, 2_732, 2_728),
    ("full", "secondary-holders-by-registration-date", 2, 995, 73, 1_475, 1_472),
    ("full", "secondary-holders-by-registration-date", 3, 1_002, 73, 1_554, 1_549),
    ("full", "active-customers-by-country", 1, 967, 73, 1_125, 1_123),
    ("full", "active-customers-by-country", 2, 971, 73, 1_332, 1_329),
    ("full", "active-customers-by-country", 3, 966, 73, 1_701, 1_699),
    ("full", "active-products-by-category", 1, 977, 73, 1_270, 1_267),
    ("full", "active-products-by-category", 2, 977, 73, 1_073, 1_070),
    ("full", "active-products-by-category", 3, 980, 73, 944, 941),
    ("full", "delivered-orders-by-day", 1, 985, 73, 4_866, 4_862),
    ("full", "delivered-orders-by-day", 2, 981, 73, 1_120, 1_117),
    ("full", "delivered-orders-by-day", 3, 983, 73, 1_247, 1_244),
    ("full", "revenue-by-order-date-and-category", 1, 982, 73, 1_402, 1_398),
    ("full", "revenue-by-order-date-and-category", 2, 983, 73, 1_480, 1_476),
    ("full", "revenue-by-order-date-and-category", 3, 982, 73, 4_153, 4_148),
)


def _campaign_attempts() -> tuple[attestation.CampaignAttemptWitness, ...]:
    return tuple(
        attestation.CampaignAttemptWitness(
            sequence=index,
            case_identity_sha256=attestation._attempt_identity(
                source=source,
                case_id=case_id,
                repetition=repetition,
                suite="core",
            ),
            stage="interpretation",
            model_snapshot=attestation.CANONICAL_MODEL_SNAPSHOT,
            configuration_fingerprint=(attestation.CANONICAL_CONFIGURATION_FINGERPRINT),
            input_tokens=input_tokens,
            output_reasoning_tokens=output_tokens,
            evaluator_adapter_duration_ms=evaluator_duration,
        )
        for index, (
            source,
            case_id,
            repetition,
            input_tokens,
            output_tokens,
            evaluator_duration,
            _provider_duration,
        ) in enumerate(_ATTEMPT_MATRIX, start=1)
    )


def _campaign() -> attestation.CampaignEvidenceFacts:
    attempts = _campaign_attempts()
    return attestation.CampaignEvidenceFacts(
        whole_file_sha256=attestation.CANONICAL_CAMPAIGN_FILE_SHA256,
        logical_report_sha256=attestation.CANONICAL_CAMPAIGN_REPORT_SHA256,
        plan_version=attestation.CANONICAL_CAMPAIGN_PLAN_VERSION,
        provider_free_preflight_pipeline_sha256=(attestation.CANONICAL_PREFLIGHT_PIPELINE_SHA256),
        evaluation_outcome="selected",
        selected_model=attestation.CANONICAL_MODEL_SNAPSHOT,
        policy_version=attestation.QUALIFICATION_POLICY_VERSION,
        configuration_fingerprint=(attestation.CANONICAL_CONFIGURATION_FINGERPRINT),
        provider_attempts=attestation.CANONICAL_PROVIDER_ATTEMPTS,
        input_tokens=attestation.CANONICAL_INPUT_TOKENS,
        output_reasoning_tokens=(attestation.CANONICAL_OUTPUT_REASONING_TOKENS),
        evaluator_adapter_duration_ms=(attestation.CANONICAL_EVALUATOR_ADAPTER_DURATION_MS),
        attempts=attempts,
        ordered_execution_witness_digest_sha256=(
            attestation.CANONICAL_CAMPAIGN_ORDERED_EXECUTION_WITNESS_SHA256
        ),
    )


def _policy(
    version: int,
    *,
    enabled: bool,
    updated_at: datetime | None = None,
) -> attestation.AiPolicyRevisionFacts:
    return attestation.AiPolicyRevisionFacts(
        workspace_id=_WORKSPACE_ID,
        version=version,
        external_ai_enabled=enabled,
        provider_governance_accepted=True,
        provider_governance_fingerprint=(attestation.CANONICAL_GOVERNANCE_FINGERPRINT),
        provider_governance_accepted_at=_POLICY_UPDATED_AT,
        model_snapshot=attestation.CANONICAL_MODEL_SNAPSHOT,
        endpoint_region="global",
        endpoint_origin_fingerprint=(attestation.CANONICAL_ENDPOINT_ORIGIN_FINGERPRINT),
        configuration_fingerprint=(attestation.CANONICAL_CONFIGURATION_FINGERPRINT),
        requests_per_minute=1_000,
        daily_input_token_limit=4_250_000,
        daily_output_token_limit=200_000,
        concurrent_attempt_limit=1,
        reservation_lease_seconds=60,
        audit_retention_seconds=2_592_000,
        updated_by="sb_m27_browser_policy_admin",
        updated_at=updated_at or _POLICY_UPDATED_AT,
    )


def _ledger_row(
    index: int,
    *,
    identity_offset: int = 0,
) -> attestation.LedgerReservationAuditFacts:
    identity = identity_offset + index + 1
    fencing_token = identity
    (
        _source,
        _case_id,
        _repetition,
        input_tokens,
        output_tokens,
        _evaluator_duration,
        duration_ms,
    ) = _ATTEMPT_MATRIX[index]
    request_id = f"airq_{identity:048x}"
    actor_digest = "a" * 64
    request_fingerprint = f"{identity + 3000:064x}"
    semantic_scope_fingerprint = attestation.CANONICAL_SEMANTIC_SCOPE_FINGERPRINT
    semantic_payload_fingerprint = f"{identity + 5000:064x}"
    idempotency_digest = attestation._fingerprint(
        {
            "attempt_number": 1,
            "request_fingerprint": request_fingerprint,
            "request_id": request_id,
            "stage": "interpretation",
            "version": 1,
            "workspace_id": _WORKSPACE_ID,
        }
    )
    reservation_id = (
        "air_"
        + hashlib.sha256(
            "|".join(
                (
                    "ai_attempt_reservation_v1",
                    _WORKSPACE_ID,
                    request_id,
                    "interpretation",
                    "1",
                    idempotency_digest,
                )
            ).encode()
        ).hexdigest()
    )
    settlement_payload = "|".join(
        (
            "ai_attempt_settlement_v1",
            reservation_id,
            str(fencing_token),
            "succeeded",
            str(input_tokens),
            str(output_tokens),
            str(input_tokens),
            str(output_tokens),
            str(duration_ms),
        )
    )
    settlement_fingerprint = hashlib.sha256(settlement_payload.encode()).hexdigest()
    audit_id = (
        "aia_" + hashlib.sha256((reservation_id + settlement_fingerprint).encode()).hexdigest()
    )
    created_at = _FIRST_CREATED_AT + timedelta(seconds=index * 10)
    settled_at = created_at + timedelta(milliseconds=duration_ms)
    audit_retain_until = settled_at + timedelta(days=30)
    workspace_scope_digest = hashlib.sha256(
        f"query_studio_scope_v1|{_WORKSPACE_ID}".encode()
    ).hexdigest()
    common = {
        "reservation_id": reservation_id,
        "audit_id": audit_id,
        "audit_reservation_id": reservation_id,
        "workspace_id": _WORKSPACE_ID,
        "request_id": request_id,
        "actor_digest": actor_digest,
        "stage": "interpretation",
        "attempt_number": 1,
        "idempotency_digest": idempotency_digest,
        "request_fingerprint": request_fingerprint,
        "semantic_scope_fingerprint": semantic_scope_fingerprint,
        "semantic_payload_fingerprint": semantic_payload_fingerprint,
        "configuration_fingerprint": (attestation.CANONICAL_CONFIGURATION_FINGERPRINT),
        "policy_version": attestation.QUALIFICATION_POLICY_VERSION,
        "model_snapshot": attestation.CANONICAL_MODEL_SNAPSHOT,
        "endpoint_region": "global",
        "estimated_input_tokens": 2_000,
        "estimated_output_tokens": 256,
        "audit_retention_seconds": 2_592_000,
        "status": "settled",
        "fencing_token": fencing_token,
        "lease_acquired_at": created_at,
        "lease_expires_at": None,
        "outcome_code": "succeeded",
        "observed_input_tokens": input_tokens,
        "observed_output_tokens": output_tokens,
        "charged_input_tokens": input_tokens,
        "charged_output_tokens": output_tokens,
        "duration_ms": duration_ms,
        "settlement_fingerprint": settlement_fingerprint,
        "created_at": created_at,
        "settled_at": settled_at,
        "retain_until": created_at + timedelta(days=30),
        "audit_workspace_scope_digest": workspace_scope_digest,
        "audit_actor_digest": actor_digest,
        "audit_request_id": request_id,
        "audit_stage": "interpretation",
        "audit_attempt_number": 1,
        "audit_model_snapshot": attestation.CANONICAL_MODEL_SNAPSHOT,
        "audit_endpoint_region": "global",
        "audit_configuration_fingerprint": (attestation.CANONICAL_CONFIGURATION_FINGERPRINT),
        "audit_request_fingerprint": request_fingerprint,
        "audit_semantic_scope_fingerprint": semantic_scope_fingerprint,
        "audit_semantic_payload_fingerprint": (semantic_payload_fingerprint),
        "audit_input_tokens": input_tokens,
        "audit_output_tokens": output_tokens,
        "audit_duration_ms": duration_ms,
        "audit_outcome_code": "succeeded",
        "audit_occurred_at": settled_at,
        "audit_retain_until": audit_retain_until,
    }
    return attestation.LedgerReservationAuditFacts.model_validate(common)


def _snapshot(
    *,
    identity_offset: int = 0,
) -> attestation.CampaignLedgerSnapshot:
    rows = tuple(
        _ledger_row(index, identity_offset=identity_offset)
        for index in range(attestation.CANONICAL_PROVIDER_ATTEMPTS)
    )
    qualification = _policy(
        attestation.QUALIFICATION_POLICY_VERSION,
        enabled=True,
    )
    last_settled_at = rows[-1].settled_at
    assert last_settled_at is not None
    disabled = _policy(
        attestation.DISABLED_POLICY_VERSION,
        enabled=False,
        updated_at=last_settled_at
        + timedelta(
            microseconds=(attestation.CANONICAL_DISABLE_AFTER_LAST_SETTLEMENT_MICROSECONDS)
        ),
    )
    return attestation.CampaignLedgerSnapshot(
        qualification_policy=qualification,
        disabled_policy=disabled,
        current_policy=disabled,
        ledger_rows=rows,
        qualification_linked_audit_count=16,
        disabled_policy_linked_audit_count=0,
        orphan_audit_count=0,
        active_attempt_count=0,
        stale_reserved_count=0,
    )


def _row_with_usage(
    row: attestation.LedgerReservationAuditFacts,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    duration_ms: int | None = None,
) -> attestation.LedgerReservationAuditFacts:
    resolved_input = input_tokens if input_tokens is not None else row.audit_input_tokens
    resolved_output = output_tokens if output_tokens is not None else row.audit_output_tokens
    resolved_duration = duration_ms if duration_ms is not None else row.audit_duration_ms
    settlement_fingerprint = hashlib.sha256(
        "|".join(
            (
                "ai_attempt_settlement_v1",
                row.reservation_id,
                str(row.fencing_token),
                "succeeded",
                str(resolved_input),
                str(resolved_output),
                str(resolved_input),
                str(resolved_output),
                str(resolved_duration),
            )
        ).encode()
    ).hexdigest()
    audit_id = (
        "aia_" + hashlib.sha256((row.reservation_id + settlement_fingerprint).encode()).hexdigest()
    )
    return attestation.LedgerReservationAuditFacts.model_validate(
        {
            **row.model_dump(mode="python"),
            "audit_id": audit_id,
            "observed_input_tokens": resolved_input,
            "observed_output_tokens": resolved_output,
            "charged_input_tokens": resolved_input,
            "charged_output_tokens": resolved_output,
            "duration_ms": resolved_duration,
            "settlement_fingerprint": settlement_fingerprint,
            "audit_input_tokens": resolved_input,
            "audit_output_tokens": resolved_output,
            "audit_duration_ms": resolved_duration,
        }
    )


def _row_with_admission(
    row: attestation.LedgerReservationAuditFacts,
    *,
    request_id: str | None = None,
    request_fingerprint: str | None = None,
) -> attestation.LedgerReservationAuditFacts:
    resolved_request_id = request_id or row.request_id
    resolved_request_fingerprint = request_fingerprint or row.request_fingerprint
    idempotency_digest = attestation._fingerprint(
        {
            "attempt_number": row.attempt_number,
            "request_fingerprint": resolved_request_fingerprint,
            "request_id": resolved_request_id,
            "stage": row.stage,
            "version": 1,
            "workspace_id": row.workspace_id,
        }
    )
    reservation_id = (
        "air_"
        + hashlib.sha256(
            "|".join(
                (
                    "ai_attempt_reservation_v1",
                    row.workspace_id,
                    resolved_request_id,
                    row.stage,
                    str(row.attempt_number),
                    idempotency_digest,
                )
            ).encode()
        ).hexdigest()
    )
    settlement_fingerprint = hashlib.sha256(
        "|".join(
            (
                "ai_attempt_settlement_v1",
                reservation_id,
                str(row.fencing_token),
                str(row.outcome_code),
                str(row.observed_input_tokens),
                str(row.observed_output_tokens),
                str(row.charged_input_tokens),
                str(row.charged_output_tokens),
                str(row.duration_ms),
            )
        ).encode()
    ).hexdigest()
    audit_id = (
        "aia_" + hashlib.sha256((reservation_id + settlement_fingerprint).encode()).hexdigest()
    )
    return attestation.LedgerReservationAuditFacts.model_validate(
        {
            **row.model_dump(mode="python"),
            "request_id": resolved_request_id,
            "audit_request_id": resolved_request_id,
            "request_fingerprint": resolved_request_fingerprint,
            "audit_request_fingerprint": resolved_request_fingerprint,
            "idempotency_digest": idempotency_digest,
            "reservation_id": reservation_id,
            "audit_reservation_id": reservation_id,
            "settlement_fingerprint": settlement_fingerprint,
            "audit_id": audit_id,
        }
    )


def _history(root: Path) -> Path:
    path = root / attestation.CANONICAL_LIVE_HISTORY_DIRECTORY
    path.mkdir(parents=True)
    return path


def test_builds_exact_sanitized_attestation_and_digest_binds_internal_ids() -> None:
    first = attestation.build_campaign_ledger_attestation(
        _campaign(),
        _snapshot(),
    )
    changed_ids = attestation.build_campaign_ledger_attestation(
        _campaign(),
        _snapshot(identity_offset=10_000),
    )
    payload = json.dumps(first.model_dump(mode="json"), sort_keys=True)

    assert first.ledger.reservation_count == 16
    assert first.ledger.audit_count == 16
    assert first.schema_version == 2
    assert (
        first.campaign.ordered_execution_witness_digest_sha256
        == attestation.CANONICAL_CAMPAIGN_ORDERED_EXECUTION_WITNESS_SHA256
    )
    assert first.ledger.ordinal_binding_count == 16
    assert first.ledger.unique_compatible_matching_count == 1
    assert first.ledger.interpretation_succeeded_count == 16
    assert first.ledger.expansion_count == 0
    assert first.ledger.mismatch_count == 0
    assert first.ledger.missing_audit_count == 0
    assert first.ledger.orphan_audit_count == 0
    assert first.ledger.open_attempt_count == 0
    assert first.ledger.stale_reserved_count == 0
    assert first.ledger.input_tokens == 15_715
    assert first.ledger.output_reasoning_tokens == 1_204
    assert first.ledger.evaluator_adapter_duration_ms == 30_016
    assert first.ledger.provider_boundary_duration_ms == 29_961
    assert first.ledger.ledger_digest_sha256 != changed_ids.ledger.ledger_digest_sha256
    assert (
        first.ledger.ordinal_binding_digest_sha256
        != changed_ids.ledger.ordinal_binding_digest_sha256
    )
    for forbidden in (
        "reservation_id",
        "audit_id",
        "request_id",
        "actor_digest",
        "idempotency_digest",
        "settlement_fingerprint",
        _snapshot().ledger_rows[0].reservation_id,
        _snapshot().ledger_rows[0].audit_id,
        "prompt",
        "response",
        '"sql"',
        '"rows"',
        '"values"',
    ):
        assert forbidden not in payload


@pytest.mark.parametrize(
    "mutator",
    (
        lambda value: value.model_copy(
            update={
                "ledger_rows": value.ledger_rows[:-1],
                "qualification_linked_audit_count": 15,
            }
        ),
        lambda value: value.model_copy(
            update={
                "ledger_rows": (
                    *value.ledger_rows,
                    _ledger_row(0, identity_offset=20_000),
                ),
                "qualification_linked_audit_count": 17,
            }
        ),
        lambda value: value.model_copy(update={"qualification_linked_audit_count": 15}),
        lambda value: value.model_copy(update={"orphan_audit_count": 1}),
        lambda value: value.model_copy(update={"active_attempt_count": 1}),
        lambda value: value.model_copy(update={"stale_reserved_count": 1}),
    ),
)
def test_extra_missing_or_open_ledger_facts_fail_closed(
    mutator: Callable[
        [attestation.CampaignLedgerSnapshot],
        attestation.CampaignLedgerSnapshot,
    ],
) -> None:
    changed = mutator(_snapshot())

    with pytest.raises(
        attestation.CampaignLedgerAttestationError,
        match="do not attest",
    ):
        attestation.build_campaign_ledger_attestation(_campaign(), changed)


def test_policy_or_duration_mismatch_fails_closed() -> None:
    snapshot = _snapshot()
    wrong_disabled = snapshot.disabled_policy.model_copy(update={"external_ai_enabled": True})
    changed_policy = snapshot.model_copy(
        update={
            "disabled_policy": wrong_disabled,
            "current_policy": wrong_disabled,
        }
    )
    with pytest.raises(attestation.CampaignLedgerAttestationError):
        attestation.build_campaign_ledger_attestation(
            _campaign(),
            changed_policy,
        )

    changed_duration_row = snapshot.ledger_rows[0].model_copy(
        update={"duration_ms": snapshot.ledger_rows[0].duration_ms + 1}
    )
    changed_duration = snapshot.model_copy(
        update={
            "ledger_rows": (
                changed_duration_row,
                *snapshot.ledger_rows[1:],
            )
        }
    )
    with pytest.raises(attestation.CampaignLedgerAttestationError):
        attestation.build_campaign_ledger_attestation(
            _campaign(),
            changed_duration,
        )


def test_same_aggregate_token_redistribution_cannot_replace_ordinal_binding() -> None:
    snapshot = _snapshot()
    changed_rows = (
        _row_with_usage(
            snapshot.ledger_rows[0],
            input_tokens=snapshot.ledger_rows[0].audit_input_tokens + 1,
        ),
        _row_with_usage(
            snapshot.ledger_rows[1],
            input_tokens=snapshot.ledger_rows[1].audit_input_tokens - 1,
        ),
        *snapshot.ledger_rows[2:],
    )
    changed = snapshot.model_copy(update={"ledger_rows": changed_rows})

    assert sum(item.audit_input_tokens for item in changed_rows) == 15_715
    with pytest.raises(attestation.CampaignLedgerAttestationError):
        attestation.build_campaign_ledger_attestation(_campaign(), changed)


def test_same_aggregate_duration_redistribution_changes_bound_sources() -> None:
    campaign = _campaign()
    snapshot = _snapshot()
    persisted = attestation.build_campaign_ledger_attestation(campaign, snapshot)
    changed_rows = (
        _row_with_usage(
            snapshot.ledger_rows[0],
            duration_ms=snapshot.ledger_rows[0].audit_duration_ms + 1,
        ),
        _row_with_usage(
            snapshot.ledger_rows[1],
            duration_ms=snapshot.ledger_rows[1].audit_duration_ms - 1,
        ),
        *snapshot.ledger_rows[2:],
    )
    changed = snapshot.model_copy(update={"ledger_rows": changed_rows})
    recomputed = attestation.build_campaign_ledger_attestation(campaign, changed)

    assert sum(item.audit_duration_ms for item in changed_rows) == 29_961
    assert (
        persisted.ledger.ordinal_binding_digest_sha256
        != recomputed.ledger.ordinal_binding_digest_sha256
    )
    with pytest.raises(attestation.CampaignLedgerAttestationError, match="no longer matches"):
        attestation.verify_campaign_ledger_attestation(
            persisted,
            campaign,
            changed,
        )


@pytest.mark.parametrize(
    "updates",
    (
        {"observed_input_tokens": 1},
        {"lease_expires_at": _FIRST_CREATED_AT + timedelta(seconds=60)},
        {"retain_until": _FIRST_CREATED_AT + timedelta(days=31)},
        {
            "settled_at": _FIRST_CREATED_AT,
            "audit_occurred_at": _FIRST_CREATED_AT,
            "audit_retain_until": _FIRST_CREATED_AT + timedelta(days=30),
        },
    ),
)
def test_succeeded_terminal_equality_and_retention_are_exact(
    updates: dict[str, object],
) -> None:
    snapshot = _snapshot()
    changed_row = snapshot.ledger_rows[0].model_copy(update=updates)
    changed = snapshot.model_copy(update={"ledger_rows": (changed_row, *snapshot.ledger_rows[1:])})

    with pytest.raises(attestation.CampaignLedgerAttestationError):
        attestation.build_campaign_ledger_attestation(_campaign(), changed)


def test_reservation_id_is_exactly_derived_and_rejects_consistent_tamper() -> None:
    row = _snapshot().ledger_rows[0]
    expected = (
        "air_"
        + hashlib.sha256(
            "|".join(
                (
                    "ai_attempt_reservation_v1",
                    row.workspace_id,
                    row.request_id,
                    row.stage,
                    str(row.attempt_number),
                    row.idempotency_digest,
                )
            ).encode()
        ).hexdigest()
    )
    assert row.reservation_id == expected

    tampered_reservation_id = "air_" + ("f" * 64)
    tampered_settlement = hashlib.sha256(
        "|".join(
            (
                "ai_attempt_settlement_v1",
                tampered_reservation_id,
                str(row.fencing_token),
                "succeeded",
                str(row.observed_input_tokens),
                str(row.observed_output_tokens),
                str(row.charged_input_tokens),
                str(row.charged_output_tokens),
                str(row.duration_ms),
            )
        ).encode()
    ).hexdigest()
    tampered_audit_id = (
        "aia_"
        + hashlib.sha256((tampered_reservation_id + tampered_settlement).encode()).hexdigest()
    )
    with pytest.raises(ValueError):
        attestation.LedgerReservationAuditFacts.model_validate(
            {
                **row.model_dump(mode="python"),
                "reservation_id": tampered_reservation_id,
                "audit_reservation_id": tampered_reservation_id,
                "settlement_fingerprint": tampered_settlement,
                "audit_id": tampered_audit_id,
            }
        )


def test_temporal_closure_rejects_overlap_and_policy_boundary_drift() -> None:
    snapshot = _snapshot()
    first = snapshot.ledger_rows[0]
    second = snapshot.ledger_rows[1]
    assert first.settled_at is not None
    overlapped = second.model_copy(
        update={
            "lease_acquired_at": first.settled_at,
            "created_at": first.settled_at,
            "retain_until": first.settled_at + timedelta(days=30),
        }
    )
    overlap_snapshot = snapshot.model_copy(
        update={"ledger_rows": (first, overlapped, *snapshot.ledger_rows[2:])}
    )
    with pytest.raises(attestation.CampaignLedgerAttestationError):
        attestation.build_campaign_ledger_attestation(
            _campaign(),
            overlap_snapshot,
        )

    changed_qualification = snapshot.qualification_policy.model_copy(
        update={
            "provider_governance_accepted_at": (
                snapshot.qualification_policy.updated_at - timedelta(microseconds=1)
            )
        }
    )
    with pytest.raises(attestation.CampaignLedgerAttestationError):
        attestation.build_campaign_ledger_attestation(
            _campaign(),
            snapshot.model_copy(update={"qualification_policy": changed_qualification}),
        )

    changed_disabled = snapshot.disabled_policy.model_copy(
        update={"updated_at": snapshot.disabled_policy.updated_at + timedelta(microseconds=1)}
    )
    with pytest.raises(attestation.CampaignLedgerAttestationError):
        attestation.build_campaign_ledger_attestation(
            _campaign(),
            snapshot.model_copy(
                update={
                    "disabled_policy": changed_disabled,
                    "current_policy": changed_disabled,
                }
            ),
        )


def test_admission_witnesses_reject_duplicate_or_noncanonical_facts() -> None:
    snapshot = _snapshot()
    first = snapshot.ledger_rows[0]
    second = snapshot.ledger_rows[1]
    duplicate_request_id = first.request_id
    changed_request = _row_with_admission(
        second,
        request_id=duplicate_request_id,
    )
    duplicate_request_snapshot = snapshot.model_copy(
        update={
            "ledger_rows": (
                first,
                changed_request,
                *snapshot.ledger_rows[2:],
            )
        }
    )
    with pytest.raises(attestation.CampaignLedgerAttestationError):
        attestation.build_campaign_ledger_attestation(
            _campaign(),
            duplicate_request_snapshot,
        )

    wrong_scope_rows = tuple(
        row.model_copy(
            update={
                "semantic_scope_fingerprint": "f" * 64,
                "audit_semantic_scope_fingerprint": "f" * 64,
            }
        )
        for row in snapshot.ledger_rows
    )
    with pytest.raises(attestation.CampaignLedgerAttestationError):
        attestation.build_campaign_ledger_attestation(
            _campaign(),
            snapshot.model_copy(update={"ledger_rows": wrong_scope_rows}),
        )


@pytest.mark.parametrize(
    "fingerprint_field",
    ("request_fingerprint", "semantic_payload_fingerprint"),
)
def test_duplicate_execution_fingerprint_is_not_a_bijection(
    fingerprint_field: str,
) -> None:
    snapshot = _snapshot()
    first = snapshot.ledger_rows[0]
    second = snapshot.ledger_rows[1]
    updates: dict[str, object] = {
        fingerprint_field: getattr(first, fingerprint_field),
        f"audit_{fingerprint_field}": getattr(first, fingerprint_field),
    }
    if fingerprint_field == "request_fingerprint":
        changed_second = _row_with_admission(
            second,
            request_fingerprint=first.request_fingerprint,
        )
    else:
        changed_second = attestation.LedgerReservationAuditFacts.model_validate(
            {**second.model_dump(mode="python"), **updates}
        )
    changed = snapshot.model_copy(
        update={
            "ledger_rows": (
                first,
                changed_second,
                *snapshot.ledger_rows[2:],
            )
        }
    )

    with pytest.raises(attestation.CampaignLedgerAttestationError):
        attestation.build_campaign_ledger_attestation(_campaign(), changed)


def test_campaign_witness_digest_prevents_hand_built_same_total_matrix() -> None:
    campaign = _campaign()
    changed_attempts = (
        campaign.attempts[1].model_copy(update={"sequence": 1}),
        campaign.attempts[0].model_copy(update={"sequence": 2}),
        *campaign.attempts[2:],
    )

    with pytest.raises(ValueError):
        attestation.CampaignEvidenceFacts.model_validate(
            {
                **campaign.model_dump(mode="python"),
                "attempts": changed_attempts,
            }
        )


def test_creation_is_bound_to_v84_but_historical_verify_allows_later_disabled_policy() -> None:
    persisted = attestation.build_campaign_ledger_attestation(
        _campaign(),
        _snapshot(),
    )
    snapshot = _snapshot()
    later_updated_at = snapshot.disabled_policy.updated_at + timedelta(hours=1)
    later_disabled = _policy(86, enabled=False).model_copy(
        update={
            "provider_governance_fingerprint": "9" * 64,
            "provider_governance_accepted_at": later_updated_at,
            "updated_by": "sb_m27_browser_post_attestation_admin",
            "updated_at": later_updated_at,
        }
    )
    historical_snapshot = snapshot.model_copy(update={"current_policy": later_disabled})

    with pytest.raises(attestation.CampaignLedgerAttestationError):
        attestation.build_campaign_ledger_attestation(
            _campaign(),
            historical_snapshot,
        )
    assert (
        attestation.verify_campaign_ledger_attestation(
            persisted,
            _campaign(),
            historical_snapshot,
        )
        == persisted
    )

    changed_configuration = historical_snapshot.model_copy(
        update={
            "current_policy": later_disabled.model_copy(
                update={"configuration_fingerprint": "8" * 64}
            )
        }
    )
    with pytest.raises(attestation.CampaignLedgerAttestationError):
        attestation.verify_campaign_ledger_attestation(
            persisted,
            _campaign(),
            changed_configuration,
        )

    stale_timestamp = historical_snapshot.model_copy(
        update={
            "current_policy": later_disabled.model_copy(
                update={"updated_at": snapshot.disabled_policy.updated_at}
            )
        }
    )
    with pytest.raises(attestation.CampaignLedgerAttestationError):
        attestation.verify_campaign_ledger_attestation(
            persisted,
            _campaign(),
            stale_timestamp,
        )

    future_acceptance = historical_snapshot.model_copy(
        update={
            "current_policy": later_disabled.model_copy(
                update={
                    "provider_governance_accepted_at": (
                        later_disabled.updated_at + timedelta(microseconds=1)
                    )
                }
            )
        }
    )
    with pytest.raises(attestation.CampaignLedgerAttestationError):
        attestation.verify_campaign_ledger_attestation(
            persisted,
            _campaign(),
            future_acceptance,
        )

    active_later = historical_snapshot.model_copy(update={"active_attempt_count": 1})
    with pytest.raises(attestation.CampaignLedgerAttestationError):
        attestation.verify_campaign_ledger_attestation(
            persisted,
            _campaign(),
            active_later,
        )


@pytest.mark.parametrize(
    ("policy_name", "changed_cap"),
    (
        ("qualification_policy", {"requests_per_minute": 999}),
        ("qualification_policy", {"daily_input_token_limit": 4_249_999}),
        ("disabled_policy", {"daily_output_token_limit": 199_999}),
        ("disabled_policy", {"concurrent_attempt_limit": 2}),
        ("disabled_policy", {"reservation_lease_seconds": 61}),
        ("disabled_policy", {"audit_retention_seconds": 2_592_001}),
    ),
)
def test_policy_83_and_84_caps_are_exact(
    policy_name: str,
    changed_cap: dict[str, int],
) -> None:
    snapshot = _snapshot()
    changed_policy = getattr(snapshot, policy_name).model_copy(update=changed_cap)
    updates: dict[str, object] = {policy_name: changed_policy}
    if policy_name == "disabled_policy":
        updates["current_policy"] = changed_policy
    changed = snapshot.model_copy(update=updates)

    with pytest.raises(attestation.CampaignLedgerAttestationError):
        attestation.build_campaign_ledger_attestation(
            _campaign(),
            changed,
        )


def test_reader_queries_bound_rows_and_count_cross_scope_orphans() -> None:
    normalized_rows = " ".join(attestation._LEDGER_ROWS_SQL.split())
    normalized_orphans = " ".join(attestation._ORPHAN_AUDIT_COUNT_SQL.split())

    assert "FETCH FIRST 181 ROWS ONLY" in normalized_rows
    assert "audit.workspace_scope_digest = encode(" in normalized_orphans
    assert "LEFT JOIN" in normalized_orphans
    assert "reservation.reservation_id IS NULL" in normalized_orphans
    assert "reservation.workspace_id <> %s" in normalized_orphans
    assert normalized_rows.startswith("SELECT")
    assert (
        "ORDER BY reservation.lease_acquired_at, reservation.created_at, "
        "reservation.reservation_id, audit.audit_id"
    ) in normalized_rows


def test_reader_uses_one_exact_repeatable_read_only_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _snapshot()
    calls: list[str] = []

    class Result:
        def __init__(
            self,
            *,
            one: tuple[object, ...] | None = None,
            many: list[tuple[object, ...]] | None = None,
        ) -> None:
            self._one = one
            self._many = many or []

        def fetchone(self) -> tuple[object, ...] | None:
            return self._one

        def fetchall(self) -> list[tuple[object, ...]]:
            return self._many

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(
            self,
            query: str,
            _parameters: tuple[object, ...] | None = None,
        ) -> Result:
            normalized = " ".join(query.split())
            calls.append(normalized)
            if normalized == ("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"):
                return Result()
            if "set_config('statement_timeout'" in normalized:
                return Result()
            if "current_setting('transaction_read_only')" in normalized:
                return Result(one=("on", "repeatable read"))
            if "tenant_ai_policy_revisions" in normalized:
                return Result(
                    many=[
                        tuple(
                            getattr(expected.qualification_policy, column)
                            for column in attestation._POLICY_COLUMNS
                        ),
                        tuple(
                            getattr(expected.disabled_policy, column)
                            for column in attestation._POLICY_COLUMNS
                        ),
                    ]
                )
            if "tenant_ai_policies" in normalized:
                return Result(
                    one=tuple(
                        getattr(expected.current_policy, column)
                        for column in attestation._POLICY_COLUMNS
                    )
                )
            if "ai_provider_admission_state" in normalized:
                return Result(one=(0, 0))
            if "workspace_scope_digest = encode" in normalized:
                return Result(one=(0,))
            if (
                "FROM schemabridge_control.ai_provider_usage_audit AS audit "
                "JOIN schemabridge_control.ai_provider_attempt_reservations" in normalized
            ):
                return Result(one=(16, 0))
            if (
                "FROM schemabridge_control.ai_provider_attempt_reservations "
                "AS reservation LEFT JOIN" in normalized
            ):
                return Result(
                    many=[
                        tuple(getattr(row, column) for column in attestation._LEDGER_COLUMNS)
                        for row in expected.ledger_rows
                    ]
                )
            raise AssertionError(f"unexpected SQL shape: {normalized}")

    monkeypatch.setattr(
        attestation.psycopg,
        "connect",
        lambda *_args, **_kwargs: Connection(),
    )

    observed = attestation.read_campaign_ledger_snapshot(
        "postgresql://bounded.invalid/control",
        _WORKSPACE_ID,
    )

    assert observed == expected
    assert calls[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    assert sum("transaction_read_only" in query for query in calls) == 1
    assert sum("transaction_isolation" in query for query in calls) == 1


def test_signed_content_address_round_trip_rejects_wrong_key_and_tamper(
    tmp_path: Path,
) -> None:
    _history(tmp_path)
    report = attestation.build_campaign_ledger_attestation(
        _campaign(),
        _snapshot(),
    )
    path = attestation.write_campaign_ledger_attestation(
        report,
        tmp_path,
        signing_key=_SIGNING_KEY,
        signing_key_version="v1",
    )

    assert (
        attestation.load_campaign_ledger_attestation(
            tmp_path,
            path,
            signing_key=_SIGNING_KEY,
            signing_key_version="v1",
        )
        == report
    )
    assert (
        attestation.write_campaign_ledger_attestation(
            report,
            tmp_path,
            signing_key=_SIGNING_KEY,
            signing_key_version="v1",
        )
        == path
    )
    with pytest.raises(attestation.CampaignLedgerAttestationError):
        attestation.load_campaign_ledger_attestation(
            tmp_path,
            path,
            signing_key=_WRONG_KEY,
            signing_key_version="v1",
        )

    tampered = json.loads(path.read_text())
    tampered["signature"] = "0" * 64
    tampered_bytes = (
        json.dumps(tampered, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    tampered_sha256 = hashlib.sha256(tampered_bytes).hexdigest()
    readdressed = path.parent / (f"campaign-ledger-attestation-{tampered_sha256}.json")
    readdressed.write_bytes(tampered_bytes)
    with pytest.raises(attestation.CampaignLedgerAttestationError):
        attestation.load_campaign_ledger_attestation(
            tmp_path,
            readdressed,
            signing_key=_SIGNING_KEY,
            signing_key_version="v1",
        )

    original = path.read_bytes()
    path.write_bytes(original.replace(b'"selected"', b'"tampered"', 1))
    with pytest.raises(attestation.CampaignLedgerAttestationError):
        attestation.load_campaign_ledger_attestation(
            tmp_path,
            path,
            signing_key=_SIGNING_KEY,
            signing_key_version="v1",
        )


def test_duplicate_keys_symlink_and_noncanonical_paths_fail_closed(
    tmp_path: Path,
) -> None:
    history = _history(tmp_path)
    report = attestation.build_campaign_ledger_attestation(
        _campaign(),
        _snapshot(),
    )
    path = attestation.write_campaign_ledger_attestation(
        report,
        tmp_path,
        signing_key=_SIGNING_KEY,
        signing_key_version="v1",
    )
    duplicate = (
        path.read_text()
        .replace(
            '"schema_version": 2',
            '"schema_version": 2,\n  "schema_version": 2',
            1,
        )
        .encode()
    )
    duplicate_sha256 = hashlib.sha256(duplicate).hexdigest()
    duplicate_path = history / (f"campaign-ledger-attestation-{duplicate_sha256}.json")
    duplicate_path.write_bytes(duplicate)
    with pytest.raises(attestation.CampaignLedgerAttestationError):
        attestation.load_campaign_ledger_attestation(
            tmp_path,
            duplicate_path,
            signing_key=_SIGNING_KEY,
            signing_key_version="v1",
        )

    symlink = history / ("campaign-ledger-attestation-" + ("a" * 64) + ".json")
    symlink.symlink_to(path)
    with pytest.raises(attestation.CampaignLedgerAttestationError):
        attestation.load_campaign_ledger_attestation(
            tmp_path,
            symlink,
            signing_key=_SIGNING_KEY,
            signing_key_version="v1",
        )

    outside = tmp_path / path.name
    outside.write_bytes(path.read_bytes())
    with pytest.raises(attestation.CampaignLedgerAttestationError):
        attestation.load_campaign_ledger_attestation(
            tmp_path,
            outside,
            signing_key=_SIGNING_KEY,
            signing_key_version="v1",
        )


def test_bounded_load_rejects_oversized_file_before_json_parsing(
    tmp_path: Path,
) -> None:
    history = _history(tmp_path)
    oversized = b"{" + (b" " * (128 * 1024)) + b"}"
    content_sha256 = hashlib.sha256(oversized).hexdigest()
    path = history / f"campaign-ledger-attestation-{content_sha256}.json"
    path.write_bytes(oversized)

    with pytest.raises(attestation.CampaignLedgerAttestationError):
        attestation.load_campaign_ledger_attestation(
            tmp_path,
            path,
            signing_key=_SIGNING_KEY,
            signing_key_version="v1",
        )


def test_history_rejects_any_symlink_ancestor_even_when_target_is_inside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    real_reports = root / "real-reports"
    real_reports.mkdir()
    (root / "reports").symlink_to(real_reports, target_is_directory=True)
    report = attestation.build_campaign_ledger_attestation(
        _campaign(),
        _snapshot(),
    )

    with pytest.raises(attestation.CampaignLedgerAttestationError):
        attestation.write_campaign_ledger_attestation(
            report,
            root,
            signing_key=_SIGNING_KEY,
            signing_key_version="v1",
        )
    with pytest.raises(ValueError):
        attestation.reject_symlink_ancestors(
            root.resolve(),
            root.resolve() / "reports/m27-query-studio-live-history/evidence.json",
        )


def test_v1_envelope_is_historical_not_reinterpreted_as_v2() -> None:
    report = attestation.build_campaign_ledger_attestation(
        _campaign(),
        _snapshot(),
    )
    envelope = attestation._signed_envelope(
        report,
        signing_key=_SIGNING_KEY,
        signing_key_version="v1",
    )
    payload = envelope.model_dump(mode="python")
    payload["schema_version"] = 1
    payload["signature_domain"] = "schemabridge:m27:query-studio:campaign-ledger-attestation:v1"

    with pytest.raises(ValueError):
        attestation.CampaignLedgerAttestationEnvelope.model_validate(payload)


def test_wrong_campaign_path_and_recomputed_source_mismatch_fail_closed(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        attestation.CampaignLedgerAttestationError,
        match="campaign evidence is invalid",
    ):
        attestation.load_canonical_campaign_evidence(
            tmp_path,
            Path("reports/wrong-campaign.json"),
            signing_key=_SIGNING_KEY,
            signing_key_version="v1",
        )

    persisted = attestation.build_campaign_ledger_attestation(
        _campaign(),
        _snapshot(),
    )
    with pytest.raises(
        attestation.CampaignLedgerAttestationError,
        match="no longer matches",
    ):
        attestation.verify_campaign_ledger_attestation(
            persisted,
            _campaign(),
            _snapshot(identity_offset=10_000),
        )
