"""Read-only operator projections for semantic-change heads and shared audit chains."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from schemabridge.application.ports.semantic_change import SemanticChangeStorePort
from schemabridge.application.semantic_change import (
    InspectSemanticChange,
    SemanticChangeError,
    SemanticChangeErrorCode,
)
from schemabridge.domain.registry_control import ControlAuditChainVerification
from schemabridge.domain.semantic_change import (
    SemanticBindingSelectionSet,
    SemanticChangeCommit,
    SemanticChangeReport,
    SemanticEvidenceObservation,
)
from schemabridge.domain.semantic_change_scans import (
    SemanticChangeScanRequest,
    SemanticChangeScanSourceKind,
    SemanticChangeScanStatus,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope


@dataclass(frozen=True, slots=True)
class SemanticChangeAuditVerification:
    """Minimized verification result safe for an operator boundary."""

    valid: bool
    event_count: int
    head_revision: int
    chain_head_hash: str | None

    def __post_init__(self) -> None:
        if (
            isinstance(self.event_count, bool)
            or self.event_count < 0
            or isinstance(self.head_revision, bool)
            or self.head_revision < 0
            or (self.valid and self.head_revision > self.event_count)
            or (self.event_count == 0) != (self.chain_head_hash is None)
        ):
            raise ValueError("semantic change audit verification is invalid")


class ControlAuditChainReadPort(Protocol):
    """Read and cryptographically verify one complete workspace audit chain."""

    def verify_audit_chain(self, workspace_id: str) -> ControlAuditChainVerification:
        """Verify every immutable workspace event in sequence."""


class LatestSemanticChangeScanReadPort(Protocol):
    """Read the latest retained exact registry-pointer scan for one scope."""

    def load_latest_for_scope(
        self,
        scope: SemanticRegistryScope,
    ) -> SemanticChangeScanRequest | None:
        """Return no cross-scope or inferred catalog-generation fallback."""


SemanticChangeInspectorFactory = Callable[
    [SemanticChangeScanRequest],
    InspectSemanticChange,
]


@dataclass(frozen=True, slots=True)
class InspectLatestSemanticChange:
    """Bind operator inspection and commit revalidation to one exact durable scan."""

    scans: LatestSemanticChangeScanReadPort = field(repr=False)
    inspector_factory: SemanticChangeInspectorFactory = field(repr=False)
    store: SemanticChangeStorePort = field(repr=False)
    scope: SemanticRegistryScope

    def execute(
        self,
        *,
        binding_selections: SemanticBindingSelectionSet | None = None,
    ) -> SemanticChangeReport:
        report, observation = self.capture_current(
            binding_selections=binding_selections,
        )
        try:
            recorded = self.store.record_report(report, observation)
            checked = SemanticChangeReport.model_validate(recorded.model_dump(mode="json"))
        except Exception as error:
            raise SemanticChangeError(
                SemanticChangeErrorCode.UNAVAILABLE,
                "semantic change report could not be recorded",
            ) from error
        if checked != report:
            raise SemanticChangeError(
                SemanticChangeErrorCode.INVALID_RESPONSE,
                "semantic change store returned another report",
            )
        return checked

    def capture_current(
        self,
        *,
        binding_selections: SemanticBindingSelectionSet | None = None,
    ) -> tuple[SemanticChangeReport, SemanticEvidenceObservation]:
        try:
            scan = self.scans.load_latest_for_scope(self.scope)
        except Exception as error:
            raise SemanticChangeError(
                SemanticChangeErrorCode.UNAVAILABLE,
                "semantic change scan is unavailable",
            ) from error
        if scan is None:
            raise SemanticChangeError(
                SemanticChangeErrorCode.UNAVAILABLE,
                "semantic change scan is unavailable",
            )
        if (
            scan.workspace_id != self.scope.workspace_id
            or scan.source_kind is not SemanticChangeScanSourceKind.REGISTRY_POINTER
            or scan.catalog_scope != self.scope.catalog_scope
            or scan.registry_id != self.scope.registry_id
            or scan.registry_generation is None
            or scan.status is SemanticChangeScanStatus.SUPERSEDED
        ):
            raise SemanticChangeError(
                SemanticChangeErrorCode.INVALID_RESPONSE,
                "semantic change scan does not match the exact registry scope",
            )
        try:
            inspector = self.inspector_factory(scan)
        except Exception as error:
            raise SemanticChangeError(
                SemanticChangeErrorCode.UNAVAILABLE,
                "semantic change inspector is unavailable",
            ) from error
        if not isinstance(inspector, InspectSemanticChange) or inspector.scope != self.scope:
            raise SemanticChangeError(
                SemanticChangeErrorCode.INVALID_RESPONSE,
                "semantic change inspector crossed its registry scope",
            )
        report, observation = inspector.capture_current(
            binding_selections=binding_selections,
        )
        if (
            report.context.scope != self.scope
            or report.context.pointer_generation != scan.registry_generation
            or observation.context != report.context
            or observation.fingerprint != report.observation_fingerprint
        ):
            raise SemanticChangeError(
                SemanticChangeErrorCode.CAS_CONFLICT,
                "semantic change scan no longer matches current evidence",
            )
        return report, observation


@dataclass(frozen=True, slots=True)
class LoadSemanticChangeHead:
    """Load one exact scope head without exposing report or evidence payloads."""

    store: SemanticChangeStorePort = field(repr=False)

    def execute(self, scope: SemanticRegistryScope) -> SemanticChangeCommit | None:
        try:
            raw = self.store.load_head(scope)
        except Exception as error:
            raise SemanticChangeError(
                SemanticChangeErrorCode.UNAVAILABLE,
                "semantic evidence head is unavailable",
            ) from error
        if raw is None:
            return None
        try:
            checked = SemanticChangeCommit.model_validate(raw.model_dump(mode="json"))
        except (AttributeError, TypeError, ValueError) as error:
            raise SemanticChangeError(
                SemanticChangeErrorCode.INVALID_RESPONSE,
                "semantic evidence head is invalid",
            ) from error
        if checked != raw or checked.scope != scope:
            raise SemanticChangeError(
                SemanticChangeErrorCode.INVALID_RESPONSE,
                "semantic evidence head crossed its scope",
            )
        return checked


@dataclass(frozen=True, slots=True)
class VerifySemanticChangeAudit:
    """Verify the shared workspace chain and relate it to one semantic head."""

    heads: LoadSemanticChangeHead
    audit: ControlAuditChainReadPort = field(repr=False)

    def execute(self, scope: SemanticRegistryScope) -> SemanticChangeAuditVerification:
        head = self.heads.execute(scope)
        try:
            raw = self.audit.verify_audit_chain(scope.workspace_id)
            checked = ControlAuditChainVerification.model_validate(raw.model_dump(mode="json"))
        except Exception as error:
            raise SemanticChangeError(
                SemanticChangeErrorCode.UNAVAILABLE,
                "semantic change audit verification is unavailable",
            ) from error
        if checked != raw or checked.workspace_id != scope.workspace_id:
            raise SemanticChangeError(
                SemanticChangeErrorCode.INVALID_RESPONSE,
                "semantic change audit verification crossed its workspace",
            )
        head_revision = 0 if head is None else head.head_revision
        # The audit chain is workspace-wide, so registry and other governed
        # operations may legitimately make its event count exceed this head.
        valid = checked.valid and checked.event_count >= head_revision
        try:
            return SemanticChangeAuditVerification(
                valid=valid,
                event_count=checked.event_count,
                head_revision=head_revision,
                chain_head_hash=checked.head_hash,
            )
        except ValueError as error:
            raise SemanticChangeError(
                SemanticChangeErrorCode.INVALID_RESPONSE,
                "semantic change audit verification is inconsistent",
            ) from error


__all__ = [
    "ControlAuditChainReadPort",
    "InspectLatestSemanticChange",
    "LatestSemanticChangeScanReadPort",
    "LoadSemanticChangeHead",
    "SemanticChangeAuditVerification",
    "VerifySemanticChangeAudit",
]
