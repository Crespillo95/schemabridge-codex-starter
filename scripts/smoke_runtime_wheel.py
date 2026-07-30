"""Build and smoke-test the installed runtime wheel outside a repository checkout."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import venv
from pathlib import Path
from typing import TypeVar

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_CONTROL_PLANE_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, "initial_control_plane"),
    (2, "authenticated_api_jobs"),
    (3, "reject_expired_job_success"),
    (4, "dynamic_catalog_inventory"),
    (5, "semantic_change_management"),
    (6, "dynamic_query_studio"),
    (7, "harden_ai_usage_settlement"),
    (8, "serialize_ai_provider_accounting"),
    (9, "tenant_connector_routing"),
    (10, "operational_observer"),
    (11, "connector_secret_versions"),
    (12, "backup_identity"),
)
EXPECTED_RUNTIME_ENTRYPOINTS: tuple[tuple[str, str], ...] = (
    ("schemabridge", "schemabridge.entrypoints.cli.main:app"),
    ("schemabridge-api", "schemabridge.entrypoints.http.main:main"),
    ("schemabridge-ai-policy", "schemabridge.entrypoints.ai_policy.main:main"),
    ("schemabridge-backup", "schemabridge.entrypoints.backup.main:main"),
    ("schemabridge-catalog", "schemabridge.entrypoints.catalog.main:main"),
    (
        "schemabridge-connector-route",
        "schemabridge.entrypoints.connector_route.main:main",
    ),
    ("schemabridge-observer", "schemabridge.entrypoints.observer.main:main"),
    (
        "schemabridge-semantic-change",
        "schemabridge.entrypoints.semantic_change.main:main",
    ),
    (
        "schemabridge-semantic-profile-worker",
        "schemabridge.entrypoints.semantic_profile_worker.main:main",
    ),
    (
        "schemabridge-semantic-reconciler",
        "schemabridge.entrypoints.semantic_reconciler.main:main",
    ),
    ("schemabridge-web", "schemabridge.entrypoints.streamlit.main:main"),
    ("schemabridge-worker", "schemabridge.entrypoints.worker.main:main"),
)
HELP_CAPABLE_RUNTIME_COMMANDS: tuple[str, ...] = (
    "schemabridge",
    "schemabridge-ai-policy",
    "schemabridge-catalog",
    "schemabridge-connector-route",
    "schemabridge-semantic-change",
    "schemabridge-semantic-profile-worker",
    "schemabridge-semantic-reconciler",
    "schemabridge-web",
    "schemabridge-worker",
)

ContractItem = TypeVar("ContractItem")


def _require_exact_contract(
    *,
    label: str,
    actual: tuple[ContractItem, ...],
    expected: tuple[ContractItem, ...],
) -> None:
    if actual != expected:
        raise RuntimeError(f"{label} contract mismatch")


SMOKE_PROGRAM = f"""
from importlib.metadata import distribution
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
expected_migrations = {EXPECTED_CONTROL_PLANE_MIGRATIONS!r}
known = PostgresControlPlaneMigrator(
    "postgresql://unused:unused@127.0.0.1:1/unused",
    path,
).known_migrations()
actual_migrations = tuple((item.version, item.name) for item in known)
if actual_migrations != expected_migrations:
    raise RuntimeError("installed control-plane migration contract mismatch")
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
composed_migrations = tuple(
    (item.version, item.name) for item in composed.known_migrations()
)
if composed_migrations != expected_migrations:
    raise RuntimeError("composed control-plane migration contract mismatch")

expected_entrypoints = tuple(sorted({EXPECTED_RUNTIME_ENTRYPOINTS!r}))
installed_entrypoints = tuple(
    sorted(
        (entry_point.name, entry_point.value)
        for entry_point in distribution("schemabridge").entry_points
        if entry_point.group == "console_scripts"
    )
)
if installed_entrypoints != expected_entrypoints:
    raise RuntimeError("installed runtime entrypoint contract mismatch")
for entry_point in distribution("schemabridge").entry_points:
    if entry_point.group != "console_scripts":
        continue
    if not callable(entry_point.load()):
        raise RuntimeError("installed runtime entrypoint is not callable")
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
        expected_commands = tuple(name for name, _target in EXPECTED_RUNTIME_ENTRYPOINTS)
        installed_commands = tuple(
            command
            for command in expected_commands
            if (
                binary_directory / (f"{command}.exe" if sys.platform == "win32" else command)
            ).is_file()
        )
        _require_exact_contract(
            label="installed runtime executable",
            actual=installed_commands,
            expected=expected_commands,
        )
        for command in HELP_CAPABLE_RUNTIME_COMMANDS:
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
    print("Installed runtime wheel validated migrations 1-12 and all runtime entrypoints.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
