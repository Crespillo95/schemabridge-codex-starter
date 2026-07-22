"""Complete guided-request to governed read-only result acceptance path."""

from __future__ import annotations

import os
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

pytestmark = pytest.mark.acceptance
ROOT = Path(__file__).resolve().parents[2]
READER_DSN = os.environ.get(
    "SCHEMABRIDGE_TEST_DATABASE_URL",
    "postgresql://schemabridge_reader:schemabridge_reader@127.0.0.1:55433/schemabridge",
)


def test_guided_north_star_resolves_guards_executes_and_reports_rejections() -> None:
    logical_path = ROOT / "demo/ground_truth/approved_logical_context.yml"
    request = BuildGuidedRequest(RecordedRequestContextAdapter(logical_path)).execute(
        build_demo_guided_input(GuidedRequestCase.NORTH_STAR)
    )
    result = ExecuteGovernedRequest(
        prepare=PrepareGovernedRequest(
            planner=PlanSemanticRequest(
                RecordedSemanticPlanningContext(
                    logical_path,
                    ROOT / "demo/ground_truth/planning_mappings.yml",
                    ROOT / "demo/ground_truth/join_contracts.yml",
                ),
                ResolutionLimits(),
            ),
            compiler=PostgresQueryCompiler(),
            guard=SqlGlotPolicyGuard(),
        ),
        executor=PsycopgQueryPreview(READER_DSN),
        rejection_reporter=PsycopgRejectedSourceReporter(
            READER_DSN,
            frozenset(
                {
                    "crm.customers.customer_id",
                    "bank.account_holders.gf_customer_id",
                }
            ),
        ),
    ).execute(request)

    assert result.policy_status == "accepted"
    assert result.preview.rows == (
        (date(2026, 1, 1), 2),
        (date(2026, 1, 2), 1),
        (date(2026, 1, 3), 1),
    )
    assert [item.code.value for item in result.rejected_sources.records] == [
        "non_integral_identifier",
        "non_finite_identifier",
        "null_join_key",
    ]
    assert result.preview.database_user == "schemabridge_reader"
