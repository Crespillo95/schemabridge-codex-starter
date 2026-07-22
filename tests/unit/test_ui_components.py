from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest


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
    ]
    rendered = " ".join(str(item.value) for item in (*app.markdown, *app.caption, *app.info))
    assert "Recorded catalog" in rendered
    assert "Deterministic fake parser" in rendered
    assert "No scenario loaded" in rendered
