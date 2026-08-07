"""Evaluation fixture, release-identity, and reporting adapters."""

from schemabridge.adapters.evaluation.ground_truth import (
    RecordedEvaluationGroundTruthAdapter,
    StaticEvaluationRecipeAdapter,
)
from schemabridge.adapters.evaluation.query_studio_matching import (
    QueryStudioDeterministicEvaluationReport,
    QueryStudioRetrievalMetrics,
    evaluate_recorded_query_studio_matching,
    write_query_studio_matching_report,
)
from schemabridge.adapters.evaluation.release import GitEvaluationReleaseIdentity
from schemabridge.adapters.evaluation.reporting import FileEvaluationReportWriter

__all__ = [
    "FileEvaluationReportWriter",
    "GitEvaluationReleaseIdentity",
    "QueryStudioDeterministicEvaluationReport",
    "QueryStudioRetrievalMetrics",
    "RecordedEvaluationGroundTruthAdapter",
    "StaticEvaluationRecipeAdapter",
    "evaluate_recorded_query_studio_matching",
    "write_query_studio_matching_report",
]
