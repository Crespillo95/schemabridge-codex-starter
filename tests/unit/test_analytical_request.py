"""Intrinsic analytical-request rules and deterministic ground-truth serialization."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from schemabridge.domain.requests import AnalyticalRequest, Filter

ROOT = Path(__file__).resolve().parents[2]


def test_analytical_request_ground_truth_cases_round_trip_deterministically() -> None:
    payload = yaml.safe_load(
        (ROOT / "demo/ground_truth/query_cases.yml").read_text(encoding="utf-8")
    )

    for case in payload["cases"]:
        request = AnalyticalRequest.model_validate(case["interpretation"])
        first = yaml.safe_dump(request.model_dump(mode="json"), sort_keys=False)
        reloaded = AnalyticalRequest.model_validate(yaml.safe_load(first))
        second = yaml.safe_dump(reloaded.model_dump(mode="json"), sort_keys=False)

        assert reloaded == request
        assert second == first
        assert "raw_sql" not in first
        assert "crm." not in first
        assert "bank." not in first


@pytest.mark.parametrize(
    "request_filter",
    [
        {
            "field": "Customer.customer_status",
            "operator": "is_null",
            "value": "ACTIVE",
        },
        {
            "field": "Customer.customer_status",
            "operator": "in",
            "value": [],
        },
        {
            "field": "Customer.customer_status",
            "operator": "equals",
            "value": None,
        },
    ],
)
def test_analytical_filter_value_shape_must_match_closed_operator(
    request_filter: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        Filter.model_validate(request_filter)


def test_analytical_request_rejects_duplicate_metrics_and_unselected_order_field() -> None:
    duplicate_metric = {
        "primary_entity": "Customer",
        "metrics": [
            {"operation": "count_distinct", "field": "Customer.customer_key"},
            {"operation": "count_distinct", "field": "Customer.customer_key"},
        ],
    }
    with pytest.raises(ValidationError, match="metrics must be unique"):
        AnalyticalRequest.model_validate(duplicate_metric)

    unselected_order = {
        "primary_entity": "Customer",
        "metrics": [{"operation": "count", "field": "Customer.customer_key"}],
        "order_by": [{"field": "Customer.registration_date", "direction": "asc"}],
    }
    with pytest.raises(ValidationError, match="must also be selected"):
        AnalyticalRequest.model_validate(unselected_order)


def test_analytical_request_has_no_free_form_sql_or_operation_fields() -> None:
    payload = {
        "primary_entity": "Customer",
        "metrics": [{"operation": "count", "field": "Customer.customer_key"}],
        "raw_sql": "SELECT customer_id FROM crm.customers",
    }
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AnalyticalRequest.model_validate(payload)

    serialized = json.dumps(AnalyticalRequest.model_json_schema(), sort_keys=True)
    assert "raw_sql" not in serialized
    assert "expression_sql" not in serialized
