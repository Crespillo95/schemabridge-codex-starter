"""Pure natural-language intent contracts with no SQL or provider types."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.concepts import CanonicalType, LogicalFieldRef, LogicalModelRef
from schemabridge.domain.joins import Cardinality, FanoutPolicy
from schemabridge.domain.request_context import LogicalFieldRole
from schemabridge.domain.requests import (
    AnalyticalRequest,
    DateGrain,
    FilterOperator,
    MetricOperation,
)


class UserLanguage(StrEnum):
    SPANISH = "es"
    ENGLISH = "en"


class IntentAmbiguityKind(StrEnum):
    COUNT_OR_LIST = "count_or_list"
    DISTINCT_OR_RELATIONSHIP_COUNT = "distinct_or_relationship_count"
    DATE_MEANING = "date_meaning"
    UNKNOWN_ROLE_VALUE = "unknown_role_value"
    UNRESOLVED_BUSINESS_REQUEST = "unresolved_business_request"


class IntentAlternativeId(StrEnum):
    CONFIRM_INTERPRETATION = "confirm-interpretation"
    COUNT_DISTINCT_CUSTOMERS = "count-distinct-customers"
    COUNT_HOLDER_RELATIONSHIPS = "count-holder-relationships"
    LIST_CUSTOMERS = "list-customers"
    USE_REGISTRATION_DATE = "use-registration-date"
    USE_RELATIONSHIP_DATE = "use-relationship-date"
    USE_PRIMARY_ROLE = "use-primary-role"
    USE_SECONDARY_ROLE = "use-secondary-role"


class IntentVocabularyField(FrozenDomainModel):
    id: LogicalFieldRef
    canonical_type: CanonicalType
    role: LogicalFieldRole
    definition: str = Field(min_length=1, max_length=300)
    allowed_values: tuple[str, ...] = Field(default=(), max_length=12)

    @field_validator("definition")
    @classmethod
    def definition_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("intent vocabulary definition must not be blank")
        return value

    @field_validator("allowed_values")
    @classmethod
    def allowed_values_must_be_bounded_and_unique(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(not value.strip() for value in values):
            raise ValueError("intent vocabulary values must be unique and nonblank")
        return values


class IntentVocabularyModel(FrozenDomainModel):
    id: LogicalModelRef
    definition: str = Field(min_length=1, max_length=300)


class IntentVocabularyJoin(FrozenDomainModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    left_model: LogicalModelRef
    right_model: LogicalModelRef
    cardinality: Cardinality
    fanout_policy: FanoutPolicy


class IntentVocabulary(FrozenDomainModel):
    context_source: str = Field(min_length=1)
    context_version: int = Field(ge=1)
    models: tuple[IntentVocabularyModel, ...] = Field(min_length=1, max_length=3)
    fields: tuple[IntentVocabularyField, ...] = Field(min_length=1, max_length=12)
    joins: tuple[IntentVocabularyJoin, ...] = Field(default=(), max_length=2)
    metric_operations: tuple[MetricOperation, ...] = Field(min_length=1)
    filter_operators: tuple[FilterOperator, ...] = Field(min_length=1)
    date_grains: tuple[DateGrain, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def vocabulary_must_be_unique_and_connected(self) -> IntentVocabulary:
        model_ids = [model.id.root for model in self.models]
        field_ids = [field.id.root for field in self.fields]
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("intent vocabulary models must be unique")
        if len(field_ids) != len(set(field_ids)):
            raise ValueError("intent vocabulary fields must be unique")
        if any(field.id.root.split(".", 1)[0] not in model_ids for field in self.fields):
            raise ValueError("intent vocabulary fields must belong to an included model")
        if any(
            join.left_model.root not in model_ids or join.right_model.root not in model_ids
            for join in self.joins
        ):
            raise ValueError("intent vocabulary joins must connect included models")
        return self

    def field_index(self) -> dict[str, IntentVocabularyField]:
        return {field.id.root: field for field in self.fields}


class IntentParseInput(FrozenDomainModel):
    text: str = Field(min_length=1, max_length=2_000)
    language: UserLanguage
    vocabulary: IntentVocabulary

    @field_validator("text")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("business request must not be blank")
        return value


class IntentModelOutput(FrozenDomainModel):
    """Only structured intent can cross the model boundary; no SQL field exists."""

    language: UserLanguage
    request: AnalyticalRequest | None
    ambiguities: tuple[IntentAmbiguityKind, ...]

    @model_validator(mode="after")
    def output_must_contain_intent_or_ambiguity(self) -> IntentModelOutput:
        if self.request is None and not self.ambiguities:
            raise ValueError("intent output requires a request or an explicit ambiguity")
        if len(self.ambiguities) != len(set(self.ambiguities)):
            raise ValueError("intent ambiguities must be unique")
        return self


class IntentConfirmation(FrozenDomainModel):
    """Explicit choice bound to the exact interpretation shown to the user."""

    interpretation_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_alternative: IntentAlternativeId


def intent_vocabulary_fingerprint(vocabulary: IntentVocabulary) -> str:
    return _fingerprint(vocabulary.model_dump(mode="json"))


def intent_interpretation_fingerprint(
    output: IntentModelOutput,
    vocabulary: IntentVocabulary,
) -> str:
    return _fingerprint(
        {
            "output": output.model_dump(mode="json"),
            "vocabulary_fingerprint": intent_vocabulary_fingerprint(vocabulary),
        }
    )


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
