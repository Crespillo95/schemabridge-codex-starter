"""Application ports for semantic-change inspection, decisions, and runtime gating."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from schemabridge.domain.semantic_change import (
    SemanticBindingSelectionSet,
    SemanticChangeCommit,
    SemanticChangeDecision,
    SemanticChangeDecisionApproval,
    SemanticChangeDecisionProposal,
    SemanticChangeFinding,
    SemanticChangeInspectionContext,
    SemanticChangeReport,
    SemanticContextGateAssessment,
    SemanticDependencyIndexState,
    SemanticEvidenceBaseline,
    SemanticEvidenceObservation,
    SemanticImpactSet,
    SemanticPlanDependencies,
)
from schemabridge.domain.semantic_registry import (
    GovernedSemanticRegistrySnapshot,
    SemanticRegistryScope,
)


class SemanticChangePortErrorCode(StrEnum):
    """Closed adapter failures translated by the application boundary."""

    RESOURCE_UNAVAILABLE = "semantic_change_resource_unavailable"
    BINDING_AMBIGUOUS = "semantic_change_binding_ambiguous"
    EVIDENCE_UNAVAILABLE = "semantic_change_evidence_unavailable"
    DEPENDENCY_INDEX_INCOMPLETE = "semantic_change_dependency_index_incomplete"
    CAS_CONFLICT = "semantic_change_cas_conflict"
    INVALID_RESPONSE = "semantic_change_invalid_response"
    STORE_UNAVAILABLE = "semantic_change_store_unavailable"


class SemanticChangePortError(RuntimeError):
    """Sanitized semantic-change adapter failure without protected payloads."""

    def __init__(self, code: SemanticChangePortErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class SemanticChangeClockPort(Protocol):
    def now(self) -> datetime:
        """Return one timezone-aware current instant."""


class SemanticChangeEvidencePort(Protocol):
    """Observe only the exact governed resources in one immutable context."""

    def observe(
        self,
        context: SemanticChangeInspectionContext,
        registry: GovernedSemanticRegistrySnapshot,
        baseline: SemanticEvidenceBaseline | None,
        *,
        binding_selections: SemanticBindingSelectionSet | None = None,
    ) -> SemanticEvidenceObservation:
        """Return bounded metadata and aggregate-only join evidence."""


class SemanticChangeDependencyIndexPort(Protocol):
    """Resolve an exact, deduplicated workflow/recipe blast radius."""

    def load_state(
        self,
        scope: SemanticRegistryScope,
    ) -> SemanticDependencyIndexState:
        """Load the exact completeness watermark used by this inspection."""

    def resolve_impacts(
        self,
        context: SemanticChangeInspectionContext,
        findings: tuple[SemanticChangeFinding, ...],
    ) -> SemanticImpactSet:
        """Return impacts plus an explicit completeness watermark."""


class SemanticChangeStorePort(Protocol):
    """Immutable reports and atomic evidence-head decision persistence."""

    def load_baseline(
        self,
        scope: SemanticRegistryScope,
    ) -> SemanticEvidenceBaseline | None:
        """Load the current approved evidence baseline, if one exists."""

    def load_report(
        self,
        scope: SemanticRegistryScope,
        report_id: str,
    ) -> SemanticChangeReport | None:
        """Load one exact immutable report without cross-scope fallback."""

    def load_observation(
        self,
        scope: SemanticRegistryScope,
        report_id: str,
    ) -> SemanticEvidenceObservation | None:
        """Load the bounded aggregate-only evidence captured for one exact report."""

    def record_report(
        self,
        report: SemanticChangeReport,
        observation: SemanticEvidenceObservation,
    ) -> SemanticChangeReport:
        """Append or exactly replay one immutable inspection report."""

    def load_head(
        self,
        scope: SemanticRegistryScope,
    ) -> SemanticChangeCommit | None:
        """Load the current compare-and-swap evidence head."""

    def commit_decision(
        self,
        proposal: SemanticChangeDecisionProposal,
        approval: SemanticChangeDecisionApproval,
        decision: SemanticChangeDecision,
        baseline: SemanticEvidenceBaseline | None,
    ) -> SemanticChangeCommit:
        """Atomically append the decision/audit and compare-and-swap the baseline head."""


class SemanticChangeCurrentInspectionPort(Protocol):
    """Capture the current exact evidence used for decision revalidation."""

    def capture_current(
        self,
        *,
        binding_selections: SemanticBindingSelectionSet | None = None,
    ) -> tuple[SemanticChangeReport, SemanticEvidenceObservation]:
        """Capture current evidence without persisting a report."""


class SemanticChangeGateReadPort(Protocol):
    """Read the minimized current-context projection used before protected I/O."""

    def assess(
        self,
        dependencies: SemanticPlanDependencies,
    ) -> SemanticContextGateAssessment:
        """Assess only the mappings and joins required by one resolved plan."""


__all__ = [
    "SemanticChangeClockPort",
    "SemanticChangeCurrentInspectionPort",
    "SemanticChangeDependencyIndexPort",
    "SemanticChangeEvidencePort",
    "SemanticChangeGateReadPort",
    "SemanticChangePortError",
    "SemanticChangePortErrorCode",
    "SemanticChangeStorePort",
]
