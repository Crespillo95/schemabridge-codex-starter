"""Deterministic catalog writer used by unit tests and explicit offline demos."""

from __future__ import annotations

from schemabridge.adapters.datahub.canonical_urns import (
    CUSTOMER_KEY_TERM_URN,
    DECISION_PROPERTY_URN,
    LOGICAL_CUSTOMER_URN,
    REGISTRATION_DATE_TERM_URN,
)
from schemabridge.application.ports.reviews import ReviewErrorCode, ReviewWorkflowError
from schemabridge.domain.decisions import ApprovalStatus, DecisionAction
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


class FakeCatalogWriteAdapter:
    """Inert writer with idempotency and injectable typed partial failure."""

    def __init__(self, fail_at: PublicationItemKind | None = None) -> None:
        self._fail_at = fail_at
        self._contexts: dict[str, PublishedCanonicalContext] = {}
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
        current = self._contexts.get(publication.fingerprint)
        if current is not None:
            return PublicationResult(
                approval_id=approval.id,
                draft_id=publication.draft_id,
                fingerprint=publication.fingerprint,
                status=PublicationStatus.ALREADY_CURRENT,
                items=tuple(
                    PublicationItemResult(
                        kind=kind,
                        target=target,
                        status=PublicationItemStatus.ALREADY_CURRENT,
                        decision_refs=decision_refs,
                    )
                    for kind, target in targets
                ),
            )

        items: list[PublicationItemResult] = []
        failed = False
        for kind, target in targets:
            if failed:
                items.append(
                    PublicationItemResult(
                        kind=kind,
                        target=target,
                        status=PublicationItemStatus.NOT_ATTEMPTED,
                        reason_code="prior_item_failed",
                        decision_refs=decision_refs,
                    )
                )
            elif kind is self._fail_at:
                failed = True
                items.append(
                    PublicationItemResult(
                        kind=kind,
                        target=target,
                        status=PublicationItemStatus.FAILED,
                        reason_code="injected_catalog_failure",
                        decision_refs=decision_refs,
                    )
                )
            else:
                items.append(
                    PublicationItemResult(
                        kind=kind,
                        target=target,
                        status=PublicationItemStatus.PUBLISHED,
                        decision_refs=decision_refs,
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
            self._contexts[publication.fingerprint] = PublishedCanonicalContext(
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
        return self._contexts.get(publication.fingerprint)


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
