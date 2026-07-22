"""Pure approval and validation boundary for guided analytical requests."""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from datetime import date, datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.concepts import (
    CanonicalType,
    LogicalFieldRef,
    LogicalModelRef,
)
from schemabridge.domain.decisions import ApprovalStatus
from schemabridge.domain.joins import Cardinality, FanoutPolicy
from schemabridge.domain.requests import (
    AnalyticalRequest,
    FilterOperator,
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
    status: ApprovalStatus
    version: int = Field(ge=1)

    @field_validator("definition")
    @classmethod
    def definition_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("approved request field definition must not be blank")
        return value

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


class AnalyticalRequestValidation(FrozenDomainModel):
    result: ValidationResult
    validated_request: ValidatedAnalyticalRequest | None = None

    @model_validator(mode="after")
    def outcome_must_match_findings(self) -> AnalyticalRequestValidation:
        if self.result.is_valid != (self.validated_request is not None):
            raise ValueError("request validation outcome does not match its findings")
        return self


def validate_analytical_request(
    request: AnalyticalRequest,
    context: ApprovedLogicalContext,
) -> AnalyticalRequestValidation:
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

    for index, dimension in enumerate(request.dimensions):
        field = field_index.get(dimension.field.root)
        if field is None:
            continue
        if field.role is LogicalFieldRole.MEASURE:
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

    for index, metric in enumerate(request.metrics):
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

    for index, request_filter in enumerate(request.filters):
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
                    "filters",
                    str(index),
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
                    "filters",
                    str(index),
                    "value",
                )
            )

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
        if join.fanout_policy is FanoutPolicy.REQUIRE_DISTINCT_FOR_LEFT_ENTITY_METRICS:
            planner_mitigated_metrics = [
                metric
                for metric in request.metrics
                if _model_for_field(metric.field) == join.left_model.root
                and metric.operation is MetricOperation.COUNT
            ]
            if planner_mitigated_metrics:
                findings.append(
                    _warning(
                        "fanout_mitigation_will_apply",
                        (
                            f"{join.id} is {join.cardinality.value}; the governed planner will "
                            f"replace count with count_distinct for {join.left_model.root}."
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
        return AnalyticalRequestValidation(result=result)
    validated = ValidatedAnalyticalRequest(
        request=request,
        context_source=context.source,
        context_version=context.version,
        context_fingerprint=approved_logical_context_fingerprint(context),
        required_models=tuple(LogicalModelRef(model) for model in resolved_models),
        join_contract_ids=tuple(join_ids),
    )
    return AnalyticalRequestValidation(result=result, validated_request=validated)


def approved_logical_context_fingerprint(context: ApprovedLogicalContext) -> str:
    """Fingerprint the complete approved logical payload, not just its mutable label/version."""

    encoded = json.dumps(
        context.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def validated_analytical_request_fingerprint(request: ValidatedAnalyticalRequest) -> str:
    """Stable identity shared by guided and confirmed language input modes."""

    encoded = json.dumps(
        request.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _referenced_fields(
    request: AnalyticalRequest,
) -> tuple[tuple[tuple[str, ...], LogicalFieldRef], ...]:
    values: list[tuple[tuple[str, ...], LogicalFieldRef]] = []
    values.extend(
        (("dimensions", str(index), "field"), item.field)
        for index, item in enumerate(request.dimensions)
    )
    values.extend(
        (("metrics", str(index), "field"), item.field) for index, item in enumerate(request.metrics)
    )
    values.extend(
        (("filters", str(index), "field"), item.field) for index, item in enumerate(request.filters)
    )
    values.extend(
        (("order_by", str(index), "field"), item.field)
        for index, item in enumerate(request.order_by)
    )
    return tuple(values)


def _selected_models(request: AnalyticalRequest) -> tuple[str, ...]:
    selected = [request.primary_entity.root]
    for _, field in _referenced_fields(request):
        model = _model_for_field(field)
        if model not in selected:
            selected.append(model)
    return tuple(selected)


def _model_for_field(field: LogicalFieldRef) -> str:
    return field.root.split(".", 1)[0]


def _metric_is_compatible(
    operation: MetricOperation,
    field: ApprovedRequestField,
) -> bool:
    if operation in {MetricOperation.COUNT, MetricOperation.COUNT_DISTINCT}:
        return True
    if operation in {MetricOperation.SUM, MetricOperation.AVG}:
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
