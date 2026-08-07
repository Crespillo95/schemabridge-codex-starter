"""Browser-visible M28 connector/cost states through production components."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from scripts.m28_connector_scenario_app import _build_acceptance_principal
from streamlit.testing.v1 import AppTest
from tests.m28_browser_support import public_m28_state

from schemabridge.entrypoints.streamlit.connector_acceptance import (
    M28_BROWSER_RELEASE_REF,
    M28_BROWSER_SCENARIO_ENV,
    M28_BROWSER_STATE_FILE_ENV,
    M28_HOSTILE_METADATA,
    M28BrowserScenario,
    is_m28_private_environment_key,
)


def _app_path() -> Path:
    return Path(__file__).parents[2] / "scripts/m28_connector_scenario_app.py"


def _visible(app: AppTest) -> str:
    return " ".join(
        str(item.value)
        for item in (
            *app.markdown,
            *app.caption,
            *app.info,
            *app.success,
            *app.warning,
            *app.error,
        )
    )


def _configure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: M28BrowserScenario,
) -> None:
    for name in tuple(os.environ):
        if is_m28_private_environment_key(name):
            monkeypatch.delenv(name, raising=False)
    state_path = tmp_path / "m28-browser-state.json"
    state_path.write_bytes(public_m28_state().to_json())
    state_path.chmod(0o600)
    monkeypatch.setenv("SCHEMABRIDGE_ENVIRONMENT", "development")
    monkeypatch.setenv("SCHEMABRIDGE_AUTH_MODE", "local-demo")
    monkeypatch.setenv("SCHEMABRIDGE_RELEASE_REF", M28_BROWSER_RELEASE_REF)
    monkeypatch.setenv(M28_BROWSER_STATE_FILE_ENV, str(state_path))
    monkeypatch.setenv(M28_BROWSER_SCENARIO_ENV, scenario.value)


@pytest.mark.acceptance
@pytest.mark.parametrize(
    ("scenario", "revision", "reader", "approved_rows"),
    (
        (
            M28BrowserScenario.TENANT_A_ACCEPTED,
            "r1",
            "sb_m28_a_reader_deadbeef",
            2,
        ),
        (
            M28BrowserScenario.TENANT_B_ACCEPTED,
            "r7",
            "sb_m28_b_reader_deadbeef",
            3,
        ),
    ),
)
def test_accepted_tenant_requires_click_then_shows_only_its_aggregate_result(
    scenario: M28BrowserScenario,
    revision: str,
    reader: str,
    approved_rows: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(tmp_path, monkeypatch, scenario)
    app = AppTest.from_file(str(_app_path()), default_timeout=20).run()

    assert not app.exception
    metrics = {metric.label: str(metric.value) for metric in app.metric}
    assert metrics["Connection"] == "warehouse-primary"
    assert metrics["Dialect"] == "postgresql"
    assert metrics["Route revision"] == revision
    assert metrics["Cost decision"] == "Accepted"
    assert app.button(key="approve-execution")
    assert not app.dataframe
    assert not app.code

    app.button(key="approve-execution").click().run()

    assert not app.exception
    assert "approve-execution" not in {button.key for button in app.button}
    metrics = {metric.label: str(metric.value) for metric in app.metric}
    assert metrics["Reader"] == reader
    result = next(
        (
            item.value
            for item in app.dataframe
            if tuple(str(column) for column in item.value.columns) == ("approved_rows",)
        ),
        None,
    )
    assert result is not None
    assert result.to_dict(orient="records") == [{"approved_rows": approved_rows}]
    visible = _visible(app)
    other_reader = (
        "sb_m28_b_reader_deadbeef"
        if scenario is M28BrowserScenario.TENANT_A_ACCEPTED
        else "sb_m28_a_reader_deadbeef"
    )
    assert other_reader not in visible
    _assert_no_private_surface(app)


@pytest.mark.acceptance
@pytest.mark.parametrize(
    ("scenario", "expected"),
    (
        (M28BrowserScenario.COST_REJECTED, "total_cost_exceeded"),
        (M28BrowserScenario.ROUTE_DISABLED, "connector_route_disabled"),
        (M28BrowserScenario.ROUTE_STALE, "connector_route_stale"),
        (M28BrowserScenario.ROUTE_UNAVAILABLE, "connector_target_unavailable"),
        (M28BrowserScenario.EXPLAIN_TIMEOUT, "query_cost_timeout"),
        (
            M28BrowserScenario.UNSUPPORTED_DIALECT,
            "connector_dialect_unsupported",
        ),
        (
            M28BrowserScenario.ROTATED_AFTER_CONFIRMATION,
            "connector_route_stale",
        ),
    ),
)
def test_closed_connector_and_cost_states_never_render_an_execution_action(
    scenario: M28BrowserScenario,
    expected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(tmp_path, monkeypatch, scenario)
    app = AppTest.from_file(str(_app_path()), default_timeout=20).run()

    assert not app.exception
    assert "approve-execution" not in {button.key for button in app.button}
    assert expected in _visible(app)
    assert not app.dataframe
    assert not app.code
    metrics = {metric.label: str(metric.value) for metric in app.metric}
    assert metrics["Connection"] == "warehouse-primary"
    assert metrics["Route revision"] in {"r1", "r2"}
    assert "Cost decision" in metrics
    _assert_no_private_surface(app)


@pytest.mark.acceptance
def test_authenticated_scope_and_hostile_metadata_are_visible_as_public_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(tmp_path, monkeypatch, M28BrowserScenario.TENANT_A_ACCEPTED)
    expected = _build_acceptance_principal()
    app = AppTest.from_file(str(_app_path()), default_timeout=20).run()

    assert not app.exception
    visible = _visible(app)
    assert "Method · local_demo" in visible
    assert "Roles · analyst" in visible
    assert f"Principal · …{expected.actor_id[-12:]}" in visible
    assert f"Workspace · …{expected.workspace_id[-12:]}" in visible
    assert expected.actor_id not in visible
    assert expected.workspace_id not in visible
    assert "m28-browser-acceptance" not in visible
    assert "browser-operator" not in visible
    assert M28_HOSTILE_METADATA in visible
    _assert_no_private_surface(app)


@pytest.mark.acceptance
def test_app_fails_closed_if_any_unenumerated_private_environment_key_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(tmp_path, monkeypatch, M28BrowserScenario.TENANT_A_ACCEPTED)
    monkeypatch.setenv("FUTURE_PROFILE_DATABASE_URL", "postgresql://private")
    app = AppTest.from_file(str(_app_path()), default_timeout=20).run()

    assert not app.exception
    assert "approve-execution" not in {button.key for button in app.button}
    assert "m28_browser_state_invalid" in _visible(app)
    assert not app.dataframe
    assert not app.code


@pytest.mark.acceptance
def test_app_allows_streamlit_empty_mapbox_placeholder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(tmp_path, monkeypatch, M28BrowserScenario.TENANT_A_ACCEPTED)
    monkeypatch.setenv("MAPBOX_API_KEY", "")
    app = AppTest.from_file(str(_app_path()), default_timeout=20).run()

    assert not app.exception
    assert "m28_browser_state_invalid" not in _visible(app)
    assert app.button(key="approve-execution")


def _assert_no_private_surface(app: AppTest) -> None:
    visible = _visible(app)
    for forbidden in (
        "postgresql://",
        "hidden_canary",
        "m28_canary_",
        "private-host",
        "password",
        "raw plan",
        "bound parameters:",
        "traceback",
    ):
        assert forbidden not in visible.casefold()
