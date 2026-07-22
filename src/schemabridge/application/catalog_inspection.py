"""Catalog inspection use case with no DataHub or MCP dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeVar

from schemabridge.application.ports.catalog import (
    CatalogAsset,
    CatalogEvidencePage,
    CatalogField,
    CatalogLineagePath,
    CatalogQueryContext,
    CatalogReadPort,
    LineageDirection,
    PageRequest,
)
from schemabridge.domain.fields import PhysicalDatasetRef


@dataclass(frozen=True, slots=True)
class CatalogInspectionReport:
    """Bounded evidence collected for one physical dataset."""

    source: str
    asset: CatalogAsset
    fields: tuple[CatalogField, ...]
    fields_partial: bool
    upstream: CatalogEvidencePage[CatalogLineagePath]
    downstream: CatalogEvidencePage[CatalogLineagePath]
    query_context: CatalogEvidencePage[CatalogQueryContext]

    def as_dict(self) -> dict[str, Any]:
        """Return a presentation-ready payload without vendor models."""

        return {
            "source": self.source,
            "asset": {
                "dataset": str(self.asset.dataset),
                "urn": self.asset.urn,
                "name": self.asset.name,
                "platform": self.asset.platform,
                "description": self.asset.description,
                "governance": {
                    "owners": list(self.asset.governance.owners),
                    "tags": list(self.asset.governance.tags),
                    "glossary_terms": list(self.asset.governance.glossary_terms),
                    "domain": self.asset.governance.domain,
                },
                "subtypes": list(self.asset.subtypes),
            },
            "fields": [
                {
                    "id": str(field.id),
                    "path": ".".join(field.field_path),
                    "native_type": field.native_type,
                    "description": field.description,
                    "nullable": field.nullable,
                    "is_part_of_key": field.is_part_of_key,
                    "tags": list(field.tags),
                    "glossary_terms": list(field.glossary_terms),
                }
                for field in self.fields
            ],
            "fields_partial": self.fields_partial,
            "lineage": {
                "upstream": _evidence_summary(self.upstream),
                "downstream": _evidence_summary(self.downstream),
            },
            "query_context": _evidence_summary(self.query_context),
        }


EvidenceT = TypeVar("EvidenceT")


def _evidence_summary(evidence: CatalogEvidencePage[EvidenceT]) -> dict[str, Any]:
    return {
        "status": evidence.status.value,
        "count": len(evidence.page.items),
        "partial": evidence.page.partial,
        "next_cursor": evidence.page.next_cursor,
        "reason_code": evidence.reason_code,
    }


@dataclass(frozen=True, slots=True)
class InspectCatalogAsset:
    """Collect bounded catalog evidence without inferring semantic meaning."""

    catalog: CatalogReadPort

    def execute(
        self,
        dataset: PhysicalDatasetRef,
        *,
        page_size: int = 50,
    ) -> CatalogInspectionReport:
        """Inspect one page of each supported evidence source."""

        page = PageRequest(size=page_size)
        fields = self.catalog.list_schema_fields(dataset, page)
        return CatalogInspectionReport(
            source=self.catalog.source_label,
            asset=self.catalog.get_asset(dataset),
            fields=fields.items,
            fields_partial=fields.partial or fields.next_cursor is not None,
            upstream=self.catalog.list_lineage_paths(
                dataset,
                LineageDirection.UPSTREAM,
                page,
            ),
            downstream=self.catalog.list_lineage_paths(
                dataset,
                LineageDirection.DOWNSTREAM,
                page,
            ),
            query_context=self.catalog.list_query_context(dataset, page),
        )
