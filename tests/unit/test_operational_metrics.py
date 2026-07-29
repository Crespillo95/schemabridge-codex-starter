from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from schemabridge.adapters.observability.contracts import (
    ObservabilityContractError,
    validate_observability_bundle,
)
from schemabridge.adapters.observability.metrics import (
    OPERATIONAL_METRICS,
    MetricContractError,
    MetricKind,
    OpenMetricsRegistry,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
OBSERVABILITY_ROOT = REPOSITORY_ROOT / "deploy" / "observability"


def test_closed_metric_catalog_covers_the_m29_operational_signals() -> None:
    definitions = {item.name: item for item in OPERATIONAL_METRICS}

    expected = {
        "schemabridge_http_authorization_denials_total",
        "schemabridge_http_requests_total",
        "schemabridge_http_request_duration_seconds",
        "schemabridge_queue_depth",
        "schemabridge_queue_oldest_age_seconds",
        "schemabridge_job_transitions_total",
        "schemabridge_job_cancellation_latency_seconds",
        "schemabridge_source_operations_total",
        "schemabridge_runtime_control_failures_total",
        "schemabridge_backup_age_seconds",
        "schemabridge_restore_drill_age_seconds",
        "schemabridge_integrity_failures_total",
        "schemabridge_capacity_utilization_ratio",
        "schemabridge_process_restarts_total",
        "schemabridge_process_ready",
        "schemabridge_telemetry_exports_total",
        "schemabridge_reconciliation_age_seconds",
        "schemabridge_backup_runs_total",
        "schemabridge_release_policy_checks_total",
    }
    assert expected <= definitions.keys()
    assert definitions["schemabridge_http_request_duration_seconds"].kind is MetricKind.HISTOGRAM
    assert definitions["schemabridge_http_authorization_denials_total"].kind is MetricKind.COUNTER
    assert definitions["schemabridge_queue_depth"].kind is MetricKind.GAUGE
    assert definitions["schemabridge_job_transitions_total"].kind is MetricKind.COUNTER

    forbidden_dimensions = {
        "tenant",
        "workspace",
        "actor",
        "table",
        "field",
        "sql",
        "prompt",
        "secret",
        "binding",
        "error",
    }
    assert not any(
        label.name in forbidden_dimensions
        for definition in definitions.values()
        for label in definition.labels
    )


def test_registry_renders_deterministic_openmetrics_counters_gauges_and_histograms() -> None:
    registry = OpenMetricsRegistry()
    registry.increment_counter(
        "schemabridge_http_requests_total",
        {"service": "api", "outcome": "success"},
        amount=2,
    )
    registry.increment_counter(
        "schemabridge_http_authorization_denials_total",
        {"service": "api"},
    )
    registry.set_gauge(
        "schemabridge_queue_depth",
        {"queue": "execution"},
        value=4,
    )
    registry.observe_histogram(
        "schemabridge_http_request_duration_seconds",
        {"service": "api", "outcome": "success"},
        value=0.15,
    )

    output = registry.render()

    assert output.endswith("# EOF\n")
    assert 'schemabridge_http_requests_total{outcome="success",service="api"} 2.0\n' in output
    assert 'schemabridge_http_authorization_denials_total{service="api"} 1.0\n' in output
    assert 'schemabridge_queue_depth{queue="execution"} 4.0\n' in output
    assert (
        'schemabridge_http_request_duration_seconds_bucket{le="0.25",'
        'outcome="success",service="api"} 1\n'
    ) in output
    assert (
        'schemabridge_http_request_duration_seconds_count{outcome="success",service="api"} 1\n'
    ) in output
    assert "# TYPE schemabridge_queue_depth gauge\n" in output


def test_atomic_gauge_batch_rejects_the_whole_update_without_partial_visibility() -> None:
    registry = OpenMetricsRegistry()
    registry.set_gauges_atomically(
        (
            (
                "schemabridge_queue_depth",
                {"queue": "execution"},
                4.0,
            ),
            (
                "schemabridge_queue_oldest_age_seconds",
                {"queue": "execution"},
                12.0,
            ),
        )
    )

    with pytest.raises(MetricContractError):
        registry.set_gauges_atomically(
            (
                (
                    "schemabridge_queue_depth",
                    {"queue": "execution"},
                    99.0,
                ),
                (
                    "schemabridge_queue_oldest_age_seconds",
                    {"queue": "unknown"},
                    99.0,
                ),
            )
        )

    output = registry.render()
    assert 'schemabridge_queue_depth{queue="execution"} 4.0\n' in output
    assert 'schemabridge_queue_oldest_age_seconds{queue="execution"} 12.0\n' in output
    assert "99.0" not in output


@pytest.mark.parametrize(
    ("operation", "name", "labels", "value"),
    [
        ("counter", "schemabridge_unknown_total", {}, 1),
        (
            "counter",
            "schemabridge_http_requests_total",
            {"service": "api", "outcome": "success", "tenant": "customer-a"},
            1,
        ),
        (
            "counter",
            "schemabridge_http_requests_total",
            {"service": "api", "outcome": "password=hunter2"},
            1,
        ),
        ("gauge", "schemabridge_queue_depth", {"queue": "execution"}, float("nan")),
        (
            "histogram",
            "schemabridge_http_request_duration_seconds",
            {"service": "api", "outcome": "success"},
            -0.1,
        ),
    ],
)
def test_registry_rejects_unknown_high_cardinality_or_non_finite_samples_without_echoing_them(
    operation: str,
    name: str,
    labels: dict[str, str],
    value: float,
) -> None:
    registry = OpenMetricsRegistry()

    with pytest.raises(MetricContractError) as raised:
        if operation == "counter":
            registry.increment_counter(name, labels, amount=value)
        elif operation == "gauge":
            registry.set_gauge(name, labels, value=value)
        else:
            registry.observe_histogram(name, labels, value=value)

    assert str(raised.value) == "operational metric sample rejected"
    assert "hunter2" not in str(raised.value)
    assert "customer-a" not in str(raised.value)


def test_versioned_alert_slo_siem_and_runbook_bundle_is_internally_consistent() -> None:
    bundle = validate_observability_bundle(OBSERVABILITY_ROOT)

    assert bundle.schema_version == "schemabridge.observability-bundle.v1"
    assert bundle.page_alerts >= 8
    assert bundle.slos == 4
    assert bundle.dashboard_panels == len(OPERATIONAL_METRICS)
    assert bundle.runbooks >= 6
    assert bundle.siem_loss_metric == "schemabridge_telemetry_exports_total"


def test_authorization_alert_and_siem_use_the_dedicated_safe_signal() -> None:
    alerts = yaml.safe_load((OBSERVABILITY_ROOT / "alert-rules.yaml").read_text())
    authorization = next(
        rule
        for rule in alerts["groups"][0]["rules"]
        if rule["alert"] == "SchemaBridgeAuthorizationDenials"
    )
    expression = authorization["expr"]
    assert "schemabridge_http_authorization_denials_total" in expression
    assert "schemabridge_http_requests_total" not in expression

    siem = yaml.safe_load((OBSERVABILITY_ROOT / "siem-export.yaml").read_text())
    assert "authorization_denials" in siem["delivery"]["allowed_fields"]
    assert {
        "tenant",
        "workspace",
        "actor",
        "resource",
    }.isdisjoint(siem["delivery"]["allowed_fields"])


def test_idle_and_zero_error_series_do_not_page_while_periodic_signals_fail_closed() -> None:
    alerts = yaml.safe_load((OBSERVABILITY_ROOT / "alert-rules.yaml").read_text())
    rules = {rule["alert"]: rule["expr"] for rule in alerts["groups"][0]["rules"]}

    event_metrics = {
        "SchemaBridgeApiAvailability": "schemabridge_http_requests_total",
        "SchemaBridgeApiP95Latency": "schemabridge_http_request_duration_seconds_bucket",
        "SchemaBridgeUnsafeJobTransitions": "schemabridge_job_transitions_total",
        "SchemaBridgeSourceFailureRate": "schemabridge_source_operations_total",
        "SchemaBridgeRuntimeSecurityControlFailure": (
            "schemabridge_runtime_control_failures_total"
        ),
        "SchemaBridgeIntegrityFailure": "schemabridge_integrity_failures_total",
        "SchemaBridgeReleasePolicyFailure": "schemabridge_release_policy_checks_total",
        "SchemaBridgeProcessRestartLoop": "schemabridge_process_restarts_total",
    }
    for alert_name, metric in event_metrics.items():
        compact = "".join(rules[alert_name].split())
        assert f"absent_over_time({metric}" not in compact

    periodic_metrics = {
        "SchemaBridgeQueueBacklog": (
            "schemabridge_queue_depth",
            "schemabridge_queue_oldest_age_seconds",
        ),
        "SchemaBridgeBackupStale": ("schemabridge_backup_age_seconds",),
        "SchemaBridgeRestoreDrillStale": ("schemabridge_restore_drill_age_seconds",),
        "SchemaBridgeCapacitySaturation": ("schemabridge_capacity_utilization_ratio",),
        "SchemaBridgeProcessNotReady": ("schemabridge_process_ready",),
        "SchemaBridgeTelemetryLoss": ("schemabridge_telemetry_exports_total",),
        "SchemaBridgeReconciliationStale": ("schemabridge_reconciliation_age_seconds",),
    }
    for alert_name, metrics in periodic_metrics.items():
        compact = "".join(rules[alert_name].split())
        for metric in metrics:
            assert f"absent_over_time({metric}" in compact


def test_every_page_alert_has_owner_dedup_resolution_safe_diagnostics_and_existing_runbook() -> (
    None
):
    payload = yaml.safe_load((OBSERVABILITY_ROOT / "alert-rules.yaml").read_text())
    rules = payload["groups"][0]["rules"]

    for rule in rules:
        if rule["labels"]["severity"] != "page":
            continue
        assert rule["labels"]["owner"]
        assert rule["labels"]["deduplication_key"]
        annotations = rule["annotations"]
        assert annotations["resolution_condition"]
        assert annotations["safe_diagnostics"]
        runbook = OBSERVABILITY_ROOT / annotations["runbook_ref"]
        assert runbook.is_file()
        assert runbook.resolve().is_relative_to(OBSERVABILITY_ROOT.resolve())


def test_bundle_validator_fails_closed_when_the_root_is_not_the_exact_bundle() -> None:
    with pytest.raises(ObservabilityContractError, match="observability bundle rejected"):
        validate_observability_bundle(OBSERVABILITY_ROOT / "runbooks")


@pytest.mark.parametrize(
    ("alert_name", "unsafe_expression"),
    [
        (
            "SchemaBridgeApiAvailability",
            'schemabridge_http_requests_total{service="api"} >= 0',
        ),
        (
            "SchemaBridgeApiP95Latency",
            "schemabridge_http_request_duration_seconds_bucket >= 0",
        ),
        (
            "SchemaBridgeQueueBacklog",
            "schemabridge_queue_depth >= 0",
        ),
        (
            "SchemaBridgeUnsafeJobTransitions",
            "schemabridge_job_transitions_total >= 0",
        ),
        (
            "SchemaBridgeSourceFailureRate",
            "schemabridge_source_operations_total >= 0",
        ),
        (
            "SchemaBridgeRuntimeSecurityControlFailure",
            "schemabridge_runtime_control_failures_total >= 0",
        ),
        (
            "SchemaBridgeBackupStale",
            "schemabridge_backup_age_seconds >= 0",
        ),
        (
            "SchemaBridgeRestoreDrillStale",
            "schemabridge_restore_drill_age_seconds >= 0",
        ),
        (
            "SchemaBridgeIntegrityFailure",
            "schemabridge_integrity_failures_total >= 0",
        ),
        (
            "SchemaBridgeReleasePolicyFailure",
            "schemabridge_release_policy_checks_total >= 0",
        ),
        (
            "SchemaBridgeCapacitySaturation",
            "schemabridge_capacity_utilization_ratio >= 0",
        ),
        (
            "SchemaBridgeProcessNotReady",
            "schemabridge_process_ready >= 0",
        ),
        (
            "SchemaBridgeProcessRestartLoop",
            "schemabridge_process_restarts_total >= 0",
        ),
        (
            "SchemaBridgeTelemetryLoss",
            "schemabridge_telemetry_exports_total >= 0",
        ),
        (
            "SchemaBridgeReconciliationStale",
            "schemabridge_reconciliation_age_seconds >= 0",
        ),
    ],
)
def test_bundle_rejects_critical_alerts_that_fail_open_when_series_disappear(
    tmp_path: Path,
    alert_name: str,
    unsafe_expression: str,
) -> None:
    bundle = tmp_path / "observability"
    shutil.copytree(OBSERVABILITY_ROOT, bundle)
    alerts_path = bundle / "alert-rules.yaml"
    payload = yaml.safe_load(alerts_path.read_text(encoding="utf-8"))
    rule = next(item for item in payload["groups"][0]["rules"] if item["alert"] == alert_name)
    rule["expr"] = unsafe_expression
    alerts_path.write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ObservabilityContractError, match="observability bundle rejected"):
        validate_observability_bundle(bundle)


def test_bundle_rejects_page_alert_with_an_unguarded_additional_metric_family(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "observability"
    shutil.copytree(OBSERVABILITY_ROOT, bundle)
    alerts_path = bundle / "alert-rules.yaml"
    payload = yaml.safe_load(alerts_path.read_text(encoding="utf-8"))
    rule = next(
        item
        for item in payload["groups"][0]["rules"]
        if item["alert"] == "SchemaBridgeApiP95Latency"
    )
    rule["expr"] += "\nor schemabridge_capacity_utilization_ratio > 2"
    alerts_path.write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ObservabilityContractError, match="observability bundle rejected"):
        validate_observability_bundle(bundle)
