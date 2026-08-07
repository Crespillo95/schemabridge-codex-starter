from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from schemabridge.adapters.catalog.synthetic_source import SyntheticCatalogSpecification
from schemabridge.adapters.semantic_registry.memory import (
    InMemoryGovernedSemanticRegistry,
)
from schemabridge.adapters.sql.compiler import PostgresQueryCompiler
from schemabridge.adapters.sql.guard import SqlGlotPolicyGuard
from schemabridge.application.governed_execution import (
    PlanSemanticRequest,
    PrepareGovernedRequest,
)
from schemabridge.application.guided_requests import (
    BuildGuidedRequest,
    GuidedDimensionInput,
    GuidedFilterInput,
    GuidedMetricInput,
    GuidedOrderInput,
    GuidedRequestInput,
    GuidedRequestValidationError,
)
from schemabridge.bootstrap import build_semantic_registry
from schemabridge.domain.plans import AggregateExpression, QueryPlan
from schemabridge.domain.request_context import ValidatedAnalyticalRequest
from schemabridge.domain.requests import MetricOperation
from schemabridge.domain.resolution import (
    ResolutionErrorCode,
    ResolutionLimits,
    SemanticResolutionError,
)

ROOT = Path(__file__).resolve().parents[2]


def _registry() -> InMemoryGovernedSemanticRegistry:
    recorded = build_semantic_registry(repository_root=ROOT)
    loaded = recorded.load()
    return InMemoryGovernedSemanticRegistry(loaded.registry, loaded.scope)


def _prepare(
    registry: InMemoryGovernedSemanticRegistry,
) -> tuple[BuildGuidedRequest, PrepareGovernedRequest]:
    return (
        BuildGuidedRequest(registry),
        PrepareGovernedRequest(
            planner=PlanSemanticRequest(registry, ResolutionLimits()),
            compiler=PostgresQueryCompiler(),
            guard=SqlGlotPolicyGuard(),
        ),
    )


def _active_products() -> GuidedRequestInput:
    return GuidedRequestInput(
        primary_entity="Product",
        dimensions=(GuidedDimensionInput("Product.category"),),
        metrics=(
            GuidedMetricInput(
                "count_distinct",
                "Product.product_key",
                "active_products",
            ),
        ),
        filters=(GuidedFilterInput("Product.is_active", "equals", True),),
        order_by=(GuidedOrderInput("Product.category"),),
        limit=100,
    )


def _delivered_orders() -> GuidedRequestInput:
    return GuidedRequestInput(
        primary_entity="SalesOrder",
        dimensions=(GuidedDimensionInput("SalesOrder.ordered_at", "month"),),
        metrics=(GuidedMetricInput("count", "SalesOrder.order_key", "delivered_orders"),),
        filters=(
            GuidedFilterInput(
                "Shipment.shipment_status",
                "equals",
                "DELIVERED",
            ),
        ),
        order_by=(GuidedOrderInput("SalesOrder.ordered_at"),),
        limit=100,
    )


def _commerce_revenue() -> GuidedRequestInput:
    return GuidedRequestInput(
        primary_entity="SaleLine",
        dimensions=(
            GuidedDimensionInput("Product.category"),
            GuidedDimensionInput("SalesOrder.ordered_at", "month"),
        ),
        metrics=(
            GuidedMetricInput("sum", "SaleLine.net_amount", "net_revenue"),
            GuidedMetricInput("sum", "SaleLine.quantity", "units"),
        ),
        filters=(
            GuidedFilterInput(
                "SalesOrder.order_status",
                "equals",
                "COMPLETED",
            ),
        ),
        order_by=(
            GuidedOrderInput("Product.category"),
            GuidedOrderInput("SalesOrder.ordered_at"),
        ),
        limit=100,
    )


def test_5434_asset_catalog_keeps_approved_one_two_and_three_table_plans_bounded() -> None:
    registry = _registry()
    builder, prepare = _prepare(registry)
    large_catalog = SyntheticCatalogSpecification(asset_count=5_434)

    active = prepare.execute(builder.execute(_active_products()))
    delivered = prepare.execute(builder.execute(_delivered_orders()))
    revenue = prepare.execute(builder.execute(_commerce_revenue()))

    prepared_by_table_count = {
        len(item.resolved_plan.query_policy.assets): item for item in (active, delivered, revenue)
    }

    assert large_catalog.asset_count == 5_434
    assert set(prepared_by_table_count) == {1, 2, 3}
    assert len(registry.registry.join_contracts.contracts) == 5
    assert active.resolved_plan.selected_contracts == ()
    assert [item.id for item in delivered.resolved_plan.selected_contracts] == [
        "sales_order_to_shipment"
    ]
    assert [item.id for item in revenue.resolved_plan.selected_contracts] == [
        "product_to_sale_line",
        "sales_order_to_sale_line",
    ]
    assert {asset.dataset.root for asset in revenue.resolved_plan.query_policy.assets} == {
        "commerce.products",
        "sales.order_lines",
        "sales.orders",
    }
    assert revenue.resolved_plan.query_policy.max_tables == 3
    assert "ROUND(CAST(r0.net_amount AS DECIMAL), 2)" in revenue.query.sql
    assert "COMPLETED" not in revenue.query.sql
    for table_count, prepared in prepared_by_table_count.items():
        assert len(prepared.resolved_plan.query_plan.joins) == table_count - 1
        assert prepared.resolved_plan.query_policy.max_tables == 3
        assert prepared.query.max_rows == 100
        assert prepared.query.statement_timeout_ms == 5_000


def test_registry_capacity_does_not_raise_the_per_request_join_or_table_limits() -> None:
    registry = _registry()
    builder, prepare = _prepare(registry)
    four_model_request = GuidedRequestInput(
        primary_entity="SalesOrder",
        dimensions=(
            GuidedDimensionInput("Product.category"),
            GuidedDimensionInput("SalesOrder.ordered_at", "month"),
        ),
        metrics=(GuidedMetricInput("sum", "SaleLine.net_amount", "net_revenue"),),
        filters=(
            GuidedFilterInput(
                "Shipment.shipment_status",
                "equals",
                "DELIVERED",
            ),
        ),
        limit=100,
    )

    assert len(registry.registry.logical_context.models) == 7
    assert len(registry.registry.join_contracts.contracts) == 5

    with pytest.raises(GuidedRequestValidationError) as captured:
        builder.execute(four_model_request)

    codes = {finding.code for finding in captured.value.result.findings}
    assert "too_many_logical_models" in codes
    assert "approved_join_path_too_long" in codes
    assert "missing_approved_join_path" not in codes

    forged_payload = builder.execute(_commerce_revenue()).model_dump(mode="json")
    forged_payload["required_models"] = ["SaleLine", "Product", "SalesOrder", "Shipment"]
    forged_payload["join_contract_ids"] = [
        "product_to_sale_line",
        "sales_order_to_sale_line",
        "sales_order_to_shipment",
    ]
    with pytest.raises(ValidationError) as forged:
        ValidatedAnalyticalRequest.model_validate(forged_payload)

    assert {
        error["loc"]
        for error in forged.value.errors(include_url=False)
        if error["type"] == "too_long"
    } == {("required_models",), ("join_contract_ids",)}

    plan_payload = prepare.execute(
        builder.execute(_commerce_revenue())
    ).resolved_plan.query_plan.model_dump(mode="json")
    plan_payload["joins"].append(plan_payload["joins"][0])
    with pytest.raises(ValidationError) as third_join:
        QueryPlan.model_validate(plan_payload)

    assert {
        error["loc"]
        for error in third_join.value.errors(include_url=False)
        if error["type"] == "too_long"
    } == {("joins",)}


def test_query_ir_rejects_cross_connection_routing_metadata() -> None:
    registry = _registry()
    builder, prepare = _prepare(registry)
    plan_payload = prepare.execute(
        builder.execute(_delivered_orders())
    ).resolved_plan.query_plan.model_dump(mode="json")
    plan_payload["root_scan"]["connection_id"] = "tenant-large-primary"
    plan_payload["joins"][0]["right_scan"]["connection_id"] = "tenant-large-secondary"

    with pytest.raises(ValidationError) as routed:
        QueryPlan.model_validate(plan_payload)

    assert {
        error["loc"]
        for error in routed.value.errors(include_url=False)
        if error["type"] == "extra_forbidden"
    } == {
        ("root_scan", "connection_id"),
        ("joins", 0, "right_scan", "connection_id"),
    }

    qualified_payload = prepare.execute(
        builder.execute(_delivered_orders())
    ).resolved_plan.query_plan.model_dump(mode="json")
    qualified_payload["joins"][0]["right_scan"]["dataset"] = (
        "tenant_large_secondary.fulfillment.shipments"
    )

    with pytest.raises(ValidationError, match="physical dataset must be schema-qualified"):
        QueryPlan.model_validate(qualified_payload)


def test_shipment_fanout_is_mitigated_and_all_join_sources_are_inspectable() -> None:
    registry = _registry()
    builder, prepare = _prepare(registry)
    prepared = prepare.execute(builder.execute(_delivered_orders()))
    aggregate = prepared.resolved_plan.query_plan.projections[1].expression

    assert isinstance(aggregate, AggregateExpression)
    assert aggregate.operation is MetricOperation.COUNT_DISTINCT
    assert prepared.resolved_plan.fanout_mitigations[0].automatic is True
    assert {check.physical_field.root for check in prepared.resolved_plan.rejection_checks} == {
        "sales.orders.order_id",
        "fulfillment.shipments.order_ref",
    }


def test_closed_semantic_values_and_homonymous_support_fields_fail_closed() -> None:
    registry = _registry()
    builder, _ = _prepare(registry)
    unsupported = _commerce_revenue()
    unsupported = GuidedRequestInput(
        primary_entity=unsupported.primary_entity,
        dimensions=unsupported.dimensions,
        metrics=unsupported.metrics,
        filters=(
            GuidedFilterInput(
                "SalesOrder.order_status",
                "equals",
                "MAYBE_COMPLETE",
            ),
        ),
        order_by=unsupported.order_by,
        limit=unsupported.limit,
    )

    with pytest.raises(GuidedRequestValidationError) as captured:
        builder.execute(unsupported)

    assert any(
        finding.code == "unapproved_filter_value" for finding in captured.value.result.findings
    )
    assert not any(
        mapping.mapping.physical_field.root.startswith("support.order_cases.")
        for mapping in registry.registry.mapping_set.mappings
    )


def test_homonymous_physical_name_is_not_guessed_as_a_logical_field() -> None:
    registry = _registry()
    builder, _ = _prepare(registry)
    physical_name_request = GuidedRequestInput(
        primary_entity="SalesOrder",
        dimensions=(GuidedDimensionInput("support.order_cases.order_id"),),
        metrics=(GuidedMetricInput("count", "SalesOrder.order_key", "orders"),),
        limit=100,
    )

    with pytest.raises(GuidedRequestValidationError) as captured:
        builder.execute(physical_name_request)

    assert [finding.code for finding in captured.value.result.findings] == ["unknown_logical_field"]
    assert "support.order_cases.order_id" not in registry.registry.logical_context.field_index()
    assert not any(
        mapping.mapping.physical_field.root == "support.order_cases.order_id"
        for mapping in registry.registry.mapping_set.mappings
    )


def test_execution_revalidation_detects_registry_revision_before_sql() -> None:
    registry = _registry()
    builder, prepare = _prepare(registry)
    validated = builder.execute(_active_products())
    approved = prepare.planner.execute(validated)
    registry.registry = registry.registry.model_copy(
        update={"version": registry.registry.version + 1}
    )

    with pytest.raises(SemanticResolutionError) as captured:
        prepare.refresh_and_validate(validated, approved)

    assert captured.value.code is ResolutionErrorCode.STALE_REGISTRY
