"""Bounded HTTP schemas for execution jobs and public catalog inventory."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    CatalogAssetId,
    CatalogAssetSummary,
    CatalogConnectionFilter,
    CatalogConnectionId,
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
from schemabridge.domain.concepts import LogicalModelRef
from schemabridge.domain.decisions import DecisionAction
from schemabridge.domain.joins import JoinProposal
from schemabridge.domain.physical_types import PhysicalValueType
from schemabridge.domain.registry_change_authoring import (
    RegistryChangeAuditRecord,
    RegistryJoinChangeSnapshot,
    RegistryJoinDraftMutation,
    RegistryJoinPreparation,
    RegistryJoinProfileAuthoringMutation,
    RegistryJoinProfileAuthoringRequest,
    RequestRegistryJoinProfileInput,
)
from schemabridge.domain.registry_changes import (
    PreparedRegistryJoinProposal,
    RegistryJoinChangeDraft,
)
from schemabridge.domain.registry_model_change_authoring import (
    CreateRegistryModelChangeInput,
    RegistryIncidentJoinInput,
    RegistryModelChangeAuditRecord,
    RegistryModelChangeDraft,
    RegistryModelChangeMutation,
    RegistryModelChangeSnapshot,
    RegistryModelChangeStatus,
    RegistryModelJoinProfileAuthoringRequest,
    RegistryModelJoinProfileMutation,
    RequestRegistryModelJoinProfileInput,
)
from schemabridge.domain.registry_model_changes import (
    PreparedRegistryModelReplacementProposal,
    RegistryModelChangeKind,
    RegistryModelJoinProfileWitness,
)
from schemabridge.domain.registry_publication import (
    PublicationReadbackReceipt,
    PublishableRegistryVersion,
    RegistryPublicationAuthorization,
    RegistryPublicationAuthorizationConfirmation,
)
from schemabridge.domain.registry_publication_jobs import (
    RegistryPublicationJob,
    RegistryPublicationJobStatus,
)
from schemabridge.domain.resolution import MAX_REJECTED_SOURCE_TOTAL
from schemabridge.domain.semantic_change import (
    SemanticChangeKind,
    SemanticChangeSeverity,
    SemanticChangeStatus,
    SemanticImpactKind,
)
from schemabridge.domain.semantic_onboarding import (
    MAX_ONBOARDING_EVIDENCE,
    MAX_ONBOARDING_MAPPINGS,
    CreateSemanticOnboardingRequest,
    OnboardingEvidence,
    OnboardingRegistryBase,
    PhysicalCatalogObservation,
    PreflightSemanticOnboardingRequest,
    PreparedSemanticOnboardingProposal,
    SemanticModelDefinition,
    SemanticOnboardingAuditRecord,
    SemanticOnboardingDecision,
    SemanticOnboardingDraft,
    SemanticOnboardingDraftMutation,
    SemanticOnboardingMappingInput,
    SemanticOnboardingPreflight,
    SemanticOnboardingPreflightSelection,
    SemanticOnboardingPreparation,
    SemanticOnboardingStatus,
)
from schemabridge.domain.semantic_profile_jobs import SemanticJoinProfileJobStatus
from schemabridge.domain.semantic_registry import SemanticRegistryScope


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


class RegistryPublicationSubmissionRequest(_StrictApiModel):
    """Reserve one exact immutable M33 proposal for publication."""

    proposal_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,199}$")
    confirmed_proposal_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class RegistryPublicationAuthorizationRequest(_StrictApiModel):
    """Fresh explicit approval of the complete assembled registry candidate."""

    expected_revision: int = Field(ge=1)
    confirmed_candidate_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation: RegistryPublicationAuthorizationConfirmation


class RegistryPublicationCancellationRequest(_StrictApiModel):
    """Optimistic cooperative cancellation of one publication job."""

    expected_revision: int = Field(ge=1)


class RegistryPublicationAuthorizationResponse(_StrictApiModel):
    """Public approval projection without session internals."""

    authorization_id: str = Field(min_length=3, max_length=200)
    candidate_id: str = Field(min_length=3, max_length=200)
    candidate_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    target: str = Field(min_length=3, max_length=500)
    actor_id: str = Field(min_length=1, max_length=200)
    approved_at: datetime
    expires_at: datetime
    confirmation: RegistryPublicationAuthorizationConfirmation

    @classmethod
    def from_domain(cls, value: RegistryPublicationAuthorization) -> Self:
        return cls(
            authorization_id=value.id,
            candidate_id=value.candidate_id,
            candidate_fingerprint=value.candidate_fingerprint,
            registry_fingerprint=value.registry_fingerprint,
            target=value.target,
            actor_id=value.actor_id,
            approved_at=value.approved_at,
            expires_at=value.expires_at,
            confirmation=value.confirmation,
        )


class RegistryPublicationJobResponse(_StrictApiModel):
    """Tenant-safe state without lease, idempotency, request, or credential material."""

    job_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,199}$")
    scope: SemanticRegistryScope
    status: RegistryPublicationJobStatus
    revision: int = Field(ge=1)
    attempt_count: int = Field(ge=0, le=10)
    max_attempts: int = Field(ge=1, le=10)
    proposal_kind: Literal[
        "onboarding_additive_v1",
        "add_join_v1",
        "replace_model_v1",
    ]
    proposal_id: str = Field(min_length=3, max_length=200)
    proposal_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_registry_version: int = Field(ge=1)
    candidate: PublishableRegistryVersion | None
    authorization: RegistryPublicationAuthorizationResponse | None
    receipt: PublicationReadbackReceipt | None
    failure_code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{1,63}$",
    )
    submitted_at: datetime
    updated_at: datetime
    cancel_requested_at: datetime | None

    @classmethod
    def from_domain(cls, value: RegistryPublicationJob) -> Self:
        return cls(
            job_id=value.id,
            scope=value.scope,
            status=value.status,
            revision=value.revision,
            attempt_count=value.attempts,
            max_attempts=value.max_attempts,
            proposal_kind=(
                "replace_model_v1"
                if isinstance(value.proposal, PreparedRegistryModelReplacementProposal)
                else (
                    "add_join_v1"
                    if isinstance(value.proposal, PreparedRegistryJoinProposal)
                    else "onboarding_additive_v1"
                )
            ),
            proposal_id=value.proposal.id,
            proposal_fingerprint=value.proposal.fingerprint,
            target_registry_version=value.proposal.target_registry_version,
            candidate=value.candidate,
            authorization=(
                None
                if value.authorization is None
                else RegistryPublicationAuthorizationResponse.from_domain(value.authorization)
            ),
            receipt=value.receipt,
            failure_code=(value.failure_code.value if value.failure_code is not None else None),
            submitted_at=value.submitted_at,
            updated_at=value.updated_at,
            cancel_requested_at=value.cancel_requested_at,
        )


class RegistryPublicationSubmissionResponse(_StrictApiModel):
    job: RegistryPublicationJobResponse
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


class SemanticOnboardingDraftListQuery(_StrictApiModel):
    """Bounded, non-cursor first vertical for one authenticated workspace."""

    limit: int = Field(default=50, ge=1, le=50)


class SemanticOnboardingDraftInspectionQuery(_StrictApiModel):
    """Bounded most-recent history window for one draft snapshot."""

    history_limit: int = Field(default=25, ge=1, le=50)


class SemanticOnboardingPreflightSelectionRequest(_StrictApiModel):
    asset_id: CatalogAssetId
    field_path: tuple[str, ...] = Field(min_length=1, max_length=1)

    def to_domain(self) -> SemanticOnboardingPreflightSelection:
        return SemanticOnboardingPreflightSelection(**self.model_dump(mode="python"))


class SemanticOnboardingPreflightRequest(_StrictApiModel):
    """Read-only exact catalog selections; physical authority is server-derived."""

    connection_id: CatalogConnectionId
    selections: tuple[SemanticOnboardingPreflightSelectionRequest, ...] = Field(
        min_length=1,
        max_length=MAX_ONBOARDING_MAPPINGS,
    )

    def to_domain(self) -> PreflightSemanticOnboardingRequest:
        return PreflightSemanticOnboardingRequest(
            connection_id=self.connection_id,
            selections=tuple(item.to_domain() for item in self.selections),
        )


class SemanticOnboardingPreflightResponse(_StrictApiModel):
    scope: SemanticRegistryScope
    connection_id: CatalogConnectionId
    catalog_generation: int = Field(ge=1)
    catalog_generation_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_registry: OnboardingRegistryBase
    observations: tuple[PhysicalCatalogObservation, ...] = Field(
        min_length=1,
        max_length=MAX_ONBOARDING_MAPPINGS,
    )
    preflight_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    external_writes_performed: Literal[False] = False

    @classmethod
    def from_domain(cls, value: SemanticOnboardingPreflight) -> Self:
        return cls(
            scope=value.scope,
            connection_id=value.connection_id,
            catalog_generation=value.catalog_generation,
            catalog_generation_fingerprint=value.catalog_generation_fingerprint,
            base_registry=value.base_registry,
            observations=value.observations,
            preflight_fingerprint=value.fingerprint,
        )


class SemanticOnboardingDraftCreateRequest(_StrictApiModel):
    """Client-safe draft input; actor, workspace and registry scope are server-derived."""

    draft_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,79}$")
    connection_id: CatalogConnectionId
    catalog_generation: int = Field(ge=1)
    catalog_generation_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_base_registry: OnboardingRegistryBase
    confirmed_preflight_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: SemanticModelDefinition
    mappings: tuple[SemanticOnboardingMappingInput, ...] = Field(
        min_length=1,
        max_length=MAX_ONBOARDING_MAPPINGS,
    )

    def to_domain(self) -> CreateSemanticOnboardingRequest:
        return CreateSemanticOnboardingRequest(**self.model_dump(mode="python"))


class SemanticOnboardingDecisionRequest(_StrictApiModel):
    """One explicit model or mapping decision with no client-controlled authority fields."""

    target_id: str = Field(min_length=3, max_length=200)
    action: Literal[DecisionAction.APPROVE, DecisionAction.REJECT]
    expected_revision: int = Field(ge=1)
    confirmed_draft_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    rationale: str = Field(min_length=12, max_length=2_000)
    evidence: tuple[OnboardingEvidence, ...] = Field(
        default=(),
        max_length=MAX_ONBOARDING_EVIDENCE,
    )

    @field_validator("rationale")
    @classmethod
    def rationale_must_be_meaningful(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) < 12:
            raise ValueError("semantic onboarding rationale must contain 12 meaningful characters")
        return stripped


class SemanticOnboardingPreparationRequest(_StrictApiModel):
    expected_revision: int = Field(ge=1)
    confirmed_draft_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class SemanticOnboardingDraftSummaryResponse(_StrictApiModel):
    """Bounded list projection without decision, evidence or physical-field payloads."""

    draft_id: str
    owner_actor_id: str
    model_id: LogicalModelRef
    connection_id: CatalogConnectionId
    revision: int = Field(ge=1)
    status: SemanticOnboardingStatus
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    updated_at: datetime

    @classmethod
    def from_domain(cls, value: SemanticOnboardingDraft) -> Self:
        return cls(
            draft_id=value.id,
            owner_actor_id=value.owner_actor_id,
            model_id=value.model.definition.id,
            connection_id=value.connection_id,
            revision=value.revision,
            status=value.status,
            fingerprint=value.fingerprint,
            updated_at=value.updated_at,
        )


class SemanticOnboardingDraftListResponse(_StrictApiModel):
    resource: Literal["semantic_onboarding_drafts"] = "semantic_onboarding_drafts"
    state: Literal["not_configured", "configured"]
    items: tuple[SemanticOnboardingDraftSummaryResponse, ...] = Field(max_length=50)

    @classmethod
    def from_domain(
        cls,
        values: tuple[SemanticOnboardingDraft, ...],
    ) -> Self:
        return cls(
            state="not_configured" if not values else "configured",
            items=tuple(
                SemanticOnboardingDraftSummaryResponse.from_domain(item) for item in values
            ),
        )


class SemanticOnboardingDraftMutationResponse(_StrictApiModel):
    draft: SemanticOnboardingDraft
    draft_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    replayed: bool
    external_writes_performed: Literal[False] = False

    @classmethod
    def from_domain(cls, value: SemanticOnboardingDraftMutation) -> Self:
        return cls(
            draft=value.draft,
            draft_fingerprint=value.draft.fingerprint,
            replayed=value.replayed,
        )


class SemanticOnboardingDraftResponse(_StrictApiModel):
    draft: SemanticOnboardingDraft
    draft_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    audit_visible: bool
    history_truncated: bool
    decisions: tuple[SemanticOnboardingDecision, ...] = Field(max_length=50)
    proposals: tuple[PreparedSemanticOnboardingProposal, ...] = Field(max_length=50)
    audit: tuple[SemanticOnboardingAuditRecord, ...] = Field(max_length=50)
    external_writes_performed: Literal[False] = False


class SemanticOnboardingPreparationResponse(_StrictApiModel):
    draft: SemanticOnboardingDraft
    draft_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal: PreparedSemanticOnboardingProposal
    replayed: bool
    external_writes_performed: Literal[False] = False

    @classmethod
    def from_domain(cls, value: SemanticOnboardingPreparation) -> Self:
        return cls(
            draft=value.draft,
            draft_fingerprint=value.draft.fingerprint,
            proposal=value.proposal,
            replayed=value.replayed,
        )


class RegistryJoinChangeListQuery(_StrictApiModel):
    """Bounded first page of join changes visible to the authenticated principal."""

    limit: int = Field(default=50, ge=1, le=50)


class RegistryJoinChangeInspectionQuery(_StrictApiModel):
    """Bounded most-recent audit window for one exact join change."""

    history_limit: int = Field(default=25, ge=1, le=50)


class RegistryJoinProfileRequest(_StrictApiModel):
    """Client intent only; base, bindings and connector authority are reread server-side."""

    change_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,79}$")
    connection_id: CatalogConnectionId
    proposal: JoinProposal
    expected_base_registry: OnboardingRegistryBase
    expected_execution_target_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    def to_domain(self) -> RequestRegistryJoinProfileInput:
        return RequestRegistryJoinProfileInput(**self.model_dump(mode="python"))


class RegistryJoinDraftFinalizationRequest(_StrictApiModel):
    confirmed_authoring_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class RegistryJoinDecisionRequest(_StrictApiModel):
    action: Literal[DecisionAction.APPROVE, DecisionAction.REJECT]
    expected_revision: int = Field(ge=1)
    confirmed_draft_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    rationale: str = Field(min_length=12, max_length=2_000)

    @field_validator("rationale")
    @classmethod
    def rationale_must_be_meaningful(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) < 12:
            raise ValueError("registry join rationale must contain 12 meaningful characters")
        return stripped


class RegistryJoinPreparationRequest(_StrictApiModel):
    expected_revision: int = Field(ge=1)
    confirmed_draft_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class RegistryJoinChangeSummaryResponse(_StrictApiModel):
    """List projection without physical mapping, evidence, decision or audit payloads."""

    change_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,79}$")
    owner_actor_id: str = Field(min_length=1, max_length=200)
    scope: SemanticRegistryScope
    connection_id: CatalogConnectionId
    scan_id: str = Field(pattern=r"^scan_[0-9a-f]{64}$")
    base_registry_version: int = Field(ge=1)
    base_registry_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoring_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @classmethod
    def from_domain(cls, value: RegistryJoinProfileAuthoringRequest) -> Self:
        base = value.request.base_evidence.base_registry
        if base.registry_version is None or base.registry_fingerprint is None:
            raise ValueError("registry join change list item has no active base")
        return cls(
            change_id=value.id,
            owner_actor_id=value.owner_actor_id,
            scope=value.request.scope,
            connection_id=value.request.proposal.connection_id,
            scan_id=value.request.scan_id,
            base_registry_version=base.registry_version,
            base_registry_fingerprint=base.registry_fingerprint,
            authoring_fingerprint=value.fingerprint,
            created_at=value.created_at,
        )


class RegistryJoinChangeListResponse(_StrictApiModel):
    resource: Literal["registry_join_changes"] = "registry_join_changes"
    state: Literal["not_configured", "configured"]
    items: tuple[RegistryJoinChangeSummaryResponse, ...] = Field(max_length=50)

    @classmethod
    def from_domain(
        cls,
        values: tuple[RegistryJoinProfileAuthoringRequest, ...],
    ) -> Self:
        return cls(
            state="not_configured" if not values else "configured",
            items=tuple(RegistryJoinChangeSummaryResponse.from_domain(item) for item in values),
        )


class RegistryJoinProfileMutationResponse(_StrictApiModel):
    authoring: RegistryJoinProfileAuthoringRequest
    authoring_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_job_id: str | None = Field(
        default=None,
        pattern=r"^profile_job_[0-9a-f]{64}$",
    )
    profile_job_status: SemanticJoinProfileJobStatus | None = None
    replayed: bool
    external_writes_performed: Literal[False] = False

    @classmethod
    def from_domain(cls, value: RegistryJoinProfileAuthoringMutation) -> Self:
        return cls(
            authoring=value.authoring,
            authoring_fingerprint=value.authoring.fingerprint,
            profile_job_id=None if value.job is None else value.job.job_id,
            profile_job_status=None if value.job is None else value.job.status,
            replayed=value.replayed,
        )


class RegistryJoinDraftMutationResponse(_StrictApiModel):
    draft: RegistryJoinChangeDraft
    draft_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    replayed: bool
    external_writes_performed: Literal[False] = False

    @classmethod
    def from_domain(cls, value: RegistryJoinDraftMutation) -> Self:
        return cls(
            draft=value.draft,
            draft_fingerprint=value.draft.fingerprint,
            replayed=value.replayed,
        )


class RegistryJoinChangeResponse(_StrictApiModel):
    authoring: RegistryJoinProfileAuthoringRequest
    authoring_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    draft: RegistryJoinChangeDraft | None
    draft_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    audit_visible: bool
    history_truncated: bool
    audit: tuple[RegistryChangeAuditRecord, ...] = Field(max_length=50)
    external_writes_performed: Literal[False] = False

    @classmethod
    def from_domain(cls, value: RegistryJoinChangeSnapshot) -> Self:
        return cls(
            authoring=value.authoring,
            authoring_fingerprint=value.authoring.fingerprint,
            draft=value.draft,
            draft_fingerprint=None if value.draft is None else value.draft.fingerprint,
            audit_visible=value.audit_visible,
            history_truncated=value.history_truncated,
            audit=value.audit,
        )


class RegistryJoinPreparationResponse(_StrictApiModel):
    draft: RegistryJoinChangeDraft
    draft_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal: PreparedRegistryJoinProposal
    replayed: bool
    external_writes_performed: Literal[False] = False

    @classmethod
    def from_domain(cls, value: RegistryJoinPreparation) -> Self:
        return cls(
            draft=value.draft,
            draft_fingerprint=value.draft.fingerprint,
            proposal=value.proposal,
            replayed=value.replayed,
        )


class RegistryModelChangeListQuery(_StrictApiModel):
    limit: int = Field(default=50, ge=1, le=50)


class RegistryModelChangeInspectionQuery(_StrictApiModel):
    history_limit: int = Field(default=25, ge=1, le=50)


class RegistryModelProfileRequest(_StrictApiModel):
    """Candidate join-profile intent; all authority is reread by the server."""

    request_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,79}$")
    change_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,79}$")
    replacement_proposal_id: str = Field(min_length=3, max_length=200)
    expected_replacement_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_model_id: LogicalModelRef
    expected_base_registry: OnboardingRegistryBase
    join_id: str = Field(min_length=3, max_length=200)
    proposal: JoinProposal
    expected_execution_target_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    def to_domain(self) -> RequestRegistryModelJoinProfileInput:
        return RequestRegistryModelJoinProfileInput(**self.model_dump(mode="python"))


class RegistryModelProfileFinalizationRequest(_StrictApiModel):
    confirmed_authoring_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class RegistryModelChangeCreateRequest(_StrictApiModel):
    """Typed model replacement/remediation intent without trusted authority payloads."""

    change_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,79}$")
    replacement_proposal_id: str = Field(min_length=3, max_length=200)
    expected_replacement_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_model_id: LogicalModelRef
    expected_base_registry: OnboardingRegistryBase
    kind: RegistryModelChangeKind
    remediation_report_id: str | None = Field(default=None, min_length=3, max_length=200)
    expected_remediation_report_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    resolved_finding_ids: tuple[str, ...] = Field(default=(), max_length=2_000)
    incident_joins: tuple[RegistryIncidentJoinInput, ...] = Field(default=(), max_length=500)
    risks: tuple[str, ...] = Field(min_length=1, max_length=100)

    def to_domain(self) -> CreateRegistryModelChangeInput:
        return CreateRegistryModelChangeInput(**self.model_dump(mode="python"))


class RegistryModelDecisionRequest(_StrictApiModel):
    action: Literal[DecisionAction.APPROVE, DecisionAction.REJECT]
    expected_revision: int = Field(ge=1)
    confirmed_draft_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    rationale: str = Field(min_length=12, max_length=2_000)

    @field_validator("rationale")
    @classmethod
    def rationale_must_be_meaningful(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) < 12:
            raise ValueError("registry model rationale must contain 12 meaningful characters")
        return stripped


class RegistryModelPreparationRequest(_StrictApiModel):
    expected_revision: int = Field(ge=1)
    confirmed_draft_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class RegistryModelProfileMutationResponse(_StrictApiModel):
    authoring: RegistryModelJoinProfileAuthoringRequest
    authoring_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_job_id: str | None = Field(default=None, pattern=r"^profile_job_[0-9a-f]{64}$")
    profile_job_status: SemanticJoinProfileJobStatus | None = None
    witness: RegistryModelJoinProfileWitness | None = None
    replayed: bool
    external_writes_performed: Literal[False] = False

    @classmethod
    def from_domain(cls, value: RegistryModelJoinProfileMutation) -> Self:
        return cls(
            authoring=value.authoring,
            authoring_fingerprint=value.authoring.fingerprint,
            profile_job_id=None if value.job is None else value.job.job_id,
            profile_job_status=None if value.job is None else value.job.status,
            witness=value.witness,
            replayed=value.replayed,
        )


class RegistryModelChangeSummaryResponse(_StrictApiModel):
    """Bounded list projection without physical mappings, evidence, or audit payloads."""

    change_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,79}$")
    owner_actor_id: str = Field(min_length=1, max_length=200)
    scope: SemanticRegistryScope
    source_proposal_id: str = Field(min_length=3, max_length=200)
    source_proposal_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_model_id: LogicalModelRef
    kind: RegistryModelChangeKind
    base_registry_version: int = Field(ge=1)
    base_registry_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: RegistryModelChangeStatus
    revision: int = Field(ge=1)
    draft_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    updated_at: datetime

    @classmethod
    def from_domain(cls, value: RegistryModelChangeDraft) -> Self:
        base = value.base.base_registry
        if base.registry_version is None or base.registry_fingerprint is None:
            raise ValueError("registry model change list item has no active base")
        return cls(
            change_id=value.id,
            owner_actor_id=value.owner_actor_id,
            scope=value.base.scope,
            source_proposal_id=value.source.proposal.id,
            source_proposal_fingerprint=value.source.proposal.fingerprint,
            target_model_id=value.base.target_model.id,
            kind=value.authority.kind,
            base_registry_version=base.registry_version,
            base_registry_fingerprint=base.registry_fingerprint,
            status=value.status,
            revision=value.revision,
            draft_fingerprint=value.fingerprint,
            updated_at=value.updated_at,
        )


class RegistryModelChangeListResponse(_StrictApiModel):
    resource: Literal["registry_model_changes"] = "registry_model_changes"
    state: Literal["not_configured", "configured"]
    items: tuple[RegistryModelChangeSummaryResponse, ...] = Field(max_length=50)

    @classmethod
    def from_domain(cls, values: tuple[RegistryModelChangeDraft, ...]) -> Self:
        return cls(
            state="not_configured" if not values else "configured",
            items=tuple(RegistryModelChangeSummaryResponse.from_domain(item) for item in values),
        )


class RegistryModelChangeMutationResponse(_StrictApiModel):
    draft: RegistryModelChangeDraft
    draft_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal: PreparedRegistryModelReplacementProposal | None = None
    replayed: bool
    external_writes_performed: Literal[False] = False

    @classmethod
    def from_domain(cls, value: RegistryModelChangeMutation) -> Self:
        return cls(
            draft=value.draft,
            draft_fingerprint=value.draft.fingerprint,
            proposal=value.proposal,
            replayed=value.replayed,
        )


class RegistryModelChangeResponse(_StrictApiModel):
    draft: RegistryModelChangeDraft
    draft_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    audit_visible: bool
    history_truncated: bool
    audit: tuple[RegistryModelChangeAuditRecord, ...] = Field(max_length=50)
    external_writes_performed: Literal[False] = False

    @classmethod
    def from_domain(cls, value: RegistryModelChangeSnapshot) -> Self:
        return cls(
            draft=value.draft,
            draft_fingerprint=value.draft.fingerprint,
            audit_visible=value.audit_visible,
            history_truncated=value.history_truncated,
            audit=value.audit,
        )


class RegistryModelPreparationResponse(_StrictApiModel):
    draft: RegistryModelChangeDraft
    draft_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal: PreparedRegistryModelReplacementProposal
    replayed: bool
    external_writes_performed: Literal[False] = False

    @classmethod
    def from_domain(cls, value: RegistryModelChangeMutation) -> Self:
        if value.proposal is None:
            raise ValueError("registry model preparation has no immutable proposal")
        return cls(
            draft=value.draft,
            draft_fingerprint=value.draft.fingerprint,
            proposal=value.proposal,
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
