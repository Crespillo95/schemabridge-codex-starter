"""Deterministic fake for the optional explanation-only language boundary."""

from schemabridge.domain.candidates import (
    CandidateFieldMetadata,
    DescriptionInterpretation,
)
from schemabridge.domain.concepts import CanonicalField


class FakeDescriptionInterpreter:
    """Return prevalidated explanations without changing any candidate score."""

    def __init__(self, interpretations: tuple[DescriptionInterpretation, ...]) -> None:
        self._interpretations = interpretations

    def interpret(
        self,
        concept: CanonicalField,
        fields: tuple[CandidateFieldMetadata, ...],
    ) -> tuple[DescriptionInterpretation, ...]:
        del concept
        requested = {field.id.root for field in fields}
        return tuple(
            interpretation
            for interpretation in self._interpretations
            if interpretation.physical_field.root in requested
        )
