"""Real PostgreSQL and DataHub acceptance story for M26 semantic change."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb
from tests.acceptance.test_workflow_acceptance import _orchestrator
from tests.integration.connector_target_support import (
    acquire_catalog_refresh_route,
    ensure_catalog_connector_target,
)
from tests.integration.test_semantic_change_postgres import (
    AUDIT_KEY,
    LEASE_CAPABILITY,
    MANIFEST,
    MIGRATIONS,
    _AggregateProfiles,
    _DatabaseUrls,
    _decision_id,
    _digest,
)

from schemabridge.adapters.catalog.postgres_governed_search import (
    PostgresGovernedBindingFactsSearch,
)
from schemabridge.adapters.catalog.postgres_inventory import (
    PostgresCatalogConnectionStore,
)
from schemabridge.adapters.catalog.postgres_refresh import (
    PostgresCatalogRefreshStore,
)
from schemabridge.adapters.control_plane.postgres_active_registry import (
    PostgresActiveRegistryPointerReader,
)
from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.adapters.control_plane.postgres_pool import (
    ControlPoolSettings,
    PostgresControlPool,
)
from schemabridge.adapters.control_plane.postgres_registry_control import (
    PostgresRegistryControlStore,
)
from schemabridge.adapters.datahub.query_recipes import DataHubQueryRecipeAdapter
from schemabridge.adapters.datahub.recipe_inventory import (
    DataHubQueryRecipeInventory,
    DataHubQueryRecipeInventoryConfig,
)
from schemabridge.adapters.identity.local import LocalDemoPrincipalFactory
from schemabridge.adapters.semantic_change.postgres_dependencies import (
    PostgresSemanticChangeDependencyIndex,
)
from schemabridge.adapters.semantic_change.postgres_dependency_sources import (
    PostgresManagedWorkflowDependencySource,
    PostgresSemanticDependencyIndexSink,
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
from schemabridge.adapters.semantic_registry.datahub import (
    DataHubRegistryReadConfig,
    DataHubRegistryWriteConfig,
    DataHubSemanticRegistryPublisher,
)
from schemabridge.adapters.semantic_registry.datahub_control import (
    DataHubRegistryVersionReader,
)
from schemabridge.adapters.semantic_registry.memory import (
    InMemoryGovernedSemanticRegistry,
)
from schemabridge.adapters.semantic_registry.recorded import (
    RecordedGovernedSemanticRegistry,
)
from schemabridge.adapters.sql.compiler import PostgresQueryCompiler
from schemabridge.adapters.sql.guard import SqlGlotPolicyGuard
from schemabridge.adapters.storage.postgres import PostgresWorkflowDraftStore
from schemabridge.adapters.storage.publication_audit import SqlitePublicationAuditStore
from schemabridge.application.governed_execution import (
    ExecuteGovernedRequest,
    PlanSemanticRequest,
    PrepareGovernedRequest,
    semantic_plan_dependencies,
)
from schemabridge.application.guided_requests import (
    BuildGuidedRequest,
    GuidedDimensionInput,
    GuidedMetricInput,
    GuidedRequestCase,
    GuidedRequestInput,
    build_demo_guided_input,
)
from schemabridge.application.ports.semantic_change_read import SemanticChangeImpactFilter
from schemabridge.application.query_execution import (
    CompiledQuery,
    QueryPreviewResult,
    ValidatedQuery,
)
from schemabridge.application.query_recipes import PrepareQueryRecipe, PublishQueryRecipe
from schemabridge.application.registry_control import (
    CommitRegistryActivation,
    PrepareRegistryActivation,
    PrepareRegistryActivationApproval,
)
from schemabridge.application.semantic_change import (
    AssertSemanticContextCurrent,
    CommitSemanticChangeDecision,
    InspectSemanticChange,
    PrepareSemanticChangeDecision,
    PrepareSemanticChangeDecisionApproval,
    SemanticChangeError,
    SemanticChangeErrorCode,
)
from schemabridge.application.semantic_change_operator import (
    LoadSemanticChangeHead,
    VerifySemanticChangeAudit,
)
from schemabridge.application.semantic_dependency_reconciler import (
    ReconcileSemanticDependencies,
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
from schemabridge.domain.connectors import (
    normalize_postgres_native_type,
    postgres_type_contract_fingerprint,
)
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.identity import IdentityRole
from schemabridge.domain.intents import IntentAlternativeId, UserLanguage
from schemabridge.domain.plans import QueryPlan, QueryPolicy
from schemabridge.domain.query_studio import GovernedBindingFactsRequest
from schemabridge.domain.recipes import (
    QueryRecipe,
    RecipePublicationApproval,
    RecipePublicationConfirmation,
    RecipePublicationStatus,
)
from schemabridge.domain.registry_control import (
    ActiveRegistryPointer,
    RegistryActivationConfirmation,
    RegistryVersionTrust,
    registry_projection_fingerprint,
)
from schemabridge.domain.resolution import (
    RejectedSourceReport,
    RejectionCheck,
    ResolutionLimits,
    ResolvedSemanticPlan,
    resolve_semantic_request,
    resolved_semantic_plan_fingerprint,
)
from schemabridge.domain.semantic_change import (
    SemanticChangeCommit,
    SemanticChangeConfirmation,
    SemanticChangeDecisionAction,
    SemanticChangeKind,
    SemanticChangeReport,
    SemanticChangeStatus,
)
from schemabridge.domain.semantic_registry import (
    GovernedFieldMapping,
    GovernedMappingRegistry,
    GovernedSemanticRegistrySnapshot,
    PhysicalValueType,
    RegistryArtifactKind,
    RegistryArtifactProvenance,
    RegistryPublicationApproval,
    RegistryPublicationConfirmation,
    RegistryPublicationStatus,
    SemanticRegistryScope,
    datahub_registry_document_urn,
    prepare_datahub_registry_version,
    registry_publication_approval_id,
    semantic_registry_decision_ids,
    semantic_registry_scope_fingerprint,
)
from schemabridge.domain.workflows import (
    AgentWorkflowDraft,
    ExecutionWorkflowDecision,
    IntentWorkflowDecision,
    StartWorkflowCommand,
    WorkflowDecisionAction,
)

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.integration,
    pytest.mark.scale,
]

ROOT = Path(__file__).resolve().parents[2]
DATAHUB_READER = ROOT / ".local/datahub/mcp.env"
DATAHUB_WRITER = ROOT / ".local/datahub/writer.env"
DEFAULT_ADMIN_DSN = "postgresql://postgres:local-only-not-a-secret@127.0.0.1:55434/postgres"
NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
LARGE_ASSET_COUNT = 5_434
M27_LARGE_FIELD_COUNT = 41_028
M27_SMALL_ASSET_COUNT = 10
M27_SMALL_FIELD_COUNT = 75
_M27_HOSTILE_METADATA_QUERY = "hostile metadata fixture"
_M27_HOSTILE_METADATA_HTML = '<script>window.__schemabridgeHostileMetadata = "executed"</script>'
_M27_HOSTILE_METADATA_BIDI = "\u202e"
_M27_HOSTILE_FIELD_SEGMENT = "hostile_metadata_fixture_" + ("x" * 175)
_M27_SUPPORT_DECOY_ASSET = "support.order_cases"
_M27_SUPPORT_DECOY_FIELD_DESCRIPTIONS = (
    ("order_id", "pedido mencionado libremente en un ticket"),
    ("customer_id", "id del cliente que abrió un ticket de soporte"),
    ("product_id", "producto escrito por el usuario en soporte"),
)
_BROWSER_DATABASE = "SCHEMABRIDGE_TEST_M26_ACCEPTANCE_DATABASE"
_BROWSER_RETAIN = "SCHEMABRIDGE_TEST_M26_RETAIN_DATABASE"
_BROWSER_WORKSPACE = "SCHEMABRIDGE_TEST_M26_LOCAL_WORKSPACE"
_BROWSER_SEED = "SCHEMABRIDGE_TEST_M26_BROWSER_SEED"
_BROWSER_SUBJECT = "SCHEMABRIDGE_TEST_BROWSER_LOCAL_SUBJECT"
_BROWSER_AUDIT_KEY = "SCHEMABRIDGE_TEST_BROWSER_CONTROL_AUDIT_SIGNING_KEY"
_M27_BROWSER_SEED = "SCHEMABRIDGE_TEST_M27_BROWSER_SEED"
_M27_BROWSER_CARDINALITY_PROFILE = "SCHEMABRIDGE_TEST_M27_CARDINALITY_PROFILE"
_M27_BROWSER_CARDINALITIES = {
    "large": (1, LARGE_ASSET_COUNT, M27_LARGE_FIELD_COUNT),
    "small": (1, M27_SMALL_ASSET_COUNT, M27_SMALL_FIELD_COUNT),
    "two_connections": (2, M27_SMALL_ASSET_COUNT + 1, M27_SMALL_FIELD_COUNT + 1),
}
_M27_SECONDARY_CONNECTION_ID = "warehouse-shadow"
_M27_CROSS_CONNECTION_PHYSICAL_FIELD = "sales.orders.order_id"
_BROWSER_DATABASE_NAME = re.compile(r"^schemabridge_m(?:26|27)_browser_[0-9a-f]{12}$")


def _browser_control_audit_key() -> bytes:
    configured = os.environ.get(_BROWSER_AUDIT_KEY)
    if configured is None:
        return AUDIT_KEY
    if os.environ.get(_M27_BROWSER_SEED) != "1":
        pytest.fail("custom browser audit key is restricted to the M27 retained seed")
    encoded = configured.encode("utf-8")
    if len(encoded) < 32 or len(set(encoded)) < 8:
        pytest.fail("M27 browser audit key is not strong enough")
    return encoded


def _m27_browser_cardinality() -> tuple[int, int, int]:
    """Resolve one exact acceptance fixture without changing product limits."""

    profile = os.environ.get(_M27_BROWSER_CARDINALITY_PROFILE, "large")
    if os.environ.get(_M27_BROWSER_SEED) != "1" and profile != "large":
        pytest.fail("an alternate M27 cardinality is restricted to the retained M27 seed")
    try:
        return _M27_BROWSER_CARDINALITIES[profile]
    except KeyError:
        pytest.fail("M27 browser cardinality profile is invalid")


@pytest.fixture
def semantic_acceptance_database() -> Iterator[_DatabaseUrls]:
    """Create an isolated current control database without touching shared state."""

    configured_database = os.environ.get(_BROWSER_DATABASE)
    if configured_database is not None and not _BROWSER_DATABASE_NAME.fullmatch(
        configured_database
    ):
        pytest.fail("M26 browser database name is outside the dedicated safe namespace")
    retain_database = os.environ.get(_BROWSER_RETAIN) == "1"
    if retain_database and configured_database is None:
        pytest.fail("M26 database retention requires an explicit dedicated database name")
    database = (
        configured_database
        if configured_database is not None
        else f"schemabridge_m26_acceptance_{uuid4().hex[:10]}"
    )
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
    roles = (
        "schemabridge_migrator",
        "schemabridge_reconciler",
        "schemabridge_runtime",
        "schemabridge_api",
        "schemabridge_worker",
        "schemabridge_catalog",
    )
    cold_plan_tables = ("catalog_assets", "catalog_fields")
    autovacuum_disabled = False
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        available = {
            str(row[0])
            for row in connection.execute(
                "SELECT rolname FROM pg_catalog.pg_roles WHERE rolname = ANY(%s)",
                (list(roles),),
            )
        }
        assert available == set(roles)
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
                sql.SQL(", ").join(sql.Identifier(role) for role in roles),
            )
        )
    try:
        migrated = PostgresControlPlaneMigrator(urls.migrator, MIGRATIONS).migrate()
        assert migrated.inspection.current_version == 14
        with psycopg.connect(urls.migrator, autocommit=True) as connection:
            for table in cold_plan_tables:
                connection.execute(
                    sql.SQL(
                        "ALTER TABLE schemabridge_control.{} SET (autovacuum_enabled = false)"
                    ).format(sql.Identifier(table))
                )
        autovacuum_disabled = True
        yield urls
    finally:
        if autovacuum_disabled:
            with psycopg.connect(urls.migrator, autocommit=True) as connection:
                for table in cold_plan_tables:
                    connection.execute(
                        sql.SQL(
                            "ALTER TABLE schemabridge_control.{} RESET (autovacuum_enabled)"
                        ).format(sql.Identifier(table))
                    )
        if not retain_database:
            with psycopg.connect(admin_dsn, autocommit=True) as connection:
                connection.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(database)
                    )
                )


@dataclass(slots=True)
class _CountingCompiler:
    calls: int = 0
    delegate: PostgresQueryCompiler = field(default_factory=PostgresQueryCompiler)

    def compile(self, plan: QueryPlan, *, max_preview_rows: int) -> CompiledQuery:
        self.calls += 1
        return self.delegate.compile(plan, max_preview_rows=max_preview_rows)


@dataclass(slots=True)
class _CountingGuard:
    calls: int = 0
    delegate: SqlGlotPolicyGuard = field(default_factory=SqlGlotPolicyGuard)

    def validate(self, query: CompiledQuery, policy: QueryPolicy) -> ValidatedQuery:
        self.calls += 1
        return self.delegate.validate(query, policy)


@dataclass(slots=True)
class _CountingPreview:
    calls: int = 0

    def execute(self, query: ValidatedQuery) -> QueryPreviewResult:
        self.calls += 1
        return QueryPreviewResult(
            columns=("category", "product_count"),
            rows=(("Synthetic category", 1),),
            database_user="schemabridge_reader",
            transaction_read_only=True,
            statement_timeout_ms=query.statement_timeout_ms,
            truncated=False,
        )


@dataclass(slots=True)
class _CountingRejections:
    calls: int = 0

    def inspect(
        self,
        checks: tuple[RejectionCheck, ...],
        *,
        statement_timeout_ms: int,
        should_continue: Callable[[], bool] | None = None,
    ) -> RejectedSourceReport:
        self.calls += 1
        assert should_continue is None or should_continue()
        fields = tuple(check.physical_field for check in checks)
        safety = (
            {
                "database_user": "schemabridge_reader",
                "transaction_read_only": True,
                "statement_timeout_ms": statement_timeout_ms,
            }
            if fields
            else {}
        )
        return RejectedSourceReport(inspected_fields=fields, **safety)


def test_real_datahub_postgres_change_lifecycle_is_exact_complete_and_bounded(
    semantic_acceptance_database: _DatabaseUrls,
    tmp_path: Path,
    record_property: Callable[[str, object], None],
) -> None:
    """Prove one complete baseline-to-remediation lifecycle at dynamic catalog scale."""

    if not DATAHUB_READER.is_file() or not DATAHUB_WRITER.is_file():
        pytest.skip("DataHub reader/writer credentials are required for M26 acceptance")

    suffix = uuid4().hex[:12]
    local_workspace = os.environ.get(_BROWSER_WORKSPACE)
    local_subject = os.environ.get(_BROWSER_SUBJECT, "m26-browser-reader")
    audit_key = _browser_control_audit_key()
    if os.environ.get(_BROWSER_SEED) == "1" and os.environ.get(_M27_BROWSER_SEED) == "1":
        pytest.fail("M26 and M27 retained browser seed modes are mutually exclusive")
    (
        browser_connection_count,
        browser_asset_count,
        browser_field_count,
    ) = _m27_browser_cardinality()
    secondary_asset_count = 1 if browser_connection_count == 2 else 0
    secondary_field_count = 1 if browser_connection_count == 2 else 0
    primary_asset_count = browser_asset_count - secondary_asset_count
    primary_field_count = browser_field_count - secondary_field_count
    workspace_id = (
        LocalDemoPrincipalFactory(
            workspace=local_workspace,
            subject=local_subject,
            roles=frozenset({IdentityRole.ANALYST}),
        )
        .create(now=NOW)
        .workspace_id
        if local_workspace is not None
        else f"workspace-m26-{suffix}"
    )
    scope = SemanticRegistryScope(
        workspace_id=workspace_id,
        catalog_scope="synthetic-demo",
        registry_id="synthetic_enterprise",
    )
    reader_config = DataHubRegistryReadConfig.from_env_file(DATAHUB_READER)
    writer_config = DataHubRegistryWriteConfig.from_env_file(DATAHUB_WRITER)
    recorded = RecordedGovernedSemanticRegistry(MANIFEST, scope).load().registry
    registry_v1 = prepare_datahub_registry_version(recorded, scope)
    _publish_registry(registry_v1, scope, writer_config, minute=0)

    versions = DataHubRegistryVersionReader(reader_config)
    loaded_v1 = versions.load_version(scope, 1)
    assert loaded_v1.trust is RegistryVersionTrust.STRICT
    assert loaded_v1.snapshot.registry == registry_v1

    control = PostgresRegistryControlStore(
        semantic_acceptance_database.runtime,
        {"v1": audit_key},
        "v1",
    )
    pointer_v1 = _activate_registry(control, versions, scope, 1, minute=1)
    assert pointer_v1.generation == 1

    _seed_catalog_generation(
        semantic_acceptance_database,
        scope,
        registry_v1,
        connection_id="warehouse-primary",
        generation=1,
        create_connection=True,
    )
    governed_asset_count = len(
        {
            item.mapping.physical_field.root.rsplit(".", 1)[0]
            for item in registry_v1.mapping_set.mappings
        }
    )
    filler = _unrelated_assets(primary_asset_count - governed_asset_count)
    large_generation = _run_catalog_delta(
        semantic_acceptance_database,
        scope,
        changes=tuple(
            CatalogSourceChange(
                kind=CatalogSourceChangeKind.UPSERT_ASSET,
                asset=asset,
            )
            for asset in filler
        ),
        label=f"large-inventory-{suffix}",
    )
    assert large_generation == 2
    assert _catalog_counts(
        semantic_acceptance_database.migrator,
        scope.workspace_id,
        generation=large_generation,
    ) == (primary_asset_count, primary_asset_count - governed_asset_count + 31)

    workflow = _active_workflow(
        tmp_path,
        registry_v1,
        scope,
        pointer_v1,
        workflow_id=f"workflow-m26-{suffix}",
    )
    PostgresWorkflowDraftStore(
        semantic_acceptance_database.runtime,
        workspace_id=scope.workspace_id,
        owner_actor_id="actor-m26-owner",
    ).save(workflow, expected_revision=None)
    recipe = _publish_workflow_recipe(workflow, tmp_path)

    inventory = DataHubQueryRecipeInventory(
        DataHubQueryRecipeInventoryConfig(
            server=reader_config.server,
            token=reader_config.token,
        )
    )
    _await_recipe_inventory(inventory, scope, recipe.fingerprint)

    index = PostgresSemanticChangeDependencyIndex(semantic_acceptance_database.reconciler)
    workflow_source = PostgresManagedWorkflowDependencySource(
        semantic_acceptance_database.reconciler
    )
    pointers = PostgresActiveRegistryPointerReader(
        semantic_acceptance_database.reconciler,
        application_name="schemabridge-control-reconciler",
    )

    incomplete_reconciler = ReconcileSemanticDependencies(
        pointers=pointers,
        versions=versions,
        workflows=workflow_source,
        recipes=DataHubQueryRecipeInventory(
            DataHubQueryRecipeInventoryConfig(
                server=reader_config.server,
                token="synthetic-invalid-acceptance-token",
                timeout_seconds=3,
            )
        ),
        index=PostgresSemanticDependencyIndexSink(index),
        page_size=7,
    )
    incomplete = incomplete_reconciler.execute(
        scope,
        watermark=1,
        indexed_at=NOW + timedelta(minutes=2),
    )
    assert not incomplete.state.complete
    assert not incomplete.manifest.recipe_source.complete
    assert incomplete.manifest.workflow_source.complete

    profiles = _AggregateProfiles(
        {item.id: item.cardinality for item in registry_v1.join_contracts.contracts}
    )
    evidence = PostgresSemanticChangeEvidenceReader(
        semantic_acceptance_database.reconciler,
        profiles,
        clock=lambda: NOW + timedelta(minutes=3),
    )
    store = PostgresSemanticChangeStore(
        semantic_acceptance_database.reconciler,
        index,
        {"v1": audit_key},
        "v1",
    )
    inspector = InspectSemanticChange(
        pointers=pointers,
        versions=versions,
        evidence=evidence,
        dependency_index=index,
        store=store,
        scope=scope,
    )
    incomplete_report = inspector.execute()
    assert not incomplete_report.impacts.complete
    with pytest.raises(SemanticChangeError) as incomplete_approval:
        PrepareSemanticChangeDecision(store).execute(
            scope,
            incomplete_report.id,
            action=SemanticChangeDecisionAction.ESTABLISH_BASELINE,
        )
    assert incomplete_approval.value.code is SemanticChangeErrorCode.BLOCKING_CHANGE
    assert LoadSemanticChangeHead(store).execute(scope) is None

    dependency_reconciler = ReconcileSemanticDependencies(
        pointers=pointers,
        versions=versions,
        workflows=workflow_source,
        recipes=inventory,
        index=PostgresSemanticDependencyIndexSink(index),
        page_size=7,
    )
    complete = dependency_reconciler.execute(
        scope,
        watermark=2,
        indexed_at=NOW + timedelta(minutes=4),
    )
    assert complete.state.complete
    assert complete.manifest.complete
    assert complete.manifest.artifact_count == 2
    assert complete.manifest.workflow_source.discovered_count == 1
    assert complete.manifest.recipe_source.discovered_count >= 1

    baseline_report = inspector.execute()
    assert baseline_report.status is SemanticChangeStatus.REVIEW_REQUIRED
    assert baseline_report.baseline_revision is None
    assert baseline_report.impacts.complete
    assert baseline_report.impacts.workflow_count == 1
    assert baseline_report.impacts.recipe_count == 1
    baseline_observation = store.load_observation(scope, baseline_report.id)
    assert baseline_observation is not None
    assert len(baseline_observation.fields) == 31
    assert all(item.candidate_count <= 2 for item in baseline_observation.fields)
    assert {
        item.binding.catalog_generation
        for item in baseline_observation.fields
        if item.binding is not None
    } == {large_generation}
    _assert_initial_candidate_scale_plan(
        semantic_acceptance_database,
        scope,
        registry_v1,
        generation=large_generation,
    )
    baseline_commit = _approve_report(
        inspector,
        store,
        baseline_report,
        action=SemanticChangeDecisionAction.ESTABLISH_BASELINE,
        confirmation=SemanticChangeConfirmation.ESTABLISH,
        minute=5,
    )
    assert baseline_commit.head_revision == 1
    assert baseline_commit.baseline is not None

    cold_governed_search = PostgresGovernedBindingFactsSearch(semantic_acceptance_database.runtime)
    cold_started = time.monotonic()
    cold_page = cold_governed_search.search(
        GovernedBindingFactsRequest(
            scope=scope,
            page_size=1,
        )
    )
    cold_elapsed = time.monotonic() - cold_started
    assert cold_elapsed < 5
    assert len(cold_page.items) == 1
    assert cold_page.rows_read <= 2
    assert cold_page.next_key is not None

    # The fixture disables catalog autovacuum so the first request above proves the
    # statistics-independent cold plan. Establish the statistics a managed database
    # would maintain before measuring a complete interactive traversal; this keeps
    # cold correctness and steady-state latency as separate, explicit contracts.
    steady_state_tables = (
        "catalog_assets",
        "catalog_connections",
        "catalog_fields",
        "registry_active_pointers",
        "semantic_change_heads",
        "semantic_change_impacts",
        "semantic_change_reports",
        "semantic_dependency_index_states",
        "semantic_resource_bindings",
    )
    with psycopg.connect(semantic_acceptance_database.migrator, autocommit=True) as connection:
        for table in steady_state_tables:
            connection.execute(
                sql.SQL("ANALYZE schemabridge_control.{}").format(sql.Identifier(table))
            )
        analyzed_statistics = connection.execute(
            """
            SELECT statistics.relname, statistics.last_analyze
            FROM pg_catalog.pg_stat_all_tables AS statistics
            WHERE statistics.schemaname = 'schemabridge_control'
              AND statistics.relname = ANY(%s)
            """,
            (list(steady_state_tables),),
        ).fetchall()
    assert {str(row[0]) for row in analyzed_statistics} == set(steady_state_tables)
    assert all(row[1] is not None for row in analyzed_statistics)

    # Full traversal uses the lifecycle-managed connection provider used by a
    # long-running product process; the unpooled cold first-page bound remains
    # independently proven above.
    query_studio_pool = PostgresControlPool(
        ControlPoolSettings(
            dsn=semantic_acceptance_database.runtime,
            application_name="schemabridge-control-runtime",
            min_size=1,
            max_size=2,
            max_waiting=4,
            acquisition_timeout_seconds=5,
            startup_timeout_seconds=15,
            statement_timeout_ms=5_000,
        )
    )
    governed_orders: dict[int, tuple[str, ...]] = {}
    governed_page_elapsed: list[float] = []
    governed_scope = cold_page.scope
    with query_studio_pool:
        governed_search = PostgresGovernedBindingFactsSearch(
            semantic_acceptance_database.runtime,
            connection_provider=query_studio_pool,
        )
        governed_started = time.monotonic()
        for page_size in (1, 17, 50):
            request = GovernedBindingFactsRequest(
                scope=scope,
                page_size=page_size,
            )
            binding_ids: list[str] = []
            while True:
                page_started = time.monotonic()
                page = governed_search.search(request)
                governed_page_elapsed.append(time.monotonic() - page_started)
                assert page.scope == governed_scope
                assert page.rows_read <= page_size + 1
                binding_ids.extend(item.binding_id for item in page.items)
                if page.next_key is None:
                    break
                request = GovernedBindingFactsRequest(
                    scope=scope,
                    page_size=page_size,
                    after=page.next_key,
                    expected_scope=page.scope,
                )
            assert len(binding_ids) == 31
            assert len(set(binding_ids)) == 31
            governed_orders[page_size] = tuple(binding_ids)
        governed_elapsed = time.monotonic() - governed_started
    record_property("governed_cold_page_seconds", round(cold_elapsed, 6))
    record_property("governed_traversal_pages", len(governed_page_elapsed))
    record_property("governed_traversal_seconds", round(governed_elapsed, 6))
    record_property(
        "governed_traversal_max_page_seconds",
        round(max(governed_page_elapsed), 6),
    )
    assert governed_orders[1] == governed_orders[17] == governed_orders[50]
    assert cold_page.items[0].binding_id == governed_orders[1][0]
    assert len(governed_page_elapsed) == 34
    assert max(governed_page_elapsed) < 5
    assert governed_scope.pointer_generation == pointer_v1.generation
    assert governed_scope.registry_version == registry_v1.version
    assert governed_scope.registry_fingerprint == registry_v1.fingerprint
    assert governed_scope.evidence_head_revision == baseline_commit.head_revision
    assert governed_scope.evidence_baseline_revision == baseline_commit.baseline.revision
    assert governed_scope.evidence_baseline_fingerprint == baseline_commit.baseline.fingerprint

    north_star_plan = _require_plan(workflow)
    north_star_dependencies = semantic_plan_dependencies(north_star_plan, scope)
    gate_reader = PostgresSemanticChangeGateReader(semantic_acceptance_database.runtime)
    noise_asset = _unrelated_assets(1)[0].model_copy(
        update={
            "description": "Synthetic unrelated metadata changed after semantic baseline",
            "metadata_fingerprint": _digest(f"m26-unrelated-change:{suffix}"),
        }
    )
    unrelated_generation = _run_catalog_delta(
        semantic_acceptance_database,
        scope,
        changes=(
            CatalogSourceChange(
                kind=CatalogSourceChangeKind.UPSERT_ASSET,
                asset=noise_asset,
            ),
        ),
        label=f"unrelated-{suffix}",
    )
    assert unrelated_generation == 3
    unrelated_report = inspector.execute()
    assert unrelated_report.status is SemanticChangeStatus.CURRENT
    assert not unrelated_report.findings
    assert unrelated_report.impacts.complete
    assert unrelated_report.impacts.workflow_count == 0
    assert unrelated_report.impacts.recipe_count == 0
    unrelated_observation = store.load_observation(scope, unrelated_report.id)
    assert unrelated_observation is not None
    assert len(unrelated_observation.fields) == 31
    assert gate_reader.assess(north_star_dependencies).eligible

    first_physical = registry_v1.mapping_set.mappings[0].mapping.physical_field.root
    first_dataset, first_field = first_physical.rsplit(".", 1)
    definition_generation = _run_catalog_delta(
        semantic_acceptance_database,
        scope,
        changes=(
            CatalogSourceChange(
                kind=CatalogSourceChangeKind.UPSERT_ASSET,
                asset=_governed_asset(
                    registry_v1,
                    first_dataset,
                    definition_overrides={
                        first_field: "Acceptance-approved customer identity definition"
                    },
                ),
            ),
        ),
        label=f"definition-{suffix}",
        normalized_types=_governed_normalized_types(registry_v1, first_dataset),
    )
    assert definition_generation == 4
    definition_report = inspector.execute()
    assert definition_report.status is SemanticChangeStatus.REVIEW_REQUIRED
    assert SemanticChangeKind.FIELD_DEFINITION_CHANGED in {
        item.kind for item in definition_report.findings
    }
    assert definition_report.impacts.workflow_count == 1
    assert definition_report.impacts.recipe_count == 1
    compatible_commit = _approve_report(
        inspector,
        store,
        definition_report,
        action=SemanticChangeDecisionAction.REVALIDATE_COMPATIBLE_CHANGE,
        confirmation=SemanticChangeConfirmation.REVALIDATE,
        minute=6,
    )
    assert compatible_commit.head_revision == 2

    assert gate_reader.assess(north_star_dependencies).eligible

    type_generation = _run_catalog_delta(
        semantic_acceptance_database,
        scope,
        changes=(
            CatalogSourceChange(
                kind=CatalogSourceChangeKind.UPSERT_ASSET,
                asset=_governed_asset(
                    registry_v1,
                    first_dataset,
                    definition_overrides={
                        first_field: "Acceptance-approved customer identity definition"
                    },
                    type_overrides={first_field: PhysicalValueType.INTEGER},
                ),
            ),
        ),
        label=f"type-{suffix}",
        normalized_types=_governed_normalized_types(
            registry_v1,
            first_dataset,
            type_overrides={first_field: PhysicalValueType.INTEGER},
        ),
    )
    assert type_generation == 5
    type_report = inspector.execute()
    assert type_report.status is SemanticChangeStatus.BLOCKED
    assert SemanticChangeKind.PHYSICAL_TYPE_CHANGED in {item.kind for item in type_report.findings}

    blocked_compiler = _CountingCompiler()
    blocked_guard = _CountingGuard()
    blocked_preview = _CountingPreview()
    blocked_rejections = _CountingRejections()
    blocked_prepare = _guarded_prepare(
        registry_v1,
        scope,
        semantic_acceptance_database.runtime,
        blocked_compiler,
        blocked_guard,
    )
    with pytest.raises(SemanticChangeError) as blocked:
        blocked_prepare.validate_resolved(north_star_plan)
    assert blocked.value.code is SemanticChangeErrorCode.STALE_CONTEXT
    assert (
        blocked_compiler.calls,
        blocked_guard.calls,
        blocked_preview.calls,
        blocked_rejections.calls,
    ) == (0, 0, 0, 0)

    product_plan = _product_plan(registry_v1, scope, pointer_v1)
    product_dependencies = semantic_plan_dependencies(product_plan, scope)
    assert not {item.approval_decision_id for item in product_dependencies.mappings}.intersection(
        {
            finding.mapping.approval_decision_id
            for finding in type_report.findings
            if finding.mapping is not None
        }
    )
    unaffected_compiler = _CountingCompiler()
    unaffected_guard = _CountingGuard()
    unaffected_preview = _CountingPreview()
    unaffected_rejections = _CountingRejections()
    unaffected_prepare = _guarded_prepare(
        registry_v1,
        scope,
        semantic_acceptance_database.runtime,
        unaffected_compiler,
        unaffected_guard,
    )
    prepared = unaffected_prepare.validate_resolved(product_plan)
    result = ExecuteGovernedRequest(
        prepare=unaffected_prepare,
        executor=unaffected_preview,
        rejection_reporter=unaffected_rejections,
    ).execute_prepared(prepared)
    assert result.preview.transaction_read_only
    assert (
        unaffected_compiler.calls,
        unaffected_guard.calls,
        unaffected_preview.calls,
        unaffected_rejections.calls,
    ) == (1, 1, 1, 1)

    key_generation = _run_catalog_delta(
        semantic_acceptance_database,
        scope,
        changes=(
            CatalogSourceChange(
                kind=CatalogSourceChangeKind.UPSERT_ASSET,
                asset=_governed_asset(
                    registry_v1,
                    first_dataset,
                    definition_overrides={
                        first_field: "Acceptance-approved customer identity definition"
                    },
                    key_overrides={first_field: False},
                ),
            ),
        ),
        label=f"key-{suffix}",
        normalized_types=_governed_normalized_types(registry_v1, first_dataset),
    )
    assert key_generation == 6
    key_report = inspector.execute()
    assert key_report.status is SemanticChangeStatus.BLOCKED
    assert SemanticChangeKind.KEY_STATUS_CHANGED in {item.kind for item in key_report.findings}

    removal_generation = _run_catalog_delta(
        semantic_acceptance_database,
        scope,
        changes=(
            CatalogSourceChange(
                kind=CatalogSourceChangeKind.DELETE_FIELD,
                asset_id=CatalogAssetId(first_dataset),
                field_path=(first_field,),
            ),
        ),
        label=f"removal-{suffix}",
    )
    assert removal_generation == 7
    removal_report = inspector.execute()
    assert removal_report.status is SemanticChangeStatus.BLOCKED
    assert SemanticChangeKind.FIELD_REMOVED in {item.kind for item in removal_report.findings}
    old_removal_snapshot = store.load_report(scope, removal_report.id)
    with pytest.raises(SemanticChangeError) as forbidden_waiver:
        PrepareSemanticChangeDecision(store).execute(
            scope,
            removal_report.id,
            action=SemanticChangeDecisionAction.REVALIDATE_COMPATIBLE_CHANGE,
        )
    assert forbidden_waiver.value.code is SemanticChangeErrorCode.BLOCKING_CHANGE

    restored_generation = _run_catalog_delta(
        semantic_acceptance_database,
        scope,
        changes=(
            CatalogSourceChange(
                kind=CatalogSourceChangeKind.UPSERT_ASSET,
                asset=_governed_asset(
                    registry_v1,
                    first_dataset,
                    definition_overrides={
                        first_field: "Acceptance-approved customer identity definition"
                    },
                ),
            ),
        ),
        label=f"restored-{suffix}",
        normalized_types=_governed_normalized_types(registry_v1, first_dataset),
    )
    assert restored_generation == 8
    registry_v2 = _corrected_registry(recorded, scope, suffix)
    _publish_registry(registry_v2, scope, writer_config, minute=7)
    loaded_v2 = versions.load_version(scope, 2)
    assert loaded_v2.trust is RegistryVersionTrust.STRICT
    assert loaded_v2.snapshot.registry == registry_v2
    pointer_v2 = _activate_registry(control, versions, scope, 2, minute=8)
    assert pointer_v2.generation == 2
    assert pointer_v2.registry_version == 2
    assert pointer_v2.registry_fingerprint != pointer_v1.registry_fingerprint

    corrected_dependencies = dependency_reconciler.execute(
        scope,
        watermark=3,
        indexed_at=NOW + timedelta(minutes=9),
    )
    assert corrected_dependencies.state.complete
    corrected_profiles = _AggregateProfiles(
        {item.id: item.cardinality for item in registry_v2.join_contracts.contracts}
    )
    corrected_inspector = InspectSemanticChange(
        pointers=pointers,
        versions=versions,
        evidence=PostgresSemanticChangeEvidenceReader(
            semantic_acceptance_database.reconciler,
            corrected_profiles,
            clock=lambda: NOW + timedelta(minutes=10),
        ),
        dependency_index=index,
        store=store,
        scope=scope,
    )
    corrected_report = corrected_inspector.execute()
    assert corrected_report.status is SemanticChangeStatus.REVIEW_REQUIRED
    assert corrected_report.baseline_revision is None
    assert corrected_report.context.registry_version == 2
    assert corrected_report.impacts.workflow_count == 1
    assert corrected_report.impacts.recipe_count == 1
    corrected_commit = _approve_report(
        corrected_inspector,
        store,
        corrected_report,
        action=SemanticChangeDecisionAction.ESTABLISH_BASELINE,
        confirmation=SemanticChangeConfirmation.ESTABLISH,
        minute=11,
    )
    assert corrected_commit.head_revision == 3
    assert corrected_commit.baseline is not None
    assert corrected_commit.baseline.context_fingerprint == corrected_report.context.fingerprint
    assert store.load_report(scope, removal_report.id) == old_removal_snapshot

    corrected_north_star = _north_star_plan(registry_v2, scope, pointer_v2)
    assert gate_reader.assess(semantic_plan_dependencies(corrected_north_star, scope)).eligible

    head = LoadSemanticChangeHead(store).execute(scope)
    assert head == corrected_commit
    audit = VerifySemanticChangeAudit(
        LoadSemanticChangeHead(store),
        control,
    ).execute(scope)
    assert audit.valid
    assert audit.head_revision == 3
    assert audit.event_count >= 5
    assert len(control.list_transitions(scope)) == 2

    read_store = PostgresSemanticChangeReadStore(semantic_acceptance_database.api)
    impact_page = read_store.list_impacts(
        scope.workspace_id,
        definition_report.id,
        filters=_impact_filters(),
        page_size=1,
        after=None,
    )
    assert len(impact_page.items) == 1
    assert impact_page.has_more
    assert impact_page.last_key is not None
    assert read_store.load_report("workspace-cross-tenant", definition_report.id) is None

    scan_count, source_kind_count = _scan_request_counts(
        semantic_acceptance_database.migrator,
        scope.workspace_id,
    )
    assert scan_count >= 10
    assert source_kind_count == 2

    if os.environ.get(_M27_BROWSER_SEED) == "1":
        physical_only_field_target = primary_field_count - len(registry_v2.mapping_set.mappings)
        scaled_assets = _m27_scaled_unrelated_assets(
            len(filler),
            target_field_count=physical_only_field_target,
        )
        browser_scale_generation = _run_catalog_delta(
            semantic_acceptance_database,
            scope,
            changes=tuple(
                CatalogSourceChange(
                    kind=CatalogSourceChangeKind.UPSERT_ASSET,
                    asset=asset,
                )
                for asset in scaled_assets
            ),
            label=f"m27-browser-scale-{suffix}",
        )
        assert browser_scale_generation == 9
        assert _catalog_counts(
            semantic_acceptance_database.migrator,
            scope.workspace_id,
            generation=browser_scale_generation,
        ) == (primary_asset_count, primary_field_count)
        if browser_connection_count == 2:
            _seed_catalog_generation(
                semantic_acceptance_database,
                scope,
                registry_v2,
                connection_id=_M27_SECONDARY_CONNECTION_ID,
                generation=1,
                create_connection=True,
                only_fields={_M27_CROSS_CONNECTION_PHYSICAL_FIELD},
            )
        assert _active_catalog_counts(
            semantic_acceptance_database.migrator,
            scope,
        ) == (
            browser_connection_count,
            browser_asset_count,
            browser_field_count,
        )
        browser_scale_report = corrected_inspector.execute()
        assert browser_scale_report.status is SemanticChangeStatus.CURRENT
        assert not browser_scale_report.findings
        browser_scale_commit = _approve_report(
            corrected_inspector,
            store,
            browser_scale_report,
            action=SemanticChangeDecisionAction.REVALIDATE_COMPATIBLE_CHANGE,
            confirmation=SemanticChangeConfirmation.REVALIDATE,
            minute=12,
        )
        assert browser_scale_commit.head_revision == 4
        assert browser_scale_commit.baseline is not None
        assert (
            browser_scale_commit.baseline.catalog_generations
            == browser_scale_report.catalog_generations
        )
        browser_scale_gate = gate_reader.assess(
            semantic_plan_dependencies(corrected_north_star, scope)
        )
        assert browser_scale_gate.eligible
        assert browser_scale_gate.status is SemanticChangeStatus.REVALIDATED

    if os.environ.get(_BROWSER_SEED) == "1":
        browser_current = corrected_inspector.execute()
        assert browser_current.status is SemanticChangeStatus.CURRENT
        assert not browser_current.findings

        browser_definition_generation = _run_catalog_delta(
            semantic_acceptance_database,
            scope,
            changes=(
                CatalogSourceChange(
                    kind=CatalogSourceChangeKind.UPSERT_ASSET,
                    asset=_governed_asset(
                        registry_v2,
                        first_dataset,
                        definition_overrides={
                            first_field: (
                                "Browser acceptance compatible customer identity definition"
                            )
                        },
                    ),
                ),
            ),
            label=f"browser-definition-{suffix}",
            normalized_types=_governed_normalized_types(
                registry_v2,
                first_dataset,
            ),
        )
        assert browser_definition_generation == 9
        browser_review = corrected_inspector.execute()
        assert browser_review.status is SemanticChangeStatus.REVIEW_REQUIRED
        assert SemanticChangeKind.FIELD_DEFINITION_CHANGED in {
            item.kind for item in browser_review.findings
        }

        browser_type_generation = _run_catalog_delta(
            semantic_acceptance_database,
            scope,
            changes=(
                CatalogSourceChange(
                    kind=CatalogSourceChangeKind.UPSERT_ASSET,
                    asset=_governed_asset(
                        registry_v2,
                        first_dataset,
                        definition_overrides={
                            first_field: (
                                "Browser acceptance compatible customer identity definition"
                            )
                        },
                        type_overrides={first_field: PhysicalValueType.INTEGER},
                    ),
                ),
            ),
            label=f"browser-type-{suffix}",
            normalized_types=_governed_normalized_types(
                registry_v2,
                first_dataset,
                type_overrides={first_field: PhysicalValueType.INTEGER},
            ),
        )
        assert browser_type_generation == 10
        browser_blocked = corrected_inspector.execute()
        assert browser_blocked.status is SemanticChangeStatus.BLOCKED
        assert SemanticChangeKind.PHYSICAL_TYPE_CHANGED in {
            item.kind for item in browser_blocked.findings
        }


def _role_dsn(role: str, database: str) -> str:
    return f"postgresql://{role}:{role}@127.0.0.1:55434/{database}"


def _publish_registry(
    registry: GovernedSemanticRegistrySnapshot,
    scope: SemanticRegistryScope,
    config: DataHubRegistryWriteConfig,
    *,
    minute: int,
) -> None:
    actor = "sb_m26_acceptance_registry_publisher"
    approval = RegistryPublicationApproval(
        id=registry_publication_approval_id(registry, scope, actor),
        workspace_id=scope.workspace_id,
        registry_id=registry.registry_id,
        registry_version=registry.version,
        catalog_scope=registry.catalog_scope,
        payload_fingerprint=registry.fingerprint,
        target=datahub_registry_document_urn(scope, registry.version),
        actor=actor,
        approved_at=NOW + timedelta(minutes=minute),
        decision_ids=semantic_registry_decision_ids(registry),
        confirmation=RegistryPublicationConfirmation.PUBLISH_APPROVED_REGISTRY_VERSION,
    )
    result = DataHubSemanticRegistryPublisher(config).publish(registry, approval)
    assert result.status in {
        RegistryPublicationStatus.PUBLISHED,
        RegistryPublicationStatus.ALREADY_CURRENT,
    }, result.reason_code


def _activate_registry(
    store: PostgresRegistryControlStore,
    versions: DataHubRegistryVersionReader,
    scope: SemanticRegistryScope,
    version: int,
    *,
    minute: int,
) -> ActiveRegistryPointer:
    proposal = PrepareRegistryActivation(store, versions, scope).execute(version)
    approval = PrepareRegistryActivationApproval().execute(
        proposal,
        actor="sb_m26_acceptance_registry_operator",
        approved_at=NOW + timedelta(minutes=minute),
        confirmation=RegistryActivationConfirmation.ACTIVATE_APPROVED_REGISTRY_VERSION,
    )
    commit = CommitRegistryActivation(store, versions).execute(
        proposal,
        approval,
        committed_at=NOW + timedelta(minutes=minute, seconds=1),
    )
    return commit.transition.active_pointer


def _unrelated_assets(count: int) -> tuple[CatalogSourceAsset, ...]:
    native_types = ("varchar", "bigint", "numeric", "date", "boolean", "jsonb")
    assets: list[CatalogSourceAsset] = []
    for index in range(count):
        asset_id = f"noise.table_{index:05d}"
        native_type = native_types[index % len(native_types)]
        field_name = ("contract_id", "customer_ref", "amount", "event_date")[index % 4]
        assets.append(
            CatalogSourceAsset(
                asset_id=CatalogAssetId(asset_id),
                qualified_name=asset_id,
                display_name=f"table_{index:05d}",
                platform="postgres",
                environment="PROD" if index % 3 else "QA",
                schema_name="noise",
                description=f"Synthetic heterogeneous acceptance asset {index}",
                fields=(
                    CatalogSourceField(
                        field_path=(field_name,),
                        native_type=native_type,
                        description=f"Synthetic {field_name} variant {index % 11}",
                        nullable=index % 5 == 0,
                        is_part_of_key=index % 7 == 0,
                        tags=(f"domain_{index % 9}",),
                        glossary_terms=(f"term_{index % 13}",),
                        metadata_fingerprint=_digest(
                            f"m26-noise-field:{index}:{field_name}:{native_type}"
                        ),
                    ),
                ),
                metadata_fingerprint=_digest(f"m26-noise-asset:{index}"),
            )
        )
    return tuple(assets)


def _m27_scaled_unrelated_assets(
    count: int,
    *,
    target_field_count: int,
) -> tuple[CatalogSourceAsset, ...]:
    """Create a heterogeneous exact-size physical-only population for browser proof."""

    maximum_fields_per_asset = 64
    if count < 1 or not count <= target_field_count <= count * maximum_fields_per_asset:
        raise ValueError("M27 physical-only field target is invalid")
    field_counts = [3 + index % 10 for index in range(count)]
    for index in range(count):
        if (index + 1) % 997 == 0:
            field_counts[index] = maximum_fields_per_asset
    difference = target_field_count - sum(field_counts)
    while difference > 0:
        progressed = False
        for index, current in enumerate(field_counts):
            if current >= maximum_fields_per_asset:
                continue
            field_counts[index] = current + 1
            difference -= 1
            progressed = True
            if difference == 0:
                break
        if not progressed:
            raise ValueError("M27 physical-only field target exceeds fixture capacity")
    while difference < 0:
        progressed = False
        for index in range(len(field_counts) - 1, -1, -1):
            current = field_counts[index]
            if current <= 1:
                continue
            field_counts[index] = current - 1
            difference += 1
            progressed = True
            if difference == 0:
                break
        if not progressed:
            raise ValueError("M27 physical-only field target is below fixture capacity")

    native_types = (
        "varchar(20)",
        "bigint",
        "numeric(18,2)",
        "date",
        "boolean",
        "jsonb",
        "double precision",
        "timestamp with time zone",
        "uuid",
    )
    field_names = (
        "contract_id",
        "customer_ref",
        "amount",
        "event_date",
        "holder_type",
        "status_code",
        "account_no",
        "created_at",
        "notes",
        "is_active",
        "shared_identifier",
        "payload_version",
    )
    assets: list[CatalogSourceAsset] = []
    observed_fields = 0
    for index, field_count in enumerate(field_counts):
        asset_id = f"noise.table_{index:05d}"
        is_support_decoy = index == 0
        qualified_name = _M27_SUPPORT_DECOY_ASSET if is_support_decoy else asset_id
        fields: list[CatalogSourceField] = []
        for position in range(field_count):
            support_decoy_field = (
                _M27_SUPPORT_DECOY_FIELD_DESCRIPTIONS[position]
                if is_support_decoy and position < len(_M27_SUPPORT_DECOY_FIELD_DESCRIPTIONS)
                else None
            )
            base_name = (
                support_decoy_field[0]
                if support_decoy_field is not None
                else field_names[(index + position) % len(field_names)]
            )
            is_hostile_metadata_fixture = index == count - 1 and position == field_count - 1
            field_path: tuple[str, ...]
            if support_decoy_field is not None:
                field_path = (support_decoy_field[0],)
            elif is_hostile_metadata_fixture:
                field_path = (_M27_HOSTILE_FIELD_SEGMENT,)
            elif position == 0:
                field_path = (base_name,)
            elif position % 11 == 0:
                field_path = ("payload", "identifiers", f"variant_{position:02d}")
            elif position % 7 == 0:
                field_path = (f"dirección_envío_{position:02d}",)
            else:
                field_path = (f"{base_name}_{position:02d}",)
            native_type = (
                "varchar(24)"
                if support_decoy_field is not None
                else native_types[(index + position) % len(native_types)]
            )
            description = (
                f"{_M27_HOSTILE_METADATA_QUERY} {_M27_HOSTILE_METADATA_HTML} "
                f"{_M27_HOSTILE_METADATA_BIDI}reversed marker"
                if is_hostile_metadata_fixture
                else (
                    support_decoy_field[1]
                    if support_decoy_field is not None
                    else f"Synthetic M27 {base_name} representation {index % 17}:{position}"
                )
            )
            fields.append(
                CatalogSourceField(
                    field_path=field_path,
                    native_type=native_type,
                    description=description,
                    nullable=(index + position) % 5 == 0,
                    is_part_of_key=position == 0 or position % 13 == 0,
                    tags=(f"domain_{index % 9}", "synthetic"),
                    glossary_terms=(f"term_{(index + position) % 13}",),
                    metadata_fingerprint=_digest(
                        "m27-noise-field:"
                        f"{index}:{position}:{'.'.join(field_path)}:{native_type}:"
                        f"{description if is_hostile_metadata_fixture or support_decoy_field else ''}"
                    ),
                )
            )
        observed_fields += len(fields)
        assets.append(
            CatalogSourceAsset(
                asset_id=CatalogAssetId(asset_id),
                qualified_name=qualified_name,
                display_name="order_cases" if is_support_decoy else f"table_{index:05d}",
                platform="postgres",
                environment="PROD" if index % 3 else "QA",
                schema_name="support" if is_support_decoy else "noise",
                description=(
                    "Synthetic physical-only support ticket references; not governed."
                    if is_support_decoy
                    else f"Synthetic heterogeneous M27 acceptance asset {index}"
                ),
                fields=tuple(sorted(fields, key=lambda field: field.field_path)),
                metadata_fingerprint=_digest(
                    f"m27-support-decoy-asset:{qualified_name}:{index}:{field_count}"
                    if is_support_decoy
                    else f"m27-noise-asset:{index}:{field_count}"
                ),
            )
        )
    if observed_fields != target_field_count:
        raise ValueError("M27 physical-only field target was not generated exactly")
    return tuple(assets)


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
    """Seed one exact v9 generation through its governed connector binding."""

    omitted = omitted_fields or set()
    overrides = type_overrides or {}
    selected = tuple(
        item
        for item in registry.mapping_set.mappings
        if (only_fields is None or item.mapping.physical_field.root in only_fields)
        and item.mapping.physical_field.root not in omitted
    )
    grouped: dict[str, list[tuple[str, PhysicalValueType]]] = {}
    for item in selected:
        physical = item.mapping.physical_field.root
        dataset, field_name = physical.rsplit(".", 1)
        grouped.setdefault(dataset, []).append(
            (field_name, overrides.get(physical, item.physical_type))
        )

    requested_at = datetime.now(UTC)
    if create_connection:
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
                    %s, 10, 10000, 100000, 1000, 60, 100, 900, 3600, 1,
                    'sb_platform_admin_v1', %s, %s
                )
                ON CONFLICT (workspace_id) DO NOTHING
                """,
                (scope.workspace_id, requested_at, requested_at),
            )
        PostgresCatalogConnectionStore(urls.api).register(
            CatalogConnectionRegistration(
                workspace_id=scope.workspace_id,
                connection_id=CatalogConnectionId(connection_id),
                display_name=connection_id,
                kind=CatalogConnectionKind.SYNTHETIC,
                environment="PROD",
                catalog_scope=scope.catalog_scope,
                requested_by="sb_catalog_admin_v1",
                requested_at=requested_at,
                idempotency_digest=_digest(f"{connection_id}-registration"),
            )
        )

    catalog_connection_id = CatalogConnectionId(connection_id)
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
                    native_type=_postgres_native_type(normalized),
                    normalized_type=normalized,
                    description=f"Stable definition for {dataset}.{field_name}",
                    nullable=False,
                    is_part_of_key=True,
                    tags=("governed",),
                    glossary_terms=("semantic",),
                    metadata_fingerprint=_digest(
                        "field:"
                        f"{connection_id}:{dataset}.{field_name}:"
                        f"{_postgres_native_type(normalized)}:{normalized.value}"
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
                        native_type="text",
                        normalized_type=PhysicalValueType.STRING,
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
    catalog = PostgresCatalogRefreshStore(urls.catalog)
    leased = catalog.claim_next(
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
    staging = catalog.begin_staging(
        scope.workspace_id,
        leased.refresh_id,
        indexer_id=leased.lease.indexer_id,
        lease_capability=LEASE_CAPABILITY,
        fencing_token=leased.lease.fencing_token,
    )
    catalog.persist_page(
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
    completed = catalog.complete(
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


def _postgres_native_type(physical_type: PhysicalValueType) -> str:
    native_type = {
        PhysicalValueType.STRING: "text",
        PhysicalValueType.INTEGER: "bigint",
        PhysicalValueType.FLOAT: "double precision",
        PhysicalValueType.DECIMAL: "numeric",
        PhysicalValueType.BOOLEAN: "boolean",
        PhysicalValueType.DATE: "date",
        PhysicalValueType.TIMESTAMP: "timestamp with time zone",
    }.get(physical_type)
    if native_type is None:
        raise ValueError("acceptance seed requires an executable PostgreSQL type")
    normalized = normalize_postgres_native_type(native_type)
    if (
        normalized.contract_fingerprint != postgres_type_contract_fingerprint()
        or not normalized.executable
        or normalized.normalized_type is not physical_type
    ):
        raise ValueError("acceptance seed PostgreSQL type is not canonical")
    return native_type


def _run_catalog_delta(
    urls: _DatabaseUrls,
    scope: SemanticRegistryScope,
    *,
    changes: tuple[CatalogSourceChange, ...],
    label: str,
    normalized_types: dict[str, PhysicalValueType] | None = None,
) -> int:
    requested_at = datetime.now(UTC)
    target = ensure_catalog_connector_target(
        urls.migrator,
        workspace_id=scope.workspace_id,
        connection_id=_connection_id(),
    )
    requested = PostgresCatalogRefreshStore(
        urls.api,
        application_name="schemabridge-control-api",
    ).request(
        CatalogRefreshCommand(
            workspace_id=scope.workspace_id,
            connection_id=_connection_id(),
            mode=CatalogRefreshMode.DELTA,
            requested_by="sb_catalog_admin_v1",
            requested_at=requested_at,
            idempotency_digest=_digest(label),
        )
    )
    pool = PostgresControlPool(
        ControlPoolSettings(
            dsn=urls.catalog,
            application_name="schemabridge-control-catalog",
            min_size=1,
            max_size=2,
            max_waiting=4,
            acquisition_timeout_seconds=5,
            startup_timeout_seconds=15,
            statement_timeout_ms=60_000,
        )
    )
    with pool:
        catalog = PostgresCatalogRefreshStore(
            urls.catalog,
            connection_provider=pool,
        )
        leased = catalog.claim_next(
            indexer_id="semantic-acceptance-indexer",
            lease_capability=LEASE_CAPABILITY,
            lease_duration=timedelta(minutes=5),
        )
        assert leased is not None
        assert leased.refresh_id == requested.refresh.refresh_id
        assert leased.lease is not None
        acquire_catalog_refresh_route(
            urls.catalog,
            workspace_id=scope.workspace_id,
            connection_id=_connection_id(),
            refresh_id=leased.refresh_id,
            indexer_id=leased.lease.indexer_id,
            lease_capability=LEASE_CAPABILITY,
            fencing_token=leased.lease.fencing_token,
        )
        staging = catalog.begin_staging(
            scope.workspace_id,
            leased.refresh_id,
            indexer_id=leased.lease.indexer_id,
            lease_capability=LEASE_CAPABILITY,
            fencing_token=leased.lease.fencing_token,
        )
        batches = tuple(changes[offset : offset + 50] for offset in range(0, len(changes), 50))
        assert batches
        for index, batch in enumerate(batches, start=1):
            terminal = index == len(batches)
            catalog.persist_page(
                scope.workspace_id,
                leased.refresh_id,
                indexer_id=leased.lease.indexer_id,
                lease_capability=LEASE_CAPABILITY,
                fencing_token=leased.lease.fencing_token,
                page=CatalogSourcePage.create(
                    mode=CatalogRefreshMode.DELTA,
                    sequence=index,
                    changes=batch,
                    next_checkpoint=None if terminal else f"m26-page-{index:04d}",
                    source_complete=terminal,
                ),
            )
        if normalized_types:
            _persist_normalized_types(
                urls.migrator,
                scope.workspace_id,
                generation=staging.target_generation,
                normalized_types=normalized_types,
            )
        completed = catalog.complete(
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
    return completed.target_generation


def _persist_normalized_types(
    dsn: str,
    workspace_id: str,
    *,
    generation: int,
    normalized_types: dict[str, PhysicalValueType],
) -> None:
    """Stand in for the upstream type-normalization phase absent from source pages."""

    with psycopg.connect(dsn) as connection:
        for physical_field, normalized_type in sorted(normalized_types.items()):
            dataset, field_name = physical_field.rsplit(".", 1)
            updated = connection.execute(
                """
                UPDATE schemabridge_control.catalog_fields AS field
                SET normalized_type = %s
                FROM schemabridge_control.catalog_assets AS asset
                WHERE field.workspace_id = %s
                  AND field.connection_id = 'warehouse-primary'
                  AND field.generation = %s
                  AND field.asset_key = asset.asset_key
                  AND asset.workspace_id = field.workspace_id
                  AND asset.connection_id = field.connection_id
                  AND asset.generation = field.generation
                  AND asset.asset_id = %s
                  AND field.field_path = %s
                """,
                (
                    normalized_type.value,
                    workspace_id,
                    generation,
                    dataset,
                    [field_name],
                ),
            )
            assert updated.rowcount == 1


def _connection_id() -> CatalogConnectionId:
    return CatalogConnectionId("warehouse-primary")


def _catalog_counts(
    dsn: str,
    workspace_id: str,
    *,
    generation: int,
) -> tuple[int, int]:
    with psycopg.connect(dsn) as connection:
        row = connection.execute(
            """
            SELECT
                (SELECT count(*)
                 FROM schemabridge_control.catalog_assets
                 WHERE workspace_id = %s
                   AND connection_id = 'warehouse-primary'
                   AND generation = %s),
                (SELECT count(*)
                 FROM schemabridge_control.catalog_fields
                 WHERE workspace_id = %s
                   AND connection_id = 'warehouse-primary'
                   AND generation = %s)
            """,
            (workspace_id, generation, workspace_id, generation),
        ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


def _active_catalog_counts(
    dsn: str,
    scope: SemanticRegistryScope,
) -> tuple[int, int, int]:
    """Read aggregate active inventory facts without materializing physical rows."""

    with psycopg.connect(dsn) as connection:
        row = connection.execute(
            """
            SELECT connection_count, asset_count, field_count
            FROM schemabridge_control.load_physical_discovery_scope(
                %s::varchar, %s::varchar, %s::varchar
            )
            """,
            (scope.workspace_id, scope.catalog_scope, scope.registry_id),
        ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1]), int(row[2])


def _assert_initial_candidate_scale_plan(
    urls: _DatabaseUrls,
    scope: SemanticRegistryScope,
    registry: GovernedSemanticRegistrySnapshot,
    *,
    generation: int,
) -> None:
    requested = [
        {
            "ordinal": ordinal,
            "dataset_ref": dataset,
            "field_path": list(field_path),
            "selected_connection_id": None,
            "selected_asset_id": None,
            "selected_field_path": None,
        }
        for ordinal, mapping in enumerate(registry.mapping_set.mappings)
        for dataset, field_path in (_physical_parts(mapping.mapping.physical_field.root),)
    ]
    with psycopg.connect(urls.reconciler) as connection:
        explained = connection.execute(
            """
            EXPLAIN (
                FORMAT JSON, ANALYZE TRUE, BUFFERS TRUE,
                COSTS TRUE, TIMING FALSE
            )
            SELECT *
            FROM schemabridge_control.load_semantic_initial_catalog_candidates(
                %s, %s, %s::jsonb
            )
            """,
            (
                scope.workspace_id,
                scope.catalog_scope,
                Jsonb(requested),
            ),
        ).fetchone()
    assert explained is not None
    function_plan = explained[0][0]
    assert function_plan["Plan"]["Node Type"] == "Function Scan"
    assert int(function_plan["Plan"]["Actual Rows"]) == len(requested) == 31
    touched_blocks = int(function_plan["Plan"].get("Shared Hit Blocks", 0)) + int(
        function_plan["Plan"].get("Shared Read Blocks", 0)
    )
    assert touched_blocks <= 4_096
    assert float(function_plan["Execution Time"]) < 5_000

    first_dataset, first_path = _physical_parts(
        registry.mapping_set.mappings[0].mapping.physical_field.root
    )
    with psycopg.connect(urls.migrator) as connection:
        cold_statistics = connection.execute(
            """
            SELECT relation.relname, relation.reltuples
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'schemabridge_control'
              AND relation.relname = ANY(
                  ARRAY['catalog_assets', 'catalog_fields']
              )
            """
        ).fetchall()
        assert {str(row[0]) for row in cold_statistics} == {
            "catalog_assets",
            "catalog_fields",
        }
        assert all(float(row[1]) < 0 for row in cold_statistics)
        asset = connection.execute(
            """
            SELECT asset_key
            FROM (
                SELECT inner_asset.*
                FROM schemabridge_control.catalog_assets AS inner_asset
                WHERE schemabridge_control.semantic_catalog_asset_locator_key(
                          inner_asset.workspace_id,
                          inner_asset.connection_id,
                          inner_asset.generation,
                          inner_asset.qualified_name
                      )
                      = schemabridge_control.semantic_catalog_asset_locator_key(
                          %s, 'warehouse-primary', %s, %s
                      )
                OFFSET 0
            ) AS asset
            WHERE asset.workspace_id = %s
              AND asset.connection_id = 'warehouse-primary'
              AND asset.generation = %s
              AND asset.qualified_name = %s
            """,
            (
                scope.workspace_id,
                generation,
                first_dataset,
                scope.workspace_id,
                generation,
                first_dataset,
            ),
        ).fetchone()
        assert asset is not None
        requested_field_key = hashlib.sha256(
            json.dumps(
                list(first_path),
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        asset_plan = connection.execute(
            """
            EXPLAIN (
                FORMAT JSON, ANALYZE TRUE, BUFFERS TRUE,
                COSTS TRUE, TIMING FALSE
            )
            SELECT asset_key
            FROM (
                SELECT inner_asset.*
                FROM schemabridge_control.catalog_assets AS inner_asset
                WHERE schemabridge_control.semantic_catalog_asset_locator_key(
                          inner_asset.workspace_id,
                          inner_asset.connection_id,
                          inner_asset.generation,
                          inner_asset.qualified_name
                      )
                      = schemabridge_control.semantic_catalog_asset_locator_key(
                          %s, 'warehouse-primary', %s, %s
                      )
                OFFSET 0
            ) AS asset
            WHERE asset.workspace_id = %s
              AND asset.connection_id = 'warehouse-primary'
              AND asset.generation = %s
              AND asset.qualified_name = %s
            ORDER BY asset_key
            LIMIT 2
            """,
            (
                scope.workspace_id,
                generation,
                first_dataset,
                scope.workspace_id,
                generation,
                first_dataset,
            ),
        ).fetchone()
        field_plan = connection.execute(
            """
            EXPLAIN (
                FORMAT JSON, ANALYZE TRUE, BUFFERS TRUE,
                COSTS TRUE, TIMING FALSE
            )
            SELECT field_key
            FROM (
                SELECT inner_field.*
                FROM schemabridge_control.catalog_fields AS inner_field
                WHERE schemabridge_control.semantic_catalog_field_locator_key(
                          inner_field.workspace_id,
                          inner_field.connection_id,
                          inner_field.generation,
                          inner_field.asset_key,
                          inner_field.field_key
                      )
                      = schemabridge_control.semantic_catalog_field_locator_key(
                          %s, 'warehouse-primary', %s, %s, %s
                      )
                OFFSET 0
            ) AS field
            WHERE field.workspace_id = %s
              AND field.connection_id = 'warehouse-primary'
              AND field.generation = %s
              AND field.asset_key = %s
              AND field.field_key = %s
              AND field.field_path = %s
            """,
            (
                scope.workspace_id,
                generation,
                str(asset[0]),
                requested_field_key,
                scope.workspace_id,
                generation,
                str(asset[0]),
                requested_field_key,
                list(first_path),
            ),
        ).fetchone()
    assert asset_plan is not None
    assert field_plan is not None
    assert "catalog_assets_semantic_lookup_idx" in _index_names(asset_plan[0][0])
    assert "catalog_fields_semantic_lookup_idx" in _index_names(field_plan[0][0])


def _physical_parts(physical_field: str) -> tuple[str, tuple[str, ...]]:
    parts = physical_field.split(".")
    assert len(parts) >= 3
    return ".".join(parts[:2]), tuple(parts[2:])


def _index_names(value: object) -> set[str]:
    if isinstance(value, dict):
        names = {str(value["Index Name"])} if isinstance(value.get("Index Name"), str) else set()
        return names.union(*(_index_names(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_index_names(item) for item in value))
    return set()


def _governed_normalized_types(
    registry: GovernedSemanticRegistrySnapshot,
    dataset: str,
    *,
    type_overrides: dict[str, PhysicalValueType] | None = None,
) -> dict[str, PhysicalValueType]:
    overrides = type_overrides or {}
    return {
        item.mapping.physical_field.root: overrides.get(field_name, item.physical_type)
        for item in registry.mapping_set.mappings
        for field_name in (item.mapping.physical_field.root.rsplit(".", 1)[1],)
        if item.mapping.physical_field.root.rsplit(".", 1)[0] == dataset
    }


def _governed_asset(
    registry: GovernedSemanticRegistrySnapshot,
    dataset: str,
    *,
    definition_overrides: dict[str, str] | None = None,
    type_overrides: dict[str, PhysicalValueType] | None = None,
    key_overrides: dict[str, bool] | None = None,
) -> CatalogSourceAsset:
    definitions = definition_overrides or {}
    types = type_overrides or {}
    keys = key_overrides or {}
    selected = tuple(
        item
        for item in registry.mapping_set.mappings
        if item.mapping.physical_field.root.rsplit(".", 1)[0] == dataset
    )
    fields = tuple(
        sorted(
            (
                CatalogSourceField(
                    field_path=(field_name,),
                    native_type=_postgres_native_type(types.get(field_name, item.physical_type)),
                    normalized_type=types.get(field_name, item.physical_type),
                    description=definitions.get(
                        field_name,
                        f"Stable definition for {dataset}.{field_name}",
                    ),
                    nullable=False,
                    is_part_of_key=keys.get(field_name, True),
                    tags=("governed",),
                    glossary_terms=("semantic",),
                    metadata_fingerprint=_digest(
                        "field:warehouse-primary:"
                        f"{dataset}.{field_name}:"
                        f"{_postgres_native_type(types.get(field_name, item.physical_type))}:"
                        f"{types.get(field_name, item.physical_type).value}"
                    ),
                )
                for item in selected
                for field_name in (item.mapping.physical_field.root.rsplit(".", 1)[1],)
            ),
            key=lambda item: item.field_path,
        )
    )
    return CatalogSourceAsset(
        asset_id=CatalogAssetId(dataset),
        qualified_name=dataset,
        display_name=dataset.rsplit(".", 1)[-1],
        platform="postgres",
        environment="PROD",
        schema_name=dataset.split(".", 1)[0],
        description=f"Synthetic governed asset {dataset}",
        fields=fields,
        metadata_fingerprint=_digest(f"asset:warehouse-primary:{dataset}:stable"),
    )


def _active_workflow(
    tmp_path: Path,
    registry: GovernedSemanticRegistrySnapshot,
    scope: SemanticRegistryScope,
    pointer: ActiveRegistryPointer,
    *,
    workflow_id: str,
) -> AgentWorkflowDraft:
    orchestrator = _orchestrator(tmp_path / "m26-workflow.sqlite3")
    paused = orchestrator.start(
        StartWorkflowCommand(
            id=workflow_id,
            text=(
                "Agrupa por fecha de registro todos los clientes que sean segundo titular "
                "de una cuenta."
            ),
            language=UserLanguage.SPANISH,
            datasets=(
                _dataset("crm.customers"),
                _dataset("bank.account_holders"),
            ),
        )
    )
    assert paused.intent is not None
    validated = orchestrator.decide_intent(
        paused.id,
        IntentWorkflowDecision(
            actor="sb_m26_acceptance_operator",
            interpretation_fingerprint=paused.intent.interpretation_fingerprint,
            selected_alternative=IntentAlternativeId.COUNT_DISTINCT_CUSTOMERS,
        ),
    )
    assert validated.plan_fingerprint is not None
    executed = orchestrator.decide_execution(
        validated.id,
        ExecutionWorkflowDecision(
            actor="sb_m26_acceptance_operator",
            plan_fingerprint=validated.plan_fingerprint,
            action=WorkflowDecisionAction.APPROVE,
        ),
    )
    assert executed.resolved_plan is not None
    assert executed.execution is not None
    active_plan = executed.resolved_plan.model_copy(
        update={
            "context_source": registry.source,
            "context_version": registry.version,
            "context_fingerprint": registry.fingerprint,
            "activation_generation": pointer.generation,
            "active_pointer_fingerprint": registry_projection_fingerprint(pointer),
            "active_scope_fingerprint": semantic_registry_scope_fingerprint(scope),
        }
    )
    plan_fingerprint = resolved_semantic_plan_fingerprint(active_plan)
    active_execution = executed.execution.model_copy(update={"plan_fingerprint": plan_fingerprint})
    return AgentWorkflowDraft.model_validate(
        {
            **executed.model_dump(mode="python"),
            "resolved_plan": active_plan.model_dump(mode="python"),
            "plan_fingerprint": plan_fingerprint,
            "execution": active_execution.model_dump(mode="python"),
        }
    )


def _dataset(value: str) -> PhysicalDatasetRef:
    return PhysicalDatasetRef(value)


def _publish_workflow_recipe(
    workflow: AgentWorkflowDraft,
    tmp_path: Path,
) -> QueryRecipe:
    repository = DataHubQueryRecipeAdapter.from_env_file(DATAHUB_WRITER)
    recipe = PrepareQueryRecipe(repository).execute(workflow)
    approval = RecipePublicationApproval(
        id=f"m26-acceptance-recipe-{uuid4().hex}",
        recipe_id=recipe.id,
        recipe_version=recipe.version,
        recipe_fingerprint=recipe.fingerprint,
        actor="sb_m26_acceptance_recipe_publisher",
        approved_at=NOW + timedelta(minutes=2),
        confirmation=RecipePublicationConfirmation.PUBLISH_VALIDATED_QUERY_RECIPE,
    )
    publish = PublishQueryRecipe(
        repository,
        SqlitePublicationAuditStore(tmp_path / "m26-recipe-audit.sqlite3"),
    )
    result = publish.execute(recipe, approval)
    for _ in range(4):
        if result.status is not RecipePublicationStatus.PARTIAL_FAILURE:
            break
        time.sleep(0.5)
        result = publish.execute(recipe, approval)
    assert result.status in {
        RecipePublicationStatus.CREATED,
        RecipePublicationStatus.ALREADY_CURRENT,
    }
    return recipe


def _await_recipe_inventory(
    inventory: DataHubQueryRecipeInventory,
    scope: SemanticRegistryScope,
    fingerprint: str,
) -> None:
    deadline = time.monotonic() + 20
    while True:
        documents = tuple(
            document for page in inventory.pages(scope, page_size=17) for document in page.items
        )
        if any(document.recipe.fingerprint == fingerprint for document in documents):
            return
        if time.monotonic() >= deadline:
            pytest.fail("published DataHub recipe was not visible to bounded inventory")
        time.sleep(0.5)


def _approve_report(
    inspector: InspectSemanticChange,
    store: PostgresSemanticChangeStore,
    report: SemanticChangeReport,
    *,
    action: SemanticChangeDecisionAction,
    confirmation: SemanticChangeConfirmation,
    minute: int,
) -> SemanticChangeCommit:
    proposal = PrepareSemanticChangeDecision(store).execute(
        report.context.scope,
        report.id,
        action=action,
    )
    approval = PrepareSemanticChangeDecisionApproval().execute(
        proposal,
        actor="sb_m26_acceptance_steward",
        approved_at=NOW + timedelta(minutes=minute),
        confirmation=confirmation,
    )
    return CommitSemanticChangeDecision(inspector, store).execute(proposal, approval)


def _require_plan(workflow: AgentWorkflowDraft) -> ResolvedSemanticPlan:
    assert workflow.resolved_plan is not None
    return workflow.resolved_plan


def _guarded_prepare(
    registry: GovernedSemanticRegistrySnapshot,
    scope: SemanticRegistryScope,
    runtime_dsn: str,
    compiler: _CountingCompiler,
    guard: _CountingGuard,
) -> PrepareGovernedRequest:
    memory = InMemoryGovernedSemanticRegistry(registry, scope)
    return PrepareGovernedRequest(
        planner=PlanSemanticRequest(memory, ResolutionLimits()),
        compiler=compiler,
        guard=guard,
        semantic_gate=AssertSemanticContextCurrent(PostgresSemanticChangeGateReader(runtime_dsn)),
        semantic_scope=scope,
    )


def _product_plan(
    registry: GovernedSemanticRegistrySnapshot,
    scope: SemanticRegistryScope,
    pointer: ActiveRegistryPointer,
) -> ResolvedSemanticPlan:
    request = BuildGuidedRequest(InMemoryGovernedSemanticRegistry(registry, scope)).execute(
        GuidedRequestInput(
            primary_entity="Product",
            dimensions=(GuidedDimensionInput(field="Product.category"),),
            metrics=(
                GuidedMetricInput(
                    operation="count_distinct",
                    field="Product.product_key",
                    alias="product_count",
                ),
            ),
            limit=50,
        )
    )
    return resolve_semantic_request(
        request,
        registry,
        ResolutionLimits(),
    ).model_copy(
        update={
            "activation_generation": pointer.generation,
            "active_pointer_fingerprint": registry_projection_fingerprint(pointer),
            "active_scope_fingerprint": semantic_registry_scope_fingerprint(scope),
        }
    )


def _north_star_plan(
    registry: GovernedSemanticRegistrySnapshot,
    scope: SemanticRegistryScope,
    pointer: ActiveRegistryPointer,
) -> ResolvedSemanticPlan:
    request = BuildGuidedRequest(InMemoryGovernedSemanticRegistry(registry, scope)).execute(
        build_demo_guided_input(GuidedRequestCase.NORTH_STAR)
    )
    return resolve_semantic_request(
        request,
        registry,
        ResolutionLimits(),
    ).model_copy(
        update={
            "activation_generation": pointer.generation,
            "active_pointer_fingerprint": registry_projection_fingerprint(pointer),
            "active_scope_fingerprint": semantic_registry_scope_fingerprint(scope),
        }
    )


def _corrected_registry(
    recorded: GovernedSemanticRegistrySnapshot,
    scope: SemanticRegistryScope,
    suffix: str,
) -> GovernedSemanticRegistrySnapshot:
    original = recorded.mapping_set.mappings[0]
    original_decision = _decision_id(original.approval_decision_id)
    corrected_decision = f"m26-corrected-mapping-{suffix}"
    corrected_mapping = GovernedFieldMapping(
        mapping=original.mapping.model_copy(update={"version": original.mapping.version + 1}),
        physical_type=original.physical_type,
        logical_field_version=original.logical_field_version,
        approval_decision_id=corrected_decision,
    )
    mappings = (corrected_mapping, *recorded.mapping_set.mappings[1:])
    provenance = tuple(
        RegistryArtifactProvenance(
            kind=item.kind,
            source=item.source,
            decision_ids=(
                tuple(
                    corrected_decision if decision == original_decision else decision
                    for decision in item.decision_ids
                )
                if item.kind is RegistryArtifactKind.PHYSICAL_MAPPINGS
                else item.decision_ids
            ),
        )
        for item in recorded.provenance
    )
    numbered = GovernedSemanticRegistrySnapshot.model_validate(
        {
            **recorded.model_dump(mode="python"),
            "version": 2,
            "mapping_set": GovernedMappingRegistry(
                version=2,
                mappings=mappings,
            ).model_dump(mode="python"),
            "provenance": tuple(item.model_dump(mode="python") for item in provenance),
        }
    )
    return prepare_datahub_registry_version(numbered, scope)


def _impact_filters() -> SemanticChangeImpactFilter:
    return SemanticChangeImpactFilter()


def _scan_request_counts(dsn: str, workspace_id: str) -> tuple[int, int]:
    with psycopg.connect(dsn) as connection:
        row = connection.execute(
            """
            SELECT count(*), count(DISTINCT source_kind)
            FROM schemabridge_control.semantic_change_scan_requests
            WHERE workspace_id = %s
            """,
            (workspace_id,),
        ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])
