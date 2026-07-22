"""Environment diagnostic use case."""

from __future__ import annotations

import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

MINIMUM_PYTHON = (3, 11)
MAXIMUM_PYTHON = (3, 14)


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One diagnostic check."""

    name: str
    ok: bool
    detail: str
    required: bool = True


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Complete diagnostic report."""

    checks: tuple[CheckResult, ...]

    @property
    def is_healthy(self) -> bool:
        """Return whether all required checks passed."""

        return all(check.ok or not check.required for check in self.checks)

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation."""

        return {
            "healthy": self.is_healthy,
            "checks": [
                {
                    "name": check.name,
                    "ok": check.ok,
                    "detail": check.detail,
                    "required": check.required,
                }
                for check in self.checks
            ],
        }


def supports_python_version(version: tuple[int, int]) -> bool:
    """Return whether a Python major/minor pair is supported by the package."""

    return MINIMUM_PYTHON <= version < MAXIMUM_PYTHON


def run_doctor(project_root: Path | None = None) -> DoctorReport:
    """Check the minimum local environment without contacting external services."""

    root = project_root or Path.cwd()
    required_files = (
        "AGENTS.md",
        "pyproject.toml",
        "plans/MASTER_PLAN.md",
        "tasks/PROJECT_STATE.md",
    )
    files_ok = all((root / relative_path).is_file() for relative_path in required_files)
    missing = [
        relative_path for relative_path in required_files if not (root / relative_path).is_file()
    ]

    python_version = (sys.version_info.major, sys.version_info.minor)
    python_ok = supports_python_version(python_version)
    docker_path = shutil.which("docker")
    git_path = shutil.which("git")
    git_repository_ok = (root / ".git").exists()

    python_detail = f"{platform.python_implementation()} {platform.python_version()}"
    if not python_ok:
        python_detail = f"{python_detail}; requires >=3.11,<3.14"

    checks = (
        CheckResult(
            name="python",
            ok=python_ok,
            detail=python_detail,
        ),
        CheckResult(
            name="project-files",
            ok=files_ok,
            detail="all required files exist" if files_ok else f"missing: {', '.join(missing)}",
        ),
        CheckResult(
            name="git",
            ok=git_path is not None,
            detail=git_path or "git not found on PATH",
        ),
        CheckResult(
            name="git-repository",
            ok=git_repository_ok,
            detail="Git metadata found"
            if git_repository_ok
            else "run git init from the project root",
        ),
        CheckResult(
            name="docker",
            ok=docker_path is not None,
            detail=docker_path or "docker not found on PATH; required from M01",
            required=False,
        ),
    )
    return DoctorReport(checks=checks)
