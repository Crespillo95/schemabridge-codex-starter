"""Fail-closed replay of one versioned synthetic north-star observation."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlglot import exp, parse
from sqlglot.errors import ParseError

from schemabridge.application.ports.planning import (
    PlanningPortError,
    PlanningPortErrorCode,
    ProtectedSourceOperationCancelled,
)
from schemabridge.application.query_execution import (
    QueryPreviewRejectedError,
    QueryPreviewResult,
    ValidatedQuery,
)
from schemabridge.domain.connectors import GovernedExecutionTarget
from schemabridge.domain.resolution import (
    RejectedSourceRecord,
    RejectedSourceReport,
    RejectionCheck,
)
from schemabridge.domain.workflows import fingerprint_payload

_OUTPUT_ALIAS = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class RecordedDemoExecutionAdapter:
    """Replay only the exact guarded query and rejection checks in an audited fixture."""

    def __init__(self, path: Path) -> None:
        self._path = path
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("recorded demo execution fixture is unavailable or invalid") from error
        if not isinstance(payload, dict) or payload.get("schema_version") != 2:
            raise ValueError("recorded demo execution fixture has an unsupported schema")
        self._payload = payload

    @property
    def source_label(self) -> str:
        return self._required_string("source")

    def execute(
        self,
        query: ValidatedQuery,
        *,
        target: GovernedExecutionTarget | None = None,
    ) -> QueryPreviewResult:
        if target is not None:
            raise QueryPreviewRejectedError(
                "recorded execution cannot replay a managed connector target"
            )
        observed = fingerprint_payload(
            {
                "sql": query.sql,
                "parameters": list(query.parameters),
                "max_rows": query.max_rows,
                "statement_timeout_ms": query.statement_timeout_ms,
            }
        )
        output_aliases: tuple[str, ...] | None = None
        if observed != self._required_string("query_fingerprint"):
            alias_insensitive, output_aliases = _alias_insensitive_query_facts(query)
            if alias_insensitive != self._required_string("alias_insensitive_query_fingerprint"):
                raise QueryPreviewRejectedError(
                    "recorded execution is available only for the exact versioned "
                    "north-star query semantics"
                )
        preview = self._required_mapping("preview")
        if preview.get("statement_timeout_ms") != query.statement_timeout_ms:
            raise QueryPreviewRejectedError(
                "recorded execution timeout does not match the guarded query"
            )
        columns = preview.get("columns")
        rows = preview.get("rows")
        if not isinstance(columns, list) or not all(isinstance(item, str) for item in columns):
            raise QueryPreviewRejectedError("recorded execution columns are invalid")
        if not isinstance(rows, list) or not all(isinstance(item, list) for item in rows):
            raise QueryPreviewRejectedError("recorded execution rows are invalid")
        if any(len(row) != len(columns) for row in rows) or any(
            value is not None and not isinstance(value, str | int | float | bool)
            for row in rows
            for value in row
        ):
            raise QueryPreviewRejectedError("recorded execution row values are invalid")
        if output_aliases is not None:
            if len(output_aliases) != len(columns):
                raise QueryPreviewRejectedError(
                    "recorded execution projection aliases are incompatible"
                )
            columns = list(output_aliases)
        return QueryPreviewResult(
            columns=tuple(columns),
            rows=tuple(tuple(row) for row in rows),
            database_user=self._mapping_string(preview, "database_user"),
            transaction_read_only=self._mapping_bool(preview, "transaction_read_only"),
            statement_timeout_ms=query.statement_timeout_ms,
            truncated=self._mapping_bool(preview, "truncated"),
        )

    def inspect(
        self,
        checks: tuple[RejectionCheck, ...],
        *,
        statement_timeout_ms: int,
        should_continue: Callable[[], bool] | None = None,
        target: GovernedExecutionTarget | None = None,
    ) -> RejectedSourceReport:
        if target is not None:
            raise PlanningPortError(
                PlanningPortErrorCode.REJECTION_INSPECTION_FORBIDDEN,
                "recorded rejection evidence cannot replay a managed connector target",
            )
        if should_continue is not None and not should_continue():
            raise ProtectedSourceOperationCancelled(
                "protected source operation was cooperatively cancelled"
            )
        observed = fingerprint_payload(
            [
                {
                    "logical_field": check.logical_field.root,
                    "physical_field": check.physical_field.root,
                    "transformation_plan": check.transformation_plan.model_dump(mode="json"),
                }
                for check in checks
            ]
        )
        if observed != self._required_string("rejection_checks_fingerprint"):
            raise PlanningPortError(
                PlanningPortErrorCode.REJECTION_INSPECTION_FORBIDDEN,
                "recorded rejection evidence does not match the governed source checks",
            )
        if statement_timeout_ms != self._required_int("statement_timeout_ms"):
            raise PlanningPortError(
                PlanningPortErrorCode.REJECTION_INSPECTION_FORBIDDEN,
                "recorded rejection timeout does not match the guarded query",
            )
        raw_records = self._payload.get("rejections")
        if not isinstance(raw_records, list):
            raise PlanningPortError(
                PlanningPortErrorCode.REJECTION_INSPECTION_UNAVAILABLE,
                "recorded rejection evidence is invalid",
            )
        try:
            records = tuple(RejectedSourceRecord.model_validate(item) for item in raw_records)
            return RejectedSourceReport(
                inspected_fields=tuple(check.physical_field for check in checks),
                records=records,
                total_records=len(records),
                truncated=False,
                database_user=self._required_string("database_user"),
                transaction_read_only=self._required_bool("transaction_read_only"),
                statement_timeout_ms=statement_timeout_ms,
            )
        except ValueError as error:
            raise PlanningPortError(
                PlanningPortErrorCode.REJECTION_INSPECTION_UNAVAILABLE,
                "recorded rejection evidence is invalid",
            ) from error

    def _required_mapping(self, key: str) -> dict[str, Any]:
        value = self._payload.get(key)
        if not isinstance(value, dict):
            raise ValueError(f"recorded demo field {key} must be an object")
        return value

    def _required_string(self, key: str) -> str:
        return self._mapping_string(self._payload, key)

    def _required_bool(self, key: str) -> bool:
        return self._mapping_bool(self._payload, key)

    def _required_int(self, key: str) -> int:
        value = self._payload.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"recorded demo field {key} must be an integer")
        return value

    @staticmethod
    def _mapping_string(mapping: dict[str, Any], key: str) -> str:
        value = mapping.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"recorded demo field {key} must be a non-empty string")
        return value

    @staticmethod
    def _mapping_bool(mapping: dict[str, Any], key: str) -> bool:
        value = mapping.get(key)
        if not isinstance(value, bool):
            raise ValueError(f"recorded demo field {key} must be a boolean")
        return value


def _alias_insensitive_query_facts(
    query: ValidatedQuery,
) -> tuple[str, tuple[str, ...]]:
    """Fingerprint guarded semantics while allowing top-level presentation aliases only."""

    try:
        statements = tuple(
            statement for statement in parse(query.sql, read="postgres") if statement
        )
    except ParseError as error:
        raise QueryPreviewRejectedError("recorded execution SQL could not be reparsed") from error
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise QueryPreviewRejectedError("recorded execution SQL is not one SELECT")
    statement = statements[0]
    aliases: list[str] = []
    for index, projection in enumerate(statement.expressions):
        if not isinstance(projection, exp.Alias):
            raise QueryPreviewRejectedError(
                "recorded execution requires explicit top-level projection aliases"
            )
        alias = projection.alias
        if _OUTPUT_ALIAS.fullmatch(alias) is None:
            raise QueryPreviewRejectedError("recorded execution projection alias is invalid")
        aliases.append(alias)
        projection.set("alias", exp.to_identifier(f"recorded_column_{index + 1}"))
    canonical_sql = statement.sql(dialect="postgres", pretty=False)
    return (
        fingerprint_payload(
            {
                "sql": canonical_sql,
                "parameters": list(query.parameters),
                "max_rows": query.max_rows,
                "statement_timeout_ms": query.statement_timeout_ms,
            }
        ),
        tuple(aliases),
    )
