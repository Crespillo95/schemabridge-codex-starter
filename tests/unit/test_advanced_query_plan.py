"""Pure-domain tests for the staged version-2 restricted query plan."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from schemabridge.application.query_demo import build_north_star_query_plan
from schemabridge.domain.advanced_plans import (
    AdvancedAggregateExpression,
    AdvancedQueryPlan,
    AdvancedSelectItem,
    AggregateBooleanPredicate,
    AggregateFilterPredicate,
    BucketExpression,
    NumericBucketPlan,
    OutputBooleanPredicatePlan,
    OutputFilterPredicate,
    OutputOrderItem,
    PhysicalBooleanPredicate,
    WindowExpression,
    WindowSelectItem,
)
from schemabridge.domain.advanced_requests import (
    AdvancedMetricOperation,
    BooleanOperator,
    GroupingMode,
    WindowOperation,
)
from schemabridge.domain.fields import PhysicalDatasetRef, PhysicalFieldRef
from schemabridge.domain.plans import (
    ColumnExpression,
    DatasetScan,
    FilterPredicate,
    OutputAlias,
    ParameterValue,
    RelationAlias,
)
from schemabridge.domain.requests import FilterOperator, SortDirection

ORDERS = DatasetScan(
    dataset=PhysicalDatasetRef("sales.orders"),
    alias=RelationAlias("orders"),
)
ORDER_KEY = ColumnExpression(
    relation=ORDERS.alias,
    field=PhysicalFieldRef("sales.orders.order_key"),
)
ORDER_STATUS = ColumnExpression(
    relation=ORDERS.alias,
    field=PhysicalFieldRef("sales.orders.order_status"),
)
ORDER_AMOUNT = ColumnExpression(
    relation=ORDERS.alias,
    field=PhysicalFieldRef("sales.orders.order_amount"),
)


def _physical_leaf(
    *,
    value: object = "COMPLETED",
) -> PhysicalBooleanPredicate:
    return PhysicalBooleanPredicate(
        kind=BooleanOperator.COMPARISON,
        comparison=FilterPredicate(
            expression=ORDER_STATUS,
            operator=FilterOperator.EQUALS,
            values=(ParameterValue(value=value),),  # type: ignore[arg-type]
        ),
    )


def _output_leaf(
    alias: str,
    *,
    operator: FilterOperator = FilterOperator.LESS_THAN_OR_EQUAL,
    value: object = 3,
) -> OutputBooleanPredicatePlan:
    return OutputBooleanPredicatePlan(
        kind=BooleanOperator.COMPARISON,
        comparison=OutputFilterPredicate(
            alias=OutputAlias(alias),
            operator=operator,
            values=(ParameterValue(value=value),),  # type: ignore[arg-type]
        ),
    )


def test_minimal_row_plan_round_trips_without_raw_sql_or_cte_names() -> None:
    plan = AdvancedQueryPlan(
        root_scan=ORDERS,
        projections=(
            AdvancedSelectItem(
                expression=ORDER_KEY,
                alias=OutputAlias("order_key"),
            ),
        ),
        where=PhysicalBooleanPredicate(
            kind=BooleanOperator.OR,
            operands=(
                _physical_leaf(),
                _physical_leaf(value="SHIPPED"),
            ),
        ),
        result_order_by=(OutputOrderItem(alias=OutputAlias("order_key")),),
        limit=50,
    )
    encoded = json.dumps(plan.model_dump(mode="json"), sort_keys=True)

    assert AdvancedQueryPlan.model_validate(plan.model_dump(mode="json")) == plan
    assert plan.version == 2
    assert "raw_sql" not in encoded
    assert "cte_name" not in encoded


def test_count_rows_aggregate_having_window_and_final_stage_are_valid() -> None:
    row_count = AdvancedAggregateExpression(
        operation=AdvancedMetricOperation.COUNT_ROWS,
    )
    having = AggregateBooleanPredicate(
        kind=BooleanOperator.COMPARISON,
        comparison=AggregateFilterPredicate(
            expression=row_count,
            operator=FilterOperator.GREATER_THAN_OR_EQUAL,
            values=(ParameterValue(value=4),),
        ),
    )
    plan = AdvancedQueryPlan(
        root_scan=ORDERS,
        projections=(
            AdvancedSelectItem(
                expression=ORDER_STATUS,
                alias=OutputAlias("status"),
            ),
            AdvancedSelectItem(
                expression=row_count,
                alias=OutputAlias("row_count"),
            ),
        ),
        group_by=(ORDER_STATUS,),
        having=having,
        windows=(
            WindowSelectItem(
                expression=WindowExpression(
                    operation=WindowOperation.ROW_NUMBER,
                    order_by=(
                        OutputOrderItem(
                            alias=OutputAlias("row_count"),
                            direction=SortDirection.DESC,
                        ),
                        OutputOrderItem(alias=OutputAlias("status")),
                    ),
                ),
                alias=OutputAlias("row_number"),
            ),
        ),
        post_filter=_output_leaf("row_number"),
        result_order_by=(OutputOrderItem(alias=OutputAlias("row_number")),),
    )

    assert plan.projections[1].expression == row_count
    assert plan.having == having
    assert plan.windows[0].alias.root == "row_number"


def test_conditional_count_rows_is_a_typed_aggregate_not_a_sql_fragment() -> None:
    completed = AdvancedAggregateExpression(
        operation=AdvancedMetricOperation.COUNT_ROWS,
        condition=_physical_leaf(value="COMPLETED"),
    )
    shipped = AdvancedAggregateExpression(
        operation=AdvancedMetricOperation.COUNT_ROWS,
        condition=_physical_leaf(value="SHIPPED"),
    )

    plan = AdvancedQueryPlan(
        root_scan=ORDERS,
        projections=(
            AdvancedSelectItem(
                expression=completed,
                alias=OutputAlias("completed_rows"),
            ),
            AdvancedSelectItem(
                expression=shipped,
                alias=OutputAlias("shipped_rows"),
            ),
        ),
    )

    assert completed != shipped
    assert len(plan.projections) == 2


@pytest.mark.parametrize(
    "payload",
    [
        {
            "operation": "count_rows",
            "source": {
                "kind": "column",
                "relation": "orders",
                "field": "sales.orders.order_key",
            },
        },
        {"operation": "sum"},
    ],
)
def test_aggregate_source_shape_is_closed(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AdvancedAggregateExpression.model_validate(payload)


def test_bucket_expression_remains_closed_and_requires_bounded_ranges() -> None:
    bucket = BucketExpression(
        source=ORDER_AMOUNT,
        buckets=(
            NumericBucketPlan(
                label=ParameterValue(value="low"),
                upper=ParameterValue(value=100),
            ),
            NumericBucketPlan(
                label=ParameterValue(value="high"),
                lower=ParameterValue(value=100),
            ),
        ),
        else_value=ParameterValue(value="unknown"),
    )
    plan = AdvancedQueryPlan(
        root_scan=ORDERS,
        projections=(
            AdvancedSelectItem(
                expression=bucket,
                alias=OutputAlias("amount_band"),
            ),
        ),
    )

    assert plan.projections[0].expression == bucket

    with pytest.raises(ValidationError, match="at least one bound"):
        NumericBucketPlan(label=ParameterValue(value="invalid"))


def test_plan_rejects_having_that_does_not_reuse_projected_aggregate() -> None:
    projected = AdvancedAggregateExpression(
        operation=AdvancedMetricOperation.SUM,
        source=ORDER_AMOUNT,
    )
    unprojected = AdvancedAggregateExpression(
        operation=AdvancedMetricOperation.AVG,
        source=ORDER_AMOUNT,
    )

    with pytest.raises(ValidationError, match="exactly projected aggregate"):
        AdvancedQueryPlan(
            root_scan=ORDERS,
            projections=(
                AdvancedSelectItem(
                    expression=projected,
                    alias=OutputAlias("total_amount"),
                ),
            ),
            having=AggregateBooleanPredicate(
                kind=BooleanOperator.COMPARISON,
                comparison=AggregateFilterPredicate(
                    expression=unprojected,
                    operator=FilterOperator.GREATER_THAN,
                    values=(ParameterValue(value=100),),
                ),
            ),
        )


def test_window_stage_cannot_read_another_window_or_unknown_output() -> None:
    amount = AdvancedAggregateExpression(
        operation=AdvancedMetricOperation.SUM,
        source=ORDER_AMOUNT,
    )
    rank = WindowSelectItem(
        expression=WindowExpression(
            operation=WindowOperation.ROW_NUMBER,
            order_by=(
                OutputOrderItem(
                    alias=OutputAlias("total_amount"),
                    direction=SortDirection.DESC,
                ),
            ),
        ),
        alias=OutputAlias("amount_rank"),
    )
    reads_prior_window = WindowSelectItem(
        expression=WindowExpression(
            operation=WindowOperation.RUNNING_SUM,
            source=OutputAlias("amount_rank"),
            order_by=(OutputOrderItem(alias=OutputAlias("total_amount")),),
        ),
        alias=OutputAlias("invalid_running"),
    )

    with pytest.raises(ValidationError, match="unknown base output"):
        AdvancedQueryPlan(
            root_scan=ORDERS,
            projections=(
                AdvancedSelectItem(
                    expression=amount,
                    alias=OutputAlias("total_amount"),
                ),
            ),
            windows=(rank, reads_prior_window),
        )


def test_plan_boolean_trees_enforce_depth_leaf_and_operand_limits() -> None:
    leaf = _physical_leaf()
    allowed = PhysicalBooleanPredicate(
        kind=BooleanOperator.NOT,
        operands=(
            PhysicalBooleanPredicate(
                kind=BooleanOperator.OR,
                operands=(
                    leaf,
                    PhysicalBooleanPredicate(
                        kind=BooleanOperator.AND,
                        operands=(leaf, leaf),
                    ),
                ),
            ),
        ),
    )

    with pytest.raises(ValidationError, match="bounded complexity"):
        PhysicalBooleanPredicate(
            kind=BooleanOperator.NOT,
            operands=(allowed,),
        )

    groups = tuple(
        PhysicalBooleanPredicate(
            kind=BooleanOperator.AND,
            operands=tuple(leaf for _index in range(8)),
        )
        for _group in range(2)
    )
    with pytest.raises(ValidationError, match="bounded complexity"):
        PhysicalBooleanPredicate(
            kind=BooleanOperator.OR,
            operands=(*groups, leaf),
        )

    with pytest.raises(ValidationError):
        PhysicalBooleanPredicate(
            kind=BooleanOperator.OR,
            operands=tuple(leaf for _index in range(9)),
        )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "operation": "row_number",
            "source": "total_amount",
            "order_by": [{"alias": "total_amount"}],
        },
        {
            "operation": "ntile",
            "order_by": [{"alias": "total_amount"}],
        },
        {
            "operation": "moving_avg",
            "source": "total_amount",
            "order_by": [{"alias": "total_amount"}],
        },
    ],
)
def test_plan_rejects_forged_window_argument_shapes(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        WindowExpression.model_validate(payload)


def test_plan_rejects_a_fifth_window() -> None:
    aggregate = AdvancedAggregateExpression(
        operation=AdvancedMetricOperation.SUM,
        source=ORDER_AMOUNT,
    )
    windows = tuple(
        WindowSelectItem(
            expression=WindowExpression(
                operation=WindowOperation.ROW_NUMBER,
                order_by=(
                    OutputOrderItem(
                        alias=OutputAlias("total_amount"),
                        direction=SortDirection.DESC,
                    ),
                ),
            ),
            alias=OutputAlias(f"rank_{index}"),
        )
        for index in range(5)
    )

    with pytest.raises(ValidationError):
        AdvancedQueryPlan(
            root_scan=ORDERS,
            projections=(
                AdvancedSelectItem(
                    expression=aggregate,
                    alias=OutputAlias("total_amount"),
                ),
            ),
            windows=windows,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "post_filter",
            _output_leaf("missing"),
            "post-window predicate",
        ),
        (
            "result_order_by",
            (OutputOrderItem(alias=OutputAlias("missing")),),
            "final ordering",
        ),
    ],
)
def test_plan_rejects_aliases_outside_their_stage(
    field: str,
    value: object,
    message: str,
) -> None:
    payload: dict[str, object] = {
        "root_scan": ORDERS,
        "projections": (
            AdvancedSelectItem(
                expression=ORDER_KEY,
                alias=OutputAlias("order_key"),
            ),
        ),
        field: value,
    }

    with pytest.raises(ValidationError, match=message):
        AdvancedQueryPlan.model_validate(payload)


def test_row_plan_rejects_grouping_having_and_rollup() -> None:
    row_projection = AdvancedSelectItem(
        expression=ORDER_STATUS,
        alias=OutputAlias("status"),
    )

    with pytest.raises(ValidationError, match="row plans"):
        AdvancedQueryPlan(
            root_scan=ORDERS,
            projections=(row_projection,),
            group_by=(ORDER_STATUS,),
        )

    with pytest.raises(ValidationError, match="ROLLUP"):
        AdvancedQueryPlan(
            root_scan=ORDERS,
            projections=(row_projection,),
            grouping=GroupingMode.ROLLUP,
        )


def test_aggregate_plan_rejects_rollup_until_null_semantics_are_explicit() -> None:
    with pytest.raises(ValidationError, match="subtotal NULL semantics"):
        AdvancedQueryPlan(
            root_scan=ORDERS,
            projections=(
                AdvancedSelectItem(
                    expression=ORDER_STATUS,
                    alias=OutputAlias("status"),
                ),
                AdvancedSelectItem(
                    expression=AdvancedAggregateExpression(
                        operation=AdvancedMetricOperation.COUNT_ROWS,
                    ),
                    alias=OutputAlias("row_count"),
                ),
            ),
            group_by=(ORDER_STATUS,),
            grouping=GroupingMode.ROLLUP,
        )


def test_plan_forbids_user_named_cte_and_raw_sql_fields() -> None:
    payload = {
        "root_scan": {"dataset": "sales.orders", "alias": "orders"},
        "projections": [
            {
                "expression": {
                    "kind": "column",
                    "relation": "orders",
                    "field": "sales.orders.order_key",
                },
                "alias": "order_key",
            }
        ],
        "cte_name": "attacker_stage",
        "raw_sql": "SELECT * FROM sales.orders",
    }

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AdvancedQueryPlan.model_validate(payload)


def test_one_to_many_row_and_count_rows_plans_fail_without_explicit_grain() -> None:
    original = build_north_star_query_plan()
    row_projection = AdvancedSelectItem(
        expression=original.projections[0].expression,
        alias=OutputAlias("registration_date"),
    )

    with pytest.raises(ValidationError, match="one-to-many row queries"):
        AdvancedQueryPlan(
            root_scan=original.root_scan,
            joins=original.joins,
            projections=(row_projection,),
        )

    with pytest.raises(ValidationError, match=r"ambiguous.*one-to-many"):
        AdvancedQueryPlan(
            root_scan=original.root_scan,
            joins=original.joins,
            projections=(
                AdvancedSelectItem(
                    expression=AdvancedAggregateExpression(
                        operation=AdvancedMetricOperation.COUNT_ROWS,
                    ),
                    alias=OutputAlias("row_count"),
                ),
            ),
        )


def test_one_to_many_plain_count_fails_but_count_distinct_is_safe() -> None:
    original = build_north_star_query_plan()
    source = original.projections[1].expression.source  # type: ignore[union-attr]

    with pytest.raises(ValidationError, match="COUNT DISTINCT"):
        AdvancedQueryPlan(
            root_scan=original.root_scan,
            joins=original.joins,
            projections=(
                AdvancedSelectItem(
                    expression=AdvancedAggregateExpression(
                        operation=AdvancedMetricOperation.COUNT,
                        source=source,
                    ),
                    alias=OutputAlias("customer_rows"),
                ),
            ),
        )

    safe = AdvancedQueryPlan(
        root_scan=original.root_scan,
        joins=original.joins,
        projections=(
            AdvancedSelectItem(
                expression=AdvancedAggregateExpression(
                    operation=AdvancedMetricOperation.COUNT_DISTINCT,
                    source=source,
                ),
                alias=OutputAlias("customers"),
            ),
        ),
    )
    assert safe.projections[0].alias.root == "customers"
