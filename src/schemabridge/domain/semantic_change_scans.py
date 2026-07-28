"""Pure lifecycle contracts for durable semantic-change scan requests."""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SCAN_ID = re.compile(r"^scan_[0-9a-f]{64}$")
_REPORT_ID = re.compile(r"^report_[0-9a-f]{64}$")
_INERT_ID = re.compile(r"^[a-z][a-z0-9_-]{2,199}$")
_REGISTRY_ID = re.compile(r"^[a-z][a-z0-9_]{2,79}$")
_SCOPE = re.compile(r"^[a-z][a-z0-9_.:-]{2,119}$")
_EVENT_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,499}$")
_MAX_LEASE_DURATION = timedelta(minutes=5)
_MAX_RETENTION = timedelta(days=366)
_INITIAL_RETRY_DELAY_SECONDS = 5
_MAX_RETRY_DELAY_SECONDS = 300


class SemanticChangeScanSourceKind(StrEnum):
    """Closed durable events that can request semantic reconciliation."""

    CATALOG_GENERATION = "catalog_generation"
    REGISTRY_POINTER = "registry_pointer"


class SemanticChangeScanStatus(StrEnum):
    """Closed lifecycle persisted by ``semantic_change_scan_requests``."""

    REQUESTED = "requested"
    LEASED = "leased"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    FAILED = "failed"
    SUPERSEDED = "superseded"

    @property
    def is_terminal(self) -> bool:
        return self in {
            SemanticChangeScanStatus.COMPLETED,
            SemanticChangeScanStatus.FAILED,
            SemanticChangeScanStatus.SUPERSEDED,
        }


class SemanticChangeScanFailureCode(StrEnum):
    """Sanitized failure facts; exception text is never durable scan state."""

    REGISTRY_UNAVAILABLE = "registry_unavailable"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"
    DEPENDENCY_INDEX_INCOMPLETE = "dependency_index_incomplete"
    SOURCE_TIMEOUT = "source_timeout"
    LEASE_EXPIRED = "lease_expired"
    SHUTDOWN_REQUESTED = "shutdown_requested"
    INSPECTION_INVALID = "inspection_invalid"
    INSPECTION_SCOPE_MISMATCH = "inspection_scope_mismatch"
    UNEXPECTED_RECONCILER_FAILURE = "unexpected_reconciler_failure"


class SemanticChangeScanFailureDisposition(StrEnum):
    """Whether a sanitized failure remains eligible for another attempt."""

    RETRY = "retry"
    FAIL = "fail"


_RETRYABLE_FAILURES = frozenset(
    {
        SemanticChangeScanFailureCode.REGISTRY_UNAVAILABLE,
        SemanticChangeScanFailureCode.EVIDENCE_UNAVAILABLE,
        SemanticChangeScanFailureCode.DEPENDENCY_INDEX_INCOMPLETE,
        SemanticChangeScanFailureCode.SOURCE_TIMEOUT,
        SemanticChangeScanFailureCode.LEASE_EXPIRED,
        SemanticChangeScanFailureCode.SHUTDOWN_REQUESTED,
    }
)


class SemanticChangeScanTransitionErrorCode(StrEnum):
    """Stable pure-domain lifecycle failures."""

    INVALID_STATE = "semantic_scan_invalid_state"
    NOT_AVAILABLE = "semantic_scan_not_available"
    ATTEMPTS_EXHAUSTED = "semantic_scan_attempts_exhausted"
    LEASE_MISMATCH = "semantic_scan_lease_mismatch"
    LEASE_EXPIRED = "semantic_scan_lease_expired"
    FENCING_MISMATCH = "semantic_scan_fencing_mismatch"
    TEMPORAL_CONFLICT = "semantic_scan_temporal_conflict"
    TERMINAL_IMMUTABLE = "semantic_scan_terminal_immutable"
    RESULT_MISMATCH = "semantic_scan_result_mismatch"
    SUPERSESSION_MISMATCH = "semantic_scan_supersession_mismatch"


class SemanticChangeScanTransitionError(ValueError):
    """Typed deterministic rejection raised before durable mutation."""

    def __init__(
        self,
        code: SemanticChangeScanTransitionErrorCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


class SemanticChangeScanLease(FrozenDomainModel):
    """Exact transient ownership represented durably only by a capability digest."""

    scan_id: str
    reconciler_id: str
    capability_digest: str
    fencing_token: int = Field(ge=1)
    attempt: int = Field(ge=1, le=100)
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime

    @field_validator("scan_id")
    @classmethod
    def scan_id_must_be_canonical(cls, value: str) -> str:
        return _scan_id(value)

    @field_validator("reconciler_id")
    @classmethod
    def reconciler_id_must_be_inert(cls, value: str) -> str:
        return _inert_id(value, "semantic reconciler identifier")

    @field_validator("capability_digest")
    @classmethod
    def capability_must_be_digest_only(cls, value: str) -> str:
        return _sha256(value, "semantic scan capability digest")

    @field_validator("acquired_at", "heartbeat_at", "expires_at")
    @classmethod
    def lease_times_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "semantic scan lease timestamp")

    @model_validator(mode="after")
    def lease_window_must_be_bounded(self) -> SemanticChangeScanLease:
        if not self.acquired_at <= self.heartbeat_at < self.expires_at:
            raise ValueError("semantic scan lease times are not ordered")
        if self.expires_at - self.heartbeat_at > _MAX_LEASE_DURATION:
            raise ValueError("semantic scan lease exceeds its maximum duration")
        if self.fencing_token != self.attempt:
            raise ValueError("semantic scan fence must equal its monotonic attempt")
        return self

    def is_current(self, at: datetime) -> bool:
        instant = _aware(at, "semantic scan lease check")
        return self.acquired_at <= instant < self.expires_at


class SemanticChangeScanFailure(FrozenDomainModel):
    """Sanitized durable failure classification without infrastructure detail."""

    code: SemanticChangeScanFailureCode
    disposition: SemanticChangeScanFailureDisposition
    attempt: int = Field(ge=1, le=100)
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def failure_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "semantic scan failure timestamp")


class SemanticChangeScanCompletion(FrozenDomainModel):
    """Exact immutable report identity committed atomically with scan completion."""

    report_id: str
    report_fingerprint: str
    completed_at: datetime

    @field_validator("report_id")
    @classmethod
    def report_id_must_be_canonical(cls, value: str) -> str:
        if _REPORT_ID.fullmatch(value) is None:
            raise ValueError("semantic scan report id must be canonical")
        return value

    @field_validator("report_fingerprint")
    @classmethod
    def report_fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "semantic scan report fingerprint")

    @field_validator("completed_at")
    @classmethod
    def completion_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "semantic scan completion timestamp")


class SemanticChangeScanSupersession(FrozenDomainModel):
    """Exact newer request that made this request obsolete."""

    scan_id: str
    source_fingerprint: str

    @field_validator("scan_id")
    @classmethod
    def scan_id_must_be_canonical(cls, value: str) -> str:
        return _scan_id(value)

    @field_validator("source_fingerprint")
    @classmethod
    def source_fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "semantic scan superseding fingerprint")


class SemanticChangeScanRequest(FrozenDomainModel):
    """Complete immutable view of one durable scan-request lifecycle state."""

    scan_id: str
    workspace_id: str = Field(min_length=1, max_length=200)
    source_kind: SemanticChangeScanSourceKind
    source_event_key: str = Field(min_length=1, max_length=500)
    source_fingerprint: str
    catalog_scope: str | None = Field(default=None, min_length=3, max_length=120)
    registry_id: str | None = Field(default=None, min_length=3, max_length=80)
    registry_generation: int | None = Field(default=None, ge=1)
    connection_id: str | None = Field(default=None, min_length=3, max_length=200)
    base_catalog_generation: int | None = Field(default=None, ge=1)
    observed_catalog_generation: int | None = Field(default=None, ge=1)
    status: SemanticChangeScanStatus
    max_attempts: int = Field(default=5, ge=1, le=100)
    attempts: int = Field(default=0, ge=0, le=100)
    available_at: datetime
    fencing_token: int = Field(default=0, ge=0)
    lease: SemanticChangeScanLease | None = None
    failure: SemanticChangeScanFailure | None = None
    requested_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    retain_until: datetime | None = None
    completion: SemanticChangeScanCompletion | None = None
    superseded_by: SemanticChangeScanSupersession | None = None

    @field_validator("scan_id")
    @classmethod
    def scan_id_must_be_canonical(cls, value: str) -> str:
        return _scan_id(value)

    @field_validator("workspace_id")
    @classmethod
    def workspace_must_be_bounded(cls, value: str) -> str:
        return _bounded_text(value, "semantic scan workspace", maximum_bytes=200)

    @field_validator("source_event_key")
    @classmethod
    def event_key_must_be_inert(cls, value: str) -> str:
        if _EVENT_KEY.fullmatch(value) is None:
            raise ValueError("semantic scan source event key must be inert")
        return value

    @field_validator("source_fingerprint")
    @classmethod
    def source_fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "semantic scan source fingerprint")

    @field_validator("catalog_scope")
    @classmethod
    def catalog_scope_must_be_inert(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _inert_scope(value, "semantic scan catalog scope")

    @field_validator("registry_id")
    @classmethod
    def registry_id_must_be_inert(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if _REGISTRY_ID.fullmatch(value) is None:
            raise ValueError("semantic scan registry id must be inert")
        return value

    @field_validator("connection_id")
    @classmethod
    def connection_id_must_be_inert(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _inert_id(value, "semantic scan connection identifier")

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
        return _aware(value, "semantic scan lifecycle timestamp")

    @model_validator(mode="after")
    def lifecycle_and_source_shape_must_be_exact(self) -> SemanticChangeScanRequest:
        if self.scan_id != f"scan_{self.source_fingerprint}":
            raise ValueError("semantic scan id must derive from its source fingerprint")
        if self.attempts > self.max_attempts:
            raise ValueError("semantic scan attempts exceed the durable maximum")
        if self.fencing_token != self.attempts:
            raise ValueError("semantic scan fence must advance exactly once per attempt")
        if self.updated_at < self.requested_at or self.available_at < self.requested_at:
            raise ValueError("semantic scan lifecycle times precede its request")
        self._validate_source_shape()
        self._validate_terminal_times()

        if self.lease is not None and (
            self.lease.scan_id != self.scan_id
            or self.lease.attempt != self.attempts
            or self.lease.fencing_token != self.fencing_token
            or self.lease.heartbeat_at != self.updated_at
        ):
            raise ValueError("semantic scan lease does not match durable owner state")
        if self.failure is not None and (
            self.failure.attempt != self.attempts
            or self.failure.occurred_at != self.updated_at
            or self.failure.disposition
            is not classify_semantic_change_scan_failure(
                self.failure.code,
                attempt=self.attempts,
                max_attempts=self.max_attempts,
            )
        ):
            raise ValueError("semantic scan failure does not match its retry policy")

        empty_terminal = (
            self.completed_at is None
            and self.retain_until is None
            and self.completion is None
            and self.superseded_by is None
        )
        if self.status is SemanticChangeScanStatus.REQUESTED:
            self._require(
                self.attempts == 0
                and self.fencing_token == 0
                and self.lease is None
                and self.failure is None
                and empty_terminal,
                "requested semantic scan state is inconsistent",
            )
        elif self.status is SemanticChangeScanStatus.LEASED:
            self._require(
                self.attempts >= 1
                and self.lease is not None
                and self.failure is None
                and empty_terminal,
                "leased semantic scan state is inconsistent",
            )
        elif self.status is SemanticChangeScanStatus.RETRY_WAIT:
            self._require(
                self.attempts >= 1
                and self.lease is None
                and self.failure is not None
                and self.failure.disposition is SemanticChangeScanFailureDisposition.RETRY
                and empty_terminal,
                "retry-wait semantic scan state is inconsistent",
            )
        elif self.status is SemanticChangeScanStatus.COMPLETED:
            self._require(
                self.lease is None
                and self.failure is None
                and self.completion is not None
                and self.superseded_by is None,
                "completed semantic scan state is inconsistent",
            )
            assert self.completion is not None
            self._require(
                self.completion.completed_at == self.completed_at,
                "semantic scan completion does not match terminal time",
            )
        elif self.status is SemanticChangeScanStatus.FAILED:
            self._require(
                self.lease is None
                and self.failure is not None
                and self.failure.disposition is SemanticChangeScanFailureDisposition.FAIL
                and self.completion is None
                and self.superseded_by is None,
                "failed semantic scan state is inconsistent",
            )
        else:
            self._require(
                self.status is SemanticChangeScanStatus.SUPERSEDED
                and self.lease is None
                and self.failure is None
                and self.completion is None
                and self.superseded_by is not None,
                "superseded semantic scan state is inconsistent",
            )
            assert self.superseded_by is not None
            self._require(
                self.superseded_by.scan_id != self.scan_id
                and self.superseded_by.source_fingerprint != self.source_fingerprint,
                "semantic scan cannot supersede itself",
            )
        return self

    def _validate_source_shape(self) -> None:
        if self.source_kind is SemanticChangeScanSourceKind.CATALOG_GENERATION:
            valid = (
                self.catalog_scope is not None
                and self.registry_id is not None
                and self.registry_generation is None
                and self.connection_id is not None
                and self.observed_catalog_generation is not None
                and (
                    self.base_catalog_generation is None
                    or self.observed_catalog_generation > self.base_catalog_generation
                )
            )
        else:
            valid = (
                self.catalog_scope is not None
                and self.registry_id is not None
                and self.registry_generation is not None
                and self.connection_id is None
                and self.base_catalog_generation is None
                and self.observed_catalog_generation is None
            )
        if not valid:
            raise ValueError("semantic scan source fields do not match its trigger kind")

    def _validate_terminal_times(self) -> None:
        if self.status.is_terminal:
            if (
                self.completed_at is None
                or self.retain_until is None
                or self.updated_at != self.completed_at
                or not self.completed_at <= self.retain_until
                or self.retain_until - self.completed_at > _MAX_RETENTION
            ):
                raise ValueError("semantic scan terminal retention window is invalid")
        elif self.completed_at is not None or self.retain_until is not None:
            raise ValueError("non-terminal semantic scan cannot carry terminal times")

    @staticmethod
    def _require(condition: bool, message: str) -> None:
        if not condition:
            raise ValueError(message)

    @classmethod
    def requested(
        cls,
        *,
        workspace_id: str,
        source_kind: SemanticChangeScanSourceKind,
        source_event_key: str,
        source_fingerprint: str,
        requested_at: datetime,
        max_attempts: int = 5,
        catalog_scope: str | None = None,
        registry_id: str | None = None,
        registry_generation: int | None = None,
        connection_id: str | None = None,
        base_catalog_generation: int | None = None,
        observed_catalog_generation: int | None = None,
    ) -> SemanticChangeScanRequest:
        return cls(
            scan_id=f"scan_{source_fingerprint}",
            workspace_id=workspace_id,
            source_kind=source_kind,
            source_event_key=source_event_key,
            source_fingerprint=source_fingerprint,
            catalog_scope=catalog_scope,
            registry_id=registry_id,
            registry_generation=registry_generation,
            connection_id=connection_id,
            base_catalog_generation=base_catalog_generation,
            observed_catalog_generation=observed_catalog_generation,
            status=SemanticChangeScanStatus.REQUESTED,
            max_attempts=max_attempts,
            attempts=0,
            available_at=requested_at,
            fencing_token=0,
            requested_at=requested_at,
            updated_at=requested_at,
        )

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal


def classify_semantic_change_scan_failure(
    code: SemanticChangeScanFailureCode,
    *,
    attempt: int,
    max_attempts: int,
) -> SemanticChangeScanFailureDisposition:
    """Apply the closed finite retry allowlist."""

    if not 1 <= attempt <= max_attempts <= 100:
        raise ValueError("semantic scan failure attempt bounds are invalid")
    if code in _RETRYABLE_FAILURES and attempt < max_attempts:
        return SemanticChangeScanFailureDisposition.RETRY
    return SemanticChangeScanFailureDisposition.FAIL


def semantic_change_scan_retry_delay(attempt: int) -> timedelta:
    """Return deterministic exponential backoff capped at five minutes."""

    if not 1 <= attempt <= 100:
        raise ValueError("semantic scan retry attempt is outside the supported bound")
    seconds = min(
        _INITIAL_RETRY_DELAY_SECONDS * (2 ** (attempt - 1)),
        _MAX_RETRY_DELAY_SECONDS,
    )
    return timedelta(seconds=seconds)


def claim_semantic_change_scan(
    request: SemanticChangeScanRequest,
    *,
    reconciler_id: str,
    lease_capability: str,
    claimed_at: datetime,
    lease_expires_at: datetime,
) -> SemanticChangeScanRequest:
    """Claim one due waiting request and advance its exact monotonic fence."""

    if request.is_terminal:
        _raise_terminal()
    if request.status not in {
        SemanticChangeScanStatus.REQUESTED,
        SemanticChangeScanStatus.RETRY_WAIT,
    }:
        raise SemanticChangeScanTransitionError(
            SemanticChangeScanTransitionErrorCode.INVALID_STATE,
            "semantic scan cannot be claimed from its current state",
        )
    claimed = _transition_time(request, claimed_at, "semantic scan claim timestamp")
    if claimed < request.available_at:
        raise SemanticChangeScanTransitionError(
            SemanticChangeScanTransitionErrorCode.NOT_AVAILABLE,
            "semantic scan is not due for delivery",
        )
    if request.attempts >= request.max_attempts:
        raise SemanticChangeScanTransitionError(
            SemanticChangeScanTransitionErrorCode.ATTEMPTS_EXHAUSTED,
            "semantic scan has no remaining delivery attempt",
        )
    expires = _aware(lease_expires_at, "semantic scan lease expiry")
    if expires <= claimed or expires - claimed > _MAX_LEASE_DURATION:
        raise SemanticChangeScanTransitionError(
            SemanticChangeScanTransitionErrorCode.TEMPORAL_CONFLICT,
            "semantic scan lease duration is outside its bounded window",
        )
    attempt = request.attempts + 1
    lease = SemanticChangeScanLease(
        scan_id=request.scan_id,
        reconciler_id=reconciler_id,
        capability_digest=digest_semantic_change_scan_capability(lease_capability),
        fencing_token=request.fencing_token + 1,
        attempt=attempt,
        acquired_at=claimed,
        heartbeat_at=claimed,
        expires_at=expires,
    )
    return _replace_request(
        request,
        status=SemanticChangeScanStatus.LEASED,
        attempts=attempt,
        fencing_token=lease.fencing_token,
        updated_at=claimed,
        lease=lease,
        failure=None,
    )


def heartbeat_semantic_change_scan(
    request: SemanticChangeScanRequest,
    *,
    reconciler_id: str,
    lease_capability: str,
    fencing_token: int,
    heartbeat_at: datetime,
    lease_expires_at: datetime,
) -> SemanticChangeScanRequest:
    """Extend only the exact current unexpired lease."""

    heartbeat = _transition_time(
        request,
        heartbeat_at,
        "semantic scan heartbeat timestamp",
    )
    lease = _require_active_lease(
        request,
        reconciler_id=reconciler_id,
        lease_capability=lease_capability,
        fencing_token=fencing_token,
        at=heartbeat,
    )
    expires = _aware(lease_expires_at, "semantic scan heartbeat expiry")
    if (
        expires <= lease.expires_at
        or expires <= heartbeat
        or expires - heartbeat > _MAX_LEASE_DURATION
    ):
        raise SemanticChangeScanTransitionError(
            SemanticChangeScanTransitionErrorCode.TEMPORAL_CONFLICT,
            "semantic scan heartbeat must extend the current lease",
        )
    return _replace_request(
        request,
        updated_at=heartbeat,
        lease=SemanticChangeScanLease(
            scan_id=lease.scan_id,
            reconciler_id=lease.reconciler_id,
            capability_digest=lease.capability_digest,
            fencing_token=lease.fencing_token,
            attempt=lease.attempt,
            acquired_at=lease.acquired_at,
            heartbeat_at=heartbeat,
            expires_at=expires,
        ),
    )


def complete_semantic_change_scan(
    request: SemanticChangeScanRequest,
    *,
    reconciler_id: str,
    lease_capability: str,
    fencing_token: int,
    completion: SemanticChangeScanCompletion,
    retain_until: datetime,
) -> SemanticChangeScanRequest:
    """Complete one exact inspection; exact terminal replay is idempotent."""

    if request.status is SemanticChangeScanStatus.COMPLETED:
        if request.completion == completion:
            return request
        _raise_terminal()
    if request.is_terminal:
        _raise_terminal()
    completed = _transition_time(
        request,
        completion.completed_at,
        "semantic scan completion timestamp",
    )
    _require_active_lease(
        request,
        reconciler_id=reconciler_id,
        lease_capability=lease_capability,
        fencing_token=fencing_token,
        at=completed,
    )
    retention = _retention_time(completed, retain_until)
    return _replace_request(
        request,
        status=SemanticChangeScanStatus.COMPLETED,
        updated_at=completed,
        lease=None,
        failure=None,
        completed_at=completed,
        retain_until=retention,
        completion=completion,
        superseded_by=None,
    )


def fail_semantic_change_scan(
    request: SemanticChangeScanRequest,
    *,
    reconciler_id: str,
    lease_capability: str,
    fencing_token: int,
    code: SemanticChangeScanFailureCode,
    failed_at: datetime,
    retry_at: datetime | None = None,
    retain_until: datetime | None = None,
) -> SemanticChangeScanRequest:
    """Schedule a deterministic retry or close one exact leased request."""

    if request.is_terminal:
        _raise_terminal()
    failed = _transition_time(request, failed_at, "semantic scan failure timestamp")
    _require_active_lease(
        request,
        reconciler_id=reconciler_id,
        lease_capability=lease_capability,
        fencing_token=fencing_token,
        at=failed,
    )
    disposition = classify_semantic_change_scan_failure(
        code,
        attempt=request.attempts,
        max_attempts=request.max_attempts,
    )
    failure = SemanticChangeScanFailure(
        code=code,
        disposition=disposition,
        attempt=request.attempts,
        occurred_at=failed,
    )
    if disposition is SemanticChangeScanFailureDisposition.RETRY:
        expected_retry = failed + semantic_change_scan_retry_delay(request.attempts)
        if retry_at is None or _aware(retry_at, "semantic scan retry timestamp") != expected_retry:
            raise SemanticChangeScanTransitionError(
                SemanticChangeScanTransitionErrorCode.TEMPORAL_CONFLICT,
                "semantic scan retry time does not match deterministic backoff",
            )
        if retain_until is not None:
            raise SemanticChangeScanTransitionError(
                SemanticChangeScanTransitionErrorCode.TEMPORAL_CONFLICT,
                "retryable semantic scan failure cannot carry retention",
            )
        return _replace_request(
            request,
            status=SemanticChangeScanStatus.RETRY_WAIT,
            available_at=expected_retry,
            updated_at=failed,
            lease=None,
            failure=failure,
        )
    if retry_at is not None or retain_until is None:
        raise SemanticChangeScanTransitionError(
            SemanticChangeScanTransitionErrorCode.TEMPORAL_CONFLICT,
            "terminal semantic scan failure requires only a retention deadline",
        )
    retention = _retention_time(failed, retain_until)
    return _replace_request(
        request,
        status=SemanticChangeScanStatus.FAILED,
        updated_at=failed,
        lease=None,
        failure=failure,
        completed_at=failed,
        retain_until=retention,
    )


def reclaim_expired_semantic_change_scan(
    request: SemanticChangeScanRequest,
    *,
    reclaimed_at: datetime,
    retain_until: datetime | None = None,
) -> SemanticChangeScanRequest:
    """Recover an abandoned lease without accepting a stale owner transition."""

    if request.is_terminal:
        _raise_terminal()
    if request.status is not SemanticChangeScanStatus.LEASED or request.lease is None:
        raise SemanticChangeScanTransitionError(
            SemanticChangeScanTransitionErrorCode.INVALID_STATE,
            "semantic scan has no leased attempt to reclaim",
        )
    reclaimed = _transition_time(
        request,
        reclaimed_at,
        "semantic scan lease reclaim timestamp",
    )
    if reclaimed < request.lease.expires_at:
        raise SemanticChangeScanTransitionError(
            SemanticChangeScanTransitionErrorCode.NOT_AVAILABLE,
            "semantic scan lease is still current",
        )
    disposition = classify_semantic_change_scan_failure(
        SemanticChangeScanFailureCode.LEASE_EXPIRED,
        attempt=request.attempts,
        max_attempts=request.max_attempts,
    )
    failure = SemanticChangeScanFailure(
        code=SemanticChangeScanFailureCode.LEASE_EXPIRED,
        disposition=disposition,
        attempt=request.attempts,
        occurred_at=reclaimed,
    )
    if disposition is SemanticChangeScanFailureDisposition.RETRY:
        if retain_until is not None:
            raise SemanticChangeScanTransitionError(
                SemanticChangeScanTransitionErrorCode.TEMPORAL_CONFLICT,
                "reclaimed retry cannot carry retention",
            )
        return _replace_request(
            request,
            status=SemanticChangeScanStatus.RETRY_WAIT,
            available_at=reclaimed + semantic_change_scan_retry_delay(request.attempts),
            updated_at=reclaimed,
            lease=None,
            failure=failure,
        )
    if retain_until is None:
        raise SemanticChangeScanTransitionError(
            SemanticChangeScanTransitionErrorCode.TEMPORAL_CONFLICT,
            "exhausted semantic scan requires a retention deadline",
        )
    return _replace_request(
        request,
        status=SemanticChangeScanStatus.FAILED,
        updated_at=reclaimed,
        lease=None,
        failure=failure,
        completed_at=reclaimed,
        retain_until=_retention_time(reclaimed, retain_until),
    )


def supersede_semantic_change_scan(
    request: SemanticChangeScanRequest,
    *,
    superseded_by: SemanticChangeScanSupersession,
    superseded_at: datetime,
    retain_until: datetime,
    reconciler_id: str | None = None,
    lease_capability: str | None = None,
    fencing_token: int | None = None,
) -> SemanticChangeScanRequest:
    """Terminally bind an obsolete request to one exact newer durable request."""

    if request.status is SemanticChangeScanStatus.SUPERSEDED:
        if request.superseded_by == superseded_by:
            return request
        _raise_terminal()
    if request.is_terminal:
        _raise_terminal()
    if (
        superseded_by.scan_id == request.scan_id
        or superseded_by.source_fingerprint == request.source_fingerprint
    ):
        raise SemanticChangeScanTransitionError(
            SemanticChangeScanTransitionErrorCode.SUPERSESSION_MISMATCH,
            "semantic scan cannot be superseded by itself",
        )
    superseded = _transition_time(
        request,
        superseded_at,
        "semantic scan supersession timestamp",
    )
    if request.status is SemanticChangeScanStatus.LEASED:
        if reconciler_id is None or lease_capability is None or fencing_token is None:
            raise SemanticChangeScanTransitionError(
                SemanticChangeScanTransitionErrorCode.LEASE_MISMATCH,
                "leased semantic scan supersession requires exact ownership",
            )
        _require_active_lease(
            request,
            reconciler_id=reconciler_id,
            lease_capability=lease_capability,
            fencing_token=fencing_token,
            at=superseded,
        )
    elif request.status not in {
        SemanticChangeScanStatus.REQUESTED,
        SemanticChangeScanStatus.RETRY_WAIT,
    }:
        raise SemanticChangeScanTransitionError(
            SemanticChangeScanTransitionErrorCode.INVALID_STATE,
            "semantic scan cannot be superseded from its current state",
        )
    return _replace_request(
        request,
        status=SemanticChangeScanStatus.SUPERSEDED,
        updated_at=superseded,
        lease=None,
        failure=None,
        completed_at=superseded,
        retain_until=_retention_time(superseded, retain_until),
        completion=None,
        superseded_by=superseded_by,
    )


def digest_semantic_change_scan_capability(capability: str) -> str:
    """Hash one high-entropy capability before any durable comparison or storage."""

    if (
        not 32 <= len(capability) <= 1_024
        or not capability.isascii()
        or not capability.isprintable()
        or len(set(capability.encode())) < 8
    ):
        raise ValueError("semantic scan capability must be bounded high-entropy printable ASCII")
    return hashlib.sha256(capability.encode()).hexdigest()


def semantic_change_scan_claim_matches(
    request: SemanticChangeScanRequest,
    *,
    reconciler_id: str,
    lease_capability: str,
    fencing_token: int,
    at: datetime,
) -> bool:
    """Return whether an externally loaded request retains the exact live lease."""

    try:
        _require_active_lease(
            request,
            reconciler_id=reconciler_id,
            lease_capability=lease_capability,
            fencing_token=fencing_token,
            at=at,
        )
    except (SemanticChangeScanTransitionError, ValueError):
        return False
    return True


def _require_active_lease(
    request: SemanticChangeScanRequest,
    *,
    reconciler_id: str,
    lease_capability: str,
    fencing_token: int,
    at: datetime,
) -> SemanticChangeScanLease:
    if request.status is not SemanticChangeScanStatus.LEASED or request.lease is None:
        raise SemanticChangeScanTransitionError(
            SemanticChangeScanTransitionErrorCode.INVALID_STATE,
            "semantic scan does not have an active lease",
        )
    lease = request.lease
    capability_digest = digest_semantic_change_scan_capability(lease_capability)
    if lease.reconciler_id != reconciler_id or not hmac.compare_digest(
        lease.capability_digest,
        capability_digest,
    ):
        raise SemanticChangeScanTransitionError(
            SemanticChangeScanTransitionErrorCode.LEASE_MISMATCH,
            "semantic scan lease ownership does not match",
        )
    if lease.fencing_token != fencing_token:
        raise SemanticChangeScanTransitionError(
            SemanticChangeScanTransitionErrorCode.FENCING_MISMATCH,
            "semantic scan fencing token is stale",
        )
    if not lease.is_current(at):
        raise SemanticChangeScanTransitionError(
            SemanticChangeScanTransitionErrorCode.LEASE_EXPIRED,
            "semantic scan lease is no longer current",
        )
    return lease


def _replace_request(
    request: SemanticChangeScanRequest,
    **updates: object,
) -> SemanticChangeScanRequest:
    payload = request.model_dump(mode="python")
    payload.update(updates)
    return SemanticChangeScanRequest.model_validate(payload)


def _transition_time(
    request: SemanticChangeScanRequest,
    value: datetime,
    label: str,
) -> datetime:
    instant = _aware(value, label)
    if instant <= request.updated_at:
        raise SemanticChangeScanTransitionError(
            SemanticChangeScanTransitionErrorCode.TEMPORAL_CONFLICT,
            f"{label} must advance the semantic scan state",
        )
    return instant


def _retention_time(completed_at: datetime, retain_until: datetime) -> datetime:
    retention = _aware(retain_until, "semantic scan retention timestamp")
    if retention < completed_at or retention - completed_at > _MAX_RETENTION:
        raise SemanticChangeScanTransitionError(
            SemanticChangeScanTransitionErrorCode.TEMPORAL_CONFLICT,
            "semantic scan retention window is invalid",
        )
    return retention


def _raise_terminal() -> None:
    raise SemanticChangeScanTransitionError(
        SemanticChangeScanTransitionErrorCode.TERMINAL_IMMUTABLE,
        "terminal semantic scan state is immutable",
    )


def _scan_id(value: str) -> str:
    if _SCAN_ID.fullmatch(value) is None:
        raise ValueError("semantic scan id must be canonical")
    return value


def _inert_id(value: str, label: str) -> str:
    if _INERT_ID.fullmatch(value) is None:
        raise ValueError(f"{label} must be a bounded inert identifier")
    return value


def _inert_scope(value: str, label: str) -> str:
    if _SCOPE.fullmatch(value) is None:
        raise ValueError(f"{label} must be inert")
    return value


def _bounded_text(value: str, label: str, *, maximum_bytes: int) -> str:
    if (
        not value.strip()
        or len(value.encode()) > maximum_bytes
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{label} must be bounded and non-blank")
    return value


def _sha256(value: str, label: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return value


__all__ = [
    "SemanticChangeScanCompletion",
    "SemanticChangeScanFailure",
    "SemanticChangeScanFailureCode",
    "SemanticChangeScanFailureDisposition",
    "SemanticChangeScanLease",
    "SemanticChangeScanRequest",
    "SemanticChangeScanSourceKind",
    "SemanticChangeScanStatus",
    "SemanticChangeScanSupersession",
    "SemanticChangeScanTransitionError",
    "SemanticChangeScanTransitionErrorCode",
    "claim_semantic_change_scan",
    "classify_semantic_change_scan_failure",
    "complete_semantic_change_scan",
    "digest_semantic_change_scan_capability",
    "fail_semantic_change_scan",
    "heartbeat_semantic_change_scan",
    "reclaim_expired_semantic_change_scan",
    "semantic_change_scan_claim_matches",
    "semantic_change_scan_retry_delay",
    "supersede_semantic_change_scan",
]
