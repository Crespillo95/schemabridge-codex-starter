"""Deny-by-default authorization for authenticated workflow operations."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    IdentityRole,
    WorkflowAccessGrant,
    WorkflowPermission,
)


class AuthorizationErrorCode(StrEnum):
    """Stable, sanitized authorization failure categories."""

    PRINCIPAL_NOT_CURRENT = "principal_not_current"
    PERMISSION_DENIED = "permission_denied"
    WORKFLOW_ACCESS_DENIED = "workflow_access_denied"
    POLICY_UNAVAILABLE = "authorization_policy_unavailable"


class AuthorizationError(RuntimeError):
    """A safe failure that does not disclose protected workflow ownership."""

    def __init__(self, code: AuthorizationErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


_ROLE_PERMISSIONS: dict[IdentityRole, frozenset[WorkflowPermission]] = {
    IdentityRole.ANALYST: frozenset(
        {
            WorkflowPermission.CREATE,
            WorkflowPermission.VIEW,
            WorkflowPermission.CONFIRM,
            WorkflowPermission.EXECUTE,
            WorkflowPermission.SKIP,
            WorkflowPermission.RETRY,
            WorkflowPermission.VIEW_RESULT,
            WorkflowPermission.EXPORT_RESULT,
        }
    ),
    IdentityRole.STEWARD: frozenset(
        {
            WorkflowPermission.VIEW,
            WorkflowPermission.CONFIRM,
        }
    ),
    IdentityRole.PUBLISHER: frozenset(
        {
            WorkflowPermission.VIEW,
            WorkflowPermission.PUBLISH,
        }
    ),
    IdentityRole.AUDITOR: frozenset({WorkflowPermission.VIEW}),
    IdentityRole.PLATFORM_ADMIN: frozenset(WorkflowPermission),
}

_WORKSPACE_WIDE_ROLES: dict[WorkflowPermission, frozenset[IdentityRole]] = {
    WorkflowPermission.VIEW: frozenset(
        {
            IdentityRole.STEWARD,
            IdentityRole.AUDITOR,
            IdentityRole.PUBLISHER,
            IdentityRole.PLATFORM_ADMIN,
        }
    ),
    WorkflowPermission.CONFIRM: frozenset(
        {
            IdentityRole.STEWARD,
            IdentityRole.PLATFORM_ADMIN,
        }
    ),
    WorkflowPermission.PUBLISH: frozenset(
        {
            IdentityRole.PUBLISHER,
            IdentityRole.PLATFORM_ADMIN,
        }
    ),
}


class DenyByDefaultAuthorizationPolicy:
    """Authorize only operations present in the closed role matrix."""

    def permissions_for(
        self,
        principal: AuthenticatedPrincipal,
        *,
        at: datetime,
    ) -> frozenset[WorkflowPermission]:
        """Return the effective permissions for a current principal."""

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
        permission: WorkflowPermission,
        *,
        at: datetime,
    ) -> None:
        """Require a current principal with an explicitly granted permission."""

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

    def require_workflow(
        self,
        principal: AuthenticatedPrincipal,
        grant: WorkflowAccessGrant,
        permission: WorkflowPermission,
        *,
        at: datetime,
    ) -> None:
        """Require both RBAC permission and tenant/owner workflow access."""

        self.require(principal, permission, at=at)
        if principal.workspace_id != grant.workspace_id:
            self._raise_workflow_access_denied()
        if principal.actor_id == grant.owner_actor_id:
            return
        if IdentityRole.PLATFORM_ADMIN in principal.roles:
            return
        if principal.roles & _WORKSPACE_WIDE_ROLES.get(permission, frozenset()):
            return
        self._raise_workflow_access_denied()

    def owner_filter_for(
        self,
        principal: AuthenticatedPrincipal,
        permission: WorkflowPermission,
        *,
        at: datetime,
    ) -> str | None:
        """Resolve the storage scope without leaking the role matrix to callers."""

        self.require(principal, permission, at=at)
        if IdentityRole.PLATFORM_ADMIN in principal.roles:
            return None
        if principal.roles & _WORKSPACE_WIDE_ROLES.get(permission, frozenset()):
            return None
        return principal.actor_id

    @staticmethod
    def _raise_workflow_access_denied() -> None:
        raise AuthorizationError(
            AuthorizationErrorCode.WORKFLOW_ACCESS_DENIED,
            "The workflow is not available to the authenticated principal.",
        )
