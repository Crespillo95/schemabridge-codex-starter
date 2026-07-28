"""Pure contracts for tenant-scoped dynamic catalog inventory."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Generic, TypeVar

from pydantic import ConfigDict, Field, RootModel, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.semantic_registry import PhysicalValueType

MAX_INVENTORY_PAGE_SIZE = 50
MAX_INVENTORY_CURSOR_BYTES = 1_024
MAX_TENANT_ASSET_LIMIT = 100_000_000
MAX_TENANT_FIELD_LIMIT = 1_000_000_000
MAX_SOURCE_ASSETS_PER_PAGE = 50
MAX_SOURCE_FIELDS_PER_ASSET = 1_000
MAX_SOURCE_FIELDS_PER_PAGE = 5_000
MAX_ASSET_SORT_KEY_BYTES = 512
MAX_FIELD_PATH_BYTES = 12_800
MAX_FIELD_SORT_KEY_BYTES = 400
MAX_CATALOG_METADATA_BYTES = 400
MAX_CATALOG_TERM_ARRAY_BYTES = 20_000

CATALOG_CONNECTION_ID_PATTERN = r"^[a-z][a-z0-9_-]{2,199}$"
CATALOG_ENVIRONMENT_PATTERN = r"^[A-Za-z][A-Za-z0-9_.-]{0,79}$"
CATALOG_INDEXER_ID_PATTERN = r"^[a-z][a-z0-9_.:-]{2,199}$"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,199}$")
_CATALOG_CONNECTION_ID = re.compile(CATALOG_CONNECTION_ID_PATTERN)
_CATALOG_ENVIRONMENT = re.compile(CATALOG_ENVIRONMENT_PATTERN)
_CATALOG_INDEXER_ID = re.compile(CATALOG_INDEXER_ID_PATTERN)
_NO_CONTROL = re.compile(r"^[^\x00-\x1f\x7f]+$")
_MAX_COUNTER = 9_223_372_036_854_775_807
_MAX_LEASE_DURATION = timedelta(minutes=5)


class CatalogConnectionId(RootModel[str]):
    """Opaque connection identity; it is never a DSN or credential reference."""

    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        return _catalog_connection_id(value, "catalog connection id")

    def __str__(self) -> str:
        return self.root


class CatalogAssetId(RootModel[str]):
    """Stable source-catalog asset identity, distinct from an executable SQL ref."""

    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        return _bounded_text(
            value,
            "catalog asset id",
            maximum=500,
            maximum_bytes=2_000,
        )

    def __str__(self) -> str:
        return self.root


class CatalogAssetLocator(FrozenDomainModel):
    """Collision-free tenant, connection, and source-asset identity."""

    workspace_id: str = Field(min_length=3, max_length=200)
    connection_id: CatalogConnectionId
    asset_id: CatalogAssetId

    @field_validator("workspace_id")
    @classmethod
    def workspace_must_be_inert(cls, value: str) -> str:
        return _inert_id(value, "catalog workspace id")


class CatalogFieldLocator(FrozenDomainModel):
    """A field path scoped by the complete inventory asset locator."""

    asset: CatalogAssetLocator
    field_path: tuple[str, ...] = Field(min_length=1, max_length=64)

    @field_validator("field_path")
    @classmethod
    def field_path_must_be_bounded(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _bounded_field_path(values, "catalog field path")


class CatalogRefreshId(RootModel[str]):
    """Opaque refresh identity generated outside the pure domain."""

    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        return _catalog_connection_id(value, "catalog refresh id")

    def __str__(self) -> str:
        return self.root


class CatalogConnectionKind(StrEnum):
    """Closed discovery transports implemented in M25."""

    DATAHUB_GRAPHQL = "datahub_graphql"
    SYNTHETIC = "synthetic"


class CatalogConnectionStatus(StrEnum):
    """Logical connection state; disabling never deletes inventory history."""

    ENABLED = "enabled"
    DISABLED = "disabled"


class CatalogConnectionConfirmation(StrEnum):
    """Exact human confirmations accepted by connection-management commands."""

    REGISTER = "register-catalog-connection"
    DISABLE = "disable-catalog-connection"


class CatalogRefreshMode(StrEnum):
    """Whether a source supplies a full reconciliation or a genuine change feed."""

    FULL = "full"
    DELTA = "delta"


class CatalogRefreshStatus(StrEnum):
    """Closed durable lifecycle for one catalog refresh generation."""

    REQUESTED = "requested"
    LEASED = "leased"
    STAGING = "staging"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in {CatalogRefreshStatus.COMPLETED, CatalogRefreshStatus.FAILED}


class CatalogRefreshConfirmation(StrEnum):
    """Exact operator confirmation for an asynchronous metadata refresh."""

    REQUEST = "request-catalog-refresh"


class CatalogRefreshFailureCode(StrEnum):
    """Sanitized failures that may be persisted or exposed without vendor payloads."""

    SOURCE_UNAVAILABLE = "source_unavailable"
    SOURCE_PERMISSION_DENIED = "source_permission_denied"
    SOURCE_INVALID_RESPONSE = "source_invalid_response"
    SOURCE_CURSOR_STALLED = "source_cursor_stalled"
    SOURCE_PAGE_REPEATED = "source_page_repeated"
    SOURCE_RESPONSE_TOO_LARGE = "source_response_too_large"
    SOURCE_ASSET_MALFORMED = "source_asset_malformed"
    DELTA_UNSUPPORTED = "delta_unsupported"
    CAPACITY_EXCEEDED = "catalog_capacity_exceeded"
    CONNECTION_DISABLED = "connection_disabled"
    BASE_GENERATION_CHANGED = "base_generation_changed"
    LEASE_LOST = "refresh_lease_lost"
    FINGERPRINT_MISMATCH = "catalog_fingerprint_mismatch"
    STORE_UNAVAILABLE = "catalog_store_unavailable"
    UNEXPECTED_INDEXER_FAILURE = "unexpected_indexer_failure"


class CatalogRefreshTransitionErrorCode(StrEnum):
    """Stable pure-domain refresh transition failures."""

    INVALID_STATE = "catalog_refresh_invalid_state"
    TERMINAL_IMMUTABLE = "catalog_refresh_terminal_immutable"
    LEASE_ACTIVE = "catalog_refresh_lease_active"
    TEMPORAL_CONFLICT = "catalog_refresh_temporal_conflict"


class CatalogRefreshTransitionError(ValueError):
    """A refresh transition violates the closed lifecycle."""

    def __init__(self, code: CatalogRefreshTransitionErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class CatalogConnectionRegistration(FrozenDomainModel):
    """One exact, idempotent registration command with no credential material."""

    workspace_id: str = Field(min_length=3, max_length=200)
    connection_id: CatalogConnectionId
    display_name: str = Field(min_length=1, max_length=200)
    kind: CatalogConnectionKind
    environment: str = Field(min_length=1, max_length=100)
    catalog_scope: str = Field(min_length=1, max_length=200)
    platform_instance: str | None = Field(default=None, max_length=200)
    requested_by: str = Field(min_length=3, max_length=200)
    requested_at: datetime
    idempotency_digest: str
    confirmation: CatalogConnectionConfirmation = CatalogConnectionConfirmation.REGISTER

    @field_validator("workspace_id", "requested_by")
    @classmethod
    def identifiers_must_be_inert(cls, value: str) -> str:
        return _inert_id(value, "catalog registration identifier")

    @field_validator("display_name", "catalog_scope")
    @classmethod
    def public_metadata_must_be_bounded(cls, value: str) -> str:
        return _bounded_storage_text(value, "catalog connection metadata")

    @field_validator("platform_instance")
    @classmethod
    def platform_instance_must_be_explicit_and_bounded(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None
        return _bounded_storage_text(value, "catalog platform instance")

    @field_validator("environment")
    @classmethod
    def environment_must_match_storage(cls, value: str) -> str:
        return _catalog_environment(value)

    @field_validator("requested_at")
    @classmethod
    def request_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "catalog registration time")

    @field_validator("idempotency_digest")
    @classmethod
    def idempotency_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "catalog registration idempotency digest")

    @model_validator(mode="after")
    def confirmation_must_match_operation(self) -> CatalogConnectionRegistration:
        if self.confirmation is not CatalogConnectionConfirmation.REGISTER:
            raise ValueError("catalog registration confirmation does not match operation")
        return self


class CatalogConnectionDisable(FrozenDomainModel):
    """A logical-disable command bound to one tenant connection."""

    workspace_id: str = Field(min_length=3, max_length=200)
    connection_id: CatalogConnectionId
    requested_by: str = Field(min_length=3, max_length=200)
    requested_at: datetime
    idempotency_digest: str
    confirmation: CatalogConnectionConfirmation = CatalogConnectionConfirmation.DISABLE

    @field_validator("workspace_id", "requested_by")
    @classmethod
    def identifiers_must_be_inert(cls, value: str) -> str:
        return _inert_id(value, "catalog disable identifier")

    @field_validator("requested_at")
    @classmethod
    def request_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "catalog disable time")

    @field_validator("idempotency_digest")
    @classmethod
    def idempotency_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "catalog disable idempotency digest")

    @model_validator(mode="after")
    def confirmation_must_match_operation(self) -> CatalogConnectionDisable:
        if self.confirmation is not CatalogConnectionConfirmation.DISABLE:
            raise ValueError("catalog disable confirmation does not match operation")
        return self


class CatalogConnectionRoute(FrozenDomainModel):
    """Public indexer routing metadata with no credential or secret reference."""

    workspace_id: str = Field(min_length=3, max_length=200)
    connection_id: CatalogConnectionId
    kind: CatalogConnectionKind
    environment: str = Field(min_length=1, max_length=100)
    catalog_scope: str = Field(min_length=1, max_length=200)
    platform_instance: str | None = Field(default=None, max_length=200)
    catalog_identity_fingerprint: str | None = None
    status: CatalogConnectionStatus
    contract_version: int | None = Field(default=None, ge=1)
    route_revision: int | None = Field(default=None, ge=1)
    target_fingerprint: str | None = None

    @field_validator("workspace_id")
    @classmethod
    def identifiers_must_be_inert(cls, value: str) -> str:
        return _inert_id(value, "catalog route identifier")

    @field_validator("catalog_scope")
    @classmethod
    def metadata_must_be_bounded(cls, value: str) -> str:
        return _bounded_storage_text(value, "catalog route metadata")

    @field_validator("platform_instance")
    @classmethod
    def platform_instance_must_be_explicit_and_bounded(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None
        return _bounded_storage_text(value, "catalog platform instance")

    @field_validator("environment")
    @classmethod
    def environment_must_match_storage(cls, value: str) -> str:
        return _catalog_environment(value)

    @field_validator("catalog_identity_fingerprint", "target_fingerprint")
    @classmethod
    def governed_fingerprint_must_be_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _sha256(value, "catalog connector governed fingerprint")

    @model_validator(mode="after")
    def governed_target_identity_must_be_complete(self) -> CatalogConnectionRoute:
        governed_values = (
            self.contract_version,
            self.route_revision,
            self.target_fingerprint,
            self.catalog_identity_fingerprint,
        )
        if any(value is not None for value in governed_values) and not all(
            value is not None for value in governed_values
        ):
            raise ValueError("catalog connector target identity must be complete")
        return self


class CatalogConnectionSummary(FrozenDomainModel):
    """Public connection state; the credential-binding reference is intentionally absent."""

    workspace_id: str = Field(min_length=3, max_length=200)
    connection_id: CatalogConnectionId
    display_name: str = Field(min_length=1, max_length=200)
    kind: CatalogConnectionKind
    environment: str = Field(min_length=1, max_length=100)
    catalog_scope: str = Field(min_length=1, max_length=200)
    platform_instance: str | None = Field(default=None, max_length=200)
    status: CatalogConnectionStatus
    active_generation: int | None = Field(default=None, ge=1)
    asset_count: int = Field(default=0, ge=0, le=_MAX_COUNTER)
    field_count: int = Field(default=0, ge=0, le=_MAX_COUNTER)
    last_completed_at: datetime | None = None
    stale: bool = False

    @field_validator("workspace_id")
    @classmethod
    def workspace_must_be_inert(cls, value: str) -> str:
        return _inert_id(value, "catalog connection workspace")

    @field_validator("display_name", "catalog_scope")
    @classmethod
    def public_metadata_must_be_bounded(cls, value: str) -> str:
        return _bounded_storage_text(value, "catalog connection metadata")

    @field_validator("platform_instance")
    @classmethod
    def platform_instance_must_be_explicit_and_bounded(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None
        return _bounded_storage_text(value, "catalog platform instance")

    @field_validator("environment")
    @classmethod
    def environment_must_match_storage(cls, value: str) -> str:
        return _catalog_environment(value)

    @field_validator("last_completed_at")
    @classmethod
    def completion_time_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            return _aware(value, "catalog connection completion time")
        return value

    @model_validator(mode="after")
    def generation_and_counts_must_be_consistent(self) -> CatalogConnectionSummary:
        if self.active_generation is None and (
            self.asset_count != 0 or self.field_count != 0 or self.last_completed_at is not None
        ):
            raise ValueError("connection without an active generation cannot expose inventory")
        if self.active_generation is not None and self.last_completed_at is None:
            raise ValueError("active catalog generation requires a completion time")
        return self


class CatalogConnectionRegistrationResult(FrozenDomainModel):
    """A new registration or an exact idempotent replay."""

    connection: CatalogConnectionSummary
    replayed: bool = False


class CatalogConnectionFilter(FrozenDomainModel):
    """Normalized public filters for connection listing."""

    query: str | None = Field(default=None, max_length=200)
    status: CatalogConnectionStatus | None = None

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str | None) -> str | None:
        return _normalize_filter_text(value, "connection query")

    @property
    def fingerprint(self) -> str:
        return inventory_filter_fingerprint(self.model_dump(mode="json", exclude_none=True))


class CatalogAssetFilter(FrozenDomainModel):
    """Normalized indexed filters for one connection generation."""

    query: str | None = Field(default=None, max_length=200)
    platform: str | None = Field(default=None, max_length=100)
    schema_name: str | None = Field(default=None, max_length=200)

    @field_validator("query", "platform", "schema_name")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        return _normalize_filter_text(value, "asset filter")

    @property
    def fingerprint(self) -> str:
        return inventory_filter_fingerprint(self.model_dump(mode="json", exclude_none=True))


class CatalogFieldFilter(FrozenDomainModel):
    """Normalized indexed filters for one asset's field paths."""

    query: str | None = Field(default=None, max_length=200)
    native_type: str | None = Field(default=None, max_length=200)

    @field_validator("query", "native_type")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        return _normalize_filter_text(value, "field filter")

    @property
    def fingerprint(self) -> str:
        return inventory_filter_fingerprint(self.model_dump(mode="json", exclude_none=True))


class CatalogAssetSummary(FrozenDomainModel):
    """Sanitized versioned asset metadata served from the active PostgreSQL index."""

    locator: CatalogAssetLocator
    generation: int = Field(ge=1)
    qualified_name: str = Field(min_length=1, max_length=500)
    display_name: str = Field(min_length=1, max_length=200)
    platform: str = Field(min_length=1, max_length=100)
    environment: str = Field(min_length=1, max_length=100)
    database_name: str | None = Field(default=None, max_length=200)
    schema_name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4_000)
    field_count: int = Field(ge=0, le=_MAX_COUNTER)
    metadata_fingerprint: str
    observed_at: datetime

    @field_validator("qualified_name")
    @classmethod
    def qualified_name_must_fit_storage_sort_key(cls, value: str) -> str:
        return _bounded_qualified_name(value, "catalog asset qualified name")

    @field_validator("display_name", "database_name", "schema_name")
    @classmethod
    def names_must_fit_storage(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_storage_text(value, "catalog asset name")

    @field_validator("platform", "environment")
    @classmethod
    def source_dimensions_must_fit_storage(cls, value: str) -> str:
        return _bounded_text(
            value,
            "catalog asset source dimension",
            maximum=100,
            maximum_bytes=MAX_CATALOG_METADATA_BYTES,
        )

    @field_validator("description")
    @classmethod
    def description_must_fit_storage(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_text(
            value,
            "catalog asset description",
            maximum=4_000,
            maximum_bytes=16_000,
        )

    @field_validator("metadata_fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "catalog asset metadata fingerprint")

    @field_validator("observed_at")
    @classmethod
    def observation_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "catalog asset observation time")


class CatalogFieldSummary(FrozenDomainModel):
    """Sanitized versioned field metadata; source samples have no representation."""

    locator: CatalogFieldLocator
    generation: int = Field(ge=1)
    native_type: str | None = Field(default=None, max_length=200)
    normalized_type: PhysicalValueType | None = None
    description: str | None = Field(default=None, max_length=4_000)
    nullable: bool | None = None
    is_part_of_key: bool | None = None
    tags: tuple[str, ...] = Field(default=(), max_length=100)
    glossary_terms: tuple[str, ...] = Field(default=(), max_length=100)
    metadata_fingerprint: str
    observed_at: datetime

    @field_validator("native_type")
    @classmethod
    def native_type_must_fit_storage(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_storage_text(value, "catalog field native type")

    @field_validator("description")
    @classmethod
    def description_must_fit_storage(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_text(
            value,
            "catalog field description",
            maximum=4_000,
            maximum_bytes=16_000,
        )

    @field_validator("tags", "glossary_terms")
    @classmethod
    def terms_must_be_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _bounded_catalog_terms(values)

    @field_validator("metadata_fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "catalog field metadata fingerprint")

    @field_validator("observed_at")
    @classmethod
    def observation_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "catalog field observation time")


class TenantCapacityPolicy(FrozenDomainModel):
    """Durable tenant-specific capacity; none of these values is code capacity."""

    workspace_id: str = Field(min_length=3, max_length=200)
    version: int = Field(ge=1)
    connection_limit: int = Field(ge=1, le=100_000)
    asset_limit: int = Field(ge=1, le=MAX_TENANT_ASSET_LIMIT)
    field_limit: int = Field(ge=1, le=MAX_TENANT_FIELD_LIMIT)
    api_requests_per_minute: int = Field(ge=1, le=1_000_000)
    nonterminal_job_limit: int = Field(ge=1, le=1_000_000)
    catalog_cursor_ttl_seconds: int = Field(default=900, ge=900, le=900)
    generation_retention_seconds: int = Field(default=1_800, ge=900, le=2_592_000)
    updated_by: str = Field(min_length=3, max_length=200)
    updated_at: datetime

    @field_validator("workspace_id", "updated_by")
    @classmethod
    def identifiers_must_be_inert(cls, value: str) -> str:
        return _inert_id(value, "tenant capacity identifier")

    @field_validator("updated_at")
    @classmethod
    def update_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "tenant capacity update time")

    @model_validator(mode="after")
    def retention_must_cover_cursor_lifetime(self) -> TenantCapacityPolicy:
        if self.generation_retention_seconds < self.catalog_cursor_ttl_seconds:
            raise ValueError("tenant generation retention cannot expire a live cursor")
        return self


class TenantCapacityPolicyConfirmation(StrEnum):
    """Exact operator phrase required before a durable policy revision."""

    APPLY = "APPLY TENANT CAPACITY POLICY"


class TenantCapacityPolicyChange(FrozenDomainModel):
    """Optimistic, explicitly confirmed capacity-policy creation or revision."""

    workspace_id: str = Field(min_length=3, max_length=200)
    expected_version: int = Field(ge=0, le=_MAX_COUNTER - 1)
    connection_limit: int = Field(ge=1, le=100_000)
    asset_limit: int = Field(ge=1, le=MAX_TENANT_ASSET_LIMIT)
    field_limit: int = Field(ge=1, le=MAX_TENANT_FIELD_LIMIT)
    api_requests_per_minute: int = Field(ge=1, le=1_000_000)
    nonterminal_job_limit: int = Field(ge=1, le=1_000_000)
    generation_retention_seconds: int = Field(default=1_800, ge=900, le=2_592_000)
    updated_by: str = Field(min_length=3, max_length=200)
    confirmation: TenantCapacityPolicyConfirmation

    @field_validator("workspace_id", "updated_by")
    @classmethod
    def identifiers_must_be_inert(cls, value: str) -> str:
        return _inert_id(value, "tenant capacity policy change identifier")


class TenantCapacityUsage(FrozenDomainModel):
    """Observed counts may exceed a newly lowered policy and remain representable."""

    workspace_id: str = Field(min_length=3, max_length=200)
    connection_count: int = Field(ge=0, le=_MAX_COUNTER)
    asset_count: int = Field(ge=0, le=_MAX_COUNTER)
    field_count: int = Field(ge=0, le=_MAX_COUNTER)
    nonterminal_job_count: int = Field(ge=0, le=_MAX_COUNTER)
    observed_at: datetime

    @field_validator("workspace_id")
    @classmethod
    def workspace_must_be_inert(cls, value: str) -> str:
        return _inert_id(value, "tenant capacity workspace")

    @field_validator("observed_at")
    @classmethod
    def observation_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "tenant capacity observation time")


class TenantCapacitySnapshot(FrozenDomainModel):
    """Policy and current use kept together for an exact tenant."""

    policy: TenantCapacityPolicy
    usage: TenantCapacityUsage

    @model_validator(mode="after")
    def tenant_must_match(self) -> TenantCapacitySnapshot:
        if self.policy.workspace_id != self.usage.workspace_id:
            raise ValueError("capacity policy and usage workspace do not match")
        return self

    @property
    def over_capacity(self) -> bool:
        return (
            self.usage.connection_count > self.policy.connection_limit
            or self.usage.asset_count > self.policy.asset_limit
            or self.usage.field_count > self.policy.field_limit
            or self.usage.nonterminal_job_count > self.policy.nonterminal_job_limit
        )


class CapacityResource(StrEnum):
    """Closed durable admission dimensions."""

    API_REQUEST = "api_request"
    EXECUTION_JOB = "execution_job"
    CONNECTION = "connection"
    ASSET = "asset"
    FIELD = "field"


class CapacityAdmission(FrozenDomainModel):
    """Sanitized result of one atomic durable capacity decision."""

    workspace_id: str = Field(min_length=3, max_length=200)
    resource: CapacityResource
    allowed: bool
    used: int = Field(ge=0, le=_MAX_COUNTER)
    limit: int = Field(ge=1, le=_MAX_COUNTER)
    retry_after_seconds: int | None = Field(default=None, ge=1, le=60)

    @field_validator("workspace_id")
    @classmethod
    def workspace_must_be_inert(cls, value: str) -> str:
        return _inert_id(value, "capacity admission workspace")

    @model_validator(mode="after")
    def decision_must_match_counts(self) -> CapacityAdmission:
        if self.allowed:
            if self.used > self.limit:
                raise ValueError("allowed capacity cannot exceed its limit")
            if self.retry_after_seconds is not None:
                raise ValueError("allowed capacity cannot request a retry")
        else:
            if self.used < self.limit:
                raise ValueError("denied capacity must have reached its limit")
            if self.resource is CapacityResource.API_REQUEST and self.retry_after_seconds is None:
                raise ValueError("denied API request requires bounded retry metadata")
        return self


class CatalogSourceField(FrozenDomainModel):
    """Bounded source field metadata without samples or values."""

    field_path: tuple[str, ...] = Field(min_length=1, max_length=64)
    native_type: str | None = Field(default=None, max_length=200)
    normalized_type: PhysicalValueType | None = None
    description: str | None = Field(default=None, max_length=4_000)
    nullable: bool | None = None
    is_part_of_key: bool | None = None
    tags: tuple[str, ...] = Field(default=(), max_length=100)
    glossary_terms: tuple[str, ...] = Field(default=(), max_length=100)
    metadata_fingerprint: str

    @field_validator("field_path")
    @classmethod
    def field_path_must_be_bounded(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _bounded_field_path(values, "source field path")

    @field_validator("native_type")
    @classmethod
    def native_type_must_fit_storage(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_storage_text(value, "source field native type")

    @field_validator("description")
    @classmethod
    def description_must_fit_storage(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_text(
            value,
            "source field description",
            maximum=4_000,
            maximum_bytes=16_000,
        )

    @field_validator("tags", "glossary_terms")
    @classmethod
    def terms_must_be_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _bounded_catalog_terms(values)

    @field_validator("metadata_fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "source field fingerprint")


class CatalogSourceAsset(FrozenDomainModel):
    """One stably ordered source asset carrying its bounded schema metadata."""

    asset_id: CatalogAssetId
    qualified_name: str = Field(min_length=1, max_length=500)
    display_name: str = Field(min_length=1, max_length=200)
    platform: str = Field(min_length=1, max_length=100)
    environment: str = Field(min_length=1, max_length=100)
    database_name: str | None = Field(default=None, max_length=200)
    schema_name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4_000)
    fields: tuple[CatalogSourceField, ...] = Field(
        default=(),
        max_length=MAX_SOURCE_FIELDS_PER_ASSET,
    )
    metadata_fingerprint: str

    @field_validator("qualified_name")
    @classmethod
    def qualified_name_must_fit_storage_sort_key(cls, value: str) -> str:
        return _bounded_qualified_name(value, "source asset qualified name")

    @field_validator("display_name", "database_name", "schema_name")
    @classmethod
    def names_must_fit_storage(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_storage_text(value, "source asset name")

    @field_validator("platform", "environment")
    @classmethod
    def source_dimensions_must_fit_storage(cls, value: str) -> str:
        return _bounded_text(
            value,
            "source asset dimension",
            maximum=100,
            maximum_bytes=MAX_CATALOG_METADATA_BYTES,
        )

    @field_validator("description")
    @classmethod
    def description_must_fit_storage(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_text(
            value,
            "source asset description",
            maximum=4_000,
            maximum_bytes=16_000,
        )

    @field_validator("fields")
    @classmethod
    def fields_must_be_unique_and_sorted(
        cls,
        values: tuple[CatalogSourceField, ...],
    ) -> tuple[CatalogSourceField, ...]:
        paths = tuple(item.field_path for item in values)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("source asset fields must have unique sorted paths")
        return values

    @field_validator("metadata_fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "source asset fingerprint")


class CatalogSourceChangeKind(StrEnum):
    """Typed metadata changes; no arbitrary source operation crosses this boundary."""

    UPSERT_ASSET = "upsert_asset"
    DELETE_ASSET = "delete_asset"
    UPSERT_FIELD = "upsert_field"
    DELETE_FIELD = "delete_field"


class CatalogSourceChange(FrozenDomainModel):
    """One full-scan upsert or genuine delta event."""

    kind: CatalogSourceChangeKind
    asset_id: CatalogAssetId | None = None
    asset: CatalogSourceAsset | None = None
    field: CatalogSourceField | None = None
    field_path: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("field_path")
    @classmethod
    def optional_field_path_must_be_bounded(
        cls,
        values: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        if values is None:
            return None
        return _bounded_field_path(values, "source delete field path")

    @model_validator(mode="after")
    def payload_must_match_change_kind(self) -> CatalogSourceChange:
        if self.kind is CatalogSourceChangeKind.UPSERT_ASSET:
            if (
                self.asset is None
                or self.asset_id is not None
                or self.field is not None
                or self.field_path is not None
            ):
                raise ValueError("asset upsert requires exactly one asset payload")
        elif self.kind is CatalogSourceChangeKind.DELETE_ASSET:
            if (
                self.asset_id is None
                or self.asset is not None
                or self.field is not None
                or self.field_path is not None
            ):
                raise ValueError("asset delete requires exactly one asset id")
        elif self.kind is CatalogSourceChangeKind.UPSERT_FIELD:
            if (
                self.asset_id is None
                or self.field is None
                or self.asset is not None
                or self.field_path is not None
            ):
                raise ValueError("field upsert requires one asset id and field payload")
        elif (
            self.asset_id is None
            or self.field_path is None
            or self.asset is not None
            or self.field is not None
        ):
            raise ValueError("field delete requires one asset id and field path")
        return self

    @property
    def identity(self) -> tuple[str, str, tuple[str, ...]]:
        if self.asset is not None:
            return (self.kind.value, self.asset.asset_id.root, ())
        if self.field is not None:
            return (self.kind.value, self.asset_id.root, self.field.field_path)  # type: ignore[union-attr]
        return (
            self.kind.value,
            self.asset_id.root,  # type: ignore[union-attr]
            self.field_path or (),
        )


class CatalogSourcePage(FrozenDomainModel):
    """A bounded page persisted atomically before the next source request."""

    mode: CatalogRefreshMode
    sequence: int = Field(ge=1)
    changes: tuple[CatalogSourceChange, ...] = Field(
        max_length=MAX_SOURCE_ASSETS_PER_PAGE,
    )
    next_checkpoint: str | None = Field(default=None, max_length=MAX_INVENTORY_CURSOR_BYTES)
    source_complete: bool
    page_fingerprint: str

    @field_validator("next_checkpoint")
    @classmethod
    def checkpoint_must_be_bounded(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value or value.strip() != value or _NO_CONTROL.fullmatch(value) is None:
            raise ValueError("source checkpoint is invalid")
        if len(value.encode("utf-8")) > MAX_INVENTORY_CURSOR_BYTES:
            raise ValueError("source checkpoint is invalid")
        return value

    @field_validator("page_fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "catalog source page fingerprint")

    @model_validator(mode="after")
    def page_shape_and_fingerprint_must_be_exact(self) -> CatalogSourcePage:
        if not self.source_complete and self.next_checkpoint is None:
            raise ValueError("nonterminal source page requires a next checkpoint")
        if not self.changes and not self.source_complete:
            raise ValueError("empty source page cannot claim more data")
        if self.mode is CatalogRefreshMode.FULL and any(
            item.kind is not CatalogSourceChangeKind.UPSERT_ASSET for item in self.changes
        ):
            raise ValueError("full discovery pages contain only complete asset upserts")
        identities = tuple(item.identity for item in self.changes)
        if len(identities) != len(set(identities)):
            raise ValueError("source page changes must have unique identities")
        field_count = sum(
            len(item.asset.fields) for item in self.changes if item.asset is not None
        ) + sum(1 for item in self.changes if item.field is not None)
        if field_count > MAX_SOURCE_FIELDS_PER_PAGE:
            raise ValueError("source page exceeds the bounded field count")
        expected = catalog_source_page_fingerprint(
            mode=self.mode,
            sequence=self.sequence,
            changes=self.changes,
            next_checkpoint=self.next_checkpoint,
            source_complete=self.source_complete,
        )
        if self.page_fingerprint != expected:
            raise ValueError("catalog source page fingerprint does not match")
        return self

    @classmethod
    def create(
        cls,
        *,
        mode: CatalogRefreshMode,
        sequence: int,
        changes: tuple[CatalogSourceChange, ...],
        next_checkpoint: str | None,
        source_complete: bool,
    ) -> CatalogSourcePage:
        return cls(
            mode=mode,
            sequence=sequence,
            changes=changes,
            next_checkpoint=next_checkpoint,
            source_complete=source_complete,
            page_fingerprint=catalog_source_page_fingerprint(
                mode=mode,
                sequence=sequence,
                changes=changes,
                next_checkpoint=next_checkpoint,
                source_complete=source_complete,
            ),
        )


class CatalogRefreshCommand(FrozenDomainModel):
    """One exact API request; discovery remains outside the API process."""

    workspace_id: str = Field(min_length=3, max_length=200)
    connection_id: CatalogConnectionId
    mode: CatalogRefreshMode
    requested_by: str = Field(min_length=3, max_length=200)
    requested_at: datetime
    idempotency_digest: str
    confirmation: CatalogRefreshConfirmation = CatalogRefreshConfirmation.REQUEST

    @field_validator("workspace_id", "requested_by")
    @classmethod
    def identifiers_must_be_inert(cls, value: str) -> str:
        return _inert_id(value, "catalog refresh request identifier")

    @field_validator("requested_at")
    @classmethod
    def request_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "catalog refresh request time")

    @field_validator("idempotency_digest")
    @classmethod
    def idempotency_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "catalog refresh idempotency digest")

    @model_validator(mode="after")
    def confirmation_must_match_operation(self) -> CatalogRefreshCommand:
        if self.confirmation is not CatalogRefreshConfirmation.REQUEST:
            raise ValueError("catalog refresh confirmation does not match operation")
        return self


class CatalogRefreshLease(FrozenDomainModel):
    """Transient indexer ownership represented only by a capability digest."""

    indexer_id: str = Field(min_length=3, max_length=200)
    capability_digest: str
    fencing_token: int = Field(ge=1)
    leased_at: datetime
    expires_at: datetime

    @field_validator("indexer_id")
    @classmethod
    def indexer_must_be_inert(cls, value: str) -> str:
        if _CATALOG_INDEXER_ID.fullmatch(value) is None:
            raise ValueError("catalog indexer id must match durable storage")
        return value

    @field_validator("capability_digest")
    @classmethod
    def capability_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "catalog refresh capability digest")

    @field_validator("leased_at", "expires_at")
    @classmethod
    def lease_times_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "catalog refresh lease time")

    @model_validator(mode="after")
    def lease_window_must_be_bounded(self) -> CatalogRefreshLease:
        if not self.leased_at < self.expires_at:
            raise ValueError("catalog refresh lease must expire after acquisition")
        if self.expires_at - self.leased_at > _MAX_LEASE_DURATION:
            raise ValueError("catalog refresh lease exceeds five minutes")
        return self

    def is_current(self, at: datetime) -> bool:
        instant = _aware(at, "catalog refresh lease check")
        return self.leased_at <= instant < self.expires_at


class CatalogRefreshState(FrozenDomainModel):
    """Internal durable refresh state; public summaries omit leases and checkpoints."""

    refresh_id: CatalogRefreshId
    workspace_id: str = Field(min_length=3, max_length=200)
    connection_id: CatalogConnectionId
    mode: CatalogRefreshMode
    status: CatalogRefreshStatus
    base_generation: int = Field(ge=0)
    target_generation: int = Field(ge=1)
    idempotency_digest: str
    requested_by: str = Field(min_length=3, max_length=200)
    requested_at: datetime
    updated_at: datetime
    lease: CatalogRefreshLease | None = None
    source_page_count: int = Field(default=0, ge=0, le=_MAX_COUNTER)
    source_page_fingerprint: str | None = None
    staged_asset_count: int = Field(default=0, ge=0, le=_MAX_COUNTER)
    staged_field_count: int = Field(default=0, ge=0, le=_MAX_COUNTER)
    source_complete: bool = False
    source_checkpoint: str | None = Field(
        default=None,
        max_length=MAX_INVENTORY_CURSOR_BYTES,
    )
    catalog_fingerprint: str | None = None
    failure_code: CatalogRefreshFailureCode | None = None
    completed_at: datetime | None = None

    @field_validator("workspace_id", "requested_by")
    @classmethod
    def identifiers_must_be_inert(cls, value: str) -> str:
        return _inert_id(value, "catalog refresh state identifier")

    @field_validator("idempotency_digest")
    @classmethod
    def idempotency_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "catalog refresh state idempotency digest")

    @field_validator("source_page_fingerprint", "catalog_fingerprint")
    @classmethod
    def optional_fingerprint_must_be_sha256(cls, value: str | None) -> str | None:
        if value is not None:
            return _sha256(value, "catalog refresh fingerprint")
        return value

    @field_validator("source_checkpoint")
    @classmethod
    def checkpoint_must_be_bounded(cls, value: str | None) -> str | None:
        if value is not None:
            return _bounded_text(
                value,
                "catalog refresh checkpoint",
                maximum=MAX_INVENTORY_CURSOR_BYTES,
            )
        return value

    @field_validator("requested_at", "updated_at", "completed_at")
    @classmethod
    def times_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            return _aware(value, "catalog refresh state time")
        return value

    @model_validator(mode="after")
    def lifecycle_shape_must_be_exact(self) -> CatalogRefreshState:
        if self.target_generation <= self.base_generation:
            raise ValueError("refresh target generation must advance the base generation")
        if self.updated_at < self.requested_at:
            raise ValueError("refresh update cannot precede its request")
        if self.source_page_count == 0 and (
            self.source_page_fingerprint is not None
            or self.staged_asset_count != 0
            or self.staged_field_count != 0
            or self.source_complete
            or self.source_checkpoint is not None
        ):
            raise ValueError("refresh counts and checkpoint require a persisted source page")
        if self.source_page_count > 0 and self.source_page_fingerprint is None:
            raise ValueError("persisted source page requires its exact fingerprint")
        if self.status in {CatalogRefreshStatus.LEASED, CatalogRefreshStatus.STAGING}:
            if self.lease is None:
                raise ValueError("leased and staging refreshes require exact lease ownership")
        elif self.lease is not None:
            raise ValueError("requested and terminal refreshes cannot retain a lease")
        if self.status is CatalogRefreshStatus.COMPLETED:
            if (
                not self.source_complete
                or self.catalog_fingerprint is None
                or self.failure_code is not None
                or self.completed_at is None
            ):
                raise ValueError("completed refresh requires fingerprint and completion time")
        elif self.status is CatalogRefreshStatus.FAILED:
            if (
                self.failure_code is None
                or self.catalog_fingerprint is not None
                or self.completed_at is None
            ):
                raise ValueError(
                    "failed refresh requires one sanitized failure and completion time"
                )
        elif (
            self.catalog_fingerprint is not None
            or self.failure_code is not None
            or self.completed_at is not None
        ):
            raise ValueError("nonterminal refresh cannot expose terminal metadata")
        if self.completed_at is not None and self.completed_at < self.updated_at:
            raise ValueError("refresh completion cannot precede its latest update")
        return self


class CatalogRefreshSummary(FrozenDomainModel):
    """Safe API representation without capability or source-checkpoint material."""

    refresh_id: CatalogRefreshId
    workspace_id: str = Field(min_length=3, max_length=200)
    connection_id: CatalogConnectionId
    mode: CatalogRefreshMode
    status: CatalogRefreshStatus
    base_generation: int = Field(ge=0)
    target_generation: int = Field(ge=1)
    source_page_count: int = Field(ge=0, le=_MAX_COUNTER)
    asset_count: int = Field(ge=0, le=_MAX_COUNTER)
    field_count: int = Field(ge=0, le=_MAX_COUNTER)
    source_complete: bool
    catalog_fingerprint: str | None = None
    failure_code: CatalogRefreshFailureCode | None = None
    requested_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    @field_validator("workspace_id")
    @classmethod
    def workspace_must_be_inert(cls, value: str) -> str:
        return _inert_id(value, "catalog refresh summary workspace")

    @field_validator("catalog_fingerprint")
    @classmethod
    def optional_fingerprint_must_be_sha256(cls, value: str | None) -> str | None:
        if value is not None:
            return _sha256(value, "catalog refresh summary fingerprint")
        return value

    @field_validator("requested_at", "updated_at", "completed_at")
    @classmethod
    def times_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            return _aware(value, "catalog refresh summary time")
        return value

    @model_validator(mode="after")
    def terminal_shape_must_be_exact(self) -> CatalogRefreshSummary:
        if not self.requested_at <= self.updated_at:
            raise ValueError("refresh summary update cannot precede request")
        if self.status is CatalogRefreshStatus.COMPLETED:
            if (
                not self.source_complete
                or self.catalog_fingerprint is None
                or self.failure_code is not None
                or self.completed_at is None
            ):
                raise ValueError("completed refresh summary is incomplete")
        elif self.status is CatalogRefreshStatus.FAILED:
            if (
                self.failure_code is None
                or self.catalog_fingerprint is not None
                or self.completed_at is None
            ):
                raise ValueError("failed refresh summary is incomplete")
        elif (
            self.catalog_fingerprint is not None
            or self.failure_code is not None
            or self.completed_at is not None
        ):
            raise ValueError("nonterminal refresh summary contains terminal metadata")
        if self.completed_at is not None and self.completed_at < self.updated_at:
            raise ValueError("refresh summary completion precedes update")
        return self

    @classmethod
    def from_state(cls, state: CatalogRefreshState) -> CatalogRefreshSummary:
        return cls(
            refresh_id=state.refresh_id,
            workspace_id=state.workspace_id,
            connection_id=state.connection_id,
            mode=state.mode,
            status=state.status,
            base_generation=state.base_generation,
            target_generation=state.target_generation,
            source_page_count=state.source_page_count,
            asset_count=state.staged_asset_count,
            field_count=state.staged_field_count,
            source_complete=state.source_complete,
            catalog_fingerprint=state.catalog_fingerprint,
            failure_code=state.failure_code,
            requested_at=state.requested_at,
            updated_at=state.updated_at,
            completed_at=state.completed_at,
        )


class CatalogRefreshRequestResult(FrozenDomainModel):
    """A newly requested refresh or an exact replay."""

    refresh: CatalogRefreshSummary
    replayed: bool = False


class CatalogScaleReport(FrozenDomainModel):
    """Bounded local regression evidence, explicitly not a production SLO."""

    workspace_id: str = Field(min_length=3, max_length=200)
    connection_id: CatalogConnectionId
    platform: str = Field(min_length=1, max_length=200)
    asset_count: int = Field(ge=0, le=_MAX_COUNTER)
    field_count: int = Field(ge=0, le=_MAX_COUNTER)
    page_size: int = Field(ge=1, le=MAX_INVENTORY_PAGE_SIZE)
    page_count: int = Field(ge=0, le=_MAX_COUNTER)
    maximum_rows_read: int = Field(ge=0, le=MAX_INVENTORY_PAGE_SIZE + 1)
    elapsed_milliseconds: int = Field(ge=0, le=_MAX_COUNTER)
    heap_delta_bytes: int = Field(ge=0, le=_MAX_COUNTER)
    rss_delta_bytes: int = Field(ge=0, le=_MAX_COUNTER)
    measured_at: datetime
    production_slo: bool = False

    @field_validator("workspace_id")
    @classmethod
    def workspace_must_be_inert(cls, value: str) -> str:
        return _inert_id(value, "catalog scale workspace")

    @field_validator("platform")
    @classmethod
    def platform_must_be_bounded(cls, value: str) -> str:
        return _bounded_text(value, "catalog scale platform", maximum=200)

    @field_validator("measured_at")
    @classmethod
    def measurement_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "catalog scale measurement time")

    @model_validator(mode="after")
    def report_must_remain_local_evidence(self) -> CatalogScaleReport:
        if self.production_slo:
            raise ValueError("local catalog scale report cannot claim a production SLO")
        if self.asset_count == 0 and self.page_count != 0:
            raise ValueError("empty catalog cannot claim nonempty asset paging")
        if self.asset_count > 0 and self.page_count == 0:
            raise ValueError("nonempty catalog requires at least one page")
        return self


def ensure_catalog_refresh_transition(
    current: CatalogRefreshState,
    target: CatalogRefreshStatus,
    *,
    at: datetime,
) -> None:
    """Reject lifecycle jumps and premature expired-lease reclamation."""

    instant = _aware(at, "catalog refresh transition time")
    if current.status.is_terminal:
        raise CatalogRefreshTransitionError(
            CatalogRefreshTransitionErrorCode.TERMINAL_IMMUTABLE,
            "terminal catalog refresh is immutable",
        )
    allowed = {
        CatalogRefreshStatus.REQUESTED: {
            CatalogRefreshStatus.LEASED,
            CatalogRefreshStatus.FAILED,
        },
        CatalogRefreshStatus.LEASED: {
            CatalogRefreshStatus.STAGING,
            CatalogRefreshStatus.FAILED,
            CatalogRefreshStatus.REQUESTED,
        },
        CatalogRefreshStatus.STAGING: {
            CatalogRefreshStatus.COMPLETED,
            CatalogRefreshStatus.FAILED,
            CatalogRefreshStatus.REQUESTED,
        },
    }
    if target not in allowed[current.status]:
        raise CatalogRefreshTransitionError(
            CatalogRefreshTransitionErrorCode.INVALID_STATE,
            "catalog refresh transition is not allowed",
        )
    if target is CatalogRefreshStatus.REQUESTED:
        if current.lease is None:
            raise CatalogRefreshTransitionError(
                CatalogRefreshTransitionErrorCode.INVALID_STATE,
                "catalog refresh reclaim requires an existing lease",
            )
        if current.lease.is_current(instant):
            raise CatalogRefreshTransitionError(
                CatalogRefreshTransitionErrorCode.LEASE_ACTIVE,
                "catalog refresh lease has not expired",
            )
    if instant < current.updated_at:
        raise CatalogRefreshTransitionError(
            CatalogRefreshTransitionErrorCode.TEMPORAL_CONFLICT,
            "catalog refresh transition cannot precede current state",
        )


def catalog_source_page_fingerprint(
    *,
    mode: CatalogRefreshMode,
    sequence: int,
    changes: tuple[CatalogSourceChange, ...],
    next_checkpoint: str | None,
    source_complete: bool,
) -> str:
    payload = {
        "changes": [item.model_dump(mode="json") for item in changes],
        "mode": mode.value,
        "next_checkpoint": next_checkpoint,
        "sequence": sequence,
        "source_complete": source_complete,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class InventoryCursorResource(StrEnum):
    """Closed resources addressable by an external inventory cursor."""

    CONNECTIONS = "connections"
    ASSETS = "assets"
    FIELDS = "fields"


class InventoryPageKey(FrozenDomainModel):
    """Exclusive final tuple key used by deterministic keyset pagination."""

    sort_value: str = Field(min_length=1, max_length=500)
    stable_id: str = Field(min_length=1, max_length=500)

    @field_validator("sort_value")
    @classmethod
    def sort_value_must_fit_storage_key(cls, value: str) -> str:
        return _bounded_text(
            value,
            "inventory page sort key",
            maximum=500,
            maximum_bytes=MAX_ASSET_SORT_KEY_BYTES,
        )

    @field_validator("stable_id")
    @classmethod
    def stable_id_must_be_bounded(cls, value: str) -> str:
        return _bounded_text(
            value,
            "inventory page stable id",
            maximum=500,
            maximum_bytes=2_000,
        )


class InventoryCursorPosition(FrozenDomainModel):
    """Authenticated keyset position and the immutable lifetime of its traversal."""

    last_key: InventoryPageKey
    issued_at: datetime
    expires_at: datetime

    @field_validator("issued_at", "expires_at")
    @classmethod
    def timestamps_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "inventory cursor timestamp")

    @model_validator(mode="after")
    def expiry_must_follow_issue(self) -> InventoryCursorPosition:
        if self.expires_at <= self.issued_at:
            raise ValueError("inventory cursor expiry must follow issue time")
        return self


class InventoryCursorBinding(FrozenDomainModel):
    """Exact scope a signed cursor must authenticate before a protected read."""

    workspace_id: str = Field(min_length=3, max_length=200)
    resource: InventoryCursorResource
    filter_fingerprint: str
    sort_fingerprint: str
    connection_id: CatalogConnectionId | None = None
    asset_id: CatalogAssetId | None = None
    generation: int | None = Field(default=None, ge=1)

    @field_validator("workspace_id")
    @classmethod
    def workspace_must_be_inert(cls, value: str) -> str:
        return _inert_id(value, "inventory cursor workspace")

    @field_validator("filter_fingerprint", "sort_fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "inventory cursor fingerprint")

    @model_validator(mode="after")
    def resource_scope_must_be_exact(self) -> InventoryCursorBinding:
        if self.resource is InventoryCursorResource.CONNECTIONS:
            if (
                self.connection_id is not None
                or self.asset_id is not None
                or self.generation is not None
            ):
                raise ValueError("connection cursor cannot bind asset scope or generation")
        elif self.resource is InventoryCursorResource.ASSETS:
            if self.connection_id is None or self.asset_id is not None or self.generation is None:
                raise ValueError("asset cursor requires one connection generation")
        elif self.connection_id is None or self.asset_id is None or self.generation is None:
            raise ValueError("field cursor requires one asset generation")
        return self


class InventoryPageRequest(FrozenDomainModel):
    """One public bounded read; page size never represents catalog capacity."""

    size: int = Field(default=20, ge=1, le=MAX_INVENTORY_PAGE_SIZE)
    cursor: str | None = Field(default=None, max_length=MAX_INVENTORY_CURSOR_BYTES)

    @field_validator("cursor")
    @classmethod
    def cursor_must_be_opaque_and_bounded(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value or value.strip() != value or _NO_CONTROL.fullmatch(value) is None:
            raise ValueError("inventory cursor is unavailable")
        if len(value.encode("utf-8")) > MAX_INVENTORY_CURSOR_BYTES:
            raise ValueError("inventory cursor is unavailable")
        return value


InventoryItemT = TypeVar("InventoryItemT")


class InventoryStorePage(FrozenDomainModel, Generic[InventoryItemT]):
    """Internal keyset page returned before the external cursor is encoded."""

    items: tuple[InventoryItemT, ...] = Field(max_length=MAX_INVENTORY_PAGE_SIZE)
    resource: InventoryCursorResource
    generation: int | None = Field(default=None, ge=1)
    page_size: int = Field(ge=1, le=MAX_INVENTORY_PAGE_SIZE)
    rows_read: int = Field(ge=0, le=MAX_INVENTORY_PAGE_SIZE + 1)
    has_more: bool
    last_key: InventoryPageKey | None = None
    cursor_valid_until: datetime | None = None

    @field_validator("cursor_valid_until")
    @classmethod
    def cursor_deadline_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _aware(value, "inventory generation cursor deadline")

    @model_validator(mode="after")
    def page_metadata_must_match_materialized_items(self) -> InventoryStorePage[InventoryItemT]:
        item_count = len(self.items)
        if item_count > self.page_size or self.rows_read > self.page_size + 1:
            raise ValueError("inventory page exceeds its requested size")
        if self.rows_read < item_count or self.rows_read > item_count + 1:
            raise ValueError("inventory page rows_read must be item count or item count plus one")
        if self.has_more != (self.rows_read == item_count + 1):
            raise ValueError("inventory page continuation does not match rows_read")
        if self.has_more and not self.items:
            raise ValueError("empty inventory page cannot have a continuation")
        if bool(self.items) != (self.last_key is not None):
            raise ValueError("inventory page final key does not match its items")
        if self.resource is InventoryCursorResource.CONNECTIONS:
            if self.generation is not None:
                raise ValueError("connection pages cannot bind a catalog generation")
            if self.cursor_valid_until is not None:
                raise ValueError("connection pages cannot bind a generation cursor deadline")
        elif self.generation is None:
            raise ValueError("asset and field pages require an active generation")
        return self


class InventoryPage(FrozenDomainModel, Generic[InventoryItemT]):
    """Sanitized public page with an opaque continuation and no full collection."""

    items: tuple[InventoryItemT, ...] = Field(max_length=MAX_INVENTORY_PAGE_SIZE)
    resource: InventoryCursorResource
    generation: int | None = Field(default=None, ge=1)
    next_cursor: str | None = Field(default=None, max_length=MAX_INVENTORY_CURSOR_BYTES)
    as_of: datetime
    stale: bool = False

    @field_validator("next_cursor")
    @classmethod
    def cursor_must_be_bounded(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if (
            not value
            or value.strip() != value
            or _NO_CONTROL.fullmatch(value) is None
            or len(value.encode("utf-8")) > MAX_INVENTORY_CURSOR_BYTES
        ):
            raise ValueError("inventory cursor is unavailable")
        return value

    @field_validator("as_of")
    @classmethod
    def timestamp_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "inventory page timestamp")

    @model_validator(mode="after")
    def generation_must_match_resource(self) -> InventoryPage[InventoryItemT]:
        if self.resource is InventoryCursorResource.CONNECTIONS:
            if self.generation is not None:
                raise ValueError("connection pages cannot expose a catalog generation")
        elif self.generation is None:
            raise ValueError("asset and field pages require an active generation")
        return self


def inventory_filter_fingerprint(payload: dict[str, object]) -> str:
    """Fingerprint a caller-normalized filter without depending on an adapter."""

    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _inert_id(value: str, label: str) -> str:
    if _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{label} must be an inert lowercase identifier")
    return value


def _catalog_connection_id(value: str, label: str) -> str:
    if _CATALOG_CONNECTION_ID.fullmatch(value) is None:
        raise ValueError(f"{label} must match durable storage")
    return value


def _catalog_environment(value: str) -> str:
    if _CATALOG_ENVIRONMENT.fullmatch(value) is None:
        raise ValueError("catalog environment must match durable storage")
    return value


def _bounded_text(
    value: str,
    label: str,
    *,
    maximum: int,
    maximum_bytes: int | None = None,
) -> str:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{label} is not bounded inert text") from None
    if (
        not value
        or value.strip() != value
        or len(value) > maximum
        or (maximum_bytes is not None and len(encoded) > maximum_bytes)
        or _NO_CONTROL.fullmatch(value) is None
    ):
        raise ValueError(f"{label} is not bounded inert text")
    return value


def _bounded_storage_text(value: str, label: str) -> str:
    return _bounded_text(
        value,
        label,
        maximum=200,
        maximum_bytes=MAX_CATALOG_METADATA_BYTES,
    )


def _bounded_catalog_terms(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(_bounded_text(value, "catalog field term", maximum=200) for value in values)
    if normalized != tuple(sorted(set(normalized))):
        raise ValueError("catalog field terms must be unique and sorted")
    if sum(len(value.encode("utf-8")) for value in normalized) > MAX_CATALOG_TERM_ARRAY_BYTES:
        raise ValueError("catalog field terms exceed durable storage bytes")
    return normalized


def _bounded_qualified_name(value: str, label: str) -> str:
    normalized = _bounded_text(
        value,
        label,
        maximum=500,
        maximum_bytes=MAX_ASSET_SORT_KEY_BYTES,
    )
    table_name = normalized.rsplit(".", maxsplit=1)[-1]
    _bounded_text(
        table_name,
        f"{label} final component",
        maximum=200,
        maximum_bytes=MAX_FIELD_SORT_KEY_BYTES,
    )
    return normalized


def _bounded_field_path(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    normalized = tuple(_bounded_text(value, f"{label} segment", maximum=200) for value in values)
    if not normalized:
        raise ValueError(f"{label} is not bounded inert text")
    joined = ".".join(normalized)
    try:
        joined_size = len(joined.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError(f"{label} is not bounded inert text") from None
    if joined_size > MAX_FIELD_PATH_BYTES:
        raise ValueError(f"{label} is not bounded inert text")
    _bounded_text(
        normalized[-1],
        f"{label} sort key",
        maximum=200,
        maximum_bytes=MAX_FIELD_SORT_KEY_BYTES,
    )
    return normalized


def _normalize_filter_text(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if len(value) > 200 or _NO_CONTROL.fullmatch(value) is None:
        raise ValueError(f"{label} is not bounded inert text")
    normalized = " ".join(value.split()).casefold()
    if not normalized:
        raise ValueError(f"{label} must not be blank")
    return normalized


def _sha256(value: str, label: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return value
