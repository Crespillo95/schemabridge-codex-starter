"""Focused adapter contracts for M35 PostgreSQL model replacement changes."""

from __future__ import annotations

from schemabridge.adapters.storage.postgres_registry_model_changes import (
    PostgresRegistryModelChangeStore,
    PostgresRegistryModelJoinProfileQueue,
    PostgresRegistryModelJoinProfileWitnessReader,
    PostgresRegistryModelProfileStore,
    PostgresRegistryModelRemediationEvidenceReader,
    PostgresRegistryModelReplacementSourceReader,
)


def test_registry_model_adapters_redact_their_dsn() -> None:
    dsn = "postgresql://schemabridge_api:do-not-render@localhost/control"

    adapters = (
        PostgresRegistryModelChangeStore(dsn),
        PostgresRegistryModelJoinProfileQueue(dsn),
        PostgresRegistryModelJoinProfileWitnessReader(dsn),
        PostgresRegistryModelProfileStore(dsn),
        PostgresRegistryModelRemediationEvidenceReader(dsn),
        PostgresRegistryModelReplacementSourceReader(dsn),
    )

    assert all(dsn not in repr(adapter) for adapter in adapters)
