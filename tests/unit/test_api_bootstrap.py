from __future__ import annotations

from datetime import UTC, datetime

import pytest

from schemabridge.application.authentication import AuthenticationBoundaryError
from schemabridge.application.postgres_health import DatabaseConfigurationError
from schemabridge.bootstrap import build_bearer_authenticator
from schemabridge.config import Settings
from schemabridge.domain.identity import AuthenticationMethod

NOW = datetime(2026, 7, 23, 18, 0, tzinfo=UTC)
LOCAL_TOKEN = "local-api-bearer-token-with-enough-byte-diversity-123"


def test_local_bearer_composition_requires_an_explicit_secret() -> None:
    with pytest.raises(DatabaseConfigurationError, match="API_LOCAL_BEARER_TOKEN"):
        build_bearer_authenticator(Settings.model_validate({}))


def test_local_bearer_composition_authenticates_without_exposing_the_secret() -> None:
    authenticator = build_bearer_authenticator(
        Settings.model_validate({"SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN": LOCAL_TOKEN})
    )

    principal = authenticator.authenticate(LOCAL_TOKEN, NOW)

    assert principal.authentication_method is AuthenticationMethod.LOCAL_DEMO
    assert LOCAL_TOKEN not in repr(authenticator)
    with pytest.raises(AuthenticationBoundaryError) as failure:
        authenticator.authenticate(f"{LOCAL_TOKEN}-wrong", NOW)
    assert str(failure.value) == "The authenticated session was rejected."


def test_oidc_bearer_composition_requires_an_explicit_jwks_url() -> None:
    with pytest.raises(DatabaseConfigurationError, match="API_OIDC_JWKS_URL"):
        build_bearer_authenticator(Settings.model_validate(_oidc_settings()))


def test_oidc_bearer_composition_accepts_loopback_only_in_development() -> None:
    payload = _oidc_settings()
    payload["SCHEMABRIDGE_API_OIDC_JWKS_URL"] = "http://127.0.0.1:8765/jwks"

    authenticator = build_bearer_authenticator(Settings.model_validate(payload))

    assert "configured=True" in repr(authenticator)
    assert "127.0.0.1" not in repr(authenticator)


def _oidc_settings() -> dict[str, object]:
    return {
        "SCHEMABRIDGE_ENVIRONMENT": "development",
        "SCHEMABRIDGE_AUTH_MODE": "oidc",
        "SCHEMABRIDGE_OIDC_ISSUER": "http://127.0.0.1:8765",
        "SCHEMABRIDGE_OIDC_AUDIENCE": "schemabridge-api",
        "SCHEMABRIDGE_OIDC_PROVIDER": "synthetic",
        "SCHEMABRIDGE_OIDC_ROLE_CLAIM": "groups",
        "SCHEMABRIDGE_OIDC_ALLOWED_GROUPS": {"analysts": ("analyst",)},
        "SCHEMABRIDGE_OIDC_ALLOWED_TENANTS": ("tenant-a",),
        "SCHEMABRIDGE_PSEUDONYMIZATION_KEY": ("api-bootstrap-pseudonymization-key-with-diversity"),
    }
