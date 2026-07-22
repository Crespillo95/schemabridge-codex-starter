"""Persistent, visibly fake SQLite query-recipe repository for offline demos."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pydantic import ValidationError

from schemabridge.application.ports.recipes import RecipeError, RecipeErrorCode
from schemabridge.domain.recipes import (
    PublishedQueryRecipe,
    QueryRecipe,
    RecipePublicationApproval,
    RecipePublicationResult,
    RecipePublicationStatus,
)


class SqliteQueryRecipeRepository:
    """Persist immutable recipe versions without simulating a DataHub identity."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def find_current(self, intent_fingerprint: str) -> PublishedQueryRecipe | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT payload, current_document_urn, versioned_document_urn,
                           approval_id, published_at
                    FROM fake_query_recipes WHERE intent_fingerprint = ? AND is_current = 1
                    """,
                    (intent_fingerprint,),
                ).fetchone()
            if row is None:
                return None
            return PublishedQueryRecipe(
                recipe=QueryRecipe.model_validate_json(row[0]),
                document_urn=str(row[1]),
                versioned_document_urn=str(row[2]),
                approval_id=str(row[3]),
                published_at=str(row[4]),
            )
        except ValidationError as error:
            raise RecipeError(
                RecipeErrorCode.INVALID_RECORDED_RECIPE,
                "fake recipe store contains an invalid typed payload",
            ) from error
        except sqlite3.Error as error:
            raise RecipeError(
                RecipeErrorCode.CATALOG_UNAVAILABLE,
                "fake recipe store is unavailable",
            ) from error

    def publish(
        self,
        recipe: QueryRecipe,
        approval: RecipePublicationApproval,
    ) -> RecipePublicationResult:
        _validate_approval(recipe, approval)
        current_document_urn = f"fake://query-recipe/{recipe.intent_fingerprint}/current"
        versioned_document_urn = (
            f"fake://query-recipe/{recipe.intent_fingerprint}/v{recipe.version}"
        )
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """
                    SELECT payload, current_document_urn, versioned_document_urn, published_at
                    FROM fake_query_recipes WHERE intent_fingerprint = ? AND is_current = 1
                    """,
                    (recipe.intent_fingerprint,),
                ).fetchone()
                if existing is not None:
                    current_recipe = QueryRecipe.model_validate_json(existing[0])
                    if current_recipe.fingerprint == recipe.fingerprint:
                        connection.commit()
                        return RecipePublicationResult(
                            status=RecipePublicationStatus.ALREADY_CURRENT,
                            recipe_fingerprint=recipe.fingerprint,
                            current_document_urn=str(existing[1]),
                            versioned_document_urn=str(existing[2]),
                            published_at=str(existing[3]),
                        )
                version_conflict = connection.execute(
                    """
                    SELECT fingerprint FROM fake_query_recipes
                    WHERE intent_fingerprint = ? AND version = ?
                    """,
                    (recipe.intent_fingerprint, recipe.version),
                ).fetchone()
                if version_conflict is not None:
                    raise RecipeError(
                        RecipeErrorCode.VERSION_CONFLICT,
                        "query recipe version already identifies different immutable content",
                    )
                connection.execute(
                    "UPDATE fake_query_recipes SET is_current = 0 WHERE intent_fingerprint = ?",
                    (recipe.intent_fingerprint,),
                )
                connection.execute(
                    """
                    INSERT INTO fake_query_recipes (
                        intent_fingerprint, version, fingerprint, payload, is_current,
                        current_document_urn, versioned_document_urn, approval_id, published_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)
                    """,
                    (
                        recipe.intent_fingerprint,
                        recipe.version,
                        recipe.fingerprint,
                        recipe.model_dump_json(),
                        current_document_urn,
                        versioned_document_urn,
                        approval.id,
                        approval.approved_at.isoformat(),
                    ),
                )
                connection.commit()
            return RecipePublicationResult(
                status=RecipePublicationStatus.CREATED,
                recipe_fingerprint=recipe.fingerprint,
                current_document_urn=current_document_urn,
                versioned_document_urn=versioned_document_urn,
                published_at=approval.approved_at,
            )
        except RecipeError:
            raise
        except (ValidationError, sqlite3.Error) as error:
            raise RecipeError(
                RecipeErrorCode.PUBLICATION_FAILED,
                "fake query-recipe publication failed",
            ) from error

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, isolation_level=None, timeout=5.0)

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS fake_query_recipes (
                        intent_fingerprint TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        fingerprint TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        is_current INTEGER NOT NULL,
                        current_document_urn TEXT NOT NULL,
                        versioned_document_urn TEXT NOT NULL,
                        approval_id TEXT NOT NULL,
                        published_at TEXT NOT NULL,
                        PRIMARY KEY (intent_fingerprint, version)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS one_current_fake_query_recipe
                    ON fake_query_recipes(intent_fingerprint) WHERE is_current = 1
                    """
                )
        except sqlite3.Error as error:
            raise RecipeError(
                RecipeErrorCode.CATALOG_UNAVAILABLE,
                "fake query-recipe store could not initialize",
            ) from error


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
            "query-recipe approval does not match the reviewed payload",
        )
