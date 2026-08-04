"""Observable copy-first M32 journeys through Streamlit."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
import streamlit.testing.v1.app_test as streamlit_app_test
from streamlit.runtime.memory_media_file_storage import MemoryMediaFileStorage
from streamlit.testing.v1 import AppTest

from schemabridge.adapters.query_studio.advanced_fake_language import (
    M32_AMBIGUOUS_DATE_QUESTION_ES,
    M32_REFERENCE_QUESTION_ES,
    M32_SIMPLE_PRODUCTS_QUESTION_ES,
)

pytestmark = pytest.mark.acceptance


def _app_path() -> Path:
    return Path(__file__).parents[2] / "src/schemabridge/entrypoints/streamlit/app.py"


def _target_app_path() -> Path:
    return Path(__file__).parents[1] / "m32_target_streamlit_app.py"


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


def _capture_media_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> list[MemoryMediaFileStorage]:
    storages: list[MemoryMediaFileStorage] = []

    def build_storage(media_endpoint: str) -> MemoryMediaFileStorage:
        storage = MemoryMediaFileStorage(media_endpoint)
        storages.append(storage)
        return storage

    monkeypatch.setattr(streamlit_app_test, "MemoryMediaFileStorage", build_storage)
    return storages


def _assert_exact_copy_and_download_bytes(
    app: AppTest,
    storages: list[MemoryMediaFileStorage],
) -> None:
    assert len(app.code) == 1
    assert storages
    code_text = app.code[0].proto.code_text
    download_button = app.download_button(key="m32-download-copyable-sql")
    download_name = Path(download_button.proto.url).name
    download = storages[-1].get_file(download_name)
    expected_bytes = code_text.encode("utf-8")
    expected_sha256 = hashlib.sha256(expected_bytes).hexdigest()

    assert str(app.code[0].value) == code_text
    assert download.content == expected_bytes
    assert download.mimetype == "text/plain"
    assert download.filename == f"schemabridge-{expected_sha256[:12]}.sql"
    assert f"SQL SHA-256: `{expected_sha256}`" in _visible(app)
    assert not download.content.startswith(b"\xef\xbb\xbf")
    assert not code_text.endswith("\n")
    assert not download.content.endswith(b"\n")
    assert "%s" not in code_text
    assert re.search(r"\$\d+", code_text) is None
    assert "executed=false" in _visible(app)
    execution_metric = next(item for item in app.metric if item.label == "Ejecutado")
    assert execution_metric.value == "No"


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
    storages = _capture_media_storage(monkeypatch)
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
    assert "no apto para uso comercial ni producción" in visible
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
    target_metric = next(item for item in app.metric if item.label == "Destino gobernado")
    assert target_metric.value == "Sin ligar"
    _assert_exact_copy_and_download_bytes(app, storages)


def test_streamlit_simple_copy_flow_uses_the_same_confirmed_non_execution_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storages = _capture_media_storage(monkeypatch)
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
    _assert_exact_copy_and_download_bytes(app, storages)

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


def test_managed_streamlit_purges_download_when_target_rotates_after_artifact() -> None:
    app = AppTest.from_file(str(_target_app_path()), default_timeout=30).run()
    app.text_area(key="m32-natural-sql-text").set_value(M32_SIMPLE_PRODUCTS_QUESTION_ES)
    app.button(key="m32-prepare-natural-sql").click().run()

    assert not app.exception
    assert not app.code
    assert "Destino gobernado para esta confirmación" in _visible(app)
    app.checkbox(key="m32-natural-sql-reviewed").set_value(True).run()
    app.button(key="m32-confirm-generate-natural-sql").click().run()

    assert not app.exception
    assert len(app.code) == 1
    assert app.download_button(key="m32-download-copyable-sql")
    target_metric = next(item for item in app.metric if item.label == "Destino gobernado")
    assert target_metric.value == "Ligado"

    app.button(key="rotate-synthetic-target").click().run()

    assert not app.exception
    assert not app.code
    assert "m32-download-copyable-sql" not in {button.key for button in app.download_button}
    visible = _visible(app)
    assert "natural_sql_target_mismatch" in visible
    assert "No se ha generado ni ejecutado SQL" in visible
