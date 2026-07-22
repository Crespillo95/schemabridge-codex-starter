"""Ports for approved semantic planning context and rejected-source inspection."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from schemabridge.domain.resolution import (
    RejectedSourceReport,
    RejectionCheck,
    SemanticPlanningContext,
)


class PlanningPortErrorCode(StrEnum):
    CONTEXT_UNAVAILABLE = "planning_context_unavailable"
    CONTEXT_INVALID = "planning_context_invalid"
    REJECTION_INSPECTION_UNAVAILABLE = "rejection_inspection_unavailable"
    REJECTION_INSPECTION_FORBIDDEN = "rejection_inspection_forbidden"


class PlanningPortError(RuntimeError):
    """Sanitized failure at a planning-context or source-inspection boundary."""

    def __init__(self, code: PlanningPortErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class SemanticPlanningContextPort(Protocol):
    def load(self) -> SemanticPlanningContext:
        """Load one explicitly labeled, approved and versioned planning context."""


class RejectedSourceReportPort(Protocol):
    def inspect(
        self,
        checks: tuple[RejectionCheck, ...],
        *,
        statement_timeout_ms: int,
    ) -> RejectedSourceReport:
        """Inspect only allowlisted source fields in a bounded read-only transaction."""
