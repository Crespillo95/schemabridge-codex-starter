from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from scripts import m28_browser_acceptance_runtime as runtime
from scripts.m28_connector_scenario_app import (
    _build_acceptance_principal,
    _capabilities,
)
from tests.m28_browser_support import public_m28_state

from schemabridge.adapters.connectors.local_secrets import OpaqueConnectorSecretRef
from schemabridge.application.query_execution import QueryPreviewResult
from schemabridge.domain.identity import IdentityRole
from schemabridge.entrypoints.streamlit.connector_acceptance import (
    M28_BROWSER_SCENARIO_ENV,
    M28_BROWSER_STATE_FILE_ENV,
    M28BrowserScenario,
    is_m28_private_environment_key,
)


class _ExecveCalled(RuntimeError):
    pass


def _state_dir(root: Path) -> Path:
    local = root / ".local"
    local.mkdir()
    state_dir = local / "m28-browser"
    state_dir.mkdir(mode=0o700)
    runtime._write_owner_only(
        state_dir / runtime.STATE_FILE,
        public_m28_state().to_json(),
    )
    return state_dir


def test_owner_only_state_load_rejects_broader_mode_and_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    state_dir = _state_dir(tmp_path)
    state_path = state_dir / runtime.STATE_FILE

    loaded_path, loaded = runtime._load_state(state_dir)
    assert loaded_path == state_dir
    assert loaded == public_m28_state()
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600

    state_path.chmod(0o640)
    with pytest.raises(runtime.BrowserAcceptanceSetupError, match="owner-only"):
        runtime._load_state(state_dir)

    state_path.chmod(0o600)
    retained = state_dir / "retained.json"
    state_path.rename(retained)
    state_path.symlink_to(retained)
    with pytest.raises(runtime.BrowserAcceptanceSetupError):
        runtime._load_state(state_dir)


def test_state_directory_must_be_dedicated_and_confined_under_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    (tmp_path / ".local").mkdir()

    with pytest.raises(runtime.BrowserAcceptanceSetupError, match="dedicated"):
        runtime._resolve_state_dir(tmp_path / ".local")
    with pytest.raises(runtime.BrowserAcceptanceSetupError, match=r"under \.local"):
        runtime._resolve_state_dir(tmp_path / "outside")

    accepted = tmp_path / ".local/m28-browser"
    assert runtime._resolve_state_dir(accepted) == accepted


def test_streamlit_exec_uses_a_clean_allowlist_and_drops_future_secret_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    state_dir = _state_dir(tmp_path)
    for name in (
        "FUTURE_DATABASE_URL",
        "UNLISTED_PASSWORD",
        "UNLISTED_TOKEN",
        "UNLISTED_API_KEY",
        "UNLISTED_SECRET_DIR",
        "UNLISTED_DSN",
        "UNLISTED_CREDENTIAL",
    ):
        monkeypatch.setenv(name, f"private-{name}")
    captured: dict[str, object] = {}

    def capture_execve(
        executable: Path,
        arguments: tuple[str, ...],
        environment: dict[str, str],
    ) -> None:
        captured.update(
            executable=str(executable),
            arguments=arguments,
            environment=environment,
        )
        raise _ExecveCalled

    monkeypatch.setattr(runtime.os, "execve", capture_execve)
    monkeypatch.setattr(runtime.os, "chdir", lambda _path: None)

    with pytest.raises(_ExecveCalled):
        runtime._serve_streamlit(
            state_dir,
            scenario=M28BrowserScenario.TENANT_A_ACCEPTED,
            port=8510,
        )

    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert environment[M28_BROWSER_SCENARIO_ENV] == "tenant_a_accepted"
    assert environment[M28_BROWSER_STATE_FILE_ENV] == str(state_dir / runtime.STATE_FILE)
    assert not any(is_m28_private_environment_key(key) for key in environment)
    assert not any(value.startswith("private-") for value in environment.values())
    assert set(environment) <= {
        "PATH",
        "PYTHONUNBUFFERED",
        "LANG",
        "LC_ALL",
        "SCHEMABRIDGE_ENVIRONMENT",
        "SCHEMABRIDGE_AUTH_MODE",
        "SCHEMABRIDGE_RELEASE_REF",
        M28_BROWSER_STATE_FILE_ENV,
        M28_BROWSER_SCENARIO_ENV,
    }


def test_cleanup_requires_exact_confirmation_and_exact_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    state_dir = _state_dir(tmp_path)

    with pytest.raises(runtime.BrowserAcceptanceSetupError, match="exact"):
        runtime._cleanup(state_dir, "yes")
    assert state_dir.is_dir()

    unexpected = state_dir / "unexpected.txt"
    unexpected.write_text("retain", encoding="utf-8")
    with pytest.raises(runtime.BrowserAcceptanceSetupError, match="unexpected"):
        runtime._cleanup(state_dir, runtime.CLEANUP_CONFIRMATION)
    assert state_dir.is_dir()

    unexpected.unlink()
    runtime._cleanup(state_dir, runtime.CLEANUP_CONFIRMATION)
    assert not state_dir.exists()


def test_private_source_fixture_repr_and_safe_summary_hide_private_material() -> None:
    fixture = runtime._SourceFixture(
        label="tenant-a",
        database="private_database",
        reader="private_reader",
        dsn="postgresql://private:password@private-host/private_database",
        row_count=2,
        reference=OpaqueConnectorSecretRef("private.route.binding"),
    )
    fixture_repr = repr(fixture)
    summary = runtime._safe_summary(public_m28_state())

    for private in (
        "private_database",
        "private_reader",
        "postgresql://",
        "password",
        "private-host",
        "private.route.binding",
        "sb_m28_a_reader_deadbeef",
    ):
        assert private not in fixture_repr
        assert private not in summary
    assert set(json.loads(summary)) == {
        "evidence_fingerprint",
        "real_preflight_calls",
        "real_preview_calls",
        "scenario_decisions",
    }


@pytest.mark.parametrize(
    ("read_only", "truncated"),
    ((False, False), (True, True)),
)
def test_public_result_rejects_an_unbounded_or_writable_preview(
    read_only: bool,
    truncated: bool,
) -> None:
    preview = QueryPreviewResult(
        columns=("approved_rows",),
        rows=((2,),),
        database_user="sb_m28_a_reader_deadbeef",
        transaction_read_only=read_only,
        statement_timeout_ms=2_000,
        truncated=truncated,
    )

    with pytest.raises(runtime.BrowserAcceptanceSetupError, match="read-only bounds"):
        runtime._public_result(preview)


def test_parser_closes_scenarios_and_port_type() -> None:
    parsed = runtime._parser().parse_args(["streamlit"])
    assert parsed.scenario == M28BrowserScenario.TENANT_A_ACCEPTED.value
    assert parsed.port == 8510

    with pytest.raises(SystemExit):
        runtime._parser().parse_args(["streamlit", "--scenario", "arbitrary"])


def test_acceptance_capabilities_come_from_the_deny_by_default_role_matrix() -> None:
    analyst = _build_acceptance_principal()
    analyst_capabilities = _capabilities(analyst)
    auditor = analyst.model_copy(update={"roles": frozenset({IdentityRole.AUDITOR})})
    auditor_capabilities = _capabilities(auditor)

    assert analyst_capabilities.can_view
    assert analyst_capabilities.can_execute
    assert analyst_capabilities.can_view_result
    assert auditor_capabilities.can_view
    assert not auditor_capabilities.can_execute
    assert not auditor_capabilities.can_view_result
