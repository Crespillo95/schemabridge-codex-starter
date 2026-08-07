from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = _ROOT / "migrations" / "control_plane" / "0011_connector_secret_versions.sql"


def _sql() -> str:
    return _MIGRATION.read_text(encoding="utf-8")


def test_v11_persists_immutable_provider_versions_without_inferred_backfill() -> None:
    sql = _sql()

    assert "CREATE TABLE schemabridge_control.connector_private_route_secret_versions" in sql
    assert "provider_secret_version bigint NOT NULL" in sql
    assert "connector_private_route_secret_versions_immutable" in sql
    assert "BEFORE UPDATE OR DELETE" in sql
    migration_prefix = sql.split(
        "CREATE FUNCTION schemabridge_control.apply_connector_route_change_v2",
        maxsplit=1,
    )[0]
    assert "INSERT INTO" not in migration_prefix
    assert "provider_secret_version = route_revision" not in sql
    assert "provider_secret_version = p_route_revision" not in sql


def test_v11_closes_old_writes_and_exposes_only_version_bearing_runtime_loaders() -> None:
    sql = _sql()

    assert "DEFERRABLE INITIALLY DEFERRED" in sql
    assert "connector private route secret version is missing" in sql
    for function_name in (
        "apply_connector_route_change_v2",
        "load_current_preflight_connector_route_v2",
        "load_owned_catalog_connector_route_v2",
        "load_owned_execution_connector_route_v2",
        "load_owned_profile_connector_route_v2",
    ):
        assert f"CREATE FUNCTION schemabridge_control.{function_name}(" in sql
    assert "FROM schemabridge_runtime;" in sql
    assert "FROM schemabridge_catalog;" in sql
    assert "FROM schemabridge_worker;" in sql
    assert sql.count("secret_version.provider_secret_version") >= 5


def test_v11_route_apply_requires_four_explicit_versions_and_exact_replay() -> None:
    sql = _sql()
    apply_v2 = sql.split(
        "CREATE FUNCTION schemabridge_control.apply_connector_route_change_v2",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.load_current_preflight_connector_route_v2",
        maxsplit=1,
    )[0]

    for capability in ("preflight", "catalog", "execution", "profile"):
        assert f"p_{capability}_secret_version IS NULL" in apply_v2
        assert f"p_{capability}_secret_version NOT BETWEEN 1 AND 9223372036854775807" in (apply_v2)
        assert f"secret_version.capability = '{capability}'" in apply_v2
        assert f"= p_{capability}_secret_version" in apply_v2
    assert "ON CONFLICT DO NOTHING" in apply_v2
    assert "IF exact_version_count <> 4 THEN" in apply_v2
    assert "connector provider secret version conflict" in apply_v2
