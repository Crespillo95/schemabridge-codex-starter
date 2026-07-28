"""Explicit M27 cardinality matrix without inventory-size product branches."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from schemabridge.application.query_studio import (
    InspectPhysicalDiscoveryCardinality,
)
from schemabridge.domain.catalog_inventory import (
    TenantCapacityPolicy,
    TenantCapacitySnapshot,
    TenantCapacityUsage,
)
from schemabridge.domain.query_studio import (
    PhysicalDiscoveryCardinality,
    PhysicalFieldDiscoveryPage,
    PhysicalFieldDiscoveryRequest,
    query_studio_fingerprint,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
CONFIGURED_POLICY_ASSET_LIMIT = 7_777
_SIZE_SPECIFIC_TOTALS = frozenset({10, 5_434, 41_028, CONFIGURED_POLICY_ASSET_LIMIT})


@dataclass(frozen=True, slots=True)
class _CardinalityCase:
    label: str
    connection_count: int
    asset_count: int
    field_count: int
    is_configured_limit: bool = False


_MATRIX = (
    _CardinalityCase("empty", 0, 0, 0),
    _CardinalityCase("single", 1, 1, 7),
    _CardinalityCase("small-multiconnection", 2, 10, 75),
    _CardinalityCase("large-multiconnection", 2, 5_434, 41_028),
    _CardinalityCase(
        "configured-policy-limit",
        3,
        CONFIGURED_POLICY_ASSET_LIMIT,
        58_333,
        is_configured_limit=True,
    ),
)


@dataclass(slots=True)
class _CardinalityOnlyDiscovery:
    value: _CardinalityCase
    cardinality_calls: int = 0
    search_calls: int = 0

    def search(self, request: PhysicalFieldDiscoveryRequest) -> PhysicalFieldDiscoveryPage:
        del request
        self.search_calls += 1
        raise AssertionError("cardinality inspection must not materialize physical rows")

    def inspect_cardinality(
        self,
        scope: SemanticRegistryScope,
    ) -> PhysicalDiscoveryCardinality:
        self.cardinality_calls += 1
        return PhysicalDiscoveryCardinality(
            scope=scope,
            catalog_generation_vector_fingerprint=query_studio_fingerprint(
                {
                    "connections": self.value.connection_count,
                    "assets": self.value.asset_count,
                    "fields": self.value.field_count,
                }
            ),
            connection_count=self.value.connection_count,
            asset_count=self.value.asset_count,
            field_count=self.value.field_count,
        )


@pytest.mark.parametrize("case", _MATRIX, ids=lambda case: case.label)
def test_m27_cardinality_matrix_is_exact_dynamic_and_row_free(
    case: _CardinalityCase,
) -> None:
    scope = SemanticRegistryScope(
        workspace_id=f"workspace-{case.label}",
        catalog_scope="synthetic-m27-cardinality",
        registry_id="synthetic_enterprise",
    )
    discovery = _CardinalityOnlyDiscovery(case)

    observed = InspectPhysicalDiscoveryCardinality(discovery).execute(scope)

    assert (
        observed.connection_count,
        observed.asset_count,
        observed.field_count,
    ) == (
        case.connection_count,
        case.asset_count,
        case.field_count,
    )
    assert discovery.cardinality_calls == 1
    assert discovery.search_calls == 0
    if case.asset_count >= 10:
        assert case.connection_count > 1

    policy_asset_limit = CONFIGURED_POLICY_ASSET_LIMIT if case.is_configured_limit else 10_000
    capacity = TenantCapacitySnapshot(
        policy=TenantCapacityPolicy(
            workspace_id=scope.workspace_id,
            version=1,
            connection_limit=10,
            asset_limit=policy_asset_limit,
            field_limit=100_000,
            api_requests_per_minute=1_000,
            nonterminal_job_limit=100,
            updated_by="actor_m27_cardinality_operator",
            updated_at=NOW,
        ),
        usage=TenantCapacityUsage(
            workspace_id=scope.workspace_id,
            connection_count=observed.connection_count,
            asset_count=observed.asset_count,
            field_count=observed.field_count,
            nonterminal_job_count=0,
            observed_at=NOW,
        ),
    )
    assert capacity.over_capacity is False
    if case.is_configured_limit:
        assert capacity.usage.asset_count == capacity.policy.asset_limit


def test_m27_cardinality_paths_have_no_branch_for_a_fixture_total() -> None:
    """Reject code paths coupled to any acceptance-fixture total.

    Generic validation such as rejecting negative counts remains allowed. The
    forbidden shape is a branch that compares a cardinality value with one of
    the small, large, field, or configured-policy fixture totals.
    """

    paths = (
        ROOT / "src/schemabridge/application/query_studio.py",
        ROOT / "src/schemabridge/adapters/catalog/postgres_physical_discovery.py",
        ROOT / "src/schemabridge/entrypoints/streamlit/query_studio.py",
    )
    findings: list[str] = []
    for path in paths:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            expression = ast.get_source_segment(source, node) or ast.dump(node)
            if not any(
                name in expression for name in ("connection_count", "asset_count", "field_count")
            ):
                continue
            compared_totals = {
                value.value
                for value in ast.walk(node)
                if isinstance(value, ast.Constant)
                and type(value.value) is int
                and value.value in _SIZE_SPECIFIC_TOTALS
            }
            if compared_totals:
                findings.append(f"{path.relative_to(ROOT)}:{node.lineno}:{sorted(compared_totals)}")

    assert findings == []
