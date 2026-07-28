"""Application port for converting a verified bearer credential into a principal."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from schemabridge.domain.identity import AuthenticatedPrincipal


class BearerAuthenticationPort(Protocol):
    """Authenticate one transient bearer value without retaining it."""

    def authenticate(
        self,
        bearer_token: str,
        now: datetime,
    ) -> AuthenticatedPrincipal:
        """Return the minimal authenticated principal or fail closed."""
