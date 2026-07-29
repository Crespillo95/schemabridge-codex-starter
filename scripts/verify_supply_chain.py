"""Deterministic supply-chain policy, SBOM, vulnerability, and provenance checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from importlib import metadata
from pathlib import Path
from typing import Any, cast

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_EXTRAS = ("api", "postgres", "sql", "ui")
RUNTIME_REQUIREMENTS = Path("requirements/runtime.txt")
BUILD_REQUIREMENTS = Path("requirements/build.txt")
EXCEPTIONS_PATH = Path("requirements/vulnerability-exceptions.json")
UV_VERSION = "0.11.30"
PROVENANCE_BUILD_TYPE = "https://github.com/SchemaBridge/buildtypes/github-actions-frozen-uv/v1"
PROVENANCE_BUILDER_ID = "https://github.com/actions/runner"

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHA256_REFERENCE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVIEWED_VERSION = re.compile(r"#\s*(?:reviewed\s+)?v?\d+(?:\.\d+){0,3}\b", re.IGNORECASE)
_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[^\s;]+)(?:\s*;\s*(?P<marker>.+?))?$"
)
_HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})\b")
_FROM = re.compile(r"^\s*FROM\s+([^\s]+)", re.IGNORECASE)
_DIRECT_NAME = re.compile(r"^\s*([A-Za-z0-9_.-]+)")
_ATTEST_ACTION = "actions/attest-build-provenance@0f67c3f4856b2e3261c31976d6725780e5e4c373"
_BUILD_PUSH_ACTION = "docker/build-push-action@53b7df96c91f9c12dcc8a07bcb9ccacbed38856a"
_LOGIN_ACTION = "docker/login-action@dbcb813823bdd20940b903addbd779551569679f"
_SETUP_BUILDX_ACTION = "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c"
_TRIVY_ACTION = "aquasecurity/trivy-action@57a97c7e7821a5776cebc9bb87c984fa69cba8f1"
_UPLOAD_ARTIFACT_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
_RELEASE_IMAGE_NAME_OUTPUT = "${{ steps.image_name.outputs.name }}"
_RELEASE_IMAGE_REFERENCE = "${{ steps.image_name.outputs.name }}@${{ steps.build.outputs.digest }}"
_RELEASE_IMAGE_TAG = "${{ steps.image_name.outputs.name }}:${{ github.sha }}"
_RELEASE_MANIFEST_DIGEST = "${{ steps.build.outputs.digest }}"
_RELEASE_IMAGE_NAME_SCRIPT = (
    "set -euo pipefail\n"
    'if [[ ! "$REPOSITORY_SLUG" =~ '
    "^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then\n"
    "  exit 1\n"
    "fi\n"
    'image_name="ghcr.io/${REPOSITORY_SLUG,,}"\n'
    'printf \'name=%s\\n\' "$image_name" >> "$GITHUB_OUTPUT"\n'
)
_RELEASE_APPROVAL_SCRIPT = (
    "set -euo pipefail\n"
    'if [[ ! "$RELEASE_APPROVAL_SENTINEL" =~ ^[A-Za-z0-9._-]{32,128}$ ]]; then\n'
    "  exit 1\n"
    "fi\n"
)
_RELEASE_APPROVAL_STEP = {
    "name": "Require the protected release approval sentinel",
    "shell": "bash",
    "env": {"RELEASE_APPROVAL_SENTINEL": ("${{ secrets.SCHEMABRIDGE_RELEASE_APPROVAL_SENTINEL }}")},
    "run": _RELEASE_APPROVAL_SCRIPT,
}
_RELEASE_AUDIT_COMMAND = re.compile(
    r"(?m)^\s*\.venv/bin/python scripts/release_audit\.py --require-release\s*$"
)
_RELEASE_JOB_PERMISSIONS = {
    "contents": "read",
    "id-token": "write",
    "attestations": "write",
    "packages": "write",
}
_RELEASE_EVIDENCE_PATHS = frozenset(
    {
        ".local/supply-chain/wheel.cdx.json",
        ".local/supply-chain/runtime-image.cdx.json",
        ".local/supply-chain/provenance.intoto.json",
    }
)
_FORBIDDEN_RELEASE_DOCKER_COMMAND = re.compile(
    r"\bdocker\s+(?:build|push|inspect|image\s+inspect)\b",
    re.IGNORECASE,
)
_CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
_RELEASE_WORKFLOW_PATH = ".github/workflows/release-evidence.yml"
_CI_TIMEOUT_JOBS = frozenset({"quality", "postgres-integration", "supply-chain"})
_SUPPLY_CHAIN_ARTIFACT_PATHS = (
    ".local/supply-chain/dist/*.whl",
    ".local/supply-chain/wheel.cdx.json",
    ".local/supply-chain/runtime-image.cdx.json",
    ".local/supply-chain/provenance.intoto.json",
    ".local/supply-chain/direct-licenses.json",
    ".local/supply-chain/pip-audit.json",
    ".local/supply-chain/trivy-image.json",
)
_DOCKER_CONTEXT_SECRET_EXCLUSIONS = (
    ".streamlit/secrets.toml",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
)

_ALLOWED_LICENSES = frozenset(
    {
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "ISC",
        "LGPL-3.0-only",
        "LGPL-3.0-or-later",
        "MIT",
        "MPL-2.0",
        "PSF-2.0",
    }
)
_PROHIBITED_LICENSE_FRAGMENTS = (
    "AGPL",
    "BUSL",
    "BUSINESS SOURCE",
    "COMMONS CLAUSE",
    "GPL-2.0",
    "GPL-3.0",
    "SSPL",
)


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    path: str
    detail: str


@dataclass(frozen=True, slots=True)
class LockedRequirement:
    name: str
    version: str
    marker: str | None
    hashes: tuple[str, ...]

    @property
    def normalized_name(self) -> str:
        return normalize_name(self.name)

    @property
    def bom_ref(self) -> str:
        return f"pkg:pypi/{self.normalized_name}@{self.version}"


@dataclass(frozen=True, slots=True)
class DirectLicense:
    package: str
    version: str
    license: str


class SupplyChainViolation(RuntimeError):
    """Raised when one or more fail-closed supply-chain checks fail."""

    def __init__(self, findings: Iterable[Finding]) -> None:
        ordered = tuple(sorted(findings, key=lambda item: (item.path, item.code, item.detail)))
        if not ordered:
            raise ValueError("at least one finding is required")
        self.findings = ordered
        super().__init__("; ".join(f"{item.code}:{item.path}" for item in ordered))


def normalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_hashed_requirements(text: str, *, source: str) -> tuple[LockedRequirement, ...]:
    blocks: list[str] = []
    current: list[str] = []
    findings: list[Finding] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            if current:
                blocks.append(" ".join(current))
            current = []
        current.append(line.strip().removesuffix("\\").strip())
    if current:
        blocks.append(" ".join(current))

    requirements: list[LockedRequirement] = []
    seen: set[tuple[str, str | None]] = set()
    for index, block in enumerate(blocks, start=1):
        hashes = tuple(sorted(set(_HASH.findall(block))))
        spec = re.sub(r"\s+--hash=sha256:[0-9a-f]{64}\b", "", block).strip()
        match = _REQUIREMENT.fullmatch(spec)
        location = f"{source}:{index}"
        if match is None:
            findings.append(
                Finding(
                    "requirement_not_exact",
                    location,
                    "only name==version with an optional environment marker is allowed",
                )
            )
            continue
        if not hashes:
            findings.append(
                Finding("requirement_hash_missing", location, "at least one SHA-256 is required")
            )
            continue
        marker = match.group("marker")
        requirement = LockedRequirement(
            name=match.group("name"),
            version=match.group("version"),
            marker=marker,
            hashes=hashes,
        )
        identity = (requirement.normalized_name, marker)
        if identity in seen:
            findings.append(
                Finding(
                    "requirement_duplicate",
                    location,
                    f"duplicate requirement for {requirement.normalized_name}",
                )
            )
            continue
        seen.add(identity)
        requirements.append(requirement)
    if not requirements and not findings:
        findings.append(Finding("requirements_empty", source, "no requirements were found"))
    if findings:
        raise SupplyChainViolation(findings)
    return tuple(sorted(requirements, key=lambda item: (item.normalized_name, item.marker or "")))


def load_requirements(root: Path, relative: Path) -> tuple[LockedRequirement, ...]:
    path = root / relative
    if not path.is_file():
        raise SupplyChainViolation(
            (Finding("requirements_missing", str(relative), "hashed export is absent"),)
        )
    return parse_hashed_requirements(path.read_text(encoding="utf-8"), source=str(relative))


@dataclass(frozen=True, slots=True)
class _YamlDocument:
    root: MappingNode
    value: Mapping[str, object]
    lines: tuple[str, ...]


def _validate_yaml_structure(node: Node, ancestors: set[int] | None = None) -> None:
    ancestors = set() if ancestors is None else ancestors
    identity = id(node)
    if identity in ancestors:
        raise ValueError("recursive YAML aliases are forbidden")
    current = {*ancestors, identity}
    if isinstance(node, MappingNode):
        keys: set[str] = set()
        for key_node, value_node in node.value:
            if not isinstance(key_node, ScalarNode):
                raise ValueError("YAML mapping keys must be scalars")
            key = key_node.value
            if key == "<<" or key in keys:
                raise ValueError("duplicate or merged YAML mappings are forbidden")
            keys.add(key)
            _validate_yaml_structure(value_node, current)
    elif isinstance(node, SequenceNode):
        for value_node in node.value:
            _validate_yaml_structure(value_node, current)
    elif not isinstance(node, ScalarNode):
        raise ValueError("unsupported YAML node")


def _yaml_value(node: Node) -> object:
    if isinstance(node, ScalarNode):
        return node.value
    if isinstance(node, SequenceNode):
        return [_yaml_value(item) for item in node.value]
    if isinstance(node, MappingNode):
        return {
            key_node.value: _yaml_value(value_node)
            for key_node, value_node in node.value
            if isinstance(key_node, ScalarNode)
        }
    raise ValueError("unsupported YAML node")


def _parse_yaml_documents(
    path: Path,
    *,
    relative: str,
) -> tuple[tuple[_YamlDocument, ...], tuple[Finding, ...]]:
    try:
        text = path.read_text(encoding="utf-8")
        if not text or len(text.encode("utf-8")) > 2_097_152:
            raise ValueError("YAML document size is invalid")
        roots = tuple(yaml.compose_all(text, Loader=yaml.BaseLoader))
        if not roots:
            raise ValueError("YAML document is empty")
        documents: list[_YamlDocument] = []
        for root_node in roots:
            if not isinstance(root_node, MappingNode):
                raise ValueError("YAML document root must be a mapping")
            _validate_yaml_structure(root_node)
            value = _yaml_value(root_node)
            if not isinstance(value, dict):
                raise ValueError("YAML document root must be a mapping")
            documents.append(
                _YamlDocument(
                    root=root_node,
                    value=value,
                    lines=tuple(text.splitlines()),
                )
            )
        return tuple(documents), ()
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as error:
        return (
            (),
            (
                Finding(
                    "yaml_structure_invalid",
                    relative,
                    type(error).__name__,
                ),
            ),
        )


def _yaml_nodes_for_key(node: Node, expected: str) -> tuple[ScalarNode, ...]:
    found: list[ScalarNode] = []
    if isinstance(node, MappingNode):
        for key_node, value_node in node.value:
            if isinstance(key_node, ScalarNode) and key_node.value == expected:
                if not isinstance(value_node, ScalarNode):
                    raise ValueError(f"{expected} must be a scalar")
                found.append(value_node)
            found.extend(_yaml_nodes_for_key(value_node, expected))
    elif isinstance(node, SequenceNode):
        for value_node in node.value:
            found.extend(_yaml_nodes_for_key(value_node, expected))
    return tuple(found)


def _remote_action_findings(
    path: Path,
    document: _YamlDocument,
    root: Path,
) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    relative = str(path.relative_to(root))
    try:
        action_nodes = _yaml_nodes_for_key(document.root, "uses")
    except ValueError:
        return (
            Finding(
                "workflow_action_invalid",
                relative,
                "uses must be a scalar",
            ),
        )
    for value_node in action_nodes:
        reference = value_node.value
        line_number = value_node.start_mark.line + 1
        line = document.lines[value_node.start_mark.line]
        _, separator, comment = line.partition("#")
        if reference.startswith("./"):
            continue
        if "@" not in reference:
            findings.append(Finding("action_ref_missing", f"{relative}:{line_number}", reference))
            continue
        action, revision = reference.rsplit("@", maxsplit=1)
        if not action or _FULL_SHA.fullmatch(revision) is None:
            findings.append(
                Finding(
                    "action_not_sha_pinned",
                    f"{relative}:{line_number}",
                    "remote actions require a full 40-character commit SHA",
                )
            )
        if not separator or _REVIEWED_VERSION.search(f"#{comment}") is None:
            findings.append(
                Finding(
                    "action_reviewed_version_missing",
                    f"{relative}:{line_number}",
                    "the reviewed release must be retained in an inline comment",
                )
            )
    return tuple(findings)


def _top_level_permissions_findings(
    path: Path,
    document: _YamlDocument,
    root: Path,
) -> tuple[Finding, ...]:
    relative = str(path.relative_to(root))
    permissions = document.value.get("permissions")
    if permissions is None:
        return (
            Finding(
                "workflow_permissions_missing",
                relative,
                "top-level permissions must default to contents: read",
            ),
        )
    if permissions != {"contents": "read"}:
        return (
            Finding(
                "workflow_permissions_not_minimal",
                relative,
                "top-level permissions are not exactly contents: read",
            ),
        )
    return ()


def _workflow_job_permission_findings(
    path: Path,
    document: _YamlDocument,
    root: Path,
) -> tuple[Finding, ...]:
    relative = str(path.relative_to(root))
    findings: list[Finding] = []
    triggers = document.value.get("on")
    release_only = isinstance(triggers, dict) and set(triggers) == {"release"}
    jobs = document.value.get("jobs")
    if not isinstance(jobs, dict):
        return (Finding("workflow_jobs_invalid", relative, "jobs must be a mapping"),)
    for job_name, raw_job in jobs.items():
        if not isinstance(job_name, str) or not isinstance(raw_job, dict):
            findings.append(Finding("workflow_job_invalid", relative, "job must be a mapping"))
            continue
        raw_permissions = raw_job.get("permissions")
        if raw_permissions is None:
            continue
        if not isinstance(raw_permissions, dict) or not all(
            isinstance(key, str) and value in {"none", "read", "write"}
            for key, value in raw_permissions.items()
        ):
            findings.append(
                Finding(
                    "workflow_job_permissions_invalid",
                    f"{relative}:{job_name}",
                    "job permissions must use closed read/write/none values",
                )
            )
            continue
        write_scopes = {
            key
            for key, value in raw_permissions.items()
            if isinstance(key, str) and value == "write"
        }
        if not write_scopes:
            continue
        environment = raw_job.get("environment")
        environment_name = environment.get("name") if isinstance(environment, dict) else environment
        if (
            not release_only
            or environment_name != "production-release"
            or not write_scopes <= {"attestations", "id-token", "packages"}
        ):
            findings.append(
                Finding(
                    "release_permission_unprotected",
                    f"{relative}:{job_name}",
                    "write permissions require one release-only protected job",
                )
            )
    return tuple(findings)


def _valid_job_timeout(raw_timeout: object) -> bool:
    return isinstance(raw_timeout, str) and raw_timeout.isdecimal() and 1 <= int(raw_timeout) <= 180


def _ci_workflow_findings(
    path: Path,
    document: _YamlDocument,
    root: Path,
) -> tuple[Finding, ...]:
    relative = str(path.relative_to(root))
    if relative != _CI_WORKFLOW_PATH:
        return ()
    findings: list[Finding] = []
    triggers = document.value.get("on")
    if (
        not isinstance(triggers, dict)
        or triggers.get("push") != {"branches": ["main"]}
        or "pull_request" not in triggers
    ):
        findings.append(
            Finding(
                "ci_push_scope_invalid",
                relative,
                "CI must run for pull requests and restrict push checks to main",
            )
        )

    concurrency = document.value.get("concurrency")
    group = concurrency.get("group") if isinstance(concurrency, dict) else None
    cancel_in_progress = (
        concurrency.get("cancel-in-progress") if isinstance(concurrency, dict) else None
    )
    if (
        not isinstance(group, str)
        or "github.workflow" not in group
        or "github.ref" not in group
        or cancel_in_progress != "true"
    ):
        findings.append(
            Finding(
                "ci_concurrency_invalid",
                relative,
                "CI concurrency must group by workflow/ref and cancel superseded runs",
            )
        )

    jobs = document.value.get("jobs")
    for job_name in sorted(_CI_TIMEOUT_JOBS):
        raw_job = jobs.get(job_name) if isinstance(jobs, dict) else None
        raw_timeout = raw_job.get("timeout-minutes") if isinstance(raw_job, dict) else None
        if not _valid_job_timeout(raw_timeout):
            findings.append(
                Finding(
                    "ci_job_timeout_invalid",
                    f"{relative}:{job_name}",
                    "required CI jobs need an explicit timeout of 1 to 180 minutes",
                )
            )
    return tuple(findings)


def _artifact_upload_paths(raw_paths: object) -> tuple[str, ...]:
    if not isinstance(raw_paths, str):
        return ()
    return tuple(line.strip() for line in raw_paths.splitlines() if line.strip())


def _supply_chain_artifact_upload_findings(
    path: Path,
    document: _YamlDocument,
    root: Path,
) -> tuple[Finding, ...]:
    relative = str(path.relative_to(root))
    if relative == _CI_WORKFLOW_PATH:
        expected_job = "supply-chain"
        expected_name = "supply-chain-evidence-${{ github.sha }}"
        expected_retention = "14"
        require_always = True
    elif relative == _RELEASE_WORKFLOW_PATH:
        expected_job = "attest"
        expected_name = "release-supply-chain-evidence-${{ github.sha }}"
        expected_retention = "35"
        require_always = False
    else:
        return ()

    jobs = document.value.get("jobs")
    upload_steps: list[tuple[str, int, Mapping[str, object]]] = []
    if isinstance(jobs, dict):
        for job_name, raw_job in jobs.items():
            if not isinstance(job_name, str) or not isinstance(raw_job, dict):
                continue
            raw_steps = raw_job.get("steps")
            if not isinstance(raw_steps, list):
                continue
            for step_index, raw_step in enumerate(raw_steps, start=1):
                if not isinstance(raw_step, dict):
                    continue
                action = raw_step.get("uses")
                if isinstance(action, str) and action.startswith("actions/upload-artifact@"):
                    upload_steps.append((job_name, step_index, raw_step))

    if len(upload_steps) != 1:
        return (
            Finding(
                "supply_chain_artifact_upload_invalid",
                relative,
                "the workflow requires exactly one reviewed supply-chain artifact upload",
            ),
        )

    job_name, step_index, raw_step = upload_steps[0]
    inputs = raw_step.get("with")
    expected_input_keys = {
        "name",
        "path",
        "if-no-files-found",
        "include-hidden-files",
        "retention-days",
    }
    condition_valid = raw_step.get("if") == "always()" if require_always else "if" not in raw_step
    upload_valid = (
        job_name == expected_job
        and raw_step.get("uses") == _UPLOAD_ARTIFACT_ACTION
        and condition_valid
        and raw_step.get("continue-on-error") in {None, "false"}
        and isinstance(inputs, dict)
        and set(inputs) == expected_input_keys
        and inputs.get("name") == expected_name
        and _artifact_upload_paths(inputs.get("path")) == _SUPPLY_CHAIN_ARTIFACT_PATHS
        and inputs.get("if-no-files-found") == "error"
        and inputs.get("include-hidden-files") == "true"
        and inputs.get("retention-days") == expected_retention
    )
    if upload_valid:
        return ()
    return (
        Finding(
            "supply_chain_artifact_upload_invalid",
            f"{relative}:{job_name}:step-{step_index}",
            "upload must use the exact reviewed action, condition, inputs, and evidence allowlist",
        ),
    )


def _checkout_credentials_findings(
    path: Path,
    document: _YamlDocument,
    root: Path,
) -> tuple[Finding, ...]:
    relative = str(path.relative_to(root))
    jobs = document.value.get("jobs")
    if not isinstance(jobs, dict):
        return ()
    findings: list[Finding] = []
    for job_name, raw_job in jobs.items():
        if not isinstance(job_name, str) or not isinstance(raw_job, dict):
            continue
        raw_steps = raw_job.get("steps")
        if not isinstance(raw_steps, list):
            continue
        for step_index, raw_step in enumerate(raw_steps, start=1):
            if not isinstance(raw_step, dict):
                continue
            action = raw_step.get("uses")
            if not isinstance(action, str) or not action.startswith("actions/checkout@"):
                continue
            inputs = raw_step.get("with")
            if not isinstance(inputs, dict) or inputs.get("persist-credentials") != "false":
                findings.append(
                    Finding(
                        "checkout_credentials_persisted",
                        f"{relative}:{job_name}:step-{step_index}",
                        "actions/checkout must set persist-credentials to false",
                    )
                )
    return tuple(findings)


def _unconditional_action_inputs(
    raw_step: object,
    action: str,
) -> Mapping[str, object] | None:
    if (
        not isinstance(raw_step, dict)
        or raw_step.get("uses") != action
        or "if" in raw_step
        or raw_step.get("continue-on-error") not in {None, "false"}
    ):
        return None
    inputs = raw_step.get("with")
    return inputs if isinstance(inputs, dict) else {}


def _release_command_has_image_binding(
    steps: Sequence[object],
    command: str,
) -> bool:
    pattern = re.compile(r"scripts/verify_supply_chain\.py\s+([a-z-]+)")
    for raw_step in steps:
        if not isinstance(raw_step, dict):
            continue
        script = raw_step.get("run")
        environment = raw_step.get("env")
        if (
            not isinstance(script, str)
            or not isinstance(environment, dict)
            or environment.get("IMAGE_NAME") != _RELEASE_IMAGE_NAME_OUTPUT
        ):
            continue
        matches = tuple(pattern.finditer(script))
        for index, match in enumerate(matches):
            if match.group(1) != command:
                continue
            end = matches[index + 1].start() if index + 1 < len(matches) else len(script)
            command_text = script[match.start() : end]
            if (
                '--image-name "$IMAGE_NAME"' in command_text
                and f'--image-digest "{_RELEASE_MANIFEST_DIGEST}"' in command_text
            ):
                return True
    return False


def _release_attestation_findings(
    path: Path,
    document: _YamlDocument,
    root: Path,
) -> tuple[Finding, ...]:
    relative = str(path.relative_to(root))
    if relative != _RELEASE_WORKFLOW_PATH:
        return ()
    findings: list[Finding] = []
    jobs = document.value.get("jobs")
    attest = jobs.get("attest") if isinstance(jobs, dict) else None
    if (
        document.value.get("on") != {"release": {"types": ["published"]}}
        or not isinstance(attest, dict)
        or attest.get("environment") != "production-release"
        or "if" in attest
    ):
        findings.append(
            Finding(
                "release_attestation_missing",
                relative,
                "protected published-release attest job is absent or unsafe",
            )
        )
    if not isinstance(attest, dict):
        return tuple(findings)

    if not _valid_job_timeout(attest.get("timeout-minutes")):
        findings.append(
            Finding(
                "release_attest_timeout_invalid",
                f"{relative}:attest",
                "the protected release attest job needs a timeout of 1 to 180 minutes",
            )
        )

    permissions = attest.get("permissions")
    if permissions != _RELEASE_JOB_PERMISSIONS:
        findings.append(
            Finding(
                "release_attestation_missing",
                relative,
                "the release job permissions are not the exact reviewed set",
            )
        )
    if not isinstance(permissions, dict) or permissions.get("packages") != "write":
        findings.append(
            Finding(
                "release_registry_permission_missing",
                relative,
                "GHCR publication requires packages: write on the protected release job",
            )
        )

    steps_value = attest.get("steps")
    if not isinstance(steps_value, list):
        findings.append(
            Finding(
                "release_attestation_missing",
                relative,
                "the protected release job has no structural steps list",
            )
        )
        return tuple(findings)
    steps = tuple(steps_value)
    if not steps or steps[0] != _RELEASE_APPROVAL_STEP:
        findings.append(
            Finding(
                "release_approval_sentinel_missing",
                relative,
                "the protected environment sentinel must be the exact first release step",
            )
        )
    scripts = tuple(
        step["run"] for step in steps if isinstance(step, dict) and isinstance(step.get("run"), str)
    )
    release_audit_steps = tuple(
        index
        for index, raw_step in enumerate(steps)
        if isinstance(raw_step, dict)
        and isinstance(raw_step.get("run"), str)
        and _RELEASE_AUDIT_COMMAND.search(raw_step["run"]) is not None
    )
    if len(release_audit_steps) != 1:
        findings.append(
            Finding(
                "release_clean_audit_missing",
                relative,
                "one exact clean-revision release audit is required before publication",
            )
        )
    image_name_steps = tuple(
        index
        for index, raw_step in enumerate(steps)
        if isinstance(raw_step, dict)
        and raw_step
        == {
            "name": "Resolve the lowercase GHCR image name",
            "id": "image_name",
            "shell": "bash",
            "env": {"REPOSITORY_SLUG": "${{ github.repository }}"},
            "run": _RELEASE_IMAGE_NAME_SCRIPT,
        }
    )
    if len(image_name_steps) != 1:
        findings.append(
            Finding(
                "release_image_name_invalid",
                relative,
                "the release subject must be a validated lowercase ghcr.io repository output",
            )
        )
    if any(_FORBIDDEN_RELEASE_DOCKER_COMMAND.search(script) for script in scripts):
        findings.append(
            Finding(
                "release_local_image_digest_forbidden",
                relative,
                "release image build, push, and digest capture must use the reviewed action output",
            )
        )

    login_step_indexes = tuple(
        index
        for index, raw_step in enumerate(steps)
        if (inputs := _unconditional_action_inputs(raw_step, _LOGIN_ACTION)) is not None
        and inputs.get("registry") == "ghcr.io"
        and inputs.get("username") == "${{ github.actor }}"
        and inputs.get("password") == "${{ secrets.GITHUB_TOKEN }}"
        and set(inputs) == {"registry", "username", "password"}
    )
    if len(login_step_indexes) != 1:
        findings.append(
            Finding(
                "release_registry_login_missing",
                relative,
                "the protected job must authenticate to ghcr.io with its scoped GitHub token",
            )
        )

    buildx_present = any(
        _unconditional_action_inputs(raw_step, _SETUP_BUILDX_ACTION) is not None
        for raw_step in steps
    )
    build_push_present = False
    build_step_indexes: list[int] = []
    for index, raw_step in enumerate(steps):
        inputs = _unconditional_action_inputs(raw_step, _BUILD_PUSH_ACTION)
        if inputs is None or not isinstance(raw_step, dict):
            continue
        build_args = inputs.get("build-args")
        build_push_present |= (
            raw_step.get("id") == "build"
            and inputs.get("context") == "."
            and inputs.get("file") == "Dockerfile.runtime"
            and isinstance(build_args, str)
            and {line.strip() for line in build_args.splitlines() if line.strip()}
            == {"SCHEMABRIDGE_RELEASE_REF=${{ github.sha }}"}
            and inputs.get("platforms") == "linux/amd64"
            and inputs.get("pull") == "true"
            and inputs.get("push") == "true"
            and inputs.get("provenance") == "false"
            and inputs.get("sbom") == "false"
            and inputs.get("tags") == _RELEASE_IMAGE_TAG
        )
        if build_push_present:
            build_step_indexes.append(index)
    if (
        len(image_name_steps) != 1
        or len(build_step_indexes) != 1
        or image_name_steps[0] >= build_step_indexes[0]
    ):
        build_push_present = False
    if (
        len(release_audit_steps) != 1
        or len(login_step_indexes) != 1
        or len(build_step_indexes) != 1
        or release_audit_steps[0] >= login_step_indexes[0]
        or release_audit_steps[0] >= build_step_indexes[0]
    ):
        findings.append(
            Finding(
                "release_clean_audit_missing",
                relative,
                "the exact clean-revision audit must precede registry login and image build",
            )
        )
    if not buildx_present or not build_push_present:
        findings.append(
            Finding(
                "release_build_push_missing",
                relative,
                "one reviewed Buildx action must build and push the exact release tag",
            )
        )

    remote_sbom_present = False
    remote_vulnerability_scan_present = False
    for raw_step in steps:
        inputs = _unconditional_action_inputs(raw_step, _TRIVY_ACTION)
        if inputs is None or inputs.get("image-ref") != _RELEASE_IMAGE_REFERENCE:
            continue
        remote_sbom_present |= (
            inputs.get("scan-type") == "image"
            and inputs.get("format") == "cyclonedx"
            and inputs.get("output") == ".local/supply-chain/runtime-image.cdx.json"
        )
        remote_vulnerability_scan_present |= (
            inputs.get("scan-type") == "image"
            and inputs.get("scanners") == "vuln"
            and inputs.get("format") == "json"
            and inputs.get("output") == ".local/supply-chain/trivy-image.json"
            and inputs.get("severity") == "HIGH,CRITICAL"
            and inputs.get("ignore-unfixed") == "false"
            and inputs.get("list-all-pkgs") == "true"
            and inputs.get("exit-code") == "0"
        )
    if not remote_sbom_present or not remote_vulnerability_scan_present:
        findings.append(
            Finding(
                "release_remote_scan_missing",
                relative,
                "SBOM and vulnerability scans must read the pushed image by manifest digest",
            )
        )

    required_commands = ("generate-evidence", "vulnerabilities", "verify-evidence")
    if not all(_release_command_has_image_binding(steps, command) for command in required_commands):
        findings.append(
            Finding(
                "release_manifest_digest_binding_missing",
                relative,
                "all evidence commands must bind the Buildx manifest digest and tag-free name",
            )
        )

    wheel_attested = False
    evidence_attested = False
    registry_image_attested = False
    for raw_step in steps:
        inputs = _unconditional_action_inputs(raw_step, _ATTEST_ACTION)
        if inputs is None:
            continue
        subject_path = inputs.get("subject-path")
        if isinstance(subject_path, str):
            paths = {line.strip() for line in subject_path.splitlines() if line.strip()}
            wheel_attested |= paths == {".local/supply-chain/dist/*.whl"}
            evidence_attested |= paths >= _RELEASE_EVIDENCE_PATHS
        registry_image_attested |= (
            inputs.get("subject-name") == _RELEASE_IMAGE_NAME_OUTPUT
            and inputs.get("subject-digest") == _RELEASE_MANIFEST_DIGEST
            and inputs.get("push-to-registry") == "true"
        )
    missing_file_attestations = tuple(
        name
        for name, present in (
            ("wheel", wheel_attested),
            ("sbom-and-provenance", evidence_attested),
        )
        if not present
    )
    if missing_file_attestations:
        findings.append(
            Finding(
                "release_attestation_missing",
                relative,
                ",".join(missing_file_attestations),
            )
        )
    if not registry_image_attested:
        findings.append(
            Finding(
                "release_registry_attestation_missing",
                relative,
                "runtime-image provenance must be pushed against the Buildx manifest digest",
            )
        )
    return tuple(findings)


def verify_workflows(root: Path) -> tuple[Finding, ...]:
    workflow_root = root / ".github/workflows"
    findings: list[Finding] = []
    paths = tuple(sorted((*workflow_root.glob("*.yml"), *workflow_root.glob("*.yaml"))))
    if not paths:
        return (Finding("workflow_missing", ".github/workflows", "no workflow exists"),)
    if workflow_root / "release-evidence.yml" not in paths:
        findings.append(
            Finding(
                "release_attestation_missing",
                ".github/workflows/release-evidence.yml",
                "protected release evidence workflow is absent",
            )
        )
    for path in paths:
        relative = str(path.relative_to(root))
        documents, parse_findings = _parse_yaml_documents(path, relative=relative)
        findings.extend(parse_findings)
        if len(documents) != 1:
            if not parse_findings:
                findings.append(
                    Finding(
                        "workflow_document_count_invalid",
                        relative,
                        "a workflow must contain exactly one YAML document",
                    )
                )
            continue
        document = documents[0]
        triggers = document.value.get("on")
        unsafe_trigger = (
            (isinstance(triggers, dict) and "pull_request_target" in triggers)
            or (isinstance(triggers, list) and "pull_request_target" in triggers)
            or triggers == "pull_request_target"
        )
        if unsafe_trigger:
            findings.append(
                Finding(
                    "unsafe_workflow_trigger",
                    relative,
                    "pull_request_target is forbidden for repository build code",
                )
            )
        findings.extend(_remote_action_findings(path, document, root))
        findings.extend(_top_level_permissions_findings(path, document, root))
        findings.extend(_workflow_job_permission_findings(path, document, root))
        findings.extend(_ci_workflow_findings(path, document, root))
        findings.extend(_supply_chain_artifact_upload_findings(path, document, root))
        findings.extend(_checkout_credentials_findings(path, document, root))
        findings.extend(_release_attestation_findings(path, document, root))
    return tuple(findings)


def verify_docker_context(root: Path) -> tuple[Finding, ...]:
    path = root / ".dockerignore"
    if not path.is_file():
        return (
            Finding(
                "docker_context_secret_exclusion_missing",
                path.name,
                "Docker context exclusions are absent",
            ),
        )
    try:
        rules = tuple(
            stripped
            for line in path.read_text(encoding="utf-8").splitlines()
            if (stripped := line.strip()) and not stripped.startswith("#")
        )
    except (OSError, UnicodeError):
        return (
            Finding(
                "docker_context_secret_exclusion_missing",
                path.name,
                "Docker context exclusions are unreadable",
            ),
        )
    last_negation = max(
        (index for index, rule in enumerate(rules) if rule.startswith("!")),
        default=-1,
    )
    missing = tuple(
        pattern
        for pattern in _DOCKER_CONTEXT_SECRET_EXCLUSIONS
        if not (positions := tuple(index for index, rule in enumerate(rules) if rule == pattern))
        or positions[-1] <= last_negation
    )
    if not missing:
        return ()
    return (
        Finding(
            "docker_context_secret_exclusion_missing",
            path.name,
            f"missing effective exclusions: {','.join(missing)}",
        ),
    )


def verify_precommit(root: Path) -> tuple[Finding, ...]:
    path = root / ".pre-commit-config.yaml"
    if not path.is_file():
        return (Finding("precommit_missing", path.name, "configuration is absent"),)
    findings: list[Finding] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("rev:"):
            continue
        value, _, comment = stripped.removeprefix("rev:").strip().partition("#")
        if _FULL_SHA.fullmatch(value.strip()) is None:
            findings.append(
                Finding(
                    "precommit_not_sha_pinned",
                    f"{path.name}:{line_number}",
                    "remote hook revisions require a full commit SHA",
                )
            )
        if _REVIEWED_VERSION.search(f"#{comment}") is None:
            findings.append(
                Finding(
                    "precommit_reviewed_version_missing",
                    f"{path.name}:{line_number}",
                    "the reviewed release must be retained in a comment",
                )
            )
    return tuple(findings)


def _pinned_image(reference: str) -> bool:
    if "@sha256:" not in reference:
        return False
    _, digest = reference.rsplit("@sha256:", maxsplit=1)
    return _SHA256.fullmatch(digest) is not None


def _yaml_image_findings(
    path: Path,
    *,
    root: Path,
    code: str,
) -> tuple[Finding, ...]:
    relative = str(path.relative_to(root))
    documents, parse_findings = _parse_yaml_documents(path, relative=relative)
    if parse_findings:
        return parse_findings
    findings: list[Finding] = []
    for document in documents:
        try:
            image_nodes = _yaml_nodes_for_key(document.root, "image")
        except ValueError:
            findings.append(Finding("yaml_image_invalid", relative, "image must be a scalar"))
            continue
        for image_node in image_nodes:
            if not _pinned_image(image_node.value):
                findings.append(
                    Finding(
                        code,
                        f"{relative}:{image_node.start_mark.line + 1}",
                        image_node.value,
                    )
                )
    return tuple(findings)


def verify_images(root: Path) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    dockerfiles = tuple(sorted(path for path in root.glob("Dockerfile*") if path.is_file()))
    for path in dockerfiles:
        relative = str(path.relative_to(root))
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = _FROM.match(line)
            if match is not None and not _pinned_image(match.group(1)):
                findings.append(
                    Finding(
                        "base_image_not_digest_pinned",
                        f"{relative}:{line_number}",
                        match.group(1),
                    )
                )
    for path in (
        *sorted(root.glob("docker-compose*.yml")),
        *sorted(root.glob("docker-compose*.yaml")),
    ):
        findings.extend(
            _yaml_image_findings(
                path,
                root=root,
                code="compose_image_not_digest_pinned",
            )
        )
    workflow_root = root / ".github/workflows"
    for path in (
        *sorted(workflow_root.glob("*.yml")),
        *sorted(workflow_root.glob("*.yaml")),
    ):
        findings.extend(
            _yaml_image_findings(
                path,
                root=root,
                code="workflow_image_not_digest_pinned",
            )
        )
    runtime = root / "Dockerfile.runtime"
    if not runtime.is_file():
        findings.append(Finding("runtime_dockerfile_missing", runtime.name, "file is absent"))
        return tuple(findings)
    text = runtime.read_text(encoding="utf-8")
    required_fragments = (
        "requirements/build.txt",
        "requirements/runtime.txt",
        "--require-hashes",
        "--no-deps",
        "--no-build-isolation",
    )
    for fragment in required_fragments:
        if fragment not in text:
            findings.append(
                Finding(
                    "runtime_install_not_frozen",
                    runtime.name,
                    f"required frozen-build fragment is absent: {fragment}",
                )
            )
    if re.search(r"pip\s+install[^\n]*['\"]?\.\[", text):
        findings.append(
            Finding(
                "runtime_mutable_resolution",
                runtime.name,
                "production image must not resolve project extras",
            )
        )
    return tuple(findings)


def _uv_binary(root: Path) -> str:
    configured = os.environ.get("UV")
    candidates = (
        configured,
        shutil.which("uv"),
        str(root / ".venv/bin/uv"),
        str(root / ".venv/Scripts/uv.exe"),
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise SupplyChainViolation(
        (
            Finding(
                "uv_unavailable",
                "uv.lock",
                f"uv {UV_VERSION} is required to verify the lock",
            ),
        )
    )


def _runtime_export_command(uv: str) -> tuple[str, ...]:
    command = [
        uv,
        "export",
        "--frozen",
        "--no-dev",
        "--no-emit-project",
        "--no-annotate",
        "--no-header",
    ]
    for extra in RUNTIME_EXTRAS:
        command.extend(("--extra", extra))
    return tuple(command)


def _build_export_command(uv: str) -> tuple[str, ...]:
    return (
        uv,
        "export",
        "--frozen",
        "--only-group",
        "build",
        "--no-emit-project",
        "--no-annotate",
        "--no-header",
    )


def verify_lock_and_exports(root: Path) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    lock = root / "uv.lock"
    if not lock.is_file():
        return (Finding("lock_missing", lock.name, "complete uv lock is absent"),)
    uv = _uv_binary(root)
    version = subprocess.run(
        (uv, "--version"), cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    if version.split()[:2] != ["uv", UV_VERSION]:
        findings.append(Finding("uv_version_mismatch", "pyproject.toml", f"observed {version!r}"))
    checked = subprocess.run(
        (uv, "lock", "--check", "--no-python-downloads"),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if checked.returncode != 0:
        findings.append(
            Finding("lock_stale", "uv.lock", "uv lock --check rejected current project metadata")
        )
    for relative, command in (
        (RUNTIME_REQUIREMENTS, _runtime_export_command(uv)),
        (BUILD_REQUIREMENTS, _build_export_command(uv)),
    ):
        path = root / relative
        if not path.is_file():
            findings.append(Finding("requirements_missing", str(relative), "export is absent"))
            continue
        exported = subprocess.run(
            command,
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        if exported.returncode != 0:
            findings.append(
                Finding("requirements_export_failed", str(relative), "uv export failed closed")
            )
        elif exported.stdout != path.read_text(encoding="utf-8"):
            findings.append(
                Finding(
                    "requirements_export_stale",
                    str(relative),
                    "checked-in bytes differ from the frozen uv export",
                )
            )
    return tuple(findings)


def static_findings(root: Path) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for relative in (RUNTIME_REQUIREMENTS, BUILD_REQUIREMENTS):
        try:
            load_requirements(root, relative)
        except SupplyChainViolation as error:
            findings.extend(error.findings)
    findings.extend(verify_lock_and_exports(root))
    findings.extend(verify_workflows(root))
    findings.extend(verify_docker_context(root))
    findings.extend(verify_precommit(root))
    findings.extend(verify_images(root))
    exceptions = root / EXCEPTIONS_PATH
    if not exceptions.is_file():
        findings.append(
            Finding("vulnerability_exceptions_missing", str(EXCEPTIONS_PATH), "file is absent")
        )
    else:
        try:
            load_exceptions(exceptions)
        except SupplyChainViolation as error:
            findings.extend(error.findings)
    return tuple(sorted(findings, key=lambda item: (item.path, item.code, item.detail)))


def _direct_specs(pyproject: Mapping[str, Any]) -> tuple[str, ...]:
    project = pyproject["project"]
    specs = list(project.get("dependencies", ()))
    for values in project.get("optional-dependencies", {}).values():
        specs.extend(values)
    for values in pyproject.get("dependency-groups", {}).values():
        specs.extend(values)
    return tuple(str(value) for value in specs)


def _license_identifier(package_metadata: metadata.PackageMetadata) -> str | None:
    values = cast(Mapping[str, str], package_metadata)
    expression = values.get("License-Expression")
    if expression:
        return " ".join(expression.split())
    raw = values.get("License")
    if raw:
        folded = " ".join(raw.split())
        if folded in _ALLOWED_LICENSES:
            return folded
        aliases = {
            "apache 2.0": "Apache-2.0",
            "apache license 2.0": "Apache-2.0",
            "bsd 2-clause": "BSD-2-Clause",
            "bsd 3-clause": "BSD-3-Clause",
            "isc license": "ISC",
            "mit license": "MIT",
            "mozilla public license 2.0": "MPL-2.0",
            "python software foundation license": "PSF-2.0",
        }
        if identifier := aliases.get(folded.casefold()):
            return identifier
    classifiers = package_metadata.get_all("Classifier") or ()
    joined = " | ".join(
        value for value in classifiers if value.startswith("License :: OSI Approved")
    )
    return _license_identifier_from_classifier(joined) or (" ".join(raw.split()) if raw else None)


def _license_identifier_from_classifier(value: str) -> str | None:
    lower = value.casefold()
    mapping = {
        "apache software license": "Apache-2.0",
        "bsd license": "BSD-3-Clause",
        "isc license": "ISC",
        "mit license": "MIT",
        "mozilla public license 2.0": "MPL-2.0",
        "python software foundation license": "PSF-2.0",
    }
    for fragment, identifier in mapping.items():
        if fragment in lower:
            return identifier
    return None


def _allowed_license_expression(value: str) -> bool:
    without_operators = re.sub(r"\b(?:AND|OR)\b|[()]", " ", value)
    identifiers = without_operators.split()
    return bool(identifiers) and all(identifier in _ALLOWED_LICENSES for identifier in identifiers)


def direct_license_inventory(root: Path) -> tuple[DirectLicense, ...]:
    with (root / "pyproject.toml").open("rb") as stream:
        pyproject = tomllib.load(stream)
    with (root / "uv.lock").open("rb") as stream:
        locked = tomllib.load(stream)
    locked_versions: dict[str, set[str]] = {}
    for package in locked.get("package", ()):
        locked_versions.setdefault(normalize_name(package["name"]), set()).add(package["version"])

    findings: list[Finding] = []
    inventory: list[DirectLicense] = []
    names = {
        normalize_name(match.group(1))
        for spec in _direct_specs(pyproject)
        if (match := _DIRECT_NAME.match(spec)) is not None
    }
    for name in sorted(names):
        try:
            package_metadata = metadata.metadata(name)
            version = metadata.version(name)
        except metadata.PackageNotFoundError:
            findings.append(
                Finding(
                    "direct_dependency_not_installed",
                    "pyproject.toml",
                    f"{name} is absent from the frozen environment",
                )
            )
            continue
        if version not in locked_versions.get(name, set()):
            findings.append(
                Finding(
                    "direct_dependency_not_locked",
                    "uv.lock",
                    f"{name}=={version} is not in the complete lock",
                )
            )
        license_id = _license_identifier(package_metadata)
        if license_id is None or license_id.casefold() == "unknown":
            findings.append(
                Finding(
                    "direct_dependency_license_unknown",
                    "pyproject.toml",
                    f"{name}=={version} has no recognized license metadata",
                )
            )
            continue
        if _allowed_license_expression(license_id):
            inventory.append(DirectLicense(name, version, license_id))
            continue
        upper = license_id.upper()
        if any(fragment in upper for fragment in _PROHIBITED_LICENSE_FRAGMENTS):
            findings.append(
                Finding(
                    "direct_dependency_license_prohibited",
                    "pyproject.toml",
                    f"{name}=={version} uses {license_id}",
                )
            )
            continue
        findings.append(
            Finding(
                "direct_dependency_license_unapproved",
                "pyproject.toml",
                f"{name}=={version} uses unreviewed {license_id}",
            )
        )
    if findings:
        raise SupplyChainViolation(findings)
    return tuple(inventory)


def write_license_report(root: Path, output: Path) -> None:
    inventory = direct_license_inventory(root)
    payload = {
        "schema_version": 1,
        "policy": {
            "allowed": sorted(_ALLOWED_LICENSES),
            "unknown_allowed": False,
        },
        "dependencies": [asdict(item) for item in inventory],
    }
    _write_json(output, payload)


def load_exceptions(path: Path) -> dict[tuple[str, str], Mapping[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SupplyChainViolation(
            (Finding("vulnerability_exceptions_invalid", str(path), type(error).__name__),)
        ) from error
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "exceptions"}:
        raise SupplyChainViolation(
            (
                Finding(
                    "vulnerability_exceptions_invalid",
                    str(path),
                    "expected exactly schema_version and exceptions",
                ),
            )
        )
    if raw["schema_version"] != 1 or not isinstance(raw["exceptions"], list):
        raise SupplyChainViolation(
            (Finding("vulnerability_exceptions_invalid", str(path), "unsupported schema"),)
        )
    exceptions: dict[tuple[str, str], Mapping[str, Any]] = {}
    findings: list[Finding] = []
    expected = {"id", "package", "expires", "owner", "reason"}
    for index, item in enumerate(raw["exceptions"]):
        location = f"{path}:{index + 1}"
        if not isinstance(item, dict) or set(item) != expected:
            findings.append(
                Finding("vulnerability_exception_invalid", location, "unexpected exception shape")
            )
            continue
        if not all(isinstance(item[key], str) and item[key].strip() for key in expected):
            findings.append(
                Finding(
                    "vulnerability_exception_invalid",
                    location,
                    "every exception value must be non-empty text",
                )
            )
            continue
        try:
            expires = date.fromisoformat(item["expires"])
        except ValueError:
            findings.append(
                Finding("vulnerability_exception_invalid", location, "expires is not ISO date")
            )
            continue
        if expires <= date.today():
            findings.append(Finding("vulnerability_exception_expired", location, item["expires"]))
            continue
        key = (item["id"], normalize_name(item["package"]))
        if key in exceptions:
            findings.append(Finding("vulnerability_exception_duplicate", location, repr(key)))
            continue
        exceptions[key] = item
    if findings:
        raise SupplyChainViolation(findings)
    return exceptions


def _pip_audit_vulnerabilities(payload: Mapping[str, Any]) -> tuple[tuple[str, str, str], ...]:
    found: list[tuple[str, str, str]] = []
    dependencies = payload.get("dependencies")
    if not isinstance(dependencies, list):
        return ()
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            continue
        name = normalize_name(str(dependency.get("name", "")))
        vulnerabilities = dependency.get("vulns")
        if not isinstance(vulnerabilities, list):
            continue
        for vulnerability in vulnerabilities:
            if isinstance(vulnerability, dict) and vulnerability.get("id"):
                found.append((str(vulnerability["id"]), name, "HIGH"))
    return tuple(found)


def _expected_pip_audit_dependencies(root: Path) -> frozenset[tuple[str, str]]:
    try:
        from packaging.markers import Marker
    except ImportError as error:
        raise SupplyChainViolation(
            (
                Finding(
                    "vulnerability_report_validation_unavailable",
                    str(RUNTIME_REQUIREMENTS),
                    "PEP 508 marker evaluation is unavailable",
                ),
            )
        ) from error
    expected: set[tuple[str, str]] = set()
    for requirement in load_requirements(root, RUNTIME_REQUIREMENTS):
        if requirement.marker is None or Marker(requirement.marker).evaluate():
            expected.add((requirement.normalized_name, requirement.version))
    if not expected:
        raise SupplyChainViolation(
            (
                Finding(
                    "vulnerability_report_validation_unavailable",
                    str(RUNTIME_REQUIREMENTS),
                    "no applicable runtime dependencies were found",
                ),
            )
        )
    return frozenset(expected)


def _validate_pip_audit_report(
    payload: Mapping[str, Any],
    *,
    path: Path,
    expected_dependencies: frozenset[tuple[str, str]],
) -> tuple[tuple[tuple[str, str, str], ...], tuple[Finding, ...]]:
    location = str(path)
    dependencies = payload.get("dependencies")
    fixes = payload.get("fixes")
    if set(payload) != {"dependencies", "fixes"} or not isinstance(fixes, list):
        return (
            (),
            (
                Finding(
                    "vulnerability_report_incomplete",
                    location,
                    "pip-audit report requires exact dependencies and fixes collections",
                ),
            ),
        )
    if not isinstance(dependencies, list) or not dependencies:
        return (
            (),
            (
                Finding(
                    "vulnerability_report_incomplete",
                    location,
                    "pip-audit dependencies must be a non-empty list",
                ),
            ),
        )
    observed_dependencies: set[tuple[str, str]] = set()
    findings: list[Finding] = []
    for index, dependency in enumerate(dependencies, start=1):
        item_location = f"{location}:{index}"
        if not isinstance(dependency, dict) or set(dependency) != {"name", "version", "vulns"}:
            findings.append(
                Finding(
                    "vulnerability_report_incomplete",
                    item_location,
                    "pip-audit dependency was skipped or has an unexpected shape",
                )
            )
            continue
        name = dependency.get("name")
        version = dependency.get("version")
        vulnerabilities = dependency.get("vulns")
        if (
            not isinstance(name, str)
            or not normalize_name(name)
            or not isinstance(version, str)
            or not version
            or not isinstance(vulnerabilities, list)
        ):
            findings.append(
                Finding(
                    "vulnerability_report_invalid",
                    item_location,
                    "pip-audit dependency fields are invalid",
                )
            )
            continue
        dependency_identity = (normalize_name(name), version)
        if dependency_identity in observed_dependencies:
            findings.append(
                Finding(
                    "vulnerability_report_invalid",
                    item_location,
                    "pip-audit dependency is duplicated",
                )
            )
        observed_dependencies.add(dependency_identity)
        for vulnerability in vulnerabilities:
            if (
                not isinstance(vulnerability, dict)
                or not isinstance(vulnerability.get("id"), str)
                or not vulnerability["id"]
                or not isinstance(vulnerability.get("fix_versions"), list)
            ):
                findings.append(
                    Finding(
                        "vulnerability_report_invalid",
                        item_location,
                        "pip-audit vulnerability fields are invalid",
                    )
                )
                break
    if observed_dependencies != expected_dependencies:
        findings.append(
            Finding(
                "vulnerability_report_incomplete",
                location,
                "pip-audit dependency set does not match the applicable frozen runtime export",
            )
        )
    return _pip_audit_vulnerabilities(payload), tuple(findings)


def _trivy_vulnerabilities(payload: Mapping[str, Any]) -> tuple[tuple[str, str, str], ...]:
    found: list[tuple[str, str, str]] = []
    results = payload.get("Results")
    if not isinstance(results, list):
        return ()
    for result in results:
        if not isinstance(result, dict):
            continue
        vulnerabilities = result.get("Vulnerabilities") or ()
        if not isinstance(vulnerabilities, list):
            continue
        for vulnerability in vulnerabilities:
            if not isinstance(vulnerability, dict):
                continue
            vulnerability_id = vulnerability.get("VulnerabilityID")
            package = vulnerability.get("PkgName")
            severity = str(vulnerability.get("Severity", "UNKNOWN")).upper()
            if vulnerability_id and package and severity in {"HIGH", "CRITICAL"}:
                found.append((str(vulnerability_id), normalize_name(str(package)), severity))
    return tuple(found)


def _validate_trivy_report(
    payload: Mapping[str, Any],
    *,
    path: Path,
    image_name: str,
    image_digest: str,
) -> tuple[tuple[tuple[str, str, str], ...], tuple[Finding, ...]]:
    location = str(path)
    normalized_digest = image_digest.removeprefix("sha256:")
    expected_manifest_reference = f"{image_name}@sha256:{normalized_digest}"
    metadata_value = payload.get("Metadata")
    results = payload.get("Results")
    artifact_name = payload.get("ArtifactName")
    image_id = metadata_value.get("ImageID") if isinstance(metadata_value, dict) else None
    repo_digests = metadata_value.get("RepoDigests") if isinstance(metadata_value, dict) else None
    local_image_binding = artifact_name == image_name and image_id == f"sha256:{normalized_digest}"
    remote_manifest_binding = (
        artifact_name == expected_manifest_reference
        and isinstance(image_id, str)
        and _SHA256_REFERENCE.fullmatch(image_id) is not None
        and isinstance(repo_digests, list)
        and expected_manifest_reference in repo_digests
        and all(isinstance(item, str) for item in repo_digests)
    )
    if (
        payload.get("SchemaVersion") != 2
        or not (local_image_binding or remote_manifest_binding)
        or payload.get("ArtifactType") != "container_image"
        or not isinstance(metadata_value, dict)
        or not isinstance(metadata_value.get("DiffIDs"), list)
        or not metadata_value["DiffIDs"]
        or not all(
            isinstance(item, str) and _SHA256_REFERENCE.fullmatch(item) is not None
            for item in metadata_value["DiffIDs"]
        )
        or not isinstance(results, list)
        or not results
    ):
        return (
            (),
            (
                Finding(
                    "vulnerability_report_incomplete",
                    location,
                    "Trivy report is not a complete digest-bound container-image scan",
                ),
            ),
        )
    findings: list[Finding] = []
    package_inventory_present = False
    for index, result in enumerate(results, start=1):
        item_location = f"{location}:{index}"
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("Target"), str)
            or not result["Target"]
            or not isinstance(result.get("Class"), str)
            or not result["Class"]
            or not isinstance(result.get("Type"), str)
            or not result["Type"]
        ):
            findings.append(
                Finding(
                    "vulnerability_report_invalid",
                    item_location,
                    "Trivy result identity is invalid",
                )
            )
            continue
        packages = result.get("Packages")
        if packages is not None and not isinstance(packages, list):
            findings.append(
                Finding(
                    "vulnerability_report_invalid",
                    item_location,
                    "Trivy Packages must be a list when present",
                )
            )
        elif isinstance(packages, list) and packages:
            package_inventory_present = True
        vulnerabilities = result.get("Vulnerabilities")
        if vulnerabilities is not None and not isinstance(vulnerabilities, list):
            findings.append(
                Finding(
                    "vulnerability_report_invalid",
                    item_location,
                    "Trivy Vulnerabilities must be a list when present",
                )
            )
            continue
        for vulnerability in vulnerabilities or ():
            if (
                not isinstance(vulnerability, dict)
                or not isinstance(vulnerability.get("VulnerabilityID"), str)
                or not vulnerability["VulnerabilityID"]
                or not isinstance(vulnerability.get("PkgName"), str)
                or not vulnerability["PkgName"]
                or str(vulnerability.get("Severity", "")).upper() not in {"HIGH", "CRITICAL"}
            ):
                findings.append(
                    Finding(
                        "vulnerability_report_invalid",
                        item_location,
                        "Trivy vulnerability fields are invalid",
                    )
                )
                break
    if not package_inventory_present:
        findings.append(
            Finding(
                "vulnerability_report_incomplete",
                location,
                "Trivy report lacks the requested non-empty package inventory",
            )
        )
    return _trivy_vulnerabilities(payload), tuple(findings)


def verify_vulnerability_reports(
    reports: Sequence[Path],
    exceptions_path: Path,
    *,
    root: Path,
    image_name: str,
    image_digest: str,
) -> tuple[tuple[str, str, str], ...]:
    normalized_image_digest = image_digest.removeprefix("sha256:")
    if not image_name.strip() or _SHA256.fullmatch(normalized_image_digest) is None:
        raise SupplyChainViolation(
            (
                Finding(
                    "vulnerability_report_binding_invalid",
                    "vulnerability-reports",
                    "expected image name and digest are invalid",
                ),
            )
        )
    exceptions = load_exceptions(exceptions_path)
    findings: list[Finding] = []
    observed: list[tuple[str, str, str]] = []
    observed_report_types: set[str] = set()
    expected_pip_dependencies: frozenset[tuple[str, str]] | None = None
    for path in reports:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            findings.append(
                Finding("vulnerability_report_invalid", str(path), type(error).__name__)
            )
            continue
        if not isinstance(payload, dict):
            findings.append(
                Finding("vulnerability_report_invalid", str(path), "root must be an object")
            )
            continue
        if "Results" in payload:
            observed_report_types.add("trivy")
            extracted, report_findings = _validate_trivy_report(
                payload,
                path=path,
                image_name=image_name,
                image_digest=normalized_image_digest,
            )
        elif "dependencies" in payload:
            observed_report_types.add("pip-audit")
            if expected_pip_dependencies is None:
                expected_pip_dependencies = _expected_pip_audit_dependencies(root)
            extracted, report_findings = _validate_pip_audit_report(
                payload,
                path=path,
                expected_dependencies=expected_pip_dependencies,
            )
        else:
            findings.append(
                Finding(
                    "vulnerability_report_invalid",
                    str(path),
                    "expected a Trivy Results list or pip-audit dependencies list",
                )
            )
            continue
        findings.extend(report_findings)
        observed.extend(extracted)
    if observed_report_types != {"pip-audit", "trivy"}:
        findings.append(
            Finding(
                "vulnerability_report_incomplete",
                "vulnerability-reports",
                "one complete pip-audit report and one complete Trivy report are required",
            )
        )
    for vulnerability_id, package, severity in sorted(set(observed)):
        if (vulnerability_id, package) not in exceptions:
            findings.append(
                Finding(
                    "vulnerability_unexcepted",
                    package,
                    f"{severity} {vulnerability_id}",
                )
            )
    if findings:
        raise SupplyChainViolation(findings)
    return tuple(sorted(set(observed)))


def _direct_runtime_names(root: Path) -> frozenset[str]:
    with (root / "pyproject.toml").open("rb") as stream:
        pyproject = tomllib.load(stream)
    project = pyproject["project"]
    specs = list(project["dependencies"])
    for extra in RUNTIME_EXTRAS:
        specs.extend(project["optional-dependencies"][extra])
    return frozenset(
        normalize_name(match.group(1))
        for spec in specs
        if (match := _DIRECT_NAME.match(str(spec))) is not None
    )


def build_cyclonedx_sbom(
    *,
    root: Path,
    artifact_name: str,
    artifact_type: str,
    artifact_digest: str,
    source_revision: str,
) -> Mapping[str, Any]:
    if _SHA256.fullmatch(artifact_digest) is None:
        raise ValueError("artifact digest must be a lowercase SHA-256")
    if _FULL_SHA.fullmatch(source_revision) is None:
        raise ValueError("source revision must be a lowercase full Git SHA")
    requirements = load_requirements(root, RUNTIME_REQUIREMENTS)
    direct = _direct_runtime_names(root)
    lock_digest = sha256_file(root / "uv.lock")
    root_ref = f"urn:schemabridge:{artifact_type}:{artifact_digest}"
    components = []
    for requirement in requirements:
        properties = [
            {
                "name": "schemabridge:direct-runtime-dependency",
                "value": str(requirement.normalized_name in direct).lower(),
            },
        ]
        if requirement.marker:
            properties.append(
                {"name": "schemabridge:environment-marker", "value": requirement.marker}
            )
        properties.extend(
            {
                "name": "schemabridge:allowed-distribution-sha256",
                "value": digest,
            }
            for digest in requirement.hashes
        )
        components.append(
            {
                "type": "library",
                "bom-ref": requirement.bom_ref,
                "name": requirement.normalized_name,
                "version": requirement.version,
                "purl": requirement.bom_ref,
                "properties": properties,
            }
        )
    serial = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"schemabridge:{artifact_type}:{artifact_name}:{artifact_digest}:{source_revision}:"
        f"{lock_digest}",
    )
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "component": {
                "type": artifact_type,
                "bom-ref": root_ref,
                "name": artifact_name,
                "version": "0.1.0",
                "hashes": [{"alg": "SHA-256", "content": artifact_digest}],
                "properties": [
                    {"name": "schemabridge:source-revision", "value": source_revision},
                    {"name": "schemabridge:uv-lock-sha256", "value": lock_digest},
                ],
            }
        },
        "components": components,
        "dependencies": [
            {"ref": root_ref, "dependsOn": [item["bom-ref"] for item in components]},
            *({"ref": item["bom-ref"], "dependsOn": []} for item in components),
        ],
    }


def verify_cyclonedx_sbom(
    payload: Mapping[str, Any],
    *,
    root: Path,
    artifact_digest: str,
    source_revision: str,
    linux_runtime: bool = False,
) -> None:
    findings: list[Finding] = []
    if payload.get("bomFormat") != "CycloneDX" or payload.get("specVersion") != "1.5":
        findings.append(Finding("sbom_schema_invalid", "sbom", "expected CycloneDX 1.5"))
    requirements = load_requirements(root, RUNTIME_REQUIREMENTS)
    expected = {
        (item.normalized_name, item.version)
        for item in requirements
        if not (
            linux_runtime and item.marker is not None and "sys_platform == 'win32'" in item.marker
        )
    }
    components = payload.get("components")
    observed: set[tuple[str, str]] = set()
    if isinstance(components, list):
        for item in components:
            if isinstance(item, dict):
                observed.add((normalize_name(str(item.get("name"))), str(item.get("version"))))
    component_mismatch = expected - observed if linux_runtime else expected ^ observed
    if component_mismatch:
        findings.append(
            Finding(
                "sbom_component_mismatch",
                "sbom",
                f"missing={sorted(expected - observed)!r} extra={sorted(observed - expected)!r}",
            )
        )
    component = payload.get("metadata", {}).get("component", {})
    hashes = component.get("hashes", ()) if isinstance(component, dict) else ()
    if {"alg": "SHA-256", "content": artifact_digest} not in hashes:
        findings.append(Finding("sbom_artifact_digest_mismatch", "sbom", artifact_digest))
    properties = component.get("properties", ()) if isinstance(component, dict) else ()
    expected_properties = {
        ("schemabridge:source-revision", source_revision),
        ("schemabridge:uv-lock-sha256", sha256_file(root / "uv.lock")),
    }
    observed_properties = {
        (item.get("name"), item.get("value")) for item in properties if isinstance(item, dict)
    }
    if not expected_properties <= observed_properties:
        findings.append(Finding("sbom_source_binding_mismatch", "sbom", "binding is incomplete"))
    if findings:
        raise SupplyChainViolation(findings)


def bind_existing_cyclonedx_sbom(
    payload: Mapping[str, Any],
    *,
    root: Path,
    artifact_name: str,
    artifact_digest: str,
    source_revision: str,
) -> Mapping[str, Any]:
    if payload.get("bomFormat") != "CycloneDX":
        raise SupplyChainViolation(
            (Finding("sbom_schema_invalid", "runtime-image.cdx.json", "not CycloneDX"),)
        )
    bound_value = json.loads(json.dumps(payload))
    if not isinstance(bound_value, dict):
        raise SupplyChainViolation(
            (Finding("sbom_schema_invalid", "runtime-image.cdx.json", "root"),)
        )
    bound: dict[str, Any] = bound_value
    metadata_value = bound.setdefault("metadata", {})
    if not isinstance(metadata_value, dict):
        raise SupplyChainViolation(
            (Finding("sbom_schema_invalid", "runtime-image.cdx.json", "metadata"),)
        )
    component = metadata_value.setdefault(
        "component",
        {
            "type": "container",
            "name": artifact_name,
            "version": "0.1.0",
        },
    )
    if not isinstance(component, dict):
        raise SupplyChainViolation(
            (Finding("sbom_schema_invalid", "runtime-image.cdx.json", "component"),)
        )
    component["hashes"] = [{"alg": "SHA-256", "content": artifact_digest}]
    properties = component.get("properties")
    retained = (
        [
            item
            for item in properties
            if isinstance(item, dict)
            and item.get("name")
            not in {"schemabridge:source-revision", "schemabridge:uv-lock-sha256"}
        ]
        if isinstance(properties, list)
        else []
    )
    retained.extend(
        (
            {"name": "schemabridge:source-revision", "value": source_revision},
            {
                "name": "schemabridge:uv-lock-sha256",
                "value": sha256_file(root / "uv.lock"),
            },
        )
    )
    component["properties"] = retained
    return bound


def _git_output(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def assert_clean_exact_revision(root: Path, revision: str) -> None:
    findings: list[Finding] = []
    if _FULL_SHA.fullmatch(revision) is None:
        findings.append(
            Finding("source_revision_invalid", "git", "revision must be a full lowercase SHA")
        )
    elif _git_output(root, "rev-parse", "HEAD") != revision:
        findings.append(Finding("source_revision_mismatch", "git", revision))
    if _git_output(root, "status", "--porcelain", "--untracked-files=normal"):
        findings.append(
            Finding(
                "release_tree_dirty",
                "git",
                "provenance requires an exact clean checkout",
            )
        )
    if findings:
        raise SupplyChainViolation(findings)


def build_provenance(
    *,
    root: Path,
    repository: str,
    revision: str,
    wheel: Path,
    image_name: str,
    image_digest: str,
    wheel_sbom: Path,
    image_sbom: Path,
    workflow: str,
) -> Mapping[str, Any]:
    if not repository.startswith("https://github.com/") or repository.endswith(".git"):
        raise ValueError("repository must be one canonical HTTPS GitHub URL")
    if _FULL_SHA.fullmatch(revision) is None:
        raise ValueError("revision must be a full lowercase Git SHA")
    if _SHA256.fullmatch(image_digest) is None:
        raise ValueError("image digest must be a lowercase SHA-256")
    if not workflow.startswith(".github/workflows/") or not (root / workflow).is_file():
        raise ValueError("workflow must name a checked-in GitHub Actions workflow")
    subjects = [
        {"name": wheel.name, "digest": {"sha256": sha256_file(wheel)}},
        {"name": image_name, "digest": {"sha256": image_digest}},
    ]
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": subjects,
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": PROVENANCE_BUILD_TYPE,
                "externalParameters": {
                    "repository": repository,
                    "revision": revision,
                    "workflow": workflow,
                },
                "internalParameters": {
                    "uvVersion": UV_VERSION,
                    "runtimeExtras": list(RUNTIME_EXTRAS),
                },
                "resolvedDependencies": [
                    {
                        "uri": f"git+{repository}@{revision}",
                        "digest": {"gitCommit": revision},
                    },
                    {
                        "uri": "uv.lock",
                        "digest": {"sha256": sha256_file(root / "uv.lock")},
                    },
                    {
                        "uri": wheel_sbom.name,
                        "digest": {"sha256": sha256_file(wheel_sbom)},
                    },
                    {
                        "uri": image_sbom.name,
                        "digest": {"sha256": sha256_file(image_sbom)},
                    },
                ],
            },
            "runDetails": {
                "builder": {"id": PROVENANCE_BUILDER_ID},
                "metadata": {
                    "invocationId": f"{repository}/actions/workflows/{workflow}@{revision}"
                },
            },
        },
    }


def verify_provenance(
    payload: Mapping[str, Any],
    *,
    root: Path,
    repository: str,
    revision: str,
    wheel: Path,
    image_name: str,
    image_digest: str,
    wheel_sbom: Path,
    image_sbom: Path,
    workflow: str,
) -> None:
    findings: list[Finding] = []
    if payload.get("_type") != "https://in-toto.io/Statement/v1":
        findings.append(Finding("provenance_statement_invalid", "provenance", "_type"))
    if payload.get("predicateType") != "https://slsa.dev/provenance/v1":
        findings.append(Finding("provenance_statement_invalid", "provenance", "predicateType"))
    subjects = payload.get("subject")
    expected_subjects = {
        (wheel.name, sha256_file(wheel)),
        (image_name, image_digest),
    }
    observed_subjects = (
        {
            (str(item.get("name")), str(item.get("digest", {}).get("sha256")))
            for item in subjects
            if isinstance(item, dict)
        }
        if isinstance(subjects, list)
        else set()
    )
    if observed_subjects != expected_subjects:
        findings.append(Finding("provenance_subject_mismatch", "provenance", "subjects"))
    definition = payload.get("predicate", {}).get("buildDefinition", {})
    external = definition.get("externalParameters", {}) if isinstance(definition, dict) else {}
    if (
        external.get("repository") != repository
        or external.get("revision") != revision
        or external.get("workflow") != workflow
    ):
        findings.append(Finding("provenance_source_mismatch", "provenance", "source"))
    if not isinstance(definition, dict) or definition.get("buildType") != PROVENANCE_BUILD_TYPE:
        findings.append(Finding("provenance_build_mismatch", "provenance", "build type"))
    predicate = payload.get("predicate", {})
    run_details = predicate.get("runDetails", {}) if isinstance(predicate, dict) else {}
    builder = run_details.get("builder", {}) if isinstance(run_details, dict) else {}
    expected_invocation = f"{repository}/actions/workflows/{workflow}@{revision}"
    metadata_value = run_details.get("metadata", {}) if isinstance(run_details, dict) else {}
    if (
        not isinstance(builder, dict)
        or builder.get("id") != PROVENANCE_BUILDER_ID
        or not isinstance(metadata_value, dict)
        or metadata_value.get("invocationId") != expected_invocation
    ):
        findings.append(Finding("provenance_builder_mismatch", "provenance", "builder"))
    dependencies = (
        definition.get("resolvedDependencies", ()) if isinstance(definition, dict) else ()
    )
    observed_dependencies = {
        (str(item.get("uri")), tuple(sorted(item.get("digest", {}).items())))
        for item in dependencies
        if isinstance(item, dict) and isinstance(item.get("digest"), dict)
    }
    expected_dependencies = {
        (f"git+{repository}@{revision}", (("gitCommit", revision),)),
        ("uv.lock", (("sha256", sha256_file(root / "uv.lock")),)),
        (wheel_sbom.name, (("sha256", sha256_file(wheel_sbom)),)),
        (image_sbom.name, (("sha256", sha256_file(image_sbom)),)),
    }
    if observed_dependencies != expected_dependencies:
        findings.append(
            Finding("provenance_dependency_mismatch", "provenance", "resolved dependencies")
        )
    if findings:
        raise SupplyChainViolation(findings)


def generate_evidence(
    *,
    root: Path,
    repository: str,
    revision: str,
    wheel: Path,
    image_name: str,
    image_digest: str,
    output_directory: Path,
    workflow: str,
) -> None:
    assert_clean_exact_revision(root, revision)
    output_directory.mkdir(parents=True, exist_ok=True)
    wheel_digest = sha256_file(wheel)
    normalized_image_digest = image_digest.removeprefix("sha256:")
    wheel_sbom = output_directory / "wheel.cdx.json"
    image_sbom = output_directory / "runtime-image.cdx.json"
    _write_json(
        wheel_sbom,
        build_cyclonedx_sbom(
            root=root,
            artifact_name=wheel.name,
            artifact_type="application",
            artifact_digest=wheel_digest,
            source_revision=revision,
        ),
    )
    image_payload = _read_json_object(image_sbom)
    _write_json(
        image_sbom,
        bind_existing_cyclonedx_sbom(
            image_payload,
            root=root,
            artifact_name=image_name,
            artifact_digest=normalized_image_digest,
            source_revision=revision,
        ),
    )
    _write_json(
        output_directory / "provenance.intoto.json",
        build_provenance(
            root=root,
            repository=repository,
            revision=revision,
            wheel=wheel,
            image_name=image_name,
            image_digest=normalized_image_digest,
            wheel_sbom=wheel_sbom,
            image_sbom=image_sbom,
            workflow=workflow,
        ),
    )
    write_license_report(root, output_directory / "direct-licenses.json")


def verify_evidence(
    *,
    root: Path,
    repository: str,
    revision: str,
    wheel: Path,
    image_name: str,
    image_digest: str,
    output_directory: Path,
    workflow: str,
) -> None:
    normalized_image_digest = image_digest.removeprefix("sha256:")
    wheel_sbom = output_directory / "wheel.cdx.json"
    image_sbom = output_directory / "runtime-image.cdx.json"
    provenance = output_directory / "provenance.intoto.json"
    wheel_payload = _read_json_object(wheel_sbom)
    image_payload = _read_json_object(image_sbom)
    provenance_payload = _read_json_object(provenance)
    verify_cyclonedx_sbom(
        wheel_payload,
        root=root,
        artifact_digest=sha256_file(wheel),
        source_revision=revision,
    )
    verify_cyclonedx_sbom(
        image_payload,
        root=root,
        artifact_digest=normalized_image_digest,
        source_revision=revision,
        linux_runtime=True,
    )
    verify_provenance(
        provenance_payload,
        root=root,
        repository=repository,
        revision=revision,
        wheel=wheel,
        image_name=image_name,
        image_digest=normalized_image_digest,
        wheel_sbom=wheel_sbom,
        image_sbom=image_sbom,
        workflow=workflow,
    )
    direct_license_inventory(root)


def _read_json_object(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SupplyChainViolation(
            (Finding("evidence_invalid", str(path), type(error).__name__),)
        ) from error
    if not isinstance(payload, dict):
        raise SupplyChainViolation(
            (Finding("evidence_invalid", str(path), "root must be an object"),)
        )
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("static")

    licenses = subparsers.add_parser("licenses")
    licenses.add_argument("--output", type=Path, required=True)

    vulnerabilities = subparsers.add_parser("vulnerabilities")
    vulnerabilities.add_argument("--report", type=Path, action="append", required=True)
    vulnerabilities.add_argument("--exceptions", type=Path, default=EXCEPTIONS_PATH)
    vulnerabilities.add_argument("--image-name", required=True)
    vulnerabilities.add_argument("--image-digest", required=True)

    for name in ("generate-evidence", "verify-evidence"):
        evidence = subparsers.add_parser(name)
        evidence.add_argument("--repository", required=True)
        evidence.add_argument("--revision", required=True)
        evidence.add_argument("--wheel", type=Path, required=True)
        evidence.add_argument("--image-name", required=True)
        evidence.add_argument("--image-digest", required=True)
        evidence.add_argument("--output-directory", type=Path, required=True)
        evidence.add_argument("--workflow", required=True)
    return parser


def _print_findings(findings: Sequence[Finding]) -> None:
    for finding in findings:
        print(f"{finding.code}: {finding.path}: {finding.detail}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    root = arguments.root.resolve()
    try:
        if arguments.command == "static":
            findings = static_findings(root)
            if findings:
                raise SupplyChainViolation(findings)
            print("Supply-chain static policy passed.")
        elif arguments.command == "licenses":
            write_license_report(root, arguments.output)
            print("Direct dependency license policy passed.")
        elif arguments.command == "vulnerabilities":
            exceptions = arguments.exceptions
            if not exceptions.is_absolute():
                exceptions = root / exceptions
            verify_vulnerability_reports(
                arguments.report,
                exceptions,
                root=root,
                image_name=arguments.image_name,
                image_digest=arguments.image_digest,
            )
            print("Vulnerability policy passed.")
        elif arguments.command == "generate-evidence":
            generate_evidence(
                root=root,
                repository=arguments.repository,
                revision=arguments.revision,
                wheel=arguments.wheel,
                image_name=arguments.image_name,
                image_digest=arguments.image_digest,
                output_directory=arguments.output_directory,
                workflow=arguments.workflow,
            )
            print("Unsigned attestable evidence generated.")
        elif arguments.command == "verify-evidence":
            verify_evidence(
                root=root,
                repository=arguments.repository,
                revision=arguments.revision,
                wheel=arguments.wheel,
                image_name=arguments.image_name,
                image_digest=arguments.image_digest,
                output_directory=arguments.output_directory,
                workflow=arguments.workflow,
            )
            print("SBOM and provenance bindings verified.")
        else:  # pragma: no cover - argparse prevents this branch
            raise AssertionError(arguments.command)
    except SupplyChainViolation as error:
        _print_findings(error.findings)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
