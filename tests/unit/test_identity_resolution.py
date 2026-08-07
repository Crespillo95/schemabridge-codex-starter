from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from schemabridge.adapters.control_plane.postgres_identity_rotation import (
    PostgresIdentityRotationStore,
)
from schemabridge.adapters.storage.identity_resolving import (
    IdentityResolvingBackgroundJobApiStore,
    IdentityResolvingWorkflowAccessStore,
    IdentityResolvingWorkflowDraftStore,
)
from schemabridge.application.ports.background_jobs import (
    JobStoreError,
    JobStoreErrorCode,
)
from schemabridge.application.ports.identity_rotation import (
    IdentityRotationStoreError,
    IdentityRotationStoreErrorCode,
)
from schemabridge.application.ports.workflow_access import (
    WorkflowAccessError,
    WorkflowAccessErrorCode,
)
from schemabridge.application.ports.workflows import WorkflowError, WorkflowErrorCode
from schemabridge.domain.background_jobs import (
    BackgroundJob,
    JobAuthorization,
    JobSubmissionResult,
    request_job_cancellation,
)
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.identity import WorkflowAccessGrant
from schemabridge.domain.identity_rotation import IdentityAuthorizationScope
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.workflows import AgentWorkflowDraft, WorkflowStage

NOW = datetime(2026, 7, 23, 17, 0, tzinfo=UTC)


def _opaque(kind: str, version: str, label: str) -> str:
    digest = hashlib.sha256(f"{kind}:{version}:{label}".encode()).hexdigest()
    return f"sb_{kind}_{version}_{digest}"


class _AccessStore:
    def __init__(self, *grants: WorkflowAccessGrant) -> None:
        self.grants = {(grant.workspace_id, grant.workflow_id): grant for grant in grants}

    def grant(self, grant: WorkflowAccessGrant) -> WorkflowAccessGrant:
        key = grant.workspace_id, grant.workflow_id
        existing = self.grants.get(key)
        if existing is not None:
            return existing
        self.grants[key] = grant
        return grant

    def load(
        self,
        workspace_id: str,
        workflow_id: str,
        *,
        owner_principal_id: str | None = None,
    ) -> WorkflowAccessGrant | None:
        grant = self.grants.get((workspace_id, workflow_id))
        if (
            grant is not None
            and owner_principal_id is not None
            and grant.owner_actor_id != owner_principal_id
        ):
            return None
        return grant

    def list_for_workspace(
        self,
        workspace_id: str,
        *,
        owner_principal_id: str | None = None,
        limit: int = 50,
    ) -> tuple[WorkflowAccessGrant, ...]:
        values = tuple(
            grant
            for grant in self.grants.values()
            if grant.workspace_id == workspace_id
            and (owner_principal_id is None or grant.owner_actor_id == owner_principal_id)
        )
        return tuple(
            sorted(
                values,
                key=lambda grant: (-grant.created_at.timestamp(), grant.workflow_id),
            )[:limit]
        )


class _JobStore:
    def __init__(self, *jobs: BackgroundJob) -> None:
        self.jobs = {job.id: job for job in jobs}
        self.calls: list[tuple[object, ...]] = []

    def submit(self, job: BackgroundJob) -> JobSubmissionResult:
        self.calls.append(("submit", job.id))
        self.jobs[job.id] = job
        return JobSubmissionResult(job=job)

    def load_by_idempotency(
        self,
        workspace_id: str,
        submitting_actor_id: str,
        idempotency_digest: str,
    ) -> BackgroundJob | None:
        self.calls.append(
            (
                "load_by_idempotency",
                workspace_id,
                submitting_actor_id,
                idempotency_digest,
            )
        )
        return next(
            (
                job
                for job in self.jobs.values()
                if job.authorization.workspace_id == workspace_id
                and job.authorization.submitting_actor_id == submitting_actor_id
                and job.idempotency_digest == idempotency_digest
            ),
            None,
        )

    def load(
        self,
        workspace_id: str,
        job_id: str,
        *,
        submitting_actor_id: str | None = None,
    ) -> BackgroundJob | None:
        self.calls.append(("load", workspace_id, job_id, submitting_actor_id))
        job = self.jobs.get(job_id)
        if (
            job is None
            or job.authorization.workspace_id != workspace_id
            or (
                submitting_actor_id is not None
                and job.authorization.submitting_actor_id != submitting_actor_id
            )
        ):
            return None
        return job

    def request_cancellation(
        self,
        workspace_id: str,
        job_id: str,
        *,
        submitting_actor_id: str | None,
        requested_at: datetime,
    ) -> BackgroundJob | None:
        self.calls.append(("cancel", workspace_id, job_id, submitting_actor_id))
        job = self.jobs.get(job_id)
        if (
            job is None
            or job.authorization.workspace_id != workspace_id
            or (
                submitting_actor_id is not None
                and job.authorization.submitting_actor_id != submitting_actor_id
            )
        ):
            return None
        changed = request_job_cancellation(job, requested_at=requested_at)
        self.jobs[job_id] = changed
        return changed


class _Resolver:
    def __init__(self) -> None:
        self.old_workspace = _opaque("workspace", "v1", "workspace")
        self.new_workspace = _opaque("workspace", "v2", "workspace")
        self.old_actor = _opaque("actor", "v1", "owner")
        self.new_actor = _opaque("actor", "v2", "owner")

    def resolve_workspace_aliases(self, workspace_id: str) -> tuple[str, ...]:
        if workspace_id != self.new_workspace:
            raise IdentityRotationStoreError(
                IdentityRotationStoreErrorCode.CROSS_WORKSPACE,
                "not active",
            )
        return self.old_workspace, self.new_workspace

    def resolve_authorization_scopes(
        self,
        workspace_id: str,
        actor_id: str,
    ) -> tuple[IdentityAuthorizationScope, ...]:
        if workspace_id != self.new_workspace or actor_id != self.new_actor:
            raise IdentityRotationStoreError(
                IdentityRotationStoreErrorCode.CROSS_WORKSPACE,
                "not active",
            )
        return (
            IdentityAuthorizationScope(
                workspace_id=self.old_workspace,
                actor_id=self.old_actor,
                key_version="v1",
            ),
            IdentityAuthorizationScope(
                workspace_id=self.new_workspace,
                actor_id=self.new_actor,
                key_version="v2",
            ),
        )


class _DraftStore:
    def __init__(self, *drafts: AgentWorkflowDraft) -> None:
        self.drafts = {draft.id: draft for draft in drafts}
        self.saves: list[tuple[str, int | None]] = []

    def load(self, workflow_id: str) -> AgentWorkflowDraft | None:
        return self.drafts.get(workflow_id)

    def save(
        self,
        draft: AgentWorkflowDraft,
        *,
        expected_revision: int | None,
    ) -> None:
        current = self.drafts.get(draft.id)
        if expected_revision is None:
            if current is not None:
                raise WorkflowError(WorkflowErrorCode.CONFLICT, "duplicate")
        elif current is None or current.revision != expected_revision:
            raise WorkflowError(WorkflowErrorCode.CONFLICT, "stale")
        self.drafts[draft.id] = draft
        self.saves.append((draft.id, expected_revision))


def _draft(
    workflow_id: str,
    *,
    revision: int = 1,
) -> AgentWorkflowDraft:
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


def _job(
    *,
    job_id: str,
    workspace_id: str,
    actor_id: str,
    idempotency_digest: str = "d" * 64,
) -> BackgroundJob:
    authorization = JobAuthorization.create(
        workspace_id=workspace_id,
        workflow_id="workflow-historical",
        workflow_owner_actor_id=actor_id,
        submitting_actor_id=actor_id,
        expected_workflow_revision=3,
        expected_plan_fingerprint="a" * 64,
        authenticated_at=NOW - timedelta(minutes=2),
        authorized_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=5),
    )
    return BackgroundJob.create(
        id=job_id,
        authorization=authorization,
        idempotency_digest=idempotency_digest,
        max_attempts=3,
        created_at=NOW,
    )


def test_rotated_principal_replays_reads_and_cancels_historical_job_exactly() -> None:
    resolver = _Resolver()
    historical = _job(
        job_id="execution-job-historical",
        workspace_id=resolver.old_workspace,
        actor_id=resolver.old_actor,
    )
    raw = _JobStore(historical)
    resolving = IdentityResolvingBackgroundJobApiStore(raw, resolver)

    assert (
        resolving.load_by_idempotency(
            resolver.new_workspace,
            resolver.new_actor,
            historical.idempotency_digest,
        )
        == historical
    )
    assert (
        resolving.load(
            resolver.new_workspace,
            historical.id,
            submitting_actor_id=resolver.new_actor,
        )
        == historical
    )
    cancelled = resolving.request_cancellation(
        resolver.new_workspace,
        historical.id,
        submitting_actor_id=resolver.new_actor,
        requested_at=NOW + timedelta(minutes=1),
    )

    assert cancelled is not None
    assert cancelled.authorization.workspace_id == resolver.old_workspace
    assert raw.calls[-1] == (
        "cancel",
        resolver.old_workspace,
        historical.id,
        resolver.old_actor,
    )
    assert all(
        call[1] in {resolver.old_workspace, resolver.new_workspace}
        for call in raw.calls
        if call[0] in {"load", "load_by_idempotency"}
    )


def test_workspace_role_uses_only_verified_workspace_aliases_for_historical_job() -> None:
    resolver = _Resolver()
    historical = _job(
        job_id="execution-job-workspace-role",
        workspace_id=resolver.old_workspace,
        actor_id=resolver.old_actor,
    )
    raw = _JobStore(historical)
    resolving = IdentityResolvingBackgroundJobApiStore(raw, resolver)

    assert resolving.load(resolver.new_workspace, historical.id) == historical
    cancelled = resolving.request_cancellation(
        resolver.new_workspace,
        historical.id,
        submitting_actor_id=None,
        requested_at=NOW + timedelta(minutes=1),
    )

    assert cancelled is not None
    assert raw.calls[-1] == (
        "cancel",
        resolver.old_workspace,
        historical.id,
        None,
    )
    assert not any(call[0] == "load_by_idempotency" for call in raw.calls)


def test_job_resolution_fails_before_store_io_for_unknown_or_invalid_lineage() -> None:
    resolver = _Resolver()
    raw = _JobStore()
    resolving = IdentityResolvingBackgroundJobApiStore(raw, resolver)

    with pytest.raises(JobStoreError) as unknown:
        resolving.load_by_idempotency(
            resolver.old_workspace,
            resolver.old_actor,
            "d" * 64,
        )
    assert unknown.value.code is JobStoreErrorCode.STORE_UNAVAILABLE
    assert raw.calls == []

    class _DuplicateResolver(_Resolver):
        def resolve_authorization_scopes(
            self,
            workspace_id: str,
            actor_id: str,
        ) -> tuple[IdentityAuthorizationScope, ...]:
            scope = IdentityAuthorizationScope(
                workspace_id=workspace_id,
                actor_id=actor_id,
                key_version="v2",
            )
            return scope, scope

    duplicate_raw = _JobStore()
    duplicate = IdentityResolvingBackgroundJobApiStore(
        duplicate_raw,
        _DuplicateResolver(),
    )
    with pytest.raises(JobStoreError) as ambiguous:
        duplicate.load_by_idempotency(
            resolver.new_workspace,
            resolver.new_actor,
            "d" * 64,
        )
    assert ambiguous.value.code is JobStoreErrorCode.STORE_UNAVAILABLE
    assert duplicate_raw.calls == []


def test_job_resolution_rejects_multiple_same_lineage_reservations() -> None:
    resolver = _Resolver()
    old = _job(
        job_id="execution-job-old",
        workspace_id=resolver.old_workspace,
        actor_id=resolver.old_actor,
    )
    current = _job(
        job_id="execution-job-current",
        workspace_id=resolver.new_workspace,
        actor_id=resolver.new_actor,
    )
    raw = _JobStore(old, current)
    resolving = IdentityResolvingBackgroundJobApiStore(raw, resolver)

    with pytest.raises(JobStoreError) as ambiguous:
        resolving.load_by_idempotency(
            resolver.new_workspace,
            resolver.new_actor,
            old.idempotency_digest,
        )
    assert ambiguous.value.code is JobStoreErrorCode.STATE_CONFLICT
    assert not any(call[0] in {"submit", "cancel"} for call in raw.calls)


def test_rotated_principal_loads_and_lists_historical_grants_without_rewrite() -> None:
    resolver = _Resolver()
    historical = WorkflowAccessGrant(
        workflow_id="workflow-historical",
        workspace_id=resolver.old_workspace,
        owner_actor_id=resolver.old_actor,
        created_at=NOW,
    )
    raw = _AccessStore(historical)
    resolving = IdentityResolvingWorkflowAccessStore(raw, resolver)

    assert (
        resolving.load(
            resolver.new_workspace,
            historical.workflow_id,
            owner_principal_id=resolver.new_actor,
        )
        == historical
    )
    assert resolving.list_for_workspace(
        resolver.new_workspace,
        owner_principal_id=resolver.new_actor,
    ) == (historical,)
    assert raw.grants[(resolver.old_workspace, historical.workflow_id)] == historical

    current = WorkflowAccessGrant(
        workflow_id="workflow-current",
        workspace_id=resolver.new_workspace,
        owner_actor_id=resolver.new_actor,
        created_at=NOW + timedelta(minutes=1),
    )
    assert resolving.grant(current) == current
    assert resolving.list_for_workspace(
        resolver.new_workspace,
        owner_principal_id=resolver.new_actor,
    ) == (current, historical)


def test_resolution_fails_closed_for_unknown_or_ambiguous_current_identity() -> None:
    resolver = _Resolver()
    historical = WorkflowAccessGrant(
        workflow_id="workflow-shared",
        workspace_id=resolver.old_workspace,
        owner_actor_id=resolver.old_actor,
        created_at=NOW,
    )
    current = historical.model_copy(
        update={
            "workspace_id": resolver.new_workspace,
            "owner_actor_id": resolver.new_actor,
        }
    )
    resolving = IdentityResolvingWorkflowAccessStore(
        _AccessStore(historical, current),
        resolver,
    )

    with pytest.raises(WorkflowAccessError) as ambiguous:
        resolving.load(
            resolver.new_workspace,
            historical.workflow_id,
            owner_principal_id=resolver.new_actor,
        )
    assert ambiguous.value.code is WorkflowAccessErrorCode.CONFLICT

    with pytest.raises(WorkflowAccessError) as unknown:
        resolving.load(
            resolver.old_workspace,
            historical.workflow_id,
            owner_principal_id=resolver.old_actor,
        )
    assert unknown.value.code is WorkflowAccessErrorCode.IDENTITY_MISMATCH

    class _UnavailableResolver(_Resolver):
        def resolve_authorization_scopes(
            self,
            workspace_id: str,
            actor_id: str,
        ) -> tuple[IdentityAuthorizationScope, ...]:
            del workspace_id, actor_id
            raise IdentityRotationStoreError(
                IdentityRotationStoreErrorCode.UNAVAILABLE,
                "sanitized unavailable",
            )

    unavailable_resolver = _UnavailableResolver()
    unavailable = IdentityResolvingWorkflowAccessStore(
        _AccessStore(),
        unavailable_resolver,
    )
    with pytest.raises(WorkflowAccessError) as store_failure:
        unavailable.load(
            unavailable_resolver.new_workspace,
            historical.workflow_id,
            owner_principal_id=unavailable_resolver.new_actor,
        )
    assert store_failure.value.code is WorkflowAccessErrorCode.STORE_FAILURE


def test_rotated_principal_loads_and_updates_historical_draft_in_its_original_scope() -> None:
    resolver = _Resolver()
    historical = _draft("workflow-historical")
    old_store = _DraftStore(historical)
    active_store = _DraftStore()
    stores = {
        (resolver.old_workspace, resolver.old_actor): old_store,
        (resolver.new_workspace, resolver.new_actor): active_store,
    }
    resolving = IdentityResolvingWorkflowDraftStore(
        active_store=active_store,
        resolver=resolver,
        workspace_id=resolver.new_workspace,
        actor_id=resolver.new_actor,
        store_factory=lambda workspace_id, actor_id: stores[(workspace_id, actor_id)],
    )

    assert resolving.load(historical.id) == historical
    updated = historical.model_copy(
        update={
            "revision": 2,
            "updated_at": NOW + timedelta(minutes=5),
        }
    )
    resolving.save(updated, expected_revision=1)

    assert old_store.load(historical.id) == updated
    assert old_store.saves == [(historical.id, 1)]
    assert active_store.load(historical.id) is None
    assert active_store.saves == []

    current = _draft("workflow-current")
    resolving.save(current, expected_revision=None)
    assert active_store.load(current.id) == current
    assert old_store.load(current.id) is None


def test_draft_resolution_rejects_unknown_and_multiple_same_lineage_matches() -> None:
    resolver = _Resolver()
    duplicate = _draft("workflow-duplicate")
    old_store = _DraftStore(duplicate)
    active_store = _DraftStore(duplicate)
    resolving = IdentityResolvingWorkflowDraftStore(
        active_store=active_store,
        resolver=resolver,
        workspace_id=resolver.new_workspace,
        actor_id=resolver.new_actor,
        store_factory=lambda workspace_id, actor_id: (
            old_store if workspace_id == resolver.old_workspace else active_store
        ),
    )

    with pytest.raises(WorkflowError) as ambiguous:
        resolving.load(duplicate.id)
    assert ambiguous.value.code is WorkflowErrorCode.CONFLICT

    unknown = IdentityResolvingWorkflowDraftStore(
        active_store=_DraftStore(),
        resolver=resolver,
        workspace_id=resolver.old_workspace,
        actor_id=resolver.old_actor,
        store_factory=lambda _workspace_id, _actor_id: old_store,
    )
    with pytest.raises(WorkflowError) as unavailable:
        unknown.load("workflow-unknown")
    assert unavailable.value.code is WorkflowErrorCode.STORE_FAILURE
    with pytest.raises(WorkflowError) as create_unavailable:
        unknown.save(_draft("workflow-new"), expected_revision=None)
    assert create_unavailable.value.code is WorkflowErrorCode.STORE_FAILURE


def test_postgres_rotation_store_rejects_weak_audit_keys_and_redacts_secrets() -> None:
    with pytest.raises(ValueError, match="audit signing keys"):
        PostgresIdentityRotationStore(
            "postgresql://user:secret@control.invalid/control",
            {"v1": b"weak"},
            "v1",
        )

    store = PostgresIdentityRotationStore(
        "postgresql://user:secret@control.invalid/control",
        {"v1": b"identity-audit-key-0123456789-abcdef"},
        "v1",
    )
    rendered = repr(store)
    assert "secret" not in rendered
    assert "identity-audit-key" not in rendered
