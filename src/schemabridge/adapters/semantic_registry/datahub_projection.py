"""Repairable DataHub projection of the authoritative active-registry pointer."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import cast

from pydantic import ValidationError

from schemabridge.adapters.semantic_registry.datahub import (
    _ALLOWED_WRITER_PRIVILEGES,
    DataHubRegistryDocument,
    DataHubRegistryDocumentWrite,
    DataHubRegistryWriteClient,
    DataHubRegistryWriteConfig,
    DataHubSdkRegistryWriteClient,
)
from schemabridge.application.ports.registry_control import (
    RegistryControlError,
    RegistryControlErrorCode,
)
from schemabridge.domain.registry_control import (
    RegistryProjectionState,
    RegistryReconciliationApproval,
    RegistryReconciliationConfirmation,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope

logger = logging.getLogger(__name__)

_PROJECTION_FORMAT_VERSION = "1"
_MAX_POINTER_BYTES = 256 * 1024
_MAX_APPROVAL_BYTES = 64 * 1024
_PROJECTION_PROPERTIES = frozenset(
    {
        "schemabridge.activeProjectionFormatVersion",
        "schemabridge.registryId",
        "schemabridge.registryCatalogScope",
        "schemabridge.registryWorkspaceDigest",
        "schemabridge.registryActivationGeneration",
        "schemabridge.registryVersion",
        "schemabridge.registryFingerprint",
        "schemabridge.registryTarget",
        "schemabridge.registryTransitionId",
        "schemabridge.registryProjectionFingerprint",
        "schemabridge.registryActivePointer",
        "schemabridge.registryReconciliationApproval",
    }
)


@dataclass(slots=True)
class DataHubRegistryProjectionAdapter:
    """Bounded projector; PostgreSQL remains the sole planning authority."""

    config: DataHubRegistryWriteConfig
    client: DataHubRegistryWriteClient | None = None

    def read(self, scope: SemanticRegistryScope) -> RegistryProjectionState | None:
        client = self.client or DataHubSdkRegistryWriteClient(
            self.config.server,
            self.config.token,
        )
        target = datahub_active_registry_projection_urn(scope)
        try:
            document = client.get_document(target)
            return None if document is None else _parse_projection(document, scope)
        except RegistryControlError:
            raise
        except Exception as error:
            raise RegistryControlError(
                RegistryControlErrorCode.PROJECTION_UNAVAILABLE,
                "DataHub active-registry projection could not be read",
            ) from error

    def project(
        self,
        desired: RegistryProjectionState,
        approval: RegistryReconciliationApproval,
    ) -> RegistryProjectionState:
        _validate_projection_approval(desired, approval)
        scope = desired.pointer.scope
        target = datahub_active_registry_projection_urn(scope)
        client = self.client or DataHubSdkRegistryWriteClient(
            self.config.server,
            self.config.token,
        )
        try:
            identity = client.identity(target)
            if (
                identity.actor_urn != self.config.actor_urn
                or "manageDocuments" not in identity.granted_platform_mutation_privileges
                or not identity.granted_platform_mutation_privileges <= _ALLOWED_WRITER_PRIVILEGES
            ):
                raise RegistryControlError(
                    RegistryControlErrorCode.PROJECTION_UNAVAILABLE,
                    "DataHub projection writer identity or privileges are not bounded",
                )
            existing_document = client.get_document(target)
            existing = (
                None if existing_document is None else _parse_projection(existing_document, scope)
            )
            if existing is not None:
                if existing == desired:
                    return existing
                if existing.pointer.generation >= desired.pointer.generation:
                    raise RegistryControlError(
                        RegistryControlErrorCode.RECONCILIATION_CONFLICT,
                        "DataHub projection is newer or conflicting at this generation",
                    )
            document = _projection_document(desired, approval)
            logger.info(
                "datahub_registry_projection_write target=%s generation=%s",
                target,
                desired.pointer.generation,
            )
            client.upsert_document(document)
            observed_document = client.get_document(target)
            if observed_document is None:
                raise ValueError("DataHub active-registry projection was absent after upsert")
            observed = _parse_projection(observed_document, scope)
            if observed != desired:
                raise ValueError("DataHub active-registry projection failed exact read-back")
            return observed
        except RegistryControlError:
            raise
        except Exception as error:
            raise RegistryControlError(
                RegistryControlErrorCode.PROJECTION_UNAVAILABLE,
                "DataHub active-registry projection write failed",
            ) from error


def datahub_active_registry_projection_id(scope: SemanticRegistryScope) -> str:
    workspace_digest = hashlib.sha256(scope.workspace_id.encode()).hexdigest()[:24]
    catalog_digest = hashlib.sha256(scope.catalog_scope.encode()).hexdigest()[:12]
    return (
        "schemabridge-semantic-registry-active-"
        f"{scope.registry_id}-{catalog_digest}-{workspace_digest}"
    )


def datahub_active_registry_projection_urn(scope: SemanticRegistryScope) -> str:
    return f"urn:li:document:{datahub_active_registry_projection_id(scope)}"


def _projection_document(
    desired: RegistryProjectionState,
    approval: RegistryReconciliationApproval,
) -> DataHubRegistryDocumentWrite:
    pointer = desired.pointer
    scope = pointer.scope
    pointer_json = pointer.model_dump_json()
    approval_json = approval.model_dump_json()
    if len(pointer_json.encode()) > _MAX_POINTER_BYTES:
        raise ValueError("active-registry pointer exceeds its storage bound")
    if len(approval_json.encode()) > _MAX_APPROVAL_BYTES:
        raise ValueError("reconciliation approval exceeds its storage bound")
    properties = {
        "schemabridge.activeProjectionFormatVersion": _PROJECTION_FORMAT_VERSION,
        "schemabridge.registryId": scope.registry_id,
        "schemabridge.registryCatalogScope": scope.catalog_scope,
        "schemabridge.registryWorkspaceDigest": hashlib.sha256(
            scope.workspace_id.encode()
        ).hexdigest()[:24],
        "schemabridge.registryActivationGeneration": str(pointer.generation),
        "schemabridge.registryVersion": str(pointer.registry_version),
        "schemabridge.registryFingerprint": pointer.registry_fingerprint,
        "schemabridge.registryTarget": pointer.registry_target,
        "schemabridge.registryTransitionId": pointer.transition_id,
        "schemabridge.registryProjectionFingerprint": desired.projection_fingerprint,
        "schemabridge.registryActivePointer": pointer_json,
        "schemabridge.registryReconciliationApproval": approval_json,
    }
    return DataHubRegistryDocumentWrite(
        document_id=datahub_active_registry_projection_id(scope),
        title=(
            f"SchemaBridge active semantic registry: {scope.registry_id} "
            f"(generation {pointer.generation})"
        ),
        text=(
            "Repairable projection of the SchemaBridge PostgreSQL control-plane pointer. "
            "Runtime planning does not trust this document as an authority."
        ),
        custom_properties=properties,
        related_asset_urns=(),
    )


def _parse_projection(
    document: DataHubRegistryDocument,
    scope: SemanticRegistryScope,
) -> RegistryProjectionState:
    target = datahub_active_registry_projection_urn(scope)
    if document.urn != target or document.removed or document.related_asset_urns:
        raise RegistryControlError(
            RegistryControlErrorCode.RECONCILIATION_CONFLICT,
            "DataHub active-registry projection identity is invalid",
        )
    properties = document.custom_properties
    if set(properties) != _PROJECTION_PROPERTIES:
        raise RegistryControlError(
            RegistryControlErrorCode.RECONCILIATION_CONFLICT,
            "DataHub active-registry projection properties are incomplete",
        )
    raw_pointer = _bounded_property(
        properties,
        "schemabridge.registryActivePointer",
        _MAX_POINTER_BYTES,
    )
    raw_approval = _bounded_property(
        properties,
        "schemabridge.registryReconciliationApproval",
        _MAX_APPROVAL_BYTES,
    )
    try:
        pointer_payload = _unique_json(raw_pointer)
        pointer = RegistryProjectionState.model_validate(
            {
                "pointer": pointer_payload,
                "projection_fingerprint": properties["schemabridge.registryProjectionFingerprint"],
            }
        )
        approval = RegistryReconciliationApproval.model_validate(_unique_json(raw_approval))
    except (ValidationError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RegistryControlError(
            RegistryControlErrorCode.RECONCILIATION_CONFLICT,
            "DataHub active-registry projection payload is invalid",
        ) from error
    observed = pointer.pointer
    expected = {
        "schemabridge.activeProjectionFormatVersion": _PROJECTION_FORMAT_VERSION,
        "schemabridge.registryId": scope.registry_id,
        "schemabridge.registryCatalogScope": scope.catalog_scope,
        "schemabridge.registryWorkspaceDigest": hashlib.sha256(
            scope.workspace_id.encode()
        ).hexdigest()[:24],
        "schemabridge.registryActivationGeneration": str(observed.generation),
        "schemabridge.registryVersion": str(observed.registry_version),
        "schemabridge.registryFingerprint": observed.registry_fingerprint,
        "schemabridge.registryTarget": observed.registry_target,
        "schemabridge.registryTransitionId": observed.transition_id,
        "schemabridge.registryProjectionFingerprint": pointer.projection_fingerprint,
        "schemabridge.registryActivePointer": raw_pointer,
        "schemabridge.registryReconciliationApproval": raw_approval,
    }
    if (
        observed.scope != scope
        or approval.scope != observed.scope
        or approval.generation != observed.generation
        or approval.registry_fingerprint != observed.registry_fingerprint
        or approval.confirmation
        is not RegistryReconciliationConfirmation.REPAIR_ACTIVE_REGISTRY_PROJECTION
        or dict(properties) != expected
        or document.title
        != (
            f"SchemaBridge active semantic registry: {scope.registry_id} "
            f"(generation {observed.generation})"
        )
        or document.text
        != (
            "Repairable projection of the SchemaBridge PostgreSQL control-plane pointer. "
            "Runtime planning does not trust this document as an authority."
        )
    ):
        raise RegistryControlError(
            RegistryControlErrorCode.RECONCILIATION_CONFLICT,
            "DataHub active-registry projection does not match its typed pointer",
        )
    return pointer


def _validate_projection_approval(
    desired: RegistryProjectionState,
    approval: RegistryReconciliationApproval,
) -> None:
    try:
        validated = RegistryReconciliationApproval.model_validate(
            approval.model_dump(mode="python", warnings=False)
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise RegistryControlError(
            RegistryControlErrorCode.APPROVAL_MISMATCH,
            "explicit typed reconciliation approval is required",
        ) from error
    pointer = desired.pointer
    if (
        validated != approval
        or approval.scope != pointer.scope
        or approval.generation != pointer.generation
        or approval.registry_fingerprint != pointer.registry_fingerprint
        or approval.confirmation
        is not RegistryReconciliationConfirmation.REPAIR_ACTIVE_REGISTRY_PROJECTION
    ):
        raise RegistryControlError(
            RegistryControlErrorCode.APPROVAL_MISMATCH,
            "reconciliation approval does not match the desired projection",
        )


def _bounded_property(
    properties: object,
    name: str,
    maximum: int,
) -> str:
    mapping = cast(dict[str, str], properties)
    value = mapping.get(name)
    if not isinstance(value, str) or not 1 <= len(value.encode()) <= maximum:
        raise ValueError("DataHub projection property is missing or oversized")
    return value


def _unique_json(payload: str) -> object:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError("DataHub projection JSON repeats a key")
            result[key] = value
        return result

    return json.loads(payload, object_pairs_hook=pairs)
