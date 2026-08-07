"""Minimal HTTP observer for bounded control-plane metrics and health."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from threading import Lock
from time import perf_counter
from typing import Protocol

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHttpException

from schemabridge.application.ports.operational_snapshot import OperationalSnapshot
from schemabridge.application.ports.operational_telemetry import OperationalTelemetryPort

MAX_METRICS_RESPONSE_BYTES = 65_536
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class ObserverLifecycleResource(Protocol):
    """An explicitly injected observer process resource, normally the control pool."""

    def open(self) -> None:
        """Acquire and validate the resource."""

    def close(self) -> None:
        """Release the resource idempotently."""


class ObserverReadinessPort(Protocol):
    """Check only the injected control-plane schema and pool contract."""

    def require_ready(self) -> None:
        """Fail unless the control-plane contract is current and reachable."""


class ObserverSnapshotRefreshPort(Protocol):
    """Refresh the bounded operational registry."""

    def execute(self) -> OperationalSnapshot:
        """Publish one aggregate snapshot without source-database access."""


class ObserverMetricsPort(Protocol):
    """Render a bounded in-memory OpenMetrics registry."""

    def render(self) -> str:
        """Render without credentials, source I/O, or mutation."""


@dataclass(frozen=True, slots=True)
class ObserverServices:
    """All observer behavior supplied explicitly by the composition root."""

    readiness: ObserverReadinessPort
    refresh_snapshot: ObserverSnapshotRefreshPort
    metrics: ObserverMetricsPort


def create_observer_app(
    services: ObserverServices,
    *,
    lifecycle_resources: tuple[ObserverLifecycleResource, ...] = (),
    max_metrics_response_bytes: int = MAX_METRICS_RESPONSE_BYTES,
    telemetry: OperationalTelemetryPort | None = None,
) -> FastAPI:
    """Create the observer without constructing pools, credentials, or source clients."""

    if (
        isinstance(max_metrics_response_bytes, bool)
        or not isinstance(max_metrics_response_bytes, int)
        or not 1_024 <= max_metrics_response_bytes <= MAX_METRICS_RESPONSE_BYTES
    ):
        raise ValueError("observer metrics response limit is invalid")

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        opened: list[ObserverLifecycleResource] = []
        lifecycle_failed = False
        started = perf_counter()
        try:
            for resource in lifecycle_resources:
                resource.open()
                opened.append(resource)
            if telemetry is not None:
                telemetry.emit(
                    event="service.health",
                    outcome="started",
                    duration_ms=min(int((perf_counter() - started) * 1_000), 86_400_000),
                )
            yield
        except Exception:
            lifecycle_failed = True
        finally:
            for resource in reversed(opened):
                try:
                    resource.close()
                except Exception:
                    lifecycle_failed = True
        if lifecycle_failed:
            if telemetry is not None:
                with suppress(Exception):
                    telemetry.emit(
                        event="service.health",
                        outcome="failed",
                        duration_ms=min(int((perf_counter() - started) * 1_000), 86_400_000),
                        error_code="internal_failure",
                    )
            raise RuntimeError("observer lifecycle failed") from None

    app = FastAPI(
        title="SchemaBridge Observer",
        version="1",
        debug=False,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    metrics_cycle_lock = Lock()

    @app.exception_handler(StarletteHttpException)
    async def http_error(
        _request: object,
        error: StarletteHttpException,
    ) -> JSONResponse:
        status = error.status_code if error.status_code in {404, 405} else 500
        return _json_status(
            status,
            "not_found" if status == 404 else "method_not_allowed",
        )

    @app.get("/health/live", include_in_schema=False)
    def live() -> JSONResponse:
        return _json_status(200, "live")

    @app.get("/health/ready", include_in_schema=False)
    def ready() -> JSONResponse:
        started = perf_counter()
        try:
            services.readiness.require_ready()
            if telemetry is not None:
                telemetry.emit(
                    event="service.health",
                    outcome="succeeded",
                    duration_ms=min(int((perf_counter() - started) * 1_000), 86_400_000),
                )
        except Exception:
            if telemetry is not None:
                with suppress(Exception):
                    telemetry.emit(
                        event="service.health",
                        outcome="failed",
                        duration_ms=min(int((perf_counter() - started) * 1_000), 86_400_000),
                        error_code="queue_unavailable",
                    )
            return _json_status(503, "not_ready")
        return _json_status(200, "ready")

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        try:
            with metrics_cycle_lock:
                services.refresh_snapshot.execute()
                document = services.metrics.render()
                encoded = document.encode("utf-8")
                if len(encoded) > max_metrics_response_bytes:
                    raise ValueError("metrics response exceeds configured bound")
        except Exception:
            return _json_status(503, "unavailable")
        return Response(
            content=encoded,
            media_type="application/openmetrics-text; version=1.0.0",
            headers=_SECURITY_HEADERS,
        )

    return app


def _json_status(status_code: int, status: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"status": status},
        headers=_SECURITY_HEADERS,
    )


def build_observer_application(runtime: object | None = None) -> FastAPI:
    """Return the ASGI app composed exclusively by the bootstrap root."""

    from schemabridge.bootstrap import ObserverProcessRuntime, build_observer_process_runtime

    resolved = runtime or build_observer_process_runtime()
    if not isinstance(resolved, ObserverProcessRuntime):
        raise TypeError("observer runtime is invalid")
    return resolved.application


def main() -> None:
    """Serve one internal observer process with bounded concurrency."""

    from schemabridge.bootstrap import (
        build_observer_process_runtime,
        configure_runtime_logging,
    )

    logging_session = configure_runtime_logging(service="observer")
    try:
        import uvicorn

        logging_session.emit(
            event="service.health",
            outcome="started",
            duration_ms=0,
        )
        runtime = build_observer_process_runtime()
        logging_session.set_level(runtime.log_level)
        uvicorn.run(
            build_observer_application(runtime),
            host=runtime.bind_host,
            port=runtime.port,
            workers=1,
            limit_concurrency=runtime.limit_concurrency,
            timeout_graceful_shutdown=runtime.graceful_shutdown_seconds,
            access_log=False,
            proxy_headers=False,
            server_header=False,
            date_header=False,
            log_config=None,
        )
    except Exception:
        logging_session.set_level("ERROR")
        logging_session.emit(
            event="service.health",
            outcome="failed",
            duration_ms=0,
            error_code="internal_failure",
        )
        raise SystemExit(1) from None
    finally:
        logging_session.close()


if __name__ == "__main__":
    main()
