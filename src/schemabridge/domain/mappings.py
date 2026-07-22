"""Evidence-bearing physical-to-logical mapping contracts."""

from __future__ import annotations

import math

from pydantic import ConfigDict, Field, RootModel, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.concepts import LogicalFieldRef
from schemabridge.domain.decisions import ApprovalStatus
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.transformations import TransformationPlan


class ConfidenceScore(RootModel[float]):
    """Bounded recommendation score that never grants approval."""

    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def validate_score(cls, value: float) -> float:
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError("confidence must be finite and between zero and one")
        return value


class ColumnMapping(FrozenDomainModel):
    """Versioned mapping proposal or approved mapping with explicit evidence."""

    logical_field: LogicalFieldRef
    physical_field: PhysicalFieldRef
    confidence: ConfidenceScore
    status: ApprovalStatus
    evidence: tuple[str, ...] = Field(min_length=1)
    risks: tuple[str, ...] = ()
    transformation_plan: TransformationPlan
    version: int = Field(default=1, ge=1)

    @field_validator("evidence", "risks")
    @classmethod
    def entries_must_not_be_blank(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("evidence and risk entries must not be blank")
        return values


class MappingPlan(FrozenDomainModel):
    """Deterministically serializable collection of column mappings."""

    version: int = Field(default=1, ge=1)
    mappings: tuple[ColumnMapping, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def approved_physical_meanings_must_be_unique(self) -> MappingPlan:
        approved: dict[str, str] = {}
        pairs: set[tuple[str, str]] = set()
        for mapping in self.mappings:
            pair = (mapping.physical_field.root, mapping.logical_field.root)
            if pair in pairs:
                raise ValueError("mapping pairs must be unique")
            pairs.add(pair)
            if mapping.status is ApprovalStatus.APPROVED:
                previous = approved.setdefault(
                    mapping.physical_field.root, mapping.logical_field.root
                )
                if previous != mapping.logical_field.root:
                    raise ValueError(
                        "an approved physical field cannot have multiple logical meanings"
                    )
        return self
