"""Composition-root fail-closed checks for semantic-registry selection."""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabridge.application.ports.planning import (
    PlanningPortError,
    PlanningPortErrorCode,
)
from schemabridge.application.postgres_health import DatabaseConfigurationError
from schemabridge.bootstrap import (
    build_recorded_registry_publication_source,
    build_semantic_registry,
    build_semantic_registry_publication_approval_preparer,
    build_semantic_registry_version_publisher,
)
from schemabridge.config import Settings

ROOT = Path(__file__).resolve().parents[2]


def test_recorded_publication_source_derives_an_explicit_immutable_version() -> None:
    settings = Settings.model_validate(
        {
            "SCHEMABRIDGE_SEMANTIC_REGISTRY_VERSION": 1,
        }
    )

    source = build_recorded_registry_publication_source(
        target_version=3,
        repository_root=ROOT,
        settings=settings,
    )

    assert source.version == 3
    assert source.registry.source.startswith("recorded:")


def test_live_registry_composition_preserves_and_rejects_a_credential_symlink(
    tmp_path: Path,
) -> None:
    credential = tmp_path / "reader-target.env"
    credential.write_text(
        "DATAHUB_GMS_URL=http://127.0.0.1:8080\nDATAHUB_GMS_TOKEN=synthetic-token\n",
        encoding="utf-8",
    )
    credential.chmod(0o600)
    symlink = tmp_path / "reader.env"
    symlink.symlink_to(credential)
    settings = Settings.model_validate(
        {
            "SCHEMABRIDGE_REGISTRY_MODE": "live",
            "SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS": True,
            "SCHEMABRIDGE_SEMANTIC_REGISTRY_READER_ENV_PATH": symlink,
        }
    )

    with pytest.raises(PlanningPortError) as raised:
        build_semantic_registry(repository_root=ROOT, settings=settings)

    assert raised.value.code is PlanningPortErrorCode.CONTEXT_UNAVAILABLE
    assert "synthetic-token" not in str(raised.value)


def test_hosted_demo_cannot_compose_registry_publication_components() -> None:
    settings = Settings.model_validate(
        {
            "SCHEMABRIDGE_ENVIRONMENT": "hosted-demo",
        }
    )

    with pytest.raises(DatabaseConfigurationError, match="disabled in the hosted demo"):
        build_semantic_registry_publication_approval_preparer(settings=settings)
    with pytest.raises(DatabaseConfigurationError, match="disabled in the hosted demo"):
        build_semantic_registry_version_publisher(repository_root=ROOT, settings=settings)


def test_registry_publication_store_initialization_failure_is_sanitized(
    tmp_path: Path,
) -> None:
    invalid_store = tmp_path / "audit-is-a-directory"
    invalid_store.mkdir()
    settings = Settings.model_validate(
        {
            "SCHEMABRIDGE_DRAFT_STORE_PATH": invalid_store,
        }
    )

    with pytest.raises(DatabaseConfigurationError) as raised:
        build_semantic_registry_publication_approval_preparer(settings=settings)

    assert str(raised.value) == ("semantic registry publication audit store is unavailable")
    assert str(invalid_store) not in str(raised.value)


def test_registry_publication_parent_creation_failure_is_sanitized() -> None:
    settings = Settings.model_validate(
        {
            "SCHEMABRIDGE_DRAFT_STORE_PATH": Path("/dev/null/audit.sqlite3"),
        }
    )

    with pytest.raises(DatabaseConfigurationError) as raised:
        build_semantic_registry_publication_approval_preparer(settings=settings)

    assert str(raised.value) == ("semantic registry publication audit store is unavailable")
    assert "/dev/null" not in str(raised.value)
