"""Ports for bounded query-recipe retrieval and approval-gated publication."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from schemabridge.domain.recipes import (
    PublishedQueryRecipe,
    QueryRecipe,
    RecipePublicationApproval,
    RecipePublicationResult,
)


class RecipeErrorCode(StrEnum):
    NOT_FOUND = "recipe_not_found"
    WORKFLOW_STORE_UNAVAILABLE = "recipe_workflow_store_unavailable"
    ACTIVE_POINTER_NOT_FOUND = "recipe_active_pointer_not_found"
    REGISTRY_CONTROL_UNAVAILABLE = "recipe_registry_control_unavailable"
    INVALID_WORKFLOW = "recipe_invalid_workflow"
    STALE_WORKFLOW = "recipe_stale_workflow"
    INTENT_MISMATCH = "recipe_migration_intent_mismatch"
    MIGRATION_NOT_REQUIRED = "recipe_migration_not_required"
    CURRENT_CHANGED = "recipe_current_changed"
    APPROVAL_REQUIRED = "recipe_approval_required"
    APPROVAL_MISMATCH = "recipe_approval_mismatch"
    VERSION_CONFLICT = "recipe_version_conflict"
    CATALOG_UNAVAILABLE = "recipe_catalog_unavailable"
    CATALOG_PERMISSION_DENIED = "recipe_catalog_permission_denied"
    PUBLICATION_FAILED = "recipe_publication_failed"
    INVALID_RECORDED_RECIPE = "recipe_invalid_recorded_payload"


class RecipeError(RuntimeError):
    """Sanitized recipe failure safe for presentation and workflow traces."""

    def __init__(self, code: RecipeErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class QueryRecipeReadPort(Protocol):
    def find_current(
        self,
        intent_fingerprint: str,
        *,
        scope_fingerprint: str | None = None,
    ) -> PublishedQueryRecipe | None:
        """Return the current recipe for one exact normalized intent, if present."""


class QueryRecipeWritePort(Protocol):
    def publish(
        self,
        recipe: QueryRecipe,
        approval: RecipePublicationApproval,
    ) -> RecipePublicationResult:
        """Publish only a recipe covered by the exact explicit approval."""


class QueryRecipeRepositoryPort(QueryRecipeReadPort, QueryRecipeWritePort, Protocol):
    """Combined port used by explicit prepare/publish composition."""
