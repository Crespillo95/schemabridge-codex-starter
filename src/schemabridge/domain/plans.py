"""Restricted, vendor-neutral query intermediate representation."""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal, TypeAlias

from pydantic import ConfigDict, Field, RootModel, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.decisions import ApprovalStatus
from schemabridge.domain.fields import PhysicalDatasetRef, PhysicalFieldRef
from schemabridge.domain.joins import Cardinality, FanoutPolicy, JoinContract
from schemabridge.domain.requests import (
    DateGrain,
    FilterOperator,
    MetricOperation,
    SortDirection,
)
from schemabridge.domain.transformations import TransformationPlan

_INERT_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class RelationAlias(RootModel[str]):
    """Validated relation alias used by the restricted query IR."""

    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def alias_must_be_inert(cls, value: str) -> str:
        if _INERT_IDENTIFIER.fullmatch(value) is None:
            raise ValueError("relation alias must be an inert identifier")
        return value


class OutputAlias(RootModel[str]):
    """Validated output alias used by projections and ordering."""

    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def alias_must_be_inert(cls, value: str) -> str:
        if _INERT_IDENTIFIER.fullmatch(value) is None:
            raise ValueError("output alias must be an inert identifier")
        return value


ParameterScalar: TypeAlias = str | int | float | bool | date | datetime | Decimal | None


class ParameterValue(FrozenDomainModel):
    """Typed value that must be bound by the compiler, never interpolated."""

    value: ParameterScalar

    @field_validator("value")
    @classmethod
    def numeric_values_must_be_finite(cls, value: ParameterScalar) -> ParameterScalar:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("query parameter floats must be finite")
        if isinstance(value, Decimal) and not value.is_finite():
            raise ValueError("query parameter decimals must be finite")
        return value


class DatasetScan(FrozenDomainModel):
    """One allowlist-addressable physical dataset scan."""

    dataset: PhysicalDatasetRef
    alias: RelationAlias


class ColumnExpression(FrozenDomainModel):
    """Column selected from a declared scan; no text expression is accepted."""

    kind: Literal["column"] = "column"
    relation: RelationAlias
    field: PhysicalFieldRef


class MappedExpression(FrozenDomainModel):
    """Physical column plus a closed approved transformation plan."""

    kind: Literal["mapped"] = "mapped"
    source: ColumnExpression
    transformation_plan: TransformationPlan


class DateGrainExpression(FrozenDomainModel):
    """Supported date truncation over one physical column."""

    kind: Literal["date_grain"] = "date_grain"
    source: ColumnExpression | MappedExpression
    grain: DateGrain


QueryValueExpression: TypeAlias = Annotated[
    ColumnExpression | MappedExpression | DateGrainExpression,
    Field(discriminator="kind"),
]


class AggregateExpression(FrozenDomainModel):
    """Closed aggregate operation over a typed value expression."""

    kind: Literal["aggregate"] = "aggregate"
    operation: MetricOperation
    source: QueryValueExpression


SelectExpression: TypeAlias = Annotated[
    ColumnExpression | MappedExpression | DateGrainExpression | AggregateExpression,
    Field(discriminator="kind"),
]


class SelectItem(FrozenDomainModel):
    expression: SelectExpression
    alias: OutputAlias


class FilterPredicate(FrozenDomainModel):
    """Typed predicate whose values become driver parameters."""

    expression: QueryValueExpression
    operator: FilterOperator
    values: tuple[ParameterValue, ...] = ()

    @model_validator(mode="after")
    def value_count_must_match_operator(self) -> FilterPredicate:
        if self.operator in {FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL}:
            if self.values:
                raise ValueError("null predicates cannot carry parameter values")
        elif self.operator is FilterOperator.IN:
            if not self.values:
                raise ValueError("IN predicate requires at least one parameter value")
        elif len(self.values) != 1:
            raise ValueError("comparison predicate requires exactly one parameter value")
        return self


class JoinPredicate(FrozenDomainModel):
    """Equijoin between two typed expressions."""

    left: QueryValueExpression
    right: QueryValueExpression


class ApprovedJoin(FrozenDomainModel):
    """Physical join backed by an explicitly approved logical contract."""

    contract: JoinContract
    right_scan: DatasetScan
    on: JoinPredicate

    @model_validator(mode="after")
    def contract_must_be_approved(self) -> ApprovedJoin:
        if self.contract.status is not ApprovalStatus.APPROVED:
            raise ValueError("query joins require an approved join contract")
        return self


class OrderItem(FrozenDomainModel):
    expression: QueryValueExpression
    direction: SortDirection = SortDirection.ASC


class AllowedAsset(FrozenDomainModel):
    """Dataset and exact column names accepted by the independent SQL guard."""

    dataset: PhysicalDatasetRef
    columns: tuple[str, ...] = Field(min_length=1)

    @field_validator("columns")
    @classmethod
    def columns_must_be_unique_inert_identifiers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("allowlisted columns must be unique")
        if any(_INERT_IDENTIFIER.fullmatch(value) is None for value in values):
            raise ValueError("allowlisted columns must be inert identifiers")
        return values


class QueryPolicy(FrozenDomainModel):
    """Explicit independent guard and preview limits."""

    assets: tuple[AllowedAsset, ...] = Field(min_length=1)
    max_tables: int = Field(default=3, ge=1, le=3)
    max_preview_rows: int = Field(default=500, ge=1, le=10_000)
    statement_timeout_ms: int = Field(default=5_000, ge=10, le=60_000)

    @model_validator(mode="after")
    def assets_must_be_unique(self) -> QueryPolicy:
        datasets = [asset.dataset.root for asset in self.assets]
        if len(datasets) != len(set(datasets)):
            raise ValueError("allowlisted assets must be unique")
        return self


def _columns_in(
    expression: SelectExpression | QueryValueExpression,
) -> tuple[ColumnExpression, ...]:
    if isinstance(expression, ColumnExpression):
        return (expression,)
    if isinstance(expression, (MappedExpression, DateGrainExpression)):
        return _columns_in(expression.source)
    return _columns_in(expression.source)


class QueryPlan(FrozenDomainModel):
    """Complete restricted IR consumed by deterministic compiler adapters."""

    version: Literal[1] = 1
    root_scan: DatasetScan
    joins: tuple[ApprovedJoin, ...] = Field(default=(), max_length=2)
    projections: tuple[SelectItem, ...] = Field(min_length=1)
    filters: tuple[FilterPredicate, ...] = ()
    group_by: tuple[QueryValueExpression, ...] = ()
    order_by: tuple[OrderItem, ...] = ()
    limit: int | None = Field(default=None, ge=1, le=10_000)

    @model_validator(mode="after")
    def references_must_resolve_to_declared_scans(self) -> QueryPlan:
        scans: dict[str, PhysicalDatasetRef] = {self.root_scan.alias.root: self.root_scan.dataset}
        datasets = {self.root_scan.dataset.root}

        for join in self.joins:
            right_alias = join.right_scan.alias.root
            right_dataset = join.right_scan.dataset.root
            if right_alias in scans:
                raise ValueError("scan aliases must be unique")
            if right_dataset in datasets:
                raise ValueError("self joins and repeated physical datasets are outside the MVP")

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

        aliases = [item.alias.root for item in self.projections]
        if len(aliases) != len(set(aliases)):
            raise ValueError("projection aliases must be unique")

        expressions: list[SelectExpression | QueryValueExpression] = [
            item.expression for item in self.projections
        ]
        expressions.extend(predicate.expression for predicate in self.filters)
        expressions.extend(self.group_by)
        expressions.extend(item.expression for item in self.order_by)
        for join in self.joins:
            expressions.extend((join.on.left, join.on.right))

        for expression in expressions:
            for column in _columns_in(expression):
                dataset = scans.get(column.relation.root)
                if dataset is None:
                    raise ValueError("expression references an undeclared relation")
                field_parts = column.field.root.split(".")
                if len(field_parts) != 3:
                    raise ValueError("query columns must have schema.dataset.column identity")
                if ".".join(field_parts[:2]) != dataset.root:
                    raise ValueError("column identity does not belong to its declared scan")

        grouped = set(self.group_by)
        has_aggregate = any(
            isinstance(item.expression, AggregateExpression) for item in self.projections
        )
        if has_aggregate:
            ungrouped = [
                item.expression
                for item in self.projections
                if not isinstance(item.expression, AggregateExpression)
                and item.expression not in grouped
            ]
            if ungrouped:
                raise ValueError("non-aggregate projections must appear in group_by")

        aggregates = tuple(
            item.expression
            for item in self.projections
            if isinstance(item.expression, AggregateExpression)
        )
        fanout_joins = tuple(
            join
            for join in self.joins
            if join.contract.cardinality in {Cardinality.ONE_TO_MANY, Cardinality.MANY_TO_MANY}
        )
        dataset_by_alias = {self.root_scan.alias.root: self.root_scan.dataset.root}
        dataset_by_alias.update(
            {join.right_scan.alias.root: join.right_scan.dataset.root for join in self.joins}
        )
        for join in fanout_joins:
            contract = join.contract
            if contract.fanout_policy is not FanoutPolicy.REQUIRE_DISTINCT_FOR_LEFT_ENTITY_METRICS:
                raise ValueError(
                    "fanout policy requires a query-plan mitigation not implemented in M03"
                )
            left_dataset = contract.left_key.physical_field.root.rsplit(".", 1)[0]
            if any(
                aggregate.operation is not MetricOperation.COUNT_DISTINCT
                and any(
                    dataset_by_alias[column.relation.root] == left_dataset
                    for column in _columns_in(aggregate.source)
                )
                for aggregate in aggregates
            ):
                raise ValueError(
                    "fanout policy requires COUNT DISTINCT for left-entity aggregate plans"
                )
        return self
