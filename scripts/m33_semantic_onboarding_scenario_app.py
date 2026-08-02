#!/usr/bin/env python3
"""Isolated browser scenario for the governed M33 onboarding journey.

The process composes the real M33 application use cases with an in-memory store and an exact
synthetic catalog.  It has no source, DataHub, LLM, compiler, executor, or activation adapter.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

import streamlit as st

from schemabridge.adapters.storage.semantic_onboarding import (
    InMemorySemanticOnboardingStore,
)
from schemabridge.application.authorization import AuthorizationError
from schemabridge.application.ports.semantic_onboarding import (
    SemanticOnboardingPortError,
    SemanticOnboardingPortErrorCode,
)
from schemabridge.application.semantic_onboarding import (
    CreateSemanticOnboardingDraft,
    DecideSemanticOnboarding,
    InspectSemanticOnboardingDraft,
    ListSemanticOnboardingDrafts,
    PreflightSemanticOnboardingDraft,
    PrepareSemanticOnboardingPublication,
    SemanticOnboardingError,
    SemanticOnboardingSnapshot,
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
from schemabridge.domain.decisions import ApprovalStatus, DecisionAction
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)
from schemabridge.domain.mappings import ConfidenceScore
from schemabridge.domain.request_context import LogicalFieldRole
from schemabridge.domain.semantic_onboarding import (
    CreateSemanticOnboardingRequest,
    OnboardingCatalogGeneration,
    OnboardingEvidence,
    OnboardingEvidenceKind,
    OnboardingRegistryBase,
    PhysicalCatalogObservation,
    PreflightSemanticOnboardingRequest,
    ResolvedOnboardingCatalogEvidence,
    SemanticFieldDefinition,
    SemanticModelDefinition,
    SemanticOnboardingAuditRecord,
    SemanticOnboardingDraft,
    SemanticOnboardingMappingInput,
    SemanticOnboardingPermission,
    SemanticOnboardingPreflight,
    SemanticOnboardingPreflightSelection,
    SemanticOnboardingStatus,
    SemanticOnboardingTargetKind,
)
from schemabridge.domain.semantic_registry import PhysicalValueType, SemanticRegistryScope
from schemabridge.domain.transformations import IdentityStep, TransformationPlan

_SCENARIO_TOKEN_ENV: Final = "SCHEMABRIDGE_M33_SCENARIO_TOKEN"
_TOKEN_PATTERN: Final = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_NOW: Final = datetime(2026, 8, 2, 10, 0, tzinfo=UTC)
_WORKSPACE: Final = "m33-synthetic-tenant"
_GENERATION_FINGERPRINT: Final = "a" * 64
_ASSET_FINGERPRINT: Final = "b" * 64
_FIELD_FINGERPRINTS: Final = ("c" * 64, "d" * 64, "e" * 64)
_ROLE_LABELS: Final = {
    IdentityRole.ANALYST.value: "Analista · crea el borrador",
    IdentityRole.STEWARD.value: "Steward · revisa decisiones",
    IdentityRole.PUBLISHER.value: "Publisher · prepara el handoff",
}


@dataclass
class _ScenarioClock:
    value: datetime = _NOW

    def now(self) -> datetime:
        return self.value


@dataclass
class _EmptyRegistryBaseReader:
    value: OnboardingRegistryBase = field(default_factory=OnboardingRegistryBase)

    def load(self, scope: SemanticRegistryScope) -> OnboardingRegistryBase:
        del scope
        return self.value


class _ExactSyntheticCatalog:
    """Read-only exact metadata resolver for this isolated scenario."""

    def __init__(self, scope: SemanticRegistryScope):
        self.scope = scope
        self.metadata_resolution_calls = 0

    def resolve_active(
        self,
        scope: SemanticRegistryScope,
        request: PreflightSemanticOnboardingRequest,
    ) -> SemanticOnboardingPreflight:
        self.metadata_resolution_calls += 1
        if scope != self.scope or request != _preflight_request():
            raise SemanticOnboardingPortError(
                SemanticOnboardingPortErrorCode.RESOURCE_UNAVAILABLE,
                "synthetic catalog identity is unavailable",
            )
        return SemanticOnboardingPreflight.create(
            scope=scope,
            evidence=self._evidence(),
            base_registry=OnboardingRegistryBase(),
        )

    def resolve_exact(
        self,
        scope: SemanticRegistryScope,
        connection_id: CatalogConnectionId,
        generation: int,
        expected_generation_fingerprint: str,
        selections: tuple[SemanticOnboardingMappingInput, ...],
    ) -> ResolvedOnboardingCatalogEvidence:
        self.metadata_resolution_calls += 1
        evidence = self._evidence()
        expected_authority = tuple(
            (
                item.locator.asset.asset_id,
                item.locator.field_path,
                item.asset_metadata_fingerprint,
                item.field_metadata_fingerprint,
                item.physical_field,
            )
            for item in evidence.observations
        )
        supplied_authority = tuple(
            (
                item.asset_id,
                item.field_path,
                item.expected_asset_metadata_fingerprint,
                item.expected_field_metadata_fingerprint,
                item.physical_field,
            )
            for item in selections
        )
        if (
            scope != self.scope
            or connection_id != evidence.generation.connection_id
            or generation != evidence.generation.generation
            or expected_generation_fingerprint != evidence.generation.inventory_fingerprint
            or supplied_authority != expected_authority
        ):
            raise SemanticOnboardingPortError(
                SemanticOnboardingPortErrorCode.RESOURCE_UNAVAILABLE,
                "synthetic catalog identity is unavailable",
            )
        return evidence

    def _evidence(self) -> ResolvedOnboardingCatalogEvidence:
        physical_types = (
            PhysicalValueType.STRING,
            PhysicalValueType.DECIMAL,
            PhysicalValueType.TIMESTAMP,
        )
        physical_fields = (
            PhysicalFieldRef("commerce.order_facts.order_id"),
            PhysicalFieldRef("commerce.order_facts.total_amount"),
            PhysicalFieldRef("commerce.order_facts.ordered_at"),
        )
        asset_id = CatalogAssetId("asset-commerce-order-facts-v7")
        observations = tuple(
            PhysicalCatalogObservation(
                locator=CatalogFieldLocator(
                    asset=CatalogAssetLocator(
                        workspace_id=self.scope.workspace_id,
                        connection_id=CatalogConnectionId("warehouse-commerce"),
                        asset_id=asset_id,
                    ),
                    field_path=(physical_field.root.rsplit(".", 1)[1],),
                ),
                catalog_scope=self.scope.catalog_scope,
                generation=7,
                generation_fingerprint=_GENERATION_FINGERPRINT,
                asset_metadata_fingerprint=_ASSET_FINGERPRINT,
                field_metadata_fingerprint=field_fingerprint,
                physical_field=physical_field,
                physical_type=physical_type,
            )
            for physical_field, physical_type, field_fingerprint in zip(
                physical_fields,
                physical_types,
                _FIELD_FINGERPRINTS,
                strict=True,
            )
        )
        return ResolvedOnboardingCatalogEvidence(
            generation=OnboardingCatalogGeneration(
                workspace_id=self.scope.workspace_id,
                connection_id=CatalogConnectionId("warehouse-commerce"),
                catalog_scope=self.scope.catalog_scope,
                generation=7,
                inventory_fingerprint=_GENERATION_FINGERPRINT,
                enabled=True,
                stale=False,
            ),
            observations=observations,
        )


@dataclass(frozen=True, slots=True)
class _ScenarioRuntime:
    preflight_request: PreflightSemanticOnboardingRequest
    catalog: _ExactSyntheticCatalog
    authorization: SemanticOnboardingAuthorizationPolicy
    preflight: PreflightSemanticOnboardingDraft
    create: CreateSemanticOnboardingDraft
    decide: DecideSemanticOnboarding
    prepare: PrepareSemanticOnboardingPublication
    inspect: InspectSemanticOnboardingDraft
    list_drafts: ListSemanticOnboardingDrafts
    principals: dict[str, AuthenticatedPrincipal]


@st.cache_resource(show_spinner=False)  # type: ignore[untyped-decorator]
def _runtime_for_token(token: str) -> _ScenarioRuntime:
    """Keep the local application store outside widget session state."""

    if _TOKEN_PATTERN.fullmatch(token) is None:
        raise ValueError("M33 scenario token is invalid")
    scope = SemanticRegistryScope(
        workspace_id=_WORKSPACE,
        catalog_scope="postgres.synthetic_acceptance",
        registry_id="commerce_registry",
    )
    store = InMemorySemanticOnboardingStore()
    catalog = _ExactSyntheticCatalog(scope)
    base_reader = _EmptyRegistryBaseReader()
    authorization = SemanticOnboardingAuthorizationPolicy()
    clock = _ScenarioClock()
    return _ScenarioRuntime(
        preflight_request=_preflight_request(),
        catalog=catalog,
        authorization=authorization,
        preflight=PreflightSemanticOnboardingDraft(
            catalog,
            authorization,
            clock,
            scope,
        ),
        create=CreateSemanticOnboardingDraft(
            store,
            catalog,
            base_reader,
            authorization,
            clock,
            scope,
        ),
        decide=DecideSemanticOnboarding(
            store,
            catalog,
            base_reader,
            authorization,
            clock,
        ),
        prepare=PrepareSemanticOnboardingPublication(
            store,
            catalog,
            base_reader,
            authorization,
            clock,
        ),
        inspect=InspectSemanticOnboardingDraft(store, authorization, clock),
        list_drafts=ListSemanticOnboardingDrafts(store, authorization, clock),
        principals={
            role.value: _principal(role, f"m33-{role.value}")
            for role in (
                IdentityRole.ANALYST,
                IdentityRole.STEWARD,
                IdentityRole.PUBLISHER,
            )
        },
    )


def main() -> None:
    """Render the isolated empty-to-prepared M33 operator journey."""

    st.set_page_config(
        page_title="SchemaBridge · M33 governed onboarding",
        page_icon="🌉",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    _inject_styles()
    token = os.environ.get(_SCENARIO_TOKEN_ENV, "m33-manual-session")
    try:
        runtime = _runtime_for_token(token)
    except ValueError:
        st.error("m33_scenario_configuration_invalid: El escenario local fue rechazado.")
        st.stop()

    st.title("Onboarding semántico gobernado")
    st.warning(
        "Escenario sintético local de aceptación M33. No es una consola productiva y no "
        "contiene credenciales ni adaptadores de origen, DataHub, LLM, compilación o ejecución."
    )
    st.caption(
        "El almacén en memoria del proceso conserva el agregado; el estado de widgets no es "
        "la fuente de verdad. Reiniciar el proceso elimina este escenario sintético."
    )
    role_value = st.selectbox(
        "Sesión autenticada simulada",
        tuple(_ROLE_LABELS),
        format_func=_role_label,
        key="m33-authenticated-role",
    )
    principal = runtime.principals[role_value]
    _render_identity(principal)
    _render_zero_side_effects(runtime)

    try:
        drafts = runtime.list_drafts.execute(principal)
    except (AuthorizationError, SemanticOnboardingError):
        st.error("semantic_onboarding_resource_unavailable: El recurso no está disponible.")
        return
    if not drafts:
        _render_empty_state(runtime, principal)
        return

    draft = drafts[0]
    _render_draft(runtime, principal, draft)


def _render_empty_state(
    runtime: _ScenarioRuntime,
    principal: AuthenticatedPrincipal,
) -> None:
    st.info(
        "not_configured · No hay borradores de onboarding para este tenant autenticado. "
        "No se ha cargado ningún fallback grabado."
    )
    permissions = runtime.authorization.permissions_for(principal, at=_NOW)
    if SemanticOnboardingPermission.CREATE not in permissions:
        st.caption("Esta sesión puede consultar el estado, pero no crear el borrador.")
        return
    try:
        preflight = runtime.preflight.execute(principal, runtime.preflight_request)
    except (AuthorizationError, SemanticOnboardingError):
        st.error("semantic_onboarding_preflight_unavailable: El contexto no está disponible.")
        return
    st.subheader("Selección exacta retenida")
    st.caption(
        f"Conexión {preflight.connection_id.root} · ámbito {preflight.scope.catalog_scope} · "
        f"generación {preflight.catalog_generation} · "
        f"vector {_short(preflight.catalog_generation_fingerprint)} · "
        f"preflight {_short(preflight.fingerprint)}"
    )
    logical_fields = ("Order.order_id", "Order.total_amount", "Order.ordered_at")
    st.markdown(
        "\n".join(
            f"- `{logical_field}` ← `{observation.physical_field.root}` · "
            f"{observation.physical_type.value}"
            for logical_field, observation in zip(
                logical_fields,
                preflight.observations,
                strict=True,
            )
        )
    )
    st.caption(
        "El servidor derivó schema, tabla, tipos, URN y fingerprints; el cliente confirma la "
        "huella completa. Create vuelve a resolverla antes de persistir. Todas las propuestas "
        "comenzarán en needs_review, incluso con coincidencia exacta."
    )
    if st.button("Crear borrador gobernado", key="m33-create-draft", type="primary"):
        try:
            runtime.create.execute(
                principal,
                _creation_request(preflight),
                idempotency_key="m33-create-commerce-orders-0001",
            )
        except (AuthorizationError, SemanticOnboardingError):
            st.error("semantic_onboarding_request_rejected: La creación fue rechazada.")
            return
        st.rerun()


def _render_draft(
    runtime: _ScenarioRuntime,
    principal: AuthenticatedPrincipal,
    draft: SemanticOnboardingDraft,
) -> None:
    try:
        snapshot = runtime.inspect.execute(principal, draft.id)
    except (AuthorizationError, SemanticOnboardingError):
        st.error("semantic_onboarding_resource_unavailable: El recurso no está disponible.")
        return
    draft = snapshot.draft
    st.divider()
    st.subheader(f"Borrador · {draft.id}")
    status_col, revision_col, mapping_col = st.columns(3)
    status_col.metric("Estado", draft.status.value)
    revision_col.metric("Revisión", str(draft.revision))
    mapping_col.metric("Mapeos", str(len(draft.mappings)))
    st.caption(
        f"Fingerprint {_short(draft.fingerprint)} · conexión {draft.connection_id.root} · "
        f"generación {draft.catalog_generation} · base de registro vacía explícita"
    )
    st.markdown(
        f"**Modelo {draft.model.definition.id.root}** · {draft.model.status.value} · "
        f"{draft.model.definition.description}"
    )
    for mapping in draft.mappings:
        observation = mapping.observation
        st.markdown(
            f"- `{mapping.logical_field.root}` ← `{observation.physical_field.root}` · "
            f"{observation.physical_type.value} · **{mapping.status.value}** · "
            f"confianza {mapping.confidence.root:.2f}"
        )
        st.caption(
            f"asset={observation.locator.asset.asset_id.root} · "
            f"field={'.'.join(observation.locator.field_path)} · "
            f"field fingerprint={_short(observation.field_metadata_fingerprint)} · "
            f"evidencia={','.join(item.kind.value for item in mapping.evidence)} · "
            "datahub_urn=not_observed_no_synthesis · "
            f"riesgos={'; '.join(mapping.risks)}"
        )

    permissions = runtime.authorization.permissions_for(principal, at=_NOW)
    if draft.status is SemanticOnboardingStatus.NEEDS_REVIEW:
        if SemanticOnboardingPermission.DECIDE in permissions:
            _render_next_decision(runtime, principal, draft)
        elif draft.ready_for_preparation() and (
            SemanticOnboardingPermission.PREPARE_PUBLICATION in permissions
        ):
            _render_preparation(runtime, principal, draft)
        elif draft.ready_for_preparation():
            st.success(
                "Cierre de decisiones completo. Inicie una sesión publisher distinta para "
                "preparar el handoff inmutable."
            )
        else:
            st.info("Pendiente de revisión explícita por un steward autenticado.")
    else:
        _render_prepared(snapshot)

    _render_audit(snapshot.audit)


def _render_next_decision(
    runtime: _ScenarioRuntime,
    principal: AuthenticatedPrincipal,
    draft: SemanticOnboardingDraft,
) -> None:
    target_kind: SemanticOnboardingTargetKind
    target_id: str
    target_label: str
    if draft.model.status is ApprovalStatus.NEEDS_REVIEW:
        target_kind = SemanticOnboardingTargetKind.MODEL
        target_id = draft.model.definition.id.root
        target_label = f"modelo {target_id}"
    else:
        unresolved = next(
            (item for item in draft.mappings if item.status is ApprovalStatus.NEEDS_REVIEW),
            None,
        )
        if unresolved is None:
            if draft.ready_for_preparation():
                st.success(
                    "Cierre de decisiones completo. Inicie una sesión publisher distinta para "
                    "preparar el handoff inmutable."
                )
            else:
                st.warning(
                    "El cierre contiene un rechazo y no puede prepararse. Cree un nuevo borrador "
                    "corregido después de revisar el contexto."
                )
            return
        target_kind = SemanticOnboardingTargetKind.MAPPING
        target_id = unresolved.id
        target_label = f"mapeo {unresolved.logical_field.root}"

    st.divider()
    st.subheader(f"Decisión steward · {target_label}")
    action_value = st.radio(
        "Decisión",
        (DecisionAction.APPROVE.value, DecisionAction.REJECT.value),
        format_func=_decision_label,
        horizontal=True,
        key=f"m33-action-{target_kind.value}-{target_id}",
    )
    rationale = st.text_area(
        "Rationale humano",
        placeholder="Explique la definición, la evidencia exacta y el riesgo revisado.",
        key=f"m33-rationale-{target_kind.value}-{target_id}",
    )
    reference = st.text_input(
        "Referencia de evidencia gobernada",
        placeholder="ticket:SEM-123",
        key=f"m33-reference-{target_kind.value}-{target_id}",
        disabled=action_value == DecisionAction.REJECT.value,
    )
    action = DecisionAction(action_value)
    disabled = len(rationale.strip()) < 12 or (
        action is DecisionAction.APPROVE and len(reference.strip()) < 3
    )
    if st.button(
        "Registrar decisión append-only",
        key=f"m33-record-{target_kind.value}-{target_id}",
        type="primary",
        disabled=disabled,
    ):
        evidence = (
            (
                OnboardingEvidence(
                    kind=OnboardingEvidenceKind.HUMAN_ATTESTATION,
                    detail=(
                        "The steward reviewed the exact catalog observation and controlled "
                        "business definition."
                    ),
                    reference=reference.strip(),
                ),
            )
            if action is DecisionAction.APPROVE
            else ()
        )
        try:
            runtime.decide.execute(
                principal,
                draft.id,
                target_kind=target_kind,
                target_id=target_id,
                action=action,
                expected_revision=draft.revision,
                confirmed_draft_fingerprint=draft.fingerprint,
                rationale=rationale.strip(),
                evidence=evidence,
                idempotency_key=(
                    f"m33-{action.value}-{target_kind.value}-{target_id}-r{draft.revision}"
                ),
            )
        except (AuthorizationError, SemanticOnboardingError):
            st.error("semantic_onboarding_decision_rejected: La decisión fue rechazada.")
            return
        st.rerun()


def _render_preparation(
    runtime: _ScenarioRuntime,
    principal: AuthenticatedPrincipal,
    draft: SemanticOnboardingDraft,
) -> None:
    st.divider()
    st.subheader("Preparar handoff inmutable")
    st.info(
        "Esta operación sólo prepara `ready_for_publication`. M33 no publica en DataHub, no "
        "activa un registro y no abre una base de datos fuente."
    )
    confirmed = st.checkbox(
        f"He recargado y confirmado la revisión {draft.revision} y su fingerprint.",
        key="m33-confirm-preparation",
    )
    if st.button(
        "Preparar propuesta para el worker futuro",
        key="m33-prepare-proposal",
        type="primary",
        disabled=not confirmed,
    ):
        try:
            runtime.prepare.execute(
                principal,
                draft.id,
                expected_revision=draft.revision,
                confirmed_draft_fingerprint=draft.fingerprint,
                idempotency_key=f"m33-prepare-{draft.id}-revision-{draft.revision}",
            )
        except (AuthorizationError, SemanticOnboardingError):
            st.error("semantic_onboarding_preparation_rejected: La preparación fue rechazada.")
            return
        st.rerun()


def _render_prepared(snapshot: SemanticOnboardingSnapshot) -> None:
    if len(snapshot.proposals) != 1:
        st.error("semantic_onboarding_resource_unavailable: El handoff no está disponible.")
        return
    proposal = snapshot.proposals[0]
    st.divider()
    st.success("ready_for_publication · Propuesta inmutable preparada, no publicada.")
    st.caption(
        f"Propuesta {proposal.id} · versión objetivo {proposal.target_registry_version} · "
        f"fingerprint {_short(proposal.fingerprint)}"
    )
    st.metric("External writes performed", str(proposal.external_writes_performed).lower())
    st.download_button(
        "Descargar handoff JSON no ejecutable",
        data=proposal.model_dump_json(indent=2),
        file_name=f"{proposal.id}.json",
        mime="application/json",
        key="m33-download-proposal",
    )
    st.warning(
        "Publicación y activación no disponibles en M33. El artefacto descargado no concede "
        "autoridad de escritura ni es ejecutable."
    )


def _render_audit(audit: tuple[SemanticOnboardingAuditRecord, ...]) -> None:
    st.divider()
    st.subheader("Trazabilidad append-only")
    for item in audit:
        st.caption(
            f"{item.event.value} · revisión {item.resulting_revision} · actor {item.actor_id}"
        )


def _render_identity(principal: AuthenticatedPrincipal) -> None:
    st.caption(
        f"Principal {principal.actor_id} · workspace {_WORKSPACE} · "
        f"rol {next(iter(principal.roles)).value} · método OIDC simulado"
    )


def _render_zero_side_effects(runtime: _ScenarioRuntime) -> None:
    external_col, sql_col, catalog_col = st.columns(3)
    external_col.metric("External writes", "0")
    sql_col.metric("SQL generado", "0")
    catalog_col.metric(
        "Resoluciones metadata",
        str(runtime.catalog.metadata_resolution_calls),
    )


def _creation_request(
    preflight: SemanticOnboardingPreflight,
) -> CreateSemanticOnboardingRequest:
    model = SemanticModelDefinition(
        id=LogicalModelRef("Order"),
        description="Governed business order recorded by the commerce platform.",
        fields=(
            SemanticFieldDefinition(
                id=LogicalFieldRef("Order.order_id"),
                canonical_type=CanonicalType.STRING,
                role=LogicalFieldRole.IDENTIFIER,
                definition="Stable business identifier for one governed order.",
            ),
            SemanticFieldDefinition(
                id=LogicalFieldRef("Order.total_amount"),
                canonical_type=CanonicalType.DECIMAL,
                role=LogicalFieldRole.MEASURE,
                definition="Governed gross order amount in the approved currency context.",
            ),
            SemanticFieldDefinition(
                id=LogicalFieldRef("Order.ordered_at"),
                canonical_type=CanonicalType.TIMESTAMP,
                role=LogicalFieldRole.TEMPORAL,
                definition="Timestamp at which the business order was accepted.",
            ),
        ),
    )
    logical_fields = tuple(field.id for field in model.fields)
    mapping_ids = (
        "mapping-order-id",
        "mapping-total-amount",
        "mapping-ordered-at",
    )
    mappings = tuple(
        SemanticOnboardingMappingInput(
            id=mapping_id,
            logical_field=logical_field,
            asset_id=observation.locator.asset.asset_id,
            field_path=observation.locator.field_path,
            expected_asset_metadata_fingerprint=(observation.asset_metadata_fingerprint),
            expected_field_metadata_fingerprint=(observation.field_metadata_fingerprint),
            physical_field=observation.physical_field,
            confidence=ConfidenceScore(1.0),
            evidence=(
                OnboardingEvidence(
                    kind=OnboardingEvidenceKind.NAME_SIMILARITY,
                    detail="Exact normalized field-name match; this is not approval evidence.",
                ),
                OnboardingEvidence(
                    kind=OnboardingEvidenceKind.CATALOG_DEFINITION,
                    detail="Retained catalog definition is compatible and requires steward review.",
                    reference="catalog:generation-7",
                ),
            ),
            risks=("Business meaning still requires explicit human confirmation.",),
            transformation_plan=TransformationPlan(steps=(IdentityStep(),)),
        )
        for mapping_id, logical_field, observation in zip(
            mapping_ids,
            logical_fields,
            preflight.observations,
            strict=True,
        )
    )
    return CreateSemanticOnboardingRequest(
        draft_id="commerce-orders-onboarding",
        connection_id=preflight.connection_id,
        catalog_generation=preflight.catalog_generation,
        catalog_generation_fingerprint=preflight.catalog_generation_fingerprint,
        expected_base_registry=preflight.base_registry,
        confirmed_preflight_fingerprint=preflight.fingerprint,
        model=model,
        mappings=mappings,
    )


def _preflight_request() -> PreflightSemanticOnboardingRequest:
    asset_id = CatalogAssetId("asset-commerce-order-facts-v7")
    return PreflightSemanticOnboardingRequest(
        connection_id=CatalogConnectionId("warehouse-commerce"),
        selections=tuple(
            SemanticOnboardingPreflightSelection(
                asset_id=asset_id,
                field_path=(field_name,),
            )
            for field_name in ("order_id", "total_amount", "ordered_at")
        ),
    )


def _principal(role: IdentityRole, actor_id: str) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        actor_id=actor_id,
        workspace_id=_WORKSPACE,
        roles=frozenset({role}),
        authentication_method=AuthenticationMethod.OIDC,
        authenticated_at=_NOW - timedelta(minutes=1),
        expires_at=_NOW + timedelta(hours=1),
    )


def _role_label(value: str) -> str:
    return _ROLE_LABELS[value]


def _decision_label(value: str) -> str:
    return "Aprobar" if value == DecisionAction.APPROVE.value else "Rechazar"


def _short(value: str) -> str:
    return f"{value[:12]}…"


def _inject_styles() -> None:
    st.markdown(
        """
        <style>
        .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
            max-width: 100%; overflow-x: hidden;
        }
        code, p, span, div { overflow-wrap: anywhere; }
        @media (max-width: 640px) {
            [data-testid="stHorizontalBlock"] { flex-wrap: wrap; }
            [data-testid="column"] {
                min-width: 0 !important; width: 100% !important; flex: 1 1 100% !important;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
