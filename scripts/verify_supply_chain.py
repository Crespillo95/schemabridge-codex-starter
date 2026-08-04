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
WATCHDOG_BUILD_REQUIREMENTS = Path("requirements/watchdog-build.txt")
BUILT_RUNTIME_REQUIREMENTS = Path("requirements/runtime-built.txt")
EXCEPTIONS_PATH = Path("requirements/vulnerability-exceptions.json")
UV_VERSION = "0.11.30"
PROVENANCE_BUILD_TYPE = "https://github.com/SchemaBridge/buildtypes/github-actions-frozen-uv/v1"
PROVENANCE_BUILDER_ID = "https://github.com/actions/runner"
SUPPORTED_CYCLONEDX_SPEC_VERSIONS = frozenset({"1.5", "1.6"})
RUNTIME_BASE_IMAGE = (
    "python:3.13.14-alpine3.24"
    "@sha256:399babc8b49529dabfd9c922f2b5eea81d611e4512e3ed250d75bd2e7683f4b0"
)
POSTGRES_CLIENT_APK_MATRIX = {
    "amd64": (
        (
            "libpq",
            "https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/libpq-18.4-r0.apk",
            "145d0d57ce40baaf2d7191e66dc18b8872822642939a0264fd4fc1c73d6599f1",
        ),
        (
            "lz4",
            "https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/lz4-libs-1.10.0-r1.apk",
            "3ad4912ab8ecf5f6236fdb751f88243006b3a8f152684e455b781e16284dd298",
        ),
        (
            "postgresql_common",
            "https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/postgresql-common-1.3-r0.apk",
            "749d8a88b56c84b415372655435600c749345113e21ce8fe9964f7e22345cbff",
        ),
        (
            "postgresql_client",
            "https://dl-cdn.alpinelinux.org/alpine/v3.24/community/x86_64/"
            "postgresql16-client-16.14-r0.apk",
            "21409f4ee297e6805a6688ba7fc91c20f66f972e79865db42b550348b20381d5",
        ),
        (
            "zstd",
            "https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/zstd-libs-1.5.7-r2.apk",
            "23c6065b0049b2406441564bcf0032515a43f78e80d76fcb85535a3803ef5d4e",
        ),
    ),
    "arm64": (
        (
            "libpq",
            "https://dl-cdn.alpinelinux.org/alpine/v3.24/main/aarch64/libpq-18.4-r0.apk",
            "d6e4808216810535808523980a78608569be2318cf05e6034ffb03f519e91a56",
        ),
        (
            "lz4",
            "https://dl-cdn.alpinelinux.org/alpine/v3.24/main/aarch64/lz4-libs-1.10.0-r1.apk",
            "af6cbe553adda941e04ac40cf0123455b714d65392472389113e7d6dcebbd8d8",
        ),
        (
            "postgresql_common",
            "https://dl-cdn.alpinelinux.org/alpine/v3.24/main/aarch64/postgresql-common-1.3-r0.apk",
            "ecddbd6272f5034f30d78ff59634c07f862a13a2ad3d29078c0e7985b827fc73",
        ),
        (
            "postgresql_client",
            "https://dl-cdn.alpinelinux.org/alpine/v3.24/community/aarch64/"
            "postgresql16-client-16.14-r0.apk",
            "d1a0095b8a4a42bd46136bec4117a64fc1f5282a0c46a01ff5642ad14bada995",
        ),
        (
            "zstd",
            "https://dl-cdn.alpinelinux.org/alpine/v3.24/main/aarch64/zstd-libs-1.5.7-r2.apk",
            "2bb5136c89f5b0bbe1554c8915a3b520d5aa63ae2a51d4d821eb81698db5a818",
        ),
    ),
}
RUNTIME_ALPINE_COMPONENTS = frozenset(
    {
        ("alpine-baselayout", "3.7.2-r1"),
        ("alpine-baselayout-data", "3.7.2-r1"),
        ("alpine-keys", "2.6-r0"),
        ("alpine-release", "3.24.1-r0"),
        ("apk-tools", "3.0.6-r0"),
        ("busybox", "1.37.0-r31"),
        ("busybox-binsh", "1.37.0-r31"),
        ("ca-certificates", "20260611-r0"),
        ("ca-certificates-bundle", "20260611-r0"),
        ("gdbm", "1.26-r0"),
        ("libapk", "3.0.6-r0"),
        ("libbz2", "1.0.8-r6"),
        ("libcrypto3", "3.5.7-r0"),
        ("libffi", "3.5.2-r1"),
        ("libncursesw", "6.6_p20260516-r0"),
        ("libpanelw", "6.6_p20260516-r0"),
        ("libpq", "18.4-r0"),
        ("libssl3", "3.5.7-r0"),
        ("libuuid", "2.42-r0"),
        ("lz4-libs", "1.10.0-r1"),
        ("musl", "1.2.6-r2"),
        ("musl-utils", "1.2.6-r2"),
        ("ncurses-terminfo-base", "6.6_p20260516-r0"),
        ("postgresql-common", "1.3-r0"),
        ("postgresql16-client", "16.14-r0"),
        ("readline", "8.3.3-r1"),
        ("scanelf", "1.3.9-r1"),
        ("sqlite-libs", "3.53.2-r0"),
        ("ssl_client", "1.37.0-r31"),
        ("tzdata", "2026b-r0"),
        ("xz-libs", "5.8.3-r0"),
        ("zlib", "1.3.2-r0"),
        ("zstd-libs", "1.5.7-r2"),
    }
)
RUNTIME_ALPINE_PLATFORM_VIRTUAL_COMPONENT = {
    "aarch64": (".python-rundeps", "20260616.002547"),
    "x86_64": (".python-rundeps", "20260616.002554"),
}
POSTGRES_CLIENT_ALPINE_COMPONENTS = frozenset(
    {
        ("libpq", "18.4-r0"),
        ("lz4-libs", "1.10.0-r1"),
        ("postgresql-common", "1.3-r0"),
        ("postgresql16-client", "16.14-r0"),
        ("zstd-libs", "1.5.7-r2"),
    }
)
RUNTIME_ALPINE_NOARCH_COMPONENTS = frozenset(RUNTIME_ALPINE_PLATFORM_VIRTUAL_COMPONENT.values())
RUNTIME_PYTHON_BASE_COMPONENTS = frozenset(
    {
        ("pip", "26.1.2"),
        ("schemabridge", "0.1.0"),
    }
)
_POSTGRES_CLIENT_COMPONENT_BY_VARIABLE = {
    "libpq": ("libpq", "18.4-r0"),
    "lz4": ("lz4-libs", "1.10.0-r1"),
    "postgresql_common": ("postgresql-common", "1.3-r0"),
    "postgresql_client": ("postgresql16-client", "16.14-r0"),
    "zstd": ("zstd-libs", "1.5.7-r2"),
}
_APK_PURL = re.compile(
    r"^pkg:apk/alpine/(?P<name>[^@?]+)@(?P<version>[^?]+)"
    r"\?arch=(?P<architecture>aarch64|x86_64|noarch)&distro=3\.24\.1$"
)
_PYPI_PURL = re.compile(r"^pkg:pypi/(?P<name>[^@?]+)@(?P<version>[^?]+)$")
_APK_ARCHITECTURE_TO_DOCKER = {"aarch64": "arm64", "x86_64": "amd64"}
_APK_SOURCE_URL_PROPERTY = "schemabridge:apk-source-url"
_APK_SOURCE_SHA256_PROPERTY = "schemabridge:apk-source-sha256"
WATCHDOG_SOURCE_DATE_EPOCH = "1730470033"
WATCHDOG_BUILD_REQUIREMENT = (
    "setuptools==83.0.0 \\\n"
    "    --hash=sha256:025bccbbf0fa05b6192bc64ae1e7b16e001fd6d6d4d5de03c97b1c1ade523bef \\\n"
    "    --hash=sha256:29b23c360f22f414dc7336bb39178cc7bcbf6021ed2733cde173f09dba19abb3\n"
)
WATCHDOG_BUILT_REQUIREMENT = (
    "watchdog @ file:///tmp/runtime-wheels/watchdog-6.0.0-py3-none-any.whl \\\n"
    "    --hash=sha256:4b510ffee66be0c794ba0a5b921451405f4c8249215168a399af15c37550ff3f\n"
)

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHA256_REFERENCE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVIEWED_VERSION = re.compile(
    r"#\s*(?:reviewed\s+)?(?P<version>v?\d+(?:\.\d+){0,3})\s*$",
    re.IGNORECASE,
)
_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[^\s;]+)(?:\s*;\s*(?P<marker>.+?))?$"
)
_HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})\b")
_FROM = re.compile(r"^\s*FROM\s+([^\s]+)", re.IGNORECASE)
_DIRECT_NAME = re.compile(r"^\s*([A-Za-z0-9_.-]+)")
_CHECKOUT_ACTION = "actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd"
_SETUP_PYTHON_ACTION = "actions/setup-python@e797f83bcb11b83ae66e0230d6156d7c80228e7c"
_SETUP_UV_ACTION = "astral-sh/setup-uv@eb1897b8dc4b5d5bfe39a428a8f2304605e0983c"
_ATTEST_ACTION = "actions/attest-build-provenance@0f67c3f4856b2e3261c31976d6725780e5e4c373"
_BUILD_PUSH_ACTION = "docker/build-push-action@53b7df96c91f9c12dcc8a07bcb9ccacbed38856a"
_SETUP_BUILDX_ACTION = "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c"
_TRIVY_ACTION = "aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25"
_UPLOAD_ARTIFACT_ACTION = "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f"
_DOWNLOAD_ARTIFACT_ACTION = "actions/download-artifact@37930b1c2abaa49bbe596cd826c3c89aef350131"
_REVIEWED_REMOTE_ACTION_VERSIONS = {
    _CHECKOUT_ACTION: "v5.0.1",
    _SETUP_PYTHON_ACTION: "v6.0.0",
    _SETUP_UV_ACTION: "v7.0.0",
    _ATTEST_ACTION: "v4.1.1",
    _BUILD_PUSH_ACTION: "v7.3.0",
    _SETUP_BUILDX_ACTION: "v4.2.0",
    _TRIVY_ACTION: "v0.36.0",
    _UPLOAD_ARTIFACT_ACTION: "v6.0.0",
    _DOWNLOAD_ARTIFACT_ACTION: "v7.0.0",
}
_RELEASE_WORKFLOW_SHA256 = "cf81724399d11f96ec4b09be33a5634fb4a1d2eaffd37da91ecf9705ae8d643d"
_M30_CAMPAIGN_WORKFLOW_SHA256 = "7419abb1fd87e66e4f24e4102c6efe28cad8f1dd7937342cadd5fddd8ce61f6f"
_RELEASE_TRIGGER = {
    "workflow_dispatch": {
        "inputs": {
            "release_tag": {
                "description": "Existing annotated canonical SemVer tag to promote",
                "required": "true",
                "type": "string",
            }
        }
    }
}
_RELEASE_ATTEST_PERMISSIONS = {
    "actions": "read",
    "attestations": "write",
    "contents": "read",
    "id-token": "write",
    "packages": "write",
}
_RELEASE_CANDIDATE_PERMISSIONS = {
    "actions": "read",
    "contents": "read",
    "packages": "write",
}
_RELEASE_CONTENT_PERMISSIONS = {
    "actions": "read",
    "attestations": "read",
    "contents": "write",
    "packages": "read",
}
_RELEASE_PREPARE_PERMISSIONS = {
    "actions": "read",
    "contents": "read",
    "packages": "read",
}
_RELEASE_AUDIT_PERMISSIONS = {
    "attestations": "read",
    "contents": "read",
    "packages": "read",
}
_RELEASE_WRITE_PERMISSIONS_BY_JOB = {
    "attest": _RELEASE_ATTEST_PERMISSIONS,
    "candidate": _RELEASE_CANDIDATE_PERMISSIONS,
    "promote": _RELEASE_CANDIDATE_PERMISSIONS,
    "release": _RELEASE_CONTENT_PERMISSIONS,
}
_CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
_RELEASE_WORKFLOW_PATH = ".github/workflows/release-evidence.yml"
_M30_CAMPAIGN_WORKFLOW_PATH = ".github/workflows/m30-manifest-attestation.yml"
_M30_CAMPAIGN_TRIGGER = {
    "workflow_dispatch": {
        "inputs": {
            "release_tag": {
                "description": "Existing annotated stable tag at protected main HEAD",
                "required": "true",
                "type": "string",
            },
            "manifest_base64": {
                "description": (
                    "Canonical public manifest bytes encoded as one base64 string; "
                    "no private cases or answer key"
                ),
                "required": "true",
                "type": "string",
            },
        }
    }
}
_M30_CAMPAIGN_PERMISSIONS = {
    "actions": "read",
    "attestations": "write",
    "contents": "read",
    "id-token": "write",
}
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
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "node_modules",
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
        immutable_reference = bool(action and _FULL_SHA.fullmatch(revision) is not None)
        if not immutable_reference:
            findings.append(
                Finding(
                    "action_not_sha_pinned",
                    f"{relative}:{line_number}",
                    "remote actions require a full 40-character commit SHA",
                )
            )
        reviewed_version = _REVIEWED_VERSION.search(f"#{comment}") if separator else None
        if reviewed_version is None:
            findings.append(
                Finding(
                    "action_reviewed_version_missing",
                    f"{relative}:{line_number}",
                    "the reviewed release must be retained in an inline comment",
                )
            )
        if not immutable_reference:
            continue
        expected_version = _REVIEWED_REMOTE_ACTION_VERSIONS.get(reference)
        if expected_version is None:
            findings.append(
                Finding(
                    "action_not_reviewed",
                    f"{relative}:{line_number}",
                    "the immutable action is absent from the closed reviewed-action allowlist",
                )
            )
        elif (
            reviewed_version is not None
            and reviewed_version.group("version").casefold() != expected_version.casefold()
        ):
            findings.append(
                Finding(
                    "action_reviewed_version_mismatch",
                    f"{relative}:{line_number}",
                    f"the reviewed release comment must be {expected_version}",
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
        exact_m30_workflow = (
            relative == _M30_CAMPAIGN_WORKFLOW_PATH
            and hashlib.sha256(path.read_bytes()).hexdigest() == _M30_CAMPAIGN_WORKFLOW_SHA256
        )
        expected_permissions = (
            _RELEASE_WRITE_PERMISSIONS_BY_JOB.get(job_name)
            if relative == _RELEASE_WORKFLOW_PATH
            else _M30_CAMPAIGN_PERMISSIONS
            if exact_m30_workflow and job_name == "sign"
            else None
        )
        expected_trigger = _M30_CAMPAIGN_TRIGGER if exact_m30_workflow else _RELEASE_TRIGGER
        expected_environment = (
            "m30-manifest-attestation" if exact_m30_workflow else "production-release"
        )
        if (
            expected_permissions is None
            or document.value.get("on") != expected_trigger
            or environment_name != expected_environment
            or raw_permissions != expected_permissions
        ):
            findings.append(
                Finding(
                    "release_permission_unprotected",
                    f"{relative}:{job_name}",
                    "write scopes are reserved for exact protected evidence workflows",
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


def _m30_campaign_workflow_findings(
    path: Path,
    document: _YamlDocument,
    root: Path,
) -> tuple[Finding, ...]:
    """Pin the only workflow allowed to authenticate M30 frozen inputs."""

    relative = str(path.relative_to(root))
    if relative != _M30_CAMPAIGN_WORKFLOW_PATH:
        return ()
    findings: list[Finding] = []
    if hashlib.sha256(path.read_bytes()).hexdigest() != _M30_CAMPAIGN_WORKFLOW_SHA256:
        findings.append(
            Finding(
                "m30_campaign_workflow_not_exact",
                relative,
                "the protected manifest attestation workflow differs from the reviewed bytes",
            )
        )
    jobs = document.value.get("jobs")
    concurrency = document.value.get("concurrency")
    validate = jobs.get("validate") if isinstance(jobs, dict) else None
    sign = jobs.get("sign") if isinstance(jobs, dict) else None
    if (
        document.value.get("on") != _M30_CAMPAIGN_TRIGGER
        or concurrency
        != {
            "group": "schemabridge-m30-manifest-attestation",
            "cancel-in-progress": "false",
        }
        or not isinstance(jobs, dict)
        or set(jobs) != {"validate", "sign"}
        or not isinstance(validate, dict)
        or validate.get("runs-on") != "ubuntu-24.04"
        or validate.get("timeout-minutes") != "15"
        or validate.get("permissions") != {"contents": "read"}
        or "environment" in validate
        or not isinstance(sign, dict)
        or sign.get("needs") != "validate"
        or sign.get("runs-on") != "ubuntu-24.04"
        or sign.get("timeout-minutes") != "5"
        or sign.get("environment") != "m30-manifest-attestation"
        or sign.get("permissions") != _M30_CAMPAIGN_PERMISSIONS
    ):
        findings.append(
            Finding(
                "m30_campaign_workflow_topology_invalid",
                relative,
                "M30 attestation requires separate bounded validation and protected signing jobs",
            )
        )
    validate_steps = validate.get("steps") if isinstance(validate, dict) else None
    sign_steps = sign.get("steps") if isinstance(sign, dict) else None
    steps = [
        *(validate_steps if isinstance(validate_steps, list) else []),
        *(sign_steps if isinstance(sign_steps, list) else []),
    ]
    actions = (
        tuple(
            step.get("uses")
            for step in steps
            if isinstance(step, dict) and isinstance(step.get("uses"), str)
        )
        if isinstance(steps, list)
        else ()
    )
    if actions != (
        _CHECKOUT_ACTION,
        _SETUP_PYTHON_ACTION,
        _SETUP_UV_ACTION,
        _UPLOAD_ARTIFACT_ACTION,
        _DOWNLOAD_ARTIFACT_ACTION,
        _ATTEST_ACTION,
    ):
        findings.append(
            Finding(
                "m30_campaign_workflow_actions_invalid",
                relative,
                "M30 attestation actions differ from the reviewed immutable sequence",
            )
        )
    signing_scripts = (
        tuple(
            step.get("run")
            for step in sign_steps
            if isinstance(step, dict) and isinstance(step.get("run"), str)
        )
        if isinstance(sign_steps, list)
        else ()
    )
    forbidden_signing_tokens = ("make ", ".venv", "python", "scripts/", "git ", "checkout")
    if any(token in script for script in signing_scripts for token in forbidden_signing_tokens):
        findings.append(
            Finding(
                "m30_campaign_signing_code_invalid",
                f"{relative}:sign",
                "the write-scoped signing job cannot execute candidate repository code",
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
    elif relative != _RELEASE_WORKFLOW_PATH:
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

    if relative == _RELEASE_WORKFLOW_PATH:
        expected_release_uploads = {
            "prepare": (
                "prepared-release-${{ github.run_id }}-${{ github.run_attempt }}",
                ".local/prepared-release/",
            ),
            "scan": (
                "release-payload-${{ github.run_id }}-${{ github.run_attempt }}",
                ".local/release-payload/",
            ),
        }
        if len(upload_steps) != len(expected_release_uploads):
            return (
                Finding(
                    "supply_chain_artifact_upload_invalid",
                    relative,
                    "release requires exactly the prepared and canonical payload uploads",
                ),
            )
        findings: list[Finding] = []
        for job_name, step_index, raw_step in upload_steps:
            expected = expected_release_uploads.get(job_name)
            inputs = raw_step.get("with")
            if (
                expected is None
                or raw_step.get("uses") != _UPLOAD_ARTIFACT_ACTION
                or "if" in raw_step
                or raw_step.get("continue-on-error") not in {None, "false"}
                or not isinstance(inputs, dict)
                or set(inputs)
                != {
                    "name",
                    "path",
                    "if-no-files-found",
                    "include-hidden-files",
                    "retention-days",
                    "compression-level",
                }
                or inputs.get("name") != expected[0]
                or _artifact_upload_paths(inputs.get("path")) != (expected[1],)
                or inputs.get("if-no-files-found") != "error"
                or inputs.get("include-hidden-files") != "true"
                or inputs.get("retention-days") != "35"
                or inputs.get("compression-level") != "0"
            ):
                findings.append(
                    Finding(
                        "supply_chain_artifact_upload_invalid",
                        f"{relative}:{job_name}:step-{step_index}",
                        "release upload must match its exact semantic-stage allowlist",
                    )
                )
        return tuple(findings)

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
    expected_paths = _SUPPLY_CHAIN_ARTIFACT_PATHS
    expected_compression: object | None = None
    condition_valid = raw_step.get("if") == "always()" if require_always else "if" not in raw_step
    upload_valid = (
        job_name == expected_job
        and raw_step.get("uses") == _UPLOAD_ARTIFACT_ACTION
        and condition_valid
        and raw_step.get("continue-on-error") in {None, "false"}
        and isinstance(inputs, dict)
        and set(inputs) == expected_input_keys
        and inputs.get("name") == expected_name
        and _artifact_upload_paths(inputs.get("path")) == expected_paths
        and inputs.get("if-no-files-found") == "error"
        and inputs.get("include-hidden-files") == "true"
        and inputs.get("retention-days") == expected_retention
        and inputs.get("compression-level") == expected_compression
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


def _trivy_cache_findings(
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
            if not isinstance(action, str) or not action.startswith("aquasecurity/trivy-action@"):
                continue
            inputs = raw_step.get("with")
            if not isinstance(inputs, dict) or inputs.get("cache-dir") != ".local/trivy-cache":
                findings.append(
                    Finding(
                        "trivy_cache_path_invalid",
                        f"{relative}:{job_name}:step-{step_index}",
                        "Trivy caches must stay in the ignored .local artifact directory",
                    )
                )
            if not isinstance(inputs, dict) or inputs.get("version") != "v0.69.3":
                findings.append(
                    Finding(
                        "trivy_version_invalid",
                        f"{relative}:{job_name}:step-{step_index}",
                        "Trivy actions must retain the reviewed v0.69.3 scanner snapshot",
                    )
                )
    return tuple(findings)


def _pip_audit_resolution_findings(
    path: Path,
    document: _YamlDocument,
    root: Path,
) -> tuple[Finding, ...]:
    relative = str(path.relative_to(root))
    expected_job = (
        "supply-chain"
        if relative == _CI_WORKFLOW_PATH
        else "prepare"
        if relative == _RELEASE_WORKFLOW_PATH
        else None
    )
    if expected_job is None:
        return ()
    jobs = document.value.get("jobs")
    raw_job = jobs.get(expected_job) if isinstance(jobs, dict) else None
    raw_steps = raw_job.get("steps") if isinstance(raw_job, dict) else None
    if not isinstance(raw_steps, list):
        return ()
    command = ".venv/bin/pip-audit --disable-pip --require-hashes --format json \\"
    scripts = tuple(
        raw_step["run"]
        for raw_step in raw_steps
        if isinstance(raw_step, dict)
        and isinstance(raw_step.get("run"), str)
        and ".venv/bin/pip-audit" in raw_step["run"]
    )
    audited_inputs = (
        RUNTIME_REQUIREMENTS,
        BUILD_REQUIREMENTS,
        WATCHDOG_BUILD_REQUIREMENTS,
    )
    if (
        len(scripts) == 1
        and scripts[0].count(command) == 1
        and all(
            scripts[0].count(f"--requirement {requirement}") == 1 for requirement in audited_inputs
        )
    ):
        return ()
    return (
        Finding(
            "pip_audit_resolution_invalid",
            f"{relative}:{expected_job}",
            "pip-audit must inspect all frozen runtime/build inputs without a resolver environment",
        ),
    )


def _ci_buildkit_findings(
    path: Path,
    document: _YamlDocument,
    root: Path,
) -> tuple[Finding, ...]:
    relative = str(path.relative_to(root))
    if relative != _CI_WORKFLOW_PATH:
        return ()
    jobs = document.value.get("jobs")
    supply_chain = jobs.get("supply-chain") if isinstance(jobs, dict) else None
    environment = supply_chain.get("env") if isinstance(supply_chain, dict) else None
    if isinstance(environment, dict) and environment.get("DOCKER_BUILDKIT") == "1":
        return ()
    return (
        Finding(
            "runtime_buildkit_invalid",
            f"{relative}:supply-chain",
            "the runtime image requires an explicit BuildKit-enabled build",
        ),
    )


def _release_attestation_findings(
    path: Path,
    document: _YamlDocument,
    root: Path,
) -> tuple[Finding, ...]:
    """Validate the reviewed staged release transaction independently of its file digest."""

    relative = str(path.relative_to(root))
    if relative != _RELEASE_WORKFLOW_PATH:
        return ()

    findings: list[Finding] = []
    workflow_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if workflow_digest != _RELEASE_WORKFLOW_SHA256:
        findings.append(
            Finding(
                "release_workflow_not_exact",
                relative,
                "release jobs, steps, scripts, actions, comments, and shell opcodes must match "
                "the reviewed fail-closed program exactly",
            )
        )

    jobs = document.value.get("jobs")
    concurrency = document.value.get("concurrency")
    if (
        document.value.get("on") != _RELEASE_TRIGGER
        or concurrency
        != {
            "group": "schemabridge-global-release-publication",
            "queue": "max",
            "cancel-in-progress": "false",
        }
        or not isinstance(jobs, dict)
        or set(jobs) != {"audit", "prepare", "candidate", "scan", "attest", "promote", "release"}
    ):
        findings.append(
            Finding(
                "release_topology_invalid",
                relative,
                "release requires the exact seven-stage graph under one global concurrency group",
            )
        )
        return tuple(findings)

    expected_jobs: Mapping[str, tuple[Mapping[str, str], object, bool]] = {
        "audit": (_RELEASE_AUDIT_PERMISSIONS, None, True),
        "prepare": (_RELEASE_PREPARE_PERMISSIONS, "audit", False),
        "candidate": (_RELEASE_CANDIDATE_PERMISSIONS, "prepare", True),
        "scan": (_RELEASE_PREPARE_PERMISSIONS, ["prepare", "candidate"], False),
        "attest": (_RELEASE_ATTEST_PERMISSIONS, "scan", True),
        "promote": (_RELEASE_CANDIDATE_PERMISSIONS, ["scan", "attest"], True),
        "release": (
            _RELEASE_CONTENT_PERMISSIONS,
            ["scan", "attest", "promote"],
            True,
        ),
    }
    for job_name, (expected_permissions, expected_needs, protected) in expected_jobs.items():
        job = jobs.get(job_name)
        if not isinstance(job, dict):
            findings.append(
                Finding(
                    "release_topology_invalid",
                    f"{relative}:{job_name}",
                    "release job must be a structural mapping",
                )
            )
            continue
        observed_needs = job.get("needs")
        needs_valid = (
            "needs" not in job if expected_needs is None else observed_needs == expected_needs
        )
        environment_valid = (
            job.get("environment") == "production-release"
            if protected
            else "environment" not in job
        )
        if (
            job.get("permissions") != expected_permissions
            or not needs_valid
            or not environment_valid
            or job.get("runs-on") != "ubuntu-24.04"
            or (
                job.get("if") != "needs.audit.outputs.published-noop != 'true'"
                if job_name == "prepare"
                else "if" in job
            )
            or not _valid_job_timeout(job.get("timeout-minutes"))
        ):
            findings.append(
                Finding(
                    "release_authority_boundary_invalid",
                    f"{relative}:{job_name}",
                    "job dependency, environment, timeout, and least-privilege scope are exact",
                )
            )

    def step_identity(raw_step: object) -> tuple[str, str] | None:
        if not isinstance(raw_step, dict):
            return None
        action = raw_step.get("uses")
        if isinstance(action, str):
            return ("uses", action)
        name = raw_step.get("name")
        script = raw_step.get("run")
        if isinstance(name, str) and isinstance(script, str):
            return ("run", name)
        return None

    expected_steps: Mapping[str, tuple[tuple[str, str], ...]] = {
        "audit": (("run", "Verify authoritative clean external state before any build"),),
        "prepare": (
            ("uses", _CHECKOUT_ACTION),
            ("run", "Verify canonical release source and protected policies"),
            ("uses", _SETUP_PYTHON_ACTION),
            ("uses", _SETUP_UV_ACTION),
            ("uses", _SETUP_BUILDX_ACTION),
            ("run", "Install and verify frozen inputs"),
            ("run", "Build the wheel once"),
            ("uses", _BUILD_PUSH_ACTION),
            ("run", "Record the unpublished local config digest"),
            ("uses", _TRIVY_ACTION),
            ("uses", _TRIVY_ACTION),
            ("run", "Audit exact Python runtime dependencies"),
            ("run", "Normalize deterministic prepublication reports"),
            ("run", "Block publication unless the local candidate passes every evidence gate"),
            ("run", "Seal the semantic prepared release payload"),
            ("uses", _UPLOAD_ARTIFACT_ACTION),
        ),
        "candidate": (
            (
                "run",
                "Verify prepared artifact and release boundary before first package mutation",
            ),
            ("run", "Validate protected environment sentinel before first mutation"),
            ("run", "Resolve the canonical GHCR image name"),
            ("run", "Publish the new stable semantic candidate"),
        ),
        "scan": (
            (
                "run",
                "Verify exact prepared payload and candidate before read-only evidence generation",
            ),
            ("run", "Authenticate the read-only registry pull used by Trivy"),
            ("uses", _TRIVY_ACTION),
            ("uses", _TRIVY_ACTION),
            (
                "run",
                "Capture the exact temporal scanner and vulnerability database identity",
            ),
            ("run", "Remove the read-only registry credential after scanning"),
            ("run", "Derive canonical public evidence using runner tools"),
            ("uses", _UPLOAD_ARTIFACT_ACTION),
        ),
        "attest": (
            (
                "run",
                "Verify canonical payload and release boundary before first attestation mutation",
            ),
            ("run", "Validate protected environment sentinel before first mutation"),
            ("uses", _ATTEST_ACTION),
            ("uses", _ATTEST_ACTION),
            ("uses", _ATTEST_ACTION),
            ("run", "Verify exact hosted attestations and unchanged source"),
        ),
        "promote": (
            (
                "run",
                "Verify canonical payload and release boundary before first promotion mutation",
            ),
            ("run", "Validate protected environment sentinel before first mutation"),
            ("run", "Promote or verify the exact candidate manifest"),
        ),
        "release": (
            (
                "run",
                "Verify canonical payload and every boundary before first content mutation",
            ),
            ("run", "Validate protected environment sentinel before first mutation"),
            ("run", "Create, resume, or publish the canonical release as the final operation"),
        ),
    }
    steps_by_job: dict[str, list[object]] = {}
    for job_name, expected in expected_steps.items():
        job = jobs.get(job_name)
        raw_steps = job.get("steps") if isinstance(job, dict) else None
        if not isinstance(raw_steps, list):
            findings.append(
                Finding(
                    "release_step_program_invalid",
                    f"{relative}:{job_name}",
                    "the reviewed job requires a structural steps list",
                )
            )
            continue
        steps_by_job[job_name] = raw_steps
        if tuple(map(step_identity, raw_steps)) != expected or any(
            not isinstance(step, dict)
            or (
                step.get("if") != "always()"
                if job_name == "scan"
                and isinstance(step, dict)
                and step.get("name") == "Remove the read-only registry credential after scanning"
                else "if" in step
            )
            or step.get("continue-on-error") not in {None, "false"}
            for step in raw_steps
        ):
            findings.append(
                Finding(
                    "release_step_program_invalid",
                    f"{relative}:{job_name}",
                    "extra, missing, reordered, conditional, or substituted steps are forbidden",
                )
            )

    privileged_jobs = ("candidate", "attest", "promote", "release")
    audit = jobs.get("audit")
    audit_outputs = audit.get("outputs") if isinstance(audit, dict) else None
    audit_steps = steps_by_job.get("audit", [])
    audit_step = audit_steps[0] if audit_steps else None
    audit_script = (
        audit_step.get("run")
        if isinstance(audit_step, dict) and isinstance(audit_step.get("run"), str)
        else ""
    )
    audit_environment = audit_step.get("env") if isinstance(audit_step, dict) else None
    published_audit = (
        audit_script.split("verify_exact_published_release() {\n", maxsplit=1)[1].split(
            "\n}\n",
            maxsplit=1,
        )[0]
        if "verify_exact_published_release() {\n" in audit_script
        else ""
    )
    audit_mutations = (
        "gh release create",
        "gh release upload",
        "gh release edit",
        "docker push",
        "--request PUT",
        "--method POST",
        "--method PATCH",
        "--method DELETE",
        "--method PUT",
    )
    if (
        audit_outputs != {"published-noop": "${{ steps.preflight.outputs.published-noop }}"}
        or not isinstance(audit_step, dict)
        or audit_step.get("id") != "preflight"
        or not isinstance(audit_environment, dict)
        or audit_environment.get("RELEASE_AUDIT_TOKEN")
        != "${{ secrets.SCHEMABRIDGE_RELEASE_AUDIT_TOKEN }}"
        or audit_environment.get("GH_TOKEN") != "${{ secrets.GITHUB_TOKEN }}"
        or "verify_exact_published_release" not in audit_script
        or "published-noop=true" not in audit_script
        or "published-noop=false" not in audit_script
        or 'test "$(jq -r \'.permissions.push == true\' <<<"$repository")" = "true"'
        not in audit_script
        or '"repos/$GITHUB_REPOSITORY/branches/main"' not in audit_script
        or 'test "$(jq -r \'.protected\' <<<"$branch_payload")" = "true"' not in audit_script
        or 'if [[ "$release_count" = "1" ]]; then' not in audit_script
        or "canonical_stable_semver" not in audit_script
        or "semver_greater" not in audit_script
        or "a newer stable release already exists" not in audit_script
        or "a newer stable registry tag already exists" not in audit_script
        or "[.[] | select(.draft == true)] | length == 0" not in audit_script
        or "/tags/list?n=100" not in audit_script
        or "ambiguous registry pagination" not in audit_script
        or "unsafe registry next link" not in audit_script
        or 'test "$registry_page" -le 1000' not in audit_script
        or 'test "$registry_page" = "1"' not in audit_script
        or '.errors | length > 0 and all(.code == "NAME_UNKNOWN")' not in audit_script
        or audit_script.find("published-noop=true")
        > audit_script.find("canonical_stable_semver() {")
        or 'test "$(jq -r \'.target_commitish\' <<<"$release")" = "$SOURCE_REVISION"'
        not in published_audit
        or 'test "$(jq -r \'.draft\' <<<"$release")" = "false"' not in published_audit
        or 'test "$(jq -r \'.immutable\' <<<"$release")" = "true"' not in published_audit
        or "([.[].name] | unique | length) == 10" not in published_audit
        or 'if [[ "$code" = "404" ]]' not in audit_script
        or "gh attestation verify" not in published_audit
        or "--bundle-from-oci" not in published_audit
        or "release-assets.sha256" not in published_audit
        or "declare -A checksum_names=()" not in published_audit
        or 'test "${#checksum_names[@]}" = "9"' not in published_audit
        or "maximum_size=4096" not in published_audit
        or "maximum_size=65536" not in published_audit
        or published_audit.find("gh attestation verify") < 0
        or published_audit.find("sha256sum --strict --check release-assets.sha256")
        < published_audit.find("gh attestation verify")
        or published_audit.find('metadata="$asset_directory/release-metadata.json"')
        < published_audit.find("sha256sum --strict --check release-assets.sha256")
        or "releases/latest" in published_audit
        or "reject_newer_stable_release" in published_audit
        or "git/ref/heads/main" in published_audit
        or any(token in audit_script for token in audit_mutations)
    ):
        findings.append(
            Finding(
                "release_audit_preflight_invalid",
                f"{relative}:audit",
                "the protected GET-only audit must fail before build on dirty state and "
                "accept only an exact attested immutable published no-op",
            )
        )
    boundary_tokens = (
        "actions/artifacts/$ARTIFACT_ID",
        "actions/artifacts/$ARTIFACT_ID/zip",
        ".size_in_bytes",
        'test "$(jq -r \'.digest\' <<<"$artifact_payload")" = "sha256:$ARTIFACT_DIGEST"',
        'test "$(sha256sum "$archive_path" | cut -d\' \' -f1)" = "$ARTIFACT_DIGEST"',
        "zipfile.ZipFile",
        "PurePosixPath",
        "entry.flag_bits & 0x1",
        "entry.external_attr",
        "payload.testzip()",
        "ZIP uncompressed-size limit exceeded",
        "test ! -e",
        "sha256sum --strict --check SHA256SUMS",
        'test "$(remote_tag_commit)" = "$SOURCE_REVISION"',
        "verify_default_head",
        "verify_authoritative_release_controls",
        "rulesets?includes_parents=true&targets=tag",
        "rulesets/$ruleset_id?includes_parents=true",
        'has("bypass_actors")',
        'include == ["refs/tags/v*"]',
        '== ["deletion", "non_fast_forward", "update"]',
        "immutable-releases",
        ".enabled == true",
    )
    for job_name, steps in steps_by_job.items():
        for step_index, step in enumerate(steps, start=1):
            if not isinstance(step, dict) or not isinstance(step.get("run"), str):
                continue
            script = step["run"]
            if (
                step.get("shell") != "bash"
                or not script.startswith("set -euo pipefail\n")
                or re.search(r"(?m)^\s*set\s+\+e(?:\s|$)", script)
                or re.search(r"\|\|\s*true(?:\s|$)", script)
            ):
                findings.append(
                    Finding(
                        "release_strict_shell_invalid",
                        f"{relative}:{job_name}:step-{step_index}",
                        "every inline program must be strict Bash without error-suppression bypasses",
                    )
                )
    for job_name in privileged_jobs:
        steps = steps_by_job.get(job_name, [])
        first = steps[0] if steps else None
        script = first.get("run") if isinstance(first, dict) else None
        environment = first.get("env") if isinstance(first, dict) else None
        action_values = {
            step.get("uses")
            for step in steps
            if isinstance(step, dict) and isinstance(step.get("uses"), str)
        }
        if (
            not isinstance(script, str)
            or any(token not in script for token in boundary_tokens)
            or script.count("verify_default_head") < 2
            or script.count("verify_authoritative_release_controls") < 2
            or not isinstance(environment, dict)
            or environment.get("RELEASE_AUDIT_TOKEN")
            != "${{ secrets.SCHEMABRIDGE_RELEASE_AUDIT_TOKEN }}"
            or any(
                isinstance(action, str)
                and (
                    action.startswith("actions/checkout@")
                    or action.startswith("actions/setup-python@")
                    or action.startswith("astral-sh/setup-uv@")
                    or action.startswith("./")
                )
                for action in action_values
            )
        ):
            findings.append(
                Finding(
                    "release_boundary_verification_invalid",
                    f"{relative}:{job_name}",
                    "write jobs must validate ZIP content, source HEAD, rulesets, and immutable "
                    "releases before their first mutation",
                )
            )
        combined = "\n".join(
            step["run"]
            for step in steps
            if isinstance(step, dict) and isinstance(step.get("run"), str)
        )
        if re.search(
            r"(?m)(?:^|[;&|]\s*)(?:make|\.venv/|scripts/)\b",
            combined,
        ) or re.search(r"(?m)(?:^|[;&|]\s*)python3?\b(?!\s+-)", combined):
            findings.append(
                Finding(
                    "release_unsealed_code_execution",
                    f"{relative}:{job_name}",
                    "privileged jobs may not execute repository code or bootstrap dependencies",
                )
            )

    zip_tokens = (
        ".size_in_bytes",
        "zipfile.ZipFile",
        "len(entries) != len(expected)",
        "set(names) != expected_set",
        '"\\x00" in name',
        '"\\\\" in name',
        "path.is_absolute()",
        "len(path.parts) != 1",
        "entry.flag_bits & 0x1",
        "entry.external_attr",
        "file_type not in {0, stat.S_IFREG}",
        "entry.file_size > 1073741824",
        "entry.file_size / entry.compress_size > 100",
        "payload.testzip()",
        "test ! -e",
        "! -type f",
    )
    default_head_tokens = (
        'test "$(jq -r \'.default_branch\' <<<"$repository")" = "main"',
        'test "$(jq -r \'.object.type\' <<<"$default_ref")" = "commit"',
        'test "$(jq -r \'.object.sha\' <<<"$default_ref")" = "$SOURCE_REVISION"',
        '"repos/$GITHUB_REPOSITORY/branches/main"',
        'test "$(jq -r \'.name\' <<<"$branch_payload")" = "main"',
        'test "$(jq -r \'.protected\' <<<"$branch_payload")" = "true"',
        'test "$(jq -r \'.commit.sha\' <<<"$branch_payload")" = "$SOURCE_REVISION"',
    )
    for job_name in privileged_jobs:
        steps = steps_by_job.get(job_name, [])
        first = steps[0] if steps else None
        script = first.get("run") if isinstance(first, dict) else ""
        if not isinstance(script, str) or any(token not in script for token in zip_tokens):
            findings.append(
                Finding(
                    "release_zip_validation_invalid",
                    f"{relative}:{job_name}",
                    "artifact ZIPs require bounded structural, type, encryption, CRC, and "
                    "fresh-extraction validation before any privileged mutation",
                )
            )
        if not isinstance(script, str) or any(token not in script for token in default_head_tokens):
            findings.append(
                Finding(
                    "release_default_head_invalid",
                    f"{relative}:{job_name}",
                    "every privileged boundary must require SOURCE_REVISION to be the exact "
                    "remote default-branch HEAD",
                )
            )

    for job_name in ("candidate", "scan", "attest", "promote", "release"):
        steps = steps_by_job.get(job_name, [])
        first = steps[0] if steps else None
        script = first.get("run") if isinstance(first, dict) else ""
        prepared_contract = job_name in {"candidate", "scan"}
        expected_call = (
            'verify_checksum_manifest_exact "$PREPARED_DIRECTORY/SHA256SUMS"'
            if prepared_contract
            else 'verify_checksum_manifest_exact "$RELEASE_PAYLOAD_DIRECTORY/SHA256SUMS"'
        )
        if (
            not isinstance(script, str)
            or "verify_checksum_manifest_exact() {" not in script
            or 'test "$(stat --format=\'%s\' "$checksum_file")" -le 4096' not in script
            or "([0-9a-f]{64})\\ \\ ([A-Za-z0-9][A-Za-z0-9._-]*)" not in script
            or "declare -A" in script
            or expected_call not in script
            or (
                not prepared_contract
                and 'verify_checksum_manifest_exact "$RELEASE_PAYLOAD_DIRECTORY/release-assets.sha256"'
                not in script
            )
            or script.find(expected_call) > script.find("sha256sum --strict --check SHA256SUMS")
        ):
            findings.append(
                Finding(
                    "release_checksum_manifest_invalid",
                    f"{relative}:{job_name}",
                    "every checksum file must have a bounded exact basename grammar and full "
                    "allowlist before sha256sum reads any named path",
                )
            )

    privileged_scripts = "\n".join(
        step["run"]
        for job_name in privileged_jobs
        for step in steps_by_job.get(job_name, [])
        if isinstance(step, dict) and isinstance(step.get("run"), str)
    )
    audit_get = re.compile(
        r'GH_TOKEN="\$RELEASE_AUDIT_TOKEN"\s+\\\s*\n\s*'
        r"gh api --method GET",
    )
    audit_non_get = re.compile(
        r'GH_TOKEN="\$RELEASE_AUDIT_TOKEN"\s+\\\s*\n\s*'
        r"gh api --method (?!GET\b)[A-Z]+",
    )
    if (
        "/rules/tags/" in privileged_scripts
        or len(audit_get.findall(privileged_scripts)) < 12
        or audit_non_get.search(privileged_scripts)
        or privileged_scripts.count("rulesets?includes_parents=true&targets=tag&per_page=100") < 6
        or privileged_scripts.count("rulesets/$ruleset_id?includes_parents=true") < 6
        or privileged_scripts.count('has("bypass_actors")') < 6
        or privileged_scripts.count('== ["deletion", "non_fast_forward", "update"]') < 6
        or privileged_scripts.count("immutable-releases") < 6
        or re.search(
            r"(?m)^\s*(?:echo|printf)\b[^\n]*RELEASE_AUDIT_TOKEN",
            privileged_scripts,
        )
    ):
        findings.append(
            Finding(
                "release_ruleset_audit_invalid",
                relative,
                "authoritative tag rulesets and immutable releases require a non-logged "
                "environment audit token used only by GET requests",
            )
        )

    prepare_policy_step = (
        steps_by_job.get("prepare", [None, None])[1]
        if len(steps_by_job.get("prepare", [])) > 1
        else None
    )
    prepare_policy_script = (
        prepare_policy_step.get("run")
        if isinstance(prepare_policy_step, dict) and isinstance(prepare_policy_step.get("run"), str)
        else ""
    )
    if any(
        token not in prepare_policy_script
        for token in (
            "canonical_semver",
            'test "$REF_TYPE" = "tag"',
            '[[ "$RELEASE_TAG" != *-* ]]',
            'test "$RELEASE_TAG" = "v$project_version"',
            'test "$(remote_tag_commit)" = "$SOURCE_REVISION"',
            "verify_tag_ruleset_metadata",
            "verify_default_head",
            "verify_public_external_state_metadata",
            "rulesets?includes_parents=true&targets=tag",
            "rulesets/$ruleset_id?includes_parents=true",
            'include == ["refs/tags/v*"]',
            '== ["deletion", "non_fast_forward", "update"]',
            'test "$(git rev-parse origin/main)" = "$SOURCE_REVISION"',
            'test "$(jq -r \'.default_branch\' <<<"$repository")" = "main"',
            'test "$(jq -r \'.object.sha\' <<<"$default_ref")" = "$SOURCE_REVISION"',
            "releases?per_page=100",
            '"candidate-$SOURCE_REVISION" "$RELEASE_TAG"',
            "manifests/$reference",
            "required_context in quality postgres-integration supply-chain",
            'test "$required_jobs" = "3:3:3"',
        )
    ) or (
        "SCHEMABRIDGE_RELEASE_AUDIT_TOKEN" in prepare_policy_script
        or "bypass_actors" in prepare_policy_script
    ):
        findings.append(
            Finding(
                "release_prepare_policy_invalid",
                f"{relative}:prepare",
                "prepare must prove stable tag, exact default HEAD, CI, ruleset metadata, and "
                "a public/basic external-state check without the administrative audit token",
            )
        )

    prepare = jobs.get("prepare")
    candidate = jobs.get("candidate")
    scan = jobs.get("scan")
    attest = jobs.get("attest")
    promote = jobs.get("promote")
    release = jobs.get("release")
    prepare_outputs = prepare.get("outputs") if isinstance(prepare, dict) else None
    candidate_outputs = candidate.get("outputs") if isinstance(candidate, dict) else None
    scan_outputs = scan.get("outputs") if isinstance(scan, dict) else None
    candidate_env = candidate.get("env") if isinstance(candidate, dict) else None
    scan_env = scan.get("env") if isinstance(scan, dict) else None
    if (
        not isinstance(prepare_outputs, dict)
        or prepare_outputs.get("artifact-id") != "${{ steps.upload_prepared.outputs.artifact-id }}"
        or prepare_outputs.get("artifact-digest")
        != "${{ steps.upload_prepared.outputs.artifact-digest }}"
        or not isinstance(candidate_outputs, dict)
        or set(candidate_outputs) != {"candidate-tag", "image-digest", "image-id", "image-name"}
        or not isinstance(scan_outputs, dict)
        or scan_outputs.get("artifact-id") != "${{ steps.upload_release.outputs.artifact-id }}"
        or scan_outputs.get("artifact-digest")
        != "${{ steps.upload_release.outputs.artifact-digest }}"
        or scan_outputs.get("candidate-tag") != "${{ needs.candidate.outputs.candidate-tag }}"
        or scan_outputs.get("image-digest") != "${{ needs.candidate.outputs.image-digest }}"
        or scan_outputs.get("image-id") != "${{ needs.candidate.outputs.image-id }}"
        or scan_outputs.get("image-name") != "${{ needs.candidate.outputs.image-name }}"
        or not isinstance(candidate_env, dict)
        or candidate_env.get("ARTIFACT_ID") != "${{ needs.prepare.outputs.artifact-id }}"
        or candidate_env.get("ARTIFACT_DIGEST") != "${{ needs.prepare.outputs.artifact-digest }}"
        or candidate_env.get("ARTIFACT_NAME") != "${{ needs.prepare.outputs.artifact-name }}"
        or candidate_env.get("CANDIDATE_TAG") != "candidate-${{ github.sha }}"
        or not isinstance(scan_env, dict)
        or scan_env.get("ARTIFACT_ID") != "${{ needs.prepare.outputs.artifact-id }}"
        or scan_env.get("ARTIFACT_DIGEST") != "${{ needs.prepare.outputs.artifact-digest }}"
        or scan_env.get("ARTIFACT_NAME") != "${{ needs.prepare.outputs.artifact-name }}"
        or scan_env.get("CANDIDATE_TAG") != "${{ needs.candidate.outputs.candidate-tag }}"
    ):
        findings.append(
            Finding(
                "release_rerun_identity_invalid",
                relative,
                "stages must consume actual upstream outputs and use one stable semantic candidate",
            )
        )
    for job_name, job in (("attest", attest), ("promote", promote), ("release", release)):
        environment = job.get("env") if isinstance(job, dict) else None
        if not isinstance(environment, dict) or any(
            environment.get(key) != f"${{{{ needs.scan.outputs.{output} }}}}"
            for key, output in (
                ("ARTIFACT_DIGEST", "artifact-digest"),
                ("ARTIFACT_ID", "artifact-id"),
                ("ARTIFACT_NAME", "artifact-name"),
                ("CANDIDATE_TAG", "candidate-tag"),
                ("IMAGE_DIGEST", "image-digest"),
                ("IMAGE_ID", "image-id"),
                ("IMAGE_NAME", "image-name"),
            )
        ):
            findings.append(
                Finding(
                    "release_rerun_identity_invalid",
                    f"{relative}:{job_name}",
                    "downstream retries must reuse the exact candidate outputs",
                )
            )

    prepare_scripts = "\n".join(
        step["run"]
        for step in steps_by_job.get("prepare", [])
        if isinstance(step, dict) and isinstance(step.get("run"), str)
    )
    candidate_scripts = "\n".join(
        step["run"]
        for step in steps_by_job.get("candidate", [])
        if isinstance(step, dict) and isinstance(step.get("run"), str)
    )
    scan_scripts = "\n".join(
        step["run"]
        for step in steps_by_job.get("scan", [])
        if isinstance(step, dict) and isinstance(step.get("run"), str)
    )
    derivation_step = next(
        (
            step
            for step in steps_by_job.get("scan", [])
            if isinstance(step, dict)
            and step.get("name") == "Derive canonical public evidence using runner tools"
        ),
        None,
    )
    derivation_script = (
        derivation_step.get("run")
        if isinstance(derivation_step, dict) and isinstance(derivation_step.get("run"), str)
        else ""
    )
    local_image_build = next(
        (
            step
            for step in steps_by_job.get("prepare", [])
            if isinstance(step, dict)
            and step.get("uses") == _BUILD_PUSH_ACTION
            and step.get("id") == "local_build"
        ),
        None,
    )
    if (
        not isinstance(local_image_build, dict)
        or local_image_build.get("env")
        != {
            "DOCKER_BUILD_RECORD_UPLOAD": "false",
            "SOURCE_DATE_EPOCH": "1730470033",
        }
        or "run_id:" in prepare_scripts
        or "run_attempt:" in prepare_scripts
        or any(
            token in derivation_script
            for token in (
                "$ARTIFACT_ID",
                "$ARTIFACT_DIGEST",
                "$GITHUB_RUN_ID",
                "$GITHUB_RUN_ATTEMPT",
                "$RUN_ID",
                "$RUN_ATTEMPT",
            )
        )
        or "candidate-${{ github.run_id }}" in candidate_scripts
        or "del(.serialNumber, .metadata.timestamp)" not in scan_scripts
        or "del(.CreatedAt)" not in scan_scripts
        or "prepared_components" not in scan_scripts
        or "registry_components" not in scan_scripts
        or 'test "$registry_components" = "$prepared_components"' not in scan_scripts
        or ".Metadata.DiffIDs" not in scan_scripts
        or 'any(.Packages | type == "array" and length > 0)' not in scan_scripts
    ):
        findings.append(
            Finding(
                "release_evidence_determinism_invalid",
                relative,
                "public evidence must exclude execution identity, suppress build-record uploads, "
                "and bind normalized complete reports without claiming bitwise rebuild identity",
            )
        )

    def named_run(job_name: str, name: str) -> Mapping[str, object] | None:
        return next(
            (
                step
                for step in steps_by_job.get(job_name, [])
                if isinstance(step, dict)
                and step.get("name") == name
                and isinstance(step.get("run"), str)
            ),
            None,
        )

    pip_audit_step = named_run("prepare", "Audit exact Python runtime dependencies")
    pip_audit_script = pip_audit_step.get("run") if isinstance(pip_audit_step, dict) else ""
    seal_step = named_run("prepare", "Seal the semantic prepared release payload")
    seal_script = seal_step.get("run") if isinstance(seal_step, dict) else ""
    scan_login_step = named_run("scan", "Authenticate the read-only registry pull used by Trivy")
    scan_login_script = scan_login_step.get("run") if isinstance(scan_login_step, dict) else ""
    scan_capture_step = named_run(
        "scan", "Capture the exact temporal scanner and vulnerability database identity"
    )
    scan_capture_script = (
        scan_capture_step.get("run") if isinstance(scan_capture_step, dict) else ""
    )
    scan_cleanup_step = named_run("scan", "Remove the read-only registry credential after scanning")
    scan_cleanup_script = (
        scan_cleanup_step.get("run") if isinstance(scan_cleanup_step, dict) else ""
    )
    scan_trivy_steps = [
        step
        for step in steps_by_job.get("scan", [])
        if isinstance(step, dict) and step.get("uses") == _TRIVY_ACTION
    ]
    if (
        not isinstance(scan, dict)
        or scan.get("permissions") != _RELEASE_PREPARE_PERMISSIONS
        or not isinstance(scan_env, dict)
        or scan_env.get("DOCKER_CONFIG") != ".local/trivy-docker-config"
        or not isinstance(scan_login_step, dict)
        or not isinstance(scan_login_step.get("env"), dict)
        or scan_login_step["env"].get("GH_TOKEN") != "${{ secrets.GITHUB_TOKEN }}"
        or "docker login ghcr.io" not in scan_login_script
        or "--password-stdin" not in scan_login_script
        or not isinstance(scan_cleanup_step, dict)
        or scan_cleanup_step.get("if") != "always()"
        or "cleanup_status=0" not in scan_cleanup_script
        or "if docker logout ghcr.io; then" not in scan_cleanup_script
        or 'if rm -f "$DOCKER_CONFIG/config.json"; then' not in scan_cleanup_script
        or 'if rmdir "$DOCKER_CONFIG"; then' not in scan_cleanup_script
        or 'exit "$cleanup_status"' not in scan_cleanup_script
        or not (
            scan_cleanup_script.index("if docker logout ghcr.io; then")
            < scan_cleanup_script.index('if rm -f "$DOCKER_CONFIG/config.json"; then')
            < scan_cleanup_script.index('if rmdir "$DOCKER_CONFIG"; then')
            < scan_cleanup_script.index('exit "$cleanup_status"')
        )
        or len(scan_trivy_steps) != 2
        or any(
            not isinstance(step.get("with"), dict)
            or step["with"].get("cache-dir") != ".local/trivy-cache"
            or step["with"].get("version") != "v0.69.3"
            or step["with"].get("image-ref") != "${{ env.IMAGE_NAME }}@${{ env.IMAGE_DIGEST }}"
            for step in scan_trivy_steps
        )
    ):
        findings.append(
            Finding(
                "release_scan_auth_invalid",
                f"{relative}:scan",
                "the read-only scan must provide and then erase one pull-only GHCR credential",
            )
        )
    scanner_snapshot_tokens = (
        "trivy --version",
        'test "$trivy_version" = "0.69.3"',
        ".local/trivy-cache/db/metadata.json",
        ".local/trivy-cache/db/trivy.db",
        'database_sha256="$(sha256sum "$database_file"',
        'metadata_sha256="$(sha256sum "$database_metadata"',
        "schema_version",
        "updated_at",
        "next_update",
        "downloaded_at",
        "observed_at",
        "scanner-evidence.json",
    )
    if (
        not isinstance(pip_audit_script, str)
        or "--vulnerability-service pypi" not in pip_audit_script
        or 'test "$pip_audit_version" = "2.10.1"' not in pip_audit_script
        or "https://pypi.org/pypi" not in pip_audit_script
        or "pip-audit-observation.json" not in pip_audit_script
        or not isinstance(seal_script, str)
        or "scanner_evidence" not in seal_script
        or "pip_audit" not in seal_script
        or not isinstance(scan_capture_script, str)
        or any(token not in scan_capture_script for token in scanner_snapshot_tokens)
        or "scanner_evidence" not in derivation_script
        or "pip_audit_evidence" not in derivation_script
        or "trivy_evidence" not in derivation_script
        or "vulnerability_database" not in derivation_script
        or ".vulnerability_database.sha256" not in derivation_script
    ):
        findings.append(
            Finding(
                "release_scanner_snapshot_invalid",
                relative,
                "attested release metadata must identify the temporal pip-audit service and "
                "the exact Trivy tool, cache metadata, and vulnerability DB bytes used",
            )
        )

    action_allowlist: Mapping[str, frozenset[str]] = {
        "audit": frozenset(),
        "candidate": frozenset(),
        "scan": frozenset({_TRIVY_ACTION, _UPLOAD_ARTIFACT_ACTION}),
        "attest": frozenset({_ATTEST_ACTION}),
        "promote": frozenset(),
        "release": frozenset(),
    }
    for job_name, allowed_actions in action_allowlist.items():
        actions = {
            step["uses"]
            for step in steps_by_job.get(job_name, [])
            if isinstance(step, dict) and isinstance(step.get("uses"), str)
        }
        if not actions <= allowed_actions:
            findings.append(
                Finding(
                    "release_action_allowlist_invalid",
                    f"{relative}:{job_name}",
                    f"privileged action outside allowlist: {sorted(actions - allowed_actions)!r}",
                )
            )

    forbidden_everywhere = (
        (r"\bgh\s+release\s+(?:delete|download|view)\b", "GitHub release destructive/generic"),
        (r"\bgh\s+api\s+--method\s+(?:DELETE|PATCH|POST|PUT)\b", "mutable GitHub API"),
        (r"\bcurl\b[\s\S]*?--request\s+(?:DELETE|PATCH)\b", "destructive registry API"),
        (r"\bgit\s+push\b", "Git ref publication"),
        (r"(?m)(?:^|[;&|]\s*)(?:eval|source)\s+", "dynamic shell evaluation"),
        (r"\b(?:bash|sh)\s+-c\b", "nested shell evaluation"),
        (r"\bif\s+false\b", "dead publication branch"),
    )
    for job_name, steps in steps_by_job.items():
        scripts = "\n".join(
            step["run"]
            for step in steps
            if isinstance(step, dict) and isinstance(step.get("run"), str)
        )
        normalized_scripts = re.sub(r"\\\s*\n", " ", scripts)
        for pattern, operation in forbidden_everywhere:
            if re.search(pattern, normalized_scripts, flags=re.IGNORECASE):
                findings.append(
                    Finding(
                        "release_opcode_forbidden",
                        f"{relative}:{job_name}",
                        operation,
                    )
                )
        if job_name != "candidate" and re.search(
            r"\bdocker\s+(?:image\s+)?push\b", normalized_scripts
        ):
            findings.append(
                Finding(
                    "release_opcode_forbidden",
                    f"{relative}:{job_name}",
                    "container push is candidate-only",
                )
            )
        if job_name != "promote" and re.search(
            r"\bcurl\b[\s\S]*?--request\s+PUT\b", normalized_scripts
        ):
            findings.append(
                Finding(
                    "release_opcode_forbidden",
                    f"{relative}:{job_name}",
                    "registry manifest PUT is promotion-only",
                )
            )
        if job_name != "release" and re.search(
            r"\bgh\s+release\s+(?:create|upload|edit)\b", normalized_scripts
        ):
            findings.append(
                Finding(
                    "release_opcode_forbidden",
                    f"{relative}:{job_name}",
                    "GitHub release mutation is content-job-only",
                )
            )

    canonical_body_program = (
        "printf '%s\\n\\n%s\\n%s\\n%s\\n' \\\n"
        '  "# SchemaBridge $RELEASE_TAG" \\\n'
        '  "- Source: \\`$SOURCE_REVISION\\`" \\\n'
        '  "- Image: \\`$IMAGE_NAME@$IMAGE_DIGEST\\`" \\\n'
        '  "- Checksums: \\`release-assets.sha256\\`" \\\n'
        '  > "$RELEASE_ASSETS_DIRECTORY/release-body.md"'
    )
    if (
        "release-body.md" not in derivation_script
        or '--arg body_sha256 "$(' not in derivation_script
        or "body_sha256" not in derivation_script
        or '--arg release_body_sha256 "$release_body_digest"' not in derivation_script
        or "release-body.md \\" not in derivation_script
        or canonical_body_program not in derivation_script
        or not isinstance(release, dict)
    ):
        findings.append(
            Finding(
                "release_body_contract_invalid",
                relative,
                "one deterministic body file and its digest must be payload-bound",
            )
        )
    release_steps = steps_by_job.get("release", [])
    release_final = release_steps[-1] if release_steps else None
    release_script = (
        release_final.get("run")
        if isinstance(release_final, dict) and isinstance(release_final.get("run"), str)
        else ""
    )
    if release_script.count("verify_default_head") < 3:
        findings.append(
            Finding(
                "release_default_head_invalid",
                f"{relative}:release",
                "release must revalidate the exact default-branch HEAD before each mutation "
                "boundary",
            )
        )
    if (
        '--notes-file "$RELEASE_PAYLOAD_DIRECTORY/release-body.md"' not in release_script
        or "jq -jr '.body'" not in release_script
        or "body_sha256" not in release_script
        or "release_body_sha256" not in release_script
        or 'test "$(jq -r \'.immutable\' <<<"$release")" = "true"' not in release_script
        or 'test "$(jq -r \'.immutable\' <<<"$release")" = "false"' not in release_script
        or '"$RELEASE_PAYLOAD_DIRECTORY/release-body.md" \\' not in release_script
    ):
        findings.append(
            Finding(
                "release_body_contract_invalid",
                f"{relative}:release",
                "draft, published no-op, and canonical payload require exact body and immutability",
            )
        )
    if (
        "reconcile_draft_assets" not in release_script
        or "verify_assets_exact" not in release_script
        or "([.[].name] | length) == 10" not in release_script
        or "([.[].name] | unique | length) == 10" not in release_script
        or "release-body.md" not in release_script
        or ".digest" not in release_script
        or ".size" not in release_script
        or ".state" not in release_script
        or "--clobber" in release_script
    ):
        findings.append(
            Finding(
                "release_asset_contract_invalid",
                f"{relative}:release",
                "draft reconciliation and published verification must allow exactly ten "
                "digest- and size-bound assets without clobber",
            )
        )
    attest_steps = steps_by_job.get("attest", [])
    attest_final = attest_steps[-1] if attest_steps else None
    attest_script = (
        attest_final.get("run")
        if isinstance(attest_final, dict) and isinstance(attest_final.get("run"), str)
        else ""
    )
    release_boundary = release_steps[0] if release_steps else None
    release_boundary_script = (
        release_boundary.get("run")
        if isinstance(release_boundary, dict) and isinstance(release_boundary.get("run"), str)
        else ""
    )
    attestation_tokens = (
        "gh attestation verify",
        "release-body.md",
        '--source-digest "$SOURCE_REVISION"',
        '--source-ref "$SOURCE_REF"',
        "--deny-self-hosted-runners",
        "--bundle-from-oci",
    )
    evidence_attestation = attest_steps[3] if len(attest_steps) > 3 else None
    evidence_inputs = (
        evidence_attestation.get("with") if isinstance(evidence_attestation, dict) else None
    )
    if (
        any(
            token not in script
            for script in (attest_script, release_boundary_script)
            for token in attestation_tokens
        )
        or not isinstance(evidence_inputs, dict)
        or "release-body.md" not in str(evidence_inputs.get("subject-path", ""))
    ):
        findings.append(
            Finding(
                "release_attestation_verification_invalid",
                relative,
                "attest and release must verify exact source-bound artifact and OCI attestations",
            )
        )
    release_transaction = release_script.rsplit(
        'canonical_stable_semver "$RELEASE_TAG"\n', maxsplit=1
    )[-1]
    published_marker = 'if [[ "$(jq -r \'.draft\' <<<"$release")" = "false" ]]; then\n'
    published_branch = (
        release_transaction.split(published_marker, maxsplit=1)[1].split("\nfi\n", maxsplit=1)[0]
        if published_marker in release_transaction
        else ""
    )
    published_verifier = (
        release_script.split("verify_exact_published_release() {\n", maxsplit=1)[1].split(
            "\n}\n",
            maxsplit=1,
        )[0]
        if "verify_exact_published_release() {\n" in release_script
        else ""
    )
    published_mutations = (
        "gh release create",
        "gh release upload",
        "gh release edit",
        "reconcile_draft_assets",
        "--request PUT",
        "docker push",
    )
    if (
        "canonical_stable_semver" not in release_script
        or release_script.count("reject_newer_stable_release") < 3
        or "semver_greater" not in release_script
        or release_script.count("verify_default_head") < 3
        or release_script.count("verify_authoritative_release_controls") < 3
        or release_script.count('test "$(remote_tag_commit)" = "$SOURCE_REVISION"') < 2
        or (
            'if [[ "$(jq \'length\' <<<"$releases")" = "0" ]]; then\n'
            "  reject_newer_stable_release\n"
            '  gh release create "$RELEASE_TAG"'
        )
        not in release_transaction
        or "verify_exact_published_release" not in published_branch
        or "exit 0" not in published_branch
        or any(token in published_branch for token in published_mutations)
        or any(token in published_verifier for token in published_mutations)
        or "--draft=false" not in release_script
        or "--latest" not in release_script
        or release_script.count('release="$(fetch_release "$release_id")"') < 2
        or "repos/$GITHUB_REPOSITORY/releases/latest" not in release_script
        or 'test "$(jq -r \'.id\' <<<"$latest_release")" = "$release_id"' not in release_script
        or 'test "$(jq -r \'.immutable\' <<<"$latest_release")" = "true"' not in release_script
        or not release_script.rstrip().endswith(
            'verify_postpublication_current_latest "$release" "$release_id"'
        )
    ):
        findings.append(
            Finding(
                "release_latest_policy_invalid",
                f"{relative}:release",
                "stable publication must re-fetch exact immutable current/latest state; a "
                "published replay is read-only",
            )
        )
    promote_steps = steps_by_job.get("promote", [])
    promote_final = promote_steps[-1] if promote_steps else None
    promote_script = (
        promote_final.get("run")
        if isinstance(promote_final, dict) and isinstance(promote_final.get("run"), str)
        else ""
    )
    candidate_publish_step = next(
        (
            step
            for step in steps_by_job.get("candidate", [])
            if isinstance(step, dict)
            and step.get("name") == "Publish the new stable semantic candidate"
        ),
        None,
    )
    candidate_publish_script = (
        candidate_publish_step.get("run")
        if isinstance(candidate_publish_step, dict)
        and isinstance(candidate_publish_step.get("run"), str)
        else ""
    )
    candidate_pre_push = candidate_publish_script.split(
        'docker image tag "schemabridge-runtime:$SOURCE_REVISION"',
        maxsplit=1,
    )[0]
    candidate_boundary_step = (
        steps_by_job.get("candidate", [None])[0] if steps_by_job.get("candidate") else None
    )
    candidate_boundary_script = (
        candidate_boundary_step.get("run")
        if isinstance(candidate_boundary_step, dict)
        and isinstance(candidate_boundary_step.get("run"), str)
        else ""
    )
    if (
        "verify_public_external_state_metadata" not in prepare_policy_script
        or 'for reference in "candidate-$SOURCE_REVISION" "$RELEASE_TAG"' not in audit_script
        or "registry reference already exists before this dispatch" not in audit_script
        or 'if [[ "$release_count" = "1" ]]; then' not in audit_script
        or "releases?per_page=100" not in candidate_boundary_script
        or "[.[][] | select(.tag_name == $tag)] | length" not in candidate_boundary_script
        or "stable tag already exists; dispatch is not clean" not in candidate_pre_push
        or "adopting the exact candidate created by this run after a retry"
        not in candidate_pre_push
        or 'test "${candidate_state##*|}" = "$LOCAL_IMAGE_ID"' not in candidate_pre_push
        or "candidate_digest=" not in candidate_pre_push
        or "exit 0" not in candidate_pre_push
        or candidate_pre_push.count("exit 1") < 1
        or 'test "${candidate_state%%|*}" = "$LOCAL_IMAGE_ID"' in candidate_pre_push
    ):
        findings.append(
            Finding(
                "release_partial_dispatch_invalid",
                relative,
                "audit must reject dirty full dispatches while a same-run candidate retry may "
                "adopt only the exact prepared config",
            )
        )
    registry_absence_tokens = (
        'if [[ "$code" = "404" ]]',
        '.code == "MANIFEST_UNKNOWN" or .code == "NAME_UNKNOWN"',
        'test "$code" = "200"',
    )
    all_release_scripts = "\n".join(
        step["run"]
        for steps in steps_by_job.values()
        for step in steps
        if isinstance(step, dict) and isinstance(step.get("run"), str)
    )
    if (
        any(token not in candidate_publish_script for token in registry_absence_tokens)
        or any(token not in promote_script for token in registry_absence_tokens)
        or 'test "${candidate_state##*|}" = "$LOCAL_IMAGE_ID"' not in candidate_publish_script
        or 'test "${candidate_state##*|}" = "$IMAGE_ID"' not in promote_script
        or all_release_scripts.count("--max-filesize 4194304") != 9
        or all_release_scripts.count('test "$(stat --format=\'%s\' "$body")" -le 4194304') < 5
        or all_release_scripts.count(
            'test "$(stat --format=\'%s\' "$candidate_manifest")" -le 4194304'
        )
        < 2
    ):
        findings.append(
            Finding(
                "release_registry_state_invalid",
                relative,
                "candidate and promotion accept only structured absence or exact digest/config state",
            )
        )
    if (
        "--request PUT" not in promote_script
        or "candidate_state" not in promote_script
        or "final_state" not in promote_script
        or promote_script.count('test "$(remote_tag_commit)" = "$SOURCE_REVISION"') < 1
        or "verify_default_head" not in promote_script
        or "verify_authoritative_release_controls" not in promote_script
    ):
        findings.append(
            Finding(
                "release_promotion_order_invalid",
                f"{relative}:promote",
                "promotion must resume exact state and revalidate immediately before the sole PUT",
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
    if root / _M30_CAMPAIGN_WORKFLOW_PATH not in paths:
        findings.append(
            Finding(
                "m30_manifest_attestation_missing",
                _M30_CAMPAIGN_WORKFLOW_PATH,
                "protected M30 manifest attestation workflow is absent",
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
        findings.extend(_m30_campaign_workflow_findings(path, document, root))
        findings.extend(_supply_chain_artifact_upload_findings(path, document, root))
        findings.extend(_checkout_credentials_findings(path, document, root))
        findings.extend(_trivy_cache_findings(path, document, root))
        findings.extend(_pip_audit_resolution_findings(path, document, root))
        findings.extend(_ci_buildkit_findings(path, document, root))
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


def _logical_dockerfile_instructions(text: str) -> tuple[str, ...]:
    instructions: list[str] = []
    current: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or (not current and line.startswith("#")):
            continue
        continued = line.endswith("\\")
        current.append(line[:-1].rstrip() if continued else line)
        if not continued:
            instruction = " ".join(current)
            opcode, separator, arguments = instruction.partition(" ")
            instructions.append(
                f"{opcode.upper()}{separator}{arguments}" if separator else opcode.upper()
            )
            current = []
    if current:
        instruction = " ".join(current)
        opcode, separator, arguments = instruction.partition(" ")
        instructions.append(
            f"{opcode.upper()}{separator}{arguments}" if separator else opcode.upper()
        )
    return tuple(instructions)


def _dockerfile_stages(instructions: Sequence[str]) -> tuple[tuple[str, ...], ...]:
    stages: list[list[str]] = []
    for instruction in instructions:
        opcode = instruction.partition(" ")[0]
        if opcode == "FROM":
            stages.append([instruction])
            continue
        if not stages:
            return ()
        stages[-1].append(instruction)
    return tuple(tuple(stage) for stage in stages)


def _expected_postgres_client_apk_fetch_command() -> str:
    parts = ['RUN set -eu; case "$TARGETARCH" in']
    for architecture, packages in POSTGRES_CLIENT_APK_MATRIX.items():
        parts.append(f"{architecture})")
        for index, (variable, url, digest) in enumerate(packages):
            parts.extend(
                (
                    f"{variable}_url={url};",
                    f"{variable}_sha256={digest}{';' if index < len(packages) - 1 else ''}",
                )
            )
        parts.append(";;")
    parts.extend(
        (
            "*)",
            r"""printf 'unsupported TARGETARCH: %s\n' "$TARGETARCH" >&2;""",
            "exit 1",
            ";;",
            "esac;",
            "mkdir -p /postgres-client-apks;",
            "fetch_apk()",
            "{",
            'url="$1";',
            'expected_sha256="$2";',
            'destination="/postgres-client-apks/${url##*/}";',
            'wget -q -T 60 -O "$destination" "$url";',
            r"""printf '%s  %s\n' "$expected_sha256" "$destination" | sha256sum -c -;""",
            "};",
            'fetch_apk "$libpq_url" "$libpq_sha256";',
            'fetch_apk "$lz4_url" "$lz4_sha256";',
            'fetch_apk "$postgresql_common_url" "$postgresql_common_sha256";',
            'fetch_apk "$postgresql_client_url" "$postgresql_client_sha256";',
            'fetch_apk "$zstd_url" "$zstd_sha256"',
        )
    )
    return " ".join(parts)


def _expected_postgres_client_apk_install_command() -> str:
    return (
        "RUN --mount=from=postgres-client-apks,source=/postgres-client-apks,"
        "target=/postgres-client-apks,ro apk add --no-cache --no-network "
        "/postgres-client-apks/libpq-18.4-r0.apk "
        "/postgres-client-apks/lz4-libs-1.10.0-r1.apk "
        "/postgres-client-apks/postgresql-common-1.3-r0.apk "
        "/postgres-client-apks/postgresql16-client-16.14-r0.apk "
        "/postgres-client-apks/zstd-libs-1.5.7-r2.apk"
    )


def _expected_runtime_builder_stage() -> tuple[str, ...]:
    return (
        f"FROM {RUNTIME_BASE_IMAGE} AS builder",
        "ARG SCHEMABRIDGE_RELEASE_REF=release-ref-not-supplied",
        "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 "
        "PIP_DISABLE_PIP_VERSION_CHECK=1 SOURCE_DATE_EPOCH=1730470033",
        "WORKDIR /opt/schemabridge",
        "COPY requirements/build.txt requirements/watchdog-build.txt "
        "requirements/runtime.txt ./requirements/",
        "RUN python -m pip install --no-cache-dir --no-deps --require-hashes "
        "-r requirements/build.txt -r requirements/watchdog-build.txt",
        "RUN python -m pip wheel --no-cache-dir --no-deps --no-build-isolation "
        "--require-hashes --wheel-dir /tmp/runtime-wheels -r requirements/runtime.txt",
        "COPY pyproject.toml README.md LICENSE ./",
        "COPY src ./src",
        "COPY migrations ./migrations",
        "RUN python -m pip wheel --no-cache-dir --no-deps --no-build-isolation "
        "--wheel-dir /tmp/dist .",
    )


def _expected_runtime_final_stage() -> tuple[str, ...]:
    return (
        f"FROM {RUNTIME_BASE_IMAGE}",
        "ARG SCHEMABRIDGE_RELEASE_REF=release-ref-not-supplied",
        'LABEL org.opencontainers.image.source="https://github.com/Crespillo95/'
        'schemabridge-codex-starter" '
        'org.opencontainers.image.revision="${SCHEMABRIDGE_RELEASE_REF}"',
        "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 "
        "PIP_DISABLE_PIP_VERSION_CHECK=1 "
        "SCHEMABRIDGE_RELEASE_REF=${SCHEMABRIDGE_RELEASE_REF}",
        _expected_postgres_client_apk_install_command(),
        "RUN adduser -D -u 10001 schemabridge",
        "WORKDIR /opt/schemabridge",
        "COPY requirements/runtime.txt ./requirements/runtime.txt",
        "COPY requirements/runtime-built.txt ./requirements/runtime-built.txt",
        "RUN --mount=from=builder,source=/tmp/runtime-wheels,target=/tmp/runtime-wheels,ro "
        "python -m pip install --no-cache-dir --no-index --no-deps --require-hashes "
        "-r requirements/runtime-built.txt && python -m pip install --no-cache-dir --no-index "
        "--no-deps --require-hashes --find-links /tmp/runtime-wheels "
        "-r requirements/runtime.txt",
        "COPY --from=builder /opt/schemabridge/src/schemabridge/entrypoints/streamlit/app.py "
        "./streamlit_app.py",
        "RUN --mount=from=builder,source=/tmp/dist,target=/tmp/dist,ro "
        "python -m pip install --no-cache-dir --no-deps "
        "/tmp/dist/schemabridge-0.1.0-py3-none-any.whl",
        "RUN chown -R schemabridge:schemabridge /opt/schemabridge",
        "USER 10001:10001",
        "EXPOSE 7860 8520",
        'CMD ["schemabridge-api"]',
    )


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
    runtime_images = tuple(
        match.group(1) for line in text.splitlines() if (match := _FROM.match(line)) is not None
    )
    if runtime_images != (RUNTIME_BASE_IMAGE, RUNTIME_BASE_IMAGE, RUNTIME_BASE_IMAGE):
        findings.append(
            Finding(
                "runtime_base_image_unreviewed",
                runtime.name,
                "the build, APK-fetch, and runtime stages must use the exact reviewed image",
            )
        )
    watchdog_build_requirement = root / WATCHDOG_BUILD_REQUIREMENTS
    if (
        not watchdog_build_requirement.is_file()
        or watchdog_build_requirement.read_text(encoding="utf-8") != WATCHDOG_BUILD_REQUIREMENT
    ):
        findings.append(
            Finding(
                "runtime_build_requirement_invalid",
                str(WATCHDOG_BUILD_REQUIREMENTS),
                "the isolated watchdog build backend must be exact and hash-bound",
            )
        )
    built_requirement = root / BUILT_RUNTIME_REQUIREMENTS
    if (
        not built_requirement.is_file()
        or built_requirement.read_text(encoding="utf-8") != WATCHDOG_BUILT_REQUIREMENT
    ):
        findings.append(
            Finding(
                "runtime_built_requirement_invalid",
                str(BUILT_RUNTIME_REQUIREMENTS),
                "the reproducible watchdog wheel must be bound to its exact local SHA-256",
            )
        )
    required_fragments = (
        "requirements/build.txt",
        "requirements/watchdog-build.txt",
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
    offline_fragments = (
        "pip wheel --no-cache-dir --no-deps --no-build-isolation",
        "--wheel-dir /tmp/runtime-wheels",
        "python -m pip install --no-cache-dir --no-index --no-deps --require-hashes \\\n"
        "    -r requirements/runtime-built.txt",
        "&& python -m pip install --no-cache-dir --no-index --no-deps --require-hashes \\",
        "--find-links /tmp/runtime-wheels",
    )
    for fragment in offline_fragments:
        if fragment not in text:
            findings.append(
                Finding(
                    "runtime_install_not_offline",
                    runtime.name,
                    f"required verified-wheelhouse fragment is absent: {fragment}",
                )
            )
    reproducible_fragments = (
        f"SOURCE_DATE_EPOCH={WATCHDOG_SOURCE_DATE_EPOCH}",
        "RUN --mount=from=builder,source=/tmp/runtime-wheels,target=/tmp/runtime-wheels,ro \\",
        "RUN --mount=from=builder,source=/tmp/dist,target=/tmp/dist,ro \\",
    )
    for fragment in reproducible_fragments:
        if fragment not in text:
            findings.append(
                Finding(
                    "runtime_install_not_reproducible",
                    runtime.name,
                    f"required reproducible-build fragment is absent: {fragment}",
                )
            )
    runtime_instructions = _logical_dockerfile_instructions(text)
    runtime_stages = _dockerfile_stages(runtime_instructions)
    expected_postgres_client_stage = (
        f"FROM {RUNTIME_BASE_IMAGE} AS postgres-client-apks",
        "ARG TARGETARCH",
        _expected_postgres_client_apk_fetch_command(),
    )
    expected_runtime_stages = (
        _expected_runtime_builder_stage(),
        expected_postgres_client_stage,
        _expected_runtime_final_stage(),
    )
    if runtime_stages != expected_runtime_stages or any(
        instruction.partition(" ")[0] == "ADD" for instruction in runtime_instructions
    ):
        findings.append(
            Finding(
                "runtime_commands_unreviewed",
                runtime.name,
                "the three runtime stages contain a missing, changed, or additional instruction",
            )
        )
    postgres_client_instructions = runtime_stages[1] if len(runtime_stages) == 3 else ()
    runtime_run_instructions = (
        tuple(
            instruction
            for instruction in runtime_stages[2]
            if instruction.partition(" ")[0] == "RUN"
        )
        if len(runtime_stages) == 3
        else ()
    )
    if (
        postgres_client_instructions != expected_postgres_client_stage
        or runtime_run_instructions.count(_expected_postgres_client_apk_install_command()) != 1
        or "COPY --from=postgres-client-apks" in text
        or text.count("apk add") != 1
    ):
        findings.append(
            Finding(
                "runtime_postgres_client_unreviewed",
                runtime.name,
                "PostgreSQL client APK URLs, architectures, hashes, and offline mount must be exact",
            )
        )
    if "COPY --from=builder /tmp/runtime-wheels" in text:
        findings.append(
            Finding(
                "runtime_wheelhouse_layer_retained",
                runtime.name,
                "the dependency wheelhouse must be mounted read-only, not retained in an OCI layer",
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
    for relative in (
        RUNTIME_REQUIREMENTS,
        BUILD_REQUIREMENTS,
        WATCHDOG_BUILD_REQUIREMENTS,
    ):
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
    audited_inputs = (
        RUNTIME_REQUIREMENTS,
        BUILD_REQUIREMENTS,
        WATCHDOG_BUILD_REQUIREMENTS,
    )
    for relative in audited_inputs:
        if not (root / relative).is_file():
            continue
        for requirement in load_requirements(root, relative):
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
                "pip-audit dependency set does not match the applicable frozen inputs",
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


def _expected_linux_python_components(root: Path) -> frozenset[tuple[str, str]]:
    requirements = load_requirements(root, RUNTIME_REQUIREMENTS)
    expected = {
        (item.normalized_name, item.version)
        for item in requirements
        if not (item.marker is not None and "sys_platform == 'win32'" in item.marker)
    }
    expected.update(RUNTIME_PYTHON_BASE_COMPONENTS)
    return frozenset(expected)


def _postgres_client_apk_bindings(
    apk_architecture: str,
) -> Mapping[tuple[str, str], tuple[str, str]]:
    docker_architecture = _APK_ARCHITECTURE_TO_DOCKER.get(apk_architecture)
    if docker_architecture is None:
        return {}
    return {
        _POSTGRES_CLIENT_COMPONENT_BY_VARIABLE[variable]: (url, digest)
        for variable, url, digest in POSTGRES_CLIENT_APK_MATRIX[docker_architecture]
    }


def _runtime_apk_architecture(payload: Mapping[str, Any]) -> str:
    components = payload.get("components")
    architectures: set[str] = set()
    if isinstance(components, list):
        for item in components:
            if not isinstance(item, dict) or not isinstance(item.get("purl"), str):
                continue
            match = _APK_PURL.fullmatch(item["purl"])
            if match is not None and match.group("architecture") != "noarch":
                architectures.add(match.group("architecture"))
    if len(architectures) != 1:
        raise SupplyChainViolation(
            (
                Finding(
                    "sbom_apk_architecture_invalid",
                    "runtime-image.cdx.json",
                    "expected one coherent aarch64 or x86_64 APK architecture",
                ),
            )
        )
    return next(iter(architectures))


def _valid_component_package_hash(item: Mapping[str, Any]) -> bool:
    hashes = item.get("hashes")
    if not isinstance(hashes, list) or not hashes:
        return False
    expected_lengths = {"SHA-1": 40, "SHA-256": 64}
    for value in hashes:
        if not isinstance(value, dict):
            continue
        algorithm = value.get("alg")
        content = value.get("content")
        if (
            isinstance(algorithm, str)
            and algorithm in expected_lengths
            and isinstance(content, str)
            and re.fullmatch(
                rf"[0-9a-f]{{{expected_lengths[algorithm]}}}",
                content,
            )
            is not None
        ):
            return True
    return False


def _root_reachable_component_refs(payload: Mapping[str, Any]) -> frozenset[str]:
    metadata = payload.get("metadata")
    component = metadata.get("component") if isinstance(metadata, dict) else None
    root_ref = component.get("bom-ref") if isinstance(component, dict) else None
    dependencies = payload.get("dependencies")
    if not isinstance(root_ref, str) or not root_ref or not isinstance(dependencies, list):
        return frozenset()
    adjacency: dict[str, tuple[str, ...]] = {}
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            return frozenset()
        reference = dependency.get("ref")
        depends_on = dependency.get("dependsOn")
        if (
            not isinstance(reference, str)
            or not reference
            or reference in adjacency
            or not isinstance(depends_on, list)
            or not all(isinstance(item, str) and item for item in depends_on)
        ):
            return frozenset()
        adjacency[reference] = tuple(depends_on)
    reachable: set[str] = set()
    pending = [root_ref]
    while pending:
        reference = pending.pop()
        if reference in reachable:
            continue
        reachable.add(reference)
        pending.extend(adjacency.get(reference, ()))
    return frozenset(reachable)


def _verify_linux_runtime_components(
    payload: Mapping[str, Any],
    *,
    root: Path,
) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    components = payload.get("components")
    if not isinstance(components, list):
        return (
            Finding(
                "sbom_component_mismatch",
                "sbom",
                "runtime components must be a list",
            ),
        )

    python_components: set[tuple[str, str]] = set()
    apk_components: dict[tuple[str, str], Mapping[str, Any]] = {}
    apk_component_architectures: dict[tuple[str, str], str] = {}
    apk_refs: set[str] = set()
    operating_system_refs: set[str] = set()
    for index, item in enumerate(components, start=1):
        if not isinstance(item, dict):
            findings.append(
                Finding(
                    "sbom_component_metadata_invalid",
                    f"sbom:component:{index}",
                    "runtime package components must be CycloneDX objects",
                )
            )
            continue
        purl = item.get("purl")
        if not isinstance(purl, str):
            operating_system_ref = item.get("bom-ref")
            if (
                item.get("type") == "operating-system"
                and item.get("name") == "alpine"
                and item.get("version") == "3.24.1"
                and isinstance(operating_system_ref, str)
                and operating_system_ref
                and not operating_system_refs
            ):
                operating_system_refs.add(operating_system_ref)
                continue
            findings.append(
                Finding(
                    "sbom_component_metadata_invalid",
                    f"sbom:component:{index}",
                    "only the Alpine 3.24.1 operating-system component may omit a purl",
                )
            )
            continue
        if purl.startswith("pkg:pypi/"):
            match = _PYPI_PURL.fullmatch(purl)
            identity = (
                normalize_name(str(item.get("name"))),
                str(item.get("version")),
            )
            if (
                match is None
                or item.get("type") != "library"
                or normalize_name(match.group("name")) != identity[0]
                or match.group("version") != identity[1]
                or item.get("bom-ref") != purl
                or identity in python_components
            ):
                findings.append(
                    Finding(
                        "sbom_component_metadata_invalid",
                        f"sbom:component:{index}",
                        "Python component purl, bom-ref, name, or version is invalid",
                    )
                )
                continue
            python_components.add(identity)
            continue
        if not purl.startswith("pkg:apk/"):
            findings.append(
                Finding(
                    "sbom_component_metadata_invalid",
                    f"sbom:component:{index}",
                    "runtime library purl must identify a reviewed PyPI or Alpine package",
                )
            )
            continue
        match = _APK_PURL.fullmatch(purl)
        if match is None:
            findings.append(
                Finding(
                    "sbom_component_metadata_invalid",
                    f"sbom:component:{index}",
                    "APK purl must bind Alpine 3.24.1 and one reviewed architecture",
                )
            )
            continue
        identity = (match.group("name"), match.group("version"))
        if (
            item.get("type") != "library"
            or item.get("name") != identity[0]
            or str(item.get("version")) != identity[1]
            or item.get("bom-ref") != purl
            or identity in apk_components
        ):
            findings.append(
                Finding(
                    "sbom_component_metadata_invalid",
                    f"sbom:component:{index}",
                    "APK purl, bom-ref, name, version, or uniqueness is invalid",
                )
            )
            continue
        if not _valid_component_package_hash(item):
            findings.append(
                Finding(
                    "sbom_component_hash_missing",
                    f"sbom:component:{index}",
                    "APK component needs a valid SHA-1 or SHA-256 package hash",
                )
            )
        apk_components[identity] = item
        apk_component_architectures[identity] = match.group("architecture")
        apk_refs.add(purl)

    expected_python = _expected_linux_python_components(root)
    if python_components != expected_python:
        findings.append(
            Finding(
                "sbom_component_mismatch",
                "sbom",
                f"python missing={sorted(expected_python - python_components)!r} "
                f"extra={sorted(python_components - expected_python)!r}",
            )
        )
    selected_architectures = {
        architecture
        for architecture in apk_component_architectures.values()
        if architecture != "noarch"
    }
    selected_architecture = (
        next(iter(selected_architectures)) if len(selected_architectures) == 1 else None
    )
    if selected_architecture not in _APK_ARCHITECTURE_TO_DOCKER:
        findings.append(
            Finding(
                "sbom_apk_architecture_invalid",
                "sbom",
                "expected one coherent aarch64 or x86_64 APK architecture",
            )
        )
    else:
        expected_apk = RUNTIME_ALPINE_COMPONENTS | {
            RUNTIME_ALPINE_PLATFORM_VIRTUAL_COMPONENT[selected_architecture]
        }
        observed_apk = set(apk_components)
        if observed_apk != expected_apk:
            findings.append(
                Finding(
                    "sbom_component_mismatch",
                    "sbom",
                    f"apk missing={sorted(expected_apk - observed_apk)!r} "
                    f"extra={sorted(observed_apk - expected_apk)!r}",
                )
            )
        for identity, architecture in apk_component_architectures.items():
            expected_architecture = (
                "noarch" if identity in RUNTIME_ALPINE_NOARCH_COMPONENTS else selected_architecture
            )
            if architecture != expected_architecture:
                findings.append(
                    Finding(
                        "sbom_apk_architecture_invalid",
                        f"sbom:{identity[0]}",
                        f"expected {expected_architecture}, observed {architecture}",
                    )
                )
        for identity, (expected_url, expected_digest) in _postgres_client_apk_bindings(
            selected_architecture
        ).items():
            item = apk_components.get(identity)
            properties = item.get("properties") if isinstance(item, Mapping) else None
            values: dict[str, list[object]] = {}
            if isinstance(properties, list):
                for value in properties:
                    if isinstance(value, dict) and isinstance(value.get("name"), str):
                        values.setdefault(value["name"], []).append(value.get("value"))
            if values.get(_APK_SOURCE_URL_PROPERTY) != [expected_url] or values.get(
                _APK_SOURCE_SHA256_PROPERTY
            ) != [expected_digest]:
                findings.append(
                    Finding(
                        "sbom_apk_binding_mismatch",
                        f"sbom:{identity[0]}",
                        "APK URL and architecture-specific SHA-256 properties are incomplete",
                    )
                )

    reachable_refs = _root_reachable_component_refs(payload)
    if not apk_refs or not apk_refs <= reachable_refs:
        findings.append(
            Finding(
                "sbom_dependency_graph_invalid",
                "sbom",
                f"unreachable APK refs={sorted(apk_refs - reachable_refs)!r}",
            )
        )
    return tuple(findings)


def verify_cyclonedx_sbom(
    payload: Mapping[str, Any],
    *,
    root: Path,
    artifact_digest: str,
    source_revision: str,
    linux_runtime: bool = False,
) -> None:
    findings: list[Finding] = []
    spec_version = payload.get("specVersion")
    if (
        payload.get("bomFormat") != "CycloneDX"
        or not isinstance(spec_version, str)
        or spec_version not in SUPPORTED_CYCLONEDX_SPEC_VERSIONS
    ):
        findings.append(
            Finding(
                "sbom_schema_invalid",
                "sbom",
                "expected CycloneDX 1.5 or 1.6",
            )
        )
    if linux_runtime:
        findings.extend(_verify_linux_runtime_components(payload, root=root))
    else:
        requirements = load_requirements(root, RUNTIME_REQUIREMENTS)
        expected = {(item.normalized_name, item.version) for item in requirements}
        components = payload.get("components")
        observed: set[tuple[str, str]] = set()
        if isinstance(components, list):
            for item in components:
                if isinstance(item, dict):
                    observed.add((normalize_name(str(item.get("name"))), str(item.get("version"))))
        component_mismatch = expected ^ observed
        if component_mismatch:
            findings.append(
                Finding(
                    "sbom_component_mismatch",
                    "sbom",
                    f"missing={sorted(expected - observed)!r} "
                    f"extra={sorted(observed - expected)!r}",
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
    apk_architecture = _runtime_apk_architecture(bound)
    apk_bindings = _postgres_client_apk_bindings(apk_architecture)
    components = bound.get("components")
    if not isinstance(components, list):
        raise SupplyChainViolation(
            (
                Finding(
                    "sbom_component_mismatch",
                    "runtime-image.cdx.json",
                    "runtime components must be a list",
                ),
            )
        )
    bound_identities: set[tuple[str, str]] = set()
    for item in components:
        if not isinstance(item, dict) or not isinstance(item.get("purl"), str):
            continue
        match = _APK_PURL.fullmatch(item["purl"])
        if match is None or match.group("architecture") != apk_architecture:
            continue
        identity = (match.group("name"), match.group("version"))
        binding = apk_bindings.get(identity)
        if binding is None:
            continue
        properties = item.get("properties")
        retained_apk_properties = (
            [
                value
                for value in properties
                if isinstance(value, dict)
                and value.get("name") not in {_APK_SOURCE_URL_PROPERTY, _APK_SOURCE_SHA256_PROPERTY}
            ]
            if isinstance(properties, list)
            else []
        )
        expected_url, expected_digest = binding
        retained_apk_properties.extend(
            (
                {"name": _APK_SOURCE_URL_PROPERTY, "value": expected_url},
                {"name": _APK_SOURCE_SHA256_PROPERTY, "value": expected_digest},
            )
        )
        item["properties"] = retained_apk_properties
        bound_identities.add(identity)
    if bound_identities != POSTGRES_CLIENT_ALPINE_COMPONENTS:
        raise SupplyChainViolation(
            (
                Finding(
                    "sbom_apk_binding_mismatch",
                    "runtime-image.cdx.json",
                    "the five reviewed PostgreSQL client APK components are incomplete",
                ),
            )
        )
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
