"""Ports and sanitized failures for the authoritative registry control plane."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from schemabridge.domain.registry_control import (
    ActiveRegistryPointer,
    ControlAuditChainVerification,
    GovernedRegistryVersion,
    RegistryActivationReadyHandoff,
    RegistryActivationTransition,
    RegistryControlCommit,
    RegistryProjectionOutboxItem,
    RegistryProjectionOutcome,
    RegistryProjectionState,
    RegistryReconciliationApproval,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope


class RegistryControlErrorCode(StrEnum):
    POINTER_NOT_FOUND = "registry_pointer_not_found"
    STORE_UNAVAILABLE = "registry_control_store_unavailable"
    VERSION_UNAVAILABLE = "registry_version_unavailable"
    VERSION_INVALID = "registry_version_invalid"
    LEGACY_VERSION = "registry_version_legacy_read_only"
    ACTIVATION_NOT_READY = "registry_activation_not_ready"
    ACTIVATION_HANDOFF_MISMATCH = "registry_activation_handoff_mismatch"
    ALREADY_ACTIVE = "registry_version_already_active"
    APPROVAL_REQUIRED = "registry_activation_approval_required"
    APPROVAL_MISMATCH = "registry_activation_approval_mismatch"
    CAS_CONFLICT = "registry_activation_cas_conflict"
    ROLLBACK_NOT_ALLOWED = "registry_rollback_not_allowed"
    INVALID_RESPONSE = "registry_control_invalid_response"
    RECONCILIATION_CONFLICT = "registry_reconciliation_conflict"
    PROJECTION_UNAVAILABLE = "registry_projection_unavailable"


class RegistryControlError(RuntimeError):
    """Safe registry-control failure suitable for an entrypoint boundary."""

    def __init__(self, code: RegistryControlErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class RegistryVersionReadPort(Protocol):
    """Read one exact immutable version with its observed approval trust."""

    def load_version(
        self,
        scope: SemanticRegistryScope,
        version: int,
    ) -> GovernedRegistryVersion:
        """Load only the requested exact DataHub version."""


class ActiveRegistryPointerReadPort(Protocol):
    """Read the authoritative active pointer for one exact scope."""

    def load_active(self, scope: SemanticRegistryScope) -> ActiveRegistryPointer | None:
        """Load the current CAS pointer for one exact scope."""


class RegistryActivationReadyReadPort(Protocol):
    """Read one bounded M34 activation-ready receipt with current catalog authority."""

    def load_activation_ready_handoff(
        self,
        scope: SemanticRegistryScope,
        version: int,
    ) -> RegistryActivationReadyHandoff | None:
        """Return only an exact activation-ready publication whose catalog is still current."""


class RegistryControlStorePort(
    ActiveRegistryPointerReadPort,
    RegistryActivationReadyReadPort,
    Protocol,
):
    """Authoritative pointer/history/outbox/audit persistence boundary."""

    def list_transitions(
        self,
        scope: SemanticRegistryScope,
        *,
        limit: int = 100,
    ) -> tuple[RegistryActivationTransition, ...]:
        """Return bounded immutable activation history in generation order."""

    def commit_transition(
        self,
        transition: RegistryActivationTransition,
        outbox: RegistryProjectionOutboxItem,
    ) -> RegistryControlCommit:
        """Atomically CAS pointer, transition, outbox, and control-audit event."""

    def load_pending_outbox(
        self,
        scope: SemanticRegistryScope,
    ) -> RegistryProjectionOutboxItem | None:
        """Load the newest pending projection for one scope."""

    def load_transition_outbox(
        self,
        scope: SemanticRegistryScope,
        transition_id: str,
    ) -> RegistryProjectionOutboxItem | None:
        """Load the bounded projection state for one exact scoped transition."""

    def has_audit_event(self, transition_id: str) -> bool:
        """Return whether the immutable control audit contains this transition."""

    def verify_audit_chain(self, workspace_id: str) -> ControlAuditChainVerification:
        """Verify the complete HMAC chain for one exact workspace."""

    def record_projection_outcome(self, outcome: RegistryProjectionOutcome) -> None:
        """Atomically close, supersede, or block one exact outbox record."""


class RegistryProjectionPort(Protocol):
    """Read or approval-gated write of the non-authoritative DataHub projection."""

    def read(self, scope: SemanticRegistryScope) -> RegistryProjectionState | None:
        """Read the exact bounded active-pointer projection."""

    def project(
        self,
        desired: RegistryProjectionState,
        approval: RegistryReconciliationApproval,
    ) -> RegistryProjectionState:
        """Write and read back only the exact approved projection."""
