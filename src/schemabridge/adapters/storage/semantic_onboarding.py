"""Deterministic in-memory M33 store used by isolated tests and local composition only."""

from __future__ import annotations

from threading import RLock

from schemabridge.application.ports.semantic_onboarding import (
    SemanticOnboardingOperationReplay,
    SemanticOnboardingPortError,
    SemanticOnboardingPortErrorCode,
)
from schemabridge.domain.semantic_onboarding import (
    MAX_ONBOARDING_DECISIONS,
    PreparedSemanticOnboardingProposal,
    SemanticOnboardingAuditRecord,
    SemanticOnboardingDecision,
    SemanticOnboardingDraft,
    SemanticOnboardingDraftMutation,
    SemanticOnboardingPreparation,
)


class InMemorySemanticOnboardingStore:
    """Atomic workspace-scoped reference store; never composed for managed runtime."""

    def __init__(self) -> None:
        self._drafts: dict[tuple[str, str], SemanticOnboardingDraft] = {}
        self._decisions: dict[tuple[str, str], list[SemanticOnboardingDecision]] = {}
        self._proposals: dict[tuple[str, str], list[PreparedSemanticOnboardingProposal]] = {}
        self._audit: dict[tuple[str, str], list[SemanticOnboardingAuditRecord]] = {}
        self._operations: dict[tuple[str, str], SemanticOnboardingOperationReplay] = {}
        self._lock = RLock()

    def load_operation_replay(
        self,
        workspace_id: str,
        idempotency_digest: str,
    ) -> SemanticOnboardingOperationReplay | None:
        with self._lock:
            return self._operations.get((workspace_id, idempotency_digest))

    def create(
        self,
        draft: SemanticOnboardingDraft,
        audit: SemanticOnboardingAuditRecord,
        *,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> SemanticOnboardingDraftMutation:
        with self._lock:
            self._require_operation_absent(draft.workspace_id, idempotency_digest)
            key = (draft.workspace_id, draft.id)
            if key in self._drafts:
                raise _conflict()
            _require_audit_binding(audit, draft)
            self._drafts[key] = draft
            self._audit[key] = [audit]
            self._operations[(draft.workspace_id, idempotency_digest)] = (
                SemanticOnboardingOperationReplay(
                    operation=operation,
                    request_fingerprint=request_fingerprint,
                    actor_id=actor_id,
                    draft=draft,
                )
            )
            return SemanticOnboardingDraftMutation(draft=draft)

    def load(self, workspace_id: str, draft_id: str) -> SemanticOnboardingDraft | None:
        with self._lock:
            return self._drafts.get((workspace_id, draft_id))

    def list_for_workspace(
        self,
        workspace_id: str,
        *,
        owner_actor_id: str | None,
        limit: int,
    ) -> tuple[SemanticOnboardingDraft, ...]:
        if not 1 <= limit <= 50:
            raise _unavailable()
        with self._lock:
            matches = [
                draft
                for (candidate_workspace, _), draft in self._drafts.items()
                if candidate_workspace == workspace_id
                and (owner_actor_id is None or draft.owner_actor_id == owner_actor_id)
            ]
            return tuple(
                sorted(matches, key=lambda item: (item.updated_at, item.id), reverse=True)[:limit]
            )

    def commit_decision(
        self,
        draft: SemanticOnboardingDraft,
        decision: SemanticOnboardingDecision,
        audit: SemanticOnboardingAuditRecord,
        *,
        expected_revision: int,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> SemanticOnboardingDraftMutation:
        with self._lock:
            self._require_operation_absent(draft.workspace_id, idempotency_digest)
            key = (draft.workspace_id, draft.id)
            current = self._drafts.get(key)
            if (
                current is None
                or current.revision != expected_revision
                or draft.revision != expected_revision + 1
                or decision.workspace_id != draft.workspace_id
                or decision.draft_id != draft.id
                or decision.source_revision != expected_revision
                or decision.resulting_revision != draft.revision
            ):
                raise _conflict()
            if any(item.id == decision.id for item in self._decisions.get(key, ())):
                raise _conflict()
            _require_audit_binding(audit, draft)
            self._drafts[key] = draft
            self._decisions.setdefault(key, []).append(decision)
            self._audit.setdefault(key, []).append(audit)
            self._operations[(draft.workspace_id, idempotency_digest)] = (
                SemanticOnboardingOperationReplay(
                    operation=operation,
                    request_fingerprint=request_fingerprint,
                    actor_id=actor_id,
                    draft=draft,
                )
            )
            return SemanticOnboardingDraftMutation(draft=draft)

    def commit_preparation(
        self,
        draft: SemanticOnboardingDraft,
        proposal: PreparedSemanticOnboardingProposal,
        audit: SemanticOnboardingAuditRecord,
        *,
        expected_revision: int,
        operation: str,
        actor_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> SemanticOnboardingPreparation:
        with self._lock:
            self._require_operation_absent(draft.workspace_id, idempotency_digest)
            key = (draft.workspace_id, draft.id)
            current = self._drafts.get(key)
            if (
                current is None
                or current.revision != expected_revision
                or draft.revision != expected_revision + 1
                or proposal.workspace_id != draft.workspace_id
                or proposal.draft_id != draft.id
                or proposal.draft_revision != expected_revision
            ):
                raise _conflict()
            if any(item.id == proposal.id for item in self._proposals.get(key, ())):
                raise _conflict()
            _require_audit_binding(audit, draft)
            self._drafts[key] = draft
            self._proposals.setdefault(key, []).append(proposal)
            self._audit.setdefault(key, []).append(audit)
            self._operations[(draft.workspace_id, idempotency_digest)] = (
                SemanticOnboardingOperationReplay(
                    operation=operation,
                    request_fingerprint=request_fingerprint,
                    actor_id=actor_id,
                    draft=draft,
                    proposal=proposal,
                )
            )
            return SemanticOnboardingPreparation(draft=draft, proposal=proposal)

    def list_decisions(
        self,
        workspace_id: str,
        draft_id: str,
        *,
        limit: int = MAX_ONBOARDING_DECISIONS + 2,
    ) -> tuple[SemanticOnboardingDecision, ...]:
        _require_history_limit(limit)
        with self._lock:
            return tuple(self._decisions.get((workspace_id, draft_id), ())[-limit:])

    def list_proposals(
        self,
        workspace_id: str,
        draft_id: str,
        *,
        limit: int = MAX_ONBOARDING_DECISIONS + 2,
    ) -> tuple[PreparedSemanticOnboardingProposal, ...]:
        _require_history_limit(limit)
        with self._lock:
            return tuple(self._proposals.get((workspace_id, draft_id), ())[-limit:])

    def list_audit(
        self,
        workspace_id: str,
        draft_id: str,
        *,
        limit: int = MAX_ONBOARDING_DECISIONS + 2,
    ) -> tuple[SemanticOnboardingAuditRecord, ...]:
        _require_history_limit(limit)
        with self._lock:
            return tuple(self._audit.get((workspace_id, draft_id), ())[-limit:])

    def _require_operation_absent(self, workspace_id: str, digest: str) -> None:
        if (workspace_id, digest) in self._operations:
            raise _conflict()


def _require_audit_binding(
    audit: SemanticOnboardingAuditRecord,
    draft: SemanticOnboardingDraft,
) -> None:
    if (
        audit.workspace_id != draft.workspace_id
        or audit.draft_id != draft.id
        or audit.resulting_revision != draft.revision
        or audit.resulting_fingerprint != draft.fingerprint
    ):
        raise _invalid_response()


def _require_history_limit(limit: int) -> None:
    if not 1 <= limit <= MAX_ONBOARDING_DECISIONS + 2:
        raise _unavailable()


def _conflict() -> SemanticOnboardingPortError:
    return SemanticOnboardingPortError(
        SemanticOnboardingPortErrorCode.CONFLICT,
        "semantic onboarding store conflict",
    )


def _unavailable() -> SemanticOnboardingPortError:
    return SemanticOnboardingPortError(
        SemanticOnboardingPortErrorCode.UNAVAILABLE,
        "semantic onboarding store unavailable",
    )


def _invalid_response() -> SemanticOnboardingPortError:
    return SemanticOnboardingPortError(
        SemanticOnboardingPortErrorCode.INVALID_RESPONSE,
        "semantic onboarding store returned an invalid response",
    )


__all__ = ["InMemorySemanticOnboardingStore"]
