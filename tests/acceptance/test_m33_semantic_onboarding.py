"""Browser-visible M33 onboarding from empty tenant to immutable handoff."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from schemabridge.domain.decisions import DecisionAction
from schemabridge.domain.identity import IdentityRole

pytestmark = pytest.mark.acceptance


def _app_path() -> Path:
    return Path(__file__).parents[2] / "scripts/m33_semantic_onboarding_scenario_app.py"


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
    monkeypatch.setenv("SCHEMABRIDGE_M33_SCENARIO_TOKEN", token)


def _record_approval(
    app: AppTest,
    *,
    target_kind: str,
    target_id: str,
    rationale: str,
    reference: str,
) -> AppTest:
    app.radio(key=f"m33-action-{target_kind}-{target_id}").set_value(DecisionAction.APPROVE.value)
    app.text_area(key=f"m33-rationale-{target_kind}-{target_id}").set_value(rationale)
    app.text_input(key=f"m33-reference-{target_kind}-{target_id}").set_value(reference)
    return app.button(key=f"m33-record-{target_kind}-{target_id}").click().run()


def test_empty_review_and_preparation_are_explicit_and_have_zero_external_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(tmp_path, monkeypatch)
    app = AppTest.from_file(str(_app_path()), default_timeout=20).run()

    assert not app.exception
    visible = _visible(app)
    assert "not_configured" in visible
    assert "No hay borradores" in visible
    assert "External writes 0" in visible
    assert "SQL generado 0" in visible
    assert not app.code
    assert not app.download_button
    assert all("demo" not in button.label.lower() for button in app.button)

    app = app.button(key="m33-create-draft").click().run()

    assert not app.exception
    visible = _visible(app)
    assert "commerce-orders-onboarding" in visible
    assert "needs_review" in visible
    assert "mapping-order-id" not in visible
    assert "Order.order_id" in visible
    assert "commerce.order_facts.order_id" in visible
    assert "catalog_definition" in visible
    assert "datahub_urn=not_observed_no_synthesis" in visible
    assert "Pendiente de revisión explícita" in visible
    assert "m33-record-model-Order" not in {button.key for button in app.button}

    app = app.selectbox(key="m33-authenticated-role").set_value(IdentityRole.STEWARD.value).run()
    app = _record_approval(
        app,
        target_kind="model",
        target_id="Order",
        rationale="La definición representa el pedido de negocio gobernado.",
        reference="ticket:SEM-301",
    )
    app = _record_approval(
        app,
        target_kind="mapping",
        target_id="mapping-order-id",
        rationale="La clave declarada identifica de forma estable el pedido exacto.",
        reference="ticket:SEM-302",
    )
    app = _record_approval(
        app,
        target_kind="mapping",
        target_id="mapping-total-amount",
        rationale="La definición y el tipo decimal confirman el importe gobernado.",
        reference="ticket:SEM-303",
    )
    app = _record_approval(
        app,
        target_kind="mapping",
        target_id="mapping-ordered-at",
        rationale="La definición temporal confirma el instante aceptado del pedido.",
        reference="ticket:SEM-304",
    )

    assert not app.exception
    visible = _visible(app)
    assert "Cierre de decisiones completo" in visible
    assert visible.count("decision_recorded") == 4
    assert "m33-prepare-proposal" not in {button.key for button in app.button}

    app = app.selectbox(key="m33-authenticated-role").set_value(IdentityRole.PUBLISHER.value).run()

    assert not app.exception
    assert app.button(key="m33-prepare-proposal").disabled
    app = app.checkbox(key="m33-confirm-preparation").set_value(True).run()
    assert not app.button(key="m33-prepare-proposal").disabled
    app = app.button(key="m33-prepare-proposal").click().run()

    assert not app.exception
    visible = _visible(app)
    assert "ready_for_publication" in visible
    assert "Propuesta inmutable preparada, no publicada" in visible
    assert "External writes performed false" in visible
    assert "Publicación y activación no disponibles" in visible
    assert "publication_prepared" in visible
    assert not app.code
    assert app.download_button(key="m33-download-proposal")
    assert app.download_button(key="m33-download-proposal").label == (
        "Descargar handoff JSON no ejecutable"
    )
    assert all("publicar" not in button.label.lower() for button in app.button)
    assert all("ejecutar" not in button.label.lower() for button in app.button)


def test_invalid_scenario_configuration_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCHEMABRIDGE_M33_SCENARIO_TOKEN", "../../invalid")

    app = AppTest.from_file(str(_app_path()), default_timeout=20).run()

    assert not app.exception
    visible = _visible(app)
    assert "m33_scenario_configuration_invalid" in visible
    assert "postgresql://" not in visible
    assert "Traceback" not in visible
    assert not app.button
    assert not app.download_button
