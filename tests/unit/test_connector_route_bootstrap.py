from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

import schemabridge.bootstrap as bootstrap_module
from schemabridge.adapters.connectors.postgres_route_operator import (
    PostgresConnectorRouteOperator,
)
from schemabridge.application.connector_route_operator import ConnectorRouteOperator
from schemabridge.application.postgres_health import DatabaseConfigurationError
from schemabridge.bootstrap import build_connector_route_operator
from schemabridge.config import Settings

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DSN = "postgresql://runtime:runtime-secret@control.example.test/control"
MIGRATOR_DSN = "postgresql://migrator:migrator-secret@control.example.test/control"


@dataclass
class _MigrationCheck:
    calls: list[str]

    def require_current(self) -> None:
        self.calls.append("require_current")


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
            "SCHEMABRIDGE_CONTROL_DATABASE_URL": RUNTIME_DSN,
            "SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL": MIGRATOR_DSN,
            "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY": (
                "route-bootstrap-audit-signing-key-with-diversity"
            ),
            "SCHEMABRIDGE_IDENTITY_MIGRATION_KEY": (
                "route-bootstrap-identity-migration-key-with-diversity"
            ),
        }
    )


def test_bootstrap_composes_only_migrator_route_operator_after_schema_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _build_migrator(**kwargs: object) -> _MigrationCheck:
        assert kwargs["credential_kind"] == "migrator"
        return _MigrationCheck(calls)

    monkeypatch.setattr(
        bootstrap_module,
        "build_control_plane_migrator",
        _build_migrator,
    )

    operator = build_connector_route_operator(
        repository_root=ROOT,
        settings=_settings(),
    )

    assert isinstance(operator, ConnectorRouteOperator)
    assert isinstance(operator.store, PostgresConnectorRouteOperator)
    assert calls == ["require_current"]
    rendered = repr(operator)
    assert RUNTIME_DSN not in rendered
    assert MIGRATOR_DSN not in rendered


def test_bootstrap_rejects_route_operator_without_postgres_control_plane() -> None:
    with pytest.raises(DatabaseConfigurationError, match="PostgreSQL control plane"):
        build_connector_route_operator(
            repository_root=ROOT,
            settings=Settings.model_validate({}),
        )
