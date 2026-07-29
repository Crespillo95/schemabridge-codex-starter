from __future__ import annotations

import io
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Event
from urllib.request import urlopen

from fastapi.testclient import TestClient

from schemabridge.adapters.identity.local import LocalDemoPrincipalFactory
from schemabridge.adapters.observability.http_export import MetricsHttpExporter
from schemabridge.adapters.observability.runtime import RuntimeOperationalTelemetry
from schemabridge.application.api_capacity import (
    ApiCapacityError,
    ApiCapacityErrorCode,
)
from schemabridge.application.api_workflows import (
    ExecutionJobUseCaseError,
    ExecutionJobUseCaseErrorCode,
)
from schemabridge.application.ports.operational_telemetry import (
    OperationalResourceAccessCause,
    OperationalTelemetryPort,
)
from schemabridge.domain.background_jobs import (
    BackgroundJob,
    JobAuthorization,
    JobSubmissionResult,
)
from schemabridge.domain.identity import AuthenticatedPrincipal, IdentityRole
from schemabridge.entrypoints.http.app import ApiHttpServices, create_http_app

NOW = datetime(2026, 7, 23, 19, 0, tzinfo=UTC)
FINGERPRINT = "a" * 64
IDEMPOTENCY_KEY = "browser-test-idempotency-key-001"


class _Clock:
    def now(self) -> datetime:
        return NOW


class _Authenticator:
    def authenticate(self, bearer_token: str, now: datetime) -> AuthenticatedPrincipal:
        assert now == NOW
        if bearer_token != "accepted-local-test-token":
            from schemabridge.application.authentication import AuthenticationBoundaryError

            raise AuthenticationBoundaryError("invalid_bearer_token")
        return LocalDemoPrincipalFactory(
            workspace="local-demo",
            subject="api-test",
            roles=frozenset({IdentityRole.ANALYST}),
        ).create(now=now)


@dataclass
class _Submit:
    replayed: bool = False
    error: Exception | None = None

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
        assert principal.roles == frozenset({IdentityRole.ANALYST})
        assert expected_workflow_revision == 7
        assert expected_plan_fingerprint == FINGERPRINT
        assert confirmation == "EXECUTE GOVERNED PREVIEW"
        assert idempotency_key == IDEMPOTENCY_KEY
        if self.error is not None:
            raise self.error
        return JobSubmissionResult(job=_job(workflow_id), replayed=self.replayed)


class _Inspect:
    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        job_id: str,
    ) -> BackgroundJob:
        del principal
        job = _job("workflow-1")
        assert job_id == job.id
        return job


class _Cancel:
    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        job_id: str,
    ) -> BackgroundJob:
        del principal
        assert job_id == _job("workflow-1").id
        return _job("workflow-1")


@dataclass
class _Readiness:
    error: Exception | None = None

    def require_ready(self) -> None:
        if self.error is not None:
            raise self.error


class _Lifecycle:
    def __init__(self) -> None:
        self.events: list[str] = []

    def open(self) -> None:
        self.events.append("opened")

    def close(self) -> None:
        self.events.append("closed")


@dataclass
class _Admission:
    error: ApiCapacityError | None = None
    calls: int = 0

    def execute(self, principal: AuthenticatedPrincipal) -> object:
        del principal
        self.calls += 1
        if self.error is not None:
            raise self.error
        return object()


def _client(
    *,
    submit: _Submit | None = None,
    readiness: _Readiness | None = None,
    admission: _Admission | None = None,
    max_body_bytes: int = 65_536,
    max_concurrency: int = 100,
    docs_enabled: bool = False,
    telemetry: OperationalTelemetryPort | None = None,
) -> TestClient:
    app = create_http_app(
        ApiHttpServices(
            authenticator=_Authenticator(),
            clock=_Clock(),
            submit=submit or _Submit(),
            inspect=_Inspect(),
            cancel=_Cancel(),
            readiness=readiness or _Readiness(),
            admission=admission,
        ),
        max_body_bytes=max_body_bytes,
        max_concurrency=max_concurrency,
        docs_enabled=docs_enabled,
        telemetry=telemetry,
    )
    return TestClient(app, raise_server_exceptions=False)


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        port = candidate.getsockname()[1]
    assert isinstance(port, int)
    return port


def _headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer accepted-local-test-token",
        "Idempotency-Key": IDEMPOTENCY_KEY,
    }


def _body() -> dict[str, object]:
    return {
        "expected_workflow_revision": 7,
        "expected_plan_fingerprint": FINGERPRINT,
        "confirmation": "EXECUTE GOVERNED PREVIEW",
    }


def _job(workflow_id: str) -> BackgroundJob:
    authorization = JobAuthorization.create(
        workspace_id="workspace-1",
        workflow_id=workflow_id,
        workflow_owner_actor_id="actor-owner",
        submitting_actor_id="actor-submitter",
        expected_workflow_revision=7,
        expected_plan_fingerprint=FINGERPRINT,
        authenticated_at=NOW - timedelta(minutes=1),
        authorized_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    return BackgroundJob.create(
        id="job-v1-" + "b" * 64,
        authorization=authorization,
        idempotency_digest="c" * 64,
        max_attempts=3,
        created_at=NOW,
    )


def test_health_is_sanitized_and_docs_are_disabled_by_default() -> None:
    with _client() as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")
        docs = client.get("/docs")

    assert live.status_code == 200 and live.json() == {"status": "live"}
    assert ready.status_code == 200 and ready.json() == {"status": "ready"}
    assert docs.status_code == 404
    assert len(live.headers["x-request-id"]) == 32
    assert live.headers["cache-control"] == "no-store"
    assert live.headers["x-content-type-options"] == "nosniff"


def test_public_metrics_is_hidden_and_requests_emit_bounded_telemetry() -> None:
    stream = io.StringIO()
    telemetry = RuntimeOperationalTelemetry(
        service="api",
        environment="staging",
        stream=stream,
    )

    with _client(telemetry=telemetry) as client:
        business = client.get(
            f"/v1/execution-jobs/{_job('workflow-1').id}",
            headers={"Authorization": "Bearer accepted-local-test-token"},
        )
        live = client.get("/health/live")
        metrics = client.get("/metrics")

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    request_events = [item for item in events if item["event"] == "http.request"]
    assert business.status_code == 200
    assert len(request_events) == 1
    assert request_events[0]["correlation_id"] == business.headers["x-request-id"]
    assert request_events[0]["requests_completed"] == 1
    assert live.headers["x-request-id"] not in {item["correlation_id"] for item in request_events}
    assert metrics.headers["x-request-id"] not in {
        item["correlation_id"] for item in request_events
    }
    assert all(
        set(item)
        <= {
            "schema_version",
            "timestamp",
            "severity",
            "service",
            "environment",
            "event",
            "outcome",
            "duration_ms",
            "correlation_id",
            "requests_completed",
        }
        for item in request_events
    )
    assert metrics.status_code == 404
    assert metrics.headers["cache-control"] == "no-store"
    assert metrics.headers["content-type"].startswith("application/problem+json")
    assert metrics.json()["code"] == "not_found"
    assert (
        'schemabridge_http_requests_total{outcome="success",service="api"} 1.0'
        in telemetry.render_openmetrics()
    )


def test_collapsed_sensitive_404_is_publicly_indistinguishable_but_internally_denied() -> None:
    secret = "tenant=private-workspace resource=other-owner-job"
    stream = io.StringIO()
    telemetry = RuntimeOperationalTelemetry(
        service="api",
        environment="staging",
        stream=stream,
    )
    submit = _Submit(
        error=ExecutionJobUseCaseError(
            ExecutionJobUseCaseErrorCode.UNAVAILABLE,
            secret,
            resource_access_cause=OperationalResourceAccessCause.DENIED,
        )
    )

    with _client(submit=submit, telemetry=telemetry) as client:
        live = client.get("/health/live")
        unknown = client.get("/v1/not-a-real-business-route")
        denied = client.post(
            "/v1/workflows/workflow-1/execution-jobs",
            headers=_headers(),
            json=_body(),
        )

    assert denied.status_code == 404
    assert denied.json() == {
        "type": "urn:schemabridge:problem:execution_job_unavailable",
        "title": "The execution job is not available.",
        "status": 404,
        "code": "execution_job_unavailable",
        "request_id": denied.headers["x-request-id"],
    }
    assert secret not in denied.text
    assert "denied" not in denied.text.casefold()
    assert "unauthorized" not in denied.text.casefold()

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    request_events = [item for item in events if item["event"] == "http.request"]
    assert request_events == [
        {
            "schema_version": "schemabridge.telemetry.v1",
            "timestamp": request_events[0]["timestamp"],
            "severity": "warning",
            "service": "api",
            "environment": "staging",
            "event": "http.request",
            "outcome": "denied",
            "duration_ms": request_events[0]["duration_ms"],
            "correlation_id": denied.headers["x-request-id"],
            "error_code": "unauthorized",
            "authorization_denials": 1,
            "requests_completed": 1,
        }
    ]
    assert live.headers["x-request-id"] != request_events[0]["correlation_id"]
    assert unknown.headers["x-request-id"] != request_events[0]["correlation_id"]
    document = telemetry.render_openmetrics()
    assert 'schemabridge_http_requests_total{outcome="denied",service="api"} 1.0' in document
    assert 'schemabridge_http_authorization_denials_total{service="api"} 1.0' in document
    assert (
        'schemabridge_http_request_duration_seconds_count{outcome="denied",service="api"} 1'
        in document
    )
    assert 'schemabridge_http_requests_total{outcome="success",service="api"}' not in document


def test_legitimate_protected_404_does_not_increment_authorization_denials() -> None:
    stream = io.StringIO()
    telemetry = RuntimeOperationalTelemetry(
        service="api",
        environment="staging",
        stream=stream,
    )
    cause = OperationalResourceAccessCause.NOT_FOUND
    submit = _Submit(
        error=ExecutionJobUseCaseError(
            ExecutionJobUseCaseErrorCode.UNAVAILABLE,
            "private store detail",
            resource_access_cause=cause,
        )
    )

    with _client(submit=submit, telemetry=telemetry) as client:
        missing = client.post(
            "/v1/workflows/workflow-1/execution-jobs",
            headers=_headers(),
            json=_body(),
        )

    assert missing.status_code == 404
    assert missing.json() == {
        "type": "urn:schemabridge:problem:execution_job_unavailable",
        "title": "The execution job is not available.",
        "status": 404,
        "code": "execution_job_unavailable",
        "request_id": missing.headers["x-request-id"],
    }
    event = next(
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if '"event":"http.request"' in line
    )
    assert event["outcome"] == "succeeded"
    assert "error_code" not in event
    assert "authorization_denials" not in event
    assert (
        'schemabridge_http_authorization_denials_total{service="api"}'
        not in telemetry.render_openmetrics()
    )
    try:
        json.dumps(cause)
    except TypeError:
        pass
    else:
        raise AssertionError("internal resource access causes must not be JSON serializable")


def test_internal_metrics_exporter_shares_api_telemetry_and_lifecycle() -> None:
    port = _available_port()
    telemetry = RuntimeOperationalTelemetry(
        service="api",
        environment="staging",
        stream=io.StringIO(),
    )
    exporter = MetricsHttpExporter(
        bind_host="127.0.0.1",
        port=port,
        renderer=telemetry,
    )
    app = create_http_app(
        ApiHttpServices(
            authenticator=_Authenticator(),
            clock=_Clock(),
            submit=_Submit(),
            inspect=_Inspect(),
            cancel=_Cancel(),
            readiness=_Readiness(),
        ),
        telemetry=telemetry,
        metrics_exporter=exporter,
    )

    assert not exporter.is_open
    with TestClient(app, raise_server_exceptions=False) as client:
        assert exporter.is_open
        assert (
            client.get(
                f"/v1/execution-jobs/{_job('workflow-1').id}",
                headers={"Authorization": "Bearer accepted-local-test-token"},
            ).status_code
            == 200
        )
        with urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2) as response:
            document = response.read().decode()
        assert 'schemabridge_process_ready{service="api"} 1.0' in document
        assert 'schemabridge_http_requests_total{outcome="success",service="api"} 1.0' in document
    assert not exporter.is_open


def test_application_owns_explicit_resource_startup_and_shutdown() -> None:
    resource = _Lifecycle()
    app = create_http_app(
        ApiHttpServices(
            authenticator=_Authenticator(),
            clock=_Clock(),
            submit=_Submit(),
            inspect=_Inspect(),
            cancel=_Cancel(),
            readiness=_Readiness(),
        ),
        lifecycle_resources=(resource,),
    )

    assert resource.events == []
    with TestClient(app, raise_server_exceptions=False) as client:
        assert resource.events == ["opened"]
        assert client.get("/health/live").status_code == 200
    assert resource.events == ["opened", "closed"]


def test_readiness_failure_is_problem_json_without_dependency_detail() -> None:
    secret = "postgresql://runtime:password@control/private"
    with _client(readiness=_Readiness(RuntimeError(secret))) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "not_ready"
    assert secret not in response.text


def test_unknown_route_and_method_use_the_bounded_problem_contract() -> None:
    with _client() as client:
        missing = client.get("/does-not-exist")
        wrong_method = client.put("/health/live")

    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("application/problem+json")
    assert missing.json()["code"] == "not_found"
    assert wrong_method.status_code == 405
    assert wrong_method.headers["content-type"].startswith("application/problem+json")
    assert wrong_method.json()["code"] == "method_not_allowed"
    for response in (missing, wrong_method):
        assert response.json()["request_id"] == response.headers["x-request-id"]
        assert response.headers["cache-control"] == "no-store"


def test_concurrency_saturation_is_rejected_inside_the_problem_boundary() -> None:
    entered = Event()
    release = Event()

    class _BlockingReadiness:
        def require_ready(self) -> None:
            entered.set()
            assert release.wait(timeout=5)

    with (
        _client(
            readiness=_BlockingReadiness(),  # type: ignore[arg-type]
            max_concurrency=1,
        ) as client,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        active = executor.submit(client.get, "/health/ready")
        assert entered.wait(timeout=5)
        saturated = client.get("/health/live")
        release.set()
        completed = active.result(timeout=5)

    assert completed.status_code == 200
    assert saturated.status_code == 503
    assert saturated.headers["content-type"].startswith("application/problem+json")
    assert saturated.headers["cache-control"] == "no-store"
    assert saturated.json()["code"] == "service_busy"
    assert saturated.json()["request_id"] == saturated.headers["x-request-id"]


def test_submission_returns_202_then_200_for_exact_replay() -> None:
    with _client() as client:
        created = client.post(
            "/v1/workflows/workflow-1/execution-jobs",
            headers=_headers(),
            json=_body(),
        )
    with _client(submit=_Submit(replayed=True)) as client:
        replayed = client.post(
            "/v1/workflows/workflow-1/execution-jobs",
            headers=_headers(),
            json=_body(),
        )

    assert created.status_code == 202
    assert created.json()["replayed"] is False
    assert replayed.status_code == 200
    assert replayed.json()["job"]["job_id"] == created.json()["job"]["job_id"]


def test_distributed_api_admission_denial_is_a_safe_bounded_429() -> None:
    secret = "postgresql://capacity-user:private-password@control/rate_state"
    stream = io.StringIO()
    telemetry = RuntimeOperationalTelemetry(
        service="api",
        environment="staging",
        stream=stream,
    )
    admission = _Admission(
        error=ApiCapacityError(
            ApiCapacityErrorCode.RATE_LIMITED,
            secret,
            retry_after_seconds=17,
        )
    )
    submit = _Submit(error=AssertionError("submission must not run after admission denial"))

    with _client(admission=admission, submit=submit, telemetry=telemetry) as client:
        response = client.post(
            "/v1/workflows/workflow-1/execution-jobs",
            headers=_headers(),
            json=_body(),
        )

    assert response.status_code == 429
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["retry-after"] == "17"
    assert 1 <= int(response.headers["retry-after"]) <= 60
    assert response.json()["code"] == "api_rate_limited"
    assert response.json()["title"] == "The authenticated request rate has been reached."
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert secret not in response.text
    assert admission.calls == 1
    request_event = next(
        item
        for item in (json.loads(line) for line in stream.getvalue().splitlines())
        if item["event"] == "http.request"
    )
    assert request_event["outcome"] == "denied"
    assert request_event["error_code"] == "request_rejected"
    assert "authorization_denials" not in request_event
    assert 'schemabridge_http_authorization_denials_total{service="api"}' not in (
        telemetry.render_openmetrics()
    )


def test_malformed_retry_metadata_is_defensively_bounded_at_http_boundary() -> None:
    admission = _Admission(
        error=ApiCapacityError(
            ApiCapacityErrorCode.RATE_LIMITED,
            "untrusted adapter detail",
            retry_after_seconds=3_600,
        )
    )

    with _client(admission=admission) as client:
        response = client.get(
            f"/v1/execution-jobs/{_job('workflow-1').id}",
            headers={"Authorization": "Bearer accepted-local-test-token"},
        )

    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    assert "untrusted adapter detail" not in response.text


def test_execution_job_capacity_error_is_a_safe_http_429() -> None:
    secret = "tenant count=6 from private capacity table"
    submit = _Submit(
        error=ExecutionJobUseCaseError(
            ExecutionJobUseCaseErrorCode.CAPACITY_EXCEEDED,
            secret,
        )
    )

    with _client(submit=submit) as client:
        response = client.post(
            "/v1/workflows/workflow-1/execution-jobs",
            headers=_headers(),
            json=_body(),
        )

    assert response.status_code == 429
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "execution_job_capacity_exceeded"
    assert response.json()["title"] == "The tenant execution-job capacity has been reached."
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert secret not in response.text


def test_missing_or_wrong_bearer_is_one_generic_401() -> None:
    with _client() as client:
        missing = client.post(
            "/v1/workflows/workflow-1/execution-jobs",
            headers={"Idempotency-Key": IDEMPOTENCY_KEY},
            json=_body(),
        )
        wrong = client.post(
            "/v1/workflows/workflow-1/execution-jobs",
            headers={
                "Authorization": "Bearer wrong",
                "Idempotency-Key": IDEMPOTENCY_KEY,
            },
            json=_body(),
        )

    for response in (missing, wrong):
        assert response.status_code == 401
        assert response.json()["code"] == "invalid_bearer_token"
        assert response.headers["www-authenticate"] == "Bearer"
        assert "wrong" not in response.text


def test_invalid_request_is_sanitized_and_extra_sql_never_echoes() -> None:
    payload = {**_body(), "sql": "SELECT highly_sensitive_value"}
    with _client() as client:
        response = client.post(
            "/v1/workflows/workflow-1/execution-jobs",
            headers=_headers(),
            json=payload,
        )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"
    assert "highly_sensitive_value" not in response.text
    assert "sql" not in response.text.casefold()


def test_body_encoding_host_and_size_are_rejected_before_parsing() -> None:
    with _client(max_body_bytes=1_024) as client:
        compressed = client.post(
            "/v1/workflows/workflow-1/execution-jobs",
            headers={**_headers(), "Content-Encoding": "gzip"},
            content=b"compressed",
        )
        invalid_host = client.get("/health/live", headers={"Host": "attacker.example"})
        oversized = client.post(
            "/v1/workflows/workflow-1/execution-jobs",
            headers=_headers(),
            content=b"x" * 1_025,
        )
        missing_content_type = client.post(
            "/v1/workflows/workflow-1/execution-jobs",
            headers=_headers(),
            content=b"{}",
        )

    assert compressed.status_code == 415
    assert compressed.json()["code"] == "unsupported_content_encoding"
    assert invalid_host.status_code == 400
    assert invalid_host.json()["code"] == "invalid_host"
    assert oversized.status_code == 413
    assert oversized.json()["code"] == "request_too_large"
    assert missing_content_type.status_code == 415
    assert missing_content_type.json()["code"] == "unsupported_content_type"


def test_duplicate_authorization_and_idempotency_headers_fail_closed() -> None:
    path = "/v1/workflows/workflow-1/execution-jobs"
    body = _body()
    with _client() as client:
        duplicate_authorization = client.post(
            path,
            headers=[
                ("Authorization", "Bearer accepted-local-test-token"),
                ("Authorization", "Bearer accepted-local-test-token"),
                ("Idempotency-Key", IDEMPOTENCY_KEY),
            ],
            json=body,
        )
        duplicate_idempotency = client.post(
            path,
            headers=[
                ("Authorization", "Bearer accepted-local-test-token"),
                ("Idempotency-Key", IDEMPOTENCY_KEY),
                ("Idempotency-Key", IDEMPOTENCY_KEY),
            ],
            json=body,
        )

    assert duplicate_authorization.status_code == 401
    assert duplicate_authorization.json()["code"] == "invalid_bearer_token"
    assert duplicate_idempotency.status_code == 422
    assert duplicate_idempotency.json()["code"] == "execution_job_invalid_request"


def test_inspection_and_explicit_cancellation_return_only_sanitized_job() -> None:
    job_id = _job("workflow-1").id
    auth = {"Authorization": "Bearer accepted-local-test-token"}
    with _client() as client:
        inspected = client.get(f"/v1/execution-jobs/{job_id}", headers=auth)
        cancelled = client.post(
            f"/v1/execution-jobs/{job_id}/cancel",
            headers=auth,
            json={"confirmation": "CANCEL EXECUTION JOB"},
        )

    assert inspected.status_code == cancelled.status_code == 200
    for response in (inspected, cancelled):
        encoded = response.text.casefold()
        for forbidden in ("token", "idempotency", "submitting_actor", "rows", "sql"):
            assert forbidden not in encoded


def test_unexpected_error_does_not_echo_exception_or_enable_debug_page() -> None:
    secret = "sensitive-control-dsn-and-token"
    with _client(submit=_Submit(error=RuntimeError(secret))) as client:
        response = client.post(
            "/v1/workflows/workflow-1/execution-jobs",
            headers=_headers(),
            json=_body(),
        )

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert secret not in response.text
    assert "traceback" not in response.text.casefold()
