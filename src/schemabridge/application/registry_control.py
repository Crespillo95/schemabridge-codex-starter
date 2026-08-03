"""Approval-gated registry activation, rollback, loading, and reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from schemabridge.application.ports.registry_control import (
    ActiveRegistryPointerReadPort,
    RegistryControlError,
    RegistryControlErrorCode,
    RegistryControlStorePort,
    RegistryProjectionPort,
    RegistryVersionReadPort,
)
from schemabridge.domain.registry_control import (
    ActiveRegistryPointer,
    ControlAuditChainVerification,
    GovernedRegistryVersion,
    RegistryActivationAction,
    RegistryActivationApproval,
    RegistryActivationConfirmation,
    RegistryActivationProposal,
    RegistryActivationReadyHandoff,
    RegistryActivationTransition,
    RegistryControlCommit,
    RegistryProjectionOutboxItem,
    RegistryProjectionOutboxStatus,
    RegistryProjectionOutcome,
    RegistryProjectionState,
    RegistryReconciliationApproval,
    RegistryReconciliationCode,
    RegistryReconciliationConfirmation,
    RegistryReconciliationFinding,
    RegistryReconciliationReport,
    RegistryReconciliationSeverity,
    RegistryVersionTrust,
    build_registry_activation_transition,
    build_registry_projection_outbox,
    pointer_matches_proposal,
    registry_activation_approval_id,
    registry_activation_transition_id,
    registry_projection_fingerprint,
    registry_reconciliation_approval_id,
    validate_registry_activation_approval,
    validate_registry_reconciliation_approval,
)
from schemabridge.domain.semantic_registry import (
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
    datahub_registry_document_urn,
    semantic_registry_decision_ids,
)


@dataclass(frozen=True, slots=True)
class PrepareRegistryActivation:
    store: RegistryControlStorePort
    versions: RegistryVersionReadPort
    scope: SemanticRegistryScope

    def execute(self, target_version: int) -> RegistryActivationProposal:
        current = _load_active_pointer(self.store, self.scope)
        target = _load_strict_version(self.versions, self.scope, target_version)
        handoff = _load_activation_ready_handoff(self.store, target)
        _ensure_not_already_active(current, target)
        return _proposal(
            action=RegistryActivationAction.ACTIVATE,
            current=current,
            target=target,
            activation_ready_handoff=handoff,
        )


@dataclass(frozen=True, slots=True)
class PrepareRegistryRollback:
    store: RegistryControlStorePort
    versions: RegistryVersionReadPort
    scope: SemanticRegistryScope
    history_limit: int = 100

    def execute(self, target_transition_id: str) -> RegistryActivationProposal:
        current = _load_active_pointer(self.store, self.scope)
        if current is None:
            raise RegistryControlError(
                RegistryControlErrorCode.POINTER_NOT_FOUND,
                "active semantic registry pointer was not found",
            )
        historical = next(
            (
                transition
                for transition in _load_transitions(
                    self.store,
                    self.scope,
                    limit=self.history_limit,
                )
                if transition.id == target_transition_id
            ),
            None,
        )
        if (
            historical is None
            or historical.active_pointer.scope != self.scope
            or historical.active_pointer.generation >= current.generation
        ):
            raise RegistryControlError(
                RegistryControlErrorCode.ROLLBACK_NOT_ALLOWED,
                "rollback target is not a previously active registry transition",
            )
        if not _has_audit_event(
            self.store,
            historical.id,
        ) or not _audit_chain_is_valid(self.store, self.scope.workspace_id):
            raise RegistryControlError(
                RegistryControlErrorCode.ROLLBACK_NOT_ALLOWED,
                "rollback target lacks a valid immutable control audit chain",
            )
        target_pointer = historical.active_pointer
        target = _load_strict_version(
            self.versions,
            self.scope,
            target_pointer.registry_version,
        )
        if not _version_matches_pointer(target, target_pointer):
            raise RegistryControlError(
                RegistryControlErrorCode.VERSION_INVALID,
                "rollback target no longer matches its immutable registry version",
            )
        _ensure_not_already_active(current, target)
        return _proposal(
            action=RegistryActivationAction.ROLLBACK,
            current=current,
            target=target,
            rollback_transition_id=historical.id,
        )


@dataclass(frozen=True, slots=True)
class PrepareRegistryActivationApproval:
    def execute(
        self,
        proposal: RegistryActivationProposal,
        *,
        actor: str,
        approved_at: datetime,
        confirmation: RegistryActivationConfirmation,
    ) -> RegistryActivationApproval:
        try:
            return RegistryActivationApproval(
                id=registry_activation_approval_id(
                    proposal,
                    actor,
                    approved_at,
                    confirmation,
                ),
                proposal=proposal,
                actor=actor,
                approved_at=approved_at,
                confirmation=confirmation,
            )
        except ValueError as error:
            raise RegistryControlError(
                RegistryControlErrorCode.APPROVAL_MISMATCH,
                "registry activation approval does not match the exact proposal",
            ) from error


@dataclass(frozen=True, slots=True)
class CommitRegistryActivation:
    store: RegistryControlStorePort
    versions: RegistryVersionReadPort

    def execute(
        self,
        proposal: RegistryActivationProposal,
        approval: RegistryActivationApproval,
        *,
        committed_at: datetime,
    ) -> RegistryControlCommit:
        if not isinstance(approval, RegistryActivationApproval):
            raise RegistryControlError(
                RegistryControlErrorCode.APPROVAL_REQUIRED,
                "explicit registry activation approval is required",
            )
        try:
            validate_registry_activation_approval(proposal, approval)
        except ValueError as error:
            raise RegistryControlError(
                RegistryControlErrorCode.APPROVAL_MISMATCH,
                "registry activation approval does not match the exact proposal",
            ) from error

        current = _load_active_pointer(self.store, proposal.scope)
        replay_transition_id = registry_activation_transition_id(approval)
        replaying = (
            current is not None
            and current.transition_id == replay_transition_id
            and current.generation == proposal.expected_generation + 1
        )
        if not _current_matches_proposal(current, proposal) and not replaying:
            raise RegistryControlError(
                RegistryControlErrorCode.CAS_CONFLICT,
                "active semantic registry changed; prepare a new activation",
            )
        target = _load_strict_version(
            self.versions,
            proposal.scope,
            proposal.target_registry_version,
        )
        if not _version_matches_proposal(target, proposal):
            raise RegistryControlError(
                RegistryControlErrorCode.VERSION_INVALID,
                "activation target changed after approval",
            )
        if not replaying and proposal.action is RegistryActivationAction.ACTIVATE:
            observed_handoff = _load_activation_ready_handoff(self.store, target)
            if observed_handoff != proposal.activation_ready_handoff:
                raise RegistryControlError(
                    RegistryControlErrorCode.ACTIVATION_HANDOFF_MISMATCH,
                    "activation-ready publication or catalog authority changed after approval",
                )
        try:
            previous_pointer = (
                _load_proposal_previous_pointer(self.store, proposal) if replaying else current
            )
            transition = build_registry_activation_transition(
                proposal,
                approval,
                previous_pointer=previous_pointer,
                committed_at=committed_at,
            )
            outbox = build_registry_projection_outbox(transition)
        except ValueError as error:
            raise RegistryControlError(
                RegistryControlErrorCode.APPROVAL_MISMATCH,
                "registry activation approval could not materialize its exact transition",
            ) from error
        if replaying and transition.active_pointer != current:
            raise RegistryControlError(
                RegistryControlErrorCode.CAS_CONFLICT,
                "registry activation replay differs from the committed transition",
            )

        result = self.store.commit_transition(transition, outbox)
        try:
            validated = RegistryControlCommit.model_validate(
                result.model_dump(mode="python", warnings=False)
            )
        except (AttributeError, TypeError, ValueError) as error:
            raise RegistryControlError(
                RegistryControlErrorCode.INVALID_RESPONSE,
                "registry control store returned an invalid commit",
            ) from error
        if validated != result or validated.transition != transition or validated.outbox != outbox:
            raise RegistryControlError(
                RegistryControlErrorCode.INVALID_RESPONSE,
                "registry control store returned a commit for another transition",
            )
        return validated


@dataclass(frozen=True, slots=True)
class LoadActiveGovernedSemanticRegistry:
    """Existing registry-port shape backed only by the authoritative pointer."""

    store: ActiveRegistryPointerReadPort
    versions: RegistryVersionReadPort
    _scope: SemanticRegistryScope

    @property
    def scope(self) -> SemanticRegistryScope:
        return self._scope

    def load(self) -> ScopedSemanticRegistrySnapshot:
        pointer = _load_active_pointer(self.store, self.scope)
        if pointer is None:
            raise RegistryControlError(
                RegistryControlErrorCode.POINTER_NOT_FOUND,
                "active semantic registry pointer was not found",
            )
        version = _load_strict_version(
            self.versions,
            self.scope,
            pointer.registry_version,
        )
        if not _version_matches_pointer(version, pointer):
            raise RegistryControlError(
                RegistryControlErrorCode.VERSION_INVALID,
                "active pointer does not match its immutable registry version",
            )
        return version.snapshot.model_copy(
            update={
                "activation_generation": pointer.generation,
                "active_pointer_fingerprint": registry_projection_fingerprint(pointer),
            }
        )


@dataclass(frozen=True, slots=True)
class InspectRegistryReconciliation:
    store: RegistryControlStorePort
    versions: RegistryVersionReadPort
    projection: RegistryProjectionPort
    scope: SemanticRegistryScope

    def execute(self, *, inspected_at: datetime) -> RegistryReconciliationReport:
        pointer = _load_active_pointer(self.store, self.scope)
        if pointer is None:
            raise RegistryControlError(
                RegistryControlErrorCode.POINTER_NOT_FOUND,
                "active semantic registry pointer was not found",
            )
        desired = RegistryProjectionState(
            pointer=pointer,
            projection_fingerprint=registry_projection_fingerprint(pointer),
        )
        observed = _read_projection(self.projection, self.scope)
        pending = _load_pending_outbox(self.store, self.scope)
        findings: list[RegistryReconciliationFinding] = []

        try:
            version = _load_strict_version(
                self.versions,
                self.scope,
                pointer.registry_version,
            )
            if not _version_matches_pointer(version, pointer):
                raise RegistryControlError(
                    RegistryControlErrorCode.VERSION_INVALID,
                    "active registry version does not match its pointer",
                )
        except RegistryControlError:
            findings.append(
                _finding(
                    RegistryReconciliationCode.VERSION_CORRUPT,
                    RegistryReconciliationSeverity.BLOCKING,
                    "The active immutable registry version is unavailable or invalid.",
                )
            )

        if pending is not None:
            if pending.desired.pointer.generation < pointer.generation:
                findings.append(
                    _finding(
                        RegistryReconciliationCode.SUPERSEDED,
                        RegistryReconciliationSeverity.WARNING,
                        "The pending projection was superseded by a newer activation.",
                    )
                )
            elif pending.desired != desired:
                findings.append(
                    _finding(
                        RegistryReconciliationCode.PROJECTION_CONFLICT,
                        RegistryReconciliationSeverity.BLOCKING,
                        "The pending projection conflicts with the authoritative pointer.",
                    )
                )
            else:
                findings.append(
                    _finding(
                        RegistryReconciliationCode.PENDING_OUTBOX,
                        RegistryReconciliationSeverity.WARNING,
                        "The authoritative activation still has a pending projection.",
                    )
                )

        if not _has_audit_event(
            self.store,
            pointer.transition_id,
        ) or not _audit_chain_is_valid(self.store, self.scope.workspace_id):
            findings.append(
                _finding(
                    RegistryReconciliationCode.AUDIT_GAP,
                    RegistryReconciliationSeverity.BLOCKING,
                    "The authoritative activation audit chain is missing or invalid.",
                )
            )

        if observed is None:
            findings.append(
                _finding(
                    RegistryReconciliationCode.PROJECTION_MISSING,
                    RegistryReconciliationSeverity.WARNING,
                    "The DataHub active-pointer projection is missing.",
                )
            )
        elif observed.pointer.generation < pointer.generation:
            findings.append(
                _finding(
                    RegistryReconciliationCode.PROJECTION_BEHIND,
                    RegistryReconciliationSeverity.WARNING,
                    "The DataHub active-pointer projection is behind.",
                )
            )
        elif observed.pointer.generation > pointer.generation:
            findings.append(
                _finding(
                    RegistryReconciliationCode.PROJECTION_AHEAD,
                    RegistryReconciliationSeverity.BLOCKING,
                    "The DataHub active-pointer projection has an unknown newer generation.",
                )
            )
        elif observed != desired:
            findings.append(
                _finding(
                    RegistryReconciliationCode.PROJECTION_CONFLICT,
                    RegistryReconciliationSeverity.BLOCKING,
                    "The DataHub active-pointer projection conflicts at the current generation.",
                )
            )
        else:
            findings.append(
                _finding(
                    RegistryReconciliationCode.IN_SYNC,
                    RegistryReconciliationSeverity.INFO,
                    "The DataHub active-pointer projection matches the control plane.",
                )
            )
        unique_findings = {finding.code: finding for finding in findings}
        return RegistryReconciliationReport(
            scope=self.scope,
            active_pointer=pointer,
            desired_projection=desired,
            observed_projection=observed,
            pending_outbox=pending,
            findings=tuple(unique_findings.values()),
            inspected_at=inspected_at,
        )


@dataclass(frozen=True, slots=True)
class PrepareRegistryReconciliationApproval:
    def execute(
        self,
        report: RegistryReconciliationReport,
        *,
        actor: str,
        approved_at: datetime,
        confirmation: RegistryReconciliationConfirmation,
    ) -> RegistryReconciliationApproval:
        try:
            return RegistryReconciliationApproval(
                id=registry_reconciliation_approval_id(
                    report,
                    actor,
                    approved_at,
                    confirmation,
                ),
                report_fingerprint=report.fingerprint,
                scope=report.scope,
                generation=report.active_pointer.generation,
                registry_fingerprint=report.active_pointer.registry_fingerprint,
                actor=actor,
                approved_at=approved_at,
                confirmation=confirmation,
            )
        except ValueError as error:
            raise RegistryControlError(
                RegistryControlErrorCode.APPROVAL_MISMATCH,
                "registry reconciliation approval does not match the exact report",
            ) from error


@dataclass(frozen=True, slots=True)
class ReconcileRegistryProjection:
    store: RegistryControlStorePort
    versions: RegistryVersionReadPort
    projection: RegistryProjectionPort

    def execute(
        self,
        report: RegistryReconciliationReport,
        approval: RegistryReconciliationApproval,
        *,
        occurred_at: datetime,
    ) -> RegistryProjectionOutcome:
        try:
            validate_registry_reconciliation_approval(report, approval)
        except ValueError as error:
            raise RegistryControlError(
                RegistryControlErrorCode.APPROVAL_MISMATCH,
                "registry reconciliation approval does not match the exact report",
            ) from error

        current_report = InspectRegistryReconciliation(
            store=self.store,
            versions=self.versions,
            projection=self.projection,
            scope=report.scope,
        ).execute(inspected_at=report.inspected_at)
        if current_report != report:
            raise RegistryControlError(
                RegistryControlErrorCode.RECONCILIATION_CONFLICT,
                "registry reconciliation state changed; inspect and approve it again",
            )
        pending = report.pending_outbox
        if pending is None:
            raise RegistryControlError(
                RegistryControlErrorCode.RECONCILIATION_CONFLICT,
                "registry projection has no pending authorized outbox record",
            )
        codes = {finding.code for finding in report.findings}
        blocking = {
            RegistryReconciliationCode.PROJECTION_AHEAD,
            RegistryReconciliationCode.PROJECTION_CONFLICT,
            RegistryReconciliationCode.VERSION_CORRUPT,
            RegistryReconciliationCode.AUDIT_GAP,
        }
        if codes & blocking:
            raise RegistryControlError(
                RegistryControlErrorCode.RECONCILIATION_CONFLICT,
                "registry projection state is not safe for automatic repair",
            )

        if RegistryReconciliationCode.SUPERSEDED in codes:
            outcome = _projection_outcome(
                report,
                pending.id,
                pending.transition_id,
                pending.desired.pointer.generation,
                RegistryProjectionOutboxStatus.SUPERSEDED,
                occurred_at,
            )
            self.store.record_projection_outcome(outcome)
            return outcome

        if RegistryReconciliationCode.IN_SYNC in codes:
            observed = report.observed_projection
            assert observed is not None
        else:
            observed = _validate_projection_response(
                self.projection.project(report.desired_projection, approval),
                report.scope,
            )
            if observed != report.desired_projection:
                raise RegistryControlError(
                    RegistryControlErrorCode.INVALID_RESPONSE,
                    "registry projector returned another active pointer",
                )
            read_back = _read_projection(self.projection, report.scope)
            if read_back != report.desired_projection:
                raise RegistryControlError(
                    RegistryControlErrorCode.INVALID_RESPONSE,
                    "registry projection read-back did not match the approved pointer",
                )
            observed = read_back
        outcome = _projection_outcome(
            report,
            pending.id,
            pending.transition_id,
            pending.desired.pointer.generation,
            RegistryProjectionOutboxStatus.DELIVERED,
            occurred_at,
            observed=observed,
        )
        self.store.record_projection_outcome(outcome)
        return outcome


def _load_active_pointer(
    store: ActiveRegistryPointerReadPort,
    scope: SemanticRegistryScope,
) -> ActiveRegistryPointer | None:
    raw = store.load_active(scope)
    if raw is None:
        return None
    try:
        validated = ActiveRegistryPointer.model_validate(
            raw.model_dump(mode="python", warnings=False)
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise RegistryControlError(
            RegistryControlErrorCode.INVALID_RESPONSE,
            "registry control store returned an invalid active pointer",
        ) from error
    if validated != raw or validated.scope != scope:
        raise RegistryControlError(
            RegistryControlErrorCode.INVALID_RESPONSE,
            "registry control store returned another registry scope",
        )
    return validated


def _load_transitions(
    store: RegistryControlStorePort,
    scope: SemanticRegistryScope,
    *,
    limit: int,
) -> tuple[RegistryActivationTransition, ...]:
    if not 1 <= limit <= 1_000:
        raise RegistryControlError(
            RegistryControlErrorCode.INVALID_RESPONSE,
            "registry activation history limit is outside the bounded range",
        )
    raw = store.list_transitions(scope, limit=limit)
    if not isinstance(raw, tuple) or len(raw) > limit:
        raise RegistryControlError(
            RegistryControlErrorCode.INVALID_RESPONSE,
            "registry control store returned unbounded activation history",
        )
    try:
        validated = tuple(
            RegistryActivationTransition.model_validate(
                transition.model_dump(mode="python", warnings=False)
            )
            for transition in raw
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise RegistryControlError(
            RegistryControlErrorCode.INVALID_RESPONSE,
            "registry control store returned invalid activation history",
        ) from error
    generations = tuple(transition.active_pointer.generation for transition in validated)
    if (
        validated != raw
        or any(transition.active_pointer.scope != scope for transition in validated)
        or generations != tuple(sorted(set(generations)))
    ):
        raise RegistryControlError(
            RegistryControlErrorCode.INVALID_RESPONSE,
            "registry control store returned inconsistent activation history",
        )
    return validated


def _load_pending_outbox(
    store: RegistryControlStorePort,
    scope: SemanticRegistryScope,
) -> RegistryProjectionOutboxItem | None:
    raw = store.load_pending_outbox(scope)
    if raw is None:
        return None
    try:
        validated = RegistryProjectionOutboxItem.model_validate(
            raw.model_dump(mode="python", warnings=False)
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise RegistryControlError(
            RegistryControlErrorCode.INVALID_RESPONSE,
            "registry control store returned an invalid projection outbox",
        ) from error
    if (
        validated != raw
        or validated.status is not RegistryProjectionOutboxStatus.PENDING
        or validated.desired.pointer.scope != scope
    ):
        raise RegistryControlError(
            RegistryControlErrorCode.INVALID_RESPONSE,
            "registry control store returned another projection outbox",
        )
    return validated


def _has_audit_event(
    store: RegistryControlStorePort,
    transition_id: str,
) -> bool:
    present = store.has_audit_event(transition_id)
    if not isinstance(present, bool):
        raise RegistryControlError(
            RegistryControlErrorCode.INVALID_RESPONSE,
            "registry control store returned an invalid audit result",
        )
    return present


def _audit_chain_is_valid(
    store: RegistryControlStorePort,
    workspace_id: str,
) -> bool:
    raw = store.verify_audit_chain(workspace_id)
    try:
        validated = ControlAuditChainVerification.model_validate(
            raw.model_dump(mode="python", warnings=False)
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise RegistryControlError(
            RegistryControlErrorCode.INVALID_RESPONSE,
            "registry control store returned an invalid audit verification",
        ) from error
    if validated != raw or validated.workspace_id != workspace_id:
        raise RegistryControlError(
            RegistryControlErrorCode.INVALID_RESPONSE,
            "registry control store returned another audit workspace",
        )
    return validated.valid


def _read_projection(
    projection: RegistryProjectionPort,
    scope: SemanticRegistryScope,
) -> RegistryProjectionState | None:
    raw = projection.read(scope)
    if raw is None:
        return None
    return _validate_projection_response(raw, scope)


def _validate_projection_response(
    raw: object,
    scope: SemanticRegistryScope,
) -> RegistryProjectionState:
    if not isinstance(raw, RegistryProjectionState):
        raise RegistryControlError(
            RegistryControlErrorCode.INVALID_RESPONSE,
            "registry projector returned an invalid active-pointer projection",
        )
    try:
        validated = RegistryProjectionState.model_validate(
            raw.model_dump(mode="python", warnings=False)
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise RegistryControlError(
            RegistryControlErrorCode.INVALID_RESPONSE,
            "registry projector returned an invalid active-pointer projection",
        ) from error
    if validated != raw or validated.pointer.scope != scope:
        raise RegistryControlError(
            RegistryControlErrorCode.INVALID_RESPONSE,
            "registry projector returned another registry scope",
        )
    return validated


def _proposal(
    *,
    action: RegistryActivationAction,
    current: ActiveRegistryPointer | None,
    target: GovernedRegistryVersion,
    rollback_transition_id: str | None = None,
    activation_ready_handoff: RegistryActivationReadyHandoff | None = None,
) -> RegistryActivationProposal:
    registry = target.snapshot.registry
    return RegistryActivationProposal(
        action=action,
        scope=target.snapshot.scope,
        expected_generation=0 if current is None else current.generation,
        expected_registry_version=None if current is None else current.registry_version,
        expected_registry_fingerprint=(None if current is None else current.registry_fingerprint),
        expected_transition_id=None if current is None else current.transition_id,
        target_registry_version=registry.version,
        target_registry_fingerprint=registry.fingerprint,
        target_registry_urn=datahub_registry_document_urn(
            target.snapshot.scope,
            registry.version,
        ),
        target_publication_approval_id=target.publication_approval_id,
        activation_ready_handoff=activation_ready_handoff,
        decision_ids=semantic_registry_decision_ids(registry),
        rollback_transition_id=rollback_transition_id,
    )


def _load_strict_version(
    versions: RegistryVersionReadPort,
    scope: SemanticRegistryScope,
    version: int,
) -> GovernedRegistryVersion:
    loaded = versions.load_version(scope, version)
    try:
        validated = GovernedRegistryVersion.model_validate(
            loaded.model_dump(mode="python", warnings=False)
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise RegistryControlError(
            RegistryControlErrorCode.VERSION_INVALID,
            "immutable registry reader returned an invalid version",
        ) from error
    if validated != loaded or validated.snapshot.scope != scope:
        raise RegistryControlError(
            RegistryControlErrorCode.VERSION_INVALID,
            "immutable registry reader returned another scope",
        )
    if validated.trust is not RegistryVersionTrust.STRICT:
        raise RegistryControlError(
            RegistryControlErrorCode.LEGACY_VERSION,
            "legacy registry versions are read-only and cannot be active",
        )
    return validated


def _load_activation_ready_handoff(
    store: RegistryControlStorePort,
    target: GovernedRegistryVersion,
) -> RegistryActivationReadyHandoff:
    scope = target.snapshot.scope
    registry = target.snapshot.registry
    if registry.format_version != 2:
        raise RegistryControlError(
            RegistryControlErrorCode.LEGACY_VERSION,
            "only an exact registry v2 publication can become activation-ready",
        )
    loaded = store.load_activation_ready_handoff(scope, registry.version)
    if loaded is None:
        raise RegistryControlError(
            RegistryControlErrorCode.ACTIVATION_NOT_READY,
            "an exact activation-ready publication with current catalog authority is required",
        )
    try:
        validated = RegistryActivationReadyHandoff.model_validate(
            loaded.model_dump(mode="python", warnings=False)
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise RegistryControlError(
            RegistryControlErrorCode.ACTIVATION_HANDOFF_MISMATCH,
            "activation-ready publication authority returned an invalid handoff",
        ) from error
    if (
        validated != loaded
        or validated.scope != scope
        or validated.target_registry_version != registry.version
        or validated.target_registry_fingerprint != registry.fingerprint
        or validated.target_registry_urn != datahub_registry_document_urn(scope, registry.version)
        or validated.observed_authorization_id != target.publication_approval_id
        or not any(
            binding.source_proposal_id == validated.source_proposal_id
            and binding.source_proposal_fingerprint == validated.source_proposal_fingerprint
            for binding in registry.physical_bindings
        )
    ):
        raise RegistryControlError(
            RegistryControlErrorCode.ACTIVATION_HANDOFF_MISMATCH,
            "activation-ready publication does not match the exact immutable registry version",
        )
    return validated


def _version_matches_proposal(
    version: GovernedRegistryVersion,
    proposal: RegistryActivationProposal,
) -> bool:
    registry = version.snapshot.registry
    return (
        version.snapshot.scope == proposal.scope
        and registry.version == proposal.target_registry_version
        and registry.fingerprint == proposal.target_registry_fingerprint
        and datahub_registry_document_urn(proposal.scope, registry.version)
        == proposal.target_registry_urn
        and version.publication_approval_id == proposal.target_publication_approval_id
        and semantic_registry_decision_ids(registry) == proposal.decision_ids
    )


def _version_matches_pointer(
    version: GovernedRegistryVersion,
    pointer: ActiveRegistryPointer,
) -> bool:
    registry = version.snapshot.registry
    return (
        version.snapshot.scope == pointer.scope
        and registry.version == pointer.registry_version
        and registry.fingerprint == pointer.registry_fingerprint
        and datahub_registry_document_urn(pointer.scope, registry.version)
        == pointer.registry_target
        and semantic_registry_decision_ids(registry) == pointer.decision_ids
    )


def _ensure_not_already_active(
    current: ActiveRegistryPointer | None,
    target: GovernedRegistryVersion,
) -> None:
    if current is not None and _version_matches_pointer(target, current):
        raise RegistryControlError(
            RegistryControlErrorCode.ALREADY_ACTIVE,
            "the requested registry version is already active",
        )


def _current_matches_proposal(
    current: ActiveRegistryPointer | None,
    proposal: RegistryActivationProposal,
) -> bool:
    if proposal.expected_generation == 0:
        return current is None
    return current is not None and pointer_matches_proposal(current, proposal)


def _load_proposal_previous_pointer(
    store: RegistryControlStorePort,
    proposal: RegistryActivationProposal,
) -> ActiveRegistryPointer | None:
    if proposal.expected_generation == 0:
        return None
    expected_transition_id = proposal.expected_transition_id
    assert expected_transition_id is not None
    previous = next(
        (
            transition.active_pointer
            for transition in _load_transitions(
                store,
                proposal.scope,
                limit=1_000,
            )
            if transition.id == expected_transition_id
        ),
        None,
    )
    if previous is None or not pointer_matches_proposal(previous, proposal):
        raise RegistryControlError(
            RegistryControlErrorCode.CAS_CONFLICT,
            "registry activation replay cannot verify its previous pointer",
        )
    return previous


def _finding(
    code: RegistryReconciliationCode,
    severity: RegistryReconciliationSeverity,
    message: str,
) -> RegistryReconciliationFinding:
    return RegistryReconciliationFinding(code=code, severity=severity, message=message)


def _projection_outcome(
    report: RegistryReconciliationReport,
    outbox_id: str,
    transition_id: str,
    generation: int,
    status: RegistryProjectionOutboxStatus,
    occurred_at: datetime,
    *,
    observed: RegistryProjectionState | None = None,
) -> RegistryProjectionOutcome:
    return RegistryProjectionOutcome(
        outbox_id=outbox_id,
        transition_id=transition_id,
        scope=report.scope,
        generation=generation,
        status=status,
        observed_projection=observed,
        occurred_at=occurred_at,
    )
