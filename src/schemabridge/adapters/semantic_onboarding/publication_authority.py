"""Exact retained authority revalidation for one registry publication attempt."""

from __future__ import annotations

from dataclasses import dataclass

from schemabridge.application.ports.registry_control import (
    ActiveRegistryPointerReadPort,
    RegistryControlError,
    RegistryControlErrorCode,
    RegistryVersionReadPort,
)
from schemabridge.application.ports.registry_publication import (
    RegistryPublicationAuthorityError,
    RegistryPublicationPhysicalBindingAuthorityPort,
    RegistryPublicationProposalPort,
    RegistryPublicationStoreError,
    RegistryPublicationStoreErrorCode,
)
from schemabridge.application.ports.semantic_onboarding import (
    SemanticOnboardingCatalogEvidencePort,
    SemanticOnboardingPortError,
    SemanticOnboardingPortErrorCode,
)
from schemabridge.domain.registry_control import (
    RegistryVersionTrust,
    registry_projection_fingerprint,
)
from schemabridge.domain.registry_publication_jobs import RegistryPublicationFailureCode
from schemabridge.domain.semantic_onboarding import (
    PreparedSemanticOnboardingProposal,
    SemanticOnboardingMappingInput,
)
from schemabridge.domain.semantic_registry import GovernedSemanticRegistrySnapshot


@dataclass(frozen=True, slots=True)
class ExactRegistryPublicationAuthority:
    """Reread every M33/catalog/pointer/version fact before an external write."""

    proposals: RegistryPublicationProposalPort
    catalog: SemanticOnboardingCatalogEvidencePort
    pointers: ActiveRegistryPointerReadPort
    versions: RegistryVersionReadPort
    physical_bindings: RegistryPublicationPhysicalBindingAuthorityPort

    def resolve_base(
        self,
        proposal: PreparedSemanticOnboardingProposal,
    ) -> GovernedSemanticRegistrySnapshot | None:
        current = self._load_proposal(proposal)
        if current != proposal:
            raise _authority_error(
                RegistryPublicationFailureCode.PROPOSAL_STALE,
                "registry publication proposal changed",
            )

        self._require_catalog(current)
        try:
            pointer = self.pointers.load_active(current.scope)
        except RegistryControlError as error:
            raise _authority_error(
                RegistryPublicationFailureCode.BASE_UNAVAILABLE,
                "registry publication base is unavailable",
            ) from error

        expected = current.base_registry
        if pointer is None:
            if expected.registry_version is not None:
                raise _authority_error(
                    RegistryPublicationFailureCode.BASE_STALE,
                    "registry publication base changed",
                )
            return None
        if (
            expected.registry_version is None
            or pointer.scope != current.scope
            or pointer.registry_version != expected.registry_version
            or pointer.registry_fingerprint != expected.registry_fingerprint
            or pointer.generation != expected.activation_generation
            or registry_projection_fingerprint(pointer) != expected.active_pointer_fingerprint
        ):
            raise _authority_error(
                RegistryPublicationFailureCode.BASE_STALE,
                "registry publication base changed",
            )

        try:
            version = self.versions.load_version(current.scope, pointer.registry_version)
        except RegistryControlError as error:
            retryable = error.code in {
                RegistryControlErrorCode.STORE_UNAVAILABLE,
                RegistryControlErrorCode.VERSION_UNAVAILABLE,
                RegistryControlErrorCode.PROJECTION_UNAVAILABLE,
            }
            raise _authority_error(
                (
                    RegistryPublicationFailureCode.BASE_UNAVAILABLE
                    if retryable
                    else RegistryPublicationFailureCode.BASE_STALE
                ),
                "registry publication base cannot be verified",
            ) from error

        snapshot = version.snapshot
        registry = snapshot.registry
        if (
            version.trust is not RegistryVersionTrust.STRICT
            or snapshot.scope != current.scope
            or registry.format_version != 2
            or registry.version != pointer.registry_version
            or registry.fingerprint != pointer.registry_fingerprint
        ):
            raise _authority_error(
                RegistryPublicationFailureCode.BASE_STALE,
                "registry publication base cannot be verified",
            )
        self.physical_bindings.require_current(current.scope, registry.physical_bindings)
        return registry

    def _load_proposal(
        self,
        proposal: PreparedSemanticOnboardingProposal,
    ) -> PreparedSemanticOnboardingProposal:
        try:
            current = self.proposals.load_exact(proposal.workspace_id, proposal.id)
        except RegistryPublicationStoreError as error:
            retryable = error.code in {
                RegistryPublicationStoreErrorCode.UNAVAILABLE,
                RegistryPublicationStoreErrorCode.SCHEMA_MISMATCH,
            }
            raise _authority_error(
                (
                    RegistryPublicationFailureCode.BASE_UNAVAILABLE
                    if retryable
                    else RegistryPublicationFailureCode.PROPOSAL_UNAVAILABLE
                ),
                "registry publication proposal is unavailable",
            ) from error
        if current is None:
            raise _authority_error(
                RegistryPublicationFailureCode.PROPOSAL_UNAVAILABLE,
                "registry publication proposal is unavailable",
            )
        return current

    def _require_catalog(self, proposal: PreparedSemanticOnboardingProposal) -> None:
        observations = tuple(mapping.observation for mapping in proposal.mappings)
        selections = tuple(
            SemanticOnboardingMappingInput(
                id=mapping.id,
                logical_field=mapping.logical_field,
                asset_id=mapping.observation.locator.asset.asset_id,
                field_path=mapping.observation.locator.field_path,
                expected_asset_metadata_fingerprint=(
                    mapping.observation.asset_metadata_fingerprint
                ),
                expected_field_metadata_fingerprint=(
                    mapping.observation.field_metadata_fingerprint
                ),
                physical_field=mapping.observation.physical_field,
                confidence=mapping.confidence,
                evidence=mapping.evidence,
                risks=mapping.risks,
                transformation_plan=mapping.transformation_plan,
            )
            for mapping in proposal.mappings
        )
        connection_id = observations[0].locator.asset.connection_id
        try:
            current = self.catalog.resolve_exact(
                proposal.scope,
                connection_id,
                observations[0].generation,
                observations[0].generation_fingerprint,
                selections,
            )
        except SemanticOnboardingPortError as error:
            retryable = error.code is SemanticOnboardingPortErrorCode.UNAVAILABLE
            raise _authority_error(
                (
                    RegistryPublicationFailureCode.BASE_UNAVAILABLE
                    if retryable
                    else RegistryPublicationFailureCode.CATALOG_STALE
                ),
                "registry publication catalog authority cannot be verified",
            ) from error
        if current.observations != observations:
            raise _authority_error(
                RegistryPublicationFailureCode.CATALOG_STALE,
                "registry publication catalog authority changed",
            )


def _authority_error(
    code: RegistryPublicationFailureCode,
    message: str,
) -> RegistryPublicationAuthorityError:
    return RegistryPublicationAuthorityError(code, message)


__all__ = ["ExactRegistryPublicationAuthority"]
