"""Authenticated, tenant-safe HTTP contract for M35 Phase-A join changes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from tests.unit.test_registry_change_authoring import NOW, _Harness

from schemabridge.application.authentication import AuthenticationBoundaryError
from schemabridge.application.registry_changes import (
    InspectRegistryJoinChange,
    ListRegistryJoinChanges,
)
from schemabridge.domain.background_jobs import BackgroundJob, JobSubmissionResult
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)
from schemabridge.entrypoints.http.app import (
    ApiHttpServices,
    RegistryChangeHttpServices,
    create_http_app,
)
from schemabridge.entrypoints.http.schemas import (
    RegistryJoinDecisionRequest,
    RegistryJoinDraftFinalizationRequest,
    RegistryJoinPreparationRequest,
    RegistryJoinProfileRequest,
)


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
        raise AssertionError("registry-change HTTP must not invoke execution jobs")


class _UnusedJobInspection:
    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        job_id: str,
    ) -> BackgroundJob:
        del principal, job_id
        raise AssertionError("registry-change HTTP must not invoke execution jobs")


class _UnusedJobCancellation:
    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        job_id: str,
    ) -> BackgroundJob:
        del principal, job_id
        raise AssertionError("registry-change HTTP must not invoke execution jobs")


class _Readiness:
    def require_ready(self) -> None:
        return None


@dataclass
class _Authenticator:
    def authenticate(self, bearer_token: str, now: datetime) -> AuthenticatedPrincipal:
        identities = {
            "analyst-a": ("workspace-a", "analyst-owner", IdentityRole.ANALYST),
            "steward-a": ("workspace-a", "steward-reviewer", IdentityRole.STEWARD),
            "publisher-a": ("workspace-a", "publisher-separate", IdentityRole.PUBLISHER),
            "auditor-a": ("workspace-a", "auditor-only", IdentityRole.AUDITOR),
            "tenant-b": ("workspace-b", "steward-b", IdentityRole.STEWARD),
        }
        identity = identities.get(bearer_token)
        if identity is None:
            raise AuthenticationBoundaryError("invalid_bearer_token")
        workspace_id, actor_id, role = identity
        return AuthenticatedPrincipal(
            actor_id=actor_id,
            workspace_id=workspace_id,
            roles=frozenset({role}),
            authentication_method=AuthenticationMethod.OIDC,
            authenticated_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(minutes=30),
        )


@dataclass
class _HttpHarness:
    domain: _Harness
    client: TestClient


def _harness() -> _HttpHarness:
    domain = _Harness.create()
    services = ApiHttpServices(
        authenticator=_Authenticator(),
        clock=domain.clock,
        submit=_UnusedJobSubmission(),
        inspect=_UnusedJobInspection(),
        cancel=_UnusedJobCancellation(),
        readiness=_Readiness(),
        registry_changes=RegistryChangeHttpServices(
            request_join_profile=domain.request_use_case(),
            list_join_changes=ListRegistryJoinChanges(
                domain.store,
                domain.policy,
                domain.clock,
            ),
            inspect_join_change=InspectRegistryJoinChange(
                domain.store,
                domain.policy,
                domain.clock,
            ),
            finalize_join_draft=domain.finalize_use_case(),
            decide_join=domain.decide_use_case(),
            prepare_join_publication=domain.prepare_use_case(),
        ),
    )
    return _HttpHarness(
        domain=domain,
        client=TestClient(create_http_app(services), raise_server_exceptions=False),
    )


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    values = {"Authorization": f"Bearer {token}"}
    if key is not None:
        values["Idempotency-Key"] = key
    return values


def test_registry_join_decision_rejects_padded_rationale() -> None:
    with pytest.raises(ValidationError, match="12 meaningful characters"):
        RegistryJoinDecisionRequest(
            action="approve",
            expected_revision=1,
            confirmed_draft_fingerprint="a" * 64,
            rationale="ok          ",
        )


def test_registry_join_http_commands_have_no_client_controlled_tenant_or_actor() -> None:
    authority_fields = {"actor", "actor_id", "workspace", "workspace_id"}
    for model in (
        RegistryJoinProfileRequest,
        RegistryJoinDraftFinalizationRequest,
        RegistryJoinDecisionRequest,
        RegistryJoinPreparationRequest,
    ):
        assert authority_fields.isdisjoint(model.model_fields)
        assert model.model_config["extra"] == "forbid"


def test_registry_join_http_full_flow_is_exact_and_sanitized() -> None:
    harness = _harness()
    request_key = "registry-http-request-0001"
    body = harness.domain.request_input.model_dump(mode="json")

    created = harness.client.post(
        "/v1/registry-changes/joins",
        headers=_headers("analyst-a", request_key),
        json=body,
    )
    assert created.status_code == 201
    created_body = created.json()
    assert created_body["external_writes_performed"] is False
    assert created_body["profile_job_status"] == "requested"
    assert "capability" not in created.text.lower()
    assert "lease" not in created.text.lower()

    replay = harness.client.post(
        "/v1/registry-changes/joins",
        headers=_headers("analyst-a", request_key),
        json=body,
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert harness.domain.queue.enqueue_calls == 1

    listed = harness.client.get(
        "/v1/registry-changes/joins?limit=20",
        headers=_headers("analyst-a"),
    )
    assert listed.status_code == 200
    assert listed.json()["items"][0]["change_id"] == body["change_id"]
    assert "physical_field" not in listed.text
    assert "proposal" not in listed.text

    harness.domain.queue.complete_current()
    harness.domain.clock.current = NOW + timedelta(minutes=2)
    finalized = harness.client.post(
        f"/v1/registry-changes/joins/{body['change_id']}/finalize",
        headers=_headers("analyst-a", "registry-http-finalize-0001"),
        json={
            "confirmed_authoring_fingerprint": created_body["authoring_fingerprint"],
        },
    )
    assert finalized.status_code == 201
    draft = finalized.json()["draft"]

    harness.domain.clock.current = NOW + timedelta(minutes=3)
    decided = harness.client.post(
        f"/v1/registry-changes/joins/{body['change_id']}/decide",
        headers=_headers("steward-a", "registry-http-decision-0001"),
        json={
            "action": "approve",
            "expected_revision": draft["revision"],
            "confirmed_draft_fingerprint": finalized.json()["draft_fingerprint"],
            "rationale": "Aggregate key evidence confirms this governed relationship safely.",
        },
    )
    assert decided.status_code == 201

    harness.domain.clock.current = NOW + timedelta(minutes=4)
    prepared = harness.client.post(
        f"/v1/registry-changes/joins/{body['change_id']}/prepare-publication",
        headers=_headers("publisher-a", "registry-http-prepare-0001"),
        json={
            "expected_revision": decided.json()["draft"]["revision"],
            "confirmed_draft_fingerprint": decided.json()["draft_fingerprint"],
        },
    )
    assert prepared.status_code == 201
    assert prepared.json()["proposal"]["proposal_kind"] == "add_join_v1"
    assert prepared.json()["external_writes_performed"] is False

    inspected = harness.client.get(
        f"/v1/registry-changes/joins/{body['change_id']}?history_limit=20",
        headers=_headers("publisher-a"),
    )
    assert inspected.status_code == 200
    assert inspected.json()["audit_visible"] is True
    assert [item["event"] for item in inspected.json()["audit"]] == [
        "profile_requested",
        "profile_job_bound",
        "draft_finalized",
        "decision_recorded",
        "publication_prepared",
    ]

    hidden = harness.client.get(
        f"/v1/registry-changes/joins/{body['change_id']}",
        headers=_headers("tenant-b"),
    )
    assert hidden.status_code == 404
    assert hidden.json()["code"] == "registry_change_resource_unavailable"


def test_registry_join_http_requires_one_idempotency_header_and_rbac() -> None:
    harness = _harness()
    body = harness.domain.request_input.model_dump(mode="json")

    missing = harness.client.post(
        "/v1/registry-changes/joins",
        headers=_headers("analyst-a"),
        json=body,
    )
    assert missing.status_code == 422
    assert missing.json()["code"] == "invalid_request"

    denied = harness.client.post(
        "/v1/registry-changes/joins",
        headers=_headers("auditor-a", "registry-http-denied-0001"),
        json=body,
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "permission_denied"
    assert (
        harness.domain.store.list_for_workspace(
            "workspace-a",
            owner_actor_id=None,
            limit=50,
        )
        == ()
    )


def test_registry_join_http_unconfigured_and_malformed_resources_fail_closed() -> None:
    domain = _Harness.create()
    services = ApiHttpServices(
        authenticator=_Authenticator(),
        clock=domain.clock,
        submit=_UnusedJobSubmission(),
        inspect=_UnusedJobInspection(),
        cancel=_UnusedJobCancellation(),
        readiness=_Readiness(),
    )
    client = TestClient(create_http_app(services), raise_server_exceptions=False)

    unavailable = client.get(
        "/v1/registry-changes/joins",
        headers=_headers("analyst-a"),
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "registry_change_service_unavailable"
    assert "dsn" not in unavailable.text.lower()

    malformed = client.get(
        "/v1/registry-changes/joins/INVALID;DROP",
        headers=_headers("analyst-a"),
    )
    assert malformed.status_code == 422
    assert malformed.json()["code"] == "invalid_request"
