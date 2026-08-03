"""Browser-visible M35 Phase-A join change through the M34 publication handoff."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

pytestmark = pytest.mark.acceptance


def _app_path() -> Path:
    return Path(__file__).parents[2] / "scripts/m35_registry_join_change_scenario_app.py"


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
    monkeypatch.setenv("SCHEMABRIDGE_M35_SCENARIO_TOKEN", token)


def _assert_zero_exposure(app: AppTest) -> None:
    visible = _visible(app)
    assert "SQL generado 0" in visible
    assert "Filas expuestas 0" in visible
    assert "Credenciales expuestas 0" in visible
    assert "Source writes 0" in visible
    assert "postgresql://" not in visible
    assert "synthetic-m35-writer-token" not in visible
    assert "m35-profile-capability" not in visible
    assert not app.code
    assert not app.download_button


def test_join_change_profile_review_publication_and_readback_are_exactly_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(tmp_path, monkeypatch)
    app = AppTest.from_file(str(_app_path()), default_timeout=20).run()

    assert not app.exception
    visible = _visible(app)
    assert "not_requested" in visible
    assert "Active registry pointer v2 · generation 4" in visible
    assert "External writes 0" in visible
    assert "self_join · rejected_before_profile · mutaciones externas 0" in visible
    assert "cross_connection · rejected_before_profile · mutaciones externas 0" in visible
    assert "stale_target · registry_change_stale_target · mutaciones externas 0" in visible
    assert "many_to_many · approval_rejected · mutaciones externas 0" in visible
    _assert_zero_exposure(app)

    app = app.button(key="m35-request-profile").click().run()

    assert not app.exception
    visible = _visible(app)
    assert "profile_request_bound" in visible
    assert "Solicitud durable antes de enqueue = true" in visible
    assert "requested · El worker de perfil todavía no ha leído" in visible
    assert "profile_requested → profile_job_bound" in visible
    assert "External writes 0" in visible
    _assert_zero_exposure(app)

    app = app.button(key="m35-complete-profile").click().run()

    assert not app.exception
    visible = _visible(app)
    assert "completed · Evidencia exclusivamente agregada y vigente" in visible
    assert "Source reads 1" in visible
    assert "Transaction read-only true" in visible
    assert "Statement timeout 2000 ms" in visible
    assert "No se retienen claves, valores, filas, SQL, parámetros, DSN ni muestras" in visible
    _assert_zero_exposure(app)

    app = app.button(key="m35-finalize").click().run()

    assert not app.exception
    visible = _visible(app)
    assert "needs_review" in visible
    assert "many_to_one" in visible
    assert "La evidencia no aprueba nada" in visible
    assert app.button(key="m35-approve").disabled
    assert "draft_finalized" in visible
    _assert_zero_exposure(app)

    app = app.checkbox(key="m35-confirm-steward").set_value(True).run()
    assert not app.button(key="m35-approve").disabled
    app = app.button(key="m35-approve").click().run()

    assert not app.exception
    visible = _visible(app)
    assert "approved · Decisión append-only" in visible
    assert "m35-steward-reviewer" in visible
    assert "La aprobación semántica aún no publica" in visible
    assert "decision_recorded" in visible
    _assert_zero_exposure(app)

    app = app.button(key="m35-prepare-change").click().run()

    assert not app.exception
    visible = _visible(app)
    assert "ready_for_publication" in visible
    assert "m35-change-publisher" in visible
    assert "external_writes_performed=false" in visible
    assert "publication_prepared" in visible
    _assert_zero_exposure(app)

    app = app.button(key="m35-submit-publication").click().run()

    assert not app.exception
    visible = _visible(app)
    assert "Publication status queued" in visible
    assert "Target reservado; external writes = 0" in visible
    assert "Active registry pointer v2 · generation 4" in visible
    _assert_zero_exposure(app)

    app = app.button(key="m35-run-publication-prepare").click().run()

    assert not app.exception
    visible = _visible(app)
    assert "Publication status awaiting_approval" in visible
    assert "Candidato v3 completo" in visible
    assert "joins 1" in visible
    assert "external writes = 0" in visible
    assert app.button(key="m35-authorize-publication").disabled
    _assert_zero_exposure(app)

    app = app.checkbox(key="m35-confirm-publication").set_value(True).run()
    assert not app.button(key="m35-authorize-publication").disabled
    app = app.button(key="m35-authorize-publication").click().run()

    assert not app.exception
    visible = _visible(app)
    assert "Publication status approved" in visible
    assert "m35-publication-authorizer" in visible
    assert "external writes = 0" in visible
    _assert_zero_exposure(app)

    # Re-open as a new browser session. The durable synthetic aggregates live in the cached
    # runtime, not in a removed confirmation widget or client-controlled session value.
    app = AppTest.from_file(str(_app_path()), default_timeout=20).run()
    app = app.button(key="m35-run-publication-publish").click().run()

    assert not app.exception
    visible = _visible(app)
    assert "Publication status activation_ready" in visible
    assert "Documento v3 inmutable publicado y leído de vuelta exactamente" in visible
    assert "queued → leased → awaiting_approval → approved → leased → activation_ready" in visible
    assert "External writes 1" in visible
    assert "Active registry pointer v2 · generation 4" in visible
    assert "No automatic activation" in visible
    assert "permanece en v2 / generation 4" in visible
    assert "SQL generado 0" in visible
    assert "Filas expuestas 0" in visible
    assert "Credenciales expuestas 0" in visible
    assert "Source writes 0" in visible
    assert "postgresql://" not in visible
    assert "synthetic-m35-writer-token" not in visible
    assert not app.code
    assert not app.download_button


def test_invalid_m35_scenario_configuration_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCHEMABRIDGE_M35_SCENARIO_TOKEN", "../../invalid")

    app = AppTest.from_file(str(_app_path()), default_timeout=20).run()

    assert not app.exception
    visible = _visible(app)
    assert "m35_scenario_configuration_invalid" in visible
    assert "Traceback" not in visible
    assert "postgresql://" not in visible
    assert not app.button


def test_m35_scenario_has_mobile_overflow_and_accessibility_guards() -> None:
    source = _app_path().read_text(encoding="utf-8")

    assert "overflow-x: hidden" in source
    assert "overflow-wrap: anywhere" in source
    assert "@media (max-width: 640px)" in source
    assert "flex: 1 1 100% !important" in source
    assert "min-height: 44px" in source
