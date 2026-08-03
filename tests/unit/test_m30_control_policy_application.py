"""M30 Phase-1b external policy loading, binding and reporting regressions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from tests.m30_campaign_support import VERIFICATION_TIME, build_manifest, write_manifest
from tests.m30_control_policy_support import build_control_policy, write_control_policy
from tests.m30_readiness_support import build_candidate_repository

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
    first[1].write_text("different\n", encoding="utf-8")
    with pytest.raises(ProductionEvidenceError) as rejected:
        writer.write(report, output)
    assert rejected.value.code.value == "m30_control_policy_report_write_failed"


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
