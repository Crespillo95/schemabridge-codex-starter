"""M33 onboarding RBAC and ownership tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.unit.test_semantic_onboarding import _draft

from schemabridge.application.authorization import AuthorizationError, AuthorizationErrorCode
from schemabridge.application.semantic_onboarding_authorization import (
    SemanticOnboardingAuthorizationPolicy,
)
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)
from schemabridge.domain.semantic_onboarding import SemanticOnboardingPermission

NOW = datetime(2026, 8, 2, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("role", "allowed"),
    (
        (IdentityRole.ANALYST, {"view", "create"}),
        (IdentityRole.STEWARD, {"view", "create", "decide", "audit_view"}),
        (IdentityRole.PUBLISHER, {"view", "prepare_publication", "audit_view"}),
        (IdentityRole.AUDITOR, {"view", "audit_view"}),
        (
            IdentityRole.PLATFORM_ADMIN,
            {item.value.split(":", 1)[1] for item in SemanticOnboardingPermission},
        ),
    ),
)
def test_closed_role_matrix(role: IdentityRole, allowed: set[str]) -> None:
    policy = SemanticOnboardingAuthorizationPolicy()
    principal = _principal(role)

    actual = {
        permission.value.split(":", 1)[1]
        for permission in policy.permissions_for(principal, at=NOW)
    }
    assert actual == allowed


def test_analyst_can_view_only_own_draft() -> None:
    policy = SemanticOnboardingAuthorizationPolicy()
    own = _principal(IdentityRole.ANALYST, actor_id="analyst-a")
    other = _principal(IdentityRole.ANALYST, actor_id="analyst-b")
    draft = _draft()

    policy.require_draft(own, draft, SemanticOnboardingPermission.VIEW, at=NOW)
    with pytest.raises(AuthorizationError) as raised:
        policy.require_draft(other, draft, SemanticOnboardingPermission.VIEW, at=NOW)
    assert raised.value.code is AuthorizationErrorCode.WORKFLOW_ACCESS_DENIED


def test_steward_has_workspace_visibility_but_not_publication_permission() -> None:
    policy = SemanticOnboardingAuthorizationPolicy()
    steward = _principal(IdentityRole.STEWARD, actor_id="steward-a")
    policy.require_draft(
        steward,
        _draft(),
        SemanticOnboardingPermission.DECIDE,
        at=NOW,
    )

    with pytest.raises(AuthorizationError) as raised:
        policy.require(
            steward,
            SemanticOnboardingPermission.PREPARE_PUBLICATION,
            at=NOW,
        )
    assert raised.value.code is AuthorizationErrorCode.PERMISSION_DENIED


def test_expired_principal_is_denied_before_role_evaluation() -> None:
    policy = SemanticOnboardingAuthorizationPolicy()
    principal = _principal(IdentityRole.PLATFORM_ADMIN, expires_at=NOW)

    with pytest.raises(AuthorizationError) as raised:
        policy.require(principal, SemanticOnboardingPermission.VIEW, at=NOW)
    assert raised.value.code is AuthorizationErrorCode.PRINCIPAL_NOT_CURRENT


def _principal(
    role: IdentityRole,
    *,
    actor_id: str = "actor-a",
    workspace_id: str = "workspace-a",
    expires_at: datetime | None = None,
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        actor_id=actor_id,
        workspace_id=workspace_id,
        roles=frozenset({role}),
        authentication_method=AuthenticationMethod.OIDC,
        authenticated_at=NOW - timedelta(minutes=1),
        expires_at=expires_at or NOW + timedelta(hours=1),
    )
