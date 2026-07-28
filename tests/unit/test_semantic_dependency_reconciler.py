"""Adversarial complete-by-construction dependency reconciliation tests."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Generic, TypeVar

import pytest
import yaml

from schemabridge.adapters.semantic_registry.recorded import (
    RecordedGovernedSemanticRegistry,
)
from schemabridge.application.ports.semantic_dependency_sources import (
    CurrentQueryRecipeDocument,
    SemanticDependencyArtifactKind,
    SemanticDependencyIndexWrite,
    SemanticDependencySourceError,
    SemanticDependencySourceErrorCode,
    SemanticDependencySourcePage,
)
from schemabridge.application.semantic_dependency_reconciler import (
    ReconcileSemanticDependencies,
    SemanticDependencyReconciliationError,
)
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.recipes import (
    QueryRecipe,
    RecipeJoinVersion,
    RecipeMappingVersion,
    RecipeModelVersion,
    RecipeRegistryBinding,
    RecipeValidationSummary,
)
from schemabridge.domain.registry_control import (
    ActiveRegistryPointer,
    GovernedRegistryVersion,
    RegistryVersionTrust,
    registry_projection_fingerprint,
)
from schemabridge.domain.request_context import validate_analytical_request
from schemabridge.domain.resolution import (
    ResolutionLimits,
    ResolvedSemanticPlan,
    resolve_semantic_request,
    resolved_semantic_plan_fingerprint,
)
from schemabridge.domain.semantic_change import (
    SemanticDependencyIndexState,
    semantic_change_fingerprint,
)
from schemabridge.domain.semantic_registry import (
    GovernedMappingRegistry,
    GovernedSemanticRegistrySnapshot,
    RegistryArtifactKind,
    RegistryArtifactProvenance,
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
    datahub_registry_document_urn,
    prepare_datahub_registry_version,
    semantic_registry_decision_ids,
    semantic_registry_scope_fingerprint,
)
from schemabridge.domain.workflows import AgentWorkflowDraft, WorkflowStage

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 24, 15, 0, tzinfo=UTC)
SCOPE = SemanticRegistryScope(
    workspace_id="workspace-dependency-unit",
    catalog_scope="synthetic-demo",
    registry_id="synthetic_enterprise",
)


@pytest.fixture(scope="module")
def governed_version() -> GovernedRegistryVersion:
    recorded = (
        RecordedGovernedSemanticRegistry(
            ROOT / "demo/ground_truth/registries/manifest.yml",
            SCOPE,
        )
        .load()
        .registry
    )
    live = prepare_datahub_registry_version(recorded, SCOPE)
    return GovernedRegistryVersion(
        snapshot=ScopedSemanticRegistrySnapshot(scope=SCOPE, registry=live),
        publication_approval_id="dependency-unit-publication",
        trust=RegistryVersionTrust.STRICT,
    )


def _pointer(version: GovernedRegistryVersion) -> ActiveRegistryPointer:
    registry = version.snapshot.registry
    return ActiveRegistryPointer(
        scope=SCOPE,
        generation=7,
        registry_version=registry.version,
        registry_fingerprint=registry.fingerprint,
        registry_target=datahub_registry_document_urn(SCOPE, registry.version),
        transition_id="transition-dependency-unit",
        activated_by="dependency-unit-publisher",
        activated_at=NOW,
        decision_ids=semantic_registry_decision_ids(registry),
    )


@dataclass
class _Pointers:
    pointer: ActiveRegistryPointer

    def load_active(self, scope: SemanticRegistryScope) -> ActiveRegistryPointer | None:
        assert scope == SCOPE
        return self.pointer


@dataclass
class _Versions:
    version: GovernedRegistryVersion

    def load_version(
        self,
        scope: SemanticRegistryScope,
        version: int,
    ) -> GovernedRegistryVersion:
        assert scope == SCOPE
        assert version == self.version.snapshot.registry.version
        return self.version


ItemT = TypeVar("ItemT")


@dataclass
class _Pages(Generic[ItemT]):
    values: tuple[SemanticDependencySourcePage[ItemT], ...]
    fail_after: int | None = None

    def pages(
        self,
        scope: SemanticRegistryScope,
        *,
        page_size: int,
    ) -> Iterator[SemanticDependencySourcePage[ItemT]]:
        assert scope == SCOPE
        assert 1 <= page_size <= 50
        for index, page in enumerate(self.values):
            if self.fail_after is not None and index == self.fail_after:
                raise SemanticDependencySourceError(
                    SemanticDependencySourceErrorCode.UNAVAILABLE,
                    "synthetic dependency source unavailable",
                )
            yield page


@dataclass
class _Sink:
    writes: list[SemanticDependencyIndexWrite] = field(default_factory=list)

    def reconcile(
        self,
        snapshot: SemanticDependencyIndexWrite,
    ) -> SemanticDependencyIndexState:
        self.writes.append(snapshot)
        return SemanticDependencyIndexState(
            scope=snapshot.scope,
            watermark=snapshot.watermark,
            fingerprint=semantic_change_fingerprint(
                {
                    "watermark": snapshot.watermark,
                    "complete": snapshot.complete,
                    "manifest": snapshot.manifest.fingerprint,
                }
            ),
            complete=snapshot.complete,
        )


def _source_pages(
    items: tuple[ItemT, ...],
    *,
    page_size: int,
    prefix: str,
) -> tuple[SemanticDependencySourcePage[ItemT], ...]:
    if not items:
        return (
            SemanticDependencySourcePage(
                sequence=1,
                items=(),
                source_position=f"{prefix}:empty",
                eof=True,
            ),
        )
    pages: list[SemanticDependencySourcePage[ItemT]] = []
    for offset in range(0, len(items), page_size):
        chunk = items[offset : offset + page_size]
        pages.append(
            SemanticDependencySourcePage(
                sequence=len(pages) + 1,
                items=chunk,
                source_position=f"{prefix}:{offset + len(chunk)}",
                eof=offset + len(chunk) == len(items),
            )
        )
    return tuple(pages)


def _empty_workflow(identifier: str) -> AgentWorkflowDraft:
    return AgentWorkflowDraft(
        id=identifier,
        revision=1,
        stage=WorkflowStage.CREATED,
        text="Petición sintética gobernada.",
        language=UserLanguage.SPANISH,
        requested_datasets=(PhysicalDatasetRef("crm.customers"),),
        created_at=NOW,
        updated_at=NOW,
    )


def _plan(registry: GovernedSemanticRegistrySnapshot) -> ResolvedSemanticPlan:
    historical = QueryRecipe.model_validate(
        yaml.safe_load((ROOT / "examples/query-recipe-secondary-holders.yml").read_text())
    )
    validated = validate_analytical_request(
        historical.normalized_intent,
        registry.logical_context,
    ).validated_request
    assert validated is not None
    return resolve_semantic_request(validated, registry, ResolutionLimits())


def _next_governed_version(
    version: GovernedRegistryVersion,
) -> GovernedRegistryVersion:
    registry = version.snapshot.registry
    selected = _plan(registry).selected_mappings[0]
    assert selected.approval_decision_id is not None
    next_decision = f"{selected.approval_decision_id}-v2"
    replacement = selected.model_copy(
        update={
            "mapping": selected.mapping.model_copy(
                update={"version": selected.mapping.version + 1}
            ),
            "approval_decision_id": next_decision,
        }
    )
    mappings = tuple(
        replacement if item == selected else item for item in registry.mapping_set.mappings
    )
    provenance = tuple(
        RegistryArtifactProvenance(
            kind=item.kind,
            source=item.source,
            decision_ids=(
                tuple(
                    next_decision if decision == selected.approval_decision_id else decision
                    for decision in item.decision_ids
                )
                if item.kind is RegistryArtifactKind.PHYSICAL_MAPPINGS
                else item.decision_ids
            ),
        )
        for item in registry.provenance
    )
    numbered = GovernedSemanticRegistrySnapshot.model_validate(
        {
            **registry.model_dump(mode="python"),
            "version": registry.version + 1,
            "mapping_set": GovernedMappingRegistry(
                version=registry.mapping_set.version + 1,
                mappings=mappings,
            ).model_dump(mode="python"),
            "provenance": tuple(item.model_dump(mode="python") for item in provenance),
        }
    )
    live = prepare_datahub_registry_version(numbered, SCOPE)
    return GovernedRegistryVersion(
        snapshot=ScopedSemanticRegistrySnapshot(scope=SCOPE, registry=live),
        publication_approval_id="dependency-unit-publication-v2",
        trust=RegistryVersionTrust.STRICT,
    )


def _planned_workflow(
    registry: GovernedSemanticRegistrySnapshot,
    pointer: ActiveRegistryPointer,
    *,
    identifier: str = "workflow-planned",
) -> AgentWorkflowDraft:
    plan = _plan(registry).model_copy(
        update={
            "activation_generation": pointer.generation,
            "active_pointer_fingerprint": registry_projection_fingerprint(pointer),
            "active_scope_fingerprint": semantic_registry_scope_fingerprint(pointer.scope),
        }
    )
    datasets = (
        plan.query_plan.root_scan.dataset,
        *(join.right_scan.dataset for join in plan.query_plan.joins),
    )
    return AgentWorkflowDraft(
        id=identifier,
        revision=3,
        stage=WorkflowStage.PLAN_READY,
        text="Agrupa clientes secundarios por fecha.",
        language=UserLanguage.SPANISH,
        requested_datasets=datasets,
        context_source=registry.source,
        resolved_plan=plan,
        plan_fingerprint=resolved_semantic_plan_fingerprint(plan),
        created_at=NOW,
        updated_at=NOW,
    )


def _recipe(
    registry: GovernedSemanticRegistrySnapshot,
    pointer: ActiveRegistryPointer,
    *,
    source: str | None = None,
    registry_fingerprint: str | None = None,
    request_limit: int | None = None,
) -> QueryRecipe:
    plan = _plan(registry)
    fingerprint = registry_fingerprint or registry.fingerprint
    return QueryRecipe.create(
        version=4,
        business_question="Agrupa clientes secundarios por fecha.",
        normalized_intent=plan.request.model_copy(
            update={"limit": request_limit or plan.request.limit}
        ),
        validated_request_fingerprint="a" * 64,
        model_version=RecipeModelVersion(
            source=source or registry.source,
            version=registry.version,
            fingerprint=fingerprint,
        ),
        registry_binding=RecipeRegistryBinding(
            generation=pointer.generation,
            pointer_fingerprint=registry_projection_fingerprint(pointer),
            scope_fingerprint=semantic_registry_scope_fingerprint(pointer.scope),
            registry_version=registry.version,
            registry_fingerprint=fingerprint,
        ),
        mapping_versions=tuple(
            sorted(
                (
                    RecipeMappingVersion(
                        logical_field=item.mapping.logical_field,
                        physical_field=item.mapping.physical_field,
                        version=item.mapping.version,
                        approval_decision_id=item.approval_decision_id or "",
                    )
                    for item in plan.selected_mappings
                ),
                key=lambda item: (
                    item.logical_field.root,
                    item.physical_field.root,
                    item.version,
                ),
            )
        ),
        join_versions=tuple(
            RecipeJoinVersion(
                contract_id=item.id,
                version=item.version,
                approval_decision_id=item.approval_decision_id or "",
            )
            for item in plan.selected_contracts
        ),
        source_schema_fingerprint="c" * 64,
        plan_fingerprint=resolved_semantic_plan_fingerprint(plan),
        query_fingerprint="d" * 64,
        compiler_version="postgres-v1",
        validation=RecipeValidationSummary(
            executed_at=NOW,
            database_user="schemabridge_reader",
            transaction_read_only=True,
            statement_timeout_ms=5_000,
            row_count=3,
            rejected_count=0,
            truncated=False,
            preview_fingerprint="e" * 64,
        ),
        limitations=("Synthetic read-only evidence.",),
        linked_asset_urns=tuple(
            sorted(f"urn:li:dataset:{asset.dataset.root}" for asset in plan.query_policy.assets)
        ),
        source_workflow_id="workflow-planned",
        created_at=NOW,
    )


def _document(recipe: QueryRecipe) -> CurrentQueryRecipeDocument:
    assert recipe.registry_binding is not None
    assert recipe.registry_binding.scope_fingerprint is not None
    prefix = (
        "urn:li:document:schemabridge-query-recipe-"
        f"{recipe.registry_binding.scope_fingerprint[:32]}-"
        f"{recipe.intent_fingerprint[:32]}"
    )
    return CurrentQueryRecipeDocument(
        document_urn=f"{prefix}-current",
        versioned_document_urn=f"{prefix}-v{recipe.version}",
        approval_id="recipe-dependency-approval",
        published_at=NOW,
        recipe=recipe,
    )


def _service(
    version: GovernedRegistryVersion,
    workflows: _Pages[AgentWorkflowDraft],
    recipes: _Pages[CurrentQueryRecipeDocument],
    sink: _Sink,
    *,
    page_size: int = 50,
) -> ReconcileSemanticDependencies:
    return ReconcileSemanticDependencies(
        pointers=_Pointers(_pointer(version)),
        versions=_Versions(version),
        workflows=workflows,
        recipes=recipes,
        index=sink,
        page_size=page_size,
    )


def test_complete_manifest_indexes_exact_active_dependencies_and_empty_workflows(
    governed_version: GovernedRegistryVersion,
) -> None:
    registry = governed_version.snapshot.registry
    pointer = _pointer(governed_version)
    planned = _planned_workflow(registry, pointer)
    empty = _empty_workflow("workflow-empty")
    active_recipe = _recipe(registry, pointer)
    sink = _Sink()

    result = _service(
        governed_version,
        _Pages(_source_pages((empty, planned), page_size=1, prefix="workflow")),
        _Pages(
            _source_pages(
                (_document(active_recipe),),
                page_size=1,
                prefix="recipe",
            )
        ),
        sink,
        page_size=1,
    ).execute(SCOPE, watermark=11, indexed_at=NOW)

    assert result.state.complete is True
    assert result.manifest.workflow_source.page_count == 2
    assert result.manifest.recipe_source.discovered_count == 1
    assert len(sink.writes) == 1
    indexed = sink.writes[0].artifacts
    assert tuple((item.kind, item.artifact_id) for item in indexed) == (
        (SemanticDependencyArtifactKind.QUERY_RECIPE, active_recipe.id),
        (SemanticDependencyArtifactKind.WORKFLOW, "workflow-empty"),
        (SemanticDependencyArtifactKind.WORKFLOW, "workflow-planned"),
    )
    empty_artifact = next(item for item in indexed if item.artifact_id == "workflow-empty")
    planned_artifact = next(item for item in indexed if item.artifact_id == "workflow-planned")
    assert planned.resolved_plan is not None
    assert empty_artifact.mapping_decision_ids == ()
    assert empty_artifact.join_contracts == ()
    assert set(planned_artifact.mapping_decision_ids) == {
        item.approval_decision_id for item in planned.resolved_plan.selected_mappings
    }
    assert set(planned_artifact.join_contracts) == {
        (item.id, item.version) for item in planned.resolved_plan.selected_contracts
    }


def test_recipe_source_crossing_workspace_scope_is_durably_incomplete(
    governed_version: GovernedRegistryVersion,
) -> None:
    registry = governed_version.snapshot.registry
    pointer = _pointer(governed_version)
    other_scope_pointer = pointer.model_copy(
        update={
            "scope": pointer.scope.model_copy(update={"workspace_id": "workspace-dependency-other"})
        }
    )
    foreign_recipe = _recipe(
        registry,
        other_scope_pointer,
        request_limit=499,
    )
    local_recipe = _recipe(registry, pointer, request_limit=499)
    assert _document(local_recipe).document_urn != _document(foreign_recipe).document_urn
    sink = _Sink()

    result = _service(
        governed_version,
        _Pages(_source_pages((), page_size=50, prefix="workflow")),
        _Pages(_source_pages((_document(foreign_recipe),), page_size=50, prefix="recipe")),
        sink,
    ).execute(SCOPE, watermark=16, indexed_at=NOW)

    assert result.state.complete is False
    assert result.manifest.recipe_source.discovered_count == 1
    assert result.manifest.recipe_source.failure_code == "artifact_registry_mismatch"
    assert sink.writes[0].artifacts == ()


def test_stale_recipe_in_same_scope_projects_to_current_governed_dependencies(
    governed_version: GovernedRegistryVersion,
) -> None:
    registry = governed_version.snapshot.registry
    pointer = _pointer(governed_version)
    stale_recipe = _recipe(
        registry,
        pointer,
        registry_fingerprint="f" * 64,
    )
    sink = _Sink()

    result = _service(
        governed_version,
        _Pages(_source_pages((), page_size=50, prefix="workflow")),
        _Pages(
            _source_pages(
                (_document(stale_recipe),),
                page_size=50,
                prefix="recipe",
            )
        ),
        sink,
    ).execute(SCOPE, watermark=18, indexed_at=NOW)

    assert result.state.complete is True
    assert len(sink.writes[0].artifacts) == 1
    artifact = sink.writes[0].artifacts[0]
    assert artifact.artifact_id == stale_recipe.id
    assert set(artifact.mapping_decision_ids) == {
        item.approval_decision_id
        for item in registry.mapping_set.mappings
        if item.mapping.logical_field
        in {mapping.logical_field for mapping in stale_recipe.mapping_versions}
    }
    assert set(artifact.join_contracts) == {
        (item.id, item.version)
        for item in registry.join_contracts.contracts
        if item.id in {join.contract_id for join in stale_recipe.join_versions}
    }


def test_stale_workflow_from_v1_source_projects_to_v2_dependencies(
    governed_version: GovernedRegistryVersion,
) -> None:
    prior_registry = governed_version.snapshot.registry
    current_version = _next_governed_version(governed_version)
    current_registry = current_version.snapshot.registry
    stale_workflow = _planned_workflow(prior_registry, _pointer(governed_version))
    assert stale_workflow.resolved_plan is not None
    assert stale_workflow.resolved_plan.context_source != current_registry.source
    sink = _Sink()

    result = _service(
        current_version,
        _Pages(_source_pages((stale_workflow,), page_size=50, prefix="workflow")),
        _Pages(_source_pages((), page_size=50, prefix="recipe")),
        sink,
    ).execute(SCOPE, watermark=19, indexed_at=NOW)

    assert result.state.complete is True
    artifact = sink.writes[0].artifacts[0]
    stale_logical_fields = {
        item.mapping.logical_field for item in stale_workflow.resolved_plan.selected_mappings
    }
    assert set(artifact.mapping_decision_ids) == {
        item.approval_decision_id
        for item in current_registry.mapping_set.mappings
        if item.mapping.logical_field in stale_logical_fields
    }
    assert set(artifact.join_contracts) == {
        (item.id, item.version)
        for item in current_registry.join_contracts.contracts
        if item.id in {contract.id for contract in stale_workflow.resolved_plan.selected_contracts}
    }


def test_stale_recipe_from_v1_source_projects_to_v2_dependencies(
    governed_version: GovernedRegistryVersion,
) -> None:
    prior_registry = governed_version.snapshot.registry
    current_version = _next_governed_version(governed_version)
    current_registry = current_version.snapshot.registry
    stale_recipe = _recipe(prior_registry, _pointer(governed_version))
    assert stale_recipe.model_version.source != current_registry.source
    sink = _Sink()

    result = _service(
        current_version,
        _Pages(_source_pages((), page_size=50, prefix="workflow")),
        _Pages(_source_pages((_document(stale_recipe),), page_size=50, prefix="recipe")),
        sink,
    ).execute(SCOPE, watermark=20, indexed_at=NOW)

    assert result.state.complete is True
    artifact = sink.writes[0].artifacts[0]
    stale_logical_fields = {item.logical_field for item in stale_recipe.mapping_versions}
    assert set(artifact.mapping_decision_ids) == {
        item.approval_decision_id
        for item in current_registry.mapping_set.mappings
        if item.mapping.logical_field in stale_logical_fields
    }
    assert set(artifact.join_contracts) == {
        (item.id, item.version)
        for item in current_registry.join_contracts.contracts
        if item.id in {join.contract_id for join in stale_recipe.join_versions}
    }


def test_workflow_crossing_workspace_scope_is_durably_incomplete(
    governed_version: GovernedRegistryVersion,
) -> None:
    registry = governed_version.snapshot.registry
    pointer = _pointer(governed_version)
    foreign_pointer = pointer.model_copy(
        update={"scope": pointer.scope.model_copy(update={"workspace_id": "workspace-foreign"})}
    )
    foreign_workflow = _planned_workflow(registry, foreign_pointer)
    sink = _Sink()

    result = _service(
        governed_version,
        _Pages(_source_pages((foreign_workflow,), page_size=50, prefix="workflow")),
        _Pages(_source_pages((), page_size=50, prefix="recipe")),
        sink,
    ).execute(SCOPE, watermark=21, indexed_at=NOW)

    assert result.state.complete is False
    assert result.manifest.workflow_source.failure_code == "artifact_registry_mismatch"
    assert sink.writes[0].artifacts == ()


def test_second_page_failure_is_durably_incomplete(
    governed_version: GovernedRegistryVersion,
) -> None:
    workflows = tuple(_empty_workflow(f"workflow-fail-{index}") for index in range(3))
    workflow_pages = _source_pages(workflows, page_size=1, prefix="workflow")
    sink = _Sink()

    result = _service(
        governed_version,
        _Pages(workflow_pages, fail_after=1),
        _Pages(_source_pages((), page_size=1, prefix="recipe")),
        sink,
        page_size=1,
    ).execute(SCOPE, watermark=12, indexed_at=NOW)

    assert result.state.complete is False
    assert result.manifest.workflow_source.eof is False
    assert result.manifest.workflow_source.failure_code == "source_unavailable"
    assert len(sink.writes) == 1
    assert sink.writes[0].complete is False


def test_lost_execution_lease_aborts_before_dependency_index_write(
    governed_version: GovernedRegistryVersion,
) -> None:
    workflows = tuple(_empty_workflow(f"workflow-lease-{index}") for index in range(3))
    sink = _Sink()
    checks = 0

    def lease() -> bool:
        nonlocal checks
        checks += 1
        return checks < 4

    with pytest.raises(SemanticDependencyReconciliationError):
        _service(
            governed_version,
            _Pages(_source_pages(workflows, page_size=1, prefix="workflow")),
            _Pages(_source_pages((), page_size=1, prefix="recipe")),
            sink,
            page_size=1,
        ).execute(
            SCOPE,
            watermark=17,
            indexed_at=NOW,
            should_continue=lease,
        )

    assert sink.writes == []


def test_duplicate_and_invalid_artifact_pages_cannot_claim_complete(
    governed_version: GovernedRegistryVersion,
) -> None:
    draft = _empty_workflow("workflow-duplicate")
    duplicate_pages = (
        SemanticDependencySourcePage(
            sequence=1,
            items=(draft,),
            source_position="workflow:1",
            eof=False,
        ),
        SemanticDependencySourcePage(
            sequence=2,
            items=(draft,),
            source_position="workflow:2",
            eof=True,
        ),
    )
    invalid_recipe_page = SemanticDependencySourcePage(
        sequence=1,
        items=("not-a-query-recipe-document",),
        source_position="recipe:invalid",
        eof=True,
    )
    sink = _Sink()

    result = _service(
        governed_version,
        _Pages(duplicate_pages),
        _Pages((invalid_recipe_page,)),  # type: ignore[arg-type]
        sink,
    ).execute(SCOPE, watermark=13, indexed_at=NOW)

    assert result.state.complete is False
    assert result.manifest.workflow_source.failure_code == "duplicate_artifact"
    assert result.manifest.recipe_source.failure_code == "invalid_artifact"
    assert sink.writes[0].complete is False


def test_recipe_claiming_active_registry_with_unknown_approval_is_incomplete(
    governed_version: GovernedRegistryVersion,
) -> None:
    registry = governed_version.snapshot.registry
    recipe = _recipe(registry, _pointer(governed_version))
    first = recipe.mapping_versions[0].model_copy(
        update={"approval_decision_id": "unknown-mapping-approval"}
    )
    tampered = recipe.model_copy(update={"mapping_versions": (first, *recipe.mapping_versions[1:])})
    sink = _Sink()

    result = _service(
        governed_version,
        _Pages(_source_pages((), page_size=50, prefix="workflow")),
        _Pages(_source_pages((_document(tampered),), page_size=50, prefix="recipe")),
        sink,
    ).execute(SCOPE, watermark=15, indexed_at=NOW)

    assert result.state.complete is False
    assert result.manifest.recipe_source.failure_code == "artifact_registry_mismatch"
    assert sink.writes[0].complete is False


@pytest.mark.scale
def test_5434_workflows_remain_bounded_to_fifty_per_source_page(
    governed_version: GovernedRegistryVersion,
) -> None:
    workflows = tuple(_empty_workflow(f"workflow-scale-{index:05d}") for index in range(5_434))
    pages = _source_pages(workflows, page_size=50, prefix="workflow")
    assert len(pages) == 109
    assert max(len(page.items) for page in pages) == 50
    sink = _Sink()
    lease_checks = 0

    def keep_lease() -> bool:
        nonlocal lease_checks
        lease_checks += 1
        return True

    result = _service(
        governed_version,
        _Pages(pages),
        _Pages(_source_pages((), page_size=50, prefix="recipe")),
        sink,
    ).execute(
        SCOPE,
        watermark=14,
        indexed_at=NOW,
        should_continue=keep_lease,
    )

    assert result.state.complete is True
    assert result.manifest.workflow_source.discovered_count == 5_434
    assert len(sink.writes[0].artifacts) == 5_434
    assert lease_checks >= 220
