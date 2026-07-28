from __future__ import annotations

import http.client
import json
import os
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest
from scripts.m26_semantic_change_browser_panel import (
    _PANEL_CSS,
    _PANEL_HTML,
    _PANEL_JS,
    MAX_PAGE_SIZE,
    HttpSemanticChangeTransport,
    PanelError,
    SemanticChangePanelBff,
    SemanticChangePanelServer,
    SemanticChangeTransport,
    SyntheticSemanticChangeTransport,
    UpstreamResponse,
    UpstreamTarget,
    _report_identifier,
    _validate_upstream_request,
    read_bearer_file,
    require_secret_free_environment,
    validate_loopback_host,
)

BEARER = "m26-server-held-acceptance-bearer"


@dataclass
class _FakeTransport(SemanticChangeTransport):
    responses: list[UpstreamResponse] = field(default_factory=list)
    error: Exception | None = None
    calls: list[dict[str, object]] = field(default_factory=list)
    label: str = "fake_http"

    def request(
        self,
        *,
        method: str,
        path: str,
        query: Mapping[str, str | int],
    ) -> UpstreamResponse:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "query": dict(query),
            }
        )
        if self.error is not None:
            raise self.error
        if not self.responses:
            raise AssertionError("unexpected fake transport call")
        return self.responses.pop(0)


def _reports_action(
    direction: str = "first",
    *,
    page_size: int = 50,
    status: str | None = None,
) -> dict[str, object]:
    return {
        "action": "reports",
        "direction": direction,
        "page_size": page_size,
        "status": status,
    }


def _findings_action(
    direction: str = "first",
    *,
    page_size: int = 50,
    kind: str | None = None,
    severity: str | None = None,
) -> dict[str, object]:
    return {
        "action": "findings",
        "direction": direction,
        "page_size": page_size,
        "kind": kind,
        "severity": severity,
    }


def _impacts_action(
    direction: str = "first",
    *,
    page_size: int = 50,
    kind: str | None = None,
) -> dict[str, object]:
    return {
        "action": "impacts",
        "direction": direction,
        "page_size": page_size,
        "kind": kind,
    }


def _select_blocked(
    bff: SemanticChangePanelBff,
    state: object,
) -> str:
    selected = bff.apply(
        state,  # type: ignore[arg-type]
        {
            "action": "select_scenario",
            "scenario": "blocked",
            "page_size": 50,
        },
    )
    report_id = selected["state"]["reports"][0]["report_id"]  # type: ignore[index]
    inspected = bff.apply(
        state,  # type: ignore[arg-type]
        {"action": "select_report", "report_id": report_id},
    )
    assert inspected["ok"] is True
    return cast(str, report_id)


@pytest.mark.parametrize(
    "key",
    [
        "DATABASE_URL",
        "DATAHUB_GMS_TOKEN",
        "OPENAI_API_KEY",
        "SCHEMABRIDGE_CONTROL_API_DATABASE_URL",
        "SCHEMABRIDGE_SEMANTIC_CHANGE_CURSOR_SIGNING_KEY",
        "some_cursor_signing_key",
    ],
)
def test_panel_fails_closed_when_environment_contains_privileged_material(
    key: str,
) -> None:
    with pytest.raises(PanelError) as captured:
        require_secret_free_environment({key: "protected"})

    assert captured.value.code == "forbidden_environment"
    assert "protected" not in captured.value.title


def test_unrelated_environment_is_allowed() -> None:
    require_secret_free_environment(
        {
            "PATH": os.environ.get("PATH", ""),
            "SCHEMABRIDGE_PROFILE": "local",
        }
    )


def test_panel_accepts_only_numeric_loopback_bind_and_upstream() -> None:
    assert validate_loopback_host("127.0.0.1") == "127.0.0.1"
    assert UpstreamTarget.parse("http://127.0.0.1:8520") == UpstreamTarget(
        host="127.0.0.1",
        port=8520,
    )

    for value in ("localhost", "0.0.0.0", "192.0.2.1", "api.internal"):
        with pytest.raises(PanelError):
            validate_loopback_host(value)
    for value in (
        "https://127.0.0.1:8520",
        "http://user:password@127.0.0.1:8520",
        "http://127.0.0.1:8520/semantic",
        "http://127.0.0.1:8520?next=http://example.com",
        "http://example.com:8520",
        "file:///etc/passwd",
    ):
        with pytest.raises(PanelError):
            UpstreamTarget.parse(value)


def test_bearer_file_must_be_regular_owner_only_and_bounded(tmp_path: Path) -> None:
    token_file = tmp_path / "reader.token"
    token_file.write_text(f"{BEARER}\n", encoding="ascii")
    token_file.chmod(0o600)
    assert read_bearer_file(token_file) == BEARER

    token_file.chmod(0o640)
    with pytest.raises(PanelError):
        read_bearer_file(token_file)
    token_file.chmod(0o600)
    link = tmp_path / "reader-link.token"
    link.symlink_to(token_file)
    with pytest.raises(PanelError):
        read_bearer_file(link)


@pytest.mark.parametrize(
    ("method", "path", "query"),
    [
        ("POST", "/v1/semantic-changes/reports", {}),
        ("PUT", f"/v1/semantic-changes/reports/{_report_identifier('a')}", {}),
        (
            "GET",
            "http://169.254.169.254/latest/meta-data",
            {},
        ),
        (
            "GET",
            "/v1/semantic-changes/reports",
            {"redirect": "http://example.com"},
        ),
        (
            "GET",
            "/v1/semantic-changes/reports/../../health/ready",
            {},
        ),
        (
            "GET",
            f"/v1/semantic-changes/reports/{_report_identifier('a')}",
            {"page_size": 1},
        ),
    ],
)
def test_upstream_allowlist_contains_only_four_get_surfaces(
    method: str,
    path: str,
    query: dict[str, str | int],
) -> None:
    with pytest.raises(PanelError) as captured:
        _validate_upstream_request(method=method, path=path, query=query)

    assert captured.value.code == "upstream_request_denied"


@pytest.mark.parametrize(
    ("path", "query"),
    [
        ("/v1/semantic-changes/reports", {"page_size": 0}),
        ("/v1/semantic-changes/reports", {"page_size": 51}),
        ("/v1/semantic-changes/reports", {"page_size": True}),
        ("/v1/semantic-changes/reports", {"status": "remediated"}),
        ("/v1/semantic-changes/reports", {"cursor": "contains whitespace"}),
        (
            f"/v1/semantic-changes/reports/{_report_identifier('a')}/findings",
            {"kind": "made_up"},
        ),
        (
            f"/v1/semantic-changes/reports/{_report_identifier('a')}/impacts",
            {"severity": "blocking"},
        ),
    ],
)
def test_upstream_filters_and_page_size_are_closed_and_bounded(
    path: str,
    query: dict[str, str | int],
) -> None:
    with pytest.raises(PanelError) as captured:
        _validate_upstream_request(method="GET", path=path, query=query)

    assert captured.value.code == "upstream_request_denied"


def test_synthetic_fixture_exposes_all_acceptance_states() -> None:
    bff = SemanticChangePanelBff(SyntheticSemanticChangeTransport.create())
    state = bff.new_state()
    expected = {
        "current": "current",
        "review": "review_required",
        "blocked": "blocked",
        "remediated": "revalidated",
    }

    for scenario, status in expected.items():
        result = bff.apply(
            state,
            {
                "action": "select_scenario",
                "scenario": scenario,
                "page_size": 50,
            },
        )
        assert result["ok"] is True
        public = result["state"]
        assert public["scenario_status"] == status  # type: ignore[index]
        assert public["reports"][0]["status"] == status  # type: ignore[index]

    guide = result["state"]["status_guide"]  # type: ignore[index]
    assert {item["label"] for item in guide} == {  # type: ignore[index]
        "Actual",
        "Revisión requerida",
        "Bloqueado",
        "Remediado",
    }


def test_synthetic_fixture_counts_and_blast_radius_links_are_coherent() -> None:
    fixture = SyntheticSemanticChangeTransport.create()

    for report in fixture.reports:
        report_id = cast(str, report["report_id"])
        findings = fixture.findings.get(report_id, ())
        impacts = fixture.impacts.get(report_id, ())
        finding_ids = {item["finding_id"] for item in findings}
        assert report["finding_count"] == len(findings)
        assert sum(
            cast(int, report[key])
            for key in (
                "mapping_impact_count",
                "join_impact_count",
                "workflow_impact_count",
                "recipe_impact_count",
            )
        ) == len(impacts)
        assert all(set(cast(list[str], item["finding_ids"])) <= finding_ids for item in impacts)


def test_findings_and_impacts_page_at_fifty_without_exposing_cursors() -> None:
    bff = SemanticChangePanelBff(SyntheticSemanticChangeTransport.create())
    state = bff.new_state()
    _select_blocked(bff, state)

    first_findings = bff.apply(state, _findings_action())
    encoded_first = json.dumps(first_findings)
    assert first_findings["ok"] is True
    assert len(first_findings["state"]["findings"]) == MAX_PAGE_SIZE  # type: ignore[index]
    assert first_findings["state"]["paging"]["findings"] == {  # type: ignore[index]
        "page": 1,
        "has_previous": False,
        "has_next": True,
        "retained_pages": 1,
    }
    assert "fixture_" not in encoded_first

    second_findings = bff.apply(state, _findings_action("next"))
    assert len(second_findings["state"]["findings"]) == 7  # type: ignore[index]
    assert second_findings["state"]["paging"]["findings"]["page"] == 2  # type: ignore[index]

    first_impacts = bff.apply(state, _impacts_action())
    second_impacts = bff.apply(state, _impacts_action("next"))
    assert len(first_impacts["state"]["impacts"]) == 50  # type: ignore[index]
    assert len(second_impacts["state"]["impacts"]) == 13  # type: ignore[index]
    assert "fixture_" not in json.dumps(second_impacts)


def test_synthetic_cursor_is_bound_to_resource_report_and_filter() -> None:
    transport = SyntheticSemanticChangeTransport.create()
    report_id = _report_identifier("c")
    first = transport.request(
        method="GET",
        path=f"/v1/semantic-changes/reports/{report_id}/findings",
        query={"page_size": 1},
    )
    cursor = cast(dict[str, object], first.payload)["next_cursor"]
    assert isinstance(cursor, str)

    wrong_resource = transport.request(
        method="GET",
        path=f"/v1/semantic-changes/reports/{report_id}/impacts",
        query={"page_size": 1, "cursor": cursor},
    )
    wrong_report = transport.request(
        method="GET",
        path=f"/v1/semantic-changes/reports/{_report_identifier('b')}/findings",
        query={"page_size": 1, "cursor": cursor},
    )
    wrong_filter = transport.request(
        method="GET",
        path=f"/v1/semantic-changes/reports/{report_id}/findings",
        query={"page_size": 1, "severity": "blocking", "cursor": cursor},
    )

    assert {
        (response.status, cast(dict[str, object], response.payload)["code"])
        for response in (wrong_resource, wrong_report, wrong_filter)
    } == {(404, "semantic_change_resource_unavailable")}


def test_filter_change_cannot_reuse_server_held_cursor_or_advance_state() -> None:
    bff = SemanticChangePanelBff(SyntheticSemanticChangeTransport.create())
    state = bff.new_state()
    _select_blocked(bff, state)
    first = bff.apply(state, _findings_action(page_size=17))
    assert first["ok"] is True
    visible_before = json.dumps(first["state"]["findings"])  # type: ignore[index]

    changed = bff.apply(
        state,
        _findings_action(
            "next",
            page_size=17,
            severity="blocking",
        ),
    )

    assert changed["ok"] is False
    assert changed["problem"]["code"] == "paging_context_changed"  # type: ignore[index]
    assert json.dumps(changed["state"]["findings"]) == visible_before  # type: ignore[index]
    assert changed["state"]["paging"]["findings"]["page"] == 1  # type: ignore[index]


def test_arbitrary_report_cannot_be_loaded_outside_visible_page() -> None:
    transport = _FakeTransport()
    bff = SemanticChangePanelBff(transport)
    state = bff.new_state()

    result = bff.apply(
        state,
        {
            "action": "select_report",
            "report_id": _report_identifier("e"),
        },
    )

    assert result["ok"] is False
    assert result["problem"]["code"] == "semantic_change_resource_unavailable"  # type: ignore[index]
    assert transport.calls == []


def test_projection_drops_unknown_protected_fields_and_upstream_problem_detail() -> None:
    raw_report = SyntheticSemanticChangeTransport.create().reports[0]
    raw_cursor = "server-only-cursor"
    fake = _FakeTransport(
        responses=[
            UpstreamResponse(
                200,
                {
                    "resource": "reports",
                    "items": [
                        {
                            **raw_report,
                            "sql": "select protected",
                            "source_value": "raw-value",
                            "credential": "secret-value",
                            "workspace_id": "tenant-private",
                        }
                    ],
                    "next_cursor": raw_cursor,
                    "as_of": "2026-07-24T18:00:00Z",
                    "audit_key": "never-visible",
                },
            ),
            UpstreamResponse(
                404,
                {
                    "code": "semantic_change_resource_unavailable",
                    "title": "do-not-trust-upstream-title",
                    "detail": "postgresql://private-dsn",
                },
            ),
        ]
    )
    bff = SemanticChangePanelBff(fake)
    state = bff.new_state()
    loaded = bff.apply(state, _reports_action(page_size=1))
    denied = bff.apply(state, _reports_action("next", page_size=1))
    encoded = json.dumps((loaded, denied))

    assert loaded["ok"] is True
    assert denied["ok"] is False
    assert denied["problem"]["title"] == _safe_title(404)  # type: ignore[index]
    for protected in (
        raw_cursor,
        "select protected",
        "raw-value",
        "secret-value",
        "tenant-private",
        "never-visible",
        "do-not-trust-upstream-title",
        "private-dsn",
    ):
        assert protected not in encoded


def _safe_title(status: int) -> str:
    return {
        404: "El recurso de cambio semántico no está disponible.",
    }[status]


def test_oversized_page_and_unexpected_exception_fail_without_partial_state() -> None:
    item = SyntheticSemanticChangeTransport.create().reports[0]
    oversized = _FakeTransport(
        responses=[
            UpstreamResponse(
                200,
                {
                    "resource": "reports",
                    "items": [item] * 51,
                    "next_cursor": None,
                    "as_of": "2026-07-24T18:00:00Z",
                },
            )
        ]
    )
    oversized_bff = SemanticChangePanelBff(oversized)
    oversized_state = oversized_bff.new_state()
    rejected = oversized_bff.apply(oversized_state, _reports_action())
    assert rejected["ok"] is False
    assert rejected["state"]["reports"] == []  # type: ignore[index]

    protected_error = "postgresql://private-dsn"
    failing_bff = SemanticChangePanelBff(_FakeTransport(error=RuntimeError(protected_error)))
    failed = failing_bff.apply(failing_bff.new_state(), _reports_action())
    assert failed["problem"] == {  # type: ignore[index]
        "status": 500,
        "code": "panel_internal_error",
        "title": "La lectura semántica falló de forma segura.",
    }
    assert protected_error not in json.dumps(failed)


def test_action_surface_has_no_approval_rejection_or_remediation_mutation() -> None:
    transport = _FakeTransport()
    bff = SemanticChangePanelBff(transport)
    state = bff.new_state()

    for action in (
        {"action": "approve", "report_id": _report_identifier("a")},
        {"action": "reject", "report_id": _report_identifier("a")},
        {"action": "remediate", "report_id": _report_identifier("a")},
        {
            **_reports_action(),
            "confirmation": "REVALIDATE COMPATIBLE SEMANTIC CHANGE",
        },
    ):
        result = bff.apply(state, action)
        assert result["ok"] is False

    assert transport.calls == []
    access = state.public()["access_boundary"]
    assert access == {
        "upstream_http_methods": ["GET"],
        "upstream_read_routes": 4,
        "upstream_mutation_routes": 0,
        "approval_controls": False,
        "operator_mutations": "CLI separada; no disponible en este panel",
    }


class _UpstreamHandler(BaseHTTPRequestHandler):
    hits = 0
    method = ""
    authorization: str | None = None
    response_status = 302
    response_content_type = "application/json"
    response_body = b"{}"
    response_location = "http://127.0.0.1:9/must-not-follow"

    def do_GET(self) -> None:
        type(self).hits += 1
        type(self).method = "GET"
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
def _server(
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


def test_http_transport_does_not_follow_redirect_and_uses_only_server_bearer() -> None:
    _UpstreamHandler.hits = 0
    _UpstreamHandler.response_status = 302
    _UpstreamHandler.response_content_type = "application/json"
    _UpstreamHandler.response_body = b"{}"
    with _server(_UpstreamHandler) as server:
        transport = HttpSemanticChangeTransport(
            target=UpstreamTarget("127.0.0.1", server.server_port),
            bearer=BEARER,
        )
        response = transport.request(
            method="GET",
            path="/v1/semantic-changes/reports",
            query={"page_size": 1},
        )

    assert response.status == 302
    assert _UpstreamHandler.hits == 1
    assert _UpstreamHandler.method == "GET"
    assert _UpstreamHandler.authorization == f"Bearer {BEARER}"


def test_http_transport_rejects_bearer_reflection_and_oversized_json() -> None:
    _UpstreamHandler.response_status = 200
    _UpstreamHandler.response_content_type = "application/json"
    _UpstreamHandler.response_body = json.dumps({"echo": BEARER}).encode()
    with _server(_UpstreamHandler) as server:
        transport = HttpSemanticChangeTransport(
            target=UpstreamTarget("127.0.0.1", server.server_port),
            bearer=BEARER,
            max_response_bytes=1024,
        )
        with pytest.raises(PanelError) as reflected:
            transport.request(
                method="GET",
                path="/v1/semantic-changes/reports",
                query={"page_size": 1},
            )
    assert reflected.value.code == "protected_value_in_upstream_response"

    _UpstreamHandler.response_body = b'{"padding":"' + (b"x" * 1100) + b'"}'
    with _server(_UpstreamHandler) as server:
        transport = HttpSemanticChangeTransport(
            target=UpstreamTarget("127.0.0.1", server.server_port),
            bearer=BEARER,
            max_response_bytes=1024,
        )
        with pytest.raises(PanelError) as oversized:
            transport.request(
                method="GET",
                path="/v1/semantic-changes/reports",
                query={"page_size": 1},
            )
    assert oversized.value.code == "upstream_response_too_large"


@contextmanager
def _panel_server(
    transport: SemanticChangeTransport,
) -> Iterator[SemanticChangePanelServer]:
    server = SemanticChangePanelServer(
        ("127.0.0.1", 0),
        SemanticChangePanelBff(transport),
    )
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


def test_panel_http_boundary_sets_security_headers_and_keeps_caller_headers_local() -> None:
    transport = _FakeTransport(
        responses=[
            SyntheticSemanticChangeTransport.create().request(
                method="GET",
                path="/v1/semantic-changes/reports",
                query={"page_size": 1},
            )
        ]
    )
    with _panel_server(transport) as server:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/state")
        initial = connection.getresponse()
        cookie = initial.getheader("Set-Cookie")
        csp = initial.getheader("Content-Security-Policy")
        _read_json(initial)
        assert initial.status == 200
        assert cookie is not None and "HttpOnly" in cookie and "SameSite=Strict" in cookie
        assert csp is not None and "frame-ancestors 'none'" in csp

        caller_bearer = "caller-bearer-must-not-forward"
        body = json.dumps(_reports_action(page_size=1)).encode()
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
                "X-Forwarded-Host": "169.254.169.254",
            },
        )
        response = connection.getresponse()
        payload = _read_json(response)
        connection.close()

    assert response.status == 200
    assert payload["ok"] is True
    assert caller_bearer not in json.dumps(transport.calls)
    assert caller_bearer not in json.dumps(payload)
    assert transport.calls[0]["method"] == "GET"


def test_panel_http_boundary_rejects_bad_host_origin_and_mutating_methods() -> None:
    with _panel_server(SyntheticSemanticChangeTransport.create()) as server:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/state", headers={"Host": "attacker.example"})
        bad_host = connection.getresponse()
        _read_json(bad_host)
        connection.close()

        body = json.dumps(_reports_action()).encode()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request(
            "POST",
            "/action",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "Origin": "http://attacker.example",
            },
        )
        bad_origin = connection.getresponse()
        _read_json(bad_origin)
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("DELETE", "/state")
        mutation = connection.getresponse()
        _read_json(mutation)
        connection.close()

    assert bad_host.status == 400
    assert bad_origin.status == 403
    assert mutation.status == 405


def test_static_ui_is_read_only_accessible_and_responsive() -> None:
    combined = f"{_PANEL_HTML}\n{_PANEL_CSS}\n{_PANEL_JS}"
    assert "SOLO ACEPTACIÓN LOCAL" in _PANEL_HTML
    assert "separado de Query Studio" in _PANEL_HTML
    assert "4 rutas GET · 0 rutas de mutación" in _PANEL_HTML
    assert "current" in _PANEL_HTML
    assert "review_required" in _PANEL_HTML
    assert "blocked" in _PANEL_HTML
    assert "revalidated" in _PANEL_HTML
    assert "Remediado" in _PANEL_HTML
    assert 'name="viewport"' in _PANEL_HTML
    assert 'aria-live="polite"' in _PANEL_HTML
    assert "@media(max-width:480px)" in _PANEL_CSS
    assert "overflow-x:hidden" in _PANEL_CSS
    assert "minmax(0,1fr)" in _PANEL_CSS
    assert "textContent" in _PANEL_JS
    assert "innerHTML" not in _PANEL_JS
    assert "Authorization" not in combined
    assert BEARER not in combined
    for forbidden_control in (
        'data-action="approve"',
        'data-action="reject"',
        'data-action="remediate"',
        "REVALIDATE COMPATIBLE SEMANTIC CHANGE",
    ):
        assert forbidden_control not in combined
