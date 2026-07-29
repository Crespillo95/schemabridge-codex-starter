"""Responsive, HTML-free Streamlit renderer for the M29 operations view."""

from __future__ import annotations

from html import escape

import streamlit as st

from schemabridge.application.m29_operations import (
    M29OperationsSeverity,
    M29OperationsView,
)


def render_m29_operations_view(view: M29OperationsView) -> None:
    """Render only the closed public M29 operations projection."""

    if not isinstance(view, M29OperationsView):
        raise TypeError("M29 operations renderer requires its typed public view")

    st.title("SchemaBridge operations")
    st.caption(
        "Deterministic local evidence. These states exercise the operator view; "
        "they do not claim an operated production environment."
    )

    message = f"{view.label}. {view.summary}"
    if view.severity is M29OperationsSeverity.HEALTHY:
        st.success(message)
    elif view.severity is M29OperationsSeverity.WARNING:
        st.warning(message)
    else:
        st.error(message)

    diagnostics = view.diagnostics
    st.subheader("Safe diagnostics")
    st.metric(
        "Ready workloads",
        f"{diagnostics.ready_workloads} of {diagnostics.expected_workloads}",
    )
    st.metric("Queue depth", diagnostics.queue_depth)
    st.metric("Oldest queued work", f"{diagnostics.oldest_queue_age_seconds} seconds")
    st.metric(
        "Secret resolution",
        diagnostics.secret_resolution.value.replace("_", " ").title(),
    )
    st.metric("Backup age", f"{diagnostics.backup_age_minutes} minutes")
    st.metric(
        "Release gate",
        diagnostics.release_gate.value.replace("_", " ").title(),
    )
    st.metric(
        "Telemetry delivery",
        diagnostics.telemetry.value.replace("_", " ").title(),
    )

    st.subheader("Recommended response")
    st.info(view.recommended_action)

    st.subheader("Escaping check")
    st.caption(
        "This fixed synthetic probe is escaped before display and is never interpreted as HTML."
    )
    st.caption(escape(view.hostile_probe, quote=True))
