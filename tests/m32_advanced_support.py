"""Shared synthetic contracts for M32 advanced SQL tests."""

from __future__ import annotations

from schemabridge.domain.advanced_plans import (
    AdvancedAggregateExpression,
    AdvancedQueryPlan,
    AdvancedSelectItem,
    AggregateBooleanPredicate,
    AggregateFilterPredicate,
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
    WindowOperation,
)
from schemabridge.domain.fields import PhysicalDatasetRef, PhysicalFieldRef
from schemabridge.domain.plans import (
    AllowedAsset,
    ColumnExpression,
    DatasetScan,
    DateGrainExpression,
    FilterPredicate,
    OutputAlias,
    ParameterValue,
    QueryPolicy,
    RelationAlias,
)
from schemabridge.domain.requests import DateGrain, FilterOperator, SortDirection

DATASET = PhysicalDatasetRef("commerce.sale_lines_enriched")
RELATION = RelationAlias("sales")


def build_m32_reference_plan() -> AdvancedQueryPlan:
    """Build the closed plan for the reviewed Spanish reference question."""

    month = DateGrainExpression(
        source=_column("ordered_at"),
        grain=DateGrain.MONTH,
    )
    category = _column("category")
    net_revenue = AdvancedAggregateExpression(
        operation=AdvancedMetricOperation.SUM,
        source=_column("net_amount"),
    )
    units = AdvancedAggregateExpression(
        operation=AdvancedMetricOperation.SUM,
        source=_column("quantity"),
    )
    distinct_orders = AdvancedAggregateExpression(
        operation=AdvancedMetricOperation.COUNT_DISTINCT,
        source=_column("order_key"),
    )
    revenue_order = (
        OutputOrderItem(
            alias=OutputAlias("net_revenue"),
            direction=SortDirection.DESC,
        ),
        OutputOrderItem(
            alias=OutputAlias("category"),
            direction=SortDirection.ASC,
        ),
    )

    return AdvancedQueryPlan(
        root_scan=DatasetScan(dataset=DATASET, alias=RELATION),
        projections=(
            AdvancedSelectItem(expression=month, alias=OutputAlias("month")),
            AdvancedSelectItem(expression=category, alias=OutputAlias("category")),
            AdvancedSelectItem(
                expression=net_revenue,
                alias=OutputAlias("net_revenue"),
            ),
            AdvancedSelectItem(expression=units, alias=OutputAlias("units")),
            AdvancedSelectItem(
                expression=distinct_orders,
                alias=OutputAlias("distinct_orders"),
            ),
        ),
        group_by=(month, category),
        where=PhysicalBooleanPredicate(
            kind=BooleanOperator.COMPARISON,
            comparison=FilterPredicate(
                expression=_column("order_status"),
                operator=FilterOperator.EQUALS,
                values=(ParameterValue(value="COMPLETED"),),
            ),
        ),
        having=AggregateBooleanPredicate(
            kind=BooleanOperator.COMPARISON,
            comparison=AggregateFilterPredicate(
                expression=distinct_orders,
                operator=FilterOperator.GREATER_THAN_OR_EQUAL,
                values=(ParameterValue(value=4),),
            ),
        ),
        windows=(
            WindowSelectItem(
                expression=WindowExpression(
                    operation=WindowOperation.ROW_NUMBER,
                    partition_by=(OutputAlias("month"),),
                    order_by=revenue_order,
                ),
                alias=OutputAlias("revenue_rank"),
            ),
            WindowSelectItem(
                expression=WindowExpression(
                    operation=WindowOperation.PERCENT_OF_TOTAL,
                    source=OutputAlias("net_revenue"),
                    partition_by=(OutputAlias("month"),),
                ),
                alias=OutputAlias("revenue_percent"),
            ),
            WindowSelectItem(
                expression=WindowExpression(
                    operation=WindowOperation.RUNNING_SUM,
                    source=OutputAlias("net_revenue"),
                    partition_by=(OutputAlias("month"),),
                    order_by=revenue_order,
                ),
                alias=OutputAlias("cumulative_revenue"),
            ),
        ),
        post_filter=OutputBooleanPredicatePlan(
            kind=BooleanOperator.COMPARISON,
            comparison=OutputFilterPredicate(
                alias=OutputAlias("revenue_rank"),
                operator=FilterOperator.LESS_THAN_OR_EQUAL,
                values=(ParameterValue(value=3),),
            ),
        ),
        result_order_by=(
            OutputOrderItem(
                alias=OutputAlias("month"),
                direction=SortDirection.ASC,
            ),
            OutputOrderItem(
                alias=OutputAlias("revenue_rank"),
                direction=SortDirection.ASC,
            ),
            OutputOrderItem(
                alias=OutputAlias("category"),
                direction=SortDirection.ASC,
            ),
        ),
        limit=100,
    )


def build_m32_reference_policy() -> QueryPolicy:
    """Allow only the synthetic columns used by the reference plan."""

    return QueryPolicy(
        assets=(
            AllowedAsset(
                dataset=DATASET,
                columns=(
                    "ordered_at",
                    "category",
                    "net_amount",
                    "quantity",
                    "order_key",
                    "order_status",
                ),
            ),
        ),
        max_tables=3,
        max_preview_rows=500,
        statement_timeout_ms=5_000,
    )


def _column(name: str) -> ColumnExpression:
    return ColumnExpression(
        relation=RELATION,
        field=PhysicalFieldRef(f"{DATASET.root}.{name}"),
    )
