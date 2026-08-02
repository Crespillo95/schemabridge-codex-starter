"""Real PostgreSQL proof for exact semantic evidence, decisions, gating, and reads."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.types.json import Jsonb
from tests.integration.connector_target_support import (
    acquire_catalog_refresh_route,
    ensure_catalog_connector_target,
)

from schemabridge.adapters.catalog.postgres_inventory import (
    PostgresCatalogConnectionStore,
)
from schemabridge.adapters.catalog.postgres_refresh import (
    PostgresCatalogRefreshStore,
)
from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.adapters.control_plane.postgres_registry_control import (
    PostgresRegistryControlStore,
)
from schemabridge.adapters.semantic_change.cursor import (
    SignedSemanticChangeCursorCodec,
)
from schemabridge.adapters.semantic_change.postgres_dependencies import (
    IndexedArtifactKind,
    IndexedSemanticArtifact,
    PostgresSemanticChangeDependencyIndex,
)
from schemabridge.adapters.semantic_change.postgres_evidence import (
    PostgresSemanticChangeEvidenceReader,
)
from schemabridge.adapters.semantic_change.postgres_read import (
    PostgresSemanticChangeGateReader,
    PostgresSemanticChangeReadStore,
)
from schemabridge.adapters.semantic_change.postgres_store import (
    PostgresSemanticChangeStore,
)
from schemabridge.adapters.semantic_registry.recorded import (
    RecordedGovernedSemanticRegistry,
)
from schemabridge.application.ports.semantic_change import (
    SemanticChangePortError,
    SemanticChangePortErrorCode,
)
from schemabridge.application.ports.semantic_change_read import (
    MAX_SEMANTIC_CHANGE_PAGE_SIZE,
    SemanticChangeFindingFilter,
    SemanticChangeImpactFilter,
    SemanticChangeReportFilter,
)
from schemabridge.application.semantic_change_read import (
    InspectSemanticChangeReport,
    ListSemanticChangeFindings,
    ListSemanticChangeImpacts,
    ListSemanticChangeReports,
)
from schemabridge.domain.catalog_inventory import (
    CatalogAssetId,
    CatalogAssetLocator,
    CatalogConnectionDisable,
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogConnectionRegistration,
    CatalogFieldLocator,
    CatalogRefreshCommand,
    CatalogRefreshMode,
    CatalogSourceAsset,
    CatalogSourceChange,
    CatalogSourceChangeKind,
    CatalogSourceField,
    CatalogSourcePage,
)
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)
from schemabridge.domain.joins import (
    Cardinality,
    DeclaredRelationship,
    RelationshipProfile,
)
from schemabridge.domain.registry_control import (
    ActiveRegistryPointer,
    registry_projection_fingerprint,
)
from schemabridge.domain.semantic_change import (
    GovernedJoinRef,
    GovernedMappingRef,
    SemanticBindingSelection,
    SemanticBindingSelectionSet,
    SemanticChangeConfirmation,
    SemanticChangeDecisionAction,
    SemanticChangeInspectionContext,
    SemanticChangeKind,
    SemanticChangeReport,
    SemanticChangeStatus,
    SemanticDependencyIndexState,
    SemanticEvidenceBaseline,
    SemanticEvidenceObservation,
    SemanticPlanDependencies,
    build_semantic_change_approval,
    build_semantic_change_decision,
    build_semantic_change_report,
    classify_semantic_change_findings,
    prepare_semantic_change_decision,
)
from schemabridge.domain.semantic_profile_jobs import SemanticJoinProfileProposal
from schemabridge.domain.semantic_registry import (
    GovernedSemanticRegistrySnapshot,
    PhysicalValueType,
    SemanticRegistryScope,
    datahub_registry_document_urn,
)
from schemabridge.entrypoints.http.app import (
    ApiHttpServices,
    SemanticChangeHttpServices,
    create_http_app,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations/control_plane"
MANIFEST = ROOT / "demo/ground_truth/registries/manifest.yml"
DEFAULT_ADMIN_DSN = "postgresql://postgres:local-only-not-a-secret@127.0.0.1:55434/postgres"
NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
AUDIT_KEY = b"semantic-change-audit-key-0123456789abcdef"
LEASE_CAPABILITY = "semantic-catalog-lease-capability-0123456789abcdef"
HTTP_BEARER = "m26-postgres-api-integration-bearer"
CURSOR_KEY = b"m26-postgres-api-integration-cursor-key"


@dataclass(frozen=True)
class _DatabaseUrls:
    database: str
    migrator: str
    reconciler: str
    runtime: str
    api: str
    catalog: str


def _role_dsn(role: str, database: str) -> str:
    return f"postgresql://{role}:{role}@127.0.0.1:55434/{database}"


def _semantic_api_client(
    store: PostgresSemanticChangeReadStore,
    workspace_id: str,
) -> TestClient:
    clock = _ApiClock()
    cursors = SignedSemanticChangeCursorCodec(
        signing_key=CURSOR_KEY,
    )
    semantic_changes = SemanticChangeHttpServices(
        list_reports=ListSemanticChangeReports(
            store=store,
            cursors=cursors,
            clock=clock,
        ),
        inspect_report=InspectSemanticChangeReport(
            store=store,
            clock=clock,
        ),
        list_findings=ListSemanticChangeFindings(
            store=store,
            cursors=cursors,
            clock=clock,
        ),
        list_impacts=ListSemanticChangeImpacts(
            store=store,
            cursors=cursors,
            clock=clock,
        ),
    )
    unused = _UnusedApiOperation()
    return TestClient(
        create_http_app(
            ApiHttpServices(
                authenticator=_ApiAuthenticator(workspace_id),
                clock=clock,
                submit=unused,  # type: ignore[arg-type]
                inspect=unused,  # type: ignore[arg-type]
                cancel=unused,  # type: ignore[arg-type]
                readiness=_ReadyApi(),
                semantic_changes=semantic_changes,
            ),
        ),
        raise_server_exceptions=False,
    )


@pytest.fixture(scope="module")
def semantic_database() -> Iterator[_DatabaseUrls]:
    database = f"schemabridge_semantic_{uuid4().hex[:12]}"
    admin_dsn = os.environ.get(
        "SCHEMABRIDGE_TEST_CONTROL_ADMIN_DATABASE_URL",
        DEFAULT_ADMIN_DSN,
    )
    urls = _DatabaseUrls(
        database=database,
        migrator=_role_dsn("schemabridge_migrator", database),
        reconciler=_role_dsn("schemabridge_reconciler", database),
        runtime=_role_dsn("schemabridge_runtime", database),
        api=_role_dsn("schemabridge_api", database),
        catalog=_role_dsn("schemabridge_catalog", database),
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
                    schemabridge_runtime,
                    schemabridge_api,
                    schemabridge_catalog
                """
            ).format(sql.Identifier(database))
        )
    try:
        migrated = PostgresControlPlaneMigrator(urls.migrator, MIGRATIONS).migrate()
        assert migrated.inspection.current_version == 13
        yield urls
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )


@dataclass(frozen=True)
class _AggregateProfiles:
    cardinalities: dict[str, Cardinality]
    drifted_contract_ids: frozenset[str] = frozenset()

    def profile_bound(self, proposal: SemanticJoinProfileProposal) -> RelationshipProfile:
        raw_proposal = proposal.proposal
        if raw_proposal.id in self.drifted_contract_ids:
            return RelationshipProfile(
                left_row_count=10,
                right_row_count=10,
                left_null_count=0,
                right_null_count=0,
                left_invalid_count=0,
                right_invalid_count=0,
                left_distinct_valid=5,
                right_distinct_valid=5,
                matching_distinct_keys=5,
                left_max_multiplicity=2,
                right_max_multiplicity=2,
                declared_relationship=DeclaredRelationship.NONE,
            )
        cardinality = self.cardinalities[raw_proposal.id]
        left_many = cardinality is Cardinality.MANY_TO_ONE
        right_many = cardinality is Cardinality.ONE_TO_MANY
        declared = (
            DeclaredRelationship.LEFT_FOREIGN_KEY_TO_RIGHT
            if left_many
            else DeclaredRelationship.RIGHT_FOREIGN_KEY_TO_LEFT
        )
        return RelationshipProfile(
            left_row_count=10,
            right_row_count=10,
            left_null_count=0,
            right_null_count=0,
            left_invalid_count=0,
            right_invalid_count=0,
            left_distinct_valid=5 if left_many else 10,
            right_distinct_valid=5 if right_many else 10,
            matching_distinct_keys=5,
            left_max_multiplicity=2 if left_many else 1,
            right_max_multiplicity=2 if right_many else 1,
            declared_relationship=declared,
        )


@dataclass(frozen=True)
class _ApiAuthenticator:
    workspace_id: str

    def authenticate(
        self,
        bearer_token: str,
        now: datetime,
    ) -> AuthenticatedPrincipal:
        assert bearer_token == HTTP_BEARER
        return AuthenticatedPrincipal(
            actor_id="sb_actor_v1_" + ("a" * 64),
            workspace_id=self.workspace_id,
            roles=frozenset({IdentityRole.ANALYST}),
            authentication_method=AuthenticationMethod.LOCAL_DEMO,
            authenticated_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(minutes=30),
        )


@dataclass(frozen=True)
class _ApiClock:
    def now(self) -> datetime:
        return NOW + timedelta(minutes=5)


@dataclass(frozen=True)
class _UnusedApiOperation:
    def execute(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("semantic-change reads must not invoke a mutation")


@dataclass(frozen=True)
class _ReadyApi:
    def require_ready(self) -> None:
        return None


def test_postgres_semantic_change_end_to_end_is_exact_and_fail_closed(
    semantic_database: _DatabaseUrls,
) -> None:
    workspace_id = f"workspace-semantic-{uuid4().hex[:10]}"
    scope = SemanticRegistryScope(
        workspace_id=workspace_id,
        catalog_scope="synthetic-demo",
        registry_id="synthetic_enterprise",
    )
    registry = RecordedGovernedSemanticRegistry(MANIFEST, scope).load().registry
    pointer = _seed_pointer(semantic_database.migrator, scope, registry)
    _seed_catalog_generation(
        semantic_database,
        scope,
        registry,
        connection_id="warehouse-primary",
        generation=1,
        create_connection=True,
    )
    first_physical = registry.mapping_set.mappings[0].mapping.physical_field.root
    _seed_catalog_generation(
        semantic_database,
        scope,
        registry,
        connection_id="warehouse-homonym",
        generation=1,
        create_connection=True,
        only_fields={first_physical},
    )
    selected_dataset, selected_field = first_physical.rsplit(".", 1)
    candidate_request = {
        "ordinal": 0,
        "dataset_ref": selected_dataset,
        "field_path": [selected_field],
        "selected_connection_id": None,
        "selected_asset_id": None,
        "selected_field_path": None,
    }
    with psycopg.connect(semantic_database.reconciler) as connection:
        candidates = connection.execute(
            """
            SELECT
                ordinal, connection_id, asset_id, asset_key,
                field_key, field_path, candidate_count
            FROM schemabridge_control.load_semantic_initial_catalog_candidates(
                %s, %s, %s::jsonb
            )
            """,
            (
                workspace_id,
                scope.catalog_scope,
                Jsonb([candidate_request]),
            ),
        ).fetchall()
        wrong_scope = connection.execute(
            """
            SELECT ordinal, connection_id, candidate_count
            FROM schemabridge_control.load_semantic_initial_catalog_candidates(
                %s, %s, %s::jsonb
            )
            """,
            (
                f"{workspace_id}-other",
                scope.catalog_scope,
                Jsonb([candidate_request]),
            ),
        ).fetchall()
        malformed = connection.execute(
            """
            SELECT *
            FROM schemabridge_control.load_semantic_initial_catalog_candidates(
                %s, %s, %s::jsonb
            )
            """,
            (
                workspace_id,
                scope.catalog_scope,
                Jsonb([{**candidate_request, "unexpected": "rejected"}]),
            ),
        ).fetchall()
        over_capacity = connection.execute(
            """
            SELECT *
            FROM schemabridge_control.load_semantic_initial_catalog_candidates(
                %s, %s, %s::jsonb
            )
            """,
            (
                workspace_id,
                scope.catalog_scope,
                Jsonb([{**candidate_request, "ordinal": ordinal} for ordinal in range(2_001)]),
            ),
        ).fetchall()
        non_contiguous_ordinals = connection.execute(
            """
            SELECT *
            FROM schemabridge_control.load_semantic_initial_catalog_candidates(
                %s, %s, %s::jsonb
            )
            """,
            (
                workspace_id,
                scope.catalog_scope,
                Jsonb([candidate_request, {**candidate_request, "ordinal": 2}]),
            ),
        ).fetchall()
        mismatched_selected_field = connection.execute(
            """
            SELECT *
            FROM schemabridge_control.load_semantic_initial_catalog_candidates(
                %s, %s, %s::jsonb
            )
            """,
            (
                workspace_id,
                scope.catalog_scope,
                Jsonb(
                    [
                        {
                            **candidate_request,
                            "selected_connection_id": "warehouse-primary",
                            "selected_asset_id": selected_dataset,
                            "selected_field_path": ["other_field"],
                        }
                    ]
                ),
            ),
        ).fetchall()
    assert len(candidates) == 2
    assert {str(row[1]) for row in candidates} == {
        "warehouse-homonym",
        "warehouse-primary",
    }
    expected_field_key = hashlib.sha256(
        json.dumps(
            [selected_field],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert all(
        str(row[2]) == selected_dataset
        and str(row[4]) == expected_field_key
        and tuple(row[5]) == (selected_field,)
        and int(row[6]) == 2
        for row in candidates
    )
    assert wrong_scope == [(0, None, 0)]
    assert malformed == []
    assert over_capacity == []
    assert non_contiguous_ordinals == []
    assert mismatched_selected_field == []

    dependency_index = PostgresSemanticChangeDependencyIndex(semantic_database.reconciler)
    mapping_decision = _decision_id(registry.mapping_set.mappings[0].approval_decision_id)
    first_join = registry.join_contracts.contracts[0]
    artifacts = tuple(
        sorted(
            (
                IndexedSemanticArtifact(
                    kind=IndexedArtifactKind.QUERY_RECIPE,
                    artifact_id="recipe-customer-rollup",
                    version=1,
                    fingerprint=_digest("recipe-customer-rollup-v1"),
                    mapping_decision_ids=(mapping_decision,),
                ),
                IndexedSemanticArtifact(
                    kind=IndexedArtifactKind.WORKFLOW,
                    artifact_id="workflow-customer-rollup",
                    version=1,
                    fingerprint=_digest("workflow-customer-rollup-v1"),
                    mapping_decision_ids=(mapping_decision,),
                    join_contracts=((first_join.id, first_join.version),),
                ),
            ),
            key=lambda item: (item.kind.value, item.artifact_id, item.version),
        )
    )
    dependency_state = dependency_index.reconcile(
        scope,
        registry_generation=pointer.generation,
        registry_version=registry.version,
        registry_fingerprint=registry.fingerprint,
        pointer_transition_id=pointer.transition_id,
        watermark=1,
        complete=True,
        artifacts=artifacts,
        indexed_at=NOW,
    )
    assert (
        dependency_index.reconcile(
            scope,
            registry_generation=pointer.generation,
            registry_version=registry.version,
            registry_fingerprint=registry.fingerprint,
            pointer_transition_id=pointer.transition_id,
            watermark=1,
            complete=True,
            artifacts=artifacts,
            indexed_at=NOW,
        )
        == dependency_state
    )
    context = _inspection_context(pointer, registry, dependency_state)
    profiles = _AggregateProfiles(
        {item.id: item.cardinality for item in registry.join_contracts.contracts}
    )
    evidence = PostgresSemanticChangeEvidenceReader(
        semantic_database.reconciler,
        profiles,
        clock=lambda: NOW,
    )

    ambiguous = evidence.observe(context, registry, None)
    ambiguous_field = next(
        item for item in ambiguous.fields if item.mapping.physical_field.root == first_physical
    )
    assert not ambiguous.complete
    assert not ambiguous_field.present
    assert ambiguous_field.candidate_count == 2
    assert ambiguous_field.reason_code == "binding_ambiguous"

    selected_mapping = next(
        item for item in context.mappings if item.physical_field.root == first_physical
    )
    selection = SemanticBindingSelection(
        mapping_approval_decision_id=selected_mapping.approval_decision_id,
        mapping_version=selected_mapping.version,
        physical_field=selected_mapping.physical_field,
        locator=CatalogFieldLocator(
            asset=CatalogAssetLocator(
                workspace_id=workspace_id,
                connection_id=CatalogConnectionId("warehouse-primary"),
                asset_id=CatalogAssetId(selected_dataset),
            ),
            field_path=(selected_field,),
        ),
    )
    assert any(
        first_physical in {item.left_field.root, item.right_field.root} for item in context.joins
    )
    cross_connection_selection = selection.model_copy(
        update={
            "locator": CatalogFieldLocator(
                asset=CatalogAssetLocator(
                    workspace_id=workspace_id,
                    connection_id=CatalogConnectionId("warehouse-homonym"),
                    asset_id=CatalogAssetId(selected_dataset),
                ),
                field_path=(selected_field,),
            )
        }
    )
    with pytest.raises(SemanticChangePortError) as cross_connection:
        evidence.observe(
            context,
            registry,
            None,
            binding_selections=SemanticBindingSelectionSet.create(
                scope=scope,
                selections=(cross_connection_selection,),
            ),
        )
    assert cross_connection.value.code is SemanticChangePortErrorCode.EVIDENCE_UNAVAILABLE

    selected_observation = evidence.observe(
        context,
        registry,
        None,
        binding_selections=SemanticBindingSelectionSet.create(
            scope=scope,
            selections=(selection,),
        ),
    )
    selected_field_evidence = next(
        item
        for item in selected_observation.fields
        if item.mapping.physical_field.root == first_physical
    )
    assert selected_observation.complete
    assert selected_field_evidence.candidate_count == 2
    assert selected_field_evidence.explicit_selection == selection

    _disable_connection(
        semantic_database.api,
        workspace_id,
        "warehouse-homonym",
    )
    observation = evidence.observe(context, registry, None)
    assert observation.complete
    assert len(observation.fields) == len(registry.mapping_set.mappings) == 31
    assert len(observation.joins) == len(registry.join_contracts.contracts) == 5
    assert all(
        item.binding is not None
        and item.binding.locator.asset.workspace_id == workspace_id
        and item.binding.locator.asset.connection_id.root == "warehouse-primary"
        and item.binding.catalog_generation == 1
        for item in observation.fields
    )

    report = _report(observation, None, dependency_index)
    assert report.status is SemanticChangeStatus.REVIEW_REQUIRED
    assert {item.kind for item in report.findings} == {SemanticChangeKind.BASELINE_REQUIRED}
    assert report.impacts.complete
    assert report.impacts.workflow_count == 1
    assert report.impacts.recipe_count == 1

    store = PostgresSemanticChangeStore(
        semantic_database.reconciler,
        dependency_index,
        {"v1": AUDIT_KEY},
        "v1",
    )
    assert store.record_report(report, observation) == report
    assert store.record_report(report, observation) == report
    assert store.load_report(scope, report.id) == report
    assert store.load_observation(scope, report.id) == observation

    proposal = prepare_semantic_change_decision(
        report,
        observation,
        action=SemanticChangeDecisionAction.ESTABLISH_BASELINE,
        expected_head_revision=0,
    )
    approval = build_semantic_change_approval(
        proposal,
        actor="sb_semantic_steward_v1",
        approved_at=NOW + timedelta(minutes=1),
        confirmation=SemanticChangeConfirmation.ESTABLISH,
    )
    decision = build_semantic_change_decision(proposal, approval)
    baseline_commit = store.commit_decision(
        proposal,
        approval,
        decision,
        decision.baseline,
    )
    assert baseline_commit.head_revision == 1
    assert baseline_commit.baseline == decision.baseline
    assert store.load_head(scope) == baseline_commit
    assert store.load_baseline(scope) == decision.baseline
    replay = store.commit_decision(proposal, approval, decision, decision.baseline)
    assert replay.replayed

    audit_before_stale = _audit_count(semantic_database.migrator, workspace_id)
    stale_approval = build_semantic_change_approval(
        proposal,
        actor="sb_other_steward_v1",
        approved_at=NOW + timedelta(minutes=2),
        confirmation=SemanticChangeConfirmation.ESTABLISH,
    )
    stale_decision = build_semantic_change_decision(proposal, stale_approval)
    with pytest.raises(SemanticChangePortError) as stale:
        store.commit_decision(
            proposal,
            stale_approval,
            stale_decision,
            stale_decision.baseline,
        )
    assert stale.value.code is SemanticChangePortErrorCode.CAS_CONFLICT
    assert _audit_count(semantic_database.migrator, workspace_id) == audit_before_stale
    verification = PostgresRegistryControlStore(
        semantic_database.reconciler,
        {"v1": AUDIT_KEY},
        "v1",
    ).verify_audit_chain(workspace_id)
    assert verification.valid
    assert verification.event_count == 1
    assert verification.head_hash == baseline_commit.audit_event_hash

    dependencies = SemanticPlanDependencies.create(
        scope=scope,
        pointer_generation=pointer.generation,
        pointer_fingerprint=registry_projection_fingerprint(pointer),
        registry_version=registry.version,
        registry_fingerprint=registry.fingerprint,
        mappings=(selected_mapping,),
        joins=(),
    )
    gate = PostgresSemanticChangeGateReader(semantic_database.runtime)
    current_assessment = gate.assess(dependencies)
    assert current_assessment.eligible
    assert current_assessment.connection_id == CatalogConnectionId("warehouse-primary")

    _seed_catalog_generation(
        semantic_database,
        scope,
        registry,
        connection_id="warehouse-primary",
        generation=2,
        create_connection=False,
        include_unrelated_asset=True,
    )
    unchanged_evidence_window = gate.assess(dependencies)
    assert unchanged_evidence_window.eligible

    baseline = _required_baseline(store.load_baseline(scope))
    equivalent_observation = evidence.observe(context, registry, baseline)
    equivalent_report = _report(
        equivalent_observation,
        baseline,
        dependency_index,
    )
    assert equivalent_observation.catalog_generations != baseline.catalog_generations
    assert equivalent_report.status is SemanticChangeStatus.CURRENT
    assert equivalent_report.findings == ()
    store.record_report(equivalent_report, equivalent_observation)
    equivalent_proposal = prepare_semantic_change_decision(
        equivalent_report,
        equivalent_observation,
        action=SemanticChangeDecisionAction.REVALIDATE_COMPATIBLE_CHANGE,
        expected_head_revision=1,
    )
    equivalent_approval = build_semantic_change_approval(
        equivalent_proposal,
        actor="sb_semantic_steward_v1",
        approved_at=NOW + timedelta(minutes=3),
        confirmation=SemanticChangeConfirmation.REVALIDATE,
    )
    equivalent_decision = build_semantic_change_decision(
        equivalent_proposal,
        equivalent_approval,
    )
    equivalent_commit = store.commit_decision(
        equivalent_proposal,
        equivalent_approval,
        equivalent_decision,
        equivalent_decision.baseline,
    )
    assert equivalent_commit.head_revision == 2
    assert gate.assess(dependencies).eligible
    assert gate.assess(dependencies).status is SemanticChangeStatus.REVALIDATED

    _seed_catalog_generation(
        semantic_database,
        scope,
        registry,
        connection_id="warehouse-primary",
        generation=3,
        create_connection=False,
        type_overrides={first_physical: PhysicalValueType.INTEGER},
    )
    current_baseline = _required_baseline(store.load_baseline(scope))
    type_observation = evidence.observe(context, registry, current_baseline)
    type_report = _report(type_observation, current_baseline, dependency_index)
    assert type_report.status is SemanticChangeStatus.BLOCKED
    assert SemanticChangeKind.PHYSICAL_TYPE_CHANGED in {item.kind for item in type_report.findings}
    store.record_report(type_report, type_observation)
    blocked_assessment = gate.assess(dependencies)
    assert not blocked_assessment.eligible
    assert blocked_assessment.connection_id is None

    _seed_catalog_generation(
        semantic_database,
        scope,
        registry,
        connection_id="warehouse-primary",
        generation=4,
        create_connection=False,
        omitted_fields={first_physical},
    )
    removal_observation = evidence.observe(context, registry, current_baseline)
    removal_report = _report(
        removal_observation,
        current_baseline,
        dependency_index,
    )
    assert removal_report.status is SemanticChangeStatus.BLOCKED
    assert SemanticChangeKind.FIELD_REMOVED in {item.kind for item in removal_report.findings}
    store.record_report(removal_report, removal_observation)
    rejection_proposal = prepare_semantic_change_decision(
        removal_report,
        removal_observation,
        action=SemanticChangeDecisionAction.REJECT_CHANGE,
        expected_head_revision=2,
    )
    rejection_approval = build_semantic_change_approval(
        rejection_proposal,
        actor="sb_semantic_steward_v1",
        approved_at=NOW + timedelta(minutes=4),
        confirmation=SemanticChangeConfirmation.REJECT,
    )
    rejection_decision = build_semantic_change_decision(
        rejection_proposal,
        rejection_approval,
    )
    rejection_commit = store.commit_decision(
        rejection_proposal,
        rejection_approval,
        rejection_decision,
        None,
    )
    assert rejection_commit.head_revision == 3
    assert rejection_commit.decision.resulting_status is SemanticChangeStatus.REJECTED

    reads = PostgresSemanticChangeReadStore(semantic_database.api)
    first_page = reads.list_reports(
        workspace_id,
        filters=SemanticChangeReportFilter(),
        page_size=1,
        after=None,
    )
    assert first_page.has_more
    assert first_page.last_key is not None
    second_page = reads.list_reports(
        workspace_id,
        filters=SemanticChangeReportFilter(),
        page_size=1,
        after=first_page.last_key,
    )
    assert second_page.items
    assert second_page.items[0].report_id != first_page.items[0].report_id
    assert second_page.items[0].inspected_at == first_page.items[0].inspected_at
    assert first_page.items[0].report_id > second_page.items[0].report_id
    initial_public = reads.load_report(workspace_id, report.id)
    assert initial_public is not None
    assert initial_public.status is SemanticChangeStatus.REVALIDATED
    equivalent_public = reads.load_report(workspace_id, equivalent_report.id)
    assert equivalent_public is not None
    assert equivalent_public.status is SemanticChangeStatus.REVALIDATED
    rejected_public = reads.load_report(workspace_id, removal_report.id)
    assert rejected_public is not None
    assert rejected_public.status is SemanticChangeStatus.REJECTED
    unresolved_public = reads.load_report(workspace_id, type_report.id)
    assert unresolved_public is not None
    assert unresolved_public.status is SemanticChangeStatus.BLOCKED
    revalidated_page = reads.list_reports(
        workspace_id,
        filters=SemanticChangeReportFilter(
            status=SemanticChangeStatus.REVALIDATED,
        ),
        page_size=MAX_SEMANTIC_CHANGE_PAGE_SIZE,
        after=None,
    )
    assert {item.report_id for item in revalidated_page.items} >= {
        report.id,
        equivalent_report.id,
    }
    rejected_page = reads.list_reports(
        workspace_id,
        filters=SemanticChangeReportFilter(
            status=SemanticChangeStatus.REJECTED,
        ),
        page_size=MAX_SEMANTIC_CHANGE_PAGE_SIZE,
        after=None,
    )
    assert {item.report_id for item in rejected_page.items} == {
        removal_report.id,
    }
    with _semantic_api_client(reads, workspace_id) as api:
        revalidated_http = api.get(
            "/v1/semantic-changes/reports",
            headers={"Authorization": f"Bearer {HTTP_BEARER}"},
            params={"status": "revalidated", "page_size": 50},
        )
        rejected_http = api.get(
            "/v1/semantic-changes/reports",
            headers={"Authorization": f"Bearer {HTTP_BEARER}"},
            params={"status": "rejected", "page_size": 50},
        )
    assert revalidated_http.status_code == 200
    assert {item["report_id"] for item in revalidated_http.json()["items"]} >= {
        report.id,
        equivalent_report.id,
    }
    assert all(
        item["status"] == SemanticChangeStatus.REVALIDATED.value
        for item in revalidated_http.json()["items"]
    )
    assert rejected_http.status_code == 200
    assert {item["report_id"] for item in rejected_http.json()["items"]} == {removal_report.id}
    assert rejected_http.json()["items"][0]["status"] == SemanticChangeStatus.REJECTED.value
    assert reads.load_report("workspace-foreign", report.id) is None
    with pytest.raises(ValueError):
        reads.list_reports(
            workspace_id,
            filters=SemanticChangeReportFilter(),
            page_size=MAX_SEMANTIC_CHANGE_PAGE_SIZE + 1,
            after=None,
        )
    findings_page = reads.list_findings(
        workspace_id,
        report.id,
        filters=SemanticChangeFindingFilter(),
        page_size=1,
        after=None,
    )
    assert findings_page.items
    assert findings_page.items[0].workspace_id == workspace_id
    impacts_page = reads.list_impacts(
        workspace_id,
        report.id,
        filters=SemanticChangeImpactFilter(),
        page_size=1,
        after=None,
    )
    assert impacts_page.items
    assert impacts_page.items[0].workspace_id == workspace_id

    drifted_join = next(
        item
        for item in context.joins
        if first_physical not in {item.left_field.root, item.right_field.root}
    )
    drift_profiles = _AggregateProfiles(
        {item.id: item.cardinality for item in registry.join_contracts.contracts},
        drifted_contract_ids=frozenset({drifted_join.contract_id}),
    )
    drift_observation = PostgresSemanticChangeEvidenceReader(
        semantic_database.reconciler,
        drift_profiles,
        clock=lambda: NOW + timedelta(hours=1),
    ).observe(context, registry, current_baseline)
    drift_report = _report(drift_observation, current_baseline, dependency_index)
    assert SemanticChangeKind.JOIN_CARDINALITY_CHANGED in {
        item.kind for item in drift_report.findings
    }

    join_fields = {drifted_join.left_field, drifted_join.right_field}
    join_dependencies = SemanticPlanDependencies.create(
        scope=scope,
        pointer_generation=pointer.generation,
        pointer_fingerprint=registry_projection_fingerprint(pointer),
        registry_version=registry.version,
        registry_fingerprint=registry.fingerprint,
        mappings=tuple(item for item in context.mappings if item.physical_field in join_fields),
        joins=(drifted_join,),
    )
    assert gate.assess(join_dependencies).eligible

    store.record_report(drift_report, drift_observation)
    assert not gate.assess(join_dependencies).eligible

    unaffected_mapping = next(
        item
        for item in context.mappings
        if item.physical_field.root != first_physical and item.physical_field not in join_fields
    )
    unaffected_dependencies = SemanticPlanDependencies.create(
        scope=scope,
        pointer_generation=pointer.generation,
        pointer_fingerprint=registry_projection_fingerprint(pointer),
        registry_version=registry.version,
        registry_fingerprint=registry.fingerprint,
        mappings=(unaffected_mapping,),
        joins=(),
    )
    assert gate.assess(unaffected_dependencies).eligible

    expired_evidence = PostgresSemanticChangeEvidenceReader(
        semantic_database.reconciler,
        profiles,
        clock=lambda: NOW - timedelta(days=2),
    ).observe(context, registry, current_baseline)
    expired_report = _report(expired_evidence, current_baseline, dependency_index)
    expiring_store = PostgresSemanticChangeStore(
        semantic_database.reconciler,
        dependency_index,
        {"v1": AUDIT_KEY},
        "v1",
        retention=timedelta(days=1),
    )
    expiring_store.record_report(expired_report, expired_evidence)
    assert expiring_store.load_report(scope, expired_report.id) == expired_report
    assert reads.load_report(workspace_id, expired_report.id) is None
    assert not reads.list_findings(
        workspace_id,
        expired_report.id,
        filters=SemanticChangeFindingFilter(),
        page_size=1,
        after=None,
    ).items
    assert not reads.list_impacts(
        workspace_id,
        expired_report.id,
        filters=SemanticChangeImpactFilter(),
        page_size=1,
        after=None,
    ).items

    incomplete_live_state = dependency_index.reconcile(
        scope,
        registry_generation=pointer.generation,
        registry_version=registry.version,
        registry_fingerprint=registry.fingerprint,
        pointer_transition_id=pointer.transition_id,
        watermark=dependency_state.watermark + 1,
        complete=False,
        artifacts=(),
        indexed_at=NOW + timedelta(hours=3),
    )
    assert not incomplete_live_state.complete
    assert not gate.assess(unaffected_dependencies).eligible
    assert not gate.assess(join_dependencies).eligible

    _advance_pointer(
        semantic_database.migrator,
        pointer,
        registry,
    )
    superseded_public = reads.load_report(workspace_id, type_report.id)
    assert superseded_public is not None
    assert superseded_public.status is SemanticChangeStatus.SUPERSEDED
    still_revalidated = reads.load_report(workspace_id, report.id)
    assert still_revalidated is not None
    assert still_revalidated.status is SemanticChangeStatus.REVALIDATED
    still_rejected = reads.load_report(workspace_id, removal_report.id)
    assert still_rejected is not None
    assert still_rejected.status is SemanticChangeStatus.REJECTED
    with _semantic_api_client(reads, workspace_id) as api:
        superseded_http = api.get(
            f"/v1/semantic-changes/reports/{type_report.id}",
            headers={"Authorization": f"Bearer {HTTP_BEARER}"},
        )
    assert superseded_http.status_code == 200
    assert superseded_http.json()["status"] == SemanticChangeStatus.SUPERSEDED.value


@pytest.mark.scale
def test_postgres_dependency_index_batches_5434_exact_artifact_edges(
    semantic_database: _DatabaseUrls,
) -> None:
    workspace_id = f"workspace-dependency-scale-{uuid4().hex[:10]}"
    scope = SemanticRegistryScope(
        workspace_id=workspace_id,
        catalog_scope="synthetic-demo",
        registry_id="synthetic_enterprise",
    )
    registry = RecordedGovernedSemanticRegistry(MANIFEST, scope).load().registry
    pointer = _seed_pointer(semantic_database.migrator, scope, registry)
    mapping_decision_id = _decision_id(registry.mapping_set.mappings[0].approval_decision_id)
    artifacts = tuple(
        IndexedSemanticArtifact(
            kind=IndexedArtifactKind.WORKFLOW,
            artifact_id=f"workflow-scale-{index:05d}",
            version=1,
            fingerprint=_digest(f"workflow-scale-{index:05d}"),
            mapping_decision_ids=(mapping_decision_id,),
        )
        for index in range(5_434)
    )
    index = PostgresSemanticChangeDependencyIndex(semantic_database.reconciler)

    started = time.monotonic()
    state = index.reconcile(
        scope,
        registry_generation=pointer.generation,
        registry_version=registry.version,
        registry_fingerprint=registry.fingerprint,
        pointer_transition_id=pointer.transition_id,
        watermark=1,
        complete=True,
        artifacts=artifacts,
        indexed_at=NOW,
    )
    elapsed = time.monotonic() - started

    assert state.complete
    assert elapsed < 30
    with psycopg.connect(semantic_database.migrator) as connection:
        row = connection.execute(
            """
            SELECT count(*)
            FROM schemabridge_control.semantic_artifact_dependencies
            WHERE workspace_id = %s
              AND catalog_scope = %s
              AND registry_id = %s
              AND dependency_index_watermark = 1
            """,
            (scope.workspace_id, scope.catalog_scope, scope.registry_id),
        ).fetchone()
    assert row == (5_434,)


def _seed_pointer(
    dsn: str,
    scope: SemanticRegistryScope,
    registry: GovernedSemanticRegistrySnapshot,
) -> ActiveRegistryPointer:
    transition_id = f"transition-semantic-{uuid4().hex}"
    decision_ids = sorted(
        {
            *(_decision_id(item.approval_decision_id) for item in registry.mapping_set.mappings),
            *(
                _decision_id(item.approval_decision_id)
                for item in registry.join_contracts.contracts
            ),
        }
    )
    target = datahub_registry_document_urn(scope, registry.version)
    with psycopg.connect(dsn) as connection:
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
                %s, %s, %s, %s, NULL, %s, %s, %s, %s, %s::jsonb, %s,
                '{}'::jsonb
            )
            """,
            (
                transition_id,
                scope.workspace_id,
                scope.catalog_scope,
                scope.registry_id,
                registry.version,
                registry.fingerprint,
                target,
                f"publication-{uuid4().hex}",
                _digest("semantic-pointer-proposal"),
                f"approval-semantic-{uuid4().hex}",
                "sb_registry_publisher_v1",
                NOW,
                json.dumps(decision_ids),
                NOW,
            ),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.registry_active_pointers (
                workspace_id, catalog_scope, registry_id, generation,
                registry_version, registry_fingerprint, registry_target,
                transition_id, activated_by, activated_at, decision_ids_json
            ) VALUES (%s, %s, %s, 1, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                scope.workspace_id,
                scope.catalog_scope,
                scope.registry_id,
                registry.version,
                registry.fingerprint,
                target,
                transition_id,
                "sb_registry_publisher_v1",
                NOW,
                json.dumps(decision_ids),
            ),
        )
    return ActiveRegistryPointer(
        scope=scope,
        generation=1,
        registry_version=registry.version,
        registry_fingerprint=registry.fingerprint,
        registry_target=target,
        transition_id=transition_id,
        activated_by="sb_registry_publisher_v1",
        activated_at=NOW,
        decision_ids=tuple(decision_ids),
    )


def _advance_pointer(
    dsn: str,
    current: ActiveRegistryPointer,
    registry: GovernedSemanticRegistrySnapshot,
) -> ActiveRegistryPointer:
    transition_id = f"transition-semantic-{uuid4().hex}"
    approved_at = NOW + timedelta(hours=4)
    with psycopg.connect(dsn) as connection:
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
                %s, %s, %s, %s, %s, 'activate', %s, %s, %s, %s,
                %s, %s, %s, %s, NULL, %s, %s, %s, %s, %s::jsonb, %s,
                '{}'::jsonb
            )
            """,
            (
                transition_id,
                current.scope.workspace_id,
                current.scope.catalog_scope,
                current.scope.registry_id,
                current.generation + 1,
                current.generation,
                current.registry_version,
                current.registry_fingerprint,
                current.transition_id,
                registry.version,
                registry.fingerprint,
                current.registry_target,
                f"publication-{uuid4().hex}",
                _digest("semantic-pointer-supersession-proposal"),
                f"approval-semantic-{uuid4().hex}",
                "sb_registry_publisher_v1",
                approved_at,
                json.dumps(list(current.decision_ids)),
                approved_at,
            ),
        )
        connection.execute(
            """
            UPDATE schemabridge_control.registry_active_pointers
            SET generation = %s,
                registry_version = %s,
                registry_fingerprint = %s,
                transition_id = %s,
                activated_by = %s,
                activated_at = %s
            WHERE workspace_id = %s
              AND catalog_scope = %s
              AND registry_id = %s
              AND generation = %s
            """,
            (
                current.generation + 1,
                registry.version,
                registry.fingerprint,
                transition_id,
                "sb_registry_publisher_v1",
                approved_at,
                current.scope.workspace_id,
                current.scope.catalog_scope,
                current.scope.registry_id,
                current.generation,
            ),
        )
    return ActiveRegistryPointer(
        scope=current.scope,
        generation=current.generation + 1,
        registry_version=registry.version,
        registry_fingerprint=registry.fingerprint,
        registry_target=current.registry_target,
        transition_id=transition_id,
        activated_by="sb_registry_publisher_v1",
        activated_at=approved_at,
        decision_ids=current.decision_ids,
    )


def _seed_catalog_generation(
    urls: _DatabaseUrls,
    scope: SemanticRegistryScope,
    registry: GovernedSemanticRegistrySnapshot,
    *,
    connection_id: str,
    generation: int,
    create_connection: bool,
    only_fields: set[str] | None = None,
    omitted_fields: set[str] | None = None,
    type_overrides: dict[str, PhysicalValueType] | None = None,
    include_unrelated_asset: bool = False,
) -> None:
    omitted = omitted_fields or set()
    overrides = type_overrides or {}
    selected = [
        item
        for item in registry.mapping_set.mappings
        if (only_fields is None or item.mapping.physical_field.root in only_fields)
        and item.mapping.physical_field.root not in omitted
    ]
    grouped: dict[str, list[tuple[str, PhysicalValueType]]] = {}
    for item in selected:
        physical = item.mapping.physical_field.root
        dataset, field_name = physical.rsplit(".", 1)
        grouped.setdefault(dataset, []).append(
            (field_name, overrides.get(physical, item.physical_type))
        )
    requested_at = datetime.now(UTC)
    with psycopg.connect(urls.migrator) as connection:
        if create_connection:
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
                (scope.workspace_id, requested_at, requested_at),
            )
    catalog_connection_id = CatalogConnectionId(connection_id)
    if create_connection:
        PostgresCatalogConnectionStore(urls.api).register(
            CatalogConnectionRegistration(
                workspace_id=scope.workspace_id,
                connection_id=catalog_connection_id,
                display_name=connection_id,
                kind=CatalogConnectionKind.SYNTHETIC,
                environment="PROD",
                catalog_scope=scope.catalog_scope,
                requested_by="sb_catalog_admin_v1",
                requested_at=requested_at,
                idempotency_digest=_digest(f"{connection_id}-registration"),
            )
        )
    target = ensure_catalog_connector_target(
        urls.migrator,
        workspace_id=scope.workspace_id,
        connection_id=catalog_connection_id,
    )
    assets = tuple(
        CatalogSourceAsset(
            asset_id=CatalogAssetId(dataset),
            qualified_name=dataset,
            display_name=dataset.rsplit(".", 1)[-1],
            platform="postgres",
            environment="PROD",
            schema_name=dataset.split(".", 1)[0],
            description=f"Synthetic governed asset {dataset}",
            fields=tuple(
                CatalogSourceField(
                    field_path=(field_name,),
                    native_type=normalized.value,
                    description=f"Stable definition for {dataset}.{field_name}",
                    nullable=False,
                    is_part_of_key=True,
                    tags=("governed",),
                    glossary_terms=("semantic",),
                    metadata_fingerprint=_digest(
                        f"field:{connection_id}:{dataset}.{field_name}:{normalized.value}"
                    ),
                )
                for field_name, normalized in sorted(fields)
            ),
            metadata_fingerprint=_digest(f"asset:{connection_id}:{dataset}:stable"),
        )
        for dataset, fields in sorted(grouped.items())
    )
    if include_unrelated_asset:
        assets += (
            CatalogSourceAsset(
                asset_id=CatalogAssetId("sandbox.unrelated_events"),
                qualified_name="sandbox.unrelated_events",
                display_name="unrelated_events",
                platform="postgres",
                environment="PROD",
                schema_name="sandbox",
                fields=(
                    CatalogSourceField(
                        field_path=("event_id",),
                        native_type="string",
                        nullable=False,
                        is_part_of_key=True,
                        metadata_fingerprint=_digest("unrelated-event-id"),
                    ),
                ),
                metadata_fingerprint=_digest("unrelated-events"),
            ),
        )
    requested = PostgresCatalogRefreshStore(
        urls.api,
        application_name="schemabridge-control-api",
    ).request(
        CatalogRefreshCommand(
            workspace_id=scope.workspace_id,
            connection_id=catalog_connection_id,
            mode=CatalogRefreshMode.FULL,
            requested_by="sb_catalog_admin_v1",
            requested_at=requested_at,
            idempotency_digest=_digest(f"{connection_id}-generation-{generation}"),
        )
    )
    catalog_refreshes = PostgresCatalogRefreshStore(urls.catalog)
    leased = catalog_refreshes.claim_next(
        indexer_id="semantic-catalog-indexer",
        lease_capability=LEASE_CAPABILITY,
        lease_duration=timedelta(minutes=5),
    )
    assert leased is not None
    assert leased.refresh_id == requested.refresh.refresh_id
    assert leased.lease is not None
    acquire_catalog_refresh_route(
        urls.catalog,
        workspace_id=scope.workspace_id,
        connection_id=catalog_connection_id,
        refresh_id=leased.refresh_id,
        indexer_id=leased.lease.indexer_id,
        lease_capability=LEASE_CAPABILITY,
        fencing_token=leased.lease.fencing_token,
    )
    staging = catalog_refreshes.begin_staging(
        scope.workspace_id,
        leased.refresh_id,
        indexer_id=leased.lease.indexer_id,
        lease_capability=LEASE_CAPABILITY,
        fencing_token=leased.lease.fencing_token,
    )
    catalog_refreshes.persist_page(
        scope.workspace_id,
        leased.refresh_id,
        indexer_id=leased.lease.indexer_id,
        lease_capability=LEASE_CAPABILITY,
        fencing_token=leased.lease.fencing_token,
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
    with psycopg.connect(urls.migrator) as connection:
        for dataset, fields in sorted(grouped.items()):
            for field_name, normalized in sorted(fields):
                connection.execute(
                    """
                    UPDATE schemabridge_control.catalog_fields AS field
                    SET normalized_type = %s
                    FROM schemabridge_control.catalog_assets AS asset
                    WHERE field.workspace_id = %s
                      AND field.connection_id = %s
                      AND field.generation = %s
                      AND field.asset_key = asset.asset_key
                      AND asset.workspace_id = field.workspace_id
                      AND asset.connection_id = field.connection_id
                      AND asset.generation = field.generation
                      AND asset.asset_id = %s
                      AND field.field_path = %s
                    """,
                    (
                        normalized.value,
                        scope.workspace_id,
                        connection_id,
                        generation,
                        dataset,
                        [field_name],
                    ),
                )
    completed = catalog_refreshes.complete(
        scope.workspace_id,
        leased.refresh_id,
        indexer_id=leased.lease.indexer_id,
        lease_capability=LEASE_CAPABILITY,
        fencing_token=leased.lease.fencing_token,
        expected_base_generation=staging.base_generation,
        expected_contract_version=target.contract_version,
        expected_route_revision=target.route_revision,
        expected_target_fingerprint=target.target_fingerprint,
    )
    assert completed.target_generation == generation


def _disable_connection(dsn: str, workspace_id: str, connection_id: str) -> None:
    PostgresCatalogConnectionStore(dsn).disable(
        CatalogConnectionDisable(
            workspace_id=workspace_id,
            connection_id=CatalogConnectionId(connection_id),
            requested_by="sb_catalog_admin_v1",
            requested_at=datetime.now(UTC),
            idempotency_digest=_digest(f"{connection_id}-disabled"),
        )
    )


def _inspection_context(
    pointer: ActiveRegistryPointer,
    registry: GovernedSemanticRegistrySnapshot,
    dependency_state: SemanticDependencyIndexState,
) -> SemanticChangeInspectionContext:
    mappings = tuple(
        GovernedMappingRef(
            logical_field=item.mapping.logical_field,
            physical_field=item.mapping.physical_field,
            version=item.mapping.version,
            approval_decision_id=_decision_id(item.approval_decision_id),
            physical_type=item.physical_type,
        )
        for item in registry.mapping_set.mappings
    )
    joins = tuple(
        GovernedJoinRef(
            contract_id=item.id,
            version=item.version,
            approval_decision_id=_decision_id(item.approval_decision_id),
            left_field=item.left_key.physical_field,
            right_field=item.right_key.physical_field,
            cardinality=item.cardinality,
            fanout_policy=item.fanout_policy,
        )
        for item in registry.join_contracts.contracts
    )
    return SemanticChangeInspectionContext.create(
        scope=pointer.scope,
        pointer_generation=pointer.generation,
        pointer_fingerprint=registry_projection_fingerprint(pointer),
        pointer_transition_id=pointer.transition_id,
        registry_version=registry.version,
        registry_fingerprint=registry.fingerprint,
        mappings=mappings,
        joins=joins,
        dependency_index=dependency_state,
    )


def _report(
    observation: SemanticEvidenceObservation,
    baseline: SemanticEvidenceBaseline | None,
    dependency_index: PostgresSemanticChangeDependencyIndex,
) -> SemanticChangeReport:
    findings = classify_semantic_change_findings(observation, baseline)
    impacts = dependency_index.resolve_impacts(observation.context, findings)
    return build_semantic_change_report(observation, baseline, impacts)


def _audit_count(dsn: str, workspace_id: str) -> int:
    with psycopg.connect(dsn) as connection:
        row = connection.execute(
            """
            SELECT count(*)
            FROM schemabridge_control.control_audit_events
            WHERE workspace_id = %s
            """,
            (workspace_id,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _decision_id(value: str | None) -> str:
    assert value is not None
    return value


def _required_baseline(
    value: SemanticEvidenceBaseline | None,
) -> SemanticEvidenceBaseline:
    assert value is not None
    return value


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
