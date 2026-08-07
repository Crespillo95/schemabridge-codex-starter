"""Authenticated identity and workflow access values."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel


class AuthenticationMethod(StrEnum):
    """Closed authentication transports accepted by SchemaBridge."""

    OIDC = "oidc"
    LOCAL_DEMO = "local_demo"


class IdentityRole(StrEnum):
    """Closed production roles; unrecognised provider groups are never roles."""

    ANALYST = "analyst"
    STEWARD = "steward"
    PUBLISHER = "publisher"
    AUDITOR = "auditor"
    PLATFORM_ADMIN = "platform_admin"


class WorkflowPermission(StrEnum):
    """Application operations protected by the workflow authorization policy."""

    CREATE = "workflow:create"
    VIEW = "workflow:view"
    CONFIRM = "workflow:confirm"
    EXECUTE = "workflow:execute"
    PUBLISH = "workflow:publish"
    SKIP = "workflow:skip"
    RETRY = "workflow:retry"
    VIEW_RESULT = "result:view"
    EXPORT_RESULT = "result:export"


class AuthenticatedPrincipal(FrozenDomainModel):
    """A verified, privacy-preserving subject used at application boundaries."""

    actor_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    roles: frozenset[IdentityRole] = frozenset()
    authentication_method: AuthenticationMethod
    authenticated_at: datetime
    expires_at: datetime

    @field_validator("actor_id", "workspace_id")
    @classmethod
    def identifiers_must_not_be_blank(cls, value: str) -> str:
        """Reject visually empty identities."""

        if not value.strip():
            raise ValueError("identity identifiers must not be blank")
        return value

    @field_validator("authenticated_at", "expires_at")
    @classmethod
    def times_must_be_aware(cls, value: datetime) -> datetime:
        """Require callers to supply unambiguous identity timestamps."""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("identity timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def expiry_must_follow_authentication(self) -> AuthenticatedPrincipal:
        """Reject zero-length and backwards authentication sessions."""

        if self.expires_at <= self.authenticated_at:
            raise ValueError("expires_at must be later than authenticated_at")
        return self

    def is_current(self, at: datetime) -> bool:
        """Return whether the injected instant is inside this session window."""

        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("at must include a timezone")
        return self.authenticated_at <= at < self.expires_at


class WorkflowAccessGrant(FrozenDomainModel):
    """Durable tenant and owner binding for one workflow identifier."""

    workflow_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    owner_actor_id: str = Field(min_length=1)
    created_at: datetime

    @field_validator("workflow_id", "workspace_id", "owner_actor_id")
    @classmethod
    def identifiers_must_not_be_blank(cls, value: str) -> str:
        """Reject grants that cannot identify their protected resource."""

        if not value.strip():
            raise ValueError("workflow access identifiers must not be blank")
        return value

    @field_validator("created_at")
    @classmethod
    def created_time_must_be_aware(cls, value: datetime) -> datetime:
        """Keep persisted grant times unambiguous."""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return value
