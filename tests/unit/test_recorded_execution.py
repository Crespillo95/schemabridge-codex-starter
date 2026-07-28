"""Fail-closed semantic replay tests for the synthetic browser preview."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from schemabridge.adapters.demo.recorded_execution import RecordedDemoExecutionAdapter
from schemabridge.adapters.planning.recorded import RecordedSemanticPlanningContext
from schemabridge.adapters.requests.recorded_context import RecordedRequestContextAdapter
from schemabridge.adapters.sql.compiler import PostgresQueryCompiler
from schemabridge.adapters.sql.guard import SqlGlotPolicyGuard
from schemabridge.application.governed_execution import (
    PlanSemanticRequest,
    PrepareGovernedRequest,
)
from schemabridge.application.guided_requests import (
    BuildGuidedRequest,
    GuidedRequestCase,
    build_demo_guided_input,
)
from schemabridge.application.query_execution import (
    QueryPreviewRejectedError,
    ValidatedQuery,
)
from schemabridge.domain.resolution import ResolutionLimits

ROOT = Path(__file__).resolve().parents[2]


def _north_star_query() -> ValidatedQuery:
    logical = ROOT / "demo/ground_truth/approved_logical_context.yml"
    context = RecordedSemanticPlanningContext(
        logical,
        ROOT / "demo/ground_truth/planning_mappings.yml",
        ROOT / "demo/ground_truth/join_contracts.yml",
    )
    request = BuildGuidedRequest(RecordedRequestContextAdapter(logical)).execute(
        build_demo_guided_input(GuidedRequestCase.NORTH_STAR)
    )
    return (
        PrepareGovernedRequest(
            PlanSemanticRequest(context, ResolutionLimits()),
            PostgresQueryCompiler(),
            SqlGlotPolicyGuard(),
        )
        .execute(request)
        .query
    )


def _adapter() -> RecordedDemoExecutionAdapter:
    return RecordedDemoExecutionAdapter(ROOT / "demo/hosted/north_star_execution.json")


def test_recorded_execution_accepts_the_exact_versioned_query() -> None:
    result = _adapter().execute(_north_star_query())

    assert result.columns == ("registration_date", "secondary_holder_customers")
    assert len(result.rows) == 3


def test_recorded_execution_accepts_only_top_level_presentation_alias_variation() -> None:
    query = _north_star_query()
    alias_variant = replace(
        query,
        sql=query.sql.replace(
            "secondary_holder_customers",
            "clientes_segundo_titular",
            1,
        ),
    )

    result = _adapter().execute(alias_variant)

    assert result.columns == ("registration_date", "clientes_segundo_titular")
    assert len(result.rows) == 3


@pytest.mark.parametrize(
    "mutation",
    (
        lambda query: replace(query, sql=query.sql.replace("COUNT(", "SUM(", 1)),
        lambda query: replace(
            query,
            parameters=(*query.parameters[:-1], "PRIMARY"),
        ),
        lambda query: replace(query, statement_timeout_ms=4_999),
    ),
)
def test_recorded_execution_rejects_every_non_alias_query_change(
    mutation: Callable[[ValidatedQuery], ValidatedQuery],
) -> None:
    query = _north_star_query()

    with pytest.raises(QueryPreviewRejectedError):
        _adapter().execute(mutation(query))
