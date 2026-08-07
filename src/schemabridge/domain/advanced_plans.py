"""Version-2 staged query IR for bounded advanced PostgreSQL compilation."""

from __future__ import annotations

from typing import Annotated, Literal, Self, TypeAlias

from pydantic import Field, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.advanced_requests import (
    MAX_PREDICATE_DEPTH,
    MAX_PREDICATE_LEAVES,
    MAX_PREDICATE_OPERANDS,
    MAX_WINDOWS,
    AdvancedMetricOperation,
    BooleanOperator,
    GroupingMode,
    WindowOperation,
    validate_postgresql_alias,
)
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.joins import Cardinality, FanoutPolicy, JoinType
from schemabridge.domain.plans import (
    ApprovedJoin,
    ColumnExpression,
    DatasetScan,
    DateGrainExpression,
    FilterPredicate,
    MappedExpression,
    OutputAlias,
    ParameterValue,
    QueryPlan,
    QueryValueExpression,
    RelationAlias,
)
from schemabridge.domain.requests import FilterOperator, SortDirection

_DUPLICATION_INVARIANT_OPERATIONS = frozenset(
    {
        AdvancedMetricOperation.COUNT_DISTINCT,
        AdvancedMetricOperation.MIN,
        AdvancedMetricOperation.MAX,
    }
)


class NumericBucketPlan(FrozenDomainModel):
    """One typed half-open numeric range used by a compiler-owned CASE."""

    label: ParameterValue
    lower: ParameterValue | None = None
    upper: ParameterValue | None = None

    @model_validator(mode="after")
    def range_must_have_a_bound(self) -> Self:
        if self.lower is None and self.upper is None:
            raise ValueError("numeric bucket plan requires at least one bound")
        return self


class BucketExpression(FrozenDomainModel):
    """Closed CASE bucketing over one governed physical value."""

    kind: Literal["bucket"] = "bucket"
    source: QueryValueExpression
    buckets: tuple[NumericBucketPlan, ...] = Field(min_length=1, max_length=12)
    else_value: ParameterValue


AdvancedValueExpression: TypeAlias = Annotated[
    ColumnExpression | MappedExpression | DateGrainExpression | BucketExpression,
    Field(discriminator="kind"),
]


class PhysicalBooleanPredicate(FrozenDomainModel):
    """Boolean tree evaluated against approved physical value expressions."""

    kind: BooleanOperator
    comparison: FilterPredicate | None = None
    operands: tuple[PhysicalBooleanPredicate, ...] = Field(
        default=(),
        max_length=MAX_PREDICATE_OPERANDS,
    )

    @model_validator(mode="after")
    def shape_and_complexity_must_be_bounded(self) -> Self:
        _validate_boolean_shape(self.kind, self.comparison, self.operands)
        depth, leaves = _predicate_complexity(self)
        if depth > MAX_PREDICATE_DEPTH or leaves > MAX_PREDICATE_LEAVES:
            raise ValueError("physical predicate exceeds its bounded complexity")
        return self


class AdvancedAggregateExpression(FrozenDomainModel):
    """Closed aggregate, optionally restricted by a typed row predicate."""

    kind: Literal["aggregate"] = "aggregate"
    operation: AdvancedMetricOperation
    source: QueryValueExpression | None = None
    condition: PhysicalBooleanPredicate | None = None

    @model_validator(mode="after")
    def source_shape_must_match_operation(self) -> Self:
        if self.operation is AdvancedMetricOperation.COUNT_ROWS:
            if self.source is not None:
                raise ValueError("count_rows aggregate cannot carry a source")
        elif self.source is None:
            raise ValueError(f"{self.operation.value} aggregate requires a source")
        return self


AdvancedSelectExpression: TypeAlias = Annotated[
    ColumnExpression
    | MappedExpression
    | DateGrainExpression
    | BucketExpression
    | AdvancedAggregateExpression,
    Field(discriminator="kind"),
]


class AdvancedSelectItem(FrozenDomainModel):
    expression: AdvancedSelectExpression
    alias: OutputAlias


class AggregateFilterPredicate(FrozenDomainModel):
    expression: AdvancedAggregateExpression
    operator: FilterOperator
    values: tuple[ParameterValue, ...] = Field(default=(), max_length=64)
    compare_to: AdvancedAggregateExpression | None = None

    @model_validator(mode="after")
    def value_shape_must_match_operator(self) -> Self:
        _validate_comparison_operands(
            operator=self.operator,
            values=self.values,
            has_comparison=self.compare_to is not None,
        )
        return self


class AggregateBooleanPredicate(FrozenDomainModel):
    kind: BooleanOperator
    comparison: AggregateFilterPredicate | None = None
    operands: tuple[AggregateBooleanPredicate, ...] = Field(
        default=(),
        max_length=MAX_PREDICATE_OPERANDS,
    )

    @model_validator(mode="after")
    def shape_and_complexity_must_be_bounded(self) -> Self:
        _validate_boolean_shape(self.kind, self.comparison, self.operands)
        depth, leaves = _predicate_complexity(self)
        if depth > MAX_PREDICATE_DEPTH or leaves > MAX_PREDICATE_LEAVES:
            raise ValueError("aggregate predicate exceeds its bounded complexity")
        return self


class OutputFilterPredicate(FrozenDomainModel):
    alias: OutputAlias
    operator: FilterOperator
    values: tuple[ParameterValue, ...] = Field(default=(), max_length=64)
    compare_to: OutputAlias | None = None

    @model_validator(mode="after")
    def value_shape_must_match_operator(self) -> Self:
        _validate_comparison_operands(
            operator=self.operator,
            values=self.values,
            has_comparison=self.compare_to is not None,
        )
        return self


class OutputBooleanPredicatePlan(FrozenDomainModel):
    kind: BooleanOperator
    comparison: OutputFilterPredicate | None = None
    operands: tuple[OutputBooleanPredicatePlan, ...] = Field(
        default=(),
        max_length=MAX_PREDICATE_OPERANDS,
    )

    @model_validator(mode="after")
    def shape_and_complexity_must_be_bounded(self) -> Self:
        _validate_boolean_shape(self.kind, self.comparison, self.operands)
        depth, leaves = _predicate_complexity(self)
        if depth > MAX_PREDICATE_DEPTH or leaves > MAX_PREDICATE_LEAVES:
            raise ValueError("output predicate exceeds its bounded complexity")
        return self


class OutputOrderItem(FrozenDomainModel):
    alias: OutputAlias
    direction: SortDirection = SortDirection.ASC


class WindowExpression(FrozenDomainModel):
    """Closed window expression over outputs of the physical/aggregate stage."""

    operation: WindowOperation
    source: OutputAlias | None = None
    partition_by: tuple[OutputAlias, ...] = Field(default=(), max_length=3)
    order_by: tuple[OutputOrderItem, ...] = Field(default=(), max_length=3)
    buckets: int | None = Field(default=None, ge=2, le=100)
    offset: int | None = Field(default=None, ge=1, le=100)
    preceding_rows: int | None = Field(default=None, ge=1, le=365)

    @model_validator(mode="after")
    def operation_shape_must_be_closed(self) -> Self:
        ranking = {
            WindowOperation.ROW_NUMBER,
            WindowOperation.RANK,
            WindowOperation.DENSE_RANK,
        }
        running = {
            WindowOperation.RUNNING_SUM,
            WindowOperation.RUNNING_AVG,
        }
        moving = {
            WindowOperation.MOVING_SUM,
            WindowOperation.MOVING_AVG,
        }
        offset_operations = {
            WindowOperation.LAG,
            WindowOperation.LEAD,
            WindowOperation.DELTA_FROM_PREVIOUS,
            WindowOperation.PERCENT_CHANGE_FROM_PREVIOUS,
        }
        if self.operation in ranking:
            valid = (
                self.source is None
                and bool(self.order_by)
                and self.buckets is None
                and self.offset is None
                and self.preceding_rows is None
            )
        elif self.operation is WindowOperation.NTILE:
            valid = (
                self.source is None
                and bool(self.order_by)
                and self.buckets is not None
                and self.offset is None
                and self.preceding_rows is None
            )
        elif self.operation in running:
            valid = (
                self.source is not None
                and bool(self.order_by)
                and self.buckets is None
                and self.offset is None
                and self.preceding_rows is None
            )
        elif self.operation in moving:
            valid = (
                self.source is not None
                and bool(self.order_by)
                and self.buckets is None
                and self.offset is None
                and self.preceding_rows is not None
            )
        elif self.operation in offset_operations:
            valid = (
                self.source is not None
                and bool(self.order_by)
                and self.buckets is None
                and self.offset is not None
                and self.preceding_rows is None
            )
        elif self.operation in {
            WindowOperation.PARTITION_AVG,
            WindowOperation.PERCENT_OF_TOTAL,
        }:
            valid = (
                self.source is not None
                and not self.order_by
                and self.buckets is None
                and self.offset is None
                and self.preceding_rows is None
            )
        else:
            valid = False
        if not valid:
            raise ValueError("window expression arguments do not match its closed operation")
        return self


class WindowSelectItem(FrozenDomainModel):
    expression: WindowExpression
    alias: OutputAlias


class AdvancedQueryPlan(FrozenDomainModel):
    """Linear physical → window → final plan; it is independent from version 1."""

    version: Literal[2] = 2
    root_scan: DatasetScan
    joins: tuple[ApprovedJoin, ...] = Field(default=(), max_length=2)
    projections: tuple[AdvancedSelectItem, ...] = Field(min_length=1, max_length=32)
    group_by: tuple[AdvancedValueExpression, ...] = Field(default=(), max_length=12)
    where: PhysicalBooleanPredicate | None = None
    having: AggregateBooleanPredicate | None = None
    windows: tuple[WindowSelectItem, ...] = Field(default=(), max_length=MAX_WINDOWS)
    post_filter: OutputBooleanPredicatePlan | None = None
    result_order_by: tuple[OutputOrderItem, ...] = Field(default=(), max_length=4)
    grouping: GroupingMode = GroupingMode.STANDARD
    limit: int = Field(default=100, ge=1, le=1_000)

    @model_validator(mode="after")
    def staged_references_must_resolve(self) -> Self:
        if self.grouping is not GroupingMode.STANDARD:
            raise ValueError("ROLLUP is unsupported until subtotal NULL semantics are explicit")
        scans = _validate_scan_graph(self.root_scan, self.joins)

        physical_expressions: list[AdvancedValueExpression | QueryValueExpression] = []
        for projection in self.projections:
            physical_expressions.extend(_physical_expressions_in(projection.expression))
            if (
                isinstance(projection.expression, AdvancedAggregateExpression)
                and projection.expression.condition is not None
            ):
                physical_expressions.extend(
                    item.expression
                    for item in _physical_predicate_leaves(projection.expression.condition)
                )
        physical_expressions.extend(self.group_by)
        if self.where is not None:
            physical_expressions.extend(
                item.expression for item in _physical_predicate_leaves(self.where)
            )
        for expression in physical_expressions:
            for column in _columns_in(expression):
                dataset = scans.get(column.relation.root)
                if dataset is None or _dataset_for_column(column) != dataset:
                    raise ValueError("advanced expression references an undeclared scan")

        base_aliases = tuple(item.alias for item in self.projections)
        for alias in base_aliases:
            validate_postgresql_alias(alias.root)
        base_names = {item.root for item in base_aliases}
        if len({item.root.casefold() for item in base_aliases}) != len(base_aliases):
            raise ValueError("advanced projection aliases must be unique")
        grouped = set(self.group_by)
        aggregates = tuple(
            item.expression
            for item in self.projections
            if isinstance(item.expression, AdvancedAggregateExpression)
        )
        if aggregates:
            ungrouped = [
                item.expression
                for item in self.projections
                if not isinstance(item.expression, AdvancedAggregateExpression)
                and item.expression not in grouped
            ]
            if ungrouped:
                raise ValueError("non-aggregate projections must appear in group_by")
        elif self.group_by or self.having is not None:
            raise ValueError("row plans cannot carry grouping or HAVING")

        if self.having is not None:
            for aggregate_leaf in _aggregate_predicate_leaves(self.having):
                if aggregate_leaf.expression not in aggregates or (
                    aggregate_leaf.compare_to is not None
                    and aggregate_leaf.compare_to not in aggregates
                ):
                    raise ValueError("HAVING must reuse an exactly projected aggregate")

        window_names = [item.alias.root for item in self.windows]
        for window_alias_name in window_names:
            validate_postgresql_alias(window_alias_name)
        if len(window_names) != len({alias.casefold() for alias in window_names}):
            raise ValueError("window aliases must be unique")
        if {alias.casefold() for alias in window_names}.intersection(
            alias.casefold() for alias in base_names
        ):
            raise ValueError("window aliases cannot shadow base outputs")
        for window in self.windows:
            referenced_aliases = (
                *(item.root for item in window.expression.partition_by),
                *(item.alias.root for item in window.expression.order_by),
                *((window.expression.source.root,) if window.expression.source is not None else ()),
            )
            for referenced_alias_name in referenced_aliases:
                validate_postgresql_alias(referenced_alias_name)
            references = {
                *(item.root for item in window.expression.partition_by),
                *(item.alias.root for item in window.expression.order_by),
            }
            if window.expression.source is not None:
                references.add(window.expression.source.root)
            if not references <= base_names:
                raise ValueError("window references an unknown base output")

        all_outputs = base_names | set(window_names)
        if self.post_filter is not None:
            for output_leaf in _output_predicate_leaves(self.post_filter):
                validate_postgresql_alias(output_leaf.alias.root)
                if output_leaf.compare_to is not None:
                    validate_postgresql_alias(output_leaf.compare_to.root)
                if output_leaf.alias.root not in all_outputs or (
                    output_leaf.compare_to is not None
                    and output_leaf.compare_to.root not in all_outputs
                ):
                    raise ValueError("post-window predicate references an unknown output")
        final_order = [item.alias.root for item in self.result_order_by]
        for final_alias_name in final_order:
            validate_postgresql_alias(final_alias_name)
        if len(final_order) != len({alias.casefold() for alias in final_order}) or any(
            alias not in all_outputs for alias in final_order
        ):
            raise ValueError("final ordering must reference unique known outputs")
        _validate_fanout(self.root_scan, self.joins, aggregates)
        return self


RestrictedQueryPlan: TypeAlias = AdvancedQueryPlan | QueryPlan


def _validate_scan_graph(
    root_scan: DatasetScan,
    joins: tuple[ApprovedJoin, ...],
) -> dict[str, PhysicalDatasetRef]:
    scans = {root_scan.alias.root: root_scan.dataset}
    datasets = {root_scan.dataset.root}
    for join in joins:
        right_alias = join.right_scan.alias.root
        right_dataset = join.right_scan.dataset.root
        if right_alias in scans:
            raise ValueError("scan aliases must be unique")
        if right_dataset in datasets:
            raise ValueError("self joins and repeated physical datasets are unsupported")
        left_relations = {column.relation.root for column in _columns_in(join.on.left)}
        right_relations = {column.relation.root for column in _columns_in(join.on.right)}
        predicate_relations = left_relations | right_relations
        if right_alias not in predicate_relations:
            raise ValueError("join predicate must reference the joined dataset")
        if not predicate_relations.intersection(scans):
            raise ValueError("join predicate must connect to an existing scan")
        if not predicate_relations <= (set(scans) | {right_alias}):
            raise ValueError("join predicate references an undeclared relation")
        scans[right_alias] = join.right_scan.dataset
        datasets.add(right_dataset)
    return scans


def _validate_fanout(
    root_scan: DatasetScan,
    joins: tuple[ApprovedJoin, ...],
    aggregates: tuple[AdvancedAggregateExpression, ...],
) -> None:
    dataset_by_alias = {root_scan.alias.root: root_scan.dataset.root}
    dataset_by_alias.update(
        {join.right_scan.alias.root: join.right_scan.dataset.root for join in joins}
    )
    available_datasets = {root_scan.dataset.root}
    available_relations = {root_scan.alias.root: root_scan.dataset.root}
    upstream_fanout_contract: str | None = None
    for join in joins:
        contract = join.contract
        left_dataset = contract.left_key.physical_field.root.rsplit(".", 1)[0]
        right_dataset = contract.right_key.physical_field.root.rsplit(".", 1)[0]
        joined_dataset = join.right_scan.dataset.root
        if joined_dataset == right_dataset and left_dataset in available_datasets:
            cardinality = contract.cardinality
            from_dataset = left_dataset
            from_key = contract.left_key
            to_key = contract.right_key
        elif joined_dataset == left_dataset and right_dataset in available_datasets:
            cardinality = _reverse_cardinality(contract.cardinality)
            if contract.default_join_type is JoinType.LEFT:
                raise ValueError("reversing an approved LEFT JOIN contract is unsupported")
            from_dataset = right_dataset
            from_key = contract.right_key
            to_key = contract.left_key
        else:
            raise ValueError("query join scans do not match the approved contract endpoints")
        from_aliases = tuple(
            alias for alias, dataset in available_relations.items() if dataset == from_dataset
        )
        if len(from_aliases) != 1 or not _join_key_matches(
            join.on.left,
            from_key.physical_field.root,
            from_key.transformation_plan,
            RelationAlias(from_aliases[0]),
        ):
            raise ValueError("query join left predicate does not match the approved join key")
        if not _join_key_matches(
            join.on.right,
            to_key.physical_field.root,
            to_key.transformation_plan,
            join.right_scan.alias,
        ):
            raise ValueError("query join right predicate does not match the approved join key")

        sourced_aggregates = tuple(item for item in aggregates if item.source is not None)
        if upstream_fanout_contract is not None and any(
            aggregate.operation not in _DUPLICATION_INVARIANT_OPERATIONS
            and dataset_by_alias[column.relation.root] == joined_dataset
            for aggregate in sourced_aggregates
            for column in _aggregate_columns(aggregate)
        ):
            raise ValueError(
                "downstream aggregate crosses an earlier fanout without approved mitigation"
            )
        affected = tuple(
            aggregate
            for aggregate in sourced_aggregates
            if any(
                dataset_by_alias[column.relation.root] in available_datasets
                for column in _aggregate_columns(aggregate)
            )
        )
        if cardinality is Cardinality.MANY_TO_MANY:
            raise ValueError("many-to-many advanced plans are unsupported")
        if cardinality is Cardinality.ONE_TO_MANY:
            if not aggregates:
                raise ValueError("one-to-many row queries require an explicit future grain policy")
            if any(
                aggregate.operation is AdvancedMetricOperation.COUNT_ROWS
                for aggregate in aggregates
            ):
                raise ValueError(
                    "count_rows is ambiguous across a one-to-many join; count an approved key"
                )
            if affected:
                if (
                    contract.fanout_policy
                    is not FanoutPolicy.REQUIRE_DISTINCT_FOR_LEFT_ENTITY_METRICS
                ):
                    raise ValueError("oriented fanout has no approved query-plan mitigation")
                if any(
                    aggregate.operation not in _DUPLICATION_INVARIANT_OPERATIONS
                    for aggregate in affected
                ):
                    raise ValueError(
                        "fanout policy requires COUNT DISTINCT, MIN, or MAX for an "
                        "existing-entity aggregate"
                    )
            upstream_fanout_contract = contract.id
        available_datasets.add(joined_dataset)
        available_relations[join.right_scan.alias.root] = joined_dataset


def _validate_boolean_shape(
    kind: BooleanOperator,
    comparison: object | None,
    operands: tuple[object, ...],
) -> None:
    if kind is BooleanOperator.COMPARISON:
        if comparison is None or operands:
            raise ValueError("comparison node requires exactly one comparison")
    elif kind in {BooleanOperator.AND, BooleanOperator.OR}:
        if comparison is not None or len(operands) < 2:
            raise ValueError("AND/OR node requires at least two operands")
    elif comparison is not None or len(operands) != 1:
        raise ValueError("NOT node requires exactly one operand")


PredicatePlan: TypeAlias = (
    PhysicalBooleanPredicate | AggregateBooleanPredicate | OutputBooleanPredicatePlan
)


def _predicate_complexity(value: PredicatePlan) -> tuple[int, int]:
    if value.kind is BooleanOperator.COMPARISON:
        return 1, 1
    stats = tuple(_predicate_complexity(item) for item in value.operands)
    return 1 + max(depth for depth, _leaves in stats), sum(leaves for _depth, leaves in stats)


def _validate_comparison_operands(
    *,
    operator: FilterOperator,
    values: tuple[ParameterValue, ...],
    has_comparison: bool,
) -> None:
    if operator in {FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL}:
        if values or has_comparison:
            raise ValueError("null predicate cannot carry another operand")
        return
    if operator is FilterOperator.IN:
        if not values or has_comparison or any(item.value is None for item in values):
            raise ValueError("IN predicate requires non-NULL parameter values only")
        return
    if has_comparison:
        if values:
            raise ValueError("alias comparison cannot also carry parameter values")
        return
    if len(values) != 1 or values[0].value is None:
        raise ValueError("comparison requires one non-NULL parameter or output")


def _physical_expressions_in(
    expression: AdvancedSelectExpression,
) -> tuple[AdvancedValueExpression | QueryValueExpression, ...]:
    if isinstance(expression, AdvancedAggregateExpression):
        return () if expression.source is None else (expression.source,)
    return (expression,)


def _columns_in(
    expression: AdvancedValueExpression | QueryValueExpression,
) -> tuple[ColumnExpression, ...]:
    if isinstance(expression, ColumnExpression):
        return (expression,)
    return _columns_in(expression.source)


def _aggregate_columns(
    expression: AdvancedAggregateExpression,
) -> tuple[ColumnExpression, ...]:
    if expression.source is None:
        return ()
    return _columns_in(expression.source)


def _dataset_for_column(column: ColumnExpression) -> PhysicalDatasetRef:
    return PhysicalDatasetRef(column.field.root.rsplit(".", 1)[0])


def _physical_predicate_leaves(
    value: PhysicalBooleanPredicate,
) -> tuple[FilterPredicate, ...]:
    if value.kind is BooleanOperator.COMPARISON:
        assert value.comparison is not None
        return (value.comparison,)
    return tuple(item for child in value.operands for item in _physical_predicate_leaves(child))


def _aggregate_predicate_leaves(
    value: AggregateBooleanPredicate,
) -> tuple[AggregateFilterPredicate, ...]:
    if value.kind is BooleanOperator.COMPARISON:
        assert value.comparison is not None
        return (value.comparison,)
    return tuple(item for child in value.operands for item in _aggregate_predicate_leaves(child))


def _output_predicate_leaves(
    value: OutputBooleanPredicatePlan,
) -> tuple[OutputFilterPredicate, ...]:
    if value.kind is BooleanOperator.COMPARISON:
        assert value.comparison is not None
        return (value.comparison,)
    return tuple(item for child in value.operands for item in _output_predicate_leaves(child))


def _reverse_cardinality(cardinality: Cardinality) -> Cardinality:
    if cardinality is Cardinality.ONE_TO_MANY:
        return Cardinality.MANY_TO_ONE
    if cardinality is Cardinality.MANY_TO_ONE:
        return Cardinality.ONE_TO_MANY
    return cardinality


def _join_key_matches(
    expression: QueryValueExpression,
    physical_field: str,
    transformation_plan: object,
    relation: RelationAlias,
) -> bool:
    return (
        isinstance(expression, MappedExpression)
        and expression.source.relation == relation
        and expression.source.field.root == physical_field
        and expression.transformation_plan == transformation_plan
    )
