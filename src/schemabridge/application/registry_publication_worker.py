"""One bounded iteration of the isolated generic registry publisher."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from schemabridge.application.ports.planning import (
    RegistryPublicationError as DataHubPublicationError,
)
from schemabridge.application.ports.planning import RegistryPublicationErrorCode as DataHubErrorCode
from schemabridge.application.ports.registry_publication import (
    ObservedRegistryPublisherPort,
    RegistryPublicationAuthorityError,
    RegistryPublicationAuthorityPort,
    RegistryPublicationHeartbeatSupervisorError,
    RegistryPublicationHeartbeatSupervisorPort,
    RegistryPublicationJobWorkerStorePort,
    RegistryPublicationStoreError,
)
from schemabridge.application.ports.semantic_onboarding import SemanticOnboardingClockPort
from schemabridge.domain.registry_publication import (
    ObservedRegistryPublicationResult,
    PublishableRegistryVersion,
    assemble_publishable_registry_version,
)
from schemabridge.domain.registry_publication_jobs import (
    RegistryPublicationFailureCode,
    RegistryPublicationJob,
    RegistryPublicationJobPhase,
    RegistryPublicationJobStatus,
    digest_registry_publication_lease_capability,
)

_SAFE_WORKER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,199}$")


class RegistryPublicationLeaseCapabilityFactoryPort(Protocol):
    def __call__(self) -> str:
        """Return a fresh transient high-entropy lease capability."""


class RegistryPublisherIterationOutcome(StrEnum):
    IDLE = "idle"
    CANDIDATE_READY = "candidate_ready"
    ACTIVATION_READY = "activation_ready"
    CANCELLED = "cancelled"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    DEAD_LETTERED = "dead_lettered"


@dataclass(frozen=True, slots=True)
class RegistryPublisherIterationResult:
    outcome: RegistryPublisherIterationOutcome
    job_id: str | None = None
    status: RegistryPublicationJobStatus | None = None
    failure_code: RegistryPublicationFailureCode | None = None


class RegistryPublisherWorkerErrorCode(StrEnum):
    STORE_UNAVAILABLE = "registry_publisher_store_unavailable"
    INVALID_CAPABILITY = "registry_publisher_invalid_capability"
    INVALID_CLAIM = "registry_publisher_invalid_claim"
    LEASE_LOST = "registry_publisher_lease_lost"


class RegistryPublisherWorkerError(RuntimeError):
    def __init__(self, code: RegistryPublisherWorkerErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RunOneRegistryPublisherWorker:
    jobs: RegistryPublicationJobWorkerStorePort
    authority: RegistryPublicationAuthorityPort
    publisher: ObservedRegistryPublisherPort
    clock: SemanticOnboardingClockPort
    capability_factory: RegistryPublicationLeaseCapabilityFactoryPort
    heartbeat_supervisor: RegistryPublicationHeartbeatSupervisorPort
    worker_id: str
    lease_duration: timedelta = timedelta(seconds=60)
    heartbeat_interval: timedelta = timedelta(seconds=20)
    reap_limit: int = 100

    def __post_init__(self) -> None:
        if _SAFE_WORKER_ID.fullmatch(self.worker_id) is None:
            raise ValueError("registry publisher worker id is invalid")
        if not timedelta(seconds=1) <= self.lease_duration <= timedelta(minutes=5):
            raise ValueError("registry publisher lease duration is outside its bound")
        if (
            self.heartbeat_interval < timedelta(milliseconds=10)
            or self.heartbeat_interval * 2 >= self.lease_duration
        ):
            raise ValueError("registry publisher heartbeat interval is unsafe")
        if not 1 <= self.reap_limit <= 1_000:
            raise ValueError("registry publisher reap limit is outside its bound")

    def execute(self) -> RegistryPublisherIterationResult:
        try:
            self.jobs.reap_expired_leases(limit=self.reap_limit)
        except RegistryPublicationStoreError as error:
            raise _store_unavailable() from error
        try:
            capability = self.capability_factory()
            capability_digest = digest_registry_publication_lease_capability(capability)
        except Exception:
            raise RegistryPublisherWorkerError(
                RegistryPublisherWorkerErrorCode.INVALID_CAPABILITY,
                "The registry publisher lease capability is invalid.",
            ) from None
        try:
            claim = self.jobs.claim_next(
                worker_id=self.worker_id,
                lease_capability=capability,
                lease_duration=self.lease_duration,
            )
        except RegistryPublicationStoreError as error:
            raise _store_unavailable() from error
        if claim is None:
            return RegistryPublisherIterationResult(
                outcome=RegistryPublisherIterationOutcome.IDLE,
            )
        if not _claim_matches(claim, self.worker_id, capability_digest, self.clock.now()):
            raise RegistryPublisherWorkerError(
                RegistryPublisherWorkerErrorCode.INVALID_CLAIM,
                "The publication store returned an invalid worker claim.",
            )
        if claim.status is RegistryPublicationJobStatus.CANCEL_REQUESTED:
            if claim.phase is RegistryPublicationJobPhase.PUBLISH:
                return self._recover_cancelled_publish(claim, capability)
            try:
                cancelled = self.jobs.acknowledge_cancellation(
                    claim.id,
                    worker_id=self.worker_id,
                    lease_capability=capability,
                    fencing_token=claim.last_fencing_token,
                )
            except RegistryPublicationStoreError as error:
                raise _store_unavailable() from error
            return _iteration_result(cancelled)
        if claim.phase is RegistryPublicationJobPhase.PREPARE:
            return self._prepare(claim, capability)
        return self._publish(claim, capability)

    def _prepare(
        self,
        claim: RegistryPublicationJob,
        capability: str,
    ) -> RegistryPublisherIterationResult:
        def operation() -> PublishableRegistryVersion:
            base = self.authority.resolve_base(claim.proposal)
            return assemble_publishable_registry_version(claim.proposal, base=base)

        try:
            candidate, refreshed = self.heartbeat_supervisor.run(
                claim=claim,
                worker_id=self.worker_id,
                lease_capability=capability,
                lease_duration=self.lease_duration,
                heartbeat_interval=self.heartbeat_interval,
                operation=operation,
            )
            self._require_refreshed_claim(
                claim,
                refreshed,
                digest_registry_publication_lease_capability(capability),
            )
            if refreshed.status is RegistryPublicationJobStatus.CANCEL_REQUESTED:
                cancelled = self.jobs.acknowledge_cancellation(
                    claim.id,
                    worker_id=self.worker_id,
                    lease_capability=capability,
                    fencing_token=refreshed.last_fencing_token,
                )
                return _iteration_result(cancelled)
            ready = self.jobs.record_candidate(
                claim.id,
                candidate,
                worker_id=self.worker_id,
                lease_capability=capability,
                fencing_token=refreshed.last_fencing_token,
            )
            return _iteration_result(ready)
        except RegistryPublicationAuthorityError as error:
            return self._fail(claim, capability, error.code)
        except ValueError:
            return self._fail(
                claim,
                capability,
                RegistryPublicationFailureCode.CANDIDATE_INVALID,
            )
        except RegistryPublicationHeartbeatSupervisorError as error:
            raise RegistryPublisherWorkerError(
                RegistryPublisherWorkerErrorCode.LEASE_LOST,
                "The registry publisher lease could not be maintained.",
            ) from error
        except RegistryPublicationStoreError as error:
            raise _store_unavailable() from error
        except Exception:
            return self._fail(
                claim,
                capability,
                RegistryPublicationFailureCode.UNEXPECTED_WORKER_FAILURE,
            )

    def _publish(
        self,
        claim: RegistryPublicationJob,
        capability: str,
    ) -> RegistryPublisherIterationResult:
        candidate = claim.candidate
        authorization = claim.authorization
        if candidate is None or authorization is None:
            return self._fail(
                claim,
                capability,
                RegistryPublicationFailureCode.AUTHORIZATION_MISMATCH,
            )

        def operation() -> ObservedRegistryPublicationResult:
            try:
                observed = self.publisher.observe(
                    candidate,
                    authorization,
                    observed_at=self.clock.now(),
                )
            except DataHubPublicationError as error:
                raise RegistryPublicationAuthorityError(
                    _observation_error_code(error),
                    "registry publication target observation failed",
                ) from error
            if observed.receipt is not None or observed.reason_code != "target_absent":
                return observed
            if not authorization.is_current(self.clock.now()):
                raise RegistryPublicationAuthorityError(
                    RegistryPublicationFailureCode.AUTHORIZATION_EXPIRED,
                    "registry publication authorization expired before a new write",
                )
            base = self.authority.resolve_base(claim.proposal)
            rebuilt = assemble_publishable_registry_version(claim.proposal, base=base)
            if rebuilt != candidate:
                raise RegistryPublicationAuthorityError(
                    RegistryPublicationFailureCode.CANDIDATE_INVALID,
                    "registry publication candidate changed after authorization",
                )
            prewrite_claim = self.jobs.heartbeat(
                claim.id,
                worker_id=self.worker_id,
                lease_capability=capability,
                fencing_token=claim.last_fencing_token,
                lease_duration=self.lease_duration,
            )
            self._require_refreshed_claim(
                claim,
                prewrite_claim,
                digest_registry_publication_lease_capability(capability),
            )
            if prewrite_claim.status is RegistryPublicationJobStatus.CANCEL_REQUESTED:
                # This heartbeat is the explicit external-write boundary. A
                # cancellation observed before it performs no DataHub write;
                # after it, exact read-back remains authoritative.
                return observed
            return self.publisher.publish(
                candidate,
                authorization,
                observed_at=self.clock.now(),
            )

        try:
            result, refreshed = self.heartbeat_supervisor.run(
                claim=claim,
                worker_id=self.worker_id,
                lease_capability=capability,
                lease_duration=self.lease_duration,
                heartbeat_interval=self.heartbeat_interval,
                operation=operation,
            )
            self._require_refreshed_claim(
                claim,
                refreshed,
                digest_registry_publication_lease_capability(capability),
            )
            if (
                refreshed.status is RegistryPublicationJobStatus.CANCEL_REQUESTED
                and result.receipt is None
                and result.reason_code == "target_absent"
            ):
                cancelled = self.jobs.acknowledge_cancellation(
                    claim.id,
                    worker_id=self.worker_id,
                    lease_capability=capability,
                    fencing_token=refreshed.last_fencing_token,
                )
                return _iteration_result(cancelled)
            if result.receipt is None:
                return self._fail(
                    refreshed,
                    capability,
                    _failed_result_code(result),
                )
            completed = self.jobs.complete(
                claim.id,
                result.receipt,
                worker_id=self.worker_id,
                lease_capability=capability,
                fencing_token=refreshed.last_fencing_token,
            )
            return _iteration_result(completed)
        except RegistryPublicationAuthorityError as error:
            return self._fail(claim, capability, error.code)
        except DataHubPublicationError as error:
            code = _datahub_error_code(error)
            if error.code is DataHubErrorCode.APPROVAL_MISMATCH and not authorization.is_current(
                self.clock.now()
            ):
                code = RegistryPublicationFailureCode.AUTHORIZATION_EXPIRED
            return self._fail(claim, capability, code)
        except RegistryPublicationHeartbeatSupervisorError as error:
            raise RegistryPublisherWorkerError(
                RegistryPublisherWorkerErrorCode.LEASE_LOST,
                "The registry publisher lease could not be maintained.",
            ) from error
        except RegistryPublicationStoreError as error:
            raise _store_unavailable() from error
        except ValueError:
            return self._fail(
                claim,
                capability,
                RegistryPublicationFailureCode.CANDIDATE_INVALID,
            )
        except Exception:
            return self._fail(
                claim,
                capability,
                RegistryPublicationFailureCode.READBACK_REQUIRED,
            )

    def _recover_cancelled_publish(
        self,
        claim: RegistryPublicationJob,
        capability: str,
    ) -> RegistryPublisherIterationResult:
        candidate = claim.candidate
        authorization = claim.authorization
        if candidate is None or authorization is None:
            return self._fail(
                claim,
                capability,
                RegistryPublicationFailureCode.AUTHORIZATION_MISMATCH,
            )

        def operation() -> ObservedRegistryPublicationResult:
            return self.publisher.observe(
                candidate,
                authorization,
                observed_at=self.clock.now(),
            )

        try:
            result, refreshed = self.heartbeat_supervisor.run(
                claim=claim,
                worker_id=self.worker_id,
                lease_capability=capability,
                lease_duration=self.lease_duration,
                heartbeat_interval=self.heartbeat_interval,
                operation=operation,
            )
            self._require_refreshed_claim(
                claim,
                refreshed,
                digest_registry_publication_lease_capability(capability),
            )
            if result.receipt is not None:
                completed = self.jobs.complete(
                    claim.id,
                    result.receipt,
                    worker_id=self.worker_id,
                    lease_capability=capability,
                    fencing_token=refreshed.last_fencing_token,
                )
                return _iteration_result(completed)
            if result.reason_code == "target_absent":
                cancelled = self.jobs.acknowledge_cancellation(
                    claim.id,
                    worker_id=self.worker_id,
                    lease_capability=capability,
                    fencing_token=refreshed.last_fencing_token,
                )
                return _iteration_result(cancelled)
            return self._fail(
                refreshed,
                capability,
                RegistryPublicationFailureCode.READBACK_REQUIRED,
            )
        except DataHubPublicationError as error:
            return self._fail(claim, capability, _observation_error_code(error))
        except RegistryPublicationHeartbeatSupervisorError as error:
            raise RegistryPublisherWorkerError(
                RegistryPublisherWorkerErrorCode.LEASE_LOST,
                "The registry publisher lease could not be maintained.",
            ) from error
        except RegistryPublicationStoreError as error:
            raise _store_unavailable() from error
        except Exception:
            return self._fail(
                claim,
                capability,
                RegistryPublicationFailureCode.READBACK_REQUIRED,
            )

    def _require_refreshed_claim(
        self,
        original: RegistryPublicationJob,
        refreshed: RegistryPublicationJob,
        capability_digest: str,
    ) -> None:
        if (
            refreshed.id != original.id
            or refreshed.scope != original.scope
            or refreshed.proposal.fingerprint != original.proposal.fingerprint
            or refreshed.last_fencing_token != original.last_fencing_token
            or not _claim_matches(
                refreshed,
                self.worker_id,
                capability_digest,
                self.clock.now(),
            )
        ):
            raise RegistryPublicationHeartbeatSupervisorError(
                "registry publisher heartbeat returned another claim"
            )

    def _fail(
        self,
        claim: RegistryPublicationJob,
        capability: str,
        code: RegistryPublicationFailureCode,
    ) -> RegistryPublisherIterationResult:
        try:
            failed = self.jobs.fail(
                claim.id,
                code,
                worker_id=self.worker_id,
                lease_capability=capability,
                fencing_token=claim.last_fencing_token,
            )
        except RegistryPublicationStoreError as error:
            raise _store_unavailable() from error
        return _iteration_result(failed)


def _claim_matches(
    claim: RegistryPublicationJob,
    worker_id: str,
    capability_digest: str,
    at: datetime,
) -> bool:
    lease = claim.lease
    return (
        claim.status
        in {
            RegistryPublicationJobStatus.LEASED,
            RegistryPublicationJobStatus.CANCEL_REQUESTED,
        }
        and lease is not None
        and lease.worker_id == worker_id
        and lease.token_digest == capability_digest
        and lease.fencing_token == claim.last_fencing_token
        and lease.is_current(at)
    )


def _datahub_error_code(error: DataHubPublicationError) -> RegistryPublicationFailureCode:
    return {
        DataHubErrorCode.CATALOG_UNAVAILABLE: RegistryPublicationFailureCode.DATAHUB_UNAVAILABLE,
        DataHubErrorCode.CATALOG_PERMISSION_DENIED: (
            RegistryPublicationFailureCode.DATAHUB_PERMISSION_DENIED
        ),
        DataHubErrorCode.CONFLICT: RegistryPublicationFailureCode.TARGET_CONFLICT,
        DataHubErrorCode.APPROVAL_REQUIRED: RegistryPublicationFailureCode.AUTHORIZATION_MISMATCH,
        DataHubErrorCode.APPROVAL_MISMATCH: RegistryPublicationFailureCode.AUTHORIZATION_MISMATCH,
        DataHubErrorCode.PAYLOAD_INVALID: RegistryPublicationFailureCode.CANDIDATE_INVALID,
        DataHubErrorCode.INVALID_RESPONSE: RegistryPublicationFailureCode.READBACK_REQUIRED,
        DataHubErrorCode.AUDIT_UNAVAILABLE: RegistryPublicationFailureCode.READBACK_REQUIRED,
    }[error.code]


def _observation_error_code(
    error: DataHubPublicationError,
) -> RegistryPublicationFailureCode:
    if error.code is DataHubErrorCode.CONFLICT:
        return RegistryPublicationFailureCode.TARGET_CONFLICT
    if error.code is DataHubErrorCode.APPROVAL_MISMATCH:
        return RegistryPublicationFailureCode.AUTHORIZATION_MISMATCH
    return RegistryPublicationFailureCode.READBACK_REQUIRED


def _failed_result_code(
    result: ObservedRegistryPublicationResult,
) -> RegistryPublicationFailureCode:
    # Failed results are emitted only after the adapter entered its external
    # write/read-back block.  Even a permission-looking transport failure can
    # therefore be a committed write whose response was lost.
    del result
    return RegistryPublicationFailureCode.READBACK_REQUIRED


def _iteration_result(job: RegistryPublicationJob) -> RegistryPublisherIterationResult:
    outcome = {
        RegistryPublicationJobStatus.AWAITING_APPROVAL: (
            RegistryPublisherIterationOutcome.CANDIDATE_READY
        ),
        RegistryPublicationJobStatus.ACTIVATION_READY: (
            RegistryPublisherIterationOutcome.ACTIVATION_READY
        ),
        RegistryPublicationJobStatus.CANCELLED: RegistryPublisherIterationOutcome.CANCELLED,
        RegistryPublicationJobStatus.RETRY_WAIT: (
            RegistryPublisherIterationOutcome.RETRY_SCHEDULED
        ),
        RegistryPublicationJobStatus.FAILED: RegistryPublisherIterationOutcome.FAILED,
        RegistryPublicationJobStatus.DEAD_LETTERED: (
            RegistryPublisherIterationOutcome.DEAD_LETTERED
        ),
    }.get(job.status)
    if outcome is None:
        raise RegistryPublisherWorkerError(
            RegistryPublisherWorkerErrorCode.INVALID_CLAIM,
            "The registry publisher produced an unsupported job state.",
        )
    return RegistryPublisherIterationResult(
        outcome=outcome,
        job_id=job.id,
        status=job.status,
        failure_code=job.failure_code,
    )


def _store_unavailable() -> RegistryPublisherWorkerError:
    return RegistryPublisherWorkerError(
        RegistryPublisherWorkerErrorCode.STORE_UNAVAILABLE,
        "The registry publisher store is temporarily unavailable.",
    )


__all__ = [
    "RegistryPublicationLeaseCapabilityFactoryPort",
    "RegistryPublisherIterationOutcome",
    "RegistryPublisherIterationResult",
    "RegistryPublisherWorkerError",
    "RegistryPublisherWorkerErrorCode",
    "RunOneRegistryPublisherWorker",
]
