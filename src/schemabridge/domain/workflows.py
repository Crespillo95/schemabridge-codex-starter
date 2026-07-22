"""Pure typed contracts for the observable, resumable agent workflow."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import TypeAlias

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.intents import (
    IntentAlternativeId,
    IntentAmbiguityKind,
    IntentVocabulary,
    UserLanguage,
)
from schemabridge.domain.recipes import RecipeReuseAssessment
from schemabridge.domain.request_context import ValidatedAnalyticalRequest
from schemabridge.domain.requests import AnalyticalRequest
from schemabridge.domain.resolution import ResolvedSemanticPlan
from schemabridge.domain.validation import ValidationFinding

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TRACE_KEY = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_FORBIDDEN_TRACE_KEYS = frozenset(
    {"chain_of_thought", "password", "prompt", "reasoning", "secret", "token"}
)


class WorkflowStage(StrEnum):
    CREATED = "created"
    CONTEXT_RETRIEVAL = "context_retrieval"
    INTENT_RESOLUTION = "intent_resolution"
    DECISION_REQUIRED = "decision_required"
    SEMANTIC_RESOLUTION = "semantic_resolution"
    PLAN_READY = "plan_ready"
    VALIDATED = "validated"
    EXECUTION = "execution"
    EXECUTED = "executed"
    PUBLICATION_PROPOSED = "publication_proposed"
    PUBLICATION_COMPLETED = "publication_completed"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkflowCheckpointKind(StrEnum):
    INTENT_CONFIRMATION = "intent_confirmation"
    EXECUTION_APPROVAL = "execution_approval"
    PUBLICATION_DECISION = "publication_decision"
    RETRY = "retry"


class WorkflowOperation(StrEnum):
    CATALOG_CONTEXT_READ = "catalog_context_read"
    INTENT_RESOLUTION = "intent_resolution"
    HUMAN_DECISION = "human_decision"
    SEMANTIC_RESOLUTION = "semantic_resolution"
    SQL_VALIDATION = "sql_validation"
    QUERY_RECIPE_LOOKUP = "query_recipe_lookup"
    PREVIEW_EXECUTION = "preview_execution"
    REJECTION_INSPECTION = "rejection_inspection"
    CONTEXT_PUBLICATION = "context_publication"


class WorkflowTraceStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class WorkflowDecisionKind(StrEnum):
    INTENT = "intent"
    EXECUTION = "execution"
    PUBLICATION = "publication"
    RETRY = "retry"


class WorkflowDecisionAction(StrEnum):
    CONFIRM = "confirm"
    APPROVE = "approve"
    DECLINE = "decline"
    PUBLISH = "publish"
    SKIP = "skip"
    RETRY = "retry"


class WorkflowPublicationConfirmation(StrEnum):
    PUBLISH_EXECUTION_CONTEXT = "PUBLISH EXECUTION CONTEXT"


class WorkflowPublicationStatus(StrEnum):
    CREATED = "created"
    ALREADY_CURRENT = "already_current"


WorkflowScalar: TypeAlias = str | int | float | bool | None


class StartWorkflowCommand(FrozenDomainModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    text: str = Field(min_length=1, max_length=2_000)
    language: UserLanguage
    datasets: tuple[PhysicalDatasetRef, ...] = Field(min_length=1, max_length=3)

    @field_validator("text")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("workflow business request must not be blank")
        return value

    @field_validator("datasets")
    @classmethod
    def datasets_must_be_unique(
        cls, values: tuple[PhysicalDatasetRef, ...]
    ) -> tuple[PhysicalDatasetRef, ...]:
        if len(values) != len({value.root for value in values}):
            raise ValueError("workflow context datasets must be unique")
        return values


class WorkflowCatalogAsset(FrozenDomainModel):
    dataset: PhysicalDatasetRef
    urn: str = Field(min_length=1, max_length=500)
    field_count: int = Field(ge=0, le=1_000)
    partial: bool = False
    schema_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class WorkflowIntentAlternative(FrozenDomainModel):
    id: IntentAlternativeId
    label: str = Field(min_length=1, max_length=160)
    rationale: str = Field(min_length=1, max_length=500)
    resolves: tuple[IntentAmbiguityKind, ...]
    request: AnalyticalRequest | None


class WorkflowIntentSnapshot(FrozenDomainModel):
    language: UserLanguage
    adapter: str = Field(min_length=1, max_length=100)
    vocabulary: IntentVocabulary
    interpretation_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposed_request: AnalyticalRequest | None
    ambiguities: tuple[IntentAmbiguityKind, ...]
    alternatives: tuple[WorkflowIntentAlternative, ...] = ()
    findings: tuple[ValidationFinding, ...]


class WorkflowCheckpoint(FrozenDomainModel):
    kind: WorkflowCheckpointKind
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=300)


class WorkflowTraceFact(FrozenDomainModel):
    key: str
    value: str = Field(min_length=1, max_length=200)

    @field_validator("key")
    @classmethod
    def key_must_be_safe(cls, value: str) -> str:
        if not _SAFE_TRACE_KEY.fullmatch(value) or value in _FORBIDDEN_TRACE_KEYS:
            raise ValueError("workflow trace fact key is not allowlisted")
        return value


class WorkflowTraceEvent(FrozenDomainModel):
    sequence: int = Field(ge=1)
    stage: WorkflowStage
    operation: WorkflowOperation
    status: WorkflowTraceStatus
    occurred_at: datetime
    input_refs: tuple[str, ...] = Field(default=(), max_length=6)
    facts: tuple[WorkflowTraceFact, ...] = Field(default=(), max_length=8)
    duration_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def timing_matches_status(self) -> WorkflowTraceEvent:
        if self.status is WorkflowTraceStatus.STARTED and self.duration_ms is not None:
            raise ValueError("started workflow trace events cannot claim a duration")
        if self.status is not WorkflowTraceStatus.STARTED and self.duration_ms is None:
            raise ValueError("completed workflow trace events require a duration")
        return self


class WorkflowDecisionRecord(FrozenDomainModel):
    kind: WorkflowDecisionKind
    action: WorkflowDecisionAction
    actor: str = Field(min_length=1, max_length=120)
    decided_at: datetime
    bound_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class IntentWorkflowDecision(FrozenDomainModel):
    actor: str = Field(min_length=1, max_length=120)
    interpretation_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_alternative: IntentAlternativeId


class ExecutionWorkflowDecision(FrozenDomainModel):
    actor: str = Field(min_length=1, max_length=120)
    plan_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: WorkflowDecisionAction

    @field_validator("action")
    @classmethod
    def action_must_be_execution_decision(
        cls, value: WorkflowDecisionAction
    ) -> WorkflowDecisionAction:
        if value not in {WorkflowDecisionAction.APPROVE, WorkflowDecisionAction.DECLINE}:
            raise ValueError("execution decision must approve or decline")
        return value


class PublicationWorkflowDecision(FrozenDomainModel):
    actor: str = Field(min_length=1, max_length=120)
    proposal_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: WorkflowDecisionAction
    confirmation: WorkflowPublicationConfirmation | None = None

    @model_validator(mode="after")
    def publication_action_requires_exact_confirmation(self) -> PublicationWorkflowDecision:
        if self.action not in {
            WorkflowDecisionAction.PUBLISH,
            WorkflowDecisionAction.SKIP,
        }:
            raise ValueError("publication decision must publish or skip")
        if self.action is WorkflowDecisionAction.PUBLISH and self.confirmation is None:
            raise ValueError("publication requires the exact typed confirmation")
        if self.action is WorkflowDecisionAction.SKIP and self.confirmation is not None:
            raise ValueError("skipped publication cannot carry approval confirmation")
        return self


class RetryWorkflowDecision(FrozenDomainModel):
    actor: str = Field(min_length=1, max_length=120)
    failure_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation: WorkflowOperation
    action: WorkflowDecisionAction = WorkflowDecisionAction.RETRY

    @field_validator("action")
    @classmethod
    def action_must_be_retry(cls, value: WorkflowDecisionAction) -> WorkflowDecisionAction:
        if value is not WorkflowDecisionAction.RETRY:
            raise ValueError("retry decision action must be retry")
        return value


class WorkflowFailure(FrozenDomainModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    operation: WorkflowOperation
    retryable: bool
    attempt: int = Field(ge=1)
    occurred_at: datetime
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def create(
        cls,
        *,
        code: str,
        operation: WorkflowOperation,
        retryable: bool,
        attempt: int,
        occurred_at: datetime,
    ) -> WorkflowFailure:
        payload = {
            "code": code,
            "operation": operation.value,
            "retryable": retryable,
            "attempt": attempt,
            "occurred_at": occurred_at.isoformat(),
        }
        return cls(
            **payload,
            fingerprint=_fingerprint(payload),
        )


class WorkflowExecutionRecord(FrozenDomainModel):
    plan_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    query_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    columns: tuple[str, ...] = Field(max_length=50)
    rows: tuple[tuple[WorkflowScalar, ...], ...] = Field(max_length=10_000)
    database_user: str = Field(min_length=1, max_length=120)
    transaction_read_only: bool
    statement_timeout_ms: int = Field(ge=1)
    truncated: bool
    preview_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    rejection_codes: tuple[str, ...] = ()
    rejected_count: int = Field(default=0, ge=0)
    rejection_complete: bool = False

    @model_validator(mode="after")
    def rows_match_columns_and_read_only(self) -> WorkflowExecutionRecord:
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("workflow preview rows must match the column count")
        if not self.transaction_read_only:
            raise ValueError("workflow execution record must prove a read-only transaction")
        if self.rejection_complete and self.rejected_count != len(self.rejection_codes):
            raise ValueError("workflow rejection count must match the recorded codes")
        return self


class WorkflowPublicationProposal(FrozenDomainModel):
    workflow_id: str
    plan_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def create(
        cls,
        *,
        workflow_id: str,
        plan_fingerprint: str,
        execution_fingerprint: str,
        request_fingerprint: str,
    ) -> WorkflowPublicationProposal:
        payload = {
            "workflow_id": workflow_id,
            "plan_fingerprint": plan_fingerprint,
            "execution_fingerprint": execution_fingerprint,
            "request_fingerprint": request_fingerprint,
        }
        idempotency_key = _fingerprint(payload)
        return cls(
            **payload,
            idempotency_key=idempotency_key,
            fingerprint=_fingerprint({**payload, "idempotency_key": idempotency_key}),
        )


class WorkflowPublicationApproval(FrozenDomainModel):
    workflow_id: str
    proposal_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    actor: str = Field(min_length=1, max_length=120)
    approved_at: datetime
    confirmation: WorkflowPublicationConfirmation


class WorkflowPublicationResult(FrozenDomainModel):
    status: WorkflowPublicationStatus
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_ref: str = Field(min_length=1, max_length=500)
    published_at: datetime


class AgentWorkflowDraft(FrozenDomainModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    revision: int = Field(ge=1)
    stage: WorkflowStage
    text: str = Field(min_length=1, max_length=2_000)
    language: UserLanguage
    requested_datasets: tuple[PhysicalDatasetRef, ...] = Field(min_length=1, max_length=3)
    context_source: str | None = Field(default=None, max_length=120)
    context_assets: tuple[WorkflowCatalogAsset, ...] = ()
    intent: WorkflowIntentSnapshot | None = None
    validated_request: ValidatedAnalyticalRequest | None = None
    resolved_plan: ResolvedSemanticPlan | None = None
    plan_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    query_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    recipe_reuse: RecipeReuseAssessment | None = None
    execution: WorkflowExecutionRecord | None = None
    publication_proposal: WorkflowPublicationProposal | None = None
    publication_approval: WorkflowPublicationApproval | None = None
    publication_result: WorkflowPublicationResult | None = None
    checkpoint: WorkflowCheckpoint | None = None
    failure: WorkflowFailure | None = None
    decisions: tuple[WorkflowDecisionRecord, ...] = ()
    trace: tuple[WorkflowTraceEvent, ...] = ()
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def state_is_internally_consistent(self) -> AgentWorkflowDraft:
        if self.trace and tuple(event.sequence for event in self.trace) != tuple(
            range(1, len(self.trace) + 1)
        ):
            raise ValueError("workflow trace sequence must be contiguous")
        if self.stage is WorkflowStage.DECISION_REQUIRED and self.checkpoint is None:
            raise ValueError("decision-required workflow stage needs a typed checkpoint")
        if self.stage is WorkflowStage.FAILED and self.failure is None:
            raise ValueError("failed workflow stage needs a typed failure")
        if self.stage is not WorkflowStage.FAILED and self.failure is not None:
            raise ValueError("only a failed workflow may carry an active failure")
        if self.resolved_plan is not None and self.plan_fingerprint is None:
            raise ValueError("resolved workflow plan requires its stable fingerprint")
        if self.execution is not None and self.resolved_plan is None:
            raise ValueError("workflow execution requires a resolved plan")
        if self.publication_result is not None and self.publication_approval is None:
            raise ValueError("workflow publication result requires explicit approval")
        return self


def fingerprint_payload(payload: object) -> str:
    """Return the stable SHA-256 used for opaque workflow references."""

    return _fingerprint(payload)


def assert_sha256(value: str) -> str:
    """Validate a computed fingerprint at pure-domain boundaries."""

    if not _SHA256.fullmatch(value):
        raise ValueError("workflow fingerprint must be lowercase SHA-256")
    return value


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
