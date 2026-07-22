"""DataHub MCP payload translation and failure semantics."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from schemabridge.adapters.datahub.catalog import DataHubMcpCatalogAdapter
from schemabridge.adapters.datahub.mcp_client import (
    McpClientError,
    McpFailureKind,
)
from schemabridge.application.ports.catalog import (
    CatalogCapabilityUnavailableError,
    CatalogPermissionDeniedError,
    CatalogUnavailableError,
    EvidenceStatus,
    LineageDirection,
    PageRequest,
)
from schemabridge.domain.fields import PhysicalDatasetRef

CRM_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge.crm.customers,PROD)"
CRM_DEV_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge.crm.customers,DEV)"


@dataclass
class StubMcpClient:
    tools: frozenset[str]
    responses: dict[str, object]
    error: McpClientError | None = None
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def list_tools(self) -> frozenset[str]:
        if self.error is not None and self.error.tool_name == "list_tools":
            raise self.error
        return self.tools

    def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        self.calls.append((name, arguments))
        if self.error is not None:
            raise self.error
        return self.responses[name]


def test_search_and_schema_pagination_translate_offsets_and_partial_results() -> None:
    client = StubMcpClient(
        tools=frozenset({"search", "list_schema_fields"}),
        responses={
            "search": {
                "start": 0,
                "total": 3,
                "searchResults": [
                    {"entity": {"urn": CRM_URN, "properties": {"name": "customers"}}},
                    {"unexpected": "omitted"},
                ],
            },
            "list_schema_fields": {
                "urn": CRM_URN,
                "fields": [
                    {
                        "fieldPath": "customer_id",
                        "nativeDataType": "VARCHAR(11)",
                        "nullable": False,
                        "isPartOfKey": True,
                    }
                ],
                "totalFields": 4,
                "returned": 1,
                "remainingCount": 3,
                "offset": 0,
            },
        },
    )
    adapter = DataHubMcpCatalogAdapter(client)

    assets = adapter.search_assets("customers", PageRequest(size=2))
    fields = adapter.list_schema_fields(
        PhysicalDatasetRef("crm.customers"),
        PageRequest(size=2),
    )

    assert assets.partial is True
    assert assets.next_cursor == "1"
    assert assets.missing_metadata == ("unparseable_search_result",)
    assert fields.partial is True
    assert fields.next_cursor == "1"
    assert fields.items[0].native_type == "VARCHAR(11)"
    assert client.calls[0][1]["offset"] == 0
    assert client.calls[1][1]["limit"] == 2


def test_search_omits_assets_from_another_datahub_environment() -> None:
    client = StubMcpClient(
        tools=frozenset({"search"}),
        responses={
            "search": {
                "start": 0,
                "total": 1,
                "searchResults": [
                    {"entity": {"urn": CRM_DEV_URN, "properties": {"name": "customers"}}}
                ],
            }
        },
    )

    assets = DataHubMcpCatalogAdapter(client, environment="PROD").search_assets(
        "customers", PageRequest(size=2)
    )

    assert assets.items == ()
    assert assets.partial is True
    assert assets.missing_metadata == ("unparseable_search_result",)


def test_absent_lineage_query_and_document_tools_are_typed_evidence() -> None:
    client = StubMcpClient(
        tools=frozenset({"get_lineage", "get_dataset_queries"}),
        responses={
            "get_lineage": {"upstreams": {"total": 0}},
            "get_dataset_queries": {"start": 0, "total": 0, "count": 2},
        },
    )
    adapter = DataHubMcpCatalogAdapter(client)
    dataset = PhysicalDatasetRef("crm.customers")

    lineage = adapter.list_lineage_paths(
        dataset,
        LineageDirection.UPSTREAM,
        PageRequest(size=2),
    )
    queries = adapter.list_query_context(dataset, PageRequest(size=2))
    documents = adapter.search_documents("customer", PageRequest(size=2))

    assert lineage.status is EvidenceStatus.MISSING
    assert lineage.reason_code == "lineage_not_recorded"
    assert queries.status is EvidenceStatus.MISSING
    assert queries.reason_code == "query_context_not_recorded"
    assert documents.status is EvidenceStatus.UNAVAILABLE
    assert documents.reason_code == "document_tools_disabled"


def test_entity_governance_lineage_query_and_document_payloads_are_translated() -> None:
    legacy_urn = (
        "urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge.legacy.client_master,PROD)"
    )
    client = StubMcpClient(
        tools=frozenset(
            {
                "get_entities",
                "get_lineage",
                "get_dataset_queries",
                "search_documents",
            }
        ),
        responses={
            "get_entities": {
                "urn": CRM_URN,
                "name": "customers",
                "platform": {"name": "postgres"},
                "properties": {"description": "CRM customer metadata."},
                "subTypes": {"typeNames": ["Table"]},
                "ownership": {
                    "owners": [{"owner": {"properties": {"displayName": "Data Steward"}}}]
                },
                "globalTags": {"tags": [{"tag": {"properties": {"name": "Synthetic"}}}]},
                "glossaryTerms": {"terms": [{"term": {"properties": {"name": "Customer"}}}]},
                "domains": {"domains": [{"domain": {"properties": {"name": "Demo"}}}]},
            },
            "get_lineage": {
                "upstreams": {
                    "total": 1,
                    "searchResults": [{"entity": {"urn": legacy_urn}, "degree": 1}],
                }
            },
            "get_dataset_queries": {
                "start": 0,
                "total": 1,
                "count": 1,
                "queries": [
                    {
                        "urn": "urn:li:query:context-1",
                        "properties": {
                            "name": "Read-only context",
                            "source": "MANUAL",
                            "statement": {
                                "value": "SELECT customer_id FROM crm.customers",
                                "language": "SQL",
                            },
                        },
                        "subjects": [CRM_URN],
                    }
                ],
            },
            "search_documents": {
                "total": 1,
                "searchResults": [
                    {
                        "entity": {
                            "urn": "urn:li:document:decision-1",
                            "properties": {
                                "name": "Approved customer mapping",
                                "description": "Sanitized decision summary.",
                            },
                        }
                    }
                ],
            },
        },
    )
    adapter = DataHubMcpCatalogAdapter(client)
    dataset = PhysicalDatasetRef("crm.customers")

    asset = adapter.get_asset(dataset)
    lineage = adapter.list_lineage_paths(
        dataset,
        LineageDirection.UPSTREAM,
        PageRequest(size=2),
    )
    queries = adapter.list_query_context(dataset, PageRequest(size=2))
    documents = adapter.search_documents("customer", PageRequest(size=2))

    assert asset.governance.owners == ("Data Steward",)
    assert asset.governance.tags == ("Synthetic",)
    assert asset.governance.glossary_terms == ("Customer",)
    assert asset.governance.domain == "Demo"
    assert lineage.status is EvidenceStatus.PRESENT
    assert lineage.page.items[0].source == PhysicalDatasetRef("legacy.client_master")
    assert queries.status is EvidenceStatus.PRESENT
    assert queries.page.items[0].subjects == (dataset,)
    assert documents.status is EvidenceStatus.PRESENT
    assert documents.page.items[0].title == "Approved customer mapping"


def test_required_tool_absence_is_a_typed_capability_error() -> None:
    adapter = DataHubMcpCatalogAdapter(StubMcpClient(tools=frozenset(), responses={}))

    with pytest.raises(CatalogCapabilityUnavailableError):
        adapter.search_assets("customers", PageRequest(size=2))


@pytest.mark.parametrize(
    ("kind", "error_type"),
    [
        (McpFailureKind.PERMISSION_DENIED, CatalogPermissionDeniedError),
        (McpFailureKind.UNAVAILABLE, CatalogUnavailableError),
    ],
)
def test_mcp_failures_are_sanitized_typed_application_errors(
    kind: McpFailureKind,
    error_type: type[Exception],
) -> None:
    client = StubMcpClient(
        tools=frozenset({"search"}),
        responses={},
        error=McpClientError("search", kind),
    )

    with pytest.raises(error_type) as raised:
        DataHubMcpCatalogAdapter(client).search_assets("customers", PageRequest(size=2))

    assert "credential" not in str(raised.value).casefold()
