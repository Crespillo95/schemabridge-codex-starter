"""Pure, immutable contracts for validated query-recipe reuse."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.concepts import LogicalFieldRef
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.requests import AnalyticalRequest

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RECIPE_ID = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_COMPILER_VERSION = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")


class RecipePublicationConfirmation(StrEnum):
    PUBLISH_VALIDATED_QUERY_RECIPE = "PUBLISH VALIDATED QUERY RECIPE"


class RecipePublicationStatus(StrEnum):
    CREATED = "created"
    ALREADY_CURRENT = "already_current"
    PARTIAL_FAILURE = "partial_failure"


class RecipeReuseStatus(StrEnum):
    NOT_FOUND = "not_found"
    REUSABLE = "reusable"
    STALE = "stale"


class RecipeStalenessCode(StrEnum):
    MODEL_CONTEXT_CHANGED = "model_context_changed"
    MAPPING_VERSION_CHANGED = "mapping_version_changed"
    JOIN_VERSION_CHANGED = "join_version_changed"
    SOURCE_SCHEMA_CHANGED = "source_schema_changed"
    COMPILER_VERSION_CHANGED = "compiler_version_changed"
    PLAN_CHANGED = "plan_changed"
    QUERY_CHANGED = "query_changed"


class RecipeModelVersion(FrozenDomainModel):
    source: str = Field(min_length=1, max_length=160)
    version: int = Field(ge=1)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class RecipeMappingVersion(FrozenDomainModel):
    logical_field: LogicalFieldRef
    physical_field: PhysicalFieldRef
    version: int = Field(ge=1)
    approval_decision_id: str = Field(min_length=1, max_length=200)


class RecipeJoinVersion(FrozenDomainModel):
    contract_id: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    version: int = Field(ge=1)
    approval_decision_id: str = Field(min_length=1, max_length=200)


class RecipeValidationSummary(FrozenDomainModel):
    policy_status: Literal["accepted"] = "accepted"
    executed_at: datetime
    database_user: str = Field(min_length=1, max_length=120)
    transaction_read_only: Literal[True]
    statement_timeout_ms: int = Field(ge=1, le=60_000)
    row_count: int = Field(ge=0, le=10_000)
    rejected_count: int = Field(ge=0, le=10_000)
    rejection_codes: tuple[str, ...] = ()
    truncated: bool
    preview_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def rejection_counts_match(self) -> RecipeValidationSummary:
        if self.rejected_count != len(self.rejection_codes):
            raise ValueError("recipe rejection count must match its stable codes")
        return self


class QueryRecipe(FrozenDomainModel):
    """Versioned context only; it deliberately contains no executable SQL."""

    id: str
    version: int = Field(ge=1)
    business_question: str = Field(min_length=1, max_length=2_000)
    normalized_intent: AnalyticalRequest
    intent_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    validated_request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_version: RecipeModelVersion
    mapping_versions: tuple[RecipeMappingVersion, ...] = Field(min_length=1)
    join_versions: tuple[RecipeJoinVersion, ...] = Field(default=(), max_length=2)
    source_schema_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    query_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    compiler_version: str
    validation: RecipeValidationSummary
    limitations: tuple[str, ...] = Field(min_length=1, max_length=20)
    linked_asset_urns: tuple[str, ...] = Field(min_length=1, max_length=3)
    source_workflow_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    created_at: datetime
    content_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("id")
    @classmethod
    def id_must_be_inert(cls, value: str) -> str:
        if _RECIPE_ID.fullmatch(value) is None:
            raise ValueError("query recipe id must be inert")
        return value

    @field_validator("compiler_version")
    @classmethod
    def compiler_version_must_be_inert(cls, value: str) -> str:
        if _COMPILER_VERSION.fullmatch(value) is None:
            raise ValueError("query recipe compiler version must be inert")
        return value

    @field_validator("business_question")
    @classmethod
    def business_question_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query recipe business question must not be blank")
        return value

    @field_validator("limitations", "linked_asset_urns")
    @classmethod
    def entries_must_be_unique_and_nonblank(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(not value.strip() for value in values):
            raise ValueError("query recipe entries must be unique and nonblank")
        return values

    @model_validator(mode="after")
    def fingerprints_and_versions_are_consistent(self) -> QueryRecipe:
        mappings = tuple(
            (item.logical_field.root, item.physical_field.root, item.version)
            for item in self.mapping_versions
        )
        joins = tuple((item.contract_id, item.version) for item in self.join_versions)
        if mappings != tuple(sorted(mappings)) or len(mappings) != len(set(mappings)):
            raise ValueError("query recipe mapping versions must be sorted and unique")
        if joins != tuple(sorted(joins)) or len(joins) != len(set(joins)):
            raise ValueError("query recipe join versions must be sorted and unique")
        if self.linked_asset_urns != tuple(sorted(self.linked_asset_urns)):
            raise ValueError("query recipe linked assets must be sorted")
        expected_intent = fingerprint_recipe_payload(self.normalized_intent.model_dump(mode="json"))
        if self.intent_fingerprint != expected_intent:
            raise ValueError("query recipe intent fingerprint does not match")
        expected_content = fingerprint_recipe_payload(_content_payload(self))
        if self.content_fingerprint != expected_content:
            raise ValueError("query recipe content fingerprint does not match")
        expected_fingerprint = fingerprint_recipe_payload(_identity_payload(self))
        if self.fingerprint != expected_fingerprint:
            raise ValueError("query recipe fingerprint does not match")
        return self

    @classmethod
    def create(
        cls,
        *,
        version: int,
        business_question: str,
        normalized_intent: AnalyticalRequest,
        validated_request_fingerprint: str,
        model_version: RecipeModelVersion,
        mapping_versions: tuple[RecipeMappingVersion, ...],
        join_versions: tuple[RecipeJoinVersion, ...],
        source_schema_fingerprint: str,
        plan_fingerprint: str,
        query_fingerprint: str,
        compiler_version: str,
        validation: RecipeValidationSummary,
        limitations: tuple[str, ...],
        linked_asset_urns: tuple[str, ...],
        source_workflow_id: str,
        created_at: datetime,
    ) -> QueryRecipe:
        intent_fingerprint = fingerprint_recipe_payload(normalized_intent.model_dump(mode="json"))
        values: dict[str, object] = {
            "id": f"query_recipe_{intent_fingerprint[:16]}",
            "version": version,
            "business_question": business_question,
            "normalized_intent": normalized_intent,
            "intent_fingerprint": intent_fingerprint,
            "validated_request_fingerprint": validated_request_fingerprint,
            "model_version": model_version,
            "mapping_versions": tuple(
                sorted(
                    mapping_versions,
                    key=lambda item: (
                        item.logical_field.root,
                        item.physical_field.root,
                        item.version,
                    ),
                )
            ),
            "join_versions": tuple(
                sorted(join_versions, key=lambda item: (item.contract_id, item.version))
            ),
            "source_schema_fingerprint": source_schema_fingerprint,
            "plan_fingerprint": plan_fingerprint,
            "query_fingerprint": query_fingerprint,
            "compiler_version": compiler_version,
            "validation": validation,
            "limitations": limitations,
            "linked_asset_urns": tuple(sorted(linked_asset_urns)),
            "source_workflow_id": source_workflow_id,
            "created_at": created_at,
        }
        construct: Any = cls.model_construct
        unchecked = construct(**values, content_fingerprint="", fingerprint="")
        values["content_fingerprint"] = fingerprint_recipe_payload(_content_payload(unchecked))
        with_content = construct(**values, fingerprint="")
        values["fingerprint"] = fingerprint_recipe_payload(_identity_payload(with_content))
        return cls.model_validate(values)


class RecipePublicationApproval(FrozenDomainModel):
    id: str = Field(min_length=1, max_length=200)
    recipe_id: str = Field(min_length=1, max_length=64)
    recipe_version: int = Field(ge=1)
    recipe_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    actor: str = Field(min_length=1, max_length=120)
    approved_at: datetime
    confirmation: RecipePublicationConfirmation


class PublishedQueryRecipe(FrozenDomainModel):
    recipe: QueryRecipe
    document_urn: str = Field(min_length=1, max_length=500)
    versioned_document_urn: str = Field(min_length=1, max_length=500)
    approval_id: str = Field(min_length=1, max_length=200)
    published_at: datetime


class RecipePublicationResult(FrozenDomainModel):
    status: RecipePublicationStatus
    recipe_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_document_urn: str = Field(min_length=1, max_length=500)
    versioned_document_urn: str = Field(min_length=1, max_length=500)
    published_at: datetime
    failure_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,63}$")

    @model_validator(mode="after")
    def failure_matches_status(self) -> RecipePublicationResult:
        partial = self.status is RecipePublicationStatus.PARTIAL_FAILURE
        if partial != (self.failure_code is not None):
            raise ValueError("partial recipe publication requires one stable failure code")
        return self


class RecipeReuseAssessment(FrozenDomainModel):
    status: RecipeReuseStatus
    intent_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    recipe_id: str | None = None
    recipe_version: int | None = Field(default=None, ge=1)
    recipe_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    provenance_document_urn: str | None = None
    reasons: tuple[RecipeStalenessCode, ...] = ()
    revalidated: bool = True

    @model_validator(mode="after")
    def state_matches_status(self) -> RecipeReuseAssessment:
        references = (
            self.recipe_id,
            self.recipe_version,
            self.recipe_fingerprint,
            self.provenance_document_urn,
        )
        if self.status is RecipeReuseStatus.NOT_FOUND:
            if any(value is not None for value in references) or self.reasons:
                raise ValueError("missing recipe assessment cannot claim provenance or staleness")
        elif any(value is None for value in references):
            raise ValueError("found recipe assessment requires complete provenance")
        if self.status is RecipeReuseStatus.REUSABLE and self.reasons:
            raise ValueError("reusable recipe assessment cannot carry stale reasons")
        if self.status is RecipeReuseStatus.STALE and not self.reasons:
            raise ValueError("stale recipe assessment requires at least one reason")
        if not self.revalidated:
            raise ValueError("recipe reuse must retain deterministic planning and SQL validation")
        return self


def fingerprint_recipe_payload(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _content_payload(recipe: QueryRecipe) -> dict[str, object]:
    return {
        "business_question": recipe.business_question,
        "normalized_intent": recipe.normalized_intent.model_dump(mode="json"),
        "intent_fingerprint": recipe.intent_fingerprint,
        "validated_request_fingerprint": recipe.validated_request_fingerprint,
        "model_version": recipe.model_version.model_dump(mode="json"),
        "mapping_versions": [item.model_dump(mode="json") for item in recipe.mapping_versions],
        "join_versions": [item.model_dump(mode="json") for item in recipe.join_versions],
        "source_schema_fingerprint": recipe.source_schema_fingerprint,
        "plan_fingerprint": recipe.plan_fingerprint,
        "query_fingerprint": recipe.query_fingerprint,
        "compiler_version": recipe.compiler_version,
        "validation": recipe.validation.model_dump(mode="json"),
        "limitations": recipe.limitations,
        "linked_asset_urns": recipe.linked_asset_urns,
    }


def _identity_payload(recipe: QueryRecipe) -> dict[str, object]:
    return {
        "id": recipe.id,
        "version": recipe.version,
        "content_fingerprint": recipe.content_fingerprint,
        "source_workflow_id": recipe.source_workflow_id,
        "created_at": recipe.created_at.isoformat(),
    }


def assert_recipe_sha256(value: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError("recipe fingerprint must be lowercase SHA-256")
    return value
