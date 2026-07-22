"""PostgreSQL integration tests for governed planning, preview, and rejections."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from schemabridge.adapters.planning.recorded import RecordedSemanticPlanningContext
from schemabridge.adapters.postgres.preview import PsycopgQueryPreview
from schemabridge.adapters.postgres.rejections import PsycopgRejectedSourceReporter
from schemabridge.adapters.requests.recorded_context import RecordedRequestContextAdapter
from schemabridge.adapters.sql.compiler import PostgresQueryCompiler
from schemabridge.adapters.sql.guard import SqlGlotPolicyGuard
from schemabridge.application.governed_execution import (
    ExecuteGovernedRequest,
    PlanSemanticRequest,
    PrepareGovernedRequest,
)
from schemabridge.application.guided_requests import (
    BuildGuidedRequest,
    GuidedRequestCase,
    build_demo_guided_input,
)
from schemabridge.domain.resolution import ResolutionLimits

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def _execute(case: GuidedRequestCase, reader_dsn: str):  # type: ignore[no-untyped-def]
    logical_path = ROOT / "demo/ground_truth/approved_logical_context.yml"
    context = RecordedSemanticPlanningContext(
        logical_path,
        ROOT / "demo/ground_truth/planning_mappings.yml",
        ROOT / "demo/ground_truth/join_contracts.yml",
    )
    request = BuildGuidedRequest(RecordedRequestContextAdapter(logical_path)).execute(
        build_demo_guided_input(case)
    )
    return ExecuteGovernedRequest(
        prepare=PrepareGovernedRequest(
            PlanSemanticRequest(context, ResolutionLimits()),
            PostgresQueryCompiler(),
            SqlGlotPolicyGuard(),
        ),
        executor=PsycopgQueryPreview(reader_dsn),
        rejection_reporter=PsycopgRejectedSourceReporter(
            reader_dsn,
            frozenset(
                {
                    "crm.customers.customer_id",
                    "bank.account_holders.gf_customer_id",
                }
            ),
        ),
    ).execute(request)


def test_governed_planner_preview_returns_north_star_and_exact_rejections(
    reader_dsn: str,
) -> None:
    result = _execute(GuidedRequestCase.NORTH_STAR, reader_dsn)

    assert result.preview.columns == ("registration_date", "secondary_holder_customers")
    assert result.preview.rows == (
        (date(2026, 1, 1), 2),
        (date(2026, 1, 2), 1),
        (date(2026, 1, 3), 1),
    )
    assert [(item.source_value, item.code.value) for item in result.rejected_sources.records] == [
        ("127.5", "non_integral_identifier"),
        ("NaN", "non_finite_identifier"),
        (None, "null_join_key"),
    ]
    assert (
        result.preview.database_user
        == result.rejected_sources.database_user
        == ("schemabridge_reader")
    )
    assert result.preview.transaction_read_only is True
    assert result.rejected_sources.transaction_read_only is True


def test_governed_planner_no_join_and_relationship_count_are_distinct(
    reader_dsn: str,
) -> None:
    no_join = _execute(GuidedRequestCase.NO_JOIN, reader_dsn)
    relationships = _execute(GuidedRequestCase.RELATIONSHIP_COUNT, reader_dsn)

    assert no_join.preview.rows == (("ES", 4), ("FR", 1), ("PT", 1))
    assert no_join.resolved_plan.selected_contracts == ()
    assert no_join.rejected_sources.records == ()
    assert relationships.preview.rows == (
        (date(2026, 1, 1), 3),
        (date(2026, 1, 2), 1),
        (date(2026, 1, 3), 1),
    )
