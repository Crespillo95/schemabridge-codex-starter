"""Observable copy-first M32 journeys through Streamlit."""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from schemabridge.adapters.query_studio.advanced_fake_language import (
    M32_AMBIGUOUS_DATE_QUESTION_ES,
    M32_REFERENCE_QUESTION_ES,
    M32_SIMPLE_PRODUCTS_QUESTION_ES,
)

pytestmark = pytest.mark.acceptance


def _app_path() -> Path:
    return Path(__file__).parents[2] / "src/schemabridge/entrypoints/streamlit/app.py"


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


def _copy_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str,
) -> AppTest:
    monkeypatch.setenv(
        "SCHEMABRIDGE_DRAFT_STORE_PATH",
        str(tmp_path / f"{name}.db"),
    )
    monkeypatch.setenv("SCHEMABRIDGE_JUDGE_EXECUTION", "recorded")
    monkeypatch.setenv("SCHEMABRIDGE_QUERY_STUDIO_AI_MODE", "fake")
    app = AppTest.from_file(str(_app_path()), default_timeout=30).run()
    return app.radio(key="navigation").set_value("Query Studio").run()


def test_streamlit_advanced_copy_flow_previews_before_generating_standalone_sql(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _copy_app(tmp_path, monkeypatch, name="m32-advanced-copy")

    assert not app.exception
    visible = _visible(app)
    assert "SQL PostgreSQL para copiar" in visible
    assert "Resultado principal" in visible
    assert "no ejecuta consultas" in visible
    assert "Workflow gobernado con validación opcional" in visible

    app.text_area(key="m32-natural-sql-text").set_value(M32_REFERENCE_QUESTION_ES)
    app.button(key="m32-prepare-natural-sql").click().run()

    assert not app.exception
    assert not app.code
    assert "m32-download-copyable-sql" not in {button.key for button in app.download_button}
    visible = _visible(app)
    assert "Interpretación tipada para confirmar" in visible
    assert "No existe SQL todavía" in visible
    assert "SaleLine" in visible
    assert "SalesOrder" in visible
    assert "Product" in visible
    assert "sales.order_lines" in visible
    assert "sales.orders" in visible
    assert "commerce.products" in visible
    assert "sales_order_to_sale_line" in visible
    assert "product_to_sale_line" in visible
    assert "HAVING tipado" in visible
    assert "row_number" in visible
    assert "percent_of_total" in visible
    assert "running_sum" in visible
    assert "revenue_rank less_than_or_equal 3" in visible
    assert app.button(key="m32-confirm-generate-natural-sql").disabled

    app.checkbox(key="m32-natural-sql-reviewed").set_value(True).run()
    assert not app.button(key="m32-confirm-generate-natural-sql").disabled
    app.button(key="m32-confirm-generate-natural-sql").click().run()

    assert not app.exception
    assert len(app.code) == 1
    sql = str(app.code[0].value)
    assert sql.startswith("WITH")
    assert "%s" not in sql
    assert "$1" not in sql
    assert "LIMIT 100" in sql
    assert (
        app.download_button(key="m32-download-copyable-sql").label
        == "Descargar SQL PostgreSQL standalone"
    )
    assert app.button(key="m32-optional-validation-disabled").disabled
    visible = _visible(app)
    assert "El artefacto no se ha enviado a ningún ejecutor" in visible
    assert "Validación/ejecución opcional deshabilitada por defecto" in visible
    assert "executed=false" in visible


def test_streamlit_simple_copy_flow_uses_the_same_confirmed_non_execution_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _copy_app(tmp_path, monkeypatch, name="m32-simple-copy")
    app.text_area(key="m32-natural-sql-text").set_value(M32_SIMPLE_PRODUCTS_QUESTION_ES)
    app.button(key="m32-prepare-natural-sql").click().run()

    assert not app.exception
    assert not app.code
    visible = _visible(app)
    assert "Product" in visible
    assert "Product.product_key" in visible
    assert "Product.category" in visible
    assert "Product.unit_price" in visible
    assert "No se requiere join" in visible

    app.checkbox(key="m32-natural-sql-reviewed").set_value(True).run()
    app.button(key="m32-confirm-generate-natural-sql").click().run()

    assert not app.exception
    sql = str(app.code[0].value)
    assert "commerce.products" in sql
    assert "LIMIT 50" in sql
    assert "%s" not in sql
    assert app.button(key="m32-optional-validation-disabled").disabled
    execution_metric = next(item for item in app.metric if item.label == "Ejecutado")
    assert execution_metric.value == "No"

    app.text_area(key="m32-natural-sql-text").set_value(M32_AMBIGUOUS_DATE_QUESTION_ES).run()

    assert not app.exception
    assert not app.code
    assert "m32-download-copyable-sql" not in {button.key for button in app.download_button}


def test_streamlit_ambiguity_produces_no_confirmation_or_sql(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _copy_app(tmp_path, monkeypatch, name="m32-ambiguous-copy")
    app.text_area(key="m32-natural-sql-text").set_value(M32_AMBIGUOUS_DATE_QUESTION_ES)
    app.button(key="m32-prepare-natural-sql").click().run()

    assert not app.exception
    assert not app.code
    assert "m32-confirm-generate-natural-sql" not in {button.key for button in app.button}
    assert "m32-download-copyable-sql" not in {button.key for button in app.download_button}
    visible = _visible(app)
    assert "necesita aclaración" in visible
    assert "date_meaning" in visible
    assert "No se ha generado SQL" in visible
