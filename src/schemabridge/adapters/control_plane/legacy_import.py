"""Offline SQLite reader and transactional PostgreSQL legacy importer."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import stat
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import psycopg
from psycopg import sql
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from schemabridge.application.ports.legacy_import import (
    LegacyImportPortError,
    LegacyImportPortErrorCode,
    LegacySqliteRow,
    LegacySqliteSnapshot,
    LegacySqliteTable,
)
from schemabridge.domain.identity import WorkflowAccessGrant
from schemabridge.domain.legacy_import import (
    LegacyImportApproval,
    LegacyImportDisposition,
    LegacyImportItem,
    LegacyImportPlan,
    LegacyImportReservation,
    LegacyImportResourceKind,
    LegacyImportResult,
    LegacyImportStatus,
    fingerprint_legacy_value,
    legacy_quarantine_id,
    validate_legacy_import_approval,
)
from schemabridge.domain.workflows import AgentWorkflowDraft

_SCHEMA = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_MAX_SOURCE_ROWS = 100_000
_MAX_TABLE_ROWS = 50_000


@dataclass(frozen=True, slots=True)
class _Column:
    name: str
    declared_type: str
    not_null: bool
    primary_key_position: int = 0


_SCHEMAS: dict[str, tuple[_Column, ...]] = {
    "agent_workflow_drafts": (
        _Column("id", "TEXT", False, 1),
        _Column("revision", "INTEGER", True),
        _Column("payload", "TEXT", True),
    ),
    "workflow_access_grants": (
        _Column("workflow_id", "TEXT", False, 1),
        _Column("workspace_id", "TEXT", True),
        _Column("owner_actor_id", "TEXT", True),
        _Column("created_at", "TEXT", True),
    ),
    "analytical_request_drafts": (
        _Column("id", "TEXT", False, 1),
        _Column("revision", "INTEGER", True),
        _Column("payload", "TEXT", True),
    ),
    "review_drafts": (
        _Column("id", "TEXT", False, 1),
        _Column("revision", "INTEGER", True),
        _Column("payload", "TEXT", True),
    ),
    "review_decisions": (
        _Column("id", "TEXT", False, 1),
        _Column("draft_id", "TEXT", True),
        _Column("resulting_version", "INTEGER", True),
        _Column("payload", "TEXT", True),
    ),
    "review_publications": (
        _Column("sequence", "INTEGER", False, 1),
        _Column("draft_id", "TEXT", True),
        _Column("approval_id", "TEXT", True),
        _Column("fingerprint", "TEXT", True),
        _Column("payload", "TEXT", True),
    ),
    "join_review_drafts": (
        _Column("id", "TEXT", False, 1),
        _Column("revision", "INTEGER", True),
        _Column("payload", "TEXT", True),
    ),
    "join_review_decisions": (
        _Column("id", "TEXT", False, 1),
        _Column("draft_id", "TEXT", True),
        _Column("resulting_version", "INTEGER", True),
        _Column("payload", "TEXT", True),
    ),
    "join_publications": (
        _Column("sequence", "INTEGER", False, 1),
        _Column("draft_id", "TEXT", True),
        _Column("payload", "TEXT", True),
    ),
    "publication_approval_identity": (
        _Column("approval_id", "TEXT", False, 1),
        _Column("family", "TEXT", True),
        _Column("actor", "TEXT", True),
        _Column("approved_at", "TEXT", True),
        _Column("new_fingerprint", "TEXT", True),
    ),
    "publication_target_audit": (
        _Column("sequence", "INTEGER", False, 1),
        _Column("approval_id", "TEXT", True),
        _Column("family", "TEXT", True),
        _Column("operation", "TEXT", True),
        _Column("target", "TEXT", True),
        _Column("record_json", "TEXT", True),
    ),
    "fake_query_recipes": (
        _Column("intent_fingerprint", "TEXT", True, 1),
        _Column("version", "INTEGER", True, 2),
        _Column("fingerprint", "TEXT", True),
        _Column("payload", "TEXT", True),
        _Column("is_current", "INTEGER", True),
        _Column("current_document_urn", "TEXT", True),
        _Column("versioned_document_urn", "TEXT", True),
        _Column("approval_id", "TEXT", True),
        _Column("published_at", "TEXT", True),
    ),
    "fake_workflow_publications": (
        _Column("idempotency_key", "TEXT", False, 1),
        _Column("document_ref", "TEXT", True),
        _Column("published_at", "TEXT", True),
    ),
}

_FOREIGN_KEYS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "review_decisions": (("review_drafts", "draft_id", "id"),),
    "review_publications": (("review_drafts", "draft_id", "id"),),
    "join_review_decisions": (("join_review_drafts", "draft_id", "id"),),
    "join_publications": (("join_review_drafts", "draft_id", "id"),),
}

_COMPLETE_FAMILIES = (
    frozenset({"review_drafts", "review_decisions", "review_publications"}),
    frozenset({"join_review_drafts", "join_review_decisions", "join_publications"}),
    frozenset({"publication_approval_identity", "publication_target_audit"}),
)


@dataclass(frozen=True, slots=True)
class SqliteLegacyControlPlaneSource:
    """Read one closed, regular SQLite file without journal recovery or writes."""

    path: Path
    max_source_bytes: int = _MAX_SOURCE_BYTES

    def __post_init__(self) -> None:
        if not 1 <= self.max_source_bytes <= _MAX_SOURCE_BYTES:
            raise ValueError("legacy source size bound is invalid")

    def inspect(self) -> LegacySqliteSnapshot:
        path, before = self._read_source()
        source_fingerprint = hashlib.sha256(before).hexdigest()
        try:
            connection = sqlite3.connect(
                f"{path.as_uri()}?mode=ro&immutable=1",
                uri=True,
                isolation_level=None,
            )
        except sqlite3.Error as error:
            raise _source_error(
                LegacyImportPortErrorCode.SOURCE_CORRUPT,
                "legacy SQLite source could not be opened read-only",
            ) from error
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            check = connection.execute("PRAGMA quick_check").fetchall()
            if check != [("ok",)]:
                raise _source_error(
                    LegacyImportPortErrorCode.SOURCE_CORRUPT,
                    "legacy SQLite integrity check failed",
                )
            table_names, schema_payload = _inspect_schema(connection)
            tables = _read_tables(connection, table_names)
        except LegacyImportPortError:
            raise
        except sqlite3.Error as error:
            raise _source_error(
                LegacyImportPortErrorCode.SOURCE_CORRUPT,
                "legacy SQLite inspection failed",
            ) from error
        finally:
            connection.close()

        try:
            after = path.read_bytes()
        except OSError as error:
            raise _source_error(
                LegacyImportPortErrorCode.SOURCE_CHANGED,
                "legacy SQLite source changed during inspection",
            ) from error
        if before != after:
            raise _source_error(
                LegacyImportPortErrorCode.SOURCE_CHANGED,
                "legacy SQLite source changed during inspection",
            )
        if _has_journal(path):
            raise _source_error(
                LegacyImportPortErrorCode.SOURCE_NOT_OFFLINE,
                "legacy SQLite source gained active journal state",
            )
        return LegacySqliteSnapshot(
            source_fingerprint=source_fingerprint,
            source_schema_fingerprint=fingerprint_legacy_value(schema_payload),
            tables=tables,
        )

    def _read_source(self) -> tuple[Path, bytes]:
        try:
            if self.path.is_symlink():
                raise OSError
            path = self.path.resolve(strict=True)
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError
            if metadata.st_size <= 0:
                raise _source_error(
                    LegacyImportPortErrorCode.SOURCE_CORRUPT,
                    "legacy SQLite source is empty",
                )
            if metadata.st_size > self.max_source_bytes:
                raise _source_error(
                    LegacyImportPortErrorCode.SOURCE_TOO_LARGE,
                    "legacy SQLite source exceeds the configured bound",
                )
            if _has_journal(path):
                raise _source_error(
                    LegacyImportPortErrorCode.SOURCE_NOT_OFFLINE,
                    "legacy SQLite source has active journal state",
                )
            return path, path.read_bytes()
        except LegacyImportPortError:
            raise
        except OSError as error:
            raise _source_error(
                LegacyImportPortErrorCode.SOURCE_UNAVAILABLE,
                "legacy SQLite source is unavailable or unsafe",
            ) from error


def _has_journal(path: Path) -> bool:
    return any(Path(f"{path}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal"))


def _inspect_schema(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, ...], list[object]]:
    objects = connection.execute(
        """
        SELECT type, name, tbl_name
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()
    if any(row[0] not in {"table", "index"} for row in objects):
        raise _schema_error("legacy SQLite contains unsupported schema objects")
    table_names = tuple(sorted(str(row[1]) for row in objects if row[0] == "table"))
    if (
        not table_names
        or len(table_names) != len(set(table_names))
        or any(name not in _SCHEMAS for name in table_names)
    ):
        raise _schema_error("legacy SQLite table set is unknown")
    present = frozenset(table_names)
    if any(family & present and not family <= present for family in _COMPLETE_FAMILIES):
        raise _schema_error("legacy SQLite contains an incomplete table family")

    schema_payload: list[object] = []
    for table_name in table_names:
        columns = connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        observed = tuple(
            _Column(
                name=str(row[1]),
                declared_type=str(row[2]).upper(),
                not_null=bool(row[3]),
                primary_key_position=int(row[5]),
            )
            for row in columns
        )
        if observed != _SCHEMAS[table_name]:
            raise _schema_error("legacy SQLite columns differ from the known schema")
        foreign_keys = tuple(
            sorted(
                (
                    str(row[2]),
                    str(row[3]),
                    str(row[4]),
                )
                for row in connection.execute(f'PRAGMA foreign_key_list("{table_name}")').fetchall()
            )
        )
        if foreign_keys != _FOREIGN_KEYS.get(table_name, ()):
            raise _schema_error("legacy SQLite foreign keys differ from the known schema")
        schema_payload.append(
            {
                "table": table_name,
                "columns": [
                    {
                        "name": column.name,
                        "type": column.declared_type,
                        "not_null": column.not_null,
                        "primary_key_position": column.primary_key_position,
                    }
                    for column in observed
                ],
                "foreign_keys": foreign_keys,
            }
        )
    return table_names, schema_payload


def _read_tables(
    connection: sqlite3.Connection,
    table_names: tuple[str, ...],
) -> tuple[LegacySqliteTable, ...]:
    tables: list[LegacySqliteTable] = []
    total_rows = 0
    for table_name in table_names:
        columns = _SCHEMAS[table_name]
        column_sql = ", ".join(f'"{column.name}"' for column in columns)
        primary_keys = tuple(column for column in columns if column.primary_key_position > 0)
        order_sql = ", ".join(
            f'"{column.name}"'
            for column in sorted(
                primary_keys,
                key=lambda column: column.primary_key_position,
            )
        )
        raw_rows = connection.execute(
            f'SELECT {column_sql} FROM "{table_name}" '
            f"ORDER BY {order_sql} LIMIT {_MAX_TABLE_ROWS + 1}"
        ).fetchall()
        if len(raw_rows) > _MAX_TABLE_ROWS:
            raise _schema_error("legacy SQLite table exceeds the row bound")
        rows = tuple(
            sorted(
                (
                    LegacySqliteRow(
                        source_id_digest=_source_row_digest(
                            table_name,
                            tuple(row),
                            columns,
                        ),
                        values=tuple(row),
                    )
                    for row in raw_rows
                ),
                key=lambda row: row.source_id_digest,
            )
        )
        total_rows += len(rows)
        if total_rows > _MAX_SOURCE_ROWS:
            raise _schema_error("legacy SQLite source exceeds the row bound")
        tables.append(
            LegacySqliteTable(
                name=table_name,
                columns=tuple(column.name for column in columns),
                rows=rows,
            )
        )
    return tuple(tables)


def _source_row_digest(
    table: str,
    values: tuple[object, ...],
    columns: tuple[_Column, ...],
) -> str:
    keys = [
        _safe_scalar(values[index])
        for index, column in enumerate(columns)
        if column.primary_key_position > 0
    ]
    return fingerprint_legacy_value({"table": table, "key": keys})


def _safe_scalar(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return {"float": repr(value)}
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest()}
    return {"type": type(value).__name__}


@dataclass(frozen=True, slots=True)
class PostgresLegacyControlPlaneImportStore:
    """Persist dry-run metadata and apply one exact plan in one transaction."""

    dsn: str = field(repr=False)
    schema: str = "schemabridge_control"
    connect_timeout_seconds: int = 5
    statement_timeout_ms: int = 10_000

    def __post_init__(self) -> None:
        if not self.dsn.strip():
            raise ValueError("control database DSN must not be blank")
        if _SCHEMA.fullmatch(self.schema) is None:
            raise ValueError("control database schema is invalid")
        if not 1 <= self.connect_timeout_seconds <= 30:
            raise ValueError("control database connect timeout is invalid")
        if not 100 <= self.statement_timeout_ms <= 60_000:
            raise ValueError("control database statement timeout is invalid")

    def reserve_dry_run(
        self,
        plan: LegacyImportPlan,
        *,
        recorded_at: datetime,
    ) -> LegacyImportReservation:
        table = self._table("legacy_control_imports")
        try:
            with self._connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT source_fingerprint, source_schema_fingerprint,
                               plan_fingerprint, status, counts_json, created_at,
                               approval_id, completed_at
                        FROM {table}
                        WHERE import_id = %s
                        FOR UPDATE
                        """
                    ).format(table=table),
                    (plan.id,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        sql.SQL(
                            """
                            INSERT INTO {table} (
                                import_id, source_kind, source_fingerprint,
                                source_schema_fingerprint, plan_fingerprint,
                                approval_id, actor, approved_at, status,
                                counts_json, created_at, completed_at
                            ) VALUES (
                                %s, 'sqlite', %s, %s, %s,
                                NULL, NULL, NULL, 'dry_run',
                                %s, %s, NULL
                            )
                            """
                        ).format(table=table),
                        (
                            plan.id,
                            plan.source_fingerprint,
                            plan.source_schema_fingerprint,
                            plan.fingerprint,
                            Jsonb(plan.counts.model_dump(mode="json")),
                            recorded_at,
                        ),
                    )
                    return LegacyImportReservation(
                        import_id=plan.id,
                        source_fingerprint=plan.source_fingerprint,
                        source_schema_fingerprint=plan.source_schema_fingerprint,
                        plan_fingerprint=plan.fingerprint,
                        counts=plan.counts,
                        status=LegacyImportStatus.DRY_RUN,
                        recorded_at=recorded_at,
                    )
                reservation = _reservation(plan.id, row)
                _assert_exact_reservation(plan, reservation)
                return reservation
        except LegacyImportPortError:
            raise
        except UniqueViolation as error:
            raise _store_conflict("legacy dry-run identity already exists") from error
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _store_unavailable("legacy dry-run reservation failed") from error

    def load_reservation(self, import_id: str) -> LegacyImportReservation | None:
        table = self._table("legacy_control_imports")
        try:
            with self._connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT source_fingerprint, source_schema_fingerprint,
                               plan_fingerprint, status, counts_json, created_at,
                               approval_id, completed_at
                        FROM {table}
                        WHERE import_id = %s
                        """
                    ).format(table=table),
                    (import_id,),
                ).fetchone()
            return None if row is None else _reservation(import_id, row)
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _store_unavailable("legacy import reservation read failed") from error

    def apply(
        self,
        plan: LegacyImportPlan,
        approval: LegacyImportApproval,
        *,
        completed_at: datetime,
    ) -> LegacyImportResult:
        try:
            validate_legacy_import_approval(plan, approval)
        except ValueError as error:
            raise _store_conflict("legacy import approval changed") from error
        imports = self._table("legacy_control_imports")
        try:
            with self._connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT source_fingerprint, source_schema_fingerprint,
                               plan_fingerprint, status, counts_json, created_at,
                               approval_id, completed_at
                        FROM {imports}
                        WHERE import_id = %s
                        FOR UPDATE
                        """
                    ).format(imports=imports),
                    (plan.id,),
                ).fetchone()
                if row is None:
                    raise _store_conflict("legacy import dry-run was not reserved")
                reserved = _reservation(plan.id, row)
                _assert_exact_reservation(plan, reserved)
                if reserved.status is LegacyImportStatus.COMPLETED:
                    if reserved.approval_id != approval.id:
                        raise _store_conflict("legacy import completion approval changed")
                    return LegacyImportResult(reservation=reserved, replayed=True)

                for item in plan.items:
                    if (
                        item.disposition is LegacyImportDisposition.IMPORT
                        and item.resource_kind is LegacyImportResourceKind.WORKFLOW_DRAFT
                    ):
                        self._insert_workflow(connection, item)
                for item in plan.items:
                    if (
                        item.disposition is LegacyImportDisposition.IMPORT
                        and item.resource_kind is LegacyImportResourceKind.WORKFLOW_ACCESS_GRANT
                    ):
                        self._insert_workflow_access(connection, item)
                for item in plan.items:
                    if (
                        item.disposition is LegacyImportDisposition.IMPORT
                        and item.resource_kind
                        not in {
                            LegacyImportResourceKind.WORKFLOW_DRAFT,
                            LegacyImportResourceKind.WORKFLOW_ACCESS_GRANT,
                        }
                    ):
                        raise _store_conflict("legacy import contains an unsupported target")
                    if item.disposition is LegacyImportDisposition.QUARANTINE:
                        self._insert_quarantine(
                            connection,
                            plan,
                            item,
                            detected_at=completed_at,
                        )
                    self._insert_item(
                        connection,
                        plan,
                        item,
                        created_at=completed_at,
                    )

                updated = connection.execute(
                    sql.SQL(
                        """
                        UPDATE {imports}
                        SET approval_id = %s,
                            actor = %s,
                            approved_at = %s,
                            status = 'completed',
                            completed_at = %s
                        WHERE import_id = %s AND status = 'dry_run'
                        RETURNING created_at
                        """
                    ).format(imports=imports),
                    (
                        approval.id,
                        approval.actor,
                        approval.approved_at,
                        completed_at,
                        plan.id,
                    ),
                ).fetchone()
                if updated is None:
                    raise _store_conflict("legacy import reservation changed")
                completed = LegacyImportReservation(
                    import_id=plan.id,
                    source_fingerprint=plan.source_fingerprint,
                    source_schema_fingerprint=plan.source_schema_fingerprint,
                    plan_fingerprint=plan.fingerprint,
                    counts=plan.counts,
                    status=LegacyImportStatus.COMPLETED,
                    recorded_at=cast(datetime, updated[0]),
                    approval_id=approval.id,
                    completed_at=completed_at,
                )
                return LegacyImportResult(reservation=completed, replayed=False)
        except LegacyImportPortError:
            raise
        except UniqueViolation as error:
            raise _store_conflict("legacy import target already exists") from error
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _store_unavailable("legacy import transaction failed") from error

    def _insert_workflow(
        self,
        connection: psycopg.Connection[Any],
        item: LegacyImportItem,
    ) -> None:
        payload = _target_payload(item)
        try:
            draft = AgentWorkflowDraft.model_validate(payload["payload"])
        except (KeyError, TypeError, ValueError, ValidationError) as error:
            raise _store_conflict("legacy workflow target payload is invalid") from error
        execution = draft.execution
        if (
            draft.id != payload.get("id")
            or draft.revision != payload.get("revision")
            or draft.updated_at.isoformat() != payload.get("updated_at")
            or (
                execution is None
                and (
                    payload.get("execution_row_count") is not None
                    or payload.get("execution_preview_fingerprint") is not None
                )
            )
            or (
                execution is not None
                and (
                    execution.rows
                    or execution.observed_row_count != payload.get("execution_row_count")
                    or execution.preview_fingerprint != payload.get("execution_preview_fingerprint")
                )
            )
        ):
            raise _store_conflict("legacy workflow target summary changed")
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {table} (
                    workspace_id, id, revision, payload,
                    execution_row_count, execution_preview_fingerprint, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """
            ).format(table=self._table("agent_workflow_drafts")),
            (
                item.workspace_id,
                draft.id,
                draft.revision,
                Jsonb(draft.model_dump(mode="json")),
                payload["execution_row_count"],
                payload["execution_preview_fingerprint"],
                payload["updated_at"],
            ),
        )

    def _insert_workflow_access(
        self,
        connection: psycopg.Connection[Any],
        item: LegacyImportItem,
    ) -> None:
        payload = _target_payload(item)
        try:
            grant = WorkflowAccessGrant.model_validate(payload)
        except (TypeError, ValueError, ValidationError) as error:
            raise _store_conflict("legacy access target payload is invalid") from error
        if grant.workspace_id != item.workspace_id or grant.workflow_id != item.target_id:
            raise _store_conflict("legacy access target scope changed")
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {table} (
                    workspace_id, workflow_id, owner_actor_id, created_at
                ) VALUES (%s, %s, %s, %s)
                """
            ).format(table=self._table("workflow_access_grants")),
            (
                item.workspace_id,
                grant.workflow_id,
                grant.owner_actor_id,
                grant.created_at,
            ),
        )

    def _insert_quarantine(
        self,
        connection: psycopg.Connection[Any],
        plan: LegacyImportPlan,
        item: LegacyImportItem,
        *,
        detected_at: datetime,
    ) -> None:
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {table} (
                    quarantine_id, import_id, workspace_id, resource_type,
                    resource_id_digest, reason_code, payload_fingerprint,
                    details_json, detected_at, resolved_at, resolution_event_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL)
                """
            ).format(table=self._table("control_quarantine_items")),
            (
                legacy_quarantine_id(plan.id, item),
                plan.id,
                item.workspace_id,
                item.resource_kind.value,
                item.source_id_digest,
                item.reason_code,
                item.payload_fingerprint,
                Jsonb(
                    {
                        "source_table": item.source_table,
                        "resource_kind": item.resource_kind.value,
                        "source_id_digest": item.source_id_digest,
                    }
                ),
                detected_at,
            ),
        )

    def _insert_item(
        self,
        connection: psycopg.Connection[Any],
        plan: LegacyImportPlan,
        item: LegacyImportItem,
        *,
        created_at: datetime,
    ) -> None:
        outcome = {
            LegacyImportDisposition.IMPORT: "imported",
            LegacyImportDisposition.QUARANTINE: "quarantined",
            LegacyImportDisposition.SKIP: "skipped",
        }[item.disposition]
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {table} (
                    import_id, source_table, source_id_digest, outcome,
                    target_type, target_id, reason_code, payload_fingerprint, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
            ).format(table=self._table("legacy_control_import_items")),
            (
                plan.id,
                item.source_table,
                item.source_id_digest,
                outcome,
                item.resource_kind.value if item.target_id is not None else None,
                item.target_id,
                item.reason_code,
                item.payload_fingerprint,
                created_at,
            ),
        )

    def _connect(self) -> psycopg.Connection[Any]:
        connection = psycopg.connect(
            self.dsn,
            connect_timeout=self.connect_timeout_seconds,
            application_name="schemabridge-legacy-import",
        )
        connection.execute(
            "SELECT set_config('statement_timeout', %s, false)",
            (f"{self.statement_timeout_ms}ms",),
        )
        return connection

    def _table(self, name: str) -> sql.Composed:
        return sql.SQL("{}.{}").format(
            sql.Identifier(self.schema),
            sql.Identifier(name),
        )


def _reservation(
    import_id: str,
    row: tuple[object, ...],
) -> LegacyImportReservation:
    (
        source_fingerprint,
        source_schema_fingerprint,
        plan_fingerprint,
        status,
        counts,
        recorded_at,
        approval_id,
        completed_at,
    ) = row
    if status not in {LegacyImportStatus.DRY_RUN.value, LegacyImportStatus.COMPLETED.value}:
        raise _store_conflict("legacy import reservation is not in a stable state")
    return LegacyImportReservation(
        import_id=import_id,
        source_fingerprint=source_fingerprint,
        source_schema_fingerprint=source_schema_fingerprint,
        plan_fingerprint=plan_fingerprint,
        counts=counts,
        status=status,
        recorded_at=recorded_at,
        approval_id=approval_id,
        completed_at=completed_at,
    )


def _assert_exact_reservation(
    plan: LegacyImportPlan,
    reservation: LegacyImportReservation,
) -> None:
    if (
        reservation.import_id != plan.id
        or reservation.source_fingerprint != plan.source_fingerprint
        or reservation.source_schema_fingerprint != plan.source_schema_fingerprint
        or reservation.plan_fingerprint != plan.fingerprint
        or reservation.counts != plan.counts
    ):
        raise _store_conflict("legacy import identity is already bound to another plan")


def _target_payload(item: LegacyImportItem) -> dict[str, object]:
    if item.target_payload_json is None:
        raise _store_conflict("legacy imported item has no target payload")
    try:
        payload = json.loads(item.target_payload_json)
    except (TypeError, ValueError) as error:
        raise _store_conflict("legacy target payload is invalid") from error
    if not isinstance(payload, dict):
        raise _store_conflict("legacy target payload is invalid")
    return cast(dict[str, object], payload)


def _source_error(
    code: LegacyImportPortErrorCode,
    message: str,
) -> LegacyImportPortError:
    return LegacyImportPortError(code, message)


def _schema_error(message: str) -> LegacyImportPortError:
    return _source_error(LegacyImportPortErrorCode.SOURCE_SCHEMA_INVALID, message)


def _store_conflict(message: str) -> LegacyImportPortError:
    return LegacyImportPortError(LegacyImportPortErrorCode.STORE_CONFLICT, message)


def _store_unavailable(message: str) -> LegacyImportPortError:
    return LegacyImportPortError(LegacyImportPortErrorCode.STORE_UNAVAILABLE, message)
