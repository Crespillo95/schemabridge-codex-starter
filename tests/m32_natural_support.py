"""Reviewed natural-language fixtures for real M32 semantic resolution tests."""

from __future__ import annotations

from schemabridge.domain.advanced_requests import (
    AdvancedAnalyticalRequest,
    AdvancedField,
    AdvancedMetric,
    AdvancedMetricOperation,
    AdvancedQueryMode,
    LogicalBooleanPredicate,
    OutputBooleanPredicate,
    OutputFilter,
    OutputOrder,
    WindowCalculation,
    WindowOperation,
)
from schemabridge.domain.concepts import LogicalFieldRef, LogicalModelRef
from schemabridge.domain.requests import (
    DateGrain,
    Filter,
    FilterOperator,
    SortDirection,
)

M32_REFERENCE_QUESTION = (
    "Para cada mes, en pedidos completados, calcula por categoría de producto "
    "los ingresos netos, unidades y pedidos distintos. Conserva solo las categorías "
    "con al menos 4 pedidos distintos; ordénalas por ingresos dentro de cada mes, "
    "desempatando alfabéticamente por categoría; asigna una posición única, calcula "
    "su porcentaje sobre los ingresos de las categorías elegibles del mes y el ingreso "
    "acumulado, y devuelve como máximo las tres primeras categorías de cada mes."
)

M32_REFERENCE_LOGICAL_FIELDS = frozenset(
    {
        "SalesOrder.ordered_at",
        "Product.category",
        "SaleLine.net_amount",
        "SaleLine.quantity",
        "SalesOrder.order_key",
        "SalesOrder.order_status",
    }
)


def build_m32_reference_request() -> AdvancedAnalyticalRequest:
    """Return the exact reviewed intent for ``M32_REFERENCE_QUESTION``."""

    revenue_order = (
        OutputOrder(alias="net_revenue", direction=SortDirection.DESC),
        OutputOrder(alias="category", direction=SortDirection.ASC),
    )
    return AdvancedAnalyticalRequest(
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
                operation=AdvancedMetricOperation.SUM,
                field=LogicalFieldRef("SaleLine.net_amount"),
                alias="net_revenue",
            ),
            AdvancedMetric(
                operation=AdvancedMetricOperation.SUM,
                field=LogicalFieldRef("SaleLine.quantity"),
                alias="units",
            ),
            AdvancedMetric(
                operation=AdvancedMetricOperation.COUNT_DISTINCT,
                field=LogicalFieldRef("SalesOrder.order_key"),
                alias="distinct_orders",
            ),
        ),
        group_by=("month", "category"),
        where=LogicalBooleanPredicate.leaf(
            Filter(
                field=LogicalFieldRef("SalesOrder.order_status"),
                operator=FilterOperator.EQUALS,
                value="COMPLETED",
            )
        ),
        having=OutputBooleanPredicate.leaf(
            OutputFilter(
                alias="distinct_orders",
                operator=FilterOperator.GREATER_THAN_OR_EQUAL,
                value=4,
            )
        ),
        windows=(
            WindowCalculation(
                operation=WindowOperation.ROW_NUMBER,
                alias="revenue_rank",
                partition_by=("month",),
                order_by=revenue_order,
            ),
            WindowCalculation(
                operation=WindowOperation.PERCENT_OF_TOTAL,
                alias="revenue_percent",
                source="net_revenue",
                partition_by=("month",),
            ),
            WindowCalculation(
                operation=WindowOperation.RUNNING_SUM,
                alias="cumulative_revenue",
                source="net_revenue",
                partition_by=("month",),
                order_by=revenue_order,
            ),
        ),
        post_filter=OutputBooleanPredicate.leaf(
            OutputFilter(
                alias="revenue_rank",
                operator=FilterOperator.LESS_THAN_OR_EQUAL,
                value=3,
            )
        ),
        result_order_by=(
            OutputOrder(alias="month"),
            OutputOrder(alias="revenue_rank"),
            OutputOrder(alias="category"),
        ),
        limit=100,
    )


def build_m32_simple_row_request() -> AdvancedAnalyticalRequest:
    """Return a one-table row request proving that M32 also covers simple SQL."""

    return AdvancedAnalyticalRequest(
        version=2,
        mode=AdvancedQueryMode.ROWS,
        primary_entity=LogicalModelRef("Product"),
        fields=(
            AdvancedField(
                field=LogicalFieldRef("Product.product_key"),
                alias="product_key",
            ),
            AdvancedField(
                field=LogicalFieldRef("Product.category"),
                alias="category",
            ),
            AdvancedField(
                field=LogicalFieldRef("Product.unit_price"),
                alias="unit_price",
            ),
        ),
        where=LogicalBooleanPredicate.leaf(
            Filter(
                field=LogicalFieldRef("Product.is_active"),
                operator=FilterOperator.EQUALS,
                value=True,
            )
        ),
        result_order_by=(
            OutputOrder(alias="category"),
            OutputOrder(alias="product_key"),
        ),
        limit=50,
    )
