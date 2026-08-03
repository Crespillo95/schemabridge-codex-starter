"""Regression contract for the honest cross-milestone commercial operator manual."""

from __future__ import annotations

from pathlib import Path

from schemabridge.domain.production_readiness import M30_REQUIRED_SOURCE_PATHS

ROOT = Path(__file__).resolve().parents[2]
COMMERCIAL_DOCS = (
    "README.md",
    "01_ROLES_RACI_SHARED_RESPONSIBILITY.md",
    "02_SETUP_DEPLOYMENT.md",
    "03_TENANT_ONBOARDING.md",
    "04_DAILY_USE_AND_UNSUPPORTED.md",
    "05_INCIDENT_RECOVERY.md",
    "06_OFFBOARDING_DECOMMISSION.md",
    "07_PILOT_SCORECARD.md",
)


def test_commercial_manual_is_complete_linked_and_explicitly_no_go() -> None:
    directory = ROOT / "docs" / "commercial"
    index = (directory / "README.md").read_text(encoding="utf-8")

    for filename in COMMERCIAL_DOCS:
        path = directory / filename
        assert path.is_file()
        assert path.stat().st_size >= 1_000
        if filename != "README.md":
            assert f"]({filename})" in index

    assert "NO-GO" in index
    assert "PostgreSQL" in index
    assert "tres tablas y dos joins" in index
    assert "no existe una consola comercial única" in index
    assert "m30-manifest-attestation" in index
    assert "24 controles materiales sin adjudicar" in " ".join(index.split())


def test_operator_procedures_retain_owner_evidence_stop_rollback_and_escalation() -> None:
    directory = ROOT / "docs" / "commercial"
    procedural = COMMERCIAL_DOCS[1:]
    for filename in procedural:
        normalized = (directory / filename).read_text(encoding="utf-8").casefold()
        assert "owner" in normalized or "responsable" in normalized
        assert "evidencia" in normalized
        assert "stop" in normalized or "parada" in normalized
        assert "rollback" in normalized
        assert "escalad" in normalized


def test_commercial_contract_is_bound_into_the_m30_candidate_identity() -> None:
    expected = {f"docs/commercial/{filename}" for filename in COMMERCIAL_DOCS}
    assert expected <= set(M30_REQUIRED_SOURCE_PATHS.values())
    assert {
        "docs/12_RUNBOOK.md",
        "docs/13_UI_SPEC.md",
        "docs/14_DEPLOYMENT.md",
        "docs/19_COMMERCIAL_USAGE.md",
        ".github/workflows/m30-manifest-attestation.yml",
    } <= set(M30_REQUIRED_SOURCE_PATHS.values())


def test_deployment_and_usage_docs_do_not_claim_missing_publisher_or_general_sql() -> None:
    deployment = (ROOT / "docs/14_DEPLOYMENT.md").read_text(encoding="utf-8")
    usage = (ROOT / "docs/19_COMMERCIAL_USAGE.md").read_text(encoding="utf-8")

    assert "no publisher queue" not in deployment.casefold()
    assert "copy-first" in usage
    assert "PostgreSQL" in usage
    assert "SQL arbitrario" in usage
    assert "(commercial/README.md)" in usage
