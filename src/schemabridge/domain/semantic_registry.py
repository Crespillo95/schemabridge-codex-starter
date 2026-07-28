"""Pure, atomic governed semantic-registry contracts."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.decisions import ApprovalStatus
from schemabridge.domain.joins import JoinContract
from schemabridge.domain.mappings import ColumnMapping
from schemabridge.domain.publication_audit import (
    PublicationAuditOutcome,
    PublicationFamily,
    PublicationTargetAuditRecord,
)
from schemabridge.domain.request_context import ApprovedLogicalContext

_SOURCE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*:[A-Za-z0-9._/-]+$")
_INERT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,79}$")
_SCOPE_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{2,119}$")
_MAX_MODELS = 100
_MAX_FIELDS = 1_000
_MAX_MAPPINGS = 2_000
_MAX_CONTRACTS = 500
_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class RegistryPublicationConfirmation(StrEnum):
    PUBLISH_APPROVED_REGISTRY_VERSION = "publish-approved-registry-version"


class RegistryPublicationStatus(StrEnum):
    PUBLISHED = "published"
    ALREADY_CURRENT = "already_current"
    FAILED = "failed"


class PhysicalValueType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"
    BINARY = "binary"
    STRUCT = "struct"
    ARRAY = "array"
    UNKNOWN = "unknown"


class RegistryArtifactKind(StrEnum):
    LOGICAL_MODELS = "logical_models"
    PHYSICAL_MAPPINGS = "physical_mappings"
    JOIN_CONTRACTS = "join_contracts"


class RegistryArtifactProvenance(FrozenDomainModel):
    kind: RegistryArtifactKind
    source: str = Field(min_length=1, max_length=240)
    decision_ids: tuple[str, ...] = Field(min_length=1, max_length=2_000)

    @field_validator("source")
    @classmethod
    def source_must_be_explicit(cls, value: str) -> str:
        if _SOURCE_PATTERN.fullmatch(value) is None:
            raise ValueError("registry artifact source must be an explicit kind:location label")
        return value

    @field_validator("decision_ids")
    @classmethod
    def decisions_must_be_unique_and_nonblank(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(not value.strip() for value in values):
            raise ValueError("registry provenance decisions must be unique and nonblank")
        return values


class GovernedFieldMapping(FrozenDomainModel):
    mapping: ColumnMapping
    physical_type: PhysicalValueType
    logical_field_version: int = Field(ge=1)
    approval_decision_id: str | None = None

    @model_validator(mode="after")
    def approval_reference_must_match_status(self) -> GovernedFieldMapping:
        approved = self.mapping.status is ApprovalStatus.APPROVED
        if approved != (self.approval_decision_id is not None):
            raise ValueError("approved mapping status must match its decision reference")
        if self.approval_decision_id is not None and not self.approval_decision_id.strip():
            raise ValueError("mapping approval decision id must not be blank")
        return self


class GovernedMappingRegistry(FrozenDomainModel):
    version: int = Field(ge=1)
    mappings: tuple[GovernedFieldMapping, ...] = Field(
        min_length=1,
        max_length=_MAX_MAPPINGS,
    )

    @model_validator(mode="after")
    def mappings_must_be_unique(self) -> GovernedMappingRegistry:
        pairs = [
            (item.mapping.logical_field.root, item.mapping.physical_field.root)
            for item in self.mappings
        ]
        if len(pairs) != len(set(pairs)):
            raise ValueError("governed mapping pairs must be unique")
        decisions = [
            item.approval_decision_id
            for item in self.mappings
            if item.approval_decision_id is not None
        ]
        if len(decisions) != len(set(decisions)):
            raise ValueError("mapping approval decisions must be unique")
        return self


class GovernedJoinRegistry(FrozenDomainModel):
    """Registry-wide contracts; executable slices retain the two-join query limit."""

    version: int = Field(ge=1)
    contracts: tuple[JoinContract, ...] = Field(default=(), max_length=_MAX_CONTRACTS)

    @model_validator(mode="after")
    def contract_identities_must_be_unique(self) -> GovernedJoinRegistry:
        identifiers = tuple(contract.id for contract in self.contracts)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("governed registry join contract ids must be unique")
        return self


class SemanticRegistryScope(FrozenDomainModel):
    """Runtime binding used to prevent an unscoped global registry read."""

    workspace_id: str = Field(min_length=1, max_length=200)
    catalog_scope: str = Field(min_length=3, max_length=120)
    registry_id: str = Field(min_length=3, max_length=80)

    @field_validator("workspace_id")
    @classmethod
    def workspace_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("semantic registry workspace must not be blank")
        return value

    @field_validator("catalog_scope")
    @classmethod
    def catalog_scope_must_be_inert(cls, value: str) -> str:
        if _SCOPE_PATTERN.fullmatch(value) is None:
            raise ValueError("semantic registry catalog scope must be inert")
        return value

    @field_validator("registry_id")
    @classmethod
    def registry_id_must_be_inert(cls, value: str) -> str:
        if _INERT_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("semantic registry id must be an inert identifier")
        return value


def semantic_registry_scope_fingerprint(scope: SemanticRegistryScope) -> str:
    """Return the stable opaque identity for one tenant/catalog/registry scope."""

    if not isinstance(scope, SemanticRegistryScope):
        raise ValueError("semantic registry scope is invalid")
    payload = json.dumps(
        scope.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


class GovernedSemanticRegistrySnapshot(FrozenDomainModel):
    """One complete approved registry revision loaded atomically."""

    format_version: int = Field(default=1, ge=1, le=1)
    registry_id: str = Field(min_length=3, max_length=80)
    version: int = Field(ge=1)
    source: str = Field(min_length=1, max_length=240)
    catalog_scope: str = Field(min_length=3, max_length=120)
    logical_context: ApprovedLogicalContext
    mapping_set: GovernedMappingRegistry
    join_contracts: GovernedJoinRegistry
    provenance: tuple[RegistryArtifactProvenance, ...] = Field(min_length=3, max_length=12)

    @field_validator("registry_id")
    @classmethod
    def registry_id_must_be_inert(cls, value: str) -> str:
        if _INERT_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("semantic registry id must be an inert identifier")
        return value

    @field_validator("source")
    @classmethod
    def source_must_be_explicit(cls, value: str) -> str:
        if _SOURCE_PATTERN.fullmatch(value) is None:
            raise ValueError("semantic registry source must be an explicit kind:location label")
        return value

    @field_validator("catalog_scope")
    @classmethod
    def catalog_scope_must_be_inert(cls, value: str) -> str:
        if _SCOPE_PATTERN.fullmatch(value) is None:
            raise ValueError("semantic registry catalog scope must be inert")
        return value

    @model_validator(mode="after")
    def active_registry_must_be_complete_and_unambiguous(
        self,
    ) -> GovernedSemanticRegistrySnapshot:
        models = self.logical_context.models
        fields = self.logical_context.field_index()
        mappings = self.mapping_set.mappings
        contracts = self.join_contracts.contracts
        if len(models) > _MAX_MODELS:
            raise ValueError("semantic registry exceeds the model limit")
        if len(fields) > _MAX_FIELDS:
            raise ValueError("semantic registry exceeds the field limit")

        mapped_fields: set[str] = set()
        physical_meanings: dict[str, str] = {}
        for governed in mappings:
            mapping = governed.mapping
            logical_id = mapping.logical_field.root
            definition = fields.get(logical_id)
            if definition is None:
                raise ValueError("semantic registry mapping references an unknown logical field")
            if mapping.status is not ApprovalStatus.APPROVED:
                raise ValueError("active semantic registry may contain only approved mappings")
            if governed.logical_field_version != definition.version:
                raise ValueError("semantic registry mapping is stale for its logical field")
            mapped_fields.add(logical_id)
            physical_id = mapping.physical_field.root
            prior = physical_meanings.setdefault(physical_id, logical_id)
            if prior != logical_id:
                raise ValueError(
                    "one physical field cannot have two active logical meanings in one registry"
                )
        if set(fields) - mapped_fields:
            raise ValueError("every active logical field requires an approved physical mapping")

        summaries = {summary.id: summary for summary in self.logical_context.joins}
        contract_index = {contract.id: contract for contract in contracts}
        if set(summaries) != set(contract_index):
            raise ValueError("logical join summaries and physical contracts must match exactly")
        mapping_index = {
            (
                item.mapping.logical_field.root,
                item.mapping.physical_field.root,
                _transformation_fingerprint(item.mapping),
            ): item
            for item in mappings
        }
        for contract in contracts:
            if contract.status is not ApprovalStatus.APPROVED:
                raise ValueError("active semantic registry may contain only approved joins")
            summary = summaries[contract.id]
            left_model = contract.left_key.logical_field.root.split(".", 1)[0]
            right_model = contract.right_key.logical_field.root.split(".", 1)[0]
            if (
                summary.version != contract.version
                or summary.left_model.root != left_model
                or summary.right_model.root != right_model
                or summary.cardinality is not contract.cardinality
                or summary.fanout_policy is not contract.fanout_policy
                or summary.approval_decision_id != contract.approval_decision_id
            ):
                raise ValueError(
                    "physical join contract does not match its approved logical summary"
                )
            for key in (contract.left_key, contract.right_key):
                identity = (
                    key.logical_field.root,
                    key.physical_field.root,
                    _plan_fingerprint(key.transformation_plan.model_dump(mode="json")),
                )
                if identity not in mapping_index:
                    raise ValueError(
                        "join key is not backed by the exact approved mapping transformation"
                    )

        provenance_kinds = tuple(item.kind for item in self.provenance)
        if set(provenance_kinds) != set(RegistryArtifactKind):
            raise ValueError("semantic registry provenance must cover every artifact family")
        if len(provenance_kinds) != len(set(provenance_kinds)):
            raise ValueError("semantic registry provenance artifact kinds must be unique")
        all_cited_decisions = tuple(
            decision for provenance in self.provenance for decision in provenance.decision_ids
        )
        if len(all_cited_decisions) != len(set(all_cited_decisions)):
            raise ValueError(
                "semantic registry provenance decisions must be unique across artifact families"
            )
        mapping_decisions = {
            item.approval_decision_id for item in mappings if item.approval_decision_id is not None
        }
        join_decisions = {
            contract.approval_decision_id
            for contract in contracts
            if contract.approval_decision_id is not None
        }
        provenance_by_kind = {item.kind: set(item.decision_ids) for item in self.provenance}
        if (
            provenance_by_kind[RegistryArtifactKind.PHYSICAL_MAPPINGS] != mapping_decisions
            or provenance_by_kind[RegistryArtifactKind.JOIN_CONTRACTS] != join_decisions
        ):
            raise ValueError(
                "semantic registry provenance must exactly match active mapping and join decisions"
            )
        return self

    @property
    def fingerprint(self) -> str:
        return governed_semantic_registry_fingerprint(self)


class ScopedSemanticRegistrySnapshot(FrozenDomainModel):
    scope: SemanticRegistryScope
    registry: GovernedSemanticRegistrySnapshot
    activation_generation: int | None = Field(default=None, ge=1)
    active_pointer_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def scope_must_match_registry(self) -> ScopedSemanticRegistrySnapshot:
        if self.scope.registry_id != self.registry.registry_id:
            raise ValueError("semantic registry scope selected a different registry")
        if self.scope.catalog_scope != self.registry.catalog_scope:
            raise ValueError("semantic registry scope selected a different catalog")
        if (self.activation_generation is None) != (self.active_pointer_fingerprint is None):
            raise ValueError(
                "active semantic registry snapshots require generation and pointer fingerprint"
            )
        return self

    @property
    def logical_context(self) -> ApprovedLogicalContext:
        """Compatibility view for readers migrating from the pre-M21 bundle."""

        return self.registry.logical_context

    @property
    def mapping_set(self) -> GovernedMappingRegistry:
        """Compatibility view for readers migrating from the pre-M21 bundle."""

        return self.registry.mapping_set

    @property
    def join_contracts(self) -> GovernedJoinRegistry:
        """Compatibility view for readers migrating from the pre-M21 bundle."""

        return self.registry.join_contracts

    @property
    def source(self) -> str:
        return self.registry.source

    @property
    def version(self) -> int:
        return self.registry.version


class RegistryPublicationApproval(FrozenDomainModel):
    """Explicit authorization for one immutable DataHub registry version."""

    id: str = Field(min_length=1, max_length=200)
    workspace_id: str = Field(min_length=1, max_length=200)
    registry_id: str = Field(min_length=3, max_length=80)
    registry_version: int = Field(ge=1)
    catalog_scope: str = Field(min_length=3, max_length=120)
    payload_fingerprint: str
    target: str = Field(min_length=1, max_length=500)
    actor: str = Field(min_length=1, max_length=120)
    approved_at: datetime
    decision_ids: tuple[str, ...] = Field(min_length=1, max_length=2_000)
    confirmation: RegistryPublicationConfirmation

    @field_validator("id", "workspace_id", "target", "actor")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("registry publication approval text must not be blank")
        return value

    @field_validator("registry_id")
    @classmethod
    def registry_id_must_be_inert(cls, value: str) -> str:
        if _INERT_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("registry publication id must be inert")
        return value

    @field_validator("catalog_scope")
    @classmethod
    def catalog_scope_must_be_inert(cls, value: str) -> str:
        if _SCOPE_PATTERN.fullmatch(value) is None:
            raise ValueError("registry publication catalog scope must be inert")
        return value

    @field_validator("payload_fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        if _FINGERPRINT_PATTERN.fullmatch(value) is None:
            raise ValueError("registry publication fingerprint must be lowercase SHA-256")
        return value

    @field_validator("approved_at")
    @classmethod
    def approval_time_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("registry publication approval time must include a timezone")
        return value

    @field_validator("decision_ids")
    @classmethod
    def decisions_must_be_unique_and_nonblank(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(not value.strip() for value in values):
            raise ValueError("registry publication decisions must be unique and nonblank")
        return values


class RegistryPublicationResult(FrozenDomainModel):
    approval_id: str = Field(min_length=1, max_length=200)
    workspace_id: str = Field(min_length=1, max_length=200)
    registry_id: str = Field(min_length=3, max_length=80)
    registry_version: int = Field(ge=1)
    fingerprint: str
    target: str = Field(min_length=1, max_length=500)
    status: RegistryPublicationStatus
    reason_code: str | None = Field(default=None, min_length=2, max_length=64)
    audit_record: PublicationTargetAuditRecord

    @field_validator("fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        if _FINGERPRINT_PATTERN.fullmatch(value) is None:
            raise ValueError("registry result fingerprint must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def audit_must_match_result(self) -> RegistryPublicationResult:
        expected_outcome = {
            RegistryPublicationStatus.PUBLISHED: PublicationAuditOutcome.SUCCEEDED,
            RegistryPublicationStatus.ALREADY_CURRENT: PublicationAuditOutcome.ALREADY_CURRENT,
            RegistryPublicationStatus.FAILED: PublicationAuditOutcome.FAILED,
        }[self.status]
        failed = self.status is RegistryPublicationStatus.FAILED
        if (
            failed != (self.reason_code is not None)
            or self.audit_record.family is not PublicationFamily.REGISTRY
            or self.audit_record.operation != "versioned_document"
            or self.audit_record.target != self.target
            or self.audit_record.approval_id != self.approval_id
            or self.audit_record.new_fingerprint != self.fingerprint
            or self.audit_record.outcome is not expected_outcome
            or self.audit_record.reason_code != self.reason_code
        ):
            raise ValueError("registry publication audit does not match its target result")
        return self

    @property
    def audit_records(self) -> tuple[PublicationTargetAuditRecord, ...]:
        return (self.audit_record,)


def semantic_registry_decision_ids(
    registry: GovernedSemanticRegistrySnapshot,
) -> tuple[str, ...]:
    """Return the complete deterministic decision closure carried by a registry."""

    return tuple(
        sorted(
            {
                decision_id
                for provenance in registry.provenance
                for decision_id in provenance.decision_ids
            }
        )
    )


def datahub_registry_document_id(
    scope: SemanticRegistryScope,
    version: int,
) -> str:
    """Return one workspace-bound inert immutable DataHub document identity."""

    if version < 1:
        raise ValueError("DataHub registry target requires a valid registry identity and version")
    workspace_digest = hashlib.sha256(scope.workspace_id.encode()).hexdigest()[:24]
    return f"schemabridge-semantic-registry-{scope.registry_id}-v{version}-w{workspace_digest}"


def datahub_registry_document_urn(
    scope: SemanticRegistryScope,
    version: int,
) -> str:
    return f"urn:li:document:{datahub_registry_document_id(scope, version)}"


def prepare_datahub_registry_version(
    registry: GovernedSemanticRegistrySnapshot,
    scope: SemanticRegistryScope,
) -> GovernedSemanticRegistrySnapshot:
    """Bind a complete approved snapshot to its immutable DataHub version target."""

    if scope.registry_id != registry.registry_id or scope.catalog_scope != registry.catalog_scope:
        raise ValueError("DataHub registry preparation scope does not match the payload")
    document_id = datahub_registry_document_id(scope, registry.version)
    source = f"datahub:{document_id}"
    provenance = tuple(
        RegistryArtifactProvenance.model_validate(
            {
                **item.model_dump(mode="python"),
                "source": f"{source}/{item.kind.value}",
            }
        )
        for item in registry.provenance
    )
    logical_context = ApprovedLogicalContext.model_validate(
        {
            **registry.logical_context.model_dump(mode="python"),
            "source": f"{source}/logical-context",
        }
    )
    return GovernedSemanticRegistrySnapshot.model_validate(
        {
            **registry.model_dump(mode="python"),
            "source": source,
            "logical_context": logical_context,
            "provenance": provenance,
        }
    )


def validate_registry_publication_approval(
    registry: GovernedSemanticRegistrySnapshot,
    approval: RegistryPublicationApproval,
) -> None:
    """Reject any approval not bound to the complete exact registry payload."""

    if not isinstance(registry, GovernedSemanticRegistrySnapshot) or not isinstance(
        approval,
        RegistryPublicationApproval,
    ):
        raise ValueError("explicit registry publication approval is required")
    try:
        validated_registry = GovernedSemanticRegistrySnapshot.model_validate(
            registry.model_dump(mode="python", warnings=False)
        )
        validated_approval = RegistryPublicationApproval.model_validate(
            approval.model_dump(mode="python", warnings=False)
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("registry publication contracts failed typed validation") from error
    if (
        validated_registry != registry
        or validated_approval != approval
        or validated_approval.confirmation
        is not RegistryPublicationConfirmation.PUBLISH_APPROVED_REGISTRY_VERSION
    ):
        raise ValueError("registry publication contracts failed typed validation")
    registry = validated_registry
    approval = validated_approval
    scope = SemanticRegistryScope(
        workspace_id=approval.workspace_id,
        catalog_scope=approval.catalog_scope,
        registry_id=approval.registry_id,
    )
    expected_source = f"datahub:{datahub_registry_document_id(scope, approval.registry_version)}"
    if (
        approval.id != registry_publication_approval_id(registry, scope, approval.actor)
        or approval.registry_id != registry.registry_id
        or approval.registry_version != registry.version
        or approval.catalog_scope != registry.catalog_scope
        or approval.payload_fingerprint != registry.fingerprint
        or approval.decision_ids != semantic_registry_decision_ids(registry)
        or approval.target != datahub_registry_document_urn(scope, approval.registry_version)
        or registry.source != expected_source
        or registry.logical_context.source != f"{expected_source}/logical-context"
        or any(
            provenance.source != f"{expected_source}/{provenance.kind.value}"
            for provenance in registry.provenance
        )
    ):
        raise ValueError("registry publication approval does not match the exact payload")


def governed_semantic_registry_fingerprint(
    registry: GovernedSemanticRegistrySnapshot,
) -> str:
    encoded = json.dumps(
        registry.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def registry_publication_approval_id(
    registry: GovernedSemanticRegistrySnapshot,
    scope: SemanticRegistryScope,
    actor: str,
) -> str:
    """Return the stable idempotency identity for one actor and exact registry payload."""

    digest = _plan_fingerprint(
        {
            "actor": actor,
            "workspace_id": scope.workspace_id,
            "registry_id": registry.registry_id,
            "registry_version": registry.version,
            "catalog_scope": scope.catalog_scope,
            "payload_fingerprint": registry.fingerprint,
        }
    )
    return f"registry-publication-v1-{digest}"


def _transformation_fingerprint(mapping: ColumnMapping) -> str:
    return _plan_fingerprint(mapping.transformation_plan.model_dump(mode="json"))


def _plan_fingerprint(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


# Compatibility names retained for stored payloads and downstream imports during M21 migration.
GovernedMappingSet = GovernedMappingRegistry
SemanticPlanningContext = GovernedSemanticRegistrySnapshot
