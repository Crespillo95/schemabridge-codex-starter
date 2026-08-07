"""One bounded, fenced semantic-change reconciler iteration."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from schemabridge.application.ports.semantic_change_scans import (
    SemanticChangeScanClockPort,
    SemanticChangeScanInspection,
    SemanticChangeScanRunnerError,
    SemanticChangeScanRunnerErrorCode,
    SemanticChangeScanRunnerPort,
    SemanticChangeScanStoreError,
    SemanticChangeScanStoreErrorCode,
    SemanticChangeScanStorePort,
)
from schemabridge.domain.semantic_change import (
    SemanticChangeReport,
    SemanticEvidenceObservation,
)
from schemabridge.domain.semantic_change_scans import (
    SemanticChangeScanFailureCode,
    SemanticChangeScanFailureDisposition,
    SemanticChangeScanRequest,
    SemanticChangeScanSourceKind,
    SemanticChangeScanStatus,
    classify_semantic_change_scan_failure,
    digest_semantic_change_scan_capability,
    semantic_change_scan_claim_matches,
    semantic_change_scan_retry_delay,
)

_SAFE_RECONCILER_ID = re.compile(r"^[a-z][a-z0-9_-]{2,199}$")


def _never_stop() -> bool:
    return False


class SemanticChangeReconcilerIterationOutcome(StrEnum):
    """Sanitized outcomes for one bounded queue iteration."""

    IDLE = "idle"
    COMPLETED = "completed"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class SemanticChangeReconcilerIterationResult:
    """Observable process result without capability, source evidence, or exception text."""

    outcome: SemanticChangeReconcilerIterationOutcome
    scan_id: str | None = None
    status: SemanticChangeScanStatus | None = None
    report_id: str | None = None
    failure_code: SemanticChangeScanFailureCode | None = None
    attempts: int = 0

    def __post_init__(self) -> None:
        if not 0 <= self.attempts <= 100:
            raise ValueError("semantic reconciler result attempt count is invalid")
        if self.outcome is SemanticChangeReconcilerIterationOutcome.IDLE:
            if (
                self.scan_id is not None
                or self.status is not None
                or self.report_id is not None
                or self.failure_code is not None
                or self.attempts
            ):
                raise ValueError("idle semantic reconciler result cannot expose scan state")
        elif self.outcome is SemanticChangeReconcilerIterationOutcome.COMPLETED:
            if (
                self.scan_id is None
                or self.status is not SemanticChangeScanStatus.COMPLETED
                or self.report_id is None
                or self.failure_code is not None
                or self.attempts < 1
            ):
                raise ValueError("completed semantic reconciler result is inconsistent")
        elif self.outcome is SemanticChangeReconcilerIterationOutcome.SUPERSEDED:
            if (
                self.scan_id is None
                or self.status is not SemanticChangeScanStatus.SUPERSEDED
                or self.report_id is not None
                or self.failure_code is not None
            ):
                raise ValueError("superseded semantic reconciler result is inconsistent")
        elif self.outcome is SemanticChangeReconcilerIterationOutcome.STOPPED:
            if self.scan_id is None:
                if (
                    self.status is not None
                    or self.report_id is not None
                    or self.failure_code is not None
                    or self.attempts
                ):
                    raise ValueError("unclaimed stopped result cannot expose scan state")
            elif (
                self.status
                not in {
                    SemanticChangeScanStatus.RETRY_WAIT,
                    SemanticChangeScanStatus.FAILED,
                }
                or self.failure_code is not SemanticChangeScanFailureCode.SHUTDOWN_REQUESTED
                or self.report_id is not None
            ):
                raise ValueError("claimed stopped result must retain sanitized shutdown failure")
        elif self.outcome is SemanticChangeReconcilerIterationOutcome.RETRY_SCHEDULED:
            if (
                self.scan_id is None
                or self.status is not SemanticChangeScanStatus.RETRY_WAIT
                or self.failure_code is None
                or self.report_id is not None
            ):
                raise ValueError("retry semantic reconciler result is inconsistent")
        elif (
            self.scan_id is None
            or self.status is not SemanticChangeScanStatus.FAILED
            or self.failure_code is None
            or self.report_id is not None
        ):
            raise ValueError("failed semantic reconciler result is inconsistent")


class SemanticChangeReconcilerUseCaseErrorCode(StrEnum):
    """Failures where no trustworthy lifecycle transition can be claimed."""

    STORE_UNAVAILABLE = "semantic_reconciler_store_unavailable"
    INVALID_CAPABILITY = "semantic_reconciler_invalid_capability"
    INVALID_CLAIM = "semantic_reconciler_invalid_claim"
    LEASE_LOST = "semantic_reconciler_lease_lost"
    INVALID_INSPECTION = "semantic_reconciler_invalid_inspection"
    INVALID_STOP_SIGNAL = "semantic_reconciler_invalid_stop_signal"


class SemanticChangeReconcilerUseCaseError(RuntimeError):
    """Sanitized worker failure without infrastructure or evidence detail."""

    def __init__(
        self,
        code: SemanticChangeReconcilerUseCaseErrorCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RunOneSemanticChangeScan:
    """Claim, inspect, and atomically complete at most one durable scan."""

    scans: SemanticChangeScanStorePort = field(repr=False)
    runner: SemanticChangeScanRunnerPort = field(repr=False)
    clock: SemanticChangeScanClockPort = field(repr=False)
    capability_factory: Callable[[], str] = field(repr=False)
    reconciler_id: str
    lease_duration: timedelta = timedelta(seconds=60)
    retention: timedelta = timedelta(days=30)
    maintenance_batch_size: int = 100
    stop_requested: Callable[[], bool] = field(default=_never_stop, repr=False)

    def __post_init__(self) -> None:
        if _SAFE_RECONCILER_ID.fullmatch(self.reconciler_id) is None:
            raise ValueError("semantic reconciler id must be a bounded inert identifier")
        if not timedelta(seconds=10) <= self.lease_duration <= timedelta(minutes=5):
            raise ValueError("semantic reconciler lease duration is outside the supported bound")
        if not timedelta(days=1) <= self.retention <= timedelta(days=366):
            raise ValueError("semantic scan retention is outside the supported bound")
        if not 1 <= self.maintenance_batch_size <= 1_000:
            raise ValueError("semantic scan maintenance batch size is outside the supported bound")

    def execute(self) -> SemanticChangeReconcilerIterationResult:
        """Run one non-recursive at-least-once delivery attempt."""

        if self._stopping():
            return SemanticChangeReconcilerIterationResult(
                outcome=SemanticChangeReconcilerIterationOutcome.STOPPED,
            )
        self._maintain()
        capability = self._new_capability()
        try:
            claimed = self.scans.claim_next(
                reconciler_id=self.reconciler_id,
                lease_capability=capability,
                lease_duration=self.lease_duration,
            )
        except SemanticChangeScanStoreError as error:
            raise _store_failure(error) from None
        except Exception:
            raise _store_unavailable() from None
        if claimed is None:
            return SemanticChangeReconcilerIterationResult(
                outcome=SemanticChangeReconcilerIterationOutcome.IDLE,
            )
        claimed = self._validate_owned_claim(claimed, capability)
        current = self._supersede_if_obsolete(claimed, capability)
        if current.status is SemanticChangeScanStatus.SUPERSEDED:
            return _superseded_result(current)
        if self._stopping():
            return self._record_failure(
                current,
                capability,
                SemanticChangeScanFailureCode.SHUTDOWN_REQUESTED,
            )
        current = self._heartbeat(current, capability)

        latest = current

        def should_continue() -> bool:
            nonlocal latest
            if self._stopping():
                return False
            latest = self._reload_owned(latest, capability)
            latest = self._heartbeat(latest, capability)
            return True

        try:
            inspection = self.runner.inspect(
                current,
                should_continue=should_continue,
            )
        except SemanticChangeReconcilerUseCaseError:
            raise
        except SemanticChangeScanRunnerError as error:
            return self._record_failure(
                latest,
                capability,
                _runner_failure_code(error.code),
            )
        except Exception:
            return self._record_failure(
                latest,
                capability,
                SemanticChangeScanFailureCode.UNEXPECTED_RECONCILER_FAILURE,
            )
        if self._stopping():
            return self._record_failure(
                latest,
                capability,
                SemanticChangeScanFailureCode.SHUTDOWN_REQUESTED,
            )
        latest = self._supersede_if_obsolete(latest, capability)
        if latest.status is SemanticChangeScanStatus.SUPERSEDED:
            return _superseded_result(latest)
        latest = self._heartbeat(latest, capability)
        try:
            inspection = self._validate_inspection(latest, inspection)
        except SemanticChangeReconcilerUseCaseError as error:
            if error.code is not SemanticChangeReconcilerUseCaseErrorCode.INVALID_INSPECTION:
                raise
            return self._record_failure(
                latest,
                capability,
                SemanticChangeScanFailureCode.INSPECTION_INVALID,
            )
        return self._complete(latest, capability, inspection)

    def _maintain(self) -> None:
        try:
            reclaimed = self.scans.reclaim_expired(
                limit=self.maintenance_batch_size,
                retention=self.retention,
            )
            superseded = self.scans.supersede_obsolete(
                limit=self.maintenance_batch_size,
                retention=self.retention,
            )
        except SemanticChangeScanStoreError as error:
            raise _store_failure(error) from None
        except Exception:
            raise _store_unavailable() from None
        for count in (reclaimed, superseded):
            if not 0 <= count <= self.maintenance_batch_size:
                raise SemanticChangeReconcilerUseCaseError(
                    SemanticChangeReconcilerUseCaseErrorCode.INVALID_CLAIM,
                    "semantic scan maintenance returned an invalid count",
                )

    def _new_capability(self) -> str:
        try:
            capability = self.capability_factory()
            digest_semantic_change_scan_capability(capability)
        except Exception:
            raise SemanticChangeReconcilerUseCaseError(
                SemanticChangeReconcilerUseCaseErrorCode.INVALID_CAPABILITY,
                "semantic scan capability generation failed",
            ) from None
        return capability

    def _validate_owned_claim(
        self,
        claimed: SemanticChangeScanRequest,
        capability: str,
    ) -> SemanticChangeScanRequest:
        checked = _validated_request(claimed)
        if (
            checked.status is not SemanticChangeScanStatus.LEASED
            or checked.lease is None
            or not semantic_change_scan_claim_matches(
                checked,
                reconciler_id=self.reconciler_id,
                lease_capability=capability,
                fencing_token=checked.fencing_token,
                at=self._now(),
            )
        ):
            raise SemanticChangeReconcilerUseCaseError(
                SemanticChangeReconcilerUseCaseErrorCode.INVALID_CLAIM,
                "semantic scan store returned an invalid claim",
            )
        return checked

    def _reload_owned(
        self,
        expected: SemanticChangeScanRequest,
        capability: str,
    ) -> SemanticChangeScanRequest:
        try:
            current = self.scans.load(expected.workspace_id, expected.scan_id)
        except SemanticChangeScanStoreError as error:
            raise _store_failure(error) from None
        except Exception:
            raise _store_unavailable() from None
        if current is None:
            raise SemanticChangeReconcilerUseCaseError(
                SemanticChangeReconcilerUseCaseErrorCode.LEASE_LOST,
                "semantic scan lease was lost",
            )
        checked = _validated_request(current)
        if (
            not _same_scan_identity(checked, expected)
            or checked.attempts != expected.attempts
            or checked.fencing_token != expected.fencing_token
            or not semantic_change_scan_claim_matches(
                checked,
                reconciler_id=self.reconciler_id,
                lease_capability=capability,
                fencing_token=expected.fencing_token,
                at=self._now(),
            )
        ):
            raise SemanticChangeReconcilerUseCaseError(
                SemanticChangeReconcilerUseCaseErrorCode.LEASE_LOST,
                "semantic scan lease was lost",
            )
        return checked

    def _heartbeat(
        self,
        expected: SemanticChangeScanRequest,
        capability: str,
    ) -> SemanticChangeScanRequest:
        if expected.lease is None:
            raise SemanticChangeReconcilerUseCaseError(
                SemanticChangeReconcilerUseCaseErrorCode.LEASE_LOST,
                "semantic scan lease was lost",
            )
        try:
            current = self.scans.heartbeat(
                expected.workspace_id,
                expected.scan_id,
                reconciler_id=self.reconciler_id,
                lease_capability=capability,
                fencing_token=expected.fencing_token,
                lease_duration=self.lease_duration,
            )
        except SemanticChangeScanStoreError as error:
            raise _store_failure(error) from None
        except Exception:
            raise _store_unavailable() from None
        checked = _validated_request(current)
        if (
            not _same_scan_identity(checked, expected)
            or checked.attempts != expected.attempts
            or checked.fencing_token != expected.fencing_token
            or checked.lease is None
            or checked.lease.heartbeat_at < expected.lease.heartbeat_at
            or not semantic_change_scan_claim_matches(
                checked,
                reconciler_id=self.reconciler_id,
                lease_capability=capability,
                fencing_token=expected.fencing_token,
                at=self._now(),
            )
        ):
            raise SemanticChangeReconcilerUseCaseError(
                SemanticChangeReconcilerUseCaseErrorCode.LEASE_LOST,
                "semantic scan heartbeat returned another claim",
            )
        return checked

    def _supersede_if_obsolete(
        self,
        expected: SemanticChangeScanRequest,
        capability: str,
    ) -> SemanticChangeScanRequest:
        try:
            current = self.scans.supersede_if_obsolete(
                expected.workspace_id,
                expected.scan_id,
                reconciler_id=self.reconciler_id,
                lease_capability=capability,
                fencing_token=expected.fencing_token,
                retention=self.retention,
            )
        except SemanticChangeScanStoreError as error:
            raise _store_failure(error) from None
        except Exception:
            raise _store_unavailable() from None
        checked = _validated_request(current)
        if not _same_scan_identity(checked, expected):
            raise SemanticChangeReconcilerUseCaseError(
                SemanticChangeReconcilerUseCaseErrorCode.INVALID_CLAIM,
                "semantic scan supersession returned another request",
            )
        if checked.status is SemanticChangeScanStatus.SUPERSEDED:
            return checked
        if (
            checked.status is not SemanticChangeScanStatus.LEASED
            or checked.attempts != expected.attempts
            or checked.fencing_token != expected.fencing_token
            or not semantic_change_scan_claim_matches(
                checked,
                reconciler_id=self.reconciler_id,
                lease_capability=capability,
                fencing_token=expected.fencing_token,
                at=self._now(),
            )
        ):
            raise SemanticChangeReconcilerUseCaseError(
                SemanticChangeReconcilerUseCaseErrorCode.LEASE_LOST,
                "semantic scan supersession check lost its lease",
            )
        return checked

    def _validate_inspection(
        self,
        request: SemanticChangeScanRequest,
        inspection: object,
    ) -> SemanticChangeScanInspection:
        if not isinstance(inspection, SemanticChangeScanInspection):
            raise SemanticChangeReconcilerUseCaseError(
                SemanticChangeReconcilerUseCaseErrorCode.INVALID_INSPECTION,
                "semantic scan runner returned an invalid inspection",
            )
        try:
            report = SemanticChangeReport.model_validate(
                inspection.report.model_dump(mode="python", warnings=False)
            )
            observation = SemanticEvidenceObservation.model_validate(
                inspection.observation.model_dump(mode="python", warnings=False)
            )
            checked = SemanticChangeScanInspection(
                report=report,
                observation=observation,
            )
        except (AttributeError, TypeError, ValueError):
            raise SemanticChangeReconcilerUseCaseError(
                SemanticChangeReconcilerUseCaseErrorCode.INVALID_INSPECTION,
                "semantic scan runner returned an invalid inspection",
            ) from None
        if checked != inspection or not _inspection_matches_request(request, checked):
            raise SemanticChangeReconcilerUseCaseError(
                SemanticChangeReconcilerUseCaseErrorCode.INVALID_INSPECTION,
                "semantic scan inspection does not match its durable trigger",
            )
        return checked

    def _complete(
        self,
        request: SemanticChangeScanRequest,
        capability: str,
        inspection: SemanticChangeScanInspection,
    ) -> SemanticChangeReconcilerIterationResult:
        completed_at = self._now()
        retain_until = completed_at + self.retention
        try:
            completed = self.scans.complete(
                request.workspace_id,
                request.scan_id,
                reconciler_id=self.reconciler_id,
                lease_capability=capability,
                fencing_token=request.fencing_token,
                inspection=inspection,
                completed_at=completed_at,
                retain_until=retain_until,
            )
        except SemanticChangeScanStoreError as error:
            recovered = self._recover_completed(request, inspection)
            if recovered is not None:
                completed = recovered
            else:
                raise _store_failure(error) from None
        except Exception:
            recovered = self._recover_completed(request, inspection)
            if recovered is not None:
                completed = recovered
            else:
                raise _store_unavailable() from None
        checked = _validated_request(completed)
        if (
            not _same_scan_identity(checked, request)
            or checked.status is not SemanticChangeScanStatus.COMPLETED
            or checked.completion is None
            or checked.completion.report_id != inspection.report.id
            or checked.completion.report_fingerprint != inspection.report.fingerprint
            or checked.attempts != request.attempts
            or checked.fencing_token != request.fencing_token
        ):
            raise SemanticChangeReconcilerUseCaseError(
                SemanticChangeReconcilerUseCaseErrorCode.INVALID_CLAIM,
                "semantic scan completion returned invalid state",
            )
        return SemanticChangeReconcilerIterationResult(
            outcome=SemanticChangeReconcilerIterationOutcome.COMPLETED,
            scan_id=checked.scan_id,
            status=checked.status,
            report_id=checked.completion.report_id,
            attempts=checked.attempts,
        )

    def _recover_completed(
        self,
        request: SemanticChangeScanRequest,
        inspection: SemanticChangeScanInspection,
    ) -> SemanticChangeScanRequest | None:
        try:
            current = self.scans.load(request.workspace_id, request.scan_id)
        except Exception:
            return None
        if (
            current is not None
            and current.status is SemanticChangeScanStatus.COMPLETED
            and current.completion is not None
            and current.completion.report_id == inspection.report.id
            and current.completion.report_fingerprint == inspection.report.fingerprint
        ):
            return current
        return None

    def _record_failure(
        self,
        request: SemanticChangeScanRequest,
        capability: str,
        code: SemanticChangeScanFailureCode,
    ) -> SemanticChangeReconcilerIterationResult:
        failed_at = self._now()
        disposition = classify_semantic_change_scan_failure(
            code,
            attempt=request.attempts,
            max_attempts=request.max_attempts,
        )
        retry_at = (
            failed_at + semantic_change_scan_retry_delay(request.attempts)
            if disposition is SemanticChangeScanFailureDisposition.RETRY
            else None
        )
        retain_until = (
            None
            if disposition is SemanticChangeScanFailureDisposition.RETRY
            else failed_at + self.retention
        )
        try:
            failed = self.scans.fail(
                request.workspace_id,
                request.scan_id,
                reconciler_id=self.reconciler_id,
                lease_capability=capability,
                fencing_token=request.fencing_token,
                code=code,
                failed_at=failed_at,
                retry_at=retry_at,
                retain_until=retain_until,
            )
        except SemanticChangeScanStoreError as error:
            raise _store_failure(error) from None
        except Exception:
            raise _store_unavailable() from None
        checked = _validated_request(failed)
        expected_status = (
            SemanticChangeScanStatus.RETRY_WAIT
            if disposition is SemanticChangeScanFailureDisposition.RETRY
            else SemanticChangeScanStatus.FAILED
        )
        if (
            not _same_scan_identity(checked, request)
            or checked.status is not expected_status
            or checked.failure is None
            or checked.failure.code is not code
            or checked.attempts != request.attempts
            or checked.fencing_token != request.fencing_token
        ):
            raise SemanticChangeReconcilerUseCaseError(
                SemanticChangeReconcilerUseCaseErrorCode.INVALID_CLAIM,
                "semantic scan failure returned invalid state",
            )
        outcome = (
            SemanticChangeReconcilerIterationOutcome.RETRY_SCHEDULED
            if checked.status is SemanticChangeScanStatus.RETRY_WAIT
            else SemanticChangeReconcilerIterationOutcome.FAILED
        )
        if code is SemanticChangeScanFailureCode.SHUTDOWN_REQUESTED:
            outcome = SemanticChangeReconcilerIterationOutcome.STOPPED
        return SemanticChangeReconcilerIterationResult(
            outcome=outcome,
            scan_id=checked.scan_id,
            status=checked.status,
            failure_code=code,
            attempts=checked.attempts,
        )

    def _stopping(self) -> bool:
        try:
            value = self.stop_requested()
        except Exception:
            raise SemanticChangeReconcilerUseCaseError(
                SemanticChangeReconcilerUseCaseErrorCode.INVALID_STOP_SIGNAL,
                "semantic reconciler stop signal is unavailable",
            ) from None
        if not isinstance(value, bool):
            raise SemanticChangeReconcilerUseCaseError(
                SemanticChangeReconcilerUseCaseErrorCode.INVALID_STOP_SIGNAL,
                "semantic reconciler stop signal is invalid",
            )
        return value

    def _now(self) -> datetime:
        try:
            value = self.clock.now()
        except Exception:
            raise _store_unavailable() from None
        if value.tzinfo is None or value.utcoffset() is None:
            raise SemanticChangeReconcilerUseCaseError(
                SemanticChangeReconcilerUseCaseErrorCode.STORE_UNAVAILABLE,
                "semantic reconciler clock returned an invalid time",
            )
        return value


def _inspection_matches_request(
    request: SemanticChangeScanRequest,
    inspection: SemanticChangeScanInspection,
) -> bool:
    report = inspection.report
    if report.context.scope.workspace_id != request.workspace_id:
        return False
    if request.source_kind is SemanticChangeScanSourceKind.REGISTRY_POINTER:
        return (
            report.context.scope.catalog_scope == request.catalog_scope
            and report.context.scope.registry_id == request.registry_id
            and report.context.pointer_generation == request.registry_generation
        )
    assert request.connection_id is not None
    assert request.observed_catalog_generation is not None
    return any(
        item.connection_id.root == request.connection_id
        and item.generation == request.observed_catalog_generation
        for item in report.catalog_generations.observations
    )


def _validated_request(value: object) -> SemanticChangeScanRequest:
    try:
        checked = SemanticChangeScanRequest.model_validate(
            value.model_dump(mode="python", warnings=False)  # type: ignore[attr-defined]
        )
    except (AttributeError, TypeError, ValueError):
        raise SemanticChangeReconcilerUseCaseError(
            SemanticChangeReconcilerUseCaseErrorCode.INVALID_CLAIM,
            "semantic scan store returned invalid state",
        ) from None
    if checked != value:
        raise SemanticChangeReconcilerUseCaseError(
            SemanticChangeReconcilerUseCaseErrorCode.INVALID_CLAIM,
            "semantic scan store returned non-canonical state",
        )
    return checked


def _same_scan_identity(
    left: SemanticChangeScanRequest,
    right: SemanticChangeScanRequest,
) -> bool:
    return (
        left.scan_id,
        left.workspace_id,
        left.source_kind,
        left.source_event_key,
        left.source_fingerprint,
        left.catalog_scope,
        left.registry_id,
        left.registry_generation,
        left.connection_id,
        left.base_catalog_generation,
        left.observed_catalog_generation,
        left.max_attempts,
        left.requested_at,
    ) == (
        right.scan_id,
        right.workspace_id,
        right.source_kind,
        right.source_event_key,
        right.source_fingerprint,
        right.catalog_scope,
        right.registry_id,
        right.registry_generation,
        right.connection_id,
        right.base_catalog_generation,
        right.observed_catalog_generation,
        right.max_attempts,
        right.requested_at,
    )


def _runner_failure_code(
    code: SemanticChangeScanRunnerErrorCode,
) -> SemanticChangeScanFailureCode:
    return {
        SemanticChangeScanRunnerErrorCode.REGISTRY_UNAVAILABLE: (
            SemanticChangeScanFailureCode.REGISTRY_UNAVAILABLE
        ),
        SemanticChangeScanRunnerErrorCode.EVIDENCE_UNAVAILABLE: (
            SemanticChangeScanFailureCode.EVIDENCE_UNAVAILABLE
        ),
        SemanticChangeScanRunnerErrorCode.DEPENDENCY_INDEX_INCOMPLETE: (
            SemanticChangeScanFailureCode.DEPENDENCY_INDEX_INCOMPLETE
        ),
        SemanticChangeScanRunnerErrorCode.SOURCE_TIMEOUT: (
            SemanticChangeScanFailureCode.SOURCE_TIMEOUT
        ),
        SemanticChangeScanRunnerErrorCode.STOP_REQUESTED: (
            SemanticChangeScanFailureCode.SHUTDOWN_REQUESTED
        ),
        SemanticChangeScanRunnerErrorCode.INVALID_INSPECTION: (
            SemanticChangeScanFailureCode.INSPECTION_INVALID
        ),
    }[code]


def _superseded_result(
    request: SemanticChangeScanRequest,
) -> SemanticChangeReconcilerIterationResult:
    return SemanticChangeReconcilerIterationResult(
        outcome=SemanticChangeReconcilerIterationOutcome.SUPERSEDED,
        scan_id=request.scan_id,
        status=request.status,
        attempts=request.attempts,
    )


def _store_failure(
    error: SemanticChangeScanStoreError,
) -> SemanticChangeReconcilerUseCaseError:
    code = (
        SemanticChangeReconcilerUseCaseErrorCode.LEASE_LOST
        if error.code
        in {
            SemanticChangeScanStoreErrorCode.STATE_CONFLICT,
            SemanticChangeScanStoreErrorCode.LEASE_LOST,
            SemanticChangeScanStoreErrorCode.NOT_FOUND,
        }
        else SemanticChangeReconcilerUseCaseErrorCode.STORE_UNAVAILABLE
    )
    return SemanticChangeReconcilerUseCaseError(
        code,
        "semantic scan store could not complete the requested operation",
    )


def _store_unavailable() -> SemanticChangeReconcilerUseCaseError:
    return SemanticChangeReconcilerUseCaseError(
        SemanticChangeReconcilerUseCaseErrorCode.STORE_UNAVAILABLE,
        "semantic scan store is unavailable",
    )


__all__ = [
    "RunOneSemanticChangeScan",
    "SemanticChangeReconcilerIterationOutcome",
    "SemanticChangeReconcilerIterationResult",
    "SemanticChangeReconcilerUseCaseError",
    "SemanticChangeReconcilerUseCaseErrorCode",
]
