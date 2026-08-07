"""Durable PostgreSQL refresh generations for the catalog indexer.

The adapter persists metadata only.  It never connects to a source database and
never stores source rows, samples, credentials, or the raw lease capability.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.errors import (
    CheckViolation,
    ForeignKeyViolation,
    SerializationFailure,
    UniqueViolation,
)
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from schemabridge.adapters.storage.postgres import (
    ControlConnectionProvider,
    _ControlDatabase,
)
from schemabridge.application.ports.catalog_inventory import (
    CatalogInventoryError,
    CatalogInventoryErrorCode,
)
from schemabridge.domain.catalog_inventory import (
    CATALOG_INDEXER_ID_PATTERN,
    CatalogAssetId,
    CatalogConnectionId,
    CatalogRefreshCommand,
    CatalogRefreshFailureCode,
    CatalogRefreshId,
    CatalogRefreshLease,
    CatalogRefreshMode,
    CatalogRefreshRequestResult,
    CatalogRefreshState,
    CatalogRefreshStatus,
    CatalogRefreshSummary,
    CatalogSourceAsset,
    CatalogSourceChange,
    CatalogSourceChangeKind,
    CatalogSourceField,
    CatalogSourcePage,
)
from schemabridge.domain.connectors import MAX_ROUTE_REVISION

_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,199}$")
_SAFE_INDEXER_ID = re.compile(CATALOG_INDEXER_ID_PATTERN)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MIN_LEASE_SECONDS = 10
_MAX_LEASE_SECONDS = 300
_MAX_BATCH = 100
_MAX_FULL_FIELD_INSERT_BATCH = 500
_FINGERPRINT_FETCH_SIZE = 1_000
_FINGERPRINT_PREFIX = b"schemabridge-catalog-generation-v2\x00"
_CAPACITY_EXCEEDED_SQLSTATE = "P2501"

_REFRESH_COLUMN_NAMES = (
    "refresh_id",
    "workspace_id",
    "connection_id",
    "refresh_mode",
    "status",
    "base_generation",
    "target_generation",
    "idempotency_digest",
    "requested_by_actor_id",
    "requested_at",
    "updated_at",
    "lease_owner_id",
    "lease_capability_digest",
    "fencing_token",
    "lease_heartbeat_at",
    "lease_expires_at",
    "source_page_number",
    "source_page_fingerprint",
    "staged_asset_count",
    "staged_field_count",
    "source_complete",
    "source_checkpoint",
    "inventory_fingerprint",
    "failure_code",
    "completed_at",
)
_REFRESH_COLUMNS = sql.SQL(", ").join(map(sql.Identifier, _REFRESH_COLUMN_NAMES))


@dataclass(frozen=True, slots=True)
class PostgresCatalogRefreshStore:
    """Catalog-indexer-only refresh request, staging, and promotion store."""

    dsn: str = field(repr=False)
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-catalog"
    connection_provider: ControlConnectionProvider | None = field(default=None, repr=False)
    _database: _ControlDatabase = field(init=False, repr=False)

    def __post_init__(self) -> None:
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

    @property
    def _db(self) -> _ControlDatabase:
        return self._database

    def request(self, command: CatalogRefreshCommand) -> CatalogRefreshRequestResult:
        """Create a request or return only an exact idempotent replay."""

        request_fingerprint = _request_fingerprint(command)
        refresh_id = f"refresh-{uuid4().hex}"
        function = sql.SQL("{}.request_catalog_refresh").format(sql.Identifier(self.schema))
        try:
            with self._db.connect() as connection, connection.transaction():
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT *
                        FROM {}(%s, %s, %s, %s, %s, %s, %s, %s)
                        """
                    ).format(function),
                    (
                        command.workspace_id,
                        command.connection_id.root,
                        refresh_id,
                        command.mode.value,
                        request_fingerprint,
                        command.idempotency_digest,
                        command.requested_by,
                        command.requested_at,
                    ),
                ).fetchone()
                if row is None:
                    raise _refresh_conflict()
                if row[0] != request_fingerprint:
                    raise _idempotency_conflict()
                if type(row[1]) is not bool:
                    raise _invalid_response()
                return CatalogRefreshRequestResult(
                    refresh=_public_refresh_from_row(row[2:]),
                    replayed=row[1],
                )
        except CatalogInventoryError:
            raise
        except UniqueViolation as error:
            raise _refresh_conflict() from error
        except (CheckViolation, ForeignKeyViolation) as error:
            raise _invalid_response() from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def load_public(
        self,
        workspace_id: str,
        refresh_id: CatalogRefreshId,
    ) -> CatalogRefreshSummary | None:
        """Load the public projection without checkpoint or lease access."""

        _workspace_id(workspace_id)
        function = sql.SQL("{}.load_catalog_refresh_public").format(sql.Identifier(self.schema))
        try:
            with self._db.connect() as connection:
                row = connection.execute(
                    sql.SQL("SELECT * FROM {}(%s, %s)").format(function),
                    (workspace_id, refresh_id.root),
                ).fetchone()
            return None if row is None else _public_refresh_from_row(row)
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def load(
        self,
        workspace_id: str,
        refresh_id: CatalogRefreshId,
    ) -> CatalogRefreshState | None:
        """Load one tenant-bound refresh without a global identifier lookup."""

        _workspace_id(workspace_id)
        refreshes = self._db.table("catalog_refresh_runs")
        try:
            with self._db.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT {columns}
                        FROM {refreshes}
                        WHERE workspace_id = %s
                          AND refresh_id = %s
                        """
                    ).format(columns=_REFRESH_COLUMNS, refreshes=refreshes),
                    (workspace_id, refresh_id.root),
                ).fetchone()
            return None if row is None else _refresh_from_row(row)
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def claim_next(
        self,
        *,
        indexer_id: str,
        lease_capability: str,
        lease_duration: timedelta,
    ) -> CatalogRefreshState | None:
        """Claim the oldest enabled request with a fresh capability digest and fence."""

        _indexer_id(indexer_id)
        capability_digest = _capability_digest(lease_capability)
        lease_seconds = _lease_seconds(lease_duration)
        refreshes = self._db.table("catalog_refresh_runs")
        connections = self._db.table("catalog_connections")
        try:
            with self._db.connect() as connection, connection.transaction():
                row = connection.execute(
                    sql.SQL(
                        """
                        WITH candidate AS (
                            SELECT refresh.workspace_id,
                                   refresh.connection_id,
                                   refresh.refresh_id
                            FROM {refreshes} AS refresh
                            JOIN {connections} AS catalog_connection
                              ON catalog_connection.workspace_id = refresh.workspace_id
                             AND catalog_connection.connection_id = refresh.connection_id
                            WHERE refresh.status = 'requested'
                              AND catalog_connection.status = 'enabled'
                            ORDER BY refresh.requested_at,
                                     refresh.workspace_id,
                                     refresh.connection_id,
                                     refresh.refresh_id
                            FOR UPDATE OF refresh SKIP LOCKED
                            LIMIT 1
                        ),
                        observed AS (
                            SELECT clock_timestamp() AS claimed_at
                        )
                        UPDATE {refreshes} AS refresh
                        SET status = 'leased',
                            lease_owner_id = %s,
                            lease_capability_digest = %s,
                            fencing_token = refresh.fencing_token + 1,
                            lease_acquired_at = observed.claimed_at,
                            lease_heartbeat_at = observed.claimed_at,
                            lease_expires_at = observed.claimed_at
                                + make_interval(secs => %s),
                            updated_at = observed.claimed_at
                        FROM candidate, observed
                        WHERE refresh.workspace_id = candidate.workspace_id
                          AND refresh.connection_id = candidate.connection_id
                          AND refresh.refresh_id = candidate.refresh_id
                          AND refresh.status = 'requested'
                        RETURNING {columns}
                        """
                    ).format(
                        refreshes=refreshes,
                        connections=connections,
                        columns=_qualified_refresh_columns("refresh"),
                    ),
                    (indexer_id, capability_digest, lease_seconds),
                ).fetchone()
            return None if row is None else _refresh_from_row(row)
        except CatalogInventoryError:
            raise
        except SerializationFailure as error:
            raise _lease_conflict() from error
        except (CheckViolation, ForeignKeyViolation, UniqueViolation) as error:
            raise _lease_conflict() from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def begin_staging(
        self,
        workspace_id: str,
        refresh_id: CatalogRefreshId,
        *,
        indexer_id: str,
        lease_capability: str,
        fencing_token: int,
    ) -> CatalogRefreshState:
        """Create the invisible generation and clone a delta base entirely in SQL."""

        _workspace_id(workspace_id)
        _indexer_id(indexer_id)
        capability_digest = _capability_digest(lease_capability)
        _fencing_token(fencing_token)
        try:
            with self._db.connect() as connection, connection.transaction():
                state, observed_at = self._load_for_update_at(
                    connection,
                    workspace_id,
                    refresh_id,
                )
                _require_owned(
                    state,
                    indexer_id=indexer_id,
                    capability_digest=capability_digest,
                    fencing_token=fencing_token,
                    observed_at=observed_at,
                    statuses={
                        CatalogRefreshStatus.LEASED,
                        CatalogRefreshStatus.STAGING,
                    },
                )
                if state.status is CatalogRefreshStatus.STAGING:
                    return state
                inserted = self._insert_generation(connection, state, observed_at)
                if inserted and state.mode is CatalogRefreshMode.DELTA:
                    if state.base_generation == 0:
                        raise _delta_unsupported()
                    self._clone_base_generation(connection, state)
                refreshes = self._db.table("catalog_refresh_runs")
                row = connection.execute(
                    sql.SQL(
                        """
                        UPDATE {refreshes}
                        SET status = 'staging',
                            updated_at = clock_timestamp()
                        WHERE workspace_id = %s
                          AND connection_id = %s
                          AND refresh_id = %s
                          AND status = 'leased'
                          AND lease_owner_id = %s
                          AND lease_capability_digest = %s
                          AND fencing_token = %s
                          AND lease_expires_at > clock_timestamp()
                        RETURNING {columns}
                        """
                    ).format(
                        refreshes=refreshes,
                        columns=_qualified_refresh_columns("catalog_refresh_runs"),
                    ),
                    (
                        state.workspace_id,
                        state.connection_id.root,
                        state.refresh_id.root,
                        indexer_id,
                        capability_digest,
                        fencing_token,
                    ),
                ).fetchone()
                if row is None:
                    raise _lease_conflict()
                return _refresh_from_row(row)
        except CatalogInventoryError:
            raise
        except SerializationFailure as error:
            raise _lease_conflict() from error
        except (CheckViolation, ForeignKeyViolation, UniqueViolation) as error:
            raise _refresh_conflict() from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def persist_page(
        self,
        workspace_id: str,
        refresh_id: CatalogRefreshId,
        *,
        indexer_id: str,
        lease_capability: str,
        fencing_token: int,
        page: CatalogSourcePage,
    ) -> CatalogRefreshState:
        """Apply one bounded source page and its checkpoint atomically."""

        _workspace_id(workspace_id)
        _indexer_id(indexer_id)
        capability_digest = _capability_digest(lease_capability)
        _fencing_token(fencing_token)
        try:
            with self._db.connect() as connection, connection.transaction():
                state, observed_at = self._load_for_update_at(
                    connection,
                    workspace_id,
                    refresh_id,
                )
                _require_owned(
                    state,
                    indexer_id=indexer_id,
                    capability_digest=capability_digest,
                    fencing_token=fencing_token,
                    observed_at=observed_at,
                    statuses={CatalogRefreshStatus.STAGING},
                )
                if page.mode is not state.mode:
                    raise _invalid_response()
                if page.sequence == state.source_page_count:
                    if (
                        state.source_page_fingerprint != page.page_fingerprint
                        or state.source_checkpoint != page.next_checkpoint
                        or state.source_complete is not page.source_complete
                    ):
                        raise _refresh_conflict()
                    return state
                if page.sequence != state.source_page_count + 1 or state.source_complete:
                    raise _refresh_conflict()

                if page.mode is CatalogRefreshMode.FULL:
                    assets = tuple(
                        change.asset for change in page.changes if change.asset is not None
                    )
                    if len(assets) != len(page.changes):
                        raise _invalid_response()
                    self._upsert_full_asset_page(
                        connection,
                        state=state,
                        assets=assets,
                        observed_at=observed_at,
                    )
                    asset_count = state.staged_asset_count + len(assets)
                    field_count = state.staged_field_count + sum(
                        len(asset.fields) for asset in assets
                    )
                else:
                    for change in page.changes:
                        self._apply_change(
                            connection,
                            state=state,
                            change=change,
                            observed_at=observed_at,
                        )
                    asset_count, field_count = self._generation_counts(connection, state)
                refreshes = self._db.table("catalog_refresh_runs")
                row = connection.execute(
                    sql.SQL(
                        """
                        UPDATE {refreshes}
                        SET source_checkpoint = %s,
                            source_page_number = %s,
                            source_page_fingerprint = %s,
                            staged_asset_count = %s,
                            staged_field_count = %s,
                            source_complete = %s,
                            updated_at = clock_timestamp()
                        WHERE workspace_id = %s
                          AND connection_id = %s
                          AND refresh_id = %s
                          AND status = 'staging'
                          AND lease_owner_id = %s
                          AND lease_capability_digest = %s
                          AND fencing_token = %s
                          AND lease_expires_at > clock_timestamp()
                          AND source_page_number = %s
                        RETURNING {columns}
                        """
                    ).format(
                        refreshes=refreshes,
                        columns=_qualified_refresh_columns("catalog_refresh_runs"),
                    ),
                    (
                        page.next_checkpoint,
                        page.sequence,
                        page.page_fingerprint,
                        asset_count,
                        field_count,
                        page.source_complete,
                        state.workspace_id,
                        state.connection_id.root,
                        state.refresh_id.root,
                        indexer_id,
                        capability_digest,
                        fencing_token,
                        state.source_page_count,
                    ),
                ).fetchone()
                if row is None:
                    raise _lease_conflict()
                return _refresh_from_row(row)
        except CatalogInventoryError:
            raise
        except SerializationFailure as error:
            raise _lease_conflict() from error
        except (CheckViolation, ForeignKeyViolation, UniqueViolation) as error:
            raise _invalid_response() from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def heartbeat(
        self,
        workspace_id: str,
        refresh_id: CatalogRefreshId,
        *,
        indexer_id: str,
        lease_capability: str,
        fencing_token: int,
        lease_duration: timedelta,
    ) -> CatalogRefreshState:
        """Renew only the exact unexpired capability and monotonic fence."""

        _workspace_id(workspace_id)
        _indexer_id(indexer_id)
        capability_digest = _capability_digest(lease_capability)
        _fencing_token(fencing_token)
        lease_seconds = _lease_seconds(lease_duration)
        refreshes = self._db.table("catalog_refresh_runs")
        try:
            with self._db.connect() as connection, connection.transaction():
                row = connection.execute(
                    sql.SQL(
                        """
                        WITH observed AS (
                            SELECT clock_timestamp() AS heartbeat_at
                        )
                        UPDATE {refreshes} AS refresh
                        SET lease_heartbeat_at = observed.heartbeat_at,
                            lease_expires_at = greatest(
                                refresh.lease_expires_at,
                                observed.heartbeat_at + make_interval(secs => %s)
                            ),
                            updated_at = observed.heartbeat_at
                        FROM observed
                        WHERE refresh.workspace_id = %s
                          AND refresh.refresh_id = %s
                          AND refresh.status IN ('leased', 'staging')
                          AND refresh.lease_owner_id = %s
                          AND refresh.lease_capability_digest = %s
                          AND refresh.fencing_token = %s
                          AND refresh.lease_expires_at > observed.heartbeat_at
                        RETURNING {columns}
                        """
                    ).format(
                        refreshes=refreshes,
                        columns=_qualified_refresh_columns("refresh"),
                    ),
                    (
                        lease_seconds,
                        workspace_id,
                        refresh_id.root,
                        indexer_id,
                        capability_digest,
                        fencing_token,
                    ),
                ).fetchone()
                if row is None:
                    raise _lease_conflict()
                return _refresh_from_row(row)
        except CatalogInventoryError:
            raise
        except SerializationFailure as error:
            raise _lease_conflict() from error
        except (CheckViolation, ForeignKeyViolation, UniqueViolation) as error:
            raise _lease_conflict() from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def complete(
        self,
        workspace_id: str,
        refresh_id: CatalogRefreshId,
        *,
        indexer_id: str,
        lease_capability: str,
        fencing_token: int,
        expected_base_generation: int,
        expected_contract_version: int,
        expected_route_revision: int,
        expected_target_fingerprint: str,
    ) -> CatalogRefreshState:
        """Promote while holding the exact governed connector head stable."""

        _workspace_id(workspace_id)
        _indexer_id(indexer_id)
        capability_digest = _capability_digest(lease_capability)
        _fencing_token(fencing_token)
        if isinstance(expected_base_generation, bool) or expected_base_generation < 0:
            raise ValueError("catalog base generation is invalid")
        if (
            type(expected_contract_version) is not int
            or not 1 <= expected_contract_version <= MAX_ROUTE_REVISION
            or type(expected_route_revision) is not int
            or not 1 <= expected_route_revision <= MAX_ROUTE_REVISION
            or not isinstance(expected_target_fingerprint, str)
            or _SHA256.fullmatch(expected_target_fingerprint) is None
        ):
            raise ValueError("catalog connector target is invalid")
        try:
            with self._db.connect() as connection, connection.transaction():
                locator = self._load_locator(connection, workspace_id, refresh_id)
                self._lock_completion_scope(
                    connection,
                    workspace_id=workspace_id,
                    connection_id=locator[0],
                    refresh_id=refresh_id,
                    target_generation=locator[1],
                    fencing_token=fencing_token,
                    capability_digest=capability_digest,
                )
                state, observed_at = self._load_for_update_at(
                    connection,
                    workspace_id,
                    refresh_id,
                )
                _require_owned(
                    state,
                    indexer_id=indexer_id,
                    capability_digest=capability_digest,
                    fencing_token=fencing_token,
                    observed_at=observed_at,
                    statuses={CatalogRefreshStatus.STAGING},
                )
                if state.base_generation != expected_base_generation or not state.source_complete:
                    raise _refresh_conflict()
                inventory_fingerprint = self._stream_generation_fingerprint(
                    connection,
                    state,
                )
                function = sql.SQL("{}.activate_catalog_generation_for_target").format(
                    sql.Identifier(self.schema)
                )
                connection.execute(
                    sql.SQL(
                        """
                        SELECT *
                        FROM {}(
                            %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s
                        )
                        """
                    ).format(function),
                    (
                        state.workspace_id,
                        state.connection_id.root,
                        state.refresh_id.root,
                        indexer_id,
                        lease_capability,
                        _physical_base_generation(state.base_generation),
                        state.target_generation,
                        fencing_token,
                        inventory_fingerprint,
                        expected_contract_version,
                        expected_route_revision,
                        expected_target_fingerprint,
                    ),
                ).fetchone()
                completed = self._load_for_update(
                    connection,
                    workspace_id,
                    refresh_id,
                )
                if (
                    completed.status is not CatalogRefreshStatus.COMPLETED
                    or completed.catalog_fingerprint != inventory_fingerprint
                ):
                    raise _invalid_response()
                return completed
        except CatalogInventoryError:
            raise
        except SerializationFailure as error:
            raise _lease_conflict() from error
        except (CheckViolation, ForeignKeyViolation, UniqueViolation) as error:
            raise _refresh_conflict() from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            if error.sqlstate == "40001":
                raise _lease_conflict() from error
            if error.sqlstate == _CAPACITY_EXCEEDED_SQLSTATE:
                raise _capacity_exceeded() from error
            raise _unavailable() from error

    def fail(
        self,
        workspace_id: str,
        refresh_id: CatalogRefreshId,
        *,
        indexer_id: str,
        lease_capability: str,
        fencing_token: int,
        code: CatalogRefreshFailureCode,
    ) -> CatalogRefreshState:
        """Persist one sanitized terminal failure for the exact lease."""

        _workspace_id(workspace_id)
        _indexer_id(indexer_id)
        capability_digest = _capability_digest(lease_capability)
        _fencing_token(fencing_token)
        try:
            with self._db.connect() as connection, connection.transaction():
                state, observed_at = self._load_for_update_at(
                    connection,
                    workspace_id,
                    refresh_id,
                )
                if state.status is CatalogRefreshStatus.FAILED:
                    if state.failure_code is code and state.lease is None:
                        return state
                    raise _refresh_conflict()
                _require_owned(
                    state,
                    indexer_id=indexer_id,
                    capability_digest=capability_digest,
                    fencing_token=fencing_token,
                    observed_at=observed_at,
                    statuses={
                        CatalogRefreshStatus.LEASED,
                        CatalogRefreshStatus.STAGING,
                    },
                )
                refreshes = self._db.table("catalog_refresh_runs")
                row = connection.execute(
                    sql.SQL(
                        """
                        UPDATE {refreshes}
                        SET status = 'failed',
                            lease_capability_digest = NULL,
                            lease_expires_at = NULL,
                            failure_code = %s,
                            updated_at = clock_timestamp(),
                            completed_at = clock_timestamp()
                        WHERE workspace_id = %s
                          AND connection_id = %s
                          AND refresh_id = %s
                          AND status IN ('leased', 'staging')
                          AND lease_owner_id = %s
                          AND lease_capability_digest = %s
                          AND fencing_token = %s
                        RETURNING {columns}
                        """
                    ).format(
                        refreshes=refreshes,
                        columns=_qualified_refresh_columns("catalog_refresh_runs"),
                    ),
                    (
                        code.value,
                        state.workspace_id,
                        state.connection_id.root,
                        state.refresh_id.root,
                        indexer_id,
                        capability_digest,
                        fencing_token,
                    ),
                ).fetchone()
                if row is None:
                    raise _lease_conflict()
                return _refresh_from_row(row)
        except CatalogInventoryError:
            raise
        except SerializationFailure as error:
            raise _lease_conflict() from error
        except (CheckViolation, ForeignKeyViolation, UniqueViolation) as error:
            raise _refresh_conflict() from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def reclaim_expired(self, *, limit: int = 100) -> int:
        """Return a bounded locked batch of expired leases to requested."""

        _batch_limit(limit)
        refreshes = self._db.table("catalog_refresh_runs")
        try:
            with self._db.connect() as connection, connection.transaction():
                row = connection.execute(
                    sql.SQL(
                        """
                        WITH expired AS (
                            SELECT workspace_id, connection_id, refresh_id
                            FROM {refreshes}
                            WHERE status IN ('leased', 'staging')
                              AND lease_expires_at <= clock_timestamp()
                            ORDER BY lease_expires_at,
                                     workspace_id,
                                     connection_id,
                                     refresh_id
                            FOR UPDATE SKIP LOCKED
                            LIMIT %s
                        )
                        UPDATE {refreshes} AS refresh
                        SET status = 'requested',
                            lease_owner_id = NULL,
                            lease_capability_digest = NULL,
                            lease_acquired_at = NULL,
                            lease_heartbeat_at = NULL,
                            lease_expires_at = NULL,
                            updated_at = clock_timestamp()
                        FROM expired
                        WHERE refresh.workspace_id = expired.workspace_id
                          AND refresh.connection_id = expired.connection_id
                          AND refresh.refresh_id = expired.refresh_id
                        RETURNING 1
                        """
                    ).format(refreshes=refreshes),
                    (limit,),
                ).fetchall()
                return len(row)
        except (CheckViolation, ForeignKeyViolation, UniqueViolation) as error:
            raise _refresh_conflict() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def prune_generations(
        self,
        workspace_id: str,
        connection_id: CatalogConnectionId,
        *,
        completed_before: datetime,
        limit: int = 100,
    ) -> int:
        """Prune only inactive generations satisfying retention and caller cutoff."""

        _workspace_id(workspace_id)
        _aware(completed_before, "catalog prune cutoff")
        _batch_limit(limit)
        function = sql.SQL("{}.prune_catalog_generations").format(sql.Identifier(self.schema))
        try:
            with self._db.connect() as connection, connection.transaction():
                row = connection.execute(
                    sql.SQL("SELECT {}(%s, %s, %s, %s)").format(function),
                    (
                        workspace_id,
                        connection_id.root,
                        completed_before,
                        limit,
                    ),
                ).fetchone()
            if row is None or not isinstance(row[0], int):
                raise _invalid_response()
            return row[0]
        except CatalogInventoryError:
            raise
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def prune_due_generations(self, *, limit: int = 100) -> int:
        """Prune at most one bounded batch from the oldest eligible connection."""

        _batch_limit(limit)
        generations = self._db.table("catalog_generations")
        connections = self._db.table("catalog_connections")
        refreshes = self._db.table("catalog_refresh_runs")
        function = sql.SQL("{}.prune_catalog_generations").format(sql.Identifier(self.schema))
        try:
            with self._db.connect() as connection, connection.transaction():
                candidate = connection.execute(
                    sql.SQL(
                        """
                        SELECT generation.workspace_id,
                               generation.connection_id
                        FROM {generations} AS generation
                        JOIN {connections} AS catalog_connection
                          ON catalog_connection.workspace_id = generation.workspace_id
                         AND catalog_connection.connection_id = generation.connection_id
                        WHERE generation.generation
                                  IS DISTINCT FROM catalog_connection.active_generation
                          AND (
                              (
                                  generation.status = 'completed'
                                  AND generation.completed_at < clock_timestamp()
                                  AND generation.retain_until < clock_timestamp()
                              )
                              OR
                              (
                                  generation.status = 'staging'
                                  AND EXISTS (
                                      SELECT 1
                                      FROM {refreshes} AS failed_refresh
                                      WHERE failed_refresh.workspace_id
                                                = generation.workspace_id
                                        AND failed_refresh.connection_id
                                                = generation.connection_id
                                        AND failed_refresh.refresh_id
                                                = generation.refresh_id
                                        AND failed_refresh.status = 'failed'
                                        AND failed_refresh.completed_at < clock_timestamp()
                                  )
                              )
                          )
                          AND NOT EXISTS (
                              SELECT 1
                              FROM {refreshes} AS active_refresh
                              WHERE active_refresh.workspace_id
                                        = generation.workspace_id
                                AND active_refresh.connection_id
                                        = generation.connection_id
                                AND active_refresh.status
                                        IN ('requested', 'leased', 'staging')
                                AND (
                                    active_refresh.base_generation
                                        = generation.generation
                                    OR active_refresh.target_generation
                                        = generation.generation
                                )
                          )
                        ORDER BY generation.retain_until NULLS FIRST,
                                 generation.workspace_id,
                                 generation.connection_id,
                                 generation.generation
                        LIMIT 1
                        """
                    ).format(
                        generations=generations,
                        connections=connections,
                        refreshes=refreshes,
                    )
                ).fetchone()
                if candidate is None:
                    return 0
                row = connection.execute(
                    sql.SQL("SELECT {}(%s, %s, %s)").format(function),
                    (str(candidate[0]), str(candidate[1]), limit),
                ).fetchone()
            if row is None or type(row[0]) is not int or not 0 <= row[0] <= limit:
                raise _invalid_response()
            return row[0]
        except CatalogInventoryError:
            raise
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def _load_for_update(
        self,
        connection: psycopg.Connection[Any],
        workspace_id: str,
        refresh_id: CatalogRefreshId,
    ) -> CatalogRefreshState:
        refreshes = self._db.table("catalog_refresh_runs")
        row = connection.execute(
            sql.SQL(
                """
                SELECT {columns}
                FROM {refreshes}
                WHERE workspace_id = %s
                  AND refresh_id = %s
                FOR UPDATE
                """
            ).format(columns=_REFRESH_COLUMNS, refreshes=refreshes),
            (workspace_id, refresh_id.root),
        ).fetchone()
        if row is None:
            raise _resource_unavailable()
        return _refresh_from_row(row)

    def _load_for_update_at(
        self,
        connection: psycopg.Connection[Any],
        workspace_id: str,
        refresh_id: CatalogRefreshId,
    ) -> tuple[CatalogRefreshState, datetime]:
        """Lock one refresh and obtain its lease-comparison time in one snapshot."""

        refreshes = self._db.table("catalog_refresh_runs")
        row = connection.execute(
            sql.SQL(
                """
                SELECT {columns}, clock_timestamp()
                FROM {refreshes}
                WHERE workspace_id = %s
                  AND refresh_id = %s
                FOR UPDATE
                """
            ).format(columns=_REFRESH_COLUMNS, refreshes=refreshes),
            (workspace_id, refresh_id.root),
        ).fetchone()
        if row is None:
            raise _resource_unavailable()
        return (
            _refresh_from_row(row[:-1]),
            _aware(row[-1], "catalog database time"),
        )

    def _load_locator(
        self,
        connection: psycopg.Connection[Any],
        workspace_id: str,
        refresh_id: CatalogRefreshId,
    ) -> tuple[str, int]:
        refreshes = self._db.table("catalog_refresh_runs")
        row = connection.execute(
            sql.SQL(
                """
                SELECT connection_id, target_generation
                FROM {refreshes}
                WHERE workspace_id = %s
                  AND refresh_id = %s
                """
            ).format(refreshes=refreshes),
            (workspace_id, refresh_id.root),
        ).fetchone()
        if row is None:
            raise _resource_unavailable()
        return str(row[0]), int(row[1])

    def _lock_completion_scope(
        self,
        connection: psycopg.Connection[Any],
        *,
        workspace_id: str,
        connection_id: str,
        refresh_id: CatalogRefreshId,
        target_generation: int,
        fencing_token: int,
        capability_digest: str,
    ) -> None:
        function = sql.SQL("{}.lock_catalog_completion_scope").format(sql.Identifier(self.schema))
        try:
            connection.execute(
                sql.SQL("SELECT {}(%s, %s, %s, %s, %s, %s)").format(function),
                (
                    workspace_id,
                    connection_id,
                    refresh_id.root,
                    target_generation,
                    fencing_token,
                    capability_digest,
                ),
            ).fetchone()
        except psycopg.Error as error:
            if error.sqlstate == "55000":
                raise _lease_conflict() from error
            raise

    def _insert_generation(
        self,
        connection: psycopg.Connection[Any],
        state: CatalogRefreshState,
        observed_at: datetime,
    ) -> bool:
        generations = self._db.table("catalog_generations")
        inserted = connection.execute(
            sql.SQL(
                """
                INSERT INTO {generations} (
                    workspace_id,
                    connection_id,
                    generation,
                    refresh_id,
                    base_generation,
                    refresh_mode,
                    status,
                    created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, 'staging', %s)
                ON CONFLICT DO NOTHING
                RETURNING 1
                """
            ).format(generations=generations),
            (
                state.workspace_id,
                state.connection_id.root,
                state.target_generation,
                state.refresh_id.root,
                _physical_base_generation(state.base_generation),
                state.mode.value,
                observed_at,
            ),
        ).fetchone()
        if inserted is not None:
            return True
        current = connection.execute(
            sql.SQL(
                """
                SELECT
                    generation.refresh_id,
                    generation.base_generation,
                    generation.refresh_mode,
                    generation.status,
                    generation.source_identity_fingerprint
                        = binding.source_identity_fingerprint,
                    generation.catalog_identity_fingerprint
                        = binding.catalog_identity_fingerprint,
                    generation.type_contract_fingerprint
                        = binding.type_contract_fingerprint
                FROM {generations} AS generation
                JOIN {bindings} AS binding
                  ON binding.workspace_id = generation.workspace_id
                 AND binding.connection_id = generation.connection_id
                 AND binding.refresh_id = generation.refresh_id
                WHERE generation.workspace_id = %s
                  AND generation.connection_id = %s
                  AND generation.generation = %s
                """
            ).format(
                generations=generations,
                bindings=self._db.table("catalog_refresh_semantic_bindings"),
            ),
            (
                state.workspace_id,
                state.connection_id.root,
                state.target_generation,
            ),
        ).fetchone()
        expected = (
            state.refresh_id.root,
            _physical_base_generation(state.base_generation),
            state.mode.value,
            "staging",
            True,
            True,
            True,
        )
        if current != expected:
            raise _refresh_conflict()
        return False

    def _clone_base_generation(
        self,
        connection: psycopg.Connection[Any],
        state: CatalogRefreshState,
    ) -> None:
        assets = self._db.table("catalog_assets")
        fields = self._db.table("catalog_fields")
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {assets} (
                    workspace_id, connection_id, generation, asset_key, asset_id,
                    qualified_name, asset_sort_key, platform, environment,
                    database_name, schema_name, table_name, display_name,
                    description, field_count, metadata_fingerprint, observed_at
                )
                SELECT workspace_id, connection_id, %s, asset_key, asset_id,
                       qualified_name, asset_sort_key, platform, environment,
                       database_name, schema_name, table_name, display_name,
                       description, field_count, metadata_fingerprint, observed_at
                FROM {assets}
                WHERE workspace_id = %s
                  AND connection_id = %s
                  AND generation = %s
                """
            ).format(assets=assets),
            (
                state.target_generation,
                state.workspace_id,
                state.connection_id.root,
                state.base_generation,
            ),
        )
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {fields} (
                    workspace_id, connection_id, generation, asset_key, field_key,
                    field_path, field_sort_key, field_name, ordinal_position,
                    native_type, normalized_type, nullable, is_part_of_key,
                    tags, glossary_terms, description, metadata_fingerprint,
                    observed_at
                )
                SELECT workspace_id, connection_id, %s, asset_key, field_key,
                       field_path, field_sort_key, field_name, ordinal_position,
                       native_type, normalized_type, nullable, is_part_of_key,
                       tags, glossary_terms, description, metadata_fingerprint,
                       observed_at
                FROM {fields}
                WHERE workspace_id = %s
                  AND connection_id = %s
                  AND generation = %s
                """
            ).format(fields=fields),
            (
                state.target_generation,
                state.workspace_id,
                state.connection_id.root,
                state.base_generation,
            ),
        )

    def _apply_change(
        self,
        connection: psycopg.Connection[Any],
        *,
        state: CatalogRefreshState,
        change: CatalogSourceChange,
        observed_at: datetime,
    ) -> None:
        if change.kind is CatalogSourceChangeKind.UPSERT_ASSET:
            assert change.asset is not None
            self._upsert_asset(
                connection,
                state=state,
                asset=change.asset,
                observed_at=observed_at,
            )
            return
        assert change.asset_id is not None
        if change.kind is CatalogSourceChangeKind.DELETE_ASSET:
            self._delete_asset(
                connection,
                state=state,
                asset_id=change.asset_id,
                observed_at=observed_at,
            )
            return
        if change.kind is CatalogSourceChangeKind.UPSERT_FIELD:
            assert change.field is not None
            self._upsert_field(
                connection,
                state=state,
                asset_id=change.asset_id,
                source_field=change.field,
                observed_at=observed_at,
            )
            return
        assert change.field_path is not None
        self._delete_field(
            connection,
            state=state,
            asset_id=change.asset_id,
            field_path=change.field_path,
            observed_at=observed_at,
        )

    def _upsert_asset(
        self,
        connection: psycopg.Connection[Any],
        *,
        state: CatalogRefreshState,
        asset: CatalogSourceAsset,
        observed_at: datetime,
    ) -> None:
        assets = self._db.table("catalog_assets")
        fields = self._db.table("catalog_fields")
        asset_key = _asset_key(asset.asset_id.root)
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {assets} (
                    workspace_id, connection_id, generation, asset_key, asset_id,
                    qualified_name, asset_sort_key, platform, environment,
                    database_name, schema_name, table_name, display_name,
                    description, field_count, metadata_fingerprint, observed_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (
                    workspace_id, connection_id, generation, asset_key
                ) DO UPDATE
                SET qualified_name = EXCLUDED.qualified_name,
                    asset_sort_key = EXCLUDED.asset_sort_key,
                    platform = EXCLUDED.platform,
                    environment = EXCLUDED.environment,
                    database_name = EXCLUDED.database_name,
                    schema_name = EXCLUDED.schema_name,
                    table_name = EXCLUDED.table_name,
                    display_name = EXCLUDED.display_name,
                    description = EXCLUDED.description,
                    field_count = EXCLUDED.field_count,
                    metadata_fingerprint = EXCLUDED.metadata_fingerprint,
                    observed_at = EXCLUDED.observed_at
                """
            ).format(assets=assets),
            (
                state.workspace_id,
                state.connection_id.root,
                state.target_generation,
                asset_key,
                asset.asset_id.root,
                asset.qualified_name,
                asset.qualified_name,
                asset.platform,
                asset.environment,
                asset.database_name,
                asset.schema_name,
                asset.qualified_name.rsplit(".", maxsplit=1)[-1],
                asset.display_name,
                asset.description,
                len(asset.fields),
                asset.metadata_fingerprint,
                observed_at,
            ),
        )
        connection.execute(
            sql.SQL(
                """
                DELETE FROM {fields}
                WHERE workspace_id = %s
                  AND connection_id = %s
                  AND generation = %s
                  AND asset_key = %s
                """
            ).format(fields=fields),
            (
                state.workspace_id,
                state.connection_id.root,
                state.target_generation,
                asset_key,
            ),
        )
        for ordinal, source_field in enumerate(asset.fields, start=1):
            self._insert_field(
                connection,
                state=state,
                asset_key=asset_key,
                source_field=source_field,
                ordinal_position=ordinal,
                observed_at=observed_at,
            )
        if state.mode is CatalogRefreshMode.DELTA and state.base_generation > 0:
            self._tombstone_missing_base_fields(
                connection,
                state=state,
                asset_key=asset_key,
                observed_at=observed_at,
            )

    def _upsert_full_asset_page(
        self,
        connection: psycopg.Connection[Any],
        *,
        state: CatalogRefreshState,
        assets: tuple[CatalogSourceAsset, ...],
        observed_at: datetime,
    ) -> None:
        """Persist one bounded full-scan page with at most two SQL calls."""

        if state.mode is not CatalogRefreshMode.FULL:
            raise _invalid_response()
        if not assets:
            return
        asset_rows = [
            {
                "asset_key": _asset_key(asset.asset_id.root),
                "asset_id": asset.asset_id.root,
                "qualified_name": asset.qualified_name,
                "platform": asset.platform,
                "environment": asset.environment,
                "database_name": asset.database_name,
                "schema_name": asset.schema_name,
                "table_name": asset.qualified_name.rsplit(".", maxsplit=1)[-1],
                "display_name": asset.display_name,
                "description": asset.description,
                "field_count": len(asset.fields),
                "metadata_fingerprint": asset.metadata_fingerprint,
            }
            for asset in assets
        ]
        field_rows = [
            {
                "asset_key": _asset_key(asset.asset_id.root),
                "field_key": _field_key(source_field.field_path),
                "field_path": list(source_field.field_path),
                "field_sort_key": source_field.field_path[-1],
                "field_name": source_field.field_path[-1],
                "ordinal_position": ordinal,
                "native_type": source_field.native_type,
                "normalized_type": (
                    source_field.normalized_type.value
                    if source_field.normalized_type is not None
                    else None
                ),
                "nullable": source_field.nullable,
                "is_part_of_key": source_field.is_part_of_key,
                "tags": list(source_field.tags),
                "glossary_terms": list(source_field.glossary_terms),
                "description": source_field.description,
                "metadata_fingerprint": source_field.metadata_fingerprint,
            }
            for asset in assets
            for ordinal, source_field in enumerate(asset.fields, start=1)
        ]
        assets_table = self._db.table("catalog_assets")
        fields_table = self._db.table("catalog_fields")
        connection.execute(
            sql.SQL(
                """
                WITH incoming AS (
                    SELECT *
                    FROM jsonb_to_recordset(%s::jsonb) AS row (
                        asset_key text,
                        asset_id text,
                        qualified_name text,
                        platform text,
                        environment text,
                        database_name text,
                        schema_name text,
                        table_name text,
                        display_name text,
                        description text,
                        field_count integer,
                        metadata_fingerprint text
                    )
                )
                INSERT INTO {assets} (
                    workspace_id, connection_id, generation, asset_key, asset_id,
                    qualified_name, asset_sort_key, platform, environment,
                    database_name, schema_name, table_name, display_name,
                    description, field_count, metadata_fingerprint, observed_at
                )
                SELECT %s, %s, %s, asset_key, asset_id,
                       qualified_name, qualified_name, platform, environment,
                       database_name, schema_name, table_name, display_name,
                       description, field_count, metadata_fingerprint, %s
                FROM incoming
                """
            ).format(assets=assets_table),
            (
                Jsonb(asset_rows),
                state.workspace_id,
                state.connection_id.root,
                state.target_generation,
                observed_at,
            ),
        )
        if not field_rows:
            return
        insert_fields = sql.SQL(
            """
            WITH incoming AS (
                SELECT *
                FROM jsonb_to_recordset(%s::jsonb) AS row (
                    asset_key text,
                    field_key text,
                    field_path text[],
                    field_sort_key text,
                    field_name text,
                    ordinal_position integer,
                    native_type text,
                    normalized_type text,
                    nullable boolean,
                    is_part_of_key boolean,
                    tags text[],
                    glossary_terms text[],
                    description text,
                    metadata_fingerprint text
                )
            )
            INSERT INTO {fields} (
                workspace_id, connection_id, generation, asset_key, field_key,
                field_path, field_sort_key, field_name, ordinal_position,
                native_type, normalized_type, nullable, is_part_of_key,
                tags, glossary_terms, description, metadata_fingerprint,
                observed_at
            )
            SELECT %s, %s, %s, asset_key, field_key,
                   field_path, field_sort_key, field_name, ordinal_position,
                   native_type, normalized_type, nullable, is_part_of_key,
                   tags, glossary_terms, description, metadata_fingerprint,
                   %s
            FROM incoming
            """
        ).format(fields=fields_table)
        for offset in range(0, len(field_rows), _MAX_FULL_FIELD_INSERT_BATCH):
            connection.execute(
                insert_fields,
                (
                    Jsonb(field_rows[offset : offset + _MAX_FULL_FIELD_INSERT_BATCH]),
                    state.workspace_id,
                    state.connection_id.root,
                    state.target_generation,
                    observed_at,
                ),
            )

    def _upsert_field(
        self,
        connection: psycopg.Connection[Any],
        *,
        state: CatalogRefreshState,
        asset_id: CatalogAssetId,
        source_field: CatalogSourceField,
        observed_at: datetime,
    ) -> None:
        assets = self._db.table("catalog_assets")
        fields = self._db.table("catalog_fields")
        asset_key = _asset_key(asset_id.root)
        asset_row = connection.execute(
            sql.SQL(
                """
                SELECT 1
                FROM {assets}
                WHERE workspace_id = %s
                  AND connection_id = %s
                  AND generation = %s
                  AND asset_key = %s
                  AND asset_id = %s
                """
            ).format(assets=assets),
            (
                state.workspace_id,
                state.connection_id.root,
                state.target_generation,
                asset_key,
                asset_id.root,
            ),
        ).fetchone()
        if asset_row is None:
            raise _resource_unavailable()
        field_key = _field_key(source_field.field_path)
        ordinal_row = connection.execute(
            sql.SQL(
                """
                SELECT ordinal_position
                FROM {fields}
                WHERE workspace_id = %s
                  AND connection_id = %s
                  AND generation = %s
                  AND asset_key = %s
                  AND field_key = %s
                """
            ).format(fields=fields),
            (
                state.workspace_id,
                state.connection_id.root,
                state.target_generation,
                asset_key,
                field_key,
            ),
        ).fetchone()
        if ordinal_row is None:
            next_ordinal = connection.execute(
                sql.SQL(
                    """
                    SELECT coalesce(max(ordinal_position), 0) + 1
                    FROM {fields}
                    WHERE workspace_id = %s
                      AND connection_id = %s
                      AND generation = %s
                      AND asset_key = %s
                    """
                ).format(fields=fields),
                (
                    state.workspace_id,
                    state.connection_id.root,
                    state.target_generation,
                    asset_key,
                ),
            ).fetchone()
            if next_ordinal is None:
                raise _invalid_response()
            ordinal_position = int(next_ordinal[0])
        else:
            ordinal_position = int(ordinal_row[0])
        self._insert_field(
            connection,
            state=state,
            asset_key=asset_key,
            source_field=source_field,
            ordinal_position=ordinal_position,
            observed_at=observed_at,
        )
        self._refresh_asset_field_count(connection, state, asset_key)

    def _insert_field(
        self,
        connection: psycopg.Connection[Any],
        *,
        state: CatalogRefreshState,
        asset_key: str,
        source_field: CatalogSourceField,
        ordinal_position: int,
        observed_at: datetime,
    ) -> None:
        fields = self._db.table("catalog_fields")
        field_key = _field_key(source_field.field_path)
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {fields} (
                    workspace_id, connection_id, generation, asset_key, field_key,
                    field_path, field_sort_key, field_name, ordinal_position,
                    native_type, normalized_type, nullable, is_part_of_key,
                    tags, glossary_terms, description, metadata_fingerprint,
                    observed_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (
                    workspace_id, connection_id, generation, asset_key, field_key
                ) DO UPDATE
                SET field_sort_key = EXCLUDED.field_sort_key,
                    field_name = EXCLUDED.field_name,
                    ordinal_position = EXCLUDED.ordinal_position,
                    native_type = EXCLUDED.native_type,
                    normalized_type = EXCLUDED.normalized_type,
                    nullable = EXCLUDED.nullable,
                    is_part_of_key = EXCLUDED.is_part_of_key,
                    tags = EXCLUDED.tags,
                    glossary_terms = EXCLUDED.glossary_terms,
                    description = EXCLUDED.description,
                    metadata_fingerprint = EXCLUDED.metadata_fingerprint,
                    observed_at = EXCLUDED.observed_at
                """
            ).format(fields=fields),
            (
                state.workspace_id,
                state.connection_id.root,
                state.target_generation,
                asset_key,
                field_key,
                list(source_field.field_path),
                source_field.field_path[-1],
                source_field.field_path[-1],
                ordinal_position,
                source_field.native_type,
                (
                    source_field.normalized_type.value
                    if source_field.normalized_type is not None
                    else None
                ),
                source_field.nullable,
                source_field.is_part_of_key,
                list(source_field.tags),
                list(source_field.glossary_terms),
                source_field.description,
                source_field.metadata_fingerprint,
                observed_at,
            ),
        )

    def _delete_asset(
        self,
        connection: psycopg.Connection[Any],
        *,
        state: CatalogRefreshState,
        asset_id: CatalogAssetId,
        observed_at: datetime,
    ) -> None:
        if state.mode is not CatalogRefreshMode.DELTA:
            raise _invalid_response()
        asset_key = _asset_key(asset_id.root)
        if state.base_generation > 0:
            self._tombstone_base_asset(
                connection,
                state=state,
                asset_key=asset_key,
                observed_at=observed_at,
            )
        assets = self._db.table("catalog_assets")
        connection.execute(
            sql.SQL(
                """
                DELETE FROM {assets}
                WHERE workspace_id = %s
                  AND connection_id = %s
                  AND generation = %s
                  AND asset_key = %s
                  AND asset_id = %s
                """
            ).format(assets=assets),
            (
                state.workspace_id,
                state.connection_id.root,
                state.target_generation,
                asset_key,
                asset_id.root,
            ),
        )

    def _delete_field(
        self,
        connection: psycopg.Connection[Any],
        *,
        state: CatalogRefreshState,
        asset_id: CatalogAssetId,
        field_path: tuple[str, ...],
        observed_at: datetime,
    ) -> None:
        if state.mode is not CatalogRefreshMode.DELTA:
            raise _invalid_response()
        asset_key = _asset_key(asset_id.root)
        field_key = _field_key(field_path)
        if state.base_generation > 0:
            self._tombstone_base_field(
                connection,
                state=state,
                asset_key=asset_key,
                field_key=field_key,
                field_path=field_path,
                observed_at=observed_at,
            )
        fields = self._db.table("catalog_fields")
        connection.execute(
            sql.SQL(
                """
                DELETE FROM {fields}
                WHERE workspace_id = %s
                  AND connection_id = %s
                  AND generation = %s
                  AND asset_key = %s
                  AND field_key = %s
                  AND field_path = %s
                """
            ).format(fields=fields),
            (
                state.workspace_id,
                state.connection_id.root,
                state.target_generation,
                asset_key,
                field_key,
                list(field_path),
            ),
        )
        self._refresh_asset_field_count(connection, state, asset_key)

    def _tombstone_base_asset(
        self,
        connection: psycopg.Connection[Any],
        *,
        state: CatalogRefreshState,
        asset_key: str,
        observed_at: datetime,
    ) -> None:
        assets = self._db.table("catalog_assets")
        fields = self._db.table("catalog_fields")
        tombstones = self._db.table("catalog_tombstones")
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {tombstones} (
                    workspace_id, connection_id, observed_missing_in_generation,
                    resource_kind, asset_key, asset_id, resource_key, field_key,
                    field_path, removed_from_generation,
                    prior_metadata_fingerprint, removal_reason, observed_at
                )
                SELECT asset.workspace_id, asset.connection_id, %s, 'asset',
                       asset.asset_key, asset.asset_id, asset.asset_key, NULL, NULL,
                       %s, asset.metadata_fingerprint, 'delta_delete', %s
                FROM {assets} AS asset
                WHERE asset.workspace_id = %s
                  AND asset.connection_id = %s
                  AND asset.generation = %s
                  AND asset.asset_key = %s
                ON CONFLICT DO NOTHING
                """
            ).format(tombstones=tombstones, assets=assets),
            (
                state.target_generation,
                state.base_generation,
                observed_at,
                state.workspace_id,
                state.connection_id.root,
                state.base_generation,
                asset_key,
            ),
        )
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {tombstones} (
                    workspace_id, connection_id, observed_missing_in_generation,
                    resource_kind, asset_key, asset_id, resource_key, field_key,
                    field_path, removed_from_generation,
                    prior_metadata_fingerprint, removal_reason, observed_at
                )
                SELECT field.workspace_id, field.connection_id, %s, 'field',
                       field.asset_key, asset.asset_id, field.field_key,
                       field.field_key, field.field_path, %s,
                       field.metadata_fingerprint, 'delta_delete', %s
                FROM {fields} AS field
                JOIN {assets} AS asset
                  ON asset.workspace_id = field.workspace_id
                 AND asset.connection_id = field.connection_id
                 AND asset.generation = field.generation
                 AND asset.asset_key = field.asset_key
                WHERE field.workspace_id = %s
                  AND field.connection_id = %s
                  AND field.generation = %s
                  AND field.asset_key = %s
                ON CONFLICT DO NOTHING
                """
            ).format(
                tombstones=tombstones,
                fields=fields,
                assets=assets,
            ),
            (
                state.target_generation,
                state.base_generation,
                observed_at,
                state.workspace_id,
                state.connection_id.root,
                state.base_generation,
                asset_key,
            ),
        )

    def _tombstone_base_field(
        self,
        connection: psycopg.Connection[Any],
        *,
        state: CatalogRefreshState,
        asset_key: str,
        field_key: str,
        field_path: tuple[str, ...],
        observed_at: datetime,
    ) -> None:
        assets = self._db.table("catalog_assets")
        fields = self._db.table("catalog_fields")
        tombstones = self._db.table("catalog_tombstones")
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {tombstones} (
                    workspace_id, connection_id, observed_missing_in_generation,
                    resource_kind, asset_key, asset_id, resource_key, field_key,
                    field_path, removed_from_generation,
                    prior_metadata_fingerprint, removal_reason, observed_at
                )
                SELECT field.workspace_id, field.connection_id, %s, 'field',
                       field.asset_key, asset.asset_id, field.field_key,
                       field.field_key, field.field_path, %s,
                       field.metadata_fingerprint, 'delta_delete', %s
                FROM {fields} AS field
                JOIN {assets} AS asset
                  ON asset.workspace_id = field.workspace_id
                 AND asset.connection_id = field.connection_id
                 AND asset.generation = field.generation
                 AND asset.asset_key = field.asset_key
                WHERE field.workspace_id = %s
                  AND field.connection_id = %s
                  AND field.generation = %s
                  AND field.asset_key = %s
                  AND field.field_key = %s
                  AND field.field_path = %s
                ON CONFLICT DO NOTHING
                """
            ).format(
                tombstones=tombstones,
                fields=fields,
                assets=assets,
            ),
            (
                state.target_generation,
                state.base_generation,
                observed_at,
                state.workspace_id,
                state.connection_id.root,
                state.base_generation,
                asset_key,
                field_key,
                list(field_path),
            ),
        )

    def _tombstone_missing_base_fields(
        self,
        connection: psycopg.Connection[Any],
        *,
        state: CatalogRefreshState,
        asset_key: str,
        observed_at: datetime,
    ) -> None:
        assets = self._db.table("catalog_assets")
        fields = self._db.table("catalog_fields")
        tombstones = self._db.table("catalog_tombstones")
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {tombstones} (
                    workspace_id, connection_id, observed_missing_in_generation,
                    resource_kind, asset_key, asset_id, resource_key, field_key,
                    field_path, removed_from_generation,
                    prior_metadata_fingerprint, removal_reason, observed_at
                )
                SELECT prior.workspace_id, prior.connection_id, %s, 'field',
                       prior.asset_key, asset.asset_id, prior.field_key,
                       prior.field_key, prior.field_path, %s,
                       prior.metadata_fingerprint, 'delta_delete', %s
                FROM {fields} AS prior
                JOIN {assets} AS asset
                  ON asset.workspace_id = prior.workspace_id
                 AND asset.connection_id = prior.connection_id
                 AND asset.generation = prior.generation
                 AND asset.asset_key = prior.asset_key
                WHERE prior.workspace_id = %s
                  AND prior.connection_id = %s
                  AND prior.generation = %s
                  AND prior.asset_key = %s
                  AND NOT EXISTS (
                      SELECT 1
                      FROM {fields} AS current
                      WHERE current.workspace_id = prior.workspace_id
                        AND current.connection_id = prior.connection_id
                        AND current.generation = %s
                        AND current.asset_key = prior.asset_key
                        AND current.field_key = prior.field_key
                  )
                ON CONFLICT DO NOTHING
                """
            ).format(
                tombstones=tombstones,
                fields=fields,
                assets=assets,
            ),
            (
                state.target_generation,
                state.base_generation,
                observed_at,
                state.workspace_id,
                state.connection_id.root,
                state.base_generation,
                asset_key,
                state.target_generation,
            ),
        )

    def _refresh_asset_field_count(
        self,
        connection: psycopg.Connection[Any],
        state: CatalogRefreshState,
        asset_key: str,
    ) -> None:
        assets = self._db.table("catalog_assets")
        fields = self._db.table("catalog_fields")
        connection.execute(
            sql.SQL(
                """
                UPDATE {assets} AS asset
                SET field_count = (
                    SELECT count(*)
                    FROM {fields} AS field
                    WHERE field.workspace_id = asset.workspace_id
                      AND field.connection_id = asset.connection_id
                      AND field.generation = asset.generation
                      AND field.asset_key = asset.asset_key
                )
                WHERE asset.workspace_id = %s
                  AND asset.connection_id = %s
                  AND asset.generation = %s
                  AND asset.asset_key = %s
                """
            ).format(assets=assets, fields=fields),
            (
                state.workspace_id,
                state.connection_id.root,
                state.target_generation,
                asset_key,
            ),
        )

    def _generation_counts(
        self,
        connection: psycopg.Connection[Any],
        state: CatalogRefreshState,
    ) -> tuple[int, int]:
        assets = self._db.table("catalog_assets")
        fields = self._db.table("catalog_fields")
        row = connection.execute(
            sql.SQL(
                """
                SELECT
                    (
                        SELECT count(*)
                        FROM {assets}
                        WHERE workspace_id = %s
                          AND connection_id = %s
                          AND generation = %s
                    ),
                    (
                        SELECT count(*)
                        FROM {fields}
                        WHERE workspace_id = %s
                          AND connection_id = %s
                          AND generation = %s
                    )
                """
            ).format(assets=assets, fields=fields),
            (
                state.workspace_id,
                state.connection_id.root,
                state.target_generation,
                state.workspace_id,
                state.connection_id.root,
                state.target_generation,
            ),
        ).fetchone()
        if row is None:
            raise _invalid_response()
        return int(row[0]), int(row[1])

    def _stream_generation_fingerprint(
        self,
        connection: psycopg.Connection[Any],
        state: CatalogRefreshState,
    ) -> str:
        assets = self._db.table("catalog_assets")
        fields = self._db.table("catalog_fields")
        generations = self._db.table("catalog_generations")
        identity_row = connection.execute(
            sql.SQL(
                """
                SELECT
                    source_identity_fingerprint,
                    catalog_identity_fingerprint,
                    type_contract_fingerprint
                FROM {generations}
                WHERE workspace_id = %s
                  AND connection_id = %s
                  AND generation = %s
                  AND status = 'staging'
                """
            ).format(generations=generations),
            (
                state.workspace_id,
                state.connection_id.root,
                state.target_generation,
            ),
        ).fetchone()
        if (
            identity_row is None
            or len(identity_row) != 3
            or any(
                not isinstance(value, str) or _SHA256.fullmatch(value) is None
                for value in identity_row
            )
        ):
            raise _refresh_conflict()
        identity_payload = json.dumps(
            {
                "catalog_identity_fingerprint": identity_row[1],
                "fingerprint_version": "m28-catalog-generation-identity-v1",
                "source_identity_fingerprint": identity_row[0],
                "type_contract_fingerprint": identity_row[2],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        query = sql.SQL(
            """
            SELECT payload
            FROM (
                SELECT
                    0 AS record_kind,
                    asset.asset_key::text AS asset_key,
                    ''::text AS field_key,
                    jsonb_build_array(
                        'asset',
                        asset.asset_key::text,
                        asset.asset_id,
                        asset.qualified_name,
                        asset.platform,
                        asset.environment,
                        asset.database_name,
                        asset.schema_name,
                        asset.table_name,
                        asset.display_name,
                        asset.description,
                        asset.field_count,
                        asset.metadata_fingerprint::text
                    )::text AS payload
                FROM {assets} AS asset
                WHERE asset.workspace_id = %s
                  AND asset.connection_id = %s
                  AND asset.generation = %s

                UNION ALL

                SELECT
                    1 AS record_kind,
                    field.asset_key::text AS asset_key,
                    field.field_key::text AS field_key,
                    jsonb_build_array(
                        'field',
                        field.asset_key::text,
                        field.field_key::text,
                        field.field_path,
                        field.native_type,
                        field.normalized_type,
                        field.nullable,
                        field.is_part_of_key,
                        field.tags,
                        field.glossary_terms,
                        field.description,
                        field.metadata_fingerprint::text
                    )::text AS payload
                FROM {fields} AS field
                WHERE field.workspace_id = %s
                  AND field.connection_id = %s
                  AND field.generation = %s
            ) AS inventory
            ORDER BY record_kind, asset_key, field_key
            """
        ).format(assets=assets, fields=fields)
        parameters = (
            state.workspace_id,
            state.connection_id.root,
            state.target_generation,
            state.workspace_id,
            state.connection_id.root,
            state.target_generation,
        )
        digest = hashlib.sha256()
        digest.update(_FINGERPRINT_PREFIX)
        digest.update(len(identity_payload).to_bytes(8, "big"))
        digest.update(identity_payload)
        cursor_name = f"catalog_fingerprint_{uuid4().hex}"
        with connection.cursor(name=cursor_name) as cursor:
            cursor.itersize = _FINGERPRINT_FETCH_SIZE
            cursor.execute(query, parameters)
            for row in cursor:
                payload = str(row[0]).encode("utf-8")
                digest.update(len(payload).to_bytes(8, "big"))
                digest.update(payload)
        return digest.hexdigest()


def state_to_summary(state: CatalogRefreshState) -> CatalogRefreshSummary:
    """Keep the adapter return conversion explicit at the domain boundary."""

    return CatalogRefreshSummary.from_state(state)


def _public_refresh_from_row(row: tuple[Any, ...]) -> CatalogRefreshSummary:
    if len(row) != 16:
        raise ValueError("catalog public refresh response has an invalid shape")
    return CatalogRefreshSummary(
        refresh_id=CatalogRefreshId(row[0]),
        workspace_id=row[1],
        connection_id=CatalogConnectionId(row[2]),
        mode=CatalogRefreshMode(row[3]),
        status=CatalogRefreshStatus(row[4]),
        base_generation=row[5],
        target_generation=row[6],
        source_page_count=row[7],
        asset_count=row[8],
        field_count=row[9],
        source_complete=row[10],
        catalog_fingerprint=row[11],
        failure_code=None if row[12] is None else CatalogRefreshFailureCode(row[12]),
        requested_at=row[13],
        updated_at=row[14],
        completed_at=row[15],
    )


def _refresh_from_row(row: tuple[Any, ...]) -> CatalogRefreshState:
    if len(row) != len(_REFRESH_COLUMN_NAMES):
        raise ValueError("catalog refresh response has an invalid shape")
    status = CatalogRefreshStatus(row[4])
    lease: CatalogRefreshLease | None = None
    if status in {CatalogRefreshStatus.LEASED, CatalogRefreshStatus.STAGING}:
        if any(row[index] is None for index in (11, 12, 14, 15)):
            raise ValueError("catalog refresh lease response is incomplete")
        lease = CatalogRefreshLease(
            indexer_id=row[11],
            capability_digest=row[12],
            fencing_token=row[13],
            leased_at=row[14],
            expires_at=row[15],
        )
    return CatalogRefreshState(
        refresh_id=CatalogRefreshId(row[0]),
        workspace_id=row[1],
        connection_id=CatalogConnectionId(row[2]),
        mode=CatalogRefreshMode(row[3]),
        status=status,
        base_generation=0 if row[5] is None else row[5],
        target_generation=row[6],
        idempotency_digest=row[7],
        requested_by=row[8],
        requested_at=row[9],
        updated_at=row[10],
        lease=lease,
        source_page_count=row[16],
        source_page_fingerprint=row[17],
        staged_asset_count=row[18],
        staged_field_count=row[19],
        source_complete=row[20],
        source_checkpoint=row[21],
        catalog_fingerprint=row[22],
        failure_code=None if row[23] is None else CatalogRefreshFailureCode(row[23]),
        completed_at=row[24],
    )


def _qualified_refresh_columns(alias: str) -> sql.Composed:
    return sql.SQL(", ").join(sql.Identifier(alias, column) for column in _REFRESH_COLUMN_NAMES)


def _request_fingerprint(command: CatalogRefreshCommand) -> str:
    payload = {
        "connection_id": command.connection_id.root,
        "mode": command.mode.value,
        "requested_by": command.requested_by,
        "workspace_id": command.workspace_id,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _asset_key(asset_id: str) -> str:
    return hashlib.sha256(asset_id.encode("utf-8")).hexdigest()


def _field_key(field_path: tuple[str, ...]) -> str:
    encoded = json.dumps(
        list(field_path),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _physical_base_generation(base_generation: int) -> int | None:
    return None if base_generation == 0 else base_generation


def _capability_digest(capability: str) -> str:
    if not isinstance(capability, str):
        raise ValueError("catalog lease capability is invalid")
    encoded = capability.encode("utf-8")
    if (
        not capability
        or capability.strip() != capability
        or not 32 <= len(encoded) <= 1_024
        or len(set(encoded)) < 8
    ):
        raise ValueError("catalog lease capability is invalid")
    return hashlib.sha256(encoded).hexdigest()


def _lease_seconds(duration: timedelta) -> int:
    if not isinstance(duration, timedelta):
        raise ValueError("catalog lease duration is invalid")
    total = duration.total_seconds()
    if not total.is_integer() or not _MIN_LEASE_SECONDS <= total <= _MAX_LEASE_SECONDS:
        raise ValueError("catalog lease duration is invalid")
    return int(total)


def _fencing_token(value: int) -> None:
    if isinstance(value, bool) or value < 1:
        raise ValueError("catalog fencing token is invalid")


def _batch_limit(value: int) -> None:
    if isinstance(value, bool) or not 1 <= value <= _MAX_BATCH:
        raise ValueError("catalog refresh batch limit is invalid")


def _workspace_id(value: str) -> str:
    return _safe_id(value, "catalog workspace identifier")


def _indexer_id(value: str) -> str:
    if not isinstance(value, str) or _SAFE_INDEXER_ID.fullmatch(value) is None:
        raise ValueError("catalog indexer identifier is invalid")
    return value


def _safe_id(value: str, label: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _require_owned(
    state: CatalogRefreshState,
    *,
    indexer_id: str,
    capability_digest: str,
    fencing_token: int,
    observed_at: datetime,
    statuses: set[CatalogRefreshStatus],
) -> None:
    lease = state.lease
    if (
        state.status not in statuses
        or lease is None
        or lease.indexer_id != indexer_id
        or lease.fencing_token != fencing_token
        or not hmac.compare_digest(lease.capability_digest, capability_digest)
        or not lease.is_current(observed_at)
    ):
        raise _lease_conflict()


def _resource_unavailable() -> CatalogInventoryError:
    return CatalogInventoryError(
        CatalogInventoryErrorCode.RESOURCE_UNAVAILABLE,
        "catalog refresh is unavailable",
    )


def _unavailable() -> CatalogInventoryError:
    return CatalogInventoryError(
        CatalogInventoryErrorCode.UNAVAILABLE,
        "catalog inventory store is unavailable",
    )


def _invalid_response() -> CatalogInventoryError:
    return CatalogInventoryError(
        CatalogInventoryErrorCode.INVALID_RESPONSE,
        "catalog inventory response is invalid",
    )


def _idempotency_conflict() -> CatalogInventoryError:
    return CatalogInventoryError(
        CatalogInventoryErrorCode.IDEMPOTENCY_CONFLICT,
        "catalog refresh idempotency conflict",
    )


def _refresh_conflict() -> CatalogInventoryError:
    return CatalogInventoryError(
        CatalogInventoryErrorCode.REFRESH_CONFLICT,
        "catalog refresh conflicts with current state",
    )


def _lease_conflict() -> CatalogInventoryError:
    return CatalogInventoryError(
        CatalogInventoryErrorCode.LEASE_CONFLICT,
        "catalog refresh lease is unavailable",
    )


def _capacity_exceeded() -> CatalogInventoryError:
    return CatalogInventoryError(
        CatalogInventoryErrorCode.CAPACITY_EXCEEDED,
        "catalog capacity is exceeded",
    )


def _delta_unsupported() -> CatalogInventoryError:
    return CatalogInventoryError(
        CatalogInventoryErrorCode.DELTA_UNSUPPORTED,
        "catalog delta refresh is unavailable",
    )
