from __future__ import annotations

import inspect

import pytest
from streamlit.testing.v1 import AppTest

from schemabridge.application.m29_operations import M29OperationsState
from schemabridge.entrypoints.streamlit import m29_operations, m29_operations_scenario

_APP_SOURCE = """
from schemabridge.entrypoints.streamlit.m29_operations_scenario import (
    render_m29_operations_scenario,
)

render_m29_operations_scenario()
"""


def _rendered_text(app: AppTest) -> str:
    values: list[str] = []
    for collection in (
        app.title,
        app.subheader,
        app.caption,
        app.success,
        app.warning,
        app.error,
        app.info,
    ):
        values.extend(str(item.value) for item in collection)
    values.extend(f"{item.label}: {item.value}" for item in app.metric)
    return "\n".join(values)


def test_m29_panel_renders_the_accessible_six_state_control() -> None:
    app = AppTest.from_string(_APP_SOURCE, default_timeout=10).run()

    assert not app.exception
    selector = app.selectbox(key="m29-operations-state")
    assert selector.label == "Operational scenario"
    assert selector.options == [
        "Healthy",
        "Degraded",
        "Secret outage",
        "Queue backlog",
        "Stale backup",
        "Failed release",
    ]
    assert selector.help == (
        "Choose one deterministic synthetic state from the M29 operations matrix."
    )
    assert 'label_visibility="visible"' in inspect.getsource(
        m29_operations_scenario.select_m29_operations_state
    )


@pytest.mark.parametrize(
    ("state", "expected_copy", "expected_metric"),
    (
        (M29OperationsState.HEALTHY, "Healthy.", "Ready workloads: 7 of 7"),
        (M29OperationsState.DEGRADED, "Degraded.", "Telemetry delivery: Delayed"),
        (
            M29OperationsState.SECRET_OUTAGE,
            "Secret outage.",
            "Secret resolution: Unavailable",
        ),
        (
            M29OperationsState.QUEUE_BACKLOG,
            "Queue backlog.",
            "Queue depth: 840",
        ),
        (
            M29OperationsState.STALE_BACKUP,
            "Stale backup.",
            "Backup age: 94 minutes",
        ),
        (
            M29OperationsState.FAILED_RELEASE,
            "Failed release.",
            "Release gate: Blocked",
        ),
    ),
)
def test_m29_panel_renders_every_state_without_runtime_exceptions(
    state: M29OperationsState,
    expected_copy: str,
    expected_metric: str,
) -> None:
    app = AppTest.from_string(_APP_SOURCE, default_timeout=10).run()
    app.selectbox(key="m29-operations-state").set_value(state).run()

    assert not app.exception
    rendered = _rendered_text(app)
    assert expected_copy in rendered
    assert expected_metric in rendered
    assert "Deterministic local evidence" in rendered
    assert "operated production environment" in rendered


def test_m29_panel_escapes_hostile_text_and_has_no_unsafe_html_surface() -> None:
    app = AppTest.from_string(_APP_SOURCE, default_timeout=10).run()

    assert not app.exception
    captions = "\n".join(str(item.value) for item in app.caption)
    assert "&lt;script data-m29-hostile&gt;" in captions
    assert "&lt;/script&gt;" in captions
    assert "<script" not in captions

    renderer_source = inspect.getsource(m29_operations)
    scenario_source = inspect.getsource(m29_operations_scenario)
    combined = renderer_source + scenario_source
    for forbidden in (
        "unsafe_allow_html",
        "st.markdown",
        "st.html",
        "st.columns",
        "components.html",
        "<style",
        "width=",
    ):
        assert forbidden not in combined


def test_m29_panel_uses_a_single_native_flow_without_fixed_width_or_overflow_css() -> None:
    renderer_source = inspect.getsource(m29_operations.render_m29_operations_view)

    assert "st.metric" in renderer_source
    assert "st.caption" in renderer_source
    assert "st.columns" not in renderer_source
    assert "min-width" not in renderer_source
    assert "max-width" not in renderer_source
    assert "overflow" not in renderer_source
