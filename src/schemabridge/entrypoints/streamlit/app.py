"""Judge-ready Streamlit entrypoint for the governed SchemaBridge workflow."""

from __future__ import annotations

import uuid
from typing import Literal

import streamlit as st

from schemabridge.application.ui_view_models import JudgeUiView
from schemabridge.application.ui_workflow import JudgeUiService, UiActionError
from schemabridge.bootstrap import build_streamlit_ui_service
from schemabridge.domain.intents import IntentAlternativeId
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

_PAGES = (
    "Overview",
    "Semantic Models",
    "Relationships",
    "Query Studio",
    "Decisions",
)


def main() -> None:
    """Render the application and translate UI events into typed use-case calls."""

    st.set_page_config(
        page_title="SchemaBridge · Governed semantic query agent",
        page_icon="🌉",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    inject_styles()
    with st.sidebar:
        st.markdown("## SchemaBridge")
        st.caption("Governed semantic query agent")
        st.markdown("#### Integration modes")
        catalog_label = st.selectbox(
            "Catalog",
            ("Recorded catalog", "Live DataHub"),
            help="The selected catalog is explicit. There is no hidden fallback.",
        )
        publication_label = st.selectbox(
            "Publication",
            ("Fake local", "Live DataHub"),
            help="Publication always requires a separate typed approval.",
        )
        actor = st.text_input("Decision actor", value="judge-operator", max_chars=120)
        st.divider()
        load_demo = st.button(
            "Load demo scenario",
            type="primary",
            width="stretch",
            key="load-demo",
        )
        with st.expander("Start another request"):
            request_text = st.text_area(
                "Business request",
                value=(
                    "Agrupa por fecha de registro todos los clientes que sean segundo "
                    "titular de una cuenta."
                ),
                max_chars=2_000,
            )
            start_request = st.button(
                "Interpret request",
                width="stretch",
                key="start-request",
            )
        st.divider()
        st.caption("Synthetic demo · no source writes · bounded previews only")

    catalog_kind: Literal["live", "recorded"] = (
        "live" if catalog_label == "Live DataHub" else "recorded"
    )
    publication_kind: Literal["live", "fake"] = (
        "live" if publication_label == "Live DataHub" else "fake"
    )
    service = build_streamlit_ui_service(
        catalog_kind,
        publication_kind=publication_kind,
    )

    if load_demo or start_request:
        workflow_id = f"m14-{uuid.uuid4().hex[:12]}"
        _clear_error()
        try:
            with st.spinner("Retrieving bounded context and interpreting the request…"):
                if load_demo:
                    service.start_demo(workflow_id)
                else:
                    service.start_request(workflow_id, request_text)
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

    pending_action: PageAction | None = render_failure(view)
    if page == "Overview":
        render_overview(view)
    elif page == "Semantic Models":
        render_semantic_models(view)
    elif page == "Relationships":
        render_relationships(view)
    elif page == "Query Studio":
        pending_action = pending_action or render_query_studio(view)
    else:
        render_decisions(view)

    if pending_action is not None:
        _perform_action(service, view, actor, pending_action)


def _load_view(service: JudgeUiService) -> JudgeUiView:
    workflow_id = st.session_state.get("workflow_id")
    if not isinstance(workflow_id, str):
        return service.empty()
    try:
        return service.inspect(workflow_id)
    except UiActionError as error:
        _store_error(error)
        return service.empty()


def _perform_action(
    service: JudgeUiService,
    view: JudgeUiView,
    actor: str,
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
                    actor,
                    IntentAlternativeId(action.alternative_id),
                )
            elif action.kind is PageActionKind.APPROVE_EXECUTION:
                service.approve_execution(workflow_id, actor)
            elif action.kind is PageActionKind.PUBLISH:
                service.publish_context(workflow_id, actor)
            elif action.kind is PageActionKind.SKIP_PUBLICATION:
                service.skip_publication(workflow_id, actor)
            elif action.kind is PageActionKind.RETRY:
                service.retry(workflow_id, actor)
        _clear_error()
    except UiActionError as error:
        _store_error(error)
    st.rerun()


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
    }[kind]


if __name__ == "__main__":
    main()
