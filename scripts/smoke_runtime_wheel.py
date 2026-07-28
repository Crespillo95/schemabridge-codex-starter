"""Build and smoke-test the installed runtime wheel outside a repository checkout."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SMOKE_PROGRAM = """
from pathlib import Path
from schemabridge.adapters.control_plane.migration_paths import (
    resolve_control_plane_migrations_path,
)
from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.bootstrap import build_control_plane_migrator
from schemabridge.config import Settings

path = resolve_control_plane_migrations_path(Path.cwd())
assert path.is_dir()
assert "site-packages" in str(path)
known = PostgresControlPlaneMigrator(
    "postgresql://unused:unused@127.0.0.1:1/unused",
    path,
).known_migrations()
assert tuple(item.version for item in known) == (1, 2, 3, 4, 5, 6, 7, 8, 9)
assert known[0].name == "initial_control_plane"
assert known[1].name == "authenticated_api_jobs"
assert known[2].name == "reject_expired_job_success"
assert known[3].name == "dynamic_catalog_inventory"
assert known[4].name == "semantic_change_management"
assert known[5].name == "dynamic_query_studio"
assert known[6].name == "harden_ai_usage_settlement"
assert known[7].name == "serialize_ai_provider_accounting"
assert known[8].name == "tenant_connector_routing"
composed = build_control_plane_migrator(
    credential_kind="migrator",
    repository_root=Path.cwd(),
    settings=Settings(
        _env_file=None,
        SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL=(
            "postgresql://unused:unused@127.0.0.1:1/unused"
        ),
    ),
)
assert composed.migrations_path == path
assert tuple(item.version for item in composed.known_migrations()) == (
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9,
)
"""


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="schemabridge-wheel-smoke-") as raw_directory:
        directory = Path(raw_directory)
        artifacts = directory / "artifacts"
        artifacts.mkdir()
        subprocess.run(
            [sys.executable, "-m", "hatch", "build", "-t", "wheel", str(artifacts)],
            cwd=ROOT,
            check=True,
        )
        wheels = tuple(artifacts.glob("*.whl"))
        if len(wheels) != 1:
            raise RuntimeError("runtime wheel build did not produce exactly one artifact")

        environment = directory / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                f"{wheels[0]}[api,postgres,sql]",
            ],
            cwd=directory,
            check=True,
        )
        empty_working_directory = directory / "outside-checkout"
        empty_working_directory.mkdir()
        subprocess.run(
            [str(python), "-I", "-c", SMOKE_PROGRAM],
            cwd=empty_working_directory,
            check=True,
        )
        binary_directory = python.parent
        for command in (
            "schemabridge-ai-policy",
            "schemabridge-semantic-change",
            "schemabridge-semantic-reconciler",
            "schemabridge-semantic-profile-worker",
            "schemabridge-connector-route",
        ):
            executable = binary_directory / (
                f"{command}.exe" if sys.platform == "win32" else command
            )
            subprocess.run(
                [str(executable), "--help"],
                cwd=empty_working_directory,
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=30,
            )
    print("Installed runtime wheel validated packaged migrations and M28 operator commands.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
