from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from schemabridge.adapters.connectors.routed_postgres import (
    WorkerOnlyManagedQueryConnector,
)
from schemabridge.adapters.workflows.read_only import DisabledWorkflowPublisher
from schemabridge.application.postgres_health import DatabaseConfigurationError
from schemabridge.application.ui_workflow import UiActionError
from schemabridge.bootstrap import (
    build_agent_workflow_orchestrator,
    build_governed_request_executor,
    build_query_recipe_repository,
    build_streamlit_ui_service,
)
from schemabridge.config import Settings
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)
from schemabridge.domain.intents import IntentAlternativeId

ROOT = Path(__file__).resolve().parents[2]


def _managed_web_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "SCHEMABRIDGE_ENVIRONMENT": "production",
        "SCHEMABRIDGE_COMPONENT": "web",
        "SCHEMABRIDGE_AUTH_MODE": "oidc",
        "SCHEMABRIDGE_OIDC_ISSUER": "https://identity.example.test",
        "SCHEMABRIDGE_OIDC_AUDIENCE": "schemabridge-web",
        "SCHEMABRIDGE_OIDC_PROVIDER": "corporate-oidc",
        "SCHEMABRIDGE_OIDC_ALLOWED_GROUPS": {
            "data-analysts": ("analyst",),
        },
        "SCHEMABRIDGE_OIDC_ALLOWED_TENANTS": ("tenant-a",),
        "SCHEMABRIDGE_PSEUDONYMIZATION_KEY": ("unit-test-pseudonymization-key-at-least-32-bytes"),
        "SCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY": (
            "unit-test-query-studio-signing-key-with-diversity"
        ),
        "SCHEMABRIDGE_CONNECTOR_SECRET_MODE": "remote",
        "SCHEMABRIDGE_CONNECTOR_SECRET_PROVIDER_URL": "https://secrets.example.test",
        "SCHEMABRIDGE_CONNECTOR_SECRET_ROLE": "schemabridge-preflight",
        "SCHEMABRIDGE_CONNECTOR_SECRET_CAPABILITY": "preflight",
        "SCHEMABRIDGE_CONNECTOR_SECRET_KV_MOUNT": "tenant-connectors",
        "SCHEMABRIDGE_CONNECTOR_SECRET_CA_BUNDLE": ("/var/run/secrets/schemabridge/trust/ca.crt"),
        "SCHEMABRIDGE_WORKLOAD_IDENTITY_TOKEN_FILE": (
            "/var/run/secrets/schemabridge/identity/token"
        ),
        "SCHEMABRIDGE_WORKLOAD_IDENTITY_ROOT": "/var/run/secrets/schemabridge/identity",
        "SCHEMABRIDGE_WORKLOAD_IDENTITY_AUDIENCE": "schemabridge-secret-manager",
        "SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_ROLE": "schemabridge-registry-reader",
        "SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_BINDING_REF": "registry.reader.primary",
        "SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_VERSION": 17,
        "SCHEMABRIDGE_CONTROL_DATABASE_URL": (
            "postgresql://schemabridge_runtime:control_password@control.example.test/control"
            "?sslmode=verify-full"
        ),
        "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY": (
            "unit-test-control-audit-signing-key-with-diversity"
        ),
        "SCHEMABRIDGE_IDENTITY_MIGRATION_KEY": ("unit-test-identity-migration-key-with-diversity"),
        "SCHEMABRIDGE_PUBLICATION_MODE": "disabled",
        "SCHEMABRIDGE_JUDGE_EXECUTION": "disabled",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "missing_variable",
    ("SCHEMABRIDGE_PUBLICATION_MODE", "SCHEMABRIDGE_JUDGE_EXECUTION"),
)
def test_managed_web_requires_explicit_disabled_mutation_modes(
    missing_variable: str,
) -> None:
    payload = _managed_web_payload()
    del payload[missing_variable]

    with pytest.raises(ValidationError, match=missing_variable):
        Settings.model_validate(payload)


@pytest.mark.parametrize(
    ("variable", "value"),
    (
        ("SCHEMABRIDGE_PUBLICATION_MODE", "live"),
        ("SCHEMABRIDGE_PUBLICATION_MODE", "fake"),
        ("SCHEMABRIDGE_JUDGE_EXECUTION", "live"),
        ("SCHEMABRIDGE_JUDGE_EXECUTION", "recorded"),
    ),
)
def test_managed_web_rejects_mutation_capable_or_recorded_modes(
    variable: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError, match=variable):
        Settings.model_validate(_managed_web_payload(**{variable: value}))


def test_managed_web_accepts_only_the_explicit_planning_profile() -> None:
    settings = Settings.model_validate(_managed_web_payload())

    assert settings.publication_mode == "disabled"
    assert settings.execution_mode == "disabled"
    assert settings.max_query_tables == 3


def test_managed_web_cannot_bypass_the_ui_to_compose_writer_adapters() -> None:
    settings = Settings.model_validate(_managed_web_payload())

    with pytest.raises(DatabaseConfigurationError, match="disabled execution and publication"):
        build_agent_workflow_orchestrator(
            publication_kind="live",
            execution_kind="disabled",
            settings=settings,
        )
    with pytest.raises(DatabaseConfigurationError, match="requires disabled mode"):
        build_governed_request_executor(execution_kind="live", settings=settings)
    with pytest.raises(DatabaseConfigurationError, match="cannot compose"):
        build_query_recipe_repository("live", settings=settings)


def test_disabled_orchestrator_composes_without_writer_or_source_executor(
    tmp_path: Path,
) -> None:
    settings = Settings.model_validate(
        {
            "SCHEMABRIDGE_DRAFT_STORE_PATH": tmp_path / "disabled-orchestrator.db",
            "SCHEMABRIDGE_PUBLICATION_MODE": "disabled",
            "SCHEMABRIDGE_JUDGE_EXECUTION": "disabled",
        }
    )

    orchestrator = build_agent_workflow_orchestrator(
        publication_kind="disabled",
        execution_kind="disabled",
        settings=settings,
    )

    assert isinstance(orchestrator.publisher, DisabledWorkflowPublisher)
    assert isinstance(orchestrator.execute.executor, WorkerOnlyManagedQueryConnector)
    assert orchestrator.recipe_assessor is None


def test_disabled_ui_actions_fail_before_orchestrator_composition(tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {
            "SCHEMABRIDGE_DRAFT_STORE_PATH": tmp_path / "disabled-ui.db",
            "SCHEMABRIDGE_PUBLICATION_MODE": "disabled",
            "SCHEMABRIDGE_JUDGE_EXECUTION": "disabled",
        }
    )
    now = datetime.now(UTC)
    principal = AuthenticatedPrincipal(
        actor_id="sb_actor_managed_ui",
        workspace_id="sb_workspace_managed_ui",
        roles=frozenset({IdentityRole.ANALYST, IdentityRole.PUBLISHER}),
        authentication_method=AuthenticationMethod.LOCAL_DEMO,
        authenticated_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
    )
    service = build_streamlit_ui_service(settings=settings, principal=principal)
    service.start_demo("managed-ui-no-mutation")
    service.confirm_intent(
        "managed-ui-no-mutation",
        IntentAlternativeId.COUNT_DISTINCT_CUSTOMERS,
    )
    calls = 0

    def forbidden_orchestrator_factory():  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        raise AssertionError("disabled UI action must not compose an orchestrator")

    service = replace(service, orchestrator_factory=forbidden_orchestrator_factory)

    assert not service.capabilities().can_execute
    assert not service.capabilities().can_publish
    with pytest.raises(UiActionError, match="cannot execute") as execution:
        service.approve_execution("managed-ui-no-mutation")
    with pytest.raises(UiActionError, match="cannot publish") as publication:
        service.publish_context("managed-ui-no-mutation")

    assert execution.value.code == "managed_execution_submission_unavailable"
    assert publication.value.code == "managed_publication_submission_unavailable"
    assert calls == 0


def test_m29_web_manifest_declares_the_closed_planning_modes() -> None:
    documents = tuple(
        document
        for document in yaml.safe_load_all(
            (ROOT / "deploy/kubernetes/m29/base/runtime-config.yaml").read_text(encoding="utf-8")
        )
        if isinstance(document, dict)
    )
    web = next(
        document
        for document in documents
        if document.get("kind") == "ConfigMap"
        and document.get("metadata", {}).get("name") == "schemabridge-web-config"
    )

    assert web["data"]["SCHEMABRIDGE_PUBLICATION_MODE"] == "disabled"
    assert web["data"]["SCHEMABRIDGE_JUDGE_EXECUTION"] == "disabled"
