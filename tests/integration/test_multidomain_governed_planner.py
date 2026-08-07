"""Exact PostgreSQL proof for the non-customer semantic-registry domain."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from schemabridge.adapters.postgres.preview import PsycopgQueryPreview
from schemabridge.adapters.postgres.rejections import PsycopgRejectedSourceReporter
from schemabridge.adapters.sql.compiler import PostgresQueryCompiler
from schemabridge.adapters.sql.guard import SqlGlotPolicyGuard
from schemabridge.application.governed_execution import (
    ExecuteGovernedRequest,
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
)
from schemabridge.bootstrap import build_semantic_registry
from schemabridge.domain.resolution import ResolutionLimits

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def _runtime(reader_dsn: str) -> tuple[BuildGuidedRequest, ExecuteGovernedRequest]:
    registry = build_semantic_registry(repository_root=ROOT)
    snapshot = registry.load().registry
    allowed_fields = frozenset(
        key.physical_field.root
        for contract in snapshot.join_contracts.contracts
        for key in (contract.left_key, contract.right_key)
    )
    prepare = PrepareGovernedRequest(
        planner=PlanSemanticRequest(registry, ResolutionLimits()),
        compiler=PostgresQueryCompiler(),
        guard=SqlGlotPolicyGuard(),
    )
    return (
        BuildGuidedRequest(registry),
        ExecuteGovernedRequest(
            prepare=prepare,
            executor=PsycopgQueryPreview(reader_dsn),
            rejection_reporter=PsycopgRejectedSourceReporter(
                reader_dsn,
                allowed_fields,
            ),
        ),
    )


def test_one_table_product_query_matches_exact_rows_under_source_guards(
    reader_dsn: str,
) -> None:
    builder, executor = _runtime(reader_dsn)
    result = executor.execute(
        builder.execute(
            GuidedRequestInput(
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
        )
    )

    assert result.resolved_plan.selected_contracts == ()
    assert len(result.resolved_plan.query_policy.assets) == 1
    assert result.preview.rows == (
        ("BOOKS", 8),
        ("ELECTRONICS", 9),
        ("HOME", 9),
        ("SPORTS", 9),
    )
    assert result.rejected_sources.total_records == 0
    assert result.preview.database_user == "schemabridge_reader"
    assert result.preview.transaction_read_only is True
    assert result.preview.statement_timeout_ms == 5_000


def test_three_table_commerce_query_matches_exact_rows_and_rejections(
    reader_dsn: str,
) -> None:
    builder, executor = _runtime(reader_dsn)
    request = builder.execute(
        GuidedRequestInput(
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
    )

    result = executor.execute(request)

    assert [item.id for item in result.resolved_plan.selected_contracts] == [
        "product_to_sale_line",
        "sales_order_to_sale_line",
    ]
    assert len(result.resolved_plan.query_policy.assets) == 3
    assert result.preview.rows == (
        ("BOOKS", date(2026, 1, 1), Decimal("1496.45"), 25),
        ("BOOKS", date(2026, 2, 1), Decimal("988.00"), 16),
        ("ELECTRONICS", date(2026, 1, 1), Decimal("938.65"), 23),
        ("ELECTRONICS", date(2026, 2, 1), Decimal("440.85"), 7),
        ("HOME", date(2026, 1, 1), Decimal("529.70"), 13),
        ("HOME", date(2026, 2, 1), Decimal("637.80"), 12),
        ("SPORTS", date(2026, 1, 1), Decimal("988.80"), 16),
        ("SPORTS", date(2026, 2, 1), Decimal("852.00"), 11),
    )
    assert [(item.source_value, item.code.value) for item in result.rejected_sources.records] == [
        ("-3", "negative_identifier"),
        (None, "null_join_key"),
        ("", "malformed_identifier"),
        ("-1001", "negative_identifier"),
        ("bad-1060", "malformed_identifier"),
        (None, "null_join_key"),
    ]
    assert result.rejected_sources.total_records == 6
    assert result.preview.database_user == "schemabridge_reader"
    assert result.preview.transaction_read_only is True
    assert result.preview.statement_timeout_ms == 5_000


def test_duplicate_shipments_apply_exact_order_count_distinct(
    reader_dsn: str,
) -> None:
    builder, executor = _runtime(reader_dsn)
    result = executor.execute(
        builder.execute(
            GuidedRequestInput(
                primary_entity="SalesOrder",
                dimensions=(GuidedDimensionInput("SalesOrder.ordered_at", "month"),),
                metrics=(
                    GuidedMetricInput(
                        "count",
                        "SalesOrder.order_key",
                        "delivered_orders",
                    ),
                ),
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
        )
    )

    assert result.preview.rows == (
        (date(2026, 1, 1), 22),
        (date(2026, 2, 1), 11),
    )
    assert len(result.resolved_plan.query_policy.assets) == 2
    assert result.resolved_plan.fanout_mitigations[0].automatic is True
    assert [(item.source_value, item.code.value) for item in result.rejected_sources.records] == [
        ("", "malformed_identifier"),
        ("-1010", "negative_identifier"),
        ("not-an-order", "malformed_identifier"),
        (None, "null_join_key"),
    ]
    assert result.rejected_sources.total_records == 4
    assert result.preview.database_user == "schemabridge_reader"
    assert result.preview.transaction_read_only is True
    assert result.preview.statement_timeout_ms == 5_000
