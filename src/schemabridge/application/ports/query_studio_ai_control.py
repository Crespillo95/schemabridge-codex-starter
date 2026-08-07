"""Sanitized control-plane ports for Query Studio provider admission and accounting."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from schemabridge.domain.query_studio import ProviderStage, query_studio_fingerprint


class AiAdmissionOutcome(StrEnum):
    RESERVED = "reserved"
    REPLAYED = "replayed"
    POLICY_DISABLED = "policy_disabled"
    POLICY_MISMATCH = "policy_mismatch"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    CONCURRENCY_LIMITED = "concurrency_limited"


class AiAttemptStatus(StrEnum):
    RESERVED = "reserved"
    SETTLED = "settled"
    EXPIRED = "expired"


class AiSettlementOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    REFUSAL = "refusal"
    MISSING_OUTPUT = "missing_output"
    INVALID_OUTPUT = "invalid_output"
    EXPIRED_CRASH = "expired_crash"


class AiControlErrorCode(StrEnum):
    RESOURCE_UNAVAILABLE = "ai_control_resource_unavailable"
    INVALID_RESPONSE = "ai_control_invalid_response"
    IDEMPOTENCY_CONFLICT = "ai_control_idempotency_conflict"
    RESERVATION_UNAVAILABLE = "ai_control_reservation_unavailable"
    LEASE_STALE = "ai_control_lease_stale"


class AiControlError(RuntimeError):
    """A bounded failure that contains no prompt, provider payload, SQL, or credential."""

    def __init__(self, code: AiControlErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class TenantAiPolicyConfirmation(StrEnum):
    """Closed phrase required in addition to the exact prepared fingerprint."""

    APPLY = "APPLY TENANT AI POLICY"


@dataclass(frozen=True, slots=True)
class TenantAiPolicyOperatorSnapshot:
    """Complete non-secret policy state available only to the migrator operator."""

    workspace_id: str
    version: int
    external_ai_enabled: bool
    provider_governance_accepted: bool
    provider_governance_fingerprint: str | None
    provider_governance_accepted_at: datetime | None
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
    updated_by: str
    updated_at: datetime

    def __post_init__(self) -> None:
        _validate_policy_values(
            workspace_id=self.workspace_id,
            version=self.version,
            external_ai_enabled=self.external_ai_enabled,
            provider_governance_accepted=self.provider_governance_accepted,
            provider_governance_fingerprint=self.provider_governance_fingerprint,
            provider_governance_accepted_at=self.provider_governance_accepted_at,
            model_snapshot=self.model_snapshot,
            endpoint_region=self.endpoint_region,
            endpoint_origin_fingerprint=self.endpoint_origin_fingerprint,
            configuration_fingerprint=self.configuration_fingerprint,
            requests_per_minute=self.requests_per_minute,
            daily_input_token_limit=self.daily_input_token_limit,
            daily_output_token_limit=self.daily_output_token_limit,
            concurrent_attempt_limit=self.concurrent_attempt_limit,
            reservation_lease_seconds=self.reservation_lease_seconds,
            audit_retention_seconds=self.audit_retention_seconds,
            updated_by=self.updated_by,
            updated_at=self.updated_at,
        )

    @property
    def fingerprint(self) -> str:
        return query_studio_fingerprint(
            {
                "audit_retention_seconds": self.audit_retention_seconds,
                "concurrent_attempt_limit": self.concurrent_attempt_limit,
                "configuration_fingerprint": self.configuration_fingerprint,
                "daily_input_token_limit": self.daily_input_token_limit,
                "daily_output_token_limit": self.daily_output_token_limit,
                "endpoint_origin_fingerprint": self.endpoint_origin_fingerprint,
                "endpoint_region": self.endpoint_region,
                "external_ai_enabled": self.external_ai_enabled,
                "model_snapshot": self.model_snapshot,
                "provider_governance_accepted": self.provider_governance_accepted,
                "provider_governance_accepted_at": (
                    None
                    if self.provider_governance_accepted_at is None
                    else self.provider_governance_accepted_at.isoformat()
                ),
                "provider_governance_fingerprint": self.provider_governance_fingerprint,
                "requests_per_minute": self.requests_per_minute,
                "reservation_lease_seconds": self.reservation_lease_seconds,
                "updated_at": self.updated_at.isoformat(),
                "updated_by": self.updated_by,
                "version": self.version,
                "workspace_id": self.workspace_id,
            }
        )


@dataclass(frozen=True, slots=True)
class TenantAiPolicyWrite:
    """Exact optimistic write sent only through the migrator capability."""

    workspace_id: str
    expected_version: int
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
    updated_by: str
    confirmation: TenantAiPolicyConfirmation

    def __post_init__(self) -> None:
        _validate_policy_values(
            workspace_id=self.workspace_id,
            version=self.expected_version,
            allow_zero_version=True,
            external_ai_enabled=self.external_ai_enabled,
            provider_governance_accepted=self.provider_governance_accepted,
            provider_governance_fingerprint=self.provider_governance_fingerprint,
            provider_governance_accepted_at=None,
            model_snapshot=self.model_snapshot,
            endpoint_region=self.endpoint_region,
            endpoint_origin_fingerprint=self.endpoint_origin_fingerprint,
            configuration_fingerprint=self.configuration_fingerprint,
            requests_per_minute=self.requests_per_minute,
            daily_input_token_limit=self.daily_input_token_limit,
            daily_output_token_limit=self.daily_output_token_limit,
            concurrent_attempt_limit=self.concurrent_attempt_limit,
            reservation_lease_seconds=self.reservation_lease_seconds,
            audit_retention_seconds=self.audit_retention_seconds,
            updated_by=self.updated_by,
            updated_at=None,
            validate_observation_times=False,
        )
        if not isinstance(self.confirmation, TenantAiPolicyConfirmation):
            raise ValueError("tenant AI policy confirmation is invalid")


@dataclass(frozen=True, slots=True)
class TenantAiPolicySnapshot:
    version: int
    external_ai_enabled: bool
    provider_governance_accepted: bool
    model_snapshot: str
    endpoint_region: str
    configuration_fingerprint: str
    requests_per_minute: int
    daily_input_token_limit: int
    daily_output_token_limit: int
    concurrent_attempt_limit: int
    reservation_lease_seconds: int
    audit_retention_seconds: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AiAttemptReservationRequest:
    workspace_id: str
    request_id: str
    actor_digest: str
    stage: ProviderStage
    attempt_number: int
    idempotency_digest: str
    request_fingerprint: str
    semantic_scope_fingerprint: str
    semantic_payload_fingerprint: str
    configuration_fingerprint: str
    estimated_input_tokens: int
    estimated_output_tokens: int
    lease_capability: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class AiAttemptReservation:
    outcome: AiAdmissionOutcome
    replayed: bool
    reservation_id: str | None
    status: AiAttemptStatus | None
    fencing_token: int | None
    lease_expires_at: datetime | None
    policy_version: int
    model_snapshot: str | None
    endpoint_region: str | None
    configuration_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class AiAttemptSettlementRequest:
    workspace_id: str
    reservation_id: str
    fencing_token: int
    outcome: AiSettlementOutcome
    reserved_input_tokens: int
    reserved_output_tokens: int
    observed_input_tokens: int | None
    observed_output_tokens: int | None
    duration_ms: int
    lease_capability: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class AiAttemptSettlement:
    reservation_id: str
    replayed: bool
    status: AiAttemptStatus
    outcome: AiSettlementOutcome
    charged_input_tokens: int
    charged_output_tokens: int
    settled_at: datetime
    audit_id: str


class TenantAiPolicyReadPort(Protocol):
    def load_policy(self, workspace_id: str) -> TenantAiPolicySnapshot | None:
        """Load one sanitized tenant policy without endpoint or credential material."""


class TenantAiPolicyOperatorPort(Protocol):
    """Migrator-only exact inspection and revision boundary."""

    def inspect(self, workspace_id: str) -> TenantAiPolicyOperatorSnapshot | None:
        """Read one complete non-secret policy state for an exact proposal."""

    def apply(self, change: TenantAiPolicyWrite) -> TenantAiPolicyOperatorSnapshot:
        """Apply one optimistic exact-confirmation revision and inspect its result."""


class AiProviderAttemptControlPort(Protocol):
    def reserve(self, request: AiAttemptReservationRequest) -> AiAttemptReservation:
        """Atomically reserve request, token, and concurrency capacity."""

    def settle(self, request: AiAttemptSettlementRequest) -> AiAttemptSettlement:
        """Settle an exact fenced lease once and persist sanitized usage."""

    def expire(self, workspace_id: str, *, limit: int = 100) -> int:
        """Conservatively charge and release a bounded set of expired attempts."""


_MODELS = frozenset(
    {
        "gpt-5-nano-2025-08-07",
        "gpt-5.4-nano-2026-03-17",
        "gpt-5.6-luna",
    }
)
_ENDPOINT_ORIGIN_FINGERPRINTS: Mapping[str, str] = MappingProxyType(
    {
        "global": ("6836a1bf89b42ed7a325b83c7825e0ae3a17048d4d7395d0606ee18f2800f71c"),
        "eu": ("83203af93d5b1b9a2b7ab344441b99f7e19d980c881ba78de44e16073bd199c7"),
        "us": ("c0559fdcd4b90b58e8af98b3fd4be11859e79e0718dc086422773bd6aa38dc85"),
    }
)
_REGIONS = frozenset(_ENDPOINT_ORIGIN_FINGERPRINTS)


def _validate_policy_values(
    *,
    workspace_id: str,
    version: int,
    external_ai_enabled: bool,
    provider_governance_accepted: bool,
    provider_governance_fingerprint: str | None,
    provider_governance_accepted_at: datetime | None,
    model_snapshot: str,
    endpoint_region: str,
    endpoint_origin_fingerprint: str,
    configuration_fingerprint: str,
    requests_per_minute: int,
    daily_input_token_limit: int,
    daily_output_token_limit: int,
    concurrent_attempt_limit: int,
    reservation_lease_seconds: int,
    audit_retention_seconds: int,
    updated_by: str,
    updated_at: datetime | None,
    allow_zero_version: bool = False,
    validate_observation_times: bool = True,
) -> None:
    if (
        not _inert_identifier(workspace_id)
        or not _inert_identifier(updated_by)
        or type(version) is not int
        or version < (0 if allow_zero_version else 1)
        or type(external_ai_enabled) is not bool
        or type(provider_governance_accepted) is not bool
        or model_snapshot not in _MODELS
        or endpoint_region not in _REGIONS
        or (endpoint_origin_fingerprint != _ENDPOINT_ORIGIN_FINGERPRINTS.get(endpoint_region))
        or not _sha256(configuration_fingerprint)
        or type(requests_per_minute) is not int
        or not 1 <= requests_per_minute <= 10_000
        or type(daily_input_token_limit) is not int
        or not 1 <= daily_input_token_limit <= 1_000_000_000
        or type(daily_output_token_limit) is not int
        or not 1 <= daily_output_token_limit <= 1_000_000_000
        or type(concurrent_attempt_limit) is not int
        or not 1 <= concurrent_attempt_limit <= 1_000
        or type(reservation_lease_seconds) is not int
        or not 10 <= reservation_lease_seconds <= 300
        or type(audit_retention_seconds) is not int
        or not 2_592_000 <= audit_retention_seconds <= 315_360_000
        or (
            validate_observation_times
            and (updated_at is None or updated_at.tzinfo is None or updated_at.utcoffset() is None)
        )
        or (
            provider_governance_accepted
            and (
                not _sha256(provider_governance_fingerprint)
                or (validate_observation_times and provider_governance_accepted_at is None)
            )
        )
        or (
            not provider_governance_accepted
            and (
                provider_governance_fingerprint is not None
                or (validate_observation_times and provider_governance_accepted_at is not None)
                or external_ai_enabled
            )
        )
        or (
            validate_observation_times
            and provider_governance_accepted_at is not None
            and (
                provider_governance_accepted_at.tzinfo is None
                or provider_governance_accepted_at.utcoffset() is None
            )
        )
    ):
        raise ValueError("tenant AI policy values are invalid")


def _sha256(value: str | None) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _inert_identifier(value: str) -> bool:
    return (
        isinstance(value, str)
        and 3 <= len(value) <= 200
        and len(value.encode("utf-8")) <= 200
        and value[0] in "abcdefghijklmnopqrstuvwxyz0123456789"
        and all(character in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in value)
    )


__all__ = [
    "AiAdmissionOutcome",
    "AiAttemptReservation",
    "AiAttemptReservationRequest",
    "AiAttemptSettlement",
    "AiAttemptSettlementRequest",
    "AiAttemptStatus",
    "AiControlError",
    "AiControlErrorCode",
    "AiProviderAttemptControlPort",
    "AiSettlementOutcome",
    "TenantAiPolicyConfirmation",
    "TenantAiPolicyOperatorPort",
    "TenantAiPolicyOperatorSnapshot",
    "TenantAiPolicyReadPort",
    "TenantAiPolicySnapshot",
    "TenantAiPolicyWrite",
]
