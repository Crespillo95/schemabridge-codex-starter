#!/usr/bin/env python3
"""Prepare and run the real M26 PostgreSQL/API browser-acceptance stack.

This helper never uses the panel's synthetic fixture.  It runs the real M26
PostgreSQL/DataHub acceptance lifecycle in an isolated retained database,
derives the retained workspace through the same local identity factory used by
the API, and keeps bearer/cursor secrets in owner-only files.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, NoReturn

import psycopg
from psycopg import sql

from schemabridge.adapters.identity.local import LocalDemoPrincipalFactory
from schemabridge.domain.identity import IdentityRole

ROOT: Final = Path(__file__).resolve().parents[1]
DEFAULT_STATE_DIR: Final = ROOT / ".local/m26-browser-acceptance"
DEFAULT_ADMIN_DSN: Final = "postgresql://postgres:local-only-not-a-secret@127.0.0.1:55434/postgres"
API_DSN_TEMPLATE: Final = (
    "postgresql://schemabridge_api:schemabridge_api@127.0.0.1:55434/{database}"
)
STATE_FILE: Final = "state.json"
BEARER_FILE: Final = "api.bearer"
CURSOR_KEY_FILE: Final = "cursor.key"
DROP_CONFIRMATION: Final = "DROP M26 BROWSER ACCEPTANCE DATABASE"
_DATABASE_NAME: Final = re.compile(r"^schemabridge_m26_browser_[0-9a-f]{12}$")
_WORKSPACE_LABEL: Final = re.compile(r"^m26-browser-[0-9a-f]{12}$")
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_STATUSES: Final = frozenset({"current", "review_required", "blocked", "revalidated"})
_SECRET_ENVIRONMENT_KEYS: Final = frozenset(
    {
        "DATABASE_URL",
        "DATAHUB_GMS_TOKEN",
        "OPENAI_API_KEY",
        "SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN",
        "SCHEMABRIDGE_INVENTORY_CURSOR_SIGNING_KEY",
    }
)


class BrowserAcceptanceSetupError(RuntimeError):
    """One bounded setup failure without secret-bearing detail."""


@dataclass(frozen=True, slots=True)
class BrowserAcceptanceState:
    database: str
    local_workspace: str
    workspace_id: str
    created_at: str
    status_counts: dict[str, int]
    report_ids: dict[str, str]
    maximum_finding_count: int
    maximum_impact_count: int

    def __post_init__(self) -> None:
        if not _DATABASE_NAME.fullmatch(self.database):
            raise ValueError("invalid browser acceptance database")
        if not _WORKSPACE_LABEL.fullmatch(self.local_workspace):
            raise ValueError("invalid browser acceptance workspace label")
        if not self.workspace_id.startswith("sb_workspace_v1_"):
            raise ValueError("invalid pseudonymous workspace")
        if set(self.status_counts) != _REQUIRED_STATUSES:
            raise ValueError("browser acceptance statuses are incomplete")
        if any(value < 1 for value in self.status_counts.values()):
            raise ValueError("browser acceptance status has no real report")
        if set(self.report_ids) != _REQUIRED_STATUSES:
            raise ValueError("browser acceptance report identities are incomplete")
        if any(
            not value.startswith("report_") or not _SHA256.fullmatch(value[7:])
            for value in self.report_ids.values()
        ):
            raise ValueError("browser acceptance report identity is invalid")
        if self.maximum_finding_count < 3 or self.maximum_impact_count < 3:
            raise ValueError("browser acceptance paging evidence is insufficient")

    def to_json(self) -> bytes:
        return (
            json.dumps(
                {
                    "schema_version": 1,
                    "database": self.database,
                    "local_workspace": self.local_workspace,
                    "workspace_id": self.workspace_id,
                    "created_at": self.created_at,
                    "status_counts": self.status_counts,
                    "report_ids": self.report_ids,
                    "maximum_finding_count": self.maximum_finding_count,
                    "maximum_impact_count": self.maximum_impact_count,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            + b"\n"
        )

    @classmethod
    def from_json(cls, raw: bytes) -> BrowserAcceptanceState:
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("schema_version") != 1:
                raise ValueError
            return cls(
                database=str(payload["database"]),
                local_workspace=str(payload["local_workspace"]),
                workspace_id=str(payload["workspace_id"]),
                created_at=str(payload["created_at"]),
                status_counts={
                    str(key): int(value) for key, value in dict(payload["status_counts"]).items()
                },
                report_ids={
                    str(key): str(value) for key, value in dict(payload["report_ids"]).items()
                },
                maximum_finding_count=int(payload["maximum_finding_count"]),
                maximum_impact_count=int(payload["maximum_impact_count"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise BrowserAcceptanceSetupError("M26 browser acceptance state is invalid.") from error


def _write_owner_only(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise BrowserAcceptanceSetupError(
            "An owner-only M26 acceptance file could not be created."
        ) from error


def _read_owner_only(path: Path, *, maximum: int) -> bytes:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            or not 0 < metadata.st_size <= maximum
        ):
            raise OSError
        return path.read_bytes()
    except OSError as error:
        raise BrowserAcceptanceSetupError(
            "An owner-only M26 acceptance file is unavailable."
        ) from error


def _resolve_state_dir(raw: Path) -> Path:
    allowed = (ROOT / ".local").resolve()
    resolved = raw.expanduser().resolve()
    if resolved == allowed or allowed not in resolved.parents:
        raise BrowserAcceptanceSetupError(
            "M26 browser state must be a dedicated directory under .local."
        )
    return resolved


def _principal_workspace(local_workspace: str) -> str:
    return (
        LocalDemoPrincipalFactory(
            workspace=local_workspace,
            subject="m26-browser-reader",
            roles=frozenset({IdentityRole.ANALYST}),
        )
        .create(now=datetime.now(UTC))
        .workspace_id
    )


def _api_dsn(database: str) -> str:
    if not _DATABASE_NAME.fullmatch(database):
        raise BrowserAcceptanceSetupError("M26 browser acceptance database identity is invalid.")
    return API_DSN_TEMPLATE.format(database=database)


def _query_state(database: str, local_workspace: str) -> BrowserAcceptanceState:
    workspace_id = _principal_workspace(local_workspace)
    try:
        with psycopg.connect(_api_dsn(database)) as connection:
            rows = connection.execute(
                """
                SELECT status, count(*)
                FROM schemabridge_control.semantic_change_report_public
                WHERE workspace_id = %s
                  AND status = ANY(%s)
                GROUP BY status
                """,
                (workspace_id, list(sorted(_REQUIRED_STATUSES))),
            ).fetchall()
            counts = {str(row[0]): int(row[1]) for row in rows}
            report_rows = connection.execute(
                """
                SELECT DISTINCT ON (status) status, report_id
                FROM schemabridge_control.semantic_change_report_public
                WHERE workspace_id = %s
                  AND status = ANY(%s)
                ORDER BY status, inspected_at DESC, report_id DESC
                """,
                (workspace_id, list(sorted(_REQUIRED_STATUSES))),
            ).fetchall()
            maxima = connection.execute(
                """
                SELECT
                    coalesce(max(finding_count), 0),
                    coalesce(max(
                        mapping_impact_count + join_impact_count
                        + workflow_impact_count + recipe_impact_count
                    ), 0)
                FROM schemabridge_control.semantic_change_report_public
                WHERE workspace_id = %s
                """,
                (workspace_id,),
            ).fetchone()
    except psycopg.Error as error:
        raise BrowserAcceptanceSetupError(
            "The retained M26 PostgreSQL evidence is unavailable."
        ) from error
    if maxima is None:
        raise BrowserAcceptanceSetupError("The retained M26 PostgreSQL evidence is incomplete.")
    try:
        return BrowserAcceptanceState(
            database=database,
            local_workspace=local_workspace,
            workspace_id=workspace_id,
            created_at=datetime.now(UTC).isoformat(),
            status_counts={status: counts.get(status, 0) for status in _REQUIRED_STATUSES},
            report_ids={str(row[0]): str(row[1]) for row in report_rows},
            maximum_finding_count=int(maxima[0]),
            maximum_impact_count=int(maxima[1]),
        )
    except ValueError as error:
        raise BrowserAcceptanceSetupError(
            "The retained M26 PostgreSQL evidence is incomplete."
        ) from error


def _acceptance_environment(
    database: str,
    local_workspace: str,
) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in _SECRET_ENVIRONMENT_KEYS
        and not (key.startswith("SCHEMABRIDGE_CONTROL") and key.endswith("DATABASE_URL"))
    }
    environment.update(
        {
            "SCHEMABRIDGE_TEST_M26_ACCEPTANCE_DATABASE": database,
            "SCHEMABRIDGE_TEST_M26_RETAIN_DATABASE": "1",
            "SCHEMABRIDGE_TEST_M26_LOCAL_WORKSPACE": local_workspace,
            "SCHEMABRIDGE_TEST_M26_BROWSER_SEED": "1",
        }
    )
    return environment


def _drop_database(database: str) -> None:
    if not _DATABASE_NAME.fullmatch(database):
        raise BrowserAcceptanceSetupError("M26 browser acceptance database identity is invalid.")
    admin_dsn = os.environ.get(
        "SCHEMABRIDGE_TEST_CONTROL_ADMIN_DATABASE_URL",
        DEFAULT_ADMIN_DSN,
    )
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )
    except psycopg.Error as error:
        raise BrowserAcceptanceSetupError(
            "The dedicated M26 browser database could not be removed."
        ) from error


def _remove_exact_state_files(state_dir: Path, expected: set[str]) -> None:
    actual = {item.name for item in state_dir.iterdir()}
    if actual != expected or any(not item.is_file() for item in state_dir.iterdir()):
        raise BrowserAcceptanceSetupError(
            "The M26 state directory contains unexpected material and was retained."
        )
    for name in sorted(expected):
        (state_dir / name).unlink()
    state_dir.rmdir()


def _prepare(state_dir: Path) -> BrowserAcceptanceState:
    state_dir = _resolve_state_dir(state_dir)
    try:
        state_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    except OSError as error:
        raise BrowserAcceptanceSetupError(
            "The dedicated M26 browser state directory must not already exist."
        ) from error
    identifier = secrets.token_hex(6)
    database = f"schemabridge_m26_browser_{identifier}"
    local_workspace = f"m26-browser-{identifier}"
    _write_owner_only(
        state_dir / BEARER_FILE,
        (secrets.token_urlsafe(48) + "\n").encode("ascii"),
    )
    _write_owner_only(
        state_dir / CURSOR_KEY_FILE,
        (secrets.token_urlsafe(48) + "\n").encode("ascii"),
    )
    command = (
        str(ROOT / ".venv/bin/python"),
        "-m",
        "pytest",
        "-m",
        "acceptance",
        "tests/acceptance/test_semantic_change_acceptance.py",
        "-q",
    )
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=_acceptance_environment(database, local_workspace),
        check=False,
    )
    if completed.returncode != 0:
        _drop_database(database)
        _remove_exact_state_files(
            state_dir,
            {BEARER_FILE, CURSOR_KEY_FILE},
        )
        raise BrowserAcceptanceSetupError("The real M26 PostgreSQL/DataHub seed did not pass.")
    try:
        state = _query_state(database, local_workspace)
        _write_owner_only(state_dir / STATE_FILE, state.to_json())
    except Exception:
        _drop_database(database)
        _remove_exact_state_files(
            state_dir,
            {BEARER_FILE, CURSOR_KEY_FILE},
        )
        raise
    return state


def _load_state(state_dir: Path) -> tuple[Path, BrowserAcceptanceState]:
    resolved = _resolve_state_dir(state_dir)
    raw = _read_owner_only(resolved / STATE_FILE, maximum=64 * 1024)
    return resolved, BrowserAcceptanceState.from_json(raw)


def _clean_process_environment() -> dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONUNBUFFERED": "1",
    }
    for name in ("LANG", "LC_ALL"):
        if name in os.environ:
            environment[name] = os.environ[name]
    return environment


def _serve_api(state_dir: Path) -> NoReturn:
    resolved, state = _load_state(state_dir)
    bearer = _read_owner_only(resolved / BEARER_FILE, maximum=16 * 1024).decode("ascii").strip()
    cursor_key = (
        _read_owner_only(
            resolved / CURSOR_KEY_FILE,
            maximum=16 * 1024,
        )
        .decode("ascii")
        .strip()
    )
    if bearer == cursor_key:
        raise BrowserAcceptanceSetupError("The M26 bearer and cursor signing key must be distinct.")
    environment = _clean_process_environment()
    environment.update(
        {
            "SCHEMABRIDGE_COMPONENT": "api",
            "SCHEMABRIDGE_ENVIRONMENT": "development",
            "SCHEMABRIDGE_AUTH_MODE": "local-demo",
            "SCHEMABRIDGE_LOCAL_WORKSPACE": state.local_workspace,
            "SCHEMABRIDGE_LOCAL_SUBJECT": "m26-browser-reader",
            "SCHEMABRIDGE_LOCAL_ROLES": '["analyst"]',
            "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
            "SCHEMABRIDGE_CONTROL_API_DATABASE_URL": _api_dsn(state.database),
            "SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN": bearer,
            "SCHEMABRIDGE_INVENTORY_CURSOR_SIGNING_KEY": cursor_key,
            "SCHEMABRIDGE_API_BIND_HOST": "127.0.0.1",
            "SCHEMABRIDGE_API_PORT": "8520",
            "SCHEMABRIDGE_API_ALLOWED_HOSTS": '["127.0.0.1","localhost"]',
            "SCHEMABRIDGE_API_DOCS_ENABLED": "false",
        }
    )
    executable = ROOT / ".venv/bin/schemabridge-api"
    os.execve(executable, (str(executable),), environment)


def _serve_panel(state_dir: Path) -> NoReturn:
    resolved, _state = _load_state(state_dir)
    executable = ROOT / ".venv/bin/python"
    panel = ROOT / "scripts/m26_semantic_change_browser_panel.py"
    arguments = (
        str(executable),
        str(panel),
        "--bind",
        "127.0.0.1",
        "--port",
        "8510",
        "--upstream",
        "http://127.0.0.1:8520",
        "--bearer-file",
        str(resolved / BEARER_FILE),
    )
    os.execve(executable, arguments, _clean_process_environment())


def _status(state_dir: Path) -> BrowserAcceptanceState:
    _resolved, state = _load_state(state_dir)
    return _query_state(state.database, state.local_workspace)


def _cleanup(state_dir: Path, confirmation: str) -> None:
    if confirmation != DROP_CONFIRMATION:
        raise BrowserAcceptanceSetupError("The exact M26 cleanup confirmation is required.")
    resolved, state = _load_state(state_dir)
    expected = {STATE_FILE, BEARER_FILE, CURSOR_KEY_FILE}
    actual = {item.name for item in resolved.iterdir()}
    if actual != expected or any(not item.is_file() for item in resolved.iterdir()):
        raise BrowserAcceptanceSetupError(
            "The M26 state directory contains unexpected material and was retained."
        )
    _drop_database(state.database)
    _remove_exact_state_files(resolved, expected)


def _safe_summary(state: BrowserAcceptanceState) -> str:
    return json.dumps(
        {
            "database": state.database,
            "workspace_id": state.workspace_id,
            "status_counts": state.status_counts,
            "report_ids": state.report_ids,
            "maximum_finding_count": state.maximum_finding_count,
            "maximum_impact_count": state.maximum_impact_count,
        },
        sort_keys=True,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Operate the real M26 browser-acceptance runtime.",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("prepare")
    subcommands.add_parser("status")
    subcommands.add_parser("api")
    subcommands.add_parser("panel")
    cleanup = subcommands.add_parser("cleanup")
    cleanup.add_argument("--confirm", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            print(_safe_summary(_prepare(arguments.state_dir)))
        elif arguments.command == "status":
            print(_safe_summary(_status(arguments.state_dir)))
        elif arguments.command == "api":
            _serve_api(arguments.state_dir)
        elif arguments.command == "panel":
            _serve_panel(arguments.state_dir)
        elif arguments.command == "cleanup":
            _cleanup(arguments.state_dir, str(arguments.confirm))
            print("M26 browser acceptance database and owner-only state removed.")
        else:
            raise BrowserAcceptanceSetupError("The M26 browser acceptance command is invalid.")
    except BrowserAcceptanceSetupError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
