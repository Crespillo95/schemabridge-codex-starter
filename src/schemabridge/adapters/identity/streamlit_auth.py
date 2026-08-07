"""Secret-safe adapter for Streamlit's native OIDC transport."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import SplitResult, urlsplit

from schemabridge.application.ports.browser_auth import (
    BrowserAuthConfigurationError,
    BrowserOidcRequirements,
    BrowserRuntimeProfile,
    ValidatedBrowserAuthConfiguration,
)

_PLACEHOLDER_MARKERS = (
    "replace-with",
    "change-me",
    "changeme",
    "placeholder",
    "example-secret",
)
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class StreamlitAuthConfigurationValidator:
    """Validate Streamlit TOML without depending on bootstrap or entrypoints."""

    def validate(
        self,
        secrets: Mapping[str, object],
        requirements: BrowserOidcRequirements,
    ) -> ValidatedBrowserAuthConfiguration:
        """Reject missing, weak, or transport-inconsistent OIDC configuration."""

        auth = _mapping(secrets.get("auth"))
        provider = _mapping(auth.get(requirements.provider))
        redirect = _url(
            auth.get("redirect_uri"),
            profile=requirements.profile,
            callback=True,
        )
        metadata = _url(
            provider.get("server_metadata_url"),
            profile=requirements.profile,
            callback=False,
        )
        issuer = _parse_url(requirements.issuer)
        if _origin(metadata) != _origin(issuer):
            raise BrowserAuthConfigurationError("OIDC metadata origin does not match issuer")

        client_id = _required_text(provider.get("client_id"))
        client_secret = _secret(provider.get("client_secret"), minimum_length=16)
        cookie_secret = _secret(auth.get("cookie_secret"), minimum_length=32)
        if client_id != requirements.audience:
            raise BrowserAuthConfigurationError("OIDC client and audience do not match")
        if client_secret == cookie_secret:
            raise BrowserAuthConfigurationError("OIDC secrets must be independent")
        if auth.get("expose_tokens") not in (None, False):
            raise BrowserAuthConfigurationError("OIDC token exposure is forbidden")
        if not redirect.path.endswith("/oauth2callback"):
            raise BrowserAuthConfigurationError("OIDC redirect path is invalid")
        return ValidatedBrowserAuthConfiguration(provider=requirements.provider)


def _mapping(value: object | None) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise BrowserAuthConfigurationError("OIDC secret section is missing")
    return value


def _required_text(value: object | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BrowserAuthConfigurationError("OIDC secret value is missing")
    return value


def _secret(value: object | None, *, minimum_length: int) -> str:
    secret = _required_text(value)
    normalized = secret.casefold()
    if (
        len(secret.encode()) < minimum_length
        or len(set(secret)) < 8
        or any(marker in normalized for marker in _PLACEHOLDER_MARKERS)
    ):
        raise BrowserAuthConfigurationError("OIDC secret value is not acceptable")
    return secret


def _url(
    value: object | None,
    *,
    profile: BrowserRuntimeProfile,
    callback: bool,
) -> SplitResult:
    parsed = _parse_url(_required_text(value))
    if profile in {"staging", "production"}:
        if parsed.scheme != "https":
            raise BrowserAuthConfigurationError("Managed OIDC transport requires HTTPS")
    elif parsed.scheme == "http" and parsed.hostname not in _LOOPBACK_HOSTS:
        raise BrowserAuthConfigurationError("Development HTTP OIDC is loopback-only")
    if parsed.query or parsed.fragment:
        kind = "redirect" if callback else "metadata"
        raise BrowserAuthConfigurationError(f"OIDC {kind} URL is invalid")
    return parsed


def _parse_url(value: str) -> SplitResult:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise BrowserAuthConfigurationError("OIDC URL is invalid")
    return parsed


def _origin(value: SplitResult) -> tuple[str, str, int]:
    default_port = 443 if value.scheme == "https" else 80
    return value.scheme, value.hostname or "", value.port or default_port
