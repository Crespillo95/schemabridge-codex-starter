"""Application-port orchestration tests for guarded semantic execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from schemabridge.adapters.planning.recorded import RecordedSemanticPlanningContext
from schemabridge.adapters.postgres.rejections import PsycopgRejectedSourceReporter
from schemabridge.adapters.requests.recorded_context import RecordedRequestContextAdapter
from schemabridge.adapters.semantic_change.postgres_read import (
    PostgresSemanticChangeGateReader,
)
from schemabridge.adapters.sql.compiler import PostgresQueryCompiler
from schemabridge.adapters.sql.guard import SqlGlotPolicyGuard
from schemabridge.application.governed_execution import (
    ExecuteGovernedRequest,
    PlanSemanticRequest,
    PrepareGovernedRequest,
)
from schemabridge.application.guided_requests import (
    BuildGuidedRequest,
    GuidedRequestCase,
    build_demo_guided_input,
)
from schemabridge.application.ports.planning import PlanningPortError, PlanningPortErrorCode
from schemabridge.application.query_execution import QueryPreviewResult, ValidatedQuery
from schemabridge.bootstrap import (
    build_governed_request_executor,
    build_governed_request_preparer,
    build_semantic_registry,
)
from schemabridge.config import Settings
from schemabridge.domain.resolution import (
    RejectedSourceReport,
    RejectionCheck,
    ResolutionLimits,
)

ROOT = Path(__file__).resolve().parents[2]


@dataclass(slots=True)
class FakePreview:
    received: list[ValidatedQuery] = field(default_factory=list)

    def execute(self, query: ValidatedQuery) -> QueryPreviewResult:
        self.received.append(query)
        return QueryPreviewResult(
            columns=("registration_date", "secondary_holder_customers"),
            rows=(("2026-01-01", 2),),
            database_user="schemabridge_reader",
            transaction_read_only=True,
            statement_timeout_ms=query.statement_timeout_ms,
            truncated=False,
        )


@dataclass(slots=True)
class FakeRejectionReporter:
    received: list[tuple[RejectionCheck, ...]] = field(default_factory=list)

    def inspect(
        self,
        checks: tuple[RejectionCheck, ...],
        *,
        statement_timeout_ms: int,
    ) -> RejectedSourceReport:
        self.received.append(checks)
        return RejectedSourceReport(
            inspected_fields=tuple(check.physical_field for check in checks),
            database_user="schemabridge_reader",
            transaction_read_only=True,
            statement_timeout_ms=statement_timeout_ms,
        )


def test_governed_execution_crosses_only_ports_and_returns_complete_typed_result() -> None:
    logical_path = ROOT / "demo/ground_truth/approved_logical_context.yml"
    context = RecordedSemanticPlanningContext(
        logical_path,
        ROOT / "demo/ground_truth/planning_mappings.yml",
        ROOT / "demo/ground_truth/join_contracts.yml",
    )
    request = BuildGuidedRequest(RecordedRequestContextAdapter(logical_path)).execute(
        build_demo_guided_input(GuidedRequestCase.NORTH_STAR)
    )
    preview = FakePreview()
    rejections = FakeRejectionReporter()
    result = ExecuteGovernedRequest(
        prepare=PrepareGovernedRequest(
            planner=PlanSemanticRequest(context, ResolutionLimits()),
            compiler=PostgresQueryCompiler(),
            guard=SqlGlotPolicyGuard(),
        ),
        executor=preview,
        rejection_reporter=rejections,
    ).execute(request)

    assert result.policy_status == "accepted"
    assert result.policy_findings == ()
    assert result.sql.count("%s") == len(result.parameters)
    assert "SECONDARY" not in result.sql
    assert "9007199254740991" in result.sql
    assert "< 0" in result.sql
    assert result.preview.database_user == "schemabridge_reader"
    assert preview.received[0].sql == result.sql
    assert {check.physical_field.root for check in rejections.received[0]} == {
        "crm.customers.customer_id",
        "bank.account_holders.gf_customer_id",
    }
    assert result.as_dict()["policy"] == {"status": "accepted", "findings": []}


def test_rejected_source_reporter_blocks_unallowlisted_fields_before_connecting() -> None:
    logical_path = ROOT / "demo/ground_truth/approved_logical_context.yml"
    request = BuildGuidedRequest(RecordedRequestContextAdapter(logical_path)).execute(
        build_demo_guided_input(GuidedRequestCase.NORTH_STAR)
    )
    prepared = PrepareGovernedRequest(
        planner=PlanSemanticRequest(
            RecordedSemanticPlanningContext(
                logical_path,
                ROOT / "demo/ground_truth/planning_mappings.yml",
                ROOT / "demo/ground_truth/join_contracts.yml",
            ),
            ResolutionLimits(),
        ),
        compiler=PostgresQueryCompiler(),
        guard=SqlGlotPolicyGuard(),
    ).execute(request)
    reporter = PsycopgRejectedSourceReporter(
        "postgresql://must-not-connect.invalid/example",
        allowed_fields=frozenset(),
    )

    with pytest.raises(PlanningPortError) as captured:
        reporter.inspect(
            prepared.resolved_plan.rejection_checks,
            statement_timeout_ms=5_000,
        )

    assert captured.value.code is PlanningPortErrorCode.REJECTION_INSPECTION_FORBIDDEN


def test_governed_application_use_case_has_no_concrete_adapter_imports() -> None:
    source = (ROOT / "src/schemabridge/application/governed_execution.py").read_text(
        encoding="utf-8"
    )

    assert "schemabridge.adapters" not in source
    assert "psycopg" not in source
    assert "sqlglot" not in source


def test_rejection_allowlist_is_derived_from_the_active_registry() -> None:
    settings = Settings.model_validate(
        {
            "DATABASE_URL": "postgresql://reader:synthetic@invalid.example/synthetic",
        }
    )
    registry = build_semantic_registry(repository_root=ROOT, settings=settings)
    prepare = build_governed_request_preparer(
        repository_root=ROOT,
        settings=settings,
        registry=registry,
    )

    executor = build_governed_request_executor(
        repository_root=ROOT,
        settings=settings,
        prepare=prepare,
    )

    assert "sales.orders.order_id" in executor.rejection_reporter.allowed_fields  # type: ignore[attr-defined]
    assert "fulfillment.shipments.order_ref" in executor.rejection_reporter.allowed_fields  # type: ignore[attr-defined]
    assert not any(  # type: ignore[attr-defined]
        item.startswith("support.order_cases.")
        for item in executor.rejection_reporter.allowed_fields
    )


def test_active_postgres_preparer_composes_mandatory_pre_compiler_semantic_gate() -> None:
    settings = Settings(
        _env_file=None,
        SCHEMABRIDGE_CONTROL_PLANE_MODE="postgres",
        SCHEMABRIDGE_CONTROL_DATABASE_URL=(
            "postgresql://schemabridge_runtime:password@control.example.test/control"
        ),
        SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY=(
            "runtime-audit-signing-key-with-enough-diversity-123"
        ),
        SCHEMABRIDGE_IDENTITY_MIGRATION_KEY=("runtime-identity-migration-key-with-diversity-456"),
        SCHEMABRIDGE_REGISTRY_MODE="live",
        SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION="active",
        SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS=True,
        SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY=ROOT / ".local/test-connectors",
    )
    registry = build_semantic_registry(
        repository_root=ROOT,
        settings=Settings.model_validate({}),
    )

    prepare = build_governed_request_preparer(
        repository_root=ROOT,
        settings=settings,
        registry=registry,
        workspace_id="local-demo",
    )

    assert prepare.semantic_gate is not None
    assert isinstance(prepare.semantic_gate.gate, PostgresSemanticChangeGateReader)
    assert prepare.semantic_scope is not None
    assert prepare.semantic_scope.workspace_id == "local-demo"
    assert prepare.target_resolver is not None
    assert prepare.cost_preflight is not None
