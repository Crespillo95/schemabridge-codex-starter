"""Fail-closed CLI and trust-provider branches for M30 Phase 1a."""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import scripts.m30_authenticate_campaign_manifest as authentication_cli
import scripts.m30_validate_campaign_manifest as validation_cli
from tests.m30_campaign_support import VERIFICATION_TIME, build_manifest, write_manifest
from tests.m30_readiness_support import build_candidate_repository

import schemabridge.adapters.evaluation.m30_campaign as campaign_adapter
from schemabridge.adapters.evaluation.m30_campaign import (
    FileM30CampaignManifest,
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
)


class _Clock:
    @classmethod
    def now(cls, tz: object = None) -> datetime:
        return VERIFICATION_TIME


@dataclass(frozen=True, slots=True)
class _FixedClock:
    instant: datetime = VERIFICATION_TIME

    def now(self) -> datetime:
        return self.instant


def test_validation_cli_prints_closed_json_schema_and_rejects_mixed_mode(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert validation_cli.main(["--print-json-schema"]) == 0

    schema = json.loads(capsys.readouterr().out)
    assert schema["title"] == "M30CampaignManifest"
    assert schema["additionalProperties"] is False
    assert {
        "artifacts",
        "candidate",
        "controls",
        "corpus",
        "owners",
        "provider",
        "target",
    } <= set(schema["required"])
    assert "status" not in schema["properties"]

    assert (
        validation_cli.main(["--print-json-schema", "--manifest", "/external/manifest.json"]) == 3
    )
    assert "cannot be combined" in capsys.readouterr().out

    assert validation_cli.main([]) == 3
    assert "--manifest is required" in capsys.readouterr().out


def test_validation_cli_has_stable_zero_two_three_exit_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(validation_cli, "datetime", _Clock)
    repository = build_candidate_repository(tmp_path / "candidate")
    manifest_path = write_manifest(
        tmp_path / "external/manifest.json",
        build_manifest(repository),
    )

    assert (
        validation_cli.main(
            [
                "--repository-root",
                str(repository),
                "--manifest",
                str(manifest_path),
            ]
        )
        == 0
    )
    success = capsys.readouterr().out
    assert "canonical and candidate-bound but not authenticated" in success
    assert "sha256=" in success

    blocked_repository = build_candidate_repository(
        tmp_path / "blocked-candidate",
        branch="feature/m30",
        annotated_tag=None,
    )
    assert (
        validation_cli.main(
            [
                "--repository-root",
                str(blocked_repository),
                "--manifest",
                str(tmp_path / "external/not-read.json"),
            ]
        )
        == 2
    )
    assert capsys.readouterr().out.strip().endswith("candidate_not_ready")

    invalid_path = tmp_path / "external/invalid.json"
    invalid_path.write_text('{"status":"passed"}\n', encoding="ascii")
    assert (
        validation_cli.main(
            [
                "--repository-root",
                str(repository),
                "--manifest",
                str(invalid_path),
            ]
        )
        == 3
    )
    assert capsys.readouterr().out.strip().endswith("m30_manifest_invalid")


def test_authentication_cli_returns_two_for_blocked_and_three_for_boundary_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(campaign_adapter, "datetime", _Clock)
    blocked_repository = build_candidate_repository(
        tmp_path / "blocked-candidate",
        branch="feature/m30",
        annotated_tag=None,
    )
    blocked_output = tmp_path / "blocked-report"

    assert (
        authentication_cli.main(
            [
                "--repository-root",
                str(blocked_repository),
                "--manifest",
                str(tmp_path / "external/not-read.json"),
                "--attestation-bundle",
                str(tmp_path / "external/not-read.jsonl"),
                "--output-directory",
                str(blocked_output),
            ]
        )
        == 2
    )
    blocked_stdout = capsys.readouterr().out
    assert "Phase 1a blocked" in blocked_stdout
    blocked_report = json.loads(
        (blocked_output / "authentication.json").read_text(encoding="utf-8")
    )["report"]
    assert blocked_report["blocking_reasons"] == ["candidate_not_ready"]
    assert blocked_report["workflow_attested_manifest_authenticated"] is False

    repository = build_candidate_repository(tmp_path / "candidate")
    manifest_path = write_manifest(
        tmp_path / "external/manifest.json",
        build_manifest(repository),
    )
    failed_output = tmp_path / "failed-report"
    assert (
        authentication_cli.main(
            [
                "--repository-root",
                str(repository),
                "--manifest",
                str(manifest_path),
                "--attestation-bundle",
                str(tmp_path / "external/missing-bundle.jsonl"),
                "--output-directory",
                str(failed_output),
            ]
        )
        == 3
    )
    assert capsys.readouterr().out.strip().endswith("m30_manifest_unavailable")
    assert not failed_output.exists()


@pytest.mark.parametrize("trust_path", ("absent", "world-writable"))
def test_authenticator_rejects_absent_or_untrusted_fixed_gh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trust_path: str,
) -> None:
    repository, manifest_path, bundle_path, loaded = _authentication_inputs(tmp_path)
    fake_gh: Path | None = None
    if trust_path == "world-writable":
        fake_gh = tmp_path / "bin/gh"
        fake_gh.parent.mkdir()
        fake_gh.write_text("not an executable trust provider\n", encoding="ascii")
        fake_gh.chmod(0o777)

    def which(executable: str, *, path: str | None = None) -> str | None:
        assert executable == "gh"
        assert path == campaign_adapter._GH_SEARCH_PATH
        return None if fake_gh is None else str(fake_gh)

    monkeypatch.setattr(shutil, "which", which)

    with pytest.raises(ProductionEvidenceError) as failure:
        GitHubCliM30ManifestAuthenticator(
            repository,
            manifest_path,
            bundle_path,
        ).authenticate(loaded, verified_at=VERIFICATION_TIME)
    assert failure.value.code.value == "m30_trust_provider_unavailable"


def test_fixed_gh_requires_the_reviewed_official_executable_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_gh = tmp_path / "bin/gh"
    fake_gh.parent.mkdir()
    fake_gh.write_bytes(b"unreviewed-gh-binary")
    fake_gh.chmod(0o755)
    monkeypatch.setattr(
        shutil,
        "which",
        lambda executable, *, path=None: str(fake_gh),
    )

    with pytest.raises(campaign_adapter._TrustProviderUnavailable, match="official release"):
        campaign_adapter._trusted_gh_executable()


def test_fixed_gh_accepts_only_a_matching_platform_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_gh = tmp_path / "bin/gh"
    fake_gh.parent.mkdir()
    fake_gh.write_bytes(b"reviewed-test-gh-binary")
    fake_gh.chmod(0o755)
    machine = platform.machine()
    if machine in {"aarch64", "arm64"}:
        machine = "arm64"
    elif machine in {"amd64", "x86_64"}:
        machine = "x86_64"
    platform_label = campaign_adapter._GH_PLATFORM_LABELS[(platform.system(), machine)]
    monkeypatch.setattr(
        campaign_adapter,
        "_GH_OFFICIAL_EXECUTABLE_SHA256",
        {platform_label: hashlib.sha256(fake_gh.read_bytes()).hexdigest()},
    )
    monkeypatch.setattr(
        shutil,
        "which",
        lambda executable, *, path=None: str(fake_gh),
    )

    assert campaign_adapter._trusted_gh_executable() == str(fake_gh.resolve())


def test_safe_gh_executes_verified_snapshot_when_original_path_is_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted_bytes = b"#!/bin/sh\nprintf 'trusted-snapshot\\n'\n"
    substituted_bytes = b"#!/bin/sh\nprintf 'substituted-path\\n'\n"
    fake_gh = tmp_path / "bin/gh"
    fake_gh.parent.mkdir()
    fake_gh.write_bytes(trusted_bytes)
    fake_gh.chmod(0o755)
    machine = platform.machine()
    if machine in {"aarch64", "arm64"}:
        machine = "arm64"
    elif machine in {"amd64", "x86_64"}:
        machine = "x86_64"
    platform_label = campaign_adapter._GH_PLATFORM_LABELS[(platform.system(), machine)]
    monkeypatch.setattr(
        campaign_adapter,
        "_GH_OFFICIAL_EXECUTABLE_SHA256",
        {platform_label: hashlib.sha256(trusted_bytes).hexdigest()},
    )
    monkeypatch.setattr(
        shutil,
        "which",
        lambda executable, *, path=None: str(fake_gh),
    )
    write_snapshot = campaign_adapter._write_private_executable_snapshot

    def replace_source_after_snapshot(path: Path, payload: bytes) -> None:
        write_snapshot(path, payload)
        fake_gh.write_bytes(substituted_bytes)
        fake_gh.chmod(0o755)

    monkeypatch.setattr(
        campaign_adapter,
        "_write_private_executable_snapshot",
        replace_source_after_snapshot,
    )

    completed = campaign_adapter._run_safe_gh(tmp_path, "--version")

    assert completed.returncode == 0
    assert completed.stdout == b"trusted-snapshot\n"
    assert isinstance(completed.args, tuple)
    assert completed.args[0] != str(fake_gh.resolve())
    assert fake_gh.read_bytes() == substituted_bytes


@pytest.mark.parametrize(
    "summary",
    (
        b"not-json\n",
        b'{"certificate":{},"count":1,"trustedTimestamps":[{"type":"rekor"}]}\n',
        b'{"certificate":{"issuer":"oidc"},"count":0,"trustedTimestamps":[{"type":"rekor"}]}\n',
        b'{"certificate":{"issuer":"oidc"},"count":1,"trustedTimestamps":[]}\n',
        b'{"certificate":{"issuer":"oidc"},"count":1,"extra":true,"trustedTimestamps":[{"type":"rekor"}]}\n',
    ),
    ids=("not-json", "empty-certificate", "wrong-count", "no-timestamp", "extra-field"),
)
def test_authenticator_rejects_invalid_success_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    summary: bytes,
) -> None:
    repository, manifest_path, bundle_path, loaded = _authentication_inputs(tmp_path)

    def run(_root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        stdout = (
            f"gh version {M30_GITHUB_CLI_VERSION} (test)\n".encode()
            if arguments == ("--version",)
            else summary
        )
        return subprocess.CompletedProcess(arguments, 0, stdout, b"")

    monkeypatch.setattr(campaign_adapter, "_run_safe_gh", run)

    with pytest.raises(ProductionEvidenceError) as failure:
        GitHubCliM30ManifestAuthenticator(
            repository,
            manifest_path,
            bundle_path,
        ).authenticate(loaded, verified_at=VERIFICATION_TIME)
    assert failure.value.code.value == "m30_manifest_authentication_failed"


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    (
        ("manifest_sha256", "f" * 64),
        ("attestation_bundle_sha256", "not-a-sha256"),
        ("verification_summary_sha256", "not-a-sha256"),
        ("certificate_evidence_sha256", "not-a-sha256"),
        ("trusted_timestamps_sha256", "not-a-sha256"),
        ("trusted_timestamp_count", 0),
        ("source_repository", "attacker/repository"),
        ("source_revision", "f" * 40),
        ("source_ref", "refs/tags/v9.9.9"),
        ("signer_workflow", "attacker/workflow.yml"),
        ("signer_digest", "f" * 40),
        ("oidc_issuer", "https://attacker.invalid"),
        ("predicate_type", "https://attacker.invalid/predicate"),
        ("github_cli_version", "0.0.0"),
        ("github_cli_platform", "attacker-platform"),
        ("github_cli_executable_sha256", "f" * 64),
        ("github_hosted_runner_required", False),
        ("trusted_timestamp_verified", False),
        ("verified_at", VERIFICATION_TIME + timedelta(seconds=1)),
    ),
)
def test_authentication_service_rejects_mismatched_authenticated_facts(
    tmp_path: Path,
    field: str,
    wrong_value: object,
) -> None:
    repository = build_candidate_repository(tmp_path / "candidate")
    manifest_path = write_manifest(
        tmp_path / "external/manifest.json",
        build_manifest(repository),
    )

    with pytest.raises(ProductionEvidenceError) as failure:
        _authentication_service(
            repository,
            manifest_path,
            _FactAuthenticator(field=field, wrong_value=wrong_value),
        ).execute()
    assert failure.value.code.value == "m30_manifest_authentication_failed"


@dataclass(frozen=True, slots=True)
class _FactAuthenticator:
    field: str | None = None
    wrong_value: object | None = None

    def authenticate(
        self,
        loaded: LoadedM30CampaignManifest,
        *,
        verified_at: datetime,
    ) -> M30AuthenticatedManifest:
        candidate = loaded.manifest.candidate
        authenticated = M30AuthenticatedManifest(
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
        if self.field is None:
            return authenticated
        return authenticated.model_copy(update={self.field: self.wrong_value})


def _authentication_inputs(
    tmp_path: Path,
) -> tuple[Path, Path, Path, LoadedM30CampaignManifest]:
    repository = build_candidate_repository(tmp_path / "candidate")
    manifest_path = write_manifest(
        tmp_path / "external/manifest.json",
        build_manifest(repository),
    )
    bundle_path = tmp_path / "external/bundle.jsonl"
    bundle_path.write_text('{"synthetic":"bundle"}\n', encoding="ascii")
    loaded = FileM30CampaignManifest(repository, manifest_path).load()
    return repository, manifest_path, bundle_path, loaded


def _authentication_service(
    repository: Path,
    manifest_path: Path,
    authenticator: _FactAuthenticator,
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
