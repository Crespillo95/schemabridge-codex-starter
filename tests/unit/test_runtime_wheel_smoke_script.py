from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from scripts.smoke_runtime_wheel import (
    EXPECTED_CONTROL_PLANE_MIGRATIONS,
    EXPECTED_RUNTIME_ENTRYPOINTS,
    HELP_CAPABLE_RUNTIME_COMMANDS,
    SMOKE_PROGRAM,
    _require_exact_contract,
)


def test_runtime_wheel_smoke_uses_isolated_installed_package() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / "scripts" / "smoke_runtime_wheel.py").read_text(encoding="utf-8")

    assert "TemporaryDirectory" in source
    assert "venv.EnvBuilder" in source
    assert '"-I", "-c", SMOKE_PROGRAM' in source
    assert "[api,postgres,sql]" in source
    assert 'assert "site-packages" in str(path)' in source
    assert "known_migrations()" in source
    assert "build_control_plane_migrator" in source
    assert "composed.migrations_path == path" in source
    assert 'distribution("schemabridge").entry_points' in source
    assert "entry_point.load()" in source
    assert "installed_commands" in source
    assert '"--help"' in source
    assert "timeout=30" in source


def test_runtime_wheel_contract_matches_every_current_packaged_asset() -> None:
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = project["project"]["scripts"]

    assert EXPECTED_CONTROL_PLANE_MIGRATIONS == (
        (1, "initial_control_plane"),
        (2, "authenticated_api_jobs"),
        (3, "reject_expired_job_success"),
        (4, "dynamic_catalog_inventory"),
        (5, "semantic_change_management"),
        (6, "dynamic_query_studio"),
        (7, "harden_ai_usage_settlement"),
        (8, "serialize_ai_provider_accounting"),
        (9, "tenant_connector_routing"),
        (10, "operational_observer"),
        (11, "connector_secret_versions"),
    )
    migration_files = sorted((root / "migrations" / "control_plane").glob("*.sql"))
    assert (
        tuple((int(path.name[:4]), path.stem[5:]) for path in migration_files)
        == EXPECTED_CONTROL_PLANE_MIGRATIONS
    )
    assert dict(EXPECTED_RUNTIME_ENTRYPOINTS) == scripts
    assert tuple(name for name, _target in EXPECTED_RUNTIME_ENTRYPOINTS) == tuple(scripts)
    assert ("schemabridge-observer", "schemabridge.entrypoints.observer.main:main") in (
        EXPECTED_RUNTIME_ENTRYPOINTS
    )
    assert HELP_CAPABLE_RUNTIME_COMMANDS == (
        "schemabridge",
        "schemabridge-ai-policy",
        "schemabridge-catalog",
        "schemabridge-connector-route",
        "schemabridge-semantic-change",
        "schemabridge-semantic-profile-worker",
        "schemabridge-semantic-reconciler",
        "schemabridge-worker",
    )
    assert set(HELP_CAPABLE_RUNTIME_COMMANDS) < set(scripts)
    assert repr(EXPECTED_CONTROL_PLANE_MIGRATIONS) in SMOKE_PROGRAM
    assert repr(EXPECTED_RUNTIME_ENTRYPOINTS) in SMOKE_PROGRAM


@pytest.mark.parametrize(
    ("label", "expected", "actual"),
    (
        (
            "migration deletion",
            EXPECTED_CONTROL_PLANE_MIGRATIONS,
            EXPECTED_CONTROL_PLANE_MIGRATIONS[:-1],
        ),
        (
            "migration rename",
            EXPECTED_CONTROL_PLANE_MIGRATIONS,
            (
                *EXPECTED_CONTROL_PLANE_MIGRATIONS[:-1],
                (11, "connector_secrets"),
            ),
        ),
        (
            "migration addition",
            EXPECTED_CONTROL_PLANE_MIGRATIONS,
            (*EXPECTED_CONTROL_PLANE_MIGRATIONS, (12, "unexpected")),
        ),
        (
            "entrypoint deletion",
            EXPECTED_RUNTIME_ENTRYPOINTS,
            tuple(
                item for item in EXPECTED_RUNTIME_ENTRYPOINTS if item[0] != "schemabridge-observer"
            ),
        ),
        (
            "entrypoint target",
            EXPECTED_RUNTIME_ENTRYPOINTS,
            tuple(
                (
                    name,
                    "schemabridge.entrypoints.http.main:main"
                    if name == "schemabridge-observer"
                    else target,
                )
                for name, target in EXPECTED_RUNTIME_ENTRYPOINTS
            ),
        ),
        (
            "entrypoint addition",
            EXPECTED_RUNTIME_ENTRYPOINTS,
            (
                *EXPECTED_RUNTIME_ENTRYPOINTS,
                ("schemabridge-unexpected", "schemabridge.entrypoints.cli.main:app"),
            ),
        ),
    ),
)
def test_runtime_wheel_exact_contract_rejects_mutations(
    label: str,
    expected: tuple[object, ...],
    actual: tuple[object, ...],
) -> None:
    with pytest.raises(RuntimeError, match=f"{label} contract mismatch"):
        _require_exact_contract(label=label, actual=actual, expected=expected)


def test_make_exposes_strict_m26_process_and_probe_targets() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / "Makefile").read_text(encoding="utf-8")

    for target in (
        "semantic-reconciler:",
        "semantic-reconciler-once:",
        "semantic-reconciler-probe:",
        "semantic-profile-worker:",
        "semantic-profile-worker-once:",
        "semantic-profile-worker-probe:",
    ):
        assert f"\n{target}\n" in source
    assert "RECONCILER_CLEAN_ENV := env" in source
    assert "PROFILE_WORKER_CLEAN_ENV := env" in source
    for forbidden in (
        "-u OPENAI_API_KEY",
        "-u SCHEMABRIDGE_LLM_MODEL",
        "-u SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN",
        "-u SCHEMABRIDGE_INVENTORY_CURSOR_SIGNING_KEY",
    ):
        assert forbidden in source


def test_env_example_documents_m28_dynamic_profile_limits_without_a_secret_value() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / ".env.example").read_text(encoding="utf-8")

    assert "SCHEMABRIDGE_SEMANTIC_RECONCILER_LEASE_SECONDS=60" in source
    assert "SCHEMABRIDGE_SEMANTIC_RECONCILER_POLL_INTERVAL_MS=1000" in source
    assert "SCHEMABRIDGE_SEMANTIC_PROFILE_MAX_ATTEMPTS=5" in source
    assert "SCHEMABRIDGE_SEMANTIC_PROFILE_RETENTION_DAYS=30" in source
    assert "SCHEMABRIDGE_SEMANTIC_PROFILE_SOURCE_WORKSPACE_ID=" not in source
    assert "SCHEMABRIDGE_SEMANTIC_PROFILE_SOURCE_CONNECTION_ID=" not in source
    assert "# SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY=" in source
    assert "require the remote exact-version resolver and projected workload identity" in source
    assert "# SCHEMABRIDGE_CONNECTOR_SECRET_CAPABILITY=execution" in source
    assert "# SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_ROLE=" in source
    assert "# SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_BINDING_REF=" in source
    assert "# SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_VERSION=17" in source
    assert "# SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY=" in source


def test_env_example_documents_query_studio_ai_as_disabled_and_keyless() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / ".env.example").read_text(encoding="utf-8")

    assert "\n# OPENAI_API_KEY=\n" in source
    assert "\nSCHEMABRIDGE_QUERY_STUDIO_AI_MODE=disabled\n" in source
    assert "\nSCHEMABRIDGE_QUERY_STUDIO_AI_MODEL=gpt-5-nano-2025-08-07\n" in source
    assert "\nSCHEMABRIDGE_QUERY_STUDIO_AI_REGION=global\n" in source
    assert "\n# SCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY=\n" in source
