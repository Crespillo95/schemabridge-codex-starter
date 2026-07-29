"""Fail-closed validation for the versioned alert, SLO, SIEM, and runbook bundle."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast

import yaml

from schemabridge.adapters.observability.metrics import OPERATIONAL_METRICS
from schemabridge.adapters.observability.structured_logging import (
    PUBLIC_LOG_FIELD_NAMES,
    TELEMETRY_SCHEMA_VERSION,
)

_METRIC_REFERENCE = re.compile(r"\bschemabridge_[a-z0-9_]+\b")
_ABSENT_METRIC_REFERENCE = re.compile(r"\babsent_over_time\s*\(\s*(schemabridge_[a-z0-9_]+)")
_SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_SAFE_DIAGNOSTICS = frozenset(
    {
        "control",
        "correlation_id",
        "error_code",
        "outcome",
        "queue",
        "resource",
        "service",
    }
)
_REQUIRED_RUNBOOK_HEADINGS = (
    "## Detection",
    "## Containment",
    "## Recovery",
    "## Evidence",
)
_REQUIRED_FORBIDDEN_SIEM_FIELDS = frozenset(
    {
        "claim",
        "credential",
        "dsn",
        "endpoint",
        "parameter",
        "password",
        "prompt",
        "raw_plan",
        "result_row",
        "secret_binding",
        "secret_path",
        "source_value",
        "sql",
        "stack_trace",
        "token",
        "username",
    }
)
_FAIL_CLOSED_ALERT_TOKENS = {
    "SchemaBridgeApiAvailability": frozenset(
        {
            'up{job="schemabridge-api"}==0',
            'absent_over_time(up{job="schemabridge-api"}[5m])',
        }
    ),
    "SchemaBridgeApiP95Latency": frozenset(
        {('rate(schemabridge_http_request_duration_seconds_bucket{service="api"}[10m])')}
    ),
    "SchemaBridgeQueueBacklog": frozenset(
        {
            'up{job="schemabridge-observer"}==0',
            'absent_over_time(up{job="schemabridge-observer"}[5m])',
            *(
                f'absent_over_time(schemabridge_queue_depth{{queue="{queue}"}}[5m])'
                for queue in ("execution", "catalog", "profile", "reconciliation")
            ),
            *(
                f'absent_over_time(schemabridge_queue_oldest_age_seconds{{queue="{queue}"}}[5m])'
                for queue in ("execution", "catalog", "profile", "reconciliation")
            ),
        }
    ),
    "SchemaBridgeUnsafeJobTransitions": frozenset(
        {
            (
                "increase("
                "schemabridge_job_transitions_total"
                '{transition=~"lease_reclaimed|dead_lettered|authorization_stale"}'
                "[15m])"
            )
        }
    ),
    "SchemaBridgeSourceFailureRate": frozenset(
        {('rate(schemabridge_source_operations_total{outcome=~"timeout|error"}[10m])')}
    ),
    "SchemaBridgeRuntimeSecurityControlFailure": frozenset(
        {"increase(schemabridge_runtime_control_failures_total[5m])"}
    ),
    "SchemaBridgeBackupStale": frozenset({"absent_over_time(schemabridge_backup_age_seconds[5m])"}),
    "SchemaBridgeRestoreDrillStale": frozenset(
        {"absent_over_time(schemabridge_restore_drill_age_seconds[5m])"}
    ),
    "SchemaBridgeIntegrityFailure": frozenset(
        {"increase(schemabridge_integrity_failures_total[5m])>0"}
    ),
    "SchemaBridgeReleasePolicyFailure": frozenset(
        {'schemabridge_release_policy_checks_total{outcome="error"}[15m]'}
    ),
    "SchemaBridgeCapacitySaturation": frozenset(
        {
            (
                "absent_over_time("
                "schemabridge_capacity_utilization_ratio"
                f'{{resource="{resource}"}}[5m])'
            )
            for resource in ("worker", "database_pool", "queue", "telemetry_buffer")
        }
    ),
    "SchemaBridgeProcessNotReady": frozenset(
        {
            *(
                f'up{{job="schemabridge-{service}"}}==0'
                for service in (
                    "api",
                    "worker",
                    "catalog",
                    "profile",
                    "reconciler",
                    "observer",
                )
            ),
            *(
                f'absent_over_time(up{{job="schemabridge-{service}"}}[5m])'
                for service in (
                    "api",
                    "worker",
                    "catalog",
                    "profile",
                    "reconciler",
                    "observer",
                )
            ),
            *(
                f'absent_over_time(schemabridge_process_ready{{service="{service}"}}[5m])'
                for service in (
                    "api",
                    "worker",
                    "catalog",
                    "profile",
                    "reconciler",
                    "observer",
                )
            ),
        }
    ),
    "SchemaBridgeProcessRestartLoop": frozenset(
        {"increase(schemabridge_process_restarts_total[15m])>3"}
    ),
    "SchemaBridgeTelemetryLoss": frozenset(
        {
            'up{job="schemabridge-observer"}==0',
            'absent_over_time(up{job="schemabridge-observer"}[5m])',
            "absent_over_time(schemabridge_telemetry_exports_total[5m])",
        }
    ),
    "SchemaBridgeReconciliationStale": frozenset(
        {"absent_over_time(schemabridge_reconciliation_age_seconds[5m])"}
    ),
}

_EXPECTED_ALERT_METRICS = {
    "SchemaBridgeApiAvailability": frozenset({"schemabridge_http_requests_total"}),
    "SchemaBridgeApiP95Latency": frozenset({"schemabridge_http_request_duration_seconds"}),
    "SchemaBridgeAuthorizationDenials": frozenset(
        {"schemabridge_http_authorization_denials_total"}
    ),
    "SchemaBridgeQueueBacklog": frozenset(
        {
            "schemabridge_queue_depth",
            "schemabridge_queue_oldest_age_seconds",
        }
    ),
    "SchemaBridgeUnsafeJobTransitions": frozenset({"schemabridge_job_transitions_total"}),
    "SchemaBridgeCancellationLatency": frozenset({"schemabridge_job_cancellation_latency_seconds"}),
    "SchemaBridgeSourceFailureRate": frozenset({"schemabridge_source_operations_total"}),
    "SchemaBridgeRuntimeSecurityControlFailure": frozenset(
        {"schemabridge_runtime_control_failures_total"}
    ),
    "SchemaBridgeBackupStale": frozenset({"schemabridge_backup_age_seconds"}),
    "SchemaBridgeRestoreDrillStale": frozenset({"schemabridge_restore_drill_age_seconds"}),
    "SchemaBridgeIntegrityFailure": frozenset({"schemabridge_integrity_failures_total"}),
    "SchemaBridgeReleasePolicyFailure": frozenset({"schemabridge_release_policy_checks_total"}),
    "SchemaBridgeCapacitySaturation": frozenset({"schemabridge_capacity_utilization_ratio"}),
    "SchemaBridgeProcessNotReady": frozenset({"schemabridge_process_ready"}),
    "SchemaBridgeProcessRestartLoop": frozenset({"schemabridge_process_restarts_total"}),
    "SchemaBridgeTelemetryLoss": frozenset({"schemabridge_telemetry_exports_total"}),
    "SchemaBridgeReconciliationStale": frozenset({"schemabridge_reconciliation_age_seconds"}),
}

# These gauges/exports are periodic contracts: missing samples are evidence of
# telemetry loss. Traffic and error-event counters are deliberately excluded,
# because a healthy idle service is expected not to emit those series.
_PERIODIC_ALERT_METRICS = {
    "SchemaBridgeQueueBacklog": frozenset(
        {
            "schemabridge_queue_depth",
            "schemabridge_queue_oldest_age_seconds",
        }
    ),
    "SchemaBridgeBackupStale": frozenset({"schemabridge_backup_age_seconds"}),
    "SchemaBridgeRestoreDrillStale": frozenset({"schemabridge_restore_drill_age_seconds"}),
    "SchemaBridgeCapacitySaturation": frozenset({"schemabridge_capacity_utilization_ratio"}),
    "SchemaBridgeProcessNotReady": frozenset({"schemabridge_process_ready"}),
    "SchemaBridgeTelemetryLoss": frozenset({"schemabridge_telemetry_exports_total"}),
    "SchemaBridgeReconciliationStale": frozenset({"schemabridge_reconciliation_age_seconds"}),
}


class ObservabilityContractError(ValueError):
    """The checked-in operational contract is absent, mutable, or inconsistent."""


@dataclass(frozen=True, slots=True)
class ObservabilityBundleReport:
    """Sanitized successful bundle summary."""

    schema_version: str
    page_alerts: int
    slos: int
    dashboard_panels: int
    runbooks: int
    siem_loss_metric: str


def _reject() -> NoReturn:
    raise ObservabilityContractError("observability bundle rejected")


def validate_observability_bundle(root: Path) -> ObservabilityBundleReport:
    """Validate one exact local bundle without following symlinks."""

    if not root.is_absolute():
        root = root.resolve()
    if not root.is_dir() or root.is_symlink():
        _reject()
    resolved_root = root.resolve()

    manifest = _load_mapping(_required_file(resolved_root, "bundle.yaml"))
    if manifest.get("schema_version") != "schemabridge.observability-bundle.v1":
        _reject()
    expected_manifest = {
        "schema_version",
        "alerts",
        "slos",
        "siem",
        "dashboard",
        "runbooks_directory",
    }
    if set(manifest) != expected_manifest:
        _reject()

    alerts_name = _safe_relative_filename(manifest.get("alerts"), ".yaml")
    slos_name = _safe_relative_filename(manifest.get("slos"), ".yaml")
    siem_name = _safe_relative_filename(manifest.get("siem"), ".yaml")
    dashboard_name = _safe_relative_filename(manifest.get("dashboard"), ".yaml")
    runbooks_name = _safe_relative_directory(manifest.get("runbooks_directory"))

    alerts_path = _required_file(resolved_root, alerts_name)
    slos_path = _required_file(resolved_root, slos_name)
    siem_path = _required_file(resolved_root, siem_name)
    runbooks_root = _required_directory(resolved_root, runbooks_name)

    known_metrics = {item.name for item in OPERATIONAL_METRICS}
    page_alerts, referenced_runbooks, alert_names = _validate_alerts(
        _load_mapping(alerts_path),
        known_metrics=known_metrics,
        bundle_root=resolved_root,
    )
    slo_count = _validate_slos(_load_mapping(slos_path), known_metrics=known_metrics)
    loss_metric = _validate_siem(_load_mapping(siem_path), known_metrics=known_metrics)
    dashboard_panels = _validate_dashboard(
        _load_mapping(_required_file(resolved_root, dashboard_name)),
        known_metrics=known_metrics,
        alert_names=alert_names,
    )
    runbook_count = _validate_runbooks(
        runbooks_root,
        bundle_root=resolved_root,
        referenced_runbooks=referenced_runbooks,
    )
    return ObservabilityBundleReport(
        schema_version="schemabridge.observability-bundle.v1",
        page_alerts=page_alerts,
        slos=slo_count,
        dashboard_panels=dashboard_panels,
        runbooks=runbook_count,
        siem_loss_metric=loss_metric,
    )


def _validate_alerts(
    payload: Mapping[str, object],
    *,
    known_metrics: set[str],
    bundle_root: Path,
) -> tuple[int, set[Path], set[str]]:
    if set(payload) != {"groups"}:
        _reject()
    groups = _as_sequence(payload.get("groups"))
    if len(groups) != 1:
        _reject()
    group = _as_mapping(groups[0])
    if set(group) != {"name", "interval", "rules"}:
        _reject()
    if group.get("name") != "schemabridge-m29-operations-v1" or group.get("interval") != "30s":
        _reject()

    rules = _as_sequence(group.get("rules"))
    if not rules:
        _reject()
    page_alerts = 0
    alert_names: set[str] = set()
    runbooks: set[Path] = set()
    for raw_rule in rules:
        rule = _as_mapping(raw_rule)
        if set(rule) != {"alert", "expr", "for", "labels", "annotations"}:
            _reject()
        alert_name = _as_nonempty_string(rule.get("alert"))
        if alert_name in alert_names:
            _reject()
        alert_names.add(alert_name)

        expression = _as_nonempty_string(rule.get("expr"))
        references = _metric_references(expression, known_metrics)
        expected_references = _EXPECTED_ALERT_METRICS.get(alert_name)
        if expected_references is None or references != expected_references:
            _reject()
        required_fail_closed_tokens = _FAIL_CLOSED_ALERT_TOKENS.get(alert_name)
        if required_fail_closed_tokens is not None:
            compact_expression = re.sub(r"\s+", "", expression)
            if not required_fail_closed_tokens <= {
                token for token in required_fail_closed_tokens if token in compact_expression
            }:
                _reject()
        _as_nonempty_string(rule.get("for"))

        labels = _as_mapping(rule.get("labels"))
        if set(labels) != {"severity", "owner", "deduplication_key"}:
            _reject()
        severity = _as_nonempty_string(labels.get("severity"))
        if severity not in {"page", "ticket"}:
            _reject()
        periodic_metrics = _PERIODIC_ALERT_METRICS.get(alert_name, frozenset())
        if not periodic_metrics <= _absence_guarded_metric_references(expression, known_metrics):
            _reject()
        owner = _as_nonempty_string(labels.get("owner"))
        deduplication_key = _as_nonempty_string(labels.get("deduplication_key"))
        if _SAFE_IDENTIFIER.fullmatch(owner) is None:
            _reject()
        if _SAFE_IDENTIFIER.fullmatch(deduplication_key) is None:
            _reject()

        annotations = _as_mapping(rule.get("annotations"))
        expected_annotations = {
            "summary",
            "runbook_ref",
            "safe_diagnostics",
            "resolution_condition",
        }
        if set(annotations) != expected_annotations:
            _reject()
        _as_nonempty_string(annotations.get("summary"))
        _as_nonempty_string(annotations.get("resolution_condition"))
        diagnostics = {
            item.strip()
            for item in _as_nonempty_string(annotations.get("safe_diagnostics")).split(",")
        }
        if not diagnostics or not diagnostics <= _SAFE_DIAGNOSTICS:
            _reject()

        runbook_ref = _as_nonempty_string(annotations.get("runbook_ref"))
        runbook_path = _safe_runbook_path(bundle_root, runbook_ref)
        runbooks.add(runbook_path)
        if severity == "page":
            page_alerts += 1
    if page_alerts < 8:
        _reject()
    if alert_names != _EXPECTED_ALERT_METRICS.keys():
        _reject()
    if not _FAIL_CLOSED_ALERT_TOKENS.keys() <= alert_names:
        _reject()
    return page_alerts, runbooks, alert_names


def _validate_dashboard(
    payload: Mapping[str, object],
    *,
    known_metrics: set[str],
    alert_names: set[str],
) -> int:
    if set(payload) != {
        "schema_version",
        "evidence_class",
        "title",
        "refresh_seconds",
        "panels",
    }:
        _reject()
    if (
        payload.get("schema_version") != "schemabridge.operations-dashboard.v1"
        or payload.get("evidence_class") != "synthetic_local_not_production"
        or payload.get("title") != "SchemaBridge operations"
        or payload.get("refresh_seconds") != 30
    ):
        _reject()
    panels = _as_sequence(payload.get("panels"))
    panel_ids: set[str] = set()
    panel_metrics: set[str] = set()
    referenced_alerts: set[str] = set()
    for raw_panel in panels:
        panel = _as_mapping(raw_panel)
        if set(panel) != {
            "id",
            "title",
            "metric",
            "visualization",
            "unit",
            "related_alerts",
        }:
            _reject()
        identifier = _as_nonempty_string(panel.get("id"))
        if _SAFE_IDENTIFIER.fullmatch(identifier) is None or identifier in panel_ids:
            _reject()
        panel_ids.add(identifier)
        _as_nonempty_string(panel.get("title"))
        metric = _as_nonempty_string(panel.get("metric"))
        if metric not in known_metrics or metric in panel_metrics:
            _reject()
        panel_metrics.add(metric)
        if panel.get("visualization") not in {"stat", "timeseries"}:
            _reject()
        if panel.get("unit") not in {
            "batches",
            "boolean",
            "checks",
            "failures",
            "items",
            "operations",
            "ratio",
            "requests",
            "restarts",
            "runs",
            "seconds",
            "transitions",
        }:
            _reject()
        related = {_as_nonempty_string(item) for item in _as_sequence(panel.get("related_alerts"))}
        if not related <= alert_names:
            _reject()
        referenced_alerts.update(related)
    if panel_metrics != known_metrics or referenced_alerts != alert_names:
        _reject()
    return len(panels)


def _metric_references(expression: str, known_metrics: set[str]) -> set[str]:
    references: set[str] = set()
    for candidate in _METRIC_REFERENCE.findall(expression):
        references.add(_normalize_metric_reference(candidate, known_metrics))
    return references


def _absence_guarded_metric_references(
    expression: str,
    known_metrics: set[str],
) -> set[str]:
    return {
        _normalize_metric_reference(candidate, known_metrics)
        for candidate in _ABSENT_METRIC_REFERENCE.findall(expression)
    }


def _normalize_metric_reference(candidate: str, known_metrics: set[str]) -> str:
    normalized = candidate
    for suffix in ("_bucket", "_sum", "_count"):
        possible = candidate.removesuffix(suffix)
        if possible in known_metrics:
            normalized = possible
            break
    if normalized not in known_metrics:
        _reject()
    return normalized


def _validate_slos(payload: Mapping[str, object], *, known_metrics: set[str]) -> int:
    if set(payload) != {"schema_version", "evidence_class", "window_days", "slos"}:
        _reject()
    if payload.get("schema_version") != "schemabridge.slo.v1":
        _reject()
    if payload.get("evidence_class") != "synthetic_local_not_production":
        _reject()
    if payload.get("window_days") != 30:
        _reject()

    slos = _as_sequence(payload.get("slos"))
    categories: set[str] = set()
    identifiers: set[str] = set()
    for raw_slo in slos:
        slo = _as_mapping(raw_slo)
        required = {
            "id",
            "category",
            "owner",
            "objective",
            "error_budget_fraction",
            "indicator",
        }
        if set(slo) != required:
            _reject()
        identifier = _as_nonempty_string(slo.get("id"))
        category = _as_nonempty_string(slo.get("category"))
        owner = _as_nonempty_string(slo.get("owner"))
        if identifier in identifiers or _SAFE_IDENTIFIER.fullmatch(owner) is None:
            _reject()
        identifiers.add(identifier)
        categories.add(category)

        objective = _as_fraction(slo.get("objective"))
        error_budget = _as_fraction(slo.get("error_budget_fraction"))
        if abs((1.0 - objective) - error_budget) > 1e-9:
            _reject()

        indicator = _as_mapping(slo.get("indicator"))
        _as_nonempty_string(indicator.get("type"))
        metrics = _as_sequence(indicator.get("metrics"))
        if not metrics:
            _reject()
        for metric in metrics:
            if _as_nonempty_string(metric) not in known_metrics:
                _reject()
    if categories != {"availability", "latency", "freshness", "correctness"}:
        _reject()
    return len(slos)


def _validate_siem(payload: Mapping[str, object], *, known_metrics: set[str]) -> str:
    if set(payload) != {
        "schema_version",
        "payload_schema",
        "transport",
        "buffer",
        "delivery",
    }:
        _reject()
    if payload.get("schema_version") != "schemabridge.siem-export.v1":
        _reject()
    if payload.get("payload_schema") != TELEMETRY_SCHEMA_VERSION:
        _reject()

    transport = _as_mapping(payload.get("transport"))
    expected_transport = {
        "scheme",
        "verify_tls",
        "require_mutual_tls",
        "authentication",
        "follow_redirects",
    }
    if set(transport) != expected_transport:
        _reject()
    if transport != {
        "scheme": "https",
        "verify_tls": True,
        "require_mutual_tls": True,
        "authentication": "workload_identity",
        "follow_redirects": False,
    }:
        _reject()

    buffer = _as_mapping(payload.get("buffer"))
    expected_buffer = {
        "max_events",
        "max_event_bytes",
        "max_batch_events",
        "max_retry_attempts",
        "overflow_policy",
    }
    if set(buffer) != expected_buffer:
        _reject()
    max_events = _as_positive_int(buffer.get("max_events"))
    max_event_bytes = _as_positive_int(buffer.get("max_event_bytes"))
    max_batch_events = _as_positive_int(buffer.get("max_batch_events"))
    max_retry_attempts = _as_positive_int(buffer.get("max_retry_attempts"))
    if not (
        max_events <= 10_000
        and max_event_bytes <= 4_096
        and max_batch_events <= 100
        and max_retry_attempts <= 5
    ):
        _reject()
    if buffer.get("overflow_policy") != "reject_and_signal_loss":
        _reject()

    delivery = _as_mapping(payload.get("delivery"))
    if set(delivery) != {"allowed_fields", "forbidden_fields", "loss_metric", "loss_label"}:
        _reject()
    allowed_fields = {
        _as_nonempty_string(item) for item in _as_sequence(delivery.get("allowed_fields"))
    }
    if allowed_fields != PUBLIC_LOG_FIELD_NAMES:
        _reject()
    forbidden_fields = {
        _as_nonempty_string(item) for item in _as_sequence(delivery.get("forbidden_fields"))
    }
    if not forbidden_fields.issuperset(_REQUIRED_FORBIDDEN_SIEM_FIELDS):
        _reject()
    if allowed_fields & forbidden_fields:
        _reject()
    loss_metric = _as_nonempty_string(delivery.get("loss_metric"))
    if loss_metric not in known_metrics:
        _reject()
    if _as_mapping(delivery.get("loss_label")) != {"outcome": "dropped"}:
        _reject()
    return loss_metric


def _validate_runbooks(
    runbooks_root: Path,
    *,
    bundle_root: Path,
    referenced_runbooks: set[Path],
) -> int:
    runbooks = set(runbooks_root.glob("*.md"))
    if not runbooks or any(path.is_symlink() for path in runbooks):
        _reject()
    if runbooks != referenced_runbooks:
        _reject()
    for path in runbooks:
        if not path.resolve().is_relative_to(bundle_root):
            _reject()
        content = path.read_text(encoding="utf-8")
        if not all(heading in content for heading in _REQUIRED_RUNBOOK_HEADINGS):
            _reject()
    return len(runbooks)


def _load_mapping(path: Path) -> Mapping[str, object]:
    try:
        payload: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        _reject()
    return _as_mapping(payload)


def _required_file(root: Path, relative: str) -> Path:
    path = root / relative
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
        _reject()
    return path


def _required_directory(root: Path, relative: str) -> Path:
    path = root / relative
    if path.is_symlink() or not path.is_dir() or not path.resolve().is_relative_to(root):
        _reject()
    return path


def _safe_relative_filename(value: object, suffix: str) -> str:
    relative = _as_nonempty_string(value)
    path = Path(relative)
    if path.is_absolute() or len(path.parts) != 1 or path.suffix != suffix:
        _reject()
    return relative


def _safe_relative_directory(value: object) -> str:
    relative = _as_nonempty_string(value)
    path = Path(relative)
    if path.is_absolute() or len(path.parts) != 1 or path.name != relative:
        _reject()
    return relative


def _safe_runbook_path(root: Path, reference: str) -> Path:
    relative = Path(reference)
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or relative.parts[0] != "runbooks"
        or relative.suffix != ".md"
    ):
        _reject()
    path = root / relative
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
        _reject()
    return path


def _as_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _reject()
    if any(not isinstance(key, str) for key in value):
        _reject()
    return cast(Mapping[str, object], value)


def _as_sequence(value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        _reject()
    return cast(Sequence[object], value)


def _as_nonempty_string(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        _reject()
    return value


def _as_fraction(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _reject()
    converted = float(value)
    if not 0 <= converted <= 1:
        _reject()
    return converted


def _as_positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _reject()
    return value
