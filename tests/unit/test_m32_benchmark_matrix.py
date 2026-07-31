"""M32 compiler/guard coverage for the supported advanced benchmark families."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from sqlglot import exp, parse_one
from tests.m32_advanced_support import (
    build_m32_reference_plan,
    build_m32_reference_policy,
)

from schemabridge.adapters.sql.compiler import PostgresQueryCompiler
from schemabridge.adapters.sql.guard import SqlGlotPolicyGuard
from schemabridge.application.query_demo import build_demo_query_policy
from schemabridge.application.query_execution import (
    CompiledQuery,
    SqlPolicyViolation,
    SqlRejectionCode,
)
from schemabridge.domain.advanced_plans import (
    AdvancedAggregateExpression,
    AdvancedQueryPlan,
    AdvancedSelectItem,
    BucketExpression,
    NumericBucketPlan,
    OutputOrderItem,
    PhysicalBooleanPredicate,
    WindowExpression,
    WindowSelectItem,
)
from schemabridge.domain.advanced_requests import (
    AdvancedMetricOperation,
    BooleanOperator,
    WindowOperation,
)
from schemabridge.domain.plans import (
    ColumnExpression,
    FilterPredicate,
    OutputAlias,
    ParameterScalar,
    ParameterValue,
)
from schemabridge.domain.requests import FilterOperator, SortDirection


@pytest.mark.parametrize("operation", tuple(WindowOperation))
def test_every_window_operation_compiles_and_passes_the_independent_guard(
    operation: WindowOperation,
) -> None:
    plan = _window_plan(operation)

    compiled = PostgresQueryCompiler().compile(plan, max_preview_rows=500)
    validated = SqlGlotPolicyGuard().validate(
        compiled,
        build_m32_reference_policy(),
    )
    statement = parse_one(compiled.sql, read="postgres")
    derived = _window_projection(statement, "derived_value").this
    windows = tuple(derived.find_all(exp.Window))

    assert validated.sql == compiled.sql
    assert compiled.plan_version == 2
    assert windows
    assert _window_function_types(operation) <= {type(window.this) for window in windows}
    _assert_operation_shape(operation, derived, windows)


@pytest.mark.parametrize("operation", tuple(AdvancedMetricOperation))
def test_every_conditional_aggregate_compiles_as_case_and_passes_guard(
    operation: AdvancedMetricOperation,
) -> None:
    plan = _conditional_aggregate_plan(operation)

    compiled = PostgresQueryCompiler().compile(plan, max_preview_rows=500)
    validated = SqlGlotPolicyGuard().validate(
        compiled,
        build_m32_reference_policy(),
    )
    statement = parse_one(compiled.sql, read="postgres")
    aggregate = _projection(statement, "conditional_metric").this

    assert validated.parameters == ("COMPLETED",)
    assert aggregate.find(exp.Case) is not None
    assert aggregate.find(exp.Filter) is None
    assert isinstance(aggregate, _aggregate_expression_type(operation))
    assert bool(aggregate.find(exp.Distinct)) is (
        operation is AdvancedMetricOperation.COUNT_DISTINCT
    )


def test_numeric_buckets_compile_as_bounded_case_and_pass_guard() -> None:
    reference = build_m32_reference_plan()
    amount = _column_projection(reference, "net_revenue")
    order_key = _column_projection(reference, "distinct_orders")
    bucket = BucketExpression(
        source=amount,
        buckets=(
            NumericBucketPlan(
                label=ParameterValue(value="low"),
                upper=ParameterValue(value=100),
            ),
            NumericBucketPlan(
                label=ParameterValue(value="medium"),
                lower=ParameterValue(value=100),
                upper=ParameterValue(value=500),
            ),
            NumericBucketPlan(
                label=ParameterValue(value="high"),
                lower=ParameterValue(value=500),
            ),
        ),
        else_value=ParameterValue(value="unknown"),
    )
    plan = AdvancedQueryPlan(
        root_scan=reference.root_scan,
        projections=(
            AdvancedSelectItem(
                expression=bucket,
                alias=OutputAlias("amount_band"),
            ),
            AdvancedSelectItem(
                expression=AdvancedAggregateExpression(
                    operation=AdvancedMetricOperation.COUNT_DISTINCT,
                    source=order_key,
                ),
                alias=OutputAlias("order_count"),
            ),
        ),
        group_by=(bucket,),
        result_order_by=(OutputOrderItem(alias=OutputAlias("amount_band")),),
        limit=100,
    )

    compiled = PostgresQueryCompiler().compile(plan, max_preview_rows=500)
    validated = SqlGlotPolicyGuard().validate(
        compiled,
        build_m32_reference_policy(),
    )
    statement = parse_one(compiled.sql, read="postgres")
    case = _projection(statement, "amount_band").this
    group = statement.args.get("group")

    assert validated.parameters == (
        100,
        "low",
        100,
        500,
        "medium",
        500,
        "high",
        "unknown",
    )
    assert isinstance(case, exp.Case)
    assert len(case.args["ifs"]) == 3
    assert isinstance(group, exp.Group)
    assert [item.this for item in group.expressions] == ["1"]


def test_three_percent_change_outputs_fit_the_semantic_and_ast_budgets() -> None:
    reference = build_m32_reference_plan()
    expression = _window_expression(WindowOperation.PERCENT_CHANGE_FROM_PREVIOUS)
    plan = AdvancedQueryPlan(
        root_scan=reference.root_scan,
        projections=reference.projections,
        group_by=reference.group_by,
        where=reference.where,
        having=reference.having,
        windows=tuple(
            WindowSelectItem(
                expression=expression,
                alias=OutputAlias(f"percent_change_{position}"),
            )
            for position in range(1, 4)
        ),
        result_order_by=(OutputOrderItem(alias=OutputAlias("month")),),
        limit=100,
    )

    compiled = PostgresQueryCompiler().compile(plan, max_preview_rows=500)
    validated = SqlGlotPolicyGuard().validate(
        compiled,
        build_m32_reference_policy(),
    )
    statement = parse_one(compiled.sql, read="postgres")

    assert validated.plan_version == 2
    assert len(tuple(statement.find_all(exp.Window))) == 6


@pytest.mark.parametrize(
    ("sql", "expected_code"),
    (
        pytest.param(
            """
            SELECT
                left_customer.customer_id AS left_customer_id,
                right_customer.customer_id AS right_customer_id
            FROM crm.customers AS left_customer
            INNER JOIN crm.customers AS right_customer
                ON left_customer.customer_id = right_customer.customer_id
            LIMIT 10
            """,
            SqlRejectionCode.REPEATED_ASSET,
            id="self-join",
        ),
        pytest.param(
            """
            SELECT
                customer.customer_id AS customer_id,
                holder.gf_customer_id AS holder_customer_id
            FROM crm.customers AS customer
            CROSS JOIN bank.account_holders AS holder
            LIMIT 10
            """,
            SqlRejectionCode.CARTESIAN_JOIN,
            id="cross-join",
        ),
    ),
)
def test_guard_rejects_self_and_cross_joins(
    sql: str,
    expected_code: SqlRejectionCode,
) -> None:
    with pytest.raises(SqlPolicyViolation) as captured:
        _guard_demo_sql(sql)

    assert captured.value.findings[0].code is expected_code


@pytest.mark.parametrize("operator", ("INTERSECT", "EXCEPT"))
def test_guard_rejects_each_remaining_set_operation(operator: str) -> None:
    sql = f"""
        WITH combined AS (
            SELECT customer.customer_id AS entity_id
            FROM crm.customers AS customer
            {operator}
            SELECT client.client_no AS entity_id
            FROM legacy.client_master AS client
        )
        SELECT combined.entity_id AS entity_id
        FROM combined
        LIMIT 10
    """

    with pytest.raises(SqlPolicyViolation) as captured:
        _guard_demo_sql(sql)

    assert captured.value.findings[0].code is SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE


def _window_plan(operation: WindowOperation) -> AdvancedQueryPlan:
    reference = build_m32_reference_plan()
    expression = _window_expression(operation)
    return AdvancedQueryPlan(
        root_scan=reference.root_scan,
        projections=reference.projections,
        group_by=reference.group_by,
        where=reference.where,
        having=reference.having,
        windows=(
            WindowSelectItem(
                expression=expression,
                alias=OutputAlias("derived_value"),
            ),
        ),
        result_order_by=(
            OutputOrderItem(alias=OutputAlias("month")),
            OutputOrderItem(alias=OutputAlias("category")),
        ),
        limit=100,
    )


def _window_expression(operation: WindowOperation) -> WindowExpression:
    order_by = (
        OutputOrderItem(
            alias=OutputAlias("net_revenue"),
            direction=SortDirection.DESC,
        ),
        OutputOrderItem(alias=OutputAlias("category")),
    )
    common: dict[str, object] = {
        "operation": operation,
        "partition_by": (OutputAlias("month"),),
    }
    if operation in {
        WindowOperation.ROW_NUMBER,
        WindowOperation.RANK,
        WindowOperation.DENSE_RANK,
    }:
        return WindowExpression(**common, order_by=order_by)
    if operation is WindowOperation.NTILE:
        return WindowExpression(**common, order_by=order_by, buckets=4)
    if operation in {
        WindowOperation.RUNNING_SUM,
        WindowOperation.RUNNING_AVG,
    }:
        return WindowExpression(
            **common,
            source=OutputAlias("net_revenue"),
            order_by=order_by,
        )
    if operation in {
        WindowOperation.MOVING_SUM,
        WindowOperation.MOVING_AVG,
    }:
        return WindowExpression(
            **common,
            source=OutputAlias("net_revenue"),
            order_by=order_by,
            preceding_rows=2,
        )
    if operation in {
        WindowOperation.LAG,
        WindowOperation.LEAD,
        WindowOperation.DELTA_FROM_PREVIOUS,
        WindowOperation.PERCENT_CHANGE_FROM_PREVIOUS,
    }:
        return WindowExpression(
            **common,
            source=OutputAlias("net_revenue"),
            order_by=order_by,
            offset=1,
        )
    return WindowExpression(
        **common,
        source=OutputAlias("net_revenue"),
    )


def _conditional_aggregate_plan(
    operation: AdvancedMetricOperation,
) -> AdvancedQueryPlan:
    reference = build_m32_reference_plan()
    category = _column_projection(reference, "category")
    source = (
        None
        if operation is AdvancedMetricOperation.COUNT_ROWS
        else _column_projection(
            reference,
            "distinct_orders"
            if operation is AdvancedMetricOperation.COUNT_DISTINCT
            else "net_revenue",
        )
    )
    condition = PhysicalBooleanPredicate(
        kind=BooleanOperator.COMPARISON,
        comparison=FilterPredicate(
            expression=_filter_column(reference),
            operator=FilterOperator.EQUALS,
            values=(ParameterValue(value="COMPLETED"),),
        ),
    )
    return AdvancedQueryPlan(
        root_scan=reference.root_scan,
        projections=(
            AdvancedSelectItem(
                expression=category,
                alias=OutputAlias("category"),
            ),
            AdvancedSelectItem(
                expression=AdvancedAggregateExpression(
                    operation=operation,
                    source=source,
                    condition=condition,
                ),
                alias=OutputAlias("conditional_metric"),
            ),
        ),
        group_by=(category,),
        result_order_by=(OutputOrderItem(alias=OutputAlias("category")),),
        limit=100,
    )


def _column_projection(
    plan: AdvancedQueryPlan,
    alias: str,
) -> ColumnExpression:
    expression = next(item.expression for item in plan.projections if item.alias.root == alias)
    if isinstance(expression, AdvancedAggregateExpression):
        assert isinstance(expression.source, ColumnExpression)
        return expression.source
    assert isinstance(expression, ColumnExpression)
    return expression


def _filter_column(plan: AdvancedQueryPlan) -> ColumnExpression:
    assert plan.where is not None
    assert plan.where.comparison is not None
    expression = plan.where.comparison.expression
    assert isinstance(expression, ColumnExpression)
    return expression


def _projection(statement: exp.Expression, alias: str) -> exp.Alias:
    projection = next(
        item
        for select in statement.find_all(exp.Select)
        for item in select.expressions
        if item.alias_or_name == alias
    )
    assert isinstance(projection, exp.Alias)
    return projection


def _window_projection(statement: exp.Expression, alias: str) -> exp.Alias:
    projection = next(
        item
        for select in statement.find_all(exp.Select)
        for item in select.expressions
        if item.alias_or_name == alias and item.find(exp.Window) is not None
    )
    assert isinstance(projection, exp.Alias)
    return projection


def _window_function_types(
    operation: WindowOperation,
) -> set[type[exp.Expression]]:
    if operation is WindowOperation.ROW_NUMBER:
        return {exp.RowNumber}
    if operation is WindowOperation.RANK:
        return {exp.Rank}
    if operation is WindowOperation.DENSE_RANK:
        return {exp.DenseRank}
    if operation is WindowOperation.NTILE:
        return {exp.Ntile}
    if operation in {WindowOperation.LAG, WindowOperation.LEAD}:
        return {exp.Lag if operation is WindowOperation.LAG else exp.Lead}
    if operation in {
        WindowOperation.DELTA_FROM_PREVIOUS,
        WindowOperation.PERCENT_CHANGE_FROM_PREVIOUS,
    }:
        return {exp.Lag}
    if operation in {
        WindowOperation.RUNNING_AVG,
        WindowOperation.MOVING_AVG,
        WindowOperation.PARTITION_AVG,
    }:
        return {exp.Avg}
    return {exp.Sum}


def _assert_operation_shape(
    operation: WindowOperation,
    derived: exp.Expression,
    windows: tuple[exp.Window, ...],
) -> None:
    if operation is WindowOperation.NTILE:
        assert windows[0].this.this.this == "4"
    if operation in {
        WindowOperation.RUNNING_SUM,
        WindowOperation.RUNNING_AVG,
        WindowOperation.MOVING_SUM,
        WindowOperation.MOVING_AVG,
    }:
        frame = windows[0].args.get("spec")
        assert isinstance(frame, exp.WindowSpec)
        assert frame.args["end"] == "CURRENT ROW"
        start = frame.args["start"]
        if operation in {
            WindowOperation.RUNNING_SUM,
            WindowOperation.RUNNING_AVG,
        }:
            assert start == "UNBOUNDED"
        else:
            assert isinstance(start, exp.Literal)
            assert start.this == "2"
    if operation in {WindowOperation.PARTITION_AVG, WindowOperation.PERCENT_OF_TOTAL}:
        assert windows[0].args.get("order") is None
    if operation in {
        WindowOperation.LAG,
        WindowOperation.LEAD,
        WindowOperation.DELTA_FROM_PREVIOUS,
        WindowOperation.PERCENT_CHANGE_FROM_PREVIOUS,
    }:
        assert all(window.this.args["offset"].this == "1" for window in windows)
    if operation is WindowOperation.DELTA_FROM_PREVIOUS:
        assert isinstance(derived, exp.Sub)
    if operation is WindowOperation.PERCENT_CHANGE_FROM_PREVIOUS:
        assert isinstance(derived, exp.Div)
        assert derived.find(exp.Mul) is not None
        assert derived.find(exp.Nullif) is not None
    if operation is WindowOperation.PERCENT_OF_TOTAL:
        assert isinstance(derived, exp.Round)
        assert derived.find(exp.Nullif) is not None


def _aggregate_expression_type(
    operation: AdvancedMetricOperation,
) -> type[exp.Expression]:
    if operation in {
        AdvancedMetricOperation.COUNT_ROWS,
        AdvancedMetricOperation.COUNT,
        AdvancedMetricOperation.COUNT_DISTINCT,
    }:
        return exp.Count
    return {
        AdvancedMetricOperation.SUM: exp.Sum,
        AdvancedMetricOperation.AVG: exp.Avg,
        AdvancedMetricOperation.MIN: exp.Min,
        AdvancedMetricOperation.MAX: exp.Max,
    }[operation]


def _guard_demo_sql(
    sql: str,
    parameters: Sequence[ParameterScalar] = (),
) -> None:
    SqlGlotPolicyGuard().validate(
        CompiledQuery(
            sql=sql,
            parameters=tuple(parameters),
            effective_limit=10,
        ),
        build_demo_query_policy(max_preview_rows=10),
    )
