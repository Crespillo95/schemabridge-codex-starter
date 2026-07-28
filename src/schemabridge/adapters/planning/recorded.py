"""Deprecated three-file adapter retained only for M10 artifact compatibility."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from schemabridge.application.ports.planning import PlanningPortError, PlanningPortErrorCode
from schemabridge.domain.joins import JoinContractSet
from schemabridge.domain.request_context import ApprovedLogicalContext
from schemabridge.domain.semantic_registry import (
    GovernedJoinRegistry,
    GovernedMappingRegistry,
    GovernedSemanticRegistrySnapshot,
    RegistryArtifactKind,
    RegistryArtifactProvenance,
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
)


class RecordedSemanticPlanningContext:
    """Adapt the historical M10 recordings into one atomic registry snapshot.

    New composition must use ``RecordedGovernedSemanticRegistry`` and its
    checksummed manifest. This adapter exists so historical examples remain
    readable while never becoming an implicit fallback.
    """

    def __init__(
        self,
        logical_context_path: Path,
        mappings_path: Path,
        join_contracts_path: Path,
    ) -> None:
        self._logical_context_path = logical_context_path
        self._mappings_path = mappings_path
        self._join_contracts_path = join_contracts_path
        self._scope = SemanticRegistryScope(
            workspace_id="legacy-m10",
            catalog_scope="synthetic-demo",
            registry_id="legacy_m10",
        )

    @property
    def scope(self) -> SemanticRegistryScope:
        return self._scope

    def load(self) -> ScopedSemanticRegistrySnapshot:
        try:
            logical = ApprovedLogicalContext.model_validate(
                yaml.safe_load(self._logical_context_path.read_text(encoding="utf-8"))
            )
            mapping_payload = yaml.safe_load(self._mappings_path.read_text(encoding="utf-8"))
            field_versions = {
                field.id.root: field.version for model in logical.models for field in model.fields
            }
            for item in mapping_payload["mappings"]:
                item["logical_field_version"] = field_versions[item["mapping"]["logical_field"]]
            mappings = GovernedMappingRegistry.model_validate(mapping_payload)
            historical_contracts = JoinContractSet.model_validate(
                yaml.safe_load(self._join_contracts_path.read_text(encoding="utf-8"))
            )
            contracts = GovernedJoinRegistry(
                version=historical_contracts.version,
                contracts=historical_contracts.contracts,
            )
            mapping_decisions = tuple(
                item.approval_decision_id
                for item in mappings.mappings
                if item.approval_decision_id is not None
            )
            join_decisions = tuple(
                item.approval_decision_id
                for item in contracts.contracts
                if item.approval_decision_id is not None
            )
            registry = GovernedSemanticRegistrySnapshot(
                registry_id=self._scope.registry_id,
                version=1,
                source="recorded:demo/ground_truth/m10",
                catalog_scope=self._scope.catalog_scope,
                logical_context=logical,
                mapping_set=mappings,
                join_contracts=contracts,
                provenance=(
                    RegistryArtifactProvenance(
                        kind=RegistryArtifactKind.LOGICAL_MODELS,
                        source="recorded:demo/ground_truth/approved_logical_context.yml",
                        decision_ids=("legacy-m10-logical-context-v1",),
                    ),
                    RegistryArtifactProvenance(
                        kind=RegistryArtifactKind.PHYSICAL_MAPPINGS,
                        source="recorded:demo/ground_truth/planning_mappings.yml",
                        decision_ids=mapping_decisions,
                    ),
                    RegistryArtifactProvenance(
                        kind=RegistryArtifactKind.JOIN_CONTRACTS,
                        source="recorded:demo/ground_truth/join_contracts.yml",
                        decision_ids=join_decisions,
                    ),
                ),
            )
            return ScopedSemanticRegistrySnapshot(scope=self._scope, registry=registry)
        except OSError as error:
            raise PlanningPortError(
                PlanningPortErrorCode.CONTEXT_UNAVAILABLE,
                "recorded governed planning context is unavailable",
            ) from error
        except (KeyError, ValidationError, yaml.YAMLError, TypeError, ValueError) as error:
            raise PlanningPortError(
                PlanningPortErrorCode.CONTEXT_INVALID,
                "recorded governed planning context is invalid",
            ) from error
