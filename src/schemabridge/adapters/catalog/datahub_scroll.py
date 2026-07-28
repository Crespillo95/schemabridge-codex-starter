"""Bounded DataHub GraphQL scrolling for the M25 catalog indexer."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Never, Protocol, cast

from pydantic import ValidationError

from schemabridge.application.ports.catalog_inventory import (
    CatalogInventoryError,
    CatalogInventoryErrorCode,
)
from schemabridge.domain.catalog_inventory import (
    MAX_INVENTORY_CURSOR_BYTES,
    MAX_INVENTORY_PAGE_SIZE,
    MAX_SOURCE_FIELDS_PER_ASSET,
    MAX_SOURCE_FIELDS_PER_PAGE,
    CatalogAssetId,
    CatalogConnectionKind,
    CatalogConnectionRoute,
    CatalogConnectionStatus,
    CatalogRefreshFailureCode,
    CatalogRefreshMode,
    CatalogSourceAsset,
    CatalogSourceChange,
    CatalogSourceChangeKind,
    CatalogSourceField,
    CatalogSourcePage,
)
from schemabridge.domain.connectors import normalize_postgres_native_type
from schemabridge.domain.semantic_registry import PhysicalValueType

_DEFAULT_TIMEOUT_SECONDS = 15.0
_DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_CONFIGURED_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_SCROLL_ID_CHARS = 512
_CHECKPOINT_VERSION = 3
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DATASET_URN = re.compile(
    r"^urn:li:dataset:\(urn:li:dataPlatform:(?P<platform>[^,()\x00-\x20]+),"
    r"(?P<name>[^,()\x00-\x1f\x7f]+),(?P<environment>[^,()\x00-\x20]+)\)$"
)
_SAFE_PLATFORM = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")
_CREDENTIAL_BINDING_REF = re.compile(r"^[a-z][a-z0-9._:-]{2,199}$")
_PRINTABLE = re.compile(r"^[^\x00-\x1f\x7f]+$")
_PERMISSION_CODES = frozenset({"FORBIDDEN", "UNAUTHENTICATED", "UNAUTHORIZED"})

_SCROLL_QUERY = """\
query SchemaBridgeCatalogScroll($input: ScrollAcrossEntitiesInput!) {
  scrollAcrossEntities(input: $input) {
    nextScrollId
    count
    total
    searchResults {
      entity {
        urn
        ... on Dataset {
          name
          platform {
            urn
            name
          }
          platformInstance {
            instanceId
          }
          properties {
            name
            description
          }
          schemaMetadata {
            fields {
              fieldPath
              nativeDataType
              description
              nullable
              isPartOfKey
              globalTags {
                tags {
                  tag {
                    urn
                    properties {
                      name
                    }
                  }
                }
              }
              glossaryTerms {
                terms {
                  term {
                    urn
                    properties {
                      name
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


class DataHubCatalogSourceError(CatalogInventoryError):
    """A sanitized source failure plus the exact durable refresh failure code."""

    def __init__(
        self,
        code: CatalogInventoryErrorCode,
        failure_code: CatalogRefreshFailureCode,
        message: str,
    ) -> None:
        self.failure_code = failure_code
        super().__init__(code, message)


@dataclass(frozen=True, slots=True)
class DataHubCatalogSourceConfig:
    """Indexer-only DataHub settings; the bearer is omitted from representations."""

    server: str = field(repr=False)
    token: str = field(repr=False)
    credential_binding_ref: str = field(repr=False)
    platform: str = "postgres"
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        normalized_server = _validated_server(self.server)
        object.__setattr__(self, "server", normalized_server)
        if (
            not self.token
            or self.token.strip() != self.token
            or len(self.token.encode("utf-8")) > 16 * 1024
            or _PRINTABLE.fullmatch(self.token) is None
        ):
            raise ValueError("DataHub catalog credential is invalid")
        if _CREDENTIAL_BINDING_REF.fullmatch(self.credential_binding_ref) is None:
            raise ValueError("DataHub catalog credential binding is invalid")
        if _SAFE_PLATFORM.fullmatch(self.platform) is None:
            raise ValueError("DataHub catalog platform is invalid")
        if isinstance(self.timeout_seconds, bool) or not 0.1 <= self.timeout_seconds <= 30.0:
            raise ValueError("DataHub catalog timeout is outside the supported bound")
        if (
            isinstance(self.max_response_bytes, bool)
            or not 1_024 <= self.max_response_bytes <= _MAX_CONFIGURED_RESPONSE_BYTES
        ):
            raise ValueError("DataHub catalog response bound is invalid")


@dataclass(frozen=True, slots=True)
class DataHubGraphQLRequest:
    """One mutation-free request passed to an injectable bounded transport."""

    endpoint: str = field(repr=False)
    token: str = field(repr=False)
    body: bytes = field(repr=False)
    timeout_seconds: float
    max_response_bytes: int


@dataclass(frozen=True, slots=True)
class DataHubGraphQLResponse:
    """The bounded status/body returned by the transport."""

    status_code: int
    body: bytes = field(repr=False)


class DataHubGraphQLTransport(Protocol):
    """Minimal injected transport; it has no DataHub mutation operation."""

    def execute(self, request: DataHubGraphQLRequest) -> DataHubGraphQLResponse:
        """Execute exactly one bounded GraphQL POST."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        del request, file_pointer, code, message, headers, new_url
        return None


@dataclass(frozen=True, slots=True)
class UrllibDataHubGraphQLTransport:
    """Standard-library HTTPS transport with redirect and byte bounds."""

    def execute(self, request: DataHubGraphQLRequest) -> DataHubGraphQLResponse:
        http_request = urllib.request.Request(
            request.endpoint,
            data=request.body,
            headers={
                "Authorization": f"Bearer {request.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        opener = urllib.request.build_opener(_RejectRedirects())
        try:
            with opener.open(http_request, timeout=request.timeout_seconds) as response:
                status = int(getattr(response, "status", 200))
                body = _read_bounded_response(response, request.max_response_bytes)
        except urllib.error.HTTPError as error:
            status = error.code
            body = _read_bounded_response(error, request.max_response_bytes)
        return DataHubGraphQLResponse(status_code=status, body=body)


@dataclass(frozen=True, slots=True)
class _Checkpoint:
    sequence: int
    scroll_id: str
    seen_count: int
    total: int
    last_urn: str
    previous_page_digest: str


@dataclass(slots=True)
class DataHubGraphQLCatalogSource:
    """Stream complete dataset metadata through stable DataHub scroll pages."""

    config: DataHubCatalogSourceConfig
    transport: DataHubGraphQLTransport = field(
        default_factory=UrllibDataHubGraphQLTransport,
        repr=False,
    )

    @property
    def source_label(self) -> str:
        return "live:datahub-graphql-scroll-v3"

    def read_page(
        self,
        route: CatalogConnectionRoute,
        *,
        mode: CatalogRefreshMode,
        checkpoint: str | None,
        page_size: int,
    ) -> CatalogSourcePage:
        self._validate_route(route)
        if mode is CatalogRefreshMode.DELTA:
            _raise_source(
                CatalogInventoryErrorCode.DELTA_UNSUPPORTED,
                CatalogRefreshFailureCode.DELTA_UNSUPPORTED,
                "configured catalog source does not provide delta discovery",
            )
        if isinstance(page_size, bool) or page_size < 1 or page_size > MAX_INVENTORY_PAGE_SIZE:
            raise ValueError("catalog source page size must be between 1 and 50")

        route_digest = _route_digest(route, self.config.platform)
        state = (
            _decode_checkpoint(checkpoint, expected_route_digest=route_digest)
            if checkpoint is not None
            else None
        )
        variables = {
            "input": _scroll_input(
                platform=self.config.platform,
                environment=route.environment,
                catalog_scope=route.catalog_scope,
                platform_instance=route.platform_instance,
                page_size=page_size,
                scroll_id=state.scroll_id if state is not None else None,
            )
        }
        request_body = json.dumps(
            {"query": _SCROLL_QUERY, "variables": variables},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        request = DataHubGraphQLRequest(
            endpoint=f"{self.config.server}/api/graphql",
            token=self.config.token,
            body=request_body,
            timeout_seconds=self.config.timeout_seconds,
            max_response_bytes=self.config.max_response_bytes,
        )
        response = self._execute(request)
        root = _parse_graphql_response(response, self.config.max_response_bytes)
        return self._page_from_root(
            route=route,
            root=root,
            state=state,
            page_size=page_size,
            route_digest=route_digest,
        )

    def _execute(self, request: DataHubGraphQLRequest) -> DataHubGraphQLResponse:
        try:
            response = self.transport.execute(request)
        except _ResponseTooLarge as error:
            _raise_source(
                CatalogInventoryErrorCode.INVALID_RESPONSE,
                CatalogRefreshFailureCode.SOURCE_RESPONSE_TOO_LARGE,
                "catalog source response exceeds the configured bound",
                cause=error,
            )
        except (TimeoutError, urllib.error.URLError, OSError) as error:
            _raise_source(
                CatalogInventoryErrorCode.UNAVAILABLE,
                CatalogRefreshFailureCode.SOURCE_UNAVAILABLE,
                "catalog source is unavailable",
                cause=error,
            )
        except DataHubCatalogSourceError:
            raise
        except Exception as error:
            _raise_source(
                CatalogInventoryErrorCode.UNAVAILABLE,
                CatalogRefreshFailureCode.SOURCE_UNAVAILABLE,
                "catalog source is unavailable",
                cause=error,
            )
        if response.status_code in {401, 403}:
            _raise_source(
                CatalogInventoryErrorCode.RESOURCE_UNAVAILABLE,
                CatalogRefreshFailureCode.SOURCE_PERMISSION_DENIED,
                "catalog source is unavailable",
            )
        if response.status_code in {408, 425, 429} or response.status_code >= 500:
            _raise_source(
                CatalogInventoryErrorCode.UNAVAILABLE,
                CatalogRefreshFailureCode.SOURCE_UNAVAILABLE,
                "catalog source is unavailable",
            )
        if response.status_code < 200 or response.status_code >= 300:
            _raise_source(
                CatalogInventoryErrorCode.INVALID_RESPONSE,
                CatalogRefreshFailureCode.SOURCE_INVALID_RESPONSE,
                "catalog source returned an invalid response",
            )
        return response

    def _validate_route(self, route: CatalogConnectionRoute) -> None:
        if route.kind is not CatalogConnectionKind.DATAHUB_GRAPHQL:
            _raise_source(
                CatalogInventoryErrorCode.RESOURCE_UNAVAILABLE,
                CatalogRefreshFailureCode.SOURCE_INVALID_RESPONSE,
                "catalog source route is unavailable",
            )
        if route.status is not CatalogConnectionStatus.ENABLED:
            _raise_source(
                CatalogInventoryErrorCode.CONNECTION_DISABLED,
                CatalogRefreshFailureCode.CONNECTION_DISABLED,
                "catalog connection is disabled",
            )

    def _page_from_root(
        self,
        *,
        route: CatalogConnectionRoute,
        root: Mapping[str, object],
        state: _Checkpoint | None,
        page_size: int,
        route_digest: str,
    ) -> CatalogSourcePage:
        _reject_unknown_keys(
            root,
            {"nextScrollId", "count", "total", "searchResults"},
            required={"nextScrollId", "count", "total", "searchResults"},
        )
        raw_results = _required_sequence(root, "searchResults")
        count = _required_nonnegative_integer(root, "count")
        total = _required_nonnegative_integer(root, "total")
        next_scroll_id = _optional_scroll_id(root.get("nextScrollId"))
        # DataHub 1.6 reports the requested scroll width in ``count`` on a
        # short terminal continuation page, while the first empty page reports
        # zero. Never accept fewer declared rows than returned or a width above
        # the caller's bound.
        if count < len(raw_results) or count > page_size or len(raw_results) > page_size:
            _raise_invalid_response()

        seen_before = state.seen_count if state is not None else 0
        if state is not None and total != state.total:
            _raise_invalid_response()
        if total < seen_before + len(raw_results):
            _raise_invalid_response()
        if not raw_results and seen_before < total:
            _raise_stalled()

        assets = tuple(
            _parse_search_result(
                raw_result,
                route=route,
                expected_platform=self.config.platform,
            )
            for raw_result in raw_results
        )
        urns = tuple(asset.asset_id.root for asset in assets)
        if urns != tuple(sorted(set(urns))):
            _raise_stalled()
        page_digest = _fingerprint({"urns": list(urns)})
        if state is not None:
            if page_digest == state.previous_page_digest:
                _raise_source(
                    CatalogInventoryErrorCode.INVALID_RESPONSE,
                    CatalogRefreshFailureCode.SOURCE_PAGE_REPEATED,
                    "catalog source repeated a page",
                )
            if urns and urns[0] <= state.last_urn:
                _raise_stalled()

        seen_after = seen_before + len(assets)
        source_complete = seen_after == total
        sequence = state.sequence if state is not None else 1
        if not source_complete:
            if not assets or next_scroll_id is None:
                _raise_stalled()
            if state is not None and next_scroll_id == state.scroll_id:
                _raise_stalled()
            next_checkpoint = _encode_checkpoint(
                sequence=sequence + 1,
                scroll_id=next_scroll_id,
                seen_count=seen_after,
                total=total,
                last_urn=urns[-1],
                previous_page_digest=page_digest,
                route_digest=route_digest,
            )
        else:
            next_checkpoint = None

        changes = tuple(
            CatalogSourceChange(
                kind=CatalogSourceChangeKind.UPSERT_ASSET,
                asset=asset,
            )
            for asset in assets
        )
        try:
            return CatalogSourcePage.create(
                mode=CatalogRefreshMode.FULL,
                sequence=sequence,
                changes=changes,
                next_checkpoint=next_checkpoint,
                source_complete=source_complete,
            )
        except ValidationError as error:
            _raise_asset_malformed(error)


def _scroll_input(
    *,
    platform: str,
    environment: str,
    catalog_scope: str,
    platform_instance: str | None,
    page_size: int,
    scroll_id: str | None,
) -> dict[str, object]:
    filters: list[dict[str, object]] = [
        {
            "field": "platform",
            "values": [f"urn:li:dataPlatform:{platform}"],
            "condition": "EQUAL",
            "negated": False,
        },
        {
            "field": "origin",
            "values": [environment],
            "condition": "EQUAL",
            "negated": False,
        },
    ]
    if platform_instance is not None:
        filters.append(
            {
                "field": "platformInstance",
                "values": [platform_instance],
                "condition": "EQUAL",
                "negated": False,
            }
        )
    filters.append(
        {
            "field": "urn",
            "values": [f"urn:li:dataset:(urn:li:dataPlatform:{platform},{catalog_scope}."],
            "condition": "START_WITH",
            "negated": False,
        }
    )
    result: dict[str, object] = {
        "types": ["DATASET"],
        "query": "*",
        "keepAlive": "5m",
        "count": page_size,
        "orFilters": [
            {
                "and": filters,
            }
        ],
        "searchFlags": {
            "skipHighlighting": True,
            "skipAggregates": True,
            "includeSoftDeleted": False,
        },
        "sortInput": {
            "sortCriteria": [
                {
                    "field": "urn",
                    "sortOrder": "ASCENDING",
                }
            ]
        },
    }
    if scroll_id is not None:
        result["scrollId"] = scroll_id
    return result


def _parse_graphql_response(
    response: DataHubGraphQLResponse,
    maximum: int,
) -> Mapping[str, object]:
    if len(response.body) > maximum:
        _raise_source(
            CatalogInventoryErrorCode.INVALID_RESPONSE,
            CatalogRefreshFailureCode.SOURCE_RESPONSE_TOO_LARGE,
            "catalog source response exceeds the configured bound",
        )
    try:
        payload = json.loads(
            response.body.decode("utf-8"),
            object_pairs_hook=_unique_json_pairs,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        _raise_source(
            CatalogInventoryErrorCode.INVALID_RESPONSE,
            CatalogRefreshFailureCode.SOURCE_INVALID_RESPONSE,
            "catalog source returned an invalid response",
            cause=error,
        )
    if not isinstance(payload, dict):
        _raise_invalid_response()
    errors = payload.get("errors")
    if errors is not None:
        if _is_permission_error(errors):
            _raise_source(
                CatalogInventoryErrorCode.RESOURCE_UNAVAILABLE,
                CatalogRefreshFailureCode.SOURCE_PERMISSION_DENIED,
                "catalog source is unavailable",
            )
        _raise_invalid_response()
    if not set(payload).issubset({"data", "extensions"}) or "data" not in payload:
        _raise_invalid_response()
    if "extensions" in payload and not isinstance(payload["extensions"], dict):
        _raise_invalid_response()
    data = payload.get("data")
    if not isinstance(data, dict) or set(data) != {"scrollAcrossEntities"}:
        _raise_invalid_response()
    root = data.get("scrollAcrossEntities")
    if not isinstance(root, dict):
        _raise_invalid_response()
    return cast(Mapping[str, object], root)


def _parse_search_result(
    raw_result: object,
    *,
    route: CatalogConnectionRoute,
    expected_platform: str,
) -> CatalogSourceAsset:
    try:
        result = _mapping(raw_result)
        _reject_unknown_keys(result, {"entity"}, required={"entity"})
        entity = _mapping(result.get("entity"))
        _reject_unknown_keys(
            entity,
            {
                "urn",
                "name",
                "platform",
                "platformInstance",
                "properties",
                "schemaMetadata",
            },
            required={"urn", "platform"},
        )
        urn = _required_nonblank_string(entity, "urn", maximum=500)
        qualified_name, schema_name, fallback_display = _parse_dataset_urn(
            urn,
            catalog_scope=route.catalog_scope,
            expected_platform=expected_platform,
            expected_environment=route.environment,
        )
        platform = _mapping(entity.get("platform"))
        _reject_unknown_keys(platform, {"urn", "name"}, required={"urn"})
        expected_platform_urn = f"urn:li:dataPlatform:{expected_platform}"
        if _required_nonblank_string(platform, "urn", maximum=300) != expected_platform_urn:
            raise ValueError("dataset platform is outside the configured route")
        platform_instance = _optional_mapping(
            entity.get("platformInstance"),
            {"instanceId"},
        )
        if route.platform_instance is not None and (
            _required_nonblank_string(
                platform_instance,
                "instanceId",
                maximum=200,
            )
            != route.platform_instance
        ):
            raise ValueError("dataset platform instance is outside the configured route")

        properties = _optional_mapping(entity.get("properties"), {"name", "description"})
        display_name = (
            _optional_text(properties.get("name"), maximum=200)
            or _optional_text(entity.get("name"), maximum=200)
            or fallback_display
        )
        description = _optional_text(properties.get("description"), maximum=4_000)
        fields = _parse_schema_metadata(
            entity.get("schemaMetadata"),
            postgresql=expected_platform == "postgres",
        )
        asset_id = CatalogAssetId(urn)
        asset_payload = {
            "asset_id": asset_id.root,
            "qualified_name": qualified_name,
            "display_name": display_name,
            "platform": expected_platform,
            "environment": route.environment,
            "database_name": route.catalog_scope,
            "schema_name": schema_name,
            "description": description,
        }
        return CatalogSourceAsset(
            asset_id=asset_id,
            qualified_name=qualified_name,
            display_name=display_name,
            platform=expected_platform,
            environment=route.environment,
            database_name=route.catalog_scope,
            schema_name=schema_name,
            description=description,
            fields=fields,
            metadata_fingerprint=_fingerprint(asset_payload),
        )
    except DataHubCatalogSourceError:
        raise
    except (KeyError, TypeError, ValueError, ValidationError) as error:
        _raise_asset_malformed(error)


def _parse_schema_metadata(
    value: object,
    *,
    postgresql: bool,
) -> tuple[CatalogSourceField, ...]:
    if value is None:
        return ()
    schema = _mapping(value)
    _reject_unknown_keys(schema, {"fields"}, required={"fields"})
    raw_fields = _required_sequence(schema, "fields")
    if len(raw_fields) > MAX_SOURCE_FIELDS_PER_ASSET:
        raise ValueError("dataset schema exceeds the per-asset field bound")
    fields = tuple(_parse_field(item, postgresql=postgresql) for item in raw_fields)
    if len(fields) > MAX_SOURCE_FIELDS_PER_PAGE:
        raise ValueError("dataset schema exceeds the per-page field bound")
    return tuple(sorted(fields, key=lambda item: item.field_path))


def _parse_field(value: object, *, postgresql: bool) -> CatalogSourceField:
    raw = _mapping(value)
    _reject_unknown_keys(
        raw,
        {
            "fieldPath",
            "nativeDataType",
            "description",
            "nullable",
            "isPartOfKey",
            "globalTags",
            "glossaryTerms",
        },
        required={"fieldPath"},
    )
    field_path_text = _required_nonblank_string(raw, "fieldPath", maximum=12_800)
    field_path = tuple(field_path_text.split("."))
    if len(field_path) > 64 or any(
        not part or part.strip() != part or len(part) > 200 or _PRINTABLE.fullmatch(part) is None
        for part in field_path
    ):
        raise ValueError("dataset field path is invalid")
    nullable = _optional_boolean(raw.get("nullable"))
    is_part_of_key = _optional_boolean(raw.get("isPartOfKey"))
    tags = _association_names(raw.get("globalTags"), "tags", "tag")
    glossary_terms = _association_names(
        raw.get("glossaryTerms"),
        "terms",
        "term",
    )
    native_type = _optional_text(raw.get("nativeDataType"), maximum=200)
    normalized_type = (
        normalize_postgres_native_type(native_type).normalized_type
        if postgresql
        else PhysicalValueType.UNKNOWN
    )
    payload = {
        "field_path": list(field_path),
        "native_type": native_type,
        "normalized_type": normalized_type.value,
        "description": _optional_text(raw.get("description"), maximum=4_000),
        "nullable": nullable,
        "is_part_of_key": is_part_of_key,
        "tags": list(tags),
        "glossary_terms": list(glossary_terms),
    }
    return CatalogSourceField(
        **payload,
        metadata_fingerprint=_fingerprint(payload),
    )


def _association_names(
    value: object,
    collection_key: str,
    entity_key: str,
) -> tuple[str, ...]:
    if value is None:
        return ()
    container = _mapping(value)
    _reject_unknown_keys(container, {collection_key}, required={collection_key})
    raw_items = _required_sequence(container, collection_key)
    if len(raw_items) > 100:
        raise ValueError("dataset field association exceeds its bound")
    names: list[str] = []
    for item in raw_items:
        association = _mapping(item)
        _reject_unknown_keys(association, {entity_key}, required={entity_key})
        entity = _mapping(association.get(entity_key))
        _reject_unknown_keys(entity, {"urn", "properties"}, required={"urn"})
        _required_nonblank_string(entity, "urn", maximum=500)
        properties = _optional_mapping(entity.get("properties"), {"name"})
        name = _optional_text(properties.get("name"), maximum=200)
        if name is None:
            raise ValueError("dataset field association name is missing")
        names.append(name)
    return tuple(sorted(set(names)))


def _parse_dataset_urn(
    urn: str,
    *,
    catalog_scope: str,
    expected_platform: str,
    expected_environment: str,
) -> tuple[str, str, str]:
    match = _DATASET_URN.fullmatch(urn)
    if (
        match is None
        or match.group("platform") != expected_platform
        or match.group("environment") != expected_environment
    ):
        raise ValueError("dataset URN is outside the configured route")
    physical_name = match.group("name")
    prefix = f"{catalog_scope}."
    if not physical_name.startswith(prefix):
        raise ValueError("dataset URN is outside the configured catalog scope")
    qualified_name = physical_name[len(prefix) :]
    parts = qualified_name.split(".")
    if (
        len(parts) < 2
        or len(qualified_name) > 500
        or any(
            not part
            or part.strip() != part
            or len(part) > 200
            or _PRINTABLE.fullmatch(part) is None
            for part in parts
        )
    ):
        raise ValueError("dataset name is not schema-qualified")
    schema_name = ".".join(parts[:-1])
    if len(schema_name) > 200 or len(parts[-1]) > 200:
        raise ValueError("dataset name exceeds its bound")
    return qualified_name, schema_name, parts[-1]


def _decode_checkpoint(
    value: str,
    *,
    expected_route_digest: str,
) -> _Checkpoint:
    try:
        if (
            not value
            or value.strip() != value
            or len(value.encode("utf-8")) > MAX_INVENTORY_CURSOR_BYTES
            or _PRINTABLE.fullmatch(value) is None
        ):
            raise ValueError("checkpoint is invalid")
        payload = json.loads(value, object_pairs_hook=_unique_json_pairs)
        if not isinstance(payload, dict):
            raise ValueError("checkpoint is invalid")
        if set(payload) != {"c", "n", "p", "q", "r", "t", "u", "v"}:
            raise ValueError("checkpoint is invalid")
        if _canonical_json(payload) != value:
            raise ValueError("checkpoint is not canonical")
        version = payload["v"]
        sequence = payload["q"]
        scroll_id = payload["c"]
        seen_count = payload["n"]
        total = payload["t"]
        last_urn = payload["u"]
        previous_page_digest = payload["p"]
        route_digest = payload["r"]
        if version != _CHECKPOINT_VERSION:
            raise ValueError("checkpoint version is unsupported")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 2
            or isinstance(seen_count, bool)
            or not isinstance(seen_count, int)
            or seen_count < 1
            or isinstance(total, bool)
            or not isinstance(total, int)
            or total < seen_count
        ):
            raise ValueError("checkpoint counters are invalid")
        if (
            not isinstance(scroll_id, str)
            or not scroll_id
            or len(scroll_id) > _MAX_SCROLL_ID_CHARS
            or _PRINTABLE.fullmatch(scroll_id) is None
            or not isinstance(last_urn, str)
            or not last_urn
            or len(last_urn) > 500
            or _PRINTABLE.fullmatch(last_urn) is None
            or not isinstance(previous_page_digest, str)
            or _SHA256.fullmatch(previous_page_digest) is None
            or route_digest != expected_route_digest
        ):
            raise ValueError("checkpoint binding is invalid")
        return _Checkpoint(
            sequence=sequence,
            scroll_id=scroll_id,
            seen_count=seen_count,
            total=total,
            last_urn=last_urn,
            previous_page_digest=previous_page_digest,
        )
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        _raise_source(
            CatalogInventoryErrorCode.INVALID_RESPONSE,
            CatalogRefreshFailureCode.SOURCE_INVALID_RESPONSE,
            "catalog source checkpoint is invalid",
            cause=error,
        )


def _encode_checkpoint(
    *,
    sequence: int,
    scroll_id: str,
    seen_count: int,
    total: int,
    last_urn: str,
    previous_page_digest: str,
    route_digest: str,
) -> str:
    value = _canonical_json(
        {
            "c": scroll_id,
            "n": seen_count,
            "p": previous_page_digest,
            "q": sequence,
            "r": route_digest,
            "t": total,
            "u": last_urn,
            "v": _CHECKPOINT_VERSION,
        }
    )
    if (
        len(value.encode("utf-8")) > MAX_INVENTORY_CURSOR_BYTES
        or _PRINTABLE.fullmatch(value) is None
    ):
        _raise_source(
            CatalogInventoryErrorCode.INVALID_RESPONSE,
            CatalogRefreshFailureCode.SOURCE_CURSOR_STALLED,
            "catalog source cursor cannot be advanced",
        )
    return value


def _route_digest(route: CatalogConnectionRoute, platform: str) -> str:
    return _fingerprint(
        {
            "catalog_identity_fingerprint": route.catalog_identity_fingerprint,
            "catalog_scope": route.catalog_scope,
            "connection_id": route.connection_id.root,
            "environment": route.environment,
            "kind": route.kind.value,
            "platform": platform,
            "platform_instance": route.platform_instance,
            "contract_version": route.contract_version,
            "route_revision": route.route_revision,
            "target_fingerprint": route.target_fingerprint,
            "workspace_id": route.workspace_id,
        }
    )


def _validated_server(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        not value
        or value.strip() != value
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.hostname is None
        or parsed.scheme not in {"http", "https"}
    ):
        raise ValueError("DataHub catalog server URL is invalid")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("DataHub catalog server URL requires HTTPS outside loopback")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("DataHub catalog server URL is invalid") from error
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    authority = f"{host}:{port}" if port is not None else host
    return f"{parsed.scheme}://{authority}"


def _read_bounded_response(response: Any, maximum: int) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError as error:
            raise _ResponseTooLarge from error
        if declared < 0 or declared > maximum:
            raise _ResponseTooLarge
    body = bytes(response.read(maximum + 1))
    if len(body) > maximum:
        raise _ResponseTooLarge
    return body


class _ResponseTooLarge(RuntimeError):
    pass


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return cast(Mapping[str, object], value)


def _optional_mapping(
    value: object,
    allowed_keys: set[str],
) -> Mapping[str, object]:
    if value is None:
        return {}
    result = _mapping(value)
    _reject_unknown_keys(result, allowed_keys)
    return result


def _required_sequence(
    value: Mapping[str, object],
    key: str,
) -> Sequence[object]:
    result = value.get(key)
    if not isinstance(result, list):
        raise ValueError("expected list")
    return cast(Sequence[object], result)


def _required_nonnegative_integer(value: Mapping[str, object], key: str) -> int:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, int) or result < 0:
        _raise_invalid_response()
    return result


def _required_nonblank_string(
    value: Mapping[str, object],
    key: str,
    *,
    maximum: int,
) -> str:
    result = value.get(key)
    if (
        not isinstance(result, str)
        or not result
        or result.strip() != result
        or len(result) > maximum
        or _PRINTABLE.fullmatch(result) is None
    ):
        raise ValueError("expected bounded string")
    return result


def _optional_text(value: object, *, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("expected optional text")
    normalized = " ".join(value.split())
    if not normalized:
        return None
    if len(normalized) > maximum or _PRINTABLE.fullmatch(normalized) is None:
        raise ValueError("optional text exceeds its bound")
    return normalized


def _optional_boolean(value: object) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError("expected optional boolean")
    return value


def _optional_scroll_id(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > _MAX_SCROLL_ID_CHARS
        or _PRINTABLE.fullmatch(value) is None
    ):
        _raise_invalid_response()
    return value


def _reject_unknown_keys(
    value: Mapping[str, object],
    allowed: set[str],
    *,
    required: set[str] | None = None,
) -> None:
    if not set(value).issubset(allowed):
        raise ValueError("response contains unexpected metadata")
    if required is not None and not required.issubset(value):
        raise ValueError("response is missing required metadata")


def _unique_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _is_permission_error(value: object) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for item in value:
        if not isinstance(item, dict):
            continue
        extensions = item.get("extensions")
        if not isinstance(extensions, dict):
            continue
        code = extensions.get("code")
        if isinstance(code, str) and code.upper() in _PERMISSION_CODES:
            return True
    return False


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _raise_invalid_response() -> Never:
    _raise_source(
        CatalogInventoryErrorCode.INVALID_RESPONSE,
        CatalogRefreshFailureCode.SOURCE_INVALID_RESPONSE,
        "catalog source returned an invalid response",
    )


def _raise_stalled() -> Never:
    _raise_source(
        CatalogInventoryErrorCode.INVALID_RESPONSE,
        CatalogRefreshFailureCode.SOURCE_CURSOR_STALLED,
        "catalog source cursor did not advance",
    )


def _raise_asset_malformed(cause: BaseException) -> Never:
    _raise_source(
        CatalogInventoryErrorCode.INVALID_RESPONSE,
        CatalogRefreshFailureCode.SOURCE_ASSET_MALFORMED,
        "catalog source returned malformed asset metadata",
        cause=cause,
    )


def _raise_source(
    code: CatalogInventoryErrorCode,
    failure_code: CatalogRefreshFailureCode,
    message: str,
    *,
    cause: BaseException | None = None,
) -> Never:
    del cause
    error = DataHubCatalogSourceError(code, failure_code, message)
    raise error from None
