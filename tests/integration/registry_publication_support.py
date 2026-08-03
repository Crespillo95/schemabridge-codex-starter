"""Reusable PostgreSQL fixtures for an exact M34 -> M23 activation handoff."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg.types.json import Jsonb
from tests.integration.connector_target_support import ensure_catalog_connector_target
from tests.unit.test_registry_publication_v2 import (
    _approved_draft_from_proposal,
    _proposal,
)

from schemabridge.adapters.control_plane.postgres_registry_publication import (
    PostgresRegistryPublicationJobStore,
)
from schemabridge.application.ports.registry_control import (
    RegistryControlError,
    RegistryControlErrorCode,
)
from schemabridge.domain.registry_control import (
    ActiveRegistryPointer,
    GovernedRegistryVersion,
    RegistryVersionTrust,
    registry_projection_fingerprint,
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
    create_registry_publication_job,
    registry_publication_request_fingerprint,
)
from schemabridge.domain.semantic_onboarding import (
    OnboardingRegistryBase,
    PreparedSemanticOnboardingProposal,
    SemanticOnboardingDraft,
    SemanticOnboardingStatus,
)
from schemabridge.domain.semantic_registry import (
    GovernedPhysicalBinding,
    GovernedSemanticRegistrySnapshot,
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
)

_CAPABILITY = "registry-publication-capability-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"


@dataclass(slots=True)
class PublishedVersionReader:
    """Mutable strict reader used while an integration test publishes an additive chain."""

    versions: dict[int, GovernedRegistryVersion] = field(default_factory=dict)

    def add(self, version: GovernedRegistryVersion) -> None:
        self.versions[version.snapshot.registry.version] = version

    def load_version(
        self,
        scope: SemanticRegistryScope,
        version: int,
    ) -> GovernedRegistryVersion:
        loaded = self.versions.get(version)
        if loaded is None or loaded.snapshot.scope != scope:
            raise RegistryControlError(
                RegistryControlErrorCode.VERSION_UNAVAILABLE,
                "strict integration registry version is unavailable",
            )
        return loaded


@dataclass(slots=True)
class M34PublicationFixture:
    """Publish a bounded additive v2 chain through the real durable M34 stores."""

    migrator_dsn: str
    api_dsn: str
    publisher_dsn: str
    scope: SemanticRegistryScope
    max_versions: int
    catalog_generation: int = 1
    catalog_generation_fingerprint: str = field(init=False)
    _base: GovernedSemanticRegistrySnapshot | None = field(default=None, init=False)
    _published_count: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not 1 <= self.max_versions <= 10:
            raise ValueError("M34 integration fixture version bound is invalid")
        self.catalog_generation_fingerprint = _digest(
            f"m34-catalog:{self.scope.workspace_id}:{self.scope.catalog_scope}"
        )
        planned_bindings = tuple(
            assemble_publishable_registry_version(
                self._proposal_for(index, OnboardingRegistryBase()),
                base=None,
            ).registry.physical_bindings[0]
            for index in range(1, self.max_versions + 1)
        )
        _seed_current_catalog(
            self.migrator_dsn,
            self.scope,
            planned_bindings,
            self.catalog_generation_fingerprint,
        )

    def publish_next(
        self,
        previous_pointer: ActiveRegistryPointer | None,
    ) -> GovernedRegistryVersion:
        index = self._published_count + 1
        if index > self.max_versions:
            raise ValueError("M34 integration fixture exhausted its version bound")
        if self._base is None:
            if previous_pointer is not None:
                raise ValueError("initial M34 fixture publication requires an empty pointer")
            base_contract = OnboardingRegistryBase()
        else:
            if (
                previous_pointer is None
                or previous_pointer.registry_version != self._base.version
                or previous_pointer.registry_fingerprint != self._base.fingerprint
            ):
                raise ValueError("additive M34 fixture publication requires its exact active base")
            base_contract = OnboardingRegistryBase(
                registry_version=self._base.version,
                registry_fingerprint=self._base.fingerprint,
                activation_generation=previous_pointer.generation,
                active_pointer_fingerprint=registry_projection_fingerprint(previous_pointer),
            )

        proposal = self._proposal_for(index, base_contract)
        candidate = assemble_publishable_registry_version(proposal, base=self._base)
        _insert_ready_proposal(self.migrator_dsn, proposal)
        receipt = _publish_candidate(
            self.api_dsn,
            self.publisher_dsn,
            proposal,
            candidate,
        )
        version = GovernedRegistryVersion(
            snapshot=ScopedSemanticRegistrySnapshot(
                scope=self.scope,
                registry=candidate.registry,
            ),
            publication_approval_id=receipt.observed_authorization_id,
            trust=RegistryVersionTrust.STRICT,
        )
        self._base = candidate.registry
        self._published_count = index
        return version

    def _proposal_for(
        self,
        index: int,
        base: OnboardingRegistryBase,
    ) -> PreparedSemanticOnboardingProposal:
        namespace = _digest(self.scope.workspace_id)[:16]
        model_id = f"ActivationModel{index}"
        observed_urn = (
            f"urn:li:dataset:(urn:li:dataPlatform:postgres,activation-{namespace}-{index},PROD)"
        )
        return _proposal(
            model_id=model_id,
            logical_field=f"{model_id}.id",
            physical_field=f"activation_{index}.records.id",
            proposal_id=f"activation-proposal-{namespace}-v{index}",
            draft_id=f"activation-draft-{namespace}-v{index}",
            model_decision=f"decision-model-{namespace}-v{index}",
            mapping_decision=f"decision-mapping-{namespace}-v{index}",
            observed_urn=observed_urn,
            asset_id=observed_urn,
            base=base,
            scope=self.scope,
            catalog_generation=self.catalog_generation,
            catalog_generation_fingerprint=self.catalog_generation_fingerprint,
        )


def _publish_candidate(
    api_dsn: str,
    publisher_dsn: str,
    proposal: PreparedSemanticOnboardingProposal,
    candidate: PublishableRegistryVersion,
) -> PublicationReadbackReceipt:
    api = PostgresRegistryPublicationJobStore(api_dsn)
    publisher = PostgresRegistryPublicationJobStore(
        publisher_dsn,
        application_name="schemabridge-control-publisher",
    )
    submitted_at = datetime.now(UTC)
    job = create_registry_publication_job(
        proposal,
        submitted_by="publisher-integration",
        submitted_at=submitted_at,
        idempotency_digest=_digest(f"m34-idempotency:{proposal.id}"),
        request_fingerprint=registry_publication_request_fingerprint(
            proposal,
            submitted_by="publisher-integration",
        ),
    )
    submitted = api.submit(job).job
    preparing = publisher.claim_next(
        worker_id="publisher-integration-worker",
        lease_capability=_CAPABILITY,
        lease_duration=timedelta(seconds=60),
    )
    if preparing is None or preparing.id != submitted.id:
        raise AssertionError("M34 integration fixture claimed another publication")
    awaiting = publisher.record_candidate(
        preparing.id,
        candidate,
        worker_id="publisher-integration-worker",
        lease_capability=_CAPABILITY,
        fencing_token=preparing.last_fencing_token,
    )
    approved_at = datetime.now(UTC)
    authorization = RegistryPublicationAuthorization.create(
        candidate,
        actor_id="publisher-integration",
        authenticated_at=approved_at - timedelta(minutes=1),
        approved_at=approved_at,
        expires_at=approved_at + timedelta(minutes=10),
        confirmation=(
            RegistryPublicationAuthorizationConfirmation.PUBLISH_EXACT_OBSERVED_REGISTRY_VERSION
        ),
    )
    api.authorize(
        awaiting.id,
        workspace_id=proposal.workspace_id,
        expected_revision=awaiting.revision,
        authorization=authorization,
    )
    publishing = publisher.claim_next(
        worker_id="publisher-integration-worker",
        lease_capability=_CAPABILITY,
        lease_duration=timedelta(seconds=60),
    )
    if publishing is None or publishing.id != submitted.id:
        raise AssertionError("M34 integration fixture could not reclaim its authorization")
    receipt = PublicationReadbackReceipt(
        candidate_id=candidate.id,
        candidate_fingerprint=candidate.fingerprint,
        scope=candidate.scope,
        registry_version=candidate.registry.version,
        registry_fingerprint=candidate.registry.fingerprint,
        target=candidate.target,
        observed_authorization_id=authorization.id,
        related_asset_urns=observed_registry_related_asset_urns(candidate.registry),
        observed_at=datetime.now(UTC),
    )
    completed = publisher.complete(
        publishing.id,
        receipt,
        worker_id="publisher-integration-worker",
        lease_capability=_CAPABILITY,
        fencing_token=publishing.last_fencing_token,
    )
    if completed.receipt != receipt:
        raise AssertionError("M34 integration fixture lost its exact read-back receipt")
    return receipt


def _insert_ready_proposal(
    dsn: str,
    proposal: PreparedSemanticOnboardingProposal,
) -> None:
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


def _seed_current_catalog(
    dsn: str,
    scope: SemanticRegistryScope,
    bindings: tuple[GovernedPhysicalBinding, ...],
    generation_fingerprint: str,
) -> None:
    if not bindings:
        raise ValueError("M34 integration fixture requires physical bindings")
    connection_ids = {binding.connection_id for binding in bindings}
    if len(connection_ids) != 1:
        raise ValueError("M34 integration fixture supports one exact connection")
    connection_id = bindings[0].connection_id
    observed_at = datetime.now(UTC)
    refresh_id = f"refresh-m34-{_digest(scope.workspace_id)[:24]}"
    capability_digest = _digest(f"catalog-capability:{scope.workspace_id}")
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
            """,
            (scope.workspace_id, observed_at, observed_at),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.catalog_connections (
                workspace_id, connection_id, display_name, source_kind,
                catalog_scope, environment, platform_instance, status,
                registration_fingerprint, idempotency_digest,
                created_by_actor_id, created_at, updated_at
            ) VALUES (
                %s, %s, 'M34 activation regression', 'synthetic',
                %s, 'PROD', 'integration', 'enabled', %s, %s,
                'sb_catalog_admin_v1', %s, %s
            )
            """,
            (
                scope.workspace_id,
                connection_id.root,
                scope.catalog_scope,
                _digest(f"registration:{scope.workspace_id}"),
                _digest(f"registration-idempotency:{scope.workspace_id}"),
                observed_at,
                observed_at,
            ),
        )
        connection.commit()
        target = ensure_catalog_connector_target(
            dsn,
            workspace_id=scope.workspace_id,
            connection_id=connection_id,
        )
        target_label = _digest(f"{scope.workspace_id}|{connection_id.root}")[:20]
        route_fingerprint = _digest(f"catalog-test-route:{target_label}")
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
                scope.workspace_id,
                connection_id.root,
                refresh_id,
                _digest(f"refresh:{scope.workspace_id}"),
                _digest(f"refresh-idempotency:{scope.workspace_id}"),
                observed_at,
                observed_at,
            ),
        )
        connection.execute(
            """
            UPDATE schemabridge_control.catalog_refresh_runs
            SET status = 'leased', fencing_token = 1,
                lease_owner_id = 'catalog-indexer-m34',
                lease_capability_digest = %s,
                lease_acquired_at = %s, lease_heartbeat_at = %s,
                lease_expires_at = %s, updated_at = %s
            WHERE workspace_id = %s AND connection_id = %s AND refresh_id = %s
            """,
            (
                capability_digest,
                observed_at,
                observed_at,
                observed_at + timedelta(minutes=5),
                observed_at + timedelta(milliseconds=1),
                scope.workspace_id,
                connection_id.root,
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
                scope.workspace_id,
                connection_id.root,
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
            (scope.workspace_id, connection_id.root, refresh_id, observed_at),
        )
        connection.execute(
            """
            UPDATE schemabridge_control.catalog_refresh_runs
            SET status = 'staging', updated_at = %s
            WHERE workspace_id = %s AND connection_id = %s AND refresh_id = %s
            """,
            (
                observed_at + timedelta(milliseconds=2),
                scope.workspace_id,
                connection_id.root,
                refresh_id,
            ),
        )
        for binding in bindings:
            schema_name, table_name, _ = binding.physical_field.root.split(".", 2)
            asset_key = _digest(binding.observed_datahub_asset_urn)
            field_key = _digest(binding.physical_field.root)
            field_name = binding.locator.field_path[-1]
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
                    'Synthetic M34 activation asset', 1, %s, %s
                )
                """,
                (
                    scope.workspace_id,
                    connection_id.root,
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
                    %s, %s, 1, %s, %s, %s, %s, %s, 1,
                    'text', %s, false, true, ARRAY['governed'], ARRAY['semantic'],
                    'Synthetic M34 activation field', %s, %s
                )
                """,
                (
                    scope.workspace_id,
                    connection_id.root,
                    asset_key,
                    field_key,
                    list(binding.locator.field_path),
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
            SET source_checkpoint = 'complete', source_page_number = 1,
                source_page_fingerprint = %s, staged_asset_count = %s,
                staged_field_count = %s, source_complete = true, updated_at = %s
            WHERE workspace_id = %s AND connection_id = %s AND refresh_id = %s
            """,
            (
                _digest(f"catalog-page:{scope.workspace_id}"),
                len(bindings),
                len(bindings),
                observed_at + timedelta(milliseconds=3),
                scope.workspace_id,
                connection_id.root,
                refresh_id,
            ),
        )
        connection.execute(
            """
            UPDATE schemabridge_control.catalog_generations
            SET status = 'completed', asset_count = %s, field_count = %s,
                inventory_fingerprint = %s, completed_at = %s, retain_until = %s
            WHERE workspace_id = %s AND connection_id = %s AND generation = 1
            """,
            (
                len(bindings),
                len(bindings),
                generation_fingerprint,
                observed_at + timedelta(milliseconds=4),
                observed_at + timedelta(days=1),
                scope.workspace_id,
                connection_id.root,
            ),
        )
        connection.execute(
            """
            UPDATE schemabridge_control.catalog_refresh_runs
            SET status = 'completed', lease_capability_digest = NULL,
                lease_expires_at = NULL, inventory_fingerprint = %s,
                completed_at = %s, updated_at = %s
            WHERE workspace_id = %s AND connection_id = %s AND refresh_id = %s
            """,
            (
                generation_fingerprint,
                observed_at + timedelta(milliseconds=5),
                observed_at + timedelta(milliseconds=5),
                scope.workspace_id,
                connection_id.root,
                refresh_id,
            ),
        )
        connection.execute(
            """
            UPDATE schemabridge_control.catalog_connections
            SET active_generation = 1, active_generation_fingerprint = %s,
                active_generation_completed_at = %s, updated_at = %s
            WHERE workspace_id = %s AND connection_id = %s
            """,
            (
                generation_fingerprint,
                observed_at + timedelta(milliseconds=5),
                observed_at + timedelta(milliseconds=6),
                scope.workspace_id,
                connection_id.root,
            ),
        )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
