"""Approval-gated DataHub document adapter for versioned query recipes."""

from __future__ import annotations

import json
import logging
import stat
import urllib.error
import urllib.request
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from schemabridge.adapters.datahub.canonical_urns import DECISION_PROPERTY_URN
from schemabridge.adapters.datahub.decision_property import ensure_decision_property
from schemabridge.application.ports.recipes import RecipeError, RecipeErrorCode
from schemabridge.domain.recipes import (
    PublishedQueryRecipe,
    QueryRecipe,
    RecipePublicationApproval,
    RecipePublicationResult,
    RecipePublicationStatus,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DataHubQueryRecipeConfig:
    server: str
    token: str
    actor_urn: str


class DataHubQueryRecipeAdapter:
    """Write an immutable recipe document, then atomically advance its current marker."""

    def __init__(self, config: DataHubQueryRecipeConfig) -> None:
        self._config = config

    @classmethod
    def from_env_file(cls, path: Path) -> DataHubQueryRecipeAdapter:
        if not path.is_file():
            raise RecipeError(
                RecipeErrorCode.CATALOG_UNAVAILABLE,
                "DataHub writer credential is absent; run make datahub-provision-writer",
            )
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise RecipeError(
                RecipeErrorCode.CATALOG_PERMISSION_DENIED,
                "DataHub writer credential must be accessible only to its owner",
            )
        values = _read_env_file(path)
        try:
            config = DataHubQueryRecipeConfig(
                server=values["DATAHUB_GMS_URL"].rstrip("/"),
                token=values["DATAHUB_GMS_TOKEN"],
                actor_urn=values["DATAHUB_WRITER_ACTOR_URN"],
            )
        except KeyError as error:
            raise RecipeError(
                RecipeErrorCode.CATALOG_UNAVAILABLE,
                "DataHub writer credential is incomplete",
            ) from error
        if not config.server.startswith(("http://", "https://")) or not config.token:
            raise RecipeError(
                RecipeErrorCode.CATALOG_UNAVAILABLE,
                "DataHub writer credential is invalid",
            )
        return cls(config)

    def find_current(self, intent_fingerprint: str) -> PublishedQueryRecipe | None:
        document_urn = _current_document_urn(intent_fingerprint)
        try:
            from datahub.metadata.schema_classes import DocumentInfoClass

            self._verify_runtime_identity()
            document = self._client()._graph.get_aspect(document_urn, DocumentInfoClass)
            if document is None:
                return None
            serialized = document.customProperties.get("schemabridge.queryRecipe")
            fingerprint = document.customProperties.get("schemabridge.recipeFingerprint")
            approval_id = document.customProperties.get("schemabridge.approvalId")
            published_at = document.customProperties.get("schemabridge.publishedAt")
            versioned_urn = document.customProperties.get("schemabridge.versionedDocumentUrn")
            if not all((serialized, fingerprint, approval_id, published_at, versioned_urn)):
                raise ValueError("query-recipe marker is incomplete")
            recipe = QueryRecipe.model_validate_json(serialized)
            if (
                recipe.intent_fingerprint != intent_fingerprint
                or recipe.fingerprint != fingerprint
                or set(recipe.linked_asset_urns)
                != {asset.asset for asset in document.relatedAssets or ()}
            ):
                raise ValueError("query-recipe marker failed typed read-back validation")
            return PublishedQueryRecipe(
                recipe=recipe,
                document_urn=document_urn,
                versioned_document_urn=versioned_urn,
                approval_id=approval_id,
                published_at=datetime.fromisoformat(published_at),
            )
        except RecipeError:
            raise
        except ValidationError as error:
            raise RecipeError(
                RecipeErrorCode.INVALID_RECORDED_RECIPE,
                "DataHub query-recipe payload is invalid",
            ) from error
        except Exception as error:
            raise RecipeError(
                _read_error_code(error),
                "DataHub query-recipe read failed",
            ) from error

    def publish(
        self,
        recipe: QueryRecipe,
        approval: RecipePublicationApproval,
    ) -> RecipePublicationResult:
        _validate_approval(recipe, approval)
        current_urn = _current_document_urn(recipe.intent_fingerprint)
        versioned_urn = _versioned_document_urn(recipe)
        current = self.find_current(recipe.intent_fingerprint)
        if current is not None and current.recipe.fingerprint == recipe.fingerprint:
            return RecipePublicationResult(
                status=RecipePublicationStatus.ALREADY_CURRENT,
                recipe_fingerprint=recipe.fingerprint,
                current_document_urn=current.document_urn,
                versioned_document_urn=current.versioned_document_urn,
                published_at=current.published_at,
            )
        try:
            client = self._client()
            existing_version = self._version_fingerprint(client, versioned_urn)
            if existing_version is not None and existing_version != recipe.fingerprint:
                raise RecipeError(
                    RecipeErrorCode.VERSION_CONFLICT,
                    "DataHub query-recipe version already identifies different immutable content",
                )
            if existing_version is None:
                logger.info(
                    "datahub_recipe_publish operation=versioned_document target=%s",
                    versioned_urn,
                )
                self._upsert_document(client, recipe, approval, current_marker=False)
            logger.info("datahub_recipe_publish operation=current_marker target=%s", current_urn)
            self._upsert_document(client, recipe, approval, current_marker=True)
            loaded = self.find_current(recipe.intent_fingerprint)
            if loaded is None or loaded.recipe.fingerprint != recipe.fingerprint:
                raise ValueError("query-recipe read-back did not match")
            return RecipePublicationResult(
                status=RecipePublicationStatus.CREATED,
                recipe_fingerprint=recipe.fingerprint,
                current_document_urn=current_urn,
                versioned_document_urn=versioned_urn,
                published_at=loaded.published_at,
            )
        except RecipeError:
            raise
        except Exception:
            logger.warning(
                "datahub_recipe_publish_failed operation=query_recipe target=%s code=partial_write",
                current_urn,
            )
            return RecipePublicationResult(
                status=RecipePublicationStatus.PARTIAL_FAILURE,
                recipe_fingerprint=recipe.fingerprint,
                current_document_urn=current_urn,
                versioned_document_urn=versioned_urn,
                published_at=approval.approved_at,
                failure_code="datahub_partial_write",
            )

    @staticmethod
    def _version_fingerprint(client: Any, document_urn: str) -> str | None:
        from datahub.metadata.schema_classes import DocumentInfoClass

        document = client._graph.get_aspect(document_urn, DocumentInfoClass)
        if document is None:
            return None
        fingerprint = document.customProperties.get("schemabridge.recipeFingerprint")
        if not fingerprint:
            raise RecipeError(
                RecipeErrorCode.INVALID_RECORDED_RECIPE,
                "DataHub versioned query-recipe document is incomplete",
            )
        return str(fingerprint)

    def _client(self) -> Any:
        try:
            from datahub.sdk.main_client import DataHubClient

            return DataHubClient(server=self._config.server, token=self._config.token)
        except ModuleNotFoundError as error:
            raise RecipeError(
                RecipeErrorCode.CATALOG_UNAVAILABLE,
                "DataHub support is not installed; install schemabridge[datahub]",
            ) from error

    def _upsert_document(
        self,
        client: Any,
        recipe: QueryRecipe,
        approval: RecipePublicationApproval,
        *,
        current_marker: bool,
    ) -> None:
        from datahub.errors import IngestionAttributionWarning
        from datahub.sdk.document import Document

        ensure_decision_property(self._graphql)

        document_id = (
            _current_document_id(recipe.intent_fingerprint)
            if current_marker
            else _versioned_document_id(recipe)
        )
        title = (
            "SchemaBridge current validated query recipe"
            if current_marker
            else f"SchemaBridge validated query recipe {recipe.id} v{recipe.version}"
        )
        document = Document.create_document(
            id=document_id,
            title=title,
            text=(
                "# Validated SchemaBridge query recipe\n\n"
                "Synthetic demo context only. This document contains normalized intent and "
                "governance fingerprints, not executable SQL, parameters, credentials, preview "
                "rows, prompts, or private reasoning. Reuse must replan, revalidate, and execute "
                "through the bounded read-only path.\n\n"
                f"Recipe: `{recipe.id}` v{recipe.version}\n\n"
                f"Source workflow: `{recipe.source_workflow_id}`\n\n"
                f"Plan fingerprint: `{recipe.plan_fingerprint}`\n"
            ),
            related_assets=recipe.linked_asset_urns,
            custom_properties={
                "schemabridge.queryRecipe": recipe.model_dump_json(),
                "schemabridge.recipeFingerprint": recipe.fingerprint,
                "schemabridge.recipeContentFingerprint": recipe.content_fingerprint,
                "schemabridge.intentFingerprint": recipe.intent_fingerprint,
                "schemabridge.contextFingerprint": recipe.model_version.fingerprint,
                "schemabridge.planFingerprint": recipe.plan_fingerprint,
                "schemabridge.queryFingerprint": recipe.query_fingerprint,
                "schemabridge.compilerVersion": recipe.compiler_version,
                "schemabridge.approvalId": approval.id,
                "schemabridge.approvedBy": approval.actor,
                "schemabridge.publishedAt": approval.approved_at.isoformat(),
                "schemabridge.versionedDocumentUrn": _versioned_document_urn(recipe),
            },
            structured_properties={DECISION_PROPERTY_URN: [approval.id]},
        )
        with warnings.catch_warnings():
            # Updating the deterministic current marker is intentionally a bounded overwrite.
            warnings.simplefilter("ignore", IngestionAttributionWarning)
            client.entities.upsert(document)

    def _verify_runtime_identity(self) -> None:
        query = """
        query VerifyQueryRecipeWriterRuntime {
          me {
            corpUser { urn }
            platformPrivileges {
              managePolicies manageIdentities manageIngestion manageSecrets manageTokens
              manageServiceAccounts manageDocuments manageStructuredProperties
              viewStructuredPropertiesPage
            }
          }
        }
        """
        me = self._graphql(query, {}).get("me")
        if not isinstance(me, dict):
            raise RecipeError(
                RecipeErrorCode.CATALOG_PERMISSION_DENIED,
                "DataHub query-recipe writer identity could not be verified",
            )
        corp_user = me.get("corpUser")
        privileges = me.get("platformPrivileges")
        required = {"manageDocuments", "manageStructuredProperties", "viewStructuredPropertiesPage"}
        forbidden = {
            "managePolicies",
            "manageIdentities",
            "manageIngestion",
            "manageSecrets",
            "manageTokens",
            "manageServiceAccounts",
        }
        if (
            not isinstance(corp_user, dict)
            or corp_user.get("urn") != self._config.actor_urn
            or not isinstance(privileges, dict)
            or any(not privileges.get(name) for name in required)
            or any(privileges.get(name) for name in forbidden)
        ):
            raise RecipeError(
                RecipeErrorCode.CATALOG_PERMISSION_DENIED,
                "DataHub query-recipe writer identity or privileges are outside policy",
            )

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self._config.server}/api/graphql",
            data=json.dumps({"query": query, "variables": variables}).encode(),
            headers={
                "Authorization": f"Bearer {self._config.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
        if not isinstance(payload, dict) or payload.get("errors"):
            raise ValueError("DataHub GraphQL returned an error")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("DataHub GraphQL returned no data")
        return data


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


def _current_document_id(intent_fingerprint: str) -> str:
    return f"schemabridge-query-recipe-{intent_fingerprint[:32]}-current"


def _current_document_urn(intent_fingerprint: str) -> str:
    return f"urn:li:document:{_current_document_id(intent_fingerprint)}"


def _versioned_document_id(recipe: QueryRecipe) -> str:
    return f"schemabridge-query-recipe-{recipe.intent_fingerprint[:32]}-v{recipe.version}"


def _versioned_document_urn(recipe: QueryRecipe) -> str:
    return f"urn:li:document:{_versioned_document_id(recipe)}"


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _read_error_code(error: Exception) -> RecipeErrorCode:
    if isinstance(error, urllib.error.HTTPError) and error.code in {401, 403}:
        return RecipeErrorCode.CATALOG_PERMISSION_DENIED
    return RecipeErrorCode.CATALOG_UNAVAILABLE
