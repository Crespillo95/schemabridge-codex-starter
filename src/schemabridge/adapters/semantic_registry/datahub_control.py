"""Exact immutable DataHub registry reader for the PostgreSQL control plane."""

from __future__ import annotations

from dataclasses import dataclass

from schemabridge.adapters.semantic_registry.datahub import (
    _READER_TOLERATED_PLATFORM_PRIVILEGES,
    DataHubHttpRegistryReadClient,
    DataHubRegistryReadClient,
    DataHubRegistryReadConfig,
    _parse_registry_document,
)
from schemabridge.application.ports.planning import (
    PlanningPortError,
    PlanningPortErrorCode,
)
from schemabridge.application.ports.registry_control import (
    RegistryControlError,
    RegistryControlErrorCode,
)
from schemabridge.domain.registry_control import (
    GovernedRegistryVersion,
    RegistryVersionTrust,
)
from schemabridge.domain.semantic_registry import (
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
    datahub_registry_document_urn,
    validate_registry_publication_approval,
)


@dataclass(slots=True)
class DataHubRegistryVersionReader:
    """Load only the requested version and explicitly classify historical trust."""

    config: DataHubRegistryReadConfig
    client: DataHubRegistryReadClient | None = None

    def load_version(
        self,
        scope: SemanticRegistryScope,
        version: int,
    ) -> GovernedRegistryVersion:
        if isinstance(version, bool) or version < 1:
            raise RegistryControlError(
                RegistryControlErrorCode.VERSION_INVALID,
                "immutable registry version is invalid",
            )
        target = datahub_registry_document_urn(scope, version)
        client = self.client or DataHubHttpRegistryReadClient(
            self.config.server,
            self.config.token,
        )
        try:
            identity = client.identity(target)
            forbidden_platform_privileges = (
                identity.granted_platform_mutation_privileges
                - _READER_TOLERATED_PLATFORM_PRIVILEGES
            )
            if forbidden_platform_privileges or identity.granted_target_edit_privileges:
                raise RegistryControlError(
                    RegistryControlErrorCode.VERSION_INVALID,
                    "DataHub registry reader has mutation privileges",
                )
            document = client.get_document(target)
            if document is None:
                raise RegistryControlError(
                    RegistryControlErrorCode.VERSION_UNAVAILABLE,
                    "immutable DataHub registry version was not found",
                )
            registry, approval, _ = _parse_registry_document(
                document,
                scope=scope,
                version=version,
            )
            try:
                validate_registry_publication_approval(registry, approval)
                trust = RegistryVersionTrust.STRICT
            except ValueError:
                # The parser accepts only the one fingerprint-pinned M22 historical shim.
                trust = RegistryVersionTrust.LEGACY_READ_ONLY
            return GovernedRegistryVersion(
                snapshot=ScopedSemanticRegistrySnapshot(
                    scope=scope,
                    registry=registry,
                ),
                publication_approval_id=approval.id,
                trust=trust,
            )
        except RegistryControlError:
            raise
        except PlanningPortError as error:
            code = (
                RegistryControlErrorCode.VERSION_UNAVAILABLE
                if error.code is PlanningPortErrorCode.REGISTRY_NOT_FOUND
                else RegistryControlErrorCode.VERSION_INVALID
            )
            raise RegistryControlError(
                code,
                "immutable DataHub registry version failed validation",
            ) from error
        except Exception as error:
            raise RegistryControlError(
                RegistryControlErrorCode.VERSION_UNAVAILABLE,
                "immutable DataHub registry version could not be read",
            ) from error
