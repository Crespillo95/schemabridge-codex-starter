"""Filesystem and GitHub-attestation adapters for M30 Phase 1a."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import TypedDict

from pydantic import ValidationError

from schemabridge.adapters.evaluation.m30_readiness import _run_safe_git
from schemabridge.application.ports.production_evidence import (
    LoadedM30CampaignManifest,
    LoadedM30ControlPolicy,
    ProductionEvidenceError,
    ProductionEvidenceErrorCode,
)
from schemabridge.domain.production_campaign import (
    M30_GITHUB_ATTESTATION_PREDICATE,
    M30_GITHUB_CLI_EXECUTABLE_SHA256,
    M30_GITHUB_CLI_VERSION,
    M30_GITHUB_OIDC_ISSUER,
    M30_GITHUB_REPOSITORY,
    M30_GITHUB_SIGNER_WORKFLOW,
    M30AuthenticatedManifest,
    M30CampaignManifest,
    M30ManifestAuthenticationReport,
)
from schemabridge.domain.production_campaign_receipts import (
    M30ControlPolicy,
    M30ControlPolicyValidationReport,
)

# 45 KiB encodes to 61,440 base64 characters and leaves bounded headroom below
# GitHub workflow_dispatch's 65,535-character aggregate inputs limit.
_MAX_MANIFEST_BYTES = 45 * 1024
_MAX_CONTROL_POLICY_BYTES = 256 * 1024
_MAX_BUNDLE_BYTES = 8 * 1024 * 1024
_MAX_REPORT_BYTES = 2 * 1024 * 1024
_MAX_JSON_NODES = 20_000
_MAX_JSON_DEPTH = 24
_GH_TIMEOUT_SECONDS = 30.0
_GH_SEARCH_PATH = os.pathsep.join(("/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"))
_GH_PLATFORM_LABELS = MappingProxyType(
    {
        ("Darwin", "arm64"): "macos-arm64",
        ("Darwin", "x86_64"): "macos-amd64",
        ("Linux", "arm64"): "linux-arm64",
        ("Linux", "x86_64"): "linux-amd64",
    }
)
# Kept as a local immutable alias so the trust-boundary implementation and its mutation tests name
# the exact bytes they enforce. Package-manager rebuilds are intentionally not accepted.
_GH_OFFICIAL_EXECUTABLE_SHA256 = M30_GITHUB_CLI_EXECUTABLE_SHA256
_VERIFICATION_SUMMARY_JQ = (
    "{count: length, certificate: .[0].verificationResult.signature.certificate, "
    "trustedTimestamps: .[0].verificationResult.verifiedTimestamps}"
)


class _VerificationSummary(TypedDict):
    count: int
    certificate: dict[str, object]
    trustedTimestamps: list[dict[str, object]]


class SystemM30Clock:
    """Provide trusted UTC instants at the M30 application boundary."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FileM30CampaignManifest:
    """Load canonical campaign bytes only from outside the candidate source tree."""

    def __init__(self, repository_root: Path, manifest_path: Path) -> None:
        self._root = repository_root.resolve()
        self._path = manifest_path

    def load(self) -> LoadedM30CampaignManifest:
        try:
            raw = _read_external_regular_file(
                self._path,
                repository_root=self._root,
                maximum_bytes=_MAX_MANIFEST_BYTES,
            )
            if not raw or b"\x00" in raw:
                raise ValueError("manifest bytes are empty or contain NUL")
            payload = json.loads(raw.decode("ascii"), object_pairs_hook=_unique_json_object)
            _validate_json_shape(payload)
            manifest = M30CampaignManifest.model_validate(payload)
            if raw != manifest.canonical_bytes():
                raise ValueError("manifest is not exact canonical JSON")
            return LoadedM30CampaignManifest(
                manifest=manifest,
                raw_sha256=hashlib.sha256(raw).hexdigest(),
            )
        except FileNotFoundError as error:
            raise ProductionEvidenceError(
                ProductionEvidenceErrorCode.MANIFEST_UNAVAILABLE,
                "M30 campaign manifest is unavailable",
            ) from error
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            RecursionError,
            ValidationError,
            ValueError,
        ) as error:
            raise ProductionEvidenceError(
                ProductionEvidenceErrorCode.MANIFEST_INVALID,
                "M30 campaign manifest is invalid",
            ) from error


class FileM30ControlPolicy:
    """Load one canonical external control policy without trusting candidate fixtures."""

    def __init__(self, repository_root: Path, policy_path: Path) -> None:
        self._root = repository_root.resolve()
        self._path = policy_path

    def load(self) -> LoadedM30ControlPolicy:
        try:
            raw = _read_external_regular_file(
                self._path,
                repository_root=self._root,
                maximum_bytes=_MAX_CONTROL_POLICY_BYTES,
            )
            if not raw or b"\x00" in raw:
                raise ValueError("control policy bytes are empty or contain NUL")
            payload = json.loads(raw.decode("ascii"), object_pairs_hook=_unique_json_object)
            _validate_json_shape(payload)
            policy = M30ControlPolicy.model_validate(payload)
            if raw != policy.canonical_bytes():
                raise ValueError("control policy is not exact canonical JSON")
            return LoadedM30ControlPolicy(
                policy=policy,
                raw_sha256=hashlib.sha256(raw).hexdigest(),
            )
        except FileNotFoundError as error:
            raise ProductionEvidenceError(
                ProductionEvidenceErrorCode.CONTROL_POLICY_UNAVAILABLE,
                "M30 external control policy is unavailable",
            ) from error
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            RecursionError,
            ValidationError,
            ValueError,
        ) as error:
            raise ProductionEvidenceError(
                ProductionEvidenceErrorCode.CONTROL_POLICY_INVALID,
                "M30 external control policy is invalid",
            ) from error


class GitHubCliM30ManifestAuthenticator:
    """Verify one detached GitHub attestation bundle under an exact hosted policy."""

    def __init__(self, repository_root: Path, manifest_path: Path, bundle_path: Path) -> None:
        self._root = repository_root.resolve()
        self._manifest_path = manifest_path
        self._bundle_path = bundle_path

    def authenticate(
        self,
        loaded: LoadedM30CampaignManifest,
        *,
        verified_at: datetime,
    ) -> M30AuthenticatedManifest:
        manifest = loaded.manifest
        source_ref = f"refs/tags/v{manifest.candidate.package_version}"
        try:
            manifest_before = _read_external_regular_file(
                self._manifest_path,
                repository_root=self._root,
                maximum_bytes=_MAX_MANIFEST_BYTES,
            )
            bundle_before = _read_external_regular_file(
                self._bundle_path,
                repository_root=self._root,
                maximum_bytes=_MAX_BUNDLE_BYTES,
            )
            if hashlib.sha256(manifest_before).hexdigest() != loaded.raw_sha256:
                raise ValueError("manifest changed after canonical loading")
            version = _run_safe_gh(self._root, "--version")
            expected_version = f"gh version {M30_GITHUB_CLI_VERSION} "
            try:
                observed_version = version.stdout.decode("ascii")
            except UnicodeError as error:
                raise _TrustProviderUnavailable("GitHub CLI version output is invalid") from error
            if version.returncode != 0 or not observed_version.startswith(expected_version):
                raise _TrustProviderUnavailable("GitHub CLI version differs")
            with tempfile.TemporaryDirectory(
                prefix="schemabridge-m30-auth-",
                dir=_trusted_temporary_parent(),
            ) as snapshot_name:
                snapshot = Path(snapshot_name).resolve(strict=True)
                manifest_snapshot = snapshot / "manifest.json"
                bundle_snapshot = snapshot / "attestation.jsonl"
                _write_private_snapshot(manifest_snapshot, manifest_before)
                _write_private_snapshot(bundle_snapshot, bundle_before)
                verification = _run_safe_gh(
                    self._root,
                    "attestation",
                    "verify",
                    str(manifest_snapshot),
                    "--bundle",
                    str(bundle_snapshot),
                    "--hostname",
                    "github.com",
                    "--repo",
                    M30_GITHUB_REPOSITORY,
                    "--signer-workflow",
                    M30_GITHUB_SIGNER_WORKFLOW,
                    "--signer-digest",
                    manifest.candidate.revision,
                    "--source-digest",
                    manifest.candidate.revision,
                    "--source-ref",
                    source_ref,
                    "--cert-oidc-issuer",
                    M30_GITHUB_OIDC_ISSUER,
                    "--predicate-type",
                    M30_GITHUB_ATTESTATION_PREDICATE,
                    "--deny-self-hosted-runners",
                    "--format",
                    "json",
                    "--limit",
                    "1",
                    "--jq",
                    _VERIFICATION_SUMMARY_JQ,
                )
                if (
                    manifest_snapshot.read_bytes() != manifest_before
                    or bundle_snapshot.read_bytes() != bundle_before
                ):
                    raise ValueError("private M30 authentication snapshot changed")
            if verification.returncode != 0:
                raise ValueError("GitHub attestation verification failed")
            summary = _verified_summary(verification.stdout)
            manifest_after = _read_external_regular_file(
                self._manifest_path,
                repository_root=self._root,
                maximum_bytes=_MAX_MANIFEST_BYTES,
            )
            bundle_after = _read_external_regular_file(
                self._bundle_path,
                repository_root=self._root,
                maximum_bytes=_MAX_BUNDLE_BYTES,
            )
            if manifest_after != manifest_before or bundle_after != bundle_before:
                raise ValueError("M30 authentication inputs changed during verification")
            github_cli_platform, github_cli_digest = _reviewed_gh_identity()
            return M30AuthenticatedManifest(
                manifest_sha256=loaded.raw_sha256,
                attestation_bundle_sha256=hashlib.sha256(bundle_before).hexdigest(),
                verification_summary_sha256=_canonical_json_sha256(summary),
                certificate_evidence_sha256=_canonical_json_sha256(summary["certificate"]),
                trusted_timestamps_sha256=_canonical_json_sha256(summary["trustedTimestamps"]),
                trusted_timestamp_count=len(summary["trustedTimestamps"]),
                source_repository=M30_GITHUB_REPOSITORY,
                source_revision=manifest.candidate.revision,
                source_ref=source_ref,
                signer_workflow=M30_GITHUB_SIGNER_WORKFLOW,
                signer_digest=manifest.candidate.revision,
                oidc_issuer=M30_GITHUB_OIDC_ISSUER,
                predicate_type=M30_GITHUB_ATTESTATION_PREDICATE,
                github_cli_version=M30_GITHUB_CLI_VERSION,
                github_cli_platform=github_cli_platform,
                github_cli_executable_sha256=github_cli_digest,
                github_hosted_runner_required=True,
                trusted_timestamp_verified=True,
                verified_at=verified_at,
            )
        except _TrustProviderUnavailable as error:
            raise ProductionEvidenceError(
                ProductionEvidenceErrorCode.TRUST_PROVIDER_UNAVAILABLE,
                "M30 GitHub attestation trust provider is unavailable",
            ) from error
        except FileNotFoundError as error:
            raise ProductionEvidenceError(
                ProductionEvidenceErrorCode.MANIFEST_UNAVAILABLE,
                "M30 manifest or attestation bundle is unavailable",
            ) from error
        except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as error:
            raise ProductionEvidenceError(
                ProductionEvidenceErrorCode.MANIFEST_AUTHENTICATION_FAILED,
                "M30 campaign manifest authentication failed",
            ) from error


class FileM30ManifestAuthenticationReportWriter:
    """Write the Phase-1a report to the canonical ignored or an external directory."""

    def __init__(self, repository_root: Path) -> None:
        self._root = repository_root.resolve()
        self._canonical_destination = self._root / ".local/m30/manifest-authentication"

    def write(
        self,
        report: M30ManifestAuthenticationReport,
        output_directory: Path,
    ) -> tuple[Path, Path]:
        try:
            destination, directory_descriptor, candidate_local = _open_report_destination(
                output_directory,
                repository_root=self._root,
                canonical_destination=self._canonical_destination,
                ignored_report_paths=(
                    ".local/m30/manifest-authentication/authentication.md",
                    ".local/m30/manifest-authentication/authentication.json",
                ),
            )
            try:
                markdown = _render_markdown(report)
                payload = {
                    "bundle_schema_version": 1,
                    "markdown_sha256": hashlib.sha256(markdown.encode()).hexdigest(),
                    "report": report.model_dump(mode="json"),
                    "report_sha256": report.fingerprint(),
                }
                document = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
                _write_report_bundle_at(
                    directory_descriptor,
                    markdown_name="authentication.md",
                    markdown=markdown,
                    json_name="authentication.json",
                    document=document,
                    allow_replace=candidate_local,
                )
                _validate_report_bundle_and_path_at(
                    destination,
                    directory_descriptor,
                    files=(
                        ("authentication.md", markdown.encode("utf-8")),
                        ("authentication.json", document.encode("utf-8")),
                    ),
                )
                return destination / "authentication.json", destination / "authentication.md"
            finally:
                os.close(directory_descriptor)
        except (OSError, subprocess.SubprocessError) as error:
            raise ProductionEvidenceError(
                ProductionEvidenceErrorCode.REPORT_WRITE_FAILED,
                "M30 manifest authentication report could not be written",
            ) from error


class FileM30ControlPolicyReportWriter:
    """Write the bounded Phase-1b policy report without accepting control evidence."""

    def __init__(self, repository_root: Path) -> None:
        self._root = repository_root.resolve()
        self._canonical_destination = self._root / ".local/m30/control-policy"

    def write(
        self,
        report: M30ControlPolicyValidationReport,
        output_directory: Path,
    ) -> tuple[Path, Path]:
        try:
            destination, directory_descriptor, candidate_local = _open_report_destination(
                output_directory,
                repository_root=self._root,
                canonical_destination=self._canonical_destination,
                ignored_report_paths=(
                    ".local/m30/control-policy/policy-validation.md",
                    ".local/m30/control-policy/policy-validation.json",
                ),
            )
            try:
                markdown = _render_control_policy_markdown(report)
                payload = {
                    "bundle_schema_version": 1,
                    "markdown_sha256": hashlib.sha256(markdown.encode()).hexdigest(),
                    "report": report.model_dump(mode="json"),
                    "report_sha256": report.fingerprint(),
                }
                document = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
                _write_report_bundle_at(
                    directory_descriptor,
                    markdown_name="policy-validation.md",
                    markdown=markdown,
                    json_name="policy-validation.json",
                    document=document,
                    allow_replace=candidate_local,
                )
                _validate_report_bundle_and_path_at(
                    destination,
                    directory_descriptor,
                    files=(
                        ("policy-validation.md", markdown.encode("utf-8")),
                        ("policy-validation.json", document.encode("utf-8")),
                    ),
                )
                return destination / "policy-validation.json", destination / "policy-validation.md"
            finally:
                os.close(directory_descriptor)
        except (OSError, subprocess.SubprocessError) as error:
            raise ProductionEvidenceError(
                ProductionEvidenceErrorCode.CONTROL_POLICY_REPORT_WRITE_FAILED,
                "M30 control-policy report could not be written",
            ) from error


class _TrustProviderUnavailable(RuntimeError):
    pass


def _run_safe_gh(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    _source, executable_bytes = _load_trusted_gh_executable()
    with tempfile.TemporaryDirectory(
        prefix="schemabridge-m30-gh-",
        dir=_trusted_temporary_parent(),
    ) as private_name:
        private = Path(private_name).resolve(strict=True)
        private.chmod(0o700)
        executable = private / "gh"
        config = private / "config"
        config.mkdir(mode=0o700)
        _write_private_executable_snapshot(executable, executable_bytes)
        _validate_trusted_gh_executable(executable)
        executable_name = str(executable)
        try:
            process = subprocess.Popen(
                (executable_name, *arguments),
                cwd=root,
                env={
                    "GH_CONFIG_DIR": str(config),
                    "GH_HOST": "github.com",
                    "GH_NO_UPDATE_NOTIFIER": "1",
                    "GH_PROMPT_DISABLED": "1",
                    "NO_COLOR": "1",
                    "PATH": os.defpath,
                },
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError as error:
            raise _TrustProviderUnavailable("GitHub CLI is unavailable") from error
        stdout, stderr = _communicate_bounded(
            process,
            timeout_seconds=_GH_TIMEOUT_SECONDS,
            maximum_stdout_bytes=65_536,
            maximum_stderr_bytes=16_384,
        )
        _validate_trusted_gh_executable(executable)
    if len(stdout) > 65_536 or len(stderr) > 16_384:
        raise subprocess.SubprocessError("GitHub attestation verifier output exceeded its bound")
    return subprocess.CompletedProcess(
        args=(executable_name, *arguments),
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _communicate_bounded(
    process: subprocess.Popen[bytes],
    *,
    timeout_seconds: float,
    maximum_stdout_bytes: int,
    maximum_stderr_bytes: int,
) -> tuple[bytes, bytes]:
    stdout_stream = process.stdout
    stderr_stream = process.stderr
    stdout = bytearray()
    stderr = bytearray()
    selector: selectors.BaseSelector | None = None
    deadline = time.monotonic() + timeout_seconds
    try:
        selector = selectors.DefaultSelector()
        if stdout_stream is None or stderr_stream is None:
            raise subprocess.SubprocessError("bounded GitHub CLI pipes are unavailable")
        streams = (
            (stdout_stream, stdout, maximum_stdout_bytes),
            (stderr_stream, stderr, maximum_stderr_bytes),
        )
        for stream, target, limit in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, (target, limit))
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.SubprocessError("GitHub attestation verification timed out")
            events = selector.select(remaining)
            if not events:
                raise subprocess.SubprocessError("GitHub attestation verification timed out")
            for key, _mask in events:
                try:
                    target, limit = key.data
                    chunk = os.read(
                        key.fd,
                        min(64 * 1024, limit - len(target) + 1),
                    )
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target.extend(chunk)
                if len(target) > limit:
                    raise subprocess.SubprocessError(
                        "GitHub attestation verifier output exceeded its bound"
                    )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.SubprocessError("GitHub attestation verification timed out")
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            raise subprocess.SubprocessError("GitHub attestation verification timed out") from error
    except BaseException:
        _kill_process_group(process)
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired as cleanup_error:
                raise subprocess.SubprocessError(
                    "GitHub attestation verifier could not be reaped"
                ) from cleanup_error
        raise
    finally:
        if selector is not None:
            selector.close()
        if stdout_stream is not None:
            stdout_stream.close()
        if stderr_stream is not None:
            stderr_stream.close()
    return bytes(stdout), bytes(stderr)


def _trusted_gh_executable() -> str:
    resolved, _payload = _load_trusted_gh_executable()
    return str(resolved)


def _load_trusted_gh_executable() -> tuple[Path, bytes]:
    candidate = shutil.which("gh", path=_GH_SEARCH_PATH)
    if candidate is None:
        raise _TrustProviderUnavailable("GitHub CLI is absent from reviewed system paths")
    try:
        resolved = Path(candidate).resolve(strict=True)
    except OSError as error:
        raise _TrustProviderUnavailable("GitHub CLI path is unavailable") from error
    return resolved, _read_trusted_gh_executable(resolved)


def _reviewed_gh_identity() -> tuple[str, str]:
    machine = platform.machine()
    if machine in {"aarch64", "arm64"}:
        machine = "arm64"
    elif machine in {"amd64", "x86_64"}:
        machine = "x86_64"
    platform_label = _GH_PLATFORM_LABELS.get((platform.system(), machine))
    if platform_label is None:
        raise _TrustProviderUnavailable("GitHub CLI platform is outside the reviewed matrix")
    return platform_label, _GH_OFFICIAL_EXECUTABLE_SHA256[platform_label]


def _validate_trusted_gh_executable(path: Path) -> None:
    _read_trusted_gh_executable(path)


def _read_trusted_gh_executable(path: Path) -> bytes:
    _platform_label, expected = _reviewed_gh_identity()
    parent, directory_descriptor = _open_anchored_directory(
        path.parent,
        create=False,
        allow_owner_group_writable=True,
    )
    try:
        directory_before = os.fstat(directory_descriptor)
        _validate_trusted_directory(directory_before, allow_owner_group_writable=True)
        flags = (
            os.O_RDONLY
            | _required_os_flag("O_CLOEXEC")
            | _required_os_flag("O_NOFOLLOW")
            | _required_os_flag("O_NONBLOCK")
        )
        descriptor = os.open(path.name, flags, dir_fd=directory_descriptor)
        try:
            before = os.fstat(descriptor)
            named_before = os.stat(
                path.name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid not in {0, _effective_uid()}
                or before.st_mode & 0o022
                or before.st_nlink != 1
                or before.st_size < 1
                or before.st_size > 128 * 1024 * 1024
                or _stable_file_identity(before) != _stable_file_identity(named_before)
            ):
                raise _TrustProviderUnavailable(
                    "GitHub CLI executable is not a protected bounded regular file"
                )
            payload = _read_exact_descriptor(descriptor, before.st_size)
            after = os.fstat(descriptor)
            named_after = os.stat(
                path.name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if (
                _stable_file_identity(before) != _stable_file_identity(after)
                or _stable_file_identity(before) != _stable_file_identity(named_after)
                or hashlib.sha256(payload).hexdigest() != expected
            ):
                raise _TrustProviderUnavailable(
                    "GitHub CLI executable differs from the reviewed official release"
                )
            directory_after = os.fstat(directory_descriptor)
            if _stable_directory_identity(directory_before) != _stable_directory_identity(
                directory_after
            ):
                raise _TrustProviderUnavailable("GitHub CLI directory changed during verification")
            _validate_anchored_directory_path(
                parent,
                directory_descriptor,
                require_private=False,
                allow_root_owner=True,
                allow_owner_group_writable=True,
                expected_identity=_stable_directory_identity(directory_before),
            )
            os.lseek(descriptor, 0, os.SEEK_SET)
            confirmed_payload = _read_exact_descriptor(descriptor, before.st_size)
            final = os.fstat(descriptor)
            named_final = os.stat(
                path.name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if (
                confirmed_payload != payload
                or _stable_file_identity(before) != _stable_file_identity(final)
                or _stable_file_identity(before) != _stable_file_identity(named_final)
            ):
                raise _TrustProviderUnavailable(
                    "GitHub CLI executable changed during final anchored read-back"
                )
            _validate_anchored_directory_path(
                parent,
                directory_descriptor,
                require_private=False,
                allow_root_owner=True,
                allow_owner_group_writable=True,
                expected_identity=_stable_directory_identity(directory_before),
            )
        finally:
            os.close(descriptor)
        return payload
    except _TrustProviderUnavailable:
        raise
    except OSError as error:
        raise _TrustProviderUnavailable("GitHub CLI executable could not be verified") from error
    finally:
        os.close(directory_descriptor)


def _verified_summary(raw: bytes) -> _VerificationSummary:
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
        _validate_json_shape(payload)
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise ValueError("GitHub verification summary is invalid") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"certificate", "count", "trustedTimestamps"}
        or type(payload["count"]) is not int
        or payload["count"] != 1
        or not isinstance(payload["certificate"], dict)
        or not payload["certificate"]
        or not isinstance(payload["trustedTimestamps"], list)
        or not 1 <= len(payload["trustedTimestamps"]) <= 16
        or any(not isinstance(item, dict) or not item for item in payload["trustedTimestamps"])
    ):
        raise ValueError("GitHub verification summary lacks one certificate and trusted timestamp")
    return _VerificationSummary(
        count=payload["count"],
        certificate=payload["certificate"],
        trustedTimestamps=payload["trustedTimestamps"],
    )


def _canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _read_external_regular_file(
    path: Path,
    *,
    repository_root: Path,
    maximum_bytes: int,
) -> bytes:
    candidate = _absolute_lexical_path(path)
    root = _absolute_lexical_path(repository_root)
    if candidate == root or candidate.is_relative_to(root):
        raise OSError("external M30 evidence cannot come from the candidate repository")
    if not candidate.name:
        raise OSError("external M30 evidence must name one file")

    _root_path, root_descriptor = _open_anchored_directory(root, create=False)
    try:
        repository_identity = _location_identity(os.fstat(root_descriptor))
    finally:
        os.close(root_descriptor)

    parent, directory_descriptor = _open_anchored_directory(
        candidate.parent,
        create=False,
        forbidden_identities=frozenset((repository_identity,)),
    )
    try:
        directory_before = os.fstat(directory_descriptor)
        _validate_protected_directory(directory_before)
        flags = (
            os.O_RDONLY
            | _required_os_flag("O_CLOEXEC")
            | _required_os_flag("O_NOFOLLOW")
            | _required_os_flag("O_NONBLOCK")
        )
        descriptor = os.open(candidate.name, flags, dir_fd=directory_descriptor)
        try:
            before = os.fstat(descriptor)
            named_before = os.stat(
                candidate.name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            _validate_external_file(before, maximum_bytes=maximum_bytes)
            if _stable_file_identity(named_before) != _stable_file_identity(before):
                raise OSError("external M30 evidence name changed before reading")
            raw = _read_exact_descriptor(descriptor, before.st_size)
            after = os.fstat(descriptor)
            named_after = os.stat(
                candidate.name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if _stable_file_identity(before) != _stable_file_identity(
                after
            ) or _stable_file_identity(before) != _stable_file_identity(named_after):
                raise OSError("external M30 evidence file changed while being read")
            directory_after = os.fstat(directory_descriptor)
            if _stable_directory_identity(directory_before) != _stable_directory_identity(
                directory_after
            ):
                raise OSError("external M30 evidence directory changed while being read")
            _validate_anchored_directory_path(
                parent,
                directory_descriptor,
                require_private=False,
                expected_identity=_stable_directory_identity(directory_before),
            )
            os.lseek(descriptor, 0, os.SEEK_SET)
            confirmed_raw = _read_exact_descriptor(descriptor, before.st_size)
            final = os.fstat(descriptor)
            named_final = os.stat(
                candidate.name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            directory_final = os.fstat(directory_descriptor)
            if (
                confirmed_raw != raw
                or _stable_file_identity(before) != _stable_file_identity(final)
                or _stable_file_identity(before) != _stable_file_identity(named_final)
                or _stable_directory_identity(directory_before)
                != _stable_directory_identity(directory_final)
            ):
                raise OSError("external M30 evidence changed during final anchored read-back")
            _validate_anchored_directory_path(
                parent,
                directory_descriptor,
                require_private=False,
                expected_identity=_stable_directory_identity(directory_before),
            )
            return raw
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_descriptor)


def _read_exact_descriptor(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, min(64 * 1024, remaining))
        if not chunk:
            raise OSError("anchored M30 evidence ended before its declared size")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise OSError("anchored M30 evidence grew while being read")
    return b"".join(chunks)


def _write_private_snapshot(path: Path, payload: bytes) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | _required_os_flag("O_CLOEXEC")
        | _required_os_flag("O_NOFOLLOW")
    )
    descriptor = os.open(path, flags, 0o600)
    try:
        consumed = 0
        while consumed < len(payload):
            written = os.write(descriptor, payload[consumed:])
            if written < 1:
                raise OSError("private M30 authentication snapshot write failed")
            consumed += written
        os.fsync(descriptor)
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_size != len(payload):
            raise OSError("private M30 authentication snapshot is invalid")
    finally:
        os.close(descriptor)


def _write_private_executable_snapshot(path: Path, payload: bytes) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | _required_os_flag("O_CLOEXEC")
        | _required_os_flag("O_NOFOLLOW")
    )
    descriptor = os.open(path, flags, 0o500)
    try:
        consumed = 0
        while consumed < len(payload):
            written = os.write(descriptor, payload[consumed:])
            if written < 1:
                raise OSError("private GitHub CLI snapshot write failed")
            consumed += written
        os.fsync(descriptor)
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or stat.S_IMODE(observed.st_mode) != 0o500
            or observed.st_size != len(payload)
        ):
            raise OSError("private GitHub CLI snapshot is invalid")
    finally:
        os.close(descriptor)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("M30 manifest contains duplicate JSON keys")
        result[key] = value
    return result


def _validate_json_shape(value: object) -> None:
    nodes = 0
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            raise ValueError("M30 manifest JSON exceeds its structural bound")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, str) and len(current.encode("utf-8")) > 4_096:
            raise ValueError("M30 manifest JSON string exceeds its bound")


def _absolute_lexical_path(path: Path) -> Path:
    if ".." in path.parts:
        raise OSError("anchored M30 path cannot contain parent traversal")
    candidate = path if path.is_absolute() else Path.cwd() / path
    if candidate.anchor != "/" or any("\x00" in part for part in candidate.parts):
        raise OSError("anchored M30 path must be one absolute POSIX path")
    return candidate


def _required_os_flag(name: str) -> int:
    try:
        value = getattr(os, name)
    except AttributeError as error:
        raise OSError(f"required anchored-filesystem flag {name} is unavailable") from error
    if not isinstance(value, int) or value == 0:
        raise OSError(f"required anchored-filesystem flag {name} is unusable")
    return value


def _effective_uid() -> int:
    try:
        get_effective_uid = os.geteuid
    except AttributeError as error:
        raise OSError("effective UID inspection is unavailable") from error
    return get_effective_uid()


def _trusted_temporary_parent() -> Path:
    try:
        resolved = Path(tempfile.gettempdir()).resolve(strict=True)
    except OSError as error:
        raise OSError("authentication temporary parent is unavailable") from error
    candidate = _absolute_lexical_path(resolved)
    parent, descriptor = _open_anchored_directory(candidate, create=False)
    try:
        before = os.fstat(descriptor)
        _validate_trusted_directory(before)
        _validate_anchored_directory_path(
            parent,
            descriptor,
            require_private=False,
            allow_root_owner=True,
            expected_identity=_stable_directory_identity(before),
        )
        return parent
    finally:
        os.close(descriptor)


def _require_anchored_filesystem() -> None:
    if os.name != "posix":
        raise OSError("anchored M30 filesystem operations require POSIX")
    for name in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK"):
        _required_os_flag(name)
    required_dir_fd_operations = (
        os.link,
        os.mkdir,
        os.open,
        os.rename,
        os.stat,
        os.unlink,
    )
    if any(operation not in os.supports_dir_fd for operation in required_dir_fd_operations):
        raise OSError("required anchored-filesystem dirfd operation is unavailable")
    if os.link not in os.supports_follow_symlinks or os.stat not in os.supports_follow_symlinks:
        raise OSError("required no-follow anchored-filesystem operation is unavailable")
    if not hasattr(os, "fchmod") or not hasattr(os, "fsync") or not hasattr(os, "lseek"):
        raise OSError("required descriptor metadata operations are unavailable")
    _effective_uid()


def _open_anchored_directory(
    path: Path,
    *,
    create: bool,
    forbidden_identities: frozenset[tuple[int, int]] = frozenset(),
    required_identity: tuple[int, int] | None = None,
    allow_owner_group_writable: bool = False,
) -> tuple[Path, int]:
    _require_anchored_filesystem()
    candidate = _absolute_lexical_path(path)
    flags = (
        os.O_RDONLY
        | _required_os_flag("O_CLOEXEC")
        | _required_os_flag("O_DIRECTORY")
        | _required_os_flag("O_NOFOLLOW")
    )
    current_descriptor = os.open(candidate.anchor, flags)
    try:
        root_metadata = os.fstat(current_descriptor)
        _validate_trusted_directory(
            root_metadata,
            allow_owner_group_writable=allow_owner_group_writable,
        )
        if _location_identity(root_metadata) in forbidden_identities:
            raise OSError("anchored M30 path enters a forbidden directory")
        required_identity_found = _location_identity(root_metadata) == required_identity
        for component in candidate.parts[1:]:
            created = False
            try:
                child_descriptor = os.open(component, flags, dir_fd=current_descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current_descriptor)
                except FileExistsError:
                    pass
                else:
                    created = True
                    os.fsync(current_descriptor)
                child_descriptor = os.open(component, flags, dir_fd=current_descriptor)
            try:
                child_metadata = os.fstat(child_descriptor)
                named_metadata = os.stat(
                    component,
                    dir_fd=current_descriptor,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(child_metadata.st_mode) or _location_identity(
                    child_metadata
                ) != _location_identity(named_metadata):
                    raise OSError("anchored M30 directory component changed during traversal")
                if created:
                    os.fchmod(child_descriptor, 0o700)
                    os.fsync(child_descriptor)
                    child_metadata = os.fstat(child_descriptor)
                    if (
                        stat.S_IMODE(child_metadata.st_mode) != 0o700
                        or child_metadata.st_uid != _effective_uid()
                    ):
                        raise OSError("new anchored M30 directory is not owner-private")
                _validate_trusted_directory(
                    child_metadata,
                    allow_owner_group_writable=allow_owner_group_writable,
                )
                if _location_identity(child_metadata) in forbidden_identities:
                    raise OSError("anchored M30 path enters a forbidden directory")
                required_identity_found = (
                    required_identity_found
                    or _location_identity(child_metadata) == required_identity
                )
            except BaseException:
                os.close(child_descriptor)
                raise
            os.close(current_descriptor)
            current_descriptor = child_descriptor
        if required_identity is not None and not required_identity_found:
            raise OSError("anchored M30 path does not enter its required directory")
        return candidate, current_descriptor
    except BaseException:
        os.close(current_descriptor)
        raise


def _location_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _stable_file_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _stable_directory_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _validate_protected_directory(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != _effective_uid()
        or metadata.st_mode & 0o022
    ):
        raise OSError("anchored M30 directory is not protected for the current owner")


def _validate_trusted_directory(
    metadata: os.stat_result,
    *,
    allow_owner_group_writable: bool = False,
) -> None:
    effective_uid = _effective_uid()
    unsafe_write_mode = bool(metadata.st_mode & 0o022)
    trusted_sticky_root = (
        metadata.st_uid == 0
        and bool(metadata.st_mode & stat.S_ISVTX)
        and bool(metadata.st_mode & 0o002)
    )
    trusted_owner_group_write = (
        allow_owner_group_writable
        and metadata.st_uid == effective_uid
        and not bool(metadata.st_mode & 0o002)
    )
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in {0, effective_uid}
        or (unsafe_write_mode and not trusted_sticky_root and not trusted_owner_group_write)
    ):
        raise OSError("anchored M30 path has an untrusted directory component")


def _validate_private_directory(metadata: os.stat_result) -> None:
    _validate_protected_directory(metadata)
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise OSError("anchored M30 report directory must have mode 0700")


def _validate_external_file(metadata: os.stat_result, *, maximum_bytes: int) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != _effective_uid()
        or metadata.st_mode & 0o022
        or metadata.st_nlink != 1
        or metadata.st_size < 1
        or metadata.st_size > maximum_bytes
    ):
        raise OSError("external M30 evidence is not a protected single-link bounded file")


def _validate_report_file(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != _effective_uid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or metadata.st_size < 1
        or metadata.st_size > _MAX_REPORT_BYTES
    ):
        raise OSError("anchored M30 report is not an owner-private single-link bounded file")


def _validate_anchored_directory_path(
    path: Path,
    descriptor: int,
    *,
    require_private: bool,
    allow_root_owner: bool = False,
    allow_owner_group_writable: bool = False,
    expected_identity: tuple[int, int, int, int, int, int, int, int, int] | None = None,
) -> None:
    held_before = os.fstat(descriptor)
    if require_private and (allow_root_owner or allow_owner_group_writable):
        raise OSError("anchored M30 directory policy is contradictory")

    def validator(metadata: os.stat_result) -> None:
        if require_private:
            _validate_private_directory(metadata)
        elif allow_root_owner:
            _validate_trusted_directory(
                metadata,
                allow_owner_group_writable=allow_owner_group_writable,
            )
        else:
            _validate_protected_directory(metadata)

    validator(held_before)
    if (
        expected_identity is not None
        and _stable_directory_identity(held_before) != expected_identity
    ):
        raise OSError("anchored M30 directory changed before its path recheck")
    _candidate, reopened_descriptor = _open_anchored_directory(
        path,
        create=False,
        allow_owner_group_writable=allow_owner_group_writable,
    )
    try:
        reopened = os.fstat(reopened_descriptor)
        validator(reopened)
        held_after = os.fstat(descriptor)
        if not (
            _stable_directory_identity(held_before)
            == _stable_directory_identity(held_after)
            == _stable_directory_identity(reopened)
        ):
            raise OSError("anchored M30 directory path was rebound during the operation")
    finally:
        os.close(reopened_descriptor)


def _open_report_destination(
    output_directory: Path,
    *,
    repository_root: Path,
    canonical_destination: Path,
    ignored_report_paths: tuple[str, ...],
) -> tuple[Path, int, bool]:
    destination = _absolute_lexical_path(output_directory)
    root = _absolute_lexical_path(repository_root)
    canonical = _absolute_lexical_path(canonical_destination)
    candidate_local = destination == root or destination.is_relative_to(root)
    if candidate_local and destination != canonical:
        raise OSError("M30 reports cannot enter a non-canonical candidate path")
    _root_path, root_descriptor = _open_anchored_directory(root, create=False)
    try:
        _validate_protected_directory(os.fstat(root_descriptor))
        repository_identity = _location_identity(os.fstat(root_descriptor))
        if candidate_local:
            for ignored_report_path in ignored_report_paths:
                tracked = _run_safe_git(
                    root,
                    "ls-files",
                    "--error-unmatch",
                    "--",
                    ignored_report_path,
                )
                if tracked.returncode == 0:
                    raise OSError("canonical M30 report cannot already be tracked")
                ignored = _run_safe_git(
                    root,
                    "check-ignore",
                    "--verbose",
                    "--",
                    ignored_report_path,
                )
                if ignored.returncode != 0 or not ignored.stdout.startswith(b".gitignore:"):
                    raise OSError("canonical M30 report is not Git-ignored")
        opened_destination, descriptor = _open_anchored_directory(
            destination,
            create=True,
            forbidden_identities=(
                frozenset() if candidate_local else frozenset((repository_identity,))
            ),
            required_identity=repository_identity if candidate_local else None,
        )
        try:
            _validate_private_directory(os.fstat(descriptor))
            os.fsync(descriptor)
            _validate_anchored_directory_path(
                root,
                root_descriptor,
                require_private=False,
            )
        except BaseException:
            os.close(descriptor)
            raise
        return opened_destination, descriptor, candidate_local
    finally:
        os.close(root_descriptor)


def _validate_report_filename(filename: str) -> None:
    if not filename or filename in {".", ".."} or "/" in filename or "\x00" in filename:
        raise OSError("anchored M30 report filename is invalid")


def _read_report_file_at(directory_descriptor: int, filename: str) -> bytes | None:
    _validate_report_filename(filename)
    flags = (
        os.O_RDONLY
        | _required_os_flag("O_CLOEXEC")
        | _required_os_flag("O_NOFOLLOW")
        | _required_os_flag("O_NONBLOCK")
    )
    try:
        descriptor = os.open(filename, flags, dir_fd=directory_descriptor)
    except FileNotFoundError:
        return None
    try:
        before = os.fstat(descriptor)
        named_before = os.stat(
            filename,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        _validate_report_file(before)
        if _stable_file_identity(before) != _stable_file_identity(named_before):
            raise OSError("anchored M30 report name changed before reading")
        value = _read_exact_descriptor(descriptor, before.st_size)
        after = os.fstat(descriptor)
        named_after = os.stat(
            filename,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if not (
            _stable_file_identity(before)
            == _stable_file_identity(after)
            == _stable_file_identity(named_after)
        ):
            raise OSError("anchored M30 report changed while being read")
        return value
    finally:
        os.close(descriptor)


def _validate_atomic_target_at(
    directory_descriptor: int,
    filename: str,
    value: str,
    *,
    allow_replace: bool,
) -> bool:
    encoded = value.encode("utf-8")
    if not encoded or len(encoded) > _MAX_REPORT_BYTES:
        raise OSError("anchored M30 report exceeds its byte bound")
    current = _read_report_file_at(directory_descriptor, filename)
    if current is None:
        return True
    if current == encoded:
        return False
    if not allow_replace:
        raise OSError("refusing to overwrite a different external M30 report")
    return True


def _write_report_bundle_at(
    directory_descriptor: int,
    *,
    markdown_name: str,
    markdown: str,
    json_name: str,
    document: str,
    allow_replace: bool,
) -> None:
    write_markdown = _validate_atomic_target_at(
        directory_descriptor,
        markdown_name,
        markdown,
        allow_replace=allow_replace,
    )
    write_json = _validate_atomic_target_at(
        directory_descriptor,
        json_name,
        document,
        allow_replace=allow_replace,
    )
    if not allow_replace and write_markdown and not write_json:
        raise OSError("external M30 JSON marker exists without its exact Markdown companion")
    if write_markdown:
        _publish_report_file_at(
            directory_descriptor,
            markdown_name,
            markdown,
            allow_replace=allow_replace,
        )
    if write_json or (allow_replace and write_markdown):
        _publish_report_file_at(
            directory_descriptor,
            json_name,
            document,
            allow_replace=allow_replace,
        )
    os.fsync(directory_descriptor)
    if _read_report_file_at(directory_descriptor, markdown_name) != markdown.encode("utf-8"):
        raise OSError("anchored M30 Markdown report failed final read-back")
    if _read_report_file_at(directory_descriptor, json_name) != document.encode("utf-8"):
        raise OSError("anchored M30 JSON report failed final read-back")


def _validate_report_bundle_and_path_at(
    directory_path: Path,
    directory_descriptor: int,
    *,
    files: tuple[tuple[str, bytes], ...],
) -> None:
    directory_before = os.fstat(directory_descriptor)
    _validate_private_directory(directory_before)
    flags = (
        os.O_RDONLY
        | _required_os_flag("O_CLOEXEC")
        | _required_os_flag("O_NOFOLLOW")
        | _required_os_flag("O_NONBLOCK")
    )
    opened_descriptors: list[int] = []
    records: list[tuple[int, str, bytes, os.stat_result]] = []
    try:
        for filename, expected in files:
            _validate_report_filename(filename)
            descriptor = os.open(filename, flags, dir_fd=directory_descriptor)
            opened_descriptors.append(descriptor)
            before = os.fstat(descriptor)
            named_before = os.stat(
                filename,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            _validate_report_file(before)
            if _stable_file_identity(before) != _stable_file_identity(named_before):
                raise OSError("anchored M30 report name changed before bundle validation")
            if _read_exact_descriptor(descriptor, before.st_size) != expected:
                raise OSError("anchored M30 report bundle content differs")
            after = os.fstat(descriptor)
            named_after = os.stat(
                filename,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if not (
                _stable_file_identity(before)
                == _stable_file_identity(after)
                == _stable_file_identity(named_after)
            ):
                raise OSError("anchored M30 report changed during bundle validation")
            records.append((descriptor, filename, expected, before))
        directory_after = os.fstat(directory_descriptor)
        if _stable_directory_identity(directory_before) != _stable_directory_identity(
            directory_after
        ):
            raise OSError("anchored M30 report directory changed during bundle validation")
        _validate_anchored_directory_path(
            directory_path,
            directory_descriptor,
            require_private=True,
            expected_identity=_stable_directory_identity(directory_before),
        )
        for descriptor, filename, expected, before in records:
            os.lseek(descriptor, 0, os.SEEK_SET)
            confirmed = _read_exact_descriptor(descriptor, before.st_size)
            final = os.fstat(descriptor)
            named_final = os.stat(
                filename,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if (
                confirmed != expected
                or _stable_file_identity(before) != _stable_file_identity(final)
                or _stable_file_identity(before) != _stable_file_identity(named_final)
            ):
                raise OSError("anchored M30 report changed during final bundle read-back")
        directory_final = os.fstat(directory_descriptor)
        if _stable_directory_identity(directory_before) != _stable_directory_identity(
            directory_final
        ):
            raise OSError("anchored M30 report directory changed during final read-back")
        _validate_anchored_directory_path(
            directory_path,
            directory_descriptor,
            require_private=True,
            expected_identity=_stable_directory_identity(directory_before),
        )
    finally:
        for descriptor in opened_descriptors:
            os.close(descriptor)


def _publish_report_file_at(
    directory_descriptor: int,
    filename: str,
    value: str,
    *,
    allow_replace: bool,
) -> None:
    if allow_replace:
        _atomic_replace_at(directory_descriptor, filename, value)
        return
    try:
        _atomic_write_at(directory_descriptor, filename, value)
    except FileExistsError:
        if _read_report_file_at(directory_descriptor, filename) != value.encode("utf-8"):
            raise


def _create_temporary_report_at(
    directory_descriptor: int,
    filename: str,
    value: str,
) -> tuple[int, str, bytes]:
    _validate_report_filename(filename)
    encoded = value.encode("utf-8")
    if not encoded or len(encoded) > _MAX_REPORT_BYTES:
        raise OSError("anchored M30 report exceeds its byte bound")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | _required_os_flag("O_CLOEXEC")
        | _required_os_flag("O_NOFOLLOW")
    )
    for _attempt in range(8):
        temporary_name = f".{filename}.{secrets.token_hex(16)}.tmp"
        try:
            descriptor = os.open(
                temporary_name,
                flags,
                0o600,
                dir_fd=directory_descriptor,
            )
        except FileExistsError:
            continue
        try:
            os.fchmod(descriptor, 0o600)
            consumed = 0
            while consumed < len(encoded):
                written = os.write(descriptor, encoded[consumed:])
                if written < 1:
                    raise OSError("anchored M30 report write made no progress")
                consumed += written
            os.fsync(descriptor)
            metadata = os.fstat(descriptor)
            _validate_report_file(metadata)
            if metadata.st_size != len(encoded):
                raise OSError("anchored M30 temporary report has the wrong size")
            named = os.stat(
                temporary_name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if _stable_file_identity(metadata) != _stable_file_identity(named):
                raise OSError("anchored M30 temporary report name was replaced")
            return descriptor, temporary_name, encoded
        except BaseException:
            os.close(descriptor)
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            raise
    raise OSError("anchored M30 temporary report name allocation failed")


def _atomic_write_at(directory_descriptor: int, filename: str, value: str) -> None:
    _publish_temporary_report_at(
        directory_descriptor,
        filename,
        value,
        replace=False,
    )


def _atomic_replace_at(directory_descriptor: int, filename: str, value: str) -> None:
    _publish_temporary_report_at(
        directory_descriptor,
        filename,
        value,
        replace=True,
    )


def _publish_temporary_report_at(
    directory_descriptor: int,
    filename: str,
    value: str,
    *,
    replace: bool,
) -> None:
    descriptor, temporary_name, encoded = _create_temporary_report_at(
        directory_descriptor,
        filename,
        value,
    )
    temporary_exists = True
    try:
        if replace:
            os.rename(
                temporary_name,
                filename,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
            temporary_exists = False
        else:
            os.link(
                temporary_name,
                filename,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            os.unlink(temporary_name, dir_fd=directory_descriptor)
            temporary_exists = False
        os.fsync(directory_descriptor)
        descriptor_metadata = os.fstat(descriptor)
        named_metadata = os.stat(
            filename,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        _validate_report_file(descriptor_metadata)
        if _stable_file_identity(descriptor_metadata) != _stable_file_identity(named_metadata):
            raise OSError("published anchored M30 report differs from its open descriptor")
        if _read_report_file_at(directory_descriptor, filename) != encoded:
            raise OSError("published anchored M30 report failed read-back")
    finally:
        os.close(descriptor)
        if temporary_exists:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory_descriptor)


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        if process.poll() is None:
            process.kill()


def _render_markdown(report: M30ManifestAuthenticationReport) -> str:
    rows = [
        "# M30 Phase 1a manifest authentication",
        "",
        f"- State: **{report.state.value.upper()}**",
        f"- Campaign executable: `{str(report.campaign_executable).lower()}`",
        f"- Release decision: **{report.release_decision.value.upper()}**",
        f"- Candidate revision: `{report.preflight.candidate.revision}`",
        f"- Campaign ID: `{report.campaign_id or 'not-authenticated'}`",
        f"- Manifest SHA-256: `{report.manifest_sha256 or 'not-authenticated'}`",
        f"- Control policy ID: `{report.control_policy_id or 'not-bound'}`",
        f"- Control policy SHA-256: `{report.control_policy_sha256 or 'not-bound'}`",
        f"- Authentication completed at: `{report.completed_at.isoformat()}`",
        f"- Workflow-attested manifest authenticated: "
        f"`{str(report.workflow_attested_manifest_authenticated).lower()}`",
        f"- Material external controls passed: `{report.external_controls_passed}`",
        f"- Material external controls remaining: `{report.external_controls_remaining}`",
    ]
    if report.authentication is not None:
        rows.extend(
            (
                f"- GitHub CLI platform: `{report.authentication.github_cli_platform}`",
                "- GitHub CLI executable SHA-256: "
                f"`{report.authentication.github_cli_executable_sha256}`",
                "- Attestation bundle SHA-256: "
                f"`{report.authentication.attestation_bundle_sha256}`",
                "- Verification summary SHA-256: "
                f"`{report.authentication.verification_summary_sha256}`",
            )
        )
    rows.append("")
    if report.blocking_reasons:
        rows.extend(
            (
                "## Blocking reasons",
                "",
                *(f"- `{reason.value}`" for reason in report.blocking_reasons),
                "",
            )
        )
    rows.extend(
        (
            "An authenticated manifest proves only the frozen input bytes. It does not authorize "
            "campaign execution, authenticate a control receipt, adjudicate a result, accept "
            "synthetic evidence, or authorize production/release.",
            "",
        )
    )
    return "\n".join(rows)


def _render_control_policy_markdown(report: M30ControlPolicyValidationReport) -> str:
    rows = [
        "# M30 Phase 1b control-policy validation",
        "",
        f"- State: **{report.state.value.upper()}**",
        f"- Campaign ID: `{report.campaign_id or 'not-authenticated'}`",
        f"- Manifest SHA-256: `{report.manifest_sha256 or 'not-authenticated'}`",
        "- Manifest authentication report SHA-256: "
        f"`{report.manifest_authentication_sha256 or 'not-authenticated'}`",
        f"- Control policy ID: `{report.control_policy_id or 'not-bound'}`",
        f"- Control policy SHA-256: `{report.control_policy_sha256 or 'not-bound'}`",
        f"- Observed at: `{report.observed_at.isoformat()}`",
        f"- Policy not before: "
        f"`{report.policy_not_before.isoformat() if report.policy_not_before else 'not-loaded'}`",
        f"- Policy expires at: "
        f"`{report.policy_expires_at.isoformat() if report.policy_expires_at else 'not-loaded'}`",
        f"- Policy bound to authenticated manifest: "
        f"`{str(report.policy_bound_to_authenticated_manifest).lower()}`",
        f"- Closed prerequisite DAG validated: `{str(report.control_dag_validated).lower()}`",
        f"- External policy trust authenticated: "
        f"`{str(report.external_policy_trust_authenticated).lower()}`",
        f"- Receipt authentication enabled: `{str(report.receipt_authentication_enabled).lower()}`",
        f"- Implemented deterministic adjudicators: `{report.implemented_adjudicators}`",
        f"- Admitted but unadjudicated controls: `{report.admitted_unadjudicated_controls}`",
        f"- External controls passed: `{report.external_controls_passed}`",
        f"- Campaign executable: `{str(report.campaign_executable).lower()}`",
        f"- Release decision: **{report.release_decision.value.upper()}**",
        "",
    ]
    if report.blocking_reasons:
        rows.extend(
            (
                "## Blocking reasons",
                "",
                *(f"- `{reason.value}`" for reason in report.blocking_reasons),
                "",
            )
        )
    rows.extend(
        (
            "A validated policy freezes trust, quorum, criteria and ordering only. It is not a "
            "control receipt, an operated result, campaign authority, M30 acceptance or release.",
            "",
        )
    )
    return "\n".join(rows)


__all__ = [
    "FileM30CampaignManifest",
    "FileM30ControlPolicy",
    "FileM30ControlPolicyReportWriter",
    "FileM30ManifestAuthenticationReportWriter",
    "GitHubCliM30ManifestAuthenticator",
    "SystemM30Clock",
]
