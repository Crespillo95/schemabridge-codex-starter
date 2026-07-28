"""Unit tests for deterministic PostgreSQL compilation."""

from __future__ import annotations

import pytest
from sqlglot import exp, parse

from schemabridge.adapters.sql.compiler import PostgresQueryCompiler
from schemabridge.application.query_demo import build_north_star_query_plan
from schemabridge.application.query_execution import QueryCompilationError
from schemabridge.domain.plans import (
    FilterPredicate,
    MappedExpression,
    OrderItem,
    OutputAlias,
    ParameterValue,
    QueryPlan,
    SelectItem,
)
from schemabridge.domain.requests import FilterOperator, SortDirection
from schemabridge.domain.transformations import ParseDateStep, TransformationPlan


def test_compiler_emits_one_parameterized_select_with_default_preview_limit() -> None:
    plan = build_north_star_query_plan()
    compiler = PostgresQueryCompiler()

    first = compiler.compile(plan, max_preview_rows=500)
    second = compiler.compile(plan, max_preview_rows=500)
    statements = [statement for statement in parse(first.sql, read="postgres") if statement]

    assert first == second
    assert len(statements) == 1
    assert isinstance(statements[0], exp.Select)
    assert first.sql.count("%s") == len(first.parameters) == 11
    assert first.effective_limit == 500
    assert "LIMIT 500" in first.sql
    assert "COUNT(" in first.sql and "DISTINCT" in first.sql
    assert "DROP" not in first.sql


def test_compiler_binds_malicious_filter_value_without_interpolation() -> None:
    plan = build_north_star_query_plan()
    attack = "SECONDARY'); DROP TABLE crm.customers; --"
    original_filter = plan.filters[0]
    malicious_filter = FilterPredicate(
        expression=original_filter.expression,
        operator=FilterOperator.EQUALS,
        values=(ParameterValue(value=attack),),
    )
    malicious_plan = plan.model_copy(update={"filters": (malicious_filter,)})

    compiled = PostgresQueryCompiler().compile(malicious_plan, max_preview_rows=500)

    assert attack not in compiled.sql
    assert compiled.parameters[-1] == attack
    assert compiled.sql.count("%s") == len(compiled.parameters)


def test_compiler_caps_requested_limit_to_preview_policy() -> None:
    plan = build_north_star_query_plan().model_copy(update={"limit": 1_000})

    compiled = PostgresQueryCompiler().compile(plan, max_preview_rows=25)

    assert compiled.effective_limit == 25
    assert compiled.sql.endswith("LIMIT 25")


def test_compiler_rejects_invalid_preview_policy() -> None:
    with pytest.raises(QueryCompilationError) as captured:
        PostgresQueryCompiler().compile(
            build_north_star_query_plan(),
            max_preview_rows=0,
        )

    assert captured.value.code == "invalid_preview_limit"


def test_compiler_parameterizes_the_closed_parse_date_format() -> None:
    plan = build_north_star_query_plan()
    original_filter = plan.filters[0]
    assert isinstance(original_filter.expression, MappedExpression)
    unsupported = MappedExpression(
        source=original_filter.expression.source,
        transformation_plan=TransformationPlan(
            steps=(ParseDateStep(format="%Y-%m-%d"),),
        ),
    )
    unsupported_plan = plan.model_copy(
        update={
            "filters": (
                FilterPredicate(
                    expression=unsupported,
                    operator=FilterOperator.EQUALS,
                    values=(ParameterValue(value="2026-01-01"),),
                ),
            )
        }
    )

    compiled = PostgresQueryCompiler().compile(unsupported_plan, max_preview_rows=500)

    assert "TO_DATE" in compiled.sql
    assert "%Y-%m-%d" not in compiled.sql
    assert compiled.parameters[-2:] == ("%Y-%m-%d", "2026-01-01")


def test_compiler_groups_parameterized_dimension_by_projection_position() -> None:
    original = build_north_star_query_plan()
    mapped_role = original.filters[0].expression
    assert isinstance(mapped_role, MappedExpression)
    plan = QueryPlan(
        root_scan=original.root_scan,
        joins=original.joins,
        projections=(
            SelectItem(expression=mapped_role, alias=OutputAlias("holder_role")),
            original.projections[1],
        ),
        group_by=(mapped_role,),
        order_by=(OrderItem(expression=mapped_role, direction=SortDirection.ASC),),
        limit=original.limit,
    )

    compiled = PostgresQueryCompiler().compile(plan, max_preview_rows=500)

    assert compiled.sql.count("%s") == len(compiled.parameters) == 10
    assert "GROUP BY\n  1" in compiled.sql
    assert "ORDER BY\n  1" in compiled.sql
