#!/usr/bin/env python3
"""Loopback-only synthetic OIDC provider for manual Streamlit browser acceptance.

This is test infrastructure, not an identity provider for deployed environments.
The configured synthetic identity is selected only when this process starts.
Browser requests cannot choose a subject, tenant, or group mapping.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final, cast
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from joserfc import jwt
from joserfc.jwk import RSAKey

_MAX_BODY_BYTES: Final = 16_384
_MAX_QUERY_FIELDS: Final = 32
_MAX_OUTSTANDING_CODES: Final = 256
_CODE_TTL_SECONDS: Final = 120
_TOKEN_TTL_SECONDS: Final = 600
_PKCE_PATTERN: Final = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_CHALLENGE_PATTERN: Final = re.compile(r"^[A-Za-z0-9_-]{43}$")


@dataclass(frozen=True, slots=True)
class SyntheticIdentity:
    """Synthetic claims selected by the operator when the provider starts."""

    subject: str
    tenant: str | None
    groups: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_claim_text(self.subject, "subject")
        if self.tenant is not None:
            _validate_claim_text(self.tenant, "tenant")
        if len(self.groups) != len(set(self.groups)):
            raise ValueError("groups must not contain duplicates")
        for group in self.groups:
            _validate_claim_text(group, "group")


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Validated process configuration with a redacted secret representation."""

    host: str
    port: int
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    identity: SyntheticIdentity

    def __post_init__(self) -> None:
        try:
            address = ipaddress.ip_address(self.host)
        except ValueError as error:
            raise ValueError("host must be a literal IPv4 loopback address") from error
        if address.version != 4 or not address.is_loopback:
            raise ValueError("host must be a literal IPv4 loopback address")
        if not 1 <= self.port <= 65_535:
            raise ValueError("port must be between 1 and 65535")
        _validate_claim_text(self.client_id, "client_id")
        if (
            len(self.client_secret) < 16
            or len(self.client_secret) > 512
            or ":" in self.client_secret
            or _has_control_characters(self.client_secret)
        ):
            raise ValueError("client_secret must be 16-512 characters without colons or controls")
        _validate_redirect_uri(self.redirect_uri)

    @property
    def issuer(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Small HTTP result shared by the real handler and socket-free unit tests."""

    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes = b""

    def header(self, name: str) -> str | None:
        target = name.casefold()
        return next(
            (value for key, value in self.headers if key.casefold() == target),
            None,
        )


@dataclass(frozen=True, slots=True)
class _AuthorizationCode:
    client_id: str
    redirect_uri: str
    nonce: str
    code_challenge: str
    issued_at: float


class SyntheticOidcProvider:
    """Minimal authorization-code OIDC provider compatible with Streamlit/Authlib."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._clock = clock
        self._codes: dict[str, _AuthorizationCode] = {}
        self._lock = threading.Lock()
        self._signing_key = RSAKey.generate_key(
            2048,
            private=True,
            parameters={
                "kid": "schemabridge-synthetic-browser-key",
                "use": "sig",
                "alg": "RS256",
            },
        )

    def ready_message(self) -> str:
        """Return a safe operator status line without credentials or identity claims."""

        return (
            f"Synthetic OIDC test provider ready at {self.config.issuer}; "
            "loopback browser acceptance only."
        )

    def route(
        self,
        method: str,
        target: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> HttpResponse:
        """Route one bounded request without logging its query, body, or result."""

        if len(body) > _MAX_BODY_BYTES:
            return _json_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "invalid_request")
        try:
            parsed = urlsplit(target)
            query = _parse_parameters(parsed.query)
        except ValueError:
            return _json_error(HTTPStatus.BAD_REQUEST, "invalid_request")
        normalized_method = method.upper()
        normalized_headers = {key.casefold(): value for key, value in (headers or {}).items()}

        if parsed.path == "/health" and normalized_method == "GET":
            return HttpResponse(
                HTTPStatus.OK,
                (("Content-Type", "text/plain; charset=utf-8"),),
                b"ok\n",
            )
        if parsed.path == "/.well-known/openid-configuration" and normalized_method == "GET":
            return self._discovery()
        if parsed.path == "/jwks" and normalized_method == "GET":
            return self._jwks()
        if parsed.path == "/authorize" and normalized_method == "GET":
            return self._authorize(query)
        if parsed.path == "/token" and normalized_method == "POST":
            return self._token(normalized_headers, body)
        if parsed.path == "/logout" and normalized_method == "GET":
            return self._logout(query)
        return _json_error(HTTPStatus.NOT_FOUND, "not_found")

    def _discovery(self) -> HttpResponse:
        issuer = self.config.issuer
        return _json_response(
            HTTPStatus.OK,
            {
                "issuer": issuer,
                "authorization_endpoint": f"{issuer}/authorize",
                "token_endpoint": f"{issuer}/token",
                "jwks_uri": f"{issuer}/jwks",
                "end_session_endpoint": f"{issuer}/logout",
                "response_types_supported": ["code"],
                "response_modes_supported": ["query"],
                "grant_types_supported": ["authorization_code"],
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic"],
                "code_challenge_methods_supported": ["S256"],
                "scopes_supported": ["openid", "profile"],
                "claims_supported": [
                    "iss",
                    "sub",
                    "aud",
                    "azp",
                    "iat",
                    "nbf",
                    "exp",
                    "auth_time",
                    "nonce",
                    "tenant_id",
                    "groups",
                ],
            },
        )

    def _jwks(self) -> HttpResponse:
        return _json_response(
            HTTPStatus.OK,
            {"keys": [self._signing_key.as_dict(private=False)]},
        )

    def _authorize(self, query: Mapping[str, tuple[str, ...]]) -> HttpResponse:
        try:
            response_type = _one(query, "response_type")
            client_id = _one(query, "client_id")
            redirect_uri = _one(query, "redirect_uri")
            scope = _one(query, "scope")
            state = _bounded_opaque(_one(query, "state"), "state")
            nonce = _bounded_opaque(_one(query, "nonce"), "nonce")
            challenge = _one(query, "code_challenge")
            challenge_method = _one(query, "code_challenge_method")
        except ValueError:
            return _json_error(HTTPStatus.BAD_REQUEST, "invalid_request")

        if response_type != "code" or not hmac.compare_digest(client_id, self.config.client_id):
            return _json_error(HTTPStatus.BAD_REQUEST, "unauthorized_client")
        if redirect_uri != self.config.redirect_uri:
            return _json_error(HTTPStatus.BAD_REQUEST, "invalid_request")
        if "openid" not in scope.split():
            return _json_error(HTTPStatus.BAD_REQUEST, "invalid_scope")
        if challenge_method != "S256" or _CHALLENGE_PATTERN.fullmatch(challenge) is None:
            return _json_error(HTTPStatus.BAD_REQUEST, "invalid_request")

        now = self._clock()
        with self._lock:
            self._prune_codes(now)
            if len(self._codes) >= _MAX_OUTSTANDING_CODES:
                return _json_error(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "temporarily_unavailable",
                )
            code = secrets.token_urlsafe(32)
            self._codes[code] = _AuthorizationCode(
                client_id=client_id,
                redirect_uri=redirect_uri,
                nonce=nonce,
                code_challenge=challenge,
                issued_at=now,
            )
        return _redirect(_append_query(redirect_uri, {"code": code, "state": state}))

    def _token(self, headers: Mapping[str, str], body: bytes) -> HttpResponse:
        if headers.get("content-type", "").split(";", 1)[0].strip().casefold() != (
            "application/x-www-form-urlencoded"
        ):
            return _json_error(HTTPStatus.BAD_REQUEST, "invalid_request")
        try:
            client_id, client_secret = _basic_credentials(headers.get("authorization"))
        except ValueError:
            return _invalid_client()
        if not (
            hmac.compare_digest(client_id, self.config.client_id)
            and hmac.compare_digest(client_secret, self.config.client_secret)
        ):
            return _invalid_client()
        try:
            form = _parse_parameters(body.decode("ascii"))
            grant_type = _one(form, "grant_type")
            code = _bounded_opaque(_one(form, "code"), "code")
            redirect_uri = _one(form, "redirect_uri")
            verifier = _one(form, "code_verifier")
        except (UnicodeDecodeError, ValueError):
            return _json_error(HTTPStatus.BAD_REQUEST, "invalid_request")
        supplied_form_client_id = form.get("client_id")
        if supplied_form_client_id is not None and supplied_form_client_id != (client_id,):
            return _invalid_client()
        if grant_type != "authorization_code":
            return _json_error(HTTPStatus.BAD_REQUEST, "unsupported_grant_type")

        now = self._clock()
        with self._lock:
            record = self._codes.pop(code, None)
        if (
            record is None
            or now - record.issued_at > _CODE_TTL_SECONDS
            or record.client_id != client_id
            or record.redirect_uri != redirect_uri
            or not _valid_pkce(verifier, record.code_challenge)
        ):
            return _json_error(HTTPStatus.BAD_REQUEST, "invalid_grant")
        return self._issue_tokens(record, int(now))

    def _issue_tokens(self, record: _AuthorizationCode, issued_at: int) -> HttpResponse:
        identity = self.config.identity
        access_token_claims: dict[str, object] = {
            "iss": self.config.issuer,
            "sub": identity.subject,
            "aud": self.config.client_id,
            "azp": self.config.client_id,
            "iat": issued_at,
            "nbf": issued_at - 1,
            "exp": issued_at + _TOKEN_TTL_SECONDS,
            "groups": list(identity.groups),
        }
        if identity.tenant is not None:
            access_token_claims["tenant_id"] = identity.tenant
        access_token = jwt.encode(
            {
                "alg": "RS256",
                "kid": self._signing_key.kid,
            },
            access_token_claims,
            self._signing_key,
        )
        id_token = jwt.encode(
            {
                "alg": "RS256",
                "kid": self._signing_key.kid,
            },
            {
                **access_token_claims,
                "auth_time": issued_at,
                "nonce": record.nonce,
            },
            self._signing_key,
        )
        return _json_response(
            HTTPStatus.OK,
            {
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": _TOKEN_TTL_SECONDS,
                "scope": "openid profile",
                "id_token": id_token,
            },
        )

    def _logout(self, query: Mapping[str, tuple[str, ...]]) -> HttpResponse:
        try:
            client_id = _one(query, "client_id")
            redirect_uri = _one(query, "post_logout_redirect_uri")
        except ValueError:
            return _json_error(HTTPStatus.BAD_REQUEST, "invalid_request")
        if (
            not hmac.compare_digest(client_id, self.config.client_id)
            or redirect_uri != self.config.redirect_uri
        ):
            return _json_error(HTTPStatus.BAD_REQUEST, "invalid_request")
        return _redirect(redirect_uri)

    def _prune_codes(self, now: float) -> None:
        expired = [
            code
            for code, record in self._codes.items()
            if now - record.issued_at > _CODE_TTL_SECONDS
        ]
        for code in expired:
            del self._codes[code]


class _LoopbackHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    oidc_provider: SyntheticOidcProvider


class _OidcRequestHandler(BaseHTTPRequestHandler):
    """Translate stdlib HTTP requests without emitting sensitive access logs."""

    protocol_version = "HTTP/1.1"
    server_version = "SchemaBridgeSyntheticOIDC/1"
    sys_version = ""

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def log_message(self, _format: str, *args: object) -> None:
        """Suppress paths, queries, authorization codes, and token payloads."""

    def _dispatch(self, method: str) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._write(_json_error(HTTPStatus.BAD_REQUEST, "invalid_request"))
            return
        if content_length < 0 or content_length > _MAX_BODY_BYTES:
            self.close_connection = True
            self._write(_json_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "invalid_request"))
            return
        body = self.rfile.read(content_length) if content_length else b""
        provider = cast(_LoopbackHttpServer, self.server).oidc_provider
        response = provider.route(
            method,
            self.path,
            headers={key: value for key, value in self.headers.items()},
            body=body,
        )
        self._write(response)

    def _write(self, response: HttpResponse) -> None:
        self.send_response(response.status)
        for name, value in response.headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        if response.body:
            self.wfile.write(response.body)


def parse_arguments(argv: Sequence[str] | None = None) -> ProviderConfig:
    parser = argparse.ArgumentParser(
        description="Run a loopback-only synthetic OIDC provider for browser acceptance."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--client-id", default="schemabridge-browser-test")
    parser.add_argument("--client-secret", required=True)
    parser.add_argument(
        "--redirect-uri",
        default="http://127.0.0.1:8501/oauth2callback",
    )
    parser.add_argument("--subject", default="synthetic-analyst-publisher-a")
    tenant = parser.add_mutually_exclusive_group()
    tenant.add_argument("--tenant", default="tenant-a")
    tenant.add_argument("--omit-tenant", action="store_true")
    parser.add_argument(
        "--group",
        action="append",
        dest="groups",
        help="Synthetic group claim; repeat for multiple groups.",
    )
    args = parser.parse_args(argv)
    groups = tuple(args.groups or ("analysts", "publishers"))
    try:
        return ProviderConfig(
            host=args.host,
            port=args.port,
            client_id=args.client_id,
            client_secret=args.client_secret,
            redirect_uri=args.redirect_uri,
            identity=SyntheticIdentity(
                subject=args.subject,
                tenant=None if args.omit_tenant else args.tenant,
                groups=groups,
            ),
        )
    except ValueError as error:
        parser.error(str(error))
    raise AssertionError("argparse exits on invalid provider configuration")


def main(argv: Sequence[str] | None = None) -> int:
    config = parse_arguments(argv)
    provider = SyntheticOidcProvider(config)
    server = _LoopbackHttpServer((config.host, config.port), _OidcRequestHandler)
    server.oidc_provider = provider
    print(provider.ready_message(), flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _validate_claim_text(value: str, label: str) -> None:
    if not value or value != value.strip() or len(value) > 256 or _has_control_characters(value):
        raise ValueError(f"{label} must be a trimmed synthetic value without controls")


def _has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _validate_redirect_uri(value: str) -> None:
    parsed = urlsplit(value)
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError as error:
        raise ValueError("redirect_uri must use a literal loopback host") from error
    if (
        _has_control_characters(value)
        or parsed.scheme != "http"
        or address.version != 4
        or not address.is_loopback
        or port is None
        or not 1 <= port <= 65_535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith("/oauth2callback")
    ):
        raise ValueError("redirect_uri must be an HTTP loopback /oauth2callback URL")


def _parse_parameters(encoded: str) -> dict[str, tuple[str, ...]]:
    values = parse_qs(
        encoded,
        keep_blank_values=True,
        strict_parsing=False,
        max_num_fields=_MAX_QUERY_FIELDS,
    )
    return {key: tuple(items) for key, items in values.items()}


def _one(parameters: Mapping[str, tuple[str, ...]], key: str) -> str:
    values = parameters.get(key)
    if values is None or len(values) != 1:
        raise ValueError(f"{key} must occur exactly once")
    return values[0]


def _bounded_opaque(value: str, label: str) -> str:
    if not value or len(value) > 512 or _has_control_characters(value):
        raise ValueError(f"{label} is invalid")
    return value


def _basic_credentials(header: str | None) -> tuple[str, str]:
    if header is None:
        raise ValueError("missing authorization")
    scheme, separator, encoded = header.partition(" ")
    if not separator or scheme.casefold() != "basic" or not encoded:
        raise ValueError("invalid authorization")
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as error:
        raise ValueError("invalid authorization") from error
    client_id, separator, client_secret = decoded.partition(":")
    if not separator or not client_id or not client_secret:
        raise ValueError("invalid authorization")
    return client_id, client_secret


def _valid_pkce(verifier: str, expected_challenge: str) -> bool:
    if _PKCE_PATTERN.fullmatch(verifier) is None:
        return False
    actual = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return hmac.compare_digest(actual, expected_challenge)


def _append_query(uri: str, parameters: Mapping[str, str]) -> str:
    parsed = urlsplit(uri)
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(parameters),
            "",
        )
    )


def _json_response(status: HTTPStatus, value: Mapping[str, object]) -> HttpResponse:
    body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return HttpResponse(
        status,
        (
            ("Content-Type", "application/json"),
            ("Cache-Control", "no-store"),
            ("Pragma", "no-cache"),
        ),
        body,
    )


def _json_error(status: HTTPStatus, error: str) -> HttpResponse:
    return _json_response(status, {"error": error})


def _invalid_client() -> HttpResponse:
    response = _json_error(HTTPStatus.UNAUTHORIZED, "invalid_client")
    return HttpResponse(
        response.status,
        (*response.headers, ("WWW-Authenticate", 'Basic realm="synthetic-oidc"')),
        response.body,
    )


def _redirect(location: str) -> HttpResponse:
    return HttpResponse(
        HTTPStatus.FOUND,
        (
            ("Location", location),
            ("Cache-Control", "no-store"),
            ("Pragma", "no-cache"),
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
