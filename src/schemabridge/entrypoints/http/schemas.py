"""Bounded HTTP schemas for execution jobs and public catalog inventory."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field

from schemabridge.application.ports.semantic_change_read import (
    SemanticChangeFindingFilter,
    SemanticChangeFindingPublic,
    SemanticChangeImpactFilter,
    SemanticChangeImpactPublic,
    SemanticChangePage,
    SemanticChangeReportFilter,
    SemanticChangeReportPublic,
    SemanticChangeTargetKind,
)
from schemabridge.domain.background_jobs import BackgroundJob, JobStatus
from schemabridge.domain.catalog_inventory import (
    CATALOG_CONNECTION_ID_PATTERN,
    CATALOG_ENVIRONMENT_PATTERN,
    CatalogAssetFilter,
    CatalogAssetSummary,
    CatalogConnectionFilter,
    CatalogConnectionKind,
    CatalogConnectionRegistrationResult,
    CatalogConnectionStatus,
    CatalogConnectionSummary,
    CatalogFieldFilter,
    CatalogFieldSummary,
    CatalogRefreshMode,
    CatalogRefreshRequestResult,
    CatalogRefreshStatus,
    CatalogRefreshSummary,
    InventoryPage,
)
from schemabridge.domain.resolution import MAX_REJECTED_SOURCE_TOTAL
from schemabridge.domain.semantic_change import (
    SemanticChangeKind,
    SemanticChangeSeverity,
    SemanticChangeStatus,
    SemanticImpactKind,
)
from schemabridge.domain.semantic_registry import PhysicalValueType


class _StrictApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExecutionJobSubmissionRequest(_StrictApiModel):
    """The only caller-controlled facts accepted by the M24 command."""

    expected_workflow_revision: int = Field(ge=1)
    expected_plan_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation: Literal["EXECUTE GOVERNED PREVIEW"]


class ExecutionJobCancellationRequest(_StrictApiModel):
    """Explicit cooperative cancellation request."""

    confirmation: Literal["CANCEL EXECUTION JOB"]


class JobRejectionSummary(_StrictApiModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    count: int = Field(ge=1, le=10_000)


class ExecutionResultResponse(_StrictApiModel):
    workflow_revision: int = Field(ge=1)
    stage: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    row_count: int = Field(ge=0, le=10_000)
    preview_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    rejected_count: int = Field(ge=0, le=MAX_REJECTED_SOURCE_TOTAL)
    rejection_counts: tuple[JobRejectionSummary, ...] = ()
    unclassified_rejection_count: int = Field(ge=0, le=MAX_REJECTED_SOURCE_TOTAL)
    rejection_counts_complete: bool
    rejection_truncated: bool
    truncated: bool
    completed_at: datetime


class ExecutionJobResponse(_StrictApiModel):
    """Sanitized job projection with no lease capability or protected payload."""

    job_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,199}$")
    status: JobStatus
    workflow_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,199}$")
    expected_workflow_revision: int = Field(ge=1)
    expected_plan_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_count: int = Field(ge=0, le=10)
    max_attempts: int = Field(ge=1, le=10)
    created_at: datetime
    updated_at: datetime
    cancel_requested_at: datetime | None
    completed_at: datetime | None
    failure_code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{1,63}$",
    )
    result: ExecutionResultResponse | None

    @classmethod
    def from_domain(cls, job: BackgroundJob) -> Self:
        result = job.result
        return cls(
            job_id=job.id,
            status=job.status,
            workflow_id=job.authorization.workflow_id,
            expected_workflow_revision=job.authorization.expected_workflow_revision,
            expected_plan_fingerprint=job.authorization.expected_plan_fingerprint,
            attempt_count=job.attempt_count,
            max_attempts=job.max_attempts,
            created_at=job.created_at,
            updated_at=job.updated_at,
            cancel_requested_at=job.cancel_requested_at,
            completed_at=job.completed_at,
            failure_code=job.failure.code.value if job.failure is not None else None,
            result=(
                None
                if result is None
                else ExecutionResultResponse(
                    workflow_revision=result.workflow_revision,
                    stage=result.stage.value,
                    row_count=result.row_count,
                    preview_fingerprint=result.preview_fingerprint,
                    rejected_count=result.rejected_count,
                    rejection_counts=tuple(
                        JobRejectionSummary(code=item.code, count=item.count)
                        for item in result.rejection_code_counts
                    ),
                    unclassified_rejection_count=result.unclassified_rejection_count,
                    rejection_counts_complete=result.rejection_counts_complete,
                    rejection_truncated=result.rejection_truncated,
                    truncated=result.truncated,
                    completed_at=result.completed_at,
                )
            ),
        )


class ExecutionJobSubmissionResponse(_StrictApiModel):
    job: ExecutionJobResponse
    replayed: bool


class CatalogConnectionListQuery(_StrictApiModel):
    """Strict indexed filters for one tenant's connection page."""

    query: str | None = Field(default=None, min_length=1, max_length=200)
    status: CatalogConnectionStatus | None = None
    page_size: int = Field(default=20, ge=1, le=50)
    cursor: str | None = Field(default=None, min_length=1, max_length=1_024)

    def to_filter(self) -> CatalogConnectionFilter:
        return CatalogConnectionFilter(query=self.query, status=self.status)


class CatalogAssetListQuery(_StrictApiModel):
    """Strict indexed filters for one connection generation."""

    query: str | None = Field(default=None, min_length=1, max_length=200)
    platform: str | None = Field(default=None, min_length=1, max_length=100)
    schema_name: str | None = Field(default=None, min_length=1, max_length=200)
    page_size: int = Field(default=20, ge=1, le=50)
    cursor: str | None = Field(default=None, min_length=1, max_length=1_024)
    generation: int | None = Field(default=None, ge=1)

    def to_filter(self) -> CatalogAssetFilter:
        return CatalogAssetFilter(
            query=self.query,
            platform=self.platform,
            schema_name=self.schema_name,
        )


class CatalogFieldListQuery(_StrictApiModel):
    """Strict indexed filters for one asset's field page."""

    query: str | None = Field(default=None, min_length=1, max_length=200)
    native_type: str | None = Field(default=None, min_length=1, max_length=200)
    page_size: int = Field(default=20, ge=1, le=50)
    cursor: str | None = Field(default=None, min_length=1, max_length=1_024)
    generation: int | None = Field(default=None, ge=1)

    def to_filter(self) -> CatalogFieldFilter:
        return CatalogFieldFilter(query=self.query, native_type=self.native_type)


class SemanticChangeReportListQuery(_StrictApiModel):
    """Bounded filters for one tenant's immutable report summaries."""

    status: SemanticChangeStatus | None = None
    page_size: int = Field(default=20, ge=1, le=50)
    cursor: str | None = Field(default=None, min_length=1, max_length=1_024)

    def to_filter(self) -> SemanticChangeReportFilter:
        return SemanticChangeReportFilter(status=self.status)


class SemanticChangeFindingListQuery(_StrictApiModel):
    """Bounded filters for one visible report's deterministic findings."""

    kind: SemanticChangeKind | None = None
    severity: SemanticChangeSeverity | None = None
    page_size: int = Field(default=20, ge=1, le=50)
    cursor: str | None = Field(default=None, min_length=1, max_length=1_024)

    def to_filter(self) -> SemanticChangeFindingFilter:
        return SemanticChangeFindingFilter(kind=self.kind, severity=self.severity)


class SemanticChangeImpactListQuery(_StrictApiModel):
    """Bounded filters for one visible report's deduplicated blast radius."""

    kind: SemanticImpactKind | None = None
    page_size: int = Field(default=20, ge=1, le=50)
    cursor: str | None = Field(default=None, min_length=1, max_length=1_024)

    def to_filter(self) -> SemanticChangeImpactFilter:
        return SemanticChangeImpactFilter(kind=self.kind)


class CatalogConnectionRegistrationRequest(_StrictApiModel):
    """Public connection metadata; credential material has no representable field."""

    connection_id: str = Field(pattern=CATALOG_CONNECTION_ID_PATTERN)
    display_name: str = Field(min_length=1, max_length=200)
    kind: CatalogConnectionKind
    environment: str = Field(
        min_length=1,
        max_length=80,
        pattern=CATALOG_ENVIRONMENT_PATTERN,
    )
    catalog_scope: str = Field(min_length=1, max_length=200)
    platform_instance: str | None = Field(default=None, min_length=1, max_length=200)
    confirmation: Literal["REGISTER CATALOG CONNECTION"]


class CatalogConnectionDisableRequest(_StrictApiModel):
    confirmation: Literal["DISABLE CATALOG CONNECTION"]


class CatalogRefreshRequest(_StrictApiModel):
    mode: CatalogRefreshMode
    confirmation: Literal["REQUEST CATALOG REFRESH"]


class CatalogConnectionResponse(_StrictApiModel):
    """Tenant-minimized public connection projection."""

    connection_id: str
    display_name: str
    kind: CatalogConnectionKind
    environment: str
    catalog_scope: str
    platform_instance: str | None
    status: CatalogConnectionStatus
    active_generation: int | None
    asset_count: int
    field_count: int
    last_completed_at: datetime | None
    stale: bool

    @classmethod
    def from_domain(cls, value: CatalogConnectionSummary) -> Self:
        return cls(
            connection_id=value.connection_id.root,
            display_name=value.display_name,
            kind=value.kind,
            environment=value.environment,
            catalog_scope=value.catalog_scope,
            platform_instance=value.platform_instance,
            status=value.status,
            active_generation=value.active_generation,
            asset_count=value.asset_count,
            field_count=value.field_count,
            last_completed_at=value.last_completed_at,
            stale=value.stale,
        )


class CatalogAssetResponse(_StrictApiModel):
    """Public catalog asset without workspace or route credentials."""

    connection_id: str
    asset_id: str
    generation: int
    qualified_name: str
    display_name: str
    platform: str
    environment: str
    database_name: str | None
    schema_name: str | None
    description: str | None
    field_count: int
    metadata_fingerprint: str
    observed_at: datetime

    @classmethod
    def from_domain(cls, value: CatalogAssetSummary) -> Self:
        return cls(
            connection_id=value.locator.connection_id.root,
            asset_id=value.locator.asset_id.root,
            generation=value.generation,
            qualified_name=value.qualified_name,
            display_name=value.display_name,
            platform=value.platform,
            environment=value.environment,
            database_name=value.database_name,
            schema_name=value.schema_name,
            description=value.description,
            field_count=value.field_count,
            metadata_fingerprint=value.metadata_fingerprint,
            observed_at=value.observed_at,
        )


class CatalogFieldResponse(_StrictApiModel):
    """Public catalog field metadata without samples or source values."""

    connection_id: str
    asset_id: str
    field_path: tuple[str, ...]
    generation: int
    native_type: str | None
    normalized_type: PhysicalValueType | None
    description: str | None
    nullable: bool | None
    is_part_of_key: bool | None
    tags: tuple[str, ...]
    glossary_terms: tuple[str, ...]
    metadata_fingerprint: str
    observed_at: datetime

    @classmethod
    def from_domain(cls, value: CatalogFieldSummary) -> Self:
        return cls(
            connection_id=value.locator.asset.connection_id.root,
            asset_id=value.locator.asset.asset_id.root,
            field_path=value.locator.field_path,
            generation=value.generation,
            native_type=value.native_type,
            normalized_type=value.normalized_type,
            description=value.description,
            nullable=value.nullable,
            is_part_of_key=value.is_part_of_key,
            tags=value.tags,
            glossary_terms=value.glossary_terms,
            metadata_fingerprint=value.metadata_fingerprint,
            observed_at=value.observed_at,
        )


class CatalogConnectionPageResponse(_StrictApiModel):
    resource: Literal["connections"] = "connections"
    items: tuple[CatalogConnectionResponse, ...] = Field(max_length=50)
    next_cursor: str | None = Field(default=None, max_length=1_024)
    as_of: datetime
    stale: bool

    @classmethod
    def from_domain(
        cls,
        value: InventoryPage[CatalogConnectionSummary],
    ) -> Self:
        return cls(
            items=tuple(CatalogConnectionResponse.from_domain(item) for item in value.items),
            next_cursor=value.next_cursor,
            as_of=value.as_of,
            stale=value.stale,
        )


class CatalogAssetPageResponse(_StrictApiModel):
    resource: Literal["assets"] = "assets"
    generation: int = Field(ge=1)
    items: tuple[CatalogAssetResponse, ...] = Field(max_length=50)
    next_cursor: str | None = Field(default=None, max_length=1_024)
    as_of: datetime
    stale: bool

    @classmethod
    def from_domain(cls, value: InventoryPage[CatalogAssetSummary]) -> Self:
        if value.generation is None:
            raise ValueError("asset page is missing its catalog generation")
        return cls(
            generation=value.generation,
            items=tuple(CatalogAssetResponse.from_domain(item) for item in value.items),
            next_cursor=value.next_cursor,
            as_of=value.as_of,
            stale=value.stale,
        )


class CatalogFieldPageResponse(_StrictApiModel):
    resource: Literal["fields"] = "fields"
    generation: int = Field(ge=1)
    items: tuple[CatalogFieldResponse, ...] = Field(max_length=50)
    next_cursor: str | None = Field(default=None, max_length=1_024)
    as_of: datetime
    stale: bool

    @classmethod
    def from_domain(cls, value: InventoryPage[CatalogFieldSummary]) -> Self:
        if value.generation is None:
            raise ValueError("field page is missing its catalog generation")
        return cls(
            generation=value.generation,
            items=tuple(CatalogFieldResponse.from_domain(item) for item in value.items),
            next_cursor=value.next_cursor,
            as_of=value.as_of,
            stale=value.stale,
        )


class CatalogConnectionRegistrationResponse(_StrictApiModel):
    connection: CatalogConnectionResponse
    replayed: bool

    @classmethod
    def from_domain(cls, value: CatalogConnectionRegistrationResult) -> Self:
        return cls(
            connection=CatalogConnectionResponse.from_domain(value.connection),
            replayed=value.replayed,
        )


class CatalogRefreshResponse(_StrictApiModel):
    """Safe refresh state without lease capability, checkpoint, or requester."""

    refresh_id: str
    connection_id: str
    mode: CatalogRefreshMode
    status: CatalogRefreshStatus
    base_generation: int
    target_generation: int
    source_page_count: int
    asset_count: int
    field_count: int
    source_complete: bool
    catalog_fingerprint: str | None
    failure_code: str | None
    requested_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    @classmethod
    def from_domain(cls, value: CatalogRefreshSummary) -> Self:
        return cls(
            refresh_id=value.refresh_id.root,
            connection_id=value.connection_id.root,
            mode=value.mode,
            status=value.status,
            base_generation=value.base_generation,
            target_generation=value.target_generation,
            source_page_count=value.source_page_count,
            asset_count=value.asset_count,
            field_count=value.field_count,
            source_complete=value.source_complete,
            catalog_fingerprint=value.catalog_fingerprint,
            failure_code=(value.failure_code.value if value.failure_code is not None else None),
            requested_at=value.requested_at,
            updated_at=value.updated_at,
            completed_at=value.completed_at,
        )


class CatalogRefreshRequestResponse(_StrictApiModel):
    refresh: CatalogRefreshResponse
    replayed: bool

    @classmethod
    def from_domain(cls, value: CatalogRefreshRequestResult) -> Self:
        return cls(
            refresh=CatalogRefreshResponse.from_domain(value.refresh),
            replayed=value.replayed,
        )


class SemanticChangeReportResponse(_StrictApiModel):
    """Minimized report projection with no physical schema or evidence payload."""

    report_id: str = Field(pattern=r"^report_[0-9a-f]{64}$")
    status: SemanticChangeStatus
    pointer_generation: int = Field(ge=1)
    pointer_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_version: int = Field(ge=1)
    registry_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_generation_count: int = Field(ge=0, le=100_000)
    observation_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_revision: int | None = Field(default=None, ge=1)
    baseline_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    finding_count: int = Field(ge=0, le=2_000)
    mapping_impact_count: int = Field(ge=0, le=10_000)
    join_impact_count: int = Field(ge=0, le=10_000)
    workflow_impact_count: int = Field(ge=0, le=10_000)
    recipe_impact_count: int = Field(ge=0, le=10_000)
    impacts_complete: bool
    dependency_watermark: int = Field(ge=0)
    impact_set_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    inspected_at: datetime
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_projection(cls, value: SemanticChangeReportPublic) -> Self:
        return cls(
            report_id=value.report_id,
            status=value.status,
            pointer_generation=value.pointer_generation,
            pointer_fingerprint=value.pointer_fingerprint,
            registry_version=value.registry_version,
            registry_fingerprint=value.registry_fingerprint,
            catalog_generation_count=value.catalog_generation_count,
            observation_fingerprint=value.observation_fingerprint,
            baseline_revision=value.baseline_revision,
            baseline_fingerprint=value.baseline_fingerprint,
            finding_count=value.finding_count,
            mapping_impact_count=value.mapping_impact_count,
            join_impact_count=value.join_impact_count,
            workflow_impact_count=value.workflow_impact_count,
            recipe_impact_count=value.recipe_impact_count,
            impacts_complete=value.impacts_complete,
            dependency_watermark=value.dependency_watermark,
            impact_set_fingerprint=value.impact_set_fingerprint,
            inspected_at=value.inspected_at,
            fingerprint=value.fingerprint,
        )


class SemanticChangeFindingResponse(_StrictApiModel):
    """Sanitized change classification without raw definitions or field paths."""

    finding_id: str = Field(pattern=r"^finding_[0-9a-f]{64}$")
    kind: SemanticChangeKind
    severity: SemanticChangeSeverity
    target_kind: SemanticChangeTargetKind
    target_id: str = Field(min_length=1, max_length=200)
    target_version: int = Field(ge=1)
    previous_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    current_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    risks: tuple[str, ...] = Field(min_length=1, max_length=20)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_projection(cls, value: SemanticChangeFindingPublic) -> Self:
        return cls(
            finding_id=value.finding_id,
            kind=value.kind,
            severity=value.severity,
            target_kind=value.target_kind,
            target_id=value.target_id,
            target_version=value.target_version,
            previous_fingerprint=value.previous_fingerprint,
            current_fingerprint=value.current_fingerprint,
            risks=value.risks,
            fingerprint=value.fingerprint,
        )


class SemanticChangeImpactResponse(_StrictApiModel):
    """Tenant-scoped downstream artifact projection."""

    kind: SemanticImpactKind
    artifact_id: str = Field(min_length=1, max_length=200)
    artifact_version: int | None = Field(default=None, ge=1)
    finding_ids: tuple[str, ...] = Field(min_length=1, max_length=2_000)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_projection(cls, value: SemanticChangeImpactPublic) -> Self:
        return cls(
            kind=value.kind,
            artifact_id=value.artifact_id,
            artifact_version=value.artifact_version,
            finding_ids=value.finding_ids,
            fingerprint=value.fingerprint,
        )


class SemanticChangeReportPageResponse(_StrictApiModel):
    resource: Literal["reports"] = "reports"
    items: tuple[SemanticChangeReportResponse, ...] = Field(max_length=50)
    next_cursor: str | None = Field(default=None, max_length=1_024)
    as_of: datetime

    @classmethod
    def from_projection(
        cls,
        value: SemanticChangePage[SemanticChangeReportPublic],
    ) -> Self:
        return cls(
            items=tuple(SemanticChangeReportResponse.from_projection(item) for item in value.items),
            next_cursor=value.next_cursor,
            as_of=value.as_of,
        )


class SemanticChangeFindingPageResponse(_StrictApiModel):
    resource: Literal["findings"] = "findings"
    items: tuple[SemanticChangeFindingResponse, ...] = Field(max_length=50)
    next_cursor: str | None = Field(default=None, max_length=1_024)
    as_of: datetime

    @classmethod
    def from_projection(
        cls,
        value: SemanticChangePage[SemanticChangeFindingPublic],
    ) -> Self:
        return cls(
            items=tuple(
                SemanticChangeFindingResponse.from_projection(item) for item in value.items
            ),
            next_cursor=value.next_cursor,
            as_of=value.as_of,
        )


class SemanticChangeImpactPageResponse(_StrictApiModel):
    resource: Literal["impacts"] = "impacts"
    items: tuple[SemanticChangeImpactResponse, ...] = Field(max_length=50)
    next_cursor: str | None = Field(default=None, max_length=1_024)
    as_of: datetime

    @classmethod
    def from_projection(
        cls,
        value: SemanticChangePage[SemanticChangeImpactPublic],
    ) -> Self:
        return cls(
            items=tuple(SemanticChangeImpactResponse.from_projection(item) for item in value.items),
            next_cursor=value.next_cursor,
            as_of=value.as_of,
        )


class HealthResponse(_StrictApiModel):
    status: Literal["live", "ready", "not_ready"]


class ProblemResponse(_StrictApiModel):
    type: str = Field(pattern=r"^urn:schemabridge:problem:[a-z][a-z0-9_]{1,63}$")
    title: str = Field(min_length=1, max_length=120)
    status: int = Field(ge=400, le=599)
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
