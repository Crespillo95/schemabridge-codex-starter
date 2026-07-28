"""Development-only fixed bearer authentication with no retained raw token."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime

from pydantic import SecretStr

from schemabridge.adapters.identity.local import LocalDemoPrincipalFactory
from schemabridge.application.authentication import AuthenticationBoundaryError
from schemabridge.domain.identity import AuthenticatedPrincipal

MAX_LOCAL_BEARER_BYTES = 4_096
MIN_LOCAL_BEARER_BYTES = 32
MIN_LOCAL_BEARER_DISTINCT_BYTES = 8


class LocalBearerAuthenticator:
    """Verify one development bearer secret using a constant-time digest comparison."""

    __slots__ = ("_expected_digest", "_max_token_bytes", "_principal_factory")

    def __init__(
        self,
        *,
        configured_token: SecretStr,
        principal_factory: LocalDemoPrincipalFactory,
        runtime_profile: str,
        max_token_bytes: int = MAX_LOCAL_BEARER_BYTES,
    ) -> None:
        if runtime_profile != "development":
            raise ValueError("local bearer authentication is development-only")
        if not 1 <= max_token_bytes <= MAX_LOCAL_BEARER_BYTES:
            raise ValueError("max_token_bytes is outside the supported bound")
        raw_token = configured_token.get_secret_value()
        encoded = _validated_token_bytes(raw_token, max_token_bytes=max_token_bytes)
        if (
            len(encoded) < MIN_LOCAL_BEARER_BYTES
            or len(set(encoded)) < MIN_LOCAL_BEARER_DISTINCT_BYTES
        ):
            raise ValueError("configured bearer token does not meet the strength requirement")
        self._expected_digest = hashlib.sha256(encoded).digest()
        self._principal_factory = principal_factory
        self._max_token_bytes = max_token_bytes

    def authenticate(
        self,
        bearer_token: str,
        now: datetime,
    ) -> AuthenticatedPrincipal:
        """Authenticate a transient token and return the configured local principal."""

        try:
            candidate = _validated_token_bytes(
                bearer_token,
                max_token_bytes=self._max_token_bytes,
            )
        except (TypeError, ValueError):
            raise AuthenticationBoundaryError("invalid_bearer_token") from None
        candidate_digest = hashlib.sha256(candidate).digest()
        if not hmac.compare_digest(candidate_digest, self._expected_digest):
            raise AuthenticationBoundaryError("invalid_bearer_token")
        return self._principal_factory.create(now=now)

    def __repr__(self) -> str:
        """Never expose the retained digest or configured local identity."""

        return "LocalBearerAuthenticator(configured=True)"


def _validated_token_bytes(token: str, *, max_token_bytes: int) -> bytes:
    if not isinstance(token, str):
        raise TypeError("bearer token must be text")
    if not token or token != token.strip() or any(ord(character) < 0x21 for character in token):
        raise ValueError("bearer token has an invalid shape")
    encoded = token.encode("utf-8")
    if len(encoded) > max_token_bytes:
        raise ValueError("bearer token exceeds the configured bound")
    return encoded
