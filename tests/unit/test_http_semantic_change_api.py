from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from schemabridge.adapters.semantic_change.cursor import (
    SignedSemanticChangeCursorCodec,
)
from schemabridge.application.authentication import AuthenticationBoundaryError
from schemabridge.application.ports.semantic_change_read import (
    SemanticChangeFindingFilter,
    SemanticChangeFindingPublic,
    SemanticChangeImpactFilter,
    SemanticChangeImpactPublic,
    SemanticChangePageKey,
    SemanticChangeReportFilter,
    SemanticChangeReportPublic,
    SemanticChangeStorePage,
    SemanticChangeTargetKind,
)
from schemabridge.application.semantic_change_read import (
    InspectSemanticChangeReport,
    ListSemanticChangeFindings,
    ListSemanticChangeImpacts,
    ListSemanticChangeReports,
)
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)
from schemabridge.domain.semantic_change import (
    SemanticChangeKind,
    SemanticChangeSeverity,
    SemanticChangeStatus,
    SemanticImpactKind,
)
from schemabridge.entrypoints.http.app import (
    ApiHttpServices,
    SemanticChangeHttpServices,
    create_http_app,
)

NOW = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)
SIGNING_KEY = b"m26-semantic-change-cursor-key-0123456789"
REPORT_A = f"report_{'a' * 64}"
REPORT_B = f"report_{'b' * 64}"
REPORT_C_STALE = f"report_{'c' * 64}"
REPORT_D_TENANT_B = f"report_{'d' * 64}"
REPORT_UNKNOWN = f"report_{'e' * 64}"
FINDING_A = f"finding_{'1' * 64}"
FINDING_B = f"finding_{'2' * 64}"


@dataclass
class _Clock:
    value: datetime = NOW

    def now(self) -> datetime:
        return self.value


class _Authenticator:
    def authenticate(self, bearer_token: str, now: datetime) -> AuthenticatedPrincipal:
        identities = {
            "analyst-token": ("workspace-a", frozenset({IdentityRole.ANALYST})),
            "auditor-token": ("workspace-a", frozenset({IdentityRole.AUDITOR})),
            "no-role-token": ("workspace-a", frozenset()),
            "tenant-b-token": ("workspace-b", frozenset({IdentityRole.ANALYST})),
        }
        identity = identities.get(bearer_token)
        if identity is None:
            raise AuthenticationBoundaryError("invalid_bearer_token")
        workspace_id, roles = identity
        return AuthenticatedPrincipal(
            actor_id=f"actor-{workspace_id}",
            workspace_id=workspace_id,
            roles=roles,
            authentication_method=AuthenticationMethod.LOCAL_DEMO,
            authenticated_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(minutes=10),
        )


class _UnusedJobPort:
    def execute(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("execution jobs must not be called by semantic-change routes")


class _Readiness:
    def require_ready(self) -> None:
        return None


@dataclass
class _Store:
    reports: dict[tuple[str, str], SemanticChangeReportPublic]
    findings: dict[tuple[str, str], tuple[SemanticChangeFindingPublic, ...]]
    impacts: dict[tuple[str, str], tuple[SemanticChangeImpactPublic, ...]]
    stale: set[tuple[str, str]] = field(default_factory=set)
    report_list_calls: list[
        tuple[str, SemanticChangeReportFilter, int, SemanticChangePageKey | None]
    ] = field(default_factory=list)
    report_load_calls: list[tuple[str, str]] = field(default_factory=list)
    finding_calls: list[
        tuple[
            str,
            str,
            SemanticChangeFindingFilter,
            int,
            SemanticChangePageKey | None,
        ]
    ] = field(default_factory=list)
    impact_calls: list[
        tuple[
            str,
            str,
            SemanticChangeImpactFilter,
            int,
            SemanticChangePageKey | None,
        ]
    ] = field(default_factory=list)

    def list_reports(
        self,
        workspace_id: str,
        *,
        filters: SemanticChangeReportFilter,
        page_size: int,
        after: SemanticChangePageKey | None,
    ) -> SemanticChangeStorePage[SemanticChangeReportPublic]:
        self.report_list_calls.append((workspace_id, filters, page_size, after))
        candidates = [
            value
            for key, value in self.reports.items()
            if key[0] == workspace_id
            and key not in self.stale
            and (filters.status is None or value.status is filters.status)
        ]
        candidates.sort(
            key=lambda item: (item.inspected_at, item.report_id),
            reverse=True,
        )
        candidates = _after(candidates, after, lambda item: item.report_id)
        return _page(
            candidates,
            page_size,
            lambda item: SemanticChangePageKey(
                sort_value=item.inspected_at.isoformat(),
                stable_id=item.report_id,
            ),
        )

    def load_report(
        self,
        workspace_id: str,
        report_id: str,
    ) -> SemanticChangeReportPublic | None:
        self.report_load_calls.append((workspace_id, report_id))
        key = (workspace_id, report_id)
        if key in self.stale:
            return None
        return self.reports.get(key)

    def list_findings(
        self,
        workspace_id: str,
        report_id: str,
        *,
        filters: SemanticChangeFindingFilter,
        page_size: int,
        after: SemanticChangePageKey | None,
    ) -> SemanticChangeStorePage[SemanticChangeFindingPublic]:
        self.finding_calls.append((workspace_id, report_id, filters, page_size, after))
        candidates = [
            value
            for value in self.findings.get((workspace_id, report_id), ())
            if (filters.kind is None or value.kind is filters.kind)
            and (filters.severity is None or value.severity is filters.severity)
        ]
        candidates.sort(key=lambda item: item.finding_id)
        candidates = _after(candidates, after, lambda item: item.finding_id)
        return _page(
            candidates,
            page_size,
            lambda item: SemanticChangePageKey(
                sort_value=item.finding_id,
                stable_id=item.finding_id,
            ),
        )

    def list_impacts(
        self,
        workspace_id: str,
        report_id: str,
        *,
        filters: SemanticChangeImpactFilter,
        page_size: int,
        after: SemanticChangePageKey | None,
    ) -> SemanticChangeStorePage[SemanticChangeImpactPublic]:
        self.impact_calls.append((workspace_id, report_id, filters, page_size, after))
        candidates = [
            value
            for value in self.impacts.get((workspace_id, report_id), ())
            if filters.kind is None or value.kind is filters.kind
        ]
        candidates.sort(
            key=lambda item: (
                item.kind.value,
                item.artifact_id,
                item.artifact_version or 0,
            )
        )
        candidates = _after(
            candidates,
            after,
            lambda item: f"{item.kind.value}:{item.artifact_id}:{item.artifact_version or 0}",
        )
        return _page(
            candidates,
            page_size,
            lambda item: SemanticChangePageKey(
                sort_value=item.kind.value,
                stable_id=(f"{item.kind.value}:{item.artifact_id}:{item.artifact_version or 0}"),
            ),
        )


@dataclass
class _Fixture:
    store: _Store
    clock: _Clock
    cursor_ttl: timedelta = timedelta(minutes=15)

    @classmethod
    def create(
        cls,
        *,
        cursor_ttl: timedelta = timedelta(minutes=15),
    ) -> _Fixture:
        reports = {
            ("workspace-a", REPORT_A): _report(
                "workspace-a",
                REPORT_A,
                status=SemanticChangeStatus.BLOCKED,
                inspected_at=NOW,
                finding_count=2,
            ),
            ("workspace-a", REPORT_B): _report(
                "workspace-a",
                REPORT_B,
                status=SemanticChangeStatus.CURRENT,
                inspected_at=NOW - timedelta(minutes=1),
                finding_count=0,
            ),
            ("workspace-a", REPORT_C_STALE): _report(
                "workspace-a",
                REPORT_C_STALE,
                status=SemanticChangeStatus.REVIEW_REQUIRED,
                inspected_at=NOW - timedelta(minutes=2),
                finding_count=1,
            ),
            ("workspace-b", REPORT_D_TENANT_B): _report(
                "workspace-b",
                REPORT_D_TENANT_B,
                status=SemanticChangeStatus.BLOCKED,
                inspected_at=NOW,
                finding_count=1,
            ),
        }
        findings = {
            ("workspace-a", REPORT_A): (
                _finding(
                    "workspace-a",
                    REPORT_A,
                    FINDING_A,
                    kind=SemanticChangeKind.PHYSICAL_TYPE_CHANGED,
                    severity=SemanticChangeSeverity.BLOCKING,
                ),
                _finding(
                    "workspace-a",
                    REPORT_A,
                    FINDING_B,
                    kind=SemanticChangeKind.FIELD_DEFINITION_CHANGED,
                    severity=SemanticChangeSeverity.REVIEW_REQUIRED,
                ),
            )
        }
        impacts = {
            ("workspace-a", REPORT_A): (
                _impact(
                    "workspace-a",
                    REPORT_A,
                    kind=SemanticImpactKind.MAPPING,
                    artifact_id="customer.contract_id",
                    finding_ids=(FINDING_A, FINDING_B),
                ),
                _impact(
                    "workspace-a",
                    REPORT_A,
                    kind=SemanticImpactKind.WORKFLOW,
                    artifact_id="workflow-secondary-holders",
                    finding_ids=(FINDING_A,),
                ),
            )
        }
        return cls(
            store=_Store(
                reports=reports,
                findings=findings,
                impacts=impacts,
                stale={("workspace-a", REPORT_C_STALE)},
            ),
            clock=_Clock(),
            cursor_ttl=cursor_ttl,
        )

    def semantic_services(self) -> SemanticChangeHttpServices:
        cursors = SignedSemanticChangeCursorCodec(
            signing_key=SIGNING_KEY,
            ttl=self.cursor_ttl,
        )
        return SemanticChangeHttpServices(
            list_reports=ListSemanticChangeReports(
                store=self.store,
                cursors=cursors,
                clock=self.clock,
            ),
            inspect_report=InspectSemanticChangeReport(
                store=self.store,
                clock=self.clock,
            ),
            list_findings=ListSemanticChangeFindings(
                store=self.store,
                cursors=cursors,
                clock=self.clock,
            ),
            list_impacts=ListSemanticChangeImpacts(
                store=self.store,
                cursors=cursors,
                clock=self.clock,
            ),
        )


def _client(
    fixture: _Fixture | None = None,
    *,
    semantic_changes: SemanticChangeHttpServices | None = None,
) -> TestClient:
    unused = _UnusedJobPort()
    selected = fixture.semantic_services() if fixture is not None else semantic_changes
    clock = fixture.clock if fixture is not None else _Clock()
    return TestClient(
        create_http_app(
            ApiHttpServices(
                authenticator=_Authenticator(),
                clock=clock,
                submit=unused,  # type: ignore[arg-type]
                inspect=unused,  # type: ignore[arg-type]
                cancel=unused,  # type: ignore[arg-type]
                readiness=_Readiness(),
                semantic_changes=selected,
            )
        ),
        raise_server_exceptions=False,
    )


def _headers(token: str = "analyst-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _report(
    workspace_id: str,
    report_id: str,
    *,
    status: SemanticChangeStatus,
    inspected_at: datetime,
    finding_count: int,
) -> SemanticChangeReportPublic:
    return SemanticChangeReportPublic(
        workspace_id=workspace_id,
        report_id=report_id,
        status=status,
        pointer_generation=7,
        pointer_fingerprint="1" * 64,
        registry_version=3,
        registry_fingerprint="2" * 64,
        catalog_generation_count=2,
        observation_fingerprint="3" * 64,
        baseline_revision=2,
        baseline_fingerprint="4" * 64,
        finding_count=finding_count,
        mapping_impact_count=1,
        join_impact_count=0,
        workflow_impact_count=1,
        recipe_impact_count=0,
        impacts_complete=True,
        dependency_watermark=11,
        impact_set_fingerprint="5" * 64,
        inspected_at=inspected_at,
        fingerprint=report_id.removeprefix("report_"),
    )


def _finding(
    workspace_id: str,
    report_id: str,
    finding_id: str,
    *,
    kind: SemanticChangeKind,
    severity: SemanticChangeSeverity,
) -> SemanticChangeFindingPublic:
    return SemanticChangeFindingPublic(
        workspace_id=workspace_id,
        report_id=report_id,
        finding_id=finding_id,
        kind=kind,
        severity=severity,
        target_kind=SemanticChangeTargetKind.MAPPING,
        target_id="customer.contract_id",
        target_version=2,
        previous_fingerprint="6" * 64,
        current_fingerprint="7" * 64,
        risks=("query_semantics_changed",),
        fingerprint=finding_id.removeprefix("finding_"),
    )


def _impact(
    workspace_id: str,
    report_id: str,
    *,
    kind: SemanticImpactKind,
    artifact_id: str,
    finding_ids: tuple[str, ...],
) -> SemanticChangeImpactPublic:
    return SemanticChangeImpactPublic(
        workspace_id=workspace_id,
        report_id=report_id,
        kind=kind,
        artifact_id=artifact_id,
        artifact_version=2,
        finding_ids=finding_ids,
        fingerprint="8" * 64,
    )


def _after(
    values: list[object],
    after: SemanticChangePageKey | None,
    identifier: object,
) -> list[object]:
    if after is None:
        return values
    identify = identifier
    assert callable(identify)
    for index, value in enumerate(values):
        if identify(value) == after.stable_id:
            return values[index + 1 :]
    return []


def _page(
    values: list[object],
    page_size: int,
    key: object,
) -> SemanticChangeStorePage[object]:
    make_key = key
    assert callable(make_key)
    items = tuple(values[:page_size])
    has_more = len(values) > page_size
    return SemanticChangeStorePage(
        items=items,
        page_size=page_size,
        rows_read=len(items) + (1 if has_more else 0),
        has_more=has_more,
        last_key=None if not items else make_key(items[-1]),
    )


def test_report_routes_are_authenticated_bounded_and_minimized() -> None:
    fixture = _Fixture.create()
    with _client(fixture) as client:
        page = client.get(
            "/v1/semantic-changes/reports",
            headers=_headers(),
            params={"page_size": 1},
        )
        detail = client.get(
            f"/v1/semantic-changes/reports/{REPORT_A}",
            headers=_headers("auditor-token"),
        )
        too_large = client.get(
            "/v1/semantic-changes/reports",
            headers=_headers(),
            params={"page_size": 51},
        )
        unauthenticated = client.get("/v1/semantic-changes/reports")

    assert page.status_code == 200
    assert page.json()["resource"] == "reports"
    assert len(page.json()["items"]) == 1
    assert page.json()["next_cursor"]
    assert detail.status_code == 200
    assert detail.json()["report_id"] == REPORT_A
    assert detail.json()["finding_count"] == 2
    assert too_large.status_code == 422
    assert unauthenticated.status_code == 401
    assert fixture.store.report_list_calls[0][2] == 1
    encoded = f"{page.text} {detail.text}".casefold()
    for forbidden in (
        "workspace",
        "physical_field",
        "field_definition",
        "source_value",
        "credential",
        "audit_key",
        "sql",
        "token",
    ):
        assert forbidden not in encoded


def test_finding_and_impact_routes_filter_and_page_without_raw_evidence() -> None:
    fixture = _Fixture.create()
    with _client(fixture) as client:
        findings = client.get(
            f"/v1/semantic-changes/reports/{REPORT_A}/findings",
            headers=_headers(),
            params={
                "severity": "blocking",
                "kind": "physical_type_changed",
                "page_size": 1,
            },
        )
        impacts = client.get(
            f"/v1/semantic-changes/reports/{REPORT_A}/impacts",
            headers=_headers(),
            params={"kind": "workflow", "page_size": 1},
        )

    assert findings.status_code == 200
    assert findings.json()["resource"] == "findings"
    assert findings.json()["items"][0]["finding_id"] == FINDING_A
    assert findings.json()["items"][0]["target_id"] == "customer.contract_id"
    assert impacts.status_code == 200
    assert impacts.json()["resource"] == "impacts"
    assert impacts.json()["items"][0]["artifact_id"] == "workflow-secondary-holders"
    assert fixture.store.finding_calls[0][2] == SemanticChangeFindingFilter(
        kind=SemanticChangeKind.PHYSICAL_TYPE_CHANGED,
        severity=SemanticChangeSeverity.BLOCKING,
    )
    assert fixture.store.impact_calls[0][2] == SemanticChangeImpactFilter(
        kind=SemanticImpactKind.WORKFLOW
    )
    encoded = f"{findings.text} {impacts.text}".casefold()
    for forbidden in (
        "workspace",
        "asset_metadata",
        "field_path",
        "definition",
        "source_value",
        "sql",
    ):
        assert forbidden not in encoded


def test_cursor_is_hmac_bound_to_tenant_report_filter_and_resource() -> None:
    fixture = _Fixture.create()
    with _client(fixture) as client:
        first = client.get(
            f"/v1/semantic-changes/reports/{REPORT_A}/findings",
            headers=_headers(),
            params={"page_size": 1},
        )
        cursor = first.json()["next_cursor"]
        valid = client.get(
            f"/v1/semantic-changes/reports/{REPORT_A}/findings",
            headers=_headers(),
            params={"page_size": 1, "cursor": cursor},
        )
        wrong_tenant = client.get(
            f"/v1/semantic-changes/reports/{REPORT_A}/findings",
            headers=_headers("tenant-b-token"),
            params={"page_size": 1, "cursor": cursor},
        )
        wrong_report = client.get(
            f"/v1/semantic-changes/reports/{REPORT_B}/findings",
            headers=_headers(),
            params={"page_size": 1, "cursor": cursor},
        )
        wrong_filter = client.get(
            f"/v1/semantic-changes/reports/{REPORT_A}/findings",
            headers=_headers(),
            params={"page_size": 1, "severity": "blocking", "cursor": cursor},
        )
        wrong_resource = client.get(
            f"/v1/semantic-changes/reports/{REPORT_A}/impacts",
            headers=_headers(),
            params={"page_size": 1, "cursor": cursor},
        )
        tampered = client.get(
            f"/v1/semantic-changes/reports/{REPORT_A}/findings",
            headers=_headers(),
            params={"page_size": 1, "cursor": f"{cursor[:-1]}A"},
        )

    assert first.status_code == 200
    assert valid.status_code == 200
    for response in (
        wrong_tenant,
        wrong_report,
        wrong_filter,
        wrong_resource,
        tampered,
    ):
        assert response.status_code == 404
        assert response.json()["code"] == "semantic_change_resource_unavailable"
    payload_segment = cursor.split(".", maxsplit=1)[0]
    payload = base64.urlsafe_b64decode(payload_segment + ("=" * (-len(payload_segment) % 4)))
    assert b"workspace-a" not in payload
    assert REPORT_A.encode() not in payload


def test_invalid_cursor_is_rejected_before_report_or_page_store_access() -> None:
    fixture = _Fixture.create()
    with _client(fixture) as client:
        first = client.get(
            f"/v1/semantic-changes/reports/{REPORT_A}/findings",
            headers=_headers(),
            params={"page_size": 1},
        )
        cursor = first.json()["next_cursor"]
        loads_before = len(fixture.store.report_load_calls)
        pages_before = len(fixture.store.finding_calls)
        denied = client.get(
            f"/v1/semantic-changes/reports/{REPORT_B}/findings",
            headers=_headers(),
            params={"page_size": 1, "cursor": cursor},
        )

    assert denied.status_code == 404
    assert len(fixture.store.report_load_calls) == loads_before
    assert len(fixture.store.finding_calls) == pages_before


def test_expired_cursor_and_stale_report_share_unavailable_boundary() -> None:
    fixture = _Fixture.create(cursor_ttl=timedelta(seconds=1))
    with _client(fixture) as client:
        first = client.get(
            "/v1/semantic-changes/reports",
            headers=_headers(),
            params={"page_size": 1},
        )
        cursor = first.json()["next_cursor"]
        fixture.clock.value = NOW + timedelta(seconds=1)
        expired = client.get(
            "/v1/semantic-changes/reports",
            headers=_headers(),
            params={"page_size": 1, "cursor": cursor},
        )
        stale = client.get(
            f"/v1/semantic-changes/reports/{REPORT_C_STALE}",
            headers=_headers(),
        )

    assert expired.status_code == stale.status_code == 404
    assert expired.json()["code"] == stale.json()["code"]
    assert expired.json()["title"] == stale.json()["title"]


def test_unknown_cross_tenant_stale_and_role_denial_are_indistinguishable() -> None:
    fixture = _Fixture.create()
    paths_and_tokens = (
        (REPORT_UNKNOWN, "analyst-token"),
        (REPORT_D_TENANT_B, "analyst-token"),
        (REPORT_C_STALE, "analyst-token"),
        (REPORT_A, "tenant-b-token"),
        (REPORT_A, "no-role-token"),
    )
    with _client(fixture) as client:
        responses = [
            client.get(
                f"/v1/semantic-changes/reports/{report_id}",
                headers=_headers(token),
            )
            for report_id, token in paths_and_tokens
        ]

    assert {
        (response.status_code, response.json()["code"], response.json()["title"])
        for response in responses
    } == {
        (
            404,
            "semantic_change_resource_unavailable",
            "The semantic-change resource is not available.",
        )
    }


def test_report_cursor_is_filter_bound_and_never_allows_more_than_fifty() -> None:
    fixture = _Fixture.create()
    with _client(fixture) as client:
        first = client.get(
            "/v1/semantic-changes/reports",
            headers=_headers(),
            params={"page_size": 1},
        )
        changed_filter = client.get(
            "/v1/semantic-changes/reports",
            headers=_headers(),
            params={
                "page_size": 1,
                "status": "blocked",
                "cursor": first.json()["next_cursor"],
            },
        )
        maximum = client.get(
            "/v1/semantic-changes/reports",
            headers=_headers(),
            params={"page_size": 50},
        )

    assert changed_filter.status_code == 404
    assert maximum.status_code == 200
    assert len(maximum.json()["items"]) <= 50
    assert fixture.store.report_list_calls[-1][2] == 50


def test_semantic_change_http_surface_is_read_only_and_optional() -> None:
    fixture = _Fixture.create()
    with _client(fixture) as client:
        mutation = client.post(
            "/v1/semantic-changes/reports",
            headers=_headers(),
            json={},
        )
    with _client() as client:
        unavailable = client.get(
            "/v1/semantic-changes/reports",
            headers=_headers(),
        )

    assert mutation.status_code == 405
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "semantic_change_service_unavailable"
