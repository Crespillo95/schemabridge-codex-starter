"""Ports and sanitized failures for the dedicated registry-publication boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol, TypeVar

from schemabridge.domain.registry_publication import (
    ObservedRegistryPublicationResult,
    PublicationReadbackReceipt,
    PublishableRegistryVersion,
    RegistryPublicationAuthorization,
)
from schemabridge.domain.registry_publication_jobs import (
    PreparedRegistryPublicationProposal,
    RegistryPublicationFailureCode,
    RegistryPublicationJob,
)
from schemabridge.domain.semantic_registry import (
    GovernedPhysicalBinding,
    GovernedSemanticRegistrySnapshot,
    SemanticRegistryScope,
)

RegistryPublicationOperationT = TypeVar("RegistryPublicationOperationT")


class RegistryPublicationStoreErrorCode(StrEnum):
    NOT_FOUND = "registry_publication_not_found"
    IDEMPOTENCY_CONFLICT = "registry_publication_idempotency_conflict"
    TARGET_RESERVED = "registry_publication_target_reserved"
    STATE_CONFLICT = "registry_publication_state_conflict"
    LEASE_CONFLICT = "registry_publication_lease_conflict"
    SCHEMA_MISMATCH = "registry_publication_schema_mismatch"
    UNAVAILABLE = "registry_publication_store_unavailable"
    INVALID_RESPONSE = "registry_publication_invalid_response"


class RegistryPublicationStoreError(RuntimeError):
    def __init__(self, code: RegistryPublicationStoreErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class RegistryPublicationAuthorityError(RuntimeError):
    """Sanitized exact-authority failure already classified for worker settlement."""

    def __init__(self, code: RegistryPublicationFailureCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class RegistryPublicationHeartbeatSupervisorError(RuntimeError):
    """Lease maintenance failed; no worker settlement can be trusted."""


@dataclass(frozen=True, slots=True)
class RegistryPublicationJobMutation:
    job: RegistryPublicationJob
    replayed: bool = False


class RegistryPublicationProposalPort(Protocol):
    def load_exact(
        self,
        workspace_id: str,
        proposal_id: str,
    ) -> PreparedRegistryPublicationProposal | None:
        """Load one exact immutable supported proposal inside the workspace."""


class RegistryPublicationAuthorityPort(Protocol):
    def resolve_base(
        self,
        proposal: PreparedRegistryPublicationProposal,
    ) -> GovernedSemanticRegistrySnapshot | None:
        """Revalidate proposal/catalog/pointer and return the exact strict v2 base."""


class RegistryPublicationPhysicalBindingAuthorityPort(Protocol):
    def require_current(
        self,
        scope: SemanticRegistryScope,
        bindings: tuple[GovernedPhysicalBinding, ...],
    ) -> None:
        """Require every retained physical binding to match current catalog authority."""


class ObservedRegistryPublisherPort(Protocol):
    def observe(
        self,
        candidate: PublishableRegistryVersion,
        authorization: RegistryPublicationAuthorization,
        *,
        observed_at: datetime,
    ) -> ObservedRegistryPublicationResult:
        """Read and verify the exact immutable target without creating it."""

    def publish(
        self,
        candidate: PublishableRegistryVersion,
        authorization: RegistryPublicationAuthorization,
        *,
        observed_at: datetime,
    ) -> ObservedRegistryPublicationResult:
        """Publish and read back one exact observed-identity registry v2 document."""


class RegistryPublicationJobApiStorePort(Protocol):
    def submit(self, job: RegistryPublicationJob) -> RegistryPublicationJobMutation:
        """Reserve the exact scope/version and persist a submitted event atomically."""

    def load_by_idempotency(
        self,
        workspace_id: str,
        submitted_by: str,
        idempotency_digest: str,
    ) -> RegistryPublicationJob | None:
        """Load one exact submitter-scoped idempotency replay."""

    def load(self, workspace_id: str, job_id: str) -> RegistryPublicationJob | None:
        """Load one exact tenant-scoped publication job."""

    def authorize(
        self,
        job_id: str,
        *,
        workspace_id: str,
        expected_revision: int,
        authorization: RegistryPublicationAuthorization,
    ) -> RegistryPublicationJob:
        """CAS one awaiting candidate to approved."""

    def request_cancellation(
        self,
        job_id: str,
        *,
        workspace_id: str,
        expected_revision: int,
        requested_by: str,
    ) -> RegistryPublicationJob:
        """Cancel waiting work or mark a leased job for cooperative cancellation."""


class RegistryPublicationJobWorkerStorePort(RegistryPublicationJobApiStorePort, Protocol):
    def claim_next(
        self,
        *,
        worker_id: str,
        lease_capability: str,
        lease_duration: timedelta,
    ) -> RegistryPublicationJob | None:
        """Claim one due job using database time and a monotonic fencing token."""

    def heartbeat(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
        lease_duration: timedelta,
    ) -> RegistryPublicationJob:
        """Renew one exact lease using database time."""

    def record_candidate(
        self,
        job_id: str,
        candidate: PublishableRegistryVersion,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
    ) -> RegistryPublicationJob:
        """Persist the exact assembled candidate and release the preparation lease."""

    def complete(
        self,
        job_id: str,
        receipt: PublicationReadbackReceipt,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
    ) -> RegistryPublicationJob:
        """Persist activation-ready only after exact external readback."""

    def fail(
        self,
        job_id: str,
        code: RegistryPublicationFailureCode,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
    ) -> RegistryPublicationJob:
        """Classify one leased failure into retry or terminal state."""

    def acknowledge_cancellation(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
    ) -> RegistryPublicationJob:
        """Acknowledge cooperative cancellation for one exact lease."""

    def reap_expired_leases(self, *, limit: int = 100) -> int:
        """Recover or dead-letter a bounded batch of expired claims using database time."""


class RegistryPublicationHeartbeatSupervisorPort(Protocol):
    def run(
        self,
        *,
        claim: RegistryPublicationJob,
        worker_id: str,
        lease_capability: str,
        lease_duration: timedelta,
        heartbeat_interval: timedelta,
        operation: Callable[[], RegistryPublicationOperationT],
    ) -> tuple[RegistryPublicationOperationT, RegistryPublicationJob]:
        """Run one external write while maintaining the exact fenced lease."""


__all__ = [
    "ObservedRegistryPublisherPort",
    "RegistryPublicationAuthorityError",
    "RegistryPublicationAuthorityPort",
    "RegistryPublicationHeartbeatSupervisorError",
    "RegistryPublicationHeartbeatSupervisorPort",
    "RegistryPublicationJobApiStorePort",
    "RegistryPublicationJobMutation",
    "RegistryPublicationJobWorkerStorePort",
    "RegistryPublicationPhysicalBindingAuthorityPort",
    "RegistryPublicationProposalPort",
    "RegistryPublicationStoreError",
    "RegistryPublicationStoreErrorCode",
]
