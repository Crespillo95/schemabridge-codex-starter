"""Synthetic external M30 control-policy fixtures used only by tests."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from schemabridge.domain.production_campaign import (
    M30_GITHUB_ATTESTATION_PREDICATE,
    M30_GITHUB_CLI_EXECUTABLE_SHA256,
    M30_GITHUB_CLI_VERSION,
    M30_GITHUB_OIDC_ISSUER,
    M30_GITHUB_REPOSITORY,
    M30_GITHUB_SIGNER_WORKFLOW,
    M30AuthenticatedManifest,
    M30CampaignManifest,
    M30CampaignOwnerRole,
    M30ManifestAuthenticationReport,
    M30ManifestAuthenticationState,
)
from schemabridge.domain.production_campaign_receipts import (
    M30_CONTROL_PREREQUISITES,
    M30_CONTROL_TOPOLOGICAL_ORDER,
    M30_STAGE_PREREQUISITES,
    M30CampaignAuthorizationStage,
    M30CampaignStagePolicy,
    M30ControlAdjudicatorProfile,
    M30ControlCode,
    M30ControlPolicy,
    M30ControlPolicyRule,
    M30ReceiptAuthenticationProfile,
    M30SignedManifestCriteria,
    signed_manifest_criteria_fingerprint,
)
from schemabridge.domain.production_readiness import (
    M30_REPOSITORY_CONTROL_CODES,
    M30_REQUIRED_CONTROL_SPECS,
    M30EvidenceClass,
    M30GateResult,
    M30GateStatus,
    M30PreflightState,
    M30ReadinessReport,
    M30ReleaseDecision,
)

EXTERNAL_WORKFLOW_REPOSITORY = "independent-evaluator/schemabridge-controls"
EXTERNAL_WORKFLOW_REVISION = "f" * 40


def build_control_policy(
    manifest: M30CampaignManifest,
    *,
    not_before: datetime | None = None,
    expires_at: datetime | None = None,
) -> M30ControlPolicy:
    assignments = {M30ControlCode(item.code): item for item in manifest.controls}
    owners = {item.role: item for item in manifest.owners}
    signed_manifest_criteria = M30SignedManifestCriteria(
        main_branch_protected_required=True,
        annotated_tag_protected_required=True,
        attestation_environment_protected_required=True,
        minimum_independent_reviewers=1,
        self_review_allowed=False,
        administrator_bypass_allowed=False,
        environment_secret_count_max=0,
        exclusive_attestation_authority_required=True,
        exact_candidate_required=True,
        trusted_timestamp_required=True,
    )
    rules: list[M30ControlPolicyRule] = []
    for position, code in enumerate(M30_CONTROL_TOPOLOGICAL_ORDER, start=1):
        assignment = assignments[code]
        roles: tuple[M30CampaignOwnerRole, ...]
        if assignment.evidence_class is M30EvidenceClass.OWNER_APPROVAL:
            roles = (
                M30CampaignOwnerRole.PRODUCT,
                M30CampaignOwnerRole.SEMANTIC,
                M30CampaignOwnerRole.SECURITY,
                M30CampaignOwnerRole.OPERATIONS,
                M30CampaignOwnerRole.RELEASE,
            )
        else:
            roles = {
                M30EvidenceClass.HOSTED_CI: (M30CampaignOwnerRole.RELEASE,),
                M30EvidenceClass.OPERATED_TARGET: (M30CampaignOwnerRole.OPERATIONS,),
                M30EvidenceClass.INDEPENDENT_THIRD_PARTY: (
                    M30CampaignOwnerRole.INDEPENDENT_EVALUATOR,
                ),
            }[assignment.evidence_class]
        hosted = assignment.evidence_class is M30EvidenceClass.HOSTED_CI
        rules.append(
            M30ControlPolicyRule(
                control_code=code,
                evidence_class=assignment.evidence_class,
                evidence_artifact_kind=assignment.evidence_artifact_kind,
                receipt_kind=f"schemabridge.m30.receipt.{code.value}.v1",
                authentication_profile=(
                    M30ReceiptAuthenticationProfile.EXTERNAL_GITHUB_WORKFLOW
                    if hosted
                    else M30ReceiptAuthenticationProfile.EXTERNAL_ED25519_THRESHOLD
                ),
                trusted_policy_sha256=f"{1_000 + position:064x}",
                trusted_issuer=(
                    "https://token.actions.githubusercontent.com"
                    if hosted
                    else "urn:schemabridge:external-ed25519"
                ),
                trusted_subject=(
                    f"repo:independent-evaluator/schemabridge-controls:{code.value}"
                    if hosted
                    else f"authority:external:{code.value}"
                ),
                criteria_policy_sha256=(
                    signed_manifest_criteria_fingerprint(signed_manifest_criteria)
                    if code is M30ControlCode.SIGNED_CAMPAIGN_MANIFEST
                    else f"{2_000 + position:064x}"
                ),
                required_approver_roles=roles,
                required_approver_key_fingerprints=tuple(
                    owners[role].approval_key_fingerprint for role in roles
                ),
                approval_quorum=len(roles),
                trusted_workflow_repository=(EXTERNAL_WORKFLOW_REPOSITORY if hosted else None),
                trusted_workflow_path=(f".github/workflows/{code.value}.yml" if hosted else None),
                trusted_workflow_revision=(EXTERNAL_WORKFLOW_REVISION if hosted else None),
                prerequisites=M30_CONTROL_PREREQUISITES[code],
                adjudicator_profile=(
                    M30ControlAdjudicatorProfile.SIGNED_CAMPAIGN_MANIFEST_V1
                    if code is M30ControlCode.SIGNED_CAMPAIGN_MANIFEST
                    else M30ControlAdjudicatorProfile.ADMITTED_UNADJUDICATED
                ),
                trusted_timestamp_required=True,
            )
        )
    return M30ControlPolicy(
        schema_version=1,
        kind="schemabridge.m30.control-policy",
        policy_id=manifest.control_policy_id,
        campaign_id=manifest.campaign_id,
        source_repository=manifest.source_repository,
        candidate_revision=manifest.candidate.revision,
        candidate_source_tree_sha256=manifest.candidate.source_tree_sha256,
        contract_sha256=manifest.contract_sha256,
        issued_at=manifest.issued_at,
        not_before=not_before or manifest.not_before,
        expires_at=expires_at or manifest.expires_at,
        signed_manifest_criteria=signed_manifest_criteria,
        rules=tuple(rules),
        authorization_stages=tuple(_stage_policy(stage) for stage in M30CampaignAuthorizationStage),
    )


def write_control_policy(path: Path, policy: M30ControlPolicy) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(policy.canonical_bytes())
    return path


def build_manifest_authentication_report(
    manifest: M30CampaignManifest,
) -> M30ManifestAuthenticationReport:
    candidate = manifest.candidate
    gates = tuple(
        sorted(
            (
                *(
                    M30GateResult(
                        code=code,
                        evidence_class=M30EvidenceClass.REPOSITORY_CONTRACT,
                        status=M30GateStatus.PASSED,
                        subject_revision=candidate.revision,
                        detail="Synthetic exact repository prerequisite passed for unit evidence",
                    )
                    for code in M30_REPOSITORY_CONTROL_CODES
                ),
                *(
                    M30GateResult(
                        code=code,
                        evidence_class=evidence_class,
                        status=M30GateStatus.MISSING_EXTERNAL,
                        subject_revision=candidate.revision,
                        detail="External control remains absent from repository-only preflight",
                    )
                    for code, (evidence_class, _label) in M30_REQUIRED_CONTROL_SPECS.items()
                ),
            ),
            key=lambda item: item.code,
        )
    )
    preflight = M30ReadinessReport(
        schema_version=1,
        milestone="M30",
        contract_sha256=manifest.contract_sha256,
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
    authentication = M30AuthenticatedManifest(
        manifest_sha256=manifest.fingerprint(),
        attestation_bundle_sha256="a" * 64,
        verification_summary_sha256="b" * 64,
        certificate_evidence_sha256="c" * 64,
        trusted_timestamps_sha256="d" * 64,
        trusted_timestamp_count=1,
        source_repository=M30_GITHUB_REPOSITORY,
        source_revision=candidate.revision,
        source_ref=f"refs/tags/v{candidate.package_version}",
        signer_workflow=M30_GITHUB_SIGNER_WORKFLOW,
        signer_digest=candidate.revision,
        oidc_issuer=M30_GITHUB_OIDC_ISSUER,
        predicate_type=M30_GITHUB_ATTESTATION_PREDICATE,
        github_cli_version=M30_GITHUB_CLI_VERSION,
        github_cli_platform="macos-arm64",
        github_cli_executable_sha256=M30_GITHUB_CLI_EXECUTABLE_SHA256["macos-arm64"],
        github_hosted_runner_required=True,
        trusted_timestamp_verified=True,
        verified_at=manifest.not_before,
    )
    return M30ManifestAuthenticationReport(
        schema_version=2,
        milestone="M30",
        phase="1a",
        preflight=preflight,
        state=M30ManifestAuthenticationState.AUTHENTICATED,
        blocking_reasons=(),
        campaign_id=manifest.campaign_id,
        manifest_sha256=manifest.fingerprint(),
        control_policy_id=manifest.control_policy_id,
        control_policy_sha256=manifest.control_policy_sha256,
        authentication=authentication,
        completed_at=manifest.not_before,
        campaign_executable=False,
        release_decision=M30ReleaseDecision.NO_GO,
        workflow_attested_manifest_authenticated=True,
        external_controls_passed=0,
        external_controls_remaining=24,
        synthetic_evidence_accepted_as_operated=False,
        database_calls=0,
        datahub_writes=0,
        source_writes=0,
    )


def _stage_policy(stage: M30CampaignAuthorizationStage) -> M30CampaignStagePolicy:
    access = {
        M30CampaignAuthorizationStage.TARGET_PREFLIGHT: (False, False, True, False),
        M30CampaignAuthorizationStage.SOURCE_READONLY_PROBE: (False, True, True, False),
        M30CampaignAuthorizationStage.CAMPAIGN_RUN: (True, True, True, True),
        M30CampaignAuthorizationStage.FINAL_REVIEW: (False, False, False, False),
        M30CampaignAuthorizationStage.M31_ELIGIBLE: (False, False, False, False),
    }[stage]
    return M30CampaignStagePolicy(
        stage=stage,
        required_controls=M30_STAGE_PREREQUISITES[stage],
        provider_access=access[0],
        source_read_access=access[1],
        target_access=access[2],
        corpus_access=access[3],
        source_write_access=False,
        raw_llm_sql_access=False,
        blanket_datahub_write_access=False,
    )


__all__ = [
    "EXTERNAL_WORKFLOW_REPOSITORY",
    "EXTERNAL_WORKFLOW_REVISION",
    "build_control_policy",
    "build_manifest_authentication_report",
    "write_control_policy",
]
