"""Unit tests for the restricted query-plan IR."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from schemabridge.application.query_demo import build_north_star_query_plan
from schemabridge.domain.plans import QueryPlan


def test_query_plan_contains_only_typed_nodes_and_round_trips() -> None:
    plan = build_north_star_query_plan()
    serialized = plan.model_dump(mode="json")
    encoded = json.dumps(serialized, sort_keys=True)

    assert QueryPlan.model_validate(serialized) == plan
    assert "raw_sql" not in encoded
    assert "expression_sql" not in encoded
    assert plan.limit is None
    assert plan.joins[0].contract.status.value == "approved"


def test_query_plan_rejects_unknown_fields_instead_of_raw_sql() -> None:
    source = build_north_star_query_plan().model_dump(mode="json")
    source["raw_sql"] = "SELECT * FROM crm.customers"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        QueryPlan.model_validate(source)


def test_query_plan_rejects_column_outside_its_scan() -> None:
    source = build_north_star_query_plan().model_dump(mode="json")
    source["projections"][0]["expression"]["source"]["field"] = (
        "bank.account_holders.relationship_start_date"
    )

    with pytest.raises(ValidationError, match="does not belong"):
        QueryPlan.model_validate(source)


def test_query_plan_rejects_unapproved_join_contract() -> None:
    source = build_north_star_query_plan().model_dump(mode="json")
    source["joins"][0]["contract"]["status"] = "proposed"

    with pytest.raises(ValidationError, match="approved join"):
        QueryPlan.model_validate(source)


def test_query_plan_rejects_malicious_physical_identifier() -> None:
    source = build_north_star_query_plan().model_dump(mode="json")
    source["projections"][0]["expression"]["source"]["field"] = (
        'crm.customers."customer_id; DROP TABLE crm.customers"'
    )

    with pytest.raises(ValidationError):
        QueryPlan.model_validate(source)


def test_query_plan_rejects_ungrouped_projection() -> None:
    source = build_north_star_query_plan().model_dump(mode="json")
    source["group_by"] = []

    with pytest.raises(ValidationError, match="group_by"):
        QueryPlan.model_validate(source)


def test_query_plan_rejects_plain_count_across_one_to_many_join() -> None:
    source = build_north_star_query_plan().model_dump(mode="json")
    source["projections"][1]["expression"]["operation"] = "count"

    with pytest.raises(ValidationError, match="COUNT DISTINCT"):
        QueryPlan.model_validate(source)
