"""Contract tests for tenant-scoped, durable workflow ownership."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

import schemabridge.adapters.storage.workflow_access as workflow_access_module
from schemabridge.adapters.storage.workflow_access import (
    InMemoryWorkflowAccessStore,
    SqliteWorkflowAccessStore,
)
from schemabridge.application.ports.workflow_access import (
    WorkflowAccessError,
    WorkflowAccessErrorCode,
    WorkflowAccessStorePort,
)
from schemabridge.domain.identity import WorkflowAccessGrant

CREATED_AT = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


@pytest.fixture(params=("memory", "sqlite"))
def store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> WorkflowAccessStorePort:
    if request.param == "memory":
        return InMemoryWorkflowAccessStore()
    return SqliteWorkflowAccessStore(tmp_path / "workflow-access.db")


def test_grant_is_idempotent_for_the_same_ownership(
    store: WorkflowAccessStorePort,
) -> None:
    original = _grant()
    replay_with_new_time = _grant(created_at=CREATED_AT + timedelta(minutes=1))

    assert store.grant(original) == original
    assert store.grant(replay_with_new_time) == original


def test_grant_rejects_a_different_owner_or_workspace(
    store: WorkflowAccessStorePort,
) -> None:
    original = store.grant(_grant())

    for conflicting in (
        _grant(owner_actor_id="actor-other"),
        _grant(workspace_id="workspace-other"),
    ):
        with pytest.raises(WorkflowAccessError) as raised:
            store.grant(conflicting)
        assert raised.value.code is WorkflowAccessErrorCode.CONFLICT

    assert store.load(original.workspace_id, original.workflow_id) == original


def test_load_hides_cross_workspace_and_cross_owner_grants(
    store: WorkflowAccessStorePort,
) -> None:
    grant = store.grant(_grant())

    assert store.load(grant.workspace_id, grant.workflow_id) == grant
    assert (
        store.load(
            grant.workspace_id,
            grant.workflow_id,
            owner_principal_id=grant.owner_actor_id,
        )
        == grant
    )
    assert store.load("workspace-other", grant.workflow_id) is None
    assert (
        store.load(
            grant.workspace_id,
            grant.workflow_id,
            owner_principal_id="actor-other",
        )
        is None
    )
    assert store.load(grant.workspace_id, "workflow-missing") is None


def test_list_for_workspace_is_bounded_scoped_and_newest_first(
    store: WorkflowAccessStorePort,
) -> None:
    for index in range(4):
        store.grant(
            _grant(
                workflow_id=f"workflow-{index}",
                owner_actor_id="actor-a" if index % 2 == 0 else "actor-b",
                created_at=CREATED_AT + timedelta(minutes=index),
            )
        )
    store.grant(_grant(workspace_id="workspace-b", workflow_id="other-workspace"))

    assert tuple(
        grant.workflow_id for grant in store.list_for_workspace("workspace-a", limit=2)
    ) == ("workflow-3", "workflow-2")
    assert tuple(
        grant.workflow_id
        for grant in store.list_for_workspace(
            "workspace-a",
            owner_principal_id="actor-a",
        )
    ) == ("workflow-2", "workflow-0")
    assert store.list_for_workspace("workspace-missing") == ()
    with pytest.raises(ValueError, match="between 1 and 100"):
        store.list_for_workspace("workspace-a", limit=101)


def test_sqlite_grant_survives_restart_and_preserves_shared_schema(tmp_path: Path) -> None:
    path = tmp_path / "shared-control-plane.db"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE existing_control_state (value TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO existing_control_state (value) VALUES (?)",
            ("preserved",),
        )

    original = SqliteWorkflowAccessStore(path).grant(_grant())
    reopened = SqliteWorkflowAccessStore(path)

    assert (
        reopened.load(
            original.workspace_id,
            original.workflow_id,
            owner_principal_id=original.owner_actor_id,
        )
        == original
    )
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT value FROM existing_control_state").fetchone() == (
            "preserved",
        )


def test_sqlite_grant_serializes_competing_owners(tmp_path: Path) -> None:
    path = tmp_path / "concurrent-access.db"
    first = SqliteWorkflowAccessStore(path)
    second = SqliteWorkflowAccessStore(path)
    barrier = Barrier(2)

    def attempt(
        access_store: SqliteWorkflowAccessStore,
        grant: WorkflowAccessGrant,
    ) -> WorkflowAccessGrant | WorkflowAccessErrorCode:
        barrier.wait()
        try:
            return access_store.grant(grant)
        except WorkflowAccessError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(attempt, first, _grant(owner_actor_id="actor-a")),
            executor.submit(attempt, second, _grant(owner_actor_id="actor-b")),
        )
        results = tuple(future.result() for future in futures)

    winners = tuple(item for item in results if isinstance(item, WorkflowAccessGrant))
    conflicts = tuple(item for item in results if item is WorkflowAccessErrorCode.CONFLICT)
    assert len(winners) == 1
    assert len(conflicts) == 1
    winner = winners[0]
    assert (
        SqliteWorkflowAccessStore(path).load(
            winner.workspace_id,
            winner.workflow_id,
            owner_principal_id=winner.owner_actor_id,
        )
        == winner
    )


def test_sqlite_invalid_persisted_grant_becomes_sanitized_store_failure(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invalid-access.db"
    store = SqliteWorkflowAccessStore(path)
    grant = store.grant(_grant())
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE workflow_access_grants SET created_at = ? WHERE workflow_id = ?",
            ("not-a-timestamp", grant.workflow_id),
        )

    with pytest.raises(WorkflowAccessError) as raised:
        store.load(grant.workspace_id, grant.workflow_id)
    assert raised.value.code is WorkflowAccessErrorCode.STORE_FAILURE
    assert str(raised.value) == "local workflow access store failed"


def test_sqlite_parent_creation_error_becomes_sanitized_store_failure(
    tmp_path: Path,
) -> None:
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.touch()

    with pytest.raises(WorkflowAccessError) as raised:
        SqliteWorkflowAccessStore(blocked_parent / "workflow-access.db")

    assert raised.value.code is WorkflowAccessErrorCode.STORE_FAILURE
    assert str(raised.value) == "local workflow access store failed"
    assert str(blocked_parent) not in str(raised.value)


def test_sqlite_connection_open_error_becomes_sanitized_store_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqliteWorkflowAccessStore(tmp_path / "workflow-access.db")

    def deny_connection(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("sensitive local control-plane path")

    monkeypatch.setattr(
        workflow_access_module,
        "managed_sqlite_connection",
        deny_connection,
    )

    with pytest.raises(WorkflowAccessError) as raised:
        store.load("workspace-a", "workflow-1")

    assert raised.value.code is WorkflowAccessErrorCode.STORE_FAILURE
    assert str(raised.value) == "local workflow access store failed"
    assert "sensitive local control-plane path" not in str(raised.value)


def _grant(
    *,
    workspace_id: str = "workspace-a",
    workflow_id: str = "workflow-1",
    owner_actor_id: str = "actor-a",
    created_at: datetime = CREATED_AT,
) -> WorkflowAccessGrant:
    return WorkflowAccessGrant(
        workflow_id=workflow_id,
        workspace_id=workspace_id,
        owner_actor_id=owner_actor_id,
        created_at=created_at,
    )
