from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest

from schemabridge.adapters.storage.sqlite_connection import managed_sqlite_connection


def test_managed_sqlite_connection_commits_and_closes_on_success(tmp_path: Path) -> None:
    database = tmp_path / "managed-success.db"

    with managed_sqlite_connection(database, isolation_level="DEFERRED") as connection:
        connection.execute("CREATE TABLE facts (value TEXT NOT NULL)")
        connection.execute("INSERT INTO facts (value) VALUES (?)", ("committed",))

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    with managed_sqlite_connection(database, isolation_level="DEFERRED") as reopened:
        assert reopened.execute("SELECT value FROM facts").fetchone() == ("committed",)


def test_managed_sqlite_connection_rolls_back_and_closes_on_error(tmp_path: Path) -> None:
    database = tmp_path / "managed-error.db"
    with managed_sqlite_connection(database, isolation_level="DEFERRED") as setup:
        setup.execute("CREATE TABLE facts (value TEXT NOT NULL)")

    with (
        pytest.raises(RuntimeError, match="stop transaction"),
        managed_sqlite_connection(database, isolation_level="DEFERRED") as connection,
    ):
        connection.execute("INSERT INTO facts (value) VALUES (?)", ("rolled-back",))
        raise RuntimeError("stop transaction")

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    with managed_sqlite_connection(database, isolation_level="DEFERRED") as reopened:
        assert reopened.execute("SELECT COUNT(*) FROM facts").fetchone() == (0,)


def test_managed_sqlite_connection_enforces_owner_only_permissions(tmp_path: Path) -> None:
    database = tmp_path / "private.db"
    database.touch(mode=0o666)
    database.chmod(0o666)

    with managed_sqlite_connection(database, isolation_level="DEFERRED") as connection:
        connection.execute("CREATE TABLE private_facts (value TEXT NOT NULL)")

    assert stat.S_IMODE(database.stat().st_mode) == 0o600
