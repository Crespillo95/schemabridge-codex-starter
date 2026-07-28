"""Regression tests for bounded, policy-consistent rejection inspection."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from schemabridge.adapters.connectors.source_identity import (
    PostgresSourceIdentityMismatchError,
)
from schemabridge.adapters.postgres.rejections import (
    PsycopgRejectedSourceReporter,
    _inspection_query,
)
from schemabridge.application.join_demo import build_north_star_join_proposals
from schemabridge.application.ports.planning import (
    PlanningPortError,
    PlanningPortErrorCode,
    ProtectedSourceOperationCancelled,
)
from schemabridge.bootstrap import build_semantic_registry
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    QueryCostBudget,
    SourceConnectorKind,
    SourceDialect,
    postgres_type_contract_fingerprint,
)
from schemabridge.domain.resolution import RejectionCheck, SourceRejectionCode


class _Cursor:
    def __init__(self) -> None:
        self.current = ""
        self.fetchmany_sizes: list[int] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: Any, parameters: object = None) -> None:
        del parameters
        self.current = query if isinstance(query, str) else query.as_string()

    def fetchone(self) -> tuple[str, bool, str] | None:
        if "current_user" in self.current:
            return ("schemabridge_reader", True, "5s")
        return None

    def fetchmany(self, size: int) -> list[tuple[str, str, int]]:
        self.fetchmany_sizes.append(size)
        return [
            ("-1", "negative_identifier", 3),
            ("127.5", "non_integral_identifier", 3),
        ][:size]


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self.read_only = False
        self._cursor = cursor

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self._cursor

    def rollback(self) -> None:
        return None


def _check(index: int) -> RejectionCheck:
    key = (
        build_north_star_join_proposals()[0].left_key
        if index == 0
        else build_north_star_join_proposals()[0].right_key
    )
    return RejectionCheck(
        logical_field=key.logical_field,
        physical_field=key.physical_field,
        transformation_plan=key.transformation_plan,
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


def test_rejection_report_is_bounded_and_reports_exact_total(monkeypatch: Any) -> None:
    cursor = _Cursor()
    monkeypatch.setattr(
        "schemabridge.adapters.postgres.rejections.psycopg.connect",
        lambda *_args, **_kwargs: _Connection(cursor),
    )
    reporter = PsycopgRejectedSourceReporter(
        "postgresql://synthetic",
        allowed_fields=frozenset({_check(1).physical_field.root}),
        max_records=1,
    )

    assert "postgresql://" not in repr(reporter)

    report = reporter.inspect((_check(1),), statement_timeout_ms=5_000)

    assert cursor.fetchmany_sizes == [2]
    assert report.total_records == 3
    assert report.truncated is True
    assert len(report.records) == 1
    assert report.records[0].code is SourceRejectionCode.NEGATIVE_IDENTIFIER


def test_cancellation_between_checks_prevents_every_later_source_statement(
    monkeypatch: Any,
) -> None:
    cursor = _Cursor()
    monkeypatch.setattr(
        "schemabridge.adapters.postgres.rejections.psycopg.connect",
        lambda *_args, **_kwargs: _Connection(cursor),
    )
    checks = (_check(0), _check(1))
    reporter = PsycopgRejectedSourceReporter(
        "postgresql://synthetic",
        allowed_fields=frozenset(check.physical_field.root for check in checks),
        max_records=10,
    )
    calls = 0

    def should_continue() -> bool:
        nonlocal calls
        calls += 1
        return calls < 5

    with pytest.raises(ProtectedSourceOperationCancelled):
        reporter.inspect(
            checks,
            statement_timeout_ms=5_000,
            should_continue=should_continue,
        )

    assert calls == 5
    assert cursor.fetchmany_sizes == [11]


def test_rejection_sql_has_negative_paths_count_and_hard_limit() -> None:
    numeric_sql = _inspection_query(_check(1), limit=11)[0].as_string()
    string_sql = _inspection_query(_check(0), limit=11)[0].as_string()

    assert "WHEN \"gf_customer_id\" < 0 THEN 'negative_identifier'" in numeric_sql
    assert "COUNT(*) OVER () AS total_records" in numeric_sql
    assert "LIMIT 11" in numeric_sql
    assert "~ '^-[0-9]+$' THEN 'negative_identifier'" in string_sql


def test_integer_identifier_rejection_does_not_apply_the_float_safety_bound() -> None:
    registry = (
        build_semantic_registry(repository_root=Path(__file__).resolve().parents[2]).load().registry
    )
    contract = next(
        item for item in registry.join_contracts.contracts if item.id == "sales_order_to_shipment"
    )
    check = RejectionCheck(
        logical_field=contract.left_key.logical_field,
        physical_field=contract.left_key.physical_field,
        transformation_plan=contract.left_key.transformation_plan,
    )

    query = _inspection_query(check, limit=11)[0].as_string()

    assert "WHEN \"order_id\" < 0 THEN 'negative_identifier'" in query
    assert "unsafe_float_identifier" not in query


@pytest.mark.parametrize(
    ("database_error", "expected_code"),
    (
        (
            psycopg.OperationalError("synthetic connection outage"),
            PlanningPortErrorCode.REJECTION_INSPECTION_UNAVAILABLE,
        ),
        (
            psycopg.errors.UndefinedTable("synthetic missing table"),
            PlanningPortErrorCode.REJECTION_INSPECTION_INVALID,
        ),
        (
            psycopg.errors.QueryCanceled("synthetic timeout"),
            PlanningPortErrorCode.REJECTION_INSPECTION_TIMEOUT,
        ),
    ),
)
def test_rejection_inspection_retries_only_reviewed_transient_database_failures(
    monkeypatch: pytest.MonkeyPatch,
    database_error: psycopg.Error,
    expected_code: PlanningPortErrorCode,
) -> None:
    def fail_connect(*_args: object, **_kwargs: object) -> None:
        raise database_error

    monkeypatch.setattr(
        "schemabridge.adapters.postgres.rejections.psycopg.connect",
        fail_connect,
    )
    check = _check(1)
    reporter = PsycopgRejectedSourceReporter(
        "postgresql://synthetic",
        allowed_fields=frozenset({check.physical_field.root}),
    )

    with pytest.raises(PlanningPortError) as raised:
        reporter.inspect((check,), statement_timeout_ms=5_000)

    assert raised.value.code is expected_code


def test_managed_rejection_inspection_rejects_source_retarget_before_data_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _managed_target()

    class _MismatchCursor(_Cursor):
        def __init__(self) -> None:
            super().__init__()
            self.executed: list[str] = []

        def execute(self, query: Any, parameters: object = None) -> None:
            super().execute(query, parameters)
            self.executed.append(self.current)

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

    class _RollbackConnection(_Connection):
        def __init__(self, cursor: _Cursor) -> None:
            super().__init__(cursor)
            self.rollbacks = 0

        def __exit__(self, *args: object) -> None:
            if args and args[0] is not None:
                self.rollbacks += 1

        def rollback(self) -> None:
            self.rollbacks += 1

    cursor = _MismatchCursor()
    connection = _RollbackConnection(cursor)
    monkeypatch.setattr(
        "schemabridge.adapters.postgres.rejections.psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )
    check = _check(1)
    reporter = PsycopgRejectedSourceReporter(
        "postgresql://synthetic",
        allowed_fields=frozenset({check.physical_field.root}),
        expected_user=target.expected_reader,
        bound_target_fingerprint=target.fingerprint,
    )

    with pytest.raises(PostgresSourceIdentityMismatchError) as raised:
        reporter.inspect(
            (check,),
            statement_timeout_ms=5_000,
            target=target,
        )

    assert len(cursor.executed) == 2
    assert cursor.fetchmany_sizes == []
    assert connection.rollbacks == 1
    assert "silently_retargeted_database" not in str(raised.value)
