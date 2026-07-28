"""Ports and immutable values for complete semantic-dependency discovery."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Generic, Protocol, TypeVar

from schemabridge.domain.recipes import QueryRecipe
from schemabridge.domain.semantic_change import SemanticDependencyIndexState
from schemabridge.domain.semantic_registry import SemanticRegistryScope
from schemabridge.domain.workflows import AgentWorkflowDraft

MAX_SEMANTIC_DEPENDENCY_PAGE_SIZE = 50
MAX_SEMANTIC_DEPENDENCY_ARTIFACTS = 10_000
MAX_SEMANTIC_DEPENDENCY_EDGES = 100_000

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_FAILURE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_CURRENT_RECIPE_URN = re.compile(
    r"^urn:li:document:schemabridge-query-recipe-"
    r"(?:[0-9a-f]{32}-)?[0-9a-f]{32}-current$"
)


class SemanticDependencyArtifactKind(StrEnum):
    """Managed artifact families represented in the blast-radius index."""

    WORKFLOW = "workflow"
    QUERY_RECIPE = "query_recipe"


class SemanticDependencySourceErrorCode(StrEnum):
    """Sanitized source failures which must make reconciliation incomplete."""

    UNAVAILABLE = "source_unavailable"
    PERMISSION_DENIED = "source_permission_denied"
    INVALID_RESPONSE = "source_invalid_response"
    INVALID_ARTIFACT = "source_invalid_artifact"


class SemanticDependencySourceError(RuntimeError):
    """One safe source failure without vendor payloads or protected content."""

    def __init__(
        self,
        code: SemanticDependencySourceErrorCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class CurrentQueryRecipeDocument:
    """One strictly validated live DataHub current-marker document."""

    document_urn: str
    versioned_document_urn: str
    approval_id: str
    published_at: datetime
    recipe: QueryRecipe

    def __post_init__(self) -> None:
        expected_current = (
            "urn:li:document:schemabridge-query-recipe-"
            f"{_recipe_identity_prefix(self.recipe)}-current"
        )
        expected_version = (
            "urn:li:document:schemabridge-query-recipe-"
            f"{_recipe_identity_prefix(self.recipe)}-v{self.recipe.version}"
        )
        if (
            _CURRENT_RECIPE_URN.fullmatch(self.document_urn) is None
            or self.document_urn != expected_current
            or self.versioned_document_urn != expected_version
            or not self.approval_id.strip()
            or len(self.approval_id) > 200
            or self.published_at.tzinfo is None
            or self.published_at.utcoffset() is None
        ):
            raise ValueError("current query-recipe document identity is invalid")


def _recipe_identity_prefix(recipe: QueryRecipe) -> str:
    binding = recipe.registry_binding
    if binding is None or binding.scope_fingerprint is None:
        return recipe.intent_fingerprint[:32]
    return f"{binding.scope_fingerprint[:32]}-{recipe.intent_fingerprint[:32]}"


PageItemT = TypeVar("PageItemT")


@dataclass(frozen=True, slots=True)
class SemanticDependencySourcePage(Generic[PageItemT]):
    """One bounded source page; terminal evidence is explicit."""

    sequence: int
    items: tuple[PageItemT, ...]
    source_position: str
    eof: bool

    def __post_init__(self) -> None:
        if (
            self.sequence < 1
            or len(self.items) > MAX_SEMANTIC_DEPENDENCY_PAGE_SIZE
            or not self.source_position
            or len(self.source_position.encode("utf-8")) > 1_024
        ):
            raise ValueError("semantic dependency source page is invalid")


@dataclass(frozen=True, slots=True)
class SemanticDependencyArtifact:
    """Vendor-neutral dependency material accepted by the durable index sink."""

    kind: SemanticDependencyArtifactKind
    artifact_id: str
    version: int
    fingerprint: str
    mapping_decision_ids: tuple[str, ...] = ()
    join_contracts: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if (
            not self.artifact_id.strip()
            or len(self.artifact_id) > 500
            or self.version < 1
            or _SHA256.fullmatch(self.fingerprint) is None
            or self.mapping_decision_ids != tuple(sorted(set(self.mapping_decision_ids)))
            or self.join_contracts != tuple(sorted(set(self.join_contracts)))
            or any(not decision.strip() for decision in self.mapping_decision_ids)
            or any(
                not contract_id.strip() or version < 1
                for contract_id, version in self.join_contracts
            )
        ):
            raise ValueError("semantic dependency artifact is invalid")


@dataclass(frozen=True, slots=True)
class SemanticDependencySourceManifest:
    """Observed coverage for one complete or fail-closed source traversal."""

    source: SemanticDependencyArtifactKind
    page_count: int
    discovered_count: int
    eof: bool
    set_fingerprint: str
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if (
            self.page_count < 0
            or self.discovered_count < 0
            or _SHA256.fullmatch(self.set_fingerprint) is None
            or (
                self.failure_code is not None and _SAFE_FAILURE.fullmatch(self.failure_code) is None
            )
            or (self.eof and self.failure_code is not None)
        ):
            raise ValueError("semantic dependency source manifest is invalid")

    @property
    def complete(self) -> bool:
        return self.eof and self.failure_code is None


@dataclass(frozen=True, slots=True)
class SemanticDependencyReconciliationManifest:
    """Coverage proof from both authoritative managed artifact sources."""

    workflow_source: SemanticDependencySourceManifest
    recipe_source: SemanticDependencySourceManifest
    artifact_count: int
    artifact_set_fingerprint: str
    fingerprint: str

    def __post_init__(self) -> None:
        if (
            self.workflow_source.source is not SemanticDependencyArtifactKind.WORKFLOW
            or self.recipe_source.source is not SemanticDependencyArtifactKind.QUERY_RECIPE
            or self.artifact_count < 0
            or _SHA256.fullmatch(self.artifact_set_fingerprint) is None
            or _SHA256.fullmatch(self.fingerprint) is None
        ):
            raise ValueError("semantic dependency reconciliation manifest is invalid")

    @property
    def complete(self) -> bool:
        return self.workflow_source.complete and self.recipe_source.complete


@dataclass(frozen=True, slots=True)
class SemanticDependencyIndexWrite:
    """One application-validated dependency snapshot for durable reconciliation."""

    scope: SemanticRegistryScope
    registry_generation: int
    registry_version: int
    registry_fingerprint: str
    pointer_transition_id: str
    watermark: int
    artifacts: tuple[SemanticDependencyArtifact, ...]
    manifest: SemanticDependencyReconciliationManifest
    indexed_at: datetime

    def __post_init__(self) -> None:
        identities = tuple(
            (artifact.kind.value, artifact.artifact_id, artifact.version)
            for artifact in self.artifacts
        )
        if (
            self.registry_generation < 1
            or self.registry_version < 1
            or _SHA256.fullmatch(self.registry_fingerprint) is None
            or not self.pointer_transition_id.strip()
            or self.watermark < 1
            or self.indexed_at.tzinfo is None
            or self.indexed_at.utcoffset() is None
            or identities != tuple(sorted(set(identities)))
            or self.manifest.artifact_count != len(self.artifacts)
            or len(self.artifacts) > MAX_SEMANTIC_DEPENDENCY_ARTIFACTS
            or sum(
                len(artifact.mapping_decision_ids) + len(artifact.join_contracts)
                for artifact in self.artifacts
            )
            > MAX_SEMANTIC_DEPENDENCY_EDGES
        ):
            raise ValueError("semantic dependency index write is invalid")

    @property
    def complete(self) -> bool:
        return self.manifest.complete


class ManagedWorkflowDependencySourcePort(Protocol):
    """Repeatable, workspace-scoped traversal of every managed workflow draft."""

    def pages(
        self,
        scope: SemanticRegistryScope,
        *,
        page_size: int,
    ) -> Iterator[SemanticDependencySourcePage[AgentWorkflowDraft]]:
        """Yield bounded keyset pages and exactly one terminal page."""


class QueryRecipeDependencySourcePort(Protocol):
    """Mutation-free traversal of every live DataHub query-recipe marker."""

    def pages(
        self,
        scope: SemanticRegistryScope,
        *,
        page_size: int,
    ) -> Iterator[SemanticDependencySourcePage[CurrentQueryRecipeDocument]]:
        """Yield bounded stable-scroll pages and exactly one terminal page."""


class SemanticDependencyIndexWritePort(Protocol):
    """Durably reconcile one complete or explicitly incomplete snapshot."""

    def reconcile(
        self,
        snapshot: SemanticDependencyIndexWrite,
    ) -> SemanticDependencyIndexState:
        """Persist exact dependencies and their fail-closed completeness state."""


__all__ = [
    "MAX_SEMANTIC_DEPENDENCY_ARTIFACTS",
    "MAX_SEMANTIC_DEPENDENCY_EDGES",
    "MAX_SEMANTIC_DEPENDENCY_PAGE_SIZE",
    "CurrentQueryRecipeDocument",
    "ManagedWorkflowDependencySourcePort",
    "QueryRecipeDependencySourcePort",
    "SemanticDependencyArtifact",
    "SemanticDependencyArtifactKind",
    "SemanticDependencyIndexWrite",
    "SemanticDependencyIndexWritePort",
    "SemanticDependencyReconciliationManifest",
    "SemanticDependencySourceError",
    "SemanticDependencySourceErrorCode",
    "SemanticDependencySourceManifest",
    "SemanticDependencySourcePage",
]
