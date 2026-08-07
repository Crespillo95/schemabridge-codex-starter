"""Workflow-access decorator that follows verified opaque identity rotations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from schemabridge.application.ports.background_jobs import (
    BackgroundJobApiStorePort,
    JobStoreError,
    JobStoreErrorCode,
)
from schemabridge.application.ports.identity_rotation import (
    IdentityBindingResolverPort,
    IdentityRotationStoreError,
    IdentityRotationStoreErrorCode,
    PersistedIdentityBindingResolverPort,
)
from schemabridge.application.ports.workflow_access import (
    WorkflowAccessError,
    WorkflowAccessErrorCode,
    WorkflowAccessStorePort,
)
from schemabridge.application.ports.workflows import (
    WorkflowDraftStorePort,
    WorkflowError,
    WorkflowErrorCode,
)
from schemabridge.domain.background_jobs import (
    BackgroundJob,
    JobSubmissionResult,
)
from schemabridge.domain.identity import WorkflowAccessGrant
from schemabridge.domain.identity_rotation import IdentityAuthorizationScope
from schemabridge.domain.workflows import AgentWorkflowDraft

_MAX_RESOLVED_SCOPES = 128
WorkflowDraftStoreFactory = Callable[[str, str], WorkflowDraftStorePort]


@dataclass(frozen=True, slots=True)
class IdentityResolvingBackgroundJobApiStore:
    """Resolve API job access through verified same-lineage identity aliases."""

    store: BackgroundJobApiStorePort
    resolver: IdentityBindingResolverPort

    def submit(self, job: BackgroundJob) -> JobSubmissionResult:
        """Create only under the exact active submitter identity."""

        authorization = job.authorization
        scopes = self._authorization_scopes(
            authorization.workspace_id,
            authorization.submitting_actor_id,
        )
        if not any(
            scope.workspace_id == authorization.workspace_id
            and scope.actor_id == authorization.submitting_actor_id
            for scope in scopes
        ):
            raise _job_store_failure()
        return self.store.submit(job)

    def load_by_idempotency(
        self,
        workspace_id: str,
        submitting_actor_id: str,
        idempotency_digest: str,
    ) -> BackgroundJob | None:
        """Find a replay only through exact same-lineage workspace/actor pairs."""

        scopes = self._authorization_scopes(workspace_id, submitting_actor_id)
        matches = tuple(
            job
            for scope in scopes
            if (
                job := self.store.load_by_idempotency(
                    scope.workspace_id,
                    scope.actor_id,
                    idempotency_digest,
                )
            )
            is not None
        )
        return _one_job_match(matches)

    def load(
        self,
        workspace_id: str,
        job_id: str,
        *,
        submitting_actor_id: str | None = None,
    ) -> BackgroundJob | None:
        """Load by exact job id across only verified same-lineage aliases."""

        return self._locate(
            workspace_id,
            job_id,
            submitting_actor_id=submitting_actor_id,
        )

    def request_cancellation(
        self,
        workspace_id: str,
        job_id: str,
        *,
        submitting_actor_id: str | None,
        requested_at: datetime,
    ) -> BackgroundJob | None:
        """Mutate only the exact historical coordinate resolved by a prior read."""

        current = self._locate(
            workspace_id,
            job_id,
            submitting_actor_id=submitting_actor_id,
        )
        if current is None:
            return None
        authorization = current.authorization
        changed = self.store.request_cancellation(
            authorization.workspace_id,
            job_id,
            submitting_actor_id=(
                authorization.submitting_actor_id if submitting_actor_id is not None else None
            ),
            requested_at=requested_at,
        )
        if (
            changed is None
            or changed.id != job_id
            or changed.authorization.workspace_id != authorization.workspace_id
            or changed.authorization.submitting_actor_id != authorization.submitting_actor_id
        ):
            raise _job_store_failure()
        return changed

    def _locate(
        self,
        workspace_id: str,
        job_id: str,
        *,
        submitting_actor_id: str | None,
    ) -> BackgroundJob | None:
        coordinates: tuple[tuple[str, str | None], ...]
        if submitting_actor_id is None:
            coordinates = tuple((alias, None) for alias in self._workspace_aliases(workspace_id))
        else:
            coordinates = tuple(
                (scope.workspace_id, scope.actor_id)
                for scope in self._authorization_scopes(
                    workspace_id,
                    submitting_actor_id,
                )
            )
        matches = tuple(
            job
            for alias, actor_id in coordinates
            if (
                job := self.store.load(
                    alias,
                    job_id,
                    submitting_actor_id=actor_id,
                )
            )
            is not None
        )
        return _one_job_match(matches)

    def _workspace_aliases(self, workspace_id: str) -> tuple[str, ...]:
        try:
            aliases = self.resolver.resolve_workspace_aliases(workspace_id)
        except IdentityRotationStoreError as error:
            raise _job_store_failure() from error
        if (
            not aliases
            or len(aliases) > _MAX_RESOLVED_SCOPES
            or len(set(aliases)) != len(aliases)
            or workspace_id not in aliases
        ):
            raise _job_store_failure()
        return aliases

    def _authorization_scopes(
        self,
        workspace_id: str,
        actor_id: str,
    ) -> tuple[IdentityAuthorizationScope, ...]:
        try:
            scopes = self.resolver.resolve_authorization_scopes(
                workspace_id,
                actor_id,
            )
        except IdentityRotationStoreError as error:
            raise _job_store_failure() from error
        coordinate_set = {(scope.workspace_id, scope.actor_id) for scope in scopes}
        if (
            not scopes
            or len(scopes) > _MAX_RESOLVED_SCOPES
            or len(coordinate_set) != len(scopes)
            or (workspace_id, actor_id) not in coordinate_set
        ):
            raise _job_store_failure()
        return scopes


@dataclass(frozen=True, slots=True)
class IdentityResolvingWorkflowDraftStore:
    """Route one current principal to exactly one same-lineage workflow draft."""

    active_store: WorkflowDraftStorePort
    resolver: IdentityBindingResolverPort
    workspace_id: str
    actor_id: str
    store_factory: WorkflowDraftStoreFactory

    def load(self, workflow_id: str) -> AgentWorkflowDraft | None:
        """Load from exactly one verified same-lineage scope."""

        matches = self._matches(workflow_id)
        return None if not matches else matches[0][1]

    def save(
        self,
        draft: AgentWorkflowDraft,
        *,
        expected_revision: int | None,
    ) -> None:
        """Create in the active scope or update the one resolved historical scope."""

        if expected_revision is None:
            _draft_authorization_scopes(
                self.resolver,
                self.workspace_id,
                self.actor_id,
            )
            self.active_store.save(draft, expected_revision=None)
            return
        matches = self._matches(draft.id)
        if not matches:
            self.active_store.save(draft, expected_revision=expected_revision)
            return
        matches[0][0].save(draft, expected_revision=expected_revision)

    def _matches(
        self,
        workflow_id: str,
    ) -> tuple[tuple[WorkflowDraftStorePort, AgentWorkflowDraft], ...]:
        matches = tuple(
            (store, draft)
            for store in self._same_lineage_stores()
            if (draft := store.load(workflow_id)) is not None
        )
        if len(matches) > 1:
            raise WorkflowError(
                WorkflowErrorCode.CONFLICT,
                "workflow identity resolves to multiple same-lineage drafts",
            )
        return matches

    def _same_lineage_stores(self) -> tuple[WorkflowDraftStorePort, ...]:
        scopes = _draft_authorization_scopes(
            self.resolver,
            self.workspace_id,
            self.actor_id,
        )
        return tuple(
            (
                self.active_store
                if scope.workspace_id == self.workspace_id and scope.actor_id == self.actor_id
                else self.store_factory(scope.workspace_id, scope.actor_id)
            )
            for scope in scopes
        )


@dataclass(frozen=True, slots=True)
class IdentityResolvingWorkflowAccessStore:
    """Resolve current principals to historical immutable grant coordinates."""

    store: WorkflowAccessStorePort
    resolver: IdentityBindingResolverPort
    persisted_job_scope_resolver: PersistedIdentityBindingResolverPort | None = None
    persisted_job_actor_id: str | None = None

    def __post_init__(self) -> None:
        if (self.persisted_job_scope_resolver is None) != (self.persisted_job_actor_id is None):
            raise ValueError("persisted job identity resolution requires both resolver and actor")

    def grant(self, grant: WorkflowAccessGrant) -> WorkflowAccessGrant:
        """Create grants only for a currently active, fully bound identity."""

        scopes = self._active_authorization_scopes(
            grant.workspace_id,
            grant.owner_actor_id,
        )
        if not any(
            scope.workspace_id == grant.workspace_id and scope.actor_id == grant.owner_actor_id
            for scope in scopes
        ):
            raise _store_failure()
        return self.store.grant(grant)

    def load(
        self,
        workspace_id: str,
        workflow_id: str,
        *,
        owner_principal_id: str | None = None,
    ) -> WorkflowAccessGrant | None:
        """Load an exact historical grant through a verified active identity."""

        coordinates: tuple[tuple[str, str | None], ...]
        if owner_principal_id is None:
            coordinates = tuple((alias, None) for alias in self._workspace_aliases(workspace_id))
        else:
            coordinates = tuple(
                (scope.workspace_id, scope.actor_id)
                for scope in self._authorization_scopes(
                    workspace_id,
                    owner_principal_id,
                )
            )
        matches = tuple(
            grant
            for alias, actor in coordinates
            if (
                grant := self.store.load(
                    alias,
                    workflow_id,
                    owner_principal_id=actor,
                )
            )
            is not None
        )
        if len(matches) > 1:
            raise WorkflowAccessError(
                WorkflowAccessErrorCode.CONFLICT,
                "workflow identity resolves to multiple immutable grants",
            )
        return None if not matches else matches[0]

    def list_for_workspace(
        self,
        workspace_id: str,
        *,
        owner_principal_id: str | None = None,
        limit: int = 50,
    ) -> tuple[WorkflowAccessGrant, ...]:
        """Merge a bounded list across same-lineage aliases without rewriting grants."""

        if isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("workflow access list limit must be between 1 and 100")
        coordinates: tuple[tuple[str, str | None], ...]
        if owner_principal_id is None:
            coordinates = tuple((alias, None) for alias in self._workspace_aliases(workspace_id))
        else:
            coordinates = tuple(
                (scope.workspace_id, scope.actor_id)
                for scope in self._authorization_scopes(
                    workspace_id,
                    owner_principal_id,
                )
            )
        grants: dict[tuple[str, str], WorkflowAccessGrant] = {}
        for alias, actor in coordinates:
            for grant in self.store.list_for_workspace(
                alias,
                owner_principal_id=actor,
                limit=limit,
            ):
                identity = (grant.workspace_id, grant.workflow_id)
                current = grants.get(identity)
                if current is not None and current != grant:
                    raise WorkflowAccessError(
                        WorkflowAccessErrorCode.CONFLICT,
                        "workflow identity resolves to conflicting immutable grants",
                    )
                grants[identity] = grant
        return tuple(
            sorted(
                grants.values(),
                key=lambda grant: (
                    -grant.created_at.timestamp(),
                    grant.workflow_id,
                    grant.workspace_id,
                ),
            )[:limit]
        )

    def _workspace_aliases(self, workspace_id: str) -> tuple[str, ...]:
        persisted = self._persisted_job_scopes(workspace_id)
        if persisted is not None:
            return tuple(scope.workspace_id for scope in persisted)
        try:
            aliases = self.resolver.resolve_workspace_aliases(workspace_id)
        except IdentityRotationStoreError as error:
            raise _identity_resolution_failure(error) from error
        if not aliases or len(aliases) > _MAX_RESOLVED_SCOPES:
            raise _store_failure()
        return aliases

    def _authorization_scopes(
        self,
        workspace_id: str,
        actor_id: str,
    ) -> tuple[IdentityAuthorizationScope, ...]:
        persisted_job = self._persisted_job_scopes(workspace_id)
        if persisted_job is None:
            return self._active_authorization_scopes(workspace_id, actor_id)
        if actor_id == self.persisted_job_actor_id:
            return persisted_job
        owner_scopes = self._persisted_authorization_scopes(workspace_id, actor_id)
        if tuple((scope.workspace_id, scope.key_version) for scope in owner_scopes) != tuple(
            (scope.workspace_id, scope.key_version) for scope in persisted_job
        ):
            raise _identity_mismatch()
        return owner_scopes

    def _persisted_job_scopes(
        self,
        workspace_id: str,
    ) -> tuple[IdentityAuthorizationScope, ...] | None:
        if self.persisted_job_scope_resolver is None:
            return None
        assert self.persisted_job_actor_id is not None
        return self._persisted_authorization_scopes(
            workspace_id,
            self.persisted_job_actor_id,
        )

    def _persisted_authorization_scopes(
        self,
        workspace_id: str,
        actor_id: str,
    ) -> tuple[IdentityAuthorizationScope, ...]:
        assert self.persisted_job_scope_resolver is not None
        try:
            scopes = self.persisted_job_scope_resolver.resolve_persisted_authorization_scopes(
                workspace_id,
                actor_id,
            )
        except IdentityRotationStoreError as error:
            raise _identity_resolution_failure(error) from error
        return _validated_authorization_scopes(scopes, workspace_id, actor_id)

    def _active_authorization_scopes(
        self,
        workspace_id: str,
        actor_id: str,
    ) -> tuple[IdentityAuthorizationScope, ...]:
        try:
            scopes = self.resolver.resolve_authorization_scopes(
                workspace_id,
                actor_id,
            )
        except IdentityRotationStoreError as error:
            raise _identity_resolution_failure(error) from error
        return _validated_authorization_scopes(scopes, workspace_id, actor_id)


def _validated_authorization_scopes(
    scopes: tuple[IdentityAuthorizationScope, ...],
    workspace_id: str,
    actor_id: str,
) -> tuple[IdentityAuthorizationScope, ...]:
    coordinates = {(scope.workspace_id, scope.actor_id) for scope in scopes}
    if (
        not scopes
        or len(scopes) > _MAX_RESOLVED_SCOPES
        or len(coordinates) != len(scopes)
        or (workspace_id, actor_id) not in coordinates
    ):
        raise _store_failure()
    return scopes


def _draft_authorization_scopes(
    resolver: IdentityBindingResolverPort,
    workspace_id: str,
    actor_id: str,
) -> tuple[IdentityAuthorizationScope, ...]:
    try:
        scopes = resolver.resolve_authorization_scopes(
            workspace_id,
            actor_id,
        )
    except IdentityRotationStoreError as error:
        raise _workflow_store_failure() from error
    if not scopes or len(scopes) > _MAX_RESOLVED_SCOPES:
        raise _workflow_store_failure()
    if not any(
        scope.workspace_id == workspace_id and scope.actor_id == actor_id for scope in scopes
    ):
        raise _workflow_store_failure()
    return scopes


def _workflow_store_failure() -> WorkflowError:
    return WorkflowError(
        WorkflowErrorCode.STORE_FAILURE,
        "workflow draft store failed",
    )


def _one_job_match(matches: tuple[BackgroundJob, ...]) -> BackgroundJob | None:
    if len(matches) > 1:
        raise JobStoreError(
            JobStoreErrorCode.STATE_CONFLICT,
            "job identity resolves to multiple same-lineage reservations",
        )
    return None if not matches else matches[0]


def _job_store_failure() -> JobStoreError:
    return JobStoreError(
        JobStoreErrorCode.STORE_UNAVAILABLE,
        "background job identity resolution failed",
    )


def _store_failure() -> WorkflowAccessError:
    return WorkflowAccessError(
        WorkflowAccessErrorCode.STORE_FAILURE,
        "workflow access control store failed",
    )


def _identity_resolution_failure(
    error: IdentityRotationStoreError,
) -> WorkflowAccessError:
    if error.code in {
        IdentityRotationStoreErrorCode.CROSS_WORKSPACE,
        IdentityRotationStoreErrorCode.INCOMPLETE_OWNER_BINDING,
    }:
        return _identity_mismatch()
    return _store_failure()


def _identity_mismatch() -> WorkflowAccessError:
    return WorkflowAccessError(
        WorkflowAccessErrorCode.IDENTITY_MISMATCH,
        "workflow identity is not available",
    )
