"""Approval-gated query-recipe preparation, publication, and compatibility checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict

from schemabridge.application.governed_execution import semantic_plan_dependencies
from schemabridge.application.ports.publication_audit import (
    PublicationAuditStoreError,
    PublicationAuditStorePort,
)
from schemabridge.application.ports.recipes import (
    QueryRecipeReadPort,
    QueryRecipeRepositoryPort,
    RecipeError,
    RecipeErrorCode,
)
from schemabridge.application.ports.registry_control import (
    ActiveRegistryPointerReadPort,
    RegistryControlError,
)
from schemabridge.application.ports.workflows import (
    WorkflowDraftStorePort,
    WorkflowError,
)
from schemabridge.application.semantic_change import (
    AssertSemanticContextCurrent,
    SemanticChangeError,
)
from schemabridge.domain.publication_audit import validate_publication_audit_binding
from schemabridge.domain.recipes import (
    PublishedQueryRecipe,
    QueryRecipe,
    RecipeJoinVersion,
    RecipeMappingVersion,
    RecipeMigrationProposal,
    RecipeMigrationProvenance,
    RecipeModelVersion,
    RecipePublicationApproval,
    RecipePublicationResult,
    RecipeRegistryBinding,
    RecipeReuseAssessment,
    RecipeReuseStatus,
    RecipeStalenessCode,
    RecipeValidationSummary,
    assert_recipe_sha256,
    fingerprint_recipe_payload,
    published_recipe_snapshot_fingerprint,
    query_recipe_payload_fingerprint,
)
from schemabridge.domain.registry_control import (
    ActiveRegistryPointer,
    registry_projection_fingerprint,
)
from schemabridge.domain.request_context import (
    ValidatedAnalyticalRequest,
    validated_analytical_request_fingerprint,
)
from schemabridge.domain.requests import AnalyticalRequest
from schemabridge.domain.resolution import (
    ResolvedSemanticPlan,
    resolved_semantic_plan_fingerprint,
)
from schemabridge.domain.semantic_registry import (
    SemanticRegistryScope,
    semantic_registry_scope_fingerprint,
)
from schemabridge.domain.workflows import AgentWorkflowDraft, WorkflowOperation, WorkflowTraceStatus

RECIPE_COMPILER_VERSION = "postgres-ir-v1"
DEFAULT_RECIPE_LIMITATIONS = (
    "Synthetic SchemaBridge demo assets only; not production evidence.",
    "Reuse never bypasses current semantic planning, SQL policy validation, or bounded preview.",
    "Validated for the recorded model, mapping, join, source-schema, and compiler versions only.",
)


class _RecipeValues(TypedDict):
    business_question: str
    normalized_intent: AnalyticalRequest
    validated_request_fingerprint: str
    model_version: RecipeModelVersion
    registry_binding: RecipeRegistryBinding | None
    mapping_versions: tuple[RecipeMappingVersion, ...]
    join_versions: tuple[RecipeJoinVersion, ...]
    source_schema_fingerprint: str
    plan_fingerprint: str
    query_fingerprint: str
    compiler_version: str
    validation: RecipeValidationSummary
    limitations: tuple[str, ...]
    linked_asset_urns: tuple[str, ...]
    source_workflow_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PrepareQueryRecipe:
    repository: QueryRecipeReadPort
    compiler_version: str = RECIPE_COMPILER_VERSION

    def execute(self, draft: AgentWorkflowDraft) -> QueryRecipe:
        _require_completed_execution(draft)
        values = _recipe_values(draft, self.compiler_version)
        candidate = QueryRecipe.create(version=1, **values)
        current = self.repository.find_current(
            candidate.intent_fingerprint,
            scope_fingerprint=_recipe_scope_fingerprint(candidate),
        )
        if current is None:
            return candidate
        if (
            current.recipe.content_fingerprint == candidate.content_fingerprint
            and current.recipe.source_workflow_id == candidate.source_workflow_id
        ):
            return current.recipe
        return QueryRecipe.create(version=current.recipe.version + 1, **values)


@dataclass(frozen=True, slots=True)
class PrepareStoredQueryRecipe:
    store: WorkflowDraftStorePort
    prepare: PrepareQueryRecipe

    def execute(self, workflow_id: str) -> QueryRecipe:
        draft = self.store.load(workflow_id)
        if draft is None:
            raise RecipeError(
                RecipeErrorCode.NOT_FOUND,
                "workflow draft for query-recipe preparation was not found",
            )
        return self.prepare.execute(draft)


@dataclass(frozen=True, slots=True)
class PrepareStoredStaleQueryRecipeMigration:
    """Load exact durable migration inputs before deterministic preparation."""

    store: WorkflowDraftStorePort
    repository: QueryRecipeReadPort
    pointers: ActiveRegistryPointerReadPort
    scope: SemanticRegistryScope
    compiler_version: str = RECIPE_COMPILER_VERSION

    def execute(
        self,
        *,
        workflow_id: str,
        intent_fingerprint: str,
    ) -> RecipeMigrationProposal:
        assert_recipe_sha256(intent_fingerprint)
        try:
            draft = self.store.load(workflow_id)
        except WorkflowError as error:
            raise RecipeError(
                RecipeErrorCode.WORKFLOW_STORE_UNAVAILABLE,
                "workflow draft storage is unavailable",
            ) from error
        if draft is None:
            raise RecipeError(
                RecipeErrorCode.NOT_FOUND,
                "workflow draft for recipe migration was not found",
            )
        try:
            active_pointer = self.pointers.load_active(self.scope)
        except RegistryControlError as error:
            raise RecipeError(
                RecipeErrorCode.REGISTRY_CONTROL_UNAVAILABLE,
                "active registry control state is unavailable",
            ) from error
        if active_pointer is None:
            raise RecipeError(
                RecipeErrorCode.ACTIVE_POINTER_NOT_FOUND,
                "active registry pointer for recipe migration was not found",
            )
        if active_pointer.scope != self.scope:
            raise RecipeError(
                RecipeErrorCode.REGISTRY_CONTROL_UNAVAILABLE,
                "active registry control state is unavailable",
            )
        historical = self.repository.find_current(
            intent_fingerprint,
            scope_fingerprint=semantic_registry_scope_fingerprint(self.scope),
        )
        if historical is None:
            raise RecipeError(
                RecipeErrorCode.NOT_FOUND,
                "current query recipe for migration was not found",
            )
        return PrepareStaleQueryRecipeMigration(
            self.repository,
            compiler_version=self.compiler_version,
        ).execute(
            historical=historical,
            draft=draft,
            active_pointer=active_pointer,
        )


@dataclass(frozen=True, slots=True)
class PrepareStaleQueryRecipeMigration:
    """Prepare a new recipe only from a completed workflow on the active pointer."""

    repository: QueryRecipeReadPort
    compiler_version: str = RECIPE_COMPILER_VERSION

    def execute(
        self,
        *,
        historical: PublishedQueryRecipe,
        draft: AgentWorkflowDraft,
        active_pointer: ActiveRegistryPointer,
    ) -> RecipeMigrationProposal:
        _require_completed_execution(draft)
        _require_active_registry_binding(draft, active_pointer)
        current = self.repository.find_current(
            historical.recipe.intent_fingerprint,
            scope_fingerprint=semantic_registry_scope_fingerprint(active_pointer.scope),
        )
        if current is None:
            raise RecipeError(
                RecipeErrorCode.NOT_FOUND,
                "historical query recipe is no longer current",
            )
        if not _same_published_snapshot(current, historical):
            raise RecipeError(
                RecipeErrorCode.CURRENT_CHANGED,
                "historical query recipe changed; restart migration review",
            )
        if draft.id == historical.recipe.source_workflow_id:
            raise RecipeError(
                RecipeErrorCode.INVALID_WORKFLOW,
                "recipe migration requires a newly completed workflow",
            )
        assert draft.validated_request is not None
        assert draft.resolved_plan is not None
        assert draft.plan_fingerprint is not None
        assert draft.query_fingerprint is not None
        intent_fingerprint = fingerprint_recipe_payload(
            draft.validated_request.request.model_dump(mode="json")
        )
        if intent_fingerprint != historical.recipe.intent_fingerprint:
            raise RecipeError(
                RecipeErrorCode.INTENT_MISMATCH,
                "migration workflow intent does not match the historical query recipe",
            )
        assessment = _assess_published_recipe(
            current,
            validated_request=draft.validated_request,
            resolved_plan=draft.resolved_plan,
            plan_fingerprint=draft.plan_fingerprint,
            query_fingerprint=draft.query_fingerprint,
            source_schema_fingerprint=source_schema_fingerprint(draft),
            compiler_version=self.compiler_version,
        )
        if assessment.status is not RecipeReuseStatus.STALE:
            raise RecipeError(
                RecipeErrorCode.MIGRATION_NOT_REQUIRED,
                "current query recipe is compatible with the active registry",
            )
        provenance = RecipeMigrationProvenance(
            previous_recipe_id=current.recipe.id,
            previous_recipe_version=current.recipe.version,
            previous_recipe_fingerprint=current.recipe.fingerprint,
            previous_recipe_payload_fingerprint=query_recipe_payload_fingerprint(current.recipe),
            previous_source_workflow_id=current.recipe.source_workflow_id,
            previous_versioned_document_urn=current.versioned_document_urn,
            staleness_reasons=assessment.reasons,
        )
        replacement = QueryRecipe.create(
            version=current.recipe.version + 1,
            migration_provenance=provenance,
            **_recipe_values(draft, self.compiler_version),
        )
        return RecipeMigrationProposal.create(
            historical=current,
            assessment=assessment,
            replacement=replacement,
        )


@dataclass(frozen=True, slots=True)
class PublishStaleQueryRecipeMigration:
    """Recheck immutable history, then require the existing exact publication approval."""

    repository: QueryRecipeRepositoryPort
    audit_store: PublicationAuditStorePort

    def execute(
        self,
        proposal: RecipeMigrationProposal,
        approval: RecipePublicationApproval,
    ) -> RecipePublicationResult:
        _validate_approval(proposal.replacement, approval)
        current = self.repository.find_current(
            proposal.historical.recipe.intent_fingerprint,
            scope_fingerprint=_recipe_scope_fingerprint(proposal.historical.recipe),
        )
        if (
            current is None
            or not _same_published_snapshot(current, proposal.historical)
            or query_recipe_payload_fingerprint(current.recipe)
            != proposal.historical_payload_fingerprint
            or published_recipe_snapshot_fingerprint(current)
            != proposal.historical_snapshot_fingerprint
        ):
            raise RecipeError(
                RecipeErrorCode.CURRENT_CHANGED,
                "historical query recipe changed; prepare and approve a new migration",
            )
        return PublishQueryRecipe(self.repository, self.audit_store).execute(
            proposal.replacement,
            approval,
        )


@dataclass(frozen=True, slots=True)
class PublishQueryRecipe:
    repository: QueryRecipeRepositoryPort
    audit_store: PublicationAuditStorePort

    def execute(
        self,
        recipe: QueryRecipe,
        approval: RecipePublicationApproval,
    ) -> RecipePublicationResult:
        _validate_approval(recipe, approval)
        result = self.repository.publish(recipe, approval)
        try:
            validate_publication_audit_binding(
                result.audit_records,
                approval_id=approval.id,
                actor=approval.actor,
                approved_at=approval.approved_at,
                new_fingerprint=recipe.fingerprint,
            )
            self.audit_store.append(result.audit_records)
        except (PublicationAuditStoreError, ValueError) as error:
            raise RecipeError(
                RecipeErrorCode.PUBLICATION_FAILED,
                "query-recipe publication audit could not be persisted",
            ) from error
        return result


@dataclass(frozen=True, slots=True)
class AssessQueryRecipeReuse:
    repository: QueryRecipeReadPort
    compiler_version: str = RECIPE_COMPILER_VERSION
    semantic_gate: AssertSemanticContextCurrent | None = None
    semantic_scope: SemanticRegistryScope | None = None

    def __post_init__(self) -> None:
        if (self.semantic_gate is None) != (self.semantic_scope is None):
            raise ValueError("recipe semantic gate and scope must be configured together")

    def execute(
        self,
        *,
        validated_request: ValidatedAnalyticalRequest,
        resolved_plan: ResolvedSemanticPlan,
        plan_fingerprint: str,
        query_fingerprint: str,
        source_schema_fingerprint: str,
    ) -> RecipeReuseAssessment:
        intent_fingerprint = fingerprint_recipe_payload(
            validated_request.request.model_dump(mode="json")
        )
        published = self.repository.find_current(
            intent_fingerprint,
            scope_fingerprint=resolved_plan.active_scope_fingerprint,
        )
        if published is None:
            return RecipeReuseAssessment(
                status=RecipeReuseStatus.NOT_FOUND,
                intent_fingerprint=intent_fingerprint,
            )
        assessment = _assess_published_recipe(
            published,
            validated_request=validated_request,
            resolved_plan=resolved_plan,
            plan_fingerprint=plan_fingerprint,
            query_fingerprint=query_fingerprint,
            source_schema_fingerprint=source_schema_fingerprint,
            compiler_version=self.compiler_version,
        )
        if self.semantic_gate is None:
            return assessment
        assert self.semantic_scope is not None
        try:
            dependencies = semantic_plan_dependencies(
                resolved_plan,
                self.semantic_scope,
            )
            self.semantic_gate.execute(dependencies)
        except SemanticChangeError:
            reasons = tuple(
                dict.fromkeys(
                    (
                        *assessment.reasons,
                        RecipeStalenessCode.SEMANTIC_EVIDENCE_CHANGED,
                    )
                )
            )
            return RecipeReuseAssessment(
                status=RecipeReuseStatus.STALE,
                intent_fingerprint=assessment.intent_fingerprint,
                recipe_id=assessment.recipe_id,
                recipe_version=assessment.recipe_version,
                recipe_fingerprint=assessment.recipe_fingerprint,
                provenance_document_urn=assessment.provenance_document_urn,
                reasons=reasons,
            )
        return assessment


def _recipe_values(
    draft: AgentWorkflowDraft,
    compiler_version: str,
) -> _RecipeValues:
    assert draft.validated_request is not None
    assert draft.resolved_plan is not None
    assert draft.plan_fingerprint is not None
    assert draft.query_fingerprint is not None
    assert draft.execution is not None
    linked_assets, schema_fingerprint = _source_assets(draft)
    mappings = tuple(
        RecipeMappingVersion(
            logical_field=item.mapping.logical_field,
            physical_field=item.mapping.physical_field,
            version=item.mapping.version,
            approval_decision_id=item.approval_decision_id or "",
        )
        for item in draft.resolved_plan.selected_mappings
    )
    joins = tuple(
        RecipeJoinVersion(
            contract_id=item.id,
            version=item.version,
            approval_decision_id=item.approval_decision_id or "",
        )
        for item in draft.resolved_plan.selected_contracts
    )
    validation = RecipeValidationSummary(
        executed_at=_execution_completed_at(draft),
        database_user=draft.execution.database_user,
        transaction_read_only=draft.execution.transaction_read_only,
        statement_timeout_ms=draft.execution.statement_timeout_ms,
        row_count=draft.execution.observed_row_count,
        rejected_count=draft.execution.rejected_count,
        rejection_codes=draft.execution.rejection_codes,
        truncated=draft.execution.any_truncated,
        preview_fingerprint=draft.execution.preview_fingerprint,
    )
    return _RecipeValues(
        business_question=draft.text,
        normalized_intent=draft.validated_request.request,
        validated_request_fingerprint=validated_analytical_request_fingerprint(
            draft.validated_request
        ),
        model_version=RecipeModelVersion(
            source=draft.resolved_plan.context_source,
            version=draft.resolved_plan.context_version,
            fingerprint=draft.resolved_plan.context_fingerprint,
        ),
        registry_binding=_registry_binding(draft.resolved_plan),
        mapping_versions=mappings,
        join_versions=joins,
        source_schema_fingerprint=schema_fingerprint,
        plan_fingerprint=draft.plan_fingerprint,
        query_fingerprint=draft.query_fingerprint,
        compiler_version=compiler_version,
        validation=validation,
        limitations=DEFAULT_RECIPE_LIMITATIONS,
        linked_asset_urns=linked_assets,
        source_workflow_id=draft.id,
        created_at=_execution_completed_at(draft),
    )


def _assess_published_recipe(
    published: PublishedQueryRecipe,
    *,
    validated_request: ValidatedAnalyticalRequest,
    resolved_plan: ResolvedSemanticPlan,
    plan_fingerprint: str,
    query_fingerprint: str,
    source_schema_fingerprint: str,
    compiler_version: str,
) -> RecipeReuseAssessment:
    recipe = published.recipe
    intent_fingerprint = fingerprint_recipe_payload(
        validated_request.request.model_dump(mode="json")
    )
    reasons: list[RecipeStalenessCode] = []
    if recipe.registry_binding != _registry_binding(resolved_plan):
        reasons.append(RecipeStalenessCode.ACTIVE_REGISTRY_CHANGED)
    if (
        recipe.model_version.source != resolved_plan.context_source
        or recipe.model_version.version != resolved_plan.context_version
        or recipe.model_version.fingerprint != resolved_plan.context_fingerprint
    ):
        reasons.append(RecipeStalenessCode.MODEL_CONTEXT_CHANGED)
    current_mappings = tuple(
        sorted(
            (
                item.mapping.logical_field.root,
                item.mapping.physical_field.root,
                item.mapping.version,
                item.approval_decision_id,
            )
            for item in resolved_plan.selected_mappings
        )
    )
    recorded_mappings = tuple(
        (
            item.logical_field.root,
            item.physical_field.root,
            item.version,
            item.approval_decision_id,
        )
        for item in recipe.mapping_versions
    )
    if recorded_mappings != current_mappings:
        reasons.append(RecipeStalenessCode.MAPPING_VERSION_CHANGED)
    current_joins = tuple(
        sorted(
            (item.id, item.version, item.approval_decision_id)
            for item in resolved_plan.selected_contracts
        )
    )
    recorded_joins = tuple(
        (item.contract_id, item.version, item.approval_decision_id) for item in recipe.join_versions
    )
    if recorded_joins != current_joins:
        reasons.append(RecipeStalenessCode.JOIN_VERSION_CHANGED)
    if recipe.source_schema_fingerprint != source_schema_fingerprint:
        reasons.append(RecipeStalenessCode.SOURCE_SCHEMA_CHANGED)
    if recipe.compiler_version != compiler_version:
        reasons.append(RecipeStalenessCode.COMPILER_VERSION_CHANGED)
    if (
        plan_fingerprint != resolved_semantic_plan_fingerprint(resolved_plan)
        or recipe.plan_fingerprint != plan_fingerprint
    ):
        reasons.append(RecipeStalenessCode.PLAN_CHANGED)
    if recipe.query_fingerprint != query_fingerprint:
        reasons.append(RecipeStalenessCode.QUERY_CHANGED)
    return RecipeReuseAssessment(
        status=RecipeReuseStatus.STALE if reasons else RecipeReuseStatus.REUSABLE,
        intent_fingerprint=intent_fingerprint,
        recipe_id=recipe.id,
        recipe_version=recipe.version,
        recipe_fingerprint=recipe.fingerprint,
        provenance_document_urn=published.versioned_document_urn,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def _registry_binding(resolved_plan: ResolvedSemanticPlan) -> RecipeRegistryBinding | None:
    if resolved_plan.activation_generation is None:
        return None
    assert resolved_plan.active_pointer_fingerprint is not None
    return RecipeRegistryBinding(
        generation=resolved_plan.activation_generation,
        pointer_fingerprint=resolved_plan.active_pointer_fingerprint,
        scope_fingerprint=resolved_plan.active_scope_fingerprint,
        registry_version=resolved_plan.context_version,
        registry_fingerprint=resolved_plan.context_fingerprint,
    )


def _recipe_scope_fingerprint(recipe: QueryRecipe) -> str | None:
    binding = recipe.registry_binding
    return binding.scope_fingerprint if binding is not None else None


def _require_active_registry_binding(
    draft: AgentWorkflowDraft,
    active_pointer: ActiveRegistryPointer,
) -> None:
    assert draft.resolved_plan is not None
    binding = _registry_binding(draft.resolved_plan)
    if (
        binding is None
        or binding.generation != active_pointer.generation
        or binding.pointer_fingerprint != registry_projection_fingerprint(active_pointer)
        or binding.registry_version != active_pointer.registry_version
        or binding.registry_fingerprint != active_pointer.registry_fingerprint
    ):
        raise RecipeError(
            RecipeErrorCode.STALE_WORKFLOW,
            "recipe migration workflow is not bound to the active registry pointer",
        )


def _same_published_snapshot(
    left: PublishedQueryRecipe,
    right: PublishedQueryRecipe,
) -> bool:
    return left == right and left.model_dump_json().encode() == right.model_dump_json().encode()


def source_schema_fingerprint(draft: AgentWorkflowDraft) -> str:
    return _source_assets(draft)[1]


def _require_completed_execution(draft: AgentWorkflowDraft) -> None:
    if (
        draft.validated_request is None
        or draft.resolved_plan is None
        or draft.plan_fingerprint is None
        or draft.query_fingerprint is None
        or draft.execution is None
        or not draft.execution.rejection_complete
    ):
        raise RecipeError(
            RecipeErrorCode.INVALID_WORKFLOW,
            "query recipe requires successful validation, read-only execution, and rejection inspection",
        )
    if (
        draft.execution.plan_fingerprint != draft.plan_fingerprint
        or draft.execution.query_fingerprint != draft.query_fingerprint
    ):
        raise RecipeError(
            RecipeErrorCode.INVALID_WORKFLOW,
            "query recipe execution fingerprints do not match the workflow draft",
        )


def _source_assets(draft: AgentWorkflowDraft) -> tuple[tuple[str, ...], str]:
    if draft.resolved_plan is None:
        raise RecipeError(RecipeErrorCode.INVALID_WORKFLOW, "query recipe has no resolved plan")
    required = {asset.dataset.root for asset in draft.resolved_plan.query_policy.assets}
    selected = tuple(
        sorted(asset.urn for asset in draft.context_assets if asset.dataset.root in required)
    )
    source_facts = tuple(
        sorted(
            (
                asset.dataset.root,
                asset.urn,
                asset.field_count,
                asset.partial,
                asset.schema_fingerprint,
            )
            for asset in draft.context_assets
            if asset.dataset.root in required
        )
    )
    if (
        len(selected) != len(required)
        or any(item[3] for item in source_facts)
        or any(item[4] is None for item in source_facts)
    ):
        raise RecipeError(
            RecipeErrorCode.INVALID_WORKFLOW,
            "query recipe requires complete catalog facts for every resolved physical asset",
        )
    return selected, fingerprint_recipe_payload(source_facts)


def _execution_completed_at(draft: AgentWorkflowDraft) -> datetime:
    for event in reversed(draft.trace):
        if (
            event.operation is WorkflowOperation.REJECTION_INSPECTION
            and event.status is WorkflowTraceStatus.SUCCEEDED
        ):
            return event.occurred_at
    return draft.updated_at


def _validate_approval(recipe: QueryRecipe, approval: RecipePublicationApproval) -> None:
    if not isinstance(approval, RecipePublicationApproval):
        raise RecipeError(
            RecipeErrorCode.APPROVAL_REQUIRED,
            "explicit typed query-recipe approval is required",
        )
    if (
        approval.recipe_id != recipe.id
        or approval.recipe_version != recipe.version
        or approval.recipe_fingerprint != recipe.fingerprint
    ):
        raise RecipeError(
            RecipeErrorCode.APPROVAL_MISMATCH,
            "query-recipe approval does not match the reviewed version and fingerprint",
        )
