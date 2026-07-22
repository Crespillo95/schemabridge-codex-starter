"""Pure semantic candidate scoring invariants and risk behavior."""

import pytest
from pydantic import ValidationError

from schemabridge.application.candidate_demo import build_customer_key_concept
from schemabridge.domain.candidates import (
    CandidateFieldMetadata,
    CandidateRecommendation,
    CandidateScoringConfig,
    CandidateSignal,
    CandidateSignalWeights,
    ExternalCandidateEvidence,
    ObservedSignal,
    SemanticCandidate,
    score_semantic_candidate,
)
from schemabridge.domain.decisions import ApprovalStatus
from schemabridge.domain.fields import PhysicalFieldRef


def _field(
    physical_field: str,
    native_type: str,
    description: str | None,
) -> CandidateFieldMetadata:
    return CandidateFieldMetadata(
        id=PhysicalFieldRef(physical_field),
        native_type=native_type,
        description=description,
    )


def _signal(candidate: SemanticCandidate, signal: CandidateSignal) -> float:
    return next(item.raw_score for item in candidate.score_breakdown if item.signal is signal)


def test_weights_must_sum_to_one_and_breakdown_is_deterministic() -> None:
    with pytest.raises(ValidationError, match="sum to one"):
        CandidateSignalWeights(normalized_name=0.16)

    concept = build_customer_key_concept()
    field = _field(
        "crm.customers.customer_id",
        "VARCHAR(11)",
        "Customer identifier encoded as an 11-character numeric string.",
    )
    evidence = ExternalCandidateEvidence(
        logical_field=concept.id,
        physical_field=field.id,
        value_pattern=ObservedSignal(score=0.98, detail="Synthetic digits."),
        normalized_overlap=ObservedSignal(score=1.0, detail="Synthetic normalized overlap."),
    )

    first = score_semantic_candidate(concept, field, evidence)
    second = score_semantic_candidate(concept, field, evidence)

    assert first == second
    assert sum(item.weight for item in first.score_breakdown) == pytest.approx(1.0)
    assert sum(item.weighted_score for item in first.score_breakdown) == pytest.approx(
        first.confidence.root
    )


def test_name_similarity_alone_cannot_cross_recommendation_threshold() -> None:
    candidate = score_semantic_candidate(
        build_customer_key_concept(),
        _field("support.accounts.customer_key", "VARCHAR(32)", None),
    )

    assert _signal(candidate, CandidateSignal.NORMALIZED_NAME) == 1.0
    assert candidate.confidence.root < CandidateScoringConfig().recommendation_threshold
    assert candidate.recommendation is CandidateRecommendation.MORE_EVIDENCE_REQUIRED
    assert candidate.status is ApprovalStatus.NEEDS_REVIEW


def test_unsafe_float_is_a_risk_and_requires_closed_validation_steps() -> None:
    concept = build_customer_key_concept()
    field = _field(
        "bank.account_holders.gf_customer_id",
        "DOUBLE PRECISION",
        "Customer identifier stored as double precision; integral values are expected.",
    )
    candidate = score_semantic_candidate(
        concept,
        field,
        ExternalCandidateEvidence(
            logical_field=concept.id,
            physical_field=field.id,
            value_pattern=ObservedSignal(
                score=0.65,
                detail="Fractional, non-finite, and NULL rejection paths are present.",
            ),
            normalized_overlap=ObservedSignal(
                score=0.83,
                detail="Most valid synthetic keys overlap.",
            ),
        ),
    )

    assert "unsafe_float_identifier" in candidate.risks
    assert [step.operation for step in candidate.suggested_transformation_plan.steps] == [
        "validate_finite",
        "validate_integral",
        "cast_integer_to_string",
        "reject_invalid",
    ]
    assert candidate.recommendation is CandidateRecommendation.RECOMMEND_FOR_REVIEW
    assert candidate.status is ApprovalStatus.NEEDS_REVIEW


def test_incompatible_type_has_no_conversion_suggestion() -> None:
    candidate = score_semantic_candidate(
        build_customer_key_concept(),
        _field(
            "crm.customers.registration_date",
            "DATE",
            "Date when the customer record was registered.",
        ),
    )

    assert "incompatible_native_type" in candidate.risks
    assert [step.operation for step in candidate.suggested_transformation_plan.steps] == [
        "reject_invalid"
    ]


def test_description_change_affects_only_the_named_description_signal() -> None:
    concept = build_customer_key_concept()
    aligned = score_semantic_candidate(
        concept,
        _field(
            "audit.records.subject_key",
            "VARCHAR(32)",
            "Stable customer identifier from a source system.",
        ),
    )
    misleading = score_semantic_candidate(
        concept,
        _field(
            "audit.records.subject_key",
            "VARCHAR(32)",
            "Operational audit event correlation value.",
        ),
    )

    aligned_scores = {item.signal: item.raw_score for item in aligned.score_breakdown}
    misleading_scores = {item.signal: item.raw_score for item in misleading.score_breakdown}
    changed = {
        signal for signal in CandidateSignal if aligned_scores[signal] != misleading_scores[signal]
    }
    assert changed == {CandidateSignal.DESCRIPTION_TERMS}
    assert aligned.confidence.root > misleading.confidence.root


def test_even_a_high_confidence_candidate_cannot_be_constructed_as_approved() -> None:
    candidate = score_semantic_candidate(
        build_customer_key_concept(),
        _field(
            "crm.customers.customer_id",
            "VARCHAR(11)",
            "Customer identifier.",
        ),
    )
    payload = candidate.model_dump(mode="json")
    payload["status"] = "approved"

    with pytest.raises(ValidationError, match="needs_review"):
        SemanticCandidate.model_validate(payload)
