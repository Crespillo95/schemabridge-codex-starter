"""Deterministic in-memory store for isolated M35 authoring tests."""

from __future__ import annotations

from threading import RLock

from schemabridge.application.ports.registry_changes import (
    RegistryChangeOperationReplay,
    RegistryChangeStoreError,
    RegistryChangeStoreErrorCode,
)
from schemabridge.domain.registry_change_authoring import (
    MAX_REGISTRY_CHANGE_HISTORY,
    RegistryChangeAuditRecord,
    RegistryJoinDraftMutation,
    RegistryJoinPreparation,
    RegistryJoinProfileAuthoringMutation,
    RegistryJoinProfileAuthoringRequest,
)
from schemabridge.domain.registry_changes import (
    PreparedRegistryJoinProposal,
    RegistryJoinChangeDraft,
    RegistryJoinChangeStatus,
)
from schemabridge.domain.semantic_profile_jobs import SemanticJoinProfileJob


class InMemoryRegistryChangeStore:
    """Atomic tenant-scoped reference store; never a managed-runtime backend."""

    def __init__(self) -> None:
        self._authoring: dict[tuple[str, str], RegistryJoinProfileAuthoringRequest] = {}
        self._jobs: dict[tuple[str, str], SemanticJoinProfileJob] = {}
        self._drafts: dict[tuple[str, str], RegistryJoinChangeDraft] = {}
        self._proposals: dict[tuple[str, str], PreparedRegistryJoinProposal] = {}
        self._audit: dict[tuple[str, str], list[RegistryChangeAuditRecord]] = {}
        self._operations: dict[tuple[str, str], RegistryChangeOperationReplay] = {}
        self._lock = RLock()

    def load_operation_replay(
        self,
        workspace_id: str,
        idempotency_digest: str,
    ) -> RegistryChangeOperationReplay | None:
        with self._lock:
            return self._operations.get((workspace_id, idempotency_digest))

    def persist_profile_request(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
        audit: RegistryChangeAuditRecord,
        *,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryJoinProfileAuthoringMutation:
        with self._lock:
            replay = self._operations.get((authoring.workspace_id, idempotency_digest))
            if replay is not None:
                if (
                    replay.operation == operation
                    and replay.actor_id == actor_id
                    and replay.request_fingerprint == request_fingerprint
                    and replay.authoring == authoring
                ):
                    return RegistryJoinProfileAuthoringMutation(
                        authoring=replay.authoring,
                        job=replay.job,
                        replayed=True,
                    )
                raise _conflict()
            key = (authoring.workspace_id, authoring.id)
            if key in self._authoring or any(
                candidate.workspace_id == authoring.workspace_id
                and candidate.request.scan_id == authoring.request.scan_id
                for candidate in self._authoring.values()
            ):
                raise _conflict()
            _require_audit_binding(audit, authoring.workspace_id, authoring.id)
            self._authoring[key] = authoring
            self._audit[key] = [audit]
            self._operations[(authoring.workspace_id, idempotency_digest)] = (
                RegistryChangeOperationReplay(
                    operation=operation,
                    request_fingerprint=request_fingerprint,
                    actor_id=actor_id,
                    authoring=authoring,
                )
            )
            return RegistryJoinProfileAuthoringMutation(authoring=authoring)

    def bind_profile_job(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
        job: SemanticJoinProfileJob,
        audit: RegistryChangeAuditRecord,
        *,
        idempotency_digest: str,
    ) -> RegistryJoinProfileAuthoringMutation:
        with self._lock:
            key = (authoring.workspace_id, authoring.id)
            replay_key = (authoring.workspace_id, idempotency_digest)
            replay = self._operations.get(replay_key)
            if self._authoring.get(key) != authoring or replay is None:
                raise _conflict()
            existing = self._jobs.get(key)
            if existing is not None:
                if existing == job and replay.job == job:
                    return RegistryJoinProfileAuthoringMutation(
                        authoring=authoring,
                        job=job,
                        replayed=True,
                    )
                raise _conflict()
            if replay.authoring != authoring or replay.job is not None:
                raise _conflict()
            mutation = RegistryJoinProfileAuthoringMutation(
                authoring=authoring,
                job=job,
            )
            _require_audit_binding(audit, authoring.workspace_id, authoring.id)
            self._jobs[key] = job
            self._audit[key].append(audit)
            self._operations[replay_key] = RegistryChangeOperationReplay(
                operation=replay.operation,
                request_fingerprint=replay.request_fingerprint,
                actor_id=replay.actor_id,
                authoring=authoring,
                job=job,
            )
            return mutation

    def load_authoring(
        self,
        workspace_id: str,
        change_id: str,
    ) -> RegistryJoinProfileAuthoringRequest | None:
        with self._lock:
            return self._authoring.get((workspace_id, change_id))

    def load_draft(
        self,
        workspace_id: str,
        change_id: str,
    ) -> RegistryJoinChangeDraft | None:
        with self._lock:
            return self._drafts.get((workspace_id, change_id))

    def list_for_workspace(
        self,
        workspace_id: str,
        *,
        owner_actor_id: str | None,
        limit: int,
    ) -> tuple[RegistryJoinProfileAuthoringRequest, ...]:
        if not 1 <= limit <= 50:
            raise _unavailable()
        with self._lock:
            values = [
                item
                for (candidate_workspace, _), item in self._authoring.items()
                if candidate_workspace == workspace_id
                and (owner_actor_id is None or item.owner_actor_id == owner_actor_id)
            ]
            return tuple(
                sorted(
                    values,
                    key=lambda item: (item.created_at, item.id),
                    reverse=True,
                )[:limit]
            )

    def commit_draft(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
        draft: RegistryJoinChangeDraft,
        audit: RegistryChangeAuditRecord,
        *,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryJoinDraftMutation:
        with self._lock:
            replay = self._exact_operation(
                authoring,
                operation=operation,
                actor_id=actor_id,
                idempotency_digest=idempotency_digest,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None:
                if replay.draft is None:
                    raise _conflict()
                return RegistryJoinDraftMutation(
                    authoring=authoring,
                    draft=replay.draft,
                    replayed=True,
                )
            key = (authoring.workspace_id, authoring.id)
            if (
                self._authoring.get(key) != authoring
                or key not in self._jobs
                or key in self._drafts
                or draft.revision != 1
            ):
                raise _conflict()
            mutation = RegistryJoinDraftMutation(authoring=authoring, draft=draft)
            _require_audit_binding(audit, authoring.workspace_id, authoring.id)
            if audit.resulting_fingerprint != draft.fingerprint:
                raise _invalid_response()
            self._drafts[key] = draft
            self._audit[key].append(audit)
            self._operations[(authoring.workspace_id, idempotency_digest)] = (
                RegistryChangeOperationReplay(
                    operation=operation,
                    request_fingerprint=request_fingerprint,
                    actor_id=actor_id,
                    authoring=authoring,
                    job=self._jobs[key],
                    draft=draft,
                )
            )
            return mutation

    def commit_decision(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
        draft: RegistryJoinChangeDraft,
        audit: RegistryChangeAuditRecord,
        *,
        expected_revision: int,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryJoinDraftMutation:
        with self._lock:
            replay = self._exact_operation(
                authoring,
                operation=operation,
                actor_id=actor_id,
                idempotency_digest=idempotency_digest,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None:
                if replay.draft is None:
                    raise _conflict()
                return RegistryJoinDraftMutation(
                    authoring=authoring,
                    draft=replay.draft,
                    replayed=True,
                )
            key = (authoring.workspace_id, authoring.id)
            current = self._drafts.get(key)
            if (
                self._authoring.get(key) != authoring
                or current is None
                or current.revision != expected_revision
                or draft.revision != expected_revision + 1
                or draft.decision is None
            ):
                raise _conflict()
            mutation = RegistryJoinDraftMutation(authoring=authoring, draft=draft)
            _require_audit_binding(audit, authoring.workspace_id, authoring.id)
            if (
                audit.resulting_fingerprint != draft.fingerprint
                or audit.decision_id != draft.decision.id
            ):
                raise _invalid_response()
            self._drafts[key] = draft
            self._audit[key].append(audit)
            self._operations[(authoring.workspace_id, idempotency_digest)] = (
                RegistryChangeOperationReplay(
                    operation=operation,
                    request_fingerprint=request_fingerprint,
                    actor_id=actor_id,
                    authoring=authoring,
                    job=self._jobs[key],
                    draft=draft,
                )
            )
            return mutation

    def commit_preparation(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
        draft: RegistryJoinChangeDraft,
        proposal: PreparedRegistryJoinProposal,
        audit: RegistryChangeAuditRecord,
        *,
        expected_revision: int,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryJoinPreparation:
        with self._lock:
            replay = self._exact_operation(
                authoring,
                operation=operation,
                actor_id=actor_id,
                idempotency_digest=idempotency_digest,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None:
                if replay.draft is None or replay.proposal is None:
                    raise _conflict()
                return RegistryJoinPreparation(
                    authoring=authoring,
                    draft=replay.draft,
                    proposal=replay.proposal,
                    replayed=True,
                )
            key = (authoring.workspace_id, authoring.id)
            current = self._drafts.get(key)
            if (
                self._authoring.get(key) != authoring
                or current is None
                or current.revision != expected_revision
                or current.status is not RegistryJoinChangeStatus.NEEDS_REVIEW
                or draft.revision != expected_revision
                or draft.status is not RegistryJoinChangeStatus.READY_FOR_PUBLICATION
                or proposal.draft_id != draft.id
                or proposal.draft_revision != expected_revision
                or (authoring.workspace_id, proposal.id) in self._proposals
            ):
                raise _conflict()
            preparation = RegistryJoinPreparation(
                authoring=authoring,
                draft=draft,
                proposal=proposal,
            )
            _require_audit_binding(audit, authoring.workspace_id, authoring.id)
            if audit.resulting_fingerprint != draft.fingerprint or audit.proposal_id != proposal.id:
                raise _invalid_response()
            self._drafts[key] = draft
            self._proposals[(authoring.workspace_id, proposal.id)] = proposal
            self._audit[key].append(audit)
            self._operations[(authoring.workspace_id, idempotency_digest)] = (
                RegistryChangeOperationReplay(
                    operation=operation,
                    request_fingerprint=request_fingerprint,
                    actor_id=actor_id,
                    authoring=authoring,
                    job=self._jobs[key],
                    draft=draft,
                    proposal=proposal,
                )
            )
            return preparation

    def list_audit(
        self,
        workspace_id: str,
        change_id: str,
        *,
        limit: int = MAX_REGISTRY_CHANGE_HISTORY + 1,
    ) -> tuple[RegistryChangeAuditRecord, ...]:
        if not 1 <= limit <= MAX_REGISTRY_CHANGE_HISTORY + 1:
            raise _unavailable()
        with self._lock:
            return tuple(self._audit.get((workspace_id, change_id), ())[-limit:])

    def _exact_operation(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
        *,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryChangeOperationReplay | None:
        replay = self._operations.get((authoring.workspace_id, idempotency_digest))
        if replay is None:
            return None
        if (
            replay.operation != operation
            or replay.actor_id != actor_id
            or replay.request_fingerprint != request_fingerprint
            or replay.authoring != authoring
        ):
            raise _conflict()
        return replay


def _require_audit_binding(
    audit: RegistryChangeAuditRecord,
    workspace_id: str,
    change_id: str,
) -> None:
    if audit.workspace_id != workspace_id or audit.change_id != change_id:
        raise _invalid_response()


def _conflict() -> RegistryChangeStoreError:
    return RegistryChangeStoreError(
        RegistryChangeStoreErrorCode.CONFLICT,
        "registry change store conflict",
    )


def _unavailable() -> RegistryChangeStoreError:
    return RegistryChangeStoreError(
        RegistryChangeStoreErrorCode.UNAVAILABLE,
        "registry change store unavailable",
    )


def _invalid_response() -> RegistryChangeStoreError:
    return RegistryChangeStoreError(
        RegistryChangeStoreErrorCode.INVALID_RESPONSE,
        "registry change store binding is invalid",
    )


__all__ = ["InMemoryRegistryChangeStore"]
