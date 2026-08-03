"""Offline adapters for the fail-closed M30 candidate-readiness preflight."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import tomllib
from pathlib import Path

import yaml
from pydantic import ValidationError
from yaml.tokens import AliasToken, AnchorToken, TagToken

from schemabridge.adapters.evaluation.yaml_loader import load_unique_yaml
from schemabridge.application.ports.production_evidence import (
    ProductionEvidenceError,
    ProductionEvidenceErrorCode,
)
from schemabridge.domain.production_readiness import (
    M30_REQUIRED_SOURCE_PATHS,
    M30CampaignContract,
    M30CandidateBranch,
    M30CandidateObservation,
    M30ReadinessReport,
    M30SourceDigest,
    is_m30_canonical_semver,
)

_MAX_CONTRACT_BYTES = 64 * 1024
_MAX_GIT_OUTPUT_BYTES = 16 * 1024 * 1024
_MAX_REPORT_BYTES = 1024 * 1024
_MAX_REQUIRED_SOURCE_BYTES = 16 * 1024 * 1024
_MAX_WORKTREE_FILE_BYTES = 16 * 1024 * 1024
_GIT_TIMEOUT_SECONDS = 30
_MIGRATION = re.compile(r"^migrations/control_plane/([0-9]{4})_[a-z0-9_]+\.sql$")
_GIT_OBJECT_ID = re.compile(r"^[0-9a-f]{40}$")
_GIT_EXECUTABLE = shutil.which("git", path=os.defpath) or shutil.which("git") or "git"
_SAFE_GIT_PREFIX = (
    _GIT_EXECUTABLE,
    "--no-replace-objects",
    "-c",
    "core.fsmonitor=false",
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    f"core.attributesFile={os.devnull}",
    "-c",
    f"core.excludesFile={os.devnull}",
)
_REGULAR_GIT_MODES = {"100644", "100755"}


class FileM30CampaignContract:
    """Load one bounded duplicate-key-safe contract from the repository."""

    def __init__(self, repository_root: Path) -> None:
        self._root = repository_root.resolve()
        self._path = self._root / M30_REQUIRED_SOURCE_PATHS["m30_campaign_contract"]

    def load(self) -> M30CampaignContract:
        try:
            _reject_symlink_ancestors(self._root, self._path)
            if not self._path.is_file():
                raise OSError("contract is absent or is not a regular file")
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self._path, flags)
            with os.fdopen(descriptor, "rb") as handle:
                metadata = os.fstat(handle.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_CONTRACT_BYTES:
                    raise ValueError("M30 campaign contract size is invalid")
                raw = handle.read(_MAX_CONTRACT_BYTES + 1)
            if not raw or len(raw) > _MAX_CONTRACT_BYTES:
                raise ValueError("M30 campaign contract size is invalid")
            text = raw.decode("utf-8")
            if any(
                isinstance(token, (AliasToken, AnchorToken, TagToken)) for token in yaml.scan(text)
            ):
                raise ValueError("M30 campaign contract aliases and tags are forbidden")
            payload = load_unique_yaml(text)
            return M30CampaignContract.model_validate(payload)
        except OSError as error:
            raise ProductionEvidenceError(
                ProductionEvidenceErrorCode.CONTRACT_UNAVAILABLE,
                "M30 campaign contract is unavailable",
            ) from error
        except (
            RecursionError,
            TypeError,
            UnicodeError,
            ValueError,
            ValidationError,
            yaml.YAMLError,
        ) as error:
            raise ProductionEvidenceError(
                ProductionEvidenceErrorCode.CONTRACT_INVALID,
                "M30 campaign contract is invalid",
            ) from error


class GitM30CandidateIdentity:
    """Bind source facts to HEAD without accepting network or self-asserted evidence."""

    def __init__(self, repository_root: Path) -> None:
        self._root = repository_root.resolve()

    def inspect(self, contract: M30CampaignContract) -> M30CandidateObservation:
        try:
            self._validate_repository_identity()
            revision = self._git_text("rev-parse", "--verify", "HEAD^{commit}")
            branch = self._branch_state()
            head_tree_oid = self._git_text("rev-parse", "HEAD^{tree}")
            raw_tree = self._git_bytes("ls-tree", "-r", "--full-tree", "-z", revision)
            tree_entries = _parse_git_tree(raw_tree)
            initial_index, index_entries = self._validated_index_state()
            initial_worktree = self._worktree_state(tree_entries, index_entries)
            annotated_tags = self._annotated_release_tags(revision)
            source_digests, all_committed = self._source_digests(tree_entries)
            migration_versions, migration_bundle_sha256 = self._migration_identity(tree_entries)
            contract_path = M30_REQUIRED_SOURCE_PATHS["m30_campaign_contract"]
            committed_contract = self._committed_or_none(tree_entries, contract_path)
            package_version = self._package_version(tree_entries)
            contract_matches_head = (
                committed_contract is not None
                and _sha256(committed_contract)
                == next(
                    item.sha256 for item in source_digests if item.name == "m30_campaign_contract"
                )
                and _contract_fingerprint(committed_contract) == contract.fingerprint()
            )
            self._assert_snapshot_stable(
                revision=revision,
                branch=branch,
                head_tree_oid=head_tree_oid,
                annotated_tags=annotated_tags,
                index_state=initial_index,
                tree_entries=tree_entries,
                worktree_state=initial_worktree,
            )
            return M30CandidateObservation(
                revision=revision,
                branch=branch,
                head_tree_oid=head_tree_oid,
                source_tree_sha256=_sha256(raw_tree),
                dirty=bool(initial_worktree),
                annotated_release_tags=annotated_tags,
                package_version=package_version,
                control_schema_version=15,
                migration_versions=migration_versions,
                migration_bundle_sha256=migration_bundle_sha256,
                contract_sha256=contract.fingerprint(),
                contract_matches_head=contract_matches_head,
                all_required_sources_committed=all_committed,
                source_digests=source_digests,
            )
        except ProductionEvidenceError:
            raise
        except (
            OSError,
            RecursionError,
            StopIteration,
            subprocess.SubprocessError,
            ValueError,
        ) as error:
            raise ProductionEvidenceError(
                ProductionEvidenceErrorCode.CANDIDATE_INSPECTION_FAILED,
                "M30 candidate repository identity could not be inspected",
            ) from error

    def _source_digests(
        self,
        tree_entries: dict[str, tuple[str, str]],
    ) -> tuple[tuple[M30SourceDigest, ...], bool]:
        results: list[M30SourceDigest] = []
        all_committed = True
        for name, relative in sorted(M30_REQUIRED_SOURCE_PATHS.items()):
            committed = self._committed_or_none(tree_entries, relative)
            if committed is None:
                all_committed = False
                path = self._root / relative
                _reject_symlink_ancestors(self._root, path)
                payload = _read_bounded_regular_file(path, _MAX_REQUIRED_SOURCE_BYTES)
            else:
                payload = committed
            results.append(M30SourceDigest(name=name, path=relative, sha256=_sha256(payload)))
        return tuple(results), all_committed

    def _migration_identity(
        self,
        tree_entries: dict[str, tuple[str, str]],
    ) -> tuple[tuple[int, ...], str]:
        paths = tuple(
            sorted(path for path in tree_entries if path.startswith("migrations/control_plane/"))
        )
        matches = tuple(_MIGRATION.fullmatch(path) for path in paths)
        if not paths or any(match is None for match in matches):
            raise ValueError("candidate contains an unexpected control-plane migration path")
        versions = tuple(int(match.group(1)) for match in matches if match is not None)
        digest = hashlib.sha256()
        for path in paths:
            mode, object_id = tree_entries[path]
            if mode != "100644":
                raise ValueError("candidate control-plane migrations must be regular data files")
            digest.update(path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(self._git_bytes("cat-file", "blob", object_id))
            digest.update(b"\0")
        return versions, digest.hexdigest()

    def _branch_state(self) -> M30CandidateBranch:
        branch = self._git_text("rev-parse", "--abbrev-ref", "HEAD")
        if branch == "HEAD":
            return M30CandidateBranch.DETACHED
        if branch == "main":
            return M30CandidateBranch.MAIN
        return M30CandidateBranch.OTHER

    def _package_version(self, tree_entries: dict[str, tuple[str, str]]) -> str:
        raw = self._committed_or_none(tree_entries, "pyproject.toml")
        if raw is None:
            raise ValueError("candidate package metadata is not committed")
        payload = tomllib.loads(raw.decode("utf-8"))
        project = payload.get("project")
        version = project.get("version") if isinstance(project, dict) else None
        if not isinstance(version, str):
            raise ValueError("candidate package version is absent")
        return version

    def _annotated_release_tags(self, revision: str) -> tuple[str, ...]:
        tags = self._git_text("tag", "--points-at", revision).splitlines()
        annotated = []
        for tag in tags:
            if not tag.startswith("v") or not is_m30_canonical_semver(tag[1:]):
                continue
            if self._git_text("cat-file", "-t", f"refs/tags/{tag}") == "tag":
                annotated.append(tag)
        return tuple(sorted(annotated))

    def _committed_or_none(
        self,
        tree_entries: dict[str, tuple[str, str]],
        relative: str,
    ) -> bytes | None:
        entry = tree_entries.get(relative)
        if entry is None:
            return None
        mode, object_id = entry
        if mode != "100644":
            raise ValueError("required M30 candidate sources must be regular data files")
        return self._git_bytes("cat-file", "blob", object_id)

    def _validated_index_state(self) -> tuple[bytes, dict[str, tuple[str, str]]]:
        raw = self._git_bytes("ls-files", "--stage", "-v", "-z")
        records = tuple(record for record in raw.split(b"\0") if record)
        entries: dict[str, tuple[str, str]] = {}
        if not records:
            raise ValueError("candidate index is empty")
        for record in records:
            metadata, separator, raw_path = record.partition(b"\t")
            parts = metadata.split(b" ")
            if (
                separator != b"\t"
                or len(parts) != 4
                or parts[0] != b"H"
                or parts[1].decode("ascii", errors="ignore") not in _REGULAR_GIT_MODES
                or _GIT_OBJECT_ID.fullmatch(parts[2].decode("ascii", errors="ignore")) is None
                or parts[3] != b"0"
            ):
                raise ValueError("candidate index contains hidden, sparse, or unsafe state")
            path = _validate_repository_path(raw_path)
            if path in entries:
                raise ValueError("candidate index contains a duplicate path")
            entries[path] = (parts[1].decode("ascii"), parts[2].decode("ascii"))
        return raw, entries

    def _worktree_state(
        self,
        tree_entries: dict[str, tuple[str, str]],
        index_entries: dict[str, tuple[str, str]],
    ) -> bytes:
        changes: list[bytes] = []
        for path in sorted(set(tree_entries) | set(index_entries)):
            if tree_entries.get(path) != index_entries.get(path):
                changes.append(b"index\t" + path.encode("utf-8"))
        for path, expected in sorted(index_entries.items()):
            if _worktree_blob_identity(self._root, path) != expected:
                changes.append(b"worktree\t" + path.encode("utf-8"))
        untracked = self._git_bytes(
            "ls-files",
            "--others",
            "-z",
            "--exclude-from=.gitignore",
        )
        for raw_path in sorted(item for item in untracked.split(b"\0") if item):
            path = _validate_repository_path(raw_path)
            changes.append(b"untracked\t" + path.encode("utf-8"))
        return b"\0".join(changes)

    def _assert_snapshot_stable(
        self,
        *,
        revision: str,
        branch: M30CandidateBranch,
        head_tree_oid: str,
        annotated_tags: tuple[str, ...],
        index_state: bytes,
        tree_entries: dict[str, tuple[str, str]],
        worktree_state: bytes,
    ) -> None:
        self._validate_repository_identity()
        final_index, final_index_entries = self._validated_index_state()
        if (
            self._git_text("rev-parse", "--verify", "HEAD^{commit}") != revision
            or self._git_text("rev-parse", "HEAD^{tree}") != head_tree_oid
            or self._branch_state() is not branch
            or self._annotated_release_tags(revision) != annotated_tags
            or final_index != index_state
            or self._worktree_state(tree_entries, final_index_entries) != worktree_state
        ):
            raise ValueError("candidate repository changed during inspection")

    def _validate_repository_identity(self) -> None:
        top_level = Path(self._git_text("rev-parse", "--show-toplevel")).resolve()
        if top_level != self._root:
            raise ValueError("candidate Git top-level differs from repository root")
        metadata_path = self._root / ".git"
        if metadata_path.is_symlink():
            raise ValueError("candidate Git metadata cannot be symlinked")
        if metadata_path.is_dir():
            expected_git_directory = metadata_path.resolve()
        elif metadata_path.is_file():
            raw = _read_bounded_regular_file(metadata_path, 4_096).decode("utf-8")
            if not raw.startswith("gitdir: "):
                raise ValueError("candidate Git metadata indirection is invalid")
            target = Path(raw.removeprefix("gitdir: ").strip())
            expected_git_directory = (
                target if target.is_absolute() else metadata_path.parent / target
            ).resolve()
        else:
            raise ValueError("candidate Git metadata is absent")
        observed_git_directory = Path(self._git_text("rev-parse", "--absolute-git-dir")).resolve()
        if observed_git_directory != expected_git_directory:
            raise ValueError("candidate Git directory differs from repository metadata")

    def _git_text(self, *arguments: str) -> str:
        return self._git_bytes(*arguments).decode("utf-8").strip()

    def _git_bytes(self, *arguments: str) -> bytes:
        result = _run_safe_git(self._root, *arguments)
        if result.returncode != 0:
            raise subprocess.SubprocessError("Git candidate inspection failed")
        return result.stdout


class FileM30ReadinessReportWriter:
    """Write deterministic evidence to the canonical ignored path or an external directory."""

    def __init__(self, repository_root: Path) -> None:
        self._root = repository_root.resolve()
        self._canonical_destination = (self._root / ".local/m30").resolve()

    def write(self, report: M30ReadinessReport, output_directory: Path) -> tuple[Path, Path]:
        try:
            if ".." in output_directory.parts:
                raise OSError("M30 report destination cannot contain parent traversal")
            _reject_existing_symlink_ancestors(output_directory)
            destination = output_directory.resolve()
            candidate_local = destination.is_relative_to(self._root)
            if candidate_local and destination != self._canonical_destination:
                raise OSError("M30 reports cannot be written into the candidate source tree")
            if candidate_local:
                _reject_symlink_ancestors(self._root, destination)
                ignored = _run_safe_git(
                    self._root,
                    "check-ignore",
                    "--verbose",
                    "--no-index",
                    "--",
                    ".local/m30/readiness.json",
                )
                if ignored.returncode != 0 or not ignored.stdout.startswith(b".gitignore:"):
                    raise OSError("canonical M30 report destination is not Git-ignored")
            destination.mkdir(parents=True, exist_ok=True)
            if not destination.is_dir() or destination.is_symlink():
                raise OSError("M30 report destination is invalid")
            json_path = destination / "readiness.json"
            markdown_path = destination / "readiness.md"
            markdown = _render_markdown(report)
            payload = {
                "bundle_schema_version": 1,
                "markdown_sha256": _sha256(markdown.encode("utf-8")),
                "report": report.model_dump(mode="json"),
                "report_sha256": report.fingerprint(),
            }
            json_document = (
                json.dumps(
                    payload,
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=True,
                )
                + "\n"
            )
            _validate_atomic_target(markdown_path, markdown, allow_replace=candidate_local)
            _validate_atomic_target(json_path, json_document, allow_replace=candidate_local)
            _atomic_write(
                markdown_path,
                markdown,
                allow_replace=candidate_local,
            )
            # JSON is the bundle commit marker and is replaced only after Markdown is durable.
            _atomic_write(
                json_path,
                json_document,
                allow_replace=candidate_local,
            )
            _fsync_directory(destination)
            return json_path, markdown_path
        except (OSError, subprocess.SubprocessError) as error:
            raise ProductionEvidenceError(
                ProductionEvidenceErrorCode.REPORT_WRITE_FAILED,
                "M30 readiness report could not be written",
            ) from error


def _contract_fingerprint(raw: bytes) -> str:
    payload = load_unique_yaml(raw.decode("utf-8"))
    return M30CampaignContract.model_validate(payload).fingerprint()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _parse_git_tree(raw: bytes) -> dict[str, tuple[str, str]]:
    if not raw or not raw.endswith(b"\0"):
        raise ValueError("candidate Git tree is empty or malformed")
    entries: dict[str, tuple[str, str]] = {}
    for record in (item for item in raw.split(b"\0") if item):
        metadata, separator, raw_path = record.partition(b"\t")
        parts = metadata.split(b" ")
        if separator != b"\t" or len(parts) != 3:
            raise ValueError("candidate Git tree record is malformed")
        try:
            mode = parts[0].decode("ascii")
            object_type = parts[1].decode("ascii")
            object_id = parts[2].decode("ascii")
        except UnicodeDecodeError as error:
            raise ValueError("candidate Git tree metadata is invalid") from error
        if (
            mode not in _REGULAR_GIT_MODES
            or object_type != "blob"
            or _GIT_OBJECT_ID.fullmatch(object_id) is None
        ):
            raise ValueError("candidate Git tree contains a symlink, gitlink, or invalid object")
        path = _validate_repository_path(raw_path)
        if path in entries:
            raise ValueError("candidate Git tree contains a duplicate path")
        entries[path] = (mode, object_id)
    if not entries:
        raise ValueError("candidate Git tree is empty")
    return entries


def _validate_repository_path(raw_path: bytes) -> str:
    if not raw_path or len(raw_path) > 1_024:
        raise ValueError("candidate repository path length is invalid")
    try:
        path = raw_path.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("candidate repository path is not UTF-8") from error
    parts = path.split("/")
    if (
        not path
        or path.startswith(("/", "\\"))
        or "\\" in path
        or any(part in {"", ".", ".."} for part in parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise ValueError("candidate repository path is unsafe")
    return path


def _read_bounded_regular_file(path: Path, maximum_bytes: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum_bytes:
            raise OSError("bounded M30 input is not a regular file")
        payload = handle.read(maximum_bytes + 1)
    if len(payload) > maximum_bytes:
        raise OSError("bounded M30 input exceeds its size limit")
    return payload


def _worktree_blob_identity(root: Path, relative: str) -> tuple[str, str] | None:
    path = root / relative
    try:
        _reject_symlink_ancestors(root, path)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_WORKTREE_FILE_BYTES:
            return None
        digest = hashlib.sha1(usedforsecurity=False)
        digest.update(f"blob {before.st_size}\0".encode("ascii"))
        consumed = 0
        while chunk := handle.read(64 * 1024):
            consumed += len(chunk)
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
    )
    if consumed != before.st_size or identity_before != identity_after:
        return None
    mode = "100755" if before.st_mode & 0o111 else "100644"
    return mode, digest.hexdigest()


def _run_safe_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    environment = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C",
        "LC_ALL": "C",
        "PAGER": "cat",
        "PATH": os.defpath,
    }
    process = subprocess.Popen(
        (*_SAFE_GIT_PREFIX, *arguments),
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
        start_new_session=True,
    )
    timed_out = threading.Event()

    def terminate_for_timeout() -> None:
        timed_out.set()
        _kill_process_group(process)

    timer = threading.Timer(_GIT_TIMEOUT_SECONDS, terminate_for_timeout)
    timer.daemon = True
    timer.start()
    try:
        assert process.stdout is not None
        stdout = process.stdout.read(_MAX_GIT_OUTPUT_BYTES + 1)
        overflow = len(stdout) > _MAX_GIT_OUTPUT_BYTES
        if overflow:
            _kill_process_group(process)
        returncode = process.wait()
    finally:
        timer.cancel()
        if process.poll() is None:
            _kill_process_group(process)
            process.wait()
        if process.stdout is not None:
            process.stdout.close()
        timer.join()
    if timed_out.is_set():
        raise subprocess.TimeoutExpired(_SAFE_GIT_PREFIX, _GIT_TIMEOUT_SECONDS)
    if overflow:
        raise subprocess.SubprocessError("Git candidate inspection output exceeded its bound")
    return subprocess.CompletedProcess(
        args=(*_SAFE_GIT_PREFIX, *arguments),
        returncode=returncode,
        stdout=stdout,
        stderr=b"",
    )


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        if process.poll() is None:
            process.kill()


def _atomic_write(path: Path, value: str, *, allow_replace: bool) -> None:
    if not _validate_atomic_target(path, value, allow_replace=allow_replace):
        return
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


def _validate_atomic_target(path: Path, value: str, *, allow_replace: bool) -> bool:
    if path.is_symlink():
        raise OSError("refusing to replace a symlinked M30 report")
    encoded = value.encode("utf-8")
    if len(encoded) > _MAX_REPORT_BYTES:
        raise OSError("generated M30 report exceeds its bound")
    if path.exists():
        if not path.is_file():
            raise OSError("M30 report target is not a regular file")
        if path.stat().st_size > _MAX_REPORT_BYTES:
            raise OSError("existing M30 report exceeds its bound")
        existing = path.read_bytes()
        if existing == encoded:
            return False
        if not allow_replace:
            raise OSError("refusing to overwrite a different external M30 report")
    return True


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _reject_symlink_ancestors(root: Path, path: Path) -> None:
    resolved_root = root.resolve()
    candidate = path.absolute()
    if not candidate.is_relative_to(resolved_root):
        raise OSError("M30 repository input escaped the repository root")
    relative = candidate.relative_to(resolved_root)
    current = resolved_root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise OSError("M30 repository input has a symlinked path component")


def _reject_existing_symlink_ancestors(path: Path) -> None:
    candidate = path if path.is_absolute() else Path.cwd() / path
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        if current.is_symlink():
            raise OSError("M30 report destination has a symlinked path component")


def _render_markdown(report: M30ReadinessReport) -> str:
    rows = [
        "# M30 candidate-readiness preflight",
        "",
        f"- State: **{report.preflight_state.value.upper()}**",
        f"- Campaign executable: `{str(report.campaign_executable).lower()}`",
        f"- Release decision: **{report.release_decision.value.upper()}**",
        f"- Candidate revision: `{report.candidate.revision}`",
        f"- Candidate branch: `{report.candidate.branch}`",
        f"- Contract SHA-256: `{report.contract_sha256}`",
        f"- Report SHA-256: `{report.fingerprint()}`",
        "",
        "This offline report prepares M30 only. It performs no network, database, DataHub, or source write and cannot convert repository fixtures or a pull-request merge ref into operated evidence.",
        "",
        "## Gates",
        "",
        "| Gate | Evidence class | Status | Detail |",
        "|---|---|---|---|",
    ]
    rows.extend(
        f"| `{gate.code}` | `{gate.evidence_class.value}` | `{gate.status.value}` | {gate.detail} |"
        for gate in report.gates
    )
    rows.extend(
        (
            "",
            "A correct `BLOCKED_PREREQUISITES` report is not M30 acceptance or release evidence.",
            "",
        )
    )
    return "\n".join(rows)


__all__ = [
    "FileM30CampaignContract",
    "FileM30ReadinessReportWriter",
    "GitM30CandidateIdentity",
]
