"""M32 regressions for scoped CTE and closed-window SQL guarding."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

import pytest

from schemabridge.adapters.sql.guard import SqlGlotPolicyGuard
from schemabridge.application.query_demo import build_demo_query_policy
from schemabridge.application.query_execution import (
    CompiledQuery,
    SqlPolicyViolation,
    ValidatedQuery,
)
from schemabridge.domain.plans import ParameterScalar


def _validate(
    sql: str,
    *,
    parameters: Sequence[ParameterScalar] = (),
    effective_limit: int = 10,
    plan_version: Literal[1, 2] = 1,
) -> ValidatedQuery:
    return SqlGlotPolicyGuard().validate(
        CompiledQuery(
            sql=sql,
            parameters=tuple(parameters),
            effective_limit=effective_limit,
            plan_version=plan_version,
        ),
        build_demo_query_policy(),
    )


def test_guard_accepts_bounded_and_predicates() -> None:
    validated = _validate(
        """
        SELECT c.customer_id
        FROM crm.customers AS c
        WHERE c.customer_status = %s
          AND c.country_cd = %s
        LIMIT 10
        """,
        parameters=("ACTIVE", "ES"),
    )

    assert validated.max_rows == 10
    assert validated.parameters == ("ACTIVE", "ES")


def test_guard_accepts_compiler_owned_advanced_window_stages() -> None:
    validated = _validate(
        """
        WITH aggregated AS (
            SELECT
                c.country_cd AS country_code,
                CAST(
                    DATE_TRUNC('month', c.registration_date) AS DATE
                ) AS registration_month,
                COUNT(c.customer_id) AS customer_count
            FROM crm.customers AS c
            WHERE c.customer_status = %s
              AND c.country_cd = %s
            GROUP BY
                c.country_cd,
                CAST(DATE_TRUNC('month', c.registration_date) AS DATE)
            HAVING COUNT(c.customer_id) >= %s
        ),
        windowed AS (
            SELECT
                aggregated.country_code,
                aggregated.registration_month,
                aggregated.customer_count,
                ROW_NUMBER() OVER (
                    PARTITION BY aggregated.country_code
                    ORDER BY
                        aggregated.customer_count DESC,
                        aggregated.registration_month ASC
                ) AS position,
                SUM(aggregated.customer_count) OVER (
                    PARTITION BY aggregated.country_code
                    ORDER BY aggregated.registration_month ASC
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) AS cumulative_count,
                ROUND(
                    100.0 * aggregated.customer_count
                    / NULLIF(
                        SUM(aggregated.customer_count) OVER (
                            PARTITION BY aggregated.country_code
                        ),
                        0
                    ),
                    2
                ) AS percentage_of_total
            FROM aggregated
        )
        SELECT
            windowed.country_code,
            windowed.registration_month,
            windowed.customer_count,
            windowed.position,
            windowed.cumulative_count,
            windowed.percentage_of_total
        FROM windowed
        WHERE windowed.position <= %s
        ORDER BY
            windowed.country_code,
            windowed.position
        LIMIT 10
        """,
        parameters=("ACTIVE", "ES", 1, 3),
        plan_version=2,
    )

    assert validated.max_rows == 10
    assert validated.parameters == ("ACTIVE", "ES", 1, 3)


@pytest.mark.parametrize(
    "sql",
    (
        pytest.param(
            """
            WITH active AS (
                SELECT c.customer_id AS customer_id
                FROM crm.customers AS c
            )
            SELECT active.secret_value
            FROM active
            LIMIT 10
            """,
            id="unknown-cte-output",
        ),
        pytest.param(
            """
            WITH first_stage AS (
                SELECT later_stage.customer_id AS customer_id
                FROM later_stage
            ),
            later_stage AS (
                SELECT c.customer_id AS customer_id
                FROM crm.customers AS c
            )
            SELECT first_stage.customer_id
            FROM first_stage
            LIMIT 10
            """,
            id="forward-cte-reference",
        ),
        pytest.param(
            """
            WITH RECURSIVE chain(customer_id) AS (
                SELECT c.customer_id
                FROM crm.customers AS c
                UNION ALL
                SELECT chain.customer_id
                FROM chain
            )
            SELECT chain.customer_id
            FROM chain
            LIMIT 10
            """,
            id="recursive-cte",
        ),
        pytest.param(
            """
            WITH stage_one AS (
                SELECT c.customer_id AS customer_id
                FROM crm.customers AS c
            ),
            stage_two AS (
                SELECT stage_one.customer_id AS customer_id
                FROM stage_one
            ),
            stage_three AS (
                SELECT stage_two.customer_id AS customer_id
                FROM stage_two
            )
            SELECT stage_three.customer_id
            FROM stage_three
            LIMIT 10
            """,
            id="more-than-two-ctes",
        ),
    ),
)
def test_guard_rejects_invalid_cte_scope_or_topology(sql: str) -> None:
    with pytest.raises(SqlPolicyViolation):
        _validate(sql, plan_version=2)


@pytest.mark.parametrize(
    ("sql", "parameters"),
    (
        pytest.param(
            """
            SELECT DISTINCT c.customer_id
            FROM crm.customers AS c
            LIMIT 10
            """,
            (),
            id="select-distinct",
        ),
        pytest.param(
            """
            SELECT c.customer_id
            FROM crm.customers AS c
            LIMIT 10 OFFSET 1
            """,
            (),
            id="offset",
        ),
        pytest.param(
            """
            SELECT (SELECT c.customer_id) AS customer_id
            FROM crm.customers AS c
            LIMIT 10
            """,
            (),
            id="scalar-subquery",
        ),
        pytest.param(
            """
            WITH combined AS (
                SELECT c.customer_id AS customer_id
                FROM crm.customers AS c
                UNION ALL
                SELECT 'synthetic' AS customer_id
            )
            SELECT combined.customer_id
            FROM combined
            LIMIT 10
            """,
            (),
            id="set-operation-inside-cte",
        ),
        pytest.param(
            """
            WITH active AS (
                SELECT c.customer_id AS customer_id
                FROM crm.customers AS c
                WHERE c.customer_status = %s /* concealed instruction */
            )
            SELECT active.customer_id
            FROM active
            LIMIT 10
            """,
            ("ACTIVE",),
            id="comment-inside-cte",
        ),
        pytest.param(
            """
            SELECT
                COUNT(c.customer_id) FILTER (
                    WHERE c.customer_status = %s
                ) AS active_customers
            FROM crm.customers AS c
            LIMIT 10
            """,
            ("ACTIVE",),
            id="aggregate-filter",
        ),
    ),
)
def test_guard_rejects_advanced_sql_outside_the_closed_language(
    sql: str,
    parameters: tuple[ParameterScalar, ...],
) -> None:
    with pytest.raises(SqlPolicyViolation):
        _validate(sql, parameters=parameters, plan_version=2)


@pytest.mark.parametrize(
    "window_expression",
    (
        pytest.param(
            """
            PERCENT_RANK() OVER (
                ORDER BY aggregated.customer_count DESC
            )
            """,
            id="unapproved-window-function",
        ),
        pytest.param(
            """
            ROW_NUMBER() OVER (
                ORDER BY aggregated.customer_count DESC
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            )
            """,
            id="ranking-window-with-forged-frame",
        ),
        pytest.param(
            """
            SUM(aggregated.customer_count) OVER (
                ORDER BY aggregated.registration_date
                RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            )
            """,
            id="range-frame",
        ),
        pytest.param(
            """
            SUM(aggregated.customer_count) OVER (
                ORDER BY aggregated.registration_date
                ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING
            )
            """,
            id="forward-looking-frame",
        ),
        pytest.param(
            """
            SUM(aggregated.unknown_output) OVER (
                ORDER BY aggregated.registration_date
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            )
            """,
            id="unknown-cte-window-input",
        ),
    ),
)
def test_guard_rejects_unapproved_window_forms(window_expression: str) -> None:
    sql = f"""
        WITH aggregated AS (
            SELECT
                c.registration_date AS registration_date,
                COUNT(c.customer_id) AS customer_count
            FROM crm.customers AS c
            GROUP BY c.registration_date
        ),
        windowed AS (
            SELECT
                aggregated.registration_date,
                {window_expression} AS derived_value
            FROM aggregated
        )
        SELECT
            windowed.registration_date,
            windowed.derived_value
        FROM windowed
        ORDER BY windowed.registration_date
        LIMIT 10
    """

    with pytest.raises(SqlPolicyViolation):
        _validate(sql, plan_version=2)


def test_guard_rejects_more_than_four_derived_window_outputs() -> None:
    derived = ",\n".join(
        (f"ROW_NUMBER() OVER (ORDER BY aggregated.registration_date) AS derived_{position}")
        for position in range(1, 6)
    )
    selected = ",\n".join(f"windowed.derived_{position}" for position in range(1, 6))
    sql = f"""
        WITH aggregated AS (
            SELECT
                c.registration_date AS registration_date,
                COUNT(c.customer_id) AS customer_count
            FROM crm.customers AS c
            GROUP BY c.registration_date
        ),
        windowed AS (
            SELECT
                aggregated.registration_date,
                aggregated.customer_count,
                {derived}
            FROM aggregated
        )
        SELECT
            windowed.registration_date,
            windowed.customer_count,
            {selected}
        FROM windowed
        ORDER BY windowed.registration_date
        LIMIT 10
    """

    with pytest.raises(SqlPolicyViolation, match="unsafe_window"):
        _validate(sql, plan_version=2)


@pytest.mark.parametrize(
    "sql",
    (
        ("SELECT c.customer_id || c.customer_id AS forged FROM crm.customers AS c LIMIT 10"),
        ("SELECT c.customer_id % c.customer_id AS forged FROM crm.customers AS c LIMIT 10"),
        (
            "SELECT CASE WHEN c.customer_status = 'ACTIVE' "
            "THEN c.customer_id ELSE c.country_cd END AS forged "
            "FROM crm.customers AS c LIMIT 10"
        ),
        ("SELECT c.customer_id FROM crm.customers AS c WHERE c.customer_status LIKE 'A%' LIMIT 10"),
        (
            "SELECT c.customer_id, h.holder_link_id "
            "FROM crm.customers AS c "
            "JOIN bank.account_holders AS h "
            "ON c.customer_status = h.holder_type "
            "LIMIT 10"
        ),
        (
            "SELECT c.customer_id FROM crm.customers AS c "
            "WHERE c.customer_id = c.country_cd LIMIT 10"
        ),
        ("SELECT c.customer_id FROM crm.customers AS c WHERE c.customer_status ~ '.*' LIMIT 10"),
        ("SELECT COALESCE(c.customer_id, c.country_cd) AS forged FROM crm.customers AS c LIMIT 10"),
        ("SELECT NULLIF(c.customer_id, c.country_cd) AS forged FROM crm.customers AS c LIMIT 10"),
        ("SELECT CAST(c.customer_status AS INTEGER) AS forged FROM crm.customers AS c LIMIT 10"),
        (
            "SELECT CASE WHEN c.customer_status = 'ACTIVE' "
            "THEN c.customer_id ELSE NULL END AS forged "
            "FROM crm.customers AS c LIMIT 10"
        ),
        (
            "SELECT COUNT(CASE WHEN c.customer_status = 'ACTIVE' "
            "THEN c.customer_id ELSE NULL END) AS forged "
            "FROM crm.customers AS c LIMIT 10"
        ),
        (
            "SELECT SUM(CASE WHEN c.customer_status = 'ACTIVE' "
            "THEN c.customer_id ELSE NULL END) AS forged "
            "FROM crm.customers AS c LIMIT 10"
        ),
        ("SELECT COUNT('anything') AS forged FROM crm.customers AS c LIMIT 10"),
        ("SELECT c.customer_id FROM crm.customers AS c WHERE 1 = 1 LIMIT 10"),
        (
            "SELECT CASE 'ACTIVE' WHEN 'ACTIVE' THEN 'A' ELSE NULL END AS forged "
            "FROM crm.customers AS c LIMIT 10"
        ),
        (
            "SELECT DATE_TRUNC('month', c.registration_date) AS forged "
            "FROM crm.customers AS c LIMIT 10"
        ),
        ("SELECT ROUND(c.customer_id, 2) AS forged FROM crm.customers AS c LIMIT 10"),
    ),
)
def test_guard_rejects_untyped_scalar_and_predicate_operators(sql: str) -> None:
    with pytest.raises(SqlPolicyViolation):
        _validate(sql)
