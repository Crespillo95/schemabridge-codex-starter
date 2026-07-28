"""Composition tests for the isolated M26 semantic reconciler."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

import schemabridge.bootstrap as bootstrap_module
from schemabridge.adapters.datahub.recipe_inventory import (
    DataHubQueryRecipeInventory,
)
from schemabridge.adapters.semantic_change.postgres_dependency_sources import (
    PostgresManagedWorkflowDependencySource,
    PostgresSemanticDependencyIndexSink,
)
from schemabridge.adapters.semantic_change.postgres_scans import (
    PostgresSemanticChangeScanStore,
)
from schemabridge.adapters.semantic_change.scan_runner import (
    InspectSemanticChangeScanRunner,
)
from schemabridge.application.postgres_health import DatabaseConfigurationError
from schemabridge.application.semantic_change_operator import (
    InspectLatestSemanticChange,
)
from schemabridge.application.semantic_change_reconciler import (
    RunOneSemanticChangeScan,
)
from schemabridge.application.semantic_dependency_reconciler import (
    ReconcileSemanticDependencies,
)
from schemabridge.bootstrap import (
    build_semantic_change_operator_runtime,
    build_semantic_dependency_reconciler,
    build_semantic_reconciler,
    build_semantic_reconciler_process_runtime,
)
from schemabridge.config import Settings

RECONCILER_DSN = (
    "postgresql://schemabridge_reconciler:reconciler-secret@control.example.test/control"
)


def _reader_env(tmp_path: Path) -> Path:
    path = tmp_path / "reader.env"
    path.write_text(
        "DATAHUB_GMS_URL=https://datahub.example.test\nDATAHUB_GMS_TOKEN=datahub-reader-secret\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        OPENAI_API_KEY=None,
        SCHEMABRIDGE_COMPONENT="reconciler",
        SCHEMABRIDGE_CONTROL_PLANE_MODE="postgres",
        SCHEMABRIDGE_CONTROL_RECONCILER_DATABASE_URL=RECONCILER_DSN,
        SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY=(
            "semantic-audit-key-with-32-distinctish-bytes-0123456789"
        ),
        SCHEMABRIDGE_REGISTRY_MODE="live",
        SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION="active",
        SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS=True,
        SCHEMABRIDGE_SEMANTIC_REGISTRY_READER_ENV_PATH=_reader_env(tmp_path),
        SCHEMABRIDGE_SEMANTIC_RECONCILER_RETENTION_DAYS=17,
        SCHEMABRIDGE_SEMANTIC_RECONCILER_MAINTENANCE_BATCH_SIZE=23,
        SCHEMABRIDGE_SEMANTIC_PROFILE_MAX_ATTEMPTS=7,
        SCHEMABRIDGE_LOCAL_ROLES=("steward",),
    )


def test_dependency_reconciler_uses_bounded_workflows_and_read_only_datahub(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks: list[str] = []
    monkeypatch.setattr(
        bootstrap_module,
        "require_current_control_plane_schema",
        lambda **kwargs: checks.append(cast(str, kwargs["credential_kind"])),
    )

    reconciler = build_semantic_dependency_reconciler(settings=_settings(tmp_path))

    assert isinstance(reconciler, ReconcileSemanticDependencies)
    assert checks == ["reconciler"]
    assert reconciler.page_size == 50
    assert isinstance(reconciler.workflows, PostgresManagedWorkflowDependencySource)
    assert isinstance(reconciler.recipes, DataHubQueryRecipeInventory)
    assert isinstance(reconciler.index, PostgresSemanticDependencyIndexSink)
    assert _settings(tmp_path).database_url is None
    representation = repr(reconciler)
    assert "reconciler-secret" not in representation
    assert "datahub-reader-secret" not in representation
    assert "source-secret" not in representation


def test_scan_reconciler_has_queue_store_and_no_source_database_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks: list[str] = []
    monkeypatch.setattr(
        bootstrap_module,
        "require_current_control_plane_schema",
        lambda **kwargs: checks.append(cast(str, kwargs["credential_kind"])),
    )

    reconciler = build_semantic_reconciler(settings=_settings(tmp_path))

    assert isinstance(reconciler, RunOneSemanticChangeScan)
    assert checks == ["reconciler", "reconciler"]
    assert isinstance(reconciler.scans, PostgresSemanticChangeScanStore)
    assert isinstance(reconciler.runner, InspectSemanticChangeScanRunner)
    assert _settings(tmp_path).database_url is None
    assert reconciler.retention.days == 17
    assert reconciler.maintenance_batch_size == 23
    representation = repr(reconciler)
    assert "reconciler-secret" not in representation
    assert "datahub-reader-secret" not in representation
    assert "source-secret" not in representation


def test_reconciler_probe_composes_no_polling_or_datahub_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    checks: list[str] = []
    monkeypatch.setattr(
        bootstrap_module,
        "require_current_control_plane_schema",
        lambda **kwargs: checks.append(cast(str, kwargs["credential_kind"])),
    )

    runtime = build_semantic_reconciler_process_runtime(
        readiness_probe=True,
        settings=settings,
    )

    assert runtime.reconciler is None
    assert runtime.control_resource is not None
    assert checks == ["reconciler"]


def test_operator_runtime_uses_lazy_latest_scan_and_trusted_local_actor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks: list[str] = []
    monkeypatch.setattr(
        bootstrap_module,
        "require_current_control_plane_schema",
        lambda **kwargs: checks.append(cast(str, kwargs["credential_kind"])),
    )

    runtime = build_semantic_change_operator_runtime(settings=_settings(tmp_path))

    assert checks == ["reconciler"]
    assert isinstance(runtime.services.inspect, InspectLatestSemanticChange)
    assert runtime.config.scope.catalog_scope == "synthetic-demo"
    representation = repr(runtime)
    assert "reconciler-secret" not in representation
    assert "datahub-reader-secret" not in representation


def test_reconciler_rejects_a_different_runtime_component(tmp_path: Path) -> None:
    settings = _settings(tmp_path).model_copy(update={"runtime_component": "web"})

    with pytest.raises(DatabaseConfigurationError, match="COMPONENT=reconciler"):
        build_semantic_reconciler(settings=settings)
