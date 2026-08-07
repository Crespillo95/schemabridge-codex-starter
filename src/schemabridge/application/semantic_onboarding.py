"""Authenticated, fail-closed use cases for generic semantic onboarding."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from schemabridge.application.ports.semantic_onboarding import (
    SemanticOnboardingCatalogEvidencePort,
    SemanticOnboardingClockPort,
    SemanticOnboardingOperationReplay,
    SemanticOnboardingPortError,
    SemanticOnboardingPortErrorCode,
    SemanticOnboardingPreflightPort,
    SemanticOnboardingRegistryBasePort,
    SemanticOnboardingStorePort,
)
from schemabridge.application.semantic_onboarding_authorization import (
    SemanticOnboardingAuthorizationPolicy,
)
from schemabridge.domain.decisions import ApprovalStatus, DecisionAction
from schemabridge.domain.identity import AuthenticatedPrincipal
from schemabridge.domain.request_context import LogicalFieldRole
from schemabridge.domain.resolution import mapping_type_is_compatible
from schemabridge.domain.semantic_onboarding import (
    MAX_ONBOARDING_DECISIONS,
    CreateSemanticOnboardingRequest,
    OnboardingEvidence,
    OnboardingEvidenceKind,
    OnboardingRegistryBase,
    PhysicalCatalogObservation,
    PreflightSemanticOnboardingRequest,
    PreparedSemanticOnboardingProposal,
    ResolvedOnboardingCatalogEvidence,
    SemanticMappingProposal,
    SemanticModelProposal,
    SemanticOnboardingAuditEvent,
    SemanticOnboardingAuditRecord,
    SemanticOnboardingDecision,
    SemanticOnboardingDraft,
    SemanticOnboardingDraftMutation,
    SemanticOnboardingMappingInput,
    SemanticOnboardingPermission,
    SemanticOnboardingPreflight,
    SemanticOnboardingPreparation,
    SemanticOnboardingStatus,
    SemanticOnboardingTargetKind,
    apply_semantic_onboarding_decision,
    semantic_onboarding_fingerprint,
)
from schemabridge.domain.semantic_registry import PhysicalValueType, SemanticRegistryScope

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
_OP_CREATE = "create_draft"
_OP_DECIDE = "record_decision"
_OP_PREPARE = "prepare_publication"
DEFAULT_SEMANTIC_ONBOARDING_HISTORY_LIMIT = 25
MAX_SEMANTIC_ONBOARDING_HISTORY_LIMIT = 50


class SemanticOnboardingErrorCode(StrEnum):
    INVALID_REQUEST = "semantic_onboarding_invalid_request"
    UNAVAILABLE = "semantic_onboarding_resource_unavailable"
    CONFLICT = "semantic_onboarding_conflict"
    STALE_CATALOG = "semantic_onboarding_stale_catalog"
    STALE_REGISTRY = "semantic_onboarding_stale_registry"
    NOT_READY = "semantic_onboarding_not_ready"
    SEPARATION_OF_DUTIES = "semantic_onboarding_separation_of_duties"
    SERVICE_UNAVAILABLE = "semantic_onboarding_service_unavailable"


class SemanticOnboardingError(RuntimeError):
    """Stable public failure without provider, source, secret, or existence detail."""

    def __init__(self, code: SemanticOnboardingErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SemanticOnboardingSnapshot:
    draft: SemanticOnboardingDraft
    audit_visible: bool
    history_truncated: bool
    decisions: tuple[SemanticOnboardingDecision, ...]
    proposals: tuple[PreparedSemanticOnboardingProposal, ...]
    audit: tuple[SemanticOnboardingAuditRecord, ...]


@dataclass(frozen=True, slots=True)
class PreflightSemanticOnboardingDraft:
    preflights: SemanticOnboardingPreflightPort
    authorization: SemanticOnboardingAuthorizationPolicy
    clock: SemanticOnboardingClockPort
    scope: SemanticRegistryScope

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        request: PreflightSemanticOnboardingRequest,
    ) -> SemanticOnboardingPreflight:
        now = self.clock.now()
        self.authorization.require(
            principal,
            SemanticOnboardingPermission.CREATE,
            at=now,
        )
        _require_scope(principal, self.scope)
        try:
            resolved = self.preflights.resolve_active(self.scope, request)
            preflight = SemanticOnboardingPreflight.model_validate(
                resolved.model_dump(mode="python")
            )
        except SemanticOnboardingPortError as error:
            raise _port_failure(error) from error
        except (AttributeError, TypeError, ValueError) as error:
            raise _service_unavailable() from error
        expected_selections = tuple((item.asset_id, item.field_path) for item in request.selections)
        observed_selections = tuple(
            (item.locator.asset.asset_id, item.locator.field_path)
            for item in preflight.observations
        )
        if (
            preflight != resolved
            or preflight.scope != self.scope
            or preflight.connection_id != request.connection_id
            or observed_selections != expected_selections
        ):
            raise _service_unavailable()
        return preflight


@dataclass(frozen=True, slots=True)
class CreateSemanticOnboardingDraft:
    store: SemanticOnboardingStorePort
    catalog: SemanticOnboardingCatalogEvidencePort
    registry_bases: SemanticOnboardingRegistryBasePort
    authorization: SemanticOnboardingAuthorizationPolicy
    clock: SemanticOnboardingClockPort
    scope: SemanticRegistryScope

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        request: CreateSemanticOnboardingRequest,
        *,
        idempotency_key: str,
    ) -> SemanticOnboardingDraftMutation:
        now = self.clock.now()
        self.authorization.require(
            principal,
            SemanticOnboardingPermission.CREATE,
            at=now,
        )
        _require_scope(principal, self.scope)
        digest = _idempotency_digest(idempotency_key)
        request_payload = request.model_dump(mode="json")
        request_fingerprint = _request_fingerprint(
            _OP_CREATE,
            principal.actor_id,
            request_payload,
        )
        replay = _load_replay(
            self.store,
            principal,
            self.authorization,
            permission=SemanticOnboardingPermission.CREATE,
            at=now,
            operation=_OP_CREATE,
            idempotency_digest=digest,
            request_payload=request_payload,
        )
        if replay is not None:
            return SemanticOnboardingDraftMutation(draft=replay.draft, replayed=True)

        base = _load_base(self.registry_bases, self.scope)
        if base != request.expected_base_registry:
            raise SemanticOnboardingError(
                SemanticOnboardingErrorCode.STALE_REGISTRY,
                "The semantic registry base changed; reload before onboarding.",
            )
        evidence = _resolve_exact(
            self.catalog,
            self.scope,
            request,
        )
        _validate_evidence_bundle(request, evidence, self.scope)
        confirmed_preflight = SemanticOnboardingPreflight.create(
            scope=self.scope,
            evidence=evidence,
            base_registry=base,
        )
        if confirmed_preflight.fingerprint != request.confirmed_preflight_fingerprint:
            raise SemanticOnboardingError(
                SemanticOnboardingErrorCode.STALE_CATALOG,
                "The semantic onboarding preflight changed; run preflight again.",
            )
        observations = evidence.observations
        mappings = tuple(
            SemanticMappingProposal(
                id=selection.id,
                logical_field=selection.logical_field,
                observation=observation,
                confidence=selection.confidence,
                evidence=selection.evidence,
                risks=selection.risks,
                transformation_plan=selection.transformation_plan,
            )
            for selection, observation in zip(request.mappings, observations, strict=True)
        )
        draft = SemanticOnboardingDraft(
            id=request.draft_id,
            workspace_id=principal.workspace_id,
            owner_actor_id=principal.actor_id,
            scope=self.scope,
            connection_id=request.connection_id,
            catalog_generation=request.catalog_generation,
            catalog_generation_fingerprint=request.catalog_generation_fingerprint,
            base_registry=base,
            model=SemanticModelProposal(definition=request.model),
            mappings=mappings,
            created_at=now,
            updated_at=now,
        )
        audit = _audit_record(
            event=SemanticOnboardingAuditEvent.DRAFT_CREATED,
            draft=draft,
            actor_id=principal.actor_id,
            occurred_at=now,
            source_revision=0,
            previous_fingerprint=None,
        )
        try:
            return self.store.create(
                draft,
                audit,
                operation=_OP_CREATE,
                actor_id=principal.actor_id,
                idempotency_digest=digest,
                request_fingerprint=request_fingerprint,
            )
        except SemanticOnboardingPortError as error:
            raise _port_failure(error) from error


@dataclass(frozen=True, slots=True)
class ListSemanticOnboardingDrafts:
    store: SemanticOnboardingStorePort
    authorization: SemanticOnboardingAuthorizationPolicy
    clock: SemanticOnboardingClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        limit: int = 50,
    ) -> tuple[SemanticOnboardingDraft, ...]:
        now = self.clock.now()
        if not 1 <= limit <= 50:
            raise _invalid_request()
        owner = self.authorization.owner_filter_for(principal, at=now)
        try:
            return self.store.list_for_workspace(
                principal.workspace_id,
                owner_actor_id=owner,
                limit=limit,
            )
        except SemanticOnboardingPortError as error:
            raise _port_failure(error) from error


@dataclass(frozen=True, slots=True)
class InspectSemanticOnboardingDraft:
    store: SemanticOnboardingStorePort
    authorization: SemanticOnboardingAuthorizationPolicy
    clock: SemanticOnboardingClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        draft_id: str,
        *,
        history_limit: int = DEFAULT_SEMANTIC_ONBOARDING_HISTORY_LIMIT,
    ) -> SemanticOnboardingSnapshot:
        if not 1 <= history_limit <= MAX_SEMANTIC_ONBOARDING_HISTORY_LIMIT:
            raise _invalid_request()
        now = self.clock.now()
        draft = _load_draft(self.store, principal.workspace_id, draft_id)
        self.authorization.require_draft(
            principal,
            draft,
            SemanticOnboardingPermission.VIEW,
            at=now,
        )
        audit_visible = (
            SemanticOnboardingPermission.AUDIT_VIEW
            in self.authorization.permissions_for(principal, at=now)
        )
        if not audit_visible:
            return SemanticOnboardingSnapshot(
                draft=draft,
                audit_visible=False,
                history_truncated=False,
                decisions=(),
                proposals=(),
                audit=(),
            )
        try:
            fetch_limit = history_limit + 1
            decisions = self.store.list_decisions(
                principal.workspace_id,
                draft.id,
                limit=fetch_limit,
            )
            proposals = self.store.list_proposals(
                principal.workspace_id,
                draft.id,
                limit=fetch_limit,
            )
            audit = self.store.list_audit(
                principal.workspace_id,
                draft.id,
                limit=fetch_limit,
            )
            if any(len(items) > fetch_limit for items in (decisions, proposals, audit)):
                raise _service_unavailable()
            return SemanticOnboardingSnapshot(
                draft=draft,
                audit_visible=True,
                history_truncated=any(
                    len(items) > history_limit for items in (decisions, proposals, audit)
                ),
                decisions=decisions[-history_limit:],
                proposals=proposals[-history_limit:],
                audit=audit[-history_limit:],
            )
        except SemanticOnboardingPortError as error:
            raise _port_failure(error) from error


@dataclass(frozen=True, slots=True)
class DecideSemanticOnboarding:
    store: SemanticOnboardingStorePort
    catalog: SemanticOnboardingCatalogEvidencePort
    registry_bases: SemanticOnboardingRegistryBasePort
    authorization: SemanticOnboardingAuthorizationPolicy
    clock: SemanticOnboardingClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        draft_id: str,
        *,
        target_kind: SemanticOnboardingTargetKind,
        target_id: str,
        action: DecisionAction,
        expected_revision: int,
        confirmed_draft_fingerprint: str,
        rationale: str,
        evidence: tuple[OnboardingEvidence, ...],
        idempotency_key: str,
    ) -> SemanticOnboardingDraftMutation:
        now = self.clock.now()
        self.authorization.require(
            principal,
            SemanticOnboardingPermission.DECIDE,
            at=now,
        )
        digest = _idempotency_digest(idempotency_key)
        request_payload = {
            "action": action.value,
            "confirmed_draft_fingerprint": confirmed_draft_fingerprint,
            "draft_id": draft_id,
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "expected_revision": expected_revision,
            "rationale": rationale,
            "target_id": target_id,
            "target_kind": target_kind.value,
        }
        replay = _load_replay(
            self.store,
            principal,
            self.authorization,
            permission=SemanticOnboardingPermission.DECIDE,
            at=now,
            operation=_OP_DECIDE,
            idempotency_digest=digest,
            request_payload=request_payload,
        )
        if replay is not None:
            return SemanticOnboardingDraftMutation(draft=replay.draft, replayed=True)

        draft = _load_draft(self.store, principal.workspace_id, draft_id)
        self.authorization.require_draft(
            principal,
            draft,
            SemanticOnboardingPermission.DECIDE,
            at=now,
        )
        persisted_actor_id = self.authorization.actor_id_for_workspace(
            principal,
            draft.workspace_id,
        )
        request_fingerprint = _request_fingerprint(
            _OP_DECIDE,
            persisted_actor_id,
            request_payload,
        )
        if (
            draft.status is not SemanticOnboardingStatus.NEEDS_REVIEW
            or draft.revision != expected_revision
            or draft.fingerprint != confirmed_draft_fingerprint
        ):
            raise SemanticOnboardingError(
                SemanticOnboardingErrorCode.CONFLICT,
                "The semantic onboarding draft changed; reload before deciding.",
            )
        if action not in {DecisionAction.APPROVE, DecisionAction.REJECT}:
            raise _invalid_request()
        target_evidence = _target_evidence(draft, target_kind, target_id)
        _revalidate_draft(self.catalog, self.registry_bases, draft)
        if action is DecisionAction.APPROVE and (
            not _has_non_name_evidence(evidence)
            or (target_kind is SemanticOnboardingTargetKind.MAPPING and not target_evidence)
        ):
            raise SemanticOnboardingError(
                SemanticOnboardingErrorCode.INVALID_REQUEST,
                "Semantic approval requires evidence beyond name similarity.",
            )
        status = (
            ApprovalStatus.APPROVED if action is DecisionAction.APPROVE else ApprovalStatus.REJECTED
        )
        decision_id = _decision_id(
            draft,
            target_kind,
            target_id,
            action,
            persisted_actor_id,
            digest,
        )
        decision = SemanticOnboardingDecision(
            id=decision_id,
            workspace_id=draft.workspace_id,
            draft_id=draft.id,
            target_kind=target_kind,
            target_id=target_id,
            action=action,
            status=status,
            actor_id=persisted_actor_id,
            decided_at=now,
            source_revision=draft.revision,
            resulting_revision=draft.revision + 1,
            rationale=rationale,
            evidence=evidence,
            idempotency_digest=digest,
            request_fingerprint=request_fingerprint,
        )
        revised = apply_semantic_onboarding_decision(draft, decision)
        audit = _audit_record(
            event=SemanticOnboardingAuditEvent.DECISION_RECORDED,
            draft=revised,
            actor_id=persisted_actor_id,
            occurred_at=now,
            source_revision=draft.revision,
            previous_fingerprint=draft.fingerprint,
            decision_id=decision.id,
        )
        try:
            return self.store.commit_decision(
                revised,
                decision,
                audit,
                expected_revision=draft.revision,
                operation=_OP_DECIDE,
                actor_id=persisted_actor_id,
                idempotency_digest=digest,
                request_fingerprint=request_fingerprint,
            )
        except SemanticOnboardingPortError as error:
            raise _port_failure(error) from error


@dataclass(frozen=True, slots=True)
class PrepareSemanticOnboardingPublication:
    store: SemanticOnboardingStorePort
    catalog: SemanticOnboardingCatalogEvidencePort
    registry_bases: SemanticOnboardingRegistryBasePort
    authorization: SemanticOnboardingAuthorizationPolicy
    clock: SemanticOnboardingClockPort
    managed_mode: bool = True
    publisher_max_session_age: timedelta = timedelta(minutes=15)

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        draft_id: str,
        *,
        expected_revision: int,
        confirmed_draft_fingerprint: str,
        idempotency_key: str,
    ) -> SemanticOnboardingPreparation:
        now = self.clock.now()
        self.authorization.require(
            principal,
            SemanticOnboardingPermission.PREPARE_PUBLICATION,
            at=now,
        )
        digest = _idempotency_digest(idempotency_key)
        request_payload = {
            "confirmed_draft_fingerprint": confirmed_draft_fingerprint,
            "draft_id": draft_id,
            "expected_revision": expected_revision,
        }
        replay = _load_replay(
            self.store,
            principal,
            self.authorization,
            permission=SemanticOnboardingPermission.PREPARE_PUBLICATION,
            at=now,
            operation=_OP_PREPARE,
            idempotency_digest=digest,
            request_payload=request_payload,
        )
        if replay is not None:
            if replay.proposal is None:
                raise _service_unavailable()
            return SemanticOnboardingPreparation(
                draft=replay.draft,
                proposal=replay.proposal,
                replayed=True,
            )

        draft = _load_draft(self.store, principal.workspace_id, draft_id)
        self.authorization.require_draft(
            principal,
            draft,
            SemanticOnboardingPermission.PREPARE_PUBLICATION,
            at=now,
        )
        persisted_actor_id = self.authorization.actor_id_for_workspace(
            principal,
            draft.workspace_id,
        )
        request_fingerprint = _request_fingerprint(
            _OP_PREPARE,
            persisted_actor_id,
            request_payload,
        )
        if (
            draft.status is not SemanticOnboardingStatus.NEEDS_REVIEW
            or draft.revision != expected_revision
            or draft.fingerprint != confirmed_draft_fingerprint
        ):
            raise SemanticOnboardingError(
                SemanticOnboardingErrorCode.CONFLICT,
                "The semantic onboarding draft changed; reload before preparation.",
            )
        _revalidate_draft(self.catalog, self.registry_bases, draft)
        if not draft.ready_for_preparation():
            raise SemanticOnboardingError(
                SemanticOnboardingErrorCode.NOT_READY,
                "The semantic onboarding decision closure is incomplete.",
            )
        decisions = _list_decisions(self.store, draft)
        _validate_decision_closure(draft, decisions)
        if self.managed_mode:
            approving_actors = {
                decision.actor_id
                for decision in decisions
                if decision.status is ApprovalStatus.APPROVED
            }
            if (
                self.authorization.actor_identity_is_one_of(
                    principal,
                    workspace_id=draft.workspace_id,
                    actor_ids={draft.owner_actor_id, *approving_actors},
                )
                or now - principal.authenticated_at > self.publisher_max_session_age
            ):
                raise SemanticOnboardingError(
                    SemanticOnboardingErrorCode.SEPARATION_OF_DUTIES,
                    "Managed preparation requires a fresh, separate publisher identity.",
                )
        proposal_id = f"proposal-{draft.id}-v{draft.base_registry.next_registry_version}"
        proposal = PreparedSemanticOnboardingProposal.create(
            id=proposal_id,
            draft=draft,
            decision_ids=tuple(decision.id for decision in decisions),
            prepared_by=persisted_actor_id,
            prepared_at=now,
        )
        ready = SemanticOnboardingDraft.model_validate(
            {
                **draft.model_dump(mode="python"),
                "revision": draft.revision + 1,
                "status": SemanticOnboardingStatus.READY_FOR_PUBLICATION,
                "prepared_proposal_id": proposal.id,
                "prepared_proposal_fingerprint": proposal.fingerprint,
                "prepared_by": persisted_actor_id,
                "updated_at": now,
            }
        )
        audit = _audit_record(
            event=SemanticOnboardingAuditEvent.PUBLICATION_PREPARED,
            draft=ready,
            actor_id=persisted_actor_id,
            occurred_at=now,
            source_revision=draft.revision,
            previous_fingerprint=draft.fingerprint,
            proposal_id=proposal.id,
        )
        try:
            return self.store.commit_preparation(
                ready,
                proposal,
                audit,
                expected_revision=draft.revision,
                operation=_OP_PREPARE,
                actor_id=persisted_actor_id,
                idempotency_digest=digest,
                request_fingerprint=request_fingerprint,
            )
        except SemanticOnboardingPortError as error:
            raise _port_failure(error) from error


def _resolve_exact(
    catalog: SemanticOnboardingCatalogEvidencePort,
    scope: SemanticRegistryScope,
    request: CreateSemanticOnboardingRequest,
) -> ResolvedOnboardingCatalogEvidence:
    try:
        return catalog.resolve_exact(
            scope,
            request.connection_id,
            request.catalog_generation,
            request.catalog_generation_fingerprint,
            request.mappings,
        )
    except SemanticOnboardingPortError as error:
        raise _port_failure(error, stale_catalog=True) from error


def _validate_evidence_bundle(
    request: CreateSemanticOnboardingRequest,
    evidence: ResolvedOnboardingCatalogEvidence,
    scope: SemanticRegistryScope,
) -> None:
    generation = evidence.generation
    if (
        generation.workspace_id != scope.workspace_id
        or generation.connection_id != request.connection_id
        or generation.catalog_scope != scope.catalog_scope
        or generation.generation != request.catalog_generation
        or generation.inventory_fingerprint != request.catalog_generation_fingerprint
        or not generation.enabled
        or generation.stale
        or len(evidence.observations) != len(request.mappings)
    ):
        raise SemanticOnboardingError(
            SemanticOnboardingErrorCode.STALE_CATALOG,
            "The selected catalog generation is not current and complete.",
        )
    for selection, observation in zip(request.mappings, evidence.observations, strict=True):
        if (
            observation.locator.asset.asset_id != selection.asset_id
            or observation.locator.field_path != selection.field_path
            or observation.asset_metadata_fingerprint
            != selection.expected_asset_metadata_fingerprint
            or observation.field_metadata_fingerprint
            != selection.expected_field_metadata_fingerprint
            or observation.physical_field != selection.physical_field
            or not _type_is_compatible(request, selection, observation)
        ):
            raise SemanticOnboardingError(
                SemanticOnboardingErrorCode.STALE_CATALOG,
                "The selected catalog evidence is not exact and current.",
            )


def _type_is_compatible(
    request: CreateSemanticOnboardingRequest,
    selection: SemanticOnboardingMappingInput,
    observation: PhysicalCatalogObservation,
) -> bool:
    field = next(item for item in request.model.fields if item.id == selection.logical_field)
    physical = observation.physical_type
    if field.role is LogicalFieldRole.IDENTIFIER and physical is PhysicalValueType.FLOAT:
        return False
    return mapping_type_is_compatible(
        field.canonical_type,
        physical,
        selection.transformation_plan,
    )


def _revalidate_draft(
    catalog: SemanticOnboardingCatalogEvidencePort,
    registry_bases: SemanticOnboardingRegistryBasePort,
    draft: SemanticOnboardingDraft,
) -> None:
    current_base = _load_base(registry_bases, draft.scope)
    if current_base != draft.base_registry:
        raise SemanticOnboardingError(
            SemanticOnboardingErrorCode.STALE_REGISTRY,
            "The semantic registry base changed; reload the onboarding draft.",
        )
    selections = tuple(
        SemanticOnboardingMappingInput(
            id=mapping.id,
            logical_field=mapping.logical_field,
            asset_id=mapping.observation.locator.asset.asset_id,
            field_path=mapping.observation.locator.field_path,
            expected_asset_metadata_fingerprint=(mapping.observation.asset_metadata_fingerprint),
            expected_field_metadata_fingerprint=(mapping.observation.field_metadata_fingerprint),
            physical_field=mapping.observation.physical_field,
            confidence=mapping.confidence,
            evidence=mapping.evidence,
            risks=mapping.risks,
            transformation_plan=mapping.transformation_plan,
        )
        for mapping in draft.mappings
    )
    try:
        current = catalog.resolve_exact(
            draft.scope,
            draft.connection_id,
            draft.catalog_generation,
            draft.catalog_generation_fingerprint,
            selections,
        )
    except SemanticOnboardingPortError as error:
        raise _port_failure(error, stale_catalog=True) from error
    if current.observations != tuple(mapping.observation for mapping in draft.mappings):
        raise SemanticOnboardingError(
            SemanticOnboardingErrorCode.STALE_CATALOG,
            "The physical catalog evidence changed; start a fresh review.",
        )


def _target_evidence(
    draft: SemanticOnboardingDraft,
    target_kind: SemanticOnboardingTargetKind,
    target_id: str,
) -> bool:
    if target_kind is SemanticOnboardingTargetKind.MODEL:
        if target_id != draft.model.definition.id.root:
            raise SemanticOnboardingError(
                SemanticOnboardingErrorCode.UNAVAILABLE,
                "The semantic onboarding resource is not available.",
            )
        if draft.model.status is not ApprovalStatus.NEEDS_REVIEW:
            raise SemanticOnboardingError(
                SemanticOnboardingErrorCode.CONFLICT,
                "The semantic onboarding decision is already terminal.",
            )
        return True
    mapping = next((item for item in draft.mappings if item.id == target_id), None)
    if mapping is None:
        raise SemanticOnboardingError(
            SemanticOnboardingErrorCode.UNAVAILABLE,
            "The semantic onboarding resource is not available.",
        )
    if mapping.status is not ApprovalStatus.NEEDS_REVIEW:
        raise SemanticOnboardingError(
            SemanticOnboardingErrorCode.CONFLICT,
            "The semantic onboarding decision is already terminal.",
        )
    return mapping.has_non_name_evidence


def _validate_decision_closure(
    draft: SemanticOnboardingDraft,
    decisions: tuple[SemanticOnboardingDecision, ...],
) -> None:
    by_id = {decision.id: decision for decision in decisions}
    current_ids = {
        draft.model.decision_id,
        *(mapping.decision_id for mapping in draft.mappings),
    }
    if None in current_ids or not current_ids.issubset(by_id):
        raise SemanticOnboardingError(
            SemanticOnboardingErrorCode.NOT_READY,
            "The semantic onboarding decision closure is incomplete.",
        )
    if len(by_id) != len(decisions):
        raise _service_unavailable()
    for decision_id in current_ids:
        assert decision_id is not None
        decision = by_id[decision_id]
        if decision.workspace_id != draft.workspace_id or decision.draft_id != draft.id:
            raise _service_unavailable()


def _list_decisions(
    store: SemanticOnboardingStorePort,
    draft: SemanticOnboardingDraft,
) -> tuple[SemanticOnboardingDecision, ...]:
    try:
        decisions = store.list_decisions(
            draft.workspace_id,
            draft.id,
            limit=MAX_ONBOARDING_DECISIONS + 1,
        )
    except SemanticOnboardingPortError as error:
        raise _port_failure(error) from error
    if len(decisions) > MAX_ONBOARDING_DECISIONS:
        raise _service_unavailable()
    return decisions


def _load_base(
    port: SemanticOnboardingRegistryBasePort,
    scope: SemanticRegistryScope,
) -> OnboardingRegistryBase:
    try:
        return port.load(scope)
    except SemanticOnboardingPortError as error:
        raise _port_failure(error) from error


def _load_draft(
    store: SemanticOnboardingStorePort,
    workspace_id: str,
    draft_id: str,
) -> SemanticOnboardingDraft:
    try:
        draft = store.load(workspace_id, draft_id)
    except SemanticOnboardingPortError as error:
        raise _port_failure(error) from error
    if draft is None:
        raise SemanticOnboardingError(
            SemanticOnboardingErrorCode.UNAVAILABLE,
            "The semantic onboarding resource is not available.",
        )
    return draft


def _load_replay(
    store: SemanticOnboardingStorePort,
    principal: AuthenticatedPrincipal,
    authorization: SemanticOnboardingAuthorizationPolicy,
    *,
    permission: SemanticOnboardingPermission,
    at: datetime,
    operation: str,
    idempotency_digest: str,
    request_payload: object,
) -> SemanticOnboardingOperationReplay | None:
    try:
        replay = store.load_operation_replay(principal.workspace_id, idempotency_digest)
    except SemanticOnboardingPortError as error:
        raise _port_failure(error) from error
    if replay is None:
        return None
    authorization.require_draft(principal, replay.draft, permission, at=at)
    if (
        replay.operation != operation
        or not authorization.actor_identity_is_one_of(
            principal,
            workspace_id=replay.draft.workspace_id,
            actor_ids=(replay.actor_id,),
        )
        or replay.request_fingerprint
        != _request_fingerprint(operation, replay.actor_id, request_payload)
    ):
        raise SemanticOnboardingError(
            SemanticOnboardingErrorCode.CONFLICT,
            "The idempotency key is already bound to another onboarding operation.",
        )
    return replay


def _require_scope(
    principal: AuthenticatedPrincipal,
    scope: SemanticRegistryScope,
) -> None:
    if principal.workspace_id != scope.workspace_id:
        raise SemanticOnboardingError(
            SemanticOnboardingErrorCode.UNAVAILABLE,
            "The semantic onboarding resource is not available.",
        )


def _has_non_name_evidence(evidence: tuple[OnboardingEvidence, ...]) -> bool:
    return any(item.kind is not OnboardingEvidenceKind.NAME_SIMILARITY for item in evidence)


def _idempotency_digest(value: str) -> str:
    if not isinstance(value, str) or _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise _invalid_request()
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request_fingerprint(operation: str, actor_id: str, payload: object) -> str:
    return semantic_onboarding_fingerprint(
        {"actor_id": actor_id, "operation": operation, "payload": payload}
    )


def _decision_id(
    draft: SemanticOnboardingDraft,
    target_kind: SemanticOnboardingTargetKind,
    target_id: str,
    action: DecisionAction,
    actor_id: str,
    idempotency_digest: str,
) -> str:
    digest = semantic_onboarding_fingerprint(
        {
            "action": action.value,
            "actor_id": actor_id,
            "draft_id": draft.id,
            "idempotency_digest": idempotency_digest,
            "source_revision": draft.revision,
            "target_id": target_id,
            "target_kind": target_kind.value,
            "workspace_id": draft.workspace_id,
        }
    )
    return f"onboarding-decision-{digest[:32]}"


def _audit_record(
    *,
    event: SemanticOnboardingAuditEvent,
    draft: SemanticOnboardingDraft,
    actor_id: str,
    occurred_at: datetime,
    source_revision: int,
    previous_fingerprint: str | None,
    decision_id: str | None = None,
    proposal_id: str | None = None,
) -> SemanticOnboardingAuditRecord:
    identity = semantic_onboarding_fingerprint(
        {
            "actor_id": actor_id,
            "draft_id": draft.id,
            "event": event.value,
            "resulting_fingerprint": draft.fingerprint,
            "resulting_revision": draft.revision,
            "workspace_id": draft.workspace_id,
        }
    )
    return SemanticOnboardingAuditRecord(
        id=f"onboarding-audit-{identity[:32]}",
        workspace_id=draft.workspace_id,
        draft_id=draft.id,
        event=event,
        actor_id=actor_id,
        occurred_at=occurred_at,
        source_revision=source_revision,
        resulting_revision=draft.revision,
        previous_fingerprint=previous_fingerprint,
        resulting_fingerprint=draft.fingerprint,
        decision_id=decision_id,
        proposal_id=proposal_id,
    )


def _port_failure(
    error: SemanticOnboardingPortError,
    *,
    stale_catalog: bool = False,
) -> SemanticOnboardingError:
    if error.code is SemanticOnboardingPortErrorCode.CONFLICT:
        return SemanticOnboardingError(
            SemanticOnboardingErrorCode.CONFLICT,
            "The semantic onboarding state changed; reload and retry.",
        )
    if error.code is SemanticOnboardingPortErrorCode.RESOURCE_UNAVAILABLE:
        code = (
            SemanticOnboardingErrorCode.STALE_CATALOG
            if stale_catalog
            else SemanticOnboardingErrorCode.UNAVAILABLE
        )
        return SemanticOnboardingError(code, "The semantic onboarding resource is not available.")
    return _service_unavailable()


def _invalid_request() -> SemanticOnboardingError:
    return SemanticOnboardingError(
        SemanticOnboardingErrorCode.INVALID_REQUEST,
        "The semantic onboarding request is invalid.",
    )


def _service_unavailable() -> SemanticOnboardingError:
    return SemanticOnboardingError(
        SemanticOnboardingErrorCode.SERVICE_UNAVAILABLE,
        "The semantic onboarding service is unavailable.",
    )


__all__ = [
    "CreateSemanticOnboardingDraft",
    "DecideSemanticOnboarding",
    "InspectSemanticOnboardingDraft",
    "ListSemanticOnboardingDrafts",
    "PreflightSemanticOnboardingDraft",
    "PrepareSemanticOnboardingPublication",
    "SemanticOnboardingError",
    "SemanticOnboardingErrorCode",
    "SemanticOnboardingSnapshot",
]
