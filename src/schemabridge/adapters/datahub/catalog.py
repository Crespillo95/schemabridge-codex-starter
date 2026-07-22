"""Translation between DataHub MCP payloads and the catalog-read port."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Never, TypeVar, cast

from pydantic import ValidationError

from schemabridge.adapters.datahub.mcp_client import (
    McpClientError,
    McpFailureKind,
    McpToolClient,
)
from schemabridge.application.ports.catalog import (
    CatalogAsset,
    CatalogCapabilityUnavailableError,
    CatalogDocument,
    CatalogEvidencePage,
    CatalogField,
    CatalogInvalidResponseError,
    CatalogLineagePath,
    CatalogNotFoundError,
    CatalogPage,
    CatalogPermissionDeniedError,
    CatalogQueryContext,
    CatalogUnavailableError,
    EvidenceStatus,
    GovernanceMetadata,
    LineageDirection,
    PageRequest,
)
from schemabridge.domain.fields import PhysicalDatasetRef, PhysicalFieldRef

logger = logging.getLogger(__name__)

EvidenceT = TypeVar("EvidenceT")

_DATASET_URN = re.compile(
    r"^urn:li:dataset:\(urn:li:dataPlatform:(?P<platform>[^,]+),"
    r"(?P<name>[^,]+),(?P<environment>[^)]+)\)$"
)


@dataclass(slots=True)
class DataHubMcpCatalogAdapter:
    """Read DataHub metadata through a mutation-disabled MCP server."""

    client: McpToolClient
    database_name: str = "schemabridge"
    platform: str = "postgres"
    environment: str = "PROD"
    _tools: frozenset[str] | None = field(default=None, init=False, repr=False)

    @property
    def source_label(self) -> str:
        return "live:datahub-mcp"

    def search_assets(self, query: str, page: PageRequest) -> CatalogPage[CatalogAsset]:
        operation = "search_assets"
        if not query.strip():
            raise ValueError("catalog search query must not be blank")
        offset = _offset(page, operation)
        payload = self._call(
            "search",
            {
                "query": query,
                "filter": "entity_type = dataset",
                "num_results": page.size,
                "offset": offset,
            },
            operation=operation,
        )
        root = _mapping(payload, operation)
        raw_results = _sequence(root.get("searchResults", ()), operation)
        assets: list[CatalogAsset] = []
        missing: set[str] = set()
        for raw_result in raw_results:
            try:
                result = _mapping(raw_result, operation)
                entity = _mapping(result.get("entity"), operation)
                assets.append(self._asset_from_search(entity, operation))
            except CatalogInvalidResponseError:
                missing.add("unparseable_search_result")

        total = _integer(root.get("total"), default=offset + len(assets))
        returned = len(assets)
        partial = bool(missing) or returned < min(page.size, max(total - offset, 0))
        return CatalogPage(
            items=tuple(assets),
            next_cursor=_next_cursor(offset, returned, total),
            partial=partial,
            missing_metadata=tuple(sorted(missing)),
        )

    def get_asset(self, dataset: PhysicalDatasetRef) -> CatalogAsset:
        operation = "get_asset"
        urn = self._urn(dataset)
        payload = self._call("get_entities", {"urns": urn}, operation=operation, urn=urn)
        root = _mapping(payload, operation)
        if "error" in root:
            raise CatalogNotFoundError(operation, "catalog asset was not found")

        parsed_dataset = self._dataset_from_urn(_required_string(root, "urn", operation), operation)
        if parsed_dataset != dataset:
            raise CatalogInvalidResponseError(operation, "catalog returned a different asset")

        platform_data = _optional_mapping(root.get("platform"))
        properties = _optional_mapping(root.get("properties"))
        description = _optional_string(properties.get("description"))
        name = _optional_string(root.get("name")) or _optional_string(properties.get("name"))
        if name is None:
            name = dataset.root.rpartition(".")[2]
        platform = _optional_string(platform_data.get("name")) or self.platform
        subtypes = _names_from_list(
            _optional_mapping(root.get("subTypes")).get("typeNames"),
        )
        return CatalogAsset(
            dataset=dataset,
            urn=urn,
            name=name,
            platform=platform,
            description=description,
            governance=_governance(root),
            subtypes=subtypes,
        )

    def list_schema_fields(
        self,
        dataset: PhysicalDatasetRef,
        page: PageRequest,
    ) -> CatalogPage[CatalogField]:
        operation = "list_schema_fields"
        offset = _offset(page, operation)
        urn = self._urn(dataset)
        payload = self._call(
            "list_schema_fields",
            {"urn": urn, "limit": page.size, "offset": offset},
            operation=operation,
            urn=urn,
        )
        root = _mapping(payload, operation)
        raw_fields = _sequence(root.get("fields", ()), operation)
        fields: list[CatalogField] = []
        missing: set[str] = set()
        for raw_field in raw_fields:
            try:
                fields.append(_catalog_field(dataset, _mapping(raw_field, operation), operation))
            except (CatalogInvalidResponseError, ValidationError):
                missing.add("unparseable_schema_field")

        total = _integer(root.get("totalFields"), default=offset + len(fields))
        returned = len(fields)
        partial = bool(missing) or returned < min(page.size, max(total - offset, 0))
        return CatalogPage(
            items=tuple(fields),
            next_cursor=_next_cursor(offset, returned, total),
            partial=partial,
            missing_metadata=tuple(sorted(missing)),
        )

    def list_lineage_paths(
        self,
        dataset: PhysicalDatasetRef,
        direction: LineageDirection,
        page: PageRequest,
    ) -> CatalogEvidencePage[CatalogLineagePath]:
        operation = "list_lineage_paths"
        if not self._has_tool("get_lineage", operation):
            return _unavailable_evidence("lineage_tool_unavailable")
        offset = _offset(page, operation)
        urn = self._urn(dataset)
        payload = self._call(
            "get_lineage",
            {
                "urn": urn,
                "upstream": direction is LineageDirection.UPSTREAM,
                "max_results": page.size,
                "offset": offset,
            },
            operation=operation,
            urn=urn,
            check_tool=False,
        )
        root = _mapping(payload, operation)
        direction_data = _optional_mapping(root.get(f"{direction.value}s"))
        raw_results = _sequence(direction_data.get("searchResults", ()), operation)
        paths: list[CatalogLineagePath] = []
        missing: set[str] = set()
        for raw_result in raw_results:
            try:
                result = _mapping(raw_result, operation)
                entity = _mapping(result.get("entity"), operation)
                related = self._dataset_from_urn(
                    _required_string(entity, "urn", operation),
                    operation,
                )
                hops = _integer(result.get("degree"), default=1)
                source = related if direction is LineageDirection.UPSTREAM else dataset
                target = dataset if direction is LineageDirection.UPSTREAM else related
                paths.append(
                    CatalogLineagePath(
                        source=source,
                        target=target,
                        direction=direction,
                        hops=hops,
                    )
                )
            except (CatalogInvalidResponseError, ValidationError):
                missing.add("unparseable_lineage_path")

        total = _integer(direction_data.get("total"), default=offset + len(paths))
        page_result = CatalogPage(
            items=tuple(paths),
            next_cursor=_next_cursor(offset, len(paths), total),
            partial=bool(missing) or len(paths) < min(page.size, max(total - offset, 0)),
            missing_metadata=tuple(sorted(missing)),
        )
        if not paths and total == 0:
            return CatalogEvidencePage(
                status=EvidenceStatus.MISSING,
                page=page_result,
                reason_code="lineage_not_recorded",
            )
        return CatalogEvidencePage(
            status=EvidenceStatus.PARTIAL if page_result.partial else EvidenceStatus.PRESENT,
            page=page_result,
        )

    def list_query_context(
        self,
        dataset: PhysicalDatasetRef,
        page: PageRequest,
    ) -> CatalogEvidencePage[CatalogQueryContext]:
        operation = "list_query_context"
        if not self._has_tool("get_dataset_queries", operation):
            return _unavailable_evidence("query_context_tool_unavailable")
        offset = _offset(page, operation)
        urn = self._urn(dataset)
        payload = self._call(
            "get_dataset_queries",
            {"urn": urn, "start": offset, "count": page.size},
            operation=operation,
            urn=urn,
            check_tool=False,
        )
        root = _mapping(payload, operation)
        raw_queries = _sequence(root.get("queries", ()), operation)
        queries: list[CatalogQueryContext] = []
        missing: set[str] = set()
        for raw_query in raw_queries:
            try:
                queries.append(self._query_context(_mapping(raw_query, operation), operation))
            except (CatalogInvalidResponseError, ValidationError):
                missing.add("unparseable_query_context")

        total = _integer(root.get("total"), default=offset + len(queries))
        page_result = CatalogPage(
            items=tuple(queries),
            next_cursor=_next_cursor(offset, len(queries), total),
            partial=bool(missing) or len(queries) < min(page.size, max(total - offset, 0)),
            missing_metadata=tuple(sorted(missing)),
        )
        if not queries and total == 0:
            return CatalogEvidencePage(
                status=EvidenceStatus.MISSING,
                page=page_result,
                reason_code="query_context_not_recorded",
            )
        return CatalogEvidencePage(
            status=EvidenceStatus.PARTIAL if page_result.partial else EvidenceStatus.PRESENT,
            page=page_result,
        )

    def search_documents(
        self,
        query: str,
        page: PageRequest,
    ) -> CatalogEvidencePage[CatalogDocument]:
        operation = "search_documents"
        if not self._has_tool("search_documents", operation):
            return _unavailable_evidence("document_tools_disabled")
        offset = _offset(page, operation)
        payload = self._call(
            "search_documents",
            {"query": query, "num_results": page.size, "offset": offset},
            operation=operation,
            check_tool=False,
        )
        root = _mapping(payload, operation)
        raw_results = _sequence(root.get("searchResults", ()), operation)
        documents: list[CatalogDocument] = []
        missing: set[str] = set()
        for raw_result in raw_results:
            try:
                result = _mapping(raw_result, operation)
                entity = _mapping(result.get("entity"), operation)
                properties = _optional_mapping(entity.get("properties"))
                documents.append(
                    CatalogDocument(
                        urn=_required_string(entity, "urn", operation),
                        title=(
                            _optional_string(properties.get("name"))
                            or _required_string(entity, "name", operation)
                        ),
                        description=_optional_string(properties.get("description")),
                    )
                )
            except CatalogInvalidResponseError:
                missing.add("unparseable_document")
        total = _integer(root.get("total"), default=offset + len(documents))
        page_result = CatalogPage(
            items=tuple(documents),
            next_cursor=_next_cursor(offset, len(documents), total),
            partial=bool(missing) or len(documents) < min(page.size, max(total - offset, 0)),
            missing_metadata=tuple(sorted(missing)),
        )
        if not documents and total == 0:
            return CatalogEvidencePage(
                status=EvidenceStatus.MISSING,
                page=page_result,
                reason_code="documents_not_recorded",
            )
        return CatalogEvidencePage(
            status=EvidenceStatus.PARTIAL if page_result.partial else EvidenceStatus.PRESENT,
            page=page_result,
        )

    def _asset_from_search(
        self,
        entity: Mapping[str, object],
        operation: str,
    ) -> CatalogAsset:
        urn = _required_string(entity, "urn", operation)
        dataset = self._dataset_from_urn(urn, operation)
        properties = _optional_mapping(entity.get("properties"))
        name = _optional_string(properties.get("name")) or dataset.root.rpartition(".")[2]
        return CatalogAsset(
            dataset=dataset,
            urn=urn,
            name=name,
            platform=self.platform,
            description=_optional_string(properties.get("description")),
        )

    def _query_context(
        self,
        query: Mapping[str, object],
        operation: str,
    ) -> CatalogQueryContext:
        properties = _mapping(query.get("properties"), operation)
        statement = _mapping(properties.get("statement"), operation)
        subjects: list[PhysicalDatasetRef] = []
        for raw_subject in _sequence(query.get("subjects", ()), operation):
            if isinstance(raw_subject, str):
                subjects.append(self._dataset_from_urn(raw_subject, operation))
        return CatalogQueryContext(
            urn=_required_string(query, "urn", operation),
            statement=_required_string(statement, "value", operation),
            language=_optional_string(statement.get("language")),
            source=_optional_string(properties.get("source")),
            name=_optional_string(properties.get("name")),
            subjects=tuple(subjects),
        )

    def _has_tool(self, name: str, operation: str) -> bool:
        if self._tools is None:
            try:
                self._tools = self.client.list_tools()
            except McpClientError as error:
                self._raise_client_error(error, operation)
        assert self._tools is not None
        return name in self._tools

    def _call(
        self,
        tool_name: str,
        arguments: dict[str, object],
        *,
        operation: str,
        urn: str | None = None,
        check_tool: bool = True,
    ) -> object:
        if check_tool and not self._has_tool(tool_name, operation):
            raise CatalogCapabilityUnavailableError(
                operation,
                "required catalog tool is unavailable",
            )
        logger.info("catalog_read operation=%s urn=%s", operation, urn or "-")
        try:
            return self.client.call_tool(tool_name, arguments)
        except McpClientError as error:
            self._raise_client_error(error, operation)

    def _raise_client_error(self, error: McpClientError, operation: str) -> Never:
        if error.kind is McpFailureKind.PERMISSION_DENIED:
            raise CatalogPermissionDeniedError(
                operation, "catalog read permission was denied"
            ) from error
        if error.kind is McpFailureKind.NOT_FOUND:
            raise CatalogNotFoundError(operation, "catalog asset was not found") from error
        if error.kind is McpFailureKind.UNAVAILABLE:
            raise CatalogUnavailableError(operation, "configured catalog is unavailable") from error
        raise CatalogInvalidResponseError(
            operation, "catalog tool returned an invalid response"
        ) from error

    def _urn(self, dataset: PhysicalDatasetRef) -> str:
        return (
            f"urn:li:dataset:(urn:li:dataPlatform:{self.platform},"
            f"{self.database_name}.{dataset.root},{self.environment})"
        )

    def _dataset_from_urn(self, urn: str, operation: str) -> PhysicalDatasetRef:
        match = _DATASET_URN.fullmatch(urn)
        if (
            match is None
            or match.group("platform") != self.platform
            or match.group("environment") != self.environment
        ):
            raise CatalogInvalidResponseError(operation, "unsupported dataset URN")
        name = match.group("name")
        prefix = f"{self.database_name}."
        if name.startswith(prefix):
            name = name[len(prefix) :]
        parts = name.split(".")
        if len(parts) != 2:
            raise CatalogInvalidResponseError(operation, "dataset is not schema-qualified")
        try:
            return PhysicalDatasetRef(name)
        except ValidationError as error:
            raise CatalogInvalidResponseError(operation, "dataset identifier is invalid") from error


def _offset(page: PageRequest, operation: str) -> int:
    if page.cursor is None:
        return 0
    if not page.cursor.isdecimal():
        raise CatalogInvalidResponseError(operation, "catalog page cursor is invalid")
    return int(page.cursor)


def _next_cursor(offset: int, returned: int, total: int) -> str | None:
    next_offset = offset + returned
    return str(next_offset) if returned > 0 and next_offset < total else None


def _mapping(value: object, operation: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise CatalogInvalidResponseError(operation, "catalog response must be an object")
    return cast(Mapping[str, object], value)


def _optional_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        return {}
    return cast(Mapping[str, object], value)


def _sequence(value: object, operation: str) -> Sequence[object]:
    if not isinstance(value, list | tuple):
        raise CatalogInvalidResponseError(operation, "catalog response must contain a list")
    return cast(Sequence[object], value)


def _required_string(value: Mapping[str, object], key: str, operation: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise CatalogInvalidResponseError(operation, f"catalog response is missing {key}")
    return result


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _integer(value: object, *, default: int) -> int:
    return (
        value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default
    )


def _names_from_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item.strip())


def _governance(root: Mapping[str, object]) -> GovernanceMetadata:
    owners: list[str] = []
    ownership = _optional_mapping(root.get("ownership"))
    raw_owners = ownership.get("owners")
    if isinstance(raw_owners, list):
        for raw_owner in raw_owners:
            owner = _optional_mapping(_optional_mapping(raw_owner).get("owner"))
            properties = _optional_mapping(owner.get("properties"))
            name = _optional_string(properties.get("displayName")) or _optional_string(
                properties.get("username")
            )
            if name is not None:
                owners.append(name)
    tags = _association_names(root.get("globalTags"), "tags", "tag")
    terms = _association_names(root.get("glossaryTerms"), "terms", "term")
    domains = _association_names(root.get("domains"), "domains", "domain")
    return GovernanceMetadata(
        owners=tuple(owners),
        tags=tags,
        glossary_terms=terms,
        domain=domains[0] if domains else None,
    )


def _association_names(value: object, collection_key: str, entity_key: str) -> tuple[str, ...]:
    container = _optional_mapping(value)
    raw_items = container.get(collection_key)
    if not isinstance(raw_items, list):
        return ()
    names: list[str] = []
    for raw_item in raw_items:
        entity = _optional_mapping(_optional_mapping(raw_item).get(entity_key))
        properties = _optional_mapping(entity.get("properties"))
        name = _optional_string(properties.get("name")) or _optional_string(entity.get("name"))
        if name is not None:
            names.append(name)
    return tuple(names)


def _catalog_field(
    dataset: PhysicalDatasetRef,
    raw_field: Mapping[str, object],
    operation: str,
) -> CatalogField:
    field_path_text = _required_string(raw_field, "fieldPath", operation)
    field_path = tuple(field_path_text.split("."))
    tags = _association_names(raw_field.get("globalTags") or raw_field.get("tags"), "tags", "tag")
    terms = _association_names(raw_field.get("glossaryTerms"), "terms", "term")
    nullable = raw_field.get("nullable")
    is_key = raw_field.get("isPartOfKey")
    return CatalogField(
        id=PhysicalFieldRef(".".join((dataset.root, *field_path))),
        dataset=dataset,
        field_path=field_path,
        native_type=_optional_string(raw_field.get("nativeDataType")),
        description=_optional_string(raw_field.get("description")),
        nullable=nullable if isinstance(nullable, bool) else None,
        is_part_of_key=is_key if isinstance(is_key, bool) else None,
        tags=tags,
        glossary_terms=terms,
    )


def _unavailable_evidence(reason_code: str) -> CatalogEvidencePage[EvidenceT]:
    return CatalogEvidencePage(
        status=EvidenceStatus.UNAVAILABLE,
        page=CatalogPage(items=()),
        reason_code=reason_code,
    )
