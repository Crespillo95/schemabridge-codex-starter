"""Pure-domain tests for the bounded version-2 analytical request."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from schemabridge.domain.advanced_requests import (
    AdvancedAnalyticalRequest,
    AdvancedField,
    AdvancedMetric,
    AdvancedMetricOperation,
    AdvancedQueryMode,
    BooleanOperator,
    GroupingMode,
    LogicalBooleanPredicate,
    NumericBucket,
    OutputBooleanPredicate,
    OutputFilter,
    OutputOrder,
    WindowCalculation,
    WindowOperation,
    predicate_complexity,
    request_output_aliases,
)
from schemabridge.domain.concepts import LogicalFieldRef, LogicalModelRef
from schemabridge.domain.requests import (
    DateGrain,
    Filter,
    FilterOperator,
    SortDirection,
)


def _logical_leaf(
    field: str = "SalesOrder.order_status",
    *,
    operator: FilterOperator = FilterOperator.EQUALS,
    value: object = "COMPLETED",
) -> LogicalBooleanPredicate:
    return LogicalBooleanPredicate.leaf(
        Filter(
            field=LogicalFieldRef(field),
            operator=operator,
            value=value,  # type: ignore[arg-type]
        )
    )


def _output_leaf(
    alias: str,
    *,
    operator: FilterOperator = FilterOperator.GREATER_THAN_OR_EQUAL,
    value: object = 1,
    compare_to_alias: str | None = None,
) -> OutputBooleanPredicate:
    return OutputBooleanPredicate.leaf(
        OutputFilter(
            alias=alias,
            operator=operator,
            value=value,  # type: ignore[arg-type]
            compare_to_alias=compare_to_alias,
        )
    )


def test_row_mode_supports_selected_rows_and_closed_ranking() -> None:
    request = AdvancedAnalyticalRequest(
        version=2,
        mode=AdvancedQueryMode.ROWS,
        primary_entity=LogicalModelRef("SalesOrder"),
        fields=(
            AdvancedField(
                field=LogicalFieldRef("SalesOrder.order_key"),
                alias="order_key",
            ),
            AdvancedField(
                field=LogicalFieldRef("SalesOrder.ordered_at"),
                alias="ordered_at",
            ),
        ),
        where=LogicalBooleanPredicate.any_of(
            _logical_leaf(),
            _logical_leaf(value="SHIPPED"),
        ),
        windows=(
            WindowCalculation(
                operation=WindowOperation.ROW_NUMBER,
                alias="row_number",
                order_by=(
                    OutputOrder(alias="ordered_at", direction=SortDirection.DESC),
                    OutputOrder(alias="order_key"),
                ),
            ),
        ),
        post_filter=_output_leaf(
            "row_number",
            operator=FilterOperator.LESS_THAN_OR_EQUAL,
            value=25,
        ),
        result_order_by=(OutputOrder(alias="row_number"),),
        limit=25,
    )

    assert request.mode is AdvancedQueryMode.ROWS
    assert request.metrics == ()
    assert predicate_complexity(request.where) == (2, 2)  # type: ignore[arg-type]
    assert request_output_aliases(request) == (
        "order_key",
        "ordered_at",
        "row_number",
    )
    assert AdvancedAnalyticalRequest.model_validate(request.model_dump(mode="json")) == request


def test_version_two_in_filters_accept_sixty_four_values_and_reject_sixty_five() -> None:
    accepted_values = tuple(range(64))

    logical = _logical_leaf(
        field="Product.unit_price",
        operator=FilterOperator.IN,
        value=accepted_values,
    )
    output = _output_leaf(
        "row_count",
        operator=FilterOperator.IN,
        value=accepted_values,
    )

    assert logical.comparison is not None
    assert logical.comparison.value == accepted_values
    assert output.comparison is not None
    assert output.comparison.value == accepted_values
    with pytest.raises(ValidationError, match="sixty-four"):
        _logical_leaf(
            field="Product.unit_price",
            operator=FilterOperator.IN,
            value=tuple(range(65)),
        )
    with pytest.raises(ValidationError, match="sixty-four"):
        _output_leaf(
            "row_count",
            operator=FilterOperator.IN,
            value=tuple(range(65)),
        )


def test_aggregate_mode_supports_count_rows_having_and_windows() -> None:
    request = AdvancedAnalyticalRequest(
        version=2,
        mode=AdvancedQueryMode.AGGREGATE,
        primary_entity=LogicalModelRef("SaleLine"),
        fields=(
            AdvancedField(
                field=LogicalFieldRef("SalesOrder.ordered_at"),
                grain=DateGrain.MONTH,
                alias="month",
            ),
            AdvancedField(
                field=LogicalFieldRef("Product.category"),
                alias="category",
            ),
        ),
        metrics=(
            AdvancedMetric(
                operation=AdvancedMetricOperation.COUNT_ROWS,
                alias="row_count",
            ),
            AdvancedMetric(
                operation=AdvancedMetricOperation.SUM,
                field=LogicalFieldRef("SaleLine.net_amount"),
                alias="net_revenue",
            ),
        ),
        group_by=("month", "category"),
        where=_logical_leaf(),
        having=_output_leaf("row_count", value=4),
        windows=(
            WindowCalculation(
                operation=WindowOperation.ROW_NUMBER,
                alias="revenue_rank",
                partition_by=("month",),
                order_by=(
                    OutputOrder(alias="net_revenue", direction=SortDirection.DESC),
                    OutputOrder(alias="category"),
                ),
            ),
            WindowCalculation(
                operation=WindowOperation.RUNNING_SUM,
                alias="cumulative_revenue",
                source="net_revenue",
                partition_by=("month",),
                order_by=(
                    OutputOrder(alias="net_revenue", direction=SortDirection.DESC),
                    OutputOrder(alias="category"),
                ),
            ),
        ),
        post_filter=_output_leaf(
            "revenue_rank",
            operator=FilterOperator.LESS_THAN_OR_EQUAL,
            value=3,
        ),
        result_order_by=(
            OutputOrder(alias="month"),
            OutputOrder(alias="revenue_rank"),
        ),
        grouping=GroupingMode.STANDARD,
    )

    assert request.metrics[0].field is None
    assert request.grouping is GroupingMode.STANDARD
    assert request_output_aliases(request) == (
        "month",
        "category",
        "row_count",
        "net_revenue",
        "revenue_rank",
        "cumulative_revenue",
    )


def test_request_rejects_rollup_until_subtotal_null_semantics_are_explicit() -> None:
    with pytest.raises(ValidationError, match="subtotal NULL semantics"):
        AdvancedAnalyticalRequest(
            version=2,
            mode=AdvancedQueryMode.AGGREGATE,
            primary_entity=LogicalModelRef("SaleLine"),
            fields=(
                AdvancedField(
                    field=LogicalFieldRef("Product.category"),
                    alias="category",
                ),
                AdvancedField(
                    field=LogicalFieldRef("SalesOrder.status"),
                    alias="status",
                ),
            ),
            metrics=(
                AdvancedMetric(
                    operation=AdvancedMetricOperation.COUNT_ROWS,
                    alias="row_count",
                ),
            ),
            group_by=("category", "status"),
            grouping=GroupingMode.ROLLUP,
        )


def test_conditional_count_rows_metrics_are_distinguished_by_alias_and_condition() -> None:
    request = AdvancedAnalyticalRequest(
        version=2,
        mode=AdvancedQueryMode.AGGREGATE,
        primary_entity=LogicalModelRef("SalesOrder"),
        metrics=(
            AdvancedMetric(
                operation=AdvancedMetricOperation.COUNT_ROWS,
                alias="completed_rows",
                condition=_logical_leaf(value="COMPLETED"),
            ),
            AdvancedMetric(
                operation=AdvancedMetricOperation.COUNT_ROWS,
                alias="shipped_rows",
                condition=_logical_leaf(value="SHIPPED"),
            ),
        ),
    )

    assert [metric.alias for metric in request.metrics] == [
        "completed_rows",
        "shipped_rows",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {
            "operation": "count_rows",
            "field": "SalesOrder.order_key",
            "alias": "row_count",
        },
        {"operation": "sum", "alias": "total"},
    ],
)
def test_metric_source_shape_is_closed(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AdvancedMetric.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "version": 2,
            "mode": "rows",
            "primary_entity": "SalesOrder",
            "fields": [{"field": "SalesOrder.order_key", "alias": "order_key"}],
            "metrics": [{"operation": "count_rows", "alias": "row_count"}],
        },
        {
            "version": 2,
            "mode": "aggregate",
            "primary_entity": "SalesOrder",
            "fields": [{"field": "SalesOrder.order_status", "alias": "status"}],
            "metrics": [{"operation": "count_rows", "alias": "row_count"}],
            "group_by": [],
        },
        {
            "version": 2,
            "mode": "aggregate",
            "primary_entity": "SalesOrder",
            "fields": [{"field": "SalesOrder.order_status", "alias": "status"}],
            "metrics": [],
            "group_by": ["status"],
        },
    ],
)
def test_rows_and_aggregate_modes_reject_crossed_stage_shapes(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        AdvancedAnalyticalRequest.model_validate(payload)


def test_boolean_trees_enforce_shape_depth_leaf_and_sql_value_limits() -> None:
    leaf = _logical_leaf()
    allowed = LogicalBooleanPredicate.negate(
        LogicalBooleanPredicate.any_of(
            leaf,
            LogicalBooleanPredicate.all_of(leaf, leaf),
        )
    )
    assert predicate_complexity(allowed) == (4, 3)

    with pytest.raises(ValidationError, match="four-level"):
        LogicalBooleanPredicate.negate(allowed)

    groups = tuple(LogicalBooleanPredicate.all_of(*([leaf] * size)) for size in (8, 8))
    with pytest.raises(ValidationError, match="sixteen-leaf"):
        LogicalBooleanPredicate.all_of(*groups, leaf)

    with pytest.raises(ValidationError, match="SQL-like"):
        _logical_leaf(value="COMPLETED'; DROP TABLE sales.orders; --")

    with pytest.raises(ValidationError, match="at least two"):
        LogicalBooleanPredicate(
            kind=BooleanOperator.AND,
            operands=(leaf,),
        )


@pytest.mark.parametrize(
    "window",
    [
        {
            "operation": "row_number",
            "alias": "ranking",
            "source": "amount",
            "order_by": [{"alias": "amount", "direction": "desc"}],
        },
        {
            "operation": "ntile",
            "alias": "quartile",
            "order_by": [{"alias": "amount", "direction": "desc"}],
        },
        {
            "operation": "moving_avg",
            "alias": "moving_amount",
            "source": "amount",
            "order_by": [{"alias": "month"}],
        },
        {
            "operation": "lag",
            "alias": "previous_amount",
            "source": "amount",
            "order_by": [{"alias": "month"}],
        },
        {
            "operation": "percent_of_total",
            "alias": "share",
            "source": "amount",
            "order_by": [{"alias": "month"}],
        },
    ],
)
def test_window_operation_arguments_are_closed(window: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        WindowCalculation.model_validate(window)


def test_request_rejects_a_fifth_window() -> None:
    windows = tuple(
        WindowCalculation(
            operation=WindowOperation.ROW_NUMBER,
            alias=f"rank_{index}",
            order_by=(OutputOrder(alias="order_key"),),
        )
        for index in range(5)
    )

    with pytest.raises(ValidationError):
        AdvancedAnalyticalRequest(
            version=2,
            mode=AdvancedQueryMode.ROWS,
            primary_entity=LogicalModelRef("SalesOrder"),
            fields=(
                AdvancedField(
                    field=LogicalFieldRef("SalesOrder.order_key"),
                    alias="order_key",
                ),
            ),
            windows=windows,
        )


def test_request_rejects_aliases_outside_their_evaluation_stage() -> None:
    base = {
        "version": 2,
        "mode": "aggregate",
        "primary_entity": "SaleLine",
        "fields": [{"field": "Product.category", "alias": "category"}],
        "metrics": [
            {
                "operation": "sum",
                "field": "SaleLine.net_amount",
                "alias": "net_revenue",
            }
        ],
        "group_by": ["category"],
    }

    with pytest.raises(ValidationError, match="window partition/order"):
        AdvancedAnalyticalRequest.model_validate(
            {
                **base,
                "windows": [
                    {
                        "operation": "row_number",
                        "alias": "rank",
                        "order_by": [{"alias": "missing", "direction": "desc"}],
                    }
                ],
            }
        )

    with pytest.raises(ValidationError, match="HAVING"):
        AdvancedAnalyticalRequest.model_validate(
            {
                **base,
                "having": {
                    "kind": "comparison",
                    "comparison": {
                        "alias": "category",
                        "operator": "equals",
                        "value": "BOOKS",
                    },
                },
            }
        )

    with pytest.raises(ValidationError, match="post-window"):
        AdvancedAnalyticalRequest.model_validate(
            {
                **base,
                "post_filter": {
                    "kind": "comparison",
                    "comparison": {
                        "alias": "missing",
                        "operator": "greater_than",
                        "value": 0,
                    },
                },
            }
        )

    with pytest.raises(ValidationError, match="final ordering"):
        AdvancedAnalyticalRequest.model_validate(
            {
                **base,
                "result_order_by": [{"alias": "missing"}],
            }
        )


@pytest.mark.parametrize("alias", ("select", "a" * 64))
def test_request_rejects_postgresql_unsafe_output_aliases(alias: str) -> None:
    with pytest.raises(ValidationError, match="output alias"):
        AdvancedAnalyticalRequest(
            version=2,
            mode=AdvancedQueryMode.ROWS,
            primary_entity=LogicalModelRef("Product"),
            fields=(
                AdvancedField(
                    field=LogicalFieldRef("Product.product_key"),
                    alias=alias,
                ),
            ),
        )


def test_request_rejects_aliases_that_collide_after_postgresql_case_folding() -> None:
    with pytest.raises(ValidationError, match="globally unique"):
        AdvancedAnalyticalRequest(
            version=2,
            mode=AdvancedQueryMode.ROWS,
            primary_entity=LogicalModelRef("Product"),
            fields=(
                AdvancedField(
                    field=LogicalFieldRef("Product.product_key"),
                    alias="product",
                ),
                AdvancedField(
                    field=LogicalFieldRef("Product.category"),
                    alias="PRODUCT",
                ),
            ),
        )


def test_bucket_ranges_and_output_comparison_operands_are_unambiguous() -> None:
    with pytest.raises(ValidationError, match="overlap"):
        AdvancedField(
            field=LogicalFieldRef("Product.unit_price"),
            buckets=(
                NumericBucket(label="low", upper=100),
                NumericBucket(label="high", lower=90),
            ),
            else_label="other",
            alias="price_band",
        )

    alias_comparison = OutputFilter(
        alias="net_revenue",
        operator=FilterOperator.GREATER_THAN,
        compare_to_alias="average_revenue",
    )
    assert alias_comparison.value is None

    with pytest.raises(ValidationError, match="exactly one"):
        OutputFilter(
            alias="net_revenue",
            operator=FilterOperator.GREATER_THAN,
            value=100,
            compare_to_alias="average_revenue",
        )


def test_advanced_request_schema_exposes_no_sql_or_user_cte_surface() -> None:
    schema = json.dumps(AdvancedAnalyticalRequest.model_json_schema(), sort_keys=True)

    assert "raw_sql" not in schema
    assert "expression_sql" not in schema
    assert "cte_name" not in schema

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AdvancedAnalyticalRequest.model_validate(
            {
                "version": 2,
                "mode": "rows",
                "primary_entity": "SalesOrder",
                "fields": [{"field": "SalesOrder.order_key", "alias": "order_key"}],
                "cte_name": "attacker_stage",
            }
        )
