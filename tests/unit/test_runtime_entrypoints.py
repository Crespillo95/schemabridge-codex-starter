"""M24 composition and independent-process entrypoint checks."""

from __future__ import annotations

import logging
import signal
from decimal import Decimal
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import cast

import pytest
import uvicorn
from fastapi import FastAPI

import schemabridge.bootstrap as bootstrap_module
import schemabridge.entrypoints.http.app as http_app_module
import schemabridge.entrypoints.http.main as http_main
import schemabridge.entrypoints.worker.main as worker_main
from schemabridge.adapters.control_plane.postgres_active_registry import (
    PostgresActiveRegistryPointerReader,
)
from schemabridge.adapters.control_plane.postgres_identity_bindings import (
    PostgresIdentityBindingResolver,
)
from schemabridge.adapters.control_plane.postgres_jobs import (
    PostgresBackgroundJobStore,
)
from schemabridge.adapters.control_plane.postgres_pool import PostgresControlPool
from schemabridge.adapters.control_plane.threaded_heartbeat import (
    ThreadedLeaseHeartbeatSupervisor,
)
from schemabridge.adapters.semantic_change.postgres_read import (
    PostgresSemanticChangeReadStore,
)
from schemabridge.adapters.storage.identity_resolving import (
    IdentityResolvingWorkflowAccessStore,
)
from schemabridge.adapters.storage.postgres import (
    PostgresWorkflowAccessStore,
    PostgresWorkflowDraftStore,
)
from schemabridge.adapters.workflows.read_only import (
    DisabledWorkflowPublisher,
    ReadOnlyWorkflowInspector,
)
from schemabridge.application.api_workflows import (
    SubmitExecutionJob,
    WorkflowOrchestratorPort,
)
from schemabridge.application.job_worker import (
    RunOneJobWorker,
    WorkerExecutionRouteContext,
    WorkerIterationOutcome,
    WorkerIterationResult,
)
from schemabridge.bootstrap import (
    ApiProcessRuntime,
    WorkerProcessRuntime,
    build_active_registry_pointer_reader,
    build_api_http_services,
    build_api_process_runtime,
    build_job_worker,
    build_read_only_worker_orchestrator,
    build_worker_process_runtime,
    require_current_control_plane_schema,
)
from schemabridge.config import Settings
from schemabridge.domain.background_jobs import JobExecutionTargetRef
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    QueryCostBudget,
    SourceConnectorKind,
    SourceDialect,
    postgres_type_contract_fingerprint,
)

ROOT = Path(__file__).resolve().parents[2]
API_DSN = "postgresql://schemabridge_api:api-secret@control.example.test/control"
WORKER_DSN = "postgresql://schemabridge_worker:worker-secret@control.example.test/control"
LOCAL_TOKEN = "local-api-bearer-token-with-enough-byte-diversity-123"
CURSOR_KEY = "inventory-cursor-signing-key-with-distinct-bytes-456"


class _OneResultWorker:
    def __init__(self, result: WorkerIterationResult) -> None:
        self.result = result
        self.calls = 0

    def execute(self) -> WorkerIterationResult:
        self.calls += 1
        return self.result


class _CrashingWorker:
    def execute(self) -> WorkerIterationResult:
        raise RuntimeError("must-not-appear-in-worker-log")


class _LifecyclePool:
    def __init__(self) -> None:
        self.events: list[str] = []

    def open(self) -> None:
        self.events.append("opened")

    def close(self) -> None:
        self.events.append("closed")


class _InjectedOrchestratorFactory:
    def __call__(
        self,
        workspace_id: str,
        actor_id: str,
        *,
        route_context: WorkerExecutionRouteContext | None = None,
    ) -> WorkflowOrchestratorPort:
        del workspace_id, actor_id, route_context
        raise AssertionError("the injected factory is not invoked during composition")


def test_api_composition_uses_only_api_role_and_read_only_workflow_inspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks: list[str] = []
    monkeypatch.setattr(
        bootstrap_module,
        "require_current_control_plane_schema",
        lambda **kwargs: checks.append(cast(str, kwargs["credential_kind"])),
    )

    services = build_api_http_services(repository_root=ROOT, settings=_api_settings())

    submit = cast(SubmitExecutionJob, services.submit)
    store = cast(PostgresBackgroundJobStore, submit.job_store)
    access = cast(
        PostgresWorkflowAccessStore,
        submit.access_store_factory("workspace-a", "actor-a"),
    )
    inspector = cast(
        ReadOnlyWorkflowInspector,
        submit.orchestrator_factory("workspace-a", "actor-a"),
    )
    draft_store = cast(PostgresWorkflowDraftStore, inspector.store)

    assert checks == ["api"]
    assert store.application_name == "schemabridge-control-api"
    assert access._db.application_name == "schemabridge-control-api"
    assert draft_store._db.application_name == "schemabridge-control-api"
    assert services.semantic_changes is not None
    semantic_store = cast(
        PostgresSemanticChangeReadStore,
        cast(SimpleNamespace, services.semantic_changes.list_reports).store,
    )
    assert semantic_store.application_name == "schemabridge-control-api"
    assert not hasattr(inspector, "execute")
    assert "api-secret" not in repr(services)
    assert LOCAL_TOKEN not in repr(services)


def test_worker_composition_uses_worker_role_and_accepts_a_future_orchestrator_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks: list[str] = []
    injected = _InjectedOrchestratorFactory()
    monkeypatch.setattr(
        bootstrap_module,
        "require_current_control_plane_schema",
        lambda **kwargs: checks.append(cast(str, kwargs["credential_kind"])),
    )

    worker = build_job_worker(
        repository_root=ROOT,
        settings=_worker_settings(),
        orchestrator_factory=injected,
    )
    access = cast(
        PostgresWorkflowAccessStore,
        worker.access_store_factory("workspace-a", "actor-a"),
    )

    assert isinstance(worker, RunOneJobWorker)
    assert checks == ["worker"]
    assert cast(object, worker.orchestrator_factory) is injected
    assert cast(PostgresBackgroundJobStore, worker.job_store).application_name == (
        "schemabridge-control-worker"
    )
    supervisor = cast(ThreadedLeaseHeartbeatSupervisor, worker.heartbeat_supervisor)
    assert supervisor.job_store is worker.job_store
    assert worker.heartbeat_interval.total_seconds() == (
        _worker_settings().worker_heartbeat_seconds
    )
    assert _worker_settings().worker_identity_lineage_mode == "exact-local"
    assert isinstance(access, PostgresWorkflowAccessStore)
    assert access._db.application_name == "schemabridge-control-worker"
    assert "worker-secret" not in repr(worker)
    assert "source-secret" not in repr(worker)


def test_managed_worker_composes_verified_lineage_with_only_worker_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks: list[str] = []
    injected = _InjectedOrchestratorFactory()
    settings = _managed_worker_settings()
    monkeypatch.setattr(
        bootstrap_module,
        "require_current_control_plane_schema",
        lambda **kwargs: checks.append(cast(str, kwargs["credential_kind"])),
    )

    worker = build_job_worker(
        repository_root=ROOT,
        settings=settings,
        orchestrator_factory=injected,
    )
    access = cast(
        IdentityResolvingWorkflowAccessStore,
        worker.access_store_factory("workspace-a", "actor-a"),
    )

    assert checks == ["worker"]
    assert isinstance(access, IdentityResolvingWorkflowAccessStore)
    assert isinstance(access.store, PostgresWorkflowAccessStore)
    assert access.store._db.application_name == "schemabridge-control-worker"
    assert isinstance(access.resolver, PostgresIdentityBindingResolver)
    assert access.resolver.application_name == "schemabridge-control-worker"
    assert access.persisted_job_scope_resolver is access.resolver
    assert access.persisted_job_actor_id == "actor-a"
    assert not hasattr(access.resolver, "audit_signing_keys")
    assert not hasattr(access.resolver, "complete_rotation")
    assert settings.auth_mode == "local-demo"
    assert settings.oidc_issuer is None
    assert settings.pseudonymization_key is None
    assert "worker-secret" not in repr(worker)


def test_worker_active_pointer_reader_has_no_write_or_signing_capability() -> None:
    reader = cast(
        PostgresActiveRegistryPointerReader,
        build_active_registry_pointer_reader(
            credential_kind="worker",
            settings=_worker_settings(),
        ),
    )

    assert reader.application_name == "schemabridge-control-worker"
    assert not hasattr(reader, "commit_transition")
    assert not hasattr(reader, "audit_signing_keys")
    assert "worker-secret" not in repr(reader)


def test_worker_orchestrator_has_no_llm_catalog_or_publication_write_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = QueryCostBudget(
        explain_timeout_ms=1_000,
        max_response_bytes=65_536,
        max_total_cost=Decimal("1000"),
        max_estimated_rows=10_000,
        max_plan_nodes=100,
        max_plan_depth=10,
        max_plan_width=1_024,
    )
    target = GovernedExecutionTarget(
        workspace_id="workspace-a",
        connection_id=CatalogConnectionId("warehouse-primary"),
        connector_kind=SourceConnectorKind.POSTGRESQL,
        dialect=SourceDialect.POSTGRESQL,
        route_revision=7,
        route_fingerprint="a" * 64,
        expected_reader="schemabridge_reader",
        source_identity_fingerprint="c" * 64,
        catalog_identity_fingerprint="d" * 64,
        type_contract_fingerprint=postgres_type_contract_fingerprint(),
        cost_budget=budget,
        cost_budget_fingerprint=budget.fingerprint,
    )

    class _Registry:
        def load(self) -> object:
            return SimpleNamespace(
                registry=SimpleNamespace(
                    join_contracts=SimpleNamespace(contracts=()),
                )
            )

    class _DraftStore:
        def load(self, workflow_id: str) -> object:
            assert workflow_id == "workflow-worker-route"
            return SimpleNamespace(
                resolved_plan=SimpleNamespace(execution_target=target),
            )

    registry = _Registry()
    prepare = object()
    connector = object()
    intent = object()
    draft_store = _DraftStore()
    monkeypatch.setattr(
        bootstrap_module,
        "build_semantic_registry",
        lambda **_kwargs: registry,
    )
    monkeypatch.setattr(
        bootstrap_module,
        "build_governed_request_preparer",
        lambda **_kwargs: prepare,
    )
    monkeypatch.setattr(
        bootstrap_module,
        "build_natural_language_intent_resolver",
        lambda *_args, **_kwargs: intent,
    )
    monkeypatch.setattr(
        bootstrap_module,
        "_build_postgres_workflow_draft_store",
        lambda *_args, **_kwargs: draft_store,
    )
    monkeypatch.setattr(
        bootstrap_module,
        "_build_connector_secret_resolver",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        "schemabridge.adapters.connectors.routed_postgres.RoutedPostgresQueryConnector",
        lambda *_args, **_kwargs: connector,
    )
    route_context = WorkerExecutionRouteContext(
        job_id="job-worker-route",
        workflow_id="workflow-worker-route",
        job_workspace_id="workspace-a",
        connector_workspace_id="workspace-a",
        worker_id="worker-route-1",
        lease_capability="lease-capability-" + ("x" * 40),
        fencing_token=1,
        execution_target=JobExecutionTargetRef.from_target(target),
        connector_contract_version=3,
    )

    orchestrator = build_read_only_worker_orchestrator(
        "workspace-a",
        "actor-a",
        route_context=route_context,
        repository_root=ROOT,
        settings=_worker_settings(),
    )

    assert cast(object, orchestrator.store) is draft_store
    assert orchestrator.intent is intent
    assert orchestrator.prepare is prepare
    assert orchestrator.execute.executor is connector
    assert cast(object, orchestrator.execute.rejection_reporter) is connector
    assert orchestrator.catalog.source_label == "disabled:worker-execution-only"
    assert isinstance(orchestrator.publisher, DisabledWorkflowPublisher)
    assert orchestrator.recipe_assessor is None


def test_worker_once_is_serial_and_returns_after_one_idle_poll() -> None:
    worker = _OneResultWorker(WorkerIterationResult(outcome=WorkerIterationOutcome.IDLE))

    status = worker_main.run_worker(
        worker,
        poll_interval_seconds=0.05,
        once=True,
    )

    assert status == 0
    assert worker.calls == 1


def test_worker_log_omits_unexpected_exception_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger=worker_main.__name__)

    status = worker_main.run_worker(
        _CrashingWorker(),
        poll_interval_seconds=0.05,
        once=True,
    )

    assert status == 1
    assert "unexpected_worker_error" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "must-not-appear-in-worker-log" not in caplog.text


def test_worker_signal_handler_requests_graceful_stop_and_restores_previous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: list[tuple[int, object]] = []
    monkeypatch.setattr(signal, "getsignal", lambda _signum: signal.SIG_DFL)
    monkeypatch.setattr(
        signal,
        "signal",
        lambda signum, handler: installed.append((signum, handler)),
    )
    stopping = Event()

    previous = worker_main._install_signal_handlers(stopping)
    handler = next(handler for signum, handler in installed if signum == signal.SIGTERM)
    assert callable(handler)
    handler(signal.SIGTERM, None)
    worker_main._restore_signal_handlers(previous)

    assert stopping.is_set()
    assert installed[-2:] == [
        (signal.SIGINT, signal.SIG_DFL),
        (signal.SIGTERM, signal.SIG_DFL),
    ]


def test_http_entrypoint_composes_one_bounded_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    services = object()
    app = FastAPI()
    settings = _api_settings()
    pool = _LifecyclePool()
    monkeypatch.setattr(bootstrap_module, "build_api_http_services", lambda **_kwargs: services)
    monkeypatch.setattr(
        bootstrap_module,
        "build_control_plane_pool",
        lambda **_kwargs: cast(PostgresControlPool, pool),
    )

    def create(
        supplied: object,
        *,
        max_body_bytes: int,
        max_concurrency: int,
        allowed_hosts: tuple[str, ...],
        docs_enabled: bool,
        lifecycle_resources: tuple[object, ...],
    ) -> FastAPI:
        captured.update(
            services=supplied,
            max_body_bytes=max_body_bytes,
            max_concurrency=max_concurrency,
            allowed_hosts=allowed_hosts,
            docs_enabled=docs_enabled,
            lifecycle_resources=lifecycle_resources,
        )
        return app

    monkeypatch.setattr(http_app_module, "create_http_app", create)

    runtime = build_api_process_runtime(settings=settings)
    result = http_main.build_http_application(runtime)

    assert result is app
    assert runtime.application is app
    assert runtime.log_level == settings.log_level
    assert runtime.bind_host == settings.api_bind_host
    assert runtime.port == settings.api_port
    assert runtime.graceful_shutdown_seconds == settings.api_graceful_shutdown_seconds
    assert captured == {
        "services": services,
        "max_body_bytes": settings.api_max_request_bytes,
        "max_concurrency": settings.api_limit_concurrency,
        "allowed_hosts": settings.api_allowed_hosts,
        "docs_enabled": False,
        "lifecycle_resources": (pool,),
    }


def test_http_main_configures_one_gracefully_stoppable_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _api_settings()
    app = FastAPI()
    runtime = ApiProcessRuntime(
        application=app,
        log_level=settings.log_level,
        bind_host=settings.api_bind_host,
        port=settings.api_port,
        graceful_shutdown_seconds=settings.api_graceful_shutdown_seconds,
    )
    calls: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(http_main, "build_api_process_runtime", lambda: runtime)
    monkeypatch.setattr(
        uvicorn,
        "run",
        lambda supplied, **kwargs: calls.append((supplied, kwargs)),
    )

    http_main.main()

    assert calls == [
        (
            app,
            {
                "host": settings.api_bind_host,
                "port": settings.api_port,
                "workers": 1,
                "timeout_graceful_shutdown": settings.api_graceful_shutdown_seconds,
                "access_log": False,
                "proxy_headers": False,
                "server_header": False,
                "date_header": False,
            },
        )
    ]


def test_worker_command_restores_signal_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _OneResultWorker(WorkerIterationResult(outcome=WorkerIterationOutcome.IDLE))
    pool = _LifecyclePool()
    runtime = WorkerProcessRuntime(
        worker=cast(RunOneJobWorker, worker),
        log_level="INFO",
        poll_interval_seconds=0.05,
        control_pool=cast(PostgresControlPool, pool),
    )
    restored: list[dict[int, worker_main._SignalHandler]] = []
    monkeypatch.setattr(worker_main, "_install_signal_handlers", lambda _event: {})
    monkeypatch.setattr(
        worker_main,
        "_restore_signal_handlers",
        lambda previous: restored.append(previous),
    )

    status = worker_main.command(["--once"], runtime=runtime)

    assert status == 0
    assert worker.calls == 1
    assert restored == [{}]
    assert pool.events == ["opened", "closed"]


def test_worker_readiness_probe_checks_exact_schema_without_composing_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _worker_settings()
    pool = _LifecyclePool()
    checks: list[dict[str, object]] = []
    monkeypatch.setattr(
        bootstrap_module,
        "require_current_control_plane_schema",
        lambda **kwargs: checks.append(kwargs),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "build_job_worker",
        lambda **_kwargs: pytest.fail("readiness must not compose or poll a worker"),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "build_control_plane_pool",
        lambda **_kwargs: cast(PostgresControlPool, pool),
    )

    runtime = build_worker_process_runtime(
        readiness_probe=True,
        settings=settings,
    )
    status = worker_main.command(["--probe-ready"], runtime=runtime)

    assert status == 0
    assert runtime.worker is None
    assert pool.events == ["opened", "closed"]
    assert checks == [
        {
            "credential_kind": "worker",
            "repository_root": None,
            "settings": settings,
        }
    ]


def test_worker_readiness_probe_failure_does_not_start_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_schema_check(**_kwargs: object) -> None:
        raise RuntimeError("schema behind")

    monkeypatch.setattr(bootstrap_module, "require_current_control_plane_schema", fail_schema_check)
    monkeypatch.setattr(
        bootstrap_module,
        "build_job_worker",
        lambda **_kwargs: pytest.fail("failed readiness must not compose a worker"),
    )

    with pytest.raises(RuntimeError, match="schema behind"):
        build_worker_process_runtime(
            readiness_probe=True,
            settings=_worker_settings(),
        )


def test_schema_preflight_never_invokes_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = object()

    class _SchemaProbe:
        def require_current(self) -> object:
            return expected

        def migrate(self) -> object:
            raise AssertionError("service schema preflight must never migrate")

    monkeypatch.setattr(
        bootstrap_module,
        "build_control_plane_migrator",
        lambda **_kwargs: _SchemaProbe(),
    )

    result = require_current_control_plane_schema(
        credential_kind="worker",
        repository_root=ROOT,
        settings=_worker_settings(),
    )

    assert result is expected


def _api_settings() -> Settings:
    return Settings(
        _env_file=None,
        OPENAI_API_KEY=None,
        DATAHUB_GMS_TOKEN=None,
        SCHEMABRIDGE_COMPONENT="api",
        SCHEMABRIDGE_CONTROL_PLANE_MODE="postgres",
        SCHEMABRIDGE_CONTROL_API_DATABASE_URL=API_DSN,
        SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN=LOCAL_TOKEN,
        SCHEMABRIDGE_INVENTORY_CURSOR_SIGNING_KEY=CURSOR_KEY,
    )


def _worker_settings() -> Settings:
    return Settings(
        _env_file=None,
        OPENAI_API_KEY=None,
        DATAHUB_GMS_TOKEN=None,
        SCHEMABRIDGE_COMPONENT="worker",
        SCHEMABRIDGE_CONTROL_PLANE_MODE="postgres",
        SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL=WORKER_DSN,
        SCHEMABRIDGE_REGISTRY_MODE="live",
        SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION="active",
        SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS=True,
        SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY=ROOT / ".local/test-connectors",
    )


def _managed_worker_settings() -> Settings:
    return Settings(
        _env_file=None,
        OPENAI_API_KEY=None,
        DATAHUB_GMS_TOKEN=None,
        SCHEMABRIDGE_ENVIRONMENT="production",
        SCHEMABRIDGE_COMPONENT="worker",
        SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL=(f"{WORKER_DSN}?sslmode=verify-full"),
        SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY=ROOT / ".local/test-connectors",
        SCHEMABRIDGE_WORKER_IDENTITY_LINEAGE_MODE="verified-oidc",
    )
