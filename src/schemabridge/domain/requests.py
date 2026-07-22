"""Vendor-neutral analytical requests without executable SQL."""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from enum import StrEnum
from typing import TypeAlias

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.concepts import LogicalFieldRef, LogicalModelRef


class DateGrain(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class MetricOperation(StrEnum):
    COUNT = "count"
    COUNT_DISTINCT = "count_distinct"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"


class FilterOperator(StrEnum):
    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    GREATER_THAN = "greater_than"
    GREATER_THAN_OR_EQUAL = "greater_than_or_equal"
    LESS_THAN = "less_than"
    LESS_THAN_OR_EQUAL = "less_than_or_equal"
    IN = "in"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


class SortDirection(StrEnum):
    ASC = "asc"
    DESC = "desc"


FilterScalar: TypeAlias = str | int | float | bool | date | datetime | None

_DRAFT_ID = re.compile(r"^[a-z][a-z0-9_-]*$")


class Dimension(FrozenDomainModel):
    field: LogicalFieldRef
    grain: DateGrain | None = None


class Metric(FrozenDomainModel):
    operation: MetricOperation
    field: LogicalFieldRef
    alias: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")


class Filter(FrozenDomainModel):
    field: LogicalFieldRef
    operator: FilterOperator
    value: FilterScalar | tuple[FilterScalar, ...] = None

    @field_validator("value")
    @classmethod
    def numeric_values_must_be_finite(
        cls,
        value: FilterScalar | tuple[FilterScalar, ...],
    ) -> FilterScalar | tuple[FilterScalar, ...]:
        values = value if isinstance(value, tuple) else (value,)
        if any(isinstance(item, float) and not math.isfinite(item) for item in values):
            raise ValueError("filter values must be finite")
        return value

    @model_validator(mode="after")
    def value_shape_must_match_operator(self) -> Filter:
        if self.operator in {FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL}:
            if self.value is not None:
                raise ValueError("null filters cannot carry a value")
        elif self.operator is FilterOperator.IN:
            if not isinstance(self.value, tuple) or not self.value:
                raise ValueError("IN filters require at least one value")
            if any(value is None for value in self.value):
                raise ValueError("IN filters cannot contain NULL; use is_null explicitly")
        elif self.value is None or isinstance(self.value, tuple):
            raise ValueError("comparison filters require exactly one non-NULL value")
        return self


class OrderBy(FrozenDomainModel):
    field: LogicalFieldRef
    direction: SortDirection = SortDirection.ASC


class AnalyticalRequest(FrozenDomainModel):
    """Restricted analytical intent; it cannot carry raw SQL."""

    primary_entity: LogicalModelRef
    dimensions: tuple[Dimension, ...] = ()
    metrics: tuple[Metric, ...] = Field(min_length=1)
    filters: tuple[Filter, ...] = ()
    order_by: tuple[OrderBy, ...] = ()
    limit: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode="after")
    def selections_must_be_unambiguous(self) -> AnalyticalRequest:
        dimensions = [item.field.root for item in self.dimensions]
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("analytical request dimensions must be unique")

        metric_signatures = [(item.operation, item.field.root) for item in self.metrics]
        if len(metric_signatures) != len(set(metric_signatures)):
            raise ValueError("analytical request metrics must be unique")
        aliases = [item.alias for item in self.metrics if item.alias is not None]
        if len(aliases) != len(set(aliases)):
            raise ValueError("analytical request metric aliases must be unique")

        ordering = [item.field.root for item in self.order_by]
        if len(ordering) != len(set(ordering)):
            raise ValueError("analytical request order fields must be unique")
        selected = set(dimensions) | {item.field.root for item in self.metrics}
        if any(field not in selected for field in ordering):
            raise ValueError("order fields must also be selected as a dimension or metric")
        return self


class AnalyticalRequestDraft(FrozenDomainModel):
    """Local request draft; loading always revalidates against current approved context."""

    id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    request: AnalyticalRequest

    @field_validator("id")
    @classmethod
    def id_must_be_inert(cls, value: str) -> str:
        if _DRAFT_ID.fullmatch(value) is None:
            raise ValueError("request draft id must be a lowercase inert identifier")
        return value
