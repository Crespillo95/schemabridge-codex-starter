"""Exact inspect/prepare/apply operator flow for tenant external-AI policy."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum

from schemabridge.application.ports.query_studio_ai_control import (
    AiControlError,
    AiControlErrorCode,
    TenantAiPolicyConfirmation,
    TenantAiPolicyOperatorPort,
    TenantAiPolicyOperatorSnapshot,
    TenantAiPolicyWrite,
)
from schemabridge.domain.query_studio import query_studio_fingerprint


class TenantAiPolicyErrorCode(StrEnum):
    INVALID_REQUEST = "tenant_ai_policy_invalid_request"
    UNAVAILABLE = "tenant_ai_policy_unavailable"
    VERSION_CONFLICT = "tenant_ai_policy_version_conflict"
    STATE_CONFLICT = "tenant_ai_policy_state_conflict"
    NO_CHANGE = "tenant_ai_policy_no_change"
    CONFIRMATION_MISMATCH = "tenant_ai_policy_confirmation_mismatch"
    INVALID_RESPONSE = "tenant_ai_policy_invalid_response"


class TenantAiPolicyError(RuntimeError):
    """Sanitized operator failure without endpoint, prompt, identity, or credential data."""

    def __init__(self, code: TenantAiPolicyErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class TenantAiPolicyDesired:
    external_ai_enabled: bool
    provider_governance_accepted: bool
    provider_governance_fingerprint: str | None
    model_snapshot: str
    endpoint_region: str
    endpoint_origin_fingerprint: str
    configuration_fingerprint: str
    requests_per_minute: int
    daily_input_token_limit: int
    daily_output_token_limit: int
    concurrent_attempt_limit: int
    reservation_lease_seconds: int
    audit_retention_seconds: int

    @property
    def fingerprint(self) -> str:
        return query_studio_fingerprint(asdict(self))


@dataclass(frozen=True, slots=True)
class TenantAiPolicyProposal:
    workspace_id: str
    expected_version: int
    expected_state_fingerprint: str
    desired: TenantAiPolicyDesired
    updated_by: str
    fingerprint: str

    def __post_init__(self) -> None:
        if self.fingerprint != _proposal_fingerprint(
            workspace_id=self.workspace_id,
            expected_version=self.expected_version,
            expected_state_fingerprint=self.expected_state_fingerprint,
            desired=self.desired,
            updated_by=self.updated_by,
        ):
            raise ValueError("tenant AI policy proposal fingerprint is invalid")


@dataclass(frozen=True, slots=True)
class TenantAiPolicyOperator:
    """Keep the policy read-only until an exact fresh proposal is confirmed."""

    store: TenantAiPolicyOperatorPort

    def inspect(self, workspace_id: str) -> TenantAiPolicyOperatorSnapshot | None:
        try:
            return self.store.inspect(workspace_id)
        except (AiControlError, TypeError, ValueError) as error:
            raise TenantAiPolicyError(
                TenantAiPolicyErrorCode.UNAVAILABLE,
                "tenant AI policy inspection is unavailable",
            ) from error

    def prepare(
        self,
        *,
        workspace_id: str,
        expected_version: int,
        desired: TenantAiPolicyDesired,
        updated_by: str,
    ) -> TenantAiPolicyProposal:
        _validate_desired(
            workspace_id=workspace_id,
            expected_version=expected_version,
            desired=desired,
            updated_by=updated_by,
        )
        current = self.inspect(workspace_id)
        current_version = 0 if current is None else current.version
        if current_version != expected_version:
            raise TenantAiPolicyError(
                TenantAiPolicyErrorCode.VERSION_CONFLICT,
                "tenant AI policy version changed before preparation",
            )
        if current is not None and _desired_payload(desired) == _snapshot_payload(current):
            raise TenantAiPolicyError(
                TenantAiPolicyErrorCode.NO_CHANGE,
                "tenant AI policy proposal would not change governed policy",
            )
        expected_state_fingerprint = _state_fingerprint(workspace_id, current)
        fingerprint = _proposal_fingerprint(
            workspace_id=workspace_id,
            expected_version=expected_version,
            expected_state_fingerprint=expected_state_fingerprint,
            desired=desired,
            updated_by=updated_by,
        )
        return TenantAiPolicyProposal(
            workspace_id=workspace_id,
            expected_version=expected_version,
            expected_state_fingerprint=expected_state_fingerprint,
            desired=desired,
            updated_by=updated_by,
            fingerprint=fingerprint,
        )

    def apply(
        self,
        proposal: TenantAiPolicyProposal,
        *,
        expected_proposal_fingerprint: str,
        confirmation: str,
    ) -> TenantAiPolicyOperatorSnapshot:
        if expected_proposal_fingerprint != proposal.fingerprint:
            raise TenantAiPolicyError(
                TenantAiPolicyErrorCode.CONFIRMATION_MISMATCH,
                "tenant AI policy proposal fingerprint was not confirmed exactly",
            )
        if confirmation != TenantAiPolicyConfirmation.APPLY.value:
            raise TenantAiPolicyError(
                TenantAiPolicyErrorCode.CONFIRMATION_MISMATCH,
                "tenant AI policy confirmation phrase is invalid",
            )
        current = self.inspect(proposal.workspace_id)
        if (0 if current is None else current.version) != proposal.expected_version:
            raise TenantAiPolicyError(
                TenantAiPolicyErrorCode.VERSION_CONFLICT,
                "tenant AI policy version changed after preparation",
            )
        if _state_fingerprint(proposal.workspace_id, current) != (
            proposal.expected_state_fingerprint
        ):
            raise TenantAiPolicyError(
                TenantAiPolicyErrorCode.STATE_CONFLICT,
                "tenant AI policy state changed after preparation",
            )
        change = _validated_write(
            workspace_id=proposal.workspace_id,
            expected_version=proposal.expected_version,
            desired=proposal.desired,
            updated_by=proposal.updated_by,
        )
        try:
            applied = self.store.apply(change)
        except AiControlError as error:
            code = (
                TenantAiPolicyErrorCode.VERSION_CONFLICT
                if error.code is AiControlErrorCode.IDEMPOTENCY_CONFLICT
                else TenantAiPolicyErrorCode.UNAVAILABLE
            )
            raise TenantAiPolicyError(
                code,
                "tenant AI policy apply operation is unavailable",
            ) from error
        except (TypeError, ValueError) as error:
            raise TenantAiPolicyError(
                TenantAiPolicyErrorCode.INVALID_RESPONSE,
                "tenant AI policy apply response is invalid",
            ) from error
        if (
            applied.workspace_id != proposal.workspace_id
            or applied.version != proposal.expected_version + 1
            or applied.updated_by != proposal.updated_by
            or _snapshot_payload(applied) != _desired_payload(proposal.desired)
        ):
            raise TenantAiPolicyError(
                TenantAiPolicyErrorCode.INVALID_RESPONSE,
                "tenant AI policy apply response did not match the exact proposal",
            )
        return applied


def _validated_write(
    *,
    workspace_id: str,
    expected_version: int,
    desired: TenantAiPolicyDesired,
    updated_by: str,
) -> TenantAiPolicyWrite:
    try:
        return TenantAiPolicyWrite(
            workspace_id=workspace_id,
            expected_version=expected_version,
            external_ai_enabled=desired.external_ai_enabled,
            provider_governance_accepted=desired.provider_governance_accepted,
            provider_governance_fingerprint=desired.provider_governance_fingerprint,
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
            updated_by=updated_by,
            confirmation=TenantAiPolicyConfirmation.APPLY,
        )
    except (TypeError, ValueError) as error:
        raise TenantAiPolicyError(
            TenantAiPolicyErrorCode.INVALID_REQUEST,
            "tenant AI policy proposal contains invalid values",
        ) from error


def _validate_desired(
    *,
    workspace_id: str,
    expected_version: int,
    desired: TenantAiPolicyDesired,
    updated_by: str,
) -> None:
    _validated_write(
        workspace_id=workspace_id,
        expected_version=expected_version,
        desired=desired,
        updated_by=updated_by,
    )


def _desired_payload(desired: TenantAiPolicyDesired) -> dict[str, object]:
    return asdict(desired)


def _snapshot_payload(snapshot: TenantAiPolicyOperatorSnapshot) -> dict[str, object]:
    return {
        "audit_retention_seconds": snapshot.audit_retention_seconds,
        "concurrent_attempt_limit": snapshot.concurrent_attempt_limit,
        "configuration_fingerprint": snapshot.configuration_fingerprint,
        "daily_input_token_limit": snapshot.daily_input_token_limit,
        "daily_output_token_limit": snapshot.daily_output_token_limit,
        "endpoint_origin_fingerprint": snapshot.endpoint_origin_fingerprint,
        "endpoint_region": snapshot.endpoint_region,
        "external_ai_enabled": snapshot.external_ai_enabled,
        "model_snapshot": snapshot.model_snapshot,
        "provider_governance_accepted": snapshot.provider_governance_accepted,
        "provider_governance_fingerprint": snapshot.provider_governance_fingerprint,
        "requests_per_minute": snapshot.requests_per_minute,
        "reservation_lease_seconds": snapshot.reservation_lease_seconds,
    }


def _state_fingerprint(
    workspace_id: str,
    snapshot: TenantAiPolicyOperatorSnapshot | None,
) -> str:
    if snapshot is not None:
        return snapshot.fingerprint
    return query_studio_fingerprint(
        {
            "kind": "tenant_ai_policy_absent",
            "version": 1,
            "workspace_id": workspace_id,
        }
    )


def _proposal_fingerprint(
    *,
    workspace_id: str,
    expected_version: int,
    expected_state_fingerprint: str,
    desired: TenantAiPolicyDesired,
    updated_by: str,
) -> str:
    return query_studio_fingerprint(
        {
            "desired": _desired_payload(desired),
            "expected_state_fingerprint": expected_state_fingerprint,
            "expected_version": expected_version,
            "kind": "tenant_ai_policy_proposal",
            "updated_by": updated_by,
            "version": 1,
            "workspace_id": workspace_id,
        }
    )


__all__ = [
    "TenantAiPolicyDesired",
    "TenantAiPolicyError",
    "TenantAiPolicyErrorCode",
    "TenantAiPolicyOperator",
    "TenantAiPolicyProposal",
]
