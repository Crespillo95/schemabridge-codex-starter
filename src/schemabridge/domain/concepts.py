"""Governed logical models and canonical fields."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import ConfigDict, Field, RootModel, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.decisions import ApprovalStatus
from schemabridge.domain.transformations import IdentifierNormalizationPlan

_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FIELD_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")


class LogicalModelRef(RootModel[str]):
    """Stable reference to a logical business model."""

    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def validate_reference(cls, value: str) -> str:
        if _NAME_PATTERN.fullmatch(value) is None:
            raise ValueError("logical model reference must be one inert identifier")
        return value

    def __str__(self) -> str:
        return self.root


class LogicalFieldRef(RootModel[str]):
    """Model-qualified reference to a canonical field."""

    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def validate_reference(cls, value: str) -> str:
        if _FIELD_PATTERN.fullmatch(value) is None:
            raise ValueError("logical field reference must be model-qualified")
        return value

    def __str__(self) -> str:
        return self.root


class CanonicalType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"


class CanonicalField(FrozenDomainModel):
    """Governed field definition independent of physical storage."""

    id: LogicalFieldRef
    canonical_name: str = Field(min_length=1)
    canonical_type: CanonicalType
    definition: str = Field(min_length=1)
    format_policy: IdentifierNormalizationPlan | None = None

    @field_validator("canonical_name")
    @classmethod
    def name_must_be_inert(cls, value: str) -> str:
        if _NAME_PATTERN.fullmatch(value) is None:
            raise ValueError("canonical_name must be an inert identifier")
        return value

    @field_validator("definition")
    @classmethod
    def definition_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("definition must not be blank")
        return value

    @model_validator(mode="after")
    def id_must_match_name(self) -> CanonicalField:
        if self.id.root.rsplit(".", 1)[1] != self.canonical_name:
            raise ValueError("canonical field id must end with canonical_name")
        if self.format_policy is not None and self.canonical_type is not CanonicalType.STRING:
            raise ValueError("identifier format policy requires canonical string type")
        return self


class LogicalModel(FrozenDomainModel):
    """Versioned logical business entity."""

    id: LogicalModelRef
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    fields: tuple[LogicalFieldRef, ...] = Field(min_length=1)
    status: ApprovalStatus
    version: int = Field(ge=1)

    @field_validator("name")
    @classmethod
    def name_must_be_inert(cls, value: str) -> str:
        if _NAME_PATTERN.fullmatch(value) is None:
            raise ValueError("name must be an inert identifier")
        return value

    @field_validator("description")
    @classmethod
    def description_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("description must not be blank")
        return value

    @model_validator(mode="after")
    def fields_must_belong_to_model(self) -> LogicalModel:
        if self.id.root != self.name:
            raise ValueError("logical model id must match name")
        expected_prefix = f"{self.id.root}."
        roots = [field.root for field in self.fields]
        if any(not root.startswith(expected_prefix) for root in roots):
            raise ValueError("every canonical field must belong to the logical model")
        if len(roots) != len(set(roots)):
            raise ValueError("logical model fields must be unique")
        return self
