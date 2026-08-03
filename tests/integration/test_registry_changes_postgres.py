"""PostgreSQL proof for M35 API-safe registry change persistence and publication."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.errors import InsufficientPrivilege, ObjectNotInPrerequisiteState
from psycopg.types.json import Jsonb
from tests.integration.test_semantic_profile_queue_postgres import (
    CAPABILITY,
    _DatabaseUrls,
    _profile,
    _seed_connector_target,
)
from tests.unit.test_registry_change_authoring import _join_proposal, _principal
from tests.unit.test_registry_join_changes import _two_model_base
from tests.unit.test_registry_model_change_authoring import _Harness
from tests.unit.test_registry_model_changes import _replacement_base
from tests.unit.test_registry_publication_v2 import (
    _approved_draft_from_proposal,
)
from tests.unit.test_registry_publication_v2 import (
    _proposal as _onboarding_proposal,
)

from schemabridge.adapters.control_plane.postgres_active_registry import (
    PostgresActiveRegistryPointerReader,
)
from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.adapters.control_plane.postgres_registry_control import (
    PostgresRegistryControlStore,
)
from schemabridge.adapters.control_plane.postgres_registry_publication import (
    PostgresRegistryPublicationJobStore,
    PostgresRegistryPublicationProposalReader,
)
from schemabridge.adapters.semantic_change.postgres_dependencies import (
    PostgresSemanticChangeDependencyIndex,
)
from schemabridge.adapters.semantic_change.postgres_profile_queue import (
    PostgresSemanticJoinProfileQueue,
)
from schemabridge.adapters.storage.postgres_registry_changes import (
    PostgresRegistryChangeStore,
    PostgresRegistryJoinProfileRequestQueue,
)
from schemabridge.adapters.storage.postgres_registry_model_changes import (
    PostgresRegistryModelChangeStore,
    PostgresRegistryModelJoinProfileQueue,
    PostgresRegistryModelProfileStore,
    PostgresRegistryModelReplacementSourceReader,
)
from schemabridge.application.ports.registry_changes import (
    RegistryChangeStoreError,
    RegistryChangeStoreErrorCode,
)
from schemabridge.application.ports.registry_model_changes import (
    RegistryModelChangePortError,
    RegistryModelChangePortErrorCode,
)
from schemabridge.application.ports.semantic_profile_jobs import (
    SemanticJoinProfileQueueError,
)
from schemabridge.domain.connectors import GovernedExecutionTarget
from schemabridge.domain.decisions import DecisionAction
from schemabridge.domain.identity import IdentityRole
from schemabridge.domain.registry_change_authoring import (
    RegistryChangeAuditEvent,
    RegistryChangeAuditRecord,
    RegistryJoinProfileAuthoringRequest,
)
from schemabridge.domain.registry_changes import (
    PreparedRegistryJoinProposal,
    RegistryJoinChangeDraft,
    RegistryJoinProfileRequest,
    decide_registry_join_change,
    mark_registry_join_change_ready,
    resolve_registry_join_base_evidence,
)
from schemabridge.domain.registry_model_change_authoring import (
    RegistryModelJoinProfileAuditEvent,
    RegistryModelJoinProfileAuditRecord,
    RegistryModelJoinProfileAuthoringRequest,
    RegistryModelJoinProfileRequest,
)
from schemabridge.domain.registry_model_changes import (
    assemble_model_replacement_registry_version,
)
from schemabridge.domain.registry_publication import (
    PublicationReadbackReceipt,
    RegistryPublicationAuthorization,
    RegistryPublicationAuthorizationConfirmation,
    observed_registry_related_asset_urns,
)
from schemabridge.domain.registry_publication_jobs import (
    RegistryPublicationJobStatus,
    create_registry_publication_job,
    registry_publication_request_fingerprint,
)
from schemabridge.domain.semantic_onboarding import (
    SemanticOnboardingDraft,
    SemanticOnboardingStatus,
)
from schemabridge.domain.semantic_profile_jobs import (
    SemanticJoinProfileProposal,
    SemanticJoinProfileTargetRef,
    semantic_join_profile_proposal_fingerprint,
)
from schemabridge.domain.semantic_registry import (
    GovernedSemanticRegistrySnapshot,
    SemanticRegistryScope,
    datahub_registry_document_urn,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations/control_plane"
ADMIN_DSN = "postgresql://postgres:local-only-not-a-secret@127.0.0.1:55434/postgres"


@dataclass(frozen=True, slots=True)
class _RegistryDatabase:
    urls: _DatabaseUrls
    target: GovernedExecutionTarget


def _role_dsn(role: str, database: str) -> str:
    return f"postgresql://{role}:{role}@127.0.0.1:55434/{database}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def registry_database() -> Iterator[_RegistryDatabase]:
    database = f"schemabridge_registry_{uuid4().hex[:12]}"
    admin_dsn = os.environ.get("SCHEMABRIDGE_TEST_CONTROL_ADMIN_DATABASE_URL", ADMIN_DSN)
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
                    schemabridge_migrator, schemabridge_reconciler,
                    schemabridge_worker, schemabridge_runtime,
                    schemabridge_api, schemabridge_catalog,
                    schemabridge_observer, schemabridge_publisher
                """
            ).format(sql.Identifier(database))
        )
    try:
        migrated = PostgresControlPlaneMigrator(urls.migrator, MIGRATIONS).migrate()
        assert migrated.inspection.current_version == 15
        target = _seed_connector_target(
            urls,
            workspace_id="workspace-a",
            connection_id="warehouse-a",
        )
        yield _RegistryDatabase(urls=urls, target=target)
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )


def test_registry_change_store_queue_cas_replay_and_api_grants(
    registry_database: _RegistryDatabase,
) -> None:
    urls = registry_database.urls
    base, authoring = _authoring(registry_database.target)
    store = PostgresRegistryChangeStore(urls.api)
    queue = PostgresRegistryJoinProfileRequestQueue(urls.api)
    requested_audit = _audit(
        authoring,
        event=RegistryChangeAuditEvent.PROFILE_REQUESTED,
        actor_id=authoring.owner_actor_id,
        occurred_at=authoring.created_at,
        source_revision=0,
        resulting_revision=0,
        previous_fingerprint=None,
        resulting_fingerprint=authoring.fingerprint,
        profile_request_fingerprint=authoring.request.fingerprint,
    )
    request_digest = _digest("registry-postgres-request")
    request_fingerprint = _digest("registry-postgres-request-payload")

    persisted = store.persist_profile_request(
        authoring,
        requested_audit,
        operation="request_profile",
        actor_id=authoring.owner_actor_id,
        idempotency_digest=request_digest,
        request_fingerprint=request_fingerprint,
    )
    assert persisted.authoring == authoring
    assert persisted.job is None
    assert store.load_operation_replay(authoring.workspace_id, request_digest).job is None  # type: ignore[union-attr]

    queued = queue.enqueue(authoring)
    assert queued.replayed is False
    assert queued.job.requested_at == authoring.created_at
    assert queue.enqueue(authoring).replayed is True
    worker = PostgresSemanticJoinProfileQueue(
        urls.worker,
        application_name="schemabridge-control-worker",
    )
    with pytest.raises(SemanticJoinProfileQueueError):
        worker.claim_next(
            worker_id="registry-profile-worker",
            lease_capability=CAPABILITY,
            lease_duration=timedelta(minutes=2),
        )

    bound_audit = _audit(
        authoring,
        event=RegistryChangeAuditEvent.PROFILE_JOB_BOUND,
        actor_id=authoring.owner_actor_id,
        occurred_at=authoring.created_at,
        source_revision=0,
        resulting_revision=0,
        previous_fingerprint=authoring.fingerprint,
        resulting_fingerprint=authoring.fingerprint,
        profile_request_fingerprint=authoring.request.fingerprint,
        profile_job_id=queued.job.job_id,
    )
    bound = store.bind_profile_job(
        authoring,
        queued.job,
        bound_audit,
        idempotency_digest=request_digest,
    )
    assert bound.job == queued.job
    assert (
        store.bind_profile_job(
            authoring,
            queued.job,
            bound_audit,
            idempotency_digest=request_digest,
        ).replayed
        is True
    )
    replay = store.load_operation_replay(authoring.workspace_id, request_digest)
    assert replay is not None
    assert replay.job == queued.job

    claimed = worker.claim_next(
        worker_id="registry-profile-worker",
        lease_capability=CAPABILITY,
        lease_duration=timedelta(minutes=2),
    )
    assert claimed is not None and claimed.lease is not None
    completed = worker.complete(
        claimed.workspace_id,
        claimed.job_id,
        worker_id="registry-profile-worker",
        lease_capability=CAPABILITY,
        fencing_token=claimed.fencing_token,
        profile=_profile(),
        retention=timedelta(days=30),
    )
    assert queue.load(authoring) == completed
    assert store.load_operation_replay(authoring.workspace_id, request_digest).job == queued.job  # type: ignore[union-attr]

    draft = RegistryJoinChangeDraft.create(
        id=authoring.id,
        workspace_id=authoring.workspace_id,
        owner_actor_id=authoring.owner_actor_id,
        scope=authoring.request.scope,
        base=base,
        base_registry=authoring.request.base_evidence.base_registry,
        profile_job=completed,
        created_at=completed.completed_at + timedelta(seconds=1),  # type: ignore[operator]
    )
    draft_audit = _audit(
        authoring,
        event=RegistryChangeAuditEvent.DRAFT_FINALIZED,
        actor_id=authoring.owner_actor_id,
        occurred_at=draft.created_at,
        source_revision=0,
        resulting_revision=1,
        previous_fingerprint=authoring.fingerprint,
        resulting_fingerprint=draft.fingerprint,
        profile_request_fingerprint=authoring.request.fingerprint,
        profile_job_id=completed.job_id,
    )
    draft_digest = _digest("registry-postgres-draft")
    draft_request_fingerprint = _digest("registry-postgres-draft-payload")
    created = store.commit_draft(
        authoring,
        draft,
        draft_audit,
        operation="finalize_draft",
        actor_id=authoring.owner_actor_id,
        idempotency_digest=draft_digest,
        request_fingerprint=draft_request_fingerprint,
    )
    assert created.draft == draft
    assert (
        store.commit_draft(
            authoring,
            draft,
            draft_audit,
            operation="finalize_draft",
            actor_id=authoring.owner_actor_id,
            idempotency_digest=draft_digest,
            request_fingerprint=draft_request_fingerprint,
        ).replayed
        is True
    )

    approved = decide_registry_join_change(
        draft,
        action=DecisionAction.APPROVE,
        expected_revision=1,
        actor_id="steward-postgres",
        decided_at=draft.created_at + timedelta(seconds=1),
        rationale="Aggregate evidence confirms the governed many-to-one relationship safely.",
    )
    assert approved.decision is not None
    decision_audit = _audit(
        authoring,
        event=RegistryChangeAuditEvent.DECISION_RECORDED,
        actor_id="steward-postgres",
        occurred_at=approved.decision.decided_at,
        source_revision=1,
        resulting_revision=2,
        previous_fingerprint=draft.fingerprint,
        resulting_fingerprint=approved.fingerprint,
        decision_id=approved.decision.id,
    )
    decision_digest = _digest("registry-postgres-decision")
    decision_request_fingerprint = _digest("registry-postgres-decision-payload")
    decided = store.commit_decision(
        authoring,
        approved,
        decision_audit,
        expected_revision=1,
        operation="record_decision",
        actor_id="steward-postgres",
        idempotency_digest=decision_digest,
        request_fingerprint=decision_request_fingerprint,
    )
    assert decided.draft == approved
    assert (
        store.commit_decision(
            authoring,
            approved,
            decision_audit,
            expected_revision=1,
            operation="record_decision",
            actor_id="steward-postgres",
            idempotency_digest=decision_digest,
            request_fingerprint=decision_request_fingerprint,
        ).replayed
        is True
    )
    with pytest.raises(RegistryChangeStoreError) as stale:
        store.commit_decision(
            authoring,
            approved,
            decision_audit,
            expected_revision=1,
            operation="record_decision",
            actor_id="steward-postgres",
            idempotency_digest=_digest("registry-postgres-stale-decision"),
            request_fingerprint=_digest("registry-postgres-stale-decision-payload"),
        )
    assert stale.value.code is RegistryChangeStoreErrorCode.CONFLICT

    proposal = PreparedRegistryJoinProposal.create(
        id="join-change-postgres-v3",
        draft=approved,
        prepared_by="publisher-postgres",
        prepared_at=approved.updated_at + timedelta(seconds=1),
    )
    ready = mark_registry_join_change_ready(approved, proposal)
    preparation_audit = _audit(
        authoring,
        event=RegistryChangeAuditEvent.PUBLICATION_PREPARED,
        actor_id="publisher-postgres",
        occurred_at=proposal.prepared_at,
        source_revision=2,
        resulting_revision=2,
        previous_fingerprint=approved.fingerprint,
        resulting_fingerprint=ready.fingerprint,
        proposal_id=proposal.id,
    )
    preparation_digest = _digest("registry-postgres-preparation")
    preparation_request_fingerprint = _digest("registry-postgres-preparation-payload")
    prepared = store.commit_preparation(
        authoring,
        ready,
        proposal,
        preparation_audit,
        expected_revision=2,
        operation="prepare_publication",
        actor_id="publisher-postgres",
        idempotency_digest=preparation_digest,
        request_fingerprint=preparation_request_fingerprint,
    )
    assert prepared.proposal == proposal
    assert (
        store.commit_preparation(
            authoring,
            ready,
            proposal,
            preparation_audit,
            expected_revision=2,
            operation="prepare_publication",
            actor_id="publisher-postgres",
            idempotency_digest=preparation_digest,
            request_fingerprint=preparation_request_fingerprint,
        ).replayed
        is True
    )
    assert store.load_authoring(authoring.workspace_id, authoring.id) == authoring
    assert store.load_draft(authoring.workspace_id, authoring.id) == ready
    assert store.list_for_workspace(
        authoring.workspace_id,
        owner_actor_id=authoring.owner_actor_id,
        limit=10,
    ) == (authoring,)
    assert tuple(item.event for item in store.list_audit(authoring.workspace_id, authoring.id)) == (
        RegistryChangeAuditEvent.PROFILE_REQUESTED,
        RegistryChangeAuditEvent.PROFILE_JOB_BOUND,
        RegistryChangeAuditEvent.DRAFT_FINALIZED,
        RegistryChangeAuditEvent.DECISION_RECORDED,
        RegistryChangeAuditEvent.PUBLICATION_PREPARED,
    )
    assert store.load_authoring("workspace-foreign", authoring.id) is None
    assert (
        store.list_for_workspace(
            "workspace-foreign",
            owner_actor_id=None,
            limit=10,
        )
        == ()
    )

    with pytest.raises(InsufficientPrivilege), psycopg.connect(urls.api) as connection:
        connection.execute(
            "INSERT INTO schemabridge_control.semantic_join_profile_jobs "
            "(job_id) VALUES ('profile_job_' || repeat('0', 64))"
        )
    with pytest.raises(ObjectNotInPrerequisiteState), psycopg.connect(urls.migrator) as connection:
        connection.execute(
            "UPDATE schemabridge_control.semantic_registry_change_audit "
            "SET actor_id = actor_id WHERE workspace_id = %s",
            (authoring.workspace_id,),
        )
    with pytest.raises(ObjectNotInPrerequisiteState), psycopg.connect(urls.api) as connection:
        connection.execute(
            "UPDATE schemabridge_control.semantic_registry_change_operations "
            "SET response_job = response_job "
            "WHERE workspace_id = %s AND idempotency_digest = %s",
            (authoring.workspace_id, request_digest),
        )


def test_registry_model_orphan_does_not_starve_valid_profile_work(
    registry_database: _RegistryDatabase,
) -> None:
    """A crash after enqueue but before binding is skipped until API recovery binds it."""

    urls = registry_database.urls
    _, authoring = _authoring(registry_database.target)
    orphan_scan_id = "scan_" + _digest("registry-model-orphan")
    valid_scan_id = "scan_" + _digest("registry-model-valid")
    now = datetime.now(UTC)
    with psycopg.connect(urls.migrator) as connection:
        connection.cursor().executemany(
            """
            INSERT INTO schemabridge_control.semantic_profile_sources (
                workspace_id, scan_id, source_kind, source_fingerprint, created_at
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (
                (
                    authoring.workspace_id,
                    orphan_scan_id,
                    "registry_model_join_profile_v1",
                    _digest("registry-model-orphan-source"),
                    now,
                ),
                (
                    authoring.workspace_id,
                    valid_scan_id,
                    "semantic_change_scan_v1",
                    _digest("registry-model-valid-source"),
                    now,
                ),
            ),
        )
    reconciler = PostgresSemanticJoinProfileQueue(urls.reconciler)
    orphan = reconciler.enqueue(
        authoring.workspace_id,
        orphan_scan_id,
        authoring.request.proposal,
        execution_target=authoring.request.execution_target,
        requested_at=now,
    ).job
    valid = reconciler.enqueue(
        authoring.workspace_id,
        valid_scan_id,
        authoring.request.proposal,
        execution_target=authoring.request.execution_target,
        requested_at=now,
    ).job

    claimed = PostgresSemanticJoinProfileQueue(
        urls.worker,
        application_name="schemabridge-control-worker",
    ).claim_next(
        worker_id="registry-profile-worker",
        lease_capability=CAPABILITY,
        lease_duration=timedelta(minutes=2),
    )

    assert claimed is not None
    assert claimed.job_id == valid.job_id
    assert claimed.job_id != orphan.job_id


def test_dependency_state_reader_is_narrow_for_api_and_publisher(
    registry_database: _RegistryDatabase,
) -> None:
    urls = registry_database.urls
    scope = SemanticRegistryScope(
        workspace_id="workspace-dependency-reader",
        catalog_scope="postgres.production",
        registry_id="reader_registry",
    )
    reconciler = PostgresSemanticChangeDependencyIndex(urls.reconciler)
    expected = reconciler.load_state(scope)
    publisher_dsn = _role_dsn("schemabridge_publisher", urls.database)

    assert PostgresSemanticChangeDependencyIndex(urls.api).load_state(scope) == expected
    assert PostgresSemanticChangeDependencyIndex(publisher_dsn).load_state(scope) == expected
    for dsn in (urls.api, publisher_dsn):
        with pytest.raises(InsufficientPrivilege), psycopg.connect(dsn) as connection:
            connection.execute(
                "SELECT * FROM schemabridge_control.semantic_dependency_index_states"
            )


def test_registry_model_profile_request_accepts_exact_endpoints_and_rejects_tamper(
    registry_database: _RegistryDatabase,
) -> None:
    harness = _Harness.create()
    registry = harness.versions.version.snapshot.registry
    base = _replacement_base(registry, harness.request.expected_base_registry)
    incident = base.incident_joins[0]
    proposal = SemanticJoinProfileProposal(
        connection_id=harness.targets.target.connection_id,
        proposal=_join_proposal(registry),
    ).proposal
    now = datetime.now(UTC)
    request = RegistryModelJoinProfileRequest.create(
        workspace_id=base.scope.workspace_id,
        change_id=harness.request.change_id,
        replacement_source=harness.source,
        base=base,
        incident_join_id=incident.contract.id,
        proposal=proposal,
        execution_target=SemanticJoinProfileTargetRef.from_target(registry_database.target),
        requested_at=now,
    )
    authoring = RegistryModelJoinProfileAuthoringRequest.create(
        id="replace-customer-profile-postgres",
        workspace_id=base.scope.workspace_id,
        owner_actor_id="analyst-profile-postgres",
        request=request,
        created_at=now,
    )
    audit = RegistryModelJoinProfileAuditRecord.create(
        authoring=authoring,
        event=RegistryModelJoinProfileAuditEvent.REQUEST_PERSISTED,
        actor_id=authoring.owner_actor_id,
        occurred_at=now,
    )
    _seed_registry_model_authority(registry_database, harness, base)
    publisher_dsn = _role_dsn(
        "schemabridge_publisher",
        registry_database.urls.database,
    )
    assert (
        PostgresRegistryModelReplacementSourceReader(publisher_dsn).load(
            base.scope.workspace_id,
            harness.source.proposal.id,
        )
        == harness.source
    )
    store = PostgresRegistryModelProfileStore(registry_database.urls.api)

    persisted = store.persist_request(
        authoring,
        audit,
        operation="request_model_join_profile",
        actor_id=authoring.owner_actor_id,
        idempotency_digest=_digest("registry-model-valid-request"),
        request_fingerprint=_digest("registry-model-valid-request-payload"),
    )

    assert persisted.authoring == authoring
    job = PostgresRegistryModelJoinProfileQueue(registry_database.urls.api).enqueue(authoring).job
    bound_audit = RegistryModelJoinProfileAuditRecord.create(
        authoring=authoring,
        event=RegistryModelJoinProfileAuditEvent.JOB_BOUND,
        actor_id=authoring.owner_actor_id,
        occurred_at=now,
        job_id=job.job_id,
    )

    def bind_job() -> object:
        return store.bind_job(
            authoring,
            job,
            bound_audit,
            idempotency_digest=_digest("registry-model-valid-request"),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        bindings = tuple(executor.map(lambda _: bind_job(), range(2)))
    assert sorted(item.replayed for item in bindings) == [False, True]  # type: ignore[attr-defined]

    tampered = authoring.model_dump(mode="json")
    tampered["id"] = "replace-customer-profile-tampered"
    tampered["fingerprint"] = _digest("registry-model-tampered-authoring")
    tampered_request = tampered["request"]
    tampered_request["scan_id"] = "scan_" + _digest("registry-model-tampered-scan")
    tampered_incident = tampered_request["base"]["incident_joins"][0]
    for endpoint in (
        tampered_incident["left"]["key"],
        tampered_incident["left"]["mapping"]["mapping"],
        tampered_incident["left"]["binding"],
        tampered_incident["contract"]["left_key"],
        tampered_request["proposal"]["left_key"],
    ):
        endpoint["physical_field"] = "sales.orders.unapproved_id"
    tampered_incident["left"]["binding"]["locator"]["field_path"] = ["unapproved_id"]
    with (
        pytest.raises(ObjectNotInPrerequisiteState),
        psycopg.connect(registry_database.urls.api) as connection,
    ):
        _insert_raw_model_profile_request(connection, authoring, tampered)

    with psycopg.connect(registry_database.urls.migrator) as connection:
        rows = connection.execute(
            """
            SELECT request_id
            FROM schemabridge_control.registry_model_join_profile_requests
            WHERE workspace_id = %s
            ORDER BY request_id
            """,
            (authoring.workspace_id,),
        ).fetchall()
    assert rows == [(authoring.id,)]

    owner = _principal(
        "analyst-outer-postgres",
        IdentityRole.ANALYST,
        authenticated_at=harness.clock.current - timedelta(minutes=1),
    )
    created = harness.create_use_case().execute(
        owner,
        harness.request,
        idempotency_key="registry-model-create-concurrency",
    )
    create_audit = harness.store.list_audit(
        created.draft.workspace_id,
        created.draft.id,
    )[-1]
    change_store = PostgresRegistryModelChangeStore(registry_database.urls.api)
    change_store.create(
        created.draft,
        create_audit,
        operation="create_model_change",
        actor_id=created.draft.owner_actor_id,
        idempotency_digest=_digest("registry-model-create-pg"),
        request_fingerprint=_digest("registry-model-create-pg-payload"),
    )
    harness.clock.current += timedelta(minutes=1)
    steward = _principal(
        "steward-outer-postgres",
        IdentityRole.STEWARD,
        authenticated_at=harness.clock.current - timedelta(minutes=1),
    )
    decided = harness.decide_use_case().execute(
        steward,
        created.draft.id,
        action=DecisionAction.APPROVE,
        expected_revision=created.draft.revision,
        confirmed_draft_fingerprint=created.draft.fingerprint,
        rationale="The complete replacement and all incident joins were reviewed.",
        idempotency_key="registry-model-decide-concurrency",
    )
    decision_audit = harness.store.list_audit(
        decided.draft.workspace_id,
        decided.draft.id,
    )[-1]

    def commit_decision() -> object:
        return change_store.commit_decision(
            decided.draft,
            decision_audit,
            expected_revision=created.draft.revision,
            operation="decide_model_change",
            actor_id="steward-outer-postgres",
            idempotency_digest=_digest("registry-model-decision-pg"),
            request_fingerprint=_digest("registry-model-decision-pg-payload"),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        decisions = tuple(executor.map(lambda _: commit_decision(), range(2)))
    assert sorted(item.replayed for item in decisions) == [False, True]  # type: ignore[attr-defined]

    altered = decided.draft.model_copy(
        update={"risks": ("A different response cannot reuse the same operation.",)}
    )
    with pytest.raises(RegistryModelChangePortError) as conflict:
        change_store.commit_decision(
            altered,
            decision_audit,
            expected_revision=created.draft.revision,
            operation="decide_model_change",
            actor_id="steward-outer-postgres",
            idempotency_digest=_digest("registry-model-decision-pg"),
            request_fingerprint=_digest("registry-model-decision-pg-payload"),
        )
    assert conflict.value.code is RegistryModelChangePortErrorCode.CONFLICT

    harness.clock.current += timedelta(minutes=1)
    publisher = _principal(
        "publisher-outer-postgres",
        IdentityRole.PUBLISHER,
        authenticated_at=harness.clock.current - timedelta(minutes=1),
    )
    prepared = harness.prepare_use_case().execute(
        publisher,
        decided.draft.id,
        expected_revision=decided.draft.revision,
        confirmed_draft_fingerprint=decided.draft.fingerprint,
        idempotency_key="registry-model-prepare-concurrency",
    )
    assert prepared.proposal is not None
    preparation_audit = harness.store.list_audit(
        prepared.draft.workspace_id,
        prepared.draft.id,
    )[-1]

    def commit_preparation() -> object:
        return change_store.commit_preparation(
            prepared.draft,
            prepared.proposal,  # type: ignore[arg-type]
            preparation_audit,
            expected_revision=decided.draft.revision,
            operation="prepare_model_change_publication",
            actor_id="publisher-outer-postgres",
            idempotency_digest=_digest("registry-model-preparation-pg"),
            request_fingerprint=_digest("registry-model-preparation-pg-payload"),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        preparations = tuple(executor.map(lambda _: commit_preparation(), range(2)))
    assert sorted(item.replayed for item in preparations) == [False, True]  # type: ignore[attr-defined]

    publication_proposal = prepared.proposal
    assert publication_proposal is not None
    proposal_reader = PostgresRegistryPublicationProposalReader(registry_database.urls.api)
    assert (
        proposal_reader.load_exact(
            publication_proposal.workspace_id,
            publication_proposal.id,
        )
        == publication_proposal
    )
    with psycopg.connect(registry_database.urls.migrator) as connection:
        source_row = connection.execute(
            """
            SELECT proposal_kind, proposal_fingerprint, target_version
            FROM schemabridge_control.registry_publication_sources
            WHERE workspace_id = %s AND proposal_id = %s
            """,
            (publication_proposal.workspace_id, publication_proposal.id),
        ).fetchone()
    assert source_row == (
        "replace_model_v1",
        publication_proposal.fingerprint,
        publication_proposal.target_registry_version,
    )

    active_reader = PostgresActiveRegistryPointerReader(registry_database.urls.api)
    active_before = active_reader.load_active(publication_proposal.scope)
    assert active_before is not None
    submitted_by = "publisher-model-publication-postgres"
    publication_job = create_registry_publication_job(
        publication_proposal,
        submitted_by=submitted_by,
        submitted_at=datetime.now(UTC),
        idempotency_digest=_digest("registry-model-publication-pg"),
        request_fingerprint=registry_publication_request_fingerprint(
            publication_proposal,
            submitted_by=submitted_by,
        ),
    )
    publication_api = PostgresRegistryPublicationJobStore(registry_database.urls.api)
    publication_publisher = PostgresRegistryPublicationJobStore(
        publisher_dsn,
        application_name="schemabridge-control-publisher",
    )

    submitted = publication_api.submit(publication_job)
    replayed_submission = publication_api.submit(publication_job)

    assert submitted.replayed is False
    assert replayed_submission.replayed is True
    assert replayed_submission.job == submitted.job
    with psycopg.connect(registry_database.urls.migrator) as connection:
        reservation_rows = connection.execute(
            """
            SELECT proposal_id, proposal_fingerprint
            FROM schemabridge_control.registry_publication_jobs
            WHERE workspace_id = %s
              AND catalog_scope = %s
              AND registry_id = %s
              AND target_version = %s
            """,
            (
                publication_proposal.scope.workspace_id,
                publication_proposal.scope.catalog_scope,
                publication_proposal.scope.registry_id,
                publication_proposal.target_registry_version,
            ),
        ).fetchall()
    assert reservation_rows == [(publication_proposal.id, publication_proposal.fingerprint)]

    preparing = publication_publisher.claim_next(
        worker_id="model-publication-worker-prepare",
        lease_capability=CAPABILITY,
        lease_duration=timedelta(seconds=60),
    )
    assert preparing is not None
    candidate = assemble_model_replacement_registry_version(
        publication_proposal,
        base=registry,
    )
    awaiting = publication_publisher.record_candidate(
        preparing.id,
        candidate,
        worker_id="model-publication-worker-prepare",
        lease_capability=CAPABILITY,
        fencing_token=preparing.last_fencing_token,
    )
    assert awaiting.status is RegistryPublicationJobStatus.AWAITING_APPROVAL

    with psycopg.connect(registry_database.urls.migrator) as connection:
        payload_row = connection.execute(
            """
            SELECT payload
            FROM schemabridge_control.registry_publication_jobs
            WHERE job_id = %s
            """,
            (awaiting.id,),
        ).fetchone()
        assert payload_row is not None
        publication_payload = payload_row[0]
        assert isinstance(publication_payload, dict)

        def witness_valid(payload: dict[str, object]) -> bool:
            row = connection.execute(
                """
                SELECT schemabridge_control.registry_model_publication_witness_valid(
                    %s, %s, %s
                )
                """,
                (
                    Jsonb(payload),
                    publication_proposal.id,
                    publication_proposal.fingerprint,
                ),
            ).fetchone()
            assert row is not None
            return bool(row[0])

        assert witness_valid(publication_payload) is True
        model_tamper = deepcopy(publication_payload)
        model_tamper["candidate"]["registry"]["logical_context"]["models"][1][  # type: ignore[index]
            "description"
        ] = "Tampered logical model"
        mapping_tamper = deepcopy(publication_payload)
        mapping_tamper["candidate"]["registry"]["mapping_set"]["mappings"][1][  # type: ignore[index]
            "mapping"
        ]["physical_field"] = "crm.customers.tampered_id"
        binding_tamper = deepcopy(publication_payload)
        binding_tamper["candidate"]["registry"]["physical_bindings"][1][  # type: ignore[index]
            "locator"
        ]["field_path"] = ["tampered_id"]
        incident_tamper = deepcopy(publication_payload)
        incident_tamper["candidate"]["registry"]["join_contracts"]["contracts"][0][  # type: ignore[index]
            "version"
        ] += 1
        assert tuple(
            witness_valid(payload)
            for payload in (
                model_tamper,
                mapping_tamper,
                binding_tamper,
                incident_tamper,
            )
        ) == (False, False, False, False)
        catalog_authority_row = connection.execute(
            """
            SELECT encode(sha256(convert_to(%s::jsonb::text, 'UTF8')), 'hex')
            """,
            (
                Jsonb(
                    publication_payload["candidate"]["registry"][  # type: ignore[index]
                        "physical_bindings"
                    ]
                ),
            ),
        ).fetchone()
        assert catalog_authority_row is not None
        expected_catalog_authority_fingerprint = str(catalog_authority_row[0])

    approved_at = datetime.now(UTC) - timedelta(seconds=1)
    authorization = RegistryPublicationAuthorization.create(
        candidate,
        actor_id="publisher-model-publication-approver",
        authenticated_at=approved_at - timedelta(minutes=1),
        approved_at=approved_at,
        expires_at=approved_at + timedelta(minutes=10),
        confirmation=(
            RegistryPublicationAuthorizationConfirmation.PUBLISH_EXACT_OBSERVED_REGISTRY_VERSION
        ),
    )
    approved = publication_api.authorize(
        awaiting.id,
        workspace_id=publication_proposal.workspace_id,
        expected_revision=awaiting.revision,
        authorization=authorization,
    )
    assert approved.status is RegistryPublicationJobStatus.APPROVED
    publishing = publication_publisher.claim_next(
        worker_id="model-publication-worker-publish",
        lease_capability=CAPABILITY,
        lease_duration=timedelta(seconds=60),
    )
    assert publishing is not None
    observed_at = datetime.now(UTC)
    receipt = PublicationReadbackReceipt(
        candidate_id=candidate.id,
        candidate_fingerprint=candidate.fingerprint,
        scope=candidate.scope,
        registry_version=candidate.registry.version,
        registry_fingerprint=candidate.registry.fingerprint,
        target=candidate.target,
        observed_authorization_id=authorization.id,
        related_asset_urns=observed_registry_related_asset_urns(candidate.registry),
        observed_at=observed_at,
    )
    completed_publication = publication_publisher.complete(
        publishing.id,
        receipt,
        worker_id="model-publication-worker-publish",
        lease_capability=CAPABILITY,
        fencing_token=publishing.last_fencing_token,
    )
    assert completed_publication.status is RegistryPublicationJobStatus.ACTIVATION_READY

    control = PostgresRegistryControlStore(
        registry_database.urls.runtime,
        {"v1": b"model-publication-audit-key-0123456789"},
        "v1",
    )
    handoff = control.load_activation_ready_handoff(
        publication_proposal.scope,
        publication_proposal.target_registry_version,
    )
    assert handoff is not None
    assert handoff.job_id == completed_publication.id
    assert handoff.source_proposal_id == publication_proposal.id
    assert handoff.source_proposal_fingerprint == publication_proposal.fingerprint
    assert handoff.candidate_id == candidate.id
    assert handoff.candidate_fingerprint == candidate.fingerprint
    assert handoff.target_registry_version == candidate.registry.version
    assert handoff.target_registry_fingerprint == candidate.registry.fingerprint
    assert handoff.target_registry_urn == candidate.target
    assert handoff.attempt_authorization_id == authorization.id
    assert handoff.observed_authorization_id == authorization.id
    assert handoff.catalog_authority_fingerprint == expected_catalog_authority_fingerprint
    assert handoff.observed_at == receipt.observed_at
    assert active_reader.load_active(publication_proposal.scope) == active_before


def _seed_registry_model_authority(
    registry_database: _RegistryDatabase,
    harness: _Harness,
    base: object,
) -> None:
    from schemabridge.domain.registry_model_changes import RegistryModelReplacementBase

    if not isinstance(base, RegistryModelReplacementBase):
        raise TypeError("registry model integration base is invalid")
    source = harness.source
    proposal = source.proposal
    initial = _approved_draft_from_proposal(proposal)
    ready = SemanticOnboardingDraft(
        **{
            **initial.model_dump(mode="python"),
            "owner_actor_id": source.owner_actor_id,
            "revision": proposal.draft_revision + 1,
            "status": SemanticOnboardingStatus.READY_FOR_PUBLICATION,
            "prepared_proposal_id": proposal.id,
            "prepared_proposal_fingerprint": proposal.fingerprint,
            "prepared_by": proposal.prepared_by,
            "updated_at": proposal.prepared_at,
        }
    )
    scope = base.scope
    registry = harness.versions.version.snapshot.registry
    now = datetime.now(UTC)
    admin_dsn = (
        "postgresql://postgres:local-only-not-a-secret@127.0.0.1:55434/"
        f"{registry_database.urls.database}"
    )
    bindings = tuple(
        {
            "workspace_id": endpoint.binding.workspace_id,
            "connection_id": endpoint.binding.connection_id.root,
            "generation": endpoint.binding.catalog_generation,
            "generation_fingerprint": endpoint.binding.catalog_generation_fingerprint,
            "asset_id": endpoint.binding.locator.asset.asset_id.root,
            "field_path": tuple(endpoint.binding.locator.field_path),
            "physical_field": endpoint.binding.physical_field.root,
            "physical_type": endpoint.binding.physical_type.value,
            "asset_fingerprint": endpoint.binding.asset_metadata_fingerprint,
            "field_fingerprint": endpoint.binding.field_metadata_fingerprint,
        }
        for endpoint in (base.incident_joins[0].left, base.incident_joins[0].right)
    )
    generation = bindings[0]["generation"]
    generation_fingerprint = bindings[0]["generation_fingerprint"]
    connection_id = bindings[0]["connection_id"]
    refresh_id = "refresh_registry_model_7"
    with psycopg.connect(admin_dsn) as connection:
        connection.execute("SET session_replication_role = replica")
        connection.execute(
            """
            UPDATE schemabridge_control.catalog_connections
            SET catalog_scope = %s, status = 'enabled', updated_at = %s
            WHERE workspace_id = %s AND connection_id = %s
            """,
            (scope.catalog_scope, now, scope.workspace_id, connection_id),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.catalog_refresh_runs (
                workspace_id, connection_id, refresh_id, refresh_mode, status,
                base_generation, target_generation, request_fingerprint,
                idempotency_digest, requested_by_actor_id, source_checkpoint,
                source_page_number, source_page_fingerprint, staged_asset_count,
                staged_field_count, source_complete, inventory_fingerprint,
                requested_at, updated_at, completed_at
            ) VALUES (
                %s, %s, %s, 'full', 'completed', 1, %s, %s, %s,
                'sb_catalog_admin_v1', 'complete', 1, %s, %s, %s, true, %s,
                %s, %s, %s
            )
            """,
            (
                scope.workspace_id,
                connection_id,
                refresh_id,
                generation,
                _digest("registry-model-refresh-request"),
                _digest("registry-model-refresh-idempotency"),
                _digest("registry-model-refresh-page"),
                len(bindings),
                len(bindings),
                generation_fingerprint,
                now,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.catalog_generations (
                workspace_id, connection_id, generation, refresh_id,
                base_generation, refresh_mode, status, asset_count, field_count,
                inventory_fingerprint, created_at, completed_at, retain_until,
                source_identity_fingerprint, catalog_identity_fingerprint,
                type_contract_fingerprint
            ) VALUES (%s, %s, %s, %s, 1, 'full', 'completed', %s, %s, %s,
                      %s, %s, %s, %s, %s, %s)
            """,
            (
                scope.workspace_id,
                connection_id,
                generation,
                refresh_id,
                len(bindings),
                len(bindings),
                generation_fingerprint,
                now,
                now,
                now + timedelta(days=30),
                registry_database.target.source_identity_fingerprint,
                registry_database.target.catalog_identity_fingerprint,
                registry_database.target.type_contract_fingerprint,
            ),
        )
        for binding in bindings:
            schema_name, table_name, _ = str(binding["physical_field"]).split(".", 2)
            asset_key = _digest(str(binding["asset_id"]))
            field_name = str(binding["field_path"][-1])
            connection.execute(
                """
                INSERT INTO schemabridge_control.catalog_assets (
                    workspace_id, connection_id, generation, asset_key, asset_id,
                    qualified_name, asset_sort_key, platform, environment,
                    database_name, schema_name, table_name, display_name,
                    description, field_count, metadata_fingerprint, observed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'postgres', 'PROD',
                          'integration', %s, %s, %s, 'Synthetic model asset', 1, %s, %s)
                """,
                (
                    scope.workspace_id,
                    connection_id,
                    generation,
                    asset_key,
                    binding["asset_id"],
                    f"{schema_name}.{table_name}",
                    f"{schema_name}.{table_name}",
                    schema_name,
                    table_name,
                    table_name,
                    binding["asset_fingerprint"],
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO schemabridge_control.catalog_fields (
                    workspace_id, connection_id, generation, asset_key, field_key,
                    field_path, field_sort_key, field_name, ordinal_position,
                    native_type, normalized_type, nullable, is_part_of_key,
                    tags, glossary_terms, description, metadata_fingerprint, observed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, 'text', %s,
                          false, true, ARRAY['governed'], ARRAY['semantic'],
                          'Synthetic model field', %s, %s)
                """,
                (
                    scope.workspace_id,
                    connection_id,
                    generation,
                    asset_key,
                    _digest(str(binding["physical_field"])),
                    list(binding["field_path"]),
                    field_name,
                    field_name,
                    binding["physical_type"],
                    binding["field_fingerprint"],
                    now,
                ),
            )
        connection.execute(
            """
            UPDATE schemabridge_control.catalog_connections
            SET active_generation = %s, active_generation_fingerprint = %s,
                active_generation_completed_at = %s, updated_at = %s
            WHERE workspace_id = %s AND connection_id = %s
            """,
            (
                generation,
                generation_fingerprint,
                now,
                now,
                scope.workspace_id,
                connection_id,
            ),
        )
        previous_transition: str | None = None
        for generation_number in range(1, 6):
            transition_id = (
                "registry-transition-5"
                if generation_number == 5
                else f"registry-transition-{generation_number}"
            )
            connection.execute(
                """
                INSERT INTO schemabridge_control.registry_activation_transitions (
                    transition_id, workspace_id, catalog_scope, registry_id,
                    generation, action, expected_generation,
                    expected_registry_version, expected_registry_fingerprint,
                    expected_transition_id, target_registry_version,
                    target_registry_fingerprint, target_registry_urn,
                    target_publication_approval_id, rollback_transition_id,
                    proposal_fingerprint, approval_id, actor, approved_at,
                    decision_ids_json, committed_at, payload_json
                ) VALUES (%s, %s, %s, %s, %s, 'activate', %s, %s, %s, %s,
                          %s, %s, %s, %s, NULL, %s, %s, 'publisher-model', %s,
                          %s, %s, '{}'::jsonb)
                """,
                (
                    transition_id,
                    scope.workspace_id,
                    scope.catalog_scope,
                    scope.registry_id,
                    generation_number,
                    generation_number - 1,
                    None if previous_transition is None else registry.version,
                    None if previous_transition is None else registry.fingerprint,
                    previous_transition,
                    registry.version,
                    registry.fingerprint,
                    datahub_registry_document_urn(scope, registry.version),
                    f"publication-model-{generation_number}",
                    _digest(f"registry-model-transition-{generation_number}"),
                    f"approval-model-{generation_number}",
                    now,
                    Jsonb(["decision-active-v3"]),
                    now,
                ),
            )
            previous_transition = transition_id
        connection.execute(
            """
            INSERT INTO schemabridge_control.registry_active_pointers (
                workspace_id, catalog_scope, registry_id, generation,
                registry_version, registry_fingerprint, registry_target,
                transition_id, activated_by, activated_at, decision_ids_json
            ) VALUES (%s, %s, %s, 5, %s, %s, %s, 'registry-transition-5',
                      'publisher-model', %s, %s)
            """,
            (
                scope.workspace_id,
                scope.catalog_scope,
                scope.registry_id,
                registry.version,
                registry.fingerprint,
                datahub_registry_document_urn(scope, registry.version),
                now,
                Jsonb(["decision-active-v3"]),
            ),
        )
        dependency = base.dependency_context.dependency_index
        connection.execute(
            """
            INSERT INTO schemabridge_control.semantic_dependency_index_states (
                workspace_id, catalog_scope, registry_id, registry_generation,
                registry_version, registry_fingerprint, pointer_transition_id,
                watermark, index_fingerprint, complete, indexed_at
            ) VALUES (%s, %s, %s, 5, %s, %s, 'registry-transition-5', %s, %s, true, %s)
            """,
            (
                scope.workspace_id,
                scope.catalog_scope,
                scope.registry_id,
                registry.version,
                registry.fingerprint,
                dependency.watermark,
                dependency.fingerprint,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.semantic_onboarding_drafts (
                workspace_id, draft_id, owner_actor_id, revision, status,
                fingerprint, payload, prepared_proposal_id,
                prepared_proposal_fingerprint, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                ready.workspace_id,
                ready.id,
                ready.owner_actor_id,
                ready.revision,
                ready.status.value,
                ready.fingerprint,
                Jsonb(ready.model_dump(mode="json")),
                ready.prepared_proposal_id,
                ready.prepared_proposal_fingerprint,
                ready.created_at,
                ready.updated_at,
            ),
        )
        for decision in source.decisions:
            connection.execute(
                """
                INSERT INTO schemabridge_control.semantic_onboarding_decisions (
                    workspace_id, decision_id, draft_id, resulting_revision,
                    actor_id, target_kind, target_id, status, payload, decided_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    decision.workspace_id,
                    decision.id,
                    decision.draft_id,
                    decision.resulting_revision,
                    decision.actor_id,
                    decision.target_kind.value,
                    decision.target_id,
                    decision.status.value,
                    Jsonb(decision.model_dump(mode="json")),
                    decision.decided_at,
                ),
            )
        connection.execute(
            """
            INSERT INTO schemabridge_control.semantic_onboarding_proposals (
                workspace_id, proposal_id, draft_id, draft_revision,
                target_registry_version, fingerprint, prepared_by, payload, prepared_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                proposal.workspace_id,
                proposal.id,
                proposal.draft_id,
                proposal.draft_revision,
                proposal.target_registry_version,
                proposal.fingerprint,
                proposal.prepared_by,
                Jsonb(proposal.model_dump(mode="json")),
                proposal.prepared_at,
            ),
        )


def _insert_raw_model_profile_request(
    connection: psycopg.Connection[object],
    authoring: RegistryModelJoinProfileAuthoringRequest,
    payload: dict[str, object],
) -> None:
    request = authoring.request
    connection.execute(
        """
        INSERT INTO schemabridge_control.registry_model_join_profile_requests (
            workspace_id, request_id, change_id, scan_id,
            source_proposal_id, source_proposal_fingerprint,
            catalog_scope, registry_id, base_registry_version,
            base_registry_fingerprint, base_fingerprint, incident_join_id,
            connection_id, proposal_fingerprint, execution_target_fingerprint,
            authoring_fingerprint, requested_by, payload, requested_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            authoring.workspace_id,
            payload["id"],
            request.change_id,
            payload["request"]["scan_id"],  # type: ignore[index]
            request.replacement_source.proposal.id,
            request.replacement_source.proposal.fingerprint,
            request.base.scope.catalog_scope,
            request.base.scope.registry_id,
            request.base.base_registry.registry_version,
            request.base.base_registry.registry_fingerprint,
            request.base.fingerprint,
            request.incident_join_id,
            request.connection_id.root,
            semantic_join_profile_proposal_fingerprint(request.bound_proposal),
            request.execution_target.target_fingerprint,
            payload["fingerprint"],
            authoring.owner_actor_id,
            Jsonb(payload),
            request.requested_at,
        ),
    )


def _authoring(
    target: GovernedExecutionTarget,
) -> tuple[GovernedSemanticRegistrySnapshot, RegistryJoinProfileAuthoringRequest]:
    raw_base, base_identity = _two_model_base()
    assert isinstance(raw_base, GovernedSemanticRegistrySnapshot)
    scope = _onboarding_proposal().scope
    proposal = SemanticJoinProfileProposal(
        connection_id=target.connection_id,
        proposal=_join_proposal(raw_base),
    )
    evidence = resolve_registry_join_base_evidence(
        scope=scope,
        base=raw_base,
        base_registry=base_identity,
        proposal=proposal,
    )
    requested_at = datetime.now(UTC)
    request = RegistryJoinProfileRequest.create(
        scope=scope,
        base_evidence=evidence,
        proposal=proposal,
        execution_target=SemanticJoinProfileTargetRef.from_target(target),
        requested_at=requested_at,
    )
    return raw_base, RegistryJoinProfileAuthoringRequest.create(
        id="join-change-postgres",
        workspace_id=scope.workspace_id,
        owner_actor_id="analyst-postgres",
        request=request,
        created_at=requested_at,
    )


def _audit(
    authoring: RegistryJoinProfileAuthoringRequest,
    *,
    event: RegistryChangeAuditEvent,
    actor_id: str,
    occurred_at: datetime,
    source_revision: int,
    resulting_revision: int,
    previous_fingerprint: str | None,
    resulting_fingerprint: str,
    profile_request_fingerprint: str | None = None,
    profile_job_id: str | None = None,
    decision_id: str | None = None,
    proposal_id: str | None = None,
) -> RegistryChangeAuditRecord:
    return RegistryChangeAuditRecord.create(
        workspace_id=authoring.workspace_id,
        change_id=authoring.id,
        event=event,
        actor_id=actor_id,
        occurred_at=occurred_at,
        source_revision=source_revision,
        resulting_revision=resulting_revision,
        previous_fingerprint=previous_fingerprint,
        resulting_fingerprint=resulting_fingerprint,
        profile_request_fingerprint=profile_request_fingerprint,
        profile_job_id=profile_job_id,
        decision_id=decision_id,
        proposal_id=proposal_id,
    )
