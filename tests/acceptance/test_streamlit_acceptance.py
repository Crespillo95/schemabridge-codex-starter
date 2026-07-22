from __future__ import annotations

import os
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest


@pytest.mark.acceptance
def test_streamlit_north_star_is_governed_and_reachable_in_three_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.environ.get("SCHEMABRIDGE_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("SCHEMABRIDGE_TEST_DATABASE_URL is required for Streamlit acceptance")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SCHEMABRIDGE_DRAFT_STORE_PATH", str(tmp_path / "ui-acceptance.db"))
    app_path = Path(__file__).parents[2] / "src/schemabridge/entrypoints/streamlit/app.py"

    app = AppTest.from_file(str(app_path), default_timeout=20).run()
    assert not app.exception

    # Primary interaction 1: deterministic scenario load.
    app.button(key="load-demo").click().run()
    assert not app.exception
    assert app.radio(key="navigation").value == "Query Studio"
    assert app.button(key="confirm-intent")
    assert "approve-execution" not in {button.key for button in app.button}

    # Primary interaction 2: exact typed intent confirmation.
    app.button(key="confirm-intent").click().run()
    assert not app.exception
    assert app.button(key="approve-execution")

    # Primary interaction 3: approval bound to the independently validated plan.
    app.button(key="approve-execution").click().run()
    assert not app.exception

    frames = [item.value for item in app.dataframe]
    frame_columns = [tuple(str(column) for column in frame.columns) for frame in frames]
    result = next(
        (
            frame
            for frame in frames
            if set(frame.columns) == {"registration_date", "secondary_holder_customers"}
        ),
        None,
    )
    assert result is not None, {
        "frame_columns": frame_columns,
        "errors": [str(item.value) for item in app.error],
        "warnings": [str(item.value) for item in app.warning],
    }
    assert list(result["secondary_holder_customers"]) == [2, 1, 1]
    rejections = next(
        frame for frame in frames if set(frame.columns) == {"record", "code", "reason"}
    )
    assert list(rejections["code"]) == [
        "non_integral_identifier",
        "non_finite_identifier",
        "null_join_key",
    ]
    assert "127.5" in rejections.iloc[0]["reason"]
    assert "NaN" in rejections.iloc[1]["reason"]
    assert "NULL" in rejections.iloc[2]["reason"]

    visible = " ".join(
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
    assert "crm.customers" in visible
    assert "customer_to_account_holder" in visible
    assert "count_distinct → count_distinct" in visible
    assert "Traceback" not in visible
    assert "postgresql://" not in visible
    assert "OPENAI_API_KEY" not in visible
