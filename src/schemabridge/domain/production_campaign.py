"""Fail-closed contracts for authorizing one external M30 campaign manifest."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.production_readiness import (
    M30_CASE_MINIMUMS,
    M30_LANGUAGE_MINIMUMS,
    M30_REQUIRED_CONTROL_SPECS,
    M30_SUPPORTED_FAMILIES,
    M30_UNSUPPORTED_FAMILIES,
    M30CampaignContract,
    M30CandidateBranch,
    M30CandidateObservation,
    M30CandidateSku,
    M30CaseClass,
    M30EvidenceClass,
    M30ReadinessReport,
    M30ReleaseDecision,
)

_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OCI_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_INERT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,159}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,79}$")

M30_MANIFEST_KIND = "schemabridge.m30.campaign-manifest"
M30_GITHUB_REPOSITORY = "Crespillo95/schemabridge-codex-starter"
M30_GITHUB_SIGNER_WORKFLOW = (
    "Crespillo95/schemabridge-codex-starter/.github/workflows/m30-manifest-attestation.yml"
)
M30_GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
M30_GITHUB_ATTESTATION_PREDICATE = "https://slsa.dev/provenance/v1"
M30_GITHUB_CLI_VERSION = "2.96.0"
M30_GITHUB_CLI_EXECUTABLE_SHA256: Mapping[str, str] = MappingProxyType(
    {
        "linux-amd64": "56b8bbbb27b066ecb33dbef9a256dc9d1314adaeff0908a752feba6c34053b40",
        "linux-arm64": "f903d2fa04ae78ee8f8df0186364d0b92f078d2720053941a47e0a3beaef54f0",
        "macos-amd64": "49380b19e758c14ce5509afe0ced58777fe45cfbcd5989941f7314d5f7e468c8",
        "macos-arm64": "b1d6c442fde99ca27c04e1e74d624895abe37785f4a3e9e9b684bf7586ce4bc8",
    }
)
M30_MAX_MANIFEST_VALIDITY = timedelta(days=30)

M30_SIMPLE_FAMILIES = (
    "dates",
    "filters",
    "identifiers",
    "limits",
    "null_semantics",
    "projection",
    "sorting",
)
M30_ADVANCED_FAMILIES = tuple(
    family for family in M30_SUPPORTED_FAMILIES if family not in M30_SIMPLE_FAMILIES
)
M30_SPECIAL_SLICE_FAMILIES = ("cross_family", "security")


class M30CampaignLanguage(StrEnum):
    SPANISH = "es"
    ENGLISH = "en"


class M30CampaignRisk(StrEnum):
    STANDARD = "standard"
    HIGH = "high"
    CRITICAL = "critical"


class M30CampaignOwnerRole(StrEnum):
    PRODUCT = "product_owner"
    SEMANTIC = "semantic_owner"
    SECURITY = "security_owner"
    OPERATIONS = "operations_owner"
    RELEASE = "release_owner"
    INDEPENDENT_EVALUATOR = "independent_evaluator"


M30_RESPONSIBLE_ROLE_BY_EVIDENCE_CLASS = MappingProxyType(
    {
        M30EvidenceClass.HOSTED_CI: M30CampaignOwnerRole.RELEASE,
        M30EvidenceClass.OPERATED_TARGET: M30CampaignOwnerRole.OPERATIONS,
        M30EvidenceClass.INDEPENDENT_THIRD_PARTY: M30CampaignOwnerRole.INDEPENDENT_EVALUATOR,
        M30EvidenceClass.OWNER_APPROVAL: M30CampaignOwnerRole.PRODUCT,
    }
)


class M30CampaignOwner(FrozenDomainModel):
    role: M30CampaignOwnerRole
    authority_id: str = Field(min_length=3, max_length=160)
    approval_key_fingerprint: str

    @field_validator("authority_id")
    @classmethod
    def authority_is_inert(cls, value: str) -> str:
        if _INERT_ID.fullmatch(value) is None:
            raise ValueError("M30 owner authority must be an opaque inert identifier")
        return value

    @field_validator("approval_key_fingerprint")
    @classmethod
    def key_fingerprint_is_sha256(cls, value: str) -> str:
        return _require_sha256(value, "M30 owner key fingerprint")


class M30CampaignArtifactSet(FrozenDomainModel):
    wheel_sha256: str
    runtime_image_digest: str
    wheel_sbom_sha256: str
    runtime_image_sbom_sha256: str
    provenance_sha256: str
    frozen_requirements_sha256: str

    @field_validator(
        "wheel_sha256",
        "wheel_sbom_sha256",
        "runtime_image_sbom_sha256",
        "provenance_sha256",
        "frozen_requirements_sha256",
    )
    @classmethod
    def artifact_digest_is_sha256(cls, value: str) -> str:
        return _require_sha256(value, "M30 artifact digest")

    @field_validator("runtime_image_digest")
    @classmethod
    def image_digest_is_oci_sha256(cls, value: str) -> str:
        if _OCI_SHA256.fullmatch(value) is None:
            raise ValueError("M30 runtime image digest must be an exact OCI sha256 digest")
        return value


class M30CampaignProviderFreeze(FrozenDomainModel):
    provider_id: str = Field(min_length=3, max_length=160)
    model_snapshot: str = Field(min_length=1, max_length=160)
    configuration_sha256: str
    prompt_bundle_sha256: str
    parameters_sha256: str

    @field_validator("provider_id", "model_snapshot")
    @classmethod
    def provider_identity_is_inert(cls, value: str) -> str:
        if _INERT_ID.fullmatch(value) is None:
            raise ValueError("M30 provider identity must be opaque and inert")
        return value

    @field_validator("configuration_sha256", "prompt_bundle_sha256", "parameters_sha256")
    @classmethod
    def provider_digest_is_sha256(cls, value: str) -> str:
        return _require_sha256(value, "M30 provider freeze digest")


class M30BrowserVersion(FrozenDomainModel):
    browser: Literal["chrome", "edge", "firefox", "safari"]
    version: str = Field(min_length=1, max_length=80)

    @field_validator("version")
    @classmethod
    def version_is_inert(cls, value: str) -> str:
        if _VERSION.fullmatch(value) is None:
            raise ValueError("M30 browser version must be inert")
        return value


class M30CampaignTargetFreeze(FrozenDomainModel):
    environment_id: str = Field(min_length=3, max_length=160)
    region: str = Field(min_length=2, max_length=80)
    environment_sha256: str
    postgresql_version: str = Field(min_length=1, max_length=80)
    datahub_version: str = Field(min_length=1, max_length=80)
    browsers: tuple[M30BrowserVersion, ...] = Field(min_length=4, max_length=4)
    mobile_viewport: Literal["390x844"]
    iam_policy_sha256: str
    network_policy_sha256: str
    secret_versions_sha256: str
    observability_routes_sha256: str

    @field_validator("environment_id", "region")
    @classmethod
    def target_identity_is_inert(cls, value: str) -> str:
        if _INERT_ID.fullmatch(value) is None:
            raise ValueError("M30 target identity must be opaque and inert")
        return value

    @field_validator("postgresql_version", "datahub_version")
    @classmethod
    def target_version_is_inert(cls, value: str) -> str:
        if _VERSION.fullmatch(value) is None:
            raise ValueError("M30 target version must be inert")
        return value

    @field_validator(
        "environment_sha256",
        "iam_policy_sha256",
        "network_policy_sha256",
        "secret_versions_sha256",
        "observability_routes_sha256",
    )
    @classmethod
    def target_digest_is_sha256(cls, value: str) -> str:
        return _require_sha256(value, "M30 target freeze digest")

    @model_validator(mode="after")
    def browser_matrix_is_exact(self) -> M30CampaignTargetFreeze:
        names = tuple(item.browser for item in self.browsers)
        if names != tuple(sorted({"chrome", "edge", "firefox", "safari"})):
            raise ValueError("M30 browser freeze must contain each supported desktop browser once")
        return self


class M30CampaignCorpusSlice(FrozenDomainModel):
    case_class: M30CaseClass
    family: str = Field(min_length=3, max_length=80)
    risk: M30CampaignRisk
    spanish_cases: int = Field(strict=True, ge=0, le=10_000)
    english_cases: int = Field(strict=True, ge=0, le=10_000)

    @field_validator("family")
    @classmethod
    def family_is_reviewed(cls, value: str) -> str:
        allowed = {*M30_SUPPORTED_FAMILIES, *M30_UNSUPPORTED_FAMILIES, *M30_SPECIAL_SLICE_FAMILIES}
        if value not in allowed:
            raise ValueError("M30 corpus slice family is outside the reviewed campaign vocabulary")
        return value

    @model_validator(mode="after")
    def slice_class_matches_family(self) -> M30CampaignCorpusSlice:
        if self.spanish_cases + self.english_cases < 1:
            raise ValueError("M30 corpus slices cannot be empty")
        allowed_by_class = {
            M30CaseClass.SUPPORTED_SIMPLE: set(M30_SIMPLE_FAMILIES),
            M30CaseClass.SUPPORTED_ADVANCED: set(M30_ADVANCED_FAMILIES),
            M30CaseClass.AMBIGUOUS: {*M30_SUPPORTED_FAMILIES, "cross_family"},
            M30CaseClass.UNSUPPORTED: set(M30_UNSUPPORTED_FAMILIES),
            M30CaseClass.ADVERSARIAL_SECURITY: {"security"},
        }
        if self.family not in allowed_by_class[self.case_class]:
            raise ValueError("M30 corpus slice family does not match its case class")
        allowed_risks = {
            M30CaseClass.SUPPORTED_SIMPLE: {M30CampaignRisk.STANDARD},
            M30CaseClass.SUPPORTED_ADVANCED: {
                M30CampaignRisk.STANDARD,
                M30CampaignRisk.HIGH,
                M30CampaignRisk.CRITICAL,
            },
            M30CaseClass.AMBIGUOUS: {M30CampaignRisk.HIGH},
            M30CaseClass.UNSUPPORTED: {M30CampaignRisk.HIGH},
            M30CaseClass.ADVERSARIAL_SECURITY: {M30CampaignRisk.CRITICAL},
        }
        if self.risk not in allowed_risks[self.case_class]:
            raise ValueError("M30 corpus slice risk does not match its case class")
        if self.spanish_cases != self.english_cases:
            raise ValueError(
                "M30 corpus slice must be exactly balanced between Spanish and English"
            )
        return self


class M30CampaignCorpusFreeze(FrozenDomainModel):
    corpus_manifest_sha256: str
    case_ids_sha256: str
    hidden_answer_key_sha256: str
    oracle_dataset_sha256: str
    slices: tuple[M30CampaignCorpusSlice, ...] = Field(min_length=5, max_length=128)

    @field_validator(
        "corpus_manifest_sha256",
        "case_ids_sha256",
        "hidden_answer_key_sha256",
        "oracle_dataset_sha256",
    )
    @classmethod
    def corpus_digest_is_sha256(cls, value: str) -> str:
        return _require_sha256(value, "M30 corpus freeze digest")

    @model_validator(mode="after")
    def slices_equal_the_reviewed_bilingual_contract(self) -> M30CampaignCorpusFreeze:
        identities = tuple((item.case_class, item.family, item.risk) for item in self.slices)
        expected_identities = {
            *(
                (M30CaseClass.SUPPORTED_SIMPLE, family, M30CampaignRisk.STANDARD)
                for family in M30_SIMPLE_FAMILIES
            ),
            *(
                (M30CaseClass.SUPPORTED_ADVANCED, family, risk)
                for family in M30_ADVANCED_FAMILIES
                for risk in (
                    M30CampaignRisk.STANDARD,
                    M30CampaignRisk.HIGH,
                    M30CampaignRisk.CRITICAL,
                )
            ),
            (M30CaseClass.AMBIGUOUS, "cross_family", M30CampaignRisk.HIGH),
            *(
                (M30CaseClass.UNSUPPORTED, family, M30CampaignRisk.HIGH)
                for family in M30_UNSUPPORTED_FAMILIES
            ),
            (M30CaseClass.ADVERSARIAL_SECURITY, "security", M30CampaignRisk.CRITICAL),
        }
        if identities != tuple(
            sorted(expected_identities, key=lambda item: tuple(str(value) for value in item))
        ):
            raise ValueError("M30 corpus slices must equal the reviewed class/family/risk matrix")
        totals: Counter[M30CaseClass] = Counter()
        spanish: Counter[M30CaseClass] = Counter()
        english: Counter[M30CaseClass] = Counter()
        families: dict[M30CaseClass, set[str]] = {case_class: set() for case_class in M30CaseClass}
        for item in self.slices:
            totals[item.case_class] += item.spanish_cases + item.english_cases
            spanish[item.case_class] += item.spanish_cases
            english[item.case_class] += item.english_cases
            families[item.case_class].add(item.family)
        if dict(totals) != dict(M30_CASE_MINIMUMS):
            raise ValueError("M30 corpus slices must total the reviewed 1,000-case contract")
        if any(
            (spanish[case_class], english[case_class]) != M30_LANGUAGE_MINIMUMS[case_class]
            for case_class in M30CaseClass
        ):
            raise ValueError("M30 corpus slices must retain every reviewed Spanish/English total")
        required_families = {
            M30CaseClass.SUPPORTED_SIMPLE: set(M30_SIMPLE_FAMILIES),
            M30CaseClass.SUPPORTED_ADVANCED: set(M30_ADVANCED_FAMILIES),
            M30CaseClass.AMBIGUOUS: {"cross_family"},
            M30CaseClass.UNSUPPORTED: set(M30_UNSUPPORTED_FAMILIES),
            M30CaseClass.ADVERSARIAL_SECURITY: {"security"},
        }
        if any(
            families[case_class] != expected for case_class, expected in required_families.items()
        ):
            raise ValueError("M30 corpus slices must cover every reviewed SQL family")
        return self


class M30CampaignControlAssignment(FrozenDomainModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    evidence_class: M30EvidenceClass
    responsible_authority: str = Field(min_length=3, max_length=160)
    evidence_artifact_kind: str = Field(min_length=3, max_length=120)

    @field_validator("responsible_authority", "evidence_artifact_kind")
    @classmethod
    def assignment_identity_is_inert(cls, value: str) -> str:
        if _INERT_ID.fullmatch(value) is None:
            raise ValueError("M30 control assignment values must be opaque and inert")
        return value


class M30CampaignManifest(FrozenDomainModel):
    """Canonical frozen inputs authenticated before, but not results from, an M30 campaign."""

    schema_version: Literal[1]
    kind: Literal["schemabridge.m30.campaign-manifest"]
    campaign_id: str = Field(min_length=8, max_length=160)
    source_repository: Literal["Crespillo95/schemabridge-codex-starter"]
    trust_policy_id: Literal["github-actions-m30-v1"]
    issued_at: datetime
    not_before: datetime
    expires_at: datetime
    contract_sha256: str
    candidate: M30CandidateObservation
    candidate_sku: M30CandidateSku
    artifacts: M30CampaignArtifactSet
    provider: M30CampaignProviderFreeze
    target: M30CampaignTargetFreeze
    corpus: M30CampaignCorpusFreeze
    owners: tuple[M30CampaignOwner, ...] = Field(min_length=6, max_length=6)
    supported_families: tuple[str, ...] = Field(min_length=1, max_length=64)
    unsupported_families: tuple[str, ...] = Field(min_length=1, max_length=64)
    controls: tuple[M30CampaignControlAssignment, ...] = Field(min_length=24, max_length=24)

    @field_validator("campaign_id")
    @classmethod
    def campaign_id_is_inert(cls, value: str) -> str:
        if _INERT_ID.fullmatch(value) is None:
            raise ValueError("M30 campaign id must be opaque and inert")
        return value

    @field_validator("contract_sha256")
    @classmethod
    def contract_digest_is_sha256(cls, value: str) -> str:
        return _require_sha256(value, "M30 campaign contract digest")

    @field_validator("issued_at", "not_before", "expires_at")
    @classmethod
    def timestamps_are_exact_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("M30 campaign timestamps must be timezone-aware UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def manifest_is_the_exact_reviewed_campaign(self) -> M30CampaignManifest:
        if not self.issued_at <= self.not_before < self.expires_at:
            raise ValueError("M30 campaign validity window is invalid")
        if self.expires_at - self.not_before > M30_MAX_MANIFEST_VALIDITY:
            raise ValueError("M30 campaign validity cannot exceed 30 days")
        if (
            self.candidate.branch is not M30CandidateBranch.MAIN
            or self.candidate.dirty
            or self.candidate.annotated_release_tags != (f"v{self.candidate.package_version}",)
            or not self.candidate.contract_matches_head
            or not self.candidate.all_required_sources_committed
        ):
            raise ValueError("M30 campaign manifest requires a clean tagged main candidate")
        if self.contract_sha256 != self.candidate.contract_sha256:
            raise ValueError("M30 manifest contract and candidate contract differ")
        if self.supported_families != M30_SUPPORTED_FAMILIES:
            raise ValueError("M30 manifest supported families differ from the campaign contract")
        if self.unsupported_families != M30_UNSUPPORTED_FAMILIES:
            raise ValueError("M30 manifest unsupported families differ from the campaign contract")
        roles = tuple(item.role for item in self.owners)
        if (
            roles != tuple(M30CampaignOwnerRole)
            or len({item.authority_id for item in self.owners}) != 6
            or len({item.approval_key_fingerprint for item in self.owners}) != 6
        ):
            raise ValueError("M30 manifest must name each distinct owner and approval key once")
        observed_controls = {item.code: item.evidence_class for item in self.controls}
        expected_controls = {code: spec[0] for code, spec in M30_REQUIRED_CONTROL_SPECS.items()}
        if (
            tuple(item.code for item in self.controls) != tuple(sorted(expected_controls))
            or observed_controls != expected_controls
        ):
            raise ValueError("M30 manifest controls differ from the reviewed 24-control contract")
        owners_by_role = {item.role: item for item in self.owners}
        if any(
            item.responsible_authority
            != owners_by_role[
                M30_RESPONSIBLE_ROLE_BY_EVIDENCE_CLASS[item.evidence_class]
            ].authority_id
            for item in self.controls
        ):
            raise ValueError("M30 control responsibility differs from the reviewed owner policy")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.model_dump(mode="json")) + b"\n"

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class M30AuthenticatedManifest(FrozenDomainModel):
    manifest_sha256: str
    attestation_bundle_sha256: str
    verification_summary_sha256: str
    certificate_evidence_sha256: str
    trusted_timestamps_sha256: str
    trusted_timestamp_count: int = Field(strict=True, ge=1, le=16)
    source_repository: Literal["Crespillo95/schemabridge-codex-starter"]
    source_revision: str
    source_ref: str = Field(pattern=r"^refs/tags/v[0-9]+\.[0-9]+\.[0-9]+$")
    signer_workflow: Literal[
        "Crespillo95/schemabridge-codex-starter/.github/workflows/m30-manifest-attestation.yml"
    ]
    signer_digest: str
    oidc_issuer: Literal["https://token.actions.githubusercontent.com"]
    predicate_type: Literal["https://slsa.dev/provenance/v1"]
    github_cli_version: Literal["2.96.0"]
    github_cli_platform: Literal["linux-amd64", "linux-arm64", "macos-amd64", "macos-arm64"]
    github_cli_executable_sha256: str
    github_hosted_runner_required: Literal[True]
    trusted_timestamp_verified: Literal[True]
    verified_at: datetime

    @field_validator(
        "manifest_sha256",
        "attestation_bundle_sha256",
        "verification_summary_sha256",
        "certificate_evidence_sha256",
        "trusted_timestamps_sha256",
        "github_cli_executable_sha256",
    )
    @classmethod
    def manifest_digest_is_sha256(cls, value: str) -> str:
        return _require_sha256(value, "M30 authenticated manifest digest")

    @field_validator("source_revision", "signer_digest")
    @classmethod
    def source_identity_is_sha1(cls, value: str) -> str:
        if _SHA1.fullmatch(value) is None:
            raise ValueError("M30 attestation source identity must be a full lowercase SHA-1")
        return value

    @field_validator("verified_at")
    @classmethod
    def verification_time_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("M30 verification time must be timezone-aware UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def verifier_binary_matches_reviewed_platform(self) -> M30AuthenticatedManifest:
        if (
            self.github_cli_executable_sha256
            != M30_GITHUB_CLI_EXECUTABLE_SHA256[self.github_cli_platform]
        ):
            raise ValueError("M30 GitHub CLI executable digest differs from its reviewed platform")
        return self


class M30ManifestAuthenticationState(StrEnum):
    AUTHENTICATED = "authenticated"
    BLOCKED = "blocked"


class M30CampaignBlockReason(StrEnum):
    CANDIDATE_NOT_READY = "candidate_not_ready"
    MANIFEST_SUBJECT_MISMATCH = "manifest_subject_mismatch"
    MANIFEST_NOT_YET_VALID = "manifest_not_yet_valid"
    MANIFEST_EXPIRED = "manifest_expired"


class M30ManifestAuthenticationReport(FrozenDomainModel):
    schema_version: Literal[1]
    milestone: Literal["M30"]
    phase: Literal["1a"]
    preflight: M30ReadinessReport
    state: M30ManifestAuthenticationState
    blocking_reasons: tuple[M30CampaignBlockReason, ...] = Field(max_length=4)
    campaign_id: str | None = Field(default=None, min_length=8, max_length=160)
    manifest_sha256: str | None = None
    authentication: M30AuthenticatedManifest | None = None
    campaign_executable: Literal[False]
    release_decision: Literal[M30ReleaseDecision.NO_GO]
    workflow_attested_manifest_authenticated: bool
    external_controls_passed: Literal[0]
    external_controls_remaining: Literal[24]
    synthetic_evidence_accepted_as_operated: Literal[False]
    database_calls: Literal[0]
    datahub_writes: Literal[0]
    source_writes: Literal[0]

    @field_validator("manifest_sha256")
    @classmethod
    def optional_manifest_digest_is_sha256(cls, value: str | None) -> str | None:
        return None if value is None else _require_sha256(value, "M30 report manifest digest")

    @model_validator(mode="after")
    def authentication_state_matches_evidence(self) -> M30ManifestAuthenticationReport:
        authenticated = self.state is M30ManifestAuthenticationState.AUTHENTICATED
        if self.workflow_attested_manifest_authenticated != authenticated:
            raise ValueError("M30 manifest authentication differs from report state")
        if authenticated:
            if (
                self.blocking_reasons
                or self.campaign_id is None
                or self.manifest_sha256 is None
                or self.authentication is None
                or self.authentication.manifest_sha256 != self.manifest_sha256
                or self.authentication.source_revision != self.preflight.candidate.revision
            ):
                raise ValueError("authenticated M30 campaign report lacks exact evidence")
        elif (
            not self.blocking_reasons
            or self.authentication is not None
            or self.workflow_attested_manifest_authenticated
        ):
            raise ValueError("blocked M30 campaign report has inconsistent evidence")
        return self

    def fingerprint(self) -> str:
        return hashlib.sha256(_canonical_json_bytes(self.model_dump(mode="json"))).hexdigest()


def manifest_matches_contract(
    manifest: M30CampaignManifest,
    contract: M30CampaignContract,
) -> bool:
    """Return exact contract equality without treating a signature as control evidence."""

    return (
        manifest.contract_sha256 == contract.fingerprint()
        and manifest.candidate_sku == contract.candidate_sku
        and manifest.supported_families == contract.supported_families
        and manifest.unsupported_families == contract.unsupported_families
        and {item.code: item.evidence_class for item in manifest.controls}
        == {item.code: item.evidence_class for item in contract.required_controls}
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


__all__ = [
    "M30_ADVANCED_FAMILIES",
    "M30_GITHUB_ATTESTATION_PREDICATE",
    "M30_GITHUB_CLI_EXECUTABLE_SHA256",
    "M30_GITHUB_CLI_VERSION",
    "M30_GITHUB_OIDC_ISSUER",
    "M30_GITHUB_REPOSITORY",
    "M30_GITHUB_SIGNER_WORKFLOW",
    "M30_MANIFEST_KIND",
    "M30_RESPONSIBLE_ROLE_BY_EVIDENCE_CLASS",
    "M30_SIMPLE_FAMILIES",
    "M30AuthenticatedManifest",
    "M30BrowserVersion",
    "M30CampaignArtifactSet",
    "M30CampaignBlockReason",
    "M30CampaignControlAssignment",
    "M30CampaignCorpusFreeze",
    "M30CampaignCorpusSlice",
    "M30CampaignLanguage",
    "M30CampaignManifest",
    "M30CampaignOwner",
    "M30CampaignOwnerRole",
    "M30CampaignProviderFreeze",
    "M30CampaignRisk",
    "M30CampaignTargetFreeze",
    "M30ManifestAuthenticationReport",
    "M30ManifestAuthenticationState",
    "manifest_matches_contract",
]
