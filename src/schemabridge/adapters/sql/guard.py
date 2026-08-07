"""Independent SQLGlot policy guard for final PostgreSQL text."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import NoReturn

from sqlglot import Tokenizer, exp, parse
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import Scope, traverse_scope

from schemabridge.application.query_execution import (
    CompiledQuery,
    SqlPolicyFinding,
    SqlPolicyViolation,
    SqlRejectionCode,
    ValidatedQuery,
)
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    SourceDialect,
    governed_execution_target_fingerprint,
)
from schemabridge.domain.joins import (
    Cardinality,
    FanoutPolicy,
    JoinType,
    NormalizedJoinKey,
)
from schemabridge.domain.plans import QueryPolicy
from schemabridge.domain.transformations import (
    CastIntegerToStringStep,
    CastTimestampToDateStep,
    EmptyToNullStep,
    IdentityStep,
    MapValuesStep,
    NormalizeDecimalScaleStep,
    PadLeftStep,
    ParseDateStep,
    PreserveNullStep,
    RejectInvalidStep,
    StripLeadingZerosStep,
    TrimStep,
    ValidateFiniteStep,
    ValidateIntegralStep,
    ValidateRegexStep,
)

_MAX_SQL_BYTES = 131_072
_MAX_AST_NODES = 5_000
_MAX_SELECTS = 3
_MAX_CTES = 2
_MAX_WINDOW_OUTPUTS = 4
_MAX_WINDOW_NODES = 8
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
_FORBIDDEN_OPERATORS = (
    exp.DPipe,
    exp.Mod,
    exp.Like,
    exp.ILike,
    exp.BitwiseAnd,
    exp.BitwiseOr,
    exp.BitwiseXor,
    exp.BitwiseLeftShift,
    exp.BitwiseRightShift,
    exp.Neg,
    exp.Between,
    exp.NullSafeEQ,
    exp.NullSafeNEQ,
    exp.SimilarTo,
    exp.Pow,
    exp.IntDiv,
    exp.Xor,
)
_SAFE_FUNCTIONS = frozenset(
    {
        "AVG",
        "AND",
        "CAST",
        "CASE",
        "COALESCE",
        "COUNT",
        "DATE_TRUNC",
        "IF",
        "LPAD",
        "MAX",
        "MIN",
        "LAG",
        "LEAD",
        "NULLIF",
        "OR",
        "RANK",
        "REGEXP_LIKE",
        "ROUND",
        "ROW_NUMBER",
        "SUM",
        "TIMESTAMP_TRUNC",
        "TRIM",
        "TRUNC",
        "DENSE_RANK",
        "NTILE",
    }
)


def _raise(code: SqlRejectionCode, message: str) -> NoReturn:
    raise SqlPolicyViolation((SqlPolicyFinding(code=code, message=message),))


def _bound_literal(value: object) -> exp.Expression:
    """Represent one already-bound driver value for structural AST comparison."""

    if value is None:
        return exp.Null()
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, (int, float, Decimal)):
        return exp.Literal.number(str(value))
    if isinstance(value, (date, datetime)):
        return exp.Literal.string(value.isoformat())
    return exp.Literal.string(str(value))


def _materialize_parameters(
    sql: str,
    parameters: tuple[object, ...],
) -> exp.Select:
    """Reparse SQL after safe token-position replacement in driver order."""

    tokens = Tokenizer(dialect="postgres").tokenize(sql)
    positions: list[tuple[int, int]] = []
    for index, token in enumerate(tokens[:-1]):
        next_token = tokens[index + 1]
        if (
            token.token_type.name == "MOD"
            and token.text == "%"
            and next_token.token_type.name == "VAR"
            and next_token.text.lower() == "s"
            and next_token.start == token.end + 1
        ):
            positions.append((token.start, next_token.end))
    if len(positions) != len(parameters):
        _raise(
            SqlRejectionCode.PARAMETER_MISMATCH,
            "placeholder count does not match bound parameter count",
        )
    materialized_sql = sql
    for (start, end), value in reversed(tuple(zip(positions, parameters, strict=True))):
        literal_sql = _bound_literal(value).sql(dialect="postgres")
        materialized_sql = materialized_sql[:start] + literal_sql + materialized_sql[end + 1 :]
    try:
        statements = tuple(item for item in parse(materialized_sql, read="postgres") if item)
    except ParseError as error:
        raise SqlPolicyViolation(
            (
                SqlPolicyFinding(
                    code=SqlRejectionCode.PARAMETER_MISMATCH,
                    message="bound parameters could not be materialized safely",
                ),
            )
        ) from error
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        _raise(
            SqlRejectionCode.PARAMETER_MISMATCH,
            "bound parameters changed the validated statement shape",
        )
    return statements[0]


def _strip_leading_zeros(expression: exp.Expression) -> exp.Expression:
    return exp.Coalesce(
        this=exp.Nullif(
            this=exp.Trim(
                this=expression,
                expression=exp.Literal.string("0"),
                position="LEADING",
            ),
            expression=exp.Literal.string(""),
        ),
        expressions=[exp.Literal.string("0")],
    )


def _expected_join_key(
    key: NormalizedJoinKey,
    *,
    relation: str,
) -> exp.Expression:
    """Independently reconstruct the one AST shape authorized by a join key."""

    current: exp.Expression = exp.column(
        key.physical_field.root.rsplit(".", maxsplit=1)[1],
        table=relation,
    )
    validity_conditions: list[exp.Expression] = []
    invalidity_conditions: list[exp.Expression] = []
    cast_integer_to_string = False
    float_identifier_validation = False

    for step in key.transformation_plan.steps:
        if isinstance(step, (IdentityStep, PreserveNullStep, RejectInvalidStep)):
            continue
        if isinstance(step, TrimStep):
            current = exp.Trim(this=current)
            continue
        if isinstance(step, EmptyToNullStep):
            current = exp.Nullif(this=current, expression=exp.Literal.string(""))
            continue
        if isinstance(step, ValidateRegexStep):
            validity_conditions.append(
                exp.RegexpLike(
                    this=current.copy(),
                    expression=exp.Literal.string(step.pattern),
                )
            )
            continue
        if isinstance(step, ValidateFiniteStep):
            float_identifier_validation = True
            double_type = exp.DataType.build("DOUBLE PRECISION", dialect="postgres")
            unsafe_values = [
                exp.Cast(this=exp.Literal.string(value), to=double_type.copy())
                for value in ("NaN", "Infinity", "-Infinity")
            ]
            invalidity_conditions.extend(
                (
                    exp.Is(this=current.copy(), expression=exp.Null()),
                    exp.In(this=current.copy(), expressions=unsafe_values),
                )
            )
            continue
        if isinstance(step, ValidateIntegralStep):
            invalidity_conditions.append(
                exp.NEQ(
                    this=current.copy(),
                    expression=exp.func("TRUNC", current.copy()),
                )
            )
            continue
        if isinstance(step, StripLeadingZerosStep):
            current = _strip_leading_zeros(current)
            continue
        if isinstance(step, PadLeftStep):
            current = exp.func(
                "LPAD",
                _strip_leading_zeros(current),
                exp.Literal.number(step.length),
                exp.Literal.string(step.fill_character),
            )
            continue
        if isinstance(step, CastIntegerToStringStep):
            invalidity_conditions.append(
                exp.LT(this=current.copy(), expression=exp.Literal.number(0))
            )
            if float_identifier_validation:
                invalidity_conditions.append(
                    exp.GT(
                        this=current.copy(),
                        expression=exp.Literal.number(9_007_199_254_740_991),
                    )
                )
            cast_integer_to_string = True
            continue
        if isinstance(step, CastTimestampToDateStep):
            current = exp.Cast(this=current, to=exp.DataType.build("DATE"))
            continue
        if isinstance(step, ParseDateStep):
            current = exp.func(
                "TO_DATE",
                current,
                exp.Literal.string(step.format),
            )
            continue
        if isinstance(step, NormalizeDecimalScaleStep):
            current = exp.func(
                "ROUND",
                exp.Cast(this=current, to=exp.DataType.build("NUMERIC")),
                exp.Literal.number(step.scale),
            )
            continue
        if isinstance(step, MapValuesStep):
            current = exp.Case(
                this=current,
                ifs=[
                    exp.If(
                        this=_bound_literal(entry.source),
                        true=_bound_literal(entry.target),
                    )
                    for entry in step.entries
                ],
                default=exp.Null(),
            )
            continue
        _raise(
            SqlRejectionCode.MISSING_JOIN_PREDICATE,
            "approved join key contains an unsupported normalization operation",
        )

    if cast_integer_to_string:
        current = exp.Cast(
            this=exp.Cast(this=current, to=exp.DataType.build("BIGINT")),
            to=exp.DataType.build("TEXT"),
        )
    if invalidity_conditions:
        current = exp.Case(
            ifs=[exp.If(this=exp.or_(*invalidity_conditions), true=exp.Null())],
            default=current,
        )
    if validity_conditions:
        current = exp.Case(
            ifs=[exp.If(this=exp.and_(*validity_conditions), true=current)],
            default=exp.Null(),
        )
    return current


def _reverse_cardinality(cardinality: Cardinality) -> Cardinality:
    if cardinality is Cardinality.ONE_TO_MANY:
        return Cardinality.MANY_TO_ONE
    if cardinality is Cardinality.MANY_TO_ONE:
        return Cardinality.ONE_TO_MANY
    return cardinality


def _unwrap_parentheses(expression: exp.Expression) -> exp.Expression:
    current = expression
    while isinstance(current, exp.Paren):
        current = current.this
    return current


def _is_bound_value(
    expression: exp.Expression | None,
    *,
    allow_null: bool = False,
) -> bool:
    if isinstance(expression, (exp.Placeholder, exp.Literal, exp.Boolean)):
        return True
    if isinstance(expression, exp.Cast) and isinstance(expression.this, exp.Literal):
        target = expression.args.get("to")
        if isinstance(target, exp.DataType):
            return target.sql(dialect="postgres").upper() in {
                "DATE",
                "TIMESTAMP",
                "TIMESTAMP WITH TIME ZONE",
                "TIMESTAMPTZ",
            }
    return allow_null and isinstance(expression, exp.Null)


def _literal_text(expression: exp.Expression | None) -> str | None:
    if not isinstance(expression, exp.Literal):
        return None
    return str(expression.this)


def _direct_boolean_children(
    expression: exp.Expression,
    connector: type[exp.Binary],
) -> tuple[exp.Expression, ...]:
    current = _unwrap_parentheses(expression)
    if isinstance(current, connector):
        return (
            *_direct_boolean_children(current.this, connector),
            *_direct_boolean_children(current.expression, connector),
        )
    return (current,)


@dataclass(frozen=True, slots=True)
class SqlGlotPolicyGuard:
    """Reparse final SQL and fail closed before any executor receives it."""

    dialect: SourceDialect = SourceDialect.POSTGRESQL

    def validate(
        self,
        query: CompiledQuery,
        policy: QueryPolicy,
        *,
        target: GovernedExecutionTarget | None = None,
    ) -> ValidatedQuery:
        if self.dialect is not SourceDialect.POSTGRESQL or query.dialect is not self.dialect:
            _raise(
                SqlRejectionCode.DIALECT_MISMATCH,
                "compiler output and independent guard must use the supported PostgreSQL dialect",
            )
        if target is None:
            if query.target_fingerprint is not None:
                _raise(
                    SqlRejectionCode.TARGET_MISMATCH,
                    "target-bound compiler output requires the exact governed target",
                )
        elif (
            target.dialect is not self.dialect
            or query.target_fingerprint != governed_execution_target_fingerprint(target)
        ):
            _raise(
                SqlRejectionCode.TARGET_MISMATCH,
                "compiler output does not match the exact governed execution target",
            )
        if len(query.sql.encode("utf-8")) > _MAX_SQL_BYTES:
            _raise(
                SqlRejectionCode.EXCESSIVE_COMPLEXITY,
                "SQL text exceeds the bounded policy size",
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

        if any(node.comments for statement in statements for node in statement.walk()):
            _raise(
                SqlRejectionCode.COMMENTS_FORBIDDEN,
                "SQL comments are forbidden outside typed string literals",
            )
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
        if any(isinstance(node, _FORBIDDEN_OPERATORS) for node in statement.walk()):
            _raise(
                SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                "SQL operator is outside the closed typed expression language",
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
        ast_nodes = sum(1 for _node in statement.walk())
        if ast_nodes > _MAX_AST_NODES:
            _raise(
                SqlRejectionCode.EXCESSIVE_COMPLEXITY,
                "SQL AST exceeds the bounded policy size",
            )
        ctes = tuple(with_clause.expressions) if isinstance(with_clause, exp.With) else ()
        if len(ctes) > _MAX_CTES:
            _raise(
                SqlRejectionCode.INVALID_CTE_TOPOLOGY,
                f"query has {len(ctes)} CTEs; maximum is {_MAX_CTES}",
            )
        if any(not isinstance(cte.this, exp.Select) for cte in ctes):
            _raise(
                SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                "every non-recursive CTE must contain exactly one SELECT",
            )
        selects = tuple(statement.find_all(exp.Select))
        if len(selects) > _MAX_SELECTS or len(selects) != 1 + len(ctes):
            _raise(
                SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                "subqueries and SELECT nodes outside compiler-owned CTE stages are forbidden",
            )
        if statement.find(exp.SetOperation) is not None:
            _raise(
                SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                "UNION, INTERSECT, and EXCEPT are outside the restricted language",
            )
        if query.plan_version == 1 and ctes:
            _raise(
                SqlRejectionCode.INVALID_CTE_TOPOLOGY,
                "version-1 compiler output cannot contain CTEs",
            )
        if any(select.args.get("distinct") for select in selects):
            _raise(
                SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                "SELECT DISTINCT is outside the restricted query shape",
            )
        if any(select.args.get("offset") is not None for select in selects):
            _raise(
                SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                "OFFSET is forbidden",
            )
        if statement.find(exp.Filter) is not None:
            _raise(
                SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                "aggregate FILTER clauses are outside the typed aggregate language",
            )
        if any(select.args.get("qualify") is not None for select in selects):
            _raise(
                SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                "QUALIFY is outside the PostgreSQL staged query shape",
            )
        if any(select.args.get("windows") for select in selects):
            _raise(
                SqlRejectionCode.UNSAFE_WINDOW,
                "named windows are outside the closed window language",
            )
        for select in selects:
            group = select.args.get("group")
            if isinstance(group, exp.Group):
                if group.args.get("cube") or group.args.get("grouping_sets"):
                    _raise(
                        SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                        "CUBE and GROUPING SETS are outside the restricted language",
                    )
                if group.args.get("rollup"):
                    _raise(
                        SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                        "ROLLUP is unsupported until subtotal NULL semantics are explicit",
                    )
        self._validate_cte_order(ctes)
        if query.plan_version == 2:
            self._validate_v2_topology(statement, ctes)
        elif statement.find(exp.Window) is not None:
            _raise(
                SqlRejectionCode.UNSAFE_WINDOW,
                "window expressions require a validated version-2 query plan",
            )
        self._validate_closed_expressions(statement, query.plan_version)
        self._validate_windows(statement)

        placeholders = sum(1 for _ in statement.find_all(exp.Placeholder))
        if placeholders != len(query.parameters):
            _raise(
                SqlRejectionCode.PARAMETER_MISMATCH,
                "placeholder count does not match bound parameter count",
            )
        materialized_statement = _materialize_parameters(
            query.sql,
            tuple(query.parameters),
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

        self._validate_column_scopes(
            statement,
            allowed_columns=allowed_columns,
            cte_aliases=cte_aliases,
        )

        for function in statement.find_all(exp.Func):
            function_name = function.sql_name()  # type: ignore[no-untyped-call]
            if function_name.upper() not in _SAFE_FUNCTIONS:
                _raise(
                    SqlRejectionCode.UNSAFE_FUNCTION,
                    f"function is not allowlisted: {function_name}",
                )

        self._validate_join_scopes(materialized_statement, policy)

        if any(
            select.args.get("limit") is not None for select in selects if select is not statement
        ):
            _raise(
                SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                "only the outer SELECT may carry LIMIT",
            )
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
            dialect=query.dialect,
            target_fingerprint=query.target_fingerprint,
            plan_version=query.plan_version,
        )

    @staticmethod
    def _validate_cte_order(ctes: tuple[exp.CTE, ...]) -> None:
        names = tuple(cte.alias_or_name for cte in ctes)
        if any(not name for name in names) or len(names) != len(set(names)):
            _raise(
                SqlRejectionCode.INVALID_CTE_TOPOLOGY,
                "CTE names must be explicit and unique",
            )
        all_names = set(names)
        available: set[str] = set()
        for cte in ctes:
            alias = cte.args.get("alias")
            if isinstance(alias, exp.TableAlias) and alias.args.get("columns"):
                _raise(
                    SqlRejectionCode.INVALID_CTE_TOPOLOGY,
                    "CTE output column lists are outside the compiler-owned topology",
                )
            select = cte.this
            assert isinstance(select, exp.Select)
            outputs = [item.alias_or_name for item in select.expressions]
            if any(not output for output in outputs) or len(outputs) != len(set(outputs)):
                _raise(
                    SqlRejectionCode.INVALID_CTE_TOPOLOGY,
                    f"CTE {cte.alias_or_name} must expose unique explicit columns",
                )
            for table in select.find_all(exp.Table):
                if not table.db and table.name in all_names and table.name not in available:
                    _raise(
                        SqlRejectionCode.INVALID_CTE_TOPOLOGY,
                        f"CTE {cte.alias_or_name} references a future or recursive CTE",
                    )
            available.add(cte.alias_or_name)

    @staticmethod
    def _validate_v2_topology(
        statement: exp.Select,
        ctes: tuple[exp.CTE, ...],
    ) -> None:
        windows = tuple(statement.find_all(exp.Window))
        if not ctes:
            if windows:
                _raise(
                    SqlRejectionCode.INVALID_CTE_TOPOLOGY,
                    "version-2 windows require aggregated and windowed stages",
                )
            return
        names = tuple(cte.alias_or_name for cte in ctes)
        if names not in {("aggregated",), ("aggregated", "windowed")}:
            _raise(
                SqlRejectionCode.INVALID_CTE_TOPOLOGY,
                "version-2 CTE names and order must be compiler-owned",
            )
        aggregated = ctes[0].this
        assert isinstance(aggregated, exp.Select)
        if tuple(aggregated.find_all(exp.Window)):
            _raise(
                SqlRejectionCode.INVALID_CTE_TOPOLOGY,
                "the aggregated stage cannot contain window expressions",
            )
        aggregated_from = aggregated.args.get("from_")
        if not isinstance(aggregated_from, exp.From):
            _raise(
                SqlRejectionCode.INVALID_CTE_TOPOLOGY,
                "the aggregated stage requires one governed physical root",
            )

        final_stage = "aggregated"
        if len(ctes) == 2:
            windowed = ctes[1].this
            assert isinstance(windowed, exp.Select)
            SqlGlotPolicyGuard._require_single_cte_source(
                windowed,
                expected="aggregated",
                stage="windowed",
            )
            if any(
                windowed.args.get(name) is not None
                for name in ("where", "group", "having", "qualify", "limit", "offset")
            ):
                _raise(
                    SqlRejectionCode.INVALID_CTE_TOPOLOGY,
                    "the windowed stage may only project base outputs and closed windows",
                )
            if not tuple(windowed.find_all(exp.Window)):
                _raise(
                    SqlRejectionCode.INVALID_CTE_TOPOLOGY,
                    "the windowed stage requires at least one closed window",
                )
            window_outputs = sum(
                any(projection.find_all(exp.Window)) for projection in windowed.expressions
            )
            if window_outputs > _MAX_WINDOW_OUTPUTS:
                _raise(
                    SqlRejectionCode.UNSAFE_WINDOW,
                    "the windowed stage exceeds four derived window outputs",
                )
            final_stage = "windowed"
        elif windows:
            _raise(
                SqlRejectionCode.INVALID_CTE_TOPOLOGY,
                "window expressions require the compiler-owned windowed stage",
            )

        SqlGlotPolicyGuard._require_single_cte_source(
            statement,
            expected=final_stage,
            stage="outer",
        )
        if any(
            statement.args.get(name) is not None
            for name in ("group", "having", "qualify", "offset")
        ):
            _raise(
                SqlRejectionCode.INVALID_CTE_TOPOLOGY,
                "the outer stage may only project, filter, order, and limit staged outputs",
            )
        if any(
            window
            for projection in statement.expressions
            for window in projection.find_all(exp.Window)
        ):
            _raise(
                SqlRejectionCode.INVALID_CTE_TOPOLOGY,
                "the outer stage cannot define new windows",
            )

    @staticmethod
    def _require_single_cte_source(
        select: exp.Select,
        *,
        expected: str,
        stage: str,
    ) -> None:
        from_clause = select.args.get("from_")
        source = from_clause.this if isinstance(from_clause, exp.From) else None
        if (
            not isinstance(source, exp.Table)
            or source.db
            or source.name != expected
            or source.alias
            or select.args.get("joins")
        ):
            _raise(
                SqlRejectionCode.INVALID_CTE_TOPOLOGY,
                f"the {stage} stage must read only from {expected}",
            )

    @staticmethod
    def _validate_closed_expressions(
        statement: exp.Select,
        plan_version: int,
    ) -> None:
        if statement.find(exp.Add) is not None:
            _raise(
                SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                "addition is outside the typed expression language",
            )
        for arithmetic_type in (exp.Sub, exp.Mul, exp.Div):
            for arithmetic in statement.find_all(arithmetic_type):
                parent = arithmetic.parent
                while parent is not None and not isinstance(parent, (exp.Alias, exp.Select)):
                    parent = parent.parent
                if (
                    plan_version != 2
                    or not isinstance(parent, exp.Alias)
                    or parent.find(exp.Window) is None
                ):
                    _raise(
                        SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                        "arithmetic is permitted only in compiler-owned window calculations",
                    )
        for clause in (
            *statement.find_all(exp.Where),
            *statement.find_all(exp.Having),
        ):
            SqlGlotPolicyGuard._validate_typed_predicate(
                clause.this,
                plan_version=plan_version,
            )
        for case in statement.find_all(exp.Case):
            if not SqlGlotPolicyGuard._is_closed_case(
                case,
                plan_version=plan_version,
            ):
                _raise(
                    SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                    "CASE expression is outside the closed typed shapes",
                )

        for coalesce in statement.find_all(exp.Coalesce):
            if not SqlGlotPolicyGuard._is_strip_leading_zeros(coalesce):
                _raise(
                    SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                    "COALESCE is permitted only for compiler-owned zero normalization",
                )

        for nullif in statement.find_all(exp.Nullif):
            if SqlGlotPolicyGuard._is_strip_zero_nullif(nullif):
                continue
            if _literal_text(nullif.expression) == "" and not _is_bound_value(
                nullif.this, allow_null=True
            ):
                continue
            if (
                _literal_text(nullif.expression) == "0"
                and nullif.this.find(exp.Window) is not None
                and isinstance(nullif.parent, exp.Div)
            ):
                continue
            _raise(
                SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                "NULLIF is outside the closed normalization/window shapes",
            )

        for cast in statement.find_all(exp.Cast):
            if not SqlGlotPolicyGuard._is_closed_cast(cast):
                _raise(
                    SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                    "CAST is outside the closed typed conversion shapes",
                )

        for regex in statement.find_all(exp.RegexpLike):
            conditional = regex.find_ancestor(exp.If)
            ancestor_case = regex.find_ancestor(exp.Case)
            if (
                not isinstance(conditional, exp.If)
                or not isinstance(ancestor_case, exp.Case)
                or not SqlGlotPolicyGuard._is_validity_case(ancestor_case)
            ):
                _raise(
                    SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                    "regular expressions are permitted only by approved normalization CASE",
                )

        for truncation in statement.find_all(exp.TimestampTrunc):
            unit = truncation.args.get("unit")
            if (
                truncation.this.find(exp.Column) is None
                or str(getattr(unit, "this", "")).upper()
                not in {"DAY", "WEEK", "MONTH", "QUARTER", "YEAR"}
                or not isinstance(truncation.parent, exp.Cast)
            ):
                _raise(
                    SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                    "DATE_TRUNC is outside the closed date-grain expression",
                )

        for rounding in statement.find_all(exp.Round):
            decimals = rounding.args.get("decimals")
            normalized_mapping = isinstance(rounding.this, exp.Cast)
            window_calculation = rounding.this.find(exp.Window) is not None
            if (
                not isinstance(decimals, exp.Literal)
                or decimals.is_string
                or not (normalized_mapping or window_calculation)
            ):
                _raise(
                    SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                    "ROUND is outside normalization or compiler-owned window arithmetic",
                )

        for aggregate in statement.find_all(exp.AggFunc):
            if not isinstance(aggregate, (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)):
                continue
            if not SqlGlotPolicyGuard._is_closed_aggregate(
                aggregate,
                plan_version=plan_version,
            ):
                _raise(
                    SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                    "aggregate source is outside the closed typed aggregate language",
                )

    @staticmethod
    def _validate_typed_predicate(
        expression: exp.Expression,
        *,
        plan_version: int,
    ) -> None:
        current = _unwrap_parentheses(expression)
        if isinstance(current, (exp.And, exp.Or)):
            SqlGlotPolicyGuard._validate_typed_predicate(
                current.this,
                plan_version=plan_version,
            )
            SqlGlotPolicyGuard._validate_typed_predicate(
                current.expression,
                plan_version=plan_version,
            )
            return
        if isinstance(current, exp.Not):
            SqlGlotPolicyGuard._validate_typed_predicate(
                current.this,
                plan_version=plan_version,
            )
            return
        if isinstance(current, exp.Is):
            if not isinstance(current.expression, exp.Null):
                _raise(
                    SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                    "IS predicates may compare only with NULL",
                )
            return
        if isinstance(current, exp.In):
            values = tuple(current.expressions)
            if not 1 <= len(values) <= 64 or any(not _is_bound_value(value) for value in values):
                _raise(
                    SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                    "IN predicates require one to 64 bound scalar values",
                )
            return
        if not isinstance(current, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
            _raise(
                SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                "predicate operator is outside the typed comparison language",
            )
        if current.this.find(exp.Column) is None and current.this.find(exp.AggFunc) is None:
            _raise(
                SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                "typed comparisons require a governed column, aggregate, or output",
            )
        left_columns = tuple(current.this.find_all(exp.Column))
        right_columns = tuple(current.expression.find_all(exp.Column))
        if left_columns and right_columns:
            column_relations = {
                column.table for column in (*left_columns, *right_columns) if column.table
            }
            output_comparison = (
                plan_version == 2
                and bool(column_relations)
                and column_relations <= {"aggregated", "windowed"}
            )
            aggregate_comparison = (
                current.this.find(exp.AggFunc) is not None
                and current.expression.find(exp.AggFunc) is not None
            )
            if not output_comparison and not aggregate_comparison:
                _raise(
                    SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                    "physical predicates cannot compare one column with another",
                )
        elif not _is_bound_value(current.expression):
            _raise(
                SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                "typed comparisons require a bound scalar or approved output comparison",
            )

    @staticmethod
    def _is_closed_case(
        case: exp.Case,
        *,
        plan_version: int,
    ) -> bool:
        branches = tuple(case.args.get("ifs") or ())
        if not branches or any(not isinstance(item, exp.If) for item in branches):
            return False
        default = case.args.get("default")
        if case.this is not None:
            return (
                case.this.find(exp.Column) is not None
                and not _is_bound_value(case.this, allow_null=True)
                and isinstance(default, exp.Null)
                and all(
                    _is_bound_value(item.this)
                    and _is_bound_value(item.args.get("true"), allow_null=True)
                    for item in branches
                )
            )
        if SqlGlotPolicyGuard._is_bucket_case(case, plan_version=plan_version):
            return True
        if SqlGlotPolicyGuard._is_conditional_aggregate_case(
            case,
            plan_version=plan_version,
        ):
            return True
        return SqlGlotPolicyGuard._is_validity_case(case) or SqlGlotPolicyGuard._is_invalidity_case(
            case
        )

    @staticmethod
    def _is_bucket_case(
        case: exp.Case,
        *,
        plan_version: int,
    ) -> bool:
        if plan_version != 2 or not _is_bound_value(case.args.get("default")):
            return False
        branches = tuple(case.args.get("ifs") or ())
        source_sql: str | None = None
        for branch in branches:
            if not isinstance(branch, exp.If) or not _is_bound_value(branch.args.get("true")):
                return False
            comparisons = _direct_boolean_children(branch.this, exp.And)
            if not comparisons:
                return False
            for comparison in comparisons:
                if not isinstance(comparison, (exp.GTE, exp.LT)):
                    return False
                if not _is_bound_value(comparison.expression):
                    return False
                candidate = comparison.this.sql(dialect="postgres")
                if source_sql is None:
                    source_sql = candidate
                elif candidate != source_sql:
                    return False
        return source_sql is not None

    @staticmethod
    def _is_conditional_aggregate_case(
        case: exp.Case,
        *,
        plan_version: int,
    ) -> bool:
        if plan_version != 2 or not isinstance(case.args.get("default"), exp.Null):
            return False
        branches = tuple(case.args.get("ifs") or ())
        aggregate = case.find_ancestor(exp.AggFunc)
        if len(branches) != 1 or not isinstance(aggregate, exp.AggFunc):
            return False
        branch = branches[0]
        assert isinstance(branch, exp.If)
        true_value = branch.args.get("true")
        if true_value is None or isinstance(true_value, exp.Null):
            return False
        if _is_bound_value(true_value):
            if not (
                isinstance(true_value, exp.Literal)
                and not true_value.is_string
                and _literal_text(true_value) == "1"
            ):
                return False
        elif true_value.find(exp.Column) is None:
            return False
        return SqlGlotPolicyGuard._is_physical_condition(branch.this)

    @staticmethod
    def _is_physical_condition(expression: exp.Expression) -> bool:
        current = _unwrap_parentheses(expression)
        if isinstance(current, (exp.And, exp.Or)):
            return SqlGlotPolicyGuard._is_physical_condition(
                current.this
            ) and SqlGlotPolicyGuard._is_physical_condition(current.expression)
        if isinstance(current, exp.Not):
            return SqlGlotPolicyGuard._is_physical_condition(current.this)
        if isinstance(current, exp.Is):
            return isinstance(current.expression, exp.Null)
        if isinstance(current, exp.In):
            return 1 <= len(current.expressions) <= 64 and all(
                _is_bound_value(value) for value in current.expressions
            )
        if isinstance(current, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
            return (
                current.this.find(exp.Column) is not None
                and current.expression.find(exp.Column) is None
                and _is_bound_value(current.expression)
            )
        return False

    @staticmethod
    def _is_validity_case(case: exp.Case) -> bool:
        if case.this is not None or not isinstance(case.args.get("default"), exp.Null):
            return False
        branches = tuple(case.args.get("ifs") or ())
        if len(branches) != 1:
            return False
        branch = branches[0]
        if not isinstance(branch, exp.If):
            return False
        true_value = branch.args.get("true")
        if true_value is None or _is_bound_value(true_value, allow_null=True):
            return False
        conditions = _direct_boolean_children(branch.this, exp.And)
        return bool(conditions) and all(
            isinstance(condition, exp.RegexpLike)
            and condition.this.find(exp.Column) is not None
            and _is_bound_value(condition.expression)
            for condition in conditions
        )

    @staticmethod
    def _is_invalidity_case(case: exp.Case) -> bool:
        if case.this is not None:
            return False
        branches = tuple(case.args.get("ifs") or ())
        default = case.args.get("default")
        if len(branches) != 1 or default is None or _is_bound_value(default, allow_null=True):
            return False
        branch = branches[0]
        if not isinstance(branch, exp.If) or not isinstance(branch.args.get("true"), exp.Null):
            return False
        conditions = _direct_boolean_children(branch.this, exp.Or)
        return bool(conditions) and all(
            SqlGlotPolicyGuard._is_normalization_invalidity(condition) for condition in conditions
        )

    @staticmethod
    def _is_normalization_invalidity(expression: exp.Expression) -> bool:
        current = _unwrap_parentheses(expression)
        if isinstance(current, exp.Is):
            return current.this.find(exp.Column) is not None and isinstance(
                current.expression, exp.Null
            )
        if isinstance(current, exp.In):
            return (
                current.this.find(exp.Column) is not None
                and bool(current.expressions)
                and all(
                    isinstance(item, exp.Cast)
                    and _literal_text(item.this) in {"NaN", "Infinity", "-Infinity"}
                    for item in current.expressions
                )
            )
        if isinstance(current, exp.NEQ):
            return (
                current.this.find(exp.Column) is not None
                and isinstance(current.expression, exp.Trunc)
                and current.this == current.expression.this
            )
        if isinstance(current, (exp.LT, exp.GT)):
            return (
                current.this.find(exp.Column) is not None
                and isinstance(current.expression, exp.Literal)
                and not current.expression.is_string
            )
        return False

    @staticmethod
    def _is_strip_zero_nullif(nullif: exp.Nullif) -> bool:
        return (
            _literal_text(nullif.expression) == ""
            and isinstance(nullif.this, exp.Trim)
            and str(nullif.this.args.get("position")).upper() == "LEADING"
            and _literal_text(nullif.this.expression) == "0"
        )

    @staticmethod
    def _is_strip_leading_zeros(coalesce: exp.Coalesce) -> bool:
        return (
            isinstance(coalesce.this, exp.Nullif)
            and SqlGlotPolicyGuard._is_strip_zero_nullif(coalesce.this)
            and len(coalesce.expressions) == 1
            and _literal_text(coalesce.expressions[0]) == "0"
        )

    @staticmethod
    def _is_closed_cast(cast: exp.Cast) -> bool:
        target = cast.args.get("to")
        if not isinstance(target, exp.DataType):
            return False
        target_sql = target.sql(dialect="postgres").upper()
        if target_sql == "DATE":
            return cast.this.find(exp.Column) is not None or isinstance(cast.this, exp.Literal)
        if target_sql in {
            "TIMESTAMP",
            "TIMESTAMP WITH TIME ZONE",
            "TIMESTAMPTZ",
        }:
            return isinstance(cast.this, exp.Literal)
        if target_sql == "BIGINT":
            parent = cast.parent
            return (
                isinstance(parent, exp.Cast)
                and isinstance(parent.args.get("to"), exp.DataType)
                and parent.args["to"].sql(dialect="postgres").upper() == "TEXT"
            )
        if target_sql == "TEXT":
            return (
                isinstance(cast.this, exp.Cast)
                and isinstance(cast.this.args.get("to"), exp.DataType)
                and cast.this.args["to"].sql(dialect="postgres").upper() == "BIGINT"
            )
        if target_sql in {"DOUBLE", "DOUBLE PRECISION"}:
            return _literal_text(cast.this) in {"NaN", "Infinity", "-Infinity"} and isinstance(
                cast.parent, exp.In
            )
        if target_sql in {"DECIMAL", "NUMERIC"}:
            if isinstance(cast.parent, exp.Round):
                return True
            alias = cast.find_ancestor(exp.Alias)
            return isinstance(alias, exp.Alias) and alias.find(exp.Window) is not None
        return False

    @staticmethod
    def _is_closed_aggregate(
        aggregate: exp.Expression,
        *,
        plan_version: int,
    ) -> bool:
        source = aggregate.this
        if aggregate.find_ancestor(exp.Window) is not None:
            return isinstance(source, exp.Column)
        if isinstance(aggregate, exp.Count):
            if isinstance(source, exp.Distinct):
                expressions = tuple(source.expressions)
                return (
                    len(expressions) == 1
                    and not _is_bound_value(expressions[0], allow_null=True)
                    and expressions[0].find(exp.Column) is not None
                )
            if isinstance(source, exp.Literal):
                return plan_version == 2 and not source.is_string and _literal_text(source) == "1"
        if _is_bound_value(source, allow_null=True):
            return False
        if isinstance(source, exp.Case):
            return SqlGlotPolicyGuard._is_closed_case(
                source,
                plan_version=plan_version,
            )
        return source is not None and source.find(exp.Column) is not None

    @staticmethod
    def _validate_windows(statement: exp.Select) -> None:
        windows = tuple(statement.find_all(exp.Window))
        if len(windows) > _MAX_WINDOW_NODES:
            _raise(
                SqlRejectionCode.UNSAFE_WINDOW,
                (f"query has {len(windows)} window AST nodes; maximum is {_MAX_WINDOW_NODES}"),
            )
        ranking_types = (exp.RowNumber, exp.Rank, exp.DenseRank)
        for window in windows:
            if window.args.get("alias") is not None:
                _raise(
                    SqlRejectionCode.UNSAFE_WINDOW,
                    "named window references are forbidden",
                )
            partition = tuple(window.args.get("partition_by") or ())
            order = window.args.get("order")
            ordered = tuple(order.expressions) if isinstance(order, exp.Order) else ()
            if len(partition) > 3 or len(ordered) > 3:
                _raise(
                    SqlRejectionCode.UNSAFE_WINDOW,
                    "window partition/order exceeds the closed three-key limit",
                )
            function = window.this
            spec = window.args.get("spec")
            if isinstance(function, ranking_types):
                if not ordered or spec is not None:
                    _raise(
                        SqlRejectionCode.UNSAFE_WINDOW,
                        "ranking windows require ordering and cannot carry a frame",
                    )
                continue
            if isinstance(function, exp.Ntile):
                buckets = function.this
                if (
                    not ordered
                    or spec is not None
                    or not isinstance(buckets, exp.Literal)
                    or buckets.is_string
                ):
                    _raise(
                        SqlRejectionCode.UNSAFE_WINDOW,
                        "NTILE requires ordering and a bounded integer bucket count",
                    )
                try:
                    bucket_count = int(buckets.this)
                except (TypeError, ValueError):
                    bucket_count = 0
                if not 2 <= bucket_count <= 100:
                    _raise(
                        SqlRejectionCode.UNSAFE_WINDOW,
                        "NTILE bucket count must be between 2 and 100",
                    )
                continue
            if isinstance(function, (exp.Lag, exp.Lead)):
                offset = function.args.get("offset")
                if (
                    not ordered
                    or spec is not None
                    or not isinstance(offset, exp.Literal)
                    or offset.is_string
                    or function.args.get("default") is not None
                ):
                    _raise(
                        SqlRejectionCode.UNSAFE_WINDOW,
                        "LAG/LEAD require one bounded offset, ordering, and no default",
                    )
                try:
                    offset_value = int(offset.this)
                except (TypeError, ValueError):
                    offset_value = 0
                if not 1 <= offset_value <= 100:
                    _raise(
                        SqlRejectionCode.UNSAFE_WINDOW,
                        "LAG/LEAD offset must be between 1 and 100",
                    )
                continue
            if not isinstance(function, (exp.Sum, exp.Avg)):
                _raise(
                    SqlRejectionCode.UNSAFE_WINDOW,
                    f"window function is not in the closed allowlist: {type(function).__name__}",
                )
            if not ordered:
                if spec is not None:
                    _raise(
                        SqlRejectionCode.UNSAFE_WINDOW,
                        "unordered aggregate windows cannot carry a frame",
                    )
                continue
            if not isinstance(spec, exp.WindowSpec):
                _raise(
                    SqlRejectionCode.UNSAFE_WINDOW,
                    "ordered aggregate windows require an explicit ROWS frame",
                )
            SqlGlotPolicyGuard._validate_window_frame(spec)

    @staticmethod
    def _validate_window_frame(spec: exp.WindowSpec) -> None:
        if (
            str(spec.args.get("kind")).upper() != "ROWS"
            or spec.args.get("end") != "CURRENT ROW"
            or spec.args.get("end_side") is not None
            or spec.args.get("exclude") is not None
            or spec.args.get("start_side") != "PRECEDING"
        ):
            _raise(
                SqlRejectionCode.UNSAFE_WINDOW,
                "only backward-looking ROWS ... PRECEDING TO CURRENT ROW frames are allowed",
            )
        start = spec.args.get("start")
        if start == "UNBOUNDED":
            return
        if not isinstance(start, exp.Literal) or start.is_string:
            _raise(
                SqlRejectionCode.UNSAFE_WINDOW,
                "moving-window frame must use a bounded integer row count",
            )
        try:
            preceding = int(start.this)
        except (TypeError, ValueError):
            preceding = 0
        if not 1 <= preceding <= 365:
            _raise(
                SqlRejectionCode.UNSAFE_WINDOW,
                "moving-window row count must be between 1 and 365",
            )

    @staticmethod
    def _validate_column_scopes(
        statement: exp.Select,
        *,
        allowed_columns: dict[str, frozenset[str]],
        cte_aliases: set[str],
    ) -> None:
        scopes = traverse_scope(statement)
        if scopes is None:
            _raise(
                SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                "SQL scopes could not be derived",
            )
        for scope in scopes:
            relation_columns: dict[str, frozenset[str]] = {}
            for relation, (_node, source) in scope.selected_sources.items():
                if isinstance(source, Scope):
                    outputs = tuple(source.expression.named_selects)
                    if any(not output for output in outputs) or len(outputs) != len(set(outputs)):
                        _raise(
                            SqlRejectionCode.INVALID_CTE_TOPOLOGY,
                            f"relation {relation} has ambiguous staged outputs",
                        )
                    relation_columns[relation] = frozenset(outputs)
                    continue
                if not isinstance(source, exp.Table):
                    _raise(
                        SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                        "only governed tables and prior CTEs may appear in FROM",
                    )
                if not source.db:
                    if source.name in cte_aliases:
                        _raise(
                            SqlRejectionCode.INVALID_CTE_TOPOLOGY,
                            f"CTE source is not visible in this scope: {source.name}",
                        )
                    _raise(
                        SqlRejectionCode.UNKNOWN_ASSET,
                        f"asset must be schema-qualified and allowlisted: {source.sql()}",
                    )
                asset = f"{source.db}.{source.name}"
                columns = allowed_columns.get(asset)
                if columns is None:
                    _raise(
                        SqlRejectionCode.UNKNOWN_ASSET,
                        f"asset is not allowlisted: {asset}",
                    )
                relation_columns[relation] = columns

            for column in scope.columns:
                if column.table:
                    columns = relation_columns.get(column.table)
                    if columns is None or column.name not in columns:
                        _raise(
                            SqlRejectionCode.UNKNOWN_COLUMN,
                            f"column is not available in its scope: {column.sql()}",
                        )
                    continue
                candidates = [
                    relation
                    for relation, columns in relation_columns.items()
                    if column.name in columns
                ]
                if len(candidates) != 1:
                    _raise(
                        SqlRejectionCode.UNKNOWN_COLUMN,
                        f"unqualified column is unknown or ambiguous: {column.name}",
                    )

    @staticmethod
    def _validate_join_scopes(
        statement: exp.Select,
        policy: QueryPolicy,
    ) -> None:
        approved = {
            frozenset(
                (
                    contract.left_key.physical_field.root,
                    contract.right_key.physical_field.root,
                )
            ): contract
            for contract in policy.approved_join_contracts
        }
        if len(approved) != len(policy.approved_join_contracts):
            _raise(
                SqlRejectionCode.MISSING_JOIN_PREDICATE,
                "approved join key pairs must be unique",
            )
        observed_contracts: set[str] = set()
        for select in statement.find_all(exp.Select):
            from_clause = select.args.get("from_")
            if not isinstance(from_clause, exp.From):
                available_relations: set[str] = set()
                relation_assets: dict[str, str] = {}
            else:
                root = from_clause.this
                root_relation = root.alias_or_name
                available_relations = {root_relation} if root_relation else set()
                relation_assets = {}
                if isinstance(root, exp.Table) and root.db and root_relation:
                    relation_assets[root_relation] = f"{root.db}.{root.name}"

            upstream_fanout = False
            aggregates: tuple[exp.AggFunc, ...] = tuple(
                node
                for node in select.find_all(exp.AggFunc)
                if isinstance(node, (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max))
                if node.find_ancestor(exp.Window) is None
            )
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

                joined = join.this
                joined_relation = joined.alias_or_name
                if not isinstance(joined, exp.Table) or not joined.db:
                    _raise(
                        SqlRejectionCode.MISSING_JOIN_PREDICATE,
                        "joined relation must be one governed physical asset",
                    )
                relation_assets[joined_relation] = f"{joined.db}.{joined.name}"
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
                unwrapped = on.this if isinstance(on, exp.Paren) else on
                if not isinstance(unwrapped, exp.EQ):
                    _raise(
                        SqlRejectionCode.MISSING_JOIN_PREDICATE,
                        "approved joins require one compiler-owned equality predicate",
                    )
                physical_fields: set[str] = set()
                for column in unwrapped.find_all(exp.Column):
                    asset = relation_assets.get(column.table)
                    if asset is None:
                        _raise(
                            SqlRejectionCode.MISSING_JOIN_PREDICATE,
                            "join key column is outside the joined physical relations",
                        )
                    physical_fields.add(f"{asset}.{column.name}")
                contract = approved.get(frozenset(physical_fields))
                if contract is None or contract.id in observed_contracts:
                    _raise(
                        SqlRejectionCode.MISSING_JOIN_PREDICATE,
                        "join keys do not match one exact approved contract",
                    )

                joined_asset = relation_assets[joined_relation]
                left_asset = contract.left_key.physical_field.root.rsplit(".", 1)[0]
                right_asset = contract.right_key.physical_field.root.rsplit(".", 1)[0]
                previous_relation: str | None = None
                if joined_asset == right_asset:
                    previous_matches = tuple(
                        relation
                        for relation in available_relations
                        if relation_assets.get(relation) == left_asset
                    )
                    if len(previous_matches) == 1:
                        previous_relation = previous_matches[0]
                        from_key = contract.left_key
                        to_key = contract.right_key
                        cardinality = contract.cardinality
                        expected_join_type = contract.default_join_type
                elif joined_asset == left_asset:
                    previous_matches = tuple(
                        relation
                        for relation in available_relations
                        if relation_assets.get(relation) == right_asset
                    )
                    if len(previous_matches) == 1:
                        if contract.default_join_type is JoinType.LEFT:
                            _raise(
                                SqlRejectionCode.MISSING_JOIN_PREDICATE,
                                "an approved LEFT JOIN contract cannot be reversed",
                            )
                        previous_relation = previous_matches[0]
                        from_key = contract.right_key
                        to_key = contract.left_key
                        cardinality = _reverse_cardinality(contract.cardinality)
                        expected_join_type = JoinType.INNER
                if previous_relation is None:
                    _raise(
                        SqlRejectionCode.MISSING_JOIN_PREDICATE,
                        "join orientation does not match one approved contract path",
                    )

                observed_join_type: JoinType | None = None
                if not join.side and join.kind.upper() == "INNER":
                    observed_join_type = JoinType.INNER
                elif join.side.upper() == "LEFT" and not join.kind:
                    observed_join_type = JoinType.LEFT
                if observed_join_type is not expected_join_type:
                    _raise(
                        SqlRejectionCode.MISSING_JOIN_PREDICATE,
                        "join type does not match the approved contract orientation",
                    )

                expected_left = _expected_join_key(from_key, relation=previous_relation)
                expected_right = _expected_join_key(to_key, relation=joined_relation)
                if unwrapped.this != expected_left or unwrapped.expression != expected_right:
                    _raise(
                        SqlRejectionCode.MISSING_JOIN_PREDICATE,
                        "join normalization does not match the exact approved contract",
                    )

                if upstream_fanout:
                    for aggregate in aggregates:
                        source_relations = {
                            column.table
                            for column in aggregate.find_all(exp.Column)
                            if column.table
                        }
                        if (
                            joined_relation in source_relations
                            and not SqlGlotPolicyGuard._aggregate_is_fanout_invariant(aggregate)
                        ):
                            _raise(
                                SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                                "downstream aggregate crosses an earlier fanout without "
                                "approved mitigation",
                            )

                if cardinality is Cardinality.MANY_TO_MANY:
                    _raise(
                        SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                        "many-to-many joins are outside the governed SQL language",
                    )
                if cardinality is Cardinality.ONE_TO_MANY:
                    if (
                        contract.fanout_policy
                        is not FanoutPolicy.REQUIRE_DISTINCT_FOR_LEFT_ENTITY_METRICS
                    ):
                        _raise(
                            SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                            "oriented fanout has no approved mitigation policy",
                        )
                    if not aggregates:
                        _raise(
                            SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                            "one-to-many row queries require an explicit grain mitigation",
                        )
                    for aggregate in aggregates:
                        source_relations = {
                            column.table
                            for column in aggregate.find_all(exp.Column)
                            if column.table
                        }
                        count_rows = isinstance(aggregate, exp.Count) and not source_relations
                        affects_existing = bool(source_relations.intersection(available_relations))
                        if (
                            count_rows or affects_existing
                        ) and not SqlGlotPolicyGuard._aggregate_is_fanout_invariant(aggregate):
                            _raise(
                                SqlRejectionCode.UNSUPPORTED_QUERY_SHAPE,
                                "fanout requires COUNT DISTINCT, MIN, or MAX for "
                                "existing-entity aggregates",
                            )
                    upstream_fanout = True

                observed_contracts.add(contract.id)
                available_relations.add(joined_relation)

    @staticmethod
    def _aggregate_is_fanout_invariant(
        aggregate: exp.Expression,
    ) -> bool:
        if isinstance(aggregate, (exp.Min, exp.Max)):
            return True
        return isinstance(aggregate, exp.Count) and isinstance(aggregate.this, exp.Distinct)

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
