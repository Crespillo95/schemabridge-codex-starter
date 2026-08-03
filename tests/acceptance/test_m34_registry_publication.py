"""Browser-visible M34 publication from reserved target to activation handoff."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

pytestmark = pytest.mark.acceptance


def _app_path() -> Path:
    return Path(__file__).parents[2] / "scripts/m34_registry_publication_scenario_app.py"


def _visible(app: AppTest) -> str:
    rendered = [
        str(item.value)
        for item in (
            *app.title,
            *app.header,
            *app.subheader,
            *app.markdown,
            *app.caption,
            *app.info,
            *app.success,
            *app.warning,
            *app.error,
        )
    ]
    rendered.extend(f"{item.label} {item.value}" for item in app.metric)
    return " ".join(rendered)


def _configure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token = "pytest-" + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:16]
    monkeypatch.setenv("SCHEMABRIDGE_M34_SCENARIO_TOKEN", token)


def test_queued_approval_and_exact_readback_leave_active_pointer_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(tmp_path, monkeypatch)
    app = AppTest.from_file(str(_app_path()), default_timeout=20).run()

    assert not app.exception
    visible = _visible(app)
    assert "ready_for_publication" in visible
    assert "Active registry pointer not_configured" in visible
    assert "External writes 0" in visible
    assert "Immutable DataHub versions 0" in visible
    assert not app.code

    app = app.button(key="m34-submit").click().run()

    assert not app.exception
    visible = _visible(app)
    assert "Publication status queued" in visible
    assert "Target reservado" in visible
    assert "External writes 0" in visible
    assert "Active registry pointer not_configured" in visible

    app = app.button(key="m34-run-prepare").click().run()

    assert not app.exception
    visible = _visible(app)
    assert "Publication status awaiting_approval" in visible
    assert "Candidato v2 completo" in visible
    assert "opaque-9f82" in visible
    assert "DataHub todavía tiene 0 versiones" in visible
    assert "External writes 0" in visible
    assert app.button(key="m34-authorize").disabled

    app = app.checkbox(key="m34-confirm-candidate").set_value(True).run()
    assert not app.button(key="m34-authorize").disabled
    app = app.button(key="m34-authorize").click().run()

    assert not app.exception
    visible = _visible(app)
    assert "Publication status approved" in visible
    assert "Candidato exacto autorizado" in visible
    assert "External writes 0" in visible

    # Re-open the synthetic operator page as a fresh browser session. The control-plane
    # aggregate lives in the cached runtime, not in the removed confirmation widget.
    app = AppTest.from_file(str(_app_path()), default_timeout=20).run()
    app = app.button(key="m34-run-publish").click().run()

    assert not app.exception
    visible = _visible(app)
    assert "Publication status activation_ready" in visible
    assert "Documento v2 inmutable publicado" in visible
    assert "queued → leased → awaiting_approval → approved → leased → activation_ready" in visible
    assert "External writes 1" in visible
    assert "Immutable DataHub versions 1" in visible
    assert "Active registry pointer not_configured" in visible
    assert "No automatic activation" in visible
    assert "opaque-9f82" in visible
    assert "postgresql://" not in visible
    assert "synthetic-m34-writer-token" not in visible
    assert not app.code
    assert not app.download_button


def test_invalid_m34_scenario_configuration_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCHEMABRIDGE_M34_SCENARIO_TOKEN", "../../invalid")

    app = AppTest.from_file(str(_app_path()), default_timeout=20).run()

    assert not app.exception
    visible = _visible(app)
    assert "m34_scenario_configuration_invalid" in visible
    assert "Traceback" not in visible
    assert "postgresql://" not in visible
    assert not app.button
