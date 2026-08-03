"""PostgreSQL proof for M34 reservations, fencing, roles, and durable recovery."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.errors import InsufficientPrivilege
from psycopg.types.json import Jsonb
from tests.integration.connector_target_support import ensure_catalog_connector_target
from tests.unit.test_registry_publication_v2 import (
    _approved_draft_from_proposal,
    _proposal,
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
from schemabridge.application.ports.registry_control import (
    RegistryControlError,
    RegistryControlErrorCode,
)
from schemabridge.application.ports.registry_publication import (
    RegistryPublicationStoreError,
    RegistryPublicationStoreErrorCode,
)
from schemabridge.application.registry_control import (
    CommitRegistryActivation,
    PrepareRegistryActivation,
    PrepareRegistryActivationApproval,
)
from schemabridge.domain.registry_control import (
    GovernedRegistryVersion,
    RegistryActivationConfirmation,
    RegistryVersionTrust,
    build_registry_activation_transition,
    build_registry_projection_outbox,
)
from schemabridge.domain.registry_publication import (
    PublicationReadbackReceipt,
    PublishableRegistryVersion,
    RegistryPublicationAuthorization,
    RegistryPublicationAuthorizationConfirmation,
    assemble_publishable_registry_version,
    observed_registry_related_asset_urns,
)
from schemabridge.domain.registry_publication_jobs import (
    RegistryPublicationJobStatus,
    create_registry_publication_job,
    registry_publication_request_fingerprint,
)
from schemabridge.domain.semantic_onboarding import (
    PreparedSemanticOnboardingProposal,
    SemanticOnboardingDraft,
    SemanticOnboardingStatus,
)
from schemabridge.domain.semantic_registry import (
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations/control_plane"
ADMIN_DSN = "postgresql://postgres:local-only-not-a-secret@127.0.0.1:55434/postgres"
CAPABILITY = "registry-publication-capability-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
AUDIT_KEYS = {"v1": b"control-audit-key-0123456789-abcdef"}


@dataclass(frozen=True, slots=True)
class _Urls:
    database: str
    migrator: str
    runtime: str
    api: str
    publisher: str


@dataclass(frozen=True, slots=True)
class _ExactVersionReader:
    version: GovernedRegistryVersion

    def load_version(
        self,
        scope: SemanticRegistryScope,
        version: int,
    ) -> GovernedRegistryVersion:
        if (
            self.version.snapshot.scope != scope
            or self.version.snapshot.registry.version != version
        ):
            raise RegistryControlError(
                RegistryControlErrorCode.VERSION_UNAVAILABLE,
                "exact integration registry version is unavailable",
            )
        return self.version


def _role_dsn(role: str, database: str) -> str:
    return f"postgresql://{role}:{role}@127.0.0.1:55434/{database}"


@pytest.fixture(scope="module")
def publication_database() -> Iterator[_Urls]:
    database = f"schemabridge_m34_{uuid4().hex[:12]}"
    admin_dsn = os.environ.get("SCHEMABRIDGE_TEST_CONTROL_ADMIN_DATABASE_URL", ADMIN_DSN)
    roles = (
        "schemabridge_migrator",
        "schemabridge_runtime",
        "schemabridge_reconciler",
        "schemabridge_api",
        "schemabridge_worker",
        "schemabridge_publisher",
        "schemabridge_catalog",
        "schemabridge_observer",
        "schemabridge_backup",
    )
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        for role in roles:
            if (
                connection.execute(
                    "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s",
                    (role,),
                ).fetchone()
                is None
            ):
                pytest.fail(f"{role} role is missing; restart the control-plane test service")
        connection.execute(
            sql.SQL("CREATE DATABASE {} OWNER schemabridge_migrator").format(
                sql.Identifier(database)
            )
        )
        connection.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database))
        )
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO ").format(sql.Identifier(database))
            + sql.SQL(", ").join(sql.Identifier(role) for role in roles)
        )
    urls = _Urls(
        database=database,
        migrator=_role_dsn("schemabridge_migrator", database),
        runtime=_role_dsn("schemabridge_runtime", database),
        api=_role_dsn("schemabridge_api", database),
        publisher=_role_dsn("schemabridge_publisher", database),
    )
    try:
        migrated = PostgresControlPlaneMigrator(urls.migrator, MIGRATIONS).migrate()
        assert migrated.inspection.current_version == 14
        yield urls
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )


def test_two_phase_publication_is_durable_fenced_and_activation_ready(
    publication_database: _Urls,
) -> None:
    urls = publication_database
    proposal = _proposal(catalog_generation=1)
    _insert_ready_proposal(urls.migrator, proposal)
    reader = PostgresRegistryPublicationProposalReader(urls.api)
    assert reader.load_exact(proposal.workspace_id, proposal.id) == proposal
    api = PostgresRegistryPublicationJobStore(urls.api)
    publisher = PostgresRegistryPublicationJobStore(
        urls.publisher,
        application_name="schemabridge-control-publisher",
    )
    submitted_at = datetime.now(UTC)
    request_fingerprint = registry_publication_request_fingerprint(
        proposal,
        submitted_by="publisher-a",
    )
    job = create_registry_publication_job(
        proposal,
        submitted_by="publisher-a",
        submitted_at=submitted_at,
        idempotency_digest=hashlib.sha256(b"m34-idempotency-key").hexdigest(),
        request_fingerprint=request_fingerprint,
    )

    submitted = api.submit(job)
    replay = api.submit(job)

    assert submitted.replayed is False
    assert replay.replayed is True
    assert replay.job == submitted.job
    preparing = publisher.claim_next(
        worker_id="publisher-worker-a",
        lease_capability=CAPABILITY,
        lease_duration=timedelta(seconds=60),
    )
    assert preparing is not None
    assert preparing.status is RegistryPublicationJobStatus.LEASED
    candidate = assemble_publishable_registry_version(proposal, base=None)
    awaiting = publisher.record_candidate(
        preparing.id,
        candidate,
        worker_id="publisher-worker-a",
        lease_capability=CAPABILITY,
        fencing_token=preparing.last_fencing_token,
    )
    assert awaiting.status is RegistryPublicationJobStatus.AWAITING_APPROVAL
    approved_at = datetime.now(UTC)
    authorization = RegistryPublicationAuthorization.create(
        candidate,
        actor_id="publisher-a",
        authenticated_at=approved_at - timedelta(minutes=1),
        approved_at=approved_at,
        expires_at=approved_at + timedelta(minutes=10),
        confirmation=(
            RegistryPublicationAuthorizationConfirmation.PUBLISH_EXACT_OBSERVED_REGISTRY_VERSION
        ),
    )
    approved = api.authorize(
        awaiting.id,
        workspace_id=proposal.workspace_id,
        expected_revision=awaiting.revision,
        authorization=authorization,
    )
    publishing = publisher.claim_next(
        worker_id="publisher-worker-b",
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
    completed = publisher.complete(
        publishing.id,
        receipt,
        worker_id="publisher-worker-b",
        lease_capability=CAPABILITY,
        fencing_token=publishing.last_fencing_token,
    )

    assert approved.status is RegistryPublicationJobStatus.APPROVED
    assert completed.status is RegistryPublicationJobStatus.ACTIVATION_READY
    assert api.load(proposal.workspace_id, completed.id) == completed
    with psycopg.connect(urls.migrator) as connection:
        row = connection.execute(
            """
            SELECT lease_capability_digest, payload::text,
                   (SELECT count(*) FROM schemabridge_control.registry_publication_events
                    WHERE job_id = jobs.job_id)
            FROM schemabridge_control.registry_publication_jobs AS jobs
            WHERE job_id = %s
            """,
            (completed.id,),
        ).fetchone()
    assert row is not None
    assert row[0] is None
    assert CAPABILITY not in str(row[1])
    assert int(row[2]) == 6

    version = GovernedRegistryVersion(
        snapshot=ScopedSemanticRegistrySnapshot(
            scope=candidate.scope,
            registry=candidate.registry,
        ),
        publication_approval_id=receipt.observed_authorization_id,
        trust=RegistryVersionTrust.STRICT,
    )
    versions = _ExactVersionReader(version)
    control = PostgresRegistryControlStore(
        urls.runtime,
        AUDIT_KEYS,
        "v1",
    )
    assert control.load_active(candidate.scope) is None
    with pytest.raises(RegistryControlError) as no_catalog:
        PrepareRegistryActivation(control, versions, candidate.scope).execute(1)
    assert no_catalog.value.code is RegistryControlErrorCode.ACTIVATION_NOT_READY
    assert control.load_active(candidate.scope) is None

    _seed_current_catalog(urls.migrator, candidate)
    activation = PrepareRegistryActivation(control, versions, candidate.scope).execute(1)
    assert activation.activation_ready_handoff is not None
    assert activation.activation_ready_handoff.job_id == completed.id
    assert activation.activation_ready_handoff.source_proposal_fingerprint == proposal.fingerprint
    activation_approval = PrepareRegistryActivationApproval().execute(
        activation,
        actor="integration-registry-operator",
        approved_at=observed_at + timedelta(seconds=1),
        confirmation=RegistryActivationConfirmation.ACTIVATE_APPROVED_REGISTRY_VERSION,
    )
    transition = build_registry_activation_transition(
        activation,
        activation_approval,
        previous_pointer=None,
        committed_at=observed_at + timedelta(seconds=2),
    )
    outbox = build_registry_projection_outbox(transition)

    _age_catalog_authority(urls.migrator, candidate, stale=True)
    with pytest.raises(RegistryControlError) as stale_catalog:
        control.commit_transition(transition, outbox)
    assert stale_catalog.value.code is RegistryControlErrorCode.ACTIVATION_NOT_READY
    assert control.load_active(candidate.scope) is None
    assert control.list_transitions(candidate.scope) == ()
    assert control.load_pending_outbox(candidate.scope) is None

    _age_catalog_authority(urls.migrator, candidate, stale=False)
    committed = CommitRegistryActivation(control, versions).execute(
        activation,
        activation_approval,
        committed_at=observed_at + timedelta(seconds=2),
    )
    assert committed.transition.active_pointer.registry_version == 1
    assert control.load_active(candidate.scope) == committed.transition.active_pointer


def test_target_reservation_and_role_boundaries_fail_closed(
    publication_database: _Urls,
) -> None:
    urls = publication_database
    _ensure_target_reserved(urls)
    proposal = _proposal(proposal_id="proposal-competing-v1", draft_id="competing-onboarding")
    _insert_ready_proposal(urls.migrator, proposal)
    api = PostgresRegistryPublicationJobStore(urls.api)
    job = create_registry_publication_job(
        proposal,
        submitted_by="publisher-a",
        submitted_at=datetime.now(UTC),
        idempotency_digest="b" * 64,
        request_fingerprint=registry_publication_request_fingerprint(
            proposal,
            submitted_by="publisher-a",
        ),
    )
    with pytest.raises(RegistryPublicationStoreError) as conflict:
        api.submit(job)
    assert conflict.value.code is RegistryPublicationStoreErrorCode.TARGET_RESERVED

    with psycopg.connect(urls.api) as connection, pytest.raises(InsufficientPrivilege):
        connection.execute(
            """
                UPDATE schemabridge_control.registry_publication_jobs
                SET candidate_fingerprint = %s
                WHERE job_id = %s
                """,
            ("c" * 64, job.id),
        )
    with psycopg.connect(urls.publisher) as connection, pytest.raises(InsufficientPrivilege):
        connection.execute(
            "UPDATE schemabridge_control.registry_active_pointers SET generation = generation"
        )
    for denied_dsn in (urls.api, urls.publisher):
        with psycopg.connect(denied_dsn) as connection, pytest.raises(InsufficientPrivilege):
            connection.execute(
                """
                SELECT *
                FROM schemabridge_control.load_registry_activation_ready_handoff(
                    %s, %s, %s, 1, 900
                )
                """,
                (
                    proposal.workspace_id,
                    proposal.scope.catalog_scope,
                    proposal.scope.registry_id,
                ),
            )
    with psycopg.connect(urls.runtime) as connection, pytest.raises(InsufficientPrivilege):
        connection.execute(
            "SELECT payload FROM schemabridge_control.registry_publication_jobs LIMIT 1"
        )


def _insert_ready_proposal(dsn: str, proposal: PreparedSemanticOnboardingProposal) -> None:
    unprepared = _approved_draft_from_proposal(proposal)
    ready = SemanticOnboardingDraft.model_validate(
        {
            **unprepared.model_dump(mode="python"),
            "revision": proposal.draft_revision + 1,
            "status": SemanticOnboardingStatus.READY_FOR_PUBLICATION,
            "prepared_proposal_id": proposal.id,
            "prepared_proposal_fingerprint": proposal.fingerprint,
            "prepared_by": proposal.prepared_by,
            "updated_at": proposal.prepared_at,
        }
    )
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.semantic_onboarding_drafts (
                workspace_id, draft_id, owner_actor_id, revision, status, fingerprint,
                payload, prepared_proposal_id, prepared_proposal_fingerprint,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (workspace_id, draft_id) DO NOTHING
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
        connection.execute(
            """
            INSERT INTO schemabridge_control.semantic_onboarding_proposals (
                workspace_id, proposal_id, draft_id, draft_revision,
                target_registry_version, fingerprint, prepared_by, payload, prepared_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (workspace_id, proposal_id) DO NOTHING
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


def _seed_current_catalog(dsn: str, candidate: PublishableRegistryVersion) -> None:
    binding = candidate.registry.physical_bindings[0]
    schema_name, table_name, field_name = binding.physical_field.root.split(".", 2)
    asset_key = hashlib.sha256(binding.observed_datahub_asset_urn.encode()).hexdigest()
    field_key = hashlib.sha256(binding.physical_field.root.encode()).hexdigest()
    refresh_id = "refresh-registry-activation-v1"
    capability_digest = hashlib.sha256(b"catalog-lease-capability-v1").hexdigest()
    observed_at = datetime.now(UTC)
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.tenant_capacity_policies (
                workspace_id, connection_limit, asset_limit, field_limit,
                api_requests_per_minute, api_window_seconds,
                nonterminal_job_limit, catalog_cursor_ttl_seconds,
                generation_retention_seconds, version, updated_by,
                created_at, updated_at
            ) VALUES (
                %s, 10, 10000, 100000, 1000, 60, 100, 900, 3600, 1,
                'sb_platform_admin_v1', %s, %s
            )
            ON CONFLICT (workspace_id) DO NOTHING
            """,
            (candidate.scope.workspace_id, observed_at, observed_at),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.catalog_connections (
                workspace_id, connection_id, display_name, source_kind,
                catalog_scope, environment, platform_instance, status,
                registration_fingerprint, idempotency_digest,
                created_by_actor_id, created_at, updated_at
            ) VALUES (
                %s, %s, 'Activation authority fixture', 'synthetic',
                %s, 'PROD', 'integration', 'enabled', %s, %s,
                'sb_catalog_admin_v1', %s, %s
            )
            """,
            (
                candidate.scope.workspace_id,
                binding.connection_id.root,
                candidate.scope.catalog_scope,
                hashlib.sha256(b"activation-registration").hexdigest(),
                hashlib.sha256(b"activation-registration-idempotency").hexdigest(),
                observed_at,
                observed_at,
            ),
        )
        connection.commit()
        target = ensure_catalog_connector_target(
            dsn,
            workspace_id=candidate.scope.workspace_id,
            connection_id=binding.connection_id,
        )
        target_label = hashlib.sha256(
            f"{candidate.scope.workspace_id}|{binding.connection_id.root}".encode()
        ).hexdigest()[:20]
        route_fingerprint = hashlib.sha256(
            f"catalog-test-route:{target_label}".encode()
        ).hexdigest()
        connection.execute(
            """
            INSERT INTO schemabridge_control.catalog_refresh_runs (
                workspace_id, connection_id, refresh_id, refresh_mode, status,
                target_generation, request_fingerprint, idempotency_digest,
                requested_by_actor_id, requested_at, updated_at
            ) VALUES (
                %s, %s, %s, 'full', 'requested', NULL, %s, %s,
                'sb_catalog_admin_v1', %s, %s
            )
            """,
            (
                candidate.scope.workspace_id,
                binding.connection_id.root,
                refresh_id,
                hashlib.sha256(b"activation-refresh").hexdigest(),
                hashlib.sha256(b"activation-refresh-idempotency").hexdigest(),
                observed_at,
                observed_at,
            ),
        )
        connection.execute(
            """
            UPDATE schemabridge_control.catalog_refresh_runs
            SET status = 'leased',
                fencing_token = 1,
                lease_owner_id = 'catalog-indexer-activation',
                lease_capability_digest = %s,
                lease_acquired_at = %s,
                lease_heartbeat_at = %s,
                lease_expires_at = %s,
                updated_at = %s
            WHERE workspace_id = %s AND connection_id = %s AND refresh_id = %s
            """,
            (
                capability_digest,
                observed_at,
                observed_at,
                observed_at + timedelta(minutes=5),
                observed_at + timedelta(milliseconds=1),
                candidate.scope.workspace_id,
                binding.connection_id.root,
                refresh_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.catalog_refresh_semantic_bindings (
                workspace_id, connection_id, refresh_id, contract_version,
                route_revision, route_fingerprint, target_fingerprint,
                source_identity_fingerprint, catalog_identity_fingerprint,
                type_contract_fingerprint, bound_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                candidate.scope.workspace_id,
                binding.connection_id.root,
                refresh_id,
                target.contract_version,
                target.route_revision,
                route_fingerprint,
                target.target_fingerprint,
                target.source_identity_fingerprint,
                target.catalog_identity_fingerprint,
                target.type_contract_fingerprint,
                observed_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.catalog_generations (
                workspace_id, connection_id, generation, refresh_id,
                base_generation, refresh_mode, status, created_at
            ) VALUES (%s, %s, 1, %s, NULL, 'full', 'staging', %s)
            """,
            (
                candidate.scope.workspace_id,
                binding.connection_id.root,
                refresh_id,
                observed_at,
            ),
        )
        connection.execute(
            """
            UPDATE schemabridge_control.catalog_refresh_runs
            SET status = 'staging', updated_at = %s
            WHERE workspace_id = %s AND connection_id = %s AND refresh_id = %s
            """,
            (
                observed_at + timedelta(milliseconds=2),
                candidate.scope.workspace_id,
                binding.connection_id.root,
                refresh_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.catalog_assets (
                workspace_id, connection_id, generation, asset_key, asset_id,
                qualified_name, asset_sort_key, platform, environment,
                database_name, schema_name, table_name, display_name,
                description, field_count, metadata_fingerprint, observed_at
            ) VALUES (
                %s, %s, 1, %s, %s, %s, %s, 'postgres', 'PROD',
                'integration', %s, %s, %s,
                'Synthetic activation authority', 1, %s, %s
            )
            """,
            (
                candidate.scope.workspace_id,
                binding.connection_id.root,
                asset_key,
                binding.observed_datahub_asset_urn,
                f"{schema_name}.{table_name}",
                f"{schema_name}.{table_name}",
                schema_name,
                table_name,
                table_name,
                binding.asset_metadata_fingerprint,
                observed_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.catalog_fields (
                workspace_id, connection_id, generation, asset_key, field_key,
                field_path, field_sort_key, field_name, ordinal_position,
                native_type, normalized_type, nullable, is_part_of_key,
                tags, glossary_terms, description, metadata_fingerprint, observed_at
            ) VALUES (
                %s, %s, 1, %s, %s, ARRAY[%s], %s, %s, 1,
                'text', %s, false, true, ARRAY['governed'], ARRAY['semantic'],
                'Synthetic activation field', %s, %s
            )
            """,
            (
                candidate.scope.workspace_id,
                binding.connection_id.root,
                asset_key,
                field_key,
                field_name,
                field_name,
                field_name,
                binding.physical_type.value,
                binding.field_metadata_fingerprint,
                observed_at,
            ),
        )
        connection.execute(
            """
            UPDATE schemabridge_control.catalog_refresh_runs
            SET source_checkpoint = 'complete',
                source_page_number = 1,
                source_page_fingerprint = %s,
                staged_asset_count = 1,
                staged_field_count = 1,
                source_complete = true,
                updated_at = %s
            WHERE workspace_id = %s AND connection_id = %s AND refresh_id = %s
            """,
            (
                hashlib.sha256(b"activation-page").hexdigest(),
                observed_at + timedelta(milliseconds=3),
                candidate.scope.workspace_id,
                binding.connection_id.root,
                refresh_id,
            ),
        )
        connection.execute(
            """
            UPDATE schemabridge_control.catalog_generations
            SET status = 'completed', asset_count = 1, field_count = 1,
                inventory_fingerprint = %s, completed_at = %s,
                retain_until = %s
            WHERE workspace_id = %s AND connection_id = %s AND generation = 1
            """,
            (
                binding.catalog_generation_fingerprint,
                observed_at + timedelta(milliseconds=4),
                observed_at + timedelta(days=1),
                candidate.scope.workspace_id,
                binding.connection_id.root,
            ),
        )
        connection.execute(
            """
            UPDATE schemabridge_control.catalog_refresh_runs
            SET status = 'completed',
                lease_capability_digest = NULL,
                lease_expires_at = NULL,
                inventory_fingerprint = %s,
                completed_at = %s,
                updated_at = %s
            WHERE workspace_id = %s AND connection_id = %s AND refresh_id = %s
            """,
            (
                binding.catalog_generation_fingerprint,
                observed_at + timedelta(milliseconds=5),
                observed_at + timedelta(milliseconds=5),
                candidate.scope.workspace_id,
                binding.connection_id.root,
                refresh_id,
            ),
        )
        connection.execute(
            """
            UPDATE schemabridge_control.catalog_connections
            SET active_generation = 1,
                active_generation_fingerprint = %s,
                active_generation_completed_at = %s,
                updated_at = %s
            WHERE workspace_id = %s AND connection_id = %s
            """,
            (
                binding.catalog_generation_fingerprint,
                observed_at + timedelta(milliseconds=5),
                observed_at + timedelta(milliseconds=6),
                candidate.scope.workspace_id,
                binding.connection_id.root,
            ),
        )


def _age_catalog_authority(
    dsn: str,
    candidate: PublishableRegistryVersion,
    *,
    stale: bool,
) -> None:
    binding = candidate.registry.physical_bindings[0]
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """
            UPDATE schemabridge_control.catalog_connections
            SET active_generation_completed_at = CASE
                    WHEN %s THEN clock_timestamp() - interval '1 hour'
                    ELSE clock_timestamp()
                END,
                updated_at = clock_timestamp()
            WHERE workspace_id = %s AND connection_id = %s
            """,
            (
                stale,
                candidate.scope.workspace_id,
                binding.connection_id.root,
            ),
        )


def _ensure_target_reserved(urls: _Urls) -> None:
    with psycopg.connect(urls.migrator) as connection:
        existing = connection.execute(
            "SELECT 1 FROM schemabridge_control.registry_publication_jobs LIMIT 1"
        ).fetchone()
    if existing is not None:
        return
    proposal = _proposal()
    _insert_ready_proposal(urls.migrator, proposal)
    job = create_registry_publication_job(
        proposal,
        submitted_by="publisher-a",
        submitted_at=datetime.now(UTC),
        idempotency_digest="a" * 64,
        request_fingerprint=registry_publication_request_fingerprint(
            proposal,
            submitted_by="publisher-a",
        ),
    )
    PostgresRegistryPublicationJobStore(urls.api).submit(job)
