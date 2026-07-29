"""Operation-scoped DataHub registry reads backed by exact-version remote secrets."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from schemabridge.adapters.connectors.remote_secrets import ConnectorSecretCapability
from schemabridge.adapters.datahub.recipe_inventory import (
    DataHubQueryRecipeInventory,
    DataHubQueryRecipeInventoryConfig,
)
from schemabridge.adapters.semantic_registry.datahub import (
    DataHubGovernedSemanticRegistry,
    DataHubRegistryReadConfig,
)
from schemabridge.adapters.semantic_registry.datahub_control import (
    DataHubRegistryVersionReader,
)
from schemabridge.application.ports.connector_secrets import (
    ConnectorSecretResolutionError,
    OpaqueConnectorSecretRef,
)
from schemabridge.application.ports.planning import (
    GovernedSemanticRegistryPort,
    PlanningPortError,
    PlanningPortErrorCode,
)
from schemabridge.application.ports.registry_control import RegistryVersionReadPort
from schemabridge.application.ports.semantic_dependency_sources import (
    CurrentQueryRecipeDocument,
    SemanticDependencySourcePage,
)
from schemabridge.domain.registry_control import GovernedRegistryVersion
from schemabridge.domain.semantic_registry import (
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
)

_MAX_SERVER_BYTES = 2 * 1024
_MAX_TOKEN_BYTES = 16 * 1024
_DOCUMENT_KEYS = frozenset({"format_version", "kind", "server", "token"})


class ExactVersionDocumentReader(Protocol):
    """Read one capability-bound exact remote document."""

    @property
    def capability(self) -> ConnectorSecretCapability:
        """Return the closed provider namespace."""

    def read_document(
        self,
        reference: OpaqueConnectorSecretRef,
        *,
        version: int,
    ) -> dict[str, object]:
        """Return one transient provider document."""


class DataHubRegistryCredentialResolver(Protocol):
    """Resolve DataHub reader material for one bounded registry operation."""

    def resolve(self) -> DataHubRegistryReadConfig:
        """Return one transient validated reader configuration."""


@dataclass(frozen=True, slots=True)
class VaultKvV2DataHubRegistryCredentialResolver:
    """Resolve a closed DataHub reader document at one immutable provider version."""

    backend: ExactVersionDocumentReader = field(repr=False)
    reference: OpaqueConnectorSecretRef = field(repr=False)
    version: int

    def __post_init__(self) -> None:
        if (
            self.backend.capability is not ConnectorSecretCapability.REGISTRY
            or not isinstance(self.reference, OpaqueConnectorSecretRef)
            or type(self.version) is not int
            or self.version < 1
        ):
            raise ValueError("remote semantic registry resolver configuration is invalid")

    def resolve(self) -> DataHubRegistryReadConfig:
        """Authenticate, read, validate, and return only operation-scoped material."""

        try:
            document = self.backend.read_document(
                self.reference,
                version=self.version,
            )
            if (
                set(document) != _DOCUMENT_KEYS
                or type(document["format_version"]) is not int
                or document["format_version"] != 1
                or document["kind"] != "datahub_registry_reader"
                or not isinstance(document["server"], str)
                or not isinstance(document["token"], str)
            ):
                raise ValueError
            server = _validated_server(document["server"])
            token = _validated_token(document["token"])
            return DataHubRegistryReadConfig(server=server, token=token)
        except PlanningPortError:
            raise
        except ConnectorSecretResolutionError as error:
            raise PlanningPortError(
                PlanningPortErrorCode.CONTEXT_UNAVAILABLE,
                "DataHub registry reader credential is unavailable",
            ) from error
        except (KeyError, TypeError, ValueError) as error:
            raise PlanningPortError(
                PlanningPortErrorCode.CONTEXT_INVALID,
                "DataHub registry reader credential is invalid",
            ) from error


@dataclass(slots=True)
class RemoteDataHubRegistryVersionReader(RegistryVersionReadPort):
    """Resolve and discard the bearer around one exact registry-version read."""

    credentials: DataHubRegistryCredentialResolver = field(repr=False)

    def load_version(
        self,
        scope: SemanticRegistryScope,
        version: int,
    ) -> GovernedRegistryVersion:
        config = self.credentials.resolve()
        return DataHubRegistryVersionReader(config=config).load_version(scope, version)


@dataclass(slots=True)
class RemoteDataHubGovernedSemanticRegistry(GovernedSemanticRegistryPort):
    """Resolve and discard the bearer around one governed registry load."""

    credentials: DataHubRegistryCredentialResolver = field(repr=False)
    _scope: SemanticRegistryScope
    version: int
    loads: int = 0

    @property
    def scope(self) -> SemanticRegistryScope:
        return self._scope

    def load(self) -> ScopedSemanticRegistrySnapshot:
        self.loads += 1
        config = self.credentials.resolve()
        return DataHubGovernedSemanticRegistry(
            config=config,
            _scope=self.scope,
            version=self.version,
        ).load()


@dataclass(slots=True)
class RemoteDataHubQueryRecipeInventory:
    """Resolve and discard the bearer around one complete bounded recipe scan."""

    credentials: DataHubRegistryCredentialResolver = field(repr=False)
    timeout_seconds: float
    max_response_bytes: int

    def pages(
        self,
        scope: SemanticRegistryScope,
        *,
        page_size: int,
    ) -> Iterator[SemanticDependencySourcePage[CurrentQueryRecipeDocument]]:
        config = self.credentials.resolve()
        inventory = DataHubQueryRecipeInventory(
            DataHubQueryRecipeInventoryConfig(
                server=config.server,
                token=config.token,
                timeout_seconds=self.timeout_seconds,
                max_response_bytes=self.max_response_bytes,
            )
        )
        yield from inventory.pages(scope, page_size=page_size)


def _validated_server(value: str) -> str:
    parsed = urlsplit(value)
    if (
        not value
        or value.strip() != value
        or len(value.encode("utf-8")) > _MAX_SERVER_BYTES
        or parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError
    port = parsed.port
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    authority = f"{host}:{port}" if port is not None else host
    return f"https://{authority}"


def _validated_token(value: str) -> str:
    if (
        not value
        or value.strip() != value
        or len(value.encode("utf-8")) > _MAX_TOKEN_BYTES
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError
    return value


__all__ = [
    "DataHubRegistryCredentialResolver",
    "RemoteDataHubGovernedSemanticRegistry",
    "RemoteDataHubQueryRecipeInventory",
    "RemoteDataHubRegistryVersionReader",
    "VaultKvV2DataHubRegistryCredentialResolver",
]
