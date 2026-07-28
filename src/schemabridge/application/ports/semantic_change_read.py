"""Read-only ports and minimized projections for semantic-change HTTP queries."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Generic, Protocol, TypeVar

from schemabridge.domain.semantic_change import (
    SemanticChangeKind,
    SemanticChangeSeverity,
    SemanticChangeStatus,
    SemanticImpactKind,
)

MAX_SEMANTIC_CHANGE_PAGE_SIZE = 50
MAX_SEMANTIC_CHANGE_CURSOR_BYTES = 1_024


class SemanticChangeReadResource(StrEnum):
    REPORTS = "reports"
    FINDINGS = "findings"
    IMPACTS = "impacts"


class SemanticChangeTargetKind(StrEnum):
    MAPPING = "mapping"
    JOIN = "join"


class SemanticChangeReadPortErrorCode(StrEnum):
    UNAVAILABLE = "semantic_change_read_unavailable"
    INVALID_RESPONSE = "semantic_change_read_invalid_response"


class SemanticChangeReadPortError(RuntimeError):
    """One sanitized persistence failure without storage or tenant details."""

    def __init__(self, code: SemanticChangeReadPortErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class SemanticChangeCursorError(RuntimeError):
    """A continuation could not be authenticated against its expected scope."""

    def __init__(self, message: str = "semantic change resource is unavailable") -> None:
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SemanticChangeReportFilter:
    status: SemanticChangeStatus | None = None

    @property
    def fingerprint(self) -> str:
        return semantic_change_read_fingerprint(
            {"status": None if self.status is None else self.status.value}
        )


@dataclass(frozen=True, slots=True)
class SemanticChangeFindingFilter:
    kind: SemanticChangeKind | None = None
    severity: SemanticChangeSeverity | None = None

    @property
    def fingerprint(self) -> str:
        return semantic_change_read_fingerprint(
            {
                "kind": None if self.kind is None else self.kind.value,
                "severity": None if self.severity is None else self.severity.value,
            }
        )


@dataclass(frozen=True, slots=True)
class SemanticChangeImpactFilter:
    kind: SemanticImpactKind | None = None

    @property
    def fingerprint(self) -> str:
        return semantic_change_read_fingerprint(
            {"kind": None if self.kind is None else self.kind.value}
        )


@dataclass(frozen=True, slots=True)
class SemanticChangePageRequest:
    size: int = 20
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class SemanticChangePageKey:
    sort_value: str
    stable_id: str


@dataclass(frozen=True, slots=True)
class SemanticChangeCursorBinding:
    workspace_id: str
    resource: SemanticChangeReadResource
    filter_fingerprint: str
    sort_fingerprint: str
    report_id: str | None = None


@dataclass(frozen=True, slots=True)
class SemanticChangeCursorPosition:
    last_key: SemanticChangePageKey
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SemanticChangeReportPublic:
    """Bounded report projection with no raw catalog or governed registry payload."""

    workspace_id: str
    report_id: str
    status: SemanticChangeStatus
    pointer_generation: int
    pointer_fingerprint: str
    registry_version: int
    registry_fingerprint: str
    catalog_generation_count: int
    observation_fingerprint: str
    baseline_revision: int | None
    baseline_fingerprint: str | None
    finding_count: int
    mapping_impact_count: int
    join_impact_count: int
    workflow_impact_count: int
    recipe_impact_count: int
    impacts_complete: bool
    dependency_watermark: int
    impact_set_fingerprint: str
    inspected_at: datetime
    fingerprint: str


@dataclass(frozen=True, slots=True)
class SemanticChangeFindingPublic:
    """Sanitized deterministic finding without physical field or definition text."""

    workspace_id: str
    report_id: str
    finding_id: str
    kind: SemanticChangeKind
    severity: SemanticChangeSeverity
    target_kind: SemanticChangeTargetKind
    target_id: str
    target_version: int
    previous_fingerprint: str | None
    current_fingerprint: str | None
    risks: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class SemanticChangeImpactPublic:
    """Sanitized downstream artifact identity for one tenant-scoped report."""

    workspace_id: str
    report_id: str
    kind: SemanticImpactKind
    artifact_id: str
    artifact_version: int | None
    finding_ids: tuple[str, ...]
    fingerprint: str


ReadableT = TypeVar("ReadableT")


@dataclass(frozen=True, slots=True)
class SemanticChangeStorePage(Generic[ReadableT]):
    items: tuple[ReadableT, ...]
    page_size: int
    rows_read: int
    has_more: bool
    last_key: SemanticChangePageKey | None


@dataclass(frozen=True, slots=True)
class SemanticChangePage(Generic[ReadableT]):
    items: tuple[ReadableT, ...]
    next_cursor: str | None
    as_of: datetime


class SemanticChangeReadClockPort(Protocol):
    def now(self) -> datetime:
        """Return one timezone-aware current instant."""


class SemanticChangeCursorPort(Protocol):
    def encode(
        self,
        *,
        binding: SemanticChangeCursorBinding,
        last_key: SemanticChangePageKey,
        issued_at: datetime,
    ) -> str:
        """Sign one exact tenant/report/filter-bound keyset position."""

    def decode(
        self,
        *,
        cursor: str,
        expected_binding: SemanticChangeCursorBinding,
        at: datetime,
    ) -> SemanticChangeCursorPosition:
        """Authenticate a cursor before its key reaches persistence."""


class SemanticChangeReadStorePort(Protocol):
    """Tenant-qualified, sanitized and bounded semantic-change reads."""

    def list_reports(
        self,
        workspace_id: str,
        *,
        filters: SemanticChangeReportFilter,
        page_size: int,
        after: SemanticChangePageKey | None,
    ) -> SemanticChangeStorePage[SemanticChangeReportPublic]:
        """List report summaries without materializing findings or observations."""

    def load_report(
        self,
        workspace_id: str,
        report_id: str,
    ) -> SemanticChangeReportPublic | None:
        """Load one currently readable report; hide unknown, stale and foreign reports."""

    def list_findings(
        self,
        workspace_id: str,
        report_id: str,
        *,
        filters: SemanticChangeFindingFilter,
        page_size: int,
        after: SemanticChangePageKey | None,
    ) -> SemanticChangeStorePage[SemanticChangeFindingPublic]:
        """List one visible report's deterministic finding projections."""

    def list_impacts(
        self,
        workspace_id: str,
        report_id: str,
        *,
        filters: SemanticChangeImpactFilter,
        page_size: int,
        after: SemanticChangePageKey | None,
    ) -> SemanticChangeStorePage[SemanticChangeImpactPublic]:
        """List one visible report's deduplicated impact projections."""


def semantic_change_read_fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "MAX_SEMANTIC_CHANGE_CURSOR_BYTES",
    "MAX_SEMANTIC_CHANGE_PAGE_SIZE",
    "SemanticChangeCursorBinding",
    "SemanticChangeCursorError",
    "SemanticChangeCursorPort",
    "SemanticChangeCursorPosition",
    "SemanticChangeFindingFilter",
    "SemanticChangeFindingPublic",
    "SemanticChangeImpactFilter",
    "SemanticChangeImpactPublic",
    "SemanticChangePage",
    "SemanticChangePageKey",
    "SemanticChangePageRequest",
    "SemanticChangeReadClockPort",
    "SemanticChangeReadPortError",
    "SemanticChangeReadPortErrorCode",
    "SemanticChangeReadResource",
    "SemanticChangeReadStorePort",
    "SemanticChangeReportFilter",
    "SemanticChangeReportPublic",
    "SemanticChangeStorePage",
    "SemanticChangeTargetKind",
    "semantic_change_read_fingerprint",
]
