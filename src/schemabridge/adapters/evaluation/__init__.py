"""Evaluation fixture, release-identity, and reporting adapters."""

from schemabridge.adapters.evaluation.ground_truth import (
    RecordedEvaluationGroundTruthAdapter,
    StaticEvaluationRecipeAdapter,
)
from schemabridge.adapters.evaluation.release import GitEvaluationReleaseIdentity
from schemabridge.adapters.evaluation.reporting import FileEvaluationReportWriter

__all__ = [
    "FileEvaluationReportWriter",
    "GitEvaluationReleaseIdentity",
    "RecordedEvaluationGroundTruthAdapter",
    "StaticEvaluationRecipeAdapter",
]
