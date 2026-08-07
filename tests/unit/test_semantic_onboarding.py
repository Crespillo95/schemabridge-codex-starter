"""Pure M33 semantic-onboarding contract tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from schemabridge.domain.catalog_inventory import (
    CatalogAssetId,
    CatalogAssetLocator,
    CatalogConnectionId,
    CatalogFieldLocator,
)
from schemabridge.domain.concepts import CanonicalType, LogicalFieldRef, LogicalModelRef
from schemabridge.domain.decisions import ApprovalStatus, DecisionAction
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.mappings import ConfidenceScore
from schemabridge.domain.request_context import LogicalFieldRole
from schemabridge.domain.resolution import mapping_type_is_compatible
from schemabridge.domain.semantic_onboarding import (
    CreateSemanticOnboardingRequest,
    OnboardingCatalogGeneration,
    OnboardingEvidence,
    OnboardingEvidenceKind,
    OnboardingRegistryBase,
    PhysicalCatalogObservation,
    PreflightSemanticOnboardingRequest,
    PreparedSemanticOnboardingProposal,
    ResolvedOnboardingCatalogEvidence,
    SemanticFieldDefinition,
    SemanticMappingProposal,
    SemanticModelDefinition,
    SemanticModelProposal,
    SemanticOnboardingDecision,
    SemanticOnboardingDraft,
    SemanticOnboardingMappingInput,
    SemanticOnboardingPreflight,
    SemanticOnboardingPreflightSelection,
    SemanticOnboardingStatus,
    SemanticOnboardingTargetKind,
)
from schemabridge.domain.semantic_registry import (
    PhysicalValueType,
    SemanticRegistryScope,
)
from schemabridge.domain.transformations import (
    CastIntegerToStringStep,
    CastTimestampToDateStep,
    EmptyToNullStep,
    IdentityStep,
    MapValuesStep,
    NormalizeDecimalScaleStep,
    ParseDateStep,
    RejectInvalidStep,
    TransformationPlan,
    TrimStep,
    ValidateFiniteStep,
    ValidateIntegralStep,
    ValueMapEntry,
)

NOW = datetime(2026, 8, 2, 10, 0, tzinfo=UTC)
FP_A = "a" * 64
FP_B = "b" * 64
FP_C = "c" * 64
FP_D = "d" * 64


def test_exact_name_and_full_confidence_never_auto_approve() -> None:
    draft = _draft()

    assert draft.model.status is ApprovalStatus.NEEDS_REVIEW
    assert draft.mappings[0].confidence == ConfidenceScore(1.0)
    assert draft.mappings[0].status is ApprovalStatus.NEEDS_REVIEW
    assert draft.ready_for_preparation() is False
    assert draft.status is SemanticOnboardingStatus.NEEDS_REVIEW


def test_physical_authority_distinguishes_equal_names_across_connections() -> None:
    first = _observation(connection_id="warehouse-a")
    second = _observation(connection_id="warehouse-b")

    assert first.physical_field == second.physical_field
    assert first.authority_identity != second.authority_identity


def test_datahub_urn_must_equal_the_observed_asset_identity() -> None:
    observed = "urn:li:dataset:(urn:li:dataPlatform:postgres,sales.orders,PROD)"
    assert (
        _observation(asset_id=observed, observed_urn=observed).observed_datahub_asset_urn
        == observed
    )

    with pytest.raises(ValidationError, match="must equal the observed"):
        _observation(
            asset_id=observed,
            observed_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,other.table,PROD)",
        )


def test_onboarding_rejects_identifiers_that_the_compiler_would_emit_unquoted() -> None:
    with pytest.raises(ValidationError, match="lowercase safe SQL identifiers"):
        _observation(physical_field="sales.Orders.order_id")

    with pytest.raises(ValidationError, match="lowercase safe SQL identifiers"):
        SemanticOnboardingMappingInput(
            id="mapping-order-id",
            logical_field=LogicalFieldRef("Order.order_id"),
            asset_id=CatalogAssetId("asset-orders"),
            field_path=("Order_ID",),
            expected_asset_metadata_fingerprint=FP_B,
            expected_field_metadata_fingerprint=FP_C,
            physical_field=PhysicalFieldRef("sales.orders.order_id"),
            confidence=ConfidenceScore(1.0),
            evidence=(
                OnboardingEvidence(
                    kind=OnboardingEvidenceKind.DECLARED_KEY,
                    detail="The catalog marks this field as a declared key.",
                ),
            ),
            transformation_plan=TransformationPlan(steps=(IdentityStep(),)),
        )

    with pytest.raises(ValidationError, match="lowercase safe SQL identifiers"):
        SemanticOnboardingMappingInput(
            id="mapping-order-id",
            logical_field=LogicalFieldRef("Order.order_id"),
            asset_id=CatalogAssetId("asset-orders"),
            field_path=("order_id",),
            expected_asset_metadata_fingerprint=FP_B,
            expected_field_metadata_fingerprint=FP_C,
            physical_field=PhysicalFieldRef("sales.Orders.order_id"),
            confidence=ConfidenceScore(1.0),
            evidence=(
                OnboardingEvidence(
                    kind=OnboardingEvidenceKind.DECLARED_KEY,
                    detail="The catalog marks this field as a declared key.",
                ),
            ),
            transformation_plan=TransformationPlan(steps=(IdentityStep(),)),
        )


def test_postgres_onboarding_accepts_only_one_column_field_path() -> None:
    with pytest.raises(ValidationError):
        SemanticOnboardingPreflightSelection(
            asset_id=CatalogAssetId("asset-orders"),
            field_path=("payload", "order_id"),
        )

    with pytest.raises(ValidationError):
        SemanticOnboardingMappingInput(
            id="mapping-order-id",
            logical_field=LogicalFieldRef("Order.order_id"),
            asset_id=CatalogAssetId("asset-orders"),
            field_path=("payload", "order_id"),
            expected_asset_metadata_fingerprint=FP_B,
            expected_field_metadata_fingerprint=FP_C,
            physical_field=PhysicalFieldRef("sales.orders.order_id"),
            confidence=ConfidenceScore(1.0),
            evidence=(
                OnboardingEvidence(
                    kind=OnboardingEvidenceKind.DECLARED_KEY,
                    detail="The catalog marks this field as a declared key.",
                ),
            ),
            transformation_plan=TransformationPlan(steps=(IdentityStep(),)),
        )

    with pytest.raises(ValidationError, match="exactly one field path segment"):
        _observation(field_path=("payload", "order_id"))


def test_postgres_onboarding_requires_exact_schema_table_column_identity() -> None:
    with pytest.raises(ValidationError, match=r"exactly schema\.table\.column"):
        _observation(physical_field="sales.orders.payload.order_id")

    with pytest.raises(ValidationError, match="match the observed field path"):
        _observation(physical_field="sales.orders.external_id")

    with pytest.raises(ValidationError, match="match its field path"):
        SemanticOnboardingMappingInput(
            id="mapping-order-id",
            logical_field=LogicalFieldRef("Order.order_id"),
            asset_id=CatalogAssetId("asset-orders"),
            field_path=("order_id",),
            expected_asset_metadata_fingerprint=FP_B,
            expected_field_metadata_fingerprint=FP_C,
            physical_field=PhysicalFieldRef("sales.orders.external_id"),
            confidence=ConfidenceScore(1.0),
            evidence=(
                OnboardingEvidence(
                    kind=OnboardingEvidenceKind.DECLARED_KEY,
                    detail="The catalog marks this field as a declared key.",
                ),
            ),
            transformation_plan=TransformationPlan(steps=(IdentityStep(),)),
        )


@pytest.mark.parametrize(
    ("canonical", "physical"),
    (
        (CanonicalType.STRING, PhysicalValueType.INTEGER),
        (CanonicalType.DATE, PhysicalValueType.TIMESTAMP),
    ),
)
def test_identity_does_not_hide_required_type_conversion(
    canonical: CanonicalType,
    physical: PhysicalValueType,
) -> None:
    assert not mapping_type_is_compatible(
        canonical,
        physical,
        TransformationPlan(steps=(IdentityStep(),)),
    )


def test_explicit_closed_casts_make_supported_type_conversions_compatible() -> None:
    assert mapping_type_is_compatible(
        CanonicalType.STRING,
        PhysicalValueType.INTEGER,
        TransformationPlan(steps=(CastIntegerToStringStep(),)),
    )
    assert mapping_type_is_compatible(
        CanonicalType.DATE,
        PhysicalValueType.TIMESTAMP,
        TransformationPlan(steps=(CastTimestampToDateStep(),)),
    )


@pytest.mark.parametrize(
    ("canonical", "physical", "plan"),
    (
        (
            CanonicalType.STRING,
            PhysicalValueType.INTEGER,
            TransformationPlan(
                steps=(ParseDateStep(format="YYYY-MM-DD"), CastIntegerToStringStep())
            ),
        ),
        (
            CanonicalType.STRING,
            PhysicalValueType.INTEGER,
            TransformationPlan(
                steps=(CastIntegerToStringStep(), ParseDateStep(format="YYYY-MM-DD"))
            ),
        ),
        (
            CanonicalType.STRING,
            PhysicalValueType.FLOAT,
            TransformationPlan(
                steps=(
                    ValidateFiniteStep(),
                    CastIntegerToStringStep(),
                    ValidateIntegralStep(),
                )
            ),
        ),
        (
            CanonicalType.BOOLEAN,
            PhysicalValueType.BOOLEAN,
            TransformationPlan(steps=(TrimStep(),)),
        ),
        (
            CanonicalType.DATE,
            PhysicalValueType.DATE,
            TransformationPlan(steps=(ParseDateStep(format="YYYY-MM-DD"),)),
        ),
        (
            CanonicalType.DATE,
            PhysicalValueType.STRING,
            TransformationPlan(
                steps=(
                    TrimStep(),
                    EmptyToNullStep(),
                    ParseDateStep(format="YYYY-MM-DD"),
                    RejectInvalidStep(),
                )
            ),
        ),
        (
            CanonicalType.INTEGER,
            PhysicalValueType.INTEGER,
            TransformationPlan(steps=(NormalizeDecimalScaleStep(scale=2),)),
        ),
        (
            CanonicalType.BOOLEAN,
            PhysicalValueType.BOOLEAN,
            TransformationPlan(
                steps=(MapValuesStep(entries=(ValueMapEntry(source=True, target="yes"),)),)
            ),
        ),
    ),
)
def test_mapping_type_compatibility_rejects_invalid_order_or_step_types(
    canonical: CanonicalType,
    physical: PhysicalValueType,
    plan: TransformationPlan,
) -> None:
    assert not mapping_type_is_compatible(canonical, physical, plan)


@pytest.mark.parametrize(
    ("canonical", "physical", "plan"),
    (
        (
            CanonicalType.STRING,
            PhysicalValueType.FLOAT,
            TransformationPlan(
                steps=(
                    ValidateFiniteStep(),
                    ValidateIntegralStep(),
                    CastIntegerToStringStep(),
                    RejectInvalidStep(),
                )
            ),
        ),
        (
            CanonicalType.STRING,
            PhysicalValueType.STRING,
            TransformationPlan(
                steps=(
                    MapValuesStep(
                        entries=(
                            ValueMapEntry(source="N", target="NEW"),
                            ValueMapEntry(source="C", target="CLOSED"),
                        )
                    ),
                    RejectInvalidStep(),
                )
            ),
        ),
        (
            CanonicalType.INTEGER,
            PhysicalValueType.FLOAT,
            TransformationPlan(steps=(ValidateFiniteStep(), ValidateIntegralStep())),
        ),
        (
            CanonicalType.DECIMAL,
            PhysicalValueType.INTEGER,
            TransformationPlan(steps=(NormalizeDecimalScaleStep(scale=2),)),
        ),
    ),
)
def test_mapping_type_compatibility_accepts_valid_ordered_plans(
    canonical: CanonicalType,
    physical: PhysicalValueType,
    plan: TransformationPlan,
) -> None:
    assert mapping_type_is_compatible(canonical, physical, plan)


def test_ready_closure_requires_model_and_every_mapping_terminal() -> None:
    draft = _draft(
        model_status=ApprovalStatus.APPROVED,
        model_decision_id="decision-model",
        model_decided_by="steward-a",
        mapping_status=ApprovalStatus.APPROVED,
        mapping_decision_id="decision-mapping",
        mapping_decided_by="steward-a",
    )

    assert draft.ready_for_preparation() is True

    pending_alternative = _mapping(
        id="mapping-alternative",
        status=ApprovalStatus.NEEDS_REVIEW,
    )
    with_pending = draft.model_copy(update={"mappings": (*draft.mappings, pending_alternative)})
    with_pending = SemanticOnboardingDraft.model_validate(with_pending.model_dump(mode="python"))
    assert with_pending.ready_for_preparation() is False


def test_asset_alias_cannot_give_one_physical_coordinate_two_approved_meanings() -> None:
    model = SemanticModelDefinition(
        id=LogicalModelRef("Order"),
        description="Governed order business model.",
        fields=(
            _field("Order.order_id"),
            _field("Order.external_id"),
        ),
    )
    first_observation = _observation(asset_id="asset-orders-alias-a")
    second_observation = _observation(asset_id="asset-orders-alias-b")
    assert first_observation.authority_identity == second_observation.authority_identity

    generation = OnboardingCatalogGeneration(
        workspace_id="workspace-a",
        connection_id=CatalogConnectionId("warehouse-a"),
        catalog_scope="postgres.production",
        generation=7,
        inventory_fingerprint=FP_A,
        enabled=True,
        stale=False,
    )
    with pytest.raises(ValidationError, match="observations must be unique"):
        ResolvedOnboardingCatalogEvidence(
            generation=generation,
            observations=(first_observation, second_observation),
        )

    first = _mapping(
        id="mapping-order-id",
        status=ApprovalStatus.APPROVED,
        decision_id="decision-one",
        decided_by="steward-a",
    ).model_copy(update={"observation": first_observation})
    second = first.model_copy(
        update={
            "id": "mapping-external-id",
            "logical_field": LogicalFieldRef("Order.external_id"),
            "decision_id": "decision-two",
            "observation": second_observation,
        }
    )

    with pytest.raises(ValidationError, match="two approved meanings"):
        _draft(model=model, mappings=(first, second))


def test_approval_decision_requires_non_name_evidence() -> None:
    with pytest.raises(ValidationError, match="beyond name similarity"):
        SemanticOnboardingDecision(
            id="decision-name-only",
            workspace_id="workspace-a",
            draft_id="orders-onboarding",
            target_kind=SemanticOnboardingTargetKind.MAPPING,
            target_id="mapping-order-id",
            action=DecisionAction.APPROVE,
            status=ApprovalStatus.APPROVED,
            actor_id="steward-a",
            decided_at=NOW,
            source_revision=1,
            resulting_revision=2,
            rationale="The names happen to be exactly equal.",
            evidence=(
                OnboardingEvidence(
                    kind=OnboardingEvidenceKind.NAME_SIMILARITY,
                    detail="Exact normalized name match.",
                ),
            ),
            idempotency_digest=FP_A,
            request_fingerprint=FP_B,
        )


def test_decision_and_evidence_reject_padding_as_meaningful_text() -> None:
    with pytest.raises(ValidationError, match="12 meaningful characters"):
        SemanticOnboardingDecision(
            id="decision-padded",
            workspace_id="workspace-a",
            draft_id="orders-onboarding",
            target_kind=SemanticOnboardingTargetKind.MAPPING,
            target_id="mapping-order-id",
            action=DecisionAction.REJECT,
            status=ApprovalStatus.REJECTED,
            actor_id="steward-a",
            decided_at=NOW,
            source_revision=1,
            resulting_revision=2,
            rationale="ok          ",
            idempotency_digest=FP_A,
            request_fingerprint=FP_B,
        )

    with pytest.raises(ValidationError, match="evidence text is invalid"):
        OnboardingEvidence(
            kind=OnboardingEvidenceKind.HUMAN_ATTESTATION,
            detail="x  ",
        )


def test_prepared_proposal_is_stable_non_executable_and_exact_successor() -> None:
    draft = _draft(
        model_status=ApprovalStatus.APPROVED,
        model_decision_id="decision-model",
        model_decided_by="steward-a",
        mapping_status=ApprovalStatus.APPROVED,
        mapping_decision_id="decision-mapping",
        mapping_decided_by="steward-a",
    )
    proposal = PreparedSemanticOnboardingProposal.create(
        id="proposal-orders-v1",
        draft=draft,
        decision_ids=("decision-mapping", "decision-model"),
        prepared_by="publisher-a",
        prepared_at=NOW,
    )
    replay = PreparedSemanticOnboardingProposal.create(
        id="proposal-orders-v1",
        draft=draft,
        decision_ids=("decision-model", "decision-mapping"),
        prepared_by="publisher-a",
        prepared_at=NOW,
    )

    assert proposal == replay
    assert proposal.target_registry_version == 1
    assert proposal.external_writes_performed is False
    assert proposal.fingerprint == replay.fingerprint


def test_preflight_fingerprint_binds_tenant_catalog_base_and_ordered_observations() -> None:
    scope = SemanticRegistryScope(
        workspace_id="workspace-a",
        catalog_scope="postgres.production",
        registry_id="orders_registry",
    )
    evidence = ResolvedOnboardingCatalogEvidence(
        generation=OnboardingCatalogGeneration(
            workspace_id=scope.workspace_id,
            connection_id=CatalogConnectionId("warehouse-a"),
            catalog_scope=scope.catalog_scope,
            generation=7,
            inventory_fingerprint=FP_A,
            enabled=True,
            stale=False,
        ),
        observations=(_observation(),),
    )

    first = SemanticOnboardingPreflight.create(
        scope=scope,
        evidence=evidence,
        base_registry=OnboardingRegistryBase(),
    )
    replay = SemanticOnboardingPreflight.create(
        scope=scope,
        evidence=evidence,
        base_registry=OnboardingRegistryBase(),
    )

    assert first == replay
    assert first.external_writes_performed is False
    assert first.fingerprint == replay.fingerprint
    with pytest.raises(ValidationError, match="fingerprint does not match"):
        SemanticOnboardingPreflight.model_validate(
            {**first.model_dump(mode="python"), "fingerprint": FP_D}
        )


def test_preflight_request_rejects_duplicate_or_unsafe_catalog_selection() -> None:
    selection = SemanticOnboardingPreflightSelection(
        asset_id=CatalogAssetId("asset-orders"),
        field_path=("order_id",),
    )
    with pytest.raises(ValidationError, match="must be unique"):
        PreflightSemanticOnboardingRequest(
            connection_id=CatalogConnectionId("warehouse-a"),
            selections=(selection, selection),
        )
    with pytest.raises(ValidationError, match="not compiler-safe"):
        SemanticOnboardingPreflightSelection(
            asset_id=CatalogAssetId("asset-orders"),
            field_path=("Order_ID",),
        )


def test_ready_status_requires_exact_prepared_proposal_binding() -> None:
    draft = _draft(
        model_status=ApprovalStatus.APPROVED,
        model_decision_id="decision-model",
        model_decided_by="steward-a",
        mapping_status=ApprovalStatus.APPROVED,
        mapping_decision_id="decision-mapping",
        mapping_decided_by="steward-a",
    )

    with pytest.raises(ValidationError, match="prepared proposal binding"):
        SemanticOnboardingDraft.model_validate(
            {**draft.model_dump(mode="python"), "status": "ready_for_publication"}
        )

    with pytest.raises(ValidationError, match="prepared proposal binding"):
        SemanticOnboardingDraft.model_validate(
            {
                **draft.model_dump(mode="python"),
                "status": "superseded",
                "prepared_proposal_id": "proposal-orders-v1",
            }
        )


def test_create_request_rejects_oversized_payload_before_catalog_access() -> None:
    definition = SemanticModelDefinition(
        id=LogicalModelRef("Order"),
        description="Governed order business model.",
        fields=(_field("Order.order_id"),),
    )
    evidence = tuple(
        OnboardingEvidence(
            kind=OnboardingEvidenceKind.CATALOG_DEFINITION,
            detail=f"{index:02d}-" + ("e" * 490),
        )
        for index in range(16)
    )
    risks = tuple(f"{index:02d}-" + ("r" * 490) for index in range(16))
    mappings = tuple(
        SemanticOnboardingMappingInput(
            id=f"mapping-order-{index:04d}",
            logical_field=LogicalFieldRef("Order.order_id"),
            asset_id=CatalogAssetId(f"asset-orders-{index:04d}"),
            field_path=("order_id",),
            expected_asset_metadata_fingerprint=FP_B,
            expected_field_metadata_fingerprint=FP_C,
            physical_field=PhysicalFieldRef("sales.orders.order_id"),
            confidence=ConfidenceScore(1.0),
            evidence=evidence,
            risks=risks,
            transformation_plan=TransformationPlan(steps=(IdentityStep(),)),
        )
        for index in range(160)
    )

    with pytest.raises(ValidationError, match="request exceeds the byte limit"):
        CreateSemanticOnboardingRequest(
            draft_id="orders-onboarding",
            connection_id=CatalogConnectionId("warehouse-a"),
            catalog_generation=7,
            catalog_generation_fingerprint=FP_A,
            expected_base_registry=OnboardingRegistryBase(),
            confirmed_preflight_fingerprint=FP_D,
            model=definition,
            mappings=mappings,
        )


def _field(identifier: str) -> SemanticFieldDefinition:
    return SemanticFieldDefinition(
        id=LogicalFieldRef(identifier),
        canonical_type=CanonicalType.STRING,
        role=LogicalFieldRole.IDENTIFIER,
        definition="Stable order identifier.",
    )


def _observation(
    *,
    connection_id: str = "warehouse-a",
    asset_id: str = "asset-orders",
    observed_urn: str | None = None,
    physical_field: str = "sales.orders.order_id",
    field_path: tuple[str, ...] = ("order_id",),
) -> PhysicalCatalogObservation:
    return PhysicalCatalogObservation(
        locator=CatalogFieldLocator(
            asset=CatalogAssetLocator(
                workspace_id="workspace-a",
                connection_id=CatalogConnectionId(connection_id),
                asset_id=CatalogAssetId(asset_id),
            ),
            field_path=field_path,
        ),
        catalog_scope="postgres.production",
        generation=7,
        generation_fingerprint=FP_A,
        asset_metadata_fingerprint=FP_B,
        field_metadata_fingerprint=FP_C,
        physical_field=PhysicalFieldRef(physical_field),
        physical_type=PhysicalValueType.STRING,
        observed_datahub_asset_urn=observed_urn,
    )


def _mapping(
    *,
    id: str = "mapping-order-id",
    status: ApprovalStatus = ApprovalStatus.NEEDS_REVIEW,
    decision_id: str | None = None,
    decided_by: str | None = None,
) -> SemanticMappingProposal:
    return SemanticMappingProposal(
        id=id,
        logical_field=LogicalFieldRef("Order.order_id"),
        observation=_observation(),
        confidence=ConfidenceScore(1.0),
        evidence=(
            OnboardingEvidence(
                kind=OnboardingEvidenceKind.NAME_SIMILARITY,
                detail="Exact normalized name match.",
            ),
            OnboardingEvidence(
                kind=OnboardingEvidenceKind.DECLARED_KEY,
                detail="Catalog marks the field as part of the primary key.",
                reference="catalog:asset-orders/order_id@generation-7",
            ),
        ),
        risks=("Identifier format still requires steward confirmation.",),
        transformation_plan=TransformationPlan(steps=(IdentityStep(),)),
        status=status,
        decision_id=decision_id,
        decided_by=decided_by,
    )


def _draft(
    *,
    model: SemanticModelDefinition | None = None,
    mappings: tuple[SemanticMappingProposal, ...] | None = None,
    model_status: ApprovalStatus = ApprovalStatus.NEEDS_REVIEW,
    model_decision_id: str | None = None,
    model_decided_by: str | None = None,
    mapping_status: ApprovalStatus = ApprovalStatus.NEEDS_REVIEW,
    mapping_decision_id: str | None = None,
    mapping_decided_by: str | None = None,
) -> SemanticOnboardingDraft:
    definition = model or SemanticModelDefinition(
        id=LogicalModelRef("Order"),
        description="Governed order business model.",
        fields=(_field("Order.order_id"),),
    )
    proposals = mappings or (
        _mapping(
            status=mapping_status,
            decision_id=mapping_decision_id,
            decided_by=mapping_decided_by,
        ),
    )
    return SemanticOnboardingDraft(
        id="orders-onboarding",
        workspace_id="workspace-a",
        owner_actor_id="analyst-a",
        scope=SemanticRegistryScope(
            workspace_id="workspace-a",
            catalog_scope="postgres.production",
            registry_id="orders_registry",
        ),
        connection_id=CatalogConnectionId("warehouse-a"),
        catalog_generation=7,
        catalog_generation_fingerprint=FP_A,
        base_registry=OnboardingRegistryBase(),
        model=SemanticModelProposal(
            definition=definition,
            status=model_status,
            decision_id=model_decision_id,
            decided_by=model_decided_by,
        ),
        mappings=proposals,
        created_at=NOW,
        updated_at=NOW,
    )
