"""Browser-visible M27 states from the isolated, key-free scenario runtime."""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from schemabridge.entrypoints.streamlit.query_studio_acceptance import (
    M27_BROWSER_RELEASE_REF,
    M27_BROWSER_SCENARIO_ENV,
    M27_DELAYED_COMPLETE_BUTTON_KEY,
    M27_DELAYED_LOADING_TEXT,
    M27BrowserScenario,
)

NORTH_STAR = (
    "Agrupa por fecha de registro todos los clientes que sean segundo titular de una cuenta."
)


def _app_path() -> Path:
    return Path(__file__).parents[2] / "scripts/m27_query_studio_scenario_app.py"


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
    scenario: M27BrowserScenario,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SCHEMABRIDGE_ENVIRONMENT", "development")
    monkeypatch.setenv("SCHEMABRIDGE_AUTH_MODE", "local-demo")
    monkeypatch.setenv("SCHEMABRIDGE_PUBLICATION_MODE", "fake")
    monkeypatch.setenv("SCHEMABRIDGE_JUDGE_EXECUTION", "recorded")
    monkeypatch.setenv("SCHEMABRIDGE_QUERY_STUDIO_AI_MODE", "fake")
    monkeypatch.setenv("SCHEMABRIDGE_RELEASE_REF", M27_BROWSER_RELEASE_REF)
    monkeypatch.setenv(M27_BROWSER_SCENARIO_ENV, scenario.value)
    monkeypatch.setenv(
        "SCHEMABRIDGE_DRAFT_STORE_PATH",
        str(tmp_path / f"{scenario.value}.db"),
    )


@pytest.mark.acceptance
def test_baseline_wrapper_delegates_to_the_unchanged_fake_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(tmp_path, monkeypatch, M27BrowserScenario.BASELINE)
    app = AppTest.from_file(str(_app_path()), default_timeout=20).run()
    app.radio(key="navigation").set_value("Query Studio").run()
    app.text_area(key="query-studio-natural-text").set_value(NORTH_STAR)
    app.button(key="prepare-natural-query-studio").click().run()

    assert not app.exception
    visible = _visible(app)
    assert app.button(key="confirm-natural-query-studio")
    assert "Interpretación alineada" in visible
    assert "Query Studio AI · fake" in visible
    assert "OPENAI_API_KEY" not in visible
    assert "Traceback" not in visible


@pytest.mark.acceptance
@pytest.mark.parametrize(
    ("scenario", "expected"),
    (
        (
            M27BrowserScenario.CONFLICTING_INTENT,
            "Conflicto semántico",
        ),
        (
            M27BrowserScenario.PROVIDER_UNAVAILABLE,
            "servicio de interpretación no está disponible",
        ),
        (
            M27BrowserScenario.RATE_LIMITED,
            "Límite temporal de peticiones alcanzado",
        ),
        (
            M27BrowserScenario.QUOTA_EXHAUSTED,
            "Cuota de IA agotada",
        ),
    ),
)
def test_synthetic_scenario_is_visible_and_never_confirmable(
    scenario: M27BrowserScenario,
    expected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(tmp_path, monkeypatch, scenario)
    app = AppTest.from_file(str(_app_path()), default_timeout=20).run()
    app.radio(key="navigation").set_value("Query Studio").run()
    app.text_area(key="query-studio-natural-text").set_value(NORTH_STAR)
    app.button(key="prepare-natural-query-studio").click().run()

    assert not app.exception
    visible = _visible(app)
    assert expected in visible
    assert "Query Studio AI · fake" in visible
    assert "no se usa una API externa" in visible
    assert "confirm-natural-query-studio" not in {button.key for button in app.button}
    assert "OPENAI_API_KEY" not in visible
    assert "postgresql://" not in visible
    assert "Traceback" not in visible


@pytest.mark.acceptance
def test_delayed_scenario_keeps_an_explicit_loading_state_until_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(tmp_path, monkeypatch, M27BrowserScenario.DELAYED_EXPANSION)
    app = AppTest.from_file(str(_app_path()), default_timeout=20).run()
    app.radio(key="navigation").set_value("Query Studio").run()
    app.text_area(key="query-studio-natural-text").set_value(NORTH_STAR)
    app.button(key="prepare-natural-query-studio").click().run()

    assert not app.exception
    visible = _visible(app)
    assert M27_DELAYED_LOADING_TEXT in visible
    assert "expansión local determinista" in visible
    assert "Carga visible de aceptación" in visible
    assert app.button(key=M27_DELAYED_COMPLETE_BUTTON_KEY)
    assert "confirm-natural-query-studio" not in {button.key for button in app.button}
    assert "OPENAI_API_KEY" not in visible

    app.button(key=M27_DELAYED_COMPLETE_BUTTON_KEY).click().run()

    assert not app.exception
    visible = _visible(app)
    assert M27_DELAYED_LOADING_TEXT not in visible
    assert "Interpretación alineada" in visible
    assert app.button(key="confirm-natural-query-studio")
    assert "OPENAI_API_KEY" not in visible
    assert "Traceback" not in visible


@pytest.mark.acceptance
def test_expired_real_preview_token_is_rejected_and_cleared_in_streamlit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(tmp_path, monkeypatch, M27BrowserScenario.EXPIRED_TOKEN)
    app = AppTest.from_file(str(_app_path()), default_timeout=20).run()
    app.radio(key="navigation").set_value("Query Studio").run()
    app.text_area(key="query-studio-natural-text").set_value(NORTH_STAR)
    app.button(key="prepare-natural-query-studio").click().run()

    assert not app.exception
    assert app.button(key="confirm-natural-query-studio")
    app.button(key="confirm-natural-query-studio").click().run()

    assert not app.exception
    visible = _visible(app)
    assert "query_studio_stale_preview" in visible
    assert "preview caducó" in visible
    app.run()
    assert "confirm-natural-query-studio" not in {button.key for button in app.button}
    assert not app.code
    assert "OPENAI_API_KEY" not in visible
    assert "Traceback" not in visible
