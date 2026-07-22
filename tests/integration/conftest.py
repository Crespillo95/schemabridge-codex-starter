"""Fixtures for the local synthetic PostgreSQL service."""

from __future__ import annotations

import os

import pytest

DEFAULT_READER_DSN = (
    "postgresql://schemabridge_reader:schemabridge_reader@127.0.0.1:55433/schemabridge"
)


@pytest.fixture(scope="session")
def reader_dsn() -> str:
    """Return the explicit synthetic reader DSN for integration tests."""

    return os.environ.get("SCHEMABRIDGE_TEST_DATABASE_URL", DEFAULT_READER_DSN)
