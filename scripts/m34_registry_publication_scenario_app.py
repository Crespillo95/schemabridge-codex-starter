#!/usr/bin/env python3
"""Isolated browser scenario for the governed M34 publication journey.

The scenario composes the real M34 API use cases, worker, immutable-v2 assembler and
DataHub publication adapter with in-memory control-plane and synthetic DataHub clients.
It has no source-database, LLM, SQL, network, activation or production-secret adapter.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final, TypeVar

import streamlit as st

from schemabridge.adapters.semantic_registry.datahub import (
    DataHubObservedSemanticRegistryPublisher,
    DataHubRegistryDocument,
    DataHubRegistryDocumentWrite,
    DataHubRegistryIdentity,
    DataHubRegistryWriteConfig,
)
from schemabridge.application.authorization import AuthorizationError
from schemabridge.application.ports.registry_publication import (
    RegistryPublicationAuthorityError,
    RegistryPublicationJobMutation,
    RegistryPublicationStoreError,
    RegistryPublicationStoreErrorCode,
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
from schemabridge.domain.decisions import ApprovalStatus
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)
from schemabridge.domain.mappings import ConfidenceScore
from schemabridge.domain.physical_types import PhysicalValueType
from schemabridge.domain.registry_publication import (
    PublicationReadbackReceipt,
    PublishableRegistryVersion,
    RegistryPublicationAuthorization,
    RegistryPublicationAuthorizationConfirmation,
)
from schemabridge.domain.registry_publication_jobs import (
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
from schemabridge.domain.semantic_registry import (
    GovernedSemanticRegistrySnapshot,
    SemanticRegistryScope,
)
from schemabridge.domain.transformations import IdentityStep, TransformationPlan

_SCENARIO_TOKEN_ENV: Final = "SCHEMABRIDGE_M34_SCENARIO_TOKEN"
_TOKEN_PATTERN: Final = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_NOW: Final = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
_WORKSPACE: Final = "m34-synthetic-tenant"
_OPAQUE_ASSET_URN: Final = "urn:li:dataset:(urn:li:dataPlatform:postgres,opaque-9f82,PROD)"
_WRITER_ACTOR: Final = "urn:li:corpuser:schemabridge-registry-publisher"
_CAPABILITY: Final = "m34-synthetic-lease-capability-" + ("x" * 48)
_T = TypeVar("_T")


@dataclass
class _ScenarioClock:
    value: datetime = _NOW

    def now(self) -> datetime:
        return self.value

    def tick(self) -> datetime:
        self.value += timedelta(seconds=1)
        return self.value


@dataclass(frozen=True, slots=True)
class _ProposalStore:
    proposal: PreparedSemanticOnboardingProposal

    def load_exact(
        self,
        workspace_id: str,
        proposal_id: str,
    ) -> PreparedSemanticOnboardingProposal | None:
        if workspace_id == self.proposal.workspace_id and proposal_id == self.proposal.id:
            return self.proposal
        return None


class _ScenarioJobStore:
    """One-job deterministic store that applies the real pure queue transitions."""

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
            "synthetic target is already reserved",
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
            authorized_at=self.clock.tick(),
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
            requested_at=self.clock.tick(),
        )
        self._record(updated)
        return updated

    def reap_expired_leases(self, *, limit: int = 100) -> int:
        job = self.job
        if limit < 1 or job is None or job.lease is None:
            return 0
        if job.lease.expires_at > self.clock.now():
            return 0
        updated = reap_expired_registry_publication_lease(
            job,
            expired_at=self.clock.now(),
        )
        self._record(updated)
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
            acquired_at=self.clock.tick(),
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
        job = self._require_job(job_id)
        updated = heartbeat_registry_publication_job(
            job,
            worker_id=worker_id,
            lease_capability=lease_capability,
            fencing_token=fencing_token,
            heartbeat_at=self.clock.tick(),
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
            completed_at=self.clock.tick(),
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
            completed_at=self.clock.tick(),
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
            failed_at=self.clock.tick(),
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
            cancelled_at=self.clock.tick(),
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
        status = job.status.value
        if not self.transitions or self.transitions[-1] != status:
            self.transitions.append(status)


@dataclass(frozen=True, slots=True)
class _ExactEmptyBaseAuthority:
    proposal: PreparedSemanticOnboardingProposal

    def resolve_base(
        self,
        proposal: PreparedSemanticOnboardingProposal,
    ) -> GovernedSemanticRegistrySnapshot | None:
        if proposal != self.proposal or any(
            mapping.observation.observed_datahub_asset_urn != _OPAQUE_ASSET_URN
            for mapping in proposal.mappings
        ):
            raise RegistryPublicationAuthorityError(
                RegistryPublicationFailureCode.CATALOG_STALE,
                "synthetic retained catalog authority changed",
            )
        return None


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
    jobs: _ScenarioJobStore

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
class _ScenarioRuntime:
    proposal: PreparedSemanticOnboardingProposal
    principal: AuthenticatedPrincipal
    jobs: _ScenarioJobStore
    datahub: _SyntheticDataHubClient
    submit: SubmitRegistryPublication
    inspect: InspectRegistryPublication
    authorize: AuthorizeRegistryPublication
    worker: RunOneRegistryPublisherWorker


@st.cache_resource(show_spinner=False)  # type: ignore[untyped-decorator]
def _runtime_for_token(token: str) -> _ScenarioRuntime:
    if _TOKEN_PATTERN.fullmatch(token) is None:
        raise ValueError("M34 scenario token is invalid")
    clock = _ScenarioClock()
    proposal = _proposal()
    jobs = _ScenarioJobStore(clock)
    datahub = _SyntheticDataHubClient()
    policy = SemanticOnboardingAuthorizationPolicy()
    principal = AuthenticatedPrincipal(
        actor_id="m34-independent-publisher",
        workspace_id=_WORKSPACE,
        roles=frozenset({IdentityRole.PUBLISHER}),
        authentication_method=AuthenticationMethod.OIDC,
        authenticated_at=_NOW,
        expires_at=_NOW + timedelta(minutes=30),
    )
    publisher = DataHubObservedSemanticRegistryPublisher(
        config=DataHubRegistryWriteConfig(
            server="http://127.0.0.1:1",
            token="synthetic-m34-writer-token",
            actor_urn=_WRITER_ACTOR,
        ),
        client=datahub,
    )
    return _ScenarioRuntime(
        proposal=proposal,
        principal=principal,
        jobs=jobs,
        datahub=datahub,
        submit=SubmitRegistryPublication(_ProposalStore(proposal), jobs, policy, clock),
        inspect=InspectRegistryPublication(jobs, policy, clock),
        authorize=AuthorizeRegistryPublication(jobs, policy, clock),
        worker=RunOneRegistryPublisherWorker(
            jobs=jobs,
            authority=_ExactEmptyBaseAuthority(proposal),
            publisher=publisher,
            clock=clock,
            capability_factory=lambda: _CAPABILITY,
            heartbeat_supervisor=_InlineHeartbeatSupervisor(jobs),
            worker_id="m34-scenario-publisher",
        ),
    )


def main() -> None:
    """Render queued to activation-ready without changing an active pointer."""

    st.set_page_config(
        page_title="SchemaBridge · M34 governed publication",
        page_icon="🌉",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    token = os.environ.get(_SCENARIO_TOKEN_ENV, "m34-manual-session")
    try:
        runtime = _runtime_for_token(token)
    except ValueError:
        st.error("m34_scenario_configuration_invalid: El escenario local fue rechazado.")
        st.stop()

    st.title("Publicación gobernada del registro")
    st.warning(
        "Escenario sintético local M34: usa los contratos reales de aplicación y DataHub v2, "
        "pero no contiene red, secretos productivos, origen, LLM, SQL ni activación."
    )
    st.caption(
        "La propuesta M33 ya fue revisada. Esta sesión publisher es independiente y reciente; "
        "la aprobación se solicita sólo después de conocer el candidato completo."
    )
    _render_invariants(runtime)

    job = runtime.jobs.job
    if job is None:
        _render_ready_proposal(runtime)
        return
    try:
        job = runtime.inspect.execute(runtime.principal, job.id)
    except (AuthorizationError, RegistryPublicationError):
        st.error("registry_publication_resource_unavailable: El recurso no está disponible.")
        return
    _render_job(runtime, job)


def _render_invariants(runtime: _ScenarioRuntime) -> None:
    pointer_col, write_col, document_col = st.columns(3)
    pointer_col.metric("Active registry pointer", "not_configured")
    write_col.metric("External writes", str(len(runtime.datahub.upserts)))
    document_col.metric(
        "Immutable DataHub versions",
        "1" if runtime.datahub.document is not None else "0",
    )


def _render_ready_proposal(runtime: _ScenarioRuntime) -> None:
    proposal = runtime.proposal
    st.info("ready_for_publication · Handoff M33 inmutable disponible.")
    st.markdown(
        f"**Propuesta:** `{proposal.id}` · versión objetivo `{proposal.target_registry_version}`"
    )
    st.caption(
        f"Fingerprint {_short(proposal.fingerprint)} · workspace {proposal.workspace_id} · "
        "base vacía explícita · external_writes_performed=false"
    )
    st.caption("Reservar crea sólo el job durable. No escribe DataHub ni cambia el active pointer.")
    if st.button("Reservar publicación", key="m34-submit", type="primary"):
        try:
            runtime.submit.execute(
                runtime.principal,
                proposal_id=proposal.id,
                confirmed_proposal_fingerprint=proposal.fingerprint,
                idempotency_key="m34-synthetic-publication-0001",
            )
        except (AuthorizationError, RegistryPublicationError):
            st.error("registry_publication_request_rejected: La reserva fue rechazada.")
            return
        st.rerun()


def _render_job(runtime: _ScenarioRuntime, job: RegistryPublicationJob) -> None:
    st.divider()
    status_col, revision_col, attempt_col = st.columns(3)
    status_col.metric("Publication status", job.status.value)
    revision_col.metric("Revision", str(job.revision))
    attempt_col.metric("Worker attempts", str(job.attempts))
    st.caption(
        f"Job {job.id} · target version {job.proposal.target_registry_version} · "
        f"scope {job.scope.catalog_scope}"
    )
    st.markdown("**Durable transitions:** " + " → ".join(runtime.jobs.transitions))

    if job.status is RegistryPublicationJobStatus.QUEUED:
        st.success("queued · Target reservado; DataHub y active pointer siguen intactos.")
        if st.button(
            "Ejecutar preparación aislada",
            key="m34-run-prepare",
            type="primary",
        ):
            _run_worker(runtime, "registry_publication_preparation_failed")
        return

    if job.status is RegistryPublicationJobStatus.AWAITING_APPROVAL:
        _render_candidate(runtime, job)
        return

    if job.status is RegistryPublicationJobStatus.APPROVED:
        st.success("approved · Candidato exacto autorizado por una sesión publisher reciente.")
        st.caption(
            "El worker volverá a resolver la autoridad y sólo aceptará éxito tras read-back "
            "exacto del documento, aprobación, auditoría y relatedAssets."
        )
        if st.button(
            "Publicar y verificar read-back",
            key="m34-run-publish",
            type="primary",
        ):
            _run_worker(runtime, "registry_publication_readback_failed")
        return

    if job.status is RegistryPublicationJobStatus.ACTIVATION_READY:
        _render_activation_ready(job)
        return

    st.error(f"registry_publication_terminal_state: {job.status.value}")


def _render_candidate(runtime: _ScenarioRuntime, job: RegistryPublicationJob) -> None:
    candidate = job.candidate
    if candidate is None:
        st.error("registry_publication_candidate_unavailable")
        return
    observed_urns = tuple(
        binding.observed_datahub_asset_urn for binding in candidate.registry.physical_bindings
    )
    st.success("awaiting_approval · Candidato v2 completo y fingerprinted.")
    st.markdown(
        f"**Target inmutable:** `{candidate.target}`  \n"
        f"**Candidate fingerprint:** `{candidate.fingerprint}`"
    )
    st.caption("relatedAssets retenidos exactamente: " + ", ".join(observed_urns))
    st.caption("No se derivó ninguna URN desde `sales.orders`; DataHub todavía tiene 0 versiones.")
    confirmed = st.checkbox(
        "He recargado y confirmado el target, el fingerprint, los bindings y decisiones.",
        key="m34-confirm-candidate",
    )
    if st.button(
        "Autorizar candidato exacto",
        key="m34-authorize",
        type="primary",
        disabled=not confirmed,
    ):
        try:
            runtime.authorize.execute(
                runtime.principal,
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


def _render_activation_ready(job: RegistryPublicationJob) -> None:
    receipt = job.receipt
    if receipt is None:
        st.error("registry_publication_readback_unavailable")
        return
    st.success("activation_ready · Documento v2 inmutable publicado y leído de vuelta exactamente.")
    st.markdown(
        f"**Observed approval:** `{receipt.observed_authorization_id}`  \n"
        f"**Registry fingerprint:** `{receipt.registry_fingerprint}`"
    )
    st.caption("relatedAssets verificados: " + ", ".join(receipt.related_asset_urns))
    st.warning(
        "No automatic activation · Active registry pointer = not_configured. "
        "La activación exige el flujo M23 separado con otro approval y CAS."
    )


def _run_worker(runtime: _ScenarioRuntime, error_code: str) -> None:
    try:
        runtime.worker.execute()
    except RegistryPublisherWorkerError:
        st.error(f"{error_code}: El worker aislado no pudo completar la iteración.")
        return
    st.rerun()


def _proposal() -> PreparedSemanticOnboardingProposal:
    scope = SemanticRegistryScope(
        workspace_id=_WORKSPACE,
        catalog_scope="postgres.synthetic_acceptance",
        registry_id="commerce_registry",
    )
    definition = SemanticModelDefinition(
        id=LogicalModelRef("Order"),
        description="Governed business order recorded by the commerce platform.",
        fields=(
            SemanticFieldDefinition(
                id=LogicalFieldRef("Order.order_id"),
                canonical_type=CanonicalType.STRING,
                role=LogicalFieldRole.IDENTIFIER,
                definition="Stable governed identifier for one business order.",
            ),
        ),
    )
    mapping = SemanticMappingProposal(
        id="mapping-order-id",
        logical_field=LogicalFieldRef("Order.order_id"),
        observation=PhysicalCatalogObservation(
            locator=CatalogFieldLocator(
                asset=CatalogAssetLocator(
                    workspace_id=_WORKSPACE,
                    connection_id=CatalogConnectionId("warehouse-commerce"),
                    asset_id=CatalogAssetId(_OPAQUE_ASSET_URN),
                ),
                field_path=("order_id",),
            ),
            catalog_scope=scope.catalog_scope,
            generation=7,
            generation_fingerprint="a" * 64,
            asset_metadata_fingerprint="b" * 64,
            field_metadata_fingerprint="c" * 64,
            physical_field=PhysicalFieldRef("sales.orders.order_id"),
            physical_type=PhysicalValueType.STRING,
            observed_datahub_asset_urn=_OPAQUE_ASSET_URN,
        ),
        confidence=ConfidenceScore(0.98),
        evidence=(
            OnboardingEvidence(
                kind=OnboardingEvidenceKind.DECLARED_KEY,
                detail="The retained catalog declares this exact field as a stable key.",
                reference="catalog:synthetic-generation-7",
            ),
        ),
        risks=("Identifier ownership was explicitly reviewed by the steward.",),
        transformation_plan=TransformationPlan(steps=(IdentityStep(),)),
        status=ApprovalStatus.APPROVED,
        decision_id="decision-mapping-order",
        decided_by="m34-steward",
    )
    draft = SemanticOnboardingDraft(
        id="commerce-orders-onboarding",
        workspace_id=_WORKSPACE,
        owner_actor_id="m34-analyst",
        scope=scope,
        connection_id=CatalogConnectionId("warehouse-commerce"),
        catalog_generation=7,
        catalog_generation_fingerprint="a" * 64,
        base_registry=OnboardingRegistryBase(),
        model=SemanticModelProposal(
            definition=definition,
            status=ApprovalStatus.APPROVED,
            decision_id="decision-model-order",
            decided_by="m34-steward",
        ),
        mappings=(mapping,),
        created_at=_NOW,
        updated_at=_NOW,
    )
    return PreparedSemanticOnboardingProposal.create(
        id="proposal-commerce-orders-v1",
        draft=draft,
        decision_ids=("decision-mapping-order", "decision-model-order"),
        prepared_by="m34-proposal-publisher",
        prepared_at=_NOW,
    )


def _state_conflict() -> RegistryPublicationStoreError:
    return RegistryPublicationStoreError(
        RegistryPublicationStoreErrorCode.STATE_CONFLICT,
        "synthetic publication state changed",
    )


def _short(value: str) -> str:
    return f"{value[:12]}…{value[-8:]}"


if __name__ == "__main__":
    main()
