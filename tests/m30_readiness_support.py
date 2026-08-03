"""Synthetic Git candidate builder shared by M30 tests."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]

_FILES = (
    ".gitignore",
    ".github/workflows/ci.yml",
    ".github/workflows/release-evidence.yml",
    "Dockerfile.runtime",
    "docs/06_SECURITY.md",
    "docs/19_COMMERCIAL_USAGE.md",
    "plans/M30_CAMPAIGN_CONTRACT.yml",
    "plans/M30_PRODUCTION_EVALUATION_SECURITY.md",
    "pyproject.toml",
    "requirements/build.txt",
    "requirements/runtime-built.txt",
    "requirements/runtime.txt",
    "requirements/vulnerability-exceptions.json",
    "requirements/watchdog-build.txt",
    "uv.lock",
)


def build_candidate_repository(
    destination: Path,
    *,
    branch: str = "main",
    annotated_tag: str | None = "v0.1.0",
    include_local_claim: bool = False,
    package_version: str | None = None,
) -> Path:
    for relative in _FILES:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    shutil.copytree(ROOT / "migrations/control_plane", destination / "migrations/control_plane")
    if package_version is not None:
        pyproject = destination / "pyproject.toml"
        pyproject.write_text(
            pyproject.read_text(encoding="utf-8").replace(
                'version = "0.1.0"', f'version = "{package_version}"'
            ),
            encoding="utf-8",
        )
    if include_local_claim:
        claim = destination / "reports/self-asserted-operated-evidence.json"
        claim.parent.mkdir(parents=True, exist_ok=True)
        claim.write_text('{"status":"passed","subject":"merge-ref"}\n', encoding="utf-8")
    _run(destination, "git", "init", "-b", branch)
    _run(destination, "git", "config", "user.name", "SchemaBridge Tests")
    _run(destination, "git", "config", "user.email", "tests@example.invalid")
    _run(destination, "git", "add", ".")
    _run(destination, "git", "commit", "-m", "synthetic M30 candidate")
    if annotated_tag is not None:
        _run(destination, "git", "tag", "-a", annotated_tag, "-m", "synthetic candidate tag")
    return destination


def _run(root: Path, *command: str) -> None:
    subprocess.run(command, cwd=root, check=True, capture_output=True)
