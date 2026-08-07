"""Resolve reviewed control migrations from a checkout or an installed wheel."""

from __future__ import annotations

from pathlib import Path

_REPOSITORY_RELATIVE_PATH = Path("migrations/control_plane")
_PACKAGED_PATH = Path(__file__).resolve().parents[2] / "migrations/control_plane"


def resolve_control_plane_migrations_path(repository_root: Path) -> Path:
    """Prefer an explicit checkout, otherwise use wheel-packaged migration files."""

    checkout_path = (repository_root / _REPOSITORY_RELATIVE_PATH).resolve()
    if checkout_path.is_dir():
        return checkout_path
    return _PACKAGED_PATH.resolve()
