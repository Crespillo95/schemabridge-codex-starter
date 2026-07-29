"""Synthetic local selector for the six-state M29 operations browser matrix."""

from __future__ import annotations

from typing import cast

import streamlit as st

from schemabridge.application.m29_operations import (
    M29OperationsState,
    build_m29_operations_view,
)
from schemabridge.entrypoints.streamlit.m29_operations import (
    render_m29_operations_view,
)


def m29_operations_state_label(state: M29OperationsState) -> str:
    """Return the trusted reader-facing label for one closed scenario."""

    return build_m29_operations_view(state).label


def select_m29_operations_state(
    *,
    default_state: M29OperationsState = M29OperationsState.HEALTHY,
) -> M29OperationsState:
    """Render an accessible selector and return one closed scenario."""

    if not isinstance(default_state, M29OperationsState):
        raise TypeError("M29 operations default must use the closed enum")
    options = tuple(M29OperationsState)
    selected = st.selectbox(
        "Operational scenario",
        options,
        index=options.index(default_state),
        format_func=m29_operations_state_label,
        help="Choose one deterministic synthetic state from the M29 operations matrix.",
        key="m29-operations-state",
        label_visibility="visible",
    )
    return cast(M29OperationsState, selected)


def render_m29_operations_scenario(
    *,
    default_state: M29OperationsState = M29OperationsState.HEALTHY,
) -> M29OperationsState:
    """Render the local six-state scenario using the production-safe component."""

    selected = select_m29_operations_state(default_state=default_state)
    render_m29_operations_view(build_m29_operations_view(selected))
    return selected
