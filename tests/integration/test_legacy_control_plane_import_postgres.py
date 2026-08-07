"""Live PostgreSQL proof for the offline M23 legacy-control import."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from schemabridge.adapters.control_plane.legacy_import import (
    PostgresLegacyControlPlaneImportStore,
    SqliteLegacyControlPlaneSource,
)
from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.adapters.storage.postgres import PostgresWorkflowDraftStore
from schemabridge.adapters.storage.request_drafts import SqliteRequestDraftStore
from schemabridge.adapters.storage.workflows import SqliteWorkflowDraftStore
from schemabridge.application.governed_execution import PlanSemanticRequest
from schemabridge.application.guided_requests import (
    BuildGuidedRequest,
    GuidedRequestCase,
    build_demo_guided_input,
)
from schemabridge.application.legacy_import import (
    ApplyLegacyControlPlaneImport,
    ApproveLegacyControlPlaneImport,
    LegacyImportError,
    LegacyImportErrorCode,
    PrepareLegacyControlPlaneImport,
)
from schemabridge.bootstrap import build_semantic_registry
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.legacy_import import (
    LegacyImportApproval,
    LegacyImportConfirmation,
    LegacyImportDisposition,
    LegacyImportPlan,
    LegacyImportResourceKind,
    LegacyImportStatus,
)
from schemabridge.domain.requests import AnalyticalRequestDraft
from schemabridge.domain.resolution import (
    ResolutionLimits,
    resolved_semantic_plan_fingerprint,
)
from schemabridge.domain.workflows import (
    AgentWorkflowDraft,
    WorkflowExecutionRecord,
    WorkflowStage,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations/control_plane"
RUNTIME_DSN = (
    "postgresql://schemabridge_runtime:schemabridge_runtime@127.0.0.1:55434/schemabridge_control"
)
MIGRATOR_DSN = (
    "postgresql://schemabridge_migrator:schemabridge_migrator@127.0.0.1:55434/schemabridge_control"
)
NOW = datetime(2026, 7, 23, 18, 0, tzinfo=UTC)
_SUPPORTED_IMPORT_TARGETS = {
    LegacyImportResourceKind.WORKFLOW_DRAFT,
    LegacyImportResourceKind.WORKFLOW_ACCESS_GRANT,
}


def _dsn(name: str, fallback: str) -> str:
    return os.environ.get(name, fallback)


def _runtime_dsn() -> str:
    return _dsn("SCHEMABRIDGE_TEST_CONTROL_DATABASE_URL", RUNTIME_DSN)


@pytest.fixture(scope="module", autouse=True)
def _require_current_control_schema() -> None:
    PostgresControlPlaneMigrator(
        _dsn("SCHEMABRIDGE_TEST_CONTROL_MIGRATOR_DATABASE_URL", MIGRATOR_DSN),
        MIGRATIONS,
    ).require_current()


def _executed_workflow(workflow_id: str) -> AgentWorkflowDraft:
    registry = build_semantic_registry(repository_root=ROOT)
    validated = BuildGuidedRequest(registry).execute(
        build_demo_guided_input(GuidedRequestCase.NORTH_STAR)
    )
    resolved = PlanSemanticRequest(registry, ResolutionLimits()).execute(validated)
    plan_fingerprint = resolved_semantic_plan_fingerprint(resolved)
    execution = WorkflowExecutionRecord(
        plan_fingerprint=plan_fingerprint,
        query_fingerprint="b" * 64,
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
        id=workflow_id,
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
        query_fingerprint="b" * 64,
        execution=execution,
        created_at=NOW,
        updated_at=NOW,
    )


def _approval(plan: LegacyImportPlan) -> LegacyImportApproval:
    return ApproveLegacyControlPlaneImport().execute(
        plan,
        actor="integration-platform-admin",
        approved_at=NOW + timedelta(minutes=2),
        confirmation=LegacyImportConfirmation.IMPORT_VALIDATED_LEGACY_CONTROL_STATE,
    )


def test_legacy_import_persists_dry_run_then_atomically_applies_and_replays(
    tmp_path: Path,
) -> None:
    suffix = uuid4().hex
    workspace_id = f"legacy-workspace-{suffix}"
    owner_actor_id = f"legacy-owner-{suffix}"
    workflow = _executed_workflow(f"legacy-wf-{suffix}")
    source_path = tmp_path / "legacy-control.sqlite3"
    SqliteWorkflowDraftStore(
        source_path,
        workspace_id=workspace_id,
        owner_actor_id=owner_actor_id,
    ).save(workflow, expected_revision=None)
    validated_request = workflow.validated_request
    assert validated_request is not None
    request = AnalyticalRequestDraft(
        id=f"legacy-request-{suffix}",
        revision=1,
        request=validated_request.request,
    )
    SqliteRequestDraftStore(source_path).save(request, expected_revision=None)
    orphan_workflow = workflow.model_copy(update={"id": f"orphan-wf-{suffix}"})
    with closing(sqlite3.connect(source_path)) as connection:
        connection.execute(
            "INSERT INTO agent_workflow_drafts (id, revision, payload) VALUES (?, ?, ?)",
            (
                orphan_workflow.id,
                orphan_workflow.revision,
                orphan_workflow.model_dump_json(),
            ),
        )
        connection.execute(
            "INSERT INTO agent_workflow_drafts (id, revision, payload) VALUES (?, ?, ?)",
            (f"broken-wf-{suffix}", 1, "{}"),
        )
        connection.execute(
            """
            INSERT INTO workflow_access_grants (
                workflow_id, workspace_id, owner_actor_id, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                f"missing-wf-{suffix}",
                workspace_id,
                owner_actor_id,
                NOW.isoformat(),
            ),
        )
        connection.commit()
    source_bytes = source_path.read_bytes()
    source = SqliteLegacyControlPlaneSource(source_path)
    store = PostgresLegacyControlPlaneImportStore(_runtime_dsn())

    plan = PrepareLegacyControlPlaneImport(source, store).execute(recorded_at=NOW)

    assert source_path.read_bytes() == source_bytes
    assert plan.source_fingerprint == hashlib.sha256(source_bytes).hexdigest()
    assert plan.counts.model_dump() == {
        "total": 6,
        "imported": 2,
        "quarantined": 4,
        "skipped": 0,
        "preview_rows_stripped": 3,
    }
    assert {
        item.resource_kind
        for item in plan.items
        if item.disposition is LegacyImportDisposition.IMPORT
    } == _SUPPORTED_IMPORT_TARGETS
    with psycopg.connect(_runtime_dsn()) as connection:
        dry_run = connection.execute(
            """
            SELECT status, source_fingerprint, source_schema_fingerprint,
                   plan_fingerprint, counts_json, approval_id, completed_at
            FROM schemabridge_control.legacy_control_imports
            WHERE import_id = %s
            """,
            (plan.id,),
        ).fetchone()
        assert dry_run is not None
        assert dry_run[:4] == (
            LegacyImportStatus.DRY_RUN.value,
            plan.source_fingerprint,
            plan.source_schema_fingerprint,
            plan.fingerprint,
        )
        assert dry_run[4] == plan.counts.model_dump(mode="json")
        assert dry_run[5:] == (None, None)
        assert connection.execute(
            """
            SELECT count(*)
            FROM schemabridge_control.legacy_control_import_items
            WHERE import_id = %s
            """,
            (plan.id,),
        ).fetchone() == (0,)

    approval = _approval(plan)
    completed_at = NOW + timedelta(minutes=3)
    first = ApplyLegacyControlPlaneImport(source, store).execute(
        plan,
        approval,
        completed_at=completed_at,
    )
    replay = ApplyLegacyControlPlaneImport(source, store).execute(
        plan,
        approval,
        completed_at=completed_at + timedelta(minutes=1),
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.reservation == first.reservation
    assert source_path.read_bytes() == source_bytes
    with psycopg.connect(_runtime_dsn()) as connection:
        persisted_workflow = connection.execute(
            """
            SELECT payload, execution_row_count, execution_preview_fingerprint
            FROM schemabridge_control.agent_workflow_drafts
            WHERE workspace_id = %s AND id = %s
            """,
            (workspace_id, workflow.id),
        ).fetchone()
        persisted_grant = connection.execute(
            """
            SELECT owner_actor_id
            FROM schemabridge_control.workflow_access_grants
            WHERE workspace_id = %s AND workflow_id = %s
            """,
            (workspace_id, workflow.id),
        ).fetchone()
        quarantine = connection.execute(
            """
            SELECT reason_code, details_json
            FROM schemabridge_control.control_quarantine_items
            WHERE import_id = %s
            ORDER BY reason_code
            """,
            (plan.id,),
        ).fetchall()
        outcomes = connection.execute(
            """
            SELECT outcome, count(*)
            FROM schemabridge_control.legacy_control_import_items
            WHERE import_id = %s
            GROUP BY outcome
            ORDER BY outcome
            """,
            (plan.id,),
        ).fetchall()
        completion = connection.execute(
            """
            SELECT status, approval_id, completed_at
            FROM schemabridge_control.legacy_control_imports
            WHERE import_id = %s
            """,
            (plan.id,),
        ).fetchone()

    assert persisted_workflow is not None
    payload = persisted_workflow[0]
    assert isinstance(payload, dict)
    execution = payload["execution"]
    assert isinstance(execution, dict)
    assert execution["rows"] == []
    assert execution["row_count"] == 3
    assert persisted_workflow[1:] == (3, "c" * 64)
    assert "2026-01-01" not in json.dumps(payload, sort_keys=True)
    assert persisted_grant == (owner_actor_id,)
    assert outcomes == [("imported", 2), ("quarantined", 4)]
    assert {row[0] for row in quarantine} == {
        "invalid_payload",
        "orphan_access_grant",
        "orphan_workflow",
        "ownership_not_provable",
    }
    assert all(
        set(row[1]) == {"resource_kind", "source_id_digest", "source_table"} for row in quarantine
    )
    assert completion == (
        LegacyImportStatus.COMPLETED.value,
        approval.id,
        completed_at,
    )


def test_legacy_import_and_quarantine_lifecycles_are_database_enforced() -> None:
    suffix = uuid4().hex
    import_id = f"legacy-import-lifecycle-{suffix}"
    source_fingerprint = hashlib.sha256(f"source-{suffix}".encode()).hexdigest()
    schema_fingerprint = hashlib.sha256(f"schema-{suffix}".encode()).hexdigest()
    plan_fingerprint = hashlib.sha256(f"plan-{suffix}".encode()).hexdigest()
    approval_id = f"legacy-import-approval-{suffix}"
    quarantine_id = f"legacy-quarantine-{suffix}"
    created_at = NOW
    approved_at = NOW + timedelta(minutes=1)
    completed_at = NOW + timedelta(minutes=2)

    with psycopg.connect(_runtime_dsn()) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.legacy_control_imports (
                import_id, source_kind, source_fingerprint,
                source_schema_fingerprint, plan_fingerprint,
                approval_id, actor, approved_at, status,
                counts_json, created_at, completed_at
            ) VALUES (
                %s, 'sqlite', %s, %s, %s,
                NULL, NULL, NULL, 'dry_run',
                %s::jsonb, %s, NULL
            )
            """,
            (
                import_id,
                source_fingerprint,
                schema_fingerprint,
                plan_fingerprint,
                json.dumps(
                    {
                        "total": 1,
                        "imported": 0,
                        "quarantined": 1,
                        "skipped": 0,
                        "preview_rows_stripped": 0,
                    }
                ),
                created_at,
            ),
        )
        connection.execute(
            """
            UPDATE schemabridge_control.legacy_control_imports
            SET approval_id = %s,
                actor = %s,
                approved_at = %s,
                status = 'completed',
                completed_at = %s
            WHERE import_id = %s
            """,
            (
                approval_id,
                "integration-platform-admin",
                approved_at,
                completed_at,
                import_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.control_quarantine_items (
                quarantine_id, import_id, workspace_id, resource_type,
                resource_id_digest, reason_code, payload_fingerprint,
                details_json, detected_at, resolved_at, resolution_event_id
            ) VALUES (
                %s, %s, NULL, 'workflow_draft',
                %s, 'orphan_workflow', %s,
                %s::jsonb, %s, NULL, NULL
            )
            """,
            (
                quarantine_id,
                import_id,
                hashlib.sha256(f"resource-{suffix}".encode()).hexdigest(),
                hashlib.sha256(f"payload-{suffix}".encode()).hexdigest(),
                json.dumps({"source_table": "agent_workflow_drafts"}),
                completed_at,
            ),
        )

    with psycopg.connect(_runtime_dsn()) as connection:
        assert connection.execute(
            """
            SELECT status, approval_id, actor, approved_at, completed_at
            FROM schemabridge_control.legacy_control_imports
            WHERE import_id = %s
            """,
            (import_id,),
        ).fetchone() == (
            "completed",
            approval_id,
            "integration-platform-admin",
            approved_at,
            completed_at,
        )

    with (
        psycopg.connect(_runtime_dsn()) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        connection.transaction(),
    ):
        connection.execute(
            """
            UPDATE schemabridge_control.legacy_control_imports
            SET plan_fingerprint = %s
            WHERE import_id = %s
            """,
            ("f" * 64, import_id),
        )

    with (
        psycopg.connect(_runtime_dsn()) as connection,
        pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState),
        connection.transaction(),
    ):
        connection.execute(
            """
            UPDATE schemabridge_control.legacy_control_imports
            SET actor = %s
            WHERE import_id = %s
            """,
            ("rewritten-actor", import_id),
        )

    with (
        psycopg.connect(_runtime_dsn()) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        connection.transaction(),
    ):
        connection.execute(
            """
            UPDATE schemabridge_control.control_quarantine_items
            SET reason_code = %s
            WHERE quarantine_id = %s
            """,
            ("rewritten_reason", quarantine_id),
        )

    with (
        psycopg.connect(
            _dsn("SCHEMABRIDGE_TEST_CONTROL_MIGRATOR_DATABASE_URL", MIGRATOR_DSN)
        ) as connection,
        pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState),
        connection.transaction(),
    ):
        connection.execute(
            """
            DELETE FROM schemabridge_control.control_quarantine_items
            WHERE quarantine_id = %s
            """,
            (quarantine_id,),
        )

    forged_suffix = uuid4().hex
    with (
        psycopg.connect(_runtime_dsn()) as connection,
        pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState),
        connection.transaction(),
    ):
        connection.execute(
            """
            INSERT INTO schemabridge_control.legacy_control_imports (
                import_id, source_kind, source_fingerprint,
                source_schema_fingerprint, plan_fingerprint,
                approval_id, actor, approved_at, status,
                counts_json, created_at, completed_at
            ) VALUES (
                %s, 'sqlite', %s, %s, %s,
                %s, 'forged-actor', %s, 'completed',
                %s::jsonb, %s, %s
            )
            """,
            (
                f"legacy-import-forged-{forged_suffix}",
                hashlib.sha256(f"source-{forged_suffix}".encode()).hexdigest(),
                hashlib.sha256(f"schema-{forged_suffix}".encode()).hexdigest(),
                hashlib.sha256(f"plan-{forged_suffix}".encode()).hexdigest(),
                f"legacy-import-approval-{forged_suffix}",
                approved_at,
                json.dumps(
                    {
                        "total": 0,
                        "imported": 0,
                        "quarantined": 0,
                        "skipped": 0,
                        "preview_rows_stripped": 0,
                    }
                ),
                created_at,
                completed_at,
            ),
        )


def test_legacy_apply_rolls_back_every_target_when_a_late_insert_conflicts(
    tmp_path: Path,
) -> None:
    suffix = uuid4().hex
    workspace_id = f"atomic-workspace-{suffix}"
    owner_actor_id = f"atomic-owner-{suffix}"
    source_path = tmp_path / "atomic-legacy.sqlite3"
    drafts = (
        _executed_workflow(f"atomic-wf-a-{suffix}"),
        _executed_workflow(f"atomic-wf-b-{suffix}"),
    )
    sqlite_store = SqliteWorkflowDraftStore(
        source_path,
        workspace_id=workspace_id,
        owner_actor_id=owner_actor_id,
    )
    for draft in drafts:
        sqlite_store.save(draft, expected_revision=None)
    source_bytes = source_path.read_bytes()
    source = SqliteLegacyControlPlaneSource(source_path)
    import_store = PostgresLegacyControlPlaneImportStore(_runtime_dsn())
    plan = PrepareLegacyControlPlaneImport(source, import_store).execute(recorded_at=NOW)
    workflow_items = tuple(
        item for item in plan.items if item.resource_kind is LegacyImportResourceKind.WORKFLOW_DRAFT
    )
    assert len(workflow_items) == 2
    first_target = workflow_items[0].target_id
    conflicting_target = workflow_items[1].target_id
    assert first_target is not None
    assert conflicting_target is not None
    by_id = {draft.id: draft for draft in drafts}
    PostgresWorkflowDraftStore(
        _runtime_dsn(),
        workspace_id=workspace_id,
        owner_actor_id=owner_actor_id,
    ).save(by_id[conflicting_target], expected_revision=None)
    approval = _approval(plan)

    with pytest.raises(LegacyImportError) as raised:
        ApplyLegacyControlPlaneImport(source, import_store).execute(
            plan,
            approval,
            completed_at=NOW + timedelta(minutes=3),
        )

    assert raised.value.code is LegacyImportErrorCode.STORE_CONFLICT
    assert source_path.read_bytes() == source_bytes
    with psycopg.connect(_runtime_dsn()) as connection:
        rolled_back_workflow = connection.execute(
            """
            SELECT count(*)
            FROM schemabridge_control.agent_workflow_drafts
            WHERE workspace_id = %s AND id = %s
            """,
            (workspace_id, first_target),
        ).fetchone()
        rolled_back_grant = connection.execute(
            """
            SELECT count(*)
            FROM schemabridge_control.workflow_access_grants
            WHERE workspace_id = %s AND workflow_id = %s
            """,
            (workspace_id, first_target),
        ).fetchone()
        reservation = connection.execute(
            """
            SELECT status, approval_id, completed_at
            FROM schemabridge_control.legacy_control_imports
            WHERE import_id = %s
            """,
            (plan.id,),
        ).fetchone()
        item_count = connection.execute(
            """
            SELECT count(*)
            FROM schemabridge_control.legacy_control_import_items
            WHERE import_id = %s
            """,
            (plan.id,),
        ).fetchone()

    assert rolled_back_workflow == (0,)
    assert rolled_back_grant == (0,)
    assert reservation == (LegacyImportStatus.DRY_RUN.value, None, None)
    assert item_count == (0,)
