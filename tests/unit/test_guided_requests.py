"""Approved-context validation, guided view models, planner seam, and local drafts."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
import yaml

from schemabridge.adapters.requests.fake_planner import FakeRequestPlanner
from schemabridge.adapters.requests.recorded_context import RecordedRequestContextAdapter
from schemabridge.adapters.storage.request_drafts import SqliteRequestDraftStore
from schemabridge.application.guided_requests import (
    BuildGuidedRequest,
    GuidedFilterInput,
    GuidedMetricInput,
    GuidedRequestCase,
    GuidedRequestInput,
    GuidedRequestValidationError,
    LoadRequestDraft,
    SaveRequestDraft,
    SubmitGuidedRequest,
    build_demo_guided_input,
)
from schemabridge.domain.request_context import (
    ApprovedLogicalContext,
    ApprovedLogicalJoin,
)
from schemabridge.domain.requests import AnalyticalRequest

ROOT = Path(__file__).resolve().parents[2]
CONTEXT_PATH = ROOT / "demo/ground_truth/approved_logical_context.yml"


@dataclass(frozen=True, slots=True)
class StaticContext:
    value: ApprovedLogicalContext

    def load(self) -> ApprovedLogicalContext:
        return self.value


def _builder(context: ApprovedLogicalContext | None = None) -> BuildGuidedRequest:
    if context is not None:
        return BuildGuidedRequest(StaticContext(context))
    return BuildGuidedRequest(RecordedRequestContextAdapter(CONTEXT_PATH))


def _query_cases() -> list[dict[str, object]]:
    payload = yaml.safe_load(
        (ROOT / "demo/ground_truth/query_cases.yml").read_text(encoding="utf-8")
    )
    return list(payload["cases"])


def test_guided_north_star_matches_exact_typed_ground_truth_and_fake_planner_seam() -> None:
    builder = _builder()
    validated = builder.execute(build_demo_guided_input(GuidedRequestCase.NORTH_STAR))
    expected = AnalyticalRequest.model_validate(_query_cases()[0]["interpretation"])
    planner = FakeRequestPlanner()
    submission = SubmitGuidedRequest(planner).execute(validated)
    encoded = json.dumps(submission.as_dict(), sort_keys=True)

    assert validated.request == expected
    assert tuple(model.root for model in validated.required_models) == (
        "Customer",
        "AccountHolder",
    )
    assert validated.join_contract_ids == ("customer_to_account_holder",)
    assert submission.summary.requires_join is True
    assert submission.planning.status == "accepted_for_future_planning"
    assert planner.accepted_requests == [validated]
    assert "raw_sql" not in encoded
    assert "crm." not in encoded
    assert "bank." not in encoded


def test_guided_no_join_control_requires_only_customer() -> None:
    validated = _builder().execute(build_demo_guided_input(GuidedRequestCase.NO_JOIN))
    expected = AnalyticalRequest.model_validate(_query_cases()[1]["interpretation"])

    assert validated.request == expected
    assert tuple(model.root for model in validated.required_models) == ("Customer",)
    assert validated.join_contract_ids == ()


@pytest.mark.parametrize(
    ("guided_input", "expected_code"),
    [
        (
            build_demo_guided_input(
                GuidedRequestCase.NORTH_STAR,
                metric_operation="sum",
            ),
            "incompatible_metric_operation",
        ),
        (
            build_demo_guided_input(
                GuidedRequestCase.NORTH_STAR,
                filter_field="Customer.unknown_status",
            ),
            "unknown_logical_field",
        ),
        (
            build_demo_guided_input(GuidedRequestCase.NORTH_STAR, grain="quarter"),
            "unsupported_date_grain",
        ),
    ],
)
def test_guided_request_rejects_invalid_choices_before_planning(
    guided_input: GuidedRequestInput,
    expected_code: str,
) -> None:
    with pytest.raises(GuidedRequestValidationError) as captured:
        _builder().execute(guided_input)

    assert captured.value.result.is_valid is False
    assert captured.value.result.findings[0].code == expected_code


def test_guided_request_accepts_sum_only_for_approved_numeric_measure() -> None:
    guided_input = GuidedRequestInput(
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

    validated = _builder().execute(guided_input)

    assert tuple(model.root for model in validated.required_models) == (
        "Customer",
        "AccountHolder",
        "Account",
    )
    assert validated.join_contract_ids == (
        "customer_to_account_holder",
        "account_holder_to_account",
    )


def test_guided_filter_value_and_operator_must_match_canonical_type() -> None:
    wrong_value = replace(
        build_demo_guided_input(GuidedRequestCase.NO_JOIN),
        filters=(
            GuidedFilterInput(
                field="Customer.customer_status",
                operator="equals",
                value=1,
            ),
        ),
    )
    with pytest.raises(GuidedRequestValidationError) as value_error:
        _builder().execute(wrong_value)
    assert value_error.value.result.findings[0].code == "incompatible_filter_value"

    wrong_operator = replace(
        build_demo_guided_input(GuidedRequestCase.NO_JOIN),
        filters=(
            GuidedFilterInput(
                field="Customer.customer_status",
                operator="greater_than",
                value="ACTIVE",
            ),
        ),
    )
    with pytest.raises(GuidedRequestValidationError) as operator_error:
        _builder().execute(wrong_operator)
    assert operator_error.value.result.findings[0].code == "incompatible_filter_operator"


def test_guided_request_rejects_ambiguous_and_missing_approved_paths() -> None:
    context = RecordedRequestContextAdapter(CONTEXT_PATH).load()
    original = context.joins[0]
    alternate = ApprovedLogicalJoin(
        id="customer_to_holder_alternate",
        left_model=original.left_model,
        right_model=original.right_model,
        cardinality=original.cardinality,
        fanout_policy=original.fanout_policy,
        status=original.status,
        version=original.version,
        approval_decision_id="alternate-approved-decision-v1",
    )
    ambiguous = ApprovedLogicalContext(
        version=context.version,
        source=context.source,
        models=context.models,
        joins=(*context.joins, alternate),
    )
    with pytest.raises(GuidedRequestValidationError) as ambiguous_error:
        _builder(ambiguous).execute(build_demo_guided_input(GuidedRequestCase.NORTH_STAR))
    assert any(
        finding.code == "ambiguous_approved_join_path"
        for finding in ambiguous_error.value.result.findings
    )

    disconnected = ApprovedLogicalContext(
        version=context.version,
        source=context.source,
        models=context.models,
        joins=(),
    )
    with pytest.raises(GuidedRequestValidationError) as missing_error:
        _builder(disconnected).execute(build_demo_guided_input(GuidedRequestCase.NORTH_STAR))
    assert any(
        finding.code == "missing_approved_join_path"
        for finding in missing_error.value.result.findings
    )


def test_guided_options_are_closed_and_come_only_from_approved_context() -> None:
    options = _builder().options().as_dict()
    field_ids = {field["id"] for field in options["fields"]}

    assert options["context_source"].startswith("recorded:")
    assert field_ids == {
        "Customer.customer_key",
        "Customer.registration_date",
        "Customer.country_code",
        "Customer.customer_status",
        "AccountHolder.customer_key",
        "AccountHolder.account_key",
        "AccountHolder.holder_role",
        "Account.account_key",
        "Account.current_balance",
    }
    assert options["metric_operations"] == [
        "count",
        "count_distinct",
        "sum",
        "avg",
        "min",
        "max",
    ]
    assert options["date_grains"] == ["day", "week", "month", "year"]


def test_request_draft_saves_idempotently_and_reload_revalidates_current_context(
    tmp_path: Path,
) -> None:
    store = SqliteRequestDraftStore(tmp_path / "request-drafts.db")
    builder = _builder()
    saver = SaveRequestDraft(store)
    north_star = builder.execute(build_demo_guided_input(GuidedRequestCase.NORTH_STAR))

    first = saver.execute("north-star-demo", north_star.request)
    idempotent = saver.execute("north-star-demo", north_star.request)
    no_join = builder.execute(build_demo_guided_input(GuidedRequestCase.NO_JOIN))
    second = saver.execute("north-star-demo", no_join.request)
    reloaded = LoadRequestDraft(store, builder).execute("north-star-demo")

    assert first.revision == idempotent.revision == 1
    assert second.revision == 2
    assert reloaded.draft == second
    assert reloaded.validated_request.request == no_join.request


def test_guided_input_is_independent_of_mutable_ui_session_state() -> None:
    original = build_demo_guided_input(GuidedRequestCase.NORTH_STAR)
    changed = replace(original, limit=25)

    assert original.limit == 500
    assert changed.limit == 25
    assert _builder().execute(original).request.limit == 500
    assert _builder().execute(changed).request.limit == 25
