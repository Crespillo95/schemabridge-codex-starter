"""Pure relationship evidence, cardinality, scoring, and governed join contracts."""

from __future__ import annotations

import math
import re
from enum import StrEnum
from types import MappingProxyType

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.concepts import LogicalFieldRef
from schemabridge.domain.decisions import ApprovalStatus
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.mappings import ConfidenceScore
from schemabridge.domain.transformations import TransformationPlan

_CONTRACT_ID_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]+")
_TOKEN_ALIASES = MappingProxyType(
    {
        "customer": "customer",
        "client": "customer",
        "id": "key",
        "identifier": "key",
        "no": "key",
        "number": "key",
    }
)


class Cardinality(StrEnum):
    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_ONE = "many_to_one"
    MANY_TO_MANY = "many_to_many"


class JoinType(StrEnum):
    INNER = "inner"
    LEFT = "left"


class FanoutPolicy(StrEnum):
    NONE = "none"
    REQUIRE_DISTINCT_FOR_LEFT_ENTITY_METRICS = "require_distinct_for_left_entity_metrics"
    PRE_AGGREGATE_RIGHT_SIDE = "pre_aggregate_right_side"
    REQUIRE_EXPLICIT_MITIGATION = "require_explicit_mitigation"


class DeclaredRelationship(StrEnum):
    NONE = "none"
    LEFT_FOREIGN_KEY_TO_RIGHT = "left_foreign_key_to_right"
    RIGHT_FOREIGN_KEY_TO_LEFT = "right_foreign_key_to_left"


class JoinRecommendation(StrEnum):
    RECOMMEND_FOR_REVIEW = "recommend_for_review"
    MORE_EVIDENCE_REQUIRED = "more_evidence_required"
    REJECT_UNSAFE = "reject_unsafe"


class JoinSignal(StrEnum):
    DECLARED_CONSTRAINT = "declared_constraint"
    LINEAGE = "lineage"
    HISTORICAL_QUERY_USAGE = "historical_query_usage"
    NAMES_AND_DEFINITIONS = "names_and_definitions"
    NORMALIZED_OVERLAP = "normalized_overlap"
    NULL_AND_INVALID_QUALITY = "null_and_invalid_quality"
    UNIQUENESS_AND_CARDINALITY = "uniqueness_and_cardinality"
    ROW_ESTIMATES = "row_estimates"


class NormalizedJoinKey(FrozenDomainModel):
    """One physical key bound to a logical field through a closed transformation plan."""

    logical_field: LogicalFieldRef
    physical_field: PhysicalFieldRef
    transformation_plan: TransformationPlan


class JoinProposal(FrozenDomainModel):
    """Bounded relationship hypothesis; discovery never performs unrestricted all-pairs work."""

    id: str = Field(min_length=1)
    left_key: NormalizedJoinKey
    right_key: NormalizedJoinKey
    default_join_type: JoinType = JoinType.INNER

    @field_validator("id")
    @classmethod
    def id_must_be_inert(cls, value: str) -> str:
        if _CONTRACT_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("join proposal id must be an inert identifier")
        return value

    @model_validator(mode="after")
    def endpoints_must_differ(self) -> JoinProposal:
        if (
            self.left_key.logical_field.root.split(".", 1)[0]
            == self.right_key.logical_field.root.split(".", 1)[0]
        ):
            raise ValueError("join endpoints must be different logical models")
        left_dataset = self.left_key.physical_field.root.rsplit(".", 1)[0]
        right_dataset = self.right_key.physical_field.root.rsplit(".", 1)[0]
        if left_dataset == right_dataset:
            raise ValueError("self joins are outside the MVP")
        return self


class RelationshipProfile(FrozenDomainModel):
    """Bounded aggregate evidence; it contains no raw sampled values."""

    left_row_count: int = Field(ge=0)
    right_row_count: int = Field(ge=0)
    left_null_count: int = Field(ge=0)
    right_null_count: int = Field(ge=0)
    left_invalid_count: int = Field(ge=0)
    right_invalid_count: int = Field(ge=0)
    left_distinct_valid: int = Field(ge=0)
    right_distinct_valid: int = Field(ge=0)
    matching_distinct_keys: int = Field(ge=0)
    left_max_multiplicity: int = Field(ge=0)
    right_max_multiplicity: int = Field(ge=0)
    declared_relationship: DeclaredRelationship = DeclaredRelationship.NONE
    reader_user: str | None = None
    transaction_read_only: bool | None = None
    statement_timeout_ms: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def aggregate_counts_must_be_possible(self) -> RelationshipProfile:
        for side in ("left", "right"):
            rows = getattr(self, f"{side}_row_count")
            nulls = getattr(self, f"{side}_null_count")
            invalid = getattr(self, f"{side}_invalid_count")
            distinct = getattr(self, f"{side}_distinct_valid")
            maximum = getattr(self, f"{side}_max_multiplicity")
            valid = rows - nulls - invalid
            if nulls + invalid > rows or distinct > valid:
                raise ValueError(f"{side} profile counts are inconsistent")
            if (distinct == 0 and maximum != 0) or maximum > valid:
                raise ValueError(f"{side} multiplicity is inconsistent")
            if distinct > 0 and maximum < 1:
                raise ValueError(f"{side} multiplicity is inconsistent")
        if self.matching_distinct_keys > min(self.left_distinct_valid, self.right_distinct_valid):
            raise ValueError("matching key count exceeds the distinct-key bounds")
        return self

    @property
    def left_duplicate_rows(self) -> int:
        return max(
            0,
            self.left_row_count
            - self.left_null_count
            - self.left_invalid_count
            - self.left_distinct_valid,
        )

    @property
    def right_duplicate_rows(self) -> int:
        return max(
            0,
            self.right_row_count
            - self.right_null_count
            - self.right_invalid_count
            - self.right_distinct_valid,
        )

    @property
    def overlap_ratio(self) -> float:
        denominator = min(self.left_distinct_valid, self.right_distinct_valid)
        return 0.0 if denominator == 0 else self.matching_distinct_keys / denominator


class ObservedJoinSignal(FrozenDomainModel):
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    detail: str | None = Field(default=None, min_length=1, max_length=500)
    missing_reason: str | None = Field(default=None, min_length=1, max_length=160)

    @model_validator(mode="after")
    def observation_must_be_present_or_missing(self) -> ObservedJoinSignal:
        if self.score is None:
            if self.detail is not None or self.missing_reason is None:
                raise ValueError("missing join observation requires only a reason")
        elif self.detail is None or self.missing_reason is not None:
            raise ValueError("present join observation requires only score and detail")
        return self

    @classmethod
    def missing(cls, reason: str) -> ObservedJoinSignal:
        return cls(missing_reason=reason)


class RelationshipContextEvidence(FrozenDomainModel):
    """Catalog evidence supplied to the pure scorer with absence kept explicit."""

    lineage: ObservedJoinSignal = Field(
        default_factory=lambda: ObservedJoinSignal.missing("lineage_not_recorded")
    )
    historical_query_usage: ObservedJoinSignal = Field(
        default_factory=lambda: ObservedJoinSignal.missing("query_usage_not_recorded")
    )
    left_description: str | None = None
    right_description: str | None = None
    alternative_path_count: int = Field(default=0, ge=0)


class CardinalityAssessment(FrozenDomainModel):
    cardinality: Cardinality
    confidence: ConfidenceScore
    evidence: tuple[str, ...] = Field(min_length=1)
    risks: tuple[str, ...] = ()


class JoinSignalWeights(FrozenDomainModel):
    declared_constraint: float = Field(default=0.15, ge=0.0, le=1.0)
    lineage: float = Field(default=0.05, ge=0.0, le=1.0)
    historical_query_usage: float = Field(default=0.05, ge=0.0, le=1.0)
    names_and_definitions: float = Field(default=0.10, ge=0.0, le=1.0)
    normalized_overlap: float = Field(default=0.30, ge=0.0, le=1.0)
    null_and_invalid_quality: float = Field(default=0.05, ge=0.0, le=1.0)
    uniqueness_and_cardinality: float = Field(default=0.25, ge=0.0, le=1.0)
    row_estimates: float = Field(default=0.05, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def weights_must_sum_to_one(self) -> JoinSignalWeights:
        if not math.isclose(sum(self.as_dict().values()), 1.0, abs_tol=1e-9):
            raise ValueError("join signal weights must sum to one")
        return self

    def as_dict(self) -> dict[JoinSignal, float]:
        return {
            JoinSignal.DECLARED_CONSTRAINT: self.declared_constraint,
            JoinSignal.LINEAGE: self.lineage,
            JoinSignal.HISTORICAL_QUERY_USAGE: self.historical_query_usage,
            JoinSignal.NAMES_AND_DEFINITIONS: self.names_and_definitions,
            JoinSignal.NORMALIZED_OVERLAP: self.normalized_overlap,
            JoinSignal.NULL_AND_INVALID_QUALITY: self.null_and_invalid_quality,
            JoinSignal.UNIQUENESS_AND_CARDINALITY: self.uniqueness_and_cardinality,
            JoinSignal.ROW_ESTIMATES: self.row_estimates,
        }


class JoinScoringConfig(FrozenDomainModel):
    weights: JoinSignalWeights = Field(default_factory=JoinSignalWeights)
    recommendation_threshold: float = Field(default=0.60, gt=0.0, le=1.0)
    minimum_overlap: float = Field(default=0.50, ge=0.0, le=1.0)


class JoinSignalScore(FrozenDomainModel):
    signal: JoinSignal
    weight: float = Field(ge=0.0, le=1.0)
    raw_score: float = Field(ge=0.0, le=1.0)
    weighted_score: float = Field(ge=0.0, le=1.0)
    available: bool
    detail: str | None = None
    missing_reason: str | None = None

    @model_validator(mode="after")
    def contribution_must_be_consistent(self) -> JoinSignalScore:
        expected = self.weight * self.raw_score if self.available else 0.0
        if not math.isclose(expected, self.weighted_score, abs_tol=1e-9):
            raise ValueError("weighted join signal is inconsistent")
        if self.available:
            if self.detail is None or self.missing_reason is not None:
                raise ValueError("available join signal requires only detail")
        elif self.raw_score != 0.0 or self.detail is not None or self.missing_reason is None:
            raise ValueError("missing join signal requires only missing_reason")
        return self


class JoinCandidate(FrozenDomainModel):
    proposal: JoinProposal
    cardinality: CardinalityAssessment
    confidence: ConfidenceScore
    status: ApprovalStatus = ApprovalStatus.NEEDS_REVIEW
    recommendation: JoinRecommendation
    score_breakdown: tuple[JoinSignalScore, ...] = Field(min_length=8, max_length=8)
    evidence: tuple[str, ...] = Field(min_length=1)
    missing_evidence: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    fanout_warning: str | None = None

    @model_validator(mode="after")
    def candidate_is_explainable_but_never_approved(self) -> JoinCandidate:
        if {item.signal for item in self.score_breakdown} != set(JoinSignal):
            raise ValueError("join score breakdown must contain each signal exactly once")
        if not math.isclose(
            sum(item.weighted_score for item in self.score_breakdown),
            self.confidence.root,
            abs_tol=1e-9,
        ):
            raise ValueError("join confidence must equal the score breakdown")
        if self.status is not ApprovalStatus.NEEDS_REVIEW:
            raise ValueError("join candidates require explicit human review")
        if self.cardinality.cardinality is Cardinality.ONE_TO_MANY and not self.fanout_warning:
            raise ValueError("one-to-many candidate requires a fanout warning")
        return self


class JoinContract(FrozenDomainModel):
    """Versioned relationship usable only after an explicit recorded approval."""

    id: str = Field(min_length=1)
    left_key: NormalizedJoinKey
    right_key: NormalizedJoinKey
    cardinality: Cardinality
    default_join_type: JoinType
    fanout_policy: FanoutPolicy
    status: ApprovalStatus
    version: int = Field(default=1, ge=1)
    evidence: tuple[str, ...] = Field(min_length=1)
    risks: tuple[str, ...] = ()
    approval_decision_id: str | None = None

    @field_validator("id")
    @classmethod
    def contract_id_must_be_inert(cls, value: str) -> str:
        if _CONTRACT_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("join contract id must be an inert identifier")
        return value

    @field_validator("evidence", "risks")
    @classmethod
    def entries_must_not_be_blank(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("evidence and risk entries must not be blank")
        return values

    @model_validator(mode="after")
    def relationship_must_be_safe(self) -> JoinContract:
        if (
            self.left_key.logical_field.root.split(".", 1)[0]
            == self.right_key.logical_field.root.split(".", 1)[0]
        ):
            raise ValueError("join endpoints must be different")
        if self.cardinality is Cardinality.ONE_TO_MANY:
            if self.fanout_policy is not FanoutPolicy.REQUIRE_DISTINCT_FOR_LEFT_ENTITY_METRICS:
                raise ValueError(
                    "one-to-many joins require distinct left-entity metrics as fanout mitigation"
                )
            if not self.risks:
                raise ValueError("one-to-many joins require an explicit fanout risk")
        elif self.cardinality is Cardinality.MANY_TO_MANY:
            if self.status is ApprovalStatus.APPROVED:
                raise ValueError("many-to-many execution is unsupported and must fail closed")
            if self.fanout_policy is FanoutPolicy.NONE:
                raise ValueError("many-to-many joins require an explicit unsupported-risk policy")
        elif self.fanout_policy is not FanoutPolicy.NONE:
            raise ValueError("non-fanout joins must use the none fanout policy")
        if self.status is ApprovalStatus.APPROVED and not self.approval_decision_id:
            raise ValueError("approved join contract requires an approval decision")
        if self.status is not ApprovalStatus.APPROVED and self.approval_decision_id is not None:
            raise ValueError("unapproved join contract cannot reference an approval decision")
        return self

    @property
    def left(self) -> LogicalFieldRef:
        """Compatibility view for the logical left endpoint."""

        return self.left_key.logical_field

    @property
    def right(self) -> LogicalFieldRef:
        """Compatibility view for the logical right endpoint."""

        return self.right_key.logical_field


class JoinContractSet(FrozenDomainModel):
    """Deterministically serializable collection of join contracts."""

    version: int = Field(default=1, ge=1)
    contracts: tuple[JoinContract, ...] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def contracts_must_be_unique_and_bounded(self) -> JoinContractSet:
        identifiers = [contract.id for contract in self.contracts]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("join contract ids must be unique")
        logical_models = {
            key.logical_field.root.split(".", 1)[0]
            for contract in self.contracts
            for key in (contract.left_key, contract.right_key)
        }
        if len(logical_models) > 3:
            raise ValueError("the MVP supports at most three logical models")
        return self


def classify_cardinality(profile: RelationshipProfile) -> CardinalityAssessment:
    """Classify direction using declared constraints first and observed multiplicity as a check."""

    left_many = profile.left_max_multiplicity > 1
    right_many = profile.right_max_multiplicity > 1
    observed = (
        Cardinality.MANY_TO_MANY
        if left_many and right_many
        else Cardinality.MANY_TO_ONE
        if left_many
        else Cardinality.ONE_TO_MANY
        if right_many
        else Cardinality.ONE_TO_ONE
    )
    evidence = [
        (
            "observed multiplicity: "
            f"left max={profile.left_max_multiplicity}, right max={profile.right_max_multiplicity}"
        )
    ]
    risks: list[str] = []
    if profile.declared_relationship is DeclaredRelationship.LEFT_FOREIGN_KEY_TO_RIGHT:
        cardinality = Cardinality.MANY_TO_ONE
        evidence.append("declared foreign key: left key references the unique right key")
        if right_many:
            cardinality = Cardinality.MANY_TO_MANY
            risks.append("observed right duplicates conflict with the declared referenced key")
        elif not left_many:
            risks.append(
                "left keys are currently unique, but the declared foreign key permits many-to-one"
            )
    elif profile.declared_relationship is DeclaredRelationship.RIGHT_FOREIGN_KEY_TO_LEFT:
        cardinality = Cardinality.ONE_TO_MANY
        evidence.append("declared foreign key: right key references the unique left key")
        if left_many:
            cardinality = Cardinality.MANY_TO_MANY
            risks.append("observed left duplicates conflict with the declared referenced key")
        elif not right_many:
            risks.append(
                "right keys are currently unique, but the declared foreign key permits one-to-many"
            )
    else:
        cardinality = observed
        evidence.append("no declared foreign key connects the proposed keys")
    confidence = 0.98 if profile.declared_relationship is not DeclaredRelationship.NONE else 0.90
    if risks:
        confidence -= 0.15
    if cardinality is Cardinality.MANY_TO_MANY:
        risks.append("many-to-many execution is unsupported in the MVP")
    return CardinalityAssessment(
        cardinality=cardinality,
        confidence=ConfidenceScore(max(0.0, confidence)),
        evidence=tuple(evidence),
        risks=tuple(dict.fromkeys(risks)),
    )


def score_join_candidate(
    proposal: JoinProposal,
    profile: RelationshipProfile,
    context: RelationshipContextEvidence | None = None,
    config: JoinScoringConfig | None = None,
) -> JoinCandidate:
    """Produce a deterministic, explainable candidate without granting approval."""

    resolved_context = context or RelationshipContextEvidence()
    resolved_config = config or JoinScoringConfig()
    cardinality = classify_cardinality(profile)
    quality_denominator = profile.left_row_count + profile.right_row_count
    rejected = (
        profile.left_null_count
        + profile.right_null_count
        + profile.left_invalid_count
        + profile.right_invalid_count
    )
    quality = 0.0 if quality_denominator == 0 else max(0.0, 1.0 - rejected / quality_denominator)
    observations = {
        JoinSignal.DECLARED_CONSTRAINT: (
            ObservedJoinSignal.missing("no_declared_constraint")
            if profile.declared_relationship is DeclaredRelationship.NONE
            else ObservedJoinSignal(
                score=1.0,
                detail=f"declared relationship: {profile.declared_relationship.value}",
            )
        ),
        JoinSignal.LINEAGE: resolved_context.lineage,
        JoinSignal.HISTORICAL_QUERY_USAGE: resolved_context.historical_query_usage,
        JoinSignal.NAMES_AND_DEFINITIONS: _name_definition_observation(proposal, resolved_context),
        JoinSignal.NORMALIZED_OVERLAP: ObservedJoinSignal(
            score=profile.overlap_ratio,
            detail=(
                f"{profile.matching_distinct_keys} matching normalized keys; "
                f"overlap={profile.overlap_ratio:.3f}"
            ),
        ),
        JoinSignal.NULL_AND_INVALID_QUALITY: ObservedJoinSignal(
            score=quality,
            detail=(
                f"nulls left/right={profile.left_null_count}/{profile.right_null_count}; "
                f"invalid left/right={profile.left_invalid_count}/{profile.right_invalid_count}"
            ),
        ),
        JoinSignal.UNIQUENESS_AND_CARDINALITY: ObservedJoinSignal(
            score=cardinality.confidence.root,
            detail=f"classified {cardinality.cardinality.value}; " + cardinality.evidence[0],
        ),
        JoinSignal.ROW_ESTIMATES: ObservedJoinSignal(
            score=1.0 if quality_denominator > 0 else 0.0,
            detail=f"bounded rows left/right={profile.left_row_count}/{profile.right_row_count}",
        ),
    }
    breakdown = tuple(
        _join_signal_score(
            signal,
            resolved_config.weights.as_dict()[signal],
            observations[signal],
        )
        for signal in JoinSignal
    )
    confidence_value = round(sum(item.weighted_score for item in breakdown), 9)
    risks = list(cardinality.risks)
    if profile.left_null_count or profile.right_null_count:
        risks.append("null join keys are excluded from equality matches")
    if profile.left_invalid_count or profile.right_invalid_count:
        risks.append("invalid join keys are rejected before matching")
    if profile.overlap_ratio < resolved_config.minimum_overlap:
        risks.append("normalized key overlap is below the recommendation minimum")
    if resolved_context.alternative_path_count > 1:
        risks.append("multiple relationship paths are ambiguous and require explicit selection")
    if cardinality.cardinality is Cardinality.ONE_TO_MANY:
        fanout_warning = (
            "Ordinary COUNT(customer) would overcount left entities; customer 123 has duplicate "
            "holder links in the synthetic evidence, so use COUNT DISTINCT."
        )
        risks.append("right-side duplicates can multiply left-entity metrics")
    elif cardinality.cardinality is Cardinality.MANY_TO_MANY:
        fanout_warning = "Many-to-many execution is unsupported and fails closed."
    else:
        fanout_warning = None
    unsafe = cardinality.cardinality is Cardinality.MANY_TO_MANY
    enough_evidence = (
        confidence_value >= resolved_config.recommendation_threshold
        and profile.overlap_ratio >= resolved_config.minimum_overlap
        and resolved_context.alternative_path_count <= 1
    )
    recommendation = (
        JoinRecommendation.REJECT_UNSAFE
        if unsafe
        else JoinRecommendation.RECOMMEND_FOR_REVIEW
        if enough_evidence
        else JoinRecommendation.MORE_EVIDENCE_REQUIRED
    )
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
    return JoinCandidate(
        proposal=proposal,
        cardinality=cardinality,
        confidence=ConfidenceScore(confidence_value),
        recommendation=recommendation,
        score_breakdown=breakdown,
        evidence=evidence or ("no_positive_relationship_signal",),
        missing_evidence=missing,
        risks=tuple(dict.fromkeys(risks)),
        fanout_warning=fanout_warning,
    )


def _join_signal_score(
    signal: JoinSignal,
    weight: float,
    observation: ObservedJoinSignal,
) -> JoinSignalScore:
    available = observation.score is not None
    raw_score = observation.score or 0.0
    return JoinSignalScore(
        signal=signal,
        weight=weight,
        raw_score=raw_score,
        weighted_score=round(weight * raw_score, 12) if available else 0.0,
        available=available,
        detail=observation.detail,
        missing_reason=observation.missing_reason,
    )


def _name_definition_observation(
    proposal: JoinProposal,
    context: RelationshipContextEvidence,
) -> ObservedJoinSignal:
    left = " ".join(
        (
            proposal.left_key.logical_field.root,
            proposal.left_key.physical_field.root.rsplit(".", 1)[1],
            context.left_description or "",
        )
    )
    right = " ".join(
        (
            proposal.right_key.logical_field.root,
            proposal.right_key.physical_field.root.rsplit(".", 1)[1],
            context.right_description or "",
        )
    )
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    union = left_tokens | right_tokens
    score = 0.0 if not union else len(left_tokens & right_tokens) / len(union)
    return ObservedJoinSignal(
        score=min(1.0, score),
        detail=f"normalized name/definition token overlap={score:.3f}",
    )


def _tokens(text: str) -> frozenset[str]:
    return frozenset(
        _TOKEN_ALIASES.get(token, token)
        for token in _TOKEN_PATTERN.findall(text.casefold())
        if token not in {"a", "an", "and", "is", "of", "the", "to"}
    )
