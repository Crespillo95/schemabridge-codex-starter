"""Approval-gated, idempotent DataHub document for bounded workflow context."""

from __future__ import annotations

import json
import logging
import re
import stat
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from schemabridge.application.ports.workflows import WorkflowError, WorkflowErrorCode
from schemabridge.domain.publication_audit import (
    PublicationAuditOutcome,
    PublicationFamily,
    PublicationTargetAuditRecord,
)
from schemabridge.domain.workflows import (
    WorkflowPublicationApproval,
    WorkflowPublicationProposal,
    WorkflowPublicationResult,
    WorkflowPublicationStatus,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DataHubWorkflowPublicationConfig:
    server: str
    token: str
    actor_urn: str


class DataHubWorkflowPublicationAdapter:
    """Publish fingerprints and approval identity, never SQL, parameters, prompts, or rows."""

    def __init__(self, config: DataHubWorkflowPublicationConfig) -> None:
        self._config = config

    @classmethod
    def from_env_file(cls, path: Path) -> DataHubWorkflowPublicationAdapter:
        if not path.is_file():
            raise WorkflowError(
                WorkflowErrorCode.PUBLICATION_FAILED,
                "DataHub writer credential is absent; run make datahub-provision-writer",
            )
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise WorkflowError(
                WorkflowErrorCode.PUBLICATION_FAILED,
                "DataHub writer credential must be accessible only to its owner",
            )
        values = _read_env_file(path)
        try:
            config = DataHubWorkflowPublicationConfig(
                server=values["DATAHUB_GMS_URL"].rstrip("/"),
                token=values["DATAHUB_GMS_TOKEN"],
                actor_urn=values["DATAHUB_WRITER_ACTOR_URN"],
            )
        except KeyError as error:
            raise WorkflowError(
                WorkflowErrorCode.PUBLICATION_FAILED,
                "DataHub writer credential is incomplete",
            ) from error
        if not config.server.startswith(("http://", "https://")) or not config.token:
            raise WorkflowError(
                WorkflowErrorCode.PUBLICATION_FAILED,
                "DataHub writer credential is invalid",
            )
        return cls(config)

    def publish(
        self,
        proposal: WorkflowPublicationProposal,
        approval: WorkflowPublicationApproval,
    ) -> WorkflowPublicationResult:
        _validate_approval(proposal, approval)
        document_ref = _document_urn(proposal)
        previous_fingerprint: str | None = None
        try:
            self._verify_runtime_identity()
            client = self._client()
            previous_fingerprint = self._current_fingerprint(client, proposal)
            if previous_fingerprint is not None and previous_fingerprint != proposal.fingerprint:
                return _result(
                    proposal,
                    approval,
                    document_ref,
                    WorkflowPublicationStatus.FAILED,
                    previous_fingerprint,
                    datetime.now(UTC),
                    failure_code="workflow_version_conflict",
                )
            if self._is_current(client, proposal, approval):
                return _result(
                    proposal,
                    approval,
                    document_ref,
                    WorkflowPublicationStatus.ALREADY_CURRENT,
                    previous_fingerprint,
                    approval.approved_at,
                )
            logger.info(
                "datahub_workflow_publish operation=upsert_document target=%s",
                document_ref,
            )
            success_audit = _audit_record(
                proposal,
                approval,
                document_ref,
                PublicationAuditOutcome.SUCCEEDED,
                previous_fingerprint,
            )
            self._upsert_document(client, proposal, approval, success_audit)
            if not self._is_current(
                client,
                proposal,
                approval,
                expected_audit=success_audit,
            ):
                raise ValueError("workflow document read-back did not match")
            return _result(
                proposal,
                approval,
                document_ref,
                WorkflowPublicationStatus.CREATED,
                previous_fingerprint,
                datetime.now(UTC),
            )
        except WorkflowError:
            raise
        except Exception:
            logger.warning(
                "datahub_workflow_publish_failed operation=upsert_document target=%s",
                document_ref,
            )
            return _result(
                proposal,
                approval,
                document_ref,
                WorkflowPublicationStatus.FAILED,
                previous_fingerprint,
                datetime.now(UTC),
                failure_code="datahub_publication_failed",
            )

    def _client(self) -> Any:
        try:
            from datahub.sdk.main_client import DataHubClient

            return DataHubClient(server=self._config.server, token=self._config.token)
        except ModuleNotFoundError as error:
            raise WorkflowError(
                WorkflowErrorCode.PUBLICATION_FAILED,
                "DataHub support is not installed; install schemabridge[datahub]",
            ) from error

    def _is_current(
        self,
        client: Any,
        proposal: WorkflowPublicationProposal,
        approval: WorkflowPublicationApproval,
        *,
        expected_audit: PublicationTargetAuditRecord | None = None,
    ) -> bool:
        from datahub.metadata.schema_classes import DocumentInfoClass

        document = client._graph.get_aspect(_document_urn(proposal), DocumentInfoClass)
        if document is None:
            return False
        properties = document.customProperties
        fingerprints_match = bool(
            properties.get("schemabridge.workflowId") == proposal.workflow_id
            and properties.get("schemabridge.workflowFingerprint") == proposal.fingerprint
            and properties.get("schemabridge.workflowIdempotencyKey") == proposal.idempotency_key
            and properties.get("schemabridge.planFingerprint") == proposal.plan_fingerprint
            and properties.get("schemabridge.executionFingerprint")
            == proposal.execution_fingerprint
            and properties.get("schemabridge.requestFingerprint") == proposal.request_fingerprint
        )
        approval_matches = bool(
            properties.get("schemabridge.approvalId") == approval.id
            and properties.get("schemabridge.approvedBy") == approval.actor
            and properties.get("schemabridge.approvedAt") == approval.approved_at.isoformat()
        )
        serialized_audit = properties.get("schemabridge.publicationAudit")
        if not fingerprints_match or not approval_matches or not isinstance(serialized_audit, str):
            return False
        try:
            audit_record = PublicationTargetAuditRecord.model_validate_json(serialized_audit)
        except ValueError:
            return False
        if expected_audit is not None:
            return audit_record == expected_audit
        return bool(
            audit_record.family is PublicationFamily.WORKFLOW
            and audit_record.operation == "upsert_document"
            and audit_record.target == _document_urn(proposal)
            and audit_record.approval_id == approval.id
            and audit_record.actor == approval.actor
            and audit_record.approved_at == approval.approved_at
            and audit_record.new_fingerprint == proposal.fingerprint
            and audit_record.outcome is PublicationAuditOutcome.SUCCEEDED
            and audit_record.reason_code is None
            and not audit_record.decision_ids
        )

    @staticmethod
    def _current_fingerprint(client: Any, proposal: WorkflowPublicationProposal) -> str | None:
        from datahub.metadata.schema_classes import DocumentInfoClass

        document = client._graph.get_aspect(_document_urn(proposal), DocumentInfoClass)
        if document is None:
            return None
        fingerprint = document.customProperties.get("schemabridge.workflowFingerprint")
        if fingerprint is None:
            return None
        if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
            raise ValueError("DataHub workflow fingerprint is invalid")
        return fingerprint

    def _upsert_document(
        self,
        client: Any,
        proposal: WorkflowPublicationProposal,
        approval: WorkflowPublicationApproval,
        audit_record: PublicationTargetAuditRecord,
    ) -> None:
        from datahub.sdk.document import Document

        document = Document.create_document(
            id=_document_id(proposal),
            title=f"SchemaBridge approved workflow context: {proposal.workflow_id}",
            text=(
                "# Approved SchemaBridge workflow context\n\n"
                "This document records only governed request, plan, and execution fingerprints. "
                "It intentionally contains no SQL, parameters, preview rows, prompt, credentials, "
                "or private reasoning.\n"
            ),
            custom_properties={
                "schemabridge.workflowId": proposal.workflow_id,
                "schemabridge.workflowFingerprint": proposal.fingerprint,
                "schemabridge.workflowIdempotencyKey": proposal.idempotency_key,
                "schemabridge.planFingerprint": proposal.plan_fingerprint,
                "schemabridge.executionFingerprint": proposal.execution_fingerprint,
                "schemabridge.requestFingerprint": proposal.request_fingerprint,
                "schemabridge.approvalId": approval.id,
                "schemabridge.approvedBy": approval.actor,
                "schemabridge.approvedAt": approval.approved_at.isoformat(),
                "schemabridge.publicationAudit": audit_record.model_dump_json(),
            },
        )
        client.entities.upsert(document)

    def _verify_runtime_identity(self) -> None:
        query = """
        query VerifyWorkflowWriterRuntime {
          me {
            corpUser { urn }
            platformPrivileges {
              managePolicies manageIdentities manageIngestion manageSecrets manageTokens
              manageServiceAccounts manageDocuments
            }
          }
        }
        """
        me = self._graphql(query, {}).get("me")
        if not isinstance(me, dict):
            raise WorkflowError(
                WorkflowErrorCode.PUBLICATION_FAILED,
                "DataHub workflow writer identity could not be verified",
            )
        corp_user = me.get("corpUser")
        privileges = me.get("platformPrivileges")
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
            or not privileges.get("manageDocuments")
            or any(privileges.get(name) for name in forbidden)
        ):
            raise WorkflowError(
                WorkflowErrorCode.PUBLICATION_FAILED,
                "DataHub workflow writer identity or privileges are outside policy",
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
    proposal: WorkflowPublicationProposal,
    approval: WorkflowPublicationApproval,
) -> None:
    if not isinstance(approval, WorkflowPublicationApproval):
        raise WorkflowError(
            WorkflowErrorCode.PUBLICATION_FAILED,
            "explicit workflow publication approval is required",
        )
    if (
        approval.workflow_id != proposal.workflow_id
        or approval.proposal_fingerprint != proposal.fingerprint
        or approval.idempotency_key != proposal.idempotency_key
    ):
        raise WorkflowError(
            WorkflowErrorCode.PUBLICATION_FAILED,
            "workflow publication approval does not match the proposal",
        )


def _result(
    proposal: WorkflowPublicationProposal,
    approval: WorkflowPublicationApproval,
    document_ref: str,
    status: WorkflowPublicationStatus,
    previous_fingerprint: str | None,
    published_at: datetime,
    *,
    failure_code: str | None = None,
) -> WorkflowPublicationResult:
    outcome = {
        WorkflowPublicationStatus.CREATED: PublicationAuditOutcome.SUCCEEDED,
        WorkflowPublicationStatus.ALREADY_CURRENT: PublicationAuditOutcome.ALREADY_CURRENT,
        WorkflowPublicationStatus.FAILED: PublicationAuditOutcome.FAILED,
    }[status]
    return WorkflowPublicationResult(
        status=status,
        approval_id=approval.id,
        proposal_fingerprint=proposal.fingerprint,
        idempotency_key=proposal.idempotency_key,
        document_ref=document_ref,
        published_at=published_at,
        failure_code=failure_code,
        audit_record=_audit_record(
            proposal,
            approval,
            document_ref,
            outcome,
            previous_fingerprint,
            reason_code=failure_code,
        ),
    )


def _audit_record(
    proposal: WorkflowPublicationProposal,
    approval: WorkflowPublicationApproval,
    document_ref: str,
    outcome: PublicationAuditOutcome,
    previous_fingerprint: str | None,
    *,
    reason_code: str | None = None,
) -> PublicationTargetAuditRecord:
    return PublicationTargetAuditRecord(
        family=PublicationFamily.WORKFLOW,
        operation="upsert_document",
        target=document_ref,
        approval_id=approval.id,
        actor=approval.actor,
        approved_at=approval.approved_at,
        previous_fingerprint=previous_fingerprint,
        new_fingerprint=proposal.fingerprint,
        outcome=outcome,
        reason_code=reason_code,
    )


def _document_id(proposal: WorkflowPublicationProposal) -> str:
    return f"schemabridge-workflow-{proposal.idempotency_key}"


def _document_urn(proposal: WorkflowPublicationProposal) -> str:
    return f"urn:li:document:{_document_id(proposal)}"


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values
