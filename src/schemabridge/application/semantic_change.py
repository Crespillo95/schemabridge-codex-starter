"""Governed semantic-change inspection, explicit decisions, and pre-I/O gating."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TypeVar

from pydantic import BaseModel

from schemabridge.application.ports.registry_control import (
    ActiveRegistryPointerReadPort,
    RegistryControlError,
    RegistryVersionReadPort,
)
from schemabridge.application.ports.semantic_change import (
    SemanticChangeCurrentInspectionPort,
    SemanticChangeDependencyIndexPort,
    SemanticChangeEvidencePort,
    SemanticChangeGateReadPort,
    SemanticChangePortError,
    SemanticChangePortErrorCode,
    SemanticChangeStorePort,
)
from schemabridge.domain.registry_control import (
    ActiveRegistryPointer,
    GovernedRegistryVersion,
    RegistryVersionTrust,
    registry_projection_fingerprint,
)
from schemabridge.domain.semantic_change import (
    GovernedJoinRef,
    GovernedMappingRef,
    SemanticBindingSelectionSet,
    SemanticChangeCommit,
    SemanticChangeConfirmation,
    SemanticChangeDecisionAction,
    SemanticChangeDecisionApproval,
    SemanticChangeDecisionProposal,
    SemanticChangeInspectionContext,
    SemanticChangeReport,
    SemanticChangeStatus,
    SemanticContextGateAssessment,
    SemanticDependencyIndexState,
    SemanticEvidenceBaseline,
    SemanticEvidenceObservation,
    SemanticImpactSet,
    SemanticPlanDependencies,
    build_semantic_change_approval,
    build_semantic_change_decision,
    build_semantic_change_report,
    classify_semantic_change_findings,
    prepare_semantic_change_decision,
    validate_semantic_change_approval,
)
from schemabridge.domain.semantic_registry import (
    GovernedSemanticRegistrySnapshot,
    SemanticRegistryScope,
)

_ModelT = TypeVar("_ModelT", bound=BaseModel)
_PortT = TypeVar("_PortT")


class SemanticChangeErrorCode(StrEnum):
    """Stable sanitized failures for operator, runtime, and HTTP boundaries."""

    INVALID_REQUEST = "semantic_change_invalid_request"
    UNAVAILABLE = "semantic_change_unavailable"
    EVIDENCE_UNAVAILABLE = "semantic_change_evidence_unavailable"
    DEPENDENCY_INDEX_INCOMPLETE = "semantic_change_dependency_index_incomplete"
    APPROVAL_REQUIRED = "semantic_change_approval_required"
    APPROVAL_MISMATCH = "semantic_change_approval_mismatch"
    BLOCKING_CHANGE = "semantic_change_blocking_change"
    CAS_CONFLICT = "semantic_change_cas_conflict"
    STALE_CONTEXT = "semantic_context_stale"
    INVALID_RESPONSE = "semantic_change_invalid_response"


class SemanticChangeError(RuntimeError):
    """A non-disclosing application failure."""

    def __init__(self, code: SemanticChangeErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class InspectSemanticChange:
    """Inspect only exact governed resources and persist one immutable report."""

    pointers: ActiveRegistryPointerReadPort
    versions: RegistryVersionReadPort
    evidence: SemanticChangeEvidencePort
    dependency_index: SemanticChangeDependencyIndexPort
    store: SemanticChangeStorePort
    scope: SemanticRegistryScope

    def execute(
        self,
        *,
        binding_selections: SemanticBindingSelectionSet | None = None,
    ) -> SemanticChangeReport:
        report, observation = self.capture_current(
            binding_selections=binding_selections,
        )
        recorded = _call_port(
            lambda: self.store.record_report(report, observation),
            operation_name="record semantic change report",
        )
        checked = _validated_exact(recorded, SemanticChangeReport)
        if checked != report:
            raise _invalid_response("semantic change store returned another report")
        return checked

    def capture_current(
        self,
        *,
        binding_selections: SemanticBindingSelectionSet | None = None,
    ) -> tuple[SemanticChangeReport, SemanticEvidenceObservation]:
        """Capture current facts without mutating the report or decision stores."""

        pointer, registry = _load_current_registry(
            self.pointers,
            self.versions,
            self.scope,
        )
        state = _call_port(
            lambda: self.dependency_index.load_state(self.scope),
            operation_name="load semantic dependency index",
        )
        state = _validated_exact(state, SemanticDependencyIndexState)
        if state.scope != self.scope:
            raise _invalid_response("semantic dependency index crossed registry scope")
        context = _inspection_context(pointer, registry, state)
        _validate_binding_selections(context, binding_selections)
        baseline = _call_port(
            lambda: self.store.load_baseline(self.scope),
            operation_name="load semantic evidence baseline",
        )
        if baseline is not None:
            baseline = _validated_exact(baseline, SemanticEvidenceBaseline)
            if baseline.scope != self.scope:
                raise _invalid_response("semantic baseline crossed registry scope")
        effective_baseline = _baseline_for_context(baseline, context)
        observation = _call_port(
            lambda: self.evidence.observe(
                context,
                registry,
                effective_baseline,
                binding_selections=binding_selections,
            ),
            operation_name="observe governed semantic evidence",
        )
        observation = _validated_exact(observation, SemanticEvidenceObservation)
        if observation.context != context:
            raise _invalid_response("semantic evidence returned another inspection context")

        findings = classify_semantic_change_findings(observation, effective_baseline)
        impacts = _call_port(
            lambda: self.dependency_index.resolve_impacts(
                context,
                findings,
            ),
            operation_name="resolve semantic blast radius",
        )
        impacts = _validated_exact(impacts, SemanticImpactSet)
        if (
            impacts.complete != state.complete
            or impacts.watermark != state.watermark
            or impacts.dependency_index_fingerprint != state.fingerprint
        ):
            raise _invalid_response("semantic impacts changed dependency-index state")
        report = build_semantic_change_report(observation, effective_baseline, impacts)
        return report, observation


@dataclass(frozen=True, slots=True)
class PrepareSemanticChangeDecision:
    """Prepare one exact, read-only decision proposal over an immutable report."""

    store: SemanticChangeStorePort

    def execute(
        self,
        scope: SemanticRegistryScope,
        report_id: str,
        *,
        action: SemanticChangeDecisionAction,
    ) -> SemanticChangeDecisionProposal:
        report, observation = _load_report_evidence(self.store, scope, report_id)
        head = _call_port(
            lambda: self.store.load_head(scope),
            operation_name="load semantic evidence head",
        )
        if head is not None:
            head = _validated_exact(head, SemanticChangeCommit)
            if head.scope != scope:
                raise _invalid_response("semantic evidence head crossed registry scope")
        expected_head_revision = 0 if head is None else head.head_revision
        previous_head_context_fingerprint = (
            None if head is None else _load_head_context_fingerprint(self.store, scope, head)
        )
        try:
            return prepare_semantic_change_decision(
                report,
                observation,
                action=action,
                expected_head_revision=expected_head_revision,
                previous_head_context_fingerprint=previous_head_context_fingerprint,
            )
        except ValueError as error:
            code = (
                SemanticChangeErrorCode.BLOCKING_CHANGE
                if report.status is SemanticChangeStatus.BLOCKED
                and action is not SemanticChangeDecisionAction.REJECT_CHANGE
                else SemanticChangeErrorCode.INVALID_REQUEST
            )
            raise SemanticChangeError(
                code,
                "semantic change decision cannot be prepared",
            ) from error


@dataclass(frozen=True, slots=True)
class PrepareSemanticChangeDecisionApproval:
    """Bind trusted actor, time, and closed confirmation to one exact proposal."""

    def execute(
        self,
        proposal: SemanticChangeDecisionProposal,
        *,
        actor: str,
        approved_at: datetime,
        confirmation: SemanticChangeConfirmation,
    ) -> SemanticChangeDecisionApproval:
        try:
            return build_semantic_change_approval(
                proposal,
                actor=actor,
                approved_at=approved_at,
                confirmation=confirmation,
            )
        except (TypeError, ValueError) as error:
            raise SemanticChangeError(
                SemanticChangeErrorCode.APPROVAL_MISMATCH,
                "semantic change approval does not match the exact proposal",
            ) from error


@dataclass(frozen=True, slots=True)
class CommitSemanticChangeDecision:
    """Revalidate current facts and atomically commit one exact CAS decision."""

    inspector: SemanticChangeCurrentInspectionPort
    store: SemanticChangeStorePort

    def execute(
        self,
        proposal: SemanticChangeDecisionProposal,
        approval: SemanticChangeDecisionApproval,
    ) -> SemanticChangeCommit:
        if not isinstance(approval, SemanticChangeDecisionApproval):
            raise SemanticChangeError(
                SemanticChangeErrorCode.APPROVAL_REQUIRED,
                "explicit semantic change approval is required",
            )
        try:
            validate_semantic_change_approval(proposal, approval)
        except (TypeError, ValueError) as error:
            raise SemanticChangeError(
                SemanticChangeErrorCode.APPROVAL_MISMATCH,
                "semantic change approval does not match the exact proposal",
            ) from error

        scope = proposal.report.context.scope
        stored_report, stored_observation = _load_report_evidence(
            self.store,
            scope,
            proposal.report.id,
        )
        if stored_report != proposal.report or stored_observation != proposal.observation:
            raise SemanticChangeError(
                SemanticChangeErrorCode.APPROVAL_MISMATCH,
                "semantic change proposal does not match immutable report evidence",
            )

        head = _call_port(
            lambda: self.store.load_head(scope),
            operation_name="load semantic evidence head",
        )
        if head is not None:
            head = _validated_exact(head, SemanticChangeCommit)
        replaying = head is not None and _is_exact_replay(head, proposal, approval)
        current_revision = 0 if head is None else head.head_revision
        if current_revision != proposal.expected_head_revision and not replaying:
            raise SemanticChangeError(
                SemanticChangeErrorCode.CAS_CONFLICT,
                "semantic evidence head changed; prepare a new decision",
            )

        if not replaying:
            current_report, current_observation = self.inspector.capture_current(
                binding_selections=_binding_selections_from_observation(proposal.observation),
            )
            if not _same_current_evidence(
                proposal,
                current_report,
                current_observation,
            ):
                raise SemanticChangeError(
                    SemanticChangeErrorCode.CAS_CONFLICT,
                    "semantic evidence changed; inspect and approve a new report",
                )

        try:
            decision = build_semantic_change_decision(proposal, approval)
        except (TypeError, ValueError) as error:
            raise SemanticChangeError(
                SemanticChangeErrorCode.APPROVAL_MISMATCH,
                "semantic change decision could not materialize its approval",
            ) from error
        committed = _call_port(
            lambda: self.store.commit_decision(
                proposal,
                approval,
                decision,
                decision.baseline,
            ),
            operation_name="commit semantic change decision",
        )
        committed = _validated_exact(committed, SemanticChangeCommit)
        expected_revision = proposal.expected_head_revision + 1
        if (
            committed.scope != scope
            or committed.head_revision != expected_revision
            or committed.decision != decision
            or committed.baseline != decision.baseline
        ):
            raise _invalid_response("semantic change store returned another commit")
        return committed


@dataclass(frozen=True, slots=True)
class AssertSemanticContextCurrent:
    """Fail closed for exact plan dependencies before compiler or source I/O."""

    gate: SemanticChangeGateReadPort

    def execute(
        self,
        dependencies: SemanticPlanDependencies,
    ) -> SemanticContextGateAssessment:
        try:
            assessment = self.gate.assess(dependencies)
        except Exception as error:
            raise SemanticChangeError(
                SemanticChangeErrorCode.STALE_CONTEXT,
                "semantic context is unavailable or stale",
            ) from error
        assessment = _validated_exact(assessment, SemanticContextGateAssessment)
        if (
            assessment.dependencies_fingerprint != dependencies.fingerprint
            or not assessment.eligible
        ):
            raise SemanticChangeError(
                SemanticChangeErrorCode.STALE_CONTEXT,
                "semantic context is unavailable or stale",
            )
        return assessment


def _inspection_context(
    pointer: ActiveRegistryPointer,
    registry: GovernedSemanticRegistrySnapshot,
    dependency_state: SemanticDependencyIndexState,
) -> SemanticChangeInspectionContext:
    mappings = tuple(
        GovernedMappingRef(
            logical_field=item.mapping.logical_field,
            physical_field=item.mapping.physical_field,
            version=item.mapping.version,
            approval_decision_id=_required_decision(item.approval_decision_id),
            physical_type=item.physical_type,
        )
        for item in registry.mapping_set.mappings
    )
    joins = tuple(
        GovernedJoinRef(
            contract_id=item.id,
            version=item.version,
            approval_decision_id=_required_decision(item.approval_decision_id),
            left_field=item.left_key.physical_field,
            right_field=item.right_key.physical_field,
            cardinality=item.cardinality,
            fanout_policy=item.fanout_policy,
        )
        for item in registry.join_contracts.contracts
    )
    values: dict[str, object] = {
        "scope": pointer.scope,
        "pointer_generation": pointer.generation,
        "pointer_fingerprint": registry_projection_fingerprint(pointer),
        "pointer_transition_id": pointer.transition_id,
        "registry_version": registry.version,
        "registry_fingerprint": registry.fingerprint,
        "mappings": mappings,
        "joins": joins,
        "dependency_index": dependency_state,
    }
    try:
        return SemanticChangeInspectionContext.create(**values)
    except ValueError as error:
        raise _invalid_response(
            "active registry could not form semantic inspection context"
        ) from error


def _load_current_registry(
    pointers: ActiveRegistryPointerReadPort,
    versions: RegistryVersionReadPort,
    scope: SemanticRegistryScope,
) -> tuple[ActiveRegistryPointer, GovernedSemanticRegistrySnapshot]:
    try:
        pointer = pointers.load_active(scope)
        if pointer is None:
            raise SemanticChangeError(
                SemanticChangeErrorCode.UNAVAILABLE,
                "active semantic registry is unavailable",
            )
        version = versions.load_version(scope, pointer.registry_version)
    except SemanticChangeError:
        raise
    except RegistryControlError as error:
        raise SemanticChangeError(
            SemanticChangeErrorCode.UNAVAILABLE,
            "active semantic registry is unavailable",
        ) from error
    except Exception as error:
        raise SemanticChangeError(
            SemanticChangeErrorCode.UNAVAILABLE,
            "active semantic registry is unavailable",
        ) from error
    pointer = _validated_exact(pointer, ActiveRegistryPointer)
    version = _validated_exact(version, GovernedRegistryVersion)
    registry = version.snapshot.registry
    if (
        pointer.scope != scope
        or version.trust is not RegistryVersionTrust.STRICT
        or version.snapshot.scope != scope
        or pointer.registry_version != registry.version
        or pointer.registry_fingerprint != registry.fingerprint
    ):
        raise SemanticChangeError(
            SemanticChangeErrorCode.UNAVAILABLE,
            "active semantic registry is unavailable",
        )
    return pointer, registry


def _load_report_evidence(
    store: SemanticChangeStorePort,
    scope: SemanticRegistryScope,
    report_id: str,
) -> tuple[SemanticChangeReport, SemanticEvidenceObservation]:
    report = _call_port(
        lambda: store.load_report(scope, report_id),
        operation_name="load semantic change report",
    )
    observation = _call_port(
        lambda: store.load_observation(scope, report_id),
        operation_name="load semantic change observation",
    )
    if report is None or observation is None:
        raise SemanticChangeError(
            SemanticChangeErrorCode.UNAVAILABLE,
            "semantic change report is unavailable",
        )
    report = _validated_exact(report, SemanticChangeReport)
    observation = _validated_exact(observation, SemanticEvidenceObservation)
    if (
        report.context.scope != scope
        or observation.context != report.context
        or observation.fingerprint != report.observation_fingerprint
    ):
        raise _invalid_response("semantic report evidence is inconsistent")
    return report, observation


def _load_head_context_fingerprint(
    store: SemanticChangeStorePort,
    scope: SemanticRegistryScope,
    head: SemanticChangeCommit,
) -> str:
    report = _call_port(
        lambda: store.load_report(scope, head.decision.report_id),
        operation_name="load semantic evidence head report",
    )
    if report is None:
        raise _invalid_response("semantic evidence head report is unavailable")
    checked = _validated_exact(report, SemanticChangeReport)
    if checked.context.scope != scope:
        raise _invalid_response("semantic evidence head report crossed registry scope")
    return checked.context.fingerprint


def _baseline_for_context(
    baseline: SemanticEvidenceBaseline | None,
    context: SemanticChangeInspectionContext,
) -> SemanticEvidenceBaseline | None:
    if baseline is None:
        return None
    if (
        baseline.scope != context.scope
        or baseline.context_fingerprint != context.fingerprint
        or baseline.pointer_generation != context.pointer_generation
        or baseline.pointer_fingerprint != context.pointer_fingerprint
        or baseline.registry_version != context.registry_version
        or baseline.registry_fingerprint != context.registry_fingerprint
    ):
        return None
    return baseline


def _validate_binding_selections(
    context: SemanticChangeInspectionContext,
    selections: SemanticBindingSelectionSet | None,
) -> None:
    if selections is None:
        return
    if selections.scope != context.scope:
        raise SemanticChangeError(
            SemanticChangeErrorCode.INVALID_REQUEST,
            "semantic binding selections do not match the inspection scope",
        )
    governed = {
        (
            item.approval_decision_id,
            item.version,
            item.physical_field.root,
        )
        for item in context.mappings
    }
    if any(item.mapping_identity not in governed for item in selections.selections):
        raise SemanticChangeError(
            SemanticChangeErrorCode.INVALID_REQUEST,
            "semantic binding selection is outside the active registry",
        )


def _binding_selections_from_observation(
    observation: SemanticEvidenceObservation,
) -> SemanticBindingSelectionSet | None:
    selections = tuple(
        item.explicit_selection
        for item in observation.fields
        if item.explicit_selection is not None
    )
    if not selections:
        return None
    return SemanticBindingSelectionSet.create(
        scope=observation.context.scope,
        selections=selections,
    )


def _same_current_evidence(
    proposal: SemanticChangeDecisionProposal,
    current_report: SemanticChangeReport,
    current_observation: SemanticEvidenceObservation,
) -> bool:
    approved = proposal.observation
    return (
        current_observation.context == approved.context
        and current_observation.catalog_generations == approved.catalog_generations
        and current_observation.fields == approved.fields
        and current_observation.joins == approved.joins
        and current_observation.complete == approved.complete
        and current_report.baseline_revision == proposal.report.baseline_revision
        and current_report.baseline_fingerprint == proposal.report.baseline_fingerprint
        and current_report.findings == proposal.report.findings
        and current_report.impacts == proposal.report.impacts
        and current_report.status is proposal.report.status
    )


def _is_exact_replay(
    head: SemanticChangeCommit,
    proposal: SemanticChangeDecisionProposal,
    approval: SemanticChangeDecisionApproval,
) -> bool:
    decision = head.decision
    return (
        decision.proposal_fingerprint == proposal.fingerprint
        and decision.report_id == proposal.report.id
        and decision.report_fingerprint == proposal.report.fingerprint
        and decision.action is proposal.action
        and decision.actor == approval.actor
        and decision.decided_at == approval.approved_at
    )


def _required_decision(value: str | None) -> str:
    if value is None:
        raise _invalid_response("active semantic registry contains an unapproved artifact")
    return value


def _call_port(
    operation: Callable[[], _PortT],
    *,
    operation_name: str,
) -> _PortT:
    """Call one port while translating vendor/storage detail into closed failures."""

    del operation_name
    try:
        return operation()
    except SemanticChangeError:
        raise
    except SemanticChangePortError as error:
        code = {
            SemanticChangePortErrorCode.EVIDENCE_UNAVAILABLE: (
                SemanticChangeErrorCode.EVIDENCE_UNAVAILABLE
            ),
            SemanticChangePortErrorCode.BINDING_AMBIGUOUS: (
                SemanticChangeErrorCode.EVIDENCE_UNAVAILABLE
            ),
            SemanticChangePortErrorCode.DEPENDENCY_INDEX_INCOMPLETE: (
                SemanticChangeErrorCode.DEPENDENCY_INDEX_INCOMPLETE
            ),
            SemanticChangePortErrorCode.CAS_CONFLICT: SemanticChangeErrorCode.CAS_CONFLICT,
            SemanticChangePortErrorCode.INVALID_RESPONSE: (
                SemanticChangeErrorCode.INVALID_RESPONSE
            ),
        }.get(error.code, SemanticChangeErrorCode.UNAVAILABLE)
        raise SemanticChangeError(
            code, "semantic change operation could not be completed"
        ) from error
    except Exception as error:
        raise SemanticChangeError(
            SemanticChangeErrorCode.UNAVAILABLE,
            "semantic change service is unavailable",
        ) from error


def _validated_exact(value: object, model: type[_ModelT]) -> _ModelT:
    try:
        checked = model.model_validate(value.model_dump(mode="python", warnings=False))  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError) as error:
        raise _invalid_response("semantic change port returned an invalid typed value") from error
    if checked != value:
        raise _invalid_response("semantic change port returned a non-canonical typed value")
    return checked


def _invalid_response(message: str) -> SemanticChangeError:
    return SemanticChangeError(SemanticChangeErrorCode.INVALID_RESPONSE, message)


__all__ = [
    "AssertSemanticContextCurrent",
    "CommitSemanticChangeDecision",
    "InspectSemanticChange",
    "PrepareSemanticChangeDecision",
    "PrepareSemanticChangeDecisionApproval",
    "SemanticChangeError",
    "SemanticChangeErrorCode",
]
