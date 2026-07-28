from __future__ import annotations

import tomllib
from pathlib import Path


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
    assert "(1, 2, 3, 4, 5, 6, 7, 8, 9)" in source
    assert '"dynamic_catalog_inventory"' in source
    assert '"semantic_change_management"' in source
    assert '"dynamic_query_studio"' in source
    assert '"harden_ai_usage_settlement"' in source
    assert '"serialize_ai_provider_accounting"' in source
    assert '"tenant_connector_routing"' in source
    for command in (
        "schemabridge-ai-policy",
        "schemabridge-semantic-change",
        "schemabridge-semantic-reconciler",
        "schemabridge-semantic-profile-worker",
        "schemabridge-connector-route",
    ):
        assert command in source
    assert '"--help"' in source
    assert "timeout=30" in source


def test_runtime_wheel_exports_independent_m26_process_commands() -> None:
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = project["project"]["scripts"]

    assert scripts["schemabridge-semantic-reconciler"] == (
        "schemabridge.entrypoints.semantic_reconciler.main:main"
    )
    assert scripts["schemabridge-semantic-profile-worker"] == (
        "schemabridge.entrypoints.semantic_profile_worker.main:main"
    )
    assert scripts["schemabridge-semantic-change"] == (
        "schemabridge.entrypoints.semantic_change.main:main"
    )
    assert scripts["schemabridge-ai-policy"] == ("schemabridge.entrypoints.ai_policy.main:main")


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
    assert "Never share one directory across capabilities" in source
    assert "# SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY=" in source


def test_env_example_documents_query_studio_ai_as_disabled_and_keyless() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / ".env.example").read_text(encoding="utf-8")

    assert "\nOPENAI_API_KEY=\n" in source
    assert "\nSCHEMABRIDGE_QUERY_STUDIO_AI_MODE=disabled\n" in source
    assert "\nSCHEMABRIDGE_QUERY_STUDIO_AI_MODEL=gpt-5-nano-2025-08-07\n" in source
    assert "\nSCHEMABRIDGE_QUERY_STUDIO_AI_REGION=global\n" in source
    assert "\nSCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY=\n" in source
