"""Live PostgreSQL proof for durable opaque identity rotation."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from schemabridge.adapters.control_plane.postgres_identity_rotation import (
    PostgresIdentityRotationStore,
)
from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.adapters.control_plane.postgres_registry_control import (
    PostgresRegistryControlStore,
)
from schemabridge.adapters.storage.identity_resolving import (
    IdentityResolvingWorkflowAccessStore,
    IdentityResolvingWorkflowDraftStore,
)
from schemabridge.adapters.storage.postgres import (
    PostgresWorkflowAccessStore,
    PostgresWorkflowDraftStore,
)
from schemabridge.application.identity_rotation import (
    ApproveIdentityRotation,
    CompleteIdentityRotation,
    CompleteReservedIdentityRotation,
    IdentityRotationError,
    IdentityRotationErrorCode,
    InitializeVerifiedIdentityState,
    PrepareIdentityRotation,
    ResolveReservedIdentityRotation,
)
from schemabridge.application.ports.identity_rotation import (
    IdentityRotationStoreError,
    IdentityRotationStoreErrorCode,
)
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.identity import AuthenticationMethod
from schemabridge.domain.identity_rotation import (
    IdentityInitializationApproval,
    IdentityInitializationConfirmation,
    IdentityRotationConfirmation,
    VerifiedDualKeyOidcDerivation,
    VerifiedOidcKeyDerivation,
    build_identity_initialization_approval,
)
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.workflows import AgentWorkflowDraft, WorkflowStage

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations/control_plane"
RUNTIME_DSN = (
    "postgresql://schemabridge_runtime:schemabridge_runtime@127.0.0.1:55434/schemabridge_control"
)
MIGRATOR_DSN = (
    "postgresql://schemabridge_migrator:schemabridge_migrator@127.0.0.1:55434/schemabridge_control"
)
AUDIT_KEYS = {"v1": b"control-audit-key-0123456789-abcdef"}
NOW = datetime(2026, 7, 23, 18, 0, tzinfo=UTC)


def _dsn(name: str, fallback: str) -> str:
    return os.environ.get(name, fallback)


def _runtime_dsn() -> str:
    return _dsn("SCHEMABRIDGE_TEST_CONTROL_DATABASE_URL", RUNTIME_DSN)


def _migrator_dsn() -> str:
    return _dsn(
        "SCHEMABRIDGE_TEST_CONTROL_MIGRATOR_DATABASE_URL",
        MIGRATOR_DSN,
    )


@pytest.fixture(scope="module", autouse=True)
def _require_current_control_schema() -> None:
    PostgresControlPlaneMigrator(_migrator_dsn(), MIGRATIONS).require_current()


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _opaque(kind: str, key_version: str, label: str) -> str:
    return f"sb_{kind}_{key_version}_{_digest(f'{key_version}:{label}')}"


def _pair(
    namespace: str,
    owner: str,
    *,
    from_key: str = "v1",
    to_key: str = "v2",
) -> VerifiedDualKeyOidcDerivation:
    workspace_reference = _digest(f"{namespace}:workspace-reference")
    actor_reference = _digest(f"{namespace}:actor-reference:{owner}")
    verification = _digest(f"{namespace}:verification:{owner}")
    provenance = _digest(f"{namespace}:oidc-provenance")

    def derivation(version: str) -> VerifiedOidcKeyDerivation:
        return VerifiedOidcKeyDerivation(
            verification_id=verification,
            workspace_reference_digest=workspace_reference,
            actor_reference_digest=actor_reference,
            workspace_id=_opaque("workspace", version, namespace),
            actor_id=_opaque("actor", version, f"{namespace}:{owner}"),
            key_version=version,
            provenance_fingerprint=provenance,
            provenance_version=1,
            policy_version=1,
            verified_at=NOW,
            authentication_method=AuthenticationMethod.OIDC,
        )

    return VerifiedDualKeyOidcDerivation(
        previous=derivation(from_key),
        current=derivation(to_key),
    )


def _store() -> PostgresIdentityRotationStore:
    return PostgresIdentityRotationStore(
        _runtime_dsn(),
        AUDIT_KEYS,
        "v1",
    )


def _initialization_approval(
    derivations: tuple[VerifiedDualKeyOidcDerivation, ...],
    *,
    evidence_fingerprint: str | None = None,
) -> IdentityInitializationApproval:
    return build_identity_initialization_approval(
        derivations,
        evidence_fingerprint=evidence_fingerprint
        or _digest(f"signed-evidence:{derivations[0].previous.workspace_id}"),
        actor=min(item.previous.actor_id for item in derivations),
        approved_at=NOW,
        confirmation=(IdentityInitializationConfirmation.INITIALIZE_VERIFIED_OIDC_BINDINGS),
    )


def _initialize(
    store: PostgresIdentityRotationStore,
    derivations: tuple[VerifiedDualKeyOidcDerivation, ...],
):
    approval = _initialization_approval(derivations)
    return InitializeVerifiedIdentityState(store).execute(
        derivations,
        evidence_fingerprint=approval.evidence_fingerprint,
        actor=approval.actor,
        approved_at=approval.approved_at,
        confirmation=approval.confirmation,
    )


def _insert_grant(
    workspace_id: str,
    actor_id: str,
    workflow_id: str,
) -> None:
    with psycopg.connect(_runtime_dsn()) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.agent_workflow_drafts (
                workspace_id, id, revision, payload, updated_at
            ) VALUES (%s, %s, 1, '{}'::jsonb, %s)
            """,
            (workspace_id, workflow_id, NOW),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.workflow_access_grants (
                workspace_id, workflow_id, owner_actor_id, created_at
            ) VALUES (%s, %s, %s, %s)
            """,
            (workspace_id, workflow_id, actor_id, NOW),
        )


def _draft(workflow_id: str, *, revision: int = 1) -> AgentWorkflowDraft:
    return AgentWorkflowDraft(
        id=workflow_id,
        revision=revision,
        stage=WorkflowStage.CREATED,
        text="Cuenta clientes por fecha.",
        language=UserLanguage.SPANISH,
        requested_datasets=(PhysicalDatasetRef("crm.customers"),),
        created_at=NOW,
        updated_at=NOW + timedelta(minutes=revision),
    )


def test_rotation_is_atomic_audited_replay_safe_and_preserves_historical_access() -> None:
    namespace = f"rotation-{uuid4().hex}"
    pairs = (_pair(namespace, "owner-a"), _pair(namespace, "owner-b"))
    old_workspace = pairs[0].previous.workspace_id
    new_workspace = pairs[0].current.workspace_id
    workflow_a = f"wf-{uuid4().hex}"
    workflow_b = f"wf-{uuid4().hex}"
    _insert_grant(old_workspace, pairs[0].previous.actor_id, workflow_a)
    _insert_grant(old_workspace, pairs[1].previous.actor_id, workflow_b)
    with psycopg.connect(_runtime_dsn()) as connection:
        before = connection.execute(
            """
            SELECT draft.payload::text, access.workspace_id,
                   access.owner_actor_id, access.created_at
            FROM schemabridge_control.agent_workflow_drafts AS draft
            JOIN schemabridge_control.workflow_access_grants AS access
              ON access.workspace_id = draft.workspace_id
             AND access.workflow_id = draft.id
            WHERE draft.workspace_id = %s
            ORDER BY draft.id
            """,
            (old_workspace,),
        ).fetchall()

    store = _store()
    initialized = _initialize(store, pairs)
    assert initialized.workspace_id == old_workspace
    assert initialized.revision == 0
    assert initialized.owner_actor_ids == tuple(sorted(pair.previous.actor_id for pair in pairs))
    assert _initialize(store, pairs) == initialized
    rebound = _initialization_approval(
        pairs,
        evidence_fingerprint=_digest("different-signed-evidence"),
    )
    with pytest.raises(IdentityRotationStoreError) as evidence_mismatch:
        store.initialize_verified_state(pairs, rebound)
    assert evidence_mismatch.value.code is IdentityRotationStoreErrorCode.CONFLICT

    plan = PrepareIdentityRotation(store).execute(old_workspace, pairs)
    approved = ApproveIdentityRotation(store).execute(
        plan,
        actor=pairs[0].previous.actor_id,
        approved_at=NOW + timedelta(minutes=1),
        confirmation=IdentityRotationConfirmation.ROTATE_VERIFIED_OIDC_BINDINGS,
    )
    result = CompleteIdentityRotation(store).execute(
        plan,
        approved.approval,
        completed_at=NOW + timedelta(minutes=2),
    )
    assert result.replayed is False

    fresh = _store()
    replayed_approval = ApproveIdentityRotation(fresh).execute(
        plan,
        actor=pairs[0].previous.actor_id,
        approved_at=NOW + timedelta(minutes=1),
        confirmation=IdentityRotationConfirmation.ROTATE_VERIFIED_OIDC_BINDINGS,
    )
    replayed = CompleteIdentityRotation(fresh).execute(
        plan,
        replayed_approval.approval,
        completed_at=NOW + timedelta(minutes=30),
    )
    assert replayed.replayed is True
    assert replayed.completion == result.completion

    current = fresh.load_state(new_workspace)
    assert current.revision == 1
    assert current.active_key_version == "v2"
    assert current.previous_key_versions == ("v1",)
    assert current.owner_actor_ids == tuple(sorted(pair.current.actor_id for pair in pairs))
    assert _initialize(fresh, pairs) == current
    with pytest.raises(IdentityRotationStoreError) as stale_workspace:
        fresh.load_state(old_workspace)
    assert stale_workspace.value.code is IdentityRotationStoreErrorCode.CROSS_WORKSPACE
    resolved = ResolveReservedIdentityRotation(fresh).execute(
        plan.fingerprint,
        pairs,
    )
    cli_replay = CompleteReservedIdentityRotation(fresh).execute(
        resolved,
        approval_id=approved.approval.id,
        actor=approved.approval.actor,
        completed_at=NOW + timedelta(minutes=45),
    )
    assert cli_replay.replayed is True
    assert cli_replay.completion == result.completion

    scopes = fresh.resolve_authorization_scopes(
        new_workspace,
        pairs[0].current.actor_id,
    )
    assert tuple(scope.key_version for scope in scopes) == ("v1", "v2")
    raw_access = PostgresWorkflowAccessStore(_runtime_dsn())
    assert (
        raw_access.load(
            new_workspace,
            workflow_a,
            owner_principal_id=pairs[0].current.actor_id,
        )
        is None
    )
    resolving_access = IdentityResolvingWorkflowAccessStore(raw_access, fresh)
    historical_grant = resolving_access.load(
        new_workspace,
        workflow_a,
        owner_principal_id=pairs[0].current.actor_id,
    )
    assert historical_grant is not None
    assert historical_grant.workspace_id == old_workspace
    assert historical_grant.owner_actor_id == pairs[0].previous.actor_id

    with psycopg.connect(_runtime_dsn()) as connection:
        after = connection.execute(
            """
            SELECT draft.payload::text, access.workspace_id,
                   access.owner_actor_id, access.created_at
            FROM schemabridge_control.agent_workflow_drafts AS draft
            JOIN schemabridge_control.workflow_access_grants AS access
              ON access.workspace_id = draft.workspace_id
             AND access.workflow_id = draft.id
            WHERE draft.workspace_id = %s
            ORDER BY draft.id
            """,
            (old_workspace,),
        ).fetchall()
        persisted = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM schemabridge_control.identity_bindings
                 WHERE workspace_id = %s),
                (SELECT count(*) FROM schemabridge_control.identity_rotation_bindings
                 WHERE plan_id = %s),
                (SELECT payload_json::text
                 FROM schemabridge_control.identity_rotation_plans
                 WHERE plan_id = %s)
            """,
            (old_workspace, plan.id, plan.id),
        ).fetchone()
        initialization_event = connection.execute(
            """
            SELECT event_json
            FROM schemabridge_control.control_audit_events
            WHERE workspace_id = %s
              AND operation = 'identity_rotation_initialized'
            """,
            (old_workspace,),
        ).fetchone()
    assert after == before
    assert persisted is not None
    assert persisted[:2] == (6, 3)
    assert "verification_id" not in str(persisted[2])
    assert "authentication_method" not in str(persisted[2])
    assert "token" not in str(persisted[2])
    assert initialization_event is not None
    assert initialization_event[0]["approval"] == _initialization_approval(pairs).model_dump(
        mode="json"
    )
    serialized_initialization = str(initialization_event[0]).lower()
    for forbidden in (
        "verification_id",
        "authentication_method",
        "email",
        "subject",
        "claims",
        "token",
    ):
        assert forbidden not in serialized_initialization

    verification = PostgresRegistryControlStore(
        _runtime_dsn(),
        AUDIT_KEYS,
        "v1",
    ).verify_audit_chain(old_workspace)
    assert verification.valid is True
    assert verification.event_count == 3
    with (
        psycopg.connect(_migrator_dsn()) as connection,
        pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState),
        connection.transaction(),
    ):
        connection.execute(
            """
            UPDATE schemabridge_control.control_audit_events
            SET operation = 'tampered'
            WHERE workspace_id = %s
              AND operation = 'identity_rotation_approved'
            """,
            (old_workspace,),
        )


def test_rotated_principal_updates_historical_draft_without_rewriting_scope() -> None:
    namespace = f"draft-rotation-{uuid4().hex}"
    pair = _pair(namespace, "owner")
    old_workspace = pair.previous.workspace_id
    new_workspace = pair.current.workspace_id
    workflow_id = f"wf-{uuid4().hex}"
    original = _draft(workflow_id)
    PostgresWorkflowDraftStore(
        _runtime_dsn(),
        workspace_id=old_workspace,
        owner_actor_id=pair.previous.actor_id,
    ).save(original, expected_revision=None)

    rotations = _store()
    _initialize(rotations, (pair,))
    plan = PrepareIdentityRotation(rotations).execute(old_workspace, (pair,))
    approved = ApproveIdentityRotation(rotations).execute(
        plan,
        actor=pair.previous.actor_id,
        approved_at=NOW + timedelta(minutes=1),
        confirmation=IdentityRotationConfirmation.ROTATE_VERIFIED_OIDC_BINDINGS,
    )
    CompleteIdentityRotation(rotations).execute(
        plan,
        approved.approval,
        completed_at=NOW + timedelta(minutes=2),
    )

    active = PostgresWorkflowDraftStore(
        _runtime_dsn(),
        workspace_id=new_workspace,
        owner_actor_id=pair.current.actor_id,
    )
    resolving = IdentityResolvingWorkflowDraftStore(
        active_store=active,
        resolver=rotations,
        workspace_id=new_workspace,
        actor_id=pair.current.actor_id,
        store_factory=lambda workspace_id, actor_id: PostgresWorkflowDraftStore(
            _runtime_dsn(),
            workspace_id=workspace_id,
            owner_actor_id=actor_id,
        ),
    )

    assert resolving.load(workflow_id) == original
    updated = original.model_copy(
        update={
            "revision": 2,
            "updated_at": NOW + timedelta(minutes=5),
        }
    )
    resolving.save(updated, expected_revision=1)
    assert resolving.load(workflow_id) == updated

    with psycopg.connect(_runtime_dsn()) as connection:
        draft_rows = connection.execute(
            """
            SELECT workspace_id, revision
            FROM schemabridge_control.agent_workflow_drafts
            WHERE id = %s
            ORDER BY workspace_id
            """,
            (workflow_id,),
        ).fetchall()
        grant_rows = connection.execute(
            """
            SELECT workspace_id, owner_actor_id
            FROM schemabridge_control.workflow_access_grants
            WHERE workflow_id = %s
            ORDER BY workspace_id
            """,
            (workflow_id,),
        ).fetchall()

    assert draft_rows == [(old_workspace, 2)]
    assert grant_rows == [(old_workspace, pair.previous.actor_id)]


def test_owner_added_after_approval_blocks_completion_without_partial_binding_change() -> None:
    namespace = f"stale-{uuid4().hex}"
    pair = _pair(namespace, "owner-a")
    old_workspace = pair.previous.workspace_id
    _insert_grant(
        old_workspace,
        pair.previous.actor_id,
        f"wf-{uuid4().hex}",
    )
    store = _store()
    _initialize(store, (pair,))
    plan = PrepareIdentityRotation(store).execute(old_workspace, (pair,))
    approved = ApproveIdentityRotation(store).execute(
        plan,
        actor=pair.previous.actor_id,
        approved_at=NOW + timedelta(minutes=3),
        confirmation=IdentityRotationConfirmation.ROTATE_VERIFIED_OIDC_BINDINGS,
    )
    orphan_owner = _opaque("actor", "v1", f"{namespace}:owner-b")
    _insert_grant(old_workspace, orphan_owner, f"wf-{uuid4().hex}")

    with pytest.raises(IdentityRotationError) as incomplete:
        CompleteIdentityRotation(store).execute(
            plan,
            approved.approval,
            completed_at=NOW + timedelta(minutes=4),
        )
    assert incomplete.value.code is IdentityRotationErrorCode.INCOMPLETE_OWNER_BINDING
    with psycopg.connect(_runtime_dsn()) as connection:
        statuses = connection.execute(
            """
            SELECT status, count(*)
            FROM schemabridge_control.identity_bindings
            WHERE workspace_id = %s
            GROUP BY status
            """,
            (old_workspace,),
        ).fetchall()
        plan_row = connection.execute(
            """
            SELECT status, verified_binding_count, completed_at
            FROM schemabridge_control.identity_rotation_plans
            WHERE plan_id = %s
            """,
            (plan.id,),
        ).fetchone()
    assert statuses == [("active", 2)]
    assert plan_row == ("approved", 0, None)


def test_initialization_rejects_omitted_historical_owner_and_writes_nothing() -> None:
    namespace = f"incomplete-{uuid4().hex}"
    pair_a = _pair(namespace, "owner-a")
    pair_b = _pair(namespace, "owner-b")
    old_workspace = pair_a.previous.workspace_id
    _insert_grant(
        old_workspace,
        pair_a.previous.actor_id,
        f"wf-{uuid4().hex}",
    )
    _insert_grant(
        old_workspace,
        pair_b.previous.actor_id,
        f"wf-{uuid4().hex}",
    )

    with pytest.raises(IdentityRotationStoreError) as incomplete:
        _store().initialize_verified_state(
            (pair_a,),
            _initialization_approval((pair_a,)),
        )
    assert incomplete.value.code is IdentityRotationStoreErrorCode.INCOMPLETE_OWNER_BINDING
    with psycopg.connect(_runtime_dsn()) as connection:
        count = connection.execute(
            """
            SELECT count(*)
            FROM schemabridge_control.identity_bindings
            WHERE workspace_id = %s
            """,
            (old_workspace,),
        ).fetchone()
    assert count == (0,)
