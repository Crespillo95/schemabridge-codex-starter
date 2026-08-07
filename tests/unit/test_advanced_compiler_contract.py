"""Contract tests for deterministic version-2 PostgreSQL compilation."""

from __future__ import annotations

import pytest
from sqlglot import exp, parse
from tests.m32_advanced_support import build_m32_reference_plan

from schemabridge.adapters.sql.compiler import PostgresQueryCompiler
from schemabridge.domain.advanced_plans import AdvancedQueryPlan
from schemabridge.domain.advanced_requests import WindowOperation

BASE_ALIASES = (
    "month",
    "category",
    "net_revenue",
    "units",
    "distinct_orders",
)
WINDOW_ALIASES = (
    "revenue_rank",
    "revenue_percent",
    "cumulative_revenue",
)


def test_reference_plan_compiles_to_exact_bounded_stage_topology() -> None:
    plan = build_m32_reference_plan()

    first = PostgresQueryCompiler().compile(plan, max_preview_rows=500)
    second = PostgresQueryCompiler().compile(plan, max_preview_rows=500)
    statement = _single_select(first.sql)
    with_clause = statement.args.get("with_")

    assert isinstance(plan, AdvancedQueryPlan)
    assert first == second
    assert first.plan_version == 2
    assert first.parameters == ("COMPLETED", 4, 3)
    assert first.effective_limit == 100
    assert with_clause is not None
    assert with_clause.args.get("recursive") is not True
    assert tuple(cte.alias_or_name for cte in with_clause.expressions) == (
        "aggregated",
        "windowed",
    )
    assert len(tuple(statement.find_all(exp.Select))) == 3
    assert not tuple(statement.find_all(exp.Star))

    aggregated = with_clause.expressions[0].this
    windowed = with_clause.expressions[1].this
    assert isinstance(aggregated, exp.Select)
    assert isinstance(windowed, exp.Select)
    assert _projection_aliases(aggregated) == BASE_ALIASES
    assert _projection_aliases(windowed) == (*BASE_ALIASES, *WINDOW_ALIASES)
    assert _projection_aliases(statement) == (*BASE_ALIASES, *WINDOW_ALIASES)
    assert aggregated.args.get("where") is not None
    assert aggregated.args.get("having") is not None
    assert len(tuple(aggregated.find_all(exp.Count))) == 2
    assert len(tuple(aggregated.find_all(exp.Distinct))) == 2
    assert windowed.args.get("where") is None
    assert statement.args.get("where") is not None
    assert statement.args.get("order") is not None
    assert statement.args["limit"].expression.this == "100"


def test_reference_windows_preserve_eligible_population_and_deterministic_ties() -> None:
    compiled = PostgresQueryCompiler().compile(
        build_m32_reference_plan(),
        max_preview_rows=500,
    )
    statement = _single_select(compiled.sql)
    with_clause = statement.args["with_"]
    aggregated = with_clause.expressions[0].this
    windowed = with_clause.expressions[1].this
    windows_by_alias = {
        projection.alias_or_name: projection.this
        for projection in windowed.expressions
        if projection.alias_or_name in WINDOW_ALIASES
    }

    rank = windows_by_alias["revenue_rank"]
    percentage = windows_by_alias["revenue_percent"]
    cumulative = windows_by_alias["cumulative_revenue"]

    assert isinstance(aggregated.args.get("having"), exp.Having)
    assert isinstance(rank, exp.Window)
    assert isinstance(rank.this, exp.RowNumber)
    assert _window_partition_names(rank) == ("month",)
    assert _window_order(rank) == (
        ("net_revenue", True),
        ("category", False),
    )

    percentage_windows = tuple(percentage.find_all(exp.Window))
    assert len(percentage_windows) == 1
    assert isinstance(percentage_windows[0].this, exp.Sum)
    assert _window_partition_names(percentage_windows[0]) == ("month",)
    assert percentage_windows[0].args.get("order") is None
    assert tuple(percentage.find_all(exp.Nullif))
    assert any(
        literal.this in {"100", "100.0"}
        for literal in percentage.find_all(exp.Literal)
        if not literal.is_string
    )

    assert isinstance(cumulative, exp.Window)
    assert isinstance(cumulative.this, exp.Sum)
    assert _window_partition_names(cumulative) == ("month",)
    assert _window_order(cumulative) == (
        ("net_revenue", True),
        ("category", False),
    )
    frame = cumulative.args.get("spec")
    assert isinstance(frame, exp.WindowSpec)
    assert frame.args["start"] == "UNBOUNDED"
    assert frame.args["start_side"] == "PRECEDING"
    assert frame.args["end"] == "CURRENT ROW"

    assert len(tuple(windowed.find_all(exp.Window))) == 3
    assert not any(tuple(projection.find_all(exp.Window)) for projection in statement.expressions)


@pytest.mark.parametrize(
    ("operation", "expected_type"),
    (
        (WindowOperation.RANK, exp.Rank),
        (WindowOperation.DENSE_RANK, exp.DenseRank),
    ),
)
def test_peer_ranking_compilation_does_not_invent_a_unique_tie_breaker(
    operation: WindowOperation,
    expected_type: type[exp.Expression],
) -> None:
    reference = build_m32_reference_plan()
    ranking = reference.windows[0]
    expression = ranking.expression.model_copy(
        update={
            "operation": operation,
            "order_by": (ranking.expression.order_by[0],),
        }
    )
    plan = reference.model_copy(
        update={
            "windows": (ranking.model_copy(update={"expression": expression}),),
        }
    )

    compiled = PostgresQueryCompiler().compile(plan, max_preview_rows=500)
    statement = _single_select(compiled.sql)
    windowed = statement.args["with_"].expressions[1].this
    peer_rank = next(
        projection.this
        for projection in windowed.expressions
        if projection.alias_or_name == "revenue_rank"
    )

    assert isinstance(peer_rank, exp.Window)
    assert isinstance(peer_rank.this, expected_type)
    assert _window_order(peer_rank) == (("net_revenue", True),)


def _single_select(sql: str) -> exp.Select:
    statements = tuple(statement for statement in parse(sql, read="postgres") if statement)
    assert len(statements) == 1
    statement = statements[0]
    assert isinstance(statement, exp.Select)
    return statement


def _projection_aliases(statement: exp.Select) -> tuple[str, ...]:
    return tuple(projection.alias_or_name for projection in statement.expressions)


def _window_partition_names(window: exp.Window) -> tuple[str, ...]:
    return tuple(expression.name for expression in window.args.get("partition_by") or ())


def _window_order(window: exp.Window) -> tuple[tuple[str, bool], ...]:
    order = window.args.get("order")
    assert isinstance(order, exp.Order)
    return tuple((item.this.name, bool(item.args.get("desc"))) for item in order.expressions)
