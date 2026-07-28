"""Ports for approved semantic planning context and rejected-source inspection."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

from schemabridge.domain.connectors import GovernedExecutionTarget
from schemabridge.domain.resolution import RejectedSourceReport, RejectionCheck
from schemabridge.domain.semantic_registry import (
    GovernedSemanticRegistrySnapshot,
    RegistryPublicationApproval,
    RegistryPublicationResult,
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
)


class PlanningPortErrorCode(StrEnum):
    CONTEXT_UNAVAILABLE = "planning_context_unavailable"
    CONTEXT_FORBIDDEN = "planning_context_forbidden"
    CONTEXT_INVALID = "planning_context_invalid"
    REGISTRY_NOT_FOUND = "semantic_registry_not_found"
    REGISTRY_SCOPE_MISMATCH = "semantic_registry_scope_mismatch"
    REGISTRY_INTEGRITY_FAILED = "semantic_registry_integrity_failed"
    REJECTION_INSPECTION_UNAVAILABLE = "rejection_inspection_unavailable"
    REJECTION_INSPECTION_TIMEOUT = "rejection_inspection_timeout"
    REJECTION_INSPECTION_INVALID = "rejection_inspection_invalid"
    REJECTION_INSPECTION_FORBIDDEN = "rejection_inspection_forbidden"


class PlanningPortError(RuntimeError):
    """Sanitized failure at a planning-context or source-inspection boundary."""

    def __init__(self, code: PlanningPortErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class ProtectedSourceOperationCancelled(RuntimeError):
    """Control-flow signal raised before another protected source statement starts."""


class RegistryPublicationErrorCode(StrEnum):
    APPROVAL_REQUIRED = "registry_publication_approval_required"
    APPROVAL_MISMATCH = "registry_publication_approval_mismatch"
    CATALOG_UNAVAILABLE = "registry_publication_catalog_unavailable"
    CATALOG_PERMISSION_DENIED = "registry_publication_permission_denied"
    PAYLOAD_INVALID = "registry_publication_payload_invalid"
    CONFLICT = "registry_publication_conflict"
    INVALID_RESPONSE = "registry_publication_invalid_response"
    AUDIT_UNAVAILABLE = "registry_publication_audit_unavailable"


class RegistryPublicationError(RuntimeError):
    """Sanitized failure at the immutable registry-publication boundary."""

    def __init__(self, code: RegistryPublicationErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class GovernedSemanticRegistryPort(Protocol):
    """One workspace/catalog-bound loader for an atomic governed registry."""

    @property
    def scope(self) -> SemanticRegistryScope:
        """Return the immutable runtime scope bound at composition."""

    def load(self) -> ScopedSemanticRegistrySnapshot:
        """Load one complete, approved and integrity-checked registry snapshot."""


class GovernedSemanticRegistryPublicationPort(Protocol):
    """Approval-gated writer for one immutable registry version document."""

    def publish(
        self,
        registry: GovernedSemanticRegistrySnapshot,
        approval: RegistryPublicationApproval,
    ) -> RegistryPublicationResult:
        """Publish and read back only the exact approved immutable version."""


# Compatibility import name while downstream modules migrate to the registry terminology.
SemanticPlanningContextPort = GovernedSemanticRegistryPort


class RejectedSourceReportPort(Protocol):
    def inspect(
        self,
        checks: tuple[RejectionCheck, ...],
        *,
        statement_timeout_ms: int,
        should_continue: Callable[[], bool] | None = None,
        target: GovernedExecutionTarget | None = None,
    ) -> RejectedSourceReport:
        """Inspect allowlisted fields, checking continuation before every statement."""
