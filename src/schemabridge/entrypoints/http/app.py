"""FastAPI boundary for bounded authenticated execution jobs."""

from __future__ import annotations

import json
import logging
import re
import secrets
from asyncio import Lock
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from time import perf_counter
from typing import Annotated, Protocol

from fastapi import FastAPI, Header, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import URL
from starlette.exceptions import HTTPException as StarletteHttpException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from schemabridge.application.api_capacity import (
    ApiCapacityError,
    ApiCapacityErrorCode,
)
from schemabridge.application.api_workflows import (
    ExecutionJobUseCaseError,
    ExecutionJobUseCaseErrorCode,
)
from schemabridge.application.authentication import AuthenticationBoundaryError
from schemabridge.application.catalog_inventory import (
    CatalogUseCaseError,
    CatalogUseCaseErrorCode,
)
from schemabridge.application.ports.authentication import BearerAuthenticationPort
from schemabridge.application.ports.operational_telemetry import (
    OperationalErrorCode,
    OperationalEvent,
    OperationalOutcome,
    OperationalResourceAccessCause,
    OperationalTelemetryPort,
)
from schemabridge.application.ports.semantic_change_read import (
    SemanticChangeFindingFilter,
    SemanticChangeFindingPublic,
    SemanticChangeImpactFilter,
    SemanticChangeImpactPublic,
    SemanticChangePage,
    SemanticChangePageRequest,
    SemanticChangeReportFilter,
    SemanticChangeReportPublic,
)
from schemabridge.application.semantic_change_read import (
    SemanticChangeReadError,
    SemanticChangeReadErrorCode,
)
from schemabridge.domain.background_jobs import (
    BackgroundJob,
    JobSubmissionResult,
)
from schemabridge.domain.catalog_inventory import (
    CatalogAssetFilter,
    CatalogAssetLocator,
    CatalogAssetSummary,
    CatalogConnectionFilter,
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogConnectionRegistrationResult,
    CatalogConnectionSummary,
    CatalogFieldFilter,
    CatalogFieldSummary,
    CatalogRefreshId,
    CatalogRefreshMode,
    CatalogRefreshRequestResult,
    CatalogRefreshSummary,
    InventoryPage,
    InventoryPageRequest,
)
from schemabridge.domain.identity import AuthenticatedPrincipal
from schemabridge.entrypoints.http.schemas import (
    CatalogAssetListQuery,
    CatalogAssetPageResponse,
    CatalogConnectionDisableRequest,
    CatalogConnectionListQuery,
    CatalogConnectionPageResponse,
    CatalogConnectionRegistrationRequest,
    CatalogConnectionRegistrationResponse,
    CatalogConnectionResponse,
    CatalogFieldListQuery,
    CatalogFieldPageResponse,
    CatalogRefreshRequest,
    CatalogRefreshRequestResponse,
    CatalogRefreshResponse,
    ExecutionJobCancellationRequest,
    ExecutionJobResponse,
    ExecutionJobSubmissionRequest,
    ExecutionJobSubmissionResponse,
    HealthResponse,
    ProblemResponse,
    SemanticChangeFindingListQuery,
    SemanticChangeFindingPageResponse,
    SemanticChangeImpactListQuery,
    SemanticChangeImpactPageResponse,
    SemanticChangeReportListQuery,
    SemanticChangeReportPageResponse,
    SemanticChangeReportResponse,
)

logger = logging.getLogger(__name__)
_BEARER_PREFIX = "Bearer "
_JOB_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{2,199}$"
_CONNECTION_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{2,199}$"
_REFRESH_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{2,199}$"
_ASSET_ID_PATTERN = r"^[^\x00-\x1f\x7f]{1,500}$"
_SEMANTIC_CHANGE_REPORT_ID_PATTERN = r"^report_[0-9a-f]{64}$"
_IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9._~-]{16,128}$"
_JSON_MEDIA_TYPE = "application/json"
_SECURITY_HEADERS = (
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
)
_HTTP_RESOURCE_ACCESS_CAUSE_STATE = "schemabridge_http_resource_access_cause"
_BUSINESS_HTTP_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("POST", re.compile(r"^/v1/workflows/[a-z0-9][a-z0-9_-]{2,63}/execution-jobs$")),
    ("GET", re.compile(r"^/v1/execution-jobs/[a-z0-9][a-z0-9_-]{2,199}$")),
    ("POST", re.compile(r"^/v1/execution-jobs/[a-z0-9][a-z0-9_-]{2,199}/cancel$")),
    ("GET", re.compile(r"^/v1/catalog/connections$")),
    ("POST", re.compile(r"^/v1/catalog/connections$")),
    ("POST", re.compile(r"^/v1/catalog/connections/[a-z0-9][a-z0-9_-]{2,199}/disable$")),
    ("GET", re.compile(r"^/v1/catalog/connections/[a-z0-9][a-z0-9_-]{2,199}/assets$")),
    (
        "GET",
        re.compile(
            r"^/v1/catalog/connections/[a-z0-9][a-z0-9_-]{2,199}"
            r"/assets/[^\x00-\x1f\x7f]{1,500}/fields$"
        ),
    ),
    (
        "POST",
        re.compile(r"^/v1/catalog/connections/[a-z0-9][a-z0-9_-]{2,199}/refreshes$"),
    ),
    ("GET", re.compile(r"^/v1/catalog/refreshes/[a-z0-9][a-z0-9_-]{2,199}$")),
    ("GET", re.compile(r"^/v1/semantic-changes/reports$")),
    ("GET", re.compile(r"^/v1/semantic-changes/reports/report_[0-9a-f]{64}$")),
    (
        "GET",
        re.compile(r"^/v1/semantic-changes/reports/report_[0-9a-f]{64}/findings$"),
    ),
    (
        "GET",
        re.compile(r"^/v1/semantic-changes/reports/report_[0-9a-f]{64}/impacts$"),
    ),
)


def _emit_safe_operational_event(
    telemetry: OperationalTelemetryPort | None,
    *,
    event: OperationalEvent,
    outcome: OperationalOutcome,
    duration_ms: int,
    correlation_id: str | None = None,
    error_code: OperationalErrorCode | None = None,
    counts: Mapping[str, int] | None = None,
) -> None:
    """Use the injected sink, retaining only a fixed safe fallback for test apps."""

    if telemetry is not None:
        with suppress(Exception):
            telemetry.emit(
                event=event,
                outcome=outcome,
                duration_ms=duration_ms,
                correlation_id=correlation_id,
                error_code=error_code,
                counts=counts,
            )
        return
    message = f"{event} outcome={outcome}"
    if error_code is not None:
        message = f"{message} error_code={error_code}"
    level = logging.ERROR if outcome in {"failed", "denied", "degraded"} else logging.INFO
    logger.log(level, message)


class _HttpTrafficClass(Enum):
    BUSINESS = auto()
    EXCLUDED = auto()


def _classify_http_traffic(scope: Scope) -> _HttpTrafficClass:
    method = scope.get("method")
    path = scope.get("path")
    if not isinstance(method, str) or not isinstance(path, str):
        return _HttpTrafficClass.EXCLUDED
    if any(
        method == expected_method and pattern.fullmatch(path) is not None
        for expected_method, pattern in _BUSINESS_HTTP_ROUTES
    ):
        return _HttpTrafficClass.BUSINESS
    return _HttpTrafficClass.EXCLUDED


def _mark_resource_access_cause(
    request: Request,
    cause: OperationalResourceAccessCause,
) -> None:
    state = request.scope.setdefault("state", {})
    state[_HTTP_RESOURCE_ACCESS_CAUSE_STATE] = cause


class ApiClockPort(Protocol):
    def now(self) -> datetime:
        """Return one aware current instant."""


class ApiLifecycleResource(Protocol):
    def open(self) -> None:
        """Acquire and validate process resources."""

    def close(self) -> None:
        """Release process resources idempotently."""


class ExecutionJobSubmissionPort(Protocol):
    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        workflow_id: str,
        expected_workflow_revision: int,
        expected_plan_fingerprint: str,
        confirmation: str,
        idempotency_key: str,
    ) -> JobSubmissionResult:
        """Authorize and atomically submit or replay one execution job."""


class ExecutionJobInspectionPort(Protocol):
    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        job_id: str,
    ) -> BackgroundJob:
        """Return one authorized sanitized job domain value."""


class ExecutionJobCancellationPort(Protocol):
    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        job_id: str,
    ) -> BackgroundJob:
        """Request or replay one authorized cancellation."""


class ApiReadinessPort(Protocol):
    def require_ready(self) -> None:
        """Fail with a sanitized exception unless dependencies are current."""


class ApiAdmissionPort(Protocol):
    def execute(self, principal: AuthenticatedPrincipal) -> object:
        """Apply one durable authenticated request admission."""


class CatalogConnectionListPort(Protocol):
    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        filters: CatalogConnectionFilter,
        page: InventoryPageRequest,
    ) -> InventoryPage[CatalogConnectionSummary]:
        """Return one bounded tenant-scoped connection page."""


class CatalogConnectionRegistrationPort(Protocol):
    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        connection_id: CatalogConnectionId,
        display_name: str,
        kind: CatalogConnectionKind,
        environment: str,
        catalog_scope: str,
        confirmation: str,
        idempotency_key: str,
        platform_instance: str | None = None,
    ) -> CatalogConnectionRegistrationResult:
        """Register or exactly replay public connection metadata."""


class CatalogConnectionDisablePort(Protocol):
    def execute(
        self,
        principal: AuthenticatedPrincipal,
        connection_id: CatalogConnectionId,
        *,
        confirmation: str,
        idempotency_key: str,
    ) -> CatalogConnectionSummary:
        """Logically disable one connection."""


class CatalogAssetListPort(Protocol):
    def execute(
        self,
        principal: AuthenticatedPrincipal,
        connection_id: CatalogConnectionId,
        *,
        filters: CatalogAssetFilter,
        page: InventoryPageRequest,
        generation: int | None = None,
    ) -> InventoryPage[CatalogAssetSummary]:
        """Return one bounded generation-bound asset page."""


class CatalogFieldListPort(Protocol):
    def execute(
        self,
        principal: AuthenticatedPrincipal,
        asset: CatalogAssetLocator,
        *,
        filters: CatalogFieldFilter,
        page: InventoryPageRequest,
        generation: int | None = None,
    ) -> InventoryPage[CatalogFieldSummary]:
        """Return one bounded generation-bound field page."""


class CatalogRefreshRequestPort(Protocol):
    def execute(
        self,
        principal: AuthenticatedPrincipal,
        connection_id: CatalogConnectionId,
        *,
        mode: CatalogRefreshMode,
        confirmation: str,
        idempotency_key: str,
    ) -> CatalogRefreshRequestResult:
        """Request or exactly replay one asynchronous metadata refresh."""


class CatalogRefreshInspectionPort(Protocol):
    def execute(
        self,
        principal: AuthenticatedPrincipal,
        refresh_id: CatalogRefreshId,
    ) -> CatalogRefreshSummary:
        """Return one tenant-scoped sanitized refresh summary."""


class SemanticChangeReportListPort(Protocol):
    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        filters: SemanticChangeReportFilter,
        page: SemanticChangePageRequest,
    ) -> SemanticChangePage[SemanticChangeReportPublic]:
        """Return one bounded tenant-scoped report page."""


class SemanticChangeReportInspectionPort(Protocol):
    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        report_id: str,
    ) -> SemanticChangeReportPublic:
        """Return one currently visible tenant-scoped report."""


class SemanticChangeFindingListPort(Protocol):
    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        report_id: str,
        filters: SemanticChangeFindingFilter,
        page: SemanticChangePageRequest,
    ) -> SemanticChangePage[SemanticChangeFindingPublic]:
        """Return one bounded page from a visible report."""


class SemanticChangeImpactListPort(Protocol):
    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        report_id: str,
        filters: SemanticChangeImpactFilter,
        page: SemanticChangePageRequest,
    ) -> SemanticChangePage[SemanticChangeImpactPublic]:
        """Return one bounded deduplicated blast-radius page."""


@dataclass(frozen=True, slots=True)
class CatalogHttpServices:
    list_connections: CatalogConnectionListPort
    register_connection: CatalogConnectionRegistrationPort
    disable_connection: CatalogConnectionDisablePort
    list_assets: CatalogAssetListPort
    list_fields: CatalogFieldListPort
    request_refresh: CatalogRefreshRequestPort
    inspect_refresh: CatalogRefreshInspectionPort


@dataclass(frozen=True, slots=True)
class SemanticChangeHttpServices:
    list_reports: SemanticChangeReportListPort
    inspect_report: SemanticChangeReportInspectionPort
    list_findings: SemanticChangeFindingListPort
    list_impacts: SemanticChangeImpactListPort


@dataclass(frozen=True, slots=True)
class ApiHttpServices:
    authenticator: BearerAuthenticationPort
    clock: ApiClockPort
    submit: ExecutionJobSubmissionPort
    inspect: ExecutionJobInspectionPort
    cancel: ExecutionJobCancellationPort
    readiness: ApiReadinessPort
    catalog: CatalogHttpServices | None = None
    admission: ApiAdmissionPort | None = None
    semantic_changes: SemanticChangeHttpServices | None = None


class ApiConcurrencyMiddleware:
    """Reject excess in-process work through the same sanitized HTTP contract."""

    def __init__(self, app: ASGIApp, *, max_concurrency: int) -> None:
        if isinstance(max_concurrency, bool) or not 1 <= max_concurrency <= 1_000:
            raise ValueError("API concurrency limit must be between 1 and 1000")
        self._app = app
        self._max_concurrency = max_concurrency
        self._active_requests = 0
        self._counter_lock = Lock()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        request_id = state.setdefault("request_id", secrets.token_hex(16))
        if not await self._try_enter():
            await _send_problem(
                send,
                status=503,
                code="service_busy",
                title="The service is temporarily busy.",
                request_id=request_id,
            )
            return
        try:
            await self._app(scope, receive, send)
        finally:
            await self._leave()

    async def _try_enter(self) -> bool:
        async with self._counter_lock:
            if self._active_requests >= self._max_concurrency:
                return False
            self._active_requests += 1
            return True

    async def _leave(self) -> None:
        async with self._counter_lock:
            self._active_requests -= 1


class ApiBoundaryMiddleware:
    """Enforce host/body/encoding bounds before request parsing."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int,
        allowed_hosts: tuple[str, ...],
        telemetry: OperationalTelemetryPort | None,
    ) -> None:
        self._app = app
        self._max_body_bytes = max_body_bytes
        self._allowed_hosts = frozenset(host.casefold() for host in allowed_hosts)
        self._telemetry = telemetry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        request_id = state.setdefault("request_id", secrets.token_hex(16))
        raw_headers = scope.get("headers", ())
        hosts = [value.decode("latin-1") for name, value in raw_headers if name.lower() == b"host"]
        try:
            hostname = URL(scope=scope).hostname
        except ValueError:
            hostname = None
        if len(hosts) != 1 or hostname is None or hostname.casefold() not in self._allowed_hosts:
            await _send_problem(
                send,
                status=400,
                code="invalid_host",
                title="The request host is not accepted.",
                request_id=request_id,
            )
            return
        encodings = [
            value.decode("latin-1").strip().casefold()
            for name, value in raw_headers
            if name.lower() == b"content-encoding"
        ]
        if encodings and encodings != ["identity"]:
            await _send_problem(
                send,
                status=415,
                code="unsupported_content_encoding",
                title="Compressed request bodies are not accepted.",
                request_id=request_id,
            )
            return
        content_lengths = [
            value.decode("ascii", errors="ignore")
            for name, value in raw_headers
            if name.lower() == b"content-length"
        ]
        if len(content_lengths) > 1:
            await _send_problem(
                send,
                status=400,
                code="invalid_content_length",
                title="The request content length is invalid.",
                request_id=request_id,
            )
            return
        if content_lengths:
            try:
                declared_length = int(content_lengths[0])
            except ValueError:
                declared_length = -1
            if declared_length < 0:
                await _send_problem(
                    send,
                    status=400,
                    code="invalid_content_length",
                    title="The request content length is invalid.",
                    request_id=request_id,
                )
                return
            if declared_length > self._max_body_bytes:
                await _send_problem(
                    send,
                    status=413,
                    code="request_too_large",
                    title="The request body exceeds the configured limit.",
                    request_id=request_id,
                )
                return
        if scope["method"] == "POST":
            content_types = [
                value.decode("latin-1").split(";", 1)[0].strip().casefold()
                for name, value in raw_headers
                if name.lower() == b"content-type"
            ]
            if len(content_types) != 1 or content_types[0] != _JSON_MEDIA_TYPE:
                await _send_problem(
                    send,
                    status=415,
                    code="unsupported_content_type",
                    title="POST request bodies must use application/json.",
                    request_id=request_id,
                )
                return

        received = 0

        async def bounded_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_body_bytes:
                    raise _RequestTooLarge
            return message

        pending_response: list[Message] = []

        async def request_id_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", ()))
                headers.append((b"x-request-id", request_id.encode("ascii")))
                headers.extend(_SECURITY_HEADERS)
                message = {**message, "headers": headers}
            pending_response.append(message)

        try:
            await self._app(scope, bounded_receive, request_id_send)
        except _RequestTooLarge:
            await _send_problem(
                send,
                status=413,
                code="request_too_large",
                title="The request body exceeds the configured limit.",
                request_id=request_id,
            )
            return
        except Exception:
            _emit_safe_operational_event(
                self._telemetry,
                event="http.request",
                outcome="failed",
                duration_ms=0,
                correlation_id=request_id,
                error_code="internal_failure",
                counts={"requests_completed": 1},
            )
            await _send_problem(
                send,
                status=500,
                code="internal_error",
                title="The request could not be completed.",
                request_id=request_id,
            )
            return
        for message in pending_response:
            await send(message)


class ApiOperationalTelemetryMiddleware:
    """Measure only allowlisted business traffic with a safe internal denial signal."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        telemetry: OperationalTelemetryPort,
    ) -> None:
        self._app = app
        self._telemetry = telemetry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        request_id = state.setdefault("request_id", secrets.token_hex(16))
        traffic_class = _classify_http_traffic(scope)
        started = perf_counter()
        status_code = 500

        async def status_send(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self._app(scope, receive, status_send)
        finally:
            if traffic_class is _HttpTrafficClass.BUSINESS:
                duration_ms = min(int((perf_counter() - started) * 1_000), 86_400_000)
                authorization_denied = state.get(
                    _HTTP_RESOURCE_ACCESS_CAUSE_STATE
                ) is OperationalResourceAccessCause.DENIED or status_code in {401, 403}
                outcome: OperationalOutcome
                error_code: OperationalErrorCode | None
                if status_code >= 500:
                    outcome = "failed"
                    error_code = "internal_failure"
                elif authorization_denied:
                    outcome = "denied"
                    error_code = "unauthorized"
                elif status_code == 429:
                    outcome = "denied"
                    error_code = "request_rejected"
                else:
                    outcome = "succeeded"
                    error_code = None
                counts = {"requests_completed": 1}
                if authorization_denied and status_code < 500:
                    counts["authorization_denials"] = 1
                self._telemetry.emit(
                    event="http.request",
                    outcome=outcome,
                    duration_ms=duration_ms,
                    correlation_id=request_id,
                    error_code=error_code,
                    counts=counts,
                )


class _RequestTooLarge(Exception):
    pass


def create_http_app(
    services: ApiHttpServices,
    *,
    max_body_bytes: int = 65_536,
    max_concurrency: int = 100,
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "testserver"),
    docs_enabled: bool = False,
    lifecycle_resources: tuple[ApiLifecycleResource, ...] = (),
    metrics_exporter: ApiLifecycleResource | None = None,
    telemetry: OperationalTelemetryPort | None = None,
) -> FastAPI:
    """Create an explicit dependency-injected HTTP application."""

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        opened: list[ApiLifecycleResource] = []
        metrics_exporter_opened = False
        started = perf_counter()
        try:
            for resource in lifecycle_resources:
                resource.open()
                opened.append(resource)
            if telemetry is not None:
                telemetry.emit(
                    event="service.health",
                    outcome="succeeded",
                    duration_ms=min(int((perf_counter() - started) * 1_000), 86_400_000),
                )
            if metrics_exporter is not None:
                metrics_exporter.open()
                metrics_exporter_opened = True
            yield
        except Exception:
            if telemetry is not None:
                telemetry.emit(
                    event="service.health",
                    outcome="failed",
                    duration_ms=min(int((perf_counter() - started) * 1_000), 86_400_000),
                    error_code="internal_failure",
                )
            raise
        finally:
            if metrics_exporter_opened and metrics_exporter is not None:
                metrics_exporter.close()
            for resource in reversed(opened):
                resource.close()

    app = FastAPI(
        title="SchemaBridge API",
        version="1",
        debug=False,
        docs_url="/docs" if docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_enabled else None,
        lifespan=lifespan,
    )
    app.add_middleware(
        ApiBoundaryMiddleware,
        max_body_bytes=max_body_bytes,
        allowed_hosts=allowed_hosts,
        telemetry=telemetry,
    )
    app.add_middleware(
        ApiConcurrencyMiddleware,
        max_concurrency=max_concurrency,
    )
    if telemetry is not None:
        app.add_middleware(
            ApiOperationalTelemetryMiddleware,
            telemetry=telemetry,
        )

    @app.exception_handler(StarletteHttpException)
    async def http_error(
        request: Request,
        error: StarletteHttpException,
    ) -> JSONResponse:
        status = error.status_code if 400 <= error.status_code <= 599 else 500
        code, title = {
            404: ("not_found", "The requested endpoint is not available."),
            405: ("method_not_allowed", "The request method is not accepted."),
        }.get(
            status,
            ("http_error", "The HTTP request could not be completed."),
        )
        return _problem_response(
            request,
            status=status,
            code=code,
            title=title,
        )

    @app.exception_handler(AuthenticationBoundaryError)
    async def authentication_error(
        request: Request,
        error: AuthenticationBoundaryError,
    ) -> JSONResponse:
        status = 503 if error.code == "authentication_unavailable" else 401
        return _problem_response(
            request,
            status=status,
            code=error.code,
            title=(
                "Authentication is temporarily unavailable."
                if status == 503
                else "Bearer authentication failed."
            ),
            headers={"WWW-Authenticate": "Bearer"} if status == 401 else None,
        )

    @app.exception_handler(ApiCapacityError)
    async def api_capacity_error(
        request: Request,
        error: ApiCapacityError,
    ) -> JSONResponse:
        if error.code is ApiCapacityErrorCode.RATE_LIMITED:
            retry_after = error.retry_after_seconds
            if retry_after is None or not 1 <= retry_after <= 60:
                retry_after = 60
            return _problem_response(
                request,
                status=429,
                code=error.code.value,
                title="The authenticated request rate has been reached.",
                headers={"Retry-After": str(retry_after)},
            )
        return _problem_response(
            request,
            status=503,
            code=error.code.value,
            title="API capacity admission is temporarily unavailable.",
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        del error
        return _problem_response(
            request,
            status=422,
            code="invalid_request",
            title="The request does not match the required schema.",
        )

    @app.exception_handler(ExecutionJobUseCaseError)
    async def execution_job_error(
        request: Request,
        error: ExecutionJobUseCaseError,
    ) -> JSONResponse:
        if error.code is ExecutionJobUseCaseErrorCode.UNAVAILABLE:
            assert error.resource_access_cause is not None
            _mark_resource_access_cause(request, error.resource_access_cause)
        status = {
            ExecutionJobUseCaseErrorCode.INVALID_REQUEST: 422,
            ExecutionJobUseCaseErrorCode.UNAVAILABLE: 404,
            ExecutionJobUseCaseErrorCode.IDEMPOTENCY_CONFLICT: 409,
            ExecutionJobUseCaseErrorCode.CAPACITY_EXCEEDED: 429,
            ExecutionJobUseCaseErrorCode.SERVICE_UNAVAILABLE: 503,
        }[error.code]
        return _problem_response(
            request,
            status=status,
            code=error.code.value,
            title={
                404: "The execution job is not available.",
                409: "The idempotency identity conflicts with another request.",
                429: "The tenant execution-job capacity has been reached.",
                422: "The execution-job request is invalid.",
                503: "The execution-job service is temporarily unavailable.",
            }[status],
        )

    @app.exception_handler(CatalogUseCaseError)
    async def catalog_error(
        request: Request,
        error: CatalogUseCaseError,
    ) -> JSONResponse:
        if error.code is CatalogUseCaseErrorCode.UNAVAILABLE:
            assert error.resource_access_cause is not None
            _mark_resource_access_cause(request, error.resource_access_cause)
        status = {
            CatalogUseCaseErrorCode.INVALID_REQUEST: 422,
            CatalogUseCaseErrorCode.UNAVAILABLE: 404,
            CatalogUseCaseErrorCode.IDEMPOTENCY_CONFLICT: 409,
            CatalogUseCaseErrorCode.CAPACITY_EXCEEDED: 429,
            CatalogUseCaseErrorCode.CURSOR_UNAVAILABLE: 404,
            CatalogUseCaseErrorCode.SERVICE_UNAVAILABLE: 503,
        }[error.code]
        return _problem_response(
            request,
            status=status,
            code=error.code.value,
            title={
                CatalogUseCaseErrorCode.INVALID_REQUEST: ("The catalog request is invalid."),
                CatalogUseCaseErrorCode.UNAVAILABLE: ("The catalog resource is not available."),
                CatalogUseCaseErrorCode.IDEMPOTENCY_CONFLICT: (
                    "The idempotency identity conflicts with another request."
                ),
                CatalogUseCaseErrorCode.CAPACITY_EXCEEDED: (
                    "The tenant catalog capacity has been reached."
                ),
                CatalogUseCaseErrorCode.CURSOR_UNAVAILABLE: (
                    "The inventory cursor is not available."
                ),
                CatalogUseCaseErrorCode.SERVICE_UNAVAILABLE: (
                    "The catalog service is temporarily unavailable."
                ),
            }[error.code],
            headers={"Retry-After": "60"} if status == 429 else None,
        )

    @app.exception_handler(SemanticChangeReadError)
    async def semantic_change_read_error(
        request: Request,
        error: SemanticChangeReadError,
    ) -> JSONResponse:
        if error.code is SemanticChangeReadErrorCode.UNAVAILABLE:
            assert error.resource_access_cause is not None
            _mark_resource_access_cause(request, error.resource_access_cause)
        status = {
            SemanticChangeReadErrorCode.INVALID_REQUEST: 422,
            SemanticChangeReadErrorCode.UNAVAILABLE: 404,
            SemanticChangeReadErrorCode.SERVICE_UNAVAILABLE: 503,
        }[error.code]
        return _problem_response(
            request,
            status=status,
            code=error.code.value,
            title={
                SemanticChangeReadErrorCode.INVALID_REQUEST: (
                    "The semantic-change request is invalid."
                ),
                SemanticChangeReadErrorCode.UNAVAILABLE: (
                    "The semantic-change resource is not available."
                ),
                SemanticChangeReadErrorCode.SERVICE_UNAVAILABLE: (
                    "The semantic-change service is temporarily unavailable."
                ),
            }[error.code],
        )

    def principal(request: Request) -> AuthenticatedPrincipal:
        raw_values = request.headers.getlist("authorization")
        if (
            len(raw_values) != 1
            or not raw_values[0].startswith(_BEARER_PREFIX)
            or len(raw_values[0]) <= len(_BEARER_PREFIX)
        ):
            raise AuthenticationBoundaryError("invalid_bearer_token")
        authenticated = services.authenticator.authenticate(
            raw_values[0][len(_BEARER_PREFIX) :],
            services.clock.now(),
        )
        if services.admission is not None:
            services.admission.execute(authenticated)
        return authenticated

    @app.get("/health/live", response_model=HealthResponse)
    def live() -> HealthResponse:
        return HealthResponse(status="live")

    @app.get(
        "/health/ready",
        response_model=HealthResponse,
        responses={503: {"model": ProblemResponse}},
    )
    def ready(request: Request) -> HealthResponse | JSONResponse:
        try:
            services.readiness.require_ready()
        except Exception:
            _emit_safe_operational_event(
                telemetry,
                event="service.health",
                outcome="failed",
                duration_ms=0,
                correlation_id=_request_id(request),
                error_code="queue_unavailable",
            )
            return _problem_response(
                request,
                status=503,
                code="not_ready",
                title="The service is not ready.",
            )
        _emit_safe_operational_event(
            telemetry,
            event="service.health",
            outcome="succeeded",
            duration_ms=0,
            correlation_id=_request_id(request),
        )
        return HealthResponse(status="ready")

    @app.post(
        "/v1/workflows/{workflow_id}/execution-jobs",
        response_model=ExecutionJobSubmissionResponse,
        responses={
            200: {"model": ExecutionJobSubmissionResponse},
            202: {"model": ExecutionJobSubmissionResponse},
        },
    )
    def submit_job(
        body: ExecutionJobSubmissionRequest,
        response: Response,
        request: Request,
        workflow_id: str = Path(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$"),
        idempotency_key: str = Header(
            alias="Idempotency-Key",
            min_length=16,
            max_length=128,
            pattern=_IDEMPOTENCY_PATTERN,
        ),
    ) -> ExecutionJobSubmissionResponse:
        idempotency_values = request.headers.getlist("idempotency-key")
        if len(idempotency_values) != 1 or idempotency_values[0] != idempotency_key:
            raise ExecutionJobUseCaseError(
                ExecutionJobUseCaseErrorCode.INVALID_REQUEST,
                "The execution-job request is invalid.",
            )
        result = services.submit.execute(
            principal(request),
            workflow_id=workflow_id,
            expected_workflow_revision=body.expected_workflow_revision,
            expected_plan_fingerprint=body.expected_plan_fingerprint,
            confirmation=body.confirmation,
            idempotency_key=idempotency_key,
        )
        response.status_code = 200 if result.replayed else 202
        return ExecutionJobSubmissionResponse(
            job=ExecutionJobResponse.from_domain(result.job),
            replayed=result.replayed,
        )

    @app.get(
        "/v1/execution-jobs/{job_id}",
        response_model=ExecutionJobResponse,
    )
    def inspect_job(
        request: Request,
        job_id: str = Path(pattern=_JOB_ID_PATTERN),
    ) -> ExecutionJobResponse:
        job = services.inspect.execute(principal(request), job_id=job_id)
        return ExecutionJobResponse.from_domain(job)

    @app.post(
        "/v1/execution-jobs/{job_id}/cancel",
        response_model=ExecutionJobResponse,
    )
    def cancel_job(
        body: ExecutionJobCancellationRequest,
        request: Request,
        job_id: str = Path(pattern=_JOB_ID_PATTERN),
    ) -> ExecutionJobResponse:
        job = services.cancel.execute(
            principal(request),
            job_id=job_id,
        )
        return ExecutionJobResponse.from_domain(job)

    @app.get(
        "/v1/catalog/connections",
        response_model=CatalogConnectionPageResponse,
    )
    def list_catalog_connections(
        request: Request,
        query: Annotated[CatalogConnectionListQuery, Query()],
    ) -> CatalogConnectionPageResponse:
        authenticated = principal(request)
        catalog = _catalog_services(services)
        try:
            filters = query.to_filter()
            page = InventoryPageRequest(size=query.page_size, cursor=query.cursor)
        except ValueError as error:
            raise _catalog_invalid_request() from error
        result = catalog.list_connections.execute(
            authenticated,
            filters=filters,
            page=page,
        )
        return CatalogConnectionPageResponse.from_domain(result)

    @app.post(
        "/v1/catalog/connections",
        response_model=CatalogConnectionRegistrationResponse,
        responses={
            200: {"model": CatalogConnectionRegistrationResponse},
            201: {"model": CatalogConnectionRegistrationResponse},
        },
    )
    def register_catalog_connection(
        body: CatalogConnectionRegistrationRequest,
        response: Response,
        request: Request,
        idempotency_key: str = Header(
            alias="Idempotency-Key",
            min_length=16,
            max_length=128,
            pattern=_IDEMPOTENCY_PATTERN,
        ),
    ) -> CatalogConnectionRegistrationResponse:
        _require_single_idempotency_header(request, idempotency_key)
        authenticated = principal(request)
        catalog = _catalog_services(services)
        try:
            connection_id = CatalogConnectionId(body.connection_id)
        except ValueError as error:
            raise _catalog_invalid_request() from error
        result = catalog.register_connection.execute(
            authenticated,
            connection_id=connection_id,
            display_name=body.display_name,
            kind=body.kind,
            environment=body.environment,
            catalog_scope=body.catalog_scope,
            confirmation=body.confirmation,
            idempotency_key=idempotency_key,
            platform_instance=body.platform_instance,
        )
        response.status_code = 200 if result.replayed else 201
        return CatalogConnectionRegistrationResponse.from_domain(result)

    @app.post(
        "/v1/catalog/connections/{connection_id}/disable",
        response_model=CatalogConnectionResponse,
    )
    def disable_catalog_connection(
        body: CatalogConnectionDisableRequest,
        request: Request,
        connection_id: str = Path(pattern=_CONNECTION_ID_PATTERN),
        idempotency_key: str = Header(
            alias="Idempotency-Key",
            min_length=16,
            max_length=128,
            pattern=_IDEMPOTENCY_PATTERN,
        ),
    ) -> CatalogConnectionResponse:
        _require_single_idempotency_header(request, idempotency_key)
        authenticated = principal(request)
        catalog = _catalog_services(services)
        result = catalog.disable_connection.execute(
            authenticated,
            _catalog_connection_id(connection_id),
            confirmation=body.confirmation,
            idempotency_key=idempotency_key,
        )
        return CatalogConnectionResponse.from_domain(result)

    @app.get(
        "/v1/catalog/connections/{connection_id}/assets",
        response_model=CatalogAssetPageResponse,
    )
    def list_catalog_assets(
        request: Request,
        query: Annotated[CatalogAssetListQuery, Query()],
        connection_id: str = Path(pattern=_CONNECTION_ID_PATTERN),
    ) -> CatalogAssetPageResponse:
        authenticated = principal(request)
        catalog = _catalog_services(services)
        try:
            filters = query.to_filter()
            page = InventoryPageRequest(size=query.page_size, cursor=query.cursor)
        except ValueError as error:
            raise _catalog_invalid_request() from error
        result = catalog.list_assets.execute(
            authenticated,
            _catalog_connection_id(connection_id),
            filters=filters,
            page=page,
            generation=query.generation,
        )
        return CatalogAssetPageResponse.from_domain(result)

    @app.get(
        "/v1/catalog/connections/{connection_id}/assets/{asset_id:path}/fields",
        response_model=CatalogFieldPageResponse,
    )
    def list_catalog_fields(
        request: Request,
        query: Annotated[CatalogFieldListQuery, Query()],
        connection_id: str = Path(pattern=_CONNECTION_ID_PATTERN),
        asset_id: str = Path(pattern=_ASSET_ID_PATTERN),
    ) -> CatalogFieldPageResponse:
        catalog = _catalog_services(services)
        authenticated = principal(request)
        try:
            filters = query.to_filter()
            page = InventoryPageRequest(size=query.page_size, cursor=query.cursor)
            asset = CatalogAssetLocator(
                workspace_id=authenticated.workspace_id,
                connection_id=_catalog_connection_id(connection_id),
                asset_id=asset_id,
            )
        except ValueError as error:
            raise _catalog_invalid_request() from error
        result = catalog.list_fields.execute(
            authenticated,
            asset,
            filters=filters,
            page=page,
            generation=query.generation,
        )
        return CatalogFieldPageResponse.from_domain(result)

    @app.post(
        "/v1/catalog/connections/{connection_id}/refreshes",
        response_model=CatalogRefreshRequestResponse,
        responses={
            200: {"model": CatalogRefreshRequestResponse},
            202: {"model": CatalogRefreshRequestResponse},
        },
    )
    def request_catalog_refresh(
        body: CatalogRefreshRequest,
        response: Response,
        request: Request,
        connection_id: str = Path(pattern=_CONNECTION_ID_PATTERN),
        idempotency_key: str = Header(
            alias="Idempotency-Key",
            min_length=16,
            max_length=128,
            pattern=_IDEMPOTENCY_PATTERN,
        ),
    ) -> CatalogRefreshRequestResponse:
        _require_single_idempotency_header(request, idempotency_key)
        authenticated = principal(request)
        catalog = _catalog_services(services)
        result = catalog.request_refresh.execute(
            authenticated,
            _catalog_connection_id(connection_id),
            mode=body.mode,
            confirmation=body.confirmation,
            idempotency_key=idempotency_key,
        )
        response.status_code = 200 if result.replayed else 202
        return CatalogRefreshRequestResponse.from_domain(result)

    @app.get(
        "/v1/catalog/refreshes/{refresh_id}",
        response_model=CatalogRefreshResponse,
    )
    def inspect_catalog_refresh(
        request: Request,
        refresh_id: str = Path(pattern=_REFRESH_ID_PATTERN),
    ) -> CatalogRefreshResponse:
        authenticated = principal(request)
        catalog = _catalog_services(services)
        try:
            identifier = CatalogRefreshId(refresh_id)
        except ValueError as error:
            raise _catalog_invalid_request() from error
        result = catalog.inspect_refresh.execute(authenticated, identifier)
        return CatalogRefreshResponse.from_domain(result)

    @app.get(
        "/v1/semantic-changes/reports",
        response_model=SemanticChangeReportPageResponse,
    )
    def list_semantic_change_reports(
        request: Request,
        query: Annotated[SemanticChangeReportListQuery, Query()],
    ) -> SemanticChangeReportPageResponse:
        authenticated = principal(request)
        semantic_changes = _semantic_change_services(services)
        result = semantic_changes.list_reports.execute(
            authenticated,
            filters=query.to_filter(),
            page=SemanticChangePageRequest(
                size=query.page_size,
                cursor=query.cursor,
            ),
        )
        return SemanticChangeReportPageResponse.from_projection(result)

    @app.get(
        "/v1/semantic-changes/reports/{report_id}",
        response_model=SemanticChangeReportResponse,
    )
    def inspect_semantic_change_report(
        request: Request,
        report_id: str = Path(pattern=_SEMANTIC_CHANGE_REPORT_ID_PATTERN),
    ) -> SemanticChangeReportResponse:
        authenticated = principal(request)
        semantic_changes = _semantic_change_services(services)
        result = semantic_changes.inspect_report.execute(
            authenticated,
            report_id=report_id,
        )
        return SemanticChangeReportResponse.from_projection(result)

    @app.get(
        "/v1/semantic-changes/reports/{report_id}/findings",
        response_model=SemanticChangeFindingPageResponse,
    )
    def list_semantic_change_findings(
        request: Request,
        query: Annotated[SemanticChangeFindingListQuery, Query()],
        report_id: str = Path(pattern=_SEMANTIC_CHANGE_REPORT_ID_PATTERN),
    ) -> SemanticChangeFindingPageResponse:
        authenticated = principal(request)
        semantic_changes = _semantic_change_services(services)
        result = semantic_changes.list_findings.execute(
            authenticated,
            report_id=report_id,
            filters=query.to_filter(),
            page=SemanticChangePageRequest(
                size=query.page_size,
                cursor=query.cursor,
            ),
        )
        return SemanticChangeFindingPageResponse.from_projection(result)

    @app.get(
        "/v1/semantic-changes/reports/{report_id}/impacts",
        response_model=SemanticChangeImpactPageResponse,
    )
    def list_semantic_change_impacts(
        request: Request,
        query: Annotated[SemanticChangeImpactListQuery, Query()],
        report_id: str = Path(pattern=_SEMANTIC_CHANGE_REPORT_ID_PATTERN),
    ) -> SemanticChangeImpactPageResponse:
        authenticated = principal(request)
        semantic_changes = _semantic_change_services(services)
        result = semantic_changes.list_impacts.execute(
            authenticated,
            report_id=report_id,
            filters=query.to_filter(),
            page=SemanticChangePageRequest(
                size=query.page_size,
                cursor=query.cursor,
            ),
        )
        return SemanticChangeImpactPageResponse.from_projection(result)

    return app


def _catalog_services(services: ApiHttpServices) -> CatalogHttpServices:
    if services.catalog is None:
        raise CatalogUseCaseError(
            CatalogUseCaseErrorCode.SERVICE_UNAVAILABLE,
            "catalog service is unavailable",
        )
    return services.catalog


def _semantic_change_services(
    services: ApiHttpServices,
) -> SemanticChangeHttpServices:
    if services.semantic_changes is None:
        raise SemanticChangeReadError(
            SemanticChangeReadErrorCode.SERVICE_UNAVAILABLE,
            "semantic change service is unavailable",
        )
    return services.semantic_changes


def _catalog_connection_id(value: str) -> CatalogConnectionId:
    try:
        return CatalogConnectionId(value)
    except ValueError as error:
        raise _catalog_invalid_request() from error


def _catalog_invalid_request() -> CatalogUseCaseError:
    return CatalogUseCaseError(
        CatalogUseCaseErrorCode.INVALID_REQUEST,
        "catalog request is invalid",
    )


def _require_single_idempotency_header(
    request: Request,
    parsed_value: str,
) -> None:
    values = request.headers.getlist("idempotency-key")
    if len(values) != 1 or values[0] != parsed_value:
        raise _catalog_invalid_request()


def _problem_response(
    request: Request,
    *,
    status: int,
    code: str,
    title: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    problem = ProblemResponse(
        type=f"urn:schemabridge:problem:{code}",
        title=title,
        status=status,
        code=code,
        request_id=_request_id(request),
    )
    return JSONResponse(
        problem.model_dump(mode="json"),
        status_code=status,
        media_type="application/problem+json",
        headers=headers,
    )


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", "")
    return value if isinstance(value, str) and len(value) == 32 else secrets.token_hex(16)


async def _send_problem(
    send: Callable[[Message], Awaitable[None]],
    *,
    status: int,
    code: str,
    title: str,
    request_id: str,
) -> None:
    payload = ProblemResponse(
        type=f"urn:schemabridge:problem:{code}",
        title=title,
        status=status,
        code=code,
        request_id=request_id,
    )
    body = json.dumps(
        payload.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": (
                (b"content-type", b"application/problem+json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"x-request-id", request_id.encode("ascii")),
                *_SECURITY_HEADERS,
            ),
        }
    )
    await send({"type": "http.response.body", "body": body})
