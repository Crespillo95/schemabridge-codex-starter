"""Pure governed resolution from logical analytical intent to the restricted query IR."""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias, overload

from pydantic import Field, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.advanced_plans import (
    AdvancedAggregateExpression,
    AdvancedQueryPlan,
    AdvancedSelectItem,
    AdvancedValueExpression,
    AggregateBooleanPredicate,
    AggregateFilterPredicate,
    BucketExpression,
    NumericBucketPlan,
    OutputBooleanPredicatePlan,
    OutputFilterPredicate,
    OutputOrderItem,
    PhysicalBooleanPredicate,
    RestrictedQueryPlan,
    WindowExpression,
    WindowSelectItem,
)
from schemabridge.domain.advanced_requests import (
    AdvancedAnalyticalRequest,
    AdvancedMetricOperation,
    BooleanOperator,
    LogicalBooleanPredicate,
    OutputBooleanPredicate,
)
from schemabridge.domain.concepts import CanonicalType, LogicalFieldRef
from schemabridge.domain.connectors import GovernedExecutionTarget
from schemabridge.domain.decisions import ApprovalStatus
from schemabridge.domain.fields import PhysicalDatasetRef, PhysicalFieldRef
from schemabridge.domain.joins import (
    Cardinality,
    FanoutPolicy,
    JoinContract,
    JoinType,
    NormalizedJoinKey,
)
from schemabridge.domain.mappings import ColumnMapping
from schemabridge.domain.plans import (
    AggregateExpression,
    AllowedAsset,
    ApprovedJoin,
    ColumnExpression,
    DatasetScan,
    DateGrainExpression,
    FilterPredicate,
    JoinPredicate,
    MappedExpression,
    OrderItem,
    OutputAlias,
    ParameterValue,
    QueryPlan,
    QueryPolicy,
    QueryValueExpression,
    RelationAlias,
    SelectItem,
)
from schemabridge.domain.request_context import (
    ApprovedLogicalContext,
    ValidatedAdvancedAnalyticalRequest,
    ValidatedAnalyticalRequest,
    ValidatedRequestLike,
    approved_logical_context_fingerprint,
    validate_analytical_request,
)
from schemabridge.domain.requests import (
    AnalyticalRequest,
    Filter,
    FilterOperator,
    MetricOperation,
)
from schemabridge.domain.semantic_registry import (
    GovernedFieldMapping,
    GovernedSemanticRegistrySnapshot,
    PhysicalValueType,
    SemanticPlanningContext,
    governed_semantic_registry_fingerprint,
)
from schemabridge.domain.semantic_registry import (
    GovernedMappingSet as GovernedMappingSet,
)
from schemabridge.domain.transformations import TransformationPlan

MAX_REJECTED_SOURCE_TOTAL = 2_147_483_647

_DUPLICATION_INVARIANT_OPERATIONS = frozenset(
    {MetricOperation.COUNT_DISTINCT, MetricOperation.MIN, MetricOperation.MAX}
)


class ResolutionErrorCode(StrEnum):
    STALE_REGISTRY = "stale_registry"
    STALE_LOGICAL_CONTEXT = "stale_logical_context"
    MISSING_MAPPING = "missing_approved_mapping"
    UNAPPROVED_MAPPING = "unapproved_mapping"
    STALE_MAPPING = "stale_mapping"
    AMBIGUOUS_MAPPING = "ambiguous_mapping"
    DISCONNECTED_MAPPING = "disconnected_mapping"
    CONTRACT_MAPPING_MISMATCH = "contract_mapping_mismatch"
    INCOMPATIBLE_MAPPING_TYPE = "incompatible_mapping_type"
    MISSING_JOIN = "missing_approved_join"
    UNAPPROVED_JOIN = "unapproved_join"
    STALE_JOIN = "stale_join"
    AMBIGUOUS_JOIN = "ambiguous_approved_join"
    TOO_MANY_TABLES = "too_many_tables"
    UNSUPPORTED_FANOUT = "unsupported_fanout_mitigation"
    UNSUPPORTED_REQUEST = "unsupported_resolved_request"


class SemanticResolutionError(RuntimeError):
    """A governed request cannot resolve without guessing or stale context."""

    def __init__(self, code: ResolutionErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class SourceRejectionCode(StrEnum):
    NULL_JOIN_KEY = "null_join_key"
    NEGATIVE_IDENTIFIER = "negative_identifier"
    NON_FINITE_IDENTIFIER = "non_finite_identifier"
    NON_INTEGRAL_IDENTIFIER = "non_integral_identifier"
    UNSAFE_FLOAT_IDENTIFIER = "unsafe_float_identifier"
    MALFORMED_IDENTIFIER = "malformed_identifier"


class ResolutionLimits(FrozenDomainModel):
    max_tables: int = Field(default=3, ge=1, le=3)
    max_preview_rows: int = Field(default=500, ge=1, le=10_000)
    statement_timeout_ms: int = Field(default=5_000, ge=10, le=60_000)


class ResolutionAssumption(FrozenDomainModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    message: str = Field(min_length=1)


class FanoutMitigation(FrozenDomainModel):
    contract_id: str = Field(min_length=1)
    metric_alias: str = Field(min_length=1)
    requested_operation: MetricOperation | AdvancedMetricOperation
    applied_operation: MetricOperation | AdvancedMetricOperation
    automatic: bool
    reason: str = Field(min_length=1)


class RejectionCheck(FrozenDomainModel):
    logical_field: LogicalFieldRef
    physical_field: PhysicalFieldRef
    transformation_plan: TransformationPlan


class RejectedSourceRecord(FrozenDomainModel):
    logical_field: LogicalFieldRef
    physical_field: PhysicalFieldRef
    source_value: str | None
    code: SourceRejectionCode
    reason: str = Field(min_length=1)


class RejectedSourceReport(FrozenDomainModel):
    inspected_fields: tuple[PhysicalFieldRef, ...] = ()
    records: tuple[RejectedSourceRecord, ...] = ()
    total_records: int = Field(default=0, ge=0, le=MAX_REJECTED_SOURCE_TOTAL)
    truncated: bool = False
    database_user: str | None = None
    transaction_read_only: bool | None = None
    statement_timeout_ms: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def safety_facts_must_exist_for_inspected_sources(self) -> RejectedSourceReport:
        safety = (
            self.database_user,
            self.transaction_read_only,
            self.statement_timeout_ms,
        )
        if self.inspected_fields and any(value is None for value in safety):
            raise ValueError("rejection inspection requires database safety facts")
        if not self.inspected_fields and any(value is not None for value in safety):
            raise ValueError("empty rejection inspection cannot claim database safety facts")
        inspected = {field.root for field in self.inspected_fields}
        if any(record.physical_field.root not in inspected for record in self.records):
            raise ValueError("rejected source record was not part of the inspected fields")
        if self.total_records < len(self.records):
            raise ValueError("rejected source total cannot be smaller than the returned records")
        if self.truncated != (self.total_records > len(self.records)):
            raise ValueError("rejected source truncation must match the bounded record sample")
        return self


class ResolvedSemanticPlan(FrozenDomainModel):
    context_source: str = Field(min_length=1)
    context_version: int = Field(ge=1)
    context_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    activation_generation: int | None = Field(default=None, ge=1)
    active_pointer_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    active_scope_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    execution_target: GovernedExecutionTarget | None = None
    request: AnalyticalRequest
    selected_mappings: tuple[GovernedFieldMapping, ...] = Field(min_length=1)
    selected_contracts: tuple[JoinContract, ...] = Field(default=(), max_length=2)
    assumptions: tuple[ResolutionAssumption, ...] = Field(min_length=1)
    fanout_mitigations: tuple[FanoutMitigation, ...] = ()
    rejection_checks: tuple[RejectionCheck, ...] = ()
    query_plan: QueryPlan
    query_policy: QueryPolicy

    @model_validator(mode="after")
    def plan_assets_must_match_selected_context(self) -> ResolvedSemanticPlan:
        if (self.activation_generation is None) != (self.active_pointer_fingerprint is None):
            raise ValueError(
                "active semantic plans require generation and pointer fingerprint together"
            )
        if self.activation_generation is None and self.active_scope_fingerprint is not None:
            raise ValueError("inactive semantic plans cannot claim a tenant scope fingerprint")
        if self.activation_generation is None and self.execution_target is not None:
            raise ValueError("inactive semantic plans cannot claim a managed execution target")
        plan_assets = {self.query_plan.root_scan.dataset.root}
        plan_assets.update(join.right_scan.dataset.root for join in self.query_plan.joins)
        policy_assets = {asset.dataset.root for asset in self.query_policy.assets}
        if plan_assets != policy_assets:
            raise ValueError("resolved plan assets must exactly match its SQL policy allowlist")
        if len(plan_assets) > self.query_policy.max_tables:
            raise ValueError("resolved plan exceeds its table-count policy")
        return self


class AdvancedResolvedSemanticPlan(FrozenDomainModel):
    context_source: str = Field(min_length=1)
    context_version: int = Field(ge=1)
    context_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    activation_generation: int | None = Field(default=None, ge=1)
    active_pointer_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    active_scope_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    execution_target: GovernedExecutionTarget | None = None
    request: AdvancedAnalyticalRequest
    selected_mappings: tuple[GovernedFieldMapping, ...] = Field(min_length=1)
    selected_contracts: tuple[JoinContract, ...] = Field(default=(), max_length=2)
    assumptions: tuple[ResolutionAssumption, ...] = Field(min_length=1)
    fanout_mitigations: tuple[FanoutMitigation, ...] = ()
    rejection_checks: tuple[RejectionCheck, ...] = ()
    query_plan: AdvancedQueryPlan
    query_policy: QueryPolicy

    @model_validator(mode="after")
    def plan_assets_must_match_selected_context(
        self,
    ) -> AdvancedResolvedSemanticPlan:
        _validate_resolved_plan_metadata(
            activation_generation=self.activation_generation,
            active_pointer_fingerprint=self.active_pointer_fingerprint,
            active_scope_fingerprint=self.active_scope_fingerprint,
            execution_target=self.execution_target,
            query_plan=self.query_plan,
            query_policy=self.query_policy,
        )
        return self


ResolvedPlanLike: TypeAlias = AdvancedResolvedSemanticPlan | ResolvedSemanticPlan


def _validate_resolved_plan_metadata(
    *,
    activation_generation: int | None,
    active_pointer_fingerprint: str | None,
    active_scope_fingerprint: str | None,
    execution_target: GovernedExecutionTarget | None,
    query_plan: RestrictedQueryPlan,
    query_policy: QueryPolicy,
) -> None:
    if (activation_generation is None) != (active_pointer_fingerprint is None):
        raise ValueError(
            "active semantic plans require generation and pointer fingerprint together"
        )
    if activation_generation is None and active_scope_fingerprint is not None:
        raise ValueError("inactive semantic plans cannot claim a tenant scope fingerprint")
    if activation_generation is None and execution_target is not None:
        raise ValueError("inactive semantic plans cannot claim a managed execution target")
    plan_assets = {query_plan.root_scan.dataset.root}
    plan_assets.update(join.right_scan.dataset.root for join in query_plan.joins)
    policy_assets = {asset.dataset.root for asset in query_policy.assets}
    if plan_assets != policy_assets:
        raise ValueError("resolved plan assets must exactly match its SQL policy allowlist")
    if len(plan_assets) > query_policy.max_tables:
        raise ValueError("resolved plan exceeds its table-count policy")


@dataclass(frozen=True, slots=True)
class _PathEdge:
    contract: JoinContract
    from_model: str
    to_model: str


@overload
def resolve_semantic_request(
    validated: ValidatedAnalyticalRequest,
    context: GovernedSemanticRegistrySnapshot,
    limits: ResolutionLimits,
) -> ResolvedSemanticPlan: ...


@overload
def resolve_semantic_request(
    validated: ValidatedAdvancedAnalyticalRequest,
    context: GovernedSemanticRegistrySnapshot,
    limits: ResolutionLimits,
) -> AdvancedResolvedSemanticPlan: ...


def resolve_semantic_request(
    validated: ValidatedRequestLike,
    context: GovernedSemanticRegistrySnapshot,
    limits: ResolutionLimits,
) -> ResolvedPlanLike:
    """Resolve only approved current context into the existing restricted query IR."""

    logical = context.logical_context
    logical_fingerprint = approved_logical_context_fingerprint(logical)
    if (
        validated.context_source != logical.source
        or validated.context_version != logical.version
        or validated.context_fingerprint != logical_fingerprint
    ):
        raise SemanticResolutionError(
            ResolutionErrorCode.STALE_LOGICAL_CONTEXT,
            "guided request context changed; rebuild and revalidate the request before planning",
        )
    revalidated = validate_analytical_request(validated.request, logical).validated_request
    if revalidated != validated:
        raise SemanticResolutionError(
            ResolutionErrorCode.STALE_LOGICAL_CONTEXT,
            "guided request no longer matches the current approved logical context",
        )

    requested_models = _requested_models(validated)
    edges, resolved_models = _resolve_join_edges(
        validated.request.primary_entity.root,
        requested_models,
        context,
    )
    contract_ids = tuple(edge.contract.id for edge in edges)
    if contract_ids != validated.join_contract_ids or tuple(resolved_models) != tuple(
        model.root for model in validated.required_models
    ):
        raise SemanticResolutionError(
            ResolutionErrorCode.STALE_LOGICAL_CONTEXT,
            "validated logical path disagrees with the current executable join contracts",
        )

    required_fields = _required_fields(validated, edges)
    selected_mappings, dataset_by_model = _select_mappings(
        required_fields,
        resolved_models,
        edges,
        context,
    )
    _validate_mapping_types(selected_mappings, logical)
    datasets = tuple(dict.fromkeys(dataset_by_model[model] for model in resolved_models))
    if len(datasets) > limits.max_tables:
        raise SemanticResolutionError(
            ResolutionErrorCode.TOO_MANY_TABLES,
            f"resolved request requires {len(datasets)} tables; maximum is {limits.max_tables}",
        )

    operations, mitigations, fanout_assumptions = _resolve_fanout(validated, edges)
    query_plan = _build_query_plan(
        validated,
        edges,
        resolved_models,
        selected_mappings,
        dataset_by_model,
        operations,
    )
    query_policy = _build_query_policy(query_plan, limits)
    assumptions = [
        ResolutionAssumption(
            code="approved_context_selected",
            message=(
                f"Selected planning context {context.source} version {context.version} with "
                "explicit mapping and decision versions."
            ),
        )
    ]
    if edges:
        assumptions.append(
            ResolutionAssumption(
                code="shortest_approved_join_path",
                message="Selected unique shortest approved join path: " + " -> ".join(contract_ids),
            )
        )
    else:
        assumptions.append(
            ResolutionAssumption(
                code="no_join_required",
                message=(
                    "Every requested logical field resolves to the primary "
                    f"{validated.request.primary_entity.root} dataset."
                ),
            )
        )
    assumptions.extend(fanout_assumptions)
    assumptions.extend(
        ResolutionAssumption(
            code="approved_dataset_selection",
            message=f"{model} resolves to {dataset_by_model[model]} using approved mappings.",
        )
        for model in resolved_models
    )
    checks = _rejection_checks(edges)
    common = {
        "context_source": context.source,
        "context_version": context.version,
        "context_fingerprint": governed_semantic_registry_fingerprint(context),
        "selected_mappings": selected_mappings,
        "selected_contracts": tuple(edge.contract for edge in edges),
        "assumptions": tuple(assumptions),
        "fanout_mitigations": mitigations,
        "rejection_checks": checks,
        "query_policy": query_policy,
    }
    if isinstance(validated, ValidatedAdvancedAnalyticalRequest):
        if not isinstance(query_plan, AdvancedQueryPlan):
            raise AssertionError("advanced resolution produced a version-1 query plan")
        return AdvancedResolvedSemanticPlan(
            request=validated.request,
            query_plan=query_plan,
            **common,
        )
    if not isinstance(query_plan, QueryPlan):
        raise AssertionError("version-1 resolution produced an advanced query plan")
    return ResolvedSemanticPlan(
        request=validated.request,
        query_plan=query_plan,
        **common,
    )


def _requested_models(validated: ValidatedRequestLike) -> tuple[str, ...]:
    models = [validated.request.primary_entity.root]
    for field in _request_fields(validated):
        model = _model_for_field(field)
        if model not in models:
            models.append(model)
    return tuple(models)


def _request_fields(validated: ValidatedRequestLike) -> tuple[LogicalFieldRef, ...]:
    request = validated.request
    if isinstance(request, AdvancedAnalyticalRequest):
        values = [item.field for item in request.fields]
        values.extend(item.field for item in request.metrics if item.field is not None)
        if request.where is not None:
            values.extend(item.field for item in _logical_filter_leaves(request.where))
        for metric in request.metrics:
            if metric.condition is not None:
                values.extend(item.field for item in _logical_filter_leaves(metric.condition))
    else:
        values = [item.field for item in request.dimensions]
        values.extend(item.field for item in request.metrics)
        values.extend(item.field for item in request.filters)
        values.extend(item.field for item in request.order_by)
    return tuple(dict.fromkeys(values))


def _logical_filter_leaves(
    predicate: LogicalBooleanPredicate,
) -> tuple[Filter, ...]:
    if predicate.kind is BooleanOperator.COMPARISON:
        assert predicate.comparison is not None
        return (predicate.comparison,)
    return tuple(item for operand in predicate.operands for item in _logical_filter_leaves(operand))


def _resolve_join_edges(
    primary: str,
    targets: tuple[str, ...],
    context: SemanticPlanningContext,
) -> tuple[tuple[_PathEdge, ...], tuple[str, ...]]:
    approved = tuple(
        contract
        for contract in context.join_contracts.contracts
        if contract.status is ApprovalStatus.APPROVED
    )
    all_contracts = context.join_contracts.contracts
    selected: list[_PathEdge] = []
    models = [primary]
    for target in targets:
        if target == primary:
            continue
        paths = _shortest_contract_paths(primary, target, approved)
        if not paths:
            if _shortest_contract_paths(primary, target, all_contracts):
                raise SemanticResolutionError(
                    ResolutionErrorCode.UNAPPROVED_JOIN,
                    f"a join path to {target} exists but is not fully approved",
                )
            raise SemanticResolutionError(
                ResolutionErrorCode.MISSING_JOIN,
                f"no approved executable join path connects {primary} to {target}",
            )
        if len(paths) > 1:
            choices = ", ".join(" -> ".join(edge.contract.id for edge in path) for path in paths)
            raise SemanticResolutionError(
                ResolutionErrorCode.AMBIGUOUS_JOIN,
                f"multiple equally short approved join paths reach {target}: {choices}",
            )
        for edge in paths[0]:
            _validate_contract_summary(edge.contract, context.logical_context)
            if edge.contract.id not in {item.contract.id for item in selected}:
                selected.append(edge)
            if edge.to_model not in models:
                models.append(edge.to_model)
    return tuple(selected), tuple(models)


def _shortest_contract_paths(
    source: str,
    target: str,
    contracts: tuple[JoinContract, ...],
) -> tuple[tuple[_PathEdge, ...], ...]:
    adjacency: dict[str, list[_PathEdge]] = {}
    for contract in contracts:
        left = _model_for_field(contract.left_key.logical_field)
        right = _model_for_field(contract.right_key.logical_field)
        adjacency.setdefault(left, []).append(_PathEdge(contract, left, right))
        adjacency.setdefault(right, []).append(_PathEdge(contract, right, left))
    queue: deque[tuple[str, tuple[str, ...], tuple[_PathEdge, ...]]] = deque(
        [(source, (source,), ())]
    )
    found: list[tuple[_PathEdge, ...]] = []
    shortest: int | None = None
    while queue:
        current, visited, path = queue.popleft()
        if shortest is not None and len(path) >= shortest:
            continue
        for edge in sorted(adjacency.get(current, ()), key=lambda item: item.contract.id):
            if edge.to_model in visited:
                continue
            next_path = (*path, edge)
            if edge.to_model == target:
                shortest = len(next_path)
                found.append(next_path)
            else:
                queue.append((edge.to_model, (*visited, edge.to_model), next_path))
    return tuple(path for path in found if len(path) == shortest)


def _validate_contract_summary(
    contract: JoinContract,
    logical_context: ApprovedLogicalContext,
) -> None:
    summary = next((item for item in logical_context.joins if item.id == contract.id), None)
    if summary is None:
        raise SemanticResolutionError(
            ResolutionErrorCode.STALE_JOIN,
            f"join contract {contract.id} is absent from the approved logical context",
        )
    left = _model_for_field(contract.left_key.logical_field)
    right = _model_for_field(contract.right_key.logical_field)
    if (
        summary.version != contract.version
        or summary.left_model.root != left
        or summary.right_model.root != right
        or summary.cardinality is not contract.cardinality
        or summary.fanout_policy is not contract.fanout_policy
    ):
        raise SemanticResolutionError(
            ResolutionErrorCode.STALE_JOIN,
            f"join contract {contract.id} no longer matches its approved logical summary",
        )


def _required_fields(
    validated: ValidatedRequestLike,
    edges: tuple[_PathEdge, ...],
) -> tuple[LogicalFieldRef, ...]:
    fields = list(_request_fields(validated))
    for edge in edges:
        for key in (edge.contract.left_key, edge.contract.right_key):
            if key.logical_field not in fields:
                fields.append(key.logical_field)
    return tuple(fields)


def _select_mappings(
    required_fields: tuple[LogicalFieldRef, ...],
    resolved_models: tuple[str, ...],
    edges: tuple[_PathEdge, ...],
    context: SemanticPlanningContext,
) -> tuple[tuple[GovernedFieldMapping, ...], dict[str, str]]:
    definitions = context.logical_context.field_index()
    by_field: dict[str, list[GovernedFieldMapping]] = {}
    for governed in context.mapping_set.mappings:
        by_field.setdefault(governed.mapping.logical_field.root, []).append(governed)
    candidates: dict[str, tuple[GovernedFieldMapping, ...]] = {}
    for field in required_fields:
        definition = definitions.get(field.root)
        if definition is None:
            raise SemanticResolutionError(
                ResolutionErrorCode.STALE_LOGICAL_CONTEXT,
                f"required field {field.root} is absent from approved logical context",
            )
        available = tuple(by_field.get(field.root, ()))
        approved = tuple(
            item for item in available if item.mapping.status is ApprovalStatus.APPROVED
        )
        if not approved:
            code = (
                ResolutionErrorCode.UNAPPROVED_MAPPING
                if available
                else ResolutionErrorCode.MISSING_MAPPING
            )
            raise SemanticResolutionError(
                code, f"no approved mapping is available for {field.root}"
            )
        current = tuple(
            item for item in approved if item.logical_field_version == definition.version
        )
        if not current:
            raise SemanticResolutionError(
                ResolutionErrorCode.STALE_MAPPING,
                f"approved mappings for {field.root} do not match logical version {definition.version}",
            )
        candidates[field.root] = current

    forced: dict[str, GovernedFieldMapping] = {}
    for edge in edges:
        for key in (edge.contract.left_key, edge.contract.right_key):
            matching = tuple(
                item
                for item in candidates[key.logical_field.root]
                if item.mapping.physical_field == key.physical_field
                and item.mapping.transformation_plan == key.transformation_plan
            )
            if len(matching) != 1:
                raise SemanticResolutionError(
                    ResolutionErrorCode.CONTRACT_MAPPING_MISMATCH,
                    f"join key {key.logical_field.root} does not match one current approved mapping",
                )
            previous = forced.setdefault(key.logical_field.root, matching[0])
            if previous != matching[0]:
                raise SemanticResolutionError(
                    ResolutionErrorCode.DISCONNECTED_MAPPING,
                    f"join paths require incompatible mappings for {key.logical_field.root}",
                )

    selected: list[GovernedFieldMapping] = []
    dataset_by_model: dict[str, str] = {}
    for model in resolved_models:
        model_fields = [field for field in required_fields if _model_for_field(field) == model]
        dataset_sets: list[set[str]] = []
        for field in model_fields:
            forced_candidate: GovernedFieldMapping | None = forced.get(field.root)
            field_candidates = (
                (forced_candidate,) if forced_candidate is not None else candidates[field.root]
            )
            dataset_sets.append(
                {_dataset(item.mapping.physical_field) for item in field_candidates}
            )
        common = set.intersection(*dataset_sets) if dataset_sets else set()
        if not common:
            raise SemanticResolutionError(
                ResolutionErrorCode.DISCONNECTED_MAPPING,
                f"approved mappings for {model} do not resolve to one physical dataset",
            )
        if len(common) != 1:
            raise SemanticResolutionError(
                ResolutionErrorCode.AMBIGUOUS_MAPPING,
                f"approved mappings for {model} resolve equally to: {', '.join(sorted(common))}",
            )
        dataset = next(iter(common))
        dataset_by_model[model] = dataset
        for field in model_fields:
            options = tuple(
                item
                for item in candidates[field.root]
                if _dataset(item.mapping.physical_field) == dataset
            )
            forced_mapping = forced.get(field.root)
            if forced_mapping is not None:
                options = tuple(item for item in options if item == forced_mapping)
            if len(options) != 1:
                raise SemanticResolutionError(
                    ResolutionErrorCode.AMBIGUOUS_MAPPING,
                    f"{field.root} does not have exactly one approved mapping on {dataset}",
                )
            if options[0] not in selected:
                selected.append(options[0])
    return tuple(selected), dataset_by_model


def _resolve_fanout(
    validated: ValidatedRequestLike,
    edges: tuple[_PathEdge, ...],
) -> tuple[
    dict[int, MetricOperation | AdvancedMetricOperation],
    tuple[FanoutMitigation, ...],
    tuple[ResolutionAssumption, ...],
]:
    if isinstance(validated, ValidatedAdvancedAnalyticalRequest):
        advanced_operations, mitigations, assumptions = _resolve_advanced_fanout(
            validated,
            edges,
        )
        combined: dict[int, MetricOperation | AdvancedMetricOperation] = dict(advanced_operations)
        return combined, mitigations, assumptions
    v1_operations, mitigations, assumptions = _resolve_v1_fanout(validated, edges)
    combined = dict(v1_operations)
    return combined, mitigations, assumptions


def _resolve_v1_fanout(
    validated: ValidatedAnalyticalRequest,
    edges: tuple[_PathEdge, ...],
) -> tuple[
    dict[int, MetricOperation],
    tuple[FanoutMitigation, ...],
    tuple[ResolutionAssumption, ...],
]:
    operations = {index: metric.operation for index, metric in enumerate(validated.request.metrics)}
    mitigations: list[FanoutMitigation] = []
    assumptions: list[ResolutionAssumption] = []
    for index, metric in enumerate(validated.request.metrics):
        metric_model = _model_for_field(metric.field)
        if metric_model != validated.request.primary_entity.root:
            assumptions.append(
                ResolutionAssumption(
                    code="relationship_metric_requested",
                    message=(
                        f"{metric.alias or metric.field.root} applies {metric.operation.value} to "
                        f"{metric_model} values, not to "
                        f"{validated.request.primary_entity.root} entities."
                    ),
                )
            )
        scanned_models = {validated.request.primary_entity.root}
        upstream_fanout_contract: str | None = None
        for edge in edges:
            contract = edge.contract
            cardinality = _oriented_cardinality(edge)
            if upstream_fanout_contract is not None and edge.to_model == metric_model:
                if metric.operation not in _DUPLICATION_INVARIANT_OPERATIONS:
                    raise SemanticResolutionError(
                        ResolutionErrorCode.UNSUPPORTED_FANOUT,
                        (
                            f"metric {metric.field.root} is downstream of fanout contract "
                            f"{upstream_fanout_contract}; {metric.operation.value} is not "
                            "invariant under duplicated rows"
                        ),
                    )
                mitigations.append(
                    FanoutMitigation(
                        contract_id=upstream_fanout_contract,
                        metric_alias=metric.alias or metric.field.root,
                        requested_operation=metric.operation,
                        applied_operation=metric.operation,
                        automatic=False,
                        reason=(
                            f"{metric.operation.value} is invariant under row duplication from "
                            f"upstream fanout contract {upstream_fanout_contract}."
                        ),
                    )
                )
            affected = metric_model in scanned_models and cardinality in {
                Cardinality.ONE_TO_MANY,
                Cardinality.MANY_TO_MANY,
            }
            scanned_models.add(edge.to_model)
            if cardinality is Cardinality.ONE_TO_MANY and upstream_fanout_contract is None:
                upstream_fanout_contract = contract.id
            if not affected:
                continue
            if cardinality is Cardinality.MANY_TO_MANY:
                raise SemanticResolutionError(
                    ResolutionErrorCode.UNSUPPORTED_FANOUT,
                    f"contract {contract.id} has unsupported many_to_many fanout",
                )
            if contract.fanout_policy is not FanoutPolicy.REQUIRE_DISTINCT_FOR_LEFT_ENTITY_METRICS:
                orientation = (
                    f"reversed {contract.cardinality.value}"
                    if edge.from_model == _model_for_field(contract.right_key.logical_field)
                    else contract.cardinality.value
                )
                raise SemanticResolutionError(
                    ResolutionErrorCode.UNSUPPORTED_FANOUT,
                    (
                        f"contract {contract.id} is {orientation} from "
                        f"{edge.from_model} to {edge.to_model} and has no approved mitigation"
                    ),
                )
            requested = metric.operation
            if requested is MetricOperation.COUNT:
                existing_key = (
                    contract.left_key
                    if edge.from_model == _model_for_field(contract.left_key.logical_field)
                    else contract.right_key
                )
                if metric.field != existing_key.logical_field:
                    raise SemanticResolutionError(
                        ResolutionErrorCode.UNSUPPORTED_FANOUT,
                        (
                            f"metric {metric.field.root} is not the exact approved one-side key "
                            f"{existing_key.logical_field.root}; automatic COUNT DISTINCT would "
                            "change the requested metric"
                        ),
                    )
                operations[index] = MetricOperation.COUNT_DISTINCT
                automatic = True
            elif requested in _DUPLICATION_INVARIANT_OPERATIONS:
                automatic = False
            else:
                raise SemanticResolutionError(
                    ResolutionErrorCode.UNSUPPORTED_FANOUT,
                    (
                        f"contract {contract.id} permits only exact-key COUNT DISTINCT or a "
                        f"duplication-invariant aggregate, not {requested.value}"
                    ),
                )
            mitigations.append(
                FanoutMitigation(
                    contract_id=contract.id,
                    metric_alias=metric.alias or metric.field.root,
                    requested_operation=requested,
                    applied_operation=operations[index],
                    automatic=automatic,
                    reason=(
                        f"{contract.id} is one_to_many and can multiply {edge.from_model} rows; "
                        + (
                            "the exact approved one-side key therefore requires COUNT DISTINCT."
                            if requested is MetricOperation.COUNT
                            else f"{requested.value} is invariant under duplicate rows."
                        )
                    ),
                )
            )
    return operations, tuple(mitigations), tuple(assumptions)


def _resolve_advanced_fanout(
    validated: ValidatedAdvancedAnalyticalRequest,
    edges: tuple[_PathEdge, ...],
) -> tuple[
    dict[int, AdvancedMetricOperation],
    tuple[FanoutMitigation, ...],
    tuple[ResolutionAssumption, ...],
]:
    request = validated.request
    operations = {index: metric.operation for index, metric in enumerate(request.metrics)}
    mitigations: list[FanoutMitigation] = []
    assumptions: list[ResolutionAssumption] = []
    invariant = {
        AdvancedMetricOperation.COUNT_DISTINCT,
        AdvancedMetricOperation.MIN,
        AdvancedMetricOperation.MAX,
    }
    for index, metric in enumerate(request.metrics):
        metric_model = (
            request.primary_entity.root if metric.field is None else _model_for_field(metric.field)
        )
        if metric_model != request.primary_entity.root and metric.field is not None:
            assumptions.append(
                ResolutionAssumption(
                    code="relationship_metric_requested",
                    message=(
                        f"{metric.alias or metric.field.root} applies "
                        f"{metric.operation.value} to {metric_model} values, not to "
                        f"{request.primary_entity.root} entities."
                    ),
                )
            )
        scanned_models = {request.primary_entity.root}
        upstream_fanout_contract: str | None = None
        for edge in edges:
            contract = edge.contract
            cardinality = _oriented_cardinality(edge)
            if cardinality is Cardinality.MANY_TO_MANY:
                raise SemanticResolutionError(
                    ResolutionErrorCode.UNSUPPORTED_FANOUT,
                    f"contract {contract.id} has unsupported many_to_many fanout",
                )
            if upstream_fanout_contract is not None and edge.to_model == metric_model:
                if metric.operation not in invariant:
                    source = metric.field.root if metric.field is not None else "COUNT_ROWS"
                    raise SemanticResolutionError(
                        ResolutionErrorCode.UNSUPPORTED_FANOUT,
                        (
                            f"metric {source} is downstream of fanout contract "
                            f"{upstream_fanout_contract}; {metric.operation.value} is not "
                            "invariant under duplicated rows"
                        ),
                    )
                mitigations.append(
                    FanoutMitigation(
                        contract_id=upstream_fanout_contract,
                        metric_alias=metric.alias
                        or (metric.field.root if metric.field is not None else "count_rows"),
                        requested_operation=metric.operation,
                        applied_operation=metric.operation,
                        automatic=False,
                        reason=(
                            f"{metric.operation.value} is invariant under row duplication from "
                            f"upstream fanout contract {upstream_fanout_contract}."
                        ),
                    )
                )
            affected = metric_model in scanned_models and cardinality is Cardinality.ONE_TO_MANY
            scanned_models.add(edge.to_model)
            if cardinality is Cardinality.ONE_TO_MANY and upstream_fanout_contract is None:
                upstream_fanout_contract = contract.id
            if not affected:
                continue
            if metric.operation is AdvancedMetricOperation.COUNT_ROWS:
                raise SemanticResolutionError(
                    ResolutionErrorCode.UNSUPPORTED_FANOUT,
                    (
                        f"COUNT_ROWS is ambiguous across one_to_many contract {contract.id}; "
                        "choose an approved identifier and COUNT DISTINCT explicitly"
                    ),
                )
            if contract.fanout_policy is not FanoutPolicy.REQUIRE_DISTINCT_FOR_LEFT_ENTITY_METRICS:
                raise SemanticResolutionError(
                    ResolutionErrorCode.UNSUPPORTED_FANOUT,
                    f"contract {contract.id} has no approved fanout mitigation",
                )
            requested = metric.operation
            if requested is AdvancedMetricOperation.COUNT:
                assert metric.field is not None
                existing_key = (
                    contract.left_key
                    if edge.from_model == _model_for_field(contract.left_key.logical_field)
                    else contract.right_key
                )
                if metric.field != existing_key.logical_field:
                    raise SemanticResolutionError(
                        ResolutionErrorCode.UNSUPPORTED_FANOUT,
                        (
                            f"metric {metric.field.root} is not the exact approved one-side key "
                            f"{existing_key.logical_field.root}; automatic COUNT DISTINCT would "
                            "change the requested metric"
                        ),
                    )
                operations[index] = AdvancedMetricOperation.COUNT_DISTINCT
                automatic = True
            elif requested in invariant:
                automatic = False
            else:
                raise SemanticResolutionError(
                    ResolutionErrorCode.UNSUPPORTED_FANOUT,
                    (
                        f"contract {contract.id} permits only exact-key COUNT DISTINCT or a "
                        f"duplication-invariant aggregate, not {requested.value}"
                    ),
                )
            assert metric.field is not None
            mitigations.append(
                FanoutMitigation(
                    contract_id=contract.id,
                    metric_alias=metric.alias or metric.field.root,
                    requested_operation=requested,
                    applied_operation=operations[index],
                    automatic=automatic,
                    reason=(
                        f"{contract.id} is one_to_many and can multiply {edge.from_model} rows; "
                        + (
                            "the exact approved one-side key therefore requires COUNT DISTINCT."
                            if requested is AdvancedMetricOperation.COUNT
                            else f"{requested.value} is invariant under duplicate rows."
                        )
                    ),
                )
            )
    return operations, tuple(mitigations), tuple(assumptions)


def _oriented_cardinality(edge: _PathEdge) -> Cardinality:
    left_model = _model_for_field(edge.contract.left_key.logical_field)
    if edge.from_model == left_model:
        return edge.contract.cardinality
    if edge.contract.cardinality is Cardinality.ONE_TO_MANY:
        return Cardinality.MANY_TO_ONE
    if edge.contract.cardinality is Cardinality.MANY_TO_ONE:
        return Cardinality.ONE_TO_MANY
    return edge.contract.cardinality


def _validate_mapping_types(
    mappings: tuple[GovernedFieldMapping, ...],
    logical_context: ApprovedLogicalContext,
) -> None:
    definitions = logical_context.field_index()
    for governed in mappings:
        mapping = governed.mapping
        canonical = definitions[mapping.logical_field.root].canonical_type
        physical = governed.physical_type
        operations = {step.operation for step in mapping.transformation_plan.steps}
        compatible = False
        if canonical is CanonicalType.STRING:
            compatible = (
                physical is PhysicalValueType.STRING
                or (
                    physical is PhysicalValueType.INTEGER and "cast_integer_to_string" in operations
                )
                or (
                    physical is PhysicalValueType.FLOAT
                    and {
                        "validate_finite",
                        "validate_integral",
                        "cast_integer_to_string",
                    }
                    <= operations
                )
            )
        elif canonical is CanonicalType.DATE:
            compatible = (
                physical is PhysicalValueType.DATE
                or (
                    physical is PhysicalValueType.TIMESTAMP
                    and "cast_timestamp_to_date" in operations
                )
                or (physical is PhysicalValueType.STRING and "parse_date" in operations)
            )
        elif canonical is CanonicalType.TIMESTAMP:
            compatible = physical is PhysicalValueType.TIMESTAMP
        elif canonical is CanonicalType.DECIMAL:
            compatible = physical in {
                PhysicalValueType.DECIMAL,
                PhysicalValueType.INTEGER,
                PhysicalValueType.FLOAT,
            }
        elif canonical is CanonicalType.INTEGER:
            compatible = physical is PhysicalValueType.INTEGER or (
                physical in {PhysicalValueType.DECIMAL, PhysicalValueType.FLOAT}
                and "validate_integral" in operations
            )
        elif canonical is CanonicalType.BOOLEAN:
            compatible = physical is PhysicalValueType.BOOLEAN
        if not compatible:
            raise SemanticResolutionError(
                ResolutionErrorCode.INCOMPATIBLE_MAPPING_TYPE,
                (
                    f"approved mapping {mapping.logical_field.root} cannot convert "
                    f"{physical.value} to {canonical.value} with its closed transformation plan"
                ),
            )


def _build_query_plan(
    validated: ValidatedRequestLike,
    edges: tuple[_PathEdge, ...],
    resolved_models: tuple[str, ...],
    selected_mappings: tuple[GovernedFieldMapping, ...],
    dataset_by_model: dict[str, str],
    metric_operations: dict[int, MetricOperation | AdvancedMetricOperation],
) -> RestrictedQueryPlan:
    if isinstance(validated, ValidatedAdvancedAnalyticalRequest):
        advanced_operations: dict[int, AdvancedMetricOperation] = {}
        for index, operation in metric_operations.items():
            if not isinstance(operation, AdvancedMetricOperation):
                raise AssertionError("advanced request received a version-1 metric operation")
            advanced_operations[index] = operation
        return _build_advanced_query_plan(
            validated,
            edges,
            resolved_models,
            selected_mappings,
            dataset_by_model,
            advanced_operations,
        )
    v1_operations: dict[int, MetricOperation] = {}
    for index, operation in metric_operations.items():
        if not isinstance(operation, MetricOperation):
            raise AssertionError("version-1 request received an advanced metric operation")
        v1_operations[index] = operation
    return _build_v1_query_plan(
        validated,
        edges,
        resolved_models,
        selected_mappings,
        dataset_by_model,
        v1_operations,
    )


def _build_v1_query_plan(
    validated: ValidatedAnalyticalRequest,
    edges: tuple[_PathEdge, ...],
    resolved_models: tuple[str, ...],
    selected_mappings: tuple[GovernedFieldMapping, ...],
    dataset_by_model: dict[str, str],
    metric_operations: dict[int, MetricOperation],
) -> QueryPlan:
    request = validated.request
    mapping_by_field = {item.mapping.logical_field.root: item.mapping for item in selected_mappings}
    aliases = {model: RelationAlias(f"r{index}") for index, model in enumerate(resolved_models)}
    scans = {
        model: DatasetScan(
            dataset=PhysicalDatasetRef(dataset_by_model[model]),
            alias=aliases[model],
        )
        for model in resolved_models
    }

    joins: list[ApprovedJoin] = []
    scanned = {resolved_models[0]}
    for edge in edges:
        if edge.from_model not in scanned or edge.to_model in scanned:
            raise SemanticResolutionError(
                ResolutionErrorCode.AMBIGUOUS_JOIN,
                "approved join paths do not form one bounded acyclic scan graph",
            )
        contract = edge.contract
        if (
            edge.from_model == _model_for_field(contract.right_key.logical_field)
            and contract.default_join_type is JoinType.LEFT
        ):
            raise SemanticResolutionError(
                ResolutionErrorCode.UNSUPPORTED_REQUEST,
                "reversing an approved LEFT JOIN contract is outside the MVP",
            )
        from_key, to_key = (
            (contract.left_key, contract.right_key)
            if edge.from_model == _model_for_field(contract.left_key.logical_field)
            else (contract.right_key, contract.left_key)
        )
        joins.append(
            ApprovedJoin(
                contract=contract,
                right_scan=scans[edge.to_model],
                on=JoinPredicate(
                    left=_expression_for_key(from_key, aliases[edge.from_model]),
                    right=_expression_for_key(to_key, aliases[edge.to_model]),
                ),
            )
        )
        scanned.add(edge.to_model)

    dimensions: dict[str, QueryValueExpression] = {}
    projections: list[SelectItem] = []
    for dimension in request.dimensions:
        mapped = _mapped_expression(mapping_by_field[dimension.field.root], aliases)
        expression: QueryValueExpression = (
            DateGrainExpression(source=mapped, grain=dimension.grain)
            if dimension.grain is not None
            else mapped
        )
        dimensions[dimension.field.root] = expression
        projections.append(
            SelectItem(
                expression=expression,
                alias=OutputAlias(dimension.field.root.rsplit(".", 1)[1]),
            )
        )

    for index, metric in enumerate(request.metrics):
        alias = metric.alias or f"{metric.operation.value}_{metric.field.root.rsplit('.', 1)[1]}"
        projections.append(
            SelectItem(
                expression=AggregateExpression(
                    operation=metric_operations[index],
                    source=_mapped_expression(mapping_by_field[metric.field.root], aliases),
                ),
                alias=OutputAlias(alias),
            )
        )

    filters: list[FilterPredicate] = []
    for request_filter in request.filters:
        if request_filter.operator in {FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL}:
            values: tuple[ParameterValue, ...] = ()
        elif isinstance(request_filter.value, tuple):
            values = tuple(ParameterValue(value=value) for value in request_filter.value)
        else:
            values = (ParameterValue(value=request_filter.value),)
        filters.append(
            FilterPredicate(
                expression=_mapped_expression(mapping_by_field[request_filter.field.root], aliases),
                operator=request_filter.operator,
                values=values,
            )
        )

    order_by: list[OrderItem] = []
    for item in request.order_by:
        order_expression: QueryValueExpression | None = dimensions.get(item.field.root)
        if order_expression is None:
            raise SemanticResolutionError(
                ResolutionErrorCode.UNSUPPORTED_REQUEST,
                "ordering by aggregate metrics is outside the current restricted query IR",
            )
        order_by.append(OrderItem(expression=order_expression, direction=item.direction))

    try:
        return QueryPlan(
            root_scan=scans[resolved_models[0]],
            joins=tuple(joins),
            projections=tuple(projections),
            filters=tuple(filters),
            group_by=tuple(dimensions.values()),
            order_by=tuple(order_by),
            limit=request.limit,
        )
    except ValueError as error:
        raise SemanticResolutionError(
            ResolutionErrorCode.UNSUPPORTED_REQUEST,
            "resolved request is outside the restricted query-plan invariants",
        ) from error


def _build_advanced_query_plan(
    validated: ValidatedAdvancedAnalyticalRequest,
    edges: tuple[_PathEdge, ...],
    resolved_models: tuple[str, ...],
    selected_mappings: tuple[GovernedFieldMapping, ...],
    dataset_by_model: dict[str, str],
    metric_operations: dict[int, AdvancedMetricOperation],
) -> AdvancedQueryPlan:
    request = validated.request
    mapping_by_field = {item.mapping.logical_field.root: item.mapping for item in selected_mappings}
    aliases = {model: RelationAlias(f"r{index}") for index, model in enumerate(resolved_models)}
    scans = {
        model: DatasetScan(
            dataset=PhysicalDatasetRef(dataset_by_model[model]),
            alias=aliases[model],
        )
        for model in resolved_models
    }

    joins: list[ApprovedJoin] = []
    scanned = {resolved_models[0]}
    for edge in edges:
        if edge.from_model not in scanned or edge.to_model in scanned:
            raise SemanticResolutionError(
                ResolutionErrorCode.AMBIGUOUS_JOIN,
                "approved join paths do not form one bounded acyclic scan graph",
            )
        contract = edge.contract
        if (
            edge.from_model == _model_for_field(contract.right_key.logical_field)
            and contract.default_join_type is JoinType.LEFT
        ):
            raise SemanticResolutionError(
                ResolutionErrorCode.UNSUPPORTED_REQUEST,
                "reversing an approved LEFT JOIN contract is unsupported",
            )
        from_key, to_key = (
            (contract.left_key, contract.right_key)
            if edge.from_model == _model_for_field(contract.left_key.logical_field)
            else (contract.right_key, contract.left_key)
        )
        joins.append(
            ApprovedJoin(
                contract=contract,
                right_scan=scans[edge.to_model],
                on=JoinPredicate(
                    left=_expression_for_key(from_key, aliases[edge.from_model]),
                    right=_expression_for_key(to_key, aliases[edge.to_model]),
                ),
            )
        )
        scanned.add(edge.to_model)

    projections: list[AdvancedSelectItem] = []
    fields_by_alias: dict[str, QueryValueExpression | BucketExpression] = {}
    for selected_field in request.fields:
        mapped = _mapped_expression(mapping_by_field[selected_field.field.root], aliases)
        if selected_field.grain is not None:
            field_expression: QueryValueExpression | BucketExpression = DateGrainExpression(
                source=mapped,
                grain=selected_field.grain,
            )
        elif selected_field.buckets:
            field_expression = BucketExpression(
                source=mapped,
                buckets=tuple(
                    NumericBucketPlan(
                        label=ParameterValue(value=bucket.label),
                        lower=(
                            ParameterValue(value=bucket.lower) if bucket.lower is not None else None
                        ),
                        upper=(
                            ParameterValue(value=bucket.upper) if bucket.upper is not None else None
                        ),
                    )
                    for bucket in selected_field.buckets
                ),
                else_value=ParameterValue(value=selected_field.else_label),
            )
        else:
            field_expression = mapped
        alias = selected_field.alias or selected_field.field.root.rsplit(".", 1)[1]
        fields_by_alias[alias] = field_expression
        projections.append(
            AdvancedSelectItem(expression=field_expression, alias=OutputAlias(alias))
        )

    metrics_by_alias: dict[str, AdvancedAggregateExpression] = {}
    for index, metric in enumerate(request.metrics):
        source = (
            _mapped_expression(mapping_by_field[metric.field.root], aliases)
            if metric.field is not None
            else None
        )
        aggregate_expression = AdvancedAggregateExpression(
            operation=metric_operations[index],
            source=source,
            condition=(
                _physical_boolean_predicate(metric.condition, mapping_by_field, aliases)
                if metric.condition is not None
                else None
            ),
        )
        alias = metric.alias or (
            metric.operation.value
            if metric.field is None
            else f"{metric.operation.value}_{metric.field.root.rsplit('.', 1)[1]}"
        )
        metrics_by_alias[alias] = aggregate_expression
        projections.append(
            AdvancedSelectItem(
                expression=aggregate_expression,
                alias=OutputAlias(alias),
            )
        )

    group_by = tuple(fields_by_alias[alias] for alias in request.group_by)
    where = (
        _physical_boolean_predicate(request.where, mapping_by_field, aliases)
        if request.where is not None
        else None
    )
    having = (
        _aggregate_boolean_predicate(request.having, metrics_by_alias)
        if request.having is not None
        else None
    )
    windows = tuple(
        WindowSelectItem(
            expression=WindowExpression(
                operation=window.operation,
                source=OutputAlias(window.source) if window.source is not None else None,
                partition_by=tuple(OutputAlias(alias) for alias in window.partition_by),
                order_by=tuple(
                    OutputOrderItem(alias=OutputAlias(item.alias), direction=item.direction)
                    for item in window.order_by
                ),
                buckets=window.buckets,
                offset=window.offset,
                preceding_rows=window.preceding_rows,
            ),
            alias=OutputAlias(window.alias),
        )
        for window in request.windows
    )
    post_filter = (
        _output_boolean_predicate(request.post_filter) if request.post_filter is not None else None
    )
    result_order = tuple(
        OutputOrderItem(alias=OutputAlias(item.alias), direction=item.direction)
        for item in request.result_order_by
    )
    try:
        return AdvancedQueryPlan(
            root_scan=scans[resolved_models[0]],
            joins=tuple(joins),
            projections=tuple(projections),
            group_by=group_by,
            where=where,
            having=having,
            windows=windows,
            post_filter=post_filter,
            result_order_by=result_order,
            grouping=request.grouping,
            limit=request.limit,
        )
    except ValueError as error:
        raise SemanticResolutionError(
            ResolutionErrorCode.UNSUPPORTED_REQUEST,
            "resolved request is outside the version-2 query-plan invariants",
        ) from error


def _physical_boolean_predicate(
    predicate: LogicalBooleanPredicate,
    mapping_by_field: dict[str, ColumnMapping],
    aliases: dict[str, RelationAlias],
) -> PhysicalBooleanPredicate:
    if predicate.kind is BooleanOperator.COMPARISON:
        assert predicate.comparison is not None
        return PhysicalBooleanPredicate(
            kind=predicate.kind,
            comparison=_physical_filter(predicate.comparison, mapping_by_field, aliases),
        )
    return PhysicalBooleanPredicate(
        kind=predicate.kind,
        operands=tuple(
            _physical_boolean_predicate(item, mapping_by_field, aliases)
            for item in predicate.operands
        ),
    )


def _physical_filter(
    request_filter: Filter,
    mapping_by_field: dict[str, ColumnMapping],
    aliases: dict[str, RelationAlias],
) -> FilterPredicate:
    if request_filter.operator in {FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL}:
        values: tuple[ParameterValue, ...] = ()
    elif isinstance(request_filter.value, tuple):
        values = tuple(ParameterValue(value=value) for value in request_filter.value)
    else:
        values = (ParameterValue(value=request_filter.value),)
    return FilterPredicate(
        expression=_mapped_expression(mapping_by_field[request_filter.field.root], aliases),
        operator=request_filter.operator,
        values=values,
    )


def _aggregate_boolean_predicate(
    predicate: OutputBooleanPredicate,
    metrics_by_alias: dict[str, AdvancedAggregateExpression],
) -> AggregateBooleanPredicate:
    if predicate.kind is BooleanOperator.COMPARISON:
        assert predicate.comparison is not None
        item = predicate.comparison
        return AggregateBooleanPredicate(
            kind=predicate.kind,
            comparison=AggregateFilterPredicate(
                expression=metrics_by_alias[item.alias],
                operator=item.operator,
                values=_parameter_values(item.value),
                compare_to=(
                    metrics_by_alias[item.compare_to_alias]
                    if item.compare_to_alias is not None
                    else None
                ),
            ),
        )
    return AggregateBooleanPredicate(
        kind=predicate.kind,
        operands=tuple(
            _aggregate_boolean_predicate(item, metrics_by_alias) for item in predicate.operands
        ),
    )


def _output_boolean_predicate(
    predicate: OutputBooleanPredicate,
) -> OutputBooleanPredicatePlan:
    if predicate.kind is BooleanOperator.COMPARISON:
        assert predicate.comparison is not None
        item = predicate.comparison
        return OutputBooleanPredicatePlan(
            kind=predicate.kind,
            comparison=OutputFilterPredicate(
                alias=OutputAlias(item.alias),
                operator=item.operator,
                values=_parameter_values(item.value),
                compare_to=(
                    OutputAlias(item.compare_to_alias)
                    if item.compare_to_alias is not None
                    else None
                ),
            ),
        )
    return OutputBooleanPredicatePlan(
        kind=predicate.kind,
        operands=tuple(_output_boolean_predicate(item) for item in predicate.operands),
    )


def _parameter_values(
    value: object,
) -> tuple[ParameterValue, ...]:
    if value is None:
        return ()
    if isinstance(value, tuple):
        return tuple(ParameterValue(value=item) for item in value)
    return (ParameterValue(value=value),)


def _mapped_expression(
    mapping: ColumnMapping,
    aliases: dict[str, RelationAlias],
) -> MappedExpression:
    model = _model_for_field(mapping.logical_field)
    return MappedExpression(
        source=ColumnExpression(
            relation=aliases[model],
            field=mapping.physical_field,
        ),
        transformation_plan=mapping.transformation_plan,
    )


def _expression_for_key(key: NormalizedJoinKey, alias: RelationAlias) -> MappedExpression:
    return MappedExpression(
        source=ColumnExpression(relation=alias, field=key.physical_field),
        transformation_plan=key.transformation_plan,
    )


def _build_query_policy(
    plan: RestrictedQueryPlan,
    limits: ResolutionLimits,
) -> QueryPolicy:
    if isinstance(plan, AdvancedQueryPlan):
        return _build_advanced_query_policy(plan, limits)
    return _build_v1_query_policy(plan, limits)


def _build_v1_query_policy(plan: QueryPlan, limits: ResolutionLimits) -> QueryPolicy:
    datasets = [plan.root_scan.dataset]
    datasets.extend(join.right_scan.dataset for join in plan.joins)
    fields: dict[str, set[str]] = {dataset.root: set() for dataset in datasets}

    def add(expression: QueryValueExpression) -> None:
        if isinstance(expression, ColumnExpression):
            dataset = expression.field.root.rsplit(".", 1)[0]
            fields[dataset].add(expression.field.root.rsplit(".", 1)[1])
        elif isinstance(expression, (MappedExpression, DateGrainExpression)):
            add(expression.source)

    for projection in plan.projections:
        if isinstance(projection.expression, AggregateExpression):
            add(projection.expression.source)
        else:
            add(projection.expression)
    for predicate in plan.filters:
        add(predicate.expression)
    for group_expression in plan.group_by:
        add(group_expression)
    for order in plan.order_by:
        add(order.expression)
    for join in plan.joins:
        add(join.on.left)
        add(join.on.right)
    return QueryPolicy(
        assets=tuple(
            AllowedAsset(dataset=dataset, columns=tuple(sorted(fields[dataset.root])))
            for dataset in datasets
        ),
        approved_join_contracts=tuple(join.contract for join in plan.joins),
        max_tables=limits.max_tables,
        max_preview_rows=limits.max_preview_rows,
        statement_timeout_ms=limits.statement_timeout_ms,
    )


def _build_advanced_query_policy(
    plan: AdvancedQueryPlan,
    limits: ResolutionLimits,
) -> QueryPolicy:
    datasets = [plan.root_scan.dataset]
    datasets.extend(join.right_scan.dataset for join in plan.joins)
    fields: dict[str, set[str]] = {dataset.root: set() for dataset in datasets}

    def add(expression: AdvancedValueExpression | QueryValueExpression) -> None:
        if isinstance(expression, ColumnExpression):
            dataset = expression.field.root.rsplit(".", 1)[0]
            fields[dataset].add(expression.field.root.rsplit(".", 1)[1])
        elif isinstance(expression, (MappedExpression, DateGrainExpression, BucketExpression)):
            add(expression.source)

    for projection in plan.projections:
        if isinstance(projection.expression, AdvancedAggregateExpression):
            if projection.expression.source is not None:
                add(projection.expression.source)
            if projection.expression.condition is not None:
                for predicate in _physical_plan_filter_leaves(projection.expression.condition):
                    add(predicate.expression)
        else:
            add(projection.expression)
    if plan.where is not None:
        for predicate in _physical_plan_filter_leaves(plan.where):
            add(predicate.expression)
    for group_expression in plan.group_by:
        add(group_expression)
    for join in plan.joins:
        add(join.on.left)
        add(join.on.right)
    return QueryPolicy(
        assets=tuple(
            AllowedAsset(dataset=dataset, columns=tuple(sorted(fields[dataset.root])))
            for dataset in datasets
        ),
        approved_join_contracts=tuple(join.contract for join in plan.joins),
        max_tables=limits.max_tables,
        max_preview_rows=limits.max_preview_rows,
        statement_timeout_ms=limits.statement_timeout_ms,
    )


def _physical_plan_filter_leaves(
    predicate: PhysicalBooleanPredicate,
) -> tuple[FilterPredicate, ...]:
    if predicate.kind is BooleanOperator.COMPARISON:
        assert predicate.comparison is not None
        return (predicate.comparison,)
    return tuple(
        item for operand in predicate.operands for item in _physical_plan_filter_leaves(operand)
    )


def _rejection_checks(edges: tuple[_PathEdge, ...]) -> tuple[RejectionCheck, ...]:
    checks: list[RejectionCheck] = []
    for edge in edges:
        for key in (edge.contract.left_key, edge.contract.right_key):
            if tuple(step.operation for step in key.transformation_plan.steps) == ("identity",):
                continue
            check = RejectionCheck(
                logical_field=key.logical_field,
                physical_field=key.physical_field,
                transformation_plan=key.transformation_plan,
            )
            if check not in checks:
                checks.append(check)
    return tuple(checks)


def _model_for_field(field: LogicalFieldRef) -> str:
    return field.root.split(".", 1)[0]


def _dataset(field: PhysicalFieldRef) -> str:
    parts = field.root.split(".")
    if len(parts) != 3:
        raise SemanticResolutionError(
            ResolutionErrorCode.UNSUPPORTED_REQUEST,
            "nested physical field paths are outside the PostgreSQL MVP",
        )
    return ".".join(parts[:2])


def resolved_semantic_plan_fingerprint(plan: ResolvedPlanLike) -> str:
    """Stable governed-plan identity independent of presentation and execution metadata."""

    encoded = json.dumps(
        plan.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
