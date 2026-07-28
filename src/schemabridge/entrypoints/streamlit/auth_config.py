"""Secret-safe preflight for Streamlit's native OIDC transport."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit

from schemabridge.bootstrap import StreamlitRuntimeOptions

_PLACEHOLDER_MARKERS = (
    "replace-with",
    "change-me",
    "changeme",
    "placeholder",
    "example-secret",
)
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class StreamlitAuthConfigurationError(RuntimeError):
    """Sanitized failure before Streamlit reads identity state."""


@dataclass(frozen=True, slots=True)
class ValidatedStreamlitAuthConfiguration:
    """Non-secret evidence that the expected OIDC transport is configured."""

    provider: str


def validate_streamlit_auth_configuration(
    secrets: Mapping[str, object],
    runtime: StreamlitRuntimeOptions,
) -> ValidatedStreamlitAuthConfiguration:
    """Reject missing, weak, or transport-inconsistent OIDC configuration."""

    if runtime.auth_mode != "oidc":
        raise StreamlitAuthConfigurationError("OIDC preflight requires OIDC mode")
    provider_name = runtime.oidc_provider
    expected_audience = runtime.oidc_audience
    if provider_name is None or expected_audience is None:
        raise StreamlitAuthConfigurationError("OIDC runtime metadata is incomplete")

    auth = _mapping(secrets.get("auth"))
    provider = _mapping(auth.get(provider_name))
    redirect = _url(
        auth.get("redirect_uri"),
        runtime=runtime,
        callback=True,
    )
    metadata = _url(
        provider.get("server_metadata_url"),
        runtime=runtime,
        callback=False,
    )
    if runtime.oidc_issuer is None:
        raise StreamlitAuthConfigurationError("OIDC issuer is missing")
    issuer = _parse_url(runtime.oidc_issuer)
    if _origin(metadata) != _origin(issuer):
        raise StreamlitAuthConfigurationError("OIDC metadata origin does not match issuer")

    client_id = _required_text(provider.get("client_id"))
    client_secret = _secret(provider.get("client_secret"), minimum_length=16)
    cookie_secret = _secret(auth.get("cookie_secret"), minimum_length=32)
    if client_id != expected_audience:
        raise StreamlitAuthConfigurationError("OIDC client and audience do not match")
    if client_secret == cookie_secret:
        raise StreamlitAuthConfigurationError("OIDC secrets must be independent")
    if auth.get("expose_tokens") not in (None, False):
        raise StreamlitAuthConfigurationError("OIDC token exposure is forbidden")
    if not redirect.path.endswith("/oauth2callback"):
        raise StreamlitAuthConfigurationError("OIDC redirect path is invalid")
    return ValidatedStreamlitAuthConfiguration(provider=provider_name)


def _mapping(value: object | None) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise StreamlitAuthConfigurationError("OIDC secret section is missing")
    return value


def _required_text(value: object | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StreamlitAuthConfigurationError("OIDC secret value is missing")
    return value


def _secret(value: object | None, *, minimum_length: int) -> str:
    secret = _required_text(value)
    normalized = secret.casefold()
    if (
        len(secret.encode()) < minimum_length
        or len(set(secret)) < 8
        or any(marker in normalized for marker in _PLACEHOLDER_MARKERS)
    ):
        raise StreamlitAuthConfigurationError("OIDC secret value is not acceptable")
    return secret


def _url(
    value: object | None,
    *,
    runtime: StreamlitRuntimeOptions,
    callback: bool,
) -> SplitResult:
    parsed = _parse_url(_required_text(value))
    if runtime.profile in {"staging", "production"}:
        if parsed.scheme != "https":
            raise StreamlitAuthConfigurationError("Managed OIDC transport requires HTTPS")
    elif parsed.scheme == "http" and parsed.hostname not in _LOOPBACK_HOSTS:
        raise StreamlitAuthConfigurationError("Development HTTP OIDC is loopback-only")
    if parsed.query or parsed.fragment:
        kind = "redirect" if callback else "metadata"
        raise StreamlitAuthConfigurationError(f"OIDC {kind} URL is invalid")
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
        raise StreamlitAuthConfigurationError("OIDC URL is invalid")
    return parsed


def _origin(value: SplitResult) -> tuple[str, str, int]:
    default_port = 443 if value.scheme == "https" else 80
    return value.scheme, value.hostname or "", value.port or default_port
