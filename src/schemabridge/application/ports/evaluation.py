"""Ports for versioned evaluation inputs, release identity, and report output."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Protocol

from schemabridge.domain.evaluation import (
    EvaluationGroundTruth,
    EvaluationReleaseIdentity,
    EvaluationReport,
)


class EvaluationErrorCode(StrEnum):
    GROUND_TRUTH_UNAVAILABLE = "evaluation_ground_truth_unavailable"
    GROUND_TRUTH_INVALID = "evaluation_ground_truth_invalid"
    REPORT_WRITE_FAILED = "evaluation_report_write_failed"
    RELEASE_IDENTITY_FAILED = "evaluation_release_identity_failed"


class EvaluationError(RuntimeError):
    """Sanitized evaluation boundary failure."""

    def __init__(self, code: EvaluationErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class EvaluationGroundTruthPort(Protocol):
    def load(self) -> EvaluationGroundTruth:
        """Load and validate every versioned synthetic evaluation source."""


class EvaluationReleaseIdentityPort(Protocol):
    def inspect(self) -> EvaluationReleaseIdentity:
        """Describe the exact commit or explicitly uncommitted source snapshot."""


class EvaluationReportWriterPort(Protocol):
    def write(self, report: EvaluationReport, json_path: Path, markdown_path: Path) -> None:
        """Write deterministic machine- and judge-readable report artifacts."""
