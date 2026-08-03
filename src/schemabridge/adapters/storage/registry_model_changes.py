"""Deterministic in-memory Phase-B store and immutable evidence readers."""

from __future__ import annotations

from datetime import datetime
from threading import RLock

from schemabridge.application.ports.registry_model_changes import (
    RegistryModelChangeOperationReplay,
    RegistryModelChangePortError,
    RegistryModelChangePortErrorCode,
    RegistryModelProfileOperationReplay,
)
from schemabridge.domain.registry_model_change_authoring import (
    MAX_REGISTRY_MODEL_CHANGE_HISTORY,
    RegistryModelChangeAuditRecord,
    RegistryModelChangeDraft,
    RegistryModelChangeMutation,
    RegistryModelChangeStatus,
    RegistryModelJoinProfileAuditRecord,
    RegistryModelJoinProfileAuthoringRequest,
    RegistryModelJoinProfileMutation,
    RegistryModelReplacementSourceEvidence,
)
from schemabridge.domain.registry_model_changes import (
    PreparedRegistryModelReplacementProposal,
    RegistryModelJoinProfileWitness,
)
from schemabridge.domain.semantic_change import SemanticChangeReport, SemanticImpactSet
from schemabridge.domain.semantic_profile_jobs import SemanticJoinProfileJob
from schemabridge.domain.semantic_registry import SemanticRegistryScope


class InMemoryRegistryModelChangeStore:
    def __init__(self) -> None:
        self._drafts: dict[tuple[str, str], RegistryModelChangeDraft] = {}
        self._source_drafts: dict[tuple[str, str], str] = {}
        self._proposals: dict[tuple[str, str], PreparedRegistryModelReplacementProposal] = {}
        self._audit: dict[tuple[str, str], list[RegistryModelChangeAuditRecord]] = {}
        self._operations: dict[tuple[str, str], RegistryModelChangeOperationReplay] = {}
        self._lock = RLock()

    def load_operation_replay(
        self,
        workspace_id: str,
        idempotency_digest: str,
    ) -> RegistryModelChangeOperationReplay | None:
        with self._lock:
            return self._operations.get((workspace_id, idempotency_digest))

    def create(
        self,
        draft: RegistryModelChangeDraft,
        audit: RegistryModelChangeAuditRecord,
        *,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryModelChangeMutation:
        with self._lock:
            replay = self._operations.get((draft.workspace_id, idempotency_digest))
            if replay is not None:
                _require_exact_replay(
                    replay,
                    operation=operation,
                    actor_id=actor_id,
                    request_fingerprint=request_fingerprint,
                )
                return RegistryModelChangeMutation(draft=replay.draft, replayed=True)
            key = (draft.workspace_id, draft.id)
            source_key = (draft.workspace_id, draft.source.proposal.id)
            if key in self._drafts or source_key in self._source_drafts or draft.revision != 1:
                raise _conflict()
            _require_audit(audit, draft)
            self._drafts[key] = draft
            self._source_drafts[source_key] = draft.id
            self._audit[key] = [audit]
            self._operations[(draft.workspace_id, idempotency_digest)] = (
                RegistryModelChangeOperationReplay(
                    operation=operation,
                    request_fingerprint=request_fingerprint,
                    actor_id=actor_id,
                    draft=draft,
                )
            )
            return RegistryModelChangeMutation(draft=draft)

    def load(self, workspace_id: str, change_id: str) -> RegistryModelChangeDraft | None:
        with self._lock:
            return self._drafts.get((workspace_id, change_id))

    def list_for_workspace(
        self,
        workspace_id: str,
        *,
        owner_actor_id: str | None,
        limit: int,
    ) -> tuple[RegistryModelChangeDraft, ...]:
        if not 1 <= limit <= 50:
            raise _unavailable()
        with self._lock:
            values = (
                item
                for (candidate_workspace, _), item in self._drafts.items()
                if candidate_workspace == workspace_id
                and (owner_actor_id is None or item.owner_actor_id == owner_actor_id)
            )
            return tuple(
                sorted(values, key=lambda item: (item.updated_at, item.id), reverse=True)[:limit]
            )

    def commit_decision(
        self,
        draft: RegistryModelChangeDraft,
        audit: RegistryModelChangeAuditRecord,
        *,
        expected_revision: int,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryModelChangeMutation:
        return self._commit(
            draft,
            audit,
            expected_revision=expected_revision,
            operation=operation,
            actor_id=actor_id,
            idempotency_digest=idempotency_digest,
            request_fingerprint=request_fingerprint,
            proposal=None,
        )

    def commit_preparation(
        self,
        draft: RegistryModelChangeDraft,
        proposal: PreparedRegistryModelReplacementProposal,
        audit: RegistryModelChangeAuditRecord,
        *,
        expected_revision: int,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryModelChangeMutation:
        return self._commit(
            draft,
            audit,
            expected_revision=expected_revision,
            operation=operation,
            actor_id=actor_id,
            idempotency_digest=idempotency_digest,
            request_fingerprint=request_fingerprint,
            proposal=proposal,
        )

    def list_audit(
        self,
        workspace_id: str,
        change_id: str,
        *,
        limit: int = MAX_REGISTRY_MODEL_CHANGE_HISTORY + 1,
    ) -> tuple[RegistryModelChangeAuditRecord, ...]:
        if not 1 <= limit <= MAX_REGISTRY_MODEL_CHANGE_HISTORY + 1:
            raise _unavailable()
        with self._lock:
            return tuple(self._audit.get((workspace_id, change_id), ())[-limit:])

    def _commit(
        self,
        draft: RegistryModelChangeDraft,
        audit: RegistryModelChangeAuditRecord,
        *,
        expected_revision: int,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
        proposal: PreparedRegistryModelReplacementProposal | None,
    ) -> RegistryModelChangeMutation:
        with self._lock:
            replay_key = (draft.workspace_id, idempotency_digest)
            replay = self._operations.get(replay_key)
            if replay is not None:
                _require_exact_replay(
                    replay,
                    operation=operation,
                    actor_id=actor_id,
                    request_fingerprint=request_fingerprint,
                )
                if (proposal is None) != (replay.proposal is None):
                    raise _conflict()
                return RegistryModelChangeMutation(
                    draft=replay.draft,
                    proposal=replay.proposal,
                    replayed=True,
                )
            key = (draft.workspace_id, draft.id)
            current = self._drafts.get(key)
            if (
                current is None
                or current.revision != expected_revision
                or draft.revision != expected_revision + 1
            ):
                raise _conflict()
            _require_audit(audit, draft)
            if proposal is not None:
                if (
                    draft.status is not RegistryModelChangeStatus.READY_FOR_PUBLICATION
                    or (draft.workspace_id, proposal.id) in self._proposals
                    or draft.prepared_proposal_fingerprint != proposal.fingerprint
                    or proposal.draft_id != draft.id
                    or proposal.draft_revision != expected_revision
                    or proposal.draft_fingerprint != current.fingerprint
                    or proposal.source_replacement_proposal_id != draft.source.proposal.id
                    or proposal.source_replacement_proposal_fingerprint
                    != draft.source.proposal.fingerprint
                ):
                    raise _conflict()
                self._proposals[(draft.workspace_id, proposal.id)] = proposal
            self._drafts[key] = draft
            self._audit[key].append(audit)
            self._operations[replay_key] = RegistryModelChangeOperationReplay(
                operation=operation,
                request_fingerprint=request_fingerprint,
                actor_id=actor_id,
                draft=draft,
                proposal=proposal,
            )
            return RegistryModelChangeMutation(draft=draft, proposal=proposal)


class InMemoryRegistryModelProfileStore:
    """Crash-window reference store for candidate-profile request/job/witness binding."""

    def __init__(self) -> None:
        self._requests: dict[tuple[str, str], RegistryModelJoinProfileAuthoringRequest] = {}
        self._jobs: dict[tuple[str, str], SemanticJoinProfileJob] = {}
        self._latest_witnesses: dict[
            tuple[str, str, str, str], RegistryModelJoinProfileWitness
        ] = {}
        self._latest_witness_order: dict[tuple[str, str, str, str], tuple[datetime, str]] = {}
        self._witness_history: dict[tuple[str, str], RegistryModelJoinProfileWitness] = {}
        self._audit: dict[tuple[str, str], list[RegistryModelJoinProfileAuditRecord]] = {}
        self._operations: dict[tuple[str, str], RegistryModelProfileOperationReplay] = {}
        self._lock = RLock()

    def load_operation_replay(
        self,
        workspace_id: str,
        idempotency_digest: str,
    ) -> RegistryModelProfileOperationReplay | None:
        with self._lock:
            return self._operations.get((workspace_id, idempotency_digest))

    def persist_request(
        self,
        authoring: RegistryModelJoinProfileAuthoringRequest,
        audit: RegistryModelJoinProfileAuditRecord,
        *,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryModelJoinProfileMutation:
        with self._lock:
            replay_key = (authoring.workspace_id, idempotency_digest)
            replay = self._operations.get(replay_key)
            if replay is not None:
                _require_profile_replay(
                    replay,
                    operation=operation,
                    actor_id=actor_id,
                    request_fingerprint=request_fingerprint,
                )
                return RegistryModelJoinProfileMutation(
                    authoring=replay.authoring,
                    job=replay.job,
                    witness=replay.witness,
                    replayed=True,
                )
            key = (authoring.workspace_id, authoring.id)
            if key in self._requests or any(
                item.request.scan_id == authoring.request.scan_id
                for item in self._requests.values()
                if item.workspace_id == authoring.workspace_id
            ):
                raise _conflict()
            _require_profile_audit(audit, authoring)
            self._requests[key] = authoring
            self._audit[key] = [audit]
            self._operations[replay_key] = RegistryModelProfileOperationReplay(
                operation=operation,
                request_fingerprint=request_fingerprint,
                actor_id=actor_id,
                authoring=authoring,
            )
            return RegistryModelJoinProfileMutation(authoring=authoring)

    def bind_job(
        self,
        authoring: RegistryModelJoinProfileAuthoringRequest,
        job: SemanticJoinProfileJob,
        audit: RegistryModelJoinProfileAuditRecord,
        *,
        idempotency_digest: str,
    ) -> RegistryModelJoinProfileMutation:
        with self._lock:
            key = (authoring.workspace_id, authoring.id)
            replay_key = (authoring.workspace_id, idempotency_digest)
            replay = self._operations.get(replay_key)
            if self._requests.get(key) != authoring or replay is None:
                raise _conflict()
            existing = self._jobs.get(key)
            if existing is not None:
                if existing != job or replay.job != job:
                    raise _conflict()
                return RegistryModelJoinProfileMutation(
                    authoring=authoring,
                    job=job,
                    replayed=True,
                )
            _require_profile_audit(audit, authoring)
            self._jobs[key] = job
            self._audit[key].append(audit)
            self._operations[replay_key] = RegistryModelProfileOperationReplay(
                operation=replay.operation,
                request_fingerprint=replay.request_fingerprint,
                actor_id=replay.actor_id,
                authoring=authoring,
                job=job,
            )
            return RegistryModelJoinProfileMutation(authoring=authoring, job=job)

    def load_request(
        self,
        workspace_id: str,
        request_id: str,
    ) -> RegistryModelJoinProfileAuthoringRequest | None:
        with self._lock:
            return self._requests.get((workspace_id, request_id))

    def record_witness(
        self,
        authoring: RegistryModelJoinProfileAuthoringRequest,
        job: SemanticJoinProfileJob,
        witness: RegistryModelJoinProfileWitness,
        audit: RegistryModelJoinProfileAuditRecord,
        *,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> RegistryModelJoinProfileMutation:
        with self._lock:
            replay_key = (authoring.workspace_id, idempotency_digest)
            replay = self._operations.get(replay_key)
            if replay is not None:
                _require_profile_replay(
                    replay,
                    operation=operation,
                    actor_id=actor_id,
                    request_fingerprint=request_fingerprint,
                )
                if replay.job is None or replay.witness is None:
                    raise _conflict()
                return RegistryModelJoinProfileMutation(
                    authoring=replay.authoring,
                    job=replay.job,
                    witness=replay.witness,
                    replayed=True,
                )
            key = (authoring.workspace_id, authoring.id)
            bound_job = self._jobs.get(key)
            if (
                self._requests.get(key) != authoring
                or bound_job is None
                or not _same_profile_job_identity(bound_job, job)
            ):
                raise _conflict()
            witness_key = (
                authoring.workspace_id,
                authoring.request.change_id,
                authoring.request.replacement_source.proposal.id,
                authoring.request.incident_join_id,
            )
            request_witness_key = (authoring.workspace_id, authoring.id)
            existing_request = self._witness_history.get(request_witness_key)
            current = self._latest_witnesses.get(witness_key)
            order = (authoring.request.requested_at, authoring.id)
            current_order = self._latest_witness_order.get(witness_key)
            if existing_request is not None and existing_request != witness:
                raise _conflict()
            if current is not None and witness.fingerprint == current.fingerprint:
                raise _conflict()
            _require_profile_audit(audit, authoring)
            self._witness_history[request_witness_key] = witness
            if current_order is None or order > current_order:
                self._latest_witnesses[witness_key] = witness
                self._latest_witness_order[witness_key] = order
            self._audit[key].append(audit)
            self._operations[replay_key] = RegistryModelProfileOperationReplay(
                operation=operation,
                request_fingerprint=request_fingerprint,
                actor_id=actor_id,
                authoring=authoring,
                job=job,
                witness=witness,
            )
            return RegistryModelJoinProfileMutation(
                authoring=authoring,
                job=job,
                witness=witness,
            )

    def load(
        self,
        workspace_id: str,
        change_id: str,
        replacement_proposal_id: str,
        join_id: str,
    ) -> RegistryModelJoinProfileWitness | None:
        with self._lock:
            return self._latest_witnesses.get(
                (workspace_id, change_id, replacement_proposal_id, join_id)
            )


class InMemoryRegistryModelReplacementEvidence:
    def __init__(self) -> None:
        self.sources: dict[tuple[str, str], RegistryModelReplacementSourceEvidence] = {}
        self.remediations: dict[
            tuple[str, str, str, str], tuple[SemanticChangeReport, SemanticImpactSet]
        ] = {}
        self.witnesses: dict[tuple[str, str, str, str], RegistryModelJoinProfileWitness] = {}

    def load(
        self,
        workspace_id: str | SemanticRegistryScope,
        proposal_or_report_id: str,
        replacement_proposal_id: str | None = None,
        join_id: str | None = None,
    ) -> (
        RegistryModelReplacementSourceEvidence
        | tuple[SemanticChangeReport, SemanticImpactSet]
        | RegistryModelJoinProfileWitness
        | None
    ):
        if isinstance(workspace_id, SemanticRegistryScope):
            scope = workspace_id
            return self.remediations.get(
                (
                    scope.workspace_id,
                    scope.catalog_scope,
                    scope.registry_id,
                    proposal_or_report_id,
                )
            )
        if replacement_proposal_id is not None and join_id is not None:
            return self.witnesses.get(
                (workspace_id, proposal_or_report_id, replacement_proposal_id, join_id)
            )
        return self.sources.get((workspace_id, proposal_or_report_id))

    def load_current(
        self,
        scope: SemanticRegistryScope,
        report_id: str,
        expected_context: object,
    ) -> tuple[SemanticChangeReport, SemanticImpactSet] | None:
        value = self.remediations.get(
            (scope.workspace_id, scope.catalog_scope, scope.registry_id, report_id)
        )
        if value is None or value[0].context != expected_context:
            return None
        return value


def _require_exact_replay(
    replay: RegistryModelChangeOperationReplay,
    *,
    operation: str,
    actor_id: str,
    request_fingerprint: str,
) -> None:
    if (
        replay.operation != operation
        or replay.actor_id != actor_id
        or replay.request_fingerprint != request_fingerprint
    ):
        raise _conflict()


def _require_profile_replay(
    replay: RegistryModelProfileOperationReplay,
    *,
    operation: str,
    actor_id: str,
    request_fingerprint: str,
) -> None:
    if (
        replay.operation != operation
        or replay.actor_id != actor_id
        or replay.request_fingerprint != request_fingerprint
    ):
        raise _conflict()


def _require_profile_audit(
    audit: RegistryModelJoinProfileAuditRecord,
    authoring: RegistryModelJoinProfileAuthoringRequest,
) -> None:
    if (
        audit.workspace_id != authoring.workspace_id
        or audit.request_id != authoring.id
        or audit.request_fingerprint != authoring.fingerprint
    ):
        raise RegistryModelChangePortError(
            RegistryModelChangePortErrorCode.INVALID_RESPONSE,
            "registry model profile audit is invalid",
        )


def _same_profile_job_identity(
    bound: SemanticJoinProfileJob,
    completed: SemanticJoinProfileJob,
) -> bool:
    return (
        bound.job_id == completed.job_id
        and bound.workspace_id == completed.workspace_id
        and bound.scan_id == completed.scan_id
        and bound.bound_proposal == completed.bound_proposal
        and bound.execution_target == completed.execution_target
        and bound.requested_at == completed.requested_at
    )


def _require_audit(
    audit: RegistryModelChangeAuditRecord,
    draft: RegistryModelChangeDraft,
) -> None:
    if (
        audit.workspace_id != draft.workspace_id
        or audit.change_id != draft.id
        or audit.resulting_revision != draft.revision
        or audit.resulting_fingerprint != draft.fingerprint
    ):
        raise RegistryModelChangePortError(
            RegistryModelChangePortErrorCode.INVALID_RESPONSE,
            "registry model change audit is invalid",
        )


def _conflict() -> RegistryModelChangePortError:
    return RegistryModelChangePortError(
        RegistryModelChangePortErrorCode.CONFLICT,
        "registry model change store conflict",
    )


def _unavailable() -> RegistryModelChangePortError:
    return RegistryModelChangePortError(
        RegistryModelChangePortErrorCode.UNAVAILABLE,
        "registry model change store unavailable",
    )


__all__ = [
    "InMemoryRegistryModelChangeStore",
    "InMemoryRegistryModelProfileStore",
    "InMemoryRegistryModelReplacementEvidence",
]
