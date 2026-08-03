"""M30 Phase-1b policy, DAG and signed-manifest adjudication regressions."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.m30_campaign_support import VERIFICATION_TIME, build_manifest
from tests.m30_control_policy_support import (
    EXTERNAL_WORKFLOW_REPOSITORY,
    EXTERNAL_WORKFLOW_REVISION,
    build_control_policy,
    build_manifest_authentication_report,
)
from tests.m30_readiness_support import build_candidate_repository

from schemabridge.domain.production_campaign import M30CampaignManifest
from schemabridge.domain.production_campaign_receipts import (
    M30_CONTROL_PREREQUISITES,
    M30_CONTROL_TOPOLOGICAL_ORDER,
    M30AuthenticatedApprover,
    M30AuthenticatedReceiptProducer,
    M30AuthenticatedSignedManifestReceipt,
    M30CampaignAuthorizationStage,
    M30ControlAdjudication,
    M30ControlAdjudicationStatus,
    M30ControlAdjudicatorProfile,
    M30ControlCode,
    M30ControlPolicy,
    M30CriterionResult,
    M30EvidenceSubject,
    M30PrerequisiteAdjudicationRef,
    M30SignedManifestControlReceipt,
    M30SignedManifestGovernanceFacts,
    adjudicate_signed_campaign_manifest,
    control_assignment_fingerprint,
    control_policy_matches_manifest,
)


def test_control_policy_closes_exact_dag_quorum_and_access_stages(tmp_path: Path) -> None:
    manifest, policy = _bound_manifest_and_policy(tmp_path)

    assert tuple(rule.control_code for rule in policy.rules) == M30_CONTROL_TOPOLOGICAL_ORDER
    assert set(M30_CONTROL_PREREQUISITES) == set(M30ControlCode)
    positions = {code: position for position, code in enumerate(M30_CONTROL_TOPOLOGICAL_ORDER)}
    assert all(
        positions[prerequisite] < positions[code]
        for code, prerequisites in M30_CONTROL_PREREQUISITES.items()
        for prerequisite in prerequisites
    )
    assert control_policy_matches_manifest(policy, manifest)
    assert policy.fingerprint() == manifest.control_policy_sha256

    owner_rules = [
        rule for rule in policy.rules if rule.control_code.value.startswith("candidate_owner")
    ] + [rule for rule in policy.rules if rule.control_code is M30ControlCode.RELEASE_RISK_REGISTER]
    assert len(owner_rules) == 2
    assert all(rule.approval_quorum == 5 for rule in owner_rules)
    assert all(len(rule.required_approver_roles) == 5 for rule in owner_rules)
    assert all(len(rule.required_approver_key_fingerprints) == 5 for rule in owner_rules)

    target = next(
        item
        for item in policy.authorization_stages
        if item.stage is M30CampaignAuthorizationStage.TARGET_PREFLIGHT
    )
    campaign = next(
        item
        for item in policy.authorization_stages
        if item.stage is M30CampaignAuthorizationStage.CAMPAIGN_RUN
    )
    assert target.target_access and not target.provider_access and not target.corpus_access
    assert campaign.provider_access and campaign.source_read_access and campaign.corpus_access
    assert all(
        not stage.source_write_access
        and not stage.raw_llm_sql_access
        and not stage.blanket_datahub_write_access
        for stage in policy.authorization_stages
    )


def test_manifest_v2_requires_external_policy_binding(tmp_path: Path) -> None:
    manifest, policy = _bound_manifest_and_policy(tmp_path)
    payload = manifest.model_dump(mode="python")

    payload["schema_version"] = 1
    with pytest.raises(ValidationError):
        type(manifest).model_validate(payload)

    payload = manifest.model_dump(mode="python")
    payload.pop("control_policy_sha256")
    with pytest.raises(ValidationError):
        type(manifest).model_validate(payload)

    changed = manifest.model_copy(update={"control_policy_sha256": "b" * 64})
    assert not control_policy_matches_manifest(policy, changed)


def test_hosted_workflow_identity_is_unique_case_insensitively(tmp_path: Path) -> None:
    _manifest, policy = _bound_manifest_and_policy(tmp_path)
    payload = policy.model_dump(mode="python")
    payload["rules"][1]["trusted_workflow_repository"] = payload["rules"][0][
        "trusted_workflow_repository"
    ].upper()
    payload["rules"][1]["trusted_workflow_path"] = payload["rules"][0]["trusted_workflow_path"]

    with pytest.raises(ValidationError):
        M30ControlPolicy.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trusted_workflow_repository", "Crespillo95/schemabridge-codex-starter"),
        ("trusted_workflow_repository", "crespillo95/schemabridge-codex-starter"),
        ("approval_quorum", 2),
        ("prerequisites", (M30ControlCode.HOSTED_QUALITY_GATE,)),
        ("receipt_kind", "schemabridge.m30.receipt.generic.v1"),
        ("adjudicator_profile", M30ControlAdjudicatorProfile.ADMITTED_UNADJUDICATED),
    ],
)
def test_signed_manifest_rule_cannot_weaken_trust_or_adjudication(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    _manifest, policy = _bound_manifest_and_policy(tmp_path)
    payload = policy.model_dump(mode="python")
    payload["rules"][0][field] = value

    with pytest.raises(ValidationError):
        M30ControlPolicy.model_validate(payload)


def test_receipt_rejects_claimed_outcome_and_nested_extra(tmp_path: Path) -> None:
    manifest, policy = _bound_manifest_and_policy(tmp_path)
    receipt = _signed_manifest_receipt(manifest, policy)
    payload = receipt.model_dump(mode="python")
    payload["claimed_outcome"] = "passed"
    with pytest.raises(ValidationError):
        M30SignedManifestControlReceipt.model_validate(payload)

    payload = receipt.model_dump(mode="python")
    payload["facts"]["status"] = "passed"
    with pytest.raises(ValidationError):
        M30SignedManifestControlReceipt.model_validate(payload)


def test_receipt_retry_is_rejected_until_a_durable_attempt_ledger_exists(tmp_path: Path) -> None:
    manifest, policy = _bound_manifest_and_policy(tmp_path)
    payload = _signed_manifest_receipt(manifest, policy).model_dump(mode="python")
    payload["attempt"] = 2
    payload["previous_receipt_sha256"] = "0" * 64

    with pytest.raises(ValidationError):
        M30SignedManifestControlReceipt.model_validate(payload)


def test_typed_authenticated_governance_receipt_is_deterministically_adjudicated(
    tmp_path: Path,
) -> None:
    manifest, policy = _bound_manifest_and_policy(tmp_path)
    authenticated = _authenticated_receipt(manifest, policy)

    result = adjudicate_signed_campaign_manifest(
        authenticated=authenticated,
        manifest=manifest,
        manifest_authentication=build_manifest_authentication_report(manifest),
        policy=policy,
    )

    assert result.status is M30ControlAdjudicationStatus.PASSED
    assert result.campaign_id == manifest.campaign_id
    assert result.manifest_sha256 == manifest.fingerprint()
    assert result.control_policy_sha256 == policy.fingerprint()
    assert result.prerequisite_adjudications == ()
    assert all(item.passed for item in result.criteria)
    assert result.campaign_executable is False
    assert result.release_decision.value == "no_go"


def test_authentic_receipt_with_failed_governance_fact_is_failed_not_invalid(
    tmp_path: Path,
) -> None:
    manifest, policy = _bound_manifest_and_policy(tmp_path)
    receipt = _signed_manifest_receipt(manifest, policy, main_protected=False)
    authenticated = _authenticated_receipt(manifest, policy, receipt=receipt)

    result = adjudicate_signed_campaign_manifest(
        authenticated=authenticated,
        manifest=manifest,
        manifest_authentication=build_manifest_authentication_report(manifest),
        policy=policy,
    )

    assert result.status is M30ControlAdjudicationStatus.FAILED
    failed = {item.criterion for item in result.criteria if not item.passed}
    assert failed == {"main_branch_protected"}


def test_valid_signature_cannot_rescue_a_cross_manifest_receipt(tmp_path: Path) -> None:
    manifest, policy = _bound_manifest_and_policy(tmp_path)
    receipt_payload = _signed_manifest_receipt(manifest, policy).model_dump(mode="python")
    receipt_payload["manifest_sha256"] = "8" * 64
    changed_receipt = M30SignedManifestControlReceipt.model_validate(receipt_payload)
    authenticated = _authenticated_receipt(manifest, policy, receipt=changed_receipt)

    result = adjudicate_signed_campaign_manifest(
        authenticated=authenticated,
        manifest=manifest,
        manifest_authentication=build_manifest_authentication_report(manifest),
        policy=policy,
    )

    assert result.status is M30ControlAdjudicationStatus.FAILED
    assert "manifest_binding" in {item.criterion for item in result.criteria if not item.passed}


def test_authenticated_producer_issuer_and_subject_are_policy_bound(tmp_path: Path) -> None:
    manifest, policy = _bound_manifest_and_policy(tmp_path)
    authenticated = _authenticated_receipt(manifest, policy)
    payload = authenticated.model_dump(mode="python")
    payload["producer"]["issuer"] = "https://issuer.invalid"
    changed = M30AuthenticatedSignedManifestReceipt.model_validate(payload)

    result = adjudicate_signed_campaign_manifest(
        authenticated=changed,
        manifest=manifest,
        manifest_authentication=build_manifest_authentication_report(manifest),
        policy=policy,
    )

    assert result.status is M30ControlAdjudicationStatus.FAILED
    assert "producer_policy" in {item.criterion for item in result.criteria if not item.passed}


def test_receipt_before_bound_policy_window_cannot_pass(tmp_path: Path) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    provisional = build_manifest(repository)
    base_policy = build_control_policy(provisional)
    policy_payload = base_policy.model_dump(mode="python")
    policy_payload["not_before"] = VERIFICATION_TIME + timedelta(hours=1)
    future_policy = M30ControlPolicy.model_validate(policy_payload)
    manifest = build_manifest(
        repository,
        control_policy_id=future_policy.policy_id,
        control_policy_sha256=future_policy.fingerprint(),
    )
    authenticated = _authenticated_receipt(manifest, future_policy)

    result = adjudicate_signed_campaign_manifest(
        authenticated=authenticated,
        manifest=manifest,
        manifest_authentication=build_manifest_authentication_report(manifest),
        policy=future_policy,
    )

    assert result.status is M30ControlAdjudicationStatus.FAILED
    assert "receipt_window" in {item.criterion for item in result.criteria if not item.passed}


def test_receipt_cannot_predate_the_bound_phase1a_completion(tmp_path: Path) -> None:
    manifest, policy = _bound_manifest_and_policy(tmp_path)
    authentication = build_manifest_authentication_report(manifest).model_copy(
        update={"completed_at": VERIFICATION_TIME + timedelta(minutes=10)}
    )

    result = adjudicate_signed_campaign_manifest(
        authenticated=_authenticated_receipt(manifest, policy),
        manifest=manifest,
        manifest_authentication=authentication,
        policy=policy,
    )

    assert result.status is M30ControlAdjudicationStatus.FAILED
    failed = {item.criterion for item in result.criteria if not item.passed}
    assert {"approver_quorum", "producer_policy", "receipt_window"} <= failed


def test_approver_signature_must_bind_exact_receipt_bytes(tmp_path: Path) -> None:
    manifest, policy = _bound_manifest_and_policy(tmp_path)
    authenticated = _authenticated_receipt(manifest, policy)
    payload = authenticated.model_dump(mode="python")
    payload["approvers"][0]["signed_payload_sha256"] = "0" * 64

    with pytest.raises(ValidationError):
        M30AuthenticatedSignedManifestReceipt.model_validate(payload)


def test_producer_cannot_alias_candidate_repository_by_case(tmp_path: Path) -> None:
    manifest, policy = _bound_manifest_and_policy(tmp_path)
    authenticated = _authenticated_receipt(manifest, policy)
    payload = authenticated.model_dump(mode="python")
    payload["producer"]["workflow_repository"] = "crespillo95/schemabridge-codex-starter"

    with pytest.raises(ValidationError):
        M30AuthenticatedSignedManifestReceipt.model_validate(payload)


def test_governance_snapshot_subjects_must_match_typed_facts(tmp_path: Path) -> None:
    manifest, policy = _bound_manifest_and_policy(tmp_path)
    receipt = _signed_manifest_receipt(manifest, policy)
    payload = receipt.model_dump(mode="python")
    payload["evidence_subjects"][0]["sha256"] = "0" * 64

    with pytest.raises(ValidationError):
        M30SignedManifestControlReceipt.model_validate(payload)


def test_other_23_controls_remain_explicitly_unadjudicated(tmp_path: Path) -> None:
    manifest, policy = _bound_manifest_and_policy(tmp_path)

    assert policy.rules[0].adjudicator_profile is (
        M30ControlAdjudicatorProfile.SIGNED_CAMPAIGN_MANIFEST_V1
    )
    assert all(
        rule.adjudicator_profile is M30ControlAdjudicatorProfile.ADMITTED_UNADJUDICATED
        for rule in policy.rules[1:]
    )

    with pytest.raises(ValidationError):
        M30ControlAdjudication(
            control_code=M30ControlCode.CANDIDATE_OWNER_SIGNATURES,
            campaign_id=manifest.campaign_id,
            manifest_sha256=manifest.fingerprint(),
            control_policy_sha256=policy.fingerprint(),
            receipt_sha256="1" * 64,
            status=M30ControlAdjudicationStatus.PASSED,
            criteria=(M30CriterionResult(criterion="caller_claim", passed=True),),
            prerequisite_adjudications=(),
            campaign_executable=False,
            release_decision="no_go",
        )


def test_root_adjudication_rejects_invented_or_cross_campaign_prerequisite(tmp_path: Path) -> None:
    manifest, policy = _bound_manifest_and_policy(tmp_path)
    authenticated = _authenticated_receipt(manifest, policy)
    result = adjudicate_signed_campaign_manifest(
        authenticated=authenticated,
        manifest=manifest,
        manifest_authentication=build_manifest_authentication_report(manifest),
        policy=policy,
    )
    payload = result.model_dump(mode="python")
    payload["prerequisite_adjudications"] = (
        M30PrerequisiteAdjudicationRef(
            control_code=M30ControlCode.HOSTED_QUALITY_GATE,
            campaign_id="campaign:m30:different",
            manifest_sha256=manifest.fingerprint(),
            control_policy_sha256=policy.fingerprint(),
            adjudication_sha256="9" * 64,
            status=M30ControlAdjudicationStatus.PASSED,
        ),
    )

    with pytest.raises(ValidationError):
        M30ControlAdjudication.model_validate(payload)


def _bound_manifest_and_policy(
    tmp_path: Path,
) -> tuple[M30CampaignManifest, M30ControlPolicy]:
    repository = build_candidate_repository(tmp_path / "candidate")
    provisional = build_manifest(repository)
    provisional_policy = build_control_policy(provisional)
    manifest = build_manifest(
        repository,
        control_policy_id=provisional_policy.policy_id,
        control_policy_sha256=provisional_policy.fingerprint(),
    )
    policy = build_control_policy(manifest)
    assert policy.fingerprint() == provisional_policy.fingerprint()
    return manifest, policy


def _signed_manifest_receipt(
    manifest: M30CampaignManifest,
    policy: M30ControlPolicy,
    *,
    main_protected: bool = True,
) -> M30SignedManifestControlReceipt:
    assignment = next(item for item in manifest.controls if item.code == "signed_campaign_manifest")
    return M30SignedManifestControlReceipt(
        schema_version=1,
        kind="schemabridge.m30.receipt.signed_campaign_manifest.v1",
        receipt_id="receipt:m30:signed-manifest:001",
        attempt=1,
        previous_receipt_sha256=None,
        campaign_id=manifest.campaign_id,
        manifest_sha256=manifest.fingerprint(),
        manifest_authentication_sha256=build_manifest_authentication_report(manifest).fingerprint(),
        control_policy_sha256=policy.fingerprint(),
        contract_sha256=manifest.contract_sha256,
        candidate_revision=manifest.candidate.revision,
        candidate_source_tree_sha256=manifest.candidate.source_tree_sha256,
        control_code=M30ControlCode.SIGNED_CAMPAIGN_MANIFEST,
        evidence_class="hosted_ci",
        control_assignment_sha256=control_assignment_fingerprint(assignment),
        observed_started_at=VERIFICATION_TIME,
        observed_completed_at=VERIFICATION_TIME + timedelta(minutes=1),
        issued_at=VERIFICATION_TIME + timedelta(minutes=2),
        evidence_subjects=tuple(
            sorted(
                (
                    M30EvidenceSubject(
                        kind="branch-ruleset",
                        media_type="application/json",
                        byte_size=100,
                        sha256="1" * 64,
                    ),
                    M30EvidenceSubject(
                        kind="environment-policy",
                        media_type="application/json",
                        byte_size=100,
                        sha256="2" * 64,
                    ),
                    M30EvidenceSubject(
                        kind="tag-ruleset",
                        media_type="application/json",
                        byte_size=100,
                        sha256="3" * 64,
                    ),
                ),
                key=lambda item: (item.kind, item.sha256),
            )
        ),
        facts=M30SignedManifestGovernanceFacts(
            source_repository=manifest.source_repository,
            source_revision=manifest.candidate.revision,
            source_ref=f"refs/tags/v{manifest.candidate.package_version}",
            release_tag=f"v{manifest.candidate.package_version}",
            attestation_environment="m30-manifest-attestation",
            manifest_workflow_sha256=next(
                item.sha256
                for item in manifest.candidate.source_digests
                if item.name == "m30_attestation_workflow"
            ),
            main_branch_protected=main_protected,
            annotated_tag_protected=True,
            attestation_environment_protected=True,
            independent_reviewer_authorities=("authority:independent-reviewer",),
            workflow_actor_authority="authority:workflow-actor",
            self_review_enabled=False,
            administrator_bypass_enabled=False,
            environment_secret_count=0,
            exclusive_attestation_authority=True,
            exact_candidate_observed=True,
            trusted_timestamp_observed=True,
            branch_ruleset_snapshot_sha256="1" * 64,
            tag_ruleset_snapshot_sha256="3" * 64,
            environment_snapshot_sha256="2" * 64,
        ),
    )


def _authenticated_receipt(
    manifest: M30CampaignManifest,
    policy: M30ControlPolicy,
    *,
    receipt: M30SignedManifestControlReceipt | None = None,
) -> M30AuthenticatedSignedManifestReceipt:
    resolved = receipt or _signed_manifest_receipt(manifest, policy)
    rule = policy.rules[0]
    owner = next(item for item in manifest.owners if item.role.value == "release_owner")
    return M30AuthenticatedSignedManifestReceipt(
        receipt=resolved,
        producer=M30AuthenticatedReceiptProducer(
            receipt_sha256=resolved.fingerprint(),
            trusted_policy_sha256=rule.trusted_policy_sha256,
            workflow_repository=EXTERNAL_WORKFLOW_REPOSITORY,
            workflow_path=".github/workflows/signed_campaign_manifest.yml",
            workflow_revision=EXTERNAL_WORKFLOW_REVISION,
            issuer="https://token.actions.githubusercontent.com",
            subject="repo:independent-evaluator/schemabridge-controls:signed_campaign_manifest",
            attestation_bundle_sha256="4" * 64,
            verification_summary_sha256="5" * 64,
            trusted_timestamps_sha256="6" * 64,
            trusted_timestamp_count=1,
            signature_verified=True,
            trusted_timestamp_verified=True,
            verified_at=VERIFICATION_TIME + timedelta(minutes=3),
        ),
        approvers=(
            M30AuthenticatedApprover(
                role=owner.role,
                authority_id=owner.authority_id,
                key_fingerprint=owner.approval_key_fingerprint,
                signed_payload_sha256=resolved.fingerprint(),
                signature_sha256="7" * 64,
                signature_verified=True,
                trusted_timestamp_verified=True,
                verified_at=VERIFICATION_TIME + timedelta(minutes=3),
            ),
        ),
    )
