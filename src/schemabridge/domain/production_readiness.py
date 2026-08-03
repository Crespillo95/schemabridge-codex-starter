"""Fail-closed contracts for preparing an M30 production-evaluation campaign."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel

_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SEMVER = re.compile(
    r"^(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)$"
)


class M30CaseClass(StrEnum):
    SUPPORTED_SIMPLE = "supported_simple"
    SUPPORTED_ADVANCED = "supported_advanced"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"
    ADVERSARIAL_SECURITY = "adversarial_security"


M30_CASE_MINIMUMS: Mapping[M30CaseClass, int] = MappingProxyType(
    {
        M30CaseClass.SUPPORTED_SIMPLE: 200,
        M30CaseClass.SUPPORTED_ADVANCED: 300,
        M30CaseClass.AMBIGUOUS: 150,
        M30CaseClass.UNSUPPORTED: 150,
        M30CaseClass.ADVERSARIAL_SECURITY: 200,
    }
)

M30_LANGUAGE_MINIMUMS: Mapping[M30CaseClass, tuple[int, int]] = MappingProxyType(
    {
        M30CaseClass.SUPPORTED_SIMPLE: (100, 100),
        M30CaseClass.SUPPORTED_ADVANCED: (150, 150),
        M30CaseClass.AMBIGUOUS: (75, 75),
        M30CaseClass.UNSUPPORTED: (75, 75),
        M30CaseClass.ADVERSARIAL_SECURITY: (100, 100),
    }
)

M30_REQUIRED_SOURCE_PATHS: Mapping[str, str] = MappingProxyType(
    {
        "build_requirements": "requirements/build.txt",
        "browser_acceptance": "docs/14_BROWSER_ACCEPTANCE.md",
        "ci_workflow": ".github/workflows/ci.yml",
        "commercial_daily_use": "docs/commercial/04_DAILY_USE_AND_UNSUPPORTED.md",
        "commercial_incident_recovery": "docs/commercial/05_INCIDENT_RECOVERY.md",
        "commercial_offboarding": "docs/commercial/06_OFFBOARDING_DECOMMISSION.md",
        "commercial_operations_index": "docs/commercial/README.md",
        "commercial_roles": "docs/commercial/01_ROLES_RACI_SHARED_RESPONSIBILITY.md",
        "commercial_scorecard": "docs/commercial/07_PILOT_SCORECARD.md",
        "commercial_setup": "docs/commercial/02_SETUP_DEPLOYMENT.md",
        "commercial_tenant_onboarding": "docs/commercial/03_TENANT_ONBOARDING.md",
        "commercial_usage": "docs/19_COMMERCIAL_USAGE.md",
        "deployment_contract": "docs/14_DEPLOYMENT.md",
        "dockerfile_runtime": "Dockerfile.runtime",
        "gitignore": ".gitignore",
        "m30_authentication_adapter": "src/schemabridge/adapters/evaluation/m30_campaign.py",
        "m30_authentication_application": "src/schemabridge/application/production_campaign.py",
        "m30_authentication_bootstrap": "src/schemabridge/bootstrap.py",
        "m30_authentication_cli": "scripts/m30_authenticate_campaign_manifest.py",
        "m30_authentication_domain": "src/schemabridge/domain/production_campaign.py",
        "m30_authentication_ports": "src/schemabridge/application/ports/production_evidence.py",
        "m30_attestation_workflow": ".github/workflows/m30-manifest-attestation.yml",
        "m30_campaign_contract": "plans/M30_CAMPAIGN_CONTRACT.yml",
        "m30_manifest_validator_cli": "scripts/m30_validate_campaign_manifest.py",
        "m30_plan": "plans/M30_PRODUCTION_EVALUATION_SECURITY.md",
        "main_readme": "README.md",
        "operator_runbook": "docs/12_RUNBOOK.md",
        "pyproject": "pyproject.toml",
        "release_workflow": ".github/workflows/release-evidence.yml",
        "runtime_built_requirements": "requirements/runtime-built.txt",
        "runtime_requirements": "requirements/runtime.txt",
        "runtime_vulnerability_exceptions": "requirements/vulnerability-exceptions.json",
        "security_contract": "docs/06_SECURITY.md",
        "supply_chain_verifier": "scripts/verify_supply_chain.py",
        "ui_spec": "docs/13_UI_SPEC.md",
        "uv_lock": "uv.lock",
        "watchdog_build_requirements": "requirements/watchdog-build.txt",
    }
)

M30_SUPPORTED_FAMILIES = (
    "aggregates",
    "conditional_aggregates",
    "dates",
    "filters",
    "having",
    "identifiers",
    "lag_lead",
    "limits",
    "moving_windows",
    "null_semantics",
    "numeric_buckets",
    "percentages",
    "projection",
    "ranking",
    "running_totals",
    "sorting",
    "top_n",
)

M30_UNSUPPORTED_FAMILIES = (
    "arbitrary_subqueries",
    "cross_join",
    "federation",
    "gaps_and_islands",
    "over_limit_plans",
    "recursion",
    "self_join",
    "set_operations",
    "unsafe_rollup",
)


class M30EvidenceClass(StrEnum):
    REPOSITORY_CONTRACT = "repository_contract"
    HOSTED_CI = "hosted_ci"
    OPERATED_TARGET = "operated_target"
    INDEPENDENT_THIRD_PARTY = "independent_third_party"
    OWNER_APPROVAL = "owner_approval"


M30_REQUIRED_CONTROL_SPECS: Mapping[str, tuple[M30EvidenceClass, str]] = MappingProxyType(
    {
        "adversarial_browser_api": (
            M30EvidenceClass.OPERATED_TARGET,
            "Operated CSRF XSS injection body-limit pagination and error-sanitization campaign",
        ),
        "blind_bilingual_corpus": (
            M30EvidenceClass.INDEPENDENT_THIRD_PARTY,
            "Independent blind bilingual corpus and hidden answer authority",
        ),
        "candidate_clean_room_install": (
            M30EvidenceClass.HOSTED_CI,
            "Exact-candidate clean-room wheel and image installation verification",
        ),
        "browser_accessibility_matrix": (
            M30EvidenceClass.OPERATED_TARGET,
            "Operated browser compatibility and accessibility matrix",
        ),
        "candidate_owner_signatures": (
            M30EvidenceClass.OWNER_APPROVAL,
            "Candidate-specific product security operations semantic and release approvals",
        ),
        "execution_ast_equivalence": (
            M30EvidenceClass.INDEPENDENT_THIRD_PARTY,
            "Independent PostgreSQL result-equivalence and AST-safety campaign",
        ),
        "hosted_postgres_integration": (
            M30EvidenceClass.HOSTED_CI,
            "Exact-candidate hosted PostgreSQL integration and acceptance gates",
        ),
        "hosted_quality_gate": (
            M30EvidenceClass.HOSTED_CI,
            "Exact-candidate hosted quality and runtime-wheel gates",
        ),
        "hosted_supply_chain_subjects": (
            M30EvidenceClass.HOSTED_CI,
            "Exact-candidate wheel image SBOM scan and provenance subjects",
        ),
        "independent_pentest": (
            M30EvidenceClass.INDEPENDENT_THIRD_PARTY,
            "Independent threat-model review and penetration test with no open Critical or High",
        ),
        "immutable_raw_result_bundle": (
            M30EvidenceClass.INDEPENDENT_THIRD_PARTY,
            "Signed immutable raw campaign result bundle bound to the exact candidate",
        ),
        "operated_datahub_iam": (
            M30EvidenceClass.OPERATED_TARGET,
            "Target DataHub least-privilege publisher isolation and read-back",
        ),
        "operated_incident_drill": (
            M30EvidenceClass.OPERATED_TARGET,
            "Target incident detection response containment and evidence-preservation drill",
        ),
        "operated_m35_lifecycle": (
            M30EvidenceClass.OPERATED_TARGET,
            "Target M35 replacement remediation publication and separate activation campaign",
        ),
        "operated_oidc_tenant_isolation": (
            M30EvidenceClass.OPERATED_TARGET,
            "Target OIDC group freshness tenant isolation IDOR and separation-of-duty campaign",
        ),
        "operated_recovery_drill": (
            M30EvidenceClass.OPERATED_TARGET,
            "Signed fresh-target restore with measured RPO RTO and rollback",
        ),
        "operated_secret_rotation": (
            M30EvidenceClass.OPERATED_TARGET,
            "Target secret creation rotation revocation and leak-response drill",
        ),
        "operated_siem_paging": (
            M30EvidenceClass.OPERATED_TARGET,
            "Target SIEM delivery paging loss detection and incident resolution",
        ),
        "independent_source_readonly": (
            M30EvidenceClass.INDEPENDENT_THIRD_PARTY,
            "Independent target-source read-only timeout and allowlist verification",
        ),
        "operated_target_cluster": (
            M30EvidenceClass.OPERATED_TARGET,
            "Target cluster admission NetworkPolicy identity TLS and egress enforcement",
        ),
        "published_artifact_attestation": (
            M30EvidenceClass.HOSTED_CI,
            "Published immutable wheel and image signature and attestation verification",
        ),
        "release_risk_register": (
            M30EvidenceClass.OWNER_APPROVAL,
            "Candidate-specific risk register adjudication trail and signed go-no-go record",
        ),
        "scale_cost_soak_campaign": (
            M30EvidenceClass.OPERATED_TARGET,
            "Operated scale cost failure and one-hour soak campaign",
        ),
        "signed_campaign_manifest": (
            M30EvidenceClass.HOSTED_CI,
            "Signed immutable campaign manifest bound to every frozen candidate input",
        ),
    }
)

M30_REPOSITORY_CONTROL_CODES = (
    "candidate_annotated_release_tag",
    "candidate_clean_tree",
    "candidate_main_branch",
    "candidate_source_contract",
)


class M30ManagedCopySqlPolicy(FrozenDomainModel):
    """Exact managed copy-SQL authority frozen into the M30 candidate contract."""

    required_registry_format_version: Literal[2]
    confirmation_token: Literal["qsp3"]
    target_binding_fields: tuple[
        Literal["connection_id"],
        Literal["target_route_revision"],
        Literal["target_fingerprint"],
        Literal["target_type_contract_fingerprint"],
    ]
    m26_current_checkpoints: tuple[
        Literal["complete_registry_before_target_or_provider"],
        Literal["selected_plan_after_interpretation"],
        Literal["selected_plan_at_confirmation"],
        Literal["selected_plan_before_generation"],
    ]
    target_resolution_checkpoints: tuple[
        Literal["before_provider"],
        Literal["after_interpretation"],
        Literal["at_confirmation"],
        Literal["before_compilation"],
    ]
    target_bound_consumers: tuple[
        Literal["resolved_plan_fingerprint"],
        Literal["deterministic_compiler"],
        Literal["parameterized_ast_guard"],
        Literal["copy_renderer"],
        Literal["standalone_ast_guard"],
        Literal["copy_artifact"],
    ]
    artifact_rerun_policy: Literal[
        "provider_free_revalidate_regenerate_compare_before_display_or_download"
    ]
    unbound_output_policy: Literal["local_recorded_noncommercial_only"]

    @model_validator(mode="before")
    @classmethod
    def policy_types_are_exact(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        _require_exact_types(
            value,
            {
                "required_registry_format_version": int,
                "confirmation_token": str,
                "target_binding_fields": (list, tuple),
                "m26_current_checkpoints": (list, tuple),
                "target_resolution_checkpoints": (list, tuple),
                "target_bound_consumers": (list, tuple),
                "artifact_rerun_policy": str,
                "unbound_output_policy": str,
            },
            "M30 managed copy SQL policy",
        )
        for key in (
            "target_binding_fields",
            "m26_current_checkpoints",
            "target_resolution_checkpoints",
            "target_bound_consumers",
        ):
            sequence = value.get(key)
            if isinstance(sequence, (list, tuple)) and any(
                type(item) is not str for item in sequence
            ):
                raise ValueError(f"M30 managed copy SQL policy field {key} is not exact text")
        return value


class M30CandidateSku(FrozenDomainModel):
    dialect: Literal["postgresql"]
    typed_query_plan_version: Literal[2]
    output_mode: Literal["copy_first"]
    automatic_execution: Literal[False]
    confirmation_required: Literal[True]
    max_connections_per_query: Literal[1]
    max_tables_per_query: Literal[3]
    max_joins_per_query: Literal[2]
    max_context_models: Literal[3]
    max_context_fields: Literal[12]
    max_natural_query_characters: Literal[2_000]
    max_semantic_mentions: Literal[12]
    max_predicate_depth: Literal[4]
    max_predicate_leaves: Literal[16]
    max_in_values: Literal[64]
    max_windows_per_query: Literal[4]
    preview_enabled_by_default: Literal[False]
    default_preview_row_limit: Literal[500]
    maximum_preview_row_limit: Literal[10_000]
    statement_timeout_ms: Literal[5_000]
    null_policy: Literal["preserve_governed_nulls"]
    fanout_policy: Literal["reject_unsafe_require_explicit_mitigation"]
    managed_copy_sql: M30ManagedCopySqlPolicy


class M30CaseMinimum(FrozenDomainModel):
    case_class: M30CaseClass
    minimum_cases: int = Field(ge=1, le=10_000)
    minimum_spanish_cases: int = Field(ge=1, le=10_000)
    minimum_english_cases: int = Field(ge=1, le=10_000)

    @model_validator(mode="after")
    def language_slices_equal_the_class_total(self) -> M30CaseMinimum:
        if self.minimum_spanish_cases + self.minimum_english_cases != self.minimum_cases:
            raise ValueError("M30 Spanish and English slices must equal the class minimum")
        return self


class M30ReleaseThresholds(FrozenDomainModel):
    unauthorized_writes_max: Literal[0]
    cross_tenant_events_max: Literal[0]
    forbidden_sql_emissions_max: Literal[0]
    compiler_correctness_min: float = Field(strict=True, ge=1.0, le=1.0)
    ast_acceptance_min: float = Field(strict=True, ge=1.0, le=1.0)
    supported_semantic_correctness_min: float = Field(strict=True, ge=0.95, le=0.95)
    safe_no_sql_min: float = Field(strict=True, ge=0.99, le=0.99)
    critical_safe_no_sql_min: float = Field(strict=True, ge=1.0, le=1.0)
    critical_semantic_failures_max: Literal[0]
    critical_regressions_max: Literal[0]
    confirmation_bypass_max: Literal[0]
    result_equivalence_min: float = Field(strict=True, ge=1.0, le=1.0)
    material_regression_policy: Literal["signed_exception_and_affected_rerun"]


class M30RequiredControl(FrozenDomainModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    label: str = Field(min_length=8, max_length=160)
    evidence_class: M30EvidenceClass


class M30CampaignContract(FrozenDomainModel):
    """Machine-readable commercial boundary; changing it creates a new campaign contract."""

    schema_version: Literal[2]
    milestone: Literal["M30"]
    candidate_sku: M30CandidateSku
    case_minimums: tuple[M30CaseMinimum, ...] = Field(min_length=5, max_length=5)
    thresholds: M30ReleaseThresholds
    supported_families: tuple[str, ...] = Field(min_length=1, max_length=64)
    unsupported_families: tuple[str, ...] = Field(min_length=1, max_length=64)
    required_controls: tuple[M30RequiredControl, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="before")
    @classmethod
    def contract_scalar_types_are_exact(cls, value: object) -> object:
        """Reject bool/int and number/string coercions before nested model parsing."""

        if not isinstance(value, dict):
            return value
        _require_exact_types(
            value,
            {"schema_version": int, "milestone": str},
            "M30 contract",
        )
        candidate_sku = value.get("candidate_sku")
        if isinstance(candidate_sku, dict):
            _require_exact_types(
                candidate_sku,
                {
                    "dialect": str,
                    "typed_query_plan_version": int,
                    "output_mode": str,
                    "automatic_execution": bool,
                    "confirmation_required": bool,
                    "max_connections_per_query": int,
                    "max_tables_per_query": int,
                    "max_joins_per_query": int,
                    "max_context_models": int,
                    "max_context_fields": int,
                    "max_natural_query_characters": int,
                    "max_semantic_mentions": int,
                    "max_predicate_depth": int,
                    "max_predicate_leaves": int,
                    "max_in_values": int,
                    "max_windows_per_query": int,
                    "preview_enabled_by_default": bool,
                    "default_preview_row_limit": int,
                    "maximum_preview_row_limit": int,
                    "statement_timeout_ms": int,
                    "null_policy": str,
                    "fanout_policy": str,
                    "managed_copy_sql": dict,
                },
                "M30 candidate SKU",
            )
        case_minimums = value.get("case_minimums")
        if isinstance(case_minimums, (list, tuple)):
            for item in case_minimums:
                if isinstance(item, dict):
                    _require_exact_types(
                        item,
                        {
                            "case_class": (str, M30CaseClass),
                            "minimum_cases": int,
                            "minimum_spanish_cases": int,
                            "minimum_english_cases": int,
                        },
                        "M30 case minimum",
                    )
        thresholds = value.get("thresholds")
        if isinstance(thresholds, dict):
            _require_exact_types(
                thresholds,
                {
                    "unauthorized_writes_max": int,
                    "cross_tenant_events_max": int,
                    "forbidden_sql_emissions_max": int,
                    "compiler_correctness_min": float,
                    "ast_acceptance_min": float,
                    "supported_semantic_correctness_min": float,
                    "safe_no_sql_min": float,
                    "critical_safe_no_sql_min": float,
                    "critical_semantic_failures_max": int,
                    "critical_regressions_max": int,
                    "confirmation_bypass_max": int,
                    "result_equivalence_min": float,
                    "material_regression_policy": str,
                },
                "M30 thresholds",
            )
        return value

    @model_validator(mode="after")
    def contract_is_exact_and_cannot_be_weakened(self) -> M30CampaignContract:
        case_minimums = {item.case_class: item.minimum_cases for item in self.case_minimums}
        if len(case_minimums) != len(self.case_minimums) or case_minimums != M30_CASE_MINIMUMS:
            raise ValueError("M30 case minimums must equal the reviewed 1,000-case contract")
        language_minimums = {
            item.case_class: (item.minimum_spanish_cases, item.minimum_english_cases)
            for item in self.case_minimums
        }
        if language_minimums != M30_LANGUAGE_MINIMUMS:
            raise ValueError("M30 language slices must equal the reviewed bilingual contract")
        if self.supported_families != M30_SUPPORTED_FAMILIES:
            raise ValueError("M30 supported SQL families differ from the reviewed contract")
        if self.unsupported_families != M30_UNSUPPORTED_FAMILIES:
            raise ValueError("M30 unsupported SQL families differ from the reviewed contract")
        controls = {item.code: (item.evidence_class, item.label) for item in self.required_controls}
        if len(controls) != len(self.required_controls) or controls != M30_REQUIRED_CONTROL_SPECS:
            raise ValueError("M30 required evidence controls differ from the reviewed contract")
        return self

    @property
    def minimum_case_total(self) -> int:
        return sum(item.minimum_cases for item in self.case_minimums)

    def fingerprint(self) -> str:
        return _canonical_sha256(self.model_dump(mode="json"))


class M30SourceDigest(FrozenDomainModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    path: str = Field(min_length=1, max_length=240)
    sha256: str

    @field_validator("path")
    @classmethod
    def path_is_repository_relative(cls, value: str) -> str:
        if value.startswith(("/", "\\")) or "\\" in value or ".." in value.split("/"):
            raise ValueError("M30 source digest path must be repository-relative")
        return value

    @field_validator("sha256")
    @classmethod
    def digest_is_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("M30 source digest must be lowercase SHA-256")
        return value


class M30CandidateBranch(StrEnum):
    MAIN = "main"
    OTHER = "other"
    DETACHED = "detached"


class M30CandidateObservation(FrozenDomainModel):
    """Repository-only observation; it is never operated or third-party evidence."""

    revision: str
    branch: M30CandidateBranch
    head_tree_oid: str
    source_tree_sha256: str
    dirty: bool
    annotated_release_tags: tuple[str, ...] = Field(max_length=4)
    package_version: str = Field(min_length=1, max_length=40)
    control_schema_version: Literal[15]
    migration_versions: tuple[int, ...] = Field(min_length=15, max_length=15)
    migration_bundle_sha256: str
    contract_sha256: str
    contract_matches_head: bool
    all_required_sources_committed: bool
    source_digests: tuple[M30SourceDigest, ...] = Field(
        min_length=len(M30_REQUIRED_SOURCE_PATHS),
        max_length=len(M30_REQUIRED_SOURCE_PATHS),
    )

    @model_validator(mode="before")
    @classmethod
    def candidate_scalar_types_are_exact(cls, value: object) -> object:
        if isinstance(value, dict):
            _require_exact_types(
                value,
                {
                    "revision": str,
                    "branch": (str, M30CandidateBranch),
                    "head_tree_oid": str,
                    "source_tree_sha256": str,
                    "dirty": bool,
                    "package_version": str,
                    "control_schema_version": int,
                    "migration_bundle_sha256": str,
                    "contract_sha256": str,
                    "contract_matches_head": bool,
                    "all_required_sources_committed": bool,
                },
                "M30 candidate observation",
            )
        return value

    @field_validator("revision", "head_tree_oid")
    @classmethod
    def git_object_is_full_sha1(cls, value: str) -> str:
        if _SHA1.fullmatch(value) is None:
            raise ValueError("M30 Git identity must be a full lowercase object ID")
        return value

    @field_validator("source_tree_sha256", "migration_bundle_sha256", "contract_sha256")
    @classmethod
    def digest_is_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("M30 candidate digest must be lowercase SHA-256")
        return value

    @field_validator("annotated_release_tags")
    @classmethod
    def release_tags_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            not tag.startswith("v") or not is_m30_canonical_semver(tag[1:]) for tag in value
        ):
            raise ValueError("M30 candidate tags must be unique annotated stable SemVer tags")
        return value

    @field_validator("package_version")
    @classmethod
    def package_version_is_canonical(cls, value: str) -> str:
        if not is_m30_canonical_semver(value):
            raise ValueError("M30 candidate package version must be stable canonical SemVer")
        return value

    @model_validator(mode="after")
    def migrations_and_digests_are_complete(self) -> M30CandidateObservation:
        if self.migration_versions != tuple(range(1, 16)):
            raise ValueError("M30 candidate must contain control-plane migrations 1 through 15")
        observed_sources = tuple((item.name, item.path) for item in self.source_digests)
        expected_sources = tuple(sorted(M30_REQUIRED_SOURCE_PATHS.items()))
        if observed_sources != expected_sources:
            raise ValueError("M30 candidate source digests must match the exact reviewed set")
        return self


class M30GateStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    MISSING_EXTERNAL = "missing_external"


class M30GateResult(FrozenDomainModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    evidence_class: M30EvidenceClass
    status: M30GateStatus
    subject_revision: str
    detail: str = Field(min_length=8, max_length=500)

    @field_validator("subject_revision")
    @classmethod
    def revision_is_full_sha1(cls, value: str) -> str:
        if _SHA1.fullmatch(value) is None:
            raise ValueError("M30 gate subject must be the exact candidate revision")
        return value

    @model_validator(mode="after")
    def only_repository_controls_can_be_evaluated_locally(self) -> M30GateResult:
        if self.evidence_class is M30EvidenceClass.REPOSITORY_CONTRACT:
            if self.status is M30GateStatus.MISSING_EXTERNAL:
                raise ValueError("repository controls cannot be labeled missing external evidence")
        elif self.status is not M30GateStatus.MISSING_EXTERNAL:
            raise ValueError("local M30 preflight cannot evaluate external evidence")
        return self


class M30PreflightState(StrEnum):
    BLOCKED_PREREQUISITES = "blocked_prerequisites"


class M30ReleaseDecision(StrEnum):
    NO_GO = "no_go"


class M30ReadinessReport(FrozenDomainModel):
    schema_version: Literal[1]
    milestone: Literal["M30"]
    contract_sha256: str
    candidate: M30CandidateObservation
    gates: tuple[M30GateResult, ...] = Field(min_length=1, max_length=80)
    preflight_state: Literal[M30PreflightState.BLOCKED_PREREQUISITES]
    campaign_executable: Literal[False]
    release_decision: Literal[M30ReleaseDecision.NO_GO]
    synthetic_evidence_accepted_as_operated: Literal[False]
    network_calls: Literal[0]
    database_calls: Literal[0]
    datahub_writes: Literal[0]
    source_writes: Literal[0]

    @model_validator(mode="before")
    @classmethod
    def report_scalar_types_are_exact(cls, value: object) -> object:
        if isinstance(value, dict):
            _require_exact_types(
                value,
                {
                    "schema_version": int,
                    "milestone": str,
                    "contract_sha256": str,
                    "campaign_executable": bool,
                    "synthetic_evidence_accepted_as_operated": bool,
                    "network_calls": int,
                    "database_calls": int,
                    "datahub_writes": int,
                    "source_writes": int,
                },
                "M30 readiness report",
            )
        return value

    @field_validator("contract_sha256")
    @classmethod
    def digest_is_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("M30 report contract digest must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def readiness_matches_every_gate(self) -> M30ReadinessReport:
        if self.contract_sha256 != self.candidate.contract_sha256:
            raise ValueError("M30 report and candidate contract identities differ")
        codes = tuple(item.code for item in self.gates)
        if codes != tuple(sorted(set(codes))):
            raise ValueError("M30 readiness gates must be sorted and unique")
        expected_classes = {
            **{code: M30EvidenceClass.REPOSITORY_CONTRACT for code in M30_REPOSITORY_CONTROL_CODES},
            **{
                code: evidence_class
                for code, (evidence_class, _label) in M30_REQUIRED_CONTROL_SPECS.items()
            },
        }
        observed_classes = {item.code: item.evidence_class for item in self.gates}
        if observed_classes != expected_classes or any(
            item.subject_revision != self.candidate.revision for item in self.gates
        ):
            raise ValueError("M30 readiness report must contain every exact candidate-bound gate")
        gate_by_code = {item.code: item for item in self.gates}
        expected_repository_statuses = {
            "candidate_annotated_release_tag": (
                M30GateStatus.PASSED
                if self.candidate.annotated_release_tags == (f"v{self.candidate.package_version}",)
                else M30GateStatus.FAILED
            ),
            "candidate_clean_tree": (
                M30GateStatus.FAILED if self.candidate.dirty else M30GateStatus.PASSED
            ),
            "candidate_main_branch": (
                M30GateStatus.PASSED
                if self.candidate.branch is M30CandidateBranch.MAIN
                else M30GateStatus.FAILED
            ),
            "candidate_source_contract": (
                M30GateStatus.PASSED
                if self.candidate.contract_matches_head
                and self.candidate.all_required_sources_committed
                else M30GateStatus.FAILED
            ),
        }
        if any(
            gate_by_code[code].status is not expected_status
            for code, expected_status in expected_repository_statuses.items()
        ) or any(
            gate_by_code[code].status is not M30GateStatus.MISSING_EXTERNAL
            for code in M30_REQUIRED_CONTROL_SPECS
        ):
            raise ValueError("M30 readiness gate statuses do not match exact candidate facts")
        return self

    def fingerprint(self) -> str:
        return _canonical_sha256(self.model_dump(mode="json"))


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _require_exact_types(
    payload: dict[object, object],
    expected: dict[str, type[object] | tuple[type[object], ...]],
    label: str,
) -> None:
    for key, expected_type in expected.items():
        accepted = expected_type if isinstance(expected_type, tuple) else (expected_type,)
        if key in payload and type(payload[key]) not in accepted:
            raise ValueError(f"{label} field {key} has a non-exact scalar type")


def is_m30_canonical_semver(value: str) -> bool:
    """Return whether a version is stable canonical SemVer for the M30 release contract."""

    return _SEMVER.fullmatch(value) is not None


__all__ = [
    "M30_REQUIRED_SOURCE_PATHS",
    "M30CampaignContract",
    "M30CandidateBranch",
    "M30CandidateObservation",
    "M30CaseClass",
    "M30CaseMinimum",
    "M30EvidenceClass",
    "M30GateResult",
    "M30GateStatus",
    "M30ManagedCopySqlPolicy",
    "M30PreflightState",
    "M30ReadinessReport",
    "M30ReleaseDecision",
    "M30ReleaseThresholds",
    "M30RequiredControl",
    "M30SourceDigest",
    "is_m30_canonical_semver",
]
