"""Checksum-pinned, explicit PostgreSQL control-plane migrations."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

from schemabridge.application.ports.control_plane_migrations import (
    ControlPlaneMigrationDefinition,
    ControlPlaneMigrationError,
    ControlPlaneMigrationErrorCode,
    ControlPlaneMigrationHistoryRecord,
    ControlPlaneMigrationInspection,
    ControlPlaneMigrationResult,
)

_MIGRATION_FILE_PATTERN = re.compile(r"^(?P<version>[0-9]{4})_(?P<name>[a-z][a-z0-9_]*)\.sql$")
_MAX_MIGRATION_BYTES = 1_048_576
_SCHEMA_NAME = "schemabridge_control"
_APPLICATION_NAME_PATTERN = re.compile(
    r"^schemabridge-control-(?:runtime|api|worker|catalog|reconciler|migrator|observer)$"
)
_HISTORY_RELATION = f"{_SCHEMA_NAME}.schema_migrations"
_LOCK_ID = int.from_bytes(
    hashlib.sha256(b"schemabridge.control-plane.migrations.v1").digest()[:8],
    byteorder="big",
    signed=True,
)

_HISTORY_RELATION_QUERY = "SELECT to_regclass(%s)"
_SCHEMA_EXISTS_QUERY = """
SELECT EXISTS (
    SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = %s
)
"""
_HISTORY_QUERY = f"""
SELECT version, name, checksum
FROM {_HISTORY_RELATION}
ORDER BY version
"""
_LOCK_QUERY = "SELECT pg_try_advisory_xact_lock(%s)"
_INSERT_HISTORY_QUERY = f"""
INSERT INTO {_HISTORY_RELATION} (
    version,
    name,
    checksum,
    applied_at,
    applied_by
) VALUES (%s, %s, %s, CURRENT_TIMESTAMP, CURRENT_USER)
"""


class _ResultCursor(Protocol):
    def fetchone(self) -> tuple[object, ...] | None: ...

    def fetchall(self) -> Sequence[tuple[object, ...]]: ...


class _Connection(Protocol):
    def execute(
        self,
        query: str,
        params: Sequence[object] | None = None,
    ) -> _ResultCursor: ...

    def transaction(self) -> AbstractContextManager[object]: ...


ConnectionFactory = Callable[[str, int], AbstractContextManager[_Connection]]


@dataclass(frozen=True, slots=True)
class _LoadedMigration:
    definition: ControlPlaneMigrationDefinition
    sql: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PostgresControlPlaneMigrator:
    """Inspect or explicitly migrate one isolated PostgreSQL control database."""

    dsn: str = field(repr=False)
    migrations_path: Path
    connect_timeout_seconds: int = 5
    application_name: str = "schemabridge-control-migrator"
    connection_factory: ConnectionFactory | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.dsn.strip():
            raise ValueError("control-plane DSN must not be blank")
        if not 1 <= self.connect_timeout_seconds <= 30:
            raise ValueError("control-plane connect timeout must be between 1 and 30 seconds")
        if _APPLICATION_NAME_PATTERN.fullmatch(self.application_name) is None:
            raise ValueError("control-plane application name is invalid")

    def inspect(self) -> ControlPlaneMigrationInspection:
        """Compare exact history without creating a schema or migration table."""

        migrations = self._load_migrations()
        try:
            with self._connection_context() as connection:
                return self._inspect_connection(connection, migrations)
        except ControlPlaneMigrationError:
            raise
        except Exception as error:
            raise ControlPlaneMigrationError(
                ControlPlaneMigrationErrorCode.DATABASE_UNAVAILABLE,
                "control-plane schema inspection failed",
            ) from error

    def require_current(self) -> ControlPlaneMigrationInspection:
        """Fail closed unless all known migrations are present and exact."""

        inspection = self.inspect()
        if not inspection.is_current:
            raise ControlPlaneMigrationError(
                ControlPlaneMigrationErrorCode.SCHEMA_NOT_CURRENT,
                (
                    "control-plane schema is not current "
                    f"(current={inspection.current_version}, "
                    f"expected={inspection.expected_version})"
                ),
            )
        return inspection

    def migrate(self) -> ControlPlaneMigrationResult:
        """Apply all pending files in one transaction under an advisory lock."""

        migrations = self._load_migrations()
        try:
            with self._connection_context() as connection:
                with connection.transaction():
                    lock_row = connection.execute(_LOCK_QUERY, (_LOCK_ID,)).fetchone()
                    if lock_row is None or lock_row[0] is not True:
                        raise ControlPlaneMigrationError(
                            ControlPlaneMigrationErrorCode.LOCK_UNAVAILABLE,
                            "another control-plane migrator holds the migration lock",
                        )
                    before = self._inspect_connection(connection, migrations)
                    pending_by_version = {
                        migration.definition.version: migration for migration in migrations
                    }
                    applied_versions: list[int] = []
                    for definition in before.pending:
                        migration = pending_by_version[definition.version]
                        connection.execute(migration.sql)
                        connection.execute(
                            _INSERT_HISTORY_QUERY,
                            (
                                definition.version,
                                definition.name,
                                definition.checksum,
                            ),
                        )
                        applied_versions.append(definition.version)
                    after = self._inspect_connection(connection, migrations)
                    if not after.is_current:
                        raise ControlPlaneMigrationError(
                            ControlPlaneMigrationErrorCode.HISTORY_INVALID,
                            "control-plane migration did not produce exact current history",
                        )
                return ControlPlaneMigrationResult(
                    inspection=after,
                    applied_versions=tuple(applied_versions),
                )
        except ControlPlaneMigrationError:
            raise
        except Exception as error:
            raise ControlPlaneMigrationError(
                ControlPlaneMigrationErrorCode.APPLY_FAILED,
                "control-plane migration failed and was rolled back",
            ) from error

    def known_migrations(self) -> tuple[ControlPlaneMigrationDefinition, ...]:
        """Return ordered local identities without opening a database connection."""

        return tuple(migration.definition for migration in self._load_migrations())

    def _connection_context(self) -> AbstractContextManager[_Connection]:
        if self.connection_factory is not None:
            return self.connection_factory(self.dsn, self.connect_timeout_seconds)
        return _default_connection_factory(
            self.dsn,
            self.connect_timeout_seconds,
            self.application_name,
        )

    def _load_migrations(self) -> tuple[_LoadedMigration, ...]:
        try:
            directory = self.migrations_path.resolve(strict=True)
            if not directory.is_dir():
                raise OSError
            candidates = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError as error:
            raise _invalid_migration_set("migration directory is unavailable") from error

        loaded: list[_LoadedMigration] = []
        for path in candidates:
            if path.suffix != ".sql":
                continue
            match = _MIGRATION_FILE_PATTERN.fullmatch(path.name)
            if match is None:
                raise _invalid_migration_set("migration filename is invalid")
            if path.is_symlink() or not path.is_file():
                raise _invalid_migration_set("migration file must be a regular file")
            try:
                if path.resolve(strict=True).parent != directory:
                    raise OSError
                payload = path.read_bytes()
            except OSError as error:
                raise _invalid_migration_set("migration file is unavailable") from error
            if not payload or len(payload) > _MAX_MIGRATION_BYTES:
                raise _invalid_migration_set("migration file size is invalid")
            try:
                sql = payload.decode("utf-8")
            except UnicodeDecodeError as error:
                raise _invalid_migration_set("migration file is not valid UTF-8") from error
            if not sql.strip():
                raise _invalid_migration_set("migration file must not be blank")
            version = int(match.group("version"))
            loaded.append(
                _LoadedMigration(
                    definition=ControlPlaneMigrationDefinition(
                        version=version,
                        name=match.group("name"),
                        checksum=hashlib.sha256(payload).hexdigest(),
                    ),
                    sql=sql,
                )
            )

        if not loaded:
            raise _invalid_migration_set("no control-plane migrations were found")
        for expected, migration in enumerate(loaded, start=1):
            if migration.definition.version != expected:
                raise _invalid_migration_set(
                    "migration versions must be contiguous and begin at one"
                )
        return tuple(loaded)

    def _inspect_connection(
        self,
        connection: _Connection,
        migrations: tuple[_LoadedMigration, ...],
    ) -> ControlPlaneMigrationInspection:
        relation_row = connection.execute(
            _HISTORY_RELATION_QUERY,
            (_HISTORY_RELATION,),
        ).fetchone()
        if relation_row is None:
            raise ControlPlaneMigrationError(
                ControlPlaneMigrationErrorCode.HISTORY_INVALID,
                "control-plane history inspection returned no result",
            )
        if relation_row[0] is None:
            schema_row = connection.execute(
                _SCHEMA_EXISTS_QUERY,
                (_SCHEMA_NAME,),
            ).fetchone()
            if schema_row is None:
                raise ControlPlaneMigrationError(
                    ControlPlaneMigrationErrorCode.HISTORY_INVALID,
                    "control-plane namespace inspection returned no result",
                )
            if schema_row[0] is True:
                raise ControlPlaneMigrationError(
                    ControlPlaneMigrationErrorCode.INCOMPATIBLE_SCHEMA,
                    "control-plane schema exists without exact migration history",
                )
            return ControlPlaneMigrationInspection(
                expected_version=migrations[-1].definition.version,
                applied=(),
                pending=tuple(migration.definition for migration in migrations),
            )

        history_rows = connection.execute(_HISTORY_QUERY).fetchall()
        if not history_rows:
            raise ControlPlaneMigrationError(
                ControlPlaneMigrationErrorCode.HISTORY_INVALID,
                "control-plane migration history is empty",
            )
        applied = self._validate_history(history_rows, migrations)
        pending = tuple(
            migration.definition
            for migration in migrations
            if migration.definition.version > applied[-1].version
        )
        return ControlPlaneMigrationInspection(
            expected_version=migrations[-1].definition.version,
            applied=applied,
            pending=pending,
        )

    @staticmethod
    def _validate_history(
        rows: Sequence[tuple[object, ...]],
        migrations: tuple[_LoadedMigration, ...],
    ) -> tuple[ControlPlaneMigrationHistoryRecord, ...]:
        applied: list[ControlPlaneMigrationHistoryRecord] = []
        expected_version = migrations[-1].definition.version
        for position, row in enumerate(rows, start=1):
            if len(row) != 3:
                raise ControlPlaneMigrationError(
                    ControlPlaneMigrationErrorCode.HISTORY_INVALID,
                    "control-plane migration history has an invalid shape",
                )
            version, name, checksum = row
            if not isinstance(version, int) or isinstance(version, bool):
                raise ControlPlaneMigrationError(
                    ControlPlaneMigrationErrorCode.HISTORY_INVALID,
                    "control-plane migration history has an invalid version",
                )
            if version > expected_version:
                raise ControlPlaneMigrationError(
                    ControlPlaneMigrationErrorCode.SCHEMA_AHEAD,
                    (
                        "control-plane schema is newer than this release "
                        f"(database={version}, expected={expected_version})"
                    ),
                )
            if version != position:
                raise ControlPlaneMigrationError(
                    ControlPlaneMigrationErrorCode.HISTORY_INVALID,
                    "control-plane migration history is not contiguous",
                )
            known = migrations[version - 1].definition
            if not isinstance(name, str) or name != known.name:
                raise ControlPlaneMigrationError(
                    ControlPlaneMigrationErrorCode.HISTORY_INVALID,
                    f"control-plane migration {version} has an unexpected name",
                )
            if not isinstance(checksum, str) or checksum != known.checksum:
                raise ControlPlaneMigrationError(
                    ControlPlaneMigrationErrorCode.CHECKSUM_DRIFT,
                    f"control-plane migration {version} checksum differs from this release",
                )
            applied.append(
                ControlPlaneMigrationHistoryRecord(
                    version=version,
                    name=name,
                    checksum=checksum,
                )
            )
        return tuple(applied)


def _default_connection_factory(
    dsn: str,
    connect_timeout_seconds: int,
    application_name: str,
) -> AbstractContextManager[_Connection]:
    try:
        import psycopg
    except ImportError as error:
        raise ControlPlaneMigrationError(
            ControlPlaneMigrationErrorCode.DATABASE_UNAVAILABLE,
            "PostgreSQL migration support is not installed",
        ) from error
    return cast(
        AbstractContextManager[_Connection],
        psycopg.connect(
            dsn,
            connect_timeout=connect_timeout_seconds,
            autocommit=True,
            application_name=application_name,
        ),
    )


def _invalid_migration_set(message: str) -> ControlPlaneMigrationError:
    return ControlPlaneMigrationError(
        ControlPlaneMigrationErrorCode.MIGRATION_SET_INVALID,
        message,
    )
