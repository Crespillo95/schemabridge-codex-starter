"""Ports for the offline M30 candidate-readiness preflight."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from schemabridge.domain.production_campaign import (
    M30AuthenticatedManifest,
    M30CampaignManifest,
    M30ManifestAuthenticationReport,
)
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
    MANIFEST_UNAVAILABLE = "m30_manifest_unavailable"
    MANIFEST_INVALID = "m30_manifest_invalid"
    MANIFEST_AUTHENTICATION_FAILED = "m30_manifest_authentication_failed"
    TRUST_PROVIDER_UNAVAILABLE = "m30_trust_provider_unavailable"


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


@dataclass(frozen=True, slots=True)
class LoadedM30CampaignManifest:
    """Parsed canonical bytes; the path remains private to its filesystem adapter."""

    manifest: M30CampaignManifest
    raw_sha256: str


class M30CampaignManifestPort(Protocol):
    def load(self) -> LoadedM30CampaignManifest:
        """Load one canonical manifest from outside the candidate source tree."""


class M30CampaignManifestAuthenticationPort(Protocol):
    def authenticate(
        self,
        loaded: LoadedM30CampaignManifest,
        *,
        verified_at: datetime,
    ) -> M30AuthenticatedManifest:
        """Authenticate exact bytes with the reviewed verifier; external trust remains separate."""


class M30ClockPort(Protocol):
    def now(self) -> datetime:
        """Return the current timezone-aware UTC instant."""


class M30ManifestAuthenticationReportWriterPort(Protocol):
    def write(
        self,
        report: M30ManifestAuthenticationReport,
        output_directory: Path,
    ) -> tuple[Path, Path]:
        """Write deterministic JSON/Markdown Phase-1a authentication reports."""


__all__ = [
    "LoadedM30CampaignManifest",
    "M30CampaignContractPort",
    "M30CampaignManifestAuthenticationPort",
    "M30CampaignManifestPort",
    "M30CandidateIdentityPort",
    "M30ClockPort",
    "M30ManifestAuthenticationReportWriterPort",
    "M30ReadinessReportWriterPort",
    "ProductionEvidenceError",
    "ProductionEvidenceErrorCode",
]
