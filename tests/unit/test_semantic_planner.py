"""Pure governed semantic-resolution and fanout tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from schemabridge.adapters.planning.recorded import RecordedSemanticPlanningContext
from schemabridge.adapters.requests.recorded_context import RecordedRequestContextAdapter
from schemabridge.application.governed_execution import PlanSemanticRequest
from schemabridge.application.guided_requests import (
    BuildGuidedRequest,
    GuidedMetricInput,
    GuidedRequestCase,
    GuidedRequestInput,
    build_demo_guided_input,
)
from schemabridge.domain.decisions import ApprovalStatus
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.joins import JoinContract, JoinContractSet
from schemabridge.domain.plans import AggregateExpression
from schemabridge.domain.requests import MetricOperation
from schemabridge.domain.resolution import (
    GovernedFieldMapping,
    GovernedMappingSet,
    PhysicalValueType,
    ResolutionErrorCode,
    ResolutionLimits,
    SemanticPlanningContext,
    SemanticResolutionError,
)

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class StaticPlanningContext:
    value: SemanticPlanningContext

    def load(self) -> SemanticPlanningContext:
        return self.value


def _context() -> SemanticPlanningContext:
    return RecordedSemanticPlanningContext(
        ROOT / "demo/ground_truth/approved_logical_context.yml",
        ROOT / "demo/ground_truth/planning_mappings.yml",
        ROOT / "demo/ground_truth/join_contracts.yml",
    ).load()


def _validated(case: GuidedRequestCase, *, metric_operation: str | None = None):  # type: ignore[no-untyped-def]
    builder = BuildGuidedRequest(
        RecordedRequestContextAdapter(ROOT / "demo/ground_truth/approved_logical_context.yml")
    )
    return builder.execute(build_demo_guided_input(case, metric_operation=metric_operation))


def _planner(context: SemanticPlanningContext | None = None, *, max_tables: int = 3):
    return PlanSemanticRequest(
        StaticPlanningContext(context or _context()),
        ResolutionLimits(max_tables=max_tables),
    )


def test_north_star_resolves_approved_assets_normalization_role_map_and_fanout() -> None:
    resolved = _planner().execute(_validated(GuidedRequestCase.NORTH_STAR))
    selected = {
        item.mapping.logical_field.root: item.mapping for item in resolved.selected_mappings
    }
    aggregate = resolved.query_plan.projections[1].expression

    assert resolved.context_source == "recorded:demo/ground_truth/m10"
    assert resolved.query_plan.root_scan.dataset.root == "crm.customers"
    assert tuple(join.right_scan.dataset.root for join in resolved.query_plan.joins) == (
        "bank.account_holders",
    )
    assert [
        step.operation for step in selected["Customer.customer_key"].transformation_plan.steps
    ] == [
        "trim",
        "validate_regex",
        "strip_leading_zeros",
        "reject_invalid",
    ]
    role_steps = selected["AccountHolder.holder_role"].transformation_plan.steps
    assert role_steps[0].operation == "map_values"
    assert isinstance(aggregate, AggregateExpression)
    assert aggregate.operation is MetricOperation.COUNT_DISTINCT
    assert resolved.fanout_mitigations[0].reason.startswith("customer_to_account_holder")
    assert {check.physical_field.root for check in resolved.rejection_checks} == {
        "crm.customers.customer_id",
        "bank.account_holders.gf_customer_id",
    }


def test_plain_customer_count_is_automatically_mitigated_but_intent_remains_visible() -> None:
    resolved = _planner().execute(
        _validated(GuidedRequestCase.NORTH_STAR, metric_operation="count")
    )
    aggregate = resolved.query_plan.projections[1].expression

    assert resolved.request.metrics[0].operation is MetricOperation.COUNT
    assert isinstance(aggregate, AggregateExpression)
    assert aggregate.operation is MetricOperation.COUNT_DISTINCT
    assert resolved.fanout_mitigations[0].automatic is True
    assert resolved.fanout_mitigations[0].requested_operation is MetricOperation.COUNT


def test_relationship_count_and_no_join_keep_their_explicit_meaning() -> None:
    relationships = _planner().execute(_validated(GuidedRequestCase.RELATIONSHIP_COUNT))
    relationship_aggregate = relationships.query_plan.projections[1].expression
    no_join = _planner().execute(_validated(GuidedRequestCase.NO_JOIN))

    assert isinstance(relationship_aggregate, AggregateExpression)
    assert relationship_aggregate.operation is MetricOperation.COUNT
    assert relationships.fanout_mitigations == ()
    assert any(item.code == "relationship_metric_requested" for item in relationships.assumptions)
    assert no_join.query_plan.joins == ()
    assert {
        item.mapping.physical_field.root.split(".", 2)[0] for item in no_join.selected_mappings
    } == {"crm"}
    assert any(item.code == "no_join_required" for item in no_join.assumptions)


def test_unique_shortest_path_resolves_two_approved_contracts_within_three_tables() -> None:
    builder = BuildGuidedRequest(
        RecordedRequestContextAdapter(ROOT / "demo/ground_truth/approved_logical_context.yml")
    )
    validated = builder.execute(
        GuidedRequestInput(
            primary_entity="Customer",
            dimensions=(),
            metrics=(
                GuidedMetricInput(
                    operation="sum",
                    field="Account.current_balance",
                    alias="total_balance",
                ),
            ),
            limit=25,
        )
    )
    resolved = _planner().execute(validated)

    assert [contract.id for contract in resolved.selected_contracts] == [
        "customer_to_account_holder",
        "account_holder_to_account",
    ]
    assert [resolved.query_plan.root_scan.dataset.root] + [
        join.right_scan.dataset.root for join in resolved.query_plan.joins
    ] == ["crm.customers", "bank.account_holders", "bank.accounts"]


def test_stale_request_unapproved_mapping_and_unapproved_join_block_with_typed_codes() -> None:
    validated = _validated(GuidedRequestCase.NORTH_STAR)
    stale = validated.model_copy(update={"context_version": validated.context_version + 1})
    with pytest.raises(SemanticResolutionError) as stale_error:
        _planner().execute(stale)
    assert stale_error.value.code is ResolutionErrorCode.STALE_LOGICAL_CONTEXT

    context = _context()
    target = next(
        item
        for item in context.mapping_set.mappings
        if item.mapping.logical_field.root == "AccountHolder.holder_role"
    )
    rejected_mapping = target.mapping.model_copy(update={"status": ApprovalStatus.REJECTED})
    replacements = tuple(
        GovernedFieldMapping(mapping=rejected_mapping, physical_type=target.physical_type)
        if item == target
        else item
        for item in context.mapping_set.mappings
    )
    unapproved_mapping_context = context.model_copy(
        update={
            "mapping_set": GovernedMappingSet(
                version=context.mapping_set.version,
                mappings=replacements,
            )
        }
    )
    with pytest.raises(SemanticResolutionError) as mapping_error:
        _planner(unapproved_mapping_context).execute(validated)
    assert mapping_error.value.code is ResolutionErrorCode.UNAPPROVED_MAPPING

    first = context.join_contracts.contracts[0]
    proposed_payload = first.model_dump(mode="json")
    proposed_payload.update(status="needs_review", approval_decision_id=None)
    proposed = JoinContract.model_validate(proposed_payload)
    unapproved_join_context = context.model_copy(
        update={
            "join_contracts": JoinContractSet(
                version=1,
                contracts=(proposed, context.join_contracts.contracts[1]),
            )
        }
    )
    with pytest.raises(SemanticResolutionError) as join_error:
        _planner(unapproved_join_context).execute(validated)
    assert join_error.value.code is ResolutionErrorCode.UNAPPROVED_JOIN


def test_incompatible_approved_physical_type_blocks_before_query_planning() -> None:
    context = _context()
    target = next(
        item
        for item in context.mapping_set.mappings
        if item.mapping.logical_field.root == "Customer.registration_date"
    )
    incompatible = target.model_copy(update={"physical_type": PhysicalValueType.BOOLEAN})
    mappings = tuple(
        incompatible if item == target else item for item in context.mapping_set.mappings
    )
    changed = context.model_copy(
        update={
            "mapping_set": GovernedMappingSet(
                version=context.mapping_set.version,
                mappings=mappings,
            )
        }
    )

    with pytest.raises(SemanticResolutionError) as captured:
        _planner(changed).execute(_validated(GuidedRequestCase.NORTH_STAR))

    assert captured.value.code is ResolutionErrorCode.INCOMPATIBLE_MAPPING_TYPE


def test_disconnected_approved_mappings_do_not_manufacture_a_dataset_path() -> None:
    context = _context()
    target = next(
        item
        for item in context.mapping_set.mappings
        if item.mapping.logical_field.root == "Customer.country_code"
    )
    moved_mapping = target.mapping.model_copy(
        update={"physical_field": PhysicalFieldRef("legacy.client_master.country")}
    )
    moved = GovernedFieldMapping(
        mapping=moved_mapping,
        physical_type=target.physical_type,
        approval_decision_id=target.approval_decision_id,
    )
    mappings = tuple(moved if item == target else item for item in context.mapping_set.mappings)
    changed = context.model_copy(
        update={
            "mapping_set": GovernedMappingSet(
                version=context.mapping_set.version,
                mappings=mappings,
            )
        }
    )

    with pytest.raises(SemanticResolutionError) as captured:
        _planner(changed).execute(_validated(GuidedRequestCase.NO_JOIN))

    assert captured.value.code is ResolutionErrorCode.DISCONNECTED_MAPPING


def test_ambiguous_join_and_table_limit_fail_closed() -> None:
    context = _context()
    first = context.join_contracts.contracts[0]
    alternate_payload = first.model_dump(mode="json")
    alternate_payload.update(
        id="customer_to_account_holder_alternate",
        approval_decision_id="ground-truth-customer-holder-alternate-v1",
    )
    alternate = JoinContract.model_validate(alternate_payload)
    ambiguous = context.model_copy(
        update={
            "join_contracts": JoinContractSet(
                version=1,
                contracts=(first, alternate),
            )
        }
    )
    with pytest.raises(SemanticResolutionError) as ambiguous_error:
        _planner(ambiguous).execute(_validated(GuidedRequestCase.NORTH_STAR))
    assert ambiguous_error.value.code is ResolutionErrorCode.AMBIGUOUS_JOIN

    with pytest.raises(SemanticResolutionError) as limit_error:
        _planner(max_tables=1).execute(_validated(GuidedRequestCase.NORTH_STAR))
    assert limit_error.value.code is ResolutionErrorCode.TOO_MANY_TABLES
