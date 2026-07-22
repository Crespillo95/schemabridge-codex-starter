"""Bounded join discovery plus explicit review, approval, publication, and reuse."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from schemabridge.application.ports.catalog import (
    CatalogReadError,
    CatalogReadPort,
    EvidenceStatus,
    LineageDirection,
    PageRequest,
)
from schemabridge.application.ports.relationships import (
    JoinContextReadPort,
    JoinContextWritePort,
    JoinReviewStorePort,
    RelationshipErrorCode,
    RelationshipEvidencePort,
    RelationshipWorkflowError,
)
from schemabridge.domain.decisions import (
    ApprovalStatus,
    DecisionAction,
    DecisionRecord,
    DecisionTargetType,
)
from schemabridge.domain.join_reviews import (
    JoinContractPublication,
    JoinPublicationApproval,
    JoinPublicationResult,
    JoinReviewDraft,
    PublishedJoinContext,
    ReviewedJoinCandidate,
)
from schemabridge.domain.joins import (
    Cardinality,
    FanoutPolicy,
    JoinCandidate,
    JoinContract,
    JoinProposal,
    JoinRecommendation,
    ObservedJoinSignal,
    RelationshipContextEvidence,
    score_join_candidate,
)


@dataclass(frozen=True, slots=True)
class JoinDiscoveryReport:
    catalog_source: str
    candidates: tuple[JoinCandidate, ...]


@dataclass(frozen=True, slots=True)
class JoinReviewSnapshot:
    draft: JoinReviewDraft
    decisions: tuple[DecisionRecord, ...]
    publications: tuple[JoinPublicationResult, ...]


@dataclass(frozen=True, slots=True)
class DiscoverJoinCandidates:
    catalog: CatalogReadPort
    evidence: RelationshipEvidencePort

    def execute(self, proposals: tuple[JoinProposal, ...]) -> JoinDiscoveryReport:
        if not proposals or len(proposals) > 2:
            raise RelationshipWorkflowError(
                RelationshipErrorCode.INVALID_TRANSITION,
                "join discovery requires one or two explicit proposals",
            )
        if len({proposal.id for proposal in proposals}) != len(proposals):
            raise RelationshipWorkflowError(
                RelationshipErrorCode.INVALID_TRANSITION,
                "join discovery proposals must be unique",
            )
        candidates: list[JoinCandidate] = []
        for proposal in proposals:
            try:
                profile = self.evidence.profile(proposal)
                context = self._catalog_context(proposal)
            except RelationshipWorkflowError:
                raise
            except CatalogReadError as error:
                raise RelationshipWorkflowError(
                    RelationshipErrorCode.CATALOG_UNAVAILABLE,
                    f"catalog relationship evidence failed during {error.operation}",
                ) from error
            candidates.append(score_join_candidate(proposal, profile, context))
        return JoinDiscoveryReport(
            catalog_source=self.catalog.source_label,
            candidates=tuple(candidates),
        )

    def _catalog_context(self, proposal: JoinProposal) -> RelationshipContextEvidence:
        left_dataset = proposal.left_key.physical_field.root.rsplit(".", 1)[0]
        right_dataset = proposal.right_key.physical_field.root.rsplit(".", 1)[0]
        from schemabridge.domain.fields import PhysicalDatasetRef

        left_ref = PhysicalDatasetRef(left_dataset)
        right_ref = PhysicalDatasetRef(right_dataset)
        left_description = self._field_description(proposal.left_key.physical_field.root, left_ref)
        right_description = self._field_description(
            proposal.right_key.physical_field.root, right_ref
        )
        left_lineage = self.catalog.list_lineage_paths(
            left_ref, LineageDirection.DOWNSTREAM, PageRequest(size=50)
        )
        right_lineage = self.catalog.list_lineage_paths(
            right_ref, LineageDirection.UPSTREAM, PageRequest(size=50)
        )
        paths = {
            (path.source.root, path.target.root, path.hops)
            for page in (left_lineage, right_lineage)
            for path in page.page.items
            if path.source == left_ref and path.target == right_ref
        }
        lineage = _catalog_observation(
            statuses=(left_lineage.status, right_lineage.status),
            found=bool(paths),
            present_detail=(f"catalog records {len(paths)} directed lineage path(s)"),
            absent_detail="catalog lineage is present but does not connect the proposed assets",
            missing_reason=left_lineage.reason_code
            or right_lineage.reason_code
            or "lineage_missing",
        )
        left_queries = self.catalog.list_query_context(left_ref, PageRequest(size=50))
        right_queries = self.catalog.list_query_context(right_ref, PageRequest(size=50))
        shared = {
            query.urn
            for page in (left_queries, right_queries)
            for query in page.page.items
            if {left_ref, right_ref} <= set(query.subjects)
        }
        query_usage = _catalog_observation(
            statuses=(left_queries.status, right_queries.status),
            found=bool(shared),
            present_detail=f"catalog records {len(shared)} shared historical query context(s)",
            absent_detail="catalog query context is present but does not use both assets",
            missing_reason=(
                left_queries.reason_code or right_queries.reason_code or "query_context_missing"
            ),
        )
        return RelationshipContextEvidence(
            lineage=lineage,
            historical_query_usage=query_usage,
            left_description=left_description,
            right_description=right_description,
            alternative_path_count=len(paths),
        )

    def _field_description(self, field_root: str, dataset: object) -> str | None:
        from schemabridge.domain.fields import PhysicalDatasetRef

        if not isinstance(dataset, PhysicalDatasetRef):
            raise TypeError("dataset identity must be a physical dataset reference")
        page = self.catalog.list_schema_fields(dataset, PageRequest(size=50))
        selected = next((field for field in page.items if field.id.root == field_root), None)
        if selected is None:
            raise RelationshipWorkflowError(
                RelationshipErrorCode.NOT_FOUND,
                f"catalog field {field_root} was not found in the bounded schema page",
            )
        return selected.description


@dataclass(frozen=True, slots=True)
class StartJoinReview:
    store: JoinReviewStorePort

    def execute(self, draft: JoinReviewDraft) -> JoinReviewDraft:
        return self.store.create(draft)


@dataclass(frozen=True, slots=True)
class InspectJoinReview:
    store: JoinReviewStorePort

    def execute(self, draft_id: str) -> JoinReviewSnapshot:
        draft = _required_draft(self.store, draft_id)
        return JoinReviewSnapshot(
            draft=draft,
            decisions=self.store.list_decisions(draft_id),
            publications=self.store.list_publications(draft_id),
        )


@dataclass(frozen=True, slots=True)
class DecideJoinCandidate:
    store: JoinReviewStorePort

    def execute(
        self,
        draft_id: str,
        proposal_id: str,
        action: DecisionAction,
        *,
        expected_revision: int,
        actor: str,
        decided_at: datetime,
        rationale: str,
    ) -> JoinReviewSnapshot:
        if action not in {DecisionAction.APPROVE, DecisionAction.REJECT}:
            raise RelationshipWorkflowError(
                RelationshipErrorCode.INVALID_TRANSITION,
                "join decision must explicitly approve or reject",
            )
        draft = _required_draft(self.store, draft_id)
        if draft.revision != expected_revision:
            raise RelationshipWorkflowError(
                RelationshipErrorCode.CONFLICT,
                "join review revision changed; reload before deciding",
            )
        selected = next(
            (item for item in draft.joins if item.candidate.proposal.id == proposal_id), None
        )
        if selected is None:
            raise RelationshipWorkflowError(
                RelationshipErrorCode.NOT_FOUND, "join candidate was not found"
            )
        if selected.status is not ApprovalStatus.NEEDS_REVIEW:
            raise RelationshipWorkflowError(
                RelationshipErrorCode.INVALID_TRANSITION, "join candidate was already decided"
            )
        if (
            action is DecisionAction.APPROVE
            and selected.candidate.recommendation is JoinRecommendation.REJECT_UNSAFE
        ):
            raise RelationshipWorkflowError(
                RelationshipErrorCode.UNSAFE_CARDINALITY,
                "many-to-many join execution is unsupported and cannot be approved",
            )
        revision = draft.revision + 1
        digest = hashlib.sha256(proposal_id.encode()).hexdigest()[:12]
        decision_id = f"{draft.id}-{action.value}-v{revision}-{digest}"
        decision = DecisionRecord(
            id=decision_id,
            target_type=DecisionTargetType.JOIN_CONTRACT,
            target_id=proposal_id,
            action=action,
            status=(
                ApprovalStatus.APPROVED
                if action is DecisionAction.APPROVE
                else ApprovalStatus.REJECTED
            ),
            actor=actor,
            decided_at=decided_at,
            source_version=draft.revision,
            resulting_version=revision,
            rationale=rationale,
            evidence=selected.candidate.evidence,
            risks=selected.candidate.risks,
        )
        reviewed = (
            _approved_review(selected.candidate, decision_id)
            if action is DecisionAction.APPROVE
            else ReviewedJoinCandidate(
                candidate=selected.candidate,
                status=ApprovalStatus.REJECTED,
                decision_id=decision_id,
            )
        )
        revised = JoinReviewDraft(
            id=draft.id,
            revision=revision,
            joins=tuple(
                reviewed if item.candidate.proposal.id == proposal_id else item
                for item in draft.joins
            ),
        )
        self.store.commit_decision(revised, decision, expected_revision=expected_revision)
        return InspectJoinReview(self.store).execute(draft_id)


@dataclass(frozen=True, slots=True)
class PrepareJoinPublication:
    store: JoinReviewStorePort

    def execute(self, draft_id: str) -> JoinContractPublication:
        draft = _required_draft(self.store, draft_id)
        if not draft.is_ready_to_publish():
            raise RelationshipWorkflowError(
                RelationshipErrorCode.NOT_READY,
                "join review must have every candidate explicitly approved",
            )
        contracts = draft.approved_contracts()
        decision_ids = {contract.approval_decision_id for contract in contracts}
        decisions = tuple(
            decision
            for decision in self.store.list_decisions(draft_id)
            if decision.id in decision_ids
        )
        if len(decisions) != len(contracts):
            raise RelationshipWorkflowError(
                RelationshipErrorCode.STORE_FAILURE,
                "approved join is missing its immutable decision record",
            )
        return JoinContractPublication.create(
            draft_id=draft.id,
            draft_version=draft.revision,
            contracts=contracts,
            decisions=decisions,
        )


@dataclass(frozen=True, slots=True)
class PublishJoinContracts:
    store: JoinReviewStorePort
    writer: JoinContextWritePort

    def execute(self, draft_id: str, approval: JoinPublicationApproval) -> JoinPublicationResult:
        if not isinstance(approval, JoinPublicationApproval):
            raise RelationshipWorkflowError(
                RelationshipErrorCode.APPROVAL_REQUIRED,
                "explicit join publication approval is required",
            )
        publication = PrepareJoinPublication(self.store).execute(draft_id)
        expected_decisions = tuple(sorted(decision.id for decision in publication.decisions))
        if (
            approval.draft_id != publication.draft_id
            or approval.draft_version != publication.draft_version
            or approval.payload_fingerprint != publication.fingerprint
            or tuple(sorted(approval.decision_ids)) != expected_decisions
        ):
            raise RelationshipWorkflowError(
                RelationshipErrorCode.APPROVAL_MISMATCH,
                "join publication approval does not match the approved payload",
            )
        result = self.writer.publish(publication, approval)
        self.store.record_publication(result)
        return result


@dataclass(frozen=True, slots=True)
class LoadPublishedJoinContracts:
    reader: JoinContextReadPort

    def execute(self) -> PublishedJoinContext | None:
        return self.reader.load_current()


def _required_draft(store: JoinReviewStorePort, draft_id: str) -> JoinReviewDraft:
    draft = store.load(draft_id)
    if draft is None:
        raise RelationshipWorkflowError(
            RelationshipErrorCode.NOT_FOUND, "join review was not found"
        )
    return draft


def _approved_review(candidate: JoinCandidate, decision_id: str) -> ReviewedJoinCandidate:
    cardinality = candidate.cardinality.cardinality
    fanout = (
        FanoutPolicy.REQUIRE_DISTINCT_FOR_LEFT_ENTITY_METRICS
        if cardinality is Cardinality.ONE_TO_MANY
        else FanoutPolicy.NONE
    )
    contract = JoinContract(
        id=candidate.proposal.id,
        left_key=candidate.proposal.left_key,
        right_key=candidate.proposal.right_key,
        cardinality=cardinality,
        default_join_type=candidate.proposal.default_join_type,
        fanout_policy=fanout,
        status=ApprovalStatus.APPROVED,
        version=1,
        evidence=tuple(dict.fromkeys((*candidate.evidence, *candidate.cardinality.evidence))),
        risks=candidate.risks,
        approval_decision_id=decision_id,
    )
    return ReviewedJoinCandidate(
        candidate=candidate,
        status=ApprovalStatus.APPROVED,
        decision_id=decision_id,
        contract=contract,
    )


def _catalog_observation(
    *,
    statuses: tuple[EvidenceStatus, EvidenceStatus],
    found: bool,
    present_detail: str,
    absent_detail: str,
    missing_reason: str,
) -> ObservedJoinSignal:
    if found:
        return ObservedJoinSignal(score=1.0, detail=present_detail)
    if EvidenceStatus.PRESENT in statuses:
        return ObservedJoinSignal(score=0.0, detail=absent_detail)
    return ObservedJoinSignal.missing(missing_reason)
