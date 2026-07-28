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
from schemabridge.domain.publication_audit import (
    PublicationAuditOutcome,
    PublicationFamily,
    PublicationTargetAuditRecord,
)
from schemabridge.domain.requests import AnalyticalRequest
from schemabridge.domain.resolution import MAX_REJECTED_SOURCE_TOTAL

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
    ACTIVE_REGISTRY_CHANGED = "active_registry_changed"
    MODEL_CONTEXT_CHANGED = "model_context_changed"
    MAPPING_VERSION_CHANGED = "mapping_version_changed"
    JOIN_VERSION_CHANGED = "join_version_changed"
    SOURCE_SCHEMA_CHANGED = "source_schema_changed"
    COMPILER_VERSION_CHANGED = "compiler_version_changed"
    PLAN_CHANGED = "plan_changed"
    QUERY_CHANGED = "query_changed"
    SEMANTIC_EVIDENCE_CHANGED = "semantic_evidence_changed"


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


class RecipeRegistryBinding(FrozenDomainModel):
    """Authoritative active-registry identity used by one completed workflow."""

    generation: int = Field(ge=1)
    pointer_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    registry_version: int = Field(ge=1)
    registry_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class RecipeMigrationProvenance(FrozenDomainModel):
    """Immutable reference to the historical recipe replaced by a migration."""

    previous_recipe_id: str
    previous_recipe_version: int = Field(ge=1)
    previous_recipe_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_recipe_payload_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_source_workflow_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    previous_versioned_document_urn: str = Field(min_length=1, max_length=500)
    staleness_reasons: tuple[RecipeStalenessCode, ...] = Field(min_length=1)

    @field_validator("previous_recipe_id")
    @classmethod
    def previous_recipe_id_must_be_inert(cls, value: str) -> str:
        if _RECIPE_ID.fullmatch(value) is None:
            raise ValueError("previous query recipe id must be inert")
        return value

    @field_validator("staleness_reasons")
    @classmethod
    def staleness_reasons_must_be_canonical(
        cls,
        values: tuple[RecipeStalenessCode, ...],
    ) -> tuple[RecipeStalenessCode, ...]:
        if len(values) != len(set(values)):
            raise ValueError("recipe migration staleness reasons must be unique")
        return tuple(sorted(values, key=lambda value: value.value))


class RecipeValidationSummary(FrozenDomainModel):
    policy_status: Literal["accepted"] = "accepted"
    executed_at: datetime
    database_user: str = Field(min_length=1, max_length=120)
    transaction_read_only: Literal[True]
    statement_timeout_ms: int = Field(ge=1, le=60_000)
    row_count: int = Field(ge=0, le=10_000)
    rejected_count: int = Field(ge=0, le=MAX_REJECTED_SOURCE_TOTAL)
    rejection_codes: tuple[str, ...] = ()
    truncated: bool
    preview_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def rejection_counts_match(self) -> RecipeValidationSummary:
        if self.sampled_rejected_count > self.rejected_count:
            raise ValueError("recipe rejection sample exceeds its exact total")
        if self.rejection_truncated and not self.truncated:
            raise ValueError("truncated recipe rejection counts require a truncated summary")
        return self

    @property
    def sampled_rejected_count(self) -> int:
        """Return the number of rejection records represented by stable sample codes."""

        return len(self.rejection_codes)

    @property
    def unclassified_rejection_count(self) -> int:
        """Return the exact residual omitted by bounded rejection inspection."""

        return self.rejected_count - self.sampled_rejected_count

    @property
    def rejection_counts_complete(self) -> bool:
        """Return whether stable codes cover the complete rejected population."""

        return self.unclassified_rejection_count == 0

    @property
    def rejection_truncated(self) -> bool:
        """Return whether stable rejection codes came from a bounded sample."""

        return not self.rejection_counts_complete


class QueryRecipe(FrozenDomainModel):
    """Versioned context only; it deliberately contains no executable SQL."""

    id: str
    version: int = Field(ge=1)
    business_question: str = Field(min_length=1, max_length=2_000)
    normalized_intent: AnalyticalRequest
    intent_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    validated_request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_version: RecipeModelVersion
    registry_binding: RecipeRegistryBinding | None = None
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
    migration_provenance: RecipeMigrationProvenance | None = None
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
        if self.registry_binding is not None and (
            self.registry_binding.registry_version != self.model_version.version
            or self.registry_binding.registry_fingerprint != self.model_version.fingerprint
        ):
            raise ValueError("query recipe registry binding must match its model version")
        if self.migration_provenance is not None:
            previous = self.migration_provenance
            if (
                self.registry_binding is None
                or previous.previous_recipe_id != self.id
                or self.version != previous.previous_recipe_version + 1
                or self.source_workflow_id == previous.previous_source_workflow_id
            ):
                raise ValueError(
                    "migrated query recipe must advance its exact historical identity "
                    "from a new active workflow"
                )
        expected_intent = fingerprint_recipe_payload(self.normalized_intent.model_dump(mode="json"))
        if self.intent_fingerprint != expected_intent:
            raise ValueError("query recipe intent fingerprint does not match")
        expected_content = fingerprint_recipe_payload(_content_payload(self))
        if self.content_fingerprint != expected_content:
            raise ValueError("query recipe content fingerprint does not match")
        expected_fingerprint = fingerprint_recipe_payload(_identity_payload(self))
        if self.fingerprint != expected_fingerprint:
            raise ValueError("query recipe fingerprint does not match")
        if (
            self.migration_provenance is not None
            and self.fingerprint == self.migration_provenance.previous_recipe_fingerprint
        ):
            raise ValueError("migrated query recipe must derive a new fingerprint")
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
        registry_binding: RecipeRegistryBinding | None = None,
        migration_provenance: RecipeMigrationProvenance | None = None,
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
            "registry_binding": registry_binding,
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
            "migration_provenance": migration_provenance,
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

    @field_validator("id", "actor")
    @classmethod
    def approval_text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("recipe publication approval text must not be blank")
        return value

    @field_validator("approved_at")
    @classmethod
    def approval_time_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("recipe publication approval time must include a timezone")
        return value


class PublishedQueryRecipe(FrozenDomainModel):
    recipe: QueryRecipe
    document_urn: str = Field(min_length=1, max_length=500)
    versioned_document_urn: str = Field(min_length=1, max_length=500)
    approval_id: str = Field(min_length=1, max_length=200)
    published_at: datetime

    @field_validator("published_at")
    @classmethod
    def publication_time_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("query recipe publication time must include a timezone")
        return value


class RecipePublicationResult(FrozenDomainModel):
    status: RecipePublicationStatus
    approval_id: str = Field(min_length=1, max_length=200)
    recipe_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_document_urn: str = Field(min_length=1, max_length=500)
    versioned_document_urn: str = Field(min_length=1, max_length=500)
    published_at: datetime
    failure_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,63}$")
    audit_records: tuple[PublicationTargetAuditRecord, ...] = Field(min_length=2, max_length=2)

    @field_validator("published_at")
    @classmethod
    def publication_time_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("query recipe result time must include a timezone")
        return value

    @model_validator(mode="after")
    def failure_matches_status(self) -> RecipePublicationResult:
        partial = self.status is RecipePublicationStatus.PARTIAL_FAILURE
        if partial != (self.failure_code is not None):
            raise ValueError("partial recipe publication requires one stable failure code")
        by_operation = {record.operation: record for record in self.audit_records}
        if set(by_operation) != {"versioned_document", "current_marker"}:
            raise ValueError("recipe publication requires one audit record for each target")
        expected_targets = {
            "versioned_document": self.versioned_document_urn,
            "current_marker": self.current_document_urn,
        }
        if any(
            record.family is not PublicationFamily.RECIPE
            or record.approval_id != self.approval_id
            or record.target != expected_targets[record.operation]
            or record.new_fingerprint != self.recipe_fingerprint
            for record in self.audit_records
        ):
            raise ValueError("recipe publication audits do not match their approved targets")
        outcomes = {record.outcome for record in self.audit_records}
        if self.status is RecipePublicationStatus.ALREADY_CURRENT:
            if outcomes != {PublicationAuditOutcome.ALREADY_CURRENT}:
                raise ValueError("current recipe publication requires current target audits")
        elif self.status is RecipePublicationStatus.CREATED:
            if (
                not outcomes
                <= {PublicationAuditOutcome.SUCCEEDED, PublicationAuditOutcome.ALREADY_CURRENT}
                or PublicationAuditOutcome.SUCCEEDED not in outcomes
            ):
                raise ValueError("created recipe publication requires successful target audits")
        elif not outcomes & {
            PublicationAuditOutcome.FAILED,
            PublicationAuditOutcome.NOT_ATTEMPTED,
        }:
            raise ValueError("partial recipe publication requires a failed target audit")
        if self.status is RecipePublicationStatus.PARTIAL_FAILURE:
            failed_reasons = {
                record.reason_code
                for record in self.audit_records
                if record.outcome is PublicationAuditOutcome.FAILED
            }
            if self.failure_code not in failed_reasons:
                raise ValueError("recipe failure code must match the failed target audit")
        if len({(record.actor, record.approved_at) for record in self.audit_records}) != 1:
            raise ValueError("recipe publication audits must share one approver and timestamp")
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


class RecipeMigrationProposal(FrozenDomainModel):
    """Exact stale-recipe replacement prepared before a separate approval."""

    historical: PublishedQueryRecipe
    historical_payload_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    historical_snapshot_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    assessment: RecipeReuseAssessment
    replacement: QueryRecipe
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def replacement_must_advance_the_exact_stale_recipe(self) -> RecipeMigrationProposal:
        historical = self.historical.recipe
        provenance = self.replacement.migration_provenance
        if self.historical_payload_fingerprint != query_recipe_payload_fingerprint(
            historical
        ) or self.historical_snapshot_fingerprint != published_recipe_snapshot_fingerprint(
            self.historical
        ):
            raise ValueError("recipe migration historical payload fingerprint does not match")
        if (
            self.assessment.status is not RecipeReuseStatus.STALE
            or self.assessment.intent_fingerprint != historical.intent_fingerprint
            or self.assessment.recipe_id != historical.id
            or self.assessment.recipe_version != historical.version
            or self.assessment.recipe_fingerprint != historical.fingerprint
            or self.assessment.provenance_document_urn != self.historical.versioned_document_urn
        ):
            raise ValueError("recipe migration assessment does not identify the historical recipe")
        if (
            provenance is None
            or provenance.previous_recipe_id != historical.id
            or provenance.previous_recipe_version != historical.version
            or provenance.previous_recipe_fingerprint != historical.fingerprint
            or provenance.previous_recipe_payload_fingerprint != self.historical_payload_fingerprint
            or provenance.previous_source_workflow_id != historical.source_workflow_id
            or provenance.previous_versioned_document_urn != self.historical.versioned_document_urn
            or provenance.staleness_reasons
            != tuple(sorted(self.assessment.reasons, key=lambda value: value.value))
        ):
            raise ValueError("recipe migration replacement lacks exact historical provenance")
        if (
            self.replacement.intent_fingerprint != historical.intent_fingerprint
            or self.replacement.fingerprint == historical.fingerprint
        ):
            raise ValueError("recipe migration replacement must retain intent and derive identity")
        if self.fingerprint != fingerprint_recipe_payload(_migration_proposal_payload(self)):
            raise ValueError("recipe migration proposal fingerprint does not match")
        return self

    @classmethod
    def create(
        cls,
        *,
        historical: PublishedQueryRecipe,
        assessment: RecipeReuseAssessment,
        replacement: QueryRecipe,
    ) -> RecipeMigrationProposal:
        values: dict[str, object] = {
            "historical": historical,
            "historical_payload_fingerprint": query_recipe_payload_fingerprint(historical.recipe),
            "historical_snapshot_fingerprint": published_recipe_snapshot_fingerprint(historical),
            "assessment": assessment,
            "replacement": replacement,
        }
        construct: Any = cls.model_construct
        unchecked = construct(**values, fingerprint="")
        values["fingerprint"] = fingerprint_recipe_payload(_migration_proposal_payload(unchecked))
        return cls.model_validate(values)


def fingerprint_recipe_payload(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def query_recipe_payload_fingerprint(recipe: QueryRecipe) -> str:
    """Fingerprint the canonical serialized recipe bytes without reusing its identity hash."""

    return hashlib.sha256(recipe.model_dump_json().encode()).hexdigest()


def published_recipe_snapshot_fingerprint(published: PublishedQueryRecipe) -> str:
    """Fingerprint the complete canonical current-marker snapshot observed by the operator."""

    return hashlib.sha256(published.model_dump_json().encode()).hexdigest()


def _content_payload(recipe: QueryRecipe) -> dict[str, object]:
    payload: dict[str, object] = {
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
    if recipe.registry_binding is not None:
        payload["registry_binding"] = recipe.registry_binding.model_dump(mode="json")
    if recipe.migration_provenance is not None:
        payload["migration_provenance"] = recipe.migration_provenance.model_dump(mode="json")
    return payload


def _identity_payload(recipe: QueryRecipe) -> dict[str, object]:
    return {
        "id": recipe.id,
        "version": recipe.version,
        "content_fingerprint": recipe.content_fingerprint,
        "source_workflow_id": recipe.source_workflow_id,
        "created_at": recipe.created_at.isoformat(),
    }


def _migration_proposal_payload(proposal: RecipeMigrationProposal) -> dict[str, object]:
    return {
        "historical": proposal.historical.model_dump(mode="json"),
        "historical_payload_fingerprint": proposal.historical_payload_fingerprint,
        "historical_snapshot_fingerprint": proposal.historical_snapshot_fingerprint,
        "assessment": proposal.assessment.model_dump(mode="json"),
        "replacement": proposal.replacement.model_dump(mode="json"),
    }


def assert_recipe_sha256(value: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError("recipe fingerprint must be lowercase SHA-256")
    return value
