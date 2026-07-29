from __future__ import annotations

from dataclasses import fields

import pytest

from schemabridge.application.m29_operations import (
    M29OperationsDiagnostics,
    M29OperationsSeverity,
    M29OperationsState,
    M29OperationsView,
    M29ReleaseGateStatus,
    M29SecretResolutionStatus,
    M29TelemetryStatus,
    build_m29_operations_view,
)


def test_m29_operations_matrix_is_closed_and_deterministic() -> None:
    assert tuple(M29OperationsState) == (
        M29OperationsState.HEALTHY,
        M29OperationsState.DEGRADED,
        M29OperationsState.SECRET_OUTAGE,
        M29OperationsState.QUEUE_BACKLOG,
        M29OperationsState.STALE_BACKUP,
        M29OperationsState.FAILED_RELEASE,
    )

    for state in M29OperationsState:
        assert build_m29_operations_view(state) == build_m29_operations_view(state)


def test_each_m29_state_exposes_coherent_public_diagnostics() -> None:
    expected = {
        M29OperationsState.HEALTHY: (
            M29OperationsSeverity.HEALTHY,
            7,
            3,
            12,
            M29SecretResolutionStatus.AVAILABLE,
            18,
            M29ReleaseGateStatus.PASSED,
            M29TelemetryStatus.CURRENT,
        ),
        M29OperationsState.DEGRADED: (
            M29OperationsSeverity.WARNING,
            6,
            42,
            180,
            M29SecretResolutionStatus.AVAILABLE,
            47,
            M29ReleaseGateStatus.PASSED,
            M29TelemetryStatus.DELAYED,
        ),
        M29OperationsState.SECRET_OUTAGE: (
            M29OperationsSeverity.CRITICAL,
            5,
            17,
            94,
            M29SecretResolutionStatus.UNAVAILABLE,
            22,
            M29ReleaseGateStatus.PASSED,
            M29TelemetryStatus.CURRENT,
        ),
        M29OperationsState.QUEUE_BACKLOG: (
            M29OperationsSeverity.CRITICAL,
            7,
            840,
            1_260,
            M29SecretResolutionStatus.AVAILABLE,
            36,
            M29ReleaseGateStatus.PASSED,
            M29TelemetryStatus.CURRENT,
        ),
        M29OperationsState.STALE_BACKUP: (
            M29OperationsSeverity.CRITICAL,
            7,
            6,
            31,
            M29SecretResolutionStatus.AVAILABLE,
            94,
            M29ReleaseGateStatus.PASSED,
            M29TelemetryStatus.CURRENT,
        ),
        M29OperationsState.FAILED_RELEASE: (
            M29OperationsSeverity.CRITICAL,
            7,
            4,
            15,
            M29SecretResolutionStatus.AVAILABLE,
            25,
            M29ReleaseGateStatus.BLOCKED,
            M29TelemetryStatus.CURRENT,
        ),
    }

    for state, values in expected.items():
        view = build_m29_operations_view(state)
        diagnostics = view.diagnostics
        assert (
            view.severity,
            diagnostics.ready_workloads,
            diagnostics.queue_depth,
            diagnostics.oldest_queue_age_seconds,
            diagnostics.secret_resolution,
            diagnostics.backup_age_minutes,
            diagnostics.release_gate,
            diagnostics.telemetry,
        ) == values
        assert diagnostics.expected_workloads == 7


def test_m29_view_has_no_arbitrary_diagnostic_or_message_surface() -> None:
    assert {field.name for field in fields(M29OperationsView)} == {"state"}
    assert {field.name for field in fields(M29OperationsDiagnostics)} == {
        "ready_workloads",
        "expected_workloads",
        "queue_depth",
        "oldest_queue_age_seconds",
        "secret_resolution",
        "backup_age_minutes",
        "release_gate",
        "telemetry",
    }

    visible = "\n".join(
        text
        for state in M29OperationsState
        for text in (
            build_m29_operations_view(state).label,
            build_m29_operations_view(state).summary,
            build_m29_operations_view(state).recommended_action,
        )
    ).casefold()
    for forbidden in (
        "postgresql://",
        "select ",
        "insert ",
        "update ",
        "delete ",
        "tenant",
        "workspace",
        "credential",
        "password",
        "api_key",
        "dsn",
        "binding",
        "token",
        "urn:",
        "/",
    ):
        assert forbidden not in visible


def test_m29_view_rejects_open_string_states() -> None:
    with pytest.raises(TypeError, match="closed enum"):
        build_m29_operations_view("healthy")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="closed enum"):
        M29OperationsView(state="degraded")  # type: ignore[arg-type]


def test_hostile_probe_is_fixed_and_inert_at_the_application_boundary() -> None:
    probes = {build_m29_operations_view(state).hostile_probe for state in M29OperationsState}

    assert probes == {
        'Synthetic label <script data-m29-hostile>window.__m29_xss = "executed"</script>'
    }
