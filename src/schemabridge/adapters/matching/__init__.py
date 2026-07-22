"""Deterministic adapters for semantic-candidate evidence and fixtures."""

from schemabridge.adapters.matching.evaluation import YamlCandidateEvaluationAdapter
from schemabridge.adapters.matching.evidence import (
    EmptyCandidateEvidenceAdapter,
    FakeCandidateEvidenceAdapter,
    RecordedCandidateEvidenceAdapter,
)
from schemabridge.adapters.matching.fake_llm import FakeDescriptionInterpreter

__all__ = [
    "EmptyCandidateEvidenceAdapter",
    "FakeCandidateEvidenceAdapter",
    "FakeDescriptionInterpreter",
    "RecordedCandidateEvidenceAdapter",
    "YamlCandidateEvaluationAdapter",
]
