"""Profile-aware routing for the M29 Operations page."""

from __future__ import annotations

from typing import Literal

from schemabridge.entrypoints.streamlit.m29_operations import (
    render_m29_operations_unavailable,
)

_RuntimeProfile = Literal["development", "hosted-demo", "staging", "production"]
_OperationsSurface = Literal["showcase", "unavailable"]


def render_m29_operations_page(profile: _RuntimeProfile) -> _OperationsSurface:
    """Expose synthetic scenarios only in explicit local/demo profiles."""

    if profile in {"staging", "production"}:
        render_m29_operations_unavailable()
        return "unavailable"
    if profile not in {"development", "hosted-demo"}:
        raise ValueError("unsupported operations profile")

    from schemabridge.entrypoints.streamlit.m29_operations_scenario import (
        render_m29_operations_scenario,
    )

    render_m29_operations_scenario()
    return "showcase"
