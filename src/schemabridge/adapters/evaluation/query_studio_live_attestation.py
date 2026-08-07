"""Provider-free M27 campaign-to-ledger attestation.

The persisted report intentionally contains only content hashes, policy facts,
counts, and aggregate usage. Reservation/audit identifiers and the remaining
immutable ledger facts participate in the internal canonical digest but never
leave this adapter.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal

import psycopg
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from schemabridge.adapters.evaluation.query_studio_live import (
    load_query_studio_qualification_campaign_report,
    read_bounded_regular_file,
    reject_symlink_ancestors,
)

CANONICAL_LIVE_HISTORY_DIRECTORY: Final = Path("reports/m27-query-studio-live-history")
CANONICAL_CAMPAIGN_FILE_SHA256: Final = (
    "beef3391aef2ea2cedfdfbc6e065ce259f7143861ccc0470197c6edcb88ba30a"
)
CANONICAL_CAMPAIGN_REPORT_SHA256: Final = (
    "e13a63fd2add281e567e3b4e4086ba3bb8510ec6e554608533a4c532215c0b7f"
)
CANONICAL_CAMPAIGN_ORDERED_EXECUTION_WITNESS_SHA256: Final = (
    "30128ad06cef91b12e1912eb9bbb25a3588447b5ee3cdaec4cc82dec882102fc"
)
CANONICAL_CAMPAIGN_PATH: Final = CANONICAL_LIVE_HISTORY_DIRECTORY / (
    f"campaign-signed-{CANONICAL_CAMPAIGN_FILE_SHA256}.json"
)
CANONICAL_CAMPAIGN_PLAN_VERSION: Final = "m27-cheapest-first-campaign-v11"
CANONICAL_PREFLIGHT_PIPELINE_SHA256: Final = (
    "42e017992067ab4f666017dbc1e65f4e695ed8f044052cbfa0d2b6a63cf81ffb"
)
CANONICAL_MODEL_SNAPSHOT: Final = "gpt-5-nano-2025-08-07"
CANONICAL_CONFIGURATION_FINGERPRINT: Final = (
    "f5fb0b20d85edca1a9780d76161342760f7d25ee4dc829fb686ec737f05cb0e7"
)
CANONICAL_GOVERNANCE_FINGERPRINT: Final = (
    "4f1e6df707017309916e17e9d2360d58de2fe4c276bf72994fd977ec5bd2b85a"
)
CANONICAL_ENDPOINT_ORIGIN_FINGERPRINT: Final = (
    "6836a1bf89b42ed7a325b83c7825e0ae3a17048d4d7395d0606ee18f2800f71c"
)
CANONICAL_SEMANTIC_SCOPE_FINGERPRINT: Final = (
    "cdd4b931cbc3de91d9668ad18f10c7e4c3f35870d08df5aa853f8a757334d406"
)
QUALIFICATION_POLICY_VERSION: Final = 83
DISABLED_POLICY_VERSION: Final = 84
CANONICAL_PROVIDER_ATTEMPTS: Final = 16
CANONICAL_INPUT_TOKENS: Final = 15_715
CANONICAL_OUTPUT_REASONING_TOKENS: Final = 1_204
CANONICAL_EVALUATOR_ADAPTER_DURATION_MS: Final = 30_016
CANONICAL_PROVIDER_BOUNDARY_DURATION_MS: Final = 29_961
CANONICAL_REQUESTS_PER_MINUTE: Final = 1_000
CANONICAL_DAILY_INPUT_TOKEN_LIMIT: Final = 4_250_000
CANONICAL_DAILY_OUTPUT_TOKEN_LIMIT: Final = 200_000
CANONICAL_CONCURRENT_ATTEMPT_LIMIT: Final = 1
CANONICAL_RESERVATION_LEASE_SECONDS: Final = 60
CANONICAL_AUDIT_RETENTION_SECONDS: Final = 2_592_000
CANONICAL_DISABLE_AFTER_LAST_SETTLEMENT_MICROSECONDS: Final = 6_753_566

_ATTESTATION_DOMAIN: Final = "schemabridge:m27:query-studio:campaign-ledger-attestation:v2"
_ATTESTATION_KEY_DOMAIN: Final = b"schemabridge:m27:query-studio:campaign-ledger-attestation:key:v2"
_LEDGER_DIGEST_DOMAIN: Final = "schemabridge:m27:query-studio:campaign-ledger-digest:v2"
_ATTEMPT_IDENTITY_DOMAIN: Final = "schemabridge:m27:query-studio:campaign-attempt:v2"
_ORDERED_EXECUTION_WITNESS_DOMAIN: Final = (
    "schemabridge:m27:query-studio:campaign-ordered-execution-witness:v2"
)
_ORDINAL_BINDING_DOMAIN: Final = "schemabridge:m27:query-studio:campaign-ledger-ordinal-binding:v2"
_ATTESTATION_FILE: Final = re.compile(r"^campaign-ledger-attestation-([0-9a-f]{64})\.json$")
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_KEY_VERSION: Final = re.compile(r"^v[1-9][0-9]{0,5}$")
_RESERVATION_ID: Final = re.compile(r"^air_[0-9a-f]{64}$")
_AUDIT_ID: Final = re.compile(r"^aia_[0-9a-f]{64}$")
_MAX_ATTESTATION_BYTES: Final = 128 * 1024
_MAX_CAMPAIGN_BYTES: Final = 4 * 1024 * 1024
_MAX_LEDGER_ROWS: Final = 180
_STATEMENT_TIMEOUT_MS: Final = 5_000


class CampaignLedgerAttestationError(ValueError):
    """One sanitized, fail-closed attestation error."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CampaignAttemptWitness(_FrozenModel):
    """One private, text-free provider attempt extracted from signed evidence."""

    sequence: int = Field(ge=1, le=CANONICAL_PROVIDER_ATTEMPTS)
    case_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage: Literal["interpretation"]
    model_snapshot: str = Field(min_length=2, max_length=120)
    configuration_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_tokens: int = Field(ge=1, le=250_000)
    output_reasoning_tokens: int = Field(ge=1, le=40_000)
    evaluator_adapter_duration_ms: int = Field(ge=0, le=3_600_000)


class CampaignEvidenceFacts(_FrozenModel):
    """Safe facts extracted only after the canonical campaign verifies."""

    whole_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    logical_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_version: str = Field(min_length=1, max_length=80)
    provider_free_preflight_pipeline_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_outcome: Literal["selected"]
    selected_model: str = Field(min_length=2, max_length=120)
    policy_version: int = Field(ge=1, le=1_000_000)
    configuration_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_attempts: int = Field(ge=0, le=180)
    input_tokens: int = Field(ge=0, le=250_000)
    output_reasoning_tokens: int = Field(ge=0, le=40_000)
    evaluator_adapter_duration_ms: int = Field(ge=0, le=3_600_000)
    attempts: tuple[CampaignAttemptWitness, ...] = Field(
        min_length=CANONICAL_PROVIDER_ATTEMPTS,
        max_length=CANONICAL_PROVIDER_ATTEMPTS,
    )
    ordered_execution_witness_digest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def must_be_the_terminal_canonical_campaign(self) -> CampaignEvidenceFacts:
        if (
            self.whole_file_sha256 != CANONICAL_CAMPAIGN_FILE_SHA256
            or self.logical_report_sha256 != CANONICAL_CAMPAIGN_REPORT_SHA256
            or self.plan_version != CANONICAL_CAMPAIGN_PLAN_VERSION
            or self.provider_free_preflight_pipeline_sha256 != CANONICAL_PREFLIGHT_PIPELINE_SHA256
            or self.selected_model != CANONICAL_MODEL_SNAPSHOT
            or self.policy_version != QUALIFICATION_POLICY_VERSION
            or self.configuration_fingerprint != CANONICAL_CONFIGURATION_FINGERPRINT
            or self.provider_attempts != CANONICAL_PROVIDER_ATTEMPTS
            or self.input_tokens != CANONICAL_INPUT_TOKENS
            or self.output_reasoning_tokens != CANONICAL_OUTPUT_REASONING_TOKENS
            or self.evaluator_adapter_duration_ms != CANONICAL_EVALUATOR_ADAPTER_DURATION_MS
            or tuple(item.sequence for item in self.attempts)
            != tuple(range(1, CANONICAL_PROVIDER_ATTEMPTS + 1))
            or len({item.case_identity_sha256 for item in self.attempts})
            != CANONICAL_PROVIDER_ATTEMPTS
            or any(
                item.stage != "interpretation"
                or item.model_snapshot != CANONICAL_MODEL_SNAPSHOT
                or item.configuration_fingerprint != CANONICAL_CONFIGURATION_FINGERPRINT
                for item in self.attempts
            )
            or sum(item.input_tokens for item in self.attempts) != self.input_tokens
            or sum(item.output_reasoning_tokens for item in self.attempts)
            != self.output_reasoning_tokens
            or sum(item.evaluator_adapter_duration_ms for item in self.attempts)
            != self.evaluator_adapter_duration_ms
            or self.ordered_execution_witness_digest_sha256
            != CANONICAL_CAMPAIGN_ORDERED_EXECUTION_WITNESS_SHA256
            or _ordered_execution_witness_digest(self.attempts)
            != self.ordered_execution_witness_digest_sha256
        ):
            raise ValueError("campaign evidence differs from the reviewed terminal run")
        return self


class AiPolicyRevisionFacts(_FrozenModel):
    """Complete immutable revision facts retained only in the digest preimage."""

    workspace_id: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=1, le=1_000_000)
    external_ai_enabled: bool
    provider_governance_accepted: bool
    provider_governance_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    provider_governance_accepted_at: datetime | None
    model_snapshot: str = Field(min_length=2, max_length=80)
    endpoint_region: Literal["global", "eu", "us"]
    endpoint_origin_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    configuration_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    requests_per_minute: int = Field(ge=1, le=10_000)
    daily_input_token_limit: int = Field(ge=1, le=1_000_000_000)
    daily_output_token_limit: int = Field(ge=1, le=1_000_000_000)
    concurrent_attempt_limit: int = Field(ge=1, le=1_000)
    reservation_lease_seconds: int = Field(ge=10, le=300)
    audit_retention_seconds: int = Field(
        ge=2_592_000,
        le=315_360_000,
    )
    updated_by: str = Field(min_length=3, max_length=200)
    updated_at: datetime

    @field_validator(
        "provider_governance_accepted_at",
        "updated_at",
    )
    @classmethod
    def timestamps_must_be_aware(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("policy timestamp must be timezone aware")
        return value

    @model_validator(mode="after")
    def governance_shape_must_be_exact(self) -> AiPolicyRevisionFacts:
        if self.provider_governance_accepted is (
            self.provider_governance_fingerprint is None
            or self.provider_governance_accepted_at is None
        ):
            raise ValueError("policy governance facts are inconsistent")
        if self.external_ai_enabled and not self.provider_governance_accepted:
            raise ValueError("enabled external AI lacks accepted governance")
        return self


class LedgerReservationAuditFacts(_FrozenModel):
    """One internal 1:1 reservation/audit pair; never serialized publicly."""

    reservation_id: str = Field(pattern=r"^air_[0-9a-f]{64}$")
    audit_id: str = Field(pattern=r"^aia_[0-9a-f]{64}$")
    audit_reservation_id: str = Field(pattern=r"^air_[0-9a-f]{64}$")
    workspace_id: str = Field(min_length=1, max_length=200)
    request_id: str = Field(pattern=r"^airq_[0-9a-f]{48}$")
    actor_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage: Literal["expansion", "interpretation"]
    attempt_number: int = Field(ge=1, le=2)
    idempotency_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_scope_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_payload_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    configuration_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: int = Field(ge=1, le=1_000_000)
    model_snapshot: str = Field(min_length=2, max_length=80)
    endpoint_region: Literal["global", "eu", "us"]
    estimated_input_tokens: int = Field(ge=1, le=1_000_000)
    estimated_output_tokens: int = Field(ge=1, le=1_000_000)
    audit_retention_seconds: int = Field(
        ge=2_592_000,
        le=315_360_000,
    )
    status: Literal["reserved", "settled", "expired"]
    fencing_token: int = Field(ge=1)
    lease_acquired_at: datetime
    lease_expires_at: datetime | None
    outcome_code: str | None = Field(default=None, max_length=32)
    observed_input_tokens: int | None = Field(
        default=None,
        ge=0,
        le=1_000_000_000,
    )
    observed_output_tokens: int | None = Field(
        default=None,
        ge=0,
        le=1_000_000_000,
    )
    charged_input_tokens: int | None = Field(
        default=None,
        ge=0,
        le=1_000_000_000,
    )
    charged_output_tokens: int | None = Field(
        default=None,
        ge=0,
        le=1_000_000_000,
    )
    duration_ms: int | None = Field(default=None, ge=0, le=3_600_000)
    settlement_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    created_at: datetime
    settled_at: datetime | None
    retain_until: datetime
    audit_workspace_scope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    audit_actor_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    audit_request_id: str = Field(pattern=r"^airq_[0-9a-f]{48}$")
    audit_stage: Literal["expansion", "interpretation"]
    audit_attempt_number: int = Field(ge=1, le=2)
    audit_model_snapshot: str = Field(min_length=2, max_length=80)
    audit_endpoint_region: Literal["global", "eu", "us"]
    audit_configuration_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    audit_request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    audit_semantic_scope_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    audit_semantic_payload_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    audit_input_tokens: int = Field(ge=1, le=1_000_000_000)
    audit_output_tokens: int = Field(ge=1, le=1_000_000_000)
    audit_duration_ms: int = Field(ge=0, le=3_600_000)
    audit_outcome_code: str = Field(min_length=1, max_length=32)
    audit_occurred_at: datetime
    audit_retain_until: datetime

    @field_validator(
        "lease_acquired_at",
        "lease_expires_at",
        "created_at",
        "settled_at",
        "retain_until",
        "audit_occurred_at",
        "audit_retain_until",
    )
    @classmethod
    def ledger_timestamps_must_be_aware(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("ledger timestamp must be timezone aware")
        return value

    @model_validator(mode="after")
    def pair_must_be_a_complete_derived_settlement(
        self,
    ) -> LedgerReservationAuditFacts:
        if self.outcome_code == "succeeded" and (
            self.status != "settled"
            or self.lease_expires_at is not None
            or self.created_at != self.lease_acquired_at
            or self.settled_at is None
            or self.settled_at <= self.created_at
            or self.observed_input_tokens is None
            or self.observed_output_tokens is None
            or self.observed_input_tokens != self.charged_input_tokens
            or self.observed_output_tokens != self.charged_output_tokens
            or self.observed_input_tokens != self.audit_input_tokens
            or self.observed_output_tokens != self.audit_output_tokens
            or self.observed_input_tokens > self.estimated_input_tokens
            or self.observed_output_tokens > self.estimated_output_tokens
            or self.retain_until
            != self.created_at + timedelta(seconds=self.audit_retention_seconds)
        ):
            raise ValueError("succeeded reservation terminal facts are inconsistent")
        if (
            self.reservation_id != self.audit_reservation_id
            or self.actor_digest != self.audit_actor_digest
            or self.request_id != self.audit_request_id
            or self.stage != self.audit_stage
            or self.attempt_number != self.audit_attempt_number
            or self.model_snapshot != self.audit_model_snapshot
            or self.endpoint_region != self.audit_endpoint_region
            or self.configuration_fingerprint != self.audit_configuration_fingerprint
            or self.request_fingerprint != self.audit_request_fingerprint
            or self.semantic_scope_fingerprint != self.audit_semantic_scope_fingerprint
            or self.semantic_payload_fingerprint != self.audit_semantic_payload_fingerprint
            or self.charged_input_tokens != self.audit_input_tokens
            or self.charged_output_tokens != self.audit_output_tokens
            or self.duration_ms != self.audit_duration_ms
            or self.outcome_code != self.audit_outcome_code
            or self.settled_at != self.audit_occurred_at
        ):
            raise ValueError("reservation/audit facts are inconsistent")
        if (
            self.settlement_fingerprint is None
            or self.settled_at is None
            or self.duration_ms is None
            or self.charged_input_tokens is None
            or self.charged_output_tokens is None
        ):
            raise ValueError("ledger pair is not terminal")
        expected_scope_digest = hashlib.sha256(
            f"query_studio_scope_v1|{self.workspace_id}".encode()
        ).hexdigest()
        expected_idempotency_digest = _fingerprint(
            {
                "attempt_number": self.attempt_number,
                "request_fingerprint": self.request_fingerprint,
                "request_id": self.request_id,
                "stage": self.stage,
                "version": 1,
                "workspace_id": self.workspace_id,
            }
        )
        expected_reservation_id = (
            "air_"
            + hashlib.sha256(
                "|".join(
                    (
                        "ai_attempt_reservation_v1",
                        self.workspace_id,
                        self.request_id,
                        self.stage,
                        str(self.attempt_number),
                        self.idempotency_digest,
                    )
                ).encode()
            ).hexdigest()
        )
        expected_audit_id = (
            "aia_"
            + hashlib.sha256(
                (self.reservation_id + self.settlement_fingerprint).encode()
            ).hexdigest()
        )
        expected_retain_until = self.settled_at + timedelta(seconds=self.audit_retention_seconds)
        if (
            self.audit_workspace_scope_digest != expected_scope_digest
            or self.reservation_id != expected_reservation_id
            or self.audit_id != expected_audit_id
            or self.audit_retain_until != expected_retain_until
            or self.idempotency_digest != expected_idempotency_digest
        ):
            raise ValueError("audit derivation is inconsistent")
        expected_settlement = _settlement_fingerprint(self)
        if self.settlement_fingerprint != expected_settlement:
            raise ValueError("settlement derivation is inconsistent")
        return self


class CampaignLedgerSnapshot(_FrozenModel):
    """Bounded database snapshot used only to derive the public attestation."""

    qualification_policy: AiPolicyRevisionFacts
    disabled_policy: AiPolicyRevisionFacts
    current_policy: AiPolicyRevisionFacts
    ledger_rows: tuple[LedgerReservationAuditFacts, ...] = Field(max_length=_MAX_LEDGER_ROWS)
    qualification_linked_audit_count: int = Field(ge=0, le=_MAX_LEDGER_ROWS)
    disabled_policy_linked_audit_count: int = Field(ge=0, le=_MAX_LEDGER_ROWS)
    orphan_audit_count: int = Field(ge=0, le=_MAX_LEDGER_ROWS)
    active_attempt_count: int = Field(ge=0, le=1_000)
    stale_reserved_count: int = Field(ge=0, le=1_000)

    @model_validator(mode="after")
    def rows_must_be_unique_and_canonically_ordered(
        self,
    ) -> CampaignLedgerSnapshot:
        identities = tuple((item.reservation_id, item.audit_id) for item in self.ledger_rows)
        chronology = tuple(
            (
                item.lease_acquired_at,
                item.created_at,
                item.reservation_id,
                item.audit_id,
            )
            for item in self.ledger_rows
        )
        if (
            chronology != tuple(sorted(chronology))
            or len({item[0] for item in identities}) != len(identities)
            or len({item[1] for item in identities}) != len(identities)
        ):
            raise ValueError("ledger snapshot identity set is invalid")
        for previous, current in zip(self.ledger_rows, self.ledger_rows[1:], strict=False):
            if previous.settled_at is None or current.lease_acquired_at <= previous.settled_at:
                raise ValueError("ledger attempts are not strictly sequential")
        return self


class PublicCampaignLink(_FrozenModel):
    """Content-addressed campaign identity without its raw report."""

    whole_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    logical_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_version: str = Field(min_length=1, max_length=80)
    provider_free_preflight_pipeline_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_outcome: Literal["selected"]
    selected_model: str = Field(min_length=2, max_length=120)
    ordered_execution_witness_digest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PublicPolicyFacts(_FrozenModel):
    """Minimal reviewed policy facts safe to retain in the attestation."""

    version: int = Field(ge=1, le=1_000_000)
    external_ai_enabled: bool
    provider_governance_accepted: bool
    provider_governance_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    configuration_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class PublicLedgerAggregate(_FrozenModel):
    """Sanitized aggregate linkage; no per-attempt identity is representable."""

    ledger_digest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ordinal_binding_digest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ordinal_binding_count: int = Field(ge=0, le=_MAX_LEDGER_ROWS)
    unique_compatible_matching_count: Literal[1] = 1
    reservation_count: int = Field(ge=0, le=_MAX_LEDGER_ROWS)
    audit_count: int = Field(ge=0, le=_MAX_LEDGER_ROWS)
    interpretation_succeeded_count: int = Field(ge=0, le=_MAX_LEDGER_ROWS)
    expansion_count: int = Field(ge=0, le=_MAX_LEDGER_ROWS)
    settled_count: int = Field(ge=0, le=_MAX_LEDGER_ROWS)
    succeeded_count: int = Field(ge=0, le=_MAX_LEDGER_ROWS)
    mismatch_count: int = Field(ge=0, le=_MAX_LEDGER_ROWS)
    missing_audit_count: int = Field(ge=0, le=_MAX_LEDGER_ROWS)
    orphan_audit_count: int = Field(ge=0, le=_MAX_LEDGER_ROWS)
    reserved_count: int = Field(ge=0, le=_MAX_LEDGER_ROWS)
    open_attempt_count: int = Field(ge=0, le=1_000)
    stale_reserved_count: int = Field(ge=0, le=1_000)
    disabled_policy_attempt_count: int = Field(ge=0, le=_MAX_LEDGER_ROWS)
    input_tokens: int = Field(ge=0, le=250_000)
    output_reasoning_tokens: int = Field(ge=0, le=40_000)
    evaluator_adapter_duration_ms: int = Field(ge=0, le=3_600_000)
    provider_boundary_duration_ms: int = Field(ge=0, le=3_600_000)


class CampaignLedgerAttestation(_FrozenModel):
    """Complete bounded public payload."""

    schema_version: Literal[2] = 2
    campaign: PublicCampaignLink
    qualification_policy: PublicPolicyFacts
    disabled_policy: PublicPolicyFacts
    ledger: PublicLedgerAggregate

    @model_validator(mode="after")
    def public_facts_must_match_the_reviewed_campaign(
        self,
    ) -> CampaignLedgerAttestation:
        if (
            self.campaign.whole_file_sha256 != CANONICAL_CAMPAIGN_FILE_SHA256
            or self.campaign.logical_report_sha256 != CANONICAL_CAMPAIGN_REPORT_SHA256
            or self.campaign.plan_version != CANONICAL_CAMPAIGN_PLAN_VERSION
            or self.campaign.provider_free_preflight_pipeline_sha256
            != CANONICAL_PREFLIGHT_PIPELINE_SHA256
            or self.campaign.selected_model != CANONICAL_MODEL_SNAPSHOT
            or self.campaign.ordered_execution_witness_digest_sha256
            != CANONICAL_CAMPAIGN_ORDERED_EXECUTION_WITNESS_SHA256
            or self.qualification_policy.version != QUALIFICATION_POLICY_VERSION
            or not self.qualification_policy.external_ai_enabled
            or self.disabled_policy.version != DISABLED_POLICY_VERSION
            or self.disabled_policy.external_ai_enabled
            or not self.qualification_policy.provider_governance_accepted
            or not self.disabled_policy.provider_governance_accepted
            or self.qualification_policy.provider_governance_fingerprint
            != CANONICAL_GOVERNANCE_FINGERPRINT
            or self.disabled_policy.provider_governance_fingerprint
            != CANONICAL_GOVERNANCE_FINGERPRINT
            or self.qualification_policy.configuration_fingerprint
            != CANONICAL_CONFIGURATION_FINGERPRINT
            or self.disabled_policy.configuration_fingerprint != CANONICAL_CONFIGURATION_FINGERPRINT
        ):
            raise ValueError("public attestation policy/campaign facts are inconsistent")
        ledger = self.ledger
        if (
            ledger.ordinal_binding_count != CANONICAL_PROVIDER_ATTEMPTS
            or ledger.reservation_count != CANONICAL_PROVIDER_ATTEMPTS
            or ledger.audit_count != CANONICAL_PROVIDER_ATTEMPTS
            or ledger.interpretation_succeeded_count != CANONICAL_PROVIDER_ATTEMPTS
            or ledger.expansion_count != 0
            or ledger.settled_count != CANONICAL_PROVIDER_ATTEMPTS
            or ledger.succeeded_count != CANONICAL_PROVIDER_ATTEMPTS
            or ledger.mismatch_count != 0
            or ledger.missing_audit_count != 0
            or ledger.orphan_audit_count != 0
            or ledger.reserved_count != 0
            or ledger.open_attempt_count != 0
            or ledger.stale_reserved_count != 0
            or ledger.disabled_policy_attempt_count != 0
            or ledger.input_tokens != CANONICAL_INPUT_TOKENS
            or ledger.output_reasoning_tokens != CANONICAL_OUTPUT_REASONING_TOKENS
            or ledger.evaluator_adapter_duration_ms != CANONICAL_EVALUATOR_ADAPTER_DURATION_MS
            or ledger.provider_boundary_duration_ms != CANONICAL_PROVIDER_BOUNDARY_DURATION_MS
        ):
            raise ValueError("public attestation ledger aggregates are inconsistent")
        return self


class CampaignLedgerAttestationEnvelope(_FrozenModel):
    """HMAC envelope for one public attestation."""

    schema_version: Literal[2] = 2
    signature_domain: Literal["schemabridge:m27:query-studio:campaign-ledger-attestation:v2"] = (
        _ATTESTATION_DOMAIN
    )
    signature_key_version: str = Field(pattern=r"^v[1-9][0-9]{0,5}$")
    attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attestation: CampaignLedgerAttestation
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="before")
    @classmethod
    def digest_must_match_raw_attestation(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        raw_attestation = value.get("attestation")
        claimed = value.get("attestation_sha256")
        if isinstance(raw_attestation, CampaignLedgerAttestation):
            payload: object = raw_attestation.model_dump(mode="json")
        elif isinstance(raw_attestation, Mapping):
            payload = raw_attestation
        else:
            raise ValueError("attestation payload is invalid")
        if not isinstance(claimed, str) or claimed != _fingerprint(payload):
            raise ValueError("attestation digest is inconsistent")
        return value

    def json_bytes(self) -> bytes:
        return (
            json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode()


def load_canonical_campaign_evidence(
    repository_root: Path,
    campaign_path: Path,
    *,
    signing_key: bytes,
    signing_key_version: str,
) -> CampaignEvidenceFacts:
    """Verify and reduce exactly the reviewed terminal campaign."""

    try:
        root = repository_root.resolve()
        expected = root / CANONICAL_CAMPAIGN_PATH
        candidate = campaign_path if campaign_path.is_absolute() else root / campaign_path
        reject_symlink_ancestors(root, expected)
        reject_symlink_ancestors(root, candidate)
        if candidate.is_symlink() or candidate.resolve() != expected.resolve():
            raise ValueError("campaign path is not canonical")
        if (
            not expected.is_file()
            or expected.is_symlink()
            or expected.parent.is_symlink()
            or not expected.resolve().is_relative_to(root)
        ):
            raise ValueError("canonical campaign is unavailable")
        raw = read_bounded_regular_file(
            expected,
            maximum_bytes=_MAX_CAMPAIGN_BYTES,
        )
        if hashlib.sha256(raw).hexdigest() != CANONICAL_CAMPAIGN_FILE_SHA256:
            raise ValueError("canonical campaign content address is invalid")
        payload = json.loads(
            raw.decode(),
            object_pairs_hook=_reject_duplicate_json_pairs,
        )
        if not isinstance(payload, Mapping):
            raise ValueError("canonical campaign envelope is invalid")
        logical_report_sha256 = payload.get("report_sha256")
        if logical_report_sha256 != CANONICAL_CAMPAIGN_REPORT_SHA256:
            raise ValueError("canonical campaign logical digest is invalid")
        report = load_query_studio_qualification_campaign_report(
            root,
            expected,
            signing_key=_validated_signing_key(signing_key, signing_key_version),
            signing_key_version=signing_key_version,
            history_directory=root / CANONICAL_LIVE_HISTORY_DIRECTORY,
        )
        preflight = report.provider_free_preflight
        if (
            preflight is None
            or not preflight.passed
            or preflight.provider_calls_performed != 0
            or preflight.planned_case_count != 15
            or len(report.qualification_reports) != 1
            or len(report.full_evaluations) != 1
        ):
            raise ValueError("canonical campaign topology is invalid")
        qualification = report.qualification_reports[0]
        full = report.full_evaluations[0]
        if (
            qualification.policy_version != QUALIFICATION_POLICY_VERSION
            or qualification.configuration_fingerprint != CANONICAL_CONFIGURATION_FINGERPRINT
            or qualification.evaluation_outcome != "qualified"
            or full.policy_version != QUALIFICATION_POLICY_VERSION
            or full.report.configuration_fingerprint != CANONICAL_CONFIGURATION_FINGERPRINT
            or full.report.evaluation_outcome != "passed"
            or full.report.usage.provider_attempts != 15
        ):
            raise ValueError("canonical campaign qualification evidence is invalid")
        raw_attempts = (
            (
                "qualification",
                qualification.model_snapshot,
                qualification.configuration_fingerprint,
                qualification.case_outcomes,
            ),
            (
                "full",
                full.model_snapshot,
                full.report.configuration_fingerprint,
                full.report.case_outcomes,
            ),
        )
        attempts: list[CampaignAttemptWitness] = []
        for source, model_snapshot, configuration_fingerprint, outcomes in raw_attempts:
            for outcome in outcomes:
                if outcome.provider_attempts == 0:
                    continue
                if outcome.provider_attempts != 1 or not outcome.passed:
                    raise ValueError("campaign provider attempt matrix is not exact")
                attempts.append(
                    CampaignAttemptWitness(
                        sequence=len(attempts) + 1,
                        case_identity_sha256=_attempt_identity(
                            source=source,
                            case_id=outcome.case_id,
                            repetition=outcome.repetition,
                            suite=outcome.suite,
                        ),
                        stage="interpretation",
                        model_snapshot=model_snapshot,
                        configuration_fingerprint=configuration_fingerprint,
                        input_tokens=outcome.input_tokens,
                        output_reasoning_tokens=outcome.output_reasoning_tokens,
                        evaluator_adapter_duration_ms=outcome.duration_ms,
                    )
                )
        ordered_execution_witness_digest = _ordered_execution_witness_digest(tuple(attempts))
        return CampaignEvidenceFacts(
            whole_file_sha256=CANONICAL_CAMPAIGN_FILE_SHA256,
            logical_report_sha256=str(logical_report_sha256),
            plan_version=report.plan_version,
            provider_free_preflight_pipeline_sha256=preflight.pipeline_fingerprint,
            evaluation_outcome=report.evaluation_outcome,
            selected_model=report.selected_model,
            policy_version=qualification.policy_version,
            configuration_fingerprint=qualification.configuration_fingerprint,
            provider_attempts=report.usage.provider_attempts,
            input_tokens=report.usage.input_tokens,
            output_reasoning_tokens=report.usage.output_reasoning_tokens,
            evaluator_adapter_duration_ms=report.usage.duration_ms,
            attempts=tuple(attempts),
            ordered_execution_witness_digest_sha256=(ordered_execution_witness_digest),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise CampaignLedgerAttestationError(
            "canonical M27 campaign evidence is invalid"
        ) from error


def read_campaign_ledger_snapshot(
    migrator_dsn: str,
    workspace_id: str,
) -> CampaignLedgerSnapshot:
    """Read one bounded, transactionally consistent policy/ledger snapshot."""

    if not isinstance(migrator_dsn, str) or not migrator_dsn.strip():
        raise CampaignLedgerAttestationError("ledger reader configuration is invalid")
    if not isinstance(workspace_id, str) or not 1 <= len(workspace_id) <= 200:
        raise CampaignLedgerAttestationError("ledger reader scope is invalid")
    try:
        with psycopg.connect(
            migrator_dsn,
            connect_timeout=5,
            application_name="schemabridge-m27-ledger-attestation",
        ) as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            connection.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (f"{_STATEMENT_TIMEOUT_MS}ms",),
            )
            transaction_mode = connection.execute(
                """
                SELECT
                    current_setting('transaction_read_only'),
                    current_setting('transaction_isolation')
                """
            ).fetchone()
            if transaction_mode != ("on", "repeatable read"):
                raise ValueError("ledger transaction mode is not exact")
            revisions = connection.execute(
                _POLICY_REVISIONS_SQL,
                (
                    workspace_id,
                    QUALIFICATION_POLICY_VERSION,
                    DISABLED_POLICY_VERSION,
                ),
            ).fetchall()
            current = connection.execute(
                _CURRENT_POLICY_SQL,
                (workspace_id,),
            ).fetchone()
            rows = connection.execute(
                _LEDGER_ROWS_SQL,
                (
                    workspace_id,
                    QUALIFICATION_POLICY_VERSION,
                    DISABLED_POLICY_VERSION,
                ),
            ).fetchall()
            linked_counts = connection.execute(
                _LINKED_AUDIT_COUNTS_SQL,
                (
                    QUALIFICATION_POLICY_VERSION,
                    DISABLED_POLICY_VERSION,
                    workspace_id,
                ),
            ).fetchone()
            orphan_audit = connection.execute(
                _ORPHAN_AUDIT_COUNT_SQL,
                (workspace_id, workspace_id),
            ).fetchone()
            activity = connection.execute(
                _ACTIVITY_SQL,
                (QUALIFICATION_POLICY_VERSION, workspace_id),
            ).fetchone()
        if (
            len(revisions) != 2
            or current is None
            or linked_counts is None
            or orphan_audit is None
            or activity is None
        ):
            raise ValueError("ledger snapshot is incomplete")
        policies = {
            int(row[1]): AiPolicyRevisionFacts.model_validate(
                dict(zip(_POLICY_COLUMNS, row, strict=True))
            )
            for row in revisions
        }
        if set(policies) != {
            QUALIFICATION_POLICY_VERSION,
            DISABLED_POLICY_VERSION,
        }:
            raise ValueError("policy revisions are not exact")
        current_policy = AiPolicyRevisionFacts.model_validate(
            dict(zip(_POLICY_COLUMNS, current, strict=True))
        )
        ledger_rows: list[LedgerReservationAuditFacts] = []
        for row in rows:
            payload = dict(zip(_LEDGER_COLUMNS, row, strict=True))
            if payload["audit_id"] is None:
                raise ValueError("ledger reservation lacks its audit")
            ledger_rows.append(LedgerReservationAuditFacts.model_validate(payload))
        ledger_rows.sort(
            key=lambda item: (
                item.lease_acquired_at,
                item.created_at,
                item.reservation_id,
                item.audit_id,
            )
        )
        return CampaignLedgerSnapshot(
            qualification_policy=policies[QUALIFICATION_POLICY_VERSION],
            disabled_policy=policies[DISABLED_POLICY_VERSION],
            current_policy=current_policy,
            ledger_rows=tuple(ledger_rows),
            qualification_linked_audit_count=int(linked_counts[0]),
            disabled_policy_linked_audit_count=int(linked_counts[1]),
            orphan_audit_count=int(orphan_audit[0]),
            active_attempt_count=int(activity[0]),
            stale_reserved_count=int(activity[1]),
        )
    except CampaignLedgerAttestationError:
        raise
    except (psycopg.Error, TypeError, ValueError) as error:
        raise CampaignLedgerAttestationError(
            "M27 campaign ledger snapshot is unavailable"
        ) from error


def build_campaign_ledger_attestation(
    campaign: CampaignEvidenceFacts,
    snapshot: CampaignLedgerSnapshot,
    *,
    allow_later_disabled_current_policy: bool = False,
) -> CampaignLedgerAttestation:
    """Recompute the exact public report from verified campaign and DB facts."""

    try:
        campaign = CampaignEvidenceFacts.model_validate(campaign.model_dump(mode="python"))
        snapshot = CampaignLedgerSnapshot.model_validate(snapshot.model_dump(mode="python"))
        _validate_policy_pair(
            snapshot,
            allow_later_disabled_current_policy=(allow_later_disabled_current_policy),
        )
        qualification_rows = tuple(
            item
            for item in snapshot.ledger_rows
            if item.policy_version == QUALIFICATION_POLICY_VERSION
        )
        disabled_rows = tuple(
            item for item in snapshot.ledger_rows if item.policy_version == DISABLED_POLICY_VERSION
        )
        missing_audit_count = len(qualification_rows) - snapshot.qualification_linked_audit_count
        if (
            len(qualification_rows) != CANONICAL_PROVIDER_ATTEMPTS
            or disabled_rows
            or snapshot.qualification_linked_audit_count != len(qualification_rows)
            or snapshot.disabled_policy_linked_audit_count != 0
            or snapshot.orphan_audit_count != 0
        ):
            raise ValueError("ledger row closure is not exact")
        _validate_temporal_closure(snapshot, qualification_rows)
        if any(
            item.workspace_id != snapshot.qualification_policy.workspace_id
            or item.stage != "interpretation"
            or item.attempt_number != 1
            or item.status != "settled"
            or item.outcome_code != "succeeded"
            or item.configuration_fingerprint != CANONICAL_CONFIGURATION_FINGERPRINT
            or item.model_snapshot != CANONICAL_MODEL_SNAPSHOT
            or item.endpoint_region != "global"
            for item in qualification_rows
        ):
            raise ValueError("ledger settlement facts differ from the campaign")
        if (
            len({item.request_id for item in qualification_rows}) != CANONICAL_PROVIDER_ATTEMPTS
            or len({item.actor_digest for item in qualification_rows}) != 1
            or {item.semantic_scope_fingerprint for item in qualification_rows}
            != {CANONICAL_SEMANTIC_SCOPE_FINGERPRINT}
            or len({item.idempotency_digest for item in qualification_rows})
            != CANONICAL_PROVIDER_ATTEMPTS
        ):
            raise ValueError("ledger admission witnesses are not exact")
        ordinal_binding_digest, unique_matching_count = _ordinal_binding_digest(
            campaign,
            qualification_rows,
        )
        input_tokens = sum(item.audit_input_tokens for item in qualification_rows)
        output_tokens = sum(item.audit_output_tokens for item in qualification_rows)
        provider_duration = sum(item.audit_duration_ms for item in qualification_rows)
        reserved_count = sum(item.status == "reserved" for item in qualification_rows)
        if (
            input_tokens != campaign.input_tokens
            or output_tokens != campaign.output_reasoning_tokens
            or provider_duration != CANONICAL_PROVIDER_BOUNDARY_DURATION_MS
            or snapshot.active_attempt_count != 0
            or snapshot.stale_reserved_count != 0
            or reserved_count != 0
        ):
            raise ValueError("ledger aggregates differ from the terminal campaign")
        return CampaignLedgerAttestation(
            campaign=PublicCampaignLink(
                whole_file_sha256=campaign.whole_file_sha256,
                logical_report_sha256=campaign.logical_report_sha256,
                plan_version=campaign.plan_version,
                provider_free_preflight_pipeline_sha256=(
                    campaign.provider_free_preflight_pipeline_sha256
                ),
                evaluation_outcome=campaign.evaluation_outcome,
                selected_model=campaign.selected_model,
                ordered_execution_witness_digest_sha256=(
                    campaign.ordered_execution_witness_digest_sha256
                ),
            ),
            qualification_policy=_public_policy(snapshot.qualification_policy),
            disabled_policy=_public_policy(snapshot.disabled_policy),
            ledger=PublicLedgerAggregate(
                ledger_digest_sha256=_ledger_digest(snapshot),
                ordinal_binding_digest_sha256=ordinal_binding_digest,
                ordinal_binding_count=len(qualification_rows),
                unique_compatible_matching_count=unique_matching_count,
                reservation_count=len(qualification_rows),
                audit_count=snapshot.qualification_linked_audit_count,
                interpretation_succeeded_count=sum(
                    item.stage == "interpretation" and item.outcome_code == "succeeded"
                    for item in qualification_rows
                ),
                expansion_count=sum(item.stage == "expansion" for item in qualification_rows),
                settled_count=sum(item.status == "settled" for item in qualification_rows),
                succeeded_count=sum(
                    item.outcome_code == "succeeded" for item in qualification_rows
                ),
                mismatch_count=0,
                missing_audit_count=missing_audit_count,
                orphan_audit_count=snapshot.orphan_audit_count,
                reserved_count=reserved_count,
                open_attempt_count=snapshot.active_attempt_count,
                stale_reserved_count=snapshot.stale_reserved_count,
                disabled_policy_attempt_count=len(disabled_rows),
                input_tokens=input_tokens,
                output_reasoning_tokens=output_tokens,
                evaluator_adapter_duration_ms=(campaign.evaluator_adapter_duration_ms),
                provider_boundary_duration_ms=provider_duration,
            ),
        )
    except (TypeError, ValueError) as error:
        raise CampaignLedgerAttestationError(
            "M27 campaign and ledger facts do not attest"
        ) from error


def write_campaign_ledger_attestation(
    attestation: CampaignLedgerAttestation,
    repository_root: Path,
    *,
    signing_key: bytes,
    signing_key_version: str,
) -> Path:
    """Write or exactly reopen one immutable content-addressed envelope."""

    try:
        root = repository_root.resolve()
        history = _canonical_history(root, create=True)
        envelope = _signed_envelope(
            attestation,
            signing_key=signing_key,
            signing_key_version=signing_key_version,
        )
        payload = envelope.json_bytes()
        content_sha256 = hashlib.sha256(payload).hexdigest()
        path = history / f"campaign-ledger-attestation-{content_sha256}.json"
        _write_immutable(path, payload)
        return path
    except (OSError, TypeError, ValueError) as error:
        raise CampaignLedgerAttestationError(
            "M27 campaign ledger attestation could not be retained"
        ) from error


def load_campaign_ledger_attestation(
    repository_root: Path,
    attestation_path: Path,
    *,
    signing_key: bytes,
    signing_key_version: str,
) -> CampaignLedgerAttestation:
    """Authenticate one canonical content-addressed attestation."""

    try:
        root = repository_root.resolve()
        history = _canonical_history(root, create=False)
        candidate = attestation_path if attestation_path.is_absolute() else root / attestation_path
        reject_symlink_ancestors(root, candidate)
        if candidate.is_symlink():
            raise ValueError("attestation cannot be a symlink")
        resolved = candidate.resolve()
        if resolved.parent != history or not resolved.is_file() or resolved.is_symlink():
            raise ValueError("attestation path is outside canonical history")
        matched = _ATTESTATION_FILE.fullmatch(resolved.name)
        if matched is None:
            raise ValueError("attestation filename is not content addressed")
        raw = read_bounded_regular_file(
            resolved,
            maximum_bytes=_MAX_ATTESTATION_BYTES,
        )
        if not hmac.compare_digest(
            hashlib.sha256(raw).hexdigest(),
            matched.group(1),
        ):
            raise ValueError("attestation content address is invalid")
        payload = json.loads(
            raw.decode(),
            object_pairs_hook=_reject_duplicate_json_pairs,
        )
        envelope = CampaignLedgerAttestationEnvelope.model_validate(payload)
        key = _validated_signing_key(signing_key, signing_key_version)
        if envelope.signature_key_version != signing_key_version:
            raise ValueError("attestation key version is unavailable")
        expected_signature = _signature(
            envelope.attestation_sha256,
            signing_key=key,
            signing_key_version=signing_key_version,
        )
        if not hmac.compare_digest(envelope.signature, expected_signature):
            raise ValueError("attestation signature is invalid")
        return envelope.attestation
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as error:
        raise CampaignLedgerAttestationError(
            "M27 campaign ledger attestation is invalid"
        ) from error


def verify_campaign_ledger_attestation(
    persisted: CampaignLedgerAttestation,
    campaign: CampaignEvidenceFacts,
    snapshot: CampaignLedgerSnapshot,
) -> CampaignLedgerAttestation:
    """Recompute against a fresh read-only DB snapshot and require exact equality."""

    recomputed = build_campaign_ledger_attestation(
        campaign,
        snapshot,
        allow_later_disabled_current_policy=True,
    )
    if persisted != recomputed:
        raise CampaignLedgerAttestationError(
            "M27 campaign ledger attestation no longer matches its sources"
        )
    return recomputed


def _validate_policy_pair(
    snapshot: CampaignLedgerSnapshot,
    *,
    allow_later_disabled_current_policy: bool,
) -> None:
    qualification = snapshot.qualification_policy
    disabled = snapshot.disabled_policy
    current = snapshot.current_policy
    reviewed_pair_exact = (
        qualification.workspace_id == disabled.workspace_id
        and qualification.model_snapshot == disabled.model_snapshot == CANONICAL_MODEL_SNAPSHOT
        and qualification.endpoint_region == disabled.endpoint_region == "global"
        and qualification.endpoint_origin_fingerprint
        == disabled.endpoint_origin_fingerprint
        == CANONICAL_ENDPOINT_ORIGIN_FINGERPRINT
        and qualification.configuration_fingerprint
        == disabled.configuration_fingerprint
        == CANONICAL_CONFIGURATION_FINGERPRINT
        and qualification.provider_governance_fingerprint
        == disabled.provider_governance_fingerprint
        == CANONICAL_GOVERNANCE_FINGERPRINT
        and qualification.provider_governance_accepted
        and disabled.provider_governance_accepted
        and _policy_caps_are_exact(qualification)
        and _policy_caps_are_exact(disabled)
    )
    if (
        not reviewed_pair_exact
        or qualification.version != QUALIFICATION_POLICY_VERSION
        or not qualification.external_ai_enabled
        or disabled.version != DISABLED_POLICY_VERSION
        or disabled.external_ai_enabled
    ):
        raise ValueError("qualification/disabled policy closure is invalid")
    if current == disabled:
        return
    if (
        not allow_later_disabled_current_policy
        or current.version <= DISABLED_POLICY_VERSION
        or current.workspace_id != disabled.workspace_id
        or current.external_ai_enabled
        or not current.provider_governance_accepted
        or current.provider_governance_fingerprint is None
        or _SHA256.fullmatch(current.provider_governance_fingerprint) is None
        or current.provider_governance_accepted_at is None
        or current.provider_governance_accepted_at > current.updated_at
        or current.updated_at <= disabled.updated_at
        or current.model_snapshot != CANONICAL_MODEL_SNAPSHOT
        or current.endpoint_region != "global"
        or current.endpoint_origin_fingerprint != CANONICAL_ENDPOINT_ORIGIN_FINGERPRINT
        or current.configuration_fingerprint != CANONICAL_CONFIGURATION_FINGERPRINT
        or not _policy_caps_are_exact(current)
    ):
        raise ValueError("current disabled policy closure is invalid")


def _policy_caps_are_exact(policy: AiPolicyRevisionFacts) -> bool:
    return (
        policy.requests_per_minute == CANONICAL_REQUESTS_PER_MINUTE
        and policy.daily_input_token_limit == CANONICAL_DAILY_INPUT_TOKEN_LIMIT
        and policy.daily_output_token_limit == CANONICAL_DAILY_OUTPUT_TOKEN_LIMIT
        and policy.concurrent_attempt_limit == CANONICAL_CONCURRENT_ATTEMPT_LIMIT
        and policy.reservation_lease_seconds == CANONICAL_RESERVATION_LEASE_SECONDS
        and policy.audit_retention_seconds == CANONICAL_AUDIT_RETENTION_SECONDS
    )


def _public_policy(policy: AiPolicyRevisionFacts) -> PublicPolicyFacts:
    governance_fingerprint = policy.provider_governance_fingerprint
    if governance_fingerprint is None:
        raise ValueError("policy governance fingerprint is unavailable")
    return PublicPolicyFacts(
        version=policy.version,
        external_ai_enabled=policy.external_ai_enabled,
        provider_governance_accepted=policy.provider_governance_accepted,
        provider_governance_fingerprint=governance_fingerprint,
        configuration_fingerprint=policy.configuration_fingerprint,
    )


def _validate_temporal_closure(
    snapshot: CampaignLedgerSnapshot,
    rows: tuple[LedgerReservationAuditFacts, ...],
) -> None:
    if not rows:
        raise ValueError("ledger chronology is unavailable")
    qualification = snapshot.qualification_policy
    disabled = snapshot.disabled_policy
    qualification_accepted_at = qualification.provider_governance_accepted_at
    disabled_accepted_at = disabled.provider_governance_accepted_at
    first_created_at = rows[0].created_at
    last_terminal_at = max(
        timestamp
        for row in rows
        for timestamp in (row.settled_at, row.audit_occurred_at)
        if timestamp is not None
    )
    if (
        qualification_accepted_at is None
        or disabled_accepted_at is None
        or qualification_accepted_at != qualification.updated_at
        or disabled_accepted_at != qualification_accepted_at
        or qualification.updated_at >= first_created_at
        or disabled.updated_at <= last_terminal_at
        or disabled.updated_at - last_terminal_at
        != timedelta(microseconds=CANONICAL_DISABLE_AFTER_LAST_SETTLEMENT_MICROSECONDS)
    ):
        raise ValueError("policy/ledger chronology is not the reviewed closure")


def _attempt_identity(
    *,
    source: str,
    case_id: str,
    repetition: int,
    suite: str,
) -> str:
    if source not in {"qualification", "full"}:
        raise ValueError("campaign attempt source is invalid")
    return _fingerprint(
        {
            "case_id": case_id,
            "domain": _ATTEMPT_IDENTITY_DOMAIN,
            "repetition": repetition,
            "source": source,
            "suite": suite,
        }
    )


def _ordered_execution_witness_digest(
    attempts: tuple[CampaignAttemptWitness, ...],
) -> str:
    return _fingerprint(
        {
            "attempts": [item.model_dump(mode="json") for item in attempts],
            "domain": _ORDERED_EXECUTION_WITNESS_DOMAIN,
            "schema_version": 2,
        }
    )


def _ordinal_binding_digest(
    campaign: CampaignEvidenceFacts,
    rows: tuple[LedgerReservationAuditFacts, ...],
) -> tuple[str, Literal[1]]:
    """Bind two complete ordered lists under the reviewed sequential harness."""

    if len(campaign.attempts) != len(rows):
        raise ValueError("campaign/ledger attempt cardinality differs")
    if len({row.request_fingerprint for row in rows}) != len(rows) or len(
        {row.semantic_payload_fingerprint for row in rows}
    ) != len(rows):
        raise ValueError("ledger execution witnesses are not bijective")
    matching_count, unique_matching = _compatible_matching_count(
        campaign.attempts,
        rows,
    )
    if matching_count != 1 or unique_matching != tuple(range(len(campaign.attempts))):
        raise ValueError("campaign/ledger compatible matching is not uniquely ordinal")
    bindings: list[dict[str, object]] = []
    duration_delta_ms = 0
    for witness, row in zip(campaign.attempts, rows, strict=True):
        if (
            witness.stage != row.stage
            or witness.model_snapshot != row.model_snapshot
            or witness.configuration_fingerprint != row.configuration_fingerprint
            or witness.input_tokens != row.audit_input_tokens
            or witness.output_reasoning_tokens != row.audit_output_tokens
            or witness.evaluator_adapter_duration_ms < row.audit_duration_ms
        ):
            raise ValueError("campaign/ledger attempt sequence differs")
        duration_delta_ms += witness.evaluator_adapter_duration_ms - row.audit_duration_ms
        bindings.append(
            {
                "campaign_attempt": witness.model_dump(mode="json"),
                "ledger_pair": row.model_dump(mode="json"),
            }
        )
    if (
        duration_delta_ms
        != CANONICAL_EVALUATOR_ADAPTER_DURATION_MS - CANONICAL_PROVIDER_BOUNDARY_DURATION_MS
    ):
        raise ValueError("campaign/ledger duration overhead is not exact")
    payload = {
        "bindings": bindings,
        "campaign_logical_report_sha256": campaign.logical_report_sha256,
        "campaign_ordered_execution_witness_digest_sha256": (
            campaign.ordered_execution_witness_digest_sha256
        ),
        "domain": _ORDINAL_BINDING_DOMAIN,
        "schema_version": 2,
        "unique_compatible_matching_count": matching_count,
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest(), 1


def _compatible_matching_count(
    attempts: tuple[CampaignAttemptWitness, ...],
    rows: tuple[LedgerReservationAuditFacts, ...],
) -> tuple[int, tuple[int, ...] | None]:
    """Count at most two complete compatibility matchings over the bounded 16x16 set."""

    if len(attempts) != CANONICAL_PROVIDER_ATTEMPTS or len(rows) != CANONICAL_PROVIDER_ATTEMPTS:
        return 0, None
    candidates = tuple(
        tuple(
            row_index
            for row_index, row in enumerate(rows)
            if attempt.stage == row.stage
            and attempt.model_snapshot == row.model_snapshot
            and attempt.configuration_fingerprint == row.configuration_fingerprint
            and attempt.input_tokens == row.audit_input_tokens
            and attempt.output_reasoning_tokens == row.audit_output_tokens
            and row.audit_duration_ms <= attempt.evaluator_adapter_duration_ms
        )
        for attempt in attempts
    )
    if any(not options for options in candidates):
        return 0, None
    matching_count = 0
    unique_matching: tuple[int, ...] | None = None
    selected = [-1] * len(attempts)

    def visit(attempt_index: int, used_rows: int) -> None:
        nonlocal matching_count, unique_matching
        if matching_count >= 2:
            return
        if attempt_index == len(attempts):
            matching_count += 1
            if matching_count == 1:
                unique_matching = tuple(selected)
            return
        for row_index in candidates[attempt_index]:
            row_bit = 1 << row_index
            if used_rows & row_bit:
                continue
            selected[attempt_index] = row_index
            visit(attempt_index + 1, used_rows | row_bit)
            if matching_count >= 2:
                return
        selected[attempt_index] = -1

    visit(0, 0)
    return matching_count, unique_matching


def _ledger_digest(snapshot: CampaignLedgerSnapshot) -> str:
    payload = {
        "active_attempt_count": snapshot.active_attempt_count,
        "disabled_policy": snapshot.disabled_policy.model_dump(mode="json"),
        "disabled_policy_linked_audit_count": (snapshot.disabled_policy_linked_audit_count),
        "domain": _LEDGER_DIGEST_DOMAIN,
        "ledger_rows": [item.model_dump(mode="json") for item in snapshot.ledger_rows],
        "qualification_linked_audit_count": (snapshot.qualification_linked_audit_count),
        "orphan_audit_count": snapshot.orphan_audit_count,
        "qualification_policy": snapshot.qualification_policy.model_dump(mode="json"),
        "schema_version": 2,
        "stale_reserved_count": snapshot.stale_reserved_count,
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _settlement_fingerprint(row: LedgerReservationAuditFacts) -> str:
    parts: tuple[str, ...]
    if row.outcome_code == "expired_crash":
        parts = (
            "ai_attempt_settlement_v1",
            row.reservation_id,
            str(row.fencing_token),
            row.outcome_code,
            str(row.estimated_input_tokens),
            str(row.estimated_output_tokens),
            str(row.duration_ms),
        )
    else:
        parts = (
            "ai_attempt_settlement_v1",
            row.reservation_id,
            str(row.fencing_token),
            str(row.outcome_code),
            ("unknown" if row.observed_input_tokens is None else str(row.observed_input_tokens)),
            ("unknown" if row.observed_output_tokens is None else str(row.observed_output_tokens)),
            str(row.charged_input_tokens),
            str(row.charged_output_tokens),
            str(row.duration_ms),
        )
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _signed_envelope(
    attestation: CampaignLedgerAttestation,
    *,
    signing_key: bytes,
    signing_key_version: str,
) -> CampaignLedgerAttestationEnvelope:
    key = _validated_signing_key(signing_key, signing_key_version)
    digest = _fingerprint(attestation.model_dump(mode="json"))
    return CampaignLedgerAttestationEnvelope(
        signature_key_version=signing_key_version,
        attestation_sha256=digest,
        attestation=attestation,
        signature=_signature(
            digest,
            signing_key=key,
            signing_key_version=signing_key_version,
        ),
    )


def _signature(
    attestation_sha256: str,
    *,
    signing_key: bytes,
    signing_key_version: str,
) -> str:
    derived = hmac.new(
        signing_key,
        _ATTESTATION_KEY_DOMAIN,
        hashlib.sha256,
    ).digest()
    payload = {
        "attestation_sha256": attestation_sha256,
        "schema_version": 2,
        "signature_domain": _ATTESTATION_DOMAIN,
        "signature_key_version": signing_key_version,
    }
    return hmac.new(
        derived,
        _canonical_json_bytes(payload),
        hashlib.sha256,
    ).hexdigest()


def _validated_signing_key(
    signing_key: bytes,
    signing_key_version: str,
) -> bytes:
    if (
        type(signing_key) is not bytes
        or len(signing_key) < 32
        or len(set(signing_key)) < 8
        or _KEY_VERSION.fullmatch(signing_key_version) is None
    ):
        raise ValueError("attestation signing material is invalid")
    return signing_key


def _canonical_history(root: Path, *, create: bool) -> Path:
    configured = root / CANONICAL_LIVE_HISTORY_DIRECTORY
    reject_symlink_ancestors(root, configured)
    if configured.is_symlink():
        raise ValueError("attestation history cannot be a symlink")
    resolved = configured.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("attestation history escapes the repository")
    if create:
        resolved.mkdir(parents=True, exist_ok=True)
    if not resolved.is_dir() or resolved.is_symlink():
        raise ValueError("attestation history is unavailable")
    return resolved


def _write_immutable(path: Path, payload: bytes) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(payload)
    except FileExistsError:
        try:
            existing = read_bounded_regular_file(
                path,
                maximum_bytes=len(payload),
            )
        except ValueError as error:
            raise ValueError("attestation content-address collision") from error
        if existing != payload:
            raise ValueError("attestation content-address collision") from None


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _reject_duplicate_json_pairs(
    pairs: Sequence[tuple[str, object]],
) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("attestation JSON contains duplicate keys")
        payload[key] = value
    return payload


_POLICY_COLUMNS: Final = (
    "workspace_id",
    "version",
    "external_ai_enabled",
    "provider_governance_accepted",
    "provider_governance_fingerprint",
    "provider_governance_accepted_at",
    "model_snapshot",
    "endpoint_region",
    "endpoint_origin_fingerprint",
    "configuration_fingerprint",
    "requests_per_minute",
    "daily_input_token_limit",
    "daily_output_token_limit",
    "concurrent_attempt_limit",
    "reservation_lease_seconds",
    "audit_retention_seconds",
    "updated_by",
    "updated_at",
)

_POLICY_SELECT: Final = """
    workspace_id,
    version,
    external_ai_enabled,
    provider_governance_accepted,
    provider_governance_fingerprint::text,
    provider_governance_accepted_at,
    model_snapshot,
    endpoint_region,
    endpoint_origin_fingerprint::text,
    configuration_fingerprint::text,
    requests_per_minute,
    daily_input_token_limit,
    daily_output_token_limit,
    concurrent_attempt_limit,
    reservation_lease_seconds,
    audit_retention_seconds,
    updated_by,
    updated_at
"""

_POLICY_REVISIONS_SQL: Final = f"""
    SELECT {_POLICY_SELECT}
    FROM schemabridge_control.tenant_ai_policy_revisions
    WHERE workspace_id = %s
      AND version IN (%s, %s)
    ORDER BY version
"""

_CURRENT_POLICY_SQL: Final = f"""
    SELECT {_POLICY_SELECT}
    FROM schemabridge_control.tenant_ai_policies
    WHERE workspace_id = %s
"""

_LEDGER_COLUMNS: Final = (
    "reservation_id",
    "audit_id",
    "audit_reservation_id",
    "workspace_id",
    "request_id",
    "actor_digest",
    "stage",
    "attempt_number",
    "idempotency_digest",
    "request_fingerprint",
    "semantic_scope_fingerprint",
    "semantic_payload_fingerprint",
    "configuration_fingerprint",
    "policy_version",
    "model_snapshot",
    "endpoint_region",
    "estimated_input_tokens",
    "estimated_output_tokens",
    "audit_retention_seconds",
    "status",
    "fencing_token",
    "lease_acquired_at",
    "lease_expires_at",
    "outcome_code",
    "observed_input_tokens",
    "observed_output_tokens",
    "charged_input_tokens",
    "charged_output_tokens",
    "duration_ms",
    "settlement_fingerprint",
    "created_at",
    "settled_at",
    "retain_until",
    "audit_workspace_scope_digest",
    "audit_actor_digest",
    "audit_request_id",
    "audit_stage",
    "audit_attempt_number",
    "audit_model_snapshot",
    "audit_endpoint_region",
    "audit_configuration_fingerprint",
    "audit_request_fingerprint",
    "audit_semantic_scope_fingerprint",
    "audit_semantic_payload_fingerprint",
    "audit_input_tokens",
    "audit_output_tokens",
    "audit_duration_ms",
    "audit_outcome_code",
    "audit_occurred_at",
    "audit_retain_until",
)

_LEDGER_ROWS_SQL: Final = """
    SELECT
        reservation.reservation_id,
        audit.audit_id,
        audit.reservation_id,
        reservation.workspace_id,
        reservation.request_id,
        reservation.actor_digest::text,
        reservation.stage,
        reservation.attempt_number,
        reservation.idempotency_digest::text,
        reservation.request_fingerprint::text,
        reservation.semantic_scope_fingerprint::text,
        reservation.semantic_payload_fingerprint::text,
        reservation.configuration_fingerprint::text,
        reservation.policy_version,
        reservation.model_snapshot,
        reservation.endpoint_region,
        reservation.estimated_input_tokens,
        reservation.estimated_output_tokens,
        reservation.audit_retention_seconds,
        reservation.status,
        reservation.fencing_token,
        reservation.lease_acquired_at,
        reservation.lease_expires_at,
        reservation.outcome_code,
        reservation.observed_input_tokens,
        reservation.observed_output_tokens,
        reservation.charged_input_tokens,
        reservation.charged_output_tokens,
        reservation.duration_ms,
        reservation.settlement_fingerprint::text,
        reservation.created_at,
        reservation.settled_at,
        reservation.retain_until,
        audit.workspace_scope_digest::text,
        audit.actor_digest::text,
        audit.request_id,
        audit.stage,
        audit.attempt_number,
        audit.model_snapshot,
        audit.endpoint_region,
        audit.configuration_fingerprint::text,
        audit.request_fingerprint::text,
        audit.semantic_scope_fingerprint::text,
        audit.semantic_payload_fingerprint::text,
        audit.input_tokens,
        audit.output_tokens,
        audit.duration_ms,
        audit.outcome_code,
        audit.occurred_at,
        audit.retain_until
    FROM schemabridge_control.ai_provider_attempt_reservations AS reservation
    LEFT JOIN schemabridge_control.ai_provider_usage_audit AS audit
      ON audit.reservation_id = reservation.reservation_id
    WHERE reservation.workspace_id = %s
      AND reservation.policy_version IN (%s, %s)
    ORDER BY
        reservation.lease_acquired_at,
        reservation.created_at,
        reservation.reservation_id,
        audit.audit_id
    FETCH FIRST 181 ROWS ONLY
"""

_LINKED_AUDIT_COUNTS_SQL: Final = """
    SELECT
        count(*) FILTER (
            WHERE reservation.policy_version = %s
        ),
        count(*) FILTER (
            WHERE reservation.policy_version = %s
        )
    FROM schemabridge_control.ai_provider_usage_audit AS audit
    JOIN schemabridge_control.ai_provider_attempt_reservations AS reservation
      ON reservation.reservation_id = audit.reservation_id
    WHERE reservation.workspace_id = %s
"""

_ORPHAN_AUDIT_COUNT_SQL: Final = """
    SELECT count(*)
    FROM schemabridge_control.ai_provider_usage_audit AS audit
    LEFT JOIN schemabridge_control.ai_provider_attempt_reservations AS reservation
      ON reservation.reservation_id = audit.reservation_id
    WHERE audit.workspace_scope_digest = encode(
        sha256(
            convert_to(
                'query_studio_scope_v1|' || %s,
                'UTF8'
            )
        ),
        'hex'
    )::char(64)
      AND (
            reservation.reservation_id IS NULL
            OR reservation.workspace_id <> %s
      )
"""

_ACTIVITY_SQL: Final = """
    SELECT
        admission.active_attempt_count,
        count(reservation.reservation_id) FILTER (
            WHERE reservation.status = 'reserved'
              AND reservation.lease_expires_at <= transaction_timestamp()
        )
    FROM schemabridge_control.ai_provider_admission_state AS admission
    LEFT JOIN schemabridge_control.ai_provider_attempt_reservations AS reservation
      ON reservation.workspace_id = admission.workspace_id
     AND reservation.policy_version = %s
    WHERE admission.workspace_id = %s
    GROUP BY admission.active_attempt_count
"""


__all__ = [
    "CANONICAL_CAMPAIGN_FILE_SHA256",
    "CANONICAL_CAMPAIGN_ORDERED_EXECUTION_WITNESS_SHA256",
    "CANONICAL_CAMPAIGN_PATH",
    "CANONICAL_CAMPAIGN_REPORT_SHA256",
    "CANONICAL_CONFIGURATION_FINGERPRINT",
    "CANONICAL_EVALUATOR_ADAPTER_DURATION_MS",
    "CANONICAL_LIVE_HISTORY_DIRECTORY",
    "CANONICAL_PROVIDER_BOUNDARY_DURATION_MS",
    "CANONICAL_SEMANTIC_SCOPE_FINGERPRINT",
    "AiPolicyRevisionFacts",
    "CampaignAttemptWitness",
    "CampaignEvidenceFacts",
    "CampaignLedgerAttestation",
    "CampaignLedgerAttestationError",
    "CampaignLedgerSnapshot",
    "LedgerReservationAuditFacts",
    "build_campaign_ledger_attestation",
    "load_campaign_ledger_attestation",
    "load_canonical_campaign_evidence",
    "read_campaign_ledger_snapshot",
    "verify_campaign_ledger_attestation",
    "write_campaign_ledger_attestation",
]
