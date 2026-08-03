"""Pure M34 registry-v2 assembly and authorization tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from schemabridge.adapters.semantic_registry.datahub import (
    DataHubObservedSemanticRegistryPublisher,
    DataHubRegistryDocument,
    DataHubRegistryDocumentWrite,
    DataHubRegistryIdentity,
    DataHubRegistryReadConfig,
    DataHubRegistryWriteConfig,
    DataHubSemanticRegistryPublisher,
)
from schemabridge.adapters.semantic_registry.datahub_control import DataHubRegistryVersionReader
from schemabridge.application.ports.planning import (
    RegistryPublicationError,
    RegistryPublicationErrorCode,
)
from schemabridge.application.ports.registry_control import (
    RegistryControlError,
    RegistryControlErrorCode,
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
from schemabridge.domain.mappings import ConfidenceScore
from schemabridge.domain.registry_control import RegistryVersionTrust
from schemabridge.domain.registry_publication import (
    RegistryPublicationAuthorization,
    RegistryPublicationAuthorizationConfirmation,
    assemble_publishable_registry_version,
    validate_registry_publication_authorization,
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
    PhysicalValueType,
    RegistryArtifactKind,
    SemanticRegistryScope,
)
from schemabridge.domain.transformations import IdentityStep, TransformationPlan

NOW = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
FP_A = "a" * 64
FP_B = "b" * 64
FP_C = "c" * 64
FP_D = "d" * 64
OPAQUE_ORDERS_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,opaque-9f82,PROD)"
OPAQUE_CUSTOMERS_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,random-c7e1,PROD)"
WRITER_ACTOR = "urn:li:corpuser:schemabridge-registry-publisher"


@dataclass(slots=True)
class _FakeDataHubV2Client:
    document: DataHubRegistryDocument | None = None
    identity_value: DataHubRegistryIdentity = field(
        default_factory=lambda: DataHubRegistryIdentity(
            actor_urn=WRITER_ACTOR,
            granted_platform_mutation_privileges=frozenset({"manageDocuments"}),
            granted_target_edit_privileges=frozenset(),
        )
    )
    after_upsert: Callable[[DataHubRegistryDocument], DataHubRegistryDocument] | None = None
    upserts: list[DataHubRegistryDocumentWrite] = field(default_factory=list)

    def identity(self, target_urn: str) -> DataHubRegistryIdentity:
        del target_urn
        return self.identity_value

    def get_document(self, urn: str) -> DataHubRegistryDocument | None:
        del urn
        return self.document

    def upsert_document(self, document: DataHubRegistryDocumentWrite) -> None:
        self.upserts.append(document)
        stored = DataHubRegistryDocument(
            urn=f"urn:li:document:{document.document_id}",
            title=document.title,
            text=document.text,
            custom_properties=dict(document.custom_properties),
            related_asset_urns=document.related_asset_urns,
            removed=False,
        )
        self.document = self.after_upsert(stored) if self.after_upsert is not None else stored


def test_empty_base_assembles_complete_v2_with_truthful_zero_join_provenance() -> None:
    proposal = _proposal()

    candidate = assemble_publishable_registry_version(proposal, base=None)
    registry = candidate.registry

    assert registry.format_version == 2
    assert registry.version == 1
    assert registry.source.startswith("datahub:schemabridge-semantic-registry-")
    assert registry.join_contracts.contracts == ()
    provenance = {item.kind: item for item in registry.provenance}
    assert provenance[RegistryArtifactKind.JOIN_CONTRACTS].decision_ids == ()
    assert candidate.active_decision_ids == ("decision-mapping-order", "decision-model-order")
    assert registry.physical_bindings[0].observed_datahub_asset_urn == OPAQUE_ORDERS_URN
    assert "sales.orders" not in registry.physical_bindings[0].observed_datahub_asset_urn


def test_missing_or_forged_observed_urn_fails_before_a_publishable_candidate_exists() -> None:
    without_urn = _proposal(observed_urn=None, asset_id="catalog-native-orders")

    with pytest.raises(ValueError, match="requires an observed DataHub asset URN"):
        assemble_publishable_registry_version(without_urn, base=None)

    with pytest.raises(ValidationError, match="must equal the observed"):
        _proposal(
            observed_urn=OPAQUE_CUSTOMERS_URN,
            asset_id=OPAQUE_ORDERS_URN,
        )


def test_additive_merge_preserves_base_and_rejects_v1_or_model_collision() -> None:
    first = assemble_publishable_registry_version(_proposal(), base=None).registry
    second_proposal = _proposal(
        model_id="Customer",
        logical_field="Customer.customer_id",
        physical_field="crm.customers.customer_id",
        proposal_id="proposal-customers-v2",
        draft_id="customers-onboarding",
        model_decision="decision-model-customer",
        mapping_decision="decision-mapping-customer",
        observed_urn=OPAQUE_CUSTOMERS_URN,
        asset_id=OPAQUE_CUSTOMERS_URN,
        base=OnboardingRegistryBase(
            registry_version=1,
            registry_fingerprint=first.fingerprint,
            activation_generation=3,
            active_pointer_fingerprint=FP_D,
        ),
    )

    second = assemble_publishable_registry_version(second_proposal, base=first)

    assert tuple(model.id.root for model in second.registry.logical_context.models) == (
        "Order",
        "Customer",
    )
    assert len(second.registry.mapping_set.mappings) == 2
    assert len(second.registry.physical_bindings) == 2
    assert second.registry.join_contracts.contracts == ()

    v1 = GovernedSemanticRegistrySnapshot.model_validate(
        {
            **first.model_dump(mode="python"),
            "format_version": 1,
            "physical_bindings": (),
        }
    )
    v1_base = second_proposal.model_copy(
        update={
            "base_registry": OnboardingRegistryBase(
                registry_version=1,
                registry_fingerprint=v1.fingerprint,
                activation_generation=3,
                active_pointer_fingerprint=FP_D,
            )
        }
    )
    v1_base = PreparedSemanticOnboardingProposal.create(
        id=v1_base.id,
        draft=_approved_draft_from_proposal(v1_base),
        decision_ids=v1_base.decision_ids,
        prepared_by=v1_base.prepared_by,
        prepared_at=v1_base.prepared_at,
    )
    with pytest.raises(ValueError, match="lacks v2 authority"):
        assemble_publishable_registry_version(v1_base, base=v1)

    collision = _proposal(
        base=OnboardingRegistryBase(
            registry_version=1,
            registry_fingerprint=first.fingerprint,
            activation_generation=3,
            active_pointer_fingerprint=FP_D,
        ),
        proposal_id="proposal-orders-v2",
    )
    with pytest.raises(ValueError, match="cannot replace an active logical model"):
        assemble_publishable_registry_version(collision, base=first)


def test_rejected_review_decision_never_enters_active_registry_provenance() -> None:
    approved = _mapping()
    rejected = _mapping(
        mapping_id="mapping-order-rejected",
        decision_id="decision-mapping-rejected",
        status=ApprovalStatus.REJECTED,
        observed_urn=OPAQUE_CUSTOMERS_URN,
        asset_id=OPAQUE_CUSTOMERS_URN,
        physical_field="archive.orders.order_id",
    )
    proposal = _proposal(mappings=(approved, rejected))

    candidate = assemble_publishable_registry_version(proposal, base=None)

    assert "decision-mapping-rejected" in candidate.review_decision_ids
    assert "decision-mapping-rejected" not in candidate.active_decision_ids
    assert len(candidate.registry.mapping_set.mappings) == 1


def test_authorization_is_fresh_exact_and_bound_to_post_assembly_fingerprint() -> None:
    candidate = assemble_publishable_registry_version(_proposal(), base=None)
    authorization = RegistryPublicationAuthorization.create(
        candidate,
        actor_id="publisher-b",
        authenticated_at=NOW - timedelta(minutes=2),
        approved_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        confirmation=(
            RegistryPublicationAuthorizationConfirmation.PUBLISH_EXACT_OBSERVED_REGISTRY_VERSION
        ),
    )

    validate_registry_publication_authorization(
        candidate,
        authorization,
        at=NOW + timedelta(minutes=1),
    )
    with pytest.raises(ValueError, match="does not match"):
        validate_registry_publication_authorization(
            candidate,
            authorization.model_copy(update={"registry_fingerprint": FP_D}),
        )


def test_v2_publisher_uses_observed_urn_and_strict_reader_reconstructs_exact_version() -> None:
    candidate = assemble_publishable_registry_version(_proposal(), base=None)
    authorization = _authorization(candidate)
    client = _FakeDataHubV2Client()

    result = _v2_publisher(client).publish(candidate, authorization, observed_at=NOW)

    assert result.status == "published"
    assert result.observed_authorization_id == authorization.id
    assert result.receipt is not None
    assert result.receipt.related_asset_urns == (OPAQUE_ORDERS_URN,)
    assert client.upserts[0].related_asset_urns == (OPAQUE_ORDERS_URN,)
    assert "sales.orders" not in client.upserts[0].related_asset_urns[0]
    client.identity_value = DataHubRegistryIdentity(
        actor_urn="urn:li:corpuser:schemabridge-registry-reader",
        granted_platform_mutation_privileges=frozenset(),
        granted_target_edit_privileges=frozenset(),
    )
    reader = DataHubRegistryVersionReader(
        config=DataHubRegistryReadConfig(
            server="http://127.0.0.1:1",
            token="synthetic-reader-token",
        ),
        client=client,
    )
    loaded = reader.load_version(candidate.scope, candidate.registry.version)
    assert loaded.snapshot.registry == candidate.registry
    assert loaded.publication_approval_id == authorization.id
    assert loaded.trust is RegistryVersionTrust.STRICT


def test_v2_replay_reports_authorization_observed_at_existing_target() -> None:
    candidate = assemble_publishable_registry_version(_proposal(), base=None)
    first_authorization = _authorization(candidate)
    client = _FakeDataHubV2Client()
    publisher = _v2_publisher(client)
    publisher.publish(candidate, first_authorization, observed_at=NOW)
    second_authorization = _authorization(
        candidate,
        actor="publisher-c",
        approved_at=NOW + timedelta(seconds=30),
    )

    replay = publisher.publish(
        candidate,
        second_authorization,
        observed_at=NOW + timedelta(seconds=30),
    )

    assert replay.status == "already_current"
    assert replay.attempt_authorization_id == second_authorization.id
    assert replay.observed_authorization_id == first_authorization.id
    assert len(client.upserts) == 1


def test_v2_expired_retry_can_read_back_existing_but_cannot_create_missing_target() -> None:
    candidate = assemble_publishable_registry_version(_proposal(), base=None)
    authorization = _authorization(candidate)
    populated = _FakeDataHubV2Client()
    publisher = _v2_publisher(populated)
    publisher.publish(candidate, authorization, observed_at=NOW)

    recovered = publisher.publish(
        candidate,
        authorization,
        observed_at=authorization.expires_at + timedelta(seconds=1),
    )

    assert recovered.status == "already_current"
    assert recovered.observed_authorization_id == authorization.id
    missing = _FakeDataHubV2Client()
    with pytest.raises(RegistryPublicationError) as raised:
        _v2_publisher(missing).publish(
            candidate,
            authorization,
            observed_at=authorization.expires_at + timedelta(seconds=1),
        )
    assert raised.value.code is RegistryPublicationErrorCode.APPROVAL_MISMATCH
    assert missing.upserts == []


def test_v2_readback_rejects_related_asset_tampering_and_legacy_writer_rejects_v2() -> None:
    candidate = assemble_publishable_registry_version(_proposal(), base=None)
    authorization = _authorization(candidate)
    client = _FakeDataHubV2Client()
    _v2_publisher(client).publish(candidate, authorization, observed_at=NOW)
    assert client.document is not None
    client.document = replace(client.document, related_asset_urns=(OPAQUE_CUSTOMERS_URN,))

    reader = DataHubRegistryVersionReader(
        config=DataHubRegistryReadConfig(
            server="http://127.0.0.1:1",
            token="synthetic-reader-token",
        ),
        client=client,
    )
    with pytest.raises(RegistryControlError) as raised:
        reader.load_version(candidate.scope, 1)
    assert raised.value.code is RegistryControlErrorCode.VERSION_INVALID

    legacy = DataHubSemanticRegistryPublisher(
        config=DataHubRegistryWriteConfig(
            server="http://127.0.0.1:1",
            token="synthetic-writer-token",
            actor_urn=WRITER_ACTOR,
        ),
        client=_FakeDataHubV2Client(),
    )
    with pytest.raises(RegistryPublicationError) as legacy_error:
        legacy.publish(candidate.registry, None)  # type: ignore[arg-type]
    assert legacy_error.value.code is RegistryPublicationErrorCode.PAYLOAD_INVALID


def test_v2_post_write_tampering_is_a_failed_attempt_not_false_success() -> None:
    candidate = assemble_publishable_registry_version(_proposal(), base=None)
    authorization = _authorization(candidate)
    client = _FakeDataHubV2Client(
        after_upsert=lambda document: replace(
            document,
            related_asset_urns=(OPAQUE_CUSTOMERS_URN,),
        )
    )

    result = _v2_publisher(client).publish(candidate, authorization, observed_at=NOW)

    assert result.status == "failed"
    assert result.reason_code == "post_write_verification_failed"
    assert result.receipt is None
    with pytest.raises(ValueError, match="does not match"):
        validate_registry_publication_authorization(
            candidate,
            authorization,
            at=authorization.expires_at,
        )


def test_v2_writer_rejects_target_edit_grants_before_read_or_upsert() -> None:
    candidate = assemble_publishable_registry_version(_proposal(), base=None)
    authorization = _authorization(candidate)
    client = _FakeDataHubV2Client(
        identity_value=DataHubRegistryIdentity(
            actor_urn=WRITER_ACTOR,
            granted_platform_mutation_privileges=frozenset({"manageDocuments"}),
            granted_target_edit_privileges=frozenset({"canEditProperties"}),
        )
    )

    with pytest.raises(RegistryPublicationError) as raised:
        _v2_publisher(client).observe(candidate, authorization, observed_at=NOW)

    assert raised.value.code is RegistryPublicationErrorCode.CATALOG_PERMISSION_DENIED
    assert client.upserts == []


def test_observed_result_rejects_audit_or_receipt_that_is_not_exactly_bound() -> None:
    candidate = assemble_publishable_registry_version(_proposal(), base=None)
    authorization = _authorization(candidate)
    result = _v2_publisher(_FakeDataHubV2Client()).publish(
        candidate,
        authorization,
        observed_at=NOW,
    )

    wrong_outcome = result.model_dump(mode="python")
    wrong_outcome["status"] = "already_current"
    with pytest.raises(ValidationError, match="result is inconsistent"):
        type(result).model_validate(wrong_outcome)

    wrong_target = result.model_dump(mode="python")
    wrong_target["audit_record"]["target"] = "urn:li:document:another-target"
    with pytest.raises(ValidationError, match="result is inconsistent"):
        type(result).model_validate(wrong_target)


def _proposal(
    *,
    model_id: str = "Order",
    logical_field: str = "Order.order_id",
    physical_field: str = "sales.orders.order_id",
    proposal_id: str = "proposal-orders-v1",
    draft_id: str = "orders-onboarding",
    model_decision: str = "decision-model-order",
    mapping_decision: str = "decision-mapping-order",
    observed_urn: str | None = OPAQUE_ORDERS_URN,
    asset_id: str = OPAQUE_ORDERS_URN,
    base: OnboardingRegistryBase | None = None,
    mappings: tuple[SemanticMappingProposal, ...] | None = None,
    scope: SemanticRegistryScope | None = None,
    catalog_generation: int = 7,
    catalog_generation_fingerprint: str = FP_A,
) -> PreparedSemanticOnboardingProposal:
    resolved_scope = scope or SemanticRegistryScope(
        workspace_id="workspace-a",
        catalog_scope="postgres.production",
        registry_id="orders_registry",
    )
    definition = SemanticModelDefinition(
        id=LogicalModelRef(model_id),
        description=f"Governed {model_id} business model.",
        fields=(
            SemanticFieldDefinition(
                id=LogicalFieldRef(logical_field),
                canonical_type=CanonicalType.STRING,
                role=LogicalFieldRole.IDENTIFIER,
                definition=f"Stable {model_id} identifier.",
            ),
        ),
    )
    resolved_mappings = mappings or (
        _mapping(
            mapping_id=f"mapping-{model_id.lower()}-id",
            logical_field=logical_field,
            physical_field=physical_field,
            decision_id=mapping_decision,
            observed_urn=observed_urn,
            asset_id=asset_id,
            workspace_id=resolved_scope.workspace_id,
            catalog_scope=resolved_scope.catalog_scope,
            catalog_generation=catalog_generation,
            catalog_generation_fingerprint=catalog_generation_fingerprint,
        ),
    )
    draft = SemanticOnboardingDraft(
        id=draft_id,
        workspace_id=resolved_scope.workspace_id,
        owner_actor_id="analyst-a",
        scope=resolved_scope,
        connection_id=CatalogConnectionId("warehouse-a"),
        catalog_generation=catalog_generation,
        catalog_generation_fingerprint=catalog_generation_fingerprint,
        base_registry=base or OnboardingRegistryBase(),
        model=SemanticModelProposal(
            definition=definition,
            status=ApprovalStatus.APPROVED,
            decision_id=model_decision,
            decided_by="steward-a",
        ),
        mappings=resolved_mappings,
        created_at=NOW,
        updated_at=NOW,
    )
    decision_ids = tuple(
        sorted({model_decision, *(mapping.decision_id for mapping in resolved_mappings)})
    )
    return PreparedSemanticOnboardingProposal.create(
        id=proposal_id,
        draft=draft,
        decision_ids=decision_ids,
        prepared_by="publisher-a",
        prepared_at=NOW,
    )


def _mapping(
    *,
    mapping_id: str = "mapping-order-id",
    logical_field: str = "Order.order_id",
    physical_field: str = "sales.orders.order_id",
    decision_id: str = "decision-mapping-order",
    status: ApprovalStatus = ApprovalStatus.APPROVED,
    observed_urn: str | None = OPAQUE_ORDERS_URN,
    asset_id: str = OPAQUE_ORDERS_URN,
    workspace_id: str = "workspace-a",
    catalog_scope: str = "postgres.production",
    catalog_generation: int = 7,
    catalog_generation_fingerprint: str = FP_A,
) -> SemanticMappingProposal:
    column = physical_field.rsplit(".", 1)[-1]
    return SemanticMappingProposal(
        id=mapping_id,
        logical_field=LogicalFieldRef(logical_field),
        observation=PhysicalCatalogObservation(
            locator=CatalogFieldLocator(
                asset=CatalogAssetLocator(
                    workspace_id=workspace_id,
                    connection_id=CatalogConnectionId("warehouse-a"),
                    asset_id=CatalogAssetId(asset_id),
                ),
                field_path=(column,),
            ),
            catalog_scope=catalog_scope,
            generation=catalog_generation,
            generation_fingerprint=catalog_generation_fingerprint,
            asset_metadata_fingerprint=FP_B,
            field_metadata_fingerprint=FP_C,
            physical_field=PhysicalFieldRef(physical_field),
            physical_type=PhysicalValueType.STRING,
            observed_datahub_asset_urn=observed_urn,
        ),
        confidence=ConfidenceScore(0.98),
        evidence=(
            OnboardingEvidence(
                kind=OnboardingEvidenceKind.DECLARED_KEY,
                detail="Catalog declares this exact field as a stable key.",
                reference="catalog:retained-generation-7",
            ),
        ),
        risks=("Identifier ownership was reviewed by the steward.",),
        transformation_plan=TransformationPlan(steps=(IdentityStep(),)),
        status=status,
        decision_id=decision_id,
        decided_by="steward-a",
    )


def _approved_draft_from_proposal(
    proposal: PreparedSemanticOnboardingProposal,
) -> SemanticOnboardingDraft:
    return SemanticOnboardingDraft(
        id=proposal.draft_id,
        workspace_id=proposal.workspace_id,
        owner_actor_id="analyst-a",
        scope=proposal.scope,
        connection_id=proposal.mappings[0].observation.locator.asset.connection_id,
        catalog_generation=proposal.mappings[0].observation.generation,
        catalog_generation_fingerprint=proposal.mappings[0].observation.generation_fingerprint,
        base_registry=proposal.base_registry,
        model=proposal.model,
        mappings=proposal.mappings,
        created_at=NOW,
        updated_at=NOW,
    )


def _authorization(
    candidate: object,
    *,
    actor: str = "publisher-b",
    approved_at: datetime = NOW,
) -> RegistryPublicationAuthorization:
    from schemabridge.domain.registry_publication import PublishableRegistryVersion

    assert isinstance(candidate, PublishableRegistryVersion)
    return RegistryPublicationAuthorization.create(
        candidate,
        actor_id=actor,
        authenticated_at=approved_at - timedelta(minutes=1),
        approved_at=approved_at,
        expires_at=approved_at + timedelta(minutes=10),
        confirmation=(
            RegistryPublicationAuthorizationConfirmation.PUBLISH_EXACT_OBSERVED_REGISTRY_VERSION
        ),
    )


def _v2_publisher(client: _FakeDataHubV2Client) -> DataHubObservedSemanticRegistryPublisher:
    return DataHubObservedSemanticRegistryPublisher(
        config=DataHubRegistryWriteConfig(
            server="http://127.0.0.1:1",
            token="synthetic-writer-token",
            actor_urn=WRITER_ACTOR,
        ),
        client=client,
    )
