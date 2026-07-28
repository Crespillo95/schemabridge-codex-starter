"""Shared lifecycle management for local SQLite adapter connections."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

SqliteIsolationLevel = Literal["DEFERRED", "EXCLUSIVE", "IMMEDIATE"] | None


@contextmanager
def managed_sqlite_connection(
    path: Path,
    *,
    isolation_level: SqliteIsolationLevel,
    timeout: float = 5.0,
    foreign_keys: bool = False,
) -> Iterator[sqlite3.Connection]:
    """Open one transactional connection and always release its native handle."""

    file_descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(file_descriptor, 0o600)
    finally:
        os.close(file_descriptor)
    connection = sqlite3.connect(
        path,
        isolation_level=isolation_level,
        timeout=timeout,
    )
    try:
        if foreign_keys:
            connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            yield connection
    finally:
        connection.close()
