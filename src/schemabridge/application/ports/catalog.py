"""Stable, vendor-neutral catalog read contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, Protocol, TypeVar

from schemabridge.domain.fields import (
    PhysicalDatasetRef,
    PhysicalFieldRef,
)

MAX_CATALOG_PAGE_SIZE = 50


class CatalogErrorCode(StrEnum):
    """Machine-readable failures at the catalog boundary."""

    UNAVAILABLE = "catalog_unavailable"
    PERMISSION_DENIED = "catalog_permission_denied"
    CAPABILITY_UNAVAILABLE = "catalog_capability_unavailable"
    NOT_FOUND = "catalog_not_found"
    INVALID_RESPONSE = "catalog_invalid_response"


class CatalogReadError(RuntimeError):
    """Base error that does not expose vendor payloads or credentials."""

    code: CatalogErrorCode

    def __init__(self, operation: str, message: str) -> None:
        super().__init__(message)
        self.operation = operation


class CatalogUnavailableError(CatalogReadError):
    """The configured catalog cannot be reached."""

    code = CatalogErrorCode.UNAVAILABLE


class CatalogPermissionDeniedError(CatalogReadError):
    """The catalog identity lacks permission for the requested read."""

    code = CatalogErrorCode.PERMISSION_DENIED


class CatalogCapabilityUnavailableError(CatalogReadError):
    """The configured catalog transport does not expose a requested read tool."""

    code = CatalogErrorCode.CAPABILITY_UNAVAILABLE


class CatalogNotFoundError(CatalogReadError):
    """The requested catalog asset does not exist or is not visible."""

    code = CatalogErrorCode.NOT_FOUND


class CatalogInvalidResponseError(CatalogReadError):
    """The catalog returned malformed or unsupported metadata."""

    code = CatalogErrorCode.INVALID_RESPONSE


class EvidenceStatus(StrEnum):
    """Whether a catalog evidence source has useful, bounded content."""

    PRESENT = "present"
    MISSING = "missing"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class LineageDirection(StrEnum):
    """Direction relative to the inspected dataset."""

    UPSTREAM = "upstream"
    DOWNSTREAM = "downstream"


@dataclass(frozen=True, slots=True)
class PageRequest:
    """Opaque-cursor request with a globally bounded page size."""

    size: int = 20
    cursor: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.size <= MAX_CATALOG_PAGE_SIZE:
            raise ValueError(f"page size must be between 1 and {MAX_CATALOG_PAGE_SIZE}")
        if self.cursor is not None and not self.cursor:
            raise ValueError("page cursor must not be blank")


ItemT = TypeVar("ItemT")


@dataclass(frozen=True, slots=True)
class CatalogPage(Generic[ItemT]):
    """A bounded catalog page with explicit truncation metadata."""

    items: tuple[ItemT, ...]
    next_cursor: str | None = None
    partial: bool = False
    missing_metadata: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CatalogEvidencePage(Generic[ItemT]):
    """Catalog evidence that distinguishes absence from tool unavailability."""

    status: EvidenceStatus
    page: CatalogPage[ItemT]
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.status in {EvidenceStatus.MISSING, EvidenceStatus.UNAVAILABLE} and self.page.items:
            raise ValueError(f"{self.status.value} evidence cannot contain items")
        if self.status is EvidenceStatus.PRESENT and not self.page.items:
            raise ValueError("present evidence must contain at least one item")
        if (
            self.status in {EvidenceStatus.MISSING, EvidenceStatus.UNAVAILABLE}
            and self.reason_code is None
        ):
            raise ValueError(f"{self.status.value} evidence requires a reason code")


@dataclass(frozen=True, slots=True)
class GovernanceMetadata:
    """Bounded governance signals translated out of the catalog model."""

    owners: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    glossary_terms: tuple[str, ...] = ()
    domain: str | None = None


@dataclass(frozen=True, slots=True)
class CatalogAsset:
    """Vendor-neutral physical asset details."""

    dataset: PhysicalDatasetRef
    urn: str
    name: str
    platform: str
    description: str | None = None
    governance: GovernanceMetadata = GovernanceMetadata()
    subtypes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CatalogField:
    """Schema metadata that remains useful when optional properties are missing."""

    id: PhysicalFieldRef
    dataset: PhysicalDatasetRef
    field_path: tuple[str, ...]
    native_type: str | None
    description: str | None = None
    nullable: bool | None = None
    is_part_of_key: bool | None = None
    tags: tuple[str, ...] = ()
    glossary_terms: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CatalogLineagePath:
    """A bounded dataset-level lineage path relative to the inspected asset."""

    source: PhysicalDatasetRef
    target: PhysicalDatasetRef
    direction: LineageDirection
    hops: int


@dataclass(frozen=True, slots=True)
class CatalogQueryContext:
    """Saved query metadata; its statement is evidence and is never executable input."""

    urn: str
    statement: str
    language: str | None = None
    source: str | None = None
    name: str | None = None
    subjects: tuple[PhysicalDatasetRef, ...] = ()


@dataclass(frozen=True, slots=True)
class CatalogDocument:
    """Read-only saved decision/document summary."""

    urn: str
    title: str
    description: str | None = None


class CatalogReadPort(Protocol):
    """Small read surface consumed by catalog-aware application use cases."""

    @property
    def source_label(self) -> str:
        """Human-visible source label such as live or recorded."""

    def search_assets(self, query: str, page: PageRequest) -> CatalogPage[CatalogAsset]:
        """Search physical catalog assets."""

    def get_asset(self, dataset: PhysicalDatasetRef) -> CatalogAsset:
        """Read one physical asset and its governance metadata."""

    def list_schema_fields(
        self,
        dataset: PhysicalDatasetRef,
        page: PageRequest,
    ) -> CatalogPage[CatalogField]:
        """Read a bounded page of schema fields."""

    def list_lineage_paths(
        self,
        dataset: PhysicalDatasetRef,
        direction: LineageDirection,
        page: PageRequest,
    ) -> CatalogEvidencePage[CatalogLineagePath]:
        """Read dataset lineage or report why evidence is absent."""

    def list_query_context(
        self,
        dataset: PhysicalDatasetRef,
        page: PageRequest,
    ) -> CatalogEvidencePage[CatalogQueryContext]:
        """Read saved query/SQL context without executing it."""

    def search_documents(
        self,
        query: str,
        page: PageRequest,
    ) -> CatalogEvidencePage[CatalogDocument]:
        """Search saved decisions/documents where the catalog exposes that capability."""
