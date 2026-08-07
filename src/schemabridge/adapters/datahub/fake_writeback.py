"""Deterministic catalog writer used by unit tests and explicit offline demos."""

from __future__ import annotations

from dataclasses import dataclass, field

from schemabridge.adapters.datahub.canonical_urns import (
    CUSTOMER_KEY_TERM_URN,
    DECISION_PROPERTY_URN,
    LOGICAL_CUSTOMER_URN,
    REGISTRATION_DATE_TERM_URN,
)
from schemabridge.application.ports.reviews import ReviewErrorCode, ReviewWorkflowError
from schemabridge.domain.decisions import ApprovalStatus, DecisionAction
from schemabridge.domain.publication_audit import (
    PublicationAuditOutcome,
    PublicationFamily,
    PublicationTargetAuditRecord,
)
from schemabridge.domain.reviews import (
    CanonicalPublication,
    PublicationApproval,
    PublicationDecisionRef,
    PublicationItemKind,
    PublicationItemResult,
    PublicationItemStatus,
    PublicationResult,
    PublicationStatus,
    PublishedCanonicalContext,
)

_UNFINGERPRINTED_TARGET_KINDS = {
    PublicationItemKind.STRUCTURED_PROPERTY,
    PublicationItemKind.PHYSICAL_LINK,
}


@dataclass(slots=True)
class FakeCatalogWriteBackend:
    """Shared per-target catalog state for partial-failure retry tests."""

    current: PublishedCanonicalContext | None = None
    target_fingerprints: dict[tuple[PublicationItemKind, str], str] = field(default_factory=dict)


class FakeCatalogWriteAdapter:
    """Inert writer with idempotency and injectable typed partial failure."""

    def __init__(
        self,
        fail_at: PublicationItemKind | None = None,
        *,
        backend: FakeCatalogWriteBackend | None = None,
    ) -> None:
        self._fail_at = fail_at
        self._backend = backend or FakeCatalogWriteBackend()
        self.publish_calls = 0

    def publish(
        self,
        publication: CanonicalPublication,
        approval: PublicationApproval,
    ) -> PublicationResult:
        _validate_approval(publication, approval)
        self.publish_calls += 1
        targets = _publication_targets(publication)
        decision_refs = _decision_refs(publication)
        previous_fingerprints = {
            (kind, target): (
                None
                if kind in _UNFINGERPRINTED_TARGET_KINDS
                else self._backend.target_fingerprints.get((kind, target))
            )
            for kind, target in targets
        }
        document_previous = next(
            previous_fingerprints[(kind, target)]
            for kind, target in targets
            if kind is PublicationItemKind.DECISION_DOCUMENT
        )
        if document_previous is not None and document_previous != publication.fingerprint:
            raise ReviewWorkflowError(
                ReviewErrorCode.CONFLICT,
                "immutable canonical decision document already identifies different content",
            )
        if all(previous == publication.fingerprint for previous in previous_fingerprints.values()):
            return PublicationResult(
                approval_id=approval.id,
                draft_id=publication.draft_id,
                fingerprint=publication.fingerprint,
                status=PublicationStatus.ALREADY_CURRENT,
                items=tuple(
                    _item(
                        publication,
                        approval,
                        kind,
                        target,
                        PublicationItemStatus.ALREADY_CURRENT,
                        decision_refs,
                        previous_fingerprints[(kind, target)],
                    )
                    for kind, target in targets
                ),
            )

        items: list[PublicationItemResult] = []
        failed = False
        for kind, target in targets:
            previous_fingerprint = previous_fingerprints[(kind, target)]
            if failed:
                items.append(
                    _item(
                        publication,
                        approval,
                        kind,
                        target,
                        PublicationItemStatus.NOT_ATTEMPTED,
                        decision_refs,
                        previous_fingerprint,
                        reason_code="prior_item_failed",
                    )
                )
            elif previous_fingerprint == publication.fingerprint:
                items.append(
                    _item(
                        publication,
                        approval,
                        kind,
                        target,
                        PublicationItemStatus.ALREADY_CURRENT,
                        decision_refs,
                        previous_fingerprint,
                    )
                )
            elif kind is self._fail_at:
                failed = True
                items.append(
                    _item(
                        publication,
                        approval,
                        kind,
                        target,
                        PublicationItemStatus.FAILED,
                        decision_refs,
                        previous_fingerprint,
                        reason_code="injected_catalog_failure",
                    )
                )
            else:
                if kind not in _UNFINGERPRINTED_TARGET_KINDS:
                    self._backend.target_fingerprints[(kind, target)] = publication.fingerprint
                items.append(
                    _item(
                        publication,
                        approval,
                        kind,
                        target,
                        PublicationItemStatus.PUBLISHED,
                        decision_refs,
                        previous_fingerprint,
                    )
                )

        status = PublicationStatus.PARTIAL_FAILURE if failed else PublicationStatus.PUBLISHED
        if not failed:
            links = tuple(
                sorted(
                    {
                        mapping.physical_field.root.rsplit(".", 1)[0]
                        for mapping in publication.mappings
                    }
                )
            )
            self._backend.current = PublishedCanonicalContext(
                logical_model_urn=LOGICAL_CUSTOMER_URN,
                fingerprint=publication.fingerprint,
                fields=tuple(field.id for field in publication.fields),
                physical_links=links,
                glossary_terms=(CUSTOMER_KEY_TERM_URN, REGISTRATION_DATE_TERM_URN),
                decision_document_urn=_document_urn(publication),
                decision_refs=decision_refs,
            )
        return PublicationResult(
            approval_id=approval.id,
            draft_id=publication.draft_id,
            fingerprint=publication.fingerprint,
            status=status,
            items=tuple(items),
        )

    def read_context(
        self,
        publication: CanonicalPublication,
    ) -> PublishedCanonicalContext | None:
        if (
            self._backend.current is not None
            and self._backend.current.fingerprint == publication.fingerprint
        ):
            return self._backend.current
        return None


def _item(
    publication: CanonicalPublication,
    approval: PublicationApproval,
    kind: PublicationItemKind,
    target: str,
    status: PublicationItemStatus,
    decision_refs: tuple[PublicationDecisionRef, ...],
    previous_fingerprint: str | None,
    *,
    reason_code: str | None = None,
) -> PublicationItemResult:
    outcome = {
        PublicationItemStatus.PUBLISHED: PublicationAuditOutcome.SUCCEEDED,
        PublicationItemStatus.ALREADY_CURRENT: PublicationAuditOutcome.ALREADY_CURRENT,
        PublicationItemStatus.FAILED: PublicationAuditOutcome.FAILED,
        PublicationItemStatus.NOT_ATTEMPTED: PublicationAuditOutcome.NOT_ATTEMPTED,
    }[status]
    return PublicationItemResult(
        kind=kind,
        target=target,
        status=status,
        decision_refs=decision_refs,
        reason_code=reason_code,
        audit_record=PublicationTargetAuditRecord(
            family=PublicationFamily.CANONICAL,
            operation=kind.value,
            target=target,
            approval_id=approval.id,
            actor=approval.actor,
            approved_at=approval.approved_at,
            previous_fingerprint=previous_fingerprint,
            new_fingerprint=publication.fingerprint,
            outcome=outcome,
            decision_ids=tuple(reference.id for reference in decision_refs),
            reason_code=reason_code,
        ),
    )


def _validate_approval(
    publication: CanonicalPublication,
    approval: PublicationApproval,
) -> None:
    if not isinstance(approval, PublicationApproval):
        raise ReviewWorkflowError(
            ReviewErrorCode.APPROVAL_REQUIRED,
            "explicit publication approval is required",
        )
    if (
        approval.draft_id != publication.draft_id
        or approval.draft_version != publication.draft_version
        or approval.payload_fingerprint != publication.fingerprint
    ):
        raise ReviewWorkflowError(
            ReviewErrorCode.APPROVAL_MISMATCH,
            "catalog approval does not match the publication payload",
        )


def _decision_refs(
    publication: CanonicalPublication,
) -> tuple[PublicationDecisionRef, ...]:
    return tuple(
        PublicationDecisionRef(
            id=decision.id,
            target_id=decision.target_id,
            version=decision.resulting_version,
        )
        for decision in publication.decisions
        if decision.action is DecisionAction.APPROVE and decision.status is ApprovalStatus.APPROVED
    )


def _publication_targets(
    publication: CanonicalPublication,
) -> tuple[tuple[PublicationItemKind, str], ...]:
    physical_datasets = tuple(
        sorted({mapping.physical_field.root.rsplit(".", 1)[0] for mapping in publication.mappings})
    )
    targets: list[tuple[PublicationItemKind, str]] = [
        (PublicationItemKind.STRUCTURED_PROPERTY, DECISION_PROPERTY_URN),
        (PublicationItemKind.GLOSSARY_TERM, CUSTOMER_KEY_TERM_URN),
        (PublicationItemKind.GLOSSARY_TERM, REGISTRATION_DATE_TERM_URN),
        (PublicationItemKind.LOGICAL_MODEL, LOGICAL_CUSTOMER_URN),
    ]
    targets.extend((PublicationItemKind.PHYSICAL_LINK, dataset) for dataset in physical_datasets)
    targets.extend(
        (
            (PublicationItemKind.DECISION_DOCUMENT, _document_urn(publication)),
            (PublicationItemKind.PUBLICATION_MARKER, LOGICAL_CUSTOMER_URN),
        )
    )
    return tuple(targets)


def _document_urn(publication: CanonicalPublication) -> str:
    return f"urn:li:document:schemabridge-{publication.draft_id}-v{publication.draft_version}"
