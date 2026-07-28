"""Real PostgreSQL integration coverage for the M23 managed-state stores."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.adapters.datahub.fake_join_context import (
    FakeJoinContextAdapter,
    FakeJoinContextBackend,
)
from schemabridge.adapters.datahub.fake_writeback import FakeCatalogWriteAdapter
from schemabridge.adapters.semantic_registry.recorded import (
    RecordedGovernedSemanticRegistry,
)
from schemabridge.adapters.storage.postgres import (
    PostgresJoinReviewStore,
    PostgresPublicationAuditStore,
    PostgresRequestDraftStore,
    PostgresReviewStore,
    PostgresWorkflowAccessStore,
    PostgresWorkflowDraftStore,
)
from schemabridge.application.canonical_review import (
    DecideCanonicalMapping,
    PrepareCanonicalPublication,
)
from schemabridge.application.governed_execution import PlanSemanticRequest
from schemabridge.application.guided_requests import (
    BuildGuidedRequest,
    GuidedRequestCase,
    build_demo_guided_input,
)
from schemabridge.application.join_demo import (
    build_join_review_draft,
    build_north_star_join_proposals,
)
from schemabridge.application.join_discovery import (
    DecideJoinCandidate,
    JoinDiscoveryReport,
    PrepareJoinPublication,
)
from schemabridge.application.ports.publication_audit import PublicationAuditStoreError
from schemabridge.application.ports.relationships import (
    RelationshipErrorCode,
    RelationshipWorkflowError,
)
from schemabridge.application.ports.requests import (
    RequestWorkflowError,
    RequestWorkflowErrorCode,
)
from schemabridge.application.ports.reviews import ReviewErrorCode, ReviewWorkflowError
from schemabridge.application.ports.workflow_access import (
    WorkflowAccessError,
    WorkflowAccessErrorCode,
)
from schemabridge.application.ports.workflows import WorkflowError, WorkflowErrorCode
from schemabridge.application.review_demo import build_customer_review_draft
from schemabridge.domain.concepts import LogicalFieldRef, LogicalModelRef
from schemabridge.domain.decisions import DecisionAction, DecisionRecord
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.join_reviews import (
    JoinContractPublication,
    JoinPublicationApproval,
    JoinPublicationConfirmation,
    JoinReviewDraft,
)
from schemabridge.domain.joins import (
    DeclaredRelationship,
    RelationshipProfile,
    score_join_candidate,
)
from schemabridge.domain.publication_audit import (
    PublicationAuditOutcome,
    PublicationFamily,
    PublicationTargetAuditRecord,
)
from schemabridge.domain.requests import (
    AnalyticalRequest,
    AnalyticalRequestDraft,
    Dimension,
    Metric,
    MetricOperation,
)
from schemabridge.domain.resolution import (
    ResolutionLimits,
    resolved_semantic_plan_fingerprint,
)
from schemabridge.domain.reviews import (
    CanonicalPublication,
    CanonicalReviewDraft,
    PublicationApproval,
    PublicationConfirmation,
    mapping_target_id,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope
from schemabridge.domain.workflows import (
    AgentWorkflowDraft,
    WorkflowExecutionRecord,
    WorkflowStage,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations/control_plane"
MANIFEST = ROOT / "demo/ground_truth/registries/manifest.yml"
RUNTIME_DSN = (
    "postgresql://schemabridge_runtime:schemabridge_runtime@127.0.0.1:55434/schemabridge_control"
)
MIGRATOR_DSN = (
    "postgresql://schemabridge_migrator:schemabridge_migrator@127.0.0.1:55434/schemabridge_control"
)
NOW = datetime(2026, 7, 23, 16, 0, tzinfo=UTC)


def _dsn(name: str, fallback: str) -> str:
    return os.environ.get(name, fallback)


def _runtime_dsn() -> str:
    return _dsn("SCHEMABRIDGE_TEST_CONTROL_DATABASE_URL", RUNTIME_DSN)


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


@pytest.fixture(scope="module", autouse=True)
def _require_current_control_schema() -> None:
    PostgresControlPlaneMigrator(
        _dsn("SCHEMABRIDGE_TEST_CONTROL_MIGRATOR_DATABASE_URL", MIGRATOR_DSN),
        MIGRATIONS,
    ).require_current()


def test_workflow_is_minimized_durable_and_scoped_for_rbac_authorization() -> None:
    workspace_id = _unique("workspace")
    other_workspace_id = _unique("workspace")
    owner_actor_id = _unique("owner")
    other_actor_id = _unique("actor")
    workflow_id = _unique("wf")
    original = _executed_workflow(workflow_id, workspace_id)
    owner_store = PostgresWorkflowDraftStore(
        _runtime_dsn(),
        workspace_id=workspace_id,
        owner_actor_id=owner_actor_id,
    )

    owner_store.save(original, expected_revision=None)

    fresh_owner_store = PostgresWorkflowDraftStore(
        _runtime_dsn(),
        workspace_id=workspace_id,
        owner_actor_id=owner_actor_id,
    )
    loaded = fresh_owner_store.load(workflow_id)
    assert loaded is not None
    assert loaded.execution is not None
    assert loaded.execution.rows == ()
    assert loaded.execution.observed_row_count == 2
    assert loaded.execution.preview_fingerprint == "c" * 64

    with psycopg.connect(_runtime_dsn()) as connection:
        persisted = connection.execute(
            """
            SELECT payload, execution_row_count, execution_preview_fingerprint
            FROM schemabridge_control.agent_workflow_drafts
            WHERE workspace_id = %s AND id = %s
            """,
            (workspace_id, workflow_id),
        ).fetchone()
    assert persisted is not None
    payload = persisted[0]
    assert isinstance(payload, dict)
    execution_payload = payload.get("execution")
    assert isinstance(execution_payload, dict)
    assert execution_payload["rows"] == []
    assert execution_payload["row_count"] == 2
    assert persisted[1:] == (2, "c" * 64)
    assert "2026-01-01" not in str(payload)
    assert "2026-01-02" not in str(payload)

    access_store = PostgresWorkflowAccessStore(_runtime_dsn())
    grant = access_store.load(workspace_id, workflow_id)
    assert grant is not None
    assert grant.owner_actor_id == owner_actor_id
    assert access_store.list_for_workspace(workspace_id) == (grant,)
    assert (
        access_store.load(
            workspace_id,
            workflow_id,
            owner_principal_id=owner_actor_id,
        )
        == grant
    )
    assert (
        access_store.load(
            workspace_id,
            workflow_id,
            owner_principal_id=other_actor_id,
        )
        is None
    )
    assert access_store.load(other_workspace_id, workflow_id) is None
    assert (
        access_store.grant(
            grant.model_copy(update={"created_at": grant.created_at + timedelta(minutes=1)})
        )
        == grant
    )
    with pytest.raises(WorkflowAccessError) as ownership_conflict:
        access_store.grant(grant.model_copy(update={"owner_actor_id": other_actor_id}))
    assert ownership_conflict.value.code is WorkflowAccessErrorCode.CONFLICT

    workspace_authorized_store = PostgresWorkflowDraftStore(
        _runtime_dsn(),
        workspace_id=workspace_id,
        owner_actor_id=other_actor_id,
    )
    assert workspace_authorized_store.load(workflow_id) == loaded
    isolated_store = PostgresWorkflowDraftStore(
        _runtime_dsn(),
        workspace_id=other_workspace_id,
        owner_actor_id=owner_actor_id,
    )
    assert isolated_store.load(workflow_id) is None

    updated = AgentWorkflowDraft.model_validate(
        {
            **loaded.model_dump(mode="python"),
            "revision": loaded.revision + 1,
            "updated_at": loaded.updated_at + timedelta(minutes=1),
        }
    )
    workspace_authorized_store.save(updated, expected_revision=loaded.revision)
    stale = AgentWorkflowDraft.model_validate(
        {
            **loaded.model_dump(mode="python"),
            "revision": loaded.revision + 1,
            "updated_at": loaded.updated_at + timedelta(minutes=2),
        }
    )
    with pytest.raises(WorkflowError) as stale_conflict:
        owner_store.save(stale, expected_revision=loaded.revision)
    assert stale_conflict.value.code is WorkflowErrorCode.CONFLICT
    assert fresh_owner_store.load(workflow_id) == updated


def test_request_drafts_survive_restart_and_reject_stale_or_cross_workspace_reads() -> None:
    workspace_id = _unique("workspace")
    other_workspace_id = _unique("workspace")
    draft_id = _unique("request")
    store = PostgresRequestDraftStore(_runtime_dsn(), workspace_id=workspace_id)
    original = _request_draft(draft_id, revision=1, limit=20)

    store.save(original, expected_revision=None)

    fresh = PostgresRequestDraftStore(_runtime_dsn(), workspace_id=workspace_id)
    assert fresh.load(draft_id) == original
    assert (
        PostgresRequestDraftStore(
            _runtime_dsn(),
            workspace_id=other_workspace_id,
        ).load(draft_id)
        is None
    )

    updated = _request_draft(draft_id, revision=2, limit=40)
    fresh.save(updated, expected_revision=1)
    stale = _request_draft(draft_id, revision=2, limit=80)
    with pytest.raises(RequestWorkflowError) as conflict:
        store.save(stale, expected_revision=1)
    assert conflict.value.code is RequestWorkflowErrorCode.DRAFT_CONFLICT
    assert (
        PostgresRequestDraftStore(_runtime_dsn(), workspace_id=workspace_id).load(draft_id)
        == updated
    )


def test_canonical_review_history_and_publication_replays_are_immutable() -> None:
    workspace_id = _unique("workspace")
    other_workspace_id = _unique("workspace")
    store = PostgresReviewStore(_runtime_dsn(), workspace_id=workspace_id)
    original = build_customer_review_draft()

    assert store.create(original) == original
    assert (
        PostgresReviewStore(_runtime_dsn(), workspace_id=workspace_id).create(original) == original
    )
    incompatible = CanonicalReviewDraft.model_validate(
        {
            **original.model_dump(mode="python"),
            "logical_model": original.logical_model.model_copy(
                update={
                    "description": (
                        "Incompatible synthetic definition for the same review identity."
                    )
                }
            ),
        }
    )
    with pytest.raises(ReviewWorkflowError) as create_conflict:
        store.create(incompatible)
    assert create_conflict.value.code is ReviewErrorCode.CONFLICT

    current = original
    decider = DecideCanonicalMapping(store)
    for item in original.mappings:
        snapshot = decider.execute(
            original.id,
            mapping_target_id(item.mapping),
            DecisionAction.APPROVE,
            expected_revision=current.revision,
            actor="synthetic-canonical-steward",
            decided_at=NOW,
            rationale="Synthetic mapping evidence reviewed and accepted.",
        )
        current = snapshot.draft

    fresh = PostgresReviewStore(_runtime_dsn(), workspace_id=workspace_id)
    decisions = fresh.list_decisions(original.id)
    assert fresh.load(original.id) == current
    assert decisions == snapshot.decisions
    assert tuple(decision.resulting_version for decision in decisions) == (2, 3, 4, 5)
    assert (
        PostgresReviewStore(
            _runtime_dsn(),
            workspace_id=other_workspace_id,
        ).load(original.id)
        is None
    )

    replacement = CanonicalReviewDraft.model_validate(
        {
            **current.model_dump(mode="python"),
            "revision": current.revision + 1,
            "logical_model": current.logical_model.model_copy(
                update={"version": current.revision + 1}
            ),
        }
    )
    duplicate_decision = DecisionRecord.model_validate(
        {
            **decisions[0].model_dump(mode="python"),
            "source_version": current.revision,
            "resulting_version": replacement.revision,
            "rationale": "Attempted reuse of an immutable synthetic decision id.",
        }
    )
    with pytest.raises(ReviewWorkflowError) as immutable_conflict:
        fresh.commit_decision(
            replacement,
            duplicate_decision,
            expected_revision=current.revision,
        )
    assert immutable_conflict.value.code is ReviewErrorCode.CONFLICT
    assert fresh.load(original.id) == current
    assert fresh.list_decisions(original.id) == decisions

    publication = PrepareCanonicalPublication(fresh).execute(original.id)
    approval = _canonical_approval(publication, _unique("canonical-approval"))
    writer = FakeCatalogWriteAdapter()
    first_result = writer.publish(publication, approval)
    incompatible_replay = writer.publish(publication, approval)
    assert incompatible_replay != first_result

    fresh.record_publication(first_result)
    PostgresReviewStore(_runtime_dsn(), workspace_id=workspace_id).record_publication(first_result)
    assert fresh.list_publications(original.id) == (first_result,)
    with pytest.raises(ReviewWorkflowError) as publication_conflict:
        fresh.record_publication(incompatible_replay)
    assert publication_conflict.value.code is ReviewErrorCode.CONFLICT
    assert fresh.list_publications(original.id) == (first_result,)


def test_join_review_history_and_publication_replays_are_immutable() -> None:
    workspace_id = _unique("workspace")
    other_workspace_id = _unique("workspace")
    store = PostgresJoinReviewStore(_runtime_dsn(), workspace_id=workspace_id)
    original = _join_review_draft()

    assert store.create(original) == original
    assert (
        PostgresJoinReviewStore(_runtime_dsn(), workspace_id=workspace_id).create(original)
        == original
    )
    incompatible = JoinReviewDraft.model_validate(
        {**original.model_dump(mode="python"), "revision": original.revision + 1}
    )
    with pytest.raises(RelationshipWorkflowError) as create_conflict:
        store.create(incompatible)
    assert create_conflict.value.code is RelationshipErrorCode.CONFLICT

    current = original
    decider = DecideJoinCandidate(store)
    for item in original.joins:
        snapshot = decider.execute(
            original.id,
            item.candidate.proposal.id,
            DecisionAction.APPROVE,
            expected_revision=current.revision,
            actor="synthetic-join-steward",
            decided_at=NOW,
            rationale="Synthetic relationship evidence and fanout policy accepted.",
        )
        current = snapshot.draft

    fresh = PostgresJoinReviewStore(_runtime_dsn(), workspace_id=workspace_id)
    decisions = fresh.list_decisions(original.id)
    assert fresh.load(original.id) == current
    assert decisions == snapshot.decisions
    assert tuple(decision.resulting_version for decision in decisions) == (2, 3)
    assert (
        PostgresJoinReviewStore(
            _runtime_dsn(),
            workspace_id=other_workspace_id,
        ).load(original.id)
        is None
    )

    replacement = JoinReviewDraft.model_validate(
        {**current.model_dump(mode="python"), "revision": current.revision + 1}
    )
    duplicate_decision = DecisionRecord.model_validate(
        {
            **decisions[0].model_dump(mode="python"),
            "source_version": current.revision,
            "resulting_version": replacement.revision,
            "rationale": "Attempted reuse of an immutable synthetic join decision id.",
        }
    )
    with pytest.raises(RelationshipWorkflowError) as immutable_conflict:
        fresh.commit_decision(
            replacement,
            duplicate_decision,
            expected_revision=current.revision,
        )
    assert immutable_conflict.value.code is RelationshipErrorCode.CONFLICT
    assert fresh.load(original.id) == current
    assert fresh.list_decisions(original.id) == decisions

    publication = PrepareJoinPublication(fresh).execute(original.id)
    approval = _join_approval(publication, _unique("join-approval"))
    writer = FakeJoinContextAdapter(FakeJoinContextBackend())
    first_result = writer.publish(publication, approval)
    incompatible_replay = writer.publish(publication, approval)
    assert incompatible_replay != first_result

    fresh.record_publication(first_result)
    PostgresJoinReviewStore(_runtime_dsn(), workspace_id=workspace_id).record_publication(
        first_result
    )
    assert fresh.list_publications(original.id) == (first_result,)
    with pytest.raises(RelationshipWorkflowError) as publication_conflict:
        fresh.record_publication(incompatible_replay)
    assert publication_conflict.value.code is RelationshipErrorCode.CONFLICT
    assert fresh.list_publications(original.id) == (first_result,)


def test_publication_audit_replay_is_exact_append_only_and_workspace_scoped() -> None:
    workspace_id = _unique("workspace")
    other_workspace_id = _unique("workspace")
    approval_id = _unique("audit-approval")
    records = _audit_records(approval_id)
    store = PostgresPublicationAuditStore(_runtime_dsn(), workspace_id=workspace_id)

    store.append(records)
    PostgresPublicationAuditStore(_runtime_dsn(), workspace_id=workspace_id).append(records)

    fresh = PostgresPublicationAuditStore(_runtime_dsn(), workspace_id=workspace_id)
    assert fresh.list_for_approval(approval_id) == records
    assert (
        PostgresPublicationAuditStore(
            _runtime_dsn(),
            workspace_id=other_workspace_id,
        ).list_for_approval(approval_id)
        == ()
    )

    changed_targets = (
        PublicationTargetAuditRecord.model_validate(
            {
                **records[0].model_dump(mode="python"),
                "target": "urn:li:dataset:synthetic-conflict",
            }
        ),
        records[1],
    )
    with pytest.raises(PublicationAuditStoreError, match="targets changed"):
        fresh.append(changed_targets)

    changed_identity = tuple(
        PublicationTargetAuditRecord.model_validate(
            {
                **record.model_dump(mode="python"),
                "actor": "different-synthetic-steward",
            }
        )
        for record in records
    )
    with pytest.raises(PublicationAuditStoreError, match="identity changed"):
        fresh.append(changed_identity)
    assert fresh.list_for_approval(approval_id) == records


def test_publication_audit_allows_bound_outcome_after_reservation_without_duplicates() -> None:
    workspace_id = _unique("workspace")
    approval_id = _unique("audit-approval")
    succeeded = _audit_records(approval_id)[0]
    reserved = PublicationTargetAuditRecord.model_validate(
        {
            **succeeded.model_dump(mode="python"),
            "outcome": PublicationAuditOutcome.NOT_ATTEMPTED,
            "reason_code": "approval_reserved",
        }
    )
    store = PostgresPublicationAuditStore(_runtime_dsn(), workspace_id=workspace_id)

    store.append((reserved,))
    store.append((succeeded,))
    store.append((succeeded,))

    assert store.list_for_approval(approval_id) == (reserved, succeeded)
    with pytest.raises(PublicationAuditStoreError, match="targets changed"):
        store.append(
            (
                PublicationTargetAuditRecord.model_validate(
                    {
                        **succeeded.model_dump(mode="python"),
                        "target": "urn:li:dataset:synthetic-conflict",
                    }
                ),
            )
        )


def _executed_workflow(workflow_id: str, workspace_id: str) -> AgentWorkflowDraft:
    registry = RecordedGovernedSemanticRegistry(
        MANIFEST,
        SemanticRegistryScope(
            workspace_id=workspace_id,
            catalog_scope="synthetic-demo",
            registry_id="synthetic_enterprise",
        ),
    )
    validated = BuildGuidedRequest(registry).execute(
        build_demo_guided_input(GuidedRequestCase.NORTH_STAR)
    )
    resolved = PlanSemanticRequest(registry, ResolutionLimits()).execute(validated)
    plan_fingerprint = resolved_semantic_plan_fingerprint(resolved)
    execution = WorkflowExecutionRecord(
        plan_fingerprint=plan_fingerprint,
        query_fingerprint="b" * 64,
        columns=("registration_date", "customer_count"),
        rows=(("2026-01-01", 2), ("2026-01-02", 1)),
        row_count=2,
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


def _request_draft(draft_id: str, *, revision: int, limit: int) -> AnalyticalRequestDraft:
    return AnalyticalRequestDraft(
        id=draft_id,
        revision=revision,
        request=AnalyticalRequest(
            primary_entity=LogicalModelRef("Customer"),
            dimensions=(Dimension(field=LogicalFieldRef("Customer.registration_date")),),
            metrics=(
                Metric(
                    operation=MetricOperation.COUNT_DISTINCT,
                    field=LogicalFieldRef("Customer.customer_key"),
                    alias="customer_count",
                ),
            ),
            limit=limit,
        ),
    )


def _canonical_approval(
    publication: CanonicalPublication,
    approval_id: str,
) -> PublicationApproval:
    return PublicationApproval(
        id=approval_id,
        draft_id=publication.draft_id,
        draft_version=publication.draft_version,
        payload_fingerprint=publication.fingerprint,
        actor="synthetic-canonical-steward",
        approved_at=NOW,
        decision_ids=tuple(decision.id for decision in publication.decisions),
        confirmation=PublicationConfirmation.PUBLISH_APPROVED_CANONICAL_CONTEXT,
    )


def _join_review_draft() -> JoinReviewDraft:
    proposals = build_north_star_join_proposals()
    profiles = (
        RelationshipProfile(
            left_row_count=7,
            right_row_count=9,
            left_null_count=0,
            right_null_count=1,
            left_invalid_count=0,
            right_invalid_count=2,
            left_distinct_valid=7,
            right_distinct_valid=5,
            matching_distinct_keys=5,
            left_max_multiplicity=1,
            right_max_multiplicity=2,
        ),
        RelationshipProfile(
            left_row_count=9,
            right_row_count=9,
            left_null_count=0,
            right_null_count=0,
            left_invalid_count=0,
            right_invalid_count=0,
            left_distinct_valid=9,
            right_distinct_valid=9,
            matching_distinct_keys=9,
            left_max_multiplicity=1,
            right_max_multiplicity=1,
            declared_relationship=DeclaredRelationship.LEFT_FOREIGN_KEY_TO_RIGHT,
        ),
    )
    report = JoinDiscoveryReport(
        catalog_source="synthetic",
        candidates=tuple(
            score_join_candidate(proposal, profile)
            for proposal, profile in zip(proposals, profiles, strict=True)
        ),
    )
    return build_join_review_draft(report)


def _join_approval(
    publication: JoinContractPublication,
    approval_id: str,
) -> JoinPublicationApproval:
    return JoinPublicationApproval(
        id=approval_id,
        draft_id=publication.draft_id,
        draft_version=publication.draft_version,
        payload_fingerprint=publication.fingerprint,
        actor="synthetic-join-steward",
        approved_at=NOW,
        decision_ids=tuple(decision.id for decision in publication.decisions),
        confirmation=JoinPublicationConfirmation.PUBLISH_APPROVED_JOIN_CONTRACTS,
    )


def _audit_records(approval_id: str) -> tuple[PublicationTargetAuditRecord, ...]:
    common = {
        "family": PublicationFamily.CANONICAL,
        "approval_id": approval_id,
        "actor": "synthetic-audit-steward",
        "approved_at": NOW,
        "previous_fingerprint": None,
        "new_fingerprint": "d" * 64,
        "outcome": PublicationAuditOutcome.SUCCEEDED,
        "decision_ids": ("synthetic-decision",),
    }
    return (
        PublicationTargetAuditRecord(
            operation="logical_model",
            target="urn:li:dataset:synthetic-customer",
            **common,
        ),
        PublicationTargetAuditRecord(
            operation="physical_link",
            target="urn:li:schemaField:synthetic-customer.customer_key",
            **common,
        ),
    )
