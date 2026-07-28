from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import pytest
from scripts import m27_browser_acceptance_runtime as runtime

from schemabridge.entrypoints.streamlit.query_studio_acceptance import (
    M27BrowserScenario,
)


def test_streamlit_scenario_selector_is_closed_and_defaults_to_baseline() -> None:
    parsed = runtime._parser().parse_args(("streamlit",))

    assert parsed.scenario == M27BrowserScenario.BASELINE.value
    for scenario in M27BrowserScenario:
        selected = runtime._parser().parse_args(("streamlit", "--scenario", scenario.value))
        assert selected.scenario == scenario.value
    with pytest.raises(SystemExit):
        runtime._parser().parse_args(("streamlit", "--scenario", "caller-controlled"))


def test_live_mode_rejects_synthetic_scenario_before_loading_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def must_not_load(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("state or provider-capable runtime was reached")

    monkeypatch.setattr(runtime, "_load_state", must_not_load)

    with pytest.raises(runtime.BrowserAcceptanceSetupError, match="key-free fake"):
        runtime._serve_streamlit(
            tmp_path / "missing",
            ai_mode="live",
            browser_scenario=M27BrowserScenario.PROVIDER_UNAVAILABLE,
        )
