"""Authenticated, tenant-safe M34 registry-publication HTTP boundary tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from tests.unit.test_registry_publication_use_cases import (
    IDEMPOTENCY_KEY,
    NOW,
    _ApiStore,
    _awaiting_from_job,
    _Clock,
    _ProposalStore,
)
from tests.unit.test_registry_publication_v2 import OPAQUE_ORDERS_URN, _proposal

from schemabridge.application.authentication import AuthenticationBoundaryError
from schemabridge.application.registry_publication import (
    AuthorizeRegistryPublication,
    CancelRegistryPublication,
    InspectRegistryPublication,
    RegistryPublicationError,
    RegistryPublicationErrorCode,
    SubmitRegistryPublication,
)
from schemabridge.application.semantic_onboarding_authorization import (
    SemanticOnboardingAuthorizationPolicy,
)
from schemabridge.domain.background_jobs import BackgroundJob, JobSubmissionResult
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)
from schemabridge.domain.registry_publication_jobs import RegistryPublicationJobStatus
from schemabridge.entrypoints.http.app import (
    ApiHttpServices,
    RegistryPublicationHttpServices,
    create_http_app,
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
        raise AssertionError("registry publication HTTP must not submit execution jobs")


class _UnusedJobInspection:
    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        job_id: str,
    ) -> BackgroundJob:
        del principal, job_id
        raise AssertionError("registry publication HTTP must not inspect execution jobs")


class _UnusedJobCancellation:
    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        job_id: str,
    ) -> BackgroundJob:
        del principal, job_id
        raise AssertionError("registry publication HTTP must not cancel execution jobs")


class _Readiness:
    def require_ready(self) -> None:
        return None


class _Authenticator:
    def authenticate(self, bearer_token: str, now: object) -> AuthenticatedPrincipal:
        assert now == NOW
        identities = {
            "publisher-a": ("workspace-a", "publisher-a", IdentityRole.PUBLISHER, 1),
            "publisher-stale": ("workspace-a", "publisher-a", IdentityRole.PUBLISHER, 15),
            "auditor-a": ("workspace-a", "auditor-a", IdentityRole.AUDITOR, 1),
            "analyst-a": ("workspace-a", "analyst-a", IdentityRole.ANALYST, 1),
            "publisher-b": ("workspace-b", "publisher-b", IdentityRole.PUBLISHER, 1),
        }
        identity = identities.get(bearer_token)
        if identity is None:
            raise AuthenticationBoundaryError("invalid_bearer_token")
        workspace_id, actor_id, role, age_minutes = identity
        return AuthenticatedPrincipal(
            actor_id=actor_id,
            workspace_id=workspace_id,
            roles=frozenset({role}),
            authentication_method=AuthenticationMethod.OIDC,
            authenticated_at=NOW - timedelta(minutes=age_minutes),
            expires_at=NOW + timedelta(minutes=30),
        )


@dataclass(slots=True)
class _HttpHarness:
    client: TestClient
    jobs: _ApiStore
    proposals: _ProposalStore


def _harness(*, configured: bool = True) -> _HttpHarness:
    proposal = _proposal()
    proposals = _ProposalStore(proposal)
    jobs = _ApiStore()
    policy = SemanticOnboardingAuthorizationPolicy()
    clock = _Clock()
    publication = (
        RegistryPublicationHttpServices(
            submit=SubmitRegistryPublication(proposals, jobs, policy, clock),
            inspect=InspectRegistryPublication(jobs, policy, clock),
            authorize=AuthorizeRegistryPublication(jobs, policy, clock),
            cancel=CancelRegistryPublication(jobs, policy, clock),
        )
        if configured
        else None
    )
    services = ApiHttpServices(
        authenticator=_Authenticator(),
        clock=clock,
        submit=_UnusedJobSubmission(),
        inspect=_UnusedJobInspection(),
        cancel=_UnusedJobCancellation(),
        readiness=_Readiness(),
        registry_publication=publication,
    )
    return _HttpHarness(
        client=TestClient(create_http_app(services), raise_server_exceptions=False),
        jobs=jobs,
        proposals=proposals,
    )


def _headers(token: str = "publisher-a") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": IDEMPOTENCY_KEY,
    }


def _submit_body() -> dict[str, object]:
    proposal = _proposal()
    return {
        "proposal_id": proposal.id,
        "confirmed_proposal_fingerprint": proposal.fingerprint,
    }


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_all_keys(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_all_keys(item) for item in value), set())
    return set()


def test_submit_inspect_authorize_and_cancel_exact_candidate_without_private_state() -> None:
    harness = _harness()

    with harness.client as client:
        submitted = client.post(
            "/v1/registry-publications",
            headers=_headers(),
            json=_submit_body(),
        )
        replay = client.post(
            "/v1/registry-publications",
            headers=_headers(),
            json=_submit_body(),
        )

        submitted_job = next(iter(harness.jobs.jobs.values()))
        awaiting = _awaiting_from_job(submitted_job)
        harness.jobs.jobs[(awaiting.scope.workspace_id, awaiting.id)] = awaiting
        harness.jobs.now = NOW + timedelta(seconds=3)
        inspected = client.get(
            f"/v1/registry-publications/{awaiting.id}",
            headers={"Authorization": "Bearer auditor-a"},
        )
        stale_authorization = client.post(
            f"/v1/registry-publications/{awaiting.id}/authorize",
            headers={"Authorization": "Bearer publisher-a"},
            json={
                "expected_revision": awaiting.revision + 1,
                "confirmed_candidate_fingerprint": awaiting.candidate.fingerprint,
                "confirmation": "publish-exact-observed-registry-version",
            },
        )
        authorized = client.post(
            f"/v1/registry-publications/{awaiting.id}/authorize",
            headers={"Authorization": "Bearer publisher-a"},
            json={
                "expected_revision": awaiting.revision,
                "confirmed_candidate_fingerprint": awaiting.candidate.fingerprint,
                "confirmation": "publish-exact-observed-registry-version",
            },
        )
        authorized_payload = authorized.json()
        cancelled = client.post(
            f"/v1/registry-publications/{awaiting.id}/cancel",
            headers={"Authorization": "Bearer publisher-a"},
            json={"expected_revision": authorized_payload["revision"]},
        )

    assert submitted.status_code == 202
    assert submitted.json()["replayed"] is False
    assert submitted.json()["job"]["status"] == "queued"
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["job"] == submitted.json()["job"]

    inspected_payload = inspected.json()
    assert inspected.status_code == 200
    assert inspected_payload["status"] == "awaiting_approval"
    assert inspected_payload["candidate"]["fingerprint"] == awaiting.candidate.fingerprint
    assert (
        inspected_payload["candidate"]["registry"]["physical_bindings"][0][
            "observed_datahub_asset_urn"
        ]
        == OPAQUE_ORDERS_URN
    )
    assert stale_authorization.status_code == 409
    assert stale_authorization.json()["code"] == "registry_publication_conflict"

    assert authorized.status_code == 200
    assert authorized_payload["status"] == "approved"
    assert authorized_payload["authorization"]["candidate_fingerprint"] == (
        awaiting.candidate.fingerprint
    )
    assert authorized_payload["authorization"]["confirmation"] == (
        "publish-exact-observed-registry-version"
    )
    assert "authenticated_at" not in authorized_payload["authorization"]
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    for payload in (
        submitted.json(),
        replay.json(),
        inspected_payload,
        authorized_payload,
        cancelled.json(),
    ):
        assert not _all_keys(payload).intersection(
            {
                "idempotency_digest",
                "request_fingerprint",
                "submitted_by",
                "lease",
                "token_digest",
                "lease_capability",
                "dsn",
                "secret",
            }
        )
        assert IDEMPOTENCY_KEY not in str(payload)


def test_tenant_and_role_boundaries_do_not_disclose_publication_resources() -> None:
    harness = _harness()

    with harness.client as client:
        submitted = client.post(
            "/v1/registry-publications",
            headers=_headers(),
            json=_submit_body(),
        )
        job_id = submitted.json()["job"]["job_id"]
        cross_tenant = client.get(
            f"/v1/registry-publications/{job_id}",
            headers={"Authorization": "Bearer publisher-b"},
        )
        denied = client.post(
            "/v1/registry-publications",
            headers=_headers("analyst-a"),
            json=_submit_body(),
        )
        stale = client.post(
            "/v1/registry-publications",
            headers=_headers("publisher-stale"),
            json=_submit_body(),
        )

    assert submitted.status_code == 202
    assert cross_tenant.status_code == 404
    assert cross_tenant.json()["code"] == "registry_publication_resource_unavailable"
    assert denied.status_code == 403
    assert denied.json()["code"] == "permission_denied"
    assert stale.status_code == 401
    assert stale.json()["code"] == "registry_publication_stale_session"
    assert stale.headers["www-authenticate"] == "Bearer"


def test_authorization_and_cancellation_require_exact_current_confirmation() -> None:
    harness = _harness()

    with harness.client as client:
        submitted = client.post(
            "/v1/registry-publications",
            headers=_headers(),
            json=_submit_body(),
        )
        submitted_job = next(iter(harness.jobs.jobs.values()))
        awaiting = _awaiting_from_job(submitted_job)
        harness.jobs.jobs[(awaiting.scope.workspace_id, awaiting.id)] = awaiting
        assert awaiting.candidate is not None
        command = {
            "expected_revision": awaiting.revision,
            "confirmed_candidate_fingerprint": awaiting.candidate.fingerprint,
            "confirmation": "publish-exact-observed-registry-version",
        }
        invalid_confirmation = client.post(
            f"/v1/registry-publications/{awaiting.id}/authorize",
            headers={"Authorization": "Bearer publisher-a"},
            json={**command, "confirmation": "publish-any-version"},
        )
        denied_authorization = client.post(
            f"/v1/registry-publications/{awaiting.id}/authorize",
            headers={"Authorization": "Bearer auditor-a"},
            json=command,
        )
        stale_authorization = client.post(
            f"/v1/registry-publications/{awaiting.id}/authorize",
            headers={"Authorization": "Bearer publisher-stale"},
            json=command,
        )
        wrong_fingerprint = client.post(
            f"/v1/registry-publications/{awaiting.id}/authorize",
            headers={"Authorization": "Bearer publisher-a"},
            json={**command, "confirmed_candidate_fingerprint": "f" * 64},
        )
        denied_cancellation = client.post(
            f"/v1/registry-publications/{awaiting.id}/cancel",
            headers={"Authorization": "Bearer auditor-a"},
            json={"expected_revision": awaiting.revision},
        )
        stale_cancellation = client.post(
            f"/v1/registry-publications/{awaiting.id}/cancel",
            headers={"Authorization": "Bearer publisher-a"},
            json={"expected_revision": awaiting.revision + 1},
        )

    assert submitted.status_code == 202
    assert invalid_confirmation.status_code == 422
    assert denied_authorization.status_code == 403
    assert stale_authorization.status_code == 401
    assert stale_authorization.json()["code"] == "registry_publication_stale_session"
    assert wrong_fingerprint.status_code == 409
    assert denied_cancellation.status_code == 403
    assert stale_cancellation.status_code == 409
    assert harness.jobs.authorize_calls == 0
    assert harness.jobs.cancel_calls == []
    assert harness.jobs.jobs[(awaiting.scope.workspace_id, awaiting.id)].status is (
        RegistryPublicationJobStatus.AWAITING_APPROVAL
    )


def test_http_schema_and_header_boundary_rejects_ambiguous_or_forged_commands() -> None:
    harness = _harness()

    with harness.client as client:
        duplicate_header = client.post(
            "/v1/registry-publications",
            headers=[
                ("Authorization", "Bearer publisher-a"),
                ("Idempotency-Key", IDEMPOTENCY_KEY),
                ("Idempotency-Key", "second-publication-key-0002"),
            ],
            json=_submit_body(),
        )
        extra = client.post(
            "/v1/registry-publications",
            headers=_headers(),
            json={**_submit_body(), "workspace_id": "workspace-b"},
        )
        invalid_fingerprint = client.post(
            "/v1/registry-publications",
            headers=_headers(),
            json={
                "proposal_id": _proposal().id,
                "confirmed_proposal_fingerprint": "not-a-fingerprint",
            },
        )
        missing_service = _harness(configured=False).client.post(
            "/v1/registry-publications",
            headers=_headers(),
            json=_submit_body(),
        )

    assert duplicate_header.status_code == 422
    assert duplicate_header.json()["code"] == "registry_publication_invalid_request"
    assert extra.status_code == 422
    assert invalid_fingerprint.status_code == 422
    assert missing_service.status_code == 503
    assert missing_service.json()["code"] == "registry_publication_service_unavailable"
    assert harness.jobs.jobs == {}


@pytest.mark.parametrize(
    ("code", "status"),
    (
        (RegistryPublicationErrorCode.INVALID_REQUEST, 422),
        (RegistryPublicationErrorCode.UNAVAILABLE, 404),
        (RegistryPublicationErrorCode.CONFLICT, 409),
        (RegistryPublicationErrorCode.TARGET_RESERVED, 409),
        (RegistryPublicationErrorCode.NOT_READY, 409),
        (RegistryPublicationErrorCode.STALE_SESSION, 401),
        (RegistryPublicationErrorCode.SERVICE_UNAVAILABLE, 503),
    ),
)
def test_registry_publication_error_mapping_is_closed_and_sanitized(
    code: RegistryPublicationErrorCode,
    status: int,
) -> None:
    class _Failure:
        def execute(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            raise RegistryPublicationError(code, "private DSN token and database row")

    services = ApiHttpServices(
        authenticator=_Authenticator(),
        clock=_Clock(),
        submit=_UnusedJobSubmission(),
        inspect=_UnusedJobInspection(),
        cancel=_UnusedJobCancellation(),
        readiness=_Readiness(),
        registry_publication=RegistryPublicationHttpServices(
            submit=_Failure(),  # type: ignore[arg-type]
            inspect=_Failure(),  # type: ignore[arg-type]
            authorize=_Failure(),  # type: ignore[arg-type]
            cancel=_Failure(),  # type: ignore[arg-type]
        ),
    )

    with TestClient(create_http_app(services), raise_server_exceptions=False) as client:
        response = client.post(
            "/v1/registry-publications",
            headers=_headers(),
            json=_submit_body(),
        )

    assert response.status_code == status
    assert response.json()["code"] == code.value
    assert "private" not in response.text
    assert "dsn" not in response.text.casefold()
    assert "token" not in response.text.casefold()
