"""Psycopg bounded read-only query preview adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import psycopg

from schemabridge.application.query_execution import (
    QueryPreviewResult,
    QueryPreviewTimeoutError,
    QueryPreviewUnavailableError,
    ValidatedQuery,
)


@dataclass(frozen=True, slots=True)
class PsycopgQueryPreview:
    """Execute guarded SQL with independent transaction and timeout controls."""

    dsn: str
    expected_user: str = "schemabridge_reader"
    connect_timeout_seconds: int = 3
    max_rows_limit: int = 500
    max_timeout_ms: int = 5_000

    def execute(self, query: ValidatedQuery) -> QueryPreviewResult:
        if not 1 <= query.max_rows <= self.max_rows_limit:
            raise QueryPreviewUnavailableError(
                "PostgreSQL preview row limit exceeds the configured safety cap"
            )
        if not 10 <= query.statement_timeout_ms <= self.max_timeout_ms:
            raise QueryPreviewUnavailableError(
                "PostgreSQL preview timeout exceeds the configured safety cap"
            )
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
                        (str(query.statement_timeout_ms),),
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
                    safety_row = cursor.fetchone()
                    if safety_row is None:
                        raise QueryPreviewUnavailableError(
                            "PostgreSQL preview safety inspection returned no result"
                        )
                    user, transaction_read_only, timeout_setting = cast(
                        tuple[str, bool, str], safety_row
                    )
                    timeout_ms = _postgres_interval_to_milliseconds(timeout_setting)
                    if user != self.expected_user:
                        raise QueryPreviewUnavailableError(
                            "PostgreSQL preview identity is not the configured read-only role"
                        )
                    if not transaction_read_only:
                        raise QueryPreviewUnavailableError(
                            "PostgreSQL preview transaction is not read-only"
                        )
                    if timeout_ms != query.statement_timeout_ms:
                        raise QueryPreviewUnavailableError(
                            "PostgreSQL preview statement timeout was not applied"
                        )

                    cursor.execute(query.sql, query.parameters)
                    description = cursor.description
                    if description is None:
                        raise QueryPreviewUnavailableError(
                            "PostgreSQL preview returned no result columns"
                        )
                    fetched = cursor.fetchmany(query.max_rows + 1)
                    truncated = len(fetched) > query.max_rows
                    rows = tuple(tuple(row) for row in fetched[: query.max_rows])
                    columns = tuple(column.name for column in description)
                connection.rollback()
        except psycopg.errors.QueryCanceled as error:
            raise QueryPreviewTimeoutError(
                "PostgreSQL preview exceeded the statement timeout"
            ) from error
        except QueryPreviewUnavailableError:
            raise
        except psycopg.Error as error:
            raise QueryPreviewUnavailableError("PostgreSQL preview failed") from error
        return QueryPreviewResult(
            columns=columns,
            rows=rows,
            database_user=user,
            transaction_read_only=transaction_read_only,
            statement_timeout_ms=timeout_ms,
            truncated=truncated,
        )


def _postgres_interval_to_milliseconds(value: str) -> int:
    """Parse PostgreSQL's bounded statement_timeout display values."""

    if value.endswith("ms"):
        return int(value.removesuffix("ms"))
    if value.endswith("min"):
        return int(float(value.removesuffix("min")) * 60_000)
    if value.endswith("s"):
        return int(float(value.removesuffix("s")) * 1_000)
    return int(value)
