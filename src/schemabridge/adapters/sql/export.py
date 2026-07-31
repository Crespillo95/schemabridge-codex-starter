"""Typed PostgreSQL literal renderer for copy/paste presentation only."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlglot import exp, parse
from sqlglot.errors import ErrorLevel, ParseError

from schemabridge.application.query_execution import (
    QueryCompilationError,
    ValidatedQuery,
)
from schemabridge.application.sql_export import CopyableSqlArtifact
from schemabridge.domain.connectors import SourceDialect
from schemabridge.domain.plans import ParameterScalar

_DOLLAR_QUOTE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")


@dataclass(frozen=True, slots=True)
class PostgresCopyableSqlRenderer:
    """Replace validated driver bindings with typed AST literals; never execute them."""

    def render(
        self,
        query: ValidatedQuery,
        *,
        request_fingerprint: str,
        plan_fingerprint: str,
    ) -> CopyableSqlArtifact:
        if query.dialect is not SourceDialect.POSTGRESQL:
            raise QueryCompilationError(
                "dialect_unsupported",
                "standalone SQL rendering currently supports PostgreSQL only",
            )
        if "\x00" in query.sql:
            raise QueryCompilationError(
                "unsafe_literal",
                "SQL text cannot contain NUL bytes",
            )
        try:
            original = tuple(
                statement for statement in parse(query.sql, read="postgres") if statement
            )
        except ParseError as error:
            raise QueryCompilationError(
                "parse_error",
                "validated PostgreSQL could not be reparsed for presentation",
            ) from error
        if len(original) != 1 or not isinstance(original[0], exp.Select):
            raise QueryCompilationError(
                "unsafe_query_shape",
                "copy rendering requires exactly one SELECT statement",
            )
        ast_placeholder_count = sum(1 for _ in original[0].find_all(exp.Placeholder))
        if ast_placeholder_count != len(query.parameters):
            raise QueryCompilationError(
                "parameter_count_mismatch",
                "validated placeholder count does not match typed bindings",
            )

        numbered_sql, marker_count = _number_placeholders(query.sql)
        if marker_count != ast_placeholder_count:
            raise QueryCompilationError(
                "parameter_count_mismatch",
                "textual placeholder positions disagree with the parsed query",
            )
        try:
            numbered = tuple(
                statement for statement in parse(numbered_sql, read="postgres") if statement
            )
        except ParseError as error:
            raise QueryCompilationError(
                "parse_error",
                "numbered presentation SQL could not be parsed",
            ) from error
        if len(numbered) != 1 or not isinstance(numbered[0], exp.Select):
            raise QueryCompilationError(
                "unsafe_query_shape",
                "numbered presentation SQL changed the statement shape",
            )
        positions: list[int] = []
        for parameter in numbered[0].find_all(exp.Parameter):
            marker = parameter.this
            if not isinstance(marker, exp.Literal) or marker.is_string:
                raise QueryCompilationError(
                    "parameter_count_mismatch",
                    "presentation marker is not a numeric positional binding",
                )
            try:
                positions.append(int(marker.this))
            except (TypeError, ValueError) as error:
                raise QueryCompilationError(
                    "parameter_count_mismatch",
                    "presentation marker is not a numeric positional binding",
                ) from error
        expected = list(range(1, len(query.parameters) + 1))
        if sorted(positions) != expected:
            raise QueryCompilationError(
                "parameter_count_mismatch",
                "every typed binding must occur exactly once",
            )

        def replace(node: exp.Expression) -> exp.Expression:
            if not isinstance(node, exp.Parameter):
                return node
            marker = node.this
            assert isinstance(marker, exp.Literal)
            position = int(marker.this)
            return _typed_literal(query.parameters[position - 1])

        literalized = numbered[0].transform(replace, copy=True)
        if (
            literalized.find(exp.Parameter) is not None
            or literalized.find(exp.Placeholder) is not None
        ):
            raise QueryCompilationError(
                "parameter_count_mismatch",
                "presentation SQL retained an unresolved parameter",
            )
        try:
            sql = literalized.sql(
                dialect="postgres",
                pretty=True,
                unsupported_level=ErrorLevel.RAISE,
            )
            reparsed = tuple(statement for statement in parse(sql, read="postgres") if statement)
        except Exception as error:
            raise QueryCompilationError(
                "postgres_render_failed",
                "typed standalone SQL could not be rendered and reparsed",
            ) from error
        if (
            len(reparsed) != 1
            or not isinstance(reparsed[0], exp.Select)
            or reparsed[0].find(exp.Parameter) is not None
            or reparsed[0].find(exp.Placeholder) is not None
        ):
            raise QueryCompilationError(
                "unsafe_query_shape",
                "standalone SQL did not preserve one parameter-free SELECT",
            )
        return CopyableSqlArtifact(
            sql=sql,
            dialect=query.dialect,
            plan_version=query.plan_version,
            request_fingerprint=request_fingerprint,
            plan_fingerprint=plan_fingerprint,
            sha256=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            target_fingerprint=query.target_fingerprint,
        )


def _typed_literal(value: ParameterScalar) -> exp.Expression:
    if value is None:
        return exp.Null()
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, int):
        return exp.Literal.number(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise QueryCompilationError(
                "unsafe_literal",
                "copyable numeric literals must be finite",
            )
        return exp.Literal.number(repr(value))
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise QueryCompilationError(
                "unsafe_literal",
                "copyable decimal literals must be finite",
            )
        return exp.Literal.number(format(value, "f"))
    if isinstance(value, datetime):
        temporal_type = "TIMESTAMPTZ" if value.tzinfo is not None else "TIMESTAMP"
        return exp.Cast(
            this=exp.Literal.string(value.isoformat(sep=" ")),
            to=exp.DataType.build(temporal_type, dialect="postgres"),
        )
    if isinstance(value, date):
        return exp.Cast(
            this=exp.Literal.string(value.isoformat()),
            to=exp.DataType.build("DATE"),
        )
    if isinstance(value, str):
        if "\x00" in value:
            raise QueryCompilationError(
                "unsafe_literal",
                "copyable text literals cannot contain NUL bytes",
            )
        return exp.Literal.string(value)
    raise QueryCompilationError(
        "unsafe_literal",
        f"unsupported copyable literal type: {type(value).__name__}",
    )


def _number_placeholders(sql: str) -> tuple[str, int]:
    """Replace only standalone, unquoted psycopg markers with $n tokens."""

    output: list[str] = []
    index = 0
    marker_count = 0
    while index < len(sql):
        character = sql[index]
        if character in {"'", '"'}:
            quote = character
            start = index
            index += 1
            while index < len(sql):
                if sql[index] == quote:
                    if index + 1 < len(sql) and sql[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                if sql[index] == "\\" and index + 1 < len(sql):
                    index += 2
                else:
                    index += 1
            output.append(sql[start:index])
            continue
        if character == "$":
            matched = _DOLLAR_QUOTE.match(sql, index)
            if matched is not None:
                delimiter = matched.group(0)
                end = sql.find(delimiter, matched.end())
                if end != -1:
                    end += len(delimiter)
                    output.append(sql[index:end])
                    index = end
                    continue
        if sql.startswith("%s", index):
            previous = sql[index - 1] if index else ""
            following = sql[index + 2] if index + 2 < len(sql) else ""
            if not (previous.isalnum() or previous in {"_", "$"}) and not (
                following.isalnum() or following == "_"
            ):
                marker_count += 1
                output.append(f"${marker_count}")
                index += 2
                continue
        output.append(character)
        index += 1
    return "".join(output), marker_count
