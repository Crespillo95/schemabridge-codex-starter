from __future__ import annotations

from pathlib import Path

import pytest

import schemabridge.adapters.control_plane.migration_paths as paths_module
from schemabridge.adapters.control_plane.migration_paths import (
    resolve_control_plane_migrations_path,
)


def test_checkout_migrations_take_precedence(tmp_path: Path) -> None:
    checkout = tmp_path / "migrations" / "control_plane"
    checkout.mkdir(parents=True)

    assert resolve_control_plane_migrations_path(tmp_path) == checkout


def test_installed_package_path_is_used_outside_a_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    packaged = tmp_path / "site-packages" / "schemabridge" / "migrations" / "control_plane"
    packaged.mkdir(parents=True)
    monkeypatch.setattr(paths_module, "_PACKAGED_PATH", packaged)

    assert resolve_control_plane_migrations_path(tmp_path / "empty-working-directory") == packaged
