"""Deny-by-default authorization for M35 registry-change authoring."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn, Protocol

from schemabridge.application.authorization import AuthorizationError, AuthorizationErrorCode
from schemabridge.application.ports.identity_rotation import (
    IdentityBindingResolverPort,
    IdentityRotationStoreError,
    IdentityRotationStoreErrorCode,
)
from schemabridge.domain.identity import AuthenticatedPrincipal, IdentityRole
from schemabridge.domain.identity_rotation import IdentityAuthorizationScope
from schemabridge.domain.registry_change_authoring import RegistryChangePermission

_ROLE_PERMISSIONS: dict[IdentityRole, frozenset[RegistryChangePermission]] = {
    IdentityRole.ANALYST: frozenset(
        {
            RegistryChangePermission.VIEW,
            RegistryChangePermission.REQUEST_PROFILE,
            RegistryChangePermission.FINALIZE_DRAFT,
        }
    ),
    IdentityRole.STEWARD: frozenset(
        {
            RegistryChangePermission.VIEW,
            RegistryChangePermission.REQUEST_PROFILE,
            RegistryChangePermission.FINALIZE_DRAFT,
            RegistryChangePermission.DECIDE,
            RegistryChangePermission.AUDIT_VIEW,
        }
    ),
    IdentityRole.PUBLISHER: frozenset(
        {
            RegistryChangePermission.VIEW,
            RegistryChangePermission.PREPARE_PUBLICATION,
            RegistryChangePermission.AUDIT_VIEW,
        }
    ),
    IdentityRole.AUDITOR: frozenset(
        {
            RegistryChangePermission.VIEW,
            RegistryChangePermission.AUDIT_VIEW,
        }
    ),
    IdentityRole.PLATFORM_ADMIN: frozenset(RegistryChangePermission),
}

_WORKSPACE_ROLES = frozenset(
    {
        IdentityRole.STEWARD,
        IdentityRole.PUBLISHER,
        IdentityRole.AUDITOR,
        IdentityRole.PLATFORM_ADMIN,
    }
)
_MAX_RESOLVED_SCOPES = 128


class _RegistryChangeResource(Protocol):
    @property
    def workspace_id(self) -> str: ...

    @property
    def owner_actor_id(self) -> str: ...


@dataclass(frozen=True, slots=True)
class RegistryChangeListScope:
    workspace_id: str
    owner_actor_id: str | None


@dataclass(frozen=True, slots=True)
class RegistryChangeAuthorizationPolicy:
    """Authorize only closed permissions and exact current/historical identity pairs."""

    identity_resolver: IdentityBindingResolverPort | None = None

    def permissions_for(
        self,
        principal: AuthenticatedPrincipal,
        *,
        at: datetime,
    ) -> frozenset[RegistryChangePermission]:
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
        permission: RegistryChangePermission,
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

    def require_resource(
        self,
        principal: AuthenticatedPrincipal,
        authoring: _RegistryChangeResource,
        permission: RegistryChangePermission,
        *,
        at: datetime,
    ) -> None:
        self.require(principal, permission, at=at)
        if self.identity_resolver is None:
            if principal.workspace_id != authoring.workspace_id:
                self._deny_resource()
            if principal.actor_id == authoring.owner_actor_id:
                return
            if principal.roles & _WORKSPACE_ROLES:
                return
            self._deny_resource()
        if principal.roles & _WORKSPACE_ROLES:
            if authoring.workspace_id in self.workspace_ids_for_principal(principal):
                return
            self._deny_resource()
        if any(
            scope.workspace_id == authoring.workspace_id
            and scope.actor_id == authoring.owner_actor_id
            for scope in self._authorization_scopes(principal)
        ):
            return
        self._deny_resource()

    def list_scopes_for(
        self,
        principal: AuthenticatedPrincipal,
        *,
        at: datetime,
    ) -> tuple[RegistryChangeListScope, ...]:
        self.require(principal, RegistryChangePermission.VIEW, at=at)
        if self.identity_resolver is None:
            return (
                RegistryChangeListScope(
                    workspace_id=principal.workspace_id,
                    owner_actor_id=(
                        None if principal.roles & _WORKSPACE_ROLES else principal.actor_id
                    ),
                ),
            )
        if principal.roles & _WORKSPACE_ROLES:
            return tuple(
                RegistryChangeListScope(workspace_id=value, owner_actor_id=None)
                for value in self.workspace_ids_for_principal(principal)
            )
        return tuple(
            RegistryChangeListScope(
                workspace_id=scope.workspace_id,
                owner_actor_id=scope.actor_id,
            )
            for scope in self._authorization_scopes(principal)
        )

    def actor_identity_is_one_of(
        self,
        principal: AuthenticatedPrincipal,
        *,
        workspace_id: str,
        actor_ids: Collection[str],
    ) -> bool:
        expected = frozenset(actor_ids)
        if self.identity_resolver is None:
            return principal.workspace_id == workspace_id and principal.actor_id in expected
        return any(
            scope.workspace_id == workspace_id and scope.actor_id in expected
            for scope in self._authorization_scopes(principal)
        )

    def operation_scopes_for(
        self,
        principal: AuthenticatedPrincipal,
    ) -> tuple[tuple[str, str], ...]:
        """Return bounded exact workspace/actor pairs for historical replay lookup."""

        if self.identity_resolver is None:
            return ((principal.workspace_id, principal.actor_id),)
        return tuple(
            (scope.workspace_id, scope.actor_id) for scope in self._authorization_scopes(principal)
        )

    def actor_id_for_workspace(
        self,
        principal: AuthenticatedPrincipal,
        workspace_id: str,
    ) -> str:
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
        if self.identity_resolver is None:
            return (principal.workspace_id,)
        try:
            aliases = self.identity_resolver.resolve_workspace_aliases(principal.workspace_id)
        except IdentityRotationStoreError as error:
            self._raise_identity_resolution_error(error)
        if (
            not aliases
            or len(aliases) > _MAX_RESOLVED_SCOPES
            or len(set(aliases)) != len(aliases)
            or principal.workspace_id not in aliases
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
            "The registry change authorization policy is unavailable.",
        )

    @staticmethod
    def _deny_resource() -> NoReturn:
        raise AuthorizationError(
            AuthorizationErrorCode.WORKFLOW_ACCESS_DENIED,
            "The registry change resource is not available.",
        )


__all__ = [
    "RegistryChangeAuthorizationPolicy",
    "RegistryChangeListScope",
]
