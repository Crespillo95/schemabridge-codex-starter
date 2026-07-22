"""LLM-free guided request construction, validation, summaries, and draft use cases."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

from pydantic import ValidationError

from schemabridge.application.ports.requests import (
    ApprovedRequestContextPort,
    RequestDraftStorePort,
    RequestPlannerPort,
    RequestPlanningAcknowledgement,
    RequestWorkflowError,
    RequestWorkflowErrorCode,
)
from schemabridge.domain.concepts import LogicalFieldRef, LogicalModelRef
from schemabridge.domain.request_context import (
    ValidatedAnalyticalRequest,
    validate_analytical_request,
)
from schemabridge.domain.requests import (
    AnalyticalRequest,
    AnalyticalRequestDraft,
    DateGrain,
    Dimension,
    Filter,
    FilterOperator,
    FilterScalar,
    Metric,
    MetricOperation,
    OrderBy,
    SortDirection,
)
from schemabridge.domain.validation import (
    ValidationFinding,
    ValidationResult,
    ValidationSeverity,
)

_EnumT = TypeVar("_EnumT", bound=StrEnum)


class GuidedRequestCase(StrEnum):
    NORTH_STAR = "north-star"
    NO_JOIN = "no-join"
    RELATIONSHIP_COUNT = "relationship-count"


@dataclass(frozen=True, slots=True)
class GuidedDimensionInput:
    field: str
    grain: str | None = None


@dataclass(frozen=True, slots=True)
class GuidedMetricInput:
    operation: str
    field: str
    alias: str | None = None


@dataclass(frozen=True, slots=True)
class GuidedFilterInput:
    field: str
    operator: str
    value: FilterScalar | tuple[FilterScalar, ...] = None


@dataclass(frozen=True, slots=True)
class GuidedOrderInput:
    field: str
    direction: str = SortDirection.ASC.value


@dataclass(frozen=True, slots=True)
class GuidedRequestInput:
    primary_entity: str
    dimensions: tuple[GuidedDimensionInput, ...]
    metrics: tuple[GuidedMetricInput, ...]
    filters: tuple[GuidedFilterInput, ...] = ()
    order_by: tuple[GuidedOrderInput, ...] = ()
    limit: int = 100


class GuidedRequestValidationError(RuntimeError):
    """Actionable closed validation findings returned before planning."""

    def __init__(self, result: ValidationResult) -> None:
        self.result = result
        super().__init__("; ".join(finding.message for finding in result.findings))


@dataclass(frozen=True, slots=True)
class GuidedFieldOption:
    id: str
    canonical_type: str
    role: str

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "type": self.canonical_type, "role": self.role}


@dataclass(frozen=True, slots=True)
class GuidedRequestOptions:
    context_source: str
    models: tuple[str, ...]
    fields: tuple[GuidedFieldOption, ...]
    metric_operations: tuple[str, ...]
    filter_operators: tuple[str, ...]
    date_grains: tuple[str, ...]
    sort_directions: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "context_source": self.context_source,
            "models": list(self.models),
            "fields": [field.as_dict() for field in self.fields],
            "metric_operations": list(self.metric_operations),
            "filter_operators": list(self.filter_operators),
            "date_grains": list(self.date_grains),
            "sort_directions": list(self.sort_directions),
        }


@dataclass(frozen=True, slots=True)
class GuidedRequestSummary:
    context_source: str
    primary_entity: str
    dimensions: tuple[str, ...]
    metrics: tuple[str, ...]
    filters: tuple[str, ...]
    order_by: tuple[str, ...]
    limit: int
    required_models: tuple[str, ...]
    join_contracts: tuple[str, ...]

    @property
    def requires_join(self) -> bool:
        return bool(self.join_contracts)

    def as_dict(self) -> dict[str, object]:
        return {
            "context_source": self.context_source,
            "primary_entity": self.primary_entity,
            "dimensions": list(self.dimensions),
            "metrics": list(self.metrics),
            "filters": list(self.filters),
            "order_by": list(self.order_by),
            "limit": self.limit,
            "required_models": list(self.required_models),
            "join_contracts": list(self.join_contracts),
            "requires_join": self.requires_join,
        }


@dataclass(frozen=True, slots=True)
class GuidedRequestSubmission:
    validated_request: ValidatedAnalyticalRequest
    summary: GuidedRequestSummary
    planning: RequestPlanningAcknowledgement

    def as_dict(self) -> dict[str, object]:
        return {
            "request": self.validated_request.request.model_dump(mode="json"),
            "validation": {
                "is_valid": True,
                "context_version": self.validated_request.context_version,
            },
            "summary": self.summary.as_dict(),
            "planning_handoff": self.planning.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class ReloadedRequestDraft:
    draft: AnalyticalRequestDraft
    validated_request: ValidatedAnalyticalRequest


@dataclass(frozen=True, slots=True)
class BuildGuidedRequest:
    context: ApprovedRequestContextPort

    def options(self) -> GuidedRequestOptions:
        approved_context = self.context.load()
        return GuidedRequestOptions(
            context_source=approved_context.source,
            models=tuple(model.id.root for model in approved_context.models),
            fields=tuple(
                GuidedFieldOption(
                    id=field.id.root,
                    canonical_type=field.canonical_type.value,
                    role=field.role.value,
                )
                for model in approved_context.models
                for field in model.fields
            ),
            metric_operations=tuple(item.value for item in MetricOperation),
            filter_operators=tuple(item.value for item in FilterOperator),
            date_grains=tuple(item.value for item in DateGrain),
            sort_directions=tuple(item.value for item in SortDirection),
        )

    def execute(self, guided_input: GuidedRequestInput) -> ValidatedAnalyticalRequest:
        approved_context = self.context.load()
        parsing_findings: list[ValidationFinding] = []
        known_models = approved_context.model_index()
        known_fields = approved_context.field_index()

        if guided_input.primary_entity not in known_models:
            parsing_findings.append(
                _error(
                    "unknown_primary_entity",
                    (
                        f"{guided_input.primary_entity} is not approved; choose one of: "
                        f"{', '.join(sorted(known_models))}."
                    ),
                    "primary_entity",
                )
            )
        for path, field in _raw_fields(guided_input):
            if field not in known_fields:
                parsing_findings.append(
                    _error(
                        "unknown_logical_field",
                        (
                            f"{field} is not approved; choose a field exposed by the "
                            "guided logical context."
                        ),
                        *path,
                    )
                )

        dimensions: list[Dimension] = []
        for index, dimension_input in enumerate(guided_input.dimensions):
            grain = _closed_enum(
                DateGrain,
                dimension_input.grain,
                code="unsupported_date_grain",
                label="date grain",
                path=("dimensions", str(index), "grain"),
                findings=parsing_findings,
                optional=True,
            )
            if dimension_input.field in known_fields and (
                dimension_input.grain is None or grain is not None
            ):
                dimensions.append(
                    Dimension(field=LogicalFieldRef(dimension_input.field), grain=grain)
                )

        metrics: list[Metric] = []
        if not guided_input.metrics:
            parsing_findings.append(
                _error("metrics_required", "Choose at least one supported metric.", "metrics")
            )
        for index, metric_input in enumerate(guided_input.metrics):
            operation = _closed_enum(
                MetricOperation,
                metric_input.operation,
                code="unsupported_metric_operation",
                label="metric operation",
                path=("metrics", str(index), "operation"),
                findings=parsing_findings,
            )
            if metric_input.field in known_fields and operation is not None:
                try:
                    metrics.append(
                        Metric(
                            operation=operation,
                            field=LogicalFieldRef(metric_input.field),
                            alias=metric_input.alias,
                        )
                    )
                except ValidationError:
                    parsing_findings.append(
                        _error(
                            "invalid_metric_alias",
                            "Metric aliases must be inert identifiers such as customer_count.",
                            "metrics",
                            str(index),
                            "alias",
                        )
                    )

        filters: list[Filter] = []
        for index, filter_input in enumerate(guided_input.filters):
            operator = _closed_enum(
                FilterOperator,
                filter_input.operator,
                code="unsupported_filter_operator",
                label="filter operator",
                path=("filters", str(index), "operator"),
                findings=parsing_findings,
            )
            if filter_input.field in known_fields and operator is not None:
                try:
                    filters.append(
                        Filter(
                            field=LogicalFieldRef(filter_input.field),
                            operator=operator,
                            value=filter_input.value,
                        )
                    )
                except ValidationError as error:
                    parsing_findings.append(
                        _error(
                            "invalid_filter_value",
                            _first_validation_message(error),
                            "filters",
                            str(index),
                            "value",
                        )
                    )

        order_by: list[OrderBy] = []
        for index, order_input in enumerate(guided_input.order_by):
            direction = _closed_enum(
                SortDirection,
                order_input.direction,
                code="unsupported_sort_direction",
                label="sort direction",
                path=("order_by", str(index), "direction"),
                findings=parsing_findings,
            )
            if order_input.field in known_fields and direction is not None:
                order_by.append(
                    OrderBy(field=LogicalFieldRef(order_input.field), direction=direction)
                )

        if parsing_findings:
            raise GuidedRequestValidationError(ValidationResult(findings=tuple(parsing_findings)))

        try:
            request = AnalyticalRequest(
                primary_entity=LogicalModelRef(guided_input.primary_entity),
                dimensions=tuple(dimensions),
                metrics=tuple(metrics),
                filters=tuple(filters),
                order_by=tuple(order_by),
                limit=guided_input.limit,
            )
        except ValidationError as error:
            raise GuidedRequestValidationError(
                ValidationResult(
                    findings=(
                        _error(
                            "invalid_analytical_request",
                            _first_validation_message(error),
                        ),
                    )
                )
            ) from error
        return self.validate(request)

    def validate(self, request: AnalyticalRequest) -> ValidatedAnalyticalRequest:
        validation = validate_analytical_request(request, self.context.load())
        if validation.validated_request is None:
            raise GuidedRequestValidationError(validation.result)
        return validation.validated_request


@dataclass(frozen=True, slots=True)
class SubmitGuidedRequest:
    planner: RequestPlannerPort

    def execute(self, validated: ValidatedAnalyticalRequest) -> GuidedRequestSubmission:
        request = validated.request
        summary = GuidedRequestSummary(
            context_source=validated.context_source,
            primary_entity=request.primary_entity.root,
            dimensions=tuple(
                f"{item.field.root}" + (f" ({item.grain.value})" if item.grain is not None else "")
                for item in request.dimensions
            ),
            metrics=tuple(
                f"{item.operation.value}({item.field.root})"
                + (f" as {item.alias}" if item.alias is not None else "")
                for item in request.metrics
            ),
            filters=tuple(
                f"{item.field.root} {item.operator.value} {_display_value(item.value)}"
                for item in request.filters
            ),
            order_by=tuple(
                f"{item.field.root} {item.direction.value}" for item in request.order_by
            ),
            limit=request.limit,
            required_models=tuple(model.root for model in validated.required_models),
            join_contracts=validated.join_contract_ids,
        )
        return GuidedRequestSubmission(
            validated_request=validated,
            summary=summary,
            planning=self.planner.accept(validated),
        )


@dataclass(frozen=True, slots=True)
class SaveRequestDraft:
    store: RequestDraftStorePort

    def execute(
        self,
        draft_id: str,
        request: AnalyticalRequest,
    ) -> AnalyticalRequestDraft:
        current = self.store.load(draft_id)
        if current is not None and current.request == request:
            return current
        revision = 1 if current is None else current.revision + 1
        try:
            draft = AnalyticalRequestDraft(id=draft_id, revision=revision, request=request)
        except ValidationError as error:
            raise GuidedRequestValidationError(
                ValidationResult(
                    findings=(
                        _error(
                            "invalid_request_draft_id",
                            "Draft ids must be lowercase inert identifiers such as north-star-demo.",
                            "draft_id",
                        ),
                    )
                )
            ) from error
        self.store.save(
            draft,
            expected_revision=None if current is None else current.revision,
        )
        return draft


@dataclass(frozen=True, slots=True)
class LoadRequestDraft:
    store: RequestDraftStorePort
    builder: BuildGuidedRequest

    def execute(self, draft_id: str) -> ReloadedRequestDraft:
        draft = self.store.load(draft_id)
        if draft is None:
            raise RequestWorkflowError(
                RequestWorkflowErrorCode.DRAFT_NOT_FOUND,
                f"request draft {draft_id!r} was not found",
            )
        return ReloadedRequestDraft(
            draft=draft,
            validated_request=self.builder.validate(draft.request),
        )


def build_demo_guided_input(
    case: GuidedRequestCase,
    *,
    metric_operation: str | None = None,
    grain: str | None = None,
    filter_field: str | None = None,
) -> GuidedRequestInput:
    """Return guided selections, not a prevalidated request or physical plan."""

    if case is GuidedRequestCase.NO_JOIN:
        return GuidedRequestInput(
            primary_entity="Customer",
            dimensions=(GuidedDimensionInput(field="Customer.country_code"),),
            metrics=(
                GuidedMetricInput(
                    operation=metric_operation or MetricOperation.COUNT_DISTINCT.value,
                    field="Customer.customer_key",
                    alias="active_customers",
                ),
            ),
            filters=(
                GuidedFilterInput(
                    field=filter_field or "Customer.customer_status",
                    operator=FilterOperator.EQUALS.value,
                    value="ACTIVE",
                ),
            ),
            order_by=(GuidedOrderInput(field="Customer.country_code"),),
            limit=500,
        )
    if case is GuidedRequestCase.RELATIONSHIP_COUNT:
        metric_operation = metric_operation or MetricOperation.COUNT.value
        metric_field = "AccountHolder.customer_key"
        metric_alias = "secondary_holder_relationships"
    else:
        metric_operation = metric_operation or MetricOperation.COUNT_DISTINCT.value
        metric_field = "Customer.customer_key"
        metric_alias = "secondary_holder_customers"
    return GuidedRequestInput(
        primary_entity="Customer",
        dimensions=(
            GuidedDimensionInput(
                field="Customer.registration_date",
                grain=grain or DateGrain.DAY.value,
            ),
        ),
        metrics=(
            GuidedMetricInput(
                operation=metric_operation,
                field=metric_field,
                alias=metric_alias,
            ),
        ),
        filters=(
            GuidedFilterInput(
                field=filter_field or "AccountHolder.holder_role",
                operator=FilterOperator.EQUALS.value,
                value="SECONDARY",
            ),
        ),
        order_by=(GuidedOrderInput(field="Customer.registration_date"),),
        limit=500,
    )


def _raw_fields(
    guided_input: GuidedRequestInput,
) -> tuple[tuple[tuple[str, ...], str], ...]:
    values: list[tuple[tuple[str, ...], str]] = []
    values.extend(
        (("dimensions", str(index), "field"), item.field)
        for index, item in enumerate(guided_input.dimensions)
    )
    values.extend(
        (("metrics", str(index), "field"), item.field)
        for index, item in enumerate(guided_input.metrics)
    )
    values.extend(
        (("filters", str(index), "field"), item.field)
        for index, item in enumerate(guided_input.filters)
    )
    values.extend(
        (("order_by", str(index), "field"), item.field)
        for index, item in enumerate(guided_input.order_by)
    )
    return tuple(values)


def _closed_enum(
    enum_type: type[_EnumT],
    value: str | None,
    *,
    code: str,
    label: str,
    path: tuple[str, ...],
    findings: list[ValidationFinding],
    optional: bool = False,
) -> _EnumT | None:
    if value is None and optional:
        return None
    if value is None:
        findings.append(_error(code, f"Choose a supported {label}.", *path))
        return None
    try:
        return enum_type(value)
    except ValueError:
        choices = ", ".join(item.value for item in enum_type)
        findings.append(
            _error(code, f"Unsupported {label} {value!r}; choose one of: {choices}.", *path)
        )
        return None


def _first_validation_message(error: ValidationError) -> str:
    first = error.errors(include_url=False)[0]
    return str(first["msg"])


def _display_value(value: object) -> str:
    if isinstance(value, tuple):
        return "[" + ", ".join(repr(item) for item in value) + "]"
    return repr(value)


def _error(code: str, message: str, *path: str) -> ValidationFinding:
    return ValidationFinding(
        code=code,
        severity=ValidationSeverity.ERROR,
        message=message,
        path=tuple(path),
    )
