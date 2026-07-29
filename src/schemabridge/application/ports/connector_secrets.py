"""Private connector-secret values and the application-facing resolver port."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from schemabridge.domain.connectors import (
    MAX_ROUTE_REVISION,
    GovernedExecutionTarget,
    SourceDialect,
)

_BINDING_REF = re.compile(r"^[a-z][a-z0-9._:-]{2,199}$")


class ConnectorSecretErrorCode(StrEnum):
    """Sanitized failures that never identify a provider, path, binding, or secret."""

    UNAVAILABLE = "connector_secret_unavailable"
    UNSAFE = "connector_secret_unsafe"
    INVALID = "connector_secret_invalid"
    TARGET_MISMATCH = "connector_secret_target_mismatch"
    IDENTITY_REJECTED = "connector_secret_identity_rejected"
    VERSION_UNAVAILABLE = "connector_secret_version_unavailable"


class ConnectorSecretResolutionError(RuntimeError):
    """A stable safe failure from any private connector-secret adapter."""

    def __init__(self, code: ConnectorSecretErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class OpaqueConnectorSecretRef:
    """Private route handle and its optional exact immutable provider version."""

    value: str = field(repr=False)
    provider_secret_version: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.value, str)
            or _BINDING_REF.fullmatch(self.value) is None
            or "://" in self.value
            or "@" in self.value
            or (
                self.provider_secret_version is not None
                and (
                    type(self.provider_secret_version) is not int
                    or not 1 <= self.provider_secret_version <= MAX_ROUTE_REVISION
                )
            )
        ):
            raise connector_secret_error(ConnectorSecretErrorCode.UNAVAILABLE)

    @property
    def digest(self) -> str:
        """Return the only provider/file path component derived from the private binding."""

        return hashlib.sha256(self.value.encode("utf-8")).hexdigest()

    @property
    def filename(self) -> str:
        """Return only the one-way content-addressed local filename."""

        return f"{self.digest}.json"


@dataclass(frozen=True, slots=True)
class ResolvedPostgresSecret:
    """Adapter-private PostgreSQL material consumed immediately by a connector."""

    dsn: str = field(repr=False)
    expected_reader: str = field(repr=False)
    dialect: SourceDialect


class ConnectorSecretResolver(Protocol):
    """Resolve one exact private secret version for a governed target."""

    def resolve(
        self,
        reference: OpaqueConnectorSecretRef,
        target: GovernedExecutionTarget,
    ) -> ResolvedPostgresSecret:
        """Return transient material or one sanitized failure."""

    def resolve_postgres_route(
        self,
        reference: OpaqueConnectorSecretRef,
        *,
        dialect: SourceDialect,
        expected_reader: str,
    ) -> ResolvedPostgresSecret:
        """Resolve a lease-bound PostgreSQL route without a public target."""


def connector_secret_error(
    code: ConnectorSecretErrorCode,
) -> ConnectorSecretResolutionError:
    """Build one closed, provider-neutral error."""

    messages = {
        ConnectorSecretErrorCode.UNAVAILABLE: "connector secret is unavailable",
        ConnectorSecretErrorCode.UNSAFE: "connector secret input is unsafe",
        ConnectorSecretErrorCode.INVALID: "connector secret payload is invalid",
        ConnectorSecretErrorCode.TARGET_MISMATCH: (
            "connector secret does not match the governed target"
        ),
        ConnectorSecretErrorCode.IDENTITY_REJECTED: "connector secret identity was rejected",
        ConnectorSecretErrorCode.VERSION_UNAVAILABLE: ("connector secret version is unavailable"),
    }
    return ConnectorSecretResolutionError(code, messages[code])


__all__ = [
    "ConnectorSecretErrorCode",
    "ConnectorSecretResolutionError",
    "ConnectorSecretResolver",
    "OpaqueConnectorSecretRef",
    "ResolvedPostgresSecret",
    "connector_secret_error",
]
