"""Environment-only PostgreSQL reader for the M25 catalog scale acceptance harness.

The no-argument factory is intentionally the only production-facing seam. It opens the bounded
catalog control-plane pool only when called by the acceptance command. Unit tests inject doubles
and never require a database.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from psycopg import sql

from schemabridge.adapters.catalog.cursor import SignedInventoryCursorCodec
from schemabridge.adapters.catalog.postgres_inventory import (
    PostgresCatalogInventoryReader,
)
from schemabridge.adapters.control_plane.postgres_pool import (
    ControlPoolSettings,
    PostgresControlPool,
)
from schemabridge.domain.catalog_inventory import (
    CatalogAssetFilter,
    CatalogConnectionId,
    InventoryCursorBinding,
    InventoryCursorResource,
    InventoryPageKey,
    InventoryStorePage,
    inventory_filter_fingerprint,
)
from scripts.benchmark_catalog_scale import (
    ScaleHarnessError,
    ScalePage,
    ScaleReaderMetadata,
)

_ASSET_SORT_FINGERPRINT = inventory_filter_fingerprint(
    {"keys": ["qualified_name", "asset_id"], "version": 1}
)
_EMPTY_ASSET_FILTER = CatalogAssetFilter()
_EXPECTED_INDEXES = {
    "assets": "catalog_assets_keyset_idx",
    "fields": "catalog_fields_keyset_idx",
}
_FORBIDDEN_PLAN_NODES = frozenset(
    {
        "Incremental Sort",
        "Parallel Seq Scan",
        "Seq Scan",
        "Sort",
    }
)
_PLAN_PAGE_SIZE = 17
_WIDE_LARGE_ASSET_ID = "synthetic-asset-00000996"
_CONTROL_SCHEMA = "schemabridge_control"


class _InventoryReader(Protocol):
    def list_assets(
        self,
        workspace_id: str,
        connection_id: CatalogConnectionId,
        *,
        filters: CatalogAssetFilter,
        page_size: int,
        after: InventoryPageKey | None,
        generation: int | None,
    ) -> InventoryStorePage[Any]:
        """Read one bounded exact-generation page."""


class _ConnectionProvider(Protocol):
    def connection(self) -> AbstractContextManager[Any]:
        """Acquire one bounded PostgreSQL connection."""


class _Pool(Protocol):
    def close(self) -> None:
        """Close the bounded pool idempotently."""


@dataclass(frozen=True, slots=True)
class PostgresScaleCase:
    """Protected exact generation behind one public small/large case name."""

    workspace_id: str = field(repr=False)
    connection_id: CatalogConnectionId = field(repr=False)
    generation: int

    def __post_init__(self) -> None:
        try:
            InventoryCursorBinding(
                workspace_id=self.workspace_id,
                resource=InventoryCursorResource.ASSETS,
                connection_id=self.connection_id,
                generation=self.generation,
                filter_fingerprint=_EMPTY_ASSET_FILTER.fingerprint,
                sort_fingerprint=_ASSET_SORT_FINGERPRINT,
            )
        except (TypeError, ValueError) as error:
            raise ScaleHarnessError("PostgreSQL scale case is invalid") from error


@dataclass(frozen=True, slots=True)
class PostgresScaleConfiguration:
    """Validated environment configuration whose representation omits protected values."""

    dsn: str = field(repr=False)
    cursor_signing_key: bytes = field(repr=False)
    small: PostgresScaleCase = field(repr=False)
    large: PostgresScaleCase = field(repr=False)
    pool_min_size: int = 1
    pool_max_size: int = 16
    pool_max_waiting: int = 32
    pool_acquisition_timeout_seconds: float = 1.0
    pool_startup_timeout_seconds: float = 15.0
    statement_timeout_ms: int = 5_000
    replica_count: int = 1

    def __post_init__(self) -> None:
        try:
            ControlPoolSettings(
                dsn=self.dsn,
                application_name="schemabridge-control-catalog",
                min_size=self.pool_min_size,
                max_size=self.pool_max_size,
                max_waiting=self.pool_max_waiting,
                acquisition_timeout_seconds=self.pool_acquisition_timeout_seconds,
                startup_timeout_seconds=self.pool_startup_timeout_seconds,
                statement_timeout_ms=self.statement_timeout_ms,
            )
            SignedInventoryCursorCodec(signing_key=self.cursor_signing_key)
            if self.small == self.large or not 1 <= self.replica_count <= 1_000:
                raise ValueError
        except (TypeError, ValueError) as error:
            raise ScaleHarnessError("PostgreSQL scale environment is invalid") from error

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str],
    ) -> PostgresScaleConfiguration:
        """Read the acceptance binding without accepting secrets through argv."""

        try:
            dsn = _required(environ, "SCHEMABRIDGE_CONTROL_CATALOG_DATABASE_URL")
            signing_key = _required(
                environ,
                "SCHEMABRIDGE_INVENTORY_CURSOR_SIGNING_KEY",
            ).encode("utf-8")
            small = _case_from_environment(environ, "SMALL")
            large = _case_from_environment(environ, "LARGE")
            return cls(
                dsn=dsn,
                cursor_signing_key=signing_key,
                small=small,
                large=large,
                pool_min_size=_integer(environ, "SCHEMABRIDGE_SCALE_POOL_MIN_SIZE", 1),
                pool_max_size=_integer(environ, "SCHEMABRIDGE_SCALE_POOL_MAX_SIZE", 16),
                pool_max_waiting=_integer(
                    environ,
                    "SCHEMABRIDGE_SCALE_POOL_MAX_WAITING",
                    32,
                ),
                pool_acquisition_timeout_seconds=_floating(
                    environ,
                    "SCHEMABRIDGE_SCALE_POOL_ACQUISITION_TIMEOUT_SECONDS",
                    1.0,
                ),
                pool_startup_timeout_seconds=_floating(
                    environ,
                    "SCHEMABRIDGE_SCALE_POOL_STARTUP_TIMEOUT_SECONDS",
                    15.0,
                ),
                statement_timeout_ms=_integer(
                    environ,
                    "SCHEMABRIDGE_SCALE_STATEMENT_TIMEOUT_MS",
                    5_000,
                ),
                replica_count=_integer(
                    environ,
                    "SCHEMABRIDGE_SCALE_REPLICA_COUNT",
                    1,
                ),
            )
        except ScaleHarnessError:
            raise
        except (TypeError, ValueError, UnicodeError) as error:
            raise ScaleHarnessError("PostgreSQL scale environment is invalid") from error


class TimedConnectionProvider:
    """Record acquisition wait per thread without exposing pool internals."""

    def __init__(self, pool: PostgresControlPool) -> None:
        self._pool = pool
        self._local = threading.local()

    @contextmanager
    def connection(self) -> Iterator[Any]:
        started = time.perf_counter_ns()
        with self._pool.connection() as connection:
            self._local.last_wait_milliseconds = (time.perf_counter_ns() - started) / 1_000_000
            yield connection

    def take_last_wait_milliseconds(self) -> float | None:
        observed = cast(
            float | None,
            getattr(self._local, "last_wait_milliseconds", None),
        )
        if hasattr(self._local, "last_wait_milliseconds"):
            del self._local.last_wait_milliseconds
        return observed


@dataclass(slots=True)
class PostgresIndexPlanProbe:
    """Review sanitized PostgreSQL plans for both M25 keyset indexes."""

    connection_provider: _ConnectionProvider = field(repr=False)
    large_case: PostgresScaleCase = field(repr=False)

    def inspect(self) -> Mapping[str, object]:
        asset_key = hashlib.sha256(_WIDE_LARGE_ASSET_ID.encode("utf-8")).hexdigest()
        with self.connection_provider.connection() as connection:
            field_count_row = connection.execute(
                sql.SQL(
                    """
                    SELECT field_count
                    FROM {assets}
                    WHERE workspace_id = %s
                      AND connection_id = %s
                      AND generation = %s
                      AND asset_id = %s
                    """
                ).format(
                    assets=sql.SQL("{}.{}").format(
                        sql.Identifier(_CONTROL_SCHEMA),
                        sql.Identifier("catalog_assets"),
                    )
                ),
                (
                    self.large_case.workspace_id,
                    self.large_case.connection_id.root,
                    self.large_case.generation,
                    _WIDE_LARGE_ASSET_ID,
                ),
            ).fetchone()
            if (
                field_count_row is None
                or type(field_count_row[0]) is not int
                or not 64 <= field_count_row[0] <= 1_000
            ):
                raise ScaleHarnessError(
                    "PostgreSQL field-plan probe requires the sparse wide fixture"
                )
            field_probe_field_count = field_count_row[0]
            assets = _execute_plan(
                connection,
                sql.SQL(
                    """
                    EXPLAIN (FORMAT JSON, ANALYZE FALSE, COSTS TRUE)
                    SELECT asset_id, qualified_name, display_name, platform,
                           environment, schema_name, description, field_count,
                           metadata_fingerprint, observed_at, asset_sort_key, asset_key
                    FROM {assets}
                    WHERE workspace_id = %s
                      AND connection_id = %s
                      AND generation = %s
                      AND (asset_sort_key, asset_key) > (%s, %s)
                    ORDER BY asset_sort_key, asset_key
                    LIMIT %s
                    """
                ).format(
                    assets=sql.SQL("{}.{}").format(
                        sql.Identifier(_CONTROL_SCHEMA),
                        sql.Identifier("catalog_assets"),
                    )
                ),
                (
                    self.large_case.workspace_id,
                    self.large_case.connection_id.root,
                    self.large_case.generation,
                    "",
                    "",
                    _PLAN_PAGE_SIZE + 1,
                ),
            )
            fields = _execute_plan(
                connection,
                sql.SQL(
                    """
                    EXPLAIN (FORMAT JSON, ANALYZE FALSE, COSTS TRUE)
                    SELECT field_path, native_type, description, nullable,
                           is_part_of_key, tags, glossary_terms,
                           metadata_fingerprint, observed_at,
                           field_sort_key, field_key
                    FROM {fields}
                    WHERE workspace_id = %s
                      AND connection_id = %s
                      AND generation = %s
                      AND asset_key = %s
                      AND (field_sort_key, field_key) > (%s, %s)
                    ORDER BY field_sort_key, field_key
                    LIMIT %s
                    """
                ).format(
                    fields=sql.SQL("{}.{}").format(
                        sql.Identifier(_CONTROL_SCHEMA),
                        sql.Identifier("catalog_fields"),
                    )
                ),
                (
                    self.large_case.workspace_id,
                    self.large_case.connection_id.root,
                    self.large_case.generation,
                    asset_key,
                    "",
                    "",
                    _PLAN_PAGE_SIZE + 1,
                ),
            )

        summaries = {
            "assets": _summarize_plan(assets),
            "fields": _summarize_plan(fields),
        }
        index_used = {
            resource: expected in cast(tuple[str, ...], summaries[resource]["index_names"])
            for resource, expected in _EXPECTED_INDEXES.items()
        }
        forbidden_nodes = sorted(
            {
                node
                for summary in summaries.values()
                for node in cast(tuple[str, ...], summary["node_types"])
                if node in _FORBIDDEN_PLAN_NODES
            }
        )
        passed = all(index_used.values()) and not forbidden_nodes
        return {
            "status": "reviewed_postgres_index_plan",
            "passed": passed,
            "sql_recorded": False,
            "parameters_recorded": False,
            "planner_control": "default_postgres_planner",
            "field_probe_field_count": field_probe_field_count,
            "expected_indexes": dict(_EXPECTED_INDEXES),
            "index_used": index_used,
            "node_types": {
                resource: list(cast(tuple[str, ...], summary["node_types"]))
                for resource, summary in summaries.items()
            },
            "forbidden_nodes": forbidden_nodes,
            "plan_digests": {
                resource: cast(str, summary["digest"]) for resource, summary in summaries.items()
            },
        }


@dataclass(slots=True)
class PostgresCatalogScaleReader:
    """Signed-cursor adapter over the exact PostgreSQL M25 inventory reader."""

    inventory: _InventoryReader = field(repr=False)
    cursors: SignedInventoryCursorCodec = field(repr=False)
    connection_provider: TimedConnectionProvider = field(repr=False)
    plan_probe: PostgresIndexPlanProbe = field(repr=False)
    pool: _Pool = field(repr=False)
    cases: Mapping[str, PostgresScaleCase] = field(repr=False)
    pool_max_size: int
    replica_count: int

    @property
    def metadata(self) -> ScaleReaderMetadata:
        return ScaleReaderMetadata(
            label="postgres-catalog-v4",
            backend_kind="postgresql-keyset",
            evidence_profile="postgres-acceptance",
            indexed_database_read=True,
            pool_max_size=self.pool_max_size,
            replica_count=self.replica_count,
            cache_context="warm-after-correctness",
        )

    def read_page(
        self,
        case: str,
        *,
        page_size: int,
        cursor: str | None,
    ) -> ScalePage:
        selected = self.cases.get(case)
        if selected is None:
            raise ScaleHarnessError("unknown scale case")
        binding = InventoryCursorBinding(
            workspace_id=selected.workspace_id,
            resource=InventoryCursorResource.ASSETS,
            connection_id=selected.connection_id,
            generation=selected.generation,
            filter_fingerprint=_EMPTY_ASSET_FILTER.fingerprint,
            sort_fingerprint=_ASSET_SORT_FINGERPRINT,
        )
        now = datetime.now(UTC)
        continuation = (
            None
            if cursor is None
            else self.cursors.decode(
                cursor=cursor,
                expected_binding=binding,
                at=now,
            )
        )
        page = self.inventory.list_assets(
            selected.workspace_id,
            selected.connection_id,
            filters=_EMPTY_ASSET_FILTER,
            page_size=page_size,
            after=None if continuation is None else continuation.last_key,
            generation=selected.generation,
        )
        if (
            page.resource is not InventoryCursorResource.ASSETS
            or page.generation != selected.generation
            or page.page_size != page_size
        ):
            raise ScaleHarnessError("PostgreSQL scale page is malformed")
        next_cursor = (
            self.cursors.encode(
                binding=binding,
                last_key=_required_last_key(page),
                issued_at=now if continuation is None else continuation.issued_at,
                not_after=page.cursor_valid_until,
            )
            if page.has_more
            else None
        )
        return ScalePage(
            item_keys=tuple(item.locator.asset_id.root for item in page.items),
            next_cursor=next_cursor,
            rows_read=page.rows_read,
            pool_wait_milliseconds=self.connection_provider.take_last_wait_milliseconds(),
        )

    def explain_index_plan(self) -> Mapping[str, object]:
        return self.plan_probe.inspect()

    def close(self) -> None:
        self.pool.close()


def create_postgres_reader() -> PostgresCatalogScaleReader:
    """Build and open the real acceptance reader from environment-only bindings."""

    configuration = PostgresScaleConfiguration.from_environment(os.environ)
    pool = PostgresControlPool(
        ControlPoolSettings(
            dsn=configuration.dsn,
            application_name="schemabridge-control-catalog",
            min_size=configuration.pool_min_size,
            max_size=configuration.pool_max_size,
            max_waiting=configuration.pool_max_waiting,
            acquisition_timeout_seconds=(configuration.pool_acquisition_timeout_seconds),
            startup_timeout_seconds=configuration.pool_startup_timeout_seconds,
            statement_timeout_ms=configuration.statement_timeout_ms,
        )
    )
    try:
        pool.open()
        provider = TimedConnectionProvider(pool)
        return PostgresCatalogScaleReader(
            inventory=PostgresCatalogInventoryReader(
                configuration.dsn,
                application_name="schemabridge-control-catalog",
                connection_provider=provider,
            ),
            cursors=SignedInventoryCursorCodec(
                signing_key=configuration.cursor_signing_key,
            ),
            connection_provider=provider,
            plan_probe=PostgresIndexPlanProbe(
                connection_provider=provider,
                large_case=configuration.large,
            ),
            pool=pool,
            cases={"small": configuration.small, "large": configuration.large},
            pool_max_size=configuration.pool_max_size,
            replica_count=configuration.replica_count,
        )
    except Exception:
        pool.close()
        raise


def _case_from_environment(
    environ: Mapping[str, str],
    label: str,
) -> PostgresScaleCase:
    return PostgresScaleCase(
        workspace_id=_required(
            environ,
            f"SCHEMABRIDGE_SCALE_{label}_WORKSPACE_ID",
        ),
        connection_id=CatalogConnectionId(
            _required(
                environ,
                f"SCHEMABRIDGE_SCALE_{label}_CONNECTION_ID",
            )
        ),
        generation=_integer(
            environ,
            f"SCHEMABRIDGE_SCALE_{label}_GENERATION",
            None,
        ),
    )


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if value is None or not value.strip() or value != value.strip():
        raise ScaleHarnessError("PostgreSQL scale environment is incomplete")
    return value


def _integer(
    environ: Mapping[str, str],
    name: str,
    default: int | None,
) -> int:
    raw = environ.get(name)
    if raw is None:
        if default is None:
            raise ScaleHarnessError("PostgreSQL scale environment is incomplete")
        return default
    if not raw.isascii() or not raw.isdecimal():
        raise ScaleHarnessError("PostgreSQL scale environment is invalid")
    return int(raw)


def _floating(
    environ: Mapping[str, str],
    name: str,
    default: float,
) -> float:
    raw = environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as error:
        raise ScaleHarnessError("PostgreSQL scale environment is invalid") from error


def _required_last_key(page: InventoryStorePage[Any]) -> InventoryPageKey:
    if page.last_key is None:
        raise ScaleHarnessError("PostgreSQL scale page is malformed")
    return page.last_key


def _execute_plan(
    connection: Any,
    statement: sql.Composed,
    parameters: tuple[object, ...],
) -> Mapping[str, object]:
    row = connection.execute(statement, parameters).fetchone()
    if row is None or len(row) != 1:
        raise ScaleHarnessError("PostgreSQL index plan is unavailable")
    value = row[0]
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise ScaleHarnessError("PostgreSQL index plan is unavailable") from error
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if not isinstance(value, Mapping):
        raise ScaleHarnessError("PostgreSQL index plan is unavailable")
    plan = value.get("Plan")
    if not isinstance(plan, Mapping):
        raise ScaleHarnessError("PostgreSQL index plan is unavailable")
    return cast(Mapping[str, object], plan)


def _summarize_plan(plan: Mapping[str, object]) -> dict[str, object]:
    node_types: set[str] = set()
    index_names: set[str] = set()
    pending = [plan]
    while pending:
        node = pending.pop()
        node_type = node.get("Node Type")
        if not isinstance(node_type, str):
            raise ScaleHarnessError("PostgreSQL index plan is unavailable")
        node_types.add(node_type)
        index_name = node.get("Index Name")
        if index_name is not None:
            if not isinstance(index_name, str):
                raise ScaleHarnessError("PostgreSQL index plan is unavailable")
            index_names.add(index_name)
        children = node.get("Plans", [])
        if not isinstance(children, list) or any(
            not isinstance(child, Mapping) for child in children
        ):
            raise ScaleHarnessError("PostgreSQL index plan is unavailable")
        pending.extend(cast(list[Mapping[str, object]], children))
    safe_summary = {
        "index_names": sorted(index_names),
        "node_types": sorted(node_types),
    }
    digest = hashlib.sha256(
        json.dumps(
            safe_summary,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    ).hexdigest()
    return {
        "index_names": tuple(safe_summary["index_names"]),
        "node_types": tuple(safe_summary["node_types"]),
        "digest": digest,
    }
