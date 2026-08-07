"""Pure contracts for generic registry assembly and exact publication authorization."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
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
from schemabridge.domain.request_context import (
    ApprovedLogicalContext,
    ApprovedRequestField,
    ApprovedRequestModel,
)
from schemabridge.domain.semantic_onboarding import (
    OnboardingRegistryBase,
    PreparedSemanticOnboardingProposal,
)
from schemabridge.domain.semantic_registry import (
    GovernedFieldMapping,
    GovernedJoinRegistry,
    GovernedMappingRegistry,
    GovernedPhysicalBinding,
    GovernedSemanticRegistrySnapshot,
    RegistryArtifactKind,
    RegistryArtifactProvenance,
    SemanticRegistryScope,
    datahub_registry_document_id,
    datahub_registry_document_urn,
    prepare_datahub_registry_version,
    semantic_registry_decision_ids,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_AUTHENTICATION_AGE = timedelta(minutes=15)
_MAX_AUTHORIZATION_LIFETIME = timedelta(minutes=15)


class RegistryPublicationAuthorizationConfirmation(StrEnum):
    PUBLISH_EXACT_OBSERVED_REGISTRY_VERSION = "publish-exact-observed-registry-version"


class PublishableRegistryVersion(FrozenDomainModel):
    """Complete v2 payload assembled before a human authorizes mutation."""

    id: str = Field(min_length=3, max_length=200)
    scope: SemanticRegistryScope
    source_proposal_id: str = Field(min_length=3, max_length=200)
    source_proposal_fingerprint: str
    base_registry: OnboardingRegistryBase
    registry: GovernedSemanticRegistrySnapshot
    target: str = Field(min_length=3, max_length=500)
    review_decision_ids: tuple[str, ...] = Field(min_length=2, max_length=2_001)
    active_decision_ids: tuple[str, ...] = Field(min_length=2, max_length=2_000)
    fingerprint: str

    @field_validator("id", "source_proposal_id", "target")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("publishable registry text must not be blank")
        return value

    @field_validator("source_proposal_fingerprint", "fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("publishable registry fingerprint must be lowercase SHA-256")
        return value

    @field_validator("review_decision_ids", "active_decision_ids")
    @classmethod
    def decisions_must_be_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))) or any(not value.strip() for value in values):
            raise ValueError("publishable registry decisions must be sorted and unique")
        return values

    @model_validator(mode="after")
    def candidate_must_bind_the_complete_exact_version(self) -> PublishableRegistryVersion:
        registry = self.registry
        if (
            self.id
            != registry_publication_candidate_id(
                self.source_proposal_id,
                self.source_proposal_fingerprint,
            )
            or registry.format_version != 2
            or registry.registry_id != self.scope.registry_id
            or registry.catalog_scope != self.scope.catalog_scope
            or registry.version != self.base_registry.next_registry_version
            or self.target != datahub_registry_document_urn(self.scope, registry.version)
            or self.active_decision_ids != semantic_registry_decision_ids(registry)
            or not set(self.active_decision_ids).issubset(set(self.review_decision_ids))
            or self.fingerprint
            != registry_publication_candidate_fingerprint(
                _candidate_identity_payload(
                    id=self.id,
                    scope=self.scope,
                    source_proposal_id=self.source_proposal_id,
                    source_proposal_fingerprint=self.source_proposal_fingerprint,
                    base_registry=self.base_registry,
                    registry_version=registry.version,
                    registry_fingerprint=registry.fingerprint,
                    target=self.target,
                    review_decision_ids=self.review_decision_ids,
                    active_decision_ids=self.active_decision_ids,
                )
            )
        ):
            raise ValueError("publishable registry candidate does not match its exact payload")
        expected_source = f"datahub:{datahub_registry_document_id(self.scope, registry.version)}"
        if (
            registry.source != expected_source
            or registry.logical_context.source != f"{expected_source}/logical-context"
            or any(
                item.source != f"{expected_source}/{item.kind.value}"
                for item in registry.provenance
            )
        ):
            raise ValueError("publishable registry is not bound to its exact DataHub target")
        return self

    @property
    def manifest(self) -> RegistryPublicationCandidateManifest:
        return RegistryPublicationCandidateManifest(
            id=self.id,
            scope=self.scope,
            source_proposal_id=self.source_proposal_id,
            source_proposal_fingerprint=self.source_proposal_fingerprint,
            base_registry=self.base_registry,
            registry_version=self.registry.version,
            registry_fingerprint=self.registry.fingerprint,
            target=self.target,
            review_decision_ids=self.review_decision_ids,
            active_decision_ids=self.active_decision_ids,
            fingerprint=self.fingerprint,
        )


class RegistryPublicationCandidateManifest(FrozenDomainModel):
    """Compact exact candidate identity retained beside the immutable registry document."""

    id: str = Field(min_length=3, max_length=200)
    scope: SemanticRegistryScope
    source_proposal_id: str = Field(min_length=3, max_length=200)
    source_proposal_fingerprint: str
    base_registry: OnboardingRegistryBase
    registry_version: int = Field(ge=1)
    registry_fingerprint: str
    target: str = Field(min_length=3, max_length=500)
    review_decision_ids: tuple[str, ...] = Field(min_length=2, max_length=2_001)
    active_decision_ids: tuple[str, ...] = Field(min_length=2, max_length=2_000)
    fingerprint: str

    @field_validator("source_proposal_fingerprint", "registry_fingerprint", "fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("registry candidate manifest fingerprint is invalid")
        return value

    @field_validator("review_decision_ids", "active_decision_ids")
    @classmethod
    def decisions_must_be_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))) or any(not value.strip() for value in values):
            raise ValueError("registry candidate manifest decisions are invalid")
        return values

    @model_validator(mode="after")
    def manifest_must_be_self_verifying(self) -> RegistryPublicationCandidateManifest:
        if (
            self.id
            != registry_publication_candidate_id(
                self.source_proposal_id,
                self.source_proposal_fingerprint,
            )
            or self.registry_version != self.base_registry.next_registry_version
            or self.target != datahub_registry_document_urn(self.scope, self.registry_version)
            or not set(self.active_decision_ids).issubset(set(self.review_decision_ids))
            or self.fingerprint
            != registry_publication_candidate_fingerprint(
                _candidate_identity_payload(
                    id=self.id,
                    scope=self.scope,
                    source_proposal_id=self.source_proposal_id,
                    source_proposal_fingerprint=self.source_proposal_fingerprint,
                    base_registry=self.base_registry,
                    registry_version=self.registry_version,
                    registry_fingerprint=self.registry_fingerprint,
                    target=self.target,
                    review_decision_ids=self.review_decision_ids,
                    active_decision_ids=self.active_decision_ids,
                )
            )
        ):
            raise ValueError("registry candidate manifest does not match its exact payload")
        return self


class RegistryPublicationAuthorization(FrozenDomainModel):
    """Fresh human authorization for one already assembled candidate."""

    id: str = Field(min_length=3, max_length=200)
    candidate_id: str = Field(min_length=3, max_length=200)
    candidate_fingerprint: str
    scope: SemanticRegistryScope
    source_proposal_id: str = Field(min_length=3, max_length=200)
    source_proposal_fingerprint: str
    registry_fingerprint: str
    target: str = Field(min_length=3, max_length=500)
    active_decision_ids: tuple[str, ...] = Field(min_length=2, max_length=2_000)
    actor_id: str = Field(min_length=1, max_length=200)
    authenticated_at: datetime
    approved_at: datetime
    expires_at: datetime
    confirmation: RegistryPublicationAuthorizationConfirmation

    @field_validator(
        "candidate_fingerprint",
        "source_proposal_fingerprint",
        "registry_fingerprint",
    )
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("registry authorization fingerprint must be lowercase SHA-256")
        return value

    @field_validator("id", "candidate_id", "source_proposal_id", "target", "actor_id")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("registry authorization text must not be blank")
        return value

    @field_validator("active_decision_ids")
    @classmethod
    def decisions_must_be_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))) or any(not value.strip() for value in values):
            raise ValueError("registry authorization decisions must be sorted and unique")
        return values

    @field_validator("authenticated_at", "approved_at", "expires_at")
    @classmethod
    def times_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("registry authorization timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def authorization_must_be_fresh_and_exact(self) -> RegistryPublicationAuthorization:
        if (
            not self.authenticated_at <= self.approved_at < self.expires_at
            or self.approved_at - self.authenticated_at > _MAX_AUTHENTICATION_AGE
            or self.expires_at - self.approved_at > _MAX_AUTHORIZATION_LIFETIME
            or self.confirmation
            is not RegistryPublicationAuthorizationConfirmation.PUBLISH_EXACT_OBSERVED_REGISTRY_VERSION
            or self.id != registry_publication_authorization_id(self)
        ):
            raise ValueError("registry publication authorization is stale or inconsistent")
        return self

    def is_current(self, at: datetime) -> bool:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("registry authorization check must include a timezone")
        return self.approved_at <= at < self.expires_at

    @classmethod
    def create(
        cls,
        candidate: PublishableRegistryVersion,
        *,
        actor_id: str,
        authenticated_at: datetime,
        approved_at: datetime,
        expires_at: datetime,
        confirmation: RegistryPublicationAuthorizationConfirmation,
    ) -> RegistryPublicationAuthorization:
        provisional = cls.model_construct(
            id="registry-authorization-placeholder",
            candidate_id=candidate.id,
            candidate_fingerprint=candidate.fingerprint,
            scope=candidate.scope,
            source_proposal_id=candidate.source_proposal_id,
            source_proposal_fingerprint=candidate.source_proposal_fingerprint,
            registry_fingerprint=candidate.registry.fingerprint,
            target=candidate.target,
            active_decision_ids=candidate.active_decision_ids,
            actor_id=actor_id,
            authenticated_at=authenticated_at,
            approved_at=approved_at,
            expires_at=expires_at,
            confirmation=confirmation,
        )
        return cls(
            id=registry_publication_authorization_id(provisional),
            candidate_id=candidate.id,
            candidate_fingerprint=candidate.fingerprint,
            scope=candidate.scope,
            source_proposal_id=candidate.source_proposal_id,
            source_proposal_fingerprint=candidate.source_proposal_fingerprint,
            registry_fingerprint=candidate.registry.fingerprint,
            target=candidate.target,
            active_decision_ids=candidate.active_decision_ids,
            actor_id=actor_id,
            authenticated_at=authenticated_at,
            approved_at=approved_at,
            expires_at=expires_at,
            confirmation=confirmation,
        )


class PublicationReadbackReceipt(FrozenDomainModel):
    """Exact successful DataHub state independently observed after publication."""

    candidate_id: str = Field(min_length=3, max_length=200)
    candidate_fingerprint: str
    scope: SemanticRegistryScope
    registry_version: int = Field(ge=1)
    registry_fingerprint: str
    target: str = Field(min_length=3, max_length=500)
    observed_authorization_id: str = Field(min_length=3, max_length=200)
    related_asset_urns: tuple[str, ...] = Field(min_length=1, max_length=256)
    observed_at: datetime

    @field_validator("candidate_fingerprint", "registry_fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("publication readback fingerprint is invalid")
        return value

    @field_validator("observed_at")
    @classmethod
    def observed_time_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("publication readback timestamp must include a timezone")
        return value

    @field_validator("related_asset_urns")
    @classmethod
    def related_assets_must_be_observed_and_canonical(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))) or any(
            not value.startswith("urn:li:dataset:") for value in values
        ):
            raise ValueError("publication readback related assets are invalid")
        return values

    @model_validator(mode="after")
    def receipt_must_match_scope_and_target(self) -> PublicationReadbackReceipt:
        if self.target != datahub_registry_document_urn(self.scope, self.registry_version):
            raise ValueError("publication readback target is outside its scope")
        return self


class ObservedRegistryPublicationResult(FrozenDomainModel):
    """One attempt outcome, distinguishing attempted from observed authorization."""

    attempt_authorization_id: str = Field(min_length=3, max_length=200)
    observed_authorization_id: str | None = Field(default=None, min_length=3, max_length=200)
    status: str = Field(pattern=r"^(published|already_current|failed)$")
    receipt: PublicationReadbackReceipt | None = None
    reason_code: str | None = Field(default=None, min_length=2, max_length=64)
    audit_record: PublicationTargetAuditRecord

    @model_validator(mode="after")
    def result_shape_must_match_status(self) -> ObservedRegistryPublicationResult:
        succeeded = self.status in {"published", "already_current"}
        expected_outcome = {
            "published": PublicationAuditOutcome.SUCCEEDED,
            "already_current": PublicationAuditOutcome.ALREADY_CURRENT,
            "failed": PublicationAuditOutcome.FAILED,
        }[self.status]
        if (
            succeeded != (self.receipt is not None)
            or succeeded != (self.observed_authorization_id is not None)
            or succeeded == (self.reason_code is not None)
            or (
                self.receipt is not None
                and self.receipt.observed_authorization_id != self.observed_authorization_id
            )
            or self.audit_record.approval_id != self.attempt_authorization_id
            or self.audit_record.reason_code != self.reason_code
            or self.audit_record.family is not PublicationFamily.REGISTRY
            or self.audit_record.operation != "versioned_document_v2"
            or self.audit_record.outcome is not expected_outcome
            or (
                self.receipt is not None
                and (
                    self.audit_record.target != self.receipt.target
                    or self.audit_record.new_fingerprint != self.receipt.registry_fingerprint
                )
            )
        ):
            raise ValueError("observed registry publication result is inconsistent")
        return self


def assemble_publishable_registry_version(
    proposal: PreparedSemanticOnboardingProposal,
    *,
    base: GovernedSemanticRegistrySnapshot | None,
) -> PublishableRegistryVersion:
    """Build one additive complete v2 version without I/O or inferred authority."""

    _validate_base(proposal, base)
    target_version = proposal.target_registry_version
    source = f"onboarding:{proposal.id}"
    model = _approved_model(proposal)
    new_mappings = tuple(
        _governed_mapping(mapping, logical_field_version=proposal.model.definition.version)
        for mapping in proposal.mappings
    )
    new_bindings = tuple(_physical_binding(proposal, mapping) for mapping in proposal.mappings)

    models: tuple[ApprovedRequestModel, ...]
    mappings: tuple[GovernedFieldMapping, ...]
    joins: tuple[JoinContract, ...]
    if base is None:
        models = (model,)
        mappings = new_mappings
        joins = ()
        logical_decisions: set[str] = set()
        mapping_decisions: set[str] = set()
        join_decisions: set[str] = set()
        base_bindings: tuple[GovernedPhysicalBinding, ...] = ()
    else:
        if model.id.root in base.logical_context.model_index():
            raise ValueError("onboarding publication cannot replace an active logical model")
        models = (*base.logical_context.models, model)
        mappings = (*base.mapping_set.mappings, *new_mappings)
        joins = base.join_contracts.contracts
        provenance = {item.kind: item for item in base.provenance}
        logical_decisions = set(provenance[RegistryArtifactKind.LOGICAL_MODELS].decision_ids)
        mapping_decisions = set(provenance[RegistryArtifactKind.PHYSICAL_MAPPINGS].decision_ids)
        join_decisions = set(provenance[RegistryArtifactKind.JOIN_CONTRACTS].decision_ids)
        base_bindings = base.physical_bindings

    model_decision = proposal.model.decision_id
    if model_decision is None:
        raise ValueError("onboarding model approval decision is missing")
    active_mapping_decisions = tuple(mapping.decision_id for mapping in proposal.mappings)
    if any(decision is None for decision in active_mapping_decisions):
        raise ValueError("onboarding mapping approval decision is missing")
    logical_decisions.add(model_decision)
    mapping_decisions.update(
        decision for decision in active_mapping_decisions if decision is not None
    )

    registry = GovernedSemanticRegistrySnapshot(
        format_version=2,
        registry_id=proposal.scope.registry_id,
        version=target_version,
        source=source,
        catalog_scope=proposal.scope.catalog_scope,
        logical_context=ApprovedLogicalContext(
            version=target_version,
            source=f"{source}/logical-context",
            models=models,
            joins=(() if base is None else base.logical_context.joins),
        ),
        mapping_set=GovernedMappingRegistry(version=target_version, mappings=mappings),
        join_contracts=GovernedJoinRegistry(version=target_version, contracts=joins),
        provenance=(
            RegistryArtifactProvenance(
                kind=RegistryArtifactKind.LOGICAL_MODELS,
                source=f"{source}/{RegistryArtifactKind.LOGICAL_MODELS.value}",
                decision_ids=tuple(sorted(logical_decisions)),
            ),
            RegistryArtifactProvenance(
                kind=RegistryArtifactKind.PHYSICAL_MAPPINGS,
                source=f"{source}/{RegistryArtifactKind.PHYSICAL_MAPPINGS.value}",
                decision_ids=tuple(sorted(mapping_decisions)),
            ),
            RegistryArtifactProvenance(
                kind=RegistryArtifactKind.JOIN_CONTRACTS,
                source=f"{source}/{RegistryArtifactKind.JOIN_CONTRACTS.value}",
                decision_ids=tuple(sorted(join_decisions)),
            ),
        ),
        physical_bindings=(*base_bindings, *new_bindings),
    )
    registry = prepare_datahub_registry_version(registry, proposal.scope)
    return create_publishable_registry_version(
        scope=proposal.scope,
        source_proposal_id=proposal.id,
        source_proposal_fingerprint=proposal.fingerprint,
        base_registry=proposal.base_registry,
        registry=registry,
        review_decision_ids=tuple(
            sorted(set(proposal.decision_ids) | set(semantic_registry_decision_ids(registry)))
        ),
    )


def create_publishable_registry_version(
    *,
    scope: SemanticRegistryScope,
    source_proposal_id: str,
    source_proposal_fingerprint: str,
    base_registry: OnboardingRegistryBase,
    registry: GovernedSemanticRegistrySnapshot,
    review_decision_ids: tuple[str, ...],
) -> PublishableRegistryVersion:
    """Create one self-verifying M34 candidate from a complete prepared v2 snapshot.

    The helper is proposal-family neutral.  It preserves the accepted M34 candidate
    identity while allowing later governed proposal types to reuse the isolated
    publication/read-back boundary.
    """

    active_decision_ids = semantic_registry_decision_ids(registry)
    payload = {
        "id": registry_publication_candidate_id(
            source_proposal_id,
            source_proposal_fingerprint,
        ),
        "scope": scope,
        "source_proposal_id": source_proposal_id,
        "source_proposal_fingerprint": source_proposal_fingerprint,
        "base_registry": base_registry,
        "registry": registry,
        "target": datahub_registry_document_urn(scope, registry.version),
        "review_decision_ids": tuple(sorted(set(review_decision_ids))),
        "active_decision_ids": active_decision_ids,
    }
    fingerprint = registry_publication_candidate_fingerprint(
        _candidate_identity_payload(
            id=payload["id"],
            scope=scope,
            source_proposal_id=source_proposal_id,
            source_proposal_fingerprint=source_proposal_fingerprint,
            base_registry=base_registry,
            registry_version=registry.version,
            registry_fingerprint=registry.fingerprint,
            target=payload["target"],
            review_decision_ids=payload["review_decision_ids"],
            active_decision_ids=active_decision_ids,
        )
    )
    return PublishableRegistryVersion(**payload, fingerprint=fingerprint)


def validate_registry_publication_authorization(
    candidate: PublishableRegistryVersion,
    authorization: RegistryPublicationAuthorization,
    *,
    at: datetime | None = None,
) -> None:
    """Reject authorization for any other candidate or an expired mutation window."""

    if (
        authorization.candidate_id != candidate.id
        or authorization.candidate_fingerprint != candidate.fingerprint
        or authorization.scope != candidate.scope
        or authorization.source_proposal_id != candidate.source_proposal_id
        or authorization.source_proposal_fingerprint != candidate.source_proposal_fingerprint
        or authorization.registry_fingerprint != candidate.registry.fingerprint
        or authorization.target != candidate.target
        or authorization.active_decision_ids != candidate.active_decision_ids
        or (at is not None and not authorization.is_current(at))
    ):
        raise ValueError("registry publication authorization does not match the candidate")


def registry_publication_candidate_id(proposal_id: str, proposal_fingerprint: str) -> str:
    digest = registry_publication_candidate_fingerprint(
        {
            "contract": "registry_publication_candidate_v1",
            "proposal_id": proposal_id,
            "proposal_fingerprint": proposal_fingerprint,
        }
    )
    return f"registry-candidate-{digest}"


def registry_publication_candidate_fingerprint(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def observed_registry_related_asset_urns(
    registry: GovernedSemanticRegistrySnapshot,
) -> tuple[str, ...]:
    if registry.format_version != 2 or not registry.physical_bindings:
        raise ValueError("observed related assets require a complete registry v2 binding")
    return tuple(
        sorted({binding.observed_datahub_asset_urn for binding in registry.physical_bindings})
    )


def registry_publication_authorization_id(
    authorization: RegistryPublicationAuthorization,
) -> str:
    payload = authorization.model_dump(mode="json", exclude={"id"})
    digest = registry_publication_candidate_fingerprint(
        {"contract": "registry_publication_authorization_v1", "payload": payload}
    )
    return f"registry-authorization-{digest}"


def _validate_base(
    proposal: PreparedSemanticOnboardingProposal,
    base: GovernedSemanticRegistrySnapshot | None,
) -> None:
    expected = proposal.base_registry
    if base is None:
        if expected != OnboardingRegistryBase():
            raise ValueError("onboarding publication base is unavailable")
        return
    if (
        base.format_version != 2
        or expected.registry_version != base.version
        or expected.registry_fingerprint != base.fingerprint
        or base.registry_id != proposal.scope.registry_id
        or base.catalog_scope != proposal.scope.catalog_scope
        or base.version + 1 != proposal.target_registry_version
    ):
        raise ValueError("onboarding publication base changed or lacks v2 authority")
    expected_source = f"datahub:{datahub_registry_document_id(proposal.scope, base.version)}"
    if (
        base.source != expected_source
        or base.logical_context.source != f"{expected_source}/logical-context"
        or any(item.workspace_id != proposal.workspace_id for item in base.physical_bindings)
    ):
        raise ValueError("onboarding publication base is outside the exact scope")
    proposal_connections = {
        mapping.observation.locator.asset.connection_id for mapping in proposal.mappings
    }
    base_connections = {binding.connection_id for binding in base.physical_bindings}
    if len(proposal_connections) != 1 or base_connections != proposal_connections:
        raise ValueError("onboarding publication cannot merge across connections")


def _approved_model(proposal: PreparedSemanticOnboardingProposal) -> ApprovedRequestModel:
    definition = proposal.model.definition
    return ApprovedRequestModel(
        id=definition.id,
        description=definition.description,
        fields=tuple(
            ApprovedRequestField(
                id=field.id,
                canonical_type=field.canonical_type,
                role=field.role,
                definition=field.definition,
                allowed_values=field.allowed_values,
                status=ApprovalStatus.APPROVED,
                version=definition.version,
            )
            for field in definition.fields
        ),
        status=ApprovalStatus.APPROVED,
        version=definition.version,
    )


def _governed_mapping(
    mapping: object,
    *,
    logical_field_version: int,
) -> GovernedFieldMapping:
    from schemabridge.domain.semantic_onboarding import SemanticMappingProposal

    if not isinstance(mapping, SemanticMappingProposal) or mapping.decision_id is None:
        raise ValueError("onboarding mapping is not approved")
    observation = mapping.observation
    return GovernedFieldMapping(
        mapping=ColumnMapping(
            logical_field=mapping.logical_field,
            physical_field=observation.physical_field,
            confidence=mapping.confidence,
            status=ApprovalStatus.APPROVED,
            evidence=tuple(_evidence_text(item) for item in mapping.evidence),
            risks=mapping.risks,
            transformation_plan=mapping.transformation_plan,
            version=1,
        ),
        physical_type=observation.physical_type,
        logical_field_version=logical_field_version,
        approval_decision_id=mapping.decision_id,
    )


def _physical_binding(
    proposal: PreparedSemanticOnboardingProposal,
    mapping: object,
) -> GovernedPhysicalBinding:
    from schemabridge.domain.semantic_onboarding import SemanticMappingProposal

    if not isinstance(mapping, SemanticMappingProposal):
        raise ValueError("onboarding mapping is invalid")
    observation = mapping.observation
    urn = observation.observed_datahub_asset_urn
    if urn is None:
        raise ValueError("every published mapping requires an observed DataHub asset URN")
    return GovernedPhysicalBinding(
        workspace_id=proposal.workspace_id,
        connection_id=observation.locator.asset.connection_id,
        catalog_scope=observation.catalog_scope,
        catalog_generation=observation.generation,
        catalog_generation_fingerprint=observation.generation_fingerprint,
        locator=observation.locator,
        asset_metadata_fingerprint=observation.asset_metadata_fingerprint,
        field_metadata_fingerprint=observation.field_metadata_fingerprint,
        logical_field=mapping.logical_field,
        physical_field=observation.physical_field,
        physical_type=observation.physical_type,
        observed_datahub_asset_urn=urn,
        source_proposal_id=proposal.id,
        source_proposal_fingerprint=proposal.fingerprint,
    )


def _evidence_text(evidence: object) -> str:
    from schemabridge.domain.semantic_onboarding import OnboardingEvidence

    if not isinstance(evidence, OnboardingEvidence):
        raise ValueError("onboarding evidence is invalid")
    suffix = "" if evidence.reference is None else f" [{evidence.reference}]"
    return f"{evidence.kind.value}: {evidence.detail}{suffix}"


def _jsonable(payload: object) -> object:
    return json.loads(json.dumps(payload, default=_json_default))


def _json_default(value: object) -> object:
    if isinstance(value, FrozenDomainModel):
        return value.model_dump(mode="json")
    raise TypeError(f"unsupported publication payload type: {type(value)!r}")


def _candidate_identity_payload(
    *,
    id: object,
    scope: SemanticRegistryScope,
    source_proposal_id: object,
    source_proposal_fingerprint: object,
    base_registry: OnboardingRegistryBase,
    registry_version: object,
    registry_fingerprint: object,
    target: object,
    review_decision_ids: object,
    active_decision_ids: object,
) -> dict[str, object]:
    return {
        "contract": "publishable_registry_version_v1",
        "id": id,
        "scope": scope.model_dump(mode="json"),
        "source_proposal_id": source_proposal_id,
        "source_proposal_fingerprint": source_proposal_fingerprint,
        "base_registry": base_registry.model_dump(mode="json"),
        "registry_version": registry_version,
        "registry_fingerprint": registry_fingerprint,
        "target": target,
        "review_decision_ids": review_decision_ids,
        "active_decision_ids": active_decision_ids,
    }


__all__ = [
    "ObservedRegistryPublicationResult",
    "PublicationReadbackReceipt",
    "PublishableRegistryVersion",
    "RegistryPublicationAuthorization",
    "RegistryPublicationAuthorizationConfirmation",
    "RegistryPublicationCandidateManifest",
    "assemble_publishable_registry_version",
    "create_publishable_registry_version",
    "observed_registry_related_asset_urns",
    "registry_publication_candidate_fingerprint",
    "registry_publication_candidate_id",
    "validate_registry_publication_authorization",
]
