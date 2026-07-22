"""Pure, explainable semantic-candidate scoring and evaluation contracts."""

from __future__ import annotations

import math
import re
from enum import StrEnum
from types import MappingProxyType

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.concepts import CanonicalField, CanonicalType, LogicalFieldRef
from schemabridge.domain.decisions import ApprovalStatus
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.mappings import ConfidenceScore
from schemabridge.domain.transformations import (
    CastIntegerToStringStep,
    IdentityStep,
    LeadingZeroPolicy,
    PadLeftStep,
    RejectInvalidStep,
    StripLeadingZerosStep,
    TransformationPlan,
    TrimStep,
    ValidateFiniteStep,
    ValidateIntegralStep,
    ValidateRegexStep,
)

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]+")
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "for",
        "from",
        "in",
        "is",
        "of",
        "on",
        "or",
        "stable",
        "source",
        "sources",
        "system",
        "systems",
        "the",
        "to",
        "value",
        "values",
    }
)
_TOKEN_ALIASES = MappingProxyType(
    {
        "client": "customer",
        "cust": "customer",
        "identifier": "key",
        "id": "key",
        "no": "key",
        "number": "key",
        "ref": "key",
        "reference": "key",
    }
)
_IGNORED_NAME_TOKENS = frozenset({"gf", "pk", "src"})


class CandidateSignal(StrEnum):
    """Closed signal set used by every score breakdown."""

    NORMALIZED_NAME = "normalized_name"
    DESCRIPTION_TERMS = "description_terms"
    TYPE_COMPATIBILITY = "type_compatibility"
    VALUE_PATTERN = "value_pattern"
    NORMALIZED_OVERLAP = "normalized_overlap"
    LINEAGE = "lineage"
    HISTORICAL_QUERY_USAGE = "historical_query_usage"


class CandidateRecommendation(StrEnum):
    """Prioritization for human review, never an approval decision."""

    RECOMMEND_FOR_REVIEW = "recommend_for_review"
    MORE_EVIDENCE_REQUIRED = "more_evidence_required"


class DescriptionVerdict(StrEnum):
    """Bounded optional interpretation supplied by an explanation adapter."""

    ALIGNED = "aligned"
    AMBIGUOUS = "ambiguous"
    CONFLICTING = "conflicting"


class EvaluationLabel(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"


class EvaluationCaseKind(StrEnum):
    TRUE_MAPPING = "true_mapping"
    NEGATIVE = "negative"
    HOMONYM = "homonym"
    HIDDEN_SYNONYM = "hidden_synonym"


class CandidateSignalWeights(FrozenDomainModel):
    """Configurable weights that must form one complete normalized score."""

    normalized_name: float = Field(default=0.15, ge=0.0, le=1.0)
    description_terms: float = Field(default=0.20, ge=0.0, le=1.0)
    type_compatibility: float = Field(default=0.15, ge=0.0, le=1.0)
    value_pattern: float = Field(default=0.10, ge=0.0, le=1.0)
    normalized_overlap: float = Field(default=0.25, ge=0.0, le=1.0)
    lineage: float = Field(default=0.075, ge=0.0, le=1.0)
    historical_query_usage: float = Field(default=0.075, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def weights_must_sum_to_one(self) -> CandidateSignalWeights:
        if not math.isclose(sum(self.as_dict().values()), 1.0, abs_tol=1e-9):
            raise ValueError("candidate signal weights must sum to one")
        return self

    def as_dict(self) -> dict[CandidateSignal, float]:
        return {
            CandidateSignal.NORMALIZED_NAME: self.normalized_name,
            CandidateSignal.DESCRIPTION_TERMS: self.description_terms,
            CandidateSignal.TYPE_COMPATIBILITY: self.type_compatibility,
            CandidateSignal.VALUE_PATTERN: self.value_pattern,
            CandidateSignal.NORMALIZED_OVERLAP: self.normalized_overlap,
            CandidateSignal.LINEAGE: self.lineage,
            CandidateSignal.HISTORICAL_QUERY_USAGE: self.historical_query_usage,
        }


class CandidateScoringConfig(FrozenDomainModel):
    """Deterministic recommendation policy; it carries no approval authority."""

    weights: CandidateSignalWeights = Field(default_factory=CandidateSignalWeights)
    recommendation_threshold: float = Field(default=0.60, gt=0.0, le=1.0)


class ObservedSignal(FrozenDomainModel):
    """One bounded external observation or an explicit missing-evidence reason."""

    score: float | None = Field(default=None, ge=0.0, le=1.0)
    detail: str | None = Field(default=None, min_length=1, max_length=300)
    missing_reason: str | None = Field(default=None, min_length=1, max_length=120)

    @model_validator(mode="after")
    def observation_must_be_present_or_missing(self) -> ObservedSignal:
        if self.score is None:
            if self.missing_reason is None or self.detail is not None:
                raise ValueError("missing observation requires only missing_reason")
        elif self.detail is None or self.missing_reason is not None:
            raise ValueError("present observation requires only score and detail")
        return self

    @classmethod
    def missing(cls, reason: str) -> ObservedSignal:
        return cls(missing_reason=reason)


class ExternalCandidateEvidence(FrozenDomainModel):
    """Signals produced outside the pure scoring core for one concept/field pair."""

    logical_field: LogicalFieldRef
    physical_field: PhysicalFieldRef
    value_pattern: ObservedSignal = Field(
        default_factory=lambda: ObservedSignal.missing("value_pattern_not_available")
    )
    normalized_overlap: ObservedSignal = Field(
        default_factory=lambda: ObservedSignal.missing("normalized_overlap_not_available")
    )
    lineage: ObservedSignal = Field(
        default_factory=lambda: ObservedSignal.missing("lineage_not_recorded")
    )
    historical_query_usage: ObservedSignal = Field(
        default_factory=lambda: ObservedSignal.missing("query_usage_not_recorded")
    )


class CandidateFieldMetadata(FrozenDomainModel):
    """Small physical-field view accepted by the pure matching core."""

    id: PhysicalFieldRef
    native_type: str | None = None
    description: str | None = None
    glossary_terms: tuple[str, ...] = ()
    is_part_of_key: bool | None = None

    @field_validator("native_type", "description")
    @classmethod
    def optional_text_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("candidate metadata text must not be blank")
        return value


class DescriptionInterpretation(FrozenDomainModel):
    """Validated explanation-only output; it has no numeric score field."""

    physical_field: PhysicalFieldRef
    verdict: DescriptionVerdict
    explanation: str = Field(min_length=1, max_length=500)

    @field_validator("explanation")
    @classmethod
    def explanation_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("description explanation must not be blank")
        return value


class CandidateSignalScore(FrozenDomainModel):
    """Traceable contribution of one configured signal."""

    signal: CandidateSignal
    weight: float = Field(ge=0.0, le=1.0)
    raw_score: float = Field(ge=0.0, le=1.0)
    weighted_score: float = Field(ge=0.0, le=1.0)
    available: bool
    detail: str | None = None
    missing_reason: str | None = None

    @model_validator(mode="after")
    def contribution_must_be_consistent(self) -> CandidateSignalScore:
        expected = self.weight * self.raw_score if self.available else 0.0
        if not math.isclose(self.weighted_score, expected, abs_tol=1e-9):
            raise ValueError("weighted candidate signal is inconsistent")
        if self.available:
            if self.detail is None or self.missing_reason is not None:
                raise ValueError("available candidate signal requires only detail")
        elif self.raw_score != 0.0 or self.detail is not None or self.missing_reason is None:
            raise ValueError("missing candidate signal requires only missing_reason")
        return self


class SemanticCandidate(FrozenDomainModel):
    """Explainable mapping candidate that always remains pending human review."""

    logical_field: LogicalFieldRef
    physical_field: PhysicalFieldRef
    native_type: str | None = None
    confidence: ConfidenceScore
    status: ApprovalStatus = ApprovalStatus.NEEDS_REVIEW
    recommendation: CandidateRecommendation
    score_breakdown: tuple[CandidateSignalScore, ...] = Field(min_length=7, max_length=7)
    evidence: tuple[str, ...] = Field(min_length=1)
    missing_evidence: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    suggested_transformation_plan: TransformationPlan
    deterministic_explanation: str = Field(min_length=1)
    semantic_explanation: DescriptionInterpretation | None = None

    @model_validator(mode="after")
    def score_and_governance_must_remain_consistent(self) -> SemanticCandidate:
        signals = {item.signal for item in self.score_breakdown}
        if signals != set(CandidateSignal):
            raise ValueError("candidate score breakdown must contain every signal exactly once")
        if not math.isclose(
            sum(item.weight for item in self.score_breakdown),
            1.0,
            abs_tol=1e-9,
        ):
            raise ValueError("candidate score weights must sum to one")
        if not math.isclose(
            sum(item.weighted_score for item in self.score_breakdown),
            self.confidence.root,
            abs_tol=1e-9,
        ):
            raise ValueError("candidate confidence must equal the score breakdown")
        if self.status is not ApprovalStatus.NEEDS_REVIEW:
            raise ValueError("semantic candidates must remain needs_review until human action")
        if (
            self.semantic_explanation is not None
            and self.semantic_explanation.physical_field != self.physical_field
        ):
            raise ValueError("semantic explanation must describe the same physical field")
        return self


class CandidateEvaluationCase(FrozenDomainModel):
    """One explicitly labeled synthetic evaluation example."""

    id: str = Field(min_length=1)
    concept: CanonicalField
    field: CandidateFieldMetadata
    label: EvaluationLabel
    kind: EvaluationCaseKind
    evidence: ExternalCandidateEvidence
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def identities_must_match(self) -> CandidateEvaluationCase:
        if self.evidence.logical_field != self.concept.id:
            raise ValueError("evaluation evidence must reference the evaluated concept")
        if self.evidence.physical_field != self.field.id:
            raise ValueError("evaluation evidence must reference the evaluated physical field")
        return self


class CandidateEvaluationDataset(FrozenDomainModel):
    """Labeled fixture that is never represented as production evidence."""

    fixture_notice: str = Field(min_length=1)
    cases: tuple[CandidateEvaluationCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def case_ids_must_be_unique(self) -> CandidateEvaluationDataset:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate evaluation case ids must be unique")
        if {case.label for case in self.cases} != {
            EvaluationLabel.POSITIVE,
            EvaluationLabel.NEGATIVE,
        }:
            raise ValueError("candidate evaluation requires positive and negative cases")
        if not any(case.kind is EvaluationCaseKind.HOMONYM for case in self.cases):
            raise ValueError("candidate evaluation requires a homonym case")
        return self


class CandidateEvaluationMetrics(FrozenDomainModel):
    """Honest fixture metrics with every error identity retained."""

    fixture_notice: str
    threshold: float = Field(ge=0.0, le=1.0)
    case_count: int = Field(ge=1)
    true_positives: tuple[str, ...]
    true_negatives: tuple[str, ...]
    false_positives: tuple[str, ...]
    false_negatives: tuple[str, ...]
    precision: float = Field(ge=0.0, le=1.0)
    recall: float = Field(ge=0.0, le=1.0)
    f1: float = Field(ge=0.0, le=1.0)
    top_k_recall: dict[int, float]

    @model_validator(mode="after")
    def every_case_must_be_reported(self) -> CandidateEvaluationMetrics:
        reported = (
            self.true_positives + self.true_negatives + self.false_positives + self.false_negatives
        )
        if len(reported) != self.case_count or len(set(reported)) != self.case_count:
            raise ValueError("evaluation metrics must report every case exactly once")
        if any(not 0.0 <= value <= 1.0 for value in self.top_k_recall.values()):
            raise ValueError("top-k recall must be bounded")
        return self


def candidate_is_blocked_in(
    concept: CanonicalField,
    field: CandidateFieldMetadata,
) -> bool:
    """Return true only when bounded metadata makes a field worth scoring."""

    name_score, _ = _name_score(concept, field)
    description = _description_observation(concept, field)
    type_score, _ = _type_score(concept.canonical_type, field.native_type)
    return bool(
        name_score > 0.0
        or (description.score is not None and description.score > 0.0)
        or (field.is_part_of_key is True and type_score >= 0.5)
    )


def score_semantic_candidate(
    concept: CanonicalField,
    field: CandidateFieldMetadata,
    external: ExternalCandidateEvidence | None = None,
    config: CandidateScoringConfig | None = None,
) -> SemanticCandidate:
    """Score one already-blocked field using only closed deterministic signals."""

    resolved_config = config or CandidateScoringConfig()
    resolved_external = external or ExternalCandidateEvidence(
        logical_field=concept.id,
        physical_field=field.id,
    )
    if (
        resolved_external.logical_field != concept.id
        or resolved_external.physical_field != field.id
    ):
        raise ValueError("external evidence identities do not match candidate")

    name_score, name_detail = _name_score(concept, field)
    description = _description_observation(concept, field)
    type_score, type_detail = _type_score(concept.canonical_type, field.native_type)
    observations = {
        CandidateSignal.NORMALIZED_NAME: ObservedSignal(score=name_score, detail=name_detail),
        CandidateSignal.DESCRIPTION_TERMS: description,
        CandidateSignal.TYPE_COMPATIBILITY: ObservedSignal(
            score=type_score,
            detail=type_detail,
        ),
        CandidateSignal.VALUE_PATTERN: resolved_external.value_pattern,
        CandidateSignal.NORMALIZED_OVERLAP: resolved_external.normalized_overlap,
        CandidateSignal.LINEAGE: resolved_external.lineage,
        CandidateSignal.HISTORICAL_QUERY_USAGE: resolved_external.historical_query_usage,
    }
    breakdown = tuple(
        _signal_score(signal, resolved_config.weights.as_dict()[signal], observations[signal])
        for signal in CandidateSignal
    )
    confidence_value = round(sum(item.weighted_score for item in breakdown), 9)
    risks = _candidate_risks(concept, field, resolved_external)
    evidence = tuple(
        f"{item.signal.value}: {item.detail}"
        for item in breakdown
        if item.available and item.raw_score > 0.0 and item.detail is not None
    )
    missing = tuple(
        f"{item.signal.value}: {item.missing_reason}"
        for item in breakdown
        if not item.available and item.missing_reason is not None
    )
    recommendation = (
        CandidateRecommendation.RECOMMEND_FOR_REVIEW
        if confidence_value >= resolved_config.recommendation_threshold
        else CandidateRecommendation.MORE_EVIDENCE_REQUIRED
    )
    return SemanticCandidate(
        logical_field=concept.id,
        physical_field=field.id,
        native_type=field.native_type,
        confidence=ConfidenceScore(confidence_value),
        recommendation=recommendation,
        score_breakdown=breakdown,
        evidence=evidence or ("no_positive_signal",),
        missing_evidence=missing,
        risks=risks,
        suggested_transformation_plan=_suggest_transformation(concept, field),
        deterministic_explanation=(
            f"Weighted deterministic score {confidence_value:.3f}; "
            f"{recommendation.value}; human approval is still required."
        ),
    )


def rank_semantic_candidates(
    candidates: tuple[SemanticCandidate, ...],
) -> tuple[SemanticCandidate, ...]:
    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (-candidate.confidence.root, candidate.physical_field.root),
        )
    )


def evaluate_candidate_fixture(
    dataset: CandidateEvaluationDataset,
    config: CandidateScoringConfig | None = None,
    *,
    top_k: tuple[int, ...] = (1, 3, 5),
) -> CandidateEvaluationMetrics:
    """Evaluate every labeled case and retain all false-positive/negative identities."""

    resolved_config = config or CandidateScoringConfig()
    scored = tuple(
        (
            case,
            score_semantic_candidate(
                case.concept,
                case.field,
                case.evidence,
                resolved_config,
            ),
        )
        for case in dataset.cases
    )
    true_positives: list[str] = []
    true_negatives: list[str] = []
    false_positives: list[str] = []
    false_negatives: list[str] = []
    for case, candidate in scored:
        predicted_positive = candidate.confidence.root >= resolved_config.recommendation_threshold
        if case.label is EvaluationLabel.POSITIVE and predicted_positive:
            true_positives.append(case.id)
        elif case.label is EvaluationLabel.POSITIVE:
            false_negatives.append(case.id)
        elif predicted_positive:
            false_positives.append(case.id)
        else:
            true_negatives.append(case.id)

    precision = _safe_ratio(len(true_positives), len(true_positives) + len(false_positives))
    recall = _safe_ratio(len(true_positives), len(true_positives) + len(false_negatives))
    f1 = _safe_ratio(2.0 * precision * recall, precision + recall)
    ranked = sorted(scored, key=lambda pair: (-pair[1].confidence.root, pair[0].id))
    relevant_count = sum(case.label is EvaluationLabel.POSITIVE for case in dataset.cases)
    top_k_recall = {
        value: _safe_ratio(
            sum(case.label is EvaluationLabel.POSITIVE for case, _ in ranked[:value]),
            relevant_count,
        )
        for value in top_k
    }
    return CandidateEvaluationMetrics(
        fixture_notice=dataset.fixture_notice,
        threshold=resolved_config.recommendation_threshold,
        case_count=len(dataset.cases),
        true_positives=tuple(true_positives),
        true_negatives=tuple(true_negatives),
        false_positives=tuple(false_positives),
        false_negatives=tuple(false_negatives),
        precision=round(precision, 9),
        recall=round(recall, 9),
        f1=round(f1, 9),
        top_k_recall={key: round(value, 9) for key, value in top_k_recall.items()},
    )


def _normalized_tokens(text: str, *, name: bool = False) -> frozenset[str]:
    tokens = []
    for raw_token in _TOKEN_PATTERN.findall(text.casefold()):
        token = _TOKEN_ALIASES.get(raw_token, raw_token)
        if token in _STOPWORDS or (name and token in _IGNORED_NAME_TOKENS):
            continue
        tokens.append(token)
    return frozenset(tokens)


def _name_score(
    concept: CanonicalField,
    field: CandidateFieldMetadata,
) -> tuple[float, str]:
    concept_tokens = _normalized_tokens(
        f"{concept.id.root} {concept.canonical_name}",
        name=True,
    )
    field_tokens = _normalized_tokens(field.id.root.rsplit(".", 1)[1], name=True)
    union = concept_tokens | field_tokens
    score = len(concept_tokens & field_tokens) / len(union) if union else 0.0
    return score, (
        f"normalized tokens concept={sorted(concept_tokens)} field={sorted(field_tokens)}"
    )


def _description_observation(
    concept: CanonicalField,
    field: CandidateFieldMetadata,
) -> ObservedSignal:
    candidate_text = " ".join((field.description or "", *field.glossary_terms)).strip()
    if not candidate_text:
        return ObservedSignal.missing("description_and_terms_missing")
    concept_tokens = _normalized_tokens(concept.definition)
    field_tokens = _normalized_tokens(candidate_text)
    score = len(concept_tokens & field_tokens) / len(concept_tokens) if concept_tokens else 0.0
    return ObservedSignal(
        score=score,
        detail=(
            f"definition tokens matched {sorted(concept_tokens & field_tokens)} "
            f"of {sorted(concept_tokens)}"
        ),
    )


def _type_score(canonical_type: CanonicalType, native_type: str | None) -> tuple[float, str]:
    if native_type is None:
        return 0.0, "native type is missing"
    normalized = native_type.casefold()
    if canonical_type is CanonicalType.STRING:
        if any(marker in normalized for marker in ("char", "text", "string", "uuid")):
            return 1.0, f"{native_type} is string-compatible"
        if "int" in normalized:
            return 0.9, f"{native_type} is losslessly transformable to canonical string"
        if any(marker in normalized for marker in ("double", "real", "float")):
            return 0.55, f"{native_type} requires finite integral validation before string cast"
        if any(marker in normalized for marker in ("numeric", "decimal")):
            return 0.6, f"{native_type} requires integral validation before string cast"
        return 0.0, f"{native_type} is not compatible with canonical string"
    exact_markers = {
        CanonicalType.INTEGER: ("int",),
        CanonicalType.DECIMAL: ("numeric", "decimal"),
        CanonicalType.BOOLEAN: ("bool",),
        CanonicalType.DATE: ("date",),
        CanonicalType.TIMESTAMP: ("timestamp",),
    }[canonical_type]
    score = 1.0 if any(marker in normalized for marker in exact_markers) else 0.0
    return score, f"{native_type} {'matches' if score else 'does not match'} {canonical_type.value}"


def _signal_score(
    signal: CandidateSignal,
    weight: float,
    observation: ObservedSignal,
) -> CandidateSignalScore:
    if observation.score is None:
        return CandidateSignalScore(
            signal=signal,
            weight=weight,
            raw_score=0.0,
            weighted_score=0.0,
            available=False,
            missing_reason=observation.missing_reason,
        )
    return CandidateSignalScore(
        signal=signal,
        weight=weight,
        raw_score=observation.score,
        weighted_score=weight * observation.score,
        available=True,
        detail=observation.detail,
    )


def _candidate_risks(
    concept: CanonicalField,
    field: CandidateFieldMetadata,
    external: ExternalCandidateEvidence,
) -> tuple[str, ...]:
    native = (field.native_type or "").casefold()
    risks: list[str] = []
    if any(marker in native for marker in ("double", "real", "float")):
        risks.extend(("unsafe_float_identifier", "finite_integral_validation_required"))
    elif "int" in native and concept.canonical_type is CanonicalType.STRING:
        risks.append("integer_to_string_transformation_required")
    if (
        concept.format_policy is not None
        and concept.format_policy.leading_zero_policy is LeadingZeroPolicy.STRIP
        and any(marker in native for marker in ("char", "text", "string"))
    ):
        risks.append("leading_zero_semantics_require_review")
    if field.description is None and not field.glossary_terms:
        risks.append("missing_description_and_terms")
    if external.normalized_overlap.score is not None and external.normalized_overlap.score < 0.5:
        risks.append("low_normalized_value_overlap")
    type_score, _ = _type_score(concept.canonical_type, field.native_type)
    if type_score == 0.0:
        risks.append("incompatible_native_type")
    return tuple(dict.fromkeys(risks))


def _suggest_transformation(
    concept: CanonicalField,
    field: CandidateFieldMetadata,
) -> TransformationPlan:
    native = (field.native_type or "").casefold()
    type_score, _ = _type_score(concept.canonical_type, field.native_type)
    if type_score == 0.0:
        return TransformationPlan(steps=(RejectInvalidStep(),))
    if concept.canonical_type is not CanonicalType.STRING:
        return TransformationPlan(steps=(IdentityStep(),))
    if any(marker in native for marker in ("double", "real", "float", "numeric", "decimal")):
        return TransformationPlan(
            steps=(
                ValidateFiniteStep(),
                ValidateIntegralStep(),
                CastIntegerToStringStep(),
                RejectInvalidStep(),
            )
        )
    if "int" in native:
        return TransformationPlan(
            steps=(CastIntegerToStringStep(), RejectInvalidStep()),
        )
    if concept.format_policy is None:
        return TransformationPlan(steps=(IdentityStep(),))
    if concept.format_policy.leading_zero_policy is LeadingZeroPolicy.STRIP:
        return TransformationPlan(
            steps=(
                TrimStep(),
                ValidateRegexStep(pattern=r"^[0-9]+$"),
                StripLeadingZerosStep(),
                RejectInvalidStep(),
            )
        )
    if concept.format_policy.leading_zero_policy is LeadingZeroPolicy.PAD_TO_LENGTH:
        assert concept.format_policy.pad_to_length is not None
        return TransformationPlan(
            steps=(
                TrimStep(),
                ValidateRegexStep(pattern=r"^[0-9]+$"),
                PadLeftStep(length=concept.format_policy.pad_to_length),
                RejectInvalidStep(),
            )
        )
    return TransformationPlan(
        steps=(
            TrimStep(),
            ValidateRegexStep(pattern=r"^[0-9]+$"),
            RejectInvalidStep(),
        )
    )


def _safe_ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0
