from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from schemabridge.adapters.semantic_registry.recorded import (
    RecordedGovernedSemanticRegistry,
)
from schemabridge.application.ports.planning import PlanningPortError, PlanningPortErrorCode
from schemabridge.domain.semantic_registry import (
    GovernedSemanticRegistrySnapshot,
    SemanticRegistryScope,
    governed_semantic_registry_fingerprint,
)

ROOT = Path(__file__).resolve().parents[2]
REGISTRY_DIRECTORY = ROOT / "demo/ground_truth/registries"
REGISTRY_PATH = REGISTRY_DIRECTORY / "synthetic_enterprise.yml"
MANIFEST_PATH = REGISTRY_DIRECTORY / "manifest.yml"


def _yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _scope(
    *,
    registry_id: str = "synthetic_enterprise",
    catalog_scope: str = "synthetic-demo",
) -> SemanticRegistryScope:
    return SemanticRegistryScope(
        workspace_id="unit-test-workspace",
        catalog_scope=catalog_scope,
        registry_id=registry_id,
    )


def _write_bundle(
    directory: Path,
    *,
    registry_payload: bytes | None = None,
    manifest_mutation: object | None = None,
) -> Path:
    resolved_registry = registry_payload or REGISTRY_PATH.read_bytes()
    (directory / "synthetic_enterprise.yml").write_bytes(resolved_registry)
    manifest = _yaml(MANIFEST_PATH)
    manifest["registries"][0]["sha256"] = hashlib.sha256(resolved_registry).hexdigest()
    if callable(manifest_mutation):
        manifest_mutation(manifest)
    path = directory / "manifest.yml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return path


def test_enterprise_registry_is_large_governed_and_manifest_verified() -> None:
    scoped = RecordedGovernedSemanticRegistry(MANIFEST_PATH, _scope()).load()
    registry = scoped.registry

    assert scoped.scope == _scope()
    assert registry.registry_id == "synthetic_enterprise"
    assert registry.catalog_scope == "synthetic-demo"
    assert len(registry.logical_context.models) == 7
    assert len(registry.logical_context.field_index()) == 31
    assert len(registry.mapping_set.mappings) == 31
    assert len(registry.join_contracts.contracts) == 5
    assert {model.id.root for model in registry.logical_context.models} == {
        "Customer",
        "AccountHolder",
        "Account",
        "Product",
        "SalesOrder",
        "SaleLine",
        "Shipment",
    }
    assert governed_semantic_registry_fingerprint(registry) == registry.fingerprint
    manifest = _yaml(MANIFEST_PATH)
    assert manifest["registries"][0]["fingerprint"] == registry.fingerprint
    assert (
        manifest["registries"][0]["sha256"]
        == hashlib.sha256(REGISTRY_PATH.read_bytes()).hexdigest()
    )


def test_enterprise_registry_preserves_the_complete_north_star_payloads() -> None:
    bundle = _yaml(REGISTRY_PATH)
    original_context = _yaml(ROOT / "demo/ground_truth/approved_logical_context.yml")
    original_mappings = _yaml(ROOT / "demo/ground_truth/planning_mappings.yml")
    original_contracts = _yaml(ROOT / "demo/ground_truth/join_contracts.yml")

    assert bundle["logical_context"]["models"][:3] == original_context["models"]
    assert bundle["logical_context"]["joins"][:2] == original_context["joins"]
    north_star_mappings = copy.deepcopy(bundle["mapping_set"]["mappings"][:9])
    for mapping in north_star_mappings:
        assert mapping.pop("logical_field_version") == 1
    assert north_star_mappings == original_mappings["mappings"]
    assert bundle["join_contracts"]["contracts"][:2] == original_contracts["contracts"]


def test_closed_new_domains_publish_only_canonical_allowed_values() -> None:
    registry = RecordedGovernedSemanticRegistry(MANIFEST_PATH, _scope()).load().registry
    fields = registry.logical_context.field_index()

    assert fields["Product.category"].allowed_values == (
        "ELECTRONICS",
        "HOME",
        "BOOKS",
        "SPORTS",
    )
    assert fields["SalesOrder.order_status"].allowed_values == (
        "CREATED",
        "PAID",
        "COMPLETED",
        "CANCELLED",
    )
    assert fields["SalesOrder.sales_channel"].allowed_values == ("WEB", "STORE", "PARTNER")
    assert fields["SalesOrder.region"].allowed_values == ("NORTH", "SOUTH", "EAST", "WEST")
    assert fields["Shipment.shipment_status"].allowed_values == (
        "PENDING",
        "IN_TRANSIT",
        "DELIVERED",
        "RETURNED",
    )


def test_registry_rejects_an_unmapped_active_logical_field() -> None:
    payload = _yaml(REGISTRY_PATH)
    payload["mapping_set"]["mappings"] = payload["mapping_set"]["mappings"][:-1]

    with pytest.raises(
        ValidationError,
        match="every active logical field requires an approved physical mapping",
    ):
        GovernedSemanticRegistrySnapshot.model_validate(payload)


def test_registry_rejects_a_join_not_backed_by_the_exact_mapping_transform() -> None:
    payload = _yaml(REGISTRY_PATH)
    shipment_join = payload["join_contracts"]["contracts"][-1]
    shipment_join["right_key"]["transformation_plan"]["steps"] = [{"operation": "identity"}]

    with pytest.raises(
        ValidationError,
        match="join key is not backed by the exact approved mapping transformation",
    ):
        GovernedSemanticRegistrySnapshot.model_validate(payload)


def test_registry_rejects_stale_logical_field_version_independently_of_mapping_version() -> None:
    payload = _yaml(REGISTRY_PATH)
    payload["mapping_set"]["mappings"][-1]["logical_field_version"] = 2
    payload["mapping_set"]["mappings"][-1]["mapping"]["version"] = 99

    with pytest.raises(ValidationError, match="mapping is stale for its logical field"):
        GovernedSemanticRegistrySnapshot.model_validate(payload)


def test_registry_rejects_provenance_that_omits_an_active_decision() -> None:
    payload = _yaml(REGISTRY_PATH)
    payload["provenance"][1]["decision_ids"].remove("ground-truth-map-product-key-v1")

    with pytest.raises(
        ValidationError,
        match="semantic registry provenance must exactly match active mapping and join decisions",
    ):
        GovernedSemanticRegistrySnapshot.model_validate(payload)


def test_recorded_adapter_rejects_checksum_tampering(
    tmp_path: Path,
) -> None:
    manifest = _write_bundle(tmp_path)
    registry_path = tmp_path / "synthetic_enterprise.yml"
    registry_path.write_bytes(registry_path.read_bytes() + b"\n")

    with pytest.raises(PlanningPortError) as captured:
        RecordedGovernedSemanticRegistry(manifest, _scope()).load()

    assert captured.value.code is PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED
    assert "checksum verification failed" in str(captured.value)


def test_recorded_adapter_rejects_duplicate_yaml_keys(
    tmp_path: Path,
) -> None:
    duplicated = REGISTRY_PATH.read_bytes() + b"\nregistry_id: forged_registry\n"
    manifest = _write_bundle(tmp_path, registry_payload=duplicated)

    with pytest.raises(PlanningPortError) as captured:
        RecordedGovernedSemanticRegistry(manifest, _scope()).load()

    assert captured.value.code is PlanningPortErrorCode.CONTEXT_INVALID
    assert str(captured.value) == "governed semantic registry is invalid"


def test_recorded_adapter_rejects_manifest_fingerprint_tampering(
    tmp_path: Path,
) -> None:
    manifest = _write_bundle(
        tmp_path,
        manifest_mutation=lambda payload: payload["registries"][0].update(
            {"fingerprint": "0" * 64}
        ),
    )

    with pytest.raises(PlanningPortError) as captured:
        RecordedGovernedSemanticRegistry(manifest, _scope()).load()

    assert captured.value.code is PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED
    assert "identity or fingerprint verification failed" in str(captured.value)


def test_recorded_adapter_rejects_scope_mismatch_before_reading_registry(
    tmp_path: Path,
) -> None:
    manifest = _write_bundle(tmp_path)

    with pytest.raises(PlanningPortError) as captured:
        RecordedGovernedSemanticRegistry(
            manifest,
            _scope(catalog_scope="another-synthetic-catalog"),
        ).load()

    assert captured.value.code is PlanningPortErrorCode.REGISTRY_SCOPE_MISMATCH
    assert "not valid for this catalog scope" in str(captured.value)


def test_recorded_adapter_rejects_a_manifest_path_escape(
    tmp_path: Path,
) -> None:
    manifest = _write_bundle(
        tmp_path,
        manifest_mutation=lambda payload: payload["registries"][0].update(
            {"path": "../synthetic_enterprise.yml"}
        ),
    )

    with pytest.raises(PlanningPortError) as captured:
        RecordedGovernedSemanticRegistry(manifest, _scope()).load()

    assert captured.value.code is PlanningPortErrorCode.CONTEXT_INVALID
    assert str(captured.value) == "governed semantic registry is invalid"
