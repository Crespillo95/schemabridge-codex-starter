"""Physical field identities and bounded profile signals."""

from __future__ import annotations

import re

from pydantic import ConfigDict, Field, RootModel, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel

_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_DATASET_PATTERN = re.compile(rf"^{_IDENTIFIER}\.{_IDENTIFIER}$")
_FIELD_PATTERN = re.compile(rf"^{_IDENTIFIER}\.{_IDENTIFIER}(?:\.{_IDENTIFIER})+$")
_PATH_SEGMENT_PATTERN = re.compile(rf"^{_IDENTIFIER}$")


class PhysicalDatasetRef(RootModel[str]):
    """Stable schema-qualified physical dataset reference."""

    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def validate_reference(cls, value: str) -> str:
        """Require the bounded PostgreSQL-style reference used by the MVP."""

        if _DATASET_PATTERN.fullmatch(value) is None:
            raise ValueError("physical dataset must be schema-qualified")
        return value

    def __str__(self) -> str:
        return self.root


class PhysicalFieldRef(RootModel[str]):
    """Stable schema-, dataset-, and path-qualified field reference."""

    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def validate_reference(cls, value: str) -> str:
        """Reject ambiguous or executable-looking field references."""

        if _FIELD_PATTERN.fullmatch(value) is None:
            raise ValueError("physical field must include schema, dataset, and field path")
        return value

    def __str__(self) -> str:
        return self.root


class FieldProfile(FrozenDomainModel):
    """Bounded aggregate signals; raw sample values are intentionally absent."""

    row_count: int = Field(ge=0)
    null_count: int = Field(ge=0)
    distinct_count: int = Field(ge=0)
    minimum_length: int | None = Field(default=None, ge=0)
    maximum_length: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def counts_must_be_consistent(self) -> FieldProfile:
        """Reject impossible profile summaries."""

        non_null_count = self.row_count - self.null_count
        if non_null_count < 0:
            raise ValueError("null_count cannot exceed row_count")
        if self.distinct_count > non_null_count:
            raise ValueError("distinct_count cannot exceed non-null row count")
        if (
            self.minimum_length is not None
            and self.maximum_length is not None
            and self.minimum_length > self.maximum_length
        ):
            raise ValueError("minimum_length cannot exceed maximum_length")
        return self


class PhysicalField(FrozenDomainModel):
    """Physical metadata without persisted raw source samples."""

    id: PhysicalFieldRef
    dataset: PhysicalDatasetRef
    field_path: tuple[str, ...] = Field(min_length=1)
    native_type: str = Field(min_length=1)
    description: str | None = None
    glossary_terms: tuple[str, ...] = ()
    profile: FieldProfile | None = None

    @field_validator("field_path")
    @classmethod
    def validate_field_path(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Require inert identifier path segments."""

        if any(_PATH_SEGMENT_PATTERN.fullmatch(segment) is None for segment in value):
            raise ValueError("field_path contains an invalid segment")
        return value

    @field_validator("native_type")
    @classmethod
    def native_type_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("native_type must not be blank")
        return value

    @field_validator("description")
    @classmethod
    def description_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("description must not be blank")
        return value

    @field_validator("glossary_terms")
    @classmethod
    def terms_must_not_be_blank(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("glossary terms must not be blank")
        return values

    @model_validator(mode="after")
    def identity_must_match_components(self) -> PhysicalField:
        """Keep the stable identity consistent with its structured components."""

        expected = ".".join((self.dataset.root, *self.field_path))
        if self.id.root != expected:
            raise ValueError("physical field id does not match dataset and field_path")
        return self
