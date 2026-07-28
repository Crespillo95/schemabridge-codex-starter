"""Bounded OIDC bearer verification before strict principal mapping."""

from __future__ import annotations

import json
import math
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime
from http.client import HTTPMessage
from types import MappingProxyType
from typing import Any, NoReturn
from urllib.parse import urlsplit

import jwt
from jwt import PyJWK

from schemabridge.adapters.identity.oidc import OidcClaimError, OidcPrincipalMapper
from schemabridge.application.authentication import AuthenticationBoundaryError
from schemabridge.domain.identity import AuthenticatedPrincipal

MAX_BEARER_TOKEN_BYTES = 16_384
MAX_JWKS_RESPONSE_BYTES = 65_536
MAX_JWKS_KEYS = 32
MAX_KEY_ID_LENGTH = 128
MAX_HTTP_TIMEOUT_SECONDS = 15.0
MAX_CACHE_TTL_SECONDS = 3_600.0
_ASYMMETRIC_ALGORITHMS = frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
        "ES256",
        "ES384",
        "ES512",
        "EdDSA",
    }
)
_PRIVATE_JWK_FIELDS = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth"})

JwksFetcher = Callable[[str, float, int], bytes]
MonotonicClock = Callable[[], float]


class _JwksUnavailableError(RuntimeError):
    pass


class _InvalidJwksError(RuntimeError):
    pass


class _UnknownKeyError(RuntimeError):
    pass


class _DuplicateJsonKey(ValueError):
    pass


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> NoReturn:
        raise _JwksUnavailableError("JWKS redirects are not accepted")


class _BoundedJwksKeyProvider:
    __slots__ = (
        "_cache_expires_at",
        "_cache_ttl_seconds",
        "_fetcher",
        "_jwks_url",
        "_keys",
        "_lock",
        "_max_response_bytes",
        "_monotonic",
        "_timeout_seconds",
    )

    def __init__(
        self,
        *,
        jwks_url: str,
        timeout_seconds: float,
        cache_ttl_seconds: float,
        max_response_bytes: int,
        fetcher: JwksFetcher,
        monotonic: MonotonicClock,
    ) -> None:
        self._jwks_url = jwks_url
        self._timeout_seconds = timeout_seconds
        self._cache_ttl_seconds = cache_ttl_seconds
        self._max_response_bytes = max_response_bytes
        self._fetcher = fetcher
        self._monotonic = monotonic
        self._keys: Mapping[str, PyJWK] = MappingProxyType({})
        self._cache_expires_at = -math.inf
        self._lock = threading.Lock()

    def get(self, key_id: str) -> PyJWK:
        """Return a current cached key or perform one fail-closed refresh."""

        with self._lock:
            current = self._monotonic()
            cached = self._keys.get(key_id)
            if cached is not None and current < self._cache_expires_at:
                return cached
            try:
                body = self._fetcher(
                    self._jwks_url,
                    self._timeout_seconds,
                    self._max_response_bytes,
                )
                refreshed = _parse_jwks(body, max_response_bytes=self._max_response_bytes)
            except _InvalidJwksError:
                raise
            except Exception as exc:
                raise _JwksUnavailableError("JWKS could not be refreshed") from exc
            self._keys = refreshed
            self._cache_expires_at = current + self._cache_ttl_seconds
            selected = refreshed.get(key_id)
            if selected is None:
                raise _UnknownKeyError("the token key is unknown")
            return selected


class OidcBearerAuthenticator:
    """Verify a bounded signed JWT and only then reuse ``OidcPrincipalMapper``."""

    __slots__ = (
        "_algorithms",
        "_expected_authorized_party",
        "_key_provider",
        "_mapper",
        "_max_token_bytes",
    )

    def __init__(
        self,
        *,
        mapper: OidcPrincipalMapper,
        jwks_url: str,
        algorithms: tuple[str, ...],
        expected_authorized_party: str | None = None,
        timeout_seconds: float = 5.0,
        cache_ttl_seconds: float = 300.0,
        max_token_bytes: int = MAX_BEARER_TOKEN_BYTES,
        max_jwks_response_bytes: int = MAX_JWKS_RESPONSE_BYTES,
        allow_insecure_loopback: bool = False,
        jwks_fetcher: JwksFetcher | None = None,
        monotonic: MonotonicClock = time.monotonic,
    ) -> None:
        _validate_jwks_url(
            jwks_url,
            allow_insecure_loopback=allow_insecure_loopback,
        )
        if (
            not algorithms
            or len(algorithms) > 8
            or len(set(algorithms)) != len(algorithms)
            or any(
                not isinstance(algorithm, str)
                or algorithm != algorithm.strip()
                or algorithm not in _ASYMMETRIC_ALGORITHMS
                for algorithm in algorithms
            )
        ):
            raise ValueError("algorithms must be a non-empty fixed asymmetric allowlist")
        if not 0 < timeout_seconds <= MAX_HTTP_TIMEOUT_SECONDS:
            raise ValueError("timeout_seconds is outside the supported bound")
        if not 0 < cache_ttl_seconds <= MAX_CACHE_TTL_SECONDS:
            raise ValueError("cache_ttl_seconds is outside the supported bound")
        if not 1 <= max_token_bytes <= MAX_BEARER_TOKEN_BYTES:
            raise ValueError("max_token_bytes is outside the supported bound")
        if not 1 <= max_jwks_response_bytes <= MAX_JWKS_RESPONSE_BYTES:
            raise ValueError("max_jwks_response_bytes is outside the supported bound")
        authorized_party = (
            mapper.expected_audience
            if expected_authorized_party is None
            else expected_authorized_party
        )
        if (
            not isinstance(authorized_party, str)
            or not authorized_party.strip()
            or authorized_party != authorized_party.strip()
            or len(authorized_party) > 256
        ):
            raise ValueError("expected_authorized_party must not be blank")
        self._mapper = mapper
        self._algorithms = algorithms
        self._expected_authorized_party = authorized_party
        self._max_token_bytes = max_token_bytes
        self._key_provider = _BoundedJwksKeyProvider(
            jwks_url=jwks_url,
            timeout_seconds=timeout_seconds,
            cache_ttl_seconds=cache_ttl_seconds,
            max_response_bytes=max_jwks_response_bytes,
            fetcher=jwks_fetcher or _fetch_jwks,
            monotonic=monotonic,
        )

    def authenticate(
        self,
        bearer_token: str,
        now: datetime,
    ) -> AuthenticatedPrincipal:
        """Authenticate without retaining the token or returning unverified claims."""

        token = self._validate_token_shape(bearer_token)
        try:
            header = jwt.get_unverified_header(token)
        except (jwt.PyJWTError, TypeError, ValueError):
            self._raise_invalid_token()
        algorithm = header.get("alg")
        key_id = header.get("kid")
        if (
            not isinstance(algorithm, str)
            or algorithm not in self._algorithms
            or not isinstance(key_id, str)
            or not key_id
            or key_id != key_id.strip()
            or len(key_id) > MAX_KEY_ID_LENGTH
            or "jku" in header
            or "x5u" in header
            or "crit" in header
        ):
            self._raise_invalid_token()
        try:
            signing_key = self._key_provider.get(key_id)
        except _UnknownKeyError:
            self._raise_invalid_token()
        except (_JwksUnavailableError, _InvalidJwksError):
            raise AuthenticationBoundaryError("authentication_unavailable") from None
        if signing_key.algorithm_name != algorithm:
            self._raise_invalid_token()
        try:
            claims = jwt.decode(
                token,
                key=signing_key,
                algorithms=list(self._algorithms),
                audience=self._mapper.expected_audience,
                issuer=self._mapper.expected_issuer,
                options={
                    "require": ["iss", "sub", "aud", "azp", "iat", "nbf", "exp"],
                    "verify_signature": True,
                    "verify_iss": True,
                    "verify_aud": True,
                    "verify_iat": False,
                    "verify_nbf": False,
                    "verify_exp": False,
                },
            )
            if claims.get("azp") != self._expected_authorized_party:
                self._raise_invalid_token()
            return self._mapper.map_claims(claims, now=now)
        except AuthenticationBoundaryError:
            raise
        except (jwt.PyJWTError, OidcClaimError, TypeError, ValueError):
            self._raise_invalid_token()

    def __repr__(self) -> str:
        """Expose policy shape only, never cached keys, claims, or credentials."""

        algorithms = ",".join(self._algorithms)
        return f"OidcBearerAuthenticator(algorithms={algorithms!r}, configured=True)"

    def _validate_token_shape(self, bearer_token: str) -> str:
        if not isinstance(bearer_token, str):
            self._raise_invalid_token()
        if (
            not bearer_token
            or bearer_token != bearer_token.strip()
            or any(ord(character) < 0x21 for character in bearer_token)
            or len(bearer_token.encode("utf-8")) > self._max_token_bytes
            or bearer_token.count(".") != 2
        ):
            self._raise_invalid_token()
        return bearer_token

    @staticmethod
    def _raise_invalid_token() -> NoReturn:
        raise AuthenticationBoundaryError("invalid_bearer_token") from None


def _validate_jwks_url(jwks_url: str, *, allow_insecure_loopback: bool) -> None:
    try:
        parsed = urlsplit(jwks_url)
        _port = parsed.port
    except ValueError as exc:
        raise ValueError("jwks_url is invalid") from exc
    if (
        not jwks_url
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("jwks_url is invalid")
    if parsed.scheme == "https":
        return
    if (
        allow_insecure_loopback
        and parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    ):
        return
    raise ValueError("jwks_url must use HTTPS")


def _fetch_jwks(url: str, timeout_seconds: float, max_response_bytes: int) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        method="GET",
    )
    opener = urllib.request.build_opener(_RejectRedirects())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            if response.geturl() != url or response.getcode() != 200:
                raise _JwksUnavailableError("JWKS response was not accepted")
            raw_content_length = response.headers.get("Content-Length")
            if raw_content_length is not None:
                try:
                    content_length = int(raw_content_length)
                except ValueError as exc:
                    raise _InvalidJwksError("JWKS content length is invalid") from exc
                if content_length < 0 or content_length > max_response_bytes:
                    raise _InvalidJwksError("JWKS response exceeds its bound")
            body = bytes(response.read(max_response_bytes + 1))
    except _InvalidJwksError:
        raise
    except _JwksUnavailableError:
        raise
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()
        raise _JwksUnavailableError("JWKS request failed") from exc
    if len(body) > max_response_bytes:
        raise _InvalidJwksError("JWKS response exceeds its bound")
    return body


def _parse_jwks(body: bytes, *, max_response_bytes: int) -> Mapping[str, PyJWK]:
    if not body or len(body) > max_response_bytes:
        raise _InvalidJwksError("JWKS response has an invalid size")
    try:
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey) as exc:
        raise _InvalidJwksError("JWKS response is malformed") from exc
    if not isinstance(payload, dict) or set(payload) != {"keys"}:
        raise _InvalidJwksError("JWKS response has an invalid shape")
    raw_keys = payload["keys"]
    if not isinstance(raw_keys, list) or not 1 <= len(raw_keys) <= MAX_JWKS_KEYS:
        raise _InvalidJwksError("JWKS key count is outside the supported bound")
    parsed_keys: dict[str, PyJWK] = {}
    try:
        for raw_key in raw_keys:
            if not isinstance(raw_key, dict) or _PRIVATE_JWK_FIELDS.intersection(raw_key):
                raise _InvalidJwksError("JWKS contains an invalid public key")
            if raw_key.get("use") not in {None, "sig"}:
                continue
            key_operations = raw_key.get("key_ops")
            if key_operations is not None and (
                not isinstance(key_operations, list)
                or any(not isinstance(operation, str) for operation in key_operations)
                or "verify" not in key_operations
            ):
                continue
            key_id = raw_key.get("kid")
            if (
                not isinstance(key_id, str)
                or not key_id
                or key_id != key_id.strip()
                or len(key_id) > MAX_KEY_ID_LENGTH
                or key_id in parsed_keys
            ):
                raise _InvalidJwksError("JWKS contains an invalid or duplicate key ID")
            if raw_key.get("kty") == "oct":
                raise _InvalidJwksError("JWKS symmetric signing keys are not accepted")
            parsed_keys[key_id] = PyJWK.from_dict(raw_key)
    except jwt.PyJWTError as exc:
        raise _InvalidJwksError("JWKS contains an invalid public key") from exc
    if not parsed_keys:
        raise _InvalidJwksError("JWKS contains no usable signing key")
    return MappingProxyType(parsed_keys)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result
