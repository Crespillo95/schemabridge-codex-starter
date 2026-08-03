"""Authenticated fail-closed use cases for M35 Phase-A join-change authoring."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from schemabridge.application.ports.connectors import ExecutionTargetResolverPort
from schemabridge.application.ports.registry_changes import (
    RegistryChangeClockPort,
    RegistryChangeOperationReplay,
    RegistryChangeStoreError,
    RegistryChangeStoreErrorCode,
    RegistryChangeStorePort,
    RegistryJoinProfileRequestQueuePort,
)
from schemabridge.application.ports.registry_control import (
    ActiveRegistryPointerReadPort,
    RegistryControlError,
    RegistryVersionReadPort,
)
from schemabridge.application.ports.registry_publication import (
    RegistryPublicationPhysicalBindingAuthorityPort,
)
from schemabridge.application.ports.semantic_profile_jobs import (
    SemanticJoinProfileQueueError,
    SemanticJoinProfileQueueErrorCode,
)
from schemabridge.application.registry_change_authorization import (
    RegistryChangeAuthorizationPolicy,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import GovernedExecutionTarget
from schemabridge.domain.decisions import DecisionAction
from schemabridge.domain.identity import AuthenticatedPrincipal
from schemabridge.domain.registry_change_authoring import (
    MAX_REGISTRY_CHANGE_HISTORY,
    RegistryChangeAuditEvent,
    RegistryChangeAuditRecord,
    RegistryChangePermission,
    RegistryJoinChangeSnapshot,
    RegistryJoinDraftMutation,
    RegistryJoinPreparation,
    RegistryJoinProfileAuthoringMutation,
    RegistryJoinProfileAuthoringRequest,
    RequestRegistryJoinProfileInput,
    registry_change_authoring_fingerprint,
)
from schemabridge.domain.registry_changes import (
    PreparedRegistryJoinProposal,
    RegistryJoinBaseEvidence,
    RegistryJoinChangeDraft,
    RegistryJoinChangeStatus,
    RegistryJoinProfileRequest,
    decide_registry_join_change,
    mark_registry_join_change_ready,
    resolve_registry_join_base_evidence,
    validate_registry_join_profile_campaign,
)
from schemabridge.domain.registry_control import (
    RegistryVersionTrust,
    registry_projection_fingerprint,
)
from schemabridge.domain.semantic_onboarding import OnboardingRegistryBase
from schemabridge.domain.semantic_profile_jobs import (
    SemanticJoinProfileJob,
    SemanticJoinProfileJobStatus,
    SemanticJoinProfileProposal,
    SemanticJoinProfileTargetRef,
)
from schemabridge.domain.semantic_registry import (
    GovernedSemanticRegistrySnapshot,
    SemanticRegistryScope,
)

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
_OP_REQUEST = "request_profile"
_OP_FINALIZE = "finalize_draft"
_OP_DECIDE = "record_decision"
_OP_PREPARE = "prepare_publication"
DEFAULT_REGISTRY_CHANGE_HISTORY_LIMIT = 25
MAX_REGISTRY_CHANGE_LIST_LIMIT = 50


class RegistryChangeAuthoringErrorCode(StrEnum):
    INVALID_REQUEST = "registry_change_invalid_request"
    UNAVAILABLE = "registry_change_resource_unavailable"
    CONFLICT = "registry_change_conflict"
    STALE_BASE = "registry_change_stale_base"
    STALE_CATALOG = "registry_change_stale_catalog"
    STALE_TARGET = "registry_change_stale_target"
    PROFILE_NOT_READY = "registry_change_profile_not_ready"
    PROFILE_STALE = "registry_change_profile_stale"
    NOT_READY = "registry_change_not_ready"
    SEPARATION_OF_DUTIES = "registry_change_separation_of_duties"
    SERVICE_UNAVAILABLE = "registry_change_service_unavailable"


class RegistryChangeAuthoringError(RuntimeError):
    def __init__(self, code: RegistryChangeAuthoringErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RequestRegistryJoinProfile:
    store: RegistryChangeStorePort
    pointers: ActiveRegistryPointerReadPort
    versions: RegistryVersionReadPort
    targets: ExecutionTargetResolverPort
    physical_bindings: RegistryPublicationPhysicalBindingAuthorityPort
    profiles: RegistryJoinProfileRequestQueuePort
    authorization: RegistryChangeAuthorizationPolicy
    clock: RegistryChangeClockPort
    scope: SemanticRegistryScope

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        request: RequestRegistryJoinProfileInput,
        *,
        idempotency_key: str,
    ) -> RegistryJoinProfileAuthoringMutation:
        now = self.clock.now()
        self.authorization.require(
            principal,
            RegistryChangePermission.REQUEST_PROFILE,
            at=now,
        )
        digest = _idempotency_digest(idempotency_key)
        request_payload = request.model_dump(mode="json")
        replay = _load_operation_replay(
            self.store,
            self.authorization,
            principal,
            operation=_OP_REQUEST,
            idempotency_digest=digest,
            request_payload=request_payload,
            at=now,
        )
        if replay is not None:
            if replay.job is not None:
                return RegistryJoinProfileAuthoringMutation(
                    authoring=replay.authoring,
                    job=replay.job,
                    replayed=True,
                )
            return self._enqueue_and_bind(
                replay.authoring,
                actor_id=replay.actor_id,
                idempotency_digest=digest,
                bound_at=now,
            )

        _require_scope(principal, self.scope)
        base, base_registry = _load_active_base(
            self.pointers,
            self.versions,
            self.scope,
        )
        if base_registry != request.expected_base_registry:
            raise _stale_base()
        bound_proposal = SemanticJoinProfileProposal(
            connection_id=request.connection_id,
            proposal=request.proposal,
        )
        evidence = resolve_registry_join_base_evidence(
            scope=self.scope,
            base=base,
            base_registry=base_registry,
            proposal=bound_proposal,
        )
        _require_current_bindings(self.physical_bindings, self.scope, evidence)
        target = _resolve_target(
            self.targets,
            workspace_id=principal.workspace_id,
            connection_id=request.connection_id,
        )
        if target.fingerprint != request.expected_execution_target_fingerprint:
            raise _stale_target()
        target_ref = SemanticJoinProfileTargetRef.from_target(target)
        profile_request = RegistryJoinProfileRequest.create(
            scope=self.scope,
            base_evidence=evidence,
            proposal=bound_proposal,
            execution_target=target_ref,
            requested_at=now,
        )
        authoring = RegistryJoinProfileAuthoringRequest.create(
            id=request.change_id,
            workspace_id=principal.workspace_id,
            owner_actor_id=principal.actor_id,
            request=profile_request,
            created_at=now,
        )
        request_fingerprint = _request_fingerprint(
            _OP_REQUEST,
            principal.actor_id,
            authoring,
            request_payload,
        )
        audit = _audit(
            authoring=authoring,
            event=RegistryChangeAuditEvent.PROFILE_REQUESTED,
            actor_id=principal.actor_id,
            occurred_at=now,
            source_revision=0,
            resulting_revision=0,
            previous_fingerprint=None,
            resulting_fingerprint=authoring.fingerprint,
            profile_request_fingerprint=profile_request.fingerprint,
        )
        try:
            persisted = self.store.persist_profile_request(
                authoring,
                audit,
                operation=_OP_REQUEST,
                actor_id=principal.actor_id,
                idempotency_digest=digest,
                request_fingerprint=request_fingerprint,
            )
        except RegistryChangeStoreError as error:
            raise _store_failure(error) from error
        if persisted.authoring != authoring or persisted.job is not None:
            raise _service_unavailable()
        return self._enqueue_and_bind(
            authoring,
            actor_id=principal.actor_id,
            idempotency_digest=digest,
            bound_at=now,
        )

    def _enqueue_and_bind(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
        *,
        actor_id: str,
        idempotency_digest: str,
        bound_at: datetime,
    ) -> RegistryJoinProfileAuthoringMutation:
        request = authoring.request
        _revalidate_profile_request_authority(
            authoring,
            pointers=self.pointers,
            versions=self.versions,
            targets=self.targets,
            physical_bindings=self.physical_bindings,
        )
        try:
            submission = self.profiles.enqueue(authoring)
        except SemanticJoinProfileQueueError as error:
            raise _profile_queue_failure(error) from error
        job = submission.job
        if not _job_matches_request(job, authoring):
            raise _service_unavailable()
        audit = _audit(
            authoring=authoring,
            event=RegistryChangeAuditEvent.PROFILE_JOB_BOUND,
            actor_id=actor_id,
            occurred_at=bound_at,
            source_revision=0,
            resulting_revision=0,
            previous_fingerprint=authoring.fingerprint,
            resulting_fingerprint=authoring.fingerprint,
            profile_request_fingerprint=request.fingerprint,
            profile_job_id=job.job_id,
        )
        try:
            bound = self.store.bind_profile_job(
                authoring,
                job,
                audit,
                idempotency_digest=idempotency_digest,
            )
        except RegistryChangeStoreError as error:
            raise _store_failure(error) from error
        if bound.authoring != authoring or bound.job != job:
            raise _service_unavailable()
        return RegistryJoinProfileAuthoringMutation(
            authoring=bound.authoring,
            job=bound.job,
            replayed=submission.replayed or bound.replayed,
        )


@dataclass(frozen=True, slots=True)
class FinalizeRegistryJoinChangeDraft:
    store: RegistryChangeStorePort
    pointers: ActiveRegistryPointerReadPort
    versions: RegistryVersionReadPort
    targets: ExecutionTargetResolverPort
    physical_bindings: RegistryPublicationPhysicalBindingAuthorityPort
    profiles: RegistryJoinProfileRequestQueuePort
    authorization: RegistryChangeAuthorizationPolicy
    clock: RegistryChangeClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        change_id: str,
        *,
        confirmed_authoring_fingerprint: str,
        idempotency_key: str,
    ) -> RegistryJoinDraftMutation:
        now = self.clock.now()
        self.authorization.require(
            principal,
            RegistryChangePermission.FINALIZE_DRAFT,
            at=now,
        )
        digest = _idempotency_digest(idempotency_key)
        payload = {
            "change_id": change_id,
            "confirmed_authoring_fingerprint": confirmed_authoring_fingerprint,
        }
        replay = _load_operation_replay(
            self.store,
            self.authorization,
            principal,
            operation=_OP_FINALIZE,
            idempotency_digest=digest,
            request_payload=payload,
            at=now,
        )
        if replay is not None:
            if replay.draft is None:
                raise _service_unavailable()
            return RegistryJoinDraftMutation(
                authoring=replay.authoring,
                draft=replay.draft,
                replayed=True,
            )
        authoring = _load_authoring(
            self.store,
            self.authorization,
            principal,
            change_id,
        )
        self.authorization.require_resource(
            principal,
            authoring,
            RegistryChangePermission.FINALIZE_DRAFT,
            at=now,
        )
        actor_id = self.authorization.actor_id_for_workspace(
            principal,
            authoring.workspace_id,
        )
        if authoring.fingerprint != confirmed_authoring_fingerprint:
            raise _conflict()
        base, job = _revalidate_authority(
            authoring,
            draft=None,
            pointers=self.pointers,
            versions=self.versions,
            targets=self.targets,
            physical_bindings=self.physical_bindings,
            profiles=self.profiles,
            at=now,
        )
        try:
            draft = RegistryJoinChangeDraft.create(
                id=authoring.id,
                workspace_id=authoring.workspace_id,
                owner_actor_id=authoring.owner_actor_id,
                scope=authoring.request.scope,
                base=base,
                base_registry=authoring.request.base_evidence.base_registry,
                profile_job=job,
                created_at=now,
            )
        except ValueError as error:
            raise _profile_stale() from error
        if draft.profile_campaign.request != authoring.request:
            raise _profile_stale()
        audit = _audit(
            authoring=authoring,
            event=RegistryChangeAuditEvent.DRAFT_FINALIZED,
            actor_id=actor_id,
            occurred_at=now,
            source_revision=0,
            resulting_revision=draft.revision,
            previous_fingerprint=authoring.fingerprint,
            resulting_fingerprint=draft.fingerprint,
            profile_request_fingerprint=authoring.request.fingerprint,
            profile_job_id=job.job_id,
        )
        request_fingerprint = _request_fingerprint(
            _OP_FINALIZE,
            actor_id,
            authoring,
            payload,
        )
        try:
            return self.store.commit_draft(
                authoring,
                draft,
                audit,
                operation=_OP_FINALIZE,
                actor_id=actor_id,
                idempotency_digest=digest,
                request_fingerprint=request_fingerprint,
            )
        except RegistryChangeStoreError as error:
            raise _store_failure(error) from error


@dataclass(frozen=True, slots=True)
class DecideRegistryJoinChange:
    store: RegistryChangeStorePort
    pointers: ActiveRegistryPointerReadPort
    versions: RegistryVersionReadPort
    targets: ExecutionTargetResolverPort
    physical_bindings: RegistryPublicationPhysicalBindingAuthorityPort
    profiles: RegistryJoinProfileRequestQueuePort
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
    ) -> RegistryJoinDraftMutation:
        now = self.clock.now()
        self.authorization.require(
            principal,
            RegistryChangePermission.DECIDE,
            at=now,
        )
        digest = _idempotency_digest(idempotency_key)
        payload = {
            "action": action.value,
            "change_id": change_id,
            "confirmed_draft_fingerprint": confirmed_draft_fingerprint,
            "expected_revision": expected_revision,
            "rationale": rationale,
        }
        replay = _load_operation_replay(
            self.store,
            self.authorization,
            principal,
            operation=_OP_DECIDE,
            idempotency_digest=digest,
            request_payload=payload,
            at=now,
        )
        if replay is not None:
            if replay.draft is None:
                raise _service_unavailable()
            return RegistryJoinDraftMutation(
                authoring=replay.authoring,
                draft=replay.draft,
                replayed=True,
            )
        authoring, draft = _load_draft_resource(
            self.store,
            self.authorization,
            principal,
            change_id,
        )
        self.authorization.require_resource(
            principal,
            authoring,
            RegistryChangePermission.DECIDE,
            at=now,
        )
        actor_id = self.authorization.actor_id_for_workspace(
            principal,
            authoring.workspace_id,
        )
        if (
            draft.status is not RegistryJoinChangeStatus.NEEDS_REVIEW
            or draft.revision != expected_revision
            or draft.fingerprint != confirmed_draft_fingerprint
        ):
            raise _conflict()
        _revalidate_authority(
            authoring,
            draft=draft,
            pointers=self.pointers,
            versions=self.versions,
            targets=self.targets,
            physical_bindings=self.physical_bindings,
            profiles=self.profiles,
            at=now,
        )
        try:
            revised = decide_registry_join_change(
                draft,
                action=action,
                expected_revision=expected_revision,
                actor_id=actor_id,
                decided_at=now,
                rationale=rationale,
            )
        except ValueError as error:
            raise _invalid_request() from error
        assert revised.decision is not None
        audit = _audit(
            authoring=authoring,
            event=RegistryChangeAuditEvent.DECISION_RECORDED,
            actor_id=actor_id,
            occurred_at=now,
            source_revision=draft.revision,
            resulting_revision=revised.revision,
            previous_fingerprint=draft.fingerprint,
            resulting_fingerprint=revised.fingerprint,
            decision_id=revised.decision.id,
        )
        request_fingerprint = _request_fingerprint(
            _OP_DECIDE,
            actor_id,
            authoring,
            payload,
        )
        try:
            return self.store.commit_decision(
                authoring,
                revised,
                audit,
                expected_revision=draft.revision,
                operation=_OP_DECIDE,
                actor_id=actor_id,
                idempotency_digest=digest,
                request_fingerprint=request_fingerprint,
            )
        except RegistryChangeStoreError as error:
            raise _store_failure(error) from error


@dataclass(frozen=True, slots=True)
class PrepareRegistryJoinChangePublication:
    store: RegistryChangeStorePort
    pointers: ActiveRegistryPointerReadPort
    versions: RegistryVersionReadPort
    targets: ExecutionTargetResolverPort
    physical_bindings: RegistryPublicationPhysicalBindingAuthorityPort
    profiles: RegistryJoinProfileRequestQueuePort
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
    ) -> RegistryJoinPreparation:
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
        replay = _load_operation_replay(
            self.store,
            self.authorization,
            principal,
            operation=_OP_PREPARE,
            idempotency_digest=digest,
            request_payload=payload,
            at=now,
        )
        if replay is not None:
            if replay.draft is None or replay.proposal is None:
                raise _service_unavailable()
            return RegistryJoinPreparation(
                authoring=replay.authoring,
                draft=replay.draft,
                proposal=replay.proposal,
                replayed=True,
            )
        authoring, draft = _load_draft_resource(
            self.store,
            self.authorization,
            principal,
            change_id,
        )
        self.authorization.require_resource(
            principal,
            authoring,
            RegistryChangePermission.PREPARE_PUBLICATION,
            at=now,
        )
        actor_id = self.authorization.actor_id_for_workspace(
            principal,
            authoring.workspace_id,
        )
        if (
            draft.status is not RegistryJoinChangeStatus.NEEDS_REVIEW
            or draft.revision != expected_revision
            or draft.fingerprint != confirmed_draft_fingerprint
            or draft.decision is None
            or draft.contract is None
        ):
            raise _conflict()
        _revalidate_authority(
            authoring,
            draft=draft,
            pointers=self.pointers,
            versions=self.versions,
            targets=self.targets,
            physical_bindings=self.physical_bindings,
            profiles=self.profiles,
            at=now,
        )
        if (
            self.authorization.actor_identity_is_one_of(
                principal,
                workspace_id=authoring.workspace_id,
                actor_ids={authoring.owner_actor_id, draft.decision.actor},
            )
            or now - principal.authenticated_at > self.publisher_max_session_age
        ):
            raise RegistryChangeAuthoringError(
                RegistryChangeAuthoringErrorCode.SEPARATION_OF_DUTIES,
                "Managed preparation requires a fresh, separate publisher identity.",
            )
        proposal = PreparedRegistryJoinProposal.create(
            id=(
                f"registry-join-{authoring.id}-v"
                f"{authoring.request.base_evidence.base_registry.next_registry_version}"
            ),
            draft=draft,
            prepared_by=actor_id,
            prepared_at=now,
        )
        ready = mark_registry_join_change_ready(draft, proposal)
        audit = _audit(
            authoring=authoring,
            event=RegistryChangeAuditEvent.PUBLICATION_PREPARED,
            actor_id=actor_id,
            occurred_at=now,
            source_revision=draft.revision,
            resulting_revision=ready.revision,
            previous_fingerprint=draft.fingerprint,
            resulting_fingerprint=ready.fingerprint,
            proposal_id=proposal.id,
        )
        request_fingerprint = _request_fingerprint(
            _OP_PREPARE,
            actor_id,
            authoring,
            payload,
        )
        try:
            return self.store.commit_preparation(
                authoring,
                ready,
                proposal,
                audit,
                expected_revision=draft.revision,
                operation=_OP_PREPARE,
                actor_id=actor_id,
                idempotency_digest=digest,
                request_fingerprint=request_fingerprint,
            )
        except RegistryChangeStoreError as error:
            raise _store_failure(error) from error


@dataclass(frozen=True, slots=True)
class ListRegistryJoinChanges:
    store: RegistryChangeStorePort
    authorization: RegistryChangeAuthorizationPolicy
    clock: RegistryChangeClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        limit: int = MAX_REGISTRY_CHANGE_LIST_LIMIT,
    ) -> tuple[RegistryJoinProfileAuthoringRequest, ...]:
        if not 1 <= limit <= MAX_REGISTRY_CHANGE_LIST_LIMIT:
            raise _invalid_request()
        scopes = self.authorization.list_scopes_for(principal, at=self.clock.now())
        collected: dict[tuple[str, str], RegistryJoinProfileAuthoringRequest] = {}
        try:
            for scope in scopes:
                for item in self.store.list_for_workspace(
                    scope.workspace_id,
                    owner_actor_id=scope.owner_actor_id,
                    limit=limit,
                ):
                    key = (item.workspace_id, item.id)
                    if key in collected and collected[key] != item:
                        raise _service_unavailable()
                    collected[key] = item
        except RegistryChangeStoreError as error:
            raise _store_failure(error) from error
        return tuple(
            sorted(
                collected.values(),
                key=lambda item: (item.created_at, item.id),
                reverse=True,
            )[:limit]
        )


@dataclass(frozen=True, slots=True)
class InspectRegistryJoinChange:
    store: RegistryChangeStorePort
    authorization: RegistryChangeAuthorizationPolicy
    clock: RegistryChangeClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        change_id: str,
        *,
        history_limit: int = DEFAULT_REGISTRY_CHANGE_HISTORY_LIMIT,
    ) -> RegistryJoinChangeSnapshot:
        if not 1 <= history_limit <= MAX_REGISTRY_CHANGE_HISTORY:
            raise _invalid_request()
        now = self.clock.now()
        authoring = _load_authoring(
            self.store,
            self.authorization,
            principal,
            change_id,
        )
        self.authorization.require_resource(
            principal,
            authoring,
            RegistryChangePermission.VIEW,
            at=now,
        )
        draft = self.store.load_draft(authoring.workspace_id, authoring.id)
        audit_visible = RegistryChangePermission.AUDIT_VIEW in self.authorization.permissions_for(
            principal, at=now
        )
        if not audit_visible:
            return RegistryJoinChangeSnapshot(authoring=authoring, draft=draft)
        try:
            audit = self.store.list_audit(
                authoring.workspace_id,
                authoring.id,
                limit=history_limit + 1,
            )
        except RegistryChangeStoreError as error:
            raise _store_failure(error) from error
        return RegistryJoinChangeSnapshot(
            authoring=authoring,
            draft=draft,
            audit_visible=True,
            history_truncated=len(audit) > history_limit,
            audit=audit[-history_limit:],
        )


def _revalidate_authority(
    authoring: RegistryJoinProfileAuthoringRequest,
    *,
    draft: RegistryJoinChangeDraft | None,
    pointers: ActiveRegistryPointerReadPort,
    versions: RegistryVersionReadPort,
    targets: ExecutionTargetResolverPort,
    physical_bindings: RegistryPublicationPhysicalBindingAuthorityPort,
    profiles: RegistryJoinProfileRequestQueuePort,
    at: datetime,
) -> tuple[GovernedSemanticRegistrySnapshot, SemanticJoinProfileJob]:
    base = _revalidate_profile_request_authority(
        authoring,
        pointers=pointers,
        versions=versions,
        targets=targets,
        physical_bindings=physical_bindings,
    )
    try:
        job = profiles.load(authoring)
    except SemanticJoinProfileQueueError as error:
        raise _profile_queue_failure(error) from error
    if job is None or job.status is not SemanticJoinProfileJobStatus.COMPLETED:
        raise RegistryChangeAuthoringError(
            RegistryChangeAuthoringErrorCode.PROFILE_NOT_READY,
            "The exact relationship profile is not complete.",
        )
    if not _job_matches_request(job, authoring):
        raise _profile_stale()
    if draft is not None:
        try:
            validate_registry_join_profile_campaign(
                draft.profile_campaign,
                scope=draft.scope,
                base_evidence=draft.base_evidence,
                profile_job=job,
                at=at,
            )
        except ValueError as error:
            raise _profile_stale() from error
    return base, job


def _revalidate_profile_request_authority(
    authoring: RegistryJoinProfileAuthoringRequest,
    *,
    pointers: ActiveRegistryPointerReadPort,
    versions: RegistryVersionReadPort,
    targets: ExecutionTargetResolverPort,
    physical_bindings: RegistryPublicationPhysicalBindingAuthorityPort,
) -> GovernedSemanticRegistrySnapshot:
    """Reread every source-I/O authority immediately before enqueue or reuse."""

    request = authoring.request
    base, base_registry = _load_active_base(pointers, versions, request.scope)
    if base_registry != request.base_evidence.base_registry:
        raise _stale_base()
    current_evidence = resolve_registry_join_base_evidence(
        scope=request.scope,
        base=base,
        base_registry=base_registry,
        proposal=request.proposal,
    )
    if current_evidence != request.base_evidence:
        raise _stale_base()
    _require_current_bindings(physical_bindings, request.scope, current_evidence)
    target = _resolve_target(
        targets,
        workspace_id=authoring.workspace_id,
        connection_id=request.proposal.connection_id,
    )
    if SemanticJoinProfileTargetRef.from_target(target) != request.execution_target:
        raise _stale_target()
    return base


def _load_active_base(
    pointers: ActiveRegistryPointerReadPort,
    versions: RegistryVersionReadPort,
    scope: SemanticRegistryScope,
) -> tuple[GovernedSemanticRegistrySnapshot, OnboardingRegistryBase]:
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
        or registry.registry_id != scope.registry_id
        or registry.catalog_scope != scope.catalog_scope
        or registry.version != pointer.registry_version
        or registry.fingerprint != pointer.registry_fingerprint
    ):
        raise _stale_base()
    base_registry = OnboardingRegistryBase(
        registry_version=pointer.registry_version,
        registry_fingerprint=pointer.registry_fingerprint,
        activation_generation=pointer.generation,
        active_pointer_fingerprint=registry_projection_fingerprint(pointer),
    )
    return registry, base_registry


def _require_current_bindings(
    authority: RegistryPublicationPhysicalBindingAuthorityPort,
    scope: SemanticRegistryScope,
    evidence: RegistryJoinBaseEvidence,
) -> None:
    try:
        authority.require_current(
            scope,
            (evidence.left.binding, evidence.right.binding),
        )
    except Exception as error:
        raise RegistryChangeAuthoringError(
            RegistryChangeAuthoringErrorCode.STALE_CATALOG,
            "The registry change catalog authority is no longer current.",
        ) from error


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
    return target


def _job_matches_request(
    job: SemanticJoinProfileJob,
    authoring: RegistryJoinProfileAuthoringRequest,
) -> bool:
    request = authoring.request
    return (
        job.workspace_id == authoring.workspace_id
        and job.scan_id == request.scan_id
        and job.bound_proposal == request.proposal
        and job.execution_target == request.execution_target
        and job.requested_at == request.requested_at
    )


def _load_operation_replay(
    store: RegistryChangeStorePort,
    authorization: RegistryChangeAuthorizationPolicy,
    principal: AuthenticatedPrincipal,
    *,
    operation: str,
    idempotency_digest: str,
    request_payload: object,
    at: datetime,
) -> RegistryChangeOperationReplay | None:
    matches: list[RegistryChangeOperationReplay] = []
    try:
        for workspace_id in authorization.workspace_ids_for_principal(principal):
            replay = store.load_operation_replay(workspace_id, idempotency_digest)
            if replay is not None:
                matches.append(replay)
    except RegistryChangeStoreError as error:
        raise _store_failure(error) from error
    if not matches:
        return None
    if len(matches) != 1:
        raise _service_unavailable()
    replay = matches[0]
    authorization.require_resource(
        principal,
        replay.authoring,
        _permission_for_operation(operation),
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
        != _request_fingerprint(
            operation,
            replay.actor_id,
            replay.authoring,
            request_payload,
        )
    ):
        raise _conflict()
    return replay


def _permission_for_operation(operation: str) -> RegistryChangePermission:
    return {
        _OP_REQUEST: RegistryChangePermission.REQUEST_PROFILE,
        _OP_FINALIZE: RegistryChangePermission.FINALIZE_DRAFT,
        _OP_DECIDE: RegistryChangePermission.DECIDE,
        _OP_PREPARE: RegistryChangePermission.PREPARE_PUBLICATION,
    }[operation]


def _load_authoring(
    store: RegistryChangeStorePort,
    authorization: RegistryChangeAuthorizationPolicy,
    principal: AuthenticatedPrincipal,
    change_id: str,
) -> RegistryJoinProfileAuthoringRequest:
    matches: list[RegistryJoinProfileAuthoringRequest] = []
    try:
        for workspace_id in authorization.workspace_ids_for_principal(principal):
            value = store.load_authoring(workspace_id, change_id)
            if value is not None:
                matches.append(value)
    except RegistryChangeStoreError as error:
        raise _store_failure(error) from error
    if len(matches) != 1:
        if len(matches) > 1:
            raise _service_unavailable()
        raise _unavailable()
    return matches[0]


def _load_draft_resource(
    store: RegistryChangeStorePort,
    authorization: RegistryChangeAuthorizationPolicy,
    principal: AuthenticatedPrincipal,
    change_id: str,
) -> tuple[RegistryJoinProfileAuthoringRequest, RegistryJoinChangeDraft]:
    authoring = _load_authoring(store, authorization, principal, change_id)
    try:
        draft = store.load_draft(authoring.workspace_id, authoring.id)
    except RegistryChangeStoreError as error:
        raise _store_failure(error) from error
    if draft is None:
        raise _unavailable()
    return authoring, draft


def _request_fingerprint(
    operation: str,
    actor_id: str,
    authoring: RegistryJoinProfileAuthoringRequest,
    payload: object,
) -> str:
    return registry_change_authoring_fingerprint(
        {
            "actor_id": actor_id,
            "authoring_fingerprint": authoring.fingerprint,
            "inner_profile_request_fingerprint": authoring.request.fingerprint,
            "operation": operation,
            "payload": payload,
        }
    )


def _audit(
    *,
    authoring: RegistryJoinProfileAuthoringRequest,
    event: RegistryChangeAuditEvent,
    actor_id: str,
    occurred_at: datetime,
    source_revision: int,
    resulting_revision: int,
    previous_fingerprint: str | None,
    resulting_fingerprint: str,
    profile_request_fingerprint: str | None = None,
    profile_job_id: str | None = None,
    decision_id: str | None = None,
    proposal_id: str | None = None,
) -> RegistryChangeAuditRecord:
    return RegistryChangeAuditRecord.create(
        workspace_id=authoring.workspace_id,
        change_id=authoring.id,
        event=event,
        actor_id=actor_id,
        occurred_at=occurred_at,
        source_revision=source_revision,
        resulting_revision=resulting_revision,
        previous_fingerprint=previous_fingerprint,
        resulting_fingerprint=resulting_fingerprint,
        profile_request_fingerprint=profile_request_fingerprint,
        profile_job_id=profile_job_id,
        decision_id=decision_id,
        proposal_id=proposal_id,
    )


def _require_scope(
    principal: AuthenticatedPrincipal,
    scope: SemanticRegistryScope,
) -> None:
    if principal.workspace_id != scope.workspace_id:
        raise _unavailable()


def _idempotency_digest(value: str) -> str:
    if not isinstance(value, str) or _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise _invalid_request()
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _store_failure(error: RegistryChangeStoreError) -> RegistryChangeAuthoringError:
    if error.code is RegistryChangeStoreErrorCode.CONFLICT:
        return _conflict()
    if error.code is RegistryChangeStoreErrorCode.NOT_FOUND:
        return _unavailable()
    return _service_unavailable()


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
            "The exact relationship profile is not complete.",
        )
    return _service_unavailable()


def _invalid_request() -> RegistryChangeAuthoringError:
    return RegistryChangeAuthoringError(
        RegistryChangeAuthoringErrorCode.INVALID_REQUEST,
        "The registry change request is invalid.",
    )


def _unavailable() -> RegistryChangeAuthoringError:
    return RegistryChangeAuthoringError(
        RegistryChangeAuthoringErrorCode.UNAVAILABLE,
        "The registry change resource is not available.",
    )


def _conflict() -> RegistryChangeAuthoringError:
    return RegistryChangeAuthoringError(
        RegistryChangeAuthoringErrorCode.CONFLICT,
        "The registry change state changed; reload and retry.",
    )


def _stale_base() -> RegistryChangeAuthoringError:
    return RegistryChangeAuthoringError(
        RegistryChangeAuthoringErrorCode.STALE_BASE,
        "The active registry-v2 base changed; start a fresh change.",
    )


def _stale_target() -> RegistryChangeAuthoringError:
    return RegistryChangeAuthoringError(
        RegistryChangeAuthoringErrorCode.STALE_TARGET,
        "The governed connector target changed; start a fresh profile request.",
    )


def _profile_stale() -> RegistryChangeAuthoringError:
    return RegistryChangeAuthoringError(
        RegistryChangeAuthoringErrorCode.PROFILE_STALE,
        "The relationship profile authority changed or expired.",
    )


def _service_unavailable() -> RegistryChangeAuthoringError:
    return RegistryChangeAuthoringError(
        RegistryChangeAuthoringErrorCode.SERVICE_UNAVAILABLE,
        "The registry change authoring service is unavailable.",
    )


__all__ = [
    "DEFAULT_REGISTRY_CHANGE_HISTORY_LIMIT",
    "DecideRegistryJoinChange",
    "FinalizeRegistryJoinChangeDraft",
    "InspectRegistryJoinChange",
    "ListRegistryJoinChanges",
    "PrepareRegistryJoinChangePublication",
    "RegistryChangeAuthoringError",
    "RegistryChangeAuthoringErrorCode",
    "RequestRegistryJoinProfile",
]
