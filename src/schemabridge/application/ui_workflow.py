"""Application facade used by the Streamlit entrypoint."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Protocol

from schemabridge.application.authorization import (
    AuthorizationError,
)
from schemabridge.application.ports.authorization import WorkflowAuthorizationPort
from schemabridge.application.ports.publication_audit import PublicationAuditStoreError
from schemabridge.application.ports.workflow_access import (
    WorkflowAccessError,
    WorkflowAccessStorePort,
)
from schemabridge.application.ports.workflows import WorkflowClockPort, WorkflowError
from schemabridge.application.postgres_health import DatabaseConfigurationError
from schemabridge.application.ui_view_models import JudgeUiView
from schemabridge.application.workflow_orchestration import (
    AgentWorkflowOrchestrator,
    workflow_recovery_operation,
)
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    WorkflowAccessGrant,
    WorkflowPermission,
)
from schemabridge.domain.intents import IntentAlternativeId, UserLanguage
from schemabridge.domain.query_studio import ConfirmedQueryStudioRequest
from schemabridge.domain.workflows import (
    AgentWorkflowDraft,
    ExecutionWorkflowDecision,
    IntentWorkflowDecision,
    PublicationWorkflowDecision,
    RetryWorkflowDecision,
    StartWorkflowCommand,
    WorkflowDecisionAction,
    WorkflowDecisionKind,
    WorkflowOperation,
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


class JudgeUiViewFactoryPort(Protocol):
    """Build UI views without exposing concrete presentation composition."""

    def empty(self) -> JudgeUiView:
        """Return the authorized empty workspace view."""

    def from_draft(self, draft: AgentWorkflowDraft) -> JudgeUiView:
        """Return one authorized workflow view."""


class DeniedJudgeUiViewFactory:
    """Fail closed if a denied service shell ever reaches view construction."""

    def empty(self) -> JudgeUiView:
        raise _view_permission_denied()

    def from_draft(self, draft: AgentWorkflowDraft) -> JudgeUiView:
        del draft
        raise _view_permission_denied()


@dataclass(frozen=True, slots=True)
class UiCapabilities:
    """Reader-facing permissions computed by the application policy."""

    can_view: bool
    can_create: bool
    can_confirm: bool
    can_execute: bool
    can_publish: bool
    can_skip: bool
    can_retry: bool
    can_view_result: bool
    can_export: bool


@dataclass(frozen=True, slots=True)
class UiWorkflowSummary:
    """Minimal authorized workflow reference for the browser inbox."""

    workflow_id: str
    owned_by_current_principal: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class JudgeUiService:
    """Invoke existing typed workflow transitions and return view models only."""

    orchestrator_factory: Callable[[], AgentWorkflowOrchestrator]
    views: JudgeUiViewFactoryPort
    principal: AuthenticatedPrincipal
    access_store: WorkflowAccessStorePort
    authorization: WorkflowAuthorizationPort
    clock: WorkflowClockPort
    require_separate_publisher: bool = False
    max_live_publication_identity_age: timedelta = timedelta(minutes=15)

    def empty(self) -> JudgeUiView:
        self._require(WorkflowPermission.VIEW)
        return self.views.empty()

    def capabilities(self) -> UiCapabilities:
        """Expose effective policy decisions without duplicating role logic in the UI."""

        try:
            permissions = self.authorization.permissions_for(
                self.principal,
                at=self.clock.now(),
            )
        except AuthorizationError:
            permissions = frozenset()
        return UiCapabilities(
            can_view=WorkflowPermission.VIEW in permissions,
            can_create=WorkflowPermission.CREATE in permissions,
            can_confirm=WorkflowPermission.CONFIRM in permissions,
            can_execute=WorkflowPermission.EXECUTE in permissions,
            can_publish=WorkflowPermission.PUBLISH in permissions,
            can_skip=WorkflowPermission.SKIP in permissions,
            can_retry=bool(
                permissions & frozenset({WorkflowPermission.RETRY, WorkflowPermission.PUBLISH})
            ),
            can_view_result=WorkflowPermission.VIEW_RESULT in permissions,
            can_export=WorkflowPermission.EXPORT_RESULT in permissions,
        )

    def start_demo(self, workflow_id: str) -> JudgeUiView:
        return self.start_request(workflow_id, NORTH_STAR_TEXT)

    def available_workflows(self, *, limit: int = 50) -> tuple[UiWorkflowSummary, ...]:
        """Return only workflow references authorized for this principal and workspace."""

        now = self.clock.now()
        try:
            owner_filter = self.authorization.owner_filter_for(
                self.principal,
                WorkflowPermission.VIEW,
                at=now,
            )
            grants = self.access_store.list_for_workspace(
                self.principal.workspace_id,
                owner_principal_id=owner_filter,
                limit=limit,
            )
        except AuthorizationError as error:
            raise _safe_authorization_error(error) from error
        except (WorkflowAccessError, ValueError) as error:
            if isinstance(error, WorkflowAccessError):
                raise _safe_access_error(error) from error
            raise UiActionError(
                "workflow_access_request_invalid",
                "The workflow inbox request was rejected.",
                "Reload the application with its bounded default inbox.",
            ) from error
        return tuple(
            UiWorkflowSummary(
                workflow_id=grant.workflow_id,
                owned_by_current_principal=grant.owner_actor_id == self.principal.actor_id,
                created_at=grant.created_at,
            )
            for grant in grants
        )

    def start_request(self, workflow_id: str, text: str) -> JudgeUiView:
        now = self.clock.now()
        self._require(WorkflowPermission.CREATE, at=now)
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
        try:
            grant = self.access_store.load(
                self.principal.workspace_id,
                workflow_id,
                owner_principal_id=self.principal.actor_id,
            )
        except WorkflowAccessError as error:
            raise _safe_access_error(error) from error
        if grant is None:
            raise UiActionError(
                "workflow_access_store_failure",
                "The workflow was not durably bound to its authenticated owner.",
                "Do not retry this identifier; ask an operator to inspect the control plane.",
            )
        return self._view(draft)

    def start_confirmed_query_studio(
        self,
        workflow_id: str,
        confirmed: ConfirmedQueryStudioRequest,
    ) -> JudgeUiView:
        """Enter the durable workflow only after Query Studio exact confirmation."""

        now = self.clock.now()
        self._require(WorkflowPermission.CREATE, at=now)
        try:
            draft = self._orchestrator().start_confirmed(workflow_id, confirmed)
        except (WorkflowError, ValueError) as error:
            raise _safe_action_error(error) from error
        try:
            grant = self.access_store.load(
                self.principal.workspace_id,
                workflow_id,
                owner_principal_id=self.principal.actor_id,
            )
        except WorkflowAccessError as error:
            raise _safe_access_error(error) from error
        if grant is None:
            raise UiActionError(
                "workflow_access_store_failure",
                "The workflow was not durably bound to its authenticated owner.",
                "Do not retry this identifier; ask an operator to inspect the control plane.",
            )
        return self._view(draft)

    def inspect(self, workflow_id: str) -> JudgeUiView:
        self._require_workflow(workflow_id, WorkflowPermission.VIEW)
        try:
            draft = self._orchestrator().inspect(workflow_id)
        except (WorkflowError, ValueError) as error:
            raise _safe_action_error(error) from error
        return self._view(draft)

    def confirm_intent(
        self,
        workflow_id: str,
        alternative: IntentAlternativeId,
    ) -> JudgeUiView:
        self._require_workflow(workflow_id, WorkflowPermission.CONFIRM)
        orchestrator = self._orchestrator()
        try:
            current = orchestrator.inspect(workflow_id)
            if current.intent is None:
                raise UiActionError(
                    "interpretation_missing",
                    "No typed interpretation is available to confirm.",
                    "Load a scenario or submit a valid business request first.",
                )
            draft = orchestrator.decide_intent(
                workflow_id,
                IntentWorkflowDecision(
                    actor=self.principal.actor_id,
                    interpretation_fingerprint=current.intent.interpretation_fingerprint,
                    selected_alternative=alternative,
                ),
            )
        except UiActionError:
            raise
        except (WorkflowError, ValueError) as error:
            raise _safe_action_error(error) from error
        return self._view(draft)

    def approve_execution(self, workflow_id: str) -> JudgeUiView:
        self._require_workflow(workflow_id, WorkflowPermission.EXECUTE)
        orchestrator = self._orchestrator()
        try:
            current = orchestrator.inspect(workflow_id)
            if current.plan_fingerprint is None:
                raise UiActionError(
                    "validated_plan_missing",
                    "No validated governed plan is ready for execution.",
                    "Resolve and confirm the interpretation, mappings, and join contract first.",
                )
            draft = orchestrator.decide_execution(
                workflow_id,
                ExecutionWorkflowDecision(
                    actor=self.principal.actor_id,
                    plan_fingerprint=current.plan_fingerprint,
                    action=WorkflowDecisionAction.APPROVE,
                ),
            )
        except UiActionError:
            raise
        except (WorkflowError, ValueError) as error:
            raise _safe_action_error(error) from error
        return self._view(draft)

    def publish_context(self, workflow_id: str) -> JudgeUiView:
        self._require_workflow(workflow_id, WorkflowPermission.PUBLISH)
        self._require_fresh_live_publisher()
        orchestrator = self._orchestrator()
        try:
            current = orchestrator.inspect(workflow_id)
            if current.publication_proposal is None:
                raise UiActionError(
                    "publication_proposal_missing",
                    "No validated execution context is available to publish.",
                    "Complete a successful governed preview first.",
                )
            if self.require_separate_publisher and any(
                decision.kind is WorkflowDecisionKind.EXECUTION
                and decision.actor == self.principal.actor_id
                for decision in current.decisions
            ):
                raise UiActionError(
                    "separation_of_duties_required",
                    "Live publication requires a publisher other than the execution approver.",
                    "Ask another authenticated publisher in this workspace to review the proposal.",
                )
            draft = orchestrator.decide_publication(
                workflow_id,
                PublicationWorkflowDecision(
                    actor=self.principal.actor_id,
                    proposal_fingerprint=current.publication_proposal.fingerprint,
                    action=WorkflowDecisionAction.PUBLISH,
                    confirmation=WorkflowPublicationConfirmation.PUBLISH_EXECUTION_CONTEXT,
                ),
            )
        except UiActionError:
            raise
        except (WorkflowError, ValueError) as error:
            raise _safe_action_error(error) from error
        return self._view(draft)

    def skip_publication(self, workflow_id: str) -> JudgeUiView:
        self._require_workflow(workflow_id, WorkflowPermission.SKIP)
        orchestrator = self._orchestrator()
        try:
            current = orchestrator.inspect(workflow_id)
            if current.publication_proposal is None:
                raise UiActionError(
                    "publication_proposal_missing",
                    "No publication decision is currently required.",
                    "Complete a successful governed preview first.",
                )
            draft = orchestrator.decide_publication(
                workflow_id,
                PublicationWorkflowDecision(
                    actor=self.principal.actor_id,
                    proposal_fingerprint=current.publication_proposal.fingerprint,
                    action=WorkflowDecisionAction.SKIP,
                ),
            )
        except UiActionError:
            raise
        except (WorkflowError, ValueError) as error:
            raise _safe_action_error(error) from error
        return self._view(draft)

    def retry(self, workflow_id: str) -> JudgeUiView:
        grant = self._require_workflow(workflow_id, WorkflowPermission.VIEW)
        orchestrator = self._orchestrator()
        try:
            current = orchestrator.inspect(workflow_id)
            if current.failure is None:
                raise UiActionError(
                    "retry_not_available",
                    "This workflow has no retryable typed failure.",
                    "Continue from the current approval checkpoint instead.",
                )
            permission = (
                WorkflowPermission.PUBLISH
                if current.failure.operation is WorkflowOperation.CONTEXT_PUBLICATION
                else WorkflowPermission.RETRY
            )
            self._require_grant(grant, permission)
            if permission is WorkflowPermission.PUBLISH:
                self._require_fresh_live_publisher()
            if current.failure.operation is WorkflowOperation.CONTEXT_PUBLICATION and (
                current.publication_approval is None
                or current.publication_approval.actor != self.principal.actor_id
            ):
                raise UiActionError(
                    "publication_reauthorization_required",
                    "Only the authenticated publisher bound to this approval may retry it.",
                    "Create a new reviewed publication approval before changing publisher.",
                )
            draft = orchestrator.retry(
                workflow_id,
                RetryWorkflowDecision(
                    actor=self.principal.actor_id,
                    failure_fingerprint=current.failure.fingerprint,
                    operation=current.failure.operation,
                ),
            )
        except UiActionError:
            raise
        except (WorkflowError, ValueError) as error:
            raise _safe_action_error(error) from error
        return self._view(draft)

    def recover(self, workflow_id: str) -> JudgeUiView:
        """Recover interrupted state only after operation-specific authorization."""

        grant = self._require_workflow(workflow_id, WorkflowPermission.VIEW)
        orchestrator = self._orchestrator()
        try:
            current = orchestrator.inspect(workflow_id)
            operation = workflow_recovery_operation(current)
            if operation is None:
                raise UiActionError(
                    "recovery_not_available",
                    "This workflow has no interrupted transition to recover.",
                    "Continue from the current governed checkpoint.",
                )
            permission = (
                WorkflowPermission.PUBLISH
                if operation is WorkflowOperation.CONTEXT_PUBLICATION
                else WorkflowPermission.RETRY
            )
            self._require_grant(grant, permission)
            if permission is WorkflowPermission.PUBLISH:
                self._require_fresh_live_publisher()
            draft = orchestrator.recover_interrupted(
                workflow_id,
                expected_operation=operation,
            )
        except UiActionError:
            raise
        except (WorkflowError, ValueError) as error:
            raise _safe_action_error(error) from error
        return self._view(draft)

    def _require(
        self,
        permission: WorkflowPermission,
        *,
        at: datetime | None = None,
    ) -> None:
        resolved_at = self.clock.now() if at is None else at
        try:
            self.authorization.require(
                self.principal,
                permission,
                at=resolved_at,
            )
        except AuthorizationError as error:
            raise _safe_authorization_error(error) from error

    def _require_workflow(
        self,
        workflow_id: str,
        permission: WorkflowPermission,
    ) -> WorkflowAccessGrant:
        self._require(permission)
        try:
            grant = self.access_store.load(self.principal.workspace_id, workflow_id)
        except WorkflowAccessError as error:
            raise _safe_access_error(error) from error
        if grant is None:
            raise _workflow_access_denied()
        self._require_grant(grant, permission)
        return grant

    def _require_grant(
        self,
        grant: WorkflowAccessGrant,
        permission: WorkflowPermission,
    ) -> None:
        try:
            self.authorization.require_workflow(
                self.principal,
                grant,
                permission,
                at=self.clock.now(),
            )
        except AuthorizationError as error:
            raise _safe_authorization_error(error) from error

    def _orchestrator(self) -> AgentWorkflowOrchestrator:
        try:
            return self.orchestrator_factory()
        except (
            DatabaseConfigurationError,
            OSError,
            PublicationAuditStoreError,
            ValueError,
            WorkflowError,
        ) as error:
            raise UiActionError(
                "integration_configuration_unavailable",
                "The selected integration is not configured or reachable.",
                "Start the synthetic services, set DATABASE_URL, and verify the selected mode.",
            ) from error

    def _view(self, draft: AgentWorkflowDraft) -> JudgeUiView:
        view = self.views.from_draft(draft)
        try:
            permissions = self.authorization.permissions_for(
                self.principal,
                at=self.clock.now(),
            )
        except AuthorizationError:
            permissions = frozenset()
        if WorkflowPermission.VIEW_RESULT in permissions or view.query is None:
            return view
        return replace(view, query=replace(view.query, result=None))

    def _require_fresh_live_publisher(self) -> None:
        if not self.require_separate_publisher:
            return
        now = self.clock.now()
        if (
            self.max_live_publication_identity_age <= timedelta(0)
            or now - self.principal.authenticated_at > self.max_live_publication_identity_age
        ):
            raise UiActionError(
                "publication_reauthentication_required",
                "Live publication requires a recently issued authenticated identity.",
                "Sign out and authenticate again before reviewing the publication proposal.",
            )


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


def _safe_authorization_error(error: AuthorizationError) -> UiActionError:
    if error.code.value == "principal_not_current":
        return UiActionError(
            error.code.value,
            "The authenticated session is no longer current.",
            "Sign in again before continuing.",
        )
    if error.code.value == "workflow_access_denied":
        return _workflow_access_denied()
    return UiActionError(
        error.code.value,
        "This action is not available to the authenticated principal.",
        "Use an account with the required governed role.",
    )


def _safe_access_error(error: WorkflowAccessError) -> UiActionError:
    return UiActionError(
        error.code.value,
        "The durable workflow access boundary failed safely.",
        "Start a new workflow or ask an operator to inspect the control-plane store.",
    )


def _workflow_access_denied() -> UiActionError:
    return UiActionError(
        "workflow_access_denied",
        "The workflow is not available to the authenticated principal.",
        "Load a workflow available in the current workspace.",
    )


def _view_permission_denied() -> UiActionError:
    return UiActionError(
        "permission_denied",
        "This action is not available to the authenticated principal.",
        "Use an account with the required governed role.",
    )
