"""Judge-ready Streamlit entrypoint for the governed SchemaBridge workflow."""

from __future__ import annotations

import uuid
from dataclasses import replace

import streamlit as st
from pydantic_settings import SettingsError

from schemabridge.application.authentication import AuthenticationBoundaryError
from schemabridge.application.ui_view_models import JudgeUiView, UiResult
from schemabridge.application.ui_workflow import (
    JudgeUiService,
    UiActionError,
)
from schemabridge.bootstrap import (
    StreamlitRuntimeOptions,
    build_query_studio_runtime,
    build_streamlit_principal,
    build_streamlit_runtime_options,
    build_streamlit_ui_service,
)
from schemabridge.domain.identity import AuthenticatedPrincipal
from schemabridge.domain.intents import IntentAlternativeId
from schemabridge.domain.workflows import fingerprint_payload
from schemabridge.entrypoints.streamlit.auth_config import (
    validate_streamlit_auth_configuration,
)
from schemabridge.entrypoints.streamlit.components import (
    PageAction,
    PageActionKind,
    inject_styles,
    render_decisions,
    render_failure,
    render_header,
    render_modes,
    render_overview,
    render_query_studio,
    render_relationships,
    render_semantic_models,
)
from schemabridge.entrypoints.streamlit.query_studio import (
    clear_query_studio_state,
    render_dynamic_query_studio,
)
from schemabridge.entrypoints.streamlit.session_state import (
    TransientExecutionResult,
)

_PAGES = (
    "Overview",
    "Semantic Models",
    "Relationships",
    "Query Studio",
    "Decisions",
)
_TRANSIENT_EXECUTION_RESULT_KEY = "_transient_execution_result"


def main() -> None:
    """Render the application and translate UI events into typed use-case calls."""

    st.set_page_config(
        page_title="SchemaBridge · Governed semantic query agent",
        page_icon="🌉",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    inject_styles()
    try:
        runtime = build_streamlit_runtime_options()
    except (OSError, RuntimeError, SettingsError, ValueError):
        _stop_for_runtime_configuration("before loading governed data")
    if runtime.auth_mode == "oidc":
        _preflight_oidc(runtime)
    principal = _authenticate(runtime)
    _reset_session_for_principal(principal)
    try:
        service = build_streamlit_ui_service(principal=principal)
    except (OSError, RuntimeError, SettingsError, ValueError):
        _stop_for_runtime_configuration("before composing an external adapter")
    capabilities = service.capabilities()
    if not capabilities.can_view:
        with st.sidebar:
            _render_principal_summary(runtime, principal)
        st.error("permission_denied: No SchemaBridge workspace role is mapped.")
        st.caption("No governed reference, workflow, or integration data was rendered.")
        st.stop()
    try:
        available_workflows = service.available_workflows()
        inbox_error: UiActionError | None = None
    except UiActionError as error:
        available_workflows = ()
        inbox_error = error

    with st.sidebar:
        st.markdown("## SchemaBridge")
        st.caption("Governed semantic query agent")
        _render_principal_summary(runtime, principal)
        st.markdown("#### Integration modes")
        st.caption(f"Catalog · {runtime.catalog_kind}")
        st.caption(f"Semantic registry · {runtime.registry_kind}")
        st.caption(f"Publication · {runtime.publication_kind}")
        st.caption(f"Execution · {runtime.execution_kind}")
        if runtime.profile in {"staging", "production"}:
            st.caption("Modes are fixed by deployment configuration.")
        st.divider()
        load_demo = st.button(
            "Load demo scenario",
            type="primary",
            width="stretch",
            key="load-demo",
            disabled=not capabilities.can_create,
        )
        reset_demo = st.button(
            "Reset demo",
            width="stretch",
            key="reset-demo",
            help="Starts a fresh synthetic workflow and changes no source or DataHub data.",
            disabled=not capabilities.can_create,
        )
        if not capabilities.can_create:
            st.caption("Your current roles do not allow workflow creation.")
        if available_workflows:
            with st.expander("Open governed workflow"):
                workflow_labels = {
                    item.workflow_id: (
                        f"{item.workflow_id} · "
                        f"{'owned' if item.owned_by_current_principal else 'workspace'} · "
                        f"{item.created_at.date().isoformat()}"
                    )
                    for item in available_workflows
                }
                selected_workflow = st.selectbox(
                    "Available workflow",
                    tuple(workflow_labels),
                    format_func=lambda value: workflow_labels[value],
                    key="available-workflow",
                )
                open_workflow = st.button(
                    "Open workflow",
                    key="open-workflow",
                    width="stretch",
                )
        else:
            selected_workflow = None
            open_workflow = False
        if inbox_error is not None:
            st.error(f"{inbox_error.code}: Workflow inbox unavailable.")
        st.divider()
        st.caption("Synthetic demo · no source writes · bounded previews only")

    if open_workflow and isinstance(selected_workflow, str):
        st.session_state.pop(_TRANSIENT_EXECUTION_RESULT_KEY, None)
        clear_query_studio_state()
        st.session_state["workflow_id"] = selected_workflow
        st.session_state["navigation"] = "Query Studio"
        _clear_error()

    if load_demo or reset_demo:
        workflow_id = f"m20-{uuid.uuid4().hex}"
        st.session_state.pop(_TRANSIENT_EXECUTION_RESULT_KEY, None)
        clear_query_studio_state()
        _clear_error()
        try:
            with st.spinner("Retrieving bounded context and interpreting the request…"):
                service.start_demo(workflow_id)
            st.session_state["workflow_id"] = workflow_id
            st.session_state["navigation"] = "Query Studio"
        except UiActionError as error:
            _store_error(error)

    view = _load_view(service)
    render_header(view)
    render_modes(view.modes)

    page = st.radio(
        "Workspace",
        _PAGES,
        horizontal=True,
        label_visibility="collapsed",
        key="navigation",
    )
    st.divider()

    pending_action: PageAction | None = render_failure(
        view,
        can_retry=capabilities.can_retry,
        can_publish=capabilities.can_publish,
    )
    if page == "Overview":
        render_overview(view)
    elif page == "Semantic Models":
        render_semantic_models(view)
    elif page == "Relationships":
        render_relationships(view)
    elif page == "Query Studio":
        confirmed_request = None
        if capabilities.can_create:
            try:
                query_studio_runtime = build_query_studio_runtime(principal=principal)
                confirmed_request = render_dynamic_query_studio(
                    query_studio_runtime,
                    can_create=True,
                )
            except (OSError, RuntimeError, SettingsError, ValueError):
                st.error(
                    "query_studio_unavailable: La creación dinámica no está "
                    "disponible en este momento."
                )
                st.caption(
                    "El demo histórico y los workflows ya confirmados siguen "
                    "disponibles sin una ruta alternativa no gobernada."
                )
        else:
            st.markdown("### Nueva solicitud")
            st.info("Tu rol actual no permite crear un workflow nuevo.")
        if confirmed_request is not None:
            workflow_id = f"m27-{uuid.uuid4().hex}"
            st.session_state.pop(_TRANSIENT_EXECUTION_RESULT_KEY, None)
            _clear_error()
            try:
                with st.spinner("Creando el workflow gobernado confirmado…"):
                    service.start_confirmed_query_studio(
                        workflow_id,
                        confirmed_request,
                    )
                st.session_state["workflow_id"] = workflow_id
            except UiActionError as error:
                _store_error(error)
            st.rerun()
        st.divider()
        pending_action = pending_action or render_query_studio(view, capabilities)
    else:
        render_decisions(view)

    if pending_action is not None:
        _perform_action(service, view, pending_action)


def _authenticate(runtime: StreamlitRuntimeOptions) -> AuthenticatedPrincipal:
    if runtime.auth_mode == "local-demo":
        return build_streamlit_principal()
    try:
        logged_in = st.user.is_logged_in
    except Exception:
        _stop_for_runtime_configuration("before reading the authenticated session")
    if not logged_in:
        st.markdown("## Sign in to SchemaBridge")
        st.caption("OIDC authentication is required before governed data is loaded.")
        if st.button("Sign in", type="primary", key="login"):
            try:
                st.login(runtime.oidc_provider)
            except Exception:
                st.error("authentication_start_failed: The OIDC login could not be started.")
                st.caption("No governed data or integration was loaded.")
        st.stop()
    try:
        claims = st.user.to_dict()
    except Exception:
        st.error("identity_session_unavailable: The authenticated session could not be read.")
        st.caption("No governed data or integration was loaded.")
        st.stop()
    try:
        return build_streamlit_principal(claims=claims)
    except (AuthenticationBoundaryError, SettingsError, ValueError) as error:
        code = getattr(error, "code", "identity_claims_invalid")
        code_value = code.value if hasattr(code, "value") else str(code)
        st.error(f"{code_value}: The authenticated session could not be accepted.")
        st.caption("No governed data or integration was loaded.")
        if st.button("Clear session", key="clear-invalid-session"):
            _logout_or_stop()
        st.stop()
    raise RuntimeError("unreachable authentication boundary")


def _preflight_oidc(runtime: StreamlitRuntimeOptions) -> None:
    try:
        raw_secrets = st.secrets.to_dict()
        validate_streamlit_auth_configuration(raw_secrets, runtime)
    except Exception:
        _stop_for_runtime_configuration("before reading the authenticated session")


def _render_principal_summary(
    runtime: StreamlitRuntimeOptions,
    principal: AuthenticatedPrincipal,
) -> None:
    st.markdown("#### Authenticated principal")
    st.caption(f"Method · {principal.authentication_method.value}")
    st.caption(f"Principal · …{principal.actor_id[-12:]}")
    roles = ", ".join(sorted(role.value for role in principal.roles)) or "no mapped roles"
    st.caption(f"Roles · {roles}")
    if runtime.auth_mode == "oidc" and st.button("Sign out", key="logout"):
        _logout_or_stop()


def _logout_or_stop() -> None:
    st.session_state.pop(_TRANSIENT_EXECUTION_RESULT_KEY, None)
    clear_query_studio_state()
    try:
        st.logout()
    except Exception:
        st.error("authentication_logout_failed: The local identity session could not be cleared.")
        st.stop()


def _stop_for_runtime_configuration(stage: str) -> None:
    st.error("runtime_configuration_invalid: The deployment configuration was rejected.")
    st.caption(f"The application failed closed {stage}.")
    st.stop()


def _reset_session_for_principal(principal: AuthenticatedPrincipal) -> None:
    previous = st.session_state.get("_principal_actor_id")
    if previous is not None and previous != principal.actor_id:
        for key in (
            "workflow_id",
            "ui_error",
            "navigation",
            _TRANSIENT_EXECUTION_RESULT_KEY,
        ):
            st.session_state.pop(key, None)
        clear_query_studio_state()
    st.session_state["_principal_actor_id"] = principal.actor_id


def _load_view(service: JudgeUiService) -> JudgeUiView:
    workflow_id = st.session_state.get("workflow_id")
    if not isinstance(workflow_id, str):
        return service.empty()
    try:
        view = service.inspect(workflow_id)
        transient = st.session_state.get(_TRANSIENT_EXECUTION_RESULT_KEY)
        merged = _merge_transient_execution_result(
            view,
            transient,
            service.principal,
        )
        if transient is not None and merged is view:
            st.session_state.pop(_TRANSIENT_EXECUTION_RESULT_KEY, None)
        return merged
    except UiActionError as error:
        st.session_state.pop(_TRANSIENT_EXECUTION_RESULT_KEY, None)
        _store_error(error)
        return service.empty()


def _perform_action(
    service: JudgeUiService,
    view: JudgeUiView,
    action: PageAction,
) -> None:
    workflow_id = view.workflow_id
    if workflow_id is None:
        _store_error(
            UiActionError(
                "workflow_missing",
                "No durable workflow is loaded.",
                "Load the demo scenario before taking a governed action.",
            )
        )
        st.rerun()
        return
    try:
        with st.spinner(_action_label(action.kind)):
            if action.kind is PageActionKind.CONFIRM_INTENT:
                assert action.alternative_id is not None
                service.confirm_intent(
                    workflow_id,
                    IntentAlternativeId(action.alternative_id),
                )
            elif action.kind is PageActionKind.APPROVE_EXECUTION:
                executed_view = service.approve_execution(workflow_id)
                transient = _transient_execution_result(
                    executed_view,
                    service.principal,
                )
                if transient is not None:
                    st.session_state[_TRANSIENT_EXECUTION_RESULT_KEY] = transient
            elif action.kind is PageActionKind.PUBLISH:
                service.publish_context(workflow_id)
            elif action.kind is PageActionKind.SKIP_PUBLICATION:
                service.skip_publication(workflow_id)
            elif action.kind is PageActionKind.RETRY:
                service.retry(workflow_id)
            elif action.kind is PageActionKind.RECOVER:
                service.recover(workflow_id)
        _clear_error()
    except UiActionError as error:
        _store_error(error)
    st.rerun()


def _transient_execution_result(
    view: JudgeUiView,
    principal: AuthenticatedPrincipal,
) -> TransientExecutionResult | None:
    if (
        view.workflow_id is None
        or view.revision is None
        or view.query is None
        or view.query.result is None
        or len(view.query.result.rows) != view.query.result.row_count
        or _preview_fingerprint(view.query.result) != view.query.result.preview_fingerprint
    ):
        return None
    return TransientExecutionResult(
        actor_id=principal.actor_id,
        workspace_id=principal.workspace_id,
        workflow_id=view.workflow_id,
        revision=view.revision,
        registry_fingerprint=view.reference.registry_fingerprint,
        activation_generation=view.reference.activation_generation,
        active_pointer_fingerprint=view.reference.active_pointer_fingerprint,
        result=view.query.result,
    )


def _merge_transient_execution_result(
    view: JudgeUiView,
    transient: object,
    principal: AuthenticatedPrincipal,
) -> JudgeUiView:
    if (
        not isinstance(transient, TransientExecutionResult)
        or transient.actor_id != principal.actor_id
        or transient.workspace_id != principal.workspace_id
        or view.workflow_id != transient.workflow_id
        or view.revision != transient.revision
        or view.reference.registry_fingerprint != transient.registry_fingerprint
        or view.reference.activation_generation != transient.activation_generation
        or view.reference.active_pointer_fingerprint != transient.active_pointer_fingerprint
        or view.query is None
        or view.query.result is None
        or view.query.result.rows
        or _preview_fingerprint(transient.result) != transient.result.preview_fingerprint
        or replace(transient.result, rows=()) != view.query.result
    ):
        return view
    return replace(
        view,
        query=replace(view.query, result=transient.result),
    )


def _preview_fingerprint(result: UiResult) -> str:
    return fingerprint_payload(
        {
            "columns": result.columns,
            "rows": result.rows,
            "database_user": result.database_user,
            "transaction_read_only": result.transaction_read_only,
            "statement_timeout_ms": result.statement_timeout_ms,
            "truncated": result.truncated,
        }
    )


def _store_error(error: UiActionError) -> None:
    st.session_state["ui_error"] = {
        "code": error.code,
        "message": str(error),
        "corrective_action": error.corrective_action,
    }


def _clear_error() -> None:
    st.session_state.pop("ui_error", None)


def _action_label(kind: PageActionKind) -> str:
    return {
        PageActionKind.CONFIRM_INTENT: "Resolving approved semantic context…",
        PageActionKind.APPROVE_EXECUTION: "Revalidating SQL and running the bounded preview…",
        PageActionKind.PUBLISH: "Publishing the explicitly approved context…",
        PageActionKind.SKIP_PUBLICATION: "Recording the publication decision…",
        PageActionKind.RETRY: "Retrying only the failed external step…",
        PageActionKind.RECOVER: "Recovering the explicitly authorized interrupted state…",
    }[kind]


if __name__ == "__main__":
    main()
