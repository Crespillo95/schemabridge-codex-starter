"""Exact retained authority revalidation for one registry publication attempt."""

from __future__ import annotations

from dataclasses import dataclass

from schemabridge.application.ports.registry_control import (
    ActiveRegistryPointerReadPort,
    RegistryControlError,
    RegistryControlErrorCode,
    RegistryVersionReadPort,
)
from schemabridge.application.ports.registry_model_changes import (
    RegistryModelJoinProfileWitnessPort,
    RegistryModelRemediationEvidencePort,
    RegistryModelReplacementSourcePort,
)
from schemabridge.application.ports.registry_publication import (
    RegistryPublicationAuthorityError,
    RegistryPublicationPhysicalBindingAuthorityPort,
    RegistryPublicationProposalPort,
    RegistryPublicationStoreError,
    RegistryPublicationStoreErrorCode,
)
from schemabridge.application.ports.semantic_change import (
    SemanticChangeDependencyIndexPort,
    SemanticChangePortError,
    SemanticChangePortErrorCode,
)
from schemabridge.application.ports.semantic_onboarding import (
    SemanticOnboardingCatalogEvidencePort,
    SemanticOnboardingPortError,
    SemanticOnboardingPortErrorCode,
)
from schemabridge.domain.registry_changes import PreparedRegistryJoinProposal
from schemabridge.domain.registry_control import (
    RegistryVersionTrust,
    registry_projection_fingerprint,
)
from schemabridge.domain.registry_model_change_authoring import replacement_source_decisions
from schemabridge.domain.registry_model_changes import (
    PreparedRegistryModelReplacementProposal,
    RegistryIncidentJoinUpsert,
    RegistryModelChangeKind,
)
from schemabridge.domain.registry_publication_jobs import (
    PreparedRegistryPublicationProposal,
    RegistryPublicationFailureCode,
)
from schemabridge.domain.semantic_onboarding import (
    OnboardingEvidence,
    OnboardingEvidenceKind,
    PhysicalCatalogObservation,
    PreparedSemanticOnboardingProposal,
    SemanticOnboardingMappingInput,
)
from schemabridge.domain.semantic_registry import GovernedSemanticRegistrySnapshot


@dataclass(frozen=True, slots=True)
class ExactRegistryPublicationAuthority:
    """Reread every proposal/catalog/pointer/version fact before an external write."""

    proposals: RegistryPublicationProposalPort
    catalog: SemanticOnboardingCatalogEvidencePort
    pointers: ActiveRegistryPointerReadPort
    versions: RegistryVersionReadPort
    physical_bindings: RegistryPublicationPhysicalBindingAuthorityPort
    model_sources: RegistryModelReplacementSourcePort | None = None
    model_dependencies: SemanticChangeDependencyIndexPort | None = None
    model_remediation: RegistryModelRemediationEvidencePort | None = None
    model_profile_witnesses: RegistryModelJoinProfileWitnessPort | None = None

    def resolve_base(
        self,
        proposal: PreparedRegistryPublicationProposal,
    ) -> GovernedSemanticRegistrySnapshot | None:
        current = self._load_proposal(proposal)
        if current != proposal:
            raise _authority_error(
                RegistryPublicationFailureCode.PROPOSAL_STALE,
                "registry publication proposal changed",
            )

        if isinstance(current, PreparedSemanticOnboardingProposal):
            self._require_catalog(current)
        elif isinstance(current, PreparedRegistryJoinProposal):
            self._require_join_catalog(current)
        elif isinstance(current, PreparedRegistryModelReplacementProposal):
            self._require_catalog(current.replacement)
        else:
            raise _authority_error(
                RegistryPublicationFailureCode.PROPOSAL_STALE,
                "registry publication proposal kind is unsupported",
            )
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
        if isinstance(current, PreparedRegistryModelReplacementProposal):
            self._require_model_replacement_authority(current)
        return registry

    def _require_model_replacement_authority(
        self,
        proposal: PreparedRegistryModelReplacementProposal,
    ) -> None:
        if self.model_sources is None or self.model_dependencies is None:
            raise _authority_error(
                RegistryPublicationFailureCode.BASE_UNAVAILABLE,
                "registry model replacement authority is unavailable",
            )
        try:
            source = self.model_sources.load(
                proposal.workspace_id,
                proposal.source_replacement_proposal_id,
            )
        except Exception as error:
            raise _authority_error(
                RegistryPublicationFailureCode.BASE_UNAVAILABLE,
                "registry model replacement source cannot be verified",
            ) from error
        if source is None:
            raise _authority_error(
                RegistryPublicationFailureCode.PROPOSAL_STALE,
                "registry model replacement source changed",
            )
        try:
            model_decision, mapping_decisions = replacement_source_decisions(
                source,
                base=proposal.base,
                model_risks=proposal.risks,
            )
        except (TypeError, ValueError) as error:
            raise _authority_error(
                RegistryPublicationFailureCode.PROPOSAL_STALE,
                "registry model replacement source changed",
            ) from error
        if (
            source.proposal != proposal.replacement
            or model_decision != proposal.model_decision
            or mapping_decisions != proposal.mapping_decisions
        ):
            raise _authority_error(
                RegistryPublicationFailureCode.PROPOSAL_STALE,
                "registry model replacement source changed",
            )

        try:
            dependency_state = self.model_dependencies.load_state(proposal.scope)
        except SemanticChangePortError as error:
            retryable = error.code is SemanticChangePortErrorCode.STORE_UNAVAILABLE
            raise _authority_error(
                (
                    RegistryPublicationFailureCode.BASE_UNAVAILABLE
                    if retryable
                    else RegistryPublicationFailureCode.BASE_STALE
                ),
                "registry model replacement dependencies cannot be verified",
            ) from error
        except Exception as error:
            raise _authority_error(
                RegistryPublicationFailureCode.BASE_UNAVAILABLE,
                "registry model replacement dependencies cannot be verified",
            ) from error
        if dependency_state != proposal.base.dependency_context.dependency_index:
            raise _authority_error(
                RegistryPublicationFailureCode.BASE_STALE,
                "registry model replacement dependencies changed",
            )

        authority = proposal.authority
        if authority.kind is RegistryModelChangeKind.M26_REMEDIATION:
            if (
                self.model_remediation is None
                or authority.report is None
                or authority.impacts is None
            ):
                raise _authority_error(
                    RegistryPublicationFailureCode.BASE_UNAVAILABLE,
                    "registry model remediation authority is unavailable",
                )
            try:
                remediation = self.model_remediation.load_current(
                    proposal.scope,
                    authority.report.id,
                    proposal.base.dependency_context,
                )
            except Exception as error:
                raise _authority_error(
                    RegistryPublicationFailureCode.BASE_UNAVAILABLE,
                    "registry model remediation authority cannot be verified",
                ) from error
            if remediation != (authority.report, authority.impacts):
                raise _authority_error(
                    RegistryPublicationFailureCode.BASE_STALE,
                    "registry model remediation authority changed",
                )

        upserts = tuple(
            item
            for item in proposal.incident_join_changes
            if isinstance(item, RegistryIncidentJoinUpsert)
        )
        if upserts and self.model_profile_witnesses is None:
            raise _authority_error(
                RegistryPublicationFailureCode.BASE_UNAVAILABLE,
                "registry model relationship authority is unavailable",
            )
        for upsert in upserts:
            assert self.model_profile_witnesses is not None
            try:
                current_witness = self.model_profile_witnesses.load(
                    proposal.workspace_id,
                    proposal.draft_id,
                    proposal.source_replacement_proposal_id,
                    upsert.base.contract.id,
                )
            except Exception as error:
                raise _authority_error(
                    RegistryPublicationFailureCode.BASE_UNAVAILABLE,
                    "registry model relationship authority cannot be verified",
                ) from error
            if current_witness != upsert.profile_witness:
                raise _authority_error(
                    RegistryPublicationFailureCode.PROPOSAL_STALE,
                    "registry model relationship authority changed",
                )

    def _load_proposal(
        self,
        proposal: PreparedRegistryPublicationProposal,
    ) -> PreparedRegistryPublicationProposal:
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

    def _require_join_catalog(self, proposal: PreparedRegistryJoinProposal) -> None:
        endpoints = (proposal.base_evidence.left, proposal.base_evidence.right)
        bindings = tuple(endpoint.binding for endpoint in endpoints)
        first = bindings[0]
        if any(
            binding.workspace_id != proposal.workspace_id
            or binding.connection_id != first.connection_id
            or binding.catalog_scope != proposal.scope.catalog_scope
            or binding.catalog_generation != first.catalog_generation
            or binding.catalog_generation_fingerprint != first.catalog_generation_fingerprint
            for binding in bindings
        ):
            raise _authority_error(
                RegistryPublicationFailureCode.CATALOG_STALE,
                "registry join publication catalog authority changed",
            )
        selections = tuple(
            SemanticOnboardingMappingInput(
                id=f"registry_join_{side}",
                logical_field=endpoint.mapping.mapping.logical_field,
                asset_id=endpoint.binding.locator.asset.asset_id,
                field_path=endpoint.binding.locator.field_path,
                expected_asset_metadata_fingerprint=(endpoint.binding.asset_metadata_fingerprint),
                expected_field_metadata_fingerprint=(endpoint.binding.field_metadata_fingerprint),
                physical_field=endpoint.binding.physical_field,
                confidence=endpoint.mapping.mapping.confidence,
                evidence=(
                    OnboardingEvidence(
                        kind=OnboardingEvidenceKind.HUMAN_ATTESTATION,
                        detail="Existing approved registry mapping authority.",
                        reference=endpoint.mapping.approval_decision_id,
                    ),
                ),
                risks=endpoint.mapping.mapping.risks,
                transformation_plan=endpoint.mapping.mapping.transformation_plan,
            )
            for side, endpoint in zip(("left", "right"), endpoints, strict=True)
        )
        expected = tuple(
            PhysicalCatalogObservation(
                locator=binding.locator,
                catalog_scope=binding.catalog_scope,
                generation=binding.catalog_generation,
                generation_fingerprint=binding.catalog_generation_fingerprint,
                asset_metadata_fingerprint=binding.asset_metadata_fingerprint,
                field_metadata_fingerprint=binding.field_metadata_fingerprint,
                physical_field=binding.physical_field,
                physical_type=binding.physical_type,
                observed_datahub_asset_urn=binding.observed_datahub_asset_urn,
            )
            for binding in bindings
        )
        try:
            current = self.catalog.resolve_exact(
                proposal.scope,
                first.connection_id,
                first.catalog_generation,
                first.catalog_generation_fingerprint,
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
                "registry join publication catalog authority cannot be verified",
            ) from error
        if current.observations != expected:
            raise _authority_error(
                RegistryPublicationFailureCode.CATALOG_STALE,
                "registry join publication catalog authority changed",
            )


def _authority_error(
    code: RegistryPublicationFailureCode,
    message: str,
) -> RegistryPublicationAuthorityError:
    return RegistryPublicationAuthorityError(code, message)


__all__ = ["ExactRegistryPublicationAuthority"]
