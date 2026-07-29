from __future__ import annotations

import io
import json
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from schemabridge.adapters.observability.metrics import OpenMetricsRegistry
from schemabridge.adapters.observability.runtime import RuntimeOperationalTelemetry
from schemabridge.application.operational_snapshot import RefreshOperationalSnapshot
from schemabridge.application.ports.operational_snapshot import (
    OperationalQueue,
    OperationalSnapshot,
    QueueOperationalSnapshot,
)
from schemabridge.entrypoints.observer.main import (
    MAX_METRICS_RESPONSE_BYTES,
    ObserverServices,
    create_observer_app,
)


class _Lifecycle:
    def __init__(
        self,
        *,
        open_failure: Exception | None = None,
        close_failure: Exception | None = None,
    ) -> None:
        self.events: list[str] = []
        self.open_failure = open_failure
        self.close_failure = close_failure

    def open(self) -> None:
        self.events.append("open")
        if self.open_failure is not None:
            raise self.open_failure

    def close(self) -> None:
        self.events.append("close")
        if self.close_failure is not None:
            raise self.close_failure


class _Readiness:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls = 0

    def require_ready(self) -> None:
        self.calls += 1
        if self.failure is not None:
            raise self.failure


class _Reader:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls = 0

    def read(self) -> OperationalSnapshot:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return OperationalSnapshot(
            queues=(
                QueueOperationalSnapshot(OperationalQueue.EXECUTION, 4, 12.0),
                QueueOperationalSnapshot(OperationalQueue.CATALOG, 1, 4.5),
                QueueOperationalSnapshot(OperationalQueue.PROFILE, 0, 0.0),
                QueueOperationalSnapshot(OperationalQueue.RECONCILIATION, 2, 7.25),
            )
        )


class _RefreshFailure:
    def execute(self) -> OperationalSnapshot:
        raise RuntimeError("postgresql://operator:secret@internal/control")


class _Metrics:
    def __init__(self, document: str) -> None:
        self.document = document
        self.calls = 0

    def render(self) -> str:
        self.calls += 1
        return self.document


class _BarrierMetricsCycle:
    def __init__(self) -> None:
        self.generation = 0
        self.first_render_entered = Event()
        self.release_first_render = Event()
        self.second_refresh_entered = Event()

    def execute(self) -> OperationalSnapshot:
        self.generation += 1
        if self.generation == 2:
            self.second_refresh_entered.set()
        return _Reader().read()

    def render(self) -> str:
        if self.generation == 1:
            self.first_render_entered.set()
            if not self.release_first_render.wait(timeout=2):
                raise RuntimeError("test render barrier timed out")
        observed_generation = self.generation
        return (
            "# HELP test_generation bounded test generation\n"
            "# TYPE test_generation gauge\n"
            f"test_generation {observed_generation}\n"
            "# EOF\n"
        )


def _application(
    *,
    readiness: _Readiness | None = None,
    reader: _Reader | None = None,
    lifecycle: _Lifecycle | None = None,
) -> tuple[object, _Readiness, _Reader, _Lifecycle]:
    resolved_readiness = readiness or _Readiness()
    resolved_reader = reader or _Reader()
    resolved_lifecycle = lifecycle or _Lifecycle()
    registry = OpenMetricsRegistry()
    app = create_observer_app(
        ObserverServices(
            readiness=resolved_readiness,
            refresh_snapshot=RefreshOperationalSnapshot(
                reader=resolved_reader,
                metrics=registry,
            ),
            metrics=registry,
        ),
        lifecycle_resources=(resolved_lifecycle,),
    )
    return app, resolved_readiness, resolved_reader, resolved_lifecycle


def test_observer_lifecycle_and_health_endpoints_are_dependency_scoped() -> None:
    app, readiness, reader, lifecycle = _application()

    with TestClient(app) as client:  # type: ignore[arg-type]
        assert lifecycle.events == ["open"]
        live = client.get("/health/live")
        assert live.status_code == 200
        assert live.json() == {"status": "live"}
        assert readiness.calls == 0
        assert reader.calls == 0

        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready"}
        assert readiness.calls == 1
        assert reader.calls == 0

    assert lifecycle.events == ["open", "close"]


def test_observer_lifecycle_failure_is_sanitized_and_closes_prior_resources() -> None:
    opened = _Lifecycle()
    failed = _Lifecycle(open_failure=RuntimeError("postgresql://operator:secret@internal/control"))
    registry = OpenMetricsRegistry()
    app = create_observer_app(
        ObserverServices(
            readiness=_Readiness(),
            refresh_snapshot=RefreshOperationalSnapshot(
                reader=_Reader(),
                metrics=registry,
            ),
            metrics=registry,
        ),
        lifecycle_resources=(opened, failed),
    )

    with pytest.raises(RuntimeError) as captured, TestClient(app):
        pass

    assert str(captured.value) == "observer lifecycle failed"
    assert "secret" not in str(captured.value)
    assert opened.events == ["open", "close"]
    assert failed.events == ["open"]


def test_metrics_refreshes_only_closed_control_plane_aggregates() -> None:
    app, readiness, reader, _lifecycle = _application()

    with TestClient(app) as client:  # type: ignore[arg-type]
        response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/openmetrics-text")
    assert response.headers["cache-control"] == "no-store"
    assert len(response.content) <= MAX_METRICS_RESPONSE_BYTES
    assert reader.calls == 1
    assert readiness.calls == 0
    assert 'schemabridge_queue_depth{queue="execution"} 4.0' in response.text
    assert 'schemabridge_queue_oldest_age_seconds{queue="reconciliation"} 7.25' in response.text
    for forbidden in (
        "workspace_id",
        "tenant_id",
        "job_id",
        "connection_id",
        "postgresql://",
        "operator:secret",
    ):
        assert forbidden not in response.text


def test_concurrent_scrapes_cannot_cross_the_refresh_render_barrier() -> None:
    cycle = _BarrierMetricsCycle()
    app = create_observer_app(
        ObserverServices(
            readiness=_Readiness(),
            refresh_snapshot=cycle,
            metrics=cycle,
        )
    )
    responses: dict[str, Response] = {}
    second_request_started = Event()

    with TestClient(app) as client:
        first = Thread(
            target=lambda: responses.__setitem__("first", client.get("/metrics")),
            daemon=True,
        )

        def request_second() -> None:
            second_request_started.set()
            responses["second"] = client.get("/metrics")

        second = Thread(target=request_second, daemon=True)
        first.start()
        assert cycle.first_render_entered.wait(timeout=1)
        second.start()
        assert second_request_started.wait(timeout=1)
        assert not cycle.second_refresh_entered.wait(timeout=0.25)
        cycle.release_first_render.set()
        first.join(timeout=2)
        second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    first_response = responses["first"]
    second_response = responses["second"]
    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert "test_generation 1\n" in first_response.text
    assert "test_generation 2\n" in second_response.text


def test_observer_exposes_no_docs_or_detail_endpoint() -> None:
    app, _readiness, _reader, _lifecycle = _application()

    with TestClient(app) as client:  # type: ignore[arg-type]
        for path in ("/docs", "/redoc", "/openapi.json", "/details", "/queues"):
            response = client.get(path)
            assert response.status_code == 404
            assert response.json() == {"status": "not_found"}


def test_readiness_failure_is_sanitized_and_does_not_refresh_metrics() -> None:
    readiness = _Readiness(
        RuntimeError("postgresql://operator:secret@internal/control schema=private")
    )
    app, _readiness, reader, _lifecycle = _application(readiness=readiness)

    with TestClient(app) as client:  # type: ignore[arg-type]
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert "secret" not in response.text
    assert reader.calls == 0


def test_metrics_failure_is_sanitized() -> None:
    app = create_observer_app(
        ObserverServices(
            readiness=_Readiness(),
            refresh_snapshot=_RefreshFailure(),
            metrics=_Metrics("credential=should-not-render\n"),
        )
    )

    with TestClient(app) as client:
        response = client.get("/metrics")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert "secret" not in response.text
    assert "credential" not in response.text


def test_metrics_response_is_fail_closed_when_renderer_exceeds_bound() -> None:
    metrics = _Metrics("x" * 1_025)
    app = create_observer_app(
        ObserverServices(
            readiness=_Readiness(),
            refresh_snapshot=RefreshOperationalSnapshot(
                reader=_Reader(),
                metrics=OpenMetricsRegistry(),
            ),
            metrics=metrics,
        ),
        max_metrics_response_bytes=1_024,
    )

    with TestClient(app) as client:
        response = client.get("/metrics")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert len(response.content) < 1_024


def test_observer_readiness_metric_stays_zero_until_schema_check_passes() -> None:
    registry = OpenMetricsRegistry()
    stream = io.StringIO()
    telemetry = RuntimeOperationalTelemetry(
        service="observer",
        environment="production",
        registry=registry,
        stream=stream,
    )
    readiness = _Readiness(RuntimeError("postgresql://observer:secret@private/control"))
    app = create_observer_app(
        ObserverServices(
            readiness=readiness,
            refresh_snapshot=RefreshOperationalSnapshot(
                reader=_Reader(),
                metrics=registry,
            ),
            metrics=registry,
        ),
        telemetry=telemetry,
    )

    with TestClient(app) as client:
        assert 'schemabridge_process_ready{service="observer"} 0.0' in registry.render()
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert 'schemabridge_process_ready{service="observer"} 0.0' in registry.render()
    events = tuple(json.loads(line) for line in stream.getvalue().splitlines())
    assert [event["outcome"] for event in events] == ["started", "failed"]
    assert events[-1]["error_code"] == "queue_unavailable"
    assert "postgresql://" not in stream.getvalue()
    assert "secret" not in stream.getvalue()


def test_observer_readiness_metric_turns_one_only_after_schema_check() -> None:
    registry = OpenMetricsRegistry()
    telemetry = RuntimeOperationalTelemetry(
        service="observer",
        environment="production",
        registry=registry,
        stream=io.StringIO(),
    )
    app = create_observer_app(
        ObserverServices(
            readiness=_Readiness(),
            refresh_snapshot=RefreshOperationalSnapshot(
                reader=_Reader(),
                metrics=registry,
            ),
            metrics=registry,
        ),
        telemetry=telemetry,
    )

    with TestClient(app) as client:
        assert 'schemabridge_process_ready{service="observer"} 0.0' in registry.render()
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert 'schemabridge_process_ready{service="observer"} 1.0' in registry.render()
