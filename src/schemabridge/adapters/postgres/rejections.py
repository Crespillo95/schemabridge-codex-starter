"""Bounded PostgreSQL inspection of rejected join-key source records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import psycopg
from psycopg import sql

from schemabridge.application.ports.planning import PlanningPortError, PlanningPortErrorCode
from schemabridge.domain.resolution import (
    RejectedSourceRecord,
    RejectedSourceReport,
    RejectionCheck,
    SourceRejectionCode,
)
from schemabridge.domain.transformations import ValidateRegexStep

_MAX_SAFE_FLOAT_INTEGER = 9_007_199_254_740_991
_REASONS = {
    SourceRejectionCode.NULL_JOIN_KEY: "NULL join keys are preserved as rejected source records.",
    SourceRejectionCode.NEGATIVE_IDENTIFIER: "Negative identifiers are not supported.",
    SourceRejectionCode.NON_FINITE_IDENTIFIER: "NaN and infinity cannot be normalized safely.",
    SourceRejectionCode.NON_INTEGRAL_IDENTIFIER: (
        "Fractional identifiers are rejected without truncation."
    ),
    SourceRejectionCode.UNSAFE_FLOAT_IDENTIFIER: (
        "The float is outside the exact integer range and may have lost precision."
    ),
    SourceRejectionCode.MALFORMED_IDENTIFIER: (
        "The string identifier does not match its approved digit-only representation."
    ),
}


@dataclass(frozen=True, slots=True)
class PsycopgRejectedSourceReporter:
    """Inspect only explicit approved fields under reader, timeout, and read-only controls."""

    dsn: str
    allowed_fields: frozenset[str]
    expected_user: str = "schemabridge_reader"
    connect_timeout_seconds: int = 3
    max_records: int = 500

    def __post_init__(self) -> None:
        if self.max_records < 1:
            raise ValueError("rejected-source record cap must be positive")

    def inspect(
        self,
        checks: tuple[RejectionCheck, ...],
        *,
        statement_timeout_ms: int,
    ) -> RejectedSourceReport:
        if not checks:
            return RejectedSourceReport()
        if any(check.physical_field.root not in self.allowed_fields for check in checks):
            raise PlanningPortError(
                PlanningPortErrorCode.REJECTION_INSPECTION_FORBIDDEN,
                "rejected-source inspection requested a field outside the approved allowlist",
            )

        inspected = tuple(dict.fromkeys(check.physical_field for check in checks))
        records: list[RejectedSourceRecord] = []
        total_records = 0
        try:
            with psycopg.connect(
                self.dsn,
                connect_timeout=self.connect_timeout_seconds,
                autocommit=False,
            ) as connection:
                connection.read_only = True
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (str(statement_timeout_ms),),
                    )
                    cursor.fetchone()
                    cursor.execute(
                        """
                        SELECT
                            current_user,
                            current_setting('transaction_read_only')::BOOLEAN,
                            current_setting('statement_timeout')
                        """
                    )
                    safety = cursor.fetchone()
                    if safety is None:
                        raise PlanningPortError(
                            PlanningPortErrorCode.REJECTION_INSPECTION_UNAVAILABLE,
                            "rejected-source safety inspection returned no result",
                        )
                    user, read_only, timeout_setting = cast(tuple[str, bool, str], safety)
                    timeout_ms = _postgres_interval_to_milliseconds(timeout_setting)
                    if user != self.expected_user or not read_only:
                        raise PlanningPortError(
                            PlanningPortErrorCode.REJECTION_INSPECTION_FORBIDDEN,
                            "rejected-source inspection is not using the approved read-only role",
                        )
                    if timeout_ms != statement_timeout_ms:
                        raise PlanningPortError(
                            PlanningPortErrorCode.REJECTION_INSPECTION_UNAVAILABLE,
                            "rejected-source statement timeout was not applied",
                        )
                    for check in checks:
                        remaining = max(self.max_records - len(records), 0)
                        query, parameters = _inspection_query(check, limit=remaining + 1)
                        cursor.execute(query, parameters)
                        rows = cursor.fetchmany(remaining + 1)
                        check_total = int(rows[0][2]) if rows else 0
                        total_records += check_total
                        for source_value, code_value, _ in rows[:remaining]:
                            code = SourceRejectionCode(str(code_value))
                            records.append(
                                RejectedSourceRecord(
                                    logical_field=check.logical_field,
                                    physical_field=check.physical_field,
                                    source_value=cast(str | None, source_value),
                                    code=code,
                                    reason=_REASONS[code],
                                )
                            )
                connection.rollback()
        except PlanningPortError:
            raise
        except (psycopg.Error, ValueError) as error:
            raise PlanningPortError(
                PlanningPortErrorCode.REJECTION_INSPECTION_UNAVAILABLE,
                "rejected-source inspection failed",
            ) from error

        return RejectedSourceReport(
            inspected_fields=inspected,
            records=tuple(records),
            total_records=total_records,
            truncated=total_records > len(records),
            database_user=user,
            transaction_read_only=read_only,
            statement_timeout_ms=timeout_ms,
        )


def _inspection_query(
    check: RejectionCheck,
    *,
    limit: int,
) -> tuple[sql.Composed, tuple[object, ...]]:
    if limit < 1:
        raise ValueError("rejected-source inspection limit must be positive")
    schema_name, table_name, column_name = check.physical_field.root.split(".")
    operations = tuple(step.operation for step in check.transformation_plan.steps)
    field = sql.Identifier(column_name)
    table = sql.Identifier(schema_name, table_name)
    if "validate_finite" in operations and "validate_integral" in operations:
        rejection = sql.SQL(
            """
            CASE
              WHEN {field} IS NULL THEN 'null_join_key'
              WHEN {field} IN ('NaN'::DOUBLE PRECISION, 'Infinity'::DOUBLE PRECISION,
                               '-Infinity'::DOUBLE PRECISION) THEN 'non_finite_identifier'
              WHEN {field} <> TRUNC({field}) THEN 'non_integral_identifier'
              WHEN {field} < 0 THEN 'negative_identifier'
              WHEN {field} > {max_safe} THEN 'unsafe_float_identifier'
              ELSE NULL
            END
            """
        ).format(field=field, max_safe=sql.Literal(_MAX_SAFE_FLOAT_INTEGER))
        parameters: tuple[object, ...] = ()
    elif "validate_regex" in operations:
        regex_steps = tuple(
            step for step in check.transformation_plan.steps if isinstance(step, ValidateRegexStep)
        )
        if len(regex_steps) != 1:
            raise ValueError("approved string rejection inspection requires one regex")
        rejection = sql.SQL(
            """
            CASE
              WHEN {field} IS NULL THEN 'null_join_key'
              WHEN BTRIM({field}) ~ '^-[0-9]+$' THEN 'negative_identifier'
              WHEN BTRIM({field}) !~ %s THEN 'malformed_identifier'
              ELSE NULL
            END
            """
        ).format(field=field)
        parameters = (regex_steps[0].pattern,)
    else:
        raise ValueError("approved rejection inspection does not support this transformation")

    query = sql.SQL(
        """
        SELECT source_value, rejection_code, COUNT(*) OVER () AS total_records
        FROM (
          SELECT {field}::TEXT AS source_value, {rejection} AS rejection_code
          FROM {table}
        ) AS rejected
        WHERE rejection_code IS NOT NULL
        ORDER BY source_value NULLS LAST
        LIMIT {limit}
        """
    ).format(
        field=field,
        rejection=rejection,
        table=table,
        limit=sql.Literal(limit),
    )
    return query, parameters


def _postgres_interval_to_milliseconds(value: str) -> int:
    if value.endswith("ms"):
        return int(value.removesuffix("ms"))
    if value.endswith("min"):
        return int(float(value.removesuffix("min")) * 60_000)
    if value.endswith("s"):
        return int(float(value.removesuffix("s")) * 1_000)
    return int(value)
