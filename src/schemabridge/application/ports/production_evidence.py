"""Ports for the offline M30 candidate-readiness preflight."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Protocol

from schemabridge.domain.production_readiness import (
    M30CampaignContract,
    M30CandidateObservation,
    M30ReadinessReport,
)


class ProductionEvidenceErrorCode(StrEnum):
    CONTRACT_UNAVAILABLE = "m30_contract_unavailable"
    CONTRACT_INVALID = "m30_contract_invalid"
    CANDIDATE_INSPECTION_FAILED = "m30_candidate_inspection_failed"
    REPORT_WRITE_FAILED = "m30_report_write_failed"


class ProductionEvidenceError(RuntimeError):
    """Sanitized failure at a production-evidence boundary."""

    def __init__(self, code: ProductionEvidenceErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class M30CampaignContractPort(Protocol):
    def load(self) -> M30CampaignContract:
        """Load the exact machine-readable commercial contract."""


class M30CandidateIdentityPort(Protocol):
    def inspect(self, contract: M30CampaignContract) -> M30CandidateObservation:
        """Inspect only repository-bound candidate facts without network or external I/O."""


class M30ReadinessReportWriterPort(Protocol):
    def write(self, report: M30ReadinessReport, output_directory: Path) -> tuple[Path, Path]:
        """Write deterministic JSON and Markdown preparation reports."""


__all__ = [
    "M30CampaignContractPort",
    "M30CandidateIdentityPort",
    "M30ReadinessReportWriterPort",
    "ProductionEvidenceError",
    "ProductionEvidenceErrorCode",
]
