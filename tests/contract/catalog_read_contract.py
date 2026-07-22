"""Contract shared by fake, recorded, and live catalog adapters."""

from schemabridge.application.ports.catalog import (
    CatalogReadPort,
    EvidenceStatus,
    LineageDirection,
    PageRequest,
)
from schemabridge.domain.fields import PhysicalDatasetRef

NORTH_STAR_FIELDS = {
    "crm.customers": {"customer_id", "registration_date"},
    "legacy.client_master": {"client_no"},
    "bank.account_holders": {"gf_customer_id", "holder_type"},
}


def assert_catalog_read_contract(catalog: CatalogReadPort) -> None:
    """Assert the stable M05 behavior without referencing DataHub models."""

    first = catalog.search_assets("*", PageRequest(size=2))
    assert len(first.items) == 2
    assert first.next_cursor is not None
    second = catalog.search_assets("*", PageRequest(size=2, cursor=first.next_cursor))
    assert second.items
    assert {asset.urn for asset in first.items}.isdisjoint(asset.urn for asset in second.items)

    for dataset_text, required_fields in NORTH_STAR_FIELDS.items():
        dataset = PhysicalDatasetRef(dataset_text)
        asset = catalog.get_asset(dataset)
        assert asset.dataset == dataset
        assert asset.platform == "postgres"
        assert asset.description

        field_names: set[str] = set()
        cursor: str | None = None
        while True:
            fields = catalog.list_schema_fields(dataset, PageRequest(size=2, cursor=cursor))
            field_names.update(".".join(field.field_path) for field in fields.items)
            cursor = fields.next_cursor
            if cursor is None:
                break
        assert required_fields <= field_names

    customers = PhysicalDatasetRef("crm.customers")
    lineage = catalog.list_lineage_paths(
        customers,
        LineageDirection.UPSTREAM,
        PageRequest(size=2),
    )
    assert lineage.status is EvidenceStatus.MISSING
    assert lineage.reason_code == "lineage_not_recorded"
    assert lineage.page.items == ()

    queries = catalog.list_query_context(customers, PageRequest(size=2))
    assert queries.status is EvidenceStatus.MISSING
    assert queries.reason_code == "query_context_not_recorded"
    assert queries.page.items == ()

    documents = catalog.search_documents("approved customer mapping", PageRequest(size=2))
    assert documents.status is EvidenceStatus.UNAVAILABLE
    assert documents.reason_code == "document_tools_disabled"
