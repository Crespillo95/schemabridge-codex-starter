"""Focused tests for PostgreSQL preview safety reporting."""

import pytest

from schemabridge.adapters.postgres.preview import (
    PsycopgQueryPreview,
    _postgres_interval_to_milliseconds,
)
from schemabridge.application.query_execution import (
    QueryPreviewUnavailableError,
    ValidatedQuery,
)


def test_postgres_timeout_display_units_are_reported_as_milliseconds() -> None:
    assert _postgres_interval_to_milliseconds("50ms") == 50
    assert _postgres_interval_to_milliseconds("5s") == 5_000
    assert _postgres_interval_to_milliseconds("1min") == 60_000
    assert _postgres_interval_to_milliseconds("250") == 250


@pytest.mark.parametrize(
    "query",
    [
        ValidatedQuery(sql="SELECT 1", parameters=(), max_rows=501, statement_timeout_ms=5_000),
        ValidatedQuery(sql="SELECT 1", parameters=(), max_rows=500, statement_timeout_ms=5_001),
        ValidatedQuery(sql="SELECT 1", parameters=(), max_rows=500, statement_timeout_ms=0),
    ],
)
def test_preview_rejects_forged_runtime_limits_before_connecting(query: ValidatedQuery) -> None:
    preview = PsycopgQueryPreview("postgresql://must-not-connect.invalid/example")

    with pytest.raises(QueryPreviewUnavailableError, match="configured safety cap"):
        preview.execute(query)
