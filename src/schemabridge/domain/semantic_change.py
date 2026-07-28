"""Pure, immutable contracts for governed semantic and join-change management."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.catalog_inventory import (
    CatalogConnectionId,
    CatalogFieldLocator,
)
from schemabridge.domain.concepts import LogicalFieldRef
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.joins import (
    Cardinality,
    FanoutPolicy,
    RelationshipProfile,
    classify_cardinality,
)
from schemabridge.domain.semantic_registry import (
    PhysicalValueType,
    SemanticRegistryScope,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[a-z][a-z0-9_-]{2,199}$")
_MAX_MAPPINGS = 2_000
_MAX_JOINS = 500
_MAX_FINDINGS = 2_000
_MAX_IMPACTS = 10_000
_MINIMUM_JOIN_OVERLAP = 0.50


class SemanticChangeKind(StrEnum):
    """Closed deterministic changes; none of these values grants approval."""

    BASELINE_REQUIRED = "baseline_required"
    BINDING_MISSING = "binding_missing"
    BINDING_AMBIGUOUS = "binding_ambiguous"
    ASSET_REMOVED = "asset_removed"
    FIELD_REMOVED = "field_removed"
    PHYSICAL_TYPE_CHANGED = "physical_type_changed"
    NULLABILITY_CHANGED = "nullability_changed"
    KEY_STATUS_CHANGED = "key_status_changed"
    FIELD_DEFINITION_CHANGED = "field_definition_changed"
    FIELD_TERMS_CHANGED = "field_terms_changed"
    ASSET_METADATA_CHANGED = "asset_metadata_changed"
    REGISTRY_CHANGED = "registry_changed"
    JOIN_CARDINALITY_CHANGED = "join_cardinality_changed"
    JOIN_FOREIGN_KEY_CHANGED = "join_foreign_key_changed"
    JOIN_OVERLAP_CHANGED = "join_overlap_changed"
    JOIN_NULLS_CHANGED = "join_nulls_changed"
    JOIN_INVALIDS_CHANGED = "join_invalids_changed"
    JOIN_MULTIPLICITY_CHANGED = "join_multiplicity_changed"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"


class SemanticChangeSeverity(StrEnum):
    INFORMATIONAL = "informational"
    REVIEW_REQUIRED = "review_required"
    BLOCKING = "blocking"


class SemanticChangeStatus(StrEnum):
    CURRENT = "current"
    REVIEW_REQUIRED = "review_required"
    BLOCKED = "blocked"
    REVALIDATED = "revalidated"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class SemanticImpactKind(StrEnum):
    MAPPING = "mapping"
    JOIN = "join"
    WORKFLOW = "workflow"
    RECIPE = "recipe"


class SemanticChangeDecisionAction(StrEnum):
    ESTABLISH_BASELINE = "establish_baseline"
    REVALIDATE_COMPATIBLE_CHANGE = "revalidate_compatible_change"
    REJECT_CHANGE = "reject_change"


class SemanticChangeConfirmation(StrEnum):
    ESTABLISH = "ESTABLISH SEMANTIC EVIDENCE BASELINE"
    REVALIDATE = "REVALIDATE COMPATIBLE SEMANTIC CHANGE"
    REJECT = "REJECT SEMANTIC CHANGE"


class CatalogGenerationObservation(FrozenDomainModel):
    connection_id: CatalogConnectionId
    generation: int = Field(ge=1)
    inventory_fingerprint: str

    @field_validator("inventory_fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "catalog generation fingerprint")


class CatalogGenerationVector(FrozenDomainModel):
    observations: tuple[CatalogGenerationObservation, ...] = Field(
        min_length=1,
        max_length=100_000,
    )
    fingerprint: str

    @field_validator("fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "catalog generation vector fingerprint")

    @model_validator(mode="after")
    def vector_must_be_sorted_unique_and_fingerprinted(self) -> CatalogGenerationVector:
        identities = tuple(item.connection_id.root for item in self.observations)
        if identities != tuple(sorted(set(identities))):
            raise ValueError("catalog generation vector must be sorted and unique")
        if self.fingerprint != semantic_change_fingerprint(
            [item.model_dump(mode="json") for item in self.observations]
        ):
            raise ValueError("catalog generation vector fingerprint does not match")
        return self

    @classmethod
    def create(
        cls,
        observations: tuple[CatalogGenerationObservation, ...],
    ) -> CatalogGenerationVector:
        ordered = tuple(sorted(observations, key=lambda item: item.connection_id.root))
        return cls(
            observations=ordered,
            fingerprint=semantic_change_fingerprint(
                [item.model_dump(mode="json") for item in ordered]
            ),
        )


class GovernedMappingRef(FrozenDomainModel):
    logical_field: LogicalFieldRef
    physical_field: PhysicalFieldRef
    version: int = Field(ge=1)
    approval_decision_id: str = Field(min_length=1, max_length=200)
    physical_type: PhysicalValueType

    @field_validator("approval_decision_id")
    @classmethod
    def decision_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("mapping decision id must not be blank")
        return value

    @property
    def identity(self) -> tuple[str, str, int, str]:
        return (
            self.logical_field.root,
            self.physical_field.root,
            self.version,
            self.approval_decision_id,
        )


class GovernedJoinRef(FrozenDomainModel):
    contract_id: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    version: int = Field(ge=1)
    approval_decision_id: str = Field(min_length=1, max_length=200)
    left_field: PhysicalFieldRef
    right_field: PhysicalFieldRef
    cardinality: Cardinality
    fanout_policy: FanoutPolicy

    @field_validator("approval_decision_id")
    @classmethod
    def decision_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("join decision id must not be blank")
        return value

    @property
    def identity(self) -> tuple[str, int, str]:
        return (self.contract_id, self.version, self.approval_decision_id)


class SemanticBindingSelection(FrozenDomainModel):
    """One explicit human selection of an exact catalog field for a governed mapping."""

    mapping_approval_decision_id: str = Field(min_length=1, max_length=200)
    mapping_version: int = Field(ge=1)
    physical_field: PhysicalFieldRef
    locator: CatalogFieldLocator

    @field_validator("mapping_approval_decision_id")
    @classmethod
    def decision_must_not_be_blank(cls, value: str) -> str:
        if not value.strip() or value.strip() != value:
            raise ValueError("binding selection decision id must be nonblank and canonical")
        return value

    @property
    def mapping_identity(self) -> tuple[str, int, str]:
        return (
            self.mapping_approval_decision_id,
            self.mapping_version,
            self.physical_field.root,
        )

    def matches(self, mapping: GovernedMappingRef) -> bool:
        """Return whether this selection names the exact governed mapping."""

        return self.mapping_identity == (
            mapping.approval_decision_id,
            mapping.version,
            mapping.physical_field.root,
        )


class SemanticBindingSelectionSet(FrozenDomainModel):
    """Canonical bounded selections supplied for one exact registry scope."""

    scope: SemanticRegistryScope
    selections: tuple[SemanticBindingSelection, ...] = Field(
        min_length=1,
        max_length=_MAX_MAPPINGS,
    )
    fingerprint: str

    @field_validator("fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "semantic binding selection-set fingerprint")

    @model_validator(mode="after")
    def selections_must_be_scoped_canonical_and_fingerprinted(
        self,
    ) -> SemanticBindingSelectionSet:
        mapping_identities = tuple(item.mapping_identity for item in self.selections)
        if mapping_identities != tuple(sorted(set(mapping_identities))):
            raise ValueError("semantic binding selections must be sorted and unique")
        locator_identities = tuple(_locator_identity(item.locator) for item in self.selections)
        if len(locator_identities) != len(set(locator_identities)):
            raise ValueError("one catalog field cannot be selected for multiple mappings")
        if any(
            item.locator.asset.workspace_id != self.scope.workspace_id for item in self.selections
        ):
            raise ValueError("semantic binding selection crosses workspaces")
        if self.fingerprint != semantic_change_fingerprint(_binding_selection_set_payload(self)):
            raise ValueError("semantic binding selection-set fingerprint does not match")
        return self

    @classmethod
    def create(
        cls,
        *,
        scope: SemanticRegistryScope,
        selections: tuple[SemanticBindingSelection, ...],
    ) -> SemanticBindingSelectionSet:
        ordered = tuple(sorted(selections, key=lambda item: item.mapping_identity))
        values: dict[str, object] = {"scope": scope, "selections": ordered}
        construct: Any = cls.model_construct
        unchecked = construct(**values, fingerprint="")
        return cls(
            **values,
            fingerprint=semantic_change_fingerprint(_binding_selection_set_payload(unchecked)),
        )


class GovernedResourceBinding(FrozenDomainModel):
    """Explicit metadata observation identity, never an execution credential."""

    binding_id: str
    mapping: GovernedMappingRef
    locator: CatalogFieldLocator
    catalog_generation: int = Field(ge=1)
    catalog_generation_fingerprint: str
    asset_metadata_fingerprint: str
    field_metadata_fingerprint: str
    field_definition_fingerprint: str
    field_terms_fingerprint: str
    fingerprint: str

    @field_validator("binding_id")
    @classmethod
    def id_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("semantic resource binding id must be inert")
        return value

    @field_validator(
        "catalog_generation_fingerprint",
        "asset_metadata_fingerprint",
        "field_metadata_fingerprint",
        "field_definition_fingerprint",
        "field_terms_fingerprint",
        "fingerprint",
    )
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "semantic resource binding fingerprint")

    @model_validator(mode="after")
    def identity_and_fingerprint_must_match(self) -> GovernedResourceBinding:
        expected = semantic_change_fingerprint(_binding_payload(self))
        expected_id = f"binding_{expected}"
        if self.binding_id != expected_id or self.fingerprint != expected:
            raise ValueError("semantic resource binding identity does not match")
        return self

    @classmethod
    def create(
        cls,
        *,
        mapping: GovernedMappingRef,
        locator: CatalogFieldLocator,
        catalog_generation: int,
        catalog_generation_fingerprint: str,
        asset_metadata_fingerprint: str,
        field_metadata_fingerprint: str,
        field_definition_fingerprint: str,
        field_terms_fingerprint: str,
    ) -> GovernedResourceBinding:
        values: dict[str, object] = {
            "mapping": mapping,
            "locator": locator,
            "catalog_generation": catalog_generation,
            "catalog_generation_fingerprint": catalog_generation_fingerprint,
            "asset_metadata_fingerprint": asset_metadata_fingerprint,
            "field_metadata_fingerprint": field_metadata_fingerprint,
            "field_definition_fingerprint": field_definition_fingerprint,
            "field_terms_fingerprint": field_terms_fingerprint,
        }
        fingerprint = semantic_change_fingerprint(_jsonable(values))
        return cls(binding_id=f"binding_{fingerprint}", fingerprint=fingerprint, **values)


class ObservedFieldEvidence(FrozenDomainModel):
    mapping: GovernedMappingRef
    binding: GovernedResourceBinding | None = None
    explicit_selection: SemanticBindingSelection | None = None
    present: bool
    candidate_count: int = Field(default=0, ge=0, le=1_000_000)
    normalized_type: PhysicalValueType | None = None
    nullable: bool | None = None
    is_part_of_key: bool | None = None
    asset_metadata_fingerprint: str | None = None
    field_metadata_fingerprint: str | None = None
    field_definition_fingerprint: str | None = None
    field_terms_fingerprint: str | None = None
    reason_code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{1,63}$",
    )
    fingerprint: str

    @field_validator(
        "asset_metadata_fingerprint",
        "field_metadata_fingerprint",
        "field_definition_fingerprint",
        "field_terms_fingerprint",
        "fingerprint",
    )
    @classmethod
    def optional_fingerprints_must_be_sha256(cls, value: str | None) -> str | None:
        if value is not None:
            return _sha256(value, "observed field evidence fingerprint")
        return value

    @model_validator(mode="after")
    def shape_and_fingerprint_must_match(self) -> ObservedFieldEvidence:
        evidence_values = (
            self.asset_metadata_fingerprint,
            self.field_metadata_fingerprint,
            self.field_definition_fingerprint,
            self.field_terms_fingerprint,
        )
        if self.present:
            if (
                self.binding is None
                or self.candidate_count < 1
                or self.normalized_type is None
                or any(value is None for value in evidence_values)
                or self.reason_code is not None
            ):
                raise ValueError("present field evidence requires one complete explicit binding")
        elif self.reason_code is None or any(value is not None for value in evidence_values):
            raise ValueError("unavailable field evidence requires one safe reason only")
        if self.binding is not None and self.binding.mapping != self.mapping:
            raise ValueError("field evidence binding identifies another mapping")
        if self.present and self.candidate_count > 1 and self.explicit_selection is None:
            raise ValueError("ambiguous field evidence requires one explicit selection")
        if self.explicit_selection is not None and (
            self.binding is None
            or not self.explicit_selection.matches(self.mapping)
            or self.explicit_selection.locator != self.binding.locator
        ):
            raise ValueError("field evidence selection does not match its exact binding")
        if self.fingerprint != semantic_change_fingerprint(_field_evidence_payload(self)):
            raise ValueError("observed field evidence fingerprint does not match")
        return self

    @classmethod
    def create(cls, **values: Any) -> ObservedFieldEvidence:
        construct: Any = cls.model_construct
        unchecked = construct(**values, fingerprint="")
        return cls(
            **values,
            fingerprint=semantic_change_fingerprint(_field_evidence_payload(unchecked)),
        )


class AggregateJoinProfile(FrozenDomainModel):
    join: GovernedJoinRef
    profile: RelationshipProfile
    observed_cardinality: Cardinality
    safety_fingerprint: str
    fingerprint: str

    @field_validator("safety_fingerprint", "fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "aggregate join profile fingerprint")

    @model_validator(mode="after")
    def classification_and_fingerprints_must_match(self) -> AggregateJoinProfile:
        if self.observed_cardinality is not classify_cardinality(self.profile).cardinality:
            raise ValueError("aggregate join cardinality does not match its profile")
        if self.safety_fingerprint != semantic_change_fingerprint(_join_safety_payload(self)):
            raise ValueError("aggregate join safety fingerprint does not match")
        if self.fingerprint != semantic_change_fingerprint(_join_profile_payload(self)):
            raise ValueError("aggregate join profile fingerprint does not match")
        return self

    @classmethod
    def create(
        cls,
        *,
        join: GovernedJoinRef,
        profile: RelationshipProfile,
    ) -> AggregateJoinProfile:
        values: dict[str, object] = {
            "join": join,
            "profile": profile,
            "observed_cardinality": classify_cardinality(profile).cardinality,
        }
        construct: Any = cls.model_construct
        unchecked = construct(**values, safety_fingerprint="", fingerprint="")
        safety = semantic_change_fingerprint(_join_safety_payload(unchecked))
        with_safety = construct(**values, safety_fingerprint=safety, fingerprint="")
        return cls(
            **values,
            safety_fingerprint=safety,
            fingerprint=semantic_change_fingerprint(_join_profile_payload(with_safety)),
        )


class JoinEvidenceBaseline(FrozenDomainModel):
    profile: AggregateJoinProfile
    approved_at: datetime

    @field_validator("approved_at")
    @classmethod
    def approved_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "join evidence baseline approval time")


class SemanticDependencyIndexState(FrozenDomainModel):
    """Exact dependency-index snapshot used to prove blast-radius completeness."""

    scope: SemanticRegistryScope
    watermark: int = Field(ge=0)
    fingerprint: str
    complete: bool

    @field_validator("fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "semantic dependency index fingerprint")


class SemanticChangeInspectionContext(FrozenDomainModel):
    scope: SemanticRegistryScope
    pointer_generation: int = Field(ge=1)
    pointer_fingerprint: str
    pointer_transition_id: str
    registry_version: int = Field(ge=1)
    registry_fingerprint: str
    mappings: tuple[GovernedMappingRef, ...] = Field(
        min_length=1,
        max_length=_MAX_MAPPINGS,
    )
    joins: tuple[GovernedJoinRef, ...] = Field(default=(), max_length=_MAX_JOINS)
    dependency_index: SemanticDependencyIndexState
    fingerprint: str

    @field_validator("pointer_fingerprint", "registry_fingerprint", "fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "semantic inspection context fingerprint")

    @field_validator("pointer_transition_id")
    @classmethod
    def transition_id_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("semantic inspection transition id must be inert")
        return value

    @model_validator(mode="after")
    def content_must_be_canonical_and_fingerprinted(self) -> SemanticChangeInspectionContext:
        mapping_ids = tuple(item.identity for item in self.mappings)
        mapping_decision_ids = tuple(item.approval_decision_id for item in self.mappings)
        join_ids = tuple(item.identity for item in self.joins)
        if mapping_ids != tuple(sorted(set(mapping_ids))):
            raise ValueError("semantic inspection mappings must be sorted and unique")
        if len(mapping_decision_ids) != len(set(mapping_decision_ids)):
            raise ValueError("semantic inspection mapping decisions must be unique")
        if join_ids != tuple(sorted(set(join_ids))):
            raise ValueError("semantic inspection joins must be sorted and unique")
        mapped_fields = {item.physical_field for item in self.mappings}
        if any(
            join.left_field not in mapped_fields or join.right_field not in mapped_fields
            for join in self.joins
        ):
            raise ValueError("semantic inspection join keys require governed mappings")
        if self.dependency_index.scope != self.scope:
            raise ValueError("semantic inspection dependency index crosses scope")
        if self.fingerprint != semantic_change_fingerprint(_inspection_context_payload(self)):
            raise ValueError("semantic inspection context fingerprint does not match")
        return self

    @classmethod
    def create(cls, **values: Any) -> SemanticChangeInspectionContext:
        mappings = tuple(sorted(values["mappings"], key=lambda item: item.identity))
        joins = tuple(sorted(values.get("joins", ()), key=lambda item: item.identity))
        values = {**values, "mappings": mappings, "joins": joins}
        construct: Any = cls.model_construct
        unchecked = construct(**values, fingerprint="")
        return cls(
            **values,
            fingerprint=semantic_change_fingerprint(_inspection_context_payload(unchecked)),
        )


class SemanticEvidenceObservation(FrozenDomainModel):
    context: SemanticChangeInspectionContext
    catalog_generations: CatalogGenerationVector
    fields: tuple[ObservedFieldEvidence, ...] = Field(
        min_length=1,
        max_length=_MAX_MAPPINGS,
    )
    joins: tuple[AggregateJoinProfile, ...] = Field(default=(), max_length=_MAX_JOINS)
    observed_at: datetime
    complete: bool
    fingerprint: str

    @field_validator("observed_at")
    @classmethod
    def observed_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "semantic evidence observation time")

    @field_validator("fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "semantic evidence observation fingerprint")

    @model_validator(mode="after")
    def evidence_must_match_context(self) -> SemanticEvidenceObservation:
        field_ids = tuple(item.mapping.identity for item in self.fields)
        mapping_ids = tuple(item.identity for item in self.context.mappings)
        join_ids = tuple(item.join.identity for item in self.joins)
        present_fields = {
            item.mapping.physical_field
            for item in self.fields
            if item.present and item.binding is not None
        }
        expected_join_ids = tuple(
            item.identity
            for item in self.context.joins
            if item.left_field in present_fields and item.right_field in present_fields
        )
        if field_ids != mapping_ids:
            raise ValueError("semantic field evidence must cover every mapping in order")
        if join_ids != expected_join_ids:
            raise ValueError("semantic join evidence must cover every exactly bound join in order")
        if any(
            item.binding is not None
            and item.binding.locator.asset.workspace_id != self.context.scope.workspace_id
            for item in self.fields
        ):
            raise ValueError("semantic field evidence binding crosses workspaces")
        if self.complete != all(item.present for item in self.fields):
            raise ValueError("semantic evidence completeness does not match its fields")
        if self.fingerprint != semantic_change_fingerprint(_observation_payload(self)):
            raise ValueError("semantic evidence observation fingerprint does not match")
        return self

    @classmethod
    def create(cls, **values: Any) -> SemanticEvidenceObservation:
        fields = tuple(sorted(values["fields"], key=lambda item: item.mapping.identity))
        joins = tuple(sorted(values.get("joins", ()), key=lambda item: item.join.identity))
        values = {**values, "fields": fields, "joins": joins}
        construct: Any = cls.model_construct
        unchecked = construct(**values, fingerprint="")
        return cls(
            **values,
            fingerprint=semantic_change_fingerprint(_observation_payload(unchecked)),
        )


class SemanticEvidenceBaseline(FrozenDomainModel):
    scope: SemanticRegistryScope
    revision: int = Field(ge=1)
    context_fingerprint: str
    pointer_generation: int = Field(ge=1)
    pointer_fingerprint: str
    registry_version: int = Field(ge=1)
    registry_fingerprint: str
    catalog_generations: CatalogGenerationVector
    fields: tuple[ObservedFieldEvidence, ...] = Field(
        min_length=1,
        max_length=_MAX_MAPPINGS,
    )
    joins: tuple[JoinEvidenceBaseline, ...] = Field(default=(), max_length=_MAX_JOINS)
    approval_id: str
    approved_at: datetime
    fingerprint: str

    @field_validator(
        "context_fingerprint",
        "pointer_fingerprint",
        "registry_fingerprint",
        "fingerprint",
    )
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "semantic evidence baseline fingerprint")

    @field_validator("approval_id")
    @classmethod
    def approval_id_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("semantic evidence approval id must be inert")
        return value

    @field_validator("approved_at")
    @classmethod
    def approved_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "semantic evidence baseline approval time")

    @model_validator(mode="after")
    def baseline_must_be_complete_and_fingerprinted(self) -> SemanticEvidenceBaseline:
        if any(not item.present or item.binding is None for item in self.fields):
            raise ValueError("approved semantic baseline requires complete explicit bindings")
        if self.fingerprint != semantic_change_fingerprint(_baseline_payload(self)):
            raise ValueError("semantic evidence baseline fingerprint does not match")
        return self


class SemanticChangeFinding(FrozenDomainModel):
    id: str
    kind: SemanticChangeKind
    severity: SemanticChangeSeverity
    mapping: GovernedMappingRef | None = None
    join: GovernedJoinRef | None = None
    previous_fingerprint: str | None = None
    current_fingerprint: str | None = None
    risks: tuple[str, ...] = Field(min_length=1, max_length=20)
    fingerprint: str

    @field_validator("id")
    @classmethod
    def id_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("semantic change finding id must be inert")
        return value

    @field_validator("previous_fingerprint", "current_fingerprint", "fingerprint")
    @classmethod
    def optional_fingerprints_must_be_sha256(cls, value: str | None) -> str | None:
        if value is not None:
            return _sha256(value, "semantic change finding fingerprint")
        return value

    @field_validator("risks")
    @classmethod
    def risks_must_be_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))) or any(not item.strip() for item in values):
            raise ValueError("semantic change risks must be sorted, unique, and nonblank")
        return values

    @model_validator(mode="after")
    def target_and_fingerprint_must_match(self) -> SemanticChangeFinding:
        if (self.mapping is None) == (self.join is None):
            raise ValueError("semantic change finding requires exactly one governed target")
        expected = semantic_change_fingerprint(_finding_payload(self))
        if self.fingerprint != expected or self.id != f"finding_{expected}":
            raise ValueError("semantic change finding identity does not match")
        return self


class SemanticChangeImpact(FrozenDomainModel):
    kind: SemanticImpactKind
    artifact_id: str = Field(min_length=1, max_length=200)
    artifact_version: int | None = Field(default=None, ge=1)
    finding_ids: tuple[str, ...] = Field(min_length=1, max_length=_MAX_FINDINGS)
    fingerprint: str

    @field_validator("artifact_id")
    @classmethod
    def artifact_id_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("semantic impact artifact id must not be blank")
        return value

    @field_validator("finding_ids")
    @classmethod
    def findings_must_be_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))):
            raise ValueError("semantic impact findings must be sorted and unique")
        return values

    @field_validator("fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "semantic impact fingerprint")

    @model_validator(mode="after")
    def fingerprint_must_match(self) -> SemanticChangeImpact:
        if self.fingerprint != semantic_change_fingerprint(_impact_payload(self)):
            raise ValueError("semantic impact fingerprint does not match")
        return self

    @classmethod
    def create(cls, **values: Any) -> SemanticChangeImpact:
        finding_ids = tuple(sorted(set(values["finding_ids"])))
        values = {**values, "finding_ids": finding_ids}
        construct: Any = cls.model_construct
        unchecked = construct(**values, fingerprint="")
        return cls(
            **values,
            fingerprint=semantic_change_fingerprint(_impact_payload(unchecked)),
        )


class SemanticImpactSet(FrozenDomainModel):
    impacts: tuple[SemanticChangeImpact, ...] = Field(default=(), max_length=_MAX_IMPACTS)
    complete: bool
    watermark: int = Field(ge=0)
    dependency_index_fingerprint: str
    fingerprint: str

    @field_validator("dependency_index_fingerprint", "fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "semantic impact-set fingerprint")

    @model_validator(mode="after")
    def impacts_must_be_canonical_and_fingerprinted(self) -> SemanticImpactSet:
        identities = tuple(
            (item.kind.value, item.artifact_id, item.artifact_version or 0) for item in self.impacts
        )
        if identities != tuple(sorted(set(identities))):
            raise ValueError("semantic impacts must be sorted and unique")
        if self.fingerprint != semantic_change_fingerprint(_impact_set_payload(self)):
            raise ValueError("semantic impact-set fingerprint does not match")
        return self

    @classmethod
    def create(
        cls,
        *,
        impacts: tuple[SemanticChangeImpact, ...],
        complete: bool,
        watermark: int,
        dependency_index_fingerprint: str,
    ) -> SemanticImpactSet:
        ordered = tuple(
            sorted(
                impacts,
                key=lambda item: (
                    item.kind.value,
                    item.artifact_id,
                    item.artifact_version or 0,
                ),
            )
        )
        values = {
            "impacts": ordered,
            "complete": complete,
            "watermark": watermark,
            "dependency_index_fingerprint": dependency_index_fingerprint,
        }
        construct: Any = cls.model_construct
        unchecked = construct(**values, fingerprint="")
        return cls(
            **values,
            fingerprint=semantic_change_fingerprint(_impact_set_payload(unchecked)),
        )


class SemanticImpactSummary(FrozenDomainModel):
    mapping_count: int = Field(ge=0)
    join_count: int = Field(ge=0)
    workflow_count: int = Field(ge=0)
    recipe_count: int = Field(ge=0)
    complete: bool
    watermark: int = Field(ge=0)
    dependency_index_fingerprint: str
    impact_set_fingerprint: str

    @field_validator("dependency_index_fingerprint", "impact_set_fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "semantic impact summary fingerprint")

    @classmethod
    def from_set(cls, impacts: SemanticImpactSet) -> SemanticImpactSummary:
        counts = {kind: 0 for kind in SemanticImpactKind}
        for impact in impacts.impacts:
            counts[impact.kind] += 1
        return cls(
            mapping_count=counts[SemanticImpactKind.MAPPING],
            join_count=counts[SemanticImpactKind.JOIN],
            workflow_count=counts[SemanticImpactKind.WORKFLOW],
            recipe_count=counts[SemanticImpactKind.RECIPE],
            complete=impacts.complete,
            watermark=impacts.watermark,
            dependency_index_fingerprint=impacts.dependency_index_fingerprint,
            impact_set_fingerprint=impacts.fingerprint,
        )


class SemanticChangeReport(FrozenDomainModel):
    id: str
    context: SemanticChangeInspectionContext
    observation_fingerprint: str
    catalog_generations: CatalogGenerationVector
    baseline_revision: int | None = Field(default=None, ge=1)
    baseline_fingerprint: str | None = None
    findings: tuple[SemanticChangeFinding, ...] = Field(default=(), max_length=_MAX_FINDINGS)
    impacts: SemanticImpactSummary
    status: SemanticChangeStatus
    inspected_at: datetime
    fingerprint: str

    @field_validator(
        "observation_fingerprint",
        "baseline_fingerprint",
        "fingerprint",
    )
    @classmethod
    def optional_fingerprints_must_be_sha256(cls, value: str | None) -> str | None:
        if value is not None:
            return _sha256(value, "semantic change report fingerprint")
        return value

    @field_validator("id")
    @classmethod
    def id_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("semantic change report id must be inert")
        return value

    @field_validator("inspected_at")
    @classmethod
    def inspected_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "semantic change report inspection time")

    @model_validator(mode="after")
    def report_must_be_canonical_and_fingerprinted(self) -> SemanticChangeReport:
        if (self.baseline_revision is None) != (self.baseline_fingerprint is None):
            raise ValueError("semantic report baseline identity must be complete")
        finding_ids = tuple(item.id for item in self.findings)
        if finding_ids != tuple(sorted(set(finding_ids))):
            raise ValueError("semantic report findings must be sorted and unique")
        severities = {finding.severity for finding in self.findings}
        expected_status = (
            SemanticChangeStatus.BLOCKED
            if SemanticChangeSeverity.BLOCKING in severities or not self.impacts.complete
            else (
                SemanticChangeStatus.REVIEW_REQUIRED
                if SemanticChangeSeverity.REVIEW_REQUIRED in severities
                else SemanticChangeStatus.CURRENT
            )
        )
        if self.status is not expected_status:
            raise ValueError("semantic report status does not match its findings")
        dependency = self.context.dependency_index
        if (
            self.impacts.watermark != dependency.watermark
            or self.impacts.dependency_index_fingerprint != dependency.fingerprint
            or self.impacts.complete != dependency.complete
        ):
            raise ValueError("semantic report impacts do not match dependency-index snapshot")
        expected = semantic_change_fingerprint(_report_payload(self))
        if self.fingerprint != expected or self.id != f"report_{expected}":
            raise ValueError("semantic report identity does not match")
        return self


class SemanticPlanDependencies(FrozenDomainModel):
    scope: SemanticRegistryScope
    pointer_generation: int = Field(ge=1)
    pointer_fingerprint: str
    registry_version: int = Field(ge=1)
    registry_fingerprint: str
    mappings: tuple[GovernedMappingRef, ...] = Field(
        min_length=1,
        max_length=_MAX_MAPPINGS,
    )
    joins: tuple[GovernedJoinRef, ...] = Field(default=(), max_length=_MAX_JOINS)
    fingerprint: str

    @field_validator("pointer_fingerprint", "registry_fingerprint", "fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "semantic plan dependency fingerprint")

    @model_validator(mode="after")
    def dependencies_must_be_canonical(self) -> SemanticPlanDependencies:
        mapping_ids = tuple(item.identity for item in self.mappings)
        join_ids = tuple(item.identity for item in self.joins)
        if mapping_ids != tuple(sorted(set(mapping_ids))) or join_ids != tuple(
            sorted(set(join_ids))
        ):
            raise ValueError("semantic plan dependencies must be sorted and unique")
        if self.fingerprint != semantic_change_fingerprint(_dependencies_payload(self)):
            raise ValueError("semantic plan dependency fingerprint does not match")
        return self

    @classmethod
    def create(cls, **values: Any) -> SemanticPlanDependencies:
        values = {
            **values,
            "mappings": tuple(sorted(values["mappings"], key=lambda item: item.identity)),
            "joins": tuple(sorted(values.get("joins", ()), key=lambda item: item.identity)),
        }
        construct: Any = cls.model_construct
        unchecked = construct(**values, fingerprint="")
        return cls(
            **values,
            fingerprint=semantic_change_fingerprint(_dependencies_payload(unchecked)),
        )


class SemanticContextGateAssessment(FrozenDomainModel):
    dependencies_fingerprint: str
    eligible: bool
    status: SemanticChangeStatus
    connection_id: CatalogConnectionId | None = None
    reason_codes: tuple[SemanticChangeKind, ...] = ()
    baseline_revision: int | None = Field(default=None, ge=1)

    @field_validator("dependencies_fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "semantic gate dependency fingerprint")

    @model_validator(mode="after")
    def eligibility_must_match_reasons(self) -> SemanticContextGateAssessment:
        if self.reason_codes != tuple(sorted(set(self.reason_codes), key=lambda item: item.value)):
            raise ValueError("semantic gate reasons must be sorted and unique")
        if self.eligible:
            if self.reason_codes or self.baseline_revision is None or self.connection_id is None:
                raise ValueError(
                    "eligible semantic context requires a current baseline and connection"
                )
            if self.status not in {
                SemanticChangeStatus.CURRENT,
                SemanticChangeStatus.REVALIDATED,
            }:
                raise ValueError("eligible semantic context must be current")
        else:
            if self.connection_id is not None:
                raise ValueError("ineligible semantic context cannot expose a connection")
            if not self.reason_codes or self.status not in {
                SemanticChangeStatus.BLOCKED,
                SemanticChangeStatus.REVIEW_REQUIRED,
                SemanticChangeStatus.REJECTED,
            }:
                raise ValueError("ineligible semantic context requires safe reasons")
        return self


class SemanticChangeDecisionProposal(FrozenDomainModel):
    report: SemanticChangeReport
    observation: SemanticEvidenceObservation
    action: SemanticChangeDecisionAction
    expected_head_revision: int = Field(ge=0)
    previous_head_context_fingerprint: str | None = None
    fingerprint: str

    @field_validator("previous_head_context_fingerprint", "fingerprint")
    @classmethod
    def optional_fingerprint_must_be_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _sha256(value, "semantic change proposal fingerprint")

    @model_validator(mode="after")
    def proposal_must_be_eligible_and_fingerprinted(self) -> SemanticChangeDecisionProposal:
        if (
            self.observation.context != self.report.context
            or self.observation.fingerprint != self.report.observation_fingerprint
            or self.observation.catalog_generations != self.report.catalog_generations
        ):
            raise ValueError("semantic decision proposal observation does not match its report")
        blocking = self.report.status is SemanticChangeStatus.BLOCKED
        if blocking and self.action is not SemanticChangeDecisionAction.REJECT_CHANGE:
            raise ValueError("blocking semantic change cannot be approved in the same registry")
        if self.action is SemanticChangeDecisionAction.ESTABLISH_BASELINE:
            if self.report.baseline_revision is not None:
                raise ValueError(
                    "semantic baseline establishment requires a report without baseline"
                )
            if self.expected_head_revision == 0:
                if self.previous_head_context_fingerprint is not None:
                    raise ValueError("initial semantic baseline cannot name a previous context")
            elif (
                self.previous_head_context_fingerprint is None
                or self.previous_head_context_fingerprint == self.report.context.fingerprint
            ):
                raise ValueError(
                    "semantic baseline recovery requires a different previous registry context"
                )
        if self.action is SemanticChangeDecisionAction.REVALIDATE_COMPATIBLE_CHANGE and (
            self.report.baseline_revision is None
            or self.expected_head_revision != self.report.baseline_revision
        ):
            raise ValueError("semantic revalidation requires the exact current baseline")
        if self.action is not SemanticChangeDecisionAction.REJECT_CHANGE and (
            not self.observation.complete or not self.report.impacts.complete
        ):
            raise ValueError("semantic approval requires complete evidence and dependencies")
        if self.fingerprint != semantic_change_fingerprint(_proposal_payload(self)):
            raise ValueError("semantic decision proposal fingerprint does not match")
        return self


class SemanticChangeDecisionApproval(FrozenDomainModel):
    id: str
    proposal_fingerprint: str
    report_fingerprint: str
    actor: str = Field(min_length=1, max_length=120)
    approved_at: datetime
    confirmation: SemanticChangeConfirmation

    @field_validator("id")
    @classmethod
    def id_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("semantic change approval id must be inert")
        return value

    @field_validator("proposal_fingerprint", "report_fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "semantic change approval fingerprint")

    @field_validator("actor")
    @classmethod
    def actor_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("semantic change approval actor must not be blank")
        return value

    @field_validator("approved_at")
    @classmethod
    def approved_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "semantic change approval time")


SemanticChangeApproval = SemanticChangeDecisionApproval


class SemanticChangeDecision(FrozenDomainModel):
    id: str
    proposal_fingerprint: str
    report_id: str
    report_fingerprint: str
    action: SemanticChangeDecisionAction
    resulting_status: SemanticChangeStatus
    actor: str = Field(min_length=1, max_length=120)
    decided_at: datetime
    baseline: SemanticEvidenceBaseline | None = None
    fingerprint: str

    @field_validator("id")
    @classmethod
    def id_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("semantic change decision id must be inert")
        return value

    @field_validator("proposal_fingerprint", "report_fingerprint", "fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "semantic change decision fingerprint")

    @field_validator("decided_at")
    @classmethod
    def decided_time_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "semantic change decision time")

    @model_validator(mode="after")
    def outcome_and_fingerprint_must_match(self) -> SemanticChangeDecision:
        rejected = self.action is SemanticChangeDecisionAction.REJECT_CHANGE
        if rejected != (self.baseline is None):
            raise ValueError("only accepted semantic decisions create an evidence baseline")
        if rejected != (self.resulting_status is SemanticChangeStatus.REJECTED):
            raise ValueError("semantic rejection status does not match its action")
        if not rejected and self.resulting_status is not SemanticChangeStatus.REVALIDATED:
            raise ValueError("accepted semantic decision must produce revalidated status")
        expected = semantic_change_fingerprint(_decision_payload(self))
        if self.fingerprint != expected or self.id != f"decision_{expected}":
            raise ValueError("semantic change decision identity does not match")
        return self


class SemanticChangeCommit(FrozenDomainModel):
    scope: SemanticRegistryScope
    head_revision: int = Field(ge=1)
    decision: SemanticChangeDecision
    baseline: SemanticEvidenceBaseline | None
    audit_event_hash: str
    replayed: bool = False

    @field_validator("audit_event_hash")
    @classmethod
    def audit_hash_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "semantic change audit hash")

    @model_validator(mode="after")
    def commit_parts_must_match(self) -> SemanticChangeCommit:
        if self.baseline != self.decision.baseline:
            raise ValueError("semantic change commit baseline does not match its decision")
        if self.baseline is not None and (
            self.baseline.scope != self.scope or self.baseline.revision != self.head_revision
        ):
            raise ValueError("semantic change commit baseline identifies another head")
        return self


def build_semantic_change_report(
    observation: SemanticEvidenceObservation,
    baseline: SemanticEvidenceBaseline | None,
    impacts: SemanticImpactSet,
) -> SemanticChangeReport:
    """Build one deterministic report from exact current and approved evidence."""

    effective_baseline = (
        baseline
        if baseline is not None
        and baseline.scope == observation.context.scope
        and baseline.context_fingerprint == observation.context.fingerprint
        else None
    )
    ordered_findings = classify_semantic_change_findings(
        observation,
        effective_baseline,
    )
    severities = {item.severity for item in ordered_findings}
    status = (
        SemanticChangeStatus.BLOCKED
        if SemanticChangeSeverity.BLOCKING in severities or not impacts.complete
        else (
            SemanticChangeStatus.REVIEW_REQUIRED
            if SemanticChangeSeverity.REVIEW_REQUIRED in severities
            else SemanticChangeStatus.CURRENT
        )
    )
    values: dict[str, object] = {
        "context": observation.context,
        "observation_fingerprint": observation.fingerprint,
        "catalog_generations": observation.catalog_generations,
        "baseline_revision": (
            effective_baseline.revision if effective_baseline is not None else None
        ),
        "baseline_fingerprint": (
            effective_baseline.fingerprint if effective_baseline is not None else None
        ),
        "findings": ordered_findings,
        "impacts": SemanticImpactSummary.from_set(impacts),
        "status": status,
        "inspected_at": observation.observed_at,
    }
    construct: Any = SemanticChangeReport.model_construct
    unchecked = construct(id="", fingerprint="", **values)
    fingerprint = semantic_change_fingerprint(_report_payload(unchecked))
    return SemanticChangeReport(
        id=f"report_{fingerprint}",
        fingerprint=fingerprint,
        **values,
    )


def classify_semantic_change_findings(
    observation: SemanticEvidenceObservation,
    baseline: SemanticEvidenceBaseline | None,
) -> tuple[SemanticChangeFinding, ...]:
    """Classify exact evidence without performing I/O or deriving downstream impact."""

    findings: list[SemanticChangeFinding] = []
    if baseline is None:
        findings.extend(
            _finding(
                SemanticChangeKind.BASELINE_REQUIRED,
                SemanticChangeSeverity.REVIEW_REQUIRED,
                mapping=current.mapping,
                previous=None,
                current=current.fingerprint,
                risks=("explicit_catalog_binding_requires_approval",),
            )
            for current in observation.fields
        )
    else:
        if (
            baseline.scope != observation.context.scope
            or baseline.context_fingerprint != observation.context.fingerprint
        ):
            target = observation.context.mappings[0]
            return _finalize_semantic_change_findings(
                observation,
                (
                    _finding(
                        SemanticChangeKind.REGISTRY_CHANGED,
                        SemanticChangeSeverity.BLOCKING,
                        mapping=target,
                        previous=baseline.context_fingerprint,
                        current=observation.context.fingerprint,
                        risks=("active_registry_requires_new_evidence",),
                    ),
                ),
            )
        previous_fields = {item.mapping.identity: item for item in baseline.fields}
        for current_field in observation.fields:
            findings.extend(
                _field_findings(
                    current_field,
                    previous_fields.get(current_field.mapping.identity),
                )
            )
        previous_joins = {item.profile.join.identity: item.profile for item in baseline.joins}
        for current_join in observation.joins:
            findings.extend(
                _join_findings(
                    current_join,
                    previous_joins.get(current_join.join.identity),
                )
            )

    if not observation.complete and not any(
        item.kind
        in {
            SemanticChangeKind.BINDING_MISSING,
            SemanticChangeKind.BINDING_AMBIGUOUS,
            SemanticChangeKind.FIELD_REMOVED,
        }
        for item in findings
    ):
        first = next(item for item in observation.fields if not item.present)
        findings.append(_unavailable_field_finding(first))
    return _finalize_semantic_change_findings(observation, findings)


def _finalize_semantic_change_findings(
    observation: SemanticEvidenceObservation,
    findings: tuple[SemanticChangeFinding, ...] | list[SemanticChangeFinding],
) -> tuple[SemanticChangeFinding, ...]:
    """Bind dependency coverage to the exact finding set used for blast radius."""

    finalized = list(findings)
    dependency = observation.context.dependency_index
    if not dependency.complete:
        finalized.append(
            _finding(
                SemanticChangeKind.EVIDENCE_UNAVAILABLE,
                SemanticChangeSeverity.BLOCKING,
                mapping=observation.context.mappings[0],
                previous=None,
                current=dependency.fingerprint,
                risks=("dependency_index_incomplete",),
            )
        )
    return tuple(sorted({item.id: item for item in finalized}.values(), key=lambda item: item.id))


def prepare_semantic_change_decision(
    report: SemanticChangeReport,
    observation: SemanticEvidenceObservation,
    *,
    action: SemanticChangeDecisionAction,
    expected_head_revision: int,
    previous_head_context_fingerprint: str | None = None,
) -> SemanticChangeDecisionProposal:
    values = {
        "report": report,
        "observation": observation,
        "action": action,
        "expected_head_revision": expected_head_revision,
        "previous_head_context_fingerprint": previous_head_context_fingerprint,
    }
    construct: Any = SemanticChangeDecisionProposal.model_construct
    unchecked = construct(**values, fingerprint="")
    return SemanticChangeDecisionProposal(
        **values,
        fingerprint=semantic_change_fingerprint(_proposal_payload(unchecked)),
    )


def semantic_change_approval_id(
    proposal: SemanticChangeDecisionProposal,
    actor: str,
    approved_at: datetime,
    confirmation: SemanticChangeConfirmation,
) -> str:
    return "approval_" + semantic_change_fingerprint(
        {
            "proposal_fingerprint": proposal.fingerprint,
            "actor": actor,
            "approved_at": _canonical_datetime(approved_at),
            "confirmation": confirmation.value,
        }
    )


def build_semantic_change_approval(
    proposal: SemanticChangeDecisionProposal,
    *,
    actor: str,
    approved_at: datetime,
    confirmation: SemanticChangeConfirmation,
) -> SemanticChangeDecisionApproval:
    approval = SemanticChangeDecisionApproval(
        id=semantic_change_approval_id(proposal, actor, approved_at, confirmation),
        proposal_fingerprint=proposal.fingerprint,
        report_fingerprint=proposal.report.fingerprint,
        actor=actor,
        approved_at=approved_at,
        confirmation=confirmation,
    )
    validate_semantic_change_approval(proposal, approval)
    return approval


def validate_semantic_change_approval(
    proposal: SemanticChangeDecisionProposal,
    approval: SemanticChangeDecisionApproval,
) -> None:
    expected_confirmation = {
        SemanticChangeDecisionAction.ESTABLISH_BASELINE: SemanticChangeConfirmation.ESTABLISH,
        SemanticChangeDecisionAction.REVALIDATE_COMPATIBLE_CHANGE: (
            SemanticChangeConfirmation.REVALIDATE
        ),
        SemanticChangeDecisionAction.REJECT_CHANGE: SemanticChangeConfirmation.REJECT,
    }[proposal.action]
    if (
        approval.proposal_fingerprint != proposal.fingerprint
        or approval.report_fingerprint != proposal.report.fingerprint
        or approval.confirmation is not expected_confirmation
        or approval.id
        != semantic_change_approval_id(
            proposal,
            approval.actor,
            approval.approved_at,
            approval.confirmation,
        )
    ):
        raise ValueError("semantic change approval does not match its exact proposal")


def build_semantic_change_decision(
    proposal: SemanticChangeDecisionProposal,
    approval: SemanticChangeDecisionApproval,
) -> SemanticChangeDecision:
    validate_semantic_change_approval(proposal, approval)
    accepted = proposal.action is not SemanticChangeDecisionAction.REJECT_CHANGE
    baseline = (
        _baseline_from_observation(
            proposal.observation,
            revision=proposal.expected_head_revision + 1,
            approval_id=approval.id,
            approved_at=approval.approved_at,
        )
        if accepted
        else None
    )
    values: dict[str, object] = {
        "proposal_fingerprint": proposal.fingerprint,
        "report_id": proposal.report.id,
        "report_fingerprint": proposal.report.fingerprint,
        "action": proposal.action,
        "resulting_status": (
            SemanticChangeStatus.REVALIDATED if accepted else SemanticChangeStatus.REJECTED
        ),
        "actor": approval.actor,
        "decided_at": approval.approved_at,
        "baseline": baseline,
    }
    construct: Any = SemanticChangeDecision.model_construct
    unchecked = construct(id="", fingerprint="", **values)
    fingerprint = semantic_change_fingerprint(_decision_payload(unchecked))
    decision_id = f"decision_{fingerprint}"
    return SemanticChangeDecision(
        id=decision_id,
        fingerprint=fingerprint,
        **values,
    )


def semantic_change_fingerprint(payload: object) -> str:
    encoded = json.dumps(
        _jsonable(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _baseline_from_observation(
    observation: SemanticEvidenceObservation,
    *,
    revision: int,
    approval_id: str,
    approved_at: datetime,
) -> SemanticEvidenceBaseline:
    joins = tuple(
        JoinEvidenceBaseline(profile=item, approved_at=approved_at) for item in observation.joins
    )
    values: dict[str, object] = {
        "scope": observation.context.scope,
        "revision": revision,
        "context_fingerprint": observation.context.fingerprint,
        "pointer_generation": observation.context.pointer_generation,
        "pointer_fingerprint": observation.context.pointer_fingerprint,
        "registry_version": observation.context.registry_version,
        "registry_fingerprint": observation.context.registry_fingerprint,
        "catalog_generations": observation.catalog_generations,
        "fields": observation.fields,
        "joins": joins,
        "approval_id": approval_id,
        "approved_at": approved_at,
    }
    construct: Any = SemanticEvidenceBaseline.model_construct
    unchecked = construct(**values, fingerprint="")
    return SemanticEvidenceBaseline(
        **values,
        fingerprint=semantic_change_fingerprint(_baseline_payload(unchecked)),
    )


def _field_findings(
    current: ObservedFieldEvidence,
    previous: ObservedFieldEvidence | None,
) -> tuple[SemanticChangeFinding, ...]:
    if previous is None:
        return (
            _finding(
                SemanticChangeKind.BINDING_MISSING,
                SemanticChangeSeverity.BLOCKING,
                mapping=current.mapping,
                previous=None,
                current=current.fingerprint,
                risks=("approved_binding_unavailable",),
            ),
        )
    if not current.present:
        return (_unavailable_field_finding(current),)
    assert current.binding is not None
    assert previous.binding is not None
    if current.binding.locator != previous.binding.locator:
        return (
            _finding(
                SemanticChangeKind.BINDING_AMBIGUOUS,
                SemanticChangeSeverity.BLOCKING,
                mapping=current.mapping,
                previous=previous.binding.fingerprint,
                current=current.binding.fingerprint,
                risks=("catalog_resource_identity_changed",),
            ),
        )
    checks = (
        (
            SemanticChangeKind.PHYSICAL_TYPE_CHANGED,
            current.normalized_type,
            previous.normalized_type,
            "physical_type_requires_registry_remediation",
        ),
        (
            SemanticChangeKind.NULLABILITY_CHANGED,
            current.nullable,
            previous.nullable,
            "null_policy_requires_registry_remediation",
        ),
        (
            SemanticChangeKind.KEY_STATUS_CHANGED,
            current.is_part_of_key,
            previous.is_part_of_key,
            "key_policy_requires_registry_remediation",
        ),
    )
    findings: list[SemanticChangeFinding] = []
    for kind, new, old, risk in checks:
        if new != old:
            findings.append(
                _finding(
                    kind,
                    SemanticChangeSeverity.BLOCKING,
                    mapping=current.mapping,
                    previous=semantic_change_fingerprint(old),
                    current=semantic_change_fingerprint(new),
                    risks=(risk,),
                )
            )
    metadata_checks: tuple[
        tuple[SemanticChangeKind, str | None, str | None, str],
        ...,
    ] = (
        (
            SemanticChangeKind.FIELD_DEFINITION_CHANGED,
            current.field_definition_fingerprint,
            previous.field_definition_fingerprint,
            "semantic_definition_requires_review",
        ),
        (
            SemanticChangeKind.FIELD_TERMS_CHANGED,
            current.field_terms_fingerprint,
            previous.field_terms_fingerprint,
            "governance_terms_require_review",
        ),
        (
            SemanticChangeKind.ASSET_METADATA_CHANGED,
            current.asset_metadata_fingerprint,
            previous.asset_metadata_fingerprint,
            "asset_context_requires_review",
        ),
    )
    for kind, new_fingerprint, old_fingerprint, risk in metadata_checks:
        if new_fingerprint != old_fingerprint:
            findings.append(
                _finding(
                    kind,
                    SemanticChangeSeverity.REVIEW_REQUIRED,
                    mapping=current.mapping,
                    previous=old_fingerprint,
                    current=new_fingerprint,
                    risks=(risk,),
                )
            )
    return tuple(findings)


def _unavailable_field_finding(
    current: ObservedFieldEvidence,
) -> SemanticChangeFinding:
    kind = {
        "binding_ambiguous": SemanticChangeKind.BINDING_AMBIGUOUS,
        "asset_removed": SemanticChangeKind.ASSET_REMOVED,
        "field_removed": SemanticChangeKind.FIELD_REMOVED,
    }.get(current.reason_code or "", SemanticChangeKind.BINDING_MISSING)
    return _finding(
        kind,
        SemanticChangeSeverity.BLOCKING,
        mapping=current.mapping,
        previous=None,
        current=current.fingerprint,
        risks=(current.reason_code or "governed_evidence_unavailable",),
    )


def _join_findings(
    current: AggregateJoinProfile,
    previous: AggregateJoinProfile | None,
) -> tuple[SemanticChangeFinding, ...]:
    if previous is None:
        return (
            _finding(
                SemanticChangeKind.BASELINE_REQUIRED,
                SemanticChangeSeverity.REVIEW_REQUIRED,
                join=current.join,
                previous=None,
                current=current.fingerprint,
                risks=("aggregate_join_baseline_requires_approval",),
            ),
        )
    findings: list[SemanticChangeFinding] = []
    checks: tuple[tuple[SemanticChangeKind, object, object, str], ...] = (
        (
            SemanticChangeKind.JOIN_CARDINALITY_CHANGED,
            current.observed_cardinality,
            previous.observed_cardinality,
            "approved_cardinality_changed",
        ),
        (
            SemanticChangeKind.JOIN_FOREIGN_KEY_CHANGED,
            current.profile.declared_relationship,
            previous.profile.declared_relationship,
            "declared_relationship_evidence_changed",
        ),
        (
            SemanticChangeKind.JOIN_NULLS_CHANGED,
            (current.profile.left_null_count > 0, current.profile.right_null_count > 0),
            (previous.profile.left_null_count > 0, previous.profile.right_null_count > 0),
            "join_null_safety_changed",
        ),
        (
            SemanticChangeKind.JOIN_INVALIDS_CHANGED,
            (
                current.profile.left_invalid_count > 0,
                current.profile.right_invalid_count > 0,
            ),
            (
                previous.profile.left_invalid_count > 0,
                previous.profile.right_invalid_count > 0,
            ),
            "join_invalid_key_safety_changed",
        ),
        (
            SemanticChangeKind.JOIN_MULTIPLICITY_CHANGED,
            (
                current.profile.left_max_multiplicity > 1,
                current.profile.right_max_multiplicity > 1,
            ),
            (
                previous.profile.left_max_multiplicity > 1,
                previous.profile.right_max_multiplicity > 1,
            ),
            "join_fanout_safety_changed",
        ),
        (
            SemanticChangeKind.JOIN_OVERLAP_CHANGED,
            current.profile.overlap_ratio >= _MINIMUM_JOIN_OVERLAP,
            previous.profile.overlap_ratio >= _MINIMUM_JOIN_OVERLAP,
            "join_overlap_safety_changed",
        ),
    )
    for kind, new, old, risk in checks:
        if new != old:
            findings.append(
                _finding(
                    kind,
                    SemanticChangeSeverity.BLOCKING,
                    join=current.join,
                    previous=semantic_change_fingerprint(old),
                    current=semantic_change_fingerprint(new),
                    risks=(risk,),
                )
            )
    return tuple(findings)


def _finding(
    kind: SemanticChangeKind,
    severity: SemanticChangeSeverity,
    *,
    mapping: GovernedMappingRef | None = None,
    join: GovernedJoinRef | None = None,
    previous: str | None,
    current: str | None,
    risks: tuple[str, ...],
) -> SemanticChangeFinding:
    values: dict[str, object] = {
        "kind": kind,
        "severity": severity,
        "mapping": mapping,
        "join": join,
        "previous_fingerprint": previous,
        "current_fingerprint": current,
        "risks": tuple(sorted(set(risks))),
    }
    construct: Any = SemanticChangeFinding.model_construct
    unchecked = construct(id="", fingerprint="", **values)
    fingerprint = semantic_change_fingerprint(_finding_payload(unchecked))
    return SemanticChangeFinding(
        id=f"finding_{fingerprint}",
        fingerprint=fingerprint,
        **values,
    )


def _inspection_context_payload(value: SemanticChangeInspectionContext) -> dict[str, object]:
    return {
        "scope": value.scope,
        "pointer_generation": value.pointer_generation,
        "pointer_fingerprint": value.pointer_fingerprint,
        "pointer_transition_id": value.pointer_transition_id,
        "registry_version": value.registry_version,
        "registry_fingerprint": value.registry_fingerprint,
        "mappings": value.mappings,
        "joins": value.joins,
        "dependency_index": value.dependency_index,
    }


def _binding_payload(value: GovernedResourceBinding) -> dict[str, object]:
    return {
        "mapping": value.mapping,
        "locator": value.locator,
        "catalog_generation": value.catalog_generation,
        "catalog_generation_fingerprint": value.catalog_generation_fingerprint,
        "asset_metadata_fingerprint": value.asset_metadata_fingerprint,
        "field_metadata_fingerprint": value.field_metadata_fingerprint,
        "field_definition_fingerprint": value.field_definition_fingerprint,
        "field_terms_fingerprint": value.field_terms_fingerprint,
    }


def _binding_selection_set_payload(
    value: SemanticBindingSelectionSet,
) -> dict[str, object]:
    return {
        "scope": value.scope,
        "selections": value.selections,
    }


def _locator_identity(
    locator: CatalogFieldLocator,
) -> tuple[str, str, str, tuple[str, ...]]:
    return (
        locator.asset.workspace_id,
        locator.asset.connection_id.root,
        locator.asset.asset_id.root,
        locator.field_path,
    )


def _field_evidence_payload(value: ObservedFieldEvidence) -> dict[str, object]:
    return {
        "mapping": value.mapping,
        "binding": value.binding,
        "explicit_selection": value.explicit_selection,
        "present": value.present,
        "candidate_count": value.candidate_count,
        "normalized_type": value.normalized_type,
        "nullable": value.nullable,
        "is_part_of_key": value.is_part_of_key,
        "asset_metadata_fingerprint": value.asset_metadata_fingerprint,
        "field_metadata_fingerprint": value.field_metadata_fingerprint,
        "field_definition_fingerprint": value.field_definition_fingerprint,
        "field_terms_fingerprint": value.field_terms_fingerprint,
        "reason_code": value.reason_code,
    }


def _join_safety_payload(value: AggregateJoinProfile) -> dict[str, object]:
    profile = value.profile
    return {
        "join": value.join,
        "observed_cardinality": value.observed_cardinality,
        "declared_relationship": profile.declared_relationship,
        "left_has_nulls": profile.left_null_count > 0,
        "right_has_nulls": profile.right_null_count > 0,
        "left_has_invalids": profile.left_invalid_count > 0,
        "right_has_invalids": profile.right_invalid_count > 0,
        "left_has_duplicates": profile.left_max_multiplicity > 1,
        "right_has_duplicates": profile.right_max_multiplicity > 1,
        "has_overlap": profile.matching_distinct_keys > 0,
        "transaction_read_only": profile.transaction_read_only,
        "statement_timeout_ms": profile.statement_timeout_ms,
    }


def _join_profile_payload(value: AggregateJoinProfile) -> dict[str, object]:
    return {
        "join": value.join,
        "profile": value.profile,
        "observed_cardinality": value.observed_cardinality,
        "safety_fingerprint": value.safety_fingerprint,
    }


def _observation_payload(value: SemanticEvidenceObservation) -> dict[str, object]:
    return {
        "context": value.context,
        "catalog_generations": value.catalog_generations,
        "fields": value.fields,
        "joins": value.joins,
        "observed_at": value.observed_at,
        "complete": value.complete,
    }


def _baseline_payload(value: SemanticEvidenceBaseline) -> dict[str, object]:
    return {
        "scope": value.scope,
        "revision": value.revision,
        "context_fingerprint": value.context_fingerprint,
        "pointer_generation": value.pointer_generation,
        "pointer_fingerprint": value.pointer_fingerprint,
        "registry_version": value.registry_version,
        "registry_fingerprint": value.registry_fingerprint,
        "catalog_generations": value.catalog_generations,
        "fields": value.fields,
        "joins": value.joins,
        "approval_id": value.approval_id,
        "approved_at": value.approved_at,
    }


def _finding_payload(value: SemanticChangeFinding) -> dict[str, object]:
    return {
        "kind": value.kind,
        "severity": value.severity,
        "mapping": value.mapping,
        "join": value.join,
        "previous_fingerprint": value.previous_fingerprint,
        "current_fingerprint": value.current_fingerprint,
        "risks": value.risks,
    }


def _impact_payload(value: SemanticChangeImpact) -> dict[str, object]:
    return {
        "kind": value.kind,
        "artifact_id": value.artifact_id,
        "artifact_version": value.artifact_version,
        "finding_ids": value.finding_ids,
    }


def _impact_set_payload(value: SemanticImpactSet) -> dict[str, object]:
    return {
        "impacts": value.impacts,
        "complete": value.complete,
        "watermark": value.watermark,
        "dependency_index_fingerprint": value.dependency_index_fingerprint,
    }


def _report_payload(value: SemanticChangeReport) -> dict[str, object]:
    return {
        "context": value.context,
        "observation_fingerprint": value.observation_fingerprint,
        "catalog_generations": value.catalog_generations,
        "baseline_revision": value.baseline_revision,
        "baseline_fingerprint": value.baseline_fingerprint,
        "findings": value.findings,
        "impacts": value.impacts,
        "status": value.status,
        "inspected_at": value.inspected_at,
    }


def _dependencies_payload(value: SemanticPlanDependencies) -> dict[str, object]:
    return {
        "scope": value.scope,
        "pointer_generation": value.pointer_generation,
        "pointer_fingerprint": value.pointer_fingerprint,
        "registry_version": value.registry_version,
        "registry_fingerprint": value.registry_fingerprint,
        "mappings": value.mappings,
        "joins": value.joins,
    }


def _proposal_payload(value: SemanticChangeDecisionProposal) -> dict[str, object]:
    return {
        "report_fingerprint": value.report.fingerprint,
        "observation_fingerprint": value.observation.fingerprint,
        "action": value.action,
        "expected_head_revision": value.expected_head_revision,
        "previous_head_context_fingerprint": value.previous_head_context_fingerprint,
    }


def _decision_payload(value: SemanticChangeDecision) -> dict[str, object]:
    return {
        "proposal_fingerprint": value.proposal_fingerprint,
        "report_id": value.report_id,
        "report_fingerprint": value.report_fingerprint,
        "action": value.action,
        "resulting_status": value.resulting_status,
        "actor": value.actor,
        "decided_at": value.decided_at,
        "baseline": value.baseline,
    }


def _jsonable(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return _canonical_datetime(value)
    return value


def _canonical_datetime(value: datetime) -> str:
    return _aware(value, "semantic change time").isoformat()


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return value


def _sha256(value: str, label: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


__all__ = [
    "AggregateJoinProfile",
    "CatalogGenerationObservation",
    "CatalogGenerationVector",
    "GovernedJoinRef",
    "GovernedMappingRef",
    "GovernedResourceBinding",
    "JoinEvidenceBaseline",
    "ObservedFieldEvidence",
    "SemanticBindingSelection",
    "SemanticBindingSelectionSet",
    "SemanticChangeApproval",
    "SemanticChangeCommit",
    "SemanticChangeConfirmation",
    "SemanticChangeDecision",
    "SemanticChangeDecisionAction",
    "SemanticChangeDecisionApproval",
    "SemanticChangeDecisionProposal",
    "SemanticChangeFinding",
    "SemanticChangeInspectionContext",
    "SemanticChangeKind",
    "SemanticChangeReport",
    "SemanticChangeSeverity",
    "SemanticChangeStatus",
    "SemanticContextGateAssessment",
    "SemanticDependencyIndexState",
    "SemanticEvidenceBaseline",
    "SemanticEvidenceObservation",
    "SemanticImpactKind",
    "SemanticImpactSet",
    "SemanticImpactSummary",
    "SemanticPlanDependencies",
    "build_semantic_change_approval",
    "build_semantic_change_decision",
    "build_semantic_change_report",
    "classify_semantic_change_findings",
    "prepare_semantic_change_decision",
    "semantic_change_approval_id",
    "semantic_change_fingerprint",
    "validate_semantic_change_approval",
]
