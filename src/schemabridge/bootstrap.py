"""Dependency-composition root.

Concrete adapters will be wired here as milestones are implemented. Business
logic must not import this module.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from schemabridge.application.candidate_demo import build_customer_key_concept
from schemabridge.application.candidate_engine import (
    EvaluateSemanticCandidates,
    GenerateSemanticCandidates,
)
from schemabridge.application.canonical_review import (
    DecideCanonicalMapping,
    EditCanonicalReview,
    InspectCanonicalReview,
    PrepareCanonicalPublication,
    PublishCanonicalReview,
    ReadPublishedCanonicalContext,
    StartCanonicalReview,
)
from schemabridge.application.catalog_inspection import InspectCatalogAsset
from schemabridge.application.evaluation import RunReleaseEvaluation
from schemabridge.application.governed_execution import (
    ExecuteGovernedRequest,
    PlanSemanticRequest,
    PrepareGovernedRequest,
)
from schemabridge.application.guided_requests import (
    BuildGuidedRequest,
    LoadRequestDraft,
    SaveRequestDraft,
    SubmitGuidedRequest,
)
from schemabridge.application.intent_resolution import ResolveNaturalLanguageIntent
from schemabridge.application.join_discovery import (
    DecideJoinCandidate,
    DiscoverJoinCandidates,
    InspectJoinReview,
    LoadPublishedJoinContracts,
    PrepareJoinPublication,
    PublishJoinContracts,
    StartJoinReview,
)
from schemabridge.application.ports.candidates import CandidateEvidencePort
from schemabridge.application.ports.catalog import CatalogReadPort
from schemabridge.application.ports.evaluation import EvaluationReportWriterPort
from schemabridge.application.ports.intents import (
    IntentParserError,
    IntentParserErrorCode,
    IntentParserPort,
)
from schemabridge.application.ports.planning import SemanticPlanningContextPort
from schemabridge.application.ports.recipes import QueryRecipeRepositoryPort
from schemabridge.application.ports.relationships import (
    JoinContextReadPort,
    JoinContextWritePort,
    JoinReviewStorePort,
)
from schemabridge.application.ports.requests import RequestDraftStorePort
from schemabridge.application.ports.reviews import (
    CanonicalContextReadPort,
    CatalogWritePort,
    ReviewStorePort,
)
from schemabridge.application.ports.workflows import WorkflowPublicationPort
from schemabridge.application.postgres_health import (
    CheckDatabaseReadiness,
    DatabaseConfigurationError,
)
from schemabridge.application.query_execution import (
    PrepareQuery,
    PreviewQuery,
    SqlPolicyGuardPort,
)
from schemabridge.application.query_recipes import (
    AssessQueryRecipeReuse,
    PrepareQueryRecipe,
    PrepareStoredQueryRecipe,
    PublishQueryRecipe,
)
from schemabridge.application.ui_view_models import (
    JudgeUiViewFactory,
    UiMode,
    build_ui_reference_data,
)
from schemabridge.application.ui_workflow import JudgeUiService
from schemabridge.application.workflow_orchestration import AgentWorkflowOrchestrator
from schemabridge.config import Settings, get_settings
from schemabridge.domain.plans import QueryPolicy
from schemabridge.domain.resolution import ResolutionLimits

_DEMO_EVALUATION_DATABASE_URL = (
    "postgresql://schemabridge_reader:schemabridge_reader@127.0.0.1:55433/schemabridge"
)


@dataclass(frozen=True, slots=True)
class ApplicationContainer:
    """Current application dependencies.

    Add dependencies only when a use case needs them. Do not use this object as
    a hidden service locator inside domain or application modules.
    """

    settings: Settings


def build_container(settings: Settings | None = None) -> ApplicationContainer:
    """Create the application dependency graph."""

    return ApplicationContainer(settings=settings or get_settings())


def build_postgres_health_check(settings: Settings | None = None) -> CheckDatabaseReadiness:
    """Compose the PostgreSQL readiness use case only when requested."""

    resolved_settings = settings or get_settings()
    if not resolved_settings.database_url:
        raise DatabaseConfigurationError("DATABASE_URL is required for postgres-health")

    try:
        from schemabridge.adapters.postgres.health import PsycopgDatabaseHealthProbe
    except ModuleNotFoundError as error:
        if error.name == "psycopg":
            raise DatabaseConfigurationError(
                "PostgreSQL support is not installed; install schemabridge[postgres]"
            ) from error
        raise

    return CheckDatabaseReadiness(
        probe=PsycopgDatabaseHealthProbe(resolved_settings.database_url),
        expected_user=resolved_settings.postgres_reader_user,
        expected_statement_timeout_ms=resolved_settings.statement_timeout_ms,
    )


def build_query_preparer(policy: QueryPolicy) -> PrepareQuery:
    """Compose the SQL compiler and independent guard without a database."""

    try:
        from schemabridge.adapters.sql.compiler import PostgresQueryCompiler
    except ModuleNotFoundError as error:
        if error.name == "sqlglot":
            raise DatabaseConfigurationError(
                "SQL support is not installed; install schemabridge[sql]"
            ) from error
        raise

    return PrepareQuery(
        compiler=PostgresQueryCompiler(),
        guard=build_sql_guard(),
        policy=policy,
    )


def build_sql_guard() -> SqlPolicyGuardPort:
    """Compose the independent final-SQL policy guard."""

    try:
        from schemabridge.adapters.sql.guard import SqlGlotPolicyGuard
    except ModuleNotFoundError as error:
        if error.name == "sqlglot":
            raise DatabaseConfigurationError(
                "SQL support is not installed; install schemabridge[sql]"
            ) from error
        raise
    return SqlGlotPolicyGuard()


def build_query_previewer(
    policy: QueryPolicy,
    settings: Settings | None = None,
) -> PreviewQuery:
    """Compose guarded compilation and PostgreSQL preview execution."""

    resolved_settings = settings or get_settings()
    if not resolved_settings.database_url:
        raise DatabaseConfigurationError("DATABASE_URL is required for query preview")

    try:
        from schemabridge.adapters.postgres.preview import PsycopgQueryPreview
    except ModuleNotFoundError as error:
        if error.name == "psycopg":
            raise DatabaseConfigurationError(
                "PostgreSQL support is not installed; install schemabridge[postgres]"
            ) from error
        raise

    return PreviewQuery(
        prepare=build_query_preparer(policy),
        executor=PsycopgQueryPreview(resolved_settings.database_url),
    )


def build_catalog_reader(
    adapter_kind: Literal["live", "recorded"] = "live",
    *,
    repository_root: Path | None = None,
) -> CatalogReadPort:
    """Compose a live DataHub MCP reader or the explicit sanitized recording."""

    root = (repository_root or Path.cwd()).resolve()
    if adapter_kind == "recorded":
        from schemabridge.adapters.datahub.recorded import RecordedCatalogAdapter

        return RecordedCatalogAdapter(root / "demo/datahub/catalog_snapshot.json")

    try:
        from schemabridge.adapters.datahub.catalog import DataHubMcpCatalogAdapter
        from schemabridge.adapters.datahub.mcp_client import McpStdioToolClient
    except ModuleNotFoundError as error:
        if error.name == "mcp":
            raise DatabaseConfigurationError(
                "DataHub MCP support is not installed; install schemabridge[datahub]"
            ) from error
        raise

    return DataHubMcpCatalogAdapter(
        client=McpStdioToolClient(
            command="bash",
            arguments=(str(root / "scripts/datahub-mcp.sh"),),
            working_directory=root,
        )
    )


def build_catalog_inspector(
    adapter_kind: Literal["live", "recorded"] = "live",
    *,
    repository_root: Path | None = None,
) -> InspectCatalogAsset:
    """Compose the catalog inspection use case at the only composition root."""

    return InspectCatalogAsset(
        catalog=build_catalog_reader(adapter_kind, repository_root=repository_root)
    )


def build_candidate_generator(
    adapter_kind: Literal["live", "recorded"] = "live",
    *,
    repository_root: Path | None = None,
) -> GenerateSemanticCandidates:
    """Compose bounded catalog retrieval with explicitly sourced semantic evidence."""

    root = (repository_root or Path.cwd()).resolve()
    catalog = build_catalog_reader(adapter_kind, repository_root=root)
    if adapter_kind == "recorded":
        from schemabridge.adapters.matching.evidence import RecordedCandidateEvidenceAdapter

        evidence: CandidateEvidencePort = RecordedCandidateEvidenceAdapter(
            root / "demo/datahub/semantic_evidence_snapshot.json"
        )
        evidence_source = "recorded:synthetic-bounded-signals"
    else:
        from schemabridge.adapters.matching.evidence import EmptyCandidateEvidenceAdapter

        evidence = EmptyCandidateEvidenceAdapter()
        evidence_source = "live:missing-profile-overlap-signals"
    return GenerateSemanticCandidates(
        catalog=catalog,
        evidence=evidence,
        evidence_source=evidence_source,
    )


def build_candidate_evaluator(
    *,
    repository_root: Path | None = None,
) -> EvaluateSemanticCandidates:
    """Compose the plainly labeled synthetic evaluation fixture."""

    from schemabridge.adapters.matching.evaluation import YamlCandidateEvaluationAdapter

    root = (repository_root or Path.cwd()).resolve()
    return EvaluateSemanticCandidates(
        dataset=YamlCandidateEvaluationAdapter(root / "demo/ground_truth/semantic_mappings.yml")
    )


def build_evaluation_runner(
    *,
    include_live_llm: bool = False,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> RunReleaseEvaluation:
    """Compose the complete labeled evaluation without mixing live and deterministic runs."""

    from schemabridge.adapters.evaluation.ground_truth import (
        RecordedEvaluationGroundTruthAdapter,
        StaticEvaluationRecipeAdapter,
    )
    from schemabridge.adapters.evaluation.release import GitEvaluationReleaseIdentity
    from schemabridge.application.join_demo import build_north_star_join_proposals
    from schemabridge.application.query_demo import build_demo_query_policy

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    if not resolved.database_url:
        resolved = resolved.model_copy(update={"database_url": _DEMO_EVALUATION_DATABASE_URL})
    ground_truth = RecordedEvaluationGroundTruthAdapter(root)
    truth = ground_truth.load()
    prepare = build_governed_request_preparer(repository_root=root, settings=resolved)
    return RunReleaseEvaluation(
        ground_truth=ground_truth,
        release_identity=GitEvaluationReleaseIdentity(root),
        candidates=build_candidate_evaluator(repository_root=root),
        joins=build_join_discoverer(
            "recorded",
            repository_root=root,
            settings=resolved,
        ),
        join_proposals=build_north_star_join_proposals(),
        guided=build_guided_request_builder(repository_root=root),
        deterministic_intent=build_natural_language_intent_resolver(
            "fake",
            repository_root=root,
            settings=resolved,
        ),
        prepare=prepare,
        execute=build_governed_request_executor(
            repository_root=root,
            settings=resolved,
            prepare=prepare,
        ),
        guard=build_sql_guard(),
        safety_policy=build_demo_query_policy(),
        recipe_assessor=AssessQueryRecipeReuse(StaticEvaluationRecipeAdapter(truth.recipe)),
        live_intent=(
            build_natural_language_intent_resolver(
                "live",
                repository_root=root,
                settings=resolved,
            )
            if include_live_llm
            else None
        ),
    )


def build_evaluation_report_writer(
    *,
    repository_root: Path | None = None,
) -> EvaluationReportWriterPort:
    from schemabridge.adapters.evaluation.reporting import FileEvaluationReportWriter

    return FileEvaluationReportWriter((repository_root or Path.cwd()).resolve())


def build_review_store(settings: Settings | None = None) -> ReviewStorePort:
    """Compose the local SQLite draft and immutable-decision store."""

    from schemabridge.adapters.storage.reviews import SqliteReviewStore

    resolved = settings or get_settings()
    return SqliteReviewStore(resolved.draft_store_path.resolve())


def build_review_start(settings: Settings | None = None) -> StartCanonicalReview:
    return StartCanonicalReview(build_review_store(settings))


def build_review_inspector(settings: Settings | None = None) -> InspectCanonicalReview:
    return InspectCanonicalReview(build_review_store(settings))


def build_review_decider(settings: Settings | None = None) -> DecideCanonicalMapping:
    return DecideCanonicalMapping(build_review_store(settings))


def build_review_editor(settings: Settings | None = None) -> EditCanonicalReview:
    return EditCanonicalReview(build_review_store(settings))


def build_publication_preparer(settings: Settings | None = None) -> PrepareCanonicalPublication:
    return PrepareCanonicalPublication(build_review_store(settings))


def build_catalog_writer(
    adapter_kind: Literal["live", "fake"],
    *,
    repository_root: Path | None = None,
) -> CatalogWritePort:
    """Compose an explicitly selected catalog writer; live never falls back to fake."""

    if adapter_kind == "fake":
        from schemabridge.adapters.datahub.fake_writeback import FakeCatalogWriteAdapter

        return FakeCatalogWriteAdapter()
    from schemabridge.adapters.datahub.writeback import DataHubCatalogWriteAdapter

    root = (repository_root or Path.cwd()).resolve()
    return DataHubCatalogWriteAdapter.from_env_file(root / ".local/datahub/writer.env")


def build_review_publisher(
    adapter_kind: Literal["live", "fake"],
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> PublishCanonicalReview:
    return PublishCanonicalReview(
        store=build_review_store(settings),
        writer=build_catalog_writer(adapter_kind, repository_root=repository_root),
    )


def build_published_context_reader(
    adapter_kind: Literal["live", "fake"],
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> ReadPublishedCanonicalContext:
    writer = build_catalog_writer(adapter_kind, repository_root=repository_root)
    return ReadPublishedCanonicalContext(
        build_review_store(settings), cast(CanonicalContextReadPort, writer)
    )


def build_join_discoverer(
    catalog_kind: Literal["live", "recorded"] = "live",
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> DiscoverJoinCandidates:
    """Compose bounded catalog reads with the read-only PostgreSQL evidence adapter."""

    from schemabridge.adapters.postgres.relationships import (
        PsycopgRelationshipEvidenceAdapter,
    )
    from schemabridge.application.join_demo import build_north_star_join_proposals

    resolved = settings or get_settings()
    if not resolved.database_url:
        raise DatabaseConfigurationError("DATABASE_URL is required for join discovery")
    proposals = build_north_star_join_proposals()
    return DiscoverJoinCandidates(
        catalog=build_catalog_reader(catalog_kind, repository_root=repository_root),
        evidence=PsycopgRelationshipEvidenceAdapter(
            resolved.database_url,
            proposals,
            expected_user=resolved.postgres_reader_user,
            statement_timeout_ms=resolved.statement_timeout_ms,
        ),
    )


def build_join_review_store(settings: Settings | None = None) -> JoinReviewStorePort:
    from schemabridge.adapters.storage.relationships import SqliteJoinReviewStore

    resolved = settings or get_settings()
    return SqliteJoinReviewStore(resolved.draft_store_path.resolve())


def build_join_review_start(settings: Settings | None = None) -> StartJoinReview:
    return StartJoinReview(build_join_review_store(settings))


def build_join_review_inspector(settings: Settings | None = None) -> InspectJoinReview:
    return InspectJoinReview(build_join_review_store(settings))


def build_join_review_decider(settings: Settings | None = None) -> DecideJoinCandidate:
    return DecideJoinCandidate(build_join_review_store(settings))


def build_join_publication_preparer(settings: Settings | None = None) -> PrepareJoinPublication:
    return PrepareJoinPublication(build_join_review_store(settings))


def build_join_context_adapter(
    adapter_kind: Literal["live", "fake"],
    *,
    repository_root: Path | None = None,
) -> JoinContextWritePort:
    if adapter_kind == "fake":
        from schemabridge.adapters.datahub.fake_join_context import FakeJoinContextAdapter

        return FakeJoinContextAdapter()
    from schemabridge.adapters.datahub.join_context import DataHubJoinContextAdapter

    root = (repository_root or Path.cwd()).resolve()
    return DataHubJoinContextAdapter.from_env_file(root / ".local/datahub/writer.env")


def build_join_publisher(
    adapter_kind: Literal["live", "fake"],
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> PublishJoinContracts:
    return PublishJoinContracts(
        store=build_join_review_store(settings),
        writer=build_join_context_adapter(adapter_kind, repository_root=repository_root),
    )


def build_join_context_loader(
    adapter_kind: Literal["live", "fake"],
    *,
    repository_root: Path | None = None,
) -> LoadPublishedJoinContracts:
    adapter = build_join_context_adapter(adapter_kind, repository_root=repository_root)
    return LoadPublishedJoinContracts(cast(JoinContextReadPort, adapter))


def build_guided_request_builder(
    *,
    repository_root: Path | None = None,
) -> BuildGuidedRequest:
    """Compose the explicit synthetic approved context; no live fallback is implied."""

    from schemabridge.adapters.requests.recorded_context import RecordedRequestContextAdapter

    root = (repository_root or Path.cwd()).resolve()
    return BuildGuidedRequest(
        context=RecordedRequestContextAdapter(
            root / "demo/ground_truth/approved_logical_context.yml"
        )
    )


def build_natural_language_intent_resolver(
    adapter_kind: Literal["fake", "live"] = "fake",
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> ResolveNaturalLanguageIntent:
    """Compose an explicit fake or live parser; live never falls back without credentials."""

    from schemabridge.adapters.requests.recorded_context import RecordedRequestContextAdapter

    root = (repository_root or Path.cwd()).resolve()
    context = RecordedRequestContextAdapter(root / "demo/ground_truth/approved_logical_context.yml")
    parser: IntentParserPort
    if adapter_kind == "fake":
        from schemabridge.adapters.language.fake import FakeIntentParser

        parser = FakeIntentParser()
    else:
        from schemabridge.adapters.language.openai import OpenAIIntentParser

        resolved = settings or get_settings()
        if resolved.openai_api_key is None or resolved.llm_model is None:
            raise IntentParserError(
                IntentParserErrorCode.CONFIGURATION_MISSING,
                "OPENAI_API_KEY and SCHEMABRIDGE_LLM_MODEL are required for live intent parsing",
            )
        parser = OpenAIIntentParser.from_api_key(
            resolved.openai_api_key.get_secret_value(),
            resolved.llm_model,
        )
    return ResolveNaturalLanguageIntent(
        parser=parser,
        context=context,
        adapter_label=f"{adapter_kind}:typed-intent-only",
    )


def build_request_draft_store(settings: Settings | None = None) -> RequestDraftStorePort:
    from schemabridge.adapters.storage.request_drafts import SqliteRequestDraftStore

    resolved = settings or get_settings()
    return SqliteRequestDraftStore(resolved.draft_store_path.resolve())


def build_request_draft_saver(settings: Settings | None = None) -> SaveRequestDraft:
    return SaveRequestDraft(build_request_draft_store(settings))


def build_request_draft_loader(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> LoadRequestDraft:
    return LoadRequestDraft(
        store=build_request_draft_store(settings),
        builder=build_guided_request_builder(repository_root=repository_root),
    )


def build_guided_request_submitter() -> SubmitGuidedRequest:
    """Compose the M09 fake planner that cannot resolve physical assets or execute SQL."""

    from schemabridge.adapters.requests.fake_planner import FakeRequestPlanner

    return SubmitGuidedRequest(planner=FakeRequestPlanner())


def build_semantic_planning_context(
    *,
    repository_root: Path | None = None,
) -> SemanticPlanningContextPort:
    """Compose the visibly recorded, synthetic M10 planning context."""

    from schemabridge.adapters.planning.recorded import RecordedSemanticPlanningContext

    root = (repository_root or Path.cwd()).resolve()
    return RecordedSemanticPlanningContext(
        root / "demo/ground_truth/approved_logical_context.yml",
        root / "demo/ground_truth/planning_mappings.yml",
        root / "demo/ground_truth/join_contracts.yml",
    )


def build_governed_request_preparer(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> PrepareGovernedRequest:
    """Compose semantic resolution, deterministic compilation, and independent guarding."""

    from schemabridge.adapters.sql.compiler import PostgresQueryCompiler

    resolved = settings or get_settings()
    return PrepareGovernedRequest(
        planner=build_semantic_request_planner(
            repository_root=repository_root,
            settings=resolved,
        ),
        compiler=PostgresQueryCompiler(),
        guard=build_sql_guard(),
    )


def build_semantic_request_planner(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> PlanSemanticRequest:
    """Compose typed semantic resolution at the sole application composition root."""

    resolved = settings or get_settings()
    return PlanSemanticRequest(
        context=build_semantic_planning_context(repository_root=repository_root),
        limits=ResolutionLimits(
            max_tables=resolved.max_query_tables,
            max_preview_rows=resolved.max_query_rows,
            statement_timeout_ms=resolved.statement_timeout_ms,
        ),
    )


def build_governed_request_executor(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
    prepare: PrepareGovernedRequest | None = None,
) -> ExecuteGovernedRequest:
    """Compose bounded PostgreSQL preview and rejected-source reporting behind ports."""

    resolved = settings or get_settings()
    if not resolved.database_url:
        raise DatabaseConfigurationError("DATABASE_URL is required for governed preview")
    from schemabridge.adapters.postgres.preview import PsycopgQueryPreview
    from schemabridge.adapters.postgres.rejections import PsycopgRejectedSourceReporter

    return ExecuteGovernedRequest(
        prepare=prepare
        or build_governed_request_preparer(
            repository_root=repository_root,
            settings=resolved,
        ),
        executor=PsycopgQueryPreview(
            resolved.database_url,
            expected_user=resolved.postgres_reader_user,
            max_rows_limit=resolved.max_query_rows,
            max_timeout_ms=resolved.statement_timeout_ms,
        ),
        rejection_reporter=PsycopgRejectedSourceReporter(
            resolved.database_url,
            allowed_fields=frozenset(
                {
                    "crm.customers.customer_id",
                    "bank.account_holders.gf_customer_id",
                }
            ),
            expected_user=resolved.postgres_reader_user,
            max_records=resolved.max_query_rows,
        ),
    )


def build_agent_workflow_orchestrator(
    catalog_kind: Literal["live", "recorded"] = "recorded",
    *,
    publication_kind: Literal["live", "fake"] = "fake",
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> AgentWorkflowOrchestrator:
    """Compose durable orchestration with explicit catalog/publication/recipe adapters."""

    from schemabridge.adapters.storage.workflows import SqliteWorkflowDraftStore
    from schemabridge.adapters.workflows.fake import SqliteFakeWorkflowPublisher
    from schemabridge.adapters.workflows.system import SystemWorkflowClock

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    prepare = build_governed_request_preparer(repository_root=root, settings=resolved)
    publisher: WorkflowPublicationPort
    if publication_kind == "live":
        from schemabridge.adapters.datahub.workflow_publication import (
            DataHubWorkflowPublicationAdapter,
        )

        publisher = DataHubWorkflowPublicationAdapter.from_env_file(
            root / ".local/datahub/writer.env"
        )
    else:
        publisher = SqliteFakeWorkflowPublisher(resolved.draft_store_path.resolve())
    recipes = build_query_recipe_repository(
        publication_kind,
        repository_root=root,
        settings=resolved,
    )
    return AgentWorkflowOrchestrator(
        store=SqliteWorkflowDraftStore(resolved.draft_store_path.resolve()),
        clock=SystemWorkflowClock(),
        catalog=build_catalog_reader(catalog_kind, repository_root=root),
        intent=build_natural_language_intent_resolver(
            "fake",
            repository_root=root,
            settings=resolved,
        ),
        prepare=prepare,
        execute=build_governed_request_executor(
            repository_root=root,
            settings=resolved,
            prepare=prepare,
        ),
        publisher=publisher,
        recipe_assessor=AssessQueryRecipeReuse(recipes),
    )


def build_query_recipe_repository(
    adapter_kind: Literal["live", "fake"] = "fake",
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> QueryRecipeRepositoryPort:
    """Compose an explicit DataHub or visibly fake persistent recipe repository."""

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    if adapter_kind == "live":
        from schemabridge.adapters.datahub.query_recipes import DataHubQueryRecipeAdapter

        return DataHubQueryRecipeAdapter.from_env_file(root / ".local/datahub/writer.env")
    from schemabridge.adapters.recipes.sqlite import SqliteQueryRecipeRepository

    return SqliteQueryRecipeRepository(resolved.draft_store_path.resolve())


def build_query_recipe_preparer(
    adapter_kind: Literal["live", "fake"] = "fake",
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> PrepareStoredQueryRecipe:
    """Prepare a recipe only from one durably completed governed workflow."""

    from schemabridge.adapters.storage.workflows import SqliteWorkflowDraftStore

    resolved = settings or get_settings()
    repository = build_query_recipe_repository(
        adapter_kind,
        repository_root=repository_root,
        settings=resolved,
    )
    return PrepareStoredQueryRecipe(
        store=SqliteWorkflowDraftStore(resolved.draft_store_path.resolve()),
        prepare=PrepareQueryRecipe(repository),
    )


def build_query_recipe_publisher(
    adapter_kind: Literal["live", "fake"] = "fake",
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> PublishQueryRecipe:
    return PublishQueryRecipe(
        build_query_recipe_repository(
            adapter_kind,
            repository_root=repository_root,
            settings=settings,
        )
    )


def build_streamlit_ui_service(
    catalog_kind: Literal["live", "recorded"] = "recorded",
    *,
    publication_kind: Literal["live", "fake"] = "fake",
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> JudgeUiService:
    """Compose the judge UI without exposing concrete adapters to page callbacks."""

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    prepare = build_governed_request_preparer(repository_root=root, settings=resolved)
    candidate_report = build_candidate_generator("recorded", repository_root=root).execute(
        build_customer_key_concept()
    )
    context = build_semantic_planning_context(repository_root=root).load()
    modes = (
        UiMode(
            "Catalog",
            "Live DataHub" if catalog_kind == "live" else "Recorded catalog",
            catalog_kind,
            "No fallback is used if the selected catalog is unavailable.",
        ),
        UiMode(
            "Candidate evidence",
            "Recorded synthetic evidence",
            "recorded",
            "Bounded, sanitized fixture evidence; never presented as production evidence.",
        ),
        UiMode(
            "Intent",
            "Deterministic fake parser",
            "fake",
            "API-key-free typed interpretation for the judge demo.",
        ),
        UiMode(
            "Source",
            "Live read-only PostgreSQL",
            "live",
            "Execution is guarded, bounded, and uses schemabridge_reader.",
        ),
        UiMode(
            "Publication",
            "Live DataHub" if publication_kind == "live" else "Fake local publication",
            publication_kind,
            "Writes still require the exact typed publication approval.",
        ),
    )
    views = JudgeUiViewFactory(
        reference=build_ui_reference_data(candidate_report, context),
        prepare=prepare,
        modes=modes,
    )
    return JudgeUiService(
        orchestrator_factory=lambda: build_agent_workflow_orchestrator(
            catalog_kind,
            publication_kind=publication_kind,
            repository_root=root,
            settings=resolved,
        ),
        views=views,
    )
