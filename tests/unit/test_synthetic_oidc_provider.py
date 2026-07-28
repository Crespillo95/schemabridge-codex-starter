"""Security contract for the loopback-only synthetic OIDC browser harness."""

from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from joserfc import jwt
from joserfc.jwk import KeySet
from scripts.synthetic_oidc_provider import (
    ProviderConfig,
    SyntheticIdentity,
    SyntheticOidcProvider,
)

CLIENT_ID = "schemabridge-browser-test"
CLIENT_SECRET = "synthetic-browser-secret"
REDIRECT_URI = "http://127.0.0.1:8501/oauth2callback"
VERIFIER = "v" * 64
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode("ascii")).digest())
    .rstrip(b"=")
    .decode("ascii")
)


class MutableClock:
    def __init__(self, value: float = 1_800_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _provider(
    *,
    clock: MutableClock | None = None,
    identity: SyntheticIdentity | None = None,
) -> SyntheticOidcProvider:
    return SyntheticOidcProvider(
        ProviderConfig(
            host="127.0.0.1",
            port=9100,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_uri=REDIRECT_URI,
            identity=identity
            or SyntheticIdentity(
                subject="synthetic-analyst-a",
                tenant="tenant-a",
                groups=("analysts",),
            ),
        ),
        clock=clock or MutableClock(),
    )


def _authorize(
    provider: SyntheticOidcProvider,
    *,
    extra: dict[str, str] | None = None,
) -> str:
    parameters = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": "openid profile",
        "state": "streamlit-state",
        "nonce": "streamlit-nonce",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        **(extra or {}),
    }
    response = provider.route("GET", f"/authorize?{urlencode(parameters)}")

    assert response.status == 302
    location = response.header("Location")
    assert location is not None
    redirected = urlsplit(location)
    assert f"{redirected.scheme}://{redirected.netloc}{redirected.path}" == REDIRECT_URI
    query = parse_qs(redirected.query)
    assert query["state"] == ["streamlit-state"]
    return query["code"][0]


def _basic_authorization() -> str:
    encoded = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    return f"Basic {encoded}"


def _exchange(
    provider: SyntheticOidcProvider,
    code: str,
    *,
    verifier: str = VERIFIER,
) -> object:
    body = urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        }
    ).encode()
    return provider.route(
        "POST",
        "/token",
        headers={
            "Authorization": _basic_authorization(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        body=body,
    )


def test_discovery_advertises_only_supported_loopback_code_flow() -> None:
    provider = _provider()

    health = provider.route("GET", "/health")
    discovery = provider.route("GET", "/.well-known/openid-configuration")
    document = json.loads(discovery.body)

    assert health.status == 200
    assert health.body == b"ok\n"
    assert discovery.status == 200
    assert document["issuer"] == "http://127.0.0.1:9100"
    assert document["response_types_supported"] == ["code"]
    assert document["grant_types_supported"] == ["authorization_code"]
    assert document["token_endpoint_auth_methods_supported"] == ["client_secret_basic"]
    assert document["code_challenge_methods_supported"] == ["S256"]
    assert document["id_token_signing_alg_values_supported"] == ["RS256"]
    assert document["authorization_endpoint"].endswith("/authorize")
    assert document["token_endpoint"].endswith("/token")
    assert document["jwks_uri"].endswith("/jwks")
    assert document["end_session_endpoint"].endswith("/logout")


def test_authorization_code_flow_binds_nonce_pkce_and_server_selected_identity() -> None:
    identity = SyntheticIdentity(
        subject="synthetic-publisher-a",
        tenant="tenant-a",
        groups=("publishers", "auditors"),
    )
    provider = _provider(identity=identity)
    code = _authorize(
        provider,
        extra={
            "sub": "attacker-selected-subject",
            "tenant_id": "attacker-tenant",
            "groups": "platform_admin",
        },
    )

    response = _exchange(provider, code)
    payload = json.loads(response.body)
    jwks = json.loads(provider.route("GET", "/jwks").body)
    token = jwt.decode(
        payload["id_token"],
        key=KeySet.import_key_set(jwks),
        algorithms=["RS256"],
    )
    access_token = jwt.decode(
        payload["access_token"],
        key=KeySet.import_key_set(jwks),
        algorithms=["RS256"],
    )

    assert response.status == 200
    assert response.header("Cache-Control") == "no-store"
    assert payload["token_type"] == "Bearer"
    assert token.claims["iss"] == "http://127.0.0.1:9100"
    assert token.claims["sub"] == identity.subject
    assert token.claims["aud"] == CLIENT_ID
    assert token.claims["azp"] == CLIENT_ID
    assert token.claims["nonce"] == "streamlit-nonce"
    assert token.claims["tenant_id"] == identity.tenant
    assert token.claims["groups"] == list(identity.groups)
    assert access_token.claims["iss"] == token.claims["iss"]
    assert access_token.claims["sub"] == token.claims["sub"]
    assert access_token.claims["aud"] == CLIENT_ID
    assert access_token.claims["azp"] == CLIENT_ID
    assert access_token.claims["tenant_id"] == identity.tenant
    assert access_token.claims["groups"] == list(identity.groups)
    assert "nonce" not in access_token.claims
    assert "auth_time" not in access_token.claims
    assert "attacker" not in json.dumps(token.claims)
    assert "attacker" not in json.dumps(access_token.claims)
    assert "email" not in token.claims
    assert "email" not in access_token.claims


def test_authorization_codes_are_one_use_and_bad_pkce_consumes_the_code() -> None:
    provider = _provider()
    first_code = _authorize(provider)
    first = _exchange(provider, first_code)
    replay = _exchange(provider, first_code)
    second_code = _authorize(provider)
    bad_pkce = _exchange(provider, second_code, verifier="x" * 64)
    second_attempt = _exchange(provider, second_code)

    assert first.status == 200
    assert replay.status == 400
    assert json.loads(replay.body)["error"] == "invalid_grant"
    assert bad_pkce.status == 400
    assert json.loads(bad_pkce.body)["error"] == "invalid_grant"
    assert second_attempt.status == 400
    assert json.loads(second_attempt.body)["error"] == "invalid_grant"


def test_expired_code_and_invalid_client_fail_without_sensitive_values() -> None:
    clock = MutableClock()
    provider = _provider(clock=clock)
    expired_code = _authorize(provider)
    clock.value += 121

    expired = _exchange(provider, expired_code)
    invalid_client_body = urlencode(
        {
            "grant_type": "authorization_code",
            "code": "not-a-code",
            "redirect_uri": REDIRECT_URI,
            "code_verifier": VERIFIER,
        }
    ).encode()
    invalid_client = provider.route(
        "POST",
        "/token",
        headers={
            "Authorization": "Basic "
            + base64.b64encode(f"{CLIENT_ID}:wrong-secret".encode()).decode(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        body=invalid_client_body,
    )
    combined = expired.body + invalid_client.body

    assert expired.status == 400
    assert json.loads(expired.body)["error"] == "invalid_grant"
    assert invalid_client.status == 401
    assert json.loads(invalid_client.body)["error"] == "invalid_client"
    assert CLIENT_SECRET.encode() not in combined
    assert expired_code.encode() not in combined
    assert b"wrong-secret" not in combined


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"state": ""}, "invalid_request"),
        ({"nonce": ""}, "invalid_request"),
        ({"redirect_uri": "http://127.0.0.1:9999/oauth2callback"}, "invalid_request"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"scope": "profile"}, "invalid_scope"),
    ],
)
def test_authorize_rejects_unbound_streamlit_requests(
    changes: dict[str, str],
    error: str,
) -> None:
    provider = _provider()
    parameters = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": "openid profile",
        "state": "state",
        "nonce": "nonce",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        **changes,
    }

    response = provider.route("GET", f"/authorize?{urlencode(parameters)}")

    assert response.status == 400
    assert json.loads(response.body)["error"] == error


def test_logout_accepts_only_the_registered_streamlit_callback() -> None:
    provider = _provider()
    valid = provider.route(
        "GET",
        "/logout?"
        + urlencode(
            {
                "client_id": CLIENT_ID,
                "post_logout_redirect_uri": REDIRECT_URI,
                "id_token_hint": "opaque-and-not-logged",
            }
        ),
    )
    invalid = provider.route(
        "GET",
        "/logout?"
        + urlencode(
            {
                "client_id": CLIENT_ID,
                "post_logout_redirect_uri": "https://attacker.example/callback",
            }
        ),
    )

    assert valid.status == 302
    assert valid.header("Location") == REDIRECT_URI
    assert invalid.status == 400
    assert b"attacker.example" not in invalid.body


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.10", "localhost"])
def test_provider_rejects_every_non_literal_ipv4_loopback_bind(host: str) -> None:
    with pytest.raises(ValueError, match="IPv4 loopback"):
        ProviderConfig(
            host=host,
            port=9100,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_uri=REDIRECT_URI,
            identity=SyntheticIdentity(
                subject="synthetic-user",
                tenant="tenant-a",
                groups=("analysts",),
            ),
        )


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "http://localhost:8501/oauth2callback",
        "http://127.0.0.1/oauth2callback",
        "http://127.0.0.1:bad/oauth2callback",
        "https://127.0.0.1:8501/oauth2callback",
        "http://127.0.0.1:8501/other",
    ],
)
def test_provider_rejects_non_exact_loopback_callback_shapes(redirect_uri: str) -> None:
    with pytest.raises(ValueError, match="redirect_uri"):
        ProviderConfig(
            host="127.0.0.1",
            port=9100,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_uri=redirect_uri,
            identity=SyntheticIdentity(
                subject="synthetic-user",
                tenant="tenant-a",
                groups=("analysts",),
            ),
        )


def test_config_repr_and_ready_message_never_contain_client_secret() -> None:
    provider = _provider()

    assert CLIENT_SECRET not in repr(provider.config)
    assert CLIENT_SECRET not in provider.ready_message()
