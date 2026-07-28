"""Focused tests for PostgreSQL preview safety reporting."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg
import pytest

from schemabridge.adapters.connectors.source_identity import (
    PostgresSourceIdentityMismatchError,
)
from schemabridge.adapters.postgres.preview import (
    PsycopgQueryPreview,
    _postgres_interval_to_milliseconds,
)
from schemabridge.application.query_execution import (
    QueryPreviewRejectedError,
    QueryPreviewTimeoutError,
    QueryPreviewUnavailableError,
    ValidatedQuery,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    QueryCostBudget,
    SourceConnectorKind,
    SourceDialect,
    postgres_type_contract_fingerprint,
)


def _managed_target() -> GovernedExecutionTarget:
    budget = QueryCostBudget(
        explain_timeout_ms=500,
        max_response_bytes=16_384,
        max_total_cost=Decimal("100"),
        max_estimated_rows=1_000,
        max_plan_nodes=20,
        max_plan_depth=8,
        max_plan_width=512,
    )
    return GovernedExecutionTarget(
        workspace_id="workspace-a",
        connection_id=CatalogConnectionId("warehouse-primary"),
        connector_kind=SourceConnectorKind.POSTGRESQL,
        dialect=SourceDialect.POSTGRESQL,
        route_revision=1,
        route_fingerprint="a" * 64,
        expected_reader="schemabridge_reader",
        source_identity_fingerprint="b" * 64,
        catalog_identity_fingerprint="c" * 64,
        type_contract_fingerprint=postgres_type_contract_fingerprint(),
        cost_budget=budget,
        cost_budget_fingerprint=budget.fingerprint,
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
    dsn = "postgresql://sensitive-preview.invalid/example"
    preview = PsycopgQueryPreview(dsn)

    assert dsn not in repr(preview)

    with pytest.raises(QueryPreviewRejectedError, match="configured safety cap"):
        preview.execute(query)


@pytest.mark.parametrize(
    ("database_error", "expected_error"),
    (
        (psycopg.OperationalError("synthetic connection outage"), QueryPreviewUnavailableError),
        (psycopg.errors.UndefinedTable("synthetic missing table"), QueryPreviewRejectedError),
        (psycopg.errors.QueryCanceled("synthetic timeout"), QueryPreviewTimeoutError),
    ),
)
def test_preview_retries_only_reviewed_transient_database_failures(
    monkeypatch: pytest.MonkeyPatch,
    database_error: psycopg.Error,
    expected_error: type[Exception],
) -> None:
    def fail_connect(*_args: object, **_kwargs: object) -> None:
        raise database_error

    monkeypatch.setattr("schemabridge.adapters.postgres.preview.psycopg.connect", fail_connect)
    preview = PsycopgQueryPreview("postgresql://synthetic")

    with pytest.raises(expected_error):
        preview.execute(
            ValidatedQuery(
                sql="SELECT 1",
                parameters=(),
                max_rows=1,
                statement_timeout_ms=5_000,
            )
        )


def test_managed_preview_rejects_observed_source_retarget_before_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _managed_target()
    query = ValidatedQuery(
        sql="SELECT customer_id FROM crm.customers LIMIT 10",
        parameters=(),
        max_rows=10,
        statement_timeout_ms=5_000,
        dialect=SourceDialect.POSTGRESQL,
        target_fingerprint=target.fingerprint,
    )

    class _Cursor:
        description = None

        def __init__(self) -> None:
            self.executed: list[str] = []
            self.current = ""

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: str, _parameters: object = None) -> None:
            self.current = statement
            self.executed.append(statement)

        def fetchone(self) -> tuple[Any, ...] | None:
            if "current_user" in self.current:
                return (
                    "schemabridge_reader",
                    True,
                    "5s",
                    "127.0.0.1",
                    5432,
                    "silently_retargeted_database",
                )
            return None

    class _Connection:
        def __init__(self, cursor: _Cursor) -> None:
            self.cursor_value = cursor
            self.read_only = False
            self.rollbacks = 0

        def __enter__(self) -> _Connection:
            return self

        def __exit__(self, exc_type: object, *_args: object) -> None:
            if exc_type is not None:
                self.rollbacks += 1

        def cursor(self) -> _Cursor:
            return self.cursor_value

        def rollback(self) -> None:
            self.rollbacks += 1

    cursor = _Cursor()
    connection = _Connection(cursor)
    monkeypatch.setattr(
        "schemabridge.adapters.postgres.preview.psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )
    preview = PsycopgQueryPreview(
        "postgresql://synthetic",
        expected_user=target.expected_reader,
        bound_target_fingerprint=target.fingerprint,
    )

    with pytest.raises(PostgresSourceIdentityMismatchError) as raised:
        preview.execute(query, target=target)

    assert query.sql not in cursor.executed
    assert len(cursor.executed) == 2
    assert connection.rollbacks == 1
    assert "silently_retargeted_database" not in str(raised.value)
