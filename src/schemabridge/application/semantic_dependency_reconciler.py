"""Complete-by-construction workflow and query-recipe dependency reconciliation."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Generic, TypeVar

from schemabridge.application.ports.registry_control import (
    ActiveRegistryPointerReadPort,
    RegistryVersionReadPort,
)
from schemabridge.application.ports.semantic_dependency_sources import (
    MAX_SEMANTIC_DEPENDENCY_ARTIFACTS,
    MAX_SEMANTIC_DEPENDENCY_EDGES,
    MAX_SEMANTIC_DEPENDENCY_PAGE_SIZE,
    CurrentQueryRecipeDocument,
    ManagedWorkflowDependencySourcePort,
    QueryRecipeDependencySourcePort,
    SemanticDependencyArtifact,
    SemanticDependencyArtifactKind,
    SemanticDependencyIndexWrite,
    SemanticDependencyIndexWritePort,
    SemanticDependencyReconciliationManifest,
    SemanticDependencySourceError,
    SemanticDependencySourceManifest,
    SemanticDependencySourcePage,
)
from schemabridge.domain.joins import JoinContract
from schemabridge.domain.registry_control import (
    ActiveRegistryPointer,
    GovernedRegistryVersion,
    RegistryVersionTrust,
    registry_projection_fingerprint,
)
from schemabridge.domain.semantic_change import (
    SemanticDependencyIndexState,
    semantic_change_fingerprint,
)
from schemabridge.domain.semantic_registry import (
    GovernedFieldMapping,
    GovernedSemanticRegistrySnapshot,
    SemanticRegistryScope,
    semantic_registry_scope_fingerprint,
)
from schemabridge.domain.workflows import AgentWorkflowDraft, fingerprint_payload


class SemanticDependencyReconciliationErrorCode(StrEnum):
    """Safe failures which prevent even an incomplete snapshot from being stored."""

    ACTIVE_REGISTRY_UNAVAILABLE = "active_registry_unavailable"
    INDEX_STORE_UNAVAILABLE = "index_store_unavailable"
    INDEX_STORE_INVALID = "index_store_invalid"


class SemanticDependencyReconciliationError(RuntimeError):
    """Sanitized dependency reconciliation failure."""

    def __init__(
        self,
        code: SemanticDependencyReconciliationErrorCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SemanticDependencyReconciliationResult:
    """Durable state plus the exact source-coverage proof used to derive it."""

    state: SemanticDependencyIndexState
    manifest: SemanticDependencyReconciliationManifest

    def __post_init__(self) -> None:
        if self.state.complete != self.manifest.complete:
            raise ValueError("dependency state completeness disagrees with its source manifest")


@dataclass(frozen=True, slots=True)
class ReconcileSemanticDependencies:
    """Enumerate both complete sources and atomically advance one index watermark."""

    pointers: ActiveRegistryPointerReadPort
    versions: RegistryVersionReadPort
    workflows: ManagedWorkflowDependencySourcePort
    recipes: QueryRecipeDependencySourcePort
    index: SemanticDependencyIndexWritePort
    page_size: int = MAX_SEMANTIC_DEPENDENCY_PAGE_SIZE

    def __post_init__(self) -> None:
        if not 1 <= self.page_size <= MAX_SEMANTIC_DEPENDENCY_PAGE_SIZE:
            raise ValueError("semantic dependency page size must be between 1 and 50")

    def execute(
        self,
        scope: SemanticRegistryScope,
        *,
        watermark: int,
        indexed_at: datetime,
        should_continue: Callable[[], bool] = lambda: True,
    ) -> SemanticDependencyReconciliationResult:
        """Reconcile every managed artifact, persisting partial coverage as incomplete."""

        if watermark < 1:
            raise ValueError("semantic dependency watermark must be positive")
        if indexed_at.tzinfo is None or indexed_at.utcoffset() is None:
            raise ValueError("semantic dependency indexed time must include a timezone")
        _require_continuation(should_continue)
        pointer, registry = self._load_registry(scope)

        workflow_scan = _scan_source(
            SemanticDependencyArtifactKind.WORKFLOW,
            lambda: self.workflows.pages(scope, page_size=self.page_size),
            expected_type=AgentWorkflowDraft,
            identity=lambda item: item.id,
            item_fingerprint=lambda item: fingerprint_payload(item.model_dump(mode="json")),
            should_continue=should_continue,
        )
        recipe_scan = _scan_source(
            SemanticDependencyArtifactKind.QUERY_RECIPE,
            lambda: self.recipes.pages(scope, page_size=self.page_size),
            expected_type=CurrentQueryRecipeDocument,
            identity=lambda item: item.document_urn,
            item_fingerprint=lambda item: semantic_change_fingerprint(
                {
                    "document_urn": item.document_urn,
                    "versioned_document_urn": item.versioned_document_urn,
                    "approval_id": item.approval_id,
                    "published_at": item.published_at,
                    "recipe_fingerprint": item.recipe.fingerprint,
                }
            ),
            should_continue=should_continue,
        )

        workflow_artifacts, workflow_manifest = _workflow_artifacts(
            workflow_scan,
            registry,
            pointer,
        )
        recipe_artifacts, recipe_manifest = _recipe_artifacts(
            recipe_scan,
            registry,
            pointer,
        )
        artifacts = tuple(
            sorted(
                (*workflow_artifacts, *recipe_artifacts),
                key=lambda item: (item.kind.value, item.artifact_id, item.version),
            )
        )
        identities = tuple((item.kind.value, item.artifact_id, item.version) for item in artifacts)
        if identities != tuple(sorted(set(identities))):
            recipe_manifest = _failed_manifest(recipe_manifest, "duplicate_artifact_identity")
            artifacts = _deduplicate_artifacts(artifacts)
        if (
            len(artifacts) > MAX_SEMANTIC_DEPENDENCY_ARTIFACTS
            or sum(len(item.mapping_decision_ids) + len(item.join_contracts) for item in artifacts)
            > MAX_SEMANTIC_DEPENDENCY_EDGES
        ):
            recipe_manifest = _failed_manifest(
                recipe_manifest,
                "dependency_capacity_exceeded",
            )
            artifacts = ()

        manifest = _manifest(
            workflow_manifest,
            recipe_manifest,
            artifacts,
        )
        snapshot = SemanticDependencyIndexWrite(
            scope=scope,
            registry_generation=pointer.generation,
            registry_version=registry.version,
            registry_fingerprint=registry.fingerprint,
            pointer_transition_id=pointer.transition_id,
            watermark=watermark,
            artifacts=artifacts,
            manifest=manifest,
            indexed_at=indexed_at,
        )
        try:
            state = self.index.reconcile(snapshot)
        except Exception as error:
            raise SemanticDependencyReconciliationError(
                SemanticDependencyReconciliationErrorCode.INDEX_STORE_UNAVAILABLE,
                "semantic dependency index reconciliation failed",
            ) from error
        if (
            not isinstance(state, SemanticDependencyIndexState)
            or state.scope != scope
            or state.watermark != watermark
            or state.complete != manifest.complete
        ):
            raise SemanticDependencyReconciliationError(
                SemanticDependencyReconciliationErrorCode.INDEX_STORE_INVALID,
                "semantic dependency index returned invalid state",
            )
        return SemanticDependencyReconciliationResult(state=state, manifest=manifest)

    def _load_registry(
        self,
        scope: SemanticRegistryScope,
    ) -> tuple[ActiveRegistryPointer, GovernedSemanticRegistrySnapshot]:
        try:
            pointer = self.pointers.load_active(scope)
            if pointer is None:
                raise ValueError("active pointer is absent")
            pointer = ActiveRegistryPointer.model_validate(pointer)
            version = GovernedRegistryVersion.model_validate(
                self.versions.load_version(scope, pointer.registry_version)
            )
        except Exception as error:
            raise SemanticDependencyReconciliationError(
                SemanticDependencyReconciliationErrorCode.ACTIVE_REGISTRY_UNAVAILABLE,
                "active semantic registry is unavailable",
            ) from error
        registry = version.snapshot.registry
        if (
            pointer.scope != scope
            or version.snapshot.scope != scope
            or version.trust is not RegistryVersionTrust.STRICT
            or pointer.registry_version != registry.version
            or pointer.registry_fingerprint != registry.fingerprint
        ):
            raise SemanticDependencyReconciliationError(
                SemanticDependencyReconciliationErrorCode.ACTIVE_REGISTRY_UNAVAILABLE,
                "active semantic registry is unavailable",
            )
        return pointer, registry


ScannedItemT = TypeVar("ScannedItemT")


@dataclass(frozen=True, slots=True)
class _SourceScan(Generic[ScannedItemT]):
    items: tuple[ScannedItemT, ...]
    manifest: SemanticDependencySourceManifest


def _scan_source(
    kind: SemanticDependencyArtifactKind,
    pages: Callable[[], Iterator[SemanticDependencySourcePage[ScannedItemT]]],
    *,
    expected_type: type[ScannedItemT],
    identity: Callable[[ScannedItemT], str],
    item_fingerprint: Callable[[ScannedItemT], str],
    should_continue: Callable[[], bool],
) -> _SourceScan[ScannedItemT]:
    items: list[ScannedItemT] = []
    identities: set[str] = set()
    positions: set[str] = set()
    fingerprints: list[tuple[str, str]] = []
    page_count = 0
    eof = False
    failure_code: str | None = None
    expected_sequence = 1
    try:
        _require_continuation(should_continue)
        for page in pages():
            _require_continuation(should_continue)
            if not isinstance(page, SemanticDependencySourcePage):
                failure_code = "invalid_page"
                break
            page_count += 1
            if (
                eof
                or page.sequence != expected_sequence
                or page.source_position in positions
                or len(page.items) > MAX_SEMANTIC_DEPENDENCY_PAGE_SIZE
            ):
                failure_code = "invalid_page"
                eof = False
                break
            positions.add(page.source_position)
            expected_sequence += 1
            for raw_item in page.items:
                if len(items) >= MAX_SEMANTIC_DEPENDENCY_ARTIFACTS:
                    failure_code = "dependency_capacity_exceeded"
                    eof = False
                    break
                if not isinstance(raw_item, expected_type):
                    failure_code = "invalid_artifact"
                    eof = False
                    break
                item_id = identity(raw_item)
                if not item_id or item_id in identities:
                    failure_code = "duplicate_artifact"
                    eof = False
                    break
                identities.add(item_id)
                items.append(raw_item)
                fingerprints.append((item_id, item_fingerprint(raw_item)))
            if failure_code is not None:
                break
            eof = page.eof
            _require_continuation(should_continue)
        if failure_code is None and not eof:
            failure_code = "source_missing_eof"
    except SemanticDependencyReconciliationError:
        raise
    except SemanticDependencySourceError as error:
        failure_code = error.code.value
        eof = False
    except Exception:
        failure_code = "source_unavailable"
        eof = False

    set_fingerprint = semantic_change_fingerprint(
        {
            "source": kind.value,
            "items": tuple(sorted(fingerprints)),
        }
    )
    return _SourceScan(
        items=tuple(items),
        manifest=SemanticDependencySourceManifest(
            source=kind,
            page_count=page_count,
            discovered_count=len(items),
            eof=eof,
            set_fingerprint=set_fingerprint,
            failure_code=failure_code,
        ),
    )


def _require_continuation(callback: Callable[[], bool]) -> None:
    try:
        current = callback()
    except Exception as error:
        raise SemanticDependencyReconciliationError(
            SemanticDependencyReconciliationErrorCode.INDEX_STORE_UNAVAILABLE,
            "semantic dependency reconciliation lost its execution lease",
        ) from error
    if not isinstance(current, bool) or not current:
        raise SemanticDependencyReconciliationError(
            SemanticDependencyReconciliationErrorCode.INDEX_STORE_UNAVAILABLE,
            "semantic dependency reconciliation lost its execution lease",
        )


def _workflow_artifacts(
    scan: _SourceScan[AgentWorkflowDraft],
    registry: GovernedSemanticRegistrySnapshot,
    pointer: ActiveRegistryPointer,
) -> tuple[tuple[SemanticDependencyArtifact, ...], SemanticDependencySourceManifest]:
    exact_mappings = {
        semantic_change_fingerprint(item.model_dump(mode="json")): item
        for item in registry.mapping_set.mappings
    }
    exact_joins = {
        semantic_change_fingerprint(item.model_dump(mode="json")): item
        for item in registry.join_contracts.contracts
    }
    mappings_by_logical = _mappings_by_logical_field(registry)
    joins_by_contract = _joins_by_contract_id(registry)
    artifacts: list[SemanticDependencyArtifact] = []
    manifest = scan.manifest
    for draft in scan.items:
        try:
            mapping_ids: set[str] = set()
            joins: set[tuple[str, int]] = set()
            plan = draft.resolved_plan
            if plan is not None:
                if plan.active_scope_fingerprint != semantic_registry_scope_fingerprint(
                    pointer.scope
                ):
                    raise ValueError("workflow scope is absent or differs from the active registry")
                active_plan = (
                    plan.context_source == registry.source
                    and plan.context_version == registry.version
                    and plan.context_fingerprint == registry.fingerprint
                    and plan.activation_generation == pointer.generation
                    and plan.active_pointer_fingerprint == registry_projection_fingerprint(pointer)
                )
                for selected_mapping in plan.selected_mappings:
                    if not active_plan:
                        mapping_ids.update(
                            _current_mapping_decisions(
                                selected_mapping.mapping.logical_field.root,
                                mappings_by_logical,
                            )
                        )
                        continue
                    key = semantic_change_fingerprint(selected_mapping.model_dump(mode="json"))
                    governed_mapping = exact_mappings.get(key)
                    if (
                        governed_mapping != selected_mapping
                        or governed_mapping.approval_decision_id is None
                    ):
                        raise ValueError("active workflow mapping is absent from registry")
                    mapping_ids.add(governed_mapping.approval_decision_id)
                for selected_contract in plan.selected_contracts:
                    if not active_plan:
                        joins.update(
                            _current_join_contracts(
                                selected_contract.id,
                                joins_by_contract,
                            )
                        )
                        continue
                    key = semantic_change_fingerprint(selected_contract.model_dump(mode="json"))
                    governed_join = exact_joins.get(key)
                    if governed_join != selected_contract:
                        raise ValueError("active workflow join is absent from registry")
                    joins.add((governed_join.id, governed_join.version))
            artifacts.append(
                SemanticDependencyArtifact(
                    kind=SemanticDependencyArtifactKind.WORKFLOW,
                    artifact_id=draft.id,
                    version=draft.revision,
                    fingerprint=fingerprint_payload(draft.model_dump(mode="json")),
                    mapping_decision_ids=tuple(sorted(mapping_ids)),
                    join_contracts=tuple(sorted(joins)),
                )
            )
        except ValueError:
            manifest = _failed_manifest(manifest, "artifact_registry_mismatch")
            break
    return tuple(artifacts), manifest


def _recipe_artifacts(
    scan: _SourceScan[CurrentQueryRecipeDocument],
    registry: GovernedSemanticRegistrySnapshot,
    pointer: ActiveRegistryPointer,
) -> tuple[tuple[SemanticDependencyArtifact, ...], SemanticDependencySourceManifest]:
    mappings = {
        (
            item.mapping.logical_field.root,
            item.mapping.physical_field.root,
            item.mapping.version,
            item.approval_decision_id,
        ): item
        for item in registry.mapping_set.mappings
    }
    joins = {
        (item.id, item.version, item.approval_decision_id): item
        for item in registry.join_contracts.contracts
    }
    mappings_by_logical = _mappings_by_logical_field(registry)
    joins_by_contract = _joins_by_contract_id(registry)
    artifacts: list[SemanticDependencyArtifact] = []
    manifest = scan.manifest
    for document in scan.items:
        recipe = document.recipe
        binding = recipe.registry_binding
        if binding is None or binding.scope_fingerprint != semantic_registry_scope_fingerprint(
            pointer.scope
        ):
            manifest = _failed_manifest(manifest, "artifact_registry_mismatch")
            break
        active = (
            recipe.model_version.source == registry.source
            and recipe.model_version.version == registry.version
            and recipe.model_version.fingerprint == registry.fingerprint
            and binding.generation == pointer.generation
            and binding.pointer_fingerprint == registry_projection_fingerprint(pointer)
            and binding.registry_version == registry.version
            and binding.registry_fingerprint == registry.fingerprint
        )
        try:
            if active:
                mapping_ids = tuple(
                    sorted(
                        _exact_recipe_mapping(mapping, mappings)
                        for mapping in recipe.mapping_versions
                    )
                )
                join_contracts = tuple(
                    sorted(_exact_recipe_join(join, joins) for join in recipe.join_versions)
                )
            else:
                mapping_ids = tuple(
                    sorted(
                        {
                            decision_id
                            for mapping in recipe.mapping_versions
                            for decision_id in _current_mapping_decisions(
                                mapping.logical_field.root,
                                mappings_by_logical,
                            )
                        }
                    )
                )
                join_contracts = tuple(
                    sorted(
                        {
                            contract
                            for join in recipe.join_versions
                            for contract in _current_join_contracts(
                                join.contract_id,
                                joins_by_contract,
                            )
                        }
                    )
                )
            artifacts.append(
                SemanticDependencyArtifact(
                    kind=SemanticDependencyArtifactKind.QUERY_RECIPE,
                    artifact_id=recipe.id,
                    version=recipe.version,
                    fingerprint=recipe.fingerprint,
                    mapping_decision_ids=mapping_ids,
                    join_contracts=join_contracts,
                )
            )
        except ValueError:
            manifest = _failed_manifest(manifest, "artifact_registry_mismatch")
            break
    return tuple(artifacts), manifest


def _mappings_by_logical_field(
    registry: GovernedSemanticRegistrySnapshot,
) -> dict[str, tuple[GovernedFieldMapping, ...]]:
    grouped: dict[str, list[GovernedFieldMapping]] = {}
    for mapping in registry.mapping_set.mappings:
        grouped.setdefault(mapping.mapping.logical_field.root, []).append(mapping)
    return {
        logical_field: tuple(
            sorted(
                mappings,
                key=lambda item: (
                    item.mapping.version,
                    item.mapping.physical_field.root,
                    item.approval_decision_id or "",
                ),
            )
        )
        for logical_field, mappings in grouped.items()
    }


def _joins_by_contract_id(
    registry: GovernedSemanticRegistrySnapshot,
) -> dict[str, tuple[JoinContract, ...]]:
    grouped: dict[str, list[JoinContract]] = {}
    for contract in registry.join_contracts.contracts:
        grouped.setdefault(contract.id, []).append(contract)
    return {
        contract_id: tuple(
            sorted(
                contracts,
                key=lambda item: (item.version, item.approval_decision_id or ""),
            )
        )
        for contract_id, contracts in grouped.items()
    }


def _current_mapping_decisions(
    logical_field: str,
    mappings: Mapping[str, tuple[GovernedFieldMapping, ...]],
) -> tuple[str, ...]:
    decisions = tuple(
        sorted(
            {
                item.approval_decision_id
                for item in mappings.get(logical_field, ())
                if item.approval_decision_id is not None
            }
        )
    )
    if not decisions:
        raise ValueError("stale artifact logical field is absent from the active registry")
    return decisions


def _current_join_contracts(
    contract_id: str,
    joins: Mapping[str, tuple[JoinContract, ...]],
) -> tuple[tuple[str, int], ...]:
    contracts = tuple(sorted({(item.id, item.version) for item in joins.get(contract_id, ())}))
    if not contracts:
        raise ValueError("stale artifact join is absent from the active registry")
    return contracts


def _exact_recipe_mapping(
    mapping: object,
    registry: dict[tuple[str, str, int, str | None], GovernedFieldMapping],
) -> str:
    from schemabridge.domain.recipes import RecipeMappingVersion

    if not isinstance(mapping, RecipeMappingVersion):
        raise ValueError("query recipe mapping is invalid")
    key = (
        mapping.logical_field.root,
        mapping.physical_field.root,
        mapping.version,
        mapping.approval_decision_id,
    )
    governed = registry.get(key)
    if governed is None or governed.approval_decision_id is None:
        raise ValueError("query recipe mapping is absent from the active registry")
    return governed.approval_decision_id


def _exact_recipe_join(
    join: object,
    registry: Mapping[tuple[str, int, str | None], JoinContract],
) -> tuple[str, int]:
    from schemabridge.domain.recipes import RecipeJoinVersion

    if not isinstance(join, RecipeJoinVersion):
        raise ValueError("query recipe join is invalid")
    key = (join.contract_id, join.version, join.approval_decision_id)
    if key not in registry:
        raise ValueError("query recipe join is absent from the active registry")
    return join.contract_id, join.version


def _failed_manifest(
    manifest: SemanticDependencySourceManifest,
    failure_code: str,
) -> SemanticDependencySourceManifest:
    return replace(
        manifest,
        eof=False,
        failure_code=failure_code,
    )


def _deduplicate_artifacts(
    artifacts: tuple[SemanticDependencyArtifact, ...],
) -> tuple[SemanticDependencyArtifact, ...]:
    unique: dict[
        tuple[str, str, int],
        SemanticDependencyArtifact,
    ] = {}
    for artifact in artifacts:
        unique.setdefault(
            (artifact.kind.value, artifact.artifact_id, artifact.version),
            artifact,
        )
    return tuple(unique[key] for key in sorted(unique))


def _manifest(
    workflows: SemanticDependencySourceManifest,
    recipes: SemanticDependencySourceManifest,
    artifacts: tuple[SemanticDependencyArtifact, ...],
) -> SemanticDependencyReconciliationManifest:
    artifact_fingerprint = semantic_change_fingerprint(
        {
            "artifacts": tuple(
                {
                    "kind": item.kind.value,
                    "artifact_id": item.artifact_id,
                    "version": item.version,
                    "fingerprint": item.fingerprint,
                    "mapping_decision_ids": item.mapping_decision_ids,
                    "join_contracts": item.join_contracts,
                }
                for item in artifacts
            )
        }
    )
    manifest_payload = {
        "workflows": {
            "pages": workflows.page_count,
            "count": workflows.discovered_count,
            "eof": workflows.eof,
            "set_fingerprint": workflows.set_fingerprint,
            "failure_code": workflows.failure_code,
        },
        "recipes": {
            "pages": recipes.page_count,
            "count": recipes.discovered_count,
            "eof": recipes.eof,
            "set_fingerprint": recipes.set_fingerprint,
            "failure_code": recipes.failure_code,
        },
        "artifact_count": len(artifacts),
        "artifact_set_fingerprint": artifact_fingerprint,
    }
    return SemanticDependencyReconciliationManifest(
        workflow_source=workflows,
        recipe_source=recipes,
        artifact_count=len(artifacts),
        artifact_set_fingerprint=artifact_fingerprint,
        fingerprint=semantic_change_fingerprint(manifest_payload),
    )


__all__ = [
    "ReconcileSemanticDependencies",
    "SemanticDependencyReconciliationError",
    "SemanticDependencyReconciliationErrorCode",
    "SemanticDependencyReconciliationResult",
]
