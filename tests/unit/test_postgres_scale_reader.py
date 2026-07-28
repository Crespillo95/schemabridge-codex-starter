from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from scripts.benchmark_catalog_scale import ScaleHarnessError
from scripts.postgres_catalog_scale_reader import (
    PostgresCatalogScaleReader,
    PostgresIndexPlanProbe,
    PostgresScaleCase,
    PostgresScaleConfiguration,
)

from schemabridge.adapters.catalog.cursor import SignedInventoryCursorCodec
from schemabridge.domain.catalog_inventory import (
    CatalogAssetId,
    CatalogAssetLocator,
    CatalogAssetSummary,
    CatalogConnectionId,
    InventoryCursorResource,
    InventoryPageKey,
    InventoryStorePage,
)

NOW = datetime(2026, 7, 23, 22, 0, tzinfo=UTC)
CURSOR_KEY = b"m25-postgres-scale-cursor-signing-key-diverse-0123456789"
CONNECTION_ID = CatalogConnectionId("warehouse-primary")


def _environment() -> dict[str, str]:
    return {
        "SCHEMABRIDGE_CONTROL_CATALOG_DATABASE_URL": (
            "postgresql://private-user:private-password@internal/control"
        ),
        "SCHEMABRIDGE_INVENTORY_CURSOR_SIGNING_KEY": CURSOR_KEY.decode(),
        "SCHEMABRIDGE_SCALE_SMALL_WORKSPACE_ID": "workspace-small",
        "SCHEMABRIDGE_SCALE_SMALL_CONNECTION_ID": "warehouse-small",
        "SCHEMABRIDGE_SCALE_SMALL_GENERATION": "3",
        "SCHEMABRIDGE_SCALE_LARGE_WORKSPACE_ID": "workspace-large",
        "SCHEMABRIDGE_SCALE_LARGE_CONNECTION_ID": "warehouse-large",
        "SCHEMABRIDGE_SCALE_LARGE_GENERATION": "7",
    }


def test_postgres_scale_configuration_is_environment_only_and_secret_safe() -> None:
    environ = _environment()

    configuration = PostgresScaleConfiguration.from_environment(environ)

    assert configuration.pool_max_size == 16
    assert configuration.small.generation == 3
    assert configuration.large.generation == 7
    rendered = repr(configuration)
    assert environ["SCHEMABRIDGE_CONTROL_CATALOG_DATABASE_URL"] not in rendered
    assert environ["SCHEMABRIDGE_INVENTORY_CURSOR_SIGNING_KEY"] not in rendered
    assert environ["SCHEMABRIDGE_SCALE_SMALL_WORKSPACE_ID"] not in rendered
    assert environ["SCHEMABRIDGE_SCALE_LARGE_CONNECTION_ID"] not in rendered


def test_postgres_scale_configuration_rejects_missing_or_invalid_values_safely() -> None:
    missing = _environment()
    secret = missing.pop("SCHEMABRIDGE_CONTROL_CATALOG_DATABASE_URL")

    with pytest.raises(ScaleHarnessError) as captured:
        PostgresScaleConfiguration.from_environment(missing)
    assert secret not in str(captured.value)

    invalid = _environment()
    invalid["SCHEMABRIDGE_SCALE_LARGE_GENERATION"] = "-1"
    with pytest.raises(ScaleHarnessError, match="invalid"):
        PostgresScaleConfiguration.from_environment(invalid)


@dataclass
class _Inventory:
    calls: list[InventoryPageKey | None]

    def list_assets(
        self,
        workspace_id: str,
        connection_id: CatalogConnectionId,
        *,
        filters: object,
        page_size: int,
        after: InventoryPageKey | None,
        generation: int | None,
    ) -> InventoryStorePage[CatalogAssetSummary]:
        del filters
        assert workspace_id == "workspace-large"
        assert connection_id == CONNECTION_ID
        assert generation == 7
        assert page_size == 2
        self.calls.append(after)
        if after is None:
            items = (_asset(0), _asset(1))
            rows_read = 3
            has_more = True
            last_key = InventoryPageKey(
                sort_value="scale.synthetic-asset-00000001",
                stable_id="b" * 64,
            )
        else:
            items = (_asset(2),)
            rows_read = 1
            has_more = False
            last_key = InventoryPageKey(
                sort_value="scale.synthetic-asset-00000002",
                stable_id="c" * 64,
            )
        return InventoryStorePage(
            items=items,
            resource=InventoryCursorResource.ASSETS,
            generation=7,
            page_size=2,
            rows_read=rows_read,
            has_more=has_more,
            last_key=last_key,
        )


class _TimedProvider:
    def __init__(self) -> None:
        self.waits = [1.25, 0.75]

    def take_last_wait_milliseconds(self) -> float:
        return self.waits.pop(0)


class _Pool:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _PlanProbe:
    def inspect(self) -> dict[str, object]:
        return {
            "status": "reviewed_postgres_index_plan",
            "passed": True,
        }


def test_postgres_reader_round_trips_signed_keyset_cursor_and_limit_plus_one() -> None:
    inventory = _Inventory(calls=[])
    provider = _TimedProvider()
    pool = _Pool()
    reader = PostgresCatalogScaleReader(
        inventory=inventory,
        cursors=SignedInventoryCursorCodec(signing_key=CURSOR_KEY),
        connection_provider=provider,  # type: ignore[arg-type]
        plan_probe=_PlanProbe(),  # type: ignore[arg-type]
        pool=pool,
        cases={
            "large": PostgresScaleCase(
                workspace_id="workspace-large",
                connection_id=CONNECTION_ID,
                generation=7,
            )
        },
        pool_max_size=16,
        replica_count=2,
    )

    first = reader.read_page("large", page_size=2, cursor=None)
    second = reader.read_page("large", page_size=2, cursor=first.next_cursor)

    assert first.item_keys == (
        "synthetic-asset-00000000",
        "synthetic-asset-00000001",
    )
    assert first.rows_read == 3
    assert first.next_cursor is not None
    assert first.pool_wait_milliseconds == 1.25
    assert second.item_keys == ("synthetic-asset-00000002",)
    assert second.next_cursor is None
    assert second.pool_wait_milliseconds == 0.75
    assert inventory.calls == [
        None,
        InventoryPageKey(
            sort_value="scale.synthetic-asset-00000001",
            stable_id="b" * 64,
        ),
    ]
    assert reader.metadata.evidence_profile == "postgres-acceptance"
    assert reader.metadata.indexed_database_read is True
    assert reader.metadata.pool_max_size == 16
    assert reader.metadata.replica_count == 2
    reader.close()
    assert pool.closed is True


class _Result:
    def __init__(self, value: object) -> None:
        self._value = value

    def fetchone(self) -> tuple[object]:
        return (self._value,)


class _PlanConnection:
    def __init__(self, plans: list[object]) -> None:
        self.plans = plans
        self.parameter_counts: list[int] = []
        self.control_statements: list[str] = []

    def execute(
        self,
        statement: object,
        parameters: tuple[object, ...] | None = None,
    ) -> _Result:
        if parameters is None:
            self.control_statements.append(str(statement))
            return _Result(None)
        self.parameter_counts.append(len(parameters))
        return _Result(self.plans.pop(0))


class _PlanProvider:
    def __init__(self, connection: _PlanConnection) -> None:
        self.connection_value = connection

    @contextmanager
    def connection(self) -> Iterator[_PlanConnection]:
        yield self.connection_value


def _plan(index_name: str) -> list[dict[str, object]]:
    return [
        {
            "Plan": {
                "Node Type": "Limit",
                "Plans": [
                    {
                        "Node Type": "Index Scan",
                        "Index Name": index_name,
                    }
                ],
            }
        }
    ]


def test_plan_probe_requires_both_keyset_indexes_and_returns_only_sanitized_evidence() -> None:
    connection = _PlanConnection(
        [
            64,
            _plan("catalog_assets_keyset_idx"),
            _plan("catalog_fields_keyset_idx"),
        ]
    )
    evidence = PostgresIndexPlanProbe(
        connection_provider=_PlanProvider(connection),
        large_case=PostgresScaleCase(
            workspace_id="workspace-private",
            connection_id=CONNECTION_ID,
            generation=7,
        ),
    ).inspect()

    assert evidence["passed"] is True
    assert evidence["index_used"] == {"assets": True, "fields": True}
    assert evidence["forbidden_nodes"] == []
    assert connection.parameter_counts == [4, 6, 7]
    assert connection.control_statements == []
    assert evidence["planner_control"] == "default_postgres_planner"
    assert evidence["field_probe_field_count"] == 64
    encoded = json.dumps(evidence, sort_keys=True)
    assert "workspace-private" not in encoded
    assert "warehouse-primary" not in encoded
    assert "SELECT" not in encoded
    assert "parameters" in encoded
    assert "private-password" not in encoded


def test_plan_probe_fails_evidence_when_expected_index_is_missing() -> None:
    connection = _PlanConnection(
        [
            64,
            _plan("catalog_assets_keyset_idx"),
            _plan("catalog_fields_exact_lookup_idx"),
        ]
    )
    evidence = PostgresIndexPlanProbe(
        connection_provider=_PlanProvider(connection),
        large_case=PostgresScaleCase(
            workspace_id="workspace-private",
            connection_id=CONNECTION_ID,
            generation=7,
        ),
    ).inspect()

    assert evidence["passed"] is False
    assert evidence["index_used"] == {"assets": True, "fields": False}


def test_plan_probe_rejects_sort_even_when_expected_indexes_are_present() -> None:
    sorted_field_plan = _plan("catalog_fields_keyset_idx")
    sorted_field_plan[0]["Plan"] = {
        "Node Type": "Sort",
        "Plans": [
            {
                "Node Type": "Index Scan",
                "Index Name": "catalog_fields_keyset_idx",
            }
        ],
    }
    connection = _PlanConnection(
        [
            64,
            _plan("catalog_assets_keyset_idx"),
            sorted_field_plan,
        ]
    )

    evidence = PostgresIndexPlanProbe(
        connection_provider=_PlanProvider(connection),
        large_case=PostgresScaleCase(
            workspace_id="workspace-private",
            connection_id=CONNECTION_ID,
            generation=7,
        ),
    ).inspect()

    assert evidence["index_used"] == {"assets": True, "fields": True}
    assert evidence["forbidden_nodes"] == ["Sort"]
    assert evidence["passed"] is False


def _asset(index: int) -> CatalogAssetSummary:
    asset_id = f"synthetic-asset-{index:08d}"
    return CatalogAssetSummary(
        locator=CatalogAssetLocator(
            workspace_id="workspace-large",
            connection_id=CONNECTION_ID,
            asset_id=CatalogAssetId(asset_id),
        ),
        generation=7,
        qualified_name=f"scale.{asset_id}",
        display_name=asset_id,
        platform="postgres",
        environment="PROD",
        schema_name="scale",
        field_count=4,
        metadata_fingerprint="a" * 64,
        observed_at=NOW,
    )
