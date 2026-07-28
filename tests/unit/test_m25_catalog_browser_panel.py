from __future__ import annotations

import http.client
import json
import os
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest
import scripts.m25_catalog_browser_panel as panel_module
from scripts.m25_catalog_browser_panel import (
    _PANEL_CSS,
    _PANEL_HTML,
    _PANEL_JS,
    MAX_CURSOR_HISTORY,
    CatalogPanelBff,
    CatalogPanelServer,
    CatalogTransport,
    HttpCatalogTransport,
    PanelError,
    UpstreamResponse,
    UpstreamTarget,
    _Pager,
    _validate_upstream_request,
    read_bearer_file,
    require_secret_free_environment,
    validate_loopback_host,
)

AS_OF = "2026-07-23T20:00:00Z"
SMALL_TOKEN = "small-server-held-bearer"
LARGE_TOKEN = "large-server-held-bearer"
DATAHUB_TOKEN = "datahub-server-held-bearer"


def _connection(
    identifier: str = "warehouse-small",
    *,
    stale: bool = False,
) -> dict[str, object]:
    return {
        "connection_id": identifier,
        "display_name": f"Warehouse {identifier}",
        "kind": "synthetic",
        "environment": "acceptance",
        "catalog_scope": "synthetic",
        "status": "enabled",
        "active_generation": 7,
        "asset_count": 5434,
        "field_count": 27170,
        "last_completed_at": AS_OF,
        "stale": stale,
        "credential_binding_ref": "must-never-reach-browser",
    }


def _connection_page(
    *,
    next_cursor: str | None = None,
    stale: bool = False,
    identifier: str = "warehouse-small",
) -> dict[str, object]:
    return {
        "resource": "connections",
        "items": [_connection(identifier, stale=stale)],
        "next_cursor": next_cursor,
        "as_of": AS_OF,
        "stale": stale,
        "unknown_protected_value": "must-never-reach-browser",
    }


def _asset(
    identifier: str = "urn:li:dataset:table_0001",
    *,
    generation: int = 7,
) -> dict[str, object]:
    return {
        "connection_id": "warehouse-small",
        "asset_id": identifier,
        "generation": generation,
        "qualified_name": "acceptance.core.table_0001",
        "display_name": "table_0001",
        "platform": "synthetic",
        "environment": "acceptance",
        "schema_name": "core",
        "description": "Synthetic acceptance table",
        "field_count": 5,
        "metadata_fingerprint": "f" * 64,
        "observed_at": AS_OF,
    }


def _asset_page(
    *,
    generation: int = 7,
    next_cursor: str | None = None,
    stale: bool = False,
) -> dict[str, object]:
    return {
        "resource": "assets",
        "generation": generation,
        "items": [_asset(generation=generation)],
        "next_cursor": next_cursor,
        "as_of": AS_OF,
        "stale": stale,
    }


def _field_page(
    *,
    generation: int = 7,
    next_cursor: str | None = None,
) -> dict[str, object]:
    return {
        "resource": "fields",
        "generation": generation,
        "items": [
            {
                "connection_id": "warehouse-small",
                "asset_id": "urn:li:dataset:table_0001",
                "field_path": ["contract_id"],
                "generation": generation,
                "native_type": "varchar",
                "description": "Contract identifier",
                "nullable": False,
                "is_part_of_key": True,
                "tags": ["identifier"],
                "glossary_terms": ["contract"],
                "metadata_fingerprint": "e" * 64,
                "observed_at": AS_OF,
            }
        ],
        "next_cursor": next_cursor,
        "as_of": AS_OF,
        "stale": False,
    }


def _refresh() -> dict[str, object]:
    return {
        "refresh_id": "refresh-acceptance-001",
        "connection_id": "warehouse-small",
        "mode": "full",
        "status": "requested",
        "base_generation": 7,
        "target_generation": 8,
        "source_page_count": 0,
        "asset_count": 0,
        "field_count": 0,
        "source_complete": False,
        "catalog_fingerprint": None,
        "failure_code": None,
        "requested_at": AS_OF,
        "updated_at": AS_OF,
        "completed_at": None,
    }


@dataclass
class _FakeTransport(CatalogTransport):
    responses: list[UpstreamResponse] = field(default_factory=list)
    responder: Callable[[dict[str, object]], UpstreamResponse] | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    def request(
        self,
        *,
        alias: str,
        method: str,
        path: str,
        query: Mapping[str, str | int],
        body: Mapping[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> UpstreamResponse:
        call: dict[str, object] = {
            "alias": alias,
            "method": method,
            "path": path,
            "query": dict(query),
            "body": None if body is None else dict(body),
            "idempotency_key": idempotency_key,
        }
        self.calls.append(call)
        if self.responder is not None:
            return self.responder(call)
        if not self.responses:
            raise AssertionError("unexpected fake transport call")
        return self.responses.pop(0)


def _connections_action(
    direction: str = "first",
    *,
    page_size: int = 50,
    query: str | None = None,
    status: str | None = None,
) -> dict[str, object]:
    return {
        "action": "connections",
        "direction": direction,
        "page_size": page_size,
        "query": query,
        "status": status,
    }


def _assets_action(
    direction: str = "first",
    *,
    page_size: int = 50,
    query: str | None = None,
) -> dict[str, object]:
    return {
        "action": "assets",
        "direction": direction,
        "page_size": page_size,
        "query": query,
        "platform": None,
        "schema_name": None,
    }


def _fields_action(direction: str = "first") -> dict[str, object]:
    return {
        "action": "fields",
        "direction": direction,
        "page_size": 50,
        "query": None,
        "native_type": None,
    }


@pytest.mark.parametrize(
    "key",
    [
        "DATABASE_URL",
        "DATAHUB_GMS_TOKEN",
        "OPENAI_API_KEY",
        "SCHEMABRIDGE_CONTROL_DATABASE_URL",
        "SCHEMABRIDGE_CONTROL_API_DATABASE_URL",
        "SCHEMABRIDGE_INVENTORY_CURSOR_SIGNING_KEY",
        "some_cursor_signing_key",
    ],
)
def test_panel_fails_closed_when_process_environment_contains_secrets(key: str) -> None:
    with pytest.raises(PanelError) as captured:
        require_secret_free_environment({key: "protected"})

    assert captured.value.code == "forbidden_environment"
    assert "protected" not in captured.value.title


def test_panel_accepts_only_numeric_loopback_bind_and_upstream() -> None:
    assert validate_loopback_host("127.0.0.1") == "127.0.0.1"
    assert UpstreamTarget.parse("http://127.0.0.1:8520") == UpstreamTarget(
        host="127.0.0.1",
        port=8520,
    )

    for value in (
        "localhost",
        "0.0.0.0",
        "192.0.2.1",
        "api.internal",
    ):
        with pytest.raises(PanelError):
            validate_loopback_host(value)
    for value in (
        "https://127.0.0.1:8520",
        "http://user:password@127.0.0.1:8520",
        "http://127.0.0.1:8520/catalog",
        "http://127.0.0.1:8520?next=http://example.com",
        "http://example.com:8520",
        "file:///etc/passwd",
    ):
        with pytest.raises(PanelError):
            UpstreamTarget.parse(value)


def test_bearer_file_must_be_regular_owner_only_and_bounded(tmp_path: Path) -> None:
    token_file = tmp_path / "small.token"
    token_file.write_text(f"{SMALL_TOKEN}\n", encoding="ascii")
    token_file.chmod(0o600)
    assert read_bearer_file(token_file) == SMALL_TOKEN

    token_file.chmod(0o640)
    with pytest.raises(PanelError):
        read_bearer_file(token_file)
    token_file.chmod(0o600)
    link = tmp_path / "link.token"
    link.symlink_to(token_file)
    with pytest.raises(PanelError):
        read_bearer_file(link)


@pytest.mark.parametrize(
    ("method", "path", "query", "body", "idempotency"),
    [
        ("GET", "http://169.254.169.254/latest/meta-data", {}, None, None),
        ("GET", "/v1/catalog/connections/../../health/ready", {}, None, None),
        ("GET", "/v1/catalog/connections", {"redirect": "http://example.com"}, None, None),
        ("DELETE", "/v1/catalog/connections", {}, None, None),
        ("POST", "/v1/catalog/connections", {}, {}, "generated-key"),
        (
            "POST",
            "/v1/catalog/connections/warehouse-small/refreshes",
            {},
            {"mode": "full", "confirmation": "wrong"},
            "generated-key",
        ),
        ("GET", "/v1/catalog/refreshes/refresh-acceptance-001", {}, None, "leaked-key"),
    ],
)
def test_upstream_method_path_query_and_body_allowlist_is_exact(
    method: str,
    path: str,
    query: dict[str, str | int],
    body: dict[str, object] | None,
    idempotency: str | None,
) -> None:
    with pytest.raises(PanelError) as captured:
        _validate_upstream_request(
            method=method,
            path=path,
            query=query,
            body=body,
            idempotency_key=idempotency,
        )

    assert captured.value.code == "upstream_request_denied"


@pytest.mark.parametrize(
    "query",
    [
        {"page_size": 0},
        {"page_size": 51},
        {"page_size": True},
        {"page_size": 1, "cursor": "contains whitespace"},
        {"page_size": 1, "cursor": "x" * 1025},
        {"page_size": 1, "status": "active"},
    ],
)
def test_upstream_query_values_are_bounded(query: dict[str, str | int]) -> None:
    with pytest.raises(PanelError) as captured:
        _validate_upstream_request(
            method="GET",
            path="/v1/catalog/connections",
            query=query,
            body=None,
            idempotency_key=None,
        )

    assert captured.value.code == "upstream_request_denied"


def test_action_allowlist_rejects_extra_parameter_before_transport() -> None:
    transport = _FakeTransport()
    bff = CatalogPanelBff(transport)
    state = bff.new_state()

    result = bff.apply(
        state,
        {
            **_connections_action(),
            "url": "http://169.254.169.254/latest/meta-data",
        },
    )

    assert result["ok"] is False
    assert result["problem"]["code"] == "invalid_action_parameters"  # type: ignore[index]
    assert transport.calls == []


def test_unexpected_transport_failure_is_projected_without_exception_text() -> None:
    protected_detail = "postgresql://protected-dsn"

    def fail(_call: dict[str, object]) -> UpstreamResponse:
        raise RuntimeError(protected_detail)

    bff = CatalogPanelBff(_FakeTransport(responder=fail))

    result = bff.apply(bff.new_state(), _connections_action())

    assert result["ok"] is False
    assert result["problem"] == {
        "status": 500,
        "code": "panel_internal_error",
        "title": "The catalog request failed safely.",
    }
    assert protected_detail not in json.dumps(result)


def test_connection_projection_exposes_stale_state_but_no_cursor_or_unknown_fields() -> None:
    raw_cursor = "opaque-server-side-cursor"
    transport = _FakeTransport(
        responses=[
            UpstreamResponse(
                200,
                _connection_page(next_cursor=raw_cursor, stale=True),
            )
        ]
    )
    bff = CatalogPanelBff(transport)

    result = bff.apply(bff.new_state(), _connections_action())
    encoded = json.dumps(result)

    assert result["ok"] is True
    state = result["state"]
    assert state["freshness"]["connections_stale"] is True  # type: ignore[index]
    assert state["freshness"]["connections_as_of"] == AS_OF  # type: ignore[index]
    assert state["paging"]["connections"]["has_next"] is True  # type: ignore[index]
    assert raw_cursor not in encoded
    assert "must-never-reach-browser" not in encoded
    assert "credential_binding_ref" not in encoded


def test_asset_and_field_paging_is_sequential_and_generation_bound() -> None:
    asset_cursor = "opaque-asset-cursor"
    field_cursor = "opaque-field-cursor"
    transport = _FakeTransport(
        responses=[
            UpstreamResponse(200, _connection_page()),
            UpstreamResponse(200, _asset_page(next_cursor=asset_cursor)),
            UpstreamResponse(200, _asset_page(next_cursor=None)),
            UpstreamResponse(200, _field_page(next_cursor=field_cursor)),
            UpstreamResponse(200, _field_page(next_cursor=None)),
        ]
    )
    bff = CatalogPanelBff(transport)
    state = bff.new_state()
    assert bff.apply(state, _connections_action())["ok"] is True
    assert (
        bff.apply(
            state,
            {"action": "select_connection", "connection_id": "warehouse-small"},
        )["ok"]
        is True
    )
    assert bff.apply(state, _assets_action())["ok"] is True
    assert bff.apply(state, _assets_action("next"))["ok"] is True
    assert (
        bff.apply(
            state,
            {"action": "select_asset", "asset_id": "urn:li:dataset:table_0001"},
        )["ok"]
        is True
    )
    assert bff.apply(state, _fields_action())["ok"] is True
    result = bff.apply(state, _fields_action("next"))

    assert result["ok"] is True
    second_asset_query = transport.calls[2]["query"]
    assert second_asset_query == {
        "page_size": 50,
        "cursor": asset_cursor,
        "generation": 7,
    }
    second_field_query = transport.calls[4]["query"]
    assert second_field_query == {
        "page_size": 50,
        "generation": 7,
        "cursor": field_cursor,
    }
    encoded = json.dumps(result)
    assert asset_cursor not in encoded
    assert field_cursor not in encoded


def test_generation_change_fails_safely_without_advancing_visible_page() -> None:
    transport = _FakeTransport(
        responses=[
            UpstreamResponse(200, _connection_page()),
            UpstreamResponse(200, _asset_page(next_cursor="cursor-1")),
            UpstreamResponse(
                200,
                _asset_page(generation=8, next_cursor="cursor-2"),
            ),
        ]
    )
    bff = CatalogPanelBff(transport)
    state = bff.new_state()
    bff.apply(state, _connections_action())
    bff.apply(
        state,
        {"action": "select_connection", "connection_id": "warehouse-small"},
    )
    bff.apply(state, _assets_action())

    result = bff.apply(state, _assets_action("next"))

    assert result["ok"] is False
    assert result["problem"]["code"] == "catalog_generation_changed"  # type: ignore[index]
    assert result["state"]["paging"]["assets"]["page"] == 1  # type: ignore[index]
    assert result["state"]["paging"]["assets"]["generation"] == 7  # type: ignore[index]


def test_filter_change_cannot_reuse_a_server_held_cursor() -> None:
    transport = _FakeTransport(
        responses=[
            UpstreamResponse(
                200,
                _connection_page(next_cursor="cursor-for-filter-a"),
            )
        ]
    )
    bff = CatalogPanelBff(transport)
    state = bff.new_state()
    bff.apply(state, _connections_action(query="contract"))

    result = bff.apply(
        state,
        _connections_action("next", query="customer"),
    )

    assert result["ok"] is False
    assert result["problem"]["code"] == "paging_context_changed"  # type: ignore[index]
    assert len(transport.calls) == 1


def test_cursor_history_is_bounded_while_page_number_remains_monotonic() -> None:
    pager = _Pager()
    filters = (("page_size", 50),)
    pager.prepare(direction="first", filters=filters)
    pager.record(next_cursor="cursor-2", generation=7)
    for page in range(2, MAX_CURSOR_HISTORY + 10):
        pager.prepare(direction="next", filters=filters)
        pager.record(next_cursor=f"cursor-{page + 1}", generation=7)

    public = pager.public()
    assert public["retained_pages"] == MAX_CURSOR_HISTORY
    assert public["page"] == MAX_CURSOR_HISTORY + 9
    assert "cursor" not in json.dumps(public)


def test_cursor_probe_uses_server_cursor_and_projects_only_safe_denial() -> None:
    raw_cursor = "opaque-valid-cursor"
    reflected_secret = "do-not-render-this-upstream-title"
    transport = _FakeTransport(
        responses=[
            UpstreamResponse(200, _connection_page(next_cursor=raw_cursor)),
            UpstreamResponse(
                404,
                {
                    "code": "inventory_cursor_unavailable",
                    "title": reflected_secret,
                    "detail": SMALL_TOKEN,
                },
            ),
        ]
    )
    bff = CatalogPanelBff(transport)
    state = bff.new_state()
    bff.apply(state, _connections_action())

    result = bff.apply(
        state,
        {
            "action": "cursor_probe",
            "resource": "connections",
            "probe": "tampered",
        },
    )

    assert result["ok"] is True
    cursor_check = result["state"]["cursor_check"]  # type: ignore[index]
    assert cursor_check == {
        "state": "denied",
        "probe": "tampered",
        "resource": "connections",
        "status": 404,
        "code": "inventory_cursor_unavailable",
    }
    encoded = json.dumps(result)
    assert raw_cursor not in encoded
    assert reflected_secret not in encoded
    assert SMALL_TOKEN not in encoded
    assert transport.calls[1]["query"]["cursor"] != raw_cursor  # type: ignore[index]


def test_expired_probe_refuses_to_claim_expiry_before_real_cursor_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_cursor = "opaque-valid-cursor"
    transport = _FakeTransport(
        responses=[
            UpstreamResponse(200, _connection_page(next_cursor=raw_cursor)),
            UpstreamResponse(
                404,
                {"code": "inventory_cursor_unavailable"},
            ),
        ]
    )
    bff = CatalogPanelBff(transport)
    state = bff.new_state()
    monkeypatch.setattr(panel_module.time, "monotonic", lambda: 100.0)
    bff.apply(state, _connections_action())

    monkeypatch.setattr(panel_module.time, "monotonic", lambda: 999.9)
    too_early = bff.apply(
        state,
        {
            "action": "cursor_probe",
            "resource": "connections",
            "probe": "expired",
        },
    )

    assert too_early["ok"] is False
    assert too_early["problem"]["code"] == "cursor_not_expired"  # type: ignore[index]
    assert len(transport.calls) == 1

    monkeypatch.setattr(panel_module.time, "monotonic", lambda: 1_000.0)
    expired = bff.apply(
        state,
        {
            "action": "cursor_probe",
            "resource": "connections",
            "probe": "expired",
        },
    )
    assert expired["ok"] is True
    assert expired["state"]["cursor_check"]["state"] == "denied"  # type: ignore[index]
    assert len(transport.calls) == 2


def test_rate_probe_is_bounded_and_shows_safe_retry_state() -> None:
    transport = _FakeTransport(
        responses=[
            UpstreamResponse(200, _connection_page()),
            UpstreamResponse(
                429,
                {"code": "api_rate_limited", "title": "do not trust me"},
                retry_after=17,
            ),
        ]
    )
    bff = CatalogPanelBff(transport)
    state = bff.new_state()

    result = bff.apply(state, {"action": "rate_probe", "requests": 64})

    assert result["ok"] is True
    assert result["state"]["rate_check"] == {  # type: ignore[index]
        "state": "denied",
        "attempts": 2,
        "status": 429,
        "code": "api_rate_limited",
        "retry_after_seconds": 17,
    }
    invalid = bff.apply(state, {"action": "rate_probe", "requests": 65})
    assert invalid["ok"] is False
    assert len(transport.calls) == 2


def test_refresh_id_and_state_are_visible_but_idempotency_is_server_generated() -> None:
    transport = _FakeTransport(
        responses=[
            UpstreamResponse(
                202,
                {"refresh": _refresh(), "replayed": False},
            ),
            UpstreamResponse(200, {**_refresh(), "status": "staging"}),
        ]
    )
    bff = CatalogPanelBff(transport)
    state = bff.new_state()
    state.selected_connection = "warehouse-small"

    requested = bff.apply(state, {"action": "refresh", "mode": "full"})
    inspected = bff.apply(state, {"action": "refresh_status"})

    assert requested["ok"] is True
    assert inspected["state"]["refresh"]["status"] == "staging"  # type: ignore[index]
    key = transport.calls[0]["idempotency_key"]
    assert isinstance(key, str)
    assert key.startswith("m25-panel-")
    assert key not in json.dumps(requested)
    assert transport.calls[1]["path"] == "/v1/catalog/refreshes/refresh-acceptance-001"
    assert "catalog_fingerprint" not in json.dumps(requested)


def test_path_traversal_asset_identifier_is_denied_before_transport() -> None:
    transport = _FakeTransport()
    bff = CatalogPanelBff(transport)
    state = bff.new_state()
    state.assets = [
        {
            "asset_id": "../../health/ready",
            "generation": 7,
        }
    ]

    result = bff.apply(
        state,
        {"action": "select_asset", "asset_id": "../../health/ready"},
    )

    assert result["ok"] is False
    assert result["problem"]["code"] == "invalid_asset_id"  # type: ignore[index]
    assert transport.calls == []


class _RedirectHandler(BaseHTTPRequestHandler):
    hits = 0
    response_body = b"{}"
    response_status = 302
    response_content_type = "application/json"
    response_location = "http://127.0.0.1:9/should-not-be-followed"
    authorization: str | None = None

    def do_GET(self) -> None:
        type(self).hits += 1
        type(self).authorization = self.headers.get("Authorization")
        self.send_response(type(self).response_status)
        self.send_header("Content-Type", type(self).response_content_type)
        self.send_header("Location", type(self).response_location)
        self.send_header("Content-Length", str(len(type(self).response_body)))
        self.end_headers()
        self.wfile.write(type(self).response_body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@contextmanager
def _upstream_server(
    handler: type[BaseHTTPRequestHandler],
) -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_http_transport_does_not_follow_redirects_or_forward_caller_headers() -> None:
    _RedirectHandler.hits = 0
    _RedirectHandler.response_status = 302
    _RedirectHandler.response_body = b"{}"
    _RedirectHandler.response_content_type = "application/json"
    with _upstream_server(_RedirectHandler) as server:
        transport = HttpCatalogTransport(
            target=UpstreamTarget("127.0.0.1", server.server_port),
            bearers={
                "small": SMALL_TOKEN,
                "large": LARGE_TOKEN,
                "datahub": DATAHUB_TOKEN,
            },
        )
        response = transport.request(
            alias="small",
            method="GET",
            path="/v1/catalog/connections",
            query={"page_size": 1},
        )

    assert response.status == 302
    assert _RedirectHandler.hits == 1
    assert _RedirectHandler.authorization == f"Bearer {SMALL_TOKEN}"


def test_http_transport_accepts_problem_json_only_for_error_responses() -> None:
    _RedirectHandler.hits = 0
    _RedirectHandler.response_status = 404
    _RedirectHandler.response_content_type = "application/problem+json; charset=utf-8"
    _RedirectHandler.response_body = json.dumps(
        {
            "status": 404,
            "code": "inventory_cursor_unavailable",
            "title": "The inventory cursor is not available.",
        }
    ).encode()
    with _upstream_server(_RedirectHandler) as server:
        transport = HttpCatalogTransport(
            target=UpstreamTarget("127.0.0.1", server.server_port),
            bearers={
                "small": SMALL_TOKEN,
                "large": LARGE_TOKEN,
                "datahub": DATAHUB_TOKEN,
            },
        )
        response = transport.request(
            alias="small",
            method="GET",
            path="/v1/catalog/connections",
            query={"page_size": 1},
        )

    assert response.status == 404
    assert response.payload == {
        "status": 404,
        "code": "inventory_cursor_unavailable",
        "title": "The inventory cursor is not available.",
    }

    _RedirectHandler.response_status = 200
    with _upstream_server(_RedirectHandler) as server:
        transport = HttpCatalogTransport(
            target=UpstreamTarget("127.0.0.1", server.server_port),
            bearers={
                "small": SMALL_TOKEN,
                "large": LARGE_TOKEN,
                "datahub": DATAHUB_TOKEN,
            },
        )
        with pytest.raises(PanelError) as invalid_success:
            transport.request(
                alias="small",
                method="GET",
                path="/v1/catalog/connections",
                query={"page_size": 1},
            )
    assert invalid_success.value.code == "invalid_upstream_response"

    _RedirectHandler.response_content_type = "application/json"


def test_http_transport_rejects_bearer_reflection_and_oversized_json() -> None:
    _RedirectHandler.hits = 0
    _RedirectHandler.response_status = 200
    _RedirectHandler.response_content_type = "application/json"
    _RedirectHandler.response_body = json.dumps({"echo": SMALL_TOKEN}).encode()
    with _upstream_server(_RedirectHandler) as server:
        transport = HttpCatalogTransport(
            target=UpstreamTarget("127.0.0.1", server.server_port),
            bearers={
                "small": SMALL_TOKEN,
                "large": LARGE_TOKEN,
                "datahub": DATAHUB_TOKEN,
            },
            max_response_bytes=1024,
        )
        with pytest.raises(PanelError) as reflected:
            transport.request(
                alias="small",
                method="GET",
                path="/v1/catalog/connections",
                query={"page_size": 1},
            )
    assert reflected.value.code == "protected_value_in_upstream_response"
    assert SMALL_TOKEN not in reflected.value.title

    _RedirectHandler.response_body = b'{"padding":"' + b"x" * 1100 + b'"}'
    with _upstream_server(_RedirectHandler) as server:
        transport = HttpCatalogTransport(
            target=UpstreamTarget("127.0.0.1", server.server_port),
            bearers={
                "small": SMALL_TOKEN,
                "large": LARGE_TOKEN,
                "datahub": DATAHUB_TOKEN,
            },
            max_response_bytes=1024,
        )
        with pytest.raises(PanelError) as oversized:
            transport.request(
                alias="small",
                method="GET",
                path="/v1/catalog/connections",
                query={"page_size": 1},
            )
    assert oversized.value.code == "upstream_response_too_large"


@contextmanager
def _panel_server(
    transport: CatalogTransport,
) -> Iterator[CatalogPanelServer]:
    server = CatalogPanelServer(("127.0.0.1", 0), CatalogPanelBff(transport))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _read_json(response: http.client.HTTPResponse) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(response.read()))


def test_panel_http_boundary_keeps_caller_headers_out_of_upstream_and_state() -> None:
    transport = _FakeTransport(responses=[UpstreamResponse(200, _connection_page())])
    with _panel_server(transport) as server:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/state")
        initial = connection.getresponse()
        cookie = initial.getheader("Set-Cookie")
        initial_payload = _read_json(initial)
        assert initial.status == 200
        assert cookie is not None
        assert "HttpOnly" in cookie
        assert initial_payload["state"]["alias"] == "small"

        caller_bearer = "caller-supplied-must-not-forward"
        caller_idempotency = "caller-idempotency-must-not-forward"
        body = json.dumps(_connections_action()).encode()
        connection.request(
            "POST",
            "/action",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "Cookie": cookie.split(";", 1)[0],
                "Origin": f"http://127.0.0.1:{server.server_port}",
                "Authorization": f"Bearer {caller_bearer}",
                "Idempotency-Key": caller_idempotency,
                "X-Forwarded-Host": "169.254.169.254",
            },
        )
        response = connection.getresponse()
        payload = _read_json(response)
        connection.close()

    assert response.status == 200
    assert payload["ok"] is True
    encoded_call = json.dumps(transport.calls[0])
    encoded_response = json.dumps(payload)
    assert caller_bearer not in encoded_call
    assert caller_idempotency not in encoded_call
    assert caller_bearer not in encoded_response
    assert caller_idempotency not in encoded_response


def test_panel_http_boundary_rejects_host_origin_query_and_duplicate_json() -> None:
    transport = _FakeTransport()
    with _panel_server(transport) as server:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/state?next=http://169.254.169.254")
        query_response = connection.getresponse()
        _read_json(query_response)
        assert query_response.status == 400
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/state", headers={"Host": "attacker.example"})
        host_response = connection.getresponse()
        _read_json(host_response)
        assert host_response.status == 400
        connection.close()

        duplicate = b'{"action":"refresh_status","action":"select_alias","alias":"small"}'
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request(
            "POST",
            "/action",
            body=duplicate,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(duplicate)),
                "Origin": "http://attacker.example",
            },
        )
        origin_response = connection.getresponse()
        _read_json(origin_response)
        assert origin_response.status == 403
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request(
            "POST",
            "/action",
            body=duplicate,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(duplicate)),
                "Origin": f"http://127.0.0.1:{server.server_port}",
            },
        )
        duplicate_response = connection.getresponse()
        _read_json(duplicate_response)
        connection.close()

    assert duplicate_response.status == 400
    assert transport.calls == []


def test_panel_static_ui_is_explicitly_local_responsive_and_accessible() -> None:
    combined = f"{_PANEL_HTML}\n{_PANEL_CSS}\n{_PANEL_JS}"
    assert "SOLO ACEPTACIÓN LOCAL" in _PANEL_HTML
    assert "no es la interfaz de producto" in _PANEL_HTML
    assert 'name="viewport"' in _PANEL_HTML
    assert 'aria-live="polite"' in _PANEL_HTML
    assert "@media(max-width:480px)" in _PANEL_CSS
    assert "overflow-x:hidden" in _PANEL_CSS
    assert "textContent" in _PANEL_JS
    assert "innerHTML" not in _PANEL_JS
    assert "Authorization" not in combined
    assert SMALL_TOKEN not in combined
    assert LARGE_TOKEN not in combined


def test_unrelated_environment_is_allowed() -> None:
    require_secret_free_environment(
        {
            "PATH": os.environ.get("PATH", ""),
            "SCHEMABRIDGE_PROFILE": "local",
        }
    )
