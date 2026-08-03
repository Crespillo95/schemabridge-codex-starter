"""Pure lifecycle and fencing contracts for durable registry-publication jobs."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TypeAlias

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.registry_changes import PreparedRegistryJoinProposal
from schemabridge.domain.registry_model_changes import (
    PreparedRegistryModelReplacementProposal,
)
from schemabridge.domain.registry_publication import (
    PublicationReadbackReceipt,
    PublishableRegistryVersion,
    RegistryPublicationAuthorization,
    observed_registry_related_asset_urns,
    validate_registry_publication_authorization,
)
from schemabridge.domain.semantic_onboarding import PreparedSemanticOnboardingProposal
from schemabridge.domain.semantic_registry import SemanticRegistryScope

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,199}$")
_MAX_LEASE = timedelta(minutes=5)
_MAX_RETRY_DELAY_SECONDS = 300


# The historical M33 proposal intentionally remains the first member and keeps
# its original, discriminator-free serialized shape.  New families must use a
# distinct validated model so extra fields cannot be silently discarded.
PreparedRegistryPublicationProposal: TypeAlias = (
    PreparedSemanticOnboardingProposal
    | PreparedRegistryJoinProposal
    | PreparedRegistryModelReplacementProposal
)


class RegistryPublicationJobStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    RETRY_WAIT = "retry_wait"
    CANCEL_REQUESTED = "cancel_requested"
    ACTIVATION_READY = "activation_ready"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD_LETTERED = "dead_lettered"

    @property
    def is_terminal(self) -> bool:
        return self in {
            RegistryPublicationJobStatus.ACTIVATION_READY,
            RegistryPublicationJobStatus.FAILED,
            RegistryPublicationJobStatus.CANCELLED,
            RegistryPublicationJobStatus.DEAD_LETTERED,
        }


class RegistryPublicationJobPhase(StrEnum):
    PREPARE = "prepare"
    PUBLISH = "publish"


class RegistryPublicationFailureCode(StrEnum):
    PROPOSAL_UNAVAILABLE = "proposal_unavailable"
    PROPOSAL_STALE = "proposal_stale"
    CATALOG_STALE = "catalog_stale"
    BASE_UNAVAILABLE = "base_unavailable"
    BASE_STALE = "base_stale"
    CANDIDATE_INVALID = "candidate_invalid"
    AUTHORIZATION_EXPIRED = "authorization_expired"
    AUTHORIZATION_MISMATCH = "authorization_mismatch"
    DATAHUB_UNAVAILABLE = "datahub_unavailable"
    DATAHUB_PERMISSION_DENIED = "datahub_permission_denied"
    TARGET_CONFLICT = "target_conflict"
    READBACK_REQUIRED = "readback_required"
    UNEXPECTED_WORKER_FAILURE = "unexpected_worker_failure"


class RegistryPublicationFailureDisposition(StrEnum):
    RETRY = "retry"
    FAIL = "fail"
    DEAD_LETTER = "dead_letter"


class RegistryPublicationTransitionCode(StrEnum):
    INVALID_STATE = "registry_publication_invalid_state"
    LEASE_MISMATCH = "registry_publication_lease_mismatch"
    LEASE_EXPIRED = "registry_publication_lease_expired"
    FENCING_MISMATCH = "registry_publication_fencing_mismatch"
    APPROVAL_MISMATCH = "registry_publication_approval_mismatch"
    RESULT_MISMATCH = "registry_publication_result_mismatch"
    ATTEMPTS_EXHAUSTED = "registry_publication_attempts_exhausted"


class RegistryPublicationTransitionError(ValueError):
    def __init__(self, code: RegistryPublicationTransitionCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class RegistryPublicationEventKind(StrEnum):
    SUBMITTED = "submitted"
    LEASED = "leased"
    RECOVERY_LEASED = "recovery_leased"
    CANDIDATE_READY = "candidate_ready"
    AUTHORIZED = "authorized"
    AUTHORIZATION_EXPIRED = "authorization_expired"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    RETRY_SCHEDULED = "retry_scheduled"
    ACTIVATION_READY = "activation_ready"
    FAILED = "failed"
    DEAD_LETTERED = "dead_lettered"


class RegistryPublicationLease(FrozenDomainModel):
    job_id: str = Field(min_length=3, max_length=200)
    worker_id: str = Field(min_length=3, max_length=200)
    token_digest: str
    fencing_token: int = Field(ge=1)
    attempt: int = Field(ge=1)
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime

    @field_validator("job_id", "worker_id")
    @classmethod
    def identifiers_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("registry publication lease identifier is invalid")
        return value

    @field_validator("token_digest")
    @classmethod
    def token_must_be_a_digest(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("registry publication lease token digest is invalid")
        return value

    @field_validator("acquired_at", "heartbeat_at", "expires_at")
    @classmethod
    def times_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("registry publication lease timestamp must include a timezone")
        return value

    @model_validator(mode="after")
    def lease_window_must_be_exact(self) -> RegistryPublicationLease:
        if (
            not self.acquired_at <= self.heartbeat_at < self.expires_at
            or self.expires_at - self.heartbeat_at > _MAX_LEASE
            or self.fencing_token != self.attempt
        ):
            raise ValueError("registry publication lease is temporally inconsistent")
        return self

    def is_current(self, at: datetime) -> bool:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("registry publication lease check must include a timezone")
        return self.acquired_at <= at < self.expires_at


class RegistryPublicationJob(FrozenDomainModel):
    """One target reservation and its complete durable publication state."""

    id: str = Field(min_length=3, max_length=200)
    scope: SemanticRegistryScope
    proposal: PreparedRegistryPublicationProposal
    status: RegistryPublicationJobStatus = RegistryPublicationJobStatus.QUEUED
    submitted_by: str = Field(min_length=1, max_length=200)
    submitted_at: datetime
    idempotency_digest: str
    request_fingerprint: str
    revision: int = Field(default=1, ge=1)
    attempts: int = Field(default=0, ge=0, le=10)
    max_attempts: int = Field(default=5, ge=1, le=10)
    last_fencing_token: int = Field(default=0, ge=0)
    available_at: datetime
    lease: RegistryPublicationLease | None = None
    candidate: PublishableRegistryVersion | None = None
    authorization: RegistryPublicationAuthorization | None = None
    failure_code: RegistryPublicationFailureCode | None = None
    receipt: PublicationReadbackReceipt | None = None
    cancel_requested_at: datetime | None = None
    updated_at: datetime

    @field_validator("id")
    @classmethod
    def id_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("registry publication job id is invalid")
        return value

    @field_validator("submitted_by")
    @classmethod
    def submitter_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("registry publication submitter must not be blank")
        return value

    @field_validator("idempotency_digest", "request_fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("registry publication job fingerprint is invalid")
        return value

    @field_validator("submitted_at", "available_at", "cancel_requested_at", "updated_at")
    @classmethod
    def timestamps_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("registry publication job timestamp must include a timezone")
        return value

    @model_validator(mode="after")
    def state_must_be_complete_and_exact(self) -> RegistryPublicationJob:
        if (
            self.id
            != registry_publication_job_id(self.scope, self.proposal.target_registry_version)
            or self.proposal.scope != self.scope
            or self.proposal.workspace_id != self.scope.workspace_id
            or self.updated_at < self.submitted_at
            or self.available_at < self.submitted_at
            or self.attempts != self.last_fencing_token
            or self.attempts > self.max_attempts
        ):
            raise ValueError("registry publication job identity or counters are inconsistent")
        if self.candidate is not None and (
            self.candidate.scope != self.scope
            or self.candidate.source_proposal_id != self.proposal.id
            or self.candidate.source_proposal_fingerprint != self.proposal.fingerprint
        ):
            raise ValueError("registry publication job candidate belongs to another proposal")
        if self.authorization is not None:
            if self.candidate is None:
                raise ValueError("registry publication authorization requires a candidate")
            validate_registry_publication_authorization(self.candidate, self.authorization)
        if self.receipt is not None and (
            self.candidate is None
            or self.receipt.candidate_id != self.candidate.id
            or self.receipt.candidate_fingerprint != self.candidate.fingerprint
            or self.receipt.scope != self.candidate.scope
            or self.receipt.registry_version != self.candidate.registry.version
            or self.receipt.registry_fingerprint != self.candidate.registry.fingerprint
            or self.receipt.target != self.candidate.target
            or self.receipt.related_asset_urns
            != observed_registry_related_asset_urns(self.candidate.registry)
            or self.receipt.observed_at < self.submitted_at
            or self.receipt.observed_at > self.updated_at
        ):
            raise ValueError("registry publication receipt differs from its candidate")

        leased = self.status in {
            RegistryPublicationJobStatus.LEASED,
            RegistryPublicationJobStatus.CANCEL_REQUESTED,
        }
        if leased != (self.lease is not None):
            raise ValueError("registry publication lease does not match job status")
        if self.lease is not None and (
            self.lease.job_id != self.id
            or self.lease.attempt != self.attempts
            or self.lease.fencing_token != self.last_fencing_token
        ):
            raise ValueError("registry publication lease does not match job fencing")
        cancellation_retained = self.cancel_requested_at is not None
        cancellation_state = self.status is RegistryPublicationJobStatus.CANCEL_REQUESTED
        cancellation_recovery = (
            self.status is RegistryPublicationJobStatus.RETRY_WAIT
            and self.candidate is not None
            and self.failure_code is RegistryPublicationFailureCode.READBACK_REQUIRED
        )
        if (cancellation_state and not cancellation_retained) or (
            cancellation_retained and not (cancellation_state or cancellation_recovery)
        ):
            raise ValueError("registry publication cancellation request is inconsistent")

        if self.status is RegistryPublicationJobStatus.QUEUED and any(
            value is not None
            for value in (
                self.lease,
                self.candidate,
                self.authorization,
                self.failure_code,
                self.receipt,
            )
        ):
            raise ValueError("queued publication job contains later-phase state")
        if self.status in {
            RegistryPublicationJobStatus.LEASED,
            RegistryPublicationJobStatus.CANCEL_REQUESTED,
            RegistryPublicationJobStatus.RETRY_WAIT,
            RegistryPublicationJobStatus.FAILED,
            RegistryPublicationJobStatus.DEAD_LETTERED,
        } and ((self.candidate is None) != (self.authorization is None)):
            raise ValueError("publication candidate and authorization phase are inconsistent")
        if self.status is RegistryPublicationJobStatus.AWAITING_APPROVAL and (
            self.candidate is None
            or self.authorization is not None
            or self.lease is not None
            or self.receipt is not None
        ):
            raise ValueError("awaiting-approval job state is incomplete")
        if self.status is RegistryPublicationJobStatus.APPROVED and (
            self.candidate is None or self.authorization is None or self.lease is not None
        ):
            raise ValueError("approved publication job state is incomplete")
        if self.status is RegistryPublicationJobStatus.RETRY_WAIT and (
            self.lease is not None or self.failure_code is None or self.receipt is not None
        ):
            raise ValueError("retrying publication job state is incomplete")
        if self.status is RegistryPublicationJobStatus.ACTIVATION_READY and (
            self.candidate is None
            or self.authorization is None
            or self.receipt is None
            or self.failure_code is not None
            or self.lease is not None
        ):
            raise ValueError("activation-ready publication job state is incomplete")
        if self.status in {
            RegistryPublicationJobStatus.FAILED,
            RegistryPublicationJobStatus.DEAD_LETTERED,
        } and (self.failure_code is None or self.lease is not None or self.receipt is not None):
            raise ValueError("terminal failed publication job state is incomplete")
        if self.status is RegistryPublicationJobStatus.CANCELLED and (
            self.lease is not None or self.receipt is not None or self.authorization is not None
        ):
            raise ValueError("cancelled publication job retains active work")
        if not self.status.is_terminal and self.receipt is not None:
            raise ValueError("non-terminal publication job cannot carry a readback receipt")
        return self

    @property
    def phase(self) -> RegistryPublicationJobPhase:
        return (
            RegistryPublicationJobPhase.PREPARE
            if self.candidate is None
            else RegistryPublicationJobPhase.PUBLISH
        )


class RegistryPublicationEvent(FrozenDomainModel):
    id: str = Field(min_length=3, max_length=200)
    workspace_id: str = Field(min_length=3, max_length=200)
    job_id: str = Field(min_length=3, max_length=200)
    revision: int = Field(ge=1)
    kind: RegistryPublicationEventKind
    status: RegistryPublicationJobStatus
    actor_id: str | None = Field(default=None, min_length=1, max_length=200)
    worker_id: str | None = Field(default=None, min_length=3, max_length=200)
    failure_code: RegistryPublicationFailureCode | None = None
    occurred_at: datetime

    @field_validator("id", "job_id", "worker_id")
    @classmethod
    def inert_ids(cls, value: str | None) -> str | None:
        if value is not None and _SAFE_ID.fullmatch(value) is None:
            raise ValueError("registry publication event identifier is invalid")
        return value

    @field_validator("occurred_at")
    @classmethod
    def occurred_time_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("registry publication event timestamp must include a timezone")
        return value


def create_registry_publication_job(
    proposal: PreparedRegistryPublicationProposal,
    *,
    submitted_by: str,
    submitted_at: datetime,
    idempotency_digest: str,
    request_fingerprint: str,
    max_attempts: int = 5,
) -> RegistryPublicationJob:
    return RegistryPublicationJob(
        id=registry_publication_job_id(proposal.scope, proposal.target_registry_version),
        scope=proposal.scope,
        proposal=proposal,
        submitted_by=submitted_by,
        submitted_at=submitted_at,
        idempotency_digest=idempotency_digest,
        request_fingerprint=request_fingerprint,
        max_attempts=max_attempts,
        available_at=submitted_at,
        updated_at=submitted_at,
    )


def lease_registry_publication_job(
    job: RegistryPublicationJob,
    *,
    worker_id: str,
    lease_capability: str,
    acquired_at: datetime,
    lease_duration: timedelta,
) -> RegistryPublicationJob:
    if (
        job.status
        not in {
            RegistryPublicationJobStatus.QUEUED,
            RegistryPublicationJobStatus.APPROVED,
            RegistryPublicationJobStatus.RETRY_WAIT,
        }
        or acquired_at < job.available_at
    ):
        raise RegistryPublicationTransitionError(
            RegistryPublicationTransitionCode.INVALID_STATE,
            "registry publication job is not claimable",
        )
    if job.attempts >= job.max_attempts:
        raise RegistryPublicationTransitionError(
            RegistryPublicationTransitionCode.ATTEMPTS_EXHAUSTED,
            "registry publication job exhausted its attempts",
        )
    if job.candidate is not None and job.authorization is None:
        raise RegistryPublicationTransitionError(
            RegistryPublicationTransitionCode.APPROVAL_MISMATCH,
            "registry publication candidate has no authorization",
        )
    if not timedelta(seconds=1) <= lease_duration <= _MAX_LEASE:
        raise ValueError("registry publication lease duration is outside its bound")
    attempt = job.attempts + 1
    lease = RegistryPublicationLease(
        job_id=job.id,
        worker_id=worker_id,
        token_digest=digest_registry_publication_lease_capability(lease_capability),
        fencing_token=attempt,
        attempt=attempt,
        acquired_at=acquired_at,
        heartbeat_at=acquired_at,
        expires_at=acquired_at + lease_duration,
    )
    claimed_status = (
        RegistryPublicationJobStatus.CANCEL_REQUESTED
        if job.cancel_requested_at is not None
        else RegistryPublicationJobStatus.LEASED
    )
    return _replace_job(
        job,
        status=claimed_status,
        attempts=attempt,
        last_fencing_token=attempt,
        lease=lease,
        failure_code=None,
        updated_at=acquired_at,
    )


def heartbeat_registry_publication_job(
    job: RegistryPublicationJob,
    *,
    worker_id: str,
    lease_capability: str,
    fencing_token: int,
    heartbeat_at: datetime,
    lease_duration: timedelta,
) -> RegistryPublicationJob:
    lease = _require_lease(
        job,
        worker_id=worker_id,
        lease_capability=lease_capability,
        fencing_token=fencing_token,
        at=heartbeat_at,
        allow_cancel=True,
    )
    renewed = RegistryPublicationLease(
        **{
            **lease.model_dump(mode="python"),
            "heartbeat_at": heartbeat_at,
            "expires_at": heartbeat_at + lease_duration,
        }
    )
    return _replace_job(job, lease=renewed, updated_at=heartbeat_at)


def record_registry_publication_candidate(
    job: RegistryPublicationJob,
    candidate: PublishableRegistryVersion,
    *,
    worker_id: str,
    lease_capability: str,
    fencing_token: int,
    completed_at: datetime,
) -> RegistryPublicationJob:
    _require_lease(
        job,
        worker_id=worker_id,
        lease_capability=lease_capability,
        fencing_token=fencing_token,
        at=completed_at,
    )
    if job.phase is not RegistryPublicationJobPhase.PREPARE:
        raise RegistryPublicationTransitionError(
            RegistryPublicationTransitionCode.INVALID_STATE,
            "registry publication job is not preparing",
        )
    if (
        candidate.scope != job.scope
        or candidate.source_proposal_id != job.proposal.id
        or candidate.source_proposal_fingerprint != job.proposal.fingerprint
    ):
        raise RegistryPublicationTransitionError(
            RegistryPublicationTransitionCode.RESULT_MISMATCH,
            "registry publication candidate differs from the reserved proposal",
        )
    return _replace_job(
        job,
        status=RegistryPublicationJobStatus.AWAITING_APPROVAL,
        lease=None,
        candidate=candidate,
        authorization=None,
        failure_code=None,
        updated_at=completed_at,
    )


def authorize_registry_publication_job(
    job: RegistryPublicationJob,
    authorization: RegistryPublicationAuthorization,
    *,
    authorized_at: datetime,
) -> RegistryPublicationJob:
    if job.status is not RegistryPublicationJobStatus.AWAITING_APPROVAL or job.candidate is None:
        raise RegistryPublicationTransitionError(
            RegistryPublicationTransitionCode.INVALID_STATE,
            "registry publication job is not awaiting approval",
        )
    try:
        validate_registry_publication_authorization(
            job.candidate,
            authorization,
            at=authorized_at,
        )
    except ValueError as error:
        raise RegistryPublicationTransitionError(
            RegistryPublicationTransitionCode.APPROVAL_MISMATCH,
            "registry publication authorization differs from the candidate",
        ) from error
    return _replace_job(
        job,
        status=RegistryPublicationJobStatus.APPROVED,
        authorization=authorization,
        failure_code=None,
        available_at=authorized_at,
        updated_at=authorized_at,
    )


def request_registry_publication_cancellation(
    job: RegistryPublicationJob,
    *,
    requested_at: datetime,
) -> RegistryPublicationJob:
    if job.status.is_terminal:
        return job
    if job.status is RegistryPublicationJobStatus.CANCEL_REQUESTED or (
        job.status is RegistryPublicationJobStatus.RETRY_WAIT
        and job.cancel_requested_at is not None
    ):
        return job
    if job.status is RegistryPublicationJobStatus.LEASED:
        return _replace_job(
            job,
            status=RegistryPublicationJobStatus.CANCEL_REQUESTED,
            cancel_requested_at=requested_at,
            updated_at=requested_at,
        )
    return _replace_job(
        job,
        status=RegistryPublicationJobStatus.CANCELLED,
        lease=None,
        authorization=None,
        failure_code=None,
        cancel_requested_at=None,
        updated_at=requested_at,
    )


def acknowledge_registry_publication_cancellation(
    job: RegistryPublicationJob,
    *,
    worker_id: str,
    lease_capability: str,
    fencing_token: int,
    cancelled_at: datetime,
) -> RegistryPublicationJob:
    _require_lease(
        job,
        worker_id=worker_id,
        lease_capability=lease_capability,
        fencing_token=fencing_token,
        at=cancelled_at,
        require_cancel=True,
    )
    return _replace_job(
        job,
        status=RegistryPublicationJobStatus.CANCELLED,
        lease=None,
        authorization=None,
        failure_code=None,
        cancel_requested_at=None,
        updated_at=cancelled_at,
    )


def complete_registry_publication_job(
    job: RegistryPublicationJob,
    receipt: PublicationReadbackReceipt,
    *,
    worker_id: str,
    lease_capability: str,
    fencing_token: int,
    completed_at: datetime,
) -> RegistryPublicationJob:
    lease = _require_lease(
        job,
        worker_id=worker_id,
        lease_capability=lease_capability,
        fencing_token=fencing_token,
        at=completed_at,
        allow_cancel=True,
    )
    if job.phase is not RegistryPublicationJobPhase.PUBLISH or job.candidate is None:
        raise RegistryPublicationTransitionError(
            RegistryPublicationTransitionCode.INVALID_STATE,
            "registry publication job is not publishing",
        )
    if (
        receipt.candidate_id != job.candidate.id
        or receipt.candidate_fingerprint != job.candidate.fingerprint
        or receipt.scope != job.candidate.scope
        or receipt.registry_version != job.candidate.registry.version
        or receipt.registry_fingerprint != job.candidate.registry.fingerprint
        or receipt.target != job.candidate.target
        or receipt.related_asset_urns
        != observed_registry_related_asset_urns(job.candidate.registry)
        or receipt.observed_at < lease.acquired_at
        or receipt.observed_at > completed_at
    ):
        raise RegistryPublicationTransitionError(
            RegistryPublicationTransitionCode.RESULT_MISMATCH,
            "registry publication readback differs from the candidate",
        )
    return _replace_job(
        job,
        status=RegistryPublicationJobStatus.ACTIVATION_READY,
        lease=None,
        failure_code=None,
        receipt=receipt,
        cancel_requested_at=None,
        updated_at=completed_at,
    )


def fail_registry_publication_job(
    job: RegistryPublicationJob,
    code: RegistryPublicationFailureCode,
    *,
    worker_id: str,
    lease_capability: str,
    fencing_token: int,
    failed_at: datetime,
) -> RegistryPublicationJob:
    _require_lease(
        job,
        worker_id=worker_id,
        lease_capability=lease_capability,
        fencing_token=fencing_token,
        at=failed_at,
        allow_cancel=True,
    )
    effective_code = (
        RegistryPublicationFailureCode.READBACK_REQUIRED
        if (
            job.status is RegistryPublicationJobStatus.CANCEL_REQUESTED
            and job.phase is RegistryPublicationJobPhase.PUBLISH
            and code is not RegistryPublicationFailureCode.TARGET_CONFLICT
        )
        else code
    )
    if effective_code is RegistryPublicationFailureCode.AUTHORIZATION_EXPIRED:
        if job.candidate is None:
            raise RegistryPublicationTransitionError(
                RegistryPublicationTransitionCode.INVALID_STATE,
                "registry publication authorization expiry requires a candidate",
            )
        return _replace_job(
            job,
            status=RegistryPublicationJobStatus.AWAITING_APPROVAL,
            lease=None,
            authorization=None,
            failure_code=None,
            cancel_requested_at=None,
            updated_at=failed_at,
        )
    disposition = classify_registry_publication_failure(effective_code)
    if (
        disposition is RegistryPublicationFailureDisposition.RETRY
        and job.attempts < job.max_attempts
    ):
        return _replace_job(
            job,
            status=RegistryPublicationJobStatus.RETRY_WAIT,
            lease=None,
            failure_code=effective_code,
            available_at=failed_at + registry_publication_retry_delay(job.attempts),
            cancel_requested_at=(
                job.cancel_requested_at
                if job.phase is RegistryPublicationJobPhase.PUBLISH
                else None
            ),
            updated_at=failed_at,
        )
    terminal = (
        RegistryPublicationJobStatus.FAILED
        if disposition is RegistryPublicationFailureDisposition.FAIL
        else RegistryPublicationJobStatus.DEAD_LETTERED
    )
    if disposition is RegistryPublicationFailureDisposition.RETRY:
        terminal = RegistryPublicationJobStatus.DEAD_LETTERED
    return _replace_job(
        job,
        status=terminal,
        lease=None,
        failure_code=effective_code,
        cancel_requested_at=None,
        updated_at=failed_at,
    )


def reap_expired_registry_publication_lease(
    job: RegistryPublicationJob,
    *,
    expired_at: datetime,
) -> RegistryPublicationJob:
    """Recover one abandoned lease without trusting the former worker.

    Preparation has no external effect and may honor a pending cancellation.
    Publication may have written before the process died, so it always enters
    read-back recovery; a cancellation label cannot erase that ambiguity.
    """

    lease = job.lease
    if (
        lease is None
        or job.status
        not in {
            RegistryPublicationJobStatus.LEASED,
            RegistryPublicationJobStatus.CANCEL_REQUESTED,
        }
        or lease.expires_at > expired_at
    ):
        raise RegistryPublicationTransitionError(
            RegistryPublicationTransitionCode.INVALID_STATE,
            "registry publication lease is not expired",
        )
    if (
        job.status is RegistryPublicationJobStatus.CANCEL_REQUESTED
        and job.phase is RegistryPublicationJobPhase.PREPARE
    ):
        return _replace_job(
            job,
            status=RegistryPublicationJobStatus.CANCELLED,
            lease=None,
            authorization=None,
            failure_code=None,
            cancel_requested_at=None,
            updated_at=expired_at,
        )
    code = (
        RegistryPublicationFailureCode.BASE_UNAVAILABLE
        if job.phase is RegistryPublicationJobPhase.PREPARE
        else RegistryPublicationFailureCode.READBACK_REQUIRED
    )
    if job.attempts < job.max_attempts:
        return _replace_job(
            job,
            status=RegistryPublicationJobStatus.RETRY_WAIT,
            lease=None,
            failure_code=code,
            available_at=expired_at + registry_publication_retry_delay(job.attempts),
            cancel_requested_at=(
                job.cancel_requested_at
                if job.phase is RegistryPublicationJobPhase.PUBLISH
                else None
            ),
            updated_at=expired_at,
        )
    return _replace_job(
        job,
        status=RegistryPublicationJobStatus.DEAD_LETTERED,
        lease=None,
        failure_code=code,
        cancel_requested_at=None,
        updated_at=expired_at,
    )


def classify_registry_publication_failure(
    code: RegistryPublicationFailureCode,
) -> RegistryPublicationFailureDisposition:
    if code in {
        RegistryPublicationFailureCode.BASE_UNAVAILABLE,
        RegistryPublicationFailureCode.DATAHUB_UNAVAILABLE,
        RegistryPublicationFailureCode.READBACK_REQUIRED,
    }:
        return RegistryPublicationFailureDisposition.RETRY
    if code in {
        RegistryPublicationFailureCode.TARGET_CONFLICT,
        RegistryPublicationFailureCode.UNEXPECTED_WORKER_FAILURE,
    }:
        return RegistryPublicationFailureDisposition.DEAD_LETTER
    return RegistryPublicationFailureDisposition.FAIL


def registry_publication_retry_delay(attempt: int) -> timedelta:
    if attempt < 1:
        raise ValueError("registry publication retry attempt must be positive")
    return timedelta(seconds=min(5 * (2 ** (attempt - 1)), _MAX_RETRY_DELAY_SECONDS))


def registry_publication_job_id(scope: SemanticRegistryScope, target_version: int) -> str:
    if target_version < 1:
        raise ValueError("registry publication target version must be positive")
    payload = {
        "contract": "registry_publication_target_reservation_v1",
        "scope": scope.model_dump(mode="json"),
        "target_version": target_version,
    }
    return f"registry-publication-{_fingerprint(payload)}"


def registry_publication_request_fingerprint(
    proposal: PreparedRegistryPublicationProposal,
    *,
    submitted_by: str,
) -> str:
    return _fingerprint(
        {
            "contract": "registry_publication_submission_v1",
            "proposal_id": proposal.id,
            "proposal_fingerprint": proposal.fingerprint,
            "scope": proposal.scope.model_dump(mode="json"),
            "target_version": proposal.target_registry_version,
            "submitted_by": submitted_by,
        }
    )


def digest_registry_publication_lease_capability(capability: str) -> str:
    if (
        not isinstance(capability, str)
        or not 32 <= len(capability) <= 512
        or capability.strip() != capability
    ):
        raise ValueError("registry publication lease capability is invalid")
    return hashlib.sha256(capability.encode()).hexdigest()


def _require_lease(
    job: RegistryPublicationJob,
    *,
    worker_id: str,
    lease_capability: str,
    fencing_token: int,
    at: datetime,
    require_cancel: bool = False,
    allow_cancel: bool = False,
) -> RegistryPublicationLease:
    expected_statuses = (
        {RegistryPublicationJobStatus.CANCEL_REQUESTED}
        if require_cancel
        else {RegistryPublicationJobStatus.LEASED}
    )
    if allow_cancel:
        expected_statuses.add(RegistryPublicationJobStatus.CANCEL_REQUESTED)
    lease = job.lease
    if lease is None or job.status not in expected_statuses:
        raise RegistryPublicationTransitionError(
            RegistryPublicationTransitionCode.INVALID_STATE,
            "registry publication job has no applicable lease",
        )
    if lease.fencing_token != fencing_token or job.last_fencing_token != fencing_token:
        raise RegistryPublicationTransitionError(
            RegistryPublicationTransitionCode.FENCING_MISMATCH,
            "registry publication fencing token is stale",
        )
    if lease.worker_id != worker_id or not hmac.compare_digest(
        lease.token_digest,
        digest_registry_publication_lease_capability(lease_capability),
    ):
        raise RegistryPublicationTransitionError(
            RegistryPublicationTransitionCode.LEASE_MISMATCH,
            "registry publication lease capability does not match",
        )
    if not lease.is_current(at):
        raise RegistryPublicationTransitionError(
            RegistryPublicationTransitionCode.LEASE_EXPIRED,
            "registry publication lease expired",
        )
    return lease


def _replace_job(job: RegistryPublicationJob, **changes: object) -> RegistryPublicationJob:
    return RegistryPublicationJob.model_validate(
        {
            **job.model_dump(mode="python"),
            **changes,
            "revision": job.revision + 1,
        }
    )


def _fingerprint(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


__all__ = [
    "PreparedRegistryPublicationProposal",
    "RegistryPublicationEvent",
    "RegistryPublicationEventKind",
    "RegistryPublicationFailureCode",
    "RegistryPublicationFailureDisposition",
    "RegistryPublicationJob",
    "RegistryPublicationJobPhase",
    "RegistryPublicationJobStatus",
    "RegistryPublicationLease",
    "RegistryPublicationTransitionCode",
    "RegistryPublicationTransitionError",
    "acknowledge_registry_publication_cancellation",
    "authorize_registry_publication_job",
    "classify_registry_publication_failure",
    "complete_registry_publication_job",
    "create_registry_publication_job",
    "digest_registry_publication_lease_capability",
    "fail_registry_publication_job",
    "heartbeat_registry_publication_job",
    "lease_registry_publication_job",
    "reap_expired_registry_publication_lease",
    "record_registry_publication_candidate",
    "registry_publication_job_id",
    "registry_publication_request_fingerprint",
    "registry_publication_retry_delay",
    "request_registry_publication_cancellation",
]
