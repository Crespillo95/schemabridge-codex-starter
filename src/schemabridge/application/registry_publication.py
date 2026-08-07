"""Authenticated API use cases for generic registry publication."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from schemabridge.application.ports.registry_publication import (
    RegistryPublicationJobApiStorePort,
    RegistryPublicationJobMutation,
    RegistryPublicationProposalPort,
    RegistryPublicationStoreError,
    RegistryPublicationStoreErrorCode,
)
from schemabridge.application.ports.semantic_onboarding import SemanticOnboardingClockPort
from schemabridge.application.semantic_onboarding_authorization import (
    SemanticOnboardingAuthorizationPolicy,
)
from schemabridge.domain.identity import AuthenticatedPrincipal
from schemabridge.domain.registry_publication import (
    RegistryPublicationAuthorization,
    RegistryPublicationAuthorizationConfirmation,
)
from schemabridge.domain.registry_publication_jobs import (
    PreparedRegistryPublicationProposal,
    RegistryPublicationJob,
    RegistryPublicationJobStatus,
    create_registry_publication_job,
    registry_publication_request_fingerprint,
)
from schemabridge.domain.semantic_onboarding import SemanticOnboardingPermission

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
_MAX_PUBLISHER_SESSION_AGE = timedelta(minutes=15)
_AUTHORIZATION_LIFETIME = timedelta(minutes=15)


class RegistryPublicationErrorCode(StrEnum):
    INVALID_REQUEST = "registry_publication_invalid_request"
    UNAVAILABLE = "registry_publication_resource_unavailable"
    CONFLICT = "registry_publication_conflict"
    TARGET_RESERVED = "registry_publication_target_reserved"
    NOT_READY = "registry_publication_not_ready"
    STALE_SESSION = "registry_publication_stale_session"
    SERVICE_UNAVAILABLE = "registry_publication_service_unavailable"


class RegistryPublicationError(RuntimeError):
    def __init__(self, code: RegistryPublicationErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SubmitRegistryPublication:
    proposals: RegistryPublicationProposalPort
    jobs: RegistryPublicationJobApiStorePort
    authorization: SemanticOnboardingAuthorizationPolicy
    clock: SemanticOnboardingClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        proposal_id: str,
        confirmed_proposal_fingerprint: str,
        idempotency_key: str,
    ) -> RegistryPublicationJobMutation:
        now = self.clock.now()
        self.authorization.require(
            principal,
            SemanticOnboardingPermission.PREPARE_PUBLICATION,
            at=now,
        )
        _require_fresh_publisher(principal, now)
        digest = _idempotency_digest(idempotency_key)
        try:
            proposal = _load_proposal_for_principal(
                self.proposals,
                self.authorization,
                principal,
                proposal_id,
            )
        except RegistryPublicationStoreError as error:
            raise _store_failure(error) from error
        if proposal is None or proposal.fingerprint != confirmed_proposal_fingerprint:
            raise _unavailable()
        persisted_actor = self.authorization.actor_id_for_workspace(
            principal,
            proposal.workspace_id,
        )
        request_fingerprint = registry_publication_request_fingerprint(
            proposal,
            submitted_by=persisted_actor,
        )
        try:
            replay = self.jobs.load_by_idempotency(
                proposal.workspace_id,
                persisted_actor,
                digest,
            )
            if replay is not None:
                if (
                    replay.request_fingerprint != request_fingerprint
                    or replay.proposal.id != proposal.id
                    or replay.proposal.fingerprint != proposal.fingerprint
                ):
                    raise RegistryPublicationError(
                        RegistryPublicationErrorCode.CONFLICT,
                        "The publication idempotency key identifies another request.",
                    )
                return RegistryPublicationJobMutation(job=replay, replayed=True)
            job = create_registry_publication_job(
                proposal,
                submitted_by=persisted_actor,
                submitted_at=now,
                idempotency_digest=digest,
                request_fingerprint=request_fingerprint,
            )
            return self.jobs.submit(job)
        except RegistryPublicationStoreError as error:
            raise _store_failure(error) from error


@dataclass(frozen=True, slots=True)
class InspectRegistryPublication:
    jobs: RegistryPublicationJobApiStorePort
    authorization: SemanticOnboardingAuthorizationPolicy
    clock: SemanticOnboardingClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        job_id: str,
    ) -> RegistryPublicationJob:
        now = self.clock.now()
        permissions = self.authorization.permissions_for(principal, at=now)
        if not permissions.intersection(
            {
                SemanticOnboardingPermission.PREPARE_PUBLICATION,
                SemanticOnboardingPermission.AUDIT_VIEW,
            }
        ):
            self.authorization.require(
                principal,
                SemanticOnboardingPermission.AUDIT_VIEW,
                at=now,
            )
        try:
            job = _load_job_for_principal(
                self.jobs,
                self.authorization,
                principal,
                job_id,
            )
        except RegistryPublicationStoreError as error:
            raise _store_failure(error) from error
        if job is None:
            raise _unavailable()
        self.authorization.actor_id_for_workspace(principal, job.scope.workspace_id)
        return job


@dataclass(frozen=True, slots=True)
class AuthorizeRegistryPublication:
    jobs: RegistryPublicationJobApiStorePort
    authorization: SemanticOnboardingAuthorizationPolicy
    clock: SemanticOnboardingClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        job_id: str,
        *,
        expected_revision: int,
        confirmed_candidate_fingerprint: str,
        confirmation: RegistryPublicationAuthorizationConfirmation,
    ) -> RegistryPublicationJob:
        now = self.clock.now()
        self.authorization.require(
            principal,
            SemanticOnboardingPermission.PREPARE_PUBLICATION,
            at=now,
        )
        _require_fresh_publisher(principal, now)
        try:
            job = _load_job_for_principal(
                self.jobs,
                self.authorization,
                principal,
                job_id,
            )
        except RegistryPublicationStoreError as error:
            raise _store_failure(error) from error
        if (
            job is None
            or job.status is not RegistryPublicationJobStatus.AWAITING_APPROVAL
            or job.revision != expected_revision
            or job.candidate is None
            or job.candidate.fingerprint != confirmed_candidate_fingerprint
        ):
            raise RegistryPublicationError(
                RegistryPublicationErrorCode.CONFLICT,
                "The publication candidate changed; reload before authorization.",
            )
        actor_id = self.authorization.actor_id_for_workspace(
            principal,
            job.scope.workspace_id,
        )
        try:
            approval = RegistryPublicationAuthorization.create(
                job.candidate,
                actor_id=actor_id,
                authenticated_at=principal.authenticated_at,
                approved_at=now,
                expires_at=min(
                    principal.expires_at,
                    now + _AUTHORIZATION_LIFETIME,
                    principal.authenticated_at + _MAX_PUBLISHER_SESSION_AGE,
                ),
                confirmation=confirmation,
            )
            return self.jobs.authorize(
                job.id,
                workspace_id=job.scope.workspace_id,
                expected_revision=job.revision,
                authorization=approval,
            )
        except ValueError as error:
            raise _invalid_request() from error
        except RegistryPublicationStoreError as error:
            raise _store_failure(error) from error


@dataclass(frozen=True, slots=True)
class CancelRegistryPublication:
    jobs: RegistryPublicationJobApiStorePort
    authorization: SemanticOnboardingAuthorizationPolicy
    clock: SemanticOnboardingClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        job_id: str,
        *,
        expected_revision: int,
    ) -> RegistryPublicationJob:
        now = self.clock.now()
        self.authorization.require(
            principal,
            SemanticOnboardingPermission.PREPARE_PUBLICATION,
            at=now,
        )
        try:
            job = _load_job_for_principal(
                self.jobs,
                self.authorization,
                principal,
                job_id,
            )
        except RegistryPublicationStoreError as error:
            raise _store_failure(error) from error
        if job is None or job.revision != expected_revision:
            raise RegistryPublicationError(
                RegistryPublicationErrorCode.CONFLICT,
                "The publication job changed; reload before cancellation.",
            )
        actor_id = self.authorization.actor_id_for_workspace(
            principal,
            job.scope.workspace_id,
        )
        try:
            return self.jobs.request_cancellation(
                job.id,
                workspace_id=job.scope.workspace_id,
                expected_revision=job.revision,
                requested_by=actor_id,
            )
        except RegistryPublicationStoreError as error:
            raise _store_failure(error) from error


def _require_fresh_publisher(principal: AuthenticatedPrincipal, now: datetime) -> None:
    if now - principal.authenticated_at >= _MAX_PUBLISHER_SESSION_AGE:
        raise RegistryPublicationError(
            RegistryPublicationErrorCode.STALE_SESSION,
            "Registry publication requires a recent authenticated publisher session.",
        )


def _load_proposal_for_principal(
    proposals: RegistryPublicationProposalPort,
    authorization: SemanticOnboardingAuthorizationPolicy,
    principal: AuthenticatedPrincipal,
    proposal_id: str,
) -> PreparedRegistryPublicationProposal | None:
    matches = tuple(
        proposal
        for workspace_id in authorization.workspace_ids_for_principal(principal)
        if (proposal := proposals.load_exact(workspace_id, proposal_id)) is not None
    )
    if len(matches) > 1:
        raise _unavailable()
    return None if not matches else matches[0]


def _load_job_for_principal(
    jobs: RegistryPublicationJobApiStorePort,
    authorization: SemanticOnboardingAuthorizationPolicy,
    principal: AuthenticatedPrincipal,
    job_id: str,
) -> RegistryPublicationJob | None:
    matches = tuple(
        job
        for workspace_id in authorization.workspace_ids_for_principal(principal)
        if (job := jobs.load(workspace_id, job_id)) is not None
    )
    if len(matches) > 1:
        raise _unavailable()
    return None if not matches else matches[0]


def _idempotency_digest(value: str) -> str:
    if _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise _invalid_request()
    return hashlib.sha256(value.encode()).hexdigest()


def _store_failure(error: RegistryPublicationStoreError) -> RegistryPublicationError:
    if error.code is RegistryPublicationStoreErrorCode.TARGET_RESERVED:
        return RegistryPublicationError(
            RegistryPublicationErrorCode.TARGET_RESERVED,
            "The target registry version is already reserved.",
        )
    if error.code in {
        RegistryPublicationStoreErrorCode.IDEMPOTENCY_CONFLICT,
        RegistryPublicationStoreErrorCode.STATE_CONFLICT,
        RegistryPublicationStoreErrorCode.LEASE_CONFLICT,
    }:
        return RegistryPublicationError(
            RegistryPublicationErrorCode.CONFLICT,
            "The registry publication state changed; reload and retry.",
        )
    if error.code is RegistryPublicationStoreErrorCode.NOT_FOUND:
        return _unavailable()
    return RegistryPublicationError(
        RegistryPublicationErrorCode.SERVICE_UNAVAILABLE,
        "The registry publication service is temporarily unavailable.",
    )


def _invalid_request() -> RegistryPublicationError:
    return RegistryPublicationError(
        RegistryPublicationErrorCode.INVALID_REQUEST,
        "The registry publication request is invalid.",
    )


def _unavailable() -> RegistryPublicationError:
    return RegistryPublicationError(
        RegistryPublicationErrorCode.UNAVAILABLE,
        "The registry publication resource is not available.",
    )


__all__ = [
    "AuthorizeRegistryPublication",
    "CancelRegistryPublication",
    "InspectRegistryPublication",
    "RegistryPublicationError",
    "RegistryPublicationErrorCode",
    "SubmitRegistryPublication",
]
