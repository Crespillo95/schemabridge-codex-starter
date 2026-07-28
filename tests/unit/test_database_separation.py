from __future__ import annotations

import pytest

from schemabridge.application.database_separation import (
    DatabaseEndpointIdentity,
    DatabaseIdentityProbeError,
    VerifySourceControlDatabaseSeparation,
)
from schemabridge.application.postgres_health import DatabaseConfigurationError


class _Probe:
    def __init__(
        self,
        identity: DatabaseEndpointIdentity | None = None,
        *,
        fails: bool = False,
    ) -> None:
        self.identity = identity
        self.fails = fails

    def inspect(self) -> DatabaseEndpointIdentity:
        if self.fails:
            raise DatabaseIdentityProbeError("unavailable")
        assert self.identity is not None
        return self.identity


def _identity(database: str, user: str) -> DatabaseEndpointIdentity:
    return DatabaseEndpointIdentity(
        server_address="127.0.0.1",
        server_port=5432,
        database=database,
        user=user,
    )


def test_server_observed_coordinates_prove_distinct_databases() -> None:
    result = VerifySourceControlDatabaseSeparation(
        source=_Probe(_identity("source", "reader")),
        control=_Probe(_identity("control", "runtime")),
    ).execute()

    assert result.separate is True
    assert result.source.database == "source"
    assert result.control.database == "control"


def test_same_database_or_unavailable_probe_fails_closed_with_sanitized_error() -> None:
    same = _identity("shared", "reader")
    with pytest.raises(DatabaseConfigurationError, match="must be separate"):
        VerifySourceControlDatabaseSeparation(
            source=_Probe(same),
            control=_Probe(
                DatabaseEndpointIdentity(
                    server_address=same.server_address,
                    server_port=same.server_port,
                    database=same.database,
                    user="runtime",
                )
            ),
        ).execute()

    with pytest.raises(DatabaseConfigurationError, match="could not be verified") as unavailable:
        VerifySourceControlDatabaseSeparation(
            source=_Probe(fails=True),
            control=_Probe(_identity("control", "runtime")),
        ).execute()
    assert "unavailable" not in str(unavailable.value)
