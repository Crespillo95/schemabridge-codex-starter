"""Exact M34 proposal, catalog and active-base authority tests."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
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
from schemabridge.domain.registry_control import ActiveRegistryPointer, GovernedRegistryVersion
from schemabridge.domain.registry_publication_jobs import RegistryPublicationFailureCode
from schemabridge.domain.semantic_onboarding import (
    OnboardingCatalogGeneration,
    OnboardingRegistryBase,
    PreparedSemanticOnboardingProposal,
    ResolvedOnboardingCatalogEvidence,
    SemanticOnboardingMappingInput,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope


@dataclass(slots=True)
class _Proposals:
    proposal: PreparedSemanticOnboardingProposal | None

    def load_exact(
        self,
        workspace_id: str,
        proposal_id: str,
    ) -> PreparedSemanticOnboardingProposal | None:
        if self.proposal is None:
            return None
        assert workspace_id == self.proposal.workspace_id
        assert proposal_id == self.proposal.id
        return self.proposal


@dataclass(slots=True)
class _Catalog:
    proposal: PreparedSemanticOnboardingProposal
    error: SemanticOnboardingPortError | None = None

    def resolve_exact(
        self,
        scope: SemanticRegistryScope,
        connection_id: object,
        generation: int,
        expected_generation_fingerprint: str,
        selections: tuple[SemanticOnboardingMappingInput, ...],
    ) -> ResolvedOnboardingCatalogEvidence:
        del connection_id, selections
        if self.error is not None:
            raise self.error
        assert scope == self.proposal.scope
        assert generation == 7
        assert expected_generation_fingerprint == FP_A
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
            observations=tuple(mapping.observation for mapping in self.proposal.mappings),
        )


@dataclass(slots=True)
class _Pointers:
    pointer: ActiveRegistryPointer | None = None

    def load_active(self, scope: SemanticRegistryScope) -> ActiveRegistryPointer | None:
        del scope
        return self.pointer


class _Versions:
    def load_version(
        self,
        scope: SemanticRegistryScope,
        version: int,
    ) -> GovernedRegistryVersion:
        del scope, version
        raise AssertionError("an empty active base must not load a DataHub version")


class _PhysicalBindings:
    def require_current(self, scope: object, bindings: tuple[object, ...]) -> None:
        del scope, bindings


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
