"""Provider-free contract tests for the M27 v18 semantic-authority boundary."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from schemabridge.adapters.language import openai_query_studio as language
from schemabridge.adapters.language.openai_boundary import (
    OpenAIAdapterError,
    OpenAIAdapterErrorCode,
)
from schemabridge.adapters.query_studio.atomic_preflight import (
    BoundaryScreenedDescriptionExpansionPreflight,
)
from schemabridge.application.guided_requests import (
    BuildGuidedRequest,
    GuidedDimensionInput,
    GuidedFilterInput,
    GuidedMetricInput,
    GuidedOrderInput,
    GuidedRequestInput,
)
from schemabridge.application.ports.query_studio import (
    QueryStudioPortError,
    QueryStudioPortErrorCode,
    QueryStudioRetryDisposition,
)
from schemabridge.application.ports.query_studio_ai_control import (
    AiAdmissionOutcome,
    AiAttemptReservation,
    AiAttemptReservationRequest,
    AiAttemptSettlement,
    AiAttemptSettlementRequest,
    AiAttemptStatus,
    AiSettlementOutcome,
)
from schemabridge.application.query_studio_ai_admission import (
    AdmittedDescriptionExpansion,
    AdmittedQueryStudioIntent,
)
from schemabridge.bootstrap import QueryStudioRuntimeServices, build_query_studio_runtime
from schemabridge.config import Settings
from schemabridge.domain.concepts import CanonicalType, LogicalFieldRef
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.query_studio import (
    MAX_PROMPT_FIELDS,
    ApprovedPublicMetadataSurface,
    DescriptionExpansion,
    DescriptionExpansionInput,
    DescriptionExpansionResult,
    DescriptionQuery,
    DescriptionSearchProbe,
    OpaqueCandidateId,
    ProviderConfigurationFacts,
    ProviderOutcomeCode,
    ProviderOutputFailureCategory,
    ProviderStage,
    ProviderUsageFacts,
    QueryFieldPurpose,
    QueryStudioConfirmation,
    QueryStudioConfirmationAction,
    QueryStudioInterpretationInput,
    QueryStudioInterpretationResult,
    QueryStudioPreview,
    QueryStudioPromptPurposeScore,
    QueryStudioPromptVocabulary,
    SemanticMatchState,
    find_explicit_logical_field_references,
    find_malformed_qualified_paths,
    governed_probe_search_requests,
)
from schemabridge.domain.query_studio_proposals import (
    canonicalize_query_studio_proposal,
)
from schemabridge.domain.request_context import (
    LogicalFieldRole,
    ValidatedAnalyticalRequest,
    validated_analytical_request_fingerprint,
)
from schemabridge.domain.requests import FilterOperator, MetricOperation
from schemabridge.domain.semantic_registry import SemanticRegistryScope

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 27, 15, 0, tzinfo=UTC)
MATCHING_FIXTURE = ROOT / "demo/ground_truth/query_studio_matching.yml"
HOLDOUT_FIXTURE = ROOT / "demo/ground_truth/query_studio_live_holdout_v1.yml"
_OWNER_FOCUS_BY_MODEL = {
    "Customer": ("customer",),
    "AccountHolder": ("account", "holder"),
    "Product": ("product",),
    "SalesOrder": ("order",),
    "Shipment": ("shipment",),
}


@dataclass(frozen=True, slots=True)
class _Oracle:
    request: GuidedRequestInput
    fields_by_slot: dict[str, str]
    filter_focus: str | None = None


@dataclass(slots=True)
class _LocalAnalyticalExpansion:
    configuration: ProviderConfigurationFacts
    calls: list[DescriptionExpansionInput] = field(default_factory=list)

    def expand(self, value: DescriptionExpansionInput) -> DescriptionExpansionResult:
        self.calls.append(value)
        return DescriptionExpansionResult(
            expansion=language.local_analytical_description_expansion(value),
            usage=None,
        )


@dataclass(slots=True)
class _OptionOneIntent:
    configuration: ProviderConfigurationFacts
    calls: list[QueryStudioInterpretationInput] = field(default_factory=list)

    def interpret(
        self,
        value: QueryStudioInterpretationInput,
    ) -> QueryStudioInterpretationResult:
        self.calls.append(value)
        return _option_one_result(value, self.configuration)


@dataclass(slots=True)
class _NeverProviderExpansion:
    configuration: ProviderConfigurationFacts
    calls: int = 0

    def expand(self, value: DescriptionExpansionInput) -> DescriptionExpansionResult:
        del value
        self.calls += 1
        raise AssertionError("local analytical expansion must not call a provider delegate")


@dataclass(slots=True)
class _RetryOnceOptionOneIntent:
    configuration: ProviderConfigurationFacts
    calls: int = 0

    def interpret(
        self,
        value: QueryStudioInterpretationInput,
    ) -> QueryStudioInterpretationResult:
        self.calls += 1
        if self.calls == 1:
            raise QueryStudioPortError(
                QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
                "synthetic retryable interpretation failure",
                retry_disposition=QueryStudioRetryDisposition.RETRY_ONCE,
                output_failure_category=ProviderOutputFailureCategory.SEMANTIC_CONTRACT,
            )
        return _option_one_result(value, self.configuration, tokens=1)


@dataclass(slots=True)
class _Nonces:
    index: int = 0

    def new_nonce(self) -> str:
        self.index += 1
        return f"v18_contract_nonce_{self.index:04d}_" + "x" * 32


@dataclass(slots=True)
class _AttemptControl:
    configuration: ProviderConfigurationFacts
    reservations: list[AiAttemptReservationRequest] = field(default_factory=list)
    settlements: list[AiAttemptSettlementRequest] = field(default_factory=list)

    def reserve(self, request: AiAttemptReservationRequest) -> AiAttemptReservation:
        self.reservations.append(request)
        index = len(self.reservations)
        return AiAttemptReservation(
            outcome=AiAdmissionOutcome.RESERVED,
            replayed=False,
            reservation_id=f"air_{index:064x}",
            status=AiAttemptStatus.RESERVED,
            fencing_token=index,
            lease_expires_at=NOW + timedelta(minutes=1),
            policy_version=1,
            model_snapshot=self.configuration.model_snapshot,
            endpoint_region=self.configuration.endpoint_region,
            configuration_fingerprint=self.configuration.fingerprint,
        )

    def settle(self, request: AiAttemptSettlementRequest) -> AiAttemptSettlement:
        self.settlements.append(request)
        return AiAttemptSettlement(
            reservation_id=request.reservation_id,
            replayed=False,
            status=AiAttemptStatus.SETTLED,
            outcome=request.outcome,
            charged_input_tokens=(
                request.observed_input_tokens
                if request.observed_input_tokens is not None
                else request.reserved_input_tokens
            ),
            charged_output_tokens=(
                request.observed_output_tokens
                if request.observed_output_tokens is not None
                else request.reserved_output_tokens
            ),
            settled_at=NOW,
            audit_id=f"aia_{len(self.settlements):064x}",
        )

    def expire(self, workspace_id: str, *, limit: int = 100) -> int:
        del workspace_id, limit
        return 0


def test_v18_originals_and_holdouts_are_exact_against_the_independent_oracle() -> None:
    runtime, expansion, intent = _contract_runtime()
    assert runtime.prepare_natural is not None
    assert runtime.confirm_natural is not None
    builder = BuildGuidedRequest(runtime.prepare_natural.registry)
    variants = _fixture_variants()

    assert len(variants) == 15
    for case_id, text, language_code in variants:
        oracle = _ORACLES[case_id]
        expected = builder.execute(oracle.request)
        preview = runtime.prepare_natural.execute(text, UserLanguage(language_code))

        assert preview.semantic_state is SemanticMatchState.ALIGNED, (case_id, text)
        assert preview.operational_state is None
        assert preview.expansion is not None
        assert preview.vocabulary is not None
        assert preview.proposal is not None
        assert preview.token is not None
        assert tuple(item.stage for item in preview.provider_usage) == (
            ProviderStage.INTERPRETATION,
        )
        _assert_selected_fields(preview, oracle)
        _assert_server_focus(preview, oracle, runtime.scope)

        confirmed = runtime.confirm_natural.execute(
            QueryStudioConfirmation(
                original_text=DescriptionQuery(text),
                language=UserLanguage(language_code),
                expansion=preview.expansion,
                proposal=preview.proposal,
                token=preview.token,
                action=QueryStudioConfirmationAction.CONFIRM_INTERPRETATION,
            )
        )
        assert _request_fingerprint(confirmed.validated_request) == _request_fingerprint(expected)

    assert len(expansion.calls) == 15
    assert len(intent.calls) == 15


def test_v18_incompatible_candidate_is_removed_before_score_or_tie_authority() -> None:
    _runtime_value, _expansion, intent = _captured_contract_case(
        "delivered-orders-by-day",
    )
    value = intent.calls[-1]
    filter_probe = next(probe for probe in value.expansion.probes if probe.purpose_id == "filter_1")
    correct = next(
        candidate
        for candidate in value.vocabulary.candidates
        if candidate.logical_field.root == "Shipment.shipment_status"
    )
    incompatible = correct.model_copy(
        update={
            "candidate_id": OpaqueCandidateId("qsc1_" + "f" * 64),
            "logical_field": LogicalFieldRef("Shipment.delivered_at"),
            "definition": "Synthetic temporal decoy ranked above the status field.",
            "canonical_type": CanonicalType.TIMESTAMP,
            "role": LogicalFieldRole.TEMPORAL,
            "score": correct.score + 100_000,
            "purpose_ids": (filter_probe.purpose_id,),
        }
    )
    mutated_vocabulary = value.vocabulary.model_copy(
        update={
            "candidates": (
                incompatible,
                *value.vocabulary.candidates,
            )
        }
    )
    mutated = value.model_copy(
        update={
            "vocabulary": mutated_vocabulary,
            "public_metadata_surface": None,
        }
    )

    options = language._selection_options_by_slot(mutated)

    assert tuple(
        candidate.logical_field.root for candidate in options[filter_probe.purpose_id]
    ) == ("Shipment.shipment_status",)


def test_v18_option_one_and_score_follow_each_slot_specific_ranking() -> None:
    _runtime_value, _expansion, intent = _captured_contract_case(
        "active-customers-by-country",
    )
    value = intent.calls[-1]
    country = next(
        candidate
        for candidate in value.vocabulary.candidates
        if candidate.logical_field.root == "Customer.country_code"
    )
    status = next(
        candidate
        for candidate in value.vocabulary.candidates
        if candidate.logical_field.root == "Customer.customer_status"
    )
    purpose_ids = ("dimension_1", "dimension_2")
    country = country.model_copy(
        update={
            "intended_uses": (QueryFieldPurpose.DIMENSION,),
            "purpose_ids": purpose_ids,
            "purpose_scores": (
                QueryStudioPromptPurposeScore(purpose_id="dimension_1", score=900),
                QueryStudioPromptPurposeScore(purpose_id="dimension_2", score=100),
            ),
            "score": 900,
        }
    )
    status = status.model_copy(
        update={
            "intended_uses": (QueryFieldPurpose.DIMENSION,),
            "purpose_ids": purpose_ids,
            "purpose_scores": (
                QueryStudioPromptPurposeScore(purpose_id="dimension_1", score=100),
                QueryStudioPromptPurposeScore(purpose_id="dimension_2", score=900),
            ),
            "score": 10_000,
        }
    )
    expansion = DescriptionExpansion(
        probes=tuple(
            DescriptionSearchProbe(
                purpose_id=purpose_id,
                query=DescriptionQuery("customer attribute"),
                intended_use=QueryFieldPurpose.DIMENSION,
                semantic_focus=("customer", "attribute"),
                owner_focus=("customer",),
                roles=(LogicalFieldRole.ATTRIBUTE,),
                canonical_types=(CanonicalType.STRING,),
            )
            for purpose_id in purpose_ids
        )
    )
    mutated = value.model_copy(
        update={
            "expansion": expansion,
            "vocabulary": value.vocabulary.model_copy(update={"candidates": (status, country)}),
            "public_metadata_surface": None,
        }
    )

    options = language._selection_options_by_slot(mutated)

    assert options["dimension_1"][0].logical_field.root == "Customer.country_code"
    assert options["dimension_2"][0].logical_field.root == "Customer.customer_status"
    assert language._candidate_score_for_slot(options["dimension_1"][0], "dimension_1") == 900
    assert language._candidate_score_for_slot(options["dimension_2"][0], "dimension_2") == 900


def test_v18_old_wrong_owner_option_one_requests_still_miss_the_oracle_fingerprint() -> None:
    runtime = _base_runtime()
    assert runtime.prepare_natural is not None
    builder = BuildGuidedRequest(runtime.prepare_natural.registry)

    for case_id, wrong in _WRONG_OWNER_REQUESTS.items():
        expected = builder.execute(_ORACLES[case_id].request)
        observed = builder.execute(wrong)
        assert _request_fingerprint(observed) != _request_fingerprint(expected), case_id


@pytest.mark.parametrize(
    "mutation",
    ("unknown", "duplicate", "partial", "mixed_ambiguity"),
)
def test_v18_unknown_duplicate_partial_or_mixed_selections_fail_semantic_contract(
    mutation: str,
) -> None:
    _runtime_value, _expansion, intent = _captured_contract_case(
        "secondary-holders-by-registration-date",
    )
    value = intent.calls[-1]
    valid = [{"slot_id": probe.purpose_id, "option_index": 1} for probe in value.expansion.probes]
    ambiguity_kinds: list[str] = []
    if mutation == "unknown":
        valid.append({"slot_id": "filter_6", "option_index": 1})
    elif mutation == "duplicate":
        valid.append(valid[0])
    elif mutation == "partial":
        valid.pop()
    else:
        ambiguity_kinds.append("field_meaning")
    parsed = language._OpenAIQueryStudioSelections.model_validate(
        {
            "selections": valid,
            "ambiguity_kinds": ambiguity_kinds,
        }
    )

    with pytest.raises(QueryStudioPortError) as raised:
        language._reconstruct_proposal(parsed, value)

    assert raised.value.code is QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT
    assert raised.value.output_failure_category is ProviderOutputFailureCategory.SEMANTIC_CONTRACT


@pytest.mark.parametrize(
    "forbidden",
    (
        {"sql": "SELECT * FROM private_table"},
        {"candidate_id": "qsc1_" + "a" * 64},
        {"value": "SECONDARY"},
        {"logical_field": "Customer.customer_key"},
    ),
)
def test_v18_provider_selection_schema_forbids_sql_ids_and_values(
    forbidden: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        language._OpenAIQueryStudioSelections.model_validate(
            {
                "selections": (
                    {
                        "slot_id": "metric_1",
                        "option_index": 1,
                        **forbidden,
                    },
                ),
                "ambiguity_kinds": (),
            }
        )


def test_v18_cross_scope_public_metadata_fails_before_provider_serialization() -> None:
    _runtime_value, _expansion, intent = _captured_contract_case(
        "active-products-by-category",
    )
    value = intent.calls[-1]
    assert value.public_metadata_surface is not None
    surface = value.public_metadata_surface
    expected_configuration = _configuration_for_surface(intent.configuration, surface)
    foreign_surface = ApprovedPublicMetadataSurface.create(
        policy_fingerprint=surface.policy_fingerprint,
        semantic_scope_fingerprint="f" * 64,
        registry_fingerprint=surface.registry_fingerprint,
        vocabulary_fingerprint=surface.vocabulary_fingerprint,
    )
    foreign = value.model_copy(update={"public_metadata_surface": foreign_surface})

    with pytest.raises(OpenAIAdapterError) as raised:
        language._interpretation_provider_input(
            foreign,
            configuration=expected_configuration,
        )

    assert raised.value.code is OpenAIAdapterErrorCode.METADATA_NOT_PUBLIC


def test_v18_prompt_field_overflow_is_rejected_by_the_typed_boundary() -> None:
    _runtime_value, _expansion, intent = _captured_contract_case(
        "active-customers-by-country",
    )
    vocabulary = intent.calls[-1].vocabulary
    template = vocabulary.candidates[0]
    candidates = tuple(
        template.model_copy(
            update={
                "candidate_id": OpaqueCandidateId(f"qsc1_{index:064x}"),
                "logical_field": LogicalFieldRef(f"Customer.synthetic_{index:02d}"),
                "definition": f"Synthetic overflow candidate {index}.",
                "purpose_ids": ("metric_1",),
            }
        )
        for index in range(MAX_PROMPT_FIELDS + 1)
    )
    payload = vocabulary.model_dump(mode="python")
    payload["candidates"] = candidates

    with pytest.raises(ValidationError):
        QueryStudioPromptVocabulary.model_validate(payload)


def test_v18_only_interpretation_can_reserve_and_retry() -> None:
    runtime, _expansion, seed_intent = _captured_contract_case(
        "active-products-by-category",
    )
    value = seed_intent.calls[-1]
    configuration = _external_configuration(runtime.configuration)
    control = _AttemptControl(configuration)
    nonces = _Nonces()
    provider_expansion = _NeverProviderExpansion(configuration)
    admitted_expansion = AdmittedDescriptionExpansion(
        delegate=provider_expansion,
        preflight=BoundaryScreenedDescriptionExpansionPreflight(),
        control=control,
        nonces=nonces,
        workspace_id="v18-contract-workspace",
        actor_digest="c" * 64,
        semantic_scope_fingerprint="d" * 64,
        configuration=configuration,
        estimated_input_tokens=100,
        estimated_output_tokens=20,
    )
    local = admitted_expansion.expand(
        DescriptionExpansionInput(
            text=value.text,
            language=value.language,
        )
    )

    assert local.usage is None
    assert provider_expansion.calls == 0
    assert control.reservations == []
    assert control.settlements == []

    retrying = _RetryOnceOptionOneIntent(configuration)
    admitted_intent = AdmittedQueryStudioIntent(
        delegate=retrying,
        control=control,
        nonces=nonces,
        workspace_id="v18-contract-workspace",
        actor_digest="c" * 64,
        semantic_scope_fingerprint="d" * 64,
        configuration=configuration,
        estimated_input_tokens=100,
        estimated_output_tokens=20,
    )
    result = admitted_intent.interpret(value)

    assert result.proposal.semantic_state is SemanticMatchState.ALIGNED
    assert retrying.calls == 2
    assert [request.stage for request in control.reservations] == [
        ProviderStage.INTERPRETATION,
        ProviderStage.INTERPRETATION,
    ]
    assert [request.attempt_number for request in control.reservations] == [1, 2]
    assert [request.outcome for request in control.settlements] == [
        AiSettlementOutcome.INVALID_OUTPUT,
        AiSettlementOutcome.SUCCEEDED,
    ]


@pytest.mark.parametrize(
    ("text", "expected_query", "expected_span", "expected_owner"),
    (
        (
            "agrupa por Customer.secret_credit_score",
            "customer.secret_credit_score",
            "Customer.secret_credit_score",
            ("customer",),
        ),
        (
            "agrupa por UnknownModel.secret_credit_score",
            "unknownmodel.secret_credit_score",
            "UnknownModel.secret_credit_score",
            ("unknownmodel",),
        ),
    ),
)
def test_v18_unknown_qualified_field_is_exact_no_match_before_ai_admission(
    text: str,
    expected_query: str,
    expected_span: str,
    expected_owner: tuple[str, ...],
) -> None:
    runtime = _base_runtime()
    assert runtime.prepare_natural is not None
    configuration = _external_configuration(runtime.configuration)
    expansion = _LocalAnalyticalExpansion(configuration)
    delegate = _RetryOnceOptionOneIntent(configuration)
    control = _AttemptControl(configuration)
    admitted = AdmittedQueryStudioIntent(
        delegate=delegate,
        control=control,
        nonces=_Nonces(),
        workspace_id="v18-contract-workspace",
        actor_digest="c" * 64,
        semantic_scope_fingerprint="d" * 64,
        configuration=configuration,
        estimated_input_tokens=100,
        estimated_output_tokens=20,
    )
    prepare = replace(
        runtime.prepare_natural,
        expansion=expansion,
        interpreter=admitted,
        configuration=configuration,
    )

    preview = prepare.execute(text, UserLanguage.SPANISH)

    assert preview.semantic_state is SemanticMatchState.NO_MATCH
    assert preview.operational_state is None
    assert preview.reason_code is None
    assert preview.expansion is not None
    dimension = next(
        probe for probe in preview.expansion.probes if probe.purpose_id == "dimension_1"
    )
    assert dimension.query.root == expected_query
    assert dimension.source_span == expected_span
    assert dimension.owner_focus == expected_owner
    assert preview.shortlist is not None
    assert preview.shortlist.candidates == ()
    assert preview.vocabulary is None
    assert preview.proposal is None
    assert preview.token is None
    assert preview.provider_usage == ()
    assert len(expansion.calls) == 1
    assert delegate.calls == 0
    assert control.reservations == []
    assert control.settlements == []


def test_v18_unknown_qualified_cross_owner_fields_never_invent_a_join() -> None:
    runtime, expansion, intent = _contract_runtime()
    assert runtime.prepare_natural is not None

    preview = runtime.prepare_natural.execute(
        "agrupa por Customer.secret_credit_score y Account.fake_join_key",
        UserLanguage.SPANISH,
    )

    assert preview.semantic_state is SemanticMatchState.NO_MATCH
    assert preview.operational_state is None
    assert preview.expansion is not None
    dimensions = tuple(
        probe
        for probe in preview.expansion.probes
        if probe.intended_use is QueryFieldPurpose.DIMENSION
    )
    assert tuple(probe.query.root for probe in dimensions) == (
        "customer.secret_credit_score",
        "account.fake_join_key",
    )
    assert tuple(probe.owner_focus for probe in dimensions) == (
        ("customer",),
        ("account",),
    )
    assert preview.vocabulary is None
    assert preview.proposal is None
    assert preview.provider_usage == ()
    assert len(expansion.calls) == 1
    assert intent.calls == []


@pytest.mark.parametrize(
    "text",
    (
        "count Customer.secret_credit_score by Customer.registration_date",
        (
            "count customers by Customer.registration_date "
            "where UnknownModel.secret_credit_score is active"
        ),
        (
            "count Customer.secret_credit_score by Customer.registration_date "
            "where UnknownModel.private_flag is active"
        ),
    ),
)
def test_v18_every_unknown_qualified_reference_blocks_rewritten_slots_before_intent(
    text: str,
) -> None:
    runtime, expansion, intent = _contract_runtime()
    assert runtime.prepare_natural is not None

    preview = runtime.prepare_natural.execute(text, UserLanguage.ENGLISH)

    assert preview.semantic_state is SemanticMatchState.NO_MATCH
    assert preview.operational_state is None
    assert preview.shortlist is not None
    assert preview.shortlist.candidates == ()
    assert preview.vocabulary is None
    assert preview.proposal is None
    assert preview.token is None
    assert preview.provider_usage == ()
    assert len(expansion.calls) == 1
    assert intent.calls == []


def test_v18_qualified_reference_parser_is_bounded_and_source_ordered() -> None:
    source = (
        "count Customer.customer_key by Customer.registration_date "
        "where UnknownModel.private_flag is active"
    )

    references = find_explicit_logical_field_references(source)

    assert tuple(reference.value for reference in references) == (
        "customer.customer_key",
        "customer.registration_date",
        "unknownmodel.private_flag",
    )
    assert tuple(source[item.start : item.end] for item in references) == (
        "Customer.customer_key",
        "Customer.registration_date",
        "UnknownModel.private_flag",
    )
    assert find_explicit_logical_field_references("db.Customer.customer_key") == ()
    malformed = find_malformed_qualified_paths("db.Customer.customer_key")
    assert tuple(path.value for path in malformed) == ("db.customer.customer_key",)
    assert malformed[0].segments == ("db", "customer", "customer_key")
    assert find_explicit_logical_field_references(f"{'a' * 41}.field_name") == ()
    assert find_malformed_qualified_paths(f"{'a' * 41}.field_name")
    assert find_explicit_logical_field_references(f"Customer.{'a' * 81}") == ()
    assert find_malformed_qualified_paths(f"Customer.{'a' * 81}")


@pytest.mark.parametrize(
    "text",
    (
        "count Customer.customer_key by Customer.registration_date",
        ("count customers by Customer.registration_date where Customer.customer_status is active"),
    ),
)
def test_v18_all_valid_qualified_references_continue_through_normal_closure(
    text: str,
) -> None:
    runtime, expansion, intent = _contract_runtime()
    assert runtime.prepare_natural is not None

    preview = runtime.prepare_natural.execute(text, UserLanguage.ENGLISH)

    assert preview.semantic_state is SemanticMatchState.ALIGNED
    assert preview.operational_state is None
    assert preview.shortlist is not None
    assert preview.proposal is not None
    assert preview.token is not None
    assert len(expansion.calls) == 1
    assert len(intent.calls) == 1


@pytest.mark.parametrize(
    "text",
    (
        "count db.Customer.customer_key by Customer.registration_date",
        (
            "count customers by Customer.registration_date "
            "where db.Customer.customer_status is active"
        ),
        ("count warehouse.analytics.Customer.customer_key by Customer.registration_date"),
        (
            "count customers by Customer.registration_date "
            "where warehouse.analytics.Customer.customer_status is active"
        ),
    ),
)
def test_v18_three_or_more_segment_paths_fail_closed_before_intent(
    text: str,
) -> None:
    runtime, expansion, intent = _contract_runtime()
    assert runtime.prepare_natural is not None

    preview = runtime.prepare_natural.execute(text, UserLanguage.ENGLISH)

    assert preview.semantic_state is SemanticMatchState.NO_MATCH
    assert preview.operational_state is None
    assert preview.shortlist is not None
    assert preview.shortlist.candidates == ()
    assert preview.vocabulary is None
    assert preview.proposal is None
    assert preview.token is None
    assert preview.provider_usage == ()
    assert len(expansion.calls) == 1
    assert intent.calls == []


@pytest.mark.parametrize(
    "text",
    (
        "agrupa por Customer.secret_credit_score; DROP TABLE customers",
        "agrupa por Customer.secret_credit_score /* JOIN Account.account_key */",
        "agrupa por Customer.secret_credit_score -- use this",
    ),
)
def test_v18_malicious_qualified_field_payload_fails_closed_before_ai(
    text: str,
) -> None:
    runtime = _base_runtime()
    assert runtime.prepare_natural is not None
    intent = _OptionOneIntent(runtime.configuration)
    prepare = replace(
        runtime.prepare_natural,
        expansion=BoundaryScreenedDescriptionExpansionPreflight(),
        interpreter=intent,
    )

    preview = prepare.execute(text, UserLanguage.SPANISH)

    assert preview.semantic_state is None
    assert preview.operational_state is not None
    assert preview.vocabulary is None
    assert preview.proposal is None
    assert preview.provider_usage == ()
    assert intent.calls == []


def test_v18_known_qualified_field_keeps_exact_lexical_probe_and_executes_normally() -> None:
    runtime, expansion, intent = _contract_runtime()
    assert runtime.prepare_natural is not None

    preview = runtime.prepare_natural.execute(
        "agrupa por Customer.registration_date",
        UserLanguage.SPANISH,
    )

    assert preview.semantic_state is SemanticMatchState.ALIGNED
    assert preview.expansion is not None
    dimension = next(
        probe for probe in preview.expansion.probes if probe.purpose_id == "dimension_1"
    )
    assert dimension.query.root == "customer.registration_date"
    assert dimension.source_span == "Customer.registration_date"
    assert dimension.owner_focus == ("customer",)
    assert preview.proposal is not None
    assert len(expansion.calls) == 1
    assert len(intent.calls) == 1


def _base_runtime() -> QueryStudioRuntimeServices:
    principal = AuthenticatedPrincipal(
        actor_id="v18-contract-analyst",
        workspace_id="query-studio-deterministic-evaluation",
        roles=frozenset({IdentityRole.ANALYST}),
        authentication_method=AuthenticationMethod.LOCAL_DEMO,
        authenticated_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    return build_query_studio_runtime(
        principal=principal,
        repository_root=ROOT,
        settings=Settings(
            _env_file=None,
            OPENAI_API_KEY=None,
            DATABASE_URL=None,
            SCHEMABRIDGE_QUERY_STUDIO_AI_MODE="fake",
            SCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY=(
                "v18-contract-signing-key-with-byte-diversity-2026"
            ),
        ),
    )


def _contract_runtime() -> tuple[
    QueryStudioRuntimeServices,
    _LocalAnalyticalExpansion,
    _OptionOneIntent,
]:
    runtime = _base_runtime()
    assert runtime.prepare_natural is not None
    assert runtime.confirm_natural is not None
    expansion = _LocalAnalyticalExpansion(runtime.configuration)
    intent = _OptionOneIntent(runtime.configuration)
    prepare = replace(
        runtime.prepare_natural,
        expansion=expansion,
        interpreter=intent,
    )
    return replace(runtime, prepare_natural=prepare), expansion, intent


def _captured_contract_case(
    case_id: str,
) -> tuple[QueryStudioRuntimeServices, _LocalAnalyticalExpansion, _OptionOneIntent]:
    runtime, expansion, intent = _contract_runtime()
    text, language_code = next(
        (text, language)
        for observed_id, text, language in _fixture_variants()
        if observed_id == case_id
    )
    assert runtime.prepare_natural is not None
    preview = runtime.prepare_natural.execute(text, UserLanguage(language_code))
    assert preview.semantic_state is SemanticMatchState.ALIGNED
    assert len(intent.calls) == 1
    return runtime, expansion, intent


def _option_one_result(
    value: QueryStudioInterpretationInput,
    configuration: ProviderConfigurationFacts,
    *,
    tokens: int = 0,
) -> QueryStudioInterpretationResult:
    parsed = language._OpenAIQueryStudioSelections.model_validate(
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
    proposal = language._reconstruct_proposal(parsed, value)
    language._validate_proposal_is_closed(proposal, value)
    proposal = canonicalize_query_studio_proposal(proposal, value)
    language._validate_proposal_is_closed(proposal, value)
    return QueryStudioInterpretationResult(
        proposal=proposal,
        usage=ProviderUsageFacts(
            stage=ProviderStage.INTERPRETATION,
            model_snapshot=configuration.model_snapshot,
            configuration_fingerprint=configuration.fingerprint,
            input_tokens=tokens,
            output_tokens=tokens,
            duration_ms=0,
            outcome=ProviderOutcomeCode.SUCCEEDED,
        ),
    )


def _assert_selected_fields(preview: QueryStudioPreview, oracle: _Oracle) -> None:
    proposal = preview.proposal
    vocabulary = preview.vocabulary
    assert proposal is not None
    assert vocabulary is not None
    by_id = {
        candidate.candidate_id.root: candidate.logical_field.root
        for candidate in vocabulary.candidates
    }
    assert proposal.primary_candidate_id is not None
    assert by_id[proposal.primary_candidate_id.root] == oracle.request.metrics[0].field
    assert tuple(by_id[item.candidate_id.root] for item in proposal.dimensions) == tuple(
        item.field for item in oracle.request.dimensions
    )
    assert tuple(by_id[item.candidate_id.root] for item in proposal.metrics) == tuple(
        item.field for item in oracle.request.metrics
    )
    assert tuple(by_id[item.candidate_id.root] for item in proposal.filters) == tuple(
        item.field for item in oracle.request.filters
    )


def _assert_server_focus(
    preview: QueryStudioPreview,
    oracle: _Oracle,
    scope: SemanticRegistryScope,
) -> None:
    expansion = preview.expansion
    assert expansion is not None
    for probe in expansion.probes:
        expected_field = oracle.fields_by_slot[probe.purpose_id]
        expected_model = expected_field.split(".", 1)[0]
        semantic_focus = probe.semantic_focus
        owner_focus = probe.owner_focus
        assert semantic_focus
        expected_owner_focus = _OWNER_FOCUS_BY_MODEL.get(expected_model)
        if expected_owner_focus is not None:
            assert owner_focus == expected_owner_focus
        if probe.purpose_id.startswith("filter_"):
            assert oracle.filter_focus is not None
            assert oracle.filter_focus in semantic_focus
            requests = governed_probe_search_requests(scope, probe)
            assert any(
                request.query is not None and oracle.filter_focus in _words(request.query.root)
                for request in requests
            )


def _configuration_for_surface(
    source: ProviderConfigurationFacts,
    surface: ApprovedPublicMetadataSurface,
) -> ProviderConfigurationFacts:
    payload = source.model_dump(mode="python", exclude={"fingerprint"})
    payload.update(
        {
            "public_metadata_policy_fingerprint": surface.policy_fingerprint,
            "public_metadata_semantic_scope_fingerprint": (surface.semantic_scope_fingerprint),
            "public_metadata_registry_fingerprint": surface.registry_fingerprint,
        }
    )
    return ProviderConfigurationFacts.create(**payload)


def _external_configuration(
    source: ProviderConfigurationFacts,
) -> ProviderConfigurationFacts:
    return ProviderConfigurationFacts.create(
        adapter="openai_responses_structured",
        model_snapshot="gpt-5-nano-2025-08-07",
        reasoning_effort="minimal",
        endpoint_region="eu",
        prompt_version="m27-v18-contract-test",
        schema_version="m27-v18-contract-test",
        matcher_version=source.matcher_version,
        orchestration_policy_version=source.orchestration_policy_version,
        attempt_policy_version=source.attempt_policy_version,
        external_ai=True,
    )


def _fixture_variants() -> tuple[tuple[str, str, str], ...]:
    matching = yaml.safe_load(MATCHING_FIXTURE.read_text(encoding="utf-8"))
    holdout = yaml.safe_load(HOLDOUT_FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(matching, dict)
    assert isinstance(holdout, dict)
    core = matching["core_queries"]
    holdout_by_id = {item["id"]: item["paraphrases"] for item in holdout["core_paraphrases"]}
    variants: list[tuple[str, str, str]] = []
    for item in core:
        case_id = item["id"]
        variants.append((case_id, item["text"], item["language"]))
        variants.extend(
            (case_id, paraphrase["text"], paraphrase["language"])
            for paraphrase in holdout_by_id[case_id]
        )
    assert set(holdout_by_id) == set(_ORACLES)
    assert {item[0] for item in variants} == set(_ORACLES)
    return tuple(variants)


def _request_fingerprint(value: ValidatedAnalyticalRequest) -> str:
    return validated_analytical_request_fingerprint(value)


def _words(value: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+", value.casefold()))


_ORACLES = {
    "secondary-holders-by-registration-date": _Oracle(
        request=GuidedRequestInput(
            primary_entity="Customer",
            dimensions=(
                GuidedDimensionInput(
                    field="Customer.registration_date",
                    grain="day",
                ),
            ),
            metrics=(
                GuidedMetricInput(
                    operation=MetricOperation.COUNT_DISTINCT.value,
                    field="Customer.customer_key",
                ),
            ),
            filters=(
                GuidedFilterInput(
                    field="AccountHolder.holder_role",
                    operator=FilterOperator.EQUALS.value,
                    value="SECONDARY",
                ),
            ),
            order_by=(GuidedOrderInput(field="Customer.registration_date"),),
            limit=500,
        ),
        fields_by_slot={
            "dimension_1": "Customer.registration_date",
            "metric_1": "Customer.customer_key",
            "filter_1": "AccountHolder.holder_role",
        },
        filter_focus="secondary",
    ),
    "active-customers-by-country": _Oracle(
        request=GuidedRequestInput(
            primary_entity="Customer",
            dimensions=(GuidedDimensionInput(field="Customer.country_code"),),
            metrics=(
                GuidedMetricInput(
                    operation=MetricOperation.COUNT_DISTINCT.value,
                    field="Customer.customer_key",
                ),
            ),
            filters=(
                GuidedFilterInput(
                    field="Customer.customer_status",
                    operator=FilterOperator.EQUALS.value,
                    value="ACTIVE",
                ),
            ),
            order_by=(GuidedOrderInput(field="Customer.country_code"),),
            limit=500,
        ),
        fields_by_slot={
            "dimension_1": "Customer.country_code",
            "metric_1": "Customer.customer_key",
            "filter_1": "Customer.customer_status",
        },
        filter_focus="active",
    ),
    "active-products-by-category": _Oracle(
        request=GuidedRequestInput(
            primary_entity="Product",
            dimensions=(GuidedDimensionInput(field="Product.category"),),
            metrics=(
                GuidedMetricInput(
                    operation=MetricOperation.COUNT_DISTINCT.value,
                    field="Product.product_key",
                ),
            ),
            filters=(
                GuidedFilterInput(
                    field="Product.is_active",
                    operator=FilterOperator.EQUALS.value,
                    value=True,
                ),
            ),
            order_by=(GuidedOrderInput(field="Product.category"),),
            limit=500,
        ),
        fields_by_slot={
            "dimension_1": "Product.category",
            "metric_1": "Product.product_key",
            "filter_1": "Product.is_active",
        },
        filter_focus="active",
    ),
    "delivered-orders-by-day": _Oracle(
        request=GuidedRequestInput(
            primary_entity="SalesOrder",
            dimensions=(
                GuidedDimensionInput(
                    field="Shipment.delivered_at",
                    grain="day",
                ),
            ),
            metrics=(
                GuidedMetricInput(
                    operation=MetricOperation.COUNT_DISTINCT.value,
                    field="SalesOrder.order_key",
                ),
            ),
            filters=(
                GuidedFilterInput(
                    field="Shipment.shipment_status",
                    operator=FilterOperator.EQUALS.value,
                    value="DELIVERED",
                ),
            ),
            order_by=(GuidedOrderInput(field="Shipment.delivered_at"),),
            limit=500,
        ),
        fields_by_slot={
            "dimension_1": "Shipment.delivered_at",
            "metric_1": "SalesOrder.order_key",
            "filter_1": "Shipment.shipment_status",
        },
        filter_focus="delivery",
    ),
    "revenue-by-order-date-and-category": _Oracle(
        request=GuidedRequestInput(
            primary_entity="SaleLine",
            dimensions=(
                GuidedDimensionInput(
                    field="SalesOrder.ordered_at",
                    grain="day",
                ),
                GuidedDimensionInput(field="Product.category"),
            ),
            metrics=(
                GuidedMetricInput(
                    operation=MetricOperation.SUM.value,
                    field="SaleLine.net_amount",
                ),
            ),
            order_by=(
                GuidedOrderInput(field="SalesOrder.ordered_at"),
                GuidedOrderInput(field="Product.category"),
            ),
            limit=500,
        ),
        fields_by_slot={
            "dimension_1": "SalesOrder.ordered_at",
            "dimension_2": "Product.category",
            "metric_1": "SaleLine.net_amount",
        },
    ),
}


_WRONG_OWNER_REQUESTS = {
    "secondary-holders-by-registration-date": GuidedRequestInput(
        primary_entity="AccountHolder",
        dimensions=(
            GuidedDimensionInput(
                field="Customer.registration_date",
                grain="day",
            ),
        ),
        metrics=(
            GuidedMetricInput(
                operation=MetricOperation.COUNT_DISTINCT.value,
                field="AccountHolder.customer_key",
            ),
        ),
        filters=(
            GuidedFilterInput(
                field="AccountHolder.holder_role",
                operator=FilterOperator.EQUALS.value,
                value="SECONDARY",
            ),
        ),
        order_by=(GuidedOrderInput(field="Customer.registration_date"),),
        limit=500,
    ),
    "active-customers-by-country": GuidedRequestInput(
        primary_entity="AccountHolder",
        dimensions=(GuidedDimensionInput(field="Customer.country_code"),),
        metrics=(
            GuidedMetricInput(
                operation=MetricOperation.COUNT_DISTINCT.value,
                field="AccountHolder.customer_key",
            ),
        ),
        filters=(
            GuidedFilterInput(
                field="Customer.customer_status",
                operator=FilterOperator.EQUALS.value,
                value="ACTIVE",
            ),
        ),
        order_by=(GuidedOrderInput(field="Customer.country_code"),),
        limit=500,
    ),
    "delivered-orders-by-day": GuidedRequestInput(
        primary_entity="Shipment",
        dimensions=(
            GuidedDimensionInput(
                field="Shipment.delivered_at",
                grain="day",
            ),
        ),
        metrics=(
            GuidedMetricInput(
                operation=MetricOperation.COUNT_DISTINCT.value,
                field="Shipment.order_key",
            ),
        ),
        filters=(
            GuidedFilterInput(
                field="Shipment.shipment_status",
                operator=FilterOperator.EQUALS.value,
                value="DELIVERED",
            ),
        ),
        order_by=(GuidedOrderInput(field="Shipment.delivered_at"),),
        limit=500,
    ),
}
