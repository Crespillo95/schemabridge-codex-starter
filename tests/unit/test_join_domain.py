"""Pure cardinality, evidence, fanout, and adversarial join invariants."""

import pytest
from psycopg import sql
from pydantic import ValidationError

from schemabridge.adapters.postgres.relationships import (
    PsycopgRelationshipEvidenceAdapter,
    _normalization_expression,
)
from schemabridge.application.join_demo import build_north_star_join_proposals
from schemabridge.application.ports.relationships import (
    RelationshipErrorCode,
    RelationshipWorkflowError,
)
from schemabridge.domain.concepts import LogicalFieldRef
from schemabridge.domain.decisions import ApprovalStatus
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.joins import (
    Cardinality,
    DeclaredRelationship,
    FanoutPolicy,
    JoinContract,
    JoinProposal,
    JoinRecommendation,
    JoinScoringConfig,
    NormalizedJoinKey,
    RelationshipContextEvidence,
    RelationshipProfile,
    classify_cardinality,
    score_join_candidate,
)
from schemabridge.domain.transformations import IdentityStep, TransformationPlan


def _profile(**changes: object) -> RelationshipProfile:
    values: dict[str, object] = {
        "left_row_count": 7,
        "right_row_count": 9,
        "left_null_count": 0,
        "right_null_count": 1,
        "left_invalid_count": 0,
        "right_invalid_count": 2,
        "left_distinct_valid": 7,
        "right_distinct_valid": 5,
        "matching_distinct_keys": 5,
        "left_max_multiplicity": 1,
        "right_max_multiplicity": 2,
    }
    values.update(changes)
    return RelationshipProfile.model_validate(values)


def test_customer_holder_cardinality_explains_duplicate_links_and_fanout() -> None:
    proposal = build_north_star_join_proposals()[0]
    profile = _profile()

    candidate = score_join_candidate(proposal, profile)

    assert candidate.cardinality.cardinality is Cardinality.ONE_TO_MANY
    assert candidate.recommendation is JoinRecommendation.RECOMMEND_FOR_REVIEW
    assert profile.right_duplicate_rows == 1
    assert profile.overlap_ratio == 1.0
    assert candidate.fanout_warning is not None
    assert "customer 123" in candidate.fanout_warning
    assert "COUNT DISTINCT" in candidate.fanout_warning


def test_account_holder_to_account_uses_declared_foreign_key_direction() -> None:
    profile = _profile(
        left_row_count=9,
        right_row_count=9,
        left_null_count=0,
        right_null_count=0,
        left_invalid_count=0,
        right_invalid_count=0,
        left_distinct_valid=9,
        right_distinct_valid=9,
        matching_distinct_keys=9,
        left_max_multiplicity=1,
        right_max_multiplicity=1,
        declared_relationship=DeclaredRelationship.LEFT_FOREIGN_KEY_TO_RIGHT,
    )

    assessment = classify_cardinality(profile)

    assert assessment.cardinality is Cardinality.MANY_TO_ONE
    assert "declared foreign key" in " ".join(assessment.evidence)
    assert "currently unique" in " ".join(assessment.risks)


def test_matching_names_with_poor_overlap_are_not_recommended() -> None:
    proposal = build_north_star_join_proposals()[1]
    candidate = score_join_candidate(
        proposal,
        _profile(
            left_row_count=100,
            right_row_count=100,
            left_null_count=0,
            right_null_count=0,
            left_invalid_count=0,
            right_invalid_count=0,
            left_distinct_valid=100,
            right_distinct_valid=100,
            matching_distinct_keys=2,
            left_max_multiplicity=1,
            right_max_multiplicity=1,
        ),
    )

    assert candidate.confidence.root >= JoinScoringConfig().weights.names_and_definitions
    assert candidate.recommendation is JoinRecommendation.MORE_EVIDENCE_REQUIRED
    assert "below the recommendation minimum" in " ".join(candidate.risks)


def test_wrong_physical_key_is_rejected_before_any_database_connection() -> None:
    allowed = build_north_star_join_proposals()
    wrong = JoinProposal(
        id="account_holder_to_account",
        left_key=allowed[1].left_key,
        right_key=NormalizedJoinKey(
            logical_field=LogicalFieldRef("Account.account_key"),
            physical_field=PhysicalFieldRef("bank.accounts.opening_date"),
            transformation_plan=TransformationPlan(steps=(IdentityStep(),)),
        ),
    )
    adapter = PsycopgRelationshipEvidenceAdapter(
        "postgresql://should-not-connect.invalid/example", allowed
    )

    with pytest.raises(RelationshipWorkflowError) as raised:
        adapter.profile(wrong)
    assert raised.value.code is RelationshipErrorCode.EVIDENCE_NOT_ALLOWED


def test_float_join_normalization_rejects_negative_identifiers() -> None:
    key = build_north_star_join_proposals()[0].right_key

    expression = _normalization_expression(key, sql.Identifier("gf_customer_id")).as_string()

    assert '"gf_customer_id" >= 0' in expression
    assert "ABS" not in expression


def test_nulls_invalid_values_and_ambiguous_paths_remain_visible() -> None:
    candidate = score_join_candidate(
        build_north_star_join_proposals()[0],
        _profile(),
        RelationshipContextEvidence(alternative_path_count=2),
    )

    assert candidate.recommendation is JoinRecommendation.MORE_EVIDENCE_REQUIRED
    assert "null join keys" in " ".join(candidate.risks)
    assert "invalid join keys" in " ".join(candidate.risks)
    assert "ambiguous" in " ".join(candidate.risks)


def test_many_to_many_fails_closed_and_cannot_be_an_approved_contract() -> None:
    candidate = score_join_candidate(
        build_north_star_join_proposals()[0],
        _profile(left_max_multiplicity=2, right_max_multiplicity=2),
    )
    assert candidate.cardinality.cardinality is Cardinality.MANY_TO_MANY
    assert candidate.recommendation is JoinRecommendation.REJECT_UNSAFE

    proposal = candidate.proposal
    with pytest.raises(ValidationError, match="many-to-many execution is unsupported"):
        JoinContract(
            id=proposal.id,
            left_key=proposal.left_key,
            right_key=proposal.right_key,
            cardinality=Cardinality.MANY_TO_MANY,
            default_join_type=proposal.default_join_type,
            fanout_policy=FanoutPolicy.REQUIRE_EXPLICIT_MITIGATION,
            status=ApprovalStatus.APPROVED,
            evidence=("matching names",),
            risks=("many-to-many risk",),
            approval_decision_id="operator-approval",
        )


def test_approved_join_requires_normalized_keys_fanout_policy_and_decision() -> None:
    proposal = build_north_star_join_proposals()[0]
    with pytest.raises(ValidationError, match="distinct left-entity metrics"):
        JoinContract(
            id=proposal.id,
            left_key=proposal.left_key,
            right_key=proposal.right_key,
            cardinality=Cardinality.ONE_TO_MANY,
            default_join_type=proposal.default_join_type,
            fanout_policy=FanoutPolicy.NONE,
            status=ApprovalStatus.APPROVED,
            evidence=("normalized overlap",),
            risks=("fanout",),
            approval_decision_id="operator-approval",
        )
