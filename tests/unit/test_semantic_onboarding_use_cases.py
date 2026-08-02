"""M33 authenticated onboarding application tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

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
    SemanticOnboardingErrorCode,
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
    MAX_ONBOARDING_DECISIONS,
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
    SemanticOnboardingAuditEvent,
    SemanticOnboardingDecision,
    SemanticOnboardingMappingInput,
    SemanticOnboardingPreflight,
    SemanticOnboardingPreflightSelection,
    SemanticOnboardingStatus,
    SemanticOnboardingTargetKind,
)
from schemabridge.domain.semantic_registry import PhysicalValueType, SemanticRegistryScope
from schemabridge.domain.transformations import (
    CastIntegerToStringStep,
    IdentityStep,
    ParseDateStep,
    TransformationPlan,
)

NOW = datetime(2026, 8, 2, 10, 0, tzinfo=UTC)
FP_A = "a" * 64
FP_B = "b" * 64
FP_C = "c" * 64


def test_create_starts_every_semantic_fact_in_review_and_replays_exactly() -> None:
    harness = _harness()
    analyst = _principal(IdentityRole.ANALYST, "analyst-a")

    first = harness.create.execute(
        analyst,
        harness.request,
        idempotency_key="create-orders-0001",
    )
    replay = harness.create.execute(
        analyst,
        harness.request,
        idempotency_key="create-orders-0001",
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.draft == first.draft
    assert first.draft.model.status is ApprovalStatus.NEEDS_REVIEW
    assert {item.status for item in first.draft.mappings} == {ApprovalStatus.NEEDS_REVIEW}
    assert first.draft.ready_for_preparation() is False
    assert first.draft.mappings[0].observation.observed_datahub_asset_urn is None
    assert harness.catalog.calls == 1
    assert len(harness.store.list_audit("workspace-a", first.draft.id)) == 1


def test_preflight_returns_server_derived_exact_context_without_mutation() -> None:
    harness = _harness()
    result = harness.preflight.execute(
        _principal(IdentityRole.ANALYST, "analyst-a"),
        harness.preflight_request,
    )

    assert result.fingerprint == harness.request.confirmed_preflight_fingerprint
    assert result.catalog_generation == 7
    assert result.base_registry == OnboardingRegistryBase()
    assert result.observations[0].physical_field == PhysicalFieldRef("sales.orders.order_id")
    assert result.external_writes_performed is False
    assert harness.store.list_for_workspace("workspace-a", owner_actor_id=None, limit=50) == ()


def test_same_idempotency_key_with_another_payload_fails_without_mutation() -> None:
    harness = _harness()
    analyst = _principal(IdentityRole.ANALYST, "analyst-a")
    first = harness.create.execute(
        analyst,
        harness.request,
        idempotency_key="create-orders-0001",
    )
    changed = harness.request.model_copy(update={"draft_id": "changed-onboarding"})

    with pytest.raises(SemanticOnboardingError) as raised:
        harness.create.execute(
            analyst,
            changed,
            idempotency_key="create-orders-0001",
        )
    assert raised.value.code is SemanticOnboardingErrorCode.CONFLICT
    assert harness.store.list_for_workspace("workspace-a", owner_actor_id=None, limit=50) == (
        first.draft,
    )


def test_cross_tenant_resource_is_unavailable_before_catalog_revalidation() -> None:
    harness = _harness()
    draft = harness.create.execute(
        _principal(IdentityRole.ANALYST, "analyst-a"),
        harness.request,
        idempotency_key="create-orders-0001",
    ).draft
    calls_before = harness.catalog.calls

    with pytest.raises(SemanticOnboardingError) as raised:
        harness.inspect.execute(
            _principal(IdentityRole.STEWARD, "steward-b", workspace="workspace-b"),
            draft.id,
        )
    assert raised.value.code is SemanticOnboardingErrorCode.UNAVAILABLE
    assert harness.catalog.calls == calls_before


def test_steward_decisions_revalidate_catalog_and_use_revision_cas() -> None:
    harness = _harness()
    draft = harness.create.execute(
        _principal(IdentityRole.ANALYST, "analyst-a"),
        harness.request,
        idempotency_key="create-orders-0001",
    ).draft
    steward = _principal(IdentityRole.STEWARD, "steward-a")

    model_result = harness.decide.execute(
        steward,
        draft.id,
        target_kind=SemanticOnboardingTargetKind.MODEL,
        target_id="Order",
        action=DecisionAction.APPROVE,
        expected_revision=draft.revision,
        confirmed_draft_fingerprint=draft.fingerprint,
        rationale="The steward confirms the governed business definition.",
        evidence=(_human_evidence(),),
        idempotency_key="approve-model-0001",
    )
    replay = harness.decide.execute(
        steward,
        draft.id,
        target_kind=SemanticOnboardingTargetKind.MODEL,
        target_id="Order",
        action=DecisionAction.APPROVE,
        expected_revision=draft.revision,
        confirmed_draft_fingerprint=draft.fingerprint,
        rationale="The steward confirms the governed business definition.",
        evidence=(_human_evidence(),),
        idempotency_key="approve-model-0001",
    )

    assert model_result.draft.revision == 2
    assert replay.replayed is True
    assert harness.catalog.calls == 2
    with pytest.raises(SemanticOnboardingError) as terminal:
        harness.decide.execute(
            steward,
            draft.id,
            target_kind=SemanticOnboardingTargetKind.MODEL,
            target_id="Order",
            action=DecisionAction.REJECT,
            expected_revision=model_result.draft.revision,
            confirmed_draft_fingerprint=model_result.draft.fingerprint,
            rationale="A terminal semantic decision cannot be overwritten in this draft.",
            evidence=(),
            idempotency_key="reject-decided-model1",
        )
    assert terminal.value.code is SemanticOnboardingErrorCode.CONFLICT
    with pytest.raises(SemanticOnboardingError) as raised:
        harness.decide.execute(
            steward,
            draft.id,
            target_kind=SemanticOnboardingTargetKind.MAPPING,
            target_id="mapping-order-id",
            action=DecisionAction.APPROVE,
            expected_revision=1,
            confirmed_draft_fingerprint=draft.fingerprint,
            rationale="This stale decision must not be accepted by the store.",
            evidence=(_human_evidence(),),
            idempotency_key="approve-mapping-stale-1",
        )
    assert raised.value.code is SemanticOnboardingErrorCode.CONFLICT
    assert len(harness.store.list_decisions("workspace-a", draft.id)) == 1


def test_catalog_drift_blocks_decision_with_zero_mutation() -> None:
    harness = _harness()
    draft = harness.create.execute(
        _principal(IdentityRole.ANALYST, "analyst-a"),
        harness.request,
        idempotency_key="create-orders-0001",
    ).draft
    harness.catalog.fail = True

    with pytest.raises(SemanticOnboardingError) as raised:
        harness.decide.execute(
            _principal(IdentityRole.STEWARD, "steward-a"),
            draft.id,
            target_kind=SemanticOnboardingTargetKind.MODEL,
            target_id="Order",
            action=DecisionAction.APPROVE,
            expected_revision=draft.revision,
            confirmed_draft_fingerprint=draft.fingerprint,
            rationale="The model is correct, but stale catalog must still block.",
            evidence=(_human_evidence(),),
            idempotency_key="approve-model-drift1",
        )
    assert raised.value.code is SemanticOnboardingErrorCode.STALE_CATALOG
    assert harness.store.load("workspace-a", draft.id) == draft
    assert harness.store.list_decisions("workspace-a", draft.id) == ()


def test_create_rejects_an_implicit_integer_to_string_conversion() -> None:
    harness = _harness()
    harness.catalog.physical_type = PhysicalValueType.INTEGER

    with pytest.raises(SemanticOnboardingError) as raised:
        harness.create.execute(
            _principal(IdentityRole.ANALYST, "analyst-a"),
            harness.request,
            idempotency_key="create-orders-types1",
        )

    assert raised.value.code is SemanticOnboardingErrorCode.STALE_CATALOG
    assert harness.store.list_for_workspace("workspace-a", owner_actor_id=None, limit=50) == ()


def test_create_rejects_a_cast_hidden_behind_an_invalid_ordered_step() -> None:
    harness = _harness()
    harness.catalog.physical_type = PhysicalValueType.INTEGER
    mapping = harness.request.mappings[0].model_copy(
        update={
            "transformation_plan": TransformationPlan(
                steps=(
                    ParseDateStep(format="YYYY-MM-DD"),
                    CastIntegerToStringStep(),
                )
            )
        }
    )
    request = harness.request.model_copy(update={"mappings": (mapping,)})

    with pytest.raises(SemanticOnboardingError) as raised:
        harness.create.execute(
            _principal(IdentityRole.ANALYST, "analyst-a"),
            request,
            idempotency_key="create-orders-types2",
        )

    assert raised.value.code is SemanticOnboardingErrorCode.STALE_CATALOG
    assert harness.store.list_for_workspace("workspace-a", owner_actor_id=None, limit=50) == ()


def test_create_rejects_string_date_parsing_until_calendar_validation_is_total() -> None:
    harness = _harness()
    source_field = harness.request.model.fields[0]
    model = harness.request.model.model_copy(
        update={
            "fields": (
                source_field.model_copy(
                    update={
                        "canonical_type": CanonicalType.DATE,
                        "role": LogicalFieldRole.TEMPORAL,
                    }
                ),
            )
        }
    )
    mapping = harness.request.mappings[0].model_copy(
        update={
            "transformation_plan": TransformationPlan(steps=(ParseDateStep(format="YYYY-MM-DD"),))
        }
    )
    request = harness.request.model_copy(update={"model": model, "mappings": (mapping,)})

    with pytest.raises(SemanticOnboardingError) as raised:
        harness.create.execute(
            _principal(IdentityRole.ANALYST, "analyst-a"),
            request,
            idempotency_key="create-orders-date-parse1",
        )

    assert raised.value.code is SemanticOnboardingErrorCode.STALE_CATALOG
    assert harness.store.list_for_workspace("workspace-a", owner_actor_id=None, limit=50) == ()


def test_complete_review_prepares_non_executable_proposal_with_separation_of_duties() -> None:
    harness = _harness()
    draft = _approve_all(harness)
    same_actor_publisher = _principal(
        IdentityRole.PUBLISHER,
        "steward-a",
    )

    with pytest.raises(SemanticOnboardingError) as raised:
        harness.prepare.execute(
            same_actor_publisher,
            draft.id,
            expected_revision=draft.revision,
            confirmed_draft_fingerprint=draft.fingerprint,
            idempotency_key="prepare-orders-v1a",
        )
    assert raised.value.code is SemanticOnboardingErrorCode.SEPARATION_OF_DUTIES

    publisher = _principal(IdentityRole.PUBLISHER, "publisher-a")
    result = harness.prepare.execute(
        publisher,
        draft.id,
        expected_revision=draft.revision,
        confirmed_draft_fingerprint=draft.fingerprint,
        idempotency_key="prepare-orders-v1b",
    )
    replay = harness.prepare.execute(
        publisher,
        draft.id,
        expected_revision=draft.revision,
        confirmed_draft_fingerprint=draft.fingerprint,
        idempotency_key="prepare-orders-v1b",
    )

    assert result.replayed is False
    assert replay.replayed is True
    assert replay.proposal == result.proposal
    assert result.draft.status is SemanticOnboardingStatus.READY_FOR_PUBLICATION
    assert result.proposal.target_registry_version == 1
    assert result.proposal.external_writes_performed is False
    snapshot = harness.inspect.execute(publisher, draft.id)
    assert len(snapshot.decisions) == 2
    assert snapshot.proposals == (result.proposal,)
    assert [item.event.value for item in snapshot.audit] == [
        "draft_created",
        "decision_recorded",
        "decision_recorded",
        "publication_prepared",
    ]


def test_preparation_loads_the_complete_structural_decision_bound_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness()
    draft = _approve_all(harness)
    stored = harness.store.list_decisions("workspace-a", draft.id)

    def excessive_history(
        workspace_id: str,
        draft_id: str,
        *,
        limit: int,
    ) -> tuple[SemanticOnboardingDecision, ...]:
        assert (workspace_id, draft_id) == ("workspace-a", draft.id)
        assert limit == MAX_ONBOARDING_DECISIONS + 1
        return (stored[0],) * (MAX_ONBOARDING_DECISIONS + 1)

    monkeypatch.setattr(harness.store, "list_decisions", excessive_history)

    with pytest.raises(SemanticOnboardingError) as raised:
        harness.prepare.execute(
            _principal(IdentityRole.PUBLISHER, "publisher-a"),
            draft.id,
            expected_revision=draft.revision,
            confirmed_draft_fingerprint=draft.fingerprint,
            idempotency_key="prepare-excess-history1",
        )
    assert raised.value.code is SemanticOnboardingErrorCode.SERVICE_UNAVAILABLE


def test_analyst_cannot_decide_and_steward_cannot_prepare() -> None:
    harness = _harness()
    draft = harness.create.execute(
        _principal(IdentityRole.ANALYST, "analyst-a"),
        harness.request,
        idempotency_key="create-orders-0001",
    ).draft

    with pytest.raises(AuthorizationError):
        harness.decide.execute(
            _principal(IdentityRole.ANALYST, "analyst-a"),
            draft.id,
            target_kind=SemanticOnboardingTargetKind.MODEL,
            target_id="Order",
            action=DecisionAction.APPROVE,
            expected_revision=draft.revision,
            confirmed_draft_fingerprint=draft.fingerprint,
            rationale="Analysts cannot approve their own semantic model.",
            evidence=(_human_evidence(),),
            idempotency_key="analyst-approve-01",
        )
    with pytest.raises(AuthorizationError):
        harness.prepare.execute(
            _principal(IdentityRole.STEWARD, "steward-a"),
            draft.id,
            expected_revision=draft.revision,
            confirmed_draft_fingerprint=draft.fingerprint,
            idempotency_key="steward-prepare1",
        )


def test_empty_workspace_lists_no_demo_or_recorded_fallback() -> None:
    harness = _harness()

    assert harness.list_drafts.execute(_principal(IdentityRole.STEWARD, "steward-a")) == ()
    assert harness.catalog.calls == 0


def test_inspection_loads_audit_only_for_explicit_audit_view_permission() -> None:
    harness = _harness()
    analyst = _principal(IdentityRole.ANALYST, "analyst-a")
    draft = harness.create.execute(
        analyst,
        harness.request,
        idempotency_key="create-orders-audit-1",
    ).draft

    owner_view = harness.inspect.execute(analyst, draft.id)
    steward_view = harness.inspect.execute(
        _principal(IdentityRole.STEWARD, "steward-a"),
        draft.id,
    )

    assert owner_view.draft == draft
    assert owner_view.audit_visible is False
    assert owner_view.history_truncated is False
    assert owner_view.decisions == ()
    assert owner_view.proposals == ()
    assert owner_view.audit == ()
    assert steward_view.audit_visible is True
    assert steward_view.history_truncated is False
    assert len(steward_view.audit) == 1


def test_inspection_returns_a_bounded_recent_chronological_history_window() -> None:
    harness = _harness()
    draft = _approve_all(harness)
    steward = _principal(IdentityRole.STEWARD, "steward-a")

    snapshot = harness.inspect.execute(steward, draft.id, history_limit=1)

    assert snapshot.history_truncated is True
    assert len(snapshot.decisions) == 1
    assert snapshot.decisions[0].target_id == "mapping-order-id"
    assert snapshot.proposals == ()
    assert len(snapshot.audit) == 1
    assert snapshot.audit[0].event is SemanticOnboardingAuditEvent.DECISION_RECORDED

    for invalid_limit in (0, 51):
        with pytest.raises(SemanticOnboardingError) as raised:
            harness.inspect.execute(
                steward,
                draft.id,
                history_limit=invalid_limit,
            )
        assert raised.value.code is SemanticOnboardingErrorCode.INVALID_REQUEST


@dataclass
class _Clock:
    value: datetime = NOW

    def now(self) -> datetime:
        return self.value


@dataclass
class _BaseReader:
    value: OnboardingRegistryBase = field(default_factory=OnboardingRegistryBase)

    def load(self, scope: SemanticRegistryScope) -> OnboardingRegistryBase:
        del scope
        return self.value


class _Catalog:
    def __init__(
        self, request: CreateSemanticOnboardingRequest, scope: SemanticRegistryScope
    ) -> None:
        self.request = request
        self.scope = scope
        self.calls = 0
        self.fail = False
        self.physical_type = PhysicalValueType.STRING

    def resolve_active(
        self,
        scope: SemanticRegistryScope,
        request: PreflightSemanticOnboardingRequest,
    ) -> SemanticOnboardingPreflight:
        self.calls += 1
        expected = tuple(
            SemanticOnboardingPreflightSelection(
                asset_id=item.asset_id,
                field_path=item.field_path,
            )
            for item in self.request.mappings
        )
        if (
            self.fail
            or scope != self.scope
            or request.connection_id != self.request.connection_id
            or request.selections != expected
        ):
            raise SemanticOnboardingPortError(
                SemanticOnboardingPortErrorCode.RESOURCE_UNAVAILABLE,
                "catalog evidence unavailable",
            )
        evidence = self._evidence(
            scope,
            request.connection_id,
            self.request.catalog_generation,
            self.request.catalog_generation_fingerprint,
            self.request.mappings,
        )
        return SemanticOnboardingPreflight.create(
            scope=scope,
            evidence=evidence,
            base_registry=self.request.expected_base_registry,
        )

    def resolve_exact(
        self,
        scope: SemanticRegistryScope,
        connection_id: CatalogConnectionId,
        generation: int,
        expected_generation_fingerprint: str,
        selections: tuple[SemanticOnboardingMappingInput, ...],
    ) -> ResolvedOnboardingCatalogEvidence:
        self.calls += 1
        if self.fail:
            raise SemanticOnboardingPortError(
                SemanticOnboardingPortErrorCode.RESOURCE_UNAVAILABLE,
                "catalog evidence unavailable",
            )
        assert scope == self.scope
        assert connection_id == self.request.connection_id
        assert generation == self.request.catalog_generation
        assert expected_generation_fingerprint == self.request.catalog_generation_fingerprint
        expected_authority = tuple(
            (
                item.asset_id,
                item.field_path,
                item.expected_asset_metadata_fingerprint,
                item.expected_field_metadata_fingerprint,
                item.physical_field,
            )
            for item in self.request.mappings
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
        if supplied_authority != expected_authority:
            raise SemanticOnboardingPortError(
                SemanticOnboardingPortErrorCode.RESOURCE_UNAVAILABLE,
                "catalog evidence unavailable",
            )
        return self._evidence(
            scope,
            connection_id,
            generation,
            expected_generation_fingerprint,
            selections,
        )

    def _evidence(
        self,
        scope: SemanticRegistryScope,
        connection_id: CatalogConnectionId,
        generation: int,
        generation_fingerprint: str,
        selections: tuple[SemanticOnboardingMappingInput, ...],
    ) -> ResolvedOnboardingCatalogEvidence:
        observations = tuple(
            PhysicalCatalogObservation(
                locator=CatalogFieldLocator(
                    asset=CatalogAssetLocator(
                        workspace_id=scope.workspace_id,
                        connection_id=connection_id,
                        asset_id=selection.asset_id,
                    ),
                    field_path=selection.field_path,
                ),
                catalog_scope=scope.catalog_scope,
                generation=generation,
                generation_fingerprint=generation_fingerprint,
                asset_metadata_fingerprint=selection.expected_asset_metadata_fingerprint,
                field_metadata_fingerprint=selection.expected_field_metadata_fingerprint,
                physical_field=selection.physical_field,
                physical_type=self.physical_type,
            )
            for selection in selections
        )
        return ResolvedOnboardingCatalogEvidence(
            generation=OnboardingCatalogGeneration(
                workspace_id=scope.workspace_id,
                connection_id=connection_id,
                catalog_scope=scope.catalog_scope,
                generation=generation,
                inventory_fingerprint=generation_fingerprint,
                enabled=True,
                stale=False,
            ),
            observations=observations,
        )


@dataclass
class _Harness:
    store: InMemorySemanticOnboardingStore
    catalog: _Catalog
    request: CreateSemanticOnboardingRequest
    preflight_request: PreflightSemanticOnboardingRequest
    preflight: PreflightSemanticOnboardingDraft
    create: CreateSemanticOnboardingDraft
    decide: DecideSemanticOnboarding
    prepare: PrepareSemanticOnboardingPublication
    inspect: InspectSemanticOnboardingDraft
    list_drafts: ListSemanticOnboardingDrafts


def _harness(*, workspace: str = "workspace-a") -> _Harness:
    scope = SemanticRegistryScope(
        workspace_id=workspace,
        catalog_scope="postgres.production",
        registry_id="orders_registry",
    )
    request = _request()
    store = InMemorySemanticOnboardingStore()
    catalog = _Catalog(request, scope)
    base = _BaseReader()
    auth = SemanticOnboardingAuthorizationPolicy()
    clock = _Clock()
    preflight_request = PreflightSemanticOnboardingRequest(
        connection_id=request.connection_id,
        selections=tuple(
            SemanticOnboardingPreflightSelection(
                asset_id=item.asset_id,
                field_path=item.field_path,
            )
            for item in request.mappings
        ),
    )
    resolved_preflight = catalog.resolve_active(scope, preflight_request)
    request = request.model_copy(
        update={"confirmed_preflight_fingerprint": resolved_preflight.fingerprint}
    )
    catalog.request = request
    catalog.calls = 0
    return _Harness(
        store=store,
        catalog=catalog,
        request=request,
        preflight_request=preflight_request,
        preflight=PreflightSemanticOnboardingDraft(catalog, auth, clock, scope),
        create=CreateSemanticOnboardingDraft(store, catalog, base, auth, clock, scope),
        decide=DecideSemanticOnboarding(store, catalog, base, auth, clock),
        prepare=PrepareSemanticOnboardingPublication(store, catalog, base, auth, clock),
        inspect=InspectSemanticOnboardingDraft(store, auth, clock),
        list_drafts=ListSemanticOnboardingDrafts(store, auth, clock),
    )


def _request() -> CreateSemanticOnboardingRequest:
    return CreateSemanticOnboardingRequest(
        draft_id="orders-onboarding",
        connection_id=CatalogConnectionId("warehouse-a"),
        catalog_generation=7,
        catalog_generation_fingerprint=FP_A,
        expected_base_registry=OnboardingRegistryBase(),
        confirmed_preflight_fingerprint=FP_B,
        model=SemanticModelDefinition(
            id=LogicalModelRef("Order"),
            description="Governed order business model.",
            fields=(
                SemanticFieldDefinition(
                    id=LogicalFieldRef("Order.order_id"),
                    canonical_type=CanonicalType.STRING,
                    role=LogicalFieldRole.IDENTIFIER,
                    definition="Stable order identifier.",
                ),
            ),
        ),
        mappings=(
            SemanticOnboardingMappingInput(
                id="mapping-order-id",
                logical_field=LogicalFieldRef("Order.order_id"),
                asset_id=CatalogAssetId("asset-orders"),
                field_path=("order_id",),
                expected_asset_metadata_fingerprint=FP_B,
                expected_field_metadata_fingerprint=FP_C,
                physical_field=PhysicalFieldRef("sales.orders.order_id"),
                confidence=ConfidenceScore(1.0),
                evidence=(
                    OnboardingEvidence(
                        kind=OnboardingEvidenceKind.NAME_SIMILARITY,
                        detail="Exact normalized name match.",
                    ),
                    OnboardingEvidence(
                        kind=OnboardingEvidenceKind.DECLARED_KEY,
                        detail="Catalog marks this field as a declared key.",
                    ),
                ),
                risks=("Identifier semantics require human confirmation.",),
                transformation_plan=TransformationPlan(steps=(IdentityStep(),)),
            ),
        ),
    )


def _approve_all(harness: _Harness):  # type: ignore[no-untyped-def]
    analyst = _principal(IdentityRole.ANALYST, "analyst-a")
    steward = _principal(IdentityRole.STEWARD, "steward-a")
    draft = harness.create.execute(
        analyst,
        harness.request,
        idempotency_key="create-orders-0001",
    ).draft
    draft = harness.decide.execute(
        steward,
        draft.id,
        target_kind=SemanticOnboardingTargetKind.MODEL,
        target_id="Order",
        action=DecisionAction.APPROVE,
        expected_revision=draft.revision,
        confirmed_draft_fingerprint=draft.fingerprint,
        rationale="The steward confirms the governed business definition.",
        evidence=(_human_evidence(),),
        idempotency_key="approve-model-0001",
    ).draft
    return harness.decide.execute(
        steward,
        draft.id,
        target_kind=SemanticOnboardingTargetKind.MAPPING,
        target_id="mapping-order-id",
        action=DecisionAction.APPROVE,
        expected_revision=draft.revision,
        confirmed_draft_fingerprint=draft.fingerprint,
        rationale="Declared key evidence confirms this exact physical observation.",
        evidence=(_human_evidence(),),
        idempotency_key="approve-mapping-01",
    ).draft


def _human_evidence() -> OnboardingEvidence:
    return OnboardingEvidence(
        kind=OnboardingEvidenceKind.HUMAN_ATTESTATION,
        detail="Steward reviewed the source definition and business owner evidence.",
        reference="ticket:SEM-42",
    )


def _principal(
    role: IdentityRole,
    actor: str,
    *,
    workspace: str = "workspace-a",
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        actor_id=actor,
        workspace_id=workspace,
        roles=frozenset({role}),
        authentication_method=AuthenticationMethod.OIDC,
        authenticated_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )
