"""Closed, synthetic, and secret-safe M29 operations view models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class M29OperationsState(StrEnum):
    """Deterministic states required by the M29 browser matrix."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    SECRET_OUTAGE = "secret_outage"
    QUEUE_BACKLOG = "queue_backlog"
    STALE_BACKUP = "stale_backup"
    FAILED_RELEASE = "failed_release"


class M29OperationsSeverity(StrEnum):
    """Closed presentation severity; it never carries provider error text."""

    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"


class M29SecretResolutionStatus(StrEnum):
    """Public secret-resolution outcome without a binding or provider surface."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class M29ReleaseGateStatus(StrEnum):
    """Public release-policy outcome."""

    PASSED = "passed"
    BLOCKED = "blocked"


class M29TelemetryStatus(StrEnum):
    """Public telemetry-delivery outcome."""

    CURRENT = "current"
    DELAYED = "delayed"


@dataclass(frozen=True, slots=True)
class M29OperationsDiagnostics:
    """Exact public diagnostic allowlist rendered by the operations view."""

    ready_workloads: int
    expected_workloads: int
    queue_depth: int
    oldest_queue_age_seconds: int
    secret_resolution: M29SecretResolutionStatus
    backup_age_minutes: int
    release_gate: M29ReleaseGateStatus
    telemetry: M29TelemetryStatus


@dataclass(frozen=True, slots=True)
class _M29ScenarioDefinition:
    severity: M29OperationsSeverity
    label: str
    summary: str
    recommended_action: str
    diagnostics: M29OperationsDiagnostics


@dataclass(frozen=True, slots=True)
class M29OperationsView:
    """One safe view selected only through the closed scenario vocabulary.

    The model stores no arbitrary text or mapping. All reader-facing copy and
    diagnostics are derived from the selected enum through deterministic code.
    """

    state: M29OperationsState

    def __post_init__(self) -> None:
        if not isinstance(self.state, M29OperationsState):
            raise TypeError("M29 operations state must use the closed enum")

    @property
    def severity(self) -> M29OperationsSeverity:
        return _definition_for(self.state).severity

    @property
    def label(self) -> str:
        return _definition_for(self.state).label

    @property
    def summary(self) -> str:
        return _definition_for(self.state).summary

    @property
    def recommended_action(self) -> str:
        return _definition_for(self.state).recommended_action

    @property
    def diagnostics(self) -> M29OperationsDiagnostics:
        return _definition_for(self.state).diagnostics

    @property
    def hostile_probe(self) -> str:
        """Return one fixed inert string used to prove browser escaping."""

        return 'Synthetic label <script data-m29-hostile>window.__m29_xss = "executed"</script>'


def build_m29_operations_view(state: M29OperationsState) -> M29OperationsView:
    """Build one deterministic operations view without I/O or ambient state."""

    if not isinstance(state, M29OperationsState):
        raise TypeError("M29 operations state must use the closed enum")
    return M29OperationsView(state=state)


def _definition_for(state: M29OperationsState) -> _M29ScenarioDefinition:
    if state is M29OperationsState.HEALTHY:
        return _M29ScenarioDefinition(
            severity=M29OperationsSeverity.HEALTHY,
            label="Healthy",
            summary="All observed operational checks are within the synthetic local policy.",
            recommended_action="No page. Continue routine monitoring.",
            diagnostics=M29OperationsDiagnostics(
                ready_workloads=7,
                expected_workloads=7,
                queue_depth=3,
                oldest_queue_age_seconds=12,
                secret_resolution=M29SecretResolutionStatus.AVAILABLE,
                backup_age_minutes=18,
                release_gate=M29ReleaseGateStatus.PASSED,
                telemetry=M29TelemetryStatus.CURRENT,
            ),
        )
    if state is M29OperationsState.DEGRADED:
        return _M29ScenarioDefinition(
            severity=M29OperationsSeverity.WARNING,
            label="Degraded",
            summary="One workload is not ready and operational telemetry is delayed.",
            recommended_action="Inspect readiness and restore current telemetry delivery.",
            diagnostics=M29OperationsDiagnostics(
                ready_workloads=6,
                expected_workloads=7,
                queue_depth=42,
                oldest_queue_age_seconds=180,
                secret_resolution=M29SecretResolutionStatus.AVAILABLE,
                backup_age_minutes=47,
                release_gate=M29ReleaseGateStatus.PASSED,
                telemetry=M29TelemetryStatus.DELAYED,
            ),
        )
    if state is M29OperationsState.SECRET_OUTAGE:
        return _M29ScenarioDefinition(
            severity=M29OperationsSeverity.CRITICAL,
            label="Secret outage",
            summary="Secret resolution is unavailable and dependent source work is blocked.",
            recommended_action="Keep source work blocked and restore the managed resolution service.",
            diagnostics=M29OperationsDiagnostics(
                ready_workloads=5,
                expected_workloads=7,
                queue_depth=17,
                oldest_queue_age_seconds=94,
                secret_resolution=M29SecretResolutionStatus.UNAVAILABLE,
                backup_age_minutes=22,
                release_gate=M29ReleaseGateStatus.PASSED,
                telemetry=M29TelemetryStatus.CURRENT,
            ),
        )
    if state is M29OperationsState.QUEUE_BACKLOG:
        return _M29ScenarioDefinition(
            severity=M29OperationsSeverity.CRITICAL,
            label="Queue backlog",
            summary="Queue depth and oldest queued work exceed the synthetic local policy.",
            recommended_action="Pause nonessential submissions and restore bounded worker capacity.",
            diagnostics=M29OperationsDiagnostics(
                ready_workloads=7,
                expected_workloads=7,
                queue_depth=840,
                oldest_queue_age_seconds=1_260,
                secret_resolution=M29SecretResolutionStatus.AVAILABLE,
                backup_age_minutes=36,
                release_gate=M29ReleaseGateStatus.PASSED,
                telemetry=M29TelemetryStatus.CURRENT,
            ),
        )
    if state is M29OperationsState.STALE_BACKUP:
        return _M29ScenarioDefinition(
            severity=M29OperationsSeverity.CRITICAL,
            label="Stale backup",
            summary="Backup freshness exceeds the synthetic sixty-minute recovery policy.",
            recommended_action="Keep cutover blocked and verify a fresh recovery point.",
            diagnostics=M29OperationsDiagnostics(
                ready_workloads=7,
                expected_workloads=7,
                queue_depth=6,
                oldest_queue_age_seconds=31,
                secret_resolution=M29SecretResolutionStatus.AVAILABLE,
                backup_age_minutes=94,
                release_gate=M29ReleaseGateStatus.PASSED,
                telemetry=M29TelemetryStatus.CURRENT,
            ),
        )
    if state is M29OperationsState.FAILED_RELEASE:
        return _M29ScenarioDefinition(
            severity=M29OperationsSeverity.CRITICAL,
            label="Failed release",
            summary="The immutable release policy rejected the candidate.",
            recommended_action="Keep promotion blocked and inspect the signed policy evidence.",
            diagnostics=M29OperationsDiagnostics(
                ready_workloads=7,
                expected_workloads=7,
                queue_depth=4,
                oldest_queue_age_seconds=15,
                secret_resolution=M29SecretResolutionStatus.AVAILABLE,
                backup_age_minutes=25,
                release_gate=M29ReleaseGateStatus.BLOCKED,
                telemetry=M29TelemetryStatus.CURRENT,
            ),
        )
    raise AssertionError("closed M29 operations state was not handled")
