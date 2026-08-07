"""PostgreSQL lifecycle store for durable semantic-change scan requests.

The reconciler receives only this control-plane adapter.  Lease capabilities are
hashed before persistence, every lifecycle timestamp comes from PostgreSQL, and
report evidence is committed in the same transaction as terminal completion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg import sql
from psycopg.errors import (
    CheckViolation,
    ForeignKeyViolation,
    SerializationFailure,
    UndefinedColumn,
    UndefinedTable,
    UniqueViolation,
)
from pydantic import ValidationError

from schemabridge.adapters.semantic_change.postgres_store import (
    PostgresSemanticChangeStore,
)
from schemabridge.adapters.storage.postgres import (
    ControlConnectionProvider,
    _ControlDatabase,
)
from schemabridge.application.ports.semantic_change import (
    SemanticChangePortError,
)
from schemabridge.application.ports.semantic_change_scans import (
    SemanticChangeScanInspection,
    SemanticChangeScanStoreError,
    SemanticChangeScanStoreErrorCode,
)
from schemabridge.domain.semantic_change import (
    SemanticChangeReport,
    SemanticEvidenceObservation,
    SemanticImpactSummary,
)
from schemabridge.domain.semantic_change_scans import (
    SemanticChangeScanCompletion,
    SemanticChangeScanFailure,
    SemanticChangeScanFailureCode,
    SemanticChangeScanLease,
    SemanticChangeScanRequest,
    SemanticChangeScanSourceKind,
    SemanticChangeScanStatus,
    SemanticChangeScanSupersession,
    SemanticChangeScanTransitionError,
    SemanticChangeScanTransitionErrorCode,
    claim_semantic_change_scan,
    classify_semantic_change_scan_failure,
    complete_semantic_change_scan,
    digest_semantic_change_scan_capability,
    fail_semantic_change_scan,
    heartbeat_semantic_change_scan,
    reclaim_expired_semantic_change_scan,
    semantic_change_scan_claim_matches,
    semantic_change_scan_retry_delay,
    supersede_semantic_change_scan,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope

_SCAN_ID = re.compile(r"^scan_[0-9a-f]{64}$")
_RECONCILER_ID = re.compile(r"^[a-z][a-z0-9_-]{2,199}$")
_MIN_LEASE_DURATION = timedelta(seconds=10)
_MAX_LEASE_DURATION = timedelta(minutes=5)
_MIN_RETENTION = timedelta(days=1)
_MAX_RETENTION = timedelta(days=366)
_MAX_MAINTENANCE_BATCH = 1_000

_SCAN_COLUMN_NAMES = (
    "scan_id",
    "workspace_id",
    "source_kind",
    "source_event_key",
    "source_fingerprint",
    "catalog_scope",
    "registry_id",
    "registry_generation",
    "connection_id",
    "base_catalog_generation",
    "observed_catalog_generation",
    "status",
    "attempts",
    "max_attempts",
    "available_at",
    "lease_owner_id",
    "lease_capability_digest",
    "fencing_token",
    "lease_acquired_at",
    "lease_heartbeat_at",
    "lease_expires_at",
    "last_reason_code",
    "requested_at",
    "updated_at",
    "completed_at",
    "completed_report_id",
    "completed_report_fingerprint",
    "superseded_by_scan_id",
    "superseded_by_scan_fingerprint",
    "retain_until",
)
_SCAN_COLUMNS = sql.SQL(", ").join(sql.Identifier(name) for name in _SCAN_COLUMN_NAMES)


@dataclass(frozen=True, slots=True)
class PostgresSemanticChangeScanStore:
    """Fenced at-least-once scan queue with an atomic report completion boundary."""

    dsn: str = field(repr=False)
    report_store: PostgresSemanticChangeStore = field(repr=False)
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-reconciler"
    connection_provider: ControlConnectionProvider | None = field(default=None, repr=False)
    _database: _ControlDatabase = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.report_store.dsn != self.dsn or self.report_store.schema != self.schema:
            raise ValueError(
                "semantic scan and report stores must share one control database and schema"
            )
        object.__setattr__(
            self,
            "_database",
            _ControlDatabase(
                self.dsn,
                self.schema,
                application_name=self.application_name,
                connection_provider=self.connection_provider,
            ),
        )

    def reclaim_expired(
        self,
        *,
        limit: int,
        retention: timedelta,
    ) -> int:
        """Requeue or close a bounded locked batch of expired leases."""

        _maintenance_limit(limit)
        retained_for = _retention(retention)
        table = self._database.table("semantic_change_scan_requests")
        try:
            with self._database.connect() as connection:
                rows = connection.execute(
                    sql.SQL(
                        """
                        SELECT {columns}
                        FROM {table}
                        WHERE status = 'leased'
                          AND lease_expires_at <= clock_timestamp()
                        ORDER BY lease_expires_at, workspace_id, scan_id
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                        """
                    ).format(columns=_SCAN_COLUMNS, table=table),
                    (limit,),
                ).fetchall()
                for row in rows:
                    current = _scan_from_row(row)
                    reclaimed_at = _database_transition_time(
                        connection,
                        current.updated_at,
                    )
                    retain_until = (
                        reclaimed_at + retained_for
                        if current.attempts >= current.max_attempts
                        else None
                    )
                    changed = reclaim_expired_semantic_change_scan(
                        current,
                        reclaimed_at=reclaimed_at,
                        retain_until=retain_until,
                    )
                    self._write_state(connection, current, changed)
                return len(rows)
        except SemanticChangeScanStoreError:
            raise
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except SemanticChangeScanTransitionError as error:
            raise _transition_error(error) from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response("semantic scan reclaim response was invalid") from error
        except psycopg.Error as error:
            raise _store_unavailable("semantic scan lease reclaim failed") from error
        except Exception as error:
            raise _store_unavailable("semantic scan lease reclaim failed") from error

    def supersede_obsolete(
        self,
        *,
        limit: int,
        retention: timedelta,
    ) -> int:
        """Supersede a bounded waiting batch when an exact newer request exists."""

        _maintenance_limit(limit)
        retained_for = _retention(retention)
        table = self._database.table("semantic_change_scan_requests")
        try:
            with self._database.connect() as connection:
                rows = connection.execute(
                    sql.SQL(
                        """
                        SELECT {columns}
                        FROM {table} AS candidate
                        WHERE candidate.status IN ('requested', 'retry_wait')
                          AND EXISTS (
                              SELECT 1
                              FROM {table} AS newer
                              WHERE newer.workspace_id = candidate.workspace_id
                                AND newer.source_kind = candidate.source_kind
                                AND newer.scan_id <> candidate.scan_id
                                AND (
                                    (
                                        candidate.source_kind = 'catalog_generation'
                                        AND newer.connection_id
                                            = candidate.connection_id
                                        AND newer.catalog_scope
                                            = candidate.catalog_scope
                                        AND newer.registry_id
                                            = candidate.registry_id
                                        AND newer.observed_catalog_generation
                                            > candidate.observed_catalog_generation
                                    )
                                    OR
                                    (
                                        candidate.source_kind = 'registry_pointer'
                                        AND newer.catalog_scope
                                            = candidate.catalog_scope
                                        AND newer.registry_id
                                            = candidate.registry_id
                                        AND newer.registry_generation
                                            > candidate.registry_generation
                                    )
                                )
                          )
                        ORDER BY candidate.available_at,
                                 candidate.requested_at,
                                 candidate.workspace_id,
                                 candidate.scan_id
                        LIMIT %s
                        FOR UPDATE OF candidate SKIP LOCKED
                        """
                    ).format(columns=_qualified_scan_columns("candidate"), table=table),
                    (limit,),
                ).fetchall()
                for row in rows:
                    current = _scan_from_row(row)
                    successor = self._newer_successor(connection, current)
                    if successor is None:
                        continue
                    superseded_at = _database_transition_time(
                        connection,
                        current.updated_at,
                    )
                    changed = supersede_semantic_change_scan(
                        current,
                        superseded_by=successor,
                        superseded_at=superseded_at,
                        retain_until=superseded_at + retained_for,
                    )
                    self._write_state(connection, current, changed)
                return len(rows)
        except SemanticChangeScanStoreError:
            raise
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except SemanticChangeScanTransitionError as error:
            raise _transition_error(error) from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response("semantic scan supersession response was invalid") from error
        except psycopg.Error as error:
            raise _store_unavailable("semantic scan supersession sweep failed") from error
        except Exception as error:
            raise _store_unavailable("semantic scan supersession sweep failed") from error

    def claim_next(
        self,
        *,
        reconciler_id: str,
        lease_capability: str,
        lease_duration: timedelta,
    ) -> SemanticChangeScanRequest | None:
        """Claim one due request with ``SKIP LOCKED`` and a monotonic fence."""

        _reconciler_id(reconciler_id)
        digest_semantic_change_scan_capability(lease_capability)
        leased_for = _lease_duration(lease_duration)
        table = self._database.table("semantic_change_scan_requests")
        try:
            with self._database.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT {columns}
                        FROM {table}
                        WHERE status IN ('requested', 'retry_wait')
                          AND available_at <= clock_timestamp()
                          AND attempts < max_attempts
                        ORDER BY available_at, requested_at, workspace_id, scan_id
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                        """
                    ).format(columns=_SCAN_COLUMNS, table=table)
                ).fetchone()
                if row is None:
                    return None
                current = _scan_from_row(row)
                claimed_at = _database_transition_time(connection, current.updated_at)
                changed = claim_semantic_change_scan(
                    current,
                    reconciler_id=reconciler_id,
                    lease_capability=lease_capability,
                    claimed_at=claimed_at,
                    lease_expires_at=claimed_at + leased_for,
                )
                self._write_state(connection, current, changed)
                return changed
        except SemanticChangeScanStoreError:
            raise
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except SemanticChangeScanTransitionError as error:
            raise _transition_error(error) from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response("semantic scan claim response was invalid") from error
        except psycopg.Error as error:
            raise _store_unavailable("semantic scan claim failed") from error
        except Exception as error:
            raise _store_unavailable("semantic scan claim failed") from error

    def load(
        self,
        workspace_id: str,
        scan_id: str,
    ) -> SemanticChangeScanRequest | None:
        """Load only one exact tenant-qualified request."""

        _workspace_id(workspace_id)
        _scan_id(scan_id)
        table = self._database.table("semantic_change_scan_requests")
        try:
            with self._database.connect() as connection, connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT {columns}
                        FROM {table}
                        WHERE workspace_id = %s
                          AND scan_id = %s
                        """
                    ).format(columns=_SCAN_COLUMNS, table=table),
                    (workspace_id, scan_id),
                ).fetchone()
            return None if row is None else _scan_from_row(row)
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response("semantic scan read response was invalid") from error
        except psycopg.Error as error:
            raise _store_unavailable("semantic scan read failed") from error
        except Exception as error:
            raise _store_unavailable("semantic scan read failed") from error

    def load_latest_for_scope(
        self,
        scope: SemanticRegistryScope,
    ) -> SemanticChangeScanRequest | None:
        """Load the latest retained, non-superseded exact registry-pointer scan.

        Catalog-generation requests intentionally do not match: their durable
        identity contains a connection but no registry scope, so associating one
        with a registry here would be an unsafe inference.
        """

        if not isinstance(scope, SemanticRegistryScope):
            raise ValueError("semantic registry scope is invalid")
        table = self._database.table("semantic_change_scan_requests")
        try:
            with self._database.connect() as connection, connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT {columns}
                        FROM {table}
                        WHERE workspace_id = %s
                          AND source_kind = 'registry_pointer'
                          AND catalog_scope = %s
                          AND registry_id = %s
                          AND status <> 'superseded'
                          AND (
                              status IN ('requested', 'leased', 'retry_wait')
                              OR retain_until > clock_timestamp()
                          )
                        ORDER BY registry_generation DESC,
                                 requested_at DESC,
                                 scan_id DESC
                        LIMIT 1
                        """
                    ).format(columns=_SCAN_COLUMNS, table=table),
                    (
                        scope.workspace_id,
                        scope.catalog_scope,
                        scope.registry_id,
                    ),
                ).fetchone()
            return None if row is None else _scan_from_row(row)
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response("semantic scan scope read response was invalid") from error
        except psycopg.Error as error:
            raise _store_unavailable("semantic scan scope read failed") from error
        except Exception as error:
            raise _store_unavailable("semantic scan scope read failed") from error

    def heartbeat(
        self,
        workspace_id: str,
        scan_id: str,
        *,
        reconciler_id: str,
        lease_capability: str,
        fencing_token: int,
        lease_duration: timedelta,
    ) -> SemanticChangeScanRequest:
        """Extend only the exact current unexpired lease."""

        leased_for = _lease_inputs(
            workspace_id,
            scan_id,
            reconciler_id,
            lease_capability,
            fencing_token,
            lease_duration,
        )
        try:
            with self._database.connect() as connection:
                current = self._load_for_update(connection, workspace_id, scan_id)
                heartbeat_at = _database_transition_time(connection, current.updated_at)
                changed = heartbeat_semantic_change_scan(
                    current,
                    reconciler_id=reconciler_id,
                    lease_capability=lease_capability,
                    fencing_token=fencing_token,
                    heartbeat_at=heartbeat_at,
                    lease_expires_at=heartbeat_at + leased_for,
                )
                self._write_state(connection, current, changed)
                return changed
        except SemanticChangeScanStoreError:
            raise
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except SemanticChangeScanTransitionError as error:
            raise _transition_error(error) from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response("semantic scan heartbeat response was invalid") from error
        except psycopg.Error as error:
            raise _store_unavailable("semantic scan heartbeat failed") from error
        except Exception as error:
            raise _store_unavailable("semantic scan heartbeat failed") from error

    def supersede_if_obsolete(
        self,
        workspace_id: str,
        scan_id: str,
        *,
        reconciler_id: str,
        lease_capability: str,
        fencing_token: int,
        retention: timedelta,
    ) -> SemanticChangeScanRequest:
        """Keep the lease or bind it atomically to an exact newer request."""

        _lease_identity(
            workspace_id,
            scan_id,
            reconciler_id,
            lease_capability,
            fencing_token,
        )
        retained_for = _retention(retention)
        try:
            with self._database.connect() as connection:
                current = self._load_for_update(connection, workspace_id, scan_id)
                checked_at = _database_transition_time(connection, current.updated_at)
                if not semantic_change_scan_claim_matches(
                    current,
                    reconciler_id=reconciler_id,
                    lease_capability=lease_capability,
                    fencing_token=fencing_token,
                    at=checked_at,
                ):
                    raise _lease_lost()
                successor = self._newer_successor(connection, current)
                if successor is None:
                    return current
                changed = supersede_semantic_change_scan(
                    current,
                    superseded_by=successor,
                    superseded_at=checked_at,
                    retain_until=checked_at + retained_for,
                    reconciler_id=reconciler_id,
                    lease_capability=lease_capability,
                    fencing_token=fencing_token,
                )
                self._write_state(connection, current, changed)
                return changed
        except SemanticChangeScanStoreError:
            raise
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except SemanticChangeScanTransitionError as error:
            raise _transition_error(error) from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response("semantic scan supersession response was invalid") from error
        except psycopg.Error as error:
            raise _store_unavailable("semantic scan supersession check failed") from error
        except Exception as error:
            raise _store_unavailable("semantic scan supersession check failed") from error

    def complete(
        self,
        workspace_id: str,
        scan_id: str,
        *,
        reconciler_id: str,
        lease_capability: str,
        fencing_token: int,
        inspection: SemanticChangeScanInspection,
        completed_at: datetime,
        retain_until: datetime,
    ) -> SemanticChangeScanRequest:
        """Commit report, observation, findings, impacts, and scan atomically."""

        _lease_identity(
            workspace_id,
            scan_id,
            reconciler_id,
            lease_capability,
            fencing_token,
        )
        checked_inspection = _inspection(inspection)
        retention = _retention_window(completed_at, retain_until)
        try:
            with self._database.connect() as connection:
                current = self._load_for_update(connection, workspace_id, scan_id)
                if current.status is SemanticChangeScanStatus.COMPLETED:
                    return self._load_exact_completion(
                        connection,
                        current,
                        checked_inspection,
                    )
                if not _inspection_matches_request(current, checked_inspection):
                    raise _state_conflict(
                        "semantic scan inspection does not match its durable trigger"
                    )
                impacts = self.report_store.dependency_index.resolve_impacts(
                    checked_inspection.report.context,
                    checked_inspection.report.findings,
                )
                if SemanticImpactSummary.from_set(impacts) != checked_inspection.report.impacts:
                    raise _state_conflict(
                        "semantic scan impact set does not match the immutable report"
                    )
                database_completed_at = _database_transition_time(
                    connection,
                    current.updated_at,
                )
                completion = SemanticChangeScanCompletion(
                    report_id=checked_inspection.report.id,
                    report_fingerprint=checked_inspection.report.fingerprint,
                    completed_at=database_completed_at,
                )
                changed = complete_semantic_change_scan(
                    current,
                    reconciler_id=reconciler_id,
                    lease_capability=lease_capability,
                    fencing_token=fencing_token,
                    completion=completion,
                    retain_until=database_completed_at + retention,
                )

                # These helpers deliberately accept an existing connection so the
                # immutable evidence and terminal queue state have no partial window.
                self.report_store._insert_report(
                    connection,
                    self._database.table("semantic_change_reports"),
                    checked_inspection.report,
                    checked_inspection.observation,
                )
                self.report_store._insert_findings(
                    connection,
                    checked_inspection.report,
                    checked_inspection.observation,
                )
                self.report_store._insert_impacts(
                    connection,
                    checked_inspection.report,
                    impacts,
                )
                stored_report = self.report_store._load_report(
                    connection,
                    checked_inspection.report.context.scope,
                    checked_inspection.report.id,
                )
                stored_observation = self.report_store._load_observation(
                    connection,
                    checked_inspection.report.context.scope,
                    checked_inspection.report.id,
                )
                if (
                    stored_report != checked_inspection.report
                    or stored_observation != checked_inspection.observation
                ):
                    raise _state_conflict(
                        "semantic report identity already contains different evidence"
                    )
                self._write_state(connection, current, changed)
                return changed
        except SemanticChangeScanStoreError:
            raise
        except SemanticChangePortError as error:
            raise _store_unavailable("semantic scan impact resolution failed") from error
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except (CheckViolation, ForeignKeyViolation, UniqueViolation) as error:
            raise _state_conflict("semantic scan completion conflicted") from error
        except SerializationFailure as error:
            raise _state_conflict("semantic scan completion conflicted") from error
        except SemanticChangeScanTransitionError as error:
            raise _transition_error(error) from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response("semantic scan completion response was invalid") from error
        except psycopg.Error as error:
            raise _store_unavailable("semantic scan completion failed") from error
        except Exception as error:
            raise _store_unavailable("semantic scan completion failed") from error

    def fail(
        self,
        workspace_id: str,
        scan_id: str,
        *,
        reconciler_id: str,
        lease_capability: str,
        fencing_token: int,
        code: SemanticChangeScanFailureCode,
        failed_at: datetime,
        retry_at: datetime | None,
        retain_until: datetime | None,
    ) -> SemanticChangeScanRequest:
        """Persist only the closed failure code and deterministic retry state."""

        _lease_identity(
            workspace_id,
            scan_id,
            reconciler_id,
            lease_capability,
            fencing_token,
        )
        _aware(failed_at, "semantic scan failure time")
        if retry_at is not None:
            _aware(retry_at, "semantic scan retry time")
        terminal_retention = (
            None if retain_until is None else _retention_window(failed_at, retain_until)
        )
        try:
            with self._database.connect() as connection:
                current = self._load_for_update(connection, workspace_id, scan_id)
                database_failed_at = _database_transition_time(
                    connection,
                    current.updated_at,
                )
                disposition = classify_semantic_change_scan_failure(
                    code,
                    attempt=current.attempts,
                    max_attempts=current.max_attempts,
                )
                if disposition.value == "retry":
                    if (
                        retry_at is None
                        or retain_until is not None
                        or retry_at
                        != failed_at + semantic_change_scan_retry_delay(current.attempts)
                    ):
                        raise _state_conflict("semantic scan retry request does not match policy")
                    database_retry_at = database_failed_at + semantic_change_scan_retry_delay(
                        current.attempts
                    )
                    database_retain_until = None
                else:
                    if retry_at is not None or terminal_retention is None:
                        raise _state_conflict("terminal semantic scan failure requires retention")
                    database_retry_at = None
                    database_retain_until = database_failed_at + terminal_retention
                changed = fail_semantic_change_scan(
                    current,
                    reconciler_id=reconciler_id,
                    lease_capability=lease_capability,
                    fencing_token=fencing_token,
                    code=code,
                    failed_at=database_failed_at,
                    retry_at=database_retry_at,
                    retain_until=database_retain_until,
                )
                self._write_state(connection, current, changed)
                return changed
        except SemanticChangeScanStoreError:
            raise
        except (UndefinedTable, UndefinedColumn) as error:
            raise _schema_mismatch() from error
        except SemanticChangeScanTransitionError as error:
            raise _transition_error(error) from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response("semantic scan failure response was invalid") from error
        except psycopg.Error as error:
            raise _store_unavailable("semantic scan failure transition failed") from error
        except Exception as error:
            raise _store_unavailable("semantic scan failure transition failed") from error

    def _load_for_update(
        self,
        connection: psycopg.Connection[Any],
        workspace_id: str,
        scan_id: str,
    ) -> SemanticChangeScanRequest:
        row = connection.execute(
            sql.SQL(
                """
                SELECT {columns}
                FROM {table}
                WHERE workspace_id = %s
                  AND scan_id = %s
                FOR UPDATE
                """
            ).format(
                columns=_SCAN_COLUMNS,
                table=self._database.table("semantic_change_scan_requests"),
            ),
            (workspace_id, scan_id),
        ).fetchone()
        if row is None:
            raise SemanticChangeScanStoreError(
                SemanticChangeScanStoreErrorCode.NOT_FOUND,
                "semantic scan is unavailable",
            )
        return _scan_from_row(row)

    def _newer_successor(
        self,
        connection: psycopg.Connection[Any],
        current: SemanticChangeScanRequest,
    ) -> SemanticChangeScanSupersession | None:
        table = self._database.table("semantic_change_scan_requests")
        row = connection.execute(
            sql.SQL(
                """
                SELECT scan_id, source_fingerprint
                FROM {table}
                WHERE workspace_id = %s
                  AND source_kind = %s
                  AND scan_id <> %s
                  AND (
                      (
                          %s = 'catalog_generation'
                          AND connection_id = %s
                          AND catalog_scope = %s
                          AND registry_id = %s
                          AND observed_catalog_generation > %s
                      )
                      OR
                      (
                          %s = 'registry_pointer'
                          AND catalog_scope = %s
                          AND registry_id = %s
                          AND registry_generation > %s
                      )
                  )
                ORDER BY
                    CASE
                        WHEN source_kind = 'catalog_generation'
                        THEN observed_catalog_generation
                        ELSE registry_generation
                    END DESC,
                    requested_at DESC,
                    scan_id DESC
                LIMIT 1
                """
            ).format(table=table),
            (
                current.workspace_id,
                current.source_kind.value,
                current.scan_id,
                current.source_kind.value,
                current.connection_id,
                current.catalog_scope,
                current.registry_id,
                current.observed_catalog_generation,
                current.source_kind.value,
                current.catalog_scope,
                current.registry_id,
                current.registry_generation,
            ),
        ).fetchone()
        if row is None:
            return None
        return SemanticChangeScanSupersession(
            scan_id=str(row[0]),
            source_fingerprint=str(row[1]),
        )

    def _write_state(
        self,
        connection: psycopg.Connection[Any],
        current: SemanticChangeScanRequest,
        changed: SemanticChangeScanRequest,
    ) -> None:
        if (
            changed.scan_id != current.scan_id
            or changed.workspace_id != current.workspace_id
            or changed.source_fingerprint != current.source_fingerprint
        ):
            raise _state_conflict("semantic scan identity changed during transition")
        lease = changed.lease
        completion = changed.completion
        supersession = changed.superseded_by
        updated = connection.execute(
            sql.SQL(
                """
                UPDATE {table}
                SET status = %s,
                    attempts = %s,
                    available_at = %s,
                    lease_owner_id = %s,
                    lease_capability_digest = %s,
                    fencing_token = %s,
                    lease_acquired_at = %s,
                    lease_heartbeat_at = %s,
                    lease_expires_at = %s,
                    last_reason_code = %s,
                    updated_at = %s,
                    completed_at = %s,
                    completed_report_id = %s,
                    completed_report_fingerprint = %s,
                    superseded_by_scan_id = %s,
                    superseded_by_scan_fingerprint = %s,
                    retain_until = %s
                WHERE workspace_id = %s
                  AND scan_id = %s
                  AND status = %s
                  AND attempts = %s
                  AND fencing_token = %s
                  AND updated_at = %s
                """
            ).format(table=self._database.table("semantic_change_scan_requests")),
            (
                changed.status.value,
                changed.attempts,
                changed.available_at,
                None if lease is None else lease.reconciler_id,
                None if lease is None else lease.capability_digest,
                changed.fencing_token,
                None if lease is None else lease.acquired_at,
                None if lease is None else lease.heartbeat_at,
                None if lease is None else lease.expires_at,
                None if changed.failure is None else changed.failure.code.value,
                changed.updated_at,
                changed.completed_at,
                None if completion is None else completion.report_id,
                None if completion is None else completion.report_fingerprint,
                None if supersession is None else supersession.scan_id,
                None if supersession is None else supersession.source_fingerprint,
                changed.retain_until,
                current.workspace_id,
                current.scan_id,
                current.status.value,
                current.attempts,
                current.fencing_token,
                current.updated_at,
            ),
        )
        if updated.rowcount != 1:
            raise _state_conflict("semantic scan changed concurrently")

    def _load_exact_completion(
        self,
        connection: psycopg.Connection[Any],
        current: SemanticChangeScanRequest,
        inspection: SemanticChangeScanInspection,
    ) -> SemanticChangeScanRequest:
        completion = current.completion
        if (
            completion is None
            or completion.report_id != inspection.report.id
            or completion.report_fingerprint != inspection.report.fingerprint
        ):
            raise _state_conflict("semantic scan already completed with different evidence")
        stored_report = self.report_store._load_report(
            connection,
            inspection.report.context.scope,
            inspection.report.id,
        )
        stored_observation = self.report_store._load_observation(
            connection,
            inspection.report.context.scope,
            inspection.report.id,
        )
        if stored_report != inspection.report or stored_observation != inspection.observation:
            raise _state_conflict("semantic scan completion evidence is inconsistent")
        return current


def _scan_from_row(row: tuple[object, ...]) -> SemanticChangeScanRequest:
    if len(row) != len(_SCAN_COLUMN_NAMES):
        raise ValueError("semantic scan row has an invalid shape")
    values = dict(zip(_SCAN_COLUMN_NAMES, row, strict=True))
    status = SemanticChangeScanStatus(str(values["status"]))
    attempts = _integer(values["attempts"], "semantic scan attempts")
    max_attempts = _integer(values["max_attempts"], "semantic scan max attempts")
    updated_at = _datetime(values["updated_at"], "semantic scan update time")

    lease = None
    if status is SemanticChangeScanStatus.LEASED:
        lease = SemanticChangeScanLease(
            scan_id=str(values["scan_id"]),
            reconciler_id=_required_string(values["lease_owner_id"]),
            capability_digest=_required_string(values["lease_capability_digest"]),
            fencing_token=_integer(values["fencing_token"], "semantic scan fence"),
            attempt=attempts,
            acquired_at=_datetime(
                values["lease_acquired_at"],
                "semantic scan lease acquisition",
            ),
            heartbeat_at=_datetime(
                values["lease_heartbeat_at"],
                "semantic scan lease heartbeat",
            ),
            expires_at=_datetime(
                values["lease_expires_at"],
                "semantic scan lease expiry",
            ),
        )

    failure = None
    if status in {
        SemanticChangeScanStatus.RETRY_WAIT,
        SemanticChangeScanStatus.FAILED,
    }:
        code = SemanticChangeScanFailureCode(_required_string(values["last_reason_code"]))
        failure = SemanticChangeScanFailure(
            code=code,
            disposition=classify_semantic_change_scan_failure(
                code,
                attempt=attempts,
                max_attempts=max_attempts,
            ),
            attempt=attempts,
            occurred_at=updated_at,
        )

    completion = None
    if status is SemanticChangeScanStatus.COMPLETED:
        completion = SemanticChangeScanCompletion(
            report_id=_required_string(values["completed_report_id"]),
            report_fingerprint=_required_string(values["completed_report_fingerprint"]),
            completed_at=_datetime(
                values["completed_at"],
                "semantic scan completion time",
            ),
        )

    supersession = None
    if status is SemanticChangeScanStatus.SUPERSEDED:
        supersession = SemanticChangeScanSupersession(
            scan_id=_required_string(values["superseded_by_scan_id"]),
            source_fingerprint=_required_string(values["superseded_by_scan_fingerprint"]),
        )

    return SemanticChangeScanRequest(
        scan_id=str(values["scan_id"]),
        workspace_id=str(values["workspace_id"]),
        source_kind=SemanticChangeScanSourceKind(str(values["source_kind"])),
        source_event_key=str(values["source_event_key"]),
        source_fingerprint=str(values["source_fingerprint"]),
        catalog_scope=_optional_string(values["catalog_scope"]),
        registry_id=_optional_string(values["registry_id"]),
        registry_generation=_optional_integer(
            values["registry_generation"],
            "semantic scan registry generation",
        ),
        connection_id=_optional_string(values["connection_id"]),
        base_catalog_generation=_optional_integer(
            values["base_catalog_generation"],
            "semantic scan base catalog generation",
        ),
        observed_catalog_generation=_optional_integer(
            values["observed_catalog_generation"],
            "semantic scan observed catalog generation",
        ),
        status=status,
        max_attempts=max_attempts,
        attempts=attempts,
        available_at=_datetime(values["available_at"], "semantic scan availability"),
        fencing_token=_integer(values["fencing_token"], "semantic scan fence"),
        lease=lease,
        failure=failure,
        requested_at=_datetime(values["requested_at"], "semantic scan request time"),
        updated_at=updated_at,
        completed_at=_optional_datetime(
            values["completed_at"],
            "semantic scan terminal time",
        ),
        retain_until=_optional_datetime(
            values["retain_until"],
            "semantic scan retention time",
        ),
        completion=completion,
        superseded_by=supersession,
    )


def _inspection(value: object) -> SemanticChangeScanInspection:
    if not isinstance(value, SemanticChangeScanInspection):
        raise ValueError("semantic scan inspection is invalid")
    checked = SemanticChangeScanInspection(
        report=SemanticChangeReport.model_validate(
            value.report.model_dump(mode="python", warnings=False)
        ),
        observation=SemanticEvidenceObservation.model_validate(
            value.observation.model_dump(mode="python", warnings=False)
        ),
    )
    if (
        checked != value
        or checked.report.catalog_generations != checked.observation.catalog_generations
    ):
        raise ValueError("semantic scan inspection is non-canonical")
    return checked


def _inspection_matches_request(
    request: SemanticChangeScanRequest,
    inspection: SemanticChangeScanInspection,
) -> bool:
    report = inspection.report
    if report.context.scope.workspace_id != request.workspace_id:
        return False
    if request.source_kind is SemanticChangeScanSourceKind.REGISTRY_POINTER:
        return (
            report.context.scope.catalog_scope == request.catalog_scope
            and report.context.scope.registry_id == request.registry_id
            and report.context.pointer_generation == request.registry_generation
        )
    return any(
        item.connection_id.root == request.connection_id
        and item.generation == request.observed_catalog_generation
        for item in report.catalog_generations.observations
    )


def _qualified_scan_columns(alias: str) -> sql.Composed:
    return sql.SQL(", ").join(
        sql.SQL("{}.{}").format(sql.Identifier(alias), sql.Identifier(name))
        for name in _SCAN_COLUMN_NAMES
    )


def _database_transition_time(
    connection: psycopg.Connection[Any],
    previous: datetime,
) -> datetime:
    row = connection.execute(
        """
        SELECT GREATEST(
            clock_timestamp(),
            %s::timestamptz + interval '1 microsecond'
        )
        """,
        (previous,),
    ).fetchone()
    if row is None:
        raise ValueError("semantic scan database clock is unavailable")
    return _datetime(row[0], "semantic scan database time")


def _lease_inputs(
    workspace_id: str,
    scan_id: str,
    reconciler_id: str,
    lease_capability: str,
    fencing_token: int,
    lease_duration: timedelta,
) -> timedelta:
    _lease_identity(
        workspace_id,
        scan_id,
        reconciler_id,
        lease_capability,
        fencing_token,
    )
    return _lease_duration(lease_duration)


def _lease_identity(
    workspace_id: str,
    scan_id: str,
    reconciler_id: str,
    lease_capability: str,
    fencing_token: int,
) -> None:
    _workspace_id(workspace_id)
    _scan_id(scan_id)
    _reconciler_id(reconciler_id)
    digest_semantic_change_scan_capability(lease_capability)
    if isinstance(fencing_token, bool) or not 1 <= fencing_token <= 100:
        raise ValueError("semantic scan fencing token is outside the supported bound")


def _workspace_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode()) > 200
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("semantic scan workspace is invalid")
    return value


def _scan_id(value: str) -> str:
    if not isinstance(value, str) or _SCAN_ID.fullmatch(value) is None:
        raise ValueError("semantic scan id is invalid")
    return value


def _reconciler_id(value: str) -> str:
    if not isinstance(value, str) or _RECONCILER_ID.fullmatch(value) is None:
        raise ValueError("semantic reconciler identifier is invalid")
    return value


def _lease_duration(value: timedelta) -> timedelta:
    if not _MIN_LEASE_DURATION <= value <= _MAX_LEASE_DURATION:
        raise ValueError("semantic scan lease duration is outside the supported bound")
    return value


def _retention(value: timedelta) -> timedelta:
    if not _MIN_RETENTION <= value <= _MAX_RETENTION:
        raise ValueError("semantic scan retention is outside the supported bound")
    return value


def _retention_window(start: datetime, end: datetime) -> timedelta:
    beginning = _aware(start, "semantic scan lifecycle time")
    finish = _aware(end, "semantic scan retention deadline")
    return _retention(finish - beginning)


def _maintenance_limit(value: int) -> int:
    if isinstance(value, bool) or not 1 <= value <= _MAX_MAINTENANCE_BATCH:
        raise ValueError("semantic scan maintenance limit is outside the supported bound")
    return value


def _aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


def _datetime(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} is invalid")
    return value


def _optional_datetime(value: object, label: str) -> datetime | None:
    return None if value is None else _datetime(value, label)


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} is invalid")
    return value


def _optional_integer(value: object, label: str) -> int | None:
    return None if value is None else _integer(value, label)


def _required_string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("semantic scan string value is invalid")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return _required_string(value)


def _transition_error(
    error: SemanticChangeScanTransitionError,
) -> SemanticChangeScanStoreError:
    if error.code in {
        SemanticChangeScanTransitionErrorCode.INVALID_STATE,
        SemanticChangeScanTransitionErrorCode.LEASE_MISMATCH,
        SemanticChangeScanTransitionErrorCode.LEASE_EXPIRED,
        SemanticChangeScanTransitionErrorCode.FENCING_MISMATCH,
    }:
        return _lease_lost()
    return _state_conflict("semantic scan transition conflicted")


def _lease_lost() -> SemanticChangeScanStoreError:
    return SemanticChangeScanStoreError(
        SemanticChangeScanStoreErrorCode.LEASE_LOST,
        "semantic scan lease is unavailable",
    )


def _state_conflict(message: str) -> SemanticChangeScanStoreError:
    return SemanticChangeScanStoreError(
        SemanticChangeScanStoreErrorCode.STATE_CONFLICT,
        message,
    )


def _schema_mismatch() -> SemanticChangeScanStoreError:
    return SemanticChangeScanStoreError(
        SemanticChangeScanStoreErrorCode.SCHEMA_MISMATCH,
        "semantic scan schema is unavailable",
    )


def _invalid_response(message: str) -> SemanticChangeScanStoreError:
    return SemanticChangeScanStoreError(
        SemanticChangeScanStoreErrorCode.INVALID_RESPONSE,
        message,
    )


def _store_unavailable(message: str) -> SemanticChangeScanStoreError:
    return SemanticChangeScanStoreError(
        SemanticChangeScanStoreErrorCode.STORE_UNAVAILABLE,
        message,
    )


__all__ = ["PostgresSemanticChangeScanStore"]
