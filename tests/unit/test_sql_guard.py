"""Security regressions for the independent final-SQL policy guard."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from schemabridge.adapters.sql.compiler import PostgresQueryCompiler
from schemabridge.adapters.sql.guard import SqlGlotPolicyGuard
from schemabridge.application.query_demo import (
    build_demo_query_policy,
    build_north_star_query_plan,
)
from schemabridge.application.query_execution import (
    CompiledQuery,
    SqlPolicyViolation,
    SqlRejectionCode,
)

ROOT = Path(__file__).resolve().parents[2]


def load_security_cases() -> list[dict[str, object]]:
    path = ROOT / "tests/fixtures/security/sql_guard_cases.yml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    cases = payload["cases"]
    assert isinstance(cases, list)
    return cases


@pytest.mark.parametrize(
    "case",
    load_security_cases(),
    ids=lambda case: str(case["id"]),
)
def test_sql_guard_rejects_security_matrix(case: dict[str, object]) -> None:
    sql = case["sql"]
    expected_code = case["expected_code"]
    effective_limit = case.get("effective_limit", 1)
    assert isinstance(sql, str)
    assert isinstance(expected_code, str)
    assert isinstance(effective_limit, int)

    with pytest.raises(SqlPolicyViolation) as captured:
        SqlGlotPolicyGuard().validate(
            CompiledQuery(sql=sql, parameters=(), effective_limit=effective_limit),
            build_demo_query_policy(),
        )

    assert captured.value.findings[0].code is SqlRejectionCode(expected_code)
    assert captured.value.findings[0].message


def test_sql_guard_accepts_compiler_output_after_reparsing() -> None:
    policy = build_demo_query_policy()
    compiled = PostgresQueryCompiler().compile(
        build_north_star_query_plan(),
        max_preview_rows=policy.max_preview_rows,
    )

    validated = SqlGlotPolicyGuard().validate(compiled, policy)

    assert validated.sql == compiled.sql
    assert validated.parameters == compiled.parameters
    assert validated.max_rows == 500
    assert validated.statement_timeout_ms == 5_000


def test_read_only_cte_is_allowed_when_assets_columns_and_limit_are_valid() -> None:
    sql = (
        "WITH active AS ("
        "SELECT c.customer_id FROM crm.customers AS c "
        "WHERE c.customer_status = %s"
        ") SELECT customer_id FROM active LIMIT 10"
    )

    validated = SqlGlotPolicyGuard().validate(
        CompiledQuery(sql=sql, parameters=("ACTIVE",), effective_limit=10),
        build_demo_query_policy(),
    )

    assert validated.max_rows == 10
