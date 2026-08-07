"""Strict mapping from authenticated OIDC claims to an opaque principal."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import NoReturn

from schemabridge.adapters.identity.pseudonyms import (
    PSEUDONYM_KEY_VERSION,
    derive_pseudonymous_id,
)
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)

MAX_ALLOWED_TENANTS = 256
MAX_TENANT_LENGTH = 120


class OidcClaimErrorCode(StrEnum):
    """Stable claim-validation failures that never contain raw claim values."""

    INVALID_ISSUER = "invalid_issuer"
    MISSING_SUBJECT = "missing_subject"
    INVALID_AUDIENCE = "invalid_audience"
    INVALID_AUTHORIZED_PARTY = "invalid_authorized_party"
    INVALID_TIMESTAMP = "invalid_timestamp"
    SESSION_NOT_CURRENT = "session_not_current"
    MISSING_WORKSPACE = "missing_workspace"
    TENANT_NOT_ALLOWED = "tenant_not_allowed"
    INVALID_GROUPS = "invalid_groups"


class OidcClaimError(RuntimeError):
    """Sanitized failure while mapping an already authenticated ID token."""

    def __init__(self, code: OidcClaimErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class OidcPrincipalMapper:
    """Validate provider claims and discard them after creating a principal."""

    expected_issuer: str
    expected_audience: str
    allowed_group_roles: Mapping[str, frozenset[IdentityRole]]
    allowed_tenants: frozenset[str]
    pseudonymization_key: bytes = field(repr=False)
    pseudonymization_key_version: str = PSEUDONYM_KEY_VERSION
    workspace_claim: str = "tenant_id"
    groups_claim: str = "groups"
    max_session_age: timedelta = timedelta(hours=12)

    def __post_init__(self) -> None:
        for value, label in (
            (self.expected_issuer, "expected_issuer"),
            (self.expected_audience, "expected_audience"),
            (self.workspace_claim, "workspace_claim"),
            (self.groups_claim, "groups_claim"),
        ):
            if not value.strip():
                raise ValueError(f"{label} must not be blank")
        if self.max_session_age <= timedelta(0):
            raise ValueError("max_session_age must be positive")
        copied_tenants = frozenset(self.allowed_tenants)
        if (
            not copied_tenants
            or len(copied_tenants) > MAX_ALLOWED_TENANTS
            or any(
                not isinstance(tenant, str)
                or not tenant
                or tenant != tenant.strip()
                or len(tenant) > MAX_TENANT_LENGTH
                for tenant in copied_tenants
            )
        ):
            raise ValueError(
                "allowed tenants must be non-empty, trimmed, and within configured bounds"
            )
        object.__setattr__(self, "allowed_tenants", copied_tenants)
        derive_pseudonymous_id(
            "actor",
            "configuration-check",
            key=self.pseudonymization_key,
            key_version=self.pseudonymization_key_version,
        )
        copied_roles = {
            group: frozenset(roles) for group, roles in self.allowed_group_roles.items()
        }
        if any(
            not isinstance(group, str)
            or not group.strip()
            or not roles
            or any(not isinstance(role, IdentityRole) for role in roles)
            for group, roles in copied_roles.items()
        ):
            raise ValueError("allowed group mappings must contain non-blank groups and known roles")
        object.__setattr__(self, "allowed_group_roles", MappingProxyType(copied_roles))

    def map_claims(
        self,
        claims: Mapping[str, object],
        *,
        now: datetime,
    ) -> AuthenticatedPrincipal:
        """Map claims exposed by Streamlit after OIDC authentication."""

        self._require_aware(now)
        issuer = self._required_text(
            claims.get("iss"),
            code=OidcClaimErrorCode.INVALID_ISSUER,
            message="The identity issuer is invalid.",
        )
        if issuer != self.expected_issuer:
            raise OidcClaimError(
                OidcClaimErrorCode.INVALID_ISSUER,
                "The identity issuer is invalid.",
            )
        subject = self._required_text(
            claims.get("sub"),
            code=OidcClaimErrorCode.MISSING_SUBJECT,
            message="The authenticated subject is missing.",
        )
        audiences = self._validate_audience(claims.get("aud"))
        self._validate_authorized_party(claims.get("azp"), audiences)

        issued_at = self._timestamp(claims.get("iat"), claim_name="iat")
        expires_at = self._timestamp(claims.get("exp"), claim_name="exp")
        not_before_value = claims.get("nbf")
        not_before = (
            self._timestamp(not_before_value, claim_name="nbf")
            if not_before_value is not None
            else None
        )
        if expires_at <= issued_at:
            raise OidcClaimError(
                OidcClaimErrorCode.INVALID_TIMESTAMP,
                "The identity timestamp order is invalid.",
            )
        if issued_at > now:
            self._raise_session_not_current()
        if expires_at <= now:
            self._raise_session_not_current()
        if now - issued_at > self.max_session_age:
            self._raise_session_not_current()
        if expires_at - issued_at > self.max_session_age:
            self._raise_session_not_current()
        if not_before is not None:
            if not_before >= expires_at:
                raise OidcClaimError(
                    OidcClaimErrorCode.INVALID_TIMESTAMP,
                    "The identity timestamp order is invalid.",
                )
            if not_before > now:
                self._raise_session_not_current()

        workspace_subject = self._required_text(
            claims.get(self.workspace_claim),
            code=OidcClaimErrorCode.MISSING_WORKSPACE,
            message="The authenticated workspace is missing.",
        )
        if workspace_subject not in self.allowed_tenants:
            raise OidcClaimError(
                OidcClaimErrorCode.TENANT_NOT_ALLOWED,
                "The authenticated tenant is not allowed.",
            )
        roles = self._map_groups(claims)
        return AuthenticatedPrincipal(
            actor_id=self._opaque_id("actor", issuer, subject),
            workspace_id=self._opaque_id("workspace", issuer, workspace_subject),
            roles=roles,
            authentication_method=AuthenticationMethod.OIDC,
            authenticated_at=issued_at,
            expires_at=expires_at,
        )

    def _validate_audience(self, raw_audience: object | None) -> tuple[str, ...]:
        if isinstance(raw_audience, str):
            audiences = (raw_audience,) if raw_audience.strip() else ()
        elif isinstance(raw_audience, (list, tuple)):
            if any(not isinstance(item, str) or not item.strip() for item in raw_audience):
                audiences = ()
            else:
                audiences = tuple(raw_audience)
        else:
            audiences = ()
        if self.expected_audience not in audiences:
            raise OidcClaimError(
                OidcClaimErrorCode.INVALID_AUDIENCE,
                "The identity audience is invalid.",
            )
        return audiences

    def _validate_authorized_party(
        self,
        raw_authorized_party: object | None,
        audiences: tuple[str, ...],
    ) -> None:
        if len(audiences) <= 1 and raw_authorized_party is None:
            return
        if (
            not isinstance(raw_authorized_party, str)
            or raw_authorized_party != self.expected_audience
        ):
            raise OidcClaimError(
                OidcClaimErrorCode.INVALID_AUTHORIZED_PARTY,
                "The identity authorized party is invalid.",
            )

    def _map_groups(self, claims: Mapping[str, object]) -> frozenset[IdentityRole]:
        if self.groups_claim not in claims:
            self._raise_invalid_groups()
        raw_groups = claims[self.groups_claim]
        if not isinstance(raw_groups, (list, tuple)):
            self._raise_invalid_groups()
        if any(not isinstance(group, str) or not group.strip() for group in raw_groups):
            self._raise_invalid_groups()
        return frozenset(
            role
            for group in raw_groups
            for role in self.allowed_group_roles.get(group, frozenset())
        )

    @staticmethod
    def _timestamp(raw_value: object | None, *, claim_name: str) -> datetime:
        if (
            isinstance(raw_value, bool)
            or not isinstance(raw_value, (int, float))
            or not math.isfinite(raw_value)
        ):
            raise OidcClaimError(
                OidcClaimErrorCode.INVALID_TIMESTAMP,
                f"The {claim_name} identity timestamp is invalid.",
            )
        try:
            return datetime.fromtimestamp(raw_value, tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise OidcClaimError(
                OidcClaimErrorCode.INVALID_TIMESTAMP,
                f"The {claim_name} identity timestamp is invalid.",
            ) from exc

    @staticmethod
    def _required_text(
        raw_value: object | None,
        *,
        code: OidcClaimErrorCode,
        message: str,
    ) -> str:
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise OidcClaimError(code, message)
        return raw_value

    def _opaque_id(self, kind: str, *parts: str) -> str:
        return derive_pseudonymous_id(
            kind,
            *parts,
            key=self.pseudonymization_key,
            key_version=self.pseudonymization_key_version,
        )

    @staticmethod
    def _require_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("now must include a timezone")

    @staticmethod
    def _raise_session_not_current() -> NoReturn:
        raise OidcClaimError(
            OidcClaimErrorCode.SESSION_NOT_CURRENT,
            "The authenticated session is not current.",
        )

    @staticmethod
    def _raise_invalid_groups() -> NoReturn:
        raise OidcClaimError(
            OidcClaimErrorCode.INVALID_GROUPS,
            "The identity groups claim is invalid.",
        )
