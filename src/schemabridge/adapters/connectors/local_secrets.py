"""Owner-only local connector secrets for bounded integration evidence."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict

from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    SourceConnectorKind,
    SourceDialect,
)

MAX_CONNECTOR_SECRET_BYTES = 16 * 1_024
MAX_CONNECTOR_DSN_BYTES = 8 * 1_024

_BINDING_REF = re.compile(r"^[a-z][a-z0-9._:-]{2,199}$")
_NO_RAW_WHITESPACE_OR_CONTROL = re.compile(r"^[^\x00-\x20\x7f]+$")
_DOCUMENT_KEYS = frozenset({"format_version", "dialect", "expected_reader", "dsn"})
_FORBIDDEN_QUERY_IDENTITY_KEYS = frozenset(
    {
        "dbname",
        "host",
        "hostaddr",
        "passfile",
        "password",
        "port",
        "service",
        "user",
    }
)


class ConnectorSecretErrorCode(StrEnum):
    """Sanitized private-adapter failures with no filesystem or DSN details."""

    UNAVAILABLE = "connector_secret_unavailable"
    UNSAFE = "connector_secret_unsafe"
    INVALID = "connector_secret_invalid"
    TARGET_MISMATCH = "connector_secret_target_mismatch"


class ConnectorSecretResolutionError(RuntimeError):
    """A safe failure from the private local secret boundary."""

    def __init__(self, code: ConnectorSecretErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class OpaqueConnectorSecretRef:
    """Private route handle whose value is never represented or used as a path."""

    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.value, str)
            or _BINDING_REF.fullmatch(self.value) is None
            or "://" in self.value
            or "@" in self.value
        ):
            raise _secret_error(ConnectorSecretErrorCode.UNAVAILABLE)

    @property
    def filename(self) -> str:
        """Return only the one-way content-addressed filename."""

        digest = hashlib.sha256(self.value.encode("utf-8")).hexdigest()
        return f"{digest}.json"


@dataclass(frozen=True, slots=True)
class ResolvedPostgresSecret:
    """Adapter-private PostgreSQL material consumed immediately by a connector."""

    dsn: str = field(repr=False)
    expected_reader: str = field(repr=False)
    dialect: SourceDialect


@dataclass(frozen=True, slots=True)
class OwnerOnlyConnectorSecretResolver:
    """Resolve one opaque reference through a stable owner-only directory."""

    secret_directory: Path = field(repr=False)
    max_document_bytes: int = MAX_CONNECTOR_SECRET_BYTES

    def __post_init__(self) -> None:
        if (
            not isinstance(self.secret_directory, Path)
            or not self.secret_directory.is_absolute()
            or ".." in self.secret_directory.parts
            or type(self.max_document_bytes) is not int
            or not 1 <= self.max_document_bytes <= MAX_CONNECTOR_SECRET_BYTES
        ):
            raise ValueError("connector secret resolver configuration is invalid")

    def resolve(
        self,
        reference: OpaqueConnectorSecretRef,
        target: GovernedExecutionTarget,
    ) -> ResolvedPostgresSecret:
        """Resolve an exact target without exposing the binding or secret in failures."""

        if (
            not isinstance(reference, OpaqueConnectorSecretRef)
            or not isinstance(target, GovernedExecutionTarget)
            or target.connector_kind is not SourceConnectorKind.POSTGRESQL
            or target.dialect is not SourceDialect.POSTGRESQL
        ):
            raise _secret_error(ConnectorSecretErrorCode.TARGET_MISMATCH)
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
        """Resolve a lease-validated private route without reconstructing a public target."""

        if (
            not isinstance(reference, OpaqueConnectorSecretRef)
            or dialect is not SourceDialect.POSTGRESQL
            or not isinstance(expected_reader, str)
            or not expected_reader
        ):
            raise _secret_error(ConnectorSecretErrorCode.TARGET_MISMATCH)
        raw = self._read_exact_owner_only_document(reference.filename)
        document = _parse_document(raw)
        document_dialect = document["dialect"]
        document_reader = document["expected_reader"]
        dsn = document["dsn"]
        if (
            document_dialect != dialect.value
            or document_reader != expected_reader
            or not isinstance(dsn, str)
        ):
            raise _secret_error(ConnectorSecretErrorCode.TARGET_MISMATCH)
        _validate_postgres_dsn(dsn, expected_reader=expected_reader)
        return ResolvedPostgresSecret(
            dsn=dsn,
            expected_reader=expected_reader,
            dialect=dialect,
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
                raise _secret_error(ConnectorSecretErrorCode.UNSAFE)
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
                raise _secret_error(ConnectorSecretErrorCode.UNSAFE)
            return raw
        except ConnectorSecretResolutionError:
            raise
        except FileNotFoundError:
            raise _secret_error(ConnectorSecretErrorCode.UNAVAILABLE) from None
        except OSError as error:
            code = (
                ConnectorSecretErrorCode.UNAVAILABLE
                if error.errno in {errno.ENOENT, errno.ENOTDIR}
                else ConnectorSecretErrorCode.UNSAFE
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
            raise _secret_error(ConnectorSecretErrorCode.UNSAFE)
        path_before = path.lstat()
        if (
            not stat.S_ISDIR(path_before.st_mode)
            or stat.S_IMODE(path_before.st_mode) != 0o700
            or path_before.st_uid != os.geteuid()
        ):
            raise _secret_error(ConnectorSecretErrorCode.UNSAFE)
        descriptor = os.open(path, _directory_nofollow_flags())
        opened = os.fstat(descriptor)
        if _directory_identity(path_before) != _directory_identity(opened):
            os.close(descriptor)
            raise _secret_error(ConnectorSecretErrorCode.UNSAFE)
        return descriptor, opened
    except ConnectorSecretResolutionError:
        raise
    except FileNotFoundError:
        raise _secret_error(ConnectorSecretErrorCode.UNAVAILABLE) from None
    except OSError:
        raise _secret_error(ConnectorSecretErrorCode.UNSAFE) from None


def _validate_secret_file_metadata(metadata: os.stat_result, maximum_bytes: int) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
        or not 1 <= metadata.st_size <= maximum_bytes
    ):
        raise _secret_error(ConnectorSecretErrorCode.UNSAFE)


def _read_exact(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 64 * 1_024))
        if not chunk:
            raise _secret_error(ConnectorSecretErrorCode.UNSAFE)
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise _secret_error(ConnectorSecretErrorCode.UNSAFE)
    return b"".join(chunks)


def _parse_document(raw: bytes) -> dict[str, object]:
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise _secret_error(ConnectorSecretErrorCode.INVALID) from None
    if (
        not isinstance(document, dict)
        or set(document) != _DOCUMENT_KEYS
        or type(document["format_version"]) is not int
        or document["format_version"] != 1
        or not isinstance(document["dialect"], str)
        or not isinstance(document["expected_reader"], str)
        or not isinstance(document["dsn"], str)
    ):
        raise _secret_error(ConnectorSecretErrorCode.INVALID)
    return document


def _validate_postgres_dsn(dsn: str, *, expected_reader: str) -> None:
    try:
        if (
            not dsn
            or dsn.strip() != dsn
            or len(dsn.encode("utf-8")) > MAX_CONNECTOR_DSN_BYTES
            or _NO_RAW_WHITESPACE_OR_CONTROL.fullmatch(dsn) is None
        ):
            raise ValueError
        parsed_url = urlsplit(dsn)
        query_pairs = parse_qsl(
            parsed_url.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=32,
        )
        query_keys = tuple(key.casefold() for key, _ in query_pairs)
        if (
            parsed_url.scheme != SourceDialect.POSTGRESQL.value
            or parsed_url.fragment
            or parsed_url.username is None
            or parsed_url.password is None
            or parsed_url.hostname is None
            or "," in parsed_url.hostname
            or parsed_url.path in {"", "/"}
            or parsed_url.path.count("/") != 1
            or len(query_keys) != len(set(query_keys))
            or set(query_keys) & _FORBIDDEN_QUERY_IDENTITY_KEYS
        ):
            raise ValueError
        port = parsed_url.port
        if port is not None and not 1 <= port <= 65_535:
            raise ValueError
        conninfo = conninfo_to_dict(dsn)
        if (
            conninfo.get("user") != expected_reader
            or not conninfo.get("password")
            or not conninfo.get("host")
            or not conninfo.get("dbname")
        ):
            raise ValueError
    except (ProgrammingError, UnicodeError, ValueError):
        raise _secret_error(ConnectorSecretErrorCode.INVALID) from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate connector secret key")
        result[key] = value
    return result


def _read_only_nofollow_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise _secret_error(ConnectorSecretErrorCode.UNSAFE)
    return os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW


def _directory_nofollow_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise _secret_error(ConnectorSecretErrorCode.UNSAFE)
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


def _secret_error(code: ConnectorSecretErrorCode) -> ConnectorSecretResolutionError:
    messages = {
        ConnectorSecretErrorCode.UNAVAILABLE: "connector secret is unavailable",
        ConnectorSecretErrorCode.UNSAFE: "connector secret file is unsafe",
        ConnectorSecretErrorCode.INVALID: "connector secret payload is invalid",
        ConnectorSecretErrorCode.TARGET_MISMATCH: (
            "connector secret does not match the governed target"
        ),
    }
    return ConnectorSecretResolutionError(code, messages[code])
