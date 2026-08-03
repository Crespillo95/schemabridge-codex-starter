"""Exact M34 proposal, catalog and active-base authority tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from tests.unit.test_registry_join_changes import _approved_draft, _two_model_base
from tests.unit.test_registry_model_change_authoring import _source_decisions
from tests.unit.test_registry_model_changes import (
    NOW as MODEL_NOW,
)
from tests.unit.test_registry_model_changes import (
    _blocking_report,
    _join_upsert,
    _joined_base,
    _outer_decision,
    _replacement,
    _replacement_base,
)
from tests.unit.test_registry_publication_v2 import FP_A, FP_D, _proposal

from schemabridge.adapters.semantic_onboarding.publication_authority import (
    ExactRegistryPublicationAuthority,
)
from schemabridge.application.ports.registry_publication import (
    RegistryPublicationAuthorityError,
)
from schemabridge.application.ports.semantic_onboarding import (
    SemanticOnboardingPortError,
    SemanticOnboardingPortErrorCode,
)
from schemabridge.domain.registry_changes import PreparedRegistryJoinProposal
from schemabridge.domain.registry_control import (
    ActiveRegistryPointer,
    GovernedRegistryVersion,
    RegistryVersionTrust,
    registry_projection_fingerprint,
)
from schemabridge.domain.registry_model_change_authoring import (
    RegistryModelReplacementSourceEvidence,
    replacement_source_decisions,
)
from schemabridge.domain.registry_model_changes import (
    PreparedRegistryModelReplacementProposal,
    RegistryIncidentJoinPreservation,
    RegistryIncidentJoinUpsert,
    RegistryModelChangeAuthority,
    RegistryModelJoinProfileWitness,
)
from schemabridge.domain.registry_publication_jobs import (
    PreparedRegistryPublicationProposal,
    RegistryPublicationFailureCode,
)
from schemabridge.domain.semantic_onboarding import (
    OnboardingCatalogGeneration,
    OnboardingRegistryBase,
    PhysicalCatalogObservation,
    PreparedSemanticOnboardingProposal,
    ResolvedOnboardingCatalogEvidence,
    SemanticOnboardingMappingInput,
)
from schemabridge.domain.semantic_registry import (
    GovernedSemanticRegistrySnapshot,
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
    datahub_registry_document_urn,
    semantic_registry_decision_ids,
)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


@dataclass(slots=True)
class _Proposals:
    proposal: PreparedRegistryPublicationProposal | None

    def load_exact(
        self,
        workspace_id: str,
        proposal_id: str,
    ) -> PreparedRegistryPublicationProposal | None:
        if self.proposal is None:
            return None
        assert workspace_id == self.proposal.workspace_id
        assert proposal_id == self.proposal.id
        return self.proposal


@dataclass(slots=True)
class _Catalog:
    proposal: PreparedSemanticOnboardingProposal
    error: SemanticOnboardingPortError | None = None
    drift: bool = False
    calls: int = 0

    def resolve_exact(
        self,
        scope: SemanticRegistryScope,
        connection_id: object,
        generation: int,
        expected_generation_fingerprint: str,
        selections: tuple[SemanticOnboardingMappingInput, ...],
    ) -> ResolvedOnboardingCatalogEvidence:
        del connection_id, selections
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert scope == self.proposal.scope
        assert generation == 7
        assert expected_generation_fingerprint == FP_A
        observations = tuple(mapping.observation for mapping in self.proposal.mappings)
        if self.drift:
            observations = (
                observations[0].model_copy(update={"field_metadata_fingerprint": "0" * 64}),
                *observations[1:],
            )
        return ResolvedOnboardingCatalogEvidence(
            generation=OnboardingCatalogGeneration(
                workspace_id=scope.workspace_id,
                connection_id=self.proposal.mappings[0].observation.locator.asset.connection_id,
                catalog_scope=scope.catalog_scope,
                generation=generation,
                inventory_fingerprint=expected_generation_fingerprint,
                enabled=True,
                stale=False,
            ),
            observations=observations,
        )


@dataclass(slots=True)
class _JoinCatalog:
    proposal: PreparedRegistryJoinProposal
    drift: bool = False
    calls: int = 0

    def resolve_exact(
        self,
        scope: SemanticRegistryScope,
        connection_id: object,
        generation: int,
        expected_generation_fingerprint: str,
        selections: tuple[SemanticOnboardingMappingInput, ...],
    ) -> ResolvedOnboardingCatalogEvidence:
        self.calls += 1
        bindings = (
            self.proposal.base_evidence.left.binding,
            self.proposal.base_evidence.right.binding,
        )
        first = bindings[0]
        assert scope == self.proposal.scope
        assert connection_id == first.connection_id
        assert generation == first.catalog_generation
        assert expected_generation_fingerprint == first.catalog_generation_fingerprint
        assert tuple(selection.asset_id for selection in selections) == tuple(
            binding.locator.asset.asset_id for binding in bindings
        )
        observations = tuple(
            PhysicalCatalogObservation(
                locator=binding.locator,
                catalog_scope=binding.catalog_scope,
                generation=binding.catalog_generation,
                generation_fingerprint=binding.catalog_generation_fingerprint,
                asset_metadata_fingerprint=binding.asset_metadata_fingerprint,
                field_metadata_fingerprint=(
                    "0" * 64 if self.drift and index == 0 else binding.field_metadata_fingerprint
                ),
                physical_field=binding.physical_field,
                physical_type=binding.physical_type,
                observed_datahub_asset_urn=binding.observed_datahub_asset_urn,
            )
            for index, binding in enumerate(bindings)
        )
        return ResolvedOnboardingCatalogEvidence(
            generation=OnboardingCatalogGeneration(
                workspace_id=scope.workspace_id,
                connection_id=first.connection_id,
                catalog_scope=scope.catalog_scope,
                generation=generation,
                inventory_fingerprint=expected_generation_fingerprint,
                enabled=True,
                stale=False,
            ),
            observations=observations,
        )


@dataclass(slots=True)
class _Pointers:
    pointer: ActiveRegistryPointer | None = None

    def load_active(self, scope: SemanticRegistryScope) -> ActiveRegistryPointer | None:
        del scope
        return self.pointer


@dataclass(slots=True)
class _Versions:
    version: GovernedRegistryVersion | None = None

    def load_version(
        self,
        scope: SemanticRegistryScope,
        version: int,
    ) -> GovernedRegistryVersion:
        del scope, version
        if self.version is None:
            raise AssertionError("an empty active base must not load a DataHub version")
        return self.version


@dataclass(slots=True)
class _PhysicalBindings:
    calls: int = 0

    def require_current(self, scope: object, bindings: tuple[object, ...]) -> None:
        del scope, bindings
        self.calls += 1


@dataclass(slots=True)
class _ModelSources:
    source: RegistryModelReplacementSourceEvidence | None

    def load(
        self,
        workspace_id: str,
        proposal_id: str,
    ) -> RegistryModelReplacementSourceEvidence | None:
        if self.source is None:
            return None
        assert workspace_id == self.source.proposal.workspace_id
        assert proposal_id == self.source.proposal.id
        return self.source


@dataclass(slots=True)
class _Dependencies:
    state: object

    def load_state(self, scope: object) -> object:
        del scope
        return self.state

    def resolve_impacts(self, context: object, findings: object) -> object:
        del context, findings
        raise AssertionError("publication authority must not recompute remediation impacts")


@dataclass(slots=True)
class _Remediation:
    value: object | None = None

    def load_current(self, scope: object, report_id: str, expected_context: object) -> object:
        del scope
        if self.value is None:
            return None
        report, _impacts = self.value  # type: ignore[misc]
        if report.id != report_id or report.context != expected_context:
            return None
        return self.value


@dataclass(slots=True)
class _Witnesses:
    witness: RegistryModelJoinProfileWitness | None = None

    def load(
        self,
        workspace_id: str,
        change_id: str,
        replacement_proposal_id: str,
        join_id: str,
    ) -> RegistryModelJoinProfileWitness | None:
        del workspace_id, change_id, replacement_proposal_id, join_id
        return self.witness


def test_authority_accepts_only_the_exact_retained_empty_base() -> None:
    proposal = _proposal()

    base = _authority(proposal).resolve_base(proposal)

    assert base is None


def test_authority_rejects_missing_or_changed_proposal() -> None:
    proposal = _proposal()
    missing = _authority(proposal, persisted=None)
    with pytest.raises(RegistryPublicationAuthorityError) as missing_error:
        missing.resolve_base(proposal)
    assert missing_error.value.code is RegistryPublicationFailureCode.PROPOSAL_UNAVAILABLE

    changed = proposal.model_copy(update={"prepared_by": "another-publisher"})
    with pytest.raises(RegistryPublicationAuthorityError) as changed_error:
        _authority(proposal, persisted=changed).resolve_base(proposal)
    assert changed_error.value.code is RegistryPublicationFailureCode.PROPOSAL_STALE


def test_authority_classifies_catalog_drift_and_transient_outage_separately() -> None:
    proposal = _proposal()
    unavailable = SemanticOnboardingPortError(
        SemanticOnboardingPortErrorCode.UNAVAILABLE,
        "sensitive backend detail",
    )
    with pytest.raises(RegistryPublicationAuthorityError) as retryable:
        _authority(proposal, catalog_error=unavailable).resolve_base(proposal)
    assert retryable.value.code is RegistryPublicationFailureCode.BASE_UNAVAILABLE
    assert "sensitive" not in str(retryable.value)

    stale = SemanticOnboardingPortError(
        SemanticOnboardingPortErrorCode.RESOURCE_UNAVAILABLE,
        "sensitive backend detail",
    )
    with pytest.raises(RegistryPublicationAuthorityError) as terminal:
        _authority(proposal, catalog_error=stale).resolve_base(proposal)
    assert terminal.value.code is RegistryPublicationFailureCode.CATALOG_STALE


def test_authority_rejects_a_base_pointer_that_disappeared_before_publication() -> None:
    proposal = _proposal(
        base=OnboardingRegistryBase(
            registry_version=3,
            registry_fingerprint=FP_D,
            activation_generation=4,
            active_pointer_fingerprint=FP_A,
        )
    )

    with pytest.raises(RegistryPublicationAuthorityError) as raised:
        _authority(proposal).resolve_base(proposal)

    assert raised.value.code is RegistryPublicationFailureCode.BASE_STALE


def test_authority_accepts_exact_join_proposal_with_join_catalog_resolution() -> None:
    proposal, base, pointer, version = _join_authority_state()
    catalog = _JoinCatalog(proposal)
    physical_bindings = _PhysicalBindings()
    authority = ExactRegistryPublicationAuthority(
        proposals=_Proposals(proposal),
        catalog=catalog,
        pointers=_Pointers(pointer),
        versions=_Versions(version),
        physical_bindings=physical_bindings,  # type: ignore[arg-type]
    )

    resolved = authority.resolve_base(proposal)

    assert resolved == base
    assert catalog.calls == 1
    assert physical_bindings.calls == 1


def test_authority_rejects_join_base_pointer_drift_before_version_read() -> None:
    proposal, _base, pointer, version = _join_authority_state()
    authority = ExactRegistryPublicationAuthority(
        proposals=_Proposals(proposal),
        catalog=_JoinCatalog(proposal),
        pointers=_Pointers(pointer.model_copy(update={"generation": pointer.generation + 1})),
        versions=_Versions(version),
        physical_bindings=_PhysicalBindings(),  # type: ignore[arg-type]
    )

    with pytest.raises(RegistryPublicationAuthorityError) as raised:
        authority.resolve_base(proposal)

    assert raised.value.code is RegistryPublicationFailureCode.BASE_STALE


def test_authority_rejects_join_catalog_drift_before_pointer_or_version_use() -> None:
    proposal, _base, pointer, version = _join_authority_state()
    catalog = _JoinCatalog(proposal, drift=True)
    authority = ExactRegistryPublicationAuthority(
        proposals=_Proposals(proposal),
        catalog=catalog,
        pointers=_Pointers(pointer),
        versions=_Versions(version),
        physical_bindings=_PhysicalBindings(),  # type: ignore[arg-type]
    )

    with pytest.raises(RegistryPublicationAuthorityError) as raised:
        authority.resolve_base(proposal)

    assert raised.value.code is RegistryPublicationFailureCode.CATALOG_STALE
    assert catalog.calls == 1


def test_authority_accepts_exact_model_replacement_source_catalog_and_dependencies() -> None:
    proposal, base, pointer, version, source = _model_replacement_authority_state()
    authority = _model_authority(
        proposal,
        pointer,
        version,
        source,
    )

    resolved = authority.resolve_base(proposal)

    assert proposal.owner_actor_id != source.owner_actor_id
    assert resolved == base


def test_authority_accepts_current_remediation_and_latest_join_witness() -> None:
    proposal, base, pointer, version, source = _model_replacement_authority_state(
        remediation=True,
        upsert=True,
    )
    authority = _model_authority(
        proposal,
        pointer,
        version,
        source,
    )

    resolved = authority.resolve_base(proposal)

    assert resolved == base


def test_authority_rejects_model_replacement_source_or_candidate_catalog_drift() -> None:
    proposal, _base, pointer, version, source = _model_replacement_authority_state()
    missing_source = _model_authority(
        proposal,
        pointer,
        version,
        None,
    )
    with pytest.raises(RegistryPublicationAuthorityError) as missing:
        missing_source.resolve_base(proposal)
    assert missing.value.code is RegistryPublicationFailureCode.PROPOSAL_STALE

    stale_catalog = _model_authority(
        proposal,
        pointer,
        version,
        source,
        catalog_drift=True,
    )
    with pytest.raises(RegistryPublicationAuthorityError) as stale:
        stale_catalog.resolve_base(proposal)
    assert stale.value.code is RegistryPublicationFailureCode.CATALOG_STALE


def test_authority_rejects_model_replacement_dependency_drift() -> None:
    proposal, _base, pointer, version, source = _model_replacement_authority_state()
    drifted = proposal.base.dependency_context.dependency_index.model_copy(
        update={"watermark": proposal.base.dependency_context.dependency_index.watermark + 1}
    )
    authority = _model_authority(
        proposal,
        pointer,
        version,
        source,
        dependency_state=drifted,
    )

    with pytest.raises(RegistryPublicationAuthorityError) as raised:
        authority.resolve_base(proposal)

    assert raised.value.code is RegistryPublicationFailureCode.BASE_STALE


def test_authority_rejects_superseded_model_remediation_report() -> None:
    proposal, _base, pointer, version, source = _model_replacement_authority_state(remediation=True)
    authority = _model_authority(
        proposal,
        pointer,
        version,
        source,
        remediation=None,
    )

    with pytest.raises(RegistryPublicationAuthorityError) as raised:
        authority.resolve_base(proposal)

    assert raised.value.code is RegistryPublicationFailureCode.BASE_STALE


def test_authority_rejects_a_newer_model_join_profile_witness() -> None:
    proposal, _base, pointer, version, source = _model_replacement_authority_state(upsert=True)
    change = proposal.incident_join_changes[0]
    assert isinstance(change, RegistryIncidentJoinUpsert)
    embedded = change.profile_witness
    latest = RegistryModelJoinProfileWitness.create(
        change_id=proposal.draft_id,
        base=proposal.base,
        replacement=proposal.replacement,
        incident=change.base,
        proposal=embedded.proposal,
        execution_target=embedded.execution_target,
        profile=embedded.result.profile,
        completed_at=embedded.result.completed_at + timedelta(minutes=1),
    )
    authority = _model_authority(
        proposal,
        pointer,
        version,
        source,
        witness=latest,
    )

    with pytest.raises(RegistryPublicationAuthorityError) as raised:
        authority.resolve_base(proposal)

    assert raised.value.code is RegistryPublicationFailureCode.PROPOSAL_STALE


def _authority(
    proposal: PreparedSemanticOnboardingProposal,
    *,
    persisted: PreparedSemanticOnboardingProposal | object | None = ...,
    catalog_error: SemanticOnboardingPortError | None = None,
) -> ExactRegistryPublicationAuthority:
    resolved = proposal if persisted is ... else persisted
    assert resolved is None or isinstance(resolved, PreparedSemanticOnboardingProposal)
    return ExactRegistryPublicationAuthority(
        proposals=_Proposals(resolved),
        catalog=_Catalog(proposal, catalog_error),
        pointers=_Pointers(),
        versions=_Versions(),
        physical_bindings=_PhysicalBindings(),  # type: ignore[arg-type]
    )


def _join_authority_state() -> tuple[
    PreparedRegistryJoinProposal,
    GovernedSemanticRegistrySnapshot,
    ActiveRegistryPointer,
    GovernedRegistryVersion,
]:
    base_value, _base_identity = _two_model_base()
    assert isinstance(base_value, GovernedSemanticRegistrySnapshot)
    scope = _proposal().scope
    pointer = ActiveRegistryPointer(
        scope=scope,
        generation=4,
        registry_version=base_value.version,
        registry_fingerprint=base_value.fingerprint,
        registry_target=datahub_registry_document_urn(scope, base_value.version),
        transition_id="transition-join-base-v2",
        activated_by="publisher-base",
        activated_at=NOW,
        decision_ids=semantic_registry_decision_ids(base_value),
    )
    base_identity = OnboardingRegistryBase(
        registry_version=base_value.version,
        registry_fingerprint=base_value.fingerprint,
        activation_generation=pointer.generation,
        active_pointer_fingerprint=registry_projection_fingerprint(pointer),
    )
    proposal = PreparedRegistryJoinProposal.create(
        id="join-change-authority-v3",
        draft=_approved_draft(base_value, base_identity),
        prepared_by="publisher-authority",
        prepared_at=NOW + timedelta(minutes=4),
    )
    version = GovernedRegistryVersion(
        snapshot=ScopedSemanticRegistrySnapshot(scope=scope, registry=base_value),
        publication_approval_id="publication-join-base-v2",
        trust=RegistryVersionTrust.STRICT,
    )
    return proposal, base_value, pointer, version


def _model_replacement_authority_state(
    *,
    remediation: bool = False,
    upsert: bool = False,
) -> tuple[
    PreparedRegistryModelReplacementProposal,
    GovernedSemanticRegistrySnapshot,
    ActiveRegistryPointer,
    GovernedRegistryVersion,
    RegistryModelReplacementSourceEvidence,
]:
    base, _ignored_identity = _joined_base()
    scope = SemanticRegistryScope(
        workspace_id=base.physical_bindings[0].workspace_id,
        catalog_scope=base.catalog_scope,
        registry_id=base.registry_id,
    )
    pointer = ActiveRegistryPointer(
        scope=scope,
        generation=5,
        registry_version=base.version,
        registry_fingerprint=base.fingerprint,
        registry_target=datahub_registry_document_urn(scope, base.version),
        transition_id="registry-transition-5",
        activated_by="activation-admin",
        activated_at=MODEL_NOW - timedelta(days=1),
        decision_ids=semantic_registry_decision_ids(base),
    )
    identity = OnboardingRegistryBase(
        registry_version=base.version,
        registry_fingerprint=base.fingerprint,
        activation_generation=pointer.generation,
        active_pointer_fingerprint=registry_projection_fingerprint(pointer),
    )
    evidence = _replacement_base(base, identity)
    replacement = _replacement(
        identity,
        physical_field=("crm.customers.customer_key_v2" if upsert else "crm.customers.customer_id"),
        asset_id=(
            "urn:li:dataset:(urn:li:dataPlatform:postgres,opaque.customers_v2,PROD)"
            if upsert
            else "urn:li:dataset:(urn:li:dataPlatform:postgres,opaque.customers,PROD)"
        ),
    )
    source = RegistryModelReplacementSourceEvidence.create(
        proposal=replacement,
        owner_actor_id="analyst-source",
        decisions=_source_decisions(replacement),
    )
    risks = ("Activation remains separate and a new M26 inspection is mandatory.",)
    model_decision, mapping_decisions = replacement_source_decisions(
        source,
        base=evidence,
        model_risks=risks,
    )
    if remediation:
        report, impacts = _blocking_report(evidence)
        change_authority = RegistryModelChangeAuthority.remediation(
            evidence,
            report=report,
            impacts=impacts,
            resolved_finding_ids=(report.findings[0].id,),
        )
    else:
        change_authority = RegistryModelChangeAuthority.planned(evidence)
    incident_changes = (
        (_join_upsert(base, identity, evidence, replacement),)
        if upsert
        else (RegistryIncidentJoinPreservation(base=evidence.incident_joins[0]),)
    )
    proposal = PreparedRegistryModelReplacementProposal.create(
        id="registry-model-authority-fixture",
        draft_id="replace-customer-change",
        draft_revision=2,
        draft_fingerprint="4" * 64,
        owner_actor_id="analyst-change-owner",
        base=evidence,
        replacement=replacement,
        outer_decision=_outer_decision(),
        model_decision=model_decision,
        mapping_decisions=mapping_decisions,
        authority=change_authority,
        incident_join_changes=incident_changes,
        risks=risks,
        created_at=MODEL_NOW,
        prepared_by="publisher-registry-authority",
        prepared_at=replacement.prepared_at + timedelta(minutes=1),
    )
    version = GovernedRegistryVersion(
        snapshot=ScopedSemanticRegistrySnapshot(scope=scope, registry=base),
        publication_approval_id="publication-model-base-v2",
        trust=RegistryVersionTrust.STRICT,
    )
    return proposal, base, pointer, version, source


def _model_authority(
    proposal: PreparedRegistryModelReplacementProposal,
    pointer: ActiveRegistryPointer,
    version: GovernedRegistryVersion,
    source: RegistryModelReplacementSourceEvidence | None,
    *,
    catalog_drift: bool = False,
    dependency_state: object | None = None,
    remediation: object | None = ...,
    witness: RegistryModelJoinProfileWitness | None = None,
) -> ExactRegistryPublicationAuthority:
    selected_remediation = (
        (proposal.authority.report, proposal.authority.impacts)
        if remediation is ... and proposal.authority.report is not None
        else remediation
    )
    embedded_upsert = next(
        (
            item
            for item in proposal.incident_join_changes
            if isinstance(item, RegistryIncidentJoinUpsert)
        ),
        None,
    )
    selected_witness = (
        witness
        if witness is not None
        else (embedded_upsert.profile_witness if embedded_upsert is not None else None)
    )
    return ExactRegistryPublicationAuthority(
        proposals=_Proposals(proposal),
        catalog=_Catalog(proposal.replacement, drift=catalog_drift),
        pointers=_Pointers(pointer),
        versions=_Versions(version),
        physical_bindings=_PhysicalBindings(),  # type: ignore[arg-type]
        model_sources=_ModelSources(source),
        model_dependencies=_Dependencies(
            dependency_state or proposal.base.dependency_context.dependency_index
        ),  # type: ignore[arg-type]
        model_remediation=_Remediation(selected_remediation),  # type: ignore[arg-type]
        model_profile_witnesses=_Witnesses(selected_witness),
    )
