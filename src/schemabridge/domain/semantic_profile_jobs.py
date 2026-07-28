"""Pure durable-job contracts for aggregate-only semantic join profiling."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import MAX_ROUTE_REVISION, GovernedExecutionTarget
from schemabridge.domain.joins import JoinProposal, RelationshipProfile

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SCAN_ID = re.compile(r"^scan_[0-9a-f]{64}$")
_JOB_ID = re.compile(r"^profile_job_[0-9a-f]{64}$")
_INERT_ID = re.compile(r"^[a-z][a-z0-9_-]{2,199}$")
_MAX_PROPOSAL_BYTES = 65_536
_MAX_RESULT_BYTES = 16_384
_MAX_LEASE_DURATION = timedelta(minutes=5)
_MAX_RETENTION = timedelta(days=366)
_INITIAL_RETRY_DELAY_SECONDS = 5
_MAX_RETRY_DELAY_SECONDS = 300
_PROTECTED_KEYS = frozenset(
    {
        "raw_value",
        "source_row",
        "sample_value",
        "sql",
        "parameters",
        "prompt",
        "token",
        "dsn",
        "password",
        "secret",
        "api_key",
        "access_token",
        "authorization",
        "credential",
        "connection_string",
        "client_secret",
    }
)


class SemanticJoinProfileJobStatus(StrEnum):
    """Closed lifecycle for one exact scan/proposal profiling request."""

    REQUESTED = "requested"
    LEASED = "leased"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in {
            SemanticJoinProfileJobStatus.COMPLETED,
            SemanticJoinProfileJobStatus.FAILED,
        }


class SemanticJoinProfileFailureCode(StrEnum):
    """Sanitized worker failures; exception text is never durable."""

    SOURCE_UNAVAILABLE = "source_unavailable"
    SOURCE_TIMEOUT = "source_timeout"
    LEASE_EXPIRED = "lease_expired"
    SHUTDOWN_REQUESTED = "shutdown_requested"
    SOURCE_CONNECTION_MISMATCH = "source_connection_mismatch"
    PROPOSAL_INVALID = "proposal_invalid"
    EVIDENCE_INVALID = "evidence_invalid"
    UNEXPECTED_WORKER_FAILURE = "unexpected_worker_failure"


class SemanticJoinProfileFailureDisposition(StrEnum):
    RETRY = "retry"
    FAIL = "fail"


_RETRYABLE_FAILURES = frozenset(
    {
        SemanticJoinProfileFailureCode.SOURCE_UNAVAILABLE,
        SemanticJoinProfileFailureCode.SOURCE_TIMEOUT,
        SemanticJoinProfileFailureCode.LEASE_EXPIRED,
        SemanticJoinProfileFailureCode.SHUTDOWN_REQUESTED,
    }
)


class SemanticJoinProfileTransitionErrorCode(StrEnum):
    """Stable deterministic lifecycle rejections."""

    INVALID_STATE = "semantic_profile_job_invalid_state"
    NOT_AVAILABLE = "semantic_profile_job_not_available"
    ATTEMPTS_EXHAUSTED = "semantic_profile_job_attempts_exhausted"
    LEASE_MISMATCH = "semantic_profile_job_lease_mismatch"
    LEASE_EXPIRED = "semantic_profile_job_lease_expired"
    FENCING_MISMATCH = "semantic_profile_job_fencing_mismatch"
    TEMPORAL_CONFLICT = "semantic_profile_job_temporal_conflict"
    TERMINAL_IMMUTABLE = "semantic_profile_job_terminal_immutable"
    RESULT_MISMATCH = "semantic_profile_job_result_mismatch"


class SemanticJoinProfileTransitionError(ValueError):
    """Pure lifecycle error raised before any durable mutation."""

    def __init__(
        self,
        code: SemanticJoinProfileTransitionErrorCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


class SemanticJoinProfileLease(FrozenDomainModel):
    """Transient ownership represented durably only by a capability digest."""

    job_id: str
    worker_id: str
    capability_digest: str
    fencing_token: int = Field(ge=1)
    attempt: int = Field(ge=1, le=100)
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime

    @field_validator("job_id")
    @classmethod
    def job_id_must_be_canonical(cls, value: str) -> str:
        return _profile_job_id(value)

    @field_validator("worker_id")
    @classmethod
    def worker_id_must_be_inert(cls, value: str) -> str:
        return _inert_id(value, "semantic profile worker identifier")

    @field_validator("capability_digest")
    @classmethod
    def capability_must_be_digest_only(cls, value: str) -> str:
        return _sha256(value, "semantic profile capability digest")

    @field_validator("acquired_at", "heartbeat_at", "expires_at")
    @classmethod
    def lease_times_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "semantic profile lease timestamp")

    @model_validator(mode="after")
    def lease_window_must_be_bounded(self) -> SemanticJoinProfileLease:
        if not self.acquired_at <= self.heartbeat_at < self.expires_at:
            raise ValueError("semantic profile lease times are not ordered")
        if self.expires_at - self.heartbeat_at > _MAX_LEASE_DURATION:
            raise ValueError("semantic profile lease exceeds its maximum duration")
        if self.fencing_token != self.attempt:
            raise ValueError("semantic profile fence must equal its monotonic attempt")
        return self

    def is_current(self, at: datetime) -> bool:
        instant = _aware(at, "semantic profile lease check")
        return self.acquired_at <= instant < self.expires_at


class SemanticJoinProfileFailure(FrozenDomainModel):
    code: SemanticJoinProfileFailureCode
    disposition: SemanticJoinProfileFailureDisposition
    attempt: int = Field(ge=1, le=100)
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def failure_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "semantic profile failure timestamp")


class SemanticJoinProfileResult(FrozenDomainModel):
    """Aggregate-only result; it cannot carry source rows, key values, or SQL."""

    profile: RelationshipProfile
    fingerprint: str
    completed_at: datetime

    @field_validator("fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "semantic profile result fingerprint")

    @field_validator("completed_at")
    @classmethod
    def completion_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "semantic profile completion timestamp")

    @model_validator(mode="after")
    def result_must_be_canonical_and_aggregate_only(self) -> SemanticJoinProfileResult:
        if self.fingerprint != semantic_join_profile_result_fingerprint(self.profile):
            raise ValueError("semantic profile result fingerprint does not match")
        return self

    @classmethod
    def create(
        cls,
        profile: RelationshipProfile,
        *,
        completed_at: datetime,
    ) -> SemanticJoinProfileResult:
        checked = validate_semantic_join_profile_result(profile)
        return cls(
            profile=checked,
            fingerprint=semantic_join_profile_result_fingerprint(checked),
            completed_at=completed_at,
        )


class SemanticJoinProfileProposal(FrozenDomainModel):
    """One aggregate profile bound to the exact catalog connection it may read."""

    connection_id: CatalogConnectionId
    proposal: JoinProposal

    @model_validator(mode="after")
    def proposal_must_be_canonical_and_aggregate_only(
        self,
    ) -> SemanticJoinProfileProposal:
        _validate_join_profile_proposal(self.proposal)
        return self


class SemanticJoinProfileTargetRef(FrozenDomainModel):
    """Minimal secret-free connector target persisted with one profile job."""

    workspace_id: str = Field(min_length=1, max_length=200)
    connection_id: CatalogConnectionId
    route_revision: int = Field(strict=True, ge=1, le=MAX_ROUTE_REVISION)
    route_fingerprint: str
    target_fingerprint: str

    @field_validator("workspace_id")
    @classmethod
    def workspace_must_be_bounded(cls, value: str) -> str:
        return _bounded_text(value, "semantic profile workspace", maximum_bytes=200)

    @field_validator("route_fingerprint", "target_fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "semantic profile connector target fingerprint")

    @classmethod
    def from_target(
        cls,
        target: GovernedExecutionTarget,
    ) -> SemanticJoinProfileTargetRef:
        """Minimize one governed target to its durable profile-job identity."""

        return cls(
            workspace_id=target.workspace_id,
            connection_id=target.connection_id,
            route_revision=target.route_revision,
            route_fingerprint=target.route_fingerprint,
            target_fingerprint=target.fingerprint,
        )


class SemanticJoinProfileJob(FrozenDomainModel):
    """Complete immutable view of one durable aggregate-profile job."""

    job_id: str
    workspace_id: str = Field(min_length=1, max_length=200)
    scan_id: str
    connection_id: CatalogConnectionId
    execution_target: SemanticJoinProfileTargetRef | None
    connector_contract_version: int | None = Field(
        default=None,
        strict=True,
        ge=1,
        le=MAX_ROUTE_REVISION,
    )
    proposal: JoinProposal
    proposal_fingerprint: str
    status: SemanticJoinProfileJobStatus
    max_attempts: int = Field(default=5, ge=1, le=100)
    attempts: int = Field(default=0, ge=0, le=100)
    available_at: datetime
    fencing_token: int = Field(default=0, ge=0)
    lease: SemanticJoinProfileLease | None = None
    failure: SemanticJoinProfileFailure | None = None
    result: SemanticJoinProfileResult | None = None
    requested_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    retain_until: datetime | None = None

    @field_validator("job_id")
    @classmethod
    def job_id_must_be_canonical(cls, value: str) -> str:
        return _profile_job_id(value)

    @field_validator("workspace_id")
    @classmethod
    def workspace_must_be_bounded(cls, value: str) -> str:
        return _bounded_text(value, "semantic profile workspace", maximum_bytes=200)

    @field_validator("scan_id")
    @classmethod
    def scan_id_must_be_canonical(cls, value: str) -> str:
        if _SCAN_ID.fullmatch(value) is None:
            raise ValueError("semantic profile scan id must be canonical")
        return value

    @field_validator("proposal_fingerprint")
    @classmethod
    def proposal_fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "semantic profile proposal fingerprint")

    @field_validator(
        "available_at",
        "requested_at",
        "updated_at",
        "completed_at",
        "retain_until",
    )
    @classmethod
    def lifecycle_times_must_be_aware(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is None:
            return None
        return _aware(value, "semantic profile lifecycle timestamp")

    @model_validator(mode="after")
    def lifecycle_and_identity_must_be_exact(self) -> SemanticJoinProfileJob:
        if self.execution_target is None:
            if not self.status.is_terminal or self.connector_contract_version is not None:
                raise ValueError("non-terminal semantic profile job requires a connector target")
        elif (
            self.execution_target.workspace_id != self.workspace_id
            or self.execution_target.connection_id != self.connection_id
        ):
            raise ValueError("semantic profile connector target does not match its exact scope")
        if (
            self.status is not SemanticJoinProfileJobStatus.REQUESTED
            and self.execution_target is not None
            and self.connector_contract_version is None
        ):
            raise ValueError("claimed semantic profile job requires a connector contract version")
        bound_proposal = SemanticJoinProfileProposal(
            connection_id=self.connection_id,
            proposal=self.proposal,
        )
        validate_semantic_join_profile_proposal(bound_proposal)
        if self.proposal_fingerprint != semantic_join_profile_proposal_fingerprint(bound_proposal):
            raise ValueError("semantic profile proposal fingerprint does not match")
        if self.job_id != semantic_join_profile_job_id(
            self.workspace_id,
            self.scan_id,
            self.proposal_fingerprint,
        ):
            raise ValueError("semantic profile job id does not match its exact scope")
        if self.attempts > self.max_attempts or self.fencing_token != self.attempts:
            raise ValueError("semantic profile attempt and fence are inconsistent")
        if self.updated_at < self.requested_at or self.available_at < self.requested_at:
            raise ValueError("semantic profile lifecycle times precede its request")
        self._validate_terminal_times()

        if self.lease is not None and (
            self.lease.job_id != self.job_id
            or self.lease.attempt != self.attempts
            or self.lease.fencing_token != self.fencing_token
            or self.lease.heartbeat_at != self.updated_at
        ):
            raise ValueError("semantic profile lease does not match durable owner state")
        if self.failure is not None and (
            self.failure.attempt != self.attempts
            or self.failure.occurred_at != self.updated_at
            or self.failure.disposition
            is not classify_semantic_join_profile_failure(
                self.failure.code,
                attempt=self.attempts,
                max_attempts=self.max_attempts,
            )
        ):
            raise ValueError("semantic profile failure does not match retry policy")

        no_terminal = (
            self.completed_at is None and self.retain_until is None and self.result is None
        )
        if self.status is SemanticJoinProfileJobStatus.REQUESTED:
            self._require(
                self.attempts == 0
                and self.fencing_token == 0
                and self.lease is None
                and self.failure is None
                and no_terminal,
                "requested semantic profile job is inconsistent",
            )
        elif self.status is SemanticJoinProfileJobStatus.LEASED:
            self._require(
                self.attempts >= 1
                and self.lease is not None
                and self.failure is None
                and no_terminal,
                "leased semantic profile job is inconsistent",
            )
        elif self.status is SemanticJoinProfileJobStatus.RETRY_WAIT:
            self._require(
                self.attempts >= 1
                and self.lease is None
                and self.failure is not None
                and self.failure.disposition is SemanticJoinProfileFailureDisposition.RETRY
                and no_terminal,
                "retry-wait semantic profile job is inconsistent",
            )
        elif self.status is SemanticJoinProfileJobStatus.COMPLETED:
            self._require(
                self.lease is None and self.failure is None and self.result is not None,
                "completed semantic profile job is inconsistent",
            )
            assert self.result is not None
            self._require(
                self.result.completed_at == self.completed_at,
                "semantic profile result does not match terminal time",
            )
        else:
            self._require(
                self.status is SemanticJoinProfileJobStatus.FAILED
                and self.lease is None
                and self.failure is not None
                and self.failure.disposition is SemanticJoinProfileFailureDisposition.FAIL
                and self.result is None,
                "failed semantic profile job is inconsistent",
            )
        return self

    def _validate_terminal_times(self) -> None:
        if self.status.is_terminal:
            if (
                self.completed_at is None
                or self.retain_until is None
                or self.updated_at != self.completed_at
                or not self.completed_at <= self.retain_until
                or self.retain_until - self.completed_at > _MAX_RETENTION
            ):
                raise ValueError("semantic profile terminal retention window is invalid")
        elif self.completed_at is not None or self.retain_until is not None:
            raise ValueError("non-terminal semantic profile job cannot carry terminal times")

    @staticmethod
    def _require(condition: bool, message: str) -> None:
        if not condition:
            raise ValueError(message)

    @classmethod
    def requested(
        cls,
        *,
        workspace_id: str,
        scan_id: str,
        proposal: SemanticJoinProfileProposal,
        execution_target: SemanticJoinProfileTargetRef,
        requested_at: datetime,
        max_attempts: int = 5,
        connector_contract_version: int | None = None,
    ) -> SemanticJoinProfileJob:
        checked = validate_semantic_join_profile_proposal(proposal)
        checked_target = SemanticJoinProfileTargetRef.model_validate(
            execution_target.model_dump(mode="python")
        )
        workspace = validate_semantic_join_profile_workspace_id(workspace_id)
        if (
            checked_target.workspace_id != workspace
            or checked_target.connection_id != checked.connection_id
        ):
            raise ValueError("semantic profile connector target does not match its exact scope")
        proposal_fingerprint = semantic_join_profile_proposal_fingerprint(checked)
        return cls(
            job_id=semantic_join_profile_job_id(
                workspace,
                scan_id,
                proposal_fingerprint,
            ),
            workspace_id=workspace,
            scan_id=scan_id,
            connection_id=checked.connection_id,
            execution_target=checked_target,
            connector_contract_version=connector_contract_version,
            proposal=checked.proposal,
            proposal_fingerprint=proposal_fingerprint,
            status=SemanticJoinProfileJobStatus.REQUESTED,
            max_attempts=max_attempts,
            attempts=0,
            available_at=requested_at,
            fencing_token=0,
            requested_at=requested_at,
            updated_at=requested_at,
        )

    @property
    def bound_proposal(self) -> SemanticJoinProfileProposal:
        """Reconstruct the exact connection-qualified proposal carried by this job."""

        return SemanticJoinProfileProposal(
            connection_id=self.connection_id,
            proposal=self.proposal,
        )


class SemanticJoinProfileSubmission(FrozenDomainModel):
    job: SemanticJoinProfileJob
    replayed: bool


def validate_semantic_join_profile_proposal(
    proposal: SemanticJoinProfileProposal,
) -> SemanticJoinProfileProposal:
    """Round-trip one bounded typed proposal and reject value-bearing transformations."""

    checked = SemanticJoinProfileProposal.model_validate(proposal.model_dump(mode="json"))
    payload = checked.model_dump(mode="json")
    _assert_no_protected_keys(payload)
    canonical = _canonical_json(payload)
    if len(canonical.encode("utf-8")) > _MAX_PROPOSAL_BYTES:
        raise ValueError("semantic profile proposal exceeds its durable payload bound")
    return checked


def _validate_join_profile_proposal(proposal: JoinProposal) -> JoinProposal:
    checked = JoinProposal.model_validate(proposal.model_dump(mode="json"))
    payload = checked.model_dump(mode="json")
    _assert_no_protected_keys(payload)
    for key in (checked.left_key, checked.right_key):
        if any(step.operation == "map_values" for step in key.transformation_plan.steps):
            raise ValueError("semantic profile proposals cannot persist value maps")
    return checked


def validate_semantic_join_profile_result(
    profile: RelationshipProfile,
) -> RelationshipProfile:
    """Round-trip aggregate evidence and enforce its small protected-data boundary."""

    checked = RelationshipProfile.model_validate(profile.model_dump(mode="json"))
    payload = checked.model_dump(mode="json")
    _assert_no_protected_keys(payload)
    canonical = _canonical_json(payload)
    if len(canonical.encode("utf-8")) > _MAX_RESULT_BYTES:
        raise ValueError("semantic profile result exceeds its durable payload bound")
    return checked


def canonical_semantic_join_profile_proposal_json(
    proposal: SemanticJoinProfileProposal,
) -> str:
    checked = validate_semantic_join_profile_proposal(proposal)
    return _canonical_json(checked.model_dump(mode="json"))


def semantic_join_profile_proposal_fingerprint(
    proposal: SemanticJoinProfileProposal,
) -> str:
    return hashlib.sha256(
        canonical_semantic_join_profile_proposal_json(proposal).encode("utf-8")
    ).hexdigest()


def semantic_join_profile_result_fingerprint(profile: RelationshipProfile) -> str:
    checked = validate_semantic_join_profile_result(profile)
    return hashlib.sha256(
        _canonical_json(checked.model_dump(mode="json")).encode("utf-8")
    ).hexdigest()


def semantic_join_profile_job_id(
    workspace_id: str,
    scan_id: str,
    proposal_fingerprint: str,
) -> str:
    workspace = _bounded_text(
        workspace_id,
        "semantic profile workspace",
        maximum_bytes=200,
    )
    if _SCAN_ID.fullmatch(scan_id) is None:
        raise ValueError("semantic profile scan id must be canonical")
    fingerprint = _sha256(
        proposal_fingerprint,
        "semantic profile proposal fingerprint",
    )
    payload = (f"semantic_join_profile_job_v1|{workspace}|{scan_id}|{fingerprint}").encode()
    return "profile_job_" + hashlib.sha256(payload).hexdigest()


def validate_semantic_join_profile_workspace_id(workspace_id: str) -> str:
    """Validate the tenant-local workspace half of one source binding."""

    return _bounded_text(
        workspace_id,
        "semantic profile workspace",
        maximum_bytes=200,
    )


def semantic_join_profile_source_matches(
    job: SemanticJoinProfileJob,
    *,
    expected_workspace_id: str,
    expected_connection_id: CatalogConnectionId,
) -> bool:
    """Return whether a job belongs to one exact worker source binding."""

    workspace_id = validate_semantic_join_profile_workspace_id(expected_workspace_id)
    connection_id = CatalogConnectionId(expected_connection_id.root)
    return (
        job.workspace_id == workspace_id
        and job.connection_id == connection_id
        and job.execution_target is not None
        and job.execution_target.workspace_id == workspace_id
        and job.execution_target.connection_id == connection_id
    )


def digest_semantic_join_profile_capability(capability: str) -> str:
    if (
        not isinstance(capability, str)
        or not 32 <= len(capability) <= 512
        or not capability.isascii()
        or not capability.isprintable()
        or len(set(capability)) < 8
    ):
        raise ValueError("semantic profile capability must be strong bounded printable ASCII")
    return hashlib.sha256(capability.encode()).hexdigest()


def classify_semantic_join_profile_failure(
    code: SemanticJoinProfileFailureCode,
    *,
    attempt: int,
    max_attempts: int,
) -> SemanticJoinProfileFailureDisposition:
    if not 1 <= attempt <= max_attempts <= 100:
        raise ValueError("semantic profile failure attempt bounds are invalid")
    if code in _RETRYABLE_FAILURES and attempt < max_attempts:
        return SemanticJoinProfileFailureDisposition.RETRY
    return SemanticJoinProfileFailureDisposition.FAIL


def semantic_join_profile_retry_delay(attempt: int) -> timedelta:
    if not 1 <= attempt <= 100:
        raise ValueError("semantic profile retry attempt is outside the supported bound")
    seconds = min(
        _INITIAL_RETRY_DELAY_SECONDS * (2 ** (attempt - 1)),
        _MAX_RETRY_DELAY_SECONDS,
    )
    return timedelta(seconds=seconds)


def claim_semantic_join_profile_job(
    job: SemanticJoinProfileJob,
    *,
    worker_id: str,
    lease_capability: str,
    claimed_at: datetime,
    lease_expires_at: datetime,
) -> SemanticJoinProfileJob:
    if job.status.is_terminal:
        raise _transition_error(
            SemanticJoinProfileTransitionErrorCode.TERMINAL_IMMUTABLE,
            "terminal semantic profile job is immutable",
        )
    if job.execution_target is None or job.connector_contract_version is None:
        raise _transition_error(
            SemanticJoinProfileTransitionErrorCode.INVALID_STATE,
            "semantic profile connector target is unavailable",
        )
    claimed = _transition_time(job, claimed_at, "semantic profile claim timestamp")
    if (
        job.status
        not in {
            SemanticJoinProfileJobStatus.REQUESTED,
            SemanticJoinProfileJobStatus.RETRY_WAIT,
        }
        or job.lease is not None
    ):
        raise _transition_error(
            SemanticJoinProfileTransitionErrorCode.INVALID_STATE,
            "semantic profile job is not claimable",
        )
    if claimed < job.available_at:
        raise _transition_error(
            SemanticJoinProfileTransitionErrorCode.NOT_AVAILABLE,
            "semantic profile job is not due",
        )
    if job.attempts >= job.max_attempts:
        raise _transition_error(
            SemanticJoinProfileTransitionErrorCode.ATTEMPTS_EXHAUSTED,
            "semantic profile job has exhausted attempts",
        )
    expires = _aware(lease_expires_at, "semantic profile lease expiry")
    if expires <= claimed or expires - claimed > _MAX_LEASE_DURATION:
        raise _transition_error(
            SemanticJoinProfileTransitionErrorCode.TEMPORAL_CONFLICT,
            "semantic profile lease duration is outside its bounded window",
        )
    attempt = job.attempts + 1
    lease = SemanticJoinProfileLease(
        job_id=job.job_id,
        worker_id=worker_id,
        capability_digest=digest_semantic_join_profile_capability(lease_capability),
        fencing_token=job.fencing_token + 1,
        attempt=attempt,
        acquired_at=claimed,
        heartbeat_at=claimed,
        expires_at=expires,
    )
    return _replace_job(
        job,
        status=SemanticJoinProfileJobStatus.LEASED,
        attempts=attempt,
        fencing_token=lease.fencing_token,
        updated_at=claimed,
        lease=lease,
        failure=None,
    )


def heartbeat_semantic_join_profile_job(
    job: SemanticJoinProfileJob,
    *,
    worker_id: str,
    lease_capability: str,
    fencing_token: int,
    heartbeat_at: datetime,
    lease_expires_at: datetime,
) -> SemanticJoinProfileJob:
    heartbeat = _transition_time(
        job,
        heartbeat_at,
        "semantic profile heartbeat timestamp",
    )
    lease = _require_active_lease(
        job,
        worker_id=worker_id,
        lease_capability=lease_capability,
        fencing_token=fencing_token,
        at=heartbeat,
    )
    expires = _aware(lease_expires_at, "semantic profile heartbeat expiry")
    if (
        expires <= lease.expires_at
        or expires <= heartbeat
        or expires - heartbeat > _MAX_LEASE_DURATION
    ):
        raise _transition_error(
            SemanticJoinProfileTransitionErrorCode.TEMPORAL_CONFLICT,
            "semantic profile heartbeat must extend the current lease",
        )
    return _replace_job(
        job,
        updated_at=heartbeat,
        lease=SemanticJoinProfileLease(
            job_id=lease.job_id,
            worker_id=lease.worker_id,
            capability_digest=lease.capability_digest,
            fencing_token=lease.fencing_token,
            attempt=lease.attempt,
            acquired_at=lease.acquired_at,
            heartbeat_at=heartbeat,
            expires_at=expires,
        ),
    )


def complete_semantic_join_profile_job(
    job: SemanticJoinProfileJob,
    *,
    worker_id: str,
    lease_capability: str,
    fencing_token: int,
    profile: RelationshipProfile,
    completed_at: datetime,
    retain_until: datetime,
) -> SemanticJoinProfileJob:
    completed = _transition_time(
        job,
        completed_at,
        "semantic profile completion timestamp",
    )
    _require_active_lease(
        job,
        worker_id=worker_id,
        lease_capability=lease_capability,
        fencing_token=fencing_token,
        at=completed,
    )
    retained = _retention_time(completed, retain_until)
    result = SemanticJoinProfileResult.create(profile, completed_at=completed)
    return _replace_job(
        job,
        status=SemanticJoinProfileJobStatus.COMPLETED,
        available_at=completed,
        updated_at=completed,
        lease=None,
        failure=None,
        result=result,
        completed_at=completed,
        retain_until=retained,
    )


def fail_semantic_join_profile_job(
    job: SemanticJoinProfileJob,
    *,
    worker_id: str,
    lease_capability: str,
    fencing_token: int,
    code: SemanticJoinProfileFailureCode,
    failed_at: datetime,
    retry_at: datetime | None,
    retain_until: datetime | None,
) -> SemanticJoinProfileJob:
    failed = _transition_time(job, failed_at, "semantic profile failure timestamp")
    _require_active_lease(
        job,
        worker_id=worker_id,
        lease_capability=lease_capability,
        fencing_token=fencing_token,
        at=failed,
    )
    disposition = classify_semantic_join_profile_failure(
        code,
        attempt=job.attempts,
        max_attempts=job.max_attempts,
    )
    failure = SemanticJoinProfileFailure(
        code=code,
        disposition=disposition,
        attempt=job.attempts,
        occurred_at=failed,
    )
    if disposition is SemanticJoinProfileFailureDisposition.RETRY:
        if retry_at is None or retain_until is not None:
            raise _transition_error(
                SemanticJoinProfileTransitionErrorCode.TEMPORAL_CONFLICT,
                "retryable semantic profile failure requires only retry time",
            )
        retry = _aware(retry_at, "semantic profile retry timestamp")
        if retry <= failed:
            raise _transition_error(
                SemanticJoinProfileTransitionErrorCode.TEMPORAL_CONFLICT,
                "semantic profile retry must be in the future",
            )
        return _replace_job(
            job,
            status=SemanticJoinProfileJobStatus.RETRY_WAIT,
            available_at=retry,
            updated_at=failed,
            lease=None,
            failure=failure,
        )
    if retry_at is not None or retain_until is None:
        raise _transition_error(
            SemanticJoinProfileTransitionErrorCode.TEMPORAL_CONFLICT,
            "terminal semantic profile failure requires only retention time",
        )
    retained = _retention_time(failed, retain_until)
    return _replace_job(
        job,
        status=SemanticJoinProfileJobStatus.FAILED,
        available_at=failed,
        updated_at=failed,
        lease=None,
        failure=failure,
        completed_at=failed,
        retain_until=retained,
    )


def reclaim_expired_semantic_join_profile_job(
    job: SemanticJoinProfileJob,
    *,
    reclaimed_at: datetime,
    retain_until: datetime | None,
) -> SemanticJoinProfileJob:
    reclaimed = _transition_time(
        job,
        reclaimed_at,
        "semantic profile lease reclaim timestamp",
    )
    if job.status is not SemanticJoinProfileJobStatus.LEASED or job.lease is None:
        raise _transition_error(
            SemanticJoinProfileTransitionErrorCode.INVALID_STATE,
            "semantic profile job has no lease to reclaim",
        )
    if reclaimed < job.lease.expires_at:
        raise _transition_error(
            SemanticJoinProfileTransitionErrorCode.LEASE_EXPIRED,
            "semantic profile lease is still current",
        )
    disposition = classify_semantic_join_profile_failure(
        SemanticJoinProfileFailureCode.LEASE_EXPIRED,
        attempt=job.attempts,
        max_attempts=job.max_attempts,
    )
    failure = SemanticJoinProfileFailure(
        code=SemanticJoinProfileFailureCode.LEASE_EXPIRED,
        disposition=disposition,
        attempt=job.attempts,
        occurred_at=reclaimed,
    )
    if disposition is SemanticJoinProfileFailureDisposition.RETRY:
        if retain_until is not None:
            raise _transition_error(
                SemanticJoinProfileTransitionErrorCode.TEMPORAL_CONFLICT,
                "retryable semantic profile reclaim cannot carry retention",
            )
        return _replace_job(
            job,
            status=SemanticJoinProfileJobStatus.RETRY_WAIT,
            available_at=reclaimed + semantic_join_profile_retry_delay(job.attempts),
            updated_at=reclaimed,
            lease=None,
            failure=failure,
        )
    if retain_until is None:
        raise _transition_error(
            SemanticJoinProfileTransitionErrorCode.TEMPORAL_CONFLICT,
            "exhausted semantic profile reclaim requires retention",
        )
    retained = _retention_time(reclaimed, retain_until)
    return _replace_job(
        job,
        status=SemanticJoinProfileJobStatus.FAILED,
        available_at=reclaimed,
        updated_at=reclaimed,
        lease=None,
        failure=failure,
        completed_at=reclaimed,
        retain_until=retained,
    )


def semantic_join_profile_claim_matches(
    job: SemanticJoinProfileJob,
    *,
    worker_id: str,
    lease_capability: str,
    fencing_token: int,
    at: datetime,
) -> bool:
    try:
        _require_active_lease(
            job,
            worker_id=worker_id,
            lease_capability=lease_capability,
            fencing_token=fencing_token,
            at=at,
        )
    except SemanticJoinProfileTransitionError:
        return False
    return True


def _require_active_lease(
    job: SemanticJoinProfileJob,
    *,
    worker_id: str,
    lease_capability: str,
    fencing_token: int,
    at: datetime,
) -> SemanticJoinProfileLease:
    if job.status is not SemanticJoinProfileJobStatus.LEASED or job.lease is None:
        raise _transition_error(
            SemanticJoinProfileTransitionErrorCode.INVALID_STATE,
            "semantic profile job does not have an active lease",
        )
    lease = job.lease
    if lease.worker_id != worker_id or not hmac.compare_digest(
        lease.capability_digest,
        digest_semantic_join_profile_capability(lease_capability),
    ):
        raise _transition_error(
            SemanticJoinProfileTransitionErrorCode.LEASE_MISMATCH,
            "semantic profile lease ownership does not match",
        )
    if lease.fencing_token != fencing_token:
        raise _transition_error(
            SemanticJoinProfileTransitionErrorCode.FENCING_MISMATCH,
            "semantic profile fencing token does not match",
        )
    if not lease.is_current(at):
        raise _transition_error(
            SemanticJoinProfileTransitionErrorCode.LEASE_EXPIRED,
            "semantic profile lease is no longer current",
        )
    return lease


def _replace_job(
    job: SemanticJoinProfileJob,
    **changes: object,
) -> SemanticJoinProfileJob:
    if job.status.is_terminal:
        raise _transition_error(
            SemanticJoinProfileTransitionErrorCode.TERMINAL_IMMUTABLE,
            "terminal semantic profile job is immutable",
        )
    return SemanticJoinProfileJob.model_validate(
        {
            **job.model_dump(mode="python"),
            **changes,
        }
    )


def _transition_time(
    job: SemanticJoinProfileJob,
    value: datetime,
    label: str,
) -> datetime:
    instant = _aware(value, label)
    if instant <= job.updated_at:
        raise _transition_error(
            SemanticJoinProfileTransitionErrorCode.TEMPORAL_CONFLICT,
            f"{label} must advance durable time",
        )
    return instant


def _retention_time(completed_at: datetime, value: datetime) -> datetime:
    retained = _aware(value, "semantic profile retention timestamp")
    if retained < completed_at or retained - completed_at > _MAX_RETENTION:
        raise _transition_error(
            SemanticJoinProfileTransitionErrorCode.TEMPORAL_CONFLICT,
            "semantic profile retention window is invalid",
        )
    return retained


def _transition_error(
    code: SemanticJoinProfileTransitionErrorCode,
    message: str,
) -> SemanticJoinProfileTransitionError:
    return SemanticJoinProfileTransitionError(code, message)


def _profile_job_id(value: str) -> str:
    if _JOB_ID.fullmatch(value) is None:
        raise ValueError("semantic profile job id must be canonical")
    return value


def _sha256(value: str, label: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


def _inert_id(value: str, label: str) -> str:
    if _INERT_ID.fullmatch(value) is None:
        raise ValueError(f"{label} must be a bounded inert identifier")
    return value


def _bounded_text(value: str, label: str, *, maximum_bytes: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value.strip() != value
        or len(value.encode("utf-8")) > maximum_bytes
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{label} must be bounded nonblank text")
    return value


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _assert_no_protected_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).casefold()
            if normalized in _PROTECTED_KEYS or normalized.endswith("_api_key"):
                raise ValueError("semantic profile payload contains a protected key")
            _assert_no_protected_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_no_protected_keys(nested)


__all__ = [
    "SemanticJoinProfileFailure",
    "SemanticJoinProfileFailureCode",
    "SemanticJoinProfileFailureDisposition",
    "SemanticJoinProfileJob",
    "SemanticJoinProfileJobStatus",
    "SemanticJoinProfileLease",
    "SemanticJoinProfileProposal",
    "SemanticJoinProfileResult",
    "SemanticJoinProfileSubmission",
    "SemanticJoinProfileTargetRef",
    "SemanticJoinProfileTransitionError",
    "SemanticJoinProfileTransitionErrorCode",
    "canonical_semantic_join_profile_proposal_json",
    "claim_semantic_join_profile_job",
    "classify_semantic_join_profile_failure",
    "complete_semantic_join_profile_job",
    "digest_semantic_join_profile_capability",
    "fail_semantic_join_profile_job",
    "heartbeat_semantic_join_profile_job",
    "reclaim_expired_semantic_join_profile_job",
    "semantic_join_profile_claim_matches",
    "semantic_join_profile_job_id",
    "semantic_join_profile_proposal_fingerprint",
    "semantic_join_profile_result_fingerprint",
    "semantic_join_profile_retry_delay",
    "semantic_join_profile_source_matches",
    "validate_semantic_join_profile_proposal",
    "validate_semantic_join_profile_result",
    "validate_semantic_join_profile_workspace_id",
]
