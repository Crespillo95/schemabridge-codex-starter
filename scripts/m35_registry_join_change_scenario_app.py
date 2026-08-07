#!/usr/bin/env python3
"""Isolated browser scenario for the governed M35 Phase-A join journey.

The process composes the real M35 authoring use cases, pure relationship-profile/job
transitions, M34 publication worker, registry-v2 assembler, and exact DataHub read-back
adapter. All catalog/profile inputs are synthetic. There is no SQL, source write, network,
activation adapter, production credential, raw row, or sample value in this process.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final, TypeVar

import streamlit as st

from schemabridge.adapters.semantic_registry.datahub import (
    DataHubObservedSemanticRegistryPublisher,
    DataHubRegistryDocument,
    DataHubRegistryDocumentWrite,
    DataHubRegistryIdentity,
    DataHubRegistryWriteConfig,
)
from schemabridge.adapters.storage.registry_changes import InMemoryRegistryChangeStore
from schemabridge.application.authorization import AuthorizationError
from schemabridge.application.ports.registry_publication import (
    RegistryPublicationAuthorityError,
    RegistryPublicationJobMutation,
    RegistryPublicationStoreError,
    RegistryPublicationStoreErrorCode,
)
from schemabridge.application.ports.semantic_profile_jobs import (
    SemanticJoinProfileQueueError,
    SemanticJoinProfileQueueErrorCode,
)
from schemabridge.application.registry_change_authorization import (
    RegistryChangeAuthorizationPolicy,
)
from schemabridge.application.registry_changes import (
    DecideRegistryJoinChange,
    FinalizeRegistryJoinChangeDraft,
    PrepareRegistryJoinChangePublication,
    RegistryChangeAuthoringError,
    RegistryChangeAuthoringErrorCode,
    RequestRegistryJoinProfile,
)
from schemabridge.application.registry_publication import (
    AuthorizeRegistryPublication,
    InspectRegistryPublication,
    RegistryPublicationError,
    SubmitRegistryPublication,
)
from schemabridge.application.registry_publication_worker import (
    RegistryPublisherWorkerError,
    RunOneRegistryPublisherWorker,
)
from schemabridge.application.semantic_onboarding_authorization import (
    SemanticOnboardingAuthorizationPolicy,
)
from schemabridge.domain.catalog_inventory import (
    CatalogAssetId,
    CatalogAssetLocator,
    CatalogConnectionId,
    CatalogFieldLocator,
)
from schemabridge.domain.concepts import CanonicalType, LogicalFieldRef, LogicalModelRef
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    QueryCostBudget,
    SourceConnectorKind,
    SourceDialect,
    postgres_type_contract_fingerprint,
)
from schemabridge.domain.decisions import ApprovalStatus, DecisionAction
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)
from schemabridge.domain.joins import (
    DeclaredRelationship,
    JoinProposal,
    NormalizedJoinKey,
    RelationshipProfile,
)
from schemabridge.domain.mappings import ConfidenceScore
from schemabridge.domain.registry_change_authoring import (
    RegistryChangeAuditEvent,
    RegistryJoinProfileAuthoringRequest,
    RequestRegistryJoinProfileInput,
)
from schemabridge.domain.registry_changes import (
    PreparedRegistryJoinProposal,
    RegistryJoinChangeDraft,
    RegistryJoinChangeStatus,
    RegistryJoinProfileRequest,
    decide_registry_join_change,
    resolve_registry_join_base_evidence,
)
from schemabridge.domain.registry_control import (
    ActiveRegistryPointer,
    GovernedRegistryVersion,
    RegistryVersionTrust,
    registry_projection_fingerprint,
)
from schemabridge.domain.registry_publication import (
    PublicationReadbackReceipt,
    PublishableRegistryVersion,
    RegistryPublicationAuthorization,
    RegistryPublicationAuthorizationConfirmation,
    assemble_publishable_registry_version,
)
from schemabridge.domain.registry_publication_jobs import (
    PreparedRegistryPublicationProposal,
    RegistryPublicationFailureCode,
    RegistryPublicationJob,
    RegistryPublicationJobStatus,
    acknowledge_registry_publication_cancellation,
    authorize_registry_publication_job,
    complete_registry_publication_job,
    fail_registry_publication_job,
    heartbeat_registry_publication_job,
    lease_registry_publication_job,
    reap_expired_registry_publication_lease,
    record_registry_publication_candidate,
    request_registry_publication_cancellation,
)
from schemabridge.domain.request_context import LogicalFieldRole
from schemabridge.domain.semantic_onboarding import (
    OnboardingEvidence,
    OnboardingEvidenceKind,
    OnboardingRegistryBase,
    PhysicalCatalogObservation,
    PreparedSemanticOnboardingProposal,
    SemanticFieldDefinition,
    SemanticMappingProposal,
    SemanticModelDefinition,
    SemanticModelProposal,
    SemanticOnboardingDraft,
)
from schemabridge.domain.semantic_profile_jobs import (
    SemanticJoinProfileJob,
    SemanticJoinProfileSubmission,
    SemanticJoinProfileTargetRef,
    claim_semantic_join_profile_job,
    complete_semantic_join_profile_job,
    semantic_join_profile_proposal_fingerprint,
)
from schemabridge.domain.semantic_registry import (
    GovernedSemanticRegistrySnapshot,
    PhysicalValueType,
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
    datahub_registry_document_urn,
)
from schemabridge.domain.transformations import IdentityStep, TransformationPlan

_SCENARIO_TOKEN_ENV: Final = "SCHEMABRIDGE_M35_SCENARIO_TOKEN"
_TOKEN_PATTERN: Final = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_NOW: Final = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
_WORKSPACE: Final = "m35-synthetic-tenant"
_CONNECTION: Final = CatalogConnectionId("warehouse-commerce")
_OPAQUE_ORDERS_URN: Final = "urn:li:dataset:(urn:li:dataPlatform:postgres,opaque-orders-9f82,PROD)"
_OPAQUE_CUSTOMERS_URN: Final = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,opaque-customers-c7e1,PROD)"
)
_WRITER_ACTOR: Final = "urn:li:corpuser:schemabridge-registry-publisher"
_CAPABILITY: Final = "m35-synthetic-lease-capability-" + ("x" * 48)
_CHANGE_ID: Final = "join-change-order-customer"
_REQUEST_KEY: Final = "m35-registry-change-request-0001"
_FINALIZE_KEY: Final = "m35-registry-change-finalize-0001"
_DECIDE_KEY: Final = "m35-registry-change-decision-0001"
_PREPARE_KEY: Final = "m35-registry-change-prepare-0001"
_PUBLICATION_KEY: Final = "m35-registry-publication-submit-0001"
_T = TypeVar("_T")


@dataclass
class _ScenarioClock:
    value: datetime = _NOW

    def now(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta = timedelta(seconds=1)) -> datetime:
        self.value += delta
        return self.value


@dataclass(frozen=True, slots=True)
class _Pointers:
    pointer: ActiveRegistryPointer

    def load_active(self, scope: object) -> ActiveRegistryPointer:
        del scope
        return self.pointer


@dataclass(frozen=True, slots=True)
class _Versions:
    version: GovernedRegistryVersion

    def load_version(self, scope: object, version: int) -> GovernedRegistryVersion:
        del scope
        if version != self.version.snapshot.registry.version:
            raise ValueError("synthetic registry version is unavailable")
        return self.version


@dataclass(frozen=True, slots=True)
class _Targets:
    target: GovernedExecutionTarget

    def resolve_current(
        self,
        *,
        workspace_id: str,
        connection_id: CatalogConnectionId,
    ) -> GovernedExecutionTarget:
        if workspace_id != self.target.workspace_id or connection_id != self.target.connection_id:
            raise ValueError("synthetic execution target is unavailable")
        return self.target


@dataclass
class _Bindings:
    calls: int = 0

    def require_current(self, scope: object, bindings: tuple[object, ...]) -> None:
        del scope
        self.calls += 1
        if len(bindings) != 2:
            raise ValueError("synthetic exact bindings changed")


class _ProfileQueue:
    """One exact synthetic aggregate-only profile queue."""

    def __init__(self, store: InMemoryRegistryChangeStore, clock: _ScenarioClock) -> None:
        self.store = store
        self.clock = clock
        self.job: SemanticJoinProfileJob | None = None
        self.enqueue_calls = 0
        self.source_read_calls = 0
        self.source_write_calls = 0
        self.persisted_before_enqueue = False

    def enqueue(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
        *,
        max_attempts: int = 5,
    ) -> SemanticJoinProfileSubmission:
        self.enqueue_calls += 1
        self.persisted_before_enqueue = bool(
            self.store.list_for_workspace(
                authoring.workspace_id,
                owner_actor_id=None,
                limit=50,
            )
        )
        if not self.persisted_before_enqueue:
            raise SemanticJoinProfileQueueError(
                SemanticJoinProfileQueueErrorCode.STORE_UNAVAILABLE,
                "synthetic queue rejected an unpersisted request",
            )
        request = authoring.request
        if self.job is not None:
            return SemanticJoinProfileSubmission(job=self.job, replayed=True)
        self.job = SemanticJoinProfileJob.requested(
            workspace_id=authoring.workspace_id,
            scan_id=request.scan_id,
            proposal=request.proposal,
            execution_target=request.execution_target,
            requested_at=request.requested_at,
            max_attempts=max_attempts,
            connector_contract_version=request.execution_target.route_revision,
        )
        return SemanticJoinProfileSubmission(job=self.job, replayed=False)

    def load(
        self,
        authoring: RegistryJoinProfileAuthoringRequest,
    ) -> SemanticJoinProfileJob | None:
        request = authoring.request
        if self.job is None:
            return None
        if (
            self.job.workspace_id != authoring.workspace_id
            or self.job.scan_id != request.scan_id
            or self.job.proposal_fingerprint
            != semantic_join_profile_proposal_fingerprint(request.proposal)
        ):
            return None
        return self.job

    def complete_current(self, profile: RelationshipProfile) -> None:
        if self.job is None:
            raise ValueError("synthetic profile job is unavailable")
        claimed_at = self.clock.advance(timedelta(seconds=10))
        leased = claim_semantic_join_profile_job(
            self.job,
            worker_id="m35-profile-worker",
            lease_capability="m35-profile-capability-0123456789abcdef",
            claimed_at=claimed_at,
            lease_expires_at=claimed_at + timedelta(minutes=3),
        )
        completed_at = self.clock.advance(timedelta(seconds=10))
        self.job = complete_semantic_join_profile_job(
            leased,
            worker_id="m35-profile-worker",
            lease_capability="m35-profile-capability-0123456789abcdef",
            fencing_token=leased.fencing_token,
            profile=profile,
            completed_at=completed_at,
            retain_until=completed_at + timedelta(days=30),
        )
        self.source_read_calls += 1


@dataclass
class _JoinProposalStore:
    proposal: PreparedRegistryJoinProposal | None = None

    def record(self, proposal: PreparedRegistryJoinProposal) -> None:
        if self.proposal is not None and self.proposal != proposal:
            raise ValueError("synthetic proposal slot is immutable")
        self.proposal = proposal

    def load_exact(
        self,
        workspace_id: str,
        proposal_id: str,
    ) -> PreparedRegistryJoinProposal | None:
        proposal = self.proposal
        if (
            proposal is not None
            and proposal.workspace_id == workspace_id
            and proposal.id == proposal_id
        ):
            return proposal
        return None


class _PublicationJobStore:
    """One-job deterministic store applying the real M34 pure transitions."""

    def __init__(self, clock: _ScenarioClock) -> None:
        self.clock = clock
        self.job: RegistryPublicationJob | None = None
        self.transitions: list[str] = []

    def submit(self, job: RegistryPublicationJob) -> RegistryPublicationJobMutation:
        if self.job is None:
            self._record(job)
            return RegistryPublicationJobMutation(job=job)
        if (
            self.job.idempotency_digest == job.idempotency_digest
            and self.job.request_fingerprint == job.request_fingerprint
            and self.job.submitted_by == job.submitted_by
        ):
            return RegistryPublicationJobMutation(job=self.job, replayed=True)
        raise RegistryPublicationStoreError(
            RegistryPublicationStoreErrorCode.TARGET_RESERVED,
            "synthetic publication target is already reserved",
        )

    def load_by_idempotency(
        self,
        workspace_id: str,
        submitted_by: str,
        idempotency_digest: str,
    ) -> RegistryPublicationJob | None:
        job = self.job
        if (
            job is not None
            and job.scope.workspace_id == workspace_id
            and job.submitted_by == submitted_by
            and job.idempotency_digest == idempotency_digest
        ):
            return job
        return None

    def load(self, workspace_id: str, job_id: str) -> RegistryPublicationJob | None:
        job = self.job
        if job is not None and job.scope.workspace_id == workspace_id and job.id == job_id:
            return job
        return None

    def authorize(
        self,
        job_id: str,
        *,
        workspace_id: str,
        expected_revision: int,
        authorization: RegistryPublicationAuthorization,
    ) -> RegistryPublicationJob:
        job = self._require(job_id, workspace_id)
        if job.revision != expected_revision:
            raise _state_conflict()
        updated = authorize_registry_publication_job(
            job,
            authorization,
            authorized_at=self.clock.advance(),
        )
        self._record(updated)
        return updated

    def request_cancellation(
        self,
        job_id: str,
        *,
        workspace_id: str,
        expected_revision: int,
        requested_by: str,
    ) -> RegistryPublicationJob:
        del requested_by
        job = self._require(job_id, workspace_id)
        if job.revision != expected_revision:
            raise _state_conflict()
        updated = request_registry_publication_cancellation(
            job,
            requested_at=self.clock.advance(),
        )
        self._record(updated)
        return updated

    def reap_expired_leases(self, *, limit: int = 100) -> int:
        job = self.job
        if limit < 1 or job is None or job.lease is None:
            return 0
        if job.lease.expires_at > self.clock.now():
            return 0
        self._record(reap_expired_registry_publication_lease(job, expired_at=self.clock.now()))
        return 1

    def claim_next(
        self,
        *,
        worker_id: str,
        lease_capability: str,
        lease_duration: timedelta,
    ) -> RegistryPublicationJob | None:
        job = self.job
        if (
            job is None
            or job.status
            not in {
                RegistryPublicationJobStatus.QUEUED,
                RegistryPublicationJobStatus.APPROVED,
                RegistryPublicationJobStatus.RETRY_WAIT,
            }
            or job.available_at > self.clock.now()
        ):
            return None
        updated = lease_registry_publication_job(
            job,
            worker_id=worker_id,
            lease_capability=lease_capability,
            acquired_at=self.clock.advance(),
            lease_duration=lease_duration,
        )
        self._record(updated)
        return updated

    def heartbeat(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
        lease_duration: timedelta,
    ) -> RegistryPublicationJob:
        updated = heartbeat_registry_publication_job(
            self._require_job(job_id),
            worker_id=worker_id,
            lease_capability=lease_capability,
            fencing_token=fencing_token,
            heartbeat_at=self.clock.advance(),
            lease_duration=lease_duration,
        )
        self._record(updated)
        return updated

    def record_candidate(
        self,
        job_id: str,
        candidate: PublishableRegistryVersion,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
    ) -> RegistryPublicationJob:
        updated = record_registry_publication_candidate(
            self._require_job(job_id),
            candidate,
            worker_id=worker_id,
            lease_capability=lease_capability,
            fencing_token=fencing_token,
            completed_at=self.clock.advance(),
        )
        self._record(updated)
        return updated

    def complete(
        self,
        job_id: str,
        receipt: PublicationReadbackReceipt,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
    ) -> RegistryPublicationJob:
        updated = complete_registry_publication_job(
            self._require_job(job_id),
            receipt,
            worker_id=worker_id,
            lease_capability=lease_capability,
            fencing_token=fencing_token,
            completed_at=self.clock.advance(),
        )
        self._record(updated)
        return updated

    def fail(
        self,
        job_id: str,
        code: RegistryPublicationFailureCode,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
    ) -> RegistryPublicationJob:
        updated = fail_registry_publication_job(
            self._require_job(job_id),
            code,
            worker_id=worker_id,
            lease_capability=lease_capability,
            fencing_token=fencing_token,
            failed_at=self.clock.advance(),
        )
        self._record(updated)
        return updated

    def acknowledge_cancellation(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_capability: str,
        fencing_token: int,
    ) -> RegistryPublicationJob:
        updated = acknowledge_registry_publication_cancellation(
            self._require_job(job_id),
            worker_id=worker_id,
            lease_capability=lease_capability,
            fencing_token=fencing_token,
            cancelled_at=self.clock.advance(),
        )
        self._record(updated)
        return updated

    def _require(self, job_id: str, workspace_id: str) -> RegistryPublicationJob:
        job = self._require_job(job_id)
        if job.scope.workspace_id != workspace_id:
            raise RegistryPublicationStoreError(
                RegistryPublicationStoreErrorCode.NOT_FOUND,
                "synthetic publication job is unavailable",
            )
        return job

    def _require_job(self, job_id: str) -> RegistryPublicationJob:
        if self.job is None or self.job.id != job_id:
            raise RegistryPublicationStoreError(
                RegistryPublicationStoreErrorCode.NOT_FOUND,
                "synthetic publication job is unavailable",
            )
        return self.job

    def _record(self, job: RegistryPublicationJob) -> None:
        self.job = job
        if not self.transitions or self.transitions[-1] != job.status.value:
            self.transitions.append(job.status.value)


@dataclass
class _ExactJoinBaseAuthority:
    proposal_store: _JoinProposalStore
    base: GovernedSemanticRegistrySnapshot
    base_registry: OnboardingRegistryBase
    revalidations: int = 0

    def resolve_base(
        self,
        proposal: PreparedRegistryPublicationProposal,
    ) -> GovernedSemanticRegistrySnapshot | None:
        if not isinstance(proposal, PreparedRegistryJoinProposal):
            raise RegistryPublicationAuthorityError(
                RegistryPublicationFailureCode.PROPOSAL_UNAVAILABLE,
                "synthetic authority accepts only one join proposal",
            )
        retained = self.proposal_store.load_exact(proposal.workspace_id, proposal.id)
        self.revalidations += 1
        if (
            retained != proposal
            or proposal.base_registry != self.base_registry
            or self.base.version != self.base_registry.registry_version
            or self.base.fingerprint != self.base_registry.registry_fingerprint
        ):
            raise RegistryPublicationAuthorityError(
                RegistryPublicationFailureCode.BASE_STALE,
                "synthetic active registry authority changed",
            )
        return self.base


@dataclass(slots=True)
class _SyntheticDataHubClient:
    document: DataHubRegistryDocument | None = None
    upserts: list[DataHubRegistryDocumentWrite] = field(default_factory=list)

    def identity(self, target_urn: str) -> DataHubRegistryIdentity:
        del target_urn
        return DataHubRegistryIdentity(
            actor_urn=_WRITER_ACTOR,
            granted_platform_mutation_privileges=frozenset({"manageDocuments"}),
            granted_target_edit_privileges=frozenset(),
        )

    def get_document(self, urn: str) -> DataHubRegistryDocument | None:
        del urn
        return self.document

    def upsert_document(self, document: DataHubRegistryDocumentWrite) -> None:
        self.upserts.append(document)
        self.document = DataHubRegistryDocument(
            urn=f"urn:li:document:{document.document_id}",
            title=document.title,
            text=document.text,
            custom_properties=dict(document.custom_properties),
            related_asset_urns=document.related_asset_urns,
            removed=False,
        )


@dataclass(frozen=True, slots=True)
class _InlineHeartbeatSupervisor:
    jobs: _PublicationJobStore

    def run(
        self,
        *,
        claim: RegistryPublicationJob,
        worker_id: str,
        lease_capability: str,
        lease_duration: timedelta,
        heartbeat_interval: timedelta,
        operation: Callable[[], _T],
    ) -> tuple[_T, RegistryPublicationJob]:
        del worker_id, lease_capability, lease_duration, heartbeat_interval
        result = operation()
        refreshed = self.jobs.job
        if refreshed is None or refreshed.id != claim.id:
            raise RuntimeError("synthetic publication claim disappeared")
        return result, refreshed


@dataclass(frozen=True, slots=True)
class _SafetyProbe:
    name: str
    outcome: str
    mutation_count: int = 0


@dataclass(frozen=True, slots=True)
class _ScenarioRuntime:
    scope: SemanticRegistryScope
    base: GovernedSemanticRegistrySnapshot
    base_registry: OnboardingRegistryBase
    pointer: ActiveRegistryPointer
    target: GovernedExecutionTarget
    request_input: RequestRegistryJoinProfileInput
    clock: _ScenarioClock
    changes: InMemoryRegistryChangeStore
    profile_queue: _ProfileQueue
    bindings: _Bindings
    request_profile: RequestRegistryJoinProfile
    finalize: FinalizeRegistryJoinChangeDraft
    decide: DecideRegistryJoinChange
    prepare_change: PrepareRegistryJoinChangePublication
    proposals: _JoinProposalStore
    publication_jobs: _PublicationJobStore
    datahub: _SyntheticDataHubClient
    submit_publication: SubmitRegistryPublication
    inspect_publication: InspectRegistryPublication
    authorize_publication: AuthorizeRegistryPublication
    publication_worker: RunOneRegistryPublisherWorker
    principals: dict[str, AuthenticatedPrincipal]
    safety_probes: tuple[_SafetyProbe, ...]


@st.cache_resource(show_spinner=False)  # type: ignore[untyped-decorator]
def _runtime_for_token(token: str) -> _ScenarioRuntime:
    if _TOKEN_PATTERN.fullmatch(token) is None:
        raise ValueError("M35 scenario token is invalid")
    scope = _scope()
    base, base_registry, pointer, version = _active_two_model_registry(scope)
    target = _target()
    clock = _ScenarioClock()
    changes = InMemoryRegistryChangeStore()
    profile_queue = _ProfileQueue(changes, clock)
    bindings = _Bindings()
    pointers = _Pointers(pointer)
    versions = _Versions(version)
    targets = _Targets(target)
    policy = RegistryChangeAuthorizationPolicy()
    request_input = RequestRegistryJoinProfileInput(
        change_id=_CHANGE_ID,
        connection_id=_CONNECTION,
        proposal=_join_proposal(base),
        expected_base_registry=base_registry,
        expected_execution_target_fingerprint=target.fingerprint,
    )
    proposals = _JoinProposalStore()
    publication_jobs = _PublicationJobStore(clock)
    datahub = _SyntheticDataHubClient()
    publication_policy = SemanticOnboardingAuthorizationPolicy()
    change_publisher = _principal(
        "m35-change-publisher",
        IdentityRole.PUBLISHER,
        authenticated_at=_NOW,
    )
    publication_authorizer = _principal(
        "m35-publication-authorizer",
        IdentityRole.PUBLISHER,
        authenticated_at=_NOW,
    )
    authority = _ExactJoinBaseAuthority(proposals, base, base_registry)
    observed_publisher = DataHubObservedSemanticRegistryPublisher(
        config=DataHubRegistryWriteConfig(
            server="http://127.0.0.1:1",
            token="synthetic-m35-writer-token",
            actor_urn=_WRITER_ACTOR,
        ),
        client=datahub,
    )
    return _ScenarioRuntime(
        scope=scope,
        base=base,
        base_registry=base_registry,
        pointer=pointer,
        target=target,
        request_input=request_input,
        clock=clock,
        changes=changes,
        profile_queue=profile_queue,
        bindings=bindings,
        request_profile=RequestRegistryJoinProfile(
            changes,
            pointers,
            versions,
            targets,
            bindings,
            profile_queue,
            policy,
            clock,
            scope,
        ),
        finalize=FinalizeRegistryJoinChangeDraft(
            changes,
            pointers,
            versions,
            targets,
            bindings,
            profile_queue,
            policy,
            clock,
        ),
        decide=DecideRegistryJoinChange(
            changes,
            pointers,
            versions,
            targets,
            bindings,
            profile_queue,
            policy,
            clock,
        ),
        prepare_change=PrepareRegistryJoinChangePublication(
            changes,
            pointers,
            versions,
            targets,
            bindings,
            profile_queue,
            policy,
            clock,
        ),
        proposals=proposals,
        publication_jobs=publication_jobs,
        datahub=datahub,
        submit_publication=SubmitRegistryPublication(
            proposals,
            publication_jobs,
            publication_policy,
            clock,
        ),
        inspect_publication=InspectRegistryPublication(
            publication_jobs,
            publication_policy,
            clock,
        ),
        authorize_publication=AuthorizeRegistryPublication(
            publication_jobs,
            publication_policy,
            clock,
        ),
        publication_worker=RunOneRegistryPublisherWorker(
            jobs=publication_jobs,
            authority=authority,
            publisher=observed_publisher,
            clock=clock,
            capability_factory=lambda: _CAPABILITY,
            heartbeat_supervisor=_InlineHeartbeatSupervisor(publication_jobs),
            worker_id="m35-scenario-publisher",
        ),
        principals={
            "owner": _principal("m35-analyst-owner", IdentityRole.ANALYST),
            "steward": _principal("m35-steward-reviewer", IdentityRole.STEWARD),
            "change_publisher": change_publisher,
            "publication_authorizer": publication_authorizer,
        },
        safety_probes=_run_safety_probes(
            scope=scope,
            base=base,
            base_registry=base_registry,
            pointer=pointer,
            version=version,
            target=target,
        ),
    )


def main() -> None:
    """Render the exact profile-to-activation-ready M35 Phase-A journey."""

    st.set_page_config(
        page_title="SchemaBridge · M35 governed join change",
        page_icon="🌉",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    _inject_styles()
    token = os.environ.get(_SCENARIO_TOKEN_ENV, "m35-manual-session")
    try:
        runtime = _runtime_for_token(token)
    except ValueError:
        st.error("m35_scenario_configuration_invalid: El escenario local fue rechazado.")
        st.stop()

    st.title("Cambio gobernado de join · M35")
    st.warning(
        "Escenario sintético local: usa los contratos reales M35/M34, pero no contiene red, "
        "SQL, filas, muestras, credenciales productivas ni autoridad de activación."
    )
    st.caption(
        "Exactitud acotada: el join sólo puede usar los dos mapeos activos y sus bindings "
        "inmutables. La similitud de nombres no concede aprobación."
    )
    _render_invariants(runtime)
    _render_flow(runtime)
    _render_safety_probes(runtime)


def _render_invariants(runtime: _ScenarioRuntime) -> None:
    pointer_col, write_col, source_col = st.columns(3)
    pointer_col.metric(
        "Active registry pointer",
        f"v{runtime.pointer.registry_version} · generation {runtime.pointer.generation}",
    )
    write_col.metric("External writes", str(len(runtime.datahub.upserts)))
    source_col.metric("Source writes", str(runtime.profile_queue.source_write_calls))
    sql_col, rows_col, secrets_col = st.columns(3)
    sql_col.metric("SQL generado", "0")
    rows_col.metric("Filas expuestas", "0")
    secrets_col.metric("Credenciales expuestas", "0")


def _render_flow(runtime: _ScenarioRuntime) -> None:
    authoring = runtime.changes.load_authoring(_WORKSPACE, _CHANGE_ID)
    draft = runtime.changes.load_draft(_WORKSPACE, _CHANGE_ID)
    proposal = runtime.proposals.proposal
    job = runtime.profile_queue.job

    st.divider()
    st.subheader("1 · Perfil relacional gobernado")
    if authoring is None:
        st.info("not_requested · Todavía no existe una solicitud durable.")
        st.caption(
            "Analista m35-analyst-owner solicita un perfil sobre el active v2 exacto. "
            "El caso de uso persiste request, idempotencia y auditoría antes de enqueue."
        )
        if st.button(
            "Persistir y solicitar perfil",
            key="m35-request-profile",
            type="primary",
            use_container_width=True,
        ):
            try:
                runtime.request_profile.execute(
                    runtime.principals["owner"],
                    runtime.request_input,
                    idempotency_key=_REQUEST_KEY,
                )
            except (AuthorizationError, RegistryChangeAuthoringError, ValueError):
                st.error("registry_change_request_rejected")
                return
            st.rerun()
        return

    st.success(
        "profile_request_bound · Solicitud durable antes de enqueue = "
        f"{str(runtime.profile_queue.persisted_before_enqueue).lower()}"
    )
    st.caption(
        f"Request fingerprint {_short(authoring.request.fingerprint)} · "
        f"scan {_short(authoring.request.scan_id)} · external_writes_performed=false"
    )
    if job is None:
        st.error("registry_change_profile_job_unavailable")
        return
    if job.status.value == "requested":
        st.info("requested · El worker de perfil todavía no ha leído el origen sintético.")
        if st.button(
            "Ejecutar perfil agregado read-only",
            key="m35-complete-profile",
            type="primary",
            use_container_width=True,
        ):
            try:
                runtime.profile_queue.complete_current(_safe_profile())
            except ValueError:
                st.error("registry_change_profile_failed")
                return
            st.rerun()
        return

    profile_result = job.result
    if profile_result is None:
        st.error("registry_change_profile_result_unavailable")
        return
    profile = profile_result.profile
    st.success("completed · Evidencia exclusivamente agregada y vigente.")
    profile_cols = st.columns(3)
    profile_cols[0].metric("Source reads", str(runtime.profile_queue.source_read_calls))
    profile_cols[1].metric("Transaction read-only", str(profile.transaction_read_only).lower())
    profile_cols[2].metric("Statement timeout", f"{profile.statement_timeout_ms} ms")
    st.caption(
        "Agregados: cardinalidad/multiplicidad, nulos, inválidos, claves distintas y overlap. "
        "No se retienen claves, valores, filas, SQL, parámetros, DSN ni muestras."
    )

    st.subheader("2 · Draft y decisión steward")
    if draft is None:
        st.info("profile_completed · Falta finalizar el candidato needs_review.")
        if st.button(
            "Finalizar draft needs_review",
            key="m35-finalize",
            type="primary",
            use_container_width=True,
        ):
            try:
                runtime.clock.advance(timedelta(seconds=10))
                runtime.finalize.execute(
                    runtime.principals["owner"],
                    _CHANGE_ID,
                    confirmed_authoring_fingerprint=authoring.fingerprint,
                    idempotency_key=_FINALIZE_KEY,
                )
            except (AuthorizationError, RegistryChangeAuthoringError, ValueError):
                st.error("registry_change_finalize_rejected")
                return
            st.rerun()
        return

    st.markdown(
        f"**Draft:** `{draft.id}` · status **{draft.status.value}** · "
        f"revision {draft.revision} · cardinalidad `{draft.candidate.cardinality.cardinality.value}`"
    )
    st.caption(
        f"Recomendación {draft.candidate.recommendation.value} · "
        f"fanout {draft.candidate.fanout_warning or 'none'} · "
        "señales "
        f"{', '.join(item.signal.value for item in draft.candidate.score_breakdown if item.available)}"
    )
    if draft.decision is None:
        st.warning(
            "needs_review · La evidencia no aprueba nada. Debe decidir el steward "
            "m35-steward-reviewer con rationale no nominal."
        )
        confirmed = st.checkbox(
            "Confirmo cardinalidad, read-only, bindings exactos, evidencia y riesgo de fanout.",
            key="m35-confirm-steward",
        )
        if st.button(
            "Aprobar join como steward",
            key="m35-approve",
            type="primary",
            disabled=not confirmed,
            use_container_width=True,
        ):
            try:
                runtime.clock.advance(timedelta(seconds=10))
                runtime.decide.execute(
                    runtime.principals["steward"],
                    draft.id,
                    action=DecisionAction.APPROVE,
                    expected_revision=draft.revision,
                    confirmed_draft_fingerprint=draft.fingerprint,
                    rationale=(
                        "Aggregate declared-key and uniqueness evidence confirms the reviewed "
                        "many-to-one relationship and its explicit fanout risk."
                    ),
                    idempotency_key=_DECIDE_KEY,
                )
            except (AuthorizationError, RegistryChangeAuthoringError, ValueError):
                st.error("registry_change_decision_rejected")
                return
            st.rerun()
        return

    st.success(f"approved · Decisión append-only {draft.decision.id} por {draft.decision.actor}.")
    st.caption(
        "La aprobación semántica aún no publica. El actor de preparación debe ser distinto "
        "del owner y del steward."
    )
    if proposal is None:
        if st.button(
            "Preparar handoff como publisher separado",
            key="m35-prepare-change",
            type="primary",
            use_container_width=True,
        ):
            try:
                runtime.clock.advance(timedelta(seconds=10))
                prepared = runtime.prepare_change.execute(
                    runtime.principals["change_publisher"],
                    draft.id,
                    expected_revision=draft.revision,
                    confirmed_draft_fingerprint=draft.fingerprint,
                    idempotency_key=_PREPARE_KEY,
                )
                runtime.proposals.record(prepared.proposal)
            except (AuthorizationError, RegistryChangeAuthoringError, ValueError):
                st.error("registry_change_preparation_rejected")
                return
            st.rerun()
        return

    st.success(
        f"{RegistryJoinChangeStatus.READY_FOR_PUBLICATION.value} · "
        f"Propuesta inmutable `{proposal.id}` para v{proposal.target_registry_version}."
    )
    st.caption(
        f"Prepared by {proposal.prepared_by} · fingerprint {_short(proposal.fingerprint)} · "
        "external_writes_performed=false"
    )
    _render_publication(runtime, proposal)


def _render_publication(
    runtime: _ScenarioRuntime,
    proposal: PreparedRegistryJoinProposal,
) -> None:
    st.subheader("3 · Publicación M34 aislada")
    job = runtime.publication_jobs.job
    if job is None:
        st.info("ready_for_publication · Aún no se ha reservado el target v3.")
        if st.button(
            "Reservar publicación M34",
            key="m35-submit-publication",
            type="primary",
            use_container_width=True,
        ):
            try:
                runtime.submit_publication.execute(
                    runtime.principals["change_publisher"],
                    proposal_id=proposal.id,
                    confirmed_proposal_fingerprint=proposal.fingerprint,
                    idempotency_key=_PUBLICATION_KEY,
                )
            except (AuthorizationError, RegistryPublicationError):
                st.error("registry_publication_request_rejected")
                return
            st.rerun()
        return

    try:
        job = runtime.inspect_publication.execute(
            runtime.principals["publication_authorizer"],
            job.id,
        )
    except (AuthorizationError, RegistryPublicationError):
        st.error("registry_publication_resource_unavailable")
        return
    status_cols = st.columns(3)
    status_cols[0].metric("Publication status", job.status.value)
    status_cols[1].metric("Revision", str(job.revision))
    status_cols[2].metric("Worker attempts", str(job.attempts))
    st.markdown("**Durable transitions:** " + " → ".join(runtime.publication_jobs.transitions))

    if job.status is RegistryPublicationJobStatus.QUEUED:
        st.success("queued · Target reservado; external writes = 0 y pointer sigue en v2.")
        if st.button(
            "Ejecutar preparación aislada M34",
            key="m35-run-publication-prepare",
            type="primary",
            use_container_width=True,
        ):
            _run_publication_worker(runtime, "registry_publication_preparation_failed")
        return

    if job.status is RegistryPublicationJobStatus.AWAITING_APPROVAL:
        candidate = job.candidate
        if candidate is None:
            st.error("registry_publication_candidate_unavailable")
            return
        st.success("awaiting_approval · Candidato v3 completo ensamblado y fingerprinted.")
        st.caption(
            f"Candidate {_short(candidate.fingerprint)} · joins "
            f"{len(candidate.registry.join_contracts.contracts)} · external writes = 0"
        )
        confirmed = st.checkbox(
            "Confirmo el candidato completo, target, bindings, decisiones y fingerprint.",
            key="m35-confirm-publication",
        )
        if st.button(
            "Autorizar candidato exacto",
            key="m35-authorize-publication",
            type="primary",
            disabled=not confirmed,
            use_container_width=True,
        ):
            try:
                runtime.authorize_publication.execute(
                    runtime.principals["publication_authorizer"],
                    job.id,
                    expected_revision=job.revision,
                    confirmed_candidate_fingerprint=candidate.fingerprint,
                    confirmation=(
                        RegistryPublicationAuthorizationConfirmation.PUBLISH_EXACT_OBSERVED_REGISTRY_VERSION
                    ),
                )
            except (AuthorizationError, RegistryPublicationError):
                st.error("registry_publication_authorization_rejected")
                return
            st.rerun()
        return

    if job.status is RegistryPublicationJobStatus.APPROVED:
        st.success(
            "approved · Autorización post-assembly por m35-publication-authorizer; "
            "external writes = 0."
        )
        if st.button(
            "Publicar con worker aislado y read-back",
            key="m35-run-publication-publish",
            type="primary",
            use_container_width=True,
        ):
            _run_publication_worker(runtime, "registry_publication_readback_failed")
        return

    if job.status is RegistryPublicationJobStatus.ACTIVATION_READY:
        receipt = job.receipt
        if receipt is None:
            st.error("registry_publication_readback_unavailable")
            return
        st.success(
            "activation_ready · Documento v3 inmutable publicado y leído de vuelta exactamente."
        )
        st.caption(
            f"Observed approval {receipt.observed_authorization_id} · registry "
            f"{_short(receipt.registry_fingerprint)} · external writes = 1"
        )
        st.warning(
            "No automatic activation · Active registry pointer permanece en v2 / generation 4. "
            "M23 exige otra aprobación y compare-and-swap separado."
        )
        return

    st.error(f"registry_publication_terminal_state: {job.status.value}")


def _render_safety_probes(runtime: _ScenarioRuntime) -> None:
    st.divider()
    st.subheader("4 · Intentos adversariales fail-closed")
    st.caption(
        "Probes aisladas sobre los contratos reales. Ninguna crea draft/propuesta, escribe "
        "DataHub, cambia el pointer ni ejecuta SQL."
    )
    for probe in runtime.safety_probes:
        st.success(f"{probe.name} · {probe.outcome} · mutaciones externas {probe.mutation_count}")

    audit = runtime.changes.list_audit(_WORKSPACE, _CHANGE_ID)
    if audit:
        st.caption("Audit append-only: " + " → ".join(item.event.value for item in audit))
        if audit[-1].event is RegistryChangeAuditEvent.PUBLICATION_PREPARED:
            st.caption("El handoff M35 termina en ready_for_publication; M34 mantiene su ledger.")


def _run_publication_worker(runtime: _ScenarioRuntime, error_code: str) -> None:
    try:
        runtime.publication_worker.execute()
    except RegistryPublisherWorkerError:
        st.error(f"{error_code}: El worker aislado no completó la iteración.")
        return
    st.rerun()


def _run_safety_probes(
    *,
    scope: SemanticRegistryScope,
    base: GovernedSemanticRegistrySnapshot,
    base_registry: OnboardingRegistryBase,
    pointer: ActiveRegistryPointer,
    version: GovernedRegistryVersion,
    target: GovernedExecutionTarget,
) -> tuple[_SafetyProbe, ...]:
    safe = _join_proposal(base)
    probes: list[_SafetyProbe] = []

    try:
        JoinProposal(
            id="self_join_probe",
            left_key=safe.left_key,
            right_key=safe.left_key,
        )
    except ValueError:
        probes.append(_SafetyProbe("self_join", "rejected_before_profile"))
    else:
        raise RuntimeError("self join probe did not fail closed")

    from schemabridge.domain.semantic_profile_jobs import SemanticJoinProfileProposal

    cross = SemanticJoinProfileProposal(
        connection_id=CatalogConnectionId("warehouse-other"),
        proposal=safe,
    )
    try:
        resolve_registry_join_base_evidence(
            scope=scope,
            base=base,
            base_registry=base_registry,
            proposal=cross,
        )
    except ValueError:
        probes.append(_SafetyProbe("cross_connection", "rejected_before_profile"))
    else:
        raise RuntimeError("cross connection probe did not fail closed")

    isolated_store = InMemoryRegistryChangeStore()
    isolated_clock = _ScenarioClock()
    isolated_queue = _ProfileQueue(isolated_store, isolated_clock)
    stale_use_case = RequestRegistryJoinProfile(
        isolated_store,
        _Pointers(pointer),
        _Versions(version),
        _Targets(target),
        _Bindings(),
        isolated_queue,
        RegistryChangeAuthorizationPolicy(),
        isolated_clock,
        scope,
    )
    stale_request = RequestRegistryJoinProfileInput(
        change_id="join-change-stale-target",
        connection_id=_CONNECTION,
        proposal=safe,
        expected_base_registry=base_registry,
        expected_execution_target_fingerprint="f" * 64,
    )
    try:
        stale_use_case.execute(
            _principal("m35-probe-analyst", IdentityRole.ANALYST),
            stale_request,
            idempotency_key="m35-stale-target-probe-0001",
        )
    except RegistryChangeAuthoringError as error:
        if error.code is not RegistryChangeAuthoringErrorCode.STALE_TARGET:
            raise
        if isolated_queue.enqueue_calls != 0:
            raise RuntimeError("stale target probe reached the profile queue") from error
        probes.append(_SafetyProbe("stale_target", error.code.value))
    else:
        raise RuntimeError("stale target probe did not fail closed")

    many_job = _completed_profile_job(
        scope=scope,
        base=base,
        base_registry=base_registry,
        target=target,
        profile=_many_to_many_profile(),
    )
    many_draft = RegistryJoinChangeDraft.create(
        id="join-change-many-to-many",
        workspace_id=_WORKSPACE,
        owner_actor_id="m35-probe-analyst",
        scope=scope,
        base=base,
        base_registry=base_registry,
        profile_job=many_job,
        created_at=_NOW + timedelta(minutes=2),
    )
    try:
        decide_registry_join_change(
            many_draft,
            action=DecisionAction.APPROVE,
            expected_revision=many_draft.revision,
            actor_id="m35-probe-steward",
            decided_at=_NOW + timedelta(minutes=3),
            rationale="The unsafe many-to-many relationship must remain non executable.",
        )
    except ValueError:
        probes.append(_SafetyProbe("many_to_many", "approval_rejected"))
    else:
        raise RuntimeError("many-to-many probe did not fail closed")
    return tuple(probes)


def _completed_profile_job(
    *,
    scope: SemanticRegistryScope,
    base: GovernedSemanticRegistrySnapshot,
    base_registry: OnboardingRegistryBase,
    target: GovernedExecutionTarget,
    profile: RelationshipProfile,
) -> SemanticJoinProfileJob:
    from schemabridge.domain.semantic_profile_jobs import SemanticJoinProfileProposal

    proposal = SemanticJoinProfileProposal(
        connection_id=_CONNECTION,
        proposal=_join_proposal(base),
    )
    evidence = resolve_registry_join_base_evidence(
        scope=scope,
        base=base,
        base_registry=base_registry,
        proposal=proposal,
    )
    target_ref = SemanticJoinProfileTargetRef.from_target(target)
    request = RegistryJoinProfileRequest.create(
        scope=scope,
        base_evidence=evidence,
        proposal=proposal,
        execution_target=target_ref,
        requested_at=_NOW,
    )
    requested = SemanticJoinProfileJob.requested(
        workspace_id=_WORKSPACE,
        scan_id=request.scan_id,
        proposal=proposal,
        execution_target=target_ref,
        requested_at=_NOW,
        connector_contract_version=target_ref.route_revision,
    )
    leased = claim_semantic_join_profile_job(
        requested,
        worker_id="m35-probe-profile-worker",
        lease_capability="m35-probe-capability-0123456789abcdef",
        claimed_at=_NOW + timedelta(seconds=10),
        lease_expires_at=_NOW + timedelta(minutes=3),
    )
    return complete_semantic_join_profile_job(
        leased,
        worker_id="m35-probe-profile-worker",
        lease_capability="m35-probe-capability-0123456789abcdef",
        fencing_token=leased.fencing_token,
        profile=profile,
        completed_at=_NOW + timedelta(minutes=1),
        retain_until=_NOW + timedelta(days=30),
    )


def _scope() -> SemanticRegistryScope:
    return SemanticRegistryScope(
        workspace_id=_WORKSPACE,
        catalog_scope="postgres.synthetic_acceptance",
        registry_id="commerce_registry",
    )


def _active_two_model_registry(
    scope: SemanticRegistryScope,
) -> tuple[
    GovernedSemanticRegistrySnapshot,
    OnboardingRegistryBase,
    ActiveRegistryPointer,
    GovernedRegistryVersion,
]:
    first = assemble_publishable_registry_version(
        _onboarding_proposal(
            scope=scope,
            model_id="Order",
            logical_field="Order.customer_id",
            physical_field="commerce.orders.customer_id",
            proposal_id="proposal-orders-v1",
            draft_id="orders-onboarding",
            model_decision="decision-model-order",
            mapping_decision="decision-mapping-order-customer",
            observed_urn=_OPAQUE_ORDERS_URN,
            base=OnboardingRegistryBase(),
        ),
        base=None,
    ).registry
    second_base = OnboardingRegistryBase(
        registry_version=first.version,
        registry_fingerprint=first.fingerprint,
        activation_generation=3,
        active_pointer_fingerprint="d" * 64,
    )
    second = assemble_publishable_registry_version(
        _onboarding_proposal(
            scope=scope,
            model_id="Customer",
            logical_field="Customer.customer_id",
            physical_field="crm.customers.customer_id",
            proposal_id="proposal-customers-v2",
            draft_id="customers-onboarding",
            model_decision="decision-model-customer",
            mapping_decision="decision-mapping-customer",
            observed_urn=_OPAQUE_CUSTOMERS_URN,
            base=second_base,
        ),
        base=first,
    ).registry
    provisional_pointer = ActiveRegistryPointer(
        scope=scope,
        generation=4,
        registry_version=second.version,
        registry_fingerprint=second.fingerprint,
        registry_target=datahub_registry_document_urn(scope, second.version),
        transition_id="transition-active-v2",
        activated_by="m35-activation-admin",
        activated_at=_NOW - timedelta(days=1),
        decision_ids=("decision-active-v2",),
    )
    pointer_fingerprint = registry_projection_fingerprint(provisional_pointer)
    base_registry = OnboardingRegistryBase(
        registry_version=second.version,
        registry_fingerprint=second.fingerprint,
        activation_generation=provisional_pointer.generation,
        active_pointer_fingerprint=pointer_fingerprint,
    )
    version = GovernedRegistryVersion(
        snapshot=ScopedSemanticRegistrySnapshot(
            scope=scope,
            registry=second,
            activation_generation=provisional_pointer.generation,
            active_pointer_fingerprint=pointer_fingerprint,
        ),
        publication_approval_id="publication-approval-v2",
        trust=RegistryVersionTrust.STRICT,
    )
    return second, base_registry, provisional_pointer, version


def _onboarding_proposal(
    *,
    scope: SemanticRegistryScope,
    model_id: str,
    logical_field: str,
    physical_field: str,
    proposal_id: str,
    draft_id: str,
    model_decision: str,
    mapping_decision: str,
    observed_urn: str,
    base: OnboardingRegistryBase,
) -> PreparedSemanticOnboardingProposal:
    definition = SemanticModelDefinition(
        id=LogicalModelRef(model_id),
        description=f"Governed synthetic {model_id} model.",
        fields=(
            SemanticFieldDefinition(
                id=LogicalFieldRef(logical_field),
                canonical_type=CanonicalType.STRING,
                role=LogicalFieldRole.IDENTIFIER,
                definition=f"Stable governed {model_id} customer identifier.",
            ),
        ),
    )
    column = physical_field.rsplit(".", 1)[-1]
    mapping = SemanticMappingProposal(
        id=f"mapping-{model_id.lower()}-customer-id",
        logical_field=LogicalFieldRef(logical_field),
        observation=PhysicalCatalogObservation(
            locator=CatalogFieldLocator(
                asset=CatalogAssetLocator(
                    workspace_id=_WORKSPACE,
                    connection_id=_CONNECTION,
                    asset_id=CatalogAssetId(observed_urn),
                ),
                field_path=(column,),
            ),
            catalog_scope=scope.catalog_scope,
            generation=7,
            generation_fingerprint="a" * 64,
            asset_metadata_fingerprint="b" * 64,
            field_metadata_fingerprint=("c" if model_id == "Order" else "e") * 64,
            physical_field=PhysicalFieldRef(physical_field),
            physical_type=PhysicalValueType.STRING,
            observed_datahub_asset_urn=observed_urn,
        ),
        confidence=ConfidenceScore(0.98),
        evidence=(
            OnboardingEvidence(
                kind=OnboardingEvidenceKind.DECLARED_KEY,
                detail="Retained catalog declares this exact synthetic field as a key.",
                reference="catalog:synthetic-generation-7",
            ),
        ),
        risks=("Identifier ownership was explicitly reviewed by the steward.",),
        transformation_plan=TransformationPlan(steps=(IdentityStep(),)),
        status=ApprovalStatus.APPROVED,
        decision_id=mapping_decision,
        decided_by="m35-base-steward",
    )
    draft = SemanticOnboardingDraft(
        id=draft_id,
        workspace_id=_WORKSPACE,
        owner_actor_id="m35-base-analyst",
        scope=scope,
        connection_id=_CONNECTION,
        catalog_generation=7,
        catalog_generation_fingerprint="a" * 64,
        base_registry=base,
        model=SemanticModelProposal(
            definition=definition,
            status=ApprovalStatus.APPROVED,
            decision_id=model_decision,
            decided_by="m35-base-steward",
        ),
        mappings=(mapping,),
        created_at=_NOW - timedelta(days=2),
        updated_at=_NOW - timedelta(days=2),
    )
    return PreparedSemanticOnboardingProposal.create(
        id=proposal_id,
        draft=draft,
        decision_ids=tuple(sorted((mapping_decision, model_decision))),
        prepared_by="m35-base-publisher",
        prepared_at=_NOW - timedelta(days=2),
    )


def _join_proposal(base: GovernedSemanticRegistrySnapshot) -> JoinProposal:
    mappings = {item.mapping.logical_field.root: item.mapping for item in base.mapping_set.mappings}
    left = mappings["Order.customer_id"]
    right = mappings["Customer.customer_id"]
    return JoinProposal(
        id="order_customer",
        left_key=NormalizedJoinKey(
            logical_field=left.logical_field,
            physical_field=left.physical_field,
            transformation_plan=left.transformation_plan,
        ),
        right_key=NormalizedJoinKey(
            logical_field=right.logical_field,
            physical_field=right.physical_field,
            transformation_plan=right.transformation_plan,
        ),
    )


def _safe_profile() -> RelationshipProfile:
    return RelationshipProfile(
        left_row_count=120,
        right_row_count=80,
        left_null_count=0,
        right_null_count=0,
        left_invalid_count=0,
        right_invalid_count=0,
        left_distinct_valid=80,
        right_distinct_valid=80,
        matching_distinct_keys=80,
        left_max_multiplicity=3,
        right_max_multiplicity=1,
        declared_relationship=DeclaredRelationship.LEFT_FOREIGN_KEY_TO_RIGHT,
        reader_user="schemabridge_reader",
        transaction_read_only=True,
        statement_timeout_ms=2_000,
    )


def _many_to_many_profile() -> RelationshipProfile:
    return RelationshipProfile(
        left_row_count=120,
        right_row_count=100,
        left_null_count=0,
        right_null_count=0,
        left_invalid_count=0,
        right_invalid_count=0,
        left_distinct_valid=80,
        right_distinct_valid=70,
        matching_distinct_keys=60,
        left_max_multiplicity=3,
        right_max_multiplicity=2,
        declared_relationship=DeclaredRelationship.NONE,
        reader_user="schemabridge_reader",
        transaction_read_only=True,
        statement_timeout_ms=2_000,
    )


def _target() -> GovernedExecutionTarget:
    budget = QueryCostBudget(
        explain_timeout_ms=1_000,
        max_response_bytes=65_536,
        max_total_cost=Decimal("10000"),
        max_estimated_rows=100_000,
        max_plan_nodes=1_000,
        max_plan_depth=32,
        max_plan_width=16_384,
    )
    return GovernedExecutionTarget(
        workspace_id=_WORKSPACE,
        connection_id=_CONNECTION,
        connector_kind=SourceConnectorKind.POSTGRESQL,
        dialect=SourceDialect.POSTGRESQL,
        route_revision=1,
        route_fingerprint="a" * 64,
        expected_reader="schemabridge_reader",
        source_identity_fingerprint="b" * 64,
        catalog_identity_fingerprint="c" * 64,
        type_contract_fingerprint=postgres_type_contract_fingerprint(),
        cost_budget=budget,
        cost_budget_fingerprint=budget.fingerprint,
    )


def _principal(
    actor_id: str,
    role: IdentityRole,
    *,
    authenticated_at: datetime = _NOW - timedelta(minutes=1),
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        actor_id=actor_id,
        workspace_id=_WORKSPACE,
        roles=frozenset({role}),
        authentication_method=AuthenticationMethod.OIDC,
        authenticated_at=authenticated_at,
        expires_at=_NOW + timedelta(hours=1),
    )


def _state_conflict() -> RegistryPublicationStoreError:
    return RegistryPublicationStoreError(
        RegistryPublicationStoreErrorCode.STATE_CONFLICT,
        "synthetic publication state changed",
    )


def _short(value: str) -> str:
    return f"{value[:12]}…{value[-8:]}"


def _inject_styles() -> None:
    st.markdown(
        """
        <style>
        .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
            max-width: 100%; overflow-x: hidden;
        }
        code, p, span, div { overflow-wrap: anywhere; }
        button, input, textarea, [role="checkbox"] { min-height: 44px; }
        @media (max-width: 640px) {
            [data-testid="stHorizontalBlock"] { flex-wrap: wrap; }
            [data-testid="column"] {
                min-width: 0 !important; width: 100% !important; flex: 1 1 100% !important;
            }
            .block-container { padding-left: 1rem; padding-right: 1rem; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
