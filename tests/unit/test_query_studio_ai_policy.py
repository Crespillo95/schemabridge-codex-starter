from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from schemabridge.application.ports.query_studio_ai_control import (
    TenantAiPolicyConfirmation,
    TenantAiPolicyOperatorSnapshot,
    TenantAiPolicyWrite,
)
from schemabridge.application.query_studio_ai_policy import (
    TenantAiPolicyDesired,
    TenantAiPolicyError,
    TenantAiPolicyErrorCode,
    TenantAiPolicyOperator,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
SHA_A = "a" * 64
SHA_C = "c" * 64
ENDPOINT_ORIGIN_FINGERPRINTS = {
    "global": "6836a1bf89b42ed7a325b83c7825e0ae3a17048d4d7395d0606ee18f2800f71c",
    "eu": "83203af93d5b1b9a2b7ab344441b99f7e19d980c881ba78de44e16073bd199c7",
    "us": "c0559fdcd4b90b58e8af98b3fd4be11859e79e0718dc086422773bd6aa38dc85",
}


def _desired(*, enabled: bool = True) -> TenantAiPolicyDesired:
    return TenantAiPolicyDesired(
        external_ai_enabled=enabled,
        provider_governance_accepted=enabled,
        provider_governance_fingerprint=SHA_A if enabled else None,
        model_snapshot="gpt-5-nano-2025-08-07",
        endpoint_region="eu",
        endpoint_origin_fingerprint=ENDPOINT_ORIGIN_FINGERPRINTS["eu"],
        configuration_fingerprint=SHA_C,
        requests_per_minute=20,
        daily_input_token_limit=100_000,
        daily_output_token_limit=20_000,
        concurrent_attempt_limit=2,
        reservation_lease_seconds=60,
        audit_retention_seconds=2_592_000,
    )


def _snapshot(
    *,
    version: int,
    desired: TenantAiPolicyDesired,
    updated_at: datetime = NOW,
) -> TenantAiPolicyOperatorSnapshot:
    return TenantAiPolicyOperatorSnapshot(
        workspace_id="workspace-alpha",
        version=version,
        external_ai_enabled=desired.external_ai_enabled,
        provider_governance_accepted=desired.provider_governance_accepted,
        provider_governance_fingerprint=desired.provider_governance_fingerprint,
        provider_governance_accepted_at=(
            updated_at if desired.provider_governance_accepted else None
        ),
        model_snapshot=desired.model_snapshot,
        endpoint_region=desired.endpoint_region,
        endpoint_origin_fingerprint=desired.endpoint_origin_fingerprint,
        configuration_fingerprint=desired.configuration_fingerprint,
        requests_per_minute=desired.requests_per_minute,
        daily_input_token_limit=desired.daily_input_token_limit,
        daily_output_token_limit=desired.daily_output_token_limit,
        concurrent_attempt_limit=desired.concurrent_attempt_limit,
        reservation_lease_seconds=desired.reservation_lease_seconds,
        audit_retention_seconds=desired.audit_retention_seconds,
        updated_by="platform-admin",
        updated_at=updated_at,
    )


def _write(
    *,
    endpoint_region: str,
    endpoint_origin_fingerprint: str,
) -> TenantAiPolicyWrite:
    return TenantAiPolicyWrite(
        workspace_id="workspace-alpha",
        expected_version=0,
        external_ai_enabled=True,
        provider_governance_accepted=True,
        provider_governance_fingerprint=SHA_A,
        model_snapshot="gpt-5-nano-2025-08-07",
        endpoint_region=endpoint_region,
        endpoint_origin_fingerprint=endpoint_origin_fingerprint,
        configuration_fingerprint=SHA_C,
        requests_per_minute=20,
        daily_input_token_limit=100_000,
        daily_output_token_limit=20_000,
        concurrent_attempt_limit=2,
        reservation_lease_seconds=60,
        audit_retention_seconds=2_592_000,
        updated_by="platform-admin",
        confirmation=TenantAiPolicyConfirmation.APPLY,
    )


@pytest.mark.parametrize("endpoint_region", ("global", "eu", "us"))
def test_policy_write_accepts_each_exact_managed_region_origin_pair(
    endpoint_region: str,
) -> None:
    endpoint_origin_fingerprint = ENDPOINT_ORIGIN_FINGERPRINTS[endpoint_region]

    change = _write(
        endpoint_region=endpoint_region,
        endpoint_origin_fingerprint=endpoint_origin_fingerprint,
    )

    assert change.endpoint_region == endpoint_region
    assert change.endpoint_origin_fingerprint == endpoint_origin_fingerprint


@pytest.mark.parametrize(
    ("endpoint_region", "origin_region"),
    [
        pytest.param("global", "eu", id="global-with-eu-origin"),
        pytest.param("global", "us", id="global-with-us-origin"),
        pytest.param("eu", "global", id="eu-with-global-origin"),
        pytest.param("eu", "us", id="eu-with-us-origin"),
        pytest.param("us", "global", id="us-with-global-origin"),
        pytest.param("us", "eu", id="us-with-eu-origin"),
    ],
)
def test_policy_write_rejects_every_managed_region_origin_mismatch(
    endpoint_region: str,
    origin_region: str,
) -> None:
    with pytest.raises(ValueError, match="tenant AI policy values are invalid"):
        _write(
            endpoint_region=endpoint_region,
            endpoint_origin_fingerprint=ENDPOINT_ORIGIN_FINGERPRINTS[origin_region],
        )


@dataclass
class _Store:
    current: TenantAiPolicyOperatorSnapshot | None = None
    writes: list[TenantAiPolicyWrite] = field(default_factory=list)

    def inspect(self, workspace_id: str) -> TenantAiPolicyOperatorSnapshot | None:
        assert workspace_id == "workspace-alpha"
        return self.current

    def apply(self, change: TenantAiPolicyWrite) -> TenantAiPolicyOperatorSnapshot:
        self.writes.append(change)
        desired = TenantAiPolicyDesired(
            external_ai_enabled=change.external_ai_enabled,
            provider_governance_accepted=change.provider_governance_accepted,
            provider_governance_fingerprint=change.provider_governance_fingerprint,
            model_snapshot=change.model_snapshot,
            endpoint_region=change.endpoint_region,
            endpoint_origin_fingerprint=change.endpoint_origin_fingerprint,
            configuration_fingerprint=change.configuration_fingerprint,
            requests_per_minute=change.requests_per_minute,
            daily_input_token_limit=change.daily_input_token_limit,
            daily_output_token_limit=change.daily_output_token_limit,
            concurrent_attempt_limit=change.concurrent_attempt_limit,
            reservation_lease_seconds=change.reservation_lease_seconds,
            audit_retention_seconds=change.audit_retention_seconds,
        )
        self.current = TenantAiPolicyOperatorSnapshot(
            **{
                **asdict(
                    _snapshot(
                        version=change.expected_version + 1,
                        desired=desired,
                        updated_at=NOW + timedelta(seconds=1),
                    )
                ),
                "updated_by": change.updated_by,
            }
        )
        return self.current


def test_prepare_is_read_only_and_exact_apply_records_one_revision() -> None:
    store = _Store()
    operator = TenantAiPolicyOperator(store)

    proposal = operator.prepare(
        workspace_id="workspace-alpha",
        expected_version=0,
        desired=_desired(),
        updated_by="platform-admin",
    )

    assert proposal.expected_version == 0
    assert len(proposal.fingerprint) == 64
    assert store.writes == []

    applied = operator.apply(
        proposal,
        expected_proposal_fingerprint=proposal.fingerprint,
        confirmation="APPLY TENANT AI POLICY",
    )

    assert applied.version == 1
    assert applied.external_ai_enabled is True
    assert len(store.writes) == 1
    assert store.writes[0].confirmation.value == "APPLY TENANT AI POLICY"


@pytest.mark.parametrize(
    ("fingerprint", "confirmation"),
    [
        ("f" * 64, "APPLY TENANT AI POLICY"),
        ("proposal", "apply tenant ai policy"),
    ],
)
def test_apply_rejects_non_exact_confirmation_before_write(
    fingerprint: str,
    confirmation: str,
) -> None:
    store = _Store()
    operator = TenantAiPolicyOperator(store)
    proposal = operator.prepare(
        workspace_id="workspace-alpha",
        expected_version=0,
        desired=_desired(),
        updated_by="platform-admin",
    )
    confirmed_fingerprint = proposal.fingerprint if fingerprint == "proposal" else fingerprint

    with pytest.raises(TenantAiPolicyError) as captured:
        operator.apply(
            proposal,
            expected_proposal_fingerprint=confirmed_fingerprint,
            confirmation=confirmation,
        )

    assert captured.value.code is TenantAiPolicyErrorCode.CONFIRMATION_MISMATCH
    assert store.writes == []


def test_stale_state_fingerprint_fails_closed_before_write() -> None:
    store = _Store(current=_snapshot(version=1, desired=_desired(enabled=False)))
    operator = TenantAiPolicyOperator(store)
    proposal = operator.prepare(
        workspace_id="workspace-alpha",
        expected_version=1,
        desired=_desired(enabled=True),
        updated_by="platform-admin",
    )
    store.current = _snapshot(
        version=1,
        desired=_desired(enabled=False),
        updated_at=NOW + timedelta(seconds=1),
    )

    with pytest.raises(TenantAiPolicyError) as captured:
        operator.apply(
            proposal,
            expected_proposal_fingerprint=proposal.fingerprint,
            confirmation="APPLY TENANT AI POLICY",
        )

    assert captured.value.code is TenantAiPolicyErrorCode.STATE_CONFLICT
    assert store.writes == []


def test_prepare_rejects_noop_and_invalid_governance_shape() -> None:
    disabled = _desired(enabled=False)
    store = _Store(current=_snapshot(version=1, desired=disabled))
    operator = TenantAiPolicyOperator(store)

    with pytest.raises(TenantAiPolicyError) as no_change:
        operator.prepare(
            workspace_id="workspace-alpha",
            expected_version=1,
            desired=disabled,
            updated_by="platform-admin",
        )
    assert no_change.value.code is TenantAiPolicyErrorCode.NO_CHANGE

    invalid = TenantAiPolicyDesired(
        **{
            **asdict(_desired(enabled=True)),
            "provider_governance_accepted": False,
            "provider_governance_fingerprint": None,
        }
    )
    with pytest.raises(TenantAiPolicyError) as bad_shape:
        operator.prepare(
            workspace_id="workspace-alpha",
            expected_version=1,
            desired=invalid,
            updated_by="platform-admin",
        )
    assert bad_shape.value.code is TenantAiPolicyErrorCode.INVALID_REQUEST
