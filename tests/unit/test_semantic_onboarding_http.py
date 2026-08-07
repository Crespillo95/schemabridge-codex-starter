"""Authenticated M33 semantic-onboarding HTTP boundary tests."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from tests.unit.test_semantic_onboarding_use_cases import (
    _BaseReader,
    _Catalog,
    _Clock,
    _request,
)

from schemabridge.adapters.storage.semantic_onboarding import (
    InMemorySemanticOnboardingStore,
)
from schemabridge.application.authentication import AuthenticationBoundaryError
from schemabridge.application.semantic_onboarding import (
    CreateSemanticOnboardingDraft,
    DecideSemanticOnboarding,
    InspectSemanticOnboardingDraft,
    ListSemanticOnboardingDrafts,
    PreflightSemanticOnboardingDraft,
    PrepareSemanticOnboardingPublication,
)
from schemabridge.application.semantic_onboarding_authorization import (
    SemanticOnboardingAuthorizationPolicy,
)
from schemabridge.bootstrap import build_api_http_services
from schemabridge.domain.background_jobs import BackgroundJob, JobSubmissionResult
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope
from schemabridge.entrypoints.http.app import (
    ApiHttpServices,
    SemanticOnboardingHttpServices,
    create_http_app,
)
from schemabridge.entrypoints.http.schemas import (
    SemanticOnboardingDecisionRequest,
    SemanticOnboardingDraftCreateRequest,
    SemanticOnboardingDraftResponse,
    SemanticOnboardingPreflightRequest,
    SemanticOnboardingPreparationRequest,
)

NOW = datetime(2026, 8, 2, 10, 0, tzinfo=UTC)
AUTHORITY_FIELDS = {"actor", "actor_id", "workspace", "workspace_id"}


class _UnusedJobSubmission:
    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        workflow_id: str,
        expected_workflow_revision: int,
        expected_plan_fingerprint: str,
        confirmation: str,
        idempotency_key: str,
    ) -> JobSubmissionResult:
        del (
            principal,
            workflow_id,
            expected_workflow_revision,
            expected_plan_fingerprint,
            confirmation,
            idempotency_key,
        )
        raise AssertionError("semantic onboarding HTTP must not invoke execution jobs")


class _UnusedJobInspection:
    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        job_id: str,
    ) -> BackgroundJob:
        del principal, job_id
        raise AssertionError("semantic onboarding HTTP must not invoke execution jobs")


class _UnusedJobCancellation:
    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        job_id: str,
    ) -> BackgroundJob:
        del principal, job_id
        raise AssertionError("semantic onboarding HTTP must not invoke execution jobs")


class _Readiness:
    def require_ready(self) -> None:
        return None


@dataclass
class _Authenticator:
    calls: int = 0

    def authenticate(self, bearer_token: str, now: datetime) -> AuthenticatedPrincipal:
        assert now == NOW
        identities = {
            "analyst-a": ("workspace-a", "analyst-a", IdentityRole.ANALYST),
            "steward-a": ("workspace-a", "steward-a", IdentityRole.STEWARD),
            "publisher-a": ("workspace-a", "publisher-a", IdentityRole.PUBLISHER),
            "tenant-b": ("workspace-b", "steward-b", IdentityRole.STEWARD),
        }
        identity = identities.get(bearer_token)
        if identity is None:
            raise AuthenticationBoundaryError("invalid_bearer_token")
        self.calls += 1
        workspace_id, actor_id, role = identity
        return AuthenticatedPrincipal(
            actor_id=actor_id,
            workspace_id=workspace_id,
            roles=frozenset({role}),
            authentication_method=AuthenticationMethod.OIDC,
            authenticated_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=30),
        )


@dataclass
class _HttpHarness:
    client: TestClient
    store: InMemorySemanticOnboardingStore
    catalog: _Catalog
    authenticator: _Authenticator


def _harness() -> _HttpHarness:
    scope = SemanticRegistryScope(
        workspace_id="workspace-a",
        catalog_scope="postgres.production",
        registry_id="orders_registry",
    )
    request = _request()
    store = InMemorySemanticOnboardingStore()
    catalog = _Catalog(request, scope)
    registry_bases = _BaseReader()
    authorization = SemanticOnboardingAuthorizationPolicy()
    clock = _Clock()
    authenticator = _Authenticator()
    services = ApiHttpServices(
        authenticator=authenticator,
        clock=clock,
        submit=_UnusedJobSubmission(),
        inspect=_UnusedJobInspection(),
        cancel=_UnusedJobCancellation(),
        readiness=_Readiness(),
        semantic_onboarding=SemanticOnboardingHttpServices(
            preflight=PreflightSemanticOnboardingDraft(
                catalog,
                authorization,
                clock,
                scope,
            ),
            list_drafts=ListSemanticOnboardingDrafts(store, authorization, clock),
            create_draft=CreateSemanticOnboardingDraft(
                store,
                catalog,
                registry_bases,
                authorization,
                clock,
                scope,
            ),
            inspect_draft=InspectSemanticOnboardingDraft(store, authorization, clock),
            decide=DecideSemanticOnboarding(
                store,
                catalog,
                registry_bases,
                authorization,
                clock,
            ),
            prepare_publication=PrepareSemanticOnboardingPublication(
                store,
                catalog,
                registry_bases,
                authorization,
                clock,
            ),
        ),
    )
    return _HttpHarness(
        client=TestClient(create_http_app(services), raise_server_exceptions=False),
        store=store,
        catalog=catalog,
        authenticator=authenticator,
    )


def _headers(token: str, idempotency_key: str | None = None) -> dict[str, str]:
    values = {"Authorization": f"Bearer {token}"}
    if idempotency_key is not None:
        values["Idempotency-Key"] = idempotency_key
    return values


def _decision_body(draft_response: dict[str, object], target_id: str) -> dict[str, object]:
    draft = draft_response["draft"]
    assert isinstance(draft, dict)
    revision = draft["revision"]
    assert isinstance(revision, int)
    fingerprint = draft_response["draft_fingerprint"]
    assert isinstance(fingerprint, str)
    return {
        "target_id": target_id,
        "action": "approve",
        "expected_revision": revision,
        "confirmed_draft_fingerprint": fingerprint,
        "rationale": "The steward confirms the exact governed semantic evidence.",
        "evidence": [
            {
                "kind": "human_attestation",
                "detail": "The steward reviewed the catalog definition and owner evidence.",
                "reference": "ticket:SEM-42",
            }
        ],
    }


def test_decision_schema_rejects_padding_as_meaningful_rationale() -> None:
    with pytest.raises(ValidationError, match="12 meaningful characters"):
        SemanticOnboardingDecisionRequest(
            target_id="mapping-order-id",
            action="approve",
            expected_revision=1,
            confirmed_draft_fingerprint="a" * 64,
            rationale="ok          ",
        )


def _preflight_and_create_body(client: TestClient) -> tuple[dict[str, object], dict[str, object]]:
    response = client.post(
        "/v1/semantic-onboarding/preflight",
        headers=_headers("analyst-a"),
        json={
            "connection_id": "warehouse-a",
            "selections": [{"asset_id": "asset-orders", "field_path": ["order_id"]}],
        },
    )
    assert response.status_code == 200
    preflight = response.json()
    body = _request().model_dump(mode="json")
    body["catalog_generation"] = preflight["catalog_generation"]
    body["catalog_generation_fingerprint"] = preflight["catalog_generation_fingerprint"]
    body["expected_base_registry"] = preflight["base_registry"]
    body["confirmed_preflight_fingerprint"] = preflight["preflight_fingerprint"]
    mappings = body["mappings"]
    observations = preflight["observations"]
    assert isinstance(mappings, list) and isinstance(observations, list)
    for mapping, observation in zip(mappings, observations, strict=True):
        assert isinstance(mapping, dict) and isinstance(observation, dict)
        locator = observation["locator"]
        assert isinstance(locator, dict)
        asset = locator["asset"]
        assert isinstance(asset, dict)
        mapping["asset_id"] = asset["asset_id"]
        mapping["field_path"] = locator["field_path"]
        mapping["expected_asset_metadata_fingerprint"] = observation["asset_metadata_fingerprint"]
        mapping["expected_field_metadata_fingerprint"] = observation["field_metadata_fingerprint"]
        mapping["physical_field"] = observation["physical_field"]
    return preflight, body


def test_preflight_http_rejects_nested_field_paths() -> None:
    harness = _harness()

    with harness.client as client:
        response = client.post(
            "/v1/semantic-onboarding/preflight",
            headers=_headers("analyst-a"),
            json={
                "connection_id": "warehouse-a",
                "selections": [{"asset_id": "asset-orders", "field_path": ["order", "id"]}],
            },
        )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "step",
    (
        {"operation": "validate_regex", "pattern": r"(?P<identifier>[0-9]+)"},
        {"operation": "parse_date", "format": "%Y-%m-%d"},
        {"operation": "pad_left", "length": 10_000_000},
    ),
)
def test_create_http_rejects_nonportable_or_unbounded_transformations(
    step: dict[str, object],
) -> None:
    harness = _harness()

    with harness.client as client:
        _, request_body = _preflight_and_create_body(client)
        mappings = request_body["mappings"]
        assert isinstance(mappings, list)
        mapping = mappings[0]
        assert isinstance(mapping, dict)
        mapping["transformation_plan"] = {"version": 1, "steps": [step]}
        response = client.post(
            "/v1/semantic-onboarding/drafts",
            headers=_headers("analyst-a", "create-invalid-transform-http-1"),
            json=request_body,
        )

    assert response.status_code == 422
    assert (
        harness.store.list_for_workspace(
            "workspace-a",
            owner_actor_id=None,
            limit=50,
        )
        == ()
    )


def test_empty_create_review_and_prepare_http_flow_stays_non_executable() -> None:
    harness = _harness()

    with harness.client as client:
        empty = client.get(
            "/v1/semantic-onboarding/drafts",
            headers=_headers("analyst-a"),
        )
        preflight, request_body = _preflight_and_create_body(client)
        created = client.post(
            "/v1/semantic-onboarding/drafts",
            headers=_headers("analyst-a", "create-orders-http-0001"),
            json=request_body,
        )
        create_replay = client.post(
            "/v1/semantic-onboarding/drafts",
            headers=_headers("analyst-a", "create-orders-http-0001"),
            json=request_body,
        )
        owner_inspected = client.get(
            "/v1/semantic-onboarding/drafts/orders-onboarding",
            headers=_headers("analyst-a"),
        )
        model_decision = client.post(
            "/v1/semantic-onboarding/drafts/orders-onboarding/model-decisions",
            headers=_headers("steward-a", "approve-model-http-001"),
            json=_decision_body(created.json(), "Order"),
        )
        mapping_decision = client.post(
            "/v1/semantic-onboarding/drafts/orders-onboarding/mapping-decisions",
            headers=_headers("steward-a", "approve-mapping-http-1"),
            json=_decision_body(model_decision.json(), "mapping-order-id"),
        )
        mapping_payload = mapping_decision.json()
        prepared = client.post(
            "/v1/semantic-onboarding/drafts/orders-onboarding/prepare-publication",
            headers=_headers("publisher-a", "prepare-orders-http-01"),
            json={
                "expected_revision": mapping_payload["draft"]["revision"],
                "confirmed_draft_fingerprint": mapping_payload["draft_fingerprint"],
            },
        )
        prepare_replay = client.post(
            "/v1/semantic-onboarding/drafts/orders-onboarding/prepare-publication",
            headers=_headers("publisher-a", "prepare-orders-http-01"),
            json={
                "expected_revision": mapping_payload["draft"]["revision"],
                "confirmed_draft_fingerprint": mapping_payload["draft_fingerprint"],
            },
        )
        inspected = client.get(
            "/v1/semantic-onboarding/drafts/orders-onboarding",
            headers=_headers("publisher-a"),
        )
        bounded = client.get(
            "/v1/semantic-onboarding/drafts/orders-onboarding?history_limit=1",
            headers=_headers("publisher-a"),
        )
        invalid_history_limit = client.get(
            "/v1/semantic-onboarding/drafts/orders-onboarding?history_limit=51",
            headers=_headers("publisher-a"),
        )

    assert empty.status_code == 200
    assert empty.json() == {
        "resource": "semantic_onboarding_drafts",
        "state": "not_configured",
        "items": [],
    }
    assert preflight["catalog_generation"] == 7
    preflight_observations = preflight["observations"]
    assert isinstance(preflight_observations, list)
    first_observation = preflight_observations[0]
    assert isinstance(first_observation, dict)
    assert first_observation["physical_field"] == "sales.orders.order_id"
    assert preflight["external_writes_performed"] is False
    assert "sql" not in str(preflight).casefold()
    assert created.status_code == 201
    assert create_replay.status_code == 200
    assert create_replay.json()["replayed"] is True
    assert create_replay.json()["draft_fingerprint"] == created.json()["draft_fingerprint"]
    assert created.json()["draft"]["model"]["status"] == "needs_review"
    assert {item["status"] for item in created.json()["draft"]["mappings"]} == {"needs_review"}
    assert created.json()["external_writes_performed"] is False
    assert "sql" not in str(created.json()).casefold()
    assert owner_inspected.status_code == 200
    assert owner_inspected.json()["audit_visible"] is False
    assert owner_inspected.json()["history_truncated"] is False
    assert owner_inspected.json()["decisions"] == []
    assert owner_inspected.json()["proposals"] == []
    assert owner_inspected.json()["audit"] == []
    assert model_decision.status_code == 201
    assert mapping_decision.status_code == 201
    assert prepared.status_code == 201
    assert prepare_replay.status_code == 200
    assert prepare_replay.json()["replayed"] is True
    assert prepare_replay.json()["proposal"] == prepared.json()["proposal"]
    assert prepared.json()["draft"]["status"] == "ready_for_publication"
    assert prepared.json()["proposal"]["external_writes_performed"] is False
    assert prepared.json()["external_writes_performed"] is False
    assert inspected.status_code == 200
    assert inspected.json()["audit_visible"] is True
    assert inspected.json()["history_truncated"] is False
    assert inspected.json()["draft_fingerprint"] == prepared.json()["draft_fingerprint"]
    assert len(inspected.json()["decisions"]) == 2
    assert len(inspected.json()["audit"]) == 4
    assert bounded.status_code == 200
    assert bounded.json()["history_truncated"] is True
    assert len(bounded.json()["decisions"]) == 1
    assert bounded.json()["decisions"][0]["target_id"] == "mapping-order-id"
    assert len(bounded.json()["proposals"]) == 1
    assert len(bounded.json()["audit"]) == 1
    assert bounded.json()["audit"][0]["event"] == "publication_prepared"
    assert invalid_history_limit.status_code == 422
    for history_field in ("decisions", "proposals", "audit"):
        oversized = inspected.json()
        oversized[history_field] = [oversized[history_field][0]] * 51
        with pytest.raises(ValidationError):
            SemanticOnboardingDraftResponse.model_validate(oversized)


def test_authority_fields_are_rejected_before_authentication_or_storage_access() -> None:
    harness = _harness()
    body = _request().model_dump(mode="json")
    body.update({"actor_id": "forged", "workspace_id": "workspace-b"})

    with harness.client as client:
        response = client.post(
            "/v1/semantic-onboarding/drafts",
            headers=_headers("analyst-a", "create-forged-http-001"),
            json=body,
        )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"
    assert harness.authenticator.calls == 0
    assert harness.catalog.calls == 0
    assert harness.store.list_for_workspace("workspace-a", owner_actor_id=None, limit=50) == ()


def test_preflight_forbids_authority_fields_and_is_tenant_bound() -> None:
    harness = _harness()
    forged_body = {
        "connection_id": "warehouse-a",
        "workspace_id": "workspace-b",
        "selections": [{"asset_id": "asset-orders", "field_path": ["order_id"]}],
    }

    with harness.client as client:
        forged = client.post(
            "/v1/semantic-onboarding/preflight",
            headers=_headers("analyst-a"),
            json=forged_body,
        )
        cross_tenant = client.post(
            "/v1/semantic-onboarding/preflight",
            headers=_headers("tenant-b"),
            json={
                "connection_id": "warehouse-a",
                "selections": [{"asset_id": "asset-orders", "field_path": ["order_id"]}],
            },
        )

    assert forged.status_code == 422
    assert cross_tenant.status_code == 404
    assert cross_tenant.json()["code"] == "semantic_onboarding_resource_unavailable"
    assert harness.catalog.calls == 0


def test_create_rejects_unconfirmed_preflight_without_mutation() -> None:
    harness = _harness()
    with harness.client as client:
        _, body = _preflight_and_create_body(client)
        body["confirmed_preflight_fingerprint"] = "f" * 64
        response = client.post(
            "/v1/semantic-onboarding/drafts",
            headers=_headers("analyst-a", "create-stale-preflight-01"),
            json=body,
        )

    assert response.status_code == 409
    assert response.json()["code"] == "semantic_onboarding_stale_catalog"
    assert harness.store.list_for_workspace("workspace-a", owner_actor_id=None, limit=50) == ()


def test_cross_tenant_and_unknown_drafts_share_the_same_bounded_response() -> None:
    harness = _harness()
    with harness.client as client:
        _, body = _preflight_and_create_body(client)
        created = client.post(
            "/v1/semantic-onboarding/drafts",
            headers=_headers("analyst-a", "create-tenant-http-0001"),
            json=body,
        )
        cross_tenant = client.get(
            "/v1/semantic-onboarding/drafts/orders-onboarding",
            headers=_headers("tenant-b"),
        )
        unknown = client.get(
            "/v1/semantic-onboarding/drafts/unknown-onboarding",
            headers=_headers("tenant-b"),
        )

    assert created.status_code == 201
    assert cross_tenant.status_code == unknown.status_code == 404
    for key in ("type", "title", "status", "code"):
        assert cross_tenant.json()[key] == unknown.json()[key]
    assert cross_tenant.json()["code"] == "semantic_onboarding_resource_unavailable"


def test_permission_and_stale_catalog_failures_use_only_the_safe_problem_vocabulary() -> None:
    harness = _harness()
    with harness.client as client:
        _, body = _preflight_and_create_body(client)
        created = client.post(
            "/v1/semantic-onboarding/drafts",
            headers=_headers("analyst-a", "create-errors-http-001"),
            json=body,
        )
        decision_body = _decision_body(created.json(), "Order")
        denied = client.post(
            "/v1/semantic-onboarding/drafts/orders-onboarding/model-decisions",
            headers=_headers("analyst-a", "analyst-decision-http1"),
            json=decision_body,
        )
        harness.catalog.fail = True
        stale = client.post(
            "/v1/semantic-onboarding/drafts/orders-onboarding/model-decisions",
            headers=_headers("steward-a", "stale-decision-http-01"),
            json=decision_body,
        )

    assert denied.status_code == 403
    assert denied.json()["code"] == "permission_denied"
    assert stale.status_code == 409
    assert stale.json()["code"] == "semantic_onboarding_stale_catalog"
    assert set(stale.json()) == {"type", "title", "status", "code", "request_id"}
    assert "catalog evidence unavailable" not in str(stale.json()).casefold()
    assert len(harness.store.list_audit("workspace-a", "orders-onboarding")) == 1


def test_request_models_have_no_representable_actor_or_workspace_authority() -> None:
    for model in (
        SemanticOnboardingPreflightRequest,
        SemanticOnboardingDraftCreateRequest,
        SemanticOnboardingDecisionRequest,
        SemanticOnboardingPreparationRequest,
    ):
        assert AUTHORITY_FIELDS.isdisjoint(model.model_fields)
        assert model.model_config["extra"] == "forbid"


def test_api_composition_contains_no_source_llm_or_datahub_writer_dependency() -> None:
    source = inspect.getsource(build_api_http_services)

    assert "PostgresSemanticOnboardingStore" in source
    assert "PostgresSemanticOnboardingCatalogEvidence" in source
    assert "AuthoritativeSemanticOnboardingRegistryBaseReader" in source
    assert "PostgresRegistryChangeStore" in source
    assert "PostgresExecutionTargetResolver" in source
    assert "schemabridge.adapters.datahub" not in source
    assert "build_registry_version_reader(" not in source
    assert "DataHubRegistry" not in source
    assert "IntentParser" not in source
    assert "RoutedPostgresQueryConnector" not in source
