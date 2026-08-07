"""Pure contracts and transition rules for durable governed background jobs."""

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
from schemabridge.domain.connectors import GovernedExecutionTarget
from schemabridge.domain.resolution import MAX_REJECTED_SOURCE_TOTAL
from schemabridge.domain.workflows import WorkflowStage

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,199}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_MAX_AUTHORIZATION_LIFETIME = timedelta(hours=1)
_MAX_LEASE_DURATION = timedelta(minutes=5)
_MAX_RETRY_DELAY_SECONDS = 300
_INITIAL_RETRY_DELAY_SECONDS = 5
UNCLASSIFIED_REJECTION_COUNT_CODE = "unclassified_rejections"


class JobKind(StrEnum):
    """Closed commands that may cross the background-worker boundary."""

    EXECUTE_WORKFLOW_PREVIEW = "execute_workflow_preview"


class JobWorkflowAccessScope(StrEnum):
    """How the submitting principal was authorized for the immutable grant."""

    OWNER = "owner"
    WORKSPACE = "workspace"


class JobStatus(StrEnum):
    """Closed durable lifecycle for an execution job."""

    QUEUED = "queued"
    LEASED = "leased"
    CANCEL_REQUESTED = "cancel_requested"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD_LETTERED = "dead_lettered"

    @property
    def is_terminal(self) -> bool:
        return self in {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.DEAD_LETTERED,
        }


class JobFailureCode(StrEnum):
    """Sanitized worker failures with a closed automatic-retry policy."""

    REGISTRY_UNAVAILABLE = "registry_unavailable"
    SOURCE_UNAVAILABLE = "source_unavailable"
    SOURCE_TIMEOUT = "source_timeout"
    AUTHORIZATION_EXPIRED = "authorization_expired"
    AUTHORIZATION_MISMATCH = "authorization_mismatch"
    WORKFLOW_UNAVAILABLE = "workflow_unavailable"
    WORKFLOW_STALE = "workflow_stale"
    WORKFLOW_RETRY_REQUIRED = "workflow_retry_required"
    REGISTRY_STALE = "registry_stale"
    SEMANTIC_CONTEXT_STALE = "semantic_context_stale"
    PLAN_STALE = "plan_stale"
    SQL_POLICY_REJECTED = "sql_policy_rejected"
    SOURCE_POLICY_REJECTED = "source_policy_rejected"
    RESULT_INVALID = "result_invalid"
    AMBIGUOUS_EXTERNAL_EFFECT = "ambiguous_external_effect"
    UNEXPECTED_WORKER_FAILURE = "unexpected_worker_failure"


class JobFailureDisposition(StrEnum):
    """The only durable outcomes permitted for a classified failure."""

    RETRY = "retry"
    FAIL = "fail"
    DEAD_LETTER = "dead_letter"


_TRANSIENT_FAILURE_CODES = frozenset(
    {
        JobFailureCode.REGISTRY_UNAVAILABLE,
        JobFailureCode.SOURCE_UNAVAILABLE,
        JobFailureCode.SOURCE_TIMEOUT,
    }
)
_DIRECT_DEAD_LETTER_CODES = frozenset(
    {
        JobFailureCode.AMBIGUOUS_EXTERNAL_EFFECT,
        JobFailureCode.UNEXPECTED_WORKER_FAILURE,
    }
)


class JobTransitionErrorCode(StrEnum):
    """Stable pure-domain transition failures."""

    INVALID_STATE = "job_invalid_state"
    NOT_AVAILABLE = "job_not_available"
    ATTEMPTS_EXHAUSTED = "job_attempts_exhausted"
    LEASE_MISMATCH = "job_lease_mismatch"
    LEASE_EXPIRED = "job_lease_expired"
    FENCING_MISMATCH = "job_fencing_mismatch"
    TEMPORAL_CONFLICT = "job_temporal_conflict"
    TERMINAL_IMMUTABLE = "job_terminal_immutable"
    CANCELLATION_PENDING = "job_cancellation_pending"
    AUTHORIZATION_EXPIRED = "job_authorization_expired"
    RESULT_MISMATCH = "job_result_mismatch"


class JobTransitionError(ValueError):
    """Typed deterministic failure raised before a job mutation is persisted."""

    def __init__(self, code: JobTransitionErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class JobExecutionTargetRef(FrozenDomainModel):
    """Minimal public target identity persisted with a background authorization."""

    workspace_id: str = Field(min_length=3, max_length=200)
    connection_id: CatalogConnectionId
    route_revision: int = Field(strict=True, ge=1)
    route_fingerprint: str
    target_fingerprint: str

    @field_validator("workspace_id")
    @classmethod
    def workspace_must_be_inert(cls, value: str) -> str:
        return _inert_id(value, "job connector workspace")

    @field_validator("route_fingerprint", "target_fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "job connector target fingerprint")

    @classmethod
    def from_target(cls, target: GovernedExecutionTarget) -> JobExecutionTargetRef:
        return cls(
            workspace_id=target.workspace_id,
            connection_id=target.connection_id,
            route_revision=target.route_revision,
            route_fingerprint=target.route_fingerprint,
            target_fingerprint=target.fingerprint,
        )


class JobAuthorization(FrozenDomainModel):
    """Immutable authorization reserved at authenticated job submission."""

    # `workspace_id` is the current submitting scope used for job visibility and
    # idempotency. `workflow_workspace_id` is the immutable historical scope in
    # which the workflow and owner grant actually remain stored.
    workspace_id: str = Field(min_length=3, max_length=200)
    workflow_workspace_id: str = Field(min_length=3, max_length=200)
    workflow_id: str = Field(min_length=3, max_length=200)
    workflow_owner_actor_id: str = Field(min_length=3, max_length=200)
    submitting_actor_id: str = Field(min_length=3, max_length=200)
    workflow_access_scope: JobWorkflowAccessScope
    operation: JobKind = JobKind.EXECUTE_WORKFLOW_PREVIEW
    expected_workflow_revision: int = Field(ge=1)
    expected_plan_fingerprint: str
    execution_target: JobExecutionTargetRef | None = None
    authenticated_at: datetime
    authorized_at: datetime
    expires_at: datetime
    payload_fingerprint: str
    request_fingerprint: str

    @field_validator(
        "workspace_id",
        "workflow_workspace_id",
        "workflow_id",
        "workflow_owner_actor_id",
        "submitting_actor_id",
    )
    @classmethod
    def identifiers_must_be_inert(cls, value: str) -> str:
        return _inert_id(value, "job authorization identifier")

    @field_validator(
        "expected_plan_fingerprint",
        "payload_fingerprint",
        "request_fingerprint",
    )
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "job authorization fingerprint")

    @field_validator("authenticated_at", "authorized_at", "expires_at")
    @classmethod
    def timestamps_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "job authorization timestamp")

    @model_validator(mode="after")
    def authorization_must_bind_one_current_request(self) -> JobAuthorization:
        if not self.authenticated_at <= self.authorized_at < self.expires_at:
            raise ValueError("job authorization times are not ordered")
        if self.expires_at - self.authenticated_at > _MAX_AUTHORIZATION_LIFETIME:
            raise ValueError("job authorization exceeds the maximum session lifetime")
        if (
            self.execution_target is not None
            and self.execution_target.workspace_id != self.workflow_workspace_id
        ):
            raise ValueError("job connector target must remain in the immutable workflow workspace")
        expected_payload = job_payload_fingerprint(
            workspace_id=self.workspace_id,
            workflow_workspace_id=self.workflow_workspace_id,
            workflow_id=self.workflow_id,
            workflow_owner_actor_id=self.workflow_owner_actor_id,
            workflow_access_scope=self.workflow_access_scope,
            operation=self.operation,
            expected_workflow_revision=self.expected_workflow_revision,
            expected_plan_fingerprint=self.expected_plan_fingerprint,
            execution_target=self.execution_target,
        )
        if self.payload_fingerprint != expected_payload:
            raise ValueError("job authorization payload fingerprint does not match")
        expected_request = job_request_fingerprint(
            payload_fingerprint=expected_payload,
            submitting_actor_id=self.submitting_actor_id,
        )
        if self.request_fingerprint != expected_request:
            raise ValueError("job authorization request fingerprint does not match")
        return self

    @classmethod
    def create(
        cls,
        *,
        workspace_id: str,
        workflow_workspace_id: str | None = None,
        workflow_id: str,
        workflow_owner_actor_id: str,
        submitting_actor_id: str,
        workflow_access_scope: JobWorkflowAccessScope = JobWorkflowAccessScope.OWNER,
        expected_workflow_revision: int,
        expected_plan_fingerprint: str,
        execution_target: JobExecutionTargetRef | None = None,
        authenticated_at: datetime,
        authorized_at: datetime,
        expires_at: datetime,
    ) -> JobAuthorization:
        """Create and fingerprint one exact authorization envelope."""

        resolved_workflow_workspace = workflow_workspace_id or workspace_id
        payload_fingerprint = job_payload_fingerprint(
            workspace_id=workspace_id,
            workflow_workspace_id=resolved_workflow_workspace,
            workflow_id=workflow_id,
            workflow_owner_actor_id=workflow_owner_actor_id,
            workflow_access_scope=workflow_access_scope,
            operation=JobKind.EXECUTE_WORKFLOW_PREVIEW,
            expected_workflow_revision=expected_workflow_revision,
            expected_plan_fingerprint=expected_plan_fingerprint,
            execution_target=execution_target,
        )
        request_fingerprint = job_request_fingerprint(
            payload_fingerprint=payload_fingerprint,
            submitting_actor_id=submitting_actor_id,
        )
        return cls(
            workspace_id=workspace_id,
            workflow_workspace_id=resolved_workflow_workspace,
            workflow_id=workflow_id,
            workflow_owner_actor_id=workflow_owner_actor_id,
            submitting_actor_id=submitting_actor_id,
            workflow_access_scope=workflow_access_scope,
            operation=JobKind.EXECUTE_WORKFLOW_PREVIEW,
            expected_workflow_revision=expected_workflow_revision,
            expected_plan_fingerprint=expected_plan_fingerprint,
            execution_target=execution_target,
            authenticated_at=authenticated_at,
            authorized_at=authorized_at,
            expires_at=expires_at,
            payload_fingerprint=payload_fingerprint,
            request_fingerprint=request_fingerprint,
        )

    def is_current(self, at: datetime) -> bool:
        """Return whether the reserved authorization is usable at an injected instant."""

        instant = _aware(at, "job authorization check")
        return self.authorized_at <= instant < self.expires_at


class JobLease(FrozenDomainModel):
    """One exact worker lease with a monotonic fencing token."""

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
        return _inert_id(value, "job lease identifier")

    @field_validator("token_digest")
    @classmethod
    def token_must_be_an_opaque_digest(cls, value: str) -> str:
        return _sha256(value, "job lease token digest")

    @field_validator("acquired_at", "heartbeat_at", "expires_at")
    @classmethod
    def timestamps_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "job lease timestamp")

    @model_validator(mode="after")
    def lease_window_and_fence_must_be_exact(self) -> JobLease:
        if not self.acquired_at <= self.heartbeat_at < self.expires_at:
            raise ValueError("job lease times are not ordered")
        if self.expires_at - self.heartbeat_at > _MAX_LEASE_DURATION:
            raise ValueError("job lease exceeds its maximum duration")
        if self.fencing_token != self.attempt:
            raise ValueError("job lease fencing token must equal its monotonic attempt")
        return self

    def is_current(self, at: datetime) -> bool:
        """Return whether the lease has not expired at an injected instant."""

        instant = _aware(at, "job lease check")
        return self.acquired_at <= instant < self.expires_at


class JobRejectionCount(FrozenDomainModel):
    """One bounded source-quality rejection count without source values."""

    code: str
    count: int = Field(ge=1, le=10_000)

    @field_validator("code")
    @classmethod
    def code_must_be_sanitized(cls, value: str) -> str:
        if _SAFE_CODE.fullmatch(value) is None:
            raise ValueError("job rejection code is not sanitized")
        if value == UNCLASSIFIED_REJECTION_COUNT_CODE:
            raise ValueError("job rejection code is reserved for durable truncation metadata")
        return value


class JobResultSummary(FrozenDomainModel):
    """Sanitized durable preview result; rows and SQL have no representation."""

    workflow_id: str = Field(min_length=3, max_length=200)
    workflow_revision: int = Field(ge=1)
    stage: WorkflowStage
    row_count: int = Field(ge=0, le=10_000)
    preview_fingerprint: str
    rejected_count: int = Field(ge=0, le=MAX_REJECTED_SOURCE_TOTAL)
    rejection_code_counts: tuple[JobRejectionCount, ...] = Field(
        default=(),
        max_length=64,
    )
    truncated: bool = False
    completed_at: datetime

    @field_validator("workflow_id")
    @classmethod
    def workflow_id_must_be_inert(cls, value: str) -> str:
        return _inert_id(value, "job result workflow identifier")

    @field_validator("preview_fingerprint")
    @classmethod
    def preview_fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "job result preview fingerprint")

    @field_validator("completed_at")
    @classmethod
    def completed_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "job result completion timestamp")

    @field_validator("rejection_code_counts")
    @classmethod
    def rejection_codes_must_be_unique_and_canonical(
        cls,
        values: tuple[JobRejectionCount, ...],
    ) -> tuple[JobRejectionCount, ...]:
        codes = tuple(item.code for item in values)
        if codes != tuple(sorted(codes)) or len(codes) != len(set(codes)):
            raise ValueError("job rejection counts must have unique sorted codes")
        return values

    @model_validator(mode="after")
    def result_must_be_a_completed_bounded_preview(self) -> JobResultSummary:
        if self.stage is not WorkflowStage.PUBLICATION_PROPOSED:
            raise ValueError("job result must describe the post-preview publication checkpoint")
        sampled_count = self.sampled_rejected_count
        if sampled_count > self.rejected_count:
            raise ValueError("job rejection sample exceeds its exact total")
        if self.rejection_truncated and not self.truncated:
            raise ValueError("truncated rejection counts require a truncated job result")
        durable_entry_count = len(self.rejection_code_counts) + int(self.rejection_truncated)
        if durable_entry_count > 64:
            raise ValueError("job rejection summary exceeds its durable entry bound")
        return self

    @property
    def sampled_rejected_count(self) -> int:
        """Return the number represented by the bounded code sample."""

        return sum(item.count for item in self.rejection_code_counts)

    @property
    def unclassified_rejection_count(self) -> int:
        """Return the exact residual omitted by bounded rejection inspection."""

        return self.rejected_count - self.sampled_rejected_count

    @property
    def rejection_counts_complete(self) -> bool:
        """Return whether code counts describe the complete rejection population."""

        return self.unclassified_rejection_count == 0

    @property
    def rejection_truncated(self) -> bool:
        """Return whether rejection code counts came from a bounded sample."""

        return not self.rejection_counts_complete


class JobFailure(FrozenDomainModel):
    """One sanitized failure classification retained in durable state."""

    code: JobFailureCode
    disposition: JobFailureDisposition
    attempt: int = Field(ge=0, le=10)
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def occurred_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "job failure timestamp")

    @model_validator(mode="after")
    def disposition_must_match_the_closed_failure_class(self) -> JobFailure:
        if self.attempt == 0 and not (
            self.code is JobFailureCode.AUTHORIZATION_EXPIRED
            and self.disposition is JobFailureDisposition.FAIL
        ):
            raise ValueError("only pre-delivery authorization expiry may use attempt zero")
        if (
            self.code in _DIRECT_DEAD_LETTER_CODES
            and self.disposition is not JobFailureDisposition.DEAD_LETTER
        ):
            raise ValueError("ambiguous worker failure must be dead-lettered")
        if (
            self.code not in _DIRECT_DEAD_LETTER_CODES | _TRANSIENT_FAILURE_CODES
            and self.disposition is not JobFailureDisposition.FAIL
        ):
            raise ValueError("permanent worker failure must fail terminally")
        if self.code in _TRANSIENT_FAILURE_CODES and self.disposition not in {
            JobFailureDisposition.RETRY,
            JobFailureDisposition.DEAD_LETTER,
        }:
            raise ValueError("transient worker failure must retry or be dead-lettered")
        return self


class BackgroundJob(FrozenDomainModel):
    """Complete immutable durable state for one governed execution job."""

    id: str = Field(min_length=3, max_length=200)
    kind: JobKind
    status: JobStatus
    authorization: JobAuthorization
    connector_contract_version: int | None = Field(default=None, strict=True, ge=1)
    idempotency_digest: str
    request_fingerprint: str
    max_attempts: int = Field(ge=1, le=10)
    attempt_count: int = Field(ge=0, le=10)
    last_fencing_token: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime
    available_at: datetime | None = None
    lease: JobLease | None = None
    cancel_requested_at: datetime | None = None
    completed_at: datetime | None = None
    result: JobResultSummary | None = None
    failure: JobFailure | None = None

    @field_validator("id")
    @classmethod
    def id_must_be_inert(cls, value: str) -> str:
        return _inert_id(value, "background job identifier")

    @field_validator("idempotency_digest", "request_fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "background job fingerprint")

    @field_validator(
        "created_at",
        "updated_at",
        "available_at",
        "cancel_requested_at",
        "completed_at",
    )
    @classmethod
    def optional_timestamps_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _aware(value, "background job timestamp")

    @model_validator(mode="after")
    def lifecycle_shape_must_match_status(self) -> BackgroundJob:
        if self.kind is not self.authorization.operation:
            raise ValueError("job kind does not match its authorization")
        if (
            self.authorization.execution_target is None
            and self.connector_contract_version is not None
        ):
            raise ValueError("legacy job cannot claim a connector contract version")
        if self.request_fingerprint != self.authorization.request_fingerprint:
            raise ValueError("job request fingerprint does not match its authorization")
        if not self.authorization.authorized_at <= self.created_at < self.authorization.expires_at:
            raise ValueError("job was not created inside its authorization window")
        if self.updated_at < self.created_at:
            raise ValueError("job update precedes its creation")
        if self.attempt_count > self.max_attempts:
            raise ValueError("job attempts exceed the configured maximum")
        if self.last_fencing_token != self.attempt_count:
            raise ValueError("job fence must advance exactly once per attempt")
        if self.cancel_requested_at is not None and not (
            self.created_at <= self.cancel_requested_at <= self.updated_at
        ):
            raise ValueError("job cancellation timestamp is outside its lifecycle")
        if self.completed_at is not None and self.completed_at != self.updated_at:
            raise ValueError("terminal job completion must equal its last update")
        if self.lease is not None and (
            self.lease.job_id != self.id
            or self.lease.attempt != self.attempt_count
            or self.lease.fencing_token != self.last_fencing_token
            or self.lease.acquired_at < self.created_at
            or self.lease.heartbeat_at > self.updated_at
        ):
            raise ValueError("job lease does not match its durable owner state")
        if self.result is not None and (
            self.result.workflow_id != self.authorization.workflow_id
            or self.result.workflow_revision < self.authorization.expected_workflow_revision
            or self.result.completed_at != self.completed_at
        ):
            raise ValueError("job result does not match its authorization or completion")
        if self.failure is not None and (
            self.failure.attempt != self.attempt_count
            or self.failure.occurred_at != self.updated_at
        ):
            raise ValueError("job failure does not match its current attempt and update")
        if (
            self.failure is not None
            and self.failure.code is JobFailureCode.AUTHORIZATION_EXPIRED
            and self.failure.occurred_at < self.authorization.expires_at
        ):
            raise ValueError("job authorization-expiry failure precedes its expiry")
        if self.failure is not None and self.failure.attempt >= 1:
            expected_disposition = classify_job_failure(
                self.failure.code,
                attempt=self.failure.attempt,
                max_attempts=self.max_attempts,
            )
            if self.failure.disposition is not expected_disposition:
                raise ValueError("job failure disposition does not match its attempt policy")

        if self.status is JobStatus.QUEUED:
            self._require(
                self.attempt_count == 0
                and self.available_at is not None
                and self.lease is None
                and self.cancel_requested_at is None
                and self.completed_at is None
                and self.result is None
                and self.failure is None,
                "queued job state is inconsistent",
            )
        elif self.status is JobStatus.LEASED:
            self._require(
                self.attempt_count >= 1
                and self.available_at is None
                and self.lease is not None
                and self.cancel_requested_at is None
                and self.completed_at is None
                and self.result is None
                and self.failure is None,
                "leased job state is inconsistent",
            )
        elif self.status is JobStatus.CANCEL_REQUESTED:
            self._require(
                self.attempt_count >= 1
                and self.available_at is None
                and self.lease is not None
                and self.cancel_requested_at is not None
                and self.completed_at is None
                and self.result is None
                and self.failure is None,
                "cancel-requested job state is inconsistent",
            )
        elif self.status is JobStatus.RETRY_WAIT:
            self._require(
                self.attempt_count >= 1
                and self.available_at is not None
                and self.lease is None
                and self.cancel_requested_at is None
                and self.completed_at is None
                and self.result is None
                and self.failure is not None
                and self.failure.disposition is JobFailureDisposition.RETRY,
                "retry-wait job state is inconsistent",
            )
        elif self.status is JobStatus.SUCCEEDED:
            self._require(
                self.attempt_count >= 1
                and self.available_at is None
                and self.lease is None
                and self.completed_at is not None
                and self.result is not None
                and self.failure is None,
                "succeeded job state is inconsistent",
            )
        elif self.status is JobStatus.FAILED:
            self._require(
                self.available_at is None
                and self.lease is None
                and self.completed_at is not None
                and self.result is None
                and self.failure is not None
                and self.failure.disposition is JobFailureDisposition.FAIL,
                "failed job state is inconsistent",
            )
        elif self.status is JobStatus.CANCELLED:
            self._require(
                self.available_at is None
                and self.lease is None
                and self.cancel_requested_at is not None
                and self.completed_at is not None
                and self.result is None
                and self.failure is None,
                "cancelled job state is inconsistent",
            )
        else:
            self._require(
                self.status is JobStatus.DEAD_LETTERED
                and self.attempt_count >= 1
                and self.available_at is None
                and self.lease is None
                and self.completed_at is not None
                and self.result is None
                and self.failure is not None
                and self.failure.disposition is JobFailureDisposition.DEAD_LETTER,
                "dead-lettered job state is inconsistent",
            )
        return self

    @staticmethod
    def _require(condition: bool, message: str) -> None:
        if not condition:
            raise ValueError(message)

    @classmethod
    def create(
        cls,
        *,
        id: str,
        authorization: JobAuthorization,
        connector_contract_version: int | None = None,
        idempotency_digest: str,
        max_attempts: int,
        created_at: datetime,
    ) -> BackgroundJob:
        """Create a queued job containing only a digest of the caller key."""

        return cls(
            id=id,
            kind=authorization.operation,
            status=JobStatus.QUEUED,
            authorization=authorization,
            connector_contract_version=connector_contract_version,
            idempotency_digest=idempotency_digest,
            request_fingerprint=authorization.request_fingerprint,
            max_attempts=max_attempts,
            attempt_count=0,
            last_fencing_token=0,
            created_at=created_at,
            updated_at=created_at,
            available_at=created_at,
        )

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal


class JobSubmissionResult(FrozenDomainModel):
    """Atomic submission result, including exact idempotent replay."""

    job: BackgroundJob
    replayed: bool = False


def job_payload_fingerprint(
    *,
    workspace_id: str,
    workflow_workspace_id: str | None = None,
    workflow_id: str,
    workflow_owner_actor_id: str,
    workflow_access_scope: JobWorkflowAccessScope = JobWorkflowAccessScope.OWNER,
    operation: JobKind,
    expected_workflow_revision: int,
    expected_plan_fingerprint: str,
    execution_target: JobExecutionTargetRef | None = None,
) -> str:
    """Fingerprint the executable authorization payload without a bearer token."""

    payload = {
        "workspace_id": _inert_id(workspace_id, "job workspace"),
        "workflow_workspace_id": _inert_id(
            workflow_workspace_id or workspace_id,
            "job workflow workspace",
        ),
        "workflow_id": _inert_id(workflow_id, "job workflow"),
        "workflow_owner_actor_id": _inert_id(
            workflow_owner_actor_id,
            "job workflow owner",
        ),
        "workflow_access_scope": workflow_access_scope.value,
        "operation": operation.value,
        "expected_workflow_revision": expected_workflow_revision,
        "expected_plan_fingerprint": _sha256(
            expected_plan_fingerprint,
            "job plan fingerprint",
        ),
        "execution_target": (
            execution_target.model_dump(mode="json") if execution_target is not None else None
        ),
    }
    if expected_workflow_revision < 1:
        raise ValueError("job workflow revision must be positive")
    return _fingerprint(payload)


def job_request_fingerprint(
    *,
    payload_fingerprint: str,
    submitting_actor_id: str,
) -> str:
    """Fingerprint stable request identity; session times remain original envelope evidence."""

    payload = {
        "payload_fingerprint": _sha256(payload_fingerprint, "job payload fingerprint"),
        "submitting_actor_id": _inert_id(submitting_actor_id, "job submitting actor"),
    }
    return _fingerprint(payload)


def classify_job_failure(
    code: JobFailureCode,
    *,
    attempt: int,
    max_attempts: int,
) -> JobFailureDisposition:
    """Apply the closed retry allowlist and finite-attempt policy."""

    if not 1 <= attempt <= max_attempts <= 10:
        raise ValueError("job failure attempt bounds are invalid")
    if code in _DIRECT_DEAD_LETTER_CODES:
        return JobFailureDisposition.DEAD_LETTER
    if code in _TRANSIENT_FAILURE_CODES:
        if attempt < max_attempts:
            return JobFailureDisposition.RETRY
        return JobFailureDisposition.DEAD_LETTER
    return JobFailureDisposition.FAIL


def job_retry_delay(attempt: int) -> timedelta:
    """Return deterministic exponential retry delay capped at five minutes."""

    if not 1 <= attempt <= 10:
        raise ValueError("job retry attempt must be between one and ten")
    seconds = min(
        _INITIAL_RETRY_DELAY_SECONDS * (2 ** (attempt - 1)),
        _MAX_RETRY_DELAY_SECONDS,
    )
    return timedelta(seconds=seconds)


def claim_job(
    job: BackgroundJob,
    *,
    worker_id: str,
    lease_token: str,
    claimed_at: datetime,
    lease_expires_at: datetime,
) -> BackgroundJob:
    """Claim a due or expired job and advance its monotonic fence."""

    if job.is_terminal:
        _raise_terminal()
    claimed = _monotonic_time(job, claimed_at, "job claim timestamp")
    expires = _aware(lease_expires_at, "job lease expiry")
    if expires <= claimed or expires - claimed > _MAX_LEASE_DURATION:
        raise JobTransitionError(
            JobTransitionErrorCode.TEMPORAL_CONFLICT,
            "job lease duration is outside its bounded window",
        )
    if job.attempt_count >= job.max_attempts:
        raise JobTransitionError(
            JobTransitionErrorCode.ATTEMPTS_EXHAUSTED,
            "job has no remaining delivery attempt",
        )

    if job.status in {JobStatus.QUEUED, JobStatus.RETRY_WAIT}:
        assert job.available_at is not None
        if claimed < job.available_at:
            raise JobTransitionError(
                JobTransitionErrorCode.NOT_AVAILABLE,
                "job is not due for delivery",
            )
        next_status = JobStatus.LEASED
        cancel_requested_at = None
    elif job.status in {JobStatus.LEASED, JobStatus.CANCEL_REQUESTED}:
        assert job.lease is not None
        if claimed < job.lease.expires_at:
            raise JobTransitionError(
                JobTransitionErrorCode.NOT_AVAILABLE,
                "job already has a current lease",
            )
        next_status = job.status
        cancel_requested_at = job.cancel_requested_at
    else:
        raise JobTransitionError(
            JobTransitionErrorCode.INVALID_STATE,
            "job cannot be claimed from its current state",
        )

    attempt = job.attempt_count + 1
    lease = JobLease(
        job_id=job.id,
        worker_id=worker_id,
        token_digest=digest_lease_token(lease_token),
        fencing_token=job.last_fencing_token + 1,
        attempt=attempt,
        acquired_at=claimed,
        heartbeat_at=claimed,
        expires_at=expires,
    )
    return _replace_job(
        job,
        status=next_status,
        attempt_count=attempt,
        last_fencing_token=lease.fencing_token,
        updated_at=claimed,
        available_at=None,
        lease=lease,
        cancel_requested_at=cancel_requested_at,
        completed_at=None,
        result=None,
        failure=None,
    )


def heartbeat_job(
    job: BackgroundJob,
    *,
    worker_id: str,
    lease_token: str,
    fencing_token: int,
    heartbeat_at: datetime,
    lease_expires_at: datetime,
) -> BackgroundJob:
    """Extend only the exact current, unexpired worker lease."""

    heartbeat = _monotonic_time(job, heartbeat_at, "job heartbeat timestamp")
    expires = _aware(lease_expires_at, "job heartbeat expiry")
    lease = _require_active_lease(
        job,
        worker_id=worker_id,
        lease_token=lease_token,
        fencing_token=fencing_token,
        at=heartbeat,
    )
    if (
        expires <= lease.expires_at
        or expires <= heartbeat
        or expires - heartbeat > _MAX_LEASE_DURATION
    ):
        raise JobTransitionError(
            JobTransitionErrorCode.TEMPORAL_CONFLICT,
            "job heartbeat must extend the current lease",
        )
    return _replace_job(
        job,
        updated_at=heartbeat,
        lease=JobLease(
            job_id=lease.job_id,
            worker_id=lease.worker_id,
            token_digest=lease.token_digest,
            fencing_token=lease.fencing_token,
            attempt=lease.attempt,
            acquired_at=lease.acquired_at,
            heartbeat_at=heartbeat,
            expires_at=expires,
        ),
    )


def request_job_cancellation(
    job: BackgroundJob,
    *,
    requested_at: datetime,
) -> BackgroundJob:
    """Cancel waiting work immediately or mark an active lease cooperatively."""

    requested = _monotonic_time(job, requested_at, "job cancellation timestamp")
    if job.status in {JobStatus.CANCELLED, JobStatus.CANCEL_REQUESTED}:
        return job
    if job.is_terminal:
        _raise_terminal()
    if job.status in {JobStatus.QUEUED, JobStatus.RETRY_WAIT}:
        return _replace_job(
            job,
            status=JobStatus.CANCELLED,
            updated_at=requested,
            available_at=None,
            lease=None,
            cancel_requested_at=requested,
            completed_at=requested,
            result=None,
            failure=None,
        )
    if job.status is JobStatus.LEASED:
        return _replace_job(
            job,
            status=JobStatus.CANCEL_REQUESTED,
            updated_at=requested,
            cancel_requested_at=requested,
        )
    raise JobTransitionError(
        JobTransitionErrorCode.INVALID_STATE,
        "job cannot accept cancellation from its current state",
    )


def acknowledge_job_cancellation(
    job: BackgroundJob,
    *,
    worker_id: str,
    lease_token: str,
    fencing_token: int,
    cancelled_at: datetime,
) -> BackgroundJob:
    """Acknowledge cooperative cancellation using the exact current lease."""

    cancelled = _monotonic_time(job, cancelled_at, "job cancellation acknowledgement")
    if job.status is not JobStatus.CANCEL_REQUESTED:
        if job.status is JobStatus.CANCELLED:
            return job
        if job.is_terminal:
            _raise_terminal()
        raise JobTransitionError(
            JobTransitionErrorCode.INVALID_STATE,
            "job has no cooperative cancellation to acknowledge",
        )
    _require_active_lease(
        job,
        worker_id=worker_id,
        lease_token=lease_token,
        fencing_token=fencing_token,
        at=cancelled,
    )
    return _replace_job(
        job,
        status=JobStatus.CANCELLED,
        updated_at=cancelled,
        available_at=None,
        lease=None,
        completed_at=cancelled,
        result=None,
        failure=None,
    )


def complete_job(
    job: BackgroundJob,
    *,
    worker_id: str,
    lease_token: str,
    fencing_token: int,
    result: JobResultSummary,
) -> BackgroundJob:
    """Commit a sanitized result through the exact current lease."""

    if job.status not in {JobStatus.LEASED, JobStatus.CANCEL_REQUESTED}:
        if job.is_terminal:
            _raise_terminal()
        raise JobTransitionError(
            JobTransitionErrorCode.INVALID_STATE,
            "job cannot complete from its current state",
        )
    completed = _monotonic_time(job, result.completed_at, "job completion timestamp")
    _require_active_lease(
        job,
        worker_id=worker_id,
        lease_token=lease_token,
        fencing_token=fencing_token,
        at=completed,
    )
    if not job.authorization.is_current(completed):
        raise JobTransitionError(
            JobTransitionErrorCode.AUTHORIZATION_EXPIRED,
            "job authorization expired before result commit",
        )
    if (
        result.workflow_id != job.authorization.workflow_id
        or result.workflow_revision < job.authorization.expected_workflow_revision
    ):
        raise JobTransitionError(
            JobTransitionErrorCode.RESULT_MISMATCH,
            "job result does not match its authorized workflow",
        )
    return _replace_job(
        job,
        status=JobStatus.SUCCEEDED,
        updated_at=result.completed_at,
        available_at=None,
        lease=None,
        completed_at=result.completed_at,
        result=result,
        failure=None,
    )


def fail_job(
    job: BackgroundJob,
    *,
    worker_id: str,
    lease_token: str,
    fencing_token: int,
    code: JobFailureCode,
    failed_at: datetime,
    retry_at: datetime | None = None,
) -> BackgroundJob:
    """Classify a worker failure into retry, terminal failure, or dead letter."""

    failed = _monotonic_time(job, failed_at, "job failure timestamp")
    if job.status not in {JobStatus.LEASED, JobStatus.CANCEL_REQUESTED}:
        if job.is_terminal:
            _raise_terminal()
        raise JobTransitionError(
            JobTransitionErrorCode.INVALID_STATE,
            "job cannot fail from its current state",
        )
    _require_active_lease(
        job,
        worker_id=worker_id,
        lease_token=lease_token,
        fencing_token=fencing_token,
        at=failed,
    )
    disposition = classify_job_failure(
        code,
        attempt=job.attempt_count,
        max_attempts=job.max_attempts,
    )
    if job.status is JobStatus.CANCEL_REQUESTED and disposition is JobFailureDisposition.RETRY:
        raise JobTransitionError(
            JobTransitionErrorCode.CANCELLATION_PENDING,
            "cancel-requested work cannot schedule another delivery",
        )
    failure = JobFailure(
        code=code,
        disposition=disposition,
        attempt=job.attempt_count,
        occurred_at=failed,
    )
    if disposition is JobFailureDisposition.RETRY:
        if retry_at is None:
            raise JobTransitionError(
                JobTransitionErrorCode.TEMPORAL_CONFLICT,
                "retryable job failure requires its next available time",
            )
        retry = _aware(retry_at, "job retry timestamp")
        if retry != failed + job_retry_delay(job.attempt_count):
            raise JobTransitionError(
                JobTransitionErrorCode.TEMPORAL_CONFLICT,
                "job retry time does not match the deterministic backoff",
            )
        return _replace_job(
            job,
            status=JobStatus.RETRY_WAIT,
            updated_at=failed,
            available_at=retry,
            lease=None,
            cancel_requested_at=None,
            completed_at=None,
            result=None,
            failure=failure,
        )
    if retry_at is not None:
        raise JobTransitionError(
            JobTransitionErrorCode.TEMPORAL_CONFLICT,
            "terminal job failure cannot carry a retry time",
        )
    status = (
        JobStatus.DEAD_LETTERED
        if disposition is JobFailureDisposition.DEAD_LETTER
        else JobStatus.FAILED
    )
    return _replace_job(
        job,
        status=status,
        updated_at=failed,
        available_at=None,
        lease=None,
        completed_at=failed,
        result=None,
        failure=failure,
    )


def expire_job_authorization(
    job: BackgroundJob,
    *,
    expired_at: datetime,
) -> BackgroundJob:
    """Fail waiting work or an abandoned lease after authorization expiry."""

    expired = _monotonic_time(job, expired_at, "job authorization expiry")
    if expired < job.authorization.expires_at:
        raise JobTransitionError(
            JobTransitionErrorCode.TEMPORAL_CONFLICT,
            "job authorization is still current",
        )
    if job.status is JobStatus.FAILED and (
        job.failure is not None and job.failure.code is JobFailureCode.AUTHORIZATION_EXPIRED
    ):
        return job
    if job.is_terminal:
        _raise_terminal()
    if job.status in {JobStatus.LEASED, JobStatus.CANCEL_REQUESTED}:
        assert job.lease is not None
        if expired < job.lease.expires_at:
            raise JobTransitionError(
                JobTransitionErrorCode.NOT_AVAILABLE,
                "current worker lease cannot be expired by authorization cleanup",
            )
    elif job.status not in {JobStatus.QUEUED, JobStatus.RETRY_WAIT}:
        raise JobTransitionError(
            JobTransitionErrorCode.INVALID_STATE,
            "job authorization cannot expire from its current state",
        )
    failure = JobFailure(
        code=JobFailureCode.AUTHORIZATION_EXPIRED,
        disposition=JobFailureDisposition.FAIL,
        attempt=job.attempt_count,
        occurred_at=expired,
    )
    return _replace_job(
        job,
        status=JobStatus.FAILED,
        updated_at=expired,
        available_at=None,
        lease=None,
        cancel_requested_at=job.cancel_requested_at,
        completed_at=expired,
        result=None,
        failure=failure,
    )


def dead_letter_exhausted_lease(
    job: BackgroundJob,
    *,
    expired_at: datetime,
) -> BackgroundJob:
    """Close an expired final lease instead of leaving it permanently unclaimable."""

    expired = _monotonic_time(job, expired_at, "job exhausted lease expiry")
    if job.status is JobStatus.DEAD_LETTERED and (
        job.failure is not None and job.failure.code is JobFailureCode.AMBIGUOUS_EXTERNAL_EFFECT
    ):
        return job
    if job.is_terminal:
        _raise_terminal()
    if job.status not in {JobStatus.LEASED, JobStatus.CANCEL_REQUESTED} or job.lease is None:
        raise JobTransitionError(
            JobTransitionErrorCode.INVALID_STATE,
            "job does not have an exhausted active lease",
        )
    if expired < job.lease.expires_at:
        raise JobTransitionError(
            JobTransitionErrorCode.NOT_AVAILABLE,
            "job lease is still current",
        )
    if job.attempt_count < job.max_attempts:
        raise JobTransitionError(
            JobTransitionErrorCode.NOT_AVAILABLE,
            "job still has a reclaim attempt",
        )
    failure = JobFailure(
        code=JobFailureCode.AMBIGUOUS_EXTERNAL_EFFECT,
        disposition=JobFailureDisposition.DEAD_LETTER,
        attempt=job.attempt_count,
        occurred_at=expired,
    )
    return _replace_job(
        job,
        status=JobStatus.DEAD_LETTERED,
        updated_at=expired,
        available_at=None,
        lease=None,
        completed_at=expired,
        result=None,
        failure=failure,
    )


def _require_active_lease(
    job: BackgroundJob,
    *,
    worker_id: str,
    lease_token: str,
    fencing_token: int,
    at: datetime,
) -> JobLease:
    if job.status not in {JobStatus.LEASED, JobStatus.CANCEL_REQUESTED} or job.lease is None:
        raise JobTransitionError(
            JobTransitionErrorCode.INVALID_STATE,
            "job does not have an active lease",
        )
    lease = job.lease
    if lease.worker_id != worker_id or not hmac.compare_digest(
        lease.token_digest,
        digest_lease_token(lease_token),
    ):
        raise JobTransitionError(
            JobTransitionErrorCode.LEASE_MISMATCH,
            "job lease ownership does not match",
        )
    if lease.fencing_token != fencing_token:
        raise JobTransitionError(
            JobTransitionErrorCode.FENCING_MISMATCH,
            "job fencing token is stale",
        )
    if not lease.is_current(at):
        raise JobTransitionError(
            JobTransitionErrorCode.LEASE_EXPIRED,
            "job lease is no longer current",
        )
    return lease


def _replace_job(job: BackgroundJob, **updates: object) -> BackgroundJob:
    payload = job.model_dump(mode="python")
    payload.update(updates)
    return BackgroundJob.model_validate(payload)


def _raise_terminal() -> None:
    raise JobTransitionError(
        JobTransitionErrorCode.TERMINAL_IMMUTABLE,
        "terminal job state is immutable",
    )


def _monotonic_time(job: BackgroundJob, value: datetime, label: str) -> datetime:
    instant = _aware(value, label)
    if instant < job.updated_at:
        raise JobTransitionError(
            JobTransitionErrorCode.TEMPORAL_CONFLICT,
            f"{label} precedes the current job state",
        )
    return instant


def _inert_id(value: str, label: str) -> str:
    if _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{label} must be a bounded inert identifier")
    return value


def _sha256(value: str, label: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


def digest_lease_token(token: str) -> str:
    """Hash one high-entropy worker capability before durable comparison or storage."""

    if not 32 <= len(token) <= 512 or not token.isascii() or not token.isprintable():
        raise ValueError("job lease token must be bounded printable ASCII")
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return value


def _fingerprint(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
