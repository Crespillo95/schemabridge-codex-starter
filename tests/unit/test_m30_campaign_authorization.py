"""M30 Phase-1a manifest, trust-boundary, and authentication regressions."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.m30_campaign_support import VERIFICATION_TIME, build_manifest, write_manifest
from tests.m30_readiness_support import build_candidate_repository

import schemabridge.adapters.evaluation.m30_campaign as campaign_adapter
from schemabridge.adapters.evaluation.m30_campaign import (
    FileM30CampaignManifest,
    FileM30ManifestAuthenticationReportWriter,
    GitHubCliM30ManifestAuthenticator,
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
from schemabridge.application.production_readiness import AssessM30Readiness
from schemabridge.domain.production_campaign import (
    M30_GITHUB_ATTESTATION_PREDICATE,
    M30_GITHUB_CLI_EXECUTABLE_SHA256,
    M30_GITHUB_CLI_VERSION,
    M30_GITHUB_OIDC_ISSUER,
    M30_GITHUB_REPOSITORY,
    M30_GITHUB_SIGNER_WORKFLOW,
    M30AuthenticatedManifest,
    M30CampaignBlockReason,
    M30CampaignManifest,
    M30ManifestAuthenticationState,
)
from schemabridge.domain.production_readiness import M30ReleaseDecision


@dataclass
class _Authenticator:
    calls: int = 0

    def authenticate(
        self,
        loaded: LoadedM30CampaignManifest,
        *,
        verified_at: datetime,
    ) -> M30AuthenticatedManifest:
        self.calls += 1
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


@dataclass(frozen=True, slots=True)
class _FixedClock:
    instant: datetime = VERIFICATION_TIME

    def now(self) -> datetime:
        return self.instant


@dataclass(slots=True)
class _SequenceClock:
    instants: list[datetime]

    def now(self) -> datetime:
        return self.instants.pop(0)


def test_exact_manifest_authenticates_only_inputs_and_keeps_execution_blocked(
    tmp_path: Path,
) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    manifest_path = write_manifest(tmp_path / "external/manifest.json", build_manifest(repository))
    authenticator = _Authenticator()

    report = _authentication_service(repository, manifest_path, authenticator).execute()

    assert report.state is M30ManifestAuthenticationState.AUTHENTICATED
    assert report.campaign_executable is False
    assert report.release_decision is M30ReleaseDecision.NO_GO
    assert report.workflow_attested_manifest_authenticated is True
    assert report.external_controls_passed == 0
    assert report.external_controls_remaining == 24
    assert report.synthetic_evidence_accepted_as_operated is False
    assert authenticator.calls == 1
    assert all(
        gate.status.value == "missing_external"
        for gate in report.preflight.gates
        if gate.evidence_class.value != "repository_contract"
    )


def test_candidate_must_be_ready_before_manifest_or_trust_provider_is_touched(
    tmp_path: Path,
) -> None:
    repository = build_candidate_repository(
        tmp_path / "candidate",
        branch="feature/m30",
        annotated_tag=None,
    )
    authenticator = _Authenticator()

    report = _authentication_service(
        repository,
        tmp_path / "external/missing.json",
        authenticator,
    ).execute()

    assert report.state is M30ManifestAuthenticationState.BLOCKED
    assert report.blocking_reasons == (M30CampaignBlockReason.CANDIDATE_NOT_READY,)
    assert report.campaign_executable is False
    assert report.manifest_sha256 is None
    assert authenticator.calls == 0


def test_wrong_subject_and_validity_window_block_without_authentication(tmp_path: Path) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    exact = build_manifest(repository)
    wrong_candidate = exact.candidate.model_copy(update={"revision": "f" * 40})
    wrong = exact.model_copy(update={"candidate": wrong_candidate})
    wrong_path = write_manifest(tmp_path / "external/wrong.json", wrong)
    authenticator = _Authenticator()

    mismatch = _authentication_service(repository, wrong_path, authenticator).execute()

    assert mismatch.blocking_reasons == (M30CampaignBlockReason.MANIFEST_SUBJECT_MISMATCH,)
    assert mismatch.campaign_executable is False
    assert authenticator.calls == 0

    future = build_manifest(
        repository,
        not_before=VERIFICATION_TIME + timedelta(minutes=1),
        expires_at=VERIFICATION_TIME + timedelta(days=1),
    )
    future_path = write_manifest(tmp_path / "external/future.json", future)
    too_early = _authentication_service(repository, future_path, authenticator).execute()
    assert too_early.blocking_reasons == (M30CampaignBlockReason.MANIFEST_NOT_YET_VALID,)

    expired = build_manifest(
        repository,
        not_before=VERIFICATION_TIME - timedelta(minutes=30),
        expires_at=VERIFICATION_TIME - timedelta(seconds=1),
    )
    expired_path = write_manifest(tmp_path / "external/expired.json", expired)
    too_late = _authentication_service(repository, expired_path, authenticator).execute()
    assert too_late.blocking_reasons == (M30CampaignBlockReason.MANIFEST_EXPIRED,)
    assert authenticator.calls == 0


def test_manifest_expiring_during_authentication_is_blocked_at_completion(
    tmp_path: Path,
) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    manifest = build_manifest(
        repository,
        not_before=VERIFICATION_TIME - timedelta(minutes=1),
        expires_at=VERIFICATION_TIME + timedelta(seconds=1),
    )
    manifest_path = write_manifest(tmp_path / "external/manifest.json", manifest)
    authenticator = _Authenticator()
    clock = _SequenceClock(
        [
            VERIFICATION_TIME,
            VERIFICATION_TIME + timedelta(seconds=2),
        ]
    )

    report = _authentication_service(
        repository,
        manifest_path,
        authenticator,
        clock=clock,
    ).execute()

    assert report.state is M30ManifestAuthenticationState.BLOCKED
    assert report.blocking_reasons == (M30CampaignBlockReason.MANIFEST_EXPIRED,)
    assert report.authentication is None
    assert report.workflow_attested_manifest_authenticated is False
    assert authenticator.calls == 1


def test_manifest_rejects_weakened_corpus_controls_and_local_pass_claim(tmp_path: Path) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    manifest = build_manifest(repository)
    payload = manifest.model_dump(mode="python")
    payload["corpus"]["slices"][0]["spanish_cases"] -= 1
    with pytest.raises(
        ValidationError,
        match=r"balanced between Spanish and English|1,000-case contract",
    ):
        M30CampaignManifest.model_validate(payload)

    payload = manifest.model_dump(mode="python")
    simple = [
        item for item in payload["corpus"]["slices"] if item["case_class"] == "supported_simple"
    ]
    simple[0]["spanish_cases"] -= 1
    simple[1]["spanish_cases"] += 1
    with pytest.raises(ValidationError, match="balanced between Spanish and English"):
        M30CampaignManifest.model_validate(payload)

    payload = manifest.model_dump(mode="python")
    critical = next(
        item
        for item in payload["corpus"]["slices"]
        if item["case_class"] == "supported_advanced" and item["risk"] == "critical"
    )
    critical["risk"] = "high"
    with pytest.raises(ValidationError, match="class/family/risk matrix"):
        M30CampaignManifest.model_validate(payload)

    payload = manifest.model_dump(mode="python")
    payload["controls"][0]["evidence_class"] = "repository_contract"
    with pytest.raises(ValidationError, match="24-control contract"):
        M30CampaignManifest.model_validate(payload)

    payload = manifest.model_dump(mode="python")
    for item in payload["corpus"]["slices"]:
        item["risk"] = "standard"
    with pytest.raises(ValidationError, match=r"risk does not match|class/family/risk matrix"):
        M30CampaignManifest.model_validate(payload)

    payload = manifest.model_dump(mode="python")
    ambiguous = next(
        item for item in payload["corpus"]["slices"] if item["case_class"] == "ambiguous"
    )
    ambiguous["family"] = "projection"
    with pytest.raises(ValidationError, match="class/family/risk matrix"):
        M30CampaignManifest.model_validate(payload)

    payload = manifest.model_dump(mode="python")
    payload["owners"][1]["approval_key_fingerprint"] = payload["owners"][0][
        "approval_key_fingerprint"
    ]
    with pytest.raises(ValidationError, match="distinct owner and approval key"):
        M30CampaignManifest.model_validate(payload)

    payload = manifest.model_dump(mode="python")
    payload["controls"][0]["responsible_authority"] = "authority:unknown"
    with pytest.raises(ValidationError, match="responsibility differs"):
        M30CampaignManifest.model_validate(payload)

    payload = manifest.model_dump(mode="json")
    payload["status"] = "passed"
    path = tmp_path / "external/status.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n",
        encoding="ascii",
    )
    with pytest.raises(ProductionEvidenceError) as failure:
        FileM30CampaignManifest(repository, path).load()
    assert failure.value.code.value == "m30_manifest_invalid"


@pytest.mark.parametrize(
    "mutation", ("noncanonical", "duplicate", "oversized", "internal", "symlink")
)
def test_manifest_file_boundary_fails_closed(
    tmp_path: Path,
    mutation: str,
) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    manifest = build_manifest(repository)
    external = tmp_path / "external"
    external.mkdir()
    path = external / "manifest.json"
    if mutation == "noncanonical":
        path.write_bytes(b" " + manifest.canonical_bytes())
    elif mutation == "duplicate":
        path.write_text('{"schema_version":1,"schema_version":1}\n', encoding="ascii")
    elif mutation == "oversized":
        path.write_bytes(b"x" * (campaign_adapter._MAX_MANIFEST_BYTES + 1))
    elif mutation == "internal":
        path = write_manifest(repository / "manifest.json", manifest)
    elif mutation == "symlink":
        target = write_manifest(external / "real.json", manifest)
        path.symlink_to(target)

    with pytest.raises(ProductionEvidenceError) as failure:
        FileM30CampaignManifest(repository, path).load()
    assert failure.value.code.value == "m30_manifest_invalid"


def test_github_authenticator_uses_only_the_exact_detached_bundle_hosted_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    manifest_path = write_manifest(tmp_path / "external/manifest.json", build_manifest(repository))
    bundle_path = tmp_path / "external/bundle.jsonl"
    bundle_path.write_text('{"synthetic":"bundle"}\n', encoding="ascii")
    loaded = FileM30CampaignManifest(repository, manifest_path).load()
    calls: list[tuple[str, ...]] = []

    def run(_root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        calls.append(arguments)
        output = (
            f"gh version {M30_GITHUB_CLI_VERSION} (test)\n".encode()
            if arguments == ("--version",)
            else _verification_summary()
        )
        return subprocess.CompletedProcess(arguments, 0, output, b"")

    monkeypatch.setattr(campaign_adapter, "_run_safe_gh", run)
    authenticated = GitHubCliM30ManifestAuthenticator(
        repository,
        manifest_path,
        bundle_path,
    ).authenticate(loaded, verified_at=VERIFICATION_TIME)

    assert authenticated.manifest_sha256 == loaded.raw_sha256
    command = calls[1]
    assert command[:2] == ("attestation", "verify")
    assert command[2] != str(manifest_path.resolve())
    assert _flag_value(command, "--bundle") != str(bundle_path.resolve())
    assert _flag_value(command, "--repo") == M30_GITHUB_REPOSITORY
    assert _flag_value(command, "--signer-workflow") == M30_GITHUB_SIGNER_WORKFLOW
    assert _flag_value(command, "--source-digest") == loaded.manifest.candidate.revision
    assert _flag_value(command, "--signer-digest") == loaded.manifest.candidate.revision
    assert _flag_value(command, "--source-ref") == "refs/tags/v0.1.0"
    assert "--deny-self-hosted-runners" in command
    assert "--bundle" in command
    assert _flag_value(command, "--hostname") == "github.com"
    assert "--owner" not in command
    assert _flag_value(command, "--jq") == campaign_adapter._VERIFICATION_SUMMARY_JQ
    assert (
        authenticated.attestation_bundle_sha256
        == hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    )
    assert authenticated.trusted_timestamp_count == 1


def test_wrong_gh_version_and_toctou_never_authenticate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    manifest_path = write_manifest(tmp_path / "external/manifest.json", build_manifest(repository))
    bundle_path = tmp_path / "external/bundle.jsonl"
    bundle_path.write_text('{"synthetic":"bundle"}\n', encoding="ascii")
    loaded = FileM30CampaignManifest(repository, manifest_path).load()

    monkeypatch.setattr(
        campaign_adapter,
        "_run_safe_gh",
        lambda _root, *_arguments: subprocess.CompletedProcess(
            (), 0, b"gh version 0.0.1 (wrong)\n", b""
        ),
    )
    with pytest.raises(ProductionEvidenceError) as unavailable:
        GitHubCliM30ManifestAuthenticator(repository, manifest_path, bundle_path).authenticate(
            loaded, verified_at=VERIFICATION_TIME
        )
    assert unavailable.value.code.value == "m30_trust_provider_unavailable"

    calls = 0

    def mutate(_root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        nonlocal calls
        calls += 1
        if arguments == ("--version",):
            return subprocess.CompletedProcess(
                arguments,
                0,
                f"gh version {M30_GITHUB_CLI_VERSION} (test)\n".encode(),
                b"",
            )
        manifest_path.write_bytes(loaded.manifest.canonical_bytes() + b" ")
        return subprocess.CompletedProcess(arguments, 0, _verification_summary(), b"")

    monkeypatch.setattr(campaign_adapter, "_run_safe_gh", mutate)
    with pytest.raises(ProductionEvidenceError) as tampered:
        GitHubCliM30ManifestAuthenticator(repository, manifest_path, bundle_path).authenticate(
            loaded, verified_at=VERIFICATION_TIME
        )
    assert tampered.value.code.value == "m30_manifest_authentication_failed"
    assert calls == 2


def test_authorization_report_writer_is_stable_and_external_overwrite_fails(
    tmp_path: Path,
) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    manifest_path = write_manifest(tmp_path / "external/manifest.json", build_manifest(repository))
    report = _authentication_service(repository, manifest_path, _Authenticator()).execute()
    writer = FileM30ManifestAuthenticationReportWriter(repository)
    output = tmp_path / "report"

    first = writer.write(report, output)
    second = writer.write(report, output)

    assert first == second
    payload = json.loads(first[0].read_text(encoding="utf-8"))
    markdown = first[1].read_text(encoding="utf-8")
    assert payload["report_sha256"] == report.fingerprint()
    assert payload["report"]["schema_version"] == 2
    assert report.completed_at.isoformat() in markdown
    assert report.control_policy_id in markdown
    assert report.control_policy_sha256 in markdown
    first[1].write_text("different\n", encoding="utf-8")
    with pytest.raises(ProductionEvidenceError) as overwrite:
        writer.write(report, output)
    assert overwrite.value.code.value == "m30_report_write_failed"


def _authentication_service(
    repository: Path,
    manifest_path: Path,
    authenticator: _Authenticator,
    *,
    clock: M30ClockPort | None = None,
) -> AuthenticateM30CampaignManifest:
    contract = FileM30CampaignContract(repository)
    return AuthenticateM30CampaignManifest(
        validator=ValidateM30CampaignManifest(
            readiness=AssessM30Readiness(
                contract_loader=contract,
                candidate_identity=GitM30CandidateIdentity(repository),
            ),
            contract_loader=contract,
            manifest_loader=FileM30CampaignManifest(repository, manifest_path),
        ),
        authenticator=authenticator,
        clock=clock or _FixedClock(),
    )


def _flag_value(command: tuple[str, ...], flag: str) -> str:
    return command[command.index(flag) + 1]


def _verification_summary() -> bytes:
    return (
        json.dumps(
            {
                "certificate": {"issuer": "synthetic-github-oidc"},
                "count": 1,
                "trustedTimestamps": [{"type": "synthetic-trusted-timestamp"}],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
