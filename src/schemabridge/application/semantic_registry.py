"""Approval-gated immutable semantic-registry publication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from schemabridge.application.ports.planning import (
    GovernedSemanticRegistryPublicationPort,
    RegistryPublicationError,
    RegistryPublicationErrorCode,
)
from schemabridge.application.ports.publication_audit import (
    PublicationAuditStoreError,
    PublicationAuditStorePort,
)
from schemabridge.domain.publication_audit import (
    PublicationAuditOutcome,
    PublicationFamily,
    PublicationTargetAuditRecord,
    validate_publication_audit_binding,
)
from schemabridge.domain.semantic_registry import (
    GovernedSemanticRegistrySnapshot,
    RegistryPublicationApproval,
    RegistryPublicationConfirmation,
    RegistryPublicationResult,
    SemanticRegistryScope,
    datahub_registry_document_urn,
    registry_publication_approval_id,
    semantic_registry_decision_ids,
    validate_registry_publication_approval,
)


@dataclass(frozen=True, slots=True)
class PrepareGovernedSemanticRegistryPublicationApproval:
    """Create or resume the exact durable approval identity for a registry publication."""

    audit_store: PublicationAuditStorePort

    def execute(
        self,
        registry: GovernedSemanticRegistrySnapshot,
        scope: SemanticRegistryScope,
        *,
        actor: str,
        approved_at: datetime,
        confirmed_fingerprint: str,
        confirmation: RegistryPublicationConfirmation,
    ) -> RegistryPublicationApproval:
        if (
            scope.registry_id != registry.registry_id
            or scope.catalog_scope != registry.catalog_scope
            or confirmed_fingerprint != registry.fingerprint
        ):
            raise RegistryPublicationError(
                RegistryPublicationErrorCode.APPROVAL_MISMATCH,
                "registry publication approval does not match the exact payload",
            )
        approval = RegistryPublicationApproval(
            id=registry_publication_approval_id(registry, scope, actor),
            workspace_id=scope.workspace_id,
            registry_id=registry.registry_id,
            registry_version=registry.version,
            catalog_scope=registry.catalog_scope,
            payload_fingerprint=registry.fingerprint,
            target=datahub_registry_document_urn(scope, registry.version),
            actor=actor,
            approved_at=approved_at,
            decision_ids=semantic_registry_decision_ids(registry),
            confirmation=confirmation,
        )
        try:
            validate_registry_publication_approval(registry, approval)
            existing = self.audit_store.list_for_approval(approval.id)
        except PublicationAuditStoreError as error:
            raise RegistryPublicationError(
                RegistryPublicationErrorCode.AUDIT_UNAVAILABLE,
                "registry publication approval history is unavailable",
            ) from error
        except ValueError as error:
            raise RegistryPublicationError(
                RegistryPublicationErrorCode.APPROVAL_MISMATCH,
                "registry publication approval does not match the exact payload",
            ) from error
        if not existing:
            return approval

        resumed = RegistryPublicationApproval.model_validate(
            {
                **approval.model_dump(mode="python"),
                "approved_at": existing[0].approved_at,
            }
        )
        try:
            validate_registry_publication_approval(registry, resumed)
            _validate_approval_history(existing, registry, resumed)
        except ValueError as error:
            raise RegistryPublicationError(
                RegistryPublicationErrorCode.AUDIT_UNAVAILABLE,
                "registry publication approval history is invalid",
            ) from error
        return resumed


@dataclass(frozen=True, slots=True)
class PublishGovernedSemanticRegistryVersion:
    """Publish one exact immutable DataHub version and durably record its audit."""

    publisher: GovernedSemanticRegistryPublicationPort
    audit_store: PublicationAuditStorePort

    def execute(
        self,
        registry: GovernedSemanticRegistrySnapshot,
        approval: RegistryPublicationApproval,
    ) -> RegistryPublicationResult:
        if not isinstance(approval, RegistryPublicationApproval):
            raise RegistryPublicationError(
                RegistryPublicationErrorCode.APPROVAL_REQUIRED,
                "explicit registry publication approval is required",
            )
        try:
            validate_registry_publication_approval(registry, approval)
        except ValueError as error:
            raise RegistryPublicationError(
                RegistryPublicationErrorCode.APPROVAL_MISMATCH,
                "registry publication approval does not match the exact payload",
            ) from error

        _ensure_approval_reservation(self.audit_store, registry, approval)
        raw_result = self.publisher.publish(registry, approval)
        try:
            result = RegistryPublicationResult.model_validate(raw_result.model_dump(mode="python"))
        except (AttributeError, TypeError, ValueError) as error:
            raise RegistryPublicationError(
                RegistryPublicationErrorCode.INVALID_RESPONSE,
                "registry publisher returned an invalid typed result",
            ) from error
        if (
            result.workspace_id != approval.workspace_id
            or result.registry_id != registry.registry_id
            or result.registry_version != registry.version
            or result.fingerprint != registry.fingerprint
            or result.approval_id != approval.id
            or result.target != approval.target
        ):
            raise RegistryPublicationError(
                RegistryPublicationErrorCode.INVALID_RESPONSE,
                "registry publisher returned a result for another payload",
            )
        try:
            validate_publication_audit_binding(
                result.audit_records,
                approval_id=approval.id,
                actor=approval.actor,
                approved_at=approval.approved_at,
                new_fingerprint=registry.fingerprint,
                approved_decision_ids=semantic_registry_decision_ids(registry),
            )
        except ValueError as error:
            raise RegistryPublicationError(
                RegistryPublicationErrorCode.INVALID_RESPONSE,
                "registry publisher returned an audit for another approval",
            ) from error
        try:
            self.audit_store.append(result.audit_records)
        except PublicationAuditStoreError as error:
            raise RegistryPublicationError(
                RegistryPublicationErrorCode.AUDIT_UNAVAILABLE,
                "registry publication audit could not be durably recorded",
            ) from error
        return result


def _ensure_approval_reservation(
    audit_store: PublicationAuditStorePort,
    registry: GovernedSemanticRegistrySnapshot,
    approval: RegistryPublicationApproval,
) -> None:
    try:
        existing = audit_store.list_for_approval(approval.id)
        if existing:
            _validate_approval_history(existing, registry, approval)
            return
        audit_store.append(
            (
                PublicationTargetAuditRecord(
                    family=PublicationFamily.REGISTRY,
                    operation="versioned_document",
                    target=approval.target,
                    approval_id=approval.id,
                    actor=approval.actor,
                    approved_at=approval.approved_at,
                    previous_fingerprint=None,
                    new_fingerprint=registry.fingerprint,
                    outcome=PublicationAuditOutcome.NOT_ATTEMPTED,
                    decision_ids=semantic_registry_decision_ids(registry),
                    reason_code="approval_reserved",
                ),
            )
        )
    except (PublicationAuditStoreError, ValueError) as error:
        raise RegistryPublicationError(
            RegistryPublicationErrorCode.AUDIT_UNAVAILABLE,
            "registry publication approval could not be durably reserved",
        ) from error


def _validate_approval_history(
    records: tuple[PublicationTargetAuditRecord, ...],
    registry: GovernedSemanticRegistrySnapshot,
    approval: RegistryPublicationApproval,
) -> None:
    validate_publication_audit_binding(
        records,
        approval_id=approval.id,
        actor=approval.actor,
        approved_at=approval.approved_at,
        new_fingerprint=registry.fingerprint,
        approved_decision_ids=semantic_registry_decision_ids(registry),
    )
    if any(
        record.family is not PublicationFamily.REGISTRY
        or record.operation != "versioned_document"
        or record.target != approval.target
        for record in records
    ):
        raise ValueError("registry publication approval history is invalid")
