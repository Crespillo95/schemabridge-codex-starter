"""Observable M27 Query Studio journeys through Streamlit's in-process browser."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest
from streamlit.testing.v1 import AppTest

import schemabridge.bootstrap as bootstrap
from schemabridge.config import Settings
from schemabridge.domain.identity import AuthenticatedPrincipal
from schemabridge.domain.query_studio import ProviderConfigurationFacts


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


@pytest.mark.acceptance
def test_natural_query_studio_recomputes_before_the_existing_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "SCHEMABRIDGE_DRAFT_STORE_PATH",
        str(tmp_path / "natural-query-studio.db"),
    )
    monkeypatch.setenv("SCHEMABRIDGE_JUDGE_EXECUTION", "recorded")
    monkeypatch.setenv("SCHEMABRIDGE_QUERY_STUDIO_AI_MODE", "fake")
    app = AppTest.from_file(str(_app_path()), default_timeout=20).run()

    app.radio(key="navigation").set_value("Query Studio").run()
    assert not app.exception
    assert "start-request" not in {button.key for button in app.button}
    assert app.radio(key="query-studio-mode").options == ["Natural", "Guiado"]
    assert not app.code
    assert "Query Studio AI · fake" in _visible(app)
    assert "Interpretación local determinista" in _visible(app)

    request = (
        "Agrupa por fecha de registro todos los clientes que sean segundo titular de una cuenta."
    )
    app.text_area(key="query-studio-natural-text").set_value(request)
    app.button(key="prepare-natural-query-studio").click().run()

    assert not app.exception
    assert app.button(key="confirm-natural-query-studio")
    assert not app.code
    visible = _visible(app)
    assert "Preview no ejecutable" in visible
    assert "Customer.registration_date" in visible
    assert "conexión warehouse-primary" in visible
    assert "Frescura · current" in visible
    assert "mapping v1" in visible
    assert "valor oculto" in visible
    assert "SECONDARY" not in visible

    # Any edit invalidates the exact signed preview. A broad one-word request
    # fails as a bounded closure overflow, never as an executable fallback.
    app.text_area(key="query-studio-natural-text").set_value("cuenta")
    app.run()
    assert "confirm-natural-query-studio" not in {button.key for button in app.button}
    app.button(key="prepare-natural-query-studio").click().run()
    assert "supera el cierre seguro" in _visible(app)
    assert "confirm-natural-query-studio" not in {button.key for button in app.button}

    app.text_area(key="query-studio-natural-text").set_value(request)
    app.run()
    app.button(key="prepare-natural-query-studio").click().run()
    app.button(key="confirm-natural-query-studio").click().run()

    assert not app.exception
    assert app.code
    assert app.button(key="approve-execution")
    visible = _visible(app)
    assert "values hidden" in visible
    assert "Validated request fingerprint:" in visible
    assert "Resolved plan fingerprint:" in visible
    assert "M26 semantic context gate:" not in visible
    assert (
        app.download_button(key="download-plan").label == "Download sanitized plan inspection JSON"
    )
    assert "SECONDARY" not in visible
    assert "OPENAI_API_KEY" not in visible
    assert "Traceback" not in visible


@pytest.mark.acceptance
@pytest.mark.parametrize(
    ("ai_mode", "expected_notice", "natural_disabled"),
    (
        ("live", "IA externa configurada", False),
        ("disabled", "La interpretación automática está desactivada", True),
    ),
)
def test_query_studio_renders_the_composed_live_or_disabled_ai_mode(
    ai_mode: Literal["disabled", "live"],
    expected_notice: str,
    natural_disabled: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv(
        "SCHEMABRIDGE_DRAFT_STORE_PATH",
        str(tmp_path / f"query-studio-{ai_mode}-presentation.db"),
    )
    monkeypatch.setenv("SCHEMABRIDGE_QUERY_STUDIO_AI_MODE", "fake")
    original_builder = bootstrap.build_query_studio_runtime

    def presentation_runtime(
        *,
        principal: AuthenticatedPrincipal,
        repository_root: Path | None = None,
        settings: Settings | None = None,
    ) -> bootstrap.QueryStudioRuntimeServices:
        runtime = original_builder(
            principal=principal,
            repository_root=repository_root,
            settings=settings,
        )
        configuration = ProviderConfigurationFacts.create(
            adapter="openai-responses" if ai_mode == "live" else "disabled",
            model_snapshot="sentinel-model",
            reasoning_effort="low" if ai_mode == "live" else "none",
            endpoint_region="sentinel-region",
            prompt_version="m27-ui-test-v1",
            schema_version="m27-ui-test-v1",
            matcher_version="m27-ui-test-v1",
            attempt_policy_version="m27-ui-attempts-v1",
            external_ai=ai_mode == "live",
        )
        return replace(
            runtime,
            ai_mode=ai_mode,
            configuration=configuration,
            prepare_natural=None if ai_mode == "disabled" else runtime.prepare_natural,
            confirm_natural=None if ai_mode == "disabled" else runtime.confirm_natural,
            recompute_natural=(None if ai_mode == "disabled" else runtime.recompute_natural),
            expansion=None if ai_mode == "disabled" else runtime.expansion,
        )

    monkeypatch.setattr(bootstrap, "build_query_studio_runtime", presentation_runtime)
    app = AppTest.from_file(str(_app_path()), default_timeout=20).run()
    app.radio(key="navigation").set_value("Query Studio").run()
    app.text_area(key="query-studio-natural-text").set_value("clientes por fecha").run()

    assert not app.exception
    visible = _visible(app)
    assert f"Query Studio AI · {ai_mode}" in visible
    assert expected_notice in visible
    assert ("sentinel-model" in visible) is (ai_mode == "live")
    assert ("sentinel-region" in visible) is (ai_mode == "live")
    assert app.button(key="prepare-natural-query-studio").disabled is natural_disabled
    assert "IA externa activada" not in visible
    assert "OPENAI_API_KEY" not in visible
    assert "postgresql://" not in visible


@pytest.mark.acceptance
def test_natural_typed_edit_hides_old_filter_and_requires_server_resign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "SCHEMABRIDGE_DRAFT_STORE_PATH",
        str(tmp_path / "natural-query-studio-edit.db"),
    )
    monkeypatch.setenv("SCHEMABRIDGE_JUDGE_EXECUTION", "recorded")
    monkeypatch.setenv("SCHEMABRIDGE_QUERY_STUDIO_AI_MODE", "fake")
    app = AppTest.from_file(str(_app_path()), default_timeout=20).run()
    app.radio(key="navigation").set_value("Query Studio").run()
    app.text_area(key="query-studio-natural-text").set_value(
        "Agrupa por fecha de registro los clientes que sean segundo titular de una cuenta."
    )
    app.button(key="prepare-natural-query-studio").click().run()

    assert not app.exception
    assert "valor actual: valor oculto" in _visible(app)
    assert "SECONDARY" not in _visible(app)
    replacement = next(
        item for item in app.checkbox if str(item.key).startswith("natural-edit-filter-replace-")
    )
    replacement.set_value(True).run()

    assert not app.exception
    assert "requiere un valor tipado explícito" in _visible(app)
    assert "confirm-natural-query-studio" not in {button.key for button in app.button}
    assert "resign-natural-query-studio" not in {button.key for button in app.button}
    explicit_value = app.selectbox(key="natural-edit-filter-value-0")
    assert explicit_value.value is None
    explicit_value.set_value("PRIMARY").run()

    assert not app.exception
    assert app.button(key="resign-natural-query-studio")
    assert "confirm-natural-query-studio" not in {button.key for button in app.button}
    assert "Firma invalidada por la edición" in _visible(app)
    app.button(key="resign-natural-query-studio").click().run()

    assert not app.exception
    assert app.button(key="confirm-natural-query-studio")
    assert "valor actual: valor oculto" in _visible(app)
    assert "PRIMARY" not in _visible(app)
    assert "natural-edit-filter-value-0" not in {str(item.key) for item in app.selectbox}
    app.button(key="confirm-natural-query-studio").click().run()

    assert not app.exception
    assert app.code
    assert app.button(key="approve-execution")
    assert "PRIMARY" not in _visible(app)


@pytest.mark.acceptance
def test_guided_query_studio_uses_bounded_keyset_pages_and_explicit_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "SCHEMABRIDGE_DRAFT_STORE_PATH",
        str(tmp_path / "guided-query-studio.db"),
    )
    monkeypatch.setenv("SCHEMABRIDGE_JUDGE_EXECUTION", "recorded")
    monkeypatch.setenv("SCHEMABRIDGE_QUERY_STUDIO_AI_MODE", "fake")
    app = AppTest.from_file(str(_app_path()), default_timeout=20).run()

    app.radio(key="navigation").set_value("Query Studio").run()
    app.radio(key="query-studio-mode").set_value("Guiado").run()
    app.text_input(key="query-studio-guided-query").set_value("product")
    app.button(key="search-guided-query-studio").click().run()

    assert not app.exception
    assert app.button(key="next-guided-query-studio")
    assert len(app.multiselect(key="guided-page-picks-1").options) == 10
    visible = _visible(app)
    assert "página 1" in visible
    assert "nunca se carga una lista global" in visible
    assert "conexión warehouse-primary" in visible
    assert "versión de mapping 1" in visible

    app.button(key="search-physical-query-studio").click().run()
    assert not app.exception
    visible = _visible(app)
    assert "Inventario físico no gobernado" in visible
    assert "needs_mapping_review" in visible
    assert "no seleccionable" in visible

    app.button(key="next-guided-query-studio").click().run()
    assert not app.exception
    assert "página 2" in _visible(app)

    # A fresh bounded search returns page one; only two opaque selections are
    # retained in the 12-field browser basket.
    app.button(key="search-guided-query-studio").click().run()
    app.multiselect(key="guided-page-picks-1").set_value(
        ["Product.product_key", "Product.category"]
    )
    app.button(key="add-guided-page-1").click().run()
    app.selectbox(key="guided-primary").set_value("Product.product_key")
    app.multiselect(key="guided-dimensions").set_value(["Product.category"])
    app.multiselect(key="guided-metrics").set_value(["Product.product_key"])
    app.run()

    assert not app.exception
    assert not app.code
    assert not app.button(key="prepare-guided-query-studio").disabled
    app.button(key="prepare-guided-query-studio").click().run()

    assert not app.exception
    assert app.button(key="confirm-guided-query-studio")
    assert not app.code
    assert "Solicitud tipada interpretada" in _visible(app)
    app.button(key="confirm-guided-query-studio").click().run()

    assert not app.exception
    assert app.code
    assert app.button(key="approve-execution")
    visible = _visible(app)
    assert "values hidden" in visible
    assert "Traceback" not in visible
    assert "postgresql://" not in visible


@pytest.mark.acceptance
def test_natural_query_studio_distinguishes_sensitive_and_no_match_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "SCHEMABRIDGE_DRAFT_STORE_PATH",
        str(tmp_path / "natural-query-studio-negative.db"),
    )
    monkeypatch.setenv("SCHEMABRIDGE_JUDGE_EXECUTION", "recorded")
    monkeypatch.setenv("SCHEMABRIDGE_QUERY_STUDIO_AI_MODE", "fake")
    app = AppTest.from_file(str(_app_path()), default_timeout=20).run()
    app.radio(key="navigation").set_value("Query Studio").run()

    secret_marker = "do-not-render-this-password"
    app.text_area(key="query-studio-natural-text").set_value(
        f"postgresql://reader:{secret_marker}@example.test/source"
    )
    app.button(key="prepare-natural-query-studio").click().run()

    assert not app.exception
    visible = _visible(app)
    assert "se bloqueó antes de enviarla" in visible
    assert "petición bloqueada no se vuelve a mostrar" in visible
    assert secret_marker not in visible
    assert app.text_area(key="query-studio-natural-text").value == ""
    assert "confirm-natural-query-studio" not in {button.key for button in app.button}
    assert not app.code

    app.text_area(key="query-studio-natural-text").set_value("teletransporta unicornios por color")
    app.run()
    app.button(key="prepare-natural-query-studio").click().run()

    assert not app.exception
    assert "Sin match gobernado" in _visible(app)
    assert "confirm-natural-query-studio" not in {button.key for button in app.button}
    assert not app.code

    app.text_area(key="query-studio-natural-text").set_value("clientes por fecha \u202erevisión")
    app.run()
    assert app.code
    assert "<U+202E>" in str(app.code[0].value)
    assert "caracteres de control o direccionales" in _visible(app)


@pytest.mark.acceptance
def test_query_studio_cardinality_band_separates_41028_physical_fields_from_31_mappings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "SCHEMABRIDGE_DRAFT_STORE_PATH",
        str(tmp_path / "query-studio-cardinality.db"),
    )
    monkeypatch.setenv("SCHEMABRIDGE_QUERY_STUDIO_AI_MODE", "fake")
    original_builder = bootstrap.build_query_studio_runtime

    def scaled_runtime(
        *,
        principal: AuthenticatedPrincipal,
        repository_root: Path | None = None,
        settings: Settings | None = None,
    ) -> bootstrap.QueryStudioRuntimeServices:
        runtime = original_builder(
            principal=principal,
            repository_root=repository_root,
            settings=settings,
        )
        return replace(
            runtime,
            catalog_cardinality=runtime.catalog_cardinality.model_copy(
                update={
                    "connection_count": 7,
                    "asset_count": 5_434,
                    "field_count": 41_028,
                }
            ),
            governed_mapping_count=31,
        )

    monkeypatch.setattr(bootstrap, "build_query_studio_runtime", scaled_runtime)
    app = AppTest.from_file(str(_app_path()), default_timeout=20).run()
    app.radio(key="navigation").set_value("Query Studio").run()

    assert not app.exception
    metrics = {metric.label: str(metric.value) for metric in app.metric}
    assert metrics["Conexiones físicas"] == "7"
    assert metrics["Assets físicos"] == "5,434"
    assert metrics["Campos físicos"] == "41,028"
    assert metrics["Mappings gobernados"] == "31"
    visible = _visible(app)
    assert "inventario físico es descubrimiento" in visible
    assert "solo los mappings gobernados pueden participar" in visible
