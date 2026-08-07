"""PostgreSQL adapters for the tenant-scoped dynamic catalog index.

The API-facing classes deliberately cannot read the opaque connection route.  The
catalog indexer receives a separate route reader and refresh writer.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg
from psycopg import sql
from psycopg.errors import (
    CheckViolation,
    ForeignKeyViolation,
    UniqueViolation,
)
from pydantic import ValidationError

from schemabridge.adapters.storage.postgres import (
    ControlConnectionProvider,
    _ControlDatabase,
)
from schemabridge.application.ports.catalog_inventory import (
    CatalogInventoryError,
    CatalogInventoryErrorCode,
    ManagedCatalogConnectorRoute,
)
from schemabridge.domain.catalog_inventory import (
    CapacityAdmission,
    CapacityResource,
    CatalogAssetFilter,
    CatalogAssetId,
    CatalogAssetLocator,
    CatalogAssetSummary,
    CatalogConnectionDisable,
    CatalogConnectionFilter,
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogConnectionRegistration,
    CatalogConnectionRegistrationResult,
    CatalogConnectionRoute,
    CatalogConnectionStatus,
    CatalogConnectionSummary,
    CatalogFieldFilter,
    CatalogFieldLocator,
    CatalogFieldSummary,
    CatalogRefreshId,
    InventoryCursorResource,
    InventoryPageKey,
    InventoryStorePage,
    TenantCapacityPolicy,
    TenantCapacityPolicyChange,
    TenantCapacitySnapshot,
    TenantCapacityUsage,
)

_MAX_PAGE_SIZE = 50


@dataclass(frozen=True, slots=True)
class _PostgresCatalogAdapter:
    dsn: str = field(repr=False)
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-api"
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


@dataclass(frozen=True, slots=True)
class PostgresCatalogConnectionStore(_PostgresCatalogAdapter):
    """API-safe connection registration, disable, and public listing."""

    stale_after_seconds: int = 900

    def __post_init__(self) -> None:
        _PostgresCatalogAdapter.__post_init__(self)
        if not 60 <= self.stale_after_seconds <= 2_592_000:
            raise ValueError("catalog stale threshold is invalid")

    def register(
        self,
        command: CatalogConnectionRegistration,
    ) -> CatalogConnectionRegistrationResult:
        connections = self._db.table("catalog_connections")
        generations = self._db.table("catalog_generations")
        registration_payload: dict[str, object] = {
            "catalog_scope": command.catalog_scope,
            "connection_id": command.connection_id.root,
            "display_name": command.display_name,
            "environment": command.environment,
            "kind": command.kind.value,
            "requested_by": command.requested_by,
            "workspace_id": command.workspace_id,
        }
        if command.platform_instance is not None:
            registration_payload["platform_instance"] = command.platform_instance
        registration_fingerprint = _fingerprint(registration_payload)
        try:
            with self._db.connect() as connection, connection.transaction():
                existing = _load_registration_row(
                    connection,
                    connections=connections,
                    generations=generations,
                    workspace_id=command.workspace_id,
                    connection_id=command.connection_id.root,
                    idempotency_digest=command.idempotency_digest,
                    stale_after_seconds=self.stale_after_seconds,
                )
                if existing is not None:
                    return _registration_result_from_row(
                        existing,
                        expected=command,
                        replayed=True,
                    )
                inserted = connection.execute(
                    sql.SQL(
                        """
                        INSERT INTO {connections} (
                            workspace_id, connection_id, display_name, source_kind,
                            catalog_scope, environment, platform_instance, status,
                            active_generation, active_generation_fingerprint,
                            active_generation_completed_at, registration_fingerprint,
                            idempotency_digest, created_by_actor_id,
                            disabled_by_actor_id, created_at, updated_at, disabled_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, 'enabled',
                            NULL, NULL, NULL, %s, %s, %s, NULL, %s, %s, NULL
                        )
                        ON CONFLICT DO NOTHING
                        RETURNING connection_id
                        """
                    ).format(connections=connections),
                    (
                        command.workspace_id,
                        command.connection_id.root,
                        command.display_name,
                        command.kind.value,
                        command.catalog_scope,
                        command.environment,
                        command.platform_instance,
                        registration_fingerprint,
                        command.idempotency_digest,
                        command.requested_by,
                        command.requested_at,
                        command.requested_at,
                    ),
                ).fetchone()
                replayed = inserted is None
                current = _load_registration_row(
                    connection,
                    connections=connections,
                    generations=generations,
                    workspace_id=command.workspace_id,
                    connection_id=command.connection_id.root,
                    idempotency_digest=command.idempotency_digest,
                    stale_after_seconds=self.stale_after_seconds,
                )
                if current is None:
                    raise _idempotency_conflict()
                return _registration_result_from_row(
                    current,
                    expected=command,
                    replayed=replayed,
                )
        except CatalogInventoryError:
            raise
        except (UniqueViolation, CheckViolation, ForeignKeyViolation) as error:
            raise _classified_write_error(error) from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            if error.sqlstate == "53300":
                try:
                    with self._db.connect() as connection, connection.transaction():
                        concurrent = _load_registration_row(
                            connection,
                            connections=connections,
                            generations=generations,
                            workspace_id=command.workspace_id,
                            connection_id=command.connection_id.root,
                            idempotency_digest=command.idempotency_digest,
                            stale_after_seconds=self.stale_after_seconds,
                        )
                        if concurrent is not None:
                            return _registration_result_from_row(
                                concurrent,
                                expected=command,
                                replayed=True,
                            )
                except CatalogInventoryError:
                    raise
                except (ValidationError, TypeError, ValueError) as replay_error:
                    raise _invalid_response() from replay_error
                except psycopg.Error as replay_error:
                    raise _unavailable() from replay_error
            raise _classified_write_error(error) from error

    def disable(self, command: CatalogConnectionDisable) -> CatalogConnectionSummary:
        connections = self._db.table("catalog_connections")
        generations = self._db.table("catalog_generations")
        try:
            with self._db.connect() as connection, connection.transaction():
                row = connection.execute(
                    sql.SQL(
                        """
                        UPDATE {connections}
                        SET status = 'disabled',
                            disabled_by_actor_id = %s,
                            disabled_idempotency_digest = %s,
                            disabled_at = %s,
                            updated_at = %s
                        WHERE workspace_id = %s
                          AND connection_id = %s
                          AND status = 'enabled'
                        RETURNING 1
                        """
                    ).format(connections=connections),
                    (
                        command.requested_by,
                        command.idempotency_digest,
                        command.requested_at,
                        command.requested_at,
                        command.workspace_id,
                        command.connection_id.root,
                    ),
                ).fetchone()
                replayed = row is None
                public_row = _load_connection_row(
                    connection,
                    connections=connections,
                    generations=generations,
                    workspace_id=command.workspace_id,
                    connection_id=command.connection_id.root,
                    for_share=True,
                    stale_after_seconds=self.stale_after_seconds,
                )
                if public_row is None:
                    raise _resource_unavailable()
                if replayed and (
                    public_row[-2] != command.idempotency_digest
                    or public_row[-1] != command.requested_by
                ):
                    raise _idempotency_conflict()
                return _connection_from_row(public_row[:-2])
        except CatalogInventoryError:
            raise
        except (CheckViolation, ForeignKeyViolation, UniqueViolation) as error:
            raise _classified_write_error(error) from error
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            raise _classified_write_error(error) from error

    def load_public(
        self,
        workspace_id: str,
        connection_id: CatalogConnectionId,
    ) -> CatalogConnectionSummary | None:
        connections = self._db.table("catalog_connections")
        generations = self._db.table("catalog_generations")
        try:
            with self._db.connect() as connection:
                row = _load_connection_row(
                    connection,
                    connections=connections,
                    generations=generations,
                    workspace_id=workspace_id,
                    connection_id=connection_id.root,
                    stale_after_seconds=self.stale_after_seconds,
                )
            return None if row is None else _connection_from_row(row[:-2])
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def list_connections(
        self,
        workspace_id: str,
        *,
        filters: CatalogConnectionFilter,
        page_size: int,
        after: InventoryPageKey | None,
    ) -> InventoryStorePage[CatalogConnectionSummary]:
        _validate_page_size(page_size)
        connections = self._db.table("catalog_connections")
        generations = self._db.table("catalog_generations")
        clauses = [sql.SQL("connection.workspace_id = %s")]
        params: list[object] = [self.stale_after_seconds, workspace_id]
        if filters.status is not None:
            clauses.append(sql.SQL("connection.status = %s"))
            params.append(filters.status.value)
        if filters.query is not None:
            clauses.append(
                sql.SQL(
                    "(connection.display_name ILIKE %s ESCAPE '\\'"
                    " OR connection.connection_id ILIKE %s ESCAPE '\\')"
                )
            )
            pattern = f"%{_escape_like(filters.query)}%"
            params.extend((pattern, pattern))
        if after is not None:
            clauses.append(
                sql.SQL("(connection.display_name, connection.connection_id) > (%s, %s)")
            )
            params.extend((after.sort_value, after.stable_id))
        params.append(page_size + 1)
        try:
            with self._db.connect() as connection:
                rows = connection.execute(
                    sql.SQL(
                        """
                        SELECT connection.workspace_id, connection.connection_id,
                               connection.display_name, connection.source_kind,
                               connection.environment, connection.catalog_scope,
                               connection.platform_instance, connection.status,
                               connection.active_generation,
                               coalesce(generation.asset_count, 0),
                               coalesce(generation.field_count, 0),
                               connection.active_generation_completed_at,
                               (
                                   connection.active_generation_completed_at IS NOT NULL
                                   AND connection.active_generation_completed_at
                                       < clock_timestamp() - make_interval(secs => %s)
                               )
                        FROM {connections} AS connection
                        LEFT JOIN {generations} AS generation
                          ON generation.workspace_id = connection.workspace_id
                         AND generation.connection_id = connection.connection_id
                         AND generation.generation = connection.active_generation
                        WHERE {where_clause}
                        ORDER BY connection.display_name, connection.connection_id
                        LIMIT %s
                        """
                    ).format(
                        connections=connections,
                        generations=generations,
                        where_clause=sql.SQL(" AND ").join(clauses),
                    ),
                    tuple(params),
                ).fetchall()
            return _connection_page(rows, page_size=page_size)
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            raise _unavailable() from error


@dataclass(frozen=True, slots=True)
class PostgresCatalogRouteReader(_PostgresCatalogAdapter):
    """Indexer-only access to one unresolved external credential binding."""

    application_name: str = "schemabridge-control-catalog"

    def load_route(
        self,
        workspace_id: str,
        connection_id: CatalogConnectionId,
        *,
        refresh_id: CatalogRefreshId,
        indexer_id: str,
        lease_capability: str,
        fencing_token: int,
    ) -> ManagedCatalogConnectorRoute | None:
        function = sql.SQL("{}.load_owned_catalog_connection_route").format(
            sql.Identifier(self.schema)
        )
        try:
            with self._db.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT *
                        FROM {}(%s, %s, %s, %s, %s, %s)
                        """
                    ).format(function),
                    (
                        workspace_id,
                        connection_id.root,
                        refresh_id.root,
                        indexer_id,
                        lease_capability,
                        fencing_token,
                    ),
                ).fetchone()
            if row is None:
                return None
            return ManagedCatalogConnectorRoute(
                route=CatalogConnectionRoute(
                    workspace_id=row[0],
                    connection_id=CatalogConnectionId(row[1]),
                    kind=CatalogConnectionKind(row[2]),
                    environment=row[3],
                    catalog_scope=row[4],
                    status=CatalogConnectionStatus(row[6]),
                ),
                credential_binding_ref=row[5],
            )
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            raise _unavailable() from error


@dataclass(frozen=True, slots=True)
class PostgresCatalogInventoryReader(_PostgresCatalogAdapter):
    """Bounded keyset reads from active or cursor-retained generations."""

    def list_assets(
        self,
        workspace_id: str,
        connection_id: CatalogConnectionId,
        *,
        filters: CatalogAssetFilter,
        page_size: int,
        after: InventoryPageKey | None,
        generation: int | None,
    ) -> InventoryStorePage[CatalogAssetSummary]:
        _validate_page_size(page_size)
        assets = self._db.table("catalog_assets")
        try:
            with self._db.connect() as connection, connection.transaction():
                selected_generation, cursor_valid_until = _select_generation(
                    connection,
                    database=self._db,
                    workspace_id=workspace_id,
                    connection_id=connection_id.root,
                    generation=generation,
                )
                clauses = [
                    sql.SQL("workspace_id = %s"),
                    sql.SQL("connection_id = %s"),
                    sql.SQL("generation = %s"),
                ]
                params: list[object] = [
                    workspace_id,
                    connection_id.root,
                    selected_generation,
                ]
                if filters.platform is not None:
                    clauses.append(sql.SQL("lower(platform) = %s"))
                    params.append(filters.platform)
                if filters.schema_name is not None:
                    clauses.append(sql.SQL("lower(schema_name) = %s"))
                    params.append(filters.schema_name)
                if filters.query is not None:
                    clauses.append(sql.SQL("search_document @@ plainto_tsquery('simple', %s)"))
                    params.append(filters.query)
                if after is not None:
                    clauses.append(sql.SQL("(asset_sort_key, asset_key) > (%s, %s)"))
                    params.extend((after.sort_value, after.stable_id))
                params.append(page_size + 1)
                rows = connection.execute(
                    sql.SQL(
                        """
                        SELECT asset_id, qualified_name, display_name, platform,
                               environment, database_name, schema_name, description, field_count,
                               metadata_fingerprint, observed_at, asset_sort_key, asset_key
                        FROM {assets}
                        WHERE {where_clause}
                        ORDER BY asset_sort_key, asset_key
                        LIMIT %s
                        """
                    ).format(
                        assets=assets,
                        where_clause=sql.SQL(" AND ").join(clauses),
                    ),
                    tuple(params),
                ).fetchall()
            return _asset_page(
                rows,
                workspace_id=workspace_id,
                connection_id=connection_id,
                generation=selected_generation,
                page_size=page_size,
                cursor_valid_until=cursor_valid_until,
            )
        except CatalogInventoryError:
            raise
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def list_fields(
        self,
        asset: CatalogAssetLocator,
        *,
        filters: CatalogFieldFilter,
        page_size: int,
        after: InventoryPageKey | None,
        generation: int | None,
    ) -> InventoryStorePage[CatalogFieldSummary]:
        _validate_page_size(page_size)
        assets = self._db.table("catalog_assets")
        fields = self._db.table("catalog_fields")
        try:
            with self._db.connect() as connection, connection.transaction():
                selected_generation, cursor_valid_until = _select_generation(
                    connection,
                    database=self._db,
                    workspace_id=asset.workspace_id,
                    connection_id=asset.connection_id.root,
                    generation=generation,
                )
                asset_key = _asset_key(asset.asset_id.root)
                exists = connection.execute(
                    sql.SQL(
                        """
                        SELECT 1 FROM {assets}
                        WHERE workspace_id = %s AND connection_id = %s
                          AND generation = %s AND asset_key = %s AND asset_id = %s
                        """
                    ).format(assets=assets),
                    (
                        asset.workspace_id,
                        asset.connection_id.root,
                        selected_generation,
                        asset_key,
                        asset.asset_id.root,
                    ),
                ).fetchone()
                if exists is None:
                    raise _resource_unavailable()
                clauses = [
                    sql.SQL("workspace_id = %s"),
                    sql.SQL("connection_id = %s"),
                    sql.SQL("generation = %s"),
                    sql.SQL("asset_key = %s"),
                ]
                params: list[object] = [
                    asset.workspace_id,
                    asset.connection_id.root,
                    selected_generation,
                    asset_key,
                ]
                if filters.native_type is not None:
                    clauses.append(sql.SQL("lower(native_type) = %s"))
                    params.append(filters.native_type)
                if filters.query is not None:
                    clauses.append(sql.SQL("search_document @@ plainto_tsquery('simple', %s)"))
                    params.append(filters.query)
                if after is not None:
                    clauses.append(sql.SQL("(field_sort_key, field_key) > (%s, %s)"))
                    params.extend((after.sort_value, after.stable_id))
                params.append(page_size + 1)
                rows = connection.execute(
                    sql.SQL(
                        """
                        SELECT field_path, native_type, normalized_type, description, nullable,
                               is_part_of_key, tags, glossary_terms,
                               metadata_fingerprint, observed_at,
                               field_sort_key, field_key
                        FROM {fields}
                        WHERE {where_clause}
                        ORDER BY field_sort_key, field_key
                        LIMIT %s
                        """
                    ).format(
                        fields=fields,
                        where_clause=sql.SQL(" AND ").join(clauses),
                    ),
                    tuple(params),
                ).fetchall()
            return _field_page(
                rows,
                asset=asset,
                generation=selected_generation,
                page_size=page_size,
                cursor_valid_until=cursor_valid_until,
            )
        except CatalogInventoryError:
            raise
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            raise _unavailable() from error


@dataclass(frozen=True, slots=True)
class PostgresTenantCapacityPolicyOperator(_PostgresCatalogAdapter):
    """Migrator-only creation/revision adapter with immutable SQL-side audit."""

    application_name: str = "schemabridge-control-migrator"

    def apply(self, change: TenantCapacityPolicyChange) -> TenantCapacityPolicy:
        function = sql.SQL("{}.apply_tenant_capacity_policy").format(sql.Identifier(self.schema))
        try:
            with self._db.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT *
                        FROM {}(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """
                    ).format(function),
                    (
                        change.workspace_id,
                        change.expected_version,
                        change.connection_limit,
                        change.asset_limit,
                        change.field_limit,
                        change.api_requests_per_minute,
                        change.nonterminal_job_limit,
                        change.generation_retention_seconds,
                        change.updated_by,
                        change.confirmation.value,
                    ),
                ).fetchone()
            if row is None:
                raise _invalid_response()
            return _capacity_policy_from_row(row)
        except CatalogInventoryError:
            raise
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            if error.sqlstate == "40001":
                raise _policy_conflict() from error
            if error.sqlstate in {"22023", "23514"}:
                raise _invalid_response() from error
            raise _unavailable() from error


@dataclass(frozen=True, slots=True)
class PostgresTenantCapacityStore(_PostgresCatalogAdapter):
    """Durable policy, usage, and cross-replica fixed-window API admission."""

    operation_scope: str = "authenticated_api"

    def load_policy(self, workspace_id: str) -> TenantCapacityPolicy | None:
        policies = self._db.table("tenant_capacity_policies")
        try:
            with self._db.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT workspace_id, version, connection_limit, asset_limit,
                               field_limit, api_requests_per_minute,
                               nonterminal_job_limit, catalog_cursor_ttl_seconds,
                               generation_retention_seconds, updated_by, updated_at
                        FROM {policies}
                        WHERE workspace_id = %s
                        """
                    ).format(policies=policies),
                    (workspace_id,),
                ).fetchone()
            return None if row is None else _capacity_policy_from_row(row)
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def inspect(self, workspace_id: str) -> TenantCapacitySnapshot:
        policies = self._db.table("tenant_capacity_policies")
        connections = self._db.table("catalog_connections")
        generations = self._db.table("catalog_generations")
        capacity = self._db.table("tenant_execution_capacity")
        try:
            with self._db.connect() as connection, connection.transaction():
                policy_row = connection.execute(
                    sql.SQL(
                        """
                        SELECT workspace_id, version, connection_limit, asset_limit,
                               field_limit, api_requests_per_minute,
                               nonterminal_job_limit, catalog_cursor_ttl_seconds,
                               generation_retention_seconds, updated_by, updated_at
                        FROM {policies} WHERE workspace_id = %s
                        """
                    ).format(policies=policies),
                    (workspace_id,),
                ).fetchone()
                if policy_row is None:
                    raise CatalogInventoryError(
                        CatalogInventoryErrorCode.POLICY_MISSING,
                        "tenant capacity policy is unavailable",
                    )
                usage_row = connection.execute(
                    sql.SQL(
                        """
                        SELECT
                            count(*) FILTER (WHERE connection.status = 'enabled'),
                            coalesce(sum(generation.asset_count)
                                FILTER (WHERE connection.status = 'enabled'), 0),
                            coalesce(sum(generation.field_count)
                                FILTER (WHERE connection.status = 'enabled'), 0),
                            coalesce(capacity.nonterminal_job_count, 0),
                            clock_timestamp()
                        FROM {connections} AS connection
                        LEFT JOIN {generations} AS generation
                          ON generation.workspace_id = connection.workspace_id
                         AND generation.connection_id = connection.connection_id
                         AND generation.generation = connection.active_generation
                        LEFT JOIN {capacity} AS capacity
                          ON capacity.workspace_id = %s
                        WHERE connection.workspace_id = %s
                        GROUP BY capacity.nonterminal_job_count
                        """
                    ).format(
                        connections=connections,
                        generations=generations,
                        capacity=capacity,
                    ),
                    (workspace_id, workspace_id),
                ).fetchone()
                if usage_row is None:
                    usage_row = connection.execute(
                        sql.SQL(
                            """
                            SELECT 0, 0, 0,
                                   coalesce(nonterminal_job_count, 0),
                                   clock_timestamp()
                            FROM {capacity}
                            WHERE workspace_id = %s
                            UNION ALL
                            SELECT 0, 0, 0, 0, clock_timestamp()
                            LIMIT 1
                            """
                        ).format(capacity=capacity),
                        (workspace_id,),
                    ).fetchone()
                assert usage_row is not None
            return TenantCapacitySnapshot(
                policy=_capacity_policy_from_row(policy_row),
                usage=TenantCapacityUsage(
                    workspace_id=workspace_id,
                    connection_count=usage_row[0],
                    asset_count=usage_row[1],
                    field_count=usage_row[2],
                    nonterminal_job_count=usage_row[3],
                    observed_at=usage_row[4],
                ),
            )
        except CatalogInventoryError:
            raise
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            raise _unavailable() from error

    def admit_api_request(
        self,
        workspace_id: str,
        actor_id: str,
    ) -> CapacityAdmission:
        principal_digest = hashlib.sha256(actor_id.encode("utf-8")).hexdigest()
        try:
            with self._db.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT is_allowed, retry_after_seconds, remaining,
                               request_limit, used
                        FROM {}.admit_api_request(%s, %s, %s)
                        """
                    ).format(sql.Identifier(self.schema)),
                    (workspace_id, principal_digest, self.operation_scope),
                ).fetchone()
            if row is None:
                raise _invalid_response()
            allowed = bool(row[0])
            return CapacityAdmission(
                workspace_id=workspace_id,
                resource=CapacityResource.API_REQUEST,
                allowed=allowed,
                used=int(row[4]),
                limit=int(row[3]),
                retry_after_seconds=None if allowed else int(row[1]),
            )
        except CatalogInventoryError:
            raise
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            raise _unavailable() from error


def _load_registration_row(
    connection: psycopg.Connection[Any],
    *,
    connections: sql.Composed,
    generations: sql.Composed,
    workspace_id: str,
    connection_id: str,
    idempotency_digest: str,
    stale_after_seconds: int,
) -> tuple[Any, ...] | None:
    row = connection.execute(
        sql.SQL(
            """
            SELECT connection.registration_fingerprint,
                   connection.idempotency_digest,
                   connection.created_by_actor_id,
                   connection.workspace_id, connection.connection_id,
                   connection.display_name, connection.source_kind,
                   connection.environment, connection.catalog_scope,
                   connection.platform_instance, connection.status,
                   connection.active_generation,
                   coalesce(generation.asset_count, 0),
                   coalesce(generation.field_count, 0),
                   connection.active_generation_completed_at,
                   (
                       connection.active_generation_completed_at IS NOT NULL
                       AND connection.active_generation_completed_at
                           < clock_timestamp() - make_interval(secs => %s)
                   )
            FROM {connections} AS connection
            LEFT JOIN {generations} AS generation
              ON generation.workspace_id = connection.workspace_id
             AND generation.connection_id = connection.connection_id
             AND generation.generation = connection.active_generation
            WHERE connection.workspace_id = %s
              AND (
                  connection.connection_id = %s
                  OR connection.idempotency_digest = %s
              )
            FOR SHARE OF connection
            """
        ).format(
            connections=connections,
            generations=generations,
        ),
        (stale_after_seconds, workspace_id, connection_id, idempotency_digest),
    ).fetchone()
    return None if row is None else tuple(row)


def _registration_result_from_row(
    row: tuple[Any, ...],
    *,
    expected: CatalogConnectionRegistration,
    replayed: bool,
) -> CatalogConnectionRegistrationResult:
    if (
        row[1] != expected.idempotency_digest
        or row[2] != expected.requested_by
        or row[3] != expected.workspace_id
        or row[4] != expected.connection_id.root
        or row[5] != expected.display_name
        or row[6] != expected.kind.value
        or row[7] != expected.environment
        or row[8] != expected.catalog_scope
        or row[9] != expected.platform_instance
    ):
        raise _idempotency_conflict()
    return CatalogConnectionRegistrationResult(
        connection=_connection_from_row(tuple(row[3:])),
        replayed=replayed,
    )


def _load_connection_row(
    connection: psycopg.Connection[Any],
    *,
    connections: sql.Composed,
    generations: sql.Composed,
    workspace_id: str,
    connection_id: str,
    for_share: bool = False,
    stale_after_seconds: int,
) -> tuple[Any, ...] | None:
    lock = sql.SQL(" FOR SHARE") if for_share else sql.SQL("")
    row = connection.execute(
        sql.SQL(
            """
            SELECT connection.workspace_id, connection.connection_id,
                   connection.display_name, connection.source_kind,
                   connection.environment, connection.catalog_scope,
                   connection.platform_instance, connection.status,
                   connection.active_generation,
                   coalesce(generation.asset_count, 0),
                   coalesce(generation.field_count, 0),
                   connection.active_generation_completed_at,
                   (
                       connection.active_generation_completed_at IS NOT NULL
                       AND connection.active_generation_completed_at
                           < clock_timestamp() - make_interval(secs => %s)
                   ),
                   connection.disabled_idempotency_digest,
                   connection.disabled_by_actor_id
            FROM {connections} AS connection
            LEFT JOIN {generations} AS generation
              ON generation.workspace_id = connection.workspace_id
             AND generation.connection_id = connection.connection_id
             AND generation.generation = connection.active_generation
            WHERE connection.workspace_id = %s
              AND connection.connection_id = %s
            """
        ).format(
            connections=connections,
            generations=generations,
        )
        + (sql.SQL(" FOR SHARE OF connection") if for_share else lock),
        (stale_after_seconds, workspace_id, connection_id),
    ).fetchone()
    return None if row is None else tuple(row)


def _connection_from_row(row: tuple[Any, ...]) -> CatalogConnectionSummary:
    return CatalogConnectionSummary(
        workspace_id=row[0],
        connection_id=CatalogConnectionId(row[1]),
        display_name=row[2],
        kind=CatalogConnectionKind(row[3]),
        environment=row[4],
        catalog_scope=row[5],
        platform_instance=row[6],
        status=CatalogConnectionStatus(row[7]),
        active_generation=row[8],
        asset_count=row[9],
        field_count=row[10],
        last_completed_at=row[11],
        stale=bool(row[12]),
    )


def _connection_page(
    rows: list[tuple[Any, ...]],
    *,
    page_size: int,
) -> InventoryStorePage[CatalogConnectionSummary]:
    visible = rows[:page_size]
    items = tuple(_connection_from_row(tuple(row)) for row in visible)
    last_key = (
        None
        if not visible
        else InventoryPageKey(sort_value=visible[-1][2], stable_id=visible[-1][1])
    )
    return InventoryStorePage(
        items=items,
        resource=InventoryCursorResource.CONNECTIONS,
        generation=None,
        page_size=page_size,
        rows_read=len(rows),
        has_more=len(rows) > page_size,
        last_key=last_key,
    )


def _asset_page(
    rows: list[tuple[Any, ...]],
    *,
    workspace_id: str,
    connection_id: CatalogConnectionId,
    generation: int,
    page_size: int,
    cursor_valid_until: datetime | None,
) -> InventoryStorePage[CatalogAssetSummary]:
    visible = rows[:page_size]
    items = tuple(
        CatalogAssetSummary(
            locator=CatalogAssetLocator(
                workspace_id=workspace_id,
                connection_id=connection_id,
                asset_id=CatalogAssetId(row[0]),
            ),
            generation=generation,
            qualified_name=row[1],
            display_name=row[2],
            platform=row[3],
            environment=row[4],
            database_name=row[5],
            schema_name=row[6],
            description=row[7],
            field_count=row[8],
            metadata_fingerprint=row[9],
            observed_at=row[10],
        )
        for row in visible
    )
    last_key = (
        None
        if not visible
        else InventoryPageKey(sort_value=visible[-1][11], stable_id=visible[-1][12])
    )
    return InventoryStorePage(
        items=items,
        resource=InventoryCursorResource.ASSETS,
        generation=generation,
        page_size=page_size,
        rows_read=len(rows),
        has_more=len(rows) > page_size,
        last_key=last_key,
        cursor_valid_until=cursor_valid_until,
    )


def _field_page(
    rows: list[tuple[Any, ...]],
    *,
    asset: CatalogAssetLocator,
    generation: int,
    page_size: int,
    cursor_valid_until: datetime | None,
) -> InventoryStorePage[CatalogFieldSummary]:
    visible = rows[:page_size]
    items = tuple(
        CatalogFieldSummary(
            locator=CatalogFieldLocator(asset=asset, field_path=tuple(row[0])),
            generation=generation,
            native_type=row[1],
            normalized_type=row[2],
            description=row[3],
            nullable=row[4],
            is_part_of_key=row[5],
            tags=tuple(row[6]),
            glossary_terms=tuple(row[7]),
            metadata_fingerprint=row[8],
            observed_at=row[9],
        )
        for row in visible
    )
    last_key = (
        None
        if not visible
        else InventoryPageKey(sort_value=visible[-1][10], stable_id=visible[-1][11])
    )
    return InventoryStorePage(
        items=items,
        resource=InventoryCursorResource.FIELDS,
        generation=generation,
        page_size=page_size,
        rows_read=len(rows),
        has_more=len(rows) > page_size,
        last_key=last_key,
        cursor_valid_until=cursor_valid_until,
    )


def _select_generation(
    connection: psycopg.Connection[Any],
    *,
    database: _ControlDatabase,
    workspace_id: str,
    connection_id: str,
    generation: int | None,
) -> tuple[int, datetime | None]:
    connections = database.table("catalog_connections")
    generations = database.table("catalog_generations")
    row = connection.execute(
        sql.SQL(
            """
            SELECT selected.generation,
                   CASE
                       WHEN selected.generation = connection.active_generation
                           THEN NULL
                       ELSE selected.retain_until
                   END AS cursor_valid_until
            FROM {connections} AS connection
            JOIN {generations} AS selected
              ON selected.workspace_id = connection.workspace_id
             AND selected.connection_id = connection.connection_id
             AND selected.generation = coalesce(%s, connection.active_generation)
            WHERE connection.workspace_id = %s
              AND connection.connection_id = %s
              AND connection.status = 'enabled'
              AND selected.status = 'completed'
              AND (
                  selected.generation = connection.active_generation
                  OR selected.retain_until >= clock_timestamp()
              )
            """
        ).format(connections=connections, generations=generations),
        (generation, workspace_id, connection_id),
    ).fetchone()
    if row is None:
        raise CatalogInventoryError(
            CatalogInventoryErrorCode.GENERATION_UNAVAILABLE,
            "catalog generation is unavailable",
        )
    return int(row[0]), row[1]


def _capacity_policy_from_row(row: tuple[Any, ...]) -> TenantCapacityPolicy:
    return TenantCapacityPolicy(
        workspace_id=row[0],
        version=row[1],
        connection_limit=row[2],
        asset_limit=row[3],
        field_limit=row[4],
        api_requests_per_minute=row[5],
        nonterminal_job_limit=row[6],
        catalog_cursor_ttl_seconds=row[7],
        generation_retention_seconds=row[8],
        updated_by=row[9],
        updated_at=row[10],
    )


def _asset_key(asset_id: str) -> str:
    return hashlib.sha256(asset_id.encode("utf-8")).hexdigest()


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _validate_page_size(page_size: int) -> None:
    if not 1 <= page_size <= _MAX_PAGE_SIZE:
        raise ValueError("catalog page size is invalid")


def _classified_write_error(error: psycopg.Error) -> CatalogInventoryError:
    if error.sqlstate == "53300":
        return CatalogInventoryError(
            CatalogInventoryErrorCode.CAPACITY_EXCEEDED,
            "catalog capacity is exhausted",
        )
    if isinstance(error, UniqueViolation):
        return _idempotency_conflict()
    return CatalogInventoryError(
        CatalogInventoryErrorCode.UNAVAILABLE,
        "catalog operation could not be completed",
    )


def _idempotency_conflict() -> CatalogInventoryError:
    return CatalogInventoryError(
        CatalogInventoryErrorCode.IDEMPOTENCY_CONFLICT,
        "catalog idempotency key conflicts with an existing operation",
    )


def _policy_conflict() -> CatalogInventoryError:
    return CatalogInventoryError(
        CatalogInventoryErrorCode.POLICY_CONFLICT,
        "tenant capacity policy version conflicts with current state",
    )


def _resource_unavailable() -> CatalogInventoryError:
    return CatalogInventoryError(
        CatalogInventoryErrorCode.RESOURCE_UNAVAILABLE,
        "catalog resource is unavailable",
    )


def _invalid_response() -> CatalogInventoryError:
    return CatalogInventoryError(
        CatalogInventoryErrorCode.INVALID_RESPONSE,
        "catalog persistence response is invalid",
    )


def _unavailable() -> CatalogInventoryError:
    return CatalogInventoryError(
        CatalogInventoryErrorCode.UNAVAILABLE,
        "catalog persistence is unavailable",
    )


__all__ = [
    "PostgresCatalogConnectionStore",
    "PostgresCatalogInventoryReader",
    "PostgresCatalogRouteReader",
    "PostgresTenantCapacityPolicyOperator",
    "PostgresTenantCapacityStore",
]
