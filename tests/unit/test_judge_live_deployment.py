"""Static safety contract for the public live-PostgreSQL judge topology."""

from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "docker-compose.judge-live.yml"
READER_INIT_PATH = ROOT / "deploy/judge-live/030_read_only_user.sh"


def _compose() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8")))


def test_live_judge_source_is_internal_and_unpublished() -> None:
    compose = _compose()
    postgres = compose["services"]["postgres"]
    backend = compose["networks"]["judge_backend"]

    assert "ports" not in postgres
    assert postgres["networks"] == ["judge_backend"]
    assert backend == {"internal": True}
    assert postgres["security_opt"] == ["no-new-privileges:true"]


def test_live_judge_app_is_read_only_and_cannot_publish() -> None:
    app = _compose()["services"]["app"]
    environment = app["environment"]

    assert environment["SCHEMABRIDGE_JUDGE_EXECUTION"] == "live"
    assert environment["SCHEMABRIDGE_CATALOG_MODE"] == "recorded"
    assert environment["SCHEMABRIDGE_REGISTRY_MODE"] == "recorded"
    assert environment["SCHEMABRIDGE_PUBLICATION_MODE"] == "disabled"
    assert environment["SCHEMABRIDGE_QUERY_STUDIO_AI_MODE"] == "fake"
    assert environment["POSTGRES_READER_USER"] == "schemabridge_reader"
    assert "schemabridge_reader:" in environment["DATABASE_URL"]
    assert "postgres:5432/schemabridge" in environment["DATABASE_URL"]
    assert app["read_only"] is True
    assert app["cap_drop"] == ["ALL"]
    assert app["security_opt"] == ["no-new-privileges:true"]
    assert app["ports"] == ["127.0.0.1:${SCHEMABRIDGE_JUDGE_PORT:-7860}:7860"]


def test_live_judge_reader_initializer_has_no_static_password_or_write_grant() -> None:
    source = READER_INIT_PATH.read_text(encoding="utf-8")

    assert READER_INIT_PATH.stat().st_mode & 0o111
    assert "PASSWORD :'reader_password'" in source
    assert "default_transaction_read_only = on" in source
    assert "NOSUPERUSER" in source
    assert "NOCREATEDB" in source
    assert "NOCREATEROLE" in source
    assert "NOBYPASSRLS" in source
    assert "GRANT SELECT ON ALL TABLES" in source
    assert "GRANT INSERT" not in source
    assert "GRANT UPDATE" not in source
    assert "GRANT DELETE" not in source
    assert "schemabridge_reader'" not in source
