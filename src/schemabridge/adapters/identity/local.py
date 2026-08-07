"""Explicit non-production identity for local and public recorded demos."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from schemabridge.adapters.identity.pseudonyms import derive_pseudonymous_id
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)

_LOCAL_DEMO_PSEUDONYM_KEY = b"schemabridge-local-demo-pseudonym-key-v1"


@dataclass(frozen=True, slots=True)
class LocalDemoPrincipalFactory:
    """Create one stable pseudonymous operator without accepting browser identity input."""

    workspace: str
    subject: str
    roles: frozenset[IdentityRole]
    session_ttl: timedelta = timedelta(hours=12)

    def __post_init__(self) -> None:
        if not self.workspace.strip():
            raise ValueError("local demo workspace must not be blank")
        if not self.subject.strip():
            raise ValueError("local demo subject must not be blank")
        if self.session_ttl <= timedelta(0):
            raise ValueError("local demo session ttl must be positive")

    def create(self, *, now: datetime) -> AuthenticatedPrincipal:
        """Return a current local principal whose identifiers reveal no configured label."""

        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must include a timezone")
        return AuthenticatedPrincipal(
            actor_id=derive_pseudonymous_id(
                "actor",
                self.workspace,
                self.subject,
                key=_LOCAL_DEMO_PSEUDONYM_KEY,
            ),
            workspace_id=derive_pseudonymous_id(
                "workspace",
                self.workspace,
                key=_LOCAL_DEMO_PSEUDONYM_KEY,
            ),
            roles=self.roles,
            authentication_method=AuthenticationMethod.LOCAL_DEMO,
            authenticated_at=now,
            expires_at=now + self.session_ttl,
        )
