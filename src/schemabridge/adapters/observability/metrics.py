"""Dependency-free OpenMetrics exposition over a closed low-cardinality registry."""

from __future__ import annotations

import math
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn

MAX_SERIES_PER_METRIC = 256


class MetricKind(StrEnum):
    """Supported OpenMetrics sample families."""

    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


class MetricContractError(ValueError):
    """A metric name, label set, or value did not satisfy the closed registry."""


@dataclass(frozen=True, slots=True)
class MetricLabel:
    """One label with a finite public value vocabulary."""

    name: str
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    """Static public metadata for one metric family."""

    name: str
    help_text: str
    kind: MetricKind
    labels: tuple[MetricLabel, ...] = ()
    buckets: tuple[float, ...] = ()


def _label(name: str, *values: str) -> MetricLabel:
    return MetricLabel(name=name, values=tuple(values))


_SERVICES = (
    "api",
    "backup",
    "catalog",
    "migrator",
    "observer",
    "profile",
    "reconciler",
    "web",
    "worker",
)
_QUEUES = ("execution", "catalog", "profile", "reconciliation")
_OUTCOMES = ("success", "error", "denied")
_DURATION_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

OPERATIONAL_METRICS = (
    MetricDefinition(
        name="schemabridge_http_requests_total",
        help_text="Completed allowlisted business HTTP requests by service and outcome.",
        kind=MetricKind.COUNTER,
        labels=(_label("service", "api", "web"), _label("outcome", *_OUTCOMES)),
    ),
    MetricDefinition(
        name="schemabridge_http_request_duration_seconds",
        help_text="Completed allowlisted business HTTP request latency in seconds.",
        kind=MetricKind.HISTOGRAM,
        labels=(_label("service", "api", "web"), _label("outcome", *_OUTCOMES)),
        buckets=_DURATION_BUCKETS,
    ),
    MetricDefinition(
        name="schemabridge_http_authorization_denials_total",
        help_text="Authorization denials for allowlisted business HTTP requests.",
        kind=MetricKind.COUNTER,
        labels=(_label("service", "api", "web"),),
    ),
    MetricDefinition(
        name="schemabridge_queue_depth",
        help_text="Current bounded queue depth.",
        kind=MetricKind.GAUGE,
        labels=(_label("queue", *_QUEUES),),
    ),
    MetricDefinition(
        name="schemabridge_queue_oldest_age_seconds",
        help_text="Age of the oldest due queue item in seconds.",
        kind=MetricKind.GAUGE,
        labels=(_label("queue", *_QUEUES),),
    ),
    MetricDefinition(
        name="schemabridge_job_transitions_total",
        help_text="Durable queue transitions by finite transition category.",
        kind=MetricKind.COUNTER,
        labels=(
            _label("queue", *_QUEUES),
            _label(
                "transition",
                "lease_reclaimed",
                "retry_scheduled",
                "dead_lettered",
                "authorization_stale",
                "cancelled",
            ),
        ),
    ),
    MetricDefinition(
        name="schemabridge_job_cancellation_latency_seconds",
        help_text="Latency from cancellation request to terminal acknowledgement.",
        kind=MetricKind.HISTOGRAM,
        labels=(_label("queue", *_QUEUES),),
        buckets=_DURATION_BUCKETS,
    ),
    MetricDefinition(
        name="schemabridge_source_operations_total",
        help_text="Bounded source operations by capability and stable outcome.",
        kind=MetricKind.COUNTER,
        labels=(
            _label("capability", "preflight", "execution", "catalog", "profile"),
            _label("outcome", *_OUTCOMES, "timeout"),
        ),
    ),
    MetricDefinition(
        name="schemabridge_runtime_control_failures_total",
        help_text="Fail-closed route, secret, TLS, identity, and capability controls.",
        kind=MetricKind.COUNTER,
        labels=(
            _label("control", "route", "secret", "tls", "identity", "cross_capability"),
            _label("reason", "unavailable", "invalid", "denied", "expired", "mismatch"),
        ),
    ),
    MetricDefinition(
        name="schemabridge_backup_age_seconds",
        help_text="Age in seconds of the newest verified control-plane backup.",
        kind=MetricKind.GAUGE,
    ),
    MetricDefinition(
        name="schemabridge_restore_drill_age_seconds",
        help_text="Age in seconds of the newest successful fresh-target restore drill.",
        kind=MetricKind.GAUGE,
    ),
    MetricDefinition(
        name="schemabridge_integrity_failures_total",
        help_text="Failed audit-chain, release-policy, or backup-integrity checks.",
        kind=MetricKind.COUNTER,
        labels=(_label("control", "audit_chain", "release_policy", "backup_verification"),),
    ),
    MetricDefinition(
        name="schemabridge_capacity_utilization_ratio",
        help_text="Current utilization ratio for a closed runtime resource class.",
        kind=MetricKind.GAUGE,
        labels=(_label("resource", "worker", "database_pool", "queue", "telemetry_buffer"),),
    ),
    MetricDefinition(
        name="schemabridge_process_restarts_total",
        help_text="Process or pod restarts by closed service identity.",
        kind=MetricKind.COUNTER,
        labels=(_label("service", *_SERVICES),),
    ),
    MetricDefinition(
        name="schemabridge_process_ready",
        help_text="Whether a service instance is ready without performing source I/O.",
        kind=MetricKind.GAUGE,
        labels=(_label("service", *_SERVICES),),
    ),
    MetricDefinition(
        name="schemabridge_telemetry_exports_total",
        help_text="Sanitized SIEM export batches by closed delivery outcome.",
        kind=MetricKind.COUNTER,
        labels=(_label("outcome", "success", "error", "dropped"),),
    ),
    MetricDefinition(
        name="schemabridge_reconciliation_age_seconds",
        help_text="Age in seconds of the newest complete semantic reconciliation.",
        kind=MetricKind.GAUGE,
    ),
    MetricDefinition(
        name="schemabridge_backup_runs_total",
        help_text="Verified backup attempts by closed outcome.",
        kind=MetricKind.COUNTER,
        labels=(_label("outcome", "success", "error"),),
    ),
    MetricDefinition(
        name="schemabridge_release_policy_checks_total",
        help_text="Immutable release-policy checks by closed outcome.",
        kind=MetricKind.COUNTER,
        labels=(_label("outcome", "success", "error"),),
    ),
)


@dataclass(slots=True)
class _HistogramState:
    bucket_counts: list[int]
    count: int = 0
    total: float = 0.0


SeriesKey = tuple[str, ...]


def _reject() -> NoReturn:
    raise MetricContractError("operational metric sample rejected")


class OpenMetricsRegistry:
    """Thread-safe, in-memory samples for the immutable metric catalog."""

    def __init__(
        self,
        definitions: tuple[MetricDefinition, ...] = OPERATIONAL_METRICS,
    ) -> None:
        self._definitions = {item.name: item for item in definitions}
        if len(self._definitions) != len(definitions):
            raise ValueError("metric definitions must have unique names")
        self._counters: dict[tuple[str, SeriesKey], float] = {}
        self._gauges: dict[tuple[str, SeriesKey], float] = {}
        self._histograms: dict[tuple[str, SeriesKey], _HistogramState] = {}
        self._lock = threading.Lock()

    def increment_counter(
        self,
        name: str,
        labels: Mapping[str, str],
        *,
        amount: float = 1.0,
    ) -> None:
        """Increase one registered counter by a finite non-negative amount."""

        definition, series = self._sample_contract(name, labels, MetricKind.COUNTER)
        del definition
        if not _is_finite_non_negative(amount):
            _reject()
        key = (name, series)
        with self._lock:
            self._admit_series(name, series)
            self._counters[key] = self._counters.get(key, 0.0) + float(amount)

    def set_gauge(
        self,
        name: str,
        labels: Mapping[str, str],
        *,
        value: float,
    ) -> None:
        """Set one registered non-negative gauge."""

        definition, series = self._sample_contract(name, labels, MetricKind.GAUGE)
        del definition
        if not _is_finite_non_negative(value):
            _reject()
        key = (name, series)
        with self._lock:
            self._admit_series(name, series)
            self._gauges[key] = float(value)

    def set_gauges_atomically(
        self,
        samples: Sequence[tuple[str, Mapping[str, str], float]],
    ) -> None:
        """Validate and commit a gauge batch under one registry lock."""

        prepared: list[tuple[str, SeriesKey, float]] = []
        keys: set[tuple[str, SeriesKey]] = set()
        for name, labels, value in samples:
            _definition, series = self._sample_contract(name, labels, MetricKind.GAUGE)
            if not _is_finite_non_negative(value):
                _reject()
            key = (name, series)
            if key in keys:
                _reject()
            keys.add(key)
            prepared.append((name, series, float(value)))
        if not prepared:
            _reject()

        with self._lock:
            proposed_by_name: dict[str, set[SeriesKey]] = {}
            for name, series, _value in prepared:
                proposed_by_name.setdefault(name, set()).add(series)
            for name, proposed in proposed_by_name.items():
                current = self._series_for_metric(name)
                if len(current | proposed) > MAX_SERIES_PER_METRIC:
                    _reject()
            for name, series, value in prepared:
                self._gauges[(name, series)] = value

    def observe_histogram(
        self,
        name: str,
        labels: Mapping[str, str],
        *,
        value: float,
    ) -> None:
        """Observe one finite non-negative value in a registered histogram."""

        definition, series = self._sample_contract(name, labels, MetricKind.HISTOGRAM)
        if not _is_finite_non_negative(value):
            _reject()
        key = (name, series)
        with self._lock:
            self._admit_series(name, series)
            state = self._histograms.get(key)
            if state is None:
                state = _HistogramState(bucket_counts=[0] * len(definition.buckets))
                self._histograms[key] = state
            for index, upper_bound in enumerate(definition.buckets):
                if value <= upper_bound:
                    state.bucket_counts[index] += 1
            state.count += 1
            state.total += float(value)

    def render(self) -> str:
        """Render a deterministic OpenMetrics 1.0 text document."""

        lines: list[str] = []
        with self._lock:
            for definition in sorted(self._definitions.values(), key=lambda item: item.name):
                lines.append(f"# HELP {definition.name} {_escape_help(definition.help_text)}")
                lines.append(f"# TYPE {definition.name} {definition.kind.value}")
                if definition.kind is MetricKind.COUNTER:
                    self._render_scalars(lines, definition, self._counters)
                elif definition.kind is MetricKind.GAUGE:
                    self._render_scalars(lines, definition, self._gauges)
                else:
                    self._render_histograms(lines, definition)
        lines.append("# EOF")
        return "\n".join(lines) + "\n"

    def _sample_contract(
        self,
        name: str,
        labels: Mapping[str, str],
        expected_kind: MetricKind,
    ) -> tuple[MetricDefinition, SeriesKey]:
        definition = self._definitions.get(name)
        if definition is None:
            _reject()
        if definition.kind is not expected_kind:
            _reject()
        expected_names = {item.name for item in definition.labels}
        if set(labels) != expected_names:
            _reject()
        series: list[str] = []
        for label in definition.labels:
            value = labels[label.name]
            if value not in label.values:
                _reject()
            series.append(value)
        return definition, tuple(series)

    def _admit_series(self, name: str, series: SeriesKey) -> None:
        current = self._series_for_metric(name)
        if series not in current and len(current) >= MAX_SERIES_PER_METRIC:
            _reject()

    def _series_for_metric(self, name: str) -> set[SeriesKey]:
        return {
            key_series
            for key_name, key_series in (
                tuple(self._counters) + tuple(self._gauges) + tuple(self._histograms)
            )
            if key_name == name
        }

    def _render_scalars(
        self,
        lines: list[str],
        definition: MetricDefinition,
        values: Mapping[tuple[str, SeriesKey], float],
    ) -> None:
        samples = sorted(
            (
                (series, value)
                for (name, series), value in values.items()
                if name == definition.name
            ),
            key=lambda item: item[0],
        )
        for series, value in samples:
            labels = _render_labels(definition, series)
            lines.append(f"{definition.name}{labels} {value}")

    def _render_histograms(
        self,
        lines: list[str],
        definition: MetricDefinition,
    ) -> None:
        samples = sorted(
            (
                (series, state)
                for (name, series), state in self._histograms.items()
                if name == definition.name
            ),
            key=lambda item: item[0],
        )
        for series, state in samples:
            label_values = {
                label.name: value for label, value in zip(definition.labels, series, strict=True)
            }
            for upper_bound, count in zip(
                definition.buckets,
                state.bucket_counts,
                strict=True,
            ):
                labels = _render_label_mapping({**label_values, "le": _format_bound(upper_bound)})
                lines.append(f"{definition.name}_bucket{labels} {count}")
            infinity_labels = _render_label_mapping({**label_values, "le": "+Inf"})
            lines.append(f"{definition.name}_bucket{infinity_labels} {state.count}")
            base_labels = _render_label_mapping(label_values)
            lines.append(f"{definition.name}_sum{base_labels} {state.total}")
            lines.append(f"{definition.name}_count{base_labels} {state.count}")


def _is_finite_non_negative(value: float) -> bool:
    return not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _render_labels(definition: MetricDefinition, series: SeriesKey) -> str:
    return _render_label_mapping(
        {label.name: value for label, value in zip(definition.labels, series, strict=True)}
    )


def _render_label_mapping(labels: Mapping[str, str]) -> str:
    if not labels:
        return ""
    rendered = ",".join(
        f'{name}="{_escape_label_value(value)}"' for name, value in sorted(labels.items())
    )
    return f"{{{rendered}}}"


def _escape_help(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n")


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format_bound(value: float) -> str:
    return str(value).removesuffix(".0")
