"""DataHub-only registry to guarded PostgreSQL acceptance evidence for M22."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from schemabridge.adapters.semantic_registry.datahub import (
    DataHubGovernedSemanticRegistry,
)
from schemabridge.adapters.sql.guard import SqlGlotPolicyGuard
from schemabridge.application.guided_requests import BuildGuidedRequest
from schemabridge.application.ports.planning import (
    PlanningPortError,
    PlanningPortErrorCode,
)
from schemabridge.application.postgres_health import DatabaseUnavailableError
from schemabridge.bootstrap import (
    build_governed_request_executor,
    build_governed_request_preparer,
    build_guided_request_builder,
    build_postgres_health_check,
    build_semantic_registry,
)
from schemabridge.config import Settings
from schemabridge.domain.requests import AnalyticalRequest

pytestmark = pytest.mark.acceptance

ROOT = Path(__file__).resolve().parents[2]
READER_CREDENTIALS = ROOT / ".local/datahub/mcp.env"
DEFAULT_READER_DSN = (
    "postgresql://schemabridge_reader:schemabridge_reader@127.0.0.1:55433/schemabridge"
)
GROUND_TRUTH_PATH = ROOT / "demo/ground_truth/query_cases.yml"


@pytest.mark.parametrize(
    "case_id",
    (
        "secondary_holders_by_registration_date",
        "commerce_revenue_by_category_month",
    ),
)
def test_live_datahub_registry_executes_exact_ground_truth_without_recorded_fallback(
    case_id: str,
    tmp_path: Path,
) -> None:
    if not READER_CREDENTIALS.is_file():
        pytest.skip(
            "DataHub reader credentials are absent; run make datahub-provision-mcp "
            "and publish the approved M22 registry"
        )

    unavailable_manifest = tmp_path / "recorded-registry-must-not-be-read.yml"
    settings = Settings.model_validate(
        {
            "environment": "development",
            "auth_mode": "local-demo",
            "catalog_kind": "live",
            "registry_kind": "live",
            "judge_execution_kind": "live",
            "allow_local_live_reads": True,
            "database_url": os.environ.get(
                "SCHEMABRIDGE_TEST_DATABASE_URL",
                DEFAULT_READER_DSN,
            ),
            "draft_store_path": tmp_path / "acceptance-control-plane.db",
            "semantic_registry_manifest_path": unavailable_manifest,
            "semantic_registry_reader_env_path": READER_CREDENTIALS,
            "semantic_registry_version": 1,
            "max_query_tables": 3,
            "max_query_rows": 500,
            "statement_timeout_ms": 5_000,
            "postgres_reader_user": "schemabridge_reader",
        }
    )
    assert settings.catalog_mode == settings.registry_mode == settings.execution_mode == "live"

    try:
        health = build_postgres_health_check(settings).execute()
    except DatabaseUnavailableError:
        pytest.skip("Synthetic PostgreSQL is unavailable; run make demo-up")
    assert health.is_ready, health.findings

    registry = build_semantic_registry(repository_root=ROOT, settings=settings)
    assert isinstance(registry, DataHubGovernedSemanticRegistry)
    assert not unavailable_manifest.exists()
    try:
        scoped = registry.load()
    except PlanningPortError as error:
        if error.code is PlanningPortErrorCode.CONTEXT_UNAVAILABLE:
            pytest.skip("Local DataHub is unavailable; run make datahub-start")
        raise

    assert scoped.registry.source.startswith("datahub:")
    assert scoped.registry.logical_context.source.startswith("datahub:")
    assert "recorded" not in scoped.registry.source
    assert len(scoped.registry.logical_context.models) == 7
    assert len(scoped.registry.mapping_set.mappings) == 31
    assert len(scoped.registry.join_contracts.contracts) == 5

    builder = build_guided_request_builder(
        repository_root=ROOT,
        settings=settings,
        registry=registry,
    )
    assert isinstance(builder, BuildGuidedRequest)
    prepare = build_governed_request_preparer(
        repository_root=ROOT,
        settings=settings,
        registry=registry,
    )
    assert isinstance(prepare.guard, SqlGlotPolicyGuard)
    executor = build_governed_request_executor(
        execution_kind="live",
        repository_root=ROOT,
        settings=settings,
        prepare=prepare,
    )

    case = _ground_truth_case(case_id)
    request = AnalyticalRequest.model_validate(case["interpretation"])
    validated = builder.validate(request)
    prepared = prepare.execute(validated)
    result = executor.execute_prepared(prepared)

    expected_columns, expected_rows = _expected_rows(case)
    expected_rejections = _expected_rejections(case)
    expected_contracts = tuple(str(item) for item in _list(case["join_contracts"]))

    assert result.policy_status == "accepted"
    assert result.policy_findings == ()
    assert result.resolved_plan.context_source.startswith("datahub:")
    assert tuple(contract.id for contract in result.resolved_plan.selected_contracts) == (
        expected_contracts
    )
    assert result.preview.columns == expected_columns
    assert result.preview.rows == expected_rows
    assert (
        tuple(
            (record.source_value, record.code.value) for record in result.rejected_sources.records
        )
        == expected_rejections
    )

    policy = result.resolved_plan.query_policy
    table_count = 1 + len(result.resolved_plan.query_plan.joins)
    assert policy.max_tables == settings.max_query_tables == 3
    assert table_count == len(_list(case["required_models"]))
    assert table_count <= policy.max_tables
    assert prepared.query.max_rows == request.limit <= settings.max_query_rows
    assert prepared.query.statement_timeout_ms == settings.statement_timeout_ms == 5_000
    assert result.preview.truncated is False

    assert result.preview.database_user == settings.postgres_reader_user == "schemabridge_reader"
    assert result.preview.transaction_read_only is True
    assert result.preview.statement_timeout_ms == settings.statement_timeout_ms
    assert result.rejected_sources.database_user == settings.postgres_reader_user
    assert result.rejected_sources.transaction_read_only is True
    assert result.rejected_sources.statement_timeout_ms == settings.statement_timeout_ms
    assert not unavailable_manifest.exists()


def _ground_truth_case(case_id: str) -> dict[str, Any]:
    payload = yaml.safe_load(GROUND_TRUTH_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    cases = _list(payload["cases"])
    case = next(item for item in cases if isinstance(item, dict) and item.get("id") == case_id)
    assert isinstance(case, dict)
    return {str(key): value for key, value in case.items()}


def _expected_rows(case: dict[str, Any]) -> tuple[tuple[str, ...], tuple[tuple[object, ...], ...]]:
    raw_rows = _list(case["expected_rows"])
    assert raw_rows
    assert all(isinstance(row, dict) for row in raw_rows)
    first = raw_rows[0]
    assert isinstance(first, dict)
    columns = tuple(str(column) for column in first)
    rows = tuple(
        tuple(_expected_cell(row[column]) for column in columns)
        for row in raw_rows
        if isinstance(row, dict)
    )
    assert len(rows) == len(raw_rows)
    return columns, rows


def _expected_rejections(case: dict[str, Any]) -> tuple[tuple[str | None, str], ...]:
    return tuple(
        (
            None if item["source_value"] is None else str(item["source_value"]),
            str(item["reason"]),
        )
        for item in _list(case["expected_rejections"])
        if isinstance(item, dict)
    )


def _expected_cell(value: object) -> object:
    if not isinstance(value, dict):
        return value
    assert set(value) == {"decimal"}
    return Decimal(str(value["decimal"]))


def _list(value: object) -> list[Any]:
    assert isinstance(value, list)
    return value
