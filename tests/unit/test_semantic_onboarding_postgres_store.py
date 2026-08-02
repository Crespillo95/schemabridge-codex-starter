"""Pure configuration checks for the M33 PostgreSQL store."""

from __future__ import annotations

import pytest

from schemabridge.adapters.storage.postgres_semantic_onboarding import (
    PostgresSemanticOnboardingStore,
)


@pytest.mark.parametrize("stale_after_seconds", [59, 2_592_001])
def test_store_rejects_an_unbounded_catalog_freshness_policy(
    stale_after_seconds: int,
) -> None:
    with pytest.raises(ValueError, match="stale threshold"):
        PostgresSemanticOnboardingStore(
            "postgresql://runtime:not-printed@control.example/control",
            stale_after_seconds=stale_after_seconds,
        )


def test_store_repr_redacts_database_credentials() -> None:
    store = PostgresSemanticOnboardingStore(
        "postgresql://runtime:not-printed@control.example/control"
    )

    assert "not-printed" not in repr(store)
