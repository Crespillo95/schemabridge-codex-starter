"""Contract tests for standalone typed PostgreSQL rendering."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlglot import exp, parse
from tests.m32_advanced_support import (
    build_m32_reference_plan,
    build_m32_reference_policy,
)

from schemabridge.adapters.sql.compiler import PostgresQueryCompiler
from schemabridge.adapters.sql.export import PostgresCopyableSqlRenderer
from schemabridge.adapters.sql.guard import SqlGlotPolicyGuard
from schemabridge.application.query_execution import (
    CompiledQuery,
    QueryCompilationError,
    ValidatedQuery,
)
from schemabridge.application.sql_export import (
    BuildCopyableSql,
    CopyableSqlArtifact,
)
from schemabridge.domain.connectors import SourceDialect


def test_reference_copyable_sql_is_standalone_and_independently_reguarded() -> None:
    policy = build_m32_reference_policy()
    compiled = PostgresQueryCompiler().compile(
        build_m32_reference_plan(),
        max_preview_rows=policy.max_preview_rows,
    )
    validated = SqlGlotPolicyGuard().validate(compiled, policy)

    artifact = PostgresCopyableSqlRenderer().render(
        validated,
        request_fingerprint="request-fingerprint",
        plan_fingerprint="plan-fingerprint",
    )
    reguarded = SqlGlotPolicyGuard().validate(
        CompiledQuery(
            sql=artifact.sql,
            parameters=(),
            effective_limit=validated.max_rows,
            dialect=artifact.dialect,
            target_fingerprint=validated.target_fingerprint,
            plan_version=artifact.plan_version,
        ),
        policy,
    )
    statement = _single_select(artifact.sql)
    with_clause = statement.args["with_"]
    aggregated = with_clause.expressions[0].this
    assert isinstance(aggregated, exp.Select)
    status_filter = aggregated.args["where"].this
    having_filter = aggregated.args["having"].this
    rank_filter = statement.args["where"].this

    assert artifact.executed is False
    assert artifact.plan_version == 2
    assert artifact.sha256 == hashlib.sha256(artifact.sql.encode("utf-8")).hexdigest()
    assert "%s" not in artifact.sql
    assert not tuple(statement.find_all(exp.Placeholder))
    assert len(tuple(statement.find_all(exp.Select))) == 3
    assert "COMPLETED" in {
        literal.this for literal in statement.find_all(exp.Literal) if literal.is_string
    }
    assert isinstance(status_filter, exp.EQ)
    assert _literal_scalar(status_filter.expression) == "COMPLETED"
    assert isinstance(having_filter, exp.GTE)
    assert _literal_scalar(having_filter.expression) == "4"
    assert isinstance(rank_filter, exp.LTE)
    assert _literal_scalar(rank_filter.expression) == "3"
    assert reguarded.parameters == ()
    assert reguarded.plan_version == 2
    assert reguarded.sql == artifact.sql


def test_copyable_renderer_preserves_typed_literals_without_executing() -> None:
    query = ValidatedQuery(
        sql=(
            "SELECT %s AS text_value, %s AS integer_value, %s AS decimal_value, "
            "%s AS boolean_value, %s AS date_value, %s AS timestamp_value, "
            "%s AS null_value LIMIT 1"
        ),
        parameters=(
            "O'Reilly; DROP TABLE commerce.sale_lines_enriched; --",
            42,
            Decimal("12.3400"),
            True,
            date(2026, 1, 31),
            datetime(2026, 1, 31, 12, 34, 56, 123456),
            None,
        ),
        max_rows=1,
        statement_timeout_ms=5_000,
        dialect=SourceDialect.POSTGRESQL,
        plan_version=2,
    )

    first = PostgresCopyableSqlRenderer().render(
        query,
        request_fingerprint="request-fingerprint",
        plan_fingerprint="plan-fingerprint",
    )
    second = PostgresCopyableSqlRenderer().render(
        query,
        request_fingerprint="request-fingerprint",
        plan_fingerprint="plan-fingerprint",
    )
    statement = _single_select(first.sql)
    projections = {
        projection.alias_or_name: projection.this for projection in statement.expressions
    }

    assert first == second
    assert first.executed is False
    assert first.dialect is SourceDialect.POSTGRESQL
    assert first.plan_version == 2
    assert first.request_fingerprint == "request-fingerprint"
    assert first.plan_fingerprint == "plan-fingerprint"
    assert first.sha256 == hashlib.sha256(first.sql.encode("utf-8")).hexdigest()
    assert "%s" not in first.sql
    assert not tuple(statement.find_all(exp.Placeholder))
    assert not tuple(statement.find_all(exp.Drop))

    assert isinstance(projections["text_value"], exp.Literal)
    assert projections["text_value"].is_string
    assert projections["text_value"].this == (
        "O'Reilly; DROP TABLE commerce.sale_lines_enriched; --"
    )
    assert _numeric_literal(projections["integer_value"]) == "42"
    assert _numeric_literal(projections["decimal_value"]) == "12.3400"
    assert isinstance(projections["boolean_value"], exp.Boolean)
    assert projections["boolean_value"].this is True
    _assert_typed_temporal(
        projections["date_value"],
        expected_type=exp.DataType.Type.DATE,
        expected_value="2026-01-31",
    )
    _assert_typed_temporal(
        projections["timestamp_value"],
        expected_type=exp.DataType.Type.TIMESTAMP,
        expected_value="2026-01-31 12:34:56.123456",
    )
    assert isinstance(projections["null_value"], exp.Null)


def test_copyable_artifact_repr_does_not_expose_literalized_sql() -> None:
    canary = "literal-canary-must-not-reach-logs"
    artifact = CopyableSqlArtifact(
        sql=f"SELECT '{canary}' AS protected_value LIMIT 1",
        dialect=SourceDialect.POSTGRESQL,
        plan_version=2,
        request_fingerprint="request-fingerprint",
        plan_fingerprint="plan-fingerprint",
        sha256="a" * 64,
    )

    assert canary not in repr(artifact)
    assert "sql=" not in repr(artifact)
    assert canary in artifact.sql


@pytest.mark.parametrize(
    "parameters",
    [
        (),
        ("extra", "binding"),
    ],
)
def test_copyable_renderer_rejects_placeholder_count_mismatch(
    parameters: tuple[str, ...],
) -> None:
    query = ValidatedQuery(
        sql="SELECT %s AS value LIMIT 1",
        parameters=parameters,
        max_rows=1,
        statement_timeout_ms=5_000,
        plan_version=2,
    )

    with pytest.raises(QueryCompilationError) as captured:
        PostgresCopyableSqlRenderer().render(
            query,
            request_fingerprint="request-fingerprint",
            plan_fingerprint="plan-fingerprint",
        )

    assert captured.value.code == "parameter_count_mismatch"


@pytest.mark.parametrize(
    "unsafe",
    [
        float("nan"),
        float("inf"),
        Decimal("NaN"),
        Decimal("Infinity"),
    ],
)
def test_copyable_renderer_rejects_non_finite_numeric_literals(
    unsafe: float | Decimal,
) -> None:
    query = ValidatedQuery(
        sql="SELECT %s AS value LIMIT 1",
        parameters=(unsafe,),
        max_rows=1,
        statement_timeout_ms=5_000,
        plan_version=2,
    )

    with pytest.raises(QueryCompilationError) as captured:
        PostgresCopyableSqlRenderer().render(
            query,
            request_fingerprint="request-fingerprint",
            plan_fingerprint="plan-fingerprint",
        )

    assert captured.value.code == "unsafe_literal"


@pytest.mark.parametrize(
    "mutation",
    (
        {"dialect": "bigquery"},
        {"plan_version": 1},
        {"request_fingerprint": "forged-request"},
        {"plan_fingerprint": "forged-plan"},
        {"target_fingerprint": "f" * 64},
        {"sha256": "0" * 64},
    ),
)
def test_copy_builder_rejects_renderer_metadata_forgery(
    mutation: dict[str, object],
) -> None:
    policy = build_m32_reference_policy()
    compiled = PostgresQueryCompiler().compile(
        build_m32_reference_plan(),
        max_preview_rows=policy.max_preview_rows,
    )
    validated = SqlGlotPolicyGuard().validate(compiled, policy)

    class ForgedRenderer:
        def render(
            self,
            query: ValidatedQuery,
            *,
            request_fingerprint: str,
            plan_fingerprint: str,
        ) -> CopyableSqlArtifact:
            legitimate = PostgresCopyableSqlRenderer().render(
                query,
                request_fingerprint=request_fingerprint,
                plan_fingerprint=plan_fingerprint,
            )
            return replace(legitimate, **mutation)

    with pytest.raises(RuntimeError, match="metadata outside"):
        BuildCopyableSql(
            renderer=ForgedRenderer(),
            guard=SqlGlotPolicyGuard(),
            policy=policy,
        ).execute(
            validated,
            request_fingerprint="request-fingerprint",
            plan_fingerprint="plan-fingerprint",
        )


def _single_select(sql: str) -> exp.Select:
    statements = tuple(statement for statement in parse(sql, read="postgres") if statement)
    assert len(statements) == 1
    statement = statements[0]
    assert isinstance(statement, exp.Select)
    return statement


def _numeric_literal(expression: exp.Expression) -> str:
    assert isinstance(expression, exp.Literal)
    assert not expression.is_string
    return str(expression.this)


def _literal_scalar(expression: exp.Expression) -> str:
    assert isinstance(expression, exp.Literal)
    return str(expression.this)


def _assert_typed_temporal(
    expression: exp.Expression,
    *,
    expected_type: exp.DataType.Type,
    expected_value: str,
) -> None:
    assert isinstance(expression, exp.Cast)
    target = expression.args.get("to")
    assert isinstance(target, exp.DataType)
    assert target.this is expected_type
    source = expression.this
    assert isinstance(source, exp.Literal)
    assert source.is_string
    assert source.this == expected_value
