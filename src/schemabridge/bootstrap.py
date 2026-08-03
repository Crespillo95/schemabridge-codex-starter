"""Dependency-composition root.

Concrete adapters will be wired here as milestones are implemented. Business
logic must not import this module.
"""

import hashlib
import hmac
import importlib.util
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import unquote, urlsplit

from schemabridge.adapters.control_plane.migration_paths import (
    resolve_control_plane_migrations_path,
)
from schemabridge.application.api_workflows import (
    CancelExecutionJob,
    InspectExecutionJob,
    SubmitExecutionJob,
)
from schemabridge.application.authentication import AuthenticationBoundaryError
from schemabridge.application.authorization import DenyByDefaultAuthorizationPolicy
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
from schemabridge.application.connectors import ExactExecutionTargetResolver
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
from schemabridge.application.identity_rotation import (
    ApproveIdentityRotation,
    CompleteReservedIdentityRotation,
    InitializeVerifiedIdentityState,
    PrepareIdentityRotation,
    ResolveReservedIdentityRotation,
)
from schemabridge.application.intent_resolution import ResolveNaturalLanguageIntent
from schemabridge.application.job_worker import (
    RunOneJobWorker,
    WorkerExecutionRouteContext,
    WorkerOrchestratorFactoryPort,
)
from schemabridge.application.join_discovery import (
    DecideJoinCandidate,
    DiscoverJoinCandidates,
    InspectJoinReview,
    LoadPublishedJoinContracts,
    PrepareJoinPublication,
    PublishJoinContracts,
    StartJoinReview,
)
from schemabridge.application.legacy_import import (
    ApplyLegacyControlPlaneImport,
    InspectLegacyControlPlaneImport,
    PrepareLegacyControlPlaneImport,
)
from schemabridge.application.ports.authentication import BearerAuthenticationPort
from schemabridge.application.ports.background_jobs import (
    BackgroundJobApiStorePort,
    BackgroundJobStorePort,
)
from schemabridge.application.ports.browser_auth import (
    BrowserAuthConfigurationError,
    BrowserAuthConfigurationValidatorPort,
    BrowserOidcRequirements,
    ValidatedBrowserAuthConfiguration,
)
from schemabridge.application.ports.candidates import CandidateEvidencePort
from schemabridge.application.ports.catalog import CatalogReadPort
from schemabridge.application.ports.connectors import ExecutionTargetResolverPort
from schemabridge.application.ports.control_plane_migrations import (
    ControlPlaneMigrationInspection,
    ControlPlaneMigrationPort,
)
from schemabridge.application.ports.control_plane_operations import (
    ControlPlaneBackupPort,
    ControlPlaneRestorePort,
)
from schemabridge.application.ports.evaluation import EvaluationReportWriterPort
from schemabridge.application.ports.intents import (
    IntentParserError,
    IntentParserErrorCode,
    IntentParserPort,
)
from schemabridge.application.ports.planning import GovernedSemanticRegistryPort
from schemabridge.application.ports.production_evidence import M30ReadinessReportWriterPort
from schemabridge.application.ports.publication_audit import (
    PublicationAuditStoreError,
    PublicationAuditStorePort,
)
from schemabridge.application.ports.recipes import QueryRecipeRepositoryPort
from schemabridge.application.ports.registry_control import (
    ActiveRegistryPointerReadPort,
    RegistryControlStorePort,
    RegistryProjectionPort,
    RegistryVersionReadPort,
)
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
from schemabridge.application.ports.runtime_logging import (
    RuntimeLoggingService,
    RuntimeLoggingSessionPort,
)
from schemabridge.application.ports.workflow_access import WorkflowAccessStorePort
from schemabridge.application.ports.workflows import (
    WorkflowDraftStorePort,
    WorkflowPublicationPort,
)
from schemabridge.application.postgres_health import (
    CheckDatabaseReadiness,
    DatabaseConfigurationError,
)
from schemabridge.application.production_readiness import AssessM30Readiness
from schemabridge.application.query_cost import AssessGovernedQueryCost
from schemabridge.application.query_execution import (
    PrepareQuery,
    PreviewQuery,
    SqlPolicyGuardPort,
)
from schemabridge.application.query_recipes import (
    AssessQueryRecipeReuse,
    PrepareQueryRecipe,
    PrepareStoredQueryRecipe,
    PrepareStoredStaleQueryRecipeMigration,
    PublishQueryRecipe,
    PublishStaleQueryRecipeMigration,
)
from schemabridge.application.registry_control import (
    CommitRegistryActivation,
    InspectRegistryReconciliation,
    PrepareRegistryActivation,
    PrepareRegistryRollback,
    ReconcileRegistryProjection,
)
from schemabridge.application.semantic_change import AssertSemanticContextCurrent
from schemabridge.application.semantic_registry import (
    PrepareGovernedSemanticRegistryPublicationApproval,
    PublishGovernedSemanticRegistryVersion,
)
from schemabridge.application.ui_view_models import (
    JudgeUiViewFactory,
    UiMode,
    UiRegistryProjectionStatus,
    build_ui_reference_data,
)
from schemabridge.application.ui_workflow import (
    DeniedJudgeUiViewFactory,
    JudgeUiService,
)
from schemabridge.application.workflow_orchestration import AgentWorkflowOrchestrator
from schemabridge.config import CONTROL_PLANE_SCHEMA, Settings, get_settings
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
    WorkflowPermission,
)
from schemabridge.domain.plans import QueryPolicy
from schemabridge.domain.query_studio_matching import (
    GOVERNED_DESCRIPTION_MATCHER_VERSION,
)
from schemabridge.domain.registry_control import registry_projection_fingerprint
from schemabridge.domain.resolution import ResolutionLimits
from schemabridge.domain.semantic_registry import (
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
)

QueryStudioRuntimeSettings = Settings

if TYPE_CHECKING:
    from fastapi import FastAPI

    from schemabridge.adapters.catalog.datahub_secrets import (
        DataHubCatalogSecretResolverPort,
    )
    from schemabridge.adapters.connectors.remote_secrets import (
        ConnectorSecretCapability,
        VaultKvV2ConnectorSecretResolver,
    )
    from schemabridge.adapters.control_plane.identity_evidence import (
        IdentityEvidenceFileReader,
    )
    from schemabridge.adapters.control_plane.postgres_identity_bindings import (
        PostgresIdentityBindingResolver,
    )
    from schemabridge.adapters.control_plane.postgres_identity_rotation import (
        PostgresIdentityRotationStore,
    )
    from schemabridge.adapters.control_plane.postgres_pool import PostgresControlPool
    from schemabridge.adapters.observability.http_export import MetricsHttpExporter
    from schemabridge.adapters.semantic_registry.remote_secrets import (
        DataHubRegistryCredentialResolver,
        DataHubRegistryWriterCredentialResolver,
    )
    from schemabridge.adapters.storage.postgres import (
        ControlConnectionProvider,
        PostgresWorkflowAccessStore,
        PostgresWorkflowDraftStore,
    )
    from schemabridge.application.catalog_indexer import RunOneCatalogRefresh
    from schemabridge.application.catalog_inventory import ApplyTenantCapacityPolicy
    from schemabridge.application.connector_route_operator import ConnectorRouteOperator
    from schemabridge.application.database_separation import (
        VerifySourceControlDatabaseSeparation,
    )
    from schemabridge.application.natural_sql import (
        ConfirmNaturalSqlPreview,
        GenerateGovernedCopyableSql,
        PrepareNaturalSqlPreview,
    )
    from schemabridge.application.ports.advanced_query_studio import (
        AdvancedInterpretationPort,
        AdvancedMentionExtractionPort,
        AdvancedSemanticRetrievalPort,
    )
    from schemabridge.application.ports.connector_secrets import (
        ConnectorSecretResolver,
    )
    from schemabridge.application.ports.operational_telemetry import (
        OperationalTelemetryPort,
    )
    from schemabridge.application.ports.query_studio import (
        DescriptionExpansionPort,
        PhysicalFieldDiscoveryPort,
    )
    from schemabridge.application.ports.semantic_dependency_sources import (
        QueryRecipeDependencySourcePort,
    )
    from schemabridge.application.query_studio import (
        BrowseGuidedGovernedFields,
        ConfirmGuidedQueryStudioPreview,
        ConfirmQueryStudioPreview,
        DiscoverPhysicalFields,
        PrepareGuidedSelectionQueryStudioPreview,
        PrepareNaturalLanguageQueryStudioPreview,
        RecomputeNaturalLanguageQueryStudioPreview,
        SearchGovernedFields,
    )
    from schemabridge.application.query_studio_ai_policy import TenantAiPolicyOperator
    from schemabridge.application.registry_publication_worker import (
        RunOneRegistryPublisherWorker,
    )
    from schemabridge.application.semantic_change import InspectSemanticChange
    from schemabridge.application.semantic_change_reconciler import (
        RunOneSemanticChangeScan,
    )
    from schemabridge.application.semantic_dependency_reconciler import (
        ReconcileSemanticDependencies,
        SemanticDependencyReconciliationResult,
    )
    from schemabridge.application.semantic_profile_worker import RunOneSemanticJoinProfile
    from schemabridge.domain.query_studio import (
        PhysicalDiscoveryCardinality,
        ProviderConfigurationFacts,
    )
    from schemabridge.domain.semantic_change_scans import SemanticChangeScanRequest
    from schemabridge.entrypoints.catalog.main import CatalogProcessRuntime
    from schemabridge.entrypoints.http.app import ApiHttpServices
    from schemabridge.entrypoints.semantic_change.main import SemanticChangeOperatorRuntime
    from schemabridge.entrypoints.semantic_profile_worker.main import (
        SemanticProfileWorkerProcessRuntime,
    )
    from schemabridge.entrypoints.semantic_reconciler.main import (
        SemanticReconcilerProcessRuntime,
    )

_DEMO_EVALUATION_DATABASE_URL = (
    "postgresql://schemabridge_reader:schemabridge_reader@127.0.0.1:55433/schemabridge"
)
_MANAGED_STREAMLIT_SECRETS_ROOT = Path("/opt/schemabridge/.streamlit")
ControlPlaneCredential = Literal[
    "runtime",
    "reconciler",
    "migrator",
    "api",
    "worker",
    "publisher",
    "catalog",
    "observer",
    "backup",
]


@dataclass(frozen=True, slots=True)
class ApplicationContainer:
    """Current application dependencies.

    Add dependencies only when a use case needs them. Do not use this object as
    a hidden service locator inside domain or application modules.
    """

    settings: Settings


@dataclass(frozen=True, slots=True)
class StreamlitRuntimeOptions:
    """Non-secret entrypoint defaults resolved only at the composition root."""

    profile: Literal["development", "hosted-demo", "staging", "production"]
    auth_mode: Literal["local-demo", "oidc"]
    catalog_kind: Literal["live", "recorded"]
    registry_kind: Literal["live", "recorded"]
    publication_kind: Literal["live", "fake", "disabled"]
    execution_kind: Literal["live", "recorded", "disabled"]
    oidc_provider: str | None
    oidc_audience: str | None
    oidc_issuer: str | None
    auth_configuration_validator: BrowserAuthConfigurationValidatorPort | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    oidc_requirements: BrowserOidcRequirements | None = field(
        default=None,
        repr=False,
    )

    def require_auth_configuration(
        self,
        secrets: Mapping[str, object],
    ) -> ValidatedBrowserAuthConfiguration:
        """Validate private OIDC material through the composed adapter only."""

        if (
            self.auth_mode != "oidc"
            or self.auth_configuration_validator is None
            or self.oidc_requirements is None
        ):
            raise BrowserAuthConfigurationError("OIDC preflight requires OIDC mode")
        return self.auth_configuration_validator.validate(
            secrets,
            self.oidc_requirements,
        )


@dataclass(frozen=True, slots=True)
class WebProcessRuntime:
    """Fail-closed web process preflight plus the fixed Streamlit invocation."""

    readiness_check: Callable[[], None] = field(repr=False)
    listener_readiness_check: Callable[[], None] = field(repr=False)
    streamlit_argv: tuple[str, ...]

    def require_ready(self) -> None:
        """Repeat every managed web readiness dependency check."""

        self.readiness_check()

    def require_probe_ready(self) -> None:
        """Repeat preflight and then require the fixed local Streamlit listener."""

        self.require_ready()
        self.listener_readiness_check()


@dataclass(frozen=True, slots=True)
class QueryStudioRuntimeServices:
    """Server-side Query Studio use cases exposed to the Streamlit entrypoint."""

    scope: SemanticRegistryScope
    ai_mode: Literal["disabled", "fake", "live"]
    configuration: "ProviderConfigurationFacts" = field(repr=False)
    search: "SearchGovernedFields" = field(repr=False)
    browse_guided: "BrowseGuidedGovernedFields" = field(repr=False)
    prepare_guided: "PrepareGuidedSelectionQueryStudioPreview" = field(repr=False)
    confirm_guided: "ConfirmGuidedQueryStudioPreview" = field(repr=False)
    catalog_cardinality: "PhysicalDiscoveryCardinality"
    governed_mapping_count: int
    prepare_natural: "PrepareNaturalLanguageQueryStudioPreview | None" = field(
        default=None,
        repr=False,
    )
    confirm_natural: "ConfirmQueryStudioPreview | None" = field(
        default=None,
        repr=False,
    )
    recompute_natural: "RecomputeNaturalLanguageQueryStudioPreview | None" = field(
        default=None,
        repr=False,
    )
    discover_physical: "DiscoverPhysicalFields | None" = field(default=None, repr=False)
    expansion: "DescriptionExpansionPort | None" = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class NaturalSqlRuntimeServices:
    """Copy-only natural SQL services; execution is deliberately absent."""

    scope: SemanticRegistryScope
    ai_mode: Literal["fake", "live"]
    prepare: "PrepareNaturalSqlPreview" = field(repr=False)
    confirm: "ConfirmNaturalSqlPreview" = field(repr=False)
    generate: "GenerateGovernedCopyableSql" = field(repr=False)


@dataclass(frozen=True, slots=True)
class ApiProcessRuntime:
    """Fully composed API process with only non-secret server settings exposed."""

    application: "FastAPI" = field(repr=False)
    log_level: str
    bind_host: str
    port: int
    graceful_shutdown_seconds: int


@dataclass(frozen=True, slots=True)
class ObserverProcessRuntime:
    """Fully composed observer with no source, DataHub, OIDC, or LLM capability."""

    application: "FastAPI" = field(repr=False)
    log_level: str
    bind_host: str
    port: int
    limit_concurrency: int
    graceful_shutdown_seconds: int


@dataclass(frozen=True, slots=True)
class WorkerProcessRuntime:
    """Fully composed worker process or a completed readiness-only preflight."""

    worker: RunOneJobWorker | None = field(repr=False)
    log_level: str
    poll_interval_seconds: float
    control_pool: "PostgresControlPool | None" = field(default=None, repr=False)
    telemetry: "OperationalTelemetryPort | None" = field(default=None, repr=False)
    metrics_exporter: "MetricsHttpExporter | None" = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class RegistryPublisherProcessRuntime:
    """Isolated publisher process or a completed readiness-only preflight."""

    publisher: "RunOneRegistryPublisherWorker | None" = field(repr=False)
    log_level: str
    poll_interval_seconds: float
    control_pool: "PostgresControlPool | None" = field(default=None, repr=False)
    telemetry: "OperationalTelemetryPort | None" = field(default=None, repr=False)
    metrics_exporter: "MetricsHttpExporter | None" = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class LegacyControlPlaneImportServices:
    """Explicit inspect, dry-run, and apply operations over one offline source."""

    inspect: InspectLegacyControlPlaneImport
    prepare: PrepareLegacyControlPlaneImport
    apply: ApplyLegacyControlPlaneImport


@dataclass(frozen=True, slots=True)
class IdentityRotationServices:
    """Approval-gated identity operations sharing one bounded durable store."""

    initialize: InitializeVerifiedIdentityState
    prepare: PrepareIdentityRotation
    approve: ApproveIdentityRotation
    resolve: ResolveReservedIdentityRotation
    complete: CompleteReservedIdentityRotation


@dataclass(frozen=True, slots=True)
class _ControlPlaneReadiness:
    """Expose only a read-only current-schema check to the HTTP boundary."""

    check: Callable[[], object]

    def require_ready(self) -> None:
        self.check()


def _build_operational_telemetry(
    settings: Settings,
    *,
    service: str,
) -> "OperationalTelemetryPort":
    """Compose one process-local, source-independent telemetry sink."""

    from schemabridge.adapters.observability.runtime import RuntimeOperationalTelemetry

    return RuntimeOperationalTelemetry(
        service=service,
        environment=settings.environment,
    )


def _build_process_metrics_exporter(
    settings: Settings,
    *,
    telemetry: "OperationalTelemetryPort",
) -> "MetricsHttpExporter":
    """Bind one bounded internal exporter to the process-local registry."""

    from schemabridge.adapters.observability.http_export import MetricsHttpExporter

    return MetricsHttpExporter(
        bind_host=settings.process_metrics_bind_host,
        port=settings.process_metrics_port,
        renderer=telemetry,
        max_response_bytes=settings.process_metrics_max_response_bytes,
    )


def configure_runtime_logging(
    *,
    service: RuntimeLoggingService,
) -> RuntimeLoggingSessionPort:
    """Compose one process-owned structured logging session before runtime startup."""

    from schemabridge.adapters.observability.structured_logging import (
        configure_structured_logging,
    )

    return configure_structured_logging(service=service)


def ensure_runtime_logging(
    *,
    service: RuntimeLoggingService,
) -> RuntimeLoggingSessionPort:
    """Compose or reuse the structured session required by a rerun-based entrypoint."""

    from schemabridge.adapters.observability.structured_logging import (
        ensure_structured_logging,
    )

    return ensure_structured_logging(service=service)


def build_container(settings: Settings | None = None) -> ApplicationContainer:
    """Create the application dependency graph."""

    return ApplicationContainer(settings=settings or get_settings())


def resolve_runtime_profile(
    settings: Settings | None = None,
) -> Literal["development", "hosted-demo", "staging", "production"]:
    """Expose only the profile an entrypoint needs, keeping configuration at the root."""

    return (settings or get_settings()).runtime_profile


def build_streamlit_runtime_options(
    settings: Settings | None = None,
) -> StreamlitRuntimeOptions:
    resolved = settings or get_settings()
    _reject_operator_credentials_in_managed_web(resolved)
    if resolved.auth_mode == "oidc" and importlib.util.find_spec("authlib") is None:
        raise RuntimeError("OIDC authentication support is unavailable; install schemabridge[ui]")
    auth_configuration_validator: BrowserAuthConfigurationValidatorPort | None = None
    oidc_requirements: BrowserOidcRequirements | None = None
    if resolved.auth_mode == "oidc":
        from schemabridge.adapters.identity.streamlit_auth import (
            StreamlitAuthConfigurationValidator,
        )

        if (
            resolved.oidc_provider is None
            or resolved.oidc_audience is None
            or resolved.oidc_issuer is None
        ):
            raise RuntimeError("OIDC runtime metadata is incomplete")
        auth_configuration_validator = StreamlitAuthConfigurationValidator()
        oidc_requirements = BrowserOidcRequirements(
            profile=resolved.runtime_profile,
            provider=resolved.oidc_provider,
            audience=resolved.oidc_audience,
            issuer=resolved.oidc_issuer,
        )
    return StreamlitRuntimeOptions(
        profile=resolved.runtime_profile,
        auth_mode=resolved.auth_mode,
        catalog_kind=resolved.catalog_mode,
        registry_kind=resolved.registry_mode,
        publication_kind=resolved.publication_mode,
        execution_kind=resolved.execution_mode,
        oidc_provider=resolved.oidc_provider,
        oidc_audience=resolved.oidc_audience,
        oidc_issuer=resolved.oidc_issuer,
        auth_configuration_validator=auth_configuration_validator,
        oidc_requirements=oidc_requirements,
    )


def build_web_process_runtime(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> WebProcessRuntime:
    """Compose a web process that refuses managed startup until auth and schema are ready."""

    resolved = settings or get_settings()
    if resolved.runtime_component != "web":
        raise RuntimeError("the web entrypoint requires the web runtime component")
    runtime = build_streamlit_runtime_options(resolved)
    root = (repository_root or Path.cwd()).resolve()
    from schemabridge.adapters.web.streamlit_health import StreamlitLoopbackHealth

    loopback_health = StreamlitLoopbackHealth()

    def local_readiness() -> None:
        return None

    readiness_check: Callable[[], None] = local_readiness
    if runtime.profile in {"staging", "production"}:
        from schemabridge.adapters.connectors.remote_secrets import (
            ProjectedServiceAccountIdentity,
        )
        from schemabridge.adapters.identity.streamlit_secrets import (
            ProjectedStreamlitSecrets,
        )

        token_file = resolved.workload_identity_token_file
        identity_root = resolved.workload_identity_root
        audience = resolved.workload_identity_audience
        if (
            runtime.auth_mode != "oidc"
            or token_file is None
            or identity_root is None
            or audience is None
        ):
            raise DatabaseConfigurationError("managed web readiness configuration is incomplete")
        auth_reader = ProjectedStreamlitSecrets(
            secrets_file=_MANAGED_STREAMLIT_SECRETS_ROOT / "secrets.toml",
            mount_root=_MANAGED_STREAMLIT_SECRETS_ROOT,
        )
        workload_identity = ProjectedServiceAccountIdentity(
            token_file=token_file,
            mount_root=identity_root,
            audience=audience,
        )

        def managed_readiness() -> None:
            raw_secrets = auth_reader.read()
            runtime.require_auth_configuration(raw_secrets)
            workload_identity.read()
            require_current_control_plane_schema(
                credential_kind="runtime",
                repository_root=root,
                settings=resolved,
            )

        readiness_check = managed_readiness

    return WebProcessRuntime(
        readiness_check=readiness_check,
        listener_readiness_check=loopback_health.require_ready,
        streamlit_argv=(
            "streamlit",
            "run",
            "streamlit_app.py",
            "--server.address=0.0.0.0",
            "--server.port=7860",
            "--server.headless=true",
            "--server.fileWatcherType=none",
            "--browser.gatherUsageStats=false",
        ),
    )


def build_streamlit_principal(
    *,
    claims: Mapping[str, object] | None = None,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> AuthenticatedPrincipal:
    """Resolve one local-demo or authenticated OIDC principal at the composition root."""

    from schemabridge.adapters.identity.local import LocalDemoPrincipalFactory
    from schemabridge.adapters.identity.oidc import OidcClaimError, OidcPrincipalMapper
    from schemabridge.adapters.workflows.system import SystemWorkflowClock

    resolved = settings or get_settings()
    current = now or SystemWorkflowClock().now()
    if resolved.auth_mode == "local-demo":
        if claims is not None:
            raise ValueError("local demo identity does not accept browser claims")
        return LocalDemoPrincipalFactory(
            workspace=resolved.local_workspace,
            subject=resolved.local_subject,
            roles=frozenset(IdentityRole(role) for role in resolved.local_roles),
        ).create(now=current)
    if claims is None:
        raise ValueError("OIDC identity requires authenticated Streamlit claims")
    assert resolved.oidc_issuer is not None
    assert resolved.oidc_audience is not None
    assert resolved.pseudonymization_key is not None
    allowed_groups = {
        group: frozenset(IdentityRole(role) for role in roles)
        for group, roles in resolved.oidc_allowed_groups.items()
    }
    try:
        return OidcPrincipalMapper(
            expected_issuer=resolved.oidc_issuer,
            expected_audience=resolved.oidc_audience,
            allowed_group_roles=allowed_groups,
            allowed_tenants=frozenset(resolved.oidc_allowed_tenants),
            pseudonymization_key=resolved.pseudonymization_key.get_secret_value().encode(),
            pseudonymization_key_version=resolved.pseudonymization_key_version,
            workspace_claim=resolved.oidc_tenant_claim,
            groups_claim=resolved.oidc_role_claim,
            max_session_age=timedelta(seconds=resolved.oidc_max_session_age_seconds),
        ).map_claims(claims, now=current)
    except OidcClaimError as error:
        raise AuthenticationBoundaryError(error.code.value) from error


def build_bearer_authenticator(
    settings: Settings | None = None,
) -> BearerAuthenticationPort:
    """Compose either the development token or signed OIDC bearer boundary."""

    resolved = settings or get_settings()
    if resolved.auth_mode == "local-demo":
        from schemabridge.adapters.identity.local import LocalDemoPrincipalFactory
        from schemabridge.adapters.identity.local_bearer import LocalBearerAuthenticator

        if resolved.api_local_bearer_token is None:
            raise DatabaseConfigurationError(
                "SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN is required for the local API"
            )
        return LocalBearerAuthenticator(
            configured_token=resolved.api_local_bearer_token,
            principal_factory=LocalDemoPrincipalFactory(
                workspace=resolved.local_workspace,
                subject=resolved.local_subject,
                roles=frozenset(IdentityRole(role) for role in resolved.local_roles),
            ),
            runtime_profile=resolved.runtime_profile,
        )

    from schemabridge.adapters.identity.oidc import OidcPrincipalMapper
    from schemabridge.adapters.identity.oidc_bearer import OidcBearerAuthenticator

    if resolved.api_oidc_jwks_url is None:
        raise DatabaseConfigurationError(
            "SCHEMABRIDGE_API_OIDC_JWKS_URL is required for the OIDC API"
        )
    assert resolved.oidc_issuer is not None
    assert resolved.oidc_audience is not None
    assert resolved.pseudonymization_key is not None
    mapper = OidcPrincipalMapper(
        expected_issuer=resolved.oidc_issuer,
        expected_audience=resolved.oidc_audience,
        allowed_group_roles={
            group: frozenset(IdentityRole(role) for role in roles)
            for group, roles in resolved.oidc_allowed_groups.items()
        },
        allowed_tenants=frozenset(resolved.oidc_allowed_tenants),
        pseudonymization_key=resolved.pseudonymization_key.get_secret_value().encode(),
        pseudonymization_key_version=resolved.pseudonymization_key_version,
        workspace_claim=resolved.oidc_tenant_claim,
        groups_claim=resolved.oidc_role_claim,
        max_session_age=timedelta(seconds=resolved.oidc_max_session_age_seconds),
    )
    issuer = urlsplit(resolved.oidc_issuer)
    allow_insecure_loopback = (
        resolved.runtime_profile == "development"
        and issuer.scheme == "http"
        and issuer.hostname in {"127.0.0.1", "::1", "localhost"}
    )
    return OidcBearerAuthenticator(
        mapper=mapper,
        jwks_url=resolved.api_oidc_jwks_url,
        algorithms=resolved.api_oidc_algorithms,
        expected_authorized_party=resolved.oidc_audience,
        allow_insecure_loopback=allow_insecure_loopback,
    )


def resolve_control_operator_actor(
    supplied_actor: str | None,
    *,
    required_role: str,
    settings: Settings | None = None,
) -> str:
    """Resolve an auditable operator without trusting managed CLI identity input."""

    resolved = settings or get_settings()
    if resolved.runtime_profile in {"staging", "production"}:
        if supplied_actor is not None:
            raise DatabaseConfigurationError(
                "managed control-plane actor identity cannot be supplied on the command line"
            )
        actor = resolved.control_operator_actor_id
        roles = set(resolved.control_operator_roles)
        if actor is None:
            raise DatabaseConfigurationError(
                "managed control-plane commands require a trusted configured operator identity"
            )
    else:
        principal = build_streamlit_principal(settings=resolved)
        actor = principal.actor_id
        roles = {role.value for role in principal.roles}
        if supplied_actor is not None and supplied_actor != actor:
            raise DatabaseConfigurationError(
                "local control-plane actor must match the authenticated local principal"
            )
    if required_role not in roles and IdentityRole.PLATFORM_ADMIN.value not in roles:
        raise DatabaseConfigurationError(
            "control-plane operator lacks the required configured role"
        )
    return actor


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


def build_source_control_database_separation(
    settings: Settings | None = None,
) -> "VerifySourceControlDatabaseSeparation":
    """Compose a server-observed, read-only source/control separation proof."""

    from schemabridge.adapters.postgres.database_identity import (
        PsycopgDatabaseIdentityProbe,
    )
    from schemabridge.application.database_separation import (
        VerifySourceControlDatabaseSeparation,
    )

    resolved = settings or get_settings()
    if resolved.database_url is None:
        raise DatabaseConfigurationError(
            "DATABASE_URL is required to verify source/control database separation"
        )
    control_dsn = _control_plane_dsn(resolved, "runtime")
    control_user = unquote(urlsplit(control_dsn).username or "")
    if not control_user:
        raise DatabaseConfigurationError("control runtime database user is invalid")
    return VerifySourceControlDatabaseSeparation(
        source=PsycopgDatabaseIdentityProbe(
            resolved.database_url,
            expected_user=resolved.postgres_reader_user,
            statement_timeout_ms=resolved.statement_timeout_ms,
        ),
        control=PsycopgDatabaseIdentityProbe(
            control_dsn,
            expected_user=control_user,
            statement_timeout_ms=resolved.statement_timeout_ms,
        ),
    )


def build_control_plane_migrator(
    *,
    credential_kind: ControlPlaneCredential = "migrator",
    repository_root: Path | None = None,
    settings: Settings | None = None,
    connection_provider: "ControlConnectionProvider | None" = None,
) -> ControlPlaneMigrationPort:
    """Compose explicit control-plane migration access with one selected credential.

    Runtime composition calls only ``require_current`` through the runtime credential.
    DDL remains available solely when an operator explicitly selects the migrator.
    """

    from schemabridge.adapters.control_plane.postgres_migrations import (
        PostgresControlPlaneMigrator,
    )

    resolved = settings or get_settings()
    _require_supported_control_plane_schema(resolved)
    root = (repository_root or Path.cwd()).resolve()
    migrator = PostgresControlPlaneMigrator(
        dsn=_control_plane_dsn(resolved, credential_kind),
        migrations_path=resolve_control_plane_migrations_path(root),
        application_name=f"schemabridge-control-{credential_kind}",
        connection_factory=(
            None
            if connection_provider is None
            else lambda _dsn, _timeout: connection_provider.connection()
        ),
    )
    known = migrator.known_migrations()
    if known[-1].version != resolved.control_plane_schema_version:
        raise DatabaseConfigurationError(
            "configured control-plane schema version does not match this release"
        )
    return migrator


def require_current_control_plane_schema(
    *,
    credential_kind: Literal[
        "runtime",
        "reconciler",
        "api",
        "worker",
        "publisher",
        "catalog",
        "observer",
        "backup",
    ] = "runtime",
    repository_root: Path | None = None,
    settings: Settings | None = None,
    connection_provider: "ControlConnectionProvider | None" = None,
) -> ControlPlaneMigrationInspection:
    """Verify exact schema history without applying migrations or other DDL."""

    return build_control_plane_migrator(
        credential_kind=credential_kind,
        repository_root=repository_root,
        settings=settings,
        connection_provider=connection_provider,
    ).require_current()


def build_control_plane_pool(
    *,
    credential_kind: Literal["api", "worker", "publisher", "catalog", "reconciler", "observer"],
    settings: Settings | None = None,
) -> "PostgresControlPool":
    """Build one closed, bounded pool for an isolated long-running process."""

    from schemabridge.adapters.control_plane.postgres_pool import (
        ControlPoolSettings,
        PostgresControlPool,
    )

    resolved = settings or get_settings()
    if resolved.runtime_component != credential_kind:
        raise DatabaseConfigurationError(
            "control pool credential does not match the isolated runtime component"
        )
    return PostgresControlPool(
        ControlPoolSettings(
            dsn=_control_plane_dsn(resolved, credential_kind),
            application_name=f"schemabridge-control-{credential_kind}",
            min_size=resolved.control_pool_min_size,
            max_size=resolved.control_pool_max_size,
            max_waiting=resolved.control_pool_max_waiting,
            acquisition_timeout_seconds=(resolved.control_pool_acquisition_timeout_seconds),
            startup_timeout_seconds=resolved.control_pool_startup_timeout_seconds,
            close_timeout_seconds=resolved.control_pool_close_timeout_seconds,
            statement_timeout_ms=resolved.statement_timeout_ms,
            max_idle_seconds=resolved.control_pool_max_idle_seconds,
            max_lifetime_seconds=resolved.control_pool_max_lifetime_seconds,
        )
    )


def build_registry_control_store(
    *,
    credential_kind: Literal["runtime", "reconciler"] = "runtime",
    settings: Settings | None = None,
) -> RegistryControlStorePort:
    """Compose the authoritative PostgreSQL registry store with a bounded role."""

    from schemabridge.adapters.control_plane.postgres_registry_control import (
        PostgresRegistryControlStore,
    )

    resolved = settings or get_settings()
    _require_supported_control_plane_schema(resolved)
    return PostgresRegistryControlStore(
        dsn=_control_plane_dsn(resolved, credential_kind),
        audit_signing_keys=_control_audit_keys(resolved),
        active_audit_key_version=resolved.control_audit_key_version,
        schema=resolved.control_plane_schema,
        catalog_stale_after_seconds=resolved.catalog_stale_after_seconds,
    )


def build_active_registry_pointer_reader(
    *,
    credential_kind: Literal["runtime", "worker", "reconciler"] = "runtime",
    settings: Settings | None = None,
    connection_provider: "ControlConnectionProvider | None" = None,
) -> ActiveRegistryPointerReadPort:
    """Compose the pointer-only reader without any control-audit signing key."""

    from schemabridge.adapters.control_plane.postgres_active_registry import (
        PostgresActiveRegistryPointerReader,
    )

    resolved = settings or get_settings()
    _require_supported_control_plane_schema(resolved)
    return PostgresActiveRegistryPointerReader(
        dsn=_control_plane_dsn(resolved, credential_kind),
        schema=resolved.control_plane_schema,
        application_name=f"schemabridge-control-{credential_kind}",
        connection_provider=connection_provider,
    )


def _build_postgres_identity_rotation_store(
    settings: Settings,
) -> "PostgresIdentityRotationStore":
    from schemabridge.adapters.control_plane.postgres_identity_rotation import (
        PostgresIdentityRotationStore,
    )

    return PostgresIdentityRotationStore(
        dsn=_control_plane_dsn(settings, "runtime"),
        audit_signing_keys=_control_audit_keys(settings),
        active_audit_key_version=settings.control_audit_key_version,
        schema=settings.control_plane_schema,
    )


def build_identity_rotation_store(
    *,
    settings: Settings | None = None,
) -> "PostgresIdentityRotationStore":
    """Compose opaque identity lineage resolution through the runtime role."""

    resolved = settings or get_settings()
    if resolved.control_plane_kind != "postgres":
        raise DatabaseConfigurationError("identity rotation requires the PostgreSQL control plane")
    _require_supported_control_plane_schema(resolved)
    return _build_postgres_identity_rotation_store(resolved)


def build_identity_binding_resolver(
    *,
    credential_kind: Literal["api", "worker"],
    settings: Settings | None = None,
    connection_provider: "ControlConnectionProvider | None" = None,
) -> "PostgresIdentityBindingResolver":
    """Compose same-lineage reads without audit/HMAC or mutation capability."""

    from schemabridge.adapters.control_plane.postgres_identity_bindings import (
        PostgresIdentityBindingResolver,
    )

    resolved = settings or get_settings()
    if resolved.control_plane_kind != "postgres":
        raise DatabaseConfigurationError(
            "identity binding resolution requires the PostgreSQL control plane"
        )
    _require_supported_control_plane_schema(resolved)
    return PostgresIdentityBindingResolver(
        dsn=_control_plane_dsn(resolved, credential_kind),
        schema=resolved.control_plane_schema,
        application_name=f"schemabridge-control-{credential_kind}",
        connection_provider=connection_provider,
    )


def build_identity_evidence_reader(
    path: Path,
    *,
    clock: Callable[[], datetime] | None = None,
    settings: Settings | None = None,
) -> "IdentityEvidenceFileReader":
    """Compose an owner-only evidence reader from the configured migration key."""

    from schemabridge.adapters.control_plane.identity_evidence import (
        IdentityEvidenceFileReader,
    )

    resolved = settings or get_settings()
    if resolved.identity_migration_key is None:
        raise DatabaseConfigurationError("SCHEMABRIDGE_IDENTITY_MIGRATION_KEY is required")
    return IdentityEvidenceFileReader(
        path,
        signing_keys={
            resolved.identity_migration_key_version: (
                resolved.identity_migration_key.get_secret_value().encode()
            )
        },
        clock=clock or (lambda: datetime.now(UTC)),
    )


def build_identity_rotation_services(
    *,
    settings: Settings | None = None,
) -> IdentityRotationServices:
    """Compose initialization, prepare, approve, and completion on one current schema."""

    resolved = settings or get_settings()
    require_current_control_plane_schema(
        credential_kind="runtime",
        settings=resolved,
    )
    store = build_identity_rotation_store(settings=resolved)
    return IdentityRotationServices(
        initialize=InitializeVerifiedIdentityState(store),
        prepare=PrepareIdentityRotation(store),
        approve=ApproveIdentityRotation(store),
        resolve=ResolveReservedIdentityRotation(store),
        complete=CompleteReservedIdentityRotation(store),
    )


def build_legacy_control_plane_import(
    source_path: Path,
    *,
    settings: Settings | None = None,
) -> LegacyControlPlaneImportServices:
    """Compose one offline SQLite inspection and transactional PostgreSQL import."""

    from schemabridge.adapters.control_plane.legacy_import import (
        PostgresLegacyControlPlaneImportStore,
        SqliteLegacyControlPlaneSource,
    )

    resolved = settings or get_settings()
    if resolved.control_plane_kind != "postgres":
        raise DatabaseConfigurationError("legacy import requires the PostgreSQL control plane")
    require_current_control_plane_schema(
        credential_kind="runtime",
        settings=resolved,
    )
    source = SqliteLegacyControlPlaneSource(source_path)
    store = PostgresLegacyControlPlaneImportStore(
        dsn=_control_plane_dsn(resolved, "runtime"),
        schema=resolved.control_plane_schema,
    )
    return LegacyControlPlaneImportServices(
        inspect=InspectLegacyControlPlaneImport(source),
        prepare=PrepareLegacyControlPlaneImport(source, store),
        apply=ApplyLegacyControlPlaneImport(source, store),
    )


def build_registry_version_reader(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> RegistryVersionReadPort:
    """Compose the exact-version, mutation-free DataHub registry reader."""

    from schemabridge.adapters.semantic_registry.datahub import DataHubRegistryReadConfig
    from schemabridge.adapters.semantic_registry.datahub_control import (
        DataHubRegistryVersionReader,
    )
    from schemabridge.adapters.semantic_registry.remote_secrets import (
        RemoteDataHubRegistryVersionReader,
    )

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    if resolved.connector_secret_mode == "remote":
        return RemoteDataHubRegistryVersionReader(
            credentials=_build_registry_credential_resolver(resolved)
        )
    reader_env_path = resolved.semantic_registry_reader_env_path
    if not reader_env_path.is_absolute():
        reader_env_path = root / reader_env_path
    return DataHubRegistryVersionReader(
        config=DataHubRegistryReadConfig.from_env_file(reader_env_path.resolve())
    )


def build_registry_activation_preparer(
    *,
    workspace_id: str,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> PrepareRegistryActivation:
    """Compose read-only preparation of one exact forward activation."""

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    require_current_control_plane_schema(
        credential_kind="runtime",
        repository_root=root,
        settings=resolved,
    )
    return PrepareRegistryActivation(
        store=build_registry_control_store(settings=resolved),
        versions=build_registry_version_reader(
            repository_root=root,
            settings=resolved,
        ),
        scope=_semantic_registry_scope(resolved, workspace_id),
    )


def build_registry_rollback_preparer(
    *,
    workspace_id: str,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> PrepareRegistryRollback:
    """Compose read-only preparation of a new generation for an approved prior version."""

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    require_current_control_plane_schema(
        credential_kind="runtime",
        repository_root=root,
        settings=resolved,
    )
    return PrepareRegistryRollback(
        store=build_registry_control_store(settings=resolved),
        versions=build_registry_version_reader(
            repository_root=root,
            settings=resolved,
        ),
        scope=_semantic_registry_scope(resolved, workspace_id),
    )


def build_registry_activation_committer(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> CommitRegistryActivation:
    """Compose the atomic runtime-role activation commit without preparing an approval."""

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    require_current_control_plane_schema(
        credential_kind="runtime",
        repository_root=root,
        settings=resolved,
    )
    return CommitRegistryActivation(
        store=build_registry_control_store(settings=resolved),
        versions=build_registry_version_reader(
            repository_root=root,
            settings=resolved,
        ),
    )


def build_registry_projection(
    *,
    repository_root: Path | None = None,
) -> RegistryProjectionPort:
    """Compose the bounded DataHub active-pointer projection adapter."""

    from schemabridge.adapters.semantic_registry.datahub import DataHubRegistryWriteConfig
    from schemabridge.adapters.semantic_registry.datahub_projection import (
        DataHubRegistryProjectionAdapter,
    )

    root = (repository_root or Path.cwd()).resolve()
    return DataHubRegistryProjectionAdapter(
        config=DataHubRegistryWriteConfig.from_env_file(root / ".local/datahub/writer.env")
    )


def build_registry_reconciliation_inspector(
    *,
    workspace_id: str,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> InspectRegistryReconciliation:
    """Compose read-only reconciliation inspection with the reconciler role."""

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    require_current_control_plane_schema(
        credential_kind="reconciler",
        repository_root=root,
        settings=resolved,
    )
    return InspectRegistryReconciliation(
        store=build_registry_control_store(
            credential_kind="reconciler",
            settings=resolved,
        ),
        versions=build_registry_version_reader(
            repository_root=root,
            settings=resolved,
        ),
        projection=build_registry_projection(repository_root=root),
        scope=_semantic_registry_scope(resolved, workspace_id),
    )


def build_registry_reconciliation_repairer(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> ReconcileRegistryProjection:
    """Compose approval-gated DataHub repair with the bounded reconciler role."""

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    require_current_control_plane_schema(
        credential_kind="reconciler",
        repository_root=root,
        settings=resolved,
    )
    return ReconcileRegistryProjection(
        store=build_registry_control_store(
            credential_kind="reconciler",
            settings=resolved,
        ),
        versions=build_registry_version_reader(
            repository_root=root,
            settings=resolved,
        ),
        projection=build_registry_projection(repository_root=root),
    )


def build_control_plane_backup(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> ControlPlaneBackupPort:
    """Compose backup only with the dedicated read-only backup credential and audit key."""

    from schemabridge.adapters.control_plane.postgres_operations import (
        PostgresControlPlaneBackup,
    )

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    _require_supported_control_plane_schema(resolved)
    migrations_path = resolve_control_plane_migrations_path(root)
    build_control_plane_migrator(
        credential_kind="backup",
        repository_root=root,
        settings=resolved,
    )
    return PostgresControlPlaneBackup(
        dsn=_control_plane_dsn(resolved, "backup"),
        migrations_path=migrations_path,
        audit_signing_keys=_control_audit_keys(resolved),
        active_audit_key_version=resolved.control_audit_key_version,
    )


def build_control_plane_restore(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> ControlPlaneRestorePort:
    """Compose restore from a dedicated secret target credential, never process arguments."""

    from schemabridge.adapters.control_plane.postgres_operations import (
        PostgresControlPlaneRestore,
    )

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    _require_supported_control_plane_schema(resolved)
    if resolved.control_restore_database_url is None:
        raise DatabaseConfigurationError("SCHEMABRIDGE_CONTROL_RESTORE_DATABASE_URL is required")
    target_dsn = resolved.control_restore_database_url.get_secret_value()
    from schemabridge.adapters.control_plane.postgres_migrations import (
        PostgresControlPlaneMigrator,
    )

    target_migrator = PostgresControlPlaneMigrator(
        target_dsn,
        resolve_control_plane_migrations_path(root),
    )
    if target_migrator.known_migrations()[-1].version != resolved.control_plane_schema_version:
        raise DatabaseConfigurationError(
            "configured control-plane schema version does not match this release"
        )
    return PostgresControlPlaneRestore(
        target_dsn=target_dsn,
        migrations_path=resolve_control_plane_migrations_path(root),
        audit_signing_keys=_control_audit_keys(resolved),
    )


def _control_plane_dsn(settings: Settings, credential_kind: ControlPlaneCredential) -> str:
    configured = {
        "runtime": settings.control_database_url,
        "reconciler": settings.control_reconciler_database_url,
        "migrator": settings.control_migrator_database_url,
        "api": settings.control_api_database_url,
        "worker": settings.control_worker_database_url,
        "publisher": settings.control_publisher_database_url,
        "catalog": settings.control_catalog_database_url,
        "observer": settings.control_observer_database_url,
        "backup": settings.control_backup_database_url,
    }[credential_kind]
    if configured is None:
        variable = {
            "runtime": "SCHEMABRIDGE_CONTROL_DATABASE_URL",
            "reconciler": "SCHEMABRIDGE_CONTROL_RECONCILER_DATABASE_URL",
            "migrator": "SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL",
            "api": "SCHEMABRIDGE_CONTROL_API_DATABASE_URL",
            "worker": "SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL",
            "publisher": "SCHEMABRIDGE_CONTROL_PUBLISHER_DATABASE_URL",
            "catalog": "SCHEMABRIDGE_CONTROL_CATALOG_DATABASE_URL",
            "observer": "SCHEMABRIDGE_CONTROL_OBSERVER_DATABASE_URL",
            "backup": "SCHEMABRIDGE_CONTROL_BACKUP_DATABASE_URL",
        }[credential_kind]
        raise DatabaseConfigurationError(f"{variable} is required")
    return configured.get_secret_value()


def _control_audit_keys(settings: Settings) -> dict[str, bytes]:
    if settings.control_audit_signing_key is None:
        raise DatabaseConfigurationError("SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY is required")
    return {
        settings.control_audit_key_version: (
            settings.control_audit_signing_key.get_secret_value().encode()
        )
    }


def _require_supported_control_plane_schema(settings: Settings) -> None:
    if settings.control_plane_schema != CONTROL_PLANE_SCHEMA:
        raise DatabaseConfigurationError(
            "configured control-plane schema is not supported by this release"
        )


def _reject_operator_credentials_in_managed_web(settings: Settings) -> None:
    if settings.runtime_profile in {"staging", "production"} and (
        settings.control_reconciler_database_url is not None
        or settings.control_migrator_database_url is not None
        or settings.control_api_database_url is not None
        or settings.control_worker_database_url is not None
        or settings.control_publisher_database_url is not None
        or settings.control_catalog_database_url is not None
        or settings.control_observer_database_url is not None
        or settings.control_restore_database_url is not None
        or settings.control_operator_actor_id is not None
        or settings.control_operator_roles
    ):
        raise RuntimeError("managed web runtime must not receive operator credentials or identity")


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
        guided=build_guided_request_builder(repository_root=root, settings=resolved),
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


def build_m30_readiness_assessor(
    *,
    repository_root: Path | None = None,
) -> AssessM30Readiness:
    """Compose the offline fail-closed M30 assessor at the only composition root."""

    from schemabridge.adapters.evaluation.m30_readiness import (
        FileM30CampaignContract,
        GitM30CandidateIdentity,
    )

    root = (repository_root or Path.cwd()).resolve()
    return AssessM30Readiness(
        contract_loader=FileM30CampaignContract(root),
        candidate_identity=GitM30CandidateIdentity(root),
    )


def build_m30_readiness_report_writer(
    *,
    repository_root: Path | None = None,
) -> M30ReadinessReportWriterPort:
    """Compose the M30 report writer with its exact candidate-root boundary."""

    from schemabridge.adapters.evaluation.m30_readiness import FileM30ReadinessReportWriter

    return FileM30ReadinessReportWriter((repository_root or Path.cwd()).resolve())


def build_review_store(
    settings: Settings | None = None,
    *,
    workspace_id: str | None = None,
) -> ReviewStorePort:
    """Compose canonical-review state for the selected control plane."""

    resolved = settings or get_settings()
    if resolved.control_plane_kind == "postgres":
        from schemabridge.adapters.storage.postgres import PostgresReviewStore

        return PostgresReviewStore(
            _control_plane_dsn(resolved, "runtime"),
            workspace_id=_control_workspace(resolved, workspace_id),
            schema=resolved.control_plane_schema,
        )
    from schemabridge.adapters.storage.reviews import SqliteReviewStore

    return SqliteReviewStore(resolved.draft_store_path.resolve())


def build_publication_audit_store(
    settings: Settings | None = None,
    *,
    workspace_id: str | None = None,
) -> PublicationAuditStorePort:
    """Compose the mandatory append-only publication audit store."""

    resolved = settings or get_settings()
    if resolved.control_plane_kind == "postgres":
        from schemabridge.adapters.storage.postgres import PostgresPublicationAuditStore

        return PostgresPublicationAuditStore(
            _control_plane_dsn(resolved, "runtime"),
            workspace_id=_control_workspace(resolved, workspace_id),
            schema=resolved.control_plane_schema,
        )
    from schemabridge.adapters.storage.publication_audit import SqlitePublicationAuditStore

    return SqlitePublicationAuditStore(resolved.draft_store_path.resolve())


def build_review_start(
    settings: Settings | None = None,
    *,
    workspace_id: str | None = None,
) -> StartCanonicalReview:
    return StartCanonicalReview(build_review_store(settings, workspace_id=workspace_id))


def build_review_inspector(
    settings: Settings | None = None,
    *,
    workspace_id: str | None = None,
) -> InspectCanonicalReview:
    return InspectCanonicalReview(build_review_store(settings, workspace_id=workspace_id))


def build_review_decider(
    settings: Settings | None = None,
    *,
    workspace_id: str | None = None,
) -> DecideCanonicalMapping:
    return DecideCanonicalMapping(build_review_store(settings, workspace_id=workspace_id))


def build_review_editor(
    settings: Settings | None = None,
    *,
    workspace_id: str | None = None,
) -> EditCanonicalReview:
    return EditCanonicalReview(build_review_store(settings, workspace_id=workspace_id))


def build_publication_preparer(
    settings: Settings | None = None,
    *,
    workspace_id: str | None = None,
) -> PrepareCanonicalPublication:
    return PrepareCanonicalPublication(build_review_store(settings, workspace_id=workspace_id))


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
    workspace_id: str | None = None,
) -> PublishCanonicalReview:
    return PublishCanonicalReview(
        store=build_review_store(settings, workspace_id=workspace_id),
        writer=build_catalog_writer(adapter_kind, repository_root=repository_root),
        audit_store=build_publication_audit_store(
            settings,
            workspace_id=workspace_id,
        ),
    )


def build_published_context_reader(
    adapter_kind: Literal["live", "fake"],
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
    workspace_id: str | None = None,
) -> ReadPublishedCanonicalContext:
    writer = build_catalog_writer(adapter_kind, repository_root=repository_root)
    return ReadPublishedCanonicalContext(
        build_review_store(settings, workspace_id=workspace_id),
        cast(CanonicalContextReadPort, writer),
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


def build_join_review_store(
    settings: Settings | None = None,
    *,
    workspace_id: str | None = None,
) -> JoinReviewStorePort:
    resolved = settings or get_settings()
    if resolved.control_plane_kind == "postgres":
        from schemabridge.adapters.storage.postgres import PostgresJoinReviewStore

        return PostgresJoinReviewStore(
            _control_plane_dsn(resolved, "runtime"),
            workspace_id=_control_workspace(resolved, workspace_id),
            schema=resolved.control_plane_schema,
        )
    from schemabridge.adapters.storage.relationships import SqliteJoinReviewStore

    return SqliteJoinReviewStore(resolved.draft_store_path.resolve())


def build_join_review_start(
    settings: Settings | None = None,
    *,
    workspace_id: str | None = None,
) -> StartJoinReview:
    return StartJoinReview(build_join_review_store(settings, workspace_id=workspace_id))


def build_join_review_inspector(
    settings: Settings | None = None,
    *,
    workspace_id: str | None = None,
) -> InspectJoinReview:
    return InspectJoinReview(build_join_review_store(settings, workspace_id=workspace_id))


def build_join_review_decider(
    settings: Settings | None = None,
    *,
    workspace_id: str | None = None,
) -> DecideJoinCandidate:
    return DecideJoinCandidate(build_join_review_store(settings, workspace_id=workspace_id))


def build_join_publication_preparer(
    settings: Settings | None = None,
    *,
    workspace_id: str | None = None,
) -> PrepareJoinPublication:
    return PrepareJoinPublication(build_join_review_store(settings, workspace_id=workspace_id))


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
    workspace_id: str | None = None,
) -> PublishJoinContracts:
    return PublishJoinContracts(
        store=build_join_review_store(settings, workspace_id=workspace_id),
        writer=build_join_context_adapter(adapter_kind, repository_root=repository_root),
        audit_store=build_publication_audit_store(
            settings,
            workspace_id=workspace_id,
        ),
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
    settings: Settings | None = None,
    registry: GovernedSemanticRegistryPort | None = None,
    workspace_id: str | None = None,
) -> BuildGuidedRequest:
    """Compose guided requests from the same atomic registry used by planning."""

    return BuildGuidedRequest(
        context=registry
        or build_semantic_registry(
            repository_root=repository_root,
            settings=settings,
            workspace_id=workspace_id,
        )
    )


def build_query_studio_runtime(
    *,
    principal: AuthenticatedPrincipal,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> QueryStudioRuntimeServices:
    """Compose dynamic governed retrieval and an explicit fake/live/disabled AI lane."""

    from schemabridge.adapters.query_studio.fake_language import (
        DeterministicDescriptionExpansion,
        DeterministicQueryStudioIntent,
        fake_query_studio_configuration,
    )
    from schemabridge.adapters.query_studio.recorded import (
        RecordedGovernedBindingFactsSearch,
    )
    from schemabridge.adapters.query_studio.security import (
        HmacQueryStudioCandidateIds,
        HmacQueryStudioPreviewTokens,
        SecureQueryStudioNonce,
        SystemQueryStudioClock,
    )
    from schemabridge.application.ports.query_studio import (
        DescriptionExpansionPort,
        GovernedBindingFactsSearchPort,
        QueryStudioIntentPort,
    )
    from schemabridge.application.query_studio import (
        BrowseGuidedGovernedFields,
        ConfirmGuidedQueryStudioPreview,
        ConfirmQueryStudioPreview,
        DiscoverPhysicalFields,
        InspectPhysicalDiscoveryCardinality,
        PrepareGuidedSelectionQueryStudioPreview,
        PrepareNaturalLanguageQueryStudioPreview,
        RecomputeGuidedQueryStudioEvidence,
        RecomputeNaturalLanguageQueryStudioPreview,
        RegistryAwareGovernedFieldSearch,
        SearchGovernedFields,
    )
    from schemabridge.domain.query_studio import (
        LOCAL_AI_ATTEMPT_POLICY_VERSION,
        QUERY_STUDIO_ORCHESTRATION_POLICY_VERSION,
        ProviderConfigurationFacts,
    )
    from schemabridge.domain.semantic_registry import (
        semantic_registry_scope_fingerprint,
    )

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    if principal.workspace_id != principal.workspace_id.strip():
        raise ValueError("Query Studio principal workspace is not canonical")
    registry = build_semantic_registry(
        repository_root=root,
        settings=resolved,
        workspace_id=principal.workspace_id,
    )
    scoped_registry = registry.load()
    facts: GovernedBindingFactsSearchPort
    if (
        resolved.control_plane_kind == "postgres"
        and resolved.semantic_registry_selection == "active"
    ):
        from schemabridge.adapters.catalog.postgres_governed_search import (
            PostgresGovernedBindingFactsSearch,
        )

        facts = PostgresGovernedBindingFactsSearch(
            _control_plane_dsn(resolved, "runtime"),
            schema=resolved.control_plane_schema,
        )
    else:
        facts = RecordedGovernedBindingFactsSearch(
            scoped_registry,
            root / "demo/datahub/catalog_snapshot.json",
        )
    registry_search = RegistryAwareGovernedFieldSearch(
        registry=registry,
        facts=facts,
    )
    search = SearchGovernedFields(registry=registry, search=registry_search)
    signing_key = _query_studio_signing_key(resolved)
    candidate_ids = HmacQueryStudioCandidateIds(signing_key)
    preview_tokens = HmacQueryStudioPreviewTokens(signing_key)
    clock = SystemQueryStudioClock()
    nonces = SecureQueryStudioNonce()
    physical_discovery: object
    if (
        resolved.control_plane_kind == "postgres"
        and resolved.semantic_registry_selection == "active"
    ):
        from schemabridge.adapters.catalog.postgres_physical_discovery import (
            PostgresPhysicalFieldDiscovery,
        )

        physical_discovery = PostgresPhysicalFieldDiscovery.from_signing_key(
            dsn=_control_plane_dsn(resolved, "runtime"),
            cursor_signing_key=signing_key,
            schema=resolved.control_plane_schema,
        )
    else:
        from schemabridge.adapters.query_studio.recorded_physical import (
            RecordedPhysicalFieldDiscovery,
        )

        physical_discovery = RecordedPhysicalFieldDiscovery(
            root / "demo/datahub/catalog_snapshot.json",
            signing_key,
        )
    discover_physical = DiscoverPhysicalFields(
        cast("PhysicalFieldDiscoveryPort", physical_discovery)
    )
    catalog_cardinality = InspectPhysicalDiscoveryCardinality(
        cast("PhysicalFieldDiscoveryPort", physical_discovery)
    ).execute(scoped_registry.scope)
    governed_mapping_count = len(scoped_registry.mapping_set.mappings)

    if resolved.query_studio_ai_mode == "disabled":
        configuration = ProviderConfigurationFacts.create(
            adapter="disabled",
            model_snapshot="query-studio-disabled-v1",
            reasoning_effort="none",
            endpoint_region="local",
            prompt_version="m27-disabled-v1",
            schema_version="m27-v1",
            matcher_version=GOVERNED_DESCRIPTION_MATCHER_VERSION,
            orchestration_policy_version=QUERY_STUDIO_ORCHESTRATION_POLICY_VERSION,
            attempt_policy_version=LOCAL_AI_ATTEMPT_POLICY_VERSION,
            external_ai=False,
        )
        guided_resolver = RecomputeGuidedQueryStudioEvidence(
            registry=registry,
            search=registry_search,
            candidate_ids=candidate_ids,
        )
        return QueryStudioRuntimeServices(
            scope=scoped_registry.scope,
            ai_mode="disabled",
            configuration=configuration,
            search=search,
            browse_guided=BrowseGuidedGovernedFields(
                registry=registry,
                search=registry_search,
                candidate_ids=candidate_ids,
                clock=clock,
                nonces=nonces,
            ),
            prepare_guided=PrepareGuidedSelectionQueryStudioPreview(
                resolver=guided_resolver,
                preview_tokens=preview_tokens,
                clock=clock,
                configuration=configuration,
            ),
            confirm_guided=ConfirmGuidedQueryStudioPreview(
                registry=registry,
                recompute=guided_resolver,
                preview_tokens=preview_tokens,
                clock=clock,
                configuration=configuration,
                guided_builder=BuildGuidedRequest(registry),
            ),
            catalog_cardinality=catalog_cardinality,
            governed_mapping_count=governed_mapping_count,
            discover_physical=discover_physical,
        )

    expansion: DescriptionExpansionPort
    interpreter: QueryStudioIntentPort
    if resolved.query_studio_ai_mode == "fake":
        expansion = DeterministicDescriptionExpansion()
        interpreter = DeterministicQueryStudioIntent()
        configuration = fake_query_studio_configuration()
    else:
        from schemabridge.adapters.control_plane.postgres_query_studio_ai import (
            PostgresQueryStudioAiControl,
        )
        from schemabridge.adapters.language.openai_boundary import (
            OpenAIRegion,
            OpenAIResponsesConfig,
            derive_safety_identifier,
        )
        from schemabridge.adapters.language.openai_query_studio import (
            create_openai_query_studio_intent_adapter_from_environment,
            openai_interpretation_input_token_reservation_bound,
        )
        from schemabridge.adapters.query_studio.atomic_preflight import (
            BoundaryScreenedDescriptionExpansionPreflight,
        )
        from schemabridge.application.query_studio_ai_admission import (
            AdmittedQueryStudioIntent,
        )

        if resolved.pseudonymization_key is None:
            raise ValueError("live Query Studio pseudonymization key is unavailable")
        require_current_control_plane_schema(
            credential_kind="runtime",
            repository_root=root,
            settings=resolved,
        )
        pseudonym_key = resolved.pseudonymization_key.get_secret_value().encode("utf-8")
        openai_config = OpenAIResponsesConfig.for_model(
            resolved.query_studio_ai_model,
            region=OpenAIRegion(resolved.query_studio_ai_region),
        )
        raw_interpreter = create_openai_query_studio_intent_adapter_from_environment(
            openai_config,
            safety_identifier=derive_safety_identifier(
                pseudonym_key,
                workspace_identity=principal.workspace_id,
                actor_identity=principal.actor_id,
            ),
            matcher_version=GOVERNED_DESCRIPTION_MATCHER_VERSION,
            semantic_scope_fingerprint=semantic_registry_scope_fingerprint(scoped_registry.scope),
            public_metadata_registry_fingerprint=scoped_registry.registry.fingerprint,
        )
        configuration = raw_interpreter.configuration
        control = PostgresQueryStudioAiControl(
            _control_plane_dsn(resolved, "runtime"),
            schema=resolved.control_plane_schema,
        )
        actor_digest = hmac.new(
            pseudonym_key,
            f"schemabridge-ai-audit-v1\0{principal.actor_id}".encode(),
            hashlib.sha256,
        ).hexdigest()
        scope_fingerprint = semantic_registry_scope_fingerprint(scoped_registry.scope)
        expansion = BoundaryScreenedDescriptionExpansionPreflight()
        interpreter = AdmittedQueryStudioIntent(
            delegate=raw_interpreter,
            control=control,
            nonces=nonces,
            workspace_id=principal.workspace_id,
            actor_digest=actor_digest,
            semantic_scope_fingerprint=scope_fingerprint,
            configuration=configuration,
            estimated_input_tokens=openai_interpretation_input_token_reservation_bound(),
            estimated_output_tokens=openai_config.interpretation_max_output_tokens,
        )

    prepare = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=registry_search,
        expansion=expansion,
        interpreter=interpreter,
        candidate_ids=candidate_ids,
        preview_tokens=preview_tokens,
        clock=clock,
        nonces=nonces,
        configuration=configuration,
    )
    confirm = ConfirmQueryStudioPreview(
        registry=registry,
        search=registry_search,
        candidate_ids=candidate_ids,
        preview_tokens=preview_tokens,
        clock=clock,
        configuration=configuration,
        guided_builder=BuildGuidedRequest(registry),
    )
    recompute_natural = RecomputeNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=registry_search,
        candidate_ids=candidate_ids,
        preview_tokens=preview_tokens,
        clock=clock,
        configuration=configuration,
    )
    guided_resolver = RecomputeGuidedQueryStudioEvidence(
        registry=registry,
        search=registry_search,
        candidate_ids=candidate_ids,
    )
    return QueryStudioRuntimeServices(
        scope=scoped_registry.scope,
        ai_mode=resolved.query_studio_ai_mode,
        configuration=configuration,
        search=search,
        browse_guided=BrowseGuidedGovernedFields(
            registry=registry,
            search=registry_search,
            candidate_ids=candidate_ids,
            clock=clock,
            nonces=nonces,
        ),
        prepare_guided=PrepareGuidedSelectionQueryStudioPreview(
            resolver=guided_resolver,
            preview_tokens=preview_tokens,
            clock=clock,
            configuration=configuration,
        ),
        confirm_guided=ConfirmGuidedQueryStudioPreview(
            registry=registry,
            recompute=guided_resolver,
            preview_tokens=preview_tokens,
            clock=clock,
            configuration=configuration,
            guided_builder=BuildGuidedRequest(registry),
        ),
        catalog_cardinality=catalog_cardinality,
        governed_mapping_count=governed_mapping_count,
        prepare_natural=prepare,
        confirm_natural=confirm,
        recompute_natural=recompute_natural,
        discover_physical=discover_physical,
        expansion=expansion,
    )


def build_natural_sql_runtime(
    *,
    principal: AuthenticatedPrincipal,
    repository_root: Path | None = None,
    settings: Settings | None = None,
    registry: GovernedSemanticRegistryPort | None = None,
    mentions: "AdvancedMentionExtractionPort | None" = None,
    interpreter: "AdvancedInterpretationPort | None" = None,
    retrieval: "AdvancedSemanticRetrievalPort | None" = None,
) -> NaturalSqlRuntimeServices:
    """Compose text → confirmed standalone SQL without an execution capability."""

    from schemabridge.adapters.query_studio.advanced_fake_language import (
        DeterministicAdvancedLanguageAdapter,
    )
    from schemabridge.adapters.query_studio.advanced_security import (
        HmacAdvancedQueryPreviewTokens,
    )
    from schemabridge.adapters.query_studio.advanced_semantic_index import (
        RegistryWideAdvancedSemanticIndex,
    )
    from schemabridge.adapters.query_studio.security import (
        SecureQueryStudioNonce,
        SystemQueryStudioClock,
    )
    from schemabridge.adapters.sql.compiler import PostgresQueryCompiler
    from schemabridge.adapters.sql.export import PostgresCopyableSqlRenderer
    from schemabridge.application.natural_sql import (
        ConfirmNaturalSqlPreview,
        GenerateGovernedCopyableSql,
        PrepareNaturalSqlPreview,
    )
    from schemabridge.domain.semantic_registry import (
        semantic_registry_scope_fingerprint,
    )

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    if principal.workspace_id != principal.workspace_id.strip():
        raise ValueError("natural SQL principal workspace is not canonical")
    if (mentions is None) != (interpreter is None):
        raise ValueError(
            "natural SQL mention extraction and interpretation must be injected together"
        )
    if resolved.query_studio_ai_mode == "disabled":
        raise ValueError("natural SQL interpretation is disabled")
    if mentions is not None and (
        resolved.query_studio_ai_mode != "fake" or resolved.runtime_profile != "development"
    ):
        raise ValueError("natural SQL language-port injection is limited to the local fake seam")
    semantic_registry = registry or build_semantic_registry(
        repository_root=root,
        settings=resolved,
        workspace_id=principal.workspace_id,
    )
    scoped = semantic_registry.load()
    limits = ResolutionLimits(
        max_tables=resolved.max_query_tables,
        max_preview_rows=resolved.max_query_rows,
        statement_timeout_ms=resolved.statement_timeout_ms,
    )
    nonces = SecureQueryStudioNonce()
    if resolved.query_studio_ai_mode == "live" and mentions is not None:
        raise ValueError(
            "live natural SQL language adapters must be composed through durable admission"
        )
    if mentions is None:
        if resolved.query_studio_ai_mode == "fake":
            fake_language = DeterministicAdvancedLanguageAdapter()
            mentions = fake_language
            interpreter = fake_language
        else:
            from schemabridge.adapters.control_plane.postgres_query_studio_ai import (
                PostgresQueryStudioAiControl,
            )
            from schemabridge.adapters.language.openai_advanced_query_studio import (
                create_openai_advanced_query_studio_adapters_from_environment,
                openai_advanced_interpretation_input_token_reservation_bound,
                openai_advanced_mention_input_token_reservation_bound,
            )
            from schemabridge.adapters.language.openai_boundary import (
                OpenAIRegion,
                OpenAIResponsesConfig,
                derive_safety_identifier,
            )
            from schemabridge.application.query_studio_ai_admission import (
                AdmittedAdvancedInterpretation,
                AdmittedAdvancedMentionExtraction,
            )

            if resolved.pseudonymization_key is None:
                raise ValueError("live natural SQL pseudonymization key is unavailable")
            require_current_control_plane_schema(
                credential_kind="runtime",
                repository_root=root,
                settings=resolved,
            )
            pseudonym_key = resolved.pseudonymization_key.get_secret_value().encode("utf-8")
            openai_config = OpenAIResponsesConfig.for_model(
                resolved.query_studio_ai_model,
                region=OpenAIRegion(resolved.query_studio_ai_region),
            )
            scope_fingerprint = semantic_registry_scope_fingerprint(scoped.scope)
            raw_mentions, raw_interpreter = (
                create_openai_advanced_query_studio_adapters_from_environment(
                    openai_config,
                    safety_identifier=derive_safety_identifier(
                        pseudonym_key,
                        workspace_identity=principal.workspace_id,
                        actor_identity=principal.actor_id,
                    ),
                    matcher_version=GOVERNED_DESCRIPTION_MATCHER_VERSION,
                    semantic_scope_fingerprint=scope_fingerprint,
                    public_metadata_registry_fingerprint=scoped.registry.fingerprint,
                )
            )
            if raw_mentions.configuration != raw_interpreter.configuration:
                raise ValueError("live natural SQL stages use different provider configurations")
            control = PostgresQueryStudioAiControl(
                _control_plane_dsn(resolved, "runtime"),
                schema=resolved.control_plane_schema,
            )
            actor_digest = hmac.new(
                pseudonym_key,
                f"schemabridge-ai-audit-v1\0{principal.actor_id}".encode(),
                hashlib.sha256,
            ).hexdigest()
            mentions = AdmittedAdvancedMentionExtraction(
                delegate=raw_mentions,
                control=control,
                nonces=nonces,
                workspace_id=principal.workspace_id,
                actor_digest=actor_digest,
                semantic_scope_fingerprint=scope_fingerprint,
                configuration=raw_mentions.configuration,
                estimated_input_tokens=(openai_advanced_mention_input_token_reservation_bound()),
                estimated_output_tokens=openai_config.expansion_max_output_tokens,
            )
            interpreter = AdmittedAdvancedInterpretation(
                delegate=raw_interpreter,
                control=control,
                nonces=nonces,
                workspace_id=principal.workspace_id,
                actor_digest=actor_digest,
                semantic_scope_fingerprint=scope_fingerprint,
                configuration=raw_interpreter.configuration,
                estimated_input_tokens=(
                    openai_advanced_interpretation_input_token_reservation_bound()
                ),
                estimated_output_tokens=(openai_config.interpretation_max_output_tokens),
            )
    assert interpreter is not None
    tokens = HmacAdvancedQueryPreviewTokens(_query_studio_signing_key(resolved))
    clock = SystemQueryStudioClock()
    return NaturalSqlRuntimeServices(
        scope=scoped.scope,
        ai_mode=("live" if resolved.query_studio_ai_mode == "live" else "fake"),
        prepare=PrepareNaturalSqlPreview(
            registry=semantic_registry,
            mentions=mentions,
            retrieval=retrieval or RegistryWideAdvancedSemanticIndex(),
            interpreter=interpreter,
            preview_tokens=tokens,
            clock=clock,
            nonces=nonces,
            limits=limits,
        ),
        confirm=ConfirmNaturalSqlPreview(
            registry=semantic_registry,
            preview_tokens=tokens,
            clock=clock,
        ),
        generate=GenerateGovernedCopyableSql(
            registry=semantic_registry,
            compiler=PostgresQueryCompiler(),
            guard=build_sql_guard(),
            renderer=PostgresCopyableSqlRenderer(),
            limits=limits,
        ),
    )


@lru_cache(maxsize=1)
def _local_query_studio_signing_key() -> bytes:
    """Keep one process-local synthetic key stable across Streamlit reruns."""

    while True:
        value = secrets.token_bytes(32)
        if len(set(value)) >= 8:
            return value


def _query_studio_signing_key(settings: Settings) -> bytes:
    if settings.query_studio_signing_key is None:
        if settings.runtime_profile in {"staging", "production"}:
            raise ValueError("managed Query Studio signing key is unavailable")
        return _local_query_studio_signing_key()
    return settings.query_studio_signing_key.get_secret_value().encode("utf-8")


def build_natural_language_intent_resolver(
    adapter_kind: Literal["fake", "live"] = "fake",
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
    registry: GovernedSemanticRegistryPort | None = None,
    workspace_id: str | None = None,
) -> ResolveNaturalLanguageIntent:
    """Compose an explicit fake or live parser; live never falls back without credentials."""

    context = registry or build_semantic_registry(
        repository_root=repository_root,
        settings=settings,
        workspace_id=workspace_id,
    )
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


def build_request_draft_store(
    settings: Settings | None = None,
    *,
    workspace_id: str | None = None,
) -> RequestDraftStorePort:
    resolved = settings or get_settings()
    if resolved.control_plane_kind == "postgres":
        from schemabridge.adapters.storage.postgres import PostgresRequestDraftStore

        return PostgresRequestDraftStore(
            _control_plane_dsn(resolved, "runtime"),
            workspace_id=_control_workspace(resolved, workspace_id),
            schema=resolved.control_plane_schema,
        )
    from schemabridge.adapters.storage.request_drafts import SqliteRequestDraftStore

    return SqliteRequestDraftStore(resolved.draft_store_path.resolve())


def build_request_draft_saver(
    settings: Settings | None = None,
    *,
    workspace_id: str | None = None,
) -> SaveRequestDraft:
    return SaveRequestDraft(build_request_draft_store(settings, workspace_id=workspace_id))


def build_request_draft_loader(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
    workspace_id: str | None = None,
) -> LoadRequestDraft:
    return LoadRequestDraft(
        store=build_request_draft_store(settings, workspace_id=workspace_id),
        builder=build_guided_request_builder(
            repository_root=repository_root,
            settings=settings,
            workspace_id=workspace_id,
        ),
    )


def build_guided_request_submitter() -> SubmitGuidedRequest:
    """Compose the M09 fake planner that cannot resolve physical assets or execute SQL."""

    from schemabridge.adapters.requests.fake_planner import FakeRequestPlanner

    return SubmitGuidedRequest(planner=FakeRequestPlanner())


def build_semantic_registry(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
    workspace_id: str | None = None,
    control_credential_kind: Literal["runtime", "worker"] = "runtime",
    control_connection_provider: "ControlConnectionProvider | None" = None,
) -> GovernedSemanticRegistryPort:
    """Compose one integrity-checked registry bound to an explicit runtime scope."""

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    scope = _semantic_registry_scope(resolved, workspace_id)
    if resolved.semantic_registry_selection == "active":
        from schemabridge.application.registry_control import (
            LoadActiveGovernedSemanticRegistry,
        )

        require_current_control_plane_schema(
            credential_kind=control_credential_kind,
            repository_root=root,
            settings=resolved,
            connection_provider=control_connection_provider,
        )
        return LoadActiveGovernedSemanticRegistry(
            store=(
                build_active_registry_pointer_reader(
                    credential_kind="worker",
                    settings=resolved,
                    connection_provider=control_connection_provider,
                )
                if control_credential_kind == "worker"
                else build_registry_control_store(settings=resolved)
            ),
            versions=build_registry_version_reader(
                repository_root=root,
                settings=resolved,
            ),
            _scope=scope,
        )
    if resolved.registry_mode == "live":
        from schemabridge.adapters.semantic_registry.datahub import (
            DataHubGovernedSemanticRegistry,
            DataHubRegistryReadConfig,
        )

        if resolved.connector_secret_mode == "remote":
            from schemabridge.adapters.semantic_registry.remote_secrets import (
                RemoteDataHubGovernedSemanticRegistry,
            )

            return RemoteDataHubGovernedSemanticRegistry(
                credentials=_build_registry_credential_resolver(resolved),
                _scope=scope,
                version=resolved.semantic_registry_version,
            )
        reader_env_path = resolved.semantic_registry_reader_env_path
        if not reader_env_path.is_absolute():
            reader_env_path = root / reader_env_path
        return DataHubGovernedSemanticRegistry(
            config=DataHubRegistryReadConfig.from_env_file(reader_env_path),
            _scope=scope,
            version=resolved.semantic_registry_version,
        )

    from schemabridge.adapters.semantic_registry.recorded import (
        RecordedGovernedSemanticRegistry,
    )

    manifest_path = resolved.semantic_registry_manifest_path
    if not manifest_path.is_absolute():
        manifest_path = root / manifest_path
    return RecordedGovernedSemanticRegistry(
        manifest_path.resolve(),
        scope,
    )


def build_recorded_registry_publication_source(
    *,
    target_version: int | None = None,
    repository_root: Path | None = None,
    settings: Settings | None = None,
    workspace_id: str | None = None,
) -> ScopedSemanticRegistrySnapshot:
    """Derive one explicit immutable version from the verified approved bundle."""

    from schemabridge.adapters.semantic_registry.recorded import (
        RecordedGovernedSemanticRegistry,
    )

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    manifest_path = resolved.semantic_registry_manifest_path
    if not manifest_path.is_absolute():
        manifest_path = root / manifest_path
    source = RecordedGovernedSemanticRegistry(
        manifest_path.resolve(),
        _semantic_registry_scope(resolved, workspace_id),
    ).load()
    version = resolved.semantic_registry_version if target_version is None else target_version
    if version < 1:
        raise DatabaseConfigurationError("registry publication target version must be positive")
    return ScopedSemanticRegistrySnapshot.model_validate(
        {
            **source.model_dump(mode="python"),
            "registry": {
                **source.registry.model_dump(mode="python"),
                "version": version,
            },
        }
    )


def build_semantic_registry_version_publisher(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
    workspace_id: str | None = None,
) -> PublishGovernedSemanticRegistryVersion:
    """Compose the explicit-approval DataHub writer and durable local audit ledger."""

    from schemabridge.adapters.semantic_registry.datahub import (
        DataHubRegistryWriteConfig,
        DataHubSemanticRegistryPublisher,
    )

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    if resolved.runtime_profile == "hosted-demo":
        raise DatabaseConfigurationError(
            "semantic registry publication is disabled in the hosted demo"
        )
    return PublishGovernedSemanticRegistryVersion(
        publisher=DataHubSemanticRegistryPublisher(
            DataHubRegistryWriteConfig.from_env_file(root / ".local/datahub/writer.env")
        ),
        audit_store=_build_registry_publication_audit_store(
            resolved,
            workspace_id=workspace_id,
        ),
    )


def build_semantic_registry_publication_approval_preparer(
    *,
    settings: Settings | None = None,
    workspace_id: str | None = None,
) -> PrepareGovernedSemanticRegistryPublicationApproval:
    """Compose replay-safe registry approval preparation over the durable audit ledger."""

    resolved = settings or get_settings()
    if resolved.runtime_profile == "hosted-demo":
        raise DatabaseConfigurationError(
            "semantic registry publication is disabled in the hosted demo"
        )
    return PrepareGovernedSemanticRegistryPublicationApproval(
        audit_store=_build_registry_publication_audit_store(
            resolved,
            workspace_id=workspace_id,
        )
    )


def _build_registry_publication_audit_store(
    settings: Settings,
    *,
    workspace_id: str | None,
) -> PublicationAuditStorePort:
    try:
        return build_publication_audit_store(settings, workspace_id=workspace_id)
    except PublicationAuditStoreError as error:
        raise DatabaseConfigurationError(
            "semantic registry publication audit store is unavailable"
        ) from error


def _semantic_registry_scope(
    settings: Settings,
    workspace_id: str | None,
) -> SemanticRegistryScope:
    if workspace_id is None:
        if settings.auth_mode != "local-demo":
            raise DatabaseConfigurationError(
                "OIDC semantic registry composition requires an authenticated workspace"
            )
        workspace_id = build_streamlit_principal(settings=settings).workspace_id
    return SemanticRegistryScope(
        workspace_id=workspace_id,
        catalog_scope=settings.semantic_registry_catalog_scope,
        registry_id=settings.semantic_registry_id,
    )


def build_semantic_registry_scope(
    *,
    workspace_id: str,
    settings: Settings | None = None,
) -> SemanticRegistryScope:
    """Compose the configured registry scope around one authenticated workspace."""

    return _semantic_registry_scope(settings or get_settings(), workspace_id)


def _control_workspace(settings: Settings, workspace_id: str | None) -> str:
    if workspace_id is not None:
        return workspace_id
    if settings.auth_mode != "local-demo":
        raise DatabaseConfigurationError(
            "managed control-plane composition requires an authenticated workspace"
        )
    return build_streamlit_principal(settings=settings).workspace_id


def build_semantic_planning_context(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
    workspace_id: str | None = None,
) -> GovernedSemanticRegistryPort:
    """Compatibility alias for callers migrating to :func:`build_semantic_registry`."""

    return build_semantic_registry(
        repository_root=repository_root,
        settings=settings,
        workspace_id=workspace_id,
    )


def _connector_secret_directory(
    settings: Settings,
    *,
    repository_root: Path,
) -> Path:
    configured = settings.connector_secret_directory
    if configured is None:
        raise DatabaseConfigurationError(
            "SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY is required for managed connectors"
        )
    directory = configured if configured.is_absolute() else repository_root / configured
    if not directory.is_absolute() or ".." in directory.parts:
        raise DatabaseConfigurationError(
            "SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY must be an absolute lexical path"
        )
    # Preserve the operator-supplied directory entry.  The owner-only resolvers
    # deliberately compare it with ``resolve(strict=True)`` and open it with
    # no-follow semantics; canonicalizing here would erase evidence that the
    # configured capability entered through a symlink.
    return directory


def _build_connector_secret_resolver(
    settings: Settings,
    *,
    repository_root: Path,
) -> "ConnectorSecretResolver":
    if settings.connector_secret_mode == "local":
        from schemabridge.adapters.connectors.local_secrets import (
            OwnerOnlyConnectorSecretResolver,
        )

        return OwnerOnlyConnectorSecretResolver(
            _connector_secret_directory(settings, repository_root=repository_root)
        )
    role = settings.connector_secret_role
    capability = settings.connector_secret_capability
    if role is None or capability is None:
        raise DatabaseConfigurationError("remote connector secret configuration is incomplete")
    from schemabridge.adapters.connectors.remote_secrets import ConnectorSecretCapability

    return _build_remote_secret_backend(
        settings,
        role=role,
        capability=ConnectorSecretCapability(capability),
    )


def _build_remote_secret_backend(
    settings: Settings,
    *,
    role: str,
    capability: "ConnectorSecretCapability",
) -> "VaultKvV2ConnectorSecretResolver":
    from schemabridge.adapters.connectors.remote_secrets import (
        ProjectedServiceAccountIdentity,
        VaultKvV2ConnectorSecretResolver,
    )

    provider = settings.connector_secret_provider_url
    mount = settings.connector_secret_kv_mount
    ca_bundle = settings.connector_secret_ca_bundle
    token_file = settings.workload_identity_token_file
    identity_root = settings.workload_identity_root
    audience = settings.workload_identity_audience
    if any(
        value is None
        for value in (
            provider,
            mount,
            ca_bundle,
            token_file,
            identity_root,
            audience,
        )
    ):
        raise DatabaseConfigurationError("remote connector secret configuration is incomplete")
    assert provider is not None
    assert mount is not None
    assert ca_bundle is not None
    assert token_file is not None
    assert identity_root is not None
    assert audience is not None
    try:
        return VaultKvV2ConnectorSecretResolver(
            server=provider,
            role=role,
            kv_mount=mount,
            capability=capability,
            identity=ProjectedServiceAccountIdentity(
                token_file=token_file,
                mount_root=identity_root,
                audience=audience,
            ),
            ca_bundle=ca_bundle,
            timeout_seconds=settings.connector_secret_timeout_seconds,
        )
    except (OSError, TypeError, ValueError) as error:
        raise DatabaseConfigurationError(
            "remote connector secret configuration is invalid"
        ) from error


def _build_registry_credential_resolver(
    settings: Settings,
) -> "DataHubRegistryCredentialResolver":
    from schemabridge.adapters.connectors.remote_secrets import ConnectorSecretCapability
    from schemabridge.adapters.semantic_registry.remote_secrets import (
        VaultKvV2DataHubRegistryCredentialResolver,
    )
    from schemabridge.application.ports.connector_secrets import OpaqueConnectorSecretRef

    role = settings.semantic_registry_secret_role
    binding_ref = settings.semantic_registry_secret_binding_ref
    version = settings.semantic_registry_secret_version
    if role is None or binding_ref is None or version is None:
        raise DatabaseConfigurationError(
            "remote semantic registry secret configuration is incomplete"
        )
    try:
        return VaultKvV2DataHubRegistryCredentialResolver(
            backend=_build_remote_secret_backend(
                settings,
                role=role,
                capability=ConnectorSecretCapability.REGISTRY,
            ),
            reference=OpaqueConnectorSecretRef(binding_ref),
            version=version,
        )
    except (OSError, TypeError, ValueError) as error:
        raise DatabaseConfigurationError(
            "remote semantic registry secret configuration is invalid"
        ) from error


def _build_registry_publisher_credential_resolver(
    settings: Settings,
) -> "DataHubRegistryWriterCredentialResolver":
    from schemabridge.adapters.connectors.remote_secrets import ConnectorSecretCapability
    from schemabridge.adapters.semantic_registry.remote_secrets import (
        VaultKvV2DataHubRegistryWriterCredentialResolver,
    )
    from schemabridge.application.ports.connector_secrets import OpaqueConnectorSecretRef

    role = settings.registry_publisher_secret_role
    binding_ref = settings.registry_publisher_secret_binding_ref
    version = settings.registry_publisher_secret_version
    if role is None or binding_ref is None or version is None:
        raise DatabaseConfigurationError(
            "remote registry publisher secret configuration is incomplete"
        )
    try:
        return VaultKvV2DataHubRegistryWriterCredentialResolver(
            backend=_build_remote_secret_backend(
                settings,
                role=role,
                capability=ConnectorSecretCapability.REGISTRY_PUBLISHER,
            ),
            reference=OpaqueConnectorSecretRef(binding_ref),
            version=version,
        )
    except (OSError, TypeError, ValueError) as error:
        raise DatabaseConfigurationError(
            "remote registry publisher secret configuration is invalid"
        ) from error


def _build_datahub_catalog_secret_resolver(
    settings: Settings,
    *,
    repository_root: Path,
) -> "DataHubCatalogSecretResolverPort":
    if settings.connector_secret_mode == "local":
        from schemabridge.adapters.catalog.datahub_secrets import (
            OwnerOnlyDataHubCatalogSecretResolver,
        )

        return OwnerOnlyDataHubCatalogSecretResolver(
            _connector_secret_directory(settings, repository_root=repository_root)
        )
    from schemabridge.adapters.catalog.remote_datahub_secrets import (
        VaultKvV2DataHubCatalogSecretResolver,
    )
    from schemabridge.adapters.connectors.remote_secrets import (
        VaultKvV2ConnectorSecretResolver,
    )

    backend = _build_connector_secret_resolver(
        settings,
        repository_root=repository_root,
    )
    if not isinstance(backend, VaultKvV2ConnectorSecretResolver):
        raise DatabaseConfigurationError("remote catalog connector secret configuration is invalid")
    return VaultKvV2DataHubCatalogSecretResolver(backend)


def _build_runtime_connector_preflight(
    settings: Settings,
    *,
    repository_root: Path,
    connection_provider: "ControlConnectionProvider | None",
) -> tuple[ExecutionTargetResolverPort, AssessGovernedQueryCost]:
    from schemabridge.adapters.connectors.postgres_routing import (
        PostgresExecutionTargetResolver,
        PostgresPreflightConnectorRouteReader,
    )
    from schemabridge.adapters.connectors.routed_postgres import (
        RoutedPostgresQueryConnector,
    )

    dsn = _control_plane_dsn(settings, "runtime")
    target_resolver = PostgresExecutionTargetResolver(
        dsn,
        schema=settings.control_plane_schema,
        connection_provider=connection_provider,
    )
    route_reader = PostgresPreflightConnectorRouteReader(
        dsn,
        schema=settings.control_plane_schema,
        connection_provider=connection_provider,
    )
    connector = RoutedPostgresQueryConnector(
        route_reader.load_secret_reference,
        _build_connector_secret_resolver(settings, repository_root=repository_root),
        allowed_fields=frozenset(),
        max_rows_limit=settings.max_query_rows,
        max_timeout_ms=settings.statement_timeout_ms,
        max_rejection_records=settings.max_query_rows,
    )
    return target_resolver, AssessGovernedQueryCost(connector)


def build_governed_request_preparer(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
    registry: GovernedSemanticRegistryPort | None = None,
    workspace_id: str | None = None,
    semantic_gate: AssertSemanticContextCurrent | None = None,
    target_resolver: ExecutionTargetResolverPort | None = None,
    cost_preflight: AssessGovernedQueryCost | None = None,
    control_credential_kind: Literal["runtime", "worker"] | None = None,
    control_connection_provider: "ControlConnectionProvider | None" = None,
) -> PrepareGovernedRequest:
    """Compose semantic resolution, deterministic compilation, and independent guarding."""

    from schemabridge.adapters.sql.compiler import PostgresQueryCompiler

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    active_gate = semantic_gate
    semantic_scope: SemanticRegistryScope | None = None
    active_target_resolver = target_resolver
    active_cost_preflight = cost_preflight
    if (active_target_resolver is None) != (active_cost_preflight is None):
        raise DatabaseConfigurationError(
            "managed target resolution and cost preflight must be configured together"
        )
    if resolved.semantic_registry_selection == "active":
        semantic_scope = _semantic_registry_scope(resolved, workspace_id)
        active_gate = active_gate or build_semantic_change_gate(
            settings=resolved,
            credential_kind=control_credential_kind,
            connection_provider=control_connection_provider,
        )
        if active_target_resolver is None and semantic_gate is None:
            selected_credential = control_credential_kind or (
                "worker" if resolved.runtime_component == "worker" else "runtime"
            )
            if selected_credential == "worker":
                raise DatabaseConfigurationError(
                    "managed worker preparation requires one lease-bound connector target"
                )
            active_target_resolver, active_cost_preflight = _build_runtime_connector_preflight(
                resolved,
                repository_root=root,
                connection_provider=control_connection_provider,
            )
    elif active_gate is not None:
        raise DatabaseConfigurationError(
            "semantic change gating requires authoritative active registry selection"
        )
    return PrepareGovernedRequest(
        planner=build_semantic_request_planner(
            repository_root=root,
            settings=resolved,
            registry=registry,
            workspace_id=workspace_id,
        ),
        compiler=PostgresQueryCompiler(),
        guard=build_sql_guard(),
        semantic_gate=active_gate,
        semantic_scope=semantic_scope,
        target_resolver=active_target_resolver,
        cost_preflight=active_cost_preflight,
    )


def build_semantic_change_gate(
    *,
    settings: Settings | None = None,
    credential_kind: Literal["runtime", "worker"] | None = None,
    connection_provider: "ControlConnectionProvider | None" = None,
) -> AssertSemanticContextCurrent:
    """Compose the minimized exact-evidence gate with a read-only control role."""

    from schemabridge.adapters.semantic_change.postgres_read import (
        PostgresSemanticChangeGateReader,
    )

    resolved = settings or get_settings()
    if (
        resolved.control_plane_kind != "postgres"
        or resolved.semantic_registry_selection != "active"
    ):
        raise DatabaseConfigurationError(
            "semantic change gating requires the active PostgreSQL control plane"
        )
    selected = credential_kind or (
        "worker" if resolved.runtime_component == "worker" else "runtime"
    )
    if selected == "worker" and resolved.runtime_component != "worker":
        raise DatabaseConfigurationError(
            "worker semantic gate requires the isolated worker component"
        )
    if selected == "runtime" and resolved.runtime_component != "web":
        raise DatabaseConfigurationError(
            "runtime semantic gate requires the isolated web component"
        )
    return AssertSemanticContextCurrent(
        PostgresSemanticChangeGateReader(
            dsn=_control_plane_dsn(resolved, selected),
            schema=resolved.control_plane_schema,
            application_name=f"schemabridge-control-{selected}",
            connection_provider=connection_provider,
        )
    )


def build_semantic_request_planner(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
    registry: GovernedSemanticRegistryPort | None = None,
    workspace_id: str | None = None,
) -> PlanSemanticRequest:
    """Compose typed semantic resolution at the sole application composition root."""

    resolved = settings or get_settings()
    return PlanSemanticRequest(
        context=registry
        or build_semantic_registry(
            repository_root=repository_root,
            settings=resolved,
            workspace_id=workspace_id,
        ),
        limits=ResolutionLimits(
            max_tables=resolved.max_query_tables,
            max_preview_rows=resolved.max_query_rows,
            statement_timeout_ms=resolved.statement_timeout_ms,
        ),
    )


def build_governed_request_executor(
    *,
    execution_kind: Literal["live", "recorded", "disabled"] = "live",
    repository_root: Path | None = None,
    settings: Settings | None = None,
    prepare: PrepareGovernedRequest | None = None,
) -> ExecuteGovernedRequest:
    """Compose bounded PostgreSQL preview and rejected-source reporting behind ports."""

    resolved = settings or get_settings()
    root = (repository_root or Path.cwd()).resolve()
    if (
        resolved.runtime_profile in {"staging", "production"}
        and resolved.runtime_component == "web"
        and execution_kind != "disabled"
    ):
        raise DatabaseConfigurationError(
            "managed web governed execution composition requires disabled mode"
        )
    if execution_kind == "recorded":
        from schemabridge.adapters.demo.recorded_execution import RecordedDemoExecutionAdapter

        recorded = RecordedDemoExecutionAdapter(root / "demo/hosted/north_star_execution.json")
        return ExecuteGovernedRequest(
            prepare=prepare
            or build_governed_request_preparer(
                repository_root=root,
                settings=resolved,
            ),
            executor=recorded,
            rejection_reporter=recorded,
        )
    active_prepare = prepare or build_governed_request_preparer(
        repository_root=repository_root,
        settings=resolved,
    )
    if execution_kind == "disabled" or active_prepare.target_resolver is not None:
        from schemabridge.adapters.connectors.routed_postgres import (
            WorkerOnlyManagedQueryConnector,
        )

        disabled = WorkerOnlyManagedQueryConnector()
        return ExecuteGovernedRequest(
            prepare=active_prepare,
            executor=disabled,
            rejection_reporter=disabled,
        )
    if not resolved.database_url:
        raise DatabaseConfigurationError("DATABASE_URL is required for governed preview")
    from schemabridge.adapters.postgres.preview import PsycopgQueryPreview
    from schemabridge.adapters.postgres.rejections import PsycopgRejectedSourceReporter

    active_registry = active_prepare.planner.context.load().registry
    rejection_fields = frozenset(
        key.physical_field.root
        for contract in active_registry.join_contracts.contracts
        for key in (contract.left_key, contract.right_key)
    )
    return ExecuteGovernedRequest(
        prepare=active_prepare,
        executor=PsycopgQueryPreview(
            resolved.database_url,
            expected_user=resolved.postgres_reader_user,
            max_rows_limit=resolved.max_query_rows,
            max_timeout_ms=resolved.statement_timeout_ms,
        ),
        rejection_reporter=PsycopgRejectedSourceReporter(
            resolved.database_url,
            allowed_fields=rejection_fields,
            expected_user=resolved.postgres_reader_user,
            max_records=resolved.max_query_rows,
        ),
    )


def _build_postgres_workflow_access_store(
    settings: Settings,
    *,
    credential_kind: Literal["runtime", "api", "worker"] = "runtime",
    connection_provider: "ControlConnectionProvider | None" = None,
) -> "PostgresWorkflowAccessStore":
    from schemabridge.adapters.storage.postgres import PostgresWorkflowAccessStore

    return PostgresWorkflowAccessStore(
        _control_plane_dsn(settings, credential_kind),
        schema=settings.control_plane_schema,
        application_name=f"schemabridge-control-{credential_kind}",
        connection_provider=connection_provider,
    )


def _build_postgres_workflow_draft_store(
    settings: Settings,
    *,
    workspace_id: str,
    owner_actor_id: str,
    credential_kind: Literal["runtime", "api", "worker"] = "runtime",
    connection_provider: "ControlConnectionProvider | None" = None,
) -> "PostgresWorkflowDraftStore":
    from schemabridge.adapters.storage.postgres import PostgresWorkflowDraftStore

    return PostgresWorkflowDraftStore(
        _control_plane_dsn(settings, credential_kind),
        workspace_id=workspace_id,
        owner_actor_id=owner_actor_id,
        schema=settings.control_plane_schema,
        application_name=f"schemabridge-control-{credential_kind}",
        connection_provider=connection_provider,
    )


def _require_initialized_oidc_identity(
    *,
    resolver: "PostgresIdentityRotationStore",
    workspace_id: str | None,
    actor_id: str | None,
) -> tuple[str, str]:
    from schemabridge.application.ports.identity_rotation import IdentityRotationStoreError

    if workspace_id is None or actor_id is None:
        raise DatabaseConfigurationError(
            "managed OIDC workflow composition requires authenticated workspace and owner"
        )
    try:
        scopes = resolver.resolve_authorization_scopes(workspace_id, actor_id)
    except IdentityRotationStoreError as error:
        raise DatabaseConfigurationError(
            "authenticated OIDC identity is not initialized in the control plane"
        ) from error
    if not any(
        scope.workspace_id == workspace_id and scope.actor_id == actor_id for scope in scopes
    ):
        raise DatabaseConfigurationError(
            "authenticated OIDC identity is not initialized in the control plane"
        )
    return workspace_id, actor_id


def build_workflow_access_store(
    *,
    workspace_id: str | None = None,
    owner_actor_id: str | None = None,
    settings: Settings | None = None,
) -> WorkflowAccessStorePort:
    """Compose exact local access or verified same-lineage PostgreSQL access."""

    resolved = settings or get_settings()
    if resolved.control_plane_kind == "postgres":
        raw = _build_postgres_workflow_access_store(resolved)
        if resolved.auth_mode != "oidc":
            return raw
        from schemabridge.adapters.storage.identity_resolving import (
            IdentityResolvingWorkflowAccessStore,
        )

        resolver = build_identity_rotation_store(settings=resolved)
        _require_initialized_oidc_identity(
            resolver=resolver,
            workspace_id=workspace_id,
            actor_id=owner_actor_id,
        )
        return IdentityResolvingWorkflowAccessStore(raw, resolver)
    from schemabridge.adapters.storage.workflow_access import SqliteWorkflowAccessStore

    return SqliteWorkflowAccessStore(resolved.draft_store_path.resolve())


def build_workflow_draft_store(
    *,
    workspace_id: str | None = None,
    owner_actor_id: str | None = None,
    settings: Settings | None = None,
) -> WorkflowDraftStorePort:
    """Compose exact local drafts or verified same-lineage PostgreSQL drafts."""

    resolved = settings or get_settings()
    if resolved.control_plane_kind == "postgres":
        if workspace_id is None or owner_actor_id is None:
            raise DatabaseConfigurationError(
                "managed workflow composition requires authenticated workspace and owner"
            )
        active = _build_postgres_workflow_draft_store(
            resolved,
            workspace_id=workspace_id,
            owner_actor_id=owner_actor_id,
        )
        if resolved.auth_mode != "oidc":
            return active
        from schemabridge.adapters.storage.identity_resolving import (
            IdentityResolvingWorkflowDraftStore,
        )

        resolver = build_identity_rotation_store(settings=resolved)
        active_workspace_id, active_actor_id = _require_initialized_oidc_identity(
            resolver=resolver,
            workspace_id=workspace_id,
            actor_id=owner_actor_id,
        )
        return IdentityResolvingWorkflowDraftStore(
            active_store=active,
            resolver=resolver,
            workspace_id=active_workspace_id,
            actor_id=active_actor_id,
            store_factory=lambda historical_workspace_id, historical_actor_id: (
                _build_postgres_workflow_draft_store(
                    resolved,
                    workspace_id=historical_workspace_id,
                    owner_actor_id=historical_actor_id,
                )
            ),
        )
    from schemabridge.adapters.storage.workflows import SqliteWorkflowDraftStore

    return SqliteWorkflowDraftStore(
        resolved.draft_store_path.resolve(),
        workspace_id=workspace_id,
        owner_actor_id=owner_actor_id,
    )


def build_agent_workflow_orchestrator(
    catalog_kind: Literal["live", "recorded"] = "recorded",
    *,
    publication_kind: Literal["live", "fake", "disabled"] = "fake",
    execution_kind: Literal["live", "recorded", "disabled"] = "live",
    workflow_workspace_id: str | None = None,
    workflow_owner_actor_id: str | None = None,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> AgentWorkflowOrchestrator:
    """Compose durable orchestration with explicit catalog/publication/recipe adapters."""

    from schemabridge.adapters.workflows.fake import SqliteFakeWorkflowPublisher
    from schemabridge.adapters.workflows.system import SystemWorkflowClock

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    if (
        resolved.runtime_profile in {"staging", "production"}
        and resolved.runtime_component == "web"
        and (publication_kind != "disabled" or execution_kind != "disabled")
    ):
        raise DatabaseConfigurationError(
            "managed web workflow composition requires disabled execution and publication"
        )
    registry = build_semantic_registry(
        repository_root=root,
        settings=resolved,
        workspace_id=workflow_workspace_id,
    )
    prepare = build_governed_request_preparer(
        repository_root=root,
        settings=resolved,
        registry=registry,
        workspace_id=workflow_workspace_id,
    )
    store = build_workflow_draft_store(
        workspace_id=workflow_workspace_id,
        owner_actor_id=workflow_owner_actor_id,
        settings=resolved,
    )
    publisher: WorkflowPublicationPort
    if publication_kind == "live":
        from schemabridge.adapters.datahub.workflow_publication import (
            DataHubWorkflowPublicationAdapter,
        )

        publisher = DataHubWorkflowPublicationAdapter.from_env_file(
            root / ".local/datahub/writer.env"
        )
    elif publication_kind == "fake":
        if resolved.runtime_profile in {"staging", "production"}:
            raise DatabaseConfigurationError(
                "fake workflow publication is available only with the local control plane"
            )
        publisher = SqliteFakeWorkflowPublisher(resolved.draft_store_path.resolve())
    else:
        from schemabridge.adapters.workflows.read_only import DisabledWorkflowPublisher

        publisher = DisabledWorkflowPublisher()
    recipes = (
        None
        if publication_kind == "disabled"
        else build_query_recipe_repository(
            publication_kind,
            repository_root=root,
            settings=resolved,
        )
    )
    return AgentWorkflowOrchestrator(
        store=store,
        clock=SystemWorkflowClock(),
        catalog=build_catalog_reader(catalog_kind, repository_root=root),
        intent=build_natural_language_intent_resolver(
            "fake",
            repository_root=root,
            settings=resolved,
            registry=registry,
        ),
        prepare=prepare,
        execute=build_governed_request_executor(
            execution_kind=execution_kind,
            repository_root=root,
            settings=resolved,
            prepare=prepare,
        ),
        publisher=publisher,
        audit_store=build_publication_audit_store(
            resolved,
            workspace_id=workflow_workspace_id,
        ),
        recipe_assessor=(
            None
            if recipes is None
            else AssessQueryRecipeReuse(
                recipes,
                semantic_gate=prepare.semantic_gate,
                semantic_scope=prepare.semantic_scope,
            )
        ),
    )


def build_background_job_store(
    *,
    credential_kind: Literal["api", "worker"],
    settings: Settings | None = None,
    connection_provider: "ControlConnectionProvider | None" = None,
) -> BackgroundJobStorePort:
    """Compose the queue only through one dedicated least-privilege role."""

    from schemabridge.adapters.control_plane.postgres_jobs import (
        PostgresBackgroundJobStore,
    )

    resolved = settings or get_settings()
    if resolved.control_plane_kind != "postgres":
        raise DatabaseConfigurationError("background jobs require the PostgreSQL control plane")
    return PostgresBackgroundJobStore(
        dsn=_control_plane_dsn(resolved, credential_kind),
        schema=resolved.control_plane_schema,
        application_name=f"schemabridge-control-{credential_kind}",
        connection_provider=connection_provider,
    )


def build_tenant_capacity_policy_operator(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
    control_connection_provider: "ControlConnectionProvider | None" = None,
) -> "ApplyTenantCapacityPolicy":
    """Compose the explicit migrator-only tenant-capacity operator."""

    from schemabridge.adapters.catalog.postgres_inventory import (
        PostgresTenantCapacityPolicyOperator,
    )
    from schemabridge.application.catalog_inventory import ApplyTenantCapacityPolicy

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    if resolved.control_plane_kind != "postgres":
        raise DatabaseConfigurationError(
            "tenant capacity policy operations require the PostgreSQL control plane"
        )
    build_control_plane_migrator(
        credential_kind="migrator",
        repository_root=root,
        settings=resolved,
        connection_provider=control_connection_provider,
    ).require_current()
    return ApplyTenantCapacityPolicy(
        PostgresTenantCapacityPolicyOperator(
            dsn=_control_plane_dsn(resolved, "migrator"),
            schema=resolved.control_plane_schema,
            connection_provider=control_connection_provider,
        )
    )


def build_tenant_ai_policy_operator(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
    control_connection_provider: "ControlConnectionProvider | None" = None,
) -> "TenantAiPolicyOperator":
    """Compose the exact inspect/prepare/apply tenant-AI policy operator."""

    from schemabridge.adapters.control_plane.postgres_query_studio_policy import (
        PostgresTenantAiPolicyOperator,
    )
    from schemabridge.application.query_studio_ai_policy import TenantAiPolicyOperator

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    if resolved.control_plane_kind != "postgres":
        raise DatabaseConfigurationError(
            "tenant AI policy operations require the PostgreSQL control plane"
        )
    build_control_plane_migrator(
        credential_kind="migrator",
        repository_root=root,
        settings=resolved,
        connection_provider=control_connection_provider,
    ).require_current()
    return TenantAiPolicyOperator(
        PostgresTenantAiPolicyOperator(
            dsn=_control_plane_dsn(resolved, "migrator"),
            schema=resolved.control_plane_schema,
            connection_provider=control_connection_provider,
        )
    )


def build_connector_route_operator(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
    control_connection_provider: "ControlConnectionProvider | None" = None,
) -> "ConnectorRouteOperator":
    """Compose the migrator-only inspect/prepare/approve/apply route operator."""

    from schemabridge.adapters.connectors.postgres_route_operator import (
        PostgresConnectorRouteOperator,
    )
    from schemabridge.application.connector_route_operator import ConnectorRouteOperator

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    if resolved.control_plane_kind != "postgres":
        raise DatabaseConfigurationError(
            "connector route operations require the PostgreSQL control plane"
        )
    build_control_plane_migrator(
        credential_kind="migrator",
        repository_root=root,
        settings=resolved,
        connection_provider=control_connection_provider,
    ).require_current()
    return ConnectorRouteOperator(
        PostgresConnectorRouteOperator(
            dsn=_control_plane_dsn(resolved, "migrator"),
            schema=resolved.control_plane_schema,
            connection_provider=control_connection_provider,
        )
    )


def build_api_http_services(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
    control_connection_provider: "ControlConnectionProvider | None" = None,
) -> "ApiHttpServices":
    """Compose the API without source, LLM, or DataHub writer adapters."""

    from schemabridge.adapters.catalog.cursor import SignedInventoryCursorCodec
    from schemabridge.adapters.catalog.postgres_inventory import (
        PostgresCatalogConnectionStore,
        PostgresCatalogInventoryReader,
        PostgresTenantCapacityStore,
    )
    from schemabridge.adapters.catalog.postgres_refresh import (
        PostgresCatalogRefreshStore,
    )
    from schemabridge.adapters.catalog.postgres_registry_publication import (
        PostgresRegistryPhysicalBindingAuthority,
    )
    from schemabridge.adapters.catalog.postgres_semantic_onboarding import (
        PostgresSemanticOnboardingCatalogEvidence,
    )
    from schemabridge.adapters.connectors.postgres_routing import (
        PostgresExecutionTargetResolver,
    )
    from schemabridge.adapters.control_plane.postgres_active_registry import (
        PostgresActiveRegistryPointerReader,
    )
    from schemabridge.adapters.control_plane.postgres_active_registry_version import (
        PostgresActiveRegistryVersionReader,
    )
    from schemabridge.adapters.control_plane.postgres_registry_publication import (
        PostgresRegistryPublicationJobStore,
        PostgresRegistryPublicationProposalReader,
    )
    from schemabridge.adapters.semantic_change.cursor import (
        SignedSemanticChangeCursorCodec,
    )
    from schemabridge.adapters.semantic_change.postgres_dependencies import (
        PostgresSemanticChangeDependencyIndex,
    )
    from schemabridge.adapters.semantic_change.postgres_read import (
        PostgresSemanticChangeReadStore,
    )
    from schemabridge.adapters.semantic_onboarding.registry_base import (
        AuthoritativeSemanticOnboardingRegistryBaseReader,
    )
    from schemabridge.adapters.storage.postgres_registry_changes import (
        PostgresRegistryChangeStore,
        PostgresRegistryJoinProfileRequestQueue,
    )
    from schemabridge.adapters.storage.postgres_registry_model_changes import (
        PostgresRegistryModelChangeStore,
        PostgresRegistryModelJoinProfileQueue,
        PostgresRegistryModelJoinProfileWitnessReader,
        PostgresRegistryModelProfileStore,
        PostgresRegistryModelRemediationEvidenceReader,
        PostgresRegistryModelReplacementSourceReader,
    )
    from schemabridge.adapters.storage.postgres_semantic_onboarding import (
        PostgresSemanticOnboardingStore,
    )
    from schemabridge.adapters.workflows.read_only import ReadOnlyWorkflowInspector
    from schemabridge.adapters.workflows.system import SystemWorkflowClock
    from schemabridge.application.api_capacity import AdmitAuthenticatedApiRequest
    from schemabridge.application.api_workflows import WorkflowOrchestratorPort
    from schemabridge.application.catalog_inventory import (
        DisableCatalogConnection,
        InspectCatalogRefresh,
        ListCatalogAssets,
        ListCatalogConnections,
        ListCatalogFields,
        RegisterCatalogConnection,
        RequestCatalogRefresh,
    )
    from schemabridge.application.ports.semantic_onboarding import (
        SemanticOnboardingStorePort,
    )
    from schemabridge.application.ports.workflow_access import WorkflowAccessStorePort
    from schemabridge.application.registry_change_authorization import (
        RegistryChangeAuthorizationPolicy,
    )
    from schemabridge.application.registry_changes import (
        DecideRegistryJoinChange,
        FinalizeRegistryJoinChangeDraft,
        InspectRegistryJoinChange,
        ListRegistryJoinChanges,
        PrepareRegistryJoinChangePublication,
        RequestRegistryJoinProfile,
    )
    from schemabridge.application.registry_model_changes import (
        CreateRegistryModelChange,
        DecideRegistryModelChange,
        FinalizeRegistryModelJoinProfile,
        InspectRegistryModelChange,
        ListRegistryModelChanges,
        PrepareRegistryModelChangePublication,
        RequestRegistryModelJoinProfile,
    )
    from schemabridge.application.registry_publication import (
        AuthorizeRegistryPublication,
        CancelRegistryPublication,
        InspectRegistryPublication,
        SubmitRegistryPublication,
    )
    from schemabridge.application.semantic_change_read import (
        InspectSemanticChangeReport,
        ListSemanticChangeFindings,
        ListSemanticChangeImpacts,
        ListSemanticChangeReports,
    )
    from schemabridge.application.semantic_onboarding import (
        CreateSemanticOnboardingDraft,
        DecideSemanticOnboarding,
        InspectSemanticOnboardingDraft,
        ListSemanticOnboardingDrafts,
        PreflightSemanticOnboardingDraft,
        PrepareSemanticOnboardingPublication,
        SemanticOnboardingSnapshot,
    )
    from schemabridge.application.semantic_onboarding_authorization import (
        SemanticOnboardingAuthorizationPolicy,
    )
    from schemabridge.domain.decisions import DecisionAction
    from schemabridge.domain.registry_change_authoring import (
        RegistryJoinChangeSnapshot,
        RegistryJoinDraftMutation,
        RegistryJoinPreparation,
        RegistryJoinProfileAuthoringMutation,
        RegistryJoinProfileAuthoringRequest,
        RequestRegistryJoinProfileInput,
    )
    from schemabridge.domain.registry_model_change_authoring import (
        CreateRegistryModelChangeInput,
        RegistryModelChangeDraft,
        RegistryModelChangeMutation,
        RegistryModelChangeSnapshot,
        RegistryModelJoinProfileMutation,
        RequestRegistryModelJoinProfileInput,
    )
    from schemabridge.domain.semantic_onboarding import (
        CreateSemanticOnboardingRequest,
        OnboardingEvidence,
        PreflightSemanticOnboardingRequest,
        SemanticOnboardingDraft,
        SemanticOnboardingDraftMutation,
        SemanticOnboardingPreflight,
        SemanticOnboardingPreparation,
        SemanticOnboardingTargetKind,
    )
    from schemabridge.entrypoints.http.app import (
        ApiHttpServices,
        CatalogHttpServices,
        RegistryChangeHttpServices,
        RegistryModelChangeHttpServices,
        RegistryPublicationHttpServices,
        SemanticChangeHttpServices,
        SemanticOnboardingHttpServices,
    )

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    if resolved.runtime_component != "api":
        raise DatabaseConfigurationError(
            "authenticated API composition requires SCHEMABRIDGE_COMPONENT=api"
        )
    require_current_control_plane_schema(
        credential_kind="api",
        repository_root=root,
        settings=resolved,
    )
    raw_job_store = build_background_job_store(
        credential_kind="api",
        settings=resolved,
        connection_provider=control_connection_provider,
    )
    clock = SystemWorkflowClock()
    if resolved.inventory_cursor_signing_key is None:
        raise DatabaseConfigurationError(
            "authenticated catalog API requires SCHEMABRIDGE_INVENTORY_CURSOR_SIGNING_KEY"
        )
    catalog_connections = PostgresCatalogConnectionStore(
        dsn=_control_plane_dsn(resolved, "api"),
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-api",
        connection_provider=control_connection_provider,
        stale_after_seconds=resolved.catalog_stale_after_seconds,
    )
    catalog_inventory = PostgresCatalogInventoryReader(
        dsn=_control_plane_dsn(resolved, "api"),
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-api",
        connection_provider=control_connection_provider,
    )
    catalog_refreshes = PostgresCatalogRefreshStore(
        dsn=_control_plane_dsn(resolved, "api"),
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-api",
        connection_provider=control_connection_provider,
    )
    inventory_cursors = SignedInventoryCursorCodec(
        signing_key=resolved.inventory_cursor_signing_key.get_secret_value().encode("utf-8")
    )
    semantic_change_cursors = SignedSemanticChangeCursorCodec(
        signing_key=resolved.inventory_cursor_signing_key.get_secret_value().encode("utf-8")
    )
    semantic_change_store = PostgresSemanticChangeReadStore(
        dsn=_control_plane_dsn(resolved, "api"),
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-api",
        connection_provider=control_connection_provider,
    )
    onboarding_store = PostgresSemanticOnboardingStore(
        dsn=_control_plane_dsn(resolved, "api"),
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-api",
        stale_after_seconds=resolved.catalog_stale_after_seconds,
        connection_provider=control_connection_provider,
    )
    registry_publication_jobs = PostgresRegistryPublicationJobStore(
        dsn=_control_plane_dsn(resolved, "api"),
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-api",
        connection_provider=control_connection_provider,
    )
    registry_publication_proposals = PostgresRegistryPublicationProposalReader(
        dsn=_control_plane_dsn(resolved, "api"),
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-api",
        connection_provider=control_connection_provider,
    )
    onboarding_catalog = PostgresSemanticOnboardingCatalogEvidence(
        dsn=_control_plane_dsn(resolved, "api"),
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-api",
        stale_after_seconds=resolved.catalog_stale_after_seconds,
        connection_provider=control_connection_provider,
    )
    api_control_dsn = _control_plane_dsn(resolved, "api")
    active_registry_pointers = PostgresActiveRegistryPointerReader(
        dsn=api_control_dsn,
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-api",
        connection_provider=control_connection_provider,
    )
    onboarding_registry_bases = AuthoritativeSemanticOnboardingRegistryBaseReader(
        pointers=active_registry_pointers
    )
    registry_versions = PostgresActiveRegistryVersionReader(
        dsn=api_control_dsn,
        pointers=active_registry_pointers,
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-api",
        connection_provider=control_connection_provider,
    )
    registry_change_store = PostgresRegistryChangeStore(
        dsn=api_control_dsn,
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-api",
        connection_provider=control_connection_provider,
    )
    registry_join_profiles = PostgresRegistryJoinProfileRequestQueue(
        dsn=api_control_dsn,
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-api",
        connection_provider=control_connection_provider,
    )
    registry_change_targets = PostgresExecutionTargetResolver(
        api_control_dsn,
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-api",
        connection_provider=control_connection_provider,
    )
    registry_change_bindings = PostgresRegistryPhysicalBindingAuthority(
        dsn=api_control_dsn,
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-api",
        stale_after_seconds=resolved.catalog_stale_after_seconds,
        connection_provider=control_connection_provider,
    )
    registry_model_changes = PostgresRegistryModelChangeStore(
        dsn=api_control_dsn,
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-api",
        connection_provider=control_connection_provider,
    )
    registry_model_profiles = PostgresRegistryModelProfileStore(
        dsn=api_control_dsn,
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-api",
        connection_provider=control_connection_provider,
    )
    registry_model_profile_queue = PostgresRegistryModelJoinProfileQueue(
        dsn=api_control_dsn,
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-api",
        connection_provider=control_connection_provider,
    )
    registry_model_sources = PostgresRegistryModelReplacementSourceReader(
        dsn=api_control_dsn,
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-api",
        connection_provider=control_connection_provider,
    )
    registry_model_remediation = PostgresRegistryModelRemediationEvidenceReader(
        dsn=api_control_dsn,
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-api",
        connection_provider=control_connection_provider,
    )
    registry_model_witnesses = PostgresRegistryModelJoinProfileWitnessReader(
        dsn=api_control_dsn,
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-api",
        connection_provider=control_connection_provider,
    )
    registry_model_dependencies = PostgresSemanticChangeDependencyIndex(
        dsn=api_control_dsn,
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-api",
        connection_provider=control_connection_provider,
    )
    identity_resolver = (
        build_identity_binding_resolver(
            credential_kind="api",
            settings=resolved,
            connection_provider=control_connection_provider,
        )
        if resolved.auth_mode == "oidc"
        else None
    )
    onboarding_authorization = SemanticOnboardingAuthorizationPolicy(identity_resolver)
    registry_change_authorization = RegistryChangeAuthorizationPolicy(identity_resolver)

    def onboarding_store_for(
        principal: AuthenticatedPrincipal,
    ) -> SemanticOnboardingStorePort:
        if identity_resolver is None:
            return onboarding_store
        from schemabridge.adapters.storage.identity_resolving_semantic_onboarding import (
            IdentityResolvingSemanticOnboardingStore,
        )

        return IdentityResolvingSemanticOnboardingStore(
            onboarding_store,
            identity_resolver,
            workspace_id=principal.workspace_id,
            actor_id=principal.actor_id,
        )

    def onboarding_scope(principal: AuthenticatedPrincipal) -> SemanticRegistryScope:
        return SemanticRegistryScope(
            workspace_id=principal.workspace_id,
            catalog_scope=resolved.semantic_registry_catalog_scope,
            registry_id=resolved.semantic_registry_id,
        )

    class TenantSemanticOnboardingPreflight:
        """Bind the read-only authoring context to authenticated tenant authority."""

        def execute(
            self,
            principal: AuthenticatedPrincipal,
            request: PreflightSemanticOnboardingRequest,
        ) -> SemanticOnboardingPreflight:
            scope = onboarding_scope(principal)
            return PreflightSemanticOnboardingDraft(
                preflights=onboarding_catalog,
                authorization=onboarding_authorization,
                clock=clock,
                scope=scope,
            ).execute(principal, request)

    class TenantSemanticOnboardingDraftCreator:
        """Bind authority scope from the principal and trusted process configuration."""

        def execute(
            self,
            principal: AuthenticatedPrincipal,
            request: CreateSemanticOnboardingRequest,
            *,
            idempotency_key: str,
        ) -> SemanticOnboardingDraftMutation:
            scope = onboarding_scope(principal)
            return CreateSemanticOnboardingDraft(
                store=onboarding_store_for(principal),
                catalog=onboarding_catalog,
                registry_bases=onboarding_registry_bases,
                authorization=onboarding_authorization,
                clock=clock,
                scope=scope,
            ).execute(
                principal,
                request,
                idempotency_key=idempotency_key,
            )

    class TenantSemanticOnboardingDraftLister:
        """Resolve historical draft aliases only for the authenticated principal."""

        def execute(
            self,
            principal: AuthenticatedPrincipal,
            *,
            limit: int = 50,
        ) -> tuple[SemanticOnboardingDraft, ...]:
            return ListSemanticOnboardingDrafts(
                store=onboarding_store_for(principal),
                authorization=onboarding_authorization,
                clock=clock,
            ).execute(principal, limit=limit)

    class TenantSemanticOnboardingDraftInspector:
        """Inspect one exact current or verified historical onboarding draft."""

        def execute(
            self,
            principal: AuthenticatedPrincipal,
            draft_id: str,
            *,
            history_limit: int = 25,
        ) -> SemanticOnboardingSnapshot:
            return InspectSemanticOnboardingDraft(
                store=onboarding_store_for(principal),
                authorization=onboarding_authorization,
                clock=clock,
            ).execute(principal, draft_id, history_limit=history_limit)

    class TenantSemanticOnboardingDecider:
        """Record a decision against one unique same-lineage draft coordinate."""

        def execute(
            self,
            principal: AuthenticatedPrincipal,
            draft_id: str,
            *,
            target_kind: SemanticOnboardingTargetKind,
            target_id: str,
            action: DecisionAction,
            expected_revision: int,
            confirmed_draft_fingerprint: str,
            rationale: str,
            evidence: tuple[OnboardingEvidence, ...],
            idempotency_key: str,
        ) -> SemanticOnboardingDraftMutation:
            return DecideSemanticOnboarding(
                store=onboarding_store_for(principal),
                catalog=onboarding_catalog,
                registry_bases=onboarding_registry_bases,
                authorization=onboarding_authorization,
                clock=clock,
            ).execute(
                principal,
                draft_id,
                target_kind=target_kind,
                target_id=target_id,
                action=action,
                expected_revision=expected_revision,
                confirmed_draft_fingerprint=confirmed_draft_fingerprint,
                rationale=rationale,
                evidence=evidence,
                idempotency_key=idempotency_key,
            )

    class TenantSemanticOnboardingPublicationPreparer:
        """Prepare one unique same-lineage draft without rewriting its scope."""

        def execute(
            self,
            principal: AuthenticatedPrincipal,
            draft_id: str,
            *,
            expected_revision: int,
            confirmed_draft_fingerprint: str,
            idempotency_key: str,
        ) -> SemanticOnboardingPreparation:
            return PrepareSemanticOnboardingPublication(
                store=onboarding_store_for(principal),
                catalog=onboarding_catalog,
                registry_bases=onboarding_registry_bases,
                authorization=onboarding_authorization,
                clock=clock,
            ).execute(
                principal,
                draft_id,
                expected_revision=expected_revision,
                confirmed_draft_fingerprint=confirmed_draft_fingerprint,
                idempotency_key=idempotency_key,
            )

    class TenantRegistryJoinProfileRequester:
        """Bind a new join-profile request to the authenticated tenant scope."""

        def execute(
            self,
            principal: AuthenticatedPrincipal,
            request: RequestRegistryJoinProfileInput,
            *,
            idempotency_key: str,
        ) -> RegistryJoinProfileAuthoringMutation:
            return RequestRegistryJoinProfile(
                store=registry_change_store,
                pointers=active_registry_pointers,
                versions=registry_versions,
                targets=registry_change_targets,
                physical_bindings=registry_change_bindings,
                profiles=registry_join_profiles,
                authorization=registry_change_authorization,
                clock=clock,
                scope=onboarding_scope(principal),
            ).execute(
                principal,
                request,
                idempotency_key=idempotency_key,
            )

    class TenantRegistryJoinChangeLister:
        def execute(
            self,
            principal: AuthenticatedPrincipal,
            *,
            limit: int = 50,
        ) -> tuple[RegistryJoinProfileAuthoringRequest, ...]:
            return ListRegistryJoinChanges(
                store=registry_change_store,
                authorization=registry_change_authorization,
                clock=clock,
            ).execute(principal, limit=limit)

    class TenantRegistryJoinChangeInspector:
        def execute(
            self,
            principal: AuthenticatedPrincipal,
            change_id: str,
            *,
            history_limit: int = 25,
        ) -> RegistryJoinChangeSnapshot:
            return InspectRegistryJoinChange(
                store=registry_change_store,
                authorization=registry_change_authorization,
                clock=clock,
            ).execute(principal, change_id, history_limit=history_limit)

    class TenantRegistryJoinDraftFinalizer:
        def execute(
            self,
            principal: AuthenticatedPrincipal,
            change_id: str,
            *,
            confirmed_authoring_fingerprint: str,
            idempotency_key: str,
        ) -> RegistryJoinDraftMutation:
            return FinalizeRegistryJoinChangeDraft(
                store=registry_change_store,
                pointers=active_registry_pointers,
                versions=registry_versions,
                targets=registry_change_targets,
                physical_bindings=registry_change_bindings,
                profiles=registry_join_profiles,
                authorization=registry_change_authorization,
                clock=clock,
            ).execute(
                principal,
                change_id,
                confirmed_authoring_fingerprint=confirmed_authoring_fingerprint,
                idempotency_key=idempotency_key,
            )

    class TenantRegistryJoinDecider:
        def execute(
            self,
            principal: AuthenticatedPrincipal,
            change_id: str,
            *,
            action: DecisionAction,
            expected_revision: int,
            confirmed_draft_fingerprint: str,
            rationale: str,
            idempotency_key: str,
        ) -> RegistryJoinDraftMutation:
            return DecideRegistryJoinChange(
                store=registry_change_store,
                pointers=active_registry_pointers,
                versions=registry_versions,
                targets=registry_change_targets,
                physical_bindings=registry_change_bindings,
                profiles=registry_join_profiles,
                authorization=registry_change_authorization,
                clock=clock,
            ).execute(
                principal,
                change_id,
                action=action,
                expected_revision=expected_revision,
                confirmed_draft_fingerprint=confirmed_draft_fingerprint,
                rationale=rationale,
                idempotency_key=idempotency_key,
            )

    class TenantRegistryJoinPublicationPreparer:
        def execute(
            self,
            principal: AuthenticatedPrincipal,
            change_id: str,
            *,
            expected_revision: int,
            confirmed_draft_fingerprint: str,
            idempotency_key: str,
        ) -> RegistryJoinPreparation:
            return PrepareRegistryJoinChangePublication(
                store=registry_change_store,
                pointers=active_registry_pointers,
                versions=registry_versions,
                targets=registry_change_targets,
                physical_bindings=registry_change_bindings,
                profiles=registry_join_profiles,
                authorization=registry_change_authorization,
                clock=clock,
            ).execute(
                principal,
                change_id,
                expected_revision=expected_revision,
                confirmed_draft_fingerprint=confirmed_draft_fingerprint,
                idempotency_key=idempotency_key,
            )

    class TenantRegistryModelProfileRequester:
        def execute(
            self,
            principal: AuthenticatedPrincipal,
            request: RequestRegistryModelJoinProfileInput,
            *,
            idempotency_key: str,
        ) -> RegistryModelJoinProfileMutation:
            return RequestRegistryModelJoinProfile(
                store=registry_model_profiles,
                queue=registry_model_profile_queue,
                sources=registry_model_sources,
                pointers=active_registry_pointers,
                versions=registry_versions,
                dependencies=registry_model_dependencies,
                targets=registry_change_targets,
                physical_bindings=registry_change_bindings,
                authorization=registry_change_authorization,
                clock=clock,
                scope=onboarding_scope(principal),
            ).execute(principal, request, idempotency_key=idempotency_key)

    class TenantRegistryModelProfileFinalizer:
        def execute(
            self,
            principal: AuthenticatedPrincipal,
            request_id: str,
            *,
            confirmed_authoring_fingerprint: str,
            idempotency_key: str,
        ) -> RegistryModelJoinProfileMutation:
            return FinalizeRegistryModelJoinProfile(
                store=registry_model_profiles,
                queue=registry_model_profile_queue,
                sources=registry_model_sources,
                pointers=active_registry_pointers,
                versions=registry_versions,
                dependencies=registry_model_dependencies,
                targets=registry_change_targets,
                physical_bindings=registry_change_bindings,
                authorization=registry_change_authorization,
                clock=clock,
            ).execute(
                principal,
                request_id,
                confirmed_authoring_fingerprint=confirmed_authoring_fingerprint,
                idempotency_key=idempotency_key,
            )

    class TenantRegistryModelChangeCreator:
        def execute(
            self,
            principal: AuthenticatedPrincipal,
            request: CreateRegistryModelChangeInput,
            *,
            idempotency_key: str,
        ) -> RegistryModelChangeMutation:
            return CreateRegistryModelChange(
                store=registry_model_changes,
                sources=registry_model_sources,
                remediation=registry_model_remediation,
                profile_witnesses=registry_model_witnesses,
                pointers=active_registry_pointers,
                versions=registry_versions,
                dependencies=registry_model_dependencies,
                targets=registry_change_targets,
                physical_bindings=registry_change_bindings,
                authorization=registry_change_authorization,
                clock=clock,
                scope=onboarding_scope(principal),
            ).execute(principal, request, idempotency_key=idempotency_key)

    class TenantRegistryModelChangeLister:
        def execute(
            self,
            principal: AuthenticatedPrincipal,
            *,
            limit: int = 50,
        ) -> tuple[RegistryModelChangeDraft, ...]:
            return ListRegistryModelChanges(
                store=registry_model_changes,
                authorization=registry_change_authorization,
                clock=clock,
            ).execute(principal, limit=limit)

    class TenantRegistryModelChangeInspector:
        def execute(
            self,
            principal: AuthenticatedPrincipal,
            change_id: str,
            *,
            history_limit: int = 25,
        ) -> RegistryModelChangeSnapshot:
            return InspectRegistryModelChange(
                store=registry_model_changes,
                authorization=registry_change_authorization,
                clock=clock,
            ).execute(principal, change_id, history_limit=history_limit)

    class TenantRegistryModelChangeDecider:
        def execute(
            self,
            principal: AuthenticatedPrincipal,
            change_id: str,
            *,
            action: DecisionAction,
            expected_revision: int,
            confirmed_draft_fingerprint: str,
            rationale: str,
            idempotency_key: str,
        ) -> RegistryModelChangeMutation:
            return DecideRegistryModelChange(
                store=registry_model_changes,
                sources=registry_model_sources,
                remediation=registry_model_remediation,
                profile_witnesses=registry_model_witnesses,
                pointers=active_registry_pointers,
                versions=registry_versions,
                dependencies=registry_model_dependencies,
                targets=registry_change_targets,
                physical_bindings=registry_change_bindings,
                authorization=registry_change_authorization,
                clock=clock,
            ).execute(
                principal,
                change_id,
                action=action,
                expected_revision=expected_revision,
                confirmed_draft_fingerprint=confirmed_draft_fingerprint,
                rationale=rationale,
                idempotency_key=idempotency_key,
            )

    class TenantRegistryModelPublicationPreparer:
        def execute(
            self,
            principal: AuthenticatedPrincipal,
            change_id: str,
            *,
            expected_revision: int,
            confirmed_draft_fingerprint: str,
            idempotency_key: str,
        ) -> RegistryModelChangeMutation:
            return PrepareRegistryModelChangePublication(
                store=registry_model_changes,
                sources=registry_model_sources,
                remediation=registry_model_remediation,
                profile_witnesses=registry_model_witnesses,
                pointers=active_registry_pointers,
                versions=registry_versions,
                dependencies=registry_model_dependencies,
                targets=registry_change_targets,
                physical_bindings=registry_change_bindings,
                authorization=registry_change_authorization,
                clock=clock,
            ).execute(
                principal,
                change_id,
                expected_revision=expected_revision,
                confirmed_draft_fingerprint=confirmed_draft_fingerprint,
                idempotency_key=idempotency_key,
            )

    capacity = PostgresTenantCapacityStore(
        dsn=_control_plane_dsn(resolved, "api"),
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-api",
        connection_provider=control_connection_provider,
    )
    authorization = DenyByDefaultAuthorizationPolicy()
    raw_access_store = _build_postgres_workflow_access_store(
        resolved,
        credential_kind="api",
        connection_provider=control_connection_provider,
    )
    store: BackgroundJobApiStorePort
    if identity_resolver is None:
        store = raw_job_store
    else:
        from schemabridge.adapters.storage.identity_resolving import (
            IdentityResolvingBackgroundJobApiStore,
        )

        store = IdentityResolvingBackgroundJobApiStore(
            raw_job_store,
            identity_resolver,
        )

    def access_store_factory(
        workspace_id: str,
        actor_id: str,
    ) -> WorkflowAccessStorePort:
        del workspace_id, actor_id
        if identity_resolver is None:
            return raw_access_store
        from schemabridge.adapters.storage.identity_resolving import (
            IdentityResolvingWorkflowAccessStore,
        )

        return IdentityResolvingWorkflowAccessStore(
            raw_access_store,
            identity_resolver,
        )

    def inspector_factory(
        workspace_id: str,
        actor_id: str,
    ) -> WorkflowOrchestratorPort:
        return ReadOnlyWorkflowInspector(
            _build_postgres_workflow_draft_store(
                resolved,
                workspace_id=workspace_id,
                owner_actor_id=actor_id,
                credential_kind="api",
                connection_provider=control_connection_provider,
            )
        )

    return ApiHttpServices(
        authenticator=build_bearer_authenticator(resolved),
        clock=clock,
        submit=SubmitExecutionJob(
            job_store=store,
            access_store_factory=access_store_factory,
            orchestrator_factory=inspector_factory,
            authorization=authorization,
            clock=clock,
            max_attempts=resolved.worker_max_attempts,
            authorization_ttl=timedelta(seconds=resolved.api_job_authorization_ttl_seconds),
        ),
        inspect=InspectExecutionJob(
            job_store=store,
            authorization=authorization,
            clock=clock,
        ),
        cancel=CancelExecutionJob(
            job_store=store,
            authorization=authorization,
            clock=clock,
        ),
        readiness=_ControlPlaneReadiness(
            lambda: require_current_control_plane_schema(
                credential_kind="api",
                repository_root=root,
                settings=resolved,
                connection_provider=control_connection_provider,
            )
        ),
        catalog=CatalogHttpServices(
            list_connections=ListCatalogConnections(
                store=catalog_connections,
                cursors=inventory_cursors,
                clock=clock,
            ),
            register_connection=RegisterCatalogConnection(
                store=catalog_connections,
                clock=clock,
            ),
            disable_connection=DisableCatalogConnection(
                store=catalog_connections,
                clock=clock,
            ),
            list_assets=ListCatalogAssets(
                connections=catalog_connections,
                inventory=catalog_inventory,
                cursors=inventory_cursors,
                clock=clock,
            ),
            list_fields=ListCatalogFields(
                connections=catalog_connections,
                inventory=catalog_inventory,
                cursors=inventory_cursors,
                clock=clock,
            ),
            request_refresh=RequestCatalogRefresh(
                connections=catalog_connections,
                refreshes=catalog_refreshes,
                clock=clock,
            ),
            inspect_refresh=InspectCatalogRefresh(
                refreshes=catalog_refreshes,
                clock=clock,
            ),
        ),
        admission=AdmitAuthenticatedApiRequest(capacity=capacity),
        semantic_changes=SemanticChangeHttpServices(
            list_reports=ListSemanticChangeReports(
                store=semantic_change_store,
                cursors=semantic_change_cursors,
                clock=clock,
            ),
            inspect_report=InspectSemanticChangeReport(
                store=semantic_change_store,
                clock=clock,
            ),
            list_findings=ListSemanticChangeFindings(
                store=semantic_change_store,
                cursors=semantic_change_cursors,
                clock=clock,
            ),
            list_impacts=ListSemanticChangeImpacts(
                store=semantic_change_store,
                cursors=semantic_change_cursors,
                clock=clock,
            ),
        ),
        semantic_onboarding=SemanticOnboardingHttpServices(
            preflight=TenantSemanticOnboardingPreflight(),
            list_drafts=TenantSemanticOnboardingDraftLister(),
            create_draft=TenantSemanticOnboardingDraftCreator(),
            inspect_draft=TenantSemanticOnboardingDraftInspector(),
            decide=TenantSemanticOnboardingDecider(),
            prepare_publication=TenantSemanticOnboardingPublicationPreparer(),
        ),
        registry_changes=RegistryChangeHttpServices(
            request_join_profile=TenantRegistryJoinProfileRequester(),
            list_join_changes=TenantRegistryJoinChangeLister(),
            inspect_join_change=TenantRegistryJoinChangeInspector(),
            finalize_join_draft=TenantRegistryJoinDraftFinalizer(),
            decide_join=TenantRegistryJoinDecider(),
            prepare_join_publication=TenantRegistryJoinPublicationPreparer(),
        ),
        registry_model_changes=RegistryModelChangeHttpServices(
            request_profile=TenantRegistryModelProfileRequester(),
            finalize_profile=TenantRegistryModelProfileFinalizer(),
            create_change=TenantRegistryModelChangeCreator(),
            list_changes=TenantRegistryModelChangeLister(),
            inspect_change=TenantRegistryModelChangeInspector(),
            decide_change=TenantRegistryModelChangeDecider(),
            prepare_publication=TenantRegistryModelPublicationPreparer(),
        ),
        registry_publication=RegistryPublicationHttpServices(
            submit=SubmitRegistryPublication(
                proposals=registry_publication_proposals,
                jobs=registry_publication_jobs,
                authorization=onboarding_authorization,
                clock=clock,
            ),
            inspect=InspectRegistryPublication(
                jobs=registry_publication_jobs,
                authorization=onboarding_authorization,
                clock=clock,
            ),
            authorize=AuthorizeRegistryPublication(
                jobs=registry_publication_jobs,
                authorization=onboarding_authorization,
                clock=clock,
            ),
            cancel=CancelRegistryPublication(
                jobs=registry_publication_jobs,
                authorization=onboarding_authorization,
                clock=clock,
            ),
        ),
    )


def build_observer_process_runtime(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> ObserverProcessRuntime:
    """Compose the isolated observer from its read-only control credential."""

    from schemabridge.adapters.observability.metrics import OpenMetricsRegistry
    from schemabridge.adapters.observability.postgres_snapshot import (
        PostgresOperationalSnapshotReader,
    )
    from schemabridge.adapters.observability.runtime import RuntimeOperationalTelemetry
    from schemabridge.application.operational_snapshot import RefreshOperationalSnapshot
    from schemabridge.entrypoints.observer.main import (
        ObserverServices,
        create_observer_app,
    )

    resolved = settings or Settings(_env_file=None)
    if resolved.runtime_component != "observer":
        raise DatabaseConfigurationError(
            "observer composition requires SCHEMABRIDGE_COMPONENT=observer"
        )
    control_pool = build_control_plane_pool(
        credential_kind="observer",
        settings=resolved,
    )
    registry = OpenMetricsRegistry()
    telemetry = RuntimeOperationalTelemetry(
        service="observer",
        environment=resolved.environment,
        registry=registry,
    )
    readiness = _ControlPlaneReadiness(
        lambda: require_current_control_plane_schema(
            credential_kind="observer",
            repository_root=repository_root,
            settings=resolved,
            connection_provider=control_pool,
        )
    )
    refresh_snapshot = RefreshOperationalSnapshot(
        reader=PostgresOperationalSnapshotReader(
            pool=control_pool,
            statement_timeout_ms=resolved.observer_snapshot_timeout_ms,
        ),
        metrics=registry,
    )
    application = create_observer_app(
        ObserverServices(
            readiness=readiness,
            refresh_snapshot=refresh_snapshot,
            metrics=registry,
        ),
        lifecycle_resources=(control_pool,),
        max_metrics_response_bytes=resolved.observer_max_metrics_response_bytes,
        telemetry=telemetry,
    )
    return ObserverProcessRuntime(
        application=application,
        log_level=resolved.log_level,
        bind_host=resolved.observer_bind_host,
        port=resolved.observer_port,
        limit_concurrency=resolved.observer_limit_concurrency,
        graceful_shutdown_seconds=resolved.observer_graceful_shutdown_seconds,
    )


def build_api_process_runtime(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> ApiProcessRuntime:
    """Load isolated API settings and compose the complete ASGI runtime."""

    from schemabridge.entrypoints.http.app import create_http_app

    resolved = settings or Settings(_env_file=None)
    control_pool = build_control_plane_pool(
        credential_kind="api",
        settings=resolved,
    )
    telemetry = _build_operational_telemetry(resolved, service="api")
    metrics_exporter = _build_process_metrics_exporter(
        resolved,
        telemetry=telemetry,
    )
    application = create_http_app(
        build_api_http_services(
            repository_root=repository_root,
            settings=resolved,
            control_connection_provider=control_pool,
        ),
        max_body_bytes=resolved.api_max_request_bytes,
        max_concurrency=resolved.api_limit_concurrency,
        allowed_hosts=resolved.api_allowed_hosts,
        docs_enabled=resolved.api_docs_enabled,
        lifecycle_resources=(control_pool,),
        metrics_exporter=metrics_exporter,
        telemetry=telemetry,
    )
    return ApiProcessRuntime(
        application=application,
        log_level=resolved.log_level,
        bind_host=resolved.api_bind_host,
        port=resolved.api_port,
        graceful_shutdown_seconds=resolved.api_graceful_shutdown_seconds,
    )


def build_read_only_worker_orchestrator(
    workspace_id: str,
    owner_actor_id: str,
    *,
    route_context: WorkerExecutionRouteContext,
    repository_root: Path | None = None,
    settings: Settings | None = None,
    control_connection_provider: "ControlConnectionProvider | None" = None,
) -> AgentWorkflowOrchestrator:
    """Compose exact workflow execution with no publication or LLM capability."""

    from schemabridge.adapters.datahub.fake import FakeCatalogAdapter
    from schemabridge.adapters.storage.publication_audit import (
        InMemoryPublicationAuditStore,
    )
    from schemabridge.adapters.workflows.read_only import DisabledWorkflowPublisher
    from schemabridge.adapters.workflows.system import SystemWorkflowClock

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    if (
        resolved.runtime_component != "worker"
        or resolved.control_plane_kind != "postgres"
        or resolved.semantic_registry_selection != "active"
        or resolved.registry_mode != "live"
    ):
        raise DatabaseConfigurationError(
            "the execution worker requires the active live PostgreSQL control plane"
        )
    if route_context.connector_workspace_id != workspace_id:
        raise DatabaseConfigurationError(
            "managed execution does not permit cross-workspace connector routing"
        )
    workflow_store = _build_postgres_workflow_draft_store(
        resolved,
        workspace_id=workspace_id,
        owner_actor_id=owner_actor_id,
        credential_kind="worker",
        connection_provider=control_connection_provider,
    )
    bound_draft = workflow_store.load(route_context.workflow_id)
    if (
        bound_draft is None
        or bound_draft.resolved_plan is None
        or bound_draft.resolved_plan.execution_target is None
    ):
        raise DatabaseConfigurationError(
            "the managed execution workflow has no bound connector target"
        )
    target = bound_draft.resolved_plan.execution_target
    from schemabridge.domain.background_jobs import JobExecutionTargetRef

    if (
        target.workspace_id != route_context.connector_workspace_id
        or JobExecutionTargetRef.from_target(target) != route_context.execution_target
    ):
        raise DatabaseConfigurationError(
            "the managed execution workflow connector target does not match its job"
        )
    registry = build_semantic_registry(
        repository_root=root,
        settings=resolved,
        workspace_id=workspace_id,
        control_credential_kind="worker",
        control_connection_provider=control_connection_provider,
    )
    from schemabridge.adapters.connectors.postgres_routing import (
        ExecutionConnectorLeaseContext,
        PostgresExecutionConnectorRouteReader,
    )
    from schemabridge.adapters.connectors.routed_postgres import (
        RoutedPostgresQueryConnector,
    )

    lease = ExecutionConnectorLeaseContext(
        job_workspace_id=route_context.job_workspace_id,
        connector_workspace_id=route_context.connector_workspace_id,
        job_id=route_context.job_id,
        worker_id=route_context.worker_id,
        lease_capability=route_context.lease_capability,
        fencing_token=route_context.fencing_token,
        connection_id=route_context.execution_target.connection_id,
        contract_version=route_context.connector_contract_version,
        route_revision=route_context.execution_target.route_revision,
        target_fingerprint=route_context.execution_target.target_fingerprint,
    )
    route_reader = PostgresExecutionConnectorRouteReader(
        _control_plane_dsn(resolved, "worker"),
        schema=resolved.control_plane_schema,
        connection_provider=control_connection_provider,
    )
    active_registry = registry.load().registry
    rejection_fields = frozenset(
        key.physical_field.root
        for contract in active_registry.join_contracts.contracts
        for key in (contract.left_key, contract.right_key)
    )
    connector = RoutedPostgresQueryConnector(
        lambda current_target: route_reader.load_secret_reference(lease, current_target),
        _build_connector_secret_resolver(resolved, repository_root=root),
        allowed_fields=rejection_fields,
        max_rows_limit=resolved.max_query_rows,
        max_timeout_ms=resolved.statement_timeout_ms,
        max_rejection_records=resolved.max_query_rows,
    )
    prepare = build_governed_request_preparer(
        repository_root=root,
        settings=resolved,
        registry=registry,
        workspace_id=workspace_id,
        target_resolver=ExactExecutionTargetResolver(target),
        cost_preflight=AssessGovernedQueryCost(connector),
        control_credential_kind="worker",
        control_connection_provider=control_connection_provider,
    )
    return AgentWorkflowOrchestrator(
        store=workflow_store,
        clock=SystemWorkflowClock(),
        catalog=FakeCatalogAdapter(
            (),
            source_label="disabled:worker-execution-only",
        ),
        intent=build_natural_language_intent_resolver(
            "fake",
            repository_root=root,
            settings=resolved,
            registry=registry,
            workspace_id=workspace_id,
        ),
        prepare=prepare,
        execute=ExecuteGovernedRequest(
            prepare=prepare,
            executor=connector,
            rejection_reporter=connector,
        ),
        publisher=DisabledWorkflowPublisher(),
        audit_store=InMemoryPublicationAuditStore(),
        recipe_assessor=None,
    )


def build_job_worker(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
    orchestrator_factory: WorkerOrchestratorFactoryPort | None = None,
    control_connection_provider: "ControlConnectionProvider | None" = None,
) -> RunOneJobWorker:
    """Compose one bounded worker iteration with a dedicated control role."""

    from schemabridge.adapters.control_plane.threaded_heartbeat import (
        ThreadedLeaseHeartbeatSupervisor,
    )
    from schemabridge.adapters.workflows.system import SystemWorkflowClock
    from schemabridge.application.ports.workflow_access import WorkflowAccessStorePort

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    if resolved.runtime_component != "worker":
        raise DatabaseConfigurationError(
            "background worker composition requires SCHEMABRIDGE_COMPONENT=worker"
        )
    require_current_control_plane_schema(
        credential_kind="worker",
        repository_root=root,
        settings=resolved,
    )
    store = build_background_job_store(
        credential_kind="worker",
        settings=resolved,
        connection_provider=control_connection_provider,
    )
    raw_access_store = _build_postgres_workflow_access_store(
        resolved,
        credential_kind="worker",
        connection_provider=control_connection_provider,
    )
    identity_resolver = (
        build_identity_binding_resolver(
            credential_kind="worker",
            settings=resolved,
            connection_provider=control_connection_provider,
        )
        if resolved.worker_identity_lineage_mode == "verified-oidc"
        else None
    )

    def access_store_factory(
        workspace_id: str,
        actor_id: str,
    ) -> WorkflowAccessStorePort:
        del workspace_id
        if identity_resolver is None:
            return raw_access_store
        from schemabridge.adapters.storage.identity_resolving import (
            IdentityResolvingWorkflowAccessStore,
        )

        return IdentityResolvingWorkflowAccessStore(
            raw_access_store,
            identity_resolver,
            persisted_job_scope_resolver=identity_resolver,
            persisted_job_actor_id=actor_id,
        )

    def managed_orchestrator_factory(
        workspace_id: str,
        actor_id: str,
        *,
        route_context: WorkerExecutionRouteContext | None = None,
    ) -> AgentWorkflowOrchestrator:
        if route_context is None:
            raise DatabaseConfigurationError(
                "managed worker job has no lease-bound connector route"
            )
        return build_read_only_worker_orchestrator(
            workspace_id,
            actor_id,
            route_context=route_context,
            repository_root=root,
            settings=resolved,
            control_connection_provider=control_connection_provider,
        )

    return RunOneJobWorker(
        job_store=store,
        access_store_factory=access_store_factory,
        orchestrator_factory=orchestrator_factory or managed_orchestrator_factory,
        clock=SystemWorkflowClock(),
        capability_factory=lambda: secrets.token_urlsafe(48),
        heartbeat_supervisor=ThreadedLeaseHeartbeatSupervisor(store),
        worker_id=resolved.worker_id,
        lease_duration=timedelta(seconds=resolved.worker_lease_seconds),
        heartbeat_interval=timedelta(seconds=resolved.worker_heartbeat_seconds),
    )


def build_worker_process_runtime(
    *,
    readiness_probe: bool = False,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> WorkerProcessRuntime:
    """Load isolated worker settings and compose either a probe or polling runtime."""

    resolved = settings or Settings(_env_file=None)
    control_pool = build_control_plane_pool(
        credential_kind="worker",
        settings=resolved,
    )
    if readiness_probe:
        require_current_control_plane_schema(
            credential_kind="worker",
            repository_root=repository_root,
            settings=resolved,
        )
        worker = None
    else:
        worker = build_job_worker(
            repository_root=repository_root,
            settings=resolved,
            control_connection_provider=control_pool,
        )
    telemetry = _build_operational_telemetry(resolved, service="worker")
    return WorkerProcessRuntime(
        worker=worker,
        log_level=resolved.log_level,
        poll_interval_seconds=resolved.worker_poll_interval_ms / 1_000,
        control_pool=control_pool,
        telemetry=telemetry,
        metrics_exporter=(
            None
            if readiness_probe
            else _build_process_metrics_exporter(resolved, telemetry=telemetry)
        ),
    )


def build_registry_publisher_worker(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
    control_connection_provider: "ControlConnectionProvider | None" = None,
) -> "RunOneRegistryPublisherWorker":
    """Compose one publisher iteration with no source, LLM, API, or activation capability."""

    from schemabridge.adapters.catalog.postgres_registry_publication import (
        PostgresRegistryPhysicalBindingAuthority,
    )
    from schemabridge.adapters.catalog.postgres_semantic_onboarding import (
        PostgresSemanticOnboardingCatalogEvidence,
    )
    from schemabridge.adapters.control_plane.postgres_active_registry import (
        PostgresActiveRegistryPointerReader,
    )
    from schemabridge.adapters.control_plane.postgres_registry_publication import (
        PostgresRegistryPublicationJobStore,
        PostgresRegistryPublicationProposalReader,
    )
    from schemabridge.adapters.control_plane.threaded_registry_publication_heartbeat import (
        ThreadedRegistryPublicationHeartbeatSupervisor,
    )
    from schemabridge.adapters.semantic_change.postgres_dependencies import (
        PostgresSemanticChangeDependencyIndex,
    )
    from schemabridge.adapters.semantic_onboarding.publication_authority import (
        ExactRegistryPublicationAuthority,
    )
    from schemabridge.adapters.semantic_registry.datahub import (
        DataHubObservedSemanticRegistryPublisher,
        DataHubRegistryWriteConfig,
        DataHubWriterRegistryVersionReader,
    )
    from schemabridge.adapters.semantic_registry.remote_secrets import (
        RemoteDataHubObservedSemanticRegistryPublisher,
        RemoteDataHubWriterRegistryVersionReader,
    )
    from schemabridge.adapters.storage.postgres_registry_model_changes import (
        PostgresRegistryModelJoinProfileWitnessReader,
        PostgresRegistryModelRemediationEvidenceReader,
        PostgresRegistryModelReplacementSourceReader,
    )
    from schemabridge.adapters.workflows.system import SystemWorkflowClock
    from schemabridge.application.ports.registry_publication import (
        ObservedRegistryPublisherPort,
    )
    from schemabridge.application.registry_publication_worker import (
        RunOneRegistryPublisherWorker,
    )

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    if resolved.runtime_component != "publisher":
        raise DatabaseConfigurationError(
            "registry publisher composition requires SCHEMABRIDGE_COMPONENT=publisher"
        )
    require_current_control_plane_schema(
        credential_kind="publisher",
        repository_root=root,
        settings=resolved,
        connection_provider=control_connection_provider,
    )
    dsn = _control_plane_dsn(resolved, "publisher")
    store = PostgresRegistryPublicationJobStore(
        dsn,
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-publisher",
        connection_provider=control_connection_provider,
    )
    proposals = PostgresRegistryPublicationProposalReader(
        dsn,
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-publisher",
        connection_provider=control_connection_provider,
    )
    publisher: ObservedRegistryPublisherPort
    versions: RegistryVersionReadPort
    if resolved.connector_secret_mode == "remote":
        credentials = _build_registry_publisher_credential_resolver(resolved)
        publisher = RemoteDataHubObservedSemanticRegistryPublisher(credentials)
        versions = RemoteDataHubWriterRegistryVersionReader(credentials)
    else:
        writer_path = resolved.registry_publisher_writer_env_path
        if not writer_path.is_absolute():
            writer_path = root / writer_path
        config = DataHubRegistryWriteConfig.from_env_file(writer_path.resolve())
        publisher = DataHubObservedSemanticRegistryPublisher(config)
        versions = DataHubWriterRegistryVersionReader(config)
    authority = ExactRegistryPublicationAuthority(
        proposals=proposals,
        catalog=PostgresSemanticOnboardingCatalogEvidence(
            dsn,
            schema=resolved.control_plane_schema,
            application_name="schemabridge-control-publisher",
            stale_after_seconds=resolved.catalog_stale_after_seconds,
            connection_provider=control_connection_provider,
        ),
        pointers=PostgresActiveRegistryPointerReader(
            dsn,
            schema=resolved.control_plane_schema,
            application_name="schemabridge-control-publisher",
            connection_provider=control_connection_provider,
        ),
        versions=versions,
        physical_bindings=PostgresRegistryPhysicalBindingAuthority(
            dsn,
            schema=resolved.control_plane_schema,
            application_name="schemabridge-control-publisher",
            stale_after_seconds=resolved.catalog_stale_after_seconds,
            connection_provider=control_connection_provider,
        ),
        model_sources=PostgresRegistryModelReplacementSourceReader(
            dsn,
            schema=resolved.control_plane_schema,
            application_name="schemabridge-control-publisher",
            connection_provider=control_connection_provider,
        ),
        model_dependencies=PostgresSemanticChangeDependencyIndex(
            dsn,
            schema=resolved.control_plane_schema,
            application_name="schemabridge-control-publisher",
            connection_provider=control_connection_provider,
        ),
        model_remediation=PostgresRegistryModelRemediationEvidenceReader(
            dsn,
            schema=resolved.control_plane_schema,
            application_name="schemabridge-control-publisher",
            connection_provider=control_connection_provider,
        ),
        model_profile_witnesses=PostgresRegistryModelJoinProfileWitnessReader(
            dsn,
            schema=resolved.control_plane_schema,
            application_name="schemabridge-control-publisher",
            connection_provider=control_connection_provider,
        ),
    )
    return RunOneRegistryPublisherWorker(
        jobs=store,
        authority=authority,
        publisher=publisher,
        clock=SystemWorkflowClock(),
        capability_factory=lambda: secrets.token_urlsafe(48),
        heartbeat_supervisor=ThreadedRegistryPublicationHeartbeatSupervisor(store),
        worker_id=resolved.registry_publisher_id,
        lease_duration=timedelta(seconds=resolved.registry_publisher_lease_seconds),
        heartbeat_interval=timedelta(seconds=resolved.registry_publisher_heartbeat_seconds),
    )


def build_registry_publisher_process_runtime(
    *,
    readiness_probe: bool = False,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> RegistryPublisherProcessRuntime:
    """Compose the isolated publisher without resolving DataHub during readiness."""

    resolved = settings or Settings(_env_file=None)
    control_pool = build_control_plane_pool(
        credential_kind="publisher",
        settings=resolved,
    )
    if readiness_probe:
        require_current_control_plane_schema(
            credential_kind="publisher",
            repository_root=repository_root,
            settings=resolved,
        )
        publisher = None
    else:
        publisher = build_registry_publisher_worker(
            repository_root=repository_root,
            settings=resolved,
            control_connection_provider=control_pool,
        )
    telemetry = _build_operational_telemetry(resolved, service="publisher")
    return RegistryPublisherProcessRuntime(
        publisher=publisher,
        log_level=resolved.log_level,
        poll_interval_seconds=resolved.registry_publisher_poll_interval_ms / 1_000,
        control_pool=control_pool,
        telemetry=telemetry,
        metrics_exporter=(
            None
            if readiness_probe
            else _build_process_metrics_exporter(resolved, telemetry=telemetry)
        ),
    )


def build_semantic_profile_worker(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
    control_connection_provider: "ControlConnectionProvider | None" = None,
    stop_requested: Callable[[], bool] | None = None,
) -> "RunOneSemanticJoinProfile":
    """Compose aggregate profiling through exact lease-bound connector routes."""

    from schemabridge.adapters.connectors.postgres_profile_routing import (
        PostgresSemanticProfileConnectorRouteReader,
    )
    from schemabridge.adapters.postgres.routed_relationships import (
        RoutedSemanticJoinProfileEvidenceFactory,
    )
    from schemabridge.adapters.semantic_change.postgres_profile_queue import (
        PostgresSemanticJoinProfileQueue,
    )
    from schemabridge.adapters.workflows.system import SystemWorkflowClock
    from schemabridge.application.semantic_profile_worker import RunOneSemanticJoinProfile

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    if resolved.runtime_component != "worker":
        raise DatabaseConfigurationError(
            "semantic profile worker composition requires SCHEMABRIDGE_COMPONENT=worker"
        )
    require_current_control_plane_schema(
        credential_kind="worker",
        repository_root=root,
        settings=resolved,
    )
    control_dsn = _control_plane_dsn(resolved, "worker")
    queue = PostgresSemanticJoinProfileQueue(
        dsn=control_dsn,
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-worker",
        connection_provider=control_connection_provider,
    )
    return RunOneSemanticJoinProfile(
        queue=queue,
        evidence_factory=RoutedSemanticJoinProfileEvidenceFactory(
            route_reader=PostgresSemanticProfileConnectorRouteReader(
                dsn=control_dsn,
                schema=resolved.control_plane_schema,
                application_name="schemabridge-control-worker",
                connection_provider=control_connection_provider,
            ),
            secret_resolver=_build_connector_secret_resolver(
                resolved,
                repository_root=root,
            ),
            statement_timeout_ms=resolved.statement_timeout_ms,
        ),
        clock=SystemWorkflowClock(),
        capability_factory=lambda: secrets.token_urlsafe(48),
        worker_id=resolved.worker_id,
        lease_duration=timedelta(seconds=resolved.worker_lease_seconds),
        retention=timedelta(days=resolved.semantic_profile_retention_days),
        maintenance_batch_size=resolved.semantic_profile_maintenance_batch_size,
        stop_requested=stop_requested or (lambda: False),
    )


def build_semantic_profile_worker_process_runtime(
    *,
    readiness_probe: bool = False,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> "SemanticProfileWorkerProcessRuntime":
    """Compose an isolated aggregate profiler or a schema-only readiness probe."""

    from threading import Event

    from schemabridge.entrypoints.semantic_profile_worker.main import (
        SemanticProfileWorkerProcessRuntime,
    )

    resolved = settings or Settings(_env_file=None)
    stop_event = Event()
    control_pool = build_control_plane_pool(
        credential_kind="worker",
        settings=resolved,
    )
    if readiness_probe:
        require_current_control_plane_schema(
            credential_kind="worker",
            repository_root=repository_root,
            settings=resolved,
        )
        worker = None
    else:
        worker = build_semantic_profile_worker(
            repository_root=repository_root,
            settings=resolved,
            control_connection_provider=control_pool,
            stop_requested=stop_event.is_set,
        )
    telemetry = _build_operational_telemetry(resolved, service="profile")
    return SemanticProfileWorkerProcessRuntime(
        worker=worker,
        log_level=resolved.log_level,
        poll_interval_seconds=resolved.worker_poll_interval_ms / 1_000,
        control_resource=control_pool,
        stop_event=stop_event,
        telemetry=telemetry,
        metrics_exporter=(
            None
            if readiness_probe
            else _build_process_metrics_exporter(resolved, telemetry=telemetry)
        ),
    )


def build_semantic_dependency_reconciler(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
    control_connection_provider: "ControlConnectionProvider | None" = None,
) -> "ReconcileSemanticDependencies":
    """Compose complete workflow/recipe indexing with read-only DataHub credentials."""

    from schemabridge.adapters.datahub.recipe_inventory import (
        DataHubQueryRecipeInventory,
        DataHubQueryRecipeInventoryConfig,
    )
    from schemabridge.adapters.semantic_change.postgres_dependencies import (
        PostgresSemanticChangeDependencyIndex,
    )
    from schemabridge.adapters.semantic_change.postgres_dependency_sources import (
        PostgresManagedWorkflowDependencySource,
        PostgresSemanticDependencyIndexSink,
    )
    from schemabridge.application.semantic_dependency_reconciler import (
        ReconcileSemanticDependencies,
    )

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    if resolved.runtime_component != "reconciler":
        raise DatabaseConfigurationError(
            "semantic dependency reconciliation requires SCHEMABRIDGE_COMPONENT=reconciler"
        )
    require_current_control_plane_schema(
        credential_kind="reconciler",
        repository_root=root,
        settings=resolved,
    )
    recipe_inventory: QueryRecipeDependencySourcePort
    if resolved.connector_secret_mode == "remote":
        from schemabridge.adapters.semantic_registry.remote_secrets import (
            RemoteDataHubQueryRecipeInventory,
        )

        recipe_inventory = RemoteDataHubQueryRecipeInventory(
            credentials=_build_registry_credential_resolver(resolved),
            timeout_seconds=resolved.catalog_source_timeout_seconds,
            max_response_bytes=min(resolved.catalog_max_response_bytes, 4 * 1024 * 1024),
        )
    else:
        from schemabridge.adapters.semantic_registry.datahub import DataHubRegistryReadConfig

        reader_env_path = resolved.semantic_registry_reader_env_path
        if not reader_env_path.is_absolute():
            reader_env_path = root / reader_env_path
        reader_config = DataHubRegistryReadConfig.from_env_file(reader_env_path.resolve())
        recipe_inventory = DataHubQueryRecipeInventory(
            DataHubQueryRecipeInventoryConfig(
                server=reader_config.server,
                token=reader_config.token,
                timeout_seconds=resolved.catalog_source_timeout_seconds,
                max_response_bytes=min(
                    resolved.catalog_max_response_bytes,
                    4 * 1024 * 1024,
                ),
            )
        )
    control_dsn = _control_plane_dsn(resolved, "reconciler")
    dependency_index = PostgresSemanticChangeDependencyIndex(
        control_dsn,
        schema=resolved.control_plane_schema,
        connection_provider=control_connection_provider,
    )
    return ReconcileSemanticDependencies(
        pointers=build_active_registry_pointer_reader(
            credential_kind="reconciler",
            settings=resolved,
            connection_provider=control_connection_provider,
        ),
        versions=build_registry_version_reader(
            repository_root=root,
            settings=resolved,
        ),
        workflows=PostgresManagedWorkflowDependencySource(
            control_dsn,
            schema=resolved.control_plane_schema,
            connection_provider=control_connection_provider,
        ),
        recipes=recipe_inventory,
        index=PostgresSemanticDependencyIndexSink(dependency_index),
        page_size=50,
    )


def build_semantic_reconciler(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
    control_connection_provider: "ControlConnectionProvider | None" = None,
    stop_requested: Callable[[], bool] | None = None,
) -> "RunOneSemanticChangeScan":
    """Compose one fenced scan reconciler without source or DataHub write credentials."""

    from schemabridge.adapters.connectors.postgres_routing import (
        PostgresExecutionTargetResolver,
    )
    from schemabridge.adapters.semantic_change.postgres_dependencies import (
        PostgresSemanticChangeDependencyIndex,
    )
    from schemabridge.adapters.semantic_change.postgres_evidence import (
        PostgresSemanticChangeEvidenceReader,
    )
    from schemabridge.adapters.semantic_change.postgres_profile_queue import (
        PostgresSemanticJoinProfileQueue,
        QueuedRelationshipEvidencePort,
    )
    from schemabridge.adapters.semantic_change.postgres_scans import (
        PostgresSemanticChangeScanStore,
    )
    from schemabridge.adapters.semantic_change.postgres_store import (
        PostgresSemanticChangeStore,
    )
    from schemabridge.adapters.semantic_change.scan_runner import (
        InspectSemanticChangeScanRunner,
    )
    from schemabridge.adapters.workflows.system import SystemWorkflowClock
    from schemabridge.application.semantic_change import InspectSemanticChange
    from schemabridge.application.semantic_change_reconciler import (
        RunOneSemanticChangeScan,
    )

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    if resolved.runtime_component != "reconciler":
        raise DatabaseConfigurationError(
            "semantic reconciler composition requires SCHEMABRIDGE_COMPONENT=reconciler"
        )
    require_current_control_plane_schema(
        credential_kind="reconciler",
        repository_root=root,
        settings=resolved,
    )
    control_dsn = _control_plane_dsn(resolved, "reconciler")
    clock = SystemWorkflowClock()
    dependency_index = PostgresSemanticChangeDependencyIndex(
        control_dsn,
        schema=resolved.control_plane_schema,
        connection_provider=control_connection_provider,
    )
    report_store = PostgresSemanticChangeStore(
        control_dsn,
        dependency_index,
        _control_audit_keys(resolved),
        resolved.control_audit_key_version,
        schema=resolved.control_plane_schema,
        retention=timedelta(days=resolved.semantic_reconciler_retention_days),
        connection_provider=control_connection_provider,
    )
    pointers = build_active_registry_pointer_reader(
        credential_kind="reconciler",
        settings=resolved,
        connection_provider=control_connection_provider,
    )
    versions = build_registry_version_reader(
        repository_root=root,
        settings=resolved,
    )
    profile_queue = PostgresSemanticJoinProfileQueue(
        control_dsn,
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-reconciler",
        connection_provider=control_connection_provider,
    )
    profile_target_resolver = PostgresExecutionTargetResolver(
        control_dsn,
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-reconciler",
        connection_provider=control_connection_provider,
    )
    dependency_reconciler = build_semantic_dependency_reconciler(
        repository_root=root,
        settings=resolved,
        control_connection_provider=control_connection_provider,
    )

    def resolve_scope(request: "SemanticChangeScanRequest") -> SemanticRegistryScope:
        if request.catalog_scope is None or request.registry_id is None:
            raise DatabaseConfigurationError(
                "semantic scan trigger does not identify an exact registry scope"
            )
        return SemanticRegistryScope(
            workspace_id=request.workspace_id,
            catalog_scope=request.catalog_scope,
            registry_id=request.registry_id,
        )

    def prepare_dependencies(
        request: "SemanticChangeScanRequest",
        scope: SemanticRegistryScope,
        should_continue: Callable[[], bool],
    ) -> "SemanticDependencyReconciliationResult":
        del request
        current = dependency_index.load_state(scope)
        result = dependency_reconciler.execute(
            scope,
            watermark=current.watermark + 1,
            indexed_at=clock.now(),
            should_continue=should_continue,
        )
        if not result.state.complete:
            raise DatabaseConfigurationError("semantic dependency coverage is incomplete")
        return result

    def inspector_factory(
        scope: SemanticRegistryScope,
        request: "SemanticChangeScanRequest",
    ) -> "InspectSemanticChange":
        relationships = QueuedRelationshipEvidencePort(
            queue=profile_queue,
            target_resolver=profile_target_resolver,
            workspace_id=scope.workspace_id,
            scan_id=request.scan_id,
            clock=clock.now,
            max_attempts=resolved.semantic_profile_max_attempts,
        )
        evidence = PostgresSemanticChangeEvidenceReader(
            control_dsn,
            relationships,
            schema=resolved.control_plane_schema,
            connection_provider=control_connection_provider,
            clock=clock.now,
        )
        return InspectSemanticChange(
            pointers=pointers,
            versions=versions,
            evidence=evidence,
            dependency_index=dependency_index,
            store=report_store,
            scope=scope,
        )

    runner = InspectSemanticChangeScanRunner(
        scope_resolver=resolve_scope,
        inspector_factory=inspector_factory,
        prepare_dependencies=prepare_dependencies,
    )
    scan_store = PostgresSemanticChangeScanStore(
        control_dsn,
        report_store,
        schema=resolved.control_plane_schema,
        connection_provider=control_connection_provider,
    )
    return RunOneSemanticChangeScan(
        scans=scan_store,
        runner=runner,
        clock=clock,
        capability_factory=lambda: secrets.token_urlsafe(48),
        reconciler_id=resolved.semantic_reconciler_id,
        lease_duration=timedelta(seconds=resolved.semantic_reconciler_lease_seconds),
        retention=timedelta(days=resolved.semantic_reconciler_retention_days),
        maintenance_batch_size=resolved.semantic_reconciler_maintenance_batch_size,
        stop_requested=stop_requested or (lambda: False),
    )


def build_semantic_reconciler_process_runtime(
    *,
    readiness_probe: bool = False,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> "SemanticReconcilerProcessRuntime":
    """Compose the isolated semantic reconciler or a schema-only readiness probe."""

    from threading import Event

    from schemabridge.entrypoints.semantic_reconciler.main import (
        SemanticReconcilerProcessRuntime,
    )

    resolved = settings or Settings(_env_file=None)
    stop_event = Event()
    require_current_control_plane_schema(
        credential_kind="reconciler",
        repository_root=repository_root,
        settings=resolved,
    )
    control_pool = build_control_plane_pool(
        credential_kind="reconciler",
        settings=resolved,
    )
    reconciler = (
        None
        if readiness_probe
        else build_semantic_reconciler(
            repository_root=repository_root,
            settings=resolved,
            control_connection_provider=control_pool,
            stop_requested=stop_event.is_set,
        )
    )
    telemetry = _build_operational_telemetry(resolved, service="reconciler")
    return SemanticReconcilerProcessRuntime(
        reconciler=reconciler,
        log_level=resolved.log_level,
        poll_interval_seconds=resolved.semantic_reconciler_poll_interval_ms / 1_000,
        control_resource=control_pool,
        stop_event=stop_event,
        telemetry=telemetry,
        metrics_exporter=(
            None
            if readiness_probe
            else _build_process_metrics_exporter(resolved, telemetry=telemetry)
        ),
    )


def build_semantic_change_operator_runtime(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> "SemanticChangeOperatorRuntime":
    """Compose the separate reconciler-credential semantic-change operator."""

    from schemabridge.adapters.connectors.postgres_routing import (
        PostgresExecutionTargetResolver,
    )
    from schemabridge.adapters.semantic_change.postgres_dependencies import (
        PostgresSemanticChangeDependencyIndex,
    )
    from schemabridge.adapters.semantic_change.postgres_evidence import (
        PostgresSemanticChangeEvidenceReader,
    )
    from schemabridge.adapters.semantic_change.postgres_profile_queue import (
        PostgresSemanticJoinProfileQueue,
        QueuedRelationshipEvidencePort,
    )
    from schemabridge.adapters.semantic_change.postgres_scans import (
        PostgresSemanticChangeScanStore,
    )
    from schemabridge.adapters.semantic_change.postgres_store import (
        PostgresSemanticChangeStore,
    )
    from schemabridge.adapters.workflows.system import SystemWorkflowClock
    from schemabridge.application.semantic_change import (
        CommitSemanticChangeDecision,
        InspectSemanticChange,
        PrepareSemanticChangeDecision,
        PrepareSemanticChangeDecisionApproval,
    )
    from schemabridge.application.semantic_change_operator import (
        InspectLatestSemanticChange,
        LoadSemanticChangeHead,
        VerifySemanticChangeAudit,
    )
    from schemabridge.domain.semantic_change_scans import SemanticChangeScanRequest
    from schemabridge.entrypoints.semantic_change.main import (
        SemanticChangeOperatorConfig,
        SemanticChangeOperatorRuntime,
        SemanticChangeOperatorServices,
    )

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or Settings(_env_file=None)
    if resolved.runtime_component != "reconciler":
        raise DatabaseConfigurationError(
            "semantic change operator requires SCHEMABRIDGE_COMPONENT=reconciler"
        )
    require_current_control_plane_schema(
        credential_kind="reconciler",
        repository_root=root,
        settings=resolved,
    )
    scope = _semantic_registry_scope(resolved, None)
    control_dsn = _control_plane_dsn(resolved, "reconciler")
    clock = SystemWorkflowClock()
    dependency_index = PostgresSemanticChangeDependencyIndex(
        control_dsn,
        schema=resolved.control_plane_schema,
    )
    store = PostgresSemanticChangeStore(
        control_dsn,
        dependency_index,
        _control_audit_keys(resolved),
        resolved.control_audit_key_version,
        schema=resolved.control_plane_schema,
        retention=timedelta(days=resolved.semantic_reconciler_retention_days),
    )
    scans = PostgresSemanticChangeScanStore(
        control_dsn,
        store,
        schema=resolved.control_plane_schema,
    )
    pointers = build_active_registry_pointer_reader(
        credential_kind="reconciler",
        settings=resolved,
    )
    versions = build_registry_version_reader(
        repository_root=root,
        settings=resolved,
    )
    profiles = PostgresSemanticJoinProfileQueue(
        control_dsn,
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-reconciler",
    )
    profile_target_resolver = PostgresExecutionTargetResolver(
        control_dsn,
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-reconciler",
    )

    def inspector_factory(request: SemanticChangeScanRequest) -> InspectSemanticChange:
        relationships = QueuedRelationshipEvidencePort(
            queue=profiles,
            target_resolver=profile_target_resolver,
            workspace_id=scope.workspace_id,
            scan_id=request.scan_id,
            clock=clock.now,
            max_attempts=resolved.semantic_profile_max_attempts,
        )
        return InspectSemanticChange(
            pointers=pointers,
            versions=versions,
            evidence=PostgresSemanticChangeEvidenceReader(
                control_dsn,
                relationships,
                schema=resolved.control_plane_schema,
                application_name="schemabridge-control-reconciler",
                clock=clock.now,
            ),
            dependency_index=dependency_index,
            store=store,
            scope=scope,
        )

    inspect = InspectLatestSemanticChange(
        scans=scans,
        inspector_factory=inspector_factory,
        store=store,
        scope=scope,
    )
    head = LoadSemanticChangeHead(store)
    audit = build_registry_control_store(
        credential_kind="reconciler",
        settings=resolved,
    )
    return SemanticChangeOperatorRuntime(
        services=SemanticChangeOperatorServices(
            inspect=inspect,
            prepare=PrepareSemanticChangeDecision(store),
            approve=PrepareSemanticChangeDecisionApproval(),
            commit=CommitSemanticChangeDecision(inspect, store),
            head=head,
            verify_audit=VerifySemanticChangeAudit(heads=head, audit=audit),
        ),
        config=SemanticChangeOperatorConfig(
            scope=scope,
            trusted_actor=resolve_control_operator_actor(
                None,
                required_role=IdentityRole.STEWARD.value,
                settings=resolved,
            ),
        ),
    )


def build_catalog_indexer(
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
    control_connection_provider: "ControlConnectionProvider | None" = None,
    stop_requested: Callable[[], bool] | None = None,
) -> "RunOneCatalogRefresh":
    """Compose one metadata-only catalog iteration through the catalog role."""

    from schemabridge.adapters.catalog.postgres_connector_routing import (
        PostgresCatalogConnectorRouteReader,
    )
    from schemabridge.adapters.catalog.postgres_refresh import (
        PostgresCatalogRefreshStore,
    )
    from schemabridge.adapters.catalog.routed_datahub import (
        RoutedCatalogSourceResolver,
        RoutedDataHubGraphQLCatalogSource,
    )
    from schemabridge.adapters.catalog.synthetic_source import (
        LazySyntheticCatalogSource,
        SyntheticCatalogSpecification,
    )
    from schemabridge.application.catalog_indexer import RunOneCatalogRefresh
    from schemabridge.domain.catalog_inventory import (
        CatalogConnectionId,
        CatalogConnectionKind,
    )

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    if resolved.runtime_component != "catalog":
        raise DatabaseConfigurationError(
            "catalog indexer composition requires SCHEMABRIDGE_COMPONENT=catalog"
        )
    require_current_control_plane_schema(
        credential_kind="catalog",
        repository_root=root,
        settings=resolved,
    )
    dsn = _control_plane_dsn(resolved, "catalog")
    refreshes = PostgresCatalogRefreshStore(
        dsn=dsn,
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-catalog",
        connection_provider=control_connection_provider,
    )
    routes = PostgresCatalogConnectorRouteReader(
        dsn=dsn,
        schema=resolved.control_plane_schema,
        application_name="schemabridge-control-catalog",
        connection_provider=control_connection_provider,
    )
    datahub = RoutedDataHubGraphQLCatalogSource(
        secrets=_build_datahub_catalog_secret_resolver(
            resolved,
            repository_root=root,
        ),
        timeout_seconds=resolved.catalog_source_timeout_seconds,
        max_response_bytes=resolved.catalog_max_response_bytes,
    )
    synthetic = LazySyntheticCatalogSource(
        {
            CatalogConnectionId(connection_id): SyntheticCatalogSpecification(
                asset_count=asset_count,
                wide_asset_every=997,
                wide_field_count=64,
            )
            for connection_id, asset_count in (resolved.catalog_synthetic_asset_counts.items())
        }
    )
    return RunOneCatalogRefresh(
        refreshes=refreshes,
        routes=routes,
        sources=RoutedCatalogSourceResolver(
            datahub=datahub,
            sources={
                CatalogConnectionKind.SYNTHETIC: synthetic,
            },
        ),
        capability_factory=lambda: secrets.token_urlsafe(48),
        indexer_id=resolved.catalog_indexer_id,
        lease_duration=timedelta(seconds=resolved.catalog_lease_seconds),
        source_operation_timeout=timedelta(seconds=resolved.catalog_source_timeout_seconds),
        store_operation_timeout=timedelta(
            seconds=(
                resolved.control_pool_acquisition_timeout_seconds
                + resolved.statement_timeout_ms / 1_000
            )
        ),
        page_size=resolved.catalog_page_size,
        stop_requested=stop_requested or (lambda: False),
    )


def build_catalog_process_runtime(
    *,
    readiness_probe: bool = False,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> "CatalogProcessRuntime":
    """Compose the isolated catalog process or a readiness-only preflight."""

    from threading import Event

    from schemabridge.entrypoints.catalog.main import CatalogProcessRuntime

    resolved = settings or Settings(_env_file=None)
    stop_event = Event()
    control_pool = build_control_plane_pool(
        credential_kind="catalog",
        settings=resolved,
    )
    if readiness_probe:
        require_current_control_plane_schema(
            credential_kind="catalog",
            repository_root=repository_root,
            settings=resolved,
        )
        indexer = None
    else:
        indexer = build_catalog_indexer(
            repository_root=repository_root,
            settings=resolved,
            control_connection_provider=control_pool,
            stop_requested=stop_event.is_set,
        )
    telemetry = _build_operational_telemetry(resolved, service="catalog")
    return CatalogProcessRuntime(
        indexer=indexer,
        log_level=resolved.log_level,
        poll_interval_seconds=resolved.catalog_poll_interval_ms / 1_000,
        control_pool=control_pool,
        stop_event=stop_event,
        telemetry=telemetry,
        metrics_exporter=(
            None
            if readiness_probe
            else _build_process_metrics_exporter(resolved, telemetry=telemetry)
        ),
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
    if (
        resolved.runtime_profile in {"staging", "production"}
        and resolved.runtime_component == "web"
    ):
        raise DatabaseConfigurationError(
            "managed web cannot compose a mutation-capable query recipe repository"
        )
    if adapter_kind == "live":
        from schemabridge.adapters.datahub.query_recipes import DataHubQueryRecipeAdapter

        return DataHubQueryRecipeAdapter.from_env_file(root / ".local/datahub/writer.env")
    if resolved.runtime_profile in {"staging", "production"}:
        raise DatabaseConfigurationError(
            "fake query recipes are available only with the local control plane"
        )
    from schemabridge.adapters.recipes.sqlite import SqliteQueryRecipeRepository

    return SqliteQueryRecipeRepository(resolved.draft_store_path.resolve())


def build_query_recipe_preparer(
    adapter_kind: Literal["live", "fake"] = "fake",
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
    workspace_id: str | None = None,
    owner_actor_id: str | None = None,
) -> PrepareStoredQueryRecipe:
    """Prepare a recipe only from one durably completed governed workflow."""

    resolved = settings or get_settings()
    repository = build_query_recipe_repository(
        adapter_kind,
        repository_root=repository_root,
        settings=resolved,
    )
    workflow_store = build_workflow_draft_store(
        workspace_id=workspace_id,
        owner_actor_id=owner_actor_id,
        settings=resolved,
    )
    return PrepareStoredQueryRecipe(
        store=workflow_store,
        prepare=PrepareQueryRecipe(repository),
    )


def build_query_recipe_migration_preparer(
    adapter_kind: Literal["live", "fake"] = "fake",
    *,
    workspace_id: str,
    owner_actor_id: str,
    repository_root: Path | None = None,
    settings: Settings | None = None,
    pointer_store: ActiveRegistryPointerReadPort | None = None,
) -> PrepareStoredStaleQueryRecipeMigration:
    """Compose exact durable inputs for a read-only stale-recipe migration review."""

    resolved = settings or get_settings()
    pointers = pointer_store
    if pointers is None:
        if resolved.control_plane_kind != "postgres":
            raise DatabaseConfigurationError(
                "recipe migration requires an authoritative PostgreSQL active pointer"
            )
        pointers = build_registry_control_store(settings=resolved)
    return PrepareStoredStaleQueryRecipeMigration(
        store=build_workflow_draft_store(
            workspace_id=workspace_id,
            owner_actor_id=owner_actor_id,
            settings=resolved,
        ),
        repository=build_query_recipe_repository(
            adapter_kind,
            repository_root=repository_root,
            settings=resolved,
        ),
        pointers=pointers,
        scope=_semantic_registry_scope(resolved, workspace_id),
    )


def build_query_recipe_publisher(
    adapter_kind: Literal["live", "fake"] = "fake",
    *,
    repository_root: Path | None = None,
    settings: Settings | None = None,
    workspace_id: str | None = None,
) -> PublishQueryRecipe:
    return PublishQueryRecipe(
        repository=build_query_recipe_repository(
            adapter_kind,
            repository_root=repository_root,
            settings=settings,
        ),
        audit_store=build_publication_audit_store(
            settings,
            workspace_id=workspace_id,
        ),
    )


def build_query_recipe_migration_publisher(
    adapter_kind: Literal["live", "fake"] = "fake",
    *,
    workspace_id: str,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> PublishStaleQueryRecipeMigration:
    """Compose approval-gated migration publication with mandatory scoped audit."""

    resolved = settings or get_settings()
    return PublishStaleQueryRecipeMigration(
        repository=build_query_recipe_repository(
            adapter_kind,
            repository_root=repository_root,
            settings=resolved,
        ),
        audit_store=build_publication_audit_store(
            resolved,
            workspace_id=workspace_id,
        ),
    )


def build_streamlit_ui_service(
    catalog_kind: Literal["live", "recorded"] | None = None,
    *,
    publication_kind: Literal["live", "fake", "disabled"] | None = None,
    execution_kind: Literal["live", "recorded", "disabled"] | None = None,
    repository_root: Path | None = None,
    settings: Settings | None = None,
    principal: AuthenticatedPrincipal | None = None,
) -> JudgeUiService:
    """Compose the judge UI without exposing concrete adapters to page callbacks."""

    root = (repository_root or Path.cwd()).resolve()
    resolved = settings or get_settings()
    _reject_operator_credentials_in_managed_web(resolved)
    selected_catalog = catalog_kind or resolved.catalog_mode
    selected_publication = publication_kind or resolved.publication_mode
    selected_execution = execution_kind or resolved.execution_mode
    if resolved.runtime_profile in {"hosted-demo", "staging", "production"}:
        requested_modes = (
            (catalog_kind, resolved.catalog_mode, "catalog"),
            (publication_kind, resolved.publication_mode, "publication"),
            (execution_kind, resolved.execution_mode, "execution"),
        )
        for requested, configured, name in requested_modes:
            if requested is not None and requested != configured:
                raise ValueError(
                    f"{name} mode is fixed by the {resolved.runtime_profile} deployment profile"
                )
    resolved_principal = principal or build_streamlit_principal(settings=resolved)
    if resolved.auth_mode == "oidc":
        if resolved_principal.authentication_method is not AuthenticationMethod.OIDC:
            raise ValueError("OIDC mode requires an OIDC-authenticated principal")
    elif resolved_principal.authentication_method is not AuthenticationMethod.LOCAL_DEMO:
        raise ValueError("local demo mode requires the local demo principal")
    if (
        selected_publication == "live"
        and resolved_principal.authentication_method is not AuthenticationMethod.OIDC
    ):
        raise ValueError("live publication requires an OIDC-authenticated principal")
    if (
        resolved.runtime_profile in {"staging", "production"}
        and resolved.control_plane_kind == "postgres"
    ):
        build_source_control_database_separation(resolved).execute()
    from schemabridge.adapters.storage.workflow_access import InMemoryWorkflowAccessStore
    from schemabridge.adapters.workflows.system import SystemWorkflowClock

    authorization = DenyByDefaultAuthorizationPolicy()
    clock = SystemWorkflowClock()
    if WorkflowPermission.VIEW not in authorization.permissions_for(
        resolved_principal,
        at=clock.now(),
    ):

        def denied_orchestrator_factory() -> AgentWorkflowOrchestrator:
            raise DatabaseConfigurationError(
                "UI integrations are unavailable without view permission"
            )

        return JudgeUiService(
            orchestrator_factory=denied_orchestrator_factory,
            views=DeniedJudgeUiViewFactory(),
            principal=resolved_principal,
            access_store=InMemoryWorkflowAccessStore(),
            authorization=authorization,
            clock=clock,
            require_separate_publisher=selected_publication == "live",
            max_live_publication_identity_age=timedelta(
                seconds=resolved.oidc_live_publication_max_identity_age_seconds
            ),
        )
    registry = build_semantic_registry(
        repository_root=root,
        settings=resolved,
        workspace_id=resolved_principal.workspace_id,
    )
    prepare = build_governed_request_preparer(
        repository_root=root,
        settings=resolved,
        registry=registry,
        workspace_id=resolved_principal.workspace_id,
    )
    candidate_report = build_candidate_generator("recorded", repository_root=root).execute(
        build_customer_key_concept()
    )
    scoped_context = registry.load()
    context = scoped_context.registry
    projection_status = UiRegistryProjectionStatus.FIXED
    if scoped_context.activation_generation is not None:
        store = build_registry_control_store(settings=resolved)
        pointer = store.load_active(scoped_context.scope)
        if (
            pointer is None
            or pointer.generation != scoped_context.activation_generation
            or scoped_context.active_pointer_fingerprint is None
            or registry_projection_fingerprint(pointer) != scoped_context.active_pointer_fingerprint
        ):
            raise DatabaseConfigurationError(
                "active registry UI state does not match the authoritative control pointer"
            )
        outbox = store.load_transition_outbox(
            scoped_context.scope,
            pointer.transition_id,
        )
        projection_status = (
            UiRegistryProjectionStatus.UNKNOWN
            if outbox is None
            else UiRegistryProjectionStatus(outbox.status.value)
        )
    modes = (
        UiMode(
            "Semantic registry",
            ("Live DataHub" if resolved.registry_mode == "live" else "Recorded version bundle"),
            resolved.registry_mode,
            "One exact workspace-scoped registry is loaded atomically; no fallback is used.",
        ),
        UiMode(
            "Catalog",
            "Live DataHub" if selected_catalog == "live" else "Recorded catalog",
            selected_catalog,
            "No fallback is used if the selected catalog is unavailable.",
        ),
        UiMode(
            "Demo candidate evidence",
            "Recorded synthetic evidence",
            "recorded",
            "Bounded, sanitized fixture evidence for the historical demo workflow only.",
        ),
        UiMode(
            "Demo workflow intent",
            "Deterministic fake parser",
            "fake",
            "API-key-free typed interpretation for the historical demo workflow only.",
        ),
        UiMode(
            "Source",
            (
                "Live read-only PostgreSQL"
                if selected_execution == "live"
                else (
                    "Recorded synthetic PostgreSQL observation"
                    if selected_execution == "recorded"
                    else "Worker submission unavailable"
                )
            ),
            selected_execution,
            (
                "Execution is guarded, bounded, and uses schemabridge_reader."
                if selected_execution == "live"
                else (
                    "SQL is compiled and guarded live; result/rejection evidence is replayed "
                    "only for the exact versioned north-star query."
                    if selected_execution == "recorded"
                    else "Planning and remote preflight remain available, but this web runtime "
                    "cannot execute source queries or submit them to a worker."
                )
            ),
        ),
        UiMode(
            "Release",
            resolved.release_ref,
            "release",
            "Build identifier supplied by the deployment; it is not a credential.",
        ),
        UiMode(
            "Publication",
            (
                "Live DataHub"
                if selected_publication == "live"
                else (
                    "Fake local publication"
                    if selected_publication == "fake"
                    else "Publisher submission unavailable"
                )
            ),
            selected_publication,
            (
                "Writes still require the exact typed publication approval."
                if selected_publication != "disabled"
                else "This web runtime cannot write to DataHub or submit publication work; "
                "the publisher queue is not implemented."
            ),
        ),
    )
    views = JudgeUiViewFactory(
        reference=build_ui_reference_data(
            candidate_report,
            context,
            activation_generation=scoped_context.activation_generation,
            active_pointer_fingerprint=scoped_context.active_pointer_fingerprint,
            projection_status=projection_status,
        ),
        prepare=prepare,
        modes=modes,
    )
    from schemabridge.application.ports.workflow_access import WorkflowAccessError

    try:
        access_store = build_workflow_access_store(
            workspace_id=resolved_principal.workspace_id,
            owner_actor_id=resolved_principal.actor_id,
            settings=resolved,
        )
    except WorkflowAccessError as error:
        raise RuntimeError("workflow control plane is unavailable") from error

    return JudgeUiService(
        orchestrator_factory=lambda: build_agent_workflow_orchestrator(
            selected_catalog,
            publication_kind=selected_publication,
            execution_kind=selected_execution,
            workflow_workspace_id=resolved_principal.workspace_id,
            workflow_owner_actor_id=resolved_principal.actor_id,
            repository_root=root,
            settings=resolved,
        ),
        views=views,
        principal=resolved_principal,
        access_store=access_store,
        authorization=authorization,
        clock=clock,
        execution_actions_enabled=selected_execution != "disabled",
        publication_actions_enabled=selected_publication != "disabled",
        require_separate_publisher=selected_publication == "live",
        max_live_publication_identity_age=timedelta(
            seconds=resolved.oidc_live_publication_max_identity_age_seconds
        ),
    )
