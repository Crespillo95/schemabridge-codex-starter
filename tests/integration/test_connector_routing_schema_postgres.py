"""PostgreSQL proof for the M28 governed connector-routing schema."""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from schemabridge.adapters.catalog.postgres_connector_routing import (
    PostgresCatalogConnectorRouteReader,
)
from schemabridge.adapters.catalog.postgres_refresh import PostgresCatalogRefreshStore
from schemabridge.adapters.connectors.postgres_routing import (
    ExecutionConnectorLeaseContext,
    PostgresExecutionConnectorRouteReader,
)
from schemabridge.adapters.control_plane.postgres_identity_bindings import (
    PostgresIdentityBindingResolver,
)
from schemabridge.adapters.control_plane.postgres_identity_rotation import (
    PostgresIdentityRotationStore,
)
from schemabridge.adapters.control_plane.postgres_jobs import (
    PostgresBackgroundJobStore,
)
from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.adapters.storage.identity_resolving import (
    IdentityResolvingBackgroundJobApiStore,
)
from schemabridge.application.identity_rotation import (
    ApproveIdentityRotation,
    CompleteIdentityRotation,
    InitializeVerifiedIdentityState,
    PrepareIdentityRotation,
)
from schemabridge.application.job_worker import WorkerExecutionRouteContext
from schemabridge.application.ports.catalog_inventory import (
    CatalogInventoryError,
    CatalogInventoryErrorCode,
)
from schemabridge.application.ports.control_plane_migrations import (
    ControlPlaneMigrationError,
)
from schemabridge.domain.background_jobs import (
    BackgroundJob,
    JobAuthorization,
    JobExecutionTargetRef,
    JobWorkflowAccessScope,
)
from schemabridge.domain.catalog_inventory import (
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogRefreshCommand,
    CatalogRefreshFailureCode,
    CatalogRefreshMode,
    CatalogSourcePage,
)
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    QueryCostBudget,
    SourceConnectorKind,
    SourceDialect,
    normalize_postgres_native_type,
    postgres_type_contract_fingerprint,
)
from schemabridge.domain.identity import AuthenticationMethod
from schemabridge.domain.identity_rotation import (
    IdentityInitializationConfirmation,
    IdentityRotationConfirmation,
    VerifiedDualKeyOidcDerivation,
    VerifiedOidcKeyDerivation,
    build_identity_initialization_approval,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations/control_plane"
ADMIN_DSN = "postgresql://postgres:postgres@127.0.0.1:55434/postgres"
CONTROL_ROLES = (
    "schemabridge_migrator",
    "schemabridge_runtime",
    "schemabridge_reconciler",
    "schemabridge_api",
    "schemabridge_worker",
    "schemabridge_catalog",
)
WORKSPACE_ID = "workspace-m28-routing"
CONNECTION_ID = "connection-m28-routing"
EXPECTED_READER = "schemabridge_source_reader"
TYPE_CONTRACT_FINGERPRINT = postgres_type_contract_fingerprint()
CONTRACT_FINGERPRINT = hashlib.sha256(b"m28-connector-contract").hexdigest()
AUDIT_KEYS = {"v1": b"control-audit-key-0123456789-abcdef"}
APPLY_ROUTE_SQL = (
    "SELECT * FROM schemabridge_control.apply_connector_route_change("
    + ", ".join(["%s"] * 35)
    + ")"
)


@dataclass(frozen=True)
class _DatabaseUrls:
    database: str
    migrator: str
    runtime: str
    reconciler: str
    api: str
    worker: str
    catalog: str


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _opaque_identity(kind: str, version: str, label: str) -> str:
    return f"sb_{kind}_{version}_{_digest(f'{version}:{label}')}"


def _oidc_pair(
    namespace: str,
    *,
    observed_at: datetime,
) -> VerifiedDualKeyOidcDerivation:
    workspace_reference = _digest(f"{namespace}:workspace-reference")
    actor_reference = _digest(f"{namespace}:actor-reference")
    verification = _digest(f"{namespace}:verification")
    provenance = _digest(f"{namespace}:oidc-provenance")

    def derivation(version: str) -> VerifiedOidcKeyDerivation:
        return VerifiedOidcKeyDerivation(
            verification_id=verification,
            workspace_reference_digest=workspace_reference,
            actor_reference_digest=actor_reference,
            workspace_id=_opaque_identity("workspace", version, namespace),
            actor_id=_opaque_identity("actor", version, namespace),
            key_version=version,
            provenance_fingerprint=provenance,
            provenance_version=1,
            policy_version=1,
            verified_at=observed_at,
            authentication_method=AuthenticationMethod.OIDC,
        )

    return VerifiedDualKeyOidcDerivation(
        previous=derivation("v1"),
        current=derivation("v2"),
    )


def _initialize_and_rotate_identity(
    urls: _DatabaseUrls,
    pair: VerifiedDualKeyOidcDerivation,
    *,
    approved_at: datetime,
) -> None:
    store = PostgresIdentityRotationStore(urls.runtime, AUDIT_KEYS, "v1")
    derivations = (pair,)
    initialization = build_identity_initialization_approval(
        derivations,
        evidence_fingerprint=_digest("connector-rotation-evidence"),
        actor=pair.previous.actor_id,
        approved_at=approved_at,
        confirmation=IdentityInitializationConfirmation.INITIALIZE_VERIFIED_OIDC_BINDINGS,
    )
    InitializeVerifiedIdentityState(store).execute(
        derivations,
        evidence_fingerprint=initialization.evidence_fingerprint,
        actor=initialization.actor,
        approved_at=initialization.approved_at,
        confirmation=initialization.confirmation,
    )
    plan = PrepareIdentityRotation(store).execute(
        pair.previous.workspace_id,
        derivations,
    )
    approved = ApproveIdentityRotation(store).execute(
        plan,
        actor=pair.previous.actor_id,
        approved_at=approved_at + timedelta(seconds=1),
        confirmation=IdentityRotationConfirmation.ROTATE_VERIFIED_OIDC_BINDINGS,
    )
    CompleteIdentityRotation(store).execute(
        plan,
        approved.approval,
        completed_at=approved_at + timedelta(seconds=2),
    )


def _admin_dsn() -> str:
    return os.environ.get("SCHEMABRIDGE_TEST_CONTROL_ADMIN_DATABASE_URL", ADMIN_DSN)


def _role_dsn(role: str, database: str) -> str:
    return f"postgresql://{role}:{role}@127.0.0.1:55434/{database}"


def _database_urls(database: str) -> _DatabaseUrls:
    return _DatabaseUrls(
        database=database,
        migrator=_role_dsn("schemabridge_migrator", database),
        runtime=_role_dsn("schemabridge_runtime", database),
        reconciler=_role_dsn("schemabridge_reconciler", database),
        api=_role_dsn("schemabridge_api", database),
        worker=_role_dsn("schemabridge_worker", database),
        catalog=_role_dsn("schemabridge_catalog", database),
    )


def _create_database(database: str) -> _DatabaseUrls:
    urls = _database_urls(database)
    with psycopg.connect(_admin_dsn(), autocommit=True) as connection:
        available_roles = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT rolname
                FROM pg_catalog.pg_roles
                WHERE rolname = ANY(%s)
                """,
                (list(CONTROL_ROLES),),
            )
        }
        if available_roles != set(CONTROL_ROLES):
            pytest.fail("the six control-plane roles must exist before the M28 test")
        connection.execute(
            sql.SQL("CREATE DATABASE {} OWNER schemabridge_migrator").format(
                sql.Identifier(database)
            )
        )
        connection.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database))
        )
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(database),
                sql.SQL(", ").join(sql.Identifier(role) for role in CONTROL_ROLES),
            )
        )
    return urls


def _drop_database(database: str) -> None:
    with psycopg.connect(_admin_dsn(), autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
        )


def _migration_subset(directory: Path, latest: int) -> Path:
    directory.mkdir()
    for version in range(1, latest + 1):
        source = next(MIGRATIONS.glob(f"{version:04d}_*.sql"))
        shutil.copyfile(source, directory / source.name)
    return directory


@pytest.fixture(scope="module")
def connector_database(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_DatabaseUrls]:
    database = f"schemabridge_connector_v9_{uuid4().hex[:12]}"
    urls = _create_database(database)
    try:
        v8_migrations = _migration_subset(
            tmp_path_factory.mktemp("connector-v8-parent") / "migrations",
            8,
        )
        v8 = PostgresControlPlaneMigrator(urls.migrator, v8_migrations).migrate()
        assert v8.applied_versions == (1, 2, 3, 4, 5, 6, 7, 8)
        assert v8.inspection.current_version == 8

        upgraded = PostgresControlPlaneMigrator(urls.migrator, MIGRATIONS).migrate()
        assert upgraded.applied_versions == (9,)
        assert upgraded.inspection.current_version == 9
        yield urls
    finally:
        _drop_database(database)


def _budget() -> QueryCostBudget:
    return QueryCostBudget(
        explain_timeout_ms=2_500,
        max_response_bytes=262_144,
        max_total_cost=Decimal("12345.67"),
        max_estimated_rows=250_000,
        max_plan_nodes=500,
        max_plan_depth=32,
        max_plan_width=8_192,
    )


def _target(
    route_revision: int,
    *,
    workspace_id: str = WORKSPACE_ID,
    connection_id: str = CONNECTION_ID,
) -> GovernedExecutionTarget:
    budget = _budget()
    return GovernedExecutionTarget(
        workspace_id=workspace_id,
        connection_id=CatalogConnectionId(connection_id),
        connector_kind=SourceConnectorKind.POSTGRESQL,
        dialect=SourceDialect.POSTGRESQL,
        route_revision=route_revision,
        route_fingerprint=_digest(f"route:{route_revision}"),
        expected_reader=EXPECTED_READER,
        source_identity_fingerprint=_digest(f"source:{workspace_id}:{connection_id}"),
        catalog_identity_fingerprint=_digest(f"catalog:{workspace_id}:{connection_id}"),
        type_contract_fingerprint=TYPE_CONTRACT_FINGERPRINT,
        cost_budget=budget,
        cost_budget_fingerprint=budget.fingerprint,
    )


def _route_change_args(
    *,
    operation: str,
    expected_head_revision: int,
    target: GovernedExecutionTarget,
    label: str,
    workspace_id: str = WORKSPACE_ID,
    connection_id: str = CONNECTION_ID,
    contract_version: int = 1,
    contract_fingerprint: str = CONTRACT_FINGERPRINT,
    type_contract_version: int = 1,
    type_contract_fingerprint: str | None = None,
) -> tuple[object, ...]:
    bindings: tuple[str | None, str | None, str | None, str | None]
    if operation == "disable":
        bindings = (None, None, None, None)
    else:
        bindings = (
            f"vault:preflight:{label}",
            f"vault:catalog:{label}",
            f"vault:execution:{label}",
            f"vault:profile:{label}",
        )
    confirmation = {
        "create": "CREATE CONNECTOR ROUTE",
        "rotate": "ROTATE CONNECTOR ROUTE",
        "disable": "DISABLE CONNECTOR ROUTE",
    }[operation]
    budget = target.cost_budget
    audit_digest = _digest(f"audit-id:{label}")
    return (
        workspace_id,
        connection_id,
        operation,
        expected_head_revision,
        contract_version,
        target.route_revision,
        target.route_fingerprint,
        target.expected_reader,
        type_contract_version,
        (
            target.type_contract_fingerprint
            if type_contract_fingerprint is None
            else type_contract_fingerprint
        ),
        target.source_identity_fingerprint,
        target.catalog_identity_fingerprint,
        budget.explain_timeout_ms,
        budget.max_response_bytes,
        budget.max_total_cost,
        budget.max_estimated_rows,
        budget.max_plan_nodes,
        budget.max_plan_depth,
        budget.max_plan_width,
        budget.fingerprint,
        contract_fingerprint,
        target.fingerprint,
        *bindings,
        _digest(f"proposal:{label}"),
        f"approval-{label}",
        _digest(f"approval:{label}"),
        "sb_platform_admin_v1",
        _digest(f"idempotency:{label}"),
        f"connector_route_audit_{audit_digest}",
        _digest(f"audit:{label}"),
        _digest(f"head:{label}"),
        confirmation,
    )


def _apply_route(dsn: str, arguments: tuple[object, ...]) -> tuple[object, ...]:
    with psycopg.connect(dsn) as connection:
        result = connection.execute(APPLY_ROUTE_SQL, arguments).fetchone()
    assert result is not None
    return result


def _insert_capacity_policy(dsn: str, workspace_id: str) -> None:
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.tenant_capacity_policies (
                workspace_id,
                connection_limit,
                asset_limit,
                field_limit,
                api_requests_per_minute,
                api_window_seconds,
                nonterminal_job_limit,
                catalog_cursor_ttl_seconds,
                generation_retention_seconds,
                version,
                updated_by,
                created_at,
                updated_at
            ) VALUES (
                %s, 10, 10000, 100000, 100, 60, 10, 900, 1800, 1,
                'sb_platform_admin_v1', clock_timestamp(), clock_timestamp()
            )
            """,
            (workspace_id,),
        )


def _insert_catalog_connection(
    dsn: str,
    *,
    workspace_id: str = WORKSPACE_ID,
    connection_id: str = CONNECTION_ID,
) -> None:
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.catalog_connections (
                workspace_id,
                connection_id,
                display_name,
                source_kind,
                catalog_scope,
                environment,
                platform_instance,
                status,
                registration_fingerprint,
                idempotency_digest,
                created_by_actor_id,
                created_at,
                updated_at
            ) VALUES (
                %s, %s, 'M28 synthetic connector', 'synthetic',
                'synthetic-test', 'TEST', 'm28-instance', 'enabled',
                %s, %s, 'sb_platform_admin_v1',
                clock_timestamp(), clock_timestamp()
            )
            """,
            (
                workspace_id,
                connection_id,
                _digest(f"connector-registration:{workspace_id}:{connection_id}"),
                _digest(f"connector-registration-idempotency:{workspace_id}:{connection_id}"),
            ),
        )


def _complete_empty_full_refresh(
    urls: _DatabaseUrls,
    *,
    workspace_id: str,
    connection_id: str,
    target: GovernedExecutionTarget,
    contract_version: int,
    label: str,
) -> None:
    catalog_connection_id = CatalogConnectionId(connection_id)
    requested = PostgresCatalogRefreshStore(urls.api).request(
        CatalogRefreshCommand(
            workspace_id=workspace_id,
            connection_id=catalog_connection_id,
            mode=CatalogRefreshMode.FULL,
            requested_by="sb_platform_admin_v1",
            requested_at=datetime.now(UTC),
            idempotency_digest=_digest(f"catalog-refresh:{label}"),
        )
    )
    capability = f"catalog-capability-{label}-{uuid4().hex}"
    indexer_id = f"catalog:indexer-{label}"
    refreshes = PostgresCatalogRefreshStore(urls.catalog)
    claimed = refreshes.claim_next(
        indexer_id=indexer_id,
        lease_capability=capability,
        lease_duration=timedelta(minutes=2),
    )
    assert claimed is not None
    assert claimed.refresh_id == requested.refresh.refresh_id
    assert claimed.lease is not None
    route = PostgresCatalogConnectorRouteReader(urls.catalog).load_route(
        workspace_id,
        catalog_connection_id,
        refresh_id=requested.refresh.refresh_id,
        indexer_id=indexer_id,
        lease_capability=capability,
        fencing_token=claimed.lease.fencing_token,
    )
    assert route is not None
    assert route.route.target_fingerprint == target.fingerprint
    staging = refreshes.begin_staging(
        workspace_id,
        requested.refresh.refresh_id,
        indexer_id=indexer_id,
        lease_capability=capability,
        fencing_token=claimed.lease.fencing_token,
    )
    refreshes.persist_page(
        workspace_id,
        requested.refresh.refresh_id,
        indexer_id=indexer_id,
        lease_capability=capability,
        fencing_token=claimed.lease.fencing_token,
        page=CatalogSourcePage.create(
            mode=CatalogRefreshMode.FULL,
            sequence=1,
            changes=(),
            next_checkpoint=None,
            source_complete=True,
        ),
    )
    completed = refreshes.complete(
        workspace_id,
        requested.refresh.refresh_id,
        indexer_id=indexer_id,
        lease_capability=capability,
        fencing_token=claimed.lease.fencing_token,
        expected_base_generation=staging.base_generation,
        expected_contract_version=contract_version,
        expected_route_revision=target.route_revision,
        expected_target_fingerprint=target.fingerprint,
    )
    assert completed.status.value == "completed"


def _insert_workflow_grant(dsn: str) -> tuple[str, str]:
    workflow_id = "workflow-m28-routing"
    owner_actor_id = "actor_m28_routing"
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.agent_workflow_drafts (
                workspace_id, id, revision, payload, updated_at
            ) VALUES (%s, %s, 1, '{}'::jsonb, clock_timestamp())
            """,
            (WORKSPACE_ID, workflow_id),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.workflow_access_grants (
                workspace_id, workflow_id, owner_actor_id, created_at
            ) VALUES (%s, %s, %s, clock_timestamp())
            """,
            (WORKSPACE_ID, workflow_id, owner_actor_id),
        )
    return workflow_id, owner_actor_id


def _submit_execution_job_without_contract_version(
    dsn: str,
    *,
    target: GovernedExecutionTarget,
    workflow_id: str,
    owner_actor_id: str,
    label: str,
) -> int:
    now = datetime.now(UTC)
    with psycopg.connect(dsn) as connection:
        row = connection.execute(
            """
            INSERT INTO schemabridge_control.execution_jobs (
                job_id,
                kind,
                workspace_id,
                workflow_workspace_id,
                workflow_id,
                workflow_owner_actor_id,
                submitting_actor_id,
                workflow_access_scope,
                authorized_operation,
                expected_workflow_revision,
                expected_plan_fingerprint,
                authenticated_at,
                authorized_at,
                authorization_expires_at,
                payload_fingerprint,
                request_fingerprint,
                idempotency_digest,
                status,
                attempt_count,
                max_attempts,
                available_at,
                fencing_token,
                created_at,
                updated_at,
                connector_workspace_id,
                connector_connection_id,
                connector_route_revision,
                connector_route_fingerprint,
                connector_target_fingerprint
            ) VALUES (
                %s, 'execute_workflow_preview',
                %s, %s, %s, %s, %s, 'owner',
                'execute_workflow_preview', 1, %s, %s, %s, %s,
                %s, %s, %s, 'queued', 0, 3, %s, 0, %s, %s,
                %s, %s, %s, %s, %s
            )
            RETURNING connector_contract_version
            """,
            (
                f"job-m28-{label}",
                WORKSPACE_ID,
                WORKSPACE_ID,
                workflow_id,
                owner_actor_id,
                owner_actor_id,
                _digest(f"job-plan:{label}"),
                now - timedelta(minutes=2),
                now - timedelta(minutes=1),
                now + timedelta(minutes=5),
                _digest(f"job-payload:{label}"),
                _digest(f"job-request:{label}"),
                _digest(f"job-idempotency:{label}"),
                now,
                now,
                now,
                target.workspace_id,
                CONNECTION_ID,
                target.route_revision,
                target.route_fingerprint,
                target.fingerprint,
            ),
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_rotated_oidc_job_resolves_exact_historical_connector_workspace() -> None:
    database = f"schemabridge_connector_rotation_{uuid4().hex[:12]}"
    urls = _create_database(database)
    try:
        migrated = PostgresControlPlaneMigrator(urls.migrator, MIGRATIONS).migrate()
        assert migrated.inspection.current_version == 9

        observed_at = datetime.now(UTC) - timedelta(minutes=2)
        pair = _oidc_pair(
            f"m28-connector-rotation-{uuid4().hex}",
            observed_at=observed_at,
        )
        for workspace_id in (
            pair.previous.workspace_id,
            pair.current.workspace_id,
        ):
            _insert_capacity_policy(urls.migrator, workspace_id)
        _initialize_and_rotate_identity(
            urls,
            pair,
            approved_at=observed_at + timedelta(seconds=2),
        )

        resolver = PostgresIdentityBindingResolver(
            urls.api,
            application_name="schemabridge-control-api",
        )
        scopes = resolver.resolve_authorization_scopes(
            pair.current.workspace_id,
            pair.current.actor_id,
        )
        assert {(scope.workspace_id, scope.actor_id) for scope in scopes} == {
            (pair.previous.workspace_id, pair.previous.actor_id),
            (pair.current.workspace_id, pair.current.actor_id),
        }

        connection_id = f"connection-m28-rotation-{uuid4().hex[:10]}"
        label = f"rotation-{uuid4().hex[:10]}"
        _insert_catalog_connection(
            urls.api,
            workspace_id=pair.previous.workspace_id,
            connection_id=connection_id,
        )
        target = _target(
            1,
            workspace_id=pair.previous.workspace_id,
            connection_id=connection_id,
        )
        _apply_route(
            urls.migrator,
            _route_change_args(
                operation="create",
                expected_head_revision=0,
                target=target,
                label=label,
                workspace_id=pair.previous.workspace_id,
                connection_id=connection_id,
            ),
        )
        _complete_empty_full_refresh(
            urls,
            workspace_id=pair.previous.workspace_id,
            connection_id=connection_id,
            target=target,
            contract_version=1,
            label=label,
        )

        workflow_id = f"workflow-m28-rotation-{uuid4().hex[:10]}"
        with psycopg.connect(urls.migrator) as connection:
            connection.execute(
                """
                INSERT INTO schemabridge_control.agent_workflow_drafts (
                    workspace_id, id, revision, payload, updated_at
                ) VALUES (%s, %s, 1, '{}'::jsonb, clock_timestamp())
                """,
                (pair.previous.workspace_id, workflow_id),
            )
            connection.execute(
                """
                INSERT INTO schemabridge_control.workflow_access_grants (
                    workspace_id, workflow_id, owner_actor_id, created_at
                ) VALUES (%s, %s, %s, clock_timestamp())
                """,
                (
                    pair.previous.workspace_id,
                    workflow_id,
                    pair.previous.actor_id,
                ),
            )

        now = datetime.now(UTC)
        target_ref = JobExecutionTargetRef.from_target(target)
        authorization = JobAuthorization.create(
            workspace_id=pair.current.workspace_id,
            workflow_workspace_id=pair.previous.workspace_id,
            workflow_id=workflow_id,
            workflow_owner_actor_id=pair.previous.actor_id,
            submitting_actor_id=pair.current.actor_id,
            workflow_access_scope=JobWorkflowAccessScope.OWNER,
            expected_workflow_revision=1,
            expected_plan_fingerprint=_digest(f"plan:{label}"),
            execution_target=target_ref,
            authenticated_at=now - timedelta(minutes=1),
            authorized_at=now,
            expires_at=now + timedelta(minutes=10),
        )
        candidate = BackgroundJob.create(
            id=f"job-m28-rotation-{uuid4().hex[:10]}",
            authorization=authorization,
            idempotency_digest=_digest(f"idempotency:{label}"),
            max_attempts=3,
            created_at=now,
        )
        api_jobs = IdentityResolvingBackgroundJobApiStore(
            PostgresBackgroundJobStore(
                urls.api,
                application_name="schemabridge-control-api",
            ),
            resolver,
        )
        submitted = api_jobs.submit(candidate)
        assert submitted.job.authorization.workspace_id == pair.current.workspace_id
        assert (
            submitted.job.authorization.execution_target is not None
            and submitted.job.authorization.execution_target.workspace_id
            == pair.previous.workspace_id
        )
        assert submitted.job.connector_contract_version == 1

        worker_id = "worker-m28-rotation"
        lease_capability = "worker-m28-rotation-capability-" + ("x" * 48)
        claimed = PostgresBackgroundJobStore(
            urls.worker,
            application_name="schemabridge-control-worker",
        ).claim_next(
            worker_id=worker_id,
            lease_token=lease_capability,
            lease_duration=timedelta(minutes=2),
        )
        assert claimed is not None
        assert claimed.id == submitted.job.id
        assert claimed.lease is not None
        route_context = WorkerExecutionRouteContext.from_claim(
            claimed,
            worker_id=worker_id,
            lease_capability=lease_capability,
        )
        assert route_context.job_workspace_id == pair.current.workspace_id
        assert route_context.connector_workspace_id == pair.previous.workspace_id

        reference = PostgresExecutionConnectorRouteReader(
            urls.worker,
        ).load_secret_reference(
            ExecutionConnectorLeaseContext(
                job_workspace_id=route_context.job_workspace_id,
                connector_workspace_id=route_context.connector_workspace_id,
                job_id=route_context.job_id,
                worker_id=route_context.worker_id,
                lease_capability=lease_capability,
                fencing_token=route_context.fencing_token,
                connection_id=route_context.execution_target.connection_id,
                contract_version=route_context.connector_contract_version,
                route_revision=route_context.execution_target.route_revision,
                target_fingerprint=route_context.execution_target.target_fingerprint,
            ),
            target,
        )
        assert reference.value == f"vault:execution:{label}"

        substitute = target_ref.model_copy(update={"workspace_id": pair.current.workspace_id})
        with pytest.raises(ValueError, match="immutable workflow workspace"):
            JobAuthorization.create(
                workspace_id=pair.current.workspace_id,
                workflow_workspace_id=pair.previous.workspace_id,
                workflow_id=workflow_id,
                workflow_owner_actor_id=pair.previous.actor_id,
                submitting_actor_id=pair.current.actor_id,
                workflow_access_scope=JobWorkflowAccessScope.OWNER,
                expected_workflow_revision=1,
                expected_plan_fingerprint=_digest(f"plan:{label}"),
                execution_target=substitute,
                authenticated_at=now - timedelta(minutes=1),
                authorized_at=now,
                expires_at=now + timedelta(minutes=10),
            )

        with (
            pytest.raises(psycopg.Error) as substitution,
            psycopg.connect(urls.migrator) as connection,
        ):
            connection.execute(
                """
                UPDATE schemabridge_control.execution_jobs
                SET connector_workspace_id = %s
                WHERE job_id = %s
                """,
                (pair.current.workspace_id, claimed.id),
            )
        assert substitution.value.sqlstate == "55000"
    finally:
        _drop_database(database)


def test_pristine_schema_applies_versions_one_through_nine() -> None:
    database = f"schemabridge_connector_pristine_{uuid4().hex[:12]}"
    urls = _create_database(database)
    try:
        migrated = PostgresControlPlaneMigrator(urls.migrator, MIGRATIONS).migrate()
        assert migrated.applied_versions == (1, 2, 3, 4, 5, 6, 7, 8, 9)
        assert migrated.inspection.current_version == 9
        with psycopg.connect(urls.migrator) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT tablename
                    FROM pg_catalog.pg_tables
                    WHERE schemaname = 'schemabridge_control'
                      AND tablename LIKE 'connector_%'
                    """
                )
            }
        assert {
            "connector_contract_revisions",
            "connector_route_revisions",
            "connector_private_route_revisions",
            "connector_route_heads",
            "connector_route_audit",
        } <= tables
    finally:
        _drop_database(database)


def test_sql_fingerprints_match_the_domain_contract(
    connector_database: _DatabaseUrls,
) -> None:
    budget = _budget()
    target = _target(1)
    with psycopg.connect(connector_database.migrator) as connection:
        row = connection.execute(
            """
            SELECT
                schemabridge_control.connector_cost_budget_fingerprint(
                    %s, %s, %s, %s, %s, %s, %s
                ),
                schemabridge_control.connector_target_fingerprint(
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
            """,
            (
                budget.explain_timeout_ms,
                budget.max_response_bytes,
                budget.max_total_cost,
                budget.max_estimated_rows,
                budget.max_plan_nodes,
                budget.max_plan_depth,
                budget.max_plan_width,
                WORKSPACE_ID,
                CONNECTION_ID,
                target.route_revision,
                target.route_fingerprint,
                target.expected_reader,
                target.type_contract_fingerprint,
                target.source_identity_fingerprint,
                target.catalog_identity_fingerprint,
                budget.explain_timeout_ms,
                budget.max_response_bytes,
                budget.max_total_cost,
                budget.max_estimated_rows,
                budget.max_plan_nodes,
                budget.max_plan_depth,
                budget.max_plan_width,
                budget.fingerprint,
            ),
        ).fetchone()
    assert row is not None
    assert str(row[0]).strip() == budget.fingerprint
    assert str(row[1]).strip() == target.fingerprint


def test_sql_native_type_resolver_matches_the_executable_domain_contract(
    connector_database: _DatabaseUrls,
) -> None:
    native_types = (
        None,
        "character varying ( 200 )",
        "pg_catalog.int8",
        "smallserial",
        "NUMERIC(38, 0)",
        "money",
        "double precision",
        "float(24)",
        "BOOL",
        "date",
        "TIMESTAMP(6) WITH TIME ZONE",
        "timestamp without time zone",
        "timestamptz",
        "uuid",
        "bytea",
        "bit varying(64)",
        "json",
        "jsonb",
        "hstore",
        "integer[]",
        "character varying(20)[][]",
        "_int4",
        "ARRAY",
        "USER-DEFINED",
        "time with time zone",
        "int4range",
        "customer_identifier",
    )
    rejected_unbounded_text = (
        "",
        " varchar",
        "varchar ",
        "varchar\nDROP TYPE",
        "x" * 201,
        "😀" * 101,
    )

    with psycopg.connect(connector_database.migrator) as connection:
        for native_type in native_types:
            normalization = normalize_postgres_native_type(native_type)
            expected = normalization.normalized_type.value if normalization.executable else None
            row = connection.execute(
                """
                SELECT schemabridge_control.resolve_query_studio_physical_type(
                    NULL, %s
                )
                """,
                (native_type,),
            ).fetchone()
            assert row == (expected,), native_type

        for native_type in rejected_unbounded_text:
            row = connection.execute(
                """
                SELECT schemabridge_control.resolve_query_studio_physical_type(
                    NULL, %s
                )
                """,
                (native_type,),
            ).fetchone()
            assert row == (None,), native_type

        for approved_type, native_type in (
            ("string", None),
            ("string", "customer_identifier"),
            ("string", "integer[]"),
            ("integer", "jsonb"),
            ("struct", "bigint"),
            ("array", "bigint"),
            ("unknown", "bigint"),
        ):
            row = connection.execute(
                """
                SELECT schemabridge_control.resolve_query_studio_physical_type(
                    %s, %s
                )
                """,
                (approved_type, native_type),
            ).fetchone()
            assert row == (None,), (approved_type, native_type)

        approved_scalar = connection.execute(
            """
            SELECT schemabridge_control.resolve_query_studio_physical_type(
                'string', 'bigint'
            )
            """
        ).fetchone()
    assert approved_scalar == ("string",)


def test_unsupported_type_contract_is_rejected_before_route_mutation(
    connector_database: _DatabaseUrls,
) -> None:
    workspace_id = "workspace-m28-type-contract"
    connection_id = "connection-m28-type-contract"
    _insert_capacity_policy(connector_database.migrator, workspace_id)
    _insert_catalog_connection(
        connector_database.api,
        workspace_id=workspace_id,
        connection_id=connection_id,
    )
    target = _target(
        1,
        workspace_id=workspace_id,
        connection_id=connection_id,
    )

    for label, version, fingerprint in (
        ("unsupported-version", 2, target.type_contract_fingerprint),
        ("unsupported-fingerprint", 1, "f" * 64),
    ):
        arguments = _route_change_args(
            operation="create",
            expected_head_revision=0,
            target=target,
            label=label,
            workspace_id=workspace_id,
            connection_id=connection_id,
            type_contract_version=version,
            type_contract_fingerprint=fingerprint,
        )
        with pytest.raises(psycopg.Error) as rejected:
            _apply_route(connector_database.migrator, arguments)
        assert rejected.value.sqlstate == "22023"

    with psycopg.connect(connector_database.migrator) as connection:
        counts = connection.execute(
            """
            SELECT
                (
                    SELECT count(*)
                    FROM schemabridge_control.connector_contract_revisions
                    WHERE workspace_id = %s AND connection_id = %s
                ),
                (
                    SELECT count(*)
                    FROM schemabridge_control.connector_route_revisions
                    WHERE workspace_id = %s AND connection_id = %s
                ),
                (
                    SELECT count(*)
                    FROM schemabridge_control.connector_private_route_revisions
                    WHERE workspace_id = %s AND connection_id = %s
                ),
                (
                    SELECT count(*)
                    FROM schemabridge_control.connector_route_heads
                    WHERE workspace_id = %s AND connection_id = %s
                ),
                (
                    SELECT count(*)
                    FROM schemabridge_control.connector_route_audit
                    WHERE workspace_id = %s AND connection_id = %s
                )
            """,
            (workspace_id, connection_id) * 5,
        ).fetchone()
    assert counts == (0, 0, 0, 0, 0)


def test_create_replay_rotate_stale_cas_disable_and_public_disabled_state(
    connector_database: _DatabaseUrls,
) -> None:
    _insert_capacity_policy(connector_database.migrator, WORKSPACE_ID)
    _insert_catalog_connection(connector_database.api)
    workflow_id, owner_actor_id = _insert_workflow_grant(connector_database.migrator)
    first_target = _target(1)
    second_target = _target(2)

    create_args = _route_change_args(
        operation="create",
        expected_head_revision=0,
        target=first_target,
        label="create",
    )
    created = _apply_route(connector_database.migrator, create_args)
    replayed = _apply_route(connector_database.migrator, create_args)
    assert created == replayed
    assert created[2:8] == (
        1,
        1,
        1,
        first_target.route_fingerprint,
        first_target.fingerprint,
        "enabled",
    )
    with psycopg.connect(connector_database.runtime) as connection:
        preflight_route = connection.execute(
            """
            SELECT
                workspace_id,
                connection_id,
                route_revision,
                target_fingerprint,
                sql_dialect,
                expected_reader,
                credential_binding_ref
            FROM schemabridge_control.load_current_preflight_connector_route(
                %s, %s, %s, %s
            )
            """,
            (
                WORKSPACE_ID,
                CONNECTION_ID,
                first_target.route_revision,
                first_target.fingerprint,
            ),
        ).fetchone()
    assert preflight_route is None
    with psycopg.connect(connector_database.migrator) as connection:
        migrator_preflight_route = connection.execute(
            """
            SELECT
                workspace_id,
                connection_id,
                route_revision,
                target_fingerprint,
                sql_dialect,
                expected_reader,
                credential_binding_ref
            FROM schemabridge_control.load_current_preflight_connector_route(
                %s, %s, %s, %s
            )
            """,
            (
                WORKSPACE_ID,
                CONNECTION_ID,
                first_target.route_revision,
                first_target.fingerprint,
            ),
        ).fetchone()
    assert migrator_preflight_route == (
        WORKSPACE_ID,
        CONNECTION_ID,
        1,
        first_target.fingerprint,
        "postgresql",
        EXPECTED_READER,
        "vault:preflight:create",
    )

    catalog_capability = f"catalog-v9-route-capability-{uuid4().hex}"
    catalog_indexer_id = "catalog:indexer-m28-route"
    requested_refresh = PostgresCatalogRefreshStore(
        connector_database.api,
        application_name="schemabridge-control-api",
    ).request(
        CatalogRefreshCommand(
            workspace_id=WORKSPACE_ID,
            connection_id=CatalogConnectionId(CONNECTION_ID),
            mode=CatalogRefreshMode.FULL,
            requested_by="sb_platform_admin_v1",
            requested_at=datetime.now(UTC),
            idempotency_digest=_digest("catalog-v9-route-refresh"),
        )
    )
    catalog_refreshes = PostgresCatalogRefreshStore(connector_database.catalog)
    claimed_refresh = catalog_refreshes.claim_next(
        indexer_id=catalog_indexer_id,
        lease_capability=catalog_capability,
        lease_duration=timedelta(minutes=2),
    )
    assert claimed_refresh is not None
    assert claimed_refresh.refresh_id == requested_refresh.refresh.refresh_id
    assert claimed_refresh.lease is not None
    catalog_route = PostgresCatalogConnectorRouteReader(connector_database.catalog).load_route(
        WORKSPACE_ID,
        CatalogConnectionId(CONNECTION_ID),
        refresh_id=requested_refresh.refresh.refresh_id,
        indexer_id=catalog_indexer_id,
        lease_capability=catalog_capability,
        fencing_token=claimed_refresh.lease.fencing_token,
    )
    assert catalog_route is not None
    assert catalog_route.route.kind is CatalogConnectionKind.SYNTHETIC
    assert catalog_route.route.platform_instance == "m28-instance"
    assert catalog_route.route.contract_version == 1
    assert catalog_route.route.route_revision == first_target.route_revision
    assert catalog_route.route.target_fingerprint == first_target.fingerprint
    assert catalog_route.credential_binding_ref == "vault:catalog:create"
    assert "vault:catalog:create" not in repr(catalog_route)
    assert catalog_capability not in repr(catalog_route)
    staged_refresh = catalog_refreshes.begin_staging(
        WORKSPACE_ID,
        requested_refresh.refresh.refresh_id,
        indexer_id=catalog_indexer_id,
        lease_capability=catalog_capability,
        fencing_token=claimed_refresh.lease.fencing_token,
    )
    catalog_refreshes.persist_page(
        WORKSPACE_ID,
        requested_refresh.refresh.refresh_id,
        indexer_id=catalog_indexer_id,
        lease_capability=catalog_capability,
        fencing_token=claimed_refresh.lease.fencing_token,
        page=CatalogSourcePage.create(
            mode=CatalogRefreshMode.FULL,
            sequence=1,
            changes=(),
            next_checkpoint=None,
            source_complete=True,
        ),
    )
    completed_refresh = catalog_refreshes.complete(
        WORKSPACE_ID,
        requested_refresh.refresh.refresh_id,
        indexer_id=catalog_indexer_id,
        lease_capability=catalog_capability,
        fencing_token=claimed_refresh.lease.fencing_token,
        expected_base_generation=staged_refresh.base_generation,
        expected_contract_version=1,
        expected_route_revision=first_target.route_revision,
        expected_target_fingerprint=first_target.fingerprint,
    )
    assert completed_refresh.target_generation == 1
    with psycopg.connect(connector_database.runtime) as connection:
        active_preflight_route = connection.execute(
            """
            SELECT
                workspace_id,
                connection_id,
                route_revision,
                target_fingerprint,
                sql_dialect,
                expected_reader,
                credential_binding_ref
            FROM schemabridge_control.load_current_preflight_connector_route(
                %s, %s, %s, %s
            )
            """,
            (
                WORKSPACE_ID,
                CONNECTION_ID,
                first_target.route_revision,
                first_target.fingerprint,
            ),
        ).fetchone()
    assert active_preflight_route == migrator_preflight_route

    with (
        psycopg.connect(connector_database.worker) as connection,
        pytest.raises(psycopg.Error) as wrong_preflight_role,
    ):
        connection.execute(
            """
            SELECT *
            FROM schemabridge_control.load_current_preflight_connector_route(
                %s, %s, %s, %s
            )
            """,
            (
                WORKSPACE_ID,
                CONNECTION_ID,
                first_target.route_revision,
                first_target.fingerprint,
            ),
        )
    assert wrong_preflight_role.value.sqlstate == "42501"
    assert (
        _submit_execution_job_without_contract_version(
            connector_database.api,
            target=first_target,
            workflow_id=workflow_id,
            owner_actor_id=owner_actor_id,
            label="current-target",
        )
        == 1
    )

    changed_replay = list(create_args)
    changed_replay[33] = _digest("different-replay-head")
    with pytest.raises(psycopg.Error) as replay_conflict:
        _apply_route(connector_database.migrator, tuple(changed_replay))
    assert replay_conflict.value.sqlstate == "23505"

    stale_capability = f"catalog-v9-stale-capability-{uuid4().hex}"
    stale_indexer_id = "catalog:indexer-m28-stale"
    stale_requested = PostgresCatalogRefreshStore(
        connector_database.api,
        application_name="schemabridge-control-api",
    ).request(
        CatalogRefreshCommand(
            workspace_id=WORKSPACE_ID,
            connection_id=CatalogConnectionId(CONNECTION_ID),
            mode=CatalogRefreshMode.FULL,
            requested_by="sb_platform_admin_v1",
            requested_at=datetime.now(UTC),
            idempotency_digest=_digest("catalog-v9-stale-route-refresh"),
        )
    )
    stale_claimed = catalog_refreshes.claim_next(
        indexer_id=stale_indexer_id,
        lease_capability=stale_capability,
        lease_duration=timedelta(minutes=5),
    )
    assert stale_claimed is not None
    assert stale_claimed.lease is not None
    stale_route = PostgresCatalogConnectorRouteReader(connector_database.catalog).load_route(
        WORKSPACE_ID,
        CatalogConnectionId(CONNECTION_ID),
        refresh_id=stale_requested.refresh.refresh_id,
        indexer_id=stale_indexer_id,
        lease_capability=stale_capability,
        fencing_token=stale_claimed.lease.fencing_token,
    )
    assert stale_route is not None
    stale_staging = catalog_refreshes.begin_staging(
        WORKSPACE_ID,
        stale_requested.refresh.refresh_id,
        indexer_id=stale_indexer_id,
        lease_capability=stale_capability,
        fencing_token=stale_claimed.lease.fencing_token,
    )
    catalog_refreshes.persist_page(
        WORKSPACE_ID,
        stale_requested.refresh.refresh_id,
        indexer_id=stale_indexer_id,
        lease_capability=stale_capability,
        fencing_token=stale_claimed.lease.fencing_token,
        page=CatalogSourcePage.create(
            mode=CatalogRefreshMode.FULL,
            sequence=1,
            changes=(),
            next_checkpoint=None,
            source_complete=True,
        ),
    )
    activate_for_target_sql = (
        "SELECT * FROM schemabridge_control.activate_catalog_generation_for_target("
        + ", ".join(["%s"] * 12)
        + ")"
    )
    with (
        psycopg.connect(connector_database.catalog) as connection,
        pytest.raises(psycopg.Error) as stolen_digest,
    ):
        connection.execute(
            activate_for_target_sql,
            (
                WORKSPACE_ID,
                CONNECTION_ID,
                stale_requested.refresh.refresh_id.root,
                stale_indexer_id,
                "wrong-catalog-capability-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                stale_staging.base_generation,
                stale_staging.target_generation,
                stale_claimed.lease.fencing_token,
                "0" * 64,
                1,
                first_target.route_revision,
                first_target.fingerprint,
            ),
        )
    assert stolen_digest.value.sqlstate == "55000"
    for invalid_index, invalid_value in ((4, None), (11, None)):
        invalid_arguments = [
            WORKSPACE_ID,
            CONNECTION_ID,
            stale_requested.refresh.refresh_id.root,
            stale_indexer_id,
            stale_capability,
            stale_staging.base_generation,
            stale_staging.target_generation,
            stale_claimed.lease.fencing_token,
            "0" * 64,
            1,
            first_target.route_revision,
            first_target.fingerprint,
        ]
        invalid_arguments[invalid_index] = invalid_value
        with (
            psycopg.connect(connector_database.catalog) as connection,
            pytest.raises(psycopg.Error) as null_capability_or_target,
        ):
            connection.execute(activate_for_target_sql, tuple(invalid_arguments))
        assert null_capability_or_target.value.sqlstate == "22023"

    rotate_args = _route_change_args(
        operation="rotate",
        expected_head_revision=1,
        target=second_target,
        label="rotate",
    )
    rotated = _apply_route(connector_database.migrator, rotate_args)
    assert rotated[2:8] == (
        2,
        1,
        2,
        second_target.route_fingerprint,
        second_target.fingerprint,
        "enabled",
    )
    with pytest.raises(CatalogInventoryError) as stale_catalog_promotion:
        catalog_refreshes.complete(
            WORKSPACE_ID,
            stale_requested.refresh.refresh_id,
            indexer_id=stale_indexer_id,
            lease_capability=stale_capability,
            fencing_token=stale_claimed.lease.fencing_token,
            expected_base_generation=stale_staging.base_generation,
            expected_contract_version=1,
            expected_route_revision=first_target.route_revision,
            expected_target_fingerprint=first_target.fingerprint,
        )
    assert stale_catalog_promotion.value.code is CatalogInventoryErrorCode.LEASE_CONFLICT
    catalog_refreshes.fail(
        WORKSPACE_ID,
        stale_requested.refresh.refresh_id,
        indexer_id=stale_indexer_id,
        lease_capability=stale_capability,
        fencing_token=stale_claimed.lease.fencing_token,
        code=CatalogRefreshFailureCode.FINGERPRINT_MISMATCH,
    )
    with pytest.raises(psycopg.Error) as stale_submission:
        _submit_execution_job_without_contract_version(
            connector_database.api,
            target=first_target,
            workflow_id=workflow_id,
            owner_actor_id=owner_actor_id,
            label="stale-target",
        )
    assert stale_submission.value.sqlstate == "55000"
    assert (
        str(stale_submission.value).splitlines()[0]
        == "execution job connector target is not current"
    )

    stale_target = _target(3)
    stale_args = _route_change_args(
        operation="rotate",
        expected_head_revision=1,
        target=stale_target,
        label="stale",
    )
    with pytest.raises(psycopg.Error) as stale:
        _apply_route(connector_database.migrator, stale_args)
    assert stale.value.sqlstate == "40001"

    disable_args = _route_change_args(
        operation="disable",
        expected_head_revision=2,
        target=second_target,
        label="disable",
    )
    disabled = _apply_route(connector_database.migrator, disable_args)
    assert disabled[2:8] == (
        3,
        1,
        2,
        second_target.route_fingerprint,
        second_target.fingerprint,
        "disabled",
    )
    with pytest.raises(psycopg.Error) as disabled_submission:
        _submit_execution_job_without_contract_version(
            connector_database.api,
            target=second_target,
            workflow_id=workflow_id,
            owner_actor_id=owner_actor_id,
            label="disabled-target",
        )
    assert disabled_submission.value.sqlstate == "55000"
    assert (
        str(disabled_submission.value).splitlines()[0]
        == "execution job connector target is not current"
    )

    with psycopg.connect(connector_database.runtime) as connection:
        public_disabled_route = connection.execute(
            """
            SELECT route_status, connection_status, head_revision,
                   route_revision, target_fingerprint
            FROM schemabridge_control.load_current_connector_target(%s, %s)
            """,
            (WORKSPACE_ID, CONNECTION_ID),
        ).fetchone()
    assert public_disabled_route == (
        "disabled",
        "enabled",
        3,
        2,
        second_target.fingerprint,
    )

    with psycopg.connect(connector_database.api) as connection:
        connection.execute(
            """
            UPDATE schemabridge_control.catalog_connections
            SET status = 'disabled',
                disabled_by_actor_id = 'sb_platform_admin_v1',
                disabled_idempotency_digest = %s,
                disabled_at = clock_timestamp(),
                updated_at = clock_timestamp()
            WHERE workspace_id = %s
              AND connection_id = %s
            """,
            (_digest("disable-connection"), WORKSPACE_ID, CONNECTION_ID),
        )
    with psycopg.connect(connector_database.runtime) as connection:
        fully_disabled = connection.execute(
            """
            SELECT route_status, connection_status
            FROM schemabridge_control.load_current_connector_target(%s, %s)
            """,
            (WORKSPACE_ID, CONNECTION_ID),
        ).fetchone()
    assert fully_disabled == ("disabled", "disabled")

    with psycopg.connect(connector_database.migrator) as connection:
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM
                    schemabridge_control.connector_contract_revisions),
                (SELECT count(*) FROM
                    schemabridge_control.connector_route_revisions),
                (SELECT count(*) FROM
                    schemabridge_control.connector_private_route_revisions),
                (SELECT count(*) FROM
                    schemabridge_control.connector_route_audit)
            """
        ).fetchone()
    assert counts == (1, 2, 8, 3)


@pytest.mark.parametrize(
    "changed_field",
    [
        "source_identity_fingerprint",
        "catalog_identity_fingerprint",
    ],
)
def test_semantic_identity_rotation_requires_a_new_full_generation(
    connector_database: _DatabaseUrls,
    changed_field: str,
) -> None:
    identity_label = changed_field.removesuffix("_identity_fingerprint").removesuffix(
        "_contract_fingerprint"
    )
    workspace_id = f"workspace-m28-{identity_label}-rotation"
    connection_id = f"connection-m28-{identity_label}-rotation"
    _insert_capacity_policy(connector_database.migrator, workspace_id)
    _insert_catalog_connection(
        connector_database.api,
        workspace_id=workspace_id,
        connection_id=connection_id,
    )
    first_target = _target(
        1,
        workspace_id=workspace_id,
        connection_id=connection_id,
    )
    _apply_route(
        connector_database.migrator,
        _route_change_args(
            operation="create",
            expected_head_revision=0,
            target=first_target,
            label=f"{identity_label}-create",
            workspace_id=workspace_id,
            connection_id=connection_id,
        ),
    )
    _complete_empty_full_refresh(
        connector_database,
        workspace_id=workspace_id,
        connection_id=connection_id,
        target=first_target,
        contract_version=1,
        label=f"m28-{identity_label}-first",
    )

    changed_payload = first_target.model_dump()
    changed_payload.update(
        {
            "route_revision": 2,
            "route_fingerprint": _digest(f"{identity_label}:route:2"),
            changed_field: _digest(f"{identity_label}:semantic:2"),
        }
    )
    second_target = GovernedExecutionTarget.model_validate(changed_payload)
    _apply_route(
        connector_database.migrator,
        _route_change_args(
            operation="rotate",
            expected_head_revision=1,
            target=second_target,
            label=f"{identity_label}-rotate",
            workspace_id=workspace_id,
            connection_id=connection_id,
            contract_version=2,
            contract_fingerprint=_digest(f"{identity_label}:contract:2"),
        ),
    )

    with (
        psycopg.connect(connector_database.runtime) as runtime_connection,
        psycopg.connect(connector_database.catalog) as catalog_connection,
    ):
        runtime_target = runtime_connection.execute(
            """
            SELECT target_fingerprint
            FROM schemabridge_control.load_current_connector_target(%s, %s)
            """,
            (workspace_id, connection_id),
        ).fetchone()
        catalog_target = catalog_connection.execute(
            """
            SELECT target_fingerprint
            FROM schemabridge_control.load_current_connector_target(%s, %s)
            """,
            (workspace_id, connection_id),
        ).fetchone()
    assert runtime_target is None
    assert catalog_target == (second_target.fingerprint,)

    _complete_empty_full_refresh(
        connector_database,
        workspace_id=workspace_id,
        connection_id=connection_id,
        target=second_target,
        contract_version=2,
        label=f"m28-{identity_label}-second",
    )
    with psycopg.connect(connector_database.runtime) as connection:
        reopened_target = connection.execute(
            """
            SELECT target_fingerprint
            FROM schemabridge_control.load_current_connector_target(%s, %s)
            """,
            (workspace_id, connection_id),
        ).fetchone()
    assert reopened_target == (second_target.fingerprint,)


def test_catalog_promotion_and_route_rotation_serialize_without_crossing(
    connector_database: _DatabaseUrls,
) -> None:
    workspace_id = "workspace-m28-promotion"
    connection_id = "connection-m28-promotion"
    catalog_connection_id = CatalogConnectionId(connection_id)
    _insert_capacity_policy(connector_database.migrator, workspace_id)
    _insert_catalog_connection(
        connector_database.api,
        workspace_id=workspace_id,
        connection_id=connection_id,
    )
    first_target = _target(
        1,
        workspace_id=workspace_id,
        connection_id=connection_id,
    )
    second_target = _target(
        2,
        workspace_id=workspace_id,
        connection_id=connection_id,
    )
    _apply_route(
        connector_database.migrator,
        _route_change_args(
            operation="create",
            expected_head_revision=0,
            target=first_target,
            label="atomic-create",
            workspace_id=workspace_id,
            connection_id=connection_id,
        ),
    )

    requested = PostgresCatalogRefreshStore(
        connector_database.api,
        application_name="schemabridge-control-api",
    ).request(
        CatalogRefreshCommand(
            workspace_id=workspace_id,
            connection_id=catalog_connection_id,
            mode=CatalogRefreshMode.FULL,
            requested_by="sb_platform_admin_v1",
            requested_at=datetime.now(UTC),
            idempotency_digest=_digest("atomic-promotion-refresh"),
        )
    )
    capability = f"catalog-atomic-promotion-{uuid4().hex}"
    indexer_id = "catalog:indexer-m28-atomic"
    refreshes = PostgresCatalogRefreshStore(connector_database.catalog)
    claimed = refreshes.claim_next(
        indexer_id=indexer_id,
        lease_capability=capability,
        lease_duration=timedelta(minutes=5),
    )
    assert claimed is not None
    assert claimed.lease is not None
    owned_route = PostgresCatalogConnectorRouteReader(connector_database.catalog).load_route(
        workspace_id,
        catalog_connection_id,
        refresh_id=requested.refresh.refresh_id,
        indexer_id=indexer_id,
        lease_capability=capability,
        fencing_token=claimed.lease.fencing_token,
    )
    assert owned_route is not None
    assert owned_route.route.target_fingerprint == first_target.fingerprint
    staging = refreshes.begin_staging(
        workspace_id,
        requested.refresh.refresh_id,
        indexer_id=indexer_id,
        lease_capability=capability,
        fencing_token=claimed.lease.fencing_token,
    )
    refreshes.persist_page(
        workspace_id,
        requested.refresh.refresh_id,
        indexer_id=indexer_id,
        lease_capability=capability,
        fencing_token=claimed.lease.fencing_token,
        page=CatalogSourcePage.create(
            mode=CatalogRefreshMode.FULL,
            sequence=1,
            changes=(),
            next_checkpoint=None,
            source_complete=True,
        ),
    )
    rotation_arguments = _route_change_args(
        operation="rotate",
        expected_head_revision=1,
        target=second_target,
        label="atomic-rotate",
        workspace_id=workspace_id,
        connection_id=connection_id,
    )

    with psycopg.connect(connector_database.migrator) as policy_blocker:
        policy_blocker.execute(
            """
            SELECT workspace_id
            FROM schemabridge_control.tenant_capacity_policies
            WHERE workspace_id = %s
            FOR UPDATE
            """,
            (workspace_id,),
        ).fetchone()
        with ThreadPoolExecutor(max_workers=2) as executor:
            completion_future = executor.submit(
                refreshes.complete,
                workspace_id,
                requested.refresh.refresh_id,
                indexer_id=indexer_id,
                lease_capability=capability,
                fencing_token=claimed.lease.fencing_token,
                expected_base_generation=staging.base_generation,
                expected_contract_version=1,
                expected_route_revision=first_target.route_revision,
                expected_target_fingerprint=first_target.fingerprint,
            )
            deadline = monotonic() + 5
            while monotonic() < deadline:
                if completion_future.done():
                    completion_future.result()
                    raise AssertionError("catalog promotion did not wait for the locked policy")
                with psycopg.connect(connector_database.catalog) as observer:
                    waiting = observer.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM pg_catalog.pg_stat_activity
                            WHERE datname = current_database()
                              AND usename = 'schemabridge_catalog'
                              AND state = 'active'
                              AND wait_event_type = 'Lock'
                        )
                        """
                    ).fetchone()
                if waiting == (True,):
                    break
                sleep(0.05)
            else:
                raise AssertionError("catalog promotion did not reach the locked policy")

            rotation_future = executor.submit(
                _apply_route,
                connector_database.migrator,
                rotation_arguments,
            )
            sleep(0.2)
            assert not completion_future.done()
            assert not rotation_future.done()
            policy_blocker.commit()
            completed = completion_future.result(timeout=5)
            rotated = rotation_future.result(timeout=5)

    assert completed.status.value == "completed"
    assert rotated[2:8] == (
        2,
        1,
        2,
        second_target.route_fingerprint,
        second_target.fingerprint,
        "enabled",
    )


def test_six_role_acl_closes_tables_and_separates_connector_loaders(
    connector_database: _DatabaseUrls,
) -> None:
    function_grants = {
        "type_resolver": (
            "schemabridge_control.resolve_query_studio_physical_type(varchar,varchar)"
        ),
        "apply": (
            "schemabridge_control.apply_connector_route_change("
            "varchar,varchar,varchar,bigint,bigint,bigint,char,varchar,"
            "integer,char,char,char,integer,integer,numeric,bigint,integer,integer,"
            "integer,char,char,char,varchar,varchar,varchar,varchar,char,varchar,"
            "char,varchar,char,varchar,char,char,varchar)"
        ),
        "public": ("schemabridge_control.load_current_connector_target(varchar,varchar)"),
        "preflight": (
            "schemabridge_control.load_current_preflight_connector_route("
            "varchar,varchar,bigint,char)"
        ),
        "catalog": (
            "schemabridge_control.load_owned_catalog_connector_route("
            "varchar,varchar,varchar,varchar,varchar,bigint,bigint,bigint,char)"
        ),
        "catalog_activation": (
            "schemabridge_control.activate_catalog_generation_for_target("
            "varchar,varchar,varchar,varchar,varchar,bigint,bigint,bigint,"
            "char,bigint,bigint,char)"
        ),
        "legacy_catalog_activation": (
            "schemabridge_control.activate_catalog_generation("
            "varchar,varchar,varchar,bigint,bigint,bigint,char,char)"
        ),
        "execution": (
            "schemabridge_control.load_owned_execution_connector_route("
            "varchar,varchar,varchar,varchar,varchar,bigint,varchar,bigint,bigint,char)"
        ),
        "profile": (
            "schemabridge_control.load_owned_profile_connector_route("
            "varchar,varchar,varchar,varchar,bigint,varchar,bigint,bigint,char)"
        ),
    }
    expected_grants = {
        "type_resolver": {"schemabridge_migrator"},
        "apply": {"schemabridge_migrator"},
        "public": {
            "schemabridge_migrator",
            "schemabridge_runtime",
            "schemabridge_reconciler",
            "schemabridge_catalog",
        },
        "preflight": {"schemabridge_migrator", "schemabridge_runtime"},
        "catalog": {"schemabridge_migrator", "schemabridge_catalog"},
        "catalog_activation": {"schemabridge_migrator", "schemabridge_catalog"},
        "legacy_catalog_activation": {"schemabridge_migrator"},
        "execution": {"schemabridge_migrator", "schemabridge_worker"},
        "profile": {"schemabridge_migrator", "schemabridge_worker"},
    }

    with psycopg.connect(connector_database.migrator) as connection:
        private_table_acl: dict[str, bool] = {}
        semantic_binding_acl: dict[str, bool] = {}
        for role in CONTROL_ROLES:
            privilege_row = connection.execute(
                """
                SELECT has_table_privilege(
                    %s,
                    'schemabridge_control.connector_private_route_revisions',
                    'SELECT'
                )
                """,
                (role,),
            ).fetchone()
            assert privilege_row is not None
            private_table_acl[role] = bool(privilege_row[0])
            binding_privilege_row = connection.execute(
                """
                SELECT has_table_privilege(
                    %s,
                    'schemabridge_control.catalog_refresh_semantic_bindings',
                    'SELECT'
                )
                """,
                (role,),
            ).fetchone()
            assert binding_privilege_row is not None
            semantic_binding_acl[role] = bool(binding_privilege_row[0])

        observed_grants: dict[str, set[str]] = {}
        for label, signature in function_grants.items():
            granted_roles: set[str] = set()
            for role in CONTROL_ROLES:
                privilege_row = connection.execute(
                    "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                    (role, signature),
                ).fetchone()
                assert privilege_row is not None
                if bool(privilege_row[0]):
                    granted_roles.add(role)
            observed_grants[label] = granted_roles
        activation_owners = {
            label: str(
                connection.execute(
                    """
                    SELECT pg_catalog.pg_get_userbyid(procedure.proowner)
                    FROM pg_catalog.pg_proc AS procedure
                    WHERE procedure.oid = pg_catalog.to_regprocedure(%s)
                    """,
                    (function_grants[label],),
                ).fetchone()[0]
            )
            for label in ("legacy_catalog_activation", "catalog_activation")
        }

    assert private_table_acl == {role: role == "schemabridge_migrator" for role in CONTROL_ROLES}
    assert semantic_binding_acl == {role: role == "schemabridge_migrator" for role in CONTROL_ROLES}
    assert observed_grants == expected_grants
    assert activation_owners == {
        "legacy_catalog_activation": "schemabridge_migrator",
        "catalog_activation": "schemabridge_migrator",
    }

    with (
        psycopg.connect(connector_database.catalog) as connection,
        pytest.raises(psycopg.Error) as private_read,
    ):
        connection.execute(
            """
            SELECT credential_binding_ref
            FROM schemabridge_control.connector_private_route_revisions
            LIMIT 1
            """
        )
    assert private_read.value.sqlstate == "42501"

    with (
        psycopg.connect(connector_database.catalog) as connection,
        pytest.raises(psycopg.Error) as binding_read,
    ):
        connection.execute(
            """
            SELECT source_identity_fingerprint
            FROM schemabridge_control.catalog_refresh_semantic_bindings
            LIMIT 1
            """
        )
    assert binding_read.value.sqlstate == "42501"

    with (
        psycopg.connect(connector_database.catalog) as connection,
        pytest.raises(psycopg.Error) as legacy_activation,
    ):
        connection.execute(
            """
            SELECT *
            FROM schemabridge_control.activate_catalog_generation(
                %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                WORKSPACE_ID,
                CONNECTION_ID,
                "refresh-m28-acl",
                None,
                1,
                1,
                _digest("acl-capability"),
                _digest("acl-inventory"),
            ),
        )
    assert legacy_activation.value.sqlstate == "42501"


def test_api_cannot_submit_an_execution_job_without_an_exact_target(
    connector_database: _DatabaseUrls,
) -> None:
    with (
        psycopg.connect(connector_database.api) as connection,
        pytest.raises(psycopg.Error) as missing_target,
    ):
        connection.execute(
            "INSERT INTO schemabridge_control.execution_jobs (job_id) VALUES (%s)",
            (f"job-m28-{uuid4().hex[:12]}",),
        )
    assert missing_target.value.sqlstate == "55000"
    assert (
        str(missing_target.value).splitlines()[0]
        == "new execution job connector target is required"
    )


def _seed_legacy_queued_job(dsn: str) -> None:
    workspace_id = "workspace-m28-preflight"
    workflow_id = "workflow-m28-preflight"
    owner_actor_id = "actor_m28_preflight"
    now = datetime.now(UTC)
    _insert_capacity_policy(dsn, workspace_id)
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.agent_workflow_drafts (
                workspace_id, id, revision, payload, updated_at
            ) VALUES (%s, %s, 1, '{}'::jsonb, %s)
            """,
            (workspace_id, workflow_id, now),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.workflow_access_grants (
                workspace_id, workflow_id, owner_actor_id, created_at
            ) VALUES (%s, %s, %s, %s)
            """,
            (workspace_id, workflow_id, owner_actor_id, now),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.execution_jobs (
                job_id,
                kind,
                workspace_id,
                workflow_workspace_id,
                workflow_id,
                workflow_owner_actor_id,
                submitting_actor_id,
                workflow_access_scope,
                authorized_operation,
                expected_workflow_revision,
                expected_plan_fingerprint,
                authenticated_at,
                authorized_at,
                authorization_expires_at,
                payload_fingerprint,
                request_fingerprint,
                idempotency_digest,
                status,
                attempt_count,
                max_attempts,
                available_at,
                fencing_token,
                created_at,
                updated_at
            ) VALUES (
                'job-m28-preflight',
                'execute_workflow_preview',
                %s, %s, %s, %s, %s, 'owner',
                'execute_workflow_preview', 1, %s, %s, %s, %s,
                %s, %s, %s, 'queued', 0, 3, %s, 0, %s, %s
            )
            """,
            (
                workspace_id,
                workspace_id,
                workflow_id,
                owner_actor_id,
                owner_actor_id,
                _digest("preflight-plan"),
                now - timedelta(minutes=2),
                now - timedelta(minutes=1),
                now + timedelta(minutes=5),
                _digest("preflight-payload"),
                _digest("preflight-request"),
                _digest("preflight-idempotency"),
                now,
                now,
                now,
            ),
        )


def _finish_legacy_job(dsn: str) -> None:
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """
            UPDATE schemabridge_control.execution_jobs AS job
            SET status = 'leased',
                attempt_count = 1,
                available_at = NULL,
                lease_owner_id = 'worker_m28_preflight',
                lease_token_digest = %s,
                fencing_token = 1,
                lease_acquired_at = observed.at,
                lease_heartbeat_at = observed.at,
                lease_expires_at = observed.at + interval '1 minute',
                updated_at = observed.at
            FROM (SELECT clock_timestamp() AS at) AS observed
            WHERE job.job_id = 'job-m28-preflight'
            """,
            (_digest("preflight-lease"),),
        )
        connection.execute(
            """
            UPDATE schemabridge_control.execution_jobs AS job
            SET status = 'failed',
                lease_owner_id = NULL,
                lease_token_digest = NULL,
                lease_acquired_at = NULL,
                lease_heartbeat_at = NULL,
                lease_expires_at = NULL,
                failure_code = 'plan_stale',
                updated_at = observed.at,
                completed_at = observed.at
            FROM (SELECT clock_timestamp() AS at) AS observed
            WHERE job.job_id = 'job-m28-preflight'
            """
        )


def test_v9_preflight_rejects_nonterminal_legacy_execution_job_atomically(
    tmp_path: Path,
) -> None:
    database = f"schemabridge_connector_preflight_{uuid4().hex[:12]}"
    urls = _create_database(database)
    try:
        v8_migrations = _migration_subset(tmp_path / "control-v8", 8)
        v8 = PostgresControlPlaneMigrator(urls.migrator, v8_migrations).migrate()
        assert v8.inspection.current_version == 8
        _seed_legacy_queued_job(urls.migrator)

        with pytest.raises(ControlPlaneMigrationError):
            PostgresControlPlaneMigrator(urls.migrator, MIGRATIONS).migrate()

        with psycopg.connect(urls.migrator) as connection:
            state = connection.execute(
                """
                SELECT
                    (SELECT max(version)
                     FROM schemabridge_control.schema_migrations),
                    (SELECT count(*)
                     FROM schemabridge_control.schema_migrations),
                    (SELECT status
                     FROM schemabridge_control.execution_jobs
                     WHERE job_id = 'job-m28-preflight')
                """
            ).fetchone()
        assert state == (8, 8, "queued")
    finally:
        _drop_database(database)


def test_v9_preserves_terminal_legacy_job_without_inventing_a_target(
    tmp_path: Path,
) -> None:
    database = f"schemabridge_connector_terminal_{uuid4().hex[:12]}"
    urls = _create_database(database)
    try:
        v8_migrations = _migration_subset(tmp_path / "terminal-control-v8", 8)
        PostgresControlPlaneMigrator(urls.migrator, v8_migrations).migrate()
        _seed_legacy_queued_job(urls.migrator)
        _finish_legacy_job(urls.migrator)

        upgraded = PostgresControlPlaneMigrator(urls.migrator, MIGRATIONS).migrate()
        assert upgraded.applied_versions == (9,)
        with psycopg.connect(urls.migrator) as connection:
            legacy = connection.execute(
                """
                SELECT status,
                       connector_connection_id,
                       connector_contract_version,
                       connector_route_revision,
                       connector_route_fingerprint,
                       connector_target_fingerprint
                FROM schemabridge_control.execution_jobs
                WHERE job_id = 'job-m28-preflight'
                """
            ).fetchone()
        assert legacy == ("failed", None, None, None, None, None)
    finally:
        _drop_database(database)


def test_v8_active_generation_stays_runtime_closed_until_v9_full_refresh(
    tmp_path: Path,
) -> None:
    database = f"schemabridge_connector_catalog_upgrade_{uuid4().hex[:12]}"
    urls = _create_database(database)
    workspace_id = "workspace-m28-catalog-upgrade"
    connection_id = "connection-m28-catalog-upgrade"
    catalog_connection_id = CatalogConnectionId(connection_id)
    database_target = _target(
        1,
        workspace_id=workspace_id,
        connection_id=connection_id,
    )
    try:
        v8_migrations = _migration_subset(tmp_path / "catalog-control-v8", 8)
        migrated_v8 = PostgresControlPlaneMigrator(urls.migrator, v8_migrations).migrate()
        assert migrated_v8.inspection.current_version == 8
        _insert_capacity_policy(urls.migrator, workspace_id)
        _insert_catalog_connection(
            urls.api,
            workspace_id=workspace_id,
            connection_id=connection_id,
        )
        with psycopg.connect(urls.migrator) as connection:
            connection.execute(
                """
                INSERT INTO schemabridge_control.catalog_connection_routes (
                    workspace_id,
                    connection_id,
                    credential_binding_ref,
                    route_fingerprint,
                    created_at
                ) VALUES (%s, %s, %s, %s, clock_timestamp())
                """,
                (
                    workspace_id,
                    connection_id,
                    "binding:v8:synthetic",
                    _digest("v8-catalog-route"),
                ),
            )

        legacy_request = PostgresCatalogRefreshStore(urls.api).request(
            CatalogRefreshCommand(
                workspace_id=workspace_id,
                connection_id=catalog_connection_id,
                mode=CatalogRefreshMode.FULL,
                requested_by="sb_platform_admin_v1",
                requested_at=datetime.now(UTC),
                idempotency_digest=_digest("v8-catalog-refresh"),
            )
        )
        legacy_capability = f"catalog-v8-capability-{uuid4().hex}"
        indexer_id = "catalog:indexer-m28-v8-upgrade"
        legacy_refreshes = PostgresCatalogRefreshStore(urls.catalog)
        legacy_claim = legacy_refreshes.claim_next(
            indexer_id=indexer_id,
            lease_capability=legacy_capability,
            lease_duration=timedelta(minutes=2),
        )
        assert legacy_claim is not None
        assert legacy_claim.lease is not None
        legacy_staging = legacy_refreshes.begin_staging(
            workspace_id,
            legacy_request.refresh.refresh_id,
            indexer_id=indexer_id,
            lease_capability=legacy_capability,
            fencing_token=legacy_claim.lease.fencing_token,
        )
        legacy_refreshes.persist_page(
            workspace_id,
            legacy_request.refresh.refresh_id,
            indexer_id=indexer_id,
            lease_capability=legacy_capability,
            fencing_token=legacy_claim.lease.fencing_token,
            page=CatalogSourcePage.create(
                mode=CatalogRefreshMode.FULL,
                sequence=1,
                changes=(),
                next_checkpoint=None,
                source_complete=True,
            ),
        )
        with psycopg.connect(urls.catalog) as connection:
            activated = connection.execute(
                """
                SELECT *
                FROM schemabridge_control.activate_catalog_generation(
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    workspace_id,
                    connection_id,
                    legacy_request.refresh.refresh_id.root,
                    None,
                    legacy_staging.target_generation,
                    legacy_claim.lease.fencing_token,
                    _digest(legacy_capability),
                    _digest("v8-empty-inventory"),
                ),
            ).fetchone()
        assert activated == (0, 0)

        upgraded = PostgresControlPlaneMigrator(urls.migrator, MIGRATIONS).migrate()
        assert upgraded.applied_versions == (9,)
        _apply_route(
            urls.migrator,
            _route_change_args(
                operation="create",
                expected_head_revision=0,
                target=database_target,
                label="catalog-upgrade",
                workspace_id=workspace_id,
                connection_id=connection_id,
            ),
        )

        with (
            psycopg.connect(urls.runtime) as runtime_connection,
            psycopg.connect(urls.catalog) as catalog_connection,
        ):
            runtime_target = runtime_connection.execute(
                """
                SELECT target_fingerprint
                FROM schemabridge_control.load_current_connector_target(%s, %s)
                """,
                (workspace_id, connection_id),
            ).fetchone()
            catalog_target = catalog_connection.execute(
                """
                SELECT target_fingerprint
                FROM schemabridge_control.load_current_connector_target(%s, %s)
                """,
                (workspace_id, connection_id),
            ).fetchone()
        assert runtime_target is None
        assert catalog_target == (database_target.fingerprint,)

        v9_request = PostgresCatalogRefreshStore(urls.api).request(
            CatalogRefreshCommand(
                workspace_id=workspace_id,
                connection_id=catalog_connection_id,
                mode=CatalogRefreshMode.FULL,
                requested_by="sb_platform_admin_v1",
                requested_at=datetime.now(UTC),
                idempotency_digest=_digest("v9-catalog-refresh"),
            )
        )
        v9_capability = f"catalog-v9-capability-{uuid4().hex}"
        v9_refreshes = PostgresCatalogRefreshStore(urls.catalog)
        v9_claim = v9_refreshes.claim_next(
            indexer_id=indexer_id,
            lease_capability=v9_capability,
            lease_duration=timedelta(minutes=2),
        )
        assert v9_claim is not None
        assert v9_claim.lease is not None
        managed_route = PostgresCatalogConnectorRouteReader(urls.catalog).load_route(
            workspace_id,
            catalog_connection_id,
            refresh_id=v9_request.refresh.refresh_id,
            indexer_id=indexer_id,
            lease_capability=v9_capability,
            fencing_token=v9_claim.lease.fencing_token,
        )
        assert managed_route is not None
        v9_staging = v9_refreshes.begin_staging(
            workspace_id,
            v9_request.refresh.refresh_id,
            indexer_id=indexer_id,
            lease_capability=v9_capability,
            fencing_token=v9_claim.lease.fencing_token,
        )
        v9_refreshes.persist_page(
            workspace_id,
            v9_request.refresh.refresh_id,
            indexer_id=indexer_id,
            lease_capability=v9_capability,
            fencing_token=v9_claim.lease.fencing_token,
            page=CatalogSourcePage.create(
                mode=CatalogRefreshMode.FULL,
                sequence=1,
                changes=(),
                next_checkpoint=None,
                source_complete=True,
            ),
        )
        v9_refreshes.complete(
            workspace_id,
            v9_request.refresh.refresh_id,
            indexer_id=indexer_id,
            lease_capability=v9_capability,
            fencing_token=v9_claim.lease.fencing_token,
            expected_base_generation=v9_staging.base_generation,
            expected_contract_version=1,
            expected_route_revision=database_target.route_revision,
            expected_target_fingerprint=database_target.fingerprint,
        )

        with psycopg.connect(urls.migrator) as connection:
            observed = connection.execute(
                """
                SELECT
                    connection.active_generation,
                    generation.generation,
                    generation.source_identity_fingerprint,
                    generation.catalog_identity_fingerprint,
                    generation.type_contract_fingerprint
                FROM schemabridge_control.catalog_connections AS connection
                JOIN schemabridge_control.catalog_generations AS generation
                  ON generation.workspace_id = connection.workspace_id
                 AND generation.connection_id = connection.connection_id
                WHERE connection.workspace_id = %s
                  AND connection.connection_id = %s
                ORDER BY generation.generation
                """,
                (workspace_id, connection_id),
            ).fetchall()
        assert observed == [
            (2, 1, None, None, None),
            (
                2,
                2,
                database_target.source_identity_fingerprint,
                database_target.catalog_identity_fingerprint,
                database_target.type_contract_fingerprint,
            ),
        ]
        with psycopg.connect(urls.runtime) as connection:
            reopened_target = connection.execute(
                """
                SELECT target_fingerprint
                FROM schemabridge_control.load_current_connector_target(%s, %s)
                """,
                (workspace_id, connection_id),
            ).fetchone()
        assert reopened_target == (database_target.fingerprint,)
    finally:
        _drop_database(database)
