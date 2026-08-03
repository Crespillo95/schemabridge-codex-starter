"""Filesystem and GitHub-attestation adapters for M30 Phase 1a."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import signal
import stat
import subprocess
import tempfile
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
            with tempfile.TemporaryDirectory(prefix="schemabridge-m30-auth-") as snapshot_name:
                snapshot = Path(snapshot_name)
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
        self._canonical_destination = (self._root / ".local/m30/manifest-authentication").resolve()

    def write(
        self,
        report: M30ManifestAuthenticationReport,
        output_directory: Path,
    ) -> tuple[Path, Path]:
        try:
            if ".." in output_directory.parts:
                raise OSError("M30 report destination cannot contain parent traversal")
            _reject_existing_symlink_ancestors(output_directory)
            destination = output_directory.resolve()
            candidate_local = destination.is_relative_to(self._root)
            if candidate_local and destination != self._canonical_destination:
                raise OSError("M30 authentication reports cannot enter the candidate source tree")
            if candidate_local:
                ignored = _run_safe_git(
                    self._root,
                    "check-ignore",
                    "--verbose",
                    "--no-index",
                    "--",
                    ".local/m30/manifest-authentication/authentication.json",
                )
                if ignored.returncode != 0 or not ignored.stdout.startswith(b".gitignore:"):
                    raise OSError("canonical M30 authentication report is not Git-ignored")
            destination.mkdir(parents=True, exist_ok=True)
            _reject_existing_symlink_ancestors(destination)
            if not destination.is_dir() or destination.is_symlink():
                raise OSError("M30 authentication report destination is invalid")
            json_path = destination / "authentication.json"
            markdown_path = destination / "authentication.md"
            markdown = _render_markdown(report)
            payload = {
                "bundle_schema_version": 1,
                "markdown_sha256": hashlib.sha256(markdown.encode()).hexdigest(),
                "report": report.model_dump(mode="json"),
                "report_sha256": report.fingerprint(),
            }
            document = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
            write_markdown = _validate_atomic_target(
                markdown_path,
                markdown,
                allow_replace=candidate_local,
            )
            write_json = _validate_atomic_target(
                json_path,
                document,
                allow_replace=candidate_local,
            )
            if write_markdown:
                _atomic_write(markdown_path, markdown)
            if write_json:
                _atomic_write(json_path, document)
            _fsync_directory(destination)
            return json_path, markdown_path
        except (OSError, subprocess.SubprocessError) as error:
            raise ProductionEvidenceError(
                ProductionEvidenceErrorCode.REPORT_WRITE_FAILED,
                "M30 manifest authentication report could not be written",
            ) from error


class FileM30ControlPolicyReportWriter:
    """Write the bounded Phase-1b policy report without accepting control evidence."""

    def __init__(self, repository_root: Path) -> None:
        self._root = repository_root.resolve()
        self._canonical_destination = (self._root / ".local/m30/control-policy").resolve()

    def write(
        self,
        report: M30ControlPolicyValidationReport,
        output_directory: Path,
    ) -> tuple[Path, Path]:
        try:
            if ".." in output_directory.parts:
                raise OSError("M30 policy report destination cannot contain parent traversal")
            _reject_existing_symlink_ancestors(output_directory)
            destination = output_directory.resolve()
            candidate_local = destination.is_relative_to(self._root)
            if candidate_local and destination != self._canonical_destination:
                raise OSError("M30 policy reports cannot enter the candidate source tree")
            if candidate_local:
                ignored = _run_safe_git(
                    self._root,
                    "check-ignore",
                    "--verbose",
                    "--no-index",
                    "--",
                    ".local/m30/control-policy/policy-validation.json",
                )
                if ignored.returncode != 0 or not ignored.stdout.startswith(b".gitignore:"):
                    raise OSError("canonical M30 policy report is not Git-ignored")
            destination.mkdir(parents=True, exist_ok=True)
            _reject_existing_symlink_ancestors(destination)
            if not destination.is_dir() or destination.is_symlink():
                raise OSError("M30 policy report destination is invalid")
            json_path = destination / "policy-validation.json"
            markdown_path = destination / "policy-validation.md"
            markdown = _render_control_policy_markdown(report)
            payload = {
                "bundle_schema_version": 1,
                "markdown_sha256": hashlib.sha256(markdown.encode()).hexdigest(),
                "report": report.model_dump(mode="json"),
                "report_sha256": report.fingerprint(),
            }
            document = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
            write_markdown = _validate_atomic_target(
                markdown_path,
                markdown,
                allow_replace=candidate_local,
            )
            write_json = _validate_atomic_target(
                json_path,
                document,
                allow_replace=candidate_local,
            )
            if write_markdown:
                _atomic_write(markdown_path, markdown)
            if write_json:
                _atomic_write(json_path, document)
            _fsync_directory(destination)
            return json_path, markdown_path
        except (OSError, subprocess.SubprocessError) as error:
            raise ProductionEvidenceError(
                ProductionEvidenceErrorCode.CONTROL_POLICY_REPORT_WRITE_FAILED,
                "M30 control-policy report could not be written",
            ) from error


class _TrustProviderUnavailable(RuntimeError):
    pass


def _run_safe_gh(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    _source, executable_bytes = _load_trusted_gh_executable()
    with tempfile.TemporaryDirectory(prefix="schemabridge-m30-gh-") as private_name:
        private = Path(private_name)
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
        try:
            stdout, stderr = process.communicate(timeout=_GH_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            _kill_process_group(process)
            process.communicate()
            raise subprocess.SubprocessError("GitHub attestation verification timed out") from error
        _validate_trusted_gh_executable(executable)
    if len(stdout) > 65_536 or len(stderr) > 16_384:
        raise subprocess.SubprocessError("GitHub attestation verifier output exceeded its bound")
    return subprocess.CompletedProcess(
        args=(executable_name, *arguments),
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )


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
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_mode & 0o022
                or before.st_size < 1
                or before.st_size > 128 * 1024 * 1024
            ):
                raise _TrustProviderUnavailable(
                    "GitHub CLI executable is not a protected bounded regular file"
                )
            digest = hashlib.sha256()
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
                chunks.append(chunk)
            payload = b"".join(chunks)
            after = os.fstat(descriptor)
            if (
                before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or len(payload) != after.st_size
                or digest.hexdigest() != expected
            ):
                raise _TrustProviderUnavailable(
                    "GitHub CLI executable differs from the reviewed official release"
                )
        finally:
            os.close(descriptor)
    except _TrustProviderUnavailable:
        raise
    except OSError as error:
        raise _TrustProviderUnavailable("GitHub CLI executable could not be verified") from error
    return payload


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
    resolved = _external_path(path, repository_root)
    _reject_existing_symlink_ancestors(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(resolved, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size < 1 or before.st_size > maximum_bytes:
            raise OSError("external M30 evidence file is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(raw) < 1
            or len(raw) > maximum_bytes
            or before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or len(raw) != after.st_size
        ):
            raise OSError("external M30 evidence file changed while being read")
        return raw
    finally:
        os.close(descriptor)


def _write_private_snapshot(path: Path, payload: bytes) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
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
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
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


def _external_path(path: Path, repository_root: Path) -> Path:
    if ".." in path.parts:
        raise OSError("external M30 evidence path cannot contain parent traversal")
    candidate = path if path.is_absolute() else Path.cwd() / path
    resolved = candidate.resolve(strict=True)
    if resolved.is_relative_to(repository_root.resolve()):
        raise OSError("external M30 evidence cannot come from the candidate repository")
    return resolved


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


def _reject_existing_symlink_ancestors(path: Path) -> None:
    candidate = path if path.is_absolute() else Path.cwd() / path
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        if current.is_symlink():
            raise OSError("M30 evidence path has a symlinked component")


def _validate_atomic_target(path: Path, value: str, *, allow_replace: bool) -> bool:
    encoded = value.encode("utf-8")
    if len(encoded) > _MAX_REPORT_BYTES or path.is_symlink():
        raise OSError("M30 authentication report target is unsafe")
    if path.exists():
        if not path.is_file() or path.stat().st_size > _MAX_REPORT_BYTES:
            raise OSError("existing M30 authentication report target is unsafe")
        if path.read_bytes() == encoded:
            return False
        if not allow_replace:
            raise OSError("refusing to overwrite a different external M30 report")
    return True


def _atomic_write(path: Path, value: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
