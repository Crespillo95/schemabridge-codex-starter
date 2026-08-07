"""Canonical, non-disclosing identities for observed PostgreSQL sources."""

from __future__ import annotations

from schemabridge.domain.connectors import postgres_source_identity_fingerprint


class PostgresSourceIdentityMismatchError(RuntimeError):
    """The connected database is not the source bound to the approved target."""


def require_postgres_source_identity(
    *,
    expected_fingerprint: str,
    server_address: object,
    server_port: object,
    database: object,
    user: object,
) -> None:
    """Fail closed when one same-transaction observation differs from approval."""

    try:
        observed = postgres_source_identity_fingerprint(
            server_address=server_address if isinstance(server_address, str) else "",
            server_port=(
                server_port
                if isinstance(server_port, int) and not isinstance(server_port, bool)
                else -1
            ),
            database=database if isinstance(database, str) else "",
            user=user if isinstance(user, str) else "",
        )
    except (TypeError, ValueError):
        raise PostgresSourceIdentityMismatchError(
            "PostgreSQL source identity does not match the governed target"
        ) from None
    if observed != expected_fingerprint:
        raise PostgresSourceIdentityMismatchError(
            "PostgreSQL source identity does not match the governed target"
        )


__all__ = [
    "PostgresSourceIdentityMismatchError",
    "postgres_source_identity_fingerprint",
    "require_postgres_source_identity",
]
