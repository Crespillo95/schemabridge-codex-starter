"""Psycopg bounded read-only query preview adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

import psycopg

from schemabridge.adapters.connectors.source_identity import (
    require_postgres_source_identity,
)
from schemabridge.adapters.postgres.transient_errors import (
    is_transient_postgres_error,
)
from schemabridge.application.query_execution import (
    QueryPreviewRejectedError,
    QueryPreviewResult,
    QueryPreviewTimeoutError,
    QueryPreviewUnavailableError,
    ValidatedQuery,
)
from schemabridge.domain.connectors import GovernedExecutionTarget, SourceDialect


@dataclass(frozen=True, slots=True)
class PsycopgQueryPreview:
    """Execute guarded SQL with independent transaction and timeout controls."""

    dsn: str = field(repr=False)
    expected_user: str = "schemabridge_reader"
    bound_target_fingerprint: str | None = field(default=None, repr=False)
    connect_timeout_seconds: int = 3
    max_rows_limit: int = 500
    max_timeout_ms: int = 5_000

    def execute(
        self,
        query: ValidatedQuery,
        *,
        target: GovernedExecutionTarget | None = None,
    ) -> QueryPreviewResult:
        if target is None:
            if self.bound_target_fingerprint is not None or query.target_fingerprint is not None:
                raise QueryPreviewRejectedError(
                    "a managed PostgreSQL preview requires its exact connector target"
                )
        elif (
            self.bound_target_fingerprint is None
            or self.bound_target_fingerprint != target.fingerprint
            or query.target_fingerprint != target.fingerprint
            or query.dialect is not SourceDialect.POSTGRESQL
            or target.dialect is not SourceDialect.POSTGRESQL
            or self.expected_user != target.expected_reader
        ):
            raise QueryPreviewRejectedError(
                "the PostgreSQL preview connector target does not match"
            )
        if not 1 <= query.max_rows <= self.max_rows_limit:
            raise QueryPreviewRejectedError(
                "PostgreSQL preview row limit exceeds the configured safety cap"
            )
        if not 10 <= query.statement_timeout_ms <= self.max_timeout_ms:
            raise QueryPreviewRejectedError(
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
                            current_setting('statement_timeout'),
                            COALESCE(inet_server_addr()::TEXT, 'local_socket'),
                            COALESCE(inet_server_port(), 0),
                            current_database()
                        """
                    )
                    safety_row = cursor.fetchone()
                    if safety_row is None:
                        raise QueryPreviewRejectedError(
                            "PostgreSQL preview safety inspection returned no result"
                        )
                    if len(safety_row) < 3:
                        raise QueryPreviewRejectedError(
                            "PostgreSQL preview safety inspection returned invalid evidence"
                        )
                    user, transaction_read_only, timeout_setting = cast(
                        tuple[str, bool, str],
                        safety_row[:3],
                    )
                    timeout_ms = _postgres_interval_to_milliseconds(timeout_setting)
                    if user != self.expected_user:
                        raise QueryPreviewRejectedError(
                            "PostgreSQL preview identity is not the configured read-only role"
                        )
                    if not transaction_read_only:
                        raise QueryPreviewRejectedError(
                            "PostgreSQL preview transaction is not read-only"
                        )
                    if timeout_ms != query.statement_timeout_ms:
                        raise QueryPreviewRejectedError(
                            "PostgreSQL preview statement timeout was not applied"
                        )
                    if target is not None:
                        require_postgres_source_identity(
                            expected_fingerprint=target.source_identity_fingerprint,
                            server_address=safety_row[3] if len(safety_row) > 3 else None,
                            server_port=safety_row[4] if len(safety_row) > 4 else None,
                            database=safety_row[5] if len(safety_row) > 5 else None,
                            user=user,
                        )

                    cursor.execute(query.sql, query.parameters)
                    description = cursor.description
                    if description is None:
                        raise QueryPreviewRejectedError(
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
        except (QueryPreviewRejectedError, QueryPreviewUnavailableError):
            raise
        except psycopg.Error as error:
            if is_transient_postgres_error(error):
                raise QueryPreviewUnavailableError("PostgreSQL preview failed") from error
            raise QueryPreviewRejectedError("PostgreSQL preview was rejected") from error
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
