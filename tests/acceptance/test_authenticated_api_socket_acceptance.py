"""Real-socket M24 acceptance over an isolated PostgreSQL control database."""

from __future__ import annotations

import logging
import os
import secrets
import socket
import time
from base64 import urlsafe_b64encode
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import psycopg
import pytest
import uvicorn
from fastapi import FastAPI
from psycopg import sql
from pydantic import SecretStr
from scripts import synthetic_oidc_provider
from tests.integration.connector_target_support import (
    ensure_catalog_connector_target,
    ensure_compatible_catalog_generation,
)

from schemabridge.adapters.catalog.postgres_inventory import (
    PostgresCatalogConnectionStore,
)
from schemabridge.adapters.connectors.postgres_routing import (
    PostgresExecutionTargetResolver,
)
from schemabridge.adapters.control_plane.postgres_jobs import (
    PostgresBackgroundJobStore,
)
from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.adapters.control_plane.threaded_heartbeat import (
    ThreadedLeaseHeartbeatSupervisor,
)
from schemabridge.adapters.datahub.recorded import RecordedCatalogAdapter
from schemabridge.adapters.identity.local import LocalDemoPrincipalFactory
from schemabridge.adapters.identity.local_bearer import LocalBearerAuthenticator
from schemabridge.adapters.identity.oidc import OidcPrincipalMapper
from schemabridge.adapters.identity.oidc_bearer import OidcBearerAuthenticator
from schemabridge.adapters.language.fake import FakeIntentParser
from schemabridge.adapters.planning.recorded import RecordedSemanticPlanningContext
from schemabridge.adapters.postgres.preview import PsycopgQueryPreview
from schemabridge.adapters.postgres.rejections import PsycopgRejectedSourceReporter
from schemabridge.adapters.requests.recorded_context import RecordedRequestContextAdapter
from schemabridge.adapters.sql.compiler import PostgresQueryCompiler
from schemabridge.adapters.sql.guard import SqlGlotPolicyGuard
from schemabridge.adapters.storage.postgres import (
    PostgresWorkflowAccessStore,
    PostgresWorkflowDraftStore,
)
from schemabridge.adapters.storage.publication_audit import (
    SqlitePublicationAuditStore,
)
from schemabridge.adapters.workflows.fake import SqliteFakeWorkflowPublisher
from schemabridge.adapters.workflows.read_only import ReadOnlyWorkflowInspector
from schemabridge.adapters.workflows.system import SystemWorkflowClock
from schemabridge.application.api_workflows import (
    CancelExecutionJob,
    InspectExecutionJob,
    SubmitExecutionJob,
    WorkflowOrchestratorPort,
)
from schemabridge.application.authentication import AuthenticationBoundaryError
from schemabridge.application.authorization import DenyByDefaultAuthorizationPolicy
from schemabridge.application.governed_execution import (
    ExecuteGovernedRequest,
    PlanSemanticRequest,
    PrepareGovernedRequest,
)
from schemabridge.application.intent_resolution import ResolveNaturalLanguageIntent
from schemabridge.application.job_worker import (
    RunOneJobWorker,
    WorkerExecutionRouteContext,
    WorkerIterationOutcome,
)
from schemabridge.application.ports.authentication import BearerAuthenticationPort
from schemabridge.application.workflow_orchestration import AgentWorkflowOrchestrator
from schemabridge.domain.background_jobs import JobExecutionTargetRef
from schemabridge.domain.catalog_inventory import (
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogConnectionRegistration,
)
from schemabridge.domain.connectors import GovernedExecutionTarget
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)
from schemabridge.domain.intents import IntentAlternativeId, UserLanguage
from schemabridge.domain.resolution import ResolutionLimits
from schemabridge.domain.workflows import (
    AgentWorkflowDraft,
    ExecutionWorkflowDecision,
    IntentWorkflowDecision,
    StartWorkflowCommand,
    WorkflowCheckpoint,
    WorkflowCheckpointKind,
    WorkflowDecisionAction,
    WorkflowDecisionKind,
    WorkflowDecisionRecord,
    WorkflowExecutionRecord,
    WorkflowOperation,
    WorkflowPublicationProposal,
    WorkflowStage,
)
from schemabridge.entrypoints.http.app import ApiHttpServices, create_http_app

pytestmark = [pytest.mark.acceptance, pytest.mark.integration]

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations/control_plane"
ADMIN_DSN = "postgresql://postgres:local-only-not-a-secret@127.0.0.1:55434/postgres"
READER_DSN = os.environ.get(
    "SCHEMABRIDGE_TEST_DATABASE_URL",
    "postgresql://schemabridge_reader:schemabridge_reader@127.0.0.1:55433/schemabridge",
)
PLAN_FINGERPRINT = "a" * 64
QUERY_FINGERPRINT = "b" * 64
PREVIEW_FINGERPRINT = "c" * 64
REQUEST_FINGERPRINT = "d" * 64
EXECUTION_CONFIRMATION = "EXECUTE GOVERNED PREVIEW"
CANCELLATION_CONFIRMATION = "CANCEL EXECUTION JOB"
OIDC_CLIENT_ID = "schemabridge-api-acceptance"
# These socket journeys test authorization and terminal states, not lease expiry.
# Match the operated worker default so coverage/CI overhead cannot consume the fixture lease.
ACCEPTANCE_WORKER_LEASE_DURATION = timedelta(seconds=120)
NORTH_STAR_REQUEST = (
    "Agrupa por fecha de registro todos los clientes que sean segundo titular de una cuenta."
)


@dataclass(frozen=True, slots=True)
class _DatabaseUrls:
    database: str
    migrator: str = field(repr=False)
    api: str = field(repr=False)
    worker: str = field(repr=False)
    runtime: str = field(repr=False)
    catalog: str = field(repr=False)


@pytest.fixture
def isolated_control_database() -> Iterator[_DatabaseUrls]:
    """Create and migrate one exact disposable schema-v4 control database."""

    database = f"schemabridge_socket_{uuid4().hex[:16]}"
    admin_dsn = os.environ.get(
        "SCHEMABRIDGE_TEST_CONTROL_ADMIN_DATABASE_URL",
        ADMIN_DSN,
    )
    urls = _DatabaseUrls(
        database=database,
        migrator=_role_dsn("schemabridge_migrator", database),
        api=_role_dsn("schemabridge_api", database),
        worker=_role_dsn("schemabridge_worker", database),
        runtime=_role_dsn("schemabridge_runtime", database),
        catalog=_role_dsn("schemabridge_catalog", database),
    )
    database_created = False
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("CREATE DATABASE {} OWNER schemabridge_migrator").format(
                    sql.Identifier(database)
                )
            )
            database_created = True
            connection.execute(
                sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database))
            )
            connection.execute(
                sql.SQL(
                    """
                    GRANT CONNECT ON DATABASE {} TO
                        schemabridge_migrator,
                        schemabridge_api,
                        schemabridge_worker,
                        schemabridge_runtime,
                        schemabridge_catalog
                    """
                ).format(sql.Identifier(database))
            )

        migrated = PostgresControlPlaneMigrator(
            urls.migrator,
            MIGRATIONS,
        ).migrate()
        assert migrated.inspection.current_version == 9
        assert migrated.inspection.is_current
        yield urls
    finally:
        if database_created:
            with psycopg.connect(admin_dsn, autocommit=True) as connection:
                connection.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(database)
                    )
                )


def _role_dsn(role: str, database: str) -> str:
    return f"postgresql://{role}:{role}@127.0.0.1:55434/{database}"


def _seed_capacity_policy(
    urls: _DatabaseUrls,
    *,
    workspace_id: str,
) -> None:
    """Model the required operator provisioning before authenticated API use."""

    now = datetime.now(UTC)
    with psycopg.connect(urls.migrator) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.tenant_capacity_policies (
                workspace_id,
                connection_limit,
                asset_limit,
                field_limit,
                api_requests_per_minute,
                nonterminal_job_limit,
                version,
                updated_by,
                created_at,
                updated_at
            ) VALUES (%s, 100, 1000000, 10000000, 10000, 1000, 1,
                      'test_platform_admin', %s, %s)
            """,
            (workspace_id, now, now),
        )


def _execution_target(
    urls: _DatabaseUrls,
    *,
    workspace_id: str,
) -> GovernedExecutionTarget:
    connection_id = CatalogConnectionId("connection_socket_acceptance")
    PostgresCatalogConnectionStore(
        urls.api,
        application_name="schemabridge-control-api",
    ).register(
        CatalogConnectionRegistration(
            workspace_id=workspace_id,
            connection_id=connection_id,
            display_name="Socket acceptance source",
            kind=CatalogConnectionKind.SYNTHETIC,
            environment="TEST",
            catalog_scope="socket-acceptance",
            requested_by="test_platform_admin",
            requested_at=datetime.now(UTC),
            idempotency_digest=sha256(f"register-target:{workspace_id}".encode()).hexdigest(),
        )
    )
    facts = ensure_catalog_connector_target(
        urls.migrator,
        workspace_id=workspace_id,
        connection_id=connection_id,
    )
    ensure_compatible_catalog_generation(
        urls.migrator,
        urls.api,
        urls.catalog,
        workspace_id=workspace_id,
        connection_id=connection_id,
        target=facts,
    )
    target = PostgresExecutionTargetResolver(urls.runtime).resolve_current(
        workspace_id=workspace_id,
        connection_id=connection_id,
    )
    assert target.route_revision == facts.route_revision
    assert target.fingerprint == facts.target_fingerprint
    return target


class _BearerRouter:
    """Route ephemeral test bearers through independent production verifiers."""

    __slots__ = ("_authenticators",)

    def __init__(
        self,
        authenticators: tuple[BearerAuthenticationPort, ...],
    ) -> None:
        self._authenticators = authenticators

    def authenticate(
        self,
        bearer_token: str,
        now: datetime,
    ) -> AuthenticatedPrincipal:
        for authenticator in self._authenticators:
            try:
                return authenticator.authenticate(bearer_token, now)
            except AuthenticationBoundaryError as error:
                if error.code != "invalid_bearer_token":
                    raise
        raise AuthenticationBoundaryError("invalid_bearer_token")

    def __repr__(self) -> str:
        return f"_BearerRouter(configured={len(self._authenticators)})"


@dataclass(frozen=True, slots=True)
class _OidcRuntime:
    issuer: str
    jwks_url: str
    bearer_token: str = field(repr=False)
    transient_values: tuple[str, ...] = field(repr=False)


@contextmanager
def _synthetic_oidc_identity(
    *,
    subject: str,
    tenant: str,
    groups: tuple[str, ...],
) -> Iterator[_OidcRuntime]:
    """Run the production synthetic provider and mint one token over real HTTP."""

    client_secret = secrets.token_urlsafe(48)
    server = synthetic_oidc_provider._LoopbackHttpServer(  # type: ignore[attr-defined]
        ("127.0.0.1", 0),
        synthetic_oidc_provider._OidcRequestHandler,  # type: ignore[attr-defined]
    )
    thread: Thread | None = None
    try:
        port = int(server.server_address[1])
        config = synthetic_oidc_provider.ProviderConfig(
            host="127.0.0.1",
            port=port,
            client_id=OIDC_CLIENT_ID,
            client_secret=client_secret,
            redirect_uri="http://127.0.0.1:65001/oauth2callback",
            identity=synthetic_oidc_provider.SyntheticIdentity(
                subject=subject,
                tenant=tenant,
                groups=groups,
            ),
        )
        server.oidc_provider = synthetic_oidc_provider.SyntheticOidcProvider(config)
        thread = Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="schemabridge-synthetic-oidc-acceptance",
            daemon=True,
        )
        thread.start()
        issuer = config.issuer
        with httpx.Client(
            base_url=issuer,
            timeout=5,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            deadline = time.monotonic() + 5
            while True:
                try:
                    health = client.get("/health")
                except httpx.TransportError:
                    health = None
                if health is not None and health.status_code == 200:
                    break
                if not thread.is_alive() or time.monotonic() >= deadline:
                    raise RuntimeError("the synthetic OIDC acceptance server did not start")
                time.sleep(0.01)
            bearer_token, transient = _mint_oidc_bearer(client, config)
            yield _OidcRuntime(
                issuer=issuer,
                jwks_url=f"{issuer}/jwks",
                bearer_token=bearer_token,
                transient_values=(client_secret, *transient),
            )
    finally:
        if thread is not None:
            server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=3)
            assert not thread.is_alive()


def _mint_oidc_bearer(
    client: httpx.Client,
    config: synthetic_oidc_provider.ProviderConfig,
) -> tuple[str, tuple[str, ...]]:
    verifier = secrets.token_urlsafe(48)
    challenge = urlsafe_b64encode(sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    authorization = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": config.redirect_uri,
            "scope": "openid profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    if authorization.status_code != 302:
        raise AssertionError("synthetic OIDC authorization failed")
    location = authorization.headers.get("location")
    if location is None:
        raise AssertionError("synthetic OIDC authorization omitted its redirect")
    query = parse_qs(urlsplit(location).query)
    codes = query.get("code", ())
    states = query.get("state", ())
    if len(codes) != 1 or states != [state]:
        raise AssertionError("synthetic OIDC authorization binding failed")
    code = codes[0]
    token_response = client.post(
        "/token",
        auth=(config.client_id, config.client_secret),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": config.redirect_uri,
            "code_verifier": verifier,
        },
    )
    if token_response.status_code != 200:
        raise AssertionError("synthetic OIDC token exchange failed")
    payload = token_response.json()
    token = payload.get("access_token") if isinstance(payload, dict) else None
    id_token = payload.get("id_token") if isinstance(payload, dict) else None
    if (
        not isinstance(token, str)
        or not token
        or not isinstance(id_token, str)
        or not id_token
        or payload.get("token_type") != "Bearer"
    ):
        raise AssertionError("synthetic OIDC token response was invalid")
    return token, (id_token, verifier, state, nonce, code)


def _oidc_authenticator(
    runtime: _OidcRuntime,
    *,
    pseudonymization_key: bytes,
) -> OidcBearerAuthenticator:
    return OidcBearerAuthenticator(
        mapper=OidcPrincipalMapper(
            expected_issuer=runtime.issuer,
            expected_audience=OIDC_CLIENT_ID,
            allowed_group_roles={
                "analysts": frozenset({IdentityRole.ANALYST}),
                "auditors": frozenset({IdentityRole.AUDITOR}),
            },
            allowed_tenants=frozenset({"socket-tenant-a", "socket-tenant-b"}),
            pseudonymization_key=pseudonymization_key,
            max_session_age=timedelta(minutes=15),
        ),
        jwks_url=runtime.jwks_url,
        algorithms=("RS256",),
        expected_authorized_party=OIDC_CLIENT_ID,
        timeout_seconds=2,
        cache_ttl_seconds=60,
        allow_insecure_loopback=True,
    )


@dataclass(frozen=True, slots=True)
class _Readiness:
    dsn: str = field(repr=False)

    def require_ready(self) -> None:
        PostgresControlPlaneMigrator(
            self.dsn,
            MIGRATIONS,
            application_name="schemabridge-control-api",
        ).require_current()


class _ExecutionTargetInspector:
    """Expose one exact v9 target only on the immutable submission view."""

    def __init__(
        self,
        delegate: ReadOnlyWorkflowInspector,
        target: GovernedExecutionTarget,
    ) -> None:
        self._delegate = delegate
        self._target = target

    def inspect(self, workflow_id: str) -> AgentWorkflowDraft:
        draft = self._delegate.inspect(workflow_id)
        resolved = (
            SimpleNamespace(execution_target=self._target)
            if draft.resolved_plan is None
            else draft.resolved_plan.model_copy(update={"execution_target": self._target})
        )
        return draft.model_copy(update={"resolved_plan": resolved})


class _CapturingOrchestrator:
    """Observe the real orchestrator result before the durable store removes rows."""

    def __init__(self, delegate: AgentWorkflowOrchestrator) -> None:
        self._delegate = delegate
        self.completed: AgentWorkflowDraft | None = None
        self.decide_calls = 0

    def inspect(self, workflow_id: str) -> AgentWorkflowDraft:
        return self._delegate.inspect(workflow_id)

    def decide_execution(
        self,
        workflow_id: str,
        decision: ExecutionWorkflowDecision,
        *,
        should_continue: Callable[[], bool] | None = None,
    ) -> AgentWorkflowDraft:
        self.decide_calls += 1
        self.completed = self._delegate.decide_execution(
            workflow_id,
            decision,
            should_continue=should_continue,
        )
        return self.completed

    def recover_interrupted(
        self,
        workflow_id: str,
        *,
        expected_operation: WorkflowOperation,
    ) -> AgentWorkflowDraft:
        return self._delegate.recover_interrupted(
            workflow_id,
            expected_operation=expected_operation,
        )


class _InspectCrashOrchestrator:
    """Inject one uncertainty before workflow/source I/O to prove dead-lettering."""

    def inspect(self, workflow_id: str) -> AgentWorkflowDraft:
        del workflow_id
        raise RuntimeError("synthetic acceptance uncertainty")

    def decide_execution(
        self,
        workflow_id: str,
        decision: ExecutionWorkflowDecision,
        *,
        should_continue: Callable[[], bool] | None = None,
    ) -> AgentWorkflowDraft:
        del workflow_id, decision, should_continue
        raise AssertionError("execution must not start after an uncertain inspection")

    def recover_interrupted(
        self,
        workflow_id: str,
        *,
        expected_operation: WorkflowOperation,
    ) -> AgentWorkflowDraft:
        del workflow_id, expected_operation
        raise AssertionError("recovery must not start after an uncertain inspection")


class _SafeCompletedOrchestrator:
    """Inject a deterministic completed workflow without source or external writes."""

    def __init__(self, approval: AgentWorkflowDraft) -> None:
        self._current = approval
        self.decide_calls = 0

    def inspect(self, workflow_id: str) -> AgentWorkflowDraft:
        assert workflow_id == self._current.id
        return self._current

    def decide_execution(
        self,
        workflow_id: str,
        decision: ExecutionWorkflowDecision,
        *,
        should_continue: Callable[[], bool] | None = None,
    ) -> AgentWorkflowDraft:
        assert workflow_id == self._current.id
        assert decision.plan_fingerprint == PLAN_FINGERPRINT
        assert decision.action is WorkflowDecisionAction.APPROVE
        assert should_continue is None or should_continue()
        self.decide_calls += 1
        completed_at = datetime.now(UTC)
        execution = WorkflowExecutionRecord(
            plan_fingerprint=PLAN_FINGERPRINT,
            query_fingerprint=QUERY_FINGERPRINT,
            columns=("registration_date", "customer_count"),
            rows=(),
            row_count=3,
            database_user="synthetic_acceptance_reader",
            transaction_read_only=True,
            statement_timeout_ms=5_000,
            truncated=False,
            preview_fingerprint=PREVIEW_FINGERPRINT,
            rejection_codes=("null_join_key", "non_finite_identifier"),
            rejected_count=25_001,
            rejection_truncated=True,
            rejection_complete=True,
        )
        proposal = WorkflowPublicationProposal.create(
            workflow_id=workflow_id,
            plan_fingerprint=PLAN_FINGERPRINT,
            execution_fingerprint=PREVIEW_FINGERPRINT,
            request_fingerprint=REQUEST_FINGERPRINT,
        )
        approval = WorkflowDecisionRecord(
            actor=decision.actor,
            kind=WorkflowDecisionKind.EXECUTION,
            action=WorkflowDecisionAction.APPROVE,
            decided_at=completed_at,
            bound_fingerprint=PLAN_FINGERPRINT,
        )
        self._current = self._current.model_copy(
            update={
                "revision": self._current.revision + 4,
                "stage": WorkflowStage.PUBLICATION_PROPOSED,
                "query_fingerprint": QUERY_FINGERPRINT,
                "execution": execution,
                "publication_proposal": proposal,
                "checkpoint": WorkflowCheckpoint(
                    kind=WorkflowCheckpointKind.PUBLICATION_DECISION,
                    fingerprint=proposal.fingerprint,
                    reason="Publication remains a separate explicit decision.",
                ),
                "decisions": (*self._current.decisions, approval),
                "updated_at": completed_at,
            }
        )
        return self._current

    def recover_interrupted(
        self,
        workflow_id: str,
        *,
        expected_operation: WorkflowOperation,
    ) -> AgentWorkflowDraft:
        del workflow_id, expected_operation
        raise AssertionError("the acceptance workflow has no interrupted external operation")


@contextmanager
def _real_socket_client(
    app: FastAPI,
    *,
    log_level: str = "critical",
) -> Iterator[httpx.Client]:
    """Serve one ASGI app through an actual loopback TCP socket."""

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_config=None,
            log_level=log_level,
            access_log=False,
            lifespan="off",
            server_header=False,
            date_header=False,
        )
    )
    thread = Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name="schemabridge-api-acceptance",
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2)
        listener.close()
        raise RuntimeError("the real-socket acceptance server did not start")

    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            timeout=5,
            trust_env=False,
        ) as client:
            yield client
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=2)
        with suppress(OSError):
            listener.close()
        assert not thread.is_alive()


def _real_orchestrator(
    store: PostgresWorkflowDraftStore,
    *,
    auxiliary_database: Path,
) -> AgentWorkflowOrchestrator:
    logical = ROOT / "demo/ground_truth/approved_logical_context.yml"
    prepare = PrepareGovernedRequest(
        planner=PlanSemanticRequest(
            RecordedSemanticPlanningContext(
                logical,
                ROOT / "demo/ground_truth/planning_mappings.yml",
                ROOT / "demo/ground_truth/join_contracts.yml",
            ),
            ResolutionLimits(),
        ),
        compiler=PostgresQueryCompiler(),
        guard=SqlGlotPolicyGuard(),
    )
    return AgentWorkflowOrchestrator(
        store=store,
        clock=SystemWorkflowClock(),
        catalog=RecordedCatalogAdapter(ROOT / "demo/datahub/catalog_snapshot.json"),
        intent=ResolveNaturalLanguageIntent(
            parser=FakeIntentParser(),
            context=RecordedRequestContextAdapter(logical),
            adapter_label="fake:typed-intent-only",
        ),
        prepare=prepare,
        execute=ExecuteGovernedRequest(
            prepare=prepare,
            executor=PsycopgQueryPreview(READER_DSN),
            rejection_reporter=PsycopgRejectedSourceReporter(
                READER_DSN,
                frozenset(
                    {
                        "crm.customers.customer_id",
                        "bank.account_holders.gf_customer_id",
                    }
                ),
            ),
        ),
        publisher=SqliteFakeWorkflowPublisher(auxiliary_database),
        audit_store=SqlitePublicationAuditStore(auxiliary_database),
    )


def _seed_real_workflow(
    *,
    urls: _DatabaseUrls,
    owner: AuthenticatedPrincipal,
    workflow_id: str,
    auxiliary_database: Path,
) -> AgentWorkflowDraft:
    orchestrator = _real_orchestrator(
        PostgresWorkflowDraftStore(
            urls.migrator,
            workspace_id=owner.workspace_id,
            owner_actor_id=owner.actor_id,
            application_name="schemabridge-control-migrator",
        ),
        auxiliary_database=auxiliary_database,
    )
    paused = orchestrator.start(
        StartWorkflowCommand(
            id=workflow_id,
            text=NORTH_STAR_REQUEST,
            language=UserLanguage.SPANISH,
            datasets=(
                PhysicalDatasetRef("crm.customers"),
                PhysicalDatasetRef("bank.account_holders"),
            ),
        )
    )
    if paused.intent is None:
        raise AssertionError("the real acceptance intent did not pause for confirmation")
    validated = orchestrator.decide_intent(
        paused.id,
        IntentWorkflowDecision(
            actor=owner.actor_id,
            interpretation_fingerprint=paused.intent.interpretation_fingerprint,
            selected_alternative=IntentAlternativeId.COUNT_DISTINCT_CUSTOMERS,
        ),
    )
    if validated.stage is not WorkflowStage.DECISION_REQUIRED or validated.plan_fingerprint is None:
        raise AssertionError("the real acceptance workflow was not execution-ready")
    return validated


def _approval_draft(workflow_id: str) -> AgentWorkflowDraft:
    now = datetime.now(UTC)
    return AgentWorkflowDraft(
        id=workflow_id,
        revision=7,
        stage=WorkflowStage.DECISION_REQUIRED,
        text="Agrupa clientes por fecha de registro.",
        language="es",
        requested_datasets=(PhysicalDatasetRef("crm.customers"),),
        plan_fingerprint=PLAN_FINGERPRINT,
        checkpoint=WorkflowCheckpoint(
            kind=WorkflowCheckpointKind.EXECUTION_APPROVAL,
            fingerprint=PLAN_FINGERPRINT,
            reason="Approval is required before governed preview execution.",
        ),
        created_at=now - timedelta(minutes=2),
        updated_at=now - timedelta(minutes=1),
    )


def _local_authenticator(
    token: str,
    factory: LocalDemoPrincipalFactory,
) -> LocalBearerAuthenticator:
    return LocalBearerAuthenticator(
        configured_token=SecretStr(token),
        principal_factory=factory,
        runtime_profile="development",
    )


def _authorization(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _submission_headers(token: str, idempotency_key: str) -> dict[str, str]:
    return {
        **_authorization(token),
        "Idempotency-Key": idempotency_key,
    }


def _problem_without_request_id(response: httpx.Response) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return {key: value for key, value in payload.items() if key != "request_id"}


def _require_sensitive_value_absent(
    candidate: str,
    *,
    surfaces: tuple[str, ...],
) -> None:
    """Fail without letting pytest render the sensitive candidate value."""

    if any(candidate in surface for surface in surfaces):
        raise AssertionError("a transient sensitive value reached an observable surface")


def _build_worker(
    *,
    urls: _DatabaseUrls,
    workspace_id: str,
    owner_actor_id: str,
    orchestrator: WorkflowOrchestratorPort,
    execution_target: GovernedExecutionTarget,
    lease_capability: str,
) -> RunOneJobWorker:
    store = PostgresBackgroundJobStore(
        urls.worker,
        application_name="schemabridge-control-worker",
    )

    def access_store_factory(
        requested_workspace_id: str,
        requested_actor_id: str,
    ) -> PostgresWorkflowAccessStore:
        assert requested_workspace_id == workspace_id
        del requested_actor_id
        return PostgresWorkflowAccessStore(
            urls.worker,
            application_name="schemabridge-control-worker",
        )

    def orchestrator_factory(
        requested_workspace_id: str,
        requested_owner_actor_id: str,
        *,
        route_context: WorkerExecutionRouteContext | None = None,
    ) -> WorkflowOrchestratorPort:
        assert requested_workspace_id == workspace_id
        assert requested_owner_actor_id == owner_actor_id
        assert route_context is not None
        assert route_context.execution_target == JobExecutionTargetRef.from_target(execution_target)
        return orchestrator

    return RunOneJobWorker(
        job_store=store,
        access_store_factory=access_store_factory,
        orchestrator_factory=orchestrator_factory,
        clock=SystemWorkflowClock(),
        capability_factory=lambda: lease_capability,
        heartbeat_supervisor=ThreadedLeaseHeartbeatSupervisor(store),
        worker_id="socket-acceptance-worker",
        lease_duration=ACCEPTANCE_WORKER_LEASE_DURATION,
        heartbeat_interval=timedelta(seconds=1),
    )


def test_unexpected_socket_error_never_reaches_uvicorn_traceback_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "SYNTHETIC-UNEXPECTED-SECRET-SENTINEL"
    token = secrets.token_urlsafe(48)

    class _ExplodingSubmit:
        def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError(secret)

    class _UnusedJobUseCase:
        def execute(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("unused job use case")

    class _Ready:
        def require_ready(self) -> None:
            return None

    app = create_http_app(
        ApiHttpServices(
            authenticator=_local_authenticator(
                token,
                LocalDemoPrincipalFactory(
                    workspace="socket-log-boundary",
                    subject="socket-log-owner",
                    roles=frozenset({IdentityRole.ANALYST}),
                ),
            ),
            clock=SystemWorkflowClock(),
            submit=_ExplodingSubmit(),  # type: ignore[arg-type]
            inspect=_UnusedJobUseCase(),  # type: ignore[arg-type]
            cancel=_UnusedJobUseCase(),  # type: ignore[arg-type]
            readiness=_Ready(),
        ),
        allowed_hosts=("127.0.0.1",),
    )
    caplog.set_level(logging.INFO)

    with _real_socket_client(app, log_level="info") as client:
        response = client.post(
            "/v1/workflows/workflow-log-boundary/execution-jobs",
            headers=_submission_headers(token, "socket-log-boundary-key-0001"),
            json={
                "expected_workflow_revision": 7,
                "expected_plan_fingerprint": PLAN_FINGERPRINT,
                "confirmation": EXECUTION_CONFIRMATION,
            },
        )

    log_text = caplog.text
    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert secret not in response.text
    assert secret not in log_text
    assert "traceback" not in log_text.casefold()
    assert "exception in asgi application" not in log_text.casefold()
    assert "error_type=RuntimeError" in log_text


def test_real_socket_saturation_and_routing_errors_keep_the_problem_contract() -> None:
    entered = Event()
    release = Event()
    active_response: list[httpx.Response] = []

    class _BlockingReadiness:
        def require_ready(self) -> None:
            entered.set()
            assert release.wait(timeout=5)

    class _UnusedJobUseCase:
        def execute(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("unused job use case")

    token = secrets.token_urlsafe(48)
    app = create_http_app(
        ApiHttpServices(
            authenticator=_local_authenticator(
                token,
                LocalDemoPrincipalFactory(
                    workspace="socket-concurrency-boundary",
                    subject="socket-concurrency-owner",
                    roles=frozenset({IdentityRole.ANALYST}),
                ),
            ),
            clock=SystemWorkflowClock(),
            submit=_UnusedJobUseCase(),  # type: ignore[arg-type]
            inspect=_UnusedJobUseCase(),  # type: ignore[arg-type]
            cancel=_UnusedJobUseCase(),  # type: ignore[arg-type]
            readiness=_BlockingReadiness(),
        ),
        max_concurrency=1,
        allowed_hosts=("127.0.0.1",),
    )

    with _real_socket_client(app) as client:
        active = Thread(
            target=lambda: active_response.append(client.get("/health/ready")),
            name="schemabridge-saturated-request",
        )
        active.start()
        assert entered.wait(timeout=5)
        saturated = client.get("/health/live")
        release.set()
        active.join(timeout=5)
        missing = client.get("/does-not-exist")
        wrong_method = client.put("/health/live")

    assert not active.is_alive()
    assert len(active_response) == 1 and active_response[0].status_code == 200
    expected = (
        (saturated, 503, "service_busy"),
        (missing, 404, "not_found"),
        (wrong_method, 405, "method_not_allowed"),
    )
    for response, status, code in expected:
        assert response.status_code == status
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["code"] == code
        assert response.json()["request_id"] == response.headers["x-request-id"]
        assert "detail" not in response.text.casefold()


def test_authenticated_job_lifecycle_over_real_socket_and_postgres(
    isolated_control_database: _DatabaseUrls,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Prove auth, scope, idempotency, cancellation, and summary-only success."""

    caplog.set_level(logging.DEBUG, logger="schemabridge.entrypoints.http.app")
    owner_token = secrets.token_urlsafe(48)
    auditor_token = secrets.token_urlsafe(48)
    other_tenant_token = secrets.token_urlsafe(48)
    invalid_token = secrets.token_urlsafe(48)
    lease_capability = secrets.token_urlsafe(48)
    owner_factory = LocalDemoPrincipalFactory(
        workspace="socket-tenant-a",
        subject="socket-owner",
        roles=frozenset({IdentityRole.ANALYST}),
    )
    auditor_factory = LocalDemoPrincipalFactory(
        workspace="socket-tenant-a",
        subject="socket-owner",
        roles=frozenset({IdentityRole.AUDITOR}),
    )
    other_tenant_factory = LocalDemoPrincipalFactory(
        workspace="socket-tenant-b",
        subject="socket-owner",
        roles=frozenset({IdentityRole.ANALYST}),
    )
    clock = SystemWorkflowClock()
    owner = owner_factory.create(now=clock.now())
    _seed_capacity_policy(
        isolated_control_database,
        workspace_id=owner.workspace_id,
    )
    execution_target = _execution_target(
        isolated_control_database,
        workspace_id=owner.workspace_id,
    )
    workflow_id = f"workflow-socket-{uuid4().hex[:16]}"
    draft = _approval_draft(workflow_id)
    PostgresWorkflowDraftStore(
        isolated_control_database.migrator,
        workspace_id=owner.workspace_id,
        owner_actor_id=owner.actor_id,
        application_name="schemabridge-control-migrator",
    ).save(draft, expected_revision=None)

    api_store = PostgresBackgroundJobStore(
        isolated_control_database.api,
        application_name="schemabridge-control-api",
    )
    policy = DenyByDefaultAuthorizationPolicy()
    access_scopes: list[tuple[str, str]] = []
    orchestrator_scopes: list[tuple[str, str]] = []

    def access_store_factory(
        workspace_id: str,
        actor_id: str,
    ) -> PostgresWorkflowAccessStore:
        access_scopes.append((workspace_id, actor_id))
        return PostgresWorkflowAccessStore(
            isolated_control_database.api,
            application_name="schemabridge-control-api",
        )

    def orchestrator_factory(
        workspace_id: str,
        actor_id: str,
    ) -> _ExecutionTargetInspector:
        orchestrator_scopes.append((workspace_id, actor_id))
        return _ExecutionTargetInspector(
            ReadOnlyWorkflowInspector(
                PostgresWorkflowDraftStore(
                    isolated_control_database.api,
                    workspace_id=workspace_id,
                    owner_actor_id=actor_id,
                    application_name="schemabridge-control-api",
                )
            ),
            execution_target,
        )

    authenticator = _BearerRouter(
        (
            _local_authenticator(owner_token, owner_factory),
            _local_authenticator(auditor_token, auditor_factory),
            _local_authenticator(other_tenant_token, other_tenant_factory),
        )
    )
    app = create_http_app(
        ApiHttpServices(
            authenticator=authenticator,
            clock=clock,
            submit=SubmitExecutionJob(
                job_store=api_store,
                access_store_factory=access_store_factory,
                orchestrator_factory=orchestrator_factory,
                authorization=policy,
                clock=clock,
            ),
            inspect=InspectExecutionJob(
                job_store=api_store,
                authorization=policy,
                clock=clock,
            ),
            cancel=CancelExecutionJob(
                job_store=api_store,
                authorization=policy,
                clock=clock,
            ),
            readiness=_Readiness(isolated_control_database.api),
        ),
        allowed_hosts=("127.0.0.1",),
    )
    body = {
        "expected_workflow_revision": 7,
        "expected_plan_fingerprint": PLAN_FINGERPRINT,
        "confirmation": EXECUTION_CONFIRMATION,
    }
    idempotency_key = f"socket-request-{uuid4().hex}"
    cancellation_key = f"socket-cancel-{uuid4().hex}"
    transient_sensitive_values = (
        owner_token,
        auditor_token,
        other_tenant_token,
        invalid_token,
        lease_capability,
        idempotency_key,
        cancellation_key,
        isolated_control_database.migrator,
        isolated_control_database.api,
        isolated_control_database.worker,
        isolated_control_database.runtime,
        isolated_control_database.catalog,
    )
    observed_response_bodies: list[str] = []
    observed_log_messages: list[str] = []

    def observe(*responses: httpx.Response) -> None:
        response_bodies = tuple(response.text for response in responses)
        log_messages = tuple(record.getMessage() for record in caplog.records)
        for candidate in transient_sensitive_values:
            if any(candidate in surface for surface in (*response_bodies, *log_messages)):
                caplog.clear()
                raise AssertionError("a transient sensitive value reached an observable surface")
        observed_response_bodies.extend(response_bodies)
        observed_log_messages.extend(log_messages)
        caplog.clear()

    with _real_socket_client(app) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")
        observe(live, ready)
        assert live.status_code == 200
        assert live.json() == {"status": "live"}
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready"}
        assert "server" not in ready.headers

        missing_bearer = client.post(
            f"/v1/workflows/{workflow_id}/execution-jobs",
            headers={"Idempotency-Key": idempotency_key},
            json=body,
        )
        wrong_bearer = client.post(
            f"/v1/workflows/{workflow_id}/execution-jobs",
            headers=_submission_headers(invalid_token, idempotency_key),
            json=body,
        )
        observe(missing_bearer, wrong_bearer)
        for denied_authentication in (missing_bearer, wrong_bearer):
            assert denied_authentication.status_code == 401
            assert denied_authentication.json()["code"] == "invalid_bearer_token"

        submitted = client.post(
            f"/v1/workflows/{workflow_id}/execution-jobs",
            headers=_submission_headers(owner_token, idempotency_key),
            json=body,
        )
        replayed = client.post(
            f"/v1/workflows/{workflow_id}/execution-jobs",
            headers=_submission_headers(owner_token, idempotency_key),
            json=body,
        )
        collision = client.post(
            f"/v1/workflows/{workflow_id}/execution-jobs",
            headers=_submission_headers(owner_token, idempotency_key),
            json={**body, "expected_workflow_revision": 8},
        )
        observe(submitted, replayed, collision)
        assert submitted.status_code == 202
        assert submitted.json()["replayed"] is False
        assert submitted.json()["job"]["status"] == "queued"
        assert replayed.status_code == 200
        assert replayed.json()["replayed"] is True
        assert replayed.json()["job"]["job_id"] == submitted.json()["job"]["job_id"]
        assert collision.status_code == 409
        assert collision.json()["code"] == "execution_job_idempotency_conflict"
        job_id = submitted.json()["job"]["job_id"]
        assert isinstance(job_id, str)
        assert access_scopes == [(owner.workspace_id, owner.actor_id)]
        assert orchestrator_scopes == [(owner.workspace_id, owner.actor_id)]

        owner_get = client.get(
            f"/v1/execution-jobs/{job_id}",
            headers=_authorization(owner_token),
        )
        cross_tenant_get = client.get(
            f"/v1/execution-jobs/{job_id}",
            headers=_authorization(other_tenant_token),
        )
        role_denied = client.post(
            f"/v1/workflows/{workflow_id}/execution-jobs",
            headers=_submission_headers(auditor_token, idempotency_key),
            json=body,
        )
        cross_tenant_submit = client.post(
            f"/v1/workflows/{workflow_id}/execution-jobs",
            headers=_submission_headers(other_tenant_token, idempotency_key),
            json=body,
        )
        observe(owner_get, cross_tenant_get, role_denied, cross_tenant_submit)
        assert owner_get.status_code == 200
        assert cross_tenant_get.status_code == 404
        assert role_denied.status_code == 404
        assert cross_tenant_submit.status_code == 404
        unavailable_problem = _problem_without_request_id(cross_tenant_get)
        assert _problem_without_request_id(role_denied) == unavailable_problem
        assert _problem_without_request_id(cross_tenant_submit) == unavailable_problem
        assert len(access_scopes) == 2
        assert orchestrator_scopes == [(owner.workspace_id, owner.actor_id)]

        orchestrator = _SafeCompletedOrchestrator(draft)
        worker_result = _build_worker(
            urls=isolated_control_database,
            workspace_id=owner.workspace_id,
            owner_actor_id=owner.actor_id,
            orchestrator=orchestrator,
            execution_target=execution_target,
            lease_capability=lease_capability,
        ).execute()
        assert worker_result.outcome is WorkerIterationOutcome.SUCCEEDED
        assert worker_result.job_id == job_id
        assert orchestrator.decide_calls == 1

        succeeded = client.get(
            f"/v1/execution-jobs/{job_id}",
            headers=_authorization(owner_token),
        )
        observe(succeeded)
        assert succeeded.status_code == 200
        succeeded_payload = succeeded.json()
        assert succeeded_payload["status"] == "succeeded"
        assert succeeded_payload["result"] == {
            "workflow_revision": 11,
            "stage": "publication_proposed",
            "row_count": 3,
            "preview_fingerprint": PREVIEW_FINGERPRINT,
            "rejected_count": 25_001,
            "rejection_counts": [
                {"code": "non_finite_identifier", "count": 1},
                {"code": "null_join_key", "count": 1},
            ],
            "unclassified_rejection_count": 24_999,
            "rejection_counts_complete": False,
            "rejection_truncated": True,
            "truncated": True,
            "completed_at": succeeded_payload["result"]["completed_at"],
        }

        queued_for_cancel = client.post(
            f"/v1/workflows/{workflow_id}/execution-jobs",
            headers=_submission_headers(owner_token, cancellation_key),
            json=body,
        )
        observe(queued_for_cancel)
        assert queued_for_cancel.status_code == 202
        cancelled_job_id = queued_for_cancel.json()["job"]["job_id"]
        assert isinstance(cancelled_job_id, str)

        role_cancel_denial = client.post(
            f"/v1/execution-jobs/{cancelled_job_id}/cancel",
            headers=_authorization(auditor_token),
            json={"confirmation": CANCELLATION_CONFIRMATION},
        )
        tenant_cancel_denial = client.post(
            f"/v1/execution-jobs/{cancelled_job_id}/cancel",
            headers=_authorization(other_tenant_token),
            json={"confirmation": CANCELLATION_CONFIRMATION},
        )
        cancelled = client.post(
            f"/v1/execution-jobs/{cancelled_job_id}/cancel",
            headers=_authorization(owner_token),
            json={"confirmation": CANCELLATION_CONFIRMATION},
        )
        cancelled_again = client.get(
            f"/v1/execution-jobs/{cancelled_job_id}",
            headers=_authorization(owner_token),
        )
        observe(role_cancel_denial, tenant_cancel_denial, cancelled, cancelled_again)
        assert role_cancel_denial.status_code == tenant_cancel_denial.status_code == 404
        assert _problem_without_request_id(role_cancel_denial) == (
            _problem_without_request_id(tenant_cancel_denial)
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert cancelled_again.status_code == 200
        assert cancelled_again.json()["status"] == "cancelled"

    with psycopg.connect(isolated_control_database.migrator) as connection:
        stored_summary = connection.execute(
            """
            SELECT
                status,
                result_rejected_count,
                result_rejection_code_counts,
                result_truncated
            FROM schemabridge_control.execution_jobs
            WHERE workspace_id = %s AND job_id = %s
            """,
            (owner.workspace_id, job_id),
        ).fetchone()
        durable_counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM schemabridge_control.execution_jobs),
                (SELECT count(*) FROM schemabridge_control.execution_job_events)
            """
        ).fetchone()
        durable = connection.execute(
            """
            SELECT jsonb_build_object(
                'jobs', COALESCE(jsonb_agg(DISTINCT to_jsonb(job)), '[]'::jsonb),
                'events', COALESCE(jsonb_agg(DISTINCT to_jsonb(event)), '[]'::jsonb)
            )::text
            FROM schemabridge_control.execution_jobs AS job
            LEFT JOIN schemabridge_control.execution_job_events AS event
              ON event.workspace_id = job.workspace_id
             AND event.job_id = job.job_id
            WHERE job.workspace_id = %s
            """,
            (owner.workspace_id,),
        ).fetchone()
    assert stored_summary == (
        "succeeded",
        25_001,
        {
            "non_finite_identifier": 1,
            "null_join_key": 1,
            "unclassified_rejections": 24_999,
        },
        True,
    )
    assert durable_counts == (2, 5)
    assert durable is not None
    durable_text = str(durable[0])
    response_text = "\n".join(observed_response_bodies)
    observed_log_messages.extend(record.getMessage() for record in caplog.records)
    log_text = "\n".join(observed_log_messages)
    caplog.clear()
    for transient_secret in transient_sensitive_values:
        _require_sensitive_value_absent(
            transient_secret,
            surfaces=(durable_text, response_text, log_text),
        )
    for forbidden in (
        '"rows"',
        '"sql"',
        '"parameters"',
        '"prompt"',
        '"bearer"',
        '"claims"',
        '"idempotency_digest"',
        '"lease"',
        '"request_fingerprint"',
        '"submitting_actor_id"',
        '"unclassified_rejections"',
        '"workflow_access_scope"',
        '"workflow_workspace_id"',
        '"workspace_id"',
        "synthetic_acceptance_reader",
    ):
        assert forbidden not in response_text.casefold()


def test_oidc_jwks_real_source_and_terminal_worker_states_over_sockets(
    isolated_control_database: _DatabaseUrls,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Prove signed OIDC/JWKS plus real source execution and worker terminals."""

    caplog.set_level(logging.DEBUG, logger="schemabridge.entrypoints.http.app")
    pseudonymization_secret = secrets.token_urlsafe(48)
    invalid_token = secrets.token_urlsafe(48)
    success_capability = secrets.token_urlsafe(48)
    cooperative_capability = secrets.token_urlsafe(48)
    dead_letter_capability = secrets.token_urlsafe(48)
    idempotency_key = f"oidc-request-{uuid4().hex}"
    cooperative_key = f"oidc-cooperative-{uuid4().hex}"
    dead_letter_key = f"oidc-dead-letter-{uuid4().hex}"
    observed_response_bodies: list[str] = []
    observed_log_messages: list[str] = []

    with ExitStack() as stack:
        owner_oidc = stack.enter_context(
            _synthetic_oidc_identity(
                subject="oidc-owner-private-subject",
                tenant="socket-tenant-a",
                groups=("analysts",),
            )
        )
        other_tenant_oidc = stack.enter_context(
            _synthetic_oidc_identity(
                subject="oidc-other-private-subject",
                tenant="socket-tenant-b",
                groups=("analysts",),
            )
        )
        pseudonymization_key = pseudonymization_secret.encode("ascii")
        seed_authenticator = _oidc_authenticator(
            owner_oidc,
            pseudonymization_key=pseudonymization_key,
        )
        owner_authenticator = _oidc_authenticator(
            owner_oidc,
            pseudonymization_key=pseudonymization_key,
        )
        other_tenant_authenticator = _oidc_authenticator(
            other_tenant_oidc,
            pseudonymization_key=pseudonymization_key,
        )
        clock = SystemWorkflowClock()
        owner = seed_authenticator.authenticate(owner_oidc.bearer_token, clock.now())
        assert owner.authentication_method is AuthenticationMethod.OIDC
        assert owner.roles == frozenset({IdentityRole.ANALYST})
        _seed_capacity_policy(
            isolated_control_database,
            workspace_id=owner.workspace_id,
        )
        execution_target = _execution_target(
            isolated_control_database,
            workspace_id=owner.workspace_id,
        )

        workflow_id = f"workflow-oidc-{uuid4().hex[:16]}"
        auxiliary_database = tmp_path / "unused-publication.db"
        validated = _seed_real_workflow(
            urls=isolated_control_database,
            owner=owner,
            workflow_id=workflow_id,
            auxiliary_database=auxiliary_database,
        )
        plan_fingerprint = validated.plan_fingerprint
        assert plan_fingerprint is not None

        api_store = PostgresBackgroundJobStore(
            isolated_control_database.api,
            application_name="schemabridge-control-api",
        )
        policy = DenyByDefaultAuthorizationPolicy()
        access_scopes: list[tuple[str, str]] = []
        orchestrator_scopes: list[tuple[str, str]] = []

        def access_store_factory(
            workspace_id: str,
            actor_id: str,
        ) -> PostgresWorkflowAccessStore:
            access_scopes.append((workspace_id, actor_id))
            return PostgresWorkflowAccessStore(
                isolated_control_database.api,
                application_name="schemabridge-control-api",
            )

        def orchestrator_factory(
            workspace_id: str,
            actor_id: str,
        ) -> _ExecutionTargetInspector:
            orchestrator_scopes.append((workspace_id, actor_id))
            return _ExecutionTargetInspector(
                ReadOnlyWorkflowInspector(
                    PostgresWorkflowDraftStore(
                        isolated_control_database.api,
                        workspace_id=workspace_id,
                        owner_actor_id=actor_id,
                        application_name="schemabridge-control-api",
                    )
                ),
                execution_target,
            )

        app = create_http_app(
            ApiHttpServices(
                authenticator=_BearerRouter((owner_authenticator, other_tenant_authenticator)),
                clock=clock,
                submit=SubmitExecutionJob(
                    job_store=api_store,
                    access_store_factory=access_store_factory,
                    orchestrator_factory=orchestrator_factory,
                    authorization=policy,
                    clock=clock,
                ),
                inspect=InspectExecutionJob(
                    job_store=api_store,
                    authorization=policy,
                    clock=clock,
                ),
                cancel=CancelExecutionJob(
                    job_store=api_store,
                    authorization=policy,
                    clock=clock,
                ),
                readiness=_Readiness(isolated_control_database.api),
            ),
            allowed_hosts=("127.0.0.1",),
        )
        body = {
            "expected_workflow_revision": validated.revision,
            "expected_plan_fingerprint": plan_fingerprint,
            "confirmation": EXECUTION_CONFIRMATION,
        }
        transient_sensitive_values = (
            owner_oidc.bearer_token,
            other_tenant_oidc.bearer_token,
            *owner_oidc.transient_values,
            *other_tenant_oidc.transient_values,
            pseudonymization_secret,
            invalid_token,
            success_capability,
            cooperative_capability,
            dead_letter_capability,
            idempotency_key,
            cooperative_key,
            dead_letter_key,
            "oidc-owner-private-subject",
            "oidc-other-private-subject",
            "socket-tenant-a",
            "socket-tenant-b",
            isolated_control_database.migrator,
            isolated_control_database.api,
            isolated_control_database.worker,
            isolated_control_database.runtime,
            isolated_control_database.catalog,
            READER_DSN,
        )

        def observe(*responses: httpx.Response) -> None:
            response_bodies = tuple(response.text for response in responses)
            log_messages = tuple(record.getMessage() for record in caplog.records)
            for candidate in transient_sensitive_values:
                if any(candidate in surface for surface in (*response_bodies, *log_messages)):
                    caplog.clear()
                    raise AssertionError(
                        "a transient sensitive value reached an observable surface"
                    )
            observed_response_bodies.extend(response_bodies)
            observed_log_messages.extend(log_messages)
            caplog.clear()

        with _real_socket_client(app) as client:
            ready = client.get("/health/ready")
            missing_bearer = client.post(
                f"/v1/workflows/{workflow_id}/execution-jobs",
                headers={"Idempotency-Key": idempotency_key},
                json=body,
            )
            wrong_bearer = client.post(
                f"/v1/workflows/{workflow_id}/execution-jobs",
                headers=_submission_headers(invalid_token, idempotency_key),
                json=body,
            )
            observe(ready, missing_bearer, wrong_bearer)
            assert ready.status_code == 200
            assert ready.json() == {"status": "ready"}
            assert missing_bearer.status_code == wrong_bearer.status_code == 401

            submitted = client.post(
                f"/v1/workflows/{workflow_id}/execution-jobs",
                headers=_submission_headers(owner_oidc.bearer_token, idempotency_key),
                json=body,
            )
            replayed = client.post(
                f"/v1/workflows/{workflow_id}/execution-jobs",
                headers=_submission_headers(owner_oidc.bearer_token, idempotency_key),
                json=body,
            )
            collision = client.post(
                f"/v1/workflows/{workflow_id}/execution-jobs",
                headers=_submission_headers(owner_oidc.bearer_token, idempotency_key),
                json={
                    **body,
                    "expected_workflow_revision": validated.revision + 1,
                },
            )
            observe(submitted, replayed, collision)
            assert submitted.status_code == 202
            assert submitted.json()["replayed"] is False
            assert replayed.status_code == 200
            assert replayed.json()["replayed"] is True
            assert replayed.json()["job"]["job_id"] == submitted.json()["job"]["job_id"]
            assert collision.status_code == 409
            assert collision.json()["code"] == "execution_job_idempotency_conflict"
            job_id = submitted.json()["job"]["job_id"]
            assert isinstance(job_id, str)
            assert access_scopes == [(owner.workspace_id, owner.actor_id)]
            assert orchestrator_scopes == [(owner.workspace_id, owner.actor_id)]

            cross_tenant_get = client.get(
                f"/v1/execution-jobs/{job_id}",
                headers=_authorization(other_tenant_oidc.bearer_token),
            )
            cross_tenant_submit = client.post(
                f"/v1/workflows/{workflow_id}/execution-jobs",
                headers=_submission_headers(
                    other_tenant_oidc.bearer_token,
                    idempotency_key,
                ),
                json=body,
            )
            observe(cross_tenant_get, cross_tenant_submit)
            assert cross_tenant_get.status_code == cross_tenant_submit.status_code == 404
            assert _problem_without_request_id(cross_tenant_get) == (
                _problem_without_request_id(cross_tenant_submit)
            )
            assert len(access_scopes) == 2
            assert orchestrator_scopes == [(owner.workspace_id, owner.actor_id)]

            cooperative_submission = client.post(
                f"/v1/workflows/{workflow_id}/execution-jobs",
                headers=_submission_headers(
                    owner_oidc.bearer_token,
                    cooperative_key,
                ),
                json=body,
            )
            dead_letter_submission = client.post(
                f"/v1/workflows/{workflow_id}/execution-jobs",
                headers=_submission_headers(
                    owner_oidc.bearer_token,
                    dead_letter_key,
                ),
                json=body,
            )
            observe(cooperative_submission, dead_letter_submission)
            assert cooperative_submission.status_code == 202
            assert dead_letter_submission.status_code == 202
            cooperative_job_id = cooperative_submission.json()["job"]["job_id"]
            dead_letter_job_id = dead_letter_submission.json()["job"]["job_id"]
            assert isinstance(cooperative_job_id, str)
            assert isinstance(dead_letter_job_id, str)

            real_worker_orchestrator = _CapturingOrchestrator(
                _real_orchestrator(
                    PostgresWorkflowDraftStore(
                        isolated_control_database.worker,
                        workspace_id=owner.workspace_id,
                        owner_actor_id=owner.actor_id,
                        application_name="schemabridge-control-worker",
                    ),
                    auxiliary_database=auxiliary_database,
                )
            )
            worker_result = _build_worker(
                urls=isolated_control_database,
                workspace_id=owner.workspace_id,
                owner_actor_id=owner.actor_id,
                orchestrator=real_worker_orchestrator,
                execution_target=execution_target,
                lease_capability=success_capability,
            ).execute()
            assert worker_result.outcome is WorkerIterationOutcome.SUCCEEDED
            assert worker_result.job_id == job_id
            assert real_worker_orchestrator.decide_calls == 1
            completed = real_worker_orchestrator.completed
            assert completed is not None and completed.execution is not None
            assert completed.execution.rows == (
                ("2026-01-01", 2),
                ("2026-01-02", 1),
                ("2026-01-03", 1),
            )
            assert completed.execution.database_user == "schemabridge_reader"
            assert completed.execution.transaction_read_only is True
            assert completed.execution.rejected_count == 3

            succeeded = client.get(
                f"/v1/execution-jobs/{job_id}",
                headers=_authorization(owner_oidc.bearer_token),
            )
            observe(succeeded)
            assert succeeded.status_code == 200
            succeeded_payload = succeeded.json()
            assert succeeded_payload["status"] == "succeeded"
            assert succeeded_payload["result"] == {
                "workflow_revision": completed.revision,
                "stage": "publication_proposed",
                "row_count": 3,
                "preview_fingerprint": completed.execution.preview_fingerprint,
                "rejected_count": 3,
                "rejection_counts": [
                    {"code": "non_finite_identifier", "count": 1},
                    {"code": "non_integral_identifier", "count": 1},
                    {"code": "null_join_key", "count": 1},
                ],
                "unclassified_rejection_count": 0,
                "rejection_counts_complete": True,
                "rejection_truncated": False,
                "truncated": False,
                "completed_at": succeeded_payload["result"]["completed_at"],
            }

            cooperative_store = PostgresBackgroundJobStore(
                isolated_control_database.worker,
                application_name="schemabridge-control-worker",
            )
            cooperative_claim = cooperative_store.claim_next(
                worker_id="socket-cooperative-worker",
                lease_token=cooperative_capability,
                lease_duration=ACCEPTANCE_WORKER_LEASE_DURATION,
            )
            assert cooperative_claim is not None
            assert cooperative_claim.id == cooperative_job_id
            assert cooperative_claim.lease is not None
            cancellation_requested = client.post(
                f"/v1/execution-jobs/{cooperative_job_id}/cancel",
                headers=_authorization(owner_oidc.bearer_token),
                json={"confirmation": CANCELLATION_CONFIRMATION},
            )
            cancellation_visible = client.get(
                f"/v1/execution-jobs/{cooperative_job_id}",
                headers=_authorization(owner_oidc.bearer_token),
            )
            observe(cancellation_requested, cancellation_visible)
            assert cancellation_requested.status_code == 200
            assert cancellation_requested.json()["status"] == "cancel_requested"
            assert cancellation_visible.status_code == 200
            assert cancellation_visible.json()["status"] == "cancel_requested"
            cooperative_store.acknowledge_cancellation(
                cooperative_job_id,
                worker_id=cooperative_claim.lease.worker_id,
                lease_token=cooperative_capability,
                fencing_token=cooperative_claim.lease.fencing_token,
                cancelled_at=datetime.now(UTC),
            )
            cooperative_cancelled = client.get(
                f"/v1/execution-jobs/{cooperative_job_id}",
                headers=_authorization(owner_oidc.bearer_token),
            )
            observe(cooperative_cancelled)
            assert cooperative_cancelled.status_code == 200
            assert cooperative_cancelled.json()["status"] == "cancelled"

            dead_letter_result = _build_worker(
                urls=isolated_control_database,
                workspace_id=owner.workspace_id,
                owner_actor_id=owner.actor_id,
                orchestrator=_InspectCrashOrchestrator(),
                execution_target=execution_target,
                lease_capability=dead_letter_capability,
            ).execute()
            assert dead_letter_result.outcome is WorkerIterationOutcome.DEAD_LETTERED
            assert dead_letter_result.job_id == dead_letter_job_id
            dead_lettered = client.get(
                f"/v1/execution-jobs/{dead_letter_job_id}",
                headers=_authorization(owner_oidc.bearer_token),
            )
            observe(dead_lettered)
            assert dead_lettered.status_code == 200
            assert dead_lettered.json()["status"] == "dead_lettered"
            assert dead_lettered.json()["failure_code"] == "unexpected_worker_failure"

        with psycopg.connect(isolated_control_database.migrator) as connection:
            stored_summary = connection.execute(
                """
                SELECT
                    status,
                    result_rejected_count,
                    result_rejection_code_counts,
                    result_truncated
                FROM schemabridge_control.execution_jobs
                WHERE workspace_id = %s AND job_id = %s
                """,
                (owner.workspace_id, job_id),
            ).fetchone()
            durable_counts = connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM schemabridge_control.execution_jobs),
                    (SELECT count(*) FROM schemabridge_control.execution_job_events)
                """
            ).fetchone()
            durable_statuses = connection.execute(
                """
                SELECT status, count(*)
                FROM schemabridge_control.execution_jobs
                GROUP BY status
                ORDER BY status
                """
            ).fetchall()
            durable = connection.execute(
                """
                SELECT jsonb_build_object(
                    'jobs', COALESCE(jsonb_agg(DISTINCT to_jsonb(job)), '[]'::jsonb),
                    'events', COALESCE(jsonb_agg(DISTINCT to_jsonb(event)), '[]'::jsonb),
                    'workflows', (
                        SELECT COALESCE(jsonb_agg(to_jsonb(draft)), '[]'::jsonb)
                        FROM schemabridge_control.agent_workflow_drafts AS draft
                        WHERE draft.workspace_id = %s
                    ),
                    'grants', (
                        SELECT COALESCE(jsonb_agg(to_jsonb(access)), '[]'::jsonb)
                        FROM schemabridge_control.workflow_access_grants AS access
                        WHERE access.workspace_id = %s
                    )
                )::text
                FROM schemabridge_control.execution_jobs AS job
                LEFT JOIN schemabridge_control.execution_job_events AS event
                  ON event.workspace_id = job.workspace_id
                 AND event.job_id = job.job_id
                WHERE job.workspace_id = %s
                """,
                (
                    owner.workspace_id,
                    owner.workspace_id,
                    owner.workspace_id,
                ),
            ).fetchone()
        assert stored_summary == (
            "succeeded",
            3,
            {
                "non_finite_identifier": 1,
                "non_integral_identifier": 1,
                "null_join_key": 1,
            },
            False,
        )
        assert durable_counts == (3, 10)
        assert durable_statuses == [
            ("cancelled", 1),
            ("dead_lettered", 1),
            ("succeeded", 1),
        ]
        assert durable is not None
        durable_text = str(durable[0])
        response_text = "\n".join(observed_response_bodies)
        observed_log_messages.extend(record.getMessage() for record in caplog.records)
        log_text = "\n".join(observed_log_messages)
        caplog.clear()
        for transient_secret in transient_sensitive_values:
            _require_sensitive_value_absent(
                transient_secret,
                surfaces=(durable_text, response_text, log_text),
            )
        for forbidden in (
            '"rows"',
            '"columns"',
            '"sql"',
            '"parameters"',
            '"prompt"',
            '"bearer"',
            '"claims"',
            '"idempotency_digest"',
            '"lease"',
            '"request_fingerprint"',
            '"submitting_actor_id"',
            '"unclassified_rejections"',
            '"workflow_access_scope"',
            '"workflow_workspace_id"',
            '"workspace_id"',
            "schemabridge_reader",
            "2026-01-01",
        ):
            assert forbidden not in response_text.casefold()
