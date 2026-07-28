"""Typed runtime configuration."""

import ipaddress
import os
import re
from pathlib import Path
from typing import Literal, Self, TypeAlias
from urllib.parse import parse_qs, urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from schemabridge.domain.catalog_inventory import (
    CATALOG_CONNECTION_ID_PATTERN,
    CATALOG_INDEXER_ID_PATTERN,
)
from schemabridge.domain.semantic_profile_jobs import (
    validate_semantic_join_profile_workspace_id,
)

RuntimeProfile: TypeAlias = Literal["development", "hosted-demo", "staging", "production"]
RuntimeComponent: TypeAlias = Literal["web", "api", "worker", "catalog", "reconciler"]
AuthMode: TypeAlias = Literal["local-demo", "oidc"]
WorkerIdentityLineageMode: TypeAlias = Literal["exact-local", "verified-oidc"]
ApiJwtAlgorithm: TypeAlias = Literal["RS256", "ES256"]
CatalogMode: TypeAlias = Literal["recorded", "live"]
RegistryMode: TypeAlias = Literal["recorded", "live"]
RegistrySelectionMode: TypeAlias = Literal["fixed", "active"]
PublicationMode: TypeAlias = Literal["fake", "live"]
ExecutionMode: TypeAlias = Literal["recorded", "live"]
ControlPlaneMode: TypeAlias = Literal["local", "postgres"]
QueryStudioAiMode: TypeAlias = Literal["disabled", "fake", "live"]
QueryStudioAiRegion: TypeAlias = Literal["global", "eu", "us"]
RoleName: TypeAlias = Literal[
    "analyst",
    "steward",
    "publisher",
    "auditor",
    "platform_admin",
]

MAX_OIDC_ALLOWED_TENANTS = 256
MAX_OIDC_TENANT_LENGTH = 120
MINIMUM_PSEUDONYM_KEY_DISTINCT_BYTES = 8
CONTROL_PLANE_SCHEMA = "schemabridge_control"


class Settings(BaseSettings):
    """Environment-backed application settings.

    Secrets are intentionally not given non-empty defaults.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
        hide_input_in_errors=True,
    )

    environment: RuntimeProfile = Field(
        default="development",
        alias="SCHEMABRIDGE_ENVIRONMENT",
    )
    runtime_component: RuntimeComponent = Field(
        default="web",
        alias="SCHEMABRIDGE_COMPONENT",
    )
    log_level: str = Field(default="INFO", alias="SCHEMABRIDGE_LOG_LEVEL")
    max_query_rows: int = Field(default=500, ge=1, le=10_000, alias="SCHEMABRIDGE_MAX_QUERY_ROWS")
    statement_timeout_ms: int = Field(
        default=5_000,
        ge=100,
        le=60_000,
        alias="SCHEMABRIDGE_STATEMENT_TIMEOUT_MS",
    )
    max_query_tables: int = Field(default=3, ge=1, le=3, alias="SCHEMABRIDGE_MAX_QUERY_TABLES")
    draft_store_path: Path = Field(
        default=Path(".local/schemabridge.db"),
        alias="SCHEMABRIDGE_DRAFT_STORE_PATH",
    )
    database_url: str | None = Field(default=None, alias="DATABASE_URL", repr=False)
    connector_secret_directory: Path | None = Field(
        default=None,
        alias="SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY",
        repr=False,
    )
    postgres_reader_user: str = Field(
        default="schemabridge_reader",
        alias="POSTGRES_READER_USER",
    )
    datahub_gms_url: str = Field(default="http://localhost:8080", alias="DATAHUB_GMS_URL")
    datahub_gms_token: str | None = Field(default=None, alias="DATAHUB_GMS_TOKEN", repr=False)
    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    llm_model: str | None = Field(default=None, alias="SCHEMABRIDGE_LLM_MODEL")
    query_studio_ai_mode: QueryStudioAiMode = Field(
        default="fake",
        alias="SCHEMABRIDGE_QUERY_STUDIO_AI_MODE",
    )
    query_studio_ai_model: str = Field(
        default="gpt-5-nano-2025-08-07",
        alias="SCHEMABRIDGE_QUERY_STUDIO_AI_MODEL",
    )
    query_studio_ai_region: QueryStudioAiRegion = Field(
        default="global",
        alias="SCHEMABRIDGE_QUERY_STUDIO_AI_REGION",
    )
    query_studio_signing_key: SecretStr | None = Field(
        default=None,
        alias="SCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY",
    )
    auth_mode: AuthMode = Field(
        default="local-demo",
        alias="SCHEMABRIDGE_AUTH_MODE",
    )
    catalog_kind: CatalogMode = Field(
        default="recorded",
        alias="SCHEMABRIDGE_CATALOG_MODE",
    )
    registry_kind: RegistryMode = Field(
        default="recorded",
        alias="SCHEMABRIDGE_REGISTRY_MODE",
    )
    publication_kind: PublicationMode = Field(
        default="fake",
        alias="SCHEMABRIDGE_PUBLICATION_MODE",
    )
    judge_execution_kind: ExecutionMode = Field(
        default="recorded",
        alias="SCHEMABRIDGE_JUDGE_EXECUTION",
    )
    allow_local_live_reads: bool = Field(
        default=False,
        alias="SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS",
    )
    local_workspace: str = Field(
        default="local-demo",
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        alias="SCHEMABRIDGE_LOCAL_WORKSPACE",
    )
    semantic_registry_id: str = Field(
        default="synthetic_enterprise",
        min_length=3,
        max_length=80,
        pattern=r"^[a-z][a-z0-9_]{2,79}$",
        alias="SCHEMABRIDGE_SEMANTIC_REGISTRY_ID",
    )
    semantic_registry_catalog_scope: str = Field(
        default="synthetic-demo",
        min_length=3,
        max_length=120,
        pattern=r"^[a-z][a-z0-9_.:-]{2,119}$",
        alias="SCHEMABRIDGE_SEMANTIC_REGISTRY_CATALOG_SCOPE",
    )
    semantic_registry_manifest_path: Path = Field(
        default=Path("demo/ground_truth/registries/manifest.yml"),
        alias="SCHEMABRIDGE_SEMANTIC_REGISTRY_MANIFEST_PATH",
    )
    semantic_registry_version: int = Field(
        default=1,
        ge=1,
        alias="SCHEMABRIDGE_SEMANTIC_REGISTRY_VERSION",
    )
    semantic_registry_selection: RegistrySelectionMode = Field(
        default="fixed",
        alias="SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION",
    )
    semantic_registry_reader_env_path: Path = Field(
        default=Path(".local/datahub/mcp.env"),
        alias="SCHEMABRIDGE_SEMANTIC_REGISTRY_READER_ENV_PATH",
    )
    control_plane_kind: ControlPlaneMode = Field(
        default="local",
        alias="SCHEMABRIDGE_CONTROL_PLANE_MODE",
    )
    control_database_url: SecretStr | None = Field(
        default=None,
        alias="SCHEMABRIDGE_CONTROL_DATABASE_URL",
    )
    control_reconciler_database_url: SecretStr | None = Field(
        default=None,
        alias="SCHEMABRIDGE_CONTROL_RECONCILER_DATABASE_URL",
    )
    control_migrator_database_url: SecretStr | None = Field(
        default=None,
        alias="SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL",
    )
    control_api_database_url: SecretStr | None = Field(
        default=None,
        alias="SCHEMABRIDGE_CONTROL_API_DATABASE_URL",
    )
    control_worker_database_url: SecretStr | None = Field(
        default=None,
        alias="SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL",
    )
    control_catalog_database_url: SecretStr | None = Field(
        default=None,
        alias="SCHEMABRIDGE_CONTROL_CATALOG_DATABASE_URL",
    )
    control_restore_database_url: SecretStr | None = Field(
        default=None,
        alias="SCHEMABRIDGE_CONTROL_RESTORE_DATABASE_URL",
    )
    control_plane_schema: str = Field(
        default="schemabridge_control",
        min_length=3,
        max_length=63,
        pattern=r"^[a-z][a-z0-9_]{2,62}$",
        alias="SCHEMABRIDGE_CONTROL_PLANE_SCHEMA",
    )
    control_plane_schema_version: int = Field(
        default=9,
        ge=1,
        le=10_000,
        alias="SCHEMABRIDGE_CONTROL_PLANE_SCHEMA_VERSION",
    )
    control_pool_min_size: int = Field(
        default=1,
        ge=1,
        le=64,
        alias="SCHEMABRIDGE_CONTROL_POOL_MIN_SIZE",
    )
    control_pool_max_size: int = Field(
        default=8,
        ge=1,
        le=64,
        alias="SCHEMABRIDGE_CONTROL_POOL_MAX_SIZE",
    )
    control_pool_max_waiting: int = Field(
        default=32,
        ge=1,
        le=10_000,
        alias="SCHEMABRIDGE_CONTROL_POOL_MAX_WAITING",
    )
    control_pool_acquisition_timeout_seconds: float = Field(
        default=2.0,
        ge=0.05,
        le=30,
        alias="SCHEMABRIDGE_CONTROL_POOL_ACQUISITION_TIMEOUT_SECONDS",
    )
    control_pool_startup_timeout_seconds: float = Field(
        default=10.0,
        ge=1,
        le=60,
        alias="SCHEMABRIDGE_CONTROL_POOL_STARTUP_TIMEOUT_SECONDS",
    )
    control_pool_close_timeout_seconds: float = Field(
        default=5.0,
        ge=0.1,
        le=30,
        alias="SCHEMABRIDGE_CONTROL_POOL_CLOSE_TIMEOUT_SECONDS",
    )
    control_pool_max_idle_seconds: float = Field(
        default=300.0,
        ge=1,
        le=3_600,
        alias="SCHEMABRIDGE_CONTROL_POOL_MAX_IDLE_SECONDS",
    )
    control_pool_max_lifetime_seconds: float = Field(
        default=1_800.0,
        ge=60,
        le=86_400,
        alias="SCHEMABRIDGE_CONTROL_POOL_MAX_LIFETIME_SECONDS",
    )
    control_audit_signing_key: SecretStr | None = Field(
        default=None,
        alias="SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY",
    )
    control_audit_key_version: str = Field(
        default="v1",
        pattern=r"^v[1-9][0-9]{0,5}$",
        alias="SCHEMABRIDGE_CONTROL_AUDIT_KEY_VERSION",
    )
    control_operator_actor_id: str | None = Field(
        default=None,
        pattern=r"^sb_actor_v[1-9][0-9]{0,5}_[0-9a-f]{64}$",
        alias="SCHEMABRIDGE_CONTROL_OPERATOR_ACTOR_ID",
    )
    control_operator_roles: tuple[RoleName, ...] = Field(
        default=(),
        alias="SCHEMABRIDGE_CONTROL_OPERATOR_ROLES",
    )
    identity_migration_key: SecretStr | None = Field(
        default=None,
        alias="SCHEMABRIDGE_IDENTITY_MIGRATION_KEY",
    )
    identity_migration_key_version: str = Field(
        default="v1",
        pattern=r"^v[1-9][0-9]{0,5}$",
        alias="SCHEMABRIDGE_IDENTITY_MIGRATION_KEY_VERSION",
    )
    identity_policy_version: str = Field(
        default="v1",
        pattern=r"^v[1-9][0-9]{0,5}$",
        alias="SCHEMABRIDGE_IDENTITY_POLICY_VERSION",
    )
    local_roles: tuple[RoleName, ...] = Field(
        default=("analyst", "publisher"),
        min_length=1,
        alias="SCHEMABRIDGE_LOCAL_ROLES",
    )
    local_subject: str = Field(
        default="operator",
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        alias="SCHEMABRIDGE_LOCAL_SUBJECT",
    )
    oidc_issuer: str | None = Field(
        default=None,
        min_length=1,
        max_length=2_048,
        alias="SCHEMABRIDGE_OIDC_ISSUER",
    )
    oidc_audience: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        alias="SCHEMABRIDGE_OIDC_AUDIENCE",
    )
    oidc_provider: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]*$",
        alias="SCHEMABRIDGE_OIDC_PROVIDER",
    )
    oidc_role_claim: str = Field(
        default="roles",
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z_][A-Za-z0-9_.:-]*$",
        alias="SCHEMABRIDGE_OIDC_ROLE_CLAIM",
    )
    oidc_tenant_claim: str = Field(
        default="tenant_id",
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z_][A-Za-z0-9_.:-]*$",
        alias="SCHEMABRIDGE_OIDC_TENANT_CLAIM",
    )
    oidc_allowed_groups: dict[str, tuple[RoleName, ...]] = Field(
        default_factory=dict,
        alias="SCHEMABRIDGE_OIDC_ALLOWED_GROUPS",
    )
    oidc_allowed_tenants: tuple[str, ...] = Field(
        default=(),
        max_length=MAX_OIDC_ALLOWED_TENANTS,
        alias="SCHEMABRIDGE_OIDC_ALLOWED_TENANTS",
    )
    oidc_max_session_age_seconds: int = Field(
        default=3_600,
        ge=300,
        le=3_600,
        alias="SCHEMABRIDGE_OIDC_MAX_SESSION_AGE_SECONDS",
    )
    oidc_live_publication_max_identity_age_seconds: int = Field(
        default=900,
        ge=60,
        le=3_600,
        alias="SCHEMABRIDGE_OIDC_LIVE_PUBLICATION_MAX_IDENTITY_AGE_SECONDS",
    )
    api_local_bearer_token: SecretStr | None = Field(
        default=None,
        alias="SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN",
    )
    inventory_cursor_signing_key: SecretStr | None = Field(
        default=None,
        alias="SCHEMABRIDGE_INVENTORY_CURSOR_SIGNING_KEY",
    )
    api_oidc_jwks_url: str | None = Field(
        default=None,
        min_length=1,
        max_length=2_048,
        alias="SCHEMABRIDGE_API_OIDC_JWKS_URL",
    )
    api_oidc_algorithms: tuple[ApiJwtAlgorithm, ...] = Field(
        default=("RS256",),
        min_length=1,
        max_length=2,
        alias="SCHEMABRIDGE_API_OIDC_ALGORITHMS",
    )
    api_max_request_bytes: int = Field(
        default=65_536,
        ge=1_024,
        le=1_048_576,
        alias="SCHEMABRIDGE_API_MAX_REQUEST_BYTES",
    )
    api_bind_host: str = Field(
        default="127.0.0.1",
        min_length=1,
        max_length=253,
        alias="SCHEMABRIDGE_API_BIND_HOST",
    )
    api_port: int = Field(
        default=8520,
        ge=1_024,
        le=65_535,
        alias="SCHEMABRIDGE_API_PORT",
    )
    api_limit_concurrency: int = Field(
        default=100,
        ge=1,
        le=1_000,
        alias="SCHEMABRIDGE_API_LIMIT_CONCURRENCY",
    )
    api_graceful_shutdown_seconds: int = Field(
        default=20,
        ge=1,
        le=120,
        alias="SCHEMABRIDGE_API_GRACEFUL_SHUTDOWN_SECONDS",
    )
    api_docs_enabled: bool = Field(
        default=False,
        alias="SCHEMABRIDGE_API_DOCS_ENABLED",
    )
    api_allowed_hosts: tuple[str, ...] = Field(
        default=("127.0.0.1", "localhost"),
        min_length=1,
        max_length=32,
        alias="SCHEMABRIDGE_API_ALLOWED_HOSTS",
    )
    api_job_authorization_ttl_seconds: int = Field(
        default=300,
        ge=30,
        le=900,
        alias="SCHEMABRIDGE_API_JOB_AUTHORIZATION_TTL_SECONDS",
    )
    worker_id: str = Field(
        default="worker-local",
        min_length=3,
        max_length=120,
        pattern=r"^[a-z0-9][a-z0-9_-]{2,119}$",
        alias="SCHEMABRIDGE_WORKER_ID",
    )
    worker_identity_lineage_mode: WorkerIdentityLineageMode = Field(
        default="exact-local",
        alias="SCHEMABRIDGE_WORKER_IDENTITY_LINEAGE_MODE",
    )
    worker_lease_seconds: int = Field(
        default=120,
        ge=5,
        le=300,
        alias="SCHEMABRIDGE_WORKER_LEASE_SECONDS",
    )
    worker_heartbeat_seconds: int = Field(
        default=30,
        ge=1,
        le=120,
        alias="SCHEMABRIDGE_WORKER_HEARTBEAT_SECONDS",
    )
    worker_max_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        alias="SCHEMABRIDGE_WORKER_MAX_ATTEMPTS",
    )
    worker_poll_interval_ms: int = Field(
        default=500,
        ge=50,
        le=10_000,
        alias="SCHEMABRIDGE_WORKER_POLL_INTERVAL_MS",
    )
    semantic_profile_max_attempts: int = Field(
        default=5,
        ge=1,
        le=100,
        alias="SCHEMABRIDGE_SEMANTIC_PROFILE_MAX_ATTEMPTS",
    )
    semantic_profile_source_workspace_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        alias="SCHEMABRIDGE_SEMANTIC_PROFILE_SOURCE_WORKSPACE_ID",
    )
    semantic_profile_source_connection_id: str | None = Field(
        default=None,
        min_length=3,
        max_length=200,
        pattern=CATALOG_CONNECTION_ID_PATTERN,
        alias="SCHEMABRIDGE_SEMANTIC_PROFILE_SOURCE_CONNECTION_ID",
    )
    semantic_profile_retention_days: int = Field(
        default=30,
        ge=1,
        le=366,
        alias="SCHEMABRIDGE_SEMANTIC_PROFILE_RETENTION_DAYS",
    )
    semantic_profile_maintenance_batch_size: int = Field(
        default=100,
        ge=1,
        le=1_000,
        alias="SCHEMABRIDGE_SEMANTIC_PROFILE_MAINTENANCE_BATCH_SIZE",
    )
    semantic_reconciler_id: str = Field(
        default="semantic-reconciler-local",
        min_length=3,
        max_length=120,
        pattern=r"^[a-z0-9][a-z0-9_-]{2,119}$",
        alias="SCHEMABRIDGE_SEMANTIC_RECONCILER_ID",
    )
    semantic_reconciler_lease_seconds: int = Field(
        default=60,
        ge=10,
        le=300,
        alias="SCHEMABRIDGE_SEMANTIC_RECONCILER_LEASE_SECONDS",
    )
    semantic_reconciler_poll_interval_ms: int = Field(
        default=1_000,
        ge=100,
        le=60_000,
        alias="SCHEMABRIDGE_SEMANTIC_RECONCILER_POLL_INTERVAL_MS",
    )
    semantic_reconciler_retention_days: int = Field(
        default=30,
        ge=1,
        le=366,
        alias="SCHEMABRIDGE_SEMANTIC_RECONCILER_RETENTION_DAYS",
    )
    semantic_reconciler_maintenance_batch_size: int = Field(
        default=100,
        ge=1,
        le=1_000,
        alias="SCHEMABRIDGE_SEMANTIC_RECONCILER_MAINTENANCE_BATCH_SIZE",
    )
    catalog_indexer_id: str = Field(
        default="catalog-local",
        min_length=3,
        max_length=200,
        pattern=CATALOG_INDEXER_ID_PATTERN,
        alias="SCHEMABRIDGE_CATALOG_INDEXER_ID",
    )
    catalog_poll_interval_ms: int = Field(
        default=1_000,
        ge=100,
        le=60_000,
        alias="SCHEMABRIDGE_CATALOG_POLL_INTERVAL_MS",
    )
    catalog_lease_seconds: int = Field(
        default=120,
        ge=10,
        le=300,
        alias="SCHEMABRIDGE_CATALOG_LEASE_SECONDS",
    )
    catalog_page_size: int = Field(
        default=50,
        ge=1,
        le=50,
        alias="SCHEMABRIDGE_CATALOG_PAGE_SIZE",
    )
    catalog_source_timeout_seconds: float = Field(
        default=15.0,
        ge=0.1,
        le=30,
        alias="SCHEMABRIDGE_CATALOG_SOURCE_TIMEOUT_SECONDS",
    )
    catalog_max_response_bytes: int = Field(
        default=4_194_304,
        ge=65_536,
        le=8_388_608,
        alias="SCHEMABRIDGE_CATALOG_MAX_RESPONSE_BYTES",
    )
    retired_catalog_datahub_credential_binding_ref: str | None = Field(
        default=None,
        alias="SCHEMABRIDGE_CATALOG_DATAHUB_CREDENTIAL_BINDING_REF",
        exclude=True,
        repr=False,
    )
    catalog_stale_after_seconds: int = Field(
        default=900,
        ge=60,
        le=2_592_000,
        alias="SCHEMABRIDGE_CATALOG_STALE_AFTER_SECONDS",
    )
    catalog_synthetic_asset_counts: dict[str, int] = Field(
        default_factory=dict,
        alias="SCHEMABRIDGE_CATALOG_SYNTHETIC_ASSET_COUNTS",
    )
    pseudonymization_key: SecretStr | None = Field(
        default=None,
        alias="SCHEMABRIDGE_PSEUDONYMIZATION_KEY",
    )
    pseudonymization_key_version: str = Field(
        default="v1",
        pattern=r"^v[1-9][0-9]{0,5}$",
        alias="SCHEMABRIDGE_PSEUDONYMIZATION_KEY_VERSION",
    )
    release_ref: str = Field(
        default="working-tree-uncommitted",
        min_length=1,
        max_length=120,
        alias="SCHEMABRIDGE_RELEASE_REF",
    )

    @property
    def runtime_profile(self) -> RuntimeProfile:
        """Return the single deployment profile under its composition-facing name."""

        return self.environment

    @property
    def catalog_mode(self) -> CatalogMode:
        """Return the server-selected catalog integration mode."""

        return self.catalog_kind

    @property
    def publication_mode(self) -> PublicationMode:
        """Return the server-selected publication integration mode."""

        return self.publication_kind

    @property
    def registry_mode(self) -> RegistryMode:
        """Return the explicitly selected semantic-registry adapter mode."""

        return self.registry_kind

    @property
    def execution_mode(self) -> ExecutionMode:
        """Return the server-selected query execution mode."""

        return self.judge_execution_kind

    @field_validator("semantic_profile_source_workspace_id")
    @classmethod
    def semantic_profile_workspace_must_be_bounded(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None
        return validate_semantic_join_profile_workspace_id(value)

    @model_validator(mode="after")
    def validate_identity_and_integration_profile(self) -> Self:
        """Resolve safe profile defaults and reject insecure combinations."""

        if self.environment == "hosted-demo":
            self._apply_hosted_demo_profile()

        if (
            self.environment in {"staging", "production"}
            and self.runtime_component not in {"worker", "catalog", "reconciler"}
            and self.auth_mode != "oidc"
        ):
            raise ValueError(f"{self.environment} requires SCHEMABRIDGE_AUTH_MODE=oidc")
        if (
            self.runtime_component in {"worker", "catalog", "reconciler"}
            and self.auth_mode != "local-demo"
        ):
            raise ValueError(
                f"{self.runtime_component} component cannot receive OIDC authentication material"
            )
        if (
            self.environment in {"staging", "production"}
            and self.runtime_component == "worker"
            and self.worker_identity_lineage_mode != "verified-oidc"
        ):
            raise ValueError(
                f"{self.environment} worker requires "
                "SCHEMABRIDGE_WORKER_IDENTITY_LINEAGE_MODE=verified-oidc"
            )
        if self.environment in {"staging", "production"}:
            if "catalog_kind" in self.model_fields_set and self.catalog_kind != "live":
                raise ValueError(f"{self.environment} requires SCHEMABRIDGE_CATALOG_MODE=live")
            if "registry_kind" in self.model_fields_set and self.registry_kind != "live":
                raise ValueError(f"{self.environment} requires SCHEMABRIDGE_REGISTRY_MODE=live")
            if (
                "semantic_registry_selection" in self.model_fields_set
                and self.semantic_registry_selection != "active"
            ):
                raise ValueError(
                    f"{self.environment} requires SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION=active"
                )
            if (
                "control_plane_kind" in self.model_fields_set
                and self.control_plane_kind != "postgres"
            ):
                raise ValueError(
                    f"{self.environment} requires SCHEMABRIDGE_CONTROL_PLANE_MODE=postgres"
                )
            self.catalog_kind = "live"
            self.registry_kind = "live"
            self.semantic_registry_selection = "active"
            self.control_plane_kind = "postgres"

        self._validate_query_studio_configuration()

        if self.publication_kind == "live" and self.auth_mode != "oidc":
            raise ValueError("live publication requires SCHEMABRIDGE_AUTH_MODE=oidc")

        local_live_reads = (
            self.catalog_kind == "live"
            or self.registry_kind == "live"
            or self.judge_execution_kind == "live"
        )
        if (
            self.environment == "development"
            and local_live_reads
            and not self.allow_local_live_reads
        ):
            raise ValueError(
                "local live reads require development and SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS=true"
            )

        if self.auth_mode == "oidc" and self.runtime_component not in {
            "worker",
            "catalog",
            "reconciler",
        }:
            self._validate_oidc_metadata()

        self._validate_api_configuration()
        self._validate_control_plane()
        self._validate_role_configuration()
        return self

    def _apply_hosted_demo_profile(self) -> None:
        forced = {
            "auth_mode": "local-demo",
            "catalog_kind": "recorded",
            "registry_kind": "recorded",
            "publication_kind": "fake",
            "judge_execution_kind": "recorded",
            "semantic_registry_selection": "fixed",
            "control_plane_kind": "local",
            "query_studio_ai_mode": "fake",
        }
        for field_name, expected in forced.items():
            if field_name in self.model_fields_set and getattr(self, field_name) != expected:
                raise ValueError(
                    f"hosted-demo requires {field_name}={expected!r}; "
                    "deployment modes cannot be overridden"
                )

        self.auth_mode = "local-demo"
        self.catalog_kind = "recorded"
        self.registry_kind = "recorded"
        self.publication_kind = "fake"
        self.judge_execution_kind = "recorded"
        self.semantic_registry_selection = "fixed"
        self.control_plane_kind = "local"
        self.query_studio_ai_mode = "fake"

    def _validate_query_studio_configuration(self) -> None:
        allowed_models = {
            "gpt-5-nano-2025-08-07",
            "gpt-5.4-nano-2026-03-17",
            "gpt-5.6-luna",
        }
        if self.query_studio_ai_model not in allowed_models:
            raise ValueError(
                "SCHEMABRIDGE_QUERY_STUDIO_AI_MODEL must be an evaluated pinned snapshot"
            )
        if self.environment in {"staging", "production"} and self.query_studio_ai_mode == "fake":
            if "query_studio_ai_mode" in self.model_fields_set:
                raise ValueError(
                    "staging and production cannot use the synthetic Query Studio parser"
                )
            self.query_studio_ai_mode = "disabled"
        if self.query_studio_ai_mode == "live":
            if self.runtime_component != "web":
                raise ValueError("live Query Studio AI is available only to the web runtime")
            if self.control_plane_kind != "postgres":
                raise ValueError(
                    "live Query Studio AI requires the durable PostgreSQL control plane"
                )
            if self.openai_api_key is None:
                raise ValueError("live Query Studio AI requires OPENAI_API_KEY")
            if self.pseudonymization_key is None:
                raise ValueError("live Query Studio AI requires SCHEMABRIDGE_PSEUDONYMIZATION_KEY")
            _validate_strong_secret(
                self.pseudonymization_key.get_secret_value(),
                "SCHEMABRIDGE_PSEUDONYMIZATION_KEY",
            )
        if self.query_studio_signing_key is not None:
            _validate_strong_secret(
                self.query_studio_signing_key.get_secret_value(),
                "SCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY",
            )
            if (
                self.pseudonymization_key is not None
                and self.query_studio_signing_key.get_secret_value()
                == self.pseudonymization_key.get_secret_value()
            ):
                raise ValueError("Query Studio signing and pseudonymization keys must be distinct")
        elif self.environment in {"staging", "production"} and self.runtime_component == "web":
            raise ValueError("managed Query Studio requires SCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY")

    def _validate_oidc_metadata(self) -> None:
        required = {
            "SCHEMABRIDGE_OIDC_ISSUER": self.oidc_issuer,
            "SCHEMABRIDGE_OIDC_AUDIENCE": self.oidc_audience,
            "SCHEMABRIDGE_OIDC_PROVIDER": self.oidc_provider,
        }
        missing = tuple(name for name, value in required.items() if value is None)
        if missing:
            raise ValueError(f"OIDC mode requires {', '.join(missing)}")
        if not self.oidc_allowed_groups:
            raise ValueError("OIDC mode requires a non-empty SCHEMABRIDGE_OIDC_ALLOWED_GROUPS map")
        if not self.oidc_allowed_tenants:
            raise ValueError(
                "OIDC mode requires a non-empty SCHEMABRIDGE_OIDC_ALLOWED_TENANTS tuple"
            )
        if len(self.oidc_allowed_tenants) != len(set(self.oidc_allowed_tenants)):
            raise ValueError("SCHEMABRIDGE_OIDC_ALLOWED_TENANTS cannot contain duplicates")
        if any(
            tenant != tenant.strip() or not tenant or len(tenant) > MAX_OIDC_TENANT_LENGTH
            for tenant in self.oidc_allowed_tenants
        ):
            raise ValueError(
                "OIDC allowlisted tenants must be non-empty, trimmed, and at most "
                f"{MAX_OIDC_TENANT_LENGTH} characters"
            )
        if self.pseudonymization_key is None:
            raise ValueError("OIDC mode requires SCHEMABRIDGE_PSEUDONYMIZATION_KEY")
        pseudonym_key = self.pseudonymization_key.get_secret_value().encode()
        if (
            len(pseudonym_key) < 32
            or _looks_like_placeholder(self.pseudonymization_key.get_secret_value())
            or len(set(pseudonym_key)) < MINIMUM_PSEUDONYM_KEY_DISTINCT_BYTES
        ):
            raise ValueError("SCHEMABRIDGE_PSEUDONYMIZATION_KEY is not acceptable")
        assert self.oidc_issuer is not None
        issuer = urlsplit(self.oidc_issuer)
        if (
            issuer.scheme not in {"http", "https"}
            or issuer.hostname is None
            or issuer.username is not None
            or issuer.password is not None
            or issuer.query
            or issuer.fragment
        ):
            raise ValueError("OIDC issuer must be an absolute HTTP(S) URL without credentials")
        if self.environment in {"staging", "production"} and issuer.scheme != "https":
            raise ValueError("staging and production OIDC issuers must use HTTPS")

    def _validate_role_configuration(self) -> None:
        if len(self.local_roles) != len(set(self.local_roles)):
            raise ValueError("SCHEMABRIDGE_LOCAL_ROLES cannot contain duplicates")
        for group, roles in self.oidc_allowed_groups.items():
            if not group or group != group.strip():
                raise ValueError("OIDC allowlisted group names must be non-empty and trimmed")
            if not roles:
                raise ValueError(f"OIDC allowlisted group {group!r} must grant at least one role")
            if len(roles) != len(set(roles)):
                raise ValueError(f"OIDC allowlisted group {group!r} cannot repeat a role")

    def _validate_api_configuration(self) -> None:
        if len(self.api_oidc_algorithms) != len(set(self.api_oidc_algorithms)):
            raise ValueError("SCHEMABRIDGE_API_OIDC_ALGORITHMS cannot contain duplicates")
        if self.worker_heartbeat_seconds * 2 >= self.worker_lease_seconds:
            raise ValueError("worker heartbeat must be less than half of the configured lease")
        minimum_query_lease = (self.statement_timeout_ms + 999) // 1_000 + 5
        if self.worker_lease_seconds < minimum_query_lease:
            raise ValueError("worker lease must exceed the maximum source statement timeout")
        if len(self.api_allowed_hosts) != len(set(self.api_allowed_hosts)):
            raise ValueError("SCHEMABRIDGE_API_ALLOWED_HOSTS cannot contain duplicates")
        try:
            ipaddress.ip_address(self.api_bind_host)
        except ValueError as error:
            raise ValueError("API bind host must be an explicit IP address") from error
        if any(
            not host or host != host.strip() or len(host) > 253 or "/" in host or "://" in host
            for host in self.api_allowed_hosts
        ):
            raise ValueError("API allowed hosts must be exact host names without a scheme or path")
        if self.api_docs_enabled and self.environment in {"staging", "production"}:
            raise ValueError("interactive API documentation is forbidden in staging and production")
        if self.api_local_bearer_token is not None:
            if self.environment != "development" or self.auth_mode != "local-demo":
                raise ValueError("local API bearer authentication is available only in development")
            _validate_strong_secret(
                self.api_local_bearer_token.get_secret_value(),
                "SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN",
            )
        if self.inventory_cursor_signing_key is not None:
            if self.runtime_component != "api":
                raise ValueError("inventory cursor signing key is available only to the API")
            cursor_key = self.inventory_cursor_signing_key.get_secret_value()
            _validate_strong_secret(
                cursor_key,
                "SCHEMABRIDGE_INVENTORY_CURSOR_SIGNING_KEY",
            )
            if (
                self.api_local_bearer_token is not None
                and cursor_key == self.api_local_bearer_token.get_secret_value()
            ):
                raise ValueError("inventory cursor and bearer keys must be distinct")
            if (
                self.pseudonymization_key is not None
                and cursor_key == self.pseudonymization_key.get_secret_value()
            ):
                raise ValueError("inventory cursor and pseudonymization keys must be distinct")
        if self.api_oidc_jwks_url is not None:
            jwks = urlsplit(self.api_oidc_jwks_url)
            if (
                jwks.scheme not in {"http", "https"}
                or jwks.hostname is None
                or jwks.username is not None
                or jwks.password is not None
                or jwks.query
                or jwks.fragment
            ):
                raise ValueError(
                    "API OIDC JWKS URL must be an absolute HTTP(S) URL without credentials"
                )
            if self.environment in {"staging", "production"} and jwks.scheme != "https":
                raise ValueError("staging and production API JWKS URLs must use HTTPS")
            if self.oidc_issuer is not None:
                issuer = urlsplit(self.oidc_issuer)
                issuer_origin = (issuer.scheme, issuer.hostname, issuer.port)
                jwks_origin = (jwks.scheme, jwks.hostname, jwks.port)
                if issuer_origin != jwks_origin:
                    raise ValueError("API JWKS URL must use the configured OIDC issuer origin")

    def _validate_control_plane(self) -> None:
        managed = self.environment in {"staging", "production"}
        active = self.semantic_registry_selection == "active"
        postgres = self.control_plane_kind == "postgres"
        if active and self.registry_kind != "live":
            raise ValueError("active semantic registry selection requires live registry mode")
        if active and not postgres:
            raise ValueError("active semantic registry selection requires PostgreSQL control plane")
        if self.runtime_component in {"api", "worker", "catalog", "reconciler"} and not postgres:
            raise ValueError(
                f"{self.runtime_component} component requires PostgreSQL control plane"
            )
        if not postgres:
            if managed:
                raise ValueError(f"{self.environment} requires PostgreSQL control plane")
            return
        if self.control_pool_min_size > self.control_pool_max_size:
            raise ValueError("control pool minimum size cannot exceed its maximum size")

        control_label, control_variable, selected_control_url = {
            "web": (
                "control database",
                "SCHEMABRIDGE_CONTROL_DATABASE_URL",
                self.control_database_url,
            ),
            "api": (
                "API database",
                "SCHEMABRIDGE_CONTROL_API_DATABASE_URL",
                self.control_api_database_url,
            ),
            "worker": (
                "worker database",
                "SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL",
                self.control_worker_database_url,
            ),
            "catalog": (
                "catalog database",
                "SCHEMABRIDGE_CONTROL_CATALOG_DATABASE_URL",
                self.control_catalog_database_url,
            ),
            "reconciler": (
                "reconciler database",
                "SCHEMABRIDGE_CONTROL_RECONCILER_DATABASE_URL",
                self.control_reconciler_database_url,
            ),
        }[self.runtime_component]
        if selected_control_url is None:
            raise ValueError(
                f"PostgreSQL {self.runtime_component} component requires {control_variable}"
            )
        requires_connector_secrets = managed and (
            (active and self.runtime_component in {"web", "worker"})
            or (self.runtime_component == "catalog" and self.catalog_kind == "live")
        )
        if requires_connector_secrets and (
            self.connector_secret_directory is None
            or not self.connector_secret_directory.is_absolute()
        ):
            raise ValueError(
                "managed connector routing requires an absolute "
                "SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY"
            )
        self._reject_cross_component_credentials()
        if self.control_plane_schema != CONTROL_PLANE_SCHEMA:
            raise ValueError(
                "this release supports only the schemabridge_control control-plane schema"
            )
        control_url = selected_control_url.get_secret_value()
        control_identity = _postgres_database_identity(
            control_url,
            require_tls=managed,
            label=control_label,
        )
        control_username = urlsplit(control_url).username
        control_users = {control_username}
        for label, operator_url in (
            ("runtime database", self.control_database_url),
            ("reconciler database", self.control_reconciler_database_url),
            ("migrator database", self.control_migrator_database_url),
            ("API database", self.control_api_database_url),
            ("worker database", self.control_worker_database_url),
            ("catalog database", self.control_catalog_database_url),
        ):
            if operator_url is None or operator_url is selected_control_url:
                continue
            raw_operator_url = operator_url.get_secret_value()
            operator_identity = _postgres_database_identity(
                raw_operator_url,
                require_tls=managed,
                label=label,
            )
            if operator_identity != control_identity:
                raise ValueError(f"{label} URL must identify the configured control database")
            operator_username = urlsplit(raw_operator_url).username
            if operator_username is None or operator_username in control_users:
                raise ValueError("control-plane credentials must use distinct roles")
            control_users.add(operator_username)
        restore_identity: tuple[str, int, str] | None = None
        if self.control_restore_database_url is not None:
            restore_identity = _postgres_database_identity(
                self.control_restore_database_url.get_secret_value(),
                require_tls=managed,
                label="restore target database",
            )
            if restore_identity == control_identity:
                raise ValueError(
                    "restore target database must be distinct from the active control database"
                )
        if self.database_url is not None:
            source_identity = _postgres_database_identity(
                self.database_url,
                require_tls=managed,
                label="source database",
            )
            if control_identity == source_identity:
                raise ValueError("control database must be separate from every source database")
            if restore_identity == source_identity:
                raise ValueError(
                    "restore target database must be separate from every source database"
                )

        if self.runtime_component == "catalog":
            self._validate_catalog_configuration(managed=managed)
        if self.runtime_component in {"api", "worker", "catalog"}:
            return
        if self.runtime_component == "reconciler":
            if self.control_audit_signing_key is None:
                raise ValueError(
                    "semantic reconciler requires SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY"
                )
            audit_key = self.control_audit_signing_key.get_secret_value()
            _validate_strong_secret(
                audit_key,
                "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY",
            )
            if (self.control_operator_actor_id is None) != (not self.control_operator_roles):
                raise ValueError("control operator actor and roles must be configured together")
            if len(self.control_operator_roles) != len(set(self.control_operator_roles)):
                raise ValueError("control operator roles cannot contain duplicates")
            return
        if self.control_audit_signing_key is None:
            raise ValueError(
                "PostgreSQL control plane requires SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY"
            )
        if self.identity_migration_key is None:
            raise ValueError(
                "PostgreSQL control plane requires SCHEMABRIDGE_IDENTITY_MIGRATION_KEY"
            )
        audit_key = self.control_audit_signing_key.get_secret_value()
        identity_key = self.identity_migration_key.get_secret_value()
        _validate_strong_secret(audit_key, "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY")
        _validate_strong_secret(identity_key, "SCHEMABRIDGE_IDENTITY_MIGRATION_KEY")
        if audit_key == identity_key:
            raise ValueError("control-plane signing and identity-migration keys must be distinct")
        if (
            self.pseudonymization_key is not None
            and self.pseudonymization_key.get_secret_value() in {audit_key, identity_key}
        ):
            raise ValueError("control-plane keys must differ from the pseudonymization key")
        if (self.control_operator_actor_id is None) != (not self.control_operator_roles):
            raise ValueError("control operator actor and roles must be configured together")
        if len(self.control_operator_roles) != len(set(self.control_operator_roles)):
            raise ValueError("control operator roles cannot contain duplicates")

    def _reject_cross_component_credentials(self) -> None:
        if self.runtime_component == "web":
            return
        if self.runtime_component == "reconciler":
            forbidden = (
                self.control_database_url,
                self.control_migrator_database_url,
                self.control_restore_database_url,
                self.control_api_database_url,
                self.control_worker_database_url,
                self.control_catalog_database_url,
                self.identity_migration_key,
                self.openai_api_key,
                self.query_studio_signing_key,
                self.database_url,
                self.datahub_gms_token,
                self.oidc_issuer,
                self.oidc_audience,
                self.oidc_provider,
                self.oidc_allowed_groups or None,
                self.oidc_allowed_tenants or None,
                self.pseudonymization_key,
                self.api_local_bearer_token,
                self.api_oidc_jwks_url,
                self.inventory_cursor_signing_key,
                self.connector_secret_directory,
            )
            if any(value is not None for value in forbidden):
                raise ValueError(
                    "reconciler component received a forbidden cross-component credential"
                )
            return
        common = (
            self.control_database_url,
            self.control_reconciler_database_url,
            self.control_migrator_database_url,
            self.control_restore_database_url,
            self.control_operator_actor_id,
            self.control_operator_roles or None,
            self.openai_api_key,
            self.query_studio_signing_key,
            self.control_audit_signing_key,
            self.identity_migration_key,
        )
        component_specific: tuple[object | None, ...]
        if self.runtime_component == "api":
            component_specific = (
                self.database_url,
                self.connector_secret_directory,
                self.control_worker_database_url,
                self.control_catalog_database_url,
                self.datahub_gms_token,
            )
        elif self.runtime_component == "worker":
            component_specific = (
                self.database_url,
                self.control_api_database_url,
                self.control_catalog_database_url,
                self.datahub_gms_token,
                self.oidc_issuer,
                self.oidc_audience,
                self.oidc_provider,
                self.oidc_allowed_groups or None,
                self.oidc_allowed_tenants or None,
                self.pseudonymization_key,
                self.api_local_bearer_token,
                self.api_oidc_jwks_url,
            )
        else:
            component_specific = (
                self.database_url,
                self.control_api_database_url,
                self.control_worker_database_url,
                self.oidc_issuer,
                self.oidc_audience,
                self.oidc_provider,
                self.oidc_allowed_groups or None,
                self.oidc_allowed_tenants or None,
                self.pseudonymization_key,
                self.api_local_bearer_token,
                self.api_oidc_jwks_url,
            )
        if any(value is not None for value in (*common, *component_specific)):
            raise ValueError(
                f"{self.runtime_component} component received a forbidden cross-component credential"
            )

    def _validate_catalog_configuration(self, *, managed: bool) -> None:
        if self.retired_catalog_datahub_credential_binding_ref is not None:
            raise ValueError(
                "SCHEMABRIDGE_CATALOG_DATAHUB_CREDENTIAL_BINDING_REF is retired; "
                "catalog credentials require an approved connector route"
            )
        if managed and "datahub_gms_url" in self.model_fields_set:
            raise ValueError("managed catalog routing forbids a deployment-wide DATAHUB_GMS_URL")
        if managed and self.datahub_gms_token is not None:
            raise ValueError("managed catalog routing forbids a deployment-wide DATAHUB_GMS_TOKEN")
        if managed and self.catalog_synthetic_asset_counts:
            raise ValueError(
                "managed catalog routing forbids synthetic catalog source configuration"
            )
        store_operation_seconds = (
            self.control_pool_acquisition_timeout_seconds + self.statement_timeout_ms / 1_000
        )
        minimum_lease_seconds = max(
            self.catalog_source_timeout_seconds + store_operation_seconds + 5,
            store_operation_seconds * 2 + 5,
        )
        if self.catalog_lease_seconds < minimum_lease_seconds:
            raise ValueError(
                "catalog lease must cover source/store operations and one renewal margin"
            )
        if any(
            re.fullmatch(r"[a-z][a-z0-9_-]{2,199}", connection_id) is None
            or isinstance(asset_count, bool)
            or not 0 <= asset_count <= 100_000_000
            for connection_id, asset_count in self.catalog_synthetic_asset_counts.items()
        ):
            raise ValueError("catalog synthetic asset-count configuration is invalid")


def _looks_like_placeholder(value: str) -> bool:
    normalized = value.casefold()
    return any(
        marker in normalized
        for marker in ("replace-with", "change-me", "changeme", "placeholder", "example-secret")
    )


def _validate_strong_secret(value: str, label: str) -> None:
    encoded = value.encode()
    if len(encoded) < 32 or len(set(encoded)) < MINIMUM_PSEUDONYM_KEY_DISTINCT_BYTES:
        raise ValueError(f"{label} is not acceptable")
    if _looks_like_placeholder(value):
        raise ValueError(f"{label} is not acceptable")


def _postgres_database_identity(
    value: str,
    *,
    require_tls: bool,
    label: str,
) -> tuple[str, int, str]:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or parsed.hostname is None
        or parsed.username is None
        or not parsed.path
        or parsed.path == "/"
        or parsed.fragment
    ):
        raise ValueError(f"{label} URL is invalid")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if any(len(values) != 1 for values in query.values()):
        raise ValueError(f"{label} URL query is invalid")
    if require_tls and query.get("sslmode") not in (["verify-ca"], ["verify-full"]):
        raise ValueError(f"{label} URL must require verified TLS")
    host = parsed.hostname.casefold().rstrip(".")
    if host == "localhost":
        host = "loopback"
    else:
        try:
            if ipaddress.ip_address(host).is_loopback:
                host = "loopback"
        except ValueError:
            pass
    return host, parsed.port or 5432, parsed.path.removeprefix("/")


def get_settings() -> Settings:
    """Build settings at the composition boundary."""

    component = os.environ.get("SCHEMABRIDGE_COMPONENT", "web")
    if component in {"api", "worker", "catalog", "reconciler"}:
        return Settings(_env_file=None)
    return Settings()
