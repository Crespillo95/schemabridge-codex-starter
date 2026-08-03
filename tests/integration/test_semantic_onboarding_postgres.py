"""PostgreSQL proof for exact M33 catalog evidence and durable onboarding state."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from tests.integration.connector_target_support import (
    acquire_catalog_refresh_route,
    ensure_catalog_connector_target,
)

from schemabridge.adapters.catalog.postgres_inventory import (
    PostgresCatalogConnectionStore,
)
from schemabridge.adapters.catalog.postgres_refresh import PostgresCatalogRefreshStore
from schemabridge.adapters.catalog.postgres_semantic_onboarding import (
    PostgresSemanticOnboardingCatalogEvidence,
)
from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.adapters.storage.postgres_semantic_onboarding import (
    PostgresSemanticOnboardingStore,
)
from schemabridge.application.ports.semantic_onboarding import (
    SemanticOnboardingPortError,
    SemanticOnboardingPortErrorCode,
)
from schemabridge.application.semantic_onboarding import (
    CreateSemanticOnboardingDraft,
    DecideSemanticOnboarding,
    PrepareSemanticOnboardingPublication,
    SemanticOnboardingError,
    SemanticOnboardingErrorCode,
)
from schemabridge.application.semantic_onboarding_authorization import (
    SemanticOnboardingAuthorizationPolicy,
)
from schemabridge.domain.catalog_inventory import (
    CatalogAssetId,
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogConnectionRegistration,
    CatalogRefreshCommand,
    CatalogRefreshMode,
    CatalogSourceAsset,
    CatalogSourceChange,
    CatalogSourceChangeKind,
    CatalogSourceField,
    CatalogSourcePage,
)
from schemabridge.domain.concepts import CanonicalType, LogicalFieldRef, LogicalModelRef
from schemabridge.domain.decisions import ApprovalStatus, DecisionAction
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)
from schemabridge.domain.mappings import ConfidenceScore
from schemabridge.domain.request_context import LogicalFieldRole
from schemabridge.domain.semantic_onboarding import (
    CreateSemanticOnboardingRequest,
    OnboardingEvidence,
    OnboardingEvidenceKind,
    OnboardingRegistryBase,
    PreflightSemanticOnboardingRequest,
    SemanticFieldDefinition,
    SemanticMappingProposal,
    SemanticModelDefinition,
    SemanticModelProposal,
    SemanticOnboardingAuditEvent,
    SemanticOnboardingAuditRecord,
    SemanticOnboardingDecision,
    SemanticOnboardingDraft,
    SemanticOnboardingMappingInput,
    SemanticOnboardingPreflight,
    SemanticOnboardingPreflightSelection,
    SemanticOnboardingStatus,
    SemanticOnboardingTargetKind,
)
from schemabridge.domain.semantic_registry import (
    PhysicalValueType,
    SemanticRegistryScope,
    datahub_registry_document_urn,
)
from schemabridge.domain.transformations import IdentityStep, TransformationPlan

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations/control_plane"
ADMIN_DSN = "postgresql://postgres:local-only-not-a-secret@127.0.0.1:55434/postgres"
CAPABILITY = "0123456789abcdef" * 4
ASSET_CUSTOMER_ID = CatalogAssetId("catalog-customer-asset")
ASSET_ORDER_ID = CatalogAssetId("urn:li:dataset:(urn:li:dataPlatform:postgres,sales.orders,PROD)")
ASSET_CUSTOMER_FP = "a" * 64
ASSET_ORDER_FP = "b" * 64
FIELD_CUSTOMER_FP = "c" * 64
FIELD_ORDER_FP = "d" * 64


@dataclass(frozen=True, slots=True)
class _DatabaseUrls:
    database: str
    migrator: str
    api: str
    catalog: str
    backup: str


@dataclass(frozen=True, slots=True)
class _CatalogFixture:
    urls: _DatabaseUrls
    workspace_id: str
    connection_id: CatalogConnectionId
    scope: SemanticRegistryScope
    generation: int
    generation_fingerprint: str
    now: datetime


@dataclass(frozen=True, slots=True)
class _Clock:
    value: datetime

    def now(self) -> datetime:
        return self.value


@dataclass(frozen=True, slots=True)
class _EmptyRegistryBase:
    def load(self, scope: SemanticRegistryScope) -> OnboardingRegistryBase:
        del scope
        return OnboardingRegistryBase()


def _admin_dsn() -> str:
    return os.environ.get("SCHEMABRIDGE_TEST_CONTROL_ADMIN_DATABASE_URL", ADMIN_DSN)


def _role_dsn(role: str, database: str) -> str:
    return f"postgresql://{role}:{role}@127.0.0.1:55434/{database}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def onboarding_catalog(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_CatalogFixture]:
    database = f"schemabridge_m33_{uuid4().hex[:12]}"
    urls = _DatabaseUrls(
        database=database,
        migrator=_role_dsn("schemabridge_migrator", database),
        api=_role_dsn("schemabridge_api", database),
        catalog=_role_dsn("schemabridge_catalog", database),
        backup=_role_dsn("schemabridge_backup", database),
    )
    with psycopg.connect(_admin_dsn(), autocommit=True) as connection:
        for role in (
            "schemabridge_migrator",
            "schemabridge_api",
            "schemabridge_catalog",
            "schemabridge_backup",
        ):
            if (
                connection.execute(
                    "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s",
                    (role,),
                ).fetchone()
                is None
            ):
                pytest.fail(f"{role} role is missing; start the control-plane test service")
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
                "GRANT CONNECT ON DATABASE {} TO "
                "schemabridge_migrator, schemabridge_api, "
                "schemabridge_catalog, schemabridge_backup"
            ).format(sql.Identifier(database))
        )
    try:
        v12_migrations = tmp_path_factory.mktemp("m33-control-v12")
        for version in range(1, 13):
            source = next(MIGRATIONS.glob(f"{version:04d}_*.sql"))
            shutil.copyfile(source, v12_migrations / source.name)
        previous = PostgresControlPlaneMigrator(urls.migrator, v12_migrations).migrate()
        assert previous.inspection.current_version == 12
        migrated = PostgresControlPlaneMigrator(urls.migrator, MIGRATIONS).migrate()
        assert migrated.applied_versions == (13, 14)
        assert migrated.inspection.current_version == 14
        workspace_id = f"workspace-m33-{uuid4().hex[:12]}"
        connection_id = CatalogConnectionId("warehouse-m33")
        catalog_scope = "postgres.production"
        requested_at = datetime.now(UTC)
        with psycopg.connect(urls.migrator) as connection:
            connection.execute(
                """
                INSERT INTO schemabridge_control.tenant_capacity_policies (
                    workspace_id, connection_limit, asset_limit, field_limit,
                    api_requests_per_minute, nonterminal_job_limit, version,
                    updated_by, created_at, updated_at
                ) VALUES (%s, 10, 10000, 100000, 100, 100, 1, %s, %s, %s)
                """,
                (
                    workspace_id,
                    "actor-platform-m33",
                    requested_at,
                    requested_at,
                ),
            )
        PostgresCatalogConnectionStore(urls.api).register(
            CatalogConnectionRegistration(
                workspace_id=workspace_id,
                connection_id=connection_id,
                display_name="M33 synthetic warehouse",
                kind=CatalogConnectionKind.SYNTHETIC,
                environment="PROD",
                catalog_scope=catalog_scope,
                requested_by="actor-platform-m33",
                requested_at=requested_at,
                idempotency_digest=_digest("m33-register"),
            )
        )
        target = ensure_catalog_connector_target(
            urls.migrator,
            workspace_id=workspace_id,
            connection_id=connection_id,
        )
        api_refreshes = PostgresCatalogRefreshStore(
            urls.api,
            application_name="schemabridge-control-api",
        )
        catalog_refreshes = PostgresCatalogRefreshStore(urls.catalog)
        requested = api_refreshes.request(
            CatalogRefreshCommand(
                workspace_id=workspace_id,
                connection_id=connection_id,
                mode=CatalogRefreshMode.FULL,
                requested_by="actor-platform-m33",
                requested_at=requested_at,
                idempotency_digest=_digest("m33-refresh"),
            )
        )
        claimed = catalog_refreshes.claim_next(
            indexer_id="catalog:m33-integration",
            lease_capability=CAPABILITY,
            lease_duration=timedelta(minutes=5),
        )
        assert claimed is not None and claimed.lease is not None
        assert claimed.refresh_id == requested.refresh.refresh_id
        acquire_catalog_refresh_route(
            urls.catalog,
            workspace_id=workspace_id,
            connection_id=connection_id,
            refresh_id=claimed.refresh_id,
            indexer_id="catalog:m33-integration",
            lease_capability=CAPABILITY,
            fencing_token=claimed.lease.fencing_token,
        )
        staging = catalog_refreshes.begin_staging(
            workspace_id,
            claimed.refresh_id,
            indexer_id="catalog:m33-integration",
            lease_capability=CAPABILITY,
            fencing_token=claimed.lease.fencing_token,
        )
        assets = (
            CatalogSourceAsset(
                asset_id=ASSET_CUSTOMER_ID,
                qualified_name="sales.customers",
                display_name="Customers",
                platform="postgres",
                environment="PROD",
                schema_name="sales",
                fields=(
                    CatalogSourceField(
                        field_path=("customer_id",),
                        native_type="text",
                        normalized_type=PhysicalValueType.STRING,
                        is_part_of_key=True,
                        metadata_fingerprint=FIELD_CUSTOMER_FP,
                    ),
                ),
                metadata_fingerprint=ASSET_CUSTOMER_FP,
            ),
            CatalogSourceAsset(
                asset_id=ASSET_ORDER_ID,
                qualified_name="sales.orders",
                display_name="Orders",
                platform="postgres",
                environment="PROD",
                schema_name="sales",
                fields=(
                    CatalogSourceField(
                        field_path=("order_id",),
                        native_type="text",
                        normalized_type=PhysicalValueType.STRING,
                        is_part_of_key=True,
                        metadata_fingerprint=FIELD_ORDER_FP,
                    ),
                ),
                metadata_fingerprint=ASSET_ORDER_FP,
            ),
        )
        catalog_refreshes.persist_page(
            workspace_id,
            claimed.refresh_id,
            indexer_id="catalog:m33-integration",
            lease_capability=CAPABILITY,
            fencing_token=claimed.lease.fencing_token,
            page=CatalogSourcePage.create(
                mode=CatalogRefreshMode.FULL,
                sequence=1,
                changes=tuple(
                    CatalogSourceChange(
                        kind=CatalogSourceChangeKind.UPSERT_ASSET,
                        asset=asset,
                    )
                    for asset in assets
                ),
                next_checkpoint=None,
                source_complete=True,
            ),
        )
        completed = catalog_refreshes.complete(
            workspace_id,
            claimed.refresh_id,
            indexer_id="catalog:m33-integration",
            lease_capability=CAPABILITY,
            fencing_token=claimed.lease.fencing_token,
            expected_base_generation=staging.base_generation,
            expected_contract_version=target.contract_version,
            expected_route_revision=target.route_revision,
            expected_target_fingerprint=target.target_fingerprint,
        )
        assert completed.catalog_fingerprint is not None
        now = datetime.now(UTC)
        yield _CatalogFixture(
            urls=urls,
            workspace_id=workspace_id,
            connection_id=connection_id,
            scope=SemanticRegistryScope(
                workspace_id=workspace_id,
                catalog_scope=catalog_scope,
                registry_id="orders_registry",
            ),
            generation=completed.target_generation,
            generation_fingerprint=completed.catalog_fingerprint,
            now=now,
        )
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )


def _mapping_inputs() -> tuple[SemanticOnboardingMappingInput, ...]:
    evidence = (
        OnboardingEvidence(
            kind=OnboardingEvidenceKind.NAME_SIMILARITY,
            detail="The normalized names match exactly.",
        ),
        OnboardingEvidence(
            kind=OnboardingEvidenceKind.DECLARED_KEY,
            detail="The retained catalog marks this exact field as a declared key.",
        ),
    )
    return (
        SemanticOnboardingMappingInput(
            id="mapping-order-id",
            logical_field=LogicalFieldRef("Order.order_id"),
            asset_id=ASSET_ORDER_ID,
            field_path=("order_id",),
            expected_asset_metadata_fingerprint=ASSET_ORDER_FP,
            expected_field_metadata_fingerprint=FIELD_ORDER_FP,
            physical_field=PhysicalFieldRef("sales.orders.order_id"),
            confidence=ConfidenceScore(1.0),
            evidence=evidence,
            transformation_plan=TransformationPlan(steps=(IdentityStep(),)),
        ),
        SemanticOnboardingMappingInput(
            id="mapping-customer-id",
            logical_field=LogicalFieldRef("Order.customer_id"),
            asset_id=ASSET_CUSTOMER_ID,
            field_path=("customer_id",),
            expected_asset_metadata_fingerprint=ASSET_CUSTOMER_FP,
            expected_field_metadata_fingerprint=FIELD_CUSTOMER_FP,
            physical_field=PhysicalFieldRef("sales.customers.customer_id"),
            confidence=ConfidenceScore(0.95),
            evidence=evidence,
            transformation_plan=TransformationPlan(steps=(IdentityStep(),)),
        ),
    )


def _request(fixture: _CatalogFixture) -> CreateSemanticOnboardingRequest:
    preflight = _preflight(fixture)
    mappings = tuple(
        mapping.model_copy(
            update={
                "asset_id": observation.locator.asset.asset_id,
                "field_path": observation.locator.field_path,
                "expected_asset_metadata_fingerprint": (observation.asset_metadata_fingerprint),
                "expected_field_metadata_fingerprint": (observation.field_metadata_fingerprint),
                "physical_field": observation.physical_field,
            }
        )
        for mapping, observation in zip(
            _mapping_inputs(),
            preflight.observations,
            strict=True,
        )
    )
    return CreateSemanticOnboardingRequest(
        draft_id="orders-onboarding",
        connection_id=fixture.connection_id,
        catalog_generation=preflight.catalog_generation,
        catalog_generation_fingerprint=preflight.catalog_generation_fingerprint,
        expected_base_registry=preflight.base_registry,
        confirmed_preflight_fingerprint=preflight.fingerprint,
        model=SemanticModelDefinition(
            id=LogicalModelRef("Order"),
            description="Governed order model for the M33 PostgreSQL integration.",
            fields=(
                SemanticFieldDefinition(
                    id=LogicalFieldRef("Order.customer_id"),
                    canonical_type=CanonicalType.STRING,
                    role=LogicalFieldRole.IDENTIFIER,
                    definition="Stable customer identifier attached to an order.",
                ),
                SemanticFieldDefinition(
                    id=LogicalFieldRef("Order.order_id"),
                    canonical_type=CanonicalType.STRING,
                    role=LogicalFieldRole.IDENTIFIER,
                    definition="Stable order identifier.",
                ),
            ),
        ),
        mappings=mappings,
    )


def _preflight(fixture: _CatalogFixture) -> SemanticOnboardingPreflight:
    selections = tuple(
        SemanticOnboardingPreflightSelection(
            asset_id=item.asset_id,
            field_path=item.field_path,
        )
        for item in _mapping_inputs()
    )
    return PostgresSemanticOnboardingCatalogEvidence(
        fixture.urls.api,
        stale_after_seconds=3_600,
    ).resolve_active(
        fixture.scope,
        PreflightSemanticOnboardingRequest(
            connection_id=fixture.connection_id,
            selections=selections,
        ),
    )


def _store_draft(
    fixture: _CatalogFixture,
    *,
    scope: SemanticRegistryScope,
    draft_id: str,
    base_registry: OnboardingRegistryBase | None = None,
) -> SemanticOnboardingDraft:
    resolved = PostgresSemanticOnboardingCatalogEvidence(
        fixture.urls.api,
        stale_after_seconds=3_600,
    ).resolve_exact(
        scope,
        fixture.connection_id,
        fixture.generation,
        fixture.generation_fingerprint,
        _mapping_inputs(),
    )
    model = SemanticModelDefinition(
        id=LogicalModelRef("Order"),
        description="Governed order model used to prove atomic M33 authority checks.",
        fields=(
            SemanticFieldDefinition(
                id=LogicalFieldRef("Order.customer_id"),
                canonical_type=CanonicalType.STRING,
                role=LogicalFieldRole.IDENTIFIER,
                definition="Stable customer identifier attached to an order.",
            ),
            SemanticFieldDefinition(
                id=LogicalFieldRef("Order.order_id"),
                canonical_type=CanonicalType.STRING,
                role=LogicalFieldRole.IDENTIFIER,
                definition="Stable order identifier.",
            ),
        ),
    )
    mappings = tuple(
        SemanticMappingProposal(
            id=selection.id,
            logical_field=selection.logical_field,
            observation=observation,
            confidence=selection.confidence,
            evidence=selection.evidence,
            risks=selection.risks,
            transformation_plan=selection.transformation_plan,
        )
        for selection, observation in zip(
            _mapping_inputs(),
            resolved.observations,
            strict=True,
        )
    )
    return SemanticOnboardingDraft(
        id=draft_id,
        workspace_id=fixture.workspace_id,
        owner_actor_id="analyst-atomic-m33",
        scope=scope,
        connection_id=fixture.connection_id,
        catalog_generation=fixture.generation,
        catalog_generation_fingerprint=fixture.generation_fingerprint,
        base_registry=base_registry or OnboardingRegistryBase(),
        model=SemanticModelProposal(definition=model),
        mappings=mappings,
        created_at=fixture.now,
        updated_at=fixture.now,
    )


def _creation_audit(draft: SemanticOnboardingDraft) -> SemanticOnboardingAuditRecord:
    return SemanticOnboardingAuditRecord(
        id=f"audit-{draft.id}-created",
        workspace_id=draft.workspace_id,
        draft_id=draft.id,
        event=SemanticOnboardingAuditEvent.DRAFT_CREATED,
        actor_id=draft.owner_actor_id,
        occurred_at=draft.created_at,
        source_revision=0,
        resulting_revision=1,
        previous_fingerprint=None,
        resulting_fingerprint=draft.fingerprint,
    )


def _persist_root(
    fixture: _CatalogFixture,
    draft: SemanticOnboardingDraft,
) -> None:
    PostgresSemanticOnboardingStore(
        fixture.urls.api,
        stale_after_seconds=3_600,
    ).create(
        draft,
        _creation_audit(draft),
        operation="create_draft",
        actor_id=draft.owner_actor_id,
        idempotency_digest=_digest(f"create:{draft.id}"),
        request_fingerprint=_digest(f"request:create:{draft.id}"),
    )


def _insert_active_pointer(
    fixture: _CatalogFixture,
    scope: SemanticRegistryScope,
) -> None:
    transition_id = f"transition-m33-{uuid4().hex}"
    decision_ids = [f"decision-m33-{uuid4().hex}"]
    target = datahub_registry_document_urn(scope, 1)
    now = datetime.now(UTC)
    with psycopg.connect(fixture.urls.migrator) as connection:
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
            ) VALUES (
                %s, %s, %s, %s, 1, 'activate', 0, NULL, NULL, NULL,
                1, %s, %s, %s, NULL, %s, %s, %s, %s, %s::jsonb, %s,
                '{}'::jsonb
            )
            """,
            (
                transition_id,
                scope.workspace_id,
                scope.catalog_scope,
                scope.registry_id,
                "e" * 64,
                target,
                f"publication-{uuid4().hex}",
                _digest(f"proposal:{transition_id}"),
                f"approval-{uuid4().hex}",
                "publisher-atomic-m33",
                now,
                json.dumps(decision_ids),
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.registry_active_pointers (
                workspace_id, catalog_scope, registry_id, generation,
                registry_version, registry_fingerprint, registry_target,
                transition_id, activated_by, activated_at, decision_ids_json
            ) VALUES (%s, %s, %s, 1, 1, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                scope.workspace_id,
                scope.catalog_scope,
                scope.registry_id,
                "e" * 64,
                target,
                transition_id,
                "publisher-atomic-m33",
                now,
                json.dumps(decision_ids),
            ),
        )


def _principal(
    fixture: _CatalogFixture,
    role: IdentityRole,
    actor_id: str,
    *,
    workspace_id: str | None = None,
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        actor_id=actor_id,
        workspace_id=workspace_id or fixture.workspace_id,
        roles=frozenset({role}),
        authentication_method=AuthenticationMethod.OIDC,
        authenticated_at=fixture.now - timedelta(minutes=1),
        expires_at=fixture.now + timedelta(hours=1),
    )


def _human_evidence() -> OnboardingEvidence:
    return OnboardingEvidence(
        kind=OnboardingEvidenceKind.HUMAN_ATTESTATION,
        detail="The steward reviewed the retained metadata and business-owner evidence.",
        reference="ticket:M33-POSTGRES",
    )


def _model_decision_mutation(
    draft: SemanticOnboardingDraft,
    *,
    actor_id: str,
    label: str,
    at: datetime,
) -> tuple[SemanticOnboardingDraft, SemanticOnboardingDecision, SemanticOnboardingAuditRecord]:
    decision_id = f"decision-model-{label}"
    idempotency_digest = _digest(f"idempotency:{label}")
    request_fingerprint = _digest(f"request:{label}")
    decision = SemanticOnboardingDecision(
        id=decision_id,
        workspace_id=draft.workspace_id,
        draft_id=draft.id,
        target_kind=SemanticOnboardingTargetKind.MODEL,
        target_id=draft.model.definition.id.root,
        action=DecisionAction.APPROVE,
        status=ApprovalStatus.APPROVED,
        actor_id=actor_id,
        decided_at=at,
        source_revision=draft.revision,
        resulting_revision=draft.revision + 1,
        rationale="The governed model definition has explicit steward approval.",
        evidence=(_human_evidence(),),
        idempotency_digest=idempotency_digest,
        request_fingerprint=request_fingerprint,
    )
    revised = SemanticOnboardingDraft.model_validate(
        {
            **draft.model_dump(mode="python"),
            "revision": draft.revision + 1,
            "model": SemanticModelProposal(
                definition=draft.model.definition,
                status=ApprovalStatus.APPROVED,
                decision_id=decision.id,
                decided_by=actor_id,
            ),
            "updated_at": at,
        }
    )
    audit = SemanticOnboardingAuditRecord(
        id=f"audit-model-{label}",
        workspace_id=draft.workspace_id,
        draft_id=draft.id,
        event=SemanticOnboardingAuditEvent.DECISION_RECORDED,
        actor_id=actor_id,
        occurred_at=at,
        source_revision=draft.revision,
        resulting_revision=revised.revision,
        previous_fingerprint=draft.fingerprint,
        resulting_fingerprint=revised.fingerprint,
        decision_id=decision.id,
    )
    return revised, decision, audit


def test_preflight_resolves_active_catalog_and_registry_base_in_one_snapshot(
    onboarding_catalog: _CatalogFixture,
) -> None:
    fixture = onboarding_catalog

    first = _preflight(fixture)
    replay = _preflight(fixture)

    assert first == replay
    assert first.connection_id == fixture.connection_id
    assert first.catalog_generation == fixture.generation
    assert first.catalog_generation_fingerprint == fixture.generation_fingerprint
    assert first.base_registry == OnboardingRegistryBase()
    assert tuple(item.physical_field.root for item in first.observations) == (
        "sales.orders.order_id",
        "sales.customers.customer_id",
    )
    assert first.observations[0].observed_datahub_asset_urn == ASSET_ORDER_ID.root
    assert first.external_writes_performed is False

    active_scope = fixture.scope.model_copy(
        update={"registry_id": f"preflight_registry_{uuid4().hex[:8]}"}
    )
    _insert_active_pointer(fixture, active_scope)
    active = PostgresSemanticOnboardingCatalogEvidence(
        fixture.urls.api,
        stale_after_seconds=3_600,
    ).resolve_active(
        active_scope,
        PreflightSemanticOnboardingRequest(
            connection_id=fixture.connection_id,
            selections=tuple(
                SemanticOnboardingPreflightSelection(
                    asset_id=item.asset_id,
                    field_path=item.field_path,
                )
                for item in _mapping_inputs()
            ),
        ),
    )
    assert active.base_registry.registry_version == 1
    assert active.base_registry.registry_fingerprint == "e" * 64
    assert active.base_registry.activation_generation == 1
    assert active.base_registry.active_pointer_fingerprint is not None

    other_scope = fixture.scope.model_copy(
        update={"workspace_id": f"workspace-other-{uuid4().hex[:8]}"}
    )
    with pytest.raises(SemanticOnboardingPortError) as raised:
        PostgresSemanticOnboardingCatalogEvidence(
            fixture.urls.api,
            stale_after_seconds=3_600,
        ).resolve_active(
            other_scope,
            PreflightSemanticOnboardingRequest(
                connection_id=fixture.connection_id,
                selections=(
                    SemanticOnboardingPreflightSelection(
                        asset_id=ASSET_ORDER_ID,
                        field_path=("order_id",),
                    ),
                ),
            ),
        )
    assert raised.value.code is SemanticOnboardingPortErrorCode.RESOURCE_UNAVAILABLE


def test_catalog_resolver_executes_exact_ordered_recordset_and_fails_closed(
    onboarding_catalog: _CatalogFixture,
) -> None:
    fixture = onboarding_catalog
    resolver = PostgresSemanticOnboardingCatalogEvidence(
        fixture.urls.api,
        stale_after_seconds=3_600,
    )
    selections = _mapping_inputs()

    resolved = resolver.resolve_exact(
        fixture.scope,
        fixture.connection_id,
        fixture.generation,
        fixture.generation_fingerprint,
        selections,
    )

    assert tuple(item.locator.asset.asset_id for item in resolved.observations) == (
        ASSET_ORDER_ID,
        ASSET_CUSTOMER_ID,
    )
    assert tuple(item.physical_field.root for item in resolved.observations) == (
        "sales.orders.order_id",
        "sales.customers.customer_id",
    )
    assert resolved.observations[0].observed_datahub_asset_urn == ASSET_ORDER_ID.root
    assert resolved.observations[1].observed_datahub_asset_urn is None

    other_scope = fixture.scope.model_copy(
        update={"workspace_id": f"workspace-other-{uuid4().hex[:8]}"}
    )
    for scope, fingerprint in (
        (other_scope, fixture.generation_fingerprint),
        (fixture.scope, "f" * 64),
    ):
        with pytest.raises(SemanticOnboardingPortError) as raised:
            resolver.resolve_exact(
                scope,
                fixture.connection_id,
                fixture.generation,
                fingerprint,
                selections,
            )
        assert raised.value.code is SemanticOnboardingPortErrorCode.RESOURCE_UNAVAILABLE


def test_store_create_rejects_catalog_that_became_stale_after_resolution(
    onboarding_catalog: _CatalogFixture,
) -> None:
    fixture = onboarding_catalog
    scope = fixture.scope.model_copy(update={"registry_id": "orders_registry_stale_create"})
    draft = _store_draft(
        fixture,
        scope=scope,
        draft_id="orders-stale-create",
    )
    with psycopg.connect(fixture.urls.migrator) as connection:
        original = connection.execute(
            """
            SELECT active_generation_completed_at
            FROM schemabridge_control.catalog_connections
            WHERE workspace_id = %s AND connection_id = %s
            """,
            (fixture.workspace_id, fixture.connection_id.root),
        ).fetchone()
        assert original is not None and original[0] is not None
        connection.execute(
            """
            UPDATE schemabridge_control.catalog_connections
            SET active_generation_completed_at = %s,
                updated_at = greatest(clock_timestamp(), updated_at + interval '1 microsecond')
            WHERE workspace_id = %s AND connection_id = %s
            """,
            (
                datetime.now(UTC) - timedelta(hours=2),
                fixture.workspace_id,
                fixture.connection_id.root,
            ),
        )
    try:
        store = PostgresSemanticOnboardingStore(
            fixture.urls.api,
            stale_after_seconds=60,
        )
        with pytest.raises(SemanticOnboardingPortError) as conflict:
            store.create(
                draft,
                _creation_audit(draft),
                operation="create_draft",
                actor_id=draft.owner_actor_id,
                idempotency_digest=_digest("create:stale-catalog-drift"),
                request_fingerprint=_digest("request:create:stale-catalog-drift"),
            )
        assert conflict.value.code is SemanticOnboardingPortErrorCode.CONFLICT
        assert store.load(fixture.workspace_id, draft.id) is None
    finally:
        with psycopg.connect(fixture.urls.migrator) as connection:
            connection.execute(
                """
                UPDATE schemabridge_control.catalog_connections
                SET active_generation_completed_at = %s,
                    updated_at = greatest(
                        clock_timestamp(),
                        updated_at + interval '1 microsecond'
                    )
                WHERE workspace_id = %s AND connection_id = %s
                """,
                (original[0], fixture.workspace_id, fixture.connection_id.root),
            )


def test_store_create_rejects_registry_pointer_drift_after_resolution(
    onboarding_catalog: _CatalogFixture,
) -> None:
    fixture = onboarding_catalog
    scope = fixture.scope.model_copy(update={"registry_id": "orders_registry_create_drift"})
    draft = _store_draft(
        fixture,
        scope=scope,
        draft_id="orders-create-drift",
    )

    _insert_active_pointer(fixture, scope)

    store = PostgresSemanticOnboardingStore(
        fixture.urls.api,
        stale_after_seconds=3_600,
    )
    with pytest.raises(SemanticOnboardingPortError) as conflict:
        store.create(
            draft,
            _creation_audit(draft),
            operation="create_draft",
            actor_id=draft.owner_actor_id,
            idempotency_digest=_digest("create:registry-pointer-drift"),
            request_fingerprint=_digest("request:create:registry-pointer-drift"),
        )
    assert conflict.value.code is SemanticOnboardingPortErrorCode.CONFLICT
    assert store.load(fixture.workspace_id, draft.id) is None


def test_store_decision_rejects_registry_pointer_drift_after_revalidation(
    onboarding_catalog: _CatalogFixture,
) -> None:
    fixture = onboarding_catalog
    scope = fixture.scope.model_copy(update={"registry_id": "orders_registry_decision_drift"})
    draft = _store_draft(
        fixture,
        scope=scope,
        draft_id="orders-decision-drift",
    )
    _persist_root(fixture, draft)
    mutation = _model_decision_mutation(
        draft,
        actor_id="steward-atomic-m33",
        label="authority-drift",
        at=fixture.now,
    )

    _insert_active_pointer(fixture, scope)

    store = PostgresSemanticOnboardingStore(
        fixture.urls.api,
        stale_after_seconds=3_600,
    )
    with pytest.raises(SemanticOnboardingPortError) as conflict:
        store.commit_decision(
            *mutation,
            expected_revision=draft.revision,
            operation="record_decision",
            actor_id=mutation[1].actor_id,
            idempotency_digest=mutation[1].idempotency_digest,
            request_fingerprint=mutation[1].request_fingerprint,
        )
    assert conflict.value.code is SemanticOnboardingPortErrorCode.CONFLICT
    assert store.load(fixture.workspace_id, draft.id) == draft
    assert store.list_decisions(fixture.workspace_id, draft.id) == ()


def test_store_preparation_rejects_registry_pointer_drift_after_revalidation(
    onboarding_catalog: _CatalogFixture,
) -> None:
    fixture = onboarding_catalog
    scope = fixture.scope.model_copy(update={"registry_id": "orders_registry_preparation_drift"})
    draft = _store_draft(
        fixture,
        scope=scope,
        draft_id="orders-preparation-drift",
    )
    _persist_root(fixture, draft)
    store = PostgresSemanticOnboardingStore(
        fixture.urls.api,
        stale_after_seconds=3_600,
    )
    catalog = PostgresSemanticOnboardingCatalogEvidence(
        fixture.urls.api,
        stale_after_seconds=3_600,
    )
    decide = DecideSemanticOnboarding(
        store,
        catalog,
        _EmptyRegistryBase(),
        SemanticOnboardingAuthorizationPolicy(),
        _Clock(fixture.now),
    )
    steward = _principal(fixture, IdentityRole.STEWARD, "steward-preparation-m33")
    for target_kind, target_id, key in (
        (SemanticOnboardingTargetKind.MODEL, "Order", "approve-atomic-model-0001"),
        (
            SemanticOnboardingTargetKind.MAPPING,
            "mapping-order-id",
            "approve-atomic-order-0001",
        ),
        (
            SemanticOnboardingTargetKind.MAPPING,
            "mapping-customer-id",
            "approve-atomic-customer-01",
        ),
    ):
        mutation = decide.execute(
            steward,
            draft.id,
            target_kind=target_kind,
            target_id=target_id,
            action=DecisionAction.APPROVE,
            expected_revision=draft.revision,
            confirmed_draft_fingerprint=draft.fingerprint,
            rationale="The steward confirmed retained authority evidence for this exact target.",
            evidence=(_human_evidence(),),
            idempotency_key=key,
        )
        draft = mutation.draft
    assert draft.ready_for_preparation()

    _insert_active_pointer(fixture, scope)

    publisher = _principal(fixture, IdentityRole.PUBLISHER, "publisher-preparation-m33")
    prepare = PrepareSemanticOnboardingPublication(
        store,
        catalog,
        _EmptyRegistryBase(),
        SemanticOnboardingAuthorizationPolicy(),
        _Clock(fixture.now),
    )
    with pytest.raises(SemanticOnboardingError) as conflict:
        prepare.execute(
            publisher,
            draft.id,
            expected_revision=draft.revision,
            confirmed_draft_fingerprint=draft.fingerprint,
            idempotency_key="prepare-atomic-authority-0001",
        )
    assert conflict.value.code is SemanticOnboardingErrorCode.CONFLICT
    assert store.load(fixture.workspace_id, draft.id) == draft
    assert store.list_proposals(fixture.workspace_id, draft.id) == ()


def test_store_is_tenant_isolated_cas_append_only_idempotent_and_restart_safe(
    onboarding_catalog: _CatalogFixture,
) -> None:
    fixture = onboarding_catalog
    store = PostgresSemanticOnboardingStore(fixture.urls.api)
    catalog = PostgresSemanticOnboardingCatalogEvidence(
        fixture.urls.api,
        stale_after_seconds=3_600,
    )
    authorization = SemanticOnboardingAuthorizationPolicy()
    clock = _Clock(fixture.now)
    base = _EmptyRegistryBase()
    request = _request(fixture)
    analyst = _principal(fixture, IdentityRole.ANALYST, "analyst-m33")
    steward = _principal(fixture, IdentityRole.STEWARD, "steward-m33")
    publisher = _principal(fixture, IdentityRole.PUBLISHER, "publisher-m33")
    created = CreateSemanticOnboardingDraft(
        store,
        catalog,
        base,
        authorization,
        clock,
        fixture.scope,
    ).execute(
        analyst,
        request,
        idempotency_key="create-orders-m33-0001",
    )
    replayed_create = CreateSemanticOnboardingDraft(
        PostgresSemanticOnboardingStore(fixture.urls.api),
        catalog,
        base,
        authorization,
        clock,
        fixture.scope,
    ).execute(
        analyst,
        request,
        idempotency_key="create-orders-m33-0001",
    )
    assert replayed_create.replayed
    assert replayed_create.draft == created.draft

    winning = _model_decision_mutation(
        created.draft,
        actor_id=steward.actor_id,
        label="winner",
        at=fixture.now,
    )
    stale = _model_decision_mutation(
        created.draft,
        actor_id=steward.actor_id,
        label="stale",
        at=fixture.now,
    )
    committed = store.commit_decision(
        *winning,
        expected_revision=created.draft.revision,
        operation="record_decision",
        actor_id=steward.actor_id,
        idempotency_digest=winning[1].idempotency_digest,
        request_fingerprint=winning[1].request_fingerprint,
    )
    replayed_decision = PostgresSemanticOnboardingStore(fixture.urls.api).commit_decision(
        *winning,
        expected_revision=created.draft.revision,
        operation="record_decision",
        actor_id=steward.actor_id,
        idempotency_digest=winning[1].idempotency_digest,
        request_fingerprint=winning[1].request_fingerprint,
    )
    assert replayed_decision.replayed
    assert replayed_decision.draft == committed.draft
    with pytest.raises(SemanticOnboardingPortError) as conflict:
        store.commit_decision(
            *stale,
            expected_revision=created.draft.revision,
            operation="record_decision",
            actor_id=steward.actor_id,
            idempotency_digest=stale[1].idempotency_digest,
            request_fingerprint=stale[1].request_fingerprint,
        )
    assert conflict.value.code is SemanticOnboardingPortErrorCode.CONFLICT

    decide = DecideSemanticOnboarding(
        PostgresSemanticOnboardingStore(fixture.urls.api),
        catalog,
        base,
        authorization,
        clock,
    )
    draft = committed.draft
    for mapping_id, key in (
        ("mapping-order-id", "approve-order-m33-0001"),
        ("mapping-customer-id", "approve-customer-m33-01"),
    ):
        result = decide.execute(
            steward,
            draft.id,
            target_kind=SemanticOnboardingTargetKind.MAPPING,
            target_id=mapping_id,
            action=DecisionAction.APPROVE,
            expected_revision=draft.revision,
            confirmed_draft_fingerprint=draft.fingerprint,
            rationale="This exact physical key has retained catalog and steward evidence.",
            evidence=(_human_evidence(),),
            idempotency_key=key,
        )
        draft = result.draft
    assert draft.ready_for_preparation()

    prepare = PrepareSemanticOnboardingPublication(
        PostgresSemanticOnboardingStore(fixture.urls.api),
        catalog,
        base,
        authorization,
        clock,
    )
    prepared = prepare.execute(
        publisher,
        draft.id,
        expected_revision=draft.revision,
        confirmed_draft_fingerprint=draft.fingerprint,
        idempotency_key="prepare-orders-m33-v0001",
    )
    replayed_preparation = PrepareSemanticOnboardingPublication(
        PostgresSemanticOnboardingStore(fixture.urls.api),
        catalog,
        base,
        authorization,
        clock,
    ).execute(
        publisher,
        draft.id,
        expected_revision=draft.revision,
        confirmed_draft_fingerprint=draft.fingerprint,
        idempotency_key="prepare-orders-m33-v0001",
    )
    assert replayed_preparation.replayed
    assert replayed_preparation == prepared.model_copy(update={"replayed": True})
    assert prepared.draft.status is SemanticOnboardingStatus.READY_FOR_PUBLICATION
    assert prepared.proposal.external_writes_performed is False

    restarted = PostgresSemanticOnboardingStore(fixture.urls.api)
    assert restarted.load(fixture.workspace_id, draft.id) == prepared.draft
    assert restarted.load(f"workspace-other-{uuid4().hex[:8]}", draft.id) is None
    assert (
        restarted.list_for_workspace(
            f"workspace-other-{uuid4().hex[:8]}",
            owner_actor_id=None,
            limit=50,
        )
        == ()
    )
    assert len(restarted.list_decisions(fixture.workspace_id, draft.id)) == 3
    recent_decisions = restarted.list_decisions(
        fixture.workspace_id,
        draft.id,
        limit=2,
    )
    assert [item.target_id for item in recent_decisions] == [
        "mapping-order-id",
        "mapping-customer-id",
    ]
    assert restarted.list_proposals(fixture.workspace_id, draft.id) == (prepared.proposal,)
    assert [item.event.value for item in restarted.list_audit(fixture.workspace_id, draft.id)] == [
        "draft_created",
        "decision_recorded",
        "decision_recorded",
        "decision_recorded",
        "publication_prepared",
    ]
    assert [
        item.event.value for item in restarted.list_audit(fixture.workspace_id, draft.id, limit=2)
    ] == ["decision_recorded", "publication_prepared"]

    late_replay = restarted.commit_decision(
        *winning,
        expected_revision=created.draft.revision,
        operation="record_decision",
        actor_id=steward.actor_id,
        idempotency_digest=winning[1].idempotency_digest,
        request_fingerprint=winning[1].request_fingerprint,
    )
    assert late_replay.replayed
    assert late_replay.draft == committed.draft

    with psycopg.connect(fixture.urls.api) as connection:
        compact_operations = connection.execute(
            """
            SELECT operation, count(*), bool_and(response_draft IS NULL)
            FROM schemabridge_control.semantic_onboarding_operations
            WHERE workspace_id = %s AND draft_id = %s
            GROUP BY operation
            ORDER BY operation
            """,
            (fixture.workspace_id, draft.id),
        ).fetchall()
    assert compact_operations == [
        ("create_draft", 1, False),
        ("prepare_publication", 1, False),
        ("record_decision", 3, True),
    ]

    with (
        pytest.raises(psycopg.Error) as immutable,
        psycopg.connect(fixture.urls.migrator) as connection,
    ):
        connection.execute(
            """
            UPDATE schemabridge_control.semantic_onboarding_decisions
            SET actor_id = 'tampered-actor'
            WHERE workspace_id = %s AND draft_id = %s
            """,
            (fixture.workspace_id, draft.id),
        )
    assert immutable.value.sqlstate == "55000"

    with psycopg.connect(fixture.urls.api) as connection:
        privileges = connection.execute(
            """
            SELECT
                has_table_privilege(
                    current_user,
                    'schemabridge_control.registry_active_pointers',
                    'SELECT'
                ),
                has_table_privilege(
                    current_user,
                    'schemabridge_control.registry_active_pointers',
                    'UPDATE'
                ),
                has_table_privilege(
                    current_user,
                    'schemabridge_control.registry_activation_transitions',
                    'SELECT'
                )
            """
        ).fetchone()
    assert privileges == (True, False, False)

    with psycopg.connect(fixture.urls.backup) as connection:
        backup_privileges = connection.execute(
            """
            SELECT
                bool_and(has_table_privilege(current_user, table_name, 'SELECT')),
                bool_or(has_table_privilege(current_user, table_name, 'INSERT'))
            FROM unnest(ARRAY[
                'schemabridge_control.semantic_onboarding_drafts',
                'schemabridge_control.semantic_onboarding_decisions',
                'schemabridge_control.semantic_onboarding_proposals',
                'schemabridge_control.semantic_onboarding_audit',
                'schemabridge_control.semantic_onboarding_operations'
            ]) AS table_name
            """
        ).fetchone()
    assert backup_privileges == (True, False)
