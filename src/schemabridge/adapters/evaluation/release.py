"""Release-source identity for reproducible evaluation artifacts."""

from __future__ import annotations

import subprocess
from pathlib import Path

from schemabridge import __version__
from schemabridge.application.ports.evaluation import EvaluationError, EvaluationErrorCode
from schemabridge.domain.evaluation import EvaluationReleaseIdentity, fingerprint_fixture


class GitEvaluationReleaseIdentity:
    """Report a commit when available, otherwise label the uncommitted working tree."""

    def __init__(self, repository_root: Path) -> None:
        self._root = repository_root.resolve()

    def inspect(self) -> EvaluationReleaseIdentity:
        try:
            revision_result = subprocess.run(
                ("git", "rev-parse", "--verify", "HEAD"),
                cwd=self._root,
                check=False,
                capture_output=True,
                text=True,
            )
            status_result = subprocess.run(
                ("git", "status", "--porcelain", "--untracked-files=normal"),
                cwd=self._root,
                check=True,
                capture_output=True,
                text=True,
            )
            revision = (
                revision_result.stdout.strip()
                if revision_result.returncode == 0
                else "working-tree-uncommitted"
            )
            paths = sorted(
                {
                    *sorted((self._root / "src/schemabridge").rglob("*.py")),
                    *sorted((self._root / "tests").rglob("*.py")),
                    *(
                        path
                        for path in (self._root / "tests/fixtures").rglob("*")
                        if path.is_file()
                    ),
                    *(
                        path
                        for path in (self._root / "demo").rglob("*")
                        if path.is_file() and path.suffix in {".json", ".sql", ".yml", ".yaml"}
                    ),
                    self._root / "docs/15_EVALUATION.md",
                    self._root / "docker-compose.demo.yml",
                    self._root / "examples/query-recipe-secondary-holders.yml",
                    self._root / "pyproject.toml",
                    self._root / "Makefile",
                }
            )
            fingerprint = fingerprint_fixture(
                tuple((str(path.relative_to(self._root)), path.read_bytes()) for path in paths)
            )
            return EvaluationReleaseIdentity(
                revision=revision,
                source_fingerprint=fingerprint,
                dirty=bool(status_result.stdout.strip()) or revision_result.returncode != 0,
                package_version=__version__,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise EvaluationError(
                EvaluationErrorCode.RELEASE_IDENTITY_FAILED,
                "evaluation release identity could not be inspected",
            ) from error
