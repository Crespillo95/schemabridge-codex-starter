"""Fail-closed publication boundary for the read-only execution worker."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from schemabridge.application.ports.workflows import (
    WorkflowDraftStorePort,
    WorkflowError,
    WorkflowErrorCode,
)
from schemabridge.domain.workflows import (
    AgentWorkflowDraft,
    ExecutionWorkflowDecision,
    RetryWorkflowDecision,
    WorkflowOperation,
    WorkflowPublicationApproval,
    WorkflowPublicationProposal,
    WorkflowPublicationResult,
)


@dataclass(frozen=True, slots=True)
class ReadOnlyWorkflowInspector:
    """Expose workflow inspection to the API without a mutation-capable orchestrator."""

    store: WorkflowDraftStorePort

    def inspect(self, workflow_id: str) -> AgentWorkflowDraft:
        draft = self.store.load(workflow_id)
        if draft is None:
            raise WorkflowError(WorkflowErrorCode.NOT_FOUND, "workflow draft was not found")
        return draft

    def decide_execution(
        self,
        workflow_id: str,
        decision: ExecutionWorkflowDecision,
        *,
        should_continue: Callable[[], bool] | None = None,
    ) -> AgentWorkflowDraft:
        del workflow_id, decision, should_continue
        raise WorkflowError(
            WorkflowErrorCode.INVALID_TRANSITION,
            "workflow mutation is unavailable at the read-only API boundary",
        )

    def recover_interrupted(
        self,
        workflow_id: str,
        *,
        expected_operation: WorkflowOperation,
    ) -> AgentWorkflowDraft:
        del workflow_id, expected_operation
        raise WorkflowError(
            WorkflowErrorCode.INVALID_TRANSITION,
            "workflow recovery is unavailable at the read-only API boundary",
        )

    def retry(
        self,
        workflow_id: str,
        decision: RetryWorkflowDecision,
        *,
        reserved_execution: ExecutionWorkflowDecision | None = None,
        should_continue: Callable[[], bool] | None = None,
    ) -> AgentWorkflowDraft:
        del workflow_id, decision, reserved_execution, should_continue
        raise WorkflowError(
            WorkflowErrorCode.INVALID_TRANSITION,
            "workflow retry is unavailable at the read-only API boundary",
        )


class DisabledWorkflowPublisher:
    """Prevent a query worker from crossing the DataHub write boundary."""

    def publish(
        self,
        proposal: WorkflowPublicationProposal,
        approval: WorkflowPublicationApproval,
    ) -> WorkflowPublicationResult:
        del proposal, approval
        raise WorkflowError(
            WorkflowErrorCode.PUBLICATION_FAILED,
            "publication is unavailable in the read-only execution worker",
        )
