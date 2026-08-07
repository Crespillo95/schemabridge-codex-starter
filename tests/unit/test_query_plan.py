"""Unit tests for the restricted query-plan IR."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from schemabridge.adapters.planning.recorded import RecordedSemanticPlanningContext
from schemabridge.application.query_demo import build_north_star_query_plan
from schemabridge.domain.fields import PhysicalDatasetRef, PhysicalFieldRef
from schemabridge.domain.plans import (
    AggregateExpression,
    ApprovedJoin,
    ColumnExpression,
    DatasetScan,
    JoinPredicate,
    MappedExpression,
    OutputAlias,
    QueryPlan,
    RelationAlias,
    SelectItem,
)
from schemabridge.domain.requests import MetricOperation

ROOT = Path(__file__).resolve().parents[2]


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


def test_query_plan_allows_plain_relationship_count_when_one_to_many_is_reversed() -> None:
    source = build_north_star_query_plan().model_dump(mode="json")
    customer_scan = source["root_scan"]
    holder_scan = source["joins"][0]["right_scan"]
    source["root_scan"] = holder_scan
    source["joins"][0]["right_scan"] = customer_scan
    predicate = source["joins"][0]["on"]
    predicate["left"], predicate["right"] = predicate["right"], predicate["left"]
    source["projections"] = [source["projections"][1]]
    source["projections"][0]["expression"]["operation"] = "count"
    source["filters"] = []
    source["group_by"] = []
    source["order_by"] = []

    plan = QueryPlan.model_validate(source)

    assert plan.projections[0].expression.operation.value == "count"  # type: ignore[union-attr]


def test_query_plan_rejects_predicate_that_does_not_match_approved_join_keys() -> None:
    source = build_north_star_query_plan().model_dump(mode="json")
    source["joins"][0]["on"]["left"]["source"]["field"] = "crm.customers.registration_date"

    with pytest.raises(ValidationError, match="left predicate does not match"):
        QueryPlan.model_validate(source)


def test_query_plan_rejects_reversing_an_approved_left_join() -> None:
    source = build_north_star_query_plan().model_dump(mode="json")
    customer_scan = source["root_scan"]
    holder_scan = source["joins"][0]["right_scan"]
    source["root_scan"] = holder_scan
    source["joins"][0]["right_scan"] = customer_scan
    predicate = source["joins"][0]["on"]
    predicate["left"], predicate["right"] = predicate["right"], predicate["left"]
    source["joins"][0]["contract"]["default_join_type"] = "left"

    with pytest.raises(ValidationError, match="reversing an approved LEFT JOIN"):
        QueryPlan.model_validate(source)


def test_query_plan_rejects_downstream_aggregate_after_prior_fanout() -> None:
    contracts = (
        RecordedSemanticPlanningContext(
            ROOT / "demo/ground_truth/approved_logical_context.yml",
            ROOT / "demo/ground_truth/planning_mappings.yml",
            ROOT / "demo/ground_truth/join_contracts.yml",
        )
        .load()
        .join_contracts.contracts
    )
    customer_to_holder, holder_to_account = contracts
    customer = DatasetScan(
        dataset=PhysicalDatasetRef("crm.customers"),
        alias=RelationAlias("customer"),
    )
    holder = DatasetScan(
        dataset=PhysicalDatasetRef("bank.account_holders"),
        alias=RelationAlias("holder"),
    )
    account = DatasetScan(
        dataset=PhysicalDatasetRef("bank.accounts"),
        alias=RelationAlias("account"),
    )

    with pytest.raises(ValidationError, match="downstream aggregate crosses an earlier fanout"):
        QueryPlan(
            root_scan=customer,
            joins=(
                ApprovedJoin(
                    contract=customer_to_holder,
                    right_scan=holder,
                    on=JoinPredicate(
                        left=MappedExpression(
                            source=ColumnExpression(
                                relation=customer.alias,
                                field=customer_to_holder.left_key.physical_field,
                            ),
                            transformation_plan=customer_to_holder.left_key.transformation_plan,
                        ),
                        right=MappedExpression(
                            source=ColumnExpression(
                                relation=holder.alias,
                                field=customer_to_holder.right_key.physical_field,
                            ),
                            transformation_plan=customer_to_holder.right_key.transformation_plan,
                        ),
                    ),
                ),
                ApprovedJoin(
                    contract=holder_to_account,
                    right_scan=account,
                    on=JoinPredicate(
                        left=MappedExpression(
                            source=ColumnExpression(
                                relation=holder.alias,
                                field=holder_to_account.left_key.physical_field,
                            ),
                            transformation_plan=holder_to_account.left_key.transformation_plan,
                        ),
                        right=MappedExpression(
                            source=ColumnExpression(
                                relation=account.alias,
                                field=holder_to_account.right_key.physical_field,
                            ),
                            transformation_plan=holder_to_account.right_key.transformation_plan,
                        ),
                    ),
                ),
            ),
            projections=(
                SelectItem(
                    expression=AggregateExpression(
                        operation=MetricOperation.SUM,
                        source=ColumnExpression(
                            relation=account.alias,
                            field=PhysicalFieldRef("bank.accounts.current_balance"),
                        ),
                    ),
                    alias=OutputAlias("total_balance"),
                ),
            ),
        )
