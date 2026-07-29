"""PostgreSQL proof for the reconciler-to-worker aggregate profile queue."""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb
from tests.integration.connector_target_support import (
    CatalogTargetFacts,
    ensure_compatible_catalog_generation,
)

from schemabridge.adapters.connectors.postgres_profile_routing import (
    PostgresSemanticProfileConnectorRouteReader,
)
from schemabridge.adapters.connectors.postgres_routing import (
    PostgresExecutionTargetResolver,
)
from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.adapters.semantic_change.postgres_profile_queue import (
    PostgresSemanticJoinProfileQueue,
    QueuedRelationshipEvidencePort,
)
from schemabridge.application.join_demo import build_north_star_join_proposals
from schemabridge.application.ports.relationships import (
    RelationshipErrorCode,
    RelationshipWorkflowError,
)
from schemabridge.application.ports.semantic_profile_jobs import (
    SemanticJoinProfileQueueError,
    SemanticJoinProfileQueueErrorCode,
    SemanticJoinProfileRouteContext,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    QueryCostBudget,
    SourceConnectorKind,
    SourceDialect,
    postgres_type_contract_fingerprint,
)
from schemabridge.domain.joins import JoinProposal, RelationshipProfile
from schemabridge.domain.semantic_profile_jobs import (
    SemanticJoinProfileFailureCode,
    SemanticJoinProfileJob,
    SemanticJoinProfileJobStatus,
    SemanticJoinProfileProposal,
    SemanticJoinProfileTargetRef,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations/control_plane"
DEFAULT_ADMIN_DSN = "postgresql://postgres:local-only-not-a-secret@127.0.0.1:55434/postgres"
NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
CAPABILITY = "semantic-profile-capability-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
OTHER_CAPABILITY = "other-profile-capability-9876543210-zyxwvutsrqponmlkjihgfedcba"
EXPECTED_READER = "schemabridge_reader"
APPLY_ROUTE_SQL = (
    "SELECT * FROM schemabridge_control.apply_connector_route_change_v2("
    + ", ".join(["%s"] * 39)
    + ")"
)


@dataclass(frozen=True)
class _DatabaseUrls:
    database: str
    migrator: str
    reconciler: str
    worker: str
    runtime: str
    api: str
    catalog: str
    observer: str


def _role_dsn(role: str, database: str) -> str:
    return f"postgresql://{role}:{role}@127.0.0.1:55434/{database}"


@pytest.fixture(scope="module")
def profile_database() -> Iterator[_DatabaseUrls]:
    database = f"schemabridge_profile_{uuid4().hex[:12]}"
    admin_dsn = os.environ.get(
        "SCHEMABRIDGE_TEST_CONTROL_ADMIN_DATABASE_URL",
        DEFAULT_ADMIN_DSN,
    )
    urls = _DatabaseUrls(
        database=database,
        migrator=_role_dsn("schemabridge_migrator", database),
        reconciler=_role_dsn("schemabridge_reconciler", database),
        worker=_role_dsn("schemabridge_worker", database),
        runtime=_role_dsn("schemabridge_runtime", database),
        api=_role_dsn("schemabridge_api", database),
        catalog=_role_dsn("schemabridge_catalog", database),
        observer=_role_dsn("schemabridge_observer", database),
    )
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {} OWNER schemabridge_migrator").format(
                sql.Identifier(database)
            )
        )
        connection.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database))
        )
        connection.execute(
            sql.SQL(
                """
                GRANT CONNECT ON DATABASE {} TO
                    schemabridge_migrator,
                    schemabridge_reconciler,
                    schemabridge_worker,
                    schemabridge_runtime,
                    schemabridge_api,
                    schemabridge_catalog,
                    schemabridge_observer
                """
            ).format(sql.Identifier(database))
        )
    try:
        migrated = PostgresControlPlaneMigrator(urls.migrator, MIGRATIONS).migrate()
        assert migrated.inspection.current_version == 11
        yield urls
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )


def _proposal(index: int = 0) -> JoinProposal:
    return build_north_star_join_proposals()[index]


def _proposal_with_id(identifier: str) -> JoinProposal:
    payload = _proposal().model_dump(mode="json")
    payload["id"] = identifier
    return JoinProposal.model_validate(payload)


def _bound_proposal(
    index: int = 0,
    *,
    connection_id: str = "warehouse-primary",
) -> SemanticJoinProfileProposal:
    return SemanticJoinProfileProposal(
        connection_id=CatalogConnectionId(connection_id),
        proposal=_proposal(index),
    )


def _bound_proposal_with_id(
    identifier: str,
    *,
    connection_id: str = "warehouse-primary",
) -> SemanticJoinProfileProposal:
    return SemanticJoinProfileProposal(
        connection_id=CatalogConnectionId(connection_id),
        proposal=_proposal_with_id(identifier),
    )


def _profile() -> RelationshipProfile:
    return RelationshipProfile(
        left_row_count=7,
        right_row_count=9,
        left_null_count=0,
        right_null_count=1,
        left_invalid_count=0,
        right_invalid_count=2,
        left_distinct_valid=7,
        right_distinct_valid=5,
        matching_distinct_keys=5,
        left_max_multiplicity=1,
        right_max_multiplicity=2,
        reader_user="schemabridge_reader",
        transaction_read_only=True,
        statement_timeout_ms=5_000,
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _connector_target(
    workspace_id: str,
    connection_id: str,
    *,
    route_revision: int = 1,
) -> GovernedExecutionTarget:
    budget = QueryCostBudget(
        explain_timeout_ms=2_500,
        max_response_bytes=262_144,
        max_total_cost=Decimal("12345.67"),
        max_estimated_rows=250_000,
        max_plan_nodes=500,
        max_plan_depth=32,
        max_plan_width=8_192,
    )
    return GovernedExecutionTarget(
        workspace_id=workspace_id,
        connection_id=CatalogConnectionId(connection_id),
        connector_kind=SourceConnectorKind.POSTGRESQL,
        dialect=SourceDialect.POSTGRESQL,
        route_revision=route_revision,
        route_fingerprint=_digest(f"profile-route:{workspace_id}:{connection_id}:{route_revision}"),
        expected_reader=EXPECTED_READER,
        source_identity_fingerprint=_digest(f"profile-source:{workspace_id}:{connection_id}"),
        catalog_identity_fingerprint=_digest(f"profile-catalog:{workspace_id}:{connection_id}"),
        type_contract_fingerprint=postgres_type_contract_fingerprint(),
        cost_budget=budget,
        cost_budget_fingerprint=budget.fingerprint,
    )


def _seed_connector_target(
    urls: _DatabaseUrls,
    *,
    workspace_id: str,
    connection_id: str = "warehouse-primary",
) -> GovernedExecutionTarget:
    target = _connector_target(workspace_id, connection_id)
    label = _digest(f"{workspace_id}|{connection_id}")[:16]
    budget = target.cost_budget
    with psycopg.connect(urls.migrator) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.tenant_capacity_policies (
                workspace_id, connection_limit, asset_limit, field_limit,
                api_requests_per_minute, api_window_seconds,
                nonterminal_job_limit, catalog_cursor_ttl_seconds,
                generation_retention_seconds, version, updated_by,
                created_at, updated_at
            ) VALUES (
                %s, 10, 10000, 100000, 100, 60, 1000, 900, 1800, 1,
                'sb_platform_admin_v1', clock_timestamp(), clock_timestamp()
            )
            ON CONFLICT (workspace_id) DO NOTHING
            """,
            (workspace_id,),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.catalog_connections (
                workspace_id, connection_id, display_name, source_kind,
                catalog_scope, environment, platform_instance, status,
                registration_fingerprint, idempotency_digest,
                created_by_actor_id, created_at, updated_at
            ) VALUES (
                %s, %s, %s, 'synthetic', 'synthetic-profile', 'TEST',
                'profile-integration', 'enabled', %s, %s,
                'sb_platform_admin_v1', clock_timestamp(), clock_timestamp()
            )
            ON CONFLICT (workspace_id, connection_id) DO NOTHING
            """,
            (
                workspace_id,
                connection_id,
                f"Profile source {connection_id}",
                _digest(f"profile-registration:{workspace_id}:{connection_id}"),
                _digest(f"profile-registration-idempotency:{workspace_id}:{connection_id}"),
            ),
        )
        applied = connection.execute(
            APPLY_ROUTE_SQL,
            (
                workspace_id,
                connection_id,
                "create",
                0,
                1,
                target.route_revision,
                target.route_fingerprint,
                target.expected_reader,
                1,
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
                _digest(f"profile-contract:{workspace_id}:{connection_id}"),
                target.fingerprint,
                f"profile.preflight.{label}",
                f"profile.catalog.{label}",
                f"profile.execution.{label}",
                f"profile.profile.{label}",
                101,
                202,
                303,
                404,
                _digest(f"profile-proposal:{workspace_id}:{connection_id}"),
                f"approval-profile-{label}",
                _digest(f"profile-approval:{workspace_id}:{connection_id}"),
                "sb_platform_admin_v1",
                _digest(f"profile-idempotency:{workspace_id}:{connection_id}"),
                "connector_route_audit_"
                + _digest(f"profile-audit-id:{workspace_id}:{connection_id}"),
                _digest(f"profile-audit:{workspace_id}:{connection_id}"),
                _digest(f"profile-head:{workspace_id}:{connection_id}"),
                "CREATE CONNECTOR ROUTE",
            ),
        ).fetchone()
    assert applied is not None
    ensure_compatible_catalog_generation(
        urls.migrator,
        urls.api,
        urls.catalog,
        workspace_id=workspace_id,
        connection_id=CatalogConnectionId(connection_id),
        target=CatalogTargetFacts(
            contract_version=1,
            route_revision=target.route_revision,
            target_fingerprint=target.fingerprint,
            source_identity_fingerprint=target.source_identity_fingerprint,
            catalog_identity_fingerprint=target.catalog_identity_fingerprint,
            type_contract_fingerprint=target.type_contract_fingerprint,
        ),
    )
    return target


def _seed_scan(
    dsn: str,
    *,
    workspace_id: str,
    fingerprint: str,
    generation: int = 1,
) -> str:
    scan_id = f"scan_{fingerprint}"
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.semantic_change_scan_requests (
                scan_id, workspace_id, source_kind, source_event_key,
                source_fingerprint, catalog_scope, registry_id,
                registry_generation, status, attempts, max_attempts,
                available_at, fencing_token, requested_at, updated_at
            ) VALUES (
                %s, %s, 'registry_pointer', %s, %s,
                'synthetic-demo', 'synthetic_enterprise', %s,
                'requested', 0, 5, clock_timestamp(), 0,
                clock_timestamp(), clock_timestamp()
            )
            """,
            (
                scan_id,
                workspace_id,
                f"transition-profile-{fingerprint[:12]}",
                fingerprint,
                generation,
            ),
        )
    return scan_id


def test_profile_queue_replays_exactly_fences_and_exposes_only_aggregates(
    profile_database: _DatabaseUrls,
) -> None:
    workspace_id = f"workspace-profile-{uuid4().hex[:12]}"
    target = _seed_connector_target(
        profile_database,
        workspace_id=workspace_id,
    )
    target_ref = SemanticJoinProfileTargetRef.from_target(target)
    scan_id = _seed_scan(
        profile_database.migrator,
        workspace_id=workspace_id,
        fingerprint="a" * 64,
    )
    reconciler_queue = PostgresSemanticJoinProfileQueue(
        profile_database.reconciler,
    )
    worker_queue = PostgresSemanticJoinProfileQueue(
        profile_database.worker,
        application_name="schemabridge-control-worker",
    )
    queued = QueuedRelationshipEvidencePort(
        queue=reconciler_queue,
        target_resolver=PostgresExecutionTargetResolver(profile_database.reconciler),
        workspace_id=workspace_id,
        scan_id=scan_id,
        clock=lambda: NOW,
    )

    with pytest.raises(RelationshipWorkflowError) as pending:
        queued.profile_bound(_bound_proposal())
    assert pending.value.code is RelationshipErrorCode.EVIDENCE_UNAVAILABLE

    first = reconciler_queue.enqueue(
        workspace_id,
        scan_id,
        _bound_proposal(),
        execution_target=target_ref,
        requested_at=NOW,
    )
    replay = reconciler_queue.enqueue(
        workspace_id,
        scan_id,
        _bound_proposal(),
        execution_target=target_ref,
        requested_at=NOW + timedelta(hours=1),
    )
    assert first.replayed is True
    assert replay.replayed is True
    assert replay.job == first.job

    with pytest.raises(SemanticJoinProfileQueueError) as changed_attempts:
        reconciler_queue.enqueue(
            workspace_id,
            scan_id,
            _bound_proposal(),
            execution_target=target_ref,
            requested_at=NOW,
            max_attempts=6,
        )
    assert changed_attempts.value.code is SemanticJoinProfileQueueErrorCode.IDEMPOTENCY_CONFLICT
    assert (
        reconciler_queue.load(
            "workspace-other",
            scan_id,
            first.job.proposal_fingerprint,
        )
        is None
    )

    claimed = worker_queue.claim_next(
        worker_id="semantic-profile-worker-a",
        lease_capability=CAPABILITY,
        lease_duration=timedelta(seconds=120),
    )
    assert claimed is not None
    assert claimed.job_id == first.job.job_id
    assert claimed.status is SemanticJoinProfileJobStatus.LEASED
    assert claimed.lease is not None
    assert claimed.lease.capability_digest != CAPABILITY
    assert claimed.execution_target == target_ref
    assert claimed.connector_contract_version == 1

    route = PostgresSemanticProfileConnectorRouteReader(profile_database.worker).load(
        SemanticJoinProfileRouteContext.from_claim(
            claimed,
            worker_id="semantic-profile-worker-a",
            lease_capability=CAPABILITY,
        )
    )
    assert route.dialect is SourceDialect.POSTGRESQL
    assert route.expected_reader == EXPECTED_READER
    assert route.secret_reference.value.startswith("profile.profile.")
    assert route.secret_reference.provider_secret_version == 404

    with pytest.raises(SemanticJoinProfileQueueError) as stale_capability:
        worker_queue.heartbeat(
            workspace_id,
            claimed.job_id,
            worker_id="semantic-profile-worker-a",
            lease_capability=OTHER_CAPABILITY,
            fencing_token=claimed.fencing_token,
            lease_duration=timedelta(seconds=120),
        )
    assert stale_capability.value.code is SemanticJoinProfileQueueErrorCode.LEASE_CONFLICT

    heartbeated = worker_queue.heartbeat(
        workspace_id,
        claimed.job_id,
        worker_id="semantic-profile-worker-a",
        lease_capability=CAPABILITY,
        fencing_token=claimed.fencing_token,
        lease_duration=timedelta(seconds=120),
    )
    non_aggregate_result = {
        **_profile().model_dump(mode="json"),
        "rows": [{"opaque": "not-allowed"}],
    }
    invalid_update_at = heartbeated.updated_at + timedelta(microseconds=1)
    with (
        psycopg.connect(profile_database.worker) as connection,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        connection.execute(
            """
            UPDATE schemabridge_control.semantic_join_profile_jobs
            SET status = 'completed',
                lease_owner_id = NULL,
                lease_capability_digest = NULL,
                lease_acquired_at = NULL,
                lease_heartbeat_at = NULL,
                lease_expires_at = NULL,
                result_profile_json = %s,
                result_profile_fingerprint = %s,
                updated_at = %s,
                completed_at = %s,
                retain_until = %s
            WHERE workspace_id = %s AND job_id = %s
            """,
            (
                Jsonb(non_aggregate_result),
                "e" * 64,
                invalid_update_at,
                invalid_update_at,
                invalid_update_at + timedelta(days=30),
                workspace_id,
                heartbeated.job_id,
            ),
        )
    completed = worker_queue.complete(
        workspace_id,
        heartbeated.job_id,
        worker_id="semantic-profile-worker-a",
        lease_capability=CAPABILITY,
        fencing_token=heartbeated.fencing_token,
        profile=_profile(),
        retention=timedelta(days=30),
    )
    assert completed.status is SemanticJoinProfileJobStatus.COMPLETED
    assert completed.result is not None
    assert completed.result.profile == _profile()
    assert queued.profile_bound(_bound_proposal()) == _profile()

    with psycopg.connect(profile_database.migrator) as connection:
        row = connection.execute(
            """
            SELECT connection_id, proposal_json, result_profile_json,
                   lease_capability_digest, failure_code
            FROM schemabridge_control.semantic_join_profile_jobs
            WHERE workspace_id = %s AND job_id = %s
            """,
            (workspace_id, completed.job_id),
        ).fetchone()
    assert row is not None
    connection_id, proposal_json, result_json, capability_digest, failure_code = row
    assert connection_id == "warehouse-primary"
    assert set(proposal_json) == {
        "id",
        "left_key",
        "right_key",
        "default_join_type",
    }
    assert set(result_json) == set(_profile().model_dump(mode="json"))
    serialized = f"{proposal_json!r}{result_json!r}".casefold()
    for protected in (
        "source_row",
        "sample_value",
        "raw_value",
        "'sql'",
        "'parameters'",
        "'dsn'",
        "'token'",
        "'password'",
    ):
        assert protected not in serialized
    assert capability_digest is None
    assert failure_code is None


def test_profile_queue_separates_homonymous_proposals_by_connection(
    profile_database: _DatabaseUrls,
) -> None:
    workspace_id = f"workspace-connections-{uuid4().hex[:12]}"
    primary_target = _seed_connector_target(
        profile_database,
        workspace_id=workspace_id,
        connection_id="warehouse-primary",
    )
    homonym_target = _seed_connector_target(
        profile_database,
        workspace_id=workspace_id,
        connection_id="warehouse-homonym",
    )
    scan_id = _seed_scan(
        profile_database.migrator,
        workspace_id=workspace_id,
        fingerprint="e" * 64,
    )
    reconciler_queue = PostgresSemanticJoinProfileQueue(profile_database.reconciler)
    worker_queue = PostgresSemanticJoinProfileQueue(
        profile_database.worker,
        application_name="schemabridge-control-worker",
    )
    primary = reconciler_queue.enqueue(
        workspace_id,
        scan_id,
        _bound_proposal(connection_id="warehouse-primary"),
        execution_target=SemanticJoinProfileTargetRef.from_target(primary_target),
        requested_at=NOW,
    ).job
    homonym = reconciler_queue.enqueue(
        workspace_id,
        scan_id,
        _bound_proposal(connection_id="warehouse-homonym"),
        execution_target=SemanticJoinProfileTargetRef.from_target(homonym_target),
        requested_at=NOW,
    ).job

    assert primary.proposal == homonym.proposal
    assert primary.proposal_fingerprint != homonym.proposal_fingerprint
    assert primary.job_id != homonym.job_id

    first_claim = worker_queue.claim_next(
        worker_id="semantic-profile-worker-dynamic",
        lease_capability=CAPABILITY,
        lease_duration=timedelta(seconds=120),
    )
    assert first_claim is not None
    expected_targets = {
        primary.job_id: SemanticJoinProfileTargetRef.from_target(primary_target),
        homonym.job_id: SemanticJoinProfileTargetRef.from_target(homonym_target),
    }
    assert first_claim.job_id in expected_targets
    assert first_claim.execution_target == expected_targets[first_claim.job_id]
    worker_queue.complete(
        first_claim.workspace_id,
        first_claim.job_id,
        worker_id="semantic-profile-worker-dynamic",
        lease_capability=CAPABILITY,
        fencing_token=first_claim.fencing_token,
        profile=_profile(),
        retention=timedelta(days=30),
    )

    second_claim = worker_queue.claim_next(
        worker_id="semantic-profile-worker-dynamic",
        lease_capability=OTHER_CAPABILITY,
        lease_duration=timedelta(seconds=120),
    )
    assert second_claim is not None
    assert second_claim.job_id == ({primary.job_id, homonym.job_id} - {first_claim.job_id}).pop()
    assert second_claim.execution_target == expected_targets[second_claim.job_id]
    worker_queue.complete(
        second_claim.workspace_id,
        second_claim.job_id,
        worker_id="semantic-profile-worker-dynamic",
        lease_capability=OTHER_CAPABILITY,
        fencing_token=second_claim.fencing_token,
        profile=_profile(),
        retention=timedelta(days=30),
    )
    assert (
        worker_queue.claim_next(
            worker_id="semantic-profile-worker-dynamic",
            lease_capability=CAPABILITY,
            lease_duration=timedelta(seconds=120),
        )
        is None
    )


def test_profile_worker_routes_dynamically_across_workspaces_with_reused_connection_id(
    profile_database: _DatabaseUrls,
) -> None:
    workspace_a = f"workspace-route-a-{uuid4().hex[:12]}"
    workspace_b = f"workspace-route-b-{uuid4().hex[:12]}"
    target_a = _seed_connector_target(
        profile_database,
        workspace_id=workspace_a,
    )
    target_b = _seed_connector_target(
        profile_database,
        workspace_id=workspace_b,
    )
    scan_a = _seed_scan(
        profile_database.migrator,
        workspace_id=workspace_a,
        fingerprint="1" * 64,
    )
    scan_b = _seed_scan(
        profile_database.migrator,
        workspace_id=workspace_b,
        fingerprint="2" * 64,
    )
    reconciler_queue = PostgresSemanticJoinProfileQueue(profile_database.reconciler)
    worker_queue = PostgresSemanticJoinProfileQueue(
        profile_database.worker,
        application_name="schemabridge-control-worker",
    )
    job_b = reconciler_queue.enqueue(
        workspace_b,
        scan_b,
        _bound_proposal(connection_id="warehouse-primary"),
        execution_target=SemanticJoinProfileTargetRef.from_target(target_b),
        requested_at=NOW,
    ).job
    job_a = reconciler_queue.enqueue(
        workspace_a,
        scan_a,
        _bound_proposal(connection_id="warehouse-primary"),
        execution_target=SemanticJoinProfileTargetRef.from_target(target_a),
        requested_at=NOW,
    ).job

    expected_targets = {
        job_a.job_id: SemanticJoinProfileTargetRef.from_target(target_a),
        job_b.job_id: SemanticJoinProfileTargetRef.from_target(target_b),
    }
    first_claim = worker_queue.claim_next(
        worker_id="semantic-profile-worker-dynamic",
        lease_capability=CAPABILITY,
        lease_duration=timedelta(seconds=120),
    )

    assert first_claim is not None
    assert first_claim.job_id in expected_targets
    assert first_claim.execution_target == expected_targets[first_claim.job_id]
    worker_queue.complete(
        first_claim.workspace_id,
        first_claim.job_id,
        worker_id="semantic-profile-worker-dynamic",
        lease_capability=CAPABILITY,
        fencing_token=first_claim.fencing_token,
        profile=_profile(),
        retention=timedelta(days=30),
    )

    second_claim = worker_queue.claim_next(
        worker_id="semantic-profile-worker-dynamic",
        lease_capability=OTHER_CAPABILITY,
        lease_duration=timedelta(seconds=120),
    )
    assert second_claim is not None
    assert second_claim.job_id == ({job_a.job_id, job_b.job_id} - {first_claim.job_id}).pop()
    assert second_claim.execution_target == expected_targets[second_claim.job_id]
    worker_queue.complete(
        second_claim.workspace_id,
        second_claim.job_id,
        worker_id="semantic-profile-worker-dynamic",
        lease_capability=OTHER_CAPABILITY,
        fencing_token=second_claim.fencing_token,
        profile=_profile(),
        retention=timedelta(days=30),
    )
    assert (
        worker_queue.claim_next(
            worker_id="semantic-profile-worker-dynamic",
            lease_capability=CAPABILITY,
            lease_duration=timedelta(seconds=120),
        )
        is None
    )


def test_profile_claim_skips_a_locked_earlier_job(
    profile_database: _DatabaseUrls,
) -> None:
    workspace_id = f"workspace-skip-{uuid4().hex[:12]}"
    target_ref = SemanticJoinProfileTargetRef.from_target(
        _seed_connector_target(
            profile_database,
            workspace_id=workspace_id,
        )
    )
    scan_id = _seed_scan(
        profile_database.migrator,
        workspace_id=workspace_id,
        fingerprint="b" * 64,
    )
    reconciler_queue = PostgresSemanticJoinProfileQueue(
        profile_database.reconciler,
    )
    worker_queue = PostgresSemanticJoinProfileQueue(
        profile_database.worker,
        application_name="schemabridge-control-worker",
    )
    first = reconciler_queue.enqueue(
        workspace_id,
        scan_id,
        _bound_proposal(0),
        execution_target=target_ref,
        requested_at=NOW,
    ).job
    second = reconciler_queue.enqueue(
        workspace_id,
        scan_id,
        _bound_proposal(1),
        execution_target=target_ref,
        requested_at=NOW,
    ).job

    with psycopg.connect(profile_database.migrator) as lock_connection:
        locked = lock_connection.execute(
            """
            SELECT job_id
            FROM schemabridge_control.semantic_join_profile_jobs
            WHERE workspace_id = %s AND job_id = %s
            FOR UPDATE
            """,
            (workspace_id, first.job_id),
        ).fetchone()
        assert locked == (first.job_id,)
        started = time.monotonic()
        claimed = worker_queue.claim_next(
            worker_id="semantic-profile-worker-b",
            lease_capability=CAPABILITY,
            lease_duration=timedelta(seconds=120),
        )
        elapsed = time.monotonic() - started

    assert claimed is not None
    assert claimed.job_id == second.job_id
    assert elapsed < 2.0
    worker_queue.complete(
        workspace_id,
        claimed.job_id,
        worker_id="semantic-profile-worker-b",
        lease_capability=CAPABILITY,
        fencing_token=claimed.fencing_token,
        profile=_profile(),
        retention=timedelta(days=30),
    )
    remaining = worker_queue.claim_next(
        worker_id="semantic-profile-worker-b",
        lease_capability=CAPABILITY,
        lease_duration=timedelta(seconds=120),
    )
    assert remaining is not None
    assert remaining.job_id == first.job_id
    worker_queue.complete(
        workspace_id,
        remaining.job_id,
        worker_id="semantic-profile-worker-b",
        lease_capability=CAPABILITY,
        fencing_token=remaining.fencing_token,
        profile=_profile(),
        retention=timedelta(days=30),
    )


def test_profile_failure_retry_and_seven_role_grants_are_minimal(
    profile_database: _DatabaseUrls,
) -> None:
    workspace_id = f"workspace-retry-{uuid4().hex[:12]}"
    target_ref = SemanticJoinProfileTargetRef.from_target(
        _seed_connector_target(
            profile_database,
            workspace_id=workspace_id,
        )
    )
    scan_id = _seed_scan(
        profile_database.migrator,
        workspace_id=workspace_id,
        fingerprint="c" * 64,
    )
    reconciler_queue = PostgresSemanticJoinProfileQueue(
        profile_database.reconciler,
    )
    worker_queue = PostgresSemanticJoinProfileQueue(
        profile_database.worker,
        application_name="schemabridge-control-worker",
    )
    requested = reconciler_queue.enqueue(
        workspace_id,
        scan_id,
        _bound_proposal(),
        execution_target=target_ref,
        requested_at=NOW,
        max_attempts=2,
    ).job
    claimed = worker_queue.claim_next(
        worker_id="semantic-profile-worker-c",
        lease_capability=CAPABILITY,
        lease_duration=timedelta(seconds=120),
    )
    assert claimed is not None and claimed.job_id == requested.job_id
    retry = worker_queue.fail(
        workspace_id,
        claimed.job_id,
        worker_id="semantic-profile-worker-c",
        lease_capability=CAPABILITY,
        fencing_token=claimed.fencing_token,
        code=SemanticJoinProfileFailureCode.SOURCE_UNAVAILABLE,
        retry_delay=timedelta(seconds=5),
        retention=None,
    )
    assert retry.status is SemanticJoinProfileJobStatus.RETRY_WAIT
    assert retry.failure is not None
    assert retry.failure.code is SemanticJoinProfileFailureCode.SOURCE_UNAVAILABLE
    assert retry.available_at > retry.updated_at

    role_dsns = {
        "migrator": profile_database.migrator,
        "reconciler": profile_database.reconciler,
        "worker": profile_database.worker,
        "runtime": profile_database.runtime,
        "api": profile_database.api,
        "catalog": profile_database.catalog,
        "observer": profile_database.observer,
    }
    expected = {
        "migrator": (True, True, True, True, True),
        "reconciler": (True, True, False, False, False),
        "worker": (True, False, False, True, False),
        "runtime": (False, False, False, False, False),
        "api": (False, False, False, False, False),
        "catalog": (False, False, False, False, False),
        "observer": (False, False, False, False, False),
    }
    for role, dsn in role_dsns.items():
        with psycopg.connect(dsn) as connection:
            privileges = connection.execute(
                """
                SELECT
                    has_table_privilege(
                        current_user,
                        'schemabridge_control.semantic_join_profile_jobs',
                        'SELECT'
                    ),
                    has_table_privilege(
                        current_user,
                        'schemabridge_control.semantic_join_profile_jobs',
                        'INSERT'
                    ),
                    has_table_privilege(
                        current_user,
                        'schemabridge_control.semantic_join_profile_jobs',
                        'UPDATE'
                    ),
                    has_column_privilege(
                        current_user,
                        'schemabridge_control.semantic_join_profile_jobs',
                        'status',
                        'UPDATE'
                    ),
                    has_column_privilege(
                        current_user,
                        'schemabridge_control.semantic_join_profile_jobs',
                        'proposal_json',
                        'UPDATE'
                    )
                """
            ).fetchone()
        assert privileges == expected[role]

    with (
        psycopg.connect(profile_database.reconciler) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        connection.execute(
            """
            UPDATE schemabridge_control.semantic_join_profile_jobs
            SET result_profile_json = NULL
            WHERE workspace_id = %s AND job_id = %s
            """,
            (workspace_id, retry.job_id),
        )
    with (
        psycopg.connect(profile_database.worker) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        connection.execute(
            """
            INSERT INTO schemabridge_control.semantic_join_profile_jobs (
                job_id, workspace_id, scan_id, proposal_fingerprint,
                proposal_json, status, attempt_count, max_attempts,
                available_at, fencing_token, requested_at, updated_at
            )
            SELECT job_id, workspace_id, scan_id, proposal_fingerprint,
                   proposal_json, status, attempt_count, max_attempts,
                   available_at, fencing_token, requested_at, updated_at
            FROM schemabridge_control.semantic_join_profile_jobs
            WHERE false
            """
        )


def test_profile_queue_has_a_hard_per_scan_capacity_with_replay_exemption(
    profile_database: _DatabaseUrls,
) -> None:
    workspace_id = f"workspace-capacity-{uuid4().hex[:12]}"
    target_ref = SemanticJoinProfileTargetRef.from_target(
        _seed_connector_target(
            profile_database,
            workspace_id=workspace_id,
        )
    )
    scan_id = _seed_scan(
        profile_database.migrator,
        workspace_id=workspace_id,
        fingerprint="d" * 64,
    )
    queue = PostgresSemanticJoinProfileQueue(profile_database.reconciler)

    jobs = [
        SemanticJoinProfileJob.requested(
            workspace_id=workspace_id,
            scan_id=scan_id,
            proposal=_bound_proposal_with_id(f"capacity_join_{index:03d}"),
            execution_target=target_ref,
            requested_at=NOW,
        )
        for index in range(500)
    ]
    with psycopg.connect(profile_database.reconciler) as connection:
        connection.cursor().executemany(
            """
            INSERT INTO schemabridge_control.semantic_join_profile_jobs (
                job_id, workspace_id, scan_id, connection_id,
                connector_route_revision, connector_route_fingerprint,
                connector_target_fingerprint,
                proposal_fingerprint, proposal_json, status,
                attempt_count, max_attempts, available_at,
                fencing_token, requested_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            [
                (
                    job.job_id,
                    job.workspace_id,
                    job.scan_id,
                    job.connection_id.root,
                    target_ref.route_revision,
                    target_ref.route_fingerprint,
                    target_ref.target_fingerprint,
                    job.proposal_fingerprint,
                    Jsonb(job.proposal.model_dump(mode="json")),
                    job.status.value,
                    job.attempts,
                    job.max_attempts,
                    job.available_at,
                    job.fencing_token,
                    job.requested_at,
                    job.updated_at,
                )
                for job in jobs
            ],
        )

    with pytest.raises(SemanticJoinProfileQueueError) as exhausted:
        queue.enqueue(
            workspace_id,
            scan_id,
            _bound_proposal_with_id("capacity_join_500"),
            execution_target=target_ref,
            requested_at=NOW,
        )
    assert exhausted.value.code is SemanticJoinProfileQueueErrorCode.CAPACITY_EXCEEDED

    replay = queue.enqueue(
        workspace_id,
        scan_id,
        _bound_proposal_with_id("capacity_join_000"),
        execution_target=target_ref,
        requested_at=NOW + timedelta(days=1),
    )
    assert replay.replayed is True
