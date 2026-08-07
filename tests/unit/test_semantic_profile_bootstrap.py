"""Composition tests for the isolated aggregate semantic profile worker."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

import schemabridge.bootstrap as bootstrap_module
from schemabridge.adapters.connectors.local_secrets import (
    OwnerOnlyConnectorSecretResolver,
)
from schemabridge.adapters.connectors.postgres_profile_routing import (
    PostgresSemanticProfileConnectorRouteReader,
)
from schemabridge.adapters.postgres.routed_relationships import (
    RoutedSemanticJoinProfileEvidenceFactory,
)
from schemabridge.adapters.semantic_change.postgres_profile_queue import (
    PostgresSemanticJoinProfileQueue,
)
from schemabridge.application.postgres_health import DatabaseConfigurationError
from schemabridge.application.semantic_profile_worker import RunOneSemanticJoinProfile
from schemabridge.bootstrap import build_semantic_profile_worker
from schemabridge.config import Settings

WORKER_DSN = "postgresql://schemabridge_worker:worker-secret@control.example.test/control"
SECRET_DIRECTORY = (Path(__file__).resolve().parent / "connector-secrets").resolve()


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        OPENAI_API_KEY=None,
        DATAHUB_GMS_TOKEN=None,
        SCHEMABRIDGE_COMPONENT="worker",
        SCHEMABRIDGE_CONTROL_PLANE_MODE="postgres",
        SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL=WORKER_DSN,
        SCHEMABRIDGE_REGISTRY_MODE="live",
        SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION="active",
        SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS=True,
        SCHEMABRIDGE_SEMANTIC_PROFILE_RETENTION_DAYS=17,
        SCHEMABRIDGE_SEMANTIC_PROFILE_MAINTENANCE_BATCH_SIZE=23,
        SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY=SECRET_DIRECTORY,
    )


def test_profile_worker_uses_only_worker_control_and_dynamic_connector_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks: list[str] = []
    monkeypatch.setattr(
        bootstrap_module,
        "require_current_control_plane_schema",
        lambda **kwargs: checks.append(cast(str, kwargs["credential_kind"])),
    )

    worker = build_semantic_profile_worker(settings=_settings())

    assert isinstance(worker, RunOneSemanticJoinProfile)
    assert checks == ["worker"]
    assert isinstance(worker.queue, PostgresSemanticJoinProfileQueue)
    assert worker.queue.application_name == "schemabridge-control-worker"
    assert isinstance(worker.evidence_factory, RoutedSemanticJoinProfileEvidenceFactory)
    assert isinstance(
        worker.evidence_factory.route_reader,
        PostgresSemanticProfileConnectorRouteReader,
    )
    assert isinstance(
        worker.evidence_factory.secret_resolver,
        OwnerOnlyConnectorSecretResolver,
    )
    assert not hasattr(worker, "expected_source_workspace_id")
    assert not hasattr(worker, "expected_source_connection_id")
    assert worker.retention.days == 17
    assert worker.maintenance_batch_size == 23
    representation = repr(worker)
    assert "worker-secret" not in representation
    assert str(SECRET_DIRECTORY) not in representation


def test_profile_worker_rejects_non_worker_component() -> None:
    settings = _settings().model_copy(update={"runtime_component": "web"})

    with pytest.raises(DatabaseConfigurationError, match="SCHEMABRIDGE_COMPONENT=worker"):
        build_semantic_profile_worker(settings=settings)


def test_profile_worker_does_not_require_a_fixed_source_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bootstrap_module,
        "require_current_control_plane_schema",
        lambda **_kwargs: None,
    )
    settings = _settings().model_copy(
        update={
            "semantic_profile_source_connection_id": None,
            "semantic_profile_source_workspace_id": None,
        }
    )

    worker = build_semantic_profile_worker(settings=settings)

    assert isinstance(worker, RunOneSemanticJoinProfile)


def test_profile_worker_requires_the_private_connector_secret_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bootstrap_module,
        "require_current_control_plane_schema",
        lambda **_kwargs: None,
    )
    settings = _settings().model_copy(update={"connector_secret_directory": None})

    with pytest.raises(
        DatabaseConfigurationError,
        match="SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY",
    ):
        build_semantic_profile_worker(settings=settings)
