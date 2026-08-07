"""Copy-first natural-language SQL orchestration for simple and advanced requests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import combinations

from schemabridge.application.connectors import ConnectorTargetError
from schemabridge.application.governed_execution import (
    semantic_plan_dependencies,
    semantic_registry_dependencies,
)
from schemabridge.application.ports.advanced_query_studio import (
    AdvancedInterpretationPort,
    AdvancedMentionExtractionPort,
    AdvancedQueryPreviewTokenPort,
    AdvancedSemanticRetrievalPort,
)
from schemabridge.application.ports.connectors import ExecutionTargetResolverPort
from schemabridge.application.ports.planning import GovernedSemanticRegistryPort
from schemabridge.application.ports.query_studio import (
    QueryStudioClockPort,
    QueryStudioNoncePort,
)
from schemabridge.application.query_execution import (
    QueryCompilerPort,
    SqlPolicyGuardPort,
    ValidatedQuery,
)
from schemabridge.application.semantic_change import (
    AssertSemanticContextCurrent,
    SemanticChangeError,
)
from schemabridge.application.sql_export import (
    BuildCopyableSql,
    CopyableSqlArtifact,
    CopyableSqlRendererPort,
)
from schemabridge.domain.advanced_query_studio import (
    AdvancedApprovedSemanticContext,
    AdvancedInterpretationAmbiguity,
    AdvancedInterpretationInput,
    AdvancedJoinReview,
    AdvancedMappingReview,
    AdvancedMentionExtractionInput,
    AdvancedNaturalLanguageInput,
    AdvancedPreviewTokenClaims,
    AdvancedQueryConfirmation,
    AdvancedQueryPreview,
    AdvancedQueryRoute,
    AdvancedSemanticField,
    AdvancedSemanticJoin,
    AdvancedSemanticModel,
    SignedAdvancedQueryPreviewToken,
    advanced_routed_request_fingerprint,
    confirm_advanced_query_preview,
)
from schemabridge.domain.advanced_requests import (
    AdvancedAnalyticalRequest,
    AdvancedMetricOperation,
    AdvancedQueryMode,
    BooleanOperator,
    GroupingMode,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.concepts import LogicalFieldRef
from schemabridge.domain.connectors import GovernedExecutionTarget
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.request_context import (
    ApprovedLogicalJoin,
    ValidatedRequestLike,
    approved_logical_context_fingerprint,
    validate_analytical_request,
    validated_analytical_request_fingerprint,
)
from schemabridge.domain.requests import (
    AnalyticalRequest,
    Dimension,
    Filter,
    Metric,
    MetricOperation,
    OrderBy,
)
from schemabridge.domain.resolution import (
    ResolutionLimits,
    ResolvedPlanLike,
    resolve_semantic_request,
    resolved_semantic_plan_fingerprint,
)
from schemabridge.domain.semantic_registry import (
    GovernedSemanticRegistrySnapshot,
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
    semantic_registry_scope_fingerprint,
)


class NaturalSqlErrorCode(StrEnum):
    AMBIGUOUS = "natural_sql_ambiguous"
    CONTEXT_OVERFLOW = "natural_sql_context_overflow"
    CONTEXT_INVALID = "natural_sql_context_invalid"
    INTERPRETATION_INVALID = "natural_sql_interpretation_invalid"
    CONFIRMATION_REQUIRED = "natural_sql_confirmation_required"
    CONFIRMATION_MISMATCH = "natural_sql_confirmation_mismatch"
    STALE_CONTEXT = "natural_sql_stale_context"
    FANOUT_MITIGATION_REQUIRED = "natural_sql_fanout_mitigation_required"
    TARGET_REQUIRED = "natural_sql_target_required"
    TARGET_UNAVAILABLE = "natural_sql_target_unavailable"
    TARGET_MISMATCH = "natural_sql_target_mismatch"
    CROSS_CONNECTION = "natural_sql_cross_connection"


class NaturalSqlError(RuntimeError):
    """Sanitized fail-closed error for the M32 copy-only workflow."""

    def __init__(self, code: NaturalSqlErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class NaturalSqlPreparation:
    """Text-free interpretation outcome; SQL does not exist at this stage."""

    request_digest: str
    semantic_context: AdvancedApprovedSemanticContext
    preview: AdvancedQueryPreview | None
    token: SignedAdvancedQueryPreviewToken | None
    ambiguities: tuple[AdvancedInterpretationAmbiguity, ...] = ()

    def __post_init__(self) -> None:
        confirmable = self.preview is not None and self.token is not None
        if confirmable == bool(self.ambiguities):
            raise ValueError(
                "natural SQL preparation requires a signed preview or explicit ambiguities"
            )
        if self.preview is not None and self.preview.request_digest != self.request_digest:
            raise ValueError("natural SQL preparation preview binds another request")
        if (
            self.preview is not None
            and self.preview.semantic_context_fingerprint != self.semantic_context.fingerprint
        ):
            raise ValueError("natural SQL preparation context differs from the signed preview")

    @property
    def can_confirm(self) -> bool:
        return self.preview is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "request_digest": self.request_digest,
            "semantic_context": self.semantic_context.model_dump(mode="json"),
            "preview": (self.preview.model_dump(mode="json") if self.preview is not None else None),
            "preview_token": self.token.root if self.token is not None else None,
            "ambiguities": [item.value for item in self.ambiguities],
            "sql": None,
            "compiled": False,
            "executed": False,
        }


@dataclass(frozen=True, slots=True)
class ConfirmedNaturalSqlRequest:
    preview: AdvancedQueryPreview
    validated_request: ValidatedRequestLike
    confirmation_fingerprint: str

    def __post_init__(self) -> None:
        if self.validated_request != self.preview.validated_request:
            raise ValueError("confirmed natural SQL request differs from its preview")
        if len(self.confirmation_fingerprint) != 64:
            raise ValueError("natural SQL confirmation fingerprint is invalid")


@dataclass(frozen=True, slots=True)
class GovernedCopyableSqlResult:
    """Standalone SQL plus its exact governed semantic lineage; never execution."""

    artifact: CopyableSqlArtifact
    resolved_plan: ResolvedPlanLike
    target: GovernedExecutionTarget | None = None

    def __post_init__(self) -> None:
        if (self.target is None) != (self.artifact.target_fingerprint is None):
            raise ValueError("copyable SQL target binding is incomplete")
        if self.target is not None and self.artifact.target_fingerprint != self.target.fingerprint:
            raise ValueError("copyable SQL artifact differs from its governed target")

    def as_dict(self) -> dict[str, object]:
        return {
            "dialect": self.artifact.dialect.value,
            "plan_version": self.artifact.plan_version,
            "request_fingerprint": self.artifact.request_fingerprint,
            "plan_fingerprint": self.artifact.plan_fingerprint,
            "sha256": self.artifact.sha256,
            "sql": self.artifact.sql,
            "executed": self.artifact.executed,
            "connection_id": (self.target.connection_id.root if self.target is not None else None),
            "target_route_revision": (
                self.target.route_revision if self.target is not None else None
            ),
            "target_fingerprint": self.artifact.target_fingerprint,
            "target_type_contract_fingerprint": (
                self.target.type_contract_fingerprint if self.target is not None else None
            ),
            "datasets": [
                self.resolved_plan.query_plan.root_scan.dataset.root,
                *(item.right_scan.dataset.root for item in self.resolved_plan.query_plan.joins),
            ],
            "join_contracts": [item.id for item in self.resolved_plan.selected_contracts],
        }


@dataclass(frozen=True, slots=True)
class OptionalNaturalSqlValidation:
    """Separately usable parameterized, guarded SQL; never the copy artifact."""

    query: ValidatedQuery
    resolved_plan: ResolvedPlanLike
    target: GovernedExecutionTarget | None = None

    def __post_init__(self) -> None:
        if (self.target is None) != (self.query.target_fingerprint is None):
            raise ValueError("optional SQL validation target binding is incomplete")
        if self.target is not None and self.query.target_fingerprint != self.target.fingerprint:
            raise ValueError("optional SQL validation differs from its governed target")


@dataclass(frozen=True, slots=True)
class PrepareNaturalSqlPreview:
    """Interpret against one complete registry and sign a text-free preview."""

    registry: GovernedSemanticRegistryPort
    mentions: AdvancedMentionExtractionPort
    retrieval: AdvancedSemanticRetrievalPort
    interpreter: AdvancedInterpretationPort
    preview_tokens: AdvancedQueryPreviewTokenPort
    clock: QueryStudioClockPort
    nonces: QueryStudioNoncePort
    limits: ResolutionLimits = field(default_factory=ResolutionLimits)
    target_resolver: ExecutionTargetResolverPort | None = None
    require_target_binding: bool = False
    semantic_gate: AssertSemanticContextCurrent | None = None
    semantic_scope: SemanticRegistryScope | None = None

    def __post_init__(self) -> None:
        _validate_semantic_target_configuration(
            target_resolver=self.target_resolver,
            require_target_binding=self.require_target_binding,
            semantic_gate=self.semantic_gate,
            semantic_scope=self.semantic_scope,
        )

    def execute(self, query: AdvancedNaturalLanguageInput) -> NaturalSqlPreparation:
        loaded = self.registry.load()
        managed = self.require_target_binding or self.target_resolver is not None
        if managed:
            _registry_target_identity(loaded, required=True)
        registry_connection_id = _assess_registry_current(
            loaded=loaded,
            semantic_gate=self.semantic_gate,
            semantic_scope=self.semantic_scope,
            required=managed,
        )
        preflight_target = _resolve_registry_target(
            loaded=loaded,
            target_resolver=self.target_resolver,
            require_target_binding=self.require_target_binding,
            expected_connection_id=registry_connection_id,
        )
        extraction_result = self.mentions.extract(AdvancedMentionExtractionInput(query=query))
        if extraction_result.input.query != query:
            raise NaturalSqlError(
                NaturalSqlErrorCode.INTERPRETATION_INVALID,
                "mention extraction returned another natural-language request",
            )
        retrieval = self.retrieval.retrieve(
            query=query,
            extraction=extraction_result.extraction,
            registry=loaded.registry,
        )
        if retrieval.registry_fingerprint != loaded.registry.fingerprint:
            raise NaturalSqlError(
                NaturalSqlErrorCode.STALE_CONTEXT,
                "semantic retrieval used another governed registry revision",
            )
        semantic_context = build_advanced_semantic_context(
            loaded,
            retrieval.logical_fields,
        )
        interpretation_input = AdvancedInterpretationInput(
            query=query,
            extraction=extraction_result.extraction,
            context=semantic_context,
        )
        interpretation = self.interpreter.interpret(interpretation_input)
        if interpretation.input != interpretation_input:
            raise NaturalSqlError(
                NaturalSqlErrorCode.INTERPRETATION_INVALID,
                "advanced interpretation returned another governed input",
            )
        envelope = interpretation.envelope
        if envelope.ambiguities:
            return NaturalSqlPreparation(
                request_digest=query.digest,
                semantic_context=semantic_context,
                preview=None,
                token=None,
                ambiguities=envelope.ambiguities,
            )
        if envelope.request is None:
            raise NaturalSqlError(
                NaturalSqlErrorCode.INTERPRETATION_INVALID,
                "advanced interpretation returned no typed request",
            )
        if envelope.request.grouping is GroupingMode.ROLLUP:
            raise NaturalSqlError(
                NaturalSqlErrorCode.INTERPRETATION_INVALID,
                "ROLLUP is not supported until subtotal NULL semantics are explicit",
            )

        route, routed = route_advanced_request(envelope.request)
        validation = validate_analytical_request(
            routed,
            loaded.registry.logical_context,
        )
        validated = validation.validated_request
        if validated is None:
            raise NaturalSqlError(
                NaturalSqlErrorCode.INTERPRETATION_INVALID,
                "typed interpretation is invalid against approved semantic context",
            )
        current = self.registry.load()
        if _registry_identity(current) != _registry_identity(loaded):
            raise NaturalSqlError(
                NaturalSqlErrorCode.STALE_CONTEXT,
                "governed context changed while the request was interpreted",
            )
        resolved = _resolve_current_request(validated, loaded, self.limits)
        if any(item.automatic for item in resolved.fanout_mitigations):
            raise NaturalSqlError(
                NaturalSqlErrorCode.FANOUT_MITIGATION_REQUIRED,
                (
                    "the request must explicitly select its fanout-safe metric "
                    "operation before confirmation"
                ),
            )
        plan_connection_id = _assess_plan_current(
            resolved=resolved,
            semantic_gate=self.semantic_gate,
            semantic_scope=self.semantic_scope,
            required=(self.require_target_binding or self.target_resolver is not None),
        )
        if registry_connection_id != plan_connection_id:
            raise NaturalSqlError(
                NaturalSqlErrorCode.STALE_CONTEXT,
                "governed semantic authority changed during interpretation",
            )
        target = _resolve_plan_target(
            loaded=loaded,
            resolved=resolved,
            target_resolver=self.target_resolver,
            require_target_binding=self.require_target_binding,
            expected_connection_id=plan_connection_id,
        )
        if preflight_target != target:
            raise NaturalSqlError(
                NaturalSqlErrorCode.TARGET_MISMATCH,
                "governed PostgreSQL target changed during interpretation",
            )
        resolved = _bind_execution_target(resolved, target)
        preview = AdvancedQueryPreview(
            request_digest=query.digest,
            mention_fingerprint=extraction_result.extraction.fingerprint,
            semantic_context_fingerprint=semantic_context.fingerprint,
            approved_context_fingerprint=approved_logical_context_fingerprint(
                loaded.registry.logical_context
            ),
            governed_registry_fingerprint=loaded.registry.fingerprint,
            scope_fingerprint=semantic_registry_scope_fingerprint(loaded.scope),
            activation_generation=loaded.activation_generation,
            active_pointer_fingerprint=loaded.active_pointer_fingerprint,
            connection_id=(target.connection_id if target is not None else None),
            target_route_revision=(target.route_revision if target is not None else None),
            target_fingerprint=(target.fingerprint if target is not None else None),
            target_type_contract_fingerprint=(
                target.type_contract_fingerprint if target is not None else None
            ),
            interpretation_fingerprint=envelope.fingerprint,
            resolved_plan_fingerprint=resolved_semantic_plan_fingerprint(resolved),
            datasets=_resolved_datasets(resolved),
            join_contract_ids=tuple(item.id for item in resolved.selected_contracts),
            mapping_reviews=tuple(
                AdvancedMappingReview(
                    logical_field=item.mapping.logical_field,
                    physical_field=item.mapping.physical_field,
                    confidence=item.mapping.confidence.root,
                    evidence=item.mapping.evidence[:8],
                    risks=item.mapping.risks[:8],
                )
                for item in resolved.selected_mappings
            ),
            join_reviews=tuple(
                AdvancedJoinReview(
                    contract_id=item.id,
                    evidence=item.evidence[:8],
                    risks=item.risks[:8],
                )
                for item in resolved.selected_contracts
            ),
            assumptions=resolved.assumptions,
            fanout_mitigations=resolved.fanout_mitigations,
            route=route,
            routed_request=routed,
            routed_request_fingerprint=advanced_routed_request_fingerprint(routed),
            validated_request=validated,
        )
        now = self.clock.now()
        claims = _preview_claims(
            preview,
            issued_at=now,
            expires_at=now + timedelta(minutes=10),
            nonce=self.nonces.new_nonce(),
        )
        return NaturalSqlPreparation(
            request_digest=query.digest,
            semantic_context=semantic_context,
            preview=preview,
            token=self.preview_tokens.issue(claims),
        )


@dataclass(frozen=True, slots=True)
class ConfirmNaturalSqlPreview:
    """Authenticate explicit confirmation and recheck current semantic bytes."""

    registry: GovernedSemanticRegistryPort
    preview_tokens: AdvancedQueryPreviewTokenPort
    clock: QueryStudioClockPort
    limits: ResolutionLimits = field(default_factory=ResolutionLimits)
    target_resolver: ExecutionTargetResolverPort | None = None
    require_target_binding: bool = False
    semantic_gate: AssertSemanticContextCurrent | None = None
    semantic_scope: SemanticRegistryScope | None = None

    def __post_init__(self) -> None:
        _validate_semantic_target_configuration(
            target_resolver=self.target_resolver,
            require_target_binding=self.require_target_binding,
            semantic_gate=self.semantic_gate,
            semantic_scope=self.semantic_scope,
        )

    def execute(
        self,
        preparation: NaturalSqlPreparation,
        confirmation: AdvancedQueryConfirmation,
    ) -> ConfirmedNaturalSqlRequest:
        if preparation.preview is None or preparation.token is None:
            raise NaturalSqlError(
                NaturalSqlErrorCode.CONFIRMATION_REQUIRED,
                "ambiguous natural SQL cannot be confirmed",
            )
        preview = preparation.preview
        if confirmation.token != preparation.token:
            raise NaturalSqlError(
                NaturalSqlErrorCode.CONFIRMATION_MISMATCH,
                "natural SQL confirmation token differs from the preview",
            )
        claims = self.preview_tokens.verify(confirmation.token, at=self.clock.now())
        if claims != _preview_claims(
            preview,
            issued_at=claims.issued_at,
            expires_at=claims.expires_at,
            nonce=claims.nonce,
        ):
            raise NaturalSqlError(
                NaturalSqlErrorCode.CONFIRMATION_MISMATCH,
                "natural SQL token claims differ from the preview",
            )
        try:
            validated = confirm_advanced_query_preview(preview, confirmation)
        except ValueError as error:
            raise NaturalSqlError(
                NaturalSqlErrorCode.CONFIRMATION_MISMATCH,
                "natural SQL confirmation differs from the preview",
            ) from error
        loaded = self.registry.load()
        _require_preview_registry(preview, loaded)
        current_validation = validate_analytical_request(
            preview.routed_request,
            loaded.registry.logical_context,
        ).validated_request
        if current_validation != validated:
            raise NaturalSqlError(
                NaturalSqlErrorCode.STALE_CONTEXT,
                "approved semantic context changed after preview",
            )
        resolved = _resolve_current_request(validated, loaded, self.limits)
        connection_id = _assess_plan_current(
            resolved=resolved,
            semantic_gate=self.semantic_gate,
            semantic_scope=self.semantic_scope,
            required=(self.require_target_binding or self.target_resolver is not None),
        )
        target = _resolve_plan_target(
            loaded=loaded,
            resolved=resolved,
            target_resolver=self.target_resolver,
            require_target_binding=self.require_target_binding,
            expected_connection_id=connection_id,
        )
        resolved = _bind_execution_target(resolved, target)
        _require_target_matches_preview(preview, target)
        _require_preview_resolution(preview, resolved)
        return ConfirmedNaturalSqlRequest(
            preview=preview,
            validated_request=validated,
            confirmation_fingerprint=confirmation.fingerprint,
        )


@dataclass(frozen=True, slots=True)
class GenerateGovernedCopyableSql:
    """Compile, guard, literalize, and re-guard without any source operation."""

    registry: GovernedSemanticRegistryPort
    compiler: QueryCompilerPort
    guard: SqlPolicyGuardPort
    renderer: CopyableSqlRendererPort
    limits: ResolutionLimits
    target_resolver: ExecutionTargetResolverPort | None = None
    require_target_binding: bool = False
    semantic_gate: AssertSemanticContextCurrent | None = None
    semantic_scope: SemanticRegistryScope | None = None

    def __post_init__(self) -> None:
        _validate_semantic_target_configuration(
            target_resolver=self.target_resolver,
            require_target_binding=self.require_target_binding,
            semantic_gate=self.semantic_gate,
            semantic_scope=self.semantic_scope,
        )

    def execute(
        self,
        confirmed: ConfirmedNaturalSqlRequest,
    ) -> GovernedCopyableSqlResult:
        validation = PrepareOptionalNaturalSqlValidation(
            registry=self.registry,
            compiler=self.compiler,
            guard=self.guard,
            limits=self.limits,
            target_resolver=self.target_resolver,
            require_target_binding=self.require_target_binding,
            semantic_gate=self.semantic_gate,
            semantic_scope=self.semantic_scope,
        ).execute(confirmed)
        resolved = validation.resolved_plan
        artifact = BuildCopyableSql(
            renderer=self.renderer,
            guard=self.guard,
            policy=resolved.query_policy,
        ).execute(
            validation.query,
            request_fingerprint=validated_analytical_request_fingerprint(
                confirmed.validated_request
            ),
            plan_fingerprint=resolved_semantic_plan_fingerprint(resolved),
            target=validation.target,
        )
        return GovernedCopyableSqlResult(
            artifact=artifact,
            resolved_plan=resolved,
            target=validation.target,
        )


@dataclass(frozen=True, slots=True)
class PrepareOptionalNaturalSqlValidation:
    """Build executor-form SQL only after confirmation; it does not execute it."""

    registry: GovernedSemanticRegistryPort
    compiler: QueryCompilerPort
    guard: SqlPolicyGuardPort
    limits: ResolutionLimits
    target_resolver: ExecutionTargetResolverPort | None = None
    require_target_binding: bool = False
    semantic_gate: AssertSemanticContextCurrent | None = None
    semantic_scope: SemanticRegistryScope | None = None

    def __post_init__(self) -> None:
        _validate_semantic_target_configuration(
            target_resolver=self.target_resolver,
            require_target_binding=self.require_target_binding,
            semantic_gate=self.semantic_gate,
            semantic_scope=self.semantic_scope,
        )

    def execute(
        self,
        confirmed: ConfirmedNaturalSqlRequest,
    ) -> OptionalNaturalSqlValidation:
        loaded = self.registry.load()
        _require_preview_registry(confirmed.preview, loaded)
        current_validation = validate_analytical_request(
            confirmed.preview.routed_request,
            loaded.registry.logical_context,
        ).validated_request
        if current_validation != confirmed.validated_request:
            raise NaturalSqlError(
                NaturalSqlErrorCode.STALE_CONTEXT,
                "approved semantic context changed before SQL generation",
            )
        resolved = _resolve_current_request(
            confirmed.validated_request,
            loaded,
            self.limits,
        )
        connection_id = _assess_plan_current(
            resolved=resolved,
            semantic_gate=self.semantic_gate,
            semantic_scope=self.semantic_scope,
            required=(self.require_target_binding or self.target_resolver is not None),
        )
        target = _resolve_plan_target(
            loaded=loaded,
            resolved=resolved,
            target_resolver=self.target_resolver,
            require_target_binding=self.require_target_binding,
            expected_connection_id=connection_id,
        )
        resolved = _bind_execution_target(resolved, target)
        _require_target_matches_preview(confirmed.preview, target)
        _require_preview_resolution(confirmed.preview, resolved)
        compiled = self.compiler.compile(
            resolved.query_plan,
            max_preview_rows=resolved.query_policy.max_preview_rows,
            target=target,
        )
        guarded = self.guard.validate(compiled, resolved.query_policy, target=target)
        return OptionalNaturalSqlValidation(
            query=guarded,
            resolved_plan=resolved,
            target=target,
        )


def route_advanced_request(
    request: AdvancedAnalyticalRequest,
) -> tuple[AdvancedQueryRoute, AnalyticalRequest | AdvancedAnalyticalRequest]:
    """Select v1 only when every requested semantic is exactly representable."""

    simple = _as_version_one(request)
    if simple is not None:
        return AdvancedQueryRoute.V1, simple
    return AdvancedQueryRoute.V2, request


def build_advanced_semantic_context(
    loaded: ScopedSemanticRegistrySnapshot,
    logical_fields: tuple[LogicalFieldRef, ...],
) -> AdvancedApprovedSemanticContext:
    """Verify retrieval against the full registry and build one connected 3/12/2 slice."""

    registry = loaded.registry
    logical = registry.logical_context
    field_index = logical.field_index()
    selected_ids = tuple(item.root for item in logical_fields)
    if not selected_ids or len(selected_ids) > 12 or len(selected_ids) != len(set(selected_ids)):
        raise NaturalSqlError(
            NaturalSqlErrorCode.CONTEXT_OVERFLOW,
            "semantic retrieval did not return a unique one-to-twelve-field closure",
        )
    if any(item not in field_index for item in selected_ids):
        raise NaturalSqlError(
            NaturalSqlErrorCode.CONTEXT_INVALID,
            "semantic retrieval returned a field outside the governed registry",
        )
    selected_models = tuple(dict.fromkeys(item.split(".", 1)[0] for item in selected_ids))
    join_summaries, all_models = _connected_join_closure(registry, selected_models)
    selected = list(selected_ids)
    for model in all_models:
        if any(item.startswith(f"{model}.") for item in selected):
            continue
        join_key = _intermediate_join_key(registry, model, join_summaries)
        if join_key is None:
            raise NaturalSqlError(
                NaturalSqlErrorCode.CONTEXT_INVALID,
                "connected semantic context lacks an approved intermediate key",
            )
        selected.append(join_key.root)
    if len(selected) > 12:
        raise NaturalSqlError(
            NaturalSqlErrorCode.CONTEXT_OVERFLOW,
            "connected semantic context exceeds twelve fields",
        )

    model_index = logical.model_index()
    try:
        return AdvancedApprovedSemanticContext(
            context_source=logical.source,
            context_version=logical.version,
            approved_context_fingerprint=approved_logical_context_fingerprint(logical),
            governed_registry_fingerprint=registry.fingerprint,
            scope_fingerprint=semantic_registry_scope_fingerprint(loaded.scope),
            models=tuple(
                AdvancedSemanticModel(
                    id=model_index[model].id,
                    definition=model_index[model].description,
                )
                for model in all_models
            ),
            fields=tuple(
                AdvancedSemanticField(
                    id=field_index[field_id].id,
                    canonical_type=field_index[field_id].canonical_type,
                    role=field_index[field_id].role,
                    definition=field_index[field_id].definition,
                    allowed_values=field_index[field_id].allowed_values,
                )
                for field_id in selected
            ),
            joins=tuple(
                AdvancedSemanticJoin(
                    id=join.id,
                    left_model=join.left_model,
                    right_model=join.right_model,
                    cardinality=join.cardinality,
                    fanout_policy=join.fanout_policy,
                )
                for join in join_summaries
            ),
        )
    except ValueError as error:
        raise NaturalSqlError(
            NaturalSqlErrorCode.CONTEXT_OVERFLOW,
            "governed semantic context exceeds the bounded provider contract",
        ) from error


def _as_version_one(request: AdvancedAnalyticalRequest) -> AnalyticalRequest | None:
    if (
        request.mode is not AdvancedQueryMode.AGGREGATE
        or request.grouping is not GroupingMode.STANDARD
        or request.having is not None
        or request.windows
        or request.post_filter is not None
        or any(item.buckets for item in request.fields)
        or any(
            item.alias is not None and item.alias != item.field.root.rsplit(".", 1)[-1]
            for item in request.fields
        )
        or any(
            item.condition is not None
            or item.operation is AdvancedMetricOperation.COUNT_ROWS
            or item.field is None
            for item in request.metrics
        )
    ):
        return None
    filters = _flat_and_filters(request)
    if filters is None:
        return None
    dimensions = tuple(Dimension(field=item.field, grain=item.grain) for item in request.fields)
    dimension_by_alias = {
        item.alias or item.field.root.rsplit(".", 1)[-1]: item.field for item in request.fields
    }
    if any(item.alias not in dimension_by_alias for item in request.result_order_by):
        return None
    metrics = tuple(
        Metric(
            operation=MetricOperation(item.operation.value),
            field=item.field,
            alias=item.alias,
        )
        for item in request.metrics
        if item.field is not None
    )
    try:
        return AnalyticalRequest(
            primary_entity=request.primary_entity,
            dimensions=dimensions,
            metrics=metrics,
            filters=filters,
            order_by=tuple(
                OrderBy(
                    field=dimension_by_alias[item.alias],
                    direction=item.direction,
                )
                for item in request.result_order_by
            ),
            limit=request.limit,
        )
    except ValueError:
        return None


def _flat_and_filters(
    request: AdvancedAnalyticalRequest,
) -> tuple[Filter, ...] | None:
    predicate = request.where
    if predicate is None:
        return ()
    if predicate.kind is BooleanOperator.COMPARISON:
        assert predicate.comparison is not None
        return (predicate.comparison,)
    if predicate.kind is not BooleanOperator.AND or any(
        item.kind is not BooleanOperator.COMPARISON for item in predicate.operands
    ):
        return None
    return tuple(item.comparison for item in predicate.operands if item.comparison is not None)


def _connected_join_closure(
    registry: GovernedSemanticRegistrySnapshot,
    selected_models: tuple[str, ...],
) -> tuple[tuple[ApprovedLogicalJoin, ...], tuple[str, ...]]:
    if not selected_models or len(selected_models) > 3:
        raise NaturalSqlError(
            NaturalSqlErrorCode.CONTEXT_OVERFLOW,
            "semantic context requires between one and three models",
        )
    if len(selected_models) == 1:
        return (), selected_models
    joins = registry.logical_context.joins
    candidates: list[tuple[tuple[ApprovedLogicalJoin, ...], tuple[str, ...]]] = []
    for size in (1, 2):
        for subset in combinations(joins, size):
            vertices = tuple(
                dict.fromkeys(
                    endpoint
                    for join in subset
                    for endpoint in (join.left_model.root, join.right_model.root)
                )
            )
            if not set(selected_models) <= set(vertices) or len(vertices) > 3:
                continue
            if _join_graph_connected(subset, vertices):
                candidates.append((subset, vertices))
        if candidates:
            break
    if not candidates:
        raise NaturalSqlError(
            NaturalSqlErrorCode.CONTEXT_OVERFLOW,
            "semantic request has no connected path within two approved joins",
        )
    signatures = {
        tuple(sorted(item.id for item in subset)): (subset, vertices)
        for subset, vertices in candidates
    }
    if len(signatures) != 1:
        raise NaturalSqlError(
            NaturalSqlErrorCode.AMBIGUOUS,
            "semantic request has multiple equally short approved join paths",
        )
    subset, vertices = next(iter(signatures.values()))
    ordered_models = tuple(
        dict.fromkeys(
            (
                *selected_models,
                *(model for model in vertices if model not in selected_models),
            )
        )
    )
    return tuple(subset), ordered_models


def _join_graph_connected(
    subset: tuple[ApprovedLogicalJoin, ...],
    vertices: tuple[str, ...],
) -> bool:
    if not vertices:
        return False
    reached = {vertices[0]}
    pending = [vertices[0]]
    while pending:
        current = pending.pop()
        for item in subset:
            left = item.left_model.root
            right = item.right_model.root
            if current == left and right not in reached:
                reached.add(right)
                pending.append(right)
            elif current == right and left not in reached:
                reached.add(left)
                pending.append(left)
    return reached == set(vertices)


def _intermediate_join_key(
    registry: GovernedSemanticRegistrySnapshot,
    model: str,
    joins: tuple[ApprovedLogicalJoin, ...],
) -> LogicalFieldRef | None:
    contract_index = {item.id: item for item in registry.join_contracts.contracts}
    for summary in joins:
        contract = contract_index.get(summary.id)
        if contract is None:
            continue
        for key in (contract.left_key, contract.right_key):
            if key.logical_field.root.startswith(f"{model}."):
                return key.logical_field
    return None


def _preview_claims(
    preview: AdvancedQueryPreview,
    *,
    issued_at: datetime,
    expires_at: datetime,
    nonce: str,
) -> AdvancedPreviewTokenClaims:
    return AdvancedPreviewTokenClaims(
        request_digest=preview.request_digest,
        mention_fingerprint=preview.mention_fingerprint,
        semantic_context_fingerprint=preview.semantic_context_fingerprint,
        approved_context_fingerprint=preview.approved_context_fingerprint,
        governed_registry_fingerprint=preview.governed_registry_fingerprint,
        scope_fingerprint=preview.scope_fingerprint,
        activation_generation=preview.activation_generation,
        active_pointer_fingerprint=preview.active_pointer_fingerprint,
        connection_id=preview.connection_id,
        target_route_revision=preview.target_route_revision,
        target_fingerprint=preview.target_fingerprint,
        target_type_contract_fingerprint=preview.target_type_contract_fingerprint,
        interpretation_fingerprint=preview.interpretation_fingerprint,
        resolved_plan_fingerprint=preview.resolved_plan_fingerprint,
        routed_request_fingerprint=preview.routed_request_fingerprint,
        preview_fingerprint=preview.fingerprint,
        issued_at=issued_at,
        expires_at=expires_at,
        nonce=nonce,
    )


def _require_preview_registry(
    preview: AdvancedQueryPreview,
    loaded: ScopedSemanticRegistrySnapshot,
) -> None:
    if (
        preview.governed_registry_fingerprint != loaded.registry.fingerprint
        or preview.scope_fingerprint != semantic_registry_scope_fingerprint(loaded.scope)
        or preview.activation_generation != loaded.activation_generation
        or preview.active_pointer_fingerprint != loaded.active_pointer_fingerprint
        or preview.approved_context_fingerprint
        != approved_logical_context_fingerprint(loaded.registry.logical_context)
    ):
        raise NaturalSqlError(
            NaturalSqlErrorCode.STALE_CONTEXT,
            "governed registry changed after natural SQL preview",
        )


def _resolve_current_request(
    validated: ValidatedRequestLike,
    loaded: ScopedSemanticRegistrySnapshot,
    limits: ResolutionLimits,
) -> ResolvedPlanLike:
    resolved = resolve_semantic_request(
        validated,
        loaded.registry,
        limits,
    )
    if loaded.activation_generation is None:
        return resolved
    assert loaded.active_pointer_fingerprint is not None
    return resolved.model_copy(
        update={
            "activation_generation": loaded.activation_generation,
            "active_pointer_fingerprint": loaded.active_pointer_fingerprint,
            "active_scope_fingerprint": semantic_registry_scope_fingerprint(loaded.scope),
        }
    )


def _validate_semantic_target_configuration(
    *,
    target_resolver: ExecutionTargetResolverPort | None,
    require_target_binding: bool,
    semantic_gate: AssertSemanticContextCurrent | None,
    semantic_scope: SemanticRegistryScope | None,
) -> None:
    if (semantic_gate is None) != (semantic_scope is None):
        raise ValueError("natural SQL semantic gate and scope must be configured together")
    if (require_target_binding or target_resolver is not None) and semantic_gate is None:
        raise ValueError("managed natural SQL target binding requires the M26 semantic gate")


def _assess_registry_current(
    *,
    loaded: ScopedSemanticRegistrySnapshot,
    semantic_gate: AssertSemanticContextCurrent | None,
    semantic_scope: SemanticRegistryScope | None,
    required: bool,
) -> CatalogConnectionId | None:
    if semantic_gate is None:
        if required:
            raise NaturalSqlError(
                NaturalSqlErrorCode.STALE_CONTEXT,
                "managed natural SQL semantic authority is unavailable",
            )
        return None
    if semantic_scope != loaded.scope:
        raise NaturalSqlError(
            NaturalSqlErrorCode.STALE_CONTEXT,
            "managed natural SQL semantic scope changed",
        )
    try:
        assessment = semantic_gate.execute(semantic_registry_dependencies(loaded))
    except (SemanticChangeError, TypeError, ValueError) as error:
        raise NaturalSqlError(
            NaturalSqlErrorCode.STALE_CONTEXT,
            "managed natural SQL semantic context is unavailable or stale",
        ) from error
    return assessment.connection_id


def _assess_plan_current(
    *,
    resolved: ResolvedPlanLike,
    semantic_gate: AssertSemanticContextCurrent | None,
    semantic_scope: SemanticRegistryScope | None,
    required: bool,
) -> CatalogConnectionId | None:
    if semantic_gate is None:
        if required:
            raise NaturalSqlError(
                NaturalSqlErrorCode.STALE_CONTEXT,
                "managed natural SQL semantic authority is unavailable",
            )
        return None
    assert semantic_scope is not None
    if resolved.active_scope_fingerprint != semantic_registry_scope_fingerprint(semantic_scope):
        raise NaturalSqlError(
            NaturalSqlErrorCode.STALE_CONTEXT,
            "managed natural SQL semantic scope changed",
        )
    try:
        assessment = semantic_gate.execute(semantic_plan_dependencies(resolved, semantic_scope))
    except (SemanticChangeError, TypeError, ValueError) as error:
        raise NaturalSqlError(
            NaturalSqlErrorCode.STALE_CONTEXT,
            "managed natural SQL semantic context is unavailable or stale",
        ) from error
    return assessment.connection_id


def _bind_execution_target(
    resolved: ResolvedPlanLike,
    target: GovernedExecutionTarget | None,
) -> ResolvedPlanLike:
    if target is None:
        return resolved
    return resolved.model_copy(update={"execution_target": target})


def _resolve_plan_target(
    *,
    loaded: ScopedSemanticRegistrySnapshot,
    resolved: ResolvedPlanLike,
    target_resolver: ExecutionTargetResolverPort | None,
    require_target_binding: bool,
    expected_connection_id: CatalogConnectionId | None,
) -> GovernedExecutionTarget | None:
    """Resolve the one current target selected by exact registry-v2 bindings."""

    required = require_target_binding or target_resolver is not None
    identity = _registry_target_identity(loaded, required=required)
    if identity is None:
        return None
    workspace_id, connection_id = identity
    if expected_connection_id is not None and expected_connection_id != connection_id:
        raise NaturalSqlError(
            NaturalSqlErrorCode.CROSS_CONNECTION,
            "semantic evidence and registry select different source connections",
        )
    registry = loaded.registry
    selected_mapping_ids = {
        (
            item.mapping.logical_field.root,
            item.mapping.physical_field.root,
        )
        for item in resolved.selected_mappings
    }
    bindings = tuple(
        item for item in registry.physical_bindings if item.mapping_identity in selected_mapping_ids
    )
    if {item.mapping_identity for item in bindings} != selected_mapping_ids:
        raise NaturalSqlError(
            NaturalSqlErrorCode.TARGET_REQUIRED,
            "resolved mappings lack exact registry-v2 physical authority",
        )
    if {item.workspace_id for item in bindings} != {workspace_id} or {
        item.connection_id for item in bindings
    } != {connection_id}:
        raise NaturalSqlError(
            NaturalSqlErrorCode.CROSS_CONNECTION,
            "natural SQL cannot span governed source connections",
        )
    return _resolve_identity_target(
        target_resolver=target_resolver,
        workspace_id=workspace_id,
        connection_id=connection_id,
        required=required,
    )


def _resolve_registry_target(
    *,
    loaded: ScopedSemanticRegistrySnapshot,
    target_resolver: ExecutionTargetResolverPort | None,
    require_target_binding: bool,
    expected_connection_id: CatalogConnectionId | None,
) -> GovernedExecutionTarget | None:
    """Fail before provider access unless the managed registry target is current."""

    required = require_target_binding or target_resolver is not None
    identity = _registry_target_identity(loaded, required=required)
    if identity is None:
        return None
    if expected_connection_id is not None and expected_connection_id != identity[1]:
        raise NaturalSqlError(
            NaturalSqlErrorCode.CROSS_CONNECTION,
            "semantic evidence and registry select different source connections",
        )
    return _resolve_identity_target(
        target_resolver=target_resolver,
        workspace_id=identity[0],
        connection_id=identity[1],
        required=required,
    )


def _registry_target_identity(
    loaded: ScopedSemanticRegistrySnapshot,
    *,
    required: bool,
) -> tuple[str, CatalogConnectionId] | None:
    registry = loaded.registry
    if registry.format_version != 2 or not registry.physical_bindings:
        if required:
            raise NaturalSqlError(
                NaturalSqlErrorCode.TARGET_REQUIRED,
                "managed natural SQL requires registry-v2 physical authority",
            )
        return None
    workspaces = {item.workspace_id for item in registry.physical_bindings}
    connections = {item.connection_id for item in registry.physical_bindings}
    if len(workspaces) != 1 or len(connections) != 1:
        raise NaturalSqlError(
            NaturalSqlErrorCode.CROSS_CONNECTION,
            "natural SQL registry cannot span governed source connections",
        )
    workspace_id = next(iter(workspaces))
    connection_id = next(iter(connections))
    if workspace_id != loaded.scope.workspace_id:
        raise NaturalSqlError(
            NaturalSqlErrorCode.TARGET_MISMATCH,
            "registry physical authority belongs to another workspace",
        )
    return workspace_id, connection_id


def _resolve_identity_target(
    *,
    target_resolver: ExecutionTargetResolverPort | None,
    workspace_id: str,
    connection_id: CatalogConnectionId,
    required: bool,
) -> GovernedExecutionTarget | None:
    if target_resolver is None:
        if required:
            raise NaturalSqlError(
                NaturalSqlErrorCode.TARGET_REQUIRED,
                "managed natural SQL target resolver is unavailable",
            )
        return None
    target = _load_current_target(
        target_resolver,
        workspace_id=workspace_id,
        connection_id=connection_id,
    )
    if target.workspace_id != workspace_id or target.connection_id != connection_id:
        raise NaturalSqlError(
            NaturalSqlErrorCode.TARGET_MISMATCH,
            "current target differs from the registry physical authority",
        )
    return target


def _load_current_target(
    target_resolver: ExecutionTargetResolverPort,
    *,
    workspace_id: str,
    connection_id: CatalogConnectionId,
) -> GovernedExecutionTarget:
    try:
        target = target_resolver.resolve_current(
            workspace_id=workspace_id,
            connection_id=connection_id,
        )
    except ConnectorTargetError as error:
        raise NaturalSqlError(
            NaturalSqlErrorCode.TARGET_UNAVAILABLE,
            "current governed PostgreSQL target is unavailable",
        ) from error
    except Exception as error:
        raise NaturalSqlError(
            NaturalSqlErrorCode.TARGET_UNAVAILABLE,
            "current governed PostgreSQL target is unavailable",
        ) from error
    if type(target) is not GovernedExecutionTarget:
        raise NaturalSqlError(
            NaturalSqlErrorCode.TARGET_UNAVAILABLE,
            "current governed PostgreSQL target is invalid",
        )
    return target


def _require_target_matches_preview(
    preview: AdvancedQueryPreview,
    target: GovernedExecutionTarget | None,
) -> None:
    expected = (
        preview.connection_id,
        preview.target_route_revision,
        preview.target_fingerprint,
        preview.target_type_contract_fingerprint,
    )
    observed = (
        target.connection_id if target is not None else None,
        target.route_revision if target is not None else None,
        target.fingerprint if target is not None else None,
        target.type_contract_fingerprint if target is not None else None,
    )
    if expected != observed:
        raise NaturalSqlError(
            NaturalSqlErrorCode.TARGET_MISMATCH,
            "governed PostgreSQL target changed after natural SQL preview",
        )


def _resolved_datasets(
    resolved: ResolvedPlanLike,
) -> tuple[PhysicalDatasetRef, ...]:
    return (
        resolved.query_plan.root_scan.dataset,
        *(item.right_scan.dataset for item in resolved.query_plan.joins),
    )


def _require_preview_resolution(
    preview: AdvancedQueryPreview,
    resolved: ResolvedPlanLike,
) -> None:
    if (
        preview.resolved_plan_fingerprint != resolved_semantic_plan_fingerprint(resolved)
        or preview.datasets != _resolved_datasets(resolved)
        or preview.join_contract_ids != tuple(item.id for item in resolved.selected_contracts)
        or preview.assumptions != resolved.assumptions
        or preview.fanout_mitigations != resolved.fanout_mitigations
        or any(item.automatic for item in resolved.fanout_mitigations)
    ):
        raise NaturalSqlError(
            NaturalSqlErrorCode.STALE_CONTEXT,
            "resolved semantic plan changed after natural SQL preview",
        )


def _registry_identity(
    loaded: ScopedSemanticRegistrySnapshot,
) -> tuple[str, str, int | None, str | None]:
    return (
        semantic_registry_scope_fingerprint(loaded.scope),
        loaded.registry.fingerprint,
        loaded.activation_generation,
        loaded.active_pointer_fingerprint,
    )


__all__ = [
    "ConfirmNaturalSqlPreview",
    "ConfirmedNaturalSqlRequest",
    "GenerateGovernedCopyableSql",
    "GovernedCopyableSqlResult",
    "NaturalSqlError",
    "NaturalSqlErrorCode",
    "NaturalSqlPreparation",
    "OptionalNaturalSqlValidation",
    "PrepareNaturalSqlPreview",
    "PrepareOptionalNaturalSqlValidation",
    "build_advanced_semantic_context",
    "route_advanced_request",
]
