"""Real M32 resolution against the approved synthetic semantic registry."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlglot import exp, parse_one
from tests.m32_natural_support import (
    M32_REFERENCE_LOGICAL_FIELDS,
    M32_REFERENCE_QUESTION,
    build_m32_reference_request,
    build_m32_simple_row_request,
)

from schemabridge.adapters.sql.compiler import PostgresQueryCompiler
from schemabridge.adapters.sql.guard import SqlGlotPolicyGuard
from schemabridge.bootstrap import build_semantic_registry
from schemabridge.domain.advanced_plans import (
    AdvancedAggregateExpression,
    AdvancedQueryPlan,
)
from schemabridge.domain.advanced_requests import (
    AdvancedAnalyticalRequest,
    AdvancedField,
    AdvancedMetric,
    AdvancedMetricOperation,
    AdvancedQueryMode,
    LogicalBooleanPredicate,
    OutputOrder,
    WindowCalculation,
    WindowOperation,
)
from schemabridge.domain.concepts import LogicalFieldRef, LogicalModelRef
from schemabridge.domain.request_context import (
    ValidatedAdvancedAnalyticalRequest,
    validate_analytical_request,
)
from schemabridge.domain.requests import (
    Filter,
    FilterOperator,
    SortDirection,
)
from schemabridge.domain.resolution import (
    AdvancedResolvedSemanticPlan,
    ResolutionErrorCode,
    ResolutionLimits,
    SemanticResolutionError,
    resolve_semantic_request,
)
from schemabridge.domain.semantic_registry import GovernedSemanticRegistrySnapshot

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def synthetic_registry() -> GovernedSemanticRegistrySnapshot:
    return build_semantic_registry(repository_root=ROOT).load().registry


def _validate(
    request: AdvancedAnalyticalRequest,
    registry: GovernedSemanticRegistrySnapshot,
) -> ValidatedAdvancedAnalyticalRequest:
    validation = validate_analytical_request(request, registry.logical_context)

    assert validation.result.findings == ()
    assert validation.validated_request is not None
    return validation.validated_request


def test_reviewed_spanish_request_resolves_exact_three_table_advanced_plan(
    synthetic_registry: GovernedSemanticRegistrySnapshot,
) -> None:
    request = build_m32_reference_request()
    validated = _validate(request, synthetic_registry)
    resolved = resolve_semantic_request(
        validated,
        synthetic_registry,
        ResolutionLimits(),
    )

    assert M32_REFERENCE_QUESTION.startswith("Para cada mes")
    assert isinstance(resolved, AdvancedResolvedSemanticPlan)
    assert isinstance(resolved.query_plan, AdvancedQueryPlan)
    assert resolved.request == request
    assert [model.root for model in validated.required_models] == [
        "SaleLine",
        "SalesOrder",
        "Product",
    ]
    assert validated.join_contract_ids == (
        "sales_order_to_sale_line",
        "product_to_sale_line",
    )
    assert [contract.id for contract in resolved.selected_contracts] == [
        "sales_order_to_sale_line",
        "product_to_sale_line",
    ]

    assert request.where is not None
    assert request.where.comparison is not None
    requested_fields = {
        *(item.field.root for item in request.fields),
        *(item.field.root for item in request.metrics if item.field is not None),
        request.where.comparison.field.root,
    }
    assert requested_fields == M32_REFERENCE_LOGICAL_FIELDS
    assert len(requested_fields) == 6

    plan = resolved.query_plan
    assert plan.version == 2
    assert plan.root_scan.dataset.root == "sales.order_lines"
    assert [join.right_scan.dataset.root for join in plan.joins] == [
        "sales.orders",
        "commerce.products",
    ]
    assert [item.alias.root for item in plan.projections] == [
        "month",
        "category",
        "net_revenue",
        "units",
        "distinct_orders",
    ]
    assert [
        item.expression.operation
        for item in plan.projections
        if isinstance(item.expression, AdvancedAggregateExpression)
    ] == [
        AdvancedMetricOperation.SUM,
        AdvancedMetricOperation.SUM,
        AdvancedMetricOperation.COUNT_DISTINCT,
    ]
    assert plan.where is not None
    assert plan.having is not None
    assert [item.expression.operation for item in plan.windows] == [
        WindowOperation.ROW_NUMBER,
        WindowOperation.PERCENT_OF_TOTAL,
        WindowOperation.RUNNING_SUM,
    ]
    assert plan.post_filter is not None
    assert [item.alias.root for item in plan.result_order_by] == [
        "month",
        "revenue_rank",
        "category",
    ]
    assert plan.limit == 100


def test_reference_policy_is_the_exact_physical_closure_and_fanout_is_safe(
    synthetic_registry: GovernedSemanticRegistrySnapshot,
) -> None:
    validated = _validate(build_m32_reference_request(), synthetic_registry)
    resolved = resolve_semantic_request(
        validated,
        synthetic_registry,
        ResolutionLimits(),
    )

    policy_columns = {asset.dataset.root: asset.columns for asset in resolved.query_policy.assets}
    assert policy_columns == {
        "sales.order_lines": (
            "net_amount",
            "order_ref",
            "product_no",
            "quantity",
        ),
        "sales.orders": (
            "order_id",
            "ordered_at",
            "status_code",
        ),
        "commerce.products": (
            "category_code",
            "product_code",
        ),
    }
    assert resolved.query_policy.max_tables == 3
    assert resolved.query_policy.max_preview_rows == 500
    assert resolved.query_policy.statement_timeout_ms == 5_000
    assert resolved.fanout_mitigations == ()
    assert any(
        assumption.code == "relationship_metric_requested" for assumption in resolved.assumptions
    )
    assert {check.physical_field.root for check in resolved.rejection_checks} == {
        "sales.order_lines.order_ref",
        "sales.order_lines.product_no",
        "sales.orders.order_id",
        "commerce.products.product_code",
    }


def test_simple_row_request_uses_one_table_without_aggregate_or_join(
    synthetic_registry: GovernedSemanticRegistrySnapshot,
) -> None:
    request = build_m32_simple_row_request()
    validated = _validate(request, synthetic_registry)
    resolved = resolve_semantic_request(
        validated,
        synthetic_registry,
        ResolutionLimits(),
    )

    assert validated.required_models == (LogicalModelRef("Product"),)
    assert validated.join_contract_ids == ()
    assert isinstance(resolved.query_plan, AdvancedQueryPlan)
    assert resolved.query_plan.joins == ()
    assert resolved.query_plan.group_by == ()
    assert resolved.query_plan.having is None
    assert resolved.query_plan.windows == ()
    assert resolved.query_plan.where is not None
    assert [item.alias.root for item in resolved.query_plan.projections] == [
        "product_key",
        "category",
        "unit_price",
    ]
    assert [item.alias.root for item in resolved.query_plan.result_order_by] == [
        "category",
        "product_key",
    ]
    assert {asset.dataset.root: asset.columns for asset in resolved.query_policy.assets} == {
        "commerce.products": (
            "category_code",
            "is_active",
            "product_code",
            "unit_price",
        )
    }
    assert resolved.fanout_mitigations == ()
    assert resolved.rejection_checks == ()


def test_null_filter_on_closed_set_field_compiles_without_a_parameter(
    synthetic_registry: GovernedSemanticRegistrySnapshot,
) -> None:
    request = build_m32_simple_row_request().model_copy(
        update={
            "where": LogicalBooleanPredicate.leaf(
                Filter(
                    field=LogicalFieldRef("Product.category"),
                    operator=FilterOperator.IS_NULL,
                    value=None,
                )
            )
        }
    )
    validated = _validate(request, synthetic_registry)
    resolved = resolve_semantic_request(
        validated,
        synthetic_registry,
        ResolutionLimits(),
    )
    compiled = PostgresQueryCompiler().compile(
        resolved.query_plan,
        max_preview_rows=resolved.query_policy.max_preview_rows,
    )
    guarded = SqlGlotPolicyGuard().validate(compiled, resolved.query_policy)

    statement = parse_one(guarded.sql, read="postgres")
    where = statement.args["where"].this
    assert isinstance(where, exp.Is)
    assert isinstance(where.this, exp.Case)
    assert isinstance(where.expression, exp.Null)
    assert None not in guarded.parameters


def test_advanced_resolution_rejects_a_stale_validated_context(
    synthetic_registry: GovernedSemanticRegistrySnapshot,
) -> None:
    validated = _validate(build_m32_reference_request(), synthetic_registry)
    stale = validated.model_copy(update={"context_version": validated.context_version + 1})

    with pytest.raises(SemanticResolutionError) as captured:
        resolve_semantic_request(stale, synthetic_registry, ResolutionLimits())

    assert captured.value.code is ResolutionErrorCode.STALE_LOGICAL_CONTEXT


@pytest.mark.parametrize(
    ("input_request", "expected_code"),
    [
        (
            build_m32_reference_request().model_copy(
                update={
                    "where": LogicalBooleanPredicate.leaf(
                        Filter(
                            field=LogicalFieldRef("SalesOrder.order_status"),
                            operator=FilterOperator.EQUALS,
                            value="MAYBE_COMPLETE",
                        )
                    )
                }
            ),
            "unapproved_filter_value",
        ),
        (
            build_m32_simple_row_request().model_copy(
                update={
                    "fields": (
                        AdvancedField(
                            field=LogicalFieldRef("Product.unknown_attribute"),
                            alias="unknown_attribute",
                        ),
                    ),
                    "result_order_by": (),
                }
            ),
            "unknown_logical_field",
        ),
        (
            build_m32_reference_request().model_copy(
                update={
                    "windows": (
                        WindowCalculation(
                            operation=WindowOperation.ROW_NUMBER,
                            alias="revenue_rank",
                            partition_by=("month",),
                            order_by=(
                                OutputOrder(
                                    alias="net_revenue",
                                    direction=SortDirection.DESC,
                                ),
                            ),
                        ),
                    ),
                    "post_filter": None,
                    "result_order_by": (),
                }
            ),
            "window_order_not_deterministic",
        ),
    ],
)
def test_advanced_context_validation_rejects_unapproved_or_ambiguous_intent(
    synthetic_registry: GovernedSemanticRegistrySnapshot,
    input_request: AdvancedAnalyticalRequest,
    expected_code: str,
) -> None:
    validation = validate_analytical_request(
        input_request,
        synthetic_registry.logical_context,
    )

    assert validation.validated_request is None
    assert expected_code in {finding.code for finding in validation.result.findings}


@pytest.mark.parametrize(
    "operation",
    (WindowOperation.RANK, WindowOperation.DENSE_RANK),
)
def test_peer_rankings_allow_equal_metric_values_without_a_unique_tie_breaker(
    synthetic_registry: GovernedSemanticRegistrySnapshot,
    operation: WindowOperation,
) -> None:
    request = build_m32_reference_request().model_copy(
        update={
            "windows": (
                WindowCalculation(
                    operation=operation,
                    alias="revenue_rank",
                    partition_by=("month",),
                    order_by=(
                        OutputOrder(
                            alias="net_revenue",
                            direction=SortDirection.DESC,
                        ),
                    ),
                ),
            ),
        }
    )

    validation = validate_analytical_request(
        request,
        synthetic_registry.logical_context,
    )

    assert validation.result.findings == ()
    assert validation.validated_request is not None
    resolved = resolve_semantic_request(
        validation.validated_request,
        synthetic_registry,
        ResolutionLimits(),
    )
    assert isinstance(resolved, AdvancedResolvedSemanticPlan)
    assert resolved.query_plan.windows[0].expression.operation is operation
    assert tuple(
        item.alias.root for item in resolved.query_plan.windows[0].expression.order_by
    ) == ("net_revenue",)
    assert tuple(item.alias.root for item in resolved.query_plan.result_order_by) == (
        "month",
        "revenue_rank",
        "category",
    )


def test_resolution_rejects_sum_of_order_total_across_sale_line_fanout(
    synthetic_registry: GovernedSemanticRegistrySnapshot,
) -> None:
    request = AdvancedAnalyticalRequest(
        version=2,
        mode=AdvancedQueryMode.AGGREGATE,
        primary_entity=LogicalModelRef("SalesOrder"),
        fields=(
            AdvancedField(
                field=LogicalFieldRef("Product.category"),
                alias="category",
            ),
        ),
        metrics=(
            AdvancedMetric(
                operation=AdvancedMetricOperation.SUM,
                field=LogicalFieldRef("SalesOrder.order_total"),
                alias="order_revenue",
            ),
        ),
        group_by=("category",),
        result_order_by=(OutputOrder(alias="category"),),
    )
    validated = _validate(request, synthetic_registry)

    with pytest.raises(SemanticResolutionError) as captured:
        resolve_semantic_request(
            validated,
            synthetic_registry,
            ResolutionLimits(),
        )

    assert captured.value.code is ResolutionErrorCode.UNSUPPORTED_FANOUT
    assert "sales_order_to_sale_line" in str(captured.value)
    assert "duplication-invariant aggregate" in str(captured.value)
