from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from streamlit.testing.v1 import AppTest

from schemabridge.entrypoints.streamlit.query_studio import (
    _query_studio_ai_mode_copy,
    _query_studio_live_disclosure_copy,
)


def test_streamlit_empty_state_has_specified_navigation_and_mode_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SCHEMABRIDGE_DRAFT_STORE_PATH", str(tmp_path / "ui-components.db"))
    app_path = Path(__file__).parents[2] / "src/schemabridge/entrypoints/streamlit/app.py"

    app = AppTest.from_file(str(app_path), default_timeout=15).run()

    assert not app.exception
    assert app.button(key="load-demo").label == "Load demo scenario"
    assert app.radio(key="navigation").options == [
        "Overview",
        "Semantic Models",
        "Relationships",
        "Query Studio",
        "Decisions",
        "Operations",
    ]
    rendered = " ".join(str(item.value) for item in (*app.markdown, *app.caption, *app.info))
    assert "Recorded catalog" in rendered
    assert "Demo candidate evidence" in rendered
    assert "Demo workflow intent" in rendered
    assert "Deterministic fake parser" in rendered
    assert "No scenario loaded" in rendered


def test_streamlit_explains_recorded_count_distinct_fanout_mitigation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SCHEMABRIDGE_JUDGE_EXECUTION", "recorded")
    monkeypatch.setenv("SCHEMABRIDGE_DRAFT_STORE_PATH", str(tmp_path / "ui-fanout.db"))
    app_path = Path(__file__).parents[2] / "src/schemabridge/entrypoints/streamlit/app.py"

    app = AppTest.from_file(str(app_path), default_timeout=15).run()
    app.button(key="load-demo").click().run()
    app.button(key="confirm-intent").click().run()

    assert not app.exception
    rendered = " ".join(
        str(item.value) for item in (*app.markdown, *app.caption, *app.info, *app.warning)
    )
    assert (
        "COUNT DISTINCT already mitigates the recorded one-to-many fanout for the "
        "affected metric; no additional rewrite is needed."
    ) in rendered
    assert "No fanout mitigation required" not in rendered


@pytest.mark.parametrize(
    ("ai_mode", "expected"),
    (
        (
            "live",
            "Query Studio AI · live · modelo sentinel-model · región sentinel-region · "
            "El texto de negocio normalizado y el cierre público gobernado y acotado salen "
            "del sistema; el modo Guiado sigue disponible.",
        ),
        (
            "fake",
            "Query Studio AI · fake · Interpretación local determinista · "
            "no se usa una API externa.",
        ),
        (
            "disabled",
            "Query Studio AI · disabled · Interpretación automática desactivada · "
            "no se usa una API externa.",
        ),
    ),
)
def test_query_studio_ai_copy_reports_only_the_composed_runtime_mode(
    ai_mode: Literal["disabled", "fake", "live"],
    expected: str,
) -> None:
    rendered = _query_studio_ai_mode_copy(
        ai_mode,
        model_snapshot="sentinel-model",
        endpoint_region="sentinel-region",
    )

    assert rendered == expected
    if ai_mode != "live":
        assert "sentinel-model" not in rendered
        assert "sentinel-region" not in rendered
    else:
        assert "texto de negocio normalizado" in rendered
        assert "cierre público gobernado y acotado" in rendered
        assert "salen del sistema" in rendered
        assert "modo Guiado sigue disponible" in rendered
    assert "OPENAI_API_KEY" not in rendered
    assert "postgresql://" not in rendered


def test_query_studio_live_disclosure_names_exact_bounded_egress_and_guided_fallback() -> None:
    rendered = _query_studio_live_disclosure_copy()

    assert "texto de negocio normalizado" in rendered
    assert "cierre público gobernado y acotado" in rendered
    assert "saldrán del sistema" in rendered
    assert "hasta 3 modelos, 12 campos y 2 joins" in rendered
    assert "modo Guiado sigue disponible" in rendered
    assert "No se envían filas, credenciales, SQL ni valores de parámetros" in rendered
    assert "OPENAI_API_KEY" not in rendered
    assert "postgresql://" not in rendered


def test_streamlit_empty_state_renders_every_read_only_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SCHEMABRIDGE_DRAFT_STORE_PATH", str(tmp_path / "ui-workspaces.db"))
    app_path = Path(__file__).parents[2] / "src/schemabridge/entrypoints/streamlit/app.py"
    app = AppTest.from_file(str(app_path), default_timeout=15).run()

    expected_copy = {
        "Semantic Models": "Confidence prioritizes review",
        "Relationships": "Only versioned approved contracts",
        "Query Studio": "Crea una solicitud Natural o Guiada",
        "Decisions": "Load a scenario to record",
    }
    for page, copy in expected_copy.items():
        app.radio(key="navigation").set_value(page).run()
        assert not app.exception
        visible = " ".join(
            str(item.value) for item in (*app.markdown, *app.caption, *app.info, *app.warning)
        )
        assert copy in visible
        if page == "Semantic Models":
            assert "7 models · 31 mappings · 5 joins" in visible
            assert (
                "fingerprint "
                "0710148874049078f751ac96f0a18131cf01daa41f27dc034ca52a8b212dd966" in visible
            )
