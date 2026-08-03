"""Identity-aware access to immutable historical semantic-onboarding scope."""

from __future__ import annotations

from dataclasses import dataclass

from schemabridge.application.ports.identity_rotation import (
    IdentityBindingResolverPort,
    IdentityRotationStoreError,
    IdentityRotationStoreErrorCode,
)
from schemabridge.application.ports.semantic_onboarding import (
    SemanticOnboardingOperationReplay,
    SemanticOnboardingPortError,
    SemanticOnboardingPortErrorCode,
    SemanticOnboardingStorePort,
)
from schemabridge.domain.identity_rotation import IdentityAuthorizationScope
from schemabridge.domain.semantic_onboarding import (
    MAX_ONBOARDING_DECISIONS,
    PreparedSemanticOnboardingProposal,
    SemanticOnboardingAuditRecord,
    SemanticOnboardingDecision,
    SemanticOnboardingDraft,
    SemanticOnboardingDraftMutation,
    SemanticOnboardingPreparation,
)

_MAX_RESOLVED_SCOPES = 128


@dataclass(frozen=True, slots=True)
class IdentityResolvingSemanticOnboardingStore:
    """Route one current principal to one verified historical draft coordinate."""

    store: SemanticOnboardingStorePort
    resolver: IdentityBindingResolverPort
    workspace_id: str
    actor_id: str

    def load_operation_replay(
        self,
        workspace_id: str,
        idempotency_digest: str,
    ) -> SemanticOnboardingOperationReplay | None:
        """Find one replay across the current workspace's verified lineage."""

        self._require_active_workspace(workspace_id)
        return self._locate_operation_replay(idempotency_digest)

    def _locate_operation_replay(
        self,
        idempotency_digest: str,
        *,
        aliases: tuple[str, ...] | None = None,
    ) -> SemanticOnboardingOperationReplay | None:
        resolved_aliases = aliases or self._workspace_aliases()
        matches = tuple(
            (alias, replay)
            for alias in resolved_aliases
            if (replay := self.store.load_operation_replay(alias, idempotency_digest)) is not None
        )
        if len(matches) > 1:
            raise _unavailable()
        if not matches:
            return None
        alias, replay = matches[0]
        located = self._locate_draft(replay.draft.id, aliases=resolved_aliases)
        if located is None or replay.draft.workspace_id != alias or located.workspace_id != alias:
            raise _unavailable()
        return replay

    def create(
        self,
        draft: SemanticOnboardingDraft,
        audit: SemanticOnboardingAuditRecord,
        *,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> SemanticOnboardingDraftMutation:
        """Create only under the exact active identity, never a historical alias."""

        self._authorization_scopes()
        if (
            draft.workspace_id != self.workspace_id
            or draft.scope.workspace_id != self.workspace_id
            or draft.owner_actor_id != self.actor_id
            or audit.workspace_id != self.workspace_id
            or audit.actor_id != self.actor_id
            or actor_id != self.actor_id
        ):
            raise _resource_unavailable()
        aliases = self._workspace_aliases()
        self._has_same_coordinate_replay(
            idempotency_digest,
            workspace_id=draft.workspace_id,
            draft_id=draft.id,
            actor_id=actor_id,
            aliases=aliases,
        )
        located = self._locate_draft(draft.id, aliases=aliases)
        if located is not None and located.workspace_id != draft.workspace_id:
            raise _conflict()
        # A matching active-coordinate draft can appear after the replay pre-check when an
        # identical concurrent request commits first.  Delegate that case to the atomic store,
        # which alone can compare the complete operation fingerprint without a TOCTOU window.
        return self.store.create(
            draft,
            audit,
            operation=operation,
            actor_id=actor_id,
            idempotency_digest=idempotency_digest,
            request_fingerprint=request_fingerprint,
        )

    def load(self, workspace_id: str, draft_id: str) -> SemanticOnboardingDraft | None:
        """Load one exact draft through the active workspace coordinate."""

        self._require_active_workspace(workspace_id)
        return self._locate_draft(draft_id)

    def list_for_workspace(
        self,
        workspace_id: str,
        *,
        owner_actor_id: str | None,
        limit: int,
    ) -> tuple[SemanticOnboardingDraft, ...]:
        """Merge a bounded list without exposing ambiguous historical IDs."""

        self._require_active_workspace(workspace_id)
        if not 1 <= limit <= 50:
            raise _unavailable()
        coordinates: tuple[tuple[str, str | None], ...]
        if owner_actor_id is None:
            coordinates = tuple((alias, None) for alias in self._workspace_aliases())
        else:
            if owner_actor_id != self.actor_id:
                raise _resource_unavailable()
            coordinates = tuple(
                (scope.workspace_id, scope.actor_id) for scope in self._authorization_scopes()
            )
        matches = tuple(
            draft
            for alias, owner in coordinates
            for draft in self.store.list_for_workspace(
                alias,
                owner_actor_id=owner,
                limit=limit,
            )
        )
        draft_ids = tuple(draft.id for draft in matches)
        if len(draft_ids) != len(set(draft_ids)):
            raise _unavailable()
        return tuple(
            sorted(
                matches,
                key=lambda draft: (
                    draft.updated_at,
                    draft.id,
                    draft.workspace_id,
                ),
                reverse=True,
            )[:limit]
        )

    def commit_decision(
        self,
        draft: SemanticOnboardingDraft,
        decision: SemanticOnboardingDecision,
        audit: SemanticOnboardingAuditRecord,
        *,
        expected_revision: int,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> SemanticOnboardingDraftMutation:
        """Update only the unique historical draft revalidated in this lineage."""

        self._require_mutation_binding(
            draft,
            actor_id=actor_id,
            idempotency_digest=idempotency_digest,
            decision=decision,
            audit=audit,
        )
        return self.store.commit_decision(
            draft,
            decision,
            audit,
            expected_revision=expected_revision,
            operation=operation,
            actor_id=actor_id,
            idempotency_digest=idempotency_digest,
            request_fingerprint=request_fingerprint,
        )

    def commit_preparation(
        self,
        draft: SemanticOnboardingDraft,
        proposal: PreparedSemanticOnboardingProposal,
        audit: SemanticOnboardingAuditRecord,
        *,
        expected_revision: int,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> SemanticOnboardingPreparation:
        """Prepare only the unique historical draft revalidated in this lineage."""

        self._require_mutation_binding(
            draft,
            actor_id=actor_id,
            idempotency_digest=idempotency_digest,
            proposal=proposal,
            audit=audit,
        )
        return self.store.commit_preparation(
            draft,
            proposal,
            audit,
            expected_revision=expected_revision,
            operation=operation,
            actor_id=actor_id,
            idempotency_digest=idempotency_digest,
            request_fingerprint=request_fingerprint,
        )

    def list_decisions(
        self,
        workspace_id: str,
        draft_id: str,
        *,
        limit: int = MAX_ONBOARDING_DECISIONS + 2,
    ) -> tuple[SemanticOnboardingDecision, ...]:
        resource_workspace = self._history_workspace(workspace_id, draft_id)
        if resource_workspace is None:
            return ()
        return self.store.list_decisions(resource_workspace, draft_id, limit=limit)

    def list_proposals(
        self,
        workspace_id: str,
        draft_id: str,
        *,
        limit: int = MAX_ONBOARDING_DECISIONS + 2,
    ) -> tuple[PreparedSemanticOnboardingProposal, ...]:
        resource_workspace = self._history_workspace(workspace_id, draft_id)
        if resource_workspace is None:
            return ()
        return self.store.list_proposals(resource_workspace, draft_id, limit=limit)

    def list_audit(
        self,
        workspace_id: str,
        draft_id: str,
        *,
        limit: int = MAX_ONBOARDING_DECISIONS + 2,
    ) -> tuple[SemanticOnboardingAuditRecord, ...]:
        resource_workspace = self._history_workspace(workspace_id, draft_id)
        if resource_workspace is None:
            return ()
        return self.store.list_audit(resource_workspace, draft_id, limit=limit)

    def _history_workspace(self, workspace_id: str, draft_id: str) -> str | None:
        aliases = self._workspace_aliases()
        if workspace_id not in aliases:
            raise _resource_unavailable()
        draft = self._locate_draft(draft_id, aliases=aliases)
        if draft is None:
            return None
        if workspace_id not in {self.workspace_id, draft.workspace_id}:
            raise _resource_unavailable()
        return draft.workspace_id

    def _require_mutation_binding(
        self,
        draft: SemanticOnboardingDraft,
        *,
        actor_id: str,
        idempotency_digest: str,
        audit: SemanticOnboardingAuditRecord,
        decision: SemanticOnboardingDecision | None = None,
        proposal: PreparedSemanticOnboardingProposal | None = None,
    ) -> None:
        aliases = self._workspace_aliases()
        persisted_actor_id = self._actor_id_for_workspace(draft.workspace_id)
        self._has_same_coordinate_replay(
            idempotency_digest,
            workspace_id=draft.workspace_id,
            draft_id=draft.id,
            actor_id=persisted_actor_id,
            aliases=aliases,
        )
        current = self._locate_draft(draft.id, aliases=aliases)
        if (
            current is None
            or current.workspace_id != draft.workspace_id
            or actor_id != persisted_actor_id
            or audit.workspace_id != draft.workspace_id
            or audit.draft_id != draft.id
            or audit.actor_id != persisted_actor_id
            or (
                decision is not None
                and (
                    decision.workspace_id != draft.workspace_id
                    or decision.draft_id != draft.id
                    or decision.actor_id != persisted_actor_id
                )
            )
            or (
                proposal is not None
                and (
                    proposal.workspace_id != draft.workspace_id
                    or proposal.draft_id != draft.id
                    or proposal.prepared_by != persisted_actor_id
                )
            )
        ):
            raise _resource_unavailable()

    def _has_same_coordinate_replay(
        self,
        idempotency_digest: str,
        *,
        workspace_id: str,
        draft_id: str,
        actor_id: str,
        aliases: tuple[str, ...],
    ) -> bool:
        replay = self._locate_operation_replay(idempotency_digest, aliases=aliases)
        if replay is None:
            return False
        if (
            replay.draft.workspace_id != workspace_id
            or replay.draft.id != draft_id
            or replay.actor_id != actor_id
        ):
            raise _conflict()
        return True

    def _locate_draft(
        self,
        draft_id: str,
        *,
        aliases: tuple[str, ...] | None = None,
    ) -> SemanticOnboardingDraft | None:
        resolved_aliases = aliases or self._workspace_aliases()
        matches = tuple(
            draft
            for alias in resolved_aliases
            if (draft := self.store.load(alias, draft_id)) is not None
        )
        if len(matches) > 1:
            raise _unavailable()
        return None if not matches else matches[0]

    def _workspace_aliases(self) -> tuple[str, ...]:
        try:
            aliases = self.resolver.resolve_workspace_aliases(self.workspace_id)
        except IdentityRotationStoreError as error:
            raise _identity_failure(error) from error
        if (
            not aliases
            or len(aliases) > _MAX_RESOLVED_SCOPES
            or len(set(aliases)) != len(aliases)
            or self.workspace_id not in aliases
        ):
            raise _unavailable()
        return aliases

    def _authorization_scopes(self) -> tuple[IdentityAuthorizationScope, ...]:
        try:
            scopes = self.resolver.resolve_authorization_scopes(
                self.workspace_id,
                self.actor_id,
            )
        except IdentityRotationStoreError as error:
            raise _identity_failure(error) from error
        coordinates = {(scope.workspace_id, scope.actor_id) for scope in scopes}
        if (
            not scopes
            or len(scopes) > _MAX_RESOLVED_SCOPES
            or len(coordinates) != len(scopes)
            or (self.workspace_id, self.actor_id) not in coordinates
        ):
            raise _unavailable()
        return scopes

    def _actor_id_for_workspace(self, workspace_id: str) -> str:
        matches = tuple(
            scope.actor_id
            for scope in self._authorization_scopes()
            if scope.workspace_id == workspace_id
        )
        if not matches:
            raise _resource_unavailable()
        if len(matches) != 1:
            raise _unavailable()
        return matches[0]

    def _require_active_workspace(self, workspace_id: str) -> None:
        if workspace_id != self.workspace_id:
            raise _resource_unavailable()


def _identity_failure(error: IdentityRotationStoreError) -> SemanticOnboardingPortError:
    if error.code in {
        IdentityRotationStoreErrorCode.CROSS_WORKSPACE,
        IdentityRotationStoreErrorCode.INCOMPLETE_OWNER_BINDING,
    }:
        return _resource_unavailable()
    return _unavailable()


def _resource_unavailable() -> SemanticOnboardingPortError:
    return SemanticOnboardingPortError(
        SemanticOnboardingPortErrorCode.RESOURCE_UNAVAILABLE,
        "semantic onboarding resource is unavailable",
    )


def _conflict() -> SemanticOnboardingPortError:
    return SemanticOnboardingPortError(
        SemanticOnboardingPortErrorCode.CONFLICT,
        "semantic onboarding store conflict",
    )


def _unavailable() -> SemanticOnboardingPortError:
    return SemanticOnboardingPortError(
        SemanticOnboardingPortErrorCode.UNAVAILABLE,
        "semantic onboarding identity resolution failed",
    )


__all__ = ["IdentityResolvingSemanticOnboardingStore"]
