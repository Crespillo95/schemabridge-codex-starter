from __future__ import annotations

import io
import re
import shutil
from pathlib import Path

import pytest
import yaml

from schemabridge.adapters.observability.contracts import (
    ObservabilityContractError,
    validate_observability_bundle,
)
from schemabridge.adapters.observability.metrics import (
    COMPOSED_OPERATIONAL_METRIC_NAMES,
    COMPOSED_OPERATIONAL_METRIC_PRODUCERS,
    OPERATIONAL_METRICS,
    UNCOMPOSED_OPERATIONAL_METRIC_NAMES,
    MetricContractError,
    MetricKind,
    OpenMetricsRegistry,
)
from schemabridge.adapters.observability.runtime import RuntimeOperationalTelemetry
from schemabridge.application.operational_snapshot import RefreshOperationalSnapshot
from schemabridge.application.ports.operational_snapshot import (
    OperationalQueue,
    OperationalSnapshot,
    QueueOperationalSnapshot,
)
from schemabridge.application.ports.operational_telemetry import (
    OperationalErrorCode,
    OperationalEvent,
    OperationalOutcome,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
OBSERVABILITY_ROOT = REPOSITORY_ROOT / "deploy" / "observability"


def _sample_metric_names(document: str) -> set[str]:
    registered = {item.name for item in OPERATIONAL_METRICS}
    names: set[str] = set()
    for line in document.splitlines():
        if not line or line.startswith("#"):
            continue
        candidate = re.split(r"[{ ]", line, maxsplit=1)[0]
        for suffix in ("_bucket", "_sum", "_count"):
            base = candidate.removesuffix(suffix)
            if base in registered:
                candidate = base
                break
        assert candidate in registered
        names.add(candidate)
    return names


class _SnapshotReader:
    def read(self) -> OperationalSnapshot:
        return OperationalSnapshot(
            queues=(
                QueueOperationalSnapshot(OperationalQueue.EXECUTION, 0, 0.0),
                QueueOperationalSnapshot(OperationalQueue.CATALOG, 0, 0.0),
                QueueOperationalSnapshot(OperationalQueue.PROFILE, 0, 0.0),
                QueueOperationalSnapshot(OperationalQueue.RECONCILIATION, 0, 0.0),
            )
        )


def test_metric_catalog_separates_composed_from_uncomposed_families() -> None:
    definitions = {item.name: item for item in OPERATIONAL_METRICS}

    expected_composed = {
        "schemabridge_http_authorization_denials_total",
        "schemabridge_http_requests_total",
        "schemabridge_http_request_duration_seconds",
        "schemabridge_queue_depth",
        "schemabridge_queue_oldest_age_seconds",
        "schemabridge_job_transitions_total",
        "schemabridge_source_operations_total",
        "schemabridge_process_ready",
    }
    expected_uncomposed = {
        "schemabridge_job_cancellation_latency_seconds",
        "schemabridge_runtime_control_failures_total",
        "schemabridge_backup_age_seconds",
        "schemabridge_restore_drill_age_seconds",
        "schemabridge_integrity_failures_total",
        "schemabridge_capacity_utilization_ratio",
        "schemabridge_process_restarts_total",
        "schemabridge_telemetry_exports_total",
        "schemabridge_reconciliation_age_seconds",
        "schemabridge_backup_runs_total",
        "schemabridge_release_policy_checks_total",
    }
    assert expected_composed == COMPOSED_OPERATIONAL_METRIC_NAMES
    assert expected_uncomposed == UNCOMPOSED_OPERATIONAL_METRIC_NAMES
    assert expected_composed | expected_uncomposed == definitions.keys()
    assert {item.metric for item in COMPOSED_OPERATIONAL_METRIC_PRODUCERS} == expected_composed
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
    http_services = definitions["schemabridge_http_requests_total"].labels[0].values
    source_capabilities = definitions["schemabridge_source_operations_total"].labels[0].values
    readiness_services = definitions["schemabridge_process_ready"].labels[0].values
    assert http_services == ("api",)
    assert source_capabilities == ("execution", "catalog", "profile")
    assert set(readiness_services) == {
        "api",
        "catalog",
        "observer",
        "profile",
        "reconciler",
        "worker",
    }
    assert {"web", "preflight", "backup", "migrator"}.isdisjoint(
        {*http_services, *source_capabilities, *readiness_services}
    )


def test_composed_producer_scenarios_emit_exactly_the_active_metric_contract() -> None:
    observed: set[str] = set()
    api = RuntimeOperationalTelemetry(
        service="api",
        environment="production",
        stream=io.StringIO(),
    )
    api.emit(event="service.health", outcome="succeeded", duration_ms=1)
    api.emit(
        event="http.request",
        outcome="succeeded",
        duration_ms=2,
        counts={"requests_completed": 1},
    )
    api.emit(
        event="http.request",
        outcome="denied",
        duration_ms=3,
        error_code="unauthorized",
        counts={"authorization_denials": 1, "requests_completed": 1},
    )
    observed.update(_sample_metric_names(api.render_openmetrics()))

    worker = RuntimeOperationalTelemetry(
        service="worker",
        environment="production",
        stream=io.StringIO(),
    )
    worker.emit(
        event="job.execution",
        outcome="degraded",
        duration_ms=4,
        error_code="source_timeout",
        counts={"retries_scheduled": 1},
    )
    worker.emit(event="source.operation", outcome="succeeded", duration_ms=5)
    observed.update(_sample_metric_names(worker.render_openmetrics()))

    registry = OpenMetricsRegistry()
    RefreshOperationalSnapshot(reader=_SnapshotReader(), metrics=registry).execute()
    observed.update(_sample_metric_names(registry.render()))

    assert observed == COMPOSED_OPERATIONAL_METRIC_NAMES


@pytest.mark.parametrize(
    (
        "service",
        "event",
        "queue",
        "outcome",
        "error_code",
        "counts",
        "transition",
        "amount",
    ),
    [
        (
            "worker",
            "job.execution",
            "execution",
            "degraded",
            "source_timeout",
            {"retries_scheduled": 2},
            "retry_scheduled",
            2,
        ),
        (
            "worker",
            "job.execution",
            "execution",
            "failed",
            "internal_failure",
            {"dead_letters": 1},
            "dead_lettered",
            1,
        ),
        (
            "worker",
            "job.execution",
            "execution",
            "failed",
            "stale_authorization",
            {"jobs_failed": 1},
            "authorization_stale",
            1,
        ),
        (
            "worker",
            "job.execution",
            "execution",
            "cancelled",
            "operation_cancelled",
            {},
            "cancelled",
            1,
        ),
        (
            "catalog",
            "catalog.refresh",
            "catalog",
            "cancelled",
            "operation_cancelled",
            {},
            "cancelled",
            1,
        ),
        (
            "profile",
            "profile.run",
            "profile",
            "degraded",
            "source_unavailable",
            {"retries_scheduled": 1},
            "retry_scheduled",
            1,
        ),
        (
            "reconciler",
            "reconciliation.run",
            "reconciliation",
            "degraded",
            "queue_unavailable",
            {"retries_scheduled": 1},
            "retry_scheduled",
            1,
        ),
    ],
)
def test_queue_transition_metrics_come_only_from_real_closed_results(
    service: str,
    event: OperationalEvent,
    queue: str,
    outcome: OperationalOutcome,
    error_code: OperationalErrorCode,
    counts: dict[str, int],
    transition: str,
    amount: int,
) -> None:
    telemetry = RuntimeOperationalTelemetry(
        service=service,
        environment="production",
        stream=io.StringIO(),
    )

    telemetry.emit(
        event=event,
        outcome=outcome,
        duration_ms=1,
        error_code=error_code,
        counts=counts,
    )

    assert (
        "schemabridge_job_transitions_total"
        f'{{queue="{queue}",transition="{transition}"}} {float(amount)}\n'
        in telemetry.render_openmetrics()
    )


def test_log_only_services_and_unrelated_counts_do_not_create_metric_claims() -> None:
    web_stream = io.StringIO()
    web = RuntimeOperationalTelemetry(
        service="web",
        environment="production",
        stream=web_stream,
    )
    web.emit(
        event="http.request",
        outcome="succeeded",
        duration_ms=1,
        counts={"requests_completed": 1},
    )
    assert web.render_openmetrics() == "# EOF\n"
    assert '"event":"http.request"' in web_stream.getvalue()

    backup_stream = io.StringIO()
    backup = RuntimeOperationalTelemetry(
        service="backup",
        environment="production",
        stream=backup_stream,
    )
    backup.emit(
        event="service.health",
        outcome="succeeded",
        duration_ms=1,
        counts={"retries_scheduled": 1},
    )
    assert backup.render_openmetrics() == "# EOF\n"
    assert '"event":"service.health"' in backup_stream.getvalue()


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
    for dormant in UNCOMPOSED_OPERATIONAL_METRIC_NAMES:
        assert f"# HELP {dormant} " not in output
        assert f"# TYPE {dormant} " not in output


def test_registry_does_not_advertise_unsampled_families() -> None:
    registry = OpenMetricsRegistry()

    assert registry.render() == "# EOF\n"

    registry.set_gauge(
        "schemabridge_queue_depth",
        {"queue": "execution"},
        value=0,
    )
    rendered = registry.render()
    assert "# HELP schemabridge_queue_depth " in rendered
    assert "# TYPE schemabridge_queue_depth gauge" in rendered
    assert "# HELP schemabridge_queue_oldest_age_seconds " not in rendered


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


def test_active_bundle_uses_only_composed_metrics_and_marks_siem_inactive() -> None:
    bundle = validate_observability_bundle(OBSERVABILITY_ROOT)

    assert bundle.schema_version == "schemabridge.observability-bundle.v1"
    assert bundle.page_alerts == 5
    assert bundle.slos == 3
    assert bundle.dashboard_panels == len(COMPOSED_OPERATIONAL_METRIC_NAMES)
    assert bundle.runbooks == 4
    assert bundle.composed_metrics == len(COMPOSED_OPERATIONAL_METRIC_NAMES)
    assert bundle.uncomposed_metrics == len(UNCOMPOSED_OPERATIONAL_METRIC_NAMES)
    assert bundle.siem_status == "validated_not_composed"


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

    siem = yaml.safe_load((OBSERVABILITY_ROOT / "inactive" / "siem-export.yaml").read_text())
    assert "authorization_denials" in siem["delivery"]["allowed_fields"]
    assert {
        "tenant",
        "workspace",
        "actor",
        "resource",
    }.isdisjoint(siem["delivery"]["allowed_fields"])


def test_idle_and_zero_error_series_do_not_page_while_queue_signals_fail_closed() -> None:
    alerts = yaml.safe_load((OBSERVABILITY_ROOT / "alert-rules.yaml").read_text())
    rules = {rule["alert"]: rule["expr"] for rule in alerts["groups"][0]["rules"]}

    event_metrics = {
        "SchemaBridgeApiAvailability": "schemabridge_http_requests_total",
        "SchemaBridgeApiP95Latency": "schemabridge_http_request_duration_seconds_bucket",
        "SchemaBridgeUnsafeJobTransitions": "schemabridge_job_transitions_total",
        "SchemaBridgeSourceFailureRate": "schemabridge_source_operations_total",
    }
    for alert_name, metric in event_metrics.items():
        compact = "".join(rules[alert_name].split())
        assert f"absent_over_time({metric}" not in compact

    periodic_metrics = {
        "SchemaBridgeQueueBacklog": (
            "schemabridge_queue_depth",
            "schemabridge_queue_oldest_age_seconds",
        ),
    }
    for alert_name, metrics in periodic_metrics.items():
        compact = "".join(rules[alert_name].split())
        for metric in metrics:
            assert f"absent_over_time({metric}" in compact

    availability = "".join(rules["SchemaBridgeApiAvailability"].split())
    request_rate = 'sum(rate(schemabridge_http_requests_total{service="api"}[10m]))'
    assert f"{request_rate}>0and(" in availability

    def availability_pages(*, total_rate: float, success_rate: float, target_up: bool) -> bool:
        return (not target_up) or (total_rate > 0 and success_rate / max(total_rate, 0.001) < 0.995)

    cold_idle_pages = availability_pages(total_rate=0, success_rate=0, target_up=True)
    post_traffic_idle_pages = availability_pages(
        total_rate=0,
        success_rate=0,
        target_up=True,
    )
    assert not cold_idle_pages
    assert not post_traffic_idle_pages
    assert not availability_pages(total_rate=10, success_rate=10, target_up=True)
    assert availability_pages(total_rate=10, success_rate=9, target_up=True)
    assert availability_pages(total_rate=0, success_rate=0, target_up=False)

    def queue_pages(
        *,
        depth: int,
        oldest_age_seconds: float,
        complete_snapshot: bool,
        observer_up: bool,
    ) -> bool:
        return depth > 1_000 or oldest_age_seconds > 300 or not complete_snapshot or not observer_up

    assert not queue_pages(
        depth=0,
        oldest_age_seconds=0,
        complete_snapshot=True,
        observer_up=True,
    )
    assert queue_pages(
        depth=0,
        oldest_age_seconds=0,
        complete_snapshot=False,
        observer_up=True,
    )
    assert queue_pages(
        depth=1_001,
        oldest_age_seconds=0,
        complete_snapshot=True,
        observer_up=True,
    )

    serialized_active_bundle = "\n".join(
        (OBSERVABILITY_ROOT / name).read_text(encoding="utf-8")
        for name in ("alert-rules.yaml", "slos.yaml", "dashboard.yaml")
    )
    assert not any(
        metric in serialized_active_bundle for metric in UNCOMPOSED_OPERATIONAL_METRIC_NAMES
    )


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
    rule["expr"] += "\nor schemabridge_source_operations_total > 2"
    alerts_path.write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ObservabilityContractError, match="observability bundle rejected"):
        validate_observability_bundle(bundle)


@pytest.mark.parametrize(
    ("alert_name", "original", "mutation"),
    [
        ("SchemaBridgeApiAvailability", "< 0.995", "> 0.995"),
        ("SchemaBridgeApiAvailability", "clamp_min(", "clamp_max("),
        ("SchemaBridgeApiP95Latency", "> 0.75", "< 0.75"),
        ("SchemaBridgeAuthorizationDenials", "> 25", ">= 25"),
        ("SchemaBridgeQueueBacklog", "or on(queue)", "and on(queue)"),
        ("SchemaBridgeUnsafeJobTransitions", "> 0", ">= 0"),
        ("SchemaBridgeSourceFailureRate", "> 0.05", "< 0.05"),
    ],
)
def test_bundle_rejects_noncanonical_promql_with_the_same_metric_families(
    tmp_path: Path,
    alert_name: str,
    original: str,
    mutation: str,
) -> None:
    bundle = tmp_path / "observability"
    shutil.copytree(OBSERVABILITY_ROOT, bundle)
    alerts_path = bundle / "alert-rules.yaml"
    payload = yaml.safe_load(alerts_path.read_text(encoding="utf-8"))
    rule = next(item for item in payload["groups"][0]["rules"] if item["alert"] == alert_name)
    assert original in rule["expr"]
    rule["expr"] = rule["expr"].replace(original, mutation, 1)
    alerts_path.write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ObservabilityContractError, match="observability bundle rejected"):
        validate_observability_bundle(bundle)


def test_bundle_rejects_promql_with_trailing_invalid_syntax_and_canonical_tokens(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "observability"
    shutil.copytree(OBSERVABILITY_ROOT, bundle)
    alerts_path = bundle / "alert-rules.yaml"
    payload = yaml.safe_load(alerts_path.read_text(encoding="utf-8"))
    payload["groups"][0]["rules"][0]["expr"] += "\nand ("
    alerts_path.write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ObservabilityContractError, match="observability bundle rejected"):
        validate_observability_bundle(bundle)


@pytest.mark.parametrize(
    ("slo_id", "mutation"),
    [
        ("api_availability", "unknown_type"),
        ("api_latency", "extra_field"),
        ("queue_freshness", "missing_field"),
        ("api_availability", "changed_outcomes"),
    ],
)
def test_bundle_rejects_noncanonical_slo_indicator_contracts(
    tmp_path: Path,
    slo_id: str,
    mutation: str,
) -> None:
    bundle = tmp_path / "observability"
    shutil.copytree(OBSERVABILITY_ROOT, bundle)
    slos_path = bundle / "slos.yaml"
    payload = yaml.safe_load(slos_path.read_text(encoding="utf-8"))
    slo = next(item for item in payload["slos"] if item["id"] == slo_id)
    indicator = slo["indicator"]
    if mutation == "unknown_type":
        indicator["type"] = "failure_ratio"
    elif mutation == "extra_field":
        indicator["window"] = "10m"
    elif mutation == "missing_field":
        indicator.pop("queue_threshold_seconds")
    else:
        indicator["total_outcomes"] = ["success", "error"]
    slos_path.write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ObservabilityContractError, match="observability bundle rejected"):
        validate_observability_bundle(bundle)


def test_bundle_rejects_uncomposed_metric_in_an_active_slo(tmp_path: Path) -> None:
    bundle = tmp_path / "observability"
    shutil.copytree(OBSERVABILITY_ROOT, bundle)
    slos_path = bundle / "slos.yaml"
    payload = yaml.safe_load(slos_path.read_text(encoding="utf-8"))
    payload["slos"][0]["indicator"]["metrics"].append("schemabridge_backup_age_seconds")
    slos_path.write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ObservabilityContractError, match="observability bundle rejected"):
        validate_observability_bundle(bundle)


def test_bundle_validates_but_cannot_activate_the_uncomposed_siem_signal(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "observability"
    shutil.copytree(OBSERVABILITY_ROOT, bundle)
    siem_path = bundle / "inactive" / "siem-export.yaml"
    payload = yaml.safe_load(siem_path.read_text(encoding="utf-8"))
    payload["delivery"]["loss_metric"] = "schemabridge_process_ready"
    siem_path.write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ObservabilityContractError, match="observability bundle rejected"):
        validate_observability_bundle(bundle)
