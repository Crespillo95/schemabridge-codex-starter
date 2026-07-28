from __future__ import annotations

from dataclasses import replace

import pytest

from schemabridge.bootstrap import StreamlitRuntimeOptions
from schemabridge.entrypoints.streamlit.auth_config import (
    StreamlitAuthConfigurationError,
    validate_streamlit_auth_configuration,
)


def _runtime(
    *,
    profile: str = "production",
    issuer: str = "https://identity.example.test",
) -> StreamlitRuntimeOptions:
    return StreamlitRuntimeOptions(
        profile=profile,  # type: ignore[arg-type]
        auth_mode="oidc",
        catalog_kind="recorded",
        registry_kind="recorded",
        publication_kind="fake",
        execution_kind="recorded",
        oidc_provider="corporate",
        oidc_audience="schemabridge",
        oidc_issuer=issuer,
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
    validated = validate_streamlit_auth_configuration(_secrets(), _runtime())

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
    with pytest.raises(StreamlitAuthConfigurationError) as failure:
        validate_streamlit_auth_configuration(secrets, _runtime())

    message = str(failure.value)
    assert "replace-with" not in message
    assert "attacker.example" not in message
    assert "G7nYw4" not in message


def test_development_allows_only_loopback_http_oidc() -> None:
    runtime = _runtime(profile="development", issuer="http://127.0.0.1:9100")
    local = _secrets(
        redirect_uri="http://127.0.0.1:8501/oauth2callback",
        metadata_url="http://127.0.0.1:9100/.well-known/openid-configuration",
    )

    validate_streamlit_auth_configuration(local, runtime)

    with pytest.raises(StreamlitAuthConfigurationError):
        validate_streamlit_auth_configuration(
            _secrets(
                redirect_uri="http://dev.example.test/oauth2callback",
                metadata_url="http://127.0.0.1:9100/.well-known/openid-configuration",
            ),
            runtime,
        )


def test_preflight_rejects_non_oidc_runtime() -> None:
    with pytest.raises(StreamlitAuthConfigurationError):
        validate_streamlit_auth_configuration(
            _secrets(),
            replace(_runtime(), auth_mode="local-demo"),
        )
