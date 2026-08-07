"""Authenticated fail-closed use cases for M35 Phase-B model replacements."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from schemabridge.application.ports.connectors import ExecutionTargetResolverPort
from schemabridge.application.ports.registry_changes import RegistryChangeClockPort
from schemabridge.application.ports.registry_control import (
    ActiveRegistryPointerReadPort,
    RegistryControlError,
    RegistryVersionReadPort,
)
from schemabridge.application.ports.registry_model_changes import (
    RegistryModelChangeOperationReplay,
    RegistryModelChangePortError,
    RegistryModelChangePortErrorCode,
    RegistryModelChangeStorePort,
    RegistryModelJoinProfileWitnessPort,
    RegistryModelProfileOperationReplay,
    RegistryModelProfileQueuePort,
    RegistryModelProfileStorePort,
    RegistryModelRemediationEvidencePort,
    RegistryModelReplacementSourcePort,
)
from schemabridge.application.ports.registry_publication import (
    RegistryPublicationPhysicalBindingAuthorityPort,
)
from schemabridge.application.ports.semantic_change import (
    SemanticChangeDependencyIndexPort,
    SemanticChangePortError,
)
from schemabridge.application.ports.semantic_profile_jobs import (
    SemanticJoinProfileQueueError,
    SemanticJoinProfileQueueErrorCode,
)
from schemabridge.application.registry_change_authorization import (
    RegistryChangeAuthorizationPolicy,
)
from schemabridge.application.registry_changes import (
    RegistryChangeAuthoringError,
    RegistryChangeAuthoringErrorCode,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import GovernedExecutionTarget
from schemabridge.domain.decisions import DecisionAction
from schemabridge.domain.identity import AuthenticatedPrincipal
from schemabridge.domain.registry_change_authoring import RegistryChangePermission
from schemabridge.domain.registry_control import (
    ActiveRegistryPointer,
    RegistryVersionTrust,
    registry_projection_fingerprint,
)
from schemabridge.domain.registry_model_change_authoring import (
    MAX_REGISTRY_MODEL_CHANGE_HISTORY,
    CreateRegistryModelChangeInput,
    RegistryIncidentJoinPreserveInput,
    RegistryIncidentJoinRemovalInput,
    RegistryIncidentJoinUpsertInput,
    RegistryModelChangeAuditEvent,
    RegistryModelChangeAuditRecord,
    RegistryModelChangeDraft,
    RegistryModelChangeMutation,
    RegistryModelChangeSnapshot,
    RegistryModelChangeStatus,
    RegistryModelIncidentIntent,
    RegistryModelJoinProfileAuditEvent,
    RegistryModelJoinProfileAuditRecord,
    RegistryModelJoinProfileAuthoringRequest,
    RegistryModelJoinProfileMutation,
    RegistryModelJoinProfileRequest,
    RegistryModelReplacementSourceEvidence,
    RequestRegistryModelJoinProfileInput,
    create_registry_model_change_draft,
    decide_registry_model_change,
    prepare_registry_model_change,
    registry_model_authoring_fingerprint,
)
from schemabridge.domain.registry_model_changes import (
    RegistryModelChangeAuthority,
    RegistryModelChangeKind,
    RegistryModelJoinProfileWitness,
    registry_model_replacement_artifacts,
    resolve_registry_model_replacement_base,
)
from schemabridge.domain.semantic_change import (
    GovernedJoinRef,
    GovernedMappingRef,
    SemanticChangeInspectionContext,
    SemanticChangeReport,
    SemanticImpactSet,
)
from schemabridge.domain.semantic_onboarding import OnboardingRegistryBase
from schemabridge.domain.semantic_profile_jobs import (
    SemanticJoinProfileJob,
    SemanticJoinProfileJobStatus,
    SemanticJoinProfileTargetRef,
)
from schemabridge.domain.semantic_registry import (
    GovernedSemanticRegistrySnapshot,
    SemanticRegistryScope,
)

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
_OP_CREATE = "create_model_change"
_OP_DECIDE = "decide_model_change"
_OP_PREPARE = "prepare_model_change_publication"
_OP_PROFILE_REQUEST = "request_model_join_profile"
_OP_PROFILE_FINALIZE = "finalize_model_join_profile"
DEFAULT_REGISTRY_MODEL_CHANGE_HISTORY_LIMIT = 25
MAX_REGISTRY_MODEL_CHANGE_LIST_LIMIT = 50


@dataclass(frozen=True, slots=True)
class RequestRegistryModelJoinProfile:
    store: RegistryModelProfileStorePort
    queue: RegistryModelProfileQueuePort
    sources: RegistryModelReplacementSourcePort
    pointers: ActiveRegistryPointerReadPort
    versions: RegistryVersionReadPort
    dependencies: SemanticChangeDependencyIndexPort
    targets: ExecutionTargetResolverPort
    physical_bindings: RegistryPublicationPhysicalBindingAuthorityPort
    authorization: RegistryChangeAuthorizationPolicy
    clock: RegistryChangeClockPort
    scope: SemanticRegistryScope

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        request: RequestRegistryModelJoinProfileInput,
        *,
        idempotency_key: str,
    ) -> RegistryModelJoinProfileMutation:
        now = self.clock.now()
        self.authorization.require(
            principal,
            RegistryChangePermission.REQUEST_PROFILE,
            at=now,
        )
        digest = _idempotency_digest(idempotency_key)
        payload = request.model_dump(mode="json")
        replay = _load_profile_replay(
            self.store,
            self.authorization,
            principal,
            operation=_OP_PROFILE_REQUEST,
            digest=digest,
            payload=payload,
            at=now,
        )
        if replay is not None:
            if replay.job is not None:
                return RegistryModelJoinProfileMutation(
                    authoring=replay.authoring,
                    job=replay.job,
                    witness=replay.witness,
                    replayed=True,
                )
            return self._enqueue_and_bind(
                replay.authoring,
                actor_id=replay.actor_id,
                digest=digest,
                at=now,
            )
        if principal.workspace_id != self.scope.workspace_id:
            raise _unavailable()
        source = _load_source(
            self.sources,
            principal.workspace_id,
            request.replacement_proposal_id,
        )
        if source.proposal.fingerprint != request.expected_replacement_fingerprint:
            raise _stale_base()
        pointer, registry, base_registry = _load_active(
            self.pointers,
            self.versions,
            self.scope,
        )
        if base_registry != request.expected_base_registry:
            raise _stale_base()
        context = _current_dependency_context(
            self.dependencies,
            pointer=pointer,
            registry=registry,
        )
        try:
            base = resolve_registry_model_replacement_base(
                scope=self.scope,
                base=registry,
                base_registry=base_registry,
                target_model_id=request.target_model_id,
                dependency_context=context,
            )
        except ValueError as error:
            raise _stale_base() from error
        _require_replacement_bindings(self.physical_bindings, self.scope, source)
        target = _resolve_target(
            self.targets,
            workspace_id=principal.workspace_id,
            connection_id=source.proposal.mappings[0].observation.locator.asset.connection_id,
        )
        if target.fingerprint != request.expected_execution_target_fingerprint:
            raise _stale_target()
        try:
            profile_request = RegistryModelJoinProfileRequest.create(
                workspace_id=principal.workspace_id,
                change_id=request.change_id,
                replacement_source=source,
                base=base,
                incident_join_id=request.join_id,
                proposal=request.proposal,
                execution_target=SemanticJoinProfileTargetRef.from_target(target),
                requested_at=now,
            )
            authoring = RegistryModelJoinProfileAuthoringRequest.create(
                id=request.request_id,
                workspace_id=principal.workspace_id,
                owner_actor_id=principal.actor_id,
                request=profile_request,
                created_at=now,
            )
        except (StopIteration, ValueError) as error:
            raise _invalid_request() from error
        actor_id = self.authorization.actor_id_for_workspace(
            principal,
            authoring.workspace_id,
        )
        request_fingerprint = _profile_request_fingerprint(
            _OP_PROFILE_REQUEST,
            actor_id,
            authoring.id,
            payload,
        )
        audit = RegistryModelJoinProfileAuditRecord.create(
            authoring=authoring,
            event=RegistryModelJoinProfileAuditEvent.REQUEST_PERSISTED,
            actor_id=actor_id,
            occurred_at=now,
        )
        try:
            persisted = self.store.persist_request(
                authoring,
                audit,
                operation=_OP_PROFILE_REQUEST,
                actor_id=actor_id,
                idempotency_digest=digest,
                request_fingerprint=request_fingerprint,
            )
        except RegistryModelChangePortError as error:
            raise _store_failure(error) from error
        if persisted.authoring != authoring or persisted.job is not None:
            raise _service_unavailable()
        return self._enqueue_and_bind(
            authoring,
            actor_id=actor_id,
            digest=digest,
            at=now,
        )

    def _enqueue_and_bind(
        self,
        authoring: RegistryModelJoinProfileAuthoringRequest,
        *,
        actor_id: str,
        digest: str,
        at: datetime,
    ) -> RegistryModelJoinProfileMutation:
        _revalidate_profile_authoring(
            authoring,
            sources=self.sources,
            pointers=self.pointers,
            versions=self.versions,
            dependencies=self.dependencies,
            targets=self.targets,
            physical_bindings=self.physical_bindings,
        )
        try:
            submission = self.queue.enqueue(authoring)
        except SemanticJoinProfileQueueError as error:
            raise _profile_queue_failure(error) from error
        job = submission.job
        if not _profile_job_matches(job, authoring):
            raise _service_unavailable()
        audit = RegistryModelJoinProfileAuditRecord.create(
            authoring=authoring,
            event=RegistryModelJoinProfileAuditEvent.JOB_BOUND,
            actor_id=actor_id,
            occurred_at=at,
            job_id=job.job_id,
        )
        try:
            bound = self.store.bind_job(
                authoring,
                job,
                audit,
                idempotency_digest=digest,
            )
        except RegistryModelChangePortError as error:
            raise _store_failure(error) from error
        if bound.authoring != authoring or bound.job != job:
            raise _service_unavailable()
        return RegistryModelJoinProfileMutation(
            authoring=authoring,
            job=job,
            replayed=submission.replayed or bound.replayed,
        )


@dataclass(frozen=True, slots=True)
class FinalizeRegistryModelJoinProfile:
    store: RegistryModelProfileStorePort
    queue: RegistryModelProfileQueuePort
    sources: RegistryModelReplacementSourcePort
    pointers: ActiveRegistryPointerReadPort
    versions: RegistryVersionReadPort
    dependencies: SemanticChangeDependencyIndexPort
    targets: ExecutionTargetResolverPort
    physical_bindings: RegistryPublicationPhysicalBindingAuthorityPort
    authorization: RegistryChangeAuthorizationPolicy
    clock: RegistryChangeClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        request_id: str,
        *,
        confirmed_authoring_fingerprint: str,
        idempotency_key: str,
    ) -> RegistryModelJoinProfileMutation:
        now = self.clock.now()
        self.authorization.require(
            principal,
            RegistryChangePermission.FINALIZE_DRAFT,
            at=now,
        )
        digest = _idempotency_digest(idempotency_key)
        payload = {
            "request_id": request_id,
            "confirmed_authoring_fingerprint": confirmed_authoring_fingerprint,
        }
        replay = _load_profile_replay(
            self.store,
            self.authorization,
            principal,
            operation=_OP_PROFILE_FINALIZE,
            digest=digest,
            payload=payload,
            at=now,
        )
        if replay is not None:
            if replay.job is None or replay.witness is None:
                raise _service_unavailable()
            return RegistryModelJoinProfileMutation(
                authoring=replay.authoring,
                job=replay.job,
                witness=replay.witness,
                replayed=True,
            )
        authoring = _load_profile_authoring(
            self.store,
            self.authorization,
            principal,
            request_id,
        )
        self.authorization.require_resource(
            principal,
            authoring,
            RegistryChangePermission.FINALIZE_DRAFT,
            at=now,
        )
        if authoring.fingerprint != confirmed_authoring_fingerprint:
            raise _conflict()
        actor_id = self.authorization.actor_id_for_workspace(
            principal,
            authoring.workspace_id,
        )
        _revalidate_profile_authoring(
            authoring,
            sources=self.sources,
            pointers=self.pointers,
            versions=self.versions,
            dependencies=self.dependencies,
            targets=self.targets,
            physical_bindings=self.physical_bindings,
        )
        try:
            job = self.queue.load(authoring)
        except SemanticJoinProfileQueueError as error:
            raise _profile_queue_failure(error) from error
        if (
            job is None
            or job.status is not SemanticJoinProfileJobStatus.COMPLETED
            or job.result is None
            or not _profile_job_matches(job, authoring)
        ):
            raise RegistryChangeAuthoringError(
                RegistryChangeAuthoringErrorCode.PROFILE_NOT_READY,
                "The exact replacement relationship profile is not complete.",
            )
        request = authoring.request
        incident = next(
            item
            for item in request.base.incident_joins
            if item.contract.id == request.incident_join_id
        )
        try:
            witness = RegistryModelJoinProfileWitness.create(
                change_id=request.change_id,
                base=request.base,
                replacement=request.replacement_source.proposal,
                incident=incident,
                proposal=request.proposal,
                execution_target=request.execution_target,
                profile=job.result.profile,
                completed_at=job.result.completed_at,
            )
        except ValueError as error:
            raise _profile_stale() from error
        if not witness.is_current(now):
            raise _profile_stale()
        audit = RegistryModelJoinProfileAuditRecord.create(
            authoring=authoring,
            event=RegistryModelJoinProfileAuditEvent.WITNESS_RECORDED,
            actor_id=actor_id,
            occurred_at=now,
            job_id=job.job_id,
            witness_fingerprint=witness.fingerprint,
        )
        request_fingerprint = _profile_request_fingerprint(
            _OP_PROFILE_FINALIZE,
            actor_id,
            authoring.id,
            payload,
        )
        try:
            return self.store.record_witness(
                authoring,
                job,
                witness,
                audit,
                operation=_OP_PROFILE_FINALIZE,
                actor_id=actor_id,
                idempotency_digest=digest,
                request_fingerprint=request_fingerprint,
            )
        except RegistryModelChangePortError as error:
            raise _store_failure(error) from error


@dataclass(frozen=True, slots=True)
class CreateRegistryModelChange:
    store: RegistryModelChangeStorePort
    sources: RegistryModelReplacementSourcePort
    remediation: RegistryModelRemediationEvidencePort
    profile_witnesses: RegistryModelJoinProfileWitnessPort
    pointers: ActiveRegistryPointerReadPort
    versions: RegistryVersionReadPort
    dependencies: SemanticChangeDependencyIndexPort
    targets: ExecutionTargetResolverPort
    physical_bindings: RegistryPublicationPhysicalBindingAuthorityPort
    authorization: RegistryChangeAuthorizationPolicy
    clock: RegistryChangeClockPort
    scope: SemanticRegistryScope

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        request: CreateRegistryModelChangeInput,
        *,
        idempotency_key: str,
    ) -> RegistryModelChangeMutation:
        now = self.clock.now()
        self.authorization.require(
            principal,
            RegistryChangePermission.FINALIZE_DRAFT,
            at=now,
        )
        digest = _idempotency_digest(idempotency_key)
        payload = request.model_dump(mode="json")
        replay = _load_replay(
            self.store,
            self.authorization,
            principal,
            operation=_OP_CREATE,
            permission=RegistryChangePermission.FINALIZE_DRAFT,
            digest=digest,
            payload=payload,
            at=now,
        )
        if replay is not None:
            return RegistryModelChangeMutation(draft=replay.draft, replayed=True)
        if principal.workspace_id != self.scope.workspace_id:
            raise _unavailable()
        source = _load_source(
            self.sources,
            principal.workspace_id,
            request.replacement_proposal_id,
        )
        if (
            source.proposal.fingerprint != request.expected_replacement_fingerprint
            or source.proposal.base_registry != request.expected_base_registry
        ):
            raise _stale_base()
        pointer, registry, base_registry = _load_active(
            self.pointers,
            self.versions,
            self.scope,
        )
        if base_registry != request.expected_base_registry:
            raise _stale_base()
        context = _current_dependency_context(
            self.dependencies,
            pointer=pointer,
            registry=registry,
        )
        try:
            base = resolve_registry_model_replacement_base(
                scope=self.scope,
                base=registry,
                base_registry=base_registry,
                target_model_id=request.target_model_id,
                dependency_context=context,
            )
        except ValueError as error:
            raise _stale_base() from error
        authority = _resolve_authority(
            self.remediation,
            request=request,
            base=base,
        )
        _require_replacement_bindings(
            self.physical_bindings,
            self.scope,
            source,
        )
        intents = _resolve_incident_intents(
            request,
            source=source,
            base=base,
            witnesses=self.profile_witnesses,
            targets=self.targets,
            physical_bindings=self.physical_bindings,
            at=now,
        )
        try:
            draft = create_registry_model_change_draft(
                change_id=request.change_id,
                owner_actor_id=principal.actor_id,
                source=source,
                base=base,
                authority=authority,
                incident_intents=intents,
                risks=request.risks,
                created_at=now,
            )
        except ValueError as error:
            raise _invalid_request() from error
        actor_id = self.authorization.actor_id_for_workspace(
            principal,
            draft.workspace_id,
        )
        request_fingerprint = _request_fingerprint(
            _OP_CREATE,
            actor_id,
            draft.id,
            payload,
        )
        audit = RegistryModelChangeAuditRecord.create(
            draft=draft,
            event=RegistryModelChangeAuditEvent.DRAFT_CREATED,
            actor_id=actor_id,
            occurred_at=now,
            source_revision=0,
            previous_fingerprint=None,
        )
        try:
            return self.store.create(
                draft,
                audit,
                operation=_OP_CREATE,
                actor_id=actor_id,
                idempotency_digest=digest,
                request_fingerprint=request_fingerprint,
            )
        except RegistryModelChangePortError as error:
            raise _store_failure(error) from error


@dataclass(frozen=True, slots=True)
class DecideRegistryModelChange:
    store: RegistryModelChangeStorePort
    sources: RegistryModelReplacementSourcePort
    remediation: RegistryModelRemediationEvidencePort
    profile_witnesses: RegistryModelJoinProfileWitnessPort
    pointers: ActiveRegistryPointerReadPort
    versions: RegistryVersionReadPort
    dependencies: SemanticChangeDependencyIndexPort
    targets: ExecutionTargetResolverPort
    physical_bindings: RegistryPublicationPhysicalBindingAuthorityPort
    authorization: RegistryChangeAuthorizationPolicy
    clock: RegistryChangeClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        change_id: str,
        *,
        action: DecisionAction,
        expected_revision: int,
        confirmed_draft_fingerprint: str,
        rationale: str,
        idempotency_key: str,
    ) -> RegistryModelChangeMutation:
        now = self.clock.now()
        self.authorization.require(principal, RegistryChangePermission.DECIDE, at=now)
        digest = _idempotency_digest(idempotency_key)
        payload = {
            "action": action.value,
            "change_id": change_id,
            "confirmed_draft_fingerprint": confirmed_draft_fingerprint,
            "expected_revision": expected_revision,
            "rationale": rationale,
        }
        replay = _load_replay(
            self.store,
            self.authorization,
            principal,
            operation=_OP_DECIDE,
            permission=RegistryChangePermission.DECIDE,
            digest=digest,
            payload=payload,
            at=now,
        )
        if replay is not None:
            return RegistryModelChangeMutation(draft=replay.draft, replayed=True)
        draft = _load_draft(self.store, self.authorization, principal, change_id)
        self.authorization.require_resource(
            principal,
            draft,
            RegistryChangePermission.DECIDE,
            at=now,
        )
        actor_id = self.authorization.actor_id_for_workspace(principal, draft.workspace_id)
        source_actors = {
            draft.owner_actor_id,
            draft.source.proposal.prepared_by,
            *(item.actor_id for item in draft.source.decisions),
        }
        if self.authorization.actor_identity_is_one_of(
            principal,
            workspace_id=draft.workspace_id,
            actor_ids=source_actors,
        ):
            raise RegistryChangeAuthoringError(
                RegistryChangeAuthoringErrorCode.SEPARATION_OF_DUTIES,
                "Model replacement review requires a separate steward identity.",
            )
        if (
            draft.revision != expected_revision
            or draft.fingerprint != confirmed_draft_fingerprint
            or draft.status is not RegistryModelChangeStatus.NEEDS_REVIEW
        ):
            raise _conflict()
        _revalidate(
            draft,
            sources=self.sources,
            remediation=self.remediation,
            witnesses=self.profile_witnesses,
            pointers=self.pointers,
            versions=self.versions,
            dependencies=self.dependencies,
            targets=self.targets,
            physical_bindings=self.physical_bindings,
            at=now,
        )
        try:
            revised = decide_registry_model_change(
                draft,
                action=action,
                actor_id=actor_id,
                decided_at=now,
                rationale=rationale,
            )
        except ValueError as error:
            raise _invalid_request() from error
        audit = RegistryModelChangeAuditRecord.create(
            draft=revised,
            event=RegistryModelChangeAuditEvent.DECISION_RECORDED,
            actor_id=actor_id,
            occurred_at=now,
            source_revision=draft.revision,
            previous_fingerprint=draft.fingerprint,
        )
        request_fingerprint = _request_fingerprint(
            _OP_DECIDE,
            actor_id,
            draft.id,
            payload,
        )
        try:
            return self.store.commit_decision(
                revised,
                audit,
                expected_revision=draft.revision,
                operation=_OP_DECIDE,
                actor_id=actor_id,
                idempotency_digest=digest,
                request_fingerprint=request_fingerprint,
            )
        except RegistryModelChangePortError as error:
            raise _store_failure(error) from error


@dataclass(frozen=True, slots=True)
class PrepareRegistryModelChangePublication:
    store: RegistryModelChangeStorePort
    sources: RegistryModelReplacementSourcePort
    remediation: RegistryModelRemediationEvidencePort
    profile_witnesses: RegistryModelJoinProfileWitnessPort
    pointers: ActiveRegistryPointerReadPort
    versions: RegistryVersionReadPort
    dependencies: SemanticChangeDependencyIndexPort
    targets: ExecutionTargetResolverPort
    physical_bindings: RegistryPublicationPhysicalBindingAuthorityPort
    authorization: RegistryChangeAuthorizationPolicy
    clock: RegistryChangeClockPort
    publisher_max_session_age: timedelta = timedelta(minutes=15)

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        change_id: str,
        *,
        expected_revision: int,
        confirmed_draft_fingerprint: str,
        idempotency_key: str,
    ) -> RegistryModelChangeMutation:
        now = self.clock.now()
        self.authorization.require(
            principal,
            RegistryChangePermission.PREPARE_PUBLICATION,
            at=now,
        )
        digest = _idempotency_digest(idempotency_key)
        payload = {
            "change_id": change_id,
            "confirmed_draft_fingerprint": confirmed_draft_fingerprint,
            "expected_revision": expected_revision,
        }
        replay = _load_replay(
            self.store,
            self.authorization,
            principal,
            operation=_OP_PREPARE,
            permission=RegistryChangePermission.PREPARE_PUBLICATION,
            digest=digest,
            payload=payload,
            at=now,
        )
        if replay is not None:
            if replay.proposal is None:
                raise _service_unavailable()
            return RegistryModelChangeMutation(
                draft=replay.draft,
                proposal=replay.proposal,
                replayed=True,
            )
        draft = _load_draft(self.store, self.authorization, principal, change_id)
        self.authorization.require_resource(
            principal,
            draft,
            RegistryChangePermission.PREPARE_PUBLICATION,
            at=now,
        )
        actor_id = self.authorization.actor_id_for_workspace(principal, draft.workspace_id)
        if (
            draft.revision != expected_revision
            or draft.fingerprint != confirmed_draft_fingerprint
            or draft.status is not RegistryModelChangeStatus.APPROVED
            or draft.reviewed_by is None
        ):
            raise _conflict()
        _revalidate(
            draft,
            sources=self.sources,
            remediation=self.remediation,
            witnesses=self.profile_witnesses,
            pointers=self.pointers,
            versions=self.versions,
            dependencies=self.dependencies,
            targets=self.targets,
            physical_bindings=self.physical_bindings,
            at=now,
        )
        decision_actors = {
            draft.owner_actor_id,
            draft.reviewed_by,
            draft.source.proposal.prepared_by,
            draft.model_decision.actor,
            *(item.actor for item in draft.mapping_decisions),
            *(item.decision.actor for item in draft.incident_changes if hasattr(item, "decision")),
        }
        if (
            self.authorization.actor_identity_is_one_of(
                principal,
                workspace_id=draft.workspace_id,
                actor_ids=decision_actors,
            )
            or now - principal.authenticated_at > self.publisher_max_session_age
        ):
            raise RegistryChangeAuthoringError(
                RegistryChangeAuthoringErrorCode.SEPARATION_OF_DUTIES,
                "Managed preparation requires a fresh, separate publisher identity.",
            )
        try:
            ready, proposal = prepare_registry_model_change(
                draft,
                prepared_by=actor_id,
                prepared_at=now,
            )
        except ValueError as error:
            raise _not_ready() from error
        audit = RegistryModelChangeAuditRecord.create(
            draft=ready,
            event=RegistryModelChangeAuditEvent.PUBLICATION_PREPARED,
            actor_id=actor_id,
            occurred_at=now,
            source_revision=draft.revision,
            previous_fingerprint=draft.fingerprint,
            proposal_id=proposal.id,
        )
        request_fingerprint = _request_fingerprint(
            _OP_PREPARE,
            actor_id,
            draft.id,
            payload,
        )
        try:
            return self.store.commit_preparation(
                ready,
                proposal,
                audit,
                expected_revision=draft.revision,
                operation=_OP_PREPARE,
                actor_id=actor_id,
                idempotency_digest=digest,
                request_fingerprint=request_fingerprint,
            )
        except RegistryModelChangePortError as error:
            raise _store_failure(error) from error


@dataclass(frozen=True, slots=True)
class ListRegistryModelChanges:
    store: RegistryModelChangeStorePort
    authorization: RegistryChangeAuthorizationPolicy
    clock: RegistryChangeClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        limit: int = MAX_REGISTRY_MODEL_CHANGE_LIST_LIMIT,
    ) -> tuple[RegistryModelChangeDraft, ...]:
        if not 1 <= limit <= MAX_REGISTRY_MODEL_CHANGE_LIST_LIMIT:
            raise _invalid_request()
        collected: dict[tuple[str, str], RegistryModelChangeDraft] = {}
        try:
            for scope in self.authorization.list_scopes_for(principal, at=self.clock.now()):
                for item in self.store.list_for_workspace(
                    scope.workspace_id,
                    owner_actor_id=scope.owner_actor_id,
                    limit=limit,
                ):
                    key = (item.workspace_id, item.id)
                    if key in collected and collected[key] != item:
                        raise _service_unavailable()
                    collected[key] = item
        except RegistryModelChangePortError as error:
            raise _store_failure(error) from error
        return tuple(
            sorted(
                collected.values(),
                key=lambda item: (item.updated_at, item.id),
                reverse=True,
            )[:limit]
        )


@dataclass(frozen=True, slots=True)
class InspectRegistryModelChange:
    store: RegistryModelChangeStorePort
    authorization: RegistryChangeAuthorizationPolicy
    clock: RegistryChangeClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        change_id: str,
        *,
        history_limit: int = DEFAULT_REGISTRY_MODEL_CHANGE_HISTORY_LIMIT,
    ) -> RegistryModelChangeSnapshot:
        if not 1 <= history_limit <= MAX_REGISTRY_MODEL_CHANGE_HISTORY:
            raise _invalid_request()
        now = self.clock.now()
        draft = _load_draft(self.store, self.authorization, principal, change_id)
        self.authorization.require_resource(
            principal,
            draft,
            RegistryChangePermission.VIEW,
            at=now,
        )
        visible = RegistryChangePermission.AUDIT_VIEW in self.authorization.permissions_for(
            principal,
            at=now,
        )
        if not visible:
            return RegistryModelChangeSnapshot(draft=draft)
        try:
            audit = self.store.list_audit(
                draft.workspace_id,
                draft.id,
                limit=history_limit + 1,
            )
        except RegistryModelChangePortError as error:
            raise _store_failure(error) from error
        return RegistryModelChangeSnapshot(
            draft=draft,
            audit_visible=True,
            history_truncated=len(audit) > history_limit,
            audit=audit[-history_limit:],
        )


def _revalidate_profile_authoring(
    authoring: RegistryModelJoinProfileAuthoringRequest,
    *,
    sources: RegistryModelReplacementSourcePort,
    pointers: ActiveRegistryPointerReadPort,
    versions: RegistryVersionReadPort,
    dependencies: SemanticChangeDependencyIndexPort,
    targets: ExecutionTargetResolverPort,
    physical_bindings: RegistryPublicationPhysicalBindingAuthorityPort,
) -> None:
    request = authoring.request
    source = _load_source(
        sources,
        authoring.workspace_id,
        request.replacement_source.proposal.id,
    )
    if source != request.replacement_source:
        raise _stale_base()
    pointer, registry, base_registry = _load_active(
        pointers,
        versions,
        request.base.scope,
    )
    context = _current_dependency_context(
        dependencies,
        pointer=pointer,
        registry=registry,
    )
    try:
        current = resolve_registry_model_replacement_base(
            scope=request.base.scope,
            base=registry,
            base_registry=base_registry,
            target_model_id=request.base.target_model.id,
            dependency_context=context,
        )
    except ValueError as error:
        raise _stale_base() from error
    if current != request.base:
        raise _stale_base()
    _require_replacement_bindings(physical_bindings, request.base.scope, source)
    target = _resolve_target(
        targets,
        workspace_id=authoring.workspace_id,
        connection_id=request.connection_id,
    )
    if SemanticJoinProfileTargetRef.from_target(target) != request.execution_target:
        raise _stale_target()


def _profile_job_matches(
    job: SemanticJoinProfileJob,
    authoring: RegistryModelJoinProfileAuthoringRequest,
) -> bool:
    request = authoring.request
    return (
        job.workspace_id == authoring.workspace_id
        and job.scan_id == request.scan_id
        and job.bound_proposal == request.bound_proposal
        and job.execution_target == request.execution_target
        and job.requested_at == request.requested_at
    )


def _load_profile_authoring(
    store: RegistryModelProfileStorePort,
    authorization: RegistryChangeAuthorizationPolicy,
    principal: AuthenticatedPrincipal,
    request_id: str,
) -> RegistryModelJoinProfileAuthoringRequest:
    matches: list[RegistryModelJoinProfileAuthoringRequest] = []
    try:
        for workspace_id in authorization.workspace_ids_for_principal(principal):
            value = store.load_request(workspace_id, request_id)
            if value is not None:
                matches.append(value)
    except RegistryModelChangePortError as error:
        raise _store_failure(error) from error
    if len(matches) != 1:
        if len(matches) > 1:
            raise _service_unavailable()
        raise _unavailable()
    return matches[0]


def _load_profile_replay(
    store: RegistryModelProfileStorePort,
    authorization: RegistryChangeAuthorizationPolicy,
    principal: AuthenticatedPrincipal,
    *,
    operation: str,
    digest: str,
    payload: object,
    at: datetime,
) -> RegistryModelProfileOperationReplay | None:
    values: list[RegistryModelProfileOperationReplay] = []
    try:
        for workspace_id in authorization.workspace_ids_for_principal(principal):
            value = store.load_operation_replay(workspace_id, digest)
            if value is not None:
                values.append(value)
    except RegistryModelChangePortError as error:
        raise _store_failure(error) from error
    if not values:
        return None
    if len(values) != 1:
        raise _service_unavailable()
    replay = values[0]
    authorization.require_resource(
        principal,
        replay.authoring,
        (
            RegistryChangePermission.REQUEST_PROFILE
            if operation == _OP_PROFILE_REQUEST
            else RegistryChangePermission.FINALIZE_DRAFT
        ),
        at=at,
    )
    if (
        replay.operation != operation
        or not authorization.actor_identity_is_one_of(
            principal,
            workspace_id=replay.authoring.workspace_id,
            actor_ids=(replay.actor_id,),
        )
        or replay.request_fingerprint
        != _profile_request_fingerprint(
            operation,
            replay.actor_id,
            replay.authoring.id,
            payload,
        )
    ):
        raise _conflict()
    return replay


def _profile_request_fingerprint(
    operation: str,
    actor_id: str,
    request_id: str,
    payload: object,
) -> str:
    return registry_model_authoring_fingerprint(
        {
            "actor_id": actor_id,
            "operation": operation,
            "request_id": request_id,
            "payload": payload,
        }
    )


def _revalidate(
    draft: RegistryModelChangeDraft,
    *,
    sources: RegistryModelReplacementSourcePort,
    remediation: RegistryModelRemediationEvidencePort,
    witnesses: RegistryModelJoinProfileWitnessPort,
    pointers: ActiveRegistryPointerReadPort,
    versions: RegistryVersionReadPort,
    dependencies: SemanticChangeDependencyIndexPort,
    targets: ExecutionTargetResolverPort,
    physical_bindings: RegistryPublicationPhysicalBindingAuthorityPort,
    at: datetime,
) -> None:
    source = _load_source(sources, draft.workspace_id, draft.source.proposal.id)
    if source != draft.source:
        raise _stale_base()
    pointer, registry, base_registry = _load_active(pointers, versions, draft.base.scope)
    context = _current_dependency_context(
        dependencies,
        pointer=pointer,
        registry=registry,
    )
    try:
        current = resolve_registry_model_replacement_base(
            scope=draft.base.scope,
            base=registry,
            base_registry=base_registry,
            target_model_id=draft.base.target_model.id,
            dependency_context=context,
        )
    except ValueError as error:
        raise _stale_base() from error
    if current != draft.base:
        raise _stale_base()
    _require_replacement_bindings(physical_bindings, draft.base.scope, source)
    if draft.authority.kind is RegistryModelChangeKind.M26_REMEDIATION:
        report = draft.authority.report
        impacts = draft.authority.impacts
        if report is None or impacts is None:
            raise _stale_base()
        loaded = _load_remediation(
            remediation,
            draft.base.scope,
            report.id,
            draft.base.dependency_context,
        )
        if loaded != (report, impacts):
            raise _stale_base()
    for intent in draft.incident_intents:
        witness = intent.profile_witness
        if witness is None:
            continue
        current_witness = _load_witness(
            witnesses,
            draft.workspace_id,
            draft.id,
            draft.source.proposal.id,
            intent.base_join_id,
        )
        if current_witness != witness or not witness.is_current(at):
            raise _profile_stale()
        _require_witness_authority(
            witness,
            targets=targets,
            physical_bindings=physical_bindings,
        )


def _resolve_incident_intents(
    request: CreateRegistryModelChangeInput,
    *,
    source: RegistryModelReplacementSourceEvidence,
    base: object,
    witnesses: RegistryModelJoinProfileWitnessPort,
    targets: ExecutionTargetResolverPort,
    physical_bindings: RegistryPublicationPhysicalBindingAuthorityPort,
    at: datetime,
) -> tuple[RegistryModelIncidentIntent, ...]:
    from schemabridge.domain.registry_model_changes import RegistryModelReplacementBase

    if not isinstance(base, RegistryModelReplacementBase):
        raise _service_unavailable()
    result: list[RegistryModelIncidentIntent] = []
    for item in request.incident_joins:
        if isinstance(item, RegistryIncidentJoinPreserveInput):
            result.append(
                RegistryModelIncidentIntent(
                    action=item.action,
                    base_join_id=item.join_id,
                )
            )
        elif isinstance(item, RegistryIncidentJoinRemovalInput):
            result.append(
                RegistryModelIncidentIntent(
                    action=item.action,
                    base_join_id=item.join_id,
                    risks=item.risks,
                )
            )
        elif isinstance(item, RegistryIncidentJoinUpsertInput):
            witness = _load_witness(
                witnesses,
                base.scope.workspace_id,
                request.change_id,
                source.proposal.id,
                item.join_id,
            )
            if (
                witness.fingerprint != item.expected_profile_witness_fingerprint
                or witness.change_id != request.change_id
                or witness.source_replacement_proposal_id != source.proposal.id
                or witness.base_fingerprint != base.fingerprint
                or witness.proposal != item.proposal
                or not witness.is_current(at)
            ):
                raise _profile_stale()
            _require_witness_authority(
                witness,
                targets=targets,
                physical_bindings=physical_bindings,
            )
            result.append(
                RegistryModelIncidentIntent(
                    action=item.action,
                    base_join_id=item.join_id,
                    proposal=item.proposal,
                    profile_witness=witness,
                    risks=item.risks,
                )
            )
    return tuple(result)


def _resolve_authority(
    remediation: RegistryModelRemediationEvidencePort,
    *,
    request: CreateRegistryModelChangeInput,
    base: object,
) -> RegistryModelChangeAuthority:
    from schemabridge.domain.registry_model_changes import RegistryModelReplacementBase

    if not isinstance(base, RegistryModelReplacementBase):
        raise _service_unavailable()
    if request.kind is RegistryModelChangeKind.PLANNED_CHANGE:
        return RegistryModelChangeAuthority.planned(base)
    assert request.remediation_report_id is not None
    assert request.expected_remediation_report_fingerprint is not None
    report, impacts = _load_remediation(
        remediation,
        base.scope,
        request.remediation_report_id,
        base.dependency_context,
    )
    if report.fingerprint != request.expected_remediation_report_fingerprint:
        raise _stale_base()
    try:
        return RegistryModelChangeAuthority.remediation(
            base,
            report=report,
            impacts=impacts,
            resolved_finding_ids=request.resolved_finding_ids,
        )
    except ValueError as error:
        raise _stale_base() from error


def _require_replacement_bindings(
    authority: RegistryPublicationPhysicalBindingAuthorityPort,
    scope: SemanticRegistryScope,
    source: RegistryModelReplacementSourceEvidence,
) -> None:
    try:
        _, _, bindings = registry_model_replacement_artifacts(source.proposal)
        authority.require_current(scope, bindings)
    except Exception as error:
        raise _stale_catalog() from error


def _require_witness_authority(
    witness: RegistryModelJoinProfileWitness,
    *,
    targets: ExecutionTargetResolverPort,
    physical_bindings: RegistryPublicationPhysicalBindingAuthorityPort,
) -> None:
    try:
        target = targets.resolve_current(
            workspace_id=witness.scope.workspace_id,
            connection_id=witness.connection_id,
        )
        if SemanticJoinProfileTargetRef.from_target(target) != witness.execution_target:
            raise ValueError("profile target changed")
        physical_bindings.require_current(
            witness.scope,
            (witness.left.binding, witness.right.binding),
        )
    except Exception as error:
        raise _profile_stale() from error


def _resolve_target(
    targets: ExecutionTargetResolverPort,
    *,
    workspace_id: str,
    connection_id: CatalogConnectionId,
) -> GovernedExecutionTarget:
    try:
        target = targets.resolve_current(
            workspace_id=workspace_id,
            connection_id=connection_id,
        )
    except Exception as error:
        raise _stale_target() from error
    if not isinstance(target, GovernedExecutionTarget):
        raise _service_unavailable()
    return target


def _current_dependency_context(
    dependencies: SemanticChangeDependencyIndexPort,
    *,
    pointer: ActiveRegistryPointer,
    registry: GovernedSemanticRegistrySnapshot,
) -> SemanticChangeInspectionContext:
    try:
        state = dependencies.load_state(pointer.scope)
    except SemanticChangePortError as error:
        raise _stale_base() from error
    if not state.complete:
        raise _stale_base()
    try:
        return SemanticChangeInspectionContext.create(
            scope=pointer.scope,
            pointer_generation=pointer.generation,
            pointer_fingerprint=registry_projection_fingerprint(pointer),
            pointer_transition_id=pointer.transition_id,
            registry_version=registry.version,
            registry_fingerprint=registry.fingerprint,
            mappings=tuple(
                GovernedMappingRef(
                    logical_field=item.mapping.logical_field,
                    physical_field=item.mapping.physical_field,
                    version=item.mapping.version,
                    approval_decision_id=_required_decision(item.approval_decision_id),
                    physical_type=item.physical_type,
                )
                for item in registry.mapping_set.mappings
            ),
            joins=tuple(
                GovernedJoinRef(
                    contract_id=item.id,
                    version=item.version,
                    approval_decision_id=_required_decision(item.approval_decision_id),
                    left_field=item.left_key.physical_field,
                    right_field=item.right_key.physical_field,
                    cardinality=item.cardinality,
                    fanout_policy=item.fanout_policy,
                )
                for item in registry.join_contracts.contracts
            ),
            dependency_index=state,
        )
    except ValueError as error:
        raise _stale_base() from error


def _load_active(
    pointers: ActiveRegistryPointerReadPort,
    versions: RegistryVersionReadPort,
    scope: SemanticRegistryScope,
) -> tuple[ActiveRegistryPointer, GovernedSemanticRegistrySnapshot, OnboardingRegistryBase]:
    try:
        pointer = pointers.load_active(scope)
        if pointer is None or pointer.scope != scope:
            raise _stale_base()
        version = versions.load_version(scope, pointer.registry_version)
    except RegistryChangeAuthoringError:
        raise
    except RegistryControlError as error:
        raise _stale_base() from error
    registry = version.snapshot.registry
    if (
        version.trust is not RegistryVersionTrust.STRICT
        or version.snapshot.scope != scope
        or registry.format_version != 2
        or registry.version != pointer.registry_version
        or registry.fingerprint != pointer.registry_fingerprint
    ):
        raise _stale_base()
    return (
        pointer,
        registry,
        OnboardingRegistryBase(
            registry_version=pointer.registry_version,
            registry_fingerprint=pointer.registry_fingerprint,
            activation_generation=pointer.generation,
            active_pointer_fingerprint=registry_projection_fingerprint(pointer),
        ),
    )


def _load_source(
    sources: RegistryModelReplacementSourcePort,
    workspace_id: str,
    proposal_id: str,
) -> RegistryModelReplacementSourceEvidence:
    try:
        value = sources.load(workspace_id, proposal_id)
    except Exception as error:
        raise _service_unavailable() from error
    if value is None:
        raise _unavailable()
    try:
        checked = RegistryModelReplacementSourceEvidence.model_validate(
            value.model_dump(mode="python")
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise _service_unavailable() from error
    if checked != value:
        raise _service_unavailable()
    return checked


def _load_remediation(
    source: RegistryModelRemediationEvidencePort,
    scope: SemanticRegistryScope,
    report_id: str,
    expected_context: SemanticChangeInspectionContext,
) -> tuple[SemanticChangeReport, SemanticImpactSet]:
    try:
        value = source.load_current(scope, report_id, expected_context)
    except Exception as error:
        raise _service_unavailable() from error
    if value is None:
        raise _stale_base()
    return value


def _load_witness(
    source: RegistryModelJoinProfileWitnessPort,
    workspace_id: str,
    change_id: str,
    proposal_id: str,
    join_id: str,
) -> RegistryModelJoinProfileWitness:
    try:
        value = source.load(workspace_id, change_id, proposal_id, join_id)
    except Exception as error:
        raise _service_unavailable() from error
    if value is None:
        raise _profile_stale()
    try:
        checked = RegistryModelJoinProfileWitness.model_validate(value.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as error:
        raise _service_unavailable() from error
    if checked != value:
        raise _service_unavailable()
    return checked


def _load_draft(
    store: RegistryModelChangeStorePort,
    authorization: RegistryChangeAuthorizationPolicy,
    principal: AuthenticatedPrincipal,
    change_id: str,
) -> RegistryModelChangeDraft:
    matches: list[RegistryModelChangeDraft] = []
    try:
        for workspace_id in authorization.workspace_ids_for_principal(principal):
            value = store.load(workspace_id, change_id)
            if value is not None:
                matches.append(value)
    except RegistryModelChangePortError as error:
        raise _store_failure(error) from error
    if len(matches) != 1:
        if len(matches) > 1:
            raise _service_unavailable()
        raise _unavailable()
    return matches[0]


def _load_replay(
    store: RegistryModelChangeStorePort,
    authorization: RegistryChangeAuthorizationPolicy,
    principal: AuthenticatedPrincipal,
    *,
    operation: str,
    permission: RegistryChangePermission,
    digest: str,
    payload: object,
    at: datetime,
) -> RegistryModelChangeOperationReplay | None:
    values: list[RegistryModelChangeOperationReplay] = []
    try:
        for workspace_id in authorization.workspace_ids_for_principal(principal):
            value = store.load_operation_replay(workspace_id, digest)
            if value is not None:
                values.append(value)
    except RegistryModelChangePortError as error:
        raise _store_failure(error) from error
    if not values:
        return None
    if len(values) != 1:
        raise _service_unavailable()
    replay = values[0]
    authorization.require_resource(
        principal,
        replay.draft,
        permission,
        at=at,
    )
    if (
        replay.operation != operation
        or not authorization.actor_identity_is_one_of(
            principal,
            workspace_id=replay.draft.workspace_id,
            actor_ids=(replay.actor_id,),
        )
        or replay.request_fingerprint
        != _request_fingerprint(
            operation,
            replay.actor_id,
            replay.draft.id,
            payload,
        )
    ):
        raise _conflict()
    return replay


def _request_fingerprint(
    operation: str,
    actor_id: str,
    change_id: str,
    payload: object,
) -> str:
    return registry_model_authoring_fingerprint(
        {
            "actor_id": actor_id,
            "change_id": change_id,
            "operation": operation,
            "payload": payload,
        }
    )


def _idempotency_digest(value: str) -> str:
    if not isinstance(value, str) or _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise _invalid_request()
    return hashlib.sha256(value.encode()).hexdigest()


def _required_decision(value: str | None) -> str:
    if value is None:
        raise ValueError("active registry artifact lacks decision authority")
    return value


def _store_failure(error: RegistryModelChangePortError) -> RegistryChangeAuthoringError:
    if error.code is RegistryModelChangePortErrorCode.CONFLICT:
        return _conflict()
    if error.code is RegistryModelChangePortErrorCode.NOT_FOUND:
        return _unavailable()
    return _service_unavailable()


def _invalid_request() -> RegistryChangeAuthoringError:
    return RegistryChangeAuthoringError(
        RegistryChangeAuthoringErrorCode.INVALID_REQUEST,
        "The registry model change request is invalid.",
    )


def _unavailable() -> RegistryChangeAuthoringError:
    return RegistryChangeAuthoringError(
        RegistryChangeAuthoringErrorCode.UNAVAILABLE,
        "The registry model change resource is not available.",
    )


def _conflict() -> RegistryChangeAuthoringError:
    return RegistryChangeAuthoringError(
        RegistryChangeAuthoringErrorCode.CONFLICT,
        "The registry model change state changed; reload and retry.",
    )


def _stale_base() -> RegistryChangeAuthoringError:
    return RegistryChangeAuthoringError(
        RegistryChangeAuthoringErrorCode.STALE_BASE,
        "The registry model change authority is stale.",
    )


def _stale_catalog() -> RegistryChangeAuthoringError:
    return RegistryChangeAuthoringError(
        RegistryChangeAuthoringErrorCode.STALE_CATALOG,
        "The replacement catalog evidence is stale.",
    )


def _profile_stale() -> RegistryChangeAuthoringError:
    return RegistryChangeAuthoringError(
        RegistryChangeAuthoringErrorCode.PROFILE_STALE,
        "The replacement join profile is stale.",
    )


def _stale_target() -> RegistryChangeAuthoringError:
    return RegistryChangeAuthoringError(
        RegistryChangeAuthoringErrorCode.STALE_TARGET,
        "The governed replacement profile target changed.",
    )


def _profile_queue_failure(
    error: SemanticJoinProfileQueueError,
) -> RegistryChangeAuthoringError:
    if error.code in {
        SemanticJoinProfileQueueErrorCode.IDEMPOTENCY_CONFLICT,
        SemanticJoinProfileQueueErrorCode.STATE_CONFLICT,
    }:
        return _conflict()
    if error.code is SemanticJoinProfileQueueErrorCode.NOT_FOUND:
        return RegistryChangeAuthoringError(
            RegistryChangeAuthoringErrorCode.PROFILE_NOT_READY,
            "The exact replacement relationship profile is not complete.",
        )
    return _service_unavailable()


def _not_ready() -> RegistryChangeAuthoringError:
    return RegistryChangeAuthoringError(
        RegistryChangeAuthoringErrorCode.NOT_READY,
        "The registry model change is not ready for publication.",
    )


def _service_unavailable() -> RegistryChangeAuthoringError:
    return RegistryChangeAuthoringError(
        RegistryChangeAuthoringErrorCode.SERVICE_UNAVAILABLE,
        "The registry model change service is unavailable.",
    )


__all__ = [
    "DEFAULT_REGISTRY_MODEL_CHANGE_HISTORY_LIMIT",
    "CreateRegistryModelChange",
    "DecideRegistryModelChange",
    "FinalizeRegistryModelJoinProfile",
    "InspectRegistryModelChange",
    "ListRegistryModelChanges",
    "PrepareRegistryModelChangePublication",
    "RequestRegistryModelJoinProfile",
]
