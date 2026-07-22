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


@dataclass(slots=True)
class FakeJoinContextBackend:
    current: PublishedJoinContext | None = None
    versioned_fingerprints: set[str] = field(default_factory=set)


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
        if self._backend.current is not None and (
            self._backend.current.fingerprint == publication.fingerprint
        ):
            return _result(
                publication,
                approval,
                JoinPublicationStatus.ALREADY_CURRENT,
                (
                    JoinPublicationItemStatus.ALREADY_CURRENT,
                    JoinPublicationItemStatus.ALREADY_CURRENT,
                ),
            )
        results: list[JoinPublicationItemResult] = []
        for kind in JoinPublicationItemKind:
            if self._fail_at is kind:
                results.append(
                    JoinPublicationItemResult(
                        kind=kind,
                        target=_target(publication, kind),
                        status=JoinPublicationItemStatus.FAILED,
                        reason_code="injected_failure",
                    )
                )
                remaining = tuple(
                    item
                    for item in JoinPublicationItemKind
                    if item not in {r.kind for r in results}
                )
                results.extend(
                    JoinPublicationItemResult(
                        kind=item,
                        target=_target(publication, item),
                        status=JoinPublicationItemStatus.NOT_ATTEMPTED,
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
                self._backend.versioned_fingerprints.add(publication.fingerprint)
            else:
                self._backend.current = _context(publication)
            results.append(
                JoinPublicationItemResult(
                    kind=kind,
                    target=_target(publication, kind),
                    status=JoinPublicationItemStatus.PUBLISHED,
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
) -> JoinPublicationResult:
    return JoinPublicationResult(
        approval_id=approval.id,
        draft_id=publication.draft_id,
        fingerprint=publication.fingerprint,
        status=status,
        items=tuple(
            JoinPublicationItemResult(
                kind=kind,
                target=_target(publication, kind),
                status=item_status,
            )
            for kind, item_status in zip(JoinPublicationItemKind, item_statuses, strict=True)
        ),
    )
