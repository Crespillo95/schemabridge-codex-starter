"""Security regressions for the independent final-SQL policy guard."""

from __future__ import annotations

from decimal import Decimal
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
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    QueryCostBudget,
    SourceConnectorKind,
    SourceDialect,
    postgres_type_contract_fingerprint,
)

ROOT = Path(__file__).resolve().parents[2]


def _execution_target() -> GovernedExecutionTarget:
    budget = QueryCostBudget(
        explain_timeout_ms=1_000,
        max_response_bytes=65_536,
        max_total_cost=Decimal("10000"),
        max_estimated_rows=100_000,
        max_plan_nodes=1_000,
        max_plan_depth=32,
        max_plan_width=16_384,
    )
    return GovernedExecutionTarget(
        workspace_id="tenant-a",
        connection_id=CatalogConnectionId("warehouse-primary"),
        connector_kind=SourceConnectorKind.POSTGRESQL,
        dialect=SourceDialect.POSTGRESQL,
        route_revision=1,
        route_fingerprint="a" * 64,
        expected_reader="schemabridge_reader",
        source_identity_fingerprint="c" * 64,
        catalog_identity_fingerprint="d" * 64,
        type_contract_fingerprint=postgres_type_contract_fingerprint(),
        cost_budget=budget,
        cost_budget_fingerprint=budget.fingerprint,
    )


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


def test_sql_guard_rejects_unregistered_dialect_before_parsing() -> None:
    with pytest.raises(SqlPolicyViolation) as captured:
        SqlGlotPolicyGuard().validate(
            CompiledQuery(
                sql="not valid SQL",
                parameters=(),
                effective_limit=1,
                dialect="mysql",  # type: ignore[arg-type]
            ),
            build_demo_query_policy(),
        )

    assert captured.value.findings[0].code is SqlRejectionCode.DIALECT_MISMATCH


def test_sql_guard_rejects_target_substitution_before_parsing() -> None:
    target = _execution_target()

    with pytest.raises(SqlPolicyViolation) as captured:
        SqlGlotPolicyGuard().validate(
            CompiledQuery(
                sql="not valid SQL",
                parameters=(),
                effective_limit=1,
                target_fingerprint="f" * 64,
            ),
            build_demo_query_policy(),
            target=target,
        )

    assert captured.value.findings[0].code is SqlRejectionCode.TARGET_MISMATCH


def test_sql_guard_rejects_target_bound_output_without_the_target() -> None:
    with pytest.raises(SqlPolicyViolation) as captured:
        SqlGlotPolicyGuard().validate(
            CompiledQuery(
                sql="not valid SQL",
                parameters=(),
                effective_limit=1,
                target_fingerprint="f" * 64,
            ),
            build_demo_query_policy(),
        )

    assert captured.value.findings[0].code is SqlRejectionCode.TARGET_MISMATCH


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
