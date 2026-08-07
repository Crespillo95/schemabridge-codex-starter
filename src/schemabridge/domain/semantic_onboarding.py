"""Pure contracts for tenant-bound governed semantic onboarding.

The aggregate deliberately stops at a non-executable publication proposal.  Physical catalog
observations are authority-bearing identities, while confidence and similarity remain advisory.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.catalog_inventory import (
    CatalogAssetId,
    CatalogConnectionId,
    CatalogFieldLocator,
)
from schemabridge.domain.concepts import CanonicalType, LogicalFieldRef, LogicalModelRef
from schemabridge.domain.decisions import ApprovalStatus, DecisionAction
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.mappings import ConfidenceScore
from schemabridge.domain.request_context import LogicalFieldRole
from schemabridge.domain.semantic_registry import PhysicalValueType, SemanticRegistryScope
from schemabridge.domain.transformations import TransformationPlan

MAX_ONBOARDING_FIELDS = 100
MAX_ONBOARDING_MAPPINGS = 2_000
MAX_ONBOARDING_EVIDENCE = 16
MAX_ONBOARDING_RISKS = 16
MAX_ONBOARDING_ALLOWED_VALUES = 64
MAX_ONBOARDING_DECISIONS = 2_001
MAX_ONBOARDING_DRAFT_BYTES = 2 * 1024 * 1024
MAX_ONBOARDING_PROPOSAL_BYTES = 2 * 1024 * 1024
MAX_ONBOARDING_DECISION_BYTES = 64 * 1024

_INERT_ID = re.compile(r"^[a-z][a-z0-9_-]{2,79}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_SQL_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_DATAHUB_DATASET_URN_PREFIX = "urn:li:dataset:"


class SemanticOnboardingStatus(StrEnum):
    """Closed lifecycle for a non-executable onboarding draft."""

    NEEDS_REVIEW = "needs_review"
    READY_FOR_PUBLICATION = "ready_for_publication"
    SUPERSEDED = "superseded"


class OnboardingEvidenceKind(StrEnum):
    """Bounded evidence categories; names alone never authorize approval."""

    NAME_SIMILARITY = "name_similarity"
    TYPE_COMPATIBILITY = "type_compatibility"
    CATALOG_DEFINITION = "catalog_definition"
    GLOSSARY_TERM = "glossary_term"
    DECLARED_KEY = "declared_key"
    LINEAGE = "lineage"
    PROFILED_CARDINALITY = "profiled_cardinality"
    HUMAN_ATTESTATION = "human_attestation"


class SemanticOnboardingTargetKind(StrEnum):
    MODEL = "model"
    MAPPING = "mapping"


class SemanticOnboardingPermission(StrEnum):
    VIEW = "onboarding:view"
    CREATE = "onboarding:create"
    DECIDE = "onboarding:decide"
    PREPARE_PUBLICATION = "onboarding:prepare_publication"
    AUDIT_VIEW = "onboarding:audit_view"


class SemanticOnboardingAuditEvent(StrEnum):
    DRAFT_CREATED = "draft_created"
    DECISION_RECORDED = "decision_recorded"
    PUBLICATION_PREPARED = "publication_prepared"


class OnboardingEvidence(FrozenDomainModel):
    kind: OnboardingEvidenceKind
    detail: str = Field(min_length=3, max_length=500)
    reference: str | None = Field(default=None, min_length=3, max_length=500)

    @field_validator("detail", "reference")
    @classmethod
    def text_must_be_meaningful(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if len(stripped) < 3 or len(stripped.encode("utf-8")) > 2_000:
            raise ValueError("onboarding evidence text is invalid")
        return stripped


class SemanticFieldDefinition(FrozenDomainModel):
    id: LogicalFieldRef
    canonical_type: CanonicalType
    role: LogicalFieldRole
    definition: str = Field(min_length=3, max_length=4_000)
    allowed_values: tuple[str, ...] = Field(
        default=(),
        max_length=MAX_ONBOARDING_ALLOWED_VALUES,
    )

    @field_validator("definition")
    @classmethod
    def definition_must_be_bounded(cls, value: str) -> str:
        if not value.strip() or len(value.encode("utf-8")) > 16_000:
            raise ValueError("semantic field definition is invalid")
        return value

    @field_validator("allowed_values")
    @classmethod
    def allowed_values_must_be_bounded(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("semantic field allowed values must be unique")
        if any(
            not value.strip() or len(value) > 200 or len(value.encode("utf-8")) > 800
            for value in values
        ):
            raise ValueError("semantic field allowed values are invalid")
        return values

    @model_validator(mode="after")
    def role_and_type_must_be_compatible(self) -> SemanticFieldDefinition:
        if self.role is LogicalFieldRole.TEMPORAL and self.canonical_type not in {
            CanonicalType.DATE,
            CanonicalType.TIMESTAMP,
        }:
            raise ValueError("temporal fields require date or timestamp type")
        if self.role is LogicalFieldRole.MEASURE and self.canonical_type not in {
            CanonicalType.INTEGER,
            CanonicalType.DECIMAL,
        }:
            raise ValueError("measure fields require integer or decimal type")
        if self.allowed_values and self.canonical_type is not CanonicalType.STRING:
            raise ValueError("allowed values require a string field")
        return self


class SemanticModelDefinition(FrozenDomainModel):
    id: LogicalModelRef
    description: str = Field(min_length=3, max_length=4_000)
    fields: tuple[SemanticFieldDefinition, ...] = Field(
        min_length=1,
        max_length=MAX_ONBOARDING_FIELDS,
    )
    version: int = Field(default=1, ge=1)

    @field_validator("description")
    @classmethod
    def description_must_be_bounded(cls, value: str) -> str:
        if not value.strip() or len(value.encode("utf-8")) > 16_000:
            raise ValueError("semantic model description is invalid")
        return value

    @model_validator(mode="after")
    def fields_must_belong_to_model(self) -> SemanticModelDefinition:
        prefix = f"{self.id.root}."
        field_ids = tuple(field.id.root for field in self.fields)
        if len(field_ids) != len(set(field_ids)):
            raise ValueError("semantic model fields must be unique")
        if any(not field_id.startswith(prefix) for field_id in field_ids):
            raise ValueError("semantic model fields must belong to the model")
        return self


class SemanticModelProposal(FrozenDomainModel):
    definition: SemanticModelDefinition
    status: ApprovalStatus = ApprovalStatus.NEEDS_REVIEW
    decision_id: str | None = Field(default=None, min_length=3, max_length=200)
    decided_by: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def decision_must_match_status(self) -> SemanticModelProposal:
        _validate_review_status(self.status, self.decision_id, self.decided_by, "model")
        return self


class OnboardingRegistryBase(FrozenDomainModel):
    """Exact active-registry state observed when the draft was created."""

    registry_version: int | None = Field(default=None, ge=1)
    registry_fingerprint: str | None = None
    activation_generation: int | None = Field(default=None, ge=1)
    active_pointer_fingerprint: str | None = None

    @field_validator("registry_fingerprint", "active_pointer_fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str | None) -> str | None:
        if value is not None and _SHA256.fullmatch(value) is None:
            raise ValueError("registry base fingerprint must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def base_must_be_empty_or_complete(self) -> OnboardingRegistryBase:
        values = (
            self.registry_version,
            self.registry_fingerprint,
            self.activation_generation,
            self.active_pointer_fingerprint,
        )
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            raise ValueError("registry base must be empty or complete")
        return self

    @property
    def next_registry_version(self) -> int:
        return 1 if self.registry_version is None else self.registry_version + 1


class OnboardingCatalogGeneration(FrozenDomainModel):
    """Exact public control-plane state required before reading catalog observations."""

    workspace_id: str = Field(min_length=3, max_length=200)
    connection_id: CatalogConnectionId
    catalog_scope: str = Field(min_length=3, max_length=120)
    generation: int = Field(ge=1)
    inventory_fingerprint: str
    enabled: bool
    stale: bool

    @field_validator("inventory_fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("onboarding catalog generation fingerprint is invalid")
        return value

    @field_validator("catalog_scope")
    @classmethod
    def scope_must_be_inert(cls, value: str) -> str:
        if re.fullmatch(r"^[a-z][a-z0-9_.:-]{2,119}$", value) is None:
            raise ValueError("onboarding catalog generation scope must be inert")
        return value


class PhysicalCatalogObservation(FrozenDomainModel):
    """Exact retained catalog facts behind one proposed physical mapping."""

    locator: CatalogFieldLocator
    catalog_scope: str = Field(min_length=3, max_length=120)
    generation: int = Field(ge=1)
    generation_fingerprint: str
    asset_metadata_fingerprint: str
    field_metadata_fingerprint: str
    physical_field: PhysicalFieldRef
    physical_type: PhysicalValueType
    observed_datahub_asset_urn: str | None = Field(
        default=None,
        min_length=3,
        max_length=500,
    )

    @field_validator(
        "generation_fingerprint",
        "asset_metadata_fingerprint",
        "field_metadata_fingerprint",
    )
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("catalog observation fingerprint must be lowercase SHA-256")
        return value

    @field_validator("catalog_scope")
    @classmethod
    def catalog_scope_must_be_inert(cls, value: str) -> str:
        if re.fullmatch(r"^[a-z][a-z0-9_.:-]{2,119}$", value) is None:
            raise ValueError("catalog observation scope must be inert")
        return value

    @field_validator("physical_field")
    @classmethod
    def physical_field_must_be_compiler_safe(cls, value: PhysicalFieldRef) -> PhysicalFieldRef:
        segments = value.root.split(".")
        if len(segments) != 3 or any(
            _SAFE_SQL_IDENTIFIER.fullmatch(segment) is None for segment in segments
        ):
            raise ValueError(
                "catalog physical field must be exactly schema.table.column with lowercase "
                "safe SQL identifiers"
            )
        return value

    @model_validator(mode="after")
    def datahub_identity_must_be_observed_exactly(self) -> PhysicalCatalogObservation:
        if len(self.locator.field_path) != 1:
            raise ValueError("catalog observation requires exactly one field path segment")
        if self.physical_field.root.rsplit(".", maxsplit=1)[-1] != self.locator.field_path[0]:
            raise ValueError("catalog physical field must match the observed field path")
        if self.physical_type is PhysicalValueType.UNKNOWN:
            raise ValueError("unknown physical types cannot enter semantic onboarding")
        urn = self.observed_datahub_asset_urn
        if urn is not None and (
            not urn.startswith(_DATAHUB_DATASET_URN_PREFIX)
            or urn != self.locator.asset.asset_id.root
        ):
            raise ValueError("DataHub asset URN must equal the observed catalog asset identity")
        return self

    @property
    def authority_identity(self) -> tuple[str, str, int, str]:
        asset = self.locator.asset
        return (
            asset.workspace_id,
            asset.connection_id.root,
            self.generation,
            self.physical_field.root,
        )


class ResolvedOnboardingCatalogEvidence(FrozenDomainModel):
    """One complete exact server-side resolution; partial evidence has no representation."""

    generation: OnboardingCatalogGeneration
    observations: tuple[PhysicalCatalogObservation, ...] = Field(
        min_length=1,
        max_length=MAX_ONBOARDING_MAPPINGS,
    )

    @model_validator(mode="after")
    def observations_must_match_generation(
        self,
    ) -> ResolvedOnboardingCatalogEvidence:
        identities = tuple(item.authority_identity for item in self.observations)
        if len(identities) != len(set(identities)):
            raise ValueError("resolved onboarding observations must be unique")
        expected = self.generation
        for item in self.observations:
            asset = item.locator.asset
            if (
                asset.workspace_id != expected.workspace_id
                or asset.connection_id != expected.connection_id
                or item.catalog_scope != expected.catalog_scope
                or item.generation != expected.generation
                or item.generation_fingerprint != expected.inventory_fingerprint
            ):
                raise ValueError("resolved onboarding observation does not match its generation")
        return self


class SemanticOnboardingPreflightSelection(FrozenDomainModel):
    """One client-selected catalog locator with no physical authority supplied by the client."""

    asset_id: CatalogAssetId
    field_path: tuple[str, ...] = Field(min_length=1, max_length=1)

    @field_validator("field_path")
    @classmethod
    def field_path_must_be_safe_postgres_identifiers(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(
            re.fullmatch(r"^[a-z_][a-z0-9_]{0,62}$", value) is None
            or len(value.encode("utf-8")) > 63
            for value in values
        ):
            raise ValueError("semantic onboarding preflight field path is not compiler-safe")
        return values


class PreflightSemanticOnboardingRequest(FrozenDomainModel):
    """Read-only request for server-derived catalog and registry authority facts."""

    connection_id: CatalogConnectionId
    selections: tuple[SemanticOnboardingPreflightSelection, ...] = Field(
        min_length=1,
        max_length=MAX_ONBOARDING_MAPPINGS,
    )

    @field_validator("selections")
    @classmethod
    def selections_must_be_unique(
        cls,
        values: tuple[SemanticOnboardingPreflightSelection, ...],
    ) -> tuple[SemanticOnboardingPreflightSelection, ...]:
        identities = tuple((item.asset_id.root, item.field_path) for item in values)
        if len(identities) != len(set(identities)):
            raise ValueError("semantic onboarding preflight selections must be unique")
        return values

    @model_validator(mode="after")
    def request_must_be_byte_bounded(self) -> PreflightSemanticOnboardingRequest:
        if len(_canonical_json(self.model_dump(mode="json"))) > MAX_ONBOARDING_DRAFT_BYTES:
            raise ValueError("semantic onboarding preflight request exceeds the byte limit")
        return self


class SemanticOnboardingPreflight(FrozenDomainModel):
    """Exact tenant-bound authority context a client must confirm before draft creation."""

    scope: SemanticRegistryScope
    connection_id: CatalogConnectionId
    catalog_generation: int = Field(ge=1)
    catalog_generation_fingerprint: str
    base_registry: OnboardingRegistryBase
    observations: tuple[PhysicalCatalogObservation, ...] = Field(
        min_length=1,
        max_length=MAX_ONBOARDING_MAPPINGS,
    )
    external_writes_performed: Literal[False] = False
    fingerprint: str

    @field_validator("catalog_generation_fingerprint", "fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("semantic onboarding preflight fingerprint is invalid")
        return value

    @model_validator(mode="after")
    def authority_context_must_be_exact(self) -> SemanticOnboardingPreflight:
        identities = tuple(item.authority_identity for item in self.observations)
        if len(identities) != len(set(identities)):
            raise ValueError("semantic onboarding preflight observations must be unique")
        for observation in self.observations:
            asset = observation.locator.asset
            if (
                asset.workspace_id != self.scope.workspace_id
                or asset.connection_id != self.connection_id
                or observation.catalog_scope != self.scope.catalog_scope
                or observation.generation != self.catalog_generation
                or observation.generation_fingerprint != self.catalog_generation_fingerprint
            ):
                raise ValueError("semantic onboarding preflight observation is out of scope")
        expected = semantic_onboarding_fingerprint(
            {
                "contract": "semantic_onboarding_preflight_v1",
                "payload": self.model_dump(mode="json", exclude={"fingerprint"}),
            }
        )
        if self.fingerprint != expected:
            raise ValueError("semantic onboarding preflight fingerprint does not match")
        if len(_canonical_json(self.model_dump(mode="json"))) > MAX_ONBOARDING_DRAFT_BYTES:
            raise ValueError("semantic onboarding preflight exceeds the byte limit")
        return self

    @classmethod
    def create(
        cls,
        *,
        scope: SemanticRegistryScope,
        evidence: ResolvedOnboardingCatalogEvidence,
        base_registry: OnboardingRegistryBase,
    ) -> SemanticOnboardingPreflight:
        generation = evidence.generation
        payload = {
            "scope": scope,
            "connection_id": generation.connection_id,
            "catalog_generation": generation.generation,
            "catalog_generation_fingerprint": generation.inventory_fingerprint,
            "base_registry": base_registry,
            "observations": evidence.observations,
            "external_writes_performed": False,
        }
        fingerprint = semantic_onboarding_fingerprint(
            {
                "contract": "semantic_onboarding_preflight_v1",
                "payload": _jsonable(payload),
            }
        )
        return cls(**payload, fingerprint=fingerprint)


class SemanticMappingProposal(FrozenDomainModel):
    id: str = Field(min_length=3, max_length=80)
    logical_field: LogicalFieldRef
    observation: PhysicalCatalogObservation
    confidence: ConfidenceScore
    evidence: tuple[OnboardingEvidence, ...] = Field(
        min_length=1,
        max_length=MAX_ONBOARDING_EVIDENCE,
    )
    risks: tuple[str, ...] = Field(default=(), max_length=MAX_ONBOARDING_RISKS)
    transformation_plan: TransformationPlan
    status: ApprovalStatus = ApprovalStatus.NEEDS_REVIEW
    decision_id: str | None = Field(default=None, min_length=3, max_length=200)
    decided_by: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("id")
    @classmethod
    def id_must_be_inert(cls, value: str) -> str:
        if _INERT_ID.fullmatch(value) is None:
            raise ValueError("semantic mapping id must be inert")
        return value

    @field_validator("risks")
    @classmethod
    def risks_must_be_bounded(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not value.strip() or len(value) > 500 or len(value.encode("utf-8")) > 2_000
            for value in values
        ):
            raise ValueError("semantic mapping risks are invalid")
        return values

    @model_validator(mode="after")
    def decision_must_match_status(self) -> SemanticMappingProposal:
        _validate_review_status(self.status, self.decision_id, self.decided_by, "mapping")
        return self

    @property
    def has_non_name_evidence(self) -> bool:
        return any(
            item.kind is not OnboardingEvidenceKind.NAME_SIMILARITY for item in self.evidence
        )


class SemanticOnboardingDraft(FrozenDomainModel):
    """Tenant-bound aggregate; it has no executable or external-write representation."""

    id: str = Field(min_length=3, max_length=80)
    workspace_id: str = Field(min_length=3, max_length=200)
    owner_actor_id: str = Field(min_length=1, max_length=200)
    scope: SemanticRegistryScope
    connection_id: CatalogConnectionId
    catalog_generation: int = Field(ge=1)
    catalog_generation_fingerprint: str
    base_registry: OnboardingRegistryBase
    revision: int = Field(default=1, ge=1)
    status: SemanticOnboardingStatus = SemanticOnboardingStatus.NEEDS_REVIEW
    model: SemanticModelProposal
    mappings: tuple[SemanticMappingProposal, ...] = Field(
        min_length=1,
        max_length=MAX_ONBOARDING_MAPPINGS,
    )
    prepared_proposal_id: str | None = Field(default=None, min_length=3, max_length=200)
    prepared_proposal_fingerprint: str | None = None
    prepared_by: str | None = Field(default=None, min_length=1, max_length=200)
    created_at: datetime
    updated_at: datetime

    @field_validator("id")
    @classmethod
    def id_must_be_inert(cls, value: str) -> str:
        if _INERT_ID.fullmatch(value) is None:
            raise ValueError("semantic onboarding draft id must be inert")
        return value

    @field_validator("workspace_id", "owner_actor_id", "prepared_by")
    @classmethod
    def identifiers_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("semantic onboarding identifiers must not be blank")
        return value

    @field_validator("catalog_generation_fingerprint", "prepared_proposal_fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str | None) -> str | None:
        if value is not None and _SHA256.fullmatch(value) is None:
            raise ValueError("semantic onboarding fingerprint must be lowercase SHA-256")
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("semantic onboarding timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def aggregate_must_be_complete_and_scoped(self) -> SemanticOnboardingDraft:
        if self.updated_at < self.created_at:
            raise ValueError("semantic onboarding update precedes creation")
        if self.scope.workspace_id != self.workspace_id:
            raise ValueError("semantic onboarding scope belongs to another workspace")
        field_ids = {field.id.root for field in self.model.definition.fields}
        mapping_ids = tuple(mapping.id for mapping in self.mappings)
        if len(mapping_ids) != len(set(mapping_ids)):
            raise ValueError("semantic onboarding mapping ids must be unique")
        if any(mapping.logical_field.root not in field_ids for mapping in self.mappings):
            raise ValueError("semantic onboarding mapping targets an unknown field")
        mapped_fields = {mapping.logical_field.root for mapping in self.mappings}
        if mapped_fields != field_ids:
            raise ValueError("every semantic field requires at least one mapping proposal")
        for mapping in self.mappings:
            observation = mapping.observation
            asset = observation.locator.asset
            if (
                asset.workspace_id != self.workspace_id
                or asset.connection_id != self.connection_id
                or observation.catalog_scope != self.scope.catalog_scope
                or observation.generation != self.catalog_generation
                or observation.generation_fingerprint != self.catalog_generation_fingerprint
            ):
                raise ValueError("semantic onboarding observation is outside the draft scope")
        approved_meanings: dict[tuple[str, str, int, str], str] = {}
        for mapping in self.mappings:
            if mapping.status is not ApprovalStatus.APPROVED:
                continue
            identity = mapping.observation.authority_identity
            prior = approved_meanings.setdefault(identity, mapping.logical_field.root)
            if prior != mapping.logical_field.root:
                raise ValueError("one physical observation cannot have two approved meanings")
        prepared_values = (
            self.prepared_proposal_id,
            self.prepared_proposal_fingerprint,
            self.prepared_by,
        )
        if self.status in {
            SemanticOnboardingStatus.READY_FOR_PUBLICATION,
            SemanticOnboardingStatus.SUPERSEDED,
        }:
            if not all(value is not None for value in prepared_values):
                raise ValueError("prepared proposal binding must be complete")
            if not self.ready_for_preparation():
                raise ValueError("prepared onboarding draft has an incomplete decision closure")
        elif any(value is not None for value in prepared_values):
            raise ValueError("unprepared onboarding draft cannot cite a proposal")
        if len(_canonical_json(self.model_dump(mode="json"))) > MAX_ONBOARDING_DRAFT_BYTES:
            raise ValueError("semantic onboarding draft exceeds the byte limit")
        return self

    def ready_for_preparation(self) -> bool:
        if self.model.status is not ApprovalStatus.APPROVED:
            return False
        if any(
            mapping.status not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}
            for mapping in self.mappings
        ):
            return False
        approved_fields = {
            mapping.logical_field.root
            for mapping in self.mappings
            if mapping.status is ApprovalStatus.APPROVED
        }
        return approved_fields == {field.id.root for field in self.model.definition.fields}

    @property
    def fingerprint(self) -> str:
        return semantic_onboarding_fingerprint(self.model_dump(mode="json"))


class SemanticOnboardingDecision(FrozenDomainModel):
    id: str = Field(min_length=3, max_length=200)
    workspace_id: str = Field(min_length=3, max_length=200)
    draft_id: str = Field(min_length=3, max_length=80)
    target_kind: SemanticOnboardingTargetKind
    target_id: str = Field(min_length=3, max_length=200)
    action: DecisionAction
    status: ApprovalStatus
    actor_id: str = Field(min_length=1, max_length=200)
    decided_at: datetime
    source_revision: int = Field(ge=1)
    resulting_revision: int = Field(ge=2)
    rationale: str = Field(min_length=12, max_length=2_000)
    evidence: tuple[OnboardingEvidence, ...] = Field(
        default=(),
        max_length=MAX_ONBOARDING_EVIDENCE,
    )
    idempotency_digest: str
    request_fingerprint: str

    @field_validator("workspace_id", "actor_id")
    @classmethod
    def text_must_be_meaningful(cls, value: str) -> str:
        if not value.strip() or len(value.encode("utf-8")) > 8_000:
            raise ValueError("semantic onboarding decision text is invalid")
        return value

    @field_validator("rationale")
    @classmethod
    def rationale_must_be_meaningful(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) < 12 or len(stripped.encode("utf-8")) > 8_000:
            raise ValueError("semantic onboarding rationale must contain 12 meaningful characters")
        return stripped

    @field_validator("idempotency_digest", "request_fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("semantic onboarding decision fingerprint is invalid")
        return value

    @field_validator("decided_at")
    @classmethod
    def decision_time_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("semantic onboarding decision time must include a timezone")
        return value

    @model_validator(mode="after")
    def decision_must_be_consistent(self) -> SemanticOnboardingDecision:
        if self.action not in {DecisionAction.APPROVE, DecisionAction.REJECT}:
            raise ValueError("semantic onboarding decisions approve or reject only")
        expected = (
            ApprovalStatus.APPROVED
            if self.action is DecisionAction.APPROVE
            else ApprovalStatus.REJECTED
        )
        if self.status is not expected or self.resulting_revision != self.source_revision + 1:
            raise ValueError("semantic onboarding decision transition is inconsistent")
        if self.action is DecisionAction.APPROVE and not any(
            item.kind is not OnboardingEvidenceKind.NAME_SIMILARITY for item in self.evidence
        ):
            raise ValueError("approval requires evidence beyond name similarity")
        if len(_canonical_json(self.model_dump(mode="json"))) > MAX_ONBOARDING_DECISION_BYTES:
            raise ValueError("semantic onboarding decision exceeds the byte limit")
        return self


def apply_semantic_onboarding_decision(
    draft: SemanticOnboardingDraft,
    decision: SemanticOnboardingDecision,
) -> SemanticOnboardingDraft:
    """Deterministically apply one immutable decision for durable replay reconstruction."""

    if (
        draft.status is not SemanticOnboardingStatus.NEEDS_REVIEW
        or decision.workspace_id != draft.workspace_id
        or decision.draft_id != draft.id
        or decision.source_revision != draft.revision
        or decision.resulting_revision != draft.revision + 1
    ):
        raise ValueError("semantic onboarding decision does not continue the draft")
    if decision.target_kind is SemanticOnboardingTargetKind.MODEL:
        if (
            decision.target_id != draft.model.definition.id.root
            or draft.model.status is not ApprovalStatus.NEEDS_REVIEW
        ):
            raise ValueError("semantic onboarding model decision target is invalid")
        model = SemanticModelProposal(
            definition=draft.model.definition,
            status=decision.status,
            decision_id=decision.id,
            decided_by=decision.actor_id,
        )
        mappings = draft.mappings
    else:
        target = next(
            (mapping for mapping in draft.mappings if mapping.id == decision.target_id),
            None,
        )
        if target is None or target.status is not ApprovalStatus.NEEDS_REVIEW:
            raise ValueError("semantic onboarding mapping decision target is invalid")
        model = draft.model
        mappings = tuple(
            SemanticMappingProposal(
                **{
                    **mapping.model_dump(mode="python"),
                    "status": decision.status,
                    "decision_id": decision.id,
                    "decided_by": decision.actor_id,
                }
            )
            if mapping.id == decision.target_id
            else mapping
            for mapping in draft.mappings
        )
    return SemanticOnboardingDraft.model_validate(
        {
            **draft.model_dump(mode="python"),
            "revision": decision.resulting_revision,
            "model": model,
            "mappings": mappings,
            "updated_at": decision.decided_at,
        }
    )


class PreparedSemanticOnboardingProposal(FrozenDomainModel):
    """Immutable, non-executable handoff for a future dedicated publication worker."""

    id: str = Field(min_length=3, max_length=200)
    workspace_id: str = Field(min_length=3, max_length=200)
    scope: SemanticRegistryScope
    draft_id: str = Field(min_length=3, max_length=80)
    draft_revision: int = Field(ge=1)
    draft_fingerprint: str
    target_registry_version: int = Field(ge=1)
    base_registry: OnboardingRegistryBase
    model: SemanticModelProposal
    mappings: tuple[SemanticMappingProposal, ...] = Field(
        min_length=1,
        max_length=MAX_ONBOARDING_MAPPINGS,
    )
    decision_ids: tuple[str, ...] = Field(
        min_length=2,
        max_length=MAX_ONBOARDING_DECISIONS,
    )
    prepared_by: str = Field(min_length=1, max_length=200)
    prepared_at: datetime
    external_writes_performed: Literal[False] = False
    fingerprint: str

    @field_validator("draft_fingerprint", "fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("prepared onboarding fingerprint must be lowercase SHA-256")
        return value

    @field_validator("decision_ids")
    @classmethod
    def decision_ids_must_be_sorted_unique(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))) or any(not value.strip() for value in values):
            raise ValueError("prepared onboarding decisions must be sorted and unique")
        return values

    @field_validator("prepared_at")
    @classmethod
    def prepared_time_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("prepared onboarding time must include a timezone")
        return value

    @model_validator(mode="after")
    def proposal_must_be_complete_and_fingerprinted(
        self,
    ) -> PreparedSemanticOnboardingProposal:
        if self.scope.workspace_id != self.workspace_id:
            raise ValueError("prepared onboarding proposal belongs to another workspace")
        if self.target_registry_version != self.base_registry.next_registry_version:
            raise ValueError("prepared onboarding target version is not the exact successor")
        if self.model.status is not ApprovalStatus.APPROVED:
            raise ValueError("prepared onboarding model must be approved")
        if any(mapping.status is not ApprovalStatus.APPROVED for mapping in self.mappings):
            raise ValueError("prepared onboarding contains an unapproved mapping")
        current_ids = {
            self.model.decision_id,
            *(mapping.decision_id for mapping in self.mappings),
        }
        if None in current_ids or not current_ids.issubset(set(self.decision_ids)):
            raise ValueError("prepared onboarding decision closure is incomplete")
        expected = semantic_onboarding_fingerprint(
            self.model_dump(mode="json", exclude={"fingerprint"})
        )
        if self.fingerprint != expected:
            raise ValueError("prepared onboarding proposal fingerprint does not match")
        if len(_canonical_json(self.model_dump(mode="json"))) > MAX_ONBOARDING_PROPOSAL_BYTES:
            raise ValueError("prepared onboarding proposal exceeds the byte limit")
        return self

    @classmethod
    def create(
        cls,
        *,
        id: str,
        draft: SemanticOnboardingDraft,
        decision_ids: tuple[str, ...],
        prepared_by: str,
        prepared_at: datetime,
    ) -> PreparedSemanticOnboardingProposal:
        approved_mappings = tuple(
            mapping for mapping in draft.mappings if mapping.status is ApprovalStatus.APPROVED
        )
        payload = {
            "id": id,
            "workspace_id": draft.workspace_id,
            "scope": draft.scope,
            "draft_id": draft.id,
            "draft_revision": draft.revision,
            "draft_fingerprint": draft.fingerprint,
            "target_registry_version": draft.base_registry.next_registry_version,
            "base_registry": draft.base_registry,
            "model": draft.model,
            "mappings": approved_mappings,
            "decision_ids": tuple(sorted(set(decision_ids))),
            "prepared_by": prepared_by,
            "prepared_at": prepared_at,
            "external_writes_performed": False,
        }
        provisional = cls.model_construct(
            id=id,
            workspace_id=draft.workspace_id,
            scope=draft.scope,
            draft_id=draft.id,
            draft_revision=draft.revision,
            draft_fingerprint=draft.fingerprint,
            target_registry_version=draft.base_registry.next_registry_version,
            base_registry=draft.base_registry,
            model=draft.model,
            mappings=approved_mappings,
            decision_ids=tuple(sorted(set(decision_ids))),
            prepared_by=prepared_by,
            prepared_at=prepared_at,
            external_writes_performed=False,
            fingerprint="0" * 64,
        )
        fingerprint = semantic_onboarding_fingerprint(
            provisional.model_dump(mode="json", exclude={"fingerprint"}),
        )
        return cls(**payload, fingerprint=fingerprint)


class SemanticOnboardingAuditRecord(FrozenDomainModel):
    id: str = Field(min_length=3, max_length=200)
    workspace_id: str = Field(min_length=3, max_length=200)
    draft_id: str = Field(min_length=3, max_length=80)
    event: SemanticOnboardingAuditEvent
    actor_id: str = Field(min_length=1, max_length=200)
    occurred_at: datetime
    source_revision: int = Field(ge=0)
    resulting_revision: int = Field(ge=1)
    previous_fingerprint: str | None = None
    resulting_fingerprint: str
    decision_id: str | None = Field(default=None, min_length=3, max_length=200)
    proposal_id: str | None = Field(default=None, min_length=3, max_length=200)
    external_writes_performed: Literal[False] = False

    @field_validator("previous_fingerprint", "resulting_fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str | None) -> str | None:
        if value is not None and _SHA256.fullmatch(value) is None:
            raise ValueError("semantic onboarding audit fingerprint is invalid")
        return value

    @field_validator("occurred_at")
    @classmethod
    def event_time_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("semantic onboarding audit time must include a timezone")
        return value

    @model_validator(mode="after")
    def event_binding_must_be_consistent(self) -> SemanticOnboardingAuditRecord:
        if self.resulting_revision != self.source_revision + 1:
            raise ValueError("semantic onboarding audit revision is inconsistent")
        if self.event is SemanticOnboardingAuditEvent.DRAFT_CREATED:
            if (
                self.source_revision != 0
                or self.decision_id is not None
                or self.proposal_id is not None
            ):
                raise ValueError("draft creation audit binding is invalid")
        elif self.event is SemanticOnboardingAuditEvent.DECISION_RECORDED:
            if self.decision_id is None or self.proposal_id is not None:
                raise ValueError("decision audit binding is invalid")
        elif self.proposal_id is None or self.decision_id is not None:
            raise ValueError("preparation audit binding is invalid")
        return self


class SemanticOnboardingDraftMutation(FrozenDomainModel):
    draft: SemanticOnboardingDraft
    replayed: bool = False


class SemanticOnboardingPreparation(FrozenDomainModel):
    draft: SemanticOnboardingDraft
    proposal: PreparedSemanticOnboardingProposal
    replayed: bool = False

    @model_validator(mode="after")
    def prepared_draft_must_reference_proposal(self) -> SemanticOnboardingPreparation:
        if (
            self.draft.status is not SemanticOnboardingStatus.READY_FOR_PUBLICATION
            or self.draft.prepared_proposal_id != self.proposal.id
            or self.draft.prepared_proposal_fingerprint != self.proposal.fingerprint
            or self.draft.prepared_by != self.proposal.prepared_by
        ):
            raise ValueError("prepared onboarding result binding is invalid")
        return self


class SemanticOnboardingMappingInput(FrozenDomainModel):
    """Client-safe mapping selection without actor or workspace authority fields."""

    id: str = Field(min_length=3, max_length=80)
    logical_field: LogicalFieldRef
    asset_id: CatalogAssetId
    field_path: tuple[str, ...] = Field(min_length=1, max_length=1)
    expected_asset_metadata_fingerprint: str
    expected_field_metadata_fingerprint: str
    physical_field: PhysicalFieldRef
    confidence: ConfidenceScore
    evidence: tuple[OnboardingEvidence, ...] = Field(
        min_length=1,
        max_length=MAX_ONBOARDING_EVIDENCE,
    )
    risks: tuple[str, ...] = Field(default=(), max_length=MAX_ONBOARDING_RISKS)
    transformation_plan: TransformationPlan

    @field_validator("id")
    @classmethod
    def id_must_be_inert(cls, value: str) -> str:
        if _INERT_ID.fullmatch(value) is None:
            raise ValueError("semantic mapping input id must be inert")
        return value

    @field_validator(
        "expected_asset_metadata_fingerprint",
        "expected_field_metadata_fingerprint",
    )
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("semantic mapping input fingerprint is invalid")
        return value

    @field_validator("field_path")
    @classmethod
    def field_path_must_be_inert(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(_SAFE_SQL_IDENTIFIER.fullmatch(value) is None for value in values):
            raise ValueError(
                "semantic mapping input field path must use lowercase safe SQL identifiers"
            )
        return values

    @field_validator("physical_field")
    @classmethod
    def physical_field_must_be_compiler_safe(
        cls,
        value: PhysicalFieldRef,
    ) -> PhysicalFieldRef:
        segments = value.root.split(".")
        if len(segments) != 3 or any(
            _SAFE_SQL_IDENTIFIER.fullmatch(segment) is None for segment in segments
        ):
            raise ValueError(
                "semantic mapping input physical field must be exactly schema.table.column with "
                "lowercase safe SQL identifiers"
            )
        return value

    @model_validator(mode="after")
    def physical_field_must_match_field_path(self) -> SemanticOnboardingMappingInput:
        if self.physical_field.root.rsplit(".", maxsplit=1)[-1] != self.field_path[0]:
            raise ValueError("semantic mapping input physical field must match its field path")
        return self

    @field_validator("risks")
    @classmethod
    def risks_must_be_bounded(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() or len(value.encode("utf-8")) > 2_000 for value in values):
            raise ValueError("semantic mapping input risks are invalid")
        return values


class CreateSemanticOnboardingRequest(FrozenDomainModel):
    """Authenticated create input; scope, workspace and actor are injected server-side."""

    draft_id: str = Field(min_length=3, max_length=80)
    connection_id: CatalogConnectionId
    catalog_generation: int = Field(ge=1)
    catalog_generation_fingerprint: str
    expected_base_registry: OnboardingRegistryBase
    confirmed_preflight_fingerprint: str
    model: SemanticModelDefinition
    mappings: tuple[SemanticOnboardingMappingInput, ...] = Field(
        min_length=1,
        max_length=MAX_ONBOARDING_MAPPINGS,
    )

    @field_validator("draft_id")
    @classmethod
    def id_must_be_inert(cls, value: str) -> str:
        if _INERT_ID.fullmatch(value) is None:
            raise ValueError("semantic onboarding request id must be inert")
        return value

    @field_validator("catalog_generation_fingerprint", "confirmed_preflight_fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("semantic onboarding generation fingerprint is invalid")
        return value

    @model_validator(mode="after")
    def mapping_targets_must_cover_model(self) -> CreateSemanticOnboardingRequest:
        field_ids = {field.id.root for field in self.model.fields}
        mapping_ids = tuple(mapping.id for mapping in self.mappings)
        if len(mapping_ids) != len(set(mapping_ids)):
            raise ValueError("semantic onboarding request mapping ids must be unique")
        targets = {mapping.logical_field.root for mapping in self.mappings}
        if targets != field_ids:
            raise ValueError("semantic onboarding request must cover every model field")
        if len(_canonical_json(self.model_dump(mode="json"))) > MAX_ONBOARDING_DRAFT_BYTES:
            raise ValueError("semantic onboarding request exceeds the byte limit")
        return self


def semantic_onboarding_fingerprint(value: object) -> str:
    """Return a stable SHA-256 over one JSON-compatible onboarding payload."""

    return hashlib.sha256(_canonical_json(_jsonable(value))).hexdigest()


def _validate_review_status(
    status: ApprovalStatus,
    decision_id: str | None,
    decided_by: str | None,
    label: str,
) -> None:
    if status not in {
        ApprovalStatus.NEEDS_REVIEW,
        ApprovalStatus.APPROVED,
        ApprovalStatus.REJECTED,
    }:
        raise ValueError(f"semantic onboarding {label} status is invalid")
    terminal = status in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}
    if terminal != (decision_id is not None and decided_by is not None):
        raise ValueError(f"semantic onboarding {label} decision binding is invalid")
    if status is ApprovalStatus.NEEDS_REVIEW and (
        decision_id is not None or decided_by is not None
    ):
        raise ValueError(f"semantic onboarding {label} awaiting review cannot cite a decision")


def _jsonable(value: object) -> object:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


__all__ = [
    "MAX_ONBOARDING_DECISION_BYTES",
    "MAX_ONBOARDING_DRAFT_BYTES",
    "MAX_ONBOARDING_PROPOSAL_BYTES",
    "CreateSemanticOnboardingRequest",
    "OnboardingCatalogGeneration",
    "OnboardingEvidence",
    "OnboardingEvidenceKind",
    "OnboardingRegistryBase",
    "PhysicalCatalogObservation",
    "PreflightSemanticOnboardingRequest",
    "PreparedSemanticOnboardingProposal",
    "ResolvedOnboardingCatalogEvidence",
    "SemanticFieldDefinition",
    "SemanticMappingProposal",
    "SemanticModelDefinition",
    "SemanticModelProposal",
    "SemanticOnboardingAuditEvent",
    "SemanticOnboardingAuditRecord",
    "SemanticOnboardingDecision",
    "SemanticOnboardingDraft",
    "SemanticOnboardingDraftMutation",
    "SemanticOnboardingMappingInput",
    "SemanticOnboardingPermission",
    "SemanticOnboardingPreflight",
    "SemanticOnboardingPreflightSelection",
    "SemanticOnboardingPreparation",
    "SemanticOnboardingStatus",
    "SemanticOnboardingTargetKind",
    "apply_semantic_onboarding_decision",
    "semantic_onboarding_fingerprint",
]
