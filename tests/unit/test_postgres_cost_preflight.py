"""PostgreSQL EXPLAIN preflight safety and parser regressions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from schemabridge.adapters.connectors.source_identity import (
    PostgresSourceIdentityMismatchError,
    postgres_source_identity_fingerprint,
)
from schemabridge.adapters.postgres.cost_preflight import (
    _EXPLAIN_PREFIX,
    PsycopgQueryCostPreflight,
    _BoundedExplainJsonLoader,
    _ExplainResponseBytesExceeded,
    _parse_cost_plan,
)
from schemabridge.application.query_cost import (
    AssessGovernedQueryCost,
    QueryCostError,
    QueryCostErrorCode,
)
from schemabridge.application.query_execution import ValidatedQuery
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    QueryCostBudget,
    QueryCostDecision,
    QueryCostRejectionCode,
    SourceConnectorKind,
    SourceDialect,
    postgres_type_contract_fingerprint,
)

_SERVER_ADDRESS = "127.0.0.1"
_SERVER_PORT = 5432
_DATABASE = "tenant_a"
_SOURCE_IDENTITY = postgres_source_identity_fingerprint(
    server_address=_SERVER_ADDRESS,
    server_port=_SERVER_PORT,
    database=_DATABASE,
    user="schemabridge_reader",
)


def _budget(**updates: object) -> QueryCostBudget:
    values: dict[str, object] = {
        "explain_timeout_ms": 1_000,
        "max_response_bytes": 65_536,
        "max_total_cost": Decimal("1000"),
        "max_estimated_rows": 10_000,
        "max_plan_nodes": 100,
        "max_plan_depth": 10,
        "max_plan_width": 1_024,
    }
    values.update(updates)
    return QueryCostBudget.model_validate(values)


def _target(*, budget: QueryCostBudget | None = None) -> GovernedExecutionTarget:
    active_budget = budget or _budget()
    return GovernedExecutionTarget(
        workspace_id="tenant-a",
        connection_id=CatalogConnectionId("warehouse-primary"),
        connector_kind=SourceConnectorKind.POSTGRESQL,
        dialect=SourceDialect.POSTGRESQL,
        route_revision=1,
        route_fingerprint="a" * 64,
        expected_reader="schemabridge_reader",
        source_identity_fingerprint=_SOURCE_IDENTITY,
        catalog_identity_fingerprint="c" * 64,
        type_contract_fingerprint=postgres_type_contract_fingerprint(),
        cost_budget=active_budget,
        cost_budget_fingerprint=active_budget.fingerprint,
    )


def _query(target: GovernedExecutionTarget) -> ValidatedQuery:
    return ValidatedQuery(
        sql="SELECT c.customer_id FROM crm.customers AS c LIMIT 10",
        parameters=(),
        max_rows=10,
        statement_timeout_ms=5_000,
        dialect=SourceDialect.POSTGRESQL,
        target_fingerprint=target.fingerprint,
    )


def _plan(
    *,
    total_cost: object = 123.45,
    rows: object = 100,
    width: object = 16,
    children: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    root: dict[str, object] = {
        "Node Type": "Synthetic",
        "Total Cost": total_cost,
        "Plan Rows": rows,
        "Plan Width": width,
    }
    if children is not None:
        root["Plans"] = children
    return [{"Plan": root}]


def _encoded(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), allow_nan=True).encode()


@dataclass
class _Adapters:
    registrations: list[tuple[str, type[object]]] = field(default_factory=list)

    def register_loader(self, type_name: str, loader: type[object]) -> None:
        self.registrations.append((type_name, loader))


@dataclass
class _Cursor:
    results: list[object]
    executions: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> None:
        self.executions.append((sql, parameters))

    def fetchone(self) -> object:
        return self.results.pop(0)


@dataclass
class _Connection:
    cursor_value: _Cursor
    adapters: _Adapters = field(default_factory=_Adapters)
    read_only: bool = False
    rollbacks: int = 0

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self.cursor_value

    def rollback(self) -> None:
        self.rollbacks += 1


def _install_connection(
    monkeypatch: pytest.MonkeyPatch,
    plan: object,
    *,
    raw_plan: bytes | None = None,
    reader: str = "schemabridge_reader",
    read_only: bool = True,
    timeout: str = "1s",
) -> _Connection:
    cursor = _Cursor(
        [
            None,
            (
                reader,
                read_only,
                timeout,
                _SERVER_ADDRESS,
                _SERVER_PORT,
                _DATABASE,
            ),
            (_encoded(plan) if raw_plan is None else raw_plan,),
            None,
        ]
    )
    connection = _Connection(cursor)
    monkeypatch.setattr(
        "schemabridge.adapters.postgres.cost_preflight.psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )
    return connection


def test_cost_preflight_runs_exact_explain_without_analyze_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target()
    query = _query(target)
    connection = _install_connection(monkeypatch, _plan())
    preflight = PsycopgQueryCostPreflight("postgresql://sensitive.invalid/source")

    assessment = AssessGovernedQueryCost(preflight).execute(query, target)

    assert assessment.decision is QueryCostDecision.ACCEPTED
    assert assessment.total_cost == Decimal("123.45")
    assert assessment.estimated_root_rows == 100
    assert assessment.plan_node_count == 1
    assert assessment.plan_depth == 1
    assert connection.read_only is True
    assert connection.rollbacks == 1
    explain_sql, parameters = connection.cursor_value.executions[2]
    assert explain_sql == f"{_EXPLAIN_PREFIX}{query.sql}"
    assert "ANALYZE FALSE" in explain_sql
    assert "ANALYZE TRUE" not in explain_sql
    assert parameters == query.parameters
    assert connection.adapters.registrations == [("json", _BoundedExplainJsonLoader)]
    assert "sensitive.invalid" not in repr(preflight)


@pytest.mark.parametrize(
    ("budget_updates", "plan", "expected"),
    (
        (
            {"max_total_cost": Decimal("100")},
            _plan(total_cost=100.01),
            QueryCostRejectionCode.TOTAL_COST_EXCEEDED,
        ),
        (
            {"max_estimated_rows": 99},
            _plan(rows=100),
            QueryCostRejectionCode.ESTIMATED_ROWS_EXCEEDED,
        ),
        (
            {"max_plan_width": 15},
            _plan(width=16),
            QueryCostRejectionCode.PLAN_WIDTH_EXCEEDED,
        ),
        (
            {"max_plan_nodes": 1},
            _plan(children=[{"Plan Width": 1}]),
            QueryCostRejectionCode.PLAN_NODES_EXCEEDED,
        ),
        (
            {"max_plan_depth": 1},
            _plan(children=[{"Plan Width": 1}]),
            QueryCostRejectionCode.PLAN_DEPTH_EXCEEDED,
        ),
    ),
)
def test_cost_preflight_rejects_each_governed_plan_bound(
    monkeypatch: pytest.MonkeyPatch,
    budget_updates: dict[str, object],
    plan: object,
    expected: QueryCostRejectionCode,
) -> None:
    target = _target(budget=_budget(**budget_updates))
    _install_connection(monkeypatch, plan)

    assessment = PsycopgQueryCostPreflight("postgresql://synthetic").assess(
        _query(target),
        target,
    )

    assert assessment.decision is QueryCostDecision.REJECTED
    assert expected in assessment.rejection_codes


def test_cost_preflight_rejection_blocks_the_application_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target(budget=_budget(max_total_cost=Decimal("100")))
    _install_connection(monkeypatch, _plan(total_cost=101))
    use_case = AssessGovernedQueryCost(PsycopgQueryCostPreflight("postgresql://synthetic"))
    assessment = use_case.execute(_query(target), target)

    with pytest.raises(QueryCostError) as captured:
        use_case.require_accepted(assessment)

    assert captured.value.code is QueryCostErrorCode.REJECTED
    assert captured.value.assessment == assessment


@pytest.mark.parametrize(
    "plan",
    (
        None,
        {},
        [],
        [{}, {"Plan": {}}],
        [{"Plan": []}],
        [{"Plan": {"Total Cost": float("nan"), "Plan Rows": 1, "Plan Width": 1}}],
        [{"Plan": {"Total Cost": -1, "Plan Rows": 1, "Plan Width": 1}}],
        [{"Plan": {"Total Cost": 1, "Plan Rows": 1.5, "Plan Width": 1}}],
        [{"Plan": {"Total Cost": 1, "Plan Rows": 1, "Plan Width": -1}}],
        [
            {
                "Plan": {
                    "Total Cost": 1,
                    "Plan Rows": 1,
                    "Plan Width": 1,
                    "Plans": ["not-a-node"],
                }
            }
        ],
    ),
)
def test_cost_plan_parser_rejects_malformed_or_unsafe_payloads(plan: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        _parse_cost_plan(_encoded(plan))


def test_cost_plan_parser_rejects_budget_excess_before_json_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _encoded(_plan())
    decoded = False

    def fail_decode(*_args: object, **_kwargs: object) -> object:
        nonlocal decoded
        decoded = True
        raise AssertionError("JSON decoding must not start")

    monkeypatch.setattr(
        "schemabridge.adapters.postgres.cost_preflight.json.loads",
        fail_decode,
    )

    with pytest.raises(_ExplainResponseBytesExceeded):
        _parse_cost_plan(raw, maximum_response_bytes=len(raw) - 1)

    assert decoded is False


def test_bounded_json_loader_rejects_hard_excess_before_copy() -> None:
    loader = _BoundedExplainJsonLoader(114)
    oversized = memoryview(bytearray(4 * 1_024 * 1_024 + 1))

    with pytest.raises(_ExplainResponseBytesExceeded):
        loader.load(oversized)


def test_cost_plan_parser_rejects_duplicate_keys() -> None:
    raw = b'[{"Plan":{"Total Cost":1,"Total Cost":2,"Plan Rows":1,"Plan Width":1}}]'

    with pytest.raises(ValueError, match="duplicate keys"):
        _parse_cost_plan(raw)


def test_cost_preflight_rejects_response_budget_before_json_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    raw = _encoded(plan)
    target = _target(budget=_budget(max_response_bytes=len(raw) - 1))
    _install_connection(monkeypatch, plan)
    decoded = False

    def fail_decode(*_args: object, **_kwargs: object) -> object:
        nonlocal decoded
        decoded = True
        raise AssertionError("JSON decoding must not start")

    monkeypatch.setattr(
        "schemabridge.adapters.postgres.cost_preflight.json.loads",
        fail_decode,
    )

    assessment = PsycopgQueryCostPreflight("postgresql://synthetic").assess(
        _query(target),
        target,
    )

    assert assessment.rejection_codes == (QueryCostRejectionCode.RESPONSE_BYTES_EXCEEDED,)
    assert assessment.total_cost is None
    assert decoded is False


def test_cost_preflight_rejects_decimal_precision_rounding_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target(budget=_budget(max_total_cost=Decimal("1000")))
    raw = b'[{"Plan":{"Total Cost":1000.0000000000000000000001,"Plan Rows":1,"Plan Width":1}}]'
    _install_connection(monkeypatch, None, raw_plan=raw)

    assessment = PsycopgQueryCostPreflight("postgresql://synthetic").assess(
        _query(target),
        target,
    )

    assert assessment.rejection_codes == (QueryCostRejectionCode.INVALID,)
    assert assessment.total_cost is None


def test_cost_preflight_does_not_round_fractional_rows_to_an_integer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target()
    raw = b'[{"Plan":{"Total Cost":1,"Plan Rows":1.0000000000000000000001,"Plan Width":1}}]'
    _install_connection(monkeypatch, None, raw_plan=raw)

    assessment = PsycopgQueryCostPreflight("postgresql://synthetic").assess(
        _query(target),
        target,
    )

    assert assessment.rejection_codes == (QueryCostRejectionCode.INVALID,)
    assert assessment.estimated_root_rows is None


def test_cost_preflight_classifies_an_empty_json_value_as_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target()
    _install_connection(monkeypatch, None, raw_plan=b"")

    assessment = PsycopgQueryCostPreflight("postgresql://synthetic").assess(
        _query(target),
        target,
    )

    assert assessment.rejection_codes == (QueryCostRejectionCode.INVALID,)


def test_cost_preflight_invalid_payload_returns_no_raw_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target()
    private_plan = [{"Plan": {"Relation Name": "private_table"}}]
    _install_connection(monkeypatch, private_plan)

    assessment = PsycopgQueryCostPreflight("postgresql://synthetic").assess(
        _query(target),
        target,
    )

    assert assessment.rejection_codes == (QueryCostRejectionCode.INVALID,)
    assert "private_table" not in repr(assessment)
    assert "private_table" not in str(assessment.model_dump(mode="json"))


def test_cost_preflight_rejects_retargeted_database_before_explain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target()
    connection = _install_connection(monkeypatch, _plan())
    connection.cursor_value.results[1] = (
        "schemabridge_reader",
        True,
        "1s",
        _SERVER_ADDRESS,
        _SERVER_PORT,
        "tenant_b",
    )

    with pytest.raises(PostgresSourceIdentityMismatchError):
        PsycopgQueryCostPreflight("postgresql://synthetic").assess(
            _query(target),
            target,
        )

    assert connection.rollbacks == 1
    assert all(
        _EXPLAIN_PREFIX not in statement for statement, _ in connection.cursor_value.executions
    )


@pytest.mark.parametrize(
    ("database_error", "expected"),
    (
        (psycopg.errors.QueryCanceled("synthetic timeout"), QueryCostRejectionCode.TIMEOUT),
        (psycopg.OperationalError("synthetic outage"), QueryCostRejectionCode.UNAVAILABLE),
        (psycopg.errors.SyntaxError("synthetic invalid"), QueryCostRejectionCode.INVALID),
    ),
)
def test_cost_preflight_translates_database_failures_without_secret_leakage(
    monkeypatch: pytest.MonkeyPatch,
    database_error: psycopg.Error,
    expected: QueryCostRejectionCode,
) -> None:
    def fail_connect(*_args: object, **_kwargs: object) -> Any:
        raise database_error

    monkeypatch.setattr(
        "schemabridge.adapters.postgres.cost_preflight.psycopg.connect",
        fail_connect,
    )
    target = _target()
    dsn = "postgresql://reader:secret@private.example/source"
    preflight = PsycopgQueryCostPreflight(dsn)

    assessment = preflight.assess(_query(target), target)

    assert assessment.rejection_codes == (expected,)
    assert dsn not in repr(preflight)
    assert "private.example" not in repr(assessment)
