"""Deterministic in-memory catalog adapter for application tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeVar

from schemabridge.application.ports.catalog import (
    CatalogAsset,
    CatalogDocument,
    CatalogEvidencePage,
    CatalogField,
    CatalogInvalidResponseError,
    CatalogLineagePath,
    CatalogNotFoundError,
    CatalogPage,
    CatalogQueryContext,
    EvidenceStatus,
    LineageDirection,
    PageRequest,
)
from schemabridge.domain.fields import PhysicalDatasetRef

ItemT = TypeVar("ItemT")


@dataclass(frozen=True, slots=True)
class CatalogRecord:
    """One complete, sanitized in-memory asset record."""

    asset: CatalogAsset
    fields: tuple[CatalogField, ...]
    upstream: tuple[CatalogLineagePath, ...] = ()
    downstream: tuple[CatalogLineagePath, ...] = ()
    query_context: tuple[CatalogQueryContext, ...] = ()


class FakeCatalogAdapter:
    """Contract-compatible catalog fake with real pagination semantics."""

    def __init__(
        self,
        records: tuple[CatalogRecord, ...],
        *,
        documents: tuple[CatalogDocument, ...] = (),
        documents_available: bool = False,
        source_label: str = "fake",
    ) -> None:
        self._records = {record.asset.dataset.root: record for record in records}
        self._documents = documents
        self._documents_available = documents_available
        self._source_label = source_label

    @property
    def source_label(self) -> str:
        return self._source_label

    def search_assets(self, query: str, page: PageRequest) -> CatalogPage[CatalogAsset]:
        normalized = query.strip().casefold()
        if not normalized:
            raise ValueError("catalog search query must not be blank")
        assets = tuple(
            record.asset
            for record in self._records.values()
            if normalized == "*"
            or normalized
            in " ".join(
                (
                    record.asset.dataset.root,
                    record.asset.name,
                    record.asset.description or "",
                )
            ).casefold()
        )
        return _page(assets, page, "search_assets")

    def get_asset(self, dataset: PhysicalDatasetRef) -> CatalogAsset:
        return self._record(dataset, "get_asset").asset

    def list_schema_fields(
        self,
        dataset: PhysicalDatasetRef,
        page: PageRequest,
    ) -> CatalogPage[CatalogField]:
        return _page(
            self._record(dataset, "list_schema_fields").fields,
            page,
            "list_schema_fields",
        )

    def list_lineage_paths(
        self,
        dataset: PhysicalDatasetRef,
        direction: LineageDirection,
        page: PageRequest,
    ) -> CatalogEvidencePage[CatalogLineagePath]:
        record = self._record(dataset, "list_lineage_paths")
        items = record.upstream if direction is LineageDirection.UPSTREAM else record.downstream
        return _evidence_page(items, page, "list_lineage_paths", "lineage_not_recorded")

    def list_query_context(
        self,
        dataset: PhysicalDatasetRef,
        page: PageRequest,
    ) -> CatalogEvidencePage[CatalogQueryContext]:
        return _evidence_page(
            self._record(dataset, "list_query_context").query_context,
            page,
            "list_query_context",
            "query_context_not_recorded",
        )

    def search_documents(
        self,
        query: str,
        page: PageRequest,
    ) -> CatalogEvidencePage[CatalogDocument]:
        if not self._documents_available:
            return CatalogEvidencePage(
                status=EvidenceStatus.UNAVAILABLE,
                page=CatalogPage(items=()),
                reason_code="document_tools_disabled",
            )
        normalized = query.strip().casefold()
        matches = tuple(
            document
            for document in self._documents
            if normalized == "*"
            or normalized in f"{document.title} {document.description or ''}".casefold()
        )
        return _evidence_page(
            matches,
            page,
            "search_documents",
            "documents_not_recorded",
        )

    def _record(self, dataset: PhysicalDatasetRef, operation: str) -> CatalogRecord:
        try:
            return self._records[dataset.root]
        except KeyError as error:
            raise CatalogNotFoundError(operation, "catalog asset was not found") from error


def _page(items: tuple[ItemT, ...], request: PageRequest, operation: str) -> CatalogPage[ItemT]:
    offset = _offset(request, operation)
    selected = items[offset : offset + request.size]
    next_offset = offset + len(selected)
    return CatalogPage(
        items=selected,
        next_cursor=str(next_offset) if next_offset < len(items) else None,
    )


def _evidence_page(
    items: tuple[ItemT, ...],
    request: PageRequest,
    operation: str,
    missing_reason: str,
) -> CatalogEvidencePage[ItemT]:
    page = _page(items, request, operation)
    if not items:
        return CatalogEvidencePage(
            status=EvidenceStatus.MISSING,
            page=page,
            reason_code=missing_reason,
        )
    return CatalogEvidencePage(status=EvidenceStatus.PRESENT, page=page)


def _offset(page: PageRequest, operation: str) -> int:
    if page.cursor is None:
        return 0
    if not page.cursor.isdecimal():
        raise CatalogInvalidResponseError(operation, "catalog page cursor is invalid")
    return int(page.cursor)
