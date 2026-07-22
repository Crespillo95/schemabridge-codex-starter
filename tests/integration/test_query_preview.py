"""Integration tests for guarded PostgreSQL query preview execution."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from schemabridge.adapters.postgres.preview import PsycopgQueryPreview
from schemabridge.adapters.sql.compiler import PostgresQueryCompiler
from schemabridge.adapters.sql.guard import SqlGlotPolicyGuard
from schemabridge.application.query_demo import (
    build_demo_query_policy,
    build_north_star_query_plan,
)
from schemabridge.application.query_execution import (
    PrepareQuery,
    PreviewQuery,
    QueryPreviewTimeoutError,
    ValidatedQuery,
)

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def test_query_preview_returns_north_star_ground_truth_as_reader(reader_dsn: str) -> None:
    policy = build_demo_query_policy()
    use_case = PreviewQuery(
        prepare=PrepareQuery(
            compiler=PostgresQueryCompiler(),
            guard=SqlGlotPolicyGuard(),
            policy=policy,
        ),
        executor=PsycopgQueryPreview(reader_dsn),
    )

    result = use_case.execute(build_north_star_query_plan())
    ground_truth = yaml.safe_load(
        (ROOT / "demo/ground_truth/query_cases.yml").read_text(encoding="utf-8")
    )
    expected_rows = tuple(
        (row["registration_date"], row["secondary_holder_customers"])
        for row in ground_truth["cases"][0]["expected_rows"]
    )

    assert result.columns == ("registration_date", "secondary_holder_customers")
    assert (
        result.rows
        == expected_rows
        == (
            (date(2026, 1, 1), 2),
            (date(2026, 1, 2), 1),
            (date(2026, 1, 3), 1),
        )
    )
    assert result.database_user == "schemabridge_reader"
    assert result.transaction_read_only is True
    assert result.statement_timeout_ms == 5_000
    assert result.truncated is False


def test_query_compiler_adds_limit_when_plan_omits_it() -> None:
    plan = build_north_star_query_plan()
    policy = build_demo_query_policy(max_preview_rows=2)
    prepared = PrepareQuery(
        compiler=PostgresQueryCompiler(),
        guard=SqlGlotPolicyGuard(),
        policy=policy,
    ).execute(plan)

    assert plan.limit is None
    assert prepared.max_rows == 2
    assert prepared.sql.endswith("LIMIT 2")


def test_query_preview_executor_applies_hard_row_cap(reader_dsn: str) -> None:
    query = ValidatedQuery(
        sql="SELECT generate_series(1, 10) AS n LIMIT 10",
        parameters=(),
        max_rows=3,
        statement_timeout_ms=5_000,
    )

    result = PsycopgQueryPreview(reader_dsn).execute(query)

    assert result.rows == ((1,), (2,), (3,))
    assert result.truncated is True


def test_query_preview_executor_enforces_statement_timeout(reader_dsn: str) -> None:
    query = ValidatedQuery(
        sql="SELECT pg_sleep(0.2) AS delayed LIMIT 1",
        parameters=(),
        max_rows=1,
        statement_timeout_ms=50,
    )

    with pytest.raises(QueryPreviewTimeoutError):
        PsycopgQueryPreview(reader_dsn).execute(query)


def test_query_preview_reports_maximum_allowed_timeout(reader_dsn: str) -> None:
    query = ValidatedQuery(
        sql="SELECT 1 AS value LIMIT 1",
        parameters=(),
        max_rows=1,
        statement_timeout_ms=60_000,
    )

    result = PsycopgQueryPreview(reader_dsn, max_timeout_ms=60_000).execute(query)

    assert result.statement_timeout_ms == 60_000
