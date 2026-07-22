"""Application facade used by the Streamlit entrypoint."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from schemabridge.application.ports.workflows import WorkflowError
from schemabridge.application.postgres_health import DatabaseConfigurationError
from schemabridge.application.ui_view_models import JudgeUiView, JudgeUiViewFactory
from schemabridge.application.workflow_orchestration import AgentWorkflowOrchestrator
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.intents import IntentAlternativeId, UserLanguage
from schemabridge.domain.workflows import (
    ExecutionWorkflowDecision,
    IntentWorkflowDecision,
    PublicationWorkflowDecision,
    RetryWorkflowDecision,
    StartWorkflowCommand,
    WorkflowDecisionAction,
    WorkflowPublicationConfirmation,
)

NORTH_STAR_TEXT = (
    "Agrupa por fecha de registro todos los clientes que sean segundo titular de una cuenta."
)


class UiActionError(RuntimeError):
    """A safe browser-facing failure with a stable code and next action."""

    def __init__(self, code: str, message: str, corrective_action: str) -> None:
        self.code = code
        self.corrective_action = corrective_action
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class JudgeUiService:
    """Invoke existing typed workflow transitions and return view models only."""

    orchestrator_factory: Callable[[], AgentWorkflowOrchestrator]
    views: JudgeUiViewFactory

    def empty(self) -> JudgeUiView:
        return self.views.empty()

    def start_demo(self, workflow_id: str) -> JudgeUiView:
        return self.start_request(workflow_id, NORTH_STAR_TEXT)

    def start_request(self, workflow_id: str, text: str) -> JudgeUiView:
        try:
            draft = self._orchestrator().start(
                StartWorkflowCommand(
                    id=workflow_id,
                    text=text,
                    language=UserLanguage.SPANISH,
                    datasets=(
                        PhysicalDatasetRef("crm.customers"),
                        PhysicalDatasetRef("bank.account_holders"),
                    ),
                )
            )
        except (WorkflowError, ValueError) as error:
            raise _safe_action_error(error) from error
        return self.views.from_draft(draft)

    def inspect(self, workflow_id: str) -> JudgeUiView:
        try:
            draft = self._orchestrator().resume(workflow_id)
        except (WorkflowError, ValueError) as error:
            raise _safe_action_error(error) from error
        return self.views.from_draft(draft)

    def confirm_intent(
        self,
        workflow_id: str,
        actor: str,
        alternative: IntentAlternativeId,
    ) -> JudgeUiView:
        orchestrator = self._orchestrator()
        try:
            current = orchestrator.resume(workflow_id)
            if current.intent is None:
                raise UiActionError(
                    "interpretation_missing",
                    "No typed interpretation is available to confirm.",
                    "Load a scenario or submit a valid business request first.",
                )
            draft = orchestrator.decide_intent(
                workflow_id,
                IntentWorkflowDecision(
                    actor=actor,
                    interpretation_fingerprint=current.intent.interpretation_fingerprint,
                    selected_alternative=alternative,
                ),
            )
        except UiActionError:
            raise
        except (WorkflowError, ValueError) as error:
            raise _safe_action_error(error) from error
        return self.views.from_draft(draft)

    def approve_execution(self, workflow_id: str, actor: str) -> JudgeUiView:
        orchestrator = self._orchestrator()
        try:
            current = orchestrator.resume(workflow_id)
            if current.plan_fingerprint is None:
                raise UiActionError(
                    "validated_plan_missing",
                    "No validated governed plan is ready for execution.",
                    "Resolve and confirm the interpretation, mappings, and join contract first.",
                )
            draft = orchestrator.decide_execution(
                workflow_id,
                ExecutionWorkflowDecision(
                    actor=actor,
                    plan_fingerprint=current.plan_fingerprint,
                    action=WorkflowDecisionAction.APPROVE,
                ),
            )
        except UiActionError:
            raise
        except (WorkflowError, ValueError) as error:
            raise _safe_action_error(error) from error
        return self.views.from_draft(draft)

    def publish_context(self, workflow_id: str, actor: str) -> JudgeUiView:
        orchestrator = self._orchestrator()
        try:
            current = orchestrator.resume(workflow_id)
            if current.publication_proposal is None:
                raise UiActionError(
                    "publication_proposal_missing",
                    "No validated execution context is available to publish.",
                    "Complete a successful governed preview first.",
                )
            draft = orchestrator.decide_publication(
                workflow_id,
                PublicationWorkflowDecision(
                    actor=actor,
                    proposal_fingerprint=current.publication_proposal.fingerprint,
                    action=WorkflowDecisionAction.PUBLISH,
                    confirmation=WorkflowPublicationConfirmation.PUBLISH_EXECUTION_CONTEXT,
                ),
            )
        except UiActionError:
            raise
        except (WorkflowError, ValueError) as error:
            raise _safe_action_error(error) from error
        return self.views.from_draft(draft)

    def skip_publication(self, workflow_id: str, actor: str) -> JudgeUiView:
        orchestrator = self._orchestrator()
        try:
            current = orchestrator.resume(workflow_id)
            if current.publication_proposal is None:
                raise UiActionError(
                    "publication_proposal_missing",
                    "No publication decision is currently required.",
                    "Complete a successful governed preview first.",
                )
            draft = orchestrator.decide_publication(
                workflow_id,
                PublicationWorkflowDecision(
                    actor=actor,
                    proposal_fingerprint=current.publication_proposal.fingerprint,
                    action=WorkflowDecisionAction.SKIP,
                ),
            )
        except UiActionError:
            raise
        except (WorkflowError, ValueError) as error:
            raise _safe_action_error(error) from error
        return self.views.from_draft(draft)

    def retry(self, workflow_id: str, actor: str) -> JudgeUiView:
        orchestrator = self._orchestrator()
        try:
            current = orchestrator.resume(workflow_id)
            if current.failure is None:
                raise UiActionError(
                    "retry_not_available",
                    "This workflow has no retryable typed failure.",
                    "Continue from the current approval checkpoint instead.",
                )
            draft = orchestrator.retry(
                workflow_id,
                RetryWorkflowDecision(
                    actor=actor,
                    failure_fingerprint=current.failure.fingerprint,
                    operation=current.failure.operation,
                ),
            )
        except UiActionError:
            raise
        except (WorkflowError, ValueError) as error:
            raise _safe_action_error(error) from error
        return self.views.from_draft(draft)

    def _orchestrator(self) -> AgentWorkflowOrchestrator:
        try:
            return self.orchestrator_factory()
        except (DatabaseConfigurationError, OSError, ValueError) as error:
            raise UiActionError(
                "integration_configuration_unavailable",
                "The selected integration is not configured or reachable.",
                "Start the synthetic services, set DATABASE_URL, and verify the selected mode.",
            ) from error


def _safe_action_error(error: Exception) -> UiActionError:
    code = getattr(error, "code", "ui_action_failed")
    code_value = code.value if hasattr(code, "value") else str(code)
    messages = {
        "not_found": "The durable workflow was not found.",
        "invalid_transition": "That action is not permitted at the current governed stage.",
        "decision_mismatch": "The workflow changed; review the latest version before deciding.",
        "revision_conflict": "Another process updated this workflow.",
    }
    return UiActionError(
        code_value,
        messages.get(code_value, "The action failed safely at a typed application boundary."),
        "Reload the workflow and follow the displayed checkpoint or corrective action.",
    )
