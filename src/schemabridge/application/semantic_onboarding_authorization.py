"""Deny-by-default authorization for the M33 semantic-onboarding boundary."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn

from schemabridge.application.authorization import AuthorizationError, AuthorizationErrorCode
from schemabridge.application.ports.identity_rotation import (
    IdentityBindingResolverPort,
    IdentityRotationStoreError,
    IdentityRotationStoreErrorCode,
)
from schemabridge.domain.identity import AuthenticatedPrincipal, IdentityRole
from schemabridge.domain.identity_rotation import IdentityAuthorizationScope
from schemabridge.domain.semantic_onboarding import (
    SemanticOnboardingDraft,
    SemanticOnboardingPermission,
)

_ROLE_PERMISSIONS: dict[IdentityRole, frozenset[SemanticOnboardingPermission]] = {
    IdentityRole.ANALYST: frozenset(
        {
            SemanticOnboardingPermission.VIEW,
            SemanticOnboardingPermission.CREATE,
        }
    ),
    IdentityRole.STEWARD: frozenset(
        {
            SemanticOnboardingPermission.VIEW,
            SemanticOnboardingPermission.CREATE,
            SemanticOnboardingPermission.DECIDE,
            SemanticOnboardingPermission.AUDIT_VIEW,
        }
    ),
    IdentityRole.PUBLISHER: frozenset(
        {
            SemanticOnboardingPermission.VIEW,
            SemanticOnboardingPermission.PREPARE_PUBLICATION,
            SemanticOnboardingPermission.AUDIT_VIEW,
        }
    ),
    IdentityRole.AUDITOR: frozenset(
        {
            SemanticOnboardingPermission.VIEW,
            SemanticOnboardingPermission.AUDIT_VIEW,
        }
    ),
    IdentityRole.PLATFORM_ADMIN: frozenset(SemanticOnboardingPermission),
}

_WORKSPACE_VIEW_ROLES = frozenset(
    {
        IdentityRole.STEWARD,
        IdentityRole.PUBLISHER,
        IdentityRole.AUDITOR,
        IdentityRole.PLATFORM_ADMIN,
    }
)
_MAX_RESOLVED_SCOPES = 128


@dataclass(frozen=True, slots=True)
class SemanticOnboardingAuthorizationPolicy:
    """Authorize only the closed onboarding matrix and exact workspace/owner scope."""

    identity_resolver: IdentityBindingResolverPort | None = None

    def permissions_for(
        self,
        principal: AuthenticatedPrincipal,
        *,
        at: datetime,
    ) -> frozenset[SemanticOnboardingPermission]:
        if not principal.is_current(at):
            return frozenset()
        return frozenset(
            permission
            for role in principal.roles
            for permission in _ROLE_PERMISSIONS.get(role, frozenset())
        )

    def require(
        self,
        principal: AuthenticatedPrincipal,
        permission: SemanticOnboardingPermission,
        *,
        at: datetime,
    ) -> None:
        if not principal.is_current(at):
            raise AuthorizationError(
                AuthorizationErrorCode.PRINCIPAL_NOT_CURRENT,
                "The authenticated session is not current.",
            )
        if permission not in self.permissions_for(principal, at=at):
            raise AuthorizationError(
                AuthorizationErrorCode.PERMISSION_DENIED,
                "The authenticated principal is not authorized for this operation.",
            )

    def require_draft(
        self,
        principal: AuthenticatedPrincipal,
        draft: SemanticOnboardingDraft,
        permission: SemanticOnboardingPermission,
        *,
        at: datetime,
    ) -> None:
        self.require(principal, permission, at=at)
        if self.identity_resolver is None:
            if principal.workspace_id != draft.workspace_id:
                self._deny_resource()
            if principal.actor_id == draft.owner_actor_id:
                return
            if principal.roles & _WORKSPACE_VIEW_ROLES:
                return
            self._deny_resource()
        if principal.roles & _WORKSPACE_VIEW_ROLES:
            if draft.workspace_id in self._workspace_aliases(principal.workspace_id):
                return
            self._deny_resource()
        if any(
            scope.workspace_id == draft.workspace_id and scope.actor_id == draft.owner_actor_id
            for scope in self._authorization_scopes(principal)
        ):
            return
        self._deny_resource()

    def owner_filter_for(
        self,
        principal: AuthenticatedPrincipal,
        *,
        at: datetime,
    ) -> str | None:
        self.require(principal, SemanticOnboardingPermission.VIEW, at=at)
        if principal.roles & _WORKSPACE_VIEW_ROLES:
            return None
        return principal.actor_id

    def actor_identity_is_one_of(
        self,
        principal: AuthenticatedPrincipal,
        *,
        workspace_id: str,
        actor_ids: Collection[str],
    ) -> bool:
        """Match current and historical actor keys without broadening workspace access."""

        expected_actor_ids = frozenset(actor_ids)
        if self.identity_resolver is None:
            return (
                principal.workspace_id == workspace_id and principal.actor_id in expected_actor_ids
            )
        return any(
            scope.workspace_id == workspace_id and scope.actor_id in expected_actor_ids
            for scope in self._authorization_scopes(principal)
        )

    def actor_id_for_workspace(
        self,
        principal: AuthenticatedPrincipal,
        workspace_id: str,
    ) -> str:
        """Return the one exact actor alias paired with a verified workspace key."""

        if self.identity_resolver is None:
            if principal.workspace_id != workspace_id:
                self._deny_resource()
            return principal.actor_id
        matches = tuple(
            scope.actor_id
            for scope in self._authorization_scopes(principal)
            if scope.workspace_id == workspace_id
        )
        if not matches:
            self._deny_resource()
        if len(matches) != 1:
            self._policy_unavailable()
        return matches[0]

    def workspace_ids_for_principal(
        self,
        principal: AuthenticatedPrincipal,
    ) -> tuple[str, ...]:
        """Return the bounded current/historical workspace keys for exact resource lookup."""

        if self.identity_resolver is None:
            return (principal.workspace_id,)
        return self._workspace_aliases(principal.workspace_id)

    def _workspace_aliases(self, workspace_id: str) -> tuple[str, ...]:
        assert self.identity_resolver is not None
        try:
            aliases = self.identity_resolver.resolve_workspace_aliases(workspace_id)
        except IdentityRotationStoreError as error:
            self._raise_identity_resolution_error(error)
        if (
            not aliases
            or len(aliases) > _MAX_RESOLVED_SCOPES
            or len(set(aliases)) != len(aliases)
            or workspace_id not in aliases
        ):
            self._policy_unavailable()
        return aliases

    def _authorization_scopes(
        self,
        principal: AuthenticatedPrincipal,
    ) -> tuple[IdentityAuthorizationScope, ...]:
        assert self.identity_resolver is not None
        try:
            scopes = self.identity_resolver.resolve_authorization_scopes(
                principal.workspace_id,
                principal.actor_id,
            )
        except IdentityRotationStoreError as error:
            self._raise_identity_resolution_error(error)
        coordinates = {(scope.workspace_id, scope.actor_id) for scope in scopes}
        if (
            not scopes
            or len(scopes) > _MAX_RESOLVED_SCOPES
            or len(coordinates) != len(scopes)
            or (principal.workspace_id, principal.actor_id) not in coordinates
        ):
            self._policy_unavailable()
        return scopes

    @classmethod
    def _raise_identity_resolution_error(
        cls,
        error: IdentityRotationStoreError,
    ) -> NoReturn:
        if error.code in {
            IdentityRotationStoreErrorCode.CROSS_WORKSPACE,
            IdentityRotationStoreErrorCode.INCOMPLETE_OWNER_BINDING,
        }:
            cls._deny_resource()
        cls._policy_unavailable()

    @staticmethod
    def _policy_unavailable() -> NoReturn:
        raise AuthorizationError(
            AuthorizationErrorCode.POLICY_UNAVAILABLE,
            "The semantic onboarding authorization policy is unavailable.",
        )

    @staticmethod
    def _deny_resource() -> NoReturn:
        raise AuthorizationError(
            AuthorizationErrorCode.WORKFLOW_ACCESS_DENIED,
            "The semantic onboarding resource is not available.",
        )


__all__ = ["SemanticOnboardingAuthorizationPolicy"]
