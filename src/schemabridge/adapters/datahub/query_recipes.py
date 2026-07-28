"""Approval-gated DataHub document adapter for versioned query recipes."""

from __future__ import annotations

import json
import logging
import re
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
from schemabridge.domain.publication_audit import (
    PublicationAuditOutcome,
    PublicationFamily,
    PublicationTargetAuditRecord,
)
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

    def find_current(
        self,
        intent_fingerprint: str,
        *,
        scope_fingerprint: str | None = None,
    ) -> PublishedQueryRecipe | None:
        document_urn = _current_document_urn(
            intent_fingerprint,
            scope_fingerprint=scope_fingerprint,
        )
        try:
            from datahub.metadata.schema_classes import DocumentInfoClass

            self._verify_runtime_identity()
            client = self._client()
            document = client._graph.get_aspect(document_urn, DocumentInfoClass)
            if document is None:
                return None
            serialized = document.customProperties.get("schemabridge.queryRecipe")
            fingerprint = document.customProperties.get("schemabridge.recipeFingerprint")
            approval_id = document.customProperties.get("schemabridge.approvalId")
            approved_by = document.customProperties.get("schemabridge.approvedBy")
            published_at = document.customProperties.get("schemabridge.publishedAt")
            versioned_urn = document.customProperties.get("schemabridge.versionedDocumentUrn")
            serialized_audit = document.customProperties.get("schemabridge.publicationAudit")
            if not all(
                (serialized, fingerprint, approval_id, approved_by, published_at, versioned_urn)
            ):
                raise ValueError("query-recipe marker is incomplete")
            recipe = QueryRecipe.model_validate_json(serialized)
            parsed_published_at = datetime.fromisoformat(published_at)
            audit_records = (
                tuple(
                    PublicationTargetAuditRecord.model_validate(item)
                    for item in json.loads(serialized_audit)
                )
                if serialized_audit is not None
                else None
            )
            expected_audit_targets = {
                "versioned_document": versioned_urn,
                "current_marker": document_urn,
            }
            if (
                recipe.intent_fingerprint != intent_fingerprint
                or _recipe_scope_fingerprint(recipe) != scope_fingerprint
                or recipe.fingerprint != fingerprint
                or self._version_fingerprint(client, versioned_urn) != fingerprint
                or (
                    audit_records is not None
                    and (
                        len(audit_records) != 2
                        or {record.operation for record in audit_records}
                        != {"versioned_document", "current_marker"}
                        or any(
                            record.family is not PublicationFamily.RECIPE
                            or record.target != expected_audit_targets[record.operation]
                            or record.approval_id != approval_id
                            or record.actor != approved_by
                            or record.approved_at != parsed_published_at
                            or record.new_fingerprint != fingerprint
                            or record.outcome
                            not in {
                                PublicationAuditOutcome.SUCCEEDED,
                                PublicationAuditOutcome.ALREADY_CURRENT,
                            }
                            or record.reason_code is not None
                            or record.decision_ids
                            for record in audit_records
                        )
                    )
                )
                or set(recipe.linked_asset_urns)
                != {asset.asset for asset in document.relatedAssets or ()}
            ):
                raise ValueError("query-recipe marker failed typed read-back validation")
            return PublishedQueryRecipe(
                recipe=recipe,
                document_urn=document_urn,
                versioned_document_urn=versioned_urn,
                approval_id=approval_id,
                published_at=parsed_published_at,
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
        if (
            recipe.registry_binding is not None
            and recipe.registry_binding.scope_fingerprint is None
        ):
            raise RecipeError(
                RecipeErrorCode.INVALID_RECORDED_RECIPE,
                "active query recipe lacks an exact tenant scope binding",
            )
        scope_fingerprint = _recipe_scope_fingerprint(recipe)
        current_urn = _current_document_urn(
            recipe.intent_fingerprint,
            scope_fingerprint=scope_fingerprint,
        )
        versioned_urn = _versioned_document_urn(recipe)
        current = self.find_current(
            recipe.intent_fingerprint,
            scope_fingerprint=scope_fingerprint,
        )
        if current is not None and current.recipe.fingerprint == recipe.fingerprint:
            return RecipePublicationResult(
                status=RecipePublicationStatus.ALREADY_CURRENT,
                approval_id=approval.id,
                recipe_fingerprint=recipe.fingerprint,
                current_document_urn=current.document_urn,
                versioned_document_urn=current.versioned_document_urn,
                published_at=current.published_at,
                audit_records=_audit_records(
                    recipe,
                    approval,
                    current_urn=current.document_urn,
                    versioned_urn=current.versioned_document_urn,
                    current_previous=recipe.fingerprint,
                    versioned_previous=recipe.fingerprint,
                    current_outcome=PublicationAuditOutcome.ALREADY_CURRENT,
                    versioned_outcome=PublicationAuditOutcome.ALREADY_CURRENT,
                ),
            )
        previous_fingerprint = current.recipe.fingerprint if current is not None else None
        existing_version: str | None = None
        versioned_outcome = PublicationAuditOutcome.NOT_ATTEMPTED
        current_outcome = PublicationAuditOutcome.NOT_ATTEMPTED
        active_operation = "versioned_document"
        mutation_attempted = False
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
                version_audit = _audit_record(
                    recipe,
                    approval,
                    operation="versioned_document",
                    target=versioned_urn,
                    previous_fingerprint=None,
                    outcome=PublicationAuditOutcome.SUCCEEDED,
                )
                mutation_attempted = True
                self._upsert_document(
                    client,
                    recipe,
                    approval,
                    current_marker=False,
                    audit_records=(version_audit,),
                )
                if self._version_fingerprint(client, versioned_urn) != recipe.fingerprint:
                    raise ValueError("DataHub query-recipe version read-back did not match")
                versioned_outcome = PublicationAuditOutcome.SUCCEEDED
            else:
                versioned_outcome = PublicationAuditOutcome.ALREADY_CURRENT
            active_operation = "current_marker"
            logger.info("datahub_recipe_publish operation=current_marker target=%s", current_urn)
            success_audits = _audit_records(
                recipe,
                approval,
                current_urn=current_urn,
                versioned_urn=versioned_urn,
                current_previous=previous_fingerprint,
                versioned_previous=existing_version,
                current_outcome=PublicationAuditOutcome.SUCCEEDED,
                versioned_outcome=versioned_outcome,
            )
            mutation_attempted = True
            self._upsert_document(
                client,
                recipe,
                approval,
                current_marker=True,
                audit_records=success_audits,
            )
            loaded = self.find_current(
                recipe.intent_fingerprint,
                scope_fingerprint=scope_fingerprint,
            )
            if loaded is None or loaded.recipe.fingerprint != recipe.fingerprint:
                raise ValueError("query-recipe read-back did not match")
            current_outcome = PublicationAuditOutcome.SUCCEEDED
            return RecipePublicationResult(
                status=RecipePublicationStatus.CREATED,
                approval_id=approval.id,
                recipe_fingerprint=recipe.fingerprint,
                current_document_urn=current_urn,
                versioned_document_urn=versioned_urn,
                published_at=loaded.published_at,
                audit_records=success_audits,
            )
        except Exception as error:
            if isinstance(error, RecipeError) and not mutation_attempted:
                raise
            logger.warning(
                "datahub_recipe_publish_failed operation=query_recipe target=%s code=partial_write",
                current_urn,
            )
            if active_operation == "versioned_document":
                versioned_outcome = PublicationAuditOutcome.FAILED
                current_outcome = PublicationAuditOutcome.NOT_ATTEMPTED
            else:
                current_outcome = PublicationAuditOutcome.FAILED
            return RecipePublicationResult(
                status=RecipePublicationStatus.PARTIAL_FAILURE,
                approval_id=approval.id,
                recipe_fingerprint=recipe.fingerprint,
                current_document_urn=current_urn,
                versioned_document_urn=versioned_urn,
                published_at=approval.approved_at,
                failure_code="datahub_partial_write",
                audit_records=_audit_records(
                    recipe,
                    approval,
                    current_urn=current_urn,
                    versioned_urn=versioned_urn,
                    current_previous=previous_fingerprint,
                    versioned_previous=existing_version,
                    current_outcome=current_outcome,
                    versioned_outcome=versioned_outcome,
                    failure_code="datahub_partial_write",
                ),
            )

    @staticmethod
    def _version_fingerprint(client: Any, document_urn: str) -> str | None:
        from datahub.metadata.schema_classes import DocumentInfoClass

        document = client._graph.get_aspect(document_urn, DocumentInfoClass)
        if document is None:
            return None
        fingerprint = document.customProperties.get("schemabridge.recipeFingerprint")
        if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
            raise RecipeError(
                RecipeErrorCode.INVALID_RECORDED_RECIPE,
                "DataHub versioned query-recipe fingerprint is invalid",
            )
        serialized = document.customProperties.get("schemabridge.queryRecipe")
        if not isinstance(serialized, str):
            raise RecipeError(
                RecipeErrorCode.INVALID_RECORDED_RECIPE,
                "DataHub versioned query-recipe document is incomplete",
            )
        try:
            recipe = QueryRecipe.model_validate_json(serialized)
        except ValidationError as error:
            raise RecipeError(
                RecipeErrorCode.INVALID_RECORDED_RECIPE,
                "DataHub versioned query-recipe payload is invalid",
            ) from error
        if (
            recipe.fingerprint != fingerprint
            or _versioned_document_urn(recipe) != document_urn
            or set(recipe.linked_asset_urns)
            != {asset.asset for asset in document.relatedAssets or ()}
        ):
            raise RecipeError(
                RecipeErrorCode.INVALID_RECORDED_RECIPE,
                "DataHub versioned query-recipe document failed typed validation",
            )
        return fingerprint

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
        audit_records: tuple[PublicationTargetAuditRecord, ...],
    ) -> None:
        from datahub.errors import IngestionAttributionWarning
        from datahub.sdk.document import Document

        ensure_decision_property(self._graphql)

        document_id = (
            _current_document_id(
                recipe.intent_fingerprint,
                scope_fingerprint=_recipe_scope_fingerprint(recipe),
            )
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
                "schemabridge.publicationAudit": json.dumps(
                    [record.model_dump(mode="json") for record in audit_records],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
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


def _audit_records(
    recipe: QueryRecipe,
    approval: RecipePublicationApproval,
    *,
    current_urn: str,
    versioned_urn: str,
    current_previous: str | None,
    versioned_previous: str | None,
    current_outcome: PublicationAuditOutcome,
    versioned_outcome: PublicationAuditOutcome,
    failure_code: str | None = None,
) -> tuple[PublicationTargetAuditRecord, PublicationTargetAuditRecord]:
    return (
        _audit_record(
            recipe,
            approval,
            operation="versioned_document",
            target=versioned_urn,
            previous_fingerprint=versioned_previous,
            outcome=versioned_outcome,
            reason_code=(
                failure_code
                if versioned_outcome
                in {PublicationAuditOutcome.FAILED, PublicationAuditOutcome.NOT_ATTEMPTED}
                else None
            ),
        ),
        _audit_record(
            recipe,
            approval,
            operation="current_marker",
            target=current_urn,
            previous_fingerprint=current_previous,
            outcome=current_outcome,
            reason_code=(
                failure_code
                if current_outcome
                in {PublicationAuditOutcome.FAILED, PublicationAuditOutcome.NOT_ATTEMPTED}
                else None
            ),
        ),
    )


def _audit_record(
    recipe: QueryRecipe,
    approval: RecipePublicationApproval,
    *,
    operation: str,
    target: str,
    previous_fingerprint: str | None,
    outcome: PublicationAuditOutcome,
    reason_code: str | None = None,
) -> PublicationTargetAuditRecord:
    return PublicationTargetAuditRecord(
        family=PublicationFamily.RECIPE,
        operation=operation,
        target=target,
        approval_id=approval.id,
        actor=approval.actor,
        approved_at=approval.approved_at,
        previous_fingerprint=previous_fingerprint,
        new_fingerprint=recipe.fingerprint,
        outcome=outcome,
        reason_code=reason_code,
    )


def _current_document_id(
    intent_fingerprint: str,
    *,
    scope_fingerprint: str | None = None,
) -> str:
    scope = f"{scope_fingerprint[:32]}-" if scope_fingerprint is not None else ""
    return f"schemabridge-query-recipe-{scope}{intent_fingerprint[:32]}-current"


def _current_document_urn(
    intent_fingerprint: str,
    *,
    scope_fingerprint: str | None = None,
) -> str:
    return (
        "urn:li:document:"
        f"{_current_document_id(intent_fingerprint, scope_fingerprint=scope_fingerprint)}"
    )


def _versioned_document_id(recipe: QueryRecipe) -> str:
    scope_fingerprint = _recipe_scope_fingerprint(recipe)
    scope = f"{scope_fingerprint[:32]}-" if scope_fingerprint is not None else ""
    return f"schemabridge-query-recipe-{scope}{recipe.intent_fingerprint[:32]}-v{recipe.version}"


def _versioned_document_urn(recipe: QueryRecipe) -> str:
    return f"urn:li:document:{_versioned_document_id(recipe)}"


def _recipe_scope_fingerprint(recipe: QueryRecipe) -> str | None:
    binding = recipe.registry_binding
    return binding.scope_fingerprint if binding is not None else None


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
    if isinstance(error, (KeyError, TypeError, ValueError)):
        return RecipeErrorCode.INVALID_RECORDED_RECIPE
    return RecipeErrorCode.CATALOG_UNAVAILABLE
