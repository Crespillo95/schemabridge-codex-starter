"""Authenticated, tenant-safe HTTP contract for M35 Phase-B model changes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError
from tests.unit.test_registry_change_http import (
    _Authenticator,
    _Readiness,
    _UnusedJobCancellation,
    _UnusedJobInspection,
    _UnusedJobSubmission,
)
from tests.unit.test_registry_model_change_authoring import _Harness
from tests.unit.test_registry_model_changes import NOW

from schemabridge.application.registry_model_changes import (
    InspectRegistryModelChange,
    ListRegistryModelChanges,
)
from schemabridge.domain.joins import JoinProposal
from schemabridge.domain.registry_model_change_authoring import (
    RequestRegistryModelJoinProfileInput,
)
from schemabridge.entrypoints.http.app import (
    ApiHttpServices,
    RegistryModelChangeHttpServices,
    create_http_app,
)
from schemabridge.entrypoints.http.schemas import (
    RegistryModelChangeCreateRequest,
    RegistryModelDecisionRequest,
    RegistryModelPreparationRequest,
    RegistryModelProfileFinalizationRequest,
    RegistryModelProfileRequest,
)

_SECURITY_HEADERS = {
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}
_AUTHORITY_FIELDS = {"actor", "actor_id", "workspace", "workspace_id"}


@dataclass
class _HttpHarness:
    domain: _Harness
    client: TestClient


def _harness(*, configured: bool = True) -> _HttpHarness:
    domain = _Harness.create()
    model_services = None
    if configured:
        model_services = RegistryModelChangeHttpServices(
            request_profile=domain.request_profile_use_case(),
            finalize_profile=domain.finalize_profile_use_case(),
            create_change=domain.create_use_case(),
            list_changes=ListRegistryModelChanges(
                domain.store,
                domain.policy,
                domain.clock,
            ),
            inspect_change=InspectRegistryModelChange(
                domain.store,
                domain.policy,
                domain.clock,
            ),
            decide_change=domain.decide_use_case(),
            prepare_publication=domain.prepare_use_case(),
        )
    services = ApiHttpServices(
        authenticator=_Authenticator(),
        clock=domain.clock,
        submit=_UnusedJobSubmission(),
        inspect=_UnusedJobInspection(),
        cancel=_UnusedJobCancellation(),
        readiness=_Readiness(),
        registry_model_changes=model_services,
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


def _profile_body(harness: _Harness) -> dict[str, object]:
    registry = harness.versions.version.snapshot.registry
    incident = next(
        item
        for item in registry.join_contracts.contracts
        if harness.request.target_model_id.root
        in {
            item.left_key.logical_field.root.split(".", 1)[0],
            item.right_key.logical_field.root.split(".", 1)[0],
        }
    )
    proposal = JoinProposal(
        id=incident.id,
        left_key=incident.left_key,
        right_key=incident.right_key,
        default_join_type=incident.default_join_type,
    )
    return cast(
        dict[str, object],
        RequestRegistryModelJoinProfileInput(
            request_id="replace-customer-profile",
            change_id=harness.request.change_id,
            replacement_proposal_id=harness.source.proposal.id,
            expected_replacement_fingerprint=harness.source.proposal.fingerprint,
            target_model_id=harness.request.target_model_id,
            expected_base_registry=harness.request.expected_base_registry,
            join_id=incident.id,
            proposal=proposal,
            expected_execution_target_fingerprint=harness.targets.target.fingerprint,
        ).model_dump(mode="json"),
    )


def _assert_security_headers(response: Any) -> None:
    for name, expected in _SECURITY_HEADERS.items():
        assert response.headers[name] == expected
    assert len(response.headers["x-request-id"]) == 32


def _assert_no_external_writes(response: Any) -> None:
    assert response.json()["external_writes_performed"] is False
    _assert_security_headers(response)


def _assert_strict_model(
    model: type[BaseModel],
    valid_body: Mapping[str, object],
) -> None:
    assert _AUTHORITY_FIELDS.isdisjoint(model.model_fields)
    assert model.model_config["extra"] == "forbid"
    with pytest.raises(ValidationError):
        model.model_validate({**valid_body, "workspace_id": "workspace-forged"})


def test_registry_model_http_request_models_are_strict_and_have_no_client_authority() -> None:
    harness = _harness()
    profile = _profile_body(harness.domain)
    create = harness.domain.request.model_dump(mode="json")
    decision = {
        "action": "approve",
        "expected_revision": 1,
        "confirmed_draft_fingerprint": "a" * 64,
        "rationale": "The exact governed replacement evidence was reviewed.",
    }
    preparation = {
        "expected_revision": 2,
        "confirmed_draft_fingerprint": "b" * 64,
    }

    for model, body in (
        (RegistryModelProfileRequest, profile),
        (
            RegistryModelProfileFinalizationRequest,
            {"confirmed_authoring_fingerprint": "c" * 64},
        ),
        (RegistryModelChangeCreateRequest, create),
        (RegistryModelDecisionRequest, decision),
        (RegistryModelPreparationRequest, preparation),
    ):
        _assert_strict_model(model, body)

    with pytest.raises(ValidationError, match="12 meaningful characters"):
        RegistryModelDecisionRequest(
            action="approve",
            expected_revision=1,
            confirmed_draft_fingerprint="d" * 64,
            rationale="ok          ",
        )

    forged = harness.client.post(
        "/v1/registry-changes/models",
        headers=_headers("analyst-a", "registry-model-forged-0001"),
        json={**create, "workspace_id": "workspace-b"},
    )
    assert forged.status_code == 422
    assert forged.json()["code"] == "invalid_request"
    assert (
        harness.domain.store.list_for_workspace(
            "workspace-a",
            owner_actor_id=None,
            limit=50,
        )
        == ()
    )


def test_registry_model_http_full_flow_replays_exactly_and_stays_non_executable() -> None:
    harness = _harness()
    profile_key = "registry-model-http-profile-0001"
    profile_body = _profile_body(harness.domain)

    requested = harness.client.post(
        "/v1/registry-changes/model-profiles",
        headers=_headers("analyst-a", profile_key),
        json=profile_body,
    )
    assert requested.status_code == 201
    _assert_no_external_writes(requested)
    requested_payload = requested.json()
    assert requested_payload["replayed"] is False
    assert requested_payload["profile_job_status"] == "requested"
    assert requested_payload["witness"] is None
    assert "lease_capability" not in requested.text

    request_replay = harness.client.post(
        "/v1/registry-changes/model-profiles",
        headers=_headers("analyst-a", profile_key),
        json=profile_body,
    )
    assert request_replay.status_code == 200
    _assert_no_external_writes(request_replay)
    assert request_replay.json()["replayed"] is True
    assert (
        request_replay.json()["authoring_fingerprint"] == requested_payload["authoring_fingerprint"]
    )
    assert harness.domain.profile_queue.persisted_before_enqueue is True

    altered_profile = dict(profile_body)
    altered_profile["request_id"] = "replace-customer-profile-altered"
    profile_conflict = harness.client.post(
        "/v1/registry-changes/model-profiles",
        headers=_headers("analyst-a", profile_key),
        json=altered_profile,
    )
    assert profile_conflict.status_code == 409
    assert profile_conflict.json()["code"] == "registry_change_conflict"

    harness.domain.profile_queue.complete()
    harness.domain.clock.current = NOW + timedelta(minutes=8)
    finalize_key = "registry-model-http-finalize-0001"
    finalize_body = {
        "confirmed_authoring_fingerprint": requested_payload["authoring_fingerprint"],
    }
    finalized = harness.client.post(
        "/v1/registry-changes/model-profiles/replace-customer-profile/finalize",
        headers=_headers("analyst-a", finalize_key),
        json=finalize_body,
    )
    assert finalized.status_code == 201
    _assert_no_external_writes(finalized)
    assert finalized.json()["witness"] is not None

    finalize_replay = harness.client.post(
        "/v1/registry-changes/model-profiles/replace-customer-profile/finalize",
        headers=_headers("analyst-a", finalize_key),
        json=finalize_body,
    )
    assert finalize_replay.status_code == 200
    _assert_no_external_writes(finalize_replay)
    assert finalize_replay.json()["replayed"] is True
    assert finalize_replay.json()["witness"] == finalized.json()["witness"]
    finalize_conflict = harness.client.post(
        "/v1/registry-changes/model-profiles/replace-customer-profile/finalize",
        headers=_headers("analyst-a", finalize_key),
        json={"confirmed_authoring_fingerprint": "f" * 64},
    )
    assert finalize_conflict.status_code == 409
    assert finalize_conflict.json()["code"] == "registry_change_conflict"

    create_key = "registry-model-http-create-0001"
    create_body = harness.domain.request.model_dump(mode="json")
    created = harness.client.post(
        "/v1/registry-changes/models",
        headers=_headers("analyst-a", create_key),
        json=create_body,
    )
    assert created.status_code == 201
    _assert_no_external_writes(created)
    assert created.json()["replayed"] is False
    assert created.json()["proposal"] is None
    assert created.json()["draft"]["status"] == "needs_review"

    create_replay = harness.client.post(
        "/v1/registry-changes/models",
        headers=_headers("analyst-a", create_key),
        json=create_body,
    )
    assert create_replay.status_code == 200
    _assert_no_external_writes(create_replay)
    assert create_replay.json()["replayed"] is True
    assert create_replay.json()["draft_fingerprint"] == created.json()["draft_fingerprint"]

    altered_create = dict(create_body)
    altered_create["risks"] = ["A different risk changes the exact command."]
    create_conflict = harness.client.post(
        "/v1/registry-changes/models",
        headers=_headers("analyst-a", create_key),
        json=altered_create,
    )
    assert create_conflict.status_code == 409
    assert create_conflict.json()["code"] == "registry_change_conflict"

    listed = harness.client.get(
        "/v1/registry-changes/models?limit=20",
        headers=_headers("steward-a"),
    )
    assert listed.status_code == 200
    _assert_security_headers(listed)
    listed_payload = listed.json()
    assert listed_payload["state"] == "configured"
    assert len(listed_payload["items"]) == 1
    summary = listed_payload["items"][0]
    assert summary["change_id"] == create_body["change_id"]
    assert set(summary) == {
        "change_id",
        "owner_actor_id",
        "scope",
        "source_proposal_id",
        "source_proposal_fingerprint",
        "target_model_id",
        "kind",
        "base_registry_version",
        "base_registry_fingerprint",
        "status",
        "revision",
        "draft_fingerprint",
        "updated_at",
    }
    assert "mappings" not in listed.text.casefold()
    assert "evidence" not in listed.text.casefold()

    harness.domain.clock.current = NOW + timedelta(minutes=9)
    decision_key = "registry-model-http-decide-0001"
    decision_body = {
        "action": "approve",
        "expected_revision": created.json()["draft"]["revision"],
        "confirmed_draft_fingerprint": created.json()["draft_fingerprint"],
        "rationale": "The complete replacement and every incident join were reviewed.",
    }
    decided = harness.client.post(
        "/v1/registry-changes/models/replace-customer-change/decide",
        headers=_headers("steward-a", decision_key),
        json=decision_body,
    )
    assert decided.status_code == 201
    _assert_no_external_writes(decided)
    assert decided.json()["draft"]["status"] == "approved"

    decision_replay = harness.client.post(
        "/v1/registry-changes/models/replace-customer-change/decide",
        headers=_headers("steward-a", decision_key),
        json=decision_body,
    )
    assert decision_replay.status_code == 200
    _assert_no_external_writes(decision_replay)
    assert decision_replay.json()["replayed"] is True

    altered_decision = dict(decision_body)
    altered_decision["rationale"] = "A materially different rationale cannot reuse this key."
    decision_conflict = harness.client.post(
        "/v1/registry-changes/models/replace-customer-change/decide",
        headers=_headers("steward-a", decision_key),
        json=altered_decision,
    )
    assert decision_conflict.status_code == 409
    assert decision_conflict.json()["code"] == "registry_change_conflict"

    harness.domain.clock.current = NOW + timedelta(minutes=10)
    prepare_key = "registry-model-http-prepare-0001"
    prepare_body = {
        "expected_revision": decided.json()["draft"]["revision"],
        "confirmed_draft_fingerprint": decided.json()["draft_fingerprint"],
    }
    prepared = harness.client.post(
        "/v1/registry-changes/models/replace-customer-change/prepare-publication",
        headers=_headers("publisher-a", prepare_key),
        json=prepare_body,
    )
    assert prepared.status_code == 201
    _assert_no_external_writes(prepared)
    assert prepared.json()["replayed"] is False
    assert prepared.json()["proposal"]["proposal_kind"] == "replace_model_v1"
    assert prepared.json()["draft"]["status"] == "ready_for_publication"

    prepare_replay = harness.client.post(
        "/v1/registry-changes/models/replace-customer-change/prepare-publication",
        headers=_headers("publisher-a", prepare_key),
        json=prepare_body,
    )
    assert prepare_replay.status_code == 200
    _assert_no_external_writes(prepare_replay)
    assert prepare_replay.json()["replayed"] is True
    assert prepare_replay.json()["proposal"] == prepared.json()["proposal"]

    altered_prepare = dict(prepare_body)
    altered_prepare["expected_revision"] = 999
    prepare_conflict = harness.client.post(
        "/v1/registry-changes/models/replace-customer-change/prepare-publication",
        headers=_headers("publisher-a", prepare_key),
        json=altered_prepare,
    )
    assert prepare_conflict.status_code == 409
    assert prepare_conflict.json()["code"] == "registry_change_conflict"

    inspected = harness.client.get(
        "/v1/registry-changes/models/replace-customer-change?history_limit=20",
        headers=_headers("auditor-a"),
    )
    assert inspected.status_code == 200
    _assert_no_external_writes(inspected)
    assert inspected.json()["audit_visible"] is True
    assert [item["event"] for item in inspected.json()["audit"]] == [
        "draft_created",
        "decision_recorded",
        "publication_prepared",
    ]
    assert "sql" not in inspected.text.casefold()
    assert "credential" not in inspected.text.casefold()


def test_registry_model_http_requires_one_idempotency_header_on_every_command() -> None:
    harness = _harness()
    profile_body = _profile_body(harness.domain)
    commands = (
        ("/v1/registry-changes/model-profiles", profile_body),
        (
            "/v1/registry-changes/model-profiles/replace-customer-profile/finalize",
            {"confirmed_authoring_fingerprint": "a" * 64},
        ),
        (
            "/v1/registry-changes/models",
            harness.domain.request.model_dump(mode="json"),
        ),
        (
            "/v1/registry-changes/models/replace-customer-change/decide",
            {
                "action": "approve",
                "expected_revision": 1,
                "confirmed_draft_fingerprint": "b" * 64,
                "rationale": "The exact replacement evidence was reviewed safely.",
            },
        ),
        (
            "/v1/registry-changes/models/replace-customer-change/prepare-publication",
            {
                "expected_revision": 2,
                "confirmed_draft_fingerprint": "c" * 64,
            },
        ),
    )

    for index, (path, body) in enumerate(commands):
        key = f"registry-model-header-{index:04d}"
        missing = harness.client.post(
            path,
            headers=_headers("analyst-a"),
            json=body,
        )
        duplicate = harness.client.post(
            path,
            headers=[
                ("Authorization", "Bearer analyst-a"),
                ("Idempotency-Key", key),
                ("Idempotency-Key", key),
            ],
            json=body,
        )
        assert missing.status_code == 422
        assert duplicate.status_code == 422
        assert missing.json()["code"] == "invalid_request"
        assert duplicate.json()["code"] == "registry_change_invalid_request"
        _assert_security_headers(missing)
        _assert_security_headers(duplicate)

    assert harness.domain.profile_queue.job is None
    assert (
        harness.domain.store.list_for_workspace(
            "workspace-a",
            owner_actor_id=None,
            limit=50,
        )
        == ()
    )


def test_registry_model_http_masks_cross_tenant_and_unknown_inspection_equally() -> None:
    harness = _harness()
    created = harness.client.post(
        "/v1/registry-changes/models",
        headers=_headers("analyst-a", "registry-model-mask-create-0001"),
        json=harness.domain.request.model_dump(mode="json"),
    )
    assert created.status_code == 201

    cross_tenant = harness.client.get(
        "/v1/registry-changes/models/replace-customer-change",
        headers=_headers("tenant-b"),
    )
    unknown = harness.client.get(
        "/v1/registry-changes/models/unknown-model-change",
        headers=_headers("tenant-b"),
    )

    assert cross_tenant.status_code == unknown.status_code == 404
    for key in ("type", "title", "status", "code"):
        assert cross_tenant.json()[key] == unknown.json()[key]
    assert cross_tenant.json()["code"] == "registry_change_resource_unavailable"
    _assert_security_headers(cross_tenant)
    _assert_security_headers(unknown)


def test_registry_model_http_query_models_and_unconfigured_service_fail_closed() -> None:
    harness = _harness()
    unknown_list_query = harness.client.get(
        "/v1/registry-changes/models?offset=10",
        headers=_headers("analyst-a"),
    )
    unknown_inspection_query = harness.client.get(
        "/v1/registry-changes/models/replace-customer-change?audit_limit=10",
        headers=_headers("analyst-a"),
    )
    unavailable = _harness(configured=False).client.get(
        "/v1/registry-changes/models",
        headers=_headers("analyst-a"),
    )

    assert unknown_list_query.status_code == 422
    assert unknown_inspection_query.status_code == 422
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "registry_change_service_unavailable"
    assert "dsn" not in unavailable.text.casefold()
    for response in (unknown_list_query, unknown_inspection_query, unavailable):
        _assert_security_headers(response)
