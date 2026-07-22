"""SQLGlot PostgreSQL compiler for the restricted query IR."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp

from schemabridge.application.query_execution import CompiledQuery, QueryCompilationError
from schemabridge.domain.joins import JoinType
from schemabridge.domain.plans import (
    AggregateExpression,
    ColumnExpression,
    DatasetScan,
    DateGrainExpression,
    FilterPredicate,
    MappedExpression,
    ParameterScalar,
    QueryPlan,
    QueryValueExpression,
    SelectExpression,
)
from schemabridge.domain.requests import FilterOperator, MetricOperation, SortDirection
from schemabridge.domain.transformations import (
    CastIntegerToStringStep,
    EmptyToNullStep,
    IdentityStep,
    MapValuesStep,
    PadLeftStep,
    PreserveNullStep,
    RejectInvalidStep,
    StripLeadingZerosStep,
    TrimStep,
    ValidateFiniteStep,
    ValidateIntegralStep,
    ValidateRegexStep,
)


@dataclass(slots=True)
class _CompilerState:
    parameters: list[ParameterScalar] = field(default_factory=list)

    def placeholder(self, value: ParameterScalar) -> exp.Placeholder:
        self.parameters.append(value)
        return exp.Placeholder()


@dataclass(frozen=True, slots=True)
class PostgresQueryCompiler:
    """Build PostgreSQL from typed nodes without accepting SQL fragments."""

    def compile(self, plan: QueryPlan, *, max_preview_rows: int) -> CompiledQuery:
        if max_preview_rows < 1:
            raise QueryCompilationError(
                "invalid_preview_limit",
                "maximum preview rows must be positive",
            )
        state = _CompilerState()
        projections = [
            exp.alias_(
                self._compile_select(item.expression, state),
                item.alias.root,
            )
            for item in plan.projections
        ]
        query = exp.select(*projections).from_(self._compile_scan(plan.root_scan))

        for join in plan.joins:
            join_type = {
                JoinType.INNER: "INNER",
                JoinType.LEFT: "LEFT",
            }[join.contract.default_join_type]
            query = query.join(
                self._compile_scan(join.right_scan),
                on=exp.EQ(
                    this=self._compile_value(join.on.left, state),
                    expression=self._compile_value(join.on.right, state),
                ),
                join_type=join_type,
            )

        predicates = [self._compile_filter(predicate, state) for predicate in plan.filters]
        if predicates:
            query = query.where(exp.and_(*predicates))
        if plan.group_by:
            query = query.group_by(
                *(self._compile_value(expression, state) for expression in plan.group_by)
            )
        if plan.order_by:
            query = query.order_by(
                *(
                    exp.Ordered(
                        this=self._compile_value(item.expression, state),
                        desc=item.direction is SortDirection.DESC,
                    )
                    for item in plan.order_by
                )
            )

        effective_limit = min(plan.limit or max_preview_rows, max_preview_rows)
        query = query.limit(effective_limit)
        sql = query.sql(dialect="postgres", pretty=True)
        if sql.count("%s") != len(state.parameters):
            raise QueryCompilationError(
                "parameter_count_mismatch",
                "compiled placeholder count does not match parameter count",
            )
        return CompiledQuery(
            sql=sql,
            parameters=tuple(state.parameters),
            effective_limit=effective_limit,
        )

    @staticmethod
    def _compile_scan(scan: DatasetScan) -> exp.Table:
        schema, table = scan.dataset.root.split(".", maxsplit=1)
        return exp.Table(
            this=exp.to_identifier(table),
            db=exp.to_identifier(schema),
            alias=exp.TableAlias(this=exp.to_identifier(scan.alias.root)),
        )

    def _compile_select(
        self,
        expression: SelectExpression,
        state: _CompilerState,
    ) -> exp.Expression:
        if isinstance(expression, AggregateExpression):
            source = self._compile_value(expression.source, state)
            if expression.operation is MetricOperation.COUNT:
                return exp.Count(this=source)
            if expression.operation is MetricOperation.COUNT_DISTINCT:
                return exp.Count(this=exp.Distinct(expressions=[source]))
            if expression.operation is MetricOperation.SUM:
                return exp.Sum(this=source)
            if expression.operation is MetricOperation.AVG:
                return exp.Avg(this=source)
            if expression.operation is MetricOperation.MIN:
                return exp.Min(this=source)
            if expression.operation is MetricOperation.MAX:
                return exp.Max(this=source)
            raise QueryCompilationError(
                "unsupported_aggregate",
                f"aggregate is not supported: {expression.operation.value}",
            )
        return self._compile_value(expression, state)

    def _compile_value(
        self,
        expression: QueryValueExpression,
        state: _CompilerState,
    ) -> exp.Expression:
        if isinstance(expression, ColumnExpression):
            column = expression.field.root.rsplit(".", maxsplit=1)[1]
            return exp.column(column, table=expression.relation.root)
        if isinstance(expression, DateGrainExpression):
            source = self._compile_value(expression.source, state)
            truncated = exp.func(
                "DATE_TRUNC",
                exp.Literal.string(expression.grain.value),
                source,
            )
            return exp.Cast(this=truncated, to=exp.DataType.build("DATE"))
        if isinstance(expression, MappedExpression):
            return self._compile_mapping(expression, state)
        raise QueryCompilationError(
            "unsupported_expression",
            f"query expression is not supported: {type(expression).__name__}",
        )

    def _compile_mapping(
        self,
        mapped: MappedExpression,
        state: _CompilerState,
    ) -> exp.Expression:
        current = self._compile_value(mapped.source, state)
        validity_conditions: list[exp.Expression] = []
        invalidity_conditions: list[exp.Expression] = []
        cast_integer_to_string = False

        for step in mapped.transformation_plan.steps:
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
                        expression=state.placeholder(step.pattern),
                    )
                )
                continue
            if isinstance(step, ValidateFiniteStep):
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
                current = self._strip_leading_zeros(current)
                continue
            if isinstance(step, PadLeftStep):
                current = exp.func(
                    "LPAD",
                    self._strip_leading_zeros(current),
                    exp.Literal.number(step.length),
                    exp.Literal.string(step.fill_character),
                )
                continue
            if isinstance(step, CastIntegerToStringStep):
                max_safe_integer = exp.Literal.number(9_007_199_254_740_991)
                invalidity_conditions.extend(
                    (
                        exp.LT(
                            this=current.copy(),
                            expression=exp.Literal.number(0),
                        ),
                        exp.GT(this=current.copy(), expression=max_safe_integer.copy()),
                    )
                )
                cast_integer_to_string = True
                continue
            if isinstance(step, MapValuesStep):
                current = exp.Case(
                    this=current,
                    ifs=[
                        exp.If(
                            this=state.placeholder(entry.source),
                            true=state.placeholder(entry.target),
                        )
                        for entry in step.entries
                    ],
                    default=exp.Null(),
                )
                continue
            raise QueryCompilationError(
                "unsupported_transformation",
                f"M03 compiler does not support transformation: {step.operation}",
            )

        if cast_integer_to_string:
            current = exp.Cast(
                this=exp.Cast(this=current, to=exp.DataType.build("BIGINT")),
                to=exp.DataType.build("TEXT"),
            )
        if invalidity_conditions:
            invalid = exp.or_(*invalidity_conditions)
            current = exp.Case(
                ifs=[exp.If(this=invalid, true=exp.Null())],
                default=current,
            )
        if validity_conditions:
            valid = exp.and_(*validity_conditions)
            current = exp.Case(
                ifs=[exp.If(this=valid, true=current)],
                default=exp.Null(),
            )
        return current

    @staticmethod
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

    def _compile_filter(
        self,
        predicate: FilterPredicate,
        state: _CompilerState,
    ) -> exp.Expression:
        expression = self._compile_value(predicate.expression, state)
        operator = predicate.operator
        if operator is FilterOperator.IS_NULL:
            return exp.Is(this=expression, expression=exp.Null())
        if operator is FilterOperator.IS_NOT_NULL:
            return exp.Not(this=exp.Is(this=expression, expression=exp.Null()))
        if operator is FilterOperator.IN:
            return exp.In(
                this=expression,
                expressions=[state.placeholder(value.value) for value in predicate.values],
            )

        parameter = state.placeholder(predicate.values[0].value)
        comparison_types: dict[FilterOperator, type[exp.Binary]] = {
            FilterOperator.EQUALS: exp.EQ,
            FilterOperator.NOT_EQUALS: exp.NEQ,
            FilterOperator.GREATER_THAN: exp.GT,
            FilterOperator.GREATER_THAN_OR_EQUAL: exp.GTE,
            FilterOperator.LESS_THAN: exp.LT,
            FilterOperator.LESS_THAN_OR_EQUAL: exp.LTE,
        }
        comparison = comparison_types.get(operator)
        if comparison is None:
            raise QueryCompilationError(
                "unsupported_filter_operator",
                f"filter operator is not supported: {operator.value}",
            )
        return comparison(this=expression, expression=parameter)
