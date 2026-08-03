"""Closed Phase-1b contracts for external M30 control evidence.

This module deliberately prepares policy binding and one deterministic control
adjudicator.  It has no provider, source, target, corpus, database, DataHub or
network capability.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from pydantic import Field, StrictBool, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.production_campaign import (
    M30_GITHUB_REPOSITORY,
    M30CampaignControlAssignment,
    M30CampaignManifest,
    M30CampaignOwnerRole,
    M30ManifestAuthenticationReport,
    M30ManifestAuthenticationState,
)
from schemabridge.domain.production_readiness import (
    M30_REQUIRED_CONTROL_SPECS,
    M30EvidenceClass,
    M30ReleaseDecision,
)

_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INERT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,159}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]{1,80}/[A-Za-z0-9_.-]{1,80}$")
_WORKFLOW_PATH = re.compile(r"^\.github/workflows/[A-Za-z0-9_.-]{1,100}\.ya?ml$")
_TRUST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+%-]{2,239}$")

M30_CONTROL_POLICY_KIND = "schemabridge.m30.control-policy"
M30_SIGNED_MANIFEST_RECEIPT_KIND = "schemabridge.m30.receipt.signed_campaign_manifest.v1"
M30_MAX_CONTROL_POLICY_VALIDITY = timedelta(days=30)
M30_MAX_CONTROL_RECEIPT_VALIDITY = timedelta(days=30)


class M30ControlCode(StrEnum):
    SIGNED_CAMPAIGN_MANIFEST = "signed_campaign_manifest"
    HOSTED_QUALITY_GATE = "hosted_quality_gate"
    HOSTED_POSTGRES_INTEGRATION = "hosted_postgres_integration"
    HOSTED_SUPPLY_CHAIN_SUBJECTS = "hosted_supply_chain_subjects"
    PUBLISHED_ARTIFACT_ATTESTATION = "published_artifact_attestation"
    CANDIDATE_CLEAN_ROOM_INSTALL = "candidate_clean_room_install"
    OPERATED_TARGET_CLUSTER = "operated_target_cluster"
    OPERATED_OIDC_TENANT_ISOLATION = "operated_oidc_tenant_isolation"
    OPERATED_SECRET_ROTATION = "operated_secret_rotation"
    OPERATED_DATAHUB_IAM = "operated_datahub_iam"
    OPERATED_SIEM_PAGING = "operated_siem_paging"
    INDEPENDENT_SOURCE_READONLY = "independent_source_readonly"
    ADVERSARIAL_BROWSER_API = "adversarial_browser_api"
    OPERATED_M35_LIFECYCLE = "operated_m35_lifecycle"
    BLIND_BILINGUAL_CORPUS = "blind_bilingual_corpus"
    EXECUTION_AST_EQUIVALENCE = "execution_ast_equivalence"
    BROWSER_ACCESSIBILITY_MATRIX = "browser_accessibility_matrix"
    SCALE_COST_SOAK_CAMPAIGN = "scale_cost_soak_campaign"
    OPERATED_RECOVERY_DRILL = "operated_recovery_drill"
    INDEPENDENT_PENTEST = "independent_pentest"
    OPERATED_INCIDENT_DRILL = "operated_incident_drill"
    IMMUTABLE_RAW_RESULT_BUNDLE = "immutable_raw_result_bundle"
    RELEASE_RISK_REGISTER = "release_risk_register"
    CANDIDATE_OWNER_SIGNATURES = "candidate_owner_signatures"


M30_CONTROL_TOPOLOGICAL_ORDER = tuple(M30ControlCode)

M30_CONTROL_PREREQUISITES: Mapping[M30ControlCode, tuple[M30ControlCode, ...]] = MappingProxyType(
    {
        M30ControlCode.SIGNED_CAMPAIGN_MANIFEST: (),
        M30ControlCode.HOSTED_QUALITY_GATE: (M30ControlCode.SIGNED_CAMPAIGN_MANIFEST,),
        M30ControlCode.HOSTED_POSTGRES_INTEGRATION: (M30ControlCode.SIGNED_CAMPAIGN_MANIFEST,),
        M30ControlCode.HOSTED_SUPPLY_CHAIN_SUBJECTS: (
            M30ControlCode.SIGNED_CAMPAIGN_MANIFEST,
            M30ControlCode.HOSTED_QUALITY_GATE,
            M30ControlCode.HOSTED_POSTGRES_INTEGRATION,
        ),
        M30ControlCode.PUBLISHED_ARTIFACT_ATTESTATION: (
            M30ControlCode.HOSTED_SUPPLY_CHAIN_SUBJECTS,
        ),
        M30ControlCode.CANDIDATE_CLEAN_ROOM_INSTALL: (
            M30ControlCode.PUBLISHED_ARTIFACT_ATTESTATION,
        ),
        M30ControlCode.OPERATED_TARGET_CLUSTER: (
            M30ControlCode.SIGNED_CAMPAIGN_MANIFEST,
            M30ControlCode.HOSTED_QUALITY_GATE,
            M30ControlCode.HOSTED_POSTGRES_INTEGRATION,
            M30ControlCode.HOSTED_SUPPLY_CHAIN_SUBJECTS,
            M30ControlCode.PUBLISHED_ARTIFACT_ATTESTATION,
            M30ControlCode.CANDIDATE_CLEAN_ROOM_INSTALL,
        ),
        M30ControlCode.OPERATED_OIDC_TENANT_ISOLATION: (M30ControlCode.OPERATED_TARGET_CLUSTER,),
        M30ControlCode.OPERATED_SECRET_ROTATION: (
            M30ControlCode.OPERATED_TARGET_CLUSTER,
            M30ControlCode.OPERATED_OIDC_TENANT_ISOLATION,
        ),
        M30ControlCode.OPERATED_DATAHUB_IAM: (
            M30ControlCode.OPERATED_TARGET_CLUSTER,
            M30ControlCode.OPERATED_OIDC_TENANT_ISOLATION,
            M30ControlCode.OPERATED_SECRET_ROTATION,
        ),
        M30ControlCode.OPERATED_SIEM_PAGING: (
            M30ControlCode.OPERATED_TARGET_CLUSTER,
            M30ControlCode.OPERATED_OIDC_TENANT_ISOLATION,
            M30ControlCode.OPERATED_SECRET_ROTATION,
            M30ControlCode.OPERATED_DATAHUB_IAM,
        ),
        M30ControlCode.INDEPENDENT_SOURCE_READONLY: (
            M30ControlCode.OPERATED_TARGET_CLUSTER,
            M30ControlCode.OPERATED_OIDC_TENANT_ISOLATION,
            M30ControlCode.OPERATED_SECRET_ROTATION,
            M30ControlCode.OPERATED_DATAHUB_IAM,
            M30ControlCode.OPERATED_SIEM_PAGING,
        ),
        M30ControlCode.ADVERSARIAL_BROWSER_API: (
            M30ControlCode.OPERATED_TARGET_CLUSTER,
            M30ControlCode.OPERATED_OIDC_TENANT_ISOLATION,
            M30ControlCode.OPERATED_SECRET_ROTATION,
            M30ControlCode.OPERATED_DATAHUB_IAM,
            M30ControlCode.OPERATED_SIEM_PAGING,
        ),
        M30ControlCode.OPERATED_M35_LIFECYCLE: (
            M30ControlCode.INDEPENDENT_SOURCE_READONLY,
            M30ControlCode.ADVERSARIAL_BROWSER_API,
        ),
        M30ControlCode.BLIND_BILINGUAL_CORPUS: (
            M30ControlCode.OPERATED_M35_LIFECYCLE,
            M30ControlCode.INDEPENDENT_SOURCE_READONLY,
            M30ControlCode.ADVERSARIAL_BROWSER_API,
        ),
        M30ControlCode.EXECUTION_AST_EQUIVALENCE: (
            M30ControlCode.BLIND_BILINGUAL_CORPUS,
            M30ControlCode.INDEPENDENT_SOURCE_READONLY,
        ),
        M30ControlCode.BROWSER_ACCESSIBILITY_MATRIX: (
            M30ControlCode.BLIND_BILINGUAL_CORPUS,
            M30ControlCode.EXECUTION_AST_EQUIVALENCE,
            M30ControlCode.ADVERSARIAL_BROWSER_API,
        ),
        M30ControlCode.SCALE_COST_SOAK_CAMPAIGN: (
            M30ControlCode.BLIND_BILINGUAL_CORPUS,
            M30ControlCode.EXECUTION_AST_EQUIVALENCE,
            M30ControlCode.OPERATED_M35_LIFECYCLE,
            M30ControlCode.OPERATED_SIEM_PAGING,
        ),
        M30ControlCode.OPERATED_RECOVERY_DRILL: (
            M30ControlCode.SCALE_COST_SOAK_CAMPAIGN,
            M30ControlCode.OPERATED_M35_LIFECYCLE,
            M30ControlCode.OPERATED_SECRET_ROTATION,
            M30ControlCode.OPERATED_SIEM_PAGING,
        ),
        M30ControlCode.INDEPENDENT_PENTEST: (
            M30ControlCode.BROWSER_ACCESSIBILITY_MATRIX,
            M30ControlCode.SCALE_COST_SOAK_CAMPAIGN,
            M30ControlCode.OPERATED_M35_LIFECYCLE,
        ),
        M30ControlCode.OPERATED_INCIDENT_DRILL: (
            M30ControlCode.INDEPENDENT_PENTEST,
            M30ControlCode.OPERATED_RECOVERY_DRILL,
            M30ControlCode.OPERATED_SIEM_PAGING,
            M30ControlCode.ADVERSARIAL_BROWSER_API,
        ),
        M30ControlCode.IMMUTABLE_RAW_RESULT_BUNDLE: tuple(
            code
            for code in M30_CONTROL_TOPOLOGICAL_ORDER
            if code
            not in {
                M30ControlCode.IMMUTABLE_RAW_RESULT_BUNDLE,
                M30ControlCode.RELEASE_RISK_REGISTER,
                M30ControlCode.CANDIDATE_OWNER_SIGNATURES,
            }
        ),
        M30ControlCode.RELEASE_RISK_REGISTER: (M30ControlCode.IMMUTABLE_RAW_RESULT_BUNDLE,),
        M30ControlCode.CANDIDATE_OWNER_SIGNATURES: (
            M30ControlCode.IMMUTABLE_RAW_RESULT_BUNDLE,
            M30ControlCode.RELEASE_RISK_REGISTER,
        ),
    }
)


class M30ReceiptAuthenticationProfile(StrEnum):
    EXTERNAL_GITHUB_WORKFLOW = "external_github_workflow"
    EXTERNAL_ED25519_THRESHOLD = "external_ed25519_threshold"


class M30ControlAdjudicatorProfile(StrEnum):
    SIGNED_CAMPAIGN_MANIFEST_V1 = "signed_campaign_manifest_v1"
    ADMITTED_UNADJUDICATED = "admitted_unadjudicated"


class M30CampaignAuthorizationStage(StrEnum):
    TARGET_PREFLIGHT = "target_preflight"
    SOURCE_READONLY_PROBE = "source_readonly_probe"
    CAMPAIGN_RUN = "campaign_run"
    FINAL_REVIEW = "final_review"
    M31_ELIGIBLE = "m31_eligible"


M30_STAGE_PREREQUISITES: Mapping[M30CampaignAuthorizationStage, tuple[M30ControlCode, ...]] = (
    MappingProxyType(
        {
            M30CampaignAuthorizationStage.TARGET_PREFLIGHT: (
                M30ControlCode.SIGNED_CAMPAIGN_MANIFEST,
                M30ControlCode.HOSTED_QUALITY_GATE,
                M30ControlCode.HOSTED_POSTGRES_INTEGRATION,
                M30ControlCode.HOSTED_SUPPLY_CHAIN_SUBJECTS,
                M30ControlCode.PUBLISHED_ARTIFACT_ATTESTATION,
                M30ControlCode.CANDIDATE_CLEAN_ROOM_INSTALL,
            ),
            M30CampaignAuthorizationStage.SOURCE_READONLY_PROBE: (
                M30ControlCode.OPERATED_TARGET_CLUSTER,
                M30ControlCode.OPERATED_OIDC_TENANT_ISOLATION,
                M30ControlCode.OPERATED_SECRET_ROTATION,
                M30ControlCode.OPERATED_DATAHUB_IAM,
                M30ControlCode.OPERATED_SIEM_PAGING,
            ),
            M30CampaignAuthorizationStage.CAMPAIGN_RUN: (
                M30ControlCode.INDEPENDENT_SOURCE_READONLY,
                M30ControlCode.ADVERSARIAL_BROWSER_API,
                M30ControlCode.OPERATED_M35_LIFECYCLE,
            ),
            M30CampaignAuthorizationStage.FINAL_REVIEW: (
                M30ControlCode.IMMUTABLE_RAW_RESULT_BUNDLE,
            ),
            M30CampaignAuthorizationStage.M31_ELIGIBLE: M30_CONTROL_TOPOLOGICAL_ORDER,
        }
    )
)


_FIVE_RELEASE_OWNER_ROLES: tuple[M30CampaignOwnerRole, ...] = (
    M30CampaignOwnerRole.PRODUCT,
    M30CampaignOwnerRole.SEMANTIC,
    M30CampaignOwnerRole.SECURITY,
    M30CampaignOwnerRole.OPERATIONS,
    M30CampaignOwnerRole.RELEASE,
)


class M30SignedManifestCriteria(FrozenDomainModel):
    main_branch_protected_required: Literal[True]
    annotated_tag_protected_required: Literal[True]
    attestation_environment_protected_required: Literal[True]
    minimum_independent_reviewers: int = Field(strict=True, ge=1, le=12)
    self_review_allowed: Literal[False]
    administrator_bypass_allowed: Literal[False]
    environment_secret_count_max: Literal[0]
    exclusive_attestation_authority_required: Literal[True]
    exact_candidate_required: Literal[True]
    trusted_timestamp_required: Literal[True]


class M30ControlPolicyRule(FrozenDomainModel):
    control_code: M30ControlCode
    evidence_class: M30EvidenceClass
    evidence_artifact_kind: str = Field(min_length=3, max_length=120)
    receipt_kind: str = Field(min_length=16, max_length=180)
    authentication_profile: M30ReceiptAuthenticationProfile
    trusted_policy_sha256: str
    trusted_issuer: str = Field(min_length=3, max_length=240)
    trusted_subject: str = Field(min_length=3, max_length=240)
    criteria_policy_sha256: str
    required_approver_roles: tuple[M30CampaignOwnerRole, ...] = Field(min_length=1, max_length=5)
    required_approver_key_fingerprints: tuple[str, ...] = Field(min_length=1, max_length=5)
    approval_quorum: int = Field(strict=True, ge=1, le=5)
    trusted_workflow_repository: str | None = Field(default=None, max_length=161)
    trusted_workflow_path: str | None = Field(default=None, max_length=122)
    trusted_workflow_revision: str | None = None
    prerequisites: tuple[M30ControlCode, ...] = Field(max_length=24)
    adjudicator_profile: M30ControlAdjudicatorProfile
    trusted_timestamp_required: Literal[True]

    @field_validator("evidence_artifact_kind", "receipt_kind")
    @classmethod
    def inert_values_are_closed(cls, value: str) -> str:
        if _INERT_ID.fullmatch(value) is None:
            raise ValueError("M30 receipt policy identifiers must be inert")
        return value

    @field_validator("trusted_policy_sha256", "criteria_policy_sha256")
    @classmethod
    def policy_digests_are_sha256(cls, value: str) -> str:
        return _require_sha256(value, "M30 control policy digest")

    @field_validator("trusted_issuer", "trusted_subject")
    @classmethod
    def trust_identities_are_inert(cls, value: str) -> str:
        if _TRUST_ID.fullmatch(value) is None:
            raise ValueError("M30 control trust identities must be inert")
        return value

    @field_validator("required_approver_key_fingerprints")
    @classmethod
    def approver_keys_are_distinct_sha256(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(_SHA256.fullmatch(item) is None for item in value) or len(set(value)) != len(value):
            raise ValueError("M30 approver keys must be distinct lowercase SHA-256 fingerprints")
        return value

    @model_validator(mode="after")
    def rule_matches_the_closed_control_profile(self) -> M30ControlPolicyRule:
        expected_class = M30_REQUIRED_CONTROL_SPECS[self.control_code.value][0]
        if self.evidence_class is not expected_class:
            raise ValueError("M30 control policy evidence class differs from the contract")
        expected_receipt_kind = f"schemabridge.m30.receipt.{self.control_code.value}.v1"
        if self.receipt_kind != expected_receipt_kind:
            raise ValueError("M30 control receipt kind differs from the reviewed policy")
        if self.prerequisites != M30_CONTROL_PREREQUISITES[self.control_code]:
            raise ValueError("M30 control prerequisites differ from the reviewed DAG")
        expected_adjudicator = (
            M30ControlAdjudicatorProfile.SIGNED_CAMPAIGN_MANIFEST_V1
            if self.control_code is M30ControlCode.SIGNED_CAMPAIGN_MANIFEST
            else M30ControlAdjudicatorProfile.ADMITTED_UNADJUDICATED
        )
        if self.adjudicator_profile is not expected_adjudicator:
            raise ValueError("M30 control adjudicator exceeds the implemented Phase-1b scope")
        if self.evidence_class is M30EvidenceClass.OWNER_APPROVAL:
            expected_roles = _FIVE_RELEASE_OWNER_ROLES
            expected_quorum = 5
        else:
            expected_roles = {
                M30EvidenceClass.HOSTED_CI: (M30CampaignOwnerRole.RELEASE,),
                M30EvidenceClass.OPERATED_TARGET: (M30CampaignOwnerRole.OPERATIONS,),
                M30EvidenceClass.INDEPENDENT_THIRD_PARTY: (
                    M30CampaignOwnerRole.INDEPENDENT_EVALUATOR,
                ),
            }[self.evidence_class]
            expected_quorum = 1
        if (
            self.required_approver_roles != expected_roles
            or self.approval_quorum != expected_quorum
            or len(self.required_approver_key_fingerprints) != expected_quorum
        ):
            raise ValueError("M30 control approval roles or quorum differ from policy")
        hosted = (
            self.authentication_profile is M30ReceiptAuthenticationProfile.EXTERNAL_GITHUB_WORKFLOW
        )
        if hosted != (self.evidence_class is M30EvidenceClass.HOSTED_CI):
            raise ValueError("M30 control authentication profile differs from its evidence class")
        workflow_values = (
            self.trusted_workflow_repository,
            self.trusted_workflow_path,
            self.trusted_workflow_revision,
        )
        if hosted:
            repository, path, revision = workflow_values
            if (
                repository is None
                or _REPOSITORY.fullmatch(repository) is None
                or _same_repository(repository, M30_GITHUB_REPOSITORY)
                or path is None
                or _WORKFLOW_PATH.fullmatch(path) is None
                or revision is None
                or _SHA1.fullmatch(revision) is None
            ):
                raise ValueError("hosted M30 receipts require an external immutable workflow")
            if self.trusted_issuer != "https://token.actions.githubusercontent.com":
                raise ValueError("hosted M30 receipts require the reviewed GitHub OIDC issuer")
        elif any(value is not None for value in workflow_values):
            raise ValueError("non-hosted M30 receipts cannot inherit GitHub workflow authority")
        return self


class M30CampaignStagePolicy(FrozenDomainModel):
    stage: M30CampaignAuthorizationStage
    required_controls: tuple[M30ControlCode, ...] = Field(min_length=1, max_length=24)
    provider_access: bool
    source_read_access: bool
    target_access: bool
    corpus_access: bool
    source_write_access: Literal[False]
    raw_llm_sql_access: Literal[False]
    blanket_datahub_write_access: Literal[False]

    @model_validator(mode="after")
    def stage_permissions_are_exact(self) -> M30CampaignStagePolicy:
        if self.required_controls != M30_STAGE_PREREQUISITES[self.stage]:
            raise ValueError("M30 authorization stage prerequisites differ from the reviewed DAG")
        expected_access = {
            M30CampaignAuthorizationStage.TARGET_PREFLIGHT: (False, False, True, False),
            M30CampaignAuthorizationStage.SOURCE_READONLY_PROBE: (False, True, True, False),
            M30CampaignAuthorizationStage.CAMPAIGN_RUN: (True, True, True, True),
            M30CampaignAuthorizationStage.FINAL_REVIEW: (False, False, False, False),
            M30CampaignAuthorizationStage.M31_ELIGIBLE: (False, False, False, False),
        }[self.stage]
        observed_access = (
            self.provider_access,
            self.source_read_access,
            self.target_access,
            self.corpus_access,
        )
        if observed_access != expected_access:
            raise ValueError("M30 authorization stage grants an unreviewed capability")
        return self


class M30ControlPolicy(FrozenDomainModel):
    schema_version: Literal[1]
    kind: Literal["schemabridge.m30.control-policy"]
    policy_id: str = Field(min_length=8, max_length=160)
    campaign_id: str = Field(min_length=8, max_length=160)
    source_repository: Literal["Crespillo95/schemabridge-codex-starter"]
    candidate_revision: str
    candidate_source_tree_sha256: str
    contract_sha256: str
    issued_at: datetime
    not_before: datetime
    expires_at: datetime
    signed_manifest_criteria: M30SignedManifestCriteria
    rules: tuple[M30ControlPolicyRule, ...] = Field(min_length=24, max_length=24)
    authorization_stages: tuple[M30CampaignStagePolicy, ...] = Field(min_length=5, max_length=5)

    @field_validator("policy_id", "campaign_id")
    @classmethod
    def policy_identifiers_are_inert(cls, value: str) -> str:
        if _INERT_ID.fullmatch(value) is None:
            raise ValueError("M30 control policy identifiers must be inert")
        return value

    @field_validator("candidate_revision")
    @classmethod
    def revision_is_full_sha1(cls, value: str) -> str:
        if _SHA1.fullmatch(value) is None:
            raise ValueError("M30 control policy revision must be a full lowercase SHA-1")
        return value

    @field_validator("candidate_source_tree_sha256", "contract_sha256")
    @classmethod
    def policy_subject_digests_are_sha256(cls, value: str) -> str:
        return _require_sha256(value, "M30 control policy subject digest")

    @field_validator("issued_at", "not_before", "expires_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "M30 control policy timestamp")

    @model_validator(mode="after")
    def policy_is_closed_and_acyclic(self) -> M30ControlPolicy:
        if not self.issued_at <= self.not_before < self.expires_at:
            raise ValueError("M30 control policy validity window is invalid")
        if self.expires_at - self.not_before > M30_MAX_CONTROL_POLICY_VALIDITY:
            raise ValueError("M30 control policy validity cannot exceed 30 days")
        if tuple(rule.control_code for rule in self.rules) != M30_CONTROL_TOPOLOGICAL_ORDER:
            raise ValueError("M30 control policy rules must follow the reviewed topological order")
        positions = {code: position for position, code in enumerate(M30_CONTROL_TOPOLOGICAL_ORDER)}
        if any(
            positions[prerequisite] >= positions[rule.control_code]
            for rule in self.rules
            for prerequisite in rule.prerequisites
        ):
            raise ValueError("M30 control policy prerequisite graph is cyclic")
        stages = tuple(item.stage for item in self.authorization_stages)
        if stages != tuple(M30CampaignAuthorizationStage):
            raise ValueError("M30 control policy authorization stages are incomplete")
        workflows = tuple(
            (
                (rule.trusted_workflow_repository or "").casefold(),
                rule.trusted_workflow_path or "",
            )
            for rule in self.rules
            if rule.authentication_profile
            is M30ReceiptAuthenticationProfile.EXTERNAL_GITHUB_WORKFLOW
        )
        if len(set(workflows)) != len(workflows):
            raise ValueError("each hosted M30 control requires a distinct external workflow")
        if self.rules[0].criteria_policy_sha256 != signed_manifest_criteria_fingerprint(
            self.signed_manifest_criteria
        ):
            raise ValueError("signed-manifest criteria digest differs from its typed policy")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.model_dump(mode="json")) + b"\n"

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class M30EvidenceSubject(FrozenDomainModel):
    kind: str = Field(min_length=3, max_length=120)
    media_type: str = Field(min_length=3, max_length=120)
    byte_size: int = Field(strict=True, ge=1, le=10_000_000_000)
    sha256: str

    @field_validator("kind", "media_type")
    @classmethod
    def subject_identity_is_inert(cls, value: str) -> str:
        if _INERT_ID.fullmatch(value) is None:
            raise ValueError("M30 evidence subject identity must be inert")
        return value

    @field_validator("sha256")
    @classmethod
    def subject_digest_is_sha256(cls, value: str) -> str:
        return _require_sha256(value, "M30 evidence subject digest")


class M30SignedManifestGovernanceFacts(FrozenDomainModel):
    source_repository: Literal["Crespillo95/schemabridge-codex-starter"]
    source_revision: str
    source_ref: str = Field(pattern=r"^refs/tags/v[0-9]+\.[0-9]+\.[0-9]+$")
    release_tag: str = Field(pattern=r"^v[0-9]+\.[0-9]+\.[0-9]+$")
    attestation_environment: Literal["m30-manifest-attestation"]
    manifest_workflow_sha256: str
    main_branch_protected: StrictBool
    annotated_tag_protected: StrictBool
    attestation_environment_protected: StrictBool
    independent_reviewer_authorities: tuple[str, ...] = Field(min_length=1, max_length=12)
    workflow_actor_authority: str = Field(min_length=3, max_length=160)
    self_review_enabled: StrictBool
    administrator_bypass_enabled: StrictBool
    environment_secret_count: int = Field(strict=True, ge=0, le=1_000)
    exclusive_attestation_authority: StrictBool
    exact_candidate_observed: StrictBool
    trusted_timestamp_observed: StrictBool
    branch_ruleset_snapshot_sha256: str
    tag_ruleset_snapshot_sha256: str
    environment_snapshot_sha256: str

    @field_validator("workflow_actor_authority")
    @classmethod
    def actor_is_inert(cls, value: str) -> str:
        if _INERT_ID.fullmatch(value) is None:
            raise ValueError("M30 workflow actor must be an inert authority identifier")
        return value

    @field_validator("source_revision")
    @classmethod
    def source_revision_is_sha1(cls, value: str) -> str:
        if _SHA1.fullmatch(value) is None:
            raise ValueError("M30 governance source revision must be a full lowercase SHA-1")
        return value

    @field_validator("independent_reviewer_authorities")
    @classmethod
    def reviewers_are_distinct_and_inert(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if (
            len(set(value)) != len(value)
            or tuple(sorted(value)) != value
            or any(_INERT_ID.fullmatch(item) is None for item in value)
        ):
            raise ValueError("M30 reviewers must be sorted distinct inert authorities")
        return value

    @field_validator(
        "branch_ruleset_snapshot_sha256",
        "tag_ruleset_snapshot_sha256",
        "environment_snapshot_sha256",
        "manifest_workflow_sha256",
    )
    @classmethod
    def governance_snapshots_are_sha256(cls, value: str) -> str:
        return _require_sha256(value, "M30 governance snapshot digest")

    @model_validator(mode="after")
    def actor_cannot_review_itself(self) -> M30SignedManifestGovernanceFacts:
        if self.workflow_actor_authority in self.independent_reviewer_authorities:
            raise ValueError("M30 workflow actor cannot count as an independent reviewer")
        if self.source_ref != f"refs/tags/{self.release_tag}":
            raise ValueError("M30 governance source ref and release tag differ")
        return self


class M30SignedManifestControlReceipt(FrozenDomainModel):
    schema_version: Literal[1]
    kind: Literal["schemabridge.m30.receipt.signed_campaign_manifest.v1"]
    receipt_id: str = Field(min_length=8, max_length=160)
    attempt: Literal[1]
    previous_receipt_sha256: Literal[None] = None
    campaign_id: str = Field(min_length=8, max_length=160)
    manifest_sha256: str
    manifest_authentication_sha256: str
    control_policy_sha256: str
    contract_sha256: str
    candidate_revision: str
    candidate_source_tree_sha256: str
    control_code: Literal[M30ControlCode.SIGNED_CAMPAIGN_MANIFEST]
    evidence_class: Literal[M30EvidenceClass.HOSTED_CI]
    control_assignment_sha256: str
    observed_started_at: datetime
    observed_completed_at: datetime
    issued_at: datetime
    evidence_subjects: tuple[M30EvidenceSubject, ...] = Field(min_length=3, max_length=3)
    facts: M30SignedManifestGovernanceFacts

    @field_validator("receipt_id", "campaign_id")
    @classmethod
    def receipt_identifiers_are_inert(cls, value: str) -> str:
        if _INERT_ID.fullmatch(value) is None:
            raise ValueError("M30 receipt identifiers must be inert")
        return value

    @field_validator(
        "previous_receipt_sha256",
        "manifest_sha256",
        "manifest_authentication_sha256",
        "control_policy_sha256",
        "contract_sha256",
        "candidate_source_tree_sha256",
        "control_assignment_sha256",
    )
    @classmethod
    def receipt_digests_are_sha256(cls, value: str | None) -> str | None:
        return None if value is None else _require_sha256(value, "M30 receipt digest")

    @field_validator("candidate_revision")
    @classmethod
    def receipt_revision_is_sha1(cls, value: str) -> str:
        if _SHA1.fullmatch(value) is None:
            raise ValueError("M30 receipt revision must be a full lowercase SHA-1")
        return value

    @field_validator("observed_started_at", "observed_completed_at", "issued_at")
    @classmethod
    def receipt_timestamps_are_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "M30 receipt timestamp")

    @model_validator(mode="after")
    def receipt_chain_and_observation_are_ordered(self) -> M30SignedManifestControlReceipt:
        if not self.observed_started_at <= self.observed_completed_at <= self.issued_at:
            raise ValueError("M30 receipt observation window is invalid")
        if self.issued_at - self.observed_started_at > M30_MAX_CONTROL_RECEIPT_VALIDITY:
            raise ValueError("M30 receipt observation cannot exceed 30 days")
        identities = tuple((item.kind, item.sha256) for item in self.evidence_subjects)
        if identities != tuple(sorted(identities)) or len(set(identities)) != len(identities):
            raise ValueError("M30 receipt subjects must be sorted and unique")
        expected_subjects = {
            ("branch-ruleset", self.facts.branch_ruleset_snapshot_sha256),
            ("environment-policy", self.facts.environment_snapshot_sha256),
            ("tag-ruleset", self.facts.tag_ruleset_snapshot_sha256),
        }
        if set(identities) != expected_subjects or any(
            item.media_type != "application/json" for item in self.evidence_subjects
        ):
            raise ValueError("M30 governance subjects differ from the typed snapshot facts")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.model_dump(mode="json")) + b"\n"

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class M30AuthenticatedReceiptProducer(FrozenDomainModel):
    receipt_sha256: str
    trusted_policy_sha256: str
    workflow_repository: str
    workflow_path: str
    workflow_revision: str
    issuer: str = Field(min_length=8, max_length=200)
    subject: str = Field(min_length=8, max_length=240)
    attestation_bundle_sha256: str
    verification_summary_sha256: str
    trusted_timestamps_sha256: str
    trusted_timestamp_count: int = Field(strict=True, ge=1, le=16)
    signature_verified: Literal[True]
    trusted_timestamp_verified: Literal[True]
    verified_at: datetime

    @field_validator(
        "receipt_sha256",
        "trusted_policy_sha256",
        "attestation_bundle_sha256",
        "verification_summary_sha256",
        "trusted_timestamps_sha256",
    )
    @classmethod
    def authentication_digests_are_sha256(cls, value: str) -> str:
        return _require_sha256(value, "M30 authenticated receipt digest")

    @field_validator("workflow_repository")
    @classmethod
    def workflow_repository_is_external(cls, value: str) -> str:
        if _REPOSITORY.fullmatch(value) is None or _same_repository(value, M30_GITHUB_REPOSITORY):
            raise ValueError("M30 receipt producer workflow must be externally controlled")
        return value

    @field_validator("workflow_path")
    @classmethod
    def workflow_path_is_exact(cls, value: str) -> str:
        if _WORKFLOW_PATH.fullmatch(value) is None:
            raise ValueError("M30 receipt producer workflow path is invalid")
        return value

    @field_validator("workflow_revision")
    @classmethod
    def workflow_revision_is_sha1(cls, value: str) -> str:
        if _SHA1.fullmatch(value) is None:
            raise ValueError("M30 receipt workflow revision must be a full lowercase SHA-1")
        return value

    @field_validator("verified_at")
    @classmethod
    def verified_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "M30 receipt verification timestamp")


class M30AuthenticatedApprover(FrozenDomainModel):
    role: M30CampaignOwnerRole
    authority_id: str = Field(min_length=3, max_length=160)
    key_fingerprint: str
    signed_payload_sha256: str
    signature_sha256: str
    signature_verified: Literal[True]
    trusted_timestamp_verified: Literal[True]
    verified_at: datetime

    @field_validator("authority_id")
    @classmethod
    def authority_is_inert(cls, value: str) -> str:
        if _INERT_ID.fullmatch(value) is None:
            raise ValueError("M30 receipt approver authority must be inert")
        return value

    @field_validator("key_fingerprint", "signed_payload_sha256", "signature_sha256")
    @classmethod
    def approval_digests_are_sha256(cls, value: str) -> str:
        return _require_sha256(value, "M30 receipt approval digest")

    @field_validator("verified_at")
    @classmethod
    def approval_verification_time_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "M30 receipt approval timestamp")


class M30AuthenticatedSignedManifestReceipt(FrozenDomainModel):
    receipt: M30SignedManifestControlReceipt
    producer: M30AuthenticatedReceiptProducer
    approvers: tuple[M30AuthenticatedApprover, ...] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def authentication_binds_the_exact_receipt(self) -> M30AuthenticatedSignedManifestReceipt:
        if self.producer.receipt_sha256 != self.receipt.fingerprint():
            raise ValueError("M30 authenticated producer differs from receipt bytes")
        roles = tuple(item.role for item in self.approvers)
        fingerprints = tuple(item.key_fingerprint for item in self.approvers)
        if len(set(roles)) != len(roles) or len(set(fingerprints)) != len(fingerprints):
            raise ValueError("M30 receipt approvals must use distinct roles and keys")
        if any(
            item.signed_payload_sha256 != self.receipt.fingerprint()
            or item.verified_at < self.receipt.issued_at
            for item in self.approvers
        ):
            raise ValueError("M30 approval signature differs from exact receipt bytes or time")
        return self


M30_SIGNED_MANIFEST_CRITERION_CODES = tuple(
    sorted(
        (
            "administrator_bypass_disabled",
            "annotated_tag_protected",
            "approver_quorum",
            "assignment_binding",
            "attestation_environment_protected",
            "campaign_binding",
            "candidate_binding",
            "environment_has_no_secrets",
            "exact_candidate_observed",
            "exclusive_attestation_authority",
            "governance_subject_identity",
            "independent_reviewers",
            "main_branch_protected",
            "manifest_authentication_binding",
            "manifest_binding",
            "policy_binding",
            "producer_policy",
            "receipt_window",
            "self_review_disabled",
            "trusted_timestamp_observed",
        )
    )
)


class M30CriterionResult(FrozenDomainModel):
    criterion: str = Field(min_length=3, max_length=120)
    passed: StrictBool

    @field_validator("criterion")
    @classmethod
    def criterion_is_inert(cls, value: str) -> str:
        if _INERT_ID.fullmatch(value) is None:
            raise ValueError("M30 criterion identifier must be inert")
        return value


class M30ControlAdjudicationStatus(StrEnum):
    BLOCKED_PREREQUISITES = "blocked_prerequisites"
    FAILED = "failed"
    PASSED = "passed"
    ADMITTED_UNADJUDICATED = "admitted_unadjudicated"


class M30PrerequisiteAdjudicationRef(FrozenDomainModel):
    control_code: M30ControlCode
    campaign_id: str = Field(min_length=8, max_length=160)
    manifest_sha256: str
    control_policy_sha256: str
    adjudication_sha256: str
    status: Literal[M30ControlAdjudicationStatus.PASSED]

    @field_validator("campaign_id")
    @classmethod
    def prerequisite_campaign_is_inert(cls, value: str) -> str:
        if _INERT_ID.fullmatch(value) is None:
            raise ValueError("M30 prerequisite campaign identifier must be inert")
        return value

    @field_validator("manifest_sha256", "control_policy_sha256", "adjudication_sha256")
    @classmethod
    def prerequisite_digests_are_sha256(cls, value: str) -> str:
        return _require_sha256(value, "M30 prerequisite adjudication digest")


class M30ControlAdjudication(FrozenDomainModel):
    control_code: M30ControlCode
    campaign_id: str = Field(min_length=8, max_length=160)
    manifest_sha256: str
    control_policy_sha256: str
    receipt_sha256: str
    status: M30ControlAdjudicationStatus
    criteria: tuple[M30CriterionResult, ...] = Field(min_length=1, max_length=32)
    prerequisite_adjudications: tuple[M30PrerequisiteAdjudicationRef, ...] = Field(max_length=24)
    campaign_executable: Literal[False]
    release_decision: Literal[M30ReleaseDecision.NO_GO]

    @field_validator("campaign_id")
    @classmethod
    def adjudication_campaign_is_inert(cls, value: str) -> str:
        if _INERT_ID.fullmatch(value) is None:
            raise ValueError("M30 adjudication campaign identifier must be inert")
        return value

    @field_validator("manifest_sha256", "control_policy_sha256", "receipt_sha256")
    @classmethod
    def adjudication_receipt_is_sha256(cls, value: str) -> str:
        return _require_sha256(value, "M30 adjudication receipt digest")

    @model_validator(mode="after")
    def status_is_derived_from_criteria(self) -> M30ControlAdjudication:
        passed = all(item.passed for item in self.criteria)
        criterion_codes = tuple(item.criterion for item in self.criteria)
        if criterion_codes != tuple(sorted(set(criterion_codes))):
            raise ValueError("M30 adjudication criteria must be sorted and unique")
        implemented = self.control_code is M30ControlCode.SIGNED_CAMPAIGN_MANIFEST
        if not implemented and self.status not in {
            M30ControlAdjudicationStatus.BLOCKED_PREREQUISITES,
            M30ControlAdjudicationStatus.ADMITTED_UNADJUDICATED,
        }:
            raise ValueError("unimplemented M30 controls cannot be adjudicated passed or failed")
        if (
            implemented
            and self.status
            in {
                M30ControlAdjudicationStatus.PASSED,
                M30ControlAdjudicationStatus.FAILED,
            }
            and criterion_codes != M30_SIGNED_MANIFEST_CRITERION_CODES
        ):
            raise ValueError("signed-manifest adjudication criteria are incomplete or invented")
        if self.status is M30ControlAdjudicationStatus.PASSED and not passed:
            raise ValueError("passed M30 control has a failed criterion")
        if self.status is M30ControlAdjudicationStatus.FAILED and passed:
            raise ValueError("failed M30 control has no failed criterion")
        prerequisite_codes = tuple(item.control_code for item in self.prerequisite_adjudications)
        expected_prerequisites = M30_CONTROL_PREREQUISITES[self.control_code]
        if len(set(prerequisite_codes)) != len(prerequisite_codes):
            raise ValueError("M30 prerequisite adjudications must be unique")
        if self.status is M30ControlAdjudicationStatus.BLOCKED_PREREQUISITES:
            if any(code not in expected_prerequisites for code in prerequisite_codes):
                raise ValueError("blocked M30 adjudication contains an invented prerequisite")
        elif prerequisite_codes != expected_prerequisites:
            raise ValueError("M30 adjudication does not bind its exact prerequisite set")
        if any(
            item.campaign_id != self.campaign_id
            or item.manifest_sha256 != self.manifest_sha256
            or item.control_policy_sha256 != self.control_policy_sha256
            for item in self.prerequisite_adjudications
        ):
            raise ValueError("M30 prerequisite adjudication crosses its campaign or policy")
        return self

    def fingerprint(self) -> str:
        return hashlib.sha256(_canonical_json_bytes(self.model_dump(mode="json"))).hexdigest()


class M30ControlPolicyValidationState(StrEnum):
    VALIDATED = "validated"
    BLOCKED = "blocked"


class M30ControlPolicyBlockReason(StrEnum):
    MANIFEST_NOT_AUTHENTICATED = "manifest_not_authenticated"
    MANIFEST_CHANGED = "manifest_changed"
    POLICY_SUBJECT_MISMATCH = "policy_subject_mismatch"
    POLICY_NOT_YET_VALID = "policy_not_yet_valid"
    POLICY_EXPIRED = "policy_expired"
    CLOCK_ROLLBACK = "clock_rollback"


class M30ControlPolicyValidationReport(FrozenDomainModel):
    schema_version: Literal[2]
    milestone: Literal["M30"]
    phase: Literal["1b-policy"]
    state: M30ControlPolicyValidationState
    blocking_reasons: tuple[M30ControlPolicyBlockReason, ...] = Field(max_length=5)
    campaign_id: str | None = Field(default=None, min_length=8, max_length=160)
    manifest_sha256: str | None = None
    manifest_authentication_sha256: str | None = None
    control_policy_id: str | None = Field(default=None, min_length=8, max_length=160)
    control_policy_sha256: str | None = None
    observed_at: datetime
    policy_not_before: datetime | None = None
    policy_expires_at: datetime | None = None
    policy_bound_to_authenticated_manifest: bool
    control_dag_validated: bool
    external_policy_trust_authenticated: Literal[False]
    receipt_authentication_enabled: Literal[False]
    implemented_adjudicators: Literal[1]
    admitted_unadjudicated_controls: Literal[23]
    external_controls_passed: Literal[0]
    external_controls_remaining: Literal[24]
    campaign_executable: Literal[False]
    release_decision: Literal[M30ReleaseDecision.NO_GO]
    production_release_authorized: Literal[False]
    provider_calls: Literal[0]
    source_reads: Literal[0]
    source_writes: Literal[0]
    target_calls: Literal[0]
    corpus_reads: Literal[0]
    datahub_writes: Literal[0]

    @field_validator(
        "manifest_sha256",
        "manifest_authentication_sha256",
        "control_policy_sha256",
    )
    @classmethod
    def optional_report_digests_are_sha256(cls, value: str | None) -> str | None:
        return None if value is None else _require_sha256(value, "M30 policy report digest")

    @field_validator("observed_at", "policy_not_before", "policy_expires_at")
    @classmethod
    def report_timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value, "M30 policy report timestamp")

    @model_validator(mode="after")
    def report_state_matches_policy_evidence(self) -> M30ControlPolicyValidationReport:
        validated = self.state is M30ControlPolicyValidationState.VALIDATED
        if (
            validated != self.policy_bound_to_authenticated_manifest
            or validated != self.control_dag_validated
        ):
            raise ValueError("M30 policy validation state differs from retained evidence")
        if validated:
            if (
                self.blocking_reasons
                or self.campaign_id is None
                or self.manifest_sha256 is None
                or self.manifest_authentication_sha256 is None
                or self.control_policy_id is None
                or self.control_policy_sha256 is None
                or self.policy_not_before is None
                or self.policy_expires_at is None
            ):
                raise ValueError("validated M30 control policy report lacks exact binding")
            if not self.policy_not_before <= self.observed_at < self.policy_expires_at:
                raise ValueError("validated M30 control policy report is outside its policy window")
        elif not self.blocking_reasons:
            raise ValueError("blocked M30 control policy report requires a reason")
        if (self.policy_not_before is None) != (self.policy_expires_at is None):
            raise ValueError("M30 control policy report window is incomplete")
        return self

    def fingerprint(self) -> str:
        return hashlib.sha256(_canonical_json_bytes(self.model_dump(mode="json"))).hexdigest()


def control_assignment_fingerprint(assignment: M30CampaignControlAssignment) -> str:
    return hashlib.sha256(_canonical_json_bytes(assignment.model_dump(mode="json"))).hexdigest()


def signed_manifest_criteria_fingerprint(criteria: M30SignedManifestCriteria) -> str:
    return hashlib.sha256(_canonical_json_bytes(criteria.model_dump(mode="json"))).hexdigest()


def control_policy_matches_manifest(
    policy: M30ControlPolicy,
    manifest: M30CampaignManifest,
) -> bool:
    """Return exact external-policy binding without adjudicating any receipt."""

    owners = {owner.role: owner for owner in manifest.owners}
    assignments = {M30ControlCode(item.code): item for item in manifest.controls}
    rules_match = all(
        rule.evidence_class is assignments[rule.control_code].evidence_class
        and rule.evidence_artifact_kind == assignments[rule.control_code].evidence_artifact_kind
        and rule.required_approver_key_fingerprints
        == tuple(owners[role].approval_key_fingerprint for role in rule.required_approver_roles)
        for rule in policy.rules
    )
    return (
        policy.policy_id == manifest.control_policy_id
        and policy.fingerprint() == manifest.control_policy_sha256
        and policy.campaign_id == manifest.campaign_id
        and policy.source_repository == manifest.source_repository
        and policy.candidate_revision == manifest.candidate.revision
        and policy.candidate_source_tree_sha256 == manifest.candidate.source_tree_sha256
        and policy.contract_sha256 == manifest.contract_sha256
        and manifest.not_before <= policy.not_before < policy.expires_at <= manifest.expires_at
        and rules_match
    )


def adjudicate_signed_campaign_manifest(
    *,
    authenticated: M30AuthenticatedSignedManifestReceipt,
    manifest: M30CampaignManifest,
    manifest_authentication: M30ManifestAuthenticationReport,
    policy: M30ControlPolicy,
) -> M30ControlAdjudication:
    """Derive the only implemented Phase-1b verdict from authenticated typed facts."""

    receipt = authenticated.receipt
    rule = policy.rules[0]
    assignment = manifest.controls[
        tuple(item.code for item in manifest.controls).index(
            M30ControlCode.SIGNED_CAMPAIGN_MANIFEST.value
        )
    ]
    owners = {owner.role: owner for owner in manifest.owners}
    source_digests = {item.name: item.sha256 for item in manifest.candidate.source_digests}
    criteria = policy.signed_manifest_criteria
    manifest_sha256 = manifest.fingerprint()
    binding_checks = {
        "campaign_binding": receipt.campaign_id == manifest.campaign_id,
        "manifest_binding": receipt.manifest_sha256 == manifest_sha256,
        "manifest_authentication_binding": (
            manifest_authentication.state is M30ManifestAuthenticationState.AUTHENTICATED
            and manifest_authentication.workflow_attested_manifest_authenticated
            and manifest_authentication.campaign_id == manifest.campaign_id
            and manifest_authentication.manifest_sha256 == manifest_sha256
            and manifest_authentication.control_policy_id == manifest.control_policy_id
            and manifest_authentication.control_policy_sha256 == manifest.control_policy_sha256
            and manifest_authentication.authentication is not None
            and manifest_authentication.authentication.manifest_sha256 == manifest_sha256
            and receipt.manifest_authentication_sha256 == manifest_authentication.fingerprint()
        ),
        "policy_binding": (
            receipt.control_policy_sha256 == policy.fingerprint()
            and control_policy_matches_manifest(policy, manifest)
        ),
        "candidate_binding": (
            receipt.contract_sha256 == manifest.contract_sha256
            and receipt.candidate_revision == manifest.candidate.revision
            and receipt.candidate_source_tree_sha256 == manifest.candidate.source_tree_sha256
        ),
        "assignment_binding": (
            receipt.control_assignment_sha256 == control_assignment_fingerprint(assignment)
        ),
        "receipt_window": (
            manifest.not_before
            <= policy.not_before
            <= manifest_authentication.completed_at
            <= receipt.observed_started_at
            <= receipt.observed_completed_at
            <= receipt.issued_at
            < policy.expires_at
            <= manifest.expires_at
        ),
        "producer_policy": (
            authenticated.producer.trusted_policy_sha256 == rule.trusted_policy_sha256
            and authenticated.producer.issuer == rule.trusted_issuer
            and authenticated.producer.subject == rule.trusted_subject
            and authenticated.producer.workflow_repository == rule.trusted_workflow_repository
            and authenticated.producer.workflow_path == rule.trusted_workflow_path
            and authenticated.producer.workflow_revision == rule.trusted_workflow_revision
            and manifest_authentication.completed_at <= authenticated.producer.verified_at
            and receipt.issued_at <= authenticated.producer.verified_at < manifest.expires_at
        ),
        "approver_quorum": (
            tuple(item.role for item in authenticated.approvers) == rule.required_approver_roles
            and tuple(item.key_fingerprint for item in authenticated.approvers)
            == rule.required_approver_key_fingerprints
            and all(
                item.authority_id == owners[item.role].authority_id
                and manifest_authentication.completed_at <= item.verified_at
                and receipt.issued_at <= item.verified_at < manifest.expires_at
                for item in authenticated.approvers
            )
            and len(authenticated.approvers) >= rule.approval_quorum
        ),
        "governance_subject_identity": (
            receipt.facts.source_repository == manifest.source_repository
            and receipt.facts.source_revision == manifest.candidate.revision
            and receipt.facts.source_ref == f"refs/tags/v{manifest.candidate.package_version}"
            and receipt.facts.release_tag == f"v{manifest.candidate.package_version}"
            and receipt.facts.attestation_environment == "m30-manifest-attestation"
            and receipt.facts.manifest_workflow_sha256 == source_digests["m30_attestation_workflow"]
        ),
        "main_branch_protected": (
            receipt.facts.main_branch_protected is criteria.main_branch_protected_required
        ),
        "annotated_tag_protected": (
            receipt.facts.annotated_tag_protected is criteria.annotated_tag_protected_required
        ),
        "attestation_environment_protected": (
            receipt.facts.attestation_environment_protected
            is criteria.attestation_environment_protected_required
        ),
        "independent_reviewers": (
            len(receipt.facts.independent_reviewer_authorities)
            >= criteria.minimum_independent_reviewers
        ),
        "self_review_disabled": (receipt.facts.self_review_enabled is criteria.self_review_allowed),
        "administrator_bypass_disabled": (
            receipt.facts.administrator_bypass_enabled is criteria.administrator_bypass_allowed
        ),
        "environment_has_no_secrets": (
            receipt.facts.environment_secret_count <= criteria.environment_secret_count_max
        ),
        "exclusive_attestation_authority": (
            receipt.facts.exclusive_attestation_authority
            is criteria.exclusive_attestation_authority_required
        ),
        "exact_candidate_observed": (
            receipt.facts.exact_candidate_observed is criteria.exact_candidate_required
        ),
        "trusted_timestamp_observed": (
            receipt.facts.trusted_timestamp_observed is criteria.trusted_timestamp_required
        ),
    }
    results = tuple(
        M30CriterionResult(criterion=code, passed=passed)
        for code, passed in sorted(binding_checks.items())
    )
    return M30ControlAdjudication(
        control_code=M30ControlCode.SIGNED_CAMPAIGN_MANIFEST,
        campaign_id=manifest.campaign_id,
        manifest_sha256=manifest_sha256,
        control_policy_sha256=policy.fingerprint(),
        receipt_sha256=receipt.fingerprint(),
        status=(
            M30ControlAdjudicationStatus.PASSED
            if all(item.passed for item in results)
            else M30ControlAdjudicationStatus.FAILED
        ),
        criteria=results,
        prerequisite_adjudications=(),
        campaign_executable=False,
        release_decision=M30ReleaseDecision.NO_GO,
    )


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _require_sha256(value: str, label: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


def _require_utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be timezone-aware UTC")
    return value.astimezone(UTC)


def _same_repository(left: str, right: str) -> bool:
    return left.casefold() == right.casefold()


__all__ = [
    "M30_CONTROL_POLICY_KIND",
    "M30_CONTROL_PREREQUISITES",
    "M30_CONTROL_TOPOLOGICAL_ORDER",
    "M30_SIGNED_MANIFEST_CRITERION_CODES",
    "M30_SIGNED_MANIFEST_RECEIPT_KIND",
    "M30_STAGE_PREREQUISITES",
    "M30AuthenticatedApprover",
    "M30AuthenticatedReceiptProducer",
    "M30AuthenticatedSignedManifestReceipt",
    "M30CampaignAuthorizationStage",
    "M30CampaignStagePolicy",
    "M30ControlAdjudication",
    "M30ControlAdjudicationStatus",
    "M30ControlAdjudicatorProfile",
    "M30ControlCode",
    "M30ControlPolicy",
    "M30ControlPolicyBlockReason",
    "M30ControlPolicyRule",
    "M30ControlPolicyValidationReport",
    "M30ControlPolicyValidationState",
    "M30CriterionResult",
    "M30EvidenceSubject",
    "M30PrerequisiteAdjudicationRef",
    "M30ReceiptAuthenticationProfile",
    "M30SignedManifestControlReceipt",
    "M30SignedManifestCriteria",
    "M30SignedManifestGovernanceFacts",
    "adjudicate_signed_campaign_manifest",
    "control_assignment_fingerprint",
    "control_policy_matches_manifest",
    "signed_manifest_criteria_fingerprint",
]
