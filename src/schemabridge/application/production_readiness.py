"""Prepare M30 without pretending that local files are operated evidence."""

from __future__ import annotations

from dataclasses import dataclass

from schemabridge.application.ports.production_evidence import (
    M30CampaignContractPort,
    M30CandidateIdentityPort,
)
from schemabridge.domain.production_readiness import (
    M30CandidateBranch,
    M30EvidenceClass,
    M30GateResult,
    M30GateStatus,
    M30PreflightState,
    M30ReadinessReport,
    M30ReleaseDecision,
)


@dataclass(frozen=True, slots=True)
class AssessM30Readiness:
    """Assess source preparation; external controls deliberately remain unverified."""

    contract_loader: M30CampaignContractPort
    candidate_identity: M30CandidateIdentityPort

    def execute(self) -> M30ReadinessReport:
        contract = self.contract_loader.load()
        candidate = self.candidate_identity.inspect(contract)
        tag_matches_package = candidate.annotated_release_tags == (f"v{candidate.package_version}",)
        repository_gates = (
            M30GateResult(
                code="candidate_annotated_release_tag",
                evidence_class=M30EvidenceClass.REPOSITORY_CONTRACT,
                status=(M30GateStatus.PASSED if tag_matches_package else M30GateStatus.FAILED),
                subject_revision=candidate.revision,
                detail=(
                    "Exactly one annotated stable tag matches the canonical package version."
                    if tag_matches_package
                    else f"The candidate requires exactly one annotated v{candidate.package_version} tag."
                ),
            ),
            M30GateResult(
                code="candidate_clean_tree",
                evidence_class=M30EvidenceClass.REPOSITORY_CONTRACT,
                status=M30GateStatus.FAILED if candidate.dirty else M30GateStatus.PASSED,
                subject_revision=candidate.revision,
                detail=(
                    "The repository has commit-visible or untracked changes."
                    if candidate.dirty
                    else "The repository worktree is clean."
                ),
            ),
            M30GateResult(
                code="candidate_main_branch",
                evidence_class=M30EvidenceClass.REPOSITORY_CONTRACT,
                status=(
                    M30GateStatus.PASSED
                    if candidate.branch is M30CandidateBranch.MAIN
                    else M30GateStatus.FAILED
                ),
                subject_revision=candidate.revision,
                detail=(
                    "The candidate is checked out from main."
                    if candidate.branch is M30CandidateBranch.MAIN
                    else "The candidate is not checked out from main."
                ),
            ),
            M30GateResult(
                code="candidate_source_contract",
                evidence_class=M30EvidenceClass.REPOSITORY_CONTRACT,
                status=(
                    M30GateStatus.PASSED
                    if candidate.contract_matches_head and candidate.all_required_sources_committed
                    else M30GateStatus.FAILED
                ),
                subject_revision=candidate.revision,
                detail=(
                    "Contract, lock, build, workflow, source-tree and migrations are digest-bound."
                    if candidate.contract_matches_head and candidate.all_required_sources_committed
                    else "The contract or another required source is not committed at candidate HEAD."
                ),
            ),
        )
        external_gates = tuple(
            M30GateResult(
                code=control.code,
                evidence_class=control.evidence_class,
                status=M30GateStatus.MISSING_EXTERNAL,
                subject_revision=candidate.revision,
                detail=(
                    f"{control.label} is not verified by this offline preflight; "
                    "repository fixtures and pull-request merge refs cannot satisfy it."
                ),
            )
            for control in contract.required_controls
        )
        gates = tuple(sorted((*repository_gates, *external_gates), key=lambda item: item.code))
        return M30ReadinessReport(
            schema_version=1,
            milestone="M30",
            contract_sha256=contract.fingerprint(),
            candidate=candidate,
            gates=gates,
            preflight_state=M30PreflightState.BLOCKED_PREREQUISITES,
            campaign_executable=False,
            release_decision=M30ReleaseDecision.NO_GO,
            synthetic_evidence_accepted_as_operated=False,
            network_calls=0,
            database_calls=0,
            datahub_writes=0,
            source_writes=0,
        )


__all__ = ["AssessM30Readiness"]
