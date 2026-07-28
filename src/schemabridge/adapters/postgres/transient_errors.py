"""Closed PostgreSQL retry classification shared by read-only source adapters."""

from __future__ import annotations

import psycopg

_TRANSIENT_SQLSTATES = frozenset(
    {
        "40001",  # serialization_failure
        "40P01",  # deadlock_detected
        "53300",  # too_many_connections
        "55P03",  # lock_not_available
        "57P01",  # admin_shutdown
        "57P02",  # crash_shutdown
        "57P03",  # cannot_connect_now
    }
)


def is_transient_postgres_error(error: psycopg.Error) -> bool:
    """Retry only connection failures and a small reviewed SQLSTATE allowlist."""

    sqlstate = error.sqlstate
    if sqlstate is not None:
        return sqlstate.startswith("08") or sqlstate in _TRANSIENT_SQLSTATES
    return isinstance(error, psycopg.OperationalError)
