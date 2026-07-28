from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import schemabridge.bootstrap as bootstrap_module
from schemabridge.application.authorization import (
    AuthorizationError,
    AuthorizationErrorCode,
)
from schemabridge.application.ports.publication_audit import PublicationAuditStoreError
from schemabridge.application.ports.workflows import WorkflowError, WorkflowErrorCode
from schemabridge.application.ui_workflow import UiActionError
from schemabridge.bootstrap import (
    build_agent_workflow_orchestrator,
    build_streamlit_ui_service,
)
from schemabridge.config import Settings
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)
from schemabridge.domain.intents import IntentAlternativeId, UserLanguage
from schemabridge.domain.workflows import (
    AgentWorkflowDraft,
    StartWorkflowCommand,
    WorkflowOperation,
    WorkflowStage,
    WorkflowTraceEvent,
    WorkflowTraceStatus,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "SCHEMABRIDGE_DRAFT_STORE_PATH": tmp_path / "identity-ui.db",
            "SCHEMABRIDGE_JUDGE_EXECUTION": "recorded",
        }
    )


def _principal(
    *roles: IdentityRole,
    actor_id: str = "sb_actor_owner",
    workspace_id: str = "sb_workspace_one",
    expired: bool = False,
) -> AuthenticatedPrincipal:
    now = datetime.now(UTC)
    return AuthenticatedPrincipal(
        actor_id=actor_id,
        workspace_id=workspace_id,
        roles=frozenset(roles),
        authentication_method=AuthenticationMethod.LOCAL_DEMO,
        authenticated_at=now - timedelta(hours=2) if expired else now - timedelta(minutes=1),
        expires_at=now - timedelta(hours=1) if expired else now + timedelta(hours=1),
    )


def test_ui_decisions_are_bound_to_principal_and_expose_no_actor_argument(
    tmp_path: Path,
) -> None:
    owner = _principal(IdentityRole.ANALYST)
    service = build_streamlit_ui_service(
        settings=_settings(tmp_path),
        principal=owner,
    )

    service.start_demo("identity-bound-workflow")
    view = service.confirm_intent(
        "identity-bound-workflow",
        IntentAlternativeId.COUNT_DISTINCT_CUSTOMERS,
    )

    assert any(decision.actor == owner.actor_id for decision in view.decisions)
    assert "actor" not in inspect.signature(service.confirm_intent).parameters
    assert "actor" not in inspect.signature(service.approve_execution).parameters
    assert "actor" not in inspect.signature(service.publish_context).parameters


def test_denied_role_fails_before_registry_candidate_access_or_orchestrator_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    composition_calls: list[str] = []

    def forbidden_registry(*args: object, **kwargs: object) -> None:
        del args, kwargs
        composition_calls.append("registry")
        raise AssertionError("semantic registry must not be composed")

    def forbidden_candidates(*args: object, **kwargs: object) -> None:
        del args, kwargs
        composition_calls.append("candidates")
        raise AssertionError("candidate generator must not be composed")

    monkeypatch.setattr(bootstrap_module, "build_semantic_registry", forbidden_registry)
    monkeypatch.setattr(bootstrap_module, "build_candidate_generator", forbidden_candidates)
    service = build_streamlit_ui_service(
        settings=settings,
        principal=_principal(),
    )
    orchestrator_calls = 0

    def forbidden_factory():  # type: ignore[no-untyped-def]
        nonlocal orchestrator_calls
        orchestrator_calls += 1
        raise AssertionError("orchestrator must not be composed")

    denied = replace(service, orchestrator_factory=forbidden_factory)

    for denied_action in (
        denied.empty,
        lambda: denied.start_demo("denied-before-io"),
        denied.available_workflows,
    ):
        with pytest.raises(UiActionError) as error:
            denied_action()
        assert error.value.code == "permission_denied"

    assert composition_calls == []
    assert orchestrator_calls == 0
    assert not settings.draft_store_path.exists()
    assert not denied.capabilities().can_create


def test_empty_reference_view_requires_application_view_permission(tmp_path: Path) -> None:
    service = build_streamlit_ui_service(
        settings=_settings(tmp_path),
        principal=_principal(),
    )

    with pytest.raises(UiActionError) as denied:
        service.empty()

    assert denied.value.code == "permission_denied"


def test_auditor_inspection_is_inert_and_cannot_recover_interrupted_state(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    owner = build_streamlit_ui_service(
        settings=settings,
        principal=_principal(IdentityRole.ANALYST),
    )
    owner.start_demo("auditor-inert-inspection")
    owner.confirm_intent(
        "auditor-inert-inspection",
        IntentAlternativeId.COUNT_DISTINCT_CUSTOMERS,
    )
    orchestrator = build_agent_workflow_orchestrator(
        settings=settings,
        execution_kind="recorded",
    )
    validated = orchestrator.inspect("auditor-inert-inspection")
    interrupted_at = datetime.now(UTC)
    interrupted = AgentWorkflowDraft.model_validate(
        {
            **validated.model_dump(mode="python"),
            "revision": validated.revision + 1,
            "stage": WorkflowStage.EXECUTION,
            "checkpoint": None,
            "trace": (
                *validated.trace,
                WorkflowTraceEvent(
                    sequence=len(validated.trace) + 1,
                    stage=WorkflowStage.EXECUTION,
                    operation=WorkflowOperation.PREVIEW_EXECUTION,
                    status=WorkflowTraceStatus.STARTED,
                    occurred_at=interrupted_at,
                    input_refs=(validated.plan_fingerprint or "missing",),
                ),
            ),
            "updated_at": interrupted_at,
        }
    )
    orchestrator.store.save(interrupted, expected_revision=validated.revision)

    auditor = build_streamlit_ui_service(
        settings=settings,
        principal=_principal(IdentityRole.AUDITOR, actor_id="sb_actor_auditor"),
    )
    before = orchestrator.inspect(interrupted.id)
    view = auditor.inspect(interrupted.id)
    after = orchestrator.inspect(interrupted.id)

    assert view.recovery_operation == WorkflowOperation.PREVIEW_EXECUTION.value
    assert before == after == interrupted
    with pytest.raises(UiActionError) as denied:
        auditor.recover(interrupted.id)
    assert denied.value.code == "permission_denied"
    assert orchestrator.inspect(interrupted.id) == interrupted

    recovered = owner.recover(interrupted.id)
    assert recovered.error_code == "workflow_external_action_interrupted"
    assert orchestrator.inspect(interrupted.id).failure is not None


def test_unavailable_authorization_policy_fails_closed_before_any_workflow_io(
    tmp_path: Path,
) -> None:
    service = build_streamlit_ui_service(
        settings=_settings(tmp_path),
        principal=_principal(IdentityRole.PLATFORM_ADMIN),
    )
    calls = 0

    class UnavailablePolicy:
        def permissions_for(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            raise AuthorizationError(
                AuthorizationErrorCode.POLICY_UNAVAILABLE,
                "policy unavailable",
            )

        def require(self, *args: object, **kwargs: object) -> None:
            raise AuthorizationError(
                AuthorizationErrorCode.POLICY_UNAVAILABLE,
                "policy unavailable",
            )

        def require_workflow(self, *args: object, **kwargs: object) -> None:
            raise AuthorizationError(
                AuthorizationErrorCode.POLICY_UNAVAILABLE,
                "policy unavailable",
            )

        def owner_filter_for(
            self,
            *args: object,
            **kwargs: object,
        ) -> str | None:
            raise AuthorizationError(
                AuthorizationErrorCode.POLICY_UNAVAILABLE,
                "policy unavailable",
            )

    def forbidden_factory():  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        raise AssertionError("orchestrator must not be composed")

    unavailable = replace(
        service,
        authorization=UnavailablePolicy(),
        orchestrator_factory=forbidden_factory,
    )

    assert not unavailable.capabilities().can_create
    with pytest.raises(UiActionError) as denied:
        unavailable.start_demo("policy-unavailable")
    assert denied.value.code == "authorization_policy_unavailable"
    assert calls == 0
    assert (
        unavailable.access_store.load(
            unavailable.principal.workspace_id,
            "policy-unavailable",
        )
        is None
    )


@pytest.mark.parametrize(
    "factory_error",
    [
        OSError("sensitive filesystem path"),
        PublicationAuditStoreError("sensitive audit storage detail"),
        WorkflowError(WorkflowErrorCode.STORE_FAILURE, "sensitive workflow store detail"),
    ],
)
def test_orchestrator_construction_failures_are_sanitized(
    tmp_path: Path,
    factory_error: Exception,
) -> None:
    service = build_streamlit_ui_service(
        settings=_settings(tmp_path),
        principal=_principal(IdentityRole.ANALYST),
    )
    service.start_demo("sanitized-construction-failure")

    def unavailable_factory():  # type: ignore[no-untyped-def]
        raise factory_error

    unavailable = replace(service, orchestrator_factory=unavailable_factory)
    with pytest.raises(UiActionError) as failure:
        unavailable.inspect("sanitized-construction-failure")

    assert failure.value.code == "integration_configuration_unavailable"
    assert "sensitive" not in str(failure.value).casefold()


def test_workflow_inbox_is_owner_scoped_for_analyst_and_workspace_scoped_for_publisher(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    first = build_streamlit_ui_service(
        settings=settings,
        principal=_principal(IdentityRole.ANALYST, actor_id="sb_actor_first"),
    )
    second = build_streamlit_ui_service(
        settings=settings,
        principal=_principal(IdentityRole.ANALYST, actor_id="sb_actor_second"),
    )
    first.start_demo("first-owned-workflow")
    second.start_demo("second-owned-workflow")

    assert tuple(item.workflow_id for item in first.available_workflows()) == (
        "first-owned-workflow",
    )
    publisher = build_streamlit_ui_service(
        settings=settings,
        principal=_principal(IdentityRole.PUBLISHER, actor_id="sb_actor_publisher"),
    )
    inbox = publisher.available_workflows()
    assert {item.workflow_id for item in inbox} == {
        "first-owned-workflow",
        "second-owned-workflow",
    }
    assert all(not item.owned_by_current_principal for item in inbox)


def test_cross_owner_and_cross_workspace_are_indistinguishable_and_do_no_workflow_io(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    owner = build_streamlit_ui_service(
        settings=settings,
        principal=_principal(IdentityRole.ANALYST),
    )
    owner.start_demo("isolated-workflow")

    calls = 0

    def forbidden_factory():  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        raise AssertionError("protected workflow must not be loaded")

    for principal in (
        _principal(IdentityRole.ANALYST, actor_id="sb_actor_other"),
        _principal(
            IdentityRole.PLATFORM_ADMIN,
            actor_id="sb_actor_admin",
            workspace_id="sb_workspace_other",
        ),
    ):
        other = build_streamlit_ui_service(settings=settings, principal=principal)
        other = replace(other, orchestrator_factory=forbidden_factory)
        with pytest.raises(UiActionError) as denied:
            other.inspect("isolated-workflow")
        assert denied.value.code == "workflow_access_denied"
        assert str(denied.value) == (
            "The workflow is not available to the authenticated principal."
        )
    assert calls == 0


def test_existing_legacy_workflow_cannot_be_claimed_by_failed_start(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    service = build_streamlit_ui_service(
        settings=settings,
        principal=_principal(IdentityRole.ANALYST),
    )
    build_agent_workflow_orchestrator(
        settings=settings,
        execution_kind="recorded",
    ).start(
        StartWorkflowCommand(
            id="legacy-unverified-workflow",
            text="Synthetic legacy workflow without authenticated ownership.",
            language=UserLanguage.ENGLISH,
            datasets=(PhysicalDatasetRef("crm.customers"),),
        )
    )

    with pytest.raises(UiActionError) as conflict:
        service.start_request(
            "legacy-unverified-workflow",
            "Attempt to claim a pre-existing workflow.",
        )
    assert conflict.value.code == "workflow_conflict"
    assert (
        service.access_store.load(
            service.principal.workspace_id,
            "legacy-unverified-workflow",
        )
        is None
    )
    with pytest.raises(UiActionError) as inaccessible:
        service.inspect("legacy-unverified-workflow")
    assert inaccessible.value.code == "workflow_access_denied"


def test_publisher_can_publish_workspace_context_but_cannot_execute_or_export(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    analyst = build_streamlit_ui_service(
        settings=settings,
        principal=_principal(IdentityRole.ANALYST),
    )
    analyst.start_demo("four-eyes-workflow")
    analyst.confirm_intent(
        "four-eyes-workflow",
        IntentAlternativeId.COUNT_DISTINCT_CUSTOMERS,
    )
    analyst.approve_execution("four-eyes-workflow")

    with pytest.raises(UiActionError) as analyst_publish:
        analyst.publish_context("four-eyes-workflow")
    assert analyst_publish.value.code == "permission_denied"

    publisher_principal = _principal(
        IdentityRole.PUBLISHER,
        actor_id="sb_actor_publisher",
    )
    publisher = build_streamlit_ui_service(
        settings=settings,
        principal=publisher_principal,
    )
    capabilities = publisher.capabilities()
    assert capabilities.can_publish
    assert not capabilities.can_execute
    assert not capabilities.can_view_result
    assert not capabilities.can_export

    published = publisher.publish_context("four-eyes-workflow")
    assert published.query is not None
    assert published.query.result is None
    assert any(
        decision.actor == publisher_principal.actor_id and decision.action == "publish"
        for decision in published.decisions
    )


def test_live_publication_requires_a_distinct_execution_approver(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    combined = _principal(IdentityRole.ANALYST, IdentityRole.PUBLISHER)
    service = build_streamlit_ui_service(settings=settings, principal=combined)
    service = replace(service, require_separate_publisher=True)
    service.start_demo("live-four-eyes")
    service.confirm_intent(
        "live-four-eyes",
        IntentAlternativeId.COUNT_DISTINCT_CUSTOMERS,
    )
    service.approve_execution("live-four-eyes")

    with pytest.raises(UiActionError) as denied:
        service.publish_context("live-four-eyes")
    assert denied.value.code == "separation_of_duties_required"

    publisher = build_streamlit_ui_service(
        settings=settings,
        principal=_principal(IdentityRole.PUBLISHER, actor_id="sb_actor_second_publisher"),
    )
    publisher = replace(publisher, require_separate_publisher=True)
    published = publisher.publish_context("live-four-eyes")
    assert any(item.action == "publish" for item in published.decisions)


def test_live_publication_requires_a_recent_identity_token(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    analyst = build_streamlit_ui_service(
        settings=settings,
        principal=_principal(IdentityRole.ANALYST),
    )
    analyst.start_demo("stale-publisher-workflow")
    analyst.confirm_intent(
        "stale-publisher-workflow",
        IntentAlternativeId.COUNT_DISTINCT_CUSTOMERS,
    )
    analyst.approve_execution("stale-publisher-workflow")
    now = datetime.now(UTC)
    stale_publisher = _principal(
        IdentityRole.PUBLISHER,
        actor_id="sb_actor_stale_publisher",
    ).model_copy(
        update={
            "authenticated_at": now - timedelta(minutes=16),
            "expires_at": now + timedelta(minutes=44),
        }
    )
    publisher = build_streamlit_ui_service(
        settings=settings,
        principal=stale_publisher,
    )
    publisher = replace(publisher, require_separate_publisher=True)

    with pytest.raises(UiActionError) as denied:
        publisher.publish_context("stale-publisher-workflow")

    assert denied.value.code == "publication_reauthentication_required"


def test_expired_principal_is_denied_before_access_lookup(tmp_path: Path) -> None:
    service = build_streamlit_ui_service(
        settings=_settings(tmp_path),
        principal=_principal(IdentityRole.PLATFORM_ADMIN, expired=True),
    )

    with pytest.raises(UiActionError) as denied:
        service.start_demo("expired-session")
    assert denied.value.code == "principal_not_current"
    assert not service.capabilities().can_create
