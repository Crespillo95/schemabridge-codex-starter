"""Exact-version remote connector secrets authenticated by projected workload identity."""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from schemabridge.adapters.connectors.local_secrets import (
    MAX_CONNECTOR_SECRET_BYTES,
    _parse_document,
    _validate_postgres_dsn,
)
from schemabridge.application.ports.connector_secrets import (
    ConnectorSecretErrorCode,
    ConnectorSecretResolutionError,
    OpaqueConnectorSecretRef,
    ResolvedPostgresSecret,
    connector_secret_error,
)
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    SourceConnectorKind,
    SourceDialect,
)

MAX_REMOTE_SECRET_RESPONSE_BYTES = 64 * 1_024
MAX_WORKLOAD_TOKEN_BYTES = 16 * 1_024
MAX_REMOTE_SECRET_TIMEOUT_SECONDS = 15.0

_INERT_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
_VAULT_TOKEN = re.compile(r"^[A-Za-z0-9._~-]{16,4096}$")
_JWT_TEXT = re.compile(r"^[A-Za-z0-9_-]+$")
_VAULT_RESPONSE_KEYS = frozenset(
    {
        "auth",
        "data",
        "lease_duration",
        "lease_id",
        "mount_type",
        "renewable",
        "request_id",
        "warnings",
        "wrap_info",
    }
)
_VAULT_AUTH_KEYS = frozenset(
    {
        "accessor",
        "client_token",
        "entity_id",
        "lease_duration",
        "metadata",
        "mfa_requirement",
        "num_uses",
        "orphan",
        "policies",
        "renewable",
        "token_policies",
        "token_type",
    }
)
_VAULT_KV_ENVELOPE_KEYS = frozenset({"data", "metadata"})
_VAULT_KV_METADATA_KEYS = frozenset(
    {
        "created_time",
        "custom_metadata",
        "deletion_time",
        "destroyed",
        "version",
    }
)


class ConnectorSecretCapability(StrEnum):
    """Closed secret namespaces that may never substitute for one another."""

    PREFLIGHT = "preflight"
    EXECUTION = "execution"
    CATALOG = "catalog"
    PROFILE = "profile"
    REGISTRY = "registry"


class WorkloadIdentitySource(Protocol):
    """Return one currently valid projected identity token."""

    def read(self) -> str:
        """Return a transient JWT or raise a sanitized connector-secret error."""


class RemoteSecretTransport(Protocol):
    """Bounded HTTPS transport injected for deterministic tests."""

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        ca_bundle: Path,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> bytes:
        """Return a bounded JSON body without redirects."""


@dataclass(frozen=True, slots=True)
class ProjectedServiceAccountIdentity:
    """Read and preflight one Kubernetes-style audience-bound projected JWT."""

    token_file: Path = field(repr=False)
    mount_root: Path = field(repr=False)
    audience: str
    max_token_bytes: int = MAX_WORKLOAD_TOKEN_BYTES
    now_epoch: Callable[[], int] = field(
        default=lambda: int(time.time()),
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.token_file, Path)
            or not self.token_file.is_absolute()
            or not isinstance(self.mount_root, Path)
            or not self.mount_root.is_absolute()
            or self.token_file == self.mount_root
            or not self.token_file.is_relative_to(self.mount_root)
            or _INERT_NAME.fullmatch(self.audience) is None
            or type(self.max_token_bytes) is not int
            or not 256 <= self.max_token_bytes <= MAX_WORKLOAD_TOKEN_BYTES
        ):
            raise ValueError("projected workload identity configuration is invalid")

    def read(self) -> str:
        """Read one stable projected file and validate non-authoritative JWT bounds."""

        descriptor: int | None = None
        try:
            root = self.mount_root.resolve(strict=True)
            if not root.is_dir():
                raise ValueError
            resolved_before = self.token_file.resolve(strict=True)
            if not resolved_before.is_relative_to(root):
                raise ValueError
            metadata_before = resolved_before.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata_before.st_mode)
                or metadata_before.st_uid not in {0, os.geteuid()}
                or metadata_before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or not 1 <= metadata_before.st_size <= self.max_token_bytes
            ):
                raise ValueError
            flags = os.O_RDONLY | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(resolved_before, flags)
            opened = os.fstat(descriptor)
            if _identity(metadata_before) != _identity(opened):
                raise ValueError
            raw = _read_exact(descriptor, opened.st_size)
            closed_over = os.fstat(descriptor)
            resolved_after = self.token_file.resolve(strict=True)
            if (
                resolved_after != resolved_before
                or _identity(opened) != _identity(closed_over)
                or len(raw) != closed_over.st_size
            ):
                raise ValueError
            token = raw.decode("ascii")
            _validate_projected_jwt(
                token,
                audience=self.audience,
                now_epoch=self.now_epoch(),
            )
            return token
        except ConnectorSecretResolutionError:
            raise
        except (OSError, UnicodeError, ValueError):
            raise connector_secret_error(ConnectorSecretErrorCode.IDENTITY_REJECTED) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)


@dataclass(frozen=True, slots=True)
class UrllibRemoteSecretTransport:
    """Minimal HTTPS-only transport with no proxy or redirect behavior."""

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        ca_bundle: Path,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> bytes:
        try:
            if method not in {"GET", "POST"}:
                raise ValueError
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname is None
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError
            context = ssl.create_default_context(cafile=str(ca_bundle))
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            opener = build_opener(
                ProxyHandler({}),
                HTTPSHandler(context=context),
                _RejectRedirects(),
            )
            request = Request(
                url=url,
                data=body,
                method=method,
                headers=dict(headers),
            )
            with opener.open(request, timeout=timeout_seconds) as response:
                if response.status != 200:
                    raise ValueError
                content_type = response.headers.get_content_type()
                if content_type != "application/json":
                    raise ValueError
                raw = bytes(response.read(max_response_bytes + 1))
                if not raw or len(raw) > max_response_bytes or response.read(1):
                    raise ValueError
                return raw
        except (HTTPError, OSError, TimeoutError, URLError, ValueError):
            raise connector_secret_error(ConnectorSecretErrorCode.UNAVAILABLE) from None


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: Any,
        msg: Any,
        headers: Any,
        newurl: Any,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


@dataclass(frozen=True, slots=True)
class VaultKvV2ConnectorSecretResolver:
    """Resolve exact KV-v2 versions with one short-lived JWT login per operation."""

    server: str = field(repr=False)
    role: str = field(repr=False)
    kv_mount: str = field(repr=False)
    capability: ConnectorSecretCapability
    identity: WorkloadIdentitySource = field(repr=False)
    ca_bundle: Path = field(repr=False)
    auth_mount: str = field(default="jwt", repr=False)
    timeout_seconds: float = 5.0
    max_response_bytes: int = MAX_REMOTE_SECRET_RESPONSE_BYTES
    transport: RemoteSecretTransport = field(
        default_factory=UrllibRemoteSecretTransport,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        parsed = urlsplit(self.server)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or _INERT_NAME.fullmatch(self.role) is None
            or _INERT_NAME.fullmatch(self.kv_mount) is None
            or _INERT_NAME.fullmatch(self.auth_mount) is None
            or not isinstance(self.capability, ConnectorSecretCapability)
            or not isinstance(self.ca_bundle, Path)
            or not self.ca_bundle.is_absolute()
            or type(self.timeout_seconds) not in {int, float}
            or not 0.1 <= float(self.timeout_seconds) <= MAX_REMOTE_SECRET_TIMEOUT_SECONDS
            or type(self.max_response_bytes) is not int
            or not MAX_CONNECTOR_SECRET_BYTES
            <= self.max_response_bytes
            <= (MAX_REMOTE_SECRET_RESPONSE_BYTES)
        ):
            raise ValueError("remote connector secret resolver configuration is invalid")
        try:
            metadata = self.ca_bundle.stat(follow_symlinks=False)
        except OSError:
            raise ValueError("remote connector secret trust bundle is unavailable") from None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or metadata.st_size < 1
        ):
            raise ValueError("remote connector secret trust bundle is unsafe")
        object.__setattr__(self, "server", self.server.rstrip("/"))

    def resolve(
        self,
        reference: OpaqueConnectorSecretRef,
        target: GovernedExecutionTarget,
    ) -> ResolvedPostgresSecret:
        """Resolve the binding's exact provider version; no latest read is possible."""

        if (
            not isinstance(reference, OpaqueConnectorSecretRef)
            or not isinstance(target, GovernedExecutionTarget)
            or target.connector_kind is not SourceConnectorKind.POSTGRESQL
            or target.dialect is not SourceDialect.POSTGRESQL
        ):
            raise connector_secret_error(ConnectorSecretErrorCode.TARGET_MISMATCH)
        return self.resolve_postgres_route(
            reference,
            dialect=target.dialect,
            expected_reader=target.expected_reader,
        )

    def resolve_postgres_route(
        self,
        reference: OpaqueConnectorSecretRef,
        *,
        dialect: SourceDialect,
        expected_reader: str,
    ) -> ResolvedPostgresSecret:
        """Authenticate, fetch one exact version, validate, and discard provider material."""

        if (
            not isinstance(reference, OpaqueConnectorSecretRef)
            or dialect is not SourceDialect.POSTGRESQL
            or not isinstance(expected_reader, str)
            or not expected_reader
            or reference.provider_secret_version is None
        ):
            raise connector_secret_error(ConnectorSecretErrorCode.TARGET_MISMATCH)
        try:
            payload = self.read_document(
                reference,
                version=reference.provider_secret_version,
            )
            document = _parse_document(
                json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            document_dialect = document["dialect"]
            document_reader = document["expected_reader"]
            dsn = document["dsn"]
            if (
                document_dialect != dialect.value
                or document_reader != expected_reader
                or not isinstance(dsn, str)
            ):
                raise connector_secret_error(ConnectorSecretErrorCode.TARGET_MISMATCH)
            _validate_postgres_dsn(
                dsn,
                expected_reader=expected_reader,
                require_tls=True,
            )
            return ResolvedPostgresSecret(
                dsn=dsn,
                expected_reader=expected_reader,
                dialect=dialect,
            )
        except ConnectorSecretResolutionError:
            raise
        except Exception:
            raise connector_secret_error(ConnectorSecretErrorCode.UNAVAILABLE) from None

    def read_document(
        self,
        reference: OpaqueConnectorSecretRef,
        *,
        version: int,
    ) -> dict[str, object]:
        """Return one transient exact-version document to a capability-specific adapter."""

        if (
            not isinstance(reference, OpaqueConnectorSecretRef)
            or type(version) is not int
            or version < 1
            or (
                reference.provider_secret_version is not None
                and reference.provider_secret_version != version
            )
        ):
            raise connector_secret_error(ConnectorSecretErrorCode.TARGET_MISMATCH)
        workload_token = self.identity.read()
        client_token = self._login(workload_token)
        return self._read_version(
            reference,
            client_token=client_token,
            version=version,
        )

    def _login(self, workload_token: str) -> str:
        body = json.dumps(
            {"jwt": workload_token, "role": self.role},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        raw = self.transport.request(
            method="POST",
            url=f"{self.server}/v1/auth/{self.auth_mount}/login",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            body=body,
            ca_bundle=self.ca_bundle,
            timeout_seconds=float(self.timeout_seconds),
            max_response_bytes=self.max_response_bytes,
        )
        document = _json_object(raw)
        if not _has_closed_shape(
            document,
            required=frozenset({"auth"}),
            allowed=_VAULT_RESPONSE_KEYS,
        ):
            raise connector_secret_error(ConnectorSecretErrorCode.IDENTITY_REJECTED)
        auth = document["auth"]
        if not isinstance(auth, dict) or not _has_closed_shape(
            auth,
            required=frozenset({"client_token", "lease_duration", "renewable"}),
            allowed=_VAULT_AUTH_KEYS,
        ):
            raise connector_secret_error(ConnectorSecretErrorCode.IDENTITY_REJECTED)
        client_token = auth.get("client_token")
        lease_duration = auth.get("lease_duration")
        renewable = auth.get("renewable")
        if (
            not isinstance(client_token, str)
            or _VAULT_TOKEN.fullmatch(client_token) is None
            or type(lease_duration) is not int
            or not 1 <= lease_duration <= 3_600
            or renewable is not False
        ):
            raise connector_secret_error(ConnectorSecretErrorCode.IDENTITY_REJECTED)
        return client_token

    def _read_version(
        self,
        reference: OpaqueConnectorSecretRef,
        *,
        client_token: str,
        version: int,
    ) -> dict[str, object]:
        query = urlencode({"version": str(version)})
        url = (
            f"{self.server}/v1/{self.kv_mount}/data/schemabridge/"
            f"{self.capability.value}/{reference.digest}?{query}"
        )
        raw = self.transport.request(
            method="GET",
            url=url,
            headers={
                "Accept": "application/json",
                "X-Vault-Token": client_token,
            },
            body=None,
            ca_bundle=self.ca_bundle,
            timeout_seconds=float(self.timeout_seconds),
            max_response_bytes=self.max_response_bytes,
        )
        outer = _json_object(raw)
        if not _has_closed_shape(
            outer,
            required=frozenset({"data"}),
            allowed=_VAULT_RESPONSE_KEYS,
        ):
            raise connector_secret_error(ConnectorSecretErrorCode.INVALID)
        envelope = outer["data"]
        if not isinstance(envelope, dict) or not _has_closed_shape(
            envelope,
            required=_VAULT_KV_ENVELOPE_KEYS,
            allowed=_VAULT_KV_ENVELOPE_KEYS,
        ):
            raise connector_secret_error(ConnectorSecretErrorCode.INVALID)
        payload = envelope.get("data")
        metadata = envelope.get("metadata")
        if (
            not isinstance(payload, dict)
            or not isinstance(metadata, dict)
            or not _has_closed_shape(
                metadata,
                required=frozenset({"deletion_time", "destroyed", "version"}),
                allowed=_VAULT_KV_METADATA_KEYS,
            )
        ):
            raise connector_secret_error(ConnectorSecretErrorCode.INVALID)
        observed_version = metadata.get("version")
        destroyed = metadata.get("destroyed")
        deletion_time = metadata.get("deletion_time")
        if (
            type(observed_version) is not int
            or observed_version != version
            or destroyed is not False
            or deletion_time not in {"", None}
        ):
            raise connector_secret_error(ConnectorSecretErrorCode.VERSION_UNAVAILABLE)
        return payload


def _json_object(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise connector_secret_error(ConnectorSecretErrorCode.INVALID) from None
    if not isinstance(value, dict):
        raise connector_secret_error(ConnectorSecretErrorCode.INVALID)
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ValueError("duplicate remote secret key")
        result[key] = value
    return result


def _has_closed_shape(
    value: dict[str, object],
    *,
    required: frozenset[str],
    allowed: frozenset[str],
) -> bool:
    keys = set(value)
    return required <= keys <= allowed


def _read_exact(descriptor: int, size: int) -> bytes:
    remaining = size
    chunks: list[bytes] = []
    while remaining:
        chunk = os.read(descriptor, min(remaining, 16 * 1_024))
        if not chunk:
            raise ValueError
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise ValueError
    return b"".join(chunks)


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _validate_projected_jwt(
    token: str,
    *,
    audience: str,
    now_epoch: int,
) -> None:
    if (
        not token
        or token.strip() != token
        or len(token.encode("ascii")) > MAX_WORKLOAD_TOKEN_BYTES
        or type(now_epoch) is not int
    ):
        raise ValueError
    parts = token.split(".")
    if len(parts) != 3 or any(_JWT_TEXT.fullmatch(part) is None for part in parts):
        raise ValueError
    payload = _decode_jwt_object(parts[1])
    audiences = payload.get("aud")
    if isinstance(audiences, str):
        accepted_audiences = (audiences,)
    elif isinstance(audiences, list) and all(isinstance(item, str) for item in audiences):
        accepted_audiences = tuple(audiences)
    else:
        raise ValueError
    expires = payload.get("exp")
    not_before = payload.get("nbf", 0)
    issued_at = payload.get("iat")
    subject = payload.get("sub")
    issuer = payload.get("iss")
    if (
        audience not in accepted_audiences
        or type(expires) is not int
        or expires <= now_epoch + 5
        or expires > now_epoch + 3_600
        or type(not_before) is not int
        or not_before > now_epoch + 5
        or type(issued_at) is not int
        or issued_at > now_epoch + 5
        or issued_at < now_epoch - 3_600
        or not isinstance(subject, str)
        or not 1 <= len(subject) <= 512
        or not isinstance(issuer, str)
        or urlsplit(issuer).scheme != "https"
    ):
        raise ValueError


def _decode_jwt_object(value: str) -> dict[str, object]:
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(value + padding)
        decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, json.JSONDecodeError):
        raise ValueError from None
    if not isinstance(decoded, dict):
        raise ValueError
    return decoded


__all__ = [
    "MAX_REMOTE_SECRET_RESPONSE_BYTES",
    "MAX_REMOTE_SECRET_TIMEOUT_SECONDS",
    "MAX_WORKLOAD_TOKEN_BYTES",
    "ConnectorSecretCapability",
    "ProjectedServiceAccountIdentity",
    "RemoteSecretTransport",
    "UrllibRemoteSecretTransport",
    "VaultKvV2ConnectorSecretResolver",
    "WorkloadIdentitySource",
]
