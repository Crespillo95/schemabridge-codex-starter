"""Tests for immutable domain contracts and deterministic YAML fixtures."""

from __future__ import annotations

import ast
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from schemabridge.domain.concepts import CanonicalField, LogicalModel
from schemabridge.domain.decisions import DecisionRecord
from schemabridge.domain.fields import FieldProfile, PhysicalField
from schemabridge.domain.joins import JoinContract, JoinContractSet
from schemabridge.domain.mappings import MappingPlan
from schemabridge.domain.requests import AnalyticalRequest
from schemabridge.domain.validation import ValidationResult

ROOT = Path(__file__).resolve().parents[2]
DOMAIN = ROOT / "src" / "schemabridge" / "domain"


def load_yaml(path: str) -> object:
    with (ROOT / path).open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


@pytest.mark.parametrize(
    ("path", "model_type"),
    [
        ("tests/fixtures/domain/mapping_plan.yml", MappingPlan),
        ("tests/fixtures/domain/join_contract.yml", JoinContractSet),
        ("demo/ground_truth/join_contracts.yml", JoinContractSet),
    ],
)
def test_typed_yaml_fixtures_round_trip_deterministically(
    path: str,
    model_type: type[MappingPlan] | type[JoinContractSet],
) -> None:
    model = model_type.model_validate(load_yaml(path))

    first_dump = yaml.safe_dump(model.model_dump(mode="json"), sort_keys=False)
    reloaded = model_type.model_validate(yaml.safe_load(first_dump))
    second_dump = yaml.safe_dump(reloaded.model_dump(mode="json"), sort_keys=False)

    assert reloaded == model
    assert second_dump == first_dump
    assert "raw_sql" not in first_dump
    assert "python_callback" not in first_dump


def test_mapping_plan_rejects_two_approved_meanings_for_one_physical_field() -> None:
    source = load_yaml("tests/fixtures/domain/mapping_plan.yml")
    assert isinstance(source, dict)
    mappings = source["mappings"]
    assert isinstance(mappings, list)
    duplicate = dict(mappings[0])
    duplicate["logical_field"] = "Customer.registration_date"
    mappings.append(duplicate)

    with pytest.raises(ValidationError, match="multiple logical meanings"):
        MappingPlan.model_validate(source)


def test_fanout_join_requires_a_mitigation_policy() -> None:
    source = load_yaml("tests/fixtures/domain/join_contract.yml")
    assert isinstance(source, dict)
    contracts = source["contracts"]
    assert isinstance(contracts, list)
    contract = dict(contracts[0])
    contract["fanout_policy"] = "none"

    with pytest.raises(ValidationError, match="mitigation"):
        JoinContract.model_validate(contract)


def test_core_domain_values_validate_and_remain_immutable() -> None:
    physical = PhysicalField.model_validate(
        {
            "id": "crm.customers.customer_id",
            "dataset": "crm.customers",
            "field_path": ["customer_id"],
            "native_type": "text",
            "description": "Synthetic customer identifier.",
            "profile": {
                "row_count": 7,
                "null_count": 0,
                "distinct_count": 7,
                "minimum_length": 11,
                "maximum_length": 11,
            },
        }
    )
    canonical = CanonicalField.model_validate(
        {
            "id": "Customer.customer_key",
            "canonical_name": "customer_key",
            "canonical_type": "string",
            "definition": "Stable identifier for a customer across source systems.",
            "format_policy": {
                "null_policy": "preserve",
                "leading_zero_policy": "strip",
            },
        }
    )
    logical = LogicalModel.model_validate(
        {
            "id": "Customer",
            "name": "Customer",
            "description": "A person or organization registered as a customer.",
            "fields": ["Customer.customer_key"],
            "status": "approved",
            "version": 1,
        }
    )
    request = AnalyticalRequest.model_validate(
        {
            "primary_entity": "Customer",
            "metrics": [
                {
                    "operation": "count_distinct",
                    "field": "Customer.customer_key",
                }
            ],
        }
    )
    decision = DecisionRecord.model_validate(
        {
            "id": "approve_customer_key_v1",
            "target_type": "column_mapping",
            "target_id": "crm.customers.customer_id->Customer.customer_key",
            "action": "approve",
            "status": "approved",
            "actor": "demo-steward",
            "decided_at": datetime(2026, 7, 21, 10, 0, tzinfo=UTC),
            "source_version": 1,
            "resulting_version": 1,
            "rationale": "Synthetic values and definitions agree.",
            "evidence": ["normalized_value_overlap"],
            "risks": ["leading_zero_semantics"],
        }
    )

    assert physical.profile == FieldProfile(
        row_count=7,
        null_count=0,
        distinct_count=7,
        minimum_length=11,
        maximum_length=11,
    )
    assert canonical.id.root in {field.root for field in logical.fields}
    assert request.metrics[0].operation.value == "count_distinct"
    assert decision.status.value == "approved"
    assert ValidationResult().is_valid is True
    with pytest.raises(ValidationError):
        logical.version = 2


def test_analytical_request_forbids_raw_sql_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        AnalyticalRequest.model_validate(
            {
                "primary_entity": "Customer",
                "metrics": [
                    {"operation": "count", "field": "Customer.customer_key"},
                ],
                "raw_sql": "SELECT * FROM crm.customers",
            }
        )


def test_domain_imports_only_standard_library_pydantic_and_domain_modules() -> None:
    allowed_roots = sys.stdlib_module_names | {"pydantic", "schemabridge"}
    violations: list[str] = []

    for path in sorted(DOMAIN.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".", 1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                roots = [node.module.split(".", 1)[0]]
            else:
                continue
            violations.extend(f"{path.name}:{root}" for root in roots if root not in allowed_roots)

    assert violations == []
