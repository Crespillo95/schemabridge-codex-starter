"""SQLGlot PostgreSQL compiler for the restricted query IR."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp
from sqlglot.errors import ErrorLevel

from schemabridge.application.query_execution import CompiledQuery, QueryCompilationError
from schemabridge.domain.advanced_plans import (
    AdvancedAggregateExpression,
    AdvancedQueryPlan,
    AdvancedSelectExpression,
    AdvancedValueExpression,
    AggregateBooleanPredicate,
    AggregateFilterPredicate,
    BucketExpression,
    OutputBooleanPredicatePlan,
    OutputFilterPredicate,
    PhysicalBooleanPredicate,
    RestrictedQueryPlan,
    WindowExpression,
)
from schemabridge.domain.advanced_requests import (
    AdvancedMetricOperation,
    BooleanOperator,
    GroupingMode,
    WindowOperation,
)
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    SourceConnectorKind,
    SourceDialect,
    governed_execution_target_fingerprint,
)
from schemabridge.domain.joins import JoinType
from schemabridge.domain.plans import (
    AggregateExpression,
    ColumnExpression,
    DatasetScan,
    DateGrainExpression,
    FilterPredicate,
    MappedExpression,
    ParameterScalar,
    ParameterValue,
    QueryValueExpression,
    SelectExpression,
)
from schemabridge.domain.requests import FilterOperator, MetricOperation, SortDirection
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


@dataclass(slots=True)
class _CompilerState:
    parameters: list[ParameterScalar] = field(default_factory=list)

    def placeholder(self, value: ParameterScalar) -> exp.Placeholder:
        self.parameters.append(value)
        return exp.Placeholder()


@dataclass(frozen=True, slots=True)
class PostgresQueryCompiler:
    """Build PostgreSQL from typed nodes without accepting SQL fragments."""

    def compile(
        self,
        plan: RestrictedQueryPlan,
        *,
        max_preview_rows: int,
        target: GovernedExecutionTarget | None = None,
    ) -> CompiledQuery:
        if max_preview_rows < 1:
            raise QueryCompilationError(
                "invalid_preview_limit",
                "maximum preview rows must be positive",
            )
        if target is not None and (
            target.connector_kind is not SourceConnectorKind.POSTGRESQL
            or target.dialect is not SourceDialect.POSTGRESQL
        ):
            raise QueryCompilationError(
                "connector_dialect_unsupported",
                "the governed execution target has no PostgreSQL compiler capability",
            )
        if isinstance(plan, AdvancedQueryPlan):
            return self._compile_advanced(
                plan,
                max_preview_rows=max_preview_rows,
                target=target,
            )
        state = _CompilerState()
        projections: list[exp.Expression] = []
        parameterized_projection_positions: list[tuple[QueryValueExpression, int]] = []
        for position, item in enumerate(plan.projections, start=1):
            parameter_count = len(state.parameters)
            compiled = self._compile_select(item.expression, state)
            projections.append(exp.alias_(compiled, item.alias.root))
            if (
                not isinstance(item.expression, AggregateExpression)
                and len(state.parameters) > parameter_count
            ):
                parameterized_projection_positions.append((item.expression, position))
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
                *(
                    self._compile_grouping_reference(
                        expression,
                        state,
                        parameterized_projection_positions,
                    )
                    for expression in plan.group_by
                )
            )
        if plan.order_by:
            query = query.order_by(
                *(
                    exp.Ordered(
                        this=self._compile_grouping_reference(
                            item.expression,
                            state,
                            parameterized_projection_positions,
                        ),
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
            dialect=SourceDialect.POSTGRESQL,
            target_fingerprint=(
                governed_execution_target_fingerprint(target) if target is not None else None
            ),
            plan_version=1,
        )

    def _compile_advanced(
        self,
        plan: AdvancedQueryPlan,
        *,
        max_preview_rows: int,
        target: GovernedExecutionTarget | None,
    ) -> CompiledQuery:
        if plan.grouping is not GroupingMode.STANDARD:
            raise QueryCompilationError(
                "unsupported_grouping",
                "ROLLUP is unsupported until subtotal NULL semantics are explicit",
            )
        state = _CompilerState()
        projections: list[exp.Expression] = []
        parameterized_projection_positions: list[tuple[AdvancedValueExpression, int]] = []
        for position, item in enumerate(plan.projections, start=1):
            parameter_count = len(state.parameters)
            compiled = self._compile_advanced_select(item.expression, state)
            projections.append(exp.alias_(compiled, item.alias.root))
            if (
                not isinstance(item.expression, AdvancedAggregateExpression)
                and len(state.parameters) > parameter_count
            ):
                parameterized_projection_positions.append((item.expression, position))

        aggregated = exp.select(*projections).from_(self._compile_scan(plan.root_scan))
        for join in plan.joins:
            join_type = {
                JoinType.INNER: "INNER",
                JoinType.LEFT: "LEFT",
            }[join.contract.default_join_type]
            aggregated = aggregated.join(
                self._compile_scan(join.right_scan),
                on=exp.EQ(
                    this=self._compile_value(join.on.left, state),
                    expression=self._compile_value(join.on.right, state),
                ),
                join_type=join_type,
            )
        if plan.where is not None:
            aggregated = aggregated.where(self._compile_physical_boolean(plan.where, state))
        if plan.group_by:
            grouping_expressions = [
                self._compile_advanced_grouping_reference(
                    item,
                    state,
                    parameterized_projection_positions,
                )
                for item in plan.group_by
            ]
            aggregated = aggregated.group_by(*grouping_expressions)
        if plan.having is not None:
            aggregated = aggregated.having(self._compile_aggregate_boolean(plan.having, state))

        effective_limit = min(plan.limit, max_preview_rows)
        base_aliases = tuple(item.alias.root for item in plan.projections)
        if not plan.windows and plan.post_filter is None:
            if plan.result_order_by:
                positions = {
                    alias: position for position, alias in enumerate(base_aliases, start=1)
                }
                aggregated = aggregated.order_by(
                    *(
                        exp.Ordered(
                            this=exp.Literal.number(positions[item.alias.root]),
                            desc=item.direction is SortDirection.DESC,
                            nulls_first=False,
                        )
                        for item in plan.result_order_by
                    )
                )
            statement = aggregated.limit(effective_limit)
        else:
            stage_name = "aggregated"
            all_aliases = base_aliases
            windowed: exp.Select | None = None
            if plan.windows:
                windowed_projections = [
                    exp.alias_(
                        exp.column(alias, table="aggregated"),
                        alias,
                    )
                    for alias in base_aliases
                ]
                windowed_projections.extend(
                    exp.alias_(
                        self._compile_window(item.expression, table="aggregated"),
                        item.alias.root,
                    )
                    for item in plan.windows
                )
                windowed = exp.select(*windowed_projections).from_(
                    exp.Table(this=exp.to_identifier("aggregated"))
                )
                stage_name = "windowed"
                all_aliases = (*base_aliases, *(item.alias.root for item in plan.windows))
            statement = exp.select(
                *(exp.alias_(exp.column(alias, table=stage_name), alias) for alias in all_aliases)
            ).from_(exp.Table(this=exp.to_identifier(stage_name)))
            if plan.post_filter is not None:
                statement = statement.where(
                    self._compile_output_boolean(
                        plan.post_filter,
                        state,
                        table=stage_name,
                    )
                )
            if plan.result_order_by:
                statement = statement.order_by(
                    *(
                        exp.Ordered(
                            this=exp.column(item.alias.root, table=stage_name),
                            desc=item.direction is SortDirection.DESC,
                            nulls_first=False,
                        )
                        for item in plan.result_order_by
                    )
                )
            statement = statement.limit(effective_limit)
            statement = statement.with_("aggregated", as_=aggregated)
            if windowed is not None:
                statement = statement.with_("windowed", as_=windowed)

        try:
            sql = statement.sql(
                dialect="postgres",
                pretty=True,
                unsupported_level=ErrorLevel.RAISE,
            )
        except Exception as error:
            raise QueryCompilationError(
                "postgres_render_failed",
                "version-2 query could not be rendered without a dialect downgrade",
            ) from error
        if sql.count("%s") != len(state.parameters):
            raise QueryCompilationError(
                "parameter_count_mismatch",
                "compiled placeholder count does not match parameter count",
            )
        return CompiledQuery(
            sql=sql,
            parameters=tuple(state.parameters),
            effective_limit=effective_limit,
            dialect=SourceDialect.POSTGRESQL,
            target_fingerprint=(
                governed_execution_target_fingerprint(target) if target is not None else None
            ),
            plan_version=2,
        )

    def _compile_advanced_select(
        self,
        expression: AdvancedSelectExpression,
        state: _CompilerState,
    ) -> exp.Expression:
        if isinstance(expression, AdvancedAggregateExpression):
            return self._compile_advanced_aggregate(expression, state)
        return self._compile_advanced_value(expression, state)

    def _compile_advanced_aggregate(
        self,
        expression: AdvancedAggregateExpression,
        state: _CompilerState,
    ) -> exp.Expression:
        condition = (
            self._compile_physical_boolean(expression.condition, state)
            if expression.condition is not None
            else None
        )
        source = (
            self._compile_value(expression.source, state) if expression.source is not None else None
        )
        if expression.operation is AdvancedMetricOperation.COUNT_ROWS:
            source = exp.Literal.number(1)
        assert source is not None
        if condition is not None:
            source = exp.Case(
                ifs=[exp.If(this=condition, true=source)],
                default=exp.Null(),
            )
        operation = expression.operation
        if operation in {
            AdvancedMetricOperation.COUNT_ROWS,
            AdvancedMetricOperation.COUNT,
        }:
            return exp.Count(this=source)
        if operation is AdvancedMetricOperation.COUNT_DISTINCT:
            return exp.Count(this=exp.Distinct(expressions=[source]))
        if operation is AdvancedMetricOperation.SUM:
            return exp.Sum(this=source)
        if operation is AdvancedMetricOperation.AVG:
            return exp.Avg(this=source)
        if operation is AdvancedMetricOperation.MIN:
            return exp.Min(this=source)
        if operation is AdvancedMetricOperation.MAX:
            return exp.Max(this=source)
        raise QueryCompilationError(
            "unsupported_aggregate",
            f"advanced aggregate is not supported: {operation.value}",
        )

    def _compile_advanced_value(
        self,
        expression: AdvancedValueExpression,
        state: _CompilerState,
    ) -> exp.Expression:
        if not isinstance(expression, BucketExpression):
            return self._compile_value(expression, state)
        parameter_count = len(state.parameters)
        source = self._compile_value(expression.source, state)
        if len(state.parameters) != parameter_count:
            raise QueryCompilationError(
                "unsupported_bucket_source",
                "numeric buckets cannot duplicate a parameterized source transformation",
            )
        branches: list[exp.If] = []
        for bucket in expression.buckets:
            conditions: list[exp.Expression] = []
            if bucket.lower is not None:
                conditions.append(
                    exp.GTE(
                        this=source.copy(),
                        expression=state.placeholder(bucket.lower.value),
                    )
                )
            if bucket.upper is not None:
                conditions.append(
                    exp.LT(
                        this=source.copy(),
                        expression=state.placeholder(bucket.upper.value),
                    )
                )
            branches.append(
                exp.If(
                    this=exp.and_(*conditions),
                    true=state.placeholder(bucket.label.value),
                )
            )
        return exp.Case(
            ifs=branches,
            default=state.placeholder(expression.else_value.value),
        )

    def _compile_advanced_grouping_reference(
        self,
        expression: AdvancedValueExpression,
        state: _CompilerState,
        parameterized_projection_positions: list[tuple[AdvancedValueExpression, int]],
    ) -> exp.Expression:
        for projected, position in parameterized_projection_positions:
            if projected == expression:
                return exp.Literal.number(position)
        return self._compile_advanced_value(expression, state)

    def _compile_physical_boolean(
        self,
        predicate: PhysicalBooleanPredicate,
        state: _CompilerState,
    ) -> exp.Expression:
        if predicate.kind is BooleanOperator.COMPARISON:
            assert predicate.comparison is not None
            return self._compile_filter(predicate.comparison, state)
        children = [self._compile_physical_boolean(item, state) for item in predicate.operands]
        return self._compile_boolean_connector(predicate.kind, children)

    def _compile_aggregate_boolean(
        self,
        predicate: AggregateBooleanPredicate,
        state: _CompilerState,
    ) -> exp.Expression:
        if predicate.kind is BooleanOperator.COMPARISON:
            assert predicate.comparison is not None
            return self._compile_aggregate_filter(predicate.comparison, state)
        children = [self._compile_aggregate_boolean(item, state) for item in predicate.operands]
        return self._compile_boolean_connector(predicate.kind, children)

    def _compile_aggregate_filter(
        self,
        predicate: AggregateFilterPredicate,
        state: _CompilerState,
    ) -> exp.Expression:
        left = self._compile_advanced_aggregate(predicate.expression, state)
        right = (
            self._compile_advanced_aggregate(predicate.compare_to, state)
            if predicate.compare_to is not None
            else None
        )
        return self._compile_comparison(
            left,
            predicate.operator,
            predicate.values,
            state,
            comparison=right,
        )

    def _compile_output_boolean(
        self,
        predicate: OutputBooleanPredicatePlan,
        state: _CompilerState,
        *,
        table: str,
    ) -> exp.Expression:
        if predicate.kind is BooleanOperator.COMPARISON:
            assert predicate.comparison is not None
            return self._compile_output_filter(
                predicate.comparison,
                state,
                table=table,
            )
        children = [
            self._compile_output_boolean(item, state, table=table) for item in predicate.operands
        ]
        return self._compile_boolean_connector(predicate.kind, children)

    def _compile_output_filter(
        self,
        predicate: OutputFilterPredicate,
        state: _CompilerState,
        *,
        table: str,
    ) -> exp.Expression:
        return self._compile_comparison(
            exp.column(predicate.alias.root, table=table),
            predicate.operator,
            predicate.values,
            state,
            comparison=(
                exp.column(predicate.compare_to.root, table=table)
                if predicate.compare_to is not None
                else None
            ),
        )

    @staticmethod
    def _compile_boolean_connector(
        kind: BooleanOperator,
        children: list[exp.Expression],
    ) -> exp.Expression:
        if kind is BooleanOperator.AND:
            return exp.and_(*children)
        if kind is BooleanOperator.OR:
            return exp.or_(*children)
        if kind is BooleanOperator.NOT:
            return exp.Not(this=children[0])
        raise QueryCompilationError(
            "unsupported_boolean_operator",
            f"boolean operator is not supported here: {kind.value}",
        )

    def _compile_comparison(
        self,
        left: exp.Expression,
        operator: FilterOperator,
        values: tuple[ParameterValue, ...],
        state: _CompilerState,
        *,
        comparison: exp.Expression | None,
    ) -> exp.Expression:
        if operator is FilterOperator.IS_NULL:
            return exp.Is(this=left, expression=exp.Null())
        if operator is FilterOperator.IS_NOT_NULL:
            return exp.Not(this=exp.Is(this=left, expression=exp.Null()))
        if operator is FilterOperator.IN:
            return exp.In(
                this=left,
                expressions=[state.placeholder(value.value) for value in values],
            )
        right = comparison
        if right is None:
            right = state.placeholder(values[0].value)
        comparison_types: dict[FilterOperator, type[exp.Binary]] = {
            FilterOperator.EQUALS: exp.EQ,
            FilterOperator.NOT_EQUALS: exp.NEQ,
            FilterOperator.GREATER_THAN: exp.GT,
            FilterOperator.GREATER_THAN_OR_EQUAL: exp.GTE,
            FilterOperator.LESS_THAN: exp.LT,
            FilterOperator.LESS_THAN_OR_EQUAL: exp.LTE,
        }
        comparison_type = comparison_types.get(operator)
        if comparison_type is None:
            raise QueryCompilationError(
                "unsupported_filter_operator",
                f"filter operator is not supported: {operator.value}",
            )
        return comparison_type(this=left, expression=right)

    def _compile_window(
        self,
        expression: WindowExpression,
        *,
        table: str,
    ) -> exp.Expression:
        source = (
            exp.column(expression.source.root, table=table)
            if expression.source is not None
            else None
        )
        partition = [exp.column(alias.root, table=table) for alias in expression.partition_by]
        order = [
            exp.Ordered(
                this=exp.column(item.alias.root, table=table),
                desc=item.direction is SortDirection.DESC,
                nulls_first=False,
            )
            for item in expression.order_by
        ]

        def window(
            function: exp.Expression,
            *,
            frame: exp.WindowSpec | None = None,
            include_order: bool = True,
        ) -> exp.Window:
            return exp.Window(
                this=function,
                partition_by=partition,
                order=exp.Order(expressions=order) if order and include_order else None,
                spec=frame,
            )

        operation = expression.operation
        if operation is WindowOperation.ROW_NUMBER:
            return window(exp.RowNumber())
        if operation is WindowOperation.RANK:
            return window(exp.Rank())
        if operation is WindowOperation.DENSE_RANK:
            return window(exp.DenseRank())
        if operation is WindowOperation.NTILE:
            assert expression.buckets is not None
            return window(exp.Ntile(this=exp.Literal.number(expression.buckets)))
        assert source is not None
        if operation in {WindowOperation.RUNNING_SUM, WindowOperation.RUNNING_AVG}:
            function = (
                exp.Sum(this=source)
                if operation is WindowOperation.RUNNING_SUM
                else exp.Avg(this=source)
            )
            return window(
                function,
                frame=exp.WindowSpec(
                    kind="ROWS",
                    start="UNBOUNDED",
                    start_side="PRECEDING",
                    end="CURRENT ROW",
                ),
            )
        if operation in {WindowOperation.MOVING_SUM, WindowOperation.MOVING_AVG}:
            assert expression.preceding_rows is not None
            function = (
                exp.Sum(this=source)
                if operation is WindowOperation.MOVING_SUM
                else exp.Avg(this=source)
            )
            return window(
                function,
                frame=exp.WindowSpec(
                    kind="ROWS",
                    start=exp.Literal.number(expression.preceding_rows),
                    start_side="PRECEDING",
                    end="CURRENT ROW",
                ),
            )
        if operation is WindowOperation.PARTITION_AVG:
            return window(exp.Avg(this=source), include_order=False)
        if operation in {WindowOperation.LAG, WindowOperation.LEAD}:
            assert expression.offset is not None
            function_type = exp.Lag if operation is WindowOperation.LAG else exp.Lead
            return window(
                function_type(
                    this=source,
                    offset=exp.Literal.number(expression.offset),
                )
            )
        if operation in {
            WindowOperation.DELTA_FROM_PREVIOUS,
            WindowOperation.PERCENT_CHANGE_FROM_PREVIOUS,
        }:
            assert expression.offset is not None
            previous = window(
                exp.Lag(
                    this=source.copy(),
                    offset=exp.Literal.number(expression.offset),
                )
            )
            delta = exp.Sub(this=source.copy(), expression=previous.copy())
            if operation is WindowOperation.DELTA_FROM_PREVIOUS:
                return delta
            return exp.Mul(
                this=exp.Literal.number(100),
                expression=exp.Div(
                    this=exp.Cast(this=delta, to=exp.DataType.build("NUMERIC")),
                    expression=exp.Nullif(
                        this=exp.Cast(
                            this=previous,
                            to=exp.DataType.build("NUMERIC"),
                        ),
                        expression=exp.Literal.number(0),
                    ),
                ),
            )
        if operation is WindowOperation.PERCENT_OF_TOTAL:
            total = window(exp.Sum(this=source.copy()), include_order=False)
            percentage = exp.Mul(
                this=exp.Literal.number(100),
                expression=exp.Div(
                    this=exp.Cast(
                        this=source,
                        to=exp.DataType.build("NUMERIC"),
                    ),
                    expression=exp.Nullif(
                        this=total,
                        expression=exp.Literal.number(0),
                    ),
                ),
            )
            return exp.func("ROUND", percentage, exp.Literal.number(2))
        raise QueryCompilationError(
            "unsupported_window",
            f"window operation is not supported: {operation.value}",
        )

    def _compile_grouping_reference(
        self,
        expression: QueryValueExpression,
        state: _CompilerState,
        parameterized_projection_positions: list[tuple[QueryValueExpression, int]],
    ) -> exp.Expression:
        for projected, position in parameterized_projection_positions:
            if projected == expression:
                return exp.Literal.number(position)
        return self._compile_value(expression, state)

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
        float_identifier_validation = False

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
                invalidity_conditions.append(
                    exp.LT(
                        this=current.copy(),
                        expression=exp.Literal.number(0),
                    )
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
                    state.placeholder(step.format),
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
