"""Approval-gated DataHub documents for versioned and reusable join contracts."""

from __future__ import annotations

import json
import logging
import re
import stat
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from schemabridge.adapters.datahub.canonical_urns import DECISION_PROPERTY_URN
from schemabridge.adapters.datahub.decision_property import ensure_decision_property
from schemabridge.application.ports.relationships import (
    RelationshipErrorCode,
    RelationshipWorkflowError,
)
from schemabridge.domain.join_reviews import (
    JoinContractPublication,
    JoinPublicationApproval,
    JoinPublicationItemKind,
    JoinPublicationItemResult,
    JoinPublicationItemStatus,
    JoinPublicationResult,
    JoinPublicationStatus,
    PublishedJoinContext,
)
from schemabridge.domain.publication_audit import (
    PublicationAuditOutcome,
    PublicationFamily,
    PublicationTargetAuditRecord,
)

logger = logging.getLogger(__name__)
_CURRENT_DOCUMENT_URN = "urn:li:document:schemabridge-join-contracts-current"


@dataclass(frozen=True, slots=True)
class DataHubJoinContextConfig:
    server: str
    token: str
    actor_urn: str


class DataHubJoinContextAdapter:
    """Persist a versioned decision document before updating the reusable current marker."""

    def __init__(self, config: DataHubJoinContextConfig) -> None:
        self._config = config

    @classmethod
    def from_env_file(cls, path: Path) -> DataHubJoinContextAdapter:
        if not path.is_file():
            raise RelationshipWorkflowError(
                RelationshipErrorCode.CATALOG_UNAVAILABLE,
                "DataHub writer credential is absent; run make datahub-provision-writer",
            )
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise RelationshipWorkflowError(
                RelationshipErrorCode.CATALOG_PERMISSION_DENIED,
                "DataHub writer credential must be accessible only to its owner",
            )
        values = _read_env_file(path)
        try:
            config = DataHubJoinContextConfig(
                server=values["DATAHUB_GMS_URL"].rstrip("/"),
                token=values["DATAHUB_GMS_TOKEN"],
                actor_urn=values["DATAHUB_WRITER_ACTOR_URN"],
            )
        except KeyError as error:
            raise RelationshipWorkflowError(
                RelationshipErrorCode.CATALOG_UNAVAILABLE,
                "DataHub writer credential is incomplete",
            ) from error
        if not config.server.startswith(("http://", "https://")) or not config.token:
            raise RelationshipWorkflowError(
                RelationshipErrorCode.CATALOG_UNAVAILABLE,
                "DataHub writer credential is invalid",
            )
        return cls(config)

    def publish(
        self,
        publication: JoinContractPublication,
        approval: JoinPublicationApproval,
    ) -> JoinPublicationResult:
        _validate_approval(publication, approval)
        current = self._load_current(require_version=False)
        client = self._client()
        versioned_target = _versioned_document_urn(publication)
        try:
            previous_fingerprints = {
                JoinPublicationItemKind.VERSIONED_DECISION_DOCUMENT: self._document_fingerprint(
                    client, versioned_target
                ),
                JoinPublicationItemKind.CURRENT_CONTEXT_MARKER: self._document_fingerprint(
                    client, _CURRENT_DOCUMENT_URN
                ),
            }
        except Exception as error:
            raise RelationshipWorkflowError(
                _relationship_error_code(error),
                "DataHub join target state read failed",
            ) from error
        versioned_previous = previous_fingerprints[
            JoinPublicationItemKind.VERSIONED_DECISION_DOCUMENT
        ]
        if versioned_previous is not None and versioned_previous != publication.fingerprint:
            raise RelationshipWorkflowError(
                RelationshipErrorCode.CONFLICT,
                "DataHub immutable join version already identifies different content",
            )
        current_previous = previous_fingerprints[JoinPublicationItemKind.CURRENT_CONTEXT_MARKER]
        if (current is None) != (current_previous is None) or (
            current is not None and current.fingerprint != current_previous
        ):
            raise RelationshipWorkflowError(
                RelationshipErrorCode.CATALOG_INVALID_RESPONSE,
                "DataHub current join target changed or failed typed validation",
            )
        if all(previous == publication.fingerprint for previous in previous_fingerprints.values()):
            return _all_current(publication, approval, previous_fingerprints)
        actions: tuple[tuple[JoinPublicationItemKind, str, Callable[[], None]], ...] = (
            (
                JoinPublicationItemKind.VERSIONED_DECISION_DOCUMENT,
                versioned_target,
                lambda: self._upsert_document(client, publication, current_marker=False),
            ),
            (
                JoinPublicationItemKind.CURRENT_CONTEXT_MARKER,
                _CURRENT_DOCUMENT_URN,
                lambda: self._upsert_document(client, publication, current_marker=True),
            ),
        )
        results: list[JoinPublicationItemResult] = []
        failed = False
        for kind, target, action in actions:
            previous_fingerprint = previous_fingerprints[kind]
            if failed:
                results.append(
                    _item(
                        publication,
                        approval,
                        kind,
                        target,
                        JoinPublicationItemStatus.NOT_ATTEMPTED,
                        previous_fingerprint,
                        reason_code="prior_item_failed",
                    )
                )
                continue
            if previous_fingerprint == publication.fingerprint:
                results.append(
                    _item(
                        publication,
                        approval,
                        kind,
                        target,
                        JoinPublicationItemStatus.ALREADY_CURRENT,
                        previous_fingerprint,
                    )
                )
                continue
            logger.info("datahub_join_publish operation=%s target=%s", kind.value, target)
            try:
                action()
                if self._document_fingerprint(client, target) != publication.fingerprint:
                    raise ValueError("DataHub join target did not match its approved publication")
            except Exception as error:
                failed = True
                reason = _reason_code(error)
                logger.warning(
                    "datahub_join_publish_failed operation=%s target=%s code=%s",
                    kind.value,
                    target,
                    reason,
                )
                results.append(
                    _item(
                        publication,
                        approval,
                        kind,
                        target,
                        JoinPublicationItemStatus.FAILED,
                        previous_fingerprint,
                        reason_code=reason,
                    )
                )
            else:
                results.append(
                    _item(
                        publication,
                        approval,
                        kind,
                        target,
                        JoinPublicationItemStatus.PUBLISHED,
                        previous_fingerprint,
                    )
                )
        return JoinPublicationResult(
            approval_id=approval.id,
            draft_id=publication.draft_id,
            fingerprint=publication.fingerprint,
            status=(
                JoinPublicationStatus.PARTIAL_FAILURE if failed else JoinPublicationStatus.PUBLISHED
            ),
            items=tuple(results),
        )

    @staticmethod
    def _document_fingerprint(client: Any, document_urn: str) -> str | None:
        from datahub.metadata.schema_classes import DocumentInfoClass

        document = client._graph.get_aspect(document_urn, DocumentInfoClass)
        if document is None:
            return None
        fingerprint = document.customProperties.get("schemabridge.joinFingerprint")
        if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
            raise ValueError("DataHub join target fingerprint is invalid")
        serialized = document.customProperties.get("schemabridge.joinPublication")
        if not isinstance(serialized, str):
            raise ValueError("DataHub join target publication is missing")
        publication = JoinContractPublication.model_validate_json(serialized)
        if publication.fingerprint != fingerprint:
            raise ValueError("DataHub join target publication does not match its fingerprint")
        if (
            document_urn != _CURRENT_DOCUMENT_URN
            and _versioned_document_urn(publication) != document_urn
        ):
            raise ValueError("DataHub immutable join target does not match its publication version")
        expected_assets = set(_related_assets(publication))
        observed_assets = {asset.asset for asset in document.relatedAssets or ()}
        if observed_assets != expected_assets:
            raise ValueError("DataHub join target related assets do not match its publication")
        return fingerprint

    def load_current(self) -> PublishedJoinContext | None:
        return self._load_current(require_version=True)

    def _load_current(self, *, require_version: bool) -> PublishedJoinContext | None:
        try:
            from datahub.metadata.schema_classes import DocumentInfoClass

            self._verify_runtime_identity()
            client = self._client()
            graph = client._graph
            document = graph.get_aspect(_CURRENT_DOCUMENT_URN, DocumentInfoClass)
            if document is None:
                return None
            serialized = document.customProperties.get("schemabridge.joinPublication")
            fingerprint = document.customProperties.get("schemabridge.joinFingerprint")
            if not serialized or not fingerprint:
                return None
            publication = JoinContractPublication.model_validate_json(serialized)
            if publication.fingerprint != fingerprint:
                return None
            expected_assets = set(_related_assets(publication))
            observed_assets = {asset.asset for asset in document.relatedAssets or ()}
            if observed_assets != expected_assets:
                return None
            if require_version and (
                self._document_fingerprint(client, _versioned_document_urn(publication))
                != publication.fingerprint
            ):
                raise ValueError("DataHub immutable join version is missing or inconsistent")
            return PublishedJoinContext(
                document_urn=_CURRENT_DOCUMENT_URN,
                fingerprint=publication.fingerprint,
                contract_set=publication.contract_set,
                decision_ids=tuple(decision.id for decision in publication.decisions),
                related_asset_urns=tuple(sorted(expected_assets)),
            )
        except RelationshipWorkflowError:
            raise
        except Exception as error:
            raise RelationshipWorkflowError(
                _relationship_error_code(error),
                "DataHub join-context read failed",
            ) from error

    def _client(self) -> Any:
        try:
            from datahub.sdk.main_client import DataHubClient

            return DataHubClient(server=self._config.server, token=self._config.token)
        except ModuleNotFoundError as error:
            raise RelationshipWorkflowError(
                RelationshipErrorCode.CATALOG_UNAVAILABLE,
                "DataHub support is not installed; install schemabridge[datahub]",
            ) from error

    def _upsert_document(
        self,
        client: Any,
        publication: JoinContractPublication,
        *,
        current_marker: bool,
    ) -> None:
        from datahub.sdk.document import Document

        ensure_decision_property(self._graphql)

        document_id = (
            "schemabridge-join-contracts-current"
            if current_marker
            else f"schemabridge-join-contracts-v{publication.draft_version}"
        )
        contracts = "\n".join(
            (
                f"- `{contract.id}` v{contract.version}: "
                f"`{contract.left_key.logical_field.root}` → "
                f"`{contract.right_key.logical_field.root}`; "
                f"{contract.cardinality.value}; fanout `{contract.fanout_policy.value}`"
            )
            for contract in publication.contract_set.contracts
        )
        decisions = "\n".join(
            f"- `{decision.id}` by `{decision.actor}`: {decision.rationale}"
            for decision in publication.decisions
        )
        document = Document.create_document(
            id=document_id,
            title=(
                "SchemaBridge current approved join contracts"
                if current_marker
                else f"SchemaBridge join decisions v{publication.draft_version}"
            ),
            text=(
                "# Approved SchemaBridge join contracts\n\n"
                f"Payload fingerprint: `{publication.fingerprint}`\n\n"
                f"## Contracts\n{contracts}\n\n"
                f"## Explicit decisions\n{decisions}\n"
            ),
            related_assets=_related_assets(publication),
            custom_properties={
                "schemabridge.joinFingerprint": publication.fingerprint,
                "schemabridge.joinPublication": publication.model_dump_json(),
                "schemabridge.contractIds": ",".join(
                    contract.id for contract in publication.contract_set.contracts
                ),
            },
            structured_properties={
                DECISION_PROPERTY_URN: [decision.id for decision in publication.decisions]
            },
        )
        client.entities.upsert(document)

    def _verify_runtime_identity(self) -> None:
        query = """
        query VerifyJoinWriterRuntime {
          me {
            corpUser { urn }
            platformPrivileges {
              managePolicies manageIdentities manageIngestion manageSecrets manageTokens
              manageServiceAccounts manageGlossaries manageDocuments
              manageStructuredProperties viewStructuredPropertiesPage
            }
          }
        }
        """
        me = self._graphql(query, {}).get("me")
        if not isinstance(me, dict):
            raise RelationshipWorkflowError(
                RelationshipErrorCode.CATALOG_PERMISSION_DENIED,
                "DataHub writer identity could not be verified",
            )
        corp_user = me.get("corpUser")
        privileges = me.get("platformPrivileges")
        required = {
            "manageGlossaries",
            "manageDocuments",
            "manageStructuredProperties",
            "viewStructuredPropertiesPage",
        }
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
            raise RelationshipWorkflowError(
                RelationshipErrorCode.CATALOG_PERMISSION_DENIED,
                "DataHub writer identity or privileges do not match the bounded configuration",
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


def _validate_approval(
    publication: JoinContractPublication, approval: JoinPublicationApproval
) -> None:
    if not isinstance(approval, JoinPublicationApproval):
        raise RelationshipWorkflowError(
            RelationshipErrorCode.APPROVAL_REQUIRED,
            "explicit join publication approval is required",
        )
    if (
        approval.draft_id != publication.draft_id
        or approval.draft_version != publication.draft_version
        or approval.payload_fingerprint != publication.fingerprint
        or tuple(sorted(approval.decision_ids))
        != tuple(sorted(decision.id for decision in publication.decisions))
    ):
        raise RelationshipWorkflowError(
            RelationshipErrorCode.APPROVAL_MISMATCH,
            "join publication approval does not match the payload",
        )


def _versioned_document_urn(publication: JoinContractPublication) -> str:
    return f"urn:li:document:schemabridge-join-contracts-v{publication.draft_version}"


def _related_assets(publication: JoinContractPublication) -> tuple[str, ...]:
    datasets = {
        key.physical_field.root.rsplit(".", 1)[0]
        for contract in publication.contract_set.contracts
        for key in (contract.left_key, contract.right_key)
    }
    return tuple(
        sorted(
            f"urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge.{dataset},PROD)"
            for dataset in datasets
        )
    )


def _all_current(
    publication: JoinContractPublication,
    approval: JoinPublicationApproval,
    previous_fingerprints: dict[JoinPublicationItemKind, str | None],
) -> JoinPublicationResult:
    return JoinPublicationResult(
        approval_id=approval.id,
        draft_id=publication.draft_id,
        fingerprint=publication.fingerprint,
        status=JoinPublicationStatus.ALREADY_CURRENT,
        items=(
            _item(
                publication,
                approval,
                JoinPublicationItemKind.VERSIONED_DECISION_DOCUMENT,
                _versioned_document_urn(publication),
                JoinPublicationItemStatus.ALREADY_CURRENT,
                previous_fingerprints[JoinPublicationItemKind.VERSIONED_DECISION_DOCUMENT],
            ),
            _item(
                publication,
                approval,
                JoinPublicationItemKind.CURRENT_CONTEXT_MARKER,
                _CURRENT_DOCUMENT_URN,
                JoinPublicationItemStatus.ALREADY_CURRENT,
                previous_fingerprints[JoinPublicationItemKind.CURRENT_CONTEXT_MARKER],
            ),
        ),
    )


def _item(
    publication: JoinContractPublication,
    approval: JoinPublicationApproval,
    kind: JoinPublicationItemKind,
    target: str,
    status: JoinPublicationItemStatus,
    previous_fingerprint: str | None,
    *,
    reason_code: str | None = None,
) -> JoinPublicationItemResult:
    outcome = {
        JoinPublicationItemStatus.PUBLISHED: PublicationAuditOutcome.SUCCEEDED,
        JoinPublicationItemStatus.ALREADY_CURRENT: PublicationAuditOutcome.ALREADY_CURRENT,
        JoinPublicationItemStatus.FAILED: PublicationAuditOutcome.FAILED,
        JoinPublicationItemStatus.NOT_ATTEMPTED: PublicationAuditOutcome.NOT_ATTEMPTED,
    }[status]
    return JoinPublicationItemResult(
        kind=kind,
        target=target,
        status=status,
        reason_code=reason_code,
        audit_record=PublicationTargetAuditRecord(
            family=PublicationFamily.JOIN,
            operation=kind.value,
            target=target,
            approval_id=approval.id,
            actor=approval.actor,
            approved_at=approval.approved_at,
            previous_fingerprint=previous_fingerprint,
            new_fingerprint=publication.fingerprint,
            outcome=outcome,
            decision_ids=approval.decision_ids,
            reason_code=reason_code,
        ),
    )


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if separator and key:
            values[key] = value
    return values


def _reason_code(error: Exception) -> str:
    if isinstance(error, urllib.error.HTTPError):
        return "permission_denied" if error.code in {401, 403} else f"http_{error.code}"
    if isinstance(error, (urllib.error.URLError, TimeoutError)):
        return "catalog_unavailable"
    return f"external_{error.__class__.__name__.casefold()}"


def _relationship_error_code(error: Exception) -> RelationshipErrorCode:
    if isinstance(error, urllib.error.HTTPError) and error.code in {401, 403}:
        return RelationshipErrorCode.CATALOG_PERMISSION_DENIED
    if isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return RelationshipErrorCode.CATALOG_UNAVAILABLE
    return RelationshipErrorCode.CATALOG_INVALID_RESPONSE
