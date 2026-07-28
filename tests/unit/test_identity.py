from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from schemabridge.adapters.identity.oidc import (
    OidcClaimError,
    OidcClaimErrorCode,
    OidcPrincipalMapper,
)
from schemabridge.adapters.identity.pseudonyms import derive_pseudonymous_id
from schemabridge.application.authorization import (
    AuthorizationError,
    AuthorizationErrorCode,
    DenyByDefaultAuthorizationPolicy,
)
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
    WorkflowAccessGrant,
    WorkflowPermission,
)

NOW = datetime(2026, 7, 23, 10, 0, tzinfo=UTC)
PSEUDONYM_KEY = b"unit-test-pseudonym-key-32-bytes-minimum"


def _principal(
    *roles: IdentityRole,
    actor_id: str = "sb_actor_owner",
    workspace_id: str = "sb_workspace_one",
    authenticated_at: datetime = NOW - timedelta(minutes=1),
    expires_at: datetime = NOW + timedelta(minutes=5),
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        actor_id=actor_id,
        workspace_id=workspace_id,
        roles=frozenset(roles),
        authentication_method=AuthenticationMethod.LOCAL_DEMO,
        authenticated_at=authenticated_at,
        expires_at=expires_at,
    )


def _grant(
    *,
    owner_actor_id: str = "sb_actor_owner",
    workspace_id: str = "sb_workspace_one",
) -> WorkflowAccessGrant:
    return WorkflowAccessGrant(
        workflow_id="workflow-1",
        workspace_id=workspace_id,
        owner_actor_id=owner_actor_id,
        created_at=NOW,
    )


def _claims(**overrides: object) -> dict[str, object]:
    claims: dict[str, object] = {
        "iss": "https://identity.example.test",
        "sub": "provider-subject-42",
        "aud": ["schemabridge", "another-client"],
        "azp": "schemabridge",
        "iat": (NOW - timedelta(minutes=1)).timestamp(),
        "nbf": (NOW - timedelta(minutes=1)).timestamp(),
        "exp": (NOW + timedelta(minutes=5)).timestamp(),
        "tenant_id": "workspace-alpha",
        "groups": ["schema-analysts", "unrecognised-provider-group"],
        "email": "must-not-be-retained@example.test",
    }
    claims.update(overrides)
    return claims


def _mapper() -> OidcPrincipalMapper:
    return OidcPrincipalMapper(
        expected_issuer="https://identity.example.test",
        expected_audience="schemabridge",
        allowed_group_roles={
            "schema-analysts": frozenset({IdentityRole.ANALYST}),
            "schema-stewards": frozenset({IdentityRole.STEWARD}),
            "schema-publishers": frozenset({IdentityRole.PUBLISHER}),
        },
        allowed_tenants=frozenset({"workspace-alpha", "workspace-beta"}),
        pseudonymization_key=PSEUDONYM_KEY,
    )


def test_oidc_mapper_never_exposes_its_pseudonymization_key_in_repr() -> None:
    assert PSEUDONYM_KEY not in repr(_mapper()).encode()


def test_identity_values_are_immutable_and_validate_boundaries() -> None:
    principal = _principal(IdentityRole.ANALYST)
    assert principal.is_current(NOW)
    assert principal.is_current(principal.authenticated_at)
    assert not principal.is_current(principal.expires_at)

    with pytest.raises(ValidationError):
        _principal(actor_id=" ")
    with pytest.raises(ValidationError):
        _principal(authenticated_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValidationError):
        _principal(expires_at=NOW - timedelta(minutes=2))
    with pytest.raises(ValidationError):
        _grant(owner_actor_id=" ")
    with pytest.raises(ValidationError):
        WorkflowAccessGrant(
            workflow_id=" ",
            workspace_id="sb_workspace_one",
            owner_actor_id="sb_actor_owner",
            created_at=NOW,
        )
    with pytest.raises(ValidationError):
        principal.actor_id = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError, match="timezone"):
        principal.is_current(NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (
            IdentityRole.ANALYST,
            {
                WorkflowPermission.CREATE,
                WorkflowPermission.VIEW,
                WorkflowPermission.CONFIRM,
                WorkflowPermission.EXECUTE,
                WorkflowPermission.SKIP,
                WorkflowPermission.RETRY,
                WorkflowPermission.VIEW_RESULT,
                WorkflowPermission.EXPORT_RESULT,
            },
        ),
        (
            IdentityRole.STEWARD,
            {WorkflowPermission.VIEW, WorkflowPermission.CONFIRM},
        ),
        (
            IdentityRole.PUBLISHER,
            {WorkflowPermission.VIEW, WorkflowPermission.PUBLISH},
        ),
        (IdentityRole.AUDITOR, {WorkflowPermission.VIEW}),
        (IdentityRole.PLATFORM_ADMIN, set(WorkflowPermission)),
    ],
)
def test_policy_has_an_exact_closed_role_matrix(
    role: IdentityRole,
    expected: set[WorkflowPermission],
) -> None:
    policy = DenyByDefaultAuthorizationPolicy()

    assert policy.permissions_for(_principal(role), at=NOW) == frozenset(expected)


def test_policy_denies_unknown_empty_and_non_current_principals() -> None:
    policy = DenyByDefaultAuthorizationPolicy()
    no_mapped_roles = _principal()
    expired = _principal(
        IdentityRole.PLATFORM_ADMIN,
        authenticated_at=NOW - timedelta(minutes=2),
        expires_at=NOW - timedelta(minutes=1),
    )
    future = _principal(
        IdentityRole.PLATFORM_ADMIN,
        authenticated_at=NOW + timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=2),
    )

    with pytest.raises(AuthorizationError) as denied:
        policy.require(no_mapped_roles, WorkflowPermission.VIEW, at=NOW)
    assert denied.value.code is AuthorizationErrorCode.PERMISSION_DENIED

    for principal in (expired, future):
        assert policy.permissions_for(principal, at=NOW) == frozenset()
        with pytest.raises(AuthorizationError) as not_current:
            policy.require(principal, WorkflowPermission.VIEW, at=NOW)
        assert not_current.value.code is AuthorizationErrorCode.PRINCIPAL_NOT_CURRENT


def test_policy_enforces_workspace_and_owner_without_disclosing_which_failed() -> None:
    policy = DenyByDefaultAuthorizationPolicy()
    owner = _principal(IdentityRole.ANALYST)
    different_owner = _principal(IdentityRole.ANALYST, actor_id="sb_actor_other")
    different_workspace = _principal(
        IdentityRole.PLATFORM_ADMIN,
        workspace_id="sb_workspace_other",
    )

    policy.require_workflow(owner, _grant(), WorkflowPermission.EXECUTE, at=NOW)
    for principal, permission in (
        (different_owner, WorkflowPermission.EXECUTE),
        (different_workspace, WorkflowPermission.VIEW),
    ):
        with pytest.raises(AuthorizationError) as denied:
            policy.require_workflow(principal, _grant(), permission, at=NOW)
        assert denied.value.code is AuthorizationErrorCode.WORKFLOW_ACCESS_DENIED
        assert str(denied.value) == "The workflow is not available to the authenticated principal."


def test_workspace_wide_roles_have_only_their_explicit_scope() -> None:
    policy = DenyByDefaultAuthorizationPolicy()
    another_owner = _grant(owner_actor_id="sb_actor_other")

    policy.require_workflow(
        _principal(IdentityRole.AUDITOR),
        another_owner,
        WorkflowPermission.VIEW,
        at=NOW,
    )
    policy.require_workflow(
        _principal(IdentityRole.STEWARD),
        another_owner,
        WorkflowPermission.CONFIRM,
        at=NOW,
    )
    policy.require_workflow(
        _principal(IdentityRole.PUBLISHER),
        another_owner,
        WorkflowPermission.PUBLISH,
        at=NOW,
    )
    policy.require_workflow(
        _principal(IdentityRole.PLATFORM_ADMIN),
        another_owner,
        WorkflowPermission.EXPORT_RESULT,
        at=NOW,
    )
    with pytest.raises(AuthorizationError) as denied:
        policy.require(
            _principal(IdentityRole.PUBLISHER),
            WorkflowPermission.EXPORT_RESULT,
            at=NOW,
        )
    assert denied.value.code is AuthorizationErrorCode.PERMISSION_DENIED
    with pytest.raises(AuthorizationError):
        policy.require(
            _principal(IdentityRole.PUBLISHER),
            WorkflowPermission.VIEW_RESULT,
            at=NOW,
        )


def test_oidc_mapper_creates_stable_opaque_minimal_principal() -> None:
    mapper = _mapper()

    principal = mapper.map_claims(_claims(), now=NOW)
    repeated = mapper.map_claims(_claims(), now=NOW)
    another_subject = mapper.map_claims(_claims(sub="provider-subject-43"), now=NOW)
    another_workspace = mapper.map_claims(_claims(tenant_id="workspace-beta"), now=NOW)

    assert principal == repeated
    assert principal.authentication_method is AuthenticationMethod.OIDC
    assert principal.roles == frozenset({IdentityRole.ANALYST})
    assert principal.actor_id.startswith("sb_actor_v1_")
    assert principal.workspace_id.startswith("sb_workspace_v1_")
    assert another_subject.actor_id != principal.actor_id
    assert another_workspace.workspace_id != principal.workspace_id
    assert "provider-subject-42" not in repr(principal)
    assert "workspace-alpha" not in repr(principal)
    assert "must-not-be-retained@example.test" not in repr(principal)
    assert set(principal.model_dump()) == {
        "actor_id",
        "workspace_id",
        "roles",
        "authentication_method",
        "authenticated_at",
        "expires_at",
    }


def test_oidc_pseudonyms_are_deployment_scoped_and_key_versioned() -> None:
    first = _mapper().map_claims(_claims(), now=NOW)
    second = OidcPrincipalMapper(
        expected_issuer="https://identity.example.test",
        expected_audience="schemabridge",
        allowed_group_roles={
            "schema-analysts": frozenset({IdentityRole.ANALYST}),
        },
        allowed_tenants=frozenset({"workspace-alpha"}),
        pseudonymization_key=b"a-different-unit-key-with-32-bytes-minimum",
    ).map_claims(_claims(), now=NOW)

    assert first.actor_id != second.actor_id
    assert first.workspace_id != second.workspace_id
    next_version = OidcPrincipalMapper(
        expected_issuer="https://identity.example.test",
        expected_audience="schemabridge",
        allowed_group_roles={
            "schema-analysts": frozenset({IdentityRole.ANALYST}),
        },
        allowed_tenants=frozenset({"workspace-alpha"}),
        pseudonymization_key=PSEUDONYM_KEY,
        pseudonymization_key_version="v2",
    ).map_claims(_claims(), now=NOW)
    assert next_version.actor_id.startswith("sb_actor_v2_")
    assert next_version.actor_id != first.actor_id
    with pytest.raises(ValueError, match="too short"):
        OidcPrincipalMapper(
            expected_issuer="https://identity.example.test",
            expected_audience="schemabridge",
            allowed_group_roles={
                "schema-analysts": frozenset({IdentityRole.ANALYST}),
            },
            allowed_tenants=frozenset({"workspace-alpha"}),
            pseudonymization_key=b"short",
        )


def test_oidc_mapper_and_derivation_reject_low_diversity_pseudonym_keys() -> None:
    weak_key = b"x" * 32

    with pytest.raises(ValueError, match="insufficient diversity"):
        derive_pseudonymous_id("actor", "subject", key=weak_key)
    with pytest.raises(ValueError, match="insufficient diversity"):
        OidcPrincipalMapper(
            expected_issuer="https://identity.example.test",
            expected_audience="schemabridge",
            allowed_group_roles={
                "schema-analysts": frozenset({IdentityRole.ANALYST}),
            },
            allowed_tenants=frozenset({"workspace-alpha"}),
            pseudonymization_key=weak_key,
        )


def test_oidc_mapper_rejects_tenant_outside_exact_allowlist_without_disclosure() -> None:
    rejected_tenant = "workspace-not-authorized"

    with pytest.raises(OidcClaimError) as failure:
        _mapper().map_claims(_claims(tenant_id=rejected_tenant), now=NOW)

    assert failure.value.code is OidcClaimErrorCode.TENANT_NOT_ALLOWED
    assert str(failure.value) == "The authenticated tenant is not allowed."
    assert rejected_tenant not in str(failure.value)
    assert rejected_tenant not in repr(failure.value)


def test_oidc_unknown_groups_create_a_principal_with_no_permissions() -> None:
    principal = _mapper().map_claims(_claims(groups=["unknown"]), now=NOW)

    assert principal.roles == frozenset()
    assert DenyByDefaultAuthorizationPolicy().permissions_for(principal, at=NOW) == frozenset()


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        ({"iss": "https://wrong.example.test"}, OidcClaimErrorCode.INVALID_ISSUER),
        ({"iss": None}, OidcClaimErrorCode.INVALID_ISSUER),
        ({"sub": " "}, OidcClaimErrorCode.MISSING_SUBJECT),
        ({"aud": ["other-client"]}, OidcClaimErrorCode.INVALID_AUDIENCE),
        ({"aud": ["schemabridge", 7]}, OidcClaimErrorCode.INVALID_AUDIENCE),
        ({"azp": "other-client"}, OidcClaimErrorCode.INVALID_AUTHORIZED_PARTY),
        ({"iat": "yesterday"}, OidcClaimErrorCode.INVALID_TIMESTAMP),
        ({"exp": float("inf")}, OidcClaimErrorCode.INVALID_TIMESTAMP),
        (
            {
                "iat": (NOW + timedelta(minutes=2)).timestamp(),
                "exp": (NOW + timedelta(minutes=1)).timestamp(),
            },
            OidcClaimErrorCode.INVALID_TIMESTAMP,
        ),
        ({"tenant_id": None}, OidcClaimErrorCode.MISSING_WORKSPACE),
        ({"groups": "schema-analysts"}, OidcClaimErrorCode.INVALID_GROUPS),
        ({"groups": ["schema-analysts", " "]}, OidcClaimErrorCode.INVALID_GROUPS),
        (
            {
                "nbf": (NOW + timedelta(minutes=6)).timestamp(),
                "exp": (NOW + timedelta(minutes=5)).timestamp(),
            },
            OidcClaimErrorCode.INVALID_TIMESTAMP,
        ),
    ],
)
def test_oidc_mapper_rejects_missing_or_malformed_required_claims(
    overrides: dict[str, object],
    expected_code: OidcClaimErrorCode,
) -> None:
    with pytest.raises(OidcClaimError) as failure:
        _mapper().map_claims(_claims(**overrides), now=NOW)

    assert failure.value.code is expected_code
    assert "provider-subject-42" not in str(failure.value)
    assert "must-not-be-retained@example.test" not in str(failure.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"iat": (NOW + timedelta(minutes=1)).timestamp()},
        {"nbf": (NOW + timedelta(minutes=1)).timestamp()},
        {"exp": NOW.timestamp()},
    ],
)
def test_oidc_mapper_rejects_future_or_expired_sessions(overrides: dict[str, object]) -> None:
    with pytest.raises(OidcClaimError) as failure:
        _mapper().map_claims(_claims(**overrides), now=NOW)

    assert failure.value.code is OidcClaimErrorCode.SESSION_NOT_CURRENT


def test_oidc_mapper_requires_azp_for_multiple_audiences_and_limits_session_age() -> None:
    without_azp = _claims()
    del without_azp["azp"]
    with pytest.raises(OidcClaimError) as missing_azp:
        _mapper().map_claims(without_azp, now=NOW)
    assert missing_azp.value.code is OidcClaimErrorCode.INVALID_AUTHORIZED_PARTY

    with pytest.raises(OidcClaimError) as stale:
        _mapper().map_claims(
            _claims(
                aud="schemabridge",
                azp=None,
                iat=(NOW - timedelta(hours=13)).timestamp(),
                nbf=(NOW - timedelta(hours=13)).timestamp(),
                exp=(NOW + timedelta(minutes=5)).timestamp(),
            ),
            now=NOW,
        )
    assert stale.value.code is OidcClaimErrorCode.SESSION_NOT_CURRENT


def test_oidc_mapper_rejects_missing_groups_and_naive_clock() -> None:
    claims = _claims()
    del claims["groups"]

    with pytest.raises(OidcClaimError) as failure:
        _mapper().map_claims(claims, now=NOW)
    assert failure.value.code is OidcClaimErrorCode.INVALID_GROUPS

    with pytest.raises(ValueError, match="timezone"):
        _mapper().map_claims(_claims(), now=NOW.replace(tzinfo=None))
