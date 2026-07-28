"""Authenticated application use cases for governed preview-execution jobs."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from schemabridge.application.authorization import AuthorizationError
from schemabridge.application.ports.authorization import WorkflowAuthorizationPort
from schemabridge.application.ports.background_jobs import (
    BackgroundJobApiStorePort,
    JobStoreError,
    JobStoreErrorCode,
)
from schemabridge.application.ports.workflow_access import (
    WorkflowAccessError,
    WorkflowAccessErrorCode,
    WorkflowAccessStorePort,
)
from schemabridge.application.ports.workflows import (
    WorkflowClockPort,
    WorkflowError,
    WorkflowErrorCode,
)
from schemabridge.domain.background_jobs import (
    BackgroundJob,
    JobAuthorization,
    JobExecutionTargetRef,
    JobKind,
    JobStatus,
    JobSubmissionResult,
    JobWorkflowAccessScope,
)
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    WorkflowPermission,
)
from schemabridge.domain.workflows import (
    AgentWorkflowDraft,
    ExecutionWorkflowDecision,
    RetryWorkflowDecision,
    WorkflowCheckpointKind,
    WorkflowOperation,
    WorkflowStage,
)

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
_SAFE_JOB_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,199}$")
_SAFE_WORKFLOW_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_JOB_AUTHORIZATION_AGE = timedelta(hours=1)
_MIN_JOB_AUTHORIZATION_TTL = timedelta(seconds=30)
_MAX_JOB_AUTHORIZATION_TTL = timedelta(minutes=15)
EXECUTION_CONFIRMATION = "EXECUTE GOVERNED PREVIEW"


class ExecutionJobUseCaseErrorCode(StrEnum):
    """Stable errors safe for an authenticated HTTP boundary."""

    INVALID_REQUEST = "execution_job_invalid_request"
    UNAVAILABLE = "execution_job_unavailable"
    IDEMPOTENCY_CONFLICT = "execution_job_idempotency_conflict"
    CAPACITY_EXCEEDED = "execution_job_capacity_exceeded"
    SERVICE_UNAVAILABLE = "execution_job_service_unavailable"


class ExecutionJobUseCaseError(RuntimeError):
    """Sanitized API workflow failure without protected resource facts."""

    def __init__(self, code: ExecutionJobUseCaseErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class WorkflowOrchestratorPort(Protocol):
    """Small workflow surface needed by API submission and the worker."""

    def inspect(self, workflow_id: str) -> AgentWorkflowDraft:
        """Load one exact workflow without advancing it."""

    def decide_execution(
        self,
        workflow_id: str,
        decision: ExecutionWorkflowDecision,
        *,
        should_continue: Callable[[], bool] | None = None,
    ) -> AgentWorkflowDraft:
        """Apply the reserved decision, checking continuation before each source read."""

    def recover_interrupted(
        self,
        workflow_id: str,
        *,
        expected_operation: WorkflowOperation,
    ) -> AgentWorkflowDraft:
        """Repair one exact interrupted external transition without replaying it."""

    def retry(
        self,
        workflow_id: str,
        decision: RetryWorkflowDecision,
        *,
        reserved_execution: ExecutionWorkflowDecision | None = None,
        should_continue: Callable[[], bool] | None = None,
    ) -> AgentWorkflowDraft:
        """Retry one typed transient failure, optionally reusing the reserved approval."""


class WorkflowOrchestratorFactoryPort(Protocol):
    """Compose a workflow boundary at its exact durable historical coordinates."""

    def __call__(
        self,
        workspace_id: str,
        actor_id: str,
    ) -> WorkflowOrchestratorPort:
        """Return an orchestrator scoped to the supplied opaque identity."""


class WorkflowAccessStoreFactoryPort(Protocol):
    """Compose a grant view rooted at the current authenticated identity."""

    def __call__(
        self,
        workspace_id: str,
        actor_id: str,
    ) -> WorkflowAccessStorePort:
        """Return a tenant-aware workflow grant store."""


@dataclass(frozen=True, slots=True)
class SubmitExecutionJob:
    """Authorize and atomically reserve one exact governed preview job."""

    job_store: BackgroundJobApiStorePort
    access_store_factory: WorkflowAccessStoreFactoryPort
    orchestrator_factory: WorkflowOrchestratorFactoryPort
    authorization: WorkflowAuthorizationPort
    clock: WorkflowClockPort
    max_attempts: int = 3
    authorization_ttl: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 10:
            raise ValueError("max_attempts must be between one and ten")
        if not _MIN_JOB_AUTHORIZATION_TTL <= self.authorization_ttl <= (_MAX_JOB_AUTHORIZATION_TTL):
            raise ValueError("authorization_ttl is outside the supported bound")

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        workflow_id: str,
        expected_workflow_revision: int,
        expected_plan_fingerprint: str,
        confirmation: str,
        idempotency_key: str,
    ) -> JobSubmissionResult:
        """Submit once, replay exactly, or fail before protected source I/O."""

        idempotency_digest = _idempotency_digest(idempotency_key)
        if (
            confirmation != EXECUTION_CONFIRMATION
            or isinstance(expected_workflow_revision, bool)
            or expected_workflow_revision < 1
            or not isinstance(workflow_id, str)
            or _SAFE_WORKFLOW_ID.fullmatch(workflow_id) is None
            or not isinstance(expected_plan_fingerprint, str)
            or _SHA256.fullmatch(expected_plan_fingerprint) is None
        ):
            raise _invalid_request()
        now = self.clock.now()
        try:
            self.authorization.require(
                principal,
                WorkflowPermission.EXECUTE,
                at=now,
            )
        except (AuthorizationError, ValueError):
            raise _unavailable() from None
        except Exception:
            raise _service_unavailable() from None

        job_id = _deterministic_job_id(
            workspace_id=principal.workspace_id,
            submitting_actor_id=principal.actor_id,
            idempotency_digest=idempotency_digest,
        )
        existing = self._load_existing(
            principal=principal,
            idempotency_digest=idempotency_digest,
        )
        if existing is not None:
            if not _matches_replay(
                existing,
                workflow_id=workflow_id,
                expected_workflow_revision=expected_workflow_revision,
                expected_plan_fingerprint=expected_plan_fingerprint,
                idempotency_digest=idempotency_digest,
            ):
                raise _idempotency_conflict()
            return JobSubmissionResult(job=existing, replayed=True)

        try:
            owner_filter = self.authorization.owner_filter_for(
                principal,
                WorkflowPermission.EXECUTE,
                at=now,
            )
            access_store = self.access_store_factory(
                principal.workspace_id,
                principal.actor_id,
            )
            grant = access_store.load(
                principal.workspace_id,
                workflow_id,
                owner_principal_id=owner_filter,
            )
            if grant is None:
                raise _unavailable()
            access_scope = (
                JobWorkflowAccessScope.OWNER
                if owner_filter is not None
                else JobWorkflowAccessScope.WORKSPACE
            )
            draft = self.orchestrator_factory(
                grant.workspace_id,
                grant.owner_actor_id,
            ).inspect(workflow_id)
        except ExecutionJobUseCaseError:
            raise
        except (AuthorizationError, ValueError):
            raise _unavailable() from None
        except WorkflowAccessError as error:
            if error.code is WorkflowAccessErrorCode.STORE_FAILURE:
                raise _service_unavailable() from None
            raise _unavailable() from None
        except WorkflowError as error:
            if error.code is WorkflowErrorCode.NOT_FOUND:
                raise _unavailable() from None
            raise _service_unavailable() from None
        except Exception:
            raise _service_unavailable() from None
        if not _is_exact_execution_checkpoint(
            draft,
            workflow_id=workflow_id,
            expected_workflow_revision=expected_workflow_revision,
            expected_plan_fingerprint=expected_plan_fingerprint,
        ):
            raise _unavailable()

        expires_at = min(
            principal.expires_at,
            now + self.authorization_ttl,
            principal.authenticated_at + _MAX_JOB_AUTHORIZATION_AGE,
        )
        if expires_at <= now:
            raise _unavailable()
        try:
            reserved = JobAuthorization.create(
                workspace_id=principal.workspace_id,
                workflow_workspace_id=grant.workspace_id,
                workflow_id=workflow_id,
                workflow_owner_actor_id=grant.owner_actor_id,
                submitting_actor_id=principal.actor_id,
                workflow_access_scope=access_scope,
                expected_workflow_revision=expected_workflow_revision,
                expected_plan_fingerprint=expected_plan_fingerprint,
                execution_target=(
                    JobExecutionTargetRef.from_target(draft.resolved_plan.execution_target)
                    if draft.resolved_plan is not None
                    and draft.resolved_plan.execution_target is not None
                    else None
                ),
                authenticated_at=principal.authenticated_at,
                authorized_at=now,
                expires_at=expires_at,
            )
            candidate = BackgroundJob.create(
                id=job_id,
                authorization=reserved,
                idempotency_digest=idempotency_digest,
                max_attempts=self.max_attempts,
                created_at=now,
            )
        except ValueError:
            raise _invalid_request() from None
        try:
            submitted = self.job_store.submit(candidate)
        except JobStoreError as error:
            if error.code is JobStoreErrorCode.IDEMPOTENCY_CONFLICT:
                raise _idempotency_conflict() from None
            if error.code is JobStoreErrorCode.CAPACITY_EXCEEDED:
                raise _capacity_exceeded() from None
            raise _service_unavailable() from None
        if not _same_reserved_request(submitted.job, candidate):
            raise _service_unavailable()
        return submitted

    def _load_existing(
        self,
        *,
        principal: AuthenticatedPrincipal,
        idempotency_digest: str,
    ) -> BackgroundJob | None:
        try:
            return self.job_store.load_by_idempotency(
                principal.workspace_id,
                principal.actor_id,
                idempotency_digest,
            )
        except JobStoreError:
            raise _service_unavailable() from None


@dataclass(frozen=True, slots=True)
class InspectExecutionJob:
    """Inspect only a job visible through current tenant and role policy."""

    job_store: BackgroundJobApiStorePort
    authorization: WorkflowAuthorizationPort
    clock: WorkflowClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        job_id: str,
    ) -> BackgroundJob:
        return _load_visible_job(
            self.job_store,
            self.authorization,
            self.clock,
            job_id=job_id,
            principal=principal,
            permission=WorkflowPermission.VIEW,
        )


@dataclass(frozen=True, slots=True)
class CancelExecutionJob:
    """Request immediate or cooperative cancellation within the exact scope."""

    job_store: BackgroundJobApiStorePort
    authorization: WorkflowAuthorizationPort
    clock: WorkflowClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        job_id: str,
    ) -> BackgroundJob:
        now = self.clock.now()
        job, owner_filter = _load_visible_job_with_owner_filter(
            self.job_store,
            self.authorization,
            job_id=job_id,
            principal=principal,
            permission=WorkflowPermission.EXECUTE,
            at=now,
        )
        if job.is_terminal:
            return job
        try:
            cancelled = self.job_store.request_cancellation(
                principal.workspace_id,
                job_id,
                submitting_actor_id=owner_filter,
                requested_at=now,
            )
        except JobStoreError as error:
            if error.code is JobStoreErrorCode.STATE_CONFLICT:
                raced = _load_visible_job(
                    self.job_store,
                    self.authorization,
                    self.clock,
                    job_id=job_id,
                    principal=principal,
                    permission=WorkflowPermission.EXECUTE,
                )
                if raced.is_terminal or raced.status is JobStatus.CANCEL_REQUESTED:
                    return raced
            raise _service_unavailable() from None
        if cancelled is None or cancelled.id != job_id:
            raise _unavailable()
        return cancelled


def _load_visible_job(
    store: BackgroundJobApiStorePort,
    authorization: WorkflowAuthorizationPort,
    clock: WorkflowClockPort,
    *,
    job_id: str,
    principal: AuthenticatedPrincipal,
    permission: WorkflowPermission,
) -> BackgroundJob:
    job, _owner_filter = _load_visible_job_with_owner_filter(
        store,
        authorization,
        job_id=job_id,
        principal=principal,
        permission=permission,
        at=clock.now(),
    )
    return job


def _load_visible_job_with_owner_filter(
    store: BackgroundJobApiStorePort,
    authorization: WorkflowAuthorizationPort,
    *,
    job_id: str,
    principal: AuthenticatedPrincipal,
    permission: WorkflowPermission,
    at: datetime,
) -> tuple[BackgroundJob, str | None]:
    if _SAFE_JOB_ID.fullmatch(job_id) is None:
        raise _unavailable()
    try:
        owner_filter = authorization.owner_filter_for(
            principal,
            permission,
            at=at,
        )
        job = store.load(
            principal.workspace_id,
            job_id,
            submitting_actor_id=owner_filter,
        )
    except (AuthorizationError, ValueError):
        raise _unavailable() from None
    except JobStoreError:
        raise _service_unavailable() from None
    except Exception:
        raise _service_unavailable() from None
    if job is None or job.id != job_id:
        raise _unavailable()
    return job, owner_filter


def _is_exact_execution_checkpoint(
    draft: AgentWorkflowDraft,
    *,
    workflow_id: str,
    expected_workflow_revision: int,
    expected_plan_fingerprint: str,
) -> bool:
    checkpoint = draft.checkpoint
    return (
        draft.id == workflow_id
        and draft.revision == expected_workflow_revision
        and draft.stage is WorkflowStage.DECISION_REQUIRED
        and draft.execution is None
        and draft.plan_fingerprint == expected_plan_fingerprint
        and checkpoint is not None
        and checkpoint.kind is WorkflowCheckpointKind.EXECUTION_APPROVAL
        and checkpoint.fingerprint == expected_plan_fingerprint
    )


def _matches_replay(
    job: BackgroundJob,
    *,
    workflow_id: str,
    expected_workflow_revision: int,
    expected_plan_fingerprint: str,
    idempotency_digest: str,
) -> bool:
    reserved = job.authorization
    return (
        job.idempotency_digest == idempotency_digest
        and reserved.workflow_id == workflow_id
        and reserved.expected_workflow_revision == expected_workflow_revision
        and reserved.expected_plan_fingerprint == expected_plan_fingerprint
        and reserved.operation is JobKind.EXECUTE_WORKFLOW_PREVIEW
    )


def _same_reserved_request(actual: BackgroundJob, expected: BackgroundJob) -> bool:
    return (
        actual.id == expected.id
        and actual.kind is expected.kind
        and actual.idempotency_digest == expected.idempotency_digest
        and actual.request_fingerprint == expected.request_fingerprint
        and actual.authorization.payload_fingerprint == expected.authorization.payload_fingerprint
    )


def _idempotency_digest(raw_key: str) -> str:
    if not isinstance(raw_key, str) or _IDEMPOTENCY_KEY.fullmatch(raw_key) is None:
        raise _invalid_request()
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _deterministic_job_id(
    *,
    workspace_id: str,
    submitting_actor_id: str,
    idempotency_digest: str,
) -> str:
    encoded = json.dumps(
        {
            "idempotency_digest": idempotency_digest,
            "submitting_actor_id": submitting_actor_id,
            "workspace_id": workspace_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"execution-job-{hashlib.sha256(encoded).hexdigest()[:48]}"


def _invalid_request() -> ExecutionJobUseCaseError:
    return ExecutionJobUseCaseError(
        ExecutionJobUseCaseErrorCode.INVALID_REQUEST,
        "The execution-job request is invalid.",
    )


def _unavailable() -> ExecutionJobUseCaseError:
    return ExecutionJobUseCaseError(
        ExecutionJobUseCaseErrorCode.UNAVAILABLE,
        "The execution job is not available to the authenticated principal.",
    )


def _idempotency_conflict() -> ExecutionJobUseCaseError:
    return ExecutionJobUseCaseError(
        ExecutionJobUseCaseErrorCode.IDEMPOTENCY_CONFLICT,
        "The idempotency identity is already bound to another request.",
    )


def _service_unavailable() -> ExecutionJobUseCaseError:
    return ExecutionJobUseCaseError(
        ExecutionJobUseCaseErrorCode.SERVICE_UNAVAILABLE,
        "The execution-job service is temporarily unavailable.",
    )


def _capacity_exceeded() -> ExecutionJobUseCaseError:
    return ExecutionJobUseCaseError(
        ExecutionJobUseCaseErrorCode.CAPACITY_EXCEEDED,
        "The tenant execution-job capacity has been reached.",
    )
