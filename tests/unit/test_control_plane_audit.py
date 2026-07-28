"""Pure integrity checks for the HMAC-chained M23 control audit."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from schemabridge.adapters.control_plane.postgres_registry_control import (
    PostgresRegistryControlStore,
    _advisory_lock_id,
    _audit_hash,
    _canonical_fingerprint,
    _event_id,
)

KEY = b"control-audit-key-0123456789-abcdef"


def _hash(**changes: object) -> str:
    values: dict[str, object] = {
        "key": KEY,
        "event_id": _event_id("registry_transition", "transition-a"),
        "workspace_id": "workspace-a",
        "operation": "registry_activate",
        "transition_id": "transition-a",
        "previous_hash": None,
        "payload_fingerprint": _canonical_fingerprint({"generation": 1}),
        "key_version": "v1",
        "occurred_at": datetime(2026, 7, 23, 12, 0, tzinfo=UTC).isoformat(),
    }
    values.update(changes)
    return _audit_hash(**values)  # type: ignore[arg-type]


def test_audit_hash_is_deterministic_and_binds_every_chain_field() -> None:
    baseline = _hash()

    assert baseline == _hash()
    assert len(baseline) == 64
    assert baseline != _hash(workspace_id="workspace-b")
    assert baseline != _hash(operation="registry_rollback")
    assert baseline != _hash(transition_id="transition-b")
    assert baseline != _hash(previous_hash="f" * 64)
    assert baseline != _hash(payload_fingerprint="a" * 64)
    assert baseline != _hash(key_version="v2")
    assert baseline != _hash(occurred_at="2026-07-23T12:00:01+00:00")
    assert baseline != _hash(key=b"another-audit-key-0123456789-abcdef")


def test_workspace_audit_lock_and_event_id_are_stable_and_scoped() -> None:
    assert _advisory_lock_id("workspace-a") == _advisory_lock_id("workspace-a")
    assert _advisory_lock_id("workspace-a") != _advisory_lock_id("workspace-b")
    assert _event_id("registry_transition", "transition-a") == _event_id(
        "registry_transition",
        "transition-a",
    )
    assert _event_id("registry_transition", "transition-a") != _event_id(
        "registry_projection",
        "transition-a",
    )


@pytest.mark.parametrize(
    ("keys", "active"),
    [
        ({}, "v1"),
        ({"v1": b"short"}, "v1"),
        ({"v1": b"a" * 32}, "v1"),
        ({"v1": KEY}, "v2"),
    ],
)
def test_store_rejects_missing_weak_or_inactive_audit_keys(
    keys: dict[str, bytes],
    active: str,
) -> None:
    with pytest.raises(ValueError, match="audit"):
        PostgresRegistryControlStore(
            "postgresql://runtime:not-printed@control.example/control",
            keys,
            active,
        )


def test_store_repr_redacts_database_and_hmac_secrets() -> None:
    store = PostgresRegistryControlStore(
        "postgresql://runtime:not-printed@control.example/control",
        {"v1": KEY},
        "v1",
    )

    rendered = repr(store)
    assert "not-printed" not in rendered
    assert "postgresql://" not in rendered
    assert KEY.decode() not in rendered
