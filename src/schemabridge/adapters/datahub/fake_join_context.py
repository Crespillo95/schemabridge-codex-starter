"""Deterministic approval-gated join context fake."""

from __future__ import annotations

from dataclasses import dataclass, field

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


@dataclass(slots=True)
class FakeJoinContextBackend:
    current: PublishedJoinContext | None = None
    versioned_fingerprints: dict[str, str] = field(default_factory=dict)


class FakeJoinContextAdapter:
    def __init__(
        self,
        backend: FakeJoinContextBackend | None = None,
        *,
        fail_at: JoinPublicationItemKind | None = None,
    ) -> None:
        self._backend = backend or FakeJoinContextBackend()
        self._fail_at = fail_at
        self.publish_calls = 0

    def publish(
        self,
        publication: JoinContractPublication,
        approval: JoinPublicationApproval,
    ) -> JoinPublicationResult:
        _validate_approval(publication, approval)
        self.publish_calls += 1
        versioned_target = _target(
            publication,
            JoinPublicationItemKind.VERSIONED_DECISION_DOCUMENT,
        )
        previous_fingerprints = {
            JoinPublicationItemKind.VERSIONED_DECISION_DOCUMENT: (
                self._backend.versioned_fingerprints.get(versioned_target)
            ),
            JoinPublicationItemKind.CURRENT_CONTEXT_MARKER: (
                self._backend.current.fingerprint if self._backend.current is not None else None
            ),
        }
        versioned_previous = previous_fingerprints[
            JoinPublicationItemKind.VERSIONED_DECISION_DOCUMENT
        ]
        if versioned_previous is not None and versioned_previous != publication.fingerprint:
            raise RelationshipWorkflowError(
                RelationshipErrorCode.CONFLICT,
                "immutable join decision document already identifies different content",
            )
        if all(previous == publication.fingerprint for previous in previous_fingerprints.values()):
            return _result(
                publication,
                approval,
                JoinPublicationStatus.ALREADY_CURRENT,
                (
                    JoinPublicationItemStatus.ALREADY_CURRENT,
                    JoinPublicationItemStatus.ALREADY_CURRENT,
                ),
                previous_fingerprints,
            )
        results: list[JoinPublicationItemResult] = []
        for kind in JoinPublicationItemKind:
            previous_fingerprint = previous_fingerprints[kind]
            if previous_fingerprint == publication.fingerprint:
                results.append(
                    _item(
                        publication,
                        approval,
                        kind,
                        JoinPublicationItemStatus.ALREADY_CURRENT,
                        previous_fingerprint,
                    )
                )
                continue
            if self._fail_at is kind:
                results.append(
                    _item(
                        publication,
                        approval,
                        kind,
                        JoinPublicationItemStatus.FAILED,
                        previous_fingerprint,
                        reason_code="injected_failure",
                    )
                )
                remaining = tuple(
                    item
                    for item in JoinPublicationItemKind
                    if item not in {r.kind for r in results}
                )
                results.extend(
                    _item(
                        publication,
                        approval,
                        item,
                        JoinPublicationItemStatus.NOT_ATTEMPTED,
                        previous_fingerprints[item],
                        reason_code="prior_item_failed",
                    )
                    for item in remaining
                )
                return JoinPublicationResult(
                    approval_id=approval.id,
                    draft_id=publication.draft_id,
                    fingerprint=publication.fingerprint,
                    status=JoinPublicationStatus.PARTIAL_FAILURE,
                    items=tuple(results),
                )
            if kind is JoinPublicationItemKind.VERSIONED_DECISION_DOCUMENT:
                self._backend.versioned_fingerprints[versioned_target] = publication.fingerprint
            else:
                self._backend.current = _context(publication)
            results.append(
                _item(
                    publication,
                    approval,
                    kind,
                    JoinPublicationItemStatus.PUBLISHED,
                    previous_fingerprint,
                )
            )
        return JoinPublicationResult(
            approval_id=approval.id,
            draft_id=publication.draft_id,
            fingerprint=publication.fingerprint,
            status=JoinPublicationStatus.PUBLISHED,
            items=tuple(results),
        )

    def load_current(self) -> PublishedJoinContext | None:
        return self._backend.current


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


def _context(publication: JoinContractPublication) -> PublishedJoinContext:
    return PublishedJoinContext(
        document_urn=_target(publication, JoinPublicationItemKind.CURRENT_CONTEXT_MARKER),
        fingerprint=publication.fingerprint,
        contract_set=publication.contract_set,
        decision_ids=tuple(decision.id for decision in publication.decisions),
        related_asset_urns=_related_assets(publication),
    )


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


def _target(publication: JoinContractPublication, kind: JoinPublicationItemKind) -> str:
    suffix = (
        f"v{publication.draft_version}"
        if kind is JoinPublicationItemKind.VERSIONED_DECISION_DOCUMENT
        else "current"
    )
    return f"urn:li:document:schemabridge-join-contracts-{suffix}"


def _result(
    publication: JoinContractPublication,
    approval: JoinPublicationApproval,
    status: JoinPublicationStatus,
    item_statuses: tuple[JoinPublicationItemStatus, JoinPublicationItemStatus],
    previous_fingerprints: dict[JoinPublicationItemKind, str | None],
) -> JoinPublicationResult:
    return JoinPublicationResult(
        approval_id=approval.id,
        draft_id=publication.draft_id,
        fingerprint=publication.fingerprint,
        status=status,
        items=tuple(
            _item(
                publication,
                approval,
                kind,
                item_status,
                previous_fingerprints[kind],
            )
            for kind, item_status in zip(JoinPublicationItemKind, item_statuses, strict=True)
        ),
    )


def _item(
    publication: JoinContractPublication,
    approval: JoinPublicationApproval,
    kind: JoinPublicationItemKind,
    status: JoinPublicationItemStatus,
    previous_fingerprint: str | None,
    *,
    reason_code: str | None = None,
) -> JoinPublicationItemResult:
    target = _target(publication, kind)
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
