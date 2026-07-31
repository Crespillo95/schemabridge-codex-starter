"""Pure approval and validation boundary for guided analytical requests."""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from datetime import date, datetime
from enum import StrEnum
from typing import TypeAlias, overload

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.advanced_requests import (
    AdvancedAnalyticalRequest,
    AdvancedField,
    AdvancedMetric,
    AdvancedMetricOperation,
    AnalyticalRequestLike,
    OutputFilter,
    WindowOperation,
    logical_predicate_filters,
    output_predicate_filters,
)
from schemabridge.domain.concepts import (
    CanonicalType,
    LogicalFieldRef,
    LogicalModelRef,
)
from schemabridge.domain.decisions import ApprovalStatus
from schemabridge.domain.joins import Cardinality, FanoutPolicy
from schemabridge.domain.requests import (
    AnalyticalRequest,
    Dimension,
    Filter,
    FilterOperator,
    Metric,
    MetricOperation,
)
from schemabridge.domain.validation import (
    ValidationFinding,
    ValidationResult,
    ValidationSeverity,
)

_CONTEXT_SOURCE = re.compile(r"^[a-z][a-z0-9_-]*:[A-Za-z0-9._/-]+$")
_JOIN_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_NUMERIC_TYPES = frozenset({CanonicalType.INTEGER, CanonicalType.DECIMAL})
_ORDERED_TYPES = _NUMERIC_TYPES | frozenset({CanonicalType.DATE, CanonicalType.TIMESTAMP})


class LogicalFieldRole(StrEnum):
    IDENTIFIER = "identifier"
    ATTRIBUTE = "attribute"
    TEMPORAL = "temporal"
    MEASURE = "measure"


class ApprovedRequestField(FrozenDomainModel):
    id: LogicalFieldRef
    canonical_type: CanonicalType
    role: LogicalFieldRole
    definition: str = Field(min_length=1)
    allowed_values: tuple[str, ...] = Field(default=(), max_length=64)
    status: ApprovalStatus
    version: int = Field(ge=1)

    @field_validator("definition")
    @classmethod
    def definition_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("approved request field definition must not be blank")
        return value

    @field_validator("allowed_values")
    @classmethod
    def allowed_values_must_be_unique_and_nonblank(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(not value.strip() for value in values):
            raise ValueError("approved field values must be unique and nonblank")
        return values

    @model_validator(mode="after")
    def field_must_be_approved_and_typed_consistently(self) -> ApprovedRequestField:
        if self.status is not ApprovalStatus.APPROVED:
            raise ValueError("guided request context may contain only approved fields")
        if self.role is LogicalFieldRole.TEMPORAL and self.canonical_type not in {
            CanonicalType.DATE,
            CanonicalType.TIMESTAMP,
        }:
            raise ValueError("temporal fields require date or timestamp canonical type")
        if self.role is LogicalFieldRole.MEASURE and self.canonical_type not in _NUMERIC_TYPES:
            raise ValueError("measure fields require integer or decimal canonical type")
        if self.allowed_values and self.canonical_type is not CanonicalType.STRING:
            raise ValueError("closed approved values require a canonical string field")
        return self


class ApprovedRequestModel(FrozenDomainModel):
    id: LogicalModelRef
    description: str = Field(min_length=1)
    fields: tuple[ApprovedRequestField, ...] = Field(min_length=1)
    status: ApprovalStatus
    version: int = Field(ge=1)

    @model_validator(mode="after")
    def model_must_be_approved_and_own_its_fields(self) -> ApprovedRequestModel:
        if self.status is not ApprovalStatus.APPROVED:
            raise ValueError("guided request context may contain only approved models")
        prefix = f"{self.id.root}."
        field_ids = [field.id.root for field in self.fields]
        if any(not field_id.startswith(prefix) for field_id in field_ids):
            raise ValueError("approved request fields must belong to their logical model")
        if len(field_ids) != len(set(field_ids)):
            raise ValueError("approved request model fields must be unique")
        return self


class ApprovedLogicalJoin(FrozenDomainModel):
    id: str = Field(min_length=1)
    left_model: LogicalModelRef
    right_model: LogicalModelRef
    cardinality: Cardinality
    fanout_policy: FanoutPolicy
    status: ApprovalStatus
    version: int = Field(ge=1)
    approval_decision_id: str = Field(min_length=1)

    @field_validator("id")
    @classmethod
    def id_must_be_inert(cls, value: str) -> str:
        if _JOIN_ID.fullmatch(value) is None:
            raise ValueError("logical join id must be a lowercase inert identifier")
        return value

    @model_validator(mode="after")
    def join_must_be_approved_and_non_reflexive(self) -> ApprovedLogicalJoin:
        if self.status is not ApprovalStatus.APPROVED:
            raise ValueError("guided request context may contain only approved joins")
        if self.left_model == self.right_model:
            raise ValueError("guided request context does not support self joins")
        if not self.approval_decision_id.strip():
            raise ValueError("approved logical join requires a decision reference")
        return self


class ApprovedLogicalContext(FrozenDomainModel):
    version: int = Field(ge=1)
    source: str = Field(min_length=1)
    models: tuple[ApprovedRequestModel, ...] = Field(min_length=1)
    joins: tuple[ApprovedLogicalJoin, ...] = ()

    @field_validator("source")
    @classmethod
    def source_must_be_explicit(cls, value: str) -> str:
        if _CONTEXT_SOURCE.fullmatch(value) is None:
            raise ValueError("logical context source must be an explicit kind:location label")
        return value

    @model_validator(mode="after")
    def identities_and_join_endpoints_must_resolve(self) -> ApprovedLogicalContext:
        model_ids = [model.id.root for model in self.models]
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("approved logical context models must be unique")
        field_ids = [field.id.root for model in self.models for field in model.fields]
        if len(field_ids) != len(set(field_ids)):
            raise ValueError("approved logical context fields must be unique")
        join_ids = [join.id for join in self.joins]
        if len(join_ids) != len(set(join_ids)):
            raise ValueError("approved logical context joins must be unique")
        known_models = set(model_ids)
        if any(
            join.left_model.root not in known_models or join.right_model.root not in known_models
            for join in self.joins
        ):
            raise ValueError("approved logical join references an unknown model")
        return self

    def field_index(self) -> dict[str, ApprovedRequestField]:
        return {field.id.root: field for model in self.models for field in model.fields}

    def model_index(self) -> dict[str, ApprovedRequestModel]:
        return {model.id.root: model for model in self.models}


class ValidatedAnalyticalRequest(FrozenDomainModel):
    """Request proven compatible with one exact approved logical context."""

    request: AnalyticalRequest
    context_source: str = Field(min_length=1)
    context_version: int = Field(ge=1)
    context_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_models: tuple[LogicalModelRef, ...] = Field(min_length=1, max_length=3)
    join_contract_ids: tuple[str, ...] = Field(default=(), max_length=2)

    @model_validator(mode="after")
    def resolution_must_start_at_primary_entity(self) -> ValidatedAnalyticalRequest:
        if self.required_models[0] != self.request.primary_entity:
            raise ValueError("validated request models must start with the primary entity")
        model_ids = [model.root for model in self.required_models]
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("validated request models must be unique")
        if len(self.join_contract_ids) != len(set(self.join_contract_ids)):
            raise ValueError("validated request join contracts must be unique")
        return self


class ValidatedAdvancedAnalyticalRequest(FrozenDomainModel):
    """Version-2 request proven compatible with one exact approved context."""

    request: AdvancedAnalyticalRequest
    context_source: str = Field(min_length=1)
    context_version: int = Field(ge=1)
    context_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_models: tuple[LogicalModelRef, ...] = Field(min_length=1, max_length=3)
    join_contract_ids: tuple[str, ...] = Field(default=(), max_length=2)

    @model_validator(mode="after")
    def resolution_must_start_at_primary_entity(
        self,
    ) -> ValidatedAdvancedAnalyticalRequest:
        if self.required_models[0] != self.request.primary_entity:
            raise ValueError("validated request models must start with the primary entity")
        model_ids = [model.root for model in self.required_models]
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("validated request models must be unique")
        if len(self.join_contract_ids) != len(set(self.join_contract_ids)):
            raise ValueError("validated request join contracts must be unique")
        return self


ValidatedRequestLike: TypeAlias = ValidatedAdvancedAnalyticalRequest | ValidatedAnalyticalRequest


class AnalyticalRequestValidation(FrozenDomainModel):
    result: ValidationResult
    validated_request: ValidatedAnalyticalRequest | None = None

    @model_validator(mode="after")
    def outcome_must_match_findings(self) -> AnalyticalRequestValidation:
        if self.result.is_valid != (self.validated_request is not None):
            raise ValueError("request validation outcome does not match its findings")
        return self


class AdvancedAnalyticalRequestValidation(FrozenDomainModel):
    result: ValidationResult
    validated_request: ValidatedAdvancedAnalyticalRequest | None = None

    @model_validator(mode="after")
    def outcome_must_match_findings(self) -> AdvancedAnalyticalRequestValidation:
        if self.result.is_valid != (self.validated_request is not None):
            raise ValueError("advanced request validation outcome does not match its findings")
        return self


@overload
def validate_analytical_request(
    request: AnalyticalRequest,
    context: ApprovedLogicalContext,
) -> AnalyticalRequestValidation: ...


@overload
def validate_analytical_request(
    request: AdvancedAnalyticalRequest,
    context: ApprovedLogicalContext,
) -> AdvancedAnalyticalRequestValidation: ...


def validate_analytical_request(
    request: AnalyticalRequestLike,
    context: ApprovedLogicalContext,
) -> AnalyticalRequestValidation | AdvancedAnalyticalRequestValidation:
    """Validate logical identifiers, operations, types, joins, and ambiguity without I/O."""

    findings: list[ValidationFinding] = []
    model_index = context.model_index()
    field_index = context.field_index()

    if request.primary_entity.root not in model_index:
        findings.append(
            _error(
                "unknown_primary_entity",
                f"{request.primary_entity.root} is not an approved logical model.",
                "primary_entity",
            )
        )

    referenced_fields = _referenced_fields(request)
    for path, field_ref in referenced_fields:
        if field_ref.root not in field_index:
            findings.append(
                _error(
                    "unknown_logical_field",
                    f"{field_ref.root} is not an approved logical field; choose a listed field.",
                    *path,
                )
            )

    for index, dimension in enumerate(_selected_request_fields(request)):
        field = field_index.get(dimension.field.root)
        if field is None:
            continue
        if not isinstance(request, AdvancedAnalyticalRequest) and (
            field.role is LogicalFieldRole.MEASURE
        ):
            findings.append(
                _error(
                    "incompatible_dimension_role",
                    f"{field.id.root} is a measure and cannot be used as a guided dimension.",
                    "dimensions",
                    str(index),
                )
            )
        if dimension.grain is not None and (
            field.role is not LogicalFieldRole.TEMPORAL
            or field.canonical_type not in {CanonicalType.DATE, CanonicalType.TIMESTAMP}
        ):
            findings.append(
                _error(
                    "grain_not_supported_for_field",
                    f"{dimension.grain.value} grain requires an approved date or timestamp field.",
                    "dimensions",
                    str(index),
                    "grain",
                )
            )

    for index, metric in enumerate(_request_metrics(request)):
        if metric.field is None:
            continue
        field = field_index.get(metric.field.root)
        if field is None:
            continue
        if not _metric_is_compatible(metric.operation, field):
            findings.append(
                _error(
                    "incompatible_metric_operation",
                    (
                        f"{metric.operation.value} is not supported for {field.id.root} "
                        f"({field.role.value}/{field.canonical_type.value}); use count_distinct "
                        "for identifiers or choose an approved numeric measure."
                    ),
                    "metrics",
                    str(index),
                    "operation",
                )
            )

    for path, request_filter in _request_filters(request):
        field = field_index.get(request_filter.field.root)
        if field is None:
            continue
        if (
            request_filter.operator
            in {
                FilterOperator.GREATER_THAN,
                FilterOperator.GREATER_THAN_OR_EQUAL,
                FilterOperator.LESS_THAN,
                FilterOperator.LESS_THAN_OR_EQUAL,
            }
            and field.canonical_type not in _ORDERED_TYPES
        ):
            findings.append(
                _error(
                    "incompatible_filter_operator",
                    (
                        f"{request_filter.operator.value} requires an approved numeric or "
                        f"temporal field; {field.id.root} is {field.canonical_type.value}."
                    ),
                    *path,
                    "operator",
                )
            )
        if not _filter_values_match_type(request_filter.value, field.canonical_type):
            findings.append(
                _error(
                    "incompatible_filter_value",
                    (
                        f"Filter values for {field.id.root} must match its "
                        f"{field.canonical_type.value} canonical type."
                    ),
                    *path,
                    "value",
                )
            )
        values = (
            request_filter.value
            if isinstance(request_filter.value, tuple)
            else (request_filter.value,)
        )
        if (
            field.allowed_values
            and request_filter.operator not in {FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL}
            and any(value not in field.allowed_values for value in values)
        ):
            findings.append(
                _error(
                    "unapproved_filter_value",
                    (
                        f"Filter values for {field.id.root} must be chosen from its approved "
                        f"closed set: {', '.join(field.allowed_values)}."
                    ),
                    *path,
                    "value",
                )
            )

    if isinstance(request, AdvancedAnalyticalRequest):
        _validate_advanced_request_stages(request, field_index, findings)

    selected_models = _selected_models(request)
    if len(selected_models) > 3:
        findings.append(
            _error(
                "too_many_logical_models",
                "Guided requests may require at most three approved logical models.",
            )
        )

    join_ids: list[str] = []
    resolved_models = list(selected_models[:1])
    if request.primary_entity.root in model_index and not any(
        finding.code == "unknown_logical_field" for finding in findings
    ):
        for target in selected_models[1:]:
            paths = _shortest_paths(request.primary_entity.root, target, context.joins)
            if not paths:
                findings.append(
                    _error(
                        "missing_approved_join_path",
                        (
                            f"No approved logical join path connects "
                            f"{request.primary_entity.root} to {target}."
                        ),
                    )
                )
                continue
            if len(paths) > 1:
                choices = ", ".join(" -> ".join(join.id for join in path) for path in paths)
                findings.append(
                    _error(
                        "ambiguous_approved_join_path",
                        f"Multiple equally short approved join paths reach {target}: {choices}.",
                    )
                )
                continue
            for join in paths[0]:
                if join.id not in join_ids:
                    join_ids.append(join.id)
                for model in (join.left_model.root, join.right_model.root):
                    if model not in resolved_models:
                        resolved_models.append(model)

    joins_by_id = {join.id: join for join in context.joins}
    for join_id in join_ids:
        join = joins_by_id[join_id]
        left_index = resolved_models.index(join.left_model.root)
        right_index = resolved_models.index(join.right_model.root)
        forward = left_index < right_index
        oriented_cardinality = (
            join.cardinality if forward else _reverse_cardinality(join.cardinality)
        )
        if (
            oriented_cardinality is Cardinality.ONE_TO_MANY
            and join.fanout_policy is FanoutPolicy.REQUIRE_DISTINCT_FOR_LEFT_ENTITY_METRICS
        ):
            joined_index = right_index if forward else left_index
            existing_models = set(resolved_models[:joined_index])
            planner_checked_metrics = []
            for metric in _request_metrics(request):
                if metric.field is None:
                    continue
                is_plain_count = (
                    metric.operation is AdvancedMetricOperation.COUNT
                    if isinstance(metric, AdvancedMetric)
                    else metric.operation is MetricOperation.COUNT
                )
                if _model_for_field(metric.field) in existing_models and is_plain_count:
                    planner_checked_metrics.append(metric)
            if planner_checked_metrics:
                findings.append(
                    _warning(
                        "fanout_mitigation_requires_planner_check",
                        (
                            f"{join.id} is {oriented_cardinality.value} in the selected path; "
                            "the governed planner will apply count_distinct only to the exact "
                            "approved one-side key and otherwise fail closed."
                        ),
                        "metrics",
                    )
                )

    if len(resolved_models) > 3:
        findings.append(
            _error(
                "approved_join_path_too_long",
                "The shortest approved join path exceeds the three-model MVP limit.",
            )
        )

    result = ValidationResult(findings=tuple(findings))
    if not result.is_valid:
        if isinstance(request, AdvancedAnalyticalRequest):
            return AdvancedAnalyticalRequestValidation(result=result)
        return AnalyticalRequestValidation(result=result)
    common = {
        "context_source": context.source,
        "context_version": context.version,
        "context_fingerprint": approved_logical_context_fingerprint(context),
        "required_models": tuple(LogicalModelRef(model) for model in resolved_models),
        "join_contract_ids": tuple(join_ids),
    }
    if isinstance(request, AdvancedAnalyticalRequest):
        advanced_validated = ValidatedAdvancedAnalyticalRequest(
            request=request,
            **common,
        )
        return AdvancedAnalyticalRequestValidation(
            result=result,
            validated_request=advanced_validated,
        )
    validated = ValidatedAnalyticalRequest(request=request, **common)
    return AnalyticalRequestValidation(result=result, validated_request=validated)


def approved_logical_context_fingerprint(context: ApprovedLogicalContext) -> str:
    """Fingerprint the complete approved logical payload, not just its mutable label/version."""

    encoded = json.dumps(
        context.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def validated_analytical_request_fingerprint(request: ValidatedRequestLike) -> str:
    """Stable identity shared by guided and confirmed language input modes."""

    encoded = json.dumps(
        request.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _referenced_fields(
    request: AnalyticalRequestLike,
) -> tuple[tuple[tuple[str, ...], LogicalFieldRef], ...]:
    values: list[tuple[tuple[str, ...], LogicalFieldRef]] = []
    selected_path = "fields" if isinstance(request, AdvancedAnalyticalRequest) else "dimensions"
    values.extend(
        ((selected_path, str(index), "field"), item.field)
        for index, item in enumerate(_selected_request_fields(request))
    )
    values.extend(
        (("metrics", str(index), "field"), item.field)
        for index, item in enumerate(_request_metrics(request))
        if item.field is not None
    )
    values.extend(((*path, "field"), item.field) for path, item in _request_filters(request))
    if not isinstance(request, AdvancedAnalyticalRequest):
        values.extend(
            (("order_by", str(index), "field"), item.field)
            for index, item in enumerate(request.order_by)
        )
    return tuple(values)


def _selected_models(request: AnalyticalRequestLike) -> tuple[str, ...]:
    selected = [request.primary_entity.root]
    for _, field in _referenced_fields(request):
        model = _model_for_field(field)
        if model not in selected:
            selected.append(model)
    return tuple(selected)


def _request_filters(
    request: AnalyticalRequestLike,
) -> tuple[tuple[tuple[str, ...], Filter], ...]:
    values: list[tuple[tuple[str, ...], Filter]] = []
    if not isinstance(request, AdvancedAnalyticalRequest):
        values.extend((("filters", str(index)), item) for index, item in enumerate(request.filters))
    if isinstance(request, AdvancedAnalyticalRequest):
        if request.where is not None:
            values.extend(
                (("where", "leaves", str(index)), item)
                for index, item in enumerate(logical_predicate_filters(request.where))
            )
        for metric_index, metric in enumerate(request.metrics):
            if metric.condition is not None:
                values.extend(
                    (
                        (
                            "metrics",
                            str(metric_index),
                            "condition",
                            "leaves",
                            str(filter_index),
                        ),
                        item,
                    )
                    for filter_index, item in enumerate(logical_predicate_filters(metric.condition))
                )
    return tuple(values)


def _selected_request_fields(
    request: AnalyticalRequestLike,
) -> tuple[AdvancedField | Dimension, ...]:
    if isinstance(request, AdvancedAnalyticalRequest):
        return request.fields
    return request.dimensions


def _request_metrics(
    request: AnalyticalRequestLike,
) -> tuple[AdvancedMetric | Metric, ...]:
    if isinstance(request, AdvancedAnalyticalRequest):
        return request.metrics
    return request.metrics


def _validate_advanced_request_stages(
    request: AdvancedAnalyticalRequest,
    field_index: dict[str, ApprovedRequestField],
    findings: list[ValidationFinding],
) -> None:
    field_types: dict[str, CanonicalType] = {}
    field_roles: dict[str, LogicalFieldRole] = {}
    for index, selected_field in enumerate(request.fields):
        field = field_index.get(selected_field.field.root)
        if field is None:
            continue
        alias = selected_field.alias or selected_field.field.root.rsplit(".", 1)[-1]
        if selected_field.buckets:
            if field.canonical_type not in _NUMERIC_TYPES:
                findings.append(
                    _error(
                        "incompatible_bucket_dimension",
                        "Numeric CASE buckets require an approved integer or decimal field.",
                        "dimensions",
                        str(index),
                        "buckets",
                    )
                )
            field_types[alias] = CanonicalType.STRING
        elif selected_field.grain is not None:
            field_types[alias] = CanonicalType.DATE
        else:
            field_types[alias] = field.canonical_type
        field_roles[alias] = field.role

    metric_types: dict[str, CanonicalType] = {}
    for metric in request.metrics:
        field = field_index.get(metric.field.root) if metric.field is not None else None
        if metric.operation is not AdvancedMetricOperation.COUNT_ROWS and field is None:
            continue
        alias = metric.alias or (
            metric.operation.value
            if metric.field is None
            else f"{metric.operation.value}_{metric.field.root.rsplit('.', 1)[-1]}"
        )
        metric_types[alias] = (
            CanonicalType.INTEGER
            if metric.operation
            in {
                AdvancedMetricOperation.COUNT_ROWS,
                AdvancedMetricOperation.COUNT,
                AdvancedMetricOperation.COUNT_DISTINCT,
            }
            else CanonicalType.DECIMAL
            if metric.operation in {AdvancedMetricOperation.SUM, AdvancedMetricOperation.AVG}
            else field.canonical_type
            if field is not None
            else CanonicalType.INTEGER
        )

    selected_aliases = set(field_types)
    base_types = {**field_types, **metric_types}
    window_types: dict[str, CanonicalType] = {}
    total_order_operations = {
        WindowOperation.ROW_NUMBER,
        WindowOperation.NTILE,
        WindowOperation.RUNNING_SUM,
        WindowOperation.RUNNING_AVG,
        WindowOperation.MOVING_SUM,
        WindowOperation.MOVING_AVG,
        WindowOperation.LAG,
        WindowOperation.LEAD,
        WindowOperation.DELTA_FROM_PREVIOUS,
        WindowOperation.PERCENT_CHANGE_FROM_PREVIOUS,
    }
    # RANK and DENSE_RANK intentionally preserve peers. Requiring every
    # non-partitioned grouping field here would make equal metric values
    # distinct and silently collapse both operations into ROW_NUMBER
    # semantics. Their non-empty peer order remains enforced by
    # WindowCalculation itself; final display ordering is a separate stage.
    for index, window in enumerate(request.windows):
        if any(alias not in selected_aliases for alias in window.partition_by):
            findings.append(
                _error(
                    "window_partition_requires_dimension",
                    "Window partitions may reference only selected dimensions.",
                    "windows",
                    str(index),
                    "partition_by",
                )
            )
        if window.operation in total_order_operations:
            observed = {item.alias for item in window.order_by}
            required_ties = set(request.group_by) - set(window.partition_by)
            identifier_ties = {
                alias for alias, role in field_roles.items() if role is LogicalFieldRole.IDENTIFIER
            }
            deterministic = (
                required_ties <= observed
                if request.metrics
                else bool(identifier_ties.intersection(observed))
            )
            if not deterministic:
                findings.append(
                    _error(
                        "window_order_not_deterministic",
                        (
                            "Order-sensitive windows require all non-partitioned grouping "
                            "fields or an approved row identifier as a deterministic tie-breaker."
                        ),
                        "windows",
                        str(index),
                        "order_by",
                    )
                )
        if window.operation in {
            WindowOperation.ROW_NUMBER,
            WindowOperation.RANK,
            WindowOperation.DENSE_RANK,
            WindowOperation.NTILE,
        }:
            window_types[window.alias] = CanonicalType.INTEGER
        elif window.operation is WindowOperation.LAG or window.operation is WindowOperation.LEAD:
            window_types[window.alias] = base_types.get(
                window.source or "",
                CanonicalType.STRING,
            )
        else:
            window_types[window.alias] = CanonicalType.DECIMAL
        if window.source is not None and window.operation not in {
            WindowOperation.LAG,
            WindowOperation.LEAD,
        }:
            source_type = base_types.get(window.source)
            if source_type not in _NUMERIC_TYPES:
                findings.append(
                    _error(
                        "window_source_not_numeric",
                        f"{window.operation.value} requires a numeric selected output.",
                        "windows",
                        str(index),
                        "source",
                    )
                )

    if request.having is not None:
        for index, output_filter in enumerate(output_predicate_filters(request.having)):
            _validate_output_filter_type(
                output_filter,
                metric_types,
                findings,
                ("having", "leaves", str(index)),
            )
    if request.post_filter is not None:
        output_types = {**base_types, **window_types}
        for index, output_filter in enumerate(output_predicate_filters(request.post_filter)):
            _validate_output_filter_type(
                output_filter,
                output_types,
                findings,
                ("post_filter", "leaves", str(index)),
            )


def _validate_output_filter_type(
    request_filter: OutputFilter,
    output_types: dict[str, CanonicalType],
    findings: list[ValidationFinding],
    path: tuple[str, ...],
) -> None:
    alias = request_filter.alias
    operator = request_filter.operator
    value = request_filter.value
    compare_to_alias = request_filter.compare_to_alias
    canonical_type = output_types.get(alias)
    if canonical_type is None:
        return
    if (
        operator
        in {
            FilterOperator.GREATER_THAN,
            FilterOperator.GREATER_THAN_OR_EQUAL,
            FilterOperator.LESS_THAN,
            FilterOperator.LESS_THAN_OR_EQUAL,
        }
        and canonical_type not in _ORDERED_TYPES
    ):
        findings.append(
            _error(
                "incompatible_output_filter_operator",
                f"{operator.value} is not compatible with output {alias}.",
                *path,
                "operator",
            )
        )
    if compare_to_alias is not None:
        if output_types.get(compare_to_alias) != canonical_type:
            findings.append(
                _error(
                    "incompatible_output_comparison",
                    "Compared output aliases must have the same canonical type.",
                    *path,
                    "compare_to_alias",
                )
            )
    elif not _filter_values_match_type(value, canonical_type):
        findings.append(
            _error(
                "incompatible_output_filter_value",
                f"Output filter values must match {canonical_type.value}.",
                *path,
                "value",
            )
        )


def _model_for_field(field: LogicalFieldRef) -> str:
    return field.root.split(".", 1)[0]


def _reverse_cardinality(cardinality: Cardinality) -> Cardinality:
    if cardinality is Cardinality.ONE_TO_MANY:
        return Cardinality.MANY_TO_ONE
    if cardinality is Cardinality.MANY_TO_ONE:
        return Cardinality.ONE_TO_MANY
    return cardinality


def _metric_is_compatible(
    operation: MetricOperation | AdvancedMetricOperation,
    field: ApprovedRequestField,
) -> bool:
    if operation in {
        MetricOperation.COUNT,
        MetricOperation.COUNT_DISTINCT,
        AdvancedMetricOperation.COUNT,
        AdvancedMetricOperation.COUNT_DISTINCT,
    }:
        return True
    if operation in {
        MetricOperation.SUM,
        MetricOperation.AVG,
        AdvancedMetricOperation.SUM,
        AdvancedMetricOperation.AVG,
    }:
        return field.role is LogicalFieldRole.MEASURE and field.canonical_type in _NUMERIC_TYPES
    return field.canonical_type in _ORDERED_TYPES and field.role in {
        LogicalFieldRole.MEASURE,
        LogicalFieldRole.TEMPORAL,
    }


def _filter_values_match_type(value: object, canonical_type: CanonicalType) -> bool:
    values = value if isinstance(value, tuple) else (value,)
    if all(item is None for item in values):
        return True

    def matches(item: object) -> bool:
        if canonical_type is CanonicalType.STRING:
            return isinstance(item, str)
        if canonical_type is CanonicalType.INTEGER:
            return isinstance(item, int) and not isinstance(item, bool)
        if canonical_type is CanonicalType.DECIMAL:
            return isinstance(item, (int, float)) and not isinstance(item, bool)
        if canonical_type is CanonicalType.BOOLEAN:
            return isinstance(item, bool)
        if canonical_type is CanonicalType.DATE:
            return type(item) is date
        return isinstance(item, datetime)

    return all(item is not None and matches(item) for item in values)


def _shortest_paths(
    source: str,
    target: str,
    joins: tuple[ApprovedLogicalJoin, ...],
) -> tuple[tuple[ApprovedLogicalJoin, ...], ...]:
    if source == target:
        return ((),)
    adjacency: dict[str, list[tuple[str, ApprovedLogicalJoin]]] = {}
    for join in joins:
        adjacency.setdefault(join.left_model.root, []).append((join.right_model.root, join))
        adjacency.setdefault(join.right_model.root, []).append((join.left_model.root, join))
    queue: deque[tuple[str, tuple[str, ...], tuple[ApprovedLogicalJoin, ...]]] = deque(
        [(source, (source,), ())]
    )
    found: list[tuple[ApprovedLogicalJoin, ...]] = []
    shortest_length: int | None = None
    while queue:
        model, visited, path = queue.popleft()
        if shortest_length is not None and len(path) >= shortest_length:
            continue
        for neighbor, join in sorted(adjacency.get(model, ()), key=lambda item: item[1].id):
            if neighbor in visited:
                continue
            next_path = (*path, join)
            if neighbor == target:
                shortest_length = len(next_path)
                found.append(next_path)
            else:
                queue.append((neighbor, (*visited, neighbor), next_path))
    return tuple(path for path in found if len(path) == shortest_length)


def _error(code: str, message: str, *path: str) -> ValidationFinding:
    return ValidationFinding(
        code=code,
        severity=ValidationSeverity.ERROR,
        message=message,
        path=tuple(path),
    )


def _warning(code: str, message: str, *path: str) -> ValidationFinding:
    return ValidationFinding(
        code=code,
        severity=ValidationSeverity.WARNING,
        message=message,
        path=tuple(path),
    )
