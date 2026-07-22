"""Application orchestration and CLI tests for guarded query preparation."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from typer.testing import CliRunner

from schemabridge.adapters.sql.guard import SqlGlotPolicyGuard
from schemabridge.application.query_demo import (
    build_demo_query_policy,
    build_north_star_query_plan,
)
from schemabridge.application.query_execution import (
    CompiledQuery,
    PrepareQuery,
    SqlPolicyViolation,
)
from schemabridge.domain.plans import QueryPlan
from schemabridge.entrypoints.cli.main import app

runner = CliRunner()


@dataclass(frozen=True)
class BuggyCompiler:
    def compile(self, plan: QueryPlan, *, max_preview_rows: int) -> CompiledQuery:
        del plan, max_preview_rows
        return CompiledQuery(
            sql="DELETE FROM crm.customers",
            parameters=(),
            effective_limit=1,
        )


def test_compiler_bug_cannot_bypass_independent_guard() -> None:
    use_case = PrepareQuery(
        compiler=BuggyCompiler(),
        guard=SqlGlotPolicyGuard(),
        policy=build_demo_query_policy(),
    )

    with pytest.raises(SqlPolicyViolation) as captured:
        use_case.execute(build_north_star_query_plan())

    assert captured.value.findings[0].code.value == "non_read_only_statement"


def test_query_demo_cli_prints_parameterized_guarded_sql() -> None:
    result = runner.invoke(app, ["query-demo", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["effective_limit"] == 500
    assert payload["preview"] is None
    assert payload["sql"].count("%s") == len(payload["parameters"])
    assert "SECONDARY" not in payload["sql"]


def test_sql_guard_demo_cli_reports_three_stable_rejections() -> None:
    result = runner.invoke(app, ["sql-guard-demo", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    codes = {item["case"]: item["findings"][0]["code"] for item in payload["results"]}
    assert codes == {
        "statement_smuggling": "multiple_statements",
        "destructive_cte": "forbidden_statement",
        "cartesian_join": "missing_join_predicate",
    }
