"""Version-2 governed analytical requests for bounded advanced PostgreSQL."""

from __future__ import annotations

import math
import re
from enum import StrEnum
from typing import Literal, Self, TypeAlias

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.concepts import LogicalFieldRef, LogicalModelRef
from schemabridge.domain.requests import (
    AnalyticalRequest,
    DateGrain,
    Filter,
    FilterOperator,
    FilterScalar,
    SortDirection,
)

MAX_PREDICATE_DEPTH = 4
MAX_PREDICATE_LEAVES = 16
MAX_PREDICATE_OPERANDS = 8
MAX_IN_VALUES = 64
MAX_WINDOWS = 4

_SQL_LIKE_VALUE = re.compile(
    r"(?i)(\b(?:select|insert|update|delete|drop|alter|create|truncate|copy|grant|revoke)\b|"
    r"/\*|--|;)"
)
_ALIAS_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
_POSTGRESQL_RESERVED_ALIASES = frozenset(
    {
        "all",
        "analyse",
        "analyze",
        "and",
        "any",
        "array",
        "as",
        "asc",
        "asymmetric",
        "authorization",
        "binary",
        "both",
        "case",
        "cast",
        "check",
        "collate",
        "collation",
        "column",
        "concurrently",
        "constraint",
        "create",
        "cross",
        "current_catalog",
        "current_date",
        "current_role",
        "current_schema",
        "current_time",
        "current_timestamp",
        "current_user",
        "default",
        "deferrable",
        "desc",
        "distinct",
        "do",
        "else",
        "end",
        "except",
        "false",
        "fetch",
        "for",
        "foreign",
        "freeze",
        "from",
        "full",
        "grant",
        "group",
        "having",
        "ilike",
        "in",
        "initially",
        "inner",
        "intersect",
        "into",
        "is",
        "isnull",
        "join",
        "lateral",
        "leading",
        "left",
        "like",
        "limit",
        "localtime",
        "localtimestamp",
        "natural",
        "not",
        "notnull",
        "null",
        "offset",
        "on",
        "only",
        "or",
        "order",
        "outer",
        "overlaps",
        "placing",
        "primary",
        "references",
        "returning",
        "right",
        "select",
        "session_user",
        "similar",
        "some",
        "symmetric",
        "table",
        "tablesample",
        "then",
        "to",
        "trailing",
        "true",
        "union",
        "unique",
        "user",
        "using",
        "variadic",
        "verbose",
        "when",
        "where",
        "window",
        "with",
    }
)


def validate_postgresql_alias(value: str) -> str:
    """Fail before preview when an unquoted PostgreSQL output name is unsafe."""

    if re.fullmatch(_ALIAS_PATTERN, value) is None:
        raise ValueError("output alias must be an inert ASCII identifier")
    if len(value.encode("ascii")) > 63:
        raise ValueError("output alias must fit PostgreSQL's 63-byte identifier limit")
    if value.casefold() in _POSTGRESQL_RESERVED_ALIASES:
        raise ValueError("output alias cannot be a PostgreSQL reserved keyword")
    return value


class BooleanOperator(StrEnum):
    COMPARISON = "comparison"
    AND = "and"
    OR = "or"
    NOT = "not"


class LogicalBooleanPredicate(FrozenDomainModel):
    """Bounded boolean tree whose leaves are ordinary typed logical filters."""

    kind: BooleanOperator
    comparison: Filter | None = None
    operands: tuple[LogicalBooleanPredicate, ...] = Field(
        default=(),
        max_length=MAX_PREDICATE_OPERANDS,
    )

    @model_validator(mode="after")
    def shape_and_complexity_must_be_bounded(self) -> Self:
        if self.kind is BooleanOperator.COMPARISON:
            if self.comparison is None or self.operands:
                raise ValueError("comparison predicate requires exactly one typed filter")
            _reject_sql_like_filter_value(self.comparison.value)
            if (
                self.comparison.operator is FilterOperator.IN
                and isinstance(self.comparison.value, tuple)
                and len(self.comparison.value) > MAX_IN_VALUES
            ):
                raise ValueError("logical IN filters support at most sixty-four values")
        elif self.kind in {BooleanOperator.AND, BooleanOperator.OR}:
            if self.comparison is not None or len(self.operands) < 2:
                raise ValueError("AND/OR predicates require at least two operands")
        elif self.comparison is not None or len(self.operands) != 1:
            raise ValueError("NOT predicate requires exactly one operand")
        depth, leaves = predicate_complexity(self)
        if depth > MAX_PREDICATE_DEPTH:
            raise ValueError("predicate exceeds the four-level complexity limit")
        if leaves > MAX_PREDICATE_LEAVES:
            raise ValueError("predicate exceeds the sixteen-leaf complexity limit")
        return self

    @classmethod
    def leaf(cls, value: Filter) -> LogicalBooleanPredicate:
        return cls(kind=BooleanOperator.COMPARISON, comparison=value)

    @classmethod
    def all_of(cls, *values: LogicalBooleanPredicate) -> LogicalBooleanPredicate:
        return cls(kind=BooleanOperator.AND, operands=values)

    @classmethod
    def any_of(cls, *values: LogicalBooleanPredicate) -> LogicalBooleanPredicate:
        return cls(kind=BooleanOperator.OR, operands=values)

    @classmethod
    def negate(cls, value: LogicalBooleanPredicate) -> LogicalBooleanPredicate:
        return cls(kind=BooleanOperator.NOT, operands=(value,))


class OutputFilter(FrozenDomainModel):
    """Typed comparison against one previously declared output alias."""

    alias: str = Field(pattern=_ALIAS_PATTERN)
    operator: FilterOperator
    value: FilterScalar | tuple[FilterScalar, ...] = None
    compare_to_alias: str | None = Field(default=None, pattern=_ALIAS_PATTERN)

    @field_validator("value")
    @classmethod
    def numeric_values_must_be_finite(
        cls,
        value: FilterScalar | tuple[FilterScalar, ...],
    ) -> FilterScalar | tuple[FilterScalar, ...]:
        values = value if isinstance(value, tuple) else (value,)
        if any(isinstance(item, float) and not math.isfinite(item) for item in values):
            raise ValueError("output-filter values must be finite")
        _reject_sql_like_filter_value(value)
        return value

    @model_validator(mode="after")
    def value_shape_must_match_operator(self) -> Self:
        if self.operator in {FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL}:
            if self.value is not None or self.compare_to_alias is not None:
                raise ValueError("null output filters cannot carry a value")
        elif self.operator is FilterOperator.IN:
            if (
                not isinstance(self.value, tuple)
                or not self.value
                or self.compare_to_alias is not None
            ):
                raise ValueError("output IN filters require at least one value")
            if len(self.value) > MAX_IN_VALUES:
                raise ValueError("output IN filters support at most sixty-four values")
            if any(value is None for value in self.value):
                raise ValueError("output IN filters cannot contain NULL")
        elif (self.value is None) == (self.compare_to_alias is None) or isinstance(
            self.value, tuple
        ):
            raise ValueError(
                "output comparisons require exactly one scalar or output-alias operand"
            )
        return self


class OutputBooleanPredicate(FrozenDomainModel):
    """Bounded boolean tree over aggregate or window output aliases."""

    kind: BooleanOperator
    comparison: OutputFilter | None = None
    operands: tuple[OutputBooleanPredicate, ...] = Field(
        default=(),
        max_length=MAX_PREDICATE_OPERANDS,
    )

    @model_validator(mode="after")
    def shape_and_complexity_must_be_bounded(self) -> Self:
        if self.kind is BooleanOperator.COMPARISON:
            if self.comparison is None or self.operands:
                raise ValueError("comparison predicate requires exactly one output filter")
        elif self.kind in {BooleanOperator.AND, BooleanOperator.OR}:
            if self.comparison is not None or len(self.operands) < 2:
                raise ValueError("AND/OR predicates require at least two operands")
        elif self.comparison is not None or len(self.operands) != 1:
            raise ValueError("NOT predicate requires exactly one operand")
        depth, leaves = predicate_complexity(self)
        if depth > MAX_PREDICATE_DEPTH:
            raise ValueError("predicate exceeds the four-level complexity limit")
        if leaves > MAX_PREDICATE_LEAVES:
            raise ValueError("predicate exceeds the sixteen-leaf complexity limit")
        return self

    @classmethod
    def leaf(cls, value: OutputFilter) -> OutputBooleanPredicate:
        return cls(kind=BooleanOperator.COMPARISON, comparison=value)

    @classmethod
    def all_of(cls, *values: OutputBooleanPredicate) -> OutputBooleanPredicate:
        return cls(kind=BooleanOperator.AND, operands=values)

    @classmethod
    def any_of(cls, *values: OutputBooleanPredicate) -> OutputBooleanPredicate:
        return cls(kind=BooleanOperator.OR, operands=values)

    @classmethod
    def negate(cls, value: OutputBooleanPredicate) -> OutputBooleanPredicate:
        return cls(kind=BooleanOperator.NOT, operands=(value,))


PredicateTree: TypeAlias = LogicalBooleanPredicate | OutputBooleanPredicate


class OutputOrder(FrozenDomainModel):
    alias: str = Field(pattern=_ALIAS_PATTERN)
    direction: SortDirection = SortDirection.ASC


class WindowOperation(StrEnum):
    ROW_NUMBER = "row_number"
    RANK = "rank"
    DENSE_RANK = "dense_rank"
    NTILE = "ntile"
    RUNNING_SUM = "running_sum"
    RUNNING_AVG = "running_avg"
    MOVING_SUM = "moving_sum"
    MOVING_AVG = "moving_avg"
    PARTITION_AVG = "partition_avg"
    LAG = "lag"
    LEAD = "lead"
    DELTA_FROM_PREVIOUS = "delta_from_previous"
    PERCENT_CHANGE_FROM_PREVIOUS = "percent_change_from_previous"
    PERCENT_OF_TOTAL = "percent_of_total"


_RANKING_WINDOWS = frozenset(
    {
        WindowOperation.ROW_NUMBER,
        WindowOperation.RANK,
        WindowOperation.DENSE_RANK,
    }
)
_RUNNING_WINDOWS = frozenset(
    {
        WindowOperation.RUNNING_SUM,
        WindowOperation.RUNNING_AVG,
    }
)
_MOVING_WINDOWS = frozenset(
    {
        WindowOperation.MOVING_SUM,
        WindowOperation.MOVING_AVG,
    }
)
_OFFSET_WINDOWS = frozenset(
    {
        WindowOperation.LAG,
        WindowOperation.LEAD,
        WindowOperation.DELTA_FROM_PREVIOUS,
        WindowOperation.PERCENT_CHANGE_FROM_PREVIOUS,
    }
)


class AdvancedMetricOperation(StrEnum):
    COUNT_ROWS = "count_rows"
    COUNT = "count"
    COUNT_DISTINCT = "count_distinct"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"


_NUMERIC_METRIC_OPERATIONS = frozenset(
    {
        AdvancedMetricOperation.COUNT_ROWS,
        AdvancedMetricOperation.COUNT,
        AdvancedMetricOperation.COUNT_DISTINCT,
        AdvancedMetricOperation.SUM,
        AdvancedMetricOperation.AVG,
        AdvancedMetricOperation.MIN,
        AdvancedMetricOperation.MAX,
    }
)


class WindowCalculation(FrozenDomainModel):
    """One closed window calculation over selected output aliases."""

    operation: WindowOperation
    alias: str = Field(pattern=_ALIAS_PATTERN)
    source: str | None = Field(default=None, pattern=_ALIAS_PATTERN)
    partition_by: tuple[str, ...] = Field(default=(), max_length=3)
    order_by: tuple[OutputOrder, ...] = Field(default=(), max_length=3)
    buckets: int | None = Field(default=None, ge=2, le=100)
    offset: int | None = Field(default=None, ge=1, le=100)
    preceding_rows: int | None = Field(default=None, ge=1, le=365)

    @field_validator("partition_by")
    @classmethod
    def partition_aliases_must_be_inert_and_unique(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("window partition aliases must be unique")
        if any(re.fullmatch(_ALIAS_PATTERN, value) is None for value in values):
            raise ValueError("window partition aliases must be inert")
        return values

    @model_validator(mode="after")
    def operation_shape_must_be_closed(self) -> Self:
        ordered_aliases = [item.alias for item in self.order_by]
        if len(ordered_aliases) != len(set(ordered_aliases)):
            raise ValueError("window order aliases must be unique")
        if self.operation in _RANKING_WINDOWS:
            if (
                self.source is not None
                or not self.order_by
                or any(
                    value is not None for value in (self.buckets, self.offset, self.preceding_rows)
                )
            ):
                raise ValueError("ranking windows require ordering and no source metric")
        elif self.operation is WindowOperation.NTILE:
            if (
                self.source is not None
                or not self.order_by
                or self.buckets is None
                or self.offset is not None
                or self.preceding_rows is not None
            ):
                raise ValueError("NTILE requires ordering and a bounded bucket count")
        elif self.operation in _RUNNING_WINDOWS:
            if (
                self.source is None
                or not self.order_by
                or any(
                    value is not None for value in (self.buckets, self.offset, self.preceding_rows)
                )
            ):
                raise ValueError("running windows require a source metric and ordering")
        elif self.operation in _MOVING_WINDOWS:
            if (
                self.source is None
                or not self.order_by
                or self.preceding_rows is None
                or self.buckets is not None
                or self.offset is not None
            ):
                raise ValueError(
                    "moving windows require a source metric, ordering, and bounded frame"
                )
        elif self.operation in _OFFSET_WINDOWS:
            if (
                self.source is None
                or not self.order_by
                or self.offset is None
                or self.buckets is not None
                or self.preceding_rows is not None
            ):
                raise ValueError("offset windows require a source metric, order, and offset")
        elif self.operation is WindowOperation.PARTITION_AVG:
            if (
                self.source is None
                or self.order_by
                or any(
                    value is not None for value in (self.buckets, self.offset, self.preceding_rows)
                )
            ):
                raise ValueError("partition average requires only a source metric")
        elif self.operation is WindowOperation.PERCENT_OF_TOTAL and (
            self.source is None
            or self.order_by
            or any(value is not None for value in (self.buckets, self.offset, self.preceding_rows))
        ):
            raise ValueError("percentage-of-total requires a source metric and no ordering")
        return self


class GroupingMode(StrEnum):
    STANDARD = "standard"
    ROLLUP = "rollup"


class AdvancedMetric(FrozenDomainModel):
    """Aggregate metric with an optional typed row-level condition."""

    operation: AdvancedMetricOperation
    field: LogicalFieldRef | None = None
    alias: str | None = Field(default=None, pattern=_ALIAS_PATTERN)
    condition: LogicalBooleanPredicate | None = None

    @model_validator(mode="after")
    def source_shape_must_match_operation(self) -> Self:
        if self.operation is AdvancedMetricOperation.COUNT_ROWS:
            if self.field is not None:
                raise ValueError("count_rows cannot carry a logical field")
        elif self.field is None:
            raise ValueError(f"{self.operation.value} requires one approved logical field")
        return self


class NumericBucket(FrozenDomainModel):
    """One ordered non-overlapping numeric CASE branch."""

    label: str = Field(min_length=1, max_length=80)
    lower: float | int | None = None
    upper: float | int | None = None

    @model_validator(mode="after")
    def range_must_be_nonempty_and_finite(self) -> Self:
        if self.lower is None and self.upper is None:
            raise ValueError("numeric bucket requires at least one bound")
        if any(
            isinstance(value, float) and not math.isfinite(value)
            for value in (self.lower, self.upper)
            if value is not None
        ):
            raise ValueError("numeric bucket bounds must be finite")
        if self.lower is not None and self.upper is not None and self.lower >= self.upper:
            raise ValueError("numeric bucket lower bound must be smaller than upper bound")
        return self


class AdvancedField(FrozenDomainModel):
    """Selected logical field with optional date grain or numeric CASE bucketing."""

    field: LogicalFieldRef
    grain: DateGrain | None = None
    buckets: tuple[NumericBucket, ...] = Field(default=(), max_length=12)
    else_label: str | None = Field(default=None, min_length=1, max_length=80)
    alias: str | None = Field(default=None, pattern=_ALIAS_PATTERN)

    @model_validator(mode="after")
    def bucketing_must_be_unambiguous(self) -> Self:
        if self.grain is not None and self.buckets:
            raise ValueError("date grain and numeric bucketing cannot be combined")
        if self.buckets and self.else_label is None:
            raise ValueError("numeric bucketing requires an explicit else label")
        if not self.buckets and self.else_label is not None:
            raise ValueError("bucket else label requires bucket definitions")
        for previous, current in zip(self.buckets, self.buckets[1:], strict=False):
            if previous.upper is None or current.lower is None:
                raise ValueError("only the first/last numeric bucket may be open-ended")
            if previous.upper > current.lower:
                raise ValueError("numeric bucket ranges cannot overlap")
        labels = [item.label for item in self.buckets]
        if len(labels) != len(set(labels)) or self.else_label in labels:
            raise ValueError("numeric bucket labels must be unique")
        return self


class AdvancedQueryMode(StrEnum):
    ROWS = "rows"
    AGGREGATE = "aggregate"


class AdvancedAnalyticalRequest(FrozenDomainModel):
    """Version-2 intent; every advanced operation remains typed and bounded."""

    version: Literal[2]
    mode: AdvancedQueryMode
    primary_entity: LogicalModelRef
    fields: tuple[AdvancedField, ...] = Field(default=(), max_length=24)
    metrics: tuple[AdvancedMetric, ...] = Field(default=(), max_length=12)
    group_by: tuple[str, ...] = Field(default=(), max_length=12)
    where: LogicalBooleanPredicate | None = None
    having: OutputBooleanPredicate | None = None
    windows: tuple[WindowCalculation, ...] = Field(default=(), max_length=MAX_WINDOWS)
    post_filter: OutputBooleanPredicate | None = None
    result_order_by: tuple[OutputOrder, ...] = Field(default=(), max_length=4)
    grouping: GroupingMode = GroupingMode.STANDARD
    limit: int = Field(default=100, ge=1, le=1_000)

    @model_validator(mode="after")
    def advanced_stages_must_resolve_exactly(self) -> Self:
        if self.grouping is not GroupingMode.STANDARD:
            raise ValueError("ROLLUP is unsupported until subtotal NULL semantics are explicit")
        field_aliases = tuple(
            item.alias or item.field.root.rsplit(".", 1)[-1] for item in self.fields
        )
        metric_aliases = tuple(
            item.alias
            or (
                item.operation.value
                if item.field is None
                else f"{item.operation.value}_{item.field.root.rsplit('.', 1)[-1]}"
            )
            for item in self.metrics
        )
        referenced_aliases = (
            *field_aliases,
            *metric_aliases,
            *self.group_by,
            *(item.alias for item in self.windows),
            *(
                alias
                for item in self.windows
                for alias in (
                    *item.partition_by,
                    *(order.alias for order in item.order_by),
                    *((item.source,) if item.source is not None else ()),
                )
            ),
            *(item.alias for item in self.result_order_by),
            *(output_predicate_aliases(self.having) if self.having is not None else ()),
            *(output_predicate_aliases(self.post_filter) if self.post_filter is not None else ()),
        )
        for alias in referenced_aliases:
            validate_postgresql_alias(alias)
        if not field_aliases and not metric_aliases:
            raise ValueError("advanced request requires at least one selected field or metric")
        field_ids = [item.field.root for item in self.fields]
        if len(field_ids) != len(set(field_ids)):
            raise ValueError("advanced selected logical fields must be unique")
        metric_signatures = [
            (
                item.operation,
                item.field.root if item.field is not None else None,
                item.condition,
            )
            for item in self.metrics
        ]
        if len(metric_signatures) != len(set(metric_signatures)):
            raise ValueError("advanced metrics must be unique")
        base_aliases = (*field_aliases, *metric_aliases)
        if len(base_aliases) != len({alias.casefold() for alias in base_aliases}):
            raise ValueError("selected field and metric aliases must be globally unique")

        if len(self.group_by) != len({alias.casefold() for alias in self.group_by}):
            raise ValueError("advanced grouping aliases must be unique")
        if self.mode is AdvancedQueryMode.ROWS:
            if self.metrics or self.group_by or self.having is not None:
                raise ValueError("row mode cannot carry aggregate metrics, grouping, or HAVING")
            if self.grouping is not GroupingMode.STANDARD:
                raise ValueError("row mode cannot use ROLLUP")
        else:
            if not self.metrics:
                raise ValueError("aggregate mode requires at least one metric")
            if set(self.group_by) != set(field_aliases):
                raise ValueError(
                    "aggregate mode must group every selected non-aggregate field exactly once"
                )

        window_aliases = tuple(item.alias for item in self.windows)
        if len(window_aliases) != len({alias.casefold() for alias in window_aliases}):
            raise ValueError("window output aliases must be unique")
        if {alias.casefold() for alias in window_aliases}.intersection(
            alias.casefold() for alias in base_aliases
        ):
            raise ValueError("window aliases cannot shadow selected output aliases")

        base = set(base_aliases)
        metrics_by_alias = dict(zip(metric_aliases, self.metrics, strict=True))
        for window in self.windows:
            if len(window.partition_by) != len({alias.casefold() for alias in window.partition_by}):
                raise ValueError("window partition aliases must be unique")
            order_aliases = tuple(item.alias for item in window.order_by)
            if len(order_aliases) != len({alias.casefold() for alias in order_aliases}):
                raise ValueError("window order aliases must be unique")
            references = {
                *window.partition_by,
                *(item.alias for item in window.order_by),
            }
            if not references <= base:
                raise ValueError("window partition/order references an unknown base output")
            if window.source is not None:
                metric = metrics_by_alias.get(window.source)
                field = next(
                    (
                        item
                        for alias, item in zip(field_aliases, self.fields, strict=True)
                        if alias == window.source
                    ),
                    None,
                )
                if metric is None and field is None:
                    raise ValueError("window source must reference one selected output alias")
                if (
                    metric is not None
                    and window.operation
                    in {
                        *_RUNNING_WINDOWS,
                        *_MOVING_WINDOWS,
                        WindowOperation.PARTITION_AVG,
                        WindowOperation.DELTA_FROM_PREVIOUS,
                        WindowOperation.PERCENT_CHANGE_FROM_PREVIOUS,
                        WindowOperation.PERCENT_OF_TOTAL,
                    }
                    and metric.operation not in _NUMERIC_METRIC_OPERATIONS
                ):
                    raise ValueError("window source metric is not guaranteed numeric")
        if self.having is not None:
            aliases = set(output_predicate_aliases(self.having))
            if not aliases <= set(metric_aliases):
                raise ValueError("HAVING predicates may reference only selected metric aliases")
        if self.post_filter is not None:
            aliases = set(output_predicate_aliases(self.post_filter))
            if not aliases <= (base | set(window_aliases)):
                raise ValueError(
                    "post-window predicates reference an unknown selected/window alias"
                )
        available_outputs = base | set(window_aliases)
        result_aliases = [item.alias for item in self.result_order_by]
        if len(result_aliases) != len({alias.casefold() for alias in result_aliases}):
            raise ValueError("final order aliases must be unique")
        if any(alias not in available_outputs for alias in result_aliases):
            raise ValueError("final ordering references an unknown output alias")
        return self


AnalyticalRequestLike: TypeAlias = AdvancedAnalyticalRequest | AnalyticalRequest


def predicate_complexity(value: PredicateTree) -> tuple[int, int]:
    """Return maximum node depth and comparison-leaf count."""

    if value.kind is BooleanOperator.COMPARISON:
        return 1, 1
    child_stats = tuple(predicate_complexity(item) for item in value.operands)
    return 1 + max(depth for depth, _leaves in child_stats), sum(
        leaves for _depth, leaves in child_stats
    )


def logical_predicate_filters(
    value: LogicalBooleanPredicate,
) -> tuple[Filter, ...]:
    if value.kind is BooleanOperator.COMPARISON:
        assert value.comparison is not None
        return (value.comparison,)
    return tuple(item for operand in value.operands for item in logical_predicate_filters(operand))


def output_predicate_filters(
    value: OutputBooleanPredicate,
) -> tuple[OutputFilter, ...]:
    if value.kind is BooleanOperator.COMPARISON:
        assert value.comparison is not None
        return (value.comparison,)
    return tuple(item for operand in value.operands for item in output_predicate_filters(operand))


def output_predicate_aliases(value: OutputBooleanPredicate) -> tuple[str, ...]:
    values: list[str] = []
    for item in output_predicate_filters(value):
        values.append(item.alias)
        if item.compare_to_alias is not None:
            values.append(item.compare_to_alias)
    return tuple(values)


def request_output_aliases(
    value: AnalyticalRequestLike,
) -> tuple[str, ...]:
    if isinstance(value, AdvancedAnalyticalRequest):
        fields = tuple(item.alias or item.field.root.rsplit(".", 1)[-1] for item in value.fields)
        metrics = tuple(
            item.alias
            or (
                item.operation.value
                if item.field is None
                else f"{item.operation.value}_{item.field.root.rsplit('.', 1)[-1]}"
            )
            for item in value.metrics
        )
        windows = tuple(item.alias for item in value.windows)
    else:
        fields = tuple(item.field.root.rsplit(".", 1)[-1] for item in value.dimensions)
        metrics = tuple(
            item.alias or f"{item.operation.value}_{item.field.root.rsplit('.', 1)[-1]}"
            for item in value.metrics
        )
        windows = ()
    return (*fields, *metrics, *windows)


def _reject_sql_like_filter_value(
    value: FilterScalar | tuple[FilterScalar, ...],
) -> None:
    values = value if isinstance(value, tuple) else (value,)
    if any(isinstance(item, str) and _SQL_LIKE_VALUE.search(item) for item in values):
        raise ValueError("advanced request values cannot contain SQL-like payloads")
