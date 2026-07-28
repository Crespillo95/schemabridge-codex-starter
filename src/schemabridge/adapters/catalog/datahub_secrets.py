"""Owner-only DataHub catalog secrets resolved from opaque route bindings."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import urllib.parse
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from schemabridge.application.ports.catalog_inventory import (
    ManagedCatalogConnectorRoute,
)
from schemabridge.domain.catalog_inventory import (
    CatalogConnectionKind,
    CatalogConnectionStatus,
)

MAX_DATAHUB_CATALOG_SECRET_BYTES = 32 * 1_024
MAX_DATAHUB_SERVER_BYTES = 2 * 1_024
MAX_DATAHUB_TOKEN_BYTES = 16 * 1_024

_DOCUMENT_KEYS = frozenset({"format_version", "kind", "server", "token", "platform"})
_SAFE_PLATFORM = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")
_NO_RAW_WHITESPACE_OR_CONTROL = re.compile(r"^[^\x00-\x20\x7f]+$")


class DataHubCatalogSecretErrorCode(StrEnum):
    """Sanitized local-secret failures with no binding, path, URL, or token."""

    UNAVAILABLE = "catalog_connector_secret_unavailable"
    UNSAFE = "catalog_connector_secret_unsafe"
    INVALID = "catalog_connector_secret_invalid"
    ROUTE_MISMATCH = "catalog_connector_secret_route_mismatch"


class DataHubCatalogSecretResolutionError(RuntimeError):
    """One safe failure at the private DataHub connector boundary."""

    def __init__(self, code: DataHubCatalogSecretErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ResolvedDataHubCatalogSecret:
    """Transient DataHub material consumed immediately by the catalog adapter."""

    server: str = field(repr=False)
    token: str = field(repr=False)
    platform: str = field(repr=False)


def datahub_catalog_identity_fingerprint(*, server: str, platform: str) -> str:
    """Bind one normalized DataHub origin and platform without exposing either."""

    payload = {
        "fingerprint_version": "m28-datahub-catalog-identity-v1",
        "kind": "datahub_graphql",
        "platform": _validated_platform(platform),
        "server": _validated_server(server),
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class DataHubCatalogSecretResolverPort(Protocol):
    """Resolve one exact managed catalog route without exposing its binding."""

    def resolve(self, route: ManagedCatalogConnectorRoute) -> ResolvedDataHubCatalogSecret:
        """Return transient DataHub material or one sanitized failure."""


@dataclass(frozen=True, slots=True)
class OwnerOnlyDataHubCatalogSecretResolver:
    """Map the SHA-256 of an opaque binding to one strict owner-only document."""

    secret_directory: Path = field(repr=False)
    max_document_bytes: int = MAX_DATAHUB_CATALOG_SECRET_BYTES

    def __post_init__(self) -> None:
        if (
            not isinstance(self.secret_directory, Path)
            or not self.secret_directory.is_absolute()
            or ".." in self.secret_directory.parts
            or type(self.max_document_bytes) is not int
            or not 1 <= self.max_document_bytes <= MAX_DATAHUB_CATALOG_SECRET_BYTES
        ):
            raise ValueError("DataHub catalog secret resolver configuration is invalid")

    def resolve(self, route: ManagedCatalogConnectorRoute) -> ResolvedDataHubCatalogSecret:
        """Resolve only an enabled, exact v9 DataHub route."""

        if (
            not isinstance(route, ManagedCatalogConnectorRoute)
            or route.route.kind is not CatalogConnectionKind.DATAHUB_GRAPHQL
            or route.route.status is not CatalogConnectionStatus.ENABLED
            or route.route.contract_version is None
            or route.route.route_revision is None
            or route.route.target_fingerprint is None
        ):
            raise _secret_error(DataHubCatalogSecretErrorCode.ROUTE_MISMATCH)
        filename = (
            f"{hashlib.sha256(route.credential_binding_ref.encode('utf-8')).hexdigest()}.json"
        )
        raw = self._read_exact_owner_only_document(filename)
        document = _parse_document(raw)
        return ResolvedDataHubCatalogSecret(
            server=_validated_server(document["server"]),
            token=_validated_token(document["token"]),
            platform=_validated_platform(document["platform"]),
        )

    def _read_exact_owner_only_document(self, filename: str) -> bytes:
        directory_descriptor: int | None = None
        file_descriptor: int | None = None
        try:
            directory_descriptor, directory_before = _open_secret_directory(self.secret_directory)
            entry_before = os.stat(
                filename,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            _validate_secret_file_metadata(entry_before, self.max_document_bytes)
            file_descriptor = os.open(
                filename,
                _read_only_nofollow_flags(),
                dir_fd=directory_descriptor,
            )
            file_before = os.fstat(file_descriptor)
            _validate_secret_file_metadata(file_before, self.max_document_bytes)
            if _file_identity(entry_before) != _file_identity(file_before):
                raise _secret_error(DataHubCatalogSecretErrorCode.UNSAFE)
            raw = _read_exact(file_descriptor, file_before.st_size)
            file_after = os.fstat(file_descriptor)
            entry_after = os.stat(
                filename,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            directory_after = os.fstat(directory_descriptor)
            root_after = os.stat(self.secret_directory, follow_symlinks=False)
            if (
                _file_identity(file_before) != _file_identity(file_after)
                or _file_identity(file_after) != _file_identity(entry_after)
                or _directory_identity(directory_before) != _directory_identity(directory_after)
                or _directory_identity(directory_after) != _directory_identity(root_after)
                or len(raw) != file_after.st_size
            ):
                raise _secret_error(DataHubCatalogSecretErrorCode.UNSAFE)
            return raw
        except DataHubCatalogSecretResolutionError:
            raise
        except FileNotFoundError:
            raise _secret_error(DataHubCatalogSecretErrorCode.UNAVAILABLE) from None
        except OSError as error:
            code = (
                DataHubCatalogSecretErrorCode.UNAVAILABLE
                if error.errno in {errno.ENOENT, errno.ENOTDIR}
                else DataHubCatalogSecretErrorCode.UNSAFE
            )
            raise _secret_error(code) from None
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            if directory_descriptor is not None:
                os.close(directory_descriptor)


def _open_secret_directory(path: Path) -> tuple[int, os.stat_result]:
    try:
        if path.resolve(strict=True) != path:
            raise _secret_error(DataHubCatalogSecretErrorCode.UNSAFE)
        path_before = path.lstat()
        if (
            not stat.S_ISDIR(path_before.st_mode)
            or stat.S_IMODE(path_before.st_mode) != 0o700
            or path_before.st_uid != os.geteuid()
        ):
            raise _secret_error(DataHubCatalogSecretErrorCode.UNSAFE)
        descriptor = os.open(path, _directory_nofollow_flags())
        opened = os.fstat(descriptor)
        if _directory_identity(path_before) != _directory_identity(opened):
            os.close(descriptor)
            raise _secret_error(DataHubCatalogSecretErrorCode.UNSAFE)
        return descriptor, opened
    except DataHubCatalogSecretResolutionError:
        raise
    except FileNotFoundError:
        raise _secret_error(DataHubCatalogSecretErrorCode.UNAVAILABLE) from None
    except (OSError, RuntimeError):
        raise _secret_error(DataHubCatalogSecretErrorCode.UNSAFE) from None


def _validate_secret_file_metadata(metadata: os.stat_result, maximum_bytes: int) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
        or not 1 <= metadata.st_size <= maximum_bytes
    ):
        raise _secret_error(DataHubCatalogSecretErrorCode.UNSAFE)


def _read_exact(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 64 * 1_024))
        if not chunk:
            raise _secret_error(DataHubCatalogSecretErrorCode.UNSAFE)
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise _secret_error(DataHubCatalogSecretErrorCode.UNSAFE)
    return b"".join(chunks)


def _parse_document(raw: bytes) -> dict[str, str]:
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise _secret_error(DataHubCatalogSecretErrorCode.INVALID) from None
    if (
        not isinstance(document, dict)
        or set(document) != _DOCUMENT_KEYS
        or type(document["format_version"]) is not int
        or document["format_version"] != 1
        or document["kind"] != CatalogConnectionKind.DATAHUB_GRAPHQL.value
        or not isinstance(document["server"], str)
        or not isinstance(document["token"], str)
        or not isinstance(document["platform"], str)
    ):
        raise _secret_error(DataHubCatalogSecretErrorCode.INVALID)
    return {
        "server": document["server"],
        "token": document["token"],
        "platform": document["platform"],
    }


def _validated_server(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        if (
            not value
            or value.strip() != value
            or len(value.encode("utf-8")) > MAX_DATAHUB_SERVER_BYTES
            or _NO_RAW_WHITESPACE_OR_CONTROL.fullmatch(value) is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or parsed.hostname is None
            or parsed.scheme not in {"http", "https"}
        ):
            raise ValueError
        if parsed.scheme == "http" and parsed.hostname not in {
            "127.0.0.1",
            "::1",
            "localhost",
        }:
            raise ValueError
        port = parsed.port
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        authority = f"{host}:{port}" if port is not None else host
        return f"{parsed.scheme}://{authority}"
    except (UnicodeError, ValueError):
        raise _secret_error(DataHubCatalogSecretErrorCode.INVALID) from None


def _validated_token(value: str) -> str:
    if (
        not value
        or value.strip() != value
        or len(value.encode("utf-8")) > MAX_DATAHUB_TOKEN_BYTES
        or _NO_RAW_WHITESPACE_OR_CONTROL.fullmatch(value) is None
    ):
        raise _secret_error(DataHubCatalogSecretErrorCode.INVALID)
    return value


def _validated_platform(value: str) -> str:
    if _SAFE_PLATFORM.fullmatch(value) is None:
        raise _secret_error(DataHubCatalogSecretErrorCode.INVALID)
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate DataHub catalog secret key")
        result[key] = value
    return result


def _read_only_nofollow_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise _secret_error(DataHubCatalogSecretErrorCode.UNSAFE)
    return os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW


def _directory_nofollow_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise _secret_error(DataHubCatalogSecretErrorCode.UNSAFE)
    return os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
    )


def _secret_error(
    code: DataHubCatalogSecretErrorCode,
) -> DataHubCatalogSecretResolutionError:
    messages = {
        DataHubCatalogSecretErrorCode.UNAVAILABLE: "catalog connector secret is unavailable",
        DataHubCatalogSecretErrorCode.UNSAFE: "catalog connector secret is unsafe",
        DataHubCatalogSecretErrorCode.INVALID: "catalog connector secret is invalid",
        DataHubCatalogSecretErrorCode.ROUTE_MISMATCH: "catalog connector route is unavailable",
    }
    return DataHubCatalogSecretResolutionError(code, messages[code])


__all__ = [
    "DataHubCatalogSecretErrorCode",
    "DataHubCatalogSecretResolutionError",
    "DataHubCatalogSecretResolverPort",
    "OwnerOnlyDataHubCatalogSecretResolver",
    "ResolvedDataHubCatalogSecret",
    "datahub_catalog_identity_fingerprint",
]
