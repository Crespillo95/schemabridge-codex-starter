from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from scripts import m26_browser_acceptance_runtime as runtime


def _state() -> runtime.BrowserAcceptanceState:
    return runtime.BrowserAcceptanceState(
        database="schemabridge_m26_browser_012345abcdef",
        local_workspace="m26-browser-012345abcdef",
        workspace_id="sb_workspace_v1_" + ("a" * 64),
        created_at="2026-07-24T12:00:00+00:00",
        status_counts={
            "current": 1,
            "review_required": 1,
            "blocked": 3,
            "revalidated": 2,
        },
        report_ids={
            "current": "report_" + ("1" * 64),
            "review_required": "report_" + ("2" * 64),
            "blocked": "report_" + ("3" * 64),
            "revalidated": "report_" + ("4" * 64),
        },
        maximum_finding_count=31,
        maximum_impact_count=38,
    )


def test_state_is_bounded_round_trippable_and_contains_no_runtime_secret() -> None:
    state = _state()

    restored = runtime.BrowserAcceptanceState.from_json(state.to_json())
    summary = runtime._safe_summary(restored)

    assert restored == state
    assert json.loads(summary)["status_counts"]["revalidated"] == 2
    for forbidden in ("bearer", "cursor.key", "password", "openai"):
        assert forbidden not in summary.casefold()


def test_owner_only_files_reject_broader_permissions(tmp_path: Path) -> None:
    secret = tmp_path / "api.bearer"
    runtime._write_owner_only(secret, b"bounded-secret\n")

    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert runtime._read_owner_only(secret, maximum=1024) == b"bounded-secret\n"

    secret.chmod(0o640)
    with pytest.raises(runtime.BrowserAcceptanceSetupError):
        runtime._read_owner_only(secret, maximum=1024)


def test_acceptance_seed_environment_removes_unrelated_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-propagate")
    monkeypatch.setenv("DATAHUB_GMS_TOKEN", "must-not-propagate")
    monkeypatch.setenv(
        "SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL",
        "must-not-propagate",
    )
    monkeypatch.setenv("UNRELATED_SAFE_SETTING", "retained")

    environment = runtime._acceptance_environment(
        "schemabridge_m26_browser_012345abcdef",
        "m26-browser-012345abcdef",
    )

    assert "OPENAI_API_KEY" not in environment
    assert "DATAHUB_GMS_TOKEN" not in environment
    assert "SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL" not in environment
    assert environment["UNRELATED_SAFE_SETTING"] == "retained"
    assert environment["SCHEMABRIDGE_TEST_M26_RETAIN_DATABASE"] == "1"
    assert environment["SCHEMABRIDGE_TEST_M26_BROWSER_SEED"] == "1"


def test_state_directory_is_confined_to_repository_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    local = tmp_path / ".local"
    local.mkdir()

    assert runtime._resolve_state_dir(local / "m26-browser") == (local / "m26-browser").resolve()
    with pytest.raises(runtime.BrowserAcceptanceSetupError):
        runtime._resolve_state_dir(tmp_path / "outside")


def test_cleanup_requires_exact_confirmation_before_reading_state(
    tmp_path: Path,
) -> None:
    with pytest.raises(runtime.BrowserAcceptanceSetupError, match="confirmation"):
        runtime._cleanup(tmp_path, "DROP SOMETHING ELSE")
