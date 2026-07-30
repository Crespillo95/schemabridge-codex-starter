"""Sanitized non-interactive entrypoint for scheduled control-plane backups."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn, TextIO

from schemabridge.application.ports.control_plane_migrations import (
    ControlPlaneMigrationError,
)
from schemabridge.application.ports.control_plane_operations import (
    ControlPlaneOperationError,
)
from schemabridge.application.postgres_health import DatabaseConfigurationError
from schemabridge.bootstrap import build_control_plane_backup

_SCHEMA_VERSION = "schemabridge.scheduled-backup.v1"


class _ArgumentsRejected(ValueError):
    pass


class _SanitizedParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise _ArgumentsRejected


def _parser() -> argparse.ArgumentParser:
    parser = _SanitizedParser(
        prog="schemabridge-backup",
        description="Create one signed control-plane backup without logging private paths.",
    )
    parser.add_argument("--destination", required=True, type=Path)
    return parser


def _write(stream: TextIO, payload: dict[str, Any]) -> None:
    print(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
        file=stream,
    )


def command(argv: Sequence[str] | None = None) -> int:
    """Create one backup and emit only bounded, path-free evidence."""

    try:
        arguments = _parser().parse_args(argv)
        destination: Path = arguments.destination
        if not destination.is_absolute():
            raise _ArgumentsRejected
        _archive, _manifest_path, manifest = build_control_plane_backup().create_backup(destination)
        _write(
            sys.stdout,
            {
                "schema_version": _SCHEMA_VERSION,
                "command": "backup",
                "outcome": "succeeded",
                "control_schema_version": manifest.schema_version,
                "control_schema_checksum": manifest.schema_checksum,
                "archive_sha256": manifest.archive_sha256,
                "state_sha256": manifest.state_sha256,
                "created_at": manifest.created_at.isoformat(),
            },
        )
        return 0
    except _ArgumentsRejected:
        code = "backup_arguments_invalid"
    except DatabaseConfigurationError:
        code = "backup_configuration_invalid"
    except ControlPlaneMigrationError:
        code = "backup_schema_invalid"
    except ControlPlaneOperationError:
        code = "backup_operation_failed"
    except Exception:
        code = "backup_internal_failure"
    _write(
        sys.stderr,
        {
            "schema_version": _SCHEMA_VERSION,
            "command": "backup",
            "outcome": "failed",
            "code": code,
        },
    )
    return 1


def main() -> int:
    return command()


if __name__ == "__main__":
    raise SystemExit(main())
