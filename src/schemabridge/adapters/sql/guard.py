"""Independent SQLGlot policy guard for final PostgreSQL text."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NoReturn

from sqlglot import exp, parse
from sqlglot.errors import ParseError

from schemabridge.application.query_execution import (
    CompiledQuery,
    SqlPolicyFinding,
    SqlPolicyViolation,
    SqlRejectionCode,
    ValidatedQuery,
)
from schemabridge.domain.plans import QueryPolicy

_COMMENT_TOKEN = re.compile(r"--|/\*|\*/")
_FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Alter,
    exp.Drop,
    exp.TruncateTable,
    exp.Copy,
    exp.Command,
    exp.Set,
    exp.Grant,
    exp.Revoke,
    exp.Lock,
)
_SAFE_FUNCTIONS = frozenset(
    {
        "AVG",
        "CAST",
        "CASE",
        "COALESCE",
        "COUNT",
        "DATE_TRUNC",
        "IF",
        "LPAD",
        "MAX",
        "MIN",
        "NULLIF",
        "OR",
        "REGEXP_LIKE",
        "SUM",
        "TIMESTAMP_TRUNC",
        "TRIM",
        "TRUNC",
    }
)


def _raise(code: SqlRejectionCode, message: str) -> NoReturn:
    raise SqlPolicyViolation((SqlPolicyFinding(code=code, message=message),))


@dataclass(frozen=True, slots=True)
class SqlGlotPolicyGuard:
    """Reparse final SQL and fail closed before any executor receives it."""

    def validate(self, query: CompiledQuery, policy: QueryPolicy) -> ValidatedQuery:
        if _COMMENT_TOKEN.search(query.sql) is not None:
            _raise(
                SqlRejectionCode.COMMENTS_FORBIDDEN,
                "SQL comments are forbidden in executable previews",
            )

        try:
            statements = tuple(
                statement for statement in parse(query.sql, read="postgres") if statement
            )
        except ParseError as error:
            raise SqlPolicyViolation(
                (
                    SqlPolicyFinding(
                        code=SqlRejectionCode.PARSE_ERROR,
                        message="final PostgreSQL could not be parsed",
                    ),
                )
            ) from error

        if len(statements) != 1:
            _raise(
                SqlRejectionCode.MULTIPLE_STATEMENTS,
                "exactly one SQL statement is permitted",
            )
        statement = statements[0]
        if not isinstance(statement, exp.Select):
            _raise(
                SqlRejectionCode.NON_READ_ONLY_STATEMENT,
                "only SELECT or WITH ... SELECT is permitted",
            )
        if any(isinstance(node, _FORBIDDEN_NODES) for node in statement.walk()):
            _raise(
                SqlRejectionCode.FORBIDDEN_STATEMENT,
                "DDL, DML, and utility statements are forbidden, including inside CTEs",
            )
        if statement.find(exp.Into) is not None:
            _raise(SqlRejectionCode.SELECT_INTO, "SELECT INTO is forbidden")
        with_clause = statement.args.get("with_")
        if isinstance(with_clause, exp.With) and with_clause.args.get("recursive"):
            _raise(
                SqlRejectionCode.RECURSIVE_CTE,
                "recursive CTEs are outside the restricted preview policy",
            )
        if statement.find(exp.Star) is not None:
            _raise(
                SqlRejectionCode.WILDCARD_PROJECTION,
                "wildcard projections are outside the restricted preview policy",
            )

        placeholders = sum(1 for _ in statement.find_all(exp.Placeholder))
        if placeholders != len(query.parameters):
            _raise(
                SqlRejectionCode.PARAMETER_MISMATCH,
                "placeholder count does not match bound parameter count",
            )

        cte_aliases = {cte.alias_or_name for cte in statement.find_all(exp.CTE)}
        allowed_columns = {asset.dataset.root: frozenset(asset.columns) for asset in policy.assets}
        physical_tables: list[exp.Table] = []
        aliases: dict[str, str] = {}
        observed_assets: set[str] = set()
        for table in statement.find_all(exp.Table):
            if not table.db and table.name in cte_aliases:
                continue
            if table.catalog or not table.db:
                _raise(
                    SqlRejectionCode.UNKNOWN_ASSET,
                    f"asset must be schema-qualified and allowlisted: {table.sql()}",
                )
            asset = f"{table.db}.{table.name}"
            if asset not in allowed_columns:
                _raise(SqlRejectionCode.UNKNOWN_ASSET, f"asset is not allowlisted: {asset}")
            if asset in observed_assets:
                _raise(
                    SqlRejectionCode.REPEATED_ASSET,
                    f"self joins and repeated assets are outside the MVP: {asset}",
                )
            observed_assets.add(asset)
            physical_tables.append(table)
            alias = table.alias_or_name
            if alias in aliases:
                _raise(
                    SqlRejectionCode.DUPLICATE_ALIAS,
                    f"relation aliases must be unique: {alias}",
                )
            aliases[alias] = asset

        if len(physical_tables) > policy.max_tables:
            _raise(
                SqlRejectionCode.TOO_MANY_TABLES,
                f"query references {len(physical_tables)} tables; maximum is {policy.max_tables}",
            )

        for column in statement.find_all(exp.Column):
            if column.table in cte_aliases:
                continue
            if column.table:
                resolved_asset = aliases.get(column.table)
                if resolved_asset is None or column.name not in allowed_columns[resolved_asset]:
                    _raise(
                        SqlRejectionCode.UNKNOWN_COLUMN,
                        f"column is not allowlisted: {column.sql()}",
                    )
                continue
            candidates = [
                asset for asset in aliases.values() if column.name in allowed_columns[asset]
            ]
            if len(set(candidates)) != 1:
                _raise(
                    SqlRejectionCode.UNKNOWN_COLUMN,
                    f"unqualified column is unknown or ambiguous: {column.name}",
                )

        for function in statement.find_all(exp.Func):
            function_name = function.sql_name()  # type: ignore[no-untyped-call]
            if function_name.upper() not in _SAFE_FUNCTIONS:
                _raise(
                    SqlRejectionCode.UNSAFE_FUNCTION,
                    f"function is not allowlisted: {function_name}",
                )

        self._validate_join_scopes(statement)

        limit = statement.args.get("limit")
        if not isinstance(limit, exp.Limit):
            _raise(
                SqlRejectionCode.MISSING_PREVIEW_LIMIT,
                "preview query must contain an explicit LIMIT",
            )
        limit_expression = limit.expression
        if not isinstance(limit_expression, exp.Literal) or limit_expression.is_string:
            _raise(
                SqlRejectionCode.INVALID_PREVIEW_LIMIT,
                "preview LIMIT must be a positive integer literal",
            )
        try:
            limit_value = int(limit_expression.this)
        except (TypeError, ValueError) as error:
            raise SqlPolicyViolation(
                (
                    SqlPolicyFinding(
                        code=SqlRejectionCode.INVALID_PREVIEW_LIMIT,
                        message="preview LIMIT must be a positive integer literal",
                    ),
                )
            ) from error
        if (
            limit_value < 1
            or limit_value > policy.max_preview_rows
            or limit_value != query.effective_limit
        ):
            _raise(
                SqlRejectionCode.INVALID_PREVIEW_LIMIT,
                "preview LIMIT exceeds policy or disagrees with compiler metadata",
            )

        return ValidatedQuery(
            sql=query.sql,
            parameters=query.parameters,
            max_rows=limit_value,
            statement_timeout_ms=policy.statement_timeout_ms,
        )

    @staticmethod
    def _validate_join_scopes(statement: exp.Select) -> None:
        for select in statement.find_all(exp.Select):
            from_clause = select.args.get("from_")
            if not isinstance(from_clause, exp.From):
                available_relations: set[str] = set()
            else:
                root_relation = from_clause.this.alias_or_name
                available_relations = {root_relation} if root_relation else set()

            for join in select.args.get("joins") or ():
                if join.kind.upper() == "CROSS":
                    _raise(SqlRejectionCode.CARTESIAN_JOIN, "CROSS JOIN is forbidden")
                on = join.args.get("on")
                using = join.args.get("using")
                if on is None and using is None:
                    _raise(
                        SqlRejectionCode.CARTESIAN_JOIN,
                        "every joined table requires an explicit predicate",
                    )
                if on is None:
                    _raise(
                        SqlRejectionCode.MISSING_JOIN_PREDICATE,
                        "USING and NATURAL joins are outside the restricted policy",
                    )

                joined_relation = join.this.alias_or_name
                if (
                    not joined_relation
                    or not available_relations
                    or not SqlGlotPolicyGuard._connects_joined_relation(
                        on,
                        joined_relation,
                        available_relations,
                    )
                ):
                    _raise(
                        SqlRejectionCode.MISSING_JOIN_PREDICATE,
                        "join predicate must connect the joined relation to an existing relation",
                    )
                available_relations.add(joined_relation)

    @staticmethod
    def _connects_joined_relation(
        expression: exp.Expression,
        joined_relation: str,
        available_relations: set[str],
    ) -> bool:
        if isinstance(expression, exp.Paren):
            return SqlGlotPolicyGuard._connects_joined_relation(
                expression.this,
                joined_relation,
                available_relations,
            )
        if isinstance(expression, exp.And):
            return SqlGlotPolicyGuard._connects_joined_relation(
                expression.this,
                joined_relation,
                available_relations,
            ) and SqlGlotPolicyGuard._connects_joined_relation(
                expression.expression,
                joined_relation,
                available_relations,
            )
        if not isinstance(expression, exp.EQ):
            return False
        left_relations = {
            column.table for column in expression.this.find_all(exp.Column) if column.table
        }
        right_relations = {
            column.table for column in expression.expression.find_all(exp.Column) if column.table
        }
        if len(left_relations) != 1 or len(right_relations) != 1:
            return False
        left_relation = next(iter(left_relations))
        right_relation = next(iter(right_relations))
        return (left_relation == joined_relation and right_relation in available_relations) or (
            right_relation == joined_relation and left_relation in available_relations
        )
