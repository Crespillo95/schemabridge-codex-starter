"""All five governed query cases through target-bound cost and preview controls."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from schemabridge.adapters.postgres.cost_preflight import (
    PsycopgQueryCostPreflight,
    _BoundedExplainJsonLoader,
    _ExplainResponseBytesExceeded,
)
from schemabridge.adapters.postgres.preview import PsycopgQueryPreview
from schemabridge.adapters.postgres.rejections import PsycopgRejectedSourceReporter
from schemabridge.application.query_cost import AssessGovernedQueryCost
from schemabridge.bootstrap import build_evaluation_runner
from schemabridge.config import Settings
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    MAX_EXPLAIN_RESPONSE_BYTES,
    GovernedExecutionTarget,
    QueryCostBudget,
    QueryCostDecision,
    SourceConnectorKind,
    SourceDialect,
    postgres_source_identity_fingerprint,
    postgres_type_contract_fingerprint,
)
from schemabridge.domain.evaluation import normalize_preview_rows

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def test_all_five_cases_keep_exact_results_under_two_accepted_preflights(
    reader_dsn: str,
    tmp_path: Path,
) -> None:
    settings = Settings.model_validate(
        {
            "DATABASE_URL": reader_dsn,
            "SCHEMABRIDGE_DRAFT_STORE_PATH": tmp_path / "m28-query-cases.db",
        }
    )
    runner = build_evaluation_runner(repository_root=ROOT, settings=settings)
    truth = runner.ground_truth.load()
    assert len(truth.queries) == 5

    target = _target(reader_dsn)
    cost = PsycopgQueryCostPreflight(reader_dsn)
    preview = PsycopgQueryPreview(
        reader_dsn,
        expected_user=target.expected_reader,
        bound_target_fingerprint=target.fingerprint,
    )

    for case in truth.queries:
        validated = runner.guided.validate(case.expected_request)
        resolved = runner.prepare.planner.execute(validated).model_copy(
            update={"execution_target": target}
        )
        compiled = runner.prepare.compiler.compile(
            resolved.query_plan,
            max_preview_rows=resolved.query_policy.max_preview_rows,
            target=target,
        )
        query = runner.prepare.guard.validate(
            compiled,
            resolved.query_policy,
            target=target,
        )

        first = cost.assess(query, target)
        AssessGovernedQueryCost.require_accepted(first)
        assert first.decision is QueryCostDecision.ACCEPTED

        second = cost.assess(query, target)
        AssessGovernedQueryCost.require_accepted(second)
        assert second.decision is QueryCostDecision.ACCEPTED
        assert second.target_fingerprint == first.target_fingerprint == target.fingerprint
        assert second.budget_fingerprint == first.budget_fingerprint

        result = preview.execute(query, target=target)
        reporter = PsycopgRejectedSourceReporter(
            reader_dsn,
            allowed_fields=frozenset(
                check.physical_field.root for check in resolved.rejection_checks
            ),
            expected_user=target.expected_reader,
            bound_target_fingerprint=target.fingerprint,
        )
        rejected = reporter.inspect(
            resolved.rejection_checks,
            statement_timeout_ms=query.statement_timeout_ms,
            target=target,
        )

        assert normalize_preview_rows(result.columns, result.rows) == case.expected_rows
        assert tuple(item.code.value for item in rejected.records) == (
            case.expected_rejection_codes
        )
        assert result.database_user == target.expected_reader
        assert result.transaction_read_only is True
        assert result.statement_timeout_ms == 5_000
        if resolved.rejection_checks:
            assert rejected.database_user == target.expected_reader
            assert rejected.transaction_read_only is True
            assert rejected.statement_timeout_ms == 5_000
        else:
            assert rejected.database_user is None
            assert rejected.transaction_read_only is None
            assert rejected.statement_timeout_ms is None


def test_psycopg_json_loader_rejects_hard_excess_during_fetch_and_recovers(
    reader_dsn: str,
) -> None:
    with psycopg.connect(reader_dsn) as connection:
        connection.read_only = True
        connection.adapters.register_loader("json", _BoundedExplainJsonLoader)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT to_json(repeat('x', %s))",
                (MAX_EXPLAIN_RESPONSE_BYTES + 1,),
            )
            with pytest.raises(_ExplainResponseBytesExceeded):
                cursor.fetchone()
        connection.rollback()
        assert connection.execute("SELECT 1").fetchone() == (1,)


def _target(reader_dsn: str) -> GovernedExecutionTarget:
    with psycopg.connect(reader_dsn) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        observed = connection.execute(
            """
            SELECT
                inet_server_addr()::TEXT,
                inet_server_port(),
                current_database(),
                current_user
            """
        ).fetchone()
    assert observed is not None
    source_identity = postgres_source_identity_fingerprint(
        server_address=str(observed[0]),
        server_port=int(observed[1]),
        database=str(observed[2]),
        user=str(observed[3]),
    )
    budget = QueryCostBudget(
        explain_timeout_ms=2_000,
        max_response_bytes=1_048_576,
        max_total_cost=Decimal("1000000000000"),
        max_estimated_rows=1_000_000_000,
        max_plan_nodes=10_000,
        max_plan_depth=128,
        max_plan_width=1_048_576,
    )
    return GovernedExecutionTarget(
        workspace_id="m28-query-cases",
        connection_id=CatalogConnectionId("synthetic-demo"),
        connector_kind=SourceConnectorKind.POSTGRESQL,
        dialect=SourceDialect.POSTGRESQL,
        route_revision=1,
        route_fingerprint=hashlib.sha256(b"m28-query-cases-route").hexdigest(),
        expected_reader=str(observed[3]),
        source_identity_fingerprint=source_identity,
        catalog_identity_fingerprint=hashlib.sha256(b"m28-query-cases-catalog").hexdigest(),
        type_contract_fingerprint=postgres_type_contract_fingerprint(),
        cost_budget=budget,
        cost_budget_fingerprint=budget.fingerprint,
    )
