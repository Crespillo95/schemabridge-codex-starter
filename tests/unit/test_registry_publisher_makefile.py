"""Static isolation contract for the local M34 publisher recipes."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"


def _publisher_clean_environment(makefile: str) -> str:
    publisher_boundary = makefile.split("PUBLISHER_CREDENTIAL_CLEAN_ENV :=", 1)[1].split(
        "OPERATOR_CLEAN_ENV :=", 1
    )[0]
    operator = makefile.split("OPERATOR_CLEAN_ENV :=", 1)[1].split("API_CLEAN_ENV :=", 1)[0]
    publisher = makefile.split("PUBLISHER_CLEAN_ENV :=", 1)[1].split("WORKER_CLEAN_ENV :=", 1)[0]
    return publisher_boundary + operator + publisher


def _recipe(makefile: str, target: str, next_target: str) -> str:
    return makefile.split(f"\n{target}:\n", 1)[1].split(f"\n{next_target}:\n", 1)[0]


def test_publisher_clean_environment_drops_every_cross_component_trust_domain() -> None:
    clean = _publisher_clean_environment(MAKEFILE.read_text(encoding="utf-8"))

    forbidden_inherited = {
        "DATABASE_URL",
        "POSTGRES_READER_USER",
        "OPENAI_API_KEY",
        "DATAHUB_GMS_URL",
        "DATAHUB_GMS_TOKEN",
        "SCHEMABRIDGE_CONTROL_DATABASE_URL",
        "SCHEMABRIDGE_CONTROL_API_DATABASE_URL",
        "SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL",
        "SCHEMABRIDGE_CONTROL_PUBLISHER_DATABASE_URL",
        "SCHEMABRIDGE_CONTROL_RECONCILER_DATABASE_URL",
        "SCHEMABRIDGE_CONNECTOR_SECRET_ROLE",
        "SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_ROLE",
        "SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_BINDING_REF",
        "SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_VERSION",
        "SCHEMABRIDGE_SEMANTIC_REGISTRY_READER_ENV_PATH",
        "SCHEMABRIDGE_REGISTRY_PUBLISHER_SECRET_ROLE",
        "SCHEMABRIDGE_REGISTRY_PUBLISHER_SECRET_BINDING_REF",
        "SCHEMABRIDGE_REGISTRY_PUBLISHER_SECRET_VERSION",
        "SCHEMABRIDGE_REGISTRY_PUBLISHER_WRITER_ENV_PATH",
        "SCHEMABRIDGE_SEMANTIC_PROFILE_SOURCE_WORKSPACE_ID",
        "SCHEMABRIDGE_SEMANTIC_PROFILE_SOURCE_CONNECTION_ID",
        "SCHEMABRIDGE_OIDC_ISSUER",
        "SCHEMABRIDGE_OIDC_ROLE_CLAIM",
        "SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN",
        "SCHEMABRIDGE_API_OIDC_JWKS_URL",
        "SCHEMABRIDGE_LLM_MODEL",
        "SCHEMABRIDGE_QUERY_STUDIO_AI_MODE",
        "SCHEMABRIDGE_QUERY_STUDIO_AI_MODEL",
        "SCHEMABRIDGE_QUERY_STUDIO_AI_REGION",
    }
    assert all(f"-u {variable}" in clean for variable in forbidden_inherited)


def test_publisher_recipes_inject_only_its_dsn_and_local_writer_document() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")
    recipes = (
        _recipe(makefile, "registry-publisher", "registry-publisher-once"),
        _recipe(makefile, "registry-publisher-once", "registry-publisher-probe"),
        _recipe(makefile, "registry-publisher-probe", "catalog"),
    )

    for recipe in recipes:
        assert "$(PUBLISHER_CLEAN_ENV)" in recipe
        assert "SCHEMABRIDGE_COMPONENT=publisher" in recipe
        assert "SCHEMABRIDGE_CONTROL_PUBLISHER_DATABASE_URL=" in recipe
        assert "SCHEMABRIDGE_REGISTRY_PUBLISHER_WRITER_ENV_PATH=" in recipe
        assert ".local/datahub/registry-publisher.env" in recipe
        assert ".local/datahub/writer.env" not in recipe
        assert "OPENAI" not in recipe
        assert "OIDC_" not in recipe
        assert "SEMANTIC_REGISTRY_READER" not in recipe
        assert "DATABASE_URL='$(DEMO_DATABASE_URL)'" not in recipe


def test_every_other_local_runtime_drops_publisher_credentials() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")
    assert "WEB_CLEAN_ENV := env $(PUBLISHER_CREDENTIAL_CLEAN_ENV)" in makefile
    assert "@$(WEB_CLEAN_ENV) DATABASE_URL='$(DEMO_DATABASE_URL)'" in makefile
    blocks = (
        ("OPERATOR_CLEAN_ENV :=", "API_CLEAN_ENV :="),
        ("API_CLEAN_ENV :=", "PUBLISHER_CLEAN_ENV :="),
        ("WORKER_CLEAN_ENV :=", "CATALOG_CLEAN_ENV :="),
        ("CATALOG_CLEAN_ENV :=", "RECONCILER_CLEAN_ENV :="),
        ("RECONCILER_CLEAN_ENV :=", "PROFILE_WORKER_CLEAN_ENV :="),
        ("PROFILE_WORKER_CLEAN_ENV :=", "OBSERVER_CLEAN_ENV :="),
        ("OBSERVER_CLEAN_ENV :=", "JUDGE_IMAGE ?="),
    )

    for start, end in blocks:
        clean = makefile.split(start, 1)[1].split(end, 1)[0]
        assert "$(PUBLISHER_CREDENTIAL_CLEAN_ENV)" in clean
