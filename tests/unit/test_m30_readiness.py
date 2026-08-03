"""M30 candidate contract, identity, and fail-closed readiness regressions."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from scripts.m30_readiness import main
from tests.m30_readiness_support import ROOT, build_candidate_repository

import schemabridge.adapters.evaluation.m30_readiness as m30_adapter
from schemabridge.adapters.evaluation.m30_readiness import (
    FileM30CampaignContract,
    FileM30ReadinessReportWriter,
    GitM30CandidateIdentity,
)
from schemabridge.application.ports.production_evidence import ProductionEvidenceError
from schemabridge.application.production_readiness import AssessM30Readiness
from schemabridge.domain.production_readiness import (
    M30_CASE_MINIMUMS,
    M30_REQUIRED_CONTROL_SPECS,
    M30_REQUIRED_SOURCE_PATHS,
    M30CampaignContract,
    M30CandidateBranch,
    M30CaseClass,
    M30EvidenceClass,
    M30GateResult,
    M30GateStatus,
    M30PreflightState,
    M30ReadinessReport,
    M30ReleaseDecision,
)


def test_reviewed_m30_policy_constants_are_immutable() -> None:
    with pytest.raises(TypeError):
        M30_CASE_MINIMUMS[M30CaseClass.SUPPORTED_SIMPLE] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        M30_REQUIRED_CONTROL_SPECS["independent_pentest"] = (  # type: ignore[index]
            M30EvidenceClass.REPOSITORY_CONTRACT,
            "weakened",
        )
    with pytest.raises(TypeError):
        M30_REQUIRED_SOURCE_PATHS["pyproject"] = "different.toml"  # type: ignore[index]


def test_machine_readable_contract_is_the_exact_reviewed_m30_boundary() -> None:
    contract = FileM30CampaignContract(ROOT).load()

    assert contract.schema_version == 2
    assert contract.minimum_case_total == 1_000
    assert contract.candidate_sku.dialect == "postgresql"
    assert contract.candidate_sku.typed_query_plan_version == 2
    assert contract.candidate_sku.output_mode == "copy_first"
    assert contract.candidate_sku.automatic_execution is False
    assert contract.candidate_sku.max_connections_per_query == 1
    assert contract.candidate_sku.max_tables_per_query == 3
    assert contract.candidate_sku.max_joins_per_query == 2
    assert contract.candidate_sku.preview_enabled_by_default is False
    assert contract.candidate_sku.default_preview_row_limit == 500
    assert contract.candidate_sku.maximum_preview_row_limit == 10_000
    assert contract.candidate_sku.max_natural_query_characters == 2_000
    assert contract.candidate_sku.max_semantic_mentions == 12
    assert contract.candidate_sku.max_predicate_depth == 4
    assert contract.candidate_sku.max_predicate_leaves == 16
    assert contract.candidate_sku.max_in_values == 64
    assert contract.candidate_sku.max_windows_per_query == 4
    assert contract.candidate_sku.statement_timeout_ms == 5_000
    assert contract.candidate_sku.null_policy == "preserve_governed_nulls"
    assert contract.candidate_sku.fanout_policy == "reject_unsafe_require_explicit_mitigation"
    managed = contract.candidate_sku.managed_copy_sql
    assert managed.required_registry_format_version == 2
    assert managed.confirmation_token == "qsp3"
    assert managed.target_binding_fields == (
        "connection_id",
        "target_route_revision",
        "target_fingerprint",
        "target_type_contract_fingerprint",
    )
    assert managed.m26_current_checkpoints == (
        "complete_registry_before_target_or_provider",
        "selected_plan_after_interpretation",
        "selected_plan_at_confirmation",
        "selected_plan_before_generation",
    )
    assert managed.target_resolution_checkpoints == (
        "before_provider",
        "after_interpretation",
        "at_confirmation",
        "before_compilation",
    )
    assert managed.target_bound_consumers == (
        "resolved_plan_fingerprint",
        "deterministic_compiler",
        "parameterized_ast_guard",
        "copy_renderer",
        "standalone_ast_guard",
        "copy_artifact",
    )
    assert (
        managed.artifact_rerun_policy
        == "provider_free_revalidate_regenerate_compare_before_display_or_download"
    )
    assert managed.unbound_output_policy == "local_recorded_noncommercial_only"
    assert sum(item.minimum_spanish_cases for item in contract.case_minimums) == 500
    assert sum(item.minimum_english_cases for item in contract.case_minimums) == 500
    assert contract.thresholds.critical_semantic_failures_max == 0
    assert contract.thresholds.critical_regressions_max == 0
    assert len(contract.required_controls) == 24
    assert len(contract.fingerprint()) == 64


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("case_minimums", 0, "minimum_cases"), 199),
        (("thresholds", "compiler_correctness_min"), 0.99),
        (("candidate_sku", "max_tables_per_query"), 4),
        (("candidate_sku", "automatic_execution"), True),
        (("candidate_sku", "automatic_execution"), 0),
        (("candidate_sku", "confirmation_required"), 1),
        (("candidate_sku", "max_connections_per_query"), True),
        (("candidate_sku", "max_windows_per_query"), 5),
        (("case_minimums", 0, "minimum_spanish_cases"), 99),
        (("thresholds", "critical_semantic_failures_max"), 1),
        (("thresholds", "critical_regressions_max"), 1),
        (("candidate_sku", "preview_enabled_by_default"), True),
        (("candidate_sku", "maximum_preview_row_limit"), 10_001),
        (("schema_version",), 1),
        (("candidate_sku", "managed_copy_sql", "required_registry_format_version"), 1),
        (("candidate_sku", "managed_copy_sql", "required_registry_format_version"), True),
        (("candidate_sku", "managed_copy_sql", "confirmation_token"), "qsp2"),
        (
            ("candidate_sku", "managed_copy_sql", "target_binding_fields"),
            (
                "target_fingerprint",
                "connection_id",
                "target_route_revision",
                "target_type_contract_fingerprint",
            ),
        ),
        (
            ("candidate_sku", "managed_copy_sql", "m26_current_checkpoints"),
            ("complete_registry_before_target_or_provider",),
        ),
        (
            ("candidate_sku", "managed_copy_sql", "target_resolution_checkpoints"),
            ("before_provider", "after_interpretation", "at_confirmation"),
        ),
        (
            ("candidate_sku", "managed_copy_sql", "target_bound_consumers"),
            (
                "resolved_plan_fingerprint",
                "deterministic_compiler",
                "parameterized_ast_guard",
                "copy_renderer",
                "copy_artifact",
            ),
        ),
        (
            ("candidate_sku", "managed_copy_sql", "artifact_rerun_policy"),
            "trust_cached_artifact",
        ),
        (
            ("candidate_sku", "managed_copy_sql", "unbound_output_policy"),
            "tenant_facing_allowed",
        ),
    ),
)
def test_contract_rejects_weakened_case_threshold_or_sku(
    path: tuple[str | int, ...],
    value: object,
) -> None:
    payload = FileM30CampaignContract(ROOT).load().model_dump(mode="python")
    target: object = payload
    for segment in path[:-1]:
        target = target[segment]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]

    with pytest.raises(ValidationError):
        M30CampaignContract.model_validate(payload)


def test_contract_rejects_family_or_evidence_class_substitution() -> None:
    payload = FileM30CampaignContract(ROOT).load().model_dump(mode="python")
    payload["supported_families"] = tuple(payload["supported_families"][:-1])
    with pytest.raises(ValidationError, match="supported SQL families"):
        M30CampaignContract.model_validate(payload)

    payload = FileM30CampaignContract(ROOT).load().model_dump(mode="python")
    controls = list(payload["required_controls"])
    controls[0] = {**controls[0], "evidence_class": M30EvidenceClass.REPOSITORY_CONTRACT}
    payload["required_controls"] = controls
    with pytest.raises(ValidationError, match="required evidence controls"):
        M30CampaignContract.model_validate(payload)

    payload = FileM30CampaignContract(ROOT).load().model_dump(mode="python")
    controls = list(payload["required_controls"])
    controls[0] = {**controls[0], "label": "Untrusted arbitrary report label"}
    payload["required_controls"] = controls
    with pytest.raises(ValidationError, match="required evidence controls"):
        M30CampaignContract.model_validate(payload)


def test_contract_loader_rejects_duplicate_keys_and_extra_fields(tmp_path: Path) -> None:
    path = tmp_path / "plans/M30_CAMPAIGN_CONTRACT.yml"
    path.parent.mkdir(parents=True)
    path.write_text("schema_version: 2\nschema_version: 2\n", encoding="utf-8")

    with pytest.raises(ProductionEvidenceError) as duplicate:
        FileM30CampaignContract(tmp_path).load()
    assert duplicate.value.code.value == "m30_contract_invalid"

    original = (ROOT / "plans/M30_CAMPAIGN_CONTRACT.yml").read_text(encoding="utf-8")
    path.write_text(f"{original}\nunreviewed: true\n", encoding="utf-8")
    with pytest.raises(ProductionEvidenceError) as extra:
        FileM30CampaignContract(tmp_path).load()
    assert extra.value.code.value == "m30_contract_invalid"

    path.write_text("schema_version: &version 1\ncopy: *version\n", encoding="utf-8")
    with pytest.raises(ProductionEvidenceError) as alias:
        FileM30CampaignContract(tmp_path).load()
    assert alias.value.code.value == "m30_contract_invalid"

    path.write_text("? [schema_version, milestone]\n: invalid\n", encoding="utf-8")
    with pytest.raises(ProductionEvidenceError) as complex_key:
        FileM30CampaignContract(tmp_path).load()
    assert complex_key.value.code.value == "m30_contract_invalid"

    path.write_text(
        "nested: " + "[" * 1_500 + "0" + "]" * 1_500 + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ProductionEvidenceError) as deeply_nested:
        FileM30CampaignContract(tmp_path).load()
    assert deeply_nested.value.code.value == "m30_contract_invalid"
    assert main(["--repository-root", str(tmp_path), "--report-only"]) == 3

    path.write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(ProductionEvidenceError) as oversized:
        FileM30CampaignContract(tmp_path).load()
    assert oversized.value.code.value == "m30_contract_invalid"


def test_clean_main_tagged_candidate_passes_only_repository_controls(tmp_path: Path) -> None:
    repository = build_candidate_repository(tmp_path / "candidate", include_local_claim=True)
    contract = FileM30CampaignContract(repository).load()

    candidate = GitM30CandidateIdentity(repository).inspect(contract)
    report = AssessM30Readiness(
        contract_loader=FileM30CampaignContract(repository),
        candidate_identity=GitM30CandidateIdentity(repository),
    ).execute()

    assert candidate.branch is M30CandidateBranch.MAIN
    assert candidate.dirty is False
    assert candidate.annotated_release_tags == ("v0.1.0",)
    assert candidate.migration_versions == tuple(range(1, 16))
    assert candidate.contract_matches_head is True
    assert candidate.all_required_sources_committed is True
    assert report.preflight_state is M30PreflightState.BLOCKED_PREREQUISITES
    assert report.campaign_executable is False
    assert report.release_decision is M30ReleaseDecision.NO_GO
    assert report.synthetic_evidence_accepted_as_operated is False
    repository_gates = {
        gate.code: gate.status
        for gate in report.gates
        if gate.evidence_class is M30EvidenceClass.REPOSITORY_CONTRACT
    }
    assert set(repository_gates.values()) == {M30GateStatus.PASSED}
    assert all(
        gate.status is M30GateStatus.MISSING_EXTERNAL
        for gate in report.gates
        if gate.evidence_class is not M30EvidenceClass.REPOSITORY_CONTRACT
    )
    assert all(
        "merge refs cannot satisfy" in gate.detail
        for gate in report.gates
        if gate.evidence_class is not M30EvidenceClass.REPOSITORY_CONTRACT
    )


def test_dirty_feature_branch_without_tag_fails_candidate_gates(tmp_path: Path) -> None:
    repository = build_candidate_repository(
        tmp_path / "candidate",
        branch="feature/m30",
        annotated_tag=None,
    )
    (repository / "pyproject.toml").write_text("dirty\n", encoding="utf-8")

    report = AssessM30Readiness(
        contract_loader=FileM30CampaignContract(repository),
        candidate_identity=GitM30CandidateIdentity(repository),
    ).execute()
    gates = {gate.code: gate for gate in report.gates}

    assert gates["candidate_clean_tree"].status is M30GateStatus.FAILED
    assert gates["candidate_main_branch"].status is M30GateStatus.FAILED
    assert gates["candidate_annotated_release_tag"].status is M30GateStatus.FAILED
    assert report.candidate.branch is M30CandidateBranch.OTHER
    assert report.campaign_executable is False


def test_package_version_is_read_from_candidate_head(tmp_path: Path) -> None:
    repository = build_candidate_repository(
        tmp_path / "candidate",
        package_version="9.8.7",
    )

    candidate = GitM30CandidateIdentity(repository).inspect(
        FileM30CampaignContract(repository).load()
    )

    assert candidate.package_version == "9.8.7"


def test_matching_stable_package_and_annotated_tag_pass_the_tag_gate(tmp_path: Path) -> None:
    repository = build_candidate_repository(
        tmp_path / "candidate",
        package_version="9.8.7",
        annotated_tag="v9.8.7",
    )

    report = AssessM30Readiness(
        contract_loader=FileM30CampaignContract(repository),
        candidate_identity=GitM30CandidateIdentity(repository),
    ).execute()
    gates = {gate.code: gate for gate in report.gates}

    assert gates["candidate_annotated_release_tag"].status is M30GateStatus.PASSED


def test_annotated_tag_must_match_the_candidate_package_version(tmp_path: Path) -> None:
    repository = build_candidate_repository(
        tmp_path / "candidate",
        annotated_tag="v9.9.9",
    )

    report = AssessM30Readiness(
        contract_loader=FileM30CampaignContract(repository),
        candidate_identity=GitM30CandidateIdentity(repository),
    ).execute()
    gates = {gate.code: gate for gate in report.gates}

    assert report.candidate.package_version == "0.1.0"
    assert report.candidate.annotated_release_tags == ("v9.9.9",)
    assert gates["candidate_annotated_release_tag"].status is M30GateStatus.FAILED


@pytest.mark.parametrize(
    "tag",
    ("v01.2.3", "v1.2.3-01", "v1.02.3", "v1.2.3-rc.1", "v1.2.3+build"),
)
def test_noncanonical_semver_tags_never_identify_a_candidate(tmp_path: Path, tag: str) -> None:
    repository = build_candidate_repository(tmp_path / "candidate", annotated_tag=tag)

    report = AssessM30Readiness(
        contract_loader=FileM30CampaignContract(repository),
        candidate_identity=GitM30CandidateIdentity(repository),
    ).execute()
    gates = {gate.code: gate for gate in report.gates}

    assert report.candidate.annotated_release_tags == ()
    assert gates["candidate_annotated_release_tag"].status is M30GateStatus.FAILED


def test_non_repository_gate_cannot_be_promoted_by_a_local_claim() -> None:
    with pytest.raises(ValidationError, match="cannot evaluate external evidence"):
        M30GateResult(
            code="independent_pentest",
            evidence_class=M30EvidenceClass.INDEPENDENT_THIRD_PARTY,
            status=M30GateStatus.PASSED,
            subject_revision="a" * 40,
            detail="A repository file claims that the external assessment passed.",
        )

    with pytest.raises(ValidationError, match="cannot evaluate external evidence"):
        M30GateResult(
            code="independent_pentest",
            evidence_class=M30EvidenceClass.INDEPENDENT_THIRD_PARTY,
            status=M30GateStatus.FAILED,
            subject_revision="a" * 40,
            detail="A local preflight cannot claim to have evaluated an external control.",
        )


def test_readiness_report_requires_every_exact_candidate_bound_gate(tmp_path: Path) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    report = AssessM30Readiness(
        contract_loader=FileM30CampaignContract(repository),
        candidate_identity=GitM30CandidateIdentity(repository),
    ).execute()
    payload = report.model_dump(mode="python")
    payload["gates"] = payload["gates"][1:]

    with pytest.raises(ValidationError, match="every exact candidate-bound gate"):
        M30ReadinessReport.model_validate(payload)

    payload = report.model_dump(mode="python")
    payload["gates"][0]["subject_revision"] = "b" * 40
    with pytest.raises(ValidationError, match="every exact candidate-bound gate"):
        M30ReadinessReport.model_validate(payload)

    payload = report.model_dump(mode="python")
    for gate in payload["gates"]:
        if gate["code"] == "candidate_clean_tree":
            gate["status"] = M30GateStatus.FAILED
            break
    with pytest.raises(ValidationError, match="statuses do not match"):
        M30ReadinessReport.model_validate(payload)

    payload = report.model_dump(mode="python")
    payload["network_calls"] = False
    with pytest.raises(ValidationError, match="non-exact scalar type"):
        M30ReadinessReport.model_validate(payload)

    payload = report.model_dump(mode="python")
    payload["candidate"]["dirty"] = 0
    with pytest.raises(ValidationError, match="non-exact scalar type"):
        M30ReadinessReport.model_validate(payload)

    payload = report.model_dump(mode="python")
    payload["candidate"]["source_digests"] = payload["candidate"]["source_digests"][:1]
    with pytest.raises(ValidationError):
        M30ReadinessReport.model_validate(payload)

    payload = report.model_dump(mode="python")
    payload["candidate"]["source_digests"][0]["path"] = "unreviewed/source.txt"
    with pytest.raises(ValidationError, match="exact reviewed set"):
        M30ReadinessReport.model_validate(payload)


def test_git_replace_refs_and_inherited_git_environment_cannot_forge_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    original_revision = _git(repository, "rev-parse", "HEAD")
    (repository / "poison.txt").write_text("replacement tree\n", encoding="utf-8")
    _git(repository, "add", "poison.txt")
    _git(repository, "commit", "-m", "replacement candidate")
    replacement_revision = _git(repository, "rev-parse", "HEAD")
    _git(repository, "replace", original_revision, replacement_revision)
    _git(repository, "reset", "--hard", original_revision)

    foreign = build_candidate_repository(tmp_path / "foreign", annotated_tag=None)
    (foreign / "foreign.txt").write_text("different repository\n", encoding="utf-8")
    _git(foreign, "add", "foreign.txt")
    _git(foreign, "commit", "-m", "different candidate")
    foreign_revision = _git(foreign, "rev-parse", "HEAD")
    assert foreign_revision != original_revision
    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repository))

    candidate = GitM30CandidateIdentity(repository).inspect(
        FileM30CampaignContract(repository).load()
    )
    expected_tree = _git(repository, "--no-replace-objects", "rev-parse", "HEAD^{tree}")

    assert candidate.revision == original_revision
    assert candidate.head_tree_oid == expected_tree
    assert candidate.package_version == "0.1.0"
    assert candidate.dirty is True


def test_candidate_inspection_disables_configured_fsmonitor_hook(tmp_path: Path) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor-hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 0\n", encoding="utf-8")
    hook.chmod(0o700)
    _git(repository, "config", "core.fsmonitor", str(hook))

    GitM30CandidateIdentity(repository).inspect(FileM30CampaignContract(repository).load())

    assert not marker.exists()


def test_candidate_inspection_never_executes_git_clean_filters(tmp_path: Path) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    marker = tmp_path / "clean-filter-ran"
    attributes = repository / ".gitattributes"
    attributes.write_text("pyproject.toml filter=probe\n", encoding="utf-8")
    _git(repository, "add", ".gitattributes")
    _git(repository, "commit", "-m", "add hostile clean-filter attribute")
    _git(
        repository,
        "config",
        "filter.probe.clean",
        f"/usr/bin/touch {marker}; /bin/cat",
    )

    candidate = GitM30CandidateIdentity(repository).inspect(
        FileM30CampaignContract(repository).load()
    )

    assert candidate.dirty is False
    assert not marker.exists()


def test_candidate_dirty_state_hashes_bytes_instead_of_trusting_git_stat_cache(
    tmp_path: Path,
) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    pyproject = repository / "pyproject.toml"
    before = pyproject.stat()
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace('version = "0.1.0"', 'version = "9.9.9"'),
        encoding="utf-8",
    )
    os.utime(pyproject, ns=(before.st_atime_ns, before.st_mtime_ns))
    _git(repository, "config", "core.trustctime", "false")
    _git(repository, "config", "core.checkStat", "minimal")

    candidate = GitM30CandidateIdentity(repository).inspect(
        FileM30CampaignContract(repository).load()
    )

    assert candidate.dirty is True


@pytest.mark.parametrize("flag", ("--assume-unchanged", "--skip-worktree"))
def test_candidate_inspection_rejects_hidden_or_sparse_index_state(
    tmp_path: Path,
    flag: str,
) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    _git(repository, "update-index", flag, "pyproject.toml")

    with pytest.raises(ProductionEvidenceError) as failure:
        GitM30CandidateIdentity(repository).inspect(FileM30CampaignContract(repository).load())

    assert failure.value.code.value == "m30_candidate_inspection_failed"


def test_candidate_inspection_rejects_symlink_blob_and_unexpected_migration(
    tmp_path: Path,
) -> None:
    symlink_repository = build_candidate_repository(tmp_path / "symlink-candidate")
    runtime_requirements = symlink_repository / "requirements/runtime.txt"
    runtime_requirements.unlink()
    runtime_requirements.symlink_to("missing-runtime.txt")
    _git(symlink_repository, "add", "requirements/runtime.txt")
    _git(symlink_repository, "commit", "-m", "replace required source with symlink")

    with pytest.raises(ProductionEvidenceError) as symlink_failure:
        GitM30CandidateIdentity(symlink_repository).inspect(
            FileM30CampaignContract(symlink_repository).load()
        )
    assert symlink_failure.value.code.value == "m30_candidate_inspection_failed"

    migration_repository = build_candidate_repository(tmp_path / "migration-candidate")
    evil = migration_repository / "migrations/control_plane/evil.sql"
    evil.write_text("SELECT 1;\n", encoding="utf-8")
    _git(migration_repository, "add", "migrations/control_plane/evil.sql")
    _git(migration_repository, "commit", "-m", "add unexpected migration")

    with pytest.raises(ProductionEvidenceError) as migration_failure:
        GitM30CandidateIdentity(migration_repository).inspect(
            FileM30CampaignContract(migration_repository).load()
        )
    assert migration_failure.value.code.value == "m30_candidate_inspection_failed"


def test_git_tree_parser_rejects_gitlinks() -> None:
    raw = b"160000 commit " + b"a" * 40 + b"\tdependency\0"

    with pytest.raises(ValueError, match="gitlink"):
        m30_adapter._parse_git_tree(raw)


def test_candidate_inspection_sanitizes_a_deep_committed_contract(tmp_path: Path) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    contract = FileM30CampaignContract(ROOT).load()
    contract_path = repository / "plans/M30_CAMPAIGN_CONTRACT.yml"
    contract_path.write_text(
        "nested: " + "[" * 1_500 + "0" + "]" * 1_500 + "\n",
        encoding="utf-8",
    )
    _git(repository, "add", "plans/M30_CAMPAIGN_CONTRACT.yml")
    _git(repository, "commit", "-m", "add adversarial deep contract")

    with pytest.raises(ProductionEvidenceError) as failure:
        GitM30CandidateIdentity(repository).inspect(contract)

    assert failure.value.code.value == "m30_candidate_inspection_failed"


def test_git_timeout_kills_descendants_that_hold_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = "import time; time.sleep(60)"
    leader = (
        "import os, subprocess, sys; "
        f"subprocess.Popen((sys.executable, '-c', {child!r}), stdout=sys.stdout); "
        "os._exit(0)"
    )
    monkeypatch.setattr(m30_adapter, "_SAFE_GIT_PREFIX", (sys.executable, "-c", leader))
    monkeypatch.setattr(m30_adapter, "_GIT_TIMEOUT_SECONDS", 0.05)
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        m30_adapter._run_safe_git(tmp_path, "ignored")

    assert time.monotonic() - started < 2


def test_candidate_inspection_rejects_a_repository_subdirectory_as_root(tmp_path: Path) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    contract = FileM30CampaignContract(repository).load()

    with pytest.raises(ProductionEvidenceError) as failure:
        GitM30CandidateIdentity(repository / "docs").inspect(contract)

    assert failure.value.code.value == "m30_candidate_inspection_failed"


def test_uncommitted_contract_is_reported_not_mistaken_for_head(tmp_path: Path) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    contract_path = repository / "plans/M30_CAMPAIGN_CONTRACT.yml"
    payload = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    payload["case_minimums"][0], payload["case_minimums"][1] = (
        payload["case_minimums"][1],
        payload["case_minimums"][0],
    )
    contract_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    report = AssessM30Readiness(
        contract_loader=FileM30CampaignContract(repository),
        candidate_identity=GitM30CandidateIdentity(repository),
    ).execute()
    gates = {gate.code: gate for gate in report.gates}

    assert report.candidate.contract_matches_head is False
    assert gates["candidate_source_contract"].status is M30GateStatus.FAILED
    assert gates["candidate_clean_tree"].status is M30GateStatus.FAILED


def test_report_writer_is_deterministic_and_cli_defaults_to_nonzero_no_go(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    report = AssessM30Readiness(
        contract_loader=FileM30CampaignContract(repository),
        candidate_identity=GitM30CandidateIdentity(repository),
    ).execute()
    output = tmp_path / "output"
    writer = FileM30ReadinessReportWriter(repository)

    json_path, markdown_path = writer.write(report, output)
    first_json = json_path.read_text(encoding="utf-8")
    first_markdown = markdown_path.read_text(encoding="utf-8")
    writer.write(report, output)

    assert json_path.read_text(encoding="utf-8") == first_json
    assert markdown_path.read_text(encoding="utf-8") == first_markdown
    bundle = json.loads(first_json)
    assert bundle["report_sha256"] == report.fingerprint()
    assert bundle["markdown_sha256"] == hashlib.sha256(first_markdown.encode()).hexdigest()
    assert "not M30 acceptance" in first_markdown
    assert main(["--repository-root", str(repository), "--output-directory", str(output)]) == 2
    assert "campaign_executable=false" in capsys.readouterr().out
    assert (
        main(
            [
                "--repository-root",
                str(repository),
                "--output-directory",
                str(output),
                "--report-only",
            ]
        )
        == 0
    )
    stdout = capsys.readouterr().out
    assert str(repository) not in stdout
    assert str(output) not in stdout


def test_external_report_writer_preflights_both_files_before_any_change(tmp_path: Path) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    report = AssessM30Readiness(
        contract_loader=FileM30CampaignContract(repository),
        candidate_identity=GitM30CandidateIdentity(repository),
    ).execute()
    output = tmp_path / "external-output"
    writer = FileM30ReadinessReportWriter(repository)
    json_path, markdown_path = writer.write(report, output)
    markdown_path.unlink()
    json_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ProductionEvidenceError) as failure:
        writer.write(report, output)

    assert failure.value.code.value == "m30_report_write_failed"
    assert not markdown_path.exists()
    assert json_path.read_text(encoding="utf-8") == "{}\n"


def test_report_writer_never_commits_json_if_markdown_only_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    report = AssessM30Readiness(
        contract_loader=FileM30CampaignContract(repository),
        candidate_identity=GitM30CandidateIdentity(repository),
    ).execute()
    output = tmp_path / "output"
    original_atomic_write = m30_adapter._atomic_write

    def fail_json(path: Path, value: str, *, allow_replace: bool) -> None:
        if path.suffix == ".json":
            raise OSError("injected bundle commit failure")
        original_atomic_write(path, value, allow_replace=allow_replace)

    monkeypatch.setattr(m30_adapter, "_atomic_write", fail_json)

    with pytest.raises(ProductionEvidenceError) as failure:
        FileM30ReadinessReportWriter(repository).write(report, output)

    assert failure.value.code.value == "m30_report_write_failed"
    assert (output / "readiness.md").is_file()
    assert not (output / "readiness.json").exists()


def test_contract_loader_rejects_a_symlinked_parent(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    (external / "M30_CAMPAIGN_CONTRACT.yml").write_text(
        (ROOT / "plans/M30_CAMPAIGN_CONTRACT.yml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    repository = tmp_path / "candidate"
    repository.mkdir()
    (repository / "plans").symlink_to(external, target_is_directory=True)

    with pytest.raises(ProductionEvidenceError) as failure:
        FileM30CampaignContract(repository).load()

    assert failure.value.code.value == "m30_contract_unavailable"


def test_cli_rejects_a_symlinked_report_destination(tmp_path: Path) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    external = tmp_path / "external"
    external.mkdir()
    linked_output = tmp_path / "linked-output"
    linked_output.symlink_to(external, target_is_directory=True)

    result = main(
        [
            "--repository-root",
            str(repository),
            "--output-directory",
            str(linked_output),
            "--report-only",
        ]
    )

    assert result == 3
    assert not (external / "readiness.json").exists()


def test_cli_rejects_lexical_parent_traversal_in_report_destination(tmp_path: Path) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    output = tmp_path / "nested" / ".." / "evidence"

    result = main(
        [
            "--repository-root",
            str(repository),
            "--output-directory",
            str(output),
            "--report-only",
        ]
    )

    assert result == 3
    assert not (tmp_path / "evidence/readiness.json").exists()


def test_cli_rejects_candidate_tree_output_and_keeps_canonical_output_ignored(
    tmp_path: Path,
) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")

    rejected = main(
        [
            "--repository-root",
            str(repository),
            "--output-directory",
            str(repository / "docs"),
            "--report-only",
        ]
    )
    accepted = main(["--repository-root", str(repository), "--report-only"])

    assert rejected == 3
    assert not (repository / "docs/readiness.json").exists()
    assert accepted == 0
    assert (repository / ".local/m30/readiness.json").is_file()
    assert _git(repository, "status", "--porcelain=v1") == ""


def _git(root: Path, *arguments: str) -> str:
    environment = os.environ.copy()
    environment.pop("GIT_DIR", None)
    environment.pop("GIT_WORK_TREE", None)
    result = subprocess.run(
        ("git", *arguments),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout.strip()
