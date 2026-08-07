"""Adversarial tests for bounded DataHub inventory scrolling."""

from __future__ import annotations

import copy
import hashlib
import json
import traceback
from dataclasses import dataclass, field

import pytest

from schemabridge.adapters.catalog.datahub_scroll import (
    DataHubCatalogSourceConfig,
    DataHubCatalogSourceError,
    DataHubGraphQLCatalogSource,
    DataHubGraphQLRequest,
    DataHubGraphQLResponse,
)
from schemabridge.application.ports.catalog_inventory import CatalogInventoryErrorCode
from schemabridge.domain.catalog_inventory import (
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogConnectionRoute,
    CatalogConnectionStatus,
    CatalogRefreshFailureCode,
    CatalogRefreshMode,
)
from schemabridge.domain.semantic_registry import PhysicalValueType

CUSTOMERS_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge.crm.customers,PROD)"
ORDERS_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge.sales.orders,PROD)"
PRODUCTS_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge.commerce.products,PROD)"


@dataclass
class StubTransport:
    responses: list[DataHubGraphQLResponse | Exception]
    requests: list[DataHubGraphQLRequest] = field(default_factory=list)

    def execute(self, request: DataHubGraphQLRequest) -> DataHubGraphQLResponse:
        self.requests.append(request)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _config(**updates: object) -> DataHubCatalogSourceConfig:
    payload: dict[str, object] = {
        "server": "http://127.0.0.1:8080",
        "token": "synthetic-test-token",
        "credential_binding_ref": "binding_datahub_primary",
    }
    payload.update(updates)
    return DataHubCatalogSourceConfig(**payload)  # type: ignore[arg-type]


def _route(**updates: object) -> CatalogConnectionRoute:
    payload: dict[str, object] = {
        "workspace_id": "workspace_alpha",
        "connection_id": CatalogConnectionId("connection_primary"),
        "kind": CatalogConnectionKind.DATAHUB_GRAPHQL,
        "environment": "PROD",
        "catalog_scope": "schemabridge",
        "status": CatalogConnectionStatus.ENABLED,
    }
    payload.update(updates)
    return CatalogConnectionRoute.model_validate(payload)


def _field(
    name: str,
    *,
    description: str | None = None,
    native_type: str = "VARCHAR(12)",
    nullable: bool = False,
    is_key: bool = False,
) -> dict[str, object]:
    return {
        "fieldPath": name,
        "nativeDataType": native_type,
        "description": description,
        "nullable": nullable,
        "isPartOfKey": is_key,
        "globalTags": {
            "tags": [
                {
                    "tag": {
                        "urn": "urn:li:tag:Synthetic",
                        "properties": {"name": "Synthetic"},
                    }
                }
            ]
        },
        "glossaryTerms": {
            "terms": [
                {
                    "term": {
                        "urn": "urn:li:glossaryTerm:Customer",
                        "properties": {"name": "Customer"},
                    }
                }
            ]
        },
    }


def _entity(
    urn: str,
    *,
    name: str | None = None,
    description: str | None = "Synthetic catalog metadata.",
    fields: list[dict[str, object]] | None = None,
    platform_instance: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "urn": urn,
        "name": name,
        "platform": {
            "urn": "urn:li:dataPlatform:postgres",
            "name": "PostgreSQL",
        },
        "properties": {
            "name": name,
            "description": description,
        },
        "schemaMetadata": {"fields": fields or []},
    }
    if platform_instance is not None:
        result["platformInstance"] = {"instanceId": platform_instance}
    return result


def _payload(
    entities: list[dict[str, object]],
    *,
    total: int,
    next_scroll_id: str | None,
    count: int | None = None,
) -> bytes:
    return json.dumps(
        {
            "data": {
                "scrollAcrossEntities": {
                    "nextScrollId": next_scroll_id,
                    "count": len(entities) if count is None else count,
                    "total": total,
                    "searchResults": [{"entity": entity} for entity in entities],
                }
            }
        },
        separators=(",", ":"),
    ).encode()


def _response(
    entities: list[dict[str, object]],
    *,
    total: int,
    next_scroll_id: str | None,
    count: int | None = None,
    status: int = 200,
) -> DataHubGraphQLResponse:
    return DataHubGraphQLResponse(
        status_code=status,
        body=_payload(
            entities,
            total=total,
            next_scroll_id=next_scroll_id,
            count=count,
        ),
    )


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _raised(
    source: DataHubGraphQLCatalogSource,
    *,
    checkpoint: str | None = None,
    route: CatalogConnectionRoute | None = None,
    mode: CatalogRefreshMode = CatalogRefreshMode.FULL,
    page_size: int = 2,
) -> DataHubCatalogSourceError:
    with pytest.raises(DataHubCatalogSourceError) as raised:
        source.read_page(
            route or _route(),
            mode=mode,
            checkpoint=checkpoint,
            page_size=page_size,
        )
    return raised.value


def test_scroll_request_is_exact_bounded_and_translates_complete_asset_metadata() -> None:
    customer_id = _field(
        "customer_id",
        description="  Stable   customer identifier.  ",
        is_key=True,
    )
    registration_date = _field(
        "registration_date",
        native_type="DATE",
        nullable=True,
    )
    transport = StubTransport(
        [
            _response(
                [
                    _entity(
                        CUSTOMERS_URN,
                        name="Customers",
                        fields=[registration_date, customer_id],
                    )
                ],
                total=2,
                next_scroll_id="scroll-page-2",
            )
        ]
    )
    config = _config(timeout_seconds=7.5, max_response_bytes=65_536)
    source = DataHubGraphQLCatalogSource(config=config, transport=transport)

    page = source.read_page(
        _route(),
        mode=CatalogRefreshMode.FULL,
        checkpoint=None,
        page_size=1,
    )

    assert page.sequence == 1
    assert page.source_complete is False
    assert page.next_checkpoint is not None
    assert len(page.changes) == 1
    asset = page.changes[0].asset
    assert asset is not None
    assert asset.asset_id.root == CUSTOMERS_URN
    assert asset.qualified_name == "crm.customers"
    assert asset.display_name == "Customers"
    assert asset.platform == "postgres"
    assert asset.environment == "PROD"
    assert asset.database_name == "schemabridge"
    assert asset.schema_name == "crm"
    assert tuple(item.field_path for item in asset.fields) == (
        ("customer_id",),
        ("registration_date",),
    )
    assert asset.fields[0].description == "Stable customer identifier."
    assert asset.fields[0].normalized_type is PhysicalValueType.STRING
    assert asset.fields[1].normalized_type is PhysicalValueType.DATE
    assert asset.fields[0].tags == ("Synthetic",)
    assert asset.fields[0].glossary_terms == ("Customer",)

    expected_field_payload = {
        "field_path": ["customer_id"],
        "native_type": "VARCHAR(12)",
        "normalized_type": "string",
        "description": "Stable customer identifier.",
        "nullable": False,
        "is_part_of_key": True,
        "tags": ["Synthetic"],
        "glossary_terms": ["Customer"],
    }
    assert asset.fields[0].metadata_fingerprint == _fingerprint(expected_field_payload)
    expected_asset_payload = {
        "asset_id": CUSTOMERS_URN,
        "qualified_name": "crm.customers",
        "display_name": "Customers",
        "platform": "postgres",
        "environment": "PROD",
        "database_name": "schemabridge",
        "schema_name": "crm",
        "description": "Synthetic catalog metadata.",
    }
    assert asset.metadata_fingerprint == _fingerprint(expected_asset_payload)

    request = transport.requests[0]
    body = json.loads(request.body)
    query = body["query"]
    scroll_input = body["variables"]["input"]
    assert request.endpoint == "http://127.0.0.1:8080/api/graphql"
    assert request.timeout_seconds == 7.5
    assert request.max_response_bytes == 65_536
    assert scroll_input["types"] == ["DATASET"]
    assert scroll_input["query"] == "*"
    assert scroll_input["count"] == 1
    assert "scrollId" not in scroll_input
    assert scroll_input["orFilters"] == [
        {
            "and": [
                {
                    "field": "platform",
                    "values": ["urn:li:dataPlatform:postgres"],
                    "condition": "EQUAL",
                    "negated": False,
                },
                {
                    "field": "origin",
                    "values": ["PROD"],
                    "condition": "EQUAL",
                    "negated": False,
                },
                {
                    "field": "urn",
                    "values": ["urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge."],
                    "condition": "START_WITH",
                    "negated": False,
                },
            ]
        }
    ]
    assert scroll_input["searchFlags"] == {
        "skipHighlighting": True,
        "skipAggregates": True,
        "includeSoftDeleted": False,
    }
    assert scroll_input["sortInput"] == {
        "sortCriteria": [{"field": "urn", "sortOrder": "ASCENDING"}]
    }
    forbidden = ("offset", "mutation", "sample", "row", "profile")
    assert all(term not in query.casefold() for term in forbidden)
    assert config.token.encode() not in request.body


def test_checkpoint_advances_sequence_scroll_and_exact_total_to_completion() -> None:
    transport = StubTransport(
        [
            _response(
                [_entity(CUSTOMERS_URN, name="Customers")],
                total=2,
                next_scroll_id="scroll-page-2",
            ),
            _response(
                [_entity(ORDERS_URN, name="Orders")],
                total=2,
                next_scroll_id="unused-final-scroll",
            ),
        ]
    )
    source = DataHubGraphQLCatalogSource(_config(), transport)

    first = source.read_page(
        _route(),
        mode=CatalogRefreshMode.FULL,
        checkpoint=None,
        page_size=1,
    )
    second = source.read_page(
        _route(),
        mode=CatalogRefreshMode.FULL,
        checkpoint=first.next_checkpoint,
        page_size=1,
    )

    assert second.sequence == 2
    assert second.source_complete is True
    assert second.next_checkpoint is None
    assert second.changes[0].asset is not None
    assert second.changes[0].asset.asset_id.root == ORDERS_URN
    second_body = json.loads(transport.requests[1].body)
    assert second_body["variables"]["input"]["scrollId"] == "scroll-page-2"


def test_scroll_filter_is_bound_to_each_catalog_scope_before_asset_validation() -> None:
    first_transport = StubTransport(
        [_response([_entity(CUSTOMERS_URN)], total=1, next_scroll_id=None)]
    )
    second_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,warehouse_two.crm.customers,PROD)"
    second_transport = StubTransport(
        [_response([_entity(second_urn)], total=1, next_scroll_id=None)]
    )

    DataHubGraphQLCatalogSource(_config(), first_transport).read_page(
        _route(),
        mode=CatalogRefreshMode.FULL,
        checkpoint=None,
        page_size=1,
    )
    DataHubGraphQLCatalogSource(_config(), second_transport).read_page(
        _route(
            connection_id="connection_secondary",
            catalog_scope="warehouse_two",
        ),
        mode=CatalogRefreshMode.FULL,
        checkpoint=None,
        page_size=1,
    )

    first_input = json.loads(first_transport.requests[0].body)["variables"]["input"]
    second_input = json.loads(second_transport.requests[0].body)["variables"]["input"]
    assert first_input["orFilters"][0]["and"][-1] == {
        "field": "urn",
        "values": ["urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge."],
        "condition": "START_WITH",
        "negated": False,
    }
    assert second_input["orFilters"][0]["and"][-1] == {
        "field": "urn",
        "values": ["urn:li:dataset:(urn:li:dataPlatform:postgres,warehouse_two."],
        "condition": "START_WITH",
        "negated": False,
    }


def test_explicit_platform_instance_is_filtered_and_revalidated_per_asset() -> None:
    transport = StubTransport(
        [
            _response(
                [_entity(CUSTOMERS_URN, platform_instance="warehouse-primary")],
                total=1,
                next_scroll_id=None,
            )
        ]
    )
    source = DataHubGraphQLCatalogSource(_config(), transport)
    route = _route(platform_instance="warehouse-primary")

    page = source.read_page(
        route,
        mode=CatalogRefreshMode.FULL,
        checkpoint=None,
        page_size=1,
    )

    assert page.source_complete
    filters = json.loads(transport.requests[0].body)["variables"]["input"]["orFilters"][0]["and"]
    assert {
        "field": "platformInstance",
        "values": ["warehouse-primary"],
        "condition": "EQUAL",
        "negated": False,
    } in filters

    mismatched = DataHubGraphQLCatalogSource(
        _config(),
        StubTransport(
            [
                _response(
                    [_entity(CUSTOMERS_URN, platform_instance="warehouse-secondary")],
                    total=1,
                    next_scroll_id=None,
                )
            ]
        ),
    )
    error = _raised(mismatched, route=route, page_size=1)
    assert error.failure_code is CatalogRefreshFailureCode.SOURCE_ASSET_MALFORMED


def test_empty_catalog_is_one_terminal_page_without_a_checkpoint() -> None:
    transport = StubTransport([_response([], total=0, next_scroll_id=None)])
    page = DataHubGraphQLCatalogSource(_config(), transport).read_page(
        _route(),
        mode=CatalogRefreshMode.FULL,
        checkpoint=None,
        page_size=50,
    )

    assert page.sequence == 1
    assert page.changes == ()
    assert page.source_complete
    assert page.next_checkpoint is None


def test_terminal_short_page_accepts_datahub_requested_width_count() -> None:
    transport = StubTransport(
        [
            _response(
                [_entity(CUSTOMERS_URN)],
                total=1,
                next_scroll_id=None,
                count=5,
            )
        ]
    )

    page = DataHubGraphQLCatalogSource(_config(), transport).read_page(
        _route(),
        mode=CatalogRefreshMode.FULL,
        checkpoint=None,
        page_size=5,
    )

    assert len(page.changes) == 1
    assert page.source_complete


def test_config_and_transport_representations_never_include_the_bearer() -> None:
    config = _config(token="super-secret-catalog-token")
    request = DataHubGraphQLRequest(
        endpoint="https://datahub.example.test/api/graphql",
        token="super-secret-catalog-token",
        body=b"{}",
        timeout_seconds=1,
        max_response_bytes=1024,
    )

    assert "super-secret-catalog-token" not in repr(config)
    assert "super-secret-catalog-token" not in repr(request)
    assert (
        DataHubGraphQLCatalogSource(
            config,
            StubTransport([_response([], total=0, next_scroll_id=None)]),
        ).source_label
        == "live:datahub-graphql-scroll-v3"
    )


def test_config_rejects_header_injection_in_the_bearer() -> None:
    with pytest.raises(ValueError, match="credential"):
        _config(token="valid-prefix\r\nX-Forged: yes")


def test_config_binding_reference_matches_durable_route_contract() -> None:
    config = _config(credential_binding_ref="vault:datahub.primary")
    assert config.credential_binding_ref == "vault:datahub.primary"

    with pytest.raises(ValueError, match="credential binding"):
        _config(credential_binding_ref="1binding")


@pytest.mark.parametrize(
    "server",
    [
        "http://datahub.example.test:8080",
        "https://user:secret@datahub.example.test",
        "https://datahub.example.test/path",
        "https://datahub.example.test?token=secret",
        "ftp://datahub.example.test",
    ],
)
def test_config_rejects_insecure_or_ambiguous_server_urls(server: str) -> None:
    with pytest.raises(ValueError, match="server URL"):
        _config(server=server)


def test_delta_is_explicitly_unsupported_without_transport_io() -> None:
    transport = StubTransport([])
    error = _raised(
        DataHubGraphQLCatalogSource(_config(), transport),
        mode=CatalogRefreshMode.DELTA,
    )

    assert error.code is CatalogInventoryErrorCode.DELTA_UNSUPPORTED
    assert error.failure_code is CatalogRefreshFailureCode.DELTA_UNSUPPORTED
    assert transport.requests == []


@pytest.mark.parametrize(
    ("route", "expected_code", "expected_failure"),
    [
        (
            _route(kind=CatalogConnectionKind.SYNTHETIC),
            CatalogInventoryErrorCode.RESOURCE_UNAVAILABLE,
            CatalogRefreshFailureCode.SOURCE_INVALID_RESPONSE,
        ),
        (
            _route(status=CatalogConnectionStatus.DISABLED),
            CatalogInventoryErrorCode.CONNECTION_DISABLED,
            CatalogRefreshFailureCode.CONNECTION_DISABLED,
        ),
    ],
)
def test_wrong_or_disabled_routes_fail_before_transport(
    route: CatalogConnectionRoute,
    expected_code: CatalogInventoryErrorCode,
    expected_failure: CatalogRefreshFailureCode,
) -> None:
    transport = StubTransport([])
    error = _raised(DataHubGraphQLCatalogSource(_config(), transport), route=route)

    assert error.code is expected_code
    assert error.failure_code is expected_failure
    assert transport.requests == []


@pytest.mark.parametrize(
    "entity",
    [
        _entity("urn:li:dataset:(urn:li:dataPlatform:postgres,another.crm.customers,PROD)"),
        _entity("urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge.crm.customers,DEV)"),
        _entity("urn:li:dataset:(urn:li:dataPlatform:mysql,schemabridge.crm.customers,PROD)"),
        {
            **_entity(CUSTOMERS_URN),
            "platform": {
                "urn": "urn:li:dataPlatform:mysql",
                "name": "MySQL",
            },
        },
        {
            **_entity(CUSTOMERS_URN),
            "sampleValues": ["forbidden"],
        },
    ],
)
def test_every_asset_is_revalidated_against_platform_origin_and_catalog_scope(
    entity: dict[str, object],
) -> None:
    source = DataHubGraphQLCatalogSource(
        _config(),
        StubTransport([_response([entity], total=1, next_scroll_id=None)]),
    )

    error = _raised(source, page_size=1)

    assert error.code is CatalogInventoryErrorCode.INVALID_RESPONSE
    assert error.failure_code is CatalogRefreshFailureCode.SOURCE_ASSET_MALFORMED
    assert "another" not in str(error)
    assert "mysql" not in str(error)


def test_duplicate_or_excessive_fields_fail_the_whole_page_without_omission() -> None:
    duplicate = _field("customer_id")
    duplicate_entity = _entity(
        CUSTOMERS_URN,
        fields=[duplicate, copy.deepcopy(duplicate)],
    )
    source = DataHubGraphQLCatalogSource(
        _config(),
        StubTransport([_response([duplicate_entity], total=1, next_scroll_id=None)]),
    )

    error = _raised(source, page_size=1)

    assert error.failure_code is CatalogRefreshFailureCode.SOURCE_ASSET_MALFORMED


def test_repeated_page_is_distinct_from_a_stalled_scroll_cursor() -> None:
    transport = StubTransport(
        [
            _response(
                [_entity(CUSTOMERS_URN)],
                total=2,
                next_scroll_id="scroll-page-2",
            ),
            _response(
                [_entity(CUSTOMERS_URN)],
                total=2,
                next_scroll_id="scroll-page-3",
            ),
        ]
    )
    source = DataHubGraphQLCatalogSource(_config(), transport)
    first = source.read_page(
        _route(),
        mode=CatalogRefreshMode.FULL,
        checkpoint=None,
        page_size=1,
    )

    error = _raised(source, checkpoint=first.next_checkpoint, page_size=1)

    assert error.failure_code is CatalogRefreshFailureCode.SOURCE_PAGE_REPEATED


@pytest.mark.parametrize("case", ["same_cursor", "empty", "unordered"])
def test_missing_cursor_progress_and_order_drift_fail_closed(case: str) -> None:
    if case == "unordered":
        source = DataHubGraphQLCatalogSource(
            _config(),
            StubTransport(
                [
                    _response(
                        [_entity(ORDERS_URN), _entity(CUSTOMERS_URN)],
                        total=3,
                        next_scroll_id="scroll-page-2",
                    )
                ]
            ),
        )
        error = _raised(source, page_size=2)
    elif case == "empty":
        source = DataHubGraphQLCatalogSource(
            _config(),
            StubTransport([_response([], total=1, next_scroll_id="scroll-page-2")]),
        )
        error = _raised(source, page_size=1)
    else:
        transport = StubTransport(
            [
                _response(
                    [_entity(CUSTOMERS_URN)],
                    total=3,
                    next_scroll_id="scroll-page-2",
                ),
                _response(
                    [_entity(ORDERS_URN)],
                    total=3,
                    next_scroll_id="scroll-page-2",
                ),
            ]
        )
        source = DataHubGraphQLCatalogSource(_config(), transport)
        first = source.read_page(
            _route(),
            mode=CatalogRefreshMode.FULL,
            checkpoint=None,
            page_size=1,
        )
        error = _raised(source, checkpoint=first.next_checkpoint, page_size=1)

    assert error.failure_code is CatalogRefreshFailureCode.SOURCE_CURSOR_STALLED


def test_checkpoint_is_route_bound_and_malformed_checkpoint_does_no_io() -> None:
    transport = StubTransport(
        [
            _response(
                [_entity(CUSTOMERS_URN)],
                total=2,
                next_scroll_id="scroll-page-2",
            )
        ]
    )
    source = DataHubGraphQLCatalogSource(_config(), transport)
    first = source.read_page(
        _route(),
        mode=CatalogRefreshMode.FULL,
        checkpoint=None,
        page_size=1,
    )

    cross_connection = _raised(
        source,
        checkpoint=first.next_checkpoint,
        route=_route(connection_id=CatalogConnectionId("connection_secondary")),
        page_size=1,
    )
    cross_platform_instance = _raised(
        source,
        checkpoint=first.next_checkpoint,
        route=_route(platform_instance="warehouse-secondary"),
        page_size=1,
    )
    malformed = _raised(source, checkpoint='{"v":1}', page_size=1)

    assert cross_connection.failure_code is CatalogRefreshFailureCode.SOURCE_INVALID_RESPONSE
    assert cross_platform_instance.failure_code is CatalogRefreshFailureCode.SOURCE_INVALID_RESPONSE
    assert malformed.failure_code is CatalogRefreshFailureCode.SOURCE_INVALID_RESPONSE
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    ("result", "expected_code", "expected_failure"),
    [
        (
            DataHubGraphQLResponse(status_code=403, body=b"forbidden secret"),
            CatalogInventoryErrorCode.RESOURCE_UNAVAILABLE,
            CatalogRefreshFailureCode.SOURCE_PERMISSION_DENIED,
        ),
        (
            DataHubGraphQLResponse(status_code=503, body=b"vendor outage secret"),
            CatalogInventoryErrorCode.UNAVAILABLE,
            CatalogRefreshFailureCode.SOURCE_UNAVAILABLE,
        ),
        (
            TimeoutError("socket includes a secret token"),
            CatalogInventoryErrorCode.UNAVAILABLE,
            CatalogRefreshFailureCode.SOURCE_UNAVAILABLE,
        ),
        (
            DataHubGraphQLResponse(status_code=200, body=b"not-json secret"),
            CatalogInventoryErrorCode.INVALID_RESPONSE,
            CatalogRefreshFailureCode.SOURCE_INVALID_RESPONSE,
        ),
        (
            DataHubGraphQLResponse(
                status_code=200,
                body=json.dumps(
                    {
                        "errors": [
                            {
                                "message": "secret policy detail",
                                "extensions": {"code": "FORBIDDEN"},
                            }
                        ]
                    }
                ).encode(),
            ),
            CatalogInventoryErrorCode.RESOURCE_UNAVAILABLE,
            CatalogRefreshFailureCode.SOURCE_PERMISSION_DENIED,
        ),
    ],
)
def test_transport_and_graphql_failures_are_typed_and_sanitized(
    result: DataHubGraphQLResponse | Exception,
    expected_code: CatalogInventoryErrorCode,
    expected_failure: CatalogRefreshFailureCode,
) -> None:
    source = DataHubGraphQLCatalogSource(_config(), StubTransport([result]))

    error = _raised(source)

    assert error.code is expected_code
    assert error.failure_code is expected_failure
    assert "secret" not in str(error).casefold()
    assert "token" not in str(error).casefold()
    assert error.__cause__ is None
    formatted = "".join(traceback.format_exception(error)).casefold()
    assert "secret" not in formatted
    assert "token" not in formatted


def test_oversized_and_duplicate_json_responses_fail_closed() -> None:
    oversized_source = DataHubGraphQLCatalogSource(
        _config(max_response_bytes=1024),
        StubTransport([DataHubGraphQLResponse(status_code=200, body=b"x" * 1025)]),
    )
    duplicate_source = DataHubGraphQLCatalogSource(
        _config(),
        StubTransport(
            [
                DataHubGraphQLResponse(
                    status_code=200,
                    body=b'{"data":{},"data":{}}',
                )
            ]
        ),
    )

    oversized = _raised(oversized_source)
    duplicate = _raised(duplicate_source)

    assert oversized.failure_code is CatalogRefreshFailureCode.SOURCE_RESPONSE_TOO_LARGE
    assert duplicate.failure_code is CatalogRefreshFailureCode.SOURCE_INVALID_RESPONSE


def test_count_total_and_page_size_contracts_are_not_silently_repaired() -> None:
    count_mismatch = DataHubGraphQLCatalogSource(
        _config(),
        StubTransport(
            [
                _response(
                    [_entity(CUSTOMERS_URN)],
                    total=1,
                    next_scroll_id=None,
                    count=0,
                )
            ]
        ),
    )
    error = _raised(count_mismatch, page_size=1)

    assert error.failure_code is CatalogRefreshFailureCode.SOURCE_INVALID_RESPONSE
    with pytest.raises(ValueError, match="between 1 and 50"):
        count_mismatch.read_page(
            _route(),
            mode=CatalogRefreshMode.FULL,
            checkpoint=None,
            page_size=51,
        )


def test_field_change_updates_field_and_page_but_not_asset_fingerprint() -> None:
    first_transport = StubTransport(
        [
            _response(
                [
                    _entity(
                        CUSTOMERS_URN,
                        fields=[_field("customer_id", description="Version one")],
                    )
                ],
                total=1,
                next_scroll_id=None,
            )
        ]
    )
    second_transport = StubTransport(
        [
            _response(
                [
                    _entity(
                        CUSTOMERS_URN,
                        fields=[_field("customer_id", description="Version two")],
                    )
                ],
                total=1,
                next_scroll_id=None,
            )
        ]
    )
    first_page = DataHubGraphQLCatalogSource(_config(), first_transport).read_page(
        _route(),
        mode=CatalogRefreshMode.FULL,
        checkpoint=None,
        page_size=1,
    )
    second_page = DataHubGraphQLCatalogSource(_config(), second_transport).read_page(
        _route(),
        mode=CatalogRefreshMode.FULL,
        checkpoint=None,
        page_size=1,
    )
    first_asset = first_page.changes[0].asset
    second_asset = second_page.changes[0].asset
    assert first_asset is not None
    assert second_asset is not None

    assert first_asset.fields[0].metadata_fingerprint != second_asset.fields[0].metadata_fingerprint
    assert first_asset.metadata_fingerprint == second_asset.metadata_fingerprint
    assert first_page.page_fingerprint != second_page.page_fingerprint


def test_order_can_progress_across_three_distinct_pages() -> None:
    transport = StubTransport(
        [
            _response(
                [_entity(PRODUCTS_URN)],
                total=3,
                next_scroll_id="scroll-page-2",
            ),
            _response(
                [_entity(CUSTOMERS_URN)],
                total=3,
                next_scroll_id="scroll-page-3",
            ),
            _response(
                [_entity(ORDERS_URN)],
                total=3,
                next_scroll_id=None,
            ),
        ]
    )
    source = DataHubGraphQLCatalogSource(_config(), transport)
    first = source.read_page(
        _route(),
        mode=CatalogRefreshMode.FULL,
        checkpoint=None,
        page_size=1,
    )
    second = source.read_page(
        _route(),
        mode=CatalogRefreshMode.FULL,
        checkpoint=first.next_checkpoint,
        page_size=1,
    )
    third = source.read_page(
        _route(),
        mode=CatalogRefreshMode.FULL,
        checkpoint=second.next_checkpoint,
        page_size=1,
    )

    assert (first.sequence, second.sequence, third.sequence) == (1, 2, 3)
    assert third.source_complete
