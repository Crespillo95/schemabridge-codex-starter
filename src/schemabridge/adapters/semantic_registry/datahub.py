"""Bounded DataHub document read/write adapter for immutable registry versions."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from schemabridge.application.ports.planning import (
    PlanningPortError,
    PlanningPortErrorCode,
    RegistryPublicationError,
    RegistryPublicationErrorCode,
)
from schemabridge.application.ports.registry_control import (
    RegistryControlError,
    RegistryControlErrorCode,
)
from schemabridge.domain.publication_audit import (
    PublicationAuditOutcome,
    PublicationFamily,
    PublicationTargetAuditRecord,
    validate_publication_audit_binding,
)
from schemabridge.domain.registry_control import (
    GovernedRegistryVersion,
    RegistryVersionTrust,
)
from schemabridge.domain.registry_publication import (
    ObservedRegistryPublicationResult,
    PublicationReadbackReceipt,
    PublishableRegistryVersion,
    RegistryPublicationAuthorization,
    RegistryPublicationCandidateManifest,
    observed_registry_related_asset_urns,
    validate_registry_publication_authorization,
)
from schemabridge.domain.semantic_registry import (
    GovernedSemanticRegistrySnapshot,
    RegistryPublicationApproval,
    RegistryPublicationResult,
    RegistryPublicationStatus,
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
    datahub_registry_document_id,
    datahub_registry_document_urn,
    registry_publication_approval_id,
    semantic_registry_decision_ids,
    validate_registry_publication_approval,
)

logger = logging.getLogger(__name__)

_MAX_REGISTRY_JSON_BYTES = 2 * 1024 * 1024
_MAX_APPROVAL_JSON_BYTES = 256 * 1024
_MAX_CANDIDATE_MANIFEST_JSON_BYTES = 512 * 1024
_MAX_AUDIT_JSON_BYTES = 64 * 1024
_MAX_RELATED_ASSETS = 256
_MAX_ENV_BYTES = 64 * 1024
_MAX_GRAPHQL_RESPONSE_BYTES = 64 * 1024
_MAX_DOCUMENT_RESPONSE_BYTES = 3 * 1024 * 1024
_MAX_STATUS_RESPONSE_BYTES = 16 * 1024
_MAX_DOCUMENT_TITLE_BYTES = 512
_MAX_DOCUMENT_TEXT_BYTES = 16 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LEGACY_V1_APPROVAL_ID = "m22-local-steward-synthetic_enterprise-v1"
_LEGACY_V1_APPROVAL_ACTOR = "m22-local-steward"
_LEGACY_V1_APPROVED_AT = datetime(2026, 7, 23, 11, 43, 46, 457987, tzinfo=UTC)
_LEGACY_V1_WORKSPACE_ID = (
    "sb_workspace_v1_0d84030bafe133623e69316b33bd9a49eff1f52ca591430655d373f252ea7421"
)
_LEGACY_V1_REGISTRY_FINGERPRINT = "ef480eb7370924ff4c94131a2c6c4063d85652cc83aaec9059538cdc2cf9c1b9"
_REGISTRY_PROPERTIES = frozenset(
    {
        "schemabridge.registryFormatVersion",
        "schemabridge.registryId",
        "schemabridge.registryVersion",
        "schemabridge.registryCatalogScope",
        "schemabridge.registryWorkspaceDigest",
        "schemabridge.registryFingerprint",
        "schemabridge.registryDecisionIds",
        "schemabridge.registrySnapshot",
        "schemabridge.registryPublicationApproval",
        "schemabridge.registryPublicationAudit",
    }
)
_REGISTRY_V2_PROPERTIES = (_REGISTRY_PROPERTIES - {"schemabridge.registryPublicationApproval"}) | {
    "schemabridge.registryPublicationCandidate",
    "schemabridge.registryPublicationAuthorization",
}
_READER_TOLERATED_PLATFORM_PRIVILEGES = frozenset({"generatePersonalAccessTokens"})
_TARGET_EDIT_PRIVILEGES = (
    "canManageEntity",
    "canEditProperties",
    "canEditDescription",
    "canEditTags",
    "canEditGlossaryTerms",
    "canEditOwners",
    "canEditDomains",
)
_TARGET_MUTATION_GRANTS = frozenset({"EDIT_ENTITY", "MANAGE_DOCUMENTS"})
_MUTATING_PLATFORM_PRIVILEGES = (
    "managePolicies",
    "manageIdentities",
    "manageIngestion",
    "manageSecrets",
    "manageTokens",
    "manageServiceAccounts",
    "generatePersonalAccessTokens",
    "manageGlossaries",
    "manageDocuments",
    "createTags",
    "manageTags",
    "manageStructuredProperties",
)
_LEGACY_ALLOWED_WRITER_PRIVILEGES = frozenset(
    {
        "generatePersonalAccessTokens",
        "manageGlossaries",
        "manageDocuments",
        "manageStructuredProperties",
    }
)
_LEGACY_ALLOWED_TARGET_GRANTS = frozenset({"MANAGE_DOCUMENTS"})
_M34_WRITER_PRIVILEGES = frozenset({"manageDocuments"})


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        del request, file_pointer, code, message, headers, new_url
        return None


@dataclass(frozen=True, slots=True)
class DataHubRegistryReadConfig:
    server: str
    token: str = field(repr=False)

    @classmethod
    def from_env_file(cls, path: Path) -> DataHubRegistryReadConfig:
        try:
            values = _secure_env_values(
                path,
                allowed_key_sets=(
                    frozenset({"DATAHUB_GMS_URL", "DATAHUB_GMS_TOKEN"}),
                    frozenset(
                        {
                            "DATAHUB_GMS_URL",
                            "DATAHUB_GMS_TOKEN",
                            "TOOLS_IS_MUTATION_ENABLED",
                            "SAVE_DOCUMENT_TOOL_ENABLED",
                            "DATAHUB_MCP_DOCUMENT_TOOLS_DISABLED",
                        }
                    ),
                ),
            )
            return cls(
                server=values["DATAHUB_GMS_URL"].rstrip("/"),
                token=values["DATAHUB_GMS_TOKEN"],
            )
        except (KeyError, OSError, ValueError) as error:
            raise PlanningPortError(
                PlanningPortErrorCode.CONTEXT_UNAVAILABLE,
                "DataHub registry reader credential is unavailable",
            ) from error


@dataclass(frozen=True, slots=True)
class DataHubRegistryWriteConfig:
    server: str
    token: str = field(repr=False)
    actor_urn: str

    @classmethod
    def from_env_file(cls, path: Path) -> DataHubRegistryWriteConfig:
        try:
            values = _secure_env_values(
                path,
                allowed_key_sets=(
                    frozenset(
                        {
                            "DATAHUB_GMS_URL",
                            "DATAHUB_GMS_TOKEN",
                            "DATAHUB_WRITER_ACTOR_URN",
                        }
                    ),
                ),
            )
            return cls(
                server=values["DATAHUB_GMS_URL"].rstrip("/"),
                token=values["DATAHUB_GMS_TOKEN"],
                actor_urn=values["DATAHUB_WRITER_ACTOR_URN"],
            )
        except (KeyError, OSError, ValueError) as error:
            raise RegistryPublicationError(
                RegistryPublicationErrorCode.CATALOG_UNAVAILABLE,
                "DataHub registry writer credential is unavailable",
            ) from error


@dataclass(frozen=True, slots=True)
class DataHubRegistryDocument:
    urn: str
    title: str
    text: str
    custom_properties: Mapping[str, str]
    related_asset_urns: tuple[str, ...]
    removed: bool


@dataclass(frozen=True, slots=True)
class DataHubRegistryDocumentWrite:
    document_id: str
    title: str
    text: str
    custom_properties: Mapping[str, str]
    related_asset_urns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DataHubRegistryIdentity:
    actor_urn: str
    granted_platform_mutation_privileges: frozenset[str]
    granted_target_edit_privileges: frozenset[str]


class DataHubRegistryReadClient(Protocol):
    def identity(self, target_urn: str) -> DataHubRegistryIdentity:
        """Return actor, platform mutations, and exact-target edit privileges."""

    def get_document(self, urn: str) -> DataHubRegistryDocument | None:
        """Read one exact document URN."""


class DataHubRegistryWriteClient(DataHubRegistryReadClient, Protocol):
    def upsert_document(self, document: DataHubRegistryDocumentWrite) -> None:
        """Upsert one exact version document."""


@dataclass(slots=True)
class DataHubHttpRegistryReadClient:
    """Bounded exact-URN DataHub reader with no mutation method."""

    server: str
    token: str = field(repr=False)

    def identity(self, target_urn: str) -> DataHubRegistryIdentity:
        privilege_fields = " ".join(_MUTATING_PLATFORM_PRIVILEGES)
        query = (
            "query RegistryRuntimeIdentity { me { corpUser { urn } "
            f"platformPrivileges {{ {privilege_fields} }} }} }}"
        )
        data = self._graphql(query, {})
        me = data.get("me")
        if not isinstance(me, dict):
            raise ValueError("DataHub runtime identity is missing")
        corp_user = me.get("corpUser")
        privileges = me.get("platformPrivileges")
        if not isinstance(corp_user, dict) or not isinstance(privileges, dict):
            raise ValueError("DataHub runtime identity is incomplete")
        if set(privileges) != set(_MUTATING_PLATFORM_PRIVILEGES) or any(
            not isinstance(granted, bool) for granted in privileges.values()
        ):
            raise ValueError("DataHub platform privilege response is incomplete")
        actor = corp_user.get("urn")
        if not isinstance(actor, str) or not actor:
            raise ValueError("DataHub runtime actor is invalid")
        edit_fields = " ".join(_TARGET_EDIT_PRIVILEGES)
        target_query = (
            "query RegistryTargetPrivileges($urn: String!, $actorUrn: String!) { "
            f"document(urn: $urn) {{ privileges {{ {edit_fields} }} }} "
            "getGrantedPrivileges(input: { actorUrn: $actorUrn, "
            "resourceSpec: { resourceType: DOCUMENT, resourceUrn: $urn }, "
            "includeEvaluationDetails: false }) { privileges } }"
        )
        target_data = self._graphql(
            target_query,
            {"urn": target_urn, "actorUrn": actor},
        )
        if "document" not in target_data:
            raise ValueError("DataHub target privilege response is incomplete")
        document = target_data["document"]
        target_privileges: dict[str, object] = {}
        if document is not None:
            if not isinstance(document, dict):
                raise ValueError("DataHub target privilege response is invalid")
            raw_target_privileges = document.get("privileges")
            if raw_target_privileges is not None:
                if not isinstance(raw_target_privileges, dict):
                    raise ValueError("DataHub target privileges are invalid")
                target_privileges = raw_target_privileges
        if document is not None and (
            set(target_privileges) != set(_TARGET_EDIT_PRIVILEGES)
            or any(not isinstance(granted, bool) for granted in target_privileges.values())
        ):
            raise ValueError("DataHub target privilege response is incomplete")
        granted = target_data.get("getGrantedPrivileges")
        if not isinstance(granted, dict) or not isinstance(
            granted.get("privileges"),
            list,
        ):
            raise ValueError("DataHub target grant response is invalid")
        raw_grants = granted["privileges"]
        if any(not isinstance(grant, str) for grant in raw_grants):
            raise ValueError("DataHub target grants are invalid")
        return DataHubRegistryIdentity(
            actor_urn=actor,
            granted_platform_mutation_privileges=frozenset(
                name for name, granted in privileges.items() if granted is True
            ),
            granted_target_edit_privileges=frozenset(
                name for name, granted in target_privileges.items() if granted is True
            )
            | (frozenset(raw_grants) & _TARGET_MUTATION_GRANTS),
        )

    def get_document(self, urn: str) -> DataHubRegistryDocument | None:
        payload = self._aspect(urn, "documentInfo", _MAX_DOCUMENT_RESPONSE_BYTES)
        if payload is None:
            return None
        aspect = _extract_aspect(payload, "com.linkedin.knowledge.DocumentInfo")
        document_status = aspect.get("status")
        if not isinstance(document_status, dict) or document_status.get("state") != "PUBLISHED":
            raise ValueError("DataHub registry document is not published")
        title = aspect.get("title")
        contents = aspect.get("contents")
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title.encode()) > _MAX_DOCUMENT_TITLE_BYTES
            or not isinstance(contents, dict)
            or set(contents) != {"text"}
            or not isinstance(contents.get("text"), str)
            or not contents["text"].strip()
            or len(contents["text"].encode()) > _MAX_DOCUMENT_TEXT_BYTES
        ):
            raise ValueError("DataHub registry document title or text is invalid")
        text = contents["text"]
        properties = aspect.get("customProperties")
        if not isinstance(properties, dict):
            raise ValueError("DataHub registry document properties are invalid")
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in properties.items()
        ):
            raise ValueError("DataHub registry document properties must be strings")
        related = aspect.get("relatedAssets", [])
        if not isinstance(related, list):
            raise ValueError("DataHub registry related assets are invalid")
        assets = tuple(item.get("asset", "") if isinstance(item, dict) else "" for item in related)
        if len(assets) > _MAX_RELATED_ASSETS or any(
            not isinstance(asset, str) or not asset for asset in assets
        ):
            raise ValueError("DataHub registry related assets are invalid")
        if len(assets) != len(set(assets)):
            raise ValueError("DataHub registry related assets are invalid")
        status_payload = self._aspect(urn, "status", _MAX_STATUS_RESPONSE_BYTES)
        removed = False
        if status_payload is not None:
            status_aspect = _extract_aspect(
                status_payload,
                "com.linkedin.common.Status",
            )
            raw_removed = status_aspect.get("removed")
            if not isinstance(raw_removed, bool):
                raise ValueError("DataHub registry status is invalid")
            removed = raw_removed
        return DataHubRegistryDocument(
            urn=urn,
            title=title,
            text=text,
            custom_properties=properties,
            related_asset_urns=assets,
            removed=removed,
        )

    def _aspect(
        self,
        urn: str,
        aspect: str,
        maximum: int,
    ) -> dict[str, Any] | None:
        encoded_urn = urllib.parse.quote(urn, safe="")
        request = urllib.request.Request(
            f"{self.server}/aspects/{encoded_urn}?aspect={aspect}&version=0",
            headers={"Authorization": f"Bearer {self.token}"},
            method="GET",
        )
        try:
            return self._request_json(request, maximum)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise

    def _graphql(
        self,
        query: str,
        variables: Mapping[str, str],
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.server}/api/graphql",
            data=json.dumps({"query": query, "variables": variables}).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        payload = self._request_json(request, _MAX_GRAPHQL_RESPONSE_BYTES)
        if not isinstance(payload, dict) or payload.get("errors"):
            raise ValueError("DataHub GraphQL returned an error")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("DataHub GraphQL returned no data")
        return data

    @staticmethod
    def _request_json(
        request: urllib.request.Request,
        maximum: int,
    ) -> dict[str, Any]:
        opener = urllib.request.build_opener(_RejectRedirects())
        with opener.open(request, timeout=15) as response:
            raw = response.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError("DataHub response exceeds the configured bound")
        payload = json.loads(raw, object_pairs_hook=_unique_json_pairs)
        if not isinstance(payload, dict):
            raise ValueError("DataHub response must be a JSON object")
        return payload


@dataclass(slots=True)
class DataHubSdkRegistryWriteClient:
    """Writer-only SDK boundary composed with the bounded read client."""

    server: str
    token: str = field(repr=False)

    def identity(self, target_urn: str) -> DataHubRegistryIdentity:
        return self._reader().identity(target_urn)

    def get_document(self, urn: str) -> DataHubRegistryDocument | None:
        return self._reader().get_document(urn)

    def upsert_document(self, document: DataHubRegistryDocumentWrite) -> None:
        from datahub.sdk.document import Document
        from datahub.sdk.main_client import DataHubClient

        entity = Document.create_document(
            id=document.document_id,
            title=document.title,
            text=document.text,
            related_assets=document.related_asset_urns,
            custom_properties=dict(document.custom_properties),
        )
        DataHubClient(server=self.server, token=self.token).entities.upsert(entity)

    def _reader(self) -> DataHubHttpRegistryReadClient:
        return DataHubHttpRegistryReadClient(self.server, self.token)


@dataclass(slots=True)
class DataHubGovernedSemanticRegistry:
    """Load one exact immutable registry version using a mutation-free DataHub identity."""

    config: DataHubRegistryReadConfig
    _scope: SemanticRegistryScope
    version: int
    client: DataHubRegistryReadClient | None = None
    loads: int = 0

    @property
    def scope(self) -> SemanticRegistryScope:
        return self._scope

    def load(self) -> ScopedSemanticRegistrySnapshot:
        self.loads += 1
        client = self.client or DataHubHttpRegistryReadClient(
            self.config.server,
            self.config.token,
        )
        try:
            target = datahub_registry_document_urn(self.scope, self.version)
            identity = client.identity(target)
            forbidden_platform_privileges = (
                identity.granted_platform_mutation_privileges
                - _READER_TOLERATED_PLATFORM_PRIVILEGES
            )
            if forbidden_platform_privileges or identity.granted_target_edit_privileges:
                raise PlanningPortError(
                    PlanningPortErrorCode.CONTEXT_FORBIDDEN,
                    "DataHub registry reader has mutation privileges",
                )
            document = client.get_document(target)
            if document is None:
                raise PlanningPortError(
                    PlanningPortErrorCode.REGISTRY_NOT_FOUND,
                    "configured DataHub semantic registry version was not found",
                )
            if document.custom_properties.get("schemabridge.registryFormatVersion") == "2":
                candidate, _, _ = _parse_registry_document_v2(
                    document,
                    scope=self.scope,
                    version=self.version,
                )
                registry = candidate.registry
            else:
                registry, _, _ = _parse_registry_document(
                    document,
                    scope=self.scope,
                    version=self.version,
                )
            return ScopedSemanticRegistrySnapshot(scope=self.scope, registry=registry)
        except PlanningPortError:
            raise
        except Exception as error:
            raise PlanningPortError(
                _planning_error_code(error),
                "DataHub semantic registry read failed",
            ) from error


@dataclass(slots=True)
class DataHubSemanticRegistryPublisher:
    """Publish one explicitly approved immutable registry version document."""

    config: DataHubRegistryWriteConfig
    client: DataHubRegistryWriteClient | None = None

    def publish(
        self,
        registry: GovernedSemanticRegistrySnapshot,
        approval: RegistryPublicationApproval,
    ) -> RegistryPublicationResult:
        if registry.format_version != 1:
            raise RegistryPublicationError(
                RegistryPublicationErrorCode.PAYLOAD_INVALID,
                "legacy registry publisher accepts only format v1",
            )
        if not isinstance(approval, RegistryPublicationApproval):
            raise RegistryPublicationError(
                RegistryPublicationErrorCode.APPROVAL_REQUIRED,
                "explicit registry publication approval is required",
            )
        try:
            validate_registry_publication_approval(registry, approval)
        except ValueError as error:
            raise RegistryPublicationError(
                RegistryPublicationErrorCode.APPROVAL_MISMATCH,
                "registry publication approval does not match the exact payload",
            ) from error

        scope = SemanticRegistryScope(
            workspace_id=approval.workspace_id,
            catalog_scope=approval.catalog_scope,
            registry_id=approval.registry_id,
        )
        target = datahub_registry_document_urn(scope, registry.version)
        stored_audit = _audit_record(
            registry,
            approval,
            target=target,
            outcome=PublicationAuditOutcome.SUCCEEDED,
            previous_fingerprint=None,
        )
        try:
            document = _document_write(registry, approval, stored_audit)
        except ValueError as error:
            raise RegistryPublicationError(
                RegistryPublicationErrorCode.PAYLOAD_INVALID,
                "registry publication payload exceeds its safe storage contract",
            ) from error
        client = self.client or DataHubSdkRegistryWriteClient(
            self.config.server,
            self.config.token,
        )
        try:
            identity = client.identity(target)
            _require_bounded_writer_identity(identity, self.config)
            existing = client.get_document(target)
        except RegistryPublicationError:
            raise
        except Exception as error:
            raise RegistryPublicationError(
                _publication_error_code(error),
                "DataHub registry target state read failed",
            ) from error

        if existing is not None:
            try:
                existing_registry, existing_approval, _ = _parse_registry_document(
                    existing,
                    scope=scope,
                    version=registry.version,
                )
            except PlanningPortError as error:
                raise RegistryPublicationError(
                    RegistryPublicationErrorCode.INVALID_RESPONSE,
                    "DataHub immutable registry target failed typed validation",
                ) from error
            if existing_registry != registry:
                raise RegistryPublicationError(
                    RegistryPublicationErrorCode.CONFLICT,
                    "DataHub immutable registry version already identifies different content",
                )
            if existing_approval.id == approval.id and existing_approval != approval:
                raise RegistryPublicationError(
                    RegistryPublicationErrorCode.APPROVAL_MISMATCH,
                    "registry publication approval id identifies different immutable facts",
                )
            return _publication_result(
                registry,
                approval,
                target=target,
                status=RegistryPublicationStatus.ALREADY_CURRENT,
                previous_fingerprint=registry.fingerprint,
            )

        logger.info(
            "datahub_registry_publish operation=versioned_document target=%s",
            target,
        )
        try:
            client.upsert_document(document)
            observed = client.get_document(target)
            if observed is None:
                raise ValueError("DataHub registry document was absent after upsert")
            observed_registry, observed_approval, observed_audit = _parse_registry_document(
                observed,
                scope=scope,
                version=registry.version,
            )
            if (
                observed_registry != registry
                or observed_approval != approval
                or observed_audit != stored_audit
            ):
                raise ValueError("DataHub registry post-write state differs from its approval")
        except Exception as error:
            reason = _reason_code(error)
            logger.warning(
                "datahub_registry_publish_failed operation=versioned_document target=%s code=%s",
                target,
                reason,
            )
            return _publication_result(
                registry,
                approval,
                target=target,
                status=RegistryPublicationStatus.FAILED,
                previous_fingerprint=None,
                reason_code=reason,
            )
        return _publication_result(
            registry,
            approval,
            target=target,
            status=RegistryPublicationStatus.PUBLISHED,
            previous_fingerprint=None,
        )


@dataclass(slots=True)
class DataHubObservedSemanticRegistryPublisher:
    """Publish one exact format-v2 registry using only observed DataHub asset identities."""

    config: DataHubRegistryWriteConfig
    client: DataHubRegistryWriteClient | None = None

    def observe(
        self,
        candidate: PublishableRegistryVersion,
        authorization: RegistryPublicationAuthorization,
        *,
        observed_at: datetime,
    ) -> ObservedRegistryPublicationResult:
        """Read back one exact target without ever invoking an upsert."""

        try:
            validate_registry_publication_authorization(candidate, authorization)
        except ValueError as error:
            raise RegistryPublicationError(
                RegistryPublicationErrorCode.APPROVAL_MISMATCH,
                "registry publication authorization does not match the exact v2 candidate",
            ) from error
        client = self.client or DataHubSdkRegistryWriteClient(
            self.config.server,
            self.config.token,
        )
        try:
            identity = client.identity(candidate.target)
            _require_document_only_writer_identity(identity, self.config)
            existing = client.get_document(candidate.target)
        except RegistryPublicationError:
            raise
        except Exception as error:
            raise RegistryPublicationError(
                _publication_error_code(error),
                "DataHub registry v2 target observation failed",
            ) from error
        if existing is None:
            return _v2_publication_result(
                candidate,
                authorization,
                observed_authorization_id=None,
                observed_at=observed_at,
                status="failed",
                previous_fingerprint=None,
                reason_code="target_absent",
            )
        try:
            observed_candidate, observed_authorization, _ = _parse_registry_document_v2(
                existing,
                scope=candidate.scope,
                version=candidate.registry.version,
            )
        except PlanningPortError as error:
            raise RegistryPublicationError(
                RegistryPublicationErrorCode.INVALID_RESPONSE,
                "DataHub immutable registry v2 target failed typed validation",
            ) from error
        if (
            observed_candidate.registry != candidate.registry
            or observed_candidate.manifest != candidate.manifest
        ):
            raise RegistryPublicationError(
                RegistryPublicationErrorCode.CONFLICT,
                "DataHub immutable registry v2 target already identifies different content",
            )
        return _v2_publication_result(
            candidate,
            authorization,
            observed_authorization_id=observed_authorization.id,
            observed_at=observed_at,
            status="already_current",
            previous_fingerprint=candidate.registry.fingerprint,
        )

    def publish(
        self,
        candidate: PublishableRegistryVersion,
        authorization: RegistryPublicationAuthorization,
        *,
        observed_at: datetime,
    ) -> ObservedRegistryPublicationResult:
        try:
            validate_registry_publication_authorization(
                candidate,
                authorization,
            )
        except ValueError as error:
            raise RegistryPublicationError(
                RegistryPublicationErrorCode.APPROVAL_MISMATCH,
                "registry publication authorization does not match the exact v2 candidate",
            ) from error
        registry = candidate.registry
        target = candidate.target
        stored_audit = _v2_audit_record(
            candidate,
            authorization,
            outcome=PublicationAuditOutcome.SUCCEEDED,
            previous_fingerprint=None,
        )
        try:
            document = _v2_document_write(candidate, authorization, stored_audit)
        except ValueError as error:
            raise RegistryPublicationError(
                RegistryPublicationErrorCode.PAYLOAD_INVALID,
                "registry v2 publication payload exceeds its safe storage contract",
            ) from error
        client = self.client or DataHubSdkRegistryWriteClient(
            self.config.server,
            self.config.token,
        )
        try:
            identity = client.identity(target)
            _require_document_only_writer_identity(identity, self.config)
            existing = client.get_document(target)
        except RegistryPublicationError:
            raise
        except Exception as error:
            raise RegistryPublicationError(
                _publication_error_code(error),
                "DataHub registry v2 target state read failed",
            ) from error

        if existing is not None:
            try:
                observed_candidate, observed_authorization, _ = _parse_registry_document_v2(
                    existing,
                    scope=candidate.scope,
                    version=registry.version,
                )
            except PlanningPortError as error:
                raise RegistryPublicationError(
                    RegistryPublicationErrorCode.INVALID_RESPONSE,
                    "DataHub immutable registry v2 target failed typed validation",
                ) from error
            if (
                observed_candidate.registry != registry
                or observed_candidate.manifest != candidate.manifest
            ):
                raise RegistryPublicationError(
                    RegistryPublicationErrorCode.CONFLICT,
                    "DataHub immutable registry v2 target already identifies different content",
                )
            return _v2_publication_result(
                candidate,
                authorization,
                observed_authorization_id=observed_authorization.id,
                observed_at=observed_at,
                status="already_current",
                previous_fingerprint=registry.fingerprint,
            )

        # A retry may arrive after the short human-authorization window.  It is
        # safe to read and reconcile an already-written immutable target, but a
        # missing target must never be created from an expired authorization.
        try:
            validate_registry_publication_authorization(
                candidate,
                authorization,
                at=observed_at,
            )
        except ValueError as error:
            raise RegistryPublicationError(
                RegistryPublicationErrorCode.APPROVAL_MISMATCH,
                "registry publication authorization is no longer current for a new write",
            ) from error

        logger.info("datahub_registry_publish_v2 operation=versioned_document target=%s", target)
        try:
            client.upsert_document(document)
            observed = client.get_document(target)
            if observed is None:
                raise ValueError("DataHub registry v2 document was absent after upsert")
            observed_candidate, observed_authorization, observed_audit = (
                _parse_registry_document_v2(
                    observed,
                    scope=candidate.scope,
                    version=registry.version,
                )
            )
            if (
                observed_candidate != candidate
                or observed_authorization != authorization
                or observed_audit != stored_audit
            ):
                raise ValueError("DataHub registry v2 post-write state differs from authorization")
        except Exception as error:
            reason = _reason_code(error)
            logger.warning(
                "datahub_registry_publish_v2_failed operation=versioned_document target=%s code=%s",
                target,
                reason,
            )
            return _v2_publication_result(
                candidate,
                authorization,
                observed_authorization_id=None,
                observed_at=observed_at,
                status="failed",
                previous_fingerprint=None,
                reason_code=reason,
            )
        return _v2_publication_result(
            candidate,
            authorization,
            observed_authorization_id=authorization.id,
            observed_at=observed_at,
            status="published",
            previous_fingerprint=None,
        )


@dataclass(slots=True)
class DataHubWriterRegistryVersionReader:
    """Publisher-only strict v2 base reader using the bounded writer identity."""

    config: DataHubRegistryWriteConfig
    client: DataHubRegistryWriteClient | None = None

    def load_version(
        self,
        scope: SemanticRegistryScope,
        version: int,
    ) -> GovernedRegistryVersion:
        target = datahub_registry_document_urn(scope, version)
        client = self.client or DataHubSdkRegistryWriteClient(
            self.config.server,
            self.config.token,
        )
        try:
            _require_document_only_writer_identity(client.identity(target), self.config)
            document = client.get_document(target)
            if document is None:
                raise RegistryControlError(
                    RegistryControlErrorCode.VERSION_UNAVAILABLE,
                    "registry publisher base version is unavailable",
                )
            candidate, authorization, _ = _parse_registry_document_v2(
                document,
                scope=scope,
                version=version,
            )
            if candidate.registry.format_version != 2:
                raise ValueError("registry publisher base is not format v2")
            return GovernedRegistryVersion(
                snapshot=ScopedSemanticRegistrySnapshot(
                    scope=scope,
                    registry=candidate.registry,
                ),
                publication_approval_id=authorization.id,
                trust=RegistryVersionTrust.STRICT,
            )
        except RegistryControlError:
            raise
        except RegistryPublicationError as error:
            raise RegistryControlError(
                RegistryControlErrorCode.VERSION_INVALID,
                "registry publisher base writer identity is invalid",
            ) from error
        except PlanningPortError as error:
            raise RegistryControlError(
                RegistryControlErrorCode.VERSION_INVALID,
                "registry publisher base version is invalid",
            ) from error
        except (ConnectionError, TimeoutError, urllib.error.URLError) as error:
            raise RegistryControlError(
                RegistryControlErrorCode.VERSION_UNAVAILABLE,
                "registry publisher base version is unavailable",
            ) from error
        except Exception as error:
            raise RegistryControlError(
                RegistryControlErrorCode.VERSION_INVALID,
                "registry publisher base version is invalid",
            ) from error


def _require_bounded_writer_identity(
    identity: DataHubRegistryIdentity,
    config: DataHubRegistryWriteConfig,
) -> None:
    """Preserve the bounded M22/v1 identity contract for the legacy publisher."""

    if (
        identity.actor_urn != config.actor_urn
        or "manageDocuments" not in identity.granted_platform_mutation_privileges
        or not identity.granted_platform_mutation_privileges <= _LEGACY_ALLOWED_WRITER_PRIVILEGES
        or not identity.granted_target_edit_privileges <= _LEGACY_ALLOWED_TARGET_GRANTS
    ):
        raise RegistryPublicationError(
            RegistryPublicationErrorCode.CATALOG_PERMISSION_DENIED,
            "DataHub registry writer identity or privileges are not bounded",
        )


def _require_document_only_writer_identity(
    identity: DataHubRegistryIdentity,
    config: DataHubRegistryWriteConfig,
) -> None:
    """Require the exact M34 capability, excluding tolerated legacy/local residual grants."""

    if (
        identity.actor_urn != config.actor_urn
        or identity.granted_platform_mutation_privileges != _M34_WRITER_PRIVILEGES
        or bool(identity.granted_target_edit_privileges)
    ):
        raise RegistryPublicationError(
            RegistryPublicationErrorCode.CATALOG_PERMISSION_DENIED,
            "DataHub registry-v2 writer identity or privileges are not document-only",
        )


def semantic_registry_related_asset_urns(
    registry: GovernedSemanticRegistrySnapshot,
) -> tuple[str, ...]:
    if registry.format_version != 1:
        raise ValueError("format-v2 related assets require observed physical bindings")
    datasets = {
        governed.mapping.physical_field.root.rsplit(".", 1)[0]
        for governed in registry.mapping_set.mappings
    }
    return tuple(
        sorted(
            f"urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge.{dataset},PROD)"
            for dataset in datasets
        )
    )


def _parse_registry_document_v2(
    document: DataHubRegistryDocument,
    *,
    scope: SemanticRegistryScope,
    version: int,
) -> tuple[
    PublishableRegistryVersion,
    RegistryPublicationAuthorization,
    PublicationTargetAuditRecord,
]:
    expected_target = datahub_registry_document_urn(scope, version)
    if document.urn != expected_target or document.removed:
        raise PlanningPortError(
            PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED,
            "DataHub registry v2 document identity or active status changed",
        )
    properties = document.custom_properties
    if set(properties) != _REGISTRY_V2_PROPERTIES:
        raise PlanningPortError(
            PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED,
            "DataHub registry v2 properties are incomplete or unexpected",
        )
    try:
        registry = GovernedSemanticRegistrySnapshot.model_validate(
            _load_unique_json(
                _bounded_property(
                    properties,
                    "schemabridge.registrySnapshot",
                    _MAX_REGISTRY_JSON_BYTES,
                )
            )
        )
        manifest = RegistryPublicationCandidateManifest.model_validate(
            _load_unique_json(
                _bounded_property(
                    properties,
                    "schemabridge.registryPublicationCandidate",
                    _MAX_CANDIDATE_MANIFEST_JSON_BYTES,
                )
            )
        )
        authorization = RegistryPublicationAuthorization.model_validate(
            _load_unique_json(
                _bounded_property(
                    properties,
                    "schemabridge.registryPublicationAuthorization",
                    _MAX_APPROVAL_JSON_BYTES,
                )
            )
        )
        audit = PublicationTargetAuditRecord.model_validate(
            _load_unique_json(
                _bounded_property(
                    properties,
                    "schemabridge.registryPublicationAudit",
                    _MAX_AUDIT_JSON_BYTES,
                )
            )
        )
        candidate = PublishableRegistryVersion(
            id=manifest.id,
            scope=manifest.scope,
            source_proposal_id=manifest.source_proposal_id,
            source_proposal_fingerprint=manifest.source_proposal_fingerprint,
            base_registry=manifest.base_registry,
            registry=registry,
            target=manifest.target,
            review_decision_ids=manifest.review_decision_ids,
            active_decision_ids=manifest.active_decision_ids,
            fingerprint=manifest.fingerprint,
        )
    except (TypeError, ValueError, ValidationError, json.JSONDecodeError) as error:
        raise PlanningPortError(
            PlanningPortErrorCode.CONTEXT_INVALID,
            "DataHub registry v2 payload is invalid",
        ) from error

    fingerprint = properties.get("schemabridge.registryFingerprint")
    workspace_digest = hashlib.sha256(scope.workspace_id.encode()).hexdigest()[:24]
    stored_decisions = _parse_stored_decisions(properties)
    if (
        registry.format_version != 2
        or manifest != candidate.manifest
        or manifest.scope != scope
        or properties.get("schemabridge.registryFormatVersion") != "2"
        or properties.get("schemabridge.registryId") != registry.registry_id
        or properties.get("schemabridge.registryVersion") != str(registry.version)
        or properties.get("schemabridge.registryCatalogScope") != registry.catalog_scope
        or properties.get("schemabridge.registryWorkspaceDigest") != workspace_digest
        or not isinstance(fingerprint, str)
        or _SHA256.fullmatch(fingerprint) is None
        or fingerprint != registry.fingerprint
        or stored_decisions != candidate.active_decision_ids
        or registry.registry_id != scope.registry_id
        or registry.catalog_scope != scope.catalog_scope
        or registry.version != version
        or document.title != _registry_document_title(registry)
        or document.text != _registry_document_text(registry)
    ):
        raise PlanningPortError(
            PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED,
            "DataHub registry v2 identity or fingerprint verification failed",
        )
    try:
        validate_registry_publication_authorization(candidate, authorization)
        validate_publication_audit_binding(
            (audit,),
            approval_id=authorization.id,
            actor=authorization.actor_id,
            approved_at=authorization.approved_at,
            new_fingerprint=registry.fingerprint,
            approved_decision_ids=candidate.active_decision_ids,
        )
    except ValueError as error:
        raise PlanningPortError(
            PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED,
            "DataHub registry v2 authorization or audit verification failed",
        ) from error
    if (
        audit.family is not PublicationFamily.REGISTRY
        or audit.operation != "versioned_document_v2"
        or audit.target != expected_target
        or audit.outcome
        not in {
            PublicationAuditOutcome.SUCCEEDED,
            PublicationAuditOutcome.ALREADY_CURRENT,
        }
        or audit.reason_code is not None
    ):
        raise PlanningPortError(
            PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED,
            "DataHub registry v2 audit is not a successful exact-target record",
        )
    expected_assets = observed_registry_related_asset_urns(registry)
    if tuple(sorted(document.related_asset_urns)) != expected_assets:
        raise PlanningPortError(
            PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED,
            "DataHub registry v2 related assets differ from observed physical bindings",
        )
    return candidate, authorization, audit


def _v2_document_write(
    candidate: PublishableRegistryVersion,
    authorization: RegistryPublicationAuthorization,
    audit: PublicationTargetAuditRecord,
) -> DataHubRegistryDocumentWrite:
    registry = candidate.registry
    if registry.format_version != 2:
        raise ValueError("observed registry publication requires format v2")
    registry_json = registry.model_dump_json()
    manifest_json = candidate.manifest.model_dump_json()
    authorization_json = authorization.model_dump_json()
    audit_json = audit.model_dump_json()
    _validate_serialized_size(registry_json, _MAX_REGISTRY_JSON_BYTES, "registry v2 snapshot")
    _validate_serialized_size(
        manifest_json,
        _MAX_CANDIDATE_MANIFEST_JSON_BYTES,
        "registry publication candidate",
    )
    _validate_serialized_size(
        authorization_json,
        _MAX_APPROVAL_JSON_BYTES,
        "registry publication authorization",
    )
    _validate_serialized_size(audit_json, _MAX_AUDIT_JSON_BYTES, "registry v2 audit")
    related_assets = observed_registry_related_asset_urns(registry)
    workspace_digest = hashlib.sha256(candidate.scope.workspace_id.encode()).hexdigest()[:24]
    return DataHubRegistryDocumentWrite(
        document_id=datahub_registry_document_id(candidate.scope, registry.version),
        title=_registry_document_title(registry),
        text=_registry_document_text(registry),
        custom_properties={
            "schemabridge.registryFormatVersion": "2",
            "schemabridge.registryId": registry.registry_id,
            "schemabridge.registryVersion": str(registry.version),
            "schemabridge.registryCatalogScope": registry.catalog_scope,
            "schemabridge.registryWorkspaceDigest": workspace_digest,
            "schemabridge.registryFingerprint": registry.fingerprint,
            "schemabridge.registryDecisionIds": json.dumps(
                candidate.active_decision_ids,
                separators=(",", ":"),
            ),
            "schemabridge.registrySnapshot": registry_json,
            "schemabridge.registryPublicationCandidate": manifest_json,
            "schemabridge.registryPublicationAuthorization": authorization_json,
            "schemabridge.registryPublicationAudit": audit_json,
        },
        related_asset_urns=related_assets,
    )


def _v2_publication_result(
    candidate: PublishableRegistryVersion,
    authorization: RegistryPublicationAuthorization,
    *,
    observed_authorization_id: str | None,
    observed_at: datetime,
    status: str,
    previous_fingerprint: str | None,
    reason_code: str | None = None,
) -> ObservedRegistryPublicationResult:
    outcome = {
        "published": PublicationAuditOutcome.SUCCEEDED,
        "already_current": PublicationAuditOutcome.ALREADY_CURRENT,
        "failed": PublicationAuditOutcome.FAILED,
    }[status]
    receipt = (
        None
        if observed_authorization_id is None
        else PublicationReadbackReceipt(
            candidate_id=candidate.id,
            candidate_fingerprint=candidate.fingerprint,
            scope=candidate.scope,
            registry_version=candidate.registry.version,
            registry_fingerprint=candidate.registry.fingerprint,
            target=candidate.target,
            observed_authorization_id=observed_authorization_id,
            related_asset_urns=observed_registry_related_asset_urns(candidate.registry),
            observed_at=observed_at,
        )
    )
    return ObservedRegistryPublicationResult(
        attempt_authorization_id=authorization.id,
        observed_authorization_id=observed_authorization_id,
        status=status,
        receipt=receipt,
        reason_code=reason_code,
        audit_record=_v2_audit_record(
            candidate,
            authorization,
            outcome=outcome,
            previous_fingerprint=previous_fingerprint,
            reason_code=reason_code,
        ),
    )


def _v2_audit_record(
    candidate: PublishableRegistryVersion,
    authorization: RegistryPublicationAuthorization,
    *,
    outcome: PublicationAuditOutcome,
    previous_fingerprint: str | None,
    reason_code: str | None = None,
) -> PublicationTargetAuditRecord:
    return PublicationTargetAuditRecord(
        family=PublicationFamily.REGISTRY,
        operation="versioned_document_v2",
        target=candidate.target,
        approval_id=authorization.id,
        actor=authorization.actor_id,
        approved_at=authorization.approved_at,
        previous_fingerprint=previous_fingerprint,
        new_fingerprint=candidate.registry.fingerprint,
        outcome=outcome,
        decision_ids=candidate.active_decision_ids,
        reason_code=reason_code,
    )


def _parse_stored_decisions(properties: Mapping[str, str]) -> tuple[str, ...]:
    serialized = _bounded_property(
        properties,
        "schemabridge.registryDecisionIds",
        _MAX_APPROVAL_JSON_BYTES,
    )
    try:
        raw = _load_unique_json(serialized)
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            raise TypeError("registry decisions must be a string array")
        return tuple(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise PlanningPortError(
            PlanningPortErrorCode.CONTEXT_INVALID,
            "DataHub registry decision closure is invalid",
        ) from error


def _parse_registry_document(
    document: DataHubRegistryDocument,
    *,
    scope: SemanticRegistryScope,
    version: int,
) -> tuple[
    GovernedSemanticRegistrySnapshot,
    RegistryPublicationApproval,
    PublicationTargetAuditRecord,
]:
    expected_target = datahub_registry_document_urn(scope, version)
    if document.urn != expected_target or document.removed:
        raise PlanningPortError(
            PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED,
            "DataHub registry document identity or active status changed",
        )
    properties = document.custom_properties
    if set(properties) != _REGISTRY_PROPERTIES:
        raise PlanningPortError(
            PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED,
            "DataHub registry document properties are incomplete or unexpected",
        )
    serialized = _bounded_property(
        properties,
        "schemabridge.registrySnapshot",
        _MAX_REGISTRY_JSON_BYTES,
    )
    serialized_approval = _bounded_property(
        properties,
        "schemabridge.registryPublicationApproval",
        _MAX_APPROVAL_JSON_BYTES,
    )
    serialized_audit = _bounded_property(
        properties,
        "schemabridge.registryPublicationAudit",
        _MAX_AUDIT_JSON_BYTES,
    )
    try:
        registry = GovernedSemanticRegistrySnapshot.model_validate(_load_unique_json(serialized))
        approval = RegistryPublicationApproval.model_validate(
            _load_unique_json(serialized_approval)
        )
        audit = PublicationTargetAuditRecord.model_validate(_load_unique_json(serialized_audit))
    except (TypeError, ValueError, ValidationError, json.JSONDecodeError) as error:
        raise PlanningPortError(
            PlanningPortErrorCode.CONTEXT_INVALID,
            "DataHub registry payload is invalid",
        ) from error

    fingerprint = properties.get("schemabridge.registryFingerprint")
    workspace_digest = hashlib.sha256(scope.workspace_id.encode()).hexdigest()[:24]
    serialized_decisions = _bounded_property(
        properties,
        "schemabridge.registryDecisionIds",
        _MAX_APPROVAL_JSON_BYTES,
    )
    try:
        raw_stored_decisions = _load_unique_json(serialized_decisions)
        if not isinstance(raw_stored_decisions, list) or any(
            not isinstance(decision_id, str) for decision_id in raw_stored_decisions
        ):
            raise TypeError("registry decisions must be a string array")
        stored_decisions = tuple(raw_stored_decisions)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise PlanningPortError(
            PlanningPortErrorCode.CONTEXT_INVALID,
            "DataHub registry decision closure is invalid",
        ) from error
    if (
        registry.format_version != 1
        or properties.get("schemabridge.registryFormatVersion") != str(registry.format_version)
        or properties.get("schemabridge.registryId") != registry.registry_id
        or properties.get("schemabridge.registryVersion") != str(registry.version)
        or properties.get("schemabridge.registryCatalogScope") != registry.catalog_scope
        or properties.get("schemabridge.registryWorkspaceDigest") != workspace_digest
        or not isinstance(fingerprint, str)
        or _SHA256.fullmatch(fingerprint) is None
        or fingerprint != registry.fingerprint
        or stored_decisions != semantic_registry_decision_ids(registry)
        or registry.registry_id != scope.registry_id
        or registry.version != version
        or document.title != _registry_document_title(registry)
        or document.text != _registry_document_text(registry)
    ):
        raise PlanningPortError(
            PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED,
            "DataHub registry identity or fingerprint verification failed",
        )
    if registry.catalog_scope != scope.catalog_scope:
        raise PlanningPortError(
            PlanningPortErrorCode.REGISTRY_SCOPE_MISMATCH,
            "DataHub registry is outside the configured catalog scope",
        )
    try:
        _validate_registry_approval_for_readback(registry, approval)
        validate_publication_audit_binding(
            (audit,),
            approval_id=approval.id,
            actor=approval.actor,
            approved_at=approval.approved_at,
            new_fingerprint=registry.fingerprint,
            approved_decision_ids=semantic_registry_decision_ids(registry),
        )
    except ValueError as error:
        raise PlanningPortError(
            PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED,
            "DataHub registry approval or audit verification failed",
        ) from error
    if approval.workspace_id != scope.workspace_id or approval.target != expected_target:
        raise PlanningPortError(
            PlanningPortErrorCode.REGISTRY_SCOPE_MISMATCH,
            "DataHub registry approval is outside the configured workspace",
        )
    if (
        audit.family is not PublicationFamily.REGISTRY
        or audit.operation != "versioned_document"
        or audit.target != expected_target
        or audit.outcome
        not in {
            PublicationAuditOutcome.SUCCEEDED,
            PublicationAuditOutcome.ALREADY_CURRENT,
        }
        or audit.reason_code is not None
    ):
        raise PlanningPortError(
            PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED,
            "DataHub registry audit is not a successful exact-target record",
        )
    expected_assets = semantic_registry_related_asset_urns(registry)
    if (
        len(document.related_asset_urns) > _MAX_RELATED_ASSETS
        or len(document.related_asset_urns) != len(set(document.related_asset_urns))
        or tuple(sorted(document.related_asset_urns)) != expected_assets
    ):
        raise PlanningPortError(
            PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED,
            "DataHub registry related assets do not match approved mappings",
        )
    return registry, approval, audit


def _validate_registry_approval_for_readback(
    registry: GovernedSemanticRegistrySnapshot,
    approval: RegistryPublicationApproval,
) -> None:
    try:
        validate_registry_publication_approval(registry, approval)
        return
    except ValueError as strict_error:
        if (
            approval.id != _LEGACY_V1_APPROVAL_ID
            or approval.actor != _LEGACY_V1_APPROVAL_ACTOR
            or approval.approved_at != _LEGACY_V1_APPROVED_AT
            or approval.workspace_id != _LEGACY_V1_WORKSPACE_ID
            or approval.registry_id != "synthetic_enterprise"
            or approval.registry_version != 1
            or approval.catalog_scope != "synthetic-demo"
            or approval.payload_fingerprint != _LEGACY_V1_REGISTRY_FINGERPRINT
            or registry.fingerprint != _LEGACY_V1_REGISTRY_FINGERPRINT
        ):
            raise strict_error
    scope = SemanticRegistryScope(
        workspace_id=approval.workspace_id,
        catalog_scope=approval.catalog_scope,
        registry_id=approval.registry_id,
    )
    migrated = RegistryPublicationApproval.model_validate(
        {
            **approval.model_dump(mode="python"),
            "id": registry_publication_approval_id(registry, scope, approval.actor),
        }
    )
    validate_registry_publication_approval(registry, migrated)


def _document_write(
    registry: GovernedSemanticRegistrySnapshot,
    approval: RegistryPublicationApproval,
    audit: PublicationTargetAuditRecord,
) -> DataHubRegistryDocumentWrite:
    scope = SemanticRegistryScope(
        workspace_id=approval.workspace_id,
        catalog_scope=approval.catalog_scope,
        registry_id=approval.registry_id,
    )
    document_id = datahub_registry_document_id(scope, registry.version)
    workspace_digest = hashlib.sha256(approval.workspace_id.encode()).hexdigest()[:24]
    registry_json = registry.model_dump_json()
    approval_json = approval.model_dump_json()
    audit_json = audit.model_dump_json()
    _validate_serialized_size(
        registry_json,
        _MAX_REGISTRY_JSON_BYTES,
        "registry snapshot",
    )
    _validate_serialized_size(
        approval_json,
        _MAX_APPROVAL_JSON_BYTES,
        "registry approval",
    )
    _validate_serialized_size(
        audit_json,
        _MAX_AUDIT_JSON_BYTES,
        "registry audit",
    )
    related_assets = semantic_registry_related_asset_urns(registry)
    if len(related_assets) > _MAX_RELATED_ASSETS or len(related_assets) != len(set(related_assets)):
        raise ValueError("registry related assets exceed their safe storage contract")
    return DataHubRegistryDocumentWrite(
        document_id=document_id,
        title=_registry_document_title(registry),
        text=_registry_document_text(registry),
        custom_properties={
            "schemabridge.registryFormatVersion": str(registry.format_version),
            "schemabridge.registryId": registry.registry_id,
            "schemabridge.registryVersion": str(registry.version),
            "schemabridge.registryCatalogScope": registry.catalog_scope,
            "schemabridge.registryWorkspaceDigest": workspace_digest,
            "schemabridge.registryFingerprint": registry.fingerprint,
            "schemabridge.registryDecisionIds": json.dumps(
                semantic_registry_decision_ids(registry),
                separators=(",", ":"),
            ),
            "schemabridge.registrySnapshot": registry_json,
            "schemabridge.registryPublicationApproval": approval_json,
            "schemabridge.registryPublicationAudit": audit_json,
        },
        related_asset_urns=related_assets,
    )


def _registry_document_title(registry: GovernedSemanticRegistrySnapshot) -> str:
    return f"SchemaBridge semantic registry {registry.registry_id} v{registry.version}"


def _registry_document_text(registry: GovernedSemanticRegistrySnapshot) -> str:
    return (
        "# Approved SchemaBridge semantic registry\n\n"
        f"Registry: `{registry.registry_id}` v{registry.version}\n\n"
        f"Catalog scope: `{registry.catalog_scope}`\n\n"
        f"Fingerprint: `{registry.fingerprint}`\n\n"
        f"Models/mappings/joins: {len(registry.logical_context.models)}/"
        f"{len(registry.mapping_set.mappings)}/{len(registry.join_contracts.contracts)}\n"
    )


def _publication_result(
    registry: GovernedSemanticRegistrySnapshot,
    approval: RegistryPublicationApproval,
    *,
    target: str,
    status: RegistryPublicationStatus,
    previous_fingerprint: str | None,
    reason_code: str | None = None,
) -> RegistryPublicationResult:
    outcome = {
        RegistryPublicationStatus.PUBLISHED: PublicationAuditOutcome.SUCCEEDED,
        RegistryPublicationStatus.ALREADY_CURRENT: PublicationAuditOutcome.ALREADY_CURRENT,
        RegistryPublicationStatus.FAILED: PublicationAuditOutcome.FAILED,
    }[status]
    return RegistryPublicationResult(
        approval_id=approval.id,
        workspace_id=approval.workspace_id,
        registry_id=registry.registry_id,
        registry_version=registry.version,
        fingerprint=registry.fingerprint,
        target=target,
        status=status,
        reason_code=reason_code,
        audit_record=_audit_record(
            registry,
            approval,
            target=target,
            outcome=outcome,
            previous_fingerprint=previous_fingerprint,
            reason_code=reason_code,
        ),
    )


def _audit_record(
    registry: GovernedSemanticRegistrySnapshot,
    approval: RegistryPublicationApproval,
    *,
    target: str,
    outcome: PublicationAuditOutcome,
    previous_fingerprint: str | None,
    reason_code: str | None = None,
) -> PublicationTargetAuditRecord:
    return PublicationTargetAuditRecord(
        family=PublicationFamily.REGISTRY,
        operation="versioned_document",
        target=target,
        approval_id=approval.id,
        actor=approval.actor,
        approved_at=approval.approved_at,
        previous_fingerprint=previous_fingerprint,
        new_fingerprint=registry.fingerprint,
        outcome=outcome,
        decision_ids=semantic_registry_decision_ids(registry),
        reason_code=reason_code,
    )


def _secure_env_values(
    path: Path,
    *,
    allowed_key_sets: tuple[frozenset[str], ...],
) -> dict[str, str]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("DataHub credential path must be a regular file")
    file_stat = path.stat()
    if file_stat.st_uid != os.getuid() or stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise ValueError("DataHub credential file must be owner-only")
    if file_stat.st_size > _MAX_ENV_BYTES:
        raise ValueError("DataHub credential file exceeds the configured bound")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key or key in values:
            raise ValueError("DataHub credential file contains an invalid or duplicate key")
        values[key] = value
    if not any(set(values) == allowed for allowed in allowed_key_sets):
        raise ValueError("DataHub credential file contains unexpected keys")
    mcp_flags = {
        "TOOLS_IS_MUTATION_ENABLED": "false",
        "SAVE_DOCUMENT_TOOL_ENABLED": "false",
        "DATAHUB_MCP_DOCUMENT_TOOLS_DISABLED": "true",
    }
    if any(key in values for key in mcp_flags) and any(
        values.get(key) != expected for key, expected in mcp_flags.items()
    ):
        raise ValueError("DataHub MCP credential flags are unsafe")
    server = values.get("DATAHUB_GMS_URL", "")
    token = values.get("DATAHUB_GMS_TOKEN", "")
    parsed_server = urllib.parse.urlsplit(server)
    if (
        parsed_server.scheme not in {"http", "https"}
        or parsed_server.hostname is None
        or parsed_server.username is not None
        or parsed_server.password is not None
        or parsed_server.query
        or parsed_server.fragment
        or parsed_server.path not in {"", "/"}
        or not token
        or token != token.strip()
        or (
            parsed_server.scheme == "http"
            and parsed_server.hostname not in {"127.0.0.1", "localhost", "::1"}
        )
    ):
        raise ValueError("DataHub credential values are invalid")
    return values


def _bounded_property(properties: Mapping[str, str], key: str, maximum: int) -> str:
    value = properties.get(key)
    if not isinstance(value, str):
        raise PlanningPortError(
            PlanningPortErrorCode.CONTEXT_INVALID,
            "DataHub registry property is missing",
        )
    size = len(value.encode())
    if size < 2 or size > maximum:
        raise PlanningPortError(
            PlanningPortErrorCode.CONTEXT_INVALID,
            "DataHub registry property size is outside the configured bound",
        )
    return value


def _validate_serialized_size(value: str, maximum: int, label: str) -> None:
    size = len(value.encode())
    if size < 2 or size > maximum:
        raise ValueError(f"{label} size is outside the configured bound")


def _load_unique_json(payload: str) -> object:
    return json.loads(payload, object_pairs_hook=_unique_json_pairs)


def _unique_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DataHub JSON repeats a key")
        result[key] = value
    return result


def _extract_aspect(
    payload: Mapping[str, object],
    expected_type: str,
) -> dict[str, Any]:
    aspect_envelope = payload.get("aspect")
    if not isinstance(aspect_envelope, dict):
        raise ValueError("DataHub aspect envelope is invalid")
    if set(aspect_envelope) != {expected_type}:
        raise ValueError("DataHub aspect type is unexpected")
    aspect = aspect_envelope.get(expected_type)
    if not isinstance(aspect, dict):
        raise ValueError("DataHub aspect payload is invalid")
    return aspect


def _planning_error_code(error: Exception) -> PlanningPortErrorCode:
    if isinstance(error, PermissionError):
        return PlanningPortErrorCode.CONTEXT_FORBIDDEN
    if isinstance(error, urllib.error.HTTPError):
        if error.code in {401, 403}:
            return PlanningPortErrorCode.CONTEXT_FORBIDDEN
        if error.code == 429 or error.code >= 500:
            return PlanningPortErrorCode.CONTEXT_UNAVAILABLE
        return PlanningPortErrorCode.CONTEXT_INVALID
    if isinstance(
        error,
        (
            ConnectionError,
            TimeoutError,
            urllib.error.URLError,
        ),
    ):
        return PlanningPortErrorCode.CONTEXT_UNAVAILABLE
    return PlanningPortErrorCode.CONTEXT_INVALID


def _publication_error_code(error: Exception) -> RegistryPublicationErrorCode:
    if isinstance(error, PermissionError):
        return RegistryPublicationErrorCode.CATALOG_PERMISSION_DENIED
    if isinstance(error, urllib.error.HTTPError):
        if error.code in {401, 403}:
            return RegistryPublicationErrorCode.CATALOG_PERMISSION_DENIED
        if error.code == 429 or error.code >= 500:
            return RegistryPublicationErrorCode.CATALOG_UNAVAILABLE
        return RegistryPublicationErrorCode.INVALID_RESPONSE
    if isinstance(
        error,
        (
            ConnectionError,
            TimeoutError,
            urllib.error.URLError,
        ),
    ):
        return RegistryPublicationErrorCode.CATALOG_UNAVAILABLE
    return RegistryPublicationErrorCode.INVALID_RESPONSE


def _reason_code(error: Exception) -> str:
    if isinstance(error, urllib.error.HTTPError):
        if error.code in {401, 403}:
            return "permission_denied"
        if error.code == 429 or error.code >= 500:
            return "catalog_unavailable"
        return f"http_{error.code}"
    if isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return "catalog_unavailable"
    return "post_write_verification_failed"
