from __future__ import annotations

import pytest

from schemabridge.adapters.identity.streamlit_auth import (
    StreamlitAuthConfigurationValidator,
)
from schemabridge.application.ports.browser_auth import (
    BrowserAuthConfigurationError,
    BrowserOidcRequirements,
)
from schemabridge.bootstrap import StreamlitRuntimeOptions


def _requirements(
    *,
    profile: str = "production",
    issuer: str = "https://identity.example.test",
) -> BrowserOidcRequirements:
    return BrowserOidcRequirements(
        profile=profile,  # type: ignore[arg-type]
        provider="corporate",
        audience="schemabridge",
        issuer=issuer,
    )


def _secrets(
    *,
    redirect_uri: str = "https://app.example.test/oauth2callback",
    metadata_url: str = ("https://identity.example.test/.well-known/openid-configuration"),
    cookie_secret: str = "6KzTEe9Wt7JfM2xP8vAc4nQ1sRg5yUhB",
    client_id: str = "schemabridge",
    client_secret: str = "G7nYw4rQ9tVz6mXp2sKa8cHd",
) -> dict[str, object]:
    return {
        "auth": {
            "redirect_uri": redirect_uri,
            "cookie_secret": cookie_secret,
            "corporate": {
                "client_id": client_id,
                "client_secret": client_secret,
                "server_metadata_url": metadata_url,
            },
        }
    }


def test_managed_oidc_secret_preflight_accepts_coherent_https_configuration() -> None:
    validated = StreamlitAuthConfigurationValidator().validate(
        _secrets(),
        _requirements(),
    )

    assert validated.provider == "corporate"
    assert "secret" not in repr(validated).casefold()


@pytest.mark.parametrize(
    "secrets",
    (
        {},
        {"auth": {}},
        _secrets(cookie_secret="replace-with-at-least-32-random-bytes"),
        _secrets(client_secret="short"),
        _secrets(client_id="other-client"),
        _secrets(redirect_uri="http://app.example.test/oauth2callback"),
        _secrets(metadata_url="http://identity.example.test/.well-known/openid-configuration"),
        _secrets(metadata_url="https://attacker.example.test/.well-known/openid-configuration"),
        _secrets(redirect_uri="https://app.example.test/not-the-callback"),
        {
            **_secrets(),
            "auth": {
                **_secrets()["auth"],  # type: ignore[dict-item]
                "expose_tokens": True,
            },
        },
    ),
)
def test_managed_oidc_secret_preflight_fails_closed_without_echoing_values(
    secrets: dict[str, object],
) -> None:
    with pytest.raises(BrowserAuthConfigurationError) as failure:
        StreamlitAuthConfigurationValidator().validate(
            secrets,
            _requirements(),
        )

    message = str(failure.value)
    assert "replace-with" not in message
    assert "attacker.example" not in message
    assert "G7nYw4" not in message


def test_development_allows_only_loopback_http_oidc() -> None:
    requirements = _requirements(
        profile="development",
        issuer="http://127.0.0.1:9100",
    )
    local = _secrets(
        redirect_uri="http://127.0.0.1:8501/oauth2callback",
        metadata_url="http://127.0.0.1:9100/.well-known/openid-configuration",
    )

    validator = StreamlitAuthConfigurationValidator()
    validator.validate(local, requirements)

    with pytest.raises(BrowserAuthConfigurationError):
        validator.validate(
            _secrets(
                redirect_uri="http://dev.example.test/oauth2callback",
                metadata_url="http://127.0.0.1:9100/.well-known/openid-configuration",
            ),
            requirements,
        )


def test_preflight_rejects_non_oidc_runtime() -> None:
    runtime = StreamlitRuntimeOptions(
        profile="development",
        auth_mode="local-demo",
        catalog_kind="recorded",
        registry_kind="recorded",
        publication_kind="fake",
        execution_kind="recorded",
        oidc_provider=None,
        oidc_audience=None,
        oidc_issuer=None,
    )

    with pytest.raises(BrowserAuthConfigurationError):
        runtime.require_auth_configuration(_secrets())
