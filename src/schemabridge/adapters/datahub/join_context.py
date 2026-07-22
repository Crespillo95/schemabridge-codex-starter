"""Approval-gated DataHub documents for versioned and reusable join contracts."""

from __future__ import annotations

import json
import logging
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
        current = self.load_current()
        if current is not None and current.fingerprint == publication.fingerprint:
            return _all_current(publication, approval)
        client = self._client()
        actions: tuple[tuple[JoinPublicationItemKind, str, Callable[[], None]], ...] = (
            (
                JoinPublicationItemKind.VERSIONED_DECISION_DOCUMENT,
                _versioned_document_urn(publication),
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
            if failed:
                results.append(
                    JoinPublicationItemResult(
                        kind=kind,
                        target=target,
                        status=JoinPublicationItemStatus.NOT_ATTEMPTED,
                        reason_code="prior_item_failed",
                    )
                )
                continue
            logger.info("datahub_join_publish operation=%s target=%s", kind.value, target)
            try:
                action()
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
                    JoinPublicationItemResult(
                        kind=kind,
                        target=target,
                        status=JoinPublicationItemStatus.FAILED,
                        reason_code=reason,
                    )
                )
            else:
                results.append(
                    JoinPublicationItemResult(
                        kind=kind,
                        target=target,
                        status=JoinPublicationItemStatus.PUBLISHED,
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

    def load_current(self) -> PublishedJoinContext | None:
        try:
            from datahub.metadata.schema_classes import DocumentInfoClass

            self._verify_runtime_identity()
            graph = self._client()._graph
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
    publication: JoinContractPublication, approval: JoinPublicationApproval
) -> JoinPublicationResult:
    return JoinPublicationResult(
        approval_id=approval.id,
        draft_id=publication.draft_id,
        fingerprint=publication.fingerprint,
        status=JoinPublicationStatus.ALREADY_CURRENT,
        items=(
            JoinPublicationItemResult(
                kind=JoinPublicationItemKind.VERSIONED_DECISION_DOCUMENT,
                target=_versioned_document_urn(publication),
                status=JoinPublicationItemStatus.ALREADY_CURRENT,
            ),
            JoinPublicationItemResult(
                kind=JoinPublicationItemKind.CURRENT_CONTEXT_MARKER,
                target=_CURRENT_DOCUMENT_URN,
                status=JoinPublicationItemStatus.ALREADY_CURRENT,
            ),
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
