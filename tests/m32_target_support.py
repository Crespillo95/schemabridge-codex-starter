"""Synthetic registry-v2 and target fixtures for M32 commercial binding tests."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from schemabridge.adapters.semantic_registry.memory import (
    InMemoryGovernedSemanticRegistry,
)
from schemabridge.application.connectors import ConnectorTargetError
from schemabridge.application.semantic_change import AssertSemanticContextCurrent
from schemabridge.bootstrap import build_semantic_registry
from schemabridge.domain.catalog_inventory import (
    CatalogAssetId,
    CatalogAssetLocator,
    CatalogConnectionId,
    CatalogFieldLocator,
)
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    QueryCostBudget,
    SourceConnectorKind,
    SourceDialect,
    postgres_type_contract_fingerprint,
)
from schemabridge.domain.semantic_change import (
    SemanticChangeStatus,
    SemanticContextGateAssessment,
    SemanticPlanDependencies,
)
from schemabridge.domain.semantic_registry import (
    GovernedPhysicalBinding,
    GovernedSemanticRegistrySnapshot,
)

ROOT = Path(__file__).resolve().parents[1]
M32_CONNECTION_ID = CatalogConnectionId("warehouse-primary")


def target_bound_registry() -> InMemoryGovernedSemanticRegistry:
    """Upgrade the synthetic M32 registry to exact same-connection v2 authority."""

    loaded = build_semantic_registry(repository_root=ROOT).load()
    generation_fingerprint = _digest("m32-target-catalog-generation")
    bindings: list[GovernedPhysicalBinding] = []
    for index, governed in enumerate(loaded.registry.mapping_set.mappings, start=1):
        physical = governed.mapping.physical_field.root
        dataset, field_name = physical.rsplit(".", 1)
        observed_urn = (
            "urn:li:dataset:(urn:li:dataPlatform:postgres,"
            f"m32-target-{dataset.replace('.', '-')},PROD)"
        )
        bindings.append(
            GovernedPhysicalBinding(
                workspace_id=loaded.scope.workspace_id,
                connection_id=M32_CONNECTION_ID,
                catalog_scope=loaded.scope.catalog_scope,
                catalog_generation=1,
                catalog_generation_fingerprint=generation_fingerprint,
                locator=CatalogFieldLocator(
                    asset=CatalogAssetLocator(
                        workspace_id=loaded.scope.workspace_id,
                        connection_id=M32_CONNECTION_ID,
                        asset_id=CatalogAssetId(observed_urn),
                    ),
                    field_path=(field_name,),
                ),
                asset_metadata_fingerprint=_digest(f"asset:{dataset}"),
                field_metadata_fingerprint=_digest(f"field:{physical}"),
                logical_field=governed.mapping.logical_field,
                physical_field=governed.mapping.physical_field,
                physical_type=governed.physical_type,
                observed_datahub_asset_urn=observed_urn,
                source_proposal_id=f"m32-target-proposal-{index}",
                source_proposal_fingerprint=_digest(f"proposal:{index}:{physical}"),
            )
        )
    registry = GovernedSemanticRegistrySnapshot.model_validate(
        {
            **loaded.registry.model_dump(mode="python"),
            "format_version": 2,
            "physical_bindings": tuple(bindings),
        }
    )
    return InMemoryGovernedSemanticRegistry(
        registry,
        loaded.scope,
        activation_generation=7,
        active_pointer_fingerprint=_digest("m32-active-registry-pointer"),
    )


def execution_target(
    *,
    workspace_id: str,
    connection_id: CatalogConnectionId = M32_CONNECTION_ID,
    route_revision: int = 1,
    marker: str = "current",
) -> GovernedExecutionTarget:
    budget = QueryCostBudget(
        explain_timeout_ms=1_000,
        max_response_bytes=65_536,
        max_total_cost=Decimal("10000"),
        max_estimated_rows=100_000,
        max_plan_nodes=1_000,
        max_plan_depth=32,
        max_plan_width=16_384,
    )
    return GovernedExecutionTarget(
        workspace_id=workspace_id,
        connection_id=connection_id,
        connector_kind=SourceConnectorKind.POSTGRESQL,
        dialect=SourceDialect.POSTGRESQL,
        route_revision=route_revision,
        route_fingerprint=_digest(f"route:{marker}:{route_revision}"),
        expected_reader="schemabridge_reader",
        source_identity_fingerprint=_digest(f"source:{marker}"),
        catalog_identity_fingerprint=_digest(f"catalog:{marker}"),
        type_contract_fingerprint=postgres_type_contract_fingerprint(),
        cost_budget=budget,
        cost_budget_fingerprint=budget.fingerprint,
    )


@dataclass(slots=True)
class MutableTargetResolver:
    target: GovernedExecutionTarget
    error: ConnectorTargetError | None = None
    calls: list[tuple[str, CatalogConnectionId]] = field(default_factory=list)

    def resolve_current(
        self,
        *,
        workspace_id: str,
        connection_id: CatalogConnectionId,
    ) -> GovernedExecutionTarget:
        self.calls.append((workspace_id, connection_id))
        if self.error is not None:
            raise self.error
        return self.target


@dataclass(slots=True)
class CurrentSemanticGate:
    connection_id: CatalogConnectionId = M32_CONNECTION_ID
    calls: list[SemanticPlanDependencies] = field(default_factory=list)

    def assess(
        self,
        dependencies: SemanticPlanDependencies,
    ) -> SemanticContextGateAssessment:
        self.calls.append(dependencies)
        return SemanticContextGateAssessment(
            dependencies_fingerprint=dependencies.fingerprint,
            eligible=True,
            status=SemanticChangeStatus.CURRENT,
            connection_id=self.connection_id,
            baseline_revision=1,
        )


def current_semantic_gate(
    connection_id: CatalogConnectionId = M32_CONNECTION_ID,
) -> AssertSemanticContextCurrent:
    return AssertSemanticContextCurrent(CurrentSemanticGate(connection_id))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


__all__ = [
    "M32_CONNECTION_ID",
    "CurrentSemanticGate",
    "MutableTargetResolver",
    "current_semantic_gate",
    "execution_target",
    "target_bound_registry",
]
