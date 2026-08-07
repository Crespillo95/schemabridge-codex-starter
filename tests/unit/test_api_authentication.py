from __future__ import annotations

import hmac
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import ClassVar, cast

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from pydantic import SecretStr

from schemabridge.adapters.identity.local import LocalDemoPrincipalFactory
from schemabridge.adapters.identity.local_bearer import LocalBearerAuthenticator
from schemabridge.adapters.identity.oidc import OidcPrincipalMapper
from schemabridge.adapters.identity.oidc_bearer import OidcBearerAuthenticator
from schemabridge.application.authentication import AuthenticationBoundaryError
from schemabridge.application.ports.authentication import BearerAuthenticationPort
from schemabridge.domain.identity import AuthenticationMethod, IdentityRole

NOW = datetime(2026, 7, 23, 18, 0, tzinfo=UTC)
ISSUER = "https://identity.example.test"
AUDIENCE = "schemabridge-api"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
KEY_ID = "signing-key-1"
PSEUDONYM_KEY = b"m24-unit-test-pseudonym-key-with-enough-diversity"
LOCAL_TOKEN = "development-only-fixed-bearer-42"
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
OTHER_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)


def _public_jwk(
    *,
    key_id: str = KEY_ID,
    algorithm: str = "RS256",
) -> dict[str, object]:
    value = cast(
        dict[str, object],
        RSAAlgorithm.to_jwk(PRIVATE_KEY.public_key(), as_dict=True),
    )
    value.update(
        {
            "kid": key_id,
            "alg": algorithm,
            "use": "sig",
            "key_ops": ["verify"],
        }
    )
    return value


def _jwks_bytes(*keys: dict[str, object]) -> bytes:
    return json.dumps({"keys": list(keys or (_public_jwk(),))}).encode()


def _claims(**overrides: object) -> dict[str, object]:
    claims: dict[str, object] = {
        "iss": ISSUER,
        "sub": "private-provider-subject",
        "aud": AUDIENCE,
        "azp": AUDIENCE,
        "iat": (NOW - timedelta(minutes=1)).timestamp(),
        "nbf": (NOW - timedelta(minutes=1)).timestamp(),
        "exp": (NOW + timedelta(minutes=9)).timestamp(),
        "tenant_id": "tenant-a",
        "groups": ["analysts", "unknown-provider-group"],
        "email": "must-not-survive@example.test",
    }
    claims.update(overrides)
    return claims


def _signed_token(
    *,
    claims: dict[str, object] | None = None,
    key_id: str | None = KEY_ID,
    private_key: rsa.RSAPrivateKey = PRIVATE_KEY,
    extra_headers: dict[str, object] | None = None,
) -> str:
    headers: dict[str, object] = {}
    if key_id is not None:
        headers["kid"] = key_id
    if extra_headers:
        headers.update(extra_headers)
    return jwt.encode(
        claims or _claims(),
        private_key,
        algorithm="RS256",
        headers=headers,
    )


def _mapper() -> OidcPrincipalMapper:
    return OidcPrincipalMapper(
        expected_issuer=ISSUER,
        expected_audience=AUDIENCE,
        allowed_group_roles={
            "analysts": frozenset({IdentityRole.ANALYST}),
            "publishers": frozenset({IdentityRole.PUBLISHER}),
        },
        allowed_tenants=frozenset({"tenant-a", "tenant-b"}),
        pseudonymization_key=PSEUDONYM_KEY,
        max_session_age=timedelta(hours=1),
    )


@dataclass
class _ScriptedFetcher:
    responses: list[bytes | Exception]
    calls: list[tuple[str, float, int]] = field(default_factory=list)

    def __call__(self, url: str, timeout_seconds: float, max_bytes: int) -> bytes:
        self.calls.append((url, timeout_seconds, max_bytes))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _authenticator(
    fetcher: _ScriptedFetcher | None = None,
    **overrides: object,
) -> OidcBearerAuthenticator:
    options: dict[str, object] = {
        "mapper": _mapper(),
        "jwks_url": JWKS_URL,
        "algorithms": ("RS256",),
        "jwks_fetcher": fetcher or _ScriptedFetcher([_jwks_bytes()]),
    }
    options.update(overrides)
    return OidcBearerAuthenticator(**options)  # type: ignore[arg-type]


def _assert_sanitized(
    failure: pytest.ExceptionInfo[AuthenticationBoundaryError],
    expected_code: str,
    *forbidden: str,
) -> None:
    assert failure.value.code == expected_code
    assert str(failure.value) == "The authenticated session was rejected."
    assert failure.value.__context__ is None or failure.value.__suppress_context__
    rendered = f"{failure.value!s} {failure.value!r}"
    assert all(not value or value not in rendered for value in forbidden)


def test_oidc_port_verifies_signature_before_mapping_a_minimal_principal() -> None:
    fetcher = _ScriptedFetcher([_jwks_bytes()])
    authenticator = _authenticator(fetcher)
    port: BearerAuthenticationPort = authenticator
    token = _signed_token()

    principal = port.authenticate(token, NOW)

    assert principal.authentication_method is AuthenticationMethod.OIDC
    assert principal.roles == frozenset({IdentityRole.ANALYST})
    assert principal.actor_id.startswith("sb_actor_v1_")
    assert principal.workspace_id.startswith("sb_workspace_v1_")
    assert "private-provider-subject" not in repr(principal)
    assert "tenant-a" not in repr(principal)
    assert "must-not-survive@example.test" not in repr(principal)
    assert token not in repr(authenticator)
    assert PSEUDONYM_KEY.decode() not in repr(authenticator)
    assert fetcher.calls == [(JWKS_URL, 5.0, 65_536)]


def test_oidc_rejects_a_token_with_a_forged_signature() -> None:
    token = _signed_token(private_key=OTHER_PRIVATE_KEY)

    with pytest.raises(AuthenticationBoundaryError) as failure:
        _authenticator().authenticate(token, NOW)

    _assert_sanitized(failure, "invalid_bearer_token", token)


def test_oidc_rejects_alg_none_and_hmac_rsa_confusion_before_jwks_io() -> None:
    fetcher = _ScriptedFetcher([_jwks_bytes()])
    authenticator = _authenticator(fetcher)
    none_token = jwt.encode(
        _claims(),
        key="",
        algorithm="none",
        headers={"kid": KEY_ID},
    )
    confused_token = jwt.encode(
        _claims(),
        key=b"attacker-chosen-hmac-key-with-adequate-length",
        algorithm="HS256",
        headers={"kid": KEY_ID},
    )

    for token in (none_token, confused_token):
        with pytest.raises(AuthenticationBoundaryError) as failure:
            authenticator.authenticate(token, NOW)
        _assert_sanitized(failure, "invalid_bearer_token", token)
    assert fetcher.calls == []


@pytest.mark.parametrize(
    "token",
    (
        _signed_token(key_id=None),
        _signed_token(key_id=" "),
        _signed_token(key_id="x" * 129),
        _signed_token(extra_headers={"jku": "https://attacker.example.test/keys"}),
        _signed_token(extra_headers={"x5u": "https://attacker.example.test/cert"}),
        _signed_token(extra_headers={"crit": ["attacker-extension"]}),
    ),
)
def test_oidc_rejects_missing_unbounded_or_remote_key_headers_without_io(token: str) -> None:
    fetcher = _ScriptedFetcher([_jwks_bytes()])

    with pytest.raises(AuthenticationBoundaryError) as failure:
        _authenticator(fetcher).authenticate(token, NOW)

    _assert_sanitized(failure, "invalid_bearer_token", token, "attacker.example.test")
    assert fetcher.calls == []


def test_oidc_unknown_kid_refreshes_once_and_fails_closed() -> None:
    unknown = "unknown-key-private-label"
    fetcher = _ScriptedFetcher([_jwks_bytes()])

    with pytest.raises(AuthenticationBoundaryError) as failure:
        _authenticator(fetcher).authenticate(_signed_token(key_id=unknown), NOW)

    _assert_sanitized(failure, "invalid_bearer_token", unknown)
    assert len(fetcher.calls) == 1


@pytest.mark.parametrize(
    "overrides",
    (
        {"iss": "https://attacker.example.test"},
        {"aud": "wrong-audience-private-label"},
        {"azp": "wrong-client-private-label"},
        {"tenant_id": "tenant-not-allowed-private-label"},
        {"groups": "analysts"},
        {"groups": ["analysts", " "]},
    ),
)
def test_oidc_rejects_signed_claims_outside_the_fixed_policy(
    overrides: dict[str, object],
) -> None:
    token = _signed_token(claims=_claims(**overrides))

    with pytest.raises(AuthenticationBoundaryError) as failure:
        _authenticator().authenticate(token, NOW)

    _assert_sanitized(
        failure,
        "invalid_bearer_token",
        token,
        *(str(value) for value in overrides.values()),
    )


@pytest.mark.parametrize(
    "overrides",
    (
        {"iat": (NOW + timedelta(seconds=1)).timestamp()},
        {"nbf": (NOW + timedelta(seconds=1)).timestamp()},
        {"exp": NOW.timestamp()},
        {
            "iat": (NOW - timedelta(hours=2)).timestamp(),
            "nbf": (NOW - timedelta(hours=2)).timestamp(),
        },
        {"iat": "not-a-time"},
        {"exp": float("inf")},
    ),
)
def test_oidc_rejects_non_current_malformed_or_over_age_signed_times(
    overrides: dict[str, object],
) -> None:
    token = _signed_token(claims=_claims(**overrides))

    with pytest.raises(AuthenticationBoundaryError) as failure:
        _authenticator().authenticate(token, NOW)

    _assert_sanitized(failure, "invalid_bearer_token", token)


def test_oidc_requires_all_signed_identity_and_time_claims() -> None:
    for claim_name in ("iss", "sub", "aud", "azp", "iat", "nbf", "exp"):
        claims = _claims()
        del claims[claim_name]
        token = _signed_token(claims=claims)

        with pytest.raises(AuthenticationBoundaryError) as failure:
            _authenticator().authenticate(token, NOW)

        _assert_sanitized(failure, "invalid_bearer_token", token, claim_name)


def test_oidc_rejects_jwk_algorithm_mismatch() -> None:
    fetcher = _ScriptedFetcher([_jwks_bytes(_public_jwk(algorithm="PS256"))])

    with pytest.raises(AuthenticationBoundaryError) as failure:
        _authenticator(fetcher).authenticate(_signed_token(), NOW)

    _assert_sanitized(failure, "invalid_bearer_token")


def test_oidc_jwks_outage_without_a_current_key_fails_closed_and_redacted() -> None:
    provider_detail = "https://private-provider.example.test/jwks?credential=secret"
    fetcher = _ScriptedFetcher([urllib.error.URLError(provider_detail)])
    token = _signed_token()

    with pytest.raises(AuthenticationBoundaryError) as failure:
        _authenticator(fetcher).authenticate(token, NOW)

    _assert_sanitized(
        failure,
        "authentication_unavailable",
        token,
        provider_detail,
        "credential",
    )


def test_oidc_uses_only_a_current_cached_key_and_failed_refresh_does_not_clear_it() -> None:
    current_time = [100.0]
    fetcher = _ScriptedFetcher(
        [
            _jwks_bytes(),
            urllib.error.URLError("private-provider-outage"),
        ]
    )
    authenticator = _authenticator(
        fetcher,
        cache_ttl_seconds=300.0,
        monotonic=lambda: current_time[0],
    )
    token = _signed_token()

    first = authenticator.authenticate(token, NOW)
    current_time[0] = 200.0
    cached = authenticator.authenticate(token, NOW)
    assert cached == first
    assert len(fetcher.calls) == 1

    current_time[0] = 401.0
    with pytest.raises(AuthenticationBoundaryError) as failure:
        authenticator.authenticate(token, NOW)
    _assert_sanitized(failure, "authentication_unavailable", "private-provider-outage")
    assert len(fetcher.calls) == 2


@pytest.mark.parametrize(
    "body",
    (
        b"",
        b"not-json",
        b'{"keys":[]}',
        b'{"keys":[],"keys":[]}',
        json.dumps(
            {"keys": [_public_jwk(), _public_jwk()]},
        ).encode(),
        b"x" * 65_537,
    ),
)
def test_oidc_rejects_malformed_ambiguous_or_oversized_jwks(body: bytes) -> None:
    with pytest.raises(AuthenticationBoundaryError) as failure:
        _authenticator(_ScriptedFetcher([body])).authenticate(_signed_token(), NOW)

    _assert_sanitized(failure, "authentication_unavailable", body[:20].decode(errors="ignore"))


class _RedirectedResponse:
    headers: ClassVar[dict[str, str]] = {}

    def __enter__(self) -> _RedirectedResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    @staticmethod
    def geturl() -> str:
        return "https://attacker.example.test/redirected-jwks"

    @staticmethod
    def getcode() -> int:
        return 200

    @staticmethod
    def read(_limit: int) -> bytes:
        return _jwks_bytes()


class _RedirectingOpener:
    seen_timeout: float | None = None

    def open(
        self,
        _request: urllib.request.Request,
        *,
        timeout: float,
    ) -> _RedirectedResponse:
        self.seen_timeout = timeout
        return _RedirectedResponse()


def test_oidc_rejects_a_redirected_jwks_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = _RedirectingOpener()
    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *_handlers: opener,
    )

    with pytest.raises(AuthenticationBoundaryError) as failure:
        OidcBearerAuthenticator(
            mapper=_mapper(),
            jwks_url=JWKS_URL,
            algorithms=("RS256",),
        ).authenticate(_signed_token(), NOW)

    _assert_sanitized(failure, "authentication_unavailable", "attacker.example.test")
    assert opener.seen_timeout == 5.0


@pytest.mark.parametrize(
    ("url", "allow_loopback"),
    (
        ("http://identity.example.test/jwks", False),
        ("http://127.0.0.1:9999/jwks", False),
        ("file:///private/keys.json", False),
        ("https://user:password@identity.example.test/jwks", False),
        ("https://identity.example.test/jwks?token=secret", False),
        ("https://identity.example.test/jwks#fragment", False),
    ),
)
def test_oidc_requires_a_static_https_jwks_url(
    url: str,
    allow_loopback: bool,
) -> None:
    with pytest.raises(ValueError, match=r"jwks_url|HTTPS"):
        OidcBearerAuthenticator(
            mapper=_mapper(),
            jwks_url=url,
            algorithms=("RS256",),
            allow_insecure_loopback=allow_loopback,
        )


def test_oidc_allows_plain_http_only_for_explicit_loopback_development() -> None:
    authenticator = OidcBearerAuthenticator(
        mapper=_mapper(),
        jwks_url="http://127.0.0.1:9999/jwks",
        algorithms=("RS256",),
        allow_insecure_loopback=True,
        jwks_fetcher=_ScriptedFetcher([_jwks_bytes()]),
    )

    assert authenticator.authenticate(_signed_token(), NOW).roles == frozenset(
        {IdentityRole.ANALYST}
    )


@pytest.mark.parametrize(
    "algorithms",
    (
        (),
        ("none",),
        ("HS256",),
        ("RS256", "RS256"),
        ("RS256", "HS256"),
    ),
)
def test_oidc_constructor_rejects_empty_symmetric_or_ambiguous_algorithm_policy(
    algorithms: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="asymmetric"):
        OidcBearerAuthenticator(
            mapper=_mapper(),
            jwks_url=JWKS_URL,
            algorithms=algorithms,
        )


@pytest.mark.parametrize(
    "token",
    (
        "",
        " leading",
        "trailing ",
        "contains\tcontrol",
        "not.a.jwt.with.too.many.parts",
        "x" * 16_385,
    ),
)
def test_oidc_rejects_malformed_or_oversized_tokens_without_jwks_io(token: str) -> None:
    fetcher = _ScriptedFetcher([_jwks_bytes()])

    with pytest.raises(AuthenticationBoundaryError) as failure:
        _authenticator(fetcher).authenticate(token, NOW)

    _assert_sanitized(failure, "invalid_bearer_token", token)
    assert fetcher.calls == []


def _local_factory() -> LocalDemoPrincipalFactory:
    return LocalDemoPrincipalFactory(
        workspace="development-workspace",
        subject="development-operator",
        roles=frozenset({IdentityRole.ANALYST}),
        session_ttl=timedelta(minutes=15),
    )


def test_local_bearer_is_development_only_constant_time_and_does_not_retain_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comparisons: list[tuple[bytes, bytes]] = []
    real_compare_digest = hmac.compare_digest

    def recording_compare_digest(candidate: bytes, expected: bytes) -> bool:
        comparisons.append((candidate, expected))
        return bool(real_compare_digest(candidate, expected))

    monkeypatch.setattr(hmac, "compare_digest", recording_compare_digest)
    authenticator = LocalBearerAuthenticator(
        configured_token=SecretStr(LOCAL_TOKEN),
        principal_factory=_local_factory(),
        runtime_profile="development",
    )
    port: BearerAuthenticationPort = authenticator

    with pytest.raises(AuthenticationBoundaryError) as failure:
        port.authenticate("wrong", NOW)
    _assert_sanitized(failure, "invalid_bearer_token", LOCAL_TOKEN, "wrong")
    principal = port.authenticate(LOCAL_TOKEN, NOW)

    assert principal.authentication_method is AuthenticationMethod.LOCAL_DEMO
    assert principal.roles == frozenset({IdentityRole.ANALYST})
    assert len(comparisons) == 2
    assert all(len(candidate) == len(expected) == 32 for candidate, expected in comparisons)
    assert LOCAL_TOKEN not in repr(authenticator)
    assert not hasattr(authenticator, "__dict__")
    assert authenticator._expected_digest != LOCAL_TOKEN.encode()


@pytest.mark.parametrize("runtime_profile", ("hosted-demo", "staging", "production"))
def test_local_bearer_rejects_every_non_development_profile(runtime_profile: str) -> None:
    with pytest.raises(ValueError, match="development-only"):
        LocalBearerAuthenticator(
            configured_token=SecretStr(LOCAL_TOKEN),
            principal_factory=_local_factory(),
            runtime_profile=runtime_profile,
        )


@pytest.mark.parametrize("token", ("", " leading", "trailing ", "contains\ncontrol"))
def test_local_bearer_rejects_invalid_presented_shape_without_disclosure(token: str) -> None:
    authenticator = LocalBearerAuthenticator(
        configured_token=SecretStr(LOCAL_TOKEN),
        principal_factory=_local_factory(),
        runtime_profile="development",
    )

    with pytest.raises(AuthenticationBoundaryError) as failure:
        authenticator.authenticate(token, NOW)

    _assert_sanitized(failure, "invalid_bearer_token", token, LOCAL_TOKEN)


def test_local_bearer_rejects_invalid_configured_secret() -> None:
    for configured in ("", " leading", "short-but-varied", "x" * 64, "x" * 4_097):
        with pytest.raises(ValueError):
            LocalBearerAuthenticator(
                configured_token=SecretStr(configured),
                principal_factory=_local_factory(),
                runtime_profile="development",
            )
