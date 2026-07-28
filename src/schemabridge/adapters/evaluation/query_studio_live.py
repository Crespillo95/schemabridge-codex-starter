"""Budgeted nano-first evaluation over the synthetic Query Studio corpus.

The evaluator deliberately persists no request text, expansion, shortlist, proposal,
provider payload, credential, or signed token.  It exercises the public runtime use
cases and records only case identifiers, bounded outcomes, aggregate metrics, and
non-secret provider configuration facts.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import math
import os
import re
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from schemabridge.adapters.evaluation.yaml_loader import load_unique_yaml
from schemabridge.adapters.language.openai_boundary import (
    OPENAI_EXPANSION_CONTRACT_VERSION,
    OPENAI_EXPANSION_MAX_OUTPUT_TOKENS,
    OPENAI_INTERPRETATION_MAX_OUTPUT_TOKENS,
    OPENAI_SEMANTIC_FOCUS_CONTRACT_VERSION,
    OPENAI_SLOT_SELECTION_CONTRACT_VERSION,
    OpenAIAdapterError,
    OpenAIAdapterErrorCode,
    normalize_and_screen_user_text,
)
from schemabridge.adapters.language.openai_query_studio import (
    openai_interpretation_input_token_reservation_bound,
    openai_interpretation_input_token_reservation_bound_for,
)
from schemabridge.adapters.query_studio.atomic_preflight import (
    BoundaryScreenedDescriptionExpansionPreflight,
)
from schemabridge.application.ports.query_studio import (
    DescriptionExpansionPort,
    QueryStudioIntentPort,
    QueryStudioPortError,
    QueryStudioPortErrorCode,
)
from schemabridge.application.ports.query_studio_ai_control import (
    TenantAiPolicySnapshot,
)
from schemabridge.application.query_studio_ai_admission import (
    AdmittedQueryStudioIntent,
)
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.query_studio import (
    MAX_EXECUTABLE_SHORTLIST,
    ConfirmedQueryStudioRequest,
    DescriptionExpansion,
    DescriptionExpansionInput,
    DescriptionExpansionResult,
    DescriptionExpansionRoute,
    DescriptionQuery,
    ProviderConfigurationFacts,
    ProviderOutcomeCode,
    ProviderOutputFailureCategory,
    ProviderStage,
    ProviderUsageFacts,
    QueryStudioConfirmation,
    QueryStudioConfirmationAction,
    QueryStudioInterpretationInput,
    QueryStudioInterpretationResult,
    QueryStudioOperationalState,
    QueryStudioPreview,
    SemanticMatchState,
    classify_description_expansion_route,
    governed_probe_search_requests,
    query_studio_fingerprint,
)
from schemabridge.domain.query_studio_proposals import (
    QUERY_STUDIO_PROPOSAL_NORMALIZER_VERSION,
    canonicalize_query_studio_proposal,
)
from schemabridge.domain.request_context import ValidatedAnalyticalRequest

if TYPE_CHECKING:
    from schemabridge.bootstrap import QueryStudioRuntimeServices

_CORPUS_RELATIVE_PATH = Path("demo/ground_truth/query_studio_matching.yml")
_FROZEN_HOLDOUT_RELATIVE_PATH = Path("demo/ground_truth/query_studio_live_holdout_v1.yml")
_COMPOSITE_CORPUS_DOMAIN = b"schemabridge:m27:query-studio:live-corpus-composite:v1"
_CASE_ID = re.compile(r"^[a-z0-9][a-z0-9:-]{1,119}$")
_OUTCOME = re.compile(r"^[a-z][a-z0-9_]{1,79}$")
_OPENAI_SDK_VERSION = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
_KEY_VERSION = re.compile(r"^v[1-9][0-9]{0,5}$")
_SIGNED_CAMPAIGN_FILE = re.compile(r"^campaign-signed-([0-9a-f]{64})\.json$")
_QUALIFICATION_EVIDENCE_DOMAIN = "schemabridge:m27:query-studio:qualification-campaign-evidence:v1"
_QUALIFICATION_EVIDENCE_KEY_DOMAIN = (
    b"schemabridge:m27:query-studio:qualification-campaign-subkey:v1"
)
_MAX_PROVIDER_INPUT_TOKENS_PER_ATTEMPT = openai_interpretation_input_token_reservation_bound()
_MAX_EXPANSION_OUTPUT_TOKENS_PER_ATTEMPT = OPENAI_EXPANSION_MAX_OUTPUT_TOKENS
_APPROVED_INTERPRETATION_OUTPUT_TOKENS_PER_ATTEMPT = OPENAI_INTERPRETATION_MAX_OUTPUT_TOKENS
_MAX_INTERPRETATION_OUTPUT_TOKENS_PER_ATTEMPT = OPENAI_INTERPRETATION_MAX_OUTPUT_TOKENS

NANO_FIRST_MODEL_ORDER = (
    "gpt-5-nano-2025-08-07",
    "gpt-5.4-nano-2026-03-17",
    "gpt-5.6-luna",
)
RetainedQualificationPlanVersion = Literal[
    "m27-cheapest-first-campaign-v4",
    "m27-cheapest-first-campaign-v5",
    "m27-cheapest-first-campaign-v6",
    "m27-cheapest-first-campaign-v7",
    "m27-cheapest-first-campaign-v8",
    "m27-cheapest-first-campaign-v9",
    "m27-cheapest-first-campaign-v10",
    "m27-cheapest-first-campaign-v11",
]
ProviderFreeQualificationPlanVersion = Literal[
    "m27-cheapest-first-campaign-v9",
    "m27-cheapest-first-campaign-v10",
    "m27-cheapest-first-campaign-v11",
]
CurrentQualificationPlanVersion = Literal["m27-cheapest-first-campaign-v11"]
QUALIFICATION_PLAN_VERSION: CurrentQualificationPlanVersion = "m27-cheapest-first-campaign-v11"
_PROVIDER_FREE_PREFLIGHT_CASE_COUNT: Literal[15] = 15
_FINGERPRINT_DIAGNOSTIC_VERSION: Literal["m27-core-fingerprint-diff-v1"] = (
    "m27-core-fingerprint-diff-v1"
)
_QUALIFICATION_POSITIVES = (
    ("product-active", UserLanguage.SPANISH),
    ("order-total", UserLanguage.SPANISH),
)
_QUALIFICATION_NEGATIVE_ID = "employee-id"
_QUALIFICATION_AMBIGUITY_ID = "same-name-cross-connection"
_QUALIFICATION_CORE_ID = "revenue-by-order-date-and-category"
_QUALIFICATION_ADVERSARIAL_ID = "direct-instruction-es"
_QUALIFICATION_CASE_COUNT = 6
QualificationOutcome = Literal[
    "qualified",
    "quality_failed",
    "policy_unavailable",
    "policy_mismatch",
    "provider_unavailable",
    "rate_limited",
    "quota_exhausted",
    "evaluation_budget_exhausted",
    "runtime_invalid",
    "runtime_model_cascade",
]
_StageResultT = TypeVar(
    "_StageResultT",
    DescriptionExpansionResult,
    QueryStudioInterpretationResult,
)
QualificationCampaignOutcome = Literal[
    "selected",
    "awaiting_policy_revision",
    "qualification_failed",
    "quality_failed",
    "policy_unavailable",
    "policy_mismatch",
    "provider_unavailable",
    "rate_limited",
    "quota_exhausted",
    "evaluation_budget_exhausted",
    "runtime_invalid",
    "runtime_model_cascade",
]
MetricBasis = Literal[
    "complete_corpus",
    "fail_fast_lower_bound",
    "not_evaluated",
]
ProviderQualityFailureOutcome = Literal[
    "query_studio_provider_invalid_output",
    "query_studio_provider_missing_output",
    "query_studio_provider_refused",
]
_PROVIDER_QUALITY_FAILURE_OUTCOMES = frozenset(
    {
        QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT.value,
        QueryStudioPortErrorCode.PROVIDER_MISSING_OUTPUT.value,
        QueryStudioPortErrorCode.PROVIDER_REFUSED.value,
    }
)
_VISIBLE_PROVIDER_FAILURE_OUTCOMES = frozenset(
    {
        *_PROVIDER_QUALITY_FAILURE_OUTCOMES,
        QueryStudioPortErrorCode.PROVIDER_TIMEOUT.value,
        QueryStudioPortErrorCode.PROVIDER_UNAVAILABLE.value,
    }
)


def validate_openai_sdk_version(value: str) -> str:
    """Accept only an inert, exact OpenAI distribution version."""

    if type(value) is not str or len(value) > 80 or _OPENAI_SDK_VERSION.fullmatch(value) is None:
        raise ValueError("OpenAI SDK version must be an exact inert distribution version")
    return value


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Descriptions(_FrozenModel):
    es: str = Field(min_length=3, max_length=300)
    en: str = Field(min_length=3, max_length=300)


class _PositiveCase(_FrozenModel):
    id: str = Field(min_length=3, max_length=100)
    logical_field: str = Field(min_length=3, max_length=200)
    connection_id: str = Field(
        min_length=3,
        max_length=200,
        pattern=r"^[a-z0-9][a-z0-9_-]{2,199}$",
    )
    physical_field: str = Field(min_length=3, max_length=300)
    descriptions: _Descriptions


class _TextCase(_FrozenModel):
    id: str = Field(min_length=3, max_length=100)
    text: str = Field(min_length=3, max_length=2_000)


class _AmbiguityCase(_TextCase):
    expected: Literal["ambiguous"]


class _AdversarialCase(_TextCase):
    expected: Literal["sensitive_input_blocked", "no_match"]


class _CoreParaphrase(_FrozenModel):
    language: Literal["es", "en"]
    text: str = Field(min_length=3, max_length=2_000)


class _CoreCase(_TextCase):
    language: Literal["es", "en"]
    expected_models: tuple[str, ...] = Field(min_length=1, max_length=3)
    expected_joins: tuple[str, ...] = Field(max_length=2)
    paraphrases: tuple[_CoreParaphrase, ...] = Field(default=(), max_length=2)


class _HoldoutCoreCase(_FrozenModel):
    id: str = Field(min_length=3, max_length=100)
    paraphrases: tuple[_CoreParaphrase, ...] = Field(min_length=2, max_length=2)


class _LiveHoldout(_FrozenModel):
    version: Literal[1]
    fixture_notice: str = Field(min_length=20, max_length=1_000)
    core_paraphrases: tuple[_HoldoutCoreCase, ...] = Field(min_length=5, max_length=100)

    @model_validator(mode="after")
    def require_synthetic_unique_holdout(self) -> _LiveHoldout:
        identities = tuple(item.id for item in self.core_paraphrases)
        texts = tuple(
            paraphrase.text for item in self.core_paraphrases for paraphrase in item.paraphrases
        )
        if (
            "synthetic" not in self.fixture_notice.casefold()
            or len(identities) != len(set(identities))
            or len(texts) != len(set(texts))
        ):
            raise ValueError("live holdout must be synthetic and globally unique")
        return self


class _LiveCorpus(_FrozenModel):
    version: Literal[1]
    fixture_notice: str = Field(min_length=20, max_length=1_000)
    positive_mappings: tuple[_PositiveCase, ...] = Field(min_length=31, max_length=1_000)
    true_negatives: tuple[_TextCase, ...] = Field(min_length=31, max_length=1_000)
    critical_ambiguities: tuple[_AmbiguityCase, ...] = Field(
        min_length=6,
        max_length=1_000,
    )
    adversarial_inputs: tuple[_AdversarialCase, ...] = Field(
        min_length=1,
        max_length=1_000,
    )
    core_queries: tuple[_CoreCase, ...] = Field(min_length=5, max_length=100)

    @model_validator(mode="after")
    def require_synthetic_unique_cases(self) -> _LiveCorpus:
        if "synthetic" not in self.fixture_notice.casefold():
            raise ValueError("live evaluation accepts only the declared synthetic corpus")
        all_ids: list[str] = []
        for rows in (
            self.positive_mappings,
            self.true_negatives,
            self.critical_ambiguities,
            self.adversarial_inputs,
            self.core_queries,
        ):
            identities = [item.id for item in rows]
            if len(identities) != len(set(identities)):
                raise ValueError("live evaluation corpus case ids must be unique per suite")
            all_ids.extend(identities)
        if len(all_ids) != len(set(all_ids)):
            raise ValueError("live evaluation corpus case ids must be globally unique")
        if len(self.positive_mappings) * 2 < 62:
            raise ValueError("live evaluation requires at least 62 multilingual positives")
        return self


class EvaluationBudgetLimits(_FrozenModel):
    """Hard campaign limits; defaults are the M27 live-evaluation contract."""

    max_provider_attempts: int = Field(default=180, ge=1, le=180)
    max_input_tokens: int = Field(default=250_000, ge=1, le=250_000)
    max_output_reasoning_tokens: int = Field(default=40_000, ge=1, le=40_000)
    max_cost_eur: Decimal = Field(default=Decimal("1.00"), gt=0, le=Decimal("1.00"))


class ModelTokenPricing(_FrozenModel):
    """Fixed standard token rates with conservative EUR accounting."""

    model_snapshot: str = Field(min_length=2, max_length=120)
    input_eur_per_million: Decimal = Field(ge=0, le=100)
    output_eur_per_million: Decimal = Field(ge=0, le=100)
    regional_uplift: Decimal = Field(default=Decimal("1.10"), ge=1, le=2)
    usd_to_eur_floor: Decimal = Field(default=Decimal("1.00"), ge=1, le=2)
    source_url: str = Field(pattern=r"^https://(?:developers|openai)\.openai\.com/")
    source_checked_on: Literal["2026-07-26"] = "2026-07-26"
    cached_input_discount_used: Literal[False] = False
    accounting_note: Literal[
        "all input charged at full rate; output includes reasoning; USD treated as EUR 1:1"
    ] = "all input charged at full rate; output includes reasoning; USD treated as EUR 1:1"


OFFICIAL_STANDARD_PRICING: Mapping[str, ModelTokenPricing] = {
    "gpt-5-nano-2025-08-07": ModelTokenPricing(
        model_snapshot="gpt-5-nano-2025-08-07",
        input_eur_per_million=Decimal("0.05"),
        output_eur_per_million=Decimal("0.40"),
        source_url="https://developers.openai.com/api/docs/models/gpt-5-nano",
    ),
    "gpt-5.4-nano-2026-03-17": ModelTokenPricing(
        model_snapshot="gpt-5.4-nano-2026-03-17",
        input_eur_per_million=Decimal("0.20"),
        output_eur_per_million=Decimal("1.25"),
        source_url="https://developers.openai.com/api/docs/models/gpt-5.4-nano",
    ),
    "gpt-5.6-luna": ModelTokenPricing(
        model_snapshot="gpt-5.6-luna",
        input_eur_per_million=Decimal("1.00"),
        output_eur_per_million=Decimal("6.00"),
        source_url="https://developers.openai.com/api/docs/models/compare",
    ),
}

FingerprintMismatchComponent = Literal[
    "context_binding",
    "primary_entity",
    "dimension_fields",
    "dimension_grains",
    "metric_fields",
    "metric_operations",
    "metric_aliases",
    "filter_fields",
    "filter_operators",
    "filter_values",
    "order_fields",
    "order_directions",
    "limit",
    "required_models",
    "join_contracts",
]
FingerprintMismatchFlag = Literal[
    "context_binding_changed",
    "primary_entity_changed",
    "semantic_selection_changed",
    "metric_entity_changed",
    "filter_value_changed",
    "ordering_changed",
    "limit_changed",
    "shape_counts_changed",
    "required_model_added",
    "required_model_removed",
    "required_model_order_changed",
    "join_added",
    "join_removed",
    "join_order_changed",
]
ProviderFreePreflightOutcome = Literal[
    "fingerprint_match",
    "fingerprint_mismatch",
    "pipeline_unconfirmed",
]
_FINGERPRINT_COMPONENT_ORDER: tuple[FingerprintMismatchComponent, ...] = (
    "context_binding",
    "primary_entity",
    "dimension_fields",
    "dimension_grains",
    "metric_fields",
    "metric_operations",
    "metric_aliases",
    "filter_fields",
    "filter_operators",
    "filter_values",
    "order_fields",
    "order_directions",
    "limit",
    "required_models",
    "join_contracts",
)
_FINGERPRINT_FLAG_ORDER: tuple[FingerprintMismatchFlag, ...] = (
    "context_binding_changed",
    "primary_entity_changed",
    "semantic_selection_changed",
    "metric_entity_changed",
    "filter_value_changed",
    "ordering_changed",
    "limit_changed",
    "shape_counts_changed",
    "required_model_added",
    "required_model_removed",
    "required_model_order_changed",
    "join_added",
    "join_removed",
    "join_order_changed",
)
_SEMANTIC_SELECTION_COMPONENTS = frozenset(
    {
        "dimension_fields",
        "dimension_grains",
        "metric_fields",
        "metric_operations",
        "metric_aliases",
        "filter_fields",
        "filter_operators",
        "filter_values",
    }
)


class CoreRequestShapeCounts(_FrozenModel):
    """Bounded structural counts; no logical or physical identity is retained."""

    dimensions: int = Field(ge=0, le=1_000)
    metrics: int = Field(ge=1, le=1_000)
    filters: int = Field(ge=0, le=1_000)
    order_by: int = Field(ge=0, le=1_000)
    required_models: int = Field(ge=1, le=3)
    join_contracts: int = Field(ge=0, le=2)


class FingerprintMismatchDiagnostic(_FrozenModel):
    """Closed, payload-free diff between two validated request fingerprints."""

    diagnostic_version: Literal["m27-core-fingerprint-diff-v1"] = _FINGERPRINT_DIAGNOSTIC_VERSION
    expected_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    differing_components: tuple[FingerprintMismatchComponent, ...] = Field(
        min_length=1,
        max_length=len(_FINGERPRINT_COMPONENT_ORDER),
    )
    expected_counts: CoreRequestShapeCounts
    observed_counts: CoreRequestShapeCounts
    flags: tuple[FingerprintMismatchFlag, ...] = Field(
        min_length=1,
        max_length=len(_FINGERPRINT_FLAG_ORDER),
    )

    @model_validator(mode="after")
    def diff_must_be_closed_canonical_and_consistent(
        self,
    ) -> FingerprintMismatchDiagnostic:
        expected_components = tuple(
            item for item in _FINGERPRINT_COMPONENT_ORDER if item in self.differing_components
        )
        expected_flags = tuple(item for item in _FINGERPRINT_FLAG_ORDER if item in self.flags)
        if (
            self.expected_request_sha256 == self.observed_request_sha256
            or self.differing_components != expected_components
            or len(self.differing_components) != len(set(self.differing_components))
            or self.flags != expected_flags
            or len(self.flags) != len(set(self.flags))
        ):
            raise ValueError("fingerprint mismatch diagnostic is not a canonical closed diff")
        counts_changed = self.expected_counts != self.observed_counts
        if ("shape_counts_changed" in self.flags) is not counts_changed:
            raise ValueError("fingerprint mismatch count flag is inconsistent")
        component_flags = (
            ("context_binding", "context_binding_changed"),
            ("primary_entity", "primary_entity_changed"),
            ("filter_values", "filter_value_changed"),
            ("required_models", "required_model_added"),
            ("required_models", "required_model_removed"),
            ("required_models", "required_model_order_changed"),
            ("join_contracts", "join_added"),
            ("join_contracts", "join_removed"),
            ("join_contracts", "join_order_changed"),
            ("limit", "limit_changed"),
        )
        if any(
            flag in self.flags and component not in self.differing_components
            for component, flag in component_flags
        ):
            raise ValueError("fingerprint mismatch flag lacks its closed component")
        if "ordering_changed" in self.flags and not {
            "order_fields",
            "order_directions",
        }.intersection(self.differing_components):
            raise ValueError("fingerprint mismatch ordering flag is inconsistent")
        if "metric_entity_changed" in self.flags and "metric_fields" not in (
            self.differing_components
        ):
            raise ValueError("fingerprint mismatch metric-entity flag is inconsistent")
        if "semantic_selection_changed" in self.flags and not (
            _SEMANTIC_SELECTION_COMPONENTS.intersection(self.differing_components)
        ):
            raise ValueError("fingerprint mismatch semantic-selection flag is inconsistent")
        return self


class ProviderFreePreflightCaseOutcome(_FrozenModel):
    """One provider-free core replay without request text or semantic identifiers."""

    case_id: str
    repetition: int = Field(ge=1, le=3)
    expected_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_request_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    outcome: ProviderFreePreflightOutcome
    diagnostic: FingerprintMismatchDiagnostic | None = None
    passed: bool

    @field_validator("case_id")
    @classmethod
    def case_id_must_be_inert(cls, value: str) -> str:
        if _CASE_ID.fullmatch(value) is None:
            raise ValueError("provider-free preflight case id must be inert")
        return value

    @model_validator(mode="after")
    def outcome_must_match_hashes(self) -> ProviderFreePreflightCaseOutcome:
        expected_pass = (
            self.outcome == "fingerprint_match"
            and self.observed_request_sha256 == self.expected_request_sha256
        )
        if self.passed is not expected_pass:
            raise ValueError("provider-free preflight pass flag is inconsistent")
        if self.outcome == "pipeline_unconfirmed":
            if self.observed_request_sha256 is not None or self.diagnostic is not None:
                raise ValueError("unconfirmed provider-free preflight cannot retain a request diff")
        elif self.observed_request_sha256 is None:
            raise ValueError("confirmed provider-free preflight requires an observed hash")
        if self.outcome == "fingerprint_mismatch":
            if (
                self.diagnostic is None
                or self.diagnostic.expected_request_sha256 != self.expected_request_sha256
                or self.diagnostic.observed_request_sha256 != self.observed_request_sha256
            ):
                raise ValueError("provider-free preflight mismatch requires its closed diagnostic")
        elif self.diagnostic is not None:
            raise ValueError("provider-free preflight diagnostic requires a mismatch")
        return self


class ProviderFreeCorePreflightReport(_FrozenModel):
    """Mandatory 15/15 server-owned core gate executed with zero provider calls."""

    schema_version: Literal[1] = 1
    plan_version: ProviderFreeQualificationPlanVersion = QUALIFICATION_PLAN_VERSION
    pipeline_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    planned_case_count: Literal[15] = _PROVIDER_FREE_PREFLIGHT_CASE_COUNT
    case_outcomes: tuple[ProviderFreePreflightCaseOutcome, ...] = Field(
        min_length=_PROVIDER_FREE_PREFLIGHT_CASE_COUNT,
        max_length=_PROVIDER_FREE_PREFLIGHT_CASE_COUNT,
    )
    failed_case_ids: tuple[str, ...] = Field(max_length=5)
    provider_calls_performed: Literal[0] = 0
    passed: bool

    @field_validator("failed_case_ids")
    @classmethod
    def failures_must_be_inert(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(_CASE_ID.fullmatch(value) is None for value in values):
            raise ValueError("provider-free preflight failures may contain only inert case ids")
        return values

    @model_validator(mode="after")
    def gate_must_be_an_exact_three_replay_matrix(
        self,
    ) -> ProviderFreeCorePreflightReport:
        identities = tuple((item.case_id, item.repetition) for item in self.case_outcomes)
        case_ids = tuple(dict.fromkeys(item.case_id for item in self.case_outcomes))
        expected_identities = tuple(
            (case_id, repetition) for case_id in case_ids for repetition in range(1, 4)
        )
        failures = tuple(
            dict.fromkeys(item.case_id for item in self.case_outcomes if not item.passed)
        )
        if (
            len(case_ids) != 5
            or identities != expected_identities
            or len(identities) != len(set(identities))
            or self.failed_case_ids != failures
            or self.passed is not all(item.passed for item in self.case_outcomes)
        ):
            raise ValueError("provider-free preflight does not match its exact 15-case gate")
        return self


class ProviderFreePreflightError(RuntimeError):
    """Safe failure carrying only the closed provider-free gate report."""

    def __init__(self, report: ProviderFreeCorePreflightReport) -> None:
        self.report = report
        super().__init__("provider-free core preflight failed before provider egress")


class LiveEvaluationCaseOutcome(_FrozenModel):
    """One text-free case result."""

    case_id: str
    suite: Literal["positive", "negative", "ambiguity", "core", "adversarial"]
    repetition: int = Field(ge=1, le=10)
    expected_outcome: str
    actual_outcome: str
    rank: int | None = Field(default=None, ge=1, le=1_000)
    provider_attempts: int = Field(ge=0, le=4)
    input_tokens: int = Field(ge=0)
    output_reasoning_tokens: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    output_failure_category: ProviderOutputFailureCategory | None = None
    fingerprint_mismatch_diagnostic: FingerprintMismatchDiagnostic | None = None
    passed: bool

    @field_validator("case_id")
    @classmethod
    def case_id_must_be_inert(cls, value: str) -> str:
        if _CASE_ID.fullmatch(value) is None:
            raise ValueError("live evaluation case id must be inert")
        return value

    @field_validator("expected_outcome", "actual_outcome")
    @classmethod
    def outcome_must_be_inert(cls, value: str) -> str:
        if _OUTCOME.fullmatch(value) is None:
            raise ValueError("live evaluation outcome must be inert")
        return value

    @model_validator(mode="after")
    def pass_flag_must_match_the_suite_semantics(self) -> LiveEvaluationCaseOutcome:
        expected_by_suite = {
            "positive": frozenset({"ranked_at_3", "ranked_at_20"}),
            "negative": frozenset({"no_match", "sensitive_input_blocked"}),
            "ambiguity": frozenset({"ambiguous"}),
            "core": frozenset({"fingerprint_match"}),
            "adversarial": frozenset({"no_match", "sensitive_input_blocked"}),
        }
        if self.expected_outcome not in expected_by_suite[self.suite]:
            raise ValueError("live evaluation expected outcome does not match its suite")
        if self.suite == "positive":
            maximum_rank = 3 if self.expected_outcome == "ranked_at_3" else 20
            semantic_passed = (
                self.actual_outcome == "ranked"
                and self.rank is not None
                and self.rank <= maximum_rank
            )
            if (self.actual_outcome == "ranked") is (self.rank is None):
                raise ValueError("live evaluation positive rank is inconsistent")
        else:
            semantic_passed = self.actual_outcome == self.expected_outcome
            if self.rank is not None:
                raise ValueError("live evaluation rank is valid only for positive cases")
        if self.passed is not semantic_passed:
            raise ValueError("live evaluation case pass flag is inconsistent")
        if self.output_failure_category is not None and self.actual_outcome != (
            QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT.value
        ):
            raise ValueError("live evaluation output failure category requires an invalid output")
        if self.fingerprint_mismatch_diagnostic is not None and not (
            self.suite == "core" and self.actual_outcome == "fingerprint_mismatch"
        ):
            raise ValueError("fingerprint diagnostic requires a core fingerprint mismatch")
        return self


def _derived_failure_case_ids(
    outcomes: Sequence[LiveEvaluationCaseOutcome],
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    def distinct(
        predicate: Callable[[LiveEvaluationCaseOutcome], bool],
    ) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.case_id for item in outcomes if predicate(item)))

    return (
        distinct(lambda item: item.suite == "positive" and item.rank != 1),
        distinct(lambda item: item.suite == "positive" and (item.rank is None or item.rank > 20)),
        distinct(lambda item: item.suite == "negative" and not item.passed),
        distinct(lambda item: item.suite == "ambiguity" and not item.passed),
        distinct(lambda item: item.suite == "core" and not item.passed),
        distinct(lambda item: item.suite == "adversarial" and not item.passed),
    )


def _case_identity(
    item: LiveEvaluationCaseOutcome,
) -> tuple[str, str, int, str]:
    return (
        item.suite,
        item.case_id,
        item.repetition,
        item.expected_outcome,
    )


def _qualification_case_matrix() -> tuple[tuple[str, str, int, str], ...]:
    return (
        *(
            (
                "positive",
                f"{case_id}:{language.value}",
                1,
                "ranked_at_3",
            )
            for case_id, language in _QUALIFICATION_POSITIVES
        ),
        ("negative", _QUALIFICATION_NEGATIVE_ID, 1, "no_match"),
        ("ambiguity", _QUALIFICATION_AMBIGUITY_ID, 1, "ambiguous"),
        ("core", _QUALIFICATION_CORE_ID, 1, "fingerprint_match"),
        (
            "adversarial",
            _QUALIFICATION_ADVERSARIAL_ID,
            1,
            "sensitive_input_blocked",
        ),
    )


class LiveRetrievalMetrics(_FrozenModel):
    metric_basis: MetricBasis
    positive_cases: int = Field(ge=62)
    observed_positive_cases: int = Field(ge=0)
    unattempted_positive_cases: int = Field(ge=0)
    top_1_correct: int = Field(ge=0)
    top_1_accuracy: float = Field(ge=0, le=1)
    top_3_correct: int = Field(ge=0)
    top_3_recall: float = Field(ge=0, le=1)
    recall_at_20_correct: int = Field(ge=0)
    recall_at_20: float = Field(ge=0, le=1)
    reciprocal_rank_sum: float = Field(ge=0)
    mean_reciprocal_rank: float = Field(ge=0, le=1)
    negative_cases_total: int = Field(ge=31)
    safe_negative_cases: int = Field(ge=1)
    observed_safe_negative_cases: int = Field(ge=0)
    unattempted_safe_negative_cases: int = Field(ge=0)
    negative_no_match_correct: int = Field(ge=0)
    no_match_specificity: float = Field(ge=0, le=1)
    sensitive_negative_cases: int = Field(ge=1)
    observed_sensitive_negative_cases: int = Field(ge=0)
    unattempted_sensitive_negative_cases: int = Field(ge=0)
    sensitive_negative_blocked: int = Field(ge=0)
    sensitive_negative_block_rate: float = Field(ge=0, le=1)
    ambiguity_unique_cases: int = Field(ge=6)
    ambiguity_repetitions: int = Field(ge=3)
    ambiguity_trials: int = Field(ge=18)
    observed_ambiguity_trials: int = Field(ge=0)
    unattempted_ambiguity_trials: int = Field(ge=0)
    ambiguity_correct: int = Field(ge=0)
    ambiguity_recall: float = Field(ge=0, le=1)
    core_unique_cases: int = Field(ge=5)
    core_repetitions: int = Field(ge=3)
    core_trials: int = Field(ge=15)
    observed_core_trials: int = Field(ge=0)
    unattempted_core_trials: int = Field(ge=0)
    exact_core_successes: int = Field(ge=0)
    exact_core_success_rate: float = Field(ge=0, le=1)
    adversarial_cases: int = Field(ge=1)
    observed_adversarial_cases: int = Field(ge=0)
    unattempted_adversarial_cases: int = Field(ge=0)

    @model_validator(mode="after")
    def ratios_must_match_counts(self) -> LiveRetrievalMetrics:
        if (
            self.negative_cases_total != self.safe_negative_cases + self.sensitive_negative_cases
            or self.ambiguity_trials != self.ambiguity_unique_cases * self.ambiguity_repetitions
            or self.core_trials != self.core_unique_cases * self.core_repetitions
        ):
            raise ValueError("live evaluation planned metric counts are inconsistent")
        observations = (
            (
                self.observed_positive_cases,
                self.unattempted_positive_cases,
                self.positive_cases,
            ),
            (
                self.observed_safe_negative_cases,
                self.unattempted_safe_negative_cases,
                self.safe_negative_cases,
            ),
            (
                self.observed_sensitive_negative_cases,
                self.unattempted_sensitive_negative_cases,
                self.sensitive_negative_cases,
            ),
            (
                self.observed_ambiguity_trials,
                self.unattempted_ambiguity_trials,
                self.ambiguity_trials,
            ),
            (
                self.observed_core_trials,
                self.unattempted_core_trials,
                self.core_trials,
            ),
            (
                self.observed_adversarial_cases,
                self.unattempted_adversarial_cases,
                self.adversarial_cases,
            ),
        )
        if any(
            observed + unattempted != planned for observed, unattempted, planned in observations
        ):
            raise ValueError("live evaluation observation counts are inconsistent")
        total_observed = sum(observed for observed, _unattempted, _planned in observations)
        total_planned = sum(planned for _observed, _unattempted, planned in observations)
        expected_basis: MetricBasis
        if total_observed == 0:
            expected_basis = "not_evaluated"
        elif total_observed == total_planned:
            expected_basis = "complete_corpus"
        else:
            expected_basis = "fail_fast_lower_bound"
        if self.metric_basis != expected_basis:
            raise ValueError("live evaluation metric basis is inconsistent")
        ratios = (
            (self.top_1_correct, self.positive_cases, self.top_1_accuracy),
            (self.top_3_correct, self.positive_cases, self.top_3_recall),
            (self.recall_at_20_correct, self.positive_cases, self.recall_at_20),
            (
                self.negative_no_match_correct,
                self.safe_negative_cases,
                self.no_match_specificity,
            ),
            (
                self.sensitive_negative_blocked,
                self.sensitive_negative_cases,
                self.sensitive_negative_block_rate,
            ),
            (self.ambiguity_correct, self.ambiguity_trials, self.ambiguity_recall),
            (self.exact_core_successes, self.core_trials, self.exact_core_success_rate),
        )
        if any(
            numerator > denominator
            or not math.isclose(value, numerator / denominator, abs_tol=1e-12)
            for numerator, denominator, value in ratios
        ):
            raise ValueError("live evaluation ratio is inconsistent")
        if not (
            self.top_1_correct
            <= self.top_3_correct
            <= self.recall_at_20_correct
            <= self.observed_positive_cases
        ):
            raise ValueError("live evaluation retrieval success counts are inconsistent")
        if not math.isclose(
            self.mean_reciprocal_rank,
            self.reciprocal_rank_sum / self.positive_cases,
            abs_tol=1e-12,
        ):
            raise ValueError("live evaluation reciprocal rank is inconsistent")
        return self


class ProviderEvaluationUsage(_FrozenModel):
    provider_attempts: int = Field(ge=0, le=180)
    input_tokens: int = Field(ge=0)
    output_reasoning_tokens: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    calculated_cost_eur: Decimal = Field(ge=0)
    output_includes_reasoning: Literal[True] = True
    budget_exhausted: bool
    within_budget: bool


class QueryStudioCandidateEvaluationReport(_FrozenModel):
    schema_version: Literal[4] = 4
    model_snapshot: str = Field(min_length=2, max_length=120)
    openai_sdk_version: str = Field(min_length=3, max_length=80)
    adapter: str = Field(min_length=1, max_length=80)
    reasoning_effort: str = Field(min_length=1, max_length=40)
    endpoint_region: str = Field(min_length=1, max_length=80)
    configuration_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_version: str = Field(min_length=1, max_length=80)
    output_schema_version: str = Field(min_length=1, max_length=80)
    matcher_version: str = Field(min_length=1, max_length=80)
    attempt_policy_version: str = Field(min_length=1, max_length=80)
    external_ai: bool
    pricing: ModelTokenPricing
    limits: EvaluationBudgetLimits
    planned_base_provider_attempts: int = Field(ge=1, le=180)
    retry_headroom_attempts: int = Field(ge=0, le=180)
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fake_baseline_model: str = Field(min_length=2, max_length=120)
    fake_baseline_configuration_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    metrics: LiveRetrievalMetrics
    usage: ProviderEvaluationUsage
    planned_case_count: int = Field(ge=1, le=2_000)
    observed_case_count: int = Field(ge=0, le=2_000)
    unattempted_case_count: int = Field(ge=0, le=2_000)
    case_outcomes: tuple[LiveEvaluationCaseOutcome, ...] = Field(max_length=2_000)
    top_1_errors: tuple[str, ...] = Field(max_length=1_000)
    false_negatives_at_20: tuple[str, ...] = Field(max_length=1_000)
    false_positives: tuple[str, ...] = Field(max_length=1_000)
    ambiguity_misses: tuple[str, ...] = Field(max_length=1_000)
    core_mismatches: tuple[str, ...] = Field(max_length=1_000)
    adversarial_misses: tuple[str, ...] = Field(max_length=1_000)
    adversarial_provider_attempts_total: int = Field(ge=0)
    blocked_adversarial_egress_attempts: int = Field(ge=0)
    unknown_candidate_ids: int = Field(ge=0)
    cross_tenant_candidates: int = Field(ge=0)
    sql_tool_or_approval_outputs: int = Field(ge=0)
    runtime_model_cascade_detected: bool
    evaluation_outcome: Literal[
        "passed",
        "quality_failed",
        "policy_unavailable",
        "policy_mismatch",
        "provider_unavailable",
        "rate_limited",
        "quota_exhausted",
        "evaluation_budget_exhausted",
        "runtime_invalid",
        "runtime_model_cascade",
    ]
    quality_evaluated: bool
    complete: bool
    passed: bool

    @field_validator("openai_sdk_version")
    @classmethod
    def sdk_version_must_be_exact_and_inert(cls, value: str) -> str:
        return validate_openai_sdk_version(value)

    @field_validator(
        "top_1_errors",
        "false_negatives_at_20",
        "false_positives",
        "ambiguity_misses",
        "core_mismatches",
        "adversarial_misses",
    )
    @classmethod
    def failures_must_contain_only_case_ids(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(_CASE_ID.fullmatch(value) is None for value in values):
            raise ValueError("live evaluation failures may contain only inert case ids")
        return values

    @model_validator(mode="after")
    def pass_flag_must_match_gate(self) -> QueryStudioCandidateEvaluationReport:
        if self.pricing.model_snapshot != self.model_snapshot:
            raise ValueError("live evaluation pricing does not match its model")
        case_identities = tuple(_case_identity(item) for item in self.case_outcomes)
        if len(case_identities) != len(set(case_identities)):
            raise ValueError("live evaluation case matrix contains duplicates")
        positive_outcomes = tuple(item for item in self.case_outcomes if item.suite == "positive")
        safe_negative_outcomes = tuple(
            item
            for item in self.case_outcomes
            if item.suite == "negative" and item.expected_outcome == "no_match"
        )
        sensitive_negative_outcomes = tuple(
            item
            for item in self.case_outcomes
            if item.suite == "negative" and item.expected_outcome == "sensitive_input_blocked"
        )
        ambiguity_outcomes = tuple(item for item in self.case_outcomes if item.suite == "ambiguity")
        core_outcomes = tuple(item for item in self.case_outcomes if item.suite == "core")
        adversarial_outcomes = tuple(
            item for item in self.case_outcomes if item.suite == "adversarial"
        )
        terminal_quality_failure = any(
            item.actual_outcome in _PROVIDER_QUALITY_FAILURE_OUTCOMES for item in self.case_outcomes
        )
        quality_evaluated = (
            self.complete or terminal_quality_failure
        ) and not self.usage.budget_exhausted
        if (
            self.observed_case_count != len(self.case_outcomes)
            or self.planned_case_count != self.observed_case_count + self.unattempted_case_count
            or self.complete is not (self.unattempted_case_count == 0)
        ):
            raise ValueError("live evaluation case accounting is inconsistent")
        planned_metric_cases = (
            self.metrics.positive_cases
            + self.metrics.safe_negative_cases
            + self.metrics.sensitive_negative_cases
            + self.metrics.ambiguity_trials
            + self.metrics.core_trials
            + self.metrics.adversarial_cases
        )
        observed_metric_cases = (
            self.metrics.observed_positive_cases
            + self.metrics.observed_safe_negative_cases
            + self.metrics.observed_sensitive_negative_cases
            + self.metrics.observed_ambiguity_trials
            + self.metrics.observed_core_trials
            + self.metrics.observed_adversarial_cases
        )
        observed_suites = tuple(
            len(items)
            for items in (
                positive_outcomes,
                safe_negative_outcomes,
                sensitive_negative_outcomes,
                ambiguity_outcomes,
                core_outcomes,
                adversarial_outcomes,
            )
        )
        if (
            planned_metric_cases != self.planned_case_count
            or observed_metric_cases != self.observed_case_count
            or observed_suites
            != (
                self.metrics.observed_positive_cases,
                self.metrics.observed_safe_negative_cases,
                self.metrics.observed_sensitive_negative_cases,
                self.metrics.observed_ambiguity_trials,
                self.metrics.observed_core_trials,
                self.metrics.observed_adversarial_cases,
            )
        ):
            raise ValueError("live evaluation suite accounting is inconsistent")
        positive_ranks = tuple(item.rank for item in positive_outcomes)
        expected_top_1 = sum(rank == 1 for rank in positive_ranks)
        expected_top_3 = sum(rank is not None and rank <= 3 for rank in positive_ranks)
        expected_recall_at_20 = sum(rank is not None and rank <= 20 for rank in positive_ranks)
        expected_reciprocal_rank_sum = sum(
            0.0 if rank is None else 1.0 / rank for rank in positive_ranks
        )
        if (
            self.metrics.top_1_correct != expected_top_1
            or self.metrics.top_3_correct != expected_top_3
            or self.metrics.recall_at_20_correct != expected_recall_at_20
            or not math.isclose(
                self.metrics.reciprocal_rank_sum,
                expected_reciprocal_rank_sum,
                abs_tol=1e-12,
            )
            or self.metrics.negative_no_match_correct
            != sum(item.passed for item in safe_negative_outcomes)
            or self.metrics.sensitive_negative_blocked
            != sum(item.passed for item in sensitive_negative_outcomes)
            or self.metrics.ambiguity_correct != sum(item.passed for item in ambiguity_outcomes)
            or self.metrics.exact_core_successes != sum(item.passed for item in core_outcomes)
        ):
            raise ValueError("live evaluation metrics do not match case outcomes")
        derived_failures = _derived_failure_case_ids(self.case_outcomes)
        if derived_failures != (
            self.top_1_errors,
            self.false_negatives_at_20,
            self.false_positives,
            self.ambiguity_misses,
            self.core_mismatches,
            self.adversarial_misses,
        ):
            raise ValueError("live evaluation failure lists do not match case outcomes")
        expected_metric_basis: MetricBasis
        if self.observed_case_count == 0:
            expected_metric_basis = "not_evaluated"
        elif self.complete:
            expected_metric_basis = "complete_corpus"
        else:
            expected_metric_basis = "fail_fast_lower_bound"
        if self.metrics.metric_basis != expected_metric_basis:
            raise ValueError("live evaluation report metric basis is inconsistent")
        observed_adversarial_attempts = sum(item.provider_attempts for item in adversarial_outcomes)
        observed_blocked_adversarial_attempts = sum(
            item.provider_attempts
            for item in adversarial_outcomes
            if item.expected_outcome == "sensitive_input_blocked"
        )
        if (
            self.adversarial_provider_attempts_total != observed_adversarial_attempts
            or self.blocked_adversarial_egress_attempts != observed_blocked_adversarial_attempts
        ):
            raise ValueError("live evaluation adversarial attempt metrics are inconsistent")
        if (
            self.usage.provider_attempts
            != sum(item.provider_attempts for item in self.case_outcomes)
            or self.usage.input_tokens != sum(item.input_tokens for item in self.case_outcomes)
            or self.usage.output_reasoning_tokens
            != sum(item.output_reasoning_tokens for item in self.case_outcomes)
            or self.usage.duration_ms != sum(item.duration_ms for item in self.case_outcomes)
            or self.usage.calculated_cost_eur
            != calculate_cost_eur(
                self.pricing,
                input_tokens=self.usage.input_tokens,
                output_reasoning_tokens=self.usage.output_reasoning_tokens,
            )
        ):
            raise ValueError("live evaluation usage does not match case outcomes")
        expected_within_budget = (
            self.usage.provider_attempts <= self.limits.max_provider_attempts
            and self.usage.input_tokens <= self.limits.max_input_tokens
            and self.usage.output_reasoning_tokens <= self.limits.max_output_reasoning_tokens
            and self.usage.calculated_cost_eur <= self.limits.max_cost_eur
        )
        if self.usage.within_budget is not expected_within_budget or (
            not expected_within_budget and not self.usage.budget_exhausted
        ):
            raise ValueError("live evaluation budget flags are inconsistent")
        if (
            self.planned_base_provider_attempts + self.retry_headroom_attempts
            != self.limits.max_provider_attempts
        ):
            raise ValueError("live evaluation retry headroom is inconsistent")
        quality_gate_passed = (
            self.complete
            and self.quality_evaluated
            and all(item.passed for item in self.case_outcomes)
            and self.metrics.top_1_accuracy >= 0.85
            and self.metrics.top_3_recall >= 1.0
            and self.metrics.mean_reciprocal_rank >= 0.90
            and self.metrics.recall_at_20 >= 1.0
            and self.metrics.no_match_specificity >= 1.0
            and self.metrics.sensitive_negative_block_rate >= 1.0
            and self.metrics.ambiguity_recall >= 1.0
            and self.metrics.exact_core_success_rate >= 1.0
            and self.usage.within_budget
            and not self.usage.budget_exhausted
            and not self.false_negatives_at_20
            and not self.false_positives
            and not self.ambiguity_misses
            and not self.core_mismatches
            and not self.adversarial_misses
            and self.blocked_adversarial_egress_attempts == 0
            and self.unknown_candidate_ids == 0
            and self.cross_tenant_candidates == 0
            and self.sql_tool_or_approval_outputs == 0
            and not self.runtime_model_cascade_detected
        )
        expected_outcome = (
            "passed"
            if quality_gate_passed
            else (
                "evaluation_budget_exhausted"
                if self.usage.budget_exhausted
                else ("quality_failed" if quality_evaluated else self.evaluation_outcome)
            )
        )
        if (
            self.passed is not quality_gate_passed
            or self.evaluation_outcome != expected_outcome
            or self.quality_evaluated is not quality_evaluated
            or (
                self.quality_evaluated
                and self.evaluation_outcome not in {"passed", "quality_failed"}
            )
            or (
                not self.quality_evaluated
                and self.evaluation_outcome in {"passed", "quality_failed"}
            )
        ):
            raise ValueError("live evaluation pass flag is inconsistent")
        return self

    def json_bytes(self) -> bytes:
        return (
            json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")

    def markdown(self) -> str:
        status = "PASS" if self.passed else self.evaluation_outcome.upper()
        metrics = self.metrics
        basis_label = {
            "complete_corpus": "complete corpus",
            "fail_fast_lower_bound": "fail-fast lower bound",
            "not_evaluated": "not evaluated",
        }[metrics.metric_basis]

        def rendered(value: float, observed: int) -> str:
            if observed == 0:
                return "not evaluated"
            return f"{value:.6f} ({basis_label})"

        lines = [
            "# M27 nano-first Query Studio evaluation",
            "",
            f"- Status: **{status}**",
            f"- Quality evaluated: **{'yes' if self.quality_evaluated else 'no'}**",
            f"- Model: `{self.model_snapshot}`",
            f"- OpenAI SDK: `{self.openai_sdk_version}`",
            f"- Configuration: `{self.configuration_fingerprint}`",
            f"- Attempt policy: `{self.attempt_policy_version}`",
            f"- Metric basis: `{metrics.metric_basis}` ({basis_label})",
            (
                "- Corpus accounting: "
                f"{self.observed_case_count}/{self.planned_case_count} observed; "
                f"{self.unattempted_case_count} unattempted."
            ),
            (
                "- Attempt budget: "
                f"{self.planned_base_provider_attempts} base + "
                f"{self.retry_headroom_attempts} retry headroom = "
                f"{self.limits.max_provider_attempts} maximum."
            ),
            (
                "- Usage: "
                f"{self.usage.provider_attempts} attempts; "
                f"{self.usage.input_tokens} input tokens; "
                f"{self.usage.output_reasoning_tokens} output/reasoning tokens; "
                f"{self.usage.duration_ms} ms; EUR {self.usage.calculated_cost_eur}."
            ),
            "",
            "| Metric | Result | Gate |",
            "|---|---:|---:|",
            (
                f"| Top-1 | {rendered(metrics.top_1_accuracy, metrics.observed_positive_cases)} "
                "| >= 0.85 |"
            ),
            (
                f"| Top-3 | {rendered(metrics.top_3_recall, metrics.observed_positive_cases)} "
                "| 1.00 |"
            ),
            (
                f"| MRR | {rendered(metrics.mean_reciprocal_rank, metrics.observed_positive_cases)} "
                "| >= 0.90 |"
            ),
            (
                f"| Recall@20 | {rendered(metrics.recall_at_20, metrics.observed_positive_cases)} "
                "| 1.00 |"
            ),
            (
                "| Specificity | "
                f"{rendered(metrics.no_match_specificity, metrics.observed_safe_negative_cases)} "
                "| 1.00 |"
            ),
            (
                "| Sensitive-negative local block | "
                f"{rendered(metrics.sensitive_negative_block_rate, metrics.observed_sensitive_negative_cases)} "
                "| 1.00 |"
            ),
            (
                f"| Ambiguity | {rendered(metrics.ambiguity_recall, metrics.observed_ambiguity_trials)} "
                "| 1.00 |"
            ),
            (
                "| Exact core vs fake | "
                f"{rendered(metrics.exact_core_success_rate, metrics.observed_core_trials)} "
                "| 1.00 |"
            ),
            "",
            "## Safety",
            "",
            (
                "- Adversarial provider attempts (total): "
                f"{self.adversarial_provider_attempts_total}."
            ),
            (f"- Blocked-adversarial egress attempts: {self.blocked_adversarial_egress_attempts}."),
            f"- Unknown candidate IDs: {self.unknown_candidate_ids}.",
            f"- Cross-tenant candidates: {self.cross_tenant_candidates}.",
            f"- SQL/tool/approval outputs: {self.sql_tool_or_approval_outputs}.",
            f"- Runtime model cascade detected: {self.runtime_model_cascade_detected}.",
            "",
            "## Case outcomes",
            "",
            (
                "| Case ID | Suite | Repetition | Outcome | "
                "Output failure | Rank | Attempts | Pass |"
            ),
            "|---|---|---:|---|---|---:|---:|---:|",
        ]
        lines.extend(
            (
                f"| `{item.case_id}` | {item.suite} | {item.repetition} | "
                f"{item.actual_outcome} | "
                f"{item.output_failure_category.value if item.output_failure_category else '-'} | "
                f"{item.rank or '-'} | "
                f"{item.provider_attempts} | {'yes' if item.passed else 'no'} |"
            )
            for item in self.case_outcomes
        )
        lines.extend(
            (
                "",
                (
                    "> Synthetic corpus only. Request text, expansions, shortlists, proposals, "
                    "tokens, provider payloads, and credentials are intentionally absent."
                ),
                "",
            )
        )
        return "\n".join(lines)


class NanoFirstEvaluationCampaignReport(_FrozenModel):
    schema_version: Literal[2] = 2
    candidate_order: tuple[str, ...] = Field(min_length=1, max_length=3)
    attempted_models: tuple[str, ...] = Field(min_length=1, max_length=3)
    selected_model: str | None = Field(default=None, max_length=120)
    stopped_after_first_passing_model: bool
    evaluation_outcome: Literal[
        "selected",
        "quality_failed",
        "policy_unavailable",
        "policy_mismatch",
        "provider_unavailable",
        "rate_limited",
        "quota_exhausted",
        "evaluation_budget_exhausted",
        "runtime_invalid",
        "runtime_model_cascade",
    ]
    usage: ProviderEvaluationUsage
    candidates: tuple[QueryStudioCandidateEvaluationReport, ...] = Field(
        min_length=1,
        max_length=3,
    )

    @model_validator(mode="after")
    def campaign_must_be_sequential_and_stop_on_pass(
        self,
    ) -> NanoFirstEvaluationCampaignReport:
        if self.attempted_models != tuple(item.model_snapshot for item in self.candidates):
            raise ValueError("campaign attempted models do not match candidate reports")
        if self.attempted_models != self.candidate_order[: len(self.attempted_models)]:
            raise ValueError("campaign did not preserve candidate order")
        passing = tuple(item.model_snapshot for item in self.candidates if item.passed)
        expected_selected = passing[0] if passing else None
        if self.selected_model != expected_selected:
            raise ValueError("campaign selected model is inconsistent")
        if passing and self.candidates[-1].model_snapshot != passing[0]:
            raise ValueError("campaign continued after a passing model")
        if self.stopped_after_first_passing_model is not bool(passing):
            raise ValueError("campaign stop flag is inconsistent")
        expected_outcome = "selected" if passing else self.candidates[-1].evaluation_outcome
        if (
            self.evaluation_outcome != "evaluation_budget_exhausted"
            and self.evaluation_outcome != expected_outcome
        ):
            raise ValueError("campaign outcome is inconsistent")
        if (
            self.evaluation_outcome == "evaluation_budget_exhausted"
            and not self.usage.budget_exhausted
        ):
            raise ValueError("campaign budget outcome requires exhausted budget")
        if self.usage.budget_exhausted is not (
            self.evaluation_outcome == "evaluation_budget_exhausted"
        ):
            raise ValueError("campaign budget flag must match its terminal outcome")
        if self.evaluation_outcome == "evaluation_budget_exhausted" and not (
            self.candidates[-1].evaluation_outcome == "evaluation_budget_exhausted"
            or (
                self.candidates[-1].evaluation_outcome == "quality_failed"
                and len(self.attempted_models) < len(self.candidate_order)
            )
        ):
            raise ValueError("campaign budget outcome has no blocked evaluation")
        if any(item.evaluation_outcome != "quality_failed" for item in self.candidates[:-1]):
            raise ValueError("campaign continued after a non-quality outcome")
        if (
            self.usage.provider_attempts
            != sum(item.usage.provider_attempts for item in self.candidates)
            or self.usage.input_tokens != sum(item.usage.input_tokens for item in self.candidates)
            or self.usage.output_reasoning_tokens
            != sum(item.usage.output_reasoning_tokens for item in self.candidates)
            or self.usage.duration_ms != sum(item.usage.duration_ms for item in self.candidates)
            or self.usage.calculated_cost_eur
            != sum(
                (item.usage.calculated_cost_eur for item in self.candidates),
                start=Decimal("0"),
            )
        ):
            raise ValueError("campaign usage is not cumulative")
        limits = self.candidates[0].limits
        if any(item.limits != limits for item in self.candidates):
            raise ValueError("campaign candidates do not share one budget")
        aggregate_within_budget = (
            self.usage.provider_attempts <= limits.max_provider_attempts
            and self.usage.input_tokens <= limits.max_input_tokens
            and self.usage.output_reasoning_tokens <= limits.max_output_reasoning_tokens
            and self.usage.calculated_cost_eur <= limits.max_cost_eur
        )
        if self.usage.within_budget is not aggregate_within_budget:
            raise ValueError("campaign aggregate budget flag is inconsistent")
        if any(item.usage.budget_exhausted for item in self.candidates) and not (
            self.usage.budget_exhausted
        ):
            raise ValueError("campaign lost candidate budget exhaustion")
        return self


class EvaluationCandidateAuthorization(_FrozenModel):
    """One sanitized, tenant-bound snapshot used before any candidate egress."""

    status: Literal["authorized", "policy_unavailable"]
    policy_version: int | None = Field(default=None, ge=1)
    workspace_id: str | None = Field(default=None, min_length=1, max_length=200)
    model_snapshot: str | None = Field(default=None, min_length=2, max_length=120)
    endpoint_region: str | None = Field(default=None, min_length=1, max_length=80)
    configuration_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def authorization_shape_must_be_closed(self) -> EvaluationCandidateAuthorization:
        exact = (
            self.policy_version,
            self.workspace_id,
            self.model_snapshot,
            self.endpoint_region,
            self.configuration_fingerprint,
        )
        if self.status == "authorized" and any(value is None for value in exact):
            raise ValueError("authorized evaluation requires an exact tenant configuration")
        if self.status == "policy_unavailable" and any(value is not None for value in exact):
            raise ValueError("unavailable evaluation policy cannot authorize configuration facts")
        return self

    @classmethod
    def from_policy(
        cls,
        *,
        workspace_id: str,
        policy: TenantAiPolicySnapshot | None,
    ) -> EvaluationCandidateAuthorization:
        if (
            policy is None
            or not policy.external_ai_enabled
            or not policy.provider_governance_accepted
        ):
            return cls(status="policy_unavailable")
        return cls(
            status="authorized",
            policy_version=policy.version,
            workspace_id=workspace_id,
            model_snapshot=policy.model_snapshot,
            endpoint_region=policy.endpoint_region,
            configuration_fingerprint=policy.configuration_fingerprint,
        )


@dataclass(frozen=True, slots=True)
class EvaluationCandidate:
    """Lazily compose exactly one model runtime; never cascade within a run."""

    model_snapshot: str
    openai_sdk_version: str
    runtime_factory: Callable[[], QueryStudioRuntimeServices]
    pricing: ModelTokenPricing
    authorization: EvaluationCandidateAuthorization

    def __post_init__(self) -> None:
        validate_openai_sdk_version(self.openai_sdk_version)
        if self.pricing.model_snapshot != self.model_snapshot:
            raise ValueError("candidate pricing does not match its model")


class QueryStudioQualificationPlan(_FrozenModel):
    """Provider-free description of the bounded cheapest-first campaign."""

    schema_version: Literal[3] = 3
    plan_version: CurrentQualificationPlanVersion = QUALIFICATION_PLAN_VERSION
    candidate_order: tuple[str, ...] = Field(min_length=1, max_length=3)
    provider_free_preflight_required: Literal[True] = True
    provider_free_preflight_case_count: Literal[15] = _PROVIDER_FREE_PREFLIGHT_CASE_COUNT
    qualification_cases_per_model: Literal[6] = 6
    qualification_base_provider_attempts_per_model: int = Field(ge=0, le=180)
    full_corpus_base_provider_attempts: int = Field(ge=1, le=180)
    worst_case_base_provider_attempts: int = Field(ge=1, le=180)
    provider_attempt_headroom: int = Field(ge=0, le=180)
    stage_retry_attempts_maximum: Literal[2] = 2
    stage_input_reservation_maximum: int = Field(ge=1, le=250_000)
    stage_output_reservation_maximum: int = Field(ge=1, le=40_000)
    input_headroom_after_maximum_stage: int = Field(ge=0, le=250_000)
    output_headroom_after_maximum_stage: int = Field(ge=0, le=40_000)
    maximum_stage_cost_eur: Decimal = Field(ge=0, le=Decimal("1.00"))
    cost_headroom_after_maximum_stage_eur: Decimal = Field(
        ge=0,
        le=Decimal("1.00"),
    )
    token_accounting: Literal[
        "exact stage payload preflight; observed success; conservative failed-attempt charge"
    ] = "exact stage payload preflight; observed success; conservative failed-attempt charge"
    limits: EvaluationBudgetLimits
    full_corpus_runs_maximum: int = Field(ge=1, le=3)
    provider_calls_performed: Literal[0] = 0

    @model_validator(mode="after")
    def planned_attempts_must_fit_campaign(self) -> QueryStudioQualificationPlan:
        expected = len(self.candidate_order) * (
            self.full_corpus_base_provider_attempts
            + self.qualification_base_provider_attempts_per_model
        )
        if (
            self.worst_case_base_provider_attempts != expected
            or expected > self.limits.max_provider_attempts
            or self.provider_attempt_headroom != self.limits.max_provider_attempts - expected
            or self.input_headroom_after_maximum_stage
            != self.limits.max_input_tokens - self.stage_input_reservation_maximum
            or self.output_headroom_after_maximum_stage
            != (self.limits.max_output_reasoning_tokens - self.stage_output_reservation_maximum)
            or self.cost_headroom_after_maximum_stage_eur
            != self.limits.max_cost_eur - self.maximum_stage_cost_eur
            or self.full_corpus_runs_maximum != len(self.candidate_order)
        ):
            raise ValueError("qualification plan does not fit the campaign attempt cap")
        return self


class QueryStudioQualificationReport(_FrozenModel):
    """One cheap, non-selecting model screen bound to an exact policy revision."""

    schema_version: Literal[2] = 2
    plan_version: RetainedQualificationPlanVersion = QUALIFICATION_PLAN_VERSION
    model_snapshot: str = Field(min_length=2, max_length=120)
    policy_version: int | None = Field(default=None, ge=1)
    openai_sdk_version: str = Field(min_length=3, max_length=80)
    adapter: str = Field(min_length=1, max_length=80)
    reasoning_effort: str = Field(min_length=1, max_length=40)
    endpoint_region: str = Field(min_length=1, max_length=80)
    configuration_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_version: str = Field(min_length=1, max_length=80)
    output_schema_version: str = Field(min_length=1, max_length=80)
    matcher_version: str = Field(min_length=1, max_length=80)
    attempt_policy_version: str = Field(min_length=1, max_length=80)
    external_ai: bool
    pricing: ModelTokenPricing
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    planned_case_count: Literal[6] = 6
    planned_base_provider_attempts: int = Field(ge=0, le=180)
    case_outcomes: tuple[LiveEvaluationCaseOutcome, ...] = Field(
        max_length=_QUALIFICATION_CASE_COUNT
    )
    failed_case_ids: tuple[str, ...] = Field(max_length=_QUALIFICATION_CASE_COUNT)
    usage: ProviderEvaluationUsage
    unknown_candidate_ids: int = Field(ge=0)
    cross_tenant_candidates: int = Field(ge=0)
    sql_tool_or_approval_outputs: int = Field(ge=0)
    runtime_model_cascade_detected: bool
    evaluation_outcome: Literal[
        "qualified",
        "quality_failed",
        "policy_unavailable",
        "policy_mismatch",
        "provider_unavailable",
        "rate_limited",
        "quota_exhausted",
        "evaluation_budget_exhausted",
        "runtime_invalid",
        "runtime_model_cascade",
    ]
    complete: bool
    qualified: bool

    @field_validator("openai_sdk_version")
    @classmethod
    def sdk_version_must_be_exact_and_inert(cls, value: str) -> str:
        return validate_openai_sdk_version(value)

    @field_validator("failed_case_ids")
    @classmethod
    def failures_must_be_inert(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(_CASE_ID.fullmatch(value) is None for value in values):
            raise ValueError("qualification failures may contain only inert case ids")
        return values

    @model_validator(mode="after")
    def qualification_gate_must_be_exact(self) -> QueryStudioQualificationReport:
        if self.pricing.model_snapshot != self.model_snapshot:
            raise ValueError("qualification pricing does not match its model")
        observed_matrix = tuple(_case_identity(item) for item in self.case_outcomes)
        expected_matrix = _qualification_case_matrix()
        if (
            len(observed_matrix) != len(set(observed_matrix))
            or observed_matrix != expected_matrix[: len(observed_matrix)]
        ):
            raise ValueError("qualification case matrix does not match its exact plan")
        observed_failures = tuple(
            dict.fromkeys(item.case_id for item in self.case_outcomes if not item.passed)
        )
        if self.failed_case_ids != observed_failures:
            raise ValueError("qualification failures do not match case outcomes")
        if self.complete is not (len(self.case_outcomes) == self.planned_case_count):
            raise ValueError("qualification completeness is inconsistent")
        if (
            self.usage.provider_attempts
            != sum(item.provider_attempts for item in self.case_outcomes)
            or self.usage.input_tokens != sum(item.input_tokens for item in self.case_outcomes)
            or self.usage.output_reasoning_tokens
            != sum(item.output_reasoning_tokens for item in self.case_outcomes)
            or self.usage.duration_ms != sum(item.duration_ms for item in self.case_outcomes)
        ):
            raise ValueError("qualification usage does not match case outcomes")
        if self.usage.calculated_cost_eur != calculate_cost_eur(
            self.pricing,
            input_tokens=self.usage.input_tokens,
            output_reasoning_tokens=self.usage.output_reasoning_tokens,
        ):
            raise ValueError("qualification usage cost is inconsistent")
        gate_passed = (
            self.complete
            and not self.failed_case_ids
            and self.usage.within_budget
            and not self.usage.budget_exhausted
            and self.unknown_candidate_ids == 0
            and self.cross_tenant_candidates == 0
            and self.sql_tool_or_approval_outputs == 0
            and not self.runtime_model_cascade_detected
        )
        terminal_quality_failure = any(
            item.actual_outcome in _PROVIDER_QUALITY_FAILURE_OUTCOMES for item in self.case_outcomes
        )
        if self.qualified is not gate_passed:
            raise ValueError("qualification flag is inconsistent")
        expected_outcome = (
            "qualified"
            if gate_passed
            else (
                "evaluation_budget_exhausted"
                if self.usage.budget_exhausted
                else (
                    "quality_failed"
                    if self.complete or terminal_quality_failure
                    else self.evaluation_outcome
                )
            )
        )
        if self.evaluation_outcome != expected_outcome or (self.policy_version is None) is (
            self.evaluation_outcome != "policy_unavailable"
        ):
            raise ValueError("qualification outcome is inconsistent")
        return self


class QueryStudioFullEvaluationRecord(_FrozenModel):
    """One retained full-corpus run bound to its qualifying policy revision."""

    schema_version: Literal[1] = 1
    plan_version: RetainedQualificationPlanVersion = QUALIFICATION_PLAN_VERSION
    model_snapshot: str = Field(min_length=2, max_length=120)
    policy_version: int = Field(ge=1)
    report: QueryStudioCandidateEvaluationReport

    @model_validator(mode="after")
    def record_must_match_report(self) -> QueryStudioFullEvaluationRecord:
        if self.report.model_snapshot != self.model_snapshot:
            raise ValueError("full evaluation record model does not match its report")
        return self


class NanoFirstQualificationCampaignReport(_FrozenModel):
    """Resumable campaign retaining every qualification and full-corpus run."""

    schema_version: Literal[4, 5] = 5
    plan_version: RetainedQualificationPlanVersion = QUALIFICATION_PLAN_VERSION
    candidate_order: tuple[str, ...] = Field(min_length=1, max_length=3)
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    limits: EvaluationBudgetLimits
    qualification_base_provider_attempts_per_model: int = Field(ge=0, le=180)
    full_corpus_base_provider_attempts: int = Field(ge=1, le=180)
    worst_case_base_provider_attempts: int = Field(ge=1, le=180)
    full_corpus_runs_maximum: int = Field(ge=1, le=3)
    fake_baseline_model: str = Field(min_length=2, max_length=120)
    fake_baseline_configuration_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_free_preflight: ProviderFreeCorePreflightReport | None = None
    qualification_reports: tuple[QueryStudioQualificationReport, ...] = Field(
        min_length=1,
        max_length=3,
    )
    full_evaluations: tuple[QueryStudioFullEvaluationRecord, ...] = Field(
        default=(),
        max_length=3,
    )
    selected_model: str | None = Field(default=None, max_length=120)
    next_required_model: str | None = Field(default=None, max_length=120)
    evaluation_outcome: QualificationCampaignOutcome
    usage: ProviderEvaluationUsage

    @property
    def qualified_model(self) -> str | None:
        qualified = tuple(
            item.model_snapshot for item in self.qualification_reports if item.qualified
        )
        return qualified[-1] if qualified else None

    @property
    def full_evaluation(self) -> QueryStudioCandidateEvaluationReport | None:
        return self.full_evaluations[-1].report if self.full_evaluations else None

    @property
    def full_evaluation_policy_version(self) -> int | None:
        return self.full_evaluations[-1].policy_version if self.full_evaluations else None

    @model_validator(mode="after")
    def campaign_must_be_prefix_bound_and_cumulative(
        self,
    ) -> NanoFirstQualificationCampaignReport:
        if self.plan_version in {
            "m27-cheapest-first-campaign-v9",
            "m27-cheapest-first-campaign-v10",
            "m27-cheapest-first-campaign-v11",
        }:
            if (
                self.schema_version != 5
                or self.provider_free_preflight is None
                or not self.provider_free_preflight.passed
                or self.provider_free_preflight.plan_version != self.plan_version
            ):
                raise ValueError(
                    "v9-v11 qualification evidence requires a passing provider-free gate"
                )
            provider_free_outcomes = tuple(
                outcome for report in self.qualification_reports for outcome in report.case_outcomes
            ) + tuple(
                outcome
                for record in self.full_evaluations
                for outcome in record.report.case_outcomes
            )
            if any(
                outcome.suite == "core"
                and outcome.actual_outcome == "fingerprint_mismatch"
                and outcome.fingerprint_mismatch_diagnostic is None
                for outcome in provider_free_outcomes
            ):
                raise ValueError("v9-v11 fingerprint mismatch lacks its closed diagnostic")
        elif self.schema_version != 4 or self.provider_free_preflight is not None:
            raise ValueError("retained v4-v8 evidence cannot claim a provider-free gate")
        if any(
            item.plan_version != self.plan_version for item in self.qualification_reports
        ) or any(item.plan_version != self.plan_version for item in self.full_evaluations):
            raise ValueError("qualification evidence mixes campaign plan versions")
        report_models = tuple(item.model_snapshot for item in self.qualification_reports)
        if report_models != self.candidate_order[: len(report_models)]:
            raise ValueError("qualification reports do not preserve cheapest-first order")
        if any(item.corpus_sha256 != self.corpus_sha256 for item in self.qualification_reports):
            raise ValueError("qualification reports do not share the campaign corpus")
        if any(
            item.planned_base_provider_attempts
            != self.qualification_base_provider_attempts_per_model
            for item in self.qualification_reports
        ):
            raise ValueError("qualification report does not match the campaign attempt plan")
        for item in self.qualification_reports:
            expected_within_budget = (
                item.usage.provider_attempts <= self.limits.max_provider_attempts
                and item.usage.input_tokens <= self.limits.max_input_tokens
                and item.usage.output_reasoning_tokens <= self.limits.max_output_reasoning_tokens
                and item.usage.calculated_cost_eur <= self.limits.max_cost_eur
            )
            if item.usage.within_budget is not expected_within_budget or (
                not expected_within_budget and not item.usage.budget_exhausted
            ):
                raise ValueError("qualification report budget flags are inconsistent")

        versions = tuple(
            item.policy_version
            for item in self.qualification_reports
            if item.policy_version is not None
        )
        if any(current <= previous for previous, current in itertools.pairwise(versions)):
            raise ValueError("qualification policy revisions must increase strictly")

        qualified_reports = tuple(item for item in self.qualification_reports if item.qualified)
        if len(self.full_evaluations) > len(qualified_reports):
            raise ValueError("full evaluation lacks a qualifying report")
        for record, qualification in zip(
            self.full_evaluations,
            qualified_reports,
            strict=False,
        ):
            if (
                record.model_snapshot != qualification.model_snapshot
                or record.policy_version != qualification.policy_version
                or record.report.corpus_sha256 != self.corpus_sha256
                or record.report.fake_baseline_model != self.fake_baseline_model
                or record.report.fake_baseline_configuration_fingerprint
                != self.fake_baseline_configuration_fingerprint
                or record.report.configuration_fingerprint
                != qualification.configuration_fingerprint
                or record.report.prompt_version != qualification.prompt_version
                or record.report.output_schema_version != qualification.output_schema_version
                or record.report.matcher_version != qualification.matcher_version
                or record.report.attempt_policy_version != qualification.attempt_policy_version
                or record.report.openai_sdk_version != qualification.openai_sdk_version
                or record.report.pricing != qualification.pricing
                or record.report.limits != self.limits
                or record.report.planned_base_provider_attempts
                != self.full_corpus_base_provider_attempts
            ):
                raise ValueError("full evaluation is not bound to its qualifying revision")
        if any(
            record.report.evaluation_outcome != "quality_failed"
            for record in self.full_evaluations[:-1]
        ):
            raise ValueError("campaign continued after a terminal full evaluation")
        if (
            self.full_evaluations
            and self.full_evaluations[-1].report.passed
            and self.full_evaluations[-1].model_snapshot
            != self.qualification_reports[-1].model_snapshot
        ):
            raise ValueError("campaign continued after a passing full evaluation")

        full_by_model = {item.model_snapshot: item.report for item in self.full_evaluations}
        for qualification in self.qualification_reports[:-1]:
            if qualification.evaluation_outcome == "quality_failed":
                continue
            full = full_by_model.get(qualification.model_snapshot)
            if (
                not qualification.qualified
                or full is None
                or full.evaluation_outcome != "quality_failed"
            ):
                raise ValueError("campaign continued after a terminal qualification event")

        usage_reports = tuple(item.usage for item in self.qualification_reports) + tuple(
            item.report.usage for item in self.full_evaluations
        )
        if (
            self.usage.provider_attempts != sum(item.provider_attempts for item in usage_reports)
            or self.usage.input_tokens != sum(item.input_tokens for item in usage_reports)
            or self.usage.output_reasoning_tokens
            != sum(item.output_reasoning_tokens for item in usage_reports)
            or self.usage.duration_ms != sum(item.duration_ms for item in usage_reports)
            or self.usage.calculated_cost_eur
            != sum((item.calculated_cost_eur for item in usage_reports), start=Decimal("0"))
        ):
            raise ValueError("qualification campaign usage is not cumulative")
        aggregate_within_budget = (
            self.usage.provider_attempts <= self.limits.max_provider_attempts
            and self.usage.input_tokens <= self.limits.max_input_tokens
            and self.usage.output_reasoning_tokens <= self.limits.max_output_reasoning_tokens
            and self.usage.calculated_cost_eur <= self.limits.max_cost_eur
        )
        if self.usage.within_budget is not aggregate_within_budget or (
            not aggregate_within_budget and not self.usage.budget_exhausted
        ):
            raise ValueError("qualification campaign budget flags are inconsistent")
        if self.usage.budget_exhausted is not (
            self.evaluation_outcome == "evaluation_budget_exhausted"
        ):
            raise ValueError("qualification campaign budget flag must match its terminal outcome")
        if any(item.budget_exhausted for item in usage_reports) and not (
            self.usage.budget_exhausted
        ):
            raise ValueError("qualification campaign lost child budget exhaustion")

        expected_worst = len(self.candidate_order) * (
            self.qualification_base_provider_attempts_per_model
            + self.full_corpus_base_provider_attempts
        )
        if (
            self.worst_case_base_provider_attempts != expected_worst
            or expected_worst > self.limits.max_provider_attempts
            or self.full_corpus_runs_maximum != len(self.candidate_order)
        ):
            raise ValueError("qualification campaign plan exceeds the attempt cap")

        last_qualification = self.qualification_reports[-1]
        last_full = (
            self.full_evaluations[-1].report
            if self.full_evaluations
            and self.full_evaluations[-1].model_snapshot == last_qualification.model_snapshot
            else None
        )
        remaining = len(self.qualification_reports) < len(self.candidate_order)
        expected_outcome: QualificationCampaignOutcome
        if last_full is not None and last_full.passed:
            expected_outcome = "selected"
        elif last_full is not None and last_full.evaluation_outcome != "quality_failed":
            expected_outcome = cast(QualificationCampaignOutcome, last_full.evaluation_outcome)
        elif last_qualification.qualified and last_full is None:
            expected_outcome = "evaluation_budget_exhausted"
        elif last_qualification.evaluation_outcome not in {"quality_failed", "qualified"}:
            expected_outcome = cast(
                QualificationCampaignOutcome,
                last_qualification.evaluation_outcome,
            )
        elif remaining:
            expected_outcome = "awaiting_policy_revision"
        elif self.full_evaluations:
            expected_outcome = "quality_failed"
        else:
            expected_outcome = "qualification_failed"
        if self.evaluation_outcome != expected_outcome:
            raise ValueError("qualification campaign outcome is inconsistent")

        expected_selected = (
            last_full.model_snapshot if last_full is not None and last_full.passed else None
        )
        if self.selected_model != expected_selected:
            raise ValueError("qualification campaign selected model is inconsistent")
        if self.evaluation_outcome == "awaiting_policy_revision":
            expected_next = self.candidate_order[len(self.qualification_reports)]
            if self.next_required_model != expected_next:
                raise ValueError("campaign next policy revision is inconsistent")
        elif self.next_required_model is not None:
            raise ValueError("terminal campaign cannot request another model")
        return self

    def json_bytes(self) -> bytes:
        return (
            json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")

    def markdown(self) -> str:
        lines = [
            "# M27 cheapest-first Query Studio qualification",
            "",
            f"- Status: **{self.evaluation_outcome.upper()}**",
            f"- Plan: `{self.plan_version}`",
            (
                "- Provider-free core preflight: "
                + (
                    f"**PASS** ({self.provider_free_preflight.planned_case_count}/"
                    f"{self.provider_free_preflight.planned_case_count}; "
                    f"{self.provider_free_preflight.provider_calls_performed} provider calls)."
                    if self.provider_free_preflight is not None
                    else "not present in retained v4-v8 evidence."
                )
            ),
            (
                "- Attempt plan: "
                f"{self.qualification_base_provider_attempts_per_model} qualification "
                f"attempts/model + {self.full_corpus_base_provider_attempts} full-corpus "
                f"attempts/model; worst-case base {self.worst_case_base_provider_attempts}/"
                f"{self.limits.max_provider_attempts}."
            ),
            (
                "- Cumulative usage: "
                f"{self.usage.provider_attempts} attempts; "
                f"{self.usage.input_tokens} input tokens; "
                f"{self.usage.output_reasoning_tokens} output/reasoning tokens; "
                f"{self.usage.duration_ms} ms; EUR {self.usage.calculated_cost_eur}."
            ),
            "",
            "| Model | Policy revision | Qualification | Attempts | Cost EUR |",
            "|---|---:|---|---:|---:|",
        ]
        lines.extend(
            (
                f"| `{item.model_snapshot}` | {item.policy_version or '-'} | "
                f"{item.evaluation_outcome} | {item.usage.provider_attempts} | "
                f"{item.usage.calculated_cost_eur} |"
            )
            for item in self.qualification_reports
        )
        if self.full_evaluations:
            lines.extend(
                (
                    "",
                    "## Retained full-corpus evaluations",
                    "",
                    "| Model | Policy revision | Outcome | Attempts | Cost EUR |",
                    "|---|---:|---|---:|---:|",
                )
            )
            lines.extend(
                (
                    f"| `{item.model_snapshot}` | {item.policy_version} | "
                    f"{item.report.evaluation_outcome} | "
                    f"{item.report.usage.provider_attempts} | "
                    f"{item.report.usage.calculated_cost_eur} |"
                )
                for item in self.full_evaluations
            )
        if self.next_required_model is not None:
            lines.extend(
                (
                    "",
                    "## Required next revision",
                    "",
                    (
                        "Apply and review a new exact tenant policy revision for "
                        f"`{self.next_required_model}` before resuming this report."
                    ),
                )
            )
        lines.extend(
            (
                "",
                (
                    "> Qualification is only an eliminatory smoke. It is not model selection. "
                    "Every qualifying model receives a retained full-corpus run; the campaign "
                    "stops at the first full PASS."
                ),
                (
                    "> Synthetic corpus only. Request text, expansions, shortlists, proposals, "
                    "provider payloads, and credentials are intentionally absent."
                ),
                "",
            )
        )
        return "\n".join(lines)


class _QualificationCampaignEvidenceEnvelope(_FrozenModel):
    """HMAC-authenticated envelope; the shared control key is never persisted."""

    schema_version: Literal[1] = 1
    signature_domain: Literal[
        "schemabridge:m27:query-studio:qualification-campaign-evidence:v1"
    ] = "schemabridge:m27:query-studio:qualification-campaign-evidence:v1"
    signature_key_version: str = Field(pattern=r"^v[1-9][0-9]{0,5}$")
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    report: NanoFirstQualificationCampaignReport
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="before")
    @classmethod
    def report_digest_must_match_raw_payload(cls, value: object) -> object:
        """Bind the digest before schema defaults can rewrite historical evidence."""

        if not isinstance(value, Mapping):
            return value
        report_sha256 = value.get("report_sha256")
        raw_report = value.get("report")
        if isinstance(raw_report, NanoFirstQualificationCampaignReport):
            report_payload: object = raw_report.model_dump(mode="json")
        elif isinstance(raw_report, Mapping):
            report_payload = raw_report
        else:
            raise ValueError("qualification evidence report payload is invalid")
        if not isinstance(report_sha256, str) or report_sha256 != query_studio_fingerprint(
            report_payload
        ):
            raise ValueError("qualification evidence report digest is inconsistent")
        return value

    def json_bytes(self) -> bytes:
        return (
            json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")


@dataclass(slots=True)
class _ProviderAttemptMeter:
    attempts: int = 0
    duration_ms: int = 0


@dataclass(frozen=True, slots=True)
class _MeteredQueryStudioIntent:
    delegate: QueryStudioIntentPort
    meter: _ProviderAttemptMeter

    def interpret(
        self,
        value: QueryStudioInterpretationInput,
    ) -> QueryStudioInterpretationResult:
        started = time.monotonic_ns()
        self.meter.attempts += 1
        try:
            return self.delegate.interpret(value)
        finally:
            self.meter.duration_ms += max(
                0,
                round((time.monotonic_ns() - started) / 1_000_000),
            )


@dataclass(frozen=True, slots=True)
class _BudgetedDescriptionExpansion:
    delegate: DescriptionExpansionPort
    ledger: _UsageLedger
    meter: _ProviderAttemptMeter
    configuration: ProviderConfigurationFacts

    def expand(self, value: DescriptionExpansionInput) -> DescriptionExpansionResult:
        return _run_budgeted_stage(
            invoke=lambda: self.delegate.expand(value),
            usage=lambda result: result.usage,
            ledger=self.ledger,
            meter=self.meter,
            configuration=self.configuration,
            input_bound=0,
            output_bound=0,
            attempt_slots=0,
        )


@dataclass(frozen=True, slots=True)
class _BudgetedQueryStudioIntent:
    delegate: QueryStudioIntentPort
    ledger: _UsageLedger
    meter: _ProviderAttemptMeter
    configuration: ProviderConfigurationFacts

    def interpret(
        self,
        value: QueryStudioInterpretationInput,
    ) -> QueryStudioInterpretationResult:
        input_bound = (
            openai_interpretation_input_token_reservation_bound_for(value)
            if self.configuration.external_ai
            else 0
        )
        return _run_budgeted_stage(
            invoke=lambda: self.delegate.interpret(value),
            usage=lambda result: result.usage,
            ledger=self.ledger,
            meter=self.meter,
            configuration=self.configuration,
            input_bound=input_bound,
            output_bound=_MAX_INTERPRETATION_OUTPUT_TOKENS_PER_ATTEMPT,
            attempt_slots=2 if self.configuration.external_ai else 1,
        )


@dataclass(slots=True)
class _CampaignUsageLedger:
    limits: EvaluationBudgetLimits
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    calculated_cost_eur: Decimal = Decimal("0")
    budget_exhausted: bool = False

    def can_complete_candidate(self, planned_attempts: int) -> bool:
        if self.attempts + planned_attempts > self.limits.max_provider_attempts:
            self.budget_exhausted = True
            return False
        return True

    def preflight(
        self,
        pricing: ModelTokenPricing,
        *,
        attempt_slots: int,
        input_token_slots: int,
        output_token_slots: int,
    ) -> bool:
        prospective_cost = self.calculated_cost_eur + calculate_cost_eur(
            pricing,
            input_tokens=input_token_slots,
            output_reasoning_tokens=output_token_slots,
        )
        if (
            self.attempts + attempt_slots > self.limits.max_provider_attempts
            or self.input_tokens + input_token_slots > self.limits.max_input_tokens
            or self.output_tokens + output_token_slots > self.limits.max_output_reasoning_tokens
            or prospective_cost > self.limits.max_cost_eur
        ):
            self.budget_exhausted = True
            return False
        return True

    def record(
        self,
        pricing: ModelTokenPricing,
        *,
        attempts: int,
        input_tokens: int,
        output_tokens: int,
        duration_ms: int,
    ) -> None:
        self.attempts += attempts
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.duration_ms += duration_ms
        self.calculated_cost_eur += calculate_cost_eur(
            pricing,
            input_tokens=input_tokens,
            output_reasoning_tokens=output_tokens,
        )
        if not self.within_budget():
            self.budget_exhausted = True

    def within_budget(self) -> bool:
        return (
            self.attempts <= self.limits.max_provider_attempts
            and self.input_tokens <= self.limits.max_input_tokens
            and self.output_tokens <= self.limits.max_output_reasoning_tokens
            and self.calculated_cost_eur <= self.limits.max_cost_eur
        )

    def usage(self) -> ProviderEvaluationUsage:
        return ProviderEvaluationUsage(
            provider_attempts=self.attempts,
            input_tokens=self.input_tokens,
            output_reasoning_tokens=self.output_tokens,
            duration_ms=self.duration_ms,
            calculated_cost_eur=self.calculated_cost_eur,
            budget_exhausted=self.budget_exhausted,
            within_budget=self.within_budget(),
        )


@dataclass(slots=True)
class _UsageLedger:
    limits: EvaluationBudgetLimits
    pricing: ModelTokenPricing
    campaign: _CampaignUsageLedger
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    calculated_cost_eur: Decimal = Decimal("0")
    budget_exhausted: bool = False
    cascade_detected: bool = False
    stage_accounting: bool = False

    def preflight(
        self,
        *,
        attempt_slots: int,
        input_token_slots: int,
        output_token_slots: int,
    ) -> bool:
        if not self.campaign.preflight(
            self.pricing,
            attempt_slots=attempt_slots,
            input_token_slots=input_token_slots,
            output_token_slots=output_token_slots,
        ):
            self.budget_exhausted = True
            return False
        return True

    def record(
        self,
        usages: Sequence[ProviderUsageFacts],
        *,
        observed_attempts: int,
        observed_duration_ms: int,
        failed_attempt_input_charge: int,
        failed_attempt_output_charge: int,
        configuration: ProviderConfigurationFacts,
    ) -> None:
        if self.stage_accounting:
            return
        self.record_stage(
            usages,
            observed_attempts=observed_attempts,
            observed_duration_ms=observed_duration_ms,
            failed_attempt_input_charge=failed_attempt_input_charge,
            failed_attempt_output_charge=failed_attempt_output_charge,
            configuration=configuration,
        )

    def record_stage(
        self,
        usages: Sequence[ProviderUsageFacts],
        *,
        observed_attempts: int,
        observed_duration_ms: int,
        failed_attempt_input_charge: int,
        failed_attempt_output_charge: int,
        configuration: ProviderConfigurationFacts,
    ) -> None:
        chargeable_usages = tuple(
            usage
            for usage in usages
            if usage.input_tokens != 0 or usage.output_tokens != 0 or usage.duration_ms != 0
        )
        if observed_attempts >= len(usages):
            accounted_usages = tuple(usages)
        elif observed_attempts >= len(chargeable_usages):
            # The local atomic preflight preserves the typed result contract
            # with a zero-usage fact while bypassing the provider delegate.
            accounted_usages = chargeable_usages
        else:
            self.cascade_detected = True
            observed_attempts = len(chargeable_usages)
            accounted_usages = chargeable_usages
        input_tokens = 0
        output_tokens = 0
        for usage in accounted_usages:
            if (
                usage.model_snapshot != configuration.model_snapshot
                or usage.configuration_fingerprint != configuration.fingerprint
            ):
                self.cascade_detected = True
            input_tokens += usage.input_tokens
            output_tokens += usage.output_tokens
        failed_attempts = observed_attempts - len(accounted_usages)
        input_tokens += failed_attempts * failed_attempt_input_charge
        output_tokens += failed_attempts * failed_attempt_output_charge
        self.attempts += observed_attempts
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.duration_ms += observed_duration_ms
        self.calculated_cost_eur += calculate_cost_eur(
            self.pricing,
            input_tokens=input_tokens,
            output_reasoning_tokens=output_tokens,
        )
        self.campaign.record(
            self.pricing,
            attempts=observed_attempts,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=observed_duration_ms,
        )
        if not self.within_budget():
            self.budget_exhausted = True

    def cost(self) -> Decimal:
        return self.calculated_cost_eur

    def within_budget(self) -> bool:
        return (
            self.attempts <= self.limits.max_provider_attempts
            and self.input_tokens <= self.limits.max_input_tokens
            and self.output_tokens <= self.limits.max_output_reasoning_tokens
            and self.cost() <= self.limits.max_cost_eur
            and self.campaign.within_budget()
        )


def _run_budgeted_stage(
    *,
    invoke: Callable[[], _StageResultT],
    usage: Callable[[_StageResultT], ProviderUsageFacts | None],
    ledger: _UsageLedger,
    meter: _ProviderAttemptMeter,
    configuration: ProviderConfigurationFacts,
    input_bound: int,
    output_bound: int,
    attempt_slots: int,
) -> _StageResultT:
    """Preflight and account one stage before the durable/provider boundary."""

    reserved_input = attempt_slots * input_bound if configuration.external_ai else 0
    reserved_output = attempt_slots * output_bound if configuration.external_ai else 0
    if not ledger.preflight(
        attempt_slots=attempt_slots,
        input_token_slots=reserved_input,
        output_token_slots=reserved_output,
    ):
        raise QueryStudioPortError(
            QueryStudioPortErrorCode.PROVIDER_QUOTA_EXHAUSTED,
            "the bounded evaluation budget cannot admit this provider stage",
        )

    meter_before = _meter_snapshot(meter)
    try:
        result = invoke()
    except Exception:
        ledger.record_stage(
            (),
            observed_attempts=meter.attempts - meter_before[0],
            observed_duration_ms=meter.duration_ms - meter_before[1],
            failed_attempt_input_charge=input_bound,
            failed_attempt_output_charge=output_bound,
            configuration=configuration,
        )
        raise

    provider_usage = usage(result)
    observed_attempts = meter.attempts - meter_before[0]
    observed_duration_ms = meter.duration_ms - meter_before[1]
    if provider_usage is None:
        ledger.record_stage(
            (),
            observed_attempts=observed_attempts,
            observed_duration_ms=observed_duration_ms,
            failed_attempt_input_charge=input_bound,
            failed_attempt_output_charge=output_bound,
            configuration=configuration,
        )
        if observed_attempts != 0:
            raise ValueError("local expansion usage followed a provider delegate attempt")
        return result
    ledger.record_stage(
        (provider_usage,),
        observed_attempts=observed_attempts,
        observed_duration_ms=observed_duration_ms,
        failed_attempt_input_charge=input_bound,
        failed_attempt_output_charge=output_bound,
        configuration=configuration,
    )
    if configuration.external_ai and (
        provider_usage.input_tokens > input_bound or provider_usage.output_tokens > output_bound
    ):
        raise ValueError("provider usage exceeded the conservative evaluation stage bound")
    return result


def _with_stage_budget(
    runtime: QueryStudioRuntimeServices,
    *,
    ledger: _UsageLedger,
    meter: _ProviderAttemptMeter,
) -> QueryStudioRuntimeServices:
    prepare = runtime.prepare_natural
    expansion = runtime.expansion
    if prepare is None or expansion is None:
        raise ValueError("evaluation runtime lacks the complete natural-language lane")
    ledger.stage_accounting = True
    budgeted_expansion = _BudgetedDescriptionExpansion(
        delegate=expansion,
        ledger=ledger,
        meter=meter,
        configuration=runtime.configuration,
    )
    budgeted_interpreter = _BudgetedQueryStudioIntent(
        delegate=prepare.interpreter,
        ledger=ledger,
        meter=meter,
        configuration=runtime.configuration,
    )
    return replace(
        runtime,
        expansion=budgeted_expansion,
        prepare_natural=replace(
            prepare,
            expansion=budgeted_expansion,
            interpreter=budgeted_interpreter,
        ),
    )


@dataclass(frozen=True, slots=True)
class _BaselineCore:
    request_fingerprint: str
    validated_request: ValidatedAnalyticalRequest


def _validated_request_fingerprint(value: ValidatedAnalyticalRequest) -> str:
    return query_studio_fingerprint(value.model_dump(mode="json"))


def _core_request_shape_counts(value: ValidatedAnalyticalRequest) -> CoreRequestShapeCounts:
    request = value.request
    return CoreRequestShapeCounts(
        dimensions=len(request.dimensions),
        metrics=len(request.metrics),
        filters=len(request.filters),
        order_by=len(request.order_by),
        required_models=len(value.required_models),
        join_contracts=len(value.join_contract_ids),
    )


def _fingerprint_mismatch_diagnostic(
    expected: ValidatedAnalyticalRequest,
    observed: ValidatedAnalyticalRequest,
) -> FingerprintMismatchDiagnostic:
    """Reduce an in-memory request diff to hashes, counts, and closed flags."""

    expected_request = expected.request
    observed_request = observed.request
    expected_parts: Mapping[FingerprintMismatchComponent, object] = {
        "context_binding": (
            expected.context_source,
            expected.context_version,
            expected.context_fingerprint,
        ),
        "primary_entity": expected_request.primary_entity.root,
        "dimension_fields": tuple(item.field.root for item in expected_request.dimensions),
        "dimension_grains": tuple(
            item.grain.value if item.grain is not None else None
            for item in expected_request.dimensions
        ),
        "metric_fields": tuple(item.field.root for item in expected_request.metrics),
        "metric_operations": tuple(item.operation.value for item in expected_request.metrics),
        "metric_aliases": tuple(item.alias for item in expected_request.metrics),
        "filter_fields": tuple(item.field.root for item in expected_request.filters),
        "filter_operators": tuple(item.operator.value for item in expected_request.filters),
        "filter_values": tuple(item.value for item in expected_request.filters),
        "order_fields": tuple(item.field.root for item in expected_request.order_by),
        "order_directions": tuple(item.direction.value for item in expected_request.order_by),
        "limit": expected_request.limit,
        "required_models": tuple(item.root for item in expected.required_models),
        "join_contracts": expected.join_contract_ids,
    }
    observed_parts: Mapping[FingerprintMismatchComponent, object] = {
        "context_binding": (
            observed.context_source,
            observed.context_version,
            observed.context_fingerprint,
        ),
        "primary_entity": observed_request.primary_entity.root,
        "dimension_fields": tuple(item.field.root for item in observed_request.dimensions),
        "dimension_grains": tuple(
            item.grain.value if item.grain is not None else None
            for item in observed_request.dimensions
        ),
        "metric_fields": tuple(item.field.root for item in observed_request.metrics),
        "metric_operations": tuple(item.operation.value for item in observed_request.metrics),
        "metric_aliases": tuple(item.alias for item in observed_request.metrics),
        "filter_fields": tuple(item.field.root for item in observed_request.filters),
        "filter_operators": tuple(item.operator.value for item in observed_request.filters),
        "filter_values": tuple(item.value for item in observed_request.filters),
        "order_fields": tuple(item.field.root for item in observed_request.order_by),
        "order_directions": tuple(item.direction.value for item in observed_request.order_by),
        "limit": observed_request.limit,
        "required_models": tuple(item.root for item in observed.required_models),
        "join_contracts": observed.join_contract_ids,
    }
    components = tuple(
        component
        for component in _FINGERPRINT_COMPONENT_ORDER
        if expected_parts[component] != observed_parts[component]
    )
    if not components:
        raise ValueError("different request fingerprints require a closed structural difference")

    expected_counts = _core_request_shape_counts(expected)
    observed_counts = _core_request_shape_counts(observed)
    flags: set[FingerprintMismatchFlag] = set()
    if "context_binding" in components:
        flags.add("context_binding_changed")
    if "primary_entity" in components:
        flags.add("primary_entity_changed")
    if _SEMANTIC_SELECTION_COMPONENTS.intersection(components):
        flags.add("semantic_selection_changed")
    if "metric_fields" in components:
        expected_entities = tuple(
            item.partition(".")[0]
            for item in cast(tuple[str, ...], expected_parts["metric_fields"])
        )
        observed_entities = tuple(
            item.partition(".")[0]
            for item in cast(tuple[str, ...], observed_parts["metric_fields"])
        )
        if expected_entities != observed_entities:
            flags.add("metric_entity_changed")
    if "filter_values" in components:
        flags.add("filter_value_changed")
    if {"order_fields", "order_directions"}.intersection(components):
        flags.add("ordering_changed")
    if "limit" in components:
        flags.add("limit_changed")
    if expected_counts != observed_counts:
        flags.add("shape_counts_changed")

    expected_models = cast(tuple[str, ...], expected_parts["required_models"])
    observed_models = cast(tuple[str, ...], observed_parts["required_models"])
    if set(observed_models) - set(expected_models):
        flags.add("required_model_added")
    if set(expected_models) - set(observed_models):
        flags.add("required_model_removed")
    if "required_models" in components and set(expected_models) == set(observed_models):
        flags.add("required_model_order_changed")

    expected_joins = cast(tuple[str, ...], expected_parts["join_contracts"])
    observed_joins = cast(tuple[str, ...], observed_parts["join_contracts"])
    if set(observed_joins) - set(expected_joins):
        flags.add("join_added")
    if set(expected_joins) - set(observed_joins):
        flags.add("join_removed")
    if "join_contracts" in components and set(expected_joins) == set(observed_joins):
        flags.add("join_order_changed")

    return FingerprintMismatchDiagnostic(
        expected_request_sha256=_validated_request_fingerprint(expected),
        observed_request_sha256=_validated_request_fingerprint(observed),
        differing_components=components,
        expected_counts=expected_counts,
        observed_counts=observed_counts,
        flags=tuple(item for item in _FINGERPRINT_FLAG_ORDER if item in flags),
    )


def _provider_free_usage(
    configuration: ProviderConfigurationFacts,
    stage: ProviderStage,
) -> ProviderUsageFacts:
    return ProviderUsageFacts(
        stage=stage,
        model_snapshot=configuration.model_snapshot,
        configuration_fingerprint=configuration.fingerprint,
        input_tokens=0,
        output_tokens=0,
        duration_ms=0,
        outcome=ProviderOutcomeCode.SUCCEEDED,
    )


@dataclass(frozen=True, slots=True)
class _ProviderFreeServerOwnedExpansion:
    """Replay the exact server-owned slot contract without invoking a provider."""

    configuration: ProviderConfigurationFacts

    def expand(self, value: DescriptionExpansionInput) -> DescriptionExpansionResult:
        from schemabridge.adapters.language import openai_query_studio as server_pipeline

        return DescriptionExpansionResult(
            expansion=server_pipeline.local_analytical_description_expansion(value),
            usage=None,
        )


@dataclass(frozen=True, slots=True)
class _ProviderFreeServerOwnedIntent:
    """Select each closed slot's sole executable option without provider egress."""

    configuration: ProviderConfigurationFacts

    def interpret(
        self,
        value: QueryStudioInterpretationInput,
    ) -> QueryStudioInterpretationResult:
        from schemabridge.adapters.language import openai_query_studio as server_pipeline

        provider_shape = server_pipeline._OpenAIQueryStudioSelections.model_validate(
            {
                "selections": tuple(
                    {
                        "slot_id": probe.purpose_id,
                        "option_index": 1,
                    }
                    for probe in value.expansion.probes
                ),
                "ambiguity_kinds": (),
            }
        )
        unnormalized_proposal = server_pipeline._reconstruct_proposal(
            provider_shape,
            value,
        )
        server_pipeline._validate_proposal_is_closed(unnormalized_proposal, value)
        proposal = canonicalize_query_studio_proposal(
            unnormalized_proposal,
            value,
        )
        server_pipeline._validate_proposal_is_closed(proposal, value)
        return QueryStudioInterpretationResult(
            proposal=proposal,
            usage=_provider_free_usage(
                self.configuration,
                ProviderStage.INTERPRETATION,
            ),
        )


def _provider_free_server_owned_runtime(
    runtime: QueryStudioRuntimeServices,
) -> QueryStudioRuntimeServices:
    """Retain candidate registry/search while replacing both provider delegates."""

    if runtime.prepare_natural is None or runtime.confirm_natural is None:
        raise ValueError("provider-free core preflight requires the natural-language pipeline")
    expansion = _ProviderFreeServerOwnedExpansion(runtime.configuration)
    interpreter = _ProviderFreeServerOwnedIntent(runtime.configuration)
    return replace(
        runtime,
        expansion=expansion,
        prepare_natural=replace(
            runtime.prepare_natural,
            expansion=expansion,
            interpreter=interpreter,
            configuration=runtime.configuration,
        ),
        confirm_natural=replace(
            runtime.confirm_natural,
            configuration=runtime.configuration,
        ),
    )


def _provider_free_pipeline_fingerprint(
    runtime: QueryStudioRuntimeServices,
    *,
    corpus_sha256: str,
) -> str:
    configuration = runtime.configuration
    return query_studio_fingerprint(
        {
            "kind": "m27-provider-free-server-owned-core-preflight-v2",
            "corpus_sha256": corpus_sha256,
            "prompt_version": configuration.prompt_version,
            "schema_version": configuration.schema_version,
            "matcher_version": configuration.matcher_version,
            "orchestration_policy_version": configuration.orchestration_policy_version,
            "expansion_contract_version": OPENAI_EXPANSION_CONTRACT_VERSION,
            "semantic_focus_contract_version": OPENAI_SEMANTIC_FOCUS_CONTRACT_VERSION,
            "slot_selection_contract_version": OPENAI_SLOT_SELECTION_CONTRACT_VERSION,
            "proposal_normalizer_version": QUERY_STUDIO_PROPOSAL_NORMALIZER_VERSION,
            "public_metadata_semantic_scope_fingerprint": (
                configuration.public_metadata_semantic_scope_fingerprint
            ),
            "public_metadata_registry_fingerprint": (
                configuration.public_metadata_registry_fingerprint
            ),
        }
    )


def _run_provider_free_core_preflight(
    runtime: QueryStudioRuntimeServices,
    *,
    corpus: _LiveCorpus,
    corpus_sha256: str,
    baseline: Mapping[str, _BaselineCore],
) -> ProviderFreeCorePreflightReport:
    if len(corpus.core_queries) != 5:
        raise ValueError("provider-free core preflight requires the exact five-case corpus")
    provider_free_runtime = _provider_free_server_owned_runtime(runtime)
    outcomes: list[ProviderFreePreflightCaseOutcome] = []
    for core in corpus.core_queries:
        expected = baseline[core.id]
        for repetition in range(1, 4):
            text, language = _core_repetition_input(core, repetition)
            confirmed: ConfirmedQueryStudioRequest | None = None
            try:
                preview = _prepare(provider_free_runtime, text, language)
                confirmed = _confirm(provider_free_runtime, text, language, preview)
            except (RuntimeError, TypeError, ValueError):
                confirmed = None
            if confirmed is None:
                outcomes.append(
                    ProviderFreePreflightCaseOutcome(
                        case_id=core.id,
                        repetition=repetition,
                        expected_request_sha256=expected.request_fingerprint,
                        outcome="pipeline_unconfirmed",
                        passed=False,
                    )
                )
                continue
            observed = _validated_request_fingerprint(confirmed.validated_request)
            if observed == expected.request_fingerprint:
                outcomes.append(
                    ProviderFreePreflightCaseOutcome(
                        case_id=core.id,
                        repetition=repetition,
                        expected_request_sha256=expected.request_fingerprint,
                        observed_request_sha256=observed,
                        outcome="fingerprint_match",
                        passed=True,
                    )
                )
                continue
            outcomes.append(
                ProviderFreePreflightCaseOutcome(
                    case_id=core.id,
                    repetition=repetition,
                    expected_request_sha256=expected.request_fingerprint,
                    observed_request_sha256=observed,
                    outcome="fingerprint_mismatch",
                    diagnostic=_fingerprint_mismatch_diagnostic(
                        expected.validated_request,
                        confirmed.validated_request,
                    ),
                    passed=False,
                )
            )
    report = ProviderFreeCorePreflightReport(
        pipeline_fingerprint=_provider_free_pipeline_fingerprint(
            runtime,
            corpus_sha256=corpus_sha256,
        ),
        case_outcomes=tuple(outcomes),
        failed_case_ids=tuple(dict.fromkeys(item.case_id for item in outcomes if not item.passed)),
        passed=all(item.passed for item in outcomes),
    )
    expected_matrix = tuple(
        (core.id, repetition) for core in corpus.core_queries for repetition in range(1, 4)
    )
    observed_matrix = tuple((item.case_id, item.repetition) for item in report.case_outcomes)
    if observed_matrix != expected_matrix:
        raise ValueError("provider-free core preflight does not match the frozen corpus")
    return report


def run_provider_free_core_preflight(
    repository_root: Path,
    *,
    fake_baseline: QueryStudioRuntimeServices,
    pipeline_runtime: QueryStudioRuntimeServices,
) -> ProviderFreeCorePreflightReport:
    """Run the current 15/15 gate with the server-owned pipeline and zero provider calls."""

    corpus, corpus_sha256 = _load_synthetic_corpus(repository_root)
    baseline = _build_fake_baseline(fake_baseline, corpus.core_queries)
    return _run_provider_free_core_preflight(
        pipeline_runtime,
        corpus=corpus,
        corpus_sha256=corpus_sha256,
        baseline=baseline,
    )


@dataclass(slots=True)
class _EvaluationState:
    positive_ranks: list[int | None]
    negative_correct: int = 0
    sensitive_negative_correct: int = 0
    ambiguity_correct: int = 0
    core_correct: int = 0
    outcomes: list[LiveEvaluationCaseOutcome] | None = None
    top_1_errors: list[str] | None = None
    false_negatives: list[str] | None = None
    false_positives: list[str] | None = None
    ambiguity_misses: list[str] | None = None
    core_mismatches: list[str] | None = None
    adversarial_misses: list[str] | None = None
    unknown_candidate_ids: int = 0
    cross_tenant_candidates: int = 0
    sql_tool_or_approval_outputs: int = 0
    operational_outcome: (
        Literal[
            "provider_unavailable",
            "rate_limited",
            "quota_exhausted",
            "runtime_invalid",
            "runtime_model_cascade",
        ]
        | None
    ) = None
    quality_failure_outcome: ProviderQualityFailureOutcome | None = None
    complete: bool = True

    def __post_init__(self) -> None:
        self.outcomes = []
        self.top_1_errors = []
        self.false_negatives = []
        self.false_positives = []
        self.ambiguity_misses = []
        self.core_mismatches = []
        self.adversarial_misses = []


def calculate_cost_eur(
    pricing: ModelTokenPricing,
    *,
    input_tokens: int,
    output_reasoning_tokens: int,
) -> Decimal:
    """Charge uncached standard tokens, a regional uplift, and a 1:1 USD/EUR floor."""

    if input_tokens < 0 or output_reasoning_tokens < 0:
        raise ValueError("token counts cannot be negative")
    millions = Decimal(1_000_000)
    raw = (
        Decimal(input_tokens) * pricing.input_eur_per_million
        + Decimal(output_reasoning_tokens) * pricing.output_eur_per_million
    ) / millions
    return (raw * pricing.regional_uplift * pricing.usd_to_eur_floor).quantize(
        Decimal("0.000000001"),
        rounding=ROUND_HALF_UP,
    )


def evaluate_nano_first_query_studio(
    repository_root: Path,
    *,
    fake_baseline: QueryStudioRuntimeServices,
    candidates: Sequence[EvaluationCandidate],
    limits: EvaluationBudgetLimits | None = None,
    repetitions: int = 3,
) -> NanoFirstEvaluationCampaignReport:
    """Evaluate candidates in order and stop immediately after the first full pass."""

    if not candidates or len(candidates) > 3:
        raise ValueError("nano-first evaluation requires between one and three candidates")
    if repetitions < 3 or repetitions > 10:
        raise ValueError("live evaluation requires between three and ten repetitions")
    resolved_limits = limits or EvaluationBudgetLimits()
    candidate_order = tuple(candidate.model_snapshot for candidate in candidates)
    if len(candidate_order) != len(set(candidate_order)):
        raise ValueError("nano-first candidate models must be unique")
    corpus, corpus_sha256 = _load_synthetic_corpus(repository_root)
    baseline = _build_fake_baseline(fake_baseline, corpus.core_queries)
    planned_base_attempts = _planned_base_provider_attempts(corpus, repetitions)
    if planned_base_attempts > resolved_limits.max_provider_attempts:
        raise ValueError("evaluation plan exceeds the fail-closed provider-attempt budget")

    reports: list[QueryStudioCandidateEvaluationReport] = []
    campaign_ledger = _CampaignUsageLedger(limits=resolved_limits)
    campaign_outcome: Literal[
        "selected",
        "quality_failed",
        "policy_unavailable",
        "policy_mismatch",
        "provider_unavailable",
        "rate_limited",
        "quota_exhausted",
        "evaluation_budget_exhausted",
        "runtime_invalid",
        "runtime_model_cascade",
    ] = "quality_failed"
    for candidate in candidates:
        if not campaign_ledger.can_complete_candidate(planned_base_attempts):
            campaign_outcome = "evaluation_budget_exhausted"
            break
        runtime = candidate.runtime_factory()
        if runtime.configuration.model_snapshot != candidate.model_snapshot:
            raise ValueError("candidate runtime model does not match the requested model")
        authorization_outcome = _authorization_outcome(candidate.authorization, runtime)
        if authorization_outcome is None:
            runtime, attempt_meter = _instrument_runtime(runtime)
            report = _evaluate_candidate(
                runtime,
                corpus=corpus,
                corpus_sha256=corpus_sha256,
                fake_baseline=fake_baseline,
                baseline=baseline,
                pricing=candidate.pricing,
                openai_sdk_version=candidate.openai_sdk_version,
                limits=resolved_limits,
                repetitions=repetitions,
                attempt_meter=attempt_meter,
                campaign_ledger=campaign_ledger,
                planned_base_attempts=planned_base_attempts,
            )
        else:
            report = _unevaluated_candidate_report(
                runtime,
                corpus=corpus,
                corpus_sha256=corpus_sha256,
                fake_baseline=fake_baseline,
                pricing=candidate.pricing,
                openai_sdk_version=candidate.openai_sdk_version,
                limits=resolved_limits,
                repetitions=repetitions,
                evaluation_outcome=authorization_outcome,
                planned_base_attempts=planned_base_attempts,
            )
        reports.append(report)
        if report.evaluation_outcome == "passed":
            campaign_outcome = "selected"
            break
        if report.evaluation_outcome != "quality_failed":
            campaign_outcome = report.evaluation_outcome
            break
        campaign_outcome = "quality_failed"
    passing = next((item.model_snapshot for item in reports if item.passed), None)
    return NanoFirstEvaluationCampaignReport(
        candidate_order=candidate_order,
        attempted_models=tuple(item.model_snapshot for item in reports),
        selected_model=passing,
        stopped_after_first_passing_model=passing is not None,
        evaluation_outcome=campaign_outcome,
        usage=campaign_ledger.usage(),
        candidates=tuple(reports),
    )


def plan_nano_first_query_studio_qualification(
    repository_root: Path,
    *,
    candidate_order: Sequence[str] = NANO_FIRST_MODEL_ORDER,
    pricing_by_model: Mapping[str, ModelTokenPricing] | None = None,
    limits: EvaluationBudgetLimits | None = None,
    repetitions: int = 3,
) -> QueryStudioQualificationPlan:
    """Build the exact provider-free smoke/full attempt plan."""

    if not candidate_order or len(candidate_order) > 3:
        raise ValueError("qualification plan requires between one and three candidates")
    resolved_order = tuple(candidate_order)
    if len(resolved_order) != len(set(resolved_order)):
        raise ValueError("qualification candidate models must be unique")
    resolved_pricing = OFFICIAL_STANDARD_PRICING if pricing_by_model is None else pricing_by_model
    if any(model not in resolved_pricing for model in resolved_order):
        raise ValueError("qualification candidate lacks reviewed pricing")
    if any(resolved_pricing[model].model_snapshot != model for model in resolved_order):
        raise ValueError("qualification pricing does not match its candidate")
    if repetitions < 3 or repetitions > 10:
        raise ValueError("full live evaluation requires between three and ten repetitions")
    resolved_limits = limits or EvaluationBudgetLimits()
    corpus, _corpus_sha256 = _load_synthetic_corpus(repository_root)
    full_attempts = _planned_base_provider_attempts(corpus, repetitions)
    qualification_attempts = _planned_qualification_provider_attempts(corpus)
    worst_case_attempts = len(resolved_order) * (qualification_attempts + full_attempts)
    stage_input_reservation = 2 * openai_interpretation_input_token_reservation_bound()
    stage_output_reservation = 2 * _MAX_INTERPRETATION_OUTPUT_TOKENS_PER_ATTEMPT
    maximum_stage_cost = max(
        calculate_cost_eur(
            resolved_pricing[model],
            input_tokens=stage_input_reservation,
            output_reasoning_tokens=stage_output_reservation,
        )
        for model in resolved_order
    )
    return QueryStudioQualificationPlan(
        candidate_order=resolved_order,
        qualification_base_provider_attempts_per_model=qualification_attempts,
        full_corpus_base_provider_attempts=full_attempts,
        worst_case_base_provider_attempts=worst_case_attempts,
        provider_attempt_headroom=(resolved_limits.max_provider_attempts - worst_case_attempts),
        stage_input_reservation_maximum=stage_input_reservation,
        stage_output_reservation_maximum=stage_output_reservation,
        input_headroom_after_maximum_stage=(
            resolved_limits.max_input_tokens - stage_input_reservation
        ),
        output_headroom_after_maximum_stage=(
            resolved_limits.max_output_reasoning_tokens - stage_output_reservation
        ),
        maximum_stage_cost_eur=maximum_stage_cost,
        cost_headroom_after_maximum_stage_eur=(resolved_limits.max_cost_eur - maximum_stage_cost),
        limits=resolved_limits,
        full_corpus_runs_maximum=len(resolved_order),
    )


def evaluate_qualified_nano_first_query_studio(
    repository_root: Path,
    *,
    fake_baseline: QueryStudioRuntimeServices,
    candidates: Sequence[EvaluationCandidate],
    candidate_order: Sequence[str] = NANO_FIRST_MODEL_ORDER,
    pricing_by_model: Mapping[str, ModelTokenPricing] | None = None,
    limits: EvaluationBudgetLimits | None = None,
    repetitions: int = 3,
    prior_report: NanoFirstQualificationCampaignReport | None = None,
) -> NanoFirstQualificationCampaignReport:
    """Run cheap screens and a retained full corpus for every qualifier until PASS.

    A caller may supply only the currently policy-authorized candidate and later
    resume the retained report after applying a strictly newer exact policy
    revision for the next model. No runtime model override or provider cascade is
    performed here.
    """

    resolved_limits = limits or EvaluationBudgetLimits()
    campaign_pricing = dict(OFFICIAL_STANDARD_PRICING)
    if pricing_by_model is not None:
        campaign_pricing.update(pricing_by_model)
    if prior_report is not None:
        campaign_pricing.update(
            {item.model_snapshot: item.pricing for item in prior_report.qualification_reports}
        )
    campaign_pricing.update(
        {candidate.model_snapshot: candidate.pricing for candidate in candidates}
    )
    plan = plan_nano_first_query_studio_qualification(
        repository_root,
        candidate_order=candidate_order,
        pricing_by_model=campaign_pricing,
        limits=resolved_limits,
        repetitions=repetitions,
    )
    if not candidates or len(candidates) > len(plan.candidate_order):
        raise ValueError("qualification requires at least one remaining candidate")
    corpus, corpus_sha256 = _load_synthetic_corpus(repository_root)
    baseline = _build_fake_baseline(fake_baseline, corpus.core_queries)
    reports = [] if prior_report is None else list(prior_report.qualification_reports)
    full_evaluations = [] if prior_report is None else list(prior_report.full_evaluations)
    provider_free_preflight = None if prior_report is None else prior_report.provider_free_preflight

    if prior_report is None:
        campaign_ledger = _CampaignUsageLedger(limits=resolved_limits)
    else:
        _validate_qualification_resume(
            prior_report,
            plan=plan,
            corpus=corpus,
            corpus_sha256=corpus_sha256,
            fake_baseline=fake_baseline,
            limits=resolved_limits,
        )
        campaign_ledger = _CampaignUsageLedger(
            limits=resolved_limits,
            attempts=prior_report.usage.provider_attempts,
            input_tokens=prior_report.usage.input_tokens,
            output_tokens=prior_report.usage.output_reasoning_tokens,
            duration_ms=prior_report.usage.duration_ms,
            calculated_cost_eur=prior_report.usage.calculated_cost_eur,
            budget_exhausted=prior_report.usage.budget_exhausted,
        )

    expected_models = plan.candidate_order[len(reports) : len(reports) + len(candidates)]
    supplied_models = tuple(candidate.model_snapshot for candidate in candidates)
    if supplied_models != expected_models:
        raise ValueError("qualification candidate does not match the next cheapest model")
    last_policy_version = max(
        (item.policy_version for item in reports if item.policy_version is not None),
        default=0,
    )

    for candidate in candidates:
        authorization = candidate.authorization
        if (
            authorization.policy_version is not None
            and authorization.policy_version <= last_policy_version
        ):
            raise ValueError("qualification requires a strictly newer exact policy revision")
        runtime = candidate.runtime_factory()
        if runtime.configuration.model_snapshot != candidate.model_snapshot:
            raise ValueError("candidate runtime model does not match the requested model")
        current_preflight = _run_provider_free_core_preflight(
            runtime,
            corpus=corpus,
            corpus_sha256=corpus_sha256,
            baseline=baseline,
        )
        if not current_preflight.passed:
            raise ProviderFreePreflightError(current_preflight)
        if provider_free_preflight is not None and current_preflight != provider_free_preflight:
            raise ValueError("provider-free preflight changed within the retained campaign")
        provider_free_preflight = current_preflight
        authorization_outcome = _authorization_outcome(authorization, runtime)
        if authorization_outcome is not None:
            qualification = _unevaluated_qualification_report(
                runtime,
                candidate=candidate,
                corpus_sha256=corpus_sha256,
                evaluation_outcome=authorization_outcome,
                campaign_ledger=campaign_ledger,
                planned_base_attempts=plan.qualification_base_provider_attempts_per_model,
            )
            reports.append(qualification)
            return _build_qualification_campaign_report(
                plan=plan,
                corpus_sha256=corpus_sha256,
                fake_baseline=fake_baseline,
                provider_free_preflight=provider_free_preflight,
                reports=reports,
                full_evaluations=full_evaluations,
                evaluation_outcome=authorization_outcome,
                next_required_model=None,
                campaign_ledger=campaign_ledger,
            )

        runtime, attempt_meter = _instrument_runtime(runtime)
        qualification = _evaluate_qualification_candidate(
            runtime,
            corpus=corpus,
            corpus_sha256=corpus_sha256,
            baseline=baseline,
            candidate=candidate,
            attempt_meter=attempt_meter,
            campaign_ledger=campaign_ledger,
            planned_base_attempts=plan.qualification_base_provider_attempts_per_model,
        )
        reports.append(qualification)
        if qualification.policy_version is not None:
            last_policy_version = qualification.policy_version
        if qualification.evaluation_outcome not in {"qualified", "quality_failed"}:
            return _build_qualification_campaign_report(
                plan=plan,
                corpus_sha256=corpus_sha256,
                fake_baseline=fake_baseline,
                provider_free_preflight=provider_free_preflight,
                reports=reports,
                full_evaluations=full_evaluations,
                evaluation_outcome=cast(
                    QualificationCampaignOutcome,
                    qualification.evaluation_outcome,
                ),
                next_required_model=None,
                campaign_ledger=campaign_ledger,
            )
        if not qualification.qualified:
            continue

        if not campaign_ledger.can_complete_candidate(plan.full_corpus_base_provider_attempts):
            return _build_qualification_campaign_report(
                plan=plan,
                corpus_sha256=corpus_sha256,
                fake_baseline=fake_baseline,
                provider_free_preflight=provider_free_preflight,
                reports=reports,
                full_evaluations=full_evaluations,
                evaluation_outcome="evaluation_budget_exhausted",
                next_required_model=None,
                campaign_ledger=campaign_ledger,
            )
        full_report = _evaluate_candidate(
            runtime,
            corpus=corpus,
            corpus_sha256=corpus_sha256,
            fake_baseline=fake_baseline,
            baseline=baseline,
            pricing=candidate.pricing,
            openai_sdk_version=candidate.openai_sdk_version,
            limits=resolved_limits,
            repetitions=repetitions,
            attempt_meter=attempt_meter,
            campaign_ledger=campaign_ledger,
            planned_base_attempts=plan.full_corpus_base_provider_attempts,
        )
        if qualification.policy_version is None:
            raise ValueError("qualified candidate lacks an exact policy revision")
        full_evaluations.append(
            QueryStudioFullEvaluationRecord(
                model_snapshot=candidate.model_snapshot,
                policy_version=qualification.policy_version,
                report=full_report,
            )
        )
        if full_report.passed:
            return _build_qualification_campaign_report(
                plan=plan,
                corpus_sha256=corpus_sha256,
                fake_baseline=fake_baseline,
                provider_free_preflight=provider_free_preflight,
                reports=reports,
                full_evaluations=full_evaluations,
                evaluation_outcome="selected",
                next_required_model=None,
                campaign_ledger=campaign_ledger,
            )
        if full_report.evaluation_outcome != "quality_failed":
            return _build_qualification_campaign_report(
                plan=plan,
                corpus_sha256=corpus_sha256,
                fake_baseline=fake_baseline,
                provider_free_preflight=provider_free_preflight,
                reports=reports,
                full_evaluations=full_evaluations,
                evaluation_outcome=cast(
                    QualificationCampaignOutcome,
                    full_report.evaluation_outcome,
                ),
                next_required_model=None,
                campaign_ledger=campaign_ledger,
            )

    outcome: QualificationCampaignOutcome
    if len(reports) < len(plan.candidate_order):
        outcome = "awaiting_policy_revision"
        next_required_model = plan.candidate_order[len(reports)]
    elif full_evaluations:
        outcome = "quality_failed"
        next_required_model = None
    else:
        outcome = "qualification_failed"
        next_required_model = None
    return _build_qualification_campaign_report(
        plan=plan,
        corpus_sha256=corpus_sha256,
        fake_baseline=fake_baseline,
        provider_free_preflight=provider_free_preflight,
        reports=reports,
        full_evaluations=full_evaluations,
        evaluation_outcome=outcome,
        next_required_model=next_required_model,
        campaign_ledger=campaign_ledger,
    )


def write_query_studio_live_report(
    report: NanoFirstEvaluationCampaignReport,
    repository_root: Path,
    *,
    json_path: Path,
    markdown_path: Path,
    corpus_repository_root: Path | None = None,
) -> None:
    """Persist the selected/last candidate report beneath the repository root."""

    root = repository_root.resolve()
    corpus, corpus_sha256 = _load_synthetic_corpus(
        root if corpus_repository_root is None else corpus_repository_root
    )
    for candidate_report in report.candidates:
        _validate_candidate_case_matrix(
            candidate_report,
            corpus=corpus,
            corpus_sha256=corpus_sha256,
        )
    resolved_json = _safe_output(root, json_path, ".json")
    resolved_markdown = _safe_output(root, markdown_path, ".md")
    resolved_json.parent.mkdir(parents=True, exist_ok=True)
    resolved_markdown.parent.mkdir(parents=True, exist_ok=True)
    candidate = report.candidates[-1]
    campaign_payload = report.model_dump(mode="json")
    resolved_json.write_bytes(
        (json.dumps(campaign_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
    )
    resolved_markdown.write_text(candidate.markdown(), encoding="utf-8")


def require_fresh_query_studio_qualification_history(
    repository_root: Path,
    *,
    history_directory: Path,
    signing_key: bytes,
    signing_key_version: str,
) -> None:
    """Reject a second fresh current-plan campaign before runtime composition."""

    root = repository_root.resolve()
    history_candidate = (
        history_directory if history_directory.is_absolute() else root / history_directory
    )
    reject_symlink_ancestors(root, history_candidate)
    if history_candidate.is_symlink():
        raise ValueError("qualification history directory cannot be a symlink")
    resolved_history = history_candidate.resolve()
    if not resolved_history.is_relative_to(root):
        raise ValueError("qualification history must remain inside the repository")
    key = _validated_evidence_signing_key(signing_key, signing_key_version)
    if not resolved_history.exists():
        return
    if not resolved_history.is_dir() or resolved_history.is_symlink():
        raise ValueError("qualification history must be a regular directory")
    for path in sorted(resolved_history.glob("campaign-signed-*.json")):
        existing = _read_signed_campaign_envelope(
            path,
            signing_key=key,
            signing_key_version=signing_key_version,
        ).report
        if existing.plan_version == QUALIFICATION_PLAN_VERSION:
            raise ValueError("signed evidence already exists for the current qualification plan")


def load_query_studio_qualification_campaign_report(
    repository_root: Path,
    report_path: Path,
    *,
    signing_key: bytes,
    signing_key_version: str,
    history_directory: Path,
    corpus_repository_root: Path | None = None,
) -> NanoFirstQualificationCampaignReport:
    """Verify one signed history snapshot and reject stale/forked resumes.

    The HMAC and complete on-disk history prevent undetected edits and ordinary
    rollback. Deletion of every newer owner-controlled history file cannot be
    detected here without a separate durable audit reader.
    """

    root = repository_root.resolve()
    configured = report_path if report_path.is_absolute() else root / report_path
    reject_symlink_ancestors(root, configured)
    if configured.is_symlink():
        raise ValueError("qualification resume report cannot be a symlink")
    resolved = _safe_output(root, report_path, ".json")
    try:
        history_candidate = (
            history_directory if history_directory.is_absolute() else root / history_directory
        )
        reject_symlink_ancestors(root, history_candidate)
        if history_candidate.is_symlink():
            raise ValueError("qualification history directory cannot be a symlink")
        resolved_history = history_candidate.resolve()
        if (
            not resolved_history.is_relative_to(root)
            or not resolved_history.is_dir()
            or resolved_history.is_symlink()
            or resolved.parent != resolved_history
        ):
            raise ValueError("qualification resume snapshot is outside its canonical history")
        key = _validated_evidence_signing_key(signing_key, signing_key_version)
        envelope = _read_signed_campaign_envelope(
            resolved,
            signing_key=key,
            signing_key_version=signing_key_version,
        )
        _validate_history_transition(
            resolved_history,
            envelope.report,
            signing_key=key,
            signing_key_version=signing_key_version,
        )
        corpus, corpus_sha256 = _load_synthetic_corpus(
            root if corpus_repository_root is None else corpus_repository_root,
            plan_version=envelope.report.plan_version,
        )
        _validate_qualification_campaign_case_matrices(
            envelope.report,
            corpus=corpus,
            corpus_sha256=corpus_sha256,
        )
        return envelope.report
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise ValueError("qualification resume report is invalid") from error


def write_query_studio_qualification_campaign_report(
    report: NanoFirstQualificationCampaignReport,
    repository_root: Path,
    *,
    json_path: Path,
    markdown_path: Path,
    history_directory: Path,
    signing_key: bytes,
    signing_key_version: str,
    corpus_repository_root: Path | None = None,
) -> tuple[Path, Path]:
    """Sign current evidence and retain an immutable content-addressed snapshot."""

    root = repository_root.resolve()
    key = _validated_evidence_signing_key(signing_key, signing_key_version)
    corpus, corpus_sha256 = _load_synthetic_corpus(
        root if corpus_repository_root is None else corpus_repository_root,
        plan_version=report.plan_version,
    )
    _validate_qualification_campaign_case_matrices(
        report,
        corpus=corpus,
        corpus_sha256=corpus_sha256,
    )
    resolved_json = _safe_non_symlink_output(root, json_path, ".json")
    resolved_markdown = _safe_non_symlink_output(root, markdown_path, ".md")
    history_candidate = (
        history_directory if history_directory.is_absolute() else root / history_directory
    )
    reject_symlink_ancestors(root, history_candidate)
    if history_candidate.is_symlink():
        raise ValueError("qualification history directory cannot be a symlink")
    resolved_history = history_candidate.resolve()
    if not resolved_history.is_relative_to(root):
        raise ValueError("qualification history must remain inside the repository")
    resolved_history.mkdir(parents=True, exist_ok=True)
    if not resolved_history.is_dir() or resolved_history.is_symlink():
        raise ValueError("qualification history must be a regular directory")

    _validate_history_transition(
        resolved_history,
        report,
        signing_key=key,
        signing_key_version=signing_key_version,
    )
    envelope = _signed_campaign_envelope(
        report,
        signing_key=key,
        signing_key_version=signing_key_version,
    )
    json_bytes = envelope.json_bytes()
    markdown_bytes = report.markdown().encode("utf-8")
    fingerprint = hashlib.sha256(json_bytes).hexdigest()
    history_json = resolved_history / f"campaign-signed-{fingerprint}.json"
    history_markdown = resolved_history / f"campaign-signed-{fingerprint}.md"
    _write_immutable_evidence(history_json, json_bytes)
    _write_immutable_evidence(history_markdown, markdown_bytes)

    resolved_json.parent.mkdir(parents=True, exist_ok=True)
    resolved_markdown.parent.mkdir(parents=True, exist_ok=True)
    resolved_json.write_bytes(json_bytes)
    resolved_markdown.write_bytes(markdown_bytes)
    return history_json, history_markdown


def _validated_evidence_signing_key(
    signing_key: bytes,
    signing_key_version: str,
) -> bytes:
    if (
        type(signing_key) is not bytes
        or len(signing_key) < 32
        or len(set(signing_key)) < 8
        or _KEY_VERSION.fullmatch(signing_key_version) is None
    ):
        raise ValueError("qualification evidence signing material is invalid")
    return signing_key


def _campaign_signature_payload(
    report_sha256: str,
    signing_key_version: str,
) -> bytes:
    return json.dumps(
        {
            "report_sha256": report_sha256,
            "schema_version": 1,
            "signature_domain": _QUALIFICATION_EVIDENCE_DOMAIN,
            "signature_key_version": signing_key_version,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _campaign_signature(
    report_sha256: str,
    *,
    signing_key: bytes,
    signing_key_version: str,
) -> str:
    derived_key = hmac.new(
        signing_key,
        _QUALIFICATION_EVIDENCE_KEY_DOMAIN,
        hashlib.sha256,
    ).digest()
    return hmac.new(
        derived_key,
        _campaign_signature_payload(report_sha256, signing_key_version),
        hashlib.sha256,
    ).hexdigest()


def _signed_campaign_envelope(
    report: NanoFirstQualificationCampaignReport,
    *,
    signing_key: bytes,
    signing_key_version: str,
) -> _QualificationCampaignEvidenceEnvelope:
    report_sha256 = query_studio_fingerprint(report.model_dump(mode="json"))
    return _QualificationCampaignEvidenceEnvelope(
        signature_key_version=signing_key_version,
        report_sha256=report_sha256,
        report=report,
        signature=_campaign_signature(
            report_sha256,
            signing_key=signing_key,
            signing_key_version=signing_key_version,
        ),
    )


def _read_signed_campaign_envelope(
    path: Path,
    *,
    signing_key: bytes,
    signing_key_version: str,
) -> _QualificationCampaignEvidenceEnvelope:
    matched = _SIGNED_CAMPAIGN_FILE.fullmatch(path.name)
    if matched is None:
        raise ValueError("qualification resume requires a content-addressed history snapshot")
    raw = read_bounded_regular_file(path, maximum_bytes=4 * 1024 * 1024)
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), matched.group(1)):
        raise ValueError("qualification resume snapshot content address is invalid")
    payload = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_reject_duplicate_json_pairs,
    )
    envelope = _QualificationCampaignEvidenceEnvelope.model_validate(payload)
    if envelope.signature_key_version != signing_key_version:
        raise ValueError("qualification resume snapshot key version is unavailable")
    expected = _campaign_signature(
        envelope.report_sha256,
        signing_key=signing_key,
        signing_key_version=signing_key_version,
    )
    if not hmac.compare_digest(envelope.signature, expected):
        raise ValueError("qualification resume snapshot signature is invalid")
    return envelope


def _campaign_identity(report: NanoFirstQualificationCampaignReport) -> str:
    return query_studio_fingerprint(
        {
            "candidate_order": report.candidate_order,
            "corpus_sha256": report.corpus_sha256,
            "fake_baseline_configuration_fingerprint": (
                report.fake_baseline_configuration_fingerprint
            ),
            "fake_baseline_model": report.fake_baseline_model,
            "full_corpus_base_provider_attempts": (report.full_corpus_base_provider_attempts),
            "limits": report.limits.model_dump(mode="json"),
            "plan_version": report.plan_version,
            "provider_free_preflight": (
                report.provider_free_preflight.model_dump(mode="json")
                if report.provider_free_preflight is not None
                else None
            ),
            "qualification_base_provider_attempts_per_model": (
                report.qualification_base_provider_attempts_per_model
            ),
            "worst_case_base_provider_attempts": (report.worst_case_base_provider_attempts),
        }
    )


def _usage_is_monotonic(
    newer: ProviderEvaluationUsage,
    older: ProviderEvaluationUsage,
) -> bool:
    return (
        newer.provider_attempts >= older.provider_attempts
        and newer.input_tokens >= older.input_tokens
        and newer.output_reasoning_tokens >= older.output_reasoning_tokens
        and newer.duration_ms >= older.duration_ms
        and newer.calculated_cost_eur >= older.calculated_cost_eur
    )


def _campaign_report_extends(
    newer: NanoFirstQualificationCampaignReport,
    older: NanoFirstQualificationCampaignReport,
) -> bool:
    return (
        _campaign_identity(newer) == _campaign_identity(older)
        and len(newer.qualification_reports) >= len(older.qualification_reports)
        and newer.qualification_reports[: len(older.qualification_reports)]
        == older.qualification_reports
        and len(newer.full_evaluations) >= len(older.full_evaluations)
        and newer.full_evaluations[: len(older.full_evaluations)] == older.full_evaluations
        and _usage_is_monotonic(newer.usage, older.usage)
        and newer != older
    )


def _validate_history_transition(
    history_directory: Path,
    report: NanoFirstQualificationCampaignReport,
    *,
    signing_key: bytes,
    signing_key_version: str,
) -> None:
    for path in sorted(history_directory.glob("campaign-signed-*.json")):
        existing = _read_signed_campaign_envelope(
            path,
            signing_key=signing_key,
            signing_key_version=signing_key_version,
        ).report
        if _campaign_identity(existing) != _campaign_identity(report) or existing == report:
            continue
        if _campaign_report_extends(report, existing):
            continue
        if _campaign_report_extends(existing, report):
            raise ValueError("qualification resume snapshot is stale")
        raise ValueError("qualification campaign history contains a fork")


def _safe_non_symlink_output(root: Path, configured: Path, suffix: str) -> Path:
    candidate = configured if configured.is_absolute() else root / configured
    reject_symlink_ancestors(root, candidate)
    if candidate.is_symlink():
        raise ValueError("qualification output cannot be a symlink")
    resolved = _safe_output(root, configured, suffix)
    if resolved.exists() and not resolved.is_file():
        raise ValueError("qualification output must be a regular file")
    return resolved


def _write_immutable_evidence(path: Path, payload: bytes) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(payload)
    except FileExistsError:
        try:
            existing = read_bounded_regular_file(
                path,
                maximum_bytes=len(payload),
            )
        except ValueError as error:
            raise ValueError("qualification history fingerprint collision") from error
        if existing != payload:
            raise ValueError("qualification history fingerprint collision") from None


def read_bounded_regular_file(
    path: Path,
    *,
    maximum_bytes: int,
) -> bytes:
    """Read one stable regular file without following a final symlink."""

    if type(maximum_bytes) is not int or maximum_bytes < 1:
        raise ValueError("bounded evidence size is invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size < 0 or before.st_size > maximum_bytes:
            raise OSError("evidence file is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        path_after = path.stat(follow_symlinks=False)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        path_identity = (
            path_after.st_dev,
            path_after.st_ino,
            path_after.st_mode,
            path_after.st_size,
            path_after.st_mtime_ns,
            path_after.st_ctime_ns,
        )
        if (
            before_identity != after_identity
            or after_identity != path_identity
            or len(raw) != after.st_size
            or len(raw) > maximum_bytes
        ):
            raise OSError("evidence file changed while being read")
        return raw
    except OSError as error:
        raise ValueError("bounded evidence file is invalid") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def reject_symlink_ancestors(root: Path, target: Path) -> None:
    """Reject every symlink component from one resolved trust root to a target."""

    if not root.is_absolute() or not target.is_absolute():
        raise ValueError("evidence path trust boundary is invalid")
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise ValueError("evidence path escapes its trust boundary") from error
    if ".." in relative.parts:
        raise ValueError("evidence path traversal is invalid")
    current = root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise ValueError("evidence path contains a symlink component")


def _reject_duplicate_json_pairs(
    pairs: Sequence[tuple[str, object]],
) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("qualification resume report contains duplicate JSON keys")
        payload[key] = value
    return payload


def classify_adversarial_without_egress(text: str) -> str:
    """Exercise only the production guard; unblocked text still requires runtime."""

    return _production_guard_outcome(text)


def _instrument_runtime(
    runtime: QueryStudioRuntimeServices,
) -> tuple[QueryStudioRuntimeServices, _ProviderAttemptMeter]:
    """Place a meter immediately around raw provider delegates, including retries."""

    meter = _ProviderAttemptMeter()
    prepare = runtime.prepare_natural
    if prepare is None or runtime.expansion is None:
        return runtime, meter
    if runtime.configuration.external_ai and not external_runtime_admission_is_aligned(runtime):
        return runtime, meter

    # Both field-match and analytical expansion are server-owned local stages in v18.
    # Only the interpretation delegate can cross the provider boundary.
    expansion = prepare.expansion

    interpreter: QueryStudioIntentPort
    if type(prepare.interpreter) is AdmittedQueryStudioIntent:
        interpreter = replace(
            prepare.interpreter,
            delegate=_MeteredQueryStudioIntent(
                delegate=prepare.interpreter.delegate,
                meter=meter,
            ),
        )
    else:
        interpreter = _MeteredQueryStudioIntent(
            delegate=prepare.interpreter,
            meter=meter,
        )

    metered_prepare = replace(
        prepare,
        expansion=expansion,
        interpreter=interpreter,
    )
    return (
        replace(
            runtime,
            expansion=expansion,
            prepare_natural=metered_prepare,
        ),
        meter,
    )


def _planned_base_provider_attempts(corpus: _LiveCorpus, repetitions: int) -> int:
    return (
        sum(
            _planned_field_match_provider_attempts(
                item.descriptions.es,
                UserLanguage.SPANISH,
            )
            + _planned_field_match_provider_attempts(
                item.descriptions.en,
                UserLanguage.ENGLISH,
            )
            for item in corpus.positive_mappings
        )
        + sum(
            0
            if _negative_requires_no_egress(item.text)
            else _planned_field_match_provider_attempts(
                item.text,
                UserLanguage.SPANISH,
            )
            for item in corpus.true_negatives
        )
        + repetitions
        * sum(
            _planned_field_match_provider_attempts(
                item.text,
                UserLanguage.SPANISH,
            )
            for item in corpus.critical_ambiguities
        )
        + sum(
            _planned_analytical_provider_attempts(
                text,
                language,
            )
            for item in corpus.core_queries
            for repetition in range(1, repetitions + 1)
            for text, language in (_core_repetition_input(item, repetition),)
        )
        + sum(
            0
            if item.expected == "sensitive_input_blocked"
            else _planned_analytical_provider_attempts(
                item.text,
                UserLanguage.SPANISH,
            )
            for item in corpus.adversarial_inputs
        )
    )


def _core_repetition_input(
    core: _CoreCase,
    repetition: int,
) -> tuple[str, UserLanguage]:
    """Use the frozen original then holdout paraphrases without changing case identity."""

    if repetition < 1:
        raise ValueError("core repetition must be positive")
    variants = (
        (core.text, UserLanguage(core.language)),
        *((paraphrase.text, UserLanguage(paraphrase.language)) for paraphrase in core.paraphrases),
    )
    return variants[(repetition - 1) % len(variants)]


def _planned_qualification_provider_attempts(corpus: _LiveCorpus) -> int:
    positives = {item.id: item for item in corpus.positive_mappings}
    positive_attempts = sum(
        _planned_field_match_provider_attempts(
            (
                positives[case_id].descriptions.es
                if language is UserLanguage.SPANISH
                else positives[case_id].descriptions.en
            ),
            language,
        )
        for case_id, language in _QUALIFICATION_POSITIVES
    )
    negative = next(item for item in corpus.true_negatives if item.id == _QUALIFICATION_NEGATIVE_ID)
    ambiguity = next(
        item for item in corpus.critical_ambiguities if item.id == _QUALIFICATION_AMBIGUITY_ID
    )
    core = next(item for item in corpus.core_queries if item.id == _QUALIFICATION_CORE_ID)
    adversarial = next(
        item for item in corpus.adversarial_inputs if item.id == _QUALIFICATION_ADVERSARIAL_ID
    )
    return (
        positive_attempts
        + _planned_field_match_provider_attempts(
            negative.text,
            UserLanguage.SPANISH,
        )
        + _planned_field_match_provider_attempts(
            ambiguity.text,
            UserLanguage.SPANISH,
        )
        + _planned_analytical_provider_attempts(
            core.text,
            UserLanguage(core.language),
        )
        + (
            0
            if _negative_requires_no_egress(adversarial.text)
            else _planned_analytical_provider_attempts(
                adversarial.text,
                UserLanguage.SPANISH,
            )
        )
    )


def _planned_field_match_provider_attempts(
    text: str,
    language: UserLanguage,
) -> int:
    classify_description_expansion_route(
        DescriptionExpansionInput(
            text=DescriptionQuery(text),
            language=language,
            lane=DescriptionExpansionRoute.FIELD_MATCH,
        )
    )
    return 0


def _planned_analytical_provider_attempts(
    text: str,
    language: UserLanguage,
) -> int:
    route = classify_description_expansion_route(
        DescriptionExpansionInput(
            text=DescriptionQuery(text),
            language=language,
            lane=DescriptionExpansionRoute.ANALYTICAL,
        )
    )
    return int(route is DescriptionExpansionRoute.ANALYTICAL)


def _authorization_outcome(
    authorization: EvaluationCandidateAuthorization,
    runtime: QueryStudioRuntimeServices,
) -> Literal["policy_unavailable", "policy_mismatch"] | None:
    if authorization.status == "policy_unavailable":
        return "policy_unavailable"
    configuration = runtime.configuration
    if (
        (configuration.external_ai and not external_runtime_admission_is_aligned(runtime))
        or authorization.workspace_id != runtime.scope.workspace_id
        or authorization.model_snapshot != configuration.model_snapshot
        or authorization.endpoint_region != configuration.endpoint_region
        or authorization.configuration_fingerprint != configuration.fingerprint
    ):
        return "policy_mismatch"
    return None


def external_runtime_admission_is_aligned(
    runtime: QueryStudioRuntimeServices,
) -> bool:
    """Require local expansion and the exact admitted interpretation stage."""

    prepare = runtime.prepare_natural
    expansion = runtime.expansion
    if (
        not runtime.configuration.external_ai
        or prepare is None
        or type(expansion) is not BoundaryScreenedDescriptionExpansionPreflight
        or type(prepare.expansion) is not BoundaryScreenedDescriptionExpansionPreflight
        or type(prepare.interpreter) is not AdmittedQueryStudioIntent
        or expansion is not prepare.expansion
    ):
        return False
    interpreter = prepare.interpreter
    return (
        interpreter.workspace_id == runtime.scope.workspace_id
        and interpreter.configuration is runtime.configuration
        and prepare.configuration is runtime.configuration
        and prepare.nonces is interpreter.nonces
        and interpreter.semantic_scope_fingerprint
        == runtime.configuration.public_metadata_semantic_scope_fingerprint
        and interpreter.estimated_input_tokens
        == openai_interpretation_input_token_reservation_bound()
        and interpreter.estimated_output_tokens
        == _APPROVED_INTERPRETATION_OUTPUT_TOKENS_PER_ATTEMPT
        and getattr(interpreter.delegate, "configuration", None) == runtime.configuration
    )


def _unevaluated_candidate_report(
    runtime: QueryStudioRuntimeServices,
    *,
    corpus: _LiveCorpus,
    corpus_sha256: str,
    fake_baseline: QueryStudioRuntimeServices,
    pricing: ModelTokenPricing,
    openai_sdk_version: str,
    limits: EvaluationBudgetLimits,
    repetitions: int,
    evaluation_outcome: Literal["policy_unavailable", "policy_mismatch"],
    planned_base_attempts: int,
) -> QueryStudioCandidateEvaluationReport:
    positive_total = len(corpus.positive_mappings) * 2
    sensitive_negative_cases = sum(
        _negative_requires_no_egress(item.text) for item in corpus.true_negatives
    )
    safe_negative_cases = len(corpus.true_negatives) - sensitive_negative_cases
    ambiguity_trials = len(corpus.critical_ambiguities) * repetitions
    core_trials = len(corpus.core_queries) * repetitions
    planned_case_count = (
        positive_total
        + len(corpus.true_negatives)
        + ambiguity_trials
        + core_trials
        + len(corpus.adversarial_inputs)
    )
    configuration = runtime.configuration
    return QueryStudioCandidateEvaluationReport(
        model_snapshot=configuration.model_snapshot,
        openai_sdk_version=openai_sdk_version,
        adapter=configuration.adapter,
        reasoning_effort=configuration.reasoning_effort,
        endpoint_region=configuration.endpoint_region,
        configuration_fingerprint=configuration.fingerprint,
        prompt_version=configuration.prompt_version,
        output_schema_version=configuration.schema_version,
        matcher_version=configuration.matcher_version,
        attempt_policy_version=configuration.attempt_policy_version,
        external_ai=configuration.external_ai,
        pricing=pricing,
        limits=limits,
        planned_base_provider_attempts=planned_base_attempts,
        retry_headroom_attempts=limits.max_provider_attempts - planned_base_attempts,
        corpus_sha256=corpus_sha256,
        fake_baseline_model=fake_baseline.configuration.model_snapshot,
        fake_baseline_configuration_fingerprint=fake_baseline.configuration.fingerprint,
        metrics=LiveRetrievalMetrics(
            metric_basis="not_evaluated",
            positive_cases=positive_total,
            observed_positive_cases=0,
            unattempted_positive_cases=positive_total,
            top_1_correct=0,
            top_1_accuracy=0,
            top_3_correct=0,
            top_3_recall=0,
            recall_at_20_correct=0,
            recall_at_20=0,
            reciprocal_rank_sum=0,
            mean_reciprocal_rank=0,
            negative_cases_total=len(corpus.true_negatives),
            safe_negative_cases=safe_negative_cases,
            observed_safe_negative_cases=0,
            unattempted_safe_negative_cases=safe_negative_cases,
            negative_no_match_correct=0,
            no_match_specificity=0,
            sensitive_negative_cases=sensitive_negative_cases,
            observed_sensitive_negative_cases=0,
            unattempted_sensitive_negative_cases=sensitive_negative_cases,
            sensitive_negative_blocked=0,
            sensitive_negative_block_rate=0,
            ambiguity_unique_cases=len(corpus.critical_ambiguities),
            ambiguity_repetitions=repetitions,
            ambiguity_trials=ambiguity_trials,
            observed_ambiguity_trials=0,
            unattempted_ambiguity_trials=ambiguity_trials,
            ambiguity_correct=0,
            ambiguity_recall=0,
            core_unique_cases=len(corpus.core_queries),
            core_repetitions=repetitions,
            core_trials=core_trials,
            observed_core_trials=0,
            unattempted_core_trials=core_trials,
            exact_core_successes=0,
            exact_core_success_rate=0,
            adversarial_cases=len(corpus.adversarial_inputs),
            observed_adversarial_cases=0,
            unattempted_adversarial_cases=len(corpus.adversarial_inputs),
        ),
        usage=ProviderEvaluationUsage(
            provider_attempts=0,
            input_tokens=0,
            output_reasoning_tokens=0,
            duration_ms=0,
            calculated_cost_eur=Decimal("0"),
            budget_exhausted=False,
            within_budget=True,
        ),
        planned_case_count=planned_case_count,
        observed_case_count=0,
        unattempted_case_count=planned_case_count,
        case_outcomes=(),
        top_1_errors=(),
        false_negatives_at_20=(),
        false_positives=(),
        ambiguity_misses=(),
        core_mismatches=(),
        adversarial_misses=(),
        adversarial_provider_attempts_total=0,
        blocked_adversarial_egress_attempts=0,
        unknown_candidate_ids=0,
        cross_tenant_candidates=0,
        sql_tool_or_approval_outputs=0,
        runtime_model_cascade_detected=False,
        evaluation_outcome=evaluation_outcome,
        quality_evaluated=False,
        complete=False,
        passed=False,
    )


def _evaluate_candidate(
    runtime: QueryStudioRuntimeServices,
    *,
    corpus: _LiveCorpus,
    corpus_sha256: str,
    fake_baseline: QueryStudioRuntimeServices,
    baseline: Mapping[str, _BaselineCore],
    pricing: ModelTokenPricing,
    openai_sdk_version: str,
    limits: EvaluationBudgetLimits,
    repetitions: int,
    attempt_meter: _ProviderAttemptMeter,
    campaign_ledger: _CampaignUsageLedger,
    planned_base_attempts: int,
) -> QueryStudioCandidateEvaluationReport:
    if runtime.scope != fake_baseline.scope:
        raise ValueError("candidate and fake baseline must use the exact same semantic scope")
    if (
        runtime.expansion is None
        or runtime.prepare_natural is None
        or runtime.confirm_natural is None
    ):
        raise ValueError("candidate runtime lacks the complete natural-language lane")
    ledger = _UsageLedger(
        limits=limits,
        pricing=pricing,
        campaign=campaign_ledger,
    )
    runtime = _with_stage_budget(
        runtime,
        ledger=ledger,
        meter=attempt_meter,
    )
    expansion = runtime.expansion
    if expansion is None:
        raise ValueError("candidate runtime lost its expansion stage")
    state = _EvaluationState(positive_ranks=[])
    configuration = runtime.configuration

    for positive in corpus.positive_mappings:
        for language, text in (
            (UserLanguage.SPANISH, positive.descriptions.es),
            (UserLanguage.ENGLISH, positive.descriptions.en),
        ):
            if not _preflight_expansion(ledger, configuration):
                state.complete = False
                break
            before = _usage_snapshot(ledger)
            meter_before = _meter_snapshot(attempt_meter)
            case_id = f"{positive.id}:{language.value}"
            output_failure_category: ProviderOutputFailureCategory | None = None
            try:
                result = expansion.expand(
                    DescriptionExpansionInput(
                        text=DescriptionQuery(text),
                        language=language,
                        lane=DescriptionExpansionRoute.FIELD_MATCH,
                    )
                )
                ledger.record(
                    _usage_tuple(result.usage),
                    observed_attempts=attempt_meter.attempts - meter_before[0],
                    observed_duration_ms=attempt_meter.duration_ms - meter_before[1],
                    failed_attempt_input_charge=_MAX_PROVIDER_INPUT_TOKENS_PER_ATTEMPT,
                    failed_attempt_output_charge=(_MAX_EXPANSION_OUTPUT_TOKENS_PER_ATTEMPT),
                    configuration=configuration,
                )
                ranked = _rank_expansion(runtime, result.expansion)
                rank = _rank_of_expected(
                    ranked,
                    logical_field=positive.logical_field,
                    connection_id=positive.connection_id,
                    physical_field=positive.physical_field,
                )
                actual = "ranked" if rank is not None else "no_match"
            except QueryStudioPortError as error:
                output_failure_category = error.output_failure_category
                ledger.record(
                    (),
                    observed_attempts=attempt_meter.attempts - meter_before[0],
                    observed_duration_ms=attempt_meter.duration_ms - meter_before[1],
                    failed_attempt_input_charge=_MAX_PROVIDER_INPUT_TOKENS_PER_ATTEMPT,
                    failed_attempt_output_charge=(_MAX_EXPANSION_OUTPUT_TOKENS_PER_ATTEMPT),
                    configuration=configuration,
                )
                rank = None
                actual = _port_error_outcome(error.code)
                if actual == "runtime_model_cascade":
                    ledger.cascade_detected = True
                _classify_candidate_failure(state, actual)
            except (TypeError, ValueError):
                ledger.record(
                    (),
                    observed_attempts=attempt_meter.attempts - meter_before[0],
                    observed_duration_ms=attempt_meter.duration_ms - meter_before[1],
                    failed_attempt_input_charge=_MAX_PROVIDER_INPUT_TOKENS_PER_ATTEMPT,
                    failed_attempt_output_charge=(_MAX_EXPANSION_OUTPUT_TOKENS_PER_ATTEMPT),
                    configuration=configuration,
                )
                rank = None
                actual = "runtime_invalid"
                state.operational_outcome = "runtime_invalid"
            if ledger.cascade_detected:
                state.operational_outcome = "runtime_model_cascade"
            state.positive_ranks.append(rank)
            if state.operational_outcome is None and rank != 1:
                assert state.top_1_errors is not None
                state.top_1_errors.append(case_id)
            if state.operational_outcome is None and (rank is None or rank > 20):
                assert state.false_negatives is not None
                state.false_negatives.append(case_id)
            assert state.outcomes is not None
            state.outcomes.append(
                _case_outcome(
                    case_id=case_id,
                    suite="positive",
                    expected="ranked_at_20",
                    actual=actual,
                    repetition=1,
                    rank=rank,
                    before=before,
                    ledger=ledger,
                    passed=rank is not None and rank <= 20,
                    output_failure_category=output_failure_category,
                )
            )
            if _candidate_evaluation_must_stop(state, ledger):
                state.complete = False
                break
        if not state.complete:
            break

    if state.complete:
        for negative in corpus.true_negatives:
            if _negative_requires_no_egress(negative.text):
                before = _usage_snapshot(ledger)
                actual = _production_guard_outcome(negative.text)
                passed = actual == "sensitive_input_blocked"
                state.sensitive_negative_correct += int(passed)
                if not passed:
                    assert state.false_positives is not None
                    state.false_positives.append(negative.id)
                assert state.outcomes is not None
                state.outcomes.append(
                    _case_outcome(
                        case_id=negative.id,
                        suite="negative",
                        expected="sensitive_input_blocked",
                        actual=actual,
                        repetition=1,
                        rank=None,
                        before=before,
                        ledger=ledger,
                        passed=passed,
                    )
                )
                continue
            if not _preflight_expansion(ledger, configuration):
                state.complete = False
                break
            before = _usage_snapshot(ledger)
            meter_before = _meter_snapshot(attempt_meter)
            output_failure_category = None
            try:
                result = expansion.expand(
                    DescriptionExpansionInput(
                        text=DescriptionQuery(negative.text),
                        language=UserLanguage.SPANISH,
                        lane=DescriptionExpansionRoute.FIELD_MATCH,
                    )
                )
                ledger.record(
                    _usage_tuple(result.usage),
                    observed_attempts=attempt_meter.attempts - meter_before[0],
                    observed_duration_ms=attempt_meter.duration_ms - meter_before[1],
                    failed_attempt_input_charge=_MAX_PROVIDER_INPUT_TOKENS_PER_ATTEMPT,
                    failed_attempt_output_charge=(_MAX_EXPANSION_OUTPUT_TOKENS_PER_ATTEMPT),
                    configuration=configuration,
                )
                ranked = _rank_expansion(runtime, result.expansion)
                actual = "no_match" if not ranked else "candidate_returned"
            except QueryStudioPortError as error:
                output_failure_category = error.output_failure_category
                ledger.record(
                    (),
                    observed_attempts=attempt_meter.attempts - meter_before[0],
                    observed_duration_ms=attempt_meter.duration_ms - meter_before[1],
                    failed_attempt_input_charge=_MAX_PROVIDER_INPUT_TOKENS_PER_ATTEMPT,
                    failed_attempt_output_charge=(_MAX_EXPANSION_OUTPUT_TOKENS_PER_ATTEMPT),
                    configuration=configuration,
                )
                actual = _port_error_outcome(error.code)
                if actual == "runtime_model_cascade":
                    ledger.cascade_detected = True
                _classify_candidate_failure(state, actual)
            except (TypeError, ValueError):
                ledger.record(
                    (),
                    observed_attempts=attempt_meter.attempts - meter_before[0],
                    observed_duration_ms=attempt_meter.duration_ms - meter_before[1],
                    failed_attempt_input_charge=_MAX_PROVIDER_INPUT_TOKENS_PER_ATTEMPT,
                    failed_attempt_output_charge=(_MAX_EXPANSION_OUTPUT_TOKENS_PER_ATTEMPT),
                    configuration=configuration,
                )
                actual = "runtime_invalid"
                state.operational_outcome = "runtime_invalid"
            if ledger.cascade_detected:
                state.operational_outcome = "runtime_model_cascade"
            passed = actual == "no_match"
            if state.operational_outcome is None:
                state.negative_correct += int(passed)
            if state.operational_outcome is None and not passed:
                assert state.false_positives is not None
                state.false_positives.append(negative.id)
            assert state.outcomes is not None
            state.outcomes.append(
                _case_outcome(
                    case_id=negative.id,
                    suite="negative",
                    expected="no_match",
                    actual=actual,
                    repetition=1,
                    rank=None,
                    before=before,
                    ledger=ledger,
                    passed=passed,
                    output_failure_category=output_failure_category,
                )
            )
            if _candidate_evaluation_must_stop(state, ledger):
                state.complete = False
                break

    if state.complete:
        for ambiguity in corpus.critical_ambiguities:
            for repetition in range(1, repetitions + 1):
                if not _preflight_expansion(ledger, configuration):
                    state.complete = False
                    break
                before = _usage_snapshot(ledger)
                meter_before = _meter_snapshot(attempt_meter)
                output_failure_category = None
                try:
                    result = expansion.expand(
                        DescriptionExpansionInput(
                            text=DescriptionQuery(ambiguity.text),
                            language=UserLanguage.SPANISH,
                            lane=DescriptionExpansionRoute.FIELD_MATCH,
                        )
                    )
                    ledger.record(
                        _usage_tuple(result.usage),
                        observed_attempts=attempt_meter.attempts - meter_before[0],
                        observed_duration_ms=attempt_meter.duration_ms - meter_before[1],
                        failed_attempt_input_charge=_MAX_PROVIDER_INPUT_TOKENS_PER_ATTEMPT,
                        failed_attempt_output_charge=_MAX_EXPANSION_OUTPUT_TOKENS_PER_ATTEMPT,
                        configuration=configuration,
                    )
                    ranked = _rank_expansion(runtime, result.expansion)
                    actual = (
                        "ambiguous" if len(ranked) > 1 else ("aligned" if ranked else "no_match")
                    )
                except QueryStudioPortError as error:
                    output_failure_category = error.output_failure_category
                    ledger.record(
                        (),
                        observed_attempts=attempt_meter.attempts - meter_before[0],
                        observed_duration_ms=attempt_meter.duration_ms - meter_before[1],
                        failed_attempt_input_charge=_MAX_PROVIDER_INPUT_TOKENS_PER_ATTEMPT,
                        failed_attempt_output_charge=_MAX_EXPANSION_OUTPUT_TOKENS_PER_ATTEMPT,
                        configuration=configuration,
                    )
                    actual = _port_error_outcome(error.code)
                    if actual == "runtime_model_cascade":
                        ledger.cascade_detected = True
                    _classify_candidate_failure(state, actual)
                except (TypeError, ValueError):
                    ledger.record(
                        (),
                        observed_attempts=attempt_meter.attempts - meter_before[0],
                        observed_duration_ms=attempt_meter.duration_ms - meter_before[1],
                        failed_attempt_input_charge=_MAX_PROVIDER_INPUT_TOKENS_PER_ATTEMPT,
                        failed_attempt_output_charge=_MAX_EXPANSION_OUTPUT_TOKENS_PER_ATTEMPT,
                        configuration=configuration,
                    )
                    actual = "runtime_invalid"
                    state.operational_outcome = "runtime_invalid"
                if ledger.cascade_detected:
                    state.operational_outcome = "runtime_model_cascade"
                passed = state.operational_outcome is None and actual == "ambiguous"
                if state.operational_outcome is None:
                    state.ambiguity_correct += int(passed)
                if state.operational_outcome is None and not passed:
                    assert state.ambiguity_misses is not None
                    state.ambiguity_misses.append(ambiguity.id)
                assert state.outcomes is not None
                state.outcomes.append(
                    _case_outcome(
                        case_id=ambiguity.id,
                        suite="ambiguity",
                        expected="ambiguous",
                        actual=actual,
                        repetition=repetition,
                        rank=None,
                        before=before,
                        ledger=ledger,
                        passed=passed,
                        output_failure_category=output_failure_category,
                    )
                )
                if _candidate_evaluation_must_stop(state, ledger):
                    state.complete = False
                    break
            if not state.complete:
                break

    if state.complete:
        for core in corpus.core_queries:
            for repetition in range(1, repetitions + 1):
                core_text, language = _core_repetition_input(core, repetition)
                if not _preflight_natural(ledger, configuration):
                    state.complete = False
                    break
                before = _usage_snapshot(ledger)
                meter_before = _meter_snapshot(attempt_meter)
                preview = _prepare(runtime, core_text, language)
                ledger.record(
                    preview.provider_usage,
                    observed_attempts=attempt_meter.attempts - meter_before[0],
                    observed_duration_ms=attempt_meter.duration_ms - meter_before[1],
                    failed_attempt_input_charge=_MAX_PROVIDER_INPUT_TOKENS_PER_ATTEMPT,
                    failed_attempt_output_charge=(_MAX_INTERPRETATION_OUTPUT_TOKENS_PER_ATTEMPT),
                    configuration=configuration,
                )
                _inspect_preview_safety(runtime, preview, state)
                actual = _preview_outcome(preview)
                if actual == "runtime_model_cascade":
                    ledger.cascade_detected = True
                _classify_candidate_failure(state, actual)
                if ledger.cascade_detected:
                    state.operational_outcome = "runtime_model_cascade"
                passed = False
                mismatch_diagnostic = None
                if state.operational_outcome is None and actual == "aligned":
                    confirmed = _confirm(runtime, core_text, language, preview)
                    if confirmed is not None:
                        fingerprint = _validated_request_fingerprint(confirmed.validated_request)
                        if fingerprint == baseline[core.id].request_fingerprint:
                            actual = "fingerprint_match"
                            passed = True
                        else:
                            actual = "fingerprint_mismatch"
                            mismatch_diagnostic = _fingerprint_mismatch_diagnostic(
                                baseline[core.id].validated_request,
                                confirmed.validated_request,
                            )
                if state.operational_outcome is None:
                    state.core_correct += int(passed)
                if state.operational_outcome is None and not passed:
                    assert state.core_mismatches is not None
                    state.core_mismatches.append(core.id)
                assert state.outcomes is not None
                state.outcomes.append(
                    _case_outcome(
                        case_id=core.id,
                        suite="core",
                        expected="fingerprint_match",
                        actual=actual,
                        repetition=repetition,
                        rank=None,
                        before=before,
                        ledger=ledger,
                        passed=passed,
                        output_failure_category=(_preview_output_failure_category(preview)),
                        fingerprint_mismatch_diagnostic=mismatch_diagnostic,
                    )
                )
                if _candidate_evaluation_must_stop(state, ledger):
                    state.complete = False
                    break
            if not state.complete:
                break

    if state.complete:
        for adversarial in corpus.adversarial_inputs:
            before = _usage_snapshot(ledger)
            actual = classify_adversarial_without_egress(adversarial.text)
            output_failure_category = None
            if actual == "guard_not_triggered":
                if not _preflight_natural(ledger, configuration):
                    state.complete = False
                    break
                meter_before = _meter_snapshot(attempt_meter)
                preview = _prepare(runtime, adversarial.text, UserLanguage.SPANISH)
                ledger.record(
                    preview.provider_usage,
                    observed_attempts=attempt_meter.attempts - meter_before[0],
                    observed_duration_ms=attempt_meter.duration_ms - meter_before[1],
                    failed_attempt_input_charge=_MAX_PROVIDER_INPUT_TOKENS_PER_ATTEMPT,
                    failed_attempt_output_charge=(_MAX_INTERPRETATION_OUTPUT_TOKENS_PER_ATTEMPT),
                    configuration=configuration,
                )
                _inspect_preview_safety(runtime, preview, state)
                actual = _preview_outcome(preview)
                output_failure_category = _preview_output_failure_category(preview)
                if actual == "runtime_model_cascade":
                    ledger.cascade_detected = True
                _classify_candidate_failure(state, actual)
                if ledger.cascade_detected:
                    state.operational_outcome = "runtime_model_cascade"
            passed = state.operational_outcome is None and actual == adversarial.expected
            if not passed and state.operational_outcome is None:
                assert state.adversarial_misses is not None
                state.adversarial_misses.append(adversarial.id)
            assert state.outcomes is not None
            state.outcomes.append(
                _case_outcome(
                    case_id=adversarial.id,
                    suite="adversarial",
                    expected=adversarial.expected,
                    actual=actual,
                    repetition=1,
                    rank=None,
                    before=before,
                    ledger=ledger,
                    passed=passed,
                    output_failure_category=output_failure_category,
                )
            )
            if _candidate_evaluation_must_stop(state, ledger):
                state.complete = False
                break

    positive_total = len(corpus.positive_mappings) * 2
    while len(state.positive_ranks) < positive_total:
        state.positive_ranks.append(None)
    ambiguity_trials = len(corpus.critical_ambiguities) * repetitions
    core_trials = len(corpus.core_queries) * repetitions
    sensitive_negative_cases = sum(
        _negative_requires_no_egress(item.text) for item in corpus.true_negatives
    )
    safe_negative_cases = len(corpus.true_negatives) - sensitive_negative_cases
    outcomes = tuple(state.outcomes or ())
    observed_positive_cases = sum(item.suite == "positive" for item in outcomes)
    observed_safe_negative_cases = sum(
        item.suite == "negative" and item.expected_outcome == "no_match" for item in outcomes
    )
    observed_sensitive_negative_cases = sum(
        item.suite == "negative" and item.expected_outcome == "sensitive_input_blocked"
        for item in outcomes
    )
    observed_ambiguity_trials = sum(item.suite == "ambiguity" for item in outcomes)
    observed_core_trials = sum(item.suite == "core" for item in outcomes)
    observed_adversarial_cases = sum(item.suite == "adversarial" for item in outcomes)
    planned_case_count = (
        positive_total
        + len(corpus.true_negatives)
        + ambiguity_trials
        + core_trials
        + len(corpus.adversarial_inputs)
    )
    observed_case_count = len(outcomes)
    complete = state.complete and observed_case_count == planned_case_count
    metric_basis: MetricBasis
    if observed_case_count == 0:
        metric_basis = "not_evaluated"
    elif complete:
        metric_basis = "complete_corpus"
    else:
        metric_basis = "fail_fast_lower_bound"
    ranks = state.positive_ranks
    top_1_correct = sum(rank == 1 for rank in ranks)
    top_3_correct = sum(rank is not None and rank <= 3 for rank in ranks)
    recall_at_20_correct = sum(rank is not None and rank <= 20 for rank in ranks)
    reciprocal_rank_sum = sum(0.0 if rank is None else 1.0 / rank for rank in ranks)
    metrics = LiveRetrievalMetrics(
        metric_basis=metric_basis,
        positive_cases=positive_total,
        observed_positive_cases=observed_positive_cases,
        unattempted_positive_cases=positive_total - observed_positive_cases,
        top_1_correct=top_1_correct,
        top_1_accuracy=top_1_correct / positive_total,
        top_3_correct=top_3_correct,
        top_3_recall=top_3_correct / positive_total,
        recall_at_20_correct=recall_at_20_correct,
        recall_at_20=recall_at_20_correct / positive_total,
        reciprocal_rank_sum=reciprocal_rank_sum,
        mean_reciprocal_rank=reciprocal_rank_sum / positive_total,
        negative_cases_total=len(corpus.true_negatives),
        safe_negative_cases=safe_negative_cases,
        observed_safe_negative_cases=observed_safe_negative_cases,
        unattempted_safe_negative_cases=safe_negative_cases - observed_safe_negative_cases,
        negative_no_match_correct=state.negative_correct,
        no_match_specificity=state.negative_correct / safe_negative_cases,
        sensitive_negative_cases=sensitive_negative_cases,
        observed_sensitive_negative_cases=observed_sensitive_negative_cases,
        unattempted_sensitive_negative_cases=(
            sensitive_negative_cases - observed_sensitive_negative_cases
        ),
        sensitive_negative_blocked=state.sensitive_negative_correct,
        sensitive_negative_block_rate=(state.sensitive_negative_correct / sensitive_negative_cases),
        ambiguity_unique_cases=len(corpus.critical_ambiguities),
        ambiguity_repetitions=repetitions,
        ambiguity_trials=ambiguity_trials,
        observed_ambiguity_trials=observed_ambiguity_trials,
        unattempted_ambiguity_trials=ambiguity_trials - observed_ambiguity_trials,
        ambiguity_correct=state.ambiguity_correct,
        ambiguity_recall=state.ambiguity_correct / ambiguity_trials,
        core_unique_cases=len(corpus.core_queries),
        core_repetitions=repetitions,
        core_trials=core_trials,
        observed_core_trials=observed_core_trials,
        unattempted_core_trials=core_trials - observed_core_trials,
        exact_core_successes=state.core_correct,
        exact_core_success_rate=state.core_correct / core_trials,
        adversarial_cases=len(corpus.adversarial_inputs),
        observed_adversarial_cases=observed_adversarial_cases,
        unattempted_adversarial_cases=(len(corpus.adversarial_inputs) - observed_adversarial_cases),
    )
    usage = ProviderEvaluationUsage(
        provider_attempts=ledger.attempts,
        input_tokens=ledger.input_tokens,
        output_reasoning_tokens=ledger.output_tokens,
        duration_ms=ledger.duration_ms,
        calculated_cost_eur=ledger.cost(),
        budget_exhausted=ledger.budget_exhausted,
        within_budget=ledger.within_budget(),
    )
    passed = (
        complete
        and metrics.top_1_accuracy >= 0.85
        and metrics.top_3_recall >= 1.0
        and metrics.mean_reciprocal_rank >= 0.90
        and metrics.recall_at_20 >= 1.0
        and metrics.no_match_specificity >= 1.0
        and metrics.sensitive_negative_block_rate >= 1.0
        and metrics.ambiguity_recall >= 1.0
        and metrics.exact_core_success_rate >= 1.0
        and usage.within_budget
        and not usage.budget_exhausted
        and not state.false_negatives
        and not state.false_positives
        and not state.ambiguity_misses
        and not state.core_mismatches
        and not state.adversarial_misses
        and state.unknown_candidate_ids == 0
        and state.cross_tenant_candidates == 0
        and state.sql_tool_or_approval_outputs == 0
        and not ledger.cascade_detected
    )
    quality_evaluated = (
        complete or state.quality_failure_outcome is not None
    ) and not ledger.budget_exhausted
    evaluation_outcome: Literal[
        "passed",
        "quality_failed",
        "provider_unavailable",
        "rate_limited",
        "quota_exhausted",
        "evaluation_budget_exhausted",
        "runtime_invalid",
        "runtime_model_cascade",
    ]
    if ledger.budget_exhausted:
        evaluation_outcome = "evaluation_budget_exhausted"
    elif state.operational_outcome is not None:
        evaluation_outcome = state.operational_outcome
    elif state.quality_failure_outcome is not None:
        evaluation_outcome = "quality_failed"
    elif complete:
        evaluation_outcome = "passed" if passed else "quality_failed"
    else:
        evaluation_outcome = "runtime_invalid"
    (
        top_1_errors,
        false_negatives_at_20,
        false_positives,
        ambiguity_misses,
        core_mismatches,
        adversarial_misses,
    ) = _derived_failure_case_ids(outcomes)
    return QueryStudioCandidateEvaluationReport(
        model_snapshot=configuration.model_snapshot,
        openai_sdk_version=openai_sdk_version,
        adapter=configuration.adapter,
        reasoning_effort=configuration.reasoning_effort,
        endpoint_region=configuration.endpoint_region,
        configuration_fingerprint=configuration.fingerprint,
        prompt_version=configuration.prompt_version,
        output_schema_version=configuration.schema_version,
        matcher_version=configuration.matcher_version,
        attempt_policy_version=configuration.attempt_policy_version,
        external_ai=configuration.external_ai,
        pricing=pricing,
        limits=limits,
        planned_base_provider_attempts=planned_base_attempts,
        retry_headroom_attempts=limits.max_provider_attempts - planned_base_attempts,
        corpus_sha256=corpus_sha256,
        fake_baseline_model=fake_baseline.configuration.model_snapshot,
        fake_baseline_configuration_fingerprint=fake_baseline.configuration.fingerprint,
        metrics=metrics,
        usage=usage,
        planned_case_count=planned_case_count,
        observed_case_count=observed_case_count,
        unattempted_case_count=planned_case_count - observed_case_count,
        case_outcomes=outcomes,
        top_1_errors=top_1_errors,
        false_negatives_at_20=false_negatives_at_20,
        false_positives=false_positives,
        ambiguity_misses=ambiguity_misses,
        core_mismatches=core_mismatches,
        adversarial_misses=adversarial_misses,
        adversarial_provider_attempts_total=sum(
            item.provider_attempts for item in state.outcomes or () if item.suite == "adversarial"
        ),
        blocked_adversarial_egress_attempts=sum(
            item.provider_attempts
            for item in state.outcomes or ()
            if item.suite == "adversarial" and item.expected_outcome == "sensitive_input_blocked"
        ),
        unknown_candidate_ids=state.unknown_candidate_ids,
        cross_tenant_candidates=state.cross_tenant_candidates,
        sql_tool_or_approval_outputs=state.sql_tool_or_approval_outputs,
        runtime_model_cascade_detected=ledger.cascade_detected,
        evaluation_outcome=evaluation_outcome,
        quality_evaluated=quality_evaluated,
        complete=complete,
        passed=passed,
    )


def _validate_qualification_resume(
    prior: NanoFirstQualificationCampaignReport,
    *,
    plan: QueryStudioQualificationPlan,
    corpus: _LiveCorpus,
    corpus_sha256: str,
    fake_baseline: QueryStudioRuntimeServices,
    limits: EvaluationBudgetLimits,
) -> None:
    if prior.plan_version != plan.plan_version:
        raise ValueError("qualification resume evidence does not match the current campaign")
    if prior.evaluation_outcome != "awaiting_policy_revision":
        raise ValueError("only an awaiting qualification campaign may be resumed")
    _validate_qualification_campaign_case_matrices(
        prior,
        corpus=corpus,
        corpus_sha256=corpus_sha256,
    )
    if (
        prior.candidate_order != plan.candidate_order
        or prior.corpus_sha256 != corpus_sha256
        or prior.limits != limits
        or prior.qualification_base_provider_attempts_per_model
        != plan.qualification_base_provider_attempts_per_model
        or prior.full_corpus_base_provider_attempts != plan.full_corpus_base_provider_attempts
        or prior.worst_case_base_provider_attempts != plan.worst_case_base_provider_attempts
        or prior.full_corpus_runs_maximum != plan.full_corpus_runs_maximum
        or prior.fake_baseline_model != fake_baseline.configuration.model_snapshot
        or prior.fake_baseline_configuration_fingerprint != fake_baseline.configuration.fingerprint
        or prior.usage.budget_exhausted
        or not prior.usage.within_budget
    ):
        raise ValueError("qualification resume evidence does not match the current campaign")


def _build_qualification_campaign_report(
    *,
    plan: QueryStudioQualificationPlan,
    corpus_sha256: str,
    fake_baseline: QueryStudioRuntimeServices,
    provider_free_preflight: ProviderFreeCorePreflightReport | None,
    reports: Sequence[QueryStudioQualificationReport],
    full_evaluations: Sequence[QueryStudioFullEvaluationRecord],
    evaluation_outcome: QualificationCampaignOutcome,
    next_required_model: str | None,
    campaign_ledger: _CampaignUsageLedger,
) -> NanoFirstQualificationCampaignReport:
    if provider_free_preflight is None or not provider_free_preflight.passed:
        raise ValueError("current campaign cannot be built without a passing provider-free gate")
    return NanoFirstQualificationCampaignReport(
        schema_version=5,
        plan_version=plan.plan_version,
        candidate_order=plan.candidate_order,
        corpus_sha256=corpus_sha256,
        limits=plan.limits,
        qualification_base_provider_attempts_per_model=(
            plan.qualification_base_provider_attempts_per_model
        ),
        full_corpus_base_provider_attempts=plan.full_corpus_base_provider_attempts,
        worst_case_base_provider_attempts=plan.worst_case_base_provider_attempts,
        full_corpus_runs_maximum=plan.full_corpus_runs_maximum,
        fake_baseline_model=fake_baseline.configuration.model_snapshot,
        fake_baseline_configuration_fingerprint=fake_baseline.configuration.fingerprint,
        provider_free_preflight=provider_free_preflight,
        qualification_reports=tuple(reports),
        full_evaluations=tuple(full_evaluations),
        selected_model=next(
            (item.model_snapshot for item in full_evaluations if item.report.passed),
            None,
        ),
        next_required_model=next_required_model,
        evaluation_outcome=evaluation_outcome,
        usage=campaign_ledger.usage(),
    )


def _unevaluated_qualification_report(
    runtime: QueryStudioRuntimeServices,
    *,
    candidate: EvaluationCandidate,
    corpus_sha256: str,
    evaluation_outcome: Literal["policy_unavailable", "policy_mismatch"],
    campaign_ledger: _CampaignUsageLedger,
    planned_base_attempts: int,
) -> QueryStudioQualificationReport:
    configuration = runtime.configuration
    return QueryStudioQualificationReport(
        model_snapshot=configuration.model_snapshot,
        policy_version=candidate.authorization.policy_version,
        openai_sdk_version=candidate.openai_sdk_version,
        adapter=configuration.adapter,
        reasoning_effort=configuration.reasoning_effort,
        endpoint_region=configuration.endpoint_region,
        configuration_fingerprint=configuration.fingerprint,
        prompt_version=configuration.prompt_version,
        output_schema_version=configuration.schema_version,
        matcher_version=configuration.matcher_version,
        attempt_policy_version=configuration.attempt_policy_version,
        external_ai=configuration.external_ai,
        pricing=candidate.pricing,
        corpus_sha256=corpus_sha256,
        planned_base_provider_attempts=planned_base_attempts,
        case_outcomes=(),
        failed_case_ids=(),
        usage=ProviderEvaluationUsage(
            provider_attempts=0,
            input_tokens=0,
            output_reasoning_tokens=0,
            duration_ms=0,
            calculated_cost_eur=Decimal("0"),
            budget_exhausted=campaign_ledger.budget_exhausted,
            within_budget=campaign_ledger.within_budget(),
        ),
        unknown_candidate_ids=0,
        cross_tenant_candidates=0,
        sql_tool_or_approval_outputs=0,
        runtime_model_cascade_detected=False,
        evaluation_outcome=evaluation_outcome,
        complete=False,
        qualified=False,
    )


def _evaluate_qualification_candidate(
    runtime: QueryStudioRuntimeServices,
    *,
    corpus: _LiveCorpus,
    corpus_sha256: str,
    baseline: Mapping[str, _BaselineCore],
    candidate: EvaluationCandidate,
    attempt_meter: _ProviderAttemptMeter,
    campaign_ledger: _CampaignUsageLedger,
    planned_base_attempts: int,
) -> QueryStudioQualificationReport:
    if (
        runtime.expansion is None
        or runtime.prepare_natural is None
        or runtime.confirm_natural is None
    ):
        raise ValueError("qualification runtime lacks the complete natural-language lane")
    ledger = _UsageLedger(
        limits=campaign_ledger.limits,
        pricing=candidate.pricing,
        campaign=campaign_ledger,
    )
    runtime = _with_stage_budget(
        runtime,
        ledger=ledger,
        meter=attempt_meter,
    )
    expansion = runtime.expansion
    if expansion is None:
        raise ValueError("qualification runtime lost its expansion stage")
    state = _EvaluationState(positive_ranks=[])
    configuration = runtime.configuration

    positives = {
        item.id: item
        for item in corpus.positive_mappings
        if item.id in {case_id for case_id, _language in _QUALIFICATION_POSITIVES}
    }
    for case_id, language in _QUALIFICATION_POSITIVES:
        positive = positives.get(case_id)
        if positive is None:
            raise ValueError("qualification positive case is absent from the synthetic corpus")
        if not _preflight_expansion(ledger, configuration):
            state.complete = False
            break
        text = (
            positive.descriptions.es
            if language is UserLanguage.SPANISH
            else positive.descriptions.en
        )
        before = _usage_snapshot(ledger)
        meter_before = _meter_snapshot(attempt_meter)
        rank: int | None = None
        output_failure_category: ProviderOutputFailureCategory | None = None
        try:
            result = expansion.expand(
                DescriptionExpansionInput(
                    text=DescriptionQuery(text),
                    language=language,
                    lane=DescriptionExpansionRoute.FIELD_MATCH,
                )
            )
            ledger.record(
                _usage_tuple(result.usage),
                observed_attempts=attempt_meter.attempts - meter_before[0],
                observed_duration_ms=attempt_meter.duration_ms - meter_before[1],
                failed_attempt_input_charge=_MAX_PROVIDER_INPUT_TOKENS_PER_ATTEMPT,
                failed_attempt_output_charge=_MAX_EXPANSION_OUTPUT_TOKENS_PER_ATTEMPT,
                configuration=configuration,
            )
            ranked = _rank_expansion(runtime, result.expansion)
            rank = _rank_of_expected(
                ranked,
                logical_field=positive.logical_field,
                connection_id=positive.connection_id,
                physical_field=positive.physical_field,
            )
            actual = "ranked" if rank is not None else "no_match"
        except QueryStudioPortError as error:
            output_failure_category = error.output_failure_category
            ledger.record(
                (),
                observed_attempts=attempt_meter.attempts - meter_before[0],
                observed_duration_ms=attempt_meter.duration_ms - meter_before[1],
                failed_attempt_input_charge=_MAX_PROVIDER_INPUT_TOKENS_PER_ATTEMPT,
                failed_attempt_output_charge=_MAX_EXPANSION_OUTPUT_TOKENS_PER_ATTEMPT,
                configuration=configuration,
            )
            actual = _port_error_outcome(error.code)
            if actual == "runtime_model_cascade":
                ledger.cascade_detected = True
            _classify_candidate_failure(state, actual)
        except (TypeError, ValueError):
            ledger.record(
                (),
                observed_attempts=attempt_meter.attempts - meter_before[0],
                observed_duration_ms=attempt_meter.duration_ms - meter_before[1],
                failed_attempt_input_charge=_MAX_PROVIDER_INPUT_TOKENS_PER_ATTEMPT,
                failed_attempt_output_charge=_MAX_EXPANSION_OUTPUT_TOKENS_PER_ATTEMPT,
                configuration=configuration,
            )
            actual = "runtime_invalid"
            state.operational_outcome = "runtime_invalid"
        if ledger.cascade_detected:
            state.operational_outcome = "runtime_model_cascade"
        assert state.outcomes is not None
        state.outcomes.append(
            _case_outcome(
                case_id=f"{positive.id}:{language.value}",
                suite="positive",
                expected="ranked_at_3",
                actual=actual,
                repetition=1,
                rank=rank,
                before=before,
                ledger=ledger,
                passed=rank is not None and rank <= 3,
                output_failure_category=output_failure_category,
            )
        )
        if _candidate_evaluation_must_stop(state, ledger):
            state.complete = False
            break

    negative = next(
        (item for item in corpus.true_negatives if item.id == _QUALIFICATION_NEGATIVE_ID),
        None,
    )
    if state.complete:
        if negative is None or _negative_requires_no_egress(negative.text):
            raise ValueError("qualification negative case is invalid")
        if not _preflight_expansion(ledger, configuration):
            state.complete = False
        else:
            before = _usage_snapshot(ledger)
            meter_before = _meter_snapshot(attempt_meter)
            output_failure_category = None
            try:
                result = expansion.expand(
                    DescriptionExpansionInput(
                        text=DescriptionQuery(negative.text),
                        language=UserLanguage.SPANISH,
                        lane=DescriptionExpansionRoute.FIELD_MATCH,
                    )
                )
                ledger.record(
                    _usage_tuple(result.usage),
                    observed_attempts=attempt_meter.attempts - meter_before[0],
                    observed_duration_ms=attempt_meter.duration_ms - meter_before[1],
                    failed_attempt_input_charge=_MAX_PROVIDER_INPUT_TOKENS_PER_ATTEMPT,
                    failed_attempt_output_charge=_MAX_EXPANSION_OUTPUT_TOKENS_PER_ATTEMPT,
                    configuration=configuration,
                )
                actual = (
                    "no_match"
                    if not _rank_expansion(runtime, result.expansion)
                    else "candidate_returned"
                )
            except QueryStudioPortError as error:
                output_failure_category = error.output_failure_category
                ledger.record(
                    (),
                    observed_attempts=attempt_meter.attempts - meter_before[0],
                    observed_duration_ms=attempt_meter.duration_ms - meter_before[1],
                    failed_attempt_input_charge=_MAX_PROVIDER_INPUT_TOKENS_PER_ATTEMPT,
                    failed_attempt_output_charge=_MAX_EXPANSION_OUTPUT_TOKENS_PER_ATTEMPT,
                    configuration=configuration,
                )
                actual = _port_error_outcome(error.code)
                if actual == "runtime_model_cascade":
                    ledger.cascade_detected = True
                _classify_candidate_failure(state, actual)
            except (TypeError, ValueError):
                ledger.record(
                    (),
                    observed_attempts=attempt_meter.attempts - meter_before[0],
                    observed_duration_ms=attempt_meter.duration_ms - meter_before[1],
                    failed_attempt_input_charge=_MAX_PROVIDER_INPUT_TOKENS_PER_ATTEMPT,
                    failed_attempt_output_charge=_MAX_EXPANSION_OUTPUT_TOKENS_PER_ATTEMPT,
                    configuration=configuration,
                )
                actual = "runtime_invalid"
                state.operational_outcome = "runtime_invalid"
            if ledger.cascade_detected:
                state.operational_outcome = "runtime_model_cascade"
            assert state.outcomes is not None
            state.outcomes.append(
                _case_outcome(
                    case_id=negative.id,
                    suite="negative",
                    expected="no_match",
                    actual=actual,
                    repetition=1,
                    rank=None,
                    before=before,
                    ledger=ledger,
                    passed=actual == "no_match",
                    output_failure_category=output_failure_category,
                )
            )
            if _candidate_evaluation_must_stop(state, ledger):
                state.complete = False

    ambiguity = next(
        (item for item in corpus.critical_ambiguities if item.id == _QUALIFICATION_AMBIGUITY_ID),
        None,
    )
    if state.complete:
        if ambiguity is None:
            raise ValueError("qualification ambiguity case is absent")
        if not _preflight_expansion(ledger, configuration):
            state.complete = False
        else:
            before = _usage_snapshot(ledger)
            meter_before = _meter_snapshot(attempt_meter)
            output_failure_category = None
            try:
                result = expansion.expand(
                    DescriptionExpansionInput(
                        text=DescriptionQuery(ambiguity.text),
                        language=UserLanguage.SPANISH,
                        lane=DescriptionExpansionRoute.FIELD_MATCH,
                    )
                )
                ledger.record(
                    _usage_tuple(result.usage),
                    observed_attempts=attempt_meter.attempts - meter_before[0],
                    observed_duration_ms=attempt_meter.duration_ms - meter_before[1],
                    failed_attempt_input_charge=_MAX_PROVIDER_INPUT_TOKENS_PER_ATTEMPT,
                    failed_attempt_output_charge=_MAX_EXPANSION_OUTPUT_TOKENS_PER_ATTEMPT,
                    configuration=configuration,
                )
                ranked = _rank_expansion(runtime, result.expansion)
                actual = "ambiguous" if len(ranked) > 1 else ("aligned" if ranked else "no_match")
            except QueryStudioPortError as error:
                output_failure_category = error.output_failure_category
                ledger.record(
                    (),
                    observed_attempts=attempt_meter.attempts - meter_before[0],
                    observed_duration_ms=attempt_meter.duration_ms - meter_before[1],
                    failed_attempt_input_charge=_MAX_PROVIDER_INPUT_TOKENS_PER_ATTEMPT,
                    failed_attempt_output_charge=_MAX_EXPANSION_OUTPUT_TOKENS_PER_ATTEMPT,
                    configuration=configuration,
                )
                actual = _port_error_outcome(error.code)
                if actual == "runtime_model_cascade":
                    ledger.cascade_detected = True
                _classify_candidate_failure(state, actual)
            except (TypeError, ValueError):
                ledger.record(
                    (),
                    observed_attempts=attempt_meter.attempts - meter_before[0],
                    observed_duration_ms=attempt_meter.duration_ms - meter_before[1],
                    failed_attempt_input_charge=_MAX_PROVIDER_INPUT_TOKENS_PER_ATTEMPT,
                    failed_attempt_output_charge=_MAX_EXPANSION_OUTPUT_TOKENS_PER_ATTEMPT,
                    configuration=configuration,
                )
                actual = "runtime_invalid"
                state.operational_outcome = "runtime_invalid"
            if ledger.cascade_detected:
                state.operational_outcome = "runtime_model_cascade"
            assert state.outcomes is not None
            state.outcomes.append(
                _case_outcome(
                    case_id=ambiguity.id,
                    suite="ambiguity",
                    expected="ambiguous",
                    actual=actual,
                    repetition=1,
                    rank=None,
                    before=before,
                    ledger=ledger,
                    passed=state.operational_outcome is None and actual == "ambiguous",
                    output_failure_category=output_failure_category,
                )
            )
            if _candidate_evaluation_must_stop(state, ledger):
                state.complete = False

    core = next(
        (item for item in corpus.core_queries if item.id == _QUALIFICATION_CORE_ID),
        None,
    )
    if state.complete:
        if core is None:
            raise ValueError("qualification core case is absent")
        if not _preflight_natural(ledger, configuration):
            state.complete = False
        else:
            language = UserLanguage(core.language)
            before = _usage_snapshot(ledger)
            meter_before = _meter_snapshot(attempt_meter)
            preview = _prepare(runtime, core.text, language)
            ledger.record(
                preview.provider_usage,
                observed_attempts=attempt_meter.attempts - meter_before[0],
                observed_duration_ms=attempt_meter.duration_ms - meter_before[1],
                failed_attempt_input_charge=_MAX_PROVIDER_INPUT_TOKENS_PER_ATTEMPT,
                failed_attempt_output_charge=_MAX_INTERPRETATION_OUTPUT_TOKENS_PER_ATTEMPT,
                configuration=configuration,
            )
            _inspect_preview_safety(runtime, preview, state)
            actual = _preview_outcome(preview)
            if actual == "runtime_model_cascade":
                ledger.cascade_detected = True
            _classify_candidate_failure(state, actual)
            passed = False
            mismatch_diagnostic = None
            if state.operational_outcome is None and actual == "aligned":
                confirmed = _confirm(runtime, core.text, language, preview)
                if confirmed is not None:
                    observed = _validated_request_fingerprint(confirmed.validated_request)
                    if observed == baseline[core.id].request_fingerprint:
                        actual = "fingerprint_match"
                        passed = True
                    else:
                        actual = "fingerprint_mismatch"
                        mismatch_diagnostic = _fingerprint_mismatch_diagnostic(
                            baseline[core.id].validated_request,
                            confirmed.validated_request,
                        )
            if ledger.cascade_detected:
                state.operational_outcome = "runtime_model_cascade"
                passed = False
            assert state.outcomes is not None
            state.outcomes.append(
                _case_outcome(
                    case_id=core.id,
                    suite="core",
                    expected="fingerprint_match",
                    actual=actual,
                    repetition=1,
                    rank=None,
                    before=before,
                    ledger=ledger,
                    passed=passed,
                    output_failure_category=(_preview_output_failure_category(preview)),
                    fingerprint_mismatch_diagnostic=mismatch_diagnostic,
                )
            )
            if _candidate_evaluation_must_stop(state, ledger):
                state.complete = False

    adversarial = next(
        (item for item in corpus.adversarial_inputs if item.id == _QUALIFICATION_ADVERSARIAL_ID),
        None,
    )
    if state.complete:
        if adversarial is None:
            raise ValueError("qualification adversarial case is absent")
        before = _usage_snapshot(ledger)
        actual = classify_adversarial_without_egress(adversarial.text)
        assert state.outcomes is not None
        state.outcomes.append(
            _case_outcome(
                case_id=adversarial.id,
                suite="adversarial",
                expected="sensitive_input_blocked",
                actual=actual,
                repetition=1,
                rank=None,
                before=before,
                ledger=ledger,
                passed=actual == "sensitive_input_blocked",
            )
        )

    outcomes = tuple(state.outcomes or ())
    complete = state.complete and len(outcomes) == _QUALIFICATION_CASE_COUNT
    usage = ProviderEvaluationUsage(
        provider_attempts=ledger.attempts,
        input_tokens=ledger.input_tokens,
        output_reasoning_tokens=ledger.output_tokens,
        duration_ms=ledger.duration_ms,
        calculated_cost_eur=ledger.cost(),
        budget_exhausted=ledger.budget_exhausted,
        within_budget=ledger.within_budget(),
    )
    qualified = (
        complete
        and all(item.passed for item in outcomes)
        and usage.within_budget
        and not usage.budget_exhausted
        and state.unknown_candidate_ids == 0
        and state.cross_tenant_candidates == 0
        and state.sql_tool_or_approval_outputs == 0
        and not ledger.cascade_detected
    )
    evaluation_outcome: QualificationOutcome
    if ledger.budget_exhausted:
        evaluation_outcome = "evaluation_budget_exhausted"
    elif state.operational_outcome is not None:
        evaluation_outcome = state.operational_outcome
    elif state.quality_failure_outcome is not None:
        evaluation_outcome = "quality_failed"
    elif complete:
        evaluation_outcome = "qualified" if qualified else "quality_failed"
    else:
        evaluation_outcome = "runtime_invalid"
    return QueryStudioQualificationReport(
        model_snapshot=configuration.model_snapshot,
        policy_version=candidate.authorization.policy_version,
        openai_sdk_version=candidate.openai_sdk_version,
        adapter=configuration.adapter,
        reasoning_effort=configuration.reasoning_effort,
        endpoint_region=configuration.endpoint_region,
        configuration_fingerprint=configuration.fingerprint,
        prompt_version=configuration.prompt_version,
        output_schema_version=configuration.schema_version,
        matcher_version=configuration.matcher_version,
        attempt_policy_version=configuration.attempt_policy_version,
        external_ai=configuration.external_ai,
        pricing=candidate.pricing,
        corpus_sha256=corpus_sha256,
        planned_base_provider_attempts=planned_base_attempts,
        case_outcomes=outcomes,
        failed_case_ids=tuple(dict.fromkeys(item.case_id for item in outcomes if not item.passed)),
        usage=usage,
        unknown_candidate_ids=state.unknown_candidate_ids,
        cross_tenant_candidates=state.cross_tenant_candidates,
        sql_tool_or_approval_outputs=state.sql_tool_or_approval_outputs,
        runtime_model_cascade_detected=ledger.cascade_detected,
        evaluation_outcome=evaluation_outcome,
        complete=complete,
        qualified=qualified,
    )


def _rank_expansion(
    runtime: QueryStudioRuntimeServices,
    expansion: DescriptionExpansion,
) -> tuple[tuple[str, str, str], ...]:
    ranked: dict[tuple[str, str, str], tuple[int, str]] = {}
    for probe in expansion.probes:
        for request in governed_probe_search_requests(runtime.scope, probe):
            page = runtime.search.execute(request)
            for item in page.items:
                identity = (
                    item.logical_field.root,
                    item.locator.asset.connection_id.root,
                    item.physical_field.root,
                )
                current = ranked.get(identity)
                score = item.signals.total
                if (
                    current is None
                    or score > current[0]
                    or (score == current[0] and item.binding_id < current[1])
                ):
                    ranked[identity] = (score, item.binding_id)
    return tuple(
        identity
        for identity, _value in sorted(
            ranked.items(),
            key=lambda item: (
                -item[1][0],
                item[0][0],
                item[0][1],
                item[1][1],
            ),
        )[:MAX_EXECUTABLE_SHORTLIST]
    )


def _rank_of_expected(
    ranked: Sequence[tuple[str, str, str]],
    *,
    logical_field: str,
    connection_id: str,
    physical_field: str,
) -> int | None:
    return next(
        (
            position
            for position, identity in enumerate(ranked, start=1)
            if identity
            == (
                logical_field,
                connection_id,
                physical_field,
            )
        ),
        None,
    )


def _build_fake_baseline(
    runtime: QueryStudioRuntimeServices,
    cases: Sequence[_CoreCase],
) -> Mapping[str, _BaselineCore]:
    if (
        runtime.ai_mode != "fake"
        or runtime.configuration.external_ai
        or runtime.prepare_natural is None
        or runtime.confirm_natural is None
    ):
        raise ValueError("exact core oracle must be a key-free fake runtime")
    baseline: dict[str, _BaselineCore] = {}
    for case in cases:
        fingerprints: set[str] = set()
        validated_requests: list[ValidatedAnalyticalRequest] = []
        for repetition in range(1, 2 + len(case.paraphrases)):
            text, language = _core_repetition_input(case, repetition)
            preview = runtime.prepare_natural.execute(text, language)
            confirmed = _confirm(runtime, text, language, preview)
            if confirmed is None:
                raise ValueError(f"fake baseline did not confirm core case {case.id}")
            required_models = {item.root for item in confirmed.validated_request.required_models}
            if required_models != set(case.expected_models) or set(
                confirmed.validated_request.join_contract_ids
            ) != set(case.expected_joins):
                raise ValueError(f"fake baseline disagrees with core labels for {case.id}")
            validated_requests.append(confirmed.validated_request)
            fingerprints.add(_validated_request_fingerprint(confirmed.validated_request))
        if len(fingerprints) != 1:
            raise ValueError(f"fake baseline paraphrases disagree for core case {case.id}")
        baseline[case.id] = _BaselineCore(
            request_fingerprint=fingerprints.pop(),
            validated_request=validated_requests[0],
        )
    return baseline


def _prepare(
    runtime: QueryStudioRuntimeServices,
    text: str,
    language: UserLanguage,
) -> QueryStudioPreview:
    if runtime.prepare_natural is None:
        raise ValueError("natural-language preview is unavailable")
    try:
        return runtime.prepare_natural.execute(text, language)
    except QueryStudioPortError as error:
        state = {
            QueryStudioPortErrorCode.PROVIDER_RATE_LIMITED: (
                QueryStudioOperationalState.RATE_LIMITED
            ),
            QueryStudioPortErrorCode.PROVIDER_QUOTA_EXHAUSTED: (
                QueryStudioOperationalState.QUOTA_EXHAUSTED
            ),
            QueryStudioPortErrorCode.SENSITIVE_INPUT_BLOCKED: (
                QueryStudioOperationalState.SENSITIVE_INPUT_BLOCKED
            ),
        }.get(error.code, QueryStudioOperationalState.PROVIDER_UNAVAILABLE)
        return QueryStudioPreview(
            mode="natural_language",
            operational_state=state,
            reason_code=error.code.value,
            output_failure_category=error.output_failure_category,
        )
    except (TypeError, ValueError):
        return QueryStudioPreview(
            mode="natural_language",
            operational_state=QueryStudioOperationalState.PROVIDER_UNAVAILABLE,
            reason_code="evaluation_runtime_failure",
        )


def _confirm(
    runtime: QueryStudioRuntimeServices,
    text: str,
    language: UserLanguage,
    preview: QueryStudioPreview,
) -> ConfirmedQueryStudioRequest | None:
    if (
        runtime.confirm_natural is None
        or preview.semantic_state is not SemanticMatchState.ALIGNED
        or preview.expansion is None
        or preview.proposal is None
        or preview.token is None
    ):
        return None
    try:
        return runtime.confirm_natural.execute(
            QueryStudioConfirmation(
                original_text=DescriptionQuery(text),
                language=language,
                expansion=preview.expansion,
                proposal=preview.proposal,
                token=preview.token,
                action=QueryStudioConfirmationAction.CONFIRM_INTERPRETATION,
            )
        )
    except (QueryStudioPortError, TypeError, ValueError):
        return None


def _inspect_preview_safety(
    runtime: QueryStudioRuntimeServices,
    preview: QueryStudioPreview,
    state: _EvaluationState,
) -> None:
    if preview.shortlist is not None:
        state.cross_tenant_candidates += sum(
            item.binding.locator.asset.workspace_id != runtime.scope.workspace_id
            for item in preview.shortlist.candidates
        )
    if preview.proposal is not None and preview.vocabulary is not None:
        allowed = {item.candidate_id.root for item in preview.vocabulary.candidates}
        state.unknown_candidate_ids += sum(
            item.root not in allowed for item in preview.proposal.referenced_candidate_ids
        )
    # The typed proposal and validated-request contracts expose no SQL, tool, or approval field.
    state.sql_tool_or_approval_outputs += 0


def _preview_outcome(preview: QueryStudioPreview) -> str:
    if preview.semantic_state is not None:
        return preview.semantic_state.value
    if preview.operational_state is not None:
        if preview.reason_code == QueryStudioPortErrorCode.PROVIDER_MODEL_MISMATCH.value:
            return "runtime_model_cascade"
        if preview.reason_code in _VISIBLE_PROVIDER_FAILURE_OUTCOMES:
            return preview.reason_code
        return preview.operational_state.value
    return "runtime_invalid"


def _preview_output_failure_category(
    preview: QueryStudioPreview,
) -> ProviderOutputFailureCategory | None:
    return preview.output_failure_category


def _detected_ambiguity(preview: QueryStudioPreview) -> bool:
    """Count only an explicit semantic ambiguity, never an operational refusal."""

    return (
        preview.operational_state is None and preview.semantic_state is SemanticMatchState.AMBIGUOUS
    )


def _port_error_outcome(code: QueryStudioPortErrorCode) -> str:
    if code is QueryStudioPortErrorCode.SENSITIVE_INPUT_BLOCKED:
        return "sensitive_input_blocked"
    if code.value in _VISIBLE_PROVIDER_FAILURE_OUTCOMES:
        return code.value
    if code is QueryStudioPortErrorCode.PROVIDER_QUOTA_EXHAUSTED:
        return "quota_exhausted"
    if code is QueryStudioPortErrorCode.PROVIDER_RATE_LIMITED:
        return "rate_limited"
    if code is QueryStudioPortErrorCode.PROVIDER_MODEL_MISMATCH:
        return "runtime_model_cascade"
    return "provider_unavailable"


def _candidate_quality_failure_outcome(
    outcome: str,
) -> ProviderQualityFailureOutcome | None:
    if outcome in _PROVIDER_QUALITY_FAILURE_OUTCOMES:
        return cast(ProviderQualityFailureOutcome, outcome)
    return None


def _classify_candidate_failure(state: _EvaluationState, outcome: str) -> None:
    state.operational_outcome = _candidate_operational_outcome(outcome)
    state.quality_failure_outcome = _candidate_quality_failure_outcome(outcome)


def _candidate_operational_outcome(
    outcome: str,
) -> (
    Literal[
        "provider_unavailable",
        "rate_limited",
        "quota_exhausted",
        "runtime_model_cascade",
    ]
    | None
):
    if outcome == "provider_unavailable":
        return "provider_unavailable"
    if outcome in {
        QueryStudioPortErrorCode.PROVIDER_TIMEOUT.value,
        QueryStudioPortErrorCode.PROVIDER_UNAVAILABLE.value,
    }:
        return "provider_unavailable"
    if outcome == "rate_limited":
        return "rate_limited"
    if outcome == "quota_exhausted":
        return "quota_exhausted"
    if outcome == "runtime_model_cascade":
        return "runtime_model_cascade"
    return None


def _candidate_evaluation_must_stop(
    state: _EvaluationState,
    ledger: _UsageLedger,
) -> bool:
    return (
        state.operational_outcome is not None
        or state.quality_failure_outcome is not None
        or ledger.budget_exhausted
    )


def _negative_requires_no_egress(text: str) -> bool:
    return _production_guard_outcome(text) == "sensitive_input_blocked"


def _production_guard_outcome(text: str) -> str:
    try:
        normalize_and_screen_user_text(text)
    except OpenAIAdapterError as error:
        if error.code is OpenAIAdapterErrorCode.SENSITIVE_INPUT_BLOCKED:
            return "sensitive_input_blocked"
        return "guard_rejected"
    return "guard_not_triggered"


def _usage_snapshot(ledger: _UsageLedger) -> tuple[int, int, int, int]:
    return (
        ledger.attempts,
        ledger.input_tokens,
        ledger.output_tokens,
        ledger.duration_ms,
    )


def _usage_tuple(
    usage: ProviderUsageFacts | None,
) -> tuple[ProviderUsageFacts, ...]:
    return () if usage is None else (usage,)


def _meter_snapshot(meter: _ProviderAttemptMeter) -> tuple[int, int]:
    return meter.attempts, meter.duration_ms


def _preflight_expansion(
    ledger: _UsageLedger,
    configuration: ProviderConfigurationFacts,
) -> bool:
    if ledger.stage_accounting:
        return not ledger.budget_exhausted
    if configuration.external_ai:
        ledger.budget_exhausted = True
        ledger.campaign.budget_exhausted = True
        return False
    return ledger.preflight(attempt_slots=0, input_token_slots=0, output_token_slots=0)


def _preflight_natural(
    ledger: _UsageLedger,
    configuration: ProviderConfigurationFacts,
) -> bool:
    if ledger.stage_accounting:
        return not ledger.budget_exhausted
    if configuration.external_ai:
        ledger.budget_exhausted = True
        ledger.campaign.budget_exhausted = True
        return False
    return ledger.preflight(attempt_slots=1, input_token_slots=0, output_token_slots=0)


def _case_outcome(
    *,
    case_id: str,
    suite: Literal["positive", "negative", "ambiguity", "core", "adversarial"],
    expected: str,
    actual: str,
    repetition: int,
    rank: int | None,
    before: tuple[int, int, int, int],
    ledger: _UsageLedger,
    passed: bool,
    output_failure_category: ProviderOutputFailureCategory | None = None,
    fingerprint_mismatch_diagnostic: FingerprintMismatchDiagnostic | None = None,
) -> LiveEvaluationCaseOutcome:
    return LiveEvaluationCaseOutcome(
        case_id=case_id,
        suite=suite,
        repetition=repetition,
        expected_outcome=expected,
        actual_outcome=actual,
        rank=rank,
        provider_attempts=ledger.attempts - before[0],
        input_tokens=ledger.input_tokens - before[1],
        output_reasoning_tokens=ledger.output_tokens - before[2],
        duration_ms=ledger.duration_ms - before[3],
        output_failure_category=output_failure_category,
        fingerprint_mismatch_diagnostic=fingerprint_mismatch_diagnostic,
        passed=passed,
    )


def _load_synthetic_corpus(
    repository_root: Path,
    *,
    plan_version: RetainedQualificationPlanVersion = QUALIFICATION_PLAN_VERSION,
) -> tuple[_LiveCorpus, str]:
    root = repository_root.resolve()
    path = (root / _CORPUS_RELATIVE_PATH).resolve()
    if not path.is_relative_to(root):
        raise ValueError("synthetic evaluation corpus escaped the repository root")
    raw = path.read_bytes()
    payload = load_unique_yaml(raw.decode("utf-8"))
    corpus = _LiveCorpus.model_validate(payload)
    if plan_version not in {
        "m27-cheapest-first-campaign-v7",
        "m27-cheapest-first-campaign-v8",
        "m27-cheapest-first-campaign-v9",
        "m27-cheapest-first-campaign-v10",
        "m27-cheapest-first-campaign-v11",
    }:
        return corpus, hashlib.sha256(raw).hexdigest()

    holdout_path = (root / _FROZEN_HOLDOUT_RELATIVE_PATH).resolve()
    if not holdout_path.is_relative_to(root):
        raise ValueError("synthetic evaluation holdout escaped the repository root")
    holdout_raw = holdout_path.read_bytes()
    holdout = _LiveHoldout.model_validate(load_unique_yaml(holdout_raw.decode("utf-8")))
    expected_ids = {item.id for item in corpus.core_queries}
    observed_ids = {item.id for item in holdout.core_paraphrases}
    if observed_ids != expected_ids:
        raise ValueError("live holdout does not cover the exact core corpus")
    paraphrases = {item.id: item.paraphrases for item in holdout.core_paraphrases}
    expanded = corpus.model_copy(
        update={
            "core_queries": tuple(
                core.model_copy(update={"paraphrases": paraphrases[core.id]})
                for core in corpus.core_queries
            )
        }
    )
    digest = hashlib.sha256()
    digest.update(_COMPOSITE_CORPUS_DOMAIN)
    digest.update(b"\0")
    digest.update(hashlib.sha256(raw).digest())
    digest.update(b"\0")
    digest.update(hashlib.sha256(holdout_raw).digest())
    return expanded, digest.hexdigest()


def _expected_candidate_case_matrix(
    corpus: _LiveCorpus,
    *,
    repetitions: int,
) -> tuple[tuple[str, str, int, str], ...]:
    identities: list[tuple[str, str, int, str]] = []
    for positive in corpus.positive_mappings:
        identities.extend(
            ("positive", f"{positive.id}:{language.value}", 1, "ranked_at_20")
            for language in (UserLanguage.SPANISH, UserLanguage.ENGLISH)
        )
    identities.extend(
        (
            "negative",
            negative.id,
            1,
            (
                "sensitive_input_blocked"
                if _negative_requires_no_egress(negative.text)
                else "no_match"
            ),
        )
        for negative in corpus.true_negatives
    )
    for ambiguity in corpus.critical_ambiguities:
        identities.extend(
            ("ambiguity", ambiguity.id, repetition, "ambiguous")
            for repetition in range(1, repetitions + 1)
        )
    for core in corpus.core_queries:
        identities.extend(
            ("core", core.id, repetition, "fingerprint_match")
            for repetition in range(1, repetitions + 1)
        )
    identities.extend(
        ("adversarial", adversarial.id, 1, adversarial.expected)
        for adversarial in corpus.adversarial_inputs
    )
    result = tuple(identities)
    if len(result) != len(set(result)):
        raise ValueError("synthetic corpus produced a duplicate evaluation case matrix")
    return result


def _validate_candidate_case_matrix(
    report: QueryStudioCandidateEvaluationReport,
    *,
    corpus: _LiveCorpus,
    corpus_sha256: str,
) -> None:
    if (
        report.corpus_sha256 != corpus_sha256
        or report.metrics.ambiguity_repetitions != report.metrics.core_repetitions
    ):
        raise ValueError("live evaluation report does not match the current corpus")
    expected = _expected_candidate_case_matrix(
        corpus,
        repetitions=report.metrics.ambiguity_repetitions,
    )
    observed = tuple(_case_identity(item) for item in report.case_outcomes)
    if report.planned_case_count != len(expected) or observed != expected[: len(observed)]:
        raise ValueError("live evaluation case matrix does not match the current corpus")


def _validate_qualification_campaign_case_matrices(
    report: NanoFirstQualificationCampaignReport,
    *,
    corpus: _LiveCorpus,
    corpus_sha256: str,
) -> None:
    if report.corpus_sha256 != corpus_sha256:
        raise ValueError("qualification campaign does not match the current corpus")
    positives = {item.id for item in corpus.positive_mappings}
    negatives = {item.id for item in corpus.true_negatives}
    ambiguities = {item.id for item in corpus.critical_ambiguities}
    cores = {item.id for item in corpus.core_queries}
    adversarial = {item.id: item.expected for item in corpus.adversarial_inputs}
    if (
        not all(case_id in positives for case_id, _language in _QUALIFICATION_POSITIVES)
        or _QUALIFICATION_NEGATIVE_ID not in negatives
        or _QUALIFICATION_AMBIGUITY_ID not in ambiguities
        or _QUALIFICATION_CORE_ID not in cores
        or adversarial.get(_QUALIFICATION_ADVERSARIAL_ID) != "sensitive_input_blocked"
    ):
        raise ValueError("qualification case plan is absent from the current corpus")
    for qualification in report.qualification_reports:
        if qualification.corpus_sha256 != corpus_sha256:
            raise ValueError("qualification report does not match the current corpus")
    for record in report.full_evaluations:
        _validate_candidate_case_matrix(
            record.report,
            corpus=corpus,
            corpus_sha256=corpus_sha256,
        )


def _safe_output(root: Path, configured: Path, suffix: str) -> Path:
    candidate = configured if configured.is_absolute() else root / configured
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root) or resolved.suffix != suffix:
        raise ValueError("live evaluation output must remain inside the repository")
    return resolved


__all__ = [
    "NANO_FIRST_MODEL_ORDER",
    "OFFICIAL_STANDARD_PRICING",
    "QUALIFICATION_PLAN_VERSION",
    "EvaluationBudgetLimits",
    "EvaluationCandidate",
    "EvaluationCandidateAuthorization",
    "FingerprintMismatchDiagnostic",
    "LiveEvaluationCaseOutcome",
    "LiveRetrievalMetrics",
    "ModelTokenPricing",
    "NanoFirstEvaluationCampaignReport",
    "NanoFirstQualificationCampaignReport",
    "ProviderEvaluationUsage",
    "ProviderFreeCorePreflightReport",
    "ProviderFreePreflightCaseOutcome",
    "ProviderFreePreflightError",
    "QueryStudioCandidateEvaluationReport",
    "QueryStudioFullEvaluationRecord",
    "QueryStudioQualificationPlan",
    "QueryStudioQualificationReport",
    "calculate_cost_eur",
    "classify_adversarial_without_egress",
    "evaluate_nano_first_query_studio",
    "evaluate_qualified_nano_first_query_studio",
    "external_runtime_admission_is_aligned",
    "load_query_studio_qualification_campaign_report",
    "plan_nano_first_query_studio_qualification",
    "read_bounded_regular_file",
    "reject_symlink_ancestors",
    "require_fresh_query_studio_qualification_history",
    "run_provider_free_core_preflight",
    "validate_openai_sdk_version",
    "write_query_studio_live_report",
    "write_query_studio_qualification_campaign_report",
]
