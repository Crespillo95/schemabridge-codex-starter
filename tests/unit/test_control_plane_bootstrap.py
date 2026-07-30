"""M23 composition checks for active registry and managed durable stores."""

from __future__ import annotations

from pathlib import Path

import pytest

import schemabridge.bootstrap as bootstrap_module
from schemabridge.adapters.control_plane.postgres_operations import (
    PostgresControlPlaneBackup,
    PostgresControlPlaneRestore,
)
from schemabridge.adapters.control_plane.postgres_registry_control import (
    PostgresRegistryControlStore,
)
from schemabridge.adapters.semantic_registry.recorded import (
    RecordedGovernedSemanticRegistry,
)
from schemabridge.adapters.storage.postgres import (
    PostgresJoinReviewStore,
    PostgresPublicationAuditStore,
    PostgresRequestDraftStore,
    PostgresReviewStore,
)
from schemabridge.adapters.storage.reviews import SqliteReviewStore
from schemabridge.application.database_separation import (
    VerifySourceControlDatabaseSeparation,
)
from schemabridge.application.postgres_health import DatabaseConfigurationError
from schemabridge.application.registry_control import (
    LoadActiveGovernedSemanticRegistry,
)
from schemabridge.bootstrap import (
    build_control_plane_backup,
    build_control_plane_migrator,
    build_control_plane_restore,
    build_join_review_store,
    build_publication_audit_store,
    build_registry_control_store,
    build_request_draft_store,
    build_review_store,
    build_semantic_registry,
    build_source_control_database_separation,
)
from schemabridge.config import Settings

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DSN = "postgresql://runtime:runtime-secret@control.example.test/control"
AUDIT_KEY = "unit-test-control-audit-signing-key-with-diversity"
IDENTITY_KEY = "unit-test-identity-migration-key-with-diversity"


def _postgres_settings(**overrides: object) -> Settings:
    payload: dict[str, object] = {
        "OPENAI_API_KEY": None,
        "DATAHUB_GMS_TOKEN": None,
        "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
        "SCHEMABRIDGE_CONTROL_DATABASE_URL": RUNTIME_DSN,
        "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY": AUDIT_KEY,
        "SCHEMABRIDGE_IDENTITY_MIGRATION_KEY": IDENTITY_KEY,
    }
    payload.update(overrides)
    return Settings.model_validate(payload)


def test_local_composition_preserves_recorded_registry_and_sqlite_state() -> None:
    settings = Settings.model_validate({})

    assert isinstance(
        build_semantic_registry(repository_root=ROOT, settings=settings),
        (RecordedGovernedSemanticRegistry),
    )
    assert isinstance(build_review_store(settings), SqliteReviewStore)


def test_postgres_control_plane_selects_managed_state_stores_without_connecting() -> None:
    settings = _postgres_settings()
    workspace_id = "sb_workspace_test"

    assert isinstance(
        build_review_store(settings, workspace_id=workspace_id),
        PostgresReviewStore,
    )
    assert isinstance(
        build_join_review_store(settings, workspace_id=workspace_id),
        PostgresJoinReviewStore,
    )
    assert isinstance(
        build_request_draft_store(settings, workspace_id=workspace_id),
        PostgresRequestDraftStore,
    )
    assert isinstance(
        build_publication_audit_store(settings, workspace_id=workspace_id),
        PostgresPublicationAuditStore,
    )
    control = build_registry_control_store(settings=settings)
    assert isinstance(control, PostgresRegistryControlStore)
    assert "runtime-secret" not in repr(control)
    assert AUDIT_KEY not in repr(control)


def test_source_control_separation_builder_hides_both_database_credentials() -> None:
    settings = _postgres_settings(
        DATABASE_URL=("postgresql://schemabridge_reader:source-secret@source.example.test/source")
    )

    check = build_source_control_database_separation(settings)

    assert isinstance(check, VerifySourceControlDatabaseSeparation)
    rendered = repr(check)
    assert "source-secret" not in rendered
    assert "runtime-secret" not in rendered


def test_active_registry_composition_checks_schema_and_uses_pointer_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _postgres_settings(
        SCHEMABRIDGE_REGISTRY_MODE="live",
        SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION="active",
        SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS=True,
    )
    schema_checks: list[str] = []
    store = object()
    versions = object()

    def check_schema(**kwargs: object) -> object:
        schema_checks.append(str(kwargs["credential_kind"]))
        return object()

    monkeypatch.setattr(
        bootstrap_module,
        "require_current_control_plane_schema",
        check_schema,
    )
    monkeypatch.setattr(
        bootstrap_module,
        "build_registry_control_store",
        lambda **_kwargs: store,
    )
    monkeypatch.setattr(
        bootstrap_module,
        "build_registry_version_reader",
        lambda **_kwargs: versions,
    )

    registry = build_semantic_registry(
        repository_root=ROOT,
        settings=settings,
        workspace_id="sb_workspace_test",
    )

    assert isinstance(registry, LoadActiveGovernedSemanticRegistry)
    assert registry.store is store
    assert registry.versions is versions
    assert schema_checks == ["runtime"]


def test_migrator_builder_uses_pinned_release_version_and_hides_dsn() -> None:
    settings = Settings.model_validate(
        {
            "SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL": (
                "postgresql://migrator:do-not-print@control.example.test/control"
            ),
        }
    )

    migrator = build_control_plane_migrator(
        credential_kind="migrator",
        repository_root=ROOT,
        settings=settings,
    )

    assert migrator.known_migrations()[-1].version == 12  # type: ignore[attr-defined]
    assert "do-not-print" not in repr(migrator)


def test_migrator_builder_rejects_configured_schema_version_drift() -> None:
    settings = Settings.model_validate(
        {
            "SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL": (
                "postgresql://migrator:secret@control.example.test/control"
            ),
            "SCHEMABRIDGE_CONTROL_PLANE_SCHEMA_VERSION": 3,
        }
    )

    with pytest.raises(DatabaseConfigurationError, match="does not match this release"):
        build_control_plane_migrator(
            credential_kind="migrator",
            repository_root=ROOT,
            settings=settings,
        )


def test_backup_and_restore_builders_keep_operator_credentials_out_of_repr() -> None:
    backup_settings = _postgres_settings(
        SCHEMABRIDGE_COMPONENT="backup",
        SCHEMABRIDGE_CONTROL_DATABASE_URL=None,
        SCHEMABRIDGE_IDENTITY_MIGRATION_KEY=None,
        SCHEMABRIDGE_CONTROL_BACKUP_DATABASE_URL=(
            "postgresql://schemabridge_backup:backup-secret@control.example.test/control"
        ),
    )
    restore_settings = _postgres_settings(
        SCHEMABRIDGE_COMPONENT="operator",
        SCHEMABRIDGE_CONTROL_DATABASE_URL=None,
        SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL=(
            "postgresql://migrator:migration-secret@control.example.test/control"
        ),
        SCHEMABRIDGE_CONTROL_RESTORE_DATABASE_URL=(
            "postgresql://migrator:restore-secret@fresh-control.example.test/control_restore"
        ),
    )

    backup = build_control_plane_backup(repository_root=ROOT, settings=backup_settings)
    restore = build_control_plane_restore(
        repository_root=ROOT,
        settings=restore_settings,
    )

    assert isinstance(backup, PostgresControlPlaneBackup)
    assert isinstance(restore, PostgresControlPlaneRestore)
    assert "backup-secret" not in repr(backup)
    assert "migration-secret" not in repr(restore)
    assert "restore-secret" not in repr(restore)
