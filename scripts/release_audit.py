"""Fail-closed release scans for architecture, secrets, links, and licenses."""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path
from typing import cast
from urllib.parse import unquote

_DEPENDENCY_NAME = re.compile(r"^[A-Za-z0-9_.-]+")
_MARKDOWN_LINK = re.compile(r"!?\[[^]]*\]\(([^)]+)\)")
_EXTERNAL_LINK = re.compile(r"https://[^\s`)<>\"]+")
_SECRET_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("gitlab_token", re.compile(r"\bglpat-[0-9A-Za-z_-]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{20,}\b")),
    ("stripe_live_key", re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{20,}\b")),
)
_FORBIDDEN_CREDENTIAL_SUFFIXES = frozenset({".jks", ".key", ".keystore", ".p12", ".pem", ".pfx"})
_PARALLEL_COVERAGE_PREFIX = ".coverage."
_FORBIDDEN_ARTIFACT_PARTS = frozenset(
    {
        ".coverage",
        ".env",
        ".local",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "artifacts",
        "datahub-quickstart",
        "htmlcov",
    }
)


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    severity: str
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class DependencyLicense:
    package: str
    version: str
    license: str


def candidate_files(root: Path) -> tuple[Path, ...]:
    """Return tracked and unignored untracked files that could enter a commit."""

    result = subprocess.run(
        ("git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"),
        cwd=root,
        check=True,
        capture_output=True,
    )
    return tuple(root / raw.decode("utf-8") for raw in result.stdout.split(b"\0") if raw)


def scan_architecture(root: Path) -> tuple[Finding, ...]:
    """Inspect every production import against the ports-and-adapters boundary matrix."""

    findings: list[Finding] = []
    source_root = root / "src/schemabridge"
    for path in sorted(source_root.rglob("*.py")):
        relative = path.relative_to(root)
        layer = path.relative_to(source_root).parts[0]
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))
        except (OSError, SyntaxError, UnicodeError) as error:
            findings.append(Finding("python_parse_error", "error", str(relative), str(error)))
            continue
        for module, line in _imports(tree):
            reason = _forbidden_import(layer, module)
            if reason is not None:
                findings.append(
                    Finding(
                        "dependency_direction",
                        "error",
                        f"{relative}:{line}",
                        f"{module}: {reason}",
                    )
                )
    return tuple(findings)


def scan_candidate_artifacts(root: Path, files: tuple[Path, ...]) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for path in files:
        relative = path.relative_to(root)
        parts = set(relative.parts)
        has_parallel_coverage_artifact = any(
            part.startswith(_PARALLEL_COVERAGE_PREFIX)
            and len(part) > len(_PARALLEL_COVERAGE_PREFIX)
            for part in parts
        )
        if path.name != ".env.example" and (
            parts & _FORBIDDEN_ARTIFACT_PARTS or has_parallel_coverage_artifact
        ):
            findings.append(
                Finding(
                    "runtime_artifact",
                    "error",
                    str(relative),
                    "runtime, cache, environment, or generated artifact is commit-visible",
                )
            )
        if (
            path.name in {".DS_Store", "Thumbs.db", "secrets.toml"}
            or path.suffix.casefold() in _FORBIDDEN_CREDENTIAL_SUFFIXES
        ):
            findings.append(
                Finding("secret_or_os_artifact", "error", str(relative), "forbidden file type")
            )
    return tuple(findings)


def scan_secrets(root: Path, files: tuple[Path, ...]) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for path in files:
        text = _read_text(path)
        if text is None:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for code, pattern in _SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append(
                        Finding(
                            code,
                            "error",
                            f"{path.relative_to(root)}:{line_number}",
                            "high-confidence secret pattern detected",
                        )
                    )
    return tuple(findings)


def scan_markdown_links(root: Path, files: tuple[Path, ...]) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for path in files:
        if path.suffix.casefold() != ".md":
            continue
        text = _read_text(path)
        if text is None:
            continue
        for match in _MARKDOWN_LINK.finditer(text):
            target = match.group(1).strip().split(maxsplit=1)[0].strip("<>")
            target = unquote(target.split("#", maxsplit=1)[0])
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            resolved = (path.parent / target).resolve()
            if not resolved.is_relative_to(root.resolve()) or not resolved.exists():
                line_number = text.count("\n", 0, match.start()) + 1
                findings.append(
                    Finding(
                        "broken_local_link",
                        "error",
                        f"{path.relative_to(root)}:{line_number}",
                        f"local target does not exist: {target}",
                    )
                )
    return tuple(findings)


def scan_external_links(root: Path, files: tuple[Path, ...]) -> tuple[int, tuple[Finding, ...]]:
    """Verify every unique HTTPS reference with curl redirects and bounded timeouts."""

    locations: dict[str, str] = {}
    for path in files:
        if path.suffix.casefold() not in {".env", ".md", ".toml", ".yaml", ".yml"}:
            continue
        text = _read_text(path)
        if text is None:
            continue
        for match in _EXTERNAL_LINK.finditer(text):
            url = match.group(0).rstrip(".,;:")
            locations.setdefault(url, str(path.relative_to(root)))
    findings: list[Finding] = []
    for url, source_path in sorted(locations.items()):
        result = subprocess.run(
            ("curl", "-fsSIL", "--max-time", "30", "--retry", "1", url),
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            findings.append(
                Finding(
                    "broken_external_link",
                    "error",
                    source_path,
                    f"HTTPS reference did not return successfully: {url}",
                )
            )
    return len(locations), tuple(findings)


def scan_disclosure(root: Path) -> tuple[Finding, ...]:
    path = root / "HACKATHON_DISCLOSURE.md"
    if not path.is_file():
        return (Finding("missing_disclosure", "error", str(path.name), "disclosure is absent"),)
    text = path.read_text(encoding="utf-8").casefold()
    stale = ("before submission", "development is intended to continue")
    return tuple(
        Finding("unfinished_disclosure", "error", path.name, f"stale phrase remains: {phrase}")
        for phrase in stale
        if phrase in text
    )


def scan_project_license(root: Path) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    license_path = root / "LICENSE"
    if not license_path.is_file() or "Apache License" not in license_path.read_text(
        encoding="utf-8"
    ):
        findings.append(
            Finding("missing_project_license", "error", "LICENSE", "Apache-2.0 text is absent")
        )
    with (root / "pyproject.toml").open("rb") as stream:
        pyproject = tomllib.load(stream)
    if pyproject["project"].get("license", {}).get("text") != "Apache-2.0":
        findings.append(
            Finding(
                "license_metadata_mismatch",
                "error",
                "pyproject.toml",
                "project license metadata is not Apache-2.0",
            )
        )
    return tuple(findings)


def dependency_licenses(root: Path) -> tuple[tuple[DependencyLicense, ...], tuple[Finding, ...]]:
    """Inventory every direct dependency and require installed license metadata."""

    with (root / "pyproject.toml").open("rb") as stream:
        pyproject = tomllib.load(stream)
    specs = list(pyproject["project"].get("dependencies", ()))
    for group in pyproject["project"].get("optional-dependencies", {}).values():
        specs.extend(group)
    packages: list[DependencyLicense] = []
    findings: list[Finding] = []
    for spec in specs:
        match = _DEPENDENCY_NAME.match(spec)
        if match is None:
            findings.append(
                Finding("invalid_dependency", "error", "pyproject.toml", f"cannot parse: {spec}")
            )
            continue
        name = match.group(0)
        try:
            package_metadata = metadata.metadata(name)
            version = metadata.version(name)
        except metadata.PackageNotFoundError:
            findings.append(
                Finding(
                    "dependency_not_installed",
                    "error",
                    "pyproject.toml",
                    f"release dependency is not installed: {name}",
                )
            )
            continue
        classifiers = package_metadata.get_all("Classifier") or []
        license_classifiers = [value for value in classifiers if value.startswith("License ::")]
        package_metadata_mapping = cast(Mapping[str, str], package_metadata)
        license_value = (
            package_metadata_mapping.get("License-Expression")
            or ", ".join(license_classifiers)
            or package_metadata_mapping.get("License")
        )
        if not license_value or license_value.casefold() == "unknown":
            findings.append(
                Finding(
                    "dependency_license_unknown",
                    "error",
                    "pyproject.toml",
                    f"installed dependency has no license metadata: {name}=={version}",
                )
            )
            continue
        packages.append(DependencyLicense(name, version, " ".join(license_value.split())))
    unique = {item.package.casefold(): item for item in packages}
    return tuple(sorted(unique.values(), key=lambda item: item.package.casefold())), tuple(findings)


def check_release_baseline(root: Path, *, required: bool) -> tuple[Finding, ...]:
    revision = subprocess.run(
        ("git", "rev-parse", "--verify", "HEAD"),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    status = subprocess.run(
        ("git", "status", "--porcelain", "--untracked-files=normal"),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    severity = "error" if required else "warning"
    findings: list[Finding] = []
    if revision.returncode != 0:
        findings.append(
            Finding(
                "release_commit_missing",
                severity,
                ".git",
                "HEAD does not exist; this cannot be release-commit evidence",
            )
        )
    if status.stdout.strip():
        findings.append(
            Finding(
                "release_tree_dirty",
                severity,
                ".git",
                "working tree is not clean; this cannot be clean-room release evidence",
            )
        )
    return tuple(findings)


def _imports(tree: ast.AST) -> tuple[tuple[str, int], ...]:
    imports: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append((node.module, node.lineno))
    return tuple(imports)


def _forbidden_import(layer: str, module: str) -> str | None:
    if layer == "domain":
        if module == "schemabridge" or (
            module.startswith("schemabridge.") and not module.startswith("schemabridge.domain")
        ):
            return "domain may import only its own package layer"
        if module.split(".", maxsplit=1)[0] in {
            "datahub",
            "mcp",
            "openai",
            "psycopg",
            "pydantic_settings",
            "sqlalchemy",
            "sqlglot",
            "streamlit",
        }:
            return "domain cannot depend on external-system or presentation libraries"
    if layer == "application" and module.startswith(
        ("schemabridge.adapters", "schemabridge.bootstrap", "schemabridge.entrypoints")
    ):
        return "application cannot depend on adapters, bootstrap, or entrypoints"
    if layer == "entrypoints" and module.startswith(
        ("schemabridge.adapters", "schemabridge.config")
    ):
        return "entrypoints must obtain configured dependencies from bootstrap"
    return None


def _read_text(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\0" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--require-release", action="store_true")
    parser.add_argument("--check-external", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()
    root = args.root.resolve()
    files = candidate_files(root)
    licenses, license_findings = dependency_licenses(root)
    external_count, external_findings = (
        scan_external_links(root, files) if args.check_external else (0, ())
    )
    findings = (
        *check_release_baseline(root, required=args.require_release),
        *scan_architecture(root),
        *scan_candidate_artifacts(root, files),
        *scan_secrets(root, files),
        *scan_markdown_links(root, files),
        *external_findings,
        *scan_disclosure(root),
        *scan_project_license(root),
        *license_findings,
    )
    payload = {
        "ok": not any(item.severity == "error" for item in findings),
        "candidate_files": len(files),
        "dependency_licenses": [asdict(item) for item in licenses],
        "external_links_checked": external_count,
        "findings": [asdict(item) for item in findings],
    }
    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"Release scan {'PASS' if payload['ok'] else 'FAIL'}: "
            f"{len(files)} candidate files, {len(licenses)} direct dependency licenses, "
            f"{external_count} external links checked."
        )
        for finding in findings:
            print(f"{finding.severity.upper()} {finding.code} {finding.path}: {finding.message}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
