"""Approval-gated query-recipe preparation, publication, and compatibility checks."""

from __future__ import annotations

from dataclasses import dataclass

from schemabridge.application.ports.recipes import (
    QueryRecipeReadPort,
    QueryRecipeRepositoryPort,
    RecipeError,
    RecipeErrorCode,
)
from schemabridge.application.ports.workflows import WorkflowDraftStorePort
from schemabridge.domain.recipes import (
    QueryRecipe,
    RecipeJoinVersion,
    RecipeMappingVersion,
    RecipeModelVersion,
    RecipePublicationApproval,
    RecipePublicationResult,
    RecipeReuseAssessment,
    RecipeReuseStatus,
    RecipeStalenessCode,
    RecipeValidationSummary,
    fingerprint_recipe_payload,
)
from schemabridge.domain.request_context import (
    ValidatedAnalyticalRequest,
    validated_analytical_request_fingerprint,
)
from schemabridge.domain.resolution import (
    ResolvedSemanticPlan,
    resolved_semantic_plan_fingerprint,
)
from schemabridge.domain.workflows import AgentWorkflowDraft, WorkflowOperation, WorkflowTraceStatus

RECIPE_COMPILER_VERSION = "postgres-ir-v1"
DEFAULT_RECIPE_LIMITATIONS = (
    "Synthetic SchemaBridge demo assets only; not production evidence.",
    "Reuse never bypasses current semantic planning, SQL policy validation, or bounded preview.",
    "Validated for the recorded model, mapping, join, source-schema, and compiler versions only.",
)


@dataclass(frozen=True, slots=True)
class PrepareQueryRecipe:
    repository: QueryRecipeReadPort
    compiler_version: str = RECIPE_COMPILER_VERSION

    def execute(self, draft: AgentWorkflowDraft) -> QueryRecipe:
        _require_completed_execution(draft)
        assert draft.validated_request is not None
        assert draft.resolved_plan is not None
        assert draft.plan_fingerprint is not None
        assert draft.query_fingerprint is not None
        assert draft.execution is not None

        linked_assets, source_schema_fingerprint = _source_assets(draft)
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
            row_count=len(draft.execution.rows),
            rejected_count=draft.execution.rejected_count,
            rejection_codes=draft.execution.rejection_codes,
            truncated=draft.execution.truncated,
            preview_fingerprint=draft.execution.preview_fingerprint,
        )
        values = {
            "business_question": draft.text,
            "normalized_intent": draft.validated_request.request,
            "validated_request_fingerprint": validated_analytical_request_fingerprint(
                draft.validated_request
            ),
            "model_version": RecipeModelVersion(
                source=draft.resolved_plan.context_source,
                version=draft.resolved_plan.context_version,
                fingerprint=draft.resolved_plan.context_fingerprint,
            ),
            "mapping_versions": mappings,
            "join_versions": joins,
            "source_schema_fingerprint": source_schema_fingerprint,
            "plan_fingerprint": draft.plan_fingerprint,
            "query_fingerprint": draft.query_fingerprint,
            "compiler_version": self.compiler_version,
            "validation": validation,
            "limitations": DEFAULT_RECIPE_LIMITATIONS,
            "linked_asset_urns": linked_assets,
            "source_workflow_id": draft.id,
            "created_at": _execution_completed_at(draft),
        }
        candidate = QueryRecipe.create(version=1, **values)
        current = self.repository.find_current(candidate.intent_fingerprint)
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
class PublishQueryRecipe:
    repository: QueryRecipeRepositoryPort

    def execute(
        self,
        recipe: QueryRecipe,
        approval: RecipePublicationApproval,
    ) -> RecipePublicationResult:
        _validate_approval(recipe, approval)
        return self.repository.publish(recipe, approval)


@dataclass(frozen=True, slots=True)
class AssessQueryRecipeReuse:
    repository: QueryRecipeReadPort
    compiler_version: str = RECIPE_COMPILER_VERSION

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
        published = self.repository.find_current(intent_fingerprint)
        if published is None:
            return RecipeReuseAssessment(
                status=RecipeReuseStatus.NOT_FOUND,
                intent_fingerprint=intent_fingerprint,
            )
        recipe = published.recipe
        reasons: list[RecipeStalenessCode] = []
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
            (item.contract_id, item.version, item.approval_decision_id)
            for item in recipe.join_versions
        )
        if recorded_joins != current_joins:
            reasons.append(RecipeStalenessCode.JOIN_VERSION_CHANGED)
        if recipe.source_schema_fingerprint != source_schema_fingerprint:
            reasons.append(RecipeStalenessCode.SOURCE_SCHEMA_CHANGED)
        if recipe.compiler_version != self.compiler_version:
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


def _execution_completed_at(draft: AgentWorkflowDraft):  # type: ignore[no-untyped-def]
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
