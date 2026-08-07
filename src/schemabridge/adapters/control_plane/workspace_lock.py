"""Shared transaction lock for workspace-scoped control-plane mutations."""

from __future__ import annotations

import hashlib


def workspace_control_lock_id(workspace_id: str) -> int:
    """Return the stable PostgreSQL advisory-lock key for one workspace."""

    return int.from_bytes(
        hashlib.sha256(f"schemabridge.audit:{workspace_id}".encode()).digest()[:8],
        byteorder="big",
        signed=True,
    )


__all__ = ["workspace_control_lock_id"]
