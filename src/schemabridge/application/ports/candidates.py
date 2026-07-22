"""Application ports for bounded semantic evidence and optional explanations."""

from __future__ import annotations

from typing import Protocol

from schemabridge.domain.candidates import (
    CandidateEvaluationDataset,
    CandidateFieldMetadata,
    DescriptionInterpretation,
    ExternalCandidateEvidence,
)
from schemabridge.domain.concepts import CanonicalField, LogicalFieldRef
from schemabridge.domain.fields import PhysicalFieldRef


class CandidateEvidencePort(Protocol):
    """Read bounded precomputed evidence without exposing source samples."""

    def evidence_for(
        self,
        logical_field: LogicalFieldRef,
        physical_fields: tuple[PhysicalFieldRef, ...],
    ) -> tuple[ExternalCandidateEvidence, ...]:
        """Return evidence only for the requested concept/field pairs."""


class DescriptionInterpreterPort(Protocol):
    """Optional explanation-only semantic interpretation boundary."""

    def interpret(
        self,
        concept: CanonicalField,
        fields: tuple[CandidateFieldMetadata, ...],
    ) -> tuple[DescriptionInterpretation, ...]:
        """Explain descriptions without scores, transformations, or approval states."""


class CandidateEvaluationDatasetPort(Protocol):
    """Load a labeled synthetic fixture through an adapter boundary."""

    def load(self) -> CandidateEvaluationDataset:
        """Return the validated evaluation fixture."""
