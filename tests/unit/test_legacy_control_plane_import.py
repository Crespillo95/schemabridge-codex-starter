from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from schemabridge.adapters.control_plane.legacy_import import (
    PostgresLegacyControlPlaneImportStore,
    SqliteLegacyControlPlaneSource,
)
from schemabridge.adapters.recipes.sqlite import SqliteQueryRecipeRepository
from schemabridge.adapters.storage.publication_audit import (
    SqlitePublicationAuditStore,
)
from schemabridge.adapters.storage.relationships import SqliteJoinReviewStore
from schemabridge.adapters.storage.request_drafts import SqliteRequestDraftStore
from schemabridge.adapters.storage.reviews import SqliteReviewStore
from schemabridge.adapters.storage.workflows import SqliteWorkflowDraftStore
from schemabridge.adapters.workflows.fake import SqliteFakeWorkflowPublisher
from schemabridge.application.governed_execution import PlanSemanticRequest
from schemabridge.application.guided_requests import (
    BuildGuidedRequest,
    GuidedRequestCase,
    build_demo_guided_input,
)
from schemabridge.application.legacy_import import (
    ApplyLegacyControlPlaneImport,
    ApproveLegacyControlPlaneImport,
    InspectLegacyControlPlaneImport,
    LegacyImportError,
    LegacyImportErrorCode,
    PrepareLegacyControlPlaneImport,
    plan_legacy_control_plane_import,
)
from schemabridge.application.ports.legacy_import import (
    LegacyImportPortError,
    LegacyImportPortErrorCode,
)
from schemabridge.bootstrap import build_semantic_registry
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.legacy_import import (
    LegacyImportApproval,
    LegacyImportConfirmation,
    LegacyImportDisposition,
    LegacyImportPlan,
    LegacyImportReservation,
    LegacyImportResourceKind,
    LegacyImportResult,
    LegacyImportStatus,
)
from schemabridge.domain.publication_audit import (
    PublicationAuditOutcome,
    PublicationFamily,
    PublicationTargetAuditRecord,
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

_ROOT = Path(__file__).resolve().parents[2]
_NOW = datetime(2026, 7, 23, 14, 0, tzinfo=UTC)


@dataclass
class _MemoryImportStore:
    reservations: dict[str, LegacyImportReservation] = field(default_factory=dict)
    apply_calls: int = 0

    def reserve_dry_run(
        self,
        plan: LegacyImportPlan,
        *,
        recorded_at: datetime,
    ) -> LegacyImportReservation:
        current = self.reservations.get(plan.id)
        if current is not None:
            if (
                current.source_fingerprint != plan.source_fingerprint
                or current.source_schema_fingerprint != plan.source_schema_fingerprint
                or current.plan_fingerprint != plan.fingerprint
                or current.counts != plan.counts
            ):
                raise LegacyImportPortError(
                    LegacyImportPortErrorCode.STORE_CONFLICT,
                    "legacy dry-run conflict",
                )
            return current
        reservation = LegacyImportReservation(
            import_id=plan.id,
            source_fingerprint=plan.source_fingerprint,
            source_schema_fingerprint=plan.source_schema_fingerprint,
            plan_fingerprint=plan.fingerprint,
            counts=plan.counts,
            status=LegacyImportStatus.DRY_RUN,
            recorded_at=recorded_at,
        )
        self.reservations[plan.id] = reservation
        return reservation

    def load_reservation(self, import_id: str) -> LegacyImportReservation | None:
        return self.reservations.get(import_id)

    def apply(
        self,
        plan: LegacyImportPlan,
        approval: LegacyImportApproval,
        *,
        completed_at: datetime,
    ) -> LegacyImportResult:
        self.apply_calls += 1
        current = self.reservations.get(plan.id)
        if current is None:
            raise LegacyImportPortError(
                LegacyImportPortErrorCode.STORE_CONFLICT,
                "dry-run absent",
            )
        if current.status is LegacyImportStatus.COMPLETED:
            return LegacyImportResult(reservation=current, replayed=True)
        completed = LegacyImportReservation(
            import_id=plan.id,
            source_fingerprint=plan.source_fingerprint,
            source_schema_fingerprint=plan.source_schema_fingerprint,
            plan_fingerprint=plan.fingerprint,
            counts=plan.counts,
            status=LegacyImportStatus.COMPLETED,
            recorded_at=current.recorded_at,
            approval_id=approval.id,
            completed_at=completed_at,
        )
        self.reservations[plan.id] = completed
        return LegacyImportResult(reservation=completed, replayed=False)


def _executed_workflow(workflow_id: str = "legacy-workflow") -> AgentWorkflowDraft:
    registry = build_semantic_registry(repository_root=_ROOT)
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
        created_at=_NOW,
        updated_at=_NOW,
    )


def _create_owned_workflow_source(path: Path) -> AgentWorkflowDraft:
    draft = _executed_workflow()
    store = SqliteWorkflowDraftStore(
        path,
        workspace_id="workspace-a",
        owner_actor_id="actor-a",
    )
    store.save(draft, expected_revision=None)
    return draft


def _approval(plan: LegacyImportPlan) -> LegacyImportApproval:
    return ApproveLegacyControlPlaneImport().execute(
        plan,
        actor="platform-admin",
        approved_at=_NOW + timedelta(minutes=2),
        confirmation=LegacyImportConfirmation.IMPORT_VALIDATED_LEGACY_CONTROL_STATE,
    )


def test_offline_source_is_deterministic_and_strips_only_target_preview_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    draft = _create_owned_workflow_source(path)
    before = path.read_bytes()
    source = SqliteLegacyControlPlaneSource(path)

    first = source.inspect()
    second = source.inspect()
    plan = plan_legacy_control_plane_import(first)

    assert first == second
    assert first.source_fingerprint == hashlib.sha256(before).hexdigest()
    assert path.read_bytes() == before
    assert plan.counts.model_dump() == {
        "total": 2,
        "imported": 2,
        "quarantined": 0,
        "skipped": 0,
        "preview_rows_stripped": 3,
    }
    assert {item.resource_kind for item in plan.items} == {
        LegacyImportResourceKind.WORKFLOW_DRAFT,
        LegacyImportResourceKind.WORKFLOW_ACCESS_GRANT,
    }
    workflow = next(
        item for item in plan.items if item.resource_kind is LegacyImportResourceKind.WORKFLOW_DRAFT
    )
    assert workflow.disposition is LegacyImportDisposition.IMPORT
    assert workflow.workspace_id == "workspace-a"
    assert workflow.target_id == draft.id
    assert workflow.target_payload_json is not None
    target = json.loads(workflow.target_payload_json)
    assert target["execution_row_count"] == 3
    assert target["execution_preview_fingerprint"] == "c" * 64
    assert target["payload"]["execution"]["rows"] == []
    assert target["payload"]["execution"]["row_count"] == 3

    with closing(sqlite3.connect(path)) as connection:
        raw = connection.execute(
            "SELECT payload FROM agent_workflow_drafts WHERE id = ?",
            (draft.id,),
        ).fetchone()
    assert raw is not None
    assert len(json.loads(raw[0])["execution"]["rows"]) == 3


def test_every_known_sqlite_table_family_passes_exact_schema_inspection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "all-known.sqlite3"
    SqliteWorkflowDraftStore(path)
    SqliteRequestDraftStore(path)
    SqliteReviewStore(path)
    SqliteJoinReviewStore(path)
    SqlitePublicationAuditStore(path)
    SqliteQueryRecipeRepository(path)
    SqliteFakeWorkflowPublisher(path)

    snapshot = SqliteLegacyControlPlaneSource(path).inspect()
    plan = plan_legacy_control_plane_import(snapshot)

    assert {table.name for table in snapshot.tables} == {
        "agent_workflow_drafts",
        "workflow_access_grants",
        "analytical_request_drafts",
        "review_drafts",
        "review_decisions",
        "review_publications",
        "join_review_drafts",
        "join_review_decisions",
        "join_publications",
        "publication_approval_identity",
        "publication_target_audit",
        "fake_query_recipes",
        "fake_workflow_publications",
    }
    assert plan.counts.total == 0


def test_publication_audit_identity_mismatch_is_quarantined_as_invalid(
    tmp_path: Path,
) -> None:
    path = tmp_path / "audit-mismatch.sqlite3"
    audit = SqlitePublicationAuditStore(path)
    audit.append(
        (
            PublicationTargetAuditRecord(
                family=PublicationFamily.WORKFLOW,
                operation="upsert_document",
                target="urn:li:document:workflow",
                approval_id="approval-a",
                actor="actor-a",
                approved_at=_NOW,
                previous_fingerprint=None,
                new_fingerprint="c" * 64,
                outcome=PublicationAuditOutcome.SUCCEEDED,
            ),
        )
    )
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            """
            UPDATE publication_approval_identity
            SET new_fingerprint = ?
            WHERE approval_id = ?
            """,
            ("d" * 64, "approval-a"),
        )
        connection.commit()

    plan = plan_legacy_control_plane_import(SqliteLegacyControlPlaneSource(path).inspect())

    target_audit = next(
        item
        for item in plan.items
        if item.resource_kind is LegacyImportResourceKind.PUBLICATION_TARGET_AUDIT
    )
    assert target_audit.disposition is LegacyImportDisposition.QUARANTINE
    assert target_audit.reason_code == "invalid_payload"


def test_invalid_orphan_ambiguous_and_fake_rows_are_never_adopted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mixed.sqlite3"
    draft = _create_owned_workflow_source(path)
    request = AnalyticalRequestDraft(
        id="request-a",
        revision=1,
        request=draft.validated_request.request,  # type: ignore[union-attr]
    )
    SqliteRequestDraftStore(path).save(request, expected_revision=None)
    orphan = draft.model_copy(update={"id": "orphan-workflow"})
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "INSERT INTO agent_workflow_drafts (id, revision, payload) VALUES (?, ?, ?)",
            (orphan.id, orphan.revision, orphan.model_dump_json()),
        )
        connection.execute(
            "INSERT INTO agent_workflow_drafts (id, revision, payload) VALUES (?, ?, ?)",
            ("broken-workflow", 1, "{}"),
        )
        connection.execute(
            """
            INSERT INTO workflow_access_grants (
                workflow_id, workspace_id, owner_actor_id, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            ("missing-workflow", "workspace-a", "actor-a", _NOW.isoformat()),
        )
        connection.execute(
            """
            CREATE TABLE fake_workflow_publications (
                idempotency_key TEXT PRIMARY KEY,
                document_ref TEXT NOT NULL,
                published_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO fake_workflow_publications (
                idempotency_key, document_ref, published_at
            ) VALUES (?, ?, ?)
            """,
            ("d" * 64, "fake://local-only", _NOW.isoformat()),
        )
        connection.commit()

    plan = plan_legacy_control_plane_import(SqliteLegacyControlPlaneSource(path).inspect())

    assert plan.counts.model_dump() == {
        "total": 7,
        "imported": 2,
        "quarantined": 4,
        "skipped": 1,
        "preview_rows_stripped": 3,
    }
    reasons = {
        item.reason_code
        for item in plan.items
        if item.disposition is not LegacyImportDisposition.IMPORT
    }
    assert reasons == {
        "invalid_payload",
        "orphan_workflow",
        "orphan_access_grant",
        "ownership_not_provable",
        "local_only_resource",
    }
    quarantined = tuple(
        item for item in plan.items if item.disposition is LegacyImportDisposition.QUARANTINE
    )
    assert {
        item.resource_kind
        for item in plan.items
        if item.disposition is LegacyImportDisposition.IMPORT
    } == {
        LegacyImportResourceKind.WORKFLOW_DRAFT,
        LegacyImportResourceKind.WORKFLOW_ACCESS_GRANT,
    }
    assert all(item.target_id is None for item in quarantined)
    assert all(item.target_payload_json is None for item in quarantined)
    assert "orphan-workflow" not in repr(quarantined)


@pytest.mark.parametrize(
    "schema_sql",
    [
        "CREATE TABLE unknown_state (id TEXT PRIMARY KEY)",
        """
        CREATE TABLE review_drafts (
            id TEXT PRIMARY KEY,
            revision INTEGER NOT NULL,
            payload TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE agent_workflow_drafts (
            id TEXT PRIMARY KEY,
            revision INTEGER NOT NULL,
            payload TEXT NOT NULL,
            unexpected TEXT
        )
        """,
    ],
)
def test_unknown_partial_or_modified_schema_fails_closed(
    tmp_path: Path,
    schema_sql: str,
) -> None:
    path = tmp_path / "invalid-schema.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(schema_sql)
        connection.commit()

    with pytest.raises(LegacyImportPortError) as raised:
        SqliteLegacyControlPlaneSource(path).inspect()

    assert raised.value.code is LegacyImportPortErrorCode.SOURCE_SCHEMA_INVALID
    assert str(path) not in str(raised.value)


def test_active_journal_state_is_rejected_before_sqlite_open(tmp_path: Path) -> None:
    path = tmp_path / "active.sqlite3"
    _create_owned_workflow_source(path)
    journal = Path(f"{path}-wal")
    journal.write_bytes(b"active")

    with pytest.raises(LegacyImportPortError) as raised:
        SqliteLegacyControlPlaneSource(path).inspect()

    assert raised.value.code is LegacyImportPortErrorCode.SOURCE_NOT_OFFLINE


def test_prepare_apply_and_replay_require_exact_dry_run_and_approval(
    tmp_path: Path,
) -> None:
    path = tmp_path / "approved.sqlite3"
    _create_owned_workflow_source(path)
    source = SqliteLegacyControlPlaneSource(path)
    store = _MemoryImportStore()
    prepare = PrepareLegacyControlPlaneImport(source, store)
    apply = ApplyLegacyControlPlaneImport(source, store)

    plan = prepare.execute(recorded_at=_NOW)
    assert store.apply_calls == 0
    approval = _approval(plan)
    completed_at = _NOW + timedelta(minutes=3)
    first = apply.execute(plan, approval, completed_at=completed_at)
    second = apply.execute(
        plan,
        approval,
        completed_at=completed_at + timedelta(minutes=1),
    )

    assert not first.replayed
    assert second.replayed
    assert second.reservation == first.reservation
    assert second.reservation.approval_id == approval.id
    assert store.apply_calls == 2


def test_inspect_is_non_writing_and_does_not_satisfy_apply_reservation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inspect-only.sqlite3"
    _create_owned_workflow_source(path)
    source_bytes = path.read_bytes()
    source = SqliteLegacyControlPlaneSource(path)
    store = _MemoryImportStore()

    plan = InspectLegacyControlPlaneImport(source).execute()

    assert path.read_bytes() == source_bytes
    assert store.reservations == {}
    with pytest.raises(LegacyImportError) as raised:
        ApplyLegacyControlPlaneImport(source, store).execute(
            plan,
            _approval(plan),
            completed_at=_NOW + timedelta(minutes=3),
        )
    assert raised.value.code is LegacyImportErrorCode.DRY_RUN_REQUIRED
    assert store.reservations == {}
    assert store.apply_calls == 0


def test_apply_rejects_missing_dry_run_stale_approval_and_changed_source(
    tmp_path: Path,
) -> None:
    path = tmp_path / "stale.sqlite3"
    _create_owned_workflow_source(path)
    source = SqliteLegacyControlPlaneSource(path)
    initial_plan = plan_legacy_control_plane_import(source.inspect())
    approval = _approval(initial_plan)

    with pytest.raises(LegacyImportError) as missing:
        ApplyLegacyControlPlaneImport(source, _MemoryImportStore()).execute(
            initial_plan,
            approval,
            completed_at=_NOW + timedelta(minutes=3),
        )
    assert missing.value.code is LegacyImportErrorCode.DRY_RUN_REQUIRED

    stale = approval.model_copy(update={"plan_fingerprint": "0" * 64})
    with pytest.raises(LegacyImportError) as mismatched:
        ApplyLegacyControlPlaneImport(source, _MemoryImportStore()).execute(
            initial_plan,
            stale,
            completed_at=_NOW + timedelta(minutes=3),
        )
    assert mismatched.value.code is LegacyImportErrorCode.APPROVAL_MISMATCH

    store = _MemoryImportStore()
    PrepareLegacyControlPlaneImport(source, store).execute(recorded_at=_NOW)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            """
            INSERT INTO workflow_access_grants (
                workflow_id, workspace_id, owner_actor_id, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            ("new-orphan", "workspace-a", "actor-a", _NOW.isoformat()),
        )
        connection.commit()
    with pytest.raises(LegacyImportError) as changed:
        ApplyLegacyControlPlaneImport(source, store).execute(
            initial_plan,
            approval,
            completed_at=_NOW + timedelta(minutes=3),
        )
    assert changed.value.code is LegacyImportErrorCode.SOURCE_CHANGED
    assert store.apply_calls == 0


def test_postgres_import_store_repr_redacts_control_dsn() -> None:
    store = PostgresLegacyControlPlaneImportStore(
        "postgresql://runtime:do-not-print@control.example/control"
    )

    assert "do-not-print" not in repr(store)
    assert "postgresql://" not in repr(store)
