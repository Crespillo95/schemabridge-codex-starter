"""Managed PostgreSQL-store behavior that can be checked without a database."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from schemabridge.adapters.storage.postgres import (
    PostgresWorkflowDraftStore,
    _durable_workflow,
)
from schemabridge.application.governed_execution import PlanSemanticRequest
from schemabridge.application.guided_requests import (
    BuildGuidedRequest,
    GuidedRequestCase,
    build_demo_guided_input,
)
from schemabridge.bootstrap import build_semantic_registry
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.resolution import (
    ResolutionLimits,
    resolved_semantic_plan_fingerprint,
)
from schemabridge.domain.workflows import (
    AgentWorkflowDraft,
    WorkflowExecutionRecord,
    WorkflowStage,
)

ROOT = Path(__file__).resolve().parents[2]


def _executed_workflow() -> AgentWorkflowDraft:
    registry = build_semantic_registry(repository_root=ROOT)
    validated = BuildGuidedRequest(registry).execute(
        build_demo_guided_input(GuidedRequestCase.NORTH_STAR)
    )
    resolved = PlanSemanticRequest(registry, ResolutionLimits()).execute(validated)
    plan_fingerprint = resolved_semantic_plan_fingerprint(resolved)
    query_fingerprint = "b" * 64
    occurred_at = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    execution = WorkflowExecutionRecord(
        plan_fingerprint=plan_fingerprint,
        query_fingerprint=query_fingerprint,
        columns=("registration_date", "customer_count"),
        rows=(
            ("2026-01-01", 2),
            ("2026-01-02", 1),
            ("2026-01-03", 1),
        ),
        row_count=3,
        database_user="schemabridge_reader",
        transaction_read_only=True,
        statement_timeout_ms=5_000,
        truncated=False,
        preview_fingerprint="c" * 64,
    )
    return AgentWorkflowDraft(
        id="managed-workflow",
        revision=4,
        stage=WorkflowStage.EXECUTED,
        text="Agrupa clientes segundo titular por fecha de registro",
        language="es",
        requested_datasets=(
            PhysicalDatasetRef("crm.customers"),
            PhysicalDatasetRef("bank.account_holders"),
        ),
        validated_request=validated,
        resolved_plan=resolved,
        plan_fingerprint=plan_fingerprint,
        query_fingerprint=query_fingerprint,
        execution=execution,
        created_at=occurred_at,
        updated_at=occurred_at,
    )


def test_durable_workflow_payload_discards_rows_but_keeps_count_and_fingerprint() -> None:
    draft = _executed_workflow()

    payload, row_count, preview_fingerprint = _durable_workflow(draft)

    assert draft.execution is not None
    assert len(draft.execution.rows) == 3
    assert row_count == 3
    assert preview_fingerprint == "c" * 64
    execution = payload["execution"]
    assert isinstance(execution, dict)
    assert execution["rows"] == []
    assert execution["row_count"] == 3


def test_postgres_store_repr_never_contains_the_control_database_secret() -> None:
    store = PostgresWorkflowDraftStore(
        "postgresql://runtime:do-not-print@control.example/control",
        workspace_id="workspace-a",
        owner_actor_id="actor-a",
    )

    assert "do-not-print" not in repr(store)
    assert "postgresql://" not in repr(store)


@pytest.mark.parametrize("value", ["", " workspace", "workspace "])
def test_postgres_store_rejects_ambiguous_scope_values(value: str) -> None:
    with pytest.raises(ValueError, match="workflow workspace is invalid"):
        PostgresWorkflowDraftStore(
            "postgresql://runtime:secret@control.example/control",
            workspace_id=value,
            owner_actor_id="actor-a",
        )
