"""M30 Phase-1b external policy loading, binding and reporting regressions."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from tests.m30_campaign_support import VERIFICATION_TIME, build_manifest, write_manifest
from tests.m30_control_policy_support import build_control_policy, write_control_policy
from tests.m30_readiness_support import build_candidate_repository

from schemabridge.adapters.evaluation import m30_campaign as campaign_adapter
from schemabridge.adapters.evaluation.m30_campaign import (
    FileM30CampaignManifest,
    FileM30ControlPolicy,
    FileM30ControlPolicyReportWriter,
)
from schemabridge.adapters.evaluation.m30_readiness import (
    FileM30CampaignContract,
    GitM30CandidateIdentity,
)
from schemabridge.application.ports.production_evidence import (
    LoadedM30CampaignManifest,
    M30ClockPort,
    ProductionEvidenceError,
)
from schemabridge.application.production_campaign import (
    AuthenticateM30CampaignManifest,
    ValidateM30CampaignManifest,
)
from schemabridge.application.production_control_policy import ValidateM30ControlPolicy
from schemabridge.application.production_readiness import AssessM30Readiness
from schemabridge.domain.production_campaign import (
    M30_GITHUB_ATTESTATION_PREDICATE,
    M30_GITHUB_CLI_EXECUTABLE_SHA256,
    M30_GITHUB_CLI_VERSION,
    M30_GITHUB_OIDC_ISSUER,
    M30_GITHUB_REPOSITORY,
    M30_GITHUB_SIGNER_WORKFLOW,
    M30AuthenticatedManifest,
)
from schemabridge.domain.production_campaign_receipts import (
    M30ControlPolicyBlockReason,
    M30ControlPolicyValidationState,
)


@dataclass(frozen=True, slots=True)
class _FixedClock:
    instant: datetime = VERIFICATION_TIME

    def now(self) -> datetime:
        return self.instant


@dataclass(slots=True)
class _SequenceClock:
    instants: tuple[datetime, ...]
    position: int = 0

    def now(self) -> datetime:
        instant = self.instants[self.position]
        self.position += 1
        return instant


@dataclass(slots=True)
class _Authenticator:
    def authenticate(
        self,
        loaded: LoadedM30CampaignManifest,
        *,
        verified_at: datetime,
    ) -> M30AuthenticatedManifest:
        candidate = loaded.manifest.candidate
        return M30AuthenticatedManifest(
            manifest_sha256=loaded.raw_sha256,
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
            verified_at=verified_at,
        )


def test_authenticated_manifest_binds_external_policy_but_passes_no_control(
    tmp_path: Path,
) -> None:
    service, _repository, _manifest_path, _policy_path = _policy_service(tmp_path)

    report = service.execute()

    assert report.state is M30ControlPolicyValidationState.VALIDATED
    assert report.schema_version == 2
    assert report.observed_at == VERIFICATION_TIME
    assert report.policy_not_before is not None
    assert report.policy_expires_at is not None
    assert report.manifest_authentication_sha256 is not None
    assert report.policy_bound_to_authenticated_manifest is True
    assert report.control_dag_validated is True
    assert report.external_policy_trust_authenticated is False
    assert report.receipt_authentication_enabled is False
    assert report.implemented_adjudicators == 1
    assert report.admitted_unadjudicated_controls == 23
    assert report.external_controls_passed == 0
    assert report.external_controls_remaining == 24
    assert report.campaign_executable is False
    assert report.production_release_authorized is False
    assert (
        report.provider_calls,
        report.source_reads,
        report.source_writes,
        report.target_calls,
        report.corpus_reads,
        report.datahub_writes,
    ) == (0, 0, 0, 0, 0, 0)


@pytest.mark.parametrize(
    ("policy_not_before", "policy_expires_at", "expected_reason"),
    [
        (
            VERIFICATION_TIME + timedelta(hours=1),
            None,
            M30ControlPolicyBlockReason.POLICY_NOT_YET_VALID,
        ),
        (None, VERIFICATION_TIME, M30ControlPolicyBlockReason.POLICY_EXPIRED),
    ],
)
def test_policy_report_retains_decision_time_and_blocks_outside_window(
    tmp_path: Path,
    policy_not_before: datetime | None,
    policy_expires_at: datetime | None,
    expected_reason: M30ControlPolicyBlockReason,
) -> None:
    service, _repository, _manifest_path, _policy_path = _policy_service(
        tmp_path,
        policy_not_before=policy_not_before,
        policy_expires_at=policy_expires_at,
    )

    report = service.execute()

    assert report.state is M30ControlPolicyValidationState.BLOCKED
    assert expected_reason in report.blocking_reasons
    assert report.observed_at == VERIFICATION_TIME
    assert report.policy_not_before is not None
    assert report.policy_expires_at is not None
    assert report.external_controls_passed == 0


def test_policy_validation_rejects_clock_rollback_after_phase1a_completion(
    tmp_path: Path,
) -> None:
    clock = _SequenceClock(
        (
            VERIFICATION_TIME,
            VERIFICATION_TIME + timedelta(minutes=6),
            VERIFICATION_TIME + timedelta(minutes=4),
        )
    )
    service, _repository, _manifest_path, _policy_path = _policy_service(
        tmp_path,
        clock=clock,
        policy_expires_at=VERIFICATION_TIME + timedelta(minutes=5),
    )

    report = service.execute()

    assert report.state is M30ControlPolicyValidationState.BLOCKED
    assert report.blocking_reasons == (M30ControlPolicyBlockReason.CLOCK_ROLLBACK,)
    assert report.observed_at == VERIFICATION_TIME + timedelta(minutes=4)


def test_different_canonical_policy_is_blocked_even_when_manifest_authenticates(
    tmp_path: Path,
) -> None:
    service, repository, manifest_path, policy_path = _policy_service(tmp_path)
    loaded = FileM30ControlPolicy(repository, policy_path).load().policy
    payload = loaded.model_dump(mode="python")
    payload["rules"][1]["criteria_policy_sha256"] = "f" * 64
    changed = type(loaded).model_validate(payload)
    policy_path.write_bytes(changed.canonical_bytes())

    report = service.execute()

    assert report.state is M30ControlPolicyValidationState.BLOCKED
    assert report.blocking_reasons == (M30ControlPolicyBlockReason.POLICY_SUBJECT_MISMATCH,)
    assert report.external_controls_passed == 0
    assert FileM30CampaignManifest(repository, manifest_path).load().raw_sha256 == (
        report.manifest_sha256
    )


@pytest.mark.parametrize(
    ("raw", "expected_code"),
    [
        (b'{"schema_version":1,"schema_version":1}\n', "m30_control_policy_invalid"),
        (b"{}\n", "m30_control_policy_invalid"),
        (b"\x00", "m30_control_policy_invalid"),
    ],
)
def test_policy_loader_rejects_noncanonical_or_ambiguous_bytes(
    tmp_path: Path,
    raw: bytes,
    expected_code: str,
) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    path = tmp_path / "external/policy.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)

    with pytest.raises(ProductionEvidenceError) as rejected:
        FileM30ControlPolicy(repository, path).load()

    assert rejected.value.code.value == expected_code


def test_policy_loader_rejects_candidate_contained_file(tmp_path: Path) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    provisional = build_manifest(repository)
    policy = build_control_policy(provisional)
    path = write_control_policy(repository / "policy.json", policy)

    with pytest.raises(ProductionEvidenceError) as rejected:
        FileM30ControlPolicy(repository, path).load()

    assert rejected.value.code.value == "m30_control_policy_invalid"


def test_policy_loader_rejects_symlinked_parent_component(tmp_path: Path) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    provisional = build_manifest(repository)
    actual = tmp_path / "actual-external"
    write_control_policy(actual / "policy.json", build_control_policy(provisional))
    alias = tmp_path / "external-alias"
    alias.symlink_to(actual, target_is_directory=True)

    with pytest.raises(ProductionEvidenceError) as rejected:
        FileM30ControlPolicy(repository, alias / "policy.json").load()

    assert rejected.value.code.value == "m30_control_policy_invalid"


def test_policy_loader_rejects_group_writable_ancestor(tmp_path: Path) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    provisional = build_manifest(repository)
    shared = tmp_path / "shared"
    path = write_control_policy(
        shared / "private/policy.json",
        build_control_policy(provisional),
    )
    shared.chmod(0o770)
    path.parent.chmod(0o700)

    with pytest.raises(ProductionEvidenceError) as rejected:
        FileM30ControlPolicy(repository, path).load()

    assert rejected.value.code.value == "m30_control_policy_invalid"


def test_policy_loader_fails_closed_without_required_dirfd_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    provisional = build_manifest(repository)
    path = write_control_policy(
        tmp_path / "external/policy.json",
        build_control_policy(provisional),
    )
    monkeypatch.setattr(campaign_adapter.os, "supports_dir_fd", set())

    with pytest.raises(ProductionEvidenceError) as rejected:
        FileM30ControlPolicy(repository, path).load()

    assert rejected.value.code.value == "m30_control_policy_invalid"


@pytest.mark.parametrize("mutation", ("group_writable", "hard_linked"))
def test_policy_loader_requires_protected_single_link_external_bytes(
    tmp_path: Path,
    mutation: str,
) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    provisional = build_manifest(repository)
    policy = build_control_policy(provisional)
    path = write_control_policy(tmp_path / "external/policy.json", policy)
    if mutation == "group_writable":
        path.chmod(0o620)
    else:
        os.link(path, path.with_name("policy-alias.json"))

    with pytest.raises(ProductionEvidenceError) as rejected:
        FileM30ControlPolicy(repository, path).load()

    assert rejected.value.code.value == "m30_control_policy_invalid"


def test_policy_loader_opens_nonregular_leaf_nonblocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    external = tmp_path / "external"
    external.mkdir()
    path = external / "policy.json"
    os.mkfifo(path, mode=0o600)
    real_open = campaign_adapter.os.open
    nonblocking_open_observed = False

    def guarded_open(
        requested: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal nonblocking_open_observed
        if requested == path.name and dir_fd is not None:
            if not flags & campaign_adapter.os.O_NONBLOCK:
                raise AssertionError("M30 FIFO leaf was opened without O_NONBLOCK")
            nonblocking_open_observed = True
        return real_open(requested, flags, mode, dir_fd=dir_fd)

    supported = set(campaign_adapter.os.supports_dir_fd)
    supported.add(guarded_open)
    monkeypatch.setattr(campaign_adapter.os, "open", guarded_open)
    monkeypatch.setattr(campaign_adapter.os, "supports_dir_fd", supported)

    with pytest.raises(ProductionEvidenceError) as rejected:
        FileM30ControlPolicy(repository, path).load()

    assert nonblocking_open_observed is True
    assert rejected.value.code.value == "m30_control_policy_invalid"


def test_policy_loader_fails_closed_when_parent_path_is_rebound_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    provisional = build_manifest(repository)
    external = tmp_path / "external"
    path = write_control_policy(external / "policy.json", build_control_policy(provisional))
    held = tmp_path / "external-held"
    attacker = tmp_path / "attacker"
    attacker.mkdir(mode=0o700)
    real_read = campaign_adapter._read_exact_descriptor
    rebound = False

    def rebind_after_read(descriptor: int, size: int) -> bytes:
        nonlocal rebound
        raw = real_read(descriptor, size)
        if not rebound:
            external.rename(held)
            external.symlink_to(attacker, target_is_directory=True)
            rebound = True
        return raw

    monkeypatch.setattr(campaign_adapter, "_read_exact_descriptor", rebind_after_read)

    with pytest.raises(ProductionEvidenceError) as rejected:
        FileM30ControlPolicy(repository, path).load()

    assert rebound is True
    assert rejected.value.code.value == "m30_control_policy_invalid"
    assert not list(attacker.iterdir())


def test_policy_loader_rechecks_outer_ancestor_after_final_descriptor_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    provisional = build_manifest(repository)
    outer = tmp_path / "external-owner"
    path = write_control_policy(
        outer / "nested/policy.json",
        build_control_policy(provisional),
    )
    held = tmp_path / "external-owner-held"
    attacker = tmp_path / "attacker-outer"
    attacker.mkdir(mode=0o700)
    real_read = campaign_adapter._read_exact_descriptor
    reads = 0

    def rebind_outer_after_final_read(descriptor: int, size: int) -> bytes:
        nonlocal reads
        raw = real_read(descriptor, size)
        reads += 1
        if reads == 2:
            outer.rename(held)
            outer.symlink_to(attacker, target_is_directory=True)
        return raw

    monkeypatch.setattr(
        campaign_adapter,
        "_read_exact_descriptor",
        rebind_outer_after_final_read,
    )

    with pytest.raises(ProductionEvidenceError) as rejected:
        FileM30ControlPolicy(repository, path).load()

    assert reads == 2
    assert rejected.value.code.value == "m30_control_policy_invalid"
    assert not list(attacker.iterdir())


def test_policy_report_is_deterministic_and_refuses_different_external_overwrite(
    tmp_path: Path,
) -> None:
    service, repository, _manifest_path, _policy_path = _policy_service(tmp_path)
    report = service.execute()
    writer = FileM30ControlPolicyReportWriter(repository)
    output = tmp_path / "policy-report"

    first = writer.write(report, output)
    second = writer.write(report, output)

    assert first == second
    payload = json.loads(first[0].read_text(encoding="utf-8"))
    assert payload["report_sha256"] == report.fingerprint()
    assert payload["report"]["external_controls_passed"] == 0
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE(first[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(first[1].stat().st_mode) == 0o600
    first[1].write_text("different\n", encoding="utf-8")
    with pytest.raises(ProductionEvidenceError) as rejected:
        writer.write(report, output)
    assert rejected.value.code.value == "m30_control_policy_report_write_failed"


def test_policy_report_atomically_replaces_changed_canonical_ignored_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, _manifest_path, _policy_path = _policy_service(tmp_path)
    report = service.execute()
    writer = FileM30ControlPolicyReportWriter(repository)
    output = repository / ".local/m30/control-policy"

    first = writer.write(report, output)
    expected_markdown = first[1].read_text(encoding="utf-8")
    first[1].write_text("changed local copy\n", encoding="utf-8")
    first[1].chmod(0o600)
    real_replace = campaign_adapter._atomic_replace_at
    replacements: list[str] = []

    def record_replace(
        directory_descriptor: int,
        filename: str,
        value: str,
    ) -> None:
        replacements.append(filename)
        real_replace(directory_descriptor, filename, value)

    monkeypatch.setattr(campaign_adapter, "_atomic_replace_at", record_replace)
    second = writer.write(report, output)

    assert first == second
    assert replacements == ["policy-validation.md", "policy-validation.json"]
    assert second[1].read_text(encoding="utf-8") == expected_markdown
    assert stat.S_IMODE(second[1].stat().st_mode) == 0o600


def test_policy_report_requires_both_canonical_files_to_be_ignored(tmp_path: Path) -> None:
    service, repository, _manifest_path, _policy_path = _policy_service(tmp_path)
    report = service.execute()
    (repository / ".gitignore").write_text(
        ".local/m30/control-policy/policy-validation.json\n",
        encoding="utf-8",
    )
    writer = FileM30ControlPolicyReportWriter(repository)
    output = repository / ".local/m30/control-policy"

    with pytest.raises(ProductionEvidenceError) as rejected:
        writer.write(report, output)

    assert rejected.value.code.value == "m30_control_policy_report_write_failed"
    assert not output.exists()


def test_policy_report_rejects_canonical_files_already_in_git_index(tmp_path: Path) -> None:
    service, repository, _manifest_path, _policy_path = _policy_service(tmp_path)
    report = service.execute()
    writer = FileM30ControlPolicyReportWriter(repository)
    output = repository / ".local/m30/control-policy"
    paths = writer.write(report, output)
    tracked = campaign_adapter._run_safe_git(
        repository,
        "add",
        "-f",
        "--",
        *(str(path.relative_to(repository)) for path in paths),
    )
    assert tracked.returncode == 0

    with pytest.raises(ProductionEvidenceError) as rejected:
        writer.write(report, output)

    assert rejected.value.code.value == "m30_control_policy_report_write_failed"


def test_policy_report_rejects_external_json_marker_without_markdown_companion(
    tmp_path: Path,
) -> None:
    service, repository, _manifest_path, _policy_path = _policy_service(tmp_path)
    report = service.execute()
    writer = FileM30ControlPolicyReportWriter(repository)
    complete = tmp_path / "complete-report"
    complete_paths = writer.write(report, complete)
    output = tmp_path / "incomplete-report"
    output.mkdir(mode=0o700)
    marker = output / "policy-validation.json"
    marker.write_bytes(complete_paths[0].read_bytes())
    marker.chmod(0o600)

    with pytest.raises(ProductionEvidenceError) as rejected:
        writer.write(report, output)

    assert rejected.value.code.value == "m30_control_policy_report_write_failed"
    assert not (output / "policy-validation.md").exists()


def test_policy_report_fails_closed_when_destination_path_is_rebound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, _manifest_path, _policy_path = _policy_service(tmp_path)
    report = service.execute()
    writer = FileM30ControlPolicyReportWriter(repository)
    output = tmp_path / "policy-report"
    output.mkdir(mode=0o700)
    held = tmp_path / "policy-report-held"
    attacker = tmp_path / "attacker-report"
    attacker.mkdir(mode=0o700)
    real_write = campaign_adapter._atomic_write_at
    rebound = False

    def rebind_before_write(directory_descriptor: int, filename: str, value: str) -> None:
        nonlocal rebound
        if not rebound:
            output.rename(held)
            output.symlink_to(attacker, target_is_directory=True)
            rebound = True
        real_write(directory_descriptor, filename, value)

    monkeypatch.setattr(campaign_adapter, "_atomic_write_at", rebind_before_write)

    with pytest.raises(ProductionEvidenceError) as rejected:
        writer.write(report, output)

    assert rebound is True
    assert rejected.value.code.value == "m30_control_policy_report_write_failed"
    assert not list(attacker.iterdir())


def test_policy_report_fails_closed_when_published_target_changes_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, _manifest_path, _policy_path = _policy_service(tmp_path)
    report = service.execute()
    writer = FileM30ControlPolicyReportWriter(repository)
    output = tmp_path / "policy-report"
    real_validate = campaign_adapter._validate_anchored_directory_path
    mutated = False

    def mutate_before_path_recheck(
        path: Path,
        descriptor: int,
        *,
        require_private: bool,
        expected_identity: tuple[int, int, int, int, int, int, int, int, int] | None = None,
    ) -> None:
        nonlocal mutated
        if path == output and require_private and not mutated:
            target = output / "policy-validation.md"
            target.write_text("changed after publication\n", encoding="utf-8")
            target.chmod(0o600)
            mutated = True
        real_validate(
            path,
            descriptor,
            require_private=require_private,
            expected_identity=expected_identity,
        )

    monkeypatch.setattr(
        campaign_adapter,
        "_validate_anchored_directory_path",
        mutate_before_path_recheck,
    )

    with pytest.raises(ProductionEvidenceError) as rejected:
        writer.write(report, output)

    assert mutated is True
    assert rejected.value.code.value == "m30_control_policy_report_write_failed"


@pytest.mark.parametrize("mutation", ("symlink", "hard_link", "group_writable"))
def test_policy_report_rejects_unsafe_preexisting_target(
    tmp_path: Path,
    mutation: str,
) -> None:
    service, repository, _manifest_path, _policy_path = _policy_service(tmp_path)
    report = service.execute()
    writer = FileM30ControlPolicyReportWriter(repository)
    output = tmp_path / "policy-report"
    output.mkdir(mode=0o700)
    target = output / "policy-validation.md"
    original = tmp_path / "original-report"
    original.write_text("original\n", encoding="utf-8")
    original.chmod(0o600)
    if mutation == "symlink":
        target.symlink_to(original)
    elif mutation == "hard_link":
        os.link(original, target)
    else:
        target.write_text("different\n", encoding="utf-8")
        target.chmod(0o620)

    with pytest.raises(ProductionEvidenceError) as rejected:
        writer.write(report, output)

    assert rejected.value.code.value == "m30_control_policy_report_write_failed"
    assert original.read_text(encoding="utf-8") == "original\n"


def test_policy_report_requires_owner_private_destination(tmp_path: Path) -> None:
    service, repository, _manifest_path, _policy_path = _policy_service(tmp_path)
    report = service.execute()
    writer = FileM30ControlPolicyReportWriter(repository)
    output = tmp_path / "policy-report"
    output.mkdir()
    output.chmod(0o755)

    with pytest.raises(ProductionEvidenceError) as rejected:
        writer.write(report, output)

    assert rejected.value.code.value == "m30_control_policy_report_write_failed"
    assert not list(output.iterdir())


def _policy_service(
    tmp_path: Path,
    *,
    clock: M30ClockPort | None = None,
    policy_not_before: datetime | None = None,
    policy_expires_at: datetime | None = None,
) -> tuple[ValidateM30ControlPolicy, Path, Path, Path]:
    repository = build_candidate_repository(tmp_path / "candidate")
    provisional = build_manifest(repository)
    provisional_policy = build_control_policy(
        provisional,
        not_before=policy_not_before,
        expires_at=policy_expires_at,
    )
    manifest = build_manifest(
        repository,
        control_policy_id=provisional_policy.policy_id,
        control_policy_sha256=provisional_policy.fingerprint(),
    )
    policy = build_control_policy(
        manifest,
        not_before=policy_not_before,
        expires_at=policy_expires_at,
    )
    manifest_path = write_manifest(tmp_path / "external/manifest.json", manifest)
    policy_path = write_control_policy(tmp_path / "external/policy.json", policy)
    manifest_loader = FileM30CampaignManifest(repository, manifest_path)
    resolved_clock: M30ClockPort = clock or _FixedClock()
    contract = FileM30CampaignContract(repository)
    authenticator = AuthenticateM30CampaignManifest(
        validator=ValidateM30CampaignManifest(
            readiness=AssessM30Readiness(
                contract_loader=contract,
                candidate_identity=GitM30CandidateIdentity(repository),
            ),
            contract_loader=contract,
            manifest_loader=manifest_loader,
        ),
        authenticator=_Authenticator(),
        clock=resolved_clock,
    )
    return (
        ValidateM30ControlPolicy(
            manifest_authenticator=authenticator,
            manifest_loader=manifest_loader,
            policy_loader=FileM30ControlPolicy(repository, policy_path),
            clock=resolved_clock,
        ),
        repository,
        manifest_path,
        policy_path,
    )
