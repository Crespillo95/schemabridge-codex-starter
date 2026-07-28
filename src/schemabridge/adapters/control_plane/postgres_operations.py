"""Transaction-consistent, signed PostgreSQL control-plane backup and restore."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

import psycopg
from psycopg import sql

from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.adapters.control_plane.postgres_registry_control import (
    PostgresRegistryControlStore,
)
from schemabridge.application.ports.control_plane_migrations import (
    ControlPlaneMigrationError,
)
from schemabridge.application.ports.control_plane_operations import (
    ControlPlaneOperationError,
    ControlPlaneOperationErrorCode,
)
from schemabridge.domain.control_plane_operations import (
    ControlPlaneBackupManifest,
    ControlPlaneRestoreVerification,
)

_MAX_MANIFEST_BYTES = 65_536
_MAX_BACKUP_SECONDS = 3_600
_SCHEMA = "schemabridge_control"
_ALLOWED_DSN_OPTIONS = frozenset({"sslmode", "sslrootcert"})


class CommandRunner(Protocol):
    def __call__(
        self,
        command: Sequence[str],
        environment: Mapping[str, str],
        timeout_seconds: int,
    ) -> int:
        """Run an operator binary and return only its status code."""


@dataclass(frozen=True, slots=True)
class PostgresControlPlaneBackup:
    """Create one custom-format pg_dump tied to an exported MVCC snapshot."""

    dsn: str = field(repr=False)
    migrations_path: Path
    audit_signing_keys: Mapping[str, bytes] = field(repr=False)
    active_audit_key_version: str
    pg_dump_binary: str = "pg_dump"
    timeout_seconds: int = 900
    runner: CommandRunner = field(default_factory=lambda: _run_command, repr=False)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)

    def __post_init__(self) -> None:
        _validate_operator_configuration(
            self.dsn,
            self.audit_signing_keys,
            self.active_audit_key_version,
            self.timeout_seconds,
        )

    def create_backup(
        self,
        destination: Path,
    ) -> tuple[Path, Path, ControlPlaneBackupManifest]:
        directory = _secure_destination(destination)
        inspection = PostgresControlPlaneMigrator(
            self.dsn,
            self.migrations_path,
        ).require_current()
        if not inspection.applied:
            raise _operation_error(
                ControlPlaneOperationErrorCode.BACKUP_FAILED,
                "control-plane backup requires an applied schema",
            )
        created_at = _aware_now(self.clock)
        stem = f"control-plane-{created_at.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:12]}"
        archive = directory / f"{stem}.dump"
        manifest_path = directory / f"{stem}.manifest.json"
        temporary_archive = directory / f".{stem}.{uuid4().hex}.tmp"
        environment, source_fingerprint = _postgres_environment(self.dsn)

        try:
            with psycopg.connect(
                self.dsn,
                application_name="schemabridge-control-backup",
            ) as connection:
                connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
                snapshot_row = connection.execute("SELECT pg_export_snapshot()").fetchone()
                if snapshot_row is None or not isinstance(snapshot_row[0], str):
                    raise _operation_error(
                        ControlPlaneOperationErrorCode.BACKUP_FAILED,
                        "control-plane backup snapshot could not be exported",
                    )
                snapshot = snapshot_row[0]
                schema_version, schema_checksum = _schema_identity(connection)
                expected = inspection.applied[-1]
                if (
                    schema_version != inspection.current_version
                    or schema_version != expected.version
                    or schema_checksum != expected.checksum
                ):
                    raise _operation_error(
                        ControlPlaneOperationErrorCode.BACKUP_FAILED,
                        "control-plane schema changed during backup preparation",
                    )
                state_sha256, table_counts = _state_digest(connection)
                return_code = self.runner(
                    (
                        self.pg_dump_binary,
                        "--format=custom",
                        "--no-owner",
                        "--no-comments",
                        "--no-publications",
                        "--no-subscriptions",
                        f"--schema={_SCHEMA}",
                        f"--snapshot={snapshot}",
                        f"--file={temporary_archive}",
                    ),
                    environment,
                    self.timeout_seconds,
                )
                if return_code != 0:
                    raise _operation_error(
                        ControlPlaneOperationErrorCode.BACKUP_FAILED,
                        "control-plane backup command failed",
                    )
            _require_new_regular_file(temporary_archive)
            archive_size = temporary_archive.stat().st_size
            archive_sha256 = _sha256_file(temporary_archive)
            os.chmod(temporary_archive, stat.S_IRUSR | stat.S_IWUSR)
            temporary_archive.replace(archive)
            manifest = _signed_manifest(
                source_database_fingerprint=source_fingerprint,
                schema_version=schema_version,
                schema_checksum=schema_checksum,
                archive_name=archive.name,
                archive_size_bytes=archive_size,
                archive_sha256=archive_sha256,
                state_sha256=state_sha256,
                table_counts=table_counts,
                key_version=self.active_audit_key_version,
                key=self.audit_signing_keys[self.active_audit_key_version],
                created_at=created_at,
            )
            _write_owner_only_json(manifest_path, manifest.model_dump(mode="json"))
            return archive, manifest_path, manifest
        except ControlPlaneOperationError:
            _unlink_generated(temporary_archive)
            _unlink_generated(archive)
            _unlink_generated(manifest_path)
            raise
        except FileNotFoundError as error:
            _unlink_generated(temporary_archive)
            raise _operation_error(
                ControlPlaneOperationErrorCode.TOOL_UNAVAILABLE,
                "control-plane backup tool is unavailable",
            ) from error
        except (
            ControlPlaneMigrationError,
            OSError,
            psycopg.Error,
            subprocess.SubprocessError,
            ValueError,
        ) as error:
            _unlink_generated(temporary_archive)
            _unlink_generated(archive)
            _unlink_generated(manifest_path)
            raise _operation_error(
                ControlPlaneOperationErrorCode.BACKUP_FAILED,
                "control-plane backup failed",
            ) from error


@dataclass(frozen=True, slots=True)
class PostgresControlPlaneRestore:
    """Restore a signed archive into a distinct, empty database and verify it."""

    target_dsn: str = field(repr=False)
    migrations_path: Path
    audit_signing_keys: Mapping[str, bytes] = field(repr=False)
    pg_restore_binary: str = "pg_restore"
    timeout_seconds: int = 900
    runner: CommandRunner = field(default_factory=lambda: _run_command, repr=False)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)

    def __post_init__(self) -> None:
        if not self.audit_signing_keys or any(
            len(key) < 32 or len(set(key)) < 8 for key in self.audit_signing_keys.values()
        ):
            raise ValueError("control-plane restore audit keys are invalid")
        _validate_timeout(self.timeout_seconds)
        _postgres_environment(self.target_dsn)

    def restore_backup(
        self,
        archive: Path,
        manifest_path: Path,
    ) -> ControlPlaneRestoreVerification:
        manifest = _load_and_verify_manifest(
            archive,
            manifest_path,
            self.audit_signing_keys,
        )
        environment, target_fingerprint = _postgres_environment(self.target_dsn)
        if target_fingerprint == manifest.source_database_fingerprint:
            raise _operation_error(
                ControlPlaneOperationErrorCode.TARGET_IS_SOURCE,
                "control-plane restore target must differ from its source",
            )
        _require_fresh_target(self.target_dsn)
        try:
            return_code = self.runner(
                (
                    self.pg_restore_binary,
                    "--exit-on-error",
                    "--single-transaction",
                    "--no-owner",
                    f"--dbname={environment['PGDATABASE']}",
                    str(archive),
                ),
                environment,
                self.timeout_seconds,
            )
            if return_code != 0:
                raise _operation_error(
                    ControlPlaneOperationErrorCode.RESTORE_FAILED,
                    "control-plane restore command failed",
                )
            return _verify_restored_database(
                self.target_dsn,
                self.migrations_path,
                self.audit_signing_keys,
                manifest,
                target_fingerprint,
                _aware_now(self.clock),
            )
        except ControlPlaneOperationError:
            raise
        except FileNotFoundError as error:
            raise _operation_error(
                ControlPlaneOperationErrorCode.TOOL_UNAVAILABLE,
                "control-plane restore tool is unavailable",
            ) from error
        except (
            ControlPlaneMigrationError,
            OSError,
            psycopg.Error,
            subprocess.SubprocessError,
            ValueError,
        ) as error:
            raise _operation_error(
                ControlPlaneOperationErrorCode.RESTORE_FAILED,
                "control-plane restore failed",
            ) from error


def _verify_restored_database(
    dsn: str,
    migrations_path: Path,
    audit_signing_keys: Mapping[str, bytes],
    manifest: ControlPlaneBackupManifest,
    target_fingerprint: str,
    verified_at: datetime,
) -> ControlPlaneRestoreVerification:
    try:
        inspection = PostgresControlPlaneMigrator(dsn, migrations_path).require_current()
        if not inspection.applied:
            raise ValueError("restored migration history is empty")
        latest = inspection.applied[-1]
        if (
            inspection.current_version != manifest.schema_version
            or latest.checksum != manifest.schema_checksum
        ):
            raise ValueError("restored migration history differs from manifest")
        with psycopg.connect(
            dsn,
            application_name="schemabridge-control-restore-verifier",
        ) as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            state_sha256, table_counts = _state_digest(connection)
            if state_sha256 != manifest.state_sha256 or table_counts != manifest.table_counts:
                raise ValueError("restored control state differs from manifest")
            workspaces = tuple(
                str(row[0])
                for row in connection.execute(
                    sql.SQL(
                        "SELECT DISTINCT workspace_id FROM {}.control_audit_events "
                        "ORDER BY workspace_id"
                    ).format(sql.Identifier(_SCHEMA))
                ).fetchall()
            )
            counts = _operational_counts(connection)
        store = PostgresRegistryControlStore(
            dsn,
            audit_signing_keys,
            manifest.audit_key_version,
        )
        audit_events = 0
        for workspace_id in workspaces:
            verification = store.verify_audit_chain(workspace_id)
            if not verification.valid:
                raise ValueError("restored control audit chain is invalid")
            audit_events += verification.event_count
        if audit_events != counts["audit_events"]:
            raise ValueError("restored control audit count is inconsistent")
        return ControlPlaneRestoreVerification(
            target_database_fingerprint=target_fingerprint,
            schema_version=inspection.current_version,
            schema_checksum=latest.checksum,
            state_sha256=state_sha256,
            table_counts=table_counts,
            audited_workspaces=len(workspaces),
            audit_events=audit_events,
            active_pointers=counts["active_pointers"],
            transition_records=counts["transitions"],
            pending_outbox_records=counts["pending_outbox"],
            quarantine_records=counts["quarantines"],
            verified_at=verified_at,
        )
    except (ControlPlaneMigrationError, psycopg.Error, ValueError) as error:
        raise _operation_error(
            ControlPlaneOperationErrorCode.VERIFICATION_FAILED,
            "restored control-plane verification failed",
        ) from error


def _schema_identity(connection: psycopg.Connection[Any]) -> tuple[int, str]:
    row = connection.execute(
        sql.SQL(
            "SELECT version, checksum FROM {}.schema_migrations ORDER BY version DESC LIMIT 1"
        ).format(sql.Identifier(_SCHEMA))
    ).fetchone()
    if (
        row is None
        or not isinstance(row[0], int)
        or isinstance(row[0], bool)
        or not isinstance(row[1], str)
    ):
        raise ValueError("control-plane schema identity is invalid")
    return row[0], row[1]


def _state_digest(
    connection: psycopg.Connection[Any],
) -> tuple[str, dict[str, int]]:
    table_rows = connection.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = %s AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """,
        (_SCHEMA,),
    ).fetchall()
    sequence_rows = connection.execute(
        """
        SELECT sequence_name
        FROM information_schema.sequences
        WHERE sequence_schema = %s
        ORDER BY sequence_name
        """,
        (_SCHEMA,),
    ).fetchall()
    if not table_rows:
        raise ValueError("control-plane schema contains no tables")
    digest = hashlib.sha256()
    table_counts: dict[str, int] = {}
    for (raw_name,) in table_rows:
        if not isinstance(raw_name, str):
            raise ValueError("control-plane table name is invalid")
        table = sql.SQL("{}.{}").format(
            sql.Identifier(_SCHEMA),
            sql.Identifier(raw_name),
        )
        rows = connection.execute(
            sql.SQL("SELECT to_jsonb(row_data)::text FROM {} AS row_data ORDER BY 1").format(table)
        )
        count = 0
        digest.update(f"table:{raw_name}\n".encode())
        for row in rows:
            payload = row[0]
            if not isinstance(payload, str):
                raise ValueError("control-plane table payload is invalid")
            digest.update(payload.encode())
            digest.update(b"\n")
            count += 1
        table_counts[raw_name] = count
    for (raw_name,) in sequence_rows:
        if not isinstance(raw_name, str):
            raise ValueError("control-plane sequence name is invalid")
        sequence = sql.SQL("{}.{}").format(
            sql.Identifier(_SCHEMA),
            sql.Identifier(raw_name),
        )
        row = connection.execute(
            sql.SQL("SELECT last_value, is_called FROM {}").format(sequence)
        ).fetchone()
        if row is None:
            raise ValueError("control-plane sequence state is invalid")
        digest.update(f"sequence:{raw_name}:{int(row[0])}:{bool(row[1])}\n".encode())
    return digest.hexdigest(), dict(sorted(table_counts.items()))


def _operational_counts(connection: psycopg.Connection[Any]) -> dict[str, int]:
    queries = {
        "audit_events": "SELECT count(*) FROM {}.control_audit_events",
        "active_pointers": "SELECT count(*) FROM {}.registry_active_pointers",
        "transitions": "SELECT count(*) FROM {}.registry_activation_transitions",
        "pending_outbox": (
            "SELECT count(*) FROM {}.registry_reconciliation_outbox WHERE status = 'pending'"
        ),
        "quarantines": "SELECT count(*) FROM {}.control_quarantine_items",
    }
    counts: dict[str, int] = {}
    for name, query in queries.items():
        row = connection.execute(sql.SQL(query).format(sql.Identifier(_SCHEMA))).fetchone()
        if row is None or not isinstance(row[0], int) or isinstance(row[0], bool):
            raise ValueError("control-plane operational count is invalid")
        counts[name] = row[0]
    return counts


def _signed_manifest(
    *,
    source_database_fingerprint: str,
    schema_version: int,
    schema_checksum: str,
    archive_name: str,
    archive_size_bytes: int,
    archive_sha256: str,
    state_sha256: str,
    table_counts: dict[str, int],
    key_version: str,
    key: bytes,
    created_at: datetime,
) -> ControlPlaneBackupManifest:
    unsigned = ControlPlaneBackupManifest(
        source_database_fingerprint=source_database_fingerprint,
        schema_name=_SCHEMA,
        schema_version=schema_version,
        schema_checksum=schema_checksum,
        archive_name=archive_name,
        archive_size_bytes=archive_size_bytes,
        archive_sha256=archive_sha256,
        state_sha256=state_sha256,
        table_counts=table_counts,
        audit_key_version=key_version,
        created_at=created_at,
        manifest_hmac="0" * 64,
    )
    signature = _manifest_signature(unsigned.unsigned_payload(), key)
    return ControlPlaneBackupManifest.model_validate(
        {
            **unsigned.model_dump(mode="python"),
            "manifest_hmac": signature,
        }
    )


def _load_and_verify_manifest(
    archive: Path,
    manifest_path: Path,
    keys: Mapping[str, bytes],
) -> ControlPlaneBackupManifest:
    try:
        _require_owner_only_regular_file(archive)
        _require_owner_only_regular_file(manifest_path)
        if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
            raise ValueError("manifest is too large")
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = ControlPlaneBackupManifest.model_validate(raw)
        key = keys.get(manifest.audit_key_version)
        if key is None or not hmac.compare_digest(
            manifest.manifest_hmac,
            _manifest_signature(manifest.unsigned_payload(), key),
        ):
            raise ValueError("manifest signature differs")
        if (
            archive.name != manifest.archive_name
            or archive.stat().st_size != manifest.archive_size_bytes
            or _sha256_file(archive) != manifest.archive_sha256
        ):
            raise ValueError("archive identity differs")
        return manifest
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise _operation_error(
            ControlPlaneOperationErrorCode.ARTIFACT_INVALID,
            "control-plane backup artifact failed integrity verification",
        ) from error


def _require_fresh_target(dsn: str) -> None:
    try:
        with psycopg.connect(
            dsn,
            application_name="schemabridge-control-restore-preflight",
        ) as connection:
            schema_row = connection.execute(
                "SELECT to_regnamespace(%s)",
                (_SCHEMA,),
            ).fetchone()
            relation_row = connection.execute(
                """
                SELECT count(*)
                FROM pg_catalog.pg_class AS class
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = class.relnamespace
                WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND namespace.nspname !~ '^pg_toast'
                  AND class.relkind IN ('r', 'p', 'v', 'm', 'S')
                """
            ).fetchone()
        if (
            schema_row is None
            or schema_row[0] is not None
            or relation_row is None
            or relation_row[0] != 0
        ):
            raise _operation_error(
                ControlPlaneOperationErrorCode.TARGET_NOT_FRESH,
                "control-plane restore target is not an empty database",
            )
    except ControlPlaneOperationError:
        raise
    except psycopg.Error as error:
        raise _operation_error(
            ControlPlaneOperationErrorCode.DATABASE_UNAVAILABLE,
            "control-plane restore target is unavailable",
        ) from error


def _postgres_environment(dsn: str) -> tuple[dict[str, str], str]:
    try:
        parsed = urlsplit(dsn)
        options = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as error:
        raise ValueError("control-plane database URL is invalid") from error
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or parsed.hostname is None
        or parsed.username is None
        or not parsed.path
        or parsed.path == "/"
        or parsed.fragment
        or any(name not in _ALLOWED_DSN_OPTIONS for name in options)
        or any(len(values) != 1 or not values[0] for values in options.values())
    ):
        raise ValueError("control-plane database URL is invalid")
    database = unquote(parsed.path.removeprefix("/"))
    username = unquote(parsed.username)
    password = "" if parsed.password is None else unquote(parsed.password)
    port = parsed.port or 5432
    if not database or "/" in database or not username:
        raise ValueError("control-plane database URL is invalid")
    environment = {
        **os.environ,
        "PGHOST": parsed.hostname,
        "PGPORT": str(port),
        "PGUSER": username,
        "PGDATABASE": database,
        "PGCONNECT_TIMEOUT": "5",
    }
    if password:
        environment["PGPASSWORD"] = password
    if "sslmode" in options:
        environment["PGSSLMODE"] = options["sslmode"][0]
    if "sslrootcert" in options:
        environment["PGSSLROOTCERT"] = options["sslrootcert"][0]
    identity = json.dumps(
        {
            "database": database,
            "host": parsed.hostname.lower(),
            "port": port,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return environment, hashlib.sha256(identity.encode()).hexdigest()


def _manifest_signature(payload: Mapping[str, object], key: bytes) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hmac.new(key, canonical, hashlib.sha256).hexdigest()


def _secure_destination(destination: Path) -> Path:
    try:
        if destination.exists():
            if destination.is_symlink() or not destination.is_dir():
                raise OSError
        else:
            destination.mkdir(mode=0o700, parents=True)
        resolved = destination.resolve(strict=True)
        mode = stat.S_IMODE(resolved.stat().st_mode)
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise OSError
        return resolved
    except OSError as error:
        raise _operation_error(
            ControlPlaneOperationErrorCode.CONFIGURATION_INVALID,
            "control-plane backup destination must be an owner-only directory",
        ) from error


def _write_owner_only_json(path: Path, payload: Mapping[str, object]) -> None:
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode()
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        _unlink_generated(path)
        raise


def _require_new_regular_file(path: Path) -> None:
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode) or details.st_size < 1:
        raise OSError("backup command did not create a regular archive")


def _require_owner_only_regular_file(path: Path) -> None:
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode) or details.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise OSError("backup artifact permissions are invalid")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1_048_576):
            digest.update(chunk)
    return digest.hexdigest()


def _unlink_generated(path: Path) -> None:
    try:
        if path.exists() and not path.is_symlink() and path.is_file():
            path.unlink()
    except OSError:
        pass


def _aware_now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("operator clock must return a timezone-aware timestamp")
    return value


def _validate_operator_configuration(
    dsn: str,
    keys: Mapping[str, bytes],
    active_key_version: str,
    timeout_seconds: int,
) -> None:
    _postgres_environment(dsn)
    _validate_timeout(timeout_seconds)
    if active_key_version not in keys or any(
        len(key) < 32 or len(set(key)) < 8 for key in keys.values()
    ):
        raise ValueError("control-plane backup audit keys are invalid")


def _validate_timeout(timeout_seconds: int) -> None:
    if isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= _MAX_BACKUP_SECONDS:
        raise ValueError("control-plane operator timeout is invalid")


def _run_command(
    command: Sequence[str],
    environment: Mapping[str, str],
    timeout_seconds: int,
) -> int:
    result = subprocess.run(
        tuple(command),
        env=dict(environment),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=timeout_seconds,
    )
    return result.returncode


def _operation_error(
    code: ControlPlaneOperationErrorCode,
    message: str,
) -> ControlPlaneOperationError:
    return ControlPlaneOperationError(code, message)
